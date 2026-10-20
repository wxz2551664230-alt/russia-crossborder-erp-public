from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp.core.database import get_db
from erp.core.errors import ConflictError, NotFoundError
from erp.core.jsonutil import dumps
from erp.models import ResearchProduct
from erp.modules.ozon_follow.legacy.services.ozon_gallery import save_extension_gallery_capture


router = APIRouter(prefix="/ozon-gallery", tags=["ozon-gallery"])


@router.post("/capture")
def capture_gallery(payload: dict, x_ozon_gallery_bridge: str = Header(default=""), session: Session = Depends(get_db)) -> dict:
    if x_ozon_gallery_bridge != "ozon-gallery-bridge-v1":
        raise ConflictError("图库桥接请求标识无效")
    sku = str(payload.get("sku") or "").strip()
    page_url = str(payload.get("page_url") or "").strip()
    if not re.fullmatch(r"\d{6,}", sku) or not page_url.startswith(("https://www.ozon.ru/product/", "https://ozon.ru/product/")):
        raise ConflictError("图库桥接只接受有效的 OZON 商品页面")
    product = session.scalar(select(ResearchProduct).where(ResearchProduct.source_sku == sku))
    if not product:
        raise NotFoundError("ERP 中找不到该 OZON SKU，请先导入 Seerfar 商品")
    try:
        images = save_extension_gallery_capture(product.source_sku, page_url=page_url, title=str(payload.get("title") or ""), images=list(payload.get("images") or []))
    except ValueError as error:
        raise ConflictError(str(error)) from error
    product.image_urls_json = dumps(images)
    if not product.image_url and images:
        product.image_url = images[0]
    session.commit()
    return {"ok":True,"accepted":True,"sku":product.source_sku,"image_count":len(images),"additional_image_count":max(0,len(images)-1),"next_stage":"ai_preparing" if product.status == "source_confirmed" else product.status}
