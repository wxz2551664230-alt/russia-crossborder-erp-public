from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from erp.core.errors import NotFoundError
from erp.core.jsonutil import dumps
from erp.models import Asset, Listing, ResearchProduct, Store, StoreListing
from erp.services.assets import register_local_asset
from erp.services.jobs import JobContext, job_handler
from erp.services.seerfar import import_seerfar_workbook

# Import modules for registration side effects.
from erp.services import legacy_migration as _legacy_migration  # noqa: F401,E402
from erp.services import listings as _listings  # noqa: F401,E402
from erp.services import sourcing as _sourcing  # noqa: F401,E402
from erp.services import ozon_follow as _ozon_follow  # noqa: F401,E402
from erp.services import catalog as _catalog  # noqa: F401,E402
from erp.services import publications as _publications  # noqa: F401,E402
from erp.services import self_listing_pipeline as _self_listing_pipeline  # noqa: F401,E402
from erp.domains.pod import runtime as _pod_studio  # noqa: F401,E402
from erp.domains.pod import migration as _pod_migration  # noqa: F401,E402
from erp.domains.pod import gallery as _pod_gallery  # noqa: F401,E402
from erp.domains.pod import supplier_catalog as _pod_supplier_catalog  # noqa: F401,E402
from erp.domains.pod import composition as _pod_composition  # noqa: F401,E402
from erp.domains.pod import artworks as _pod_artworks  # noqa: F401,E402
from erp.domains.pod import wb_export as _pod_wb_export  # noqa: F401,E402
from erp.domains.pod.ai_design import service as _pod_ai_design  # noqa: F401,E402
from erp.domains.orders import service as _pod_orders  # noqa: F401,E402
from erp.services import ad_jobs as _ad_jobs  # noqa: F401,E402


@job_handler("seerfar_import")
def seerfar_import_job(context: JobContext) -> None:
    context.start_step("read_workbook")
    asset = context.session.get(Asset, str(context.payload.get("asset_id") or ""))
    if not asset or not asset.local_path:
        raise NotFoundError("导入文件不存在")
    products = import_seerfar_workbook(asset.local_path)
    context.complete_step("read_workbook", details={"rows": len(products)})
    context.start_step("upsert_products")
    inserted = 0
    updated = 0
    stores = context.session.scalars(select(Store).where(Store.platform == "ozon", Store.enabled.is_(True))).all()
    date_key = datetime.now().strftime("%Y%m%d")
    used = [row for row in context.session.scalars(select(Listing.base_offer_id).where(Listing.base_offer_id.like(f"AUTO-{date_key}-%"))).all()]
    numbers = [int(match.group(1)) for value in used if (match := re.fullmatch(rf"AUTO-{date_key}-(\d+)", value))]
    sequence = max(numbers, default=0)
    for index, value in enumerate(products, 1):
        context.refresh()
        item = context.session.scalar(select(ResearchProduct).where(
            ResearchProduct.source == "seerfar", ResearchProduct.source_sku == value.source_sku,
        ))
        if item:
            updated += 1
        else:
            inserted += 1
            item = ResearchProduct(source="seerfar", source_sku=value.source_sku)
            context.session.add(item)
        item.title = value.title
        item.category = value.category
        item.ozon_url = value.ozon_url
        item.image_url = value.image_url
        item.image_urls_json = dumps(value.image_urls)
        item.brand = value.brand
        item.selling_price_rub = value.selling_price_rub
        item.sales = value.sales
        item.rating = value.rating
        item.weight_grams = value.weight_grams
        item.source_file = value.source_file
        item.source_row = value.source_row
        item.raw_json = dumps(value.raw)
        item.description_category_id = int(context.payload["description_category_id"])
        item.type_id = int(context.payload["type_id"])
        item.category_display = str(context.payload["category_display"])
        item.status = "category_confirmed"
        context.session.flush()
        listing = context.session.scalar(select(Listing).where(Listing.research_product_id == item.id))
        if not listing and stores:
            sequence += 1
            base_offer = f"AUTO-{date_key}-{sequence:03d}"
            listing = Listing(research_product_id=item.id, base_offer_id=base_offer, title=item.title, package_json=dumps({"depth_mm":None,"width_mm":None,"height_mm":None,"weight_g":item.weight_grams}), status="pending_sourcing")
            context.session.add(listing); context.session.flush()
            for store in stores:
                offer = f"{store.offer_prefix}{base_offer}" if store.offer_prefix else base_offer
                context.session.add(StoreListing(listing_id=listing.id, store_id=store.id, offer_id=offer, idempotency_key=f"listing:{listing.id}:{store.id}", pricing_json=dumps({"currency":store.currency})))
        if index % 100 == 0:
            context.session.commit()
            context.set_progress("upsert_products", index * 100 / max(1, len(products)), f"已保存 {index}/{len(products)} 个商品")
    context.session.commit()
    context.complete_step("upsert_products", details={"inserted": inserted, "updated": updated})


def _iter_files(root: Path, extensions: set[str] | None = None):
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            path = Path(entry.path)
                            if not extensions or path.suffix.casefold() in extensions:
                                yield path
                    except OSError:
                        continue
        except OSError:
            continue


@job_handler("asset_index")
def asset_index_job(context: JobContext) -> None:
    roots = [Path(value).expanduser().resolve() for value in context.payload.get("roots") or []]
    extensions = {str(value).casefold() for value in context.payload.get("extensions") or []} or None
    calculate_hash = bool(context.payload.get("calculate_hash", False))
    context.start_step("index")
    indexed = 0
    skipped = 0
    for root in roots:
        if not root.is_dir():
            context.event(f"目录不存在，已跳过：{root}", level="warning")
            continue
        for path in _iter_files(root, extensions):
            context.refresh()
            legacy_id = str(path).casefold()
            existing = context.session.scalar(select(Asset).where(
                Asset.legacy_source == "filesystem", Asset.legacy_id == legacy_id,
            ))
            if existing:
                skipped += 1
                continue
            register_local_asset(
                context.session, path, kind="historical_file", source="legacy",
                read_only=True, calculate_hash=calculate_hash,
                legacy_source="filesystem", legacy_id=legacy_id,
                metadata={"indexed_root": str(root)},
            )
            indexed += 1
            if indexed % 250 == 0:
                context.session.commit()
                context.event(f"已建立 {indexed} 个文件索引")
    context.session.commit()
    context.complete_step("index", details={"indexed": indexed, "skipped": skipped})
