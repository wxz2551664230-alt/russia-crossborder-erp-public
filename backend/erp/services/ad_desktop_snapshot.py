"""Prepare only one store's advertisement analysis inputs for its owner's PC."""
from pathlib import Path
from contextlib import closing
import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from erp.core.errors import ConflictError
from erp.core.config import get_settings
from erp.core.jsonutil import dumps, loads
from erp.models import (Base, Store, SystemSetting, AdSku, AdDailyMetric,
                        AdCampaign, AdCampaignSku, AdCampaignDailyMetric,
                        AdSkuState, AdThresholdVersion, AdExecutionBatch, AdAction)
from erp.services.config_resolution import system_defaults


def sync_source_fingerprint(session: Session, store_id: str) -> str:
    """Include public configuration and decrypted credentials in the CAS digest."""
    from erp.services.config_resolution import build_job_snapshot, resolve_job_snapshot
    config = resolve_job_snapshot(build_job_snapshot(session, store_id=store_id))
    sha = hashlib.sha256()
    sha.update(analysis_source_fingerprint(session, store_id).encode('ascii'))
    sha.update(json.dumps(config, sort_keys=True, default=str, ensure_ascii=False).encode('utf-8'))
    return sha.hexdigest()


def analysis_source_fingerprint(session: Session, store_id: str) -> str:
    """Detect changed source rows before applying a result from an older PC snapshot."""
    sha = hashlib.sha256()
    sha.update(json.dumps(system_defaults(session).get('financial_policy'), sort_keys=True, default=str).encode())
    store = session.get(Store, store_id)
    if not store:
        raise ConflictError('广告店铺不存在')
    sha.update(json.dumps({'id': store.id, 'platform': store.platform, 'enabled': store.enabled}, sort_keys=True).encode())
    sku_ids = select(AdSku.id).where(AdSku.store_id == store_id)
    campaign_ids = select(AdCampaign.id).where(AdCampaign.store_id == store_id)
    selections = (
        (AdSku, AdSku.store_id == store_id),
        (AdCampaign, AdCampaign.store_id == store_id),
        (AdDailyMetric, AdDailyMetric.ad_sku_id.in_(sku_ids)),
        (AdCampaignDailyMetric, AdCampaignDailyMetric.store_id == store_id),
        (AdCampaignSku, AdCampaignSku.ad_campaign_id.in_(campaign_ids)),
        (AdThresholdVersion, AdThresholdVersion.store_id == store_id),
        (AdSkuState, AdSkuState.ad_sku_id.in_(sku_ids)),
    )
    for model, where in selections:
        table = model.__table__
        primary = list(table.primary_key.columns)
        sha.update(table.name.encode())
        for row in session.execute(select(table).where(where).order_by(*primary).execution_options(yield_per=500)).mappings():
            sha.update(json.dumps(dict(row), sort_keys=True, default=str, ensure_ascii=False).encode('utf-8'))
            sha.update(b'\n')
    return sha.hexdigest()


def create_analysis_snapshot(session: Session, store_id: str, path: Path,
                             *, sync_input: bool = False) -> dict[str, int]:
    """Write a private SQLite input DB; no store credentials or unrelated rows."""
    store = session.get(Store, store_id)
    if not store or store.platform != 'ozon' or not store.enabled:
        raise ConflictError('Ozon 店铺不可用于广告任务')
    if path.exists():
        raise ConflictError('广告快照文件已经存在')
    target = create_engine('sqlite:///' + path.resolve().as_posix())
    counts = {}
    sku_ids = select(AdSku.id).where(AdSku.store_id == store_id)
    campaign_ids = select(AdCampaign.id).where(AdCampaign.store_id == store_id)
    selections = (
        (AdSku, AdSku.store_id == store_id),
        (AdCampaign, AdCampaign.store_id == store_id),
        (AdDailyMetric, AdDailyMetric.ad_sku_id.in_(sku_ids)),
        (AdCampaignDailyMetric, AdCampaignDailyMetric.store_id == store_id),
        (AdCampaignSku, AdCampaignSku.ad_campaign_id.in_(campaign_ids)),
        (AdThresholdVersion, AdThresholdVersion.store_id == store_id),
        (AdSkuState, AdSkuState.ad_sku_id.in_(sku_ids)),
    )
    try:
        Base.metadata.create_all(target)
        with target.begin() as destination:
            safe_store = {column.name: getattr(store, column.name) for column in Store.__table__.columns}
            safe_store.update(secrets_encrypted='', connection_id=None, config_json='{}')
            if not sync_input:
                safe_store.update(external_account_id=None)
            destination.execute(insert(Store), [safe_store])
            defaults = {'financial_policy': system_defaults(session).get('financial_policy')}
            destination.execute(insert(SystemSetting), [{'key': 'defaults', 'value_json': dumps(defaults)}])
            for model, where in selections:
                count = 0
                batch = []
                rows = session.execute(select(model.__table__).where(where).execution_options(yield_per=500)).mappings()
                for row in rows:
                    batch.append(dict(row))
                    if len(batch) == 500:
                        destination.execute(insert(model), batch)
                        count += len(batch)
                        batch.clear()
                if batch:
                    destination.execute(insert(model), batch)
                    count += len(batch)
                counts[model.__tablename__] = count
        with closing(sqlite3.connect(path)) as check:
            if check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('广告任务快照校验失败')
        return counts
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        target.dispose()


def create_execution_snapshot(session: Session, batch: AdExecutionBatch, path: Path) -> dict[str, int]:
    """Include only one approved batch with its store data for original PC execution."""
    if batch.status != 'executing' or batch.active_store != batch.store_id:
        raise ConflictError('广告批次尚未锁定，不能创建执行快照')
    counts = create_analysis_snapshot(session, batch.store_id, path)
    target = create_engine('sqlite:///' + path.resolve().as_posix())
    try:
        row = {column.name: getattr(batch, column.name) for column in AdExecutionBatch.__table__.columns}
        row.update(status='preview', active_store=None, results_json='[]')
        with target.begin() as destination:
            destination.execute(insert(AdExecutionBatch), [row])
        counts['ad_execution_batches'] = 1
        return counts
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        target.dispose()


def create_reconcile_snapshot(session: Session, batch: AdExecutionBatch, path: Path) -> dict[str, int]:
    """Freeze one uncertain batch and its audits for read-only platform checking."""
    if batch.status not in {'unknown', 'executing'} or batch.active_store != batch.store_id:
        raise ConflictError('广告批次无需核对或店铺锁已释放')
    counts = create_analysis_snapshot(session, batch.store_id, path)
    target = create_engine('sqlite:///' + path.resolve().as_posix())
    try:
        rows = loads(batch.results_json, [])
        audit_ids = {row.get('audit_id') for row in rows if isinstance(row, dict) and row.get('audit_id')}
        audits = [session.get(AdAction, audit_id) for audit_id in audit_ids]
        if any(item is None for item in audits):
            raise ConflictError('待核对广告批次缺少操作审计')
        batch_row = {column.name: getattr(batch, column.name) for column in AdExecutionBatch.__table__.columns}
        with target.begin() as destination:
            destination.execute(insert(AdExecutionBatch), [batch_row])
            if audits:
                destination.execute(insert(AdAction), [
                    {column.name: getattr(item, column.name) for column in AdAction.__table__.columns}
                    for item in audits])
        counts['ad_execution_batches'] = 1
        counts['ad_actions'] = len(audits)
        return counts
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        target.dispose()


def execution_source_fingerprint(session: Session, batch: AdExecutionBatch) -> str:
    expires = batch.expires_at
    expires = (expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None
               else expires.astimezone(timezone.utc))
    value = {'batch_id': batch.id, 'store_id': batch.store_id, 'actor': batch.actor,
             'action': batch.action, 'preview_json': batch.preview_json,
             'expires_at': expires.isoformat()}
    digest = hashlib.sha256(analysis_source_fingerprint(session, batch.store_id).encode('ascii'))
    digest.update(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8'))
    return digest.hexdigest()


def reconcile_source_fingerprint(session: Session, batch: AdExecutionBatch) -> str:
    """Bind a read-only reconciliation to the exact uncertain receipt and audits."""
    rows = loads(batch.results_json, [])
    audit_ids = sorted({row.get('audit_id') for row in rows
                        if isinstance(row, dict) and row.get('audit_id')})
    audits = [session.get(AdAction, audit_id) for audit_id in audit_ids]
    if any(item is None for item in audits):
        raise ConflictError('待核对广告批次缺少操作审计')
    def canonical(value):
        if isinstance(value, datetime):
            value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
            return value.isoformat()
        return str(value)

    value = {'status': batch.status, 'active_store': batch.active_store,
             'results_json': batch.results_json,
             'audits': [{column.name: canonical(getattr(item, column.name))
                         for column in AdAction.__table__.columns}
                        for item in audits]}
    digest = hashlib.sha256(execution_source_fingerprint(session, batch).encode('ascii'))
    digest.update(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8'))
    return digest.hexdigest()


def analysis_download(session: Session, job_id: str, store_id: str,
                      *, sync_input: bool = False,
                      execution_batch: AdExecutionBatch | None = None,
                      reconcile_batch: AdExecutionBatch | None = None) -> tuple[Path, str, str]:
    """Freeze one task's input once; retries receive the same bytes."""
    if len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
        raise ConflictError('广告任务编号无效')
    directory = get_settings().data_dir / 'ad_task_snapshots'
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / f'{job_id}.sqlite.gz'
    digest_file = directory / f'{job_id}.sha256'
    source_file = directory / f'{job_id}.source.sha256'
    if archive.exists() and digest_file.exists() and source_file.exists():
        digest = digest_file.read_text(encoding='ascii').strip()
        if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
            raise ConflictError('广告任务快照校验失败')
        return archive, digest, source_file.read_text(encoding='ascii').strip()
    if archive.exists() or digest_file.exists() or source_file.exists():
        raise ConflictError('广告任务快照不完整')
    if sum((bool(sync_input), execution_batch is not None, reconcile_batch is not None)) > 1:
        raise ConflictError('广告快照类型冲突')
    source_digest = (reconcile_source_fingerprint(session, reconcile_batch) if reconcile_batch is not None else
                     execution_source_fingerprint(session, execution_batch) if execution_batch is not None else
                     sync_source_fingerprint(session, store_id) if sync_input else
                     analysis_source_fingerprint(session, store_id))
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        raw = Path(temporary) / 'input.sqlite'
        packed = Path(temporary) / 'input.sqlite.gz'
        if reconcile_batch is not None:
            if reconcile_batch.store_id != store_id:
                raise ConflictError('广告批次与店铺不一致')
            create_reconcile_snapshot(session, reconcile_batch, raw)
        elif execution_batch is not None:
            if execution_batch.store_id != store_id:
                raise ConflictError('广告批次与店铺不一致')
            create_execution_snapshot(session, execution_batch, raw)
        else:
            create_analysis_snapshot(session, store_id, raw, sync_input=sync_input)
        with raw.open('rb') as source, packed.open('wb') as output:
            with gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as compressor:
                while chunk := source.read(1024 * 1024):
                    compressor.write(chunk)
        digest = hashlib.sha256(packed.read_bytes()).hexdigest()
        os.replace(packed, archive)
        digest_file.write_text(digest, encoding='ascii')
        source_file.write_text(source_digest, encoding='ascii')
    return archive, digest, source_digest


def cleanup_completed_snapshot(job_id: str) -> None:
    if len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
        raise ValueError('广告任务编号无效')
    directory = get_settings().data_dir / 'ad_task_snapshots'
    for suffix in ('.sqlite.gz', '.sha256', '.source.sha256'):
        (directory / (job_id + suffix)).unlink(missing_ok=True)
