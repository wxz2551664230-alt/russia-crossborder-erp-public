from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp.core.config import get_settings
from erp.core.database import get_db
from erp.core.errors import AppError, ConflictError
from erp.core.jsonutil import dumps
from erp.serializers import job_dict
from erp.services.jobs import enqueue_job, get_job, request_cancel, request_resume, retry_job
from erp.domains.pod.runtime import LEGACY_URL, ensure_pod_runtime, legacy_request, pod_status
from erp.models import CatalogImportRun
from erp.domains.pod.artworks import (
    artwork_product_options as native_artwork_product_options,
    artwork_usages as native_artwork_usages,
    artwork_file as native_artwork_file,
    assign_category as native_assign_artwork_category,
    create_artwork as native_create_artwork,
    create_category as native_create_artwork_category,
    delete_category as native_delete_artwork_category,
    list_artworks as native_list_artworks,
    rename_category as native_rename_artwork_category,
    stage_artwork_upload,
    stage_artwork_upload_batch,
    used_artwork_ids,
)
from erp.domains.pod.gallery import (
    enqueue_bulk_gallery_cleanup,
    enqueue_gallery_cleanup,
    enqueue_gallery_sync,
    list_gallery,
    mark_gallery_deleted,
)
from erp.domains.pod.supplier_catalog import (
    catalog_view,
    current_snapshot as current_supplier_catalog_snapshot,
    product_detail as native_product_detail,
    product_previews as native_product_previews,
    sync_catalog as sync_supplier_catalog,
)
from erp.domains.pod.integrations.common import load_supplier_connection
from erp.domains.pod.composition import (
    bind_unknown_gallery, bind_unknown_result, clear_finished_compositions, composition_dict,
    create_native_composition, list_compositions,
)
from erp.domains.pod.recovery import failed_composition_retry_preview, retry_all_failed_compositions
from erp.domains.pod.models import PodComposition
from erp.domains.pod.library import (
    assign_category as assign_pod_product_category,
    create_category as create_pod_product_category,
    delete_category as delete_pod_product_category,
    enqueue_restore as enqueue_pod_product_restore,
    library_product_detail,
    library_summary,
    list_categories as list_pod_product_categories,
    list_library_products,
    open_product_folder,
    refresh_supplier_facts,
    rename_category as rename_pod_product_category,
)
from erp.domains.pod.compatibility import open_workspace_folder, update_workspace_settings, workspace_status
from erp.domains.pod.ui_archive import archived_ui_file, ensure_ui_archive
from erp.domains.pod.wb_export import (
    create_export as create_wb_export,
    export_file as wb_export_file,
    list_exports as list_wb_exports,
    list_templates as list_wb_templates,
    save_template as save_wb_template,
    preview_export as preview_wb_export,
    publication_availability as wb_publication_availability,
    confirm_export as confirm_wb_export,
    reconcile_excel_export as reconcile_wb_excel_export,
    enqueue_excel_reconcile as enqueue_wb_excel_reconcile,
    enqueue_history_reconcile as enqueue_wb_history_reconcile,
    list_publications as list_wb_publications,
    remove_publication_from_queue,
    wb_warehouses,
    enqueue_stock as enqueue_wb_stock,
    enqueue_price as enqueue_wb_price,
)


router=APIRouter(prefix="/pod",tags=["pod"])
IMAGE_LIMIT=30*1024*1024


class PodPlacement(BaseModel):
    view_id:int=Field(gt=0);view_name:str="";width:int=Field(gt=0);height:int=Field(gt=0);top_x:float;top_y:float
    artwork_id:str=Field(min_length=1);rotation:Literal[0,90,180,270]=0


class PodCompositionCreate(BaseModel):
    market:Literal["CN","US"]="CN"
    supplier:Literal["hicustom","s2b"]="hicustom";product_type_id:int=Field(gt=0);product_name:str=Field(min_length=1,max_length=200)
    default_color_id:int=Field(gt=0);default_view_id:int=Field(gt=0);selected_size_ids:list[int]=Field(default_factory=list);category_id:str="";placements:list[PodPlacement]=Field(min_length=1,max_length=12)


class PodCompositionBatchCreate(PodCompositionCreate):
    artwork_ids:list[str]=Field(min_length=1,max_length=500)
    prevent_duplicate:bool=False


class PodNativeMigrationCreate(BaseModel):
    dry_run: bool = True
    source_root: str = ""


class PodUnknownResultBind(BaseModel):
    product_code: str = Field(min_length=1, max_length=200)
    remote_product_id: str = Field(default="", max_length=200)


class PodUnknownGalleryBind(BaseModel):
    view_id: int = Field(gt=0)
    gallery_code: str = Field(min_length=1, max_length=300)


class PodProductCategoryRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class PodProductCategoryBatch(BaseModel):
    product_ids: list[str] = Field(min_length=1, max_length=500)
    category: str = Field(default="", max_length=300)


class PodWbExportCreate(BaseModel):
    product_ids: list[str] = Field(min_length=1, max_length=1000)
    store_id: str = Field(min_length=1, max_length=32)
    template_id: str = Field(min_length=1, max_length=32)
    brand: str = Field(min_length=1, max_length=100)
    requirement: str = Field(default="", max_length=4000)
    price_cny: float = Field(gt=0, description="WB 定价数值；按人民币口径计算后原样提交，不做汇率换算")
    pricing: dict[str, Any] = Field(default_factory=dict)
    image_rules: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    variant_selection: Literal["all", "selected", "largest_one", "largest_two", "largest_three"] = "all"
    variant_ids: list[str] = Field(default_factory=list, max_length=5000)
    mode: Literal["excel", "api"] = "excel"
    generation_provider: Literal["model", "codex"] = "model"
    codex_request_id: str = Field(default="",pattern="^([a-f0-9]{32})?$")


class PodWbExportPreview(BaseModel):
    product_ids: list[str] = Field(min_length=1, max_length=1000)
    subject_id: int = Field(gt=0)
    variant_selection: Literal["all", "selected", "largest_one", "largest_two", "largest_three"] = "selected"
    variant_ids: list[str] = Field(default_factory=list, max_length=5000)
    pricing: dict[str, Any] = Field(default_factory=dict)


class PodWbAvailability(BaseModel):
    store_id: str = Field(min_length=1, max_length=32)
    product_ids: list[str] = Field(default_factory=list, max_length=1000)


class PodWbHistoryReconcile(BaseModel):
    store_id: str = Field(min_length=1, max_length=32)


class PodWbStockBatch(BaseModel):
    ledger_ids:list[str]=Field(min_length=1,max_length=1000)
    warehouse_id:int=Field(gt=0)
    amount:int=Field(ge=0,le=100000)


class PodWbPriceBatch(BaseModel):
    ledger_ids:list[str]=Field(min_length=1,max_length=1000)
    price:float|None=Field(default=None,gt=0)
    discount_percent:int=Field(default=0,ge=0,le=99)


@router.get("/status")
def status()->dict[str,Any]: return pod_status()


@router.post("/migrations", status_code=202)
def create_native_migration(request: PodNativeMigrationCreate, session: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    source_root = Path(request.source_root).expanduser().resolve() if request.source_root else settings.legacy_composer_root.resolve()
    run = CatalogImportRun(kind="pod_native_history", source_path=str(source_root), dry_run=request.dry_run, status="queued")
    session.add(run)
    session.flush()
    job = enqueue_job(
        session,
        job_type="pod_native_migration",
        payload={
            "source_root": str(source_root),
            "asset_root": str((settings.data_dir / "pod" / "assets").resolve()),
            "import_run_id": run.id,
        },
        config_snapshot=dumps({"source_read_only": True, "asset_strategy": "hardlink_with_copy_fallback"}),
        idempotency_key=f"pod_native_migration:{run.id}",
        steps=[
            ("inspect", "检查旧 POD 数据完整性"),
            ("products", "迁移产品、SKU 和当前资产"),
            ("artwork_assets", "迁移并校验设计图库原图"),
            ("product_history", "迁移版本、编码和颜色图片历史"),
            ("compositions", "迁移合成任务和供应商图库台账"),
            ("catalog_snapshot", "保存供应商目录快照"),
            ("normalize_assets", "修复本地与 OSS 资产状态"),
        ],
        max_attempts=1,
    )
    run.job_id = job.id
    session.commit()
    return {"job": job_dict(job), "import_run_id": run.id, "source_read_only": True}


@router.get("/original-composer",response_class=HTMLResponse,include_in_schema=False)
def original_composer()->HTMLResponse:
    """Serve the ERP-owned snapshot of the original operation layout."""
    html=archived_ui_file("index.html").read_text(encoding="utf-8")
    html=html.replace('href="/styles.css?v=53"','href="/api/v1/pod/original-assets/styles.css?v=53"')
    html=html.replace('src="/app.js?v=78"','src="/api/v1/pod/original-assets/app.js?v=78"')
    embedded_style="""
    <style id="erp-pod-embed">
      .app-sidebar { display: none !important; }
      .application-shell { grid-template-columns: minmax(0, 1fr) !important; }
      .app-workspace { grid-column: 1 !important; }
    </style>
    """
    html=html.replace("</head>",f"{embedded_style}</head>")
    return HTMLResponse(html,headers={"Cache-Control":"no-store"})


@router.get("/original-assets/{asset_name}",include_in_schema=False)
def original_asset(asset_name:Literal["styles.css","app.js"])->Response:
    path=archived_ui_file(asset_name)
    content=path.read_text(encoding="utf-8")
    media_type="text/css" if asset_name.endswith(".css") else "text/javascript"
    if asset_name.endswith(".js"):
        content=content.replace("/api/","/api/v1/pod/legacy-api/")
        content=content.replace("/health","/api/v1/pod/legacy-health")
        content=content.replace("/api/v1/pod/legacy-api/catalog/sync","/api/v1/pod/catalog/sync-compat")
        content=content.replace("/api/v1/pod/legacy-api/catalog","/api/v1/pod/catalog")
        content=content.replace("/api/v1/pod/legacy-api/product-types/previews","/api/v1/pod/product-types/previews")
        content=content.replace("/api/v1/pod/legacy-api/product-types/","/api/v1/pod/product-types/")
        content=content.replace("/api/v1/pod/legacy-api/artworks","/api/v1/pod/artworks")
        content=content.replace("/api/v1/pod/legacy-api/library/categories","/api/v1/pod/library/categories/compat")
        bridge=(Path(__file__).with_name("composer_batch_bridge.js")).read_text(encoding="utf-8")
        # Initialize after adapters are installed; avoid the archived eager boot
        # racing with batch import and loading the same supplier catalog twice.
        content=content.replace("void initialize();", "")
        content += f"\n{bridge}\nvoid initializeErpComposer();\n"
    return Response(content,media_type=media_type,headers={"Cache-Control":"no-store"})


@router.get("/original-assets-status",include_in_schema=False)
def original_assets_status()->dict[str,Any]:
    return ensure_ui_archive()


def _forward_headers(response:httpx.Response)->dict[str,str]:
    allowed={"content-type","content-disposition","cache-control","etag","last-modified"}
    return {key:value for key,value in response.headers.items() if key.lower() in allowed}


def _rewrite_legacy_links(response:httpx.Response)->bytes:
    content=response.content
    if "application/json" in response.headers.get("content-type","").lower():
        content=content.replace(b'\"/api/',b'\"/api/v1/pod/legacy-api/')
    return content


def _track_legacy_job(session:Session,path:str,response:httpx.Response)->None:
    if response.status_code>=400: return
    normalized=path.strip("/")
    initial=normalized in {"jobs","jobs/multi"}
    retry=normalized.endswith("/retry") or normalized.endswith("/retry-oss")
    if not initial and not retry: return
    external_id=""
    try:
        body=response.json();candidate=body.get("job",body) if isinstance(body,dict) else {}
        external_id=str(candidate.get("id") or "")
    except Exception:
        pass
    if not external_id and retry:
        parts=normalized.split("/")
        if len(parts)>=3: external_id=parts[1]
    if not external_id: return
    suffix="" if initial else f":{uuid.uuid4().hex}"
    job=enqueue_job(session,job_type="pod_composition",payload={"external_ref":external_id},config_snapshot=dumps({"source":"original_pod_workspace"}),idempotency_key=f"pod_legacy:{external_id}{suffix}",steps=[("submit","接管原版合成任务"),("compose","生成效果图与规格"),("sync_catalog","更新 ERP POD 产品库")],max_attempts=1)
    job.external_ref=external_id
    session.commit()


@router.get("/legacy-health",include_in_schema=False)
def legacy_health()->Response:
    return Response(dumps({"status":"ok","application":"russia-crossborder-erp-pod"}),media_type="application/json")


@router.api_route("/legacy-api/{path:path}",methods=["GET","POST","PUT","PATCH","DELETE"],include_in_schema=False)
async def legacy_api_proxy(path:str,request:Request,session:Session=Depends(get_db))->Response:
    """Transparent same-origin bridge used by the unmodified legacy operation UI."""
    normalized=path.strip("/")
    if request.method=="GET" and normalized=="status":
        return Response(dumps(workspace_status(session)),media_type="application/json")
    if request.method=="PUT" and normalized=="settings":
        values=await request.json()
        return Response(dumps(update_workspace_settings(session,values if isinstance(values,dict) else {})),media_type="application/json")
    if request.method=="POST" and normalized=="library/open-folder":
        return Response(dumps(open_workspace_folder()),media_type="application/json")
    if request.method=="GET" and normalized=="jobs":
        return Response(dumps(list_compositions(session,limit=2000)),media_type="application/json")
    if request.method=="DELETE" and normalized=="jobs/finished":
        return Response(
            dumps({"removed":clear_finished_compositions(session),"records_preserved":True}),
            media_type="application/json",
        )
    if request.method=="POST" and normalized in {"jobs","jobs/multi"}:
        form=await request.form()
        try:
            placement_values=json.loads(str(form.get("placements") or "[]")) if normalized.endswith("multi") else [{
                "view_id":int(form.get("default_view_id") or 0),"view_name":"",
                "width":float(form.get("width") or 0),"height":float(form.get("height") or 0),
                "top_x":float(form.get("top_x") or 0),"top_y":float(form.get("top_y") or 0),"rotation":0,
            }]
            size_values=json.loads(str(form.get("selected_size_ids") or "[]"))
        except (ValueError,TypeError,json.JSONDecodeError) as error:
            raise AppError("多面印刷参数格式无效",code="pod_composition_form_invalid",status_code=422) from error
        payload=_prepare_composition_payload(session,{"market":str(form.get("market") or "CN"),"supplier":str(form.get("supplier") or "hicustom"),"product_type_id":int(form.get("product_type_id") or 0),"product_name":str(form.get("product_name") or ""),"default_color_id":int(form.get("default_color_id") or 0),"default_view_id":int(form.get("default_view_id") or 0),"selected_size_ids":size_values,"category_id":str(form.get("category_id") or ""),"placements":placement_values})
        upload_fields=list(form.getlist("images")) if normalized.endswith("multi") else [form.get("image")]
        uploads=[]
        for image in upload_fields:
            if image is not None and hasattr(image,"read"):
                uploads.append((getattr(image,"filename",None) or "design.png",await image.read(IMAGE_LIMIT+1),getattr(image,"content_type",None) or "application/octet-stream"))
        item=create_native_composition(session,request=payload,uploads=uploads)
        return Response(dumps({"job":composition_dict(item)}),media_type="application/json")
    action_match=re.fullmatch(r"jobs/([^/]+)/(retry|retry-oss|cancel)",normalized)
    if request.method=="POST" and action_match:
        identity,action=action_match.groups()
        item=session.get(PodComposition,identity) or session.scalar(select(PodComposition).where(PodComposition.legacy_id==identity))
        if not item or not item.job_id: raise AppError("队列任务不存在",code="not_found",status_code=404)
        job=get_job(session,item.job_id)
        if action=="cancel": request_cancel(session,job.id)
        elif job.status=="paused": request_resume(session,job.id)
        else: retry_job(session,job.id)
        return Response(dumps({"job":composition_dict(item)}),media_type="application/json")
    ensure_pod_runtime()
    headers={key:value for key,value in request.headers.items() if key.lower() in {"content-type","accept"}}
    try:
        response=httpx.request(request.method,f"{LEGACY_URL}/api/{path}",params=list(request.query_params.multi_items()),content=await request.body(),headers=headers,timeout=180)
    except httpx.HTTPError as error:
        raise AppError(f"POD 合成内核无法连接：{error}",code="pod_runtime_unavailable") from error
    if request.method=="POST": _track_legacy_job(session,path,response)
    return Response(_rewrite_legacy_links(response),status_code=response.status_code,headers=_forward_headers(response))


@router.get("/catalog")
def catalog(session:Session=Depends(get_db))->dict[str,Any]: return catalog_view(session)


@router.get("/us-catalog")
def us_catalog(search: str = "", category: str = "", page: int = Query(default=1, ge=1),
               page_size: int = Query(default=50, ge=1, le=100), session: Session = Depends(get_db)) -> dict:
    from erp.domains.pod.us_supplier_catalog import view
    return view(session, search=search, category=category, page=page, page_size=page_size)


class UsFreightRequest(BaseModel):
    variant_id: int = Field(gt=0)
    refresh: bool = False


class UsFreightPreview(BaseModel):
    variant_id: int = Field(gt=0)
    manual_freight_cny: float | None = Field(default=None, ge=0, le=1000000, allow_inf_nan=False)


@router.get('/us-catalog/freight-rule')
def us_freight_rule(session: Session = Depends(get_db)) -> dict:
    from erp.domains.pod.us_freight import rule
    return rule(session)


@router.post('/us-catalog/products/{product_key}/freight', status_code=202)
def us_freight_read(product_key: str, payload: UsFreightRequest, session: Session = Depends(get_db)) -> dict:
    from erp.domains.pod.us_freight import create_job
    return create_job(session, product_key, payload.variant_id, payload.refresh)


@router.post('/us-catalog/products/{product_key}/freight-preview')
def us_freight_preview(product_key: str, payload: UsFreightPreview, session: Session = Depends(get_db)) -> dict:
    from erp.domains.pod.us_freight import preview
    return preview(session, product_key, payload.variant_id, payload.manual_freight_cny)


@router.post("/us-catalog/sync", status_code=202)
def sync_us_catalog(session: Session = Depends(get_db)) -> dict:
    from erp.domains.pod.us_supplier_catalog import create_job
    return create_job(session)


@router.get("/us-catalog/products/{product_key}")
def us_catalog_product(product_key: str, session: Session = Depends(get_db)) -> dict:
    from erp.domains.pod.us_supplier_catalog import product
    return product(session, product_key)


@router.get("/us-composer/catalog")
def us_composer_catalog(session:Session=Depends(get_db))->dict:
    from erp.domains.pod.us_supplier_catalog import composer_catalog
    return composer_catalog(session)


@router.get("/us-composer/products/{product_key}")
def us_composer_product(product_key:str,session:Session=Depends(get_db))->dict:
    from erp.domains.pod.us_supplier_catalog import composition_detail
    return composition_detail(session,product_key)


@router.post("/us-catalog/products/{product_key}/refresh", status_code=202)
def refresh_us_catalog_product(product_key: str, session: Session = Depends(get_db)) -> dict:
    from erp.domains.pod.us_supplier_catalog import create_job
    return create_job(session, key=product_key)


@router.post("/catalog/sync",status_code=202)
def sync_catalog(session:Session=Depends(get_db))->dict[str,Any]:
    token=uuid.uuid4().hex
    connection_ids={}
    for provider in ("hicustom","s2b"):
        try: connection_ids[provider]=load_supplier_connection(session,provider).connection_id
        except AppError: pass
    current_catalog=current_supplier_catalog_snapshot(session)
    job=enqueue_job(
        session,job_type="pod_supplier_catalog_sync",payload={"connection_ids":connection_ids,
            "base_snapshot_id":current_catalog.id if current_catalog else None},
        config_snapshot=dumps({"providers":["hicustom","s2b"],"connection_ids":connection_ids,"read_only":True}),
        idempotency_key=f"pod_supplier_catalog_sync:{token}",
        steps=[("sync","同步 HICUSTOM 与 S2B 供应商货盘")],max_attempts=2,
    )
    return {"job":job_dict(job)}


@router.post("/catalog/sync-compat")
def sync_catalog_compat(session:Session=Depends(get_db))->dict[str,Any]:
    sync_supplier_catalog(session)
    return catalog_view(session)


@router.post("/catalog/previews")
def catalog_previews(product_type_ids:list[str],session:Session=Depends(get_db))->dict[str,str|None]: return native_product_previews(session,product_type_ids)


@router.post("/product-types/previews")
def product_type_previews(product_type_ids:list[str],session:Session=Depends(get_db))->dict[str,str|None]: return native_product_previews(session,product_type_ids)


@router.get("/product-types/{product_key}")
def product_type(product_key:str,session:Session=Depends(get_db))->dict[str,Any]: return native_product_detail(session,product_key)


@router.get("/artworks")
def artworks(search:str="",category_id:str="",uploaded_from:str="",uploaded_to:str="",product_key:str="",usage:str="all",market:Literal["CN","US"]="CN",page:int=Query(default=1,ge=1),page_size:int=Query(default=60,ge=1,le=200),limit:int|None=Query(default=None,ge=1,le=200),offset:int=Query(default=0,ge=0),session:Session=Depends(get_db))->dict[str,Any]:
    if limit is not None:
        page_size=limit;page=offset//limit+1
    return native_list_artworks(session,search=search,category_id=category_id,page=page,page_size=page_size,uploaded_from=uploaded_from,uploaded_to=uploaded_to,product_key=product_key,usage=usage,market=market)


@router.get("/artworks/product-options")
def artwork_product_options(search:str="",limit:int=Query(default=60,ge=1,le=100),market:Literal["CN","US"]="CN",session:Session=Depends(get_db))->dict[str,Any]:
    return native_artwork_product_options(session,search=search,limit=limit,market=market)


@router.get("/artworks/{artwork_id}/usages")
def artwork_usage_history(artwork_id:str,session:Session=Depends(get_db))->dict[str,Any]:
    return native_artwork_usages(session,artwork_id)


def _artwork_file(artwork_id:str,kind:str,session:Session)->FileResponse:
    if get_settings().web_ui_profile == 'shared-products-pilot':
        from erp.domains.pod.cloud_files import cloud_artwork_response
        return cloud_artwork_response(session,artwork_id,kind)
    path,mime_type=native_artwork_file(session,artwork_id,kind)
    return FileResponse(path,media_type=mime_type)


@router.get("/artworks/{artwork_id}/thumbnail")
def artwork_thumbnail(artwork_id:str,session:Session=Depends(get_db)): return _artwork_file(artwork_id,"thumbnail",session)


@router.get("/artworks/{artwork_id}/file")
def artwork_original(artwork_id:str,session:Session=Depends(get_db)): return _artwork_file(artwork_id,"file",session)


@router.get("/artworks/{artwork_id}/usable")
def artwork_usable(artwork_id:str,session:Session=Depends(get_db)): return _artwork_file(artwork_id,"usable",session)


@router.post("/artworks",status_code=201)
async def upload_artwork(image:UploadFile=File(...),category_id:str=Form(default="uncategorized"),session:Session=Depends(get_db))->dict[str,Any]:
    content=await image.read(IMAGE_LIMIT+1)
    if get_settings().multiuser_enabled:
        from erp.domains.pod.desktop_artwork import stage
        return stage(session,content,image.filename or 'artwork.png',image.content_type or '',category_id)
    return native_create_artwork(session,content=content,filename=image.filename or "artwork.png",content_type=image.content_type or "application/octet-stream",category_id=category_id)


@router.post("/artworks/batch",status_code=202)
async def upload_artworks_batch(images:list[UploadFile]=File(...),category_id:str=Form(default="uncategorized"),session:Session=Depends(get_db))->dict[str,Any]:
    if not images:
        raise AppError("请选择需要上传的图片",code="pod_artwork_empty")
    if len(images)>100:
        raise AppError("每批最多上传 100 张设计图",code="pod_artwork_batch_too_large",status_code=413)
    batch_id=uuid.uuid4().hex
    if get_settings().multiuser_enabled:
        from erp.domains.pod.desktop_artwork import stage
        results=[]
        for position,image in enumerate(images,1):
            try:
                content=await image.read(IMAGE_LIMIT+1)
                results.append(stage(session,content,image.filename or f'artwork-{position}.png',image.content_type or '',category_id))
            except AppError as error:
                session.rollback();results.append({'filename':image.filename or '', 'status':'failed','message':str(error)})
            finally:await image.close()
        failed=sum(r['status']=='failed' for r in results)
        return {'batch_id':batch_id,'total':len(images),'accepted':len(images)-failed,'failed':failed,'results':results}
    uploads=[]
    for position,image in enumerate(images,1):
        filename=image.filename or f"artwork-{position}.png"
        try:
            content=await image.read(IMAGE_LIMIT+1)
            uploads.append((content,filename,image.content_type or "application/octet-stream"))
        finally:
            await image.close()
    results=stage_artwork_upload_batch(session,uploads=uploads,category_id=category_id,batch_id=batch_id)
    return {"batch_id":batch_id,"total":len(images),"accepted":len(results),"failed":0,"results":results}


@router.get('/artworks/upload-result/{job_id}')
def artwork_upload_result(job_id:str,request:Request,session:Session=Depends(get_db)):
    from erp.models import Job
    from erp.domains.pod.models import PodArtwork
    from erp.domains.pod.artworks import artwork_dict
    from erp.core.jsonutil import loads
    from sqlalchemy import select
    job=session.get(Job,job_id)
    if not job or job.type!='pod_artwork_upload' or job.created_by!=request.state.user['id']:
        raise AppError('上传任务不属于当前账号',code='forbidden',status_code=403)
    if job.status!='completed':return {'status':job.status,'message':job.error_message}
    item=session.scalar(select(PodArtwork).where(PodArtwork.sha256==loads(job.payload_json,{})['sha256']).order_by(PodArtwork.created_at))
    if not item:raise AppError('图库记录不存在',code='artwork_missing',status_code=409)
    return {'status':'completed','artwork':artwork_dict(item)}


class CategoryCreate(BaseModel): name:str=Field(min_length=1,max_length=100)
class CategoryAssign(BaseModel): category_id:str=Field(min_length=1,max_length=100)
class BatchCategoryAssign(CategoryAssign): item_ids:list[str]=Field(min_length=1,max_length=500)


@router.post("/artworks/categories",status_code=201)
def create_artwork_category(request:CategoryCreate,session:Session=Depends(get_db))->dict[str,Any]:
    item=native_create_artwork_category(session,request.name)
    return {"category":{"id":item.id,"name":item.name}}


@router.delete("/artworks/categories/{category_id}")
def delete_artwork_category(category_id:str,session:Session=Depends(get_db))->dict[str,Any]:
    native_delete_artwork_category(session,category_id);return {"deleted":True}


@router.patch("/artworks/categories/{category_id}")
def rename_artwork_category(category_id:str,request:CategoryCreate,session:Session=Depends(get_db))->dict[str,Any]:
    item=native_rename_artwork_category(session,category_id,request.name)
    return {"category":{"id":item.id,"name":item.name}}


@router.patch("/artworks/{artwork_id}/category")
def assign_artwork_category(artwork_id:str,request:CategoryAssign,session:Session=Depends(get_db))->dict[str,Any]:
    return {"moved":native_assign_artwork_category(session,[artwork_id],request.category_id)}


@router.patch("/artworks/batch-category")
def assign_artwork_batch_category(request:BatchCategoryAssign,session:Session=Depends(get_db))->dict[str,Any]:
    return {"moved":native_assign_artwork_category(session,request.item_ids,request.category_id)}


@router.get("/supplier-gallery")
def supplier_gallery(usage:str="pending",supplier:str="",search:str="",include_deleted:bool=False,page:int=Query(default=1,ge=1),page_size:int=Query(default=50,ge=1,le=200),session:Session=Depends(get_db))->dict[str,Any]:
    return list_gallery(session,usage=usage,supplier=supplier,search=search,include_deleted=include_deleted,page=page,page_size=page_size)


@router.post("/supplier-gallery/sync",status_code=202)
def sync_supplier_gallery(supplier:str="",session:Session=Depends(get_db))->dict[str,Any]:
    return enqueue_gallery_sync(session,supplier.casefold())


@router.post("/supplier-gallery/cleanup-pending",status_code=202)
def cleanup_pending_supplier_gallery(supplier:str="",session:Session=Depends(get_db))->dict[str,Any]:
    return enqueue_bulk_gallery_cleanup(session,supplier.casefold())


@router.delete("/supplier-gallery/{supplier}/{gallery_code}",status_code=202)
def delete_supplier_gallery(supplier:str,gallery_code:str,session:Session=Depends(get_db))->dict[str,Any]: return enqueue_gallery_cleanup(session,supplier.casefold(),gallery_code)


@router.post("/supplier-gallery/{supplier}/{gallery_code}/mark-deleted")
def mark_supplier_gallery_deleted(supplier:str,gallery_code:str,session:Session=Depends(get_db))->dict[str,Any]: return mark_gallery_deleted(session,supplier.casefold(),gallery_code)


@router.get("/legacy-jobs")
def legacy_jobs()->dict[str,Any]: return legacy_request("GET","/api/jobs")


@router.get("/library-categories")
def library_categories()->dict[str,Any]: return legacy_request("GET","/api/library/categories")


@router.post("/legacy-jobs/{job_id}/{action}")
def legacy_job_action(job_id:str,action:Literal["retry","retry-oss","cancel"])->dict[str,Any]: return legacy_request("POST",f"/api/jobs/{job_id}/{action}",timeout=90)


@router.delete("/legacy-jobs/finished")
def clear_legacy_jobs()->dict[str,Any]: return legacy_request("DELETE","/api/jobs/finished")


def _prepare_composition_payload(session:Session,payload:dict[str,Any])->dict[str,Any]:
    supplier=str(payload.get("supplier") or "hicustom").casefold();product_type_id=int(payload.get("product_type_id") or 0)
    default_color_id=int(payload.get("default_color_id") or 0);default_view_id=int(payload.get("default_view_id") or 0)
    if default_view_id not in {int(item.get("view_id") or 0) for item in payload.get("placements") or []}:
        raise ConflictError("默认印刷面必须已配置图案")
    connection=load_supplier_connection(session,supplier)
    market=str(payload.get("market") or "CN").upper()
    if market not in {"CN","US"}: raise ConflictError("合成货盘国家无效")
    if market=="US":
        from erp.domains.pod.us_supplier_catalog import composition_detail
        detail=composition_detail(session,f"{supplier}:{product_type_id}")
        colors={int(c.get("id") or 0) for c in detail.get("colors") or []}
        views={int(v.get("id") or v.get("view_id") or 0) for v in (detail.get("product_description") or {}).get("print_areas") or []}
        if default_color_id not in colors or any(int(p.get("view_id") or 0) not in views for p in payload.get("placements") or []):
            raise ConflictError("所选颜色或印刷面不属于当前美国商品")
    else:
        detail=native_product_detail(session,f"{supplier}:{product_type_id}")
        if str(detail.get("produce_country") or "").upper()=="US": raise ConflictError("美国货盘不能作为国内产品合成")
    payload["market"]=market
    sizes={int(item.get("id") or item.get("size_id") or 0) for item in detail.get("sizes") or [] if isinstance(item,dict)}
    stocks={int(item.get("size_id") or 0) for item in detail.get("stock_info") or [] if isinstance(item,dict) and int(item.get("color_id") or 0)==default_color_id}
    available=stocks or sizes
    selected=list(dict.fromkeys(payload.get("selected_size_ids") or sorted(value for value in available if value>0)))
    if available and (not selected or any(value not in available for value in selected)):
        raise AppError("所选尺寸不属于当前颜色的可售规格",code="pod_variant_selection_invalid",status_code=422)
    # Preserve the complete supplier fact sheet for later AI copy and attribute
    # generation.  Exact numeric fields are also normalized into ERP columns.
    snapshot=dict(detail)
    snapshot.update({"supplier":supplier,"supplier_name":"S2B 货盘" if supplier=="s2b" else "指纹科技"})
    payload["selected_size_ids"]=selected;payload["variant_snapshot"]=snapshot
    payload["config_snapshot"]={"connection_ids":{supplier:connection.connection_id},"external_customer_id":str(connection.config.get("external_customer_id") or connection.config.get("hicustom_external_customer_id") or "")}
    return payload


@router.post("/compositions",status_code=202)
def create_composition(request:PodCompositionCreate,session:Session=Depends(get_db))->dict[str,Any]:
    payload=_prepare_composition_payload(session,request.model_dump())
    item=create_native_composition(session,request=payload)
    return {"job":job_dict(get_job(session,item.job_id)),"composition":composition_dict(item)}


@router.post("/compositions/batch", status_code=202)
def create_composition_batch(request:PodCompositionBatchCreate,session:Session=Depends(get_db))->dict[str,Any]:
    """Persist the complete selection first; expensive image preparation belongs to workers."""
    artwork_ids=list(dict.fromkeys(value.strip() for value in request.artwork_ids if value.strip()))
    if not artwork_ids:
        raise AppError("批量合成至少需要一张设计图",code="pod_composition_batch_empty",status_code=422)
    if len(request.placements)!=1:
        raise AppError("批量合成当前只支持复用一个印刷面的排版",code="pod_composition_batch_sides",status_code=422)
    if request.prevent_duplicate and used_artwork_ids(session,artwork_ids=artwork_ids,supplier=request.supplier,product_type_id=request.product_type_id,market=request.market):
        raise ConflictError("所选设计图已有用于当前产品的记录，请返回图库刷新后重新选择")
    base=request.model_dump(exclude={"artwork_ids","prevent_duplicate"})
    payload=_prepare_composition_payload(session,base)
    template=dict(payload["placements"][0])
    created=[]
    for artwork_id in artwork_ids:
        item=create_native_composition(
            session,
            request={**payload,"placements":[{**template,"artwork_id":artwork_id}]},
            defer_source_preparation=True,
        )
        created.append(item)
    return {
        "accepted":len(created),
        "composition_ids":[item.id for item in created],
        "job_ids":[item.job_id for item in created],
        "compositions":[composition_dict(item) for item in created],
    }


@router.post("/compositions/multi",status_code=202)
async def create_composition_multi(
    placements:str=Form(...),product_type_id:int=Form(...),product_name:str=Form(...),
    default_color_id:int=Form(...),default_view_id:int=Form(...),supplier:str=Form(default="hicustom"),
    images:list[UploadFile]|None=File(default=None),category_id:str=Form(default=""),selected_size_ids:str=Form(default="[]"),market:Literal["CN","US"]=Form(default="CN"),
    session:Session=Depends(get_db),
)->dict[str,Any]:
    try:
        placement_values=json.loads(placements);size_values=json.loads(selected_size_ids or "[]")
        if not isinstance(placement_values,list) or not isinstance(size_values,list): raise TypeError
    except (json.JSONDecodeError,TypeError) as error:
        raise AppError("多面印刷或销售尺寸参数格式无效",code="pod_composition_form_invalid",status_code=422) from error
    payload=_prepare_composition_payload(session,{"market":market,"supplier":supplier,"product_type_id":product_type_id,"product_name":product_name,"default_color_id":default_color_id,"default_view_id":default_view_id,"selected_size_ids":size_values,"category_id":category_id,"placements":placement_values})
    uploads=[(image.filename or "design.png",await image.read(IMAGE_LIMIT+1),image.content_type or "application/octet-stream") for image in images or []]
    item=create_native_composition(session,request=payload,uploads=uploads)
    return {"job":job_dict(get_job(session,item.job_id)),"composition":composition_dict(item)}


@router.get("/compositions")
def compositions(limit:int=Query(default=500,ge=1,le=2000),session:Session=Depends(get_db))->dict[str,Any]:
    return list_compositions(session,limit=limit)


@router.get("/compositions/retry-failed/preview")
def preview_failed_composition_retry(session:Session=Depends(get_db))->dict[str,Any]:
    if get_settings().multiuser_enabled:
        from erp.domains.pod.composition_retry import preview
        return preview(session)
    return failed_composition_retry_preview(session)


@router.post("/compositions/retry-failed",status_code=202)
def retry_failed_composition_batch(session:Session=Depends(get_db))->dict[str,Any]:
    if get_settings().multiuser_enabled:
        from erp.domains.pod.composition_retry import retry_failed
        return retry_failed(session)
    return retry_all_failed_compositions(session)


@router.post("/compositions/{composition_id}/bind-result")
def bind_composition_result(composition_id:str,request:PodUnknownResultBind,session:Session=Depends(get_db))->dict[str,Any]:
    item=bind_unknown_result(session,composition_id,product_code=request.product_code,remote_product_id=request.remote_product_id)
    if item.job_id:
        job=request_resume(session,item.job_id)
    else:
        job=None
    return {"composition":composition_dict(item),"job":job_dict(job) if job else None}


@router.post("/compositions/{composition_id}/bind-gallery")
def bind_composition_gallery(composition_id:str,request:PodUnknownGalleryBind,session:Session=Depends(get_db))->dict[str,Any]:
    item=bind_unknown_gallery(session,composition_id,view_id=request.view_id,gallery_code=request.gallery_code)
    job=request_resume(session,item.job_id) if item.job_id else None
    return {"composition":composition_dict(item),"job":job_dict(job) if job else None}


@router.get("/library/summary")
def pod_library_summary(session:Session=Depends(get_db))->dict[str,Any]: return library_summary(session)


@router.get("/wb/templates")
def pod_wb_templates()->dict[str,Any]: return {"items":list_wb_templates()}


@router.post("/wb/templates",status_code=201)
async def upload_pod_wb_template(
    file:UploadFile=File(...),subject_name:str=Form(...),subject_id:int=Form(...),
)->dict[str,Any]:
    if not str(file.filename or "").casefold().endswith(".xlsx"): raise AppError("请上传 .xlsx 格式的 WB 官方模板",code="wb_template_type")
    return {"template":save_wb_template(await file.read(),file.filename or "WB模板.xlsx",subject_name,subject_id)}


@router.get("/wb/exports")
def pod_wb_exports(include_codex_siblings:bool=False,session:Session=Depends(get_db))->dict[str,Any]:
    return {"items":list_wb_exports(session,include_codex_siblings=include_codex_siblings)}


@router.post("/wb/availability")
def pod_wb_availability(request:PodWbAvailability,session:Session=Depends(get_db))->dict[str,Any]:
    return wb_publication_availability(session,**request.model_dump())


@router.get("/wb/publications")
def pod_wb_publications(
    stage:str=Query(pattern="^(processing|pending_price|price_processing|pending_stock|stock_processing|completed|exceptions)$"),
    page:int=Query(1,ge=1),
    page_size:int=Query(30,ge=1,le=200),
    search:str=Query("",max_length=200),
    store_id:str=Query("",max_length=32),
    session:Session=Depends(get_db),
)->dict[str,Any]:
    return list_wb_publications(session,stage,page=page,page_size=page_size,search=search,store_id=store_id)


@router.get("/wb/warehouses")
def pod_wb_warehouses(store_id:str=Query(min_length=1,max_length=32),session:Session=Depends(get_db))->dict[str,Any]:
    return wb_warehouses(session,store_id)


@router.post("/wb/publications/{ledger_id}/remove")
def remove_pod_wb_publication(ledger_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
    return remove_publication_from_queue(session, ledger_id)


@router.post("/wb/publications/stock",status_code=202)
def pod_wb_stock(request:PodWbStockBatch,session:Session=Depends(get_db))->dict[str,Any]:
    return {"job":job_dict(enqueue_wb_stock(session,request.ledger_ids,request.warehouse_id,request.amount))}


@router.post("/wb/publications/price",status_code=202)
def pod_wb_price(request:PodWbPriceBatch,session:Session=Depends(get_db))->dict[str,Any]:
    return {"job":job_dict(enqueue_wb_price(session,request.ledger_ids,request.price,request.discount_percent))}


@router.post("/wb/publications/price/reconcile",status_code=202)
def reconcile_pod_wb_price(request:PodWbPriceBatch,session:Session=Depends(get_db))->dict[str,Any]:
    return {"job":job_dict(enqueue_wb_price(session,request.ledger_ids,request.price,request.discount_percent,verify_only=True))}


@router.post("/wb/history/reconcile",status_code=202)
def reconcile_pod_wb_history(request:PodWbHistoryReconcile,session:Session=Depends(get_db))->dict[str,Any]:
    return {"job":job_dict(enqueue_wb_history_reconcile(session,request.store_id))}


@router.post("/wb/history/sync-uploaded",status_code=202)
def sync_uploaded_pod_wb_history(request:PodWbHistoryReconcile,session:Session=Depends(get_db))->dict[str,Any]:
    return {"job":job_dict(enqueue_wb_history_reconcile(session,request.store_id,stock_workflow=True))}


@router.post('/wb/stores/{store_id}/sync',status_code=202)
def sync_wb_shop(store_id:str,refresh:bool=False,session:Session=Depends(get_db))->dict:
    from erp.domains.pod.wb_shop_sync import enqueue
    return enqueue(session,store_id,refresh)


@router.get('/wb/stores/{store_id}/sync-status')
def wb_shop_sync_status(store_id:str,session:Session=Depends(get_db))->dict:
    from erp.domains.pod.wb_shop_sync import status
    return status(session,store_id)


@router.get("/wb/history/{job_id}/report")
def download_pod_wb_history_report(job_id:str,session:Session=Depends(get_db))->FileResponse:
    import hashlib
    from erp.core.config import get_settings
    from erp.core.errors import NotFoundError
    from erp.core.task_identity import task_identity
    from erp.domains.pod.wb_export import _roots
    from erp.models import Asset
    from erp.services.jobs import get_job
    job=get_job(session,job_id)
    if job.type!='pod_wb_history_reconcile':raise ConflictError('不是 WB 历史扫描任务')
    if get_settings().multiuser_enabled and job.created_by!=(task_identity.get() or {}).get('id'):
        raise ConflictError('只能下载本人任务报告')
    assets=[session.get(Asset,item.asset_id) for item in job.artifacts if item.role=='wb_reconcile_report']
    asset=max((a for a in assets if a),key=lambda a:a.created_at,default=None)
    folder=(_roots()[1]/job.id).resolve()
    path=Path(asset.local_path).resolve() if asset else folder/'missing'
    valid_name=path.name=='WB历史POD匹配报告.json' or path.name=='WB店铺完整同步报告.json' or re.fullmatch(r'WB店铺完整同步报告-[a-f0-9]{8}\.json',path.name)
    if not asset or not path.is_file() or path.parent!=folder or not valid_name:
        raise NotFoundError('WB 历史匹配报告不存在')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=asset.sha256:
        raise ConflictError('WB 历史匹配报告校验不一致')
    return FileResponse(path,media_type='application/json',filename=path.name)


@router.post("/wb/exports/preview")
def preview_pod_wb_export(request:PodWbExportPreview,session:Session=Depends(get_db))->dict[str,Any]:
    return preview_wb_export(session,**request.model_dump())


@router.post("/wb/exports",status_code=202)
def start_pod_wb_export(request:PodWbExportCreate,session:Session=Depends(get_db))->dict[str,Any]:
    job=create_wb_export(session,**request.model_dump());session.commit();session.refresh(job)
    return {"job":job_dict(job)}


@router.get("/wb/exports/{job_id}/download")
def download_pod_wb_export(job_id:str,session:Session=Depends(get_db))->FileResponse:
    path=wb_export_file(session,job_id)
    return FileResponse(path,filename=path.name,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@router.post("/wb/exports/{job_id}/confirm-upload")
def confirm_pod_wb_upload(job_id:str,session:Session=Depends(get_db))->dict[str,Any]: return confirm_wb_export(session,job_id,uploaded=True)


@router.post("/wb/exports/{job_id}/reconcile-upload")
def reconcile_pod_wb_upload(job_id:str,session:Session=Depends(get_db))->dict[str,Any]:
    if not get_settings().multiuser_enabled:return reconcile_wb_excel_export(session,job_id)
    return {"job":job_dict(enqueue_wb_excel_reconcile(session,job_id))}


@router.post("/wb/exports/{job_id}/cancel-upload")
def cancel_pod_wb_upload(job_id:str,session:Session=Depends(get_db))->dict[str,Any]: return confirm_wb_export(session,job_id,uploaded=False)


@router.get("/library/products")
def pod_library_products(
    page:int=Query(default=1,ge=1),page_size:int=Query(default=50,ge=1,le=200),
    search:str=Query(default="",max_length=200),supplier:str=Query(default="",max_length=120),
    category:str=Query(default="",max_length=800),status:str=Query(default="",max_length=40),market:Literal["CN","US"]="CN",
    wb_store_id:str=Query(default="",max_length=32),wb_state:str=Query(default="",pattern="^(|available|listed|processing|retryable)$"),
    session:Session=Depends(get_db),
)->dict[str,Any]: return list_library_products(session,page=page,page_size=page_size,search=search,supplier=supplier,category=category,status=status,wb_store_id=wb_store_id,wb_state=wb_state,market=market)


@router.get("/library/products/{product_id}")
def pod_library_product(product_id:str,session:Session=Depends(get_db))->dict[str,Any]: return library_product_detail(session,product_id)


@router.post("/library/products/{product_id}/refresh-supplier")
def refresh_pod_library_product(product_id:str,session:Session=Depends(get_db))->dict[str,Any]: return refresh_supplier_facts(session,product_id)


@router.post("/library/products/{product_id}/open-folder")
def open_pod_library_product_folder(product_id:str,session:Session=Depends(get_db))->dict[str,Any]: return open_product_folder(session,product_id)


@router.post("/library/products/{product_id}/restore",status_code=202)
def restore_pod_library_product(product_id:str,session:Session=Depends(get_db))->dict[str,Any]: return enqueue_pod_product_restore(session,product_id)


@router.get("/library/categories")
def pod_library_categories(market:Literal["","CN","US"]="",session:Session=Depends(get_db))->dict[str,Any]: return list_pod_product_categories(session,market=market)


@router.get("/library/categories/compat")
def pod_library_categories_compat(session:Session=Depends(get_db))->dict[str,Any]:
    payload=list_pod_product_categories(session)
    return {"categories":[{"id":item["id"],"name":item["name"],"product_count":item["count"],"is_supplier":False} for item in payload["items"]]}


@router.post("/library/categories",status_code=201)
def add_pod_library_category(request:PodProductCategoryRequest,session:Session=Depends(get_db))->dict[str,Any]: return {"category":create_pod_product_category(session,request.name)}


@router.patch("/library/categories/{category_id}")
def edit_pod_library_category(category_id:str,request:PodProductCategoryRequest,session:Session=Depends(get_db))->dict[str,Any]: return {"category":rename_pod_product_category(session,category_id,request.name)}


@router.delete("/library/categories/{category_id}")
def remove_pod_library_category(category_id:str,session:Session=Depends(get_db))->dict[str,Any]: return delete_pod_product_category(session,category_id)


@router.patch("/library/products/batch-category")
def move_pod_library_products(request:PodProductCategoryBatch,session:Session=Depends(get_db))->dict[str,Any]: return {"updated":assign_pod_product_category(session,request.product_ids,request.category)}


class PodWbCodexApproval(BaseModel):
    revision:str=Field(min_length=64,max_length=64)
    output_sha256:str=Field(min_length=64,max_length=64)


@router.post("/wb/exports/{job_id}/approve-codex")
def approve_wb_codex(job_id:str,request:PodWbCodexApproval,session:Session=Depends(get_db))->dict[str,Any]:
    from erp.services import wb_codex
    return wb_codex.approve(session,job_id,request.revision,request.output_sha256)
