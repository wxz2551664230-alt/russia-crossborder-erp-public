from pathlib import Path
from types import SimpleNamespace
import pytest
from cryptography.fernet import Fernet


@pytest.mark.parametrize("phase", ["stock", "promotion"])
@pytest.mark.parametrize("mode", ["success", "lost_receipt", "unknown", "ineligible"])
def test_durable_followup_never_repeats_write(tmp_path, monkeypatch, phase, mode):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "client" / "desktop"))
    from catalog_ozon_followup_worker import execute_operation, FollowupOutcomeUnknown
    cipher = Fernet(Fernet.generate_key())
    def protect(data, decrypt=False): return cipher.decrypt(data) if decrypt else cipher.encrypt(data)
    snapshot = {"schema": 1, "job_id": "a" * 32, "operation_hash": "b" * 64, "phase": phase,
                "offer_id": "SKU", "product_id": "99", "params": {"warehouse_id": 1, "stock": 100, "action_id": 2, "action_price": 50}}
    writes = []; receipts = []
    def write(*args):
        writes.append(args)
        if mode == "unknown": raise OSError("lost platform response")
    adapter = SimpleNamespace(update_stock=write, activate_promotion=write,
                              find_promotion_candidate=lambda *args: mode != "ineligible")
    def post(path, body):
        if path.endswith("result"):
            receipts.append(body)
            if mode == "lost_receipt" and len(receipts) == 1: raise OSError("lost cloud response")
        return {"status": "completed"}
    if mode == "unknown":
        with pytest.raises(FollowupOutcomeUnknown): execute_operation(snapshot, tmp_path, protect, post, lambda: None, adapter)
        with pytest.raises(FollowupOutcomeUnknown): execute_operation(snapshot, tmp_path, protect, post, lambda: None, adapter)
    else:
        if mode == "lost_receipt":
            with pytest.raises(OSError): execute_operation(snapshot, tmp_path, protect, post, lambda: None, adapter)
        else: execute_operation(snapshot, tmp_path, protect, post, lambda: None, adapter)
        assert execute_operation(snapshot, tmp_path, protect, post, lambda: None, adapter)["status"] == "completed"
        assert receipts[-1]["result"]["eligible"] == (phase == "stock" or mode != "ineligible")
    assert len(writes) == (0 if phase == "promotion" and mode == "ineligible" else 1)
