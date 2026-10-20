from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from erp.core.config import get_settings
from erp.core.errors import AppError, ConflictError
from erp.core.jsonutil import dumps
from erp.models import CatalogImportRun
from erp.services.catalog import import_pod_database
from erp.services.jobs import JobContext, JobPaused, job_handler


LEGACY_URL = "http://127.0.0.1:8020"


def _health() -> bool:
    try:
        response=httpx.get(f"{LEGACY_URL}/health",timeout=1.5)
        return response.status_code==200 and response.json().get("application")=="hicustom-queue-composer"
    except Exception:
        return False


def ensure_pod_runtime() -> None:
    raise AppError("旧程序永久只读，ERP 不再启动旧内核；请使用 ERP 原生 POD 工作台", code="legacy_runtime_disabled", status_code=409)


def legacy_request(method: str, path: str, **kwargs: Any) -> Any:
    if method.upper() != "GET":
        raise AppError("禁止通过 ERP 修改旧程序", code="legacy_source_read_only", status_code=403)
    ensure_pod_runtime()


def pod_status() -> dict[str, Any]:
    settings=get_settings();root=settings.legacy_composer_root
    return {"available":_health(),"legacy_root":str(root),"artwork_index":str(root/"data"/"artwork_library.json"),"product_database":str(root/"data"/"product_library.db")}


@job_handler("pod_composition")
def pod_composition_job(context: JobContext) -> None:
    payload=context.payload
    context.start_step("submit")
    if not context.job.external_ref and payload.get("external_ref"):
        context.job.external_ref=str(payload["external_ref"]);context.session.commit()
    if not context.job.external_ref:
        placements=[]
        for side in payload.get("placements") or []:
            placements.append({key:side.get(key) for key in ("view_id","view_name","width","height","top_x","top_y","artwork_id","rotation")})
        data={"placements":json.dumps(placements,ensure_ascii=False),"product_type_id":str(payload["product_type_id"]),"product_name":payload["product_name"],"default_color_id":str(payload["default_color_id"]),"default_view_id":str(payload["default_view_id"]),"supplier":payload["supplier"],"category_id":payload.get("category_id","") ,"selected_size_ids":json.dumps(payload.get("selected_size_ids") or [])}
        created=legacy_request("POST","/api/jobs/multi",data=data,timeout=90)["job"]
        context.job.external_ref=str(created["id"]);context.session.commit()
    context.complete_step("submit",details={"external_job_id":context.job.external_ref})
    context.start_step("compose")
    deadline=time.monotonic()+60*60*3
    result={}
    while time.monotonic()<deadline:
        context.refresh()
        jobs=legacy_request("GET","/api/jobs",timeout=15).get("jobs",[])
        result=next((item for item in jobs if str(item.get("id"))==context.job.external_ref),{})
        if not result: raise AppError("POD 合成任务不存在",code="pod_external_job_missing")
        status=str(result.get("status") or "")
        current=float(result.get("progress_current") or 0);total=float(result.get("progress_total") or 0)
        context.set_progress("compose",current*100/total if total else (100 if status=="completed" else 10),str(result.get("stage_text") or "处理中"))
        if status=="completed": break
        if status=="failed":
            if result.get("outcome_unknown") and not result.get("product_code"):
                context.job.pause_requested=True;context.session.commit();raise JobPaused()
            raise AppError(str(result.get("error") or "POD 合成失败"),code="pod_composition_failed")
        if status=="canceled": raise ConflictError("POD 合成任务已取消")
        time.sleep(2)
    else: raise AppError("POD 合成等待超时",code="pod_composition_timeout")
    context.complete_step("compose",details={"product_code":result.get("product_code"),"library_sku":result.get("library_sku"),"oss_status":result.get("oss_status")})
    context.start_step("sync_catalog")
    source=get_settings().legacy_composer_root/"data"/"product_library.db"
    run=CatalogImportRun(kind="pod_history",source_path=str(source),dry_run=False,status="running",job_id=context.job.id)
    context.session.add(run);context.session.flush();import_pod_database(context,source,run)
    context.complete_step("sync_catalog",details={"library_sku":result.get("library_sku")})
