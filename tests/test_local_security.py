from __future__ import annotations


BRIDGE_ORIGIN = "chrome-extension://jkfidffpjgcedfhbggfkokakhnaealgf"


def test_gallery_bridge_origin_is_limited_to_capture_endpoint(client):
    headers = {
        "Origin": BRIDGE_ORIGIN,
        "X-Ozon-Gallery-Bridge": "ozon-gallery-bridge-v1",
    }
    capture = client.post("/api/v1/ozon-gallery/capture", headers=headers, json={})
    assert capture.status_code == 409
    assert capture.json()["code"] != "origin_rejected"

    other = client.post("/api/v1/jobs", headers=headers, json={})
    assert other.status_code == 403
    assert other.json()["code"] == "origin_rejected"


def test_gallery_bridge_origin_requires_fixed_request_marker(client):
    response = client.post(
        "/api/v1/ozon-gallery/capture",
        headers={"Origin": BRIDGE_ORIGIN},
        json={},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "origin_rejected"
