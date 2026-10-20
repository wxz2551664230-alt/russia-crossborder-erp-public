"""Prepare a task-local store for the unchanged Ozon advertising sync."""
from pathlib import Path
from contextlib import closing
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys


def prepare_sync_database(database: Path, store_id: str, config: dict) -> None:
    """Re-encrypt only this store's credentials with the PC task key."""
    if (not os.environ.get('ERP_SECRET_KEY') or
            Path(os.environ.get('ERP_DATA_DIR', '')).resolve() != database.parent.resolve()):
        raise ValueError('广告同步必须在独立任务目录和临时密钥中运行')
    if not re.fullmatch(r'[a-f0-9]{32}', store_id) or config.get('store_id') != store_id:
        raise ValueError('广告同步店铺不匹配')
    secrets = config.get('secrets')
    if not isinstance(secrets, dict) or not secrets.get('performance_client_id') or not secrets.get('performance_client_secret'):
        raise ValueError('广告同步缺少 Ozon 广告接口凭据')
    if not (secrets.get('client_id') or config.get('external_account_id')) or not secrets.get('api_key'):
        raise ValueError('广告同步缺少 Ozon 卖家接口凭据')
    runtime = Path(__file__).parent / 'runtime'
    sys.path.insert(0, str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2] / 'backend'))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from erp.core.jsonutil import dumps
    from erp.core.security import vault
    from erp.models import Store
    engine = create_engine('sqlite:///' + database.resolve().as_posix())
    try:
        with Session(engine) as session:
            store = session.get(Store, store_id)
            if not store or store.platform != 'ozon':
                raise ValueError('广告同步快照未包含目标 Ozon 店铺')
            public = {key: value for key, value in config.items() if key != 'secrets'}
            store.config_json = dumps(public)
            store.secrets_encrypted = vault.encrypt(secrets)
            store.external_account_id = str(config.get('external_account_id') or store.external_account_id or '')
            session.commit()
    finally:
        engine.dispose()


def scrub_sync_credentials(database: Path, store_id: str) -> None:
    """Remove temporary credentials before retaining a result or retry checkpoint."""
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(database)) as check:
        check.execute('update stores set secrets_encrypted=?, config_json=? where id=?', ('', '{}', store_id))
        check.commit()


def execute_sync_database(database: Path, store_id: str, days: int, job_id: str) -> dict:
    """Invoke the original sync on the initiating PC's isolated SQLite copy."""
    if not re.fullmatch(r'[a-f0-9]{32}', store_id) or not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise ValueError('广告同步任务范围无效')
    if not 1 <= days <= 62:
        raise ValueError('广告同步天数必须为 1 至 62')
    if (not os.environ.get('ERP_SECRET_KEY') or
            Path(os.environ.get('ERP_DATA_DIR', '')).resolve() != database.parent.resolve()):
        raise ValueError('广告同步必须在独立任务目录和临时密钥中运行')
    runtime = Path(__file__).parent / 'runtime'
    sys.path.insert(0, str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2] / 'backend'))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from erp.api.routes.ad_optimization import sync_from_ozon
    engine = create_engine('sqlite:///' + database.resolve().as_posix())
    try:
        with Session(engine) as session:
            try:
                return sync_from_ozon(store_id=store_id, days=days,
                                      checkpoint_job_id=job_id, session=session)
            except BaseException:
                session.rollback()
                raise
    finally:
        engine.dispose()
        scrub_sync_credentials(database, store_id)


SYNC_TABLES = (
    'ad_skus', 'ad_campaigns', 'ad_campaign_skus', 'ad_campaign_daily_metrics',
    'ad_daily_metrics', 'ad_threshold_versions', 'ad_sku_states',
    'ad_sku_state_history',
)


def export_sync_delta(database: Path, snapshot: Path, result: Path, *,
                      store_id: str, snapshot_sha256: str, source_sha256: str,
                      summary: dict) -> dict[str, int]:
    """Export changed rows from a scrubbed task DB, never the task's credentials."""
    if not re.fullmatch(r'[a-f0-9]{32}', store_id):
        raise ValueError('广告同步店铺无效')
    if not all(re.fullmatch(r'[a-f0-9]{64}', value) for value in
               (snapshot_sha256, source_sha256)):
        raise ValueError('广告同步来源校验值无效')
    if result.exists():
        raise ValueError('广告同步结果文件已经存在')
    if hashlib.sha256(snapshot.read_bytes()).hexdigest() != snapshot_sha256:
        raise ValueError('广告同步输入快照校验失败')
    baseline = database.parent / (database.stem + '.baseline.sqlite')
    if baseline.exists():
        raise ValueError('广告同步基准文件已经存在')
    try:
        with gzip.open(snapshot, 'rb') as source, baseline.open('xb') as target:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            scrubbed = connection.execute('select secrets_encrypted, config_json from stores where id=?',
                                          (store_id,)).fetchone()
            if not scrubbed or tuple(scrubbed) != ('', '{}'):
                raise ValueError('广告同步凭据尚未清除')
            connection.execute('attach database ? as baseline', (str(baseline),))
            if connection.execute('pragma main.integrity_check').fetchone()[0] != 'ok':
                raise ValueError('广告同步结果数据库损坏')
            if connection.execute('pragma baseline.integrity_check').fetchone()[0] != 'ok':
                raise ValueError('广告同步输入数据库损坏')
            changes = {}
            counts = {}
            for table in SYNC_TABLES:
                columns = [row['name'] for row in connection.execute(f'pragma main.table_info({table})')]
                original = [row['name'] for row in connection.execute(f'pragma baseline.table_info({table})')]
                if not columns or columns != original or 'id' not in columns:
                    raise ValueError('广告同步数据表结构不一致')
                scope = ('current.store_id' if 'store_id' in columns else
                         'owner.store_id' if table in ('ad_campaign_skus',) else
                         'owner.store_id')
                parent = ('ad_campaigns' if table == 'ad_campaign_skus' else 'ad_skus')
                parent_key = ('ad_campaign_id' if table == 'ad_campaign_skus' else 'ad_sku_id')
                join = '' if 'store_id' in columns else (
                    f' join main.{parent} owner on owner.id=current.{parent_key}')
                diff = ' or '.join(f'current."{column}" is not prior."{column}"'
                                   for column in columns if column != 'id')
                query = (f'select current.* from main.{table} current{join} '
                         f'left join baseline.{table} prior on prior.id=current.id '
                         f'where {scope}=? and (prior.id is null or {diff})')
                upserts = [dict(row) for row in connection.execute(query, (store_id,))]
                if 'store_id' in columns:
                    deleted_query = (f'select prior.id from baseline.{table} prior '
                                     f'left join main.{table} current on current.id=prior.id '
                                     'where prior.store_id=? and current.id is null')
                else:
                    deleted_query = (f'select prior.id from baseline.{table} prior '
                                     f'join baseline.{parent} owner on owner.id=prior.{parent_key} '
                                     f'left join main.{table} current on current.id=prior.id '
                                     'where owner.store_id=? and current.id is null')
                deletes = [row[0] for row in connection.execute(deleted_query, (store_id,))]
                if deletes and table != 'ad_campaign_daily_metrics':
                    raise ValueError(f'广告同步意外删除了 {table} 的记录')
                changes[table] = {'upserts': upserts, 'deletes': deletes}
                counts[table] = len(upserts) + len(deletes)
        payload = {'schema': 3, 'store_id': store_id,
                   'snapshot_sha256': snapshot_sha256,
                   'source_sha256': source_sha256, 'summary': summary,
                   'changes': changes}
        temporary = result.with_name(result.name + '.tmp')
        try:
            with temporary.open('wb') as output:
                with gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as packed:
                    packed.write(json.dumps(payload, ensure_ascii=False, default=str,
                                            separators=(',', ':')).encode('utf-8'))
            os.replace(temporary, result)
        finally:
            temporary.unlink(missing_ok=True)
        return counts
    finally:
        baseline.unlink(missing_ok=True)


def run(client, endpoint: str, job: dict, root: Path, lost) -> dict:
    """Run once on the initiating PC, then resume only the journaled upload."""
    import base64
    import gzip
    import hashlib
    import json
    import subprocess
    from cryptography.fernet import Fernet
    from ad_worker import download_snapshot
    from erp_desktop import TemporaryConnectionError

    if job.get('type') != 'ad_optimization_sync' or not re.fullmatch(r'[a-f0-9]{32}', job.get('id', '')):
        raise ValueError('广告同步任务类型无效')
    store_id = job.get('payload', {}).get('store_id', '')
    days = job.get('payload', {}).get('days', 62)
    if not re.fullmatch(r'[a-f0-9]{32}', store_id) or type(days) is not int or not 1 <= days <= 62:
        raise ValueError('广告同步店铺或天数无效')
    work = root / 'ad-tasks' / job['id']
    for retry in range(3):
        try:
            database, snapshot_hash, source_hash = download_snapshot(
                client, endpoint, job['attempts'], work, lost, sync_input=True)
            break
        except TemporaryConnectionError:
            if retry == 2 or lost.wait(5):
                raise
    result_file = work / 'sync-result.json.gz'
    started = work / 'sync-started'
    summary_file = work / 'sync-summary.json'
    headers = {'X-Worker-Attempt': str(job['attempts'])}
    if not result_file.exists():
        if not summary_file.exists():
            if started.exists():
                raise ValueError('广告同步上次执行结果不明，请保留任务目录核对后恢复')
            config, _ = client.request('GET', endpoint + '/ad-sync-credentials',
                                       extra_headers=headers)
            if config.get('store_id') != store_id:
                raise ValueError('广告同步凭据店铺不匹配')
            started.write_text(job['id'], encoding='ascii')
            env = os.environ.copy()
            env['ERP_DATA_DIR'] = str(work.resolve())
            env['ERP_DATABASE_URL'] = 'sqlite:///' + database.resolve().as_posix()
            env['ERP_SECRET_KEY'] = Fernet.generate_key().decode('ascii')
            env['ERP_RUNTIME_MODE'] = 'worker'
            env['PYTHONIOENCODING'] = 'utf-8'
            runtime = Path(__file__).parent / 'runtime'
            env['PYTHONPATH'] = os.pathsep.join((str(runtime if runtime.is_dir() else
                                                     Path(__file__).resolve().parents[2] / 'backend'),
                                                env.get('PYTHONPATH', '')))
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), str(database.resolve()),
                 store_id, str(days), job['id']],
                input=json.dumps(config['config']), text=True, encoding='utf-8',
                errors='replace', capture_output=True,
                env=env, timeout=7200)
            if completed.returncode:
                lines = [line.strip() for line in completed.stderr.splitlines() if line.strip()]
                reason = lines[-1] if lines else '子进程未返回错误信息'
                for secret in config['config'].get('secrets', {}).values():
                    if isinstance(secret, str) and secret:
                        reason = reason.replace(secret, '[已隐藏]')
                (work / 'sync-error.txt').write_text(reason[:1000], encoding='utf-8')
                raise ValueError('广告同步执行中断，结果可能未知：' + reason[:180])
            try:
                summary = json.loads(completed.stdout)
            except (ValueError, TypeError) as error:
                raise ValueError('广告同步本机结果格式无效；已保留任务目录') from error
            summary_file.write_text(json.dumps(summary, ensure_ascii=False), encoding='utf-8')
        else:
            summary = json.loads(summary_file.read_text(encoding='utf-8'))
        export_sync_delta(database, work / 'input.sqlite.gz', result_file,
                          store_id=store_id, snapshot_sha256=snapshot_hash,
                          source_sha256=source_hash, summary=summary)
    packed = result_file.read_bytes()
    if not packed or len(packed) > 128 * 1024 * 1024:
        raise ValueError('广告同步结果超过上传限制')
    payload = json.loads(gzip.decompress(packed))
    if (payload.get('store_id') != store_id
            or payload.get('snapshot_sha256') != snapshot_hash
            or payload.get('source_sha256') != source_hash):
        raise ValueError('本机广告同步结果与任务来源不一致')
    digest = hashlib.sha256(packed).hexdigest()
    chunk_size = 40 * 1024
    total = (len(packed) + chunk_size - 1) // chunk_size
    if total > 4000:
        raise ValueError('广告同步结果分片数量超过限制')
    for index in range(total):
        if lost.is_set():
            raise ValueError('广告同步租约失效，已保留本机结果')
        part = packed[index * chunk_size:(index + 1) * chunk_size]
        body = {'index': index, 'total': total, 'sha256': digest,
                'content': base64.b64encode(part).decode('ascii')}
        for retry in range(3):
            try:
                client.request('POST', endpoint + '/ad-sync-chunk', body, extra_headers=headers)
                break
            except TemporaryConnectionError:
                if retry == 2 or lost.wait(5):
                    raise
    for retry in range(3):
        if lost.is_set():
            raise ValueError('广告同步租约失效，已保留本机结果')
        try:
            receipt = client.request('POST', endpoint + '/ad-sync-result',
                                     {'sha256': digest}, extra_headers=headers)[0]
            changed = sum(len(section['upserts']) + len(section['deletes'])
                          for section in payload['changes'].values())
            expected = {'store_id': store_id, 'changed': changed,
                        'result_sha256': digest, 'result_bytes': len(packed)}
            if receipt != expected:
                raise ValueError('服务器广告同步回执与本机结果不一致')
            return receipt
        except TemporaryConnectionError:
            if retry == 2 or lost.wait(5):
                raise
    raise AssertionError('广告同步回执重试次数异常')


if __name__ == '__main__':
    import json
    database, store_id, days, job_id = sys.argv[1:5]
    config = json.load(sys.stdin)
    prepare_sync_database(Path(database), store_id, config)
    print(json.dumps(execute_sync_database(Path(database), store_id, int(days), job_id),
                     default=str, ensure_ascii=False))
