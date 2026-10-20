import importlib.util
import sqlite3
from pathlib import Path


def test_inventory_is_metadata_only_and_not_a_deletion_manifest(tmp_path):
    spec = importlib.util.spec_from_file_location("audit_cloud_storage", Path(__file__).resolve().parents[1] / "scripts/audit_cloud_storage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "source.png"
    source.write_bytes(b"original")
    db_path = tmp_path / "erp.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE assets(id TEXT, local_path TEXT,name TEXT,oss_bucket TEXT,oss_key TEXT,oss_url TEXT,sha256 TEXT,size_bytes INTEGER,mirror_status TEXT)")
        db.execute("INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?)", ("a", str(source), "source.png", "bucket", "key", "https://bucket.example/source.png?signature=private-token", "", 8, "mirrored"))
        db.execute("CREATE TABLE connection_secrets(secret TEXT)")
        db.execute("INSERT INTO connection_secrets VALUES('never-export-this')")
    original = db_path.read_bytes()
    report = module.build_report(tmp_path, include_assets=True)
    row = report["databases"]["erp.db"]["assets"][0]
    assert row["state"] == "local_and_remote_recorded"
    assert row["safe_to_delete"] is False and row["remote_verification"] == "not_checked"
    assert "private-token" not in str(report) and "never-export-this" not in str(report)
    assert db_path.read_bytes() == original and source.read_bytes() == b"original"
