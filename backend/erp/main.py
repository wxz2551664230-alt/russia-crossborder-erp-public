from __future__ import annotations

import mimetypes
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from erp.api.router import api_router
from erp.core.config import get_settings
from erp.core.database import engine
from erp.core.errors import AppError
from erp.core.access import principal, permitted, trusted_origin
from starlette.concurrency import run_in_threadpool
from erp.core.task_identity import task_identity


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    settings.ensure_directories()
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    yield


app = FastAPI(
    title="俄罗斯跨境 ERP API",
    version="0.1.0",
    description="统一店铺、接口、选品、货源、资产、上架和后台任务。",
    lifespan=lifespan,
)

OZON_GALLERY_BRIDGE_ORIGIN = "chrome-extension://jkfidffpjgcedfhbggfkokakhnaealgf"


@app.middleware("http")
async def local_security_and_trace(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
    request.state.trace_id = trace_id
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        local_origin = trusted_origin(request)
        gallery_bridge = (
            request.url.path == f"{get_settings().api_prefix}/ozon-gallery/capture"
            and origin == OZON_GALLERY_BRIDGE_ORIGIN
            and request.headers.get("X-Ozon-Gallery-Bridge") == "ozon-gallery-bridge-v1"
        )
        if not local_origin and not gallery_bridge:
            return JSONResponse(
                status_code=403,
                content={"code": "origin_rejected", "message": "请求来源不受信任", "details": None, "trace_id": trace_id},
            )
    prefix = get_settings().api_prefix
    event_id = None
    if request.url.path.startswith(prefix + "/") and request.url.path not in {prefix + "/auth/login", prefix + "/auth/config"} and not request.url.path.startswith(prefix + "/workers/"):
        try:
            user = await run_in_threadpool(principal, request)
        except AppError as error:
            return JSONResponse(status_code=error.status_code, content={"code": error.code, "message": str(error), "trace_id": trace_id})
        if not user:
            return JSONResponse(status_code=401, content={"code": "authentication_required", "message": "请登录 ERP", "trace_id": trace_id})
        request.state.user = user
        request.state.operator = user["id"]
        if get_settings().multiuser_enabled:
            from erp.services.accounts import begin_operation
            path = request.url.path[len(prefix):]
            if not permitted(user, request.method, path):
                await run_in_threadpool(begin_operation, user, request.method, path, 403)
                # Consume a small rejected request before responding. On Windows,
                # closing a keep-alive POST/PUT with an unread body can reset the
                # client's socket before it receives the intended 403 response.
                try:
                    if 0 < int(request.headers.get('content-length', '0')) <= 65536:
                        await request.body()
                except (ValueError, OSError):
                    pass
                return JSONResponse(status_code=403, content={"code": "permission_denied", "message": "当前账号无此操作权限", "trace_id": trace_id})
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not path.startswith("/auth/"):
                event_id = await run_in_threadpool(begin_operation, user, request.method, path)
    identity_token = task_identity.set(getattr(request.state, "user", None))
    try:
        response = await call_next(request)
    except Exception:
        if event_id:
            from erp.services.accounts import finish_operation
            await run_in_threadpool(finish_operation, event_id, 500)
        raise
    finally:
        task_identity.reset(identity_token)
    if event_id:
        from erp.services.accounts import finish_operation
        await run_in_threadpool(finish_operation, event_id, response.status_code)
    response.headers["X-Trace-Id"] = trace_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith(prefix + "/") or "text/html" in response.headers.get("Content-Type", ""):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(AppError)
async def app_error_handler(request: Request, error: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "code": error.code,
            "message": str(error),
            "details": error.details,
            "trace_id": getattr(request.state, "trace_id", ""),
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, error: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "code": "validation_error",
            "message": "提交的数据不完整或格式不正确",
            "details": [{k: v for k, v in item.items() if k in {"type", "loc", "msg"}} for item in error.errors()],
            "trace_id": getattr(request.state, "trace_id", ""),
        },
    )


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "application": "russia-crossborder-erp"}


@app.get("/readyz", tags=["system"])
def readiness():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1 FROM alembic_version LIMIT 1"))
            if get_settings().multiuser_enabled:
                connection.execute(text("SELECT 1 FROM erp_users LIMIT 1"))
        return {"status": "ready"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready"})


app.include_router(api_router, prefix=get_settings().api_prefix)


frontend_dist = get_settings().frontend_dist
assets_dir = frontend_dist / "assets"
if assets_dir.is_dir():
    app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend-assets")


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str):
    target = frontend_dist / path
    if path and target.is_file() and target.resolve().is_relative_to(frontend_dist.resolve()):
        return FileResponse(target, media_type=mimetypes.guess_type(target.name)[0])
    index = frontend_dist / "index.html"
    if index.is_file():
        return FileResponse(index, media_type="text/html")
    return JSONResponse(
        status_code=503,
        content={"code": "frontend_not_built", "message": "前端尚未构建，请先运行安装脚本", "details": None, "trace_id": ""},
    )


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("erp.main:app", host=settings.host, port=settings.port, reload=False)
