"""Apply one PC's Ozon advertisement sync delta to the shared database."""
from datetime import date, datetime
import json
import re

from sqlalchemy import Date, DateTime, delete, insert, select, update
from sqlalchemy.orm import Session

from erp.core.config import get_settings
from erp.core.errors import ConflictError
from erp.core.jsonutil import loads
from erp.models import (AdCampaign, AdCampaignDailyMetric, AdCampaignSku,
                        AdDailyMetric, AdSku, AdSkuState, AdSkuStateHistory,
                        AdThresholdVersion, Job)
from erp.services.ad_desktop_snapshot import sync_source_fingerprint


TABLES = (AdSku, AdCampaign, AdCampaignSku, AdCampaignDailyMetric,
          AdDailyMetric, AdThresholdVersion, AdSkuState, AdSkuStateHistory)
MAX_ROWS = 500_000


def completion_summary(job: Job) -> dict:
    receipt = loads(job.external_ref, {})
    if receipt.get('kind') != 'ad_sync':
        raise ConflictError('广告同步结果尚未被服务器接收')
    return {key: receipt[key] for key in ('store_id', 'changed', 'result_sha256', 'result_bytes')}


def _ids(session: Session, model, store_id: str) -> set[str]:
    if model in (AdSku, AdCampaign, AdCampaignDailyMetric, AdThresholdVersion):
        condition = model.store_id == store_id
    elif model == AdCampaignSku:
        condition = AdCampaignSku.ad_campaign_id.in_(select(AdCampaign.id).where(AdCampaign.store_id == store_id))
    else:
        condition = model.ad_sku_id.in_(select(AdSku.id).where(AdSku.store_id == store_id))
    return set(session.scalars(select(model.id).where(condition)))


def _coerce(model, row: dict) -> dict:
    fields = model.__table__.columns
    if set(row) != {column.name for column in fields}:
        raise ConflictError('广告同步字段与服务器表结构不一致')
    converted = dict(row)
    for column in fields:
        value = converted[column.name]
        if isinstance(value, str) and isinstance(column.type, DateTime):
            converted[column.name] = datetime.fromisoformat(value)
        elif isinstance(value, str) and isinstance(column.type, Date):
            converted[column.name] = date.fromisoformat(value)
    return converted


def apply_sync_result(session: Session, job: Job, payload: dict,
                      digest: str, size: int) -> dict:
    """Validate the frozen source and every row before applying any change."""
    previous = loads(job.external_ref, {})
    if previous.get('kind') == 'ad_sync':
        if previous.get('result_sha256') != digest:
            raise ConflictError('广告同步任务已经写入不同结果')
        return completion_summary(job)
    if previous:
        raise ConflictError('广告同步任务已有其他结果')
    if not re.fullmatch(r'[a-f0-9]{64}', digest) or size <= 0:
        raise ConflictError('广告同步结果校验值无效')
    store_id = loads(job.payload_json, {}).get('store_id')
    archive = get_settings().data_dir / 'ad_task_snapshots'
    try:
        snapshot_hash = (archive / f'{job.id}.sha256').read_text(encoding='ascii').strip()
        source_hash = (archive / f'{job.id}.source.sha256').read_text(encoding='ascii').strip()
    except OSError as error:
        raise ConflictError('广告同步来源快照不存在') from error
    changes = payload.get('changes')
    summary = payload.get('summary')
    if (payload.get('schema') != 3 or payload.get('store_id') != store_id
            or payload.get('snapshot_sha256') != snapshot_hash
            or payload.get('source_sha256') != source_hash
            or not isinstance(changes, dict)
            or set(changes) != {model.__tablename__ for model in TABLES}
            or not isinstance(summary, dict)
            or summary.get('mode') != 'live_reviewed'
            or summary.get('executed') is not False):
        raise ConflictError('广告同步结果与任务来源不一致')
    if sync_source_fingerprint(session, store_id) != source_hash:
        raise ConflictError('店铺广告数据或接口配置已变化，请重新发起同步')
    existing = {model.__tablename__: _ids(session, model, store_id) for model in TABLES}
    normalized = {}
    changed = 0
    for model in TABLES:
        name = model.__tablename__
        section = changes[name]
        if not isinstance(section, dict) or set(section) != {'upserts', 'deletes'}:
            raise ConflictError('广告同步差异格式无效')
        upserts, deleted = section['upserts'], section['deletes']
        if not isinstance(upserts, list) or not isinstance(deleted, list):
            raise ConflictError('广告同步差异格式无效')
        changed += len(upserts) + len(deleted)
        if changed > MAX_ROWS:
            raise ConflictError('广告同步变更数量超过限制')
        try:
            rows = [_coerce(model, row) for row in upserts if isinstance(row, dict)]
        except (ValueError, TypeError, KeyError) as error:
            raise ConflictError('广告同步日期或字段无效') from error
        if len(rows) != len(upserts) or len({row['id'] for row in rows}) != len(rows):
            raise ConflictError('广告同步记录重复或格式无效')
        if (any(not isinstance(identity, str) for identity in deleted)
                or len(set(deleted)) != len(deleted)
                or (deleted and name != 'ad_campaign_daily_metrics')
                or set(deleted) - existing[name]):
            raise ConflictError('广告同步删除范围无效')
        ids = {row['id'] for row in rows}
        collisions = set(session.scalars(select(model.id).where(model.id.in_(ids)))) - existing[name]
        if collisions:
            raise ConflictError('广告同步记录编号与其他店铺冲突')
        normalized[name] = (rows, deleted)
    sku_ids = existing['ad_skus'] | {row['id'] for row in normalized['ad_skus'][0]}
    campaign_ids = existing['ad_campaigns'] | {row['id'] for row in normalized['ad_campaigns'][0]}
    threshold_ids = existing['ad_threshold_versions'] | {row['id'] for row in normalized['ad_threshold_versions'][0]}
    for model in TABLES:
        for row in normalized[model.__tablename__][0]:
            if 'store_id' in row and row['store_id'] != store_id:
                raise ConflictError('广告同步记录包含其他店铺')
            if 'ad_sku_id' in row and row['ad_sku_id'] not in sku_ids:
                raise ConflictError('广告同步记录引用了其他店铺商品')
            if 'ad_campaign_id' in row and row['ad_campaign_id'] not in campaign_ids:
                raise ConflictError('广告同步记录引用了其他店铺计划')
            if 'threshold_version_id' in row and row['threshold_version_id'] and row['threshold_version_id'] not in threshold_ids:
                raise ConflictError('广告同步记录引用了其他店铺阈值')
    for model in TABLES:
        name = model.__tablename__
        rows, deleted = normalized[name]
        if deleted:
            session.execute(delete(model).where(model.id.in_(deleted)))
        for row in rows:
            if row['id'] in existing[name]:
                session.execute(update(model).where(model.id == row['id']).values(**{
                    key: value for key, value in row.items() if key != 'id'}))
            else:
                session.execute(insert(model).values(**row))
    receipt = {'kind': 'ad_sync', 'store_id': store_id, 'changed': changed,
               'result_sha256': digest, 'result_bytes': size}
    job.external_ref = json.dumps(receipt, separators=(',', ':'))
    session.flush()
    return completion_summary(job)
