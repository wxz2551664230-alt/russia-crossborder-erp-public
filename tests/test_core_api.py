from __future__ import annotations

def test_connection_secrets_are_masked(client):
    response = client.post("/api/v1/connections", json={"provider":"daji","name":"主找源","secrets":{"app_key":"visible-key","app_secret":"never-return-this"}})
    assert response.status_code == 201
    body = response.json()
    assert body["secret_status"]["app_secret"]["configured"] is True
    assert "never-return-this" not in response.text
    listed = client.get("/api/v1/connections").text
    assert "never-return-this" not in listed

def test_same_product_has_isolated_store_listings(client):
    product = _product(client)
    first = _store(client, "店铺 A", "10001", "A-")
    second = _store(client, "店铺 B", "10002", "B-")
    response = client.post("/api/v1/listings", json={"research_product_id":product,"base_offer_id":"SKU-7","store_ids":[first,second]})
    assert response.status_code == 201
    stores = response.json()["stores"]
    assert {item["offer_id"] for item in stores} == {"A-SKU-7", "B-SKU-7"}
    assert len({item["id"] for item in stores}) == 2

def test_offer_prefix_change_updates_only_unsubmitted_drafts(client):
    from erp.core.database import SessionLocal
    from erp.models import StoreListing

    product = _product(client)
    store = _store(client, "前缀更新店", "10006", "OLD-")
    first = client.post("/api/v1/listings", json={"research_product_id":product,"base_offer_id":"PREFIX-1","store_ids":[store]}).json()
    second_product = _product_with_sku("P-2")
    second = client.post("/api/v1/listings", json={"research_product_id":second_product,"base_offer_id":"PREFIX-2","store_ids":[store]}).json()
    with SessionLocal() as session:
        submitted = session.query(StoreListing).filter_by(listing_id=second["id"], store_id=store).one()
        submitted.external_task_id = "12345"
        session.commit()

    response = client.patch(f"/api/v1/stores/{store}", json={"offer_prefix":"NEW-"})
    assert response.status_code == 200
    assert client.get(f"/api/v1/listings/{first['id']}").json()["stores"][0]["offer_id"] == "NEW-PREFIX-1"
    assert client.get(f"/api/v1/listings/{second['id']}").json()["stores"][0]["offer_id"] == "OLD-PREFIX-2"

def test_followup_actions_create_only_the_requested_pipeline_stage(client):
    product = _product(client)
    store = _store(client, "阶段测试店", "10003", "T-")
    listing = client.post("/api/v1/listings", json={"research_product_id": product, "base_offer_id": "SKU-8", "store_ids": [store]}).json()

    submit_job = client.post(f"/api/v1/listings/{listing['id']}/run", json={"dry_run": True, "store_ids": [store], "start_at": "submit"}).json()
    assert [step["key"].split(":")[-1] for step in submit_job["steps"]] == ["submit", "product_sync"]

    stock_job = client.post(f"/api/v1/listings/{listing['id']}/run", json={"dry_run": True, "store_ids": [store], "start_at": "stock", "warehouse_id": 9, "stock": 12}).json()
    assert [step["key"].split(":")[-1] for step in stock_job["steps"]] == ["stock"]

    promotion_job = client.post(f"/api/v1/listings/{listing['id']}/run", json={"dry_run": True, "store_ids": [store], "start_at": "promotion", "promotion_action_id": 7, "ozon_product_id": 8, "action_price": 999}).json()
    assert [step["key"].split(":")[-1] for step in promotion_job["steps"]] == ["promotion"]

def test_pending_store_listing_can_be_manually_completed_with_audit(client):
    from erp.core.database import SessionLocal
    from erp.models import StoreListing

    product = _product(client)
    store = _store(client, "手动完成店", "10007", "M-")
    listing = client.post("/api/v1/listings", json={"research_product_id": product, "base_offer_id": "SKU-M", "store_ids": [store]}).json()
    with SessionLocal() as session:
        child = session.query(StoreListing).filter_by(listing_id=listing["id"], store_id=store).one()
        child.status = "stock_pending"
        session.commit()

    response = client.post(f"/api/v1/listings/{listing['id']}/stores/{store}/manual-complete")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["stock_status"] == "completed"
    assert body["promotion_status"] == "completed"
    assert body["manual_completion"]["from_status"] == "stock_pending"
    assert body["manual_completion"]["completed_at"]
    assert client.get(f"/api/v1/listings/{listing['id']}").json()["status"] == "completed"

def test_manual_completion_rejects_store_before_stock_stage(client):
    product = _product(client)
    store = _store(client, "禁止提前完成店", "10008", "N-")
    listing = client.post("/api/v1/listings", json={"research_product_id": product, "base_offer_id": "SKU-N", "store_ids": [store]}).json()
    response = client.post(f"/api/v1/listings/{listing['id']}/stores/{store}/manual-complete")
    assert response.status_code == 409

def test_repeated_listing_stage_request_returns_the_active_job(client):
    product = _product(client)
    store = _store(client, "防重复店", "10004", "D-")
    listing = client.post("/api/v1/listings", json={"research_product_id": product, "base_offer_id": "SKU-D", "store_ids": [store]}).json()
    body = {"dry_run": False, "store_ids": [store], "start_at": "submit"}

    first = client.post(f"/api/v1/listings/{listing['id']}/run", json=body)
    second = client.post(f"/api/v1/listings/{listing['id']}/run", json=body)

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]

def test_repeated_ai_prepare_request_returns_the_active_job(client):
    product = _product(client)
    store = _store(client, "AI 防重复店", "10005", "AI-")
    listing = client.post("/api/v1/listings", json={"research_product_id": product, "base_offer_id": "SKU-AI", "store_ids": [store]}).json()

    first = client.post(f"/api/v1/listings/{listing['id']}/ai-prepare")
    second = client.post(f"/api/v1/listings/{listing['id']}/ai-prepare")

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]

def test_job_idempotency_and_controls(client):
    body={"type":"asset_index","payload":{"root":"D:/not-present"},"idempotency_key":"same-index"}
    first=client.post("/api/v1/jobs",json=body).json(); second=client.post("/api/v1/jobs",json=body).json()
    assert first["id"] == second["id"]
    paused=client.post(f"/api/v1/jobs/{first['id']}/pause").json(); assert paused["status"] == "paused"
    resumed=client.post(f"/api/v1/jobs/{first['id']}/resume").json(); assert resumed["status"] == "queued"
    stopped=client.post(f"/api/v1/jobs/{first['id']}/cancel").json(); assert stopped["status"] == "cancelled"

def test_error_has_trace_id(client):
    response=client.get("/api/v1/jobs/missing")
    assert response.status_code == 404
    assert response.json()["trace_id"]

def _product(client):
    from erp.core.database import SessionLocal
    from erp.models import ResearchProduct
    with SessionLocal() as session:
        item=ResearchProduct(source="test",source_sku="P-1",title="测试商品",status="source_confirmed")
        session.add(item);session.commit();return item.id

def _product_with_sku(source_sku):
    from erp.core.database import SessionLocal
    from erp.models import ResearchProduct
    with SessionLocal() as session:
        item=ResearchProduct(source="test",source_sku=source_sku,title="测试商品",status="source_confirmed")
        session.add(item);session.commit();return item.id

def _store(client,name,external,prefix):
    response=client.post("/api/v1/stores",json={"platform":"ozon","name":name,"external_account_id":external,"offer_prefix":prefix,"secrets":{"api_key":"secret"}})
    assert response.status_code == 201
    return response.json()["id"]
