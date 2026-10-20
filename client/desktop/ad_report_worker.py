"""Generate the original advertisement report files on the initiating PC."""
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

from ad_worker import download_snapshot
from erp_desktop import TemporaryConnectionError

MAX_ARCHIVE = 12 * 1024 * 1024


def create_report_archive(database: Path, store_id: str, folder: Path,
                          destination: Path) -> dict:
    if not re.fullmatch(r'[a-f0-9]{32}', store_id):
        raise ValueError('广告报告店铺编号无效')
    if destination.exists():
        raise ValueError('广告报告结果文件已经存在')
    runtime = Path(__file__).parent / 'runtime'
    sys.path.insert(0, str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2] / 'backend'))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from erp.services.ad_analysis import write_ad_reports

    engine = create_engine('sqlite:///' + database.resolve().as_posix())
    try:
        with Session(engine) as session:
            result = write_ad_reports(session, store_id, root=folder)
    finally:
        engine.dispose()
    files = sorted(path for path in folder.rglob('*') if path.is_file())
    if not files or len(files) > 60:
        raise ValueError('广告报告文件数量无效')
    summary = {'store_id': store_id,
               'date_from': str(result['analysis']['audit']['date_from']),
               'date_to': str(result['analysis']['audit']['date_to']),
               'advertised_sku_count': result['analysis']['audit']['advertised_sku_count'],
               'candidate_counts': result['analysis']['candidate_counts'],
               'mode': 'live_reviewed', 'executed': False,
               'files': [path.relative_to(folder).as_posix() for path in files]}
    temporary = destination.with_suffix('.partial')
    try:
        with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, path.relative_to(folder).as_posix())
        if temporary.stat().st_size > MAX_ARCHIVE:
            raise ValueError('广告报告压缩包超过上传限制')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return summary


def run(client, endpoint: str, job: dict, root: Path, lost) -> dict:
    if job.get('type') != 'ad_optimization_reports' or not re.fullmatch(r'[a-f0-9]{32}', job.get('id', '')):
        raise ValueError('广告报告任务类型无效')
    store_id = job.get('payload', {}).get('store_id', '')
    work = root / 'ad-tasks' / job['id']
    database, snapshot_hash, source_hash = download_snapshot(
        client, endpoint, job['attempts'], work, lost)
    archive = work / 'reports.zip'
    summary_file = work / 'reports-summary.json'
    if not archive.exists():
        summary = create_report_archive(database, store_id, work / 'reports', archive)
        summary_file.write_text(json.dumps(summary, ensure_ascii=False), encoding='utf-8')
    else:
        if not summary_file.exists():
            raise ValueError('本机广告报告回执不完整')
        summary = json.loads(summary_file.read_text(encoding='utf-8'))
    content = archive.read_bytes()
    if not content or len(content) > MAX_ARCHIVE or summary.get('store_id') != store_id:
        raise ValueError('本机广告报告结果无效')
    digest = hashlib.sha256(content).hexdigest()
    body = {'content': base64.b64encode(content).decode('ascii'),
            'sha256': digest, 'snapshot_sha256': snapshot_hash,
            'source_sha256': source_hash, 'summary': summary}
    expected = {'store_id': store_id, 'report_sha256': digest,
                'report_bytes': len(content), 'file_count': len(summary['files']),
                'download_path': f'/ad-optimization/reports/{job["id"]}/download'}
    headers = {'X-Worker-Attempt': str(job['attempts'])}
    for retry in range(3):
        if lost.is_set():
            raise ValueError('广告报告任务租约失效，已保留本机结果')
        try:
            received = client.request('POST', endpoint + '/ad-report-result',
                                      body, extra_headers=headers)[0]
            if received != expected:
                raise ValueError('云端广告报告回执与本机结果不一致')
            return received
        except TemporaryConnectionError:
            if retry == 2 or lost.wait(5):
                raise
    raise AssertionError('广告报告回执重试次数异常')
