from __future__ import annotations

import argparse
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import platform
import socket
import time
import threading
from contextlib import contextmanager
from pathlib import Path

import httpx

from erp.core.config import get_settings
from erp.worker_tasks.image_prepare import execute_image_prepare
from erp.worker_tasks.oss_upload import OssUploadConfig, upload_prepared_image


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def execute_probe(payload: dict) -> dict:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {"ok": True, "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}


def _configure_logging(data_dir: Path) -> logging.Logger:
    logger = logging.getLogger("erp.remote_worker")
    if logger.handlers:
        return logger
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "remote-worker.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)
    return logger


@contextmanager
def _renew_lease(base: str, headers: dict, worker_id: str, job_id: str):
    stop = threading.Event()
    lost = threading.Event()
    def renew():
        with httpx.Client(base_url=base, headers=headers, timeout=10) as client:
            while not stop.wait(max(1, get_settings().worker_lease_seconds / 3)):
                try:
                    client.post(f"/workers/{worker_id}/jobs/{job_id}/heartbeat").raise_for_status()
                except Exception:
                    lost.set()
                    return
    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        yield lost
    finally:
        stop.set(); thread.join(timeout=12)


def _run_session(*, once: bool, logger: logging.Logger) -> None:
    settings = get_settings()
    base = settings.control_plane_url.rstrip("/") + settings.api_prefix
    headers = _headers(settings.worker_token)
    capabilities = ["worker_probe", "image_prepare"]
    metadata = {"hostname": socket.gethostname(), "platform": platform.platform()}
    with httpx.Client(base_url=base, headers=headers, timeout=30) as client:
        client.post("/workers/register", json={"id": settings.worker_id, "name": socket.gethostname(), "capabilities": capabilities, "metadata": metadata}).raise_for_status()
        logger.info("Worker %s 已连接控制中心", settings.worker_id)
        while True:
            client.post(f"/workers/{settings.worker_id}/heartbeat", json={"metadata": metadata}).raise_for_status()
            response = client.post(f"/workers/{settings.worker_id}/claim", json={"capabilities": capabilities})
            response.raise_for_status()
            job = response.json()
            if job:
                job_headers = {**headers, "X-Worker-Attempt": str(job["attempts"])}
                with _renew_lease(base, job_headers, settings.worker_id, job["id"]) as lost:
                    try:
                        if job["type"] == "worker_probe":
                            result = execute_probe(job.get("payload") or {})
                        elif job["type"] == "image_prepare":
                            client.post(f"/workers/{settings.worker_id}/jobs/{job['id']}/heartbeat", headers=job_headers).raise_for_status()
                            result = execute_image_prepare(
                                job["id"], job.get("payload") or {}, settings.data_dir,
                                allow_synthetic_dns=settings.worker_allow_synthetic_dns,
                            )
                            if bool((job.get("payload") or {}).get("upload_to_oss")):
                                local_path = settings.data_dir / result["local_relative_path"]
                                uploaded = upload_prepared_image(
                                    local_path,
                                    job_id=job["id"],
                                    sha256=result["sha256"],
                                    config=OssUploadConfig(
                                        region=settings.worker_oss_region,
                                        endpoint=settings.worker_oss_endpoint,
                                        bucket=settings.worker_oss_bucket,
                                        public_base_url=settings.worker_oss_public_base_url,
                                        prefix=settings.worker_oss_prefix,
                                        access_key_id=settings.worker_oss_access_key_id,
                                        access_key_secret=settings.worker_oss_access_key_secret,
                                    ),
                                )
                                result.update(uploaded)
                        else:
                            raise ValueError(f"不支持的远程任务类型：{job['type']}")
                        if lost.is_set():
                            raise RuntimeError("任务续租失败，停止回传旧结果")
                        client.post(f"/workers/{settings.worker_id}/jobs/{job['id']}/complete", headers=job_headers, json={"result": result}).raise_for_status()
                    except Exception as error:
                        client.post(f"/workers/{settings.worker_id}/jobs/{job['id']}/fail", headers=job_headers, json={"error_code": error.__class__.__name__, "error_message": str(error)}).raise_for_status()
            if once:
                return
            time.sleep(settings.worker_poll_seconds)


def run(*, once: bool = False) -> None:
    settings = get_settings()
    if settings.runtime_mode == "server":
        raise RuntimeError("服务器只负责数据与任务协调；任务执行端必须运行在使用者电脑上")
    if not settings.control_plane_url or not settings.worker_id or not settings.worker_token:
        raise RuntimeError("Worker 模式需要 ERP_CONTROL_PLANE_URL、ERP_WORKER_ID 和 ERP_WORKER_TOKEN")
    logger = _configure_logging(settings.data_dir)
    delay = 2.0
    while True:
        try:
            _run_session(once=once, logger=logger)
            return
        except KeyboardInterrupt:
            logger.info("Worker 已停止")
            return
        except Exception:
            if once:
                raise
            logger.exception("连接中断，%.0f 秒后重试", delay)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    run(once=parser.parse_args().once)
