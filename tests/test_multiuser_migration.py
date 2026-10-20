"""Migration rehearsal uses only disposable databases; never the real erp.db."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_account_migration_preserves_business_and_backup(tmp_path):
    path = tmp_path / "migration.db"
    env = {**os.environ, "ERP_DATA_DIR": str(tmp_path), "ERP_DATABASE_URL": "sqlite:///" + path.as_posix(), "ERP_MULTIUSER_ENABLED": "false"}
    def run(*args):
        result = subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
    run("-m", "alembic", "upgrade", "e9f0a1b2c3d4")
    run("-c", "from erp.core.database import SessionLocal; from erp.models import Store; db=SessionLocal(); db.add(Store(id='preserved-store', platform='ozon', name='Preserved business')); db.commit(); db.close()")
    with sqlite3.connect(path) as original:
        original.execute("INSERT INTO jobs (id,type,status,priority,idempotency_key,payload_json,config_snapshot_json,progress,cancel_requested,pause_requested,lease_owner,attempts,max_attempts,external_ref,error_code,error_message,created_at,updated_at) VALUES ('old-job','worker_probe','queued',100,'old-job','{}','{}',0,0,0,'',0,3,'','','',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
    with sqlite3.connect(path) as original, sqlite3.connect(tmp_path / "before-upgrade.db") as backup:
        original.backup(backup)
    run("-m", "alembic", "upgrade", "head")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "b2c3d4e5f6a7"
        assert db.execute("SELECT name FROM stores WHERE id='preserved-store'").fetchone()[0] == "Preserved business"
        assert db.execute("SELECT target_worker_id, created_by, status FROM jobs WHERE id='old-job'").fetchone() == ("", "", "queued")
        for table in ["erp_users", "erp_user_sessions", "erp_auth_throttles", "erp_auth_control", "erp_user_audit"]:
            assert db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0
        for table in ['amazon_listing_batches','amazon_listing_items']:
            assert db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with sqlite3.connect(tmp_path / "before-upgrade.db") as backup:
        assert backup.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "e9f0a1b2c3d4"
        assert backup.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 1


def test_new_migration_generates_postgresql_sql_without_connecting():
    import importlib.util
    import io
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec = importlib.util.spec_from_file_location("account_migration", ROOT / "backend/alembic/versions/f0a1b2c3d4e5_add_shared_user_accounts.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    output = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        migration.upgrade()
    assert "CREATE TABLE erp_users" in output.getvalue()
    assert "TIMESTAMP WITH TIME ZONE" in output.getvalue()
    assert "DROP TABLE" not in output.getvalue()
