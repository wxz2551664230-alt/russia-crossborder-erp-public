from __future__ import annotations

import base64
import io
import json
import math
import mimetypes
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageOps
from sqlalchemy import select

from erp.core import ai_usage
from erp.core.config import get_settings
from erp.core.errors import AppError, NotFoundError
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.models import IntegrationConnection
from erp.services.assets import register_local_asset
from erp.services.jobs import JobContext, job_handler

from .super_resolution import find_realesrgan_executable, upscale_with_realesrgan


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MAX_UPLOAD_BYTES = 30_000_000
MAX_BATCH_IMAGES = 30


def workspace_root() -> Path:
    root = (get_settings().data_dir / "pod" / "ai_design").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def batch_dir(batch_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", batch_id):
        raise NotFoundError("AI 生图批次不存在")
    path = (workspace_root() / batch_id).resolve()
    if not path.is_relative_to(workspace_root()):
        raise NotFoundError("AI 生图批次不存在")
    return path


def _manifest_path(batch_id: str) -> Path:
    return batch_dir(batch_id) / "manifest.json"


def load_manifest(batch_id: str) -> dict[str, Any]:
    path = _manifest_path(batch_id)
    if not path.is_file():
        raise NotFoundError("AI 生图批次不存在")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError("AI 生图批次记录损坏", code="ai_design_manifest_invalid") from exc


def save_manifest(value: dict[str, Any]) -> None:
    value["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = _manifest_path(str(value["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def public_manifest(value: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(value, ensure_ascii=False))
    for collection in ("references", "inputs", "outputs"):
        for item in result.get(collection, []):
            item.pop("path", None)
    result["reference_count"] = len(result.get("references", []))
    result["output_count"] = len(result.get("outputs", []))
    return result


def list_batches(limit: int = 50) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in workspace_root().glob("*/manifest.json"):
        try:
            rows.append(public_manifest(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError):
            continue
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows[:limit]


def inspect_image(path: Path) -> dict[str, Any]:
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            return {"width": image.width, "height": image.height, "mode": image.mode}
    except Exception as exc:
        raise AppError(f"图片无法解析：{path.name}", code="invalid_image") from exc


def save_upload(batch_id: str, filename: str, content: bytes, *, folder: str = "references") -> dict[str, Any]:
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise AppError("单张图片必须大于 0 且不超过 30 MB", code="image_size_invalid")
    suffix = Path(filename).suffix.casefold()
    if suffix not in IMAGE_SUFFIXES:
        raise AppError("仅支持 PNG、JPG、JPEG、WebP 图片", code="image_type_invalid")
    target_dir = batch_dir(batch_id) / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4().hex}{suffix}"
    target.write_bytes(content)
    details = inspect_image(target)
    return {
        "name": Path(filename).name,
        "stored_name": target.name,
        "path": str(target),
        "url": f"/api/v1/pod/ai-design/batches/{batch_id}/files/{folder}/{target.name}",
        "size_bytes": len(content),
        **details,
    }


def calculate_size(ratio: str, max_edge: int) -> str:
    match = re.fullmatch(r"\s*(\d+)\s*:\s*(\d+)\s*", ratio)
    if not match:
        raise AppError("画布比例格式应为 4:3、3:4 或 1:1", code="invalid_aspect_ratio")
    left, right = int(match.group(1)), int(match.group(2))
    if left <= 0 or right <= 0 or max(left / right, right / left) > 3:
        raise AppError("画布比例必须在 1:3 到 3:1 之间", code="invalid_aspect_ratio")
    edge = max(256, min(int(max_edge), 3840))
    if left >= right:
        width, height = edge, edge * right / left
    else:
        width, height = edge * left / right, edge
    width, height = max(16, int(width) // 16 * 16), max(16, int(height) // 16 * 16)
    pixels = width * height
    if pixels > 8_294_400:
        factor = math.sqrt(8_294_400 / pixels)
        width, height = int(width * factor) // 16 * 16, int(height * factor) // 16 * 16
    return f"{width}x{height}"


def _connection(context: JobContext, connection_id: str, provider: str) -> tuple[IntegrationConnection, dict[str, Any], dict[str, str]]:
    row = context.session.get(IntegrationConnection, connection_id)
    if not row or row.provider != provider or not row.enabled:
        raise AppError(f"请选择已启用的 {provider} 接口连接", code="ai_connection_unavailable")
    secrets = vault.decrypt(row.secrets_encrypted) if row.secrets_encrypted else {}
    if not secrets.get("api_key"):
        raise AppError("AI 接口连接缺少 api_key", code="ai_connection_incomplete")
    return row, loads(row.config_json, {}), secrets


def _endpoint(row: IntegrationConnection, config: dict[str, Any], kind: str) -> str:
    value = str(config.get(f"{kind}_endpoint") or row.base_url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AppError("AI 接口地址必须是完整的 HTTP(S) 地址", code="ai_endpoint_invalid")
    return value


def _request(kind: str, method: str, url: str, *, model: str, **kwargs: Any) -> dict[str, Any]:
    identity = ai_usage.begin(kind, url, kwargs, model_hint=model)
    response: httpx.Response | None = None
    body: dict[str, Any] | None = None
    error: Exception | None = None
    try:
        with httpx.Client(timeout=httpx.Timeout(600.0), follow_redirects=True) as client:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise AppError("AI 接口没有返回 JSON 对象", code="ai_response_invalid")
            return body
    except Exception as exc:
        error = exc
        if isinstance(exc, AppError):
            raise
        detail = ""
        if response is not None:
            detail = response.text[-500:]
        raise AppError(f"AI 接口调用失败：{detail or exc}", code="ai_request_failed") from exc
    finally:
        ai_usage.finish(identity, response=response, body=body, error=error)


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(item.get("text") or "") for item in value if isinstance(item, dict))
    return str(value or "")


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise AppError("规划模型没有返回有效 JSON", code="ai_plan_invalid")
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise AppError("规划模型没有返回 JSON 对象", code="ai_plan_invalid")
    return value


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _planner_prompt(
    manifest: dict[str, Any],
    *,
    count: int | None = None,
    start_index: int = 1,
    previous_titles: list[str] | None = None,
) -> str:
    config = manifest["config"]
    requested = int(count or config["count"])
    end_index = start_index + requested - 1
    previous = "、".join(previous_titles or []) or "无"
    return f"""你是俄罗斯电商 POD 印花设计总监。请只规划本批 {requested} 个彼此明显不同、可直接用于高清印刷的设计，对应全局编号 {start_index}-{end_index}。
产品载体：{config['product_type']}
画布比例：{config['aspect_ratio']}，基础尺寸：{config['size']}
核心需求：{config['brief']}
补充要求：{config.get('custom_instruction') or '无'}
此前已经完成的方案：{previous}。本批不得与它们重复。
背景：{config['background']}。避免商标、水印、错误文字和商品场景；输出独立平面印花。
每个 prompt 必须为英文并明确 print-ready、flat artwork、exact aspect ratio。
只返回合法 JSON：{{"summary":"中文批次策略","audience":"中文人群","palette":["颜色"],"items":[{{"index":1,"title":"中文方案名","concept":"中文简述","prompt":"English prompt","negative_prompt":"English exclusions","selected":true}}]}}"""


@job_handler("pod_ai_design_plan")
def plan_job(context: JobContext) -> None:
    batch_id = str(context.payload["batch_id"])
    manifest = load_manifest(batch_id)
    context.start_step("plan")
    row, config, secrets = _connection(context, str(manifest["config"]["planner_connection_id"]), "ai_chat")
    endpoint = _endpoint(row, config, "planner")
    model = str(manifest["config"].get("planner_model") or config.get("model") or "gpt-5.6-sol")
    content: list[dict[str, Any]] = [{"type": "text", "text": _planner_prompt(manifest)}]
    for reference in manifest.get("references", [])[:16]:
        content.append({"type": "image_url", "image_url": {"url": _data_url(Path(reference["path"]))}})
    requested = int(manifest["config"]["count"])
    items: list[dict[str, Any]] = []
    summaries: list[str] = []
    audience = ""
    palette: list[str] = []
    seen_prompts: set[str] = set()
    while len(items) < requested:
        chunk_count = min(10, requested - len(items))
        content[0]["text"] = _planner_prompt(
            manifest,
            count=chunk_count,
            start_index=len(items) + 1,
            previous_titles=[str(item["title"]) for item in items[-20:]],
        )
        payload = {"model": model, "messages": [{"role": "user", "content": content}]}
        body = _request("text", "POST", endpoint, model=model, headers={"Authorization": f"Bearer {secrets['api_key']}"}, json=payload)
        try:
            raw = _message_text(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise AppError("规划接口缺少 choices[0].message.content", code="ai_plan_response_invalid") from exc
        parsed = _parse_json_object(raw)
        added = 0
        for item in parsed.get("items") or []:
            if not isinstance(item, dict) or not str(item.get("prompt") or "").strip():
                continue
            prompt = str(item["prompt"]).strip()
            prompt_key = re.sub(r"\s+", " ", prompt).casefold()
            if prompt_key in seen_prompts:
                continue
            seen_prompts.add(prompt_key)
            index = len(items) + 1
            items.append({
                "index": index,
                "title": str(item.get("title") or f"设计方案 {index}")[:200],
                "concept": str(item.get("concept") or "")[:2000],
                "prompt": prompt,
                "negative_prompt": str(item.get("negative_prompt") or "").strip(),
                "selected": bool(item.get("selected", True)),
                "status": "待生成",
            })
            added += 1
            if len(items) >= requested:
                break
        summary = str(parsed.get("summary") or "").strip()
        if summary and summary not in summaries:
            summaries.append(summary)
        audience = audience or str(parsed.get("audience") or "").strip()
        for color in parsed.get("palette") or []:
            if str(color) not in palette:
                palette.append(str(color))
        context.set_progress("plan", len(items) * 100 / requested, f"已规划 {len(items)}/{requested} 个方案")
        if not added:
            break
    if not items:
        raise AppError("规划结果没有有效的生图提示词", code="ai_plan_empty")
    if len(items) < requested:
        raise AppError(f"规划结果只有 {len(items)}/{requested} 个不重复方案，请重试", code="ai_plan_incomplete")
    manifest["plan"] = {"summary": "；".join(summaries), "audience": audience, "palette": palette, "items": items[:requested]}
    manifest["status"] = "planned"
    save_manifest(manifest)
    context.complete_step("plan", details={"items": len(manifest["plan"]["items"]), "model": model})


def _find_image(value: Any) -> tuple[bytes, str] | None:
    if isinstance(value, dict):
        for key in ("b64_json", "base64", "image_base64"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                text = raw.split(",", 1)[-1] if raw.lstrip().startswith("data:") else raw
                return base64.b64decode(re.sub(r"\s+", "", text)), "png"
        for key in ("url", "image_url"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.startswith(("http://", "https://")):
                response = httpx.get(raw, timeout=120, follow_redirects=True)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                ext = "jpg" if "jpeg" in content_type else "webp" if "webp" in content_type else "png"
                return response.content, ext
        for nested in value.values():
            found = _find_image(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_image(nested)
            if found:
                return found
    elif isinstance(value, str):
        match = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=\s]+)", value)
        if match:
            return base64.b64decode(re.sub(r"\s+", "", match.group(1))), "png"
        match = re.search(r"https?://[^\s<>)\]]+", value)
        if match:
            response = httpx.get(match.group(0), timeout=120, follow_redirects=True)
            response.raise_for_status()
            return response.content, "png"
    return None


def _generate_one(context: JobContext, manifest: dict[str, Any], prompt: str) -> tuple[bytes, str, str]:
    row, connection_config, secrets = _connection(context, str(manifest["config"]["image_connection_id"]), "ai_image")
    endpoint = _endpoint(row, connection_config, "image")
    model = str(manifest["config"].get("image_model") or connection_config.get("model") or "gpt-image-2-openai")
    cfg = manifest["config"]
    headers = {"Authorization": f"Bearer {secrets['api_key']}"}
    refs = [Path(item["path"]) for item in manifest.get("references", [])]
    if refs:
        files = []
        field = str(connection_config.get("image_field") or "image[]")
        for path in refs[:16]:
            files.append((field, (path.name, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/png")))
        data = {"model": model, "prompt": prompt, "n": "1", "size": cfg["size"], "quality": cfg["quality"], "background": cfg["background"], "output_format": cfg["output_format"], "response_format": "b64_json"}
        body = _request("image", "POST", endpoint, model=model, headers=headers, data=data, files=files)
    else:
        headers["Content-Type"] = "application/json"
        data = {"model": model, "messages": [{"role": "user", "content": prompt + "\nGenerate the image now."}], "n": 1, "size": cfg["size"], "quality": cfg["quality"], "background": cfg["background"], "output_format": cfg["output_format"]}
        body = _request("image", "POST", endpoint, model=model, headers=headers, json=data)
    found = _find_image(body)
    if not found:
        raise AppError("生图接口返回中没有图片", code="ai_image_missing")
    return found[0], found[1], model


def _write_verified(path: Path, content: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    inspect_image(temporary)
    temporary.replace(path)


def _convert_output(content: bytes, output_format: str) -> tuple[bytes, str]:
    normalized = output_format.casefold().replace("jpeg", "jpg")
    try:
        with Image.open(io.BytesIO(content)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            output = io.BytesIO()
            if normalized == "jpg":
                image.convert("RGB").save(output, format="JPEG", quality=95, subsampling=0)
            elif normalized == "webp":
                image.save(output, format="WEBP", quality=95, method=6)
            else:
                image.save(output, format="PNG", optimize=True)
                normalized = "png"
            return output.getvalue(), normalized
    except Exception as exc:
        raise AppError("生图接口返回了无效图片", code="ai_image_invalid") from exc


@job_handler("pod_ai_design_generate")
def generate_job(context: JobContext) -> None:
    batch_id = str(context.payload["batch_id"])
    manifest = load_manifest(batch_id)
    items = [item for item in manifest.get("plan", {}).get("items", []) if item.get("selected", True)]
    if not items:
        raise AppError("没有选择需要生成的方案", code="ai_design_no_selection")
    output_dir = batch_dir(batch_id) / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest["status"] = "generating"
    save_manifest(manifest)
    context.start_step("generate")
    completed = 0
    outputs = manifest.setdefault("outputs", [])
    existing_indexes = {int(row.get("index") or 0) for row in outputs if row.get("status") == "completed"}
    for item in items:
        context.refresh()
        index = int(item["index"])
        if index in existing_indexes:
            completed += 1
            continue
        item["status"] = "生成中"
        save_manifest(manifest)
        try:
            prompt = str(item["prompt"])
            negative = str(item.get("negative_prompt") or "")
            if negative:
                prompt += f"\nAvoid: {negative}"
            content, extension, model = _generate_one(context, manifest, prompt)
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
            output_format = str(manifest["config"].get("output_format") or extension).replace("jpeg", "jpg")
            content, output_format = _convert_output(content, output_format)
            scale = int(manifest["config"].get("upscale_scale") or 0)
            metadata: dict[str, Any] = {"super_resolution_enabled": False}
            if scale:
                content, metadata = upscale_with_realesrgan(content, extension, scale)
            path = output_dir / f"{index:03d}_{re.sub(r'[^\w\u4e00-\u9fff-]+', '_', str(item['title']))[:60]}.{output_format}"
            _write_verified(path, content)
            details = inspect_image(path)
            asset = register_local_asset(context.session, path, kind="pod_ai_design", source="pod_ai_design", read_only=False, calculate_hash=True, metadata={"batch_id": batch_id, "plan_index": index, **metadata}, legacy_source="pod_ai_design", legacy_id=f"{batch_id}:{index}")
            context.session.commit()
            output = {"index": index, "title": item["title"], "status": "completed", "path": str(path), "url": f"/api/v1/pod/ai-design/batches/{batch_id}/files/outputs/{path.name}", "asset_id": asset.id, "model": model, "size_bytes": len(content), **details, "metadata": metadata}
            outputs.append(output)
            item["status"] = "已完成"
            completed += 1
            context.set_progress("generate", completed * 100 / len(items), f"已生成 {completed}/{len(items)} 张")
        except Exception as exc:
            item["status"] = "失败"
            if isinstance(exc,AppError) and exc.code=='ai_write_outcome_unknown':raise
            item["error"] = str(exc)
            outputs.append({"index": index, "title": item["title"], "status": "failed", "error": str(exc)})
            context.event(f"第 {index} 张生成失败：{exc}", level="warning")
        save_manifest(manifest)
    failures = sum(row.get("status") == "failed" for row in outputs)
    manifest["status"] = "completed" if completed else "failed"
    save_manifest(manifest)
    if not completed:
        raise AppError("本批图片全部生成失败", code="ai_design_generation_failed")
    context.complete_step("generate", details={"completed": completed, "failed": failures})


@job_handler("pod_ai_design_upscale")
def upscale_job(context: JobContext) -> None:
    batch_id = str(context.payload["batch_id"])
    manifest = load_manifest(batch_id)
    scale = int(manifest["config"]["upscale_scale"])
    context.start_step("upscale")
    output_dir = batch_dir(batch_id) / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    completed = 0
    for index, source in enumerate(manifest.get("inputs", []), 1):
        context.refresh()
        source_path = Path(source["path"])
        try:
            content, metadata = upscale_with_realesrgan(source_path.read_bytes(), source_path.suffix.lstrip("."), scale)
            path = output_dir / source_path.name
            _write_verified(path, content)
            details = inspect_image(path)
            asset = register_local_asset(context.session, path, kind="pod_ai_upscaled", source="pod_ai_design", read_only=False, calculate_hash=True, metadata={"batch_id": batch_id, **metadata}, legacy_source="pod_ai_upscaled", legacy_id=f"{batch_id}:{index}")
            context.session.commit()
            manifest.setdefault("outputs", []).append({"index": index, "title": source["name"], "status": "completed", "path": str(path), "url": f"/api/v1/pod/ai-design/batches/{batch_id}/files/outputs/{path.name}", "asset_id": asset.id, "size_bytes": len(content), **details, "metadata": metadata})
            completed += 1
        except Exception as exc:
            # Asset registration may fail during a transient SQLite write lock.
            # Clear the failed transaction before recording this item's error
            # or updating job progress; otherwise the worker exits with
            # PendingRollbackError and leaves the remaining batch stranded.
            context.session.rollback()
            manifest.setdefault("outputs", []).append({"index": index, "title": source["name"], "status": "failed", "error": str(exc)})
        context.set_progress("upscale", index * 100 / max(1, len(manifest.get("inputs", []))), f"已处理 {index}/{len(manifest.get('inputs', []))} 张")
        save_manifest(manifest)
    manifest["status"] = "completed" if completed else "failed"
    save_manifest(manifest)
    if not completed:
        raise AppError("本批图片全部超分失败", code="ai_upscale_failed")
    context.complete_step("upscale", details={"completed": completed, "failed": len(manifest.get("inputs", [])) - completed})


def runtime_status() -> dict[str, Any]:
    executable = find_realesrgan_executable()
    return {"available": bool(executable), "engine": "realesrgan-ncnn-vulkan", "model": "realesrgan-x4plus", "executable": str(executable) if executable else "", "token_usage": False, "output_limit_bytes": 30_000_000}
