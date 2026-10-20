"""Run the original advertisement analysis against one leased store snapshot."""
import gzip
import base64
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid

from erp_desktop import Client, NoRedirect, TemporaryConnectionError

MAX_COMPRESSED = 128 * 1024 * 1024
MAX_UNCOMPRESSED = 512 * 1024 * 1024
MIN_FREE = 512 * 1024 * 1024


def download_snapshot(client: Client, endpoint: str, attempt: int, work: Path, lost,
                      *, sync_input: bool = False,
                      execution_input: bool = False,
                      reconcile_input: bool = False) -> tuple[Path, str, str]:
    """Only fetch through the authenticated, leased personal-device endpoint."""
    work.mkdir(parents=True, exist_ok=True)
    archive = work / 'input.sqlite.gz'
    database = work / 'input.sqlite'
    digest_path = work / 'input.sha256'
    source_path = work / 'source.sha256'
    if archive.exists() and database.exists() and digest_path.exists() and source_path.exists():
        expected = digest_path.read_text(encoding='ascii').strip()
        if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise ValueError('本机广告快照校验失败，请保留任务目录核对')
        with closing(sqlite3.connect(database)) as check:
            if check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('本机广告数据库完整性校验失败')
        return database, expected, source_path.read_text(encoding='ascii').strip()
    if archive.exists() or database.exists() or digest_path.exists() or source_path.exists():
        raise ValueError('本机广告任务快照不完整，请保留目录核对')
    if shutil.disk_usage(work).free < MAX_UNCOMPRESSED + MIN_FREE:
        raise ValueError('本机空间不足，无法下载广告分析数据')
    if not endpoint.startswith('/execution-devices/local/') or not re.fullmatch(
        r'/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}', endpoint):
        raise ValueError('广告任务接口路径无效')
    if sum((bool(sync_input), bool(execution_input), bool(reconcile_input))) > 1:
        raise ValueError('广告快照类型冲突')
    suffix = ('/ad-reconcile-snapshot' if reconcile_input else
              '/ad-execution-snapshot' if execution_input else
              '/ad-sync-snapshot' if sync_input else '/ad-analysis-snapshot')
    request = urllib.request.Request(
        client.base + '/api/v1' + endpoint + suffix,
        headers={'Cookie': 'erp_session=' + client.profile['cookie'],
                 'X-Worker-Attempt': str(attempt), 'Accept': 'application/gzip'},
        method='GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    temporary = work / (uuid.uuid4().hex + '.partial')
    extracted = work / (uuid.uuid4().hex + '.sqlite.partial')
    try:
        with opener.open(request, timeout=120) as response, temporary.open('xb') as output:
            expected = response.headers.get('X-Content-SHA256', '')
            source_digest = response.headers.get('X-Source-SHA256', '')
            if not re.fullmatch(r'[a-f0-9]{64}', expected) or not re.fullmatch(r'[a-f0-9]{64}', source_digest):
                raise ValueError('广告数据包缺少校验值')
            actual = hashlib.sha256()
            size = 0
            while chunk := response.read(1024 * 1024):
                if lost.is_set():
                    raise ValueError('任务租约失效，停止下载广告数据')
                size += len(chunk)
                if size > MAX_COMPRESSED:
                    raise ValueError('广告数据包超过大小限制')
                actual.update(chunk)
                output.write(chunk)
            if not size or actual.hexdigest() != expected:
                raise ValueError('广告数据包 SHA256 校验失败')
        with gzip.open(temporary, 'rb') as source, extracted.open('xb') as output:
            size = 0
            while chunk := source.read(1024 * 1024):
                if lost.is_set():
                    raise ValueError('任务租约失效，停止解压广告数据')
                size += len(chunk)
                if size > MAX_UNCOMPRESSED:
                    raise ValueError('广告数据解压超过大小限制')
                output.write(chunk)
        with closing(sqlite3.connect(extracted)) as check:
            if check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('广告数据包 SQLite 校验失败')
        os.replace(temporary, archive)
        os.replace(extracted, database)
        digest_path.write_text(expected, encoding='ascii')
        source_path.write_text(source_digest, encoding='ascii')
        return database, expected, source_digest
    except (urllib.error.URLError, TimeoutError) as error:
        raise TemporaryConnectionError('下载广告任务快照时连接暂时中断') from error
    finally:
        temporary.unlink(missing_ok=True)
        extracted.unlink(missing_ok=True)


def analyze_snapshot(database: Path, store_id: str) -> dict:
    """Execute unchanged ERP label and analysis functions in the isolated DB."""
    if not re.fullmatch(r'[a-f0-9]{32}', store_id):
        raise ValueError('广告店铺编号无效')
    runtime = Path(__file__).parent / 'runtime'
    sys.path.insert(0, str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2] / 'backend'))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from erp.services.ad_data_sync import assign_weak_labels_and_maturity
    from erp.services.ad_optimization import analyze_store
    engine = create_engine('sqlite:///' + database.resolve().as_posix())
    try:
        with Session(engine) as session:
            labels = assign_weak_labels_and_maturity(session, store_id)
            result = analyze_store(session, store_id)
            return {**result, 'labels': labels}
    finally:
        engine.dispose()


def backtest_snapshot(database: Path, store_id: str) -> dict:
    """Run the unchanged chronological replay in the task-local database."""
    if not re.fullmatch(r'[a-f0-9]{32}', store_id):
        raise ValueError('广告店铺编号无效')
    runtime = Path(__file__).parent / 'runtime'
    sys.path.insert(0, str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2] / 'backend'))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from erp.services.ad_optimization import run_backtest
    engine = create_engine('sqlite:///' + database.resolve().as_posix())
    try:
        with Session(engine) as session:
            result = run_backtest(session, store_id)
            return {'store_id': store_id, 'run_id': result.id, 'sample_size': result.sample_size}
    finally:
        engine.dispose()


def export_analysis_result(database: Path, summary: dict, snapshot_digest: str, source_digest: str,
                           destination: Path, *, plan: dict | None = None) -> dict:
    """Journal the exact derived rows for a later authenticated cloud apply."""
    store_id = summary.get('store_id', '')
    version = summary.get('version', '')
    if (not re.fullmatch(r'[a-f0-9]{32}', store_id) or not re.fullmatch(r'[a-f0-9]{64}', snapshot_digest)
            or not re.fullmatch(r'[a-f0-9]{64}', source_digest)):
        raise ValueError('广告分析结果缺少来源标识')
    if not isinstance(version, str) or not version.startswith('TV_'):
        raise ValueError('广告阈值版本无效')
    with closing(sqlite3.connect(database)) as check:
        check.row_factory = sqlite3.Row
        sku_ids = {row[0] for row in check.execute('select id from ad_skus where store_id=?', (store_id,))}
        def rows(sql, args=()):
            return [dict(row) for row in check.execute(sql, args)]
        thresholds = rows('select * from ad_threshold_versions where store_id=? and version=?', (store_id, version))
        threshold_ids = {row['id'] for row in thresholds}
        if not thresholds or len(threshold_ids) != len(thresholds):
            raise ValueError('广告阈值结果不完整')
        states = rows('select s.* from ad_sku_states s join ad_skus k on k.id=s.ad_sku_id where k.store_id=?', (store_id,))
        histories = rows('select h.* from ad_sku_state_history h join ad_skus k on k.id=h.ad_sku_id '
                         'where k.store_id=? and h.threshold_version_id in '
                         '(select id from ad_threshold_versions where store_id=? and version=?)',
                         (store_id, store_id, version))
        labels = rows('select id, weak_label, data_maturity from ad_skus where store_id=?', (store_id,))
        if (len(sku_ids) != summary.get('analyzed') or len(states) != len(sku_ids)
                or len(histories) != len(sku_ids) or len(labels) != len(sku_ids)
                or {row['ad_sku_id'] for row in states} != sku_ids
                or {row['ad_sku_id'] for row in histories} != sku_ids
                or any(row['threshold_version_id'] not in threshold_ids for row in states + histories)):
            raise ValueError('广告分析结果的 SKU 或阈值数量不一致')
    payload = {'schema': 1, 'store_id': store_id, 'snapshot_sha256': snapshot_digest,
               'source_sha256': source_digest,
               'summary': summary, 'thresholds': thresholds, 'states': states,
               'histories': histories, 'labels': labels}
    if plan is not None:
        if (not isinstance(plan, dict) or not isinstance(plan.get('state_ids'), list)
                or not isinstance(plan.get('items'), list) or not isinstance(plan.get('blocked'), list)):
            raise ValueError('广告预览计划格式无效')
        payload['preview_plan'] = plan
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.partial')
    if destination.exists() or temporary.exists():
        raise ValueError('广告分析结果文件已存在，请先核对原任务')
    try:
        with temporary.open('xb') as output:
            with gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as compressor:
                compressor.write(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        if temporary.stat().st_size > MAX_COMPRESSED:
            raise ValueError('广告分析结果超过传输上限')
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {'store_id': store_id, 'version': version, 'analyzed': len(states),
            'result_sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
            'result_bytes': destination.stat().st_size}


def export_backtest_result(database: Path, result: dict, snapshot_digest: str,
                           source_digest: str, destination: Path) -> dict:
    store_id = result.get('store_id', '')
    run_id = result.get('run_id', '')
    if (not re.fullmatch(r'[a-f0-9]{32}', store_id) or not re.fullmatch(r'[a-f0-9]{32}', run_id)
            or not re.fullmatch(r'[a-f0-9]{64}', snapshot_digest)
            or not re.fullmatch(r'[a-f0-9]{64}', source_digest)):
        raise ValueError('广告回测缺少来源标识')
    with closing(sqlite3.connect(database)) as check:
        check.row_factory = sqlite3.Row
        row = check.execute('select * from ad_backtest_runs where id=? and store_id=?',
                            (run_id, store_id)).fetchone()
        if not row or row['sample_size'] != result.get('sample_size'):
            raise ValueError('广告回测结果不存在或样本数量不一致')
        record = dict(row)
    payload = {'schema': 2, 'store_id': store_id, 'snapshot_sha256': snapshot_digest,
               'source_sha256': source_digest, 'backtest': record}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.partial')
    if destination.exists() or temporary.exists():
        raise ValueError('广告回测结果文件已存在')
    try:
        with temporary.open('xb') as output:
            with gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as compressor:
                compressor.write(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        if temporary.stat().st_size > 12 * 1024 * 1024:
            raise ValueError('广告回测结果超过上传限制')
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {'store_id': store_id, 'run_id': run_id, 'sample_size': record['sample_size'],
            'result_sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
            'result_bytes': destination.stat().st_size}


def run(client: Client, endpoint: str, job: dict, root: Path, lost) -> dict:
    """Resume the same local result after a disconnect; never rerun a finished analysis."""
    if not re.fullmatch(r'[a-f0-9]{32}', job.get('id', '')):
        raise ValueError('广告任务编号无效')
    store_id = job.get('payload', {}).get('store_id', '')
    work = root / 'ad-tasks' / job['id']
    work.mkdir(parents=True, exist_ok=True)
    backtest = job.get('type') == 'ad_optimization_backtest'
    if job.get('type') not in {'ad_optimization_analyze', 'ad_optimization_backtest'}:
        raise ValueError('广告任务类型无效')
    result_file = work / ('backtest-result.json.gz' if backtest else 'analysis-result.json.gz')
    database, snapshot_digest, source_digest = download_snapshot(client, endpoint, job['attempts'], work, lost)
    if not result_file.exists():
        summary = backtest_snapshot(database, store_id) if backtest else analyze_snapshot(database, store_id)
        if lost.is_set():
            raise ValueError('广告任务租约失效，停止回传')
        if backtest:
            export_backtest_result(database, summary, snapshot_digest, source_digest, result_file)
        else:
            export_analysis_result(database, summary, snapshot_digest, source_digest, result_file)
    packed = result_file.read_bytes()
    if len(packed) > 12 * 1024 * 1024:
        raise ValueError('广告分析回执超过上传限制')
    try:
        payload = json.loads(gzip.decompress(packed))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('本机广告回执损坏，请保留原任务目录') from error
    if (payload.get('snapshot_sha256') != snapshot_digest or payload.get('source_sha256') != source_digest
            or payload.get('store_id') != store_id):
        raise ValueError('本机广告回执与任务快照不一致')
    expected = ({'store_id': store_id, 'run_id': payload['backtest']['id'],
                 'sample_size': payload['backtest']['sample_size'],
                 'result_sha256': hashlib.sha256(packed).hexdigest(), 'result_bytes': len(packed)}
                if backtest else
                {'store_id': store_id, 'version': payload['summary']['version'],
                 'analyzed': payload['summary']['analyzed'],
                 'result_sha256': hashlib.sha256(packed).hexdigest(), 'result_bytes': len(packed)})
    headers = {'X-Worker-Attempt': str(job['attempts'])}
    from erp_desktop import TemporaryConnectionError
    for attempt in range(3):
        if lost.is_set():
            raise ValueError('广告任务租约失效，已保留本机分析结果')
        try:
            received = client.request('POST', endpoint + ('/ad-backtest-result' if backtest else '/ad-analysis-result'),
                                      {'content': base64.b64encode(packed).decode('ascii')},
                                      extra_headers=headers)[0]
            if received != expected:
                raise ValueError('服务器广告回执与本机结果不一致')
            return received
        except TemporaryConnectionError:
            if attempt == 2:
                raise
            if lost.wait(5):
                raise ValueError('广告任务租约失效，已保留本机分析结果')
    raise AssertionError('广告回执重试次数异常')


def cleanup_completed_input(root: Path, job_id: str) -> None:
    """After cloud completion, retain only the small result receipt for audit."""
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise ValueError('广告任务编号无效')
    work = root / 'ad-tasks' / job_id
    for name in ('input.sqlite', 'input.sqlite.gz', 'input.sha256', 'source.sha256'):
        (work / name).unlink(missing_ok=True)
