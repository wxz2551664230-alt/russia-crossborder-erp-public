import asyncio
import json
import sqlite3
from contextlib import closing

import pytest
from cryptography.fernet import Fernet
from openpyxl import load_workbook

from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps
from erp.core.security import vault
from erp.domains.orders import api as order_api
from erp.domains.orders.legacy_engine import HicustomOrderQueue, OrderError, OrderManager, official_supplier_tracking_number, s2b_country_code
from erp.domains.orders.migration import migrate
from erp.domains.orders.products import ProductLibrary
from erp.domains.orders.service import ErpOrderManager, sync_platform_images
from erp.domains.orders.service import AsyncSupplier
from erp.core.errors import AppError
from erp.domains.pod.models import PodComposition, PodProductVersion
from erp.domains.pod.integrations.hicustom import HicustomClient
from erp.models import CatalogProduct, CatalogVariant, IntegrationConnection


def seed(session):
    product = CatalogProduct(sku="POD-000001", group_code="POD-000001", name="测试 POD", source_type="pod", supplier="hicustom")
    session.add(product); session.flush()
    session.add(CatalogVariant(product_id=product.id, sku="POD-000001-01", supplier_sku_id="123", color="白", size="M", properties_json=dumps({"color_id": 1, "size_id": 2})))
    session.add(PodProductVersion(catalog_product_id=product.id, version_no=1, is_current=True, supplier="hicustom", product_code="CODE-1"))
    session.commit()


def posting(number="N1"):
    return {"posting_number": number, "products": [{"offer_id": "SHOP-POD-000001-01", "name": "测试商品", "quantity": 2, "price": "25"}]}


@pytest.fixture
def orders(tmp_path):
    with SessionLocal() as session:
        seed(session)
        value = OrderManager(tmp_path, ProductLibrary(session), Fernet(Fernet.generate_key()))
        value.initialize()
        yield value


def test_import_store_isolation_and_idempotency(orders):
    assert orders.import_postings([posting()], store_id="A")["imported"] == 1
    assert orders.import_postings([posting()], store_id="A")["updated"] == 1
    orders.import_postings([posting()], store_id="B")
    rows = orders.list_orders()["orders"]
    assert len(rows) == 2
    assert len({row["order_key"] for row in rows}) == 2
    assert all(row["match_status"] == "matched" for row in rows)
    assert all(row["items"][0]["matched_sku"] == "POD-000001-01" for row in rows)


def test_s2b_order_match_recovers_remote_id_from_matching_composition(tmp_path):
    with SessionLocal() as session:
        product = CatalogProduct(sku="S2B-004400-01", group_code="S2B-004400", name="桌布", source_type="pod", supplier="S2B")
        session.add(product); session.flush()
        session.add(CatalogVariant(product_id=product.id, sku="S2B-004400-01", supplier_sku_id="42270", properties_json=dumps({"size_id": 1119, "color_id": 6})))
        session.add(PodProductVersion(catalog_product_id=product.id, version_no=1, is_current=True, supplier="s2b", product_code="FYGWQD", recipe_json=dumps({"supplier": "s2b"})))
        session.add(PodComposition(catalog_product_id=product.id, supplier="s2b", status="cleared", product_code="FYGWQD", remote_product_id="36547879"))
        session.commit()
        manager = OrderManager(tmp_path, ProductLibrary(session), Fernet(Fernet.generate_key()))
        manager.initialize()
        result = manager._match_offer("XSZ-S2B-004400-01")
        assert result["match_status"] == "matched"
        assert result["remote_product_id"] == 36547879


def test_platform_images_update_only_matching_store_and_offer(orders):
    orders.import_postings([posting()], store_id="A")
    orders.import_postings([posting()], store_id="B")
    assert orders.platform_image_offers("A") == ["SHOP-POD-000001-01"]
    assert orders.update_platform_images("A", {"SHOP-POD-000001-01": "https://platform.example/image.jpg"}) == 1
    assert orders.list_orders(store_id="A")["orders"][0]["items"][0]["platform_image_url"] == "https://platform.example/image.jpg"
    assert orders.list_orders(store_id="B")["orders"][0]["items"][0]["platform_image_url"] == ""


def test_ozon_platform_image_sync_reads_product_card(orders, monkeypatch):
    import erp.domains.orders.service as service
    orders.import_postings([posting()], store_id="A")
    monkeypatch.setattr(service, "manager", lambda _session: orders)

    class FakeOzon:
        def __init__(self, _config): pass
        async def _post(self, path, payload):
            assert path == "/v3/product/info/list"
            assert payload["offer_id"] == ["SHOP-POD-000001-01"]
            return {"items": [{"offer_id": "SHOP-POD-000001-01", "primary_image": ["https://platform.example/current.jpg"]}]}
        async def aclose(self): pass

    monkeypatch.setattr(service, "OzonClient", FakeOzon)
    with SessionLocal() as session:
        result = sync_platform_images(session, {"platform": "ozon", "store_id": "A"})
    assert result["order_items_updated"] == 1
    assert orders.list_orders(store_id="A")["orders"][0]["items"][0]["platform_image_url"] == "https://platform.example/current.jpg"


def test_wb_platform_image_sync_reads_product_card(orders, monkeypatch):
    import erp.domains.orders.service as service
    orders.import_postings([{"id": 123, "article": "SHOP-POD-000001-01", "productName": "测试商品"}], platform="wb", store_id="A")
    monkeypatch.setattr(service, "manager", lambda _session: orders)

    class FakeWbContent:
        def __init__(self, _config): pass
        def find_card(self, offer):
            return {"vendorCode": offer, "photos": [{"big": "https://wb.example/current.jpg"}]}
        def close(self): pass

    monkeypatch.setattr(service, "_WbContentClient", FakeWbContent)
    with SessionLocal() as session:
        result = sync_platform_images(session, {"platform": "wb", "store_id": "A", "wildberries_token": "test"})
    assert result["order_items_updated"] == 1
    assert orders.list_orders(store_id="A")["orders"][0]["items"][0]["platform_image_url"] == "https://wb.example/current.jpg"


def test_order_list_uses_stable_server_pagination(orders):
    for number in ("PAGE-1", "PAGE-2", "PAGE-3"):
        orders.import_postings([posting(number)], store_id="A")
    first = orders.list_orders(limit=2, offset=0)
    second = orders.list_orders(limit=2, offset=2)
    assert first["total"] == second["total"] == 3
    assert len(first["orders"]) == 2 and len(second["orders"]) == 1
    assert {row["order_key"] for row in first["orders"]}.isdisjoint(
        {row["order_key"] for row in second["orders"]}
    )


def test_order_list_includes_product_image_and_filters_formal_tracking(orders, monkeypatch):
    orders.import_postings([posting("TRACKED"), posting("WAITING")], store_id="A")
    internal_keys = {
        row["posting_number"]: row["order_key"] for row in orders.list_orders()["orders"]
    }
    monkeypatch.setattr(
        orders.library,
        "get_product",
        lambda _sku: {
            "oss_assets": [
                {
                    "asset_type": "rendering",
                    "color_id": 1,
                    "oss_url": "https://oss.example.test/pod-main.jpg",
                }
            ]
        },
    )
    with closing(orders._connect()) as db:
        now = "2026-09-17T00:00:00+00:00"
        db.execute(
            """INSERT INTO batch_order_groups (
                id, batch_id, sku, product_name, product_code, stock_item_id,
                quantity, address_enc, submission_status, created_at, updated_at,
                tracking_number
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "TRACK-GROUP", "TRACK-BATCH", "POD-000001-01", "测试 POD", "CODE-1", 123,
                2, orders._encrypt_recipient({}), "submitted", now, now, "YT123456789",
            ),
        )
        db.execute(
            "INSERT INTO batch_order_sources (group_id, posting_number, quantity) VALUES (?, ?, ?)",
            ("TRACK-GROUP", internal_keys["TRACKED"], 2),
        )
        db.commit()

    ready = orders.list_orders(tracking_status="ready")["orders"]
    waiting = orders.list_orders(tracking_status="waiting")["orders"]
    assert [row["posting_number"] for row in ready] == ["TRACKED"]
    assert [row["posting_number"] for row in waiting] == ["WAITING"]
    assert ready[0]["items"][0]["image_url"] == "https://oss.example.test/pod-main.jpg"
    assert [row["posting_number"] for row in orders.list_orders(search="YT123456789")["orders"]] == ["TRACKED"]
    assert orders.list_orders(search="YT123456789", store_id="another-store")["total"] == 0


def test_order_search_matches_tms_tracking_number(orders):
    orders.import_postings([posting("TMS-TRACKED"), posting("TMS-OTHER")], store_id="A")
    with closing(orders._connect()) as db:
        db.execute(
            "UPDATE ozon_orders SET tms_tail_tracking_numbers=? WHERE external_order_id=?",
            (json.dumps(["TMS987654321"]), "TMS-TRACKED"),
        )
        db.commit()
    result = orders.list_orders(search="987654321")
    assert result["total"] == 1
    assert result["orders"][0]["posting_number"] == "TMS-TRACKED"


def test_post_purchase_processing_includes_ozon_and_wb(orders):
    orders.import_postings([posting("OZ-1")], platform="ozon", store_id="OZ")
    orders.import_postings(
        [{"id": 123, "article": "SHOP-POD-000001-01", "productName": "测试商品"}],
        platform="wb", store_id="WB",
    )
    with closing(orders._connect()) as db:
        db.execute("UPDATE ozon_orders SET submission_status='submitted'")
        db.commit()
    result = orders.list_orders(workflow_status="post_purchase_processing")
    assert result["total"] == 2
    assert {row["platform"] for row in result["orders"]} == {"ozon", "wb"}
    assert result["counts"]["post_purchase_processing"] == 2
    assert orders.list_orders(workflow_status="post_purchase_processing", platform="wb")["total"] == 1
    assert orders.list_orders(workflow_status="purchased_pending_tms")["total"] == 1


def test_wb_buyer_cancelled_order_is_not_a_formal_purchase(orders):
    orders.import_postings(
        [{"id": 5800735850, "article": "SHOP-POD-000001-01", "productName": "测试商品",
          "supplierStatus": "new", "wbStatus": "declined_by_client"}],
        platform="wb", store_id="WXZ",
    )
    row = orders.list_orders()["orders"][0]
    assert row["order_status"] == "new"
    assert row["substatus"] == "declined_by_client"
    assert row["workflow_status"] == "cancelled"
    assert orders.list_orders(workflow_status="pending_purchase")["total"] == 0
    assert orders.list_orders(workflow_status="cancelled")["total"] == 1
    with pytest.raises(OrderError, match="已被平台取消"):
        orders.preview_batch([row["order_key"]])

    with closing(orders._connect()) as db:
        db.execute("UPDATE ozon_orders SET submission_status='submitted' WHERE posting_number=?", (row["order_key"],))
        db.commit()
    purchased = orders.list_orders()["orders"][0]
    assert purchased["workflow_status"] == "cancelled_purchased"
    assert orders.list_orders(workflow_status="post_purchase_processing")["total"] == 0
    assert orders.counts()["cancelled_purchased"] == 1
    with pytest.raises(OrderError, match="已被平台取消"):
        orders.mark_orders_completed([row["order_key"]])


def test_manual_tracking_moves_unpurchased_order_to_post_purchase_processing(orders):
    orders.import_postings([posting("MANUAL-TRACK")], store_id="A")
    key = orders.list_orders()["orders"][0]["order_key"]
    result = orders.record_manual_tracking(key, "YT123456789", "圆通速递")
    assert result["submission_status"] == "not_submitted"
    assert result["workflow_status"] == "manual_tracking"
    assert result["tracking_number"] == "YT123456789"
    assert result["items"][0]["tracking_number"] == "YT123456789"
    assert orders.list_orders(workflow_status="pending_purchase")["total"] == 0
    assert orders.list_orders(workflow_status="post_purchase_processing")["total"] == 1
    assert orders.list_orders(tracking_status="ready", search="YT123456789")["total"] == 1
    assert orders.counts()["post_purchase_processing"] == 1
    export_path, export_count = orders.export_tms_tracking([key])
    assert export_count == 1
    assert load_workbook(export_path).active[3][3].value == "YT123456789"
    with pytest.raises(OrderError, match="不能重复创建"):
        orders.preview_batch([key])
    orders.record_manual_tracking(key, "YT987654321", "圆通速递")
    assert orders.get_order(key)["tracking_number"] == "YT987654321"
    assert orders.mark_orders_completed([key]) == {"completed": 1}
    assert orders.list_orders(workflow_status="post_purchase_processing")["total"] == 0


def test_cancelled_order_cannot_receive_manual_tracking(orders):
    orders.import_postings([{
        "id": 123, "article": "SHOP-POD-000001-01", "productName": "测试商品",
        "supplierStatus": "new", "wbStatus": "declined_by_client",
    }], platform="wb", store_id="WXZ")
    key = orders.list_orders()["orders"][0]["order_key"]
    with pytest.raises(OrderError, match="平台已取消"):
        orders.record_manual_tracking(key, "YT123456789", "圆通速递")


def test_encrypted_recipient_and_batch_preview(orders):
    orders.import_postings([posting()], store_id="A")
    key = orders.list_orders()["orders"][0]["order_key"]
    address = {name: "test-private-address" for name in orders.REQUIRED_ADDRESS_FIELDS}
    assert orders.update_recipient(key, address)["recipient_ready"]
    with closing(orders._connect()) as db:
        encrypted = db.execute("SELECT recipient_enc FROM ozon_orders").fetchone()[0]
        assert b"test-private-address" not in encrypted
    assert "recipient" not in orders.list_orders()["orders"][0]
    preview = orders.preview_batch([key], "smart")
    assert preview["total_quantity"] == 2
    assert orders.get_order(key)["submission_status"] == "not_submitted"
    with pytest.raises(OrderError):
        orders.manual_match(key, 1, "MISSING")


def test_legacy_worker_cannot_replay_purchases(orders):
    queue = HicustomOrderQueue(orders, lambda: {}, lambda: None)
    with pytest.raises(OrderError, match="旧采购 Worker 已禁用"):
        asyncio.run(queue.start())


def test_bound_store_resync_preserves_original_order_identity(orders):
    orders.import_postings([posting()], store_id='legacy-store')
    original = orders.list_orders()['orders'][0]['order_key']
    with closing(orders._connect()) as db:
        db.execute("UPDATE ozon_orders SET store_id='erp-store'"); db.commit()
    erp = ErpOrderManager(orders.root, orders.library, orders.cipher)
    erp.import_postings([posting()], store_id='erp-store')
    assert erp.list_orders()['counts']['total'] == 1
    assert erp.list_orders()['orders'][0]['order_key'] == original


def test_resync_and_rematch_do_not_change_purchased_production_mapping(orders):
    orders.import_postings([posting()], store_id='A')
    key = orders.list_orders()['orders'][0]['order_key']
    orders.update_submission(key, submission_status='unknown')
    changed = posting(); changed['products'][0]['quantity'] = 999
    orders.import_postings([changed], store_id='A')
    assert orders.get_order(key)['items'][0]['quantity'] == 2
    assert orders.rematch() == {'matched': 0, 'unmatched': 0}
    with pytest.raises(OrderError):
        orders.manual_match(key, 1, 'POD-000001-01')


def test_lightweight_list_retains_status_without_finance_payload(orders):
    orders.import_postings([posting()], store_id='A')
    erp = ErpOrderManager(orders.root, orders.library, orders.cipher)
    row = erp.list_orders()['orders'][0]
    assert row['match_status'] == 'matched'
    assert 'profit' not in row
    assert 'financial_data_json' not in row
    assert 'recipient' in erp.get_order(row['order_key'])


def test_supplier_timeout_is_marked_unknown_and_not_silently_retried():
    class Fake:
        def create_order(self, _payload):
            raise AppError('timeout', code='pod_write_outcome_unknown', details={'outcome_unknown': True})
    with pytest.raises(OrderError) as caught:
        asyncio.run(AsyncSupplier(Fake()).create_order({'out_order_id': 'stable-id'}))
    assert caught.value.outcome_unknown is True


@pytest.mark.parametrize(("value", "expected"), [
    ("TV3GZA", ""), ("YT1234", ""), (" yt7624881916623 ", "YT7624881916623"),
    ("YT12345", "YT12345"), ("", ""),
])
def test_s2b_uses_only_formal_yto_tracking_numbers(value, expected):
    assert official_supplier_tracking_number("s2b", value) == expected


def test_hicustom_tracking_rule_is_unchanged():
    assert official_supplier_tracking_number("hicustom", " HC-TRACK-1 ") == "HC-TRACK-1"


@pytest.mark.parametrize("value", ["中国", "中华人民共和国", "China", "cn", "CN"])
def test_s2b_domestic_country_uses_cn_code(value):
    assert s2b_country_code(value) == "CN"


def test_empty_supplier_gallery_reuploads_sources_and_rebuilds_product(tmp_path):
    source = tmp_path / "original.png"
    source.write_bytes(b"local-print-source")

    class Library:
        def get_product(self, _sku):
            return {
                "sku": "HC-000001-01", "product_name": "测试产品", "supplier": "hicustom",
                "output_folder": str(tmp_path), "oss_assets": [],
                "recipe": {"supplier": "hicustom", "product_type_id": 11912,
                           "default_color_id": 29, "default_view_id": 1,
                           "sides": [{"view_id": 1, "source_path": str(source),
                                      "source_filename": "original.png", "mime_type": "image/png",
                                      "width": 100, "height": 120, "top_x": 0, "top_y": 0}]},
            }

    class Orders:
        library = Library()
        group = {"id": "group-1", "batch_id": "batch-1", "sku": "HC-000001-01",
                 "product_revision": 0, "product_name": "测试产品"}
        def get_batch_group(self, _): return dict(self.group)
        def reusable_hicustom_product(self, *_args, **_kwargs): return None
        def update_batch_group(self, _group_id, **values): self.group.update(values); return dict(self.group)

    class Supplier:
        uploaded = []
        async def upload_gallery(self, **value): self.uploaded.append(value); return {"code": "NEW-GALLERY-1"}
        async def create_product(self, value):
            assert value["product_type_id"] == 11912
            assert value["default_color_id"] == 29 and value["default_view_id"] == 1
            assert value["sides"][0]["gallery_code"] == "NEW-GALLERY-1"
            return {"code": "NEW-PRODUCT-1"}

    orders = Orders(); supplier = Supplier()
    queue = HicustomOrderQueue(orders, lambda: {}, lambda: supplier)
    rebuilt = asyncio.run(queue._prepare_batch_product("group-1", supplier, {}))
    assert supplier.uploaded[0]["content"] == b"local-print-source"
    assert rebuilt["fulfillment_product_code"] == "NEW-PRODUCT-1"
    assert json.loads(rebuilt["fulfillment_gallery_codes"]) == ["NEW-GALLERY-1"]


def test_hicustom_product_accepts_native_nested_placement(monkeypatch):
    client = object.__new__(HicustomClient)
    monkeypatch.setattr(client, "access_token", lambda: "token")
    captured = {}
    def request(_method, _path, **kwargs):
        captured.update(kwargs); return {"data": {"code": "PRODUCT-1"}}
    monkeypatch.setattr(client, "_request", request)
    result = client.create_product({
        "external_id": "test", "product_type_id": 1, "default_color_id": 2, "default_view_id": 3,
        "sides": [{"view_id": 3, "gallery_code": "G-1", "placement": {"width": 100, "height": 200, "top_x": 1.5, "top_y": -2}}],
    })
    cfg = json.loads(captured["data"]["cfgs"])
    assert result["code"] == "PRODUCT-1"
    assert cfg == [{"view_id": 3, "gallery_code": "G-1", "width": 100, "height": 200, "top_x": 1.5, "top_y": -2.0}]


def test_s2b_reuses_existing_remote_product_without_uploading_or_composing():
    class Library:
        def get_product(self, _sku):
            raise AssertionError("有效的历史 S2B 产品不应重新读取原图")

    class Orders:
        library = Library()
        group = {
            "id": "new-group", "batch_id": "new-batch", "sku": "S2B-000985-01",
            "stock_item_id": 42270, "quantity": 1, "address": {"country": "中国"},
            "logistics_id": 109, "logistics_company_name": "圆通",
            "quoted_shipping_amount": "5.00", "product_revision": 0,
        }
        historical = {
            "id": "old-group", "remote_product_id": 36400243,
            "fulfillment_product_code": "DW52XV",
            "fulfillment_gallery_codes": '["23583908"]', "product_revision": 1,
        }
        def get_batch_group(self, _): return dict(self.group)
        def reusable_s2b_product(self, sku, *, exclude_group_id=""):
            assert sku == "S2B-000985-01" and exclude_group_id == "new-group"
            return dict(self.historical)
        def update_batch_group(self, _group_id, **values):
            self.group.update(values); return dict(self.group)

    class Supplier:
        uploaded = False
        composed = False
        async def custom_product(self, product_id):
            assert product_id == 36400243
            return {"id": product_id, "product_code": "DW52XV"}
        async def calculate_products(self, **kwargs):
            assert kwargs["products"][0]["product_id"] == 36400243
            return [{"logistics_id": 109, "name": "圆通", "amount": "5.00"}]
        async def upload_gallery(self, **_kwargs):
            self.uploaded = True; raise AssertionError("不应重新上传 S2B 素材")
        async def create_product(self, _value):
            self.composed = True; raise AssertionError("不应重新创建 S2B 产品")

    orders = Orders(); supplier = Supplier()
    queue = HicustomOrderQueue(orders, lambda: {}, lambda: supplier)
    prepared = asyncio.run(queue._prepare_s2b_batch_product("new-group", supplier, {}))
    assert prepared["remote_product_id"] == 36400243
    assert prepared["fulfillment_product_code"] == "DW52XV"
    assert json.loads(prepared["fulfillment_gallery_codes"]) == ["23583908"]
    assert not supplier.uploaded and not supplier.composed


def test_s2b_resume_reuses_prepared_product_and_selected_yto():
    class Orders:
        group = {
            "id": "group-1", "supplier": "s2b", "purchase_id": "purchase-1",
            "fulfillment_product_code": "READY-CODE", "remote_product_id": 12345,
            "out_order_id": "BATCH-STABLE-R1", "logistics_id": 109,
            "logistics_company_name": "圆通", "submission_status": "failed",
        }
        def get_batch_group(self, _): return dict(self.group)
        def assert_purchase_sources_active(self, _group_ids): return None
        def update_batch_group(self, _group_id, **values): self.group.update(values); return dict(self.group)
        def s2b_batch_payload(self, _group_id, _store_id): return {"third_order_id": self.group["out_order_id"], "logistics_id": self.group["logistics_id"]}
        def record_batch_costs(self, _group_id, _created): return None

    class Supplier:
        async def order_by_out_order_id(self, _): return None
        async def create_order(self, payload):
            assert payload["logistics_id"] == 109
            return {"id": 7788, "order_id": "7788"}

    orders = Orders(); supplier = Supplier()
    async def load_supplier(*_): return supplier
    queue = HicustomOrderQueue(orders, lambda: {"s2b_store_id": 89831}, load_supplier)
    asyncio.run(queue.process_batch("group-1"))
    assert orders.group["submission_status"] == "submitted"
    assert orders.group["hicustom_order_id"] == "7788"


def test_real_write_routes_require_explicit_confirmation(client):
    assert client.post('/api/v1/orders/pod/purchase/submit', json={'target_type':'group','target_id':'x','confirmation':'NO'}).status_code == 422
    assert client.post('/api/v1/orders/pod/x/tms/submit', json={'confirmation':'NO'}).status_code == 422


@pytest.mark.parametrize(("method", "path", "payload"), [
    ("post", "/api/v1/orders/pod/tms/batch-submit", {
        "order_keys": ["x"], "confirmation": "CONFIRM_REAL_TMS", "idempotency_key": "retired-batch",
    }),
    ("get", "/api/v1/orders/pod/x/tms/preview", None),
    ("post", "/api/v1/orders/pod/x/tms/submit", {
        "confirmation": "CONFIRM_REAL_TMS", "idempotency_key": "retired-single",
    }),
])
def test_tms_routes_are_retired(client, method, path, payload):
    response = getattr(client, method)(path, json=payload) if payload is not None else getattr(client, method)(path)
    assert response.status_code == 410
    assert response.json()["code"] == "pod_tms_retired"


def test_saved_purchase_addresses_are_encrypted_and_manageable(client):
    payload = {
        "name": "义乌测试仓", "is_default": True,
        "address": {"country": "中国", "region": "浙江省", "city": "义乌市", "area": "稠城",
                    "street": "测试私密街道 88 号", "postcode": "322000", "firstname": "三",
                    "lastname": "张", "mobile_phone": "13800000000", "telephone": "", "email": ""},
    }
    created = client.post('/api/v1/orders/pod/addresses', json=payload)
    assert created.status_code == 200, created.text
    item = created.json()["items"][0]
    assert item["is_default"] and item["address"]["street"] == "测试私密街道 88 号"
    with SessionLocal() as session:
        encrypted = session.query(IntegrationConnection).filter_by(provider="procurement").one().secrets_encrypted
        assert "测试私密街道" not in encrypted and "13800000000" not in encrypted
    assert client.get('/api/v1/orders/pod/addresses').json()["items"][0]["id"] == item["id"]
    deleted = client.delete(f'/api/v1/orders/pod/addresses/{item["id"]}')
    assert deleted.status_code == 200 and deleted.json()["items"] == []


def test_purchase_address_full_name_without_surname_roundtrips(client):
    address = {'country': 'CN', 'region': '浙江省', 'city': '金华市', 'area': '义乌市',
               'street': '测试仓地址', 'postcode': '322014', 'firstname': '测试仓收件人',
               'lastname': '', 'mobile_phone': '13800000000'}
    response = client.post('/api/v1/orders/pod/addresses', json={'name': '测试仓', 'address': address})
    assert response.status_code == 200, response.text
    item = response.json()['items'][0]
    assert item['address']['firstname'] == '测试仓收件人'
    assert item['address']['lastname'] == ''
    assert client.get('/api/v1/orders/pod/addresses').json()['items'][0] == item
    address['firstname'] = ' '
    address['mobile_phone'] = ''
    rejected = client.post('/api/v1/orders/pod/addresses', json={'name': '测试仓', 'address': address})
    assert rejected.status_code == 400
    assert '名字（收件人）' in rejected.json()['message']
    assert '手机' in rejected.json()['message']
    assert client.get('/api/v1/orders/pod/addresses').json()['items'] == [item]


def test_migrated_purchase_addresses_remain_visible_and_are_upgraded(client):
    legacy = {
        "id": "legacy-address", "name": "旧仓库", "is_default": True,
        "address": {"country": "中国", "region": "浙江省", "city": "义乌市", "area": "",
                    "street": "旧地址", "postcode": "322000", "firstname": "三", "lastname": "张",
                    "mobile_phone": "13800000000", "telephone": "", "email": ""},
    }
    with SessionLocal() as session:
        session.add(IntegrationConnection(
            provider="procurement", name="采购与收货资料", enabled=True,
            secrets_encrypted=vault.encrypt({"addresses": [legacy]}),
        ))
        session.commit()

    assert client.get('/api/v1/orders/pod/addresses').json()["items"][0]["id"] == "legacy-address"
    legacy["name"] = "新仓库"
    response = client.post('/api/v1/orders/pod/addresses', json=legacy)
    assert response.status_code == 200
    with SessionLocal() as session:
        row = session.query(IntegrationConnection).filter_by(provider="procurement").one()
        values = vault.decrypt(row.secrets_encrypted)
        assert "addresses" not in values
        assert values["saved_addresses"][0]["name"] == "新仓库"


def test_api_routes_and_no_purchase_write_route(client, orders, monkeypatch):
    monkeypatch.setattr(order_api, "manager", lambda session: orders)
    assert client.get('/api/v1/orders/pod').status_code == 200
    store = client.post('/api/v1/stores', json={"platform": "ozon", "name": "订单测试店", "external_account_id": "123", "secrets": {"api_key": "fake"}}).json()
    response = client.post('/api/v1/orders/pod/import', json={"store_id": store['id'], "postings": [posting()]})
    assert response.status_code == 200, response.text
    rows = client.get('/api/v1/orders/pod').json()['orders']
    assert rows[0]['match_status'] == 'matched'
    assert client.get('/api/v1/orders/pod?store_id=undefined').json()['total'] == len(rows)
    assert client.get('/api/v1/orders/pod?store_id=null').json()['total'] == len(rows)
    assert client.post('/api/v1/orders/pod/batch/queue', json={}).status_code == 422
    assert client.post('/api/v1/orders/pod/purchase/submit', json={"target_type":"group","target_id":"x","confirmation":"wrong"}).status_code == 422
    request = {"store_id": store['id'], "idempotency_key": "same-sync"}
    first = client.post('/api/v1/orders/pod/sync', json=request)
    assert first.status_code == 200, first.text
    assert client.post('/api/v1/orders/pod/sync', json=request).json() == first.json()


def test_snapshot_migration_reencrypts_and_preserves_source(tmp_path):
    source = tmp_path / 'legacy'; root = source / 'data'; root.mkdir(parents=True)
    old_key = Fernet.generate_key(); (root / 'master.key').write_bytes(old_key)
    old = OrderManager(root, None, Fernet(old_key)); old.initialize()
    # Empty-item posting avoids needing a product library in this isolated historical fixture.
    old.import_postings([{"posting_number": "LEGACY-1", "products": []}])
    key = old.list_orders()['orders'][0]['order_key']
    old.update_recipient(key, {"firstname": "private-name"})
    with closing(old._connect()) as db:
        before = db.execute('SELECT recipient_enc FROM ozon_orders').fetchone()[0]
    target = tmp_path / 'erp-orders'; cipher = Fernet(Fernet.generate_key())
    assert migrate(source, target)['counts']['ozon_orders'] == 1
    assert not target.exists()
    report = migrate(source, target, execute=True, target_cipher=cipher)
    assert report['counts'] == report['verified_counts']
    copied = OrderManager(target, None, cipher)
    assert copied.get_order(key)['recipient']['firstname'] == 'private-name'
    with closing(old._connect()) as db:
        assert db.execute('SELECT recipient_enc FROM ozon_orders').fetchone()[0] == before
    assert migrate(source, target, execute=True, target_cipher=cipher)['already_imported']
    with pytest.raises(ValueError):
        migrate(source, root, execute=True, target_cipher=cipher)
