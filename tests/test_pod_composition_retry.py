"""Exercise the actual gateway, ownership and desktop retry contract."""
import uuid

import pytest
from sqlalchemy import select

from deploy.pilot_gateway import allowed
from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps, loads
from erp.domains.pod.models import PodComposition, PodCompositionSide
from erp.models import Asset, CatalogProduct, Job, JobStep
from test_personal_job_controls import team


BASE = "/api/v1/pod/compositions/retry-failed"


def seed(owner, worker, *, market="CN", supplier="s2b", status="failed", cleared=False,
         unknown=False, error_code="", missing_source=False, mismatch=False, published=False):
    with SessionLocal() as db:
        job = Job(type="pod_us_composition" if market == "US" else "pod_native_composition",
                  created_by=owner, target_worker_id=worker, status=status,
                  idempotency_key="retry-test:" + uuid.uuid4().hex, attempts=1, max_attempts=1,
                  error_code=error_code, error_message="previous failed attempt", progress=40)
        db.add(job); db.flush()
        item = PodComposition(job_id=job.id, supplier=supplier, status="cleared" if cleared else status,
                              outcome_unknown=unknown, product_name="retry fixture",
                              remote_product_id="old-remote", product_code="old-code",
                              rendering_groups_json=dumps([{"rendering_urls": ["https://example.invalid/effect.png"]}]))
        if published:
            product = CatalogProduct(source_type="pod", name="already imported", sku="retry-" + uuid.uuid4().hex)
            db.add(product); db.flush(); item.catalog_product_id = product.id
        db.add(item); db.flush()
        asset = Asset(name="design.png", local_path="", oss_url="https://example.invalid/design.png",
                      sha256="a" * 64, size_bytes=12)
        db.add(asset); db.flush()
        side = PodCompositionSide(composition_id=item.id, supplier=supplier, view_id=1,
                                  asset_id=None if missing_source else asset.id, gallery_code="old-gallery")
        db.add(side); db.flush()
        item.raw_json = dumps({"market": market,
                               "sides": [{"side_id": side.id, "asset_id": side.asset_id,
                                          "source_path": "Z:/not-on-the-server/source.png",
                                          "gallery_code": "old-gallery", "source_prepared": True}]})
        job.payload_json = dumps({"composition_id": "wrong-id" if mismatch else item.id})
        db.add_all([JobStep(job_id=job.id, key="prepare", name="prepare", position=1,
                            status="completed", progress=100, details_json=dumps({"checkpoint": "keep"})),
                    JobStep(job_id=job.id, key="download", name="download", position=2,
                            status="failed", error_message="network failed", progress=20)])
        db.commit()
        return job.id, item.id, side.id, asset.id, item.raw_json, item.rendering_groups_json


def actor(client):
    return client.get("/api/v1/auth/session").json()["user"]["id"]


def test_gateway_opens_only_exact_retry_routes():
    assert allowed("GET", BASE + "/preview")
    assert allowed("POST", BASE)
    for method, path in [("DELETE", BASE), ("GET", BASE), ("POST", BASE + "/preview"),
                         ("POST", BASE + "/all"), ("POST", "/api/v1/pod/legacy-api/jobs/clear")]:
        assert not allowed(method, path)


@pytest.mark.parametrize("market,supplier", [("CN", "s2b"), ("CN", "hicustom"), ("US", "s2b")])
def test_shared_retry_preserves_checkpoint_and_original_pc_without_server_supplier_calls(team, monkeypatch, market, supplier):
    clients, worker = team; owner = clients["owner"]
    row = seed(actor(owner), worker, market=market, supplier=supplier, cleared=True)
    from erp.domains.pod.integrations import registry
    from erp.domains.pod import composition, desktop_composition
    def forbidden(*args, **kwargs):
        pytest.fail("Retry coordination must not execute supplier operations or resolve desktop paths on server")
    monkeypatch.setattr(registry, "supplier_client", forbidden)
    monkeypatch.setattr(composition, "resolve_asset_path", forbidden)
    monkeypatch.setattr(desktop_composition, "source_path", forbidden)
    preview = owner.get(BASE + "/preview")
    assert preview.status_code == 200, preview.text
    assert preview.json()["eligible"] == 1 and preview.json()["hidden_history"] == 1
    response = owner.post(BASE)
    assert response.status_code == 202, response.text
    assert response.json()["queued"] == 1 and response.json()["skipped"] == 0
    assert owner.post(BASE).json()["queued"] == 0
    with SessionLocal() as db:
        job, item, side = db.get(Job, row[0]), db.get(PodComposition, row[1]), db.get(PodCompositionSide, row[2])
        assert job.status == item.status == "queued"
        assert job.target_worker_id == worker and job.created_by == actor(owner)
        assert job.attempts == 1 and job.max_attempts == 2 and job.error_message == ""
        assert (item.product_code, item.remote_product_id, side.gallery_code) == ("old-code", "old-remote", "old-gallery")
        assert (item.raw_json, item.rendering_groups_json) == row[4:]
        assert side.asset_id == row[3]
        steps = {s.key: s for s in db.scalars(select(JobStep).where(JobStep.job_id == job.id))}
        assert steps["prepare"].status == "completed" and loads(steps["prepare"].details_json, {}) == {"checkpoint": "keep"}
        assert steps["download"].status == "pending"


def test_retry_isolated_from_other_accounts_computers_and_nonfailed_tasks(team):
    clients, worker = team; owner = clients["owner"]; owner_id = actor(owner)
    mine = seed(owner_id, worker)
    second = "pc-" + "b" * 32
    assert owner.post("/api/v1/execution-devices/local/register", json={"id": second, "name": "second PC"}).status_code == 200
    assert owner.post("/api/v1/execution-devices/select", json={"worker_id": worker}).status_code == 200
    others = [seed(owner_id, second), seed(actor(clients["other"]), worker)]
    others += [seed(owner_id, worker, status=s) for s in ("running", "queued", "paused", "completed", "cancelled")]
    assert owner.get(BASE + "/preview").json()["failed"] == 1
    assert clients["admin"].get(BASE + "/preview").status_code == 409  # no original computer selected
    for name in ("other", "viewer"):
        assert clients[name].post(BASE).status_code in (403, 409)
    assert owner.post(BASE).json()["job_ids"] == [mine[0]]
    with SessionLocal() as db:
        assert db.get(Job, others[0][0]).status == "failed"
        assert db.get(Job, others[1][0]).status == "failed"
        for row, status in zip(others[2:], ("running", "queued", "paused", "completed", "cancelled")):
            assert db.get(Job, row[0]).status == status


def test_blocked_tasks_report_reasons_and_do_not_prevent_safe_retries(team):
    clients, worker = team; owner = clients["owner"]; uid = actor(owner)
    good = seed(uid, worker)
    blocked = [seed(uid, worker, unknown=True), seed(uid, worker, error_code="pod_write_outcome_unknown"),
               seed(uid, worker, missing_source=True), seed(uid, worker, mismatch=True),
               seed(uid, worker, published=True)]
    preview = owner.get(BASE + "/preview").json()
    assert preview["failed"] == 6 and preview["eligible"] == 1 and preview["blocked"] == 5
    result = owner.post(BASE).json()
    assert result["queued"] == 1 and result["skipped"] == 5 and result["job_ids"] == [good[0]]
    assert len(result["errors"]) == 5 and all(e["message"] for e in result["errors"])
    with SessionLocal() as db:
        for row in blocked:
            job, item = db.get(Job, row[0]), db.get(PodComposition, row[1])
            assert job.status == item.status == "failed" and job.max_attempts == 1
            assert job.error_message == "previous failed attempt"


def test_batch_not_silently_truncated_at_old_200_limit(team):
    clients, worker = team; owner = clients["owner"]; uid = actor(owner)
    for _ in range(201):
        seed(uid, worker)
    assert owner.get(BASE + "/preview").json()["eligible"] == 201
    result = owner.post(BASE)
    assert result.status_code == 202 and result.json()["queued"] == 201
    assert owner.post(BASE).json()["queued"] == 0


def test_cross_origin_and_unauthenticated_retries_cannot_queue(team):
    from fastapi.testclient import TestClient
    from deploy.pilot_gateway import app
    clients, worker = team; owner = clients["owner"]
    row = seed(actor(owner), worker)
    assert owner.post(BASE, headers={"Origin": "https://untrusted.invalid"}).status_code == 403
    with TestClient(app) as anonymous:
        assert anonymous.post(BASE).status_code == 401
    with SessionLocal() as db:
        assert db.get(Job, row[0]).status == "failed"


def test_manual_batch_retry_grants_one_attempt_and_old_attempt_is_fenced(team):
    clients, worker = team; owner = clients["owner"]
    assert owner.post("/api/v1/execution-devices/local/register", json={"id": worker, "name": "PC", "composition_version": 2}).status_code == 200
    row = seed(actor(owner), worker, market="US")
    assert owner.post(BASE).json()["queued"] == 1
    base = f"/api/v1/execution-devices/local/{worker}"
    claim = owner.post(base + "/claim").json()
    assert claim["id"] == row[0] and claim["attempts"] == 2
    assert owner.post(base + f"/jobs/{row[0]}/heartbeat", headers={"X-Worker-Attempt": "1"}).status_code == 409
    failed = owner.post(base + f"/jobs/{row[0]}/fail", headers={"X-Worker-Attempt": "2"},
                        json={"error_code": "local_execution_failed", "error_message": "read failed", "retryable": True})
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "failed"
    assert owner.post(base + "/claim").json() is None
