"""Last ERP-confirmed Ozon stock writes, used as the promotion quantity."""
from sqlalchemy import select

from erp.core.jsonutil import loads
from erp.models import Job, JobStep, PublicationStore


def confirmed_stock_quantities(session, child_ids: list[str]) -> dict[str, int]:
    requested = set(child_ids)
    if not requested:
        return {}
    wanted: set[str] = set()
    result: dict[str, int] = {}
    for child in session.scalars(select(PublicationStore).where(PublicationStore.id.in_(requested))):
        if child.stock_status != 'completed':
            continue
        wanted.add(child.id)
        quantity = loads(child.assets_json, {}).get('stock_confirmation', {}).get('quantity')
        if type(quantity) is int and quantity > 0:
            result[child.id] = quantity
    steps = session.execute(
        select(Job, JobStep).join(JobStep, JobStep.job_id == Job.id)
        .where(Job.type == 'catalog_ozon_publication', JobStep.key.in_(wanted - set(result)),
               JobStep.status == 'completed')
        .order_by(JobStep.finished_at.desc(), Job.created_at.desc())
    ).all()
    for job, step in steps:
        if step.key in result:
            continue
        payload = loads(job.payload_json, {})
        if payload.get('batch_phase') != 'stock':
            continue
        for entry in payload.get('batch_entries', []):
            if entry.get('child_id') == step.key:
                quantity = (entry.get('params') or {}).get('stock')
                if type(quantity) is int and quantity > 0:
                    result[step.key] = quantity
                break
    missing = wanted - set(result)
    if missing:
        jobs = session.scalars(
            select(Job).where(Job.type == 'catalog_ozon_followup', Job.status == 'completed')
            .order_by(Job.finished_at.desc(), Job.created_at.desc())
        )
        for job in jobs:
            payload = loads(job.payload_json, {})
            identity = payload.get('child_id')
            if identity not in missing or payload.get('phase') != 'stock':
                continue
            quantity = (payload.get('params') or {}).get('stock')
            if type(quantity) is int and quantity > 0:
                result[identity] = quantity
                missing.remove(identity)
                if not missing:
                    break
    return result
