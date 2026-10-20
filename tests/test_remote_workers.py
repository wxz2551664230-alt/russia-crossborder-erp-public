from __future__ import annotations


HEADERS = {"Authorization": "Bearer test-worker-token", "X-Worker-Attempt": "1"}


def test_remote_worker_probe_round_trip(client) -> None:
    denied = client.post("/api/v1/workers/register", json={"id": "worker-1"})
    assert denied.status_code == 401

    registered = client.post(
        "/api/v1/workers/register",
        headers=HEADERS,
        json={"id": "worker-1", "name": "测试电脑", "capabilities": ["worker_probe"]},
    )
    assert registered.status_code == 200

    created = client.post(
        "/api/v1/jobs",
        json={"type": "worker_probe", "payload": {"message": "hello"}, "idempotency_key": "probe-1"},
    ).json()
    claimed = client.post(
        "/api/v1/workers/worker-1/claim",
        headers=HEADERS,
        json={"capabilities": ["worker_probe"]},
    ).json()
    assert claimed["id"] == created["id"]
    assert claimed["status"] == "running"

    completed = client.post(
        f"/api/v1/workers/worker-1/jobs/{created['id']}/complete",
        headers=HEADERS,
        json={"result": {"ok": True}},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["steps"][0]["status"] == "completed"
    assert completed.json()["steps"][0]["details"]["ok"] is True

    duplicate_completion = client.post(
        f"/api/v1/workers/worker-1/jobs/{created['id']}/complete",
        headers=HEADERS,
        json={"result": {"ok": True}},
    )
    assert duplicate_completion.status_code == 409


def test_worker_cannot_complete_another_workers_lease(client) -> None:
    for worker_id in ("worker-a", "worker-b"):
        client.post("/api/v1/workers/register", headers=HEADERS, json={"id": worker_id})
    job = client.post("/api/v1/jobs", json={"type": "worker_probe", "idempotency_key": "probe-owner"}).json()
    client.post("/api/v1/workers/worker-a/claim", headers=HEADERS, json={"capabilities": ["worker_probe"]})

    response = client.post(
        f"/api/v1/workers/worker-b/jobs/{job['id']}/complete",
        headers=HEADERS,
        json={"result": {}},
    )
    assert response.status_code == 409


def test_remote_worker_can_claim_image_prepare(client) -> None:
    headers = HEADERS
    client.post(
        "/api/v1/workers/register",
        headers=headers,
        json={"id": "image-worker", "capabilities": ["image_prepare"]},
    )
    created = client.post(
        "/api/v1/jobs",
        json={
            "type": "image_prepare",
            "payload": {"source_url": "https://example.com/image.jpg"},
            "idempotency_key": "image-prepare-1",
        },
    )
    assert created.status_code == 202
    claimed = client.post(
        "/api/v1/workers/image-worker/claim",
        headers=headers,
        json={"capabilities": ["image_prepare"]},
    ).json()
    assert claimed["type"] == "image_prepare"


def test_product_image_prepare_registers_asset_on_completion(client) -> None:
    product = client.post(
        "/api/v1/catalog/products",
        json={"sku": "WORKER-IMAGE-1", "source_type": "self_selected", "name": "Worker 图片测试"},
    ).json()
    queued = client.post(
        f"/api/v1/catalog/products/{product['id']}/assets/prepare",
        json={
            "source_url": "https://example.com/source.png",
            "name": "主图.jpg",
            "role": "main",
            "quality": 82,
        },
    )
    assert queued.status_code == 202
    job = queued.json()
    client.post(
        "/api/v1/workers/register",
        headers=HEADERS,
        json={"id": "asset-worker", "capabilities": ["image_prepare"]},
    )
    claimed = client.post(
        "/api/v1/workers/asset-worker/claim",
        headers=HEADERS,
        json={"capabilities": ["image_prepare"]},
    ).json()
    assert claimed["id"] == job["id"]
    completed = client.post(
        f"/api/v1/workers/asset-worker/jobs/{job['id']}/complete",
        headers=HEADERS,
        json={"result": {
            "ok": True,
            "oss_bucket": "example-assets",
            "oss_key": f"erp-worker/staging/{job['id']}/image.jpg",
            "oss_url": f"https://example-assets.example/erp-worker/staging/{job['id']}/image.jpg",
            "sha256": "a" * 64,
            "output_bytes": 1234,
            "width": 1000,
            "height": 1000,
            "output_format": "jpeg",
        }},
    )
    assert completed.status_code == 200
    asset_id = completed.json()["steps"][0]["details"]["asset_id"]
    detail = client.get(f"/api/v1/catalog/products/{product['id']}").json()
    assert detail["assets"][0]["asset"]["id"] == asset_id
    assert detail["assets"][0]["role"] == "main"
    assert detail["assets"][0]["asset"]["oss_bucket"] == "example-assets"

    duplicate = client.post(
        f"/api/v1/workers/asset-worker/jobs/{job['id']}/complete",
        headers=HEADERS,
        json={"result": {}},
    )
    assert duplicate.status_code == 409
