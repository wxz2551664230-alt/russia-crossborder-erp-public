"""Adapt the migrated order rules to ERP-owned POD products, never the old runtime."""
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from erp.core.jsonutil import loads
from erp.domains.pod.models import PodComposition, PodProductVersion
from erp.models import CatalogProduct, CatalogVariant


class ProductLibraryError(ValueError):
    pass


class ProductLibrary:
    def __init__(self, session: Session):
        self.session = session

    def variant_skus_contained_in(self, value: str) -> list[str]:
        rows = self.session.scalars(select(CatalogVariant.sku).join(CatalogProduct).where(
            CatalogProduct.source_type == "pod", CatalogVariant.active.is_(True))).all()
        return sorted({sku for sku in rows if sku and sku.casefold() in value.casefold()}, key=lambda sku: (-len(sku), sku))

    def get_product(self, sku: str) -> dict:
        products = self.session.scalars(select(CatalogProduct).where(
            CatalogProduct.source_type == "pod",
            or_(func.lower(CatalogProduct.sku) == sku.casefold(), CatalogProduct.id.in_(
                select(CatalogVariant.product_id).where(func.lower(CatalogVariant.sku) == sku.casefold(), CatalogVariant.active.is_(True)))))).all()
        if len(products) != 1:
            raise ProductLibraryError("SKU 不存在或对应多个产品，请先处理产品库重复项")
        product = products[0]
        version = self.session.scalar(select(PodProductVersion).where(
            PodProductVersion.catalog_product_id == product.id, PodProductVersion.is_current.is_(True)))
        variants = []
        for row in product.variants:
            if not row.active:
                continue
            properties = loads(row.properties_json, {})
            variants.append({**properties, "sku": row.sku, "color_name": row.color, "size_name": row.size,
                             "stock_item_id": int(row.supplier_sku_id) if row.supplier_sku_id.isdigit() else None})
        recipe = loads(version.recipe_json, {}) if version else {}
        # Older cloud compositions kept the S2B ID on the composition but
        # omitted it from the product version used by order matching.
        if version and version.supplier.lower() == "s2b" and not recipe.get("remote_product_id"):
            remote_ids = {
                int(value) for value in self.session.scalars(
                    select(PodComposition.remote_product_id).where(
                        PodComposition.catalog_product_id == product.id,
                        PodComposition.product_code == version.product_code,
                    )
                ).all() if str(value).isdigit() and int(value) > 0
            }
            if len(remote_ids) == 1:
                recipe["remote_product_id"] = remote_ids.pop()
        snapshot = loads(version.assets_json, {}) if version else {}
        supplier = str(recipe.get('supplier') or (version.supplier if version else '') or ('s2b' if product.sku.upper().startswith('S2B-') else 'hicustom')).lower()
        recipe = {
            **recipe, "supplier": supplier,
            "product_type_id": int(recipe.get("product_type_id") or (version.product_type_id if version else 0) or product.supplier_product_id or 0),
            "product_name": str(recipe.get("product_name") or product.name),
            "default_color_id": int(recipe.get("default_color_id") or (version.default_color_id if version else 0) or 0),
            "default_view_id": int(recipe.get("default_view_id") or (version.default_view_id if version else 0) or 0),
        }
        oss_assets = list(snapshot.get("oss_assets") or snapshot.get("library_assets") or [])
        known_source_views = {int(row.get("view_id") or 0) for row in oss_assets if isinstance(row, dict) and row.get("asset_type") == "source"}
        for link in product.asset_links:
            if not link.is_current or link.role != "artwork":
                continue
            metadata = loads(link.metadata_json, {})
            view_id = int(metadata.get("view_id") or 0)
            if view_id in known_source_views:
                continue
            oss_assets.append({"asset_type": "source", "view_id": view_id, "asset_id": link.asset_id,
                               "local_path": link.asset.local_path, "oss_url": link.asset.oss_url,
                               "oss_key": link.asset.oss_key, "sha256": link.asset.sha256,
                               "content_type": link.asset.mime_type, "source_filename": link.asset.name})
        return {"id": product.id, "sku": product.sku, "name": product.name, "product_name": product.name, "supplier": supplier,
                "product_code": version.product_code if version else "", "variants": variants,
                "recipe": recipe, "output_folder": version.legacy_output_folder if version else "",
                "oss_assets": oss_assets}

    def list_products(self, *, search="", supplier="", limit=30, offset=0) -> dict:
        query = select(CatalogProduct).where(CatalogProduct.source_type == "pod")
        if search:
            query = query.where(or_(CatalogProduct.name.contains(search), CatalogProduct.sku.contains(search),
                CatalogProduct.id.in_(select(CatalogVariant.product_id).where(CatalogVariant.sku.contains(search)))))
        if supplier:
            query = query.where(CatalogProduct.supplier == supplier)
        return {"products": [{"sku": row.sku} for row in self.session.scalars(query.order_by(CatalogProduct.sku).offset(offset).limit(limit))]}
