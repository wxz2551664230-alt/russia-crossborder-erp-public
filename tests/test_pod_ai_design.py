from __future__ import annotations

from io import BytesIO

from PIL import Image

from erp.core.database import SessionLocal
from erp.domains.pod.ai_design.service import calculate_size, load_manifest
from erp.models import Job
from erp.services.jobs import acquire_job, execute_job
from erp.services import job_handlers as _job_handlers  # noqa: F401


def image_bytes(size=(32, 24)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (36, 102, 148)).save(output, "PNG")
    return output.getvalue()


def test_print_size_calculation_respects_ratio_and_limits():
    assert calculate_size("4:3", 3264) == "3264x2448"
    assert calculate_size("3:4", 3264) == "2448x3264"
    assert calculate_size("1:1", 3840) == "2880x2880"


def test_status_exposes_local_upscale_without_secrets(client):
    response = client.get("/api/v1/pod/ai-design/status")
    assert response.status_code == 200
    body = response.json()
    assert body["runtime"]["available"] is True
    assert body["runtime"]["token_usage"] is False
    assert body["connections"] == []


def test_standalone_upscale_creates_durable_job_and_manifest(client):
    response = client.post(
        "/api/v1/pod/ai-design/upscale",
        data={"scale": "2"},
        files=[("images", ("sample.png", image_bytes(), "image/png"))],
    )
    assert response.status_code == 202, response.text
    body = response.json()
    batch_id = body["batch"]["id"]
    assert body["batch"]["kind"] == "upscale"
    manifest = load_manifest(batch_id)
    assert manifest["inputs"][0]["width"] == 32
    assert "path" in manifest["inputs"][0]
    assert "path" not in body["batch"].get("inputs", [{}])[0]
    with SessionLocal() as session:
        job = session.get(Job, body["job"]["id"])
        assert job and job.type == "pod_ai_design_upscale"
        claimed = acquire_job(session, allowed_types={"pod_ai_design_upscale"})
        assert claimed and claimed.id == job.id
        execute_job(session, claimed)
    finished = load_manifest(batch_id)
    assert finished["status"] == "completed"
    assert finished["outputs"][0]["width"] == 64
    assert finished["outputs"][0]["height"] == 48
