from types import SimpleNamespace

from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps, loads
from erp.models import Listing, ResearchProduct, Store, StoreListing
from erp.services import ozon_follow


def _records(session, statuses):
    product = ResearchProduct(source="test", source_sku="CLEAN-SKU", title="清理测试")
    session.add(product); session.flush()
    listing = Listing(research_product_id=product.id, base_offer_id="CLEAN-1", title="清理测试")
    session.add(listing); session.flush()
    for index, status in enumerate(statuses):
        store = Store(platform="ozon", name=f"清理店{index}", external_account_id=f"clean-{index}", currency="CNY")
        session.add(store); session.flush()
        assets = {"primary_image":f"https://oss.test/{index}/main.jpg", "images":[], "ai_preparation":{"image_review_status":status,"generated_image_path":"local.jpg"}}
        session.add(StoreListing(listing_id=listing.id, store_id=store.id, offer_id=f"CLEAN-{index}", idempotency_key=f"clean-{index}", assets_json=dumps(assets)))
    session.commit(); session.refresh(listing)
    return listing


def test_cleanup_waits_until_every_store_image_is_approved(monkeypatch, tmp_path):
    monkeypatch.setattr(ozon_follow, "DATA_DIR", tmp_path)
    folder = tmp_path / "generated" / "CLEAN-SKU"; folder.mkdir(parents=True); candidate = folder / "candidate.jpg"; candidate.write_bytes(b"image")
    with SessionLocal() as session:
        listing = _records(session, ["APPROVED", "PENDING"])
        context = SimpleNamespace(session=session, event=lambda *_args, **_kwargs: None)
        assert ozon_follow._cleanup_uploaded_product_files(context, listing) is None
        assert candidate.exists()


def test_cleanup_removes_files_after_all_store_images_are_on_oss(monkeypatch, tmp_path):
    monkeypatch.setattr(ozon_follow, "DATA_DIR", tmp_path)
    folder = tmp_path / "generated" / "CLEAN-SKU"; folder.mkdir(parents=True); (folder / "candidate.jpg").write_bytes(b"image")
    with SessionLocal() as session:
        listing = _records(session, ["APPROVED", "APPROVED"])
        events = []; context = SimpleNamespace(session=session, event=events.append)
        assert ozon_follow._cleanup_uploaded_product_files(context, listing) == {"files":1,"bytes":5}
        assert not folder.exists()
        assert all(loads(child.assets_json,{})["ai_preparation"]["generated_image_path"] == "" for child in listing.store_listings)
        assert all(loads(child.assets_json,{})["primary_image"].startswith("https://") for child in listing.store_listings)
