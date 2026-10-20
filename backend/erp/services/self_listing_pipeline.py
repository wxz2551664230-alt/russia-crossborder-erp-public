from __future__ import annotations

from erp.core.ai_concurrency import limits as ai_limits

from pathlib import Path
import threading
from urllib.parse import urlsplit

import pandas as pd

from erp.core.errors import AppError
from erp.core.paths import require_writable_path
from erp.services.assets import register_local_asset
from erp.services.jobs import JobContext, job_handler
from erp.services.config_resolution import resolve_job_snapshot
from erp.models import JobArtifact
from erp.core.database import SessionLocal
from erp.models import Job
from erp.domains.self_listing.vendor_sources import copywriting, image_generation, oss_uploader, watermark


def _endpoint(value: str, default_path: str) -> tuple[str, str]:
    from erp.core.endpoints import ai_endpoint
    parsed = urlsplit(ai_endpoint(value, default_path))
    if not parsed.scheme or not parsed.netloc:
        raise AppError("AI 接口地址无效", code="self_listing_ai_endpoint_invalid")
    return f"{parsed.scheme}://{parsed.netloc}", parsed.path or default_path


def _output_asset(context: JobContext, path: Path, role: str) -> None:
    asset = register_local_asset(
        context.session, path, kind="spreadsheet", source="self_listing_pipeline",
        read_only=True, calculate_hash=True,
        metadata={"job_id": context.job.id, "role": role},
        legacy_source="self_listing_pipeline", legacy_id=f"{context.job.id}:{role}",
    )
    context.session.add(JobArtifact(job_id=context.job.id, asset_id=asset.id, role=role))
    context.session.commit()


def _watch_job_cancel(job_id: str, stop_event: threading.Event, finished: threading.Event) -> None:
    """Bridge the persisted job cancel flag into the image generator's stop event."""
    while not finished.wait(1):
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if not job or job.cancel_requested or job.status in {"cancelled", "failed"}:
                stop_event.set()
                return


def _step_needs_run(context: JobContext, key: str) -> bool:
    """Retries must never repeat a step that the original run already completed."""
    return not any(step.key == key and step.status == "completed" for step in context.job.steps)


@job_handler("self_listing_pipeline")
def self_listing_pipeline_job(context: JobContext) -> None:
    from erp.core.config import get_settings
    if get_settings().runtime_mode == 'server':
        raise AppError('自主上架目录流水线必须在发起人的电脑执行', code='personal_execution_required')
    run_self_listing_pipeline(context)


def run_self_listing_pipeline(context, *, snapshot: dict | None = None,
                              output_asset=None, watch_cancel=None) -> None:
    """Keep the original steps while allowing a personal-PC result sink."""
    payload = context.payload
    snapshot = snapshot if snapshot is not None else resolve_job_snapshot(context.job.config_snapshot_json)
    output_asset = output_asset or _output_asset
    watch_cancel = watch_cancel or _watch_job_cancel
    product_root = Path(payload["product_root"]).expanduser().resolve()
    if not product_root.is_dir():
        raise AppError(f"产品图片目录不存在：{product_root}", code="self_listing_root_missing")
    output_dir = Path(payload.get("output_dir") or product_root.parent / f"{product_root.name}-ERP输出").expanduser().resolve()
    watermark_root = Path(payload.get("watermark_root") or product_root.parent / f"{product_root.name}-水印图").expanduser().resolve()
    require_writable_path(output_dir); require_writable_path(watermark_root)
    if "generate_images" in (payload.get("selected_steps") or ["generate_images"]):
        require_writable_path(product_root)
    output_dir.mkdir(parents=True, exist_ok=True); watermark_root.mkdir(parents=True, exist_ok=True)
    source_subfolder = str(payload.get("source_subfolder") or "_generated")
    selected_steps = set(payload.get("selected_steps") or ["generate_images", "generate_copywriting", "watermark", "upload_original", "upload_watermarked"])
    connections = snapshot.get("connections") or {}
    secrets = snapshot["secrets"]

    chat = connections.get("ai_chat")
    chat_host, chat_path = _endpoint(chat["base_url"], "/v1/chat/completions") if chat else ("", "")
    if "generate_images" in selected_steps and _step_needs_run(context, "generate_images"):
        context.start_step("generate_images")
        image = connections["ai_image"]
        image_host, image_path = _endpoint(image["base_url"], "/v1/images/edits")
        image_cfg = image_generation.AppConfig(
        root_dir=str(product_root), product_hint=str(payload.get("product_hint") or ""),
        api_key=str(secrets["ai_chat"].get("api_key") or ""),
        analysis_api_key=str(secrets["ai_chat"].get("api_key") or ""), analysis_api_host=chat_host, analysis_chat_path=chat_path,
        image_api_key=str(secrets["ai_image"].get("api_key") or ""), image_api_host=image_host, image_edit_path=image_path,
        analysis_model=str(chat["config"].get("default_model") or "gpt-5.6-terra"), image_model=str(image["config"].get("default_model") or "gpt-image-2"),
        out_subfolder=source_subfolder, image_plan_count=int(payload.get("image_plan_count") or 8),
        generate_dimension_image=False,
        max_concurrent_requests=max(ai_limits().values()), folder_concurrency=ai_limits()["image"],
        repair_only_missing=bool(payload.get("repair_only_missing", False)),
        request_retries=2, failed_task_reruns=0,
        )
        image_stop = threading.Event()
        watcher_finished = threading.Event()
        watcher = threading.Thread(
            target=watch_cancel,
            args=(context.job.id, image_stop, watcher_finished),
            name=f"self-listing-cancel-{context.job.id[:8]}",
            daemon=True,
        )
        watcher.start()
        try:
            image_generation.run_generation(image_cfg, context.event, stop_event=image_stop)
        finally:
            watcher_finished.set()
            watcher.join(timeout=2)
        context.refresh()
        context.complete_step("generate_images")

    if "generate_copywriting" in selected_steps and _step_needs_run(context, "generate_copywriting"):
        context.start_step("generate_copywriting")
        copy_path = output_dir / "ozon_copywriting.xlsx"
        copy_cfg = dict(copywriting.DEFAULT_CONFIG)
        copy_cfg.update({
        "concurrency_limit": ai_limits()["text"],
        "api_host": chat_host, "api_path": chat_path,
        "api_token": str(secrets["ai_chat"].get("api_key") or ""),
        "model": str(chat["config"].get("default_model") or "gpt-5.6-terra"),
        "category_hint": str(payload.get("category_hint") or ""), "page_keywords": str(payload.get("page_keywords") or ""),
        "output_xlsx": str(copy_path), "source_subfolder": source_subfolder,
        })
        returned_copy_path = copywriting.run_copywriting(str(product_root), copy_cfg)
        actual_copy_path = Path(returned_copy_path or copy_path)
        output_asset(context, actual_copy_path, "copywriting_xlsx")
        try:
            copy_frame = pd.read_excel(actual_copy_path, dtype=str).fillna("")
            failed_count = int(copy_frame.get("error", pd.Series(dtype=str)).astype(str).str.strip().ne("").sum())
        except (ValueError, OSError):
            # Keep compatibility with externally supplied generators. The bundled
            # generator always writes a readable workbook with an error column.
            failed_count = 0
        if failed_count:
            raise AppError(
                f"文案生成有 {failed_count} 个产品失败；成功结果已保存，重跑任务将只补失败项",
                code="self_listing_copywriting_partial_failure",
            )
        context.complete_step("generate_copywriting", details={"output": str(actual_copy_path)})

    if "watermark" in selected_steps and _step_needs_run(context, "watermark"):
        context.start_step("watermark")
        watermark.process_watermark_directory(
        str(product_root), watermark_filename=str(payload.get("watermark_filename") or "水印.png"),
        position=str(payload.get("watermark_position") or "bottom-right"), scale=float(payload.get("watermark_scale") or 0.2),
        opacity=float(payload.get("watermark_opacity") or 0.7), output_mode="overwrite",
        target_subfolder=source_subfolder, output_root=str(watermark_root), skip_suffix="_watermarked",
        )
        context.complete_step("watermark", details={"output": str(watermark_root)})

    oss = connections.get("oss")
    base_oss = dict(oss["config"]) if oss else {}; oss_secret = secrets.get("oss") or {}
    def upload(root: Path, prefix: str, filename: str, role: str) -> None:
        cfg = oss_uploader.OssUploadConfig(
            region=str(base_oss.get("region") or ""), endpoint=str(oss.get("base_url") or base_oss.get("endpoint") or ""),
            bucket=str(base_oss.get("bucket") or ""), public_base_url=str(base_oss.get("public_base_url") or ""),
            access_key_id=str(oss_secret.get("access_key_id") or ""), access_key_secret=str(oss_secret.get("access_key_secret") or ""),
            object_prefix=prefix, source_subfolder=source_subfolder, only_suffix="",
            max_workers=int(base_oss.get("max_workers") or 4), upload_retries=int(base_oss.get("upload_retries") or 3),
        )
        target = output_dir / filename
        oss_uploader.batch_upload_images(str(root), cfg=cfg, output_path=target, dry_run=bool(payload.get("oss_dry_run")), log_func=context.event)
        output_asset(context, target, role)

    if "upload_original" in selected_steps and _step_needs_run(context, "upload_original"):
        context.start_step("upload_original")
        upload(product_root, str(payload.get("original_oss_prefix") or "OZON/original"), "oss_original.xlsx", "oss_original_xlsx")
        context.complete_step("upload_original")
    if "upload_watermarked" in selected_steps and _step_needs_run(context, "upload_watermarked"):
        context.start_step("upload_watermarked")
        upload(watermark_root, str(payload.get("watermarked_oss_prefix") or "OZON/watermarked"), "oss_watermarked.xlsx", "oss_watermarked_xlsx")
        context.complete_step("upload_watermarked", details={"output_dir": str(output_dir)})
