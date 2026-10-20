from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select

from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.models import IntegrationConnection, Job, JobArtifact
from erp.services.config_resolution import resolve_job_snapshot
from erp.services.jobs import JobContext
from erp.services.self_listing_pipeline import self_listing_pipeline_job
from erp.core.errors import AppError
from erp.domains.self_listing.vendor_sources.copywriting import get_folder_number, load_resume_rows
from erp.domains.self_listing.vendor_sources.image_generation import (
    build_appendix_filename,
    build_plan_item_prompt,
    build_sku_cover_filename,
    compact_image_edit_prompt,
    inspect_product_generation,
    prepare_sku_cover_plan_item,
)


def _connections() -> dict[str, str]:
    rows = [
        IntegrationConnection(
            provider="ai_chat",
            name="copywriter",
            base_url="https://chat.invalid/v1/chat/completions",
            config_json=dumps({"default_model": "test-chat"}),
            secrets_encrypted=vault.encrypt({"api_key": "chat-secret"}),
            enabled=True,
        ),
        IntegrationConnection(
            provider="ai_image",
            name="image-generator",
            base_url="https://image.invalid/v1/images/edits",
            config_json=dumps({"default_model": "test-image"}),
            secrets_encrypted=vault.encrypt({"api_key": "image-secret"}),
            enabled=True,
        ),
        IntegrationConnection(
            provider="oss",
            name="oss",
            base_url="https://oss.invalid",
            config_json=dumps({"region": "cn-test", "bucket": "test-bucket"}),
            secrets_encrypted=vault.encrypt({"access_key_id": "oss-key", "access_key_secret": "oss-secret"}),
            enabled=True,
        ),
    ]
    with SessionLocal() as session:
        session.add_all(rows)
        session.commit()
        return {row.provider: row.id for row in rows}


def test_pipeline_endpoint_queues_five_steps_without_plaintext_secrets(client, tmp_path: Path):
    ids = _connections()
    product_root = tmp_path / "products"
    product_root.mkdir()

    response = client.post("/api/v1/catalog/self-listing/pipeline", json={
        "product_root": str(product_root),
        "ai_chat_connection_id": ids["ai_chat"],
        "ai_image_connection_id": ids["ai_image"],
        "oss_connection_id": ids["oss"],
        "oss_dry_run": True,
    })

    assert response.status_code == 202
    body = response.json()
    assert body["type"] == "self_listing_pipeline"
    assert [step["key"] for step in body["steps"]] == [
        "generate_images", "generate_copywriting", "watermark", "upload_original", "upload_watermarked",
    ]
    with SessionLocal() as session:
        job = session.get(Job, body["id"])
        assert job is not None
        assert "secret" not in job.payload_json
        snapshot = resolve_job_snapshot(job.config_snapshot_json)
        assert snapshot["secrets"]["ai_chat"]["api_key"] == "chat-secret"
        assert loads(job.payload_json, {})["oss_dry_run"] is True


def test_pipeline_can_queue_one_standalone_step_without_unused_connections(client, tmp_path: Path):
    product_root = tmp_path / "products"
    product_root.mkdir()

    response = client.post("/api/v1/catalog/self-listing/pipeline", json={
        "product_root": str(product_root),
        "selected_steps": ["watermark"],
    })

    assert response.status_code == 202
    body = response.json()
    assert [step["key"] for step in body["steps"]] == ["watermark"]
    with SessionLocal() as session:
        job = session.get(Job, body["id"])
        assert resolve_job_snapshot(job.config_snapshot_json)["connections"] == {}


def test_local_directory_picker_returns_selected_path(client, monkeypatch, tmp_path: Path):
    class FakeRoot:
        def withdraw(self): pass
        def attributes(self, *_args): pass
        def destroy(self): pass

    monkeypatch.setattr("tkinter.Tk", FakeRoot)
    monkeypatch.setattr("tkinter.filedialog.askdirectory", lambda **_kwargs: str(tmp_path))

    response = client.post("/api/v1/catalog/self-listing/select-directory", json={
        "title": "选择产品图片根目录",
    })

    assert response.status_code == 200
    assert response.json() == {"path": str(tmp_path.resolve()), "cancelled": False}


def test_pipeline_generates_three_registered_spreadsheets(monkeypatch, tmp_path: Path):
    ids = _connections()
    product_root = tmp_path / "products"
    generated = product_root / "item-1" / "_generated"
    generated.mkdir(parents=True)
    (product_root / "watermark.png").write_bytes(b"watermark")
    output_dir = tmp_path / "outputs"
    watermark_root = tmp_path / "watermarked"

    with SessionLocal() as session:
        connections = {provider: session.get(IntegrationConnection, connection_id) for provider, connection_id in ids.items()}
        public = {"connections": {
            provider: {"id": row.id, "base_url": row.base_url, "config": loads(row.config_json, {})}
            for provider, row in connections.items()
        }}
        secrets = {
            provider: vault.decrypt(row.secrets_encrypted)
            for provider, row in connections.items()
        }
        job = Job(
            type="self_listing_pipeline",
            status="running",
            idempotency_key="self-listing-test",
            payload_json=dumps({
                "product_root": str(product_root), "output_dir": str(output_dir),
                "watermark_root": str(watermark_root), "watermark_filename": "watermark.png",
                "source_subfolder": "_generated", "oss_dry_run": True,
            }),
            config_snapshot_json=dumps({"public": public, "secrets_encrypted": vault.encrypt(secrets)}),
        )
        session.add(job)
        session.commit()

        observed = {}

        def fake_images(config, log_func, **_kwargs):
            observed["dimension_image"] = config.generate_dimension_image
            observed["product_hint"] = config.product_hint
            (generated / "main.jpg").write_bytes(b"image")
            log_func("images done")

        def fake_copywriting(_root, config):
            Path(config["output_xlsx"]).parent.mkdir(parents=True, exist_ok=True)
            Path(config["output_xlsx"]).write_bytes(b"copy")

        def fake_watermark(*_args, **kwargs):
            target = Path(kwargs["output_root"]) / "item-1" / "_generated"
            target.mkdir(parents=True, exist_ok=True)
            (target / "main.jpg").write_bytes(b"watermarked")

        def fake_upload(_root, *, output_path, **_kwargs):
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"urls")
            return [], {"images": Path(output_path), "products": Path(output_path)}

        monkeypatch.setattr("erp.services.self_listing_pipeline.image_generation.run_generation", fake_images)
        monkeypatch.setattr("erp.services.self_listing_pipeline.copywriting.run_copywriting", fake_copywriting)
        monkeypatch.setattr("erp.services.self_listing_pipeline.watermark.process_watermark_directory", fake_watermark)
        monkeypatch.setattr("erp.services.self_listing_pipeline.oss_uploader.batch_upload_images", fake_upload)

        self_listing_pipeline_job(JobContext(session=session, job=job))

        assert observed["dimension_image"] is False
        assert observed["product_hint"] == ""
        artifacts = session.scalars(select(JobArtifact).where(JobArtifact.job_id == job.id)).all()
        assert {item.role for item in artifacts} == {
            "copywriting_xlsx", "oss_original_xlsx", "oss_watermarked_xlsx",
        }
        assert (output_dir / "ozon_copywriting.xlsx").is_file()
        assert (output_dir / "oss_original.xlsx").is_file()
        assert (output_dir / "oss_watermarked.xlsx").is_file()


def test_pipeline_retry_skips_steps_that_already_completed(monkeypatch, tmp_path: Path):
    from erp.models import JobStep
    from erp.services.self_listing_pipeline import _step_needs_run

    with SessionLocal() as session:
        job = Job(type="self_listing_pipeline", status="running", idempotency_key="retry-skip-test")
        session.add(job)
        session.flush()
        session.add_all([
            JobStep(job_id=job.id, key="generate_images", name="AI 批量生图", position=1, status="pending"),
            JobStep(job_id=job.id, key="generate_copywriting", name="生成文案表", position=2, status="completed"),
            JobStep(job_id=job.id, key="watermark", name="批量加水印", position=3, status="completed"),
        ])
        session.commit()
        session.refresh(job)
        context = JobContext(session=session, job=job)
        assert _step_needs_run(context, "generate_images") is True
        assert _step_needs_run(context, "generate_copywriting") is False
        assert _step_needs_run(context, "watermark") is False


def test_image_generation_preflight_skips_complete_product(tmp_path: Path):
    from PIL import Image
    from erp.domains.self_listing.vendor_sources.image_generation import AppConfig

    product = tmp_path / "item-1"
    output = product / "_generated"
    output.mkdir(parents=True)
    Image.new("RGB", (1792, 2400), "red").save(product / "SKU-red.jpg")
    Image.new("RGB", (1792, 2400), "blue").save(output / build_sku_cover_filename(product / "SKU-red.jpg"))
    Image.new("RGB", (1792, 2400), "green").save(output / build_appendix_filename(1))
    (output / "image_plan.json").write_text(
        dumps({"images": [
            {"id": "main", "image_type": "hero"},
            {"id": "detail", "image_type": "lifestyle"},
        ]}),
        encoding="utf-8",
    )

    state = inspect_product_generation(product, AppConfig(root_dir=str(tmp_path)))

    assert state["complete"] is True
    assert state["missing"] == []


def test_image_generation_preflight_returns_only_missing_outputs(tmp_path: Path):
    from PIL import Image
    from erp.domains.self_listing.vendor_sources.image_generation import AppConfig

    product = tmp_path / "item-1"
    output = product / "_generated"
    output.mkdir(parents=True)
    Image.new("RGB", (1792, 2400), "red").save(product / "SKU-red.jpg")
    Image.new("RGB", (1792, 2400), "blue").save(output / build_sku_cover_filename(product / "SKU-red.jpg"))
    (output / "image_plan.json").write_text(
        dumps({"images": [
            {"id": "main", "image_type": "hero"},
            {"id": "detail-1", "image_type": "lifestyle"},
            {"id": "detail-2", "image_type": "detail"},
        ]}),
        encoding="utf-8",
    )

    state = inspect_product_generation(product, AppConfig(root_dir=str(tmp_path)))

    assert state["complete"] is False
    assert state["missing"] == [build_appendix_filename(1), build_appendix_filename(2)]


def test_copywriting_resume_only_restores_successful_rows(tmp_path: Path):
    partial = tmp_path / "copy.partial.csv"
    pd.DataFrame([
        {"variant_key": "one::a", "title": "ready", "description": "done", "error": ""},
        {"variant_key": "two::b", "title": "", "description": "", "error": "timeout"},
    ]).to_csv(partial, index=False, encoding="utf-8-sig")

    class Queue:
        def put(self, _item):
            pass

    resumed = load_resume_rows(str(partial), Queue())
    assert set(resumed) == {"one::a"}


def test_copywriting_number_uses_exact_local_product_folder_name():
    assert get_folder_number("01-12") == "01-12"
    assert get_folder_number("product-007-blue") == "product-007-blue"


def test_copywriting_partial_failure_keeps_artifact_and_fails_step(monkeypatch, tmp_path: Path):
    ids = _connections()
    product_root = tmp_path / "products"
    product_root.mkdir()
    output_dir = tmp_path / "outputs"

    with SessionLocal() as session:
        connections = {provider: session.get(IntegrationConnection, connection_id) for provider, connection_id in ids.items()}
        public = {"connections": {
            provider: {"id": row.id, "base_url": row.base_url, "config": loads(row.config_json, {})}
            for provider, row in connections.items()
        }}
        secrets = {provider: vault.decrypt(row.secrets_encrypted) for provider, row in connections.items()}
        job = Job(
            type="self_listing_pipeline", status="running", idempotency_key="copy-partial-failure",
            payload_json=dumps({
                "product_root": str(product_root), "output_dir": str(output_dir),
                "selected_steps": ["generate_copywriting"], "category_hint": "bags",
            }),
            config_snapshot_json=dumps({"public": public, "secrets_encrypted": vault.encrypt(secrets)}),
        )
        session.add(job)
        session.commit()

        def fake_copywriting(_root, config):
            path = Path(config["output_xlsx"])
            path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame([
                {"variant_key": "one::a", "title": "ready", "error": ""},
                {"variant_key": "two::b", "title": "", "error": "timeout"},
            ]).to_excel(path, index=False)
            return str(path)

        monkeypatch.setattr("erp.services.self_listing_pipeline.copywriting.run_copywriting", fake_copywriting)

        with pytest.raises(AppError, match="1 个产品失败"):
            self_listing_pipeline_job(JobContext(session=session, job=job))

        artifacts = session.scalars(select(JobArtifact).where(JobArtifact.job_id == job.id)).all()
        assert [item.role for item in artifacts] == ["copywriting_xlsx"]


def test_main_image_art_direction_varies_by_recognized_product():
    editorial = prepare_sku_cover_plan_item({
        "design_style": "editorial travel poster",
        "layout_direction": "diagonal composition",
    }, {"category_en": "cosmetic_bag", "product_name_ru": "Косметичка"})
    technical = prepare_sku_cover_plan_item({
        "design_style": "clean technical poster",
        "layout_direction": "low-angle product with vertical type",
    }, {"category_en": "power_tool", "product_name_ru": "Аккумуляторная дрель"})
    assert editorial["design_style"] != technical["design_style"]
    assert editorial["layout_direction"] != technical["layout_direction"]
    prompt = prepare_sku_cover_plan_item({}, {
        "category_en": "cosmetic_bag", "product_name_ru": "Косметичка",
    })["image2_prompt"]
    assert "Do not reuse the generic beauty-ad layout" in prompt
    assert "real, recognizable everyday environment" in prompt
    assert "muddy beige or gray color casts" in prompt
    plan = prepare_sku_cover_plan_item({}, {
        "category_en": "cosmetic_bag",
        "product_name_ru": "Косметичка",
        "headline_ru": "Удобная косметичка",
        "benefits_ru": ["Удобно в поездке"],
        "physical_behavior": "flexible",
        "allowed_transformations": ["открытие", "естественный изгиб"],
        "identity_anchors": ["леопардовый рисунок", "две секции"],
        "scene_integration_ru": "сумка стоит на полке и касается поверхности",
    })
    assert plan["image_type"] == "editorial_lifestyle_poster"
    assert plan["needs_text_in_image"] is True
    assert len(plan["text_lines_ru"]) == 2
    assert "Never paste a flat cutout" in plan["image2_prompt"]
    assert "леопардовый рисунок" in plan["image2_prompt"]
    assert "editorial travel poster" in editorial["image2_prompt"]
    assert "clean technical poster" in technical["image2_prompt"]


def test_cover_prompt_compaction_preserves_approved_copy_and_physical_rules():
    plan = prepare_sku_cover_plan_item({"image2_prompt": "planner detail " * 500}, {
        "category_en": "jewelry necklace",
        "product_name_ru": "Necklace",
        "headline_ru": "LIGHT ACCENT",
        "benefits_ru": ["FOR EVERYDAY STYLE"],
        "physical_behavior": "wearable flexible",
        "allowed_transformations": ["natural chain drape around neck"],
        "identity_anchors": ["pendant silhouette"],
        "scene_integration_ru": "chain follows neck curvature with contact shadow",
    })
    compacted = compact_image_edit_prompt(
        build_plan_item_prompt(plan),
        "https://www.micuapi.ai/v1/images/edits",
    )
    assert len(compacted) <= 3000
    assert "natural chain drape around neck" in compacted
    assert "Never paste a flat cutout" in compacted
    assert "LIGHT ACCENT" in compacted
    assert "FOR EVERYDAY STYLE" in compacted
