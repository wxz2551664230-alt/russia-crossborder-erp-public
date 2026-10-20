"""Task-scoped WB inputs for personal-computer execution.

The caller must verify the account, assigned device and live attempt before
using this private transport. It is deliberately not a public product API.
"""
import hashlib
import math
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from erp.core.errors import ConflictError
from erp.core.jsonutil import loads, dumps
from erp.core.security import vault
from erp.domains.pod.desktop_composition import columns
from erp.domains.pod import wb_export
from erp.models import (
    Asset, CatalogProduct, CatalogVariant, CatalogProductAsset, Store,
    WbPublicationItem, Job, JobStep,
)


def reconcile_snapshot(session, job):
    if job.type != 'pod_wb_excel_reconcile':
        raise ConflictError('不是 WB 商品卡核对任务')
    payload = loads(job.payload_json, {})
    rows = list(session.scalars(select(WbPublicationItem).where(
        WbPublicationItem.job_id == payload.get('export_job_id'),
        WbPublicationItem.offer_id.in_(payload.get('offer_ids') or []))))
    if not rows or {row.offer_id for row in rows} != set(payload.get('offer_ids') or []) or any(
        row.store_id != payload.get('store_id') or row.status not in {'submitted', 'pending_stock'} for row in rows
    ):
        raise ConflictError('WB 台账已变化，请刷新后重新核对')
    return {'offer_ids': payload['offer_ids'], 'config': vault.decrypt(job.config_snapshot_json)}


def accept_reconcile_result(session, job, result):
    if job.type != 'pod_wb_excel_reconcile' or not isinstance(result, dict):
        raise ConflictError('WB 核对结果无效')
    payload = loads(job.payload_json, {})
    expected = set(payload.get('offer_ids') or [])
    found = result.get('found')
    if not isinstance(found, dict) or not set(found).issubset(expected) or any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in found.values()
    ):
        raise ConflictError('WB 商品编号结果无效')
    rows = list(session.scalars(select(WbPublicationItem).where(
        WbPublicationItem.job_id == payload.get('export_job_id'),
        WbPublicationItem.offer_id.in_(expected)).with_for_update()))
    if not rows or {row.offer_id for row in rows} != expected or any(
        row.store_id != payload.get('store_id') or row.status not in {'submitted', 'pending_stock'} for row in rows
    ):
        raise ConflictError('WB 台账已变化，拒绝回写')
    from erp.models import utc_now
    for row in rows:
        nm_id = found.get(row.offer_id)
        if nm_id is None:
            continue
        if row.external_product_id and row.external_product_id != str(nm_id):
            raise ConflictError('WB 商品编号与已有记录冲突')
        row.external_product_id = str(nm_id)
        row.status = 'pending_stock'
        row.last_error = ''
        row.confirmed_at = utc_now()


def stock_snapshot(session, job):
    if job.type != 'pod_wb_stock':
        raise ConflictError('不是 WB 库存任务')
    payload = loads(job.payload_json, {})
    ids = payload.get('ledger_ids') or []
    rows = list(session.scalars(select(WbPublicationItem).where(WbPublicationItem.id.in_(ids))))
    if len(rows) != len(ids) or {row.id for row in rows} != set(ids) or any(
        row.store_id != payload.get('store_id') or row.status != 'stock_processing' or not row.external_product_id
        for row in rows
    ):
        raise ConflictError('WB 库存台账已变化，请刷新后核对')
    return {'rows': [{'id': row.id, 'offer_id': row.offer_id, 'nm_id': row.external_product_id} for row in rows],
            'warehouse_id': payload['warehouse_id'], 'amount': payload['amount'],
            'config': vault.decrypt(job.config_snapshot_json)}


def accept_stock_result(session, job, result):
    if job.type != 'pod_wb_stock' or not isinstance(result, dict) or result.get('verified') is not True:
        raise ConflictError('WB 库存回传无效')
    payload = loads(job.payload_json, {})
    items = result.get('items')
    if (not isinstance(items, list) or len(items) != len(payload.get('ledger_ids') or [])
        or {item.get('id') for item in items if isinstance(item, dict)} != set(payload['ledger_ids'])
        or result.get('warehouse_id') != payload.get('warehouse_id')
        or result.get('amount') != payload.get('amount')):
        raise ConflictError('WB 库存结果与任务不符')
    by_id = {item['id']: item for item in items}
    rows = list(session.scalars(select(WbPublicationItem).where(
        WbPublicationItem.id.in_(payload['ledger_ids'])).with_for_update()))
    if len(rows) != len(items) or any(
        row.status != 'stock_processing' or row.store_id != payload['store_id']
        or str(by_id[row.id].get('nm_id')) != row.external_product_id
        or not isinstance(by_id[row.id].get('chrt_id'), int)
        or by_id[row.id]['chrt_id'] <= 0 for row in rows
    ):
        raise ConflictError('WB 库存台账已变化，拒绝回写')
    for row in rows:
        row.status = 'pending_price'
        row.last_error = ''


def price_snapshot(session, job):
    if job.type != 'pod_wb_price':
        raise ConflictError('不是 WB 价格任务')
    payload = loads(job.payload_json, {})
    ids = payload.get('ledger_ids') or []
    rows = list(session.scalars(select(WbPublicationItem).where(WbPublicationItem.id.in_(ids))))
    saved_upload = loads(job.external_ref, {}).get('kind') == 'wb_price_upload'
    recovering = bool(payload.get('wb_price_recovery'))
    allowed_statuses = {'price_processing', 'pending_price', 'price_failed'} if recovering else ({'price_processing', 'pending_price'} if saved_upload else {'price_processing'})
    if len(rows) != len(ids) or {row.id for row in rows} != set(ids) or any(
        row.store_id != payload.get('store_id') or row.status not in allowed_statuses
        or not row.external_product_id for row in rows
    ):
        raise ConflictError('WB 价格台账已变化，请刷新后核对')
    from erp.domains.pod.wb_price_state import latest_operations
    latest = latest_operations(session, rows)
    if any(latest.get(row.id) is not job for row in rows):
        raise ConflictError('已有较新的价格任务，原任务只能保留历史记录')
    from erp.domains.pod.wb_price_recovery import freeze_plan
    priced = freeze_plan(session, job, rows)
    return {'progress_protocol': 1, 'upload_receipt': loads(job.external_ref, {}), 'rows': priced, 'config': vault.decrypt(job.config_snapshot_json),
            'verify_only': recovering or payload.get('verify_only') is True}


def accept_price_result(session, job, result):
    if job.type != 'pod_wb_price' or not isinstance(result, dict) or result.get('verified') is not True:
        raise ConflictError('WB 价格回传无效')
    expected = price_snapshot(session, job)['rows']
    got = result.get('rows')
    if (not isinstance(got, list) or len(got) != len(expected)
        or sorted(got, key=lambda row: row.get('id', '')) != sorted(expected, key=lambda row: row['id'])
        or not isinstance(result.get('upload_id'), int)
        or not (result['upload_id'] > 0 or (result['upload_id'] == 0 and result.get('verified_existing') is True))):
        raise ConflictError('WB 价格回传与任务不符')
    from erp.models import utc_now
    for row in session.scalars(select(WbPublicationItem).where(
        WbPublicationItem.id.in_([item['id'] for item in expected])).with_for_update()):
        row.status = 'completed'
        row.last_error = ''
        row.confirmed_at = utc_now()
    if loads(job.payload_json, {}).get('wb_price_recovery'):
        if result.get('verified_existing') is not True or result.get('upload_id') != 0:
            raise ConflictError('中断恢复只接受只读核验结果，不接受重新上传')
        from erp.domains.pod.wb_price_recovery import verified
        verified(session, job, via='original_device')


def snapshot(session, job):
    if job.type not in {'pod_wb_excel_export', 'pod_wb_api_publish'}:
        raise ConflictError('不是 WB 发布任务')
    payload = loads(job.payload_json, {})
    product_ids = payload['product_ids']
    products = list(session.scalars(select(CatalogProduct).where(
        CatalogProduct.id.in_(product_ids), CatalogProduct.source_type == 'pod').options(
            selectinload(CatalogProduct.variants),
            selectinload(CatalogProduct.asset_links).selectinload(CatalogProductAsset.asset),
        )))
    if len(products) != len(product_ids):
        raise ConflictError('WB 任务产品资料不完整')
    store = session.get(Store, payload['store_id'])
    if not store or not store.enabled or store.platform.casefold() not in {'wb', 'wildberries'}:
        raise ConflictError('WB 店铺不可用')
    template = wb_export._template(payload['template_id'])
    content = Path(template['path']).read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != payload['template_sha256'] or digest != template['sha256']:
        raise ConflictError('WB 模板已变化，请重新创建任务')
    variants = [v for p in products for v in p.variants]
    links = [link for p in products for link in p.asset_links if link.is_current]
    assets = {link.asset_id: link.asset for link in links}
    ledgers = list(session.scalars(select(WbPublicationItem).where(WbPublicationItem.job_id == job.id)))
    selected_ids = {v.id for p in products for v in wb_export._selected_variants(
        p, payload['variant_selection'], payload.get('variant_ids') or [])}
    if not ledgers or {r.variant_id for r in ledgers} != selected_ids or any(
        r.store_id != store.id or r.product_id not in product_ids for r in ledgers
    ):
        raise ConflictError('WB 发布任务与上架台账不一致')
    limits=wb_export._template_value_limits(Path(template['path']))
    template={**template,'schema':{**(template.get('schema') or {}),'characteristics':[{**x,'max_count':limits.get(int(x.get('id') or 0),int(x.get('max_count') or 0))} for x in (template.get('schema') or {}).get('characteristics',[])]}}
    public_job = columns(job)
    public_job['config_snapshot_json'] = ''
    public_store = columns(store)
    public_store.update(secrets_encrypted='', connection_id=None)
    return {
        'job': public_job,
        'steps': [columns(step) for step in job.steps],
        'store': public_store,
        'products': [columns(p) for p in products],
        'variants': [columns(v) for v in variants],
        'links': [columns(link) for link in links],
        'assets': [{**columns(a), 'local_path': ''} for a in assets.values()],
        'ledgers': [columns(r) for r in ledgers],
        'template': {k: v for k, v in template.items() if k != 'path'},
        'config': vault.decrypt(job.config_snapshot_json),
        # These are references only; never seed the current job's copy.json.
        'copy_references': wb_export._previous_copy_references_bulk(session, [p.id for p in products], job.id),
    }


def install_snapshot(session, value, template_content):
    """Install only the selected job in an empty private database on the PC.

    No shared database is accepted. Resuming a job uses its existing private
    database rather than overwriting its checkpoints with a fresh snapshot.
    """
    if session.scalar(select(Job.id).limit(1)) or session.scalar(select(CatalogProduct.id).limit(1)):
        raise ConflictError('WB 输入只能导入空的任务数据库')
    template = dict(value['template'])
    if hashlib.sha256(template_content).hexdigest() != template['sha256']:
        raise ConflictError('下载的 WB 模板校验失败')
    import re
    if not re.fullmatch('[a-f0-9]{32}', str(template['id'])):
        raise ConflictError('WB 模板编号无效')
    path = wb_export._roots()[0] / (template['id'] + '.xlsx')
    path.write_bytes(template_content)
    template['path'] = str(path)
    wb_export._save_manifest([template])
    session.add(Store(**value['store']))
    session.add_all(CatalogProduct(**row) for row in value['products'])
    session.flush()
    session.add_all(CatalogVariant(**row) for row in value['variants'])
    session.add_all(Asset(**row) for row in value['assets'])
    session.flush()
    session.add_all(CatalogProductAsset(**row) for row in value['links'])
    payload = loads(value['job']['payload_json'], {})
    payload['_copy_references_by_product'] = value['copy_references']
    job = Job(**{**value['job'], 'payload_json': dumps(payload), 'config_snapshot_json': vault.encrypt(value['config'])})
    session.add(job)
    session.flush()
    session.add_all(JobStep(**row) for row in value['steps'])
    session.add_all(WbPublicationItem(**row) for row in value['ledgers'])
    session.flush()
    return job


FILES = {'wb_excel': ('upload.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
         'wb_package': ('publication-package.json', 'application/json'),
         'wb_copy': ('copy.json', 'application/json'),
         'wb_copy_failures': ('copy-failures.json', 'application/json')}


def result_file(job, role):
    import re
    expected = {'wb_excel', 'wb_package', 'wb_copy', 'wb_copy_failures'} if job.type == 'pod_wb_excel_export' else {'wb_package', 'wb_copy', 'wb_copy_failures'} if job.type == 'pod_wb_api_publish' else set()
    if role not in expected or not re.fullmatch('[a-f0-9]{32}', job.id):
        raise ConflictError('WB 结果文件类型无效')
    return wb_export._roots()[1] / job.id / FILES[role][0]


def stage_file(job, role, content):
    path = result_file(job, role)
    if not content or len(content) > 24 * 1024**2:
        raise ConflictError('WB 文件为空或超过24MiB')
    if role == 'wb_excel':
        from io import BytesIO
        from zipfile import ZipFile, BadZipFile
        try:
            with ZipFile(BytesIO(content)) as archive:
                if '[Content_Types].xml' not in archive.namelist() or 'xl/workbook.xml' not in archive.namelist():
                    raise ValueError()
                if sum(i.file_size for i in archive.infolist()) > 128*1024**2:
                    raise ValueError()
        except (BadZipFile, ValueError):
            raise ConflictError('WB 工作簿格式无效')
    else:
        import json
        try:
            if not isinstance(json.loads(content), dict): raise ValueError()
        except (ValueError, UnicodeError):
            raise ConflictError('WB JSON 文件格式无效')
    digest = hashlib.sha256(content).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ConflictError('已有 WB 结果不同，拒绝覆盖')
    else:
        # The route holds the job row lock until this write is complete.
        temporary = path.with_suffix(path.suffix + '.partial')
        temporary.write_bytes(content); temporary.replace(path)
    return {'role': role, 'sha256': digest, 'size_bytes': len(content)}


def accept_result(session, job, result):
    from erp.models import JobArtifact
    if job.type != 'pod_wb_excel_export' or not isinstance(result, dict):
        raise ConflictError('WB 回传结果无效')
    rows = list(session.scalars(select(WbPublicationItem).where(WbPublicationItem.job_id == job.id)))
    returned = result.get('ledgers', [])
    if not isinstance(returned, list) or any(not isinstance(r, dict) or not isinstance(r.get('id'),str) for r in returned):
        raise ConflictError('WB 台账结果无效')
    if len(returned) != len(rows) or {r.get('id') for r in returned} != {r.id for r in rows}:
        raise ConflictError('WB 回传台账不完整')
    if any(r.get('status') not in {'failed', 'exported_pending_confirmation'} or r.get('external_product_id') for r in returned):
        raise ConflictError('Excel 任务不能确认平台已上架')
    receipts = result.get('files', [])
    if not isinstance(receipts, list) or any(not isinstance(r, dict) or not isinstance(r.get('role'),str) for r in receipts):
        raise ConflictError('WB 文件回执无效')
    roles = [r.get('role') for r in receipts]
    if len(roles) != len(set(roles)) or not {'wb_excel','wb_package','wb_copy'} <= set(roles):
        raise ConflictError('WB 发布文件不完整')
    for receipt in receipts:
        path = result_file(job, receipt['role'])
        if not path.is_file() or path.stat().st_size != receipt.get('size_bytes') or hashlib.sha256(path.read_bytes()).hexdigest() != receipt.get('sha256'):
            raise ConflictError('WB 文件哈希或大小不一致')
    import json
    copies = json.loads(result_file(job, 'wb_copy').read_text(encoding='utf-8'))
    result_by_id = {r['id']: r for r in returned}
    expected_ids = {r.variant_id for r in rows if result_by_id[r.id]['status'] == 'exported_pending_confirmation'}
    package = json.loads(result_file(job, 'wb_package').read_text(encoding='utf-8'))
    source_payload = loads(job.payload_json, {})
    if any(package.get('payload', {}).get(k) != source_payload.get(k) for k in ('product_ids','store_id','template_id','mode')):
        raise ConflictError('WB 发布包不属于当前任务')
    try:
        ids = [size['variant_id'] for group in package['groups'] for card in group['variants'] for size in card['sizes']]
    except (KeyError, TypeError): raise ConflictError('WB 发布包规格无效')
    if not expected_ids or len(ids) != len(set(ids)) or set(ids) != expected_ids:
        raise ConflictError('WB 发布包与成功台账不一致')
    for row in rows:
        entry = result_by_id[row.id]
        if entry['status'] == 'exported_pending_confirmation':
            copy = copies.get(row.product_id, {})
            if not copy.get('title') or not copy.get('description'):
                raise ConflictError('已导出商品缺少本次文案')
    # Commit together with fenced task completion in the caller.
    for row in rows:
        entry = result_by_id[row.id]
        row.status = entry['status']; row.last_error = str(entry.get('last_error', ''))[:1000]
    for receipt in receipts:
        role = receipt['role']; path = result_file(job, role)
        if session.scalar(select(JobArtifact.id).where(JobArtifact.job_id == job.id, JobArtifact.role == role)):
            raise ConflictError('WB 文件已登记，拒绝覆盖')
        asset = Asset(kind='file', name=path.name, source='pod_wb_export', local_path=str(path),
                      sha256=receipt['sha256'], size_bytes=receipt['size_bytes'], mime_type=FILES[role][1], read_only=True)
        session.add(asset); session.flush()
        session.add(JobArtifact(job_id=job.id, asset_id=asset.id, role=role))


def accept_api_result(session, job, result):
    """Commit a personal computer's WB card outcome with the fenced job result."""
    from erp.models import JobArtifact
    if job.type != 'pod_wb_api_publish' or not isinstance(result, dict):
        raise ConflictError('WB API 回传结果无效')
    rows = list(session.scalars(select(WbPublicationItem).where(WbPublicationItem.job_id == job.id).with_for_update()))
    returned = result.get('ledgers')
    if not isinstance(returned, list) or len(returned) != len(rows) or {r.get('id') for r in returned if isinstance(r, dict)} != {r.id for r in rows}:
        raise ConflictError('WB API 台账结果不完整')
    by_id = {r['id']: r for r in returned}
    if any(r.get('status') not in {'pending_stock', 'failed'} or
           (r.get('status') == 'pending_stock' and (not str(r.get('external_product_id') or '').isdigit() or int(r['external_product_id']) <= 0)) or
           (r.get('status') == 'failed' and r.get('external_product_id')) for r in returned):
        raise ConflictError('WB API 台账状态或商品编号无效')
    receipts = result.get('files')
    if not isinstance(receipts, list) or {r.get('role') for r in receipts if isinstance(r, dict)} not in ({'wb_package','wb_copy'}, {'wb_package','wb_copy','wb_copy_failures'}):
        raise ConflictError('WB API 发布文件不完整')
    for receipt in receipts:
        path = result_file(job, receipt['role'])
        if not path.is_file() or path.stat().st_size != receipt.get('size_bytes') or hashlib.sha256(path.read_bytes()).hexdigest() != receipt.get('sha256'):
            raise ConflictError('WB API 文件哈希或大小不一致')
    import json
    package = json.loads(result_file(job, 'wb_package').read_text(encoding='utf-8'))
    copies = json.loads(result_file(job, 'wb_copy').read_text(encoding='utf-8'))
    source_payload = loads(job.payload_json, {})
    if package.get('payload', {}).get('mode') != 'api' or any(package.get('payload', {}).get(k) != source_payload.get(k) for k in ('product_ids','store_id','template_id')):
        raise ConflictError('WB API 发布包不属于当前任务')
    try:
        ids = [size['variant_id'] for group in package['groups'] for card in group['variants'] for size in card['sizes']]
    except (KeyError, TypeError):
        raise ConflictError('WB API 发布包规格无效')
    successful = {r.variant_id for r in rows if by_id[r.id]['status'] == 'pending_stock'}
    if not successful or len(ids) != len(set(ids)) or set(ids) != successful:
        raise ConflictError('WB API 发布包与台账不一致')
    if any(not copies.get(row.product_id, {}).get('title') or not copies.get(row.product_id, {}).get('description') for row in rows if row.variant_id in successful):
        raise ConflictError('WB API 商品缺少本次文案')
    for row in rows:
        entry = by_id[row.id]
        if row.status not in {'queued', 'running', 'submitted', 'pending_stock', 'failed'} or row.store_id != source_payload['store_id']:
            raise ConflictError('WB API 台账已变化')
        if row.external_product_id and row.external_product_id != str(entry.get('external_product_id') or ''):
            raise ConflictError('WB API 商品编号与已有记录冲突')
        row.status = entry['status']
        row.external_product_id = str(entry.get('external_product_id') or '')
        row.last_error = str(entry.get('last_error') or '')[:1000]
    for receipt in receipts:
        role = receipt['role']; path = result_file(job, role)
        if session.scalar(select(JobArtifact.id).where(JobArtifact.job_id == job.id, JobArtifact.role == role)):
            raise ConflictError('WB API 文件已登记，拒绝覆盖')
        asset = Asset(kind='file', name=path.name, source='pod_wb_export', local_path=str(path),
                      sha256=receipt['sha256'], size_bytes=receipt['size_bytes'], mime_type=FILES[role][1], read_only=True)
        session.add(asset); session.flush()
        session.add(JobArtifact(job_id=job.id, asset_id=asset.id, role=role))


def price_progress(session, job, value):
    expected = price_snapshot(session, job)
    digest = hashlib.sha256(__import__('json').dumps({'rows': expected['rows']}, sort_keys=True).encode()).hexdigest()
    upload_id = value.get('upload_id')
    if value.get('digest') != digest or type(upload_id) is not int or upload_id <= 0:
        raise ConflictError('价格上传编号与原任务快照不符')
    prior = loads(job.external_ref, {})
    if prior and (prior.get('kind') != 'wb_price_upload' or prior.get('upload_id') != upload_id or prior.get('digest') != digest):
        raise ConflictError('已有其他价格上传回执，禁止覆盖')
    from erp.models import JobEvent, utc_now
    job.external_ref = dumps({'kind': 'wb_price_upload', 'upload_id': upload_id, 'digest': digest})
    # A confirmed upload may only be inspected, never uploaded again.
    for step in job.steps:
        if step.key == 'submit': step.status = 'completed'; step.progress = 100
        if step.key == 'verify':
            step.status = 'pending' if value.get('wait_seconds') else 'running'
            step.name = '等待人工核对原 WB 价格上传' if value.get('wait_seconds') else '核对原 WB 价格上传结果'
    wait = value.get('wait_seconds', 0)
    if type(wait) is not int or not 0 <= wait <= 86400: raise ConflictError('等待时长无效')
    message = f'WB 上传编号 {upload_id} 已保存；' + ('平台限流或仍在处理，已停止自动重试，需人工继续只读核对' if wait else '正在只读核对处理结果')
    job.progress = 50
    session.add(JobEvent(job_id=job.id, message=message))
    if wait:
        job.status = 'action_required'; job.heartbeat_at = utc_now()
        job.error_code = 'wb_price_manual_review'
        job.error_message = message
        job.finished_at = utc_now()
        job.lease_owner = ''; job.lease_expires_at = None
        from erp.models import WbPublicationItem
        for item in session.scalars(select(WbPublicationItem).where(WbPublicationItem.id.in_(loads(job.payload_json, {}).get('ledger_ids', [])))):
            if item.status == 'price_processing':
                item.status = 'pending_price'
                item.last_error = message[:1000]
    return {'wb_price_deferred': bool(wait), 'upload_id': upload_id}
