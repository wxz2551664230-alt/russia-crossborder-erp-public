"""Keep interrupted WB price operations from leaving a permanent processing flag."""
from datetime import timezone
from sqlalchemy import or_, select

from erp.core.jsonutil import dumps, loads
from erp.models import Job, JobEvent, WbPublicationItem, utc_now


def latest_operations(session, rows):
    wanted = {r.id for r in rows}
    stores = {r.store_id for r in rows}
    if not wanted:
        return {}
    jobs = session.scalars(select(Job).where(
        Job.type == 'pod_wb_price',
        or_(*(Job.payload_json.contains(store) for store in stores)),
    ).order_by(Job.created_at.desc(), Job.id.desc()))
    result = {}
    for job in jobs:
        for identity in loads(job.payload_json, {}).get('ledger_ids', []):
            if identity in wanted:
                result.setdefault(identity, job)
    return result


def release_interrupted(session, job):
    """Part of the caller's transaction; never retries or calls WB."""
    if job.type != 'pod_wb_price' or (job.status not in {'failed', 'cancelled'} and not (
            job.status == 'action_required' and loads(job.payload_json, {}).get('wb_price_recovery'))):
        return []
    ids = loads(job.payload_json, {}).get('ledger_ids') or []
    rows = list(session.scalars(select(WbPublicationItem).where(
        WbPublicationItem.id.in_(ids), WbPublicationItem.status == 'price_processing',
    ).with_for_update().execution_options(populate_existing=True)))
    latest = latest_operations(session, rows)
    changed = []
    for row in rows:
        if latest.get(row.id) is not job:
            continue
        # A lease expiry cannot prove a price upload was never sent. Preserve
        # the source/pricing job and require read-only verification before writes.
        row.status = 'pending_price' if job.error_code in {'wb_price_rate_limited', 'wb_price_manual_submit_required'} else 'price_failed'
        row.last_error = (job.error_message if row.status == 'pending_price' else
                          '原价格任务已中断，请先只读核对平台价格；本次未重新提交。' + (job.error_message or ''))[:1000]
        changed.append(row.id)
    return changed


def blocked_reason(row, operation):
    if row.status == 'price_processing':
        status = operation.status if operation else ''
        return {'queued': '价格任务等待原电脑执行，请查看任务',
                'running': '价格任务正在运行，完成后自动解除占用',
                'paused': '价格任务已暂停，请在原任务中继续',
                'retrying': '原价格任务正在恢复，请查看任务',
                'failed': '原价格任务已中断，请先只读核对价格',
                'cancelled': '原价格任务已停止，请先只读核对价格'}.get(status, '价格请求尚未核对，请查看原任务')
    if row.last_error.startswith('WB 上传编号 '):
        return '原 WB 价格上传已受理，请先只读核对，不能重复提交'
    return ''


def release_after_refresh(session, refresh, existing, matched, *, warnings):
    """A complete forced read supersedes older, inactive operations of this owner."""
    request = loads(refresh.payload_json, {})
    store = request['store_id']
    verified = {row.id for variant, row in existing.items() if variant in matched
                and row.external_product_id in {'', str(matched[variant]['nmID'])}
                and matched[variant]['stock']['state'] == 'verified'
                and matched[variant]['price']['state'] in {'verified', 'missing'}}
    jobs = list(session.scalars(select(Job).where(
        Job.type.in_({'pod_wb_stock', 'pod_wb_price'}), Job.status != 'completed',
        Job.payload_json.contains(store),
    ).with_for_update()))
    released, held = [], set()
    now = utc_now()
    for job in jobs:
        payload = loads(job.payload_json, {})
        ids = set(payload.get('ledger_ids') or [])
        if payload.get('store_id') != store or not ids or payload.get('wb_shop_sync_released_by'):
            continue
        created = job.created_at.replace(tzinfo=timezone.utc) if job.created_at.tzinfo is None else job.created_at
        cutoff = refresh.created_at.replace(tzinfo=timezone.utc) if refresh.created_at.tzinfo is None else refresh.created_at
        if (not request.get('refresh') or warnings or not ids <= verified
                or job.created_by != refresh.created_by or created > cutoff):
            if job.status not in {'failed', 'cancelled'} or warnings:
                held.update(ids)
            continue
        lease = job.lease_expires_at
        if lease and lease.tzinfo is None:
            lease = lease.replace(tzinfo=timezone.utc)
        if job.status == 'running' and lease and lease > now:
            held.update(ids)
            continue
        payload['wb_shop_sync_original_status'] = job.status
        payload['wb_shop_sync_original_cancel_requested'] = job.cancel_requested
        payload['wb_shop_sync_released_by'] = refresh.id
        job.payload_json = dumps(payload)
        if job.status not in {'failed', 'cancelled'}:
            job.status = 'cancelled'
            job.finished_at = now
        job.cancel_requested = True
        job.pause_requested = False
        job.lease_owner = ''
        job.lease_expires_at = None
        session.add(JobEvent(job_id=job.id, message='已由本店铺完整强制刷新释放；商品按WB回执重新分类，原任务不能重发',
                             data_json=dumps({'refresh_job_id': refresh.id, 'items': len(ids)})))
        released.append(job.id)
    return released, held
