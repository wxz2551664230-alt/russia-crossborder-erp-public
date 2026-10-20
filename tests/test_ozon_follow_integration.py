from __future__ import annotations

from unittest.mock import patch
from sqlalchemy import select

from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps, loads
from erp.models import CatalogProduct, Job, Listing, ResearchProduct, SourcingCandidate, StoreListing
from erp.services.jobs import execute_job, get_job
from erp.services.listings import PIPELINE_STEPS, build_import_item
from erp.services.catalog import ensure_follow_catalog_product
from erp.integrations.ozon import OzonSellerAdapter


class FakeOzonAdapter:
    calls: list[tuple] = []

    def __init__(self, config): self.config = config
    def import_product(self, item): self.calls.append(("submit", item["offer_id"])); return 101
    def wait_import(self, task_id): return {"result": {"items": [{"status": "imported", "product_id": 777}]}}
    def update_stock(self, offer_id, warehouse_id, stock): self.calls.append(("stock", offer_id, warehouse_id, stock)); return {"result": []}
    def find_promotion_candidate(self, action_id, product_id): self.calls.append(("eligibility", action_id, product_id)); return {"id": product_id}
    def activate_promotion(self, action_id, product_id, action_price, stock): self.calls.append(("promotion", action_id, product_id, float(action_price), stock)); return {"result": []}


class FakeProductManager:
    def __init__(self, *_args): pass
    def upsert_product(self, payload): return {"ok": True, "product": payload.get("offer_id")}


def _run(job_id: str):
    with SessionLocal() as session:
        execute_job(session, get_job(session, job_id))
        return get_job(session, job_id).status


def test_test_adapters_complete_submit_stock_and_promotion_without_real_calls(client):
    FakeOzonAdapter.calls.clear()
    store = client.post("/api/v1/stores", json={"platform": "ozon", "name": "闭环店", "external_account_id": "90001", "secrets": {"api_key": "test"}}).json()["id"]
    client.post("/api/v1/connections", json={"provider": "product_library", "name": "A4 测试产品库", "base_url": "https://test.invalid", "config": {"shop_id": 1}, "secrets": {"token": "test"}})
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="FLOW-1", title="流程商品", status="source_confirmed", description_category_id=10, type_id=20)
        session.add(product); session.commit(); product_id = product.id
    client.post(f"/api/v1/research-products/{product_id}/sourcing-candidates", json={"provider":"product_library","external_product_id":"P1","external_sku_id":"S1","title":"货源","sku_name":"黑色","source_url":"https://test.invalid/p1","unit_price_cny":10})
    detail = client.get(f"/api/v1/research-products/{product_id}").json()
    client.post(f"/api/v1/research-products/{product_id}/sourcing-candidates/{detail['sourcing_candidates'][0]['id']}/select")
    listing = client.post("/api/v1/listings", json={"research_product_id":product_id,"base_offer_id":"FLOW-1","store_ids":[store]}).json()
    client.patch(f"/api/v1/listings/{listing['id']}", json={"title":"Русское название","pricing":{"price":999,"old_price":1299,"action_price":899},"package":{"depth_mm":300,"width_mm":200,"height_mm":100,"weight_g":500}})
    with SessionLocal() as session:
        child = session.query(StoreListing).filter_by(listing_id=listing["id"]).one()
        child.content_json = dumps({"title":"Русское название"})
        child.assets_json = dumps({"primary_image":"https://img.test/main.jpg","images":[]})
        session.commit()

    with patch("erp.services.listings.OzonSellerAdapter", FakeOzonAdapter), patch("erp.services.listings.ProductManagerApi", FakeProductManager):
        submit = client.post(f"/api/v1/listings/{listing['id']}/run", json={"dry_run":False,"store_ids":[store],"start_at":"submit"}).json()
        assert _run(submit["id"]) == "completed"
        stock = client.post(f"/api/v1/listings/{listing['id']}/run", json={"dry_run":False,"store_ids":[store],"start_at":"stock","warehouse_id":9,"stock":12}).json()
        assert _run(stock["id"]) == "completed"
        promo = client.post(f"/api/v1/listings/{listing['id']}/run", json={"dry_run":False,"store_ids":[store],"start_at":"promotion","promotion_action_id":7,"ozon_product_id":777,"action_price":899}).json()
        assert _run(promo["id"]) == "completed"

        # Finished stages may be requested again from a stale browser window, but
        # their Ozon write calls must not run a second time.
        repeat_submit = client.post(f"/api/v1/listings/{listing['id']}/run", json={"dry_run":False,"store_ids":[store],"start_at":"submit"}).json()
        assert _run(repeat_submit["id"]) == "completed"
        repeat_stock = client.post(f"/api/v1/listings/{listing['id']}/run", json={"dry_run":False,"store_ids":[store],"start_at":"stock","warehouse_id":9,"stock":12}).json()
        assert _run(repeat_stock["id"]) == "completed"
        repeat_promo = client.post(f"/api/v1/listings/{listing['id']}/run", json={"dry_run":False,"store_ids":[store],"start_at":"promotion","promotion_action_id":7,"ozon_product_id":777,"action_price":899}).json()
        assert _run(repeat_promo["id"]) == "completed"

    final = client.get(f"/api/v1/listings/{listing['id']}").json()
    assert final["stores"][0]["status"] == "completed"
    assert [call[0] for call in FakeOzonAdapter.calls] == ["submit", "stock", "eligibility", "promotion"]


def test_delete_product_is_local_and_blocked_by_active_job(client):
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="DELETE-1", title="删除测试")
        session.add(product); session.commit(); product_id = product.id
    job = client.post("/api/v1/jobs", json={"type":"asset_index","payload":{"research_product_id":product_id},"idempotency_key":"delete-block"}).json()
    blocked = client.delete(f"/api/v1/research-products/{product_id}")
    assert blocked.status_code == 409
    client.post(f"/api/v1/jobs/{job['id']}/cancel")
    deleted = client.delete(f"/api/v1/research-products/{product_id}")
    assert deleted.status_code == 200 and deleted.json()["local_only"] is True


def test_automation_board_reports_current_stage(client):
    store = client.post("/api/v1/stores", json={"platform":"ozon","name":"看板店","external_account_id":"90002","secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product=ResearchProduct(source="test",source_sku="BOARD-1",title="看板商品",description_category_id=1,type_id=2)
        session.add(product);session.commit();product_id=product.id
    client.post("/api/v1/listings",json={"research_product_id":product_id,"base_offer_id":"BOARD-1","store_ids":[store]})
    board=client.get("/api/v1/ozon-follow/automation").json()
    assert board["items"][0]["current"]["key"] == "source"
    assert len(board["items"][0]["steps"]) == 10


def test_attribute_dictionary_adapter_reads_all_ozon_pages():
    class AttributeAdapter(OzonSellerAdapter):
        def __init__(self): pass
        def post(self, path, payload, **_kwargs):
            assert path == "/v1/description-category/attribute/values"
            if payload["last_value_id"] == 0:
                return {"result":{"values":[{"id":1,"value":"Красный"}],"has_next":True,"last_value_id":1}}
            return {"result":{"values":[{"id":2,"value":"Синий"}],"has_next":False,"last_value_id":2}}
    values = AttributeAdapter().category_attribute_values(10, 20, 30)
    assert [item["id"] for item in values] == [1, 2]


def test_attribute_dictionary_adapter_supports_current_top_level_paging_shape():
    class AttributeAdapter(OzonSellerAdapter):
        def __init__(self): pass
        def post(self, path, payload, **_kwargs):
            if payload["last_value_id"] == 0:
                return {"result":[{"id":10,"value":"黑色"}],"has_next":True}
            assert payload["last_value_id"] == 10
            return {"result":[{"id":11,"value":"白色"}],"has_next":False}
    values=AttributeAdapter().category_attribute_values(10,20,30,"ZH_HANS")
    assert [item["id"] for item in values]==[10,11]


def test_uncertain_import_requires_task_id_before_retry(client):
    store = client.post("/api/v1/stores", json={"platform":"ozon","name":"恢复店","external_account_id":"90003","secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product=ResearchProduct(source="test",source_sku="RECOVER-1",title="恢复商品",description_category_id=1,type_id=2)
        session.add(product);session.commit();product_id=product.id
    listing=client.post("/api/v1/listings",json={"research_product_id":product_id,"base_offer_id":"RECOVER-1","store_ids":[store]}).json()
    with SessionLocal() as session:
        child=session.query(StoreListing).filter_by(listing_id=listing["id"]).one()
        child.status="import_uncertain";session.commit()
    invalid=client.post(f"/api/v1/listings/{listing['id']}/stores/{store}/recover-import",json={"external_task_id":0})
    assert invalid.status_code == 422
    recovered=client.post(f"/api/v1/listings/{listing['id']}/stores/{store}/recover-import",json={"external_task_id":12345})
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "import_pending"
    assert recovered.json()["external_task_id"] == "12345"


def test_new_follow_listing_does_not_invent_package_dimensions(client):
    store = client.post("/api/v1/stores", json={"platform":"ozon","name":"事实店","external_account_id":"90004","secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="FACT-1", title="事实商品", weight_grams=420)
        session.add(product); session.commit(); product_id = product.id
    listing = client.post("/api/v1/listings", json={"research_product_id":product_id,"base_offer_id":"FACT-1","store_ids":[store]}).json()
    assert listing["package"] == {"depth_mm": 300, "width_mm": 200, "height_mm": 100, "weight_g": 420, "dimensions_source": "operational_default"}


def test_attributes_and_content_are_isolated_per_ozon_store(client):
    store_a = client.post("/api/v1/stores", json={"platform":"ozon","name":"属性甲店","external_account_id":"90005","secrets":{"api_key":"test"}}).json()["id"]
    store_b = client.post("/api/v1/stores", json={"platform":"ozon","name":"属性乙店","external_account_id":"90006","secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="ATTR-1", title="属性商品")
        session.add(product); session.commit(); product_id = product.id
    listing = client.post("/api/v1/listings", json={"research_product_id":product_id,"base_offer_id":"ATTR-1","store_ids":[store_a,store_b]}).json()
    response = client.patch(f"/api/v1/listings/{listing['id']}", json={"store_id":store_a,"title":"Русское название A","attributes":[{"id":11,"values":[{"value":"A"}]}]})
    assert response.status_code == 200, response.text
    stores = {row["store_id"]: row for row in response.json()["stores"]}
    assert stores[store_a]["content"]["title"] == "Русское название A"
    assert stores[store_a]["attributes"][0]["id"] == 11
    assert stores[store_b]["content"] == {}
    assert stores[store_b]["attributes"] == []


def test_image_approval_immediately_leaves_review_queue_without_duplicate_job(client):
    store = client.post("/api/v1/stores", json={"platform":"ozon","name":"主图核验店","external_account_id":"90010","secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="IMAGE-1", title="主图核验商品")
        session.add(product); session.commit(); product_id = product.id
    listing = client.post("/api/v1/listings", json={"research_product_id":product_id,"base_offer_id":"IMAGE-1","store_ids":[store]}).json()
    with SessionLocal() as session:
        child = session.query(StoreListing).filter_by(listing_id=listing["id"], store_id=store).one()
        child.status = "image_review"
        session.commit()

    endpoint = f"/api/v1/listings/{listing['id']}/stores/{store}/image/approve"
    first = client.post(endpoint)
    second = client.post(endpoint)

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    with SessionLocal() as session:
        child = session.query(StoreListing).filter_by(listing_id=listing["id"], store_id=store).one()
        jobs = session.scalars(select(Job).where(Job.type == "ozon_image_review_action")).all()
        assert child.status == "image_approval_queued"
        assert child.last_error == ""
        assert len(jobs) == 1


def test_submit_packages_use_each_store_offer_content_attributes_and_images(client):
    store_a = client.post("/api/v1/stores", json={"platform":"ozon","name":"提交甲店","external_account_id":"90105","secrets":{"api_key":"test"}}).json()["id"]
    store_b = client.post("/api/v1/stores", json={"platform":"ozon","name":"提交乙店","external_account_id":"90106","secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="SUBMIT-1", title="源商品", description_category_id=10, type_id=20)
        session.add(product); session.commit(); product_id = product.id
    listing = client.post("/api/v1/listings", json={"research_product_id":product_id,"base_offer_id":"SUBMIT-1","store_ids":[store_a,store_b]}).json()
    with SessionLocal() as session:
        row = session.query(Listing).filter_by(id=listing["id"]).one()
        row.package_json = dumps({"depth_mm":300,"width_mm":200,"height_mm":100,"weight_g":500})
        children = {child.store_id: child for child in row.store_listings}
        for store_id, title, suffix in ((store_a, "Название магазина А", "a"), (store_b, "Название магазина Б", "b")):
            child = children[store_id]
            child.offer_id = f"OFFER-{suffix.upper()}"
            child.content_json = dumps({"title":title,"description_ru":f"Описание {suffix}"})
            child.attributes_json = dumps([{"id":11,"values":[{"value":suffix}]}])
            child.assets_json = dumps({"primary_image":f"https://img.test/{suffix}-main.jpg","images":[f"https://img.test/{suffix}-side.jpg"]})
            child.pricing_json = dumps({"price":999 if suffix == "a" else 1099})
        session.commit()
        packages = {store_id: build_import_item(row, child, session) for store_id, child in children.items()}
    assert packages[store_a]["offer_id"] == "OFFER-A"
    assert packages[store_b]["offer_id"] == "OFFER-B"
    assert packages[store_a]["name"] == "Название магазина А"
    assert packages[store_b]["name"] == "Название магазина Б"
    assert packages[store_a]["attributes"] != packages[store_b]["attributes"]
    assert packages[store_a]["primary_image"] != packages[store_b]["primary_image"]
    assert packages[store_a]["price"] != packages[store_b]["price"]


def test_follow_pipeline_contains_only_real_checkpoints():
    assert PIPELINE_STEPS == [
        ("validate", "检查上架资料"),
        ("ai_prepare", "生成并核验文案与主图"),
        ("submit", "提交 OZON 商品"),
        ("product_sync", "同步统一产品库"),
        ("stock", "发布库存"),
        ("promotion", "加入促销"),
    ]


def test_follow_auto_sourcing_is_disabled(client):
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="MANUAL-1", title="只允许人工找源")
        session.add(product); session.commit(); product_id = product.id
    response = client.post(f"/api/v1/research-products/{product_id}/sourcing", json={"candidate_count":10})
    assert response.status_code == 409
    assert "人工" in response.json()["message"]


def test_follow_catalog_uses_erp_sku_and_keeps_ozon_facts(client):
    store = client.post("/api/v1/stores", json={"platform":"ozon","name":"产品库店","external_account_id":"90007","secrets":{"api_key":"test"}}).json()["id"]
    other_store = client.post("/api/v1/stores", json={"platform":"ozon","name":"其他风格店","external_account_id":"90008","secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="seerfar", source_sku="123456789", title="OZON原始标题", category="家居", ozon_url="https://www.ozon.ru/product/123456789/", image_url="https://cdn.test/main.jpg", image_urls_json=dumps(["https://cdn.test/main.jpg","https://cdn.test/side.jpg"]), brand="原品牌", selling_price_rub=999, sales=88, rating=4.8, weight_grams=500, raw_json=dumps({"source":"seerfar"}), description_category_id=10, type_id=20, category_display="家居")
        session.add(product); session.flush()
        source = SourcingCandidate(research_product_id=product.id,provider="manual",external_product_id="16880001",external_sku_id="SKU-RED",title="1688标题",sku_name="红色",source_url="https://detail.1688.com/offer/16880001.html",unit_price_cny=20,domestic_delivery_cny=5,selected=True,raw_json=dumps({"skuName":"红色","title":"1688标题"}))
        session.add(source); session.flush()
        listing = Listing(research_product_id=product.id,base_offer_id="AUTO-20260911-001",title=product.title,package_json=dumps({"depth_mm":300,"width_mm":200,"height_mm":100,"weight_g":500}),image_policy_json=dumps({"catalog_primary_store_id":store}))
        session.add(listing); session.flush()
        session.add(StoreListing(listing_id=listing.id,store_id=other_store,offer_id=f"OTHER-{listing.base_offer_id}",idempotency_key="follow-library-other",assets_json=dumps({"primary_image":"https://oss.test/other-main.jpg","images":["https://oss.test/other-side.jpg"]})))
        session.add(StoreListing(listing_id=listing.id,store_id=store,offer_id=listing.base_offer_id,idempotency_key="follow-library-test",assets_json=dumps({"primary_image":"https://oss.test/main.jpg","images":["https://oss.test/side.jpg"]})))
        session.commit()
        catalog = ensure_follow_catalog_product(session, listing)
        assert catalog.sku == "FOL-AUTO-20260911-001"
        assert catalog.variants[0].sku == catalog.sku
        assert loads(catalog.production_json,{})["selected_supplier_sku_id"] == "SKU-RED"
        assert loads(catalog.production_json,{})["catalog_primary_store_id"] == store
        assert [link.asset.oss_url for link in catalog.asset_links if link.role == "main"] == ["https://oss.test/main.jpg"]
        assert loads(catalog.source_snapshot_json,{})["source_sku"] == "123456789"


def test_follow_catalog_accepts_missing_dimensions_and_prefers_complete_gallery(client):
    first = client.post("/api/v1/stores", json={"platform":"ozon","name":"仅主图店","external_account_id":"90017","secrets":{"api_key":"test"}}).json()["id"]
    complete = client.post("/api/v1/stores", json={"platform":"ozon","name":"完整图库店","external_account_id":"90018","secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="seerfar", source_sku="NO-DIMS", title="无尺寸跟卖商品", weight_grams=500)
        session.add(product); session.flush()
        session.add(SourcingCandidate(research_product_id=product.id, provider="manual", external_product_id="1688-ND", external_sku_id="SKU-ND", title="货源", sku_name="默认", source_url="https://detail.1688.com/offer/1.html", unit_price_cny=20, selected=True))
        listing = Listing(research_product_id=product.id, base_offer_id="NO-DIMS", title=product.title, package_json=dumps({"depth_mm":300,"width_mm":200,"height_mm":100,"weight_g":500,"dimensions_source":"operational_default"}))
        session.add(listing); session.flush()
        session.add(StoreListing(listing_id=listing.id, store_id=first, offer_id="FIRST-ND", idempotency_key="first-nd", assets_json=dumps({"primary_image":"https://oss.test/first.jpg","images":[]})))
        session.add(StoreListing(listing_id=listing.id, store_id=complete, offer_id="COMPLETE-ND", idempotency_key="complete-nd", assets_json=dumps({"primary_image":"https://oss.test/complete.jpg","images":["https://oss.test/side.jpg"]})))
        session.commit()

        catalog = ensure_follow_catalog_product(session, listing)
        assert (catalog.package_length_mm, catalog.package_width_mm, catalog.package_height_mm) == (None, None, None)
        assert loads(catalog.production_json, {})["catalog_primary_store_id"] == complete
        assert sorted(link.asset.oss_url for link in catalog.asset_links) == ["https://oss.test/complete.jpg", "https://oss.test/side.jpg"]


def test_erp_gallery_extension_accepts_only_imported_ozon_sku(client):
    with SessionLocal() as session:
        session.add(ResearchProduct(source="seerfar",source_sku="987654321",title="图库商品")); session.commit()
    payload={"sku":"987654321","page_url":"https://www.ozon.ru/product/demo-987654321/?__ozon_gallery_bridge=1","title":"图库商品","images":["https://cdn1.ozone.ru/s3/multimedia-a/wc1000/demo.jpg"]}
    response=client.post("/api/v1/ozon-gallery/capture",json=payload,headers={"X-Ozon-Gallery-Bridge":"ozon-gallery-bridge-v1"})
    assert response.status_code == 200
    assert response.json()["image_count"] == 1
    with SessionLocal() as session:
        product = session.scalar(select(ResearchProduct).where(ResearchProduct.source_sku == "987654321"))
        assert loads(product.image_urls_json, []) == ["https://cdn1.ozone.ru/s3/multimedia-a/wc2000/demo.jpg"]


def test_manual_source_selection_queues_per_store_ai_and_synthesizes_missing_supplier_sku(client):
    store = client.post("/api/v1/stores", json={"platform":"ozon","name":"自动推进店","external_account_id":"90009","secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="seerfar",source_sku="9988776655",title="手工规格商品",status="category_confirmed",description_category_id=10,type_id=20)
        session.add(product); session.commit(); product_id=product.id
    listing = client.post("/api/v1/listings",json={"research_product_id":product_id,"base_offer_id":"AUTO-NEXT-1","store_ids":[store],"catalog_primary_store_id":store}).json()
    candidate = client.post(f"/api/v1/research-products/{product_id}/sourcing-candidates",json={"provider":"manual","external_product_id":"123456789","external_sku_id":"","title":"1688货源","sku_name":"米白色 / 160x100cm","source_url":"https://detail.1688.com/offer/123456789.html","unit_price_cny":12.5}).json()
    assert candidate["external_sku_id"] == "MANUAL-123456789"
    selected = client.post(f"/api/v1/research-products/{product_id}/sourcing-candidates/{candidate['id']}/select").json()
    assert selected["next_stage"] == "ai_preparing"
    assert len(selected["jobs"]) == 1
    assert selected["jobs"][0]["type"] == "ozon_ai_prepare"
    assert client.get(f"/api/v1/listings/{listing['id']}").json()["status"] == "ai_preparing"


def test_ai_prepare_retry_skips_store_steps_that_already_completed(client):
    store = client.post("/api/v1/stores", json={"platform":"ozon","name":"重试店","external_account_id":"90201","secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="RETRY-1", title="重试商品", description_category_id=10, type_id=20)
        session.add(product); session.commit(); product_id = product.id
    listing = client.post("/api/v1/listings", json={"research_product_id":product_id,"base_offer_id":"RETRY-1","store_ids":[store]}).json()
    candidate = client.post(f"/api/v1/research-products/{product_id}/sourcing-candidates", json={"provider":"manual","external_product_id":"12345678","external_sku_id":"SKU-1","title":"货源","sku_name":"默认","source_url":"https://detail.1688.com/offer/12345678.html","unit_price_cny":10}).json()
    selected = client.post(f"/api/v1/research-products/{product_id}/sourcing-candidates/{candidate['id']}/select").json()
    job_id = selected["jobs"][0]["id"]
    with SessionLocal() as session:
        job = get_job(session, job_id)
        job.steps[0].status = "completed"
        job.steps[0].progress = 100
        session.commit()
    with patch("erp.services.ozon_follow.prepare_listing_with_ai") as prepare:
        assert _run(job_id) == "completed"
        prepare.assert_not_called()


def test_image_actions_are_mutually_exclusive_and_require_review_state(client):
    store = client.post("/api/v1/stores", json={"platform":"ozon","name":"互斥店","external_account_id":"90202","secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="MUTEX-1", title="互斥商品")
        session.add(product); session.commit(); product_id = product.id
    listing = client.post("/api/v1/listings", json={"research_product_id":product_id,"base_offer_id":"MUTEX-1","store_ids":[store]}).json()
    with SessionLocal() as session:
        child = session.query(StoreListing).filter_by(listing_id=listing["id"], store_id=store).one()
        child.status = "image_review"
        session.commit()
    regenerate = client.post(f"/api/v1/listings/{listing['id']}/stores/{store}/image/regenerate")
    assert regenerate.status_code == 202
    assert client.get(f"/api/v1/listings/{listing['id']}").json()["stores"][0]["status"] == "image_regeneration_queued"
    blocked = client.post(f"/api/v1/listings/{listing['id']}/stores/{store}/image/approve")
    assert blocked.status_code == 409
    assert "另一项主图操作" in blocked.json()["message"]

    client.post(f"/api/v1/jobs/{regenerate.json()['id']}/cancel")
    with SessionLocal() as session:
        child = session.query(StoreListing).filter_by(listing_id=listing["id"], store_id=store).one()
        child.status = "ready"
        session.commit()
    invalid_state = client.post(f"/api/v1/listings/{listing['id']}/stores/{store}/image/approve")
    assert invalid_state.status_code == 409
    assert "等待人工核验" in invalid_state.json()["message"]


def test_reject_image_review_removes_only_the_selected_store_version(client):
    store_a = client.post("/api/v1/stores", json={"platform":"ozon","name":"不通过店","external_account_id":"90210","secrets":{"api_key":"test"}}).json()["id"]
    store_b = client.post("/api/v1/stores", json={"platform":"ozon","name":"已通过店","external_account_id":"90211","secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="REJECT-STORE-1", title="分店删除商品")
        session.add(product); session.commit(); product_id = product.id
    listing = client.post("/api/v1/listings", json={"research_product_id":product_id,"base_offer_id":"REJECT-STORE-1","store_ids":[store_a,store_b]}).json()
    with SessionLocal() as session:
        children = {row.store_id: row for row in session.query(StoreListing).filter_by(listing_id=listing["id"]).all()}
        children[store_a].status = "image_review"
        children[store_b].status = "ready"
        session.commit()
    removed = client.delete(f"/api/v1/listings/{listing['id']}/stores/{store_a}/image-review")
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"removed": True, "store_id": store_a, "listing_removed": False, "local_only": True}
    remaining = client.get(f"/api/v1/listings/{listing['id']}").json()["stores"]
    assert [(row["store_id"], row["status"]) for row in remaining] == [(store_b, "ready")]


def test_image_action_is_blocked_while_ai_prepare_is_active(client):
    store = client.post("/api/v1/stores", json={"platform":"ozon","name":"准备互斥店","external_account_id":"90203","secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="AI-MUTEX-1", title="准备互斥商品")
        session.add(product); session.commit(); product_id = product.id
    listing = client.post("/api/v1/listings", json={"research_product_id":product_id,"base_offer_id":"AI-MUTEX-1","store_ids":[store]}).json()
    with SessionLocal() as session:
        child = session.query(StoreListing).filter_by(listing_id=listing["id"], store_id=store).one()
        child.status = "image_review"
        session.commit()
    client.post(f"/api/v1/listings/{listing['id']}/ai-prepare")
    blocked = client.post(f"/api/v1/listings/{listing['id']}/stores/{store}/image/approve")
    assert blocked.status_code == 409
    assert "仍在执行 AI 准备" in blocked.json()["message"]


def test_completed_store_can_be_reviewed_while_other_store_ai_is_still_running(client):
    store_a = client.post("/api/v1/stores", json={"platform":"ozon","name":"先完成店","external_account_id":"90206","secrets":{"api_key":"test"}}).json()["id"]
    store_b = client.post("/api/v1/stores", json={"platform":"ozon","name":"生成中店","external_account_id":"90207","secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="PARTIAL-AI-1", title="分店铺进度商品")
        session.add(product); session.commit(); product_id = product.id
    listing = client.post("/api/v1/listings", json={"research_product_id":product_id,"base_offer_id":"PARTIAL-AI-1","store_ids":[store_a,store_b]}).json()
    job = client.post(f"/api/v1/listings/{listing['id']}/ai-prepare").json()
    with SessionLocal() as session:
        child = session.query(StoreListing).filter_by(listing_id=listing["id"], store_id=store_a).one()
        child.status = "image_review"
        active = get_job(session, job["id"])
        completed_step = next(step for step in active.steps if step.key == f"{store_a}:ai_prepare")
        completed_step.status = "completed"
        completed_step.progress = 100
        session.commit()
    approved = client.post(f"/api/v1/listings/{listing['id']}/stores/{store_a}/image/approve")
    assert approved.status_code == 202, approved.text
    assert approved.json()["type"] == "ozon_image_review_action"


def test_dry_run_completes_only_the_steps_created_for_the_requested_stage(client):
    store = client.post("/api/v1/stores", json={"platform":"ozon","name":"模拟店","external_account_id":"90204","secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="DRY-1", title="模拟商品", description_category_id=10, type_id=20)
        session.add(product); session.flush()
        listing = Listing(research_product_id=product.id, base_offer_id="DRY-1", title="模拟商品", package_json=dumps({"depth_mm":300,"width_mm":200,"height_mm":100,"weight_g":500}))
        session.add(listing); session.flush()
        session.add(StoreListing(listing_id=listing.id, store_id=store, offer_id="DRY-1", idempotency_key="dry-1", status="ready", pricing_json=dumps({"price":"999","old_price":"1299"}), content_json=dumps({"title":"Название","description_ru":"Описание"}), assets_json=dumps({"primary_image":"https://img.test/main.jpg","images":[],"ai_preparation":{"image_review_status":"APPROVED"}})))
        session.commit(); listing_id = listing.id
    job = client.post(f"/api/v1/listings/{listing_id}/run", json={"dry_run":True,"store_ids":[store],"start_at":"stock","warehouse_id":9,"stock":2}).json()
    assert [step["key"].split(":")[-1] for step in job["steps"]] == ["stock"]
    assert _run(job["id"]) == "completed"
    with SessionLocal() as session:
        finished = get_job(session, job["id"])
        assert [(step.key.split(":")[-1], step.status) for step in finished.steps] == [("stock", "completed")]


def test_calculated_price_is_written_per_store_and_used_by_submit_package(client):
    store = client.post("/api/v1/stores", json={"platform":"ozon","name":"价格写入店","external_account_id":"90205","config":{"ozon_follow_pricing":{"target_cost_profit_margin":30,"pricing_category_module":"家居与汽车用品","pricing_product_category":"装饰、清洁与储物"}},"secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="PRICE-WRITE-1", title="价格商品", description_category_id=10, type_id=20, weight_grams=500)
        session.add(product); session.flush()
        session.add(SourcingCandidate(research_product_id=product.id, provider="manual", external_product_id="12345678", external_sku_id="SKU-1", title="货源", sku_name="默认", source_url="https://detail.1688.com/offer/12345678.html", unit_price_cny=20, domestic_delivery_cny=3, selected=True))
        session.commit(); product_id = product.id
    listing = client.post("/api/v1/listings", json={"research_product_id":product_id,"base_offer_id":"PRICE-WRITE-1","store_ids":[store]}).json()
    response = client.post(f"/api/v1/listings/{listing['id']}/price")
    assert response.status_code == 200, response.text
    saved = response.json()["stores"][0]["pricing"]
    assert float(saved["price"]) > 0
    assert float(saved["old_price"]) > float(saved["price"])
    assert float(saved["action_price"]) > 0
    assert saved["calculation"]["ozon_price"] == saved["price"]
    assert float(saved["calculation"]["purchase_price"]) == 23
    with SessionLocal() as session:
        row = session.query(Listing).filter_by(id=listing["id"]).one()
        child = row.store_listings[0]
        child.assets_json = dumps({"primary_image":"https://img.test/main.jpg","images":[]})
        session.commit()
        package = build_import_item(row, child, session)
        assert package["price"] == saved["price"]
        assert package["old_price"] == saved["old_price"]


def test_source_confirmation_writes_prices_for_existing_listing_with_empty_dimensions(client):
    store = client.post("/api/v1/stores", json={"platform":"ozon","name":"确认后定价店","external_account_id":"90208","config":{"ozon_follow_pricing":{"target_cost_profit_margin":30,"pricing_category_module":"家居与汽车用品","pricing_product_category":"装饰、清洁与储物"}},"secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="PRICE-AFTER-SOURCE", title="确认后定价商品", description_category_id=10, type_id=20, weight_grams=220)
        session.add(product); session.commit(); product_id = product.id
    listing = client.post("/api/v1/listings", json={"research_product_id":product_id,"base_offer_id":"PRICE-AFTER-SOURCE","store_ids":[store]}).json()
    with SessionLocal() as session:
        row = session.query(Listing).filter_by(id=listing["id"]).one()
        row.package_json = dumps({"depth_mm":None,"width_mm":None,"height_mm":None,"weight_g":220})
        session.commit()
    candidate = client.post(f"/api/v1/research-products/{product_id}/sourcing-candidates", json={"provider":"manual","external_product_id":"22345678","external_sku_id":"SKU-2","title":"货源","sku_name":"默认","source_url":"https://detail.1688.com/offer/22345678.html","unit_price_cny":3.44,"domestic_delivery_cny":2}).json()
    selected = client.post(f"/api/v1/research-products/{product_id}/sourcing-candidates/{candidate['id']}/select")
    assert selected.status_code == 200, selected.text
    saved = client.get(f"/api/v1/listings/{listing['id']}").json()["stores"][0]["pricing"]
    assert float(saved["price"]) > 0
    assert float(saved["calculation"]["purchase_price"]) == 5.44


def test_ozon_submit_uses_operational_dimensions_without_treating_them_as_ai_facts(client):
    store = client.post("/api/v1/stores", json={"platform":"ozon","name":"虚拟尺寸店","external_account_id":"90209","secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="DEFAULT-DIMS-1", title="虚拟尺寸商品", description_category_id=10, type_id=20, weight_grams=220)
        session.add(product); session.flush()
        listing = Listing(research_product_id=product.id, base_offer_id="DEFAULT-DIMS-1", title="虚拟尺寸商品", package_json=dumps({"depth_mm":None,"width_mm":None,"height_mm":None,"weight_g":220,"dimensions_source":"operational_default"}))
        session.add(listing); session.flush()
        child = StoreListing(listing_id=listing.id, store_id=store, offer_id="DEFAULT-DIMS-1", idempotency_key="test", pricing_json=dumps({"price":"99","old_price":"129"}), assets_json=dumps({"primary_image":"https://img.test/main.jpg","images":[]}))
        session.add(child); session.commit()
        package = build_import_item(listing, child, session)
        assert (package["depth"], package["width"], package["height"]) == (300.0, 200.0, 100.0)


def test_disabled_store_is_excluded_from_follow_pricing_ai_and_submission(client):
    store = client.post("/api/v1/stores", json={"platform":"ozon","name":"停用跟卖店","external_account_id":"90212","config":{"ozon_follow_pricing":{"target_cost_profit_margin":30,"pricing_category_module":"家居与汽车用品","pricing_product_category":"装饰、清洁与储物"}},"secrets":{"api_key":"test"}}).json()["id"]
    with SessionLocal() as session:
        product = ResearchProduct(source="test", source_sku="DISABLED-FOLLOW-1", title="停用店商品", description_category_id=10, type_id=20, weight_grams=220)
        session.add(product); session.commit(); product_id = product.id
    listing = client.post("/api/v1/listings", json={"research_product_id":product_id,"base_offer_id":"DISABLED-FOLLOW-1","store_ids":[store]}).json()
    assert client.patch(f"/api/v1/stores/{store}", json={"enabled":False}).status_code == 200
    candidate = client.post(f"/api/v1/research-products/{product_id}/sourcing-candidates", json={"provider":"manual","external_product_id":"32345678","external_sku_id":"SKU-3","title":"货源","sku_name":"默认","source_url":"https://detail.1688.com/offer/32345678.html","unit_price_cny":5,"domestic_delivery_cny":1}).json()
    selected = client.post(f"/api/v1/research-products/{product_id}/sourcing-candidates/{candidate['id']}/select").json()
    assert selected["jobs"] == []
    detail = client.get(f"/api/v1/listings/{listing['id']}").json()
    assert detail["stores"][0]["store_enabled"] is False
    assert "price" not in detail["stores"][0]["pricing"]
    run = client.post(f"/api/v1/listings/{listing['id']}/run", json={"dry_run":False,"store_ids":[store],"start_at":"submit"})
    assert run.status_code == 409
