from __future__ import annotations

from datetime import timedelta
import pytest

from erp.core.database import SessionLocal
from erp.models import JobEvent, utc_now
from erp.services import job_handlers  # noqa: F401
from erp.services.jobs import HANDLERS, JOB_QUEUE_TYPES, acquire_job, cleanup_completed_jobs, enqueue_job, job_types_for_queue


def test_queue_worker_skips_unrelated_older_job() -> None:
    with SessionLocal() as session:
        older = enqueue_job(
            session,
            job_type="ozon_ai_prepare",
            payload={},
            config_snapshot="{}",
            idempotency_key="test-queue-ai",
        )
        newer = enqueue_job(
            session,
            job_type="pod_native_composition",
            payload={},
            config_snapshot="{}",
            idempotency_key="test-queue-pod",
        )

        claimed = acquire_job(session, "pod-test", allowed_types=job_types_for_queue("pod"))

        assert claimed is not None
        assert claimed.id == newer.id
        assert claimed.id != older.id
        assert claimed.lease_owner == "pod-test"


def test_job_queue_groups_are_disjoint() -> None:
    queue_names = JOB_QUEUE_TYPES
    groups = [job_types_for_queue(name) for name in queue_names]
    for index, group in enumerate(groups):
        for other in groups[index + 1:]:
            assert group is not None and other is not None
            assert group.isdisjoint(other)


def test_every_registered_local_job_has_an_isolated_queue() -> None:
    queue_names = JOB_QUEUE_TYPES
    assigned = set().union(*(job_types_for_queue(name) or set() for name in queue_names))
    assert set(HANDLERS).issubset(assigned)


def test_completed_job_cleanup_preserves_recent_history() -> None:
    with SessionLocal() as session:
        old = enqueue_job(session, job_type="asset_index", payload={}, config_snapshot="{}", idempotency_key="cleanup-old")
        recent = enqueue_job(session, job_type="asset_index", payload={}, config_snapshot="{}", idempotency_key="cleanup-recent")
        old.status = recent.status = "completed"
        old.finished_at = utc_now() - timedelta(days=40)
        recent.finished_at = utc_now() - timedelta(days=2)
        session.add(JobEvent(job_id=old.id, message="old event"))
        session.commit()
        assert cleanup_completed_jobs(session, retention_days=30, keep_latest=0) == 1
        assert session.get(type(old), old.id) is None
        assert session.get(type(recent), recent.id) is not None


def _enqueue(session, key):
    return enqueue_job(session, job_type="asset_index", payload={}, config_snapshot="{}", idempotency_key=key)


def test_retry_backoff_releases_worker_and_is_shared_by_consumers():
    with SessionLocal() as session:
        failed = _enqueue(session, "failed")
        failed.status = "retrying"
        failed.attempts = 1
        failed.heartbeat_at = utc_now()
        session.commit()
        other = _enqueue(session, "other")
        assert acquire_job(session, "first").id == other.id
        assert acquire_job(session, "second") is None
        failed.heartbeat_at = utc_now() - timedelta(seconds=3)
        session.commit()
        assert acquire_job(session, "second").id == failed.id


def test_retry_goes_behind_already_waiting_peer():
    with SessionLocal() as session:
        retry = _enqueue(session, "retry")
        peer = _enqueue(session, "peer")
        peer.created_at = utc_now() - timedelta(minutes=1)
        retry.status = "retrying"
        retry.attempts = 1
        retry.created_at = utc_now() - timedelta(minutes=2)
        retry.heartbeat_at = utc_now() - timedelta(seconds=3)
        session.commit()
        assert acquire_job(session, "worker").id == peer.id


@pytest.mark.parametrize("flag,expected", [("pause_requested", "paused"), ("cancel_requested", "cancelled"), (None, "retrying")])
def test_expired_lease_preserves_stop_request_and_frees_queue(flag, expected):
    with SessionLocal() as session:
        stale = _enqueue(session, "stale")
        stale.status = "running"
        stale.attempts = 1
        stale.lease_owner = "dead"
        stale.lease_expires_at = utc_now() - timedelta(seconds=1)
        if flag:
            setattr(stale, flag, True)
        session.commit()
        peer = _enqueue(session, "peer")
        assert acquire_job(session, "live").id == peer.id
        session.refresh(stale)
        assert stale.status == expected
        assert stale.lease_owner == ""
        assert stale.lease_expires_at is None


def test_active_lease_and_waiting_user_jobs_are_not_claimed():
    with SessionLocal() as session:
        for status in ("running", "paused", "action_required", "cancelled"):
            job = _enqueue(session, status)
            job.status = status
            job.lease_owner = "live"
            job.lease_expires_at = utc_now() + timedelta(minutes=1)
        session.commit()
        assert acquire_job(session, "other") is None


def test_long_jobs_have_separate_consumers():
    assert "pod_wb_api_publish" in job_types_for_queue("wb")
    assert "pod_wb_stock" in job_types_for_queue("wb_operations")
    assert "pod_native_composition" in job_types_for_queue("pod")
    assert "pod_supplier_gallery_sync" in job_types_for_queue("pod_sync")
    assert "sourcing_search" in job_types_for_queue("sourcing")
    assert "self_listing_pipeline" in job_types_for_queue("self_listing")


def test_all_launchers_cover_every_queue():
    from pathlib import Path
    import re
    from erp.services.jobs import LOCAL_WORKER_QUEUES
    script = (Path(__file__).resolve().parents[1] / "client/start-queue-workers.ps1").read_text(encoding="utf-8")
    from collections import Counter
    assert Counter(re.findall(r'Queue = "([a-z_]+)"', script)) + Counter({"ozon": 1}) == Counter(LOCAL_WORKER_QUEUES)
    assert set(LOCAL_WORKER_QUEUES) == set(JOB_QUEUE_TYPES)
