from types import SimpleNamespace as Obj
import pytest
from erp.core.jsonutil import dumps
from erp.core.errors import ConflictError
from erp.services import catalog_personal_followup as server
from test_catalog_personal_publications import prepared


@pytest.mark.parametrize("phase,eligible", [("stock", True), ("promotion", True), ("promotion", False)])
def test_followup_receipt_is_bound_and_idempotent(prepared, monkeypatch, phase, eligible):
    session, job, publication, child, result = prepared
    child.external_product_id = "99"; child.status = phase + "_pending"
    child.stock_status = "processing" if phase == "stock" else "completed"
    child.promotion_status = "processing" if phase == "promotion" else "pending"
    child.assets_json = '{}'
    job.type = "catalog_ozon_followup"; job.external_ref = ""
    job.payload_json = dumps({"publication_id": publication.id, "store_id": child.store_id,
        "child_id": child.id, "offer_id": child.offer_id, "product_id": "99", "phase": phase, "params": {"stock": 100, "warehouse_id": 10}})
    monkeypatch.setattr(server, "get_publication", lambda *args: publication)
    import erp.api.routes.publications as routes
    completed = []
    monkeypatch.setattr(routes, "_complete_followup_step", lambda *args: completed.append(args))
    operation_hash = server.digest(__import__("json").loads(job.payload_json))
    with pytest.raises(ConflictError): server.accept(session, job, operation_hash, {"ok": True, "eligible": eligible})
    server.intent(session, job, operation_hash)
    with pytest.raises(ConflictError): server.accept(session, job, "c" * 64, {"ok": True, "eligible": eligible})
    receipt = server.accept(session, job, operation_hash, {"ok": True, "eligible": eligible})
    assert receipt["status"] == "completed"
    assert child.status == ("promotion_pending" if phase == "stock" or not eligible else "completed")
    assert server.accept(session, job, operation_hash, {"ok": True, "eligible": eligible}) == receipt
    assert len(completed) == int(eligible)
    with pytest.raises(ConflictError): server.accept(session, job, operation_hash, {"ok": True, "eligible": not eligible})
