from __future__ import annotations

import io
from datetime import datetime
from unittest.mock import patch

import httpx
from PIL import Image
from sqlalchemy import select

from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.domains.pod.models import PodArtwork, PodComposition, PodCompositionSide, PodRemoteGalleryAsset, PodSupplierCatalogSnapshot
from erp.domains.pod.recovery import retry_deleted_s2b_product, retry_failed_composition
from erp.models import CatalogImportRun, CatalogProduct, IntegrationConnection, Job
from erp.services.jobs import HANDLERS, execute_job, get_job
from erp.core.errors import AppError


def test_pod_catalog_and_artwork_are_exposed_through_erp(client):
    with SessionLocal() as session:
        session.add(PodSupplierCatalogSnapshot(
            provider="all",is_current=True,product_count=1,category_count=0,
            fetched_at="2026-09-09T00:00:00+00:00",
            payload_json=dumps({"synced_at":"2026-09-09T00:00:00+00:00","products":[{"id":1,"supplier":"hicustom","cn_name":"T恤"}],"categories":[]}),
        ))
        session.commit()
    category=client.post("/api/v1/pod/artworks/categories",json={"name":"图案"}).json()["category"]
    image=io.BytesIO();Image.new("RGBA",(12,8),(255,0,0,120)).save(image,"PNG")
    uploaded=client.post(
        "/api/v1/pod/artworks",
        files={"image":("artwork.png",image.getvalue(),"image/png")},
        data={"category_id":category["id"]},
    ).json()
    assert client.get("/api/v1/pod/catalog").json()["products"][0]["cn_name"]=="T恤"
    artwork=client.get("/api/v1/pod/artworks").json()["items"][0]
    assert artwork["id"]==uploaded["id"]
    assert artwork["thumbnail_url"]==f"/api/v1/pod/artworks/{uploaded['id']}/thumbnail"
    assert client.get(artwork["thumbnail_url"]).status_code==200
    assert client.get(artwork["file_url"]).content==image.getvalue()


def test_pod_artwork_batch_upload_is_persisted_then_processed_in_background(client):
    first=io.BytesIO();Image.new("RGB",(31,31),"red").save(first,"PNG")
    second=io.BytesIO();Image.new("RGB",(32,32),"blue").save(second,"JPEG")
    files=[
        ("images",("red.png",first.getvalue(),"image/png")),
        ("images",("red-copy.png",first.getvalue(),"image/png")),
        ("images",("blue.jpg",second.getvalue(),"image/jpeg")),
        ("images",("broken.png",b"not-an-image","image/png")),
    ]
    response=client.post("/api/v1/pod/artworks/batch",files=files,data={"category_id":"uncategorized"})
    assert response.status_code==202,response.text
    result=response.json()
    assert (result["total"],result["accepted"],result["failed"])==(4,4,0)
    assert [row["status"] for row in result["results"]]==["queued"]*4
    with SessionLocal() as session:
        jobs=[session.get(Job,row["job_id"]) for row in result["results"]]
        assert all(job.status=="queued" and job.max_attempts==3 for job in jobs)
        for job in jobs[:3]:
            job.attempts=1;execute_job(session,job)
        jobs[3].attempts=1;execute_job(session,jobs[3])
        assert jobs[3].status=="retrying"
    assert client.get("/api/v1/pod/artworks").json()["total"]==2


def test_pod_artwork_batch_upload_limits_file_count(client):
    tiny=io.BytesIO();Image.new("RGB",(1,1),"white").save(tiny,"PNG")
    files=[("images",(f"{index}.png",tiny.getvalue(),"image/png")) for index in range(101)]
    response=client.post("/api/v1/pod/artworks/batch",files=files,data={"category_id":"uncategorized"})
    assert response.status_code==413
    assert response.json()["code"]=="pod_artwork_batch_too_large"


def test_pod_artwork_library_filters_by_upload_date(client):
    image=io.BytesIO();Image.new("RGB",(8,8),"green").save(image,"PNG")
    created=client.post("/api/v1/pod/artworks",files={"image":("dated.png",image.getvalue(),"image/png")}).json()
    with SessionLocal() as session:
        artwork=session.get(PodArtwork,created["id"])
        artwork.created_at=datetime(2026,9,8,12,0,0);session.commit()
    included=client.get("/api/v1/pod/artworks?uploaded_from=2026-09-08&uploaded_to=2026-09-08").json()
    excluded=client.get("/api/v1/pod/artworks?uploaded_from=2026-09-09&uploaded_to=2026-09-10").json()
    assert included["total"]==1 and included["items"][0]["uploaded_at"].startswith("2026-09-08")
    assert excluded["total"]==0


def test_pod_composition_uses_native_recoverable_job_and_syncs_catalog(client):
    with SessionLocal() as session:
        session.add(IntegrationConnection(provider="hicustom",name="HICUSTOM",base_url="https://supplier.invalid",secrets_encrypted=vault.encrypt({"app_key":"key","app_secret":"secret"}),enabled=True))
        session.commit()
    image=io.BytesIO();Image.new("RGB",(12,8),(255,0,0)).save(image,"PNG")
    artwork=client.post("/api/v1/pod/artworks",files={"image":("art.png",image.getvalue(),"image/png")},data={"category_id":"uncategorized"}).json()
    detail={"supplier":"hicustom","colors":[{"id":29,"cn_name":"黑色"}],"sizes":[{"id":7,"cn_name":"M"}],"stock_info":[{"id":701,"color_id":29,"size_id":7,"code":"BLACK-M","length":14,"width":12.5,"height":5,"weight":321}],"prices":{"price":31.92,"membership_price":"gold","wholesale_price":[{"stock_info":[{"id":701,"color_id":29,"size_id":7}],"retail_price":{"price":"34.53"},"gold":{"price":"31.92"}}]},"product_description":{"product_material":{"cn_name":"聚酯纤维"},"product_features":[{"title":"使用场景","value":"餐桌与派对"}]}}
    payload={"supplier":"hicustom","product_type_id":10000,"product_name":"T恤","default_color_id":29,"default_view_id":1,"selected_size_ids":[7],"placements":[{"view_id":1,"view_name":"正面","width":1000,"height":1200,"top_x":0,"top_y":0,"artwork_id":artwork["id"],"rotation":90}]}
    with patch("erp.domains.pod.api.native_product_detail",return_value=detail):
        response=client.post("/api/v1/pod/compositions",json=payload)
        duplicate_batch=client.post("/api/v1/pod/compositions/batch",json={**payload,"artwork_ids":[artwork["id"]],"prevent_duplicate":True})
    assert response.status_code==202,response.text
    assert duplicate_batch.status_code==409
    job_id=response.json()["job"]["id"]
    used=client.get("/api/v1/pod/artworks?product_key=hicustom:10000&usage=used_selected").json()
    available=client.get("/api/v1/pod/artworks?product_key=hicustom:10000&usage=available").json()
    other=client.get("/api/v1/pod/artworks?product_key=hicustom:10001&usage=available").json()
    history=client.get(f"/api/v1/pod/artworks/{artwork['id']}/usages").json()
    assert used["total"]==1 and used["items"][0]["used_by_selected"] is True
    assert available["total"]==0 and other["total"]==1
    assert history["total"]==1 and history["items"][0]["product_key"]=="hicustom:10000"

    class FakeSupplier:
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def upload_gallery(self,**_): return {"code":"gallery-1"}
        def create_product(self,_): return {"id":"remote-1","code":"HC-PRODUCT-1","colors":[{"id":29,"cn_name":"黑色","renderings":["https://example.test/effect.png"]}]}
        def custom_product(self,_): return {}
        @staticmethod
        def rendering_groups(product,default_color_id=0):
            return [{"color_id":29,"color_name":"黑色","rendering_urls":["https://example.test/effect.png"],"saved_files":[]}] if product else []
        def download_rendering(self,_): return image.getvalue(),"image/png"

    with patch("erp.domains.pod.composition.supplier_client",return_value=FakeSupplier()):
        with SessionLocal() as session:
            execute_job(session,get_job(session,job_id));job=get_job(session,job_id)
            assert job.status=="completed",(job.error_code,job.error_message)
            assert all(step.status=="completed" for step in job.steps)
            from erp.models import CatalogProduct
            product=session.query(CatalogProduct).filter(CatalogProduct.source_type=="pod").one()
            assert product.sku=="HC-000001-01"
            assert product.group_code=="HC-000001"
            assert product.supplier_product_id=="10000" and len(product.variants)==1
            assert product.variants[0].sku=="HC-000001-01"
            assert product.purchase_price_cny==31.92
            assert (product.package_length_mm,product.package_width_mm,product.package_height_mm,product.package_weight_g)==(140,125,50,321)
            assert product.variants[0].specification=="BLACK-M"
            assert "product_description" in product.source_snapshot_json
            from erp.domains.pod.models import PodCompositionSide
            side=session.query(PodCompositionSide).one()
            assert (side.asset.width,side.asset.height)==(8,12)


def test_pod_default_view_must_have_artwork(client):
    payload={"supplier":"hicustom","product_type_id":1,"product_name":"T恤","default_color_id":1,"default_view_id":2,"selected_size_ids":[1],"placements":[{"view_id":1,"width":1,"height":1,"top_x":0,"top_y":0,"artwork_id":"art-1","rotation":0}]}
    assert client.post("/api/v1/pod/compositions",json=payload).status_code==409


def test_unknown_supplier_write_pauses_until_gallery_is_bound(client):
    with SessionLocal() as session:
        session.add(IntegrationConnection(provider="hicustom",name="HICUSTOM",base_url="https://supplier.invalid",secrets_encrypted=vault.encrypt({"app_key":"key","app_secret":"secret"}),enabled=True));session.commit()
    image=io.BytesIO();Image.new("RGB",(8,8),(10,20,30)).save(image,"PNG")
    artwork=client.post("/api/v1/pod/artworks",files={"image":("art.png",image.getvalue(),"image/png")},data={"category_id":"uncategorized"}).json()
    detail={"supplier":"hicustom","colors":[{"id":1}],"sizes":[{"id":2}],"stock_info":[{"id":3,"color_id":1,"size_id":2}]}
    payload={"supplier":"hicustom","product_type_id":9,"product_name":"暂停测试","default_color_id":1,"default_view_id":1,"selected_size_ids":[2],"placements":[{"view_id":1,"width":100,"height":100,"top_x":0,"top_y":0,"artwork_id":artwork["id"],"rotation":0}]}
    with patch("erp.domains.pod.api.native_product_detail",return_value=detail):
        created=client.post("/api/v1/pod/compositions",json=payload).json()
    composition_id=created["composition"]["id"];job_id=created["job"]["id"]

    class UnknownSupplier:
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def upload_gallery(self,**_): raise AppError("结果未知",code="pod_write_outcome_unknown",details={"outcome_unknown":True,"stage":"gallery"})

    with patch("erp.domains.pod.composition.supplier_client",return_value=UnknownSupplier()):
        with SessionLocal() as session:
            execute_job(session,get_job(session,job_id))
            assert get_job(session,job_id).status=="paused"
            from erp.domains.pod.models import PodComposition
            assert session.get(PodComposition,composition_id).outcome_unknown is True
    resumed=client.post(f"/api/v1/pod/compositions/{composition_id}/bind-gallery",json={"view_id":1,"gallery_code":"confirmed-gallery"})
    assert resumed.status_code==200
    assert resumed.json()["job"]["status"]=="queued"


def test_pod_artwork_and_supplier_gallery_management_are_not_read_only(client):
    source=client.post("/api/v1/pod/artworks/categories",json={"name":"来源"}).json()["category"]
    target=client.post("/api/v1/pod/artworks/categories",json={"name":"毛毯"}).json()["category"]
    renamed=client.patch(f"/api/v1/pod/artworks/categories/{target['id']}",json={"name":"家居毛毯"})
    assert renamed.status_code==200
    assert renamed.json()["category"]["name"]=="家居毛毯"
    duplicate=client.patch(f"/api/v1/pod/artworks/categories/{target['id']}",json={"name":"来源"})
    assert duplicate.status_code==409
    artwork_ids=[]
    for color in ((255,0,0),(0,0,255)):
        image=io.BytesIO();Image.new("RGB",(10,10),color).save(image,"PNG")
        response=client.post(
            "/api/v1/pod/artworks",
            files={"image":("artwork.png",image.getvalue(),"image/png")},
            data={"category_id":source["id"]},
        )
        assert response.status_code==201
        artwork_ids.append(response.json()["id"])
    assert client.patch(
        "/api/v1/pod/artworks/batch-category",
        json={"item_ids":artwork_ids,"category_id":target["id"]},
    ).json()["moved"]==2
    assert client.delete(f"/api/v1/pod/artworks/categories/{target['id']}").status_code==409
    assert client.patch(
        "/api/v1/pod/artworks/batch-category",
        json={"item_ids":artwork_ids,"category_id":"uncategorized"},
    ).json()["moved"]==2
    assert client.delete(f"/api/v1/pod/artworks/categories/{target['id']}").json()["deleted"] is True

    with SessionLocal() as session:
        session.add(PodRemoteGalleryAsset(
            artwork_id=artwork_ids[0],supplier="hicustom",gallery_code="g1",usage="pending",
            protected=False,cleanup_candidate=True,sku="HC-1",product_name="毛毯",filename="artwork.png",
        ))
        session.add(IntegrationConnection(
            provider="hicustom",name="HICUSTOM",base_url="https://example.invalid",
            secrets_encrypted=vault.encrypt({"app_key":"key","app_secret":"secret"}),enabled=True,
        ))
        session.commit()
    gallery=client.get("/api/v1/pod/supplier-gallery").json()["items"][0]
    assert gallery["thumbnail_url"]==f"/api/v1/pod/artworks/{artwork_ids[0]}/thumbnail"
    queued=client.delete("/api/v1/pod/supplier-gallery/hicustom/g1")
    assert queued.status_code==202
    assert queued.json()["local_preserved"] is True
    with SessionLocal() as session:
        job=get_job(session,queued.json()["job"]["id"])
        assert job.type=="pod_supplier_gallery_cleanup"


def test_ordered_gallery_is_protected_and_bulk_cleanup_only_queues_pending(client):
    from erp.domains.pod.gallery import mark_ordered_gallery_codes
    with SessionLocal() as session:
        session.add_all([
            PodRemoteGalleryAsset(supplier="hicustom",gallery_code="pending-1",usage="pending",protected=False,cleanup_candidate=True),
            PodRemoteGalleryAsset(supplier="hicustom",gallery_code="ordered-1",usage="pending",protected=False,cleanup_candidate=True),
            IntegrationConnection(provider="hicustom",name="HICUSTOM",base_url="https://example.invalid",secrets_encrypted=vault.encrypt({"app_key":"key","app_secret":"secret"}),enabled=True),
        ]);session.commit()
        mark_ordered_gallery_codes(session,supplier="hicustom",gallery_codes=["ordered-1"],sku="HC-000001-01",product_name="毛毯",order_id="ORDER-1")
    ordered=client.get("/api/v1/pod/supplier-gallery?usage=ordered").json()["items"][0]
    assert ordered["protected"] is True and ordered["order_ids"]==["ORDER-1"]
    assert client.delete("/api/v1/pod/supplier-gallery/hicustom/ordered-1").status_code==409
    result=client.post("/api/v1/pod/supplier-gallery/cleanup-pending",json={}).json()
    assert result["queued"]==1


def test_original_composer_is_embedded_without_legacy_sidebar(client):
    page=client.get("/api/v1/pod/original-composer")
    assert page.status_code==200
    assert "composerWorkspace" in page.text
    assert ".app-sidebar { display: none !important; }" in page.text
    assert "/api/v1/pod/original-assets/app.js" in page.text
    script=client.get("/api/v1/pod/original-assets/app.js")
    assert script.status_code==200
    assert '/api/v1/pod/catalog' in script.text
    assert '/api/v1/pod/artworks' in script.text
    assert '/api/v1/pod/product-types/' in script.text
    assert '/api/v1/pod/legacy-api/jobs/multi' in script.text
    assert '/api/v1/pod/legacy-api/catalog' not in script.text
    assert '/api/v1/pod/legacy-api/artworks' not in script.text
    assert 'setWorkspaceView("composer")' in script.text
    assert 'erp.pod.composerBatch.v1' in script.text
    assert 'importErpComposerBatch' in script.text
    archive=client.get("/api/v1/pod/original-assets-status").json()
    assert archive["ready"] is True and archive["source_read_only"] is True
    assert len(archive["assets"])==3
    assert all(item["matches_source"] for item in archive["assets"])


def test_native_composition_handler_is_loaded_by_worker_bootstrap():
    import erp.worker  # noqa: F401

    assert "pod_native_composition" in HANDLERS


def test_clear_finished_queue_cards_preserves_audit_jobs(client):
    with SessionLocal() as session:
        job=Job(
            type="pod_native_composition",status="failed",idempotency_key="test:failed-composition",
            error_code="unknown_job_type",error_message="旧错误",
        )
        session.add(job);session.flush()
        composition=PodComposition(
            job_id=job.id,supplier="hicustom",status="queued",product_name="失败记录",
            library_sku="HC-000001-01",product_type_id=1,default_color_id=1,default_view_id=1,
        )
        session.add(composition);session.commit();job_id=job.id;composition_id=composition.id

    assert client.get("/api/v1/pod/legacy-api/jobs").json()["counts"]["failed"]==1
    cleared=client.delete("/api/v1/pod/legacy-api/jobs/finished")
    assert cleared.status_code==200
    assert cleared.json()=={"removed":1,"records_preserved":True}
    assert client.get("/api/v1/pod/legacy-api/jobs").json()["jobs"]==[]
    with SessionLocal() as session:
        assert session.get(Job,job_id) is not None
        assert session.get(PodComposition,composition_id).status=="cleared"


def test_failed_composition_is_visible_before_newer_success_in_limited_list(client):
    with SessionLocal() as session:
        failed=Job(type="pod_native_composition",status="failed",idempotency_key="test:visible-failure",error_message="S2B: No query results for model 123")
        completed=Job(type="pod_native_composition",status="completed",idempotency_key="test:newer-success")
        session.add_all([failed,completed]);session.flush()
        session.add_all([
            PodComposition(job_id=failed.id,supplier="s2b",status="queued",product_name="失败记录",product_type_id=1,remote_product_id="123",created_at=datetime(2026,9,1)),
            PodComposition(job_id=completed.id,supplier="s2b",status="completed",product_name="成功记录",product_type_id=1,created_at=datetime(2026,9,2)),
        ])
        session.commit()
    result=client.get("/api/v1/pod/compositions?limit=1").json()
    assert result["jobs"][0]["status"]=="failed"
    assert "供应商产品 123 已不存在" in result["jobs"][0]["error"]


def test_deleted_supplier_product_recovery_preserves_old_ids_and_requeues_local_source(client,tmp_path):
    source=tmp_path/"source.png";source.write_bytes(b"local source")
    with SessionLocal() as session:
        job=Job(type="pod_native_composition",status="failed",attempts=3,max_attempts=3,idempotency_key="test:deleted-s2b")
        session.add(job);session.flush()
        composition=PodComposition(job_id=job.id,supplier="s2b",status="failed",product_name="待恢复",product_type_id=5490,
                                   remote_product_id="36547778",product_code="OLD123")
        session.add(composition);session.flush()
        side=PodCompositionSide(composition_id=composition.id,supplier="s2b",view_id=1,gallery_code="23722185")
        session.add(side);session.flush()
        gallery=PodRemoteGalleryAsset(supplier="s2b",gallery_code="23722185",composition_side_id=side.id,usage="product",protected=True)
        session.add(gallery)
        composition.raw_json=dumps({"sides":[{"side_id":side.id,"source_path":str(source),"gallery_code":"23722185"}]})
        session.commit();composition_id=composition.id;job_id=job.id;gallery_id=gallery.id
        class MissingClient:
            def custom_product(self,_): raise AppError("No query results for model",code="s2b_api_error")
        assert retry_deleted_s2b_product(session,composition_id,MissingClient())==job_id
        session.refresh(composition)
        assert composition.remote_product_id=="" and composition.product_code==""
        assert loads(composition.raw_json,{})["deleted_remote_recovery_history"][0]["remote_product_id"]=="36547778"
        assert session.get(PodCompositionSide,side.id).gallery_code==""
        assert session.get(PodRemoteGalleryAsset,gallery_id).remote_deleted is True
        assert session.get(Job,job_id).status=="queued" and session.get(Job,job_id).max_attempts==6


def test_one_click_retry_includes_cleared_failed_history_and_is_idempotent(client,tmp_path):
    source=tmp_path/"source.png";source.write_bytes(b"local source")
    with SessionLocal() as session:
        job=Job(type="pod_native_composition",status="failed",idempotency_key="test:one-click-retry")
        session.add(job);session.flush()
        composition=PodComposition(job_id=job.id,supplier="s2b",status="cleared",product_name="待恢复",product_type_id=5490)
        session.add(composition);session.flush()
        side=PodCompositionSide(composition_id=composition.id,supplier="s2b",view_id=1)
        session.add(side);session.flush()
        composition.raw_json=dumps({"sides":[{"side_id":side.id,"source_path":str(source),"gallery_code":""}]})
        session.commit();job_id=job.id;composition_id=composition.id
    preview=client.get("/api/v1/pod/compositions/retry-failed/preview").json()
    assert preview=={"failed":1,"local_ready":1,"hidden_history":1,"remote_check_required":0}
    class NoRemoteCalls:
        def __enter__(self): return self
        def __exit__(self,*_): return None
    with patch("erp.domains.pod.integrations.registry.supplier_client",return_value=NoRemoteCalls()):
        result=client.post("/api/v1/pod/compositions/retry-failed")
        again=client.post("/api/v1/pod/compositions/retry-failed")
    assert result.status_code==202 and result.json()["queued"]==1
    assert again.status_code==202 and again.json()["queued"]==0
    with SessionLocal() as session:
        assert session.get(Job,job_id).status=="queued"
        assert session.get(PodComposition,composition_id).status=="queued"


def test_retry_existing_supplier_product_keeps_remote_ids(client,tmp_path):
    source=tmp_path/"source.png";source.write_bytes(b"local source")
    with SessionLocal() as session:
        job=Job(type="pod_native_composition",status="failed",idempotency_key="test:resume-existing")
        session.add(job);session.flush()
        composition=PodComposition(job_id=job.id,supplier="s2b",status="failed",product_name="继续原产品",product_type_id=5490,
                                   remote_product_id="123",product_code="KEEP123")
        session.add(composition);session.flush()
        composition.raw_json=dumps({"sides":[{"source_path":str(source),"gallery_code":"456"}]})
        session.commit();composition_id=composition.id;job_id=job.id
        class ExistingClient:
            def custom_product(self,_): return {"id":"123"}
        assert retry_failed_composition(session,composition_id,ExistingClient())==job_id
        session.refresh(composition)
        assert composition.remote_product_id=="123" and composition.product_code=="KEEP123"
        assert loads(composition.raw_json,{})["sides"][0]["gallery_code"]=="456"
        assert session.get(Job,job_id).status=="queued"


def test_new_pod_sku_uses_legacy_global_family_sequence(client):
    with SessionLocal() as session:
        session.add(CatalogProduct(
            sku="S2B-000007-01",group_code="S2B-000007",source_type="pod",name="旧产品",
        ))
        session.add(IntegrationConnection(
            provider="hicustom",name="HICUSTOM",base_url="https://supplier.invalid",
            secrets_encrypted=vault.encrypt({"app_key":"key","app_secret":"secret"}),enabled=True,
        ))
        session.commit()
    image=io.BytesIO();Image.new("RGB",(8,8),(1,2,3)).save(image,"PNG")
    artwork=client.post(
        "/api/v1/pod/artworks",files={"image":("art.png",image.getvalue(),"image/png")},
        data={"category_id":"uncategorized"},
    ).json()
    detail={"supplier":"hicustom","colors":[{"id":1}],"sizes":[{"id":2}],"stock_info":[{"id":3,"color_id":1,"size_id":2}]}
    payload={"supplier":"hicustom","product_type_id":9,"product_name":"新产品","default_color_id":1,"default_view_id":1,"selected_size_ids":[2],"placements":[{"view_id":1,"width":100,"height":100,"top_x":0,"top_y":0,"artwork_id":artwork["id"],"rotation":0}]}
    with patch("erp.domains.pod.api.native_product_detail",return_value=detail):
        created=client.post("/api/v1/pod/compositions",json=payload)
    assert created.status_code==202,created.text
    assert created.json()["composition"]["library_sku"]=="HC-000008-01"


def test_original_composer_status_and_settings_use_masked_erp_connections(client):
    with SessionLocal() as session:
        session.add(IntegrationConnection(
            provider="hicustom",name="HICUSTOM",base_url="https://supplier.invalid",
            config_json=dumps({"store_id":"store-1"}),
            secrets_encrypted=vault.encrypt({"app_key":"plain-key","app_secret":"plain-secret"}),enabled=True,
        ))
        session.commit()
    status=client.get("/api/v1/pod/legacy-api/status")
    assert status.status_code==200
    configuration=status.json()["configuration"]
    assert configuration["configured"] is True
    assert configuration["hicustom_store_id"]=="store-1"
    assert configuration["app_key"]["configured"] is True
    assert "plain-key" not in status.text and "plain-secret" not in status.text

    updated=client.put("/api/v1/pod/legacy-api/settings",json={
        "hicustom_base_url":"https://supplier-updated.invalid",
        "hicustom_store_id":"store-2",
        "composition_concurrency":2,
        "copy_api_host":"https://www.micuapi.ai",
        "copy_api_token":"chat-token",
        "copy_model":"unsupported-model",
        "ozon_performance_token":"must-not-be-migrated",
    })
    assert updated.status_code==200
    assert updated.json()["copywriting"]["model"]=="gpt-5.6-terra"
    assert "chat-token" not in updated.text
    with SessionLocal() as session:
        assert session.scalar(select(IntegrationConnection).where(
            IntegrationConnection.provider=="ozon_performance"
        )) is None


def test_original_composer_submit_is_intercepted_by_native_erp_job(client):
    with SessionLocal() as session:
        session.add(IntegrationConnection(provider="hicustom",name="HICUSTOM",base_url="https://supplier.invalid",secrets_encrypted=vault.encrypt({"app_key":"key","app_secret":"secret"}),enabled=True));session.commit()
    image=io.BytesIO();Image.new("RGB",(8,8),(1,2,3)).save(image,"PNG")
    detail={"supplier":"hicustom","colors":[{"id":29}],"sizes":[{"id":7}],"stock_info":[{"id":701,"color_id":29,"size_id":7}]}
    form={"placements":dumps([{"view_id":1,"view_name":"正面","width":100,"height":100,"top_x":0,"top_y":0,"rotation":0}]),"product_type_id":"10000","product_name":"T恤","default_color_id":"29","default_view_id":"1","supplier":"hicustom","selected_size_ids":"[7]"}
    with patch("erp.domains.pod.api.native_product_detail",return_value=detail),patch("erp.domains.pod.api.httpx.request") as legacy_request:
        response=client.post("/api/v1/pod/legacy-api/jobs/multi",data=form,files={"images":("design.png",image.getvalue(),"image/png")})
    assert response.status_code==200,response.text
    legacy_request.assert_not_called()
    composition_id=response.json()["job"]["id"]
    with SessionLocal() as session:
        from erp.domains.pod.models import PodComposition
        composition=session.get(PodComposition,composition_id)
        assert composition is not None
        job=session.get(Job,composition.job_id)
        assert job.type=="pod_native_composition"


def test_supplier_refresh_preserves_product_and_variant_identity(client):
    from erp.models import CatalogVariant
    with SessionLocal() as session:
        product=CatalogProduct(sku="S2B-009999-01",group_code="S2B-009999",source_type="pod",name="S2B 测试",supplier="S2B",supplier_product_id="4839",status="ready")
        session.add(product);session.flush()
        variant=CatalogVariant(product_id=product.id,sku=product.sku,supplier_product_id="4839",supplier_sku_id="33308",properties_json=dumps({"color_id":6,"size_id":1119}),active=True)
        session.add(variant);session.commit();product_id=product.id;variant_id=variant.id
    detail={"supplier":"s2b","colors":[{"id":6,"cn_name":"白色"}],"sizes":[{"id":1119,"name":"140×180"}],"stock_info":[{"id":33308,"code":"OM4BGU","color_id":6,"size_id":1119,"price":"17.70","length":19,"width":16,"height":5,"weight":324}],"prices":{"wholesale_price":[{"stock_info":[{"id":33308}],"retail_price":{"price":"17.70"}}]}}
    with patch("erp.domains.pod.supplier_catalog.refresh_product_detail",return_value=detail):
        response=client.post(f"/api/v1/pod/library/products/{product_id}/refresh-supplier",json={})
    assert response.status_code==200,response.text
    result=response.json()
    assert result["created_products"]==0 and result["created_variants"]==0
    assert result["preserved_variant_ids"]==[variant_id]
    assert result["product"]["id"]==product_id and result["product"]["variants"][0]["id"]==variant_id
    assert result["product"]["variants"][0]["purchase_price_cny"]==17.7


def test_pod_library_exposes_versions_categories_and_restore_job(client,tmp_path):
    from erp.domains.pod.models import PodColorImageGroup,PodProductVersion
    from erp.models import Asset,CatalogProduct,CatalogProductAsset,CatalogVariant
    with SessionLocal() as session:
        product=CatalogProduct(sku="HC-000001",group_code="HC-000001",source_type="pod",name="POD 毛毯",supplier="HICUSTOM",supplier_product_id="10000",purchase_price_cny=20,package_length_mm=300,package_width_mm=200,package_height_mm=100,package_weight_g=500,status="ready")
        session.add(product);session.flush()
        session.add(CatalogVariant(product_id=product.id,sku="HC-000001-01",supplier_sku_id="SUP-1",color="黑色",size="M",legacy_source="pod_product_library",legacy_id="variant-1"))
        asset=Asset(kind="image",name="effect.png",source="pod",oss_bucket="bucket",oss_key="hicustom-products/effect.png",oss_url="https://bucket.example/effect.png",sha256="",mime_type="image/png",read_only=True,mirror_status="mirrored")
        session.add(asset);session.flush();session.add_all([CatalogProductAsset(product_id=product.id,asset_id=asset.id,role="main",position=0,is_current=True,source_kind="pod_native_composition"),CatalogProductAsset(product_id=product.id,asset_id=asset.id,role="artwork",position=0,is_current=True,source_kind="pod_native_composition")])
        version=PodProductVersion(catalog_product_id=product.id,version_no=1,is_current=True,legacy_job_id="",job_status="completed",supplier="hicustom",product_type_id=10000,default_color_id=29,default_view_id=1,product_code="HC-P1",product_code_status="active",legacy_output_folder="",recipe_json=dumps({"supplier":"hicustom","sides":[{"view_id":1,"source_filename":"design.png"}]}),assets_json="{}")
        session.add(version);session.flush();session.add(PodColorImageGroup(version_id=version.id,color_id=29,color_name="黑色",rendering_urls_json='["https://bucket.example/effect.png"]',saved_files_json='[]'))
        session.add(IntegrationConnection(provider="oss",name="POD OSS",base_url="https://oss.example",config_json=dumps({"region":"cn-test","bucket":"bucket","prefix":"hicustom-products"}),secrets_encrypted=vault.encrypt({"access_key_id":"key","access_key_secret":"secret"}),enabled=True));session.commit();product_id=product.id
    summary=client.get("/api/v1/pod/library/summary").json()
    assert summary["products"]==1 and summary["variants"]==1 and summary["versions"]==1
    listing=client.get("/api/v1/pod/library/products").json()
    assert listing["items"][0]["thumbnail_url"]=="https://bucket.example/effect.png"
    detail=client.get(f"/api/v1/pod/library/products/{product_id}").json()
    assert detail["versions"][0]["product_code"]=="HC-P1"
    assert detail["publication_readiness"]["ready_for_publication"] is True
    assert detail["publication_readiness"]["completeness_percent"] == 100
    assert detail["production_readiness"]["ready_for_production"] is True
    assert detail["production_readiness"]["can_rebuild_supplier_product"] is True
    assert listing["items"][0]["library_state"] == "ready"
    variant=detail["variants"][0]
    updated=client.put(f"/api/v1/catalog/products/{product_id}/variants",json=[{**variant,"purchase_price_cny":22}]).json()
    assert updated["variants"][0]["id"]==variant["id"]
    assert updated["variants"][0]["legacy_id"]=="variant-1"
    category=client.post("/api/v1/pod/library/categories",json={"name":"毛毯"}).json()["category"]
    assert client.patch("/api/v1/pod/library/products/batch-category",json={"product_ids":[product_id],"category":"毛毯"}).json()["updated"]==1
    assert client.get(f"/api/v1/pod/library/products/{product_id}").json()["category"]=="毛毯"

    class FakeOss:
        connection_id="oss-1"
        def download_asset(self,*,key,target,expected_sha256=""):
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b"restored")
            return {"path":str(target),"sha256":"restored-hash","size":8}
    with patch("erp.domains.pod.library.PodOssService.from_session",return_value=FakeOss()):
        queued=client.post(f"/api/v1/pod/library/products/{product_id}/restore").json()["job"]
        with SessionLocal() as session:
            execute_job(session,get_job(session,queued["id"]))
            assert get_job(session,queued["id"]).status=="completed"


def test_pod_library_orders_products_by_numeric_sku_descending(client):
    with SessionLocal() as session:
        session.add_all(
            [
                CatalogProduct(sku="HC-000009-01", group_code="HC-000009", source_type="pod", name="产品 9", status="ready"),
                CatalogProduct(sku="S2B-000100-01", group_code="S2B-000100", source_type="pod", name="产品 100", status="ready"),
                CatalogProduct(sku="HC-000020-01", group_code="HC-000020", source_type="pod", name="产品 20", status="ready"),
            ]
        )
        session.commit()

    response = client.get("/api/v1/pod/library/products?page=1&page_size=50")

    assert response.status_code == 200, response.text
    assert [item["sku"] for item in response.json()["items"]] == [
        "S2B-000100-01",
        "HC-000020-01",
        "HC-000009-01",
    ]


def test_composition_batch_persists_all_items_before_background_preparation(client):
    artwork_ids = []
    for index in range(3):
        image = io.BytesIO()
        Image.new("RGB", (40 + index, 40), (index * 40, 20, 30)).save(image, "JPEG")
        result = client.post(
            "/api/v1/pod/artworks",
            files={"image": (f"batch-{index}.jpg", image.getvalue(), "image/jpeg")},
        ).json()
        artwork_ids.append(result["id"])
    with SessionLocal() as session:
        session.add(IntegrationConnection(
            provider="s2b", name="S2B", base_url="https://supplier.invalid",
            secrets_encrypted=vault.encrypt({"app_key": "key", "app_secret": "secret"}), enabled=True,
        ))
        session.commit()
    detail = {
        "supplier": "s2b", "sizes": [{"id": 1119}],
        "stock_info": [{"id": 1, "color_id": 6, "size_id": 1119}],
    }
    request = {
        "supplier": "s2b", "product_type_id": 5490, "product_name": "批量桌布",
        "default_color_id": 6, "default_view_id": 1, "selected_size_ids": [1119],
        "placements": [{
            "view_id": 1, "view_name": "A面", "width": 575, "height": 431,
            "top_x": -12.2, "top_y": 0, "artwork_id": artwork_ids[0], "rotation": 0,
        }],
        "artwork_ids": artwork_ids,
    }
    with patch("erp.domains.pod.api.native_product_detail", return_value=detail):
        response = client.post("/api/v1/pod/compositions/batch", json=request)

    assert response.status_code == 202, response.text
    assert response.json()["accepted"] == 3
    assert len(set(response.json()["composition_ids"])) == 3
    with SessionLocal() as session:
        compositions = [session.get(PodComposition, identity) for identity in response.json()["composition_ids"]]
        jobs = [session.get(Job, item.job_id) for item in compositions]
        assert all(loads(item.raw_json, {})["sides"][0]["source_prepared"] is False for item in compositions)
        assert all(job.max_attempts == 3 and job.status == "queued" for job in jobs)
