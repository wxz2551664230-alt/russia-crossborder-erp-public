from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ["ERP_DATA_DIR"] = tempfile.mkdtemp(prefix="erp-tests-")
os.environ["ERP_DATABASE_URL"] = "sqlite:///" + (Path(os.environ["ERP_DATA_DIR"]) / "test.db").as_posix()
os.environ["ERP_SECRET_KEY"] = "NWFmOTczNzQ2NWIxOWJjODliYjZlODI1NjQ5Zjg4NzI="
os.environ["ERP_WORKER_TOKEN"] = "test-worker-token"
os.environ["ERP_ACCESS_TOKEN"] = "test-operator-token"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from erp.core.database import SessionLocal, engine
from erp.main import app
from erp.models import Base

Base.metadata.create_all(engine)

@pytest.fixture(autouse=True)
def clean_database():
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables): session.execute(delete(table))
        session.commit()
    yield

@pytest.fixture
def client():
    with TestClient(app, headers={"Authorization": "Bearer test-operator-token"}) as value: yield value
