from __future__ import annotations

import io
import uuid

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import FileResponse, StreamingResponse
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload, load_only

from erp.services.publication_options import pricing_profile, publication_images, resolve_pod_image_template
from erp.core.database import get_db
from erp.core.config import get_settings
from erp.core.errors import ConflictError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.models import CatalogProduct, Job, JobEvent, JobStep, Publication, PublicationStore, Store, utc_now
from erp.schemas import ImportRecoveryRequest, PublicationCreate, PublicationPreviewRequest, PublicationPromotionRequest, PublicationStockRequest
from erp.serializers import job_dict, publication_dict, publication_store_dict, publication_summary_dict
from erp.services.catalog import get_catalog_product
from erp.services.jobs import enqueue_job, request_resume
from erp.services.publications import PUBLICATION_STEPS, _price, _review_draft, build_ozon_merge_plan, build_publication_snapshot, get_publication, product_snapshot, publication_query, publication_status, resolve_publication_snapshot, review_publication_image, validate_product_facts
from erp.modules.ozon_follow.legacy.services.ai_listing import pending_main_image_path
from erp.integrations.ozon import OzonSellerAdapter


router = APIRouter(prefix="/publications", tags=["publications"])


@router.get('/stock-quantities')
def publication_stock_quantities(child_ids: str = Query('', max_length=6500),
                                 session: Session = Depends(get_db)) -> dict:
    from erp.services.catalog_ozon_stock_quantity import confirmed_stock_quantities
    identities = [value for value in child_ids.split(',') if value]
    if len(identities) > 200 or any(len(value) != 32 or not all(char in '0123456789abcdef' for char in value) for value in identities):
        raise ConflictError('库存查询最多接受200个有效商品记录')
    return {'quantities': confirmed_stock_quantities(session, identities)}


@router.get("")
def list_publications(
    page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=200),
    product_id: str = "", status: str = "", store_status: str = "", compact: bool = False, session: Session = Depends(get_db),
) -> dict:
    conditions=[]
    if product_id: conditions.append(Publication.product_id==product_id)
    if status: conditions.append(Publication.status==status)
    if store_status: conditions.append(Publication.stores.any(PublicationStore.status==store_status))
    total=int(session.scalar(select(func.count()).select_from(Publication).where(*conditions)) or 0)
    options=[selectinload(Publication.product).load_only(CatalogProduct.id,CatalogProduct.name,CatalogProduct.sku),selectinload(Publication.variant),selectinload(Publication.stores).selectinload(PublicationStore.store)]
    if compact:
        options=[load_only(Publication.id,Publication.product_id,Publication.variant_id),options[0],options[1],
            selectinload(Publication.stores).load_only(PublicationStore.id,PublicationStore.publication_id,PublicationStore.store_id,PublicationStore.offer_id,PublicationStore.status,PublicationStore.pricing_json,PublicationStore.stock_status,PublicationStore.promotion_status,PublicationStore.external_product_id,PublicationStore.external_task_id,PublicationStore.last_error).selectinload(PublicationStore.store).load_only(Store.id,Store.name)]
    items=session.scalars(select(Publication).options(*options).where(*conditions).order_by(Publication.created_at.desc()).offset((page-1)*page_size).limit(page_size)).all()
    return {"items":[_queue_summary(item,store_status) if compact else publication_summary_dict(item) for item in items],"page":page,"page_size":page_size,"total":total}


def _queue_summary(item, stage):
    return {'id':item.id,'product_name':item.product.name if item.product else '',
        'sku':item.variant.sku if item.variant else (item.product.sku if item.product else ''),
        'stores':[{'id':c.id,'store_id':c.store_id,'store_name':c.store.name if c.store else '',
            'offer_id':c.offer_id,'status':c.status,'pricing':loads(c.pricing_json,{}),
            'stock_status':c.stock_status,'promotion_status':c.promotion_status,
            'external_product_id':c.external_product_id,'external_task_id':c.external_task_id,
            'last_error':c.last_error} for c in item.stores if not stage or c.status==stage]}


@router.post('/stock-scan')
def scan_before_stock(request: dict, session: Session = Depends(get_db)) -> dict:
    from erp.services.catalog_ozon_stock_scan import enqueue
    return enqueue(session, request.get('entries'), request.get('store_id',''), bool(request.get('refresh',False)))


@router.get("/pricing-options")
def publication_pricing_options(session: Session = Depends(get_db)) -> dict:
    from erp.modules.ozon_follow.legacy.services.pricing import OZON_RFBS_COMMISSION_TARIFFS
    defaults={"target_cost_profit_margin":30,"pricing_advertising_rate":0,"pricing_return_rate":10,"pricing_sticker_fee":2.5,"pricing_operating_cost":3,"pricing_logistics_service_rate":2,"pricing_price_multiplier":3,"pricing_old_price_multiplier":3.7,"pricing_category_module":"","pricing_product_category":""}
    store_profiles=[]
    for store in session.scalars(select(Store).where(Store.platform=="ozon",Store.enabled.is_(True)).order_by(Store.name)):
        profile=pricing_profile(resolve_publication_snapshot(build_publication_snapshot(session,store.id,{})),{})
        store_profiles.append({"store_id":store.id,"store_name":store.name,"values":{key:profile.get(key,value) for key,value in defaults.items()}})
    return {"stores":store_profiles,"modules": [{"name":module,"categories":[{"name":name,"rates":[str(rate*100) for rate in rates]} for name,rates in categories.items()]} for module,categories in OZON_RFBS_COMMISSION_TARIFFS.items()]}


def selected_publication_variants(products, variant_ids):
    active = {v.id for p in products for v in p.variants if v.active}
    chosen = None if variant_ids is None else set(variant_ids)
    if chosen is not None and (not chosen or not chosen.issubset(active)):
        raise ConflictError("所选规格不存在、已停用或不属于本批产品，请刷新后重新选择")
    return {p.id: ([v for v in p.variants if v.active and (chosen is None or v.id in chosen)]
                   or ([None] if chosen is None and not any(v.active for v in p.variants) else [])) for p in products}


@router.post("/preview")
def preview_publications(request: PublicationPreviewRequest, session: Session = Depends(get_db)) -> dict:
    products=[get_catalog_product(session,product_id) for product_id in dict.fromkeys(request.product_ids)]
    variant_map=selected_publication_variants(products,request.variant_ids)
    stores=session.scalars(select(Store).where(Store.id.in_(request.store_ids),Store.platform=="ozon",Store.enabled.is_(True))).all()
    if len(stores)!=len(set(request.store_ids)):
        raise ConflictError("部分 OZON 店铺不存在或已停用")
    rows=[]
    for product in products:
        variants=variant_map[product.id]
        for variant in variants:
            facts=product_snapshot(product,variant)
            requirements=resolve_pod_image_template(facts,request.requirements)
            facts["assets"]=publication_images(facts,requirements)
            missing=validate_product_facts(facts,requirements)
            for store in stores:
                snapshot=resolve_publication_snapshot(build_publication_snapshot(session,store.id,{}))
                pricing=None; pricing_error=""
                try:
                    pricing=_price(snapshot,facts,request.requirements)
                except Exception as error:
                    pricing_error=str(error)
                profile=pricing_profile(snapshot,request.requirements)
                rows.append({
                    "key":f"{product.id}:{variant.id if variant else ''}:{store.id}",
                    "product_id":product.id,"product_name":product.name,"sku":facts["sku"],
                    "store_id":store.id,"store_name":store.name,"offer_id":f"{store.offer_prefix}{facts['sku']}" if store.offer_prefix else facts["sku"],
                    "purchase_price_cny":facts.get("purchase_price_cny"),"domestic_delivery_cny":facts.get("domestic_delivery_cny"),
                    "package":{"length_mm":facts.get("package_length_mm"),"width_mm":facts.get("package_width_mm"),"height_mm":facts.get("package_height_mm"),"weight_g":facts.get("package_weight_g"),"dimensions_source":facts.get("package_dimensions_source")},
                    "category_display":request.requirements.get("category_display") or facts.get("category_display") or "",
                    "image_count":len(publication_images(facts,requirements)),
                    "missing":missing,"pricing":pricing,"pricing_error":pricing_error,
                    "pricing_profile":{
                        "category_module":profile.get("pricing_category_module",""),"product_category":profile.get("pricing_product_category",""),"target_cost_profit_margin":profile.get("target_cost_profit_margin",30),
                        "sticker_fee":profile.get("pricing_sticker_fee",2.5),"operating_cost":profile.get("pricing_operating_cost",3),
                        "advertising_rate":profile.get("pricing_advertising_rate",0),"return_rate":profile.get("pricing_return_rate",10),"logistics_service_rate":profile.get("pricing_logistics_service_rate",2),
                        "price_multiplier":profile.get("pricing_price_multiplier",3),"old_price_multiplier":profile.get("pricing_old_price_multiplier",3.7),
                    },
                })
    return {"items":rows,"count":len(rows)}


@router.get("/{publication_id}")
def publication_detail(publication_id: str, session: Session = Depends(get_db)) -> dict:
    return publication_dict(get_publication(session,publication_id))


@router.get("/{publication_id}/stores/{store_id}/image-preview")
def publication_image_preview(publication_id: str, store_id: str, session: Session = Depends(get_db)):
    publication=get_publication(session,publication_id);child=next((row for row in publication.stores if row.store_id==store_id),None)
    if not child: raise NotFoundError("店铺发布记录不存在")
    review = (loads(child.assets_json, {}).get('personal_review') or {})
    if review.get('url') and child.status == 'image_review':
        from fastapi.responses import RedirectResponse
        return RedirectResponse(review['url'], status_code=302, headers={'Cache-Control': 'no-store'})
    return FileResponse(pending_main_image_path(_review_draft(publication,child)),media_type="image/jpeg",headers={"Cache-Control":"no-store"})


@router.post("/{publication_id}/stores/{store_id}/image/{action}")
def publication_image_action(publication_id: str, store_id: str, action: str, session: Session = Depends(get_db), request: dict = Body(default={})) -> dict:
    publication=get_publication(session,publication_id);child=next((row for row in publication.stores if row.store_id==store_id),None)
    if not child: raise NotFoundError("店铺发布记录不存在")
    if action == 'approve' and 'expected_review' in request:
        current_review=loads(child.assets_json, {}).get('personal_review') or {}
        if request['expected_review'] != current_review:
            raise ConflictError('候选图片已变化，请刷新并重新核验')
    if action == 'approve' and loads(child.assets_json, {}).get('personal_review'):
        from erp.core.task_identity import task_identity
        from erp.services.catalog_personal_publications import approve_review_candidate
        job = session.get(Job, loads(child.assets_json,{}).get('personal_prepare_job_id') or publication.job_id)
        if not job:
            raise ConflictError('原电脑准备任务不存在')
        actor = task_identity.get() or {}
        approve_review_candidate(session, job, store_id, actor.get('id', ''))
        resumed = request_resume(session, job.id)
        session.commit()
        return {'store': publication_store_dict(child), 'job': job_dict(resumed)}
    if action in {"regenerate","redesign","full_redesign","repair"} and loads(child.assets_json, {}).get('personal_review'):
        from erp.core.task_identity import task_identity
        from erp.services.catalog_personal_publications import request_image_regeneration
        job = session.get(Job, loads(child.assets_json,{}).get('personal_prepare_job_id') or publication.job_id)
        if not job: raise ConflictError('原电脑准备任务不存在')
        request_image_regeneration(session, job, store_id, (task_identity.get() or {}).get('id',''), action)
        if loads(publication.requirements_json,{}).get('generation_provider')=='codex':
            job.status='paused';job.pause_requested=True;session.commit()
            return {'store':publication_store_dict(child),'job':None,'codex_batch_id':publication.batch_id}
        resumed = request_resume(session, job.id)
        session.commit()
        return {'store': publication_store_dict(child), 'job': job_dict(resumed)}
    if get_settings().multiuser_enabled:
        raise ConflictError('该历史图片没有原电脑核验任务，请重新创建发布准备任务')
    if action in {"regenerate","redesign","full_redesign","repair"}:
        job=enqueue_job(
            session,
            job_type="catalog_publication_image_action",
            payload={"publication_id":publication.id,"store_id":store_id,"action":action},
            config_snapshot=child.settings_snapshot_json,
            idempotency_key=f"publication-image:{action}:{child.id}:{uuid.uuid4().hex}",
            steps=[(f"{store_id}:image_{action}",f"{child.store.name} · 重新生成主图")],
            max_attempts=3,
        )
        return {"store":publication_store_dict(child),"job":job_dict(job)}
    review_publication_image(session,publication,child,action);resumed=None
    if action=="approve" and publication.job_id:
        job=session.get(Job,publication.job_id)
        if job and job.status=="paused": resumed=request_resume(session,job.id)
    return {"store":publication_store_dict(child),"job":job_dict(resumed) if resumed else None}


@router.post("", status_code=202)
def create_publications(request: PublicationCreate, session: Session = Depends(get_db)) -> dict:
    if request.generation_provider == 'codex':
        from erp.core.task_identity import task_identity
        from erp.models import WorkerNode
        actor=task_identity.get() or {}
        device=session.get(WorkerNode,actor.get('worker_id',''))
        if not get_settings().multiuser_enabled or not device or 'codex_handoff_v1' not in loads(device.capabilities_json,[]):
            raise ConflictError('请连接支持 Codex 接手的新版 ERP 客户端，再创建任务')
        if request.image_policy!='reuse' and 'codex_ozon_poster_v1' not in loads(device.capabilities_json,[]):
            raise ConflictError('Codex生图需要0.6.68或更新客户端，才能使用原3:4俄文海报规则与比例校验')
    products=[get_catalog_product(session,product_id) for product_id in dict.fromkeys(request.product_ids)]
    variant_map=selected_publication_variants(products,request.variant_ids)
    stores=session.scalars(select(Store).where(Store.id.in_(request.store_ids),Store.platform=="ozon",Store.enabled.is_(True))).all()
    if len(stores)!=len(set(request.store_ids)): raise ConflictError("部分 OZON 店铺不存在或已停用")
    for store in stores:pricing_profile(resolve_publication_snapshot(build_publication_snapshot(session,store.id,{})),request.requirements)
    if request.save_generated_to_catalog and len(stores)!=1: raise ConflictError("设为产品库当前图片时请只选择一家店，避免多店图片相互覆盖")
    facts_list=[product_snapshot(product,variant) for product in products for variant in variant_map[product.id]]
    for facts in facts_list:
        publication_images(facts,resolve_pod_image_template(facts,request.requirements))
    merge_plans: dict[str,dict]={}
    if request.ozon_merge.mode=="custom_group" and not get_settings().multiuser_enabled:
        category_pairs={(int(request.requirements.get("description_category_id") or facts.get("description_category_id") or 0),int(request.requirements.get("type_id") or facts.get("type_id") or 0)) for facts in facts_list}
        if len(category_pairs)!=1 or (0,0) in category_pairs: raise ConflictError("自定义合并组内商品必须使用同一个明确的 Ozon 类目")
        category_id,type_id=next(iter(category_pairs));adapter=OzonSellerAdapter(resolve_publication_snapshot(build_publication_snapshot(session,stores[0].id,{})))
        definitions=adapter.category_attributes(category_id,type_id,"ZH_HANS")
        if not any("型号名称" in str(item.get("name") or "") for item in definitions): raise ConflictError("当前 Ozon 类目不支持通过型号名称合并商品卡")
        dictionary_values={attribute_id:adapter.category_attribute_values(category_id,type_id,attribute_id,"ZH_HANS") for attribute_id in request.ozon_merge.variant_attribute_ids}
        merge_plans=build_ozon_merge_plan(facts_list,definitions,dictionary_values,model_name=request.ozon_merge.model_name,variant_attribute_ids=request.ozon_merge.variant_attribute_ids)
    batch_id=uuid.uuid4().hex; created=[]
    overrides={key:value for key,value in {"stock":request.stock,"warehouse_id":request.warehouse_id,"promotion_action_id":request.promotion_action_id,"action_price":request.action_price}.items() if value is not None}
    for product in products:
        variants=variant_map[product.id]
        for variant in variants:
            facts=product_snapshot(product,variant)
            requirements=resolve_pod_image_template(facts,request.requirements)
            facts["assets"]=publication_images(facts,requirements)
            requirements={**requirements,"ozon_merge":merge_plans.get(str(facts["sku"]),{"mode":"single"})}
            requirements['generation_provider']=request.generation_provider
            if request.ozon_merge.mode == 'custom_group' and get_settings().multiuser_enabled:
                category_pairs = {(int(request.requirements.get('description_category_id') or row.get('description_category_id') or 0),
                                   int(request.requirements.get('type_id') or row.get('type_id') or 0)) for row in facts_list}
                if len(category_pairs) != 1 or (0, 0) in category_pairs:
                    raise ConflictError('自定义合并组内商品必须使用同一个明确的 Ozon 类目')
                requirements['personal_merge_request'] = {**request.ozon_merge.model_dump(),
                    'facts': [{key: row.get(key) for key in ('sku', 'color', 'size')} for row in facts_list]}
            publication=Publication(batch_id=batch_id,product_id=product.id,variant_id=variant.id if variant else None,requirements_json=dumps(requirements),image_policy_json=dumps({"mode":request.image_policy,"roles":request.custom_regenerate_roles}),save_generated_to_catalog=request.save_generated_to_catalog,submit_via_api=request.submit_via_api and not request.dry_run,product_snapshot_json=dumps(facts))
            session.add(publication);session.flush()
            for store in stores:
                offer_id=f"{store.offer_prefix}{facts['sku']}" if store.offer_prefix else str(facts["sku"])
                session.add(PublicationStore(publication_id=publication.id,store_id=store.id,offer_id=offer_id,settings_snapshot_json=build_publication_snapshot(session,store.id,overrides),idempotency_key=f"publication:{publication.id}:{store.id}"))
            session.flush()
            steps=[(f"{store.id}:{key}",f"{store.name} · {label}") for store in stores for key,label in PUBLICATION_STEPS]
            job=enqueue_job(session,job_type="catalog_ozon_prepare",payload={"publication_id":publication.id,"batch_id":batch_id,"generation_provider":request.generation_provider},config_snapshot=dumps({"publication_id":publication.id,"store_snapshot_ids":[store.id for store in stores]}),idempotency_key=f"catalog_publication:{publication.id}",steps=steps,max_attempts=3,commit=request.generation_provider!='codex')
            if request.generation_provider=='codex':
                job.status='paused';job.pause_requested=True
                publication.status='waiting_codex'
                for child in get_publication(session,publication.id).stores:child.status='waiting_codex'
                session.add(JobEvent(job_id=job.id,message='等待 Codex 接手文案与所选生图；不会调用配置的生成模型'))
            if get_settings().multiuser_enabled:
                for child in get_publication(session,publication.id).stores:
                    state=loads(child.assets_json,{})
                    state['personal_prepare_job_id']=job.id
                    child.assets_json=dumps(state)
            publication.job_id=job.id;session.commit();created.append({"publication":publication_dict(get_publication(session,publication.id)),"job":job_dict(job)})
    return {"batch_id":batch_id,"items":created,"publication_count":len(created),"store_count":len(stores)}


@router.post("/{publication_id}/stores/{store_id}/recover-import")
def recover_publication_import(publication_id: str, store_id: str, request: ImportRecoveryRequest, session: Session = Depends(get_db)) -> dict:
    publication=get_publication(session,publication_id);child=next((row for row in publication.stores if row.store_id==store_id),None)
    if not child: raise NotFoundError("店铺发布记录不存在")
    if child.status not in {"import_uncertain","import_pending","failed"}: raise ConflictError("只有结果不确定、等待中或失败的记录可绑定 task_id")
    if get_settings().multiuser_enabled:
        from erp.services.jobs import _controllable_job
        parent_id=loads(child.assets_json,{}).get('personal_submit_job_id') or loads(child.assets_json,{}).get('personal_prepare_job_id') or publication.job_id
        original=_controllable_job(session,parent_id)
        if original.type!='catalog_ozon_publication': raise ConflictError('没有本次电脑建品任务')
        if child.external_task_id and child.external_task_id!=str(request.external_task_id):
            raise ConflictError('不能覆盖已保存的原 Ozon task_id')
        if original.status in {'running','queued','retrying'}: raise ConflictError('原电脑任务尚在执行，请先等待')
    child.external_task_id=str(request.external_task_id);child.status="import_pending";child.last_error="";session.commit()
    return publication_store_dict(child)


@router.post("/{publication_id}/stores/{store_id}/retry", status_code=202)
def retry_publication_store(publication_id: str, store_id: str, session: Session = Depends(get_db)) -> dict:
    publication=get_publication(session,publication_id);child=next((row for row in publication.stores if row.store_id==store_id),None)
    if not child: raise NotFoundError("店铺发布记录不存在")
    if child.status in {"completed","ready_for_export"}: raise ConflictError("该店铺已完成，不会重复提交")
    if child.status=="import_uncertain" and not child.external_task_id: raise ConflictError("请先绑定原 OZON task_id，避免重复建品")
    if get_settings().multiuser_enabled:
        from erp.services.jobs import _controllable_job, retry_job
        parent_id=loads(child.assets_json,{}).get('personal_submit_job_id') or loads(child.assets_json,{}).get('personal_prepare_job_id') or publication.job_id
        original=_controllable_job(session,parent_id)
        if original.type=='catalog_ozon_publication':
            if not child.external_task_id: raise ConflictError('请先核对原 Ozon task_id，不会重新建品')
            if child.status not in {'import_pending','import_uncertain'}: raise ConflictError('原导入已结束，请核对平台结果')
            if original.status=='action_required':
                reference=loads(original.external_ref,{})
                if reference.get('state')!='pending_result': raise ConflictError('原导入阶段不允许回查')
                reference.pop('finished_hash',None)
                original.external_ref=dumps(reference)
                original.status='failed'
            resumed=retry_job(session,original.id)
        elif original.type=='catalog_ozon_prepare':
            if child.status in {'ready_for_submit','image_review'}: raise ConflictError('请使用原提交或图片核验按钮')
            resumed=retry_job(session,original.id)
        else: raise ConflictError('原任务不支持产品库恢复')
        return {'publication':publication_dict(publication),'store':publication_store_dict(child),'job':job_dict(resumed)}
    child.last_error="";child.status="import_pending" if child.external_task_id else "queued"
    steps=[(f"{store_id}:{key}",f"{child.store.name} · {label}") for key,label in PUBLICATION_STEPS]
    retry_token=uuid.uuid4().hex
    job=enqueue_job(session,job_type="catalog_ozon_publication" if child.external_task_id else "catalog_ozon_prepare",payload={"publication_id":publication.id,"batch_id":publication.batch_id,"store_ids":[store_id]},config_snapshot=dumps({"publication_id":publication.id,"store_snapshot_ids":[store_id],"retry":True}),idempotency_key=f"catalog_publication_retry:{publication.id}:{store_id}:{retry_token}",steps=steps,max_attempts=3)
    publication.job_id=job.id;publication.status="running";session.commit()
    return {"publication":publication_dict(get_publication(session,publication.id)),"store":publication_store_dict(child),"job":job_dict(job)}


@router.post("/{publication_id}/stores/{store_id}/submit", status_code=202)
def submit_publication_store(publication_id: str, store_id: str, session: Session = Depends(get_db)) -> dict:
    publication=get_publication(session,publication_id);child=next((row for row in publication.stores if row.store_id==store_id),None)
    if not child: raise NotFoundError("店铺发布记录不存在")
    if child.status!="ready_for_submit" or child.external_task_id: raise ConflictError("只有已完成人工核验且尚未提交的商品可以提交")
    steps=[(f"{store_id}:{key}",f"{child.store.name} · {label}") for key,label in PUBLICATION_STEPS if key in {"validate","submit","stock","promotion"}]
    job=enqueue_job(session,job_type="catalog_ozon_publication",payload={"publication_id":publication.id,"batch_id":publication.batch_id,"store_ids":[store_id],"submit_ready":True},config_snapshot=dumps({"publication_id":publication.id,"store_snapshot_ids":[store_id],"submit_ready":True}),idempotency_key=f"catalog_publication_submit:{publication.id}:{store_id}",steps=steps,max_attempts=3)
    if get_settings().multiuser_enabled:
        state=loads(child.assets_json,{})
        state['personal_submit_job_id']=job.id
        child.assets_json=dumps(state)
    publication.job_id=job.id;publication.status="running";child.status="submit_queued";session.commit()
    return {"publication":publication_dict(get_publication(session,publication.id)),"store":publication_store_dict(child),"job":job_dict(job)}


def _publication_store(session: Session, publication_id: str, store_id: str) -> tuple[Publication, PublicationStore]:
    publication=get_publication(session,publication_id)
    child=next((row for row in publication.stores if row.store_id==store_id),None)
    if not child: raise NotFoundError("店铺发布记录不存在")
    return publication,child


def _refresh_publication_status(publication: Publication) -> None:
    publication.status=publication_status(publication.stores)


def _complete_followup_step(session: Session, publication: Publication, store_id: str, phase: str, details: dict | None = None) -> None:
    if not publication.job_id:
        return
    child=next((row for row in publication.stores if row.store_id==store_id),None)
    parent_id=(loads(child.assets_json,{}).get('personal_submit_job_id') if child and get_settings().multiuser_enabled else '') or publication.job_id
    job=session.get(Job,parent_id)
    step=session.scalar(select(JobStep).where(JobStep.job_id==parent_id,JobStep.key==f"{store_id}:{phase}"))
    if step:
        step.status="completed";step.progress=100;step.details_json=dumps(details or {});step.error_message="";step.started_at=step.started_at or utc_now();step.finished_at=utc_now()
    if not job:
        return
    completed=int(session.scalar(select(func.count()).select_from(JobStep).where(JobStep.job_id==job.id,JobStep.status=="completed")) or 0)
    total=int(session.scalar(select(func.count()).select_from(JobStep).where(JobStep.job_id==job.id)) or 0)
    job.progress=round(completed*100/max(1,total),2)
    if (child.status=="completed" if child and get_settings().multiuser_enabled else publication.status=="completed"):
        job.status="completed";job.progress=100;job.finished_at=utc_now();job.error_code="";job.error_message=""
        session.add(JobEvent(job_id=job.id,message="Ozon 建品、库存和促销流程已全部处理完成"))


@router.post("/{publication_id}/stores/{store_id}/stock")
def add_publication_stock(publication_id: str, store_id: str, request: PublicationStockRequest, session: Session = Depends(get_db)) -> dict:
    publication,child=_publication_store(session,publication_id,store_id)
    if child.status!="stock_pending": raise ConflictError("该商品当前不在待添加库存阶段")
    if get_settings().multiuser_enabled:
        from erp.services.catalog_personal_followup import enqueue
        job = enqueue(session, publication, child, 'stock', request.model_dump(mode='json'))
        return {'job_id': job.id, 'store': publication_store_dict(child)}
    OzonSellerAdapter(resolve_publication_snapshot(child.settings_snapshot_json)).update_stock(child.offer_id,request.warehouse_id,request.stock)
    child.stock_status="completed";child.promotion_status="pending";child.status="promotion_pending";child.last_error=""
    assets=loads(child.assets_json,{})
    assets['stock_confirmation']={'quantity':request.stock,'warehouse_id':request.warehouse_id}
    child.assets_json=dumps(assets)
    _refresh_publication_status(publication);_complete_followup_step(session,publication,store_id,"stock",{"stock":request.stock,"warehouse_id":request.warehouse_id});session.commit()
    return publication_store_dict(child)


@router.post("/{publication_id}/stores/{store_id}/skip-stock")
def skip_publication_stock(publication_id: str, store_id: str, session: Session = Depends(get_db)) -> dict:
    publication,child=_publication_store(session,publication_id,store_id)
    if child.status!="stock_pending": raise ConflictError("该商品当前不在待添加库存阶段")
    if child.stock_status in {"processing", "unknown"}: raise ConflictError("库存操作正在执行或结果未知，请先核对原任务")
    child.stock_status="skipped";child.promotion_status="pending";child.status="promotion_pending";child.last_error=""
    _refresh_publication_status(publication);_complete_followup_step(session,publication,store_id,"stock",{"skipped":True});session.commit()
    return publication_store_dict(child)


@router.post("/{publication_id}/stores/{store_id}/promotion")
def add_publication_promotion(publication_id: str, store_id: str, request: PublicationPromotionRequest, session: Session = Depends(get_db)) -> dict:
    publication,child=_publication_store(session,publication_id,store_id)
    if child.status!="promotion_pending": raise ConflictError("该商品当前不在待添加促销阶段")
    product_id=int(child.external_product_id or 0)
    if not product_id: raise ConflictError("OZON 商品 ID 尚未回写，暂时不能添加促销")
    if get_settings().multiuser_enabled:
        from erp.services.catalog_personal_followup import enqueue
        job = enqueue(session, publication, child, 'promotion', request.model_dump(mode='json'))
        return {'job_id': job.id, 'store': publication_store_dict(child)}
    adapter=OzonSellerAdapter(resolve_publication_snapshot(child.settings_snapshot_json))
    if not adapter.find_promotion_candidate(request.action_id,product_id):
        child.promotion_status="pending_eligibility";session.commit()
        raise ConflictError("OZON 暂未将该商品列入此活动候选，请稍后重试")
    adapter.activate_promotion(request.action_id,product_id,request.action_price,request.stock)
    child.promotion_status="completed";child.status="completed";child.last_error=""
    _refresh_publication_status(publication);_complete_followup_step(session,publication,store_id,"promotion",{"action_id":request.action_id});session.commit()
    return publication_store_dict(child)


@router.post("/{publication_id}/stores/{store_id}/skip-promotion")
def skip_publication_promotion(publication_id: str, store_id: str, session: Session = Depends(get_db)) -> dict:
    publication,child=_publication_store(session,publication_id,store_id)
    if child.status!="promotion_pending": raise ConflictError("该商品当前不在待添加促销阶段")
    if child.promotion_status in {"processing", "unknown"}: raise ConflictError("促销操作正在执行或结果未知，请先核对原任务")
    child.promotion_status="skipped";child.status="completed";child.last_error=""
    _refresh_publication_status(publication);_complete_followup_step(session,publication,store_id,"promotion",{"skipped":True});session.commit()
    return publication_store_dict(child)


def _flat(value):
    if isinstance(value,(dict,list)): return dumps(value)
    return value


@router.get("/{publication_id}/export/ozon.xlsx")
def export_publication_excel(publication_id: str, session: Session = Depends(get_db)):
    publication=get_publication(session,publication_id)
    rows=[]
    for child in publication.stores:
        state=loads(child.assets_json,{})
        item=state.get("import_item")
        if not item: raise ConflictError(f"{child.store.name} 尚未生成标准发布包")
        rows.append({"店铺":child.store.name,**item,"俄文描述":loads(child.content_json,{}).get("description_ru",""),"主题标签":loads(child.content_json,{}).get("tags",[])})
    headers=["店铺","offer_id","name","俄文描述","主题标签","description_category_id","type_id","price","old_price","currency_code","depth","width","height","weight","dimension_unit","weight_unit","primary_image","images","color_image","attributes","complex_attributes","barcode","vat","pdf_list"]
    workbook=Workbook();sheet=workbook.active;sheet.title="OZON发布包";sheet.append(headers)
    for row in rows: sheet.append([_flat(row.get(header,"")) for header in headers])
    for column in sheet.columns: sheet.column_dimensions[column[0].column_letter].width=min(55,max(12,max(len(str(cell.value or "")) for cell in column)+2))
    meta=workbook.create_sheet("发布信息");meta.append(["发布ID",publication.id]);meta.append(["批次ID",publication.batch_id]);meta.append(["产品ID",publication.product_id]);meta.append(["生成原则","本表与 Seller API 提交使用同一标准发布包，导出不会再次调用 AI"])
    stream=io.BytesIO();workbook.save(stream);stream.seek(0)
    return StreamingResponse(stream,media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",headers={"Content-Disposition":f"attachment; filename=ozon-publication-{publication.id[:8]}.xlsx"})


from pydantic import BaseModel, Field
class OzonBatchEntry(BaseModel):
    publication_id: str = Field(min_length=1,max_length=100)
    store_id: str = Field(min_length=1,max_length=100)
    params: dict = Field(default_factory=dict)

class OzonBatchRequest(BaseModel):
    entries: list[OzonBatchEntry] = Field(min_length=1,max_length=1000)

@router.post('/batch/{phase}', status_code=202)
def batch_publications(phase: str, request: OzonBatchRequest, session: Session = Depends(get_db)):
    from erp.services.catalog_ozon_batch import enqueue
    return enqueue(session,phase,[entry.model_dump(mode='json') for entry in request.entries])
