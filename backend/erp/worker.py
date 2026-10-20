from __future__ import annotations

import logging
import os
import socket
import threading
import time
import argparse
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.models import Job, utc_now
from erp.services.job_handlers import *  # noqa: F403,F401
from erp.services.jobs import acquire_job, execute_job, job_types_for_queue


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("erp.worker")


def _keep_job_lease_alive(job_id: str, owner: str, stop: threading.Event) -> None:
    """Keep long external AI/API calls from being mistaken for a dead worker."""
    lease_seconds = get_settings().worker_lease_seconds
    interval = max(5.0, lease_seconds / 3)
    while not stop.wait(interval):
        now = utc_now()
        with SessionLocal() as heartbeat_session:
            heartbeat_session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "running", Job.lease_owner == owner)
                .values(
                    heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                )
            )
            heartbeat_session.commit()


def run_forever(queue_name: str = "all") -> None:
    settings = get_settings()
    if settings.runtime_mode != "local":
        raise RuntimeError("本地数据库 Worker 仅限 local 模式；worker 模式请运行 erp.remote_worker")
    allowed_types = job_types_for_queue(queue_name)
    owner = f"{socket.gethostname()}:{os.getpid()}:{queue_name}"
    logger.info("ERP worker started as %s", owner)
    while True:
        try:
            with SessionLocal() as session:
                job = acquire_job(session, owner, allowed_types=allowed_types)
                if job:
                    heartbeat_stop = threading.Event()
                    heartbeat = threading.Thread(
                        target=_keep_job_lease_alive,
                        args=(job.id, owner, heartbeat_stop),
                        name=f"job-heartbeat-{job.id[:8]}",
                        daemon=True,
                    )
                    heartbeat.start()
                    try:
                        execute_job(session, job)
                    finally:
                        heartbeat_stop.set()
                        heartbeat.join(timeout=2)
                    continue
        except SQLAlchemyError:
            # A transient SQLite lock must not terminate the sole local worker.
            # The abandoned job's lease will expire and be reconciled by the
            # next acquisition attempt; this session is disposed by `with`.
            logger.exception("Database error in worker loop; retrying after backoff")
            time.sleep(max(2.0, settings.worker_poll_seconds))
        time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", default="all")
    arguments = parser.parse_args()
    run_forever(arguments.queue)
