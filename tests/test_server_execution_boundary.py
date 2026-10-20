import pytest

from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.core.errors import AppError
from erp.services.jobs import enqueue_job, execute_job, HANDLERS


def test_server_cannot_execute_registered_business_handler(monkeypatch):
    monkeypatch.setattr(get_settings(), "runtime_mode", "server")
    called = []
    monkeypatch.setitem(HANDLERS, "worker_probe", lambda context: called.append(context))
    with SessionLocal() as db:
        job = enqueue_job(db, job_type="worker_probe", payload={}, config_snapshot="{}", idempotency_key="server-boundary")
        with pytest.raises(AppError) as error:
            execute_job(db, job)
        assert error.value.code == "server_execution_forbidden"
        db.refresh(job)
        assert job.status == "queued" and job.attempts == 0
        assert called == []


def test_server_cannot_start_local_or_network_executor(monkeypatch):
    from erp import remote_worker, worker
    monkeypatch.setattr(get_settings(), "runtime_mode", "server")
    with pytest.raises(RuntimeError):
        worker.run_forever()
    with pytest.raises(RuntimeError):
        remote_worker.run(once=True)


def test_server_cannot_run_ad_review_or_external_write(monkeypatch):
    from erp.services import ad_execution
    from erp.schemas import AdBatchExecutionRequest

    monkeypatch.setattr(get_settings(), "runtime_mode", "server")
    request = AdBatchExecutionRequest(action="SCALE_CANDIDATES", state_ids=["a" * 32])
    with SessionLocal() as db:
        for operation in (
            lambda: ad_execution.preview(db, "b" * 32, request, "owner"),
            lambda: ad_execution.execute(db, "b" * 32, request, "owner"),
            lambda: ad_execution.reconcile(db, "c" * 32, "b" * 32),
        ):
            with pytest.raises(AppError) as error:
                operation()
            assert error.value.status_code == 409
