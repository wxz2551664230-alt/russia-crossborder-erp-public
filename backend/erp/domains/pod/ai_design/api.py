from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from erp.core.database import get_db
from erp.core.config import get_settings
from erp.core.errors import AppError, NotFoundError
from erp.core.jsonutil import dumps
from erp.models import IntegrationConnection
from erp.serializers import connection_dict, job_dict
from erp.services.jobs import enqueue_job

from .service import MAX_BATCH_IMAGES, MAX_UPLOAD_BYTES, batch_dir, calculate_size, list_batches, load_manifest, public_manifest, runtime_status, save_manifest, save_upload


router = APIRouter(prefix="/pod/ai-design", tags=["pod-ai-design"])


class PlanItemUpdate(BaseModel):
    index: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)
    concept: str = Field(default="", max_length=2000)
    prompt: str = Field(min_length=1, max_length=32000)
    negative_prompt: str = Field(default="", max_length=8000)
    selected: bool = True


class PlanUpdate(BaseModel):
    items: list[PlanItemUpdate] = Field(min_length=1, max_length=MAX_BATCH_IMAGES)


@router.post('/uploads',status_code=201)
async def stage_cloud_input(image:UploadFile=File(...),session:Session=Depends(get_db)):
    if not get_settings().multiuser_enabled:raise AppError('此接口用于多人电脑任务')
    from . import cloud
    try:return cloud.stage(session,await image.read(MAX_UPLOAD_BYTES+1),image.filename or 'input.png')
    finally:await image.close()


@router.get("/status")
def status(session: Session = Depends(get_db)) -> dict[str, Any]:
    if get_settings().multiuser_enabled:
        from . import cloud
        return cloud.status(session)
    connections = session.scalars(select(IntegrationConnection).where(IntegrationConnection.provider.in_(["ai_chat", "ai_image"]), IntegrationConnection.enabled.is_(True)).order_by(IntegrationConnection.provider, IntegrationConnection.name)).all()
    return {"runtime": runtime_status(), "connections": [connection_dict(row) for row in connections], "batches": list_batches()}


@router.get("/batches")
def batches(session:Session=Depends(get_db)) -> dict[str, Any]:
    if get_settings().multiuser_enabled:
        from . import cloud
        return {'items':cloud.status(session)['batches']}
    return {"items": list_batches()}


@router.get("/batches/{batch_id}")
def batch_detail(batch_id: str,session:Session=Depends(get_db)) -> dict[str, Any]:
    if get_settings().multiuser_enabled:
        from . import cloud
        return cloud.read(session,batch_id)
    return public_manifest(load_manifest(batch_id))


@router.post("/batches", status_code=202)
async def create_batch(
    brief: str = Form(min_length=1, max_length=8000),
    product_type: str = Form(default="桌布 / 毛毯", max_length=200),
    count: int = Form(default=4, ge=1, le=MAX_BATCH_IMAGES),
    aspect_ratio: str = Form(default="4:3", max_length=20),
    max_edge: int = Form(default=3264, ge=256, le=3840),
    quality: Literal["low", "medium", "high", "auto"] = Form(default="high"),
    background: Literal["transparent", "opaque", "auto"] = Form(default="opaque"),
    output_format: Literal["png", "jpeg", "webp"] = Form(default="png"),
    upscale_scale: int = Form(default=2, ge=0, le=4),
    planner_connection_id: str = Form(min_length=1, max_length=32),
    image_connection_id: str = Form(min_length=1, max_length=32),
    planner_model: str = Form(default="gpt-5.6-sol", max_length=120),
    image_model: str = Form(default="gpt-image-2-openai", max_length=120),
    custom_instruction: str = Form(default="", max_length=8000),
    references: list[UploadFile] = File(default=[]),
    uploaded_inputs: str = Form(default='[]'),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    if upscale_scale not in {0, 2, 4}:
        raise AppError("AI 超分倍率仅支持关闭、2×或4×", code="invalid_upscale_scale")
    if get_settings().multiuser_enabled:
        from . import cloud
        from erp.core.jsonutil import loads
        ids=loads(uploaded_inputs,[])
        if not isinstance(ids,list):raise AppError('图片列表无效')
        for upload in references:ids.append(cloud.stage(session,await upload.read(MAX_UPLOAD_BYTES+1),upload.filename or 'reference.png')['id'])
        return cloud.create(session,'generation',{'brief':brief,'product_type':product_type,'count':count,'aspect_ratio':aspect_ratio,'max_edge':max_edge,'quality':quality,'background':background,'output_format':output_format,'upscale_scale':upscale_scale,'planner_connection_id':planner_connection_id,'image_connection_id':image_connection_id,'planner_model':planner_model,'image_model':image_model,'custom_instruction':custom_instruction},ids)
    if len(references) > 16:
        raise AppError("参考图最多 16 张", code="too_many_reference_images")
    for connection_id, provider in ((planner_connection_id, "ai_chat"), (image_connection_id, "ai_image")):
        row = session.get(IntegrationConnection, connection_id)
        if not row or row.provider != provider or not row.enabled:
            raise AppError(f"请选择已启用的 {provider} 接口连接", code="ai_connection_unavailable")
    batch_id = uuid.uuid4().hex
    batch_dir(batch_id).mkdir(parents=True)
    saved = []
    try:
        for upload in references:
            saved.append(save_upload(batch_id, upload.filename or "reference.png", await upload.read()))
    except Exception:
        import shutil
        shutil.rmtree(batch_dir(batch_id), ignore_errors=True)
        raise
    now = datetime.now(timezone.utc).isoformat()
    manifest = {"id": batch_id, "kind": "generation", "status": "planning", "created_at": now, "updated_at": now, "references": saved, "outputs": [], "config": {"brief": brief.strip(), "product_type": product_type.strip(), "count": count, "aspect_ratio": aspect_ratio.strip(), "size": calculate_size(aspect_ratio, max_edge), "max_edge": max_edge, "quality": quality, "background": background, "output_format": output_format, "upscale_scale": upscale_scale, "planner_connection_id": planner_connection_id, "image_connection_id": image_connection_id, "planner_model": planner_model.strip(), "image_model": image_model.strip(), "custom_instruction": custom_instruction.strip()}}
    save_manifest(manifest)
    job = enqueue_job(session, job_type="pod_ai_design_plan", payload={"batch_id": batch_id}, config_snapshot=dumps({"batch_id": batch_id, "size": manifest["config"]["size"], "count": count}), idempotency_key=f"pod_ai_design_plan:{batch_id}", max_attempts=2, steps=[("plan", "AI 设计规划")])
    manifest["plan_job_id"] = job.id
    save_manifest(manifest)
    return {"batch": public_manifest(manifest), "job": job_dict(job)}


@router.patch("/batches/{batch_id}/plan")
def update_plan(batch_id: str, request: PlanUpdate,session:Session=Depends(get_db)) -> dict[str, Any]:
    if get_settings().multiuser_enabled:
        from . import cloud
        return cloud.edit_plan(session,batch_id,[r.model_dump() for r in request.items])
    manifest = load_manifest(batch_id)
    if manifest.get("status") not in {"planned", "failed"}:
        raise AppError("当前批次不能修改规划", code="ai_plan_not_editable", status_code=409)
    manifest.setdefault("plan", {})["items"] = [row.model_dump() | {"status": "待生成"} for row in request.items]
    save_manifest(manifest)
    return public_manifest(manifest)


@router.post("/batches/{batch_id}/generate", status_code=202)
def generate(batch_id: str, session: Session = Depends(get_db)) -> dict[str, Any]:
    if get_settings().multiuser_enabled:
        from . import cloud
        return cloud.generate(session,batch_id)
    manifest = load_manifest(batch_id)
    if manifest.get("status") not in {"planned", "failed", "completed"} or not manifest.get("plan", {}).get("items"):
        raise AppError("请先完成并检查 AI 规划", code="ai_plan_required", status_code=409)
    manifest["status"] = "queued"
    save_manifest(manifest)
    revision = uuid.uuid4().hex
    job = enqueue_job(session, job_type="pod_ai_design_generate", payload={"batch_id": batch_id}, config_snapshot=dumps({"batch_id": batch_id, "revision": revision}), idempotency_key=f"pod_ai_design_generate:{batch_id}:{revision}", max_attempts=1, steps=[("generate", "批量生图与超分")])
    manifest["generation_job_id"] = job.id
    save_manifest(manifest)
    return {"batch": public_manifest(manifest), "job": job_dict(job)}


@router.post("/upscale", status_code=202)
async def create_upscale(
    scale: int = Form(default=2, ge=2, le=4),
    images: list[UploadFile] = File(default=[], max_length=MAX_BATCH_IMAGES),
    uploaded_inputs: str = Form(default='[]'),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    if scale not in {2, 4}:
        raise AppError("AI 超分倍率仅支持 2×或4×", code="invalid_upscale_scale")
    if get_settings().multiuser_enabled:
        from . import cloud
        from erp.core.jsonutil import loads
        ids=loads(uploaded_inputs,[])
        if not isinstance(ids,list):raise AppError('图片列表无效')
        for upload in images:ids.append(cloud.stage(session,await upload.read(MAX_UPLOAD_BYTES+1),upload.filename or 'input.png')['id'])
        return cloud.create(session,'upscale',{'upscale_scale':scale},ids)
    if not images:raise AppError('请选择超分图片')
    if not runtime_status()["available"]:
        raise AppError("Real-ESRGAN 运行时不可用", code="upscale_runtime_unavailable")
    batch_id = uuid.uuid4().hex
    batch_dir(batch_id).mkdir(parents=True)
    saved = []
    try:
        for upload in images:
            saved.append(save_upload(batch_id, upload.filename or "image.png", await upload.read(), folder="inputs"))
    except Exception:
        import shutil
        shutil.rmtree(batch_dir(batch_id), ignore_errors=True)
        raise
    now = datetime.now(timezone.utc).isoformat()
    manifest = {"id": batch_id, "kind": "upscale", "status": "queued", "created_at": now, "updated_at": now, "inputs": saved, "outputs": [], "config": {"upscale_scale": scale}}
    save_manifest(manifest)
    job = enqueue_job(session, job_type="pod_ai_design_upscale", payload={"batch_id": batch_id}, config_snapshot=dumps({"batch_id": batch_id, "scale": scale}), idempotency_key=f"pod_ai_design_upscale:{batch_id}", max_attempts=1, steps=[("upscale", f"本地 AI 超分 {scale}×")])
    manifest["job_id"] = job.id
    save_manifest(manifest)
    return {"batch": public_manifest(manifest), "job": job_dict(job)}


@router.get("/batches/{batch_id}/files/{folder}/{filename}")
def batch_file(batch_id: str, folder: Literal["references", "inputs", "outputs"], filename: str) -> FileResponse:
    root = (batch_dir(batch_id) / folder).resolve()
    path = (root / Path(filename).name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise NotFoundError("图片文件不存在")
    return FileResponse(path)
