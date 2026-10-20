"""Validate an uploaded self-listing workbook on its initiating computer."""
import hashlib
import base64
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import sys
import urllib.error
import urllib.request
import uuid
import zipfile

from erp_desktop import NoRedirect, TemporaryConnectionError


MAX_WORKBOOK = 20 * 1024 * 1024
MAX_BUNDLE = 2 * 1024**3


def _sha256_file(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _extract_bundle(source: Path, root: Path) -> Path:
    """Extract only ordinary members inside a disposable per-task directory."""
    target = root / 'bundle'
    if target.exists():
        workbooks = sorted(path for path in target.rglob('*') if path.suffix.casefold() in {'.xlsx', '.xlsm'} and not path.name.startswith('~$'))
        if len(workbooks) == 1:
            return workbooks[0]
        raise ValueError('资料包已有不完整解包结果，请保留任务目录核对')
    try:
        with zipfile.ZipFile(source) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) > 5000 or sum(item.file_size for item in members) > MAX_BUNDLE:
                raise ValueError('资料包过大（最多 5000 个文件、解压后 2GB）')
            paths: set[str] = set()
            base = target.resolve()
            for item in members:
                relative = item.filename.replace('\\', '/')
                destination = (target / relative).resolve()
                if (not relative or relative.startswith('/') or ':' in relative
                        or base not in destination.parents
                        or ((item.external_attr >> 16) & 0o170000) == 0o120000
                        or str(destination).casefold() in paths):
                    raise ValueError('资料包包含不安全或重复的文件路径')
                paths.add(str(destination).casefold())
            target.mkdir(parents=True)
            for item in members:
                destination = target / item.filename.replace('\\', '/')
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source_file, destination.open('xb') as output:
                    size = 0
                    while chunk := source_file.read(1024 * 1024):
                        size += len(chunk)
                        if size > item.file_size:
                            raise ValueError('资料包成员大小校验失败')
                        output.write(chunk)
                    if size != item.file_size:
                        raise ValueError('资料包成员大小校验失败')
    except zipfile.BadZipFile as error:
        raise ValueError('资料包不是有效的 ZIP 文件') from error
    workbooks = sorted(path for path in target.rglob('*') if path.suffix.casefold() in {'.xlsx', '.xlsm'} and not path.name.startswith('~$'))
    if len(workbooks) != 1:
        raise ValueError(f'资料包必须且只能包含一个 Excel，当前找到 {len(workbooks)} 个')
    return workbooks[0]


def run(client, endpoint: str, job: dict, root: Path, lost) -> dict:
    if (job.get('type') != 'catalog_self_selected_import'
            or not re.fullmatch(r'[a-f0-9]{32}', job.get('id', ''))
            or not re.fullmatch(r'/execution-devices/local/pc-[a-f0-9]{32}/jobs/[a-f0-9]{32}', endpoint)):
        raise ValueError('自主上架预演任务无效')
    work = root / 'catalog-import-tasks' / job['id']
    work.mkdir(parents=True, exist_ok=True)
    bundle = job.get('payload', {}).get('source_kind') == 'self_selected_bundle'
    workbook = work / ('source.zip' if bundle else 'source.xlsx')
    journal = work / 'validation.json'
    formal_journal = work / 'import-result.json.gz'
    dry_run = job.get('payload', {}).get('dry_run')
    if type(dry_run) is not bool:
        raise ValueError('自主上架任务缺少预演或正式导入标记')
    source_sha256 = job.get('payload', {}).get('source_sha256', '')
    if not re.fullmatch(r'[a-f0-9]{64}', source_sha256):
        raise ValueError('自主上架任务缺少源文件校验值')
    if dry_run and journal.exists():
        result = json.loads(journal.read_text(encoding='utf-8'))
        if not workbook.is_file() or _sha256_file(workbook) != source_sha256 or result['sha256'] != source_sha256:
            raise ValueError('本机预演结果与工作簿不一致，请保留任务目录核对')
        return result
    if not workbook.exists():
        request = urllib.request.Request(
            client.base + '/api/v1' + endpoint + '/catalog-import-source',
            headers={'Cookie': 'erp_session=' + client.profile['cookie'],
                     'X-Worker-Attempt': str(job['attempts']), 'Accept': 'application/octet-stream'},
            method='GET')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        temporary = work / (uuid.uuid4().hex + '.partial')
        try:
            with opener.open(request, timeout=60) as response, temporary.open('xb') as target:
                expected = response.headers.get('X-Content-SHA256', '')
                if expected != source_sha256:
                    raise ValueError('导入工作簿缺少校验值')
                size = 0
                digest = hashlib.sha256()
                while chunk := response.read(1024 * 1024):
                    if lost.is_set():
                        raise ValueError('任务租约已失效，停止下载工作簿')
                    size += len(chunk)
                    if size > (MAX_BUNDLE if bundle else MAX_WORKBOOK):
                        raise ValueError('导入源文件超过上限')
                    digest.update(chunk)
                    target.write(chunk)
                if not size or digest.hexdigest() != expected:
                    raise ValueError('导入工作簿校验失败')
            os.replace(temporary, workbook)
        except urllib.error.HTTPError as error:
            if error.code not in (502, 503, 504):
                raise ValueError(f'无法领取导入工作簿（HTTP {error.code}）') from None
            raise TemporaryConnectionError('服务器暂时无法下载导入工作簿') from error
        except urllib.error.URLError as error:
            raise TemporaryConnectionError('暂时无法下载导入工作簿') from error
        finally:
            temporary.unlink(missing_ok=True)
    if lost.is_set():
        raise ValueError('任务租约已失效，停止预演')
    if _sha256_file(workbook) != source_sha256:
        raise ValueError('本机工作簿与任务来源不一致')
    if not dry_run and formal_journal.exists():
        return _submit_formal(client, endpoint, job, formal_journal, lost)
    runtime = Path(__file__).parent / 'runtime'
    sys.path.insert(0, str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2] / 'backend'))
    from erp.services.catalog import parse_self_selected_workbook, parse_self_selected_images

    if bundle and shutil.disk_usage(work).free < min(MAX_BUNDLE, workbook.stat().st_size * 20 + 256 * 1024**2):
        raise ValueError('本机空间不足，停止解压资料包')
    source_workbook = _extract_bundle(workbook, work) if bundle else workbook
    records, errors = parse_self_selected_workbook(source_workbook)
    images, image_errors = parse_self_selected_images(source_workbook)
    errors.extend(image_errors)
    groups = {str(row.get('group_code')) for row in records}
    skus = {str(row.get('sku')) for row in records}
    for image in images:
        if image['group_code'] not in groups:
            errors.append({'row': image['row'], 'field': 'group_code', 'message': '图片对应的产品组不在产品导入表中'})
        if image['sku'] and image['sku'] not in skus:
            errors.append({'row': image['row'], 'field': 'sku', 'message': '图片对应的 SKU 不在产品导入表中'})
    result = {'sha256': source_sha256,
              'rows': len(records), 'product_groups': len(groups),
              'local_images': len(images), 'errors': errors}
    if len(errors) > 5000:
        raise ValueError('导入错误清单过大，请修正工作簿后重试')
    if not dry_run:
        cloud_images = []
        if bundle:
            invalid_rows = {int(error['row']) for error in errors if isinstance(error, dict) and error.get('row')}
            invalid_groups = {str(row['group_code']) for row in records if row['row'] in invalid_rows}
            invalid_groups.update(str(image['group_code']) for image in images if image['row'] in invalid_rows)
            selected = [image for image in images if image['group_code'] not in invalid_groups]
            if selected:
                from erp.core.security import vault
                from erp.domains.pod.integrations.oss import PodOssService
                from erp.models import IntegrationConnection
                header = {'X-Worker-Attempt': str(job['attempts'])}
                connection = client.request('GET', endpoint + '/catalog-import-oss',
                                            extra_headers=header)[0]
                secret = connection.pop('secrets')
                connection['secrets_encrypted'] = vault.encrypt(secret)
                service = PodOssService(IntegrationConnection(**connection))
                for image in selected:
                    if lost.is_set():
                        raise ValueError('任务租约已失效，停止上传图片')
                    client.request('POST', endpoint + '/heartbeat', {}, extra_headers=header)
                    path = image['path']
                    digest = _sha256_file(path)
                    uploaded = service.upload_self_selected_asset(path, digest)
                    cloud_images.append({key: image[key] for key in ('row', 'group_code', 'sku', 'relative_path', 'role', 'position')}
                                        | {key: uploaded[key] for key in ('oss_url', 'sha256', 'size_bytes')})
        value = {'schema': 2 if bundle else 1, 'source_sha256': source_sha256,
                 'records': records, 'errors': errors}
        if bundle:
            value['images'] = cloud_images
        packed = gzip.compress(json.dumps(value, ensure_ascii=False, default=str,
                                          separators=(',', ':')).encode('utf-8'), mtime=0)
        if not packed or len(packed) > 25 * 1024 * 1024:
            raise ValueError('自主上架结果超过安全传输上限')
        temporary = formal_journal.with_suffix('.tmp')
        try:
            temporary.write_bytes(packed)
            os.replace(temporary, formal_journal)
        finally:
            temporary.unlink(missing_ok=True)
        return _submit_formal(client, endpoint, job, formal_journal, lost)
    if len(json.dumps(result, ensure_ascii=False)) > 1_000_000:
        raise ValueError('导入错误清单过大，请修正工作簿后重试')
    temporary = journal.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, journal)
    finally:
        temporary.unlink(missing_ok=True)
    return result


def _submit_formal(client, endpoint: str, job: dict, journal: Path, lost) -> dict:
    if lost.is_set():
        raise ValueError('任务租约已失效，停止提交导入结果')
    packed = journal.read_bytes()
    if not packed or len(packed) > 25 * 1024 * 1024:
        raise ValueError('本机导入回执无效')
    digest = hashlib.sha256(packed).hexdigest()
    result = client.request('POST', endpoint + '/catalog-import-result', {
        'sha256': digest, 'content': base64.b64encode(packed).decode('ascii')},
        extra_headers={'X-Worker-Attempt': str(job['attempts'])})[0]
    if (not isinstance(result, dict)
            or set(result) != {'inserted', 'updated', 'rejected_groups', 'status'}):
        raise ValueError('云端导入回执无效，请保留本机任务文件')
    return result
