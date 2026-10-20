from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select, update

from erp.core.config import get_settings
from erp.core.errors import NotFoundError
from erp.core.jsonutil import dumps
from erp.integrations.sourcing import SourcingAdapter, download_sourcing_image
from erp.models import ResearchProduct, SourcingCandidate
from erp.services.config_resolution import resolve_job_snapshot
from erp.services.jobs import JobContext, job_handler


def _number(value):
    try:
        return float(Decimal(str(value))) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


@job_handler("sourcing_search")
def sourcing_search_job(context: JobContext) -> None:
    product = context.session.get(ResearchProduct, str(context.payload.get("research_product_id") or ""))
    if not product:
        raise NotFoundError("选品商品不存在")
    config = resolve_job_snapshot(context.job.config_snapshot_json)
    count = max(1, min(50, int(context.payload.get("candidate_count") or 10)))
    context.start_step("prepare_image")
    image_path = download_sourcing_image(
        product.image_url,
        get_settings().data_dir / "sourcing" / product.id / "source",
    )
    context.complete_step("prepare_image", details={"path": str(image_path)})
    context.start_step("provider_search")
    rows = SourcingAdapter(config).search(image_path, count=count)
    context.complete_step("provider_search", details={"candidates": len(rows), "provider": config.get("provider")})
    context.start_step("save_candidates")
    for index, row in enumerate(rows, 1):
        item_id = str(row.get("itemId") or "")
        sku_id = str(row.get("skuId") or "")
        candidate = context.session.scalar(select(SourcingCandidate).where(
            SourcingCandidate.research_product_id == product.id,
            SourcingCandidate.provider == config.get("provider"),
            SourcingCandidate.external_product_id == item_id,
            SourcingCandidate.external_sku_id == sku_id,
        ))
        if not candidate:
            candidate = SourcingCandidate(
                research_product_id=product.id,
                provider=str(config.get("provider") or "unknown"),
                external_product_id=item_id,
                external_sku_id=sku_id,
            )
            context.session.add(candidate)
        unit = _number(row.get("realtimeSinglePrice"))
        delivery = _number(row.get("realtimeDeliveryPrice"))
        candidate.title = str(row.get("title") or "")
        candidate.sku_name = str(row.get("skuName") or "")
        candidate.image_url = str(row.get("imageUrl") or "")
        candidate.source_url = str(row.get("sourceUrl") or "")
        candidate.unit_price_cny = unit
        candidate.domestic_delivery_cny = delivery
        candidate.landed_cost_cny = (unit or 0) + (delivery or 0) if unit is not None else None
        candidate.sales = int(row.get("salesCountNum") or 0) if str(row.get("salesCountNum") or "").isdigit() else None
        candidate.minimum_order_quantity = _number((row.get("newtonData") or {}).get("min_order_qty"))
        candidate.rank = index
        candidate.raw_json = dumps(row)
    product.status = "pending_source_confirmation"
    context.session.commit()
    context.complete_step("save_candidates", details={"saved": len(rows)})

