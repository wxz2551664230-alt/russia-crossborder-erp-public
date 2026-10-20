from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from PIL import Image
from sqlalchemy import select

from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.domains.pod.migration import inspect_legacy_pod
from erp.domains.pod.models import (
    PodArtwork,
    PodColorImageGroup,
    PodComposition,
    PodProductCodeHistory,
    PodProductVersion,
    PodRemoteGalleryAsset,
)
from erp.models import Asset, CatalogImportRun, CatalogProduct, Job
from erp.services.jobs import acquire_job, execute_job


def _legacy_root(tmp_path: Path) -> tuple[Path, str, bytes]:
    root = tmp_path / "指纹科技图片合成"
    data = root / "data"
    gallery = root / "图库" / "images"
    output = root / "产品库" / "products" / "HC-000001" / "originals" / "view-1"
    data.mkdir(parents=True)
    gallery.mkdir(parents=True)
    output.mkdir(parents=True)
    image_path = gallery / "artwork.png"
    Image.new("RGB", (16, 12), "red").save(image_path)
    content = image_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    (output / "original.png").write_bytes(content)

    product_db = sqlite3.connect(data / "product_library.db")
    product_db.executescript(
        """
        create table product_categories(id text primary key,name text,sort_order integer,is_system integer,created_at text,updated_at text);
        create table category_product_type_rules(product_type_id integer,category_id text,created_at text,updated_at text);
        create table product_deletion_tombstones(job_id text,sku text,product_name text,deleted_at text);
        create table products(id text primary key,sku text,family_no integer,variant_no integer,name text,status text,tags_json text,notes text,current_version_id text,legacy_import integer,created_at text,updated_at text,category_id text);
        create table product_versions(id text primary key,product_id text,version_no integer,job_id text,job_status text,product_type_id integer,default_color_id integer,default_view_id integer,product_code text,product_code_status text,output_folder text,recipe_json text,assets_json text,created_at text,updated_at text);
        create table product_code_history(id text primary key,version_id text,product_code text,status text,first_seen_at text,last_seen_at text);
        create table product_color_images(id text primary key,version_id text,color_id integer,color_name text,rendering_urls_json text,saved_files_json text,created_at text,updated_at text);
        create table product_variants(id text primary key,product_id text,sku text,variant_no integer,color_id integer,color_name text,size_id integer,size_name text,stock_item_id integer,stock_code text,package_length real,package_width real,package_height real,package_volume real,weight real,size_attributes_json text,prices_json text,image_group_id text,selected integer,active integer,created_at text,updated_at text);
        create table product_oss_assets(id text primary key,version_id text,asset_id text,asset_type text,color_id integer,color_name text,view_id integer,local_path text,oss_bucket text,oss_key text,oss_uri text,oss_url text,content_type text,size integer,sha256 text,etag text,crc64 text,oss_version_id text,uploaded_at text,updated_at text);
        """
    )
    now = "2026-09-09T08:00:00+00:00"
    product_db.execute("insert into product_categories values(?,?,?,?,?,?)", ("cat-1", "毛毯", 1, 0, now, now))
    product_db.execute("insert into category_product_type_rules values(?,?,?,?)", (100, "cat-1", now, now))
    product_db.execute("insert into products values(?,?,?,?,?,?,?,?,?,?,?,?,?)", ("product-1", "HC-000001-01", 1, 1, "测试毛毯", "ready", "[]", "", "version-1", 0, now, now, "cat-1"))
    recipe = json.dumps({"supplier": "hicustom", "product_type_id": 100}, ensure_ascii=False)
    product_db.execute("insert into product_versions values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("version-1", "product-1", 1, "job-1", "completed", 100, 29, 1, "REMOTE-1", "confirmed", str(output.parent.parent), recipe, "{}", now, now))
    product_db.execute("insert into product_code_history values(?,?,?,?,?,?)", ("code-1", "version-1", "REMOTE-1", "confirmed", now, now))
    product_db.execute("insert into product_color_images values(?,?,?,?,?,?,?,?)", ("group-1", "version-1", 29, "红色", "[]", "[]", now, now))
    product_db.execute("insert into product_variants values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("variant-1", "product-1", "HC-000001-01", 1, 29, "红色", 1, "S", 9001, "RED-S", 10, 20, 30, 6000, 200, "[]", "{}", "group-1", 1, 1, now, now))
    oss_key = f"hicustom-products/library/products/HC-000001/originals/view-1/original.png"
    product_db.execute("insert into product_oss_assets values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("media-1", "version-1", "source-1", "source", None, "", 1, "originals/view-1/original.png", "example-assets", oss_key, f"oss://example-assets/{oss_key}", f"https://example.invalid/{oss_key}", "image/png", len(content), digest, "etag", "crc", "", now, now))
    product_db.commit(); product_db.close()

    orders = sqlite3.connect(data / "orders.db")
    orders.execute("create table batch_order_groups(id text,sku text,product_name text,supplier text,submission_status text,fulfillment_gallery_codes text,out_order_id text,hicustom_order_id text,tracking_number text)")
    orders.execute("insert into batch_order_groups values(?,?,?,?,?,?,?,?,?)", ("order-1", "HC-000001-01", "测试毛毯", "hicustom", "submitted", '[\"GALLERY-1\"]', "ORDER-1", "", "TRACK-1"))
    orders.commit(); orders.close()

    artwork = {
        "categories": [{"id": "art-cat", "name": "毛毯"}],
        "assignments": {"art-1": "art-cat"},
        "uploads": [{
            "id": "art-1", "name": "测试图.png", "category_id": "art-cat", "relative_path": "images/artwork.png",
            "mime_type": "image/png", "sha256": digest, "updated_at": now, "source": "legacy",
            "oss_sync_status": "synced", "oss_asset": {"oss_bucket": "example-assets", "oss_key": "hicustom-products/artwork-library/test.png", "oss_url": "https://example.invalid/test.png", "content_type": "image/png", "size": len(content), "sha256": digest},
        }],
    }
    (data / "artwork_library.json").write_text(json.dumps(artwork, ensure_ascii=False), encoding="utf-8")
    jobs = [{
        "id": "job-1", "status": "completed", "stage_text": "完成", "supplier": "hicustom", "product_name": "测试毛毯",
        "library_sku": "HC-000001-01", "product_type_id": 100, "default_color_id": 29, "default_view_id": 1,
        "remote_product_id": "1", "product_code": "REMOTE-1", "external_id": "queue-1", "selected_size_ids": [1],
        "sides": [{"view_id": 1, "view_name": "正面", "gallery_code": "GALLERY-1", "artwork_id": "art-1", "source_filename": "测试图.png", "source_path": str(image_path), "mime_type": "image/png", "rotation": 0, "placement": {"width": 100, "height": 80, "top_x": 10, "top_y": 20}}],
        "created_at": now, "updated_at": now,
    }]
    (data / "jobs.json").write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
    (data / "hicustom_catalog.json").write_text(json.dumps({"products": [{"id": 100}], "categories": [{"id": 1}], "previews": {}, "details": {}}, ensure_ascii=False), encoding="utf-8")
    return root, digest, content


def test_native_pod_preview_is_read_only(tmp_path: Path):
    root, digest, content = _legacy_root(tmp_path)
    before = (root / "图库" / "images" / "artwork.png").read_bytes()
    report = inspect_legacy_pod(root)
    assert report["source_read_only"] is True
    assert report["product_counts"]["products"] == 1
    assert report["artworks"]["uploads"] == 1
    assert report["composition_jobs"]["completed"] == 1
    assert before == content == (root / "图库" / "images" / "artwork.png").read_bytes()
    assert hashlib.sha256(before).hexdigest() == digest


def test_native_pod_migration_preserves_source_and_imports_full_history(client, tmp_path: Path):
    root, digest, source_content = _legacy_root(tmp_path)
    response = client.post("/api/v1/pod/migrations", json={"dry_run": False, "source_root": str(root)})
    assert response.status_code == 202
    job_id = response.json()["job"]["id"]
    with SessionLocal() as session:
        job = acquire_job(session, "test-worker")
        assert job and job.id == job_id
        execute_job(session, job)
    with SessionLocal() as session:
        assert session.get(Job, job_id).status == "completed"
        run = session.scalar(select(CatalogImportRun).where(CatalogImportRun.job_id == job_id))
        assert run and run.status == "completed"
        product = session.scalar(select(CatalogProduct).where(CatalogProduct.legacy_id == "product-1"))
        assert product and product.sku == "HC-000001-01"
        assert session.scalar(select(PodProductVersion).where(PodProductVersion.legacy_id == "version-1"))
        assert session.scalar(select(PodProductCodeHistory).where(PodProductCodeHistory.legacy_id == "code-1"))
        assert session.scalar(select(PodColorImageGroup).where(PodColorImageGroup.legacy_id == "group-1"))
        artwork = session.scalar(select(PodArtwork).where(PodArtwork.legacy_id == "art-1"))
        assert artwork and artwork.sha256 == digest
        asset = session.get(Asset, artwork.asset_id)
        assert asset and Path(asset.local_path).is_file()
        assert Path(asset.local_path).read_bytes() == source_content
        composition = session.scalar(select(PodComposition).where(PodComposition.legacy_id == "job-1"))
        assert composition and composition.catalog_product_id == product.id
        gallery = session.scalar(select(PodRemoteGalleryAsset).where(PodRemoteGalleryAsset.gallery_code == "GALLERY-1"))
        assert gallery and gallery.usage == "ordered" and gallery.protected is True
    assert (root / "图库" / "images" / "artwork.png").read_bytes() == source_content
