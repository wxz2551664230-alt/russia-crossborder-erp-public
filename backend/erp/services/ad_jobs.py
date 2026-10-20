"""Advertising data preparation background jobs.

Live advertising actions stay on the reviewed, per-batch approval route.
"""

import asyncio
import io
from pathlib import Path

from fastapi import UploadFile

from erp.api.routes import ad_optimization
from erp.core.config import get_settings
from erp.core.errors import AppError
from erp.services.jobs import JobContext, job_handler


def _run(context: JobContext, operation: str) -> None:
    if context.start_step(operation).status == "completed":
        return
    store_id = str(context.payload["store_id"])
    try:
        if operation == "sync":
            result = ad_optimization.sync_from_ozon(
                store_id=store_id, days=int(context.payload.get("days") or 62), checkpoint_job_id=context.job.id, session=context.session,
            )
        elif operation == "analyze":
            result = ad_optimization.analyze(store_id=store_id, session=context.session)
        elif operation == "backtest":
            result = ad_optimization.backtest(store_id=store_id, session=context.session)
        else:
            result = ad_optimization.generate_reports(store_id=store_id, session=context.session)
    except Exception:
        context.session.rollback()
        raise
    context.complete_step(operation, details=result)


@job_handler("ad_optimization_sync")
def sync_job(context: JobContext) -> None:
    _run(context, "sync")


@job_handler("ad_optimization_analyze")
def analyze_job(context: JobContext) -> None:
    _run(context, "analyze")


@job_handler("ad_optimization_backtest")
def backtest_job(context: JobContext) -> None:
    _run(context, "backtest")


@job_handler("ad_optimization_reports")
def reports_job(context: JobContext) -> None:
    _run(context, "reports")


@job_handler("ad_optimization_import")
def import_job(context: JobContext) -> None:
    if context.start_step("import").status == "completed":
        return
    upload_id = str(context.payload.get("upload_id") or "")
    if len(upload_id) != 32 or any(character not in "0123456789abcdef" for character in upload_id):
        raise AppError("广告导入文件标识无效", code="ad_import_invalid_upload")
    directory = (get_settings().data_dir / "ad_imports").resolve()
    path = directory / f"{upload_id}.csv"
    if not path.is_file():
        raise AppError("广告导入文件不存在，请重新上传", code="ad_import_file_missing")
    try:
        with path.open("rb") as source:
            result = asyncio.run(ad_optimization.import_csv(
                store_id=str(context.payload["store_id"]),
                file=UploadFile(file=io.BytesIO(source.read()), filename="advertising.csv"),
                session=context.session,
            ))
        context.complete_step("import", details=result)
    except Exception:
        context.session.rollback()
        raise
    finally:
        path.unlink(missing_ok=True)
