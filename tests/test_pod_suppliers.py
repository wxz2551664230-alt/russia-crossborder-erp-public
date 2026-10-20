from __future__ import annotations

from unittest.mock import patch
import httpx
import pytest

from erp.core.database import SessionLocal
from erp.core.security import vault
from erp.domains.pod.models import PodRemoteGalleryAsset
from erp.models import IntegrationConnection
from erp.services.jobs import execute_job, get_job
from erp.domains.pod.integrations.s2b import S2BClient
from erp.domains.pod.integrations.hicustom import HicustomClient
from erp.domains.pod.integrations.common import SupplierConnectionConfig
from erp.core.errors import AppError


def test_hicustom_missing_delete_route_is_an_error_not_a_success():
    connection = SupplierConnectionConfig(provider="hicustom", connection_id="isolated",
        base_url="https://example.invalid", config={}, secrets={"app_key": "key", "app_secret": "secret"})
    def respond(request):
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "test-token"})
        return httpx.Response(404, text="<html>Not Found</html>", headers={"content-type": "text/html"})
    with HicustomClient(connection, transport=httpx.MockTransport(respond)) as supplier:
        with pytest.raises(AppError) as caught:
            supplier.delete_gallery("TEST01")
    assert caught.value.code == "hicustom_api_error"
    assert caught.value.details["http_status"] == 404


class FakeGalleryClient:
    def __init__(self, *, exists: bool = True) -> None:
        self.exists = exists
        self.deleted: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def gallery_exists(self, code: str) -> bool:
        return self.exists

    def delete_gallery(self, code: str) -> None:
        self.deleted.append(code)


def test_native_gallery_cleanup_is_protected_and_recoverable(client):
    with SessionLocal() as session:
        session.add_all([
            PodRemoteGalleryAsset(supplier="hicustom",gallery_code="PENDING",usage="pending",protected=False,cleanup_candidate=True),
            PodRemoteGalleryAsset(supplier="hicustom",gallery_code="ORDERED",usage="ordered",protected=True,cleanup_candidate=False),
            PodRemoteGalleryAsset(supplier="s2b",gallery_code="88",usage="pending",protected=False,cleanup_candidate=True),
            IntegrationConnection(
                provider="hicustom",name="HICUSTOM",base_url="https://example.invalid",
                secrets_encrypted=vault.encrypt({"app_key":"key","app_secret":"secret"}),enabled=True,
            ),
        ])
        session.commit()
    listing=client.get("/api/v1/pod/supplier-gallery?usage=&include_deleted=false").json()
    assert listing["counts"]=={"total":3,"pending":2,"ordered":1,"cleanup_candidates":2,"protected":1}
    assert client.delete("/api/v1/pod/supplier-gallery/hicustom/ORDERED").status_code==409
    assert client.delete("/api/v1/pod/supplier-gallery/s2b/88").status_code==409
    marked=client.post("/api/v1/pod/supplier-gallery/s2b/88/mark-deleted")
    assert marked.status_code==200 and marked.json()["local_preserved"] is True

    queued=client.delete("/api/v1/pod/supplier-gallery/hicustom/PENDING").json()["job"]
    fake=FakeGalleryClient()
    with patch("erp.domains.pod.gallery.supplier_client",return_value=fake):
        with SessionLocal() as session:
            execute_job(session,get_job(session,queued["id"]))
            job=get_job(session,queued["id"])
            item=session.query(PodRemoteGalleryAsset).filter_by(gallery_code="PENDING").one()
            assert job.status=="completed"
            assert item.remote_deleted is True
    assert fake.deleted==["PENDING"]


class FakeCatalogClient:
    def __init__(self, provider: str) -> None:
        self.provider=provider

    def __enter__(self): return self
    def __exit__(self,*_): return None
    def product_categories(self): return [{"id":1,"name":f"{self.provider}-category"}]
    def product_types(self,page:int,page_size:int=100):
        return {"items":[{"id":1,"cn_name":f"{self.provider}-product"}],"total":1} if page==1 else {"items":[],"total":1}
    def product_type(self,product_id:int): return {"id":product_id,"cn_name":f"{self.provider}-detail","detail_img":[f"https://example.invalid/{self.provider}.jpg"]}
    def preview_url(self,detail): return detail["detail_img"][0]


def test_native_supplier_catalog_sync_uses_both_adapters(client):
    with patch("erp.domains.pod.supplier_catalog.supplier_client",side_effect=lambda session,provider,*args,**kwargs:FakeCatalogClient(provider)):
        queued=client.post("/api/v1/pod/catalog/sync").json()["job"]
        with SessionLocal() as session:
            execute_job(session,get_job(session,queued["id"]))
            assert get_job(session,queued["id"]).status=="completed"
        catalog=client.get("/api/v1/pod/catalog").json()
        assert catalog["product_count"]==2
        assert catalog["provider_counts"]=={"hicustom":1,"s2b":1}
        assert client.get("/api/v1/pod/product-types/hicustom:1").json()["cn_name"]=="hicustom-detail"


def test_s2b_detail_keeps_variant_price_package_attributes_and_production_facts():
    detail = S2BClient.normalize_detail({
        "id": 4839, "name": "S2B 毛毯", "produce_area": 1,
        "product_material_text": "聚酯纤维", "en_product_material_text": "Polyester",
        "product_technology_text": "热转印", "desc": "可水洗产品说明",
        "colors": [{"id": 6, "name": "白色"}], "sizes": [{"id": 1119, "name": "140×180cm"}],
        "views": [{"id": 1, "name": "A面"}],
        "print_areas": [{"view_id": 1, "width": 575, "height": 431}],
        "items": [{"id": 33308, "code": "OM4BGU", "size_id": 1119, "color_id": 6,
                   "price": "17.70", "weight": 324, "length": "19", "width": "16", "height": "5"}],
        "attr_values": [{"size_id": 1119, "attr_value": [{"attr_id": 8, "attr_name": "长度", "attr_value": "180cm"}]}],
    })
    assert detail["prices"]["wholesale_price"][0]["retail_price"]["price"] == "17.70"
    assert detail["size_attr_info"] == [{"size_id": 1119, "attr_id": 8, "attr_value": "180cm"}]
    assert detail["product_description"]["product_material"]["cn_name"] == "聚酯纤维"
    assert detail["product_description"]["product_technology"] == "热转印"
    assert detail["product_description"]["print_areas"][0]["name"] == "A面"
