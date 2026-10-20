import os
import json
import base64
import re
from erp.core.ai_concurrency import async_post as ai_async_post
import asyncio
import aiohttp
import threading
import queue
import argparse
import time
import glob
from io import BytesIO
from PIL import Image
import pandas as pd
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# ======================
# 默认配置
# ======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "api_host": "https://api.gpt.ge",
    "api_path": "/v1/chat/completions",
    "api_token": "",
    "model": "gpt-5.4",

    "category_hint": "пижама для малышей",
    "page_keywords": "",
    "output_xlsx": os.path.join(BASE_DIR, "ozon_output_full.xlsx"),
    "source_subfolder": "",

    "title_target_chars": 150,
    "title_max_chars": 200,
    "max_tokens": 900,
    "max_images_per_product": 5,

    "desc_min_chars": 400,
    "desc_max_chars": 470,

    "concurrency_limit": 5,
    "request_timeout": 180,
    "request_retries": 3
}


def natural_sort_key(value):
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(value))]


def get_variant_name(sku_path):
    if not sku_path:
        return "default"
    stem = os.path.splitext(os.path.basename(sku_path))[0].strip()
    if stem in {"(无SKU)", "default"}:
        return "default"
    if stem.casefold().startswith("cover_"):
        stem = stem[len("cover_"):]
    return stem or "default"


def get_folder_number(folder_name):
    # The import template uses this column to join generated copy back to the
    # local product directory.  Keep the exact directory name (for example
    # ``01-12``) instead of extracting its first number and turning every
    # product in the batch into ``001``.
    return str(folder_name or "").strip()


def get_color_from_sku_name(sku_path):
    variant_name = get_variant_name(sku_path)
    if not variant_name or variant_name == "default":
        return ""

    parts = [part.strip() for part in re.split(r"[-_]", variant_name) if part.strip()]
    if len(parts) >= 3 and parts[0].casefold() == "sku":
        return parts[-1]
    if len(parts) >= 2 and parts[0].casefold().startswith("sku"):
        return parts[-1]
    if variant_name.casefold().startswith("sku") and len(variant_name) > 3:
        return variant_name[3:].strip("-_ ")
    return parts[-1] if len(parts) > 1 else variant_name


def build_variant_key(folder_name, sku_path):
    return f"{str(folder_name).strip()}::{get_variant_name(sku_path)}"


def timestamped_output_path(path, label="recovered"):
    base, ext = os.path.splitext(path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{base}.{label}-{stamp}{ext}"


def atomic_save_csv(df, path):
    path = os.path.abspath(path)
    temp_path = f"{path}.{os.getpid()}.tmp"
    try:
        df.to_csv(temp_path, index=False, encoding="utf-8-sig")
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def save_partial_csv(df, preferred_path, ui_queue, current_path=None):
    target_path = current_path or preferred_path
    try:
        atomic_save_csv(df, target_path)
        return target_path
    except PermissionError as exc:
        fallback_path = timestamped_output_path(preferred_path)
        try:
            atomic_save_csv(df, fallback_path)
            ui_queue.put((
                "log",
                f"   [警告] partial CSV 被占用，已改存: {fallback_path} ({exc})",
            ))
            return fallback_path
        except Exception as fallback_exc:
            ui_queue.put((
                "log",
                f"   [警告] partial CSV 保存失败，文案任务继续: {fallback_exc}",
            ))
            return current_path
    except Exception as exc:
        ui_queue.put(("log", f"   [警告] partial CSV 保存失败，文案任务继续: {exc}"))
        return current_path


def save_final_xlsx(df, preferred_path, ui_queue):
    def write_xlsx(path):
        path = os.path.abspath(path)
        base, ext = os.path.splitext(path)
        temp_path = f"{base}.{os.getpid()}.tmp{ext or '.xlsx'}"
        try:
            with pd.ExcelWriter(temp_path, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="products")
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    try:
        write_xlsx(preferred_path)
        return preferred_path
    except PermissionError as exc:
        fallback_path = timestamped_output_path(preferred_path)
        write_xlsx(fallback_path)
        ui_queue.put((
            "log",
            f"[警告] 目标 Excel 被占用，完整结果已改存: {fallback_path} ({exc})",
        ))
        return fallback_path


def load_resume_rows(partial_csv, ui_queue):
    base, ext = os.path.splitext(partial_csv)
    candidates = [partial_csv, *glob.glob(f"{base}.recovered-*{ext}")]
    candidates = [path for path in candidates if os.path.isfile(path)]
    candidates.sort(key=os.path.getmtime, reverse=True)

    resumed_by_key = {}
    loaded_files = []
    for path in candidates:
        try:
            frame = pd.read_csv(path, encoding="utf-8-sig")
        except Exception as exc:
            ui_queue.put(("log", f"[警告] 无法读取断点文件，已跳过: {path} ({exc})"))
            continue

        loaded_files.append(path)
        for raw_row in frame.to_dict("records"):
            row = {
                key: "" if pd.isna(value) else value
                for key, value in raw_row.items()
            }
            variant_key = str(row.get("variant_key", "")).strip()
            if not variant_key:
                folder_name = str(row.get("folder_name", "")).strip()
                sku_image = str(row.get("sku_image", "")).strip()
                if folder_name:
                    variant_key = build_variant_key(folder_name, sku_image or None)
            if not variant_key or variant_key in resumed_by_key:
                continue
            if str(row.get("error", "")).strip():
                continue
            if not str(row.get("title", "")).strip() and not str(row.get("description", "")).strip():
                continue
            row["variant_key"] = variant_key
            resumed_by_key[variant_key] = row

    if resumed_by_key:
        ui_queue.put((
            "log",
            f"[断点续跑] 从 {len(loaded_files)} 个 partial 文件恢复 {len(resumed_by_key)} 个成功变体",
        ))
    return resumed_by_key


# ======================
# 配置读写
# ======================
def load_config():
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)

            cfg = DEFAULT_CONFIG.copy()
            cfg.update(data)
            return cfg

        except Exception:
            return DEFAULT_CONFIG.copy()

    return DEFAULT_CONFIG.copy()


def load_config_from_file(path):
    if not path:
        return load_config()

    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)

    cfg = DEFAULT_CONFIG.copy()
    cfg.update(data)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ======================
# 文件与图片处理
# ======================
def list_product_folders(root_dir):
    """
    返回根目录下所有一级子文件夹。
    每个一级子文件夹视为一个产品文件夹。
    """
    items = []

    if not os.path.isdir(root_dir):
        return items

    for name in sorted(os.listdir(root_dir), key=natural_sort_key):
        path = os.path.join(root_dir, name)

        if os.path.isdir(path):
            items.append(path)

    return items


def iter_image_files(root_dir):
    """
    递归遍历所有图片文件。
    """
    exts = (".jpg", ".jpeg", ".png", ".webp", ".bmp")

    for dirpath, _, filenames in os.walk(root_dir):
        for fname in filenames:
            if fname.lower().endswith(exts):
                yield os.path.join(dirpath, fname)


def get_product_variants(folder_path, max_images_per_product):
    """
    识别文件夹中的 SKU 图片。

    如果图片文件名以 sku 开头：
    每个 SKU 图片生成一个变体。
    该 SKU 图作为主图，其他非 SKU 图片作为附图。

    如果没有 SKU 图片：
    整个文件夹视为一个产品。
    """
    all_images = list(iter_image_files(folder_path))
    all_images.sort(key=lambda p: natural_sort_key(os.path.relpath(p, folder_path)))

    sku_images = []
    other_images = []

    for path in all_images:
        base = os.path.basename(path)
        stem = os.path.splitext(base)[0]

        stem_lower = stem.lower()

        if stem_lower.startswith("sku"):
            sku_images.append(path)
        else:
            other_images.append(path)

    if not sku_images:
        cover_sku_images = []
        non_cover_images = []
        for path in other_images:
            stem = os.path.splitext(os.path.basename(path))[0].lower()
            if stem.startswith("cover_sku"):
                cover_sku_images.append(path)
            else:
                non_cover_images.append(path)

        if cover_sku_images:
            sku_images = cover_sku_images
            other_images = non_cover_images

    if not sku_images:
        selected = other_images[:max_images_per_product]

        deduped = []
        seen = set()

        for p in selected:
            if p not in seen:
                deduped.append(p)
                seen.add(p)

        return [(None, deduped)]

    variants = []

    for sku_path in sku_images:
        variant_imgs = [sku_path]

        for p in other_images:
            if len(variant_imgs) >= max_images_per_product:
                break

            if p not in variant_imgs:
                variant_imgs.append(p)

        variants.append((sku_path, variant_imgs))

    return variants


def image_to_data_url(image_path: str, max_side: int = 1024, quality: int = 85) -> str:
    """
    将图片压缩并转为 Data URL。
    """
    img = Image.open(image_path).convert("RGB")

    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    w, h = img.size
    scale = min(max_side / max(w, h), 1.0)

    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)))

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)

    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    return f"data:image/jpeg;base64,{b64}"


DATA_URL_CACHE = {}


def image_to_data_url_cached(image_path: str, max_side: int = 1024, quality: int = 85) -> str:
    stat = os.stat(image_path)
    key = (os.path.abspath(image_path), stat.st_size, stat.st_mtime_ns, max_side, quality)
    if key not in DATA_URL_CACHE:
        DATA_URL_CACHE[key] = image_to_data_url(image_path, max_side=max_side, quality=quality)
    return DATA_URL_CACHE[key]


# ======================
# 关键词处理
# ======================
def parse_keywords_text(text: str) -> list:
    """
    将页面输入的关键词解析成列表。
    支持英文逗号、英文分号、中文逗号、中文分号、换行。
    """
    if not text:
        return []

    parts = re.split(r"[;,，；\n]+", text)
    keywords = [p.strip() for p in parts if p.strip()]

    return keywords


def merge_keywords(*keyword_lists) -> list:
    """
    合并多个关键词列表，保持顺序并去重。
    """
    result = []
    seen = set()

    for keyword_list in keyword_lists:
        for kw in keyword_list:
            kw = kw.strip()
            key = kw.lower()

            if kw and key not in seen:
                result.append(kw)
                seen.add(key)

    return result


def read_keywords_from_folder(folder_path: str) -> list:
    """
    从产品文件夹内读取关键词 TXT 文件。
    支持：
    keywords.txt
    关键词.txt
    文件夹名.txt
    """
    possible_names = [
        "keywords.txt",
        "关键词.txt",
        f"{os.path.basename(folder_path)}.txt"
    ]

    for fname in possible_names:
        txt_path = os.path.join(folder_path, fname)

        if os.path.isfile(txt_path):
            try:
                with open(txt_path, "r", encoding="utf-8") as f:
                    content = f.read()

                return parse_keywords_text(content)

            except Exception:
                return []

    return []


# ======================
# 模型输出处理
# ======================
def extract_json_object(text: str):
    """
    从模型输出中提取 JSON 对象。
    """
    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    m = re.search(r"\{.*\}", text, flags=re.S)

    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass

    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass

    raise ValueError(f"无法从模型输出中解析 JSON：\n{text}")


def normalize_title(title: str, title_max_chars: int) -> str:
    title = str(title or "").strip()

    if len(title) > title_max_chars:
        title = title[:title_max_chars].rstrip()

    return title


def normalize_output(obj: dict, cfg: dict) -> dict:
    title_max_chars = int(cfg["title_max_chars"])
    desc_min_chars = int(cfg["desc_min_chars"])
    desc_max_chars = int(cfg["desc_max_chars"])

    title = normalize_title(obj.get("title", ""), title_max_chars)
    description = str(obj.get("description", "")).strip()

    if len(description) > desc_max_chars:
        truncated = description[:desc_max_chars]
        last_space = truncated.rfind(" ")

        if last_space > 0:
            description = truncated[:last_space]
        else:
            description = truncated

        if len(description) > desc_max_chars:
            description = description[:desc_max_chars]

    keywords = str(obj.get("keywords", "")).strip()
    tags = str(obj.get("tags", "")).strip()

    tag_list = re.findall(r"#\S+", tags)

    if len(tag_list) >= 30:
        tags = " ".join(tag_list[:30])
    else:
        tags = " ".join(tag_list)

    if keywords and not keywords.endswith(";"):
        keywords += ";"

    return {
        "title": title,
        "description": description,
        "keywords": keywords,
        "tags": tags,
        "_desc_len": len(description),
        "_desc_too_short": len(description) < desc_min_chars
    }


# ======================
# API 调用
# ======================
async def build_image_contents(image_paths):
    """
    异步构建图片 content。
    """
    def _convert_all():
        items = []

        for path in image_paths:
            items.append({
                "type": "image_url",
                "image_url": {
                    "url": image_to_data_url_cached(path)
                }
            })

        return items

    return await asyncio.to_thread(_convert_all)


async def call_api_with_images_async(session, image_paths, cfg, custom_keywords=None):
    api_host = cfg["api_host"].rstrip("/")
    api_path = cfg["api_path"]
    api_token = cfg["api_token"]
    model = cfg["model"]
    category_hint = cfg["category_hint"]

    title_target_chars = int(cfg["title_target_chars"])
    title_max_chars = int(cfg["title_max_chars"])
    max_tokens = int(cfg["max_tokens"])
    desc_min_chars = int(cfg["desc_min_chars"])
    desc_max_chars = int(cfg["desc_max_chars"])
    request_timeout = int(cfg["request_timeout"])

    system_prompt = (
        "Ты — помощник по созданию карточек товара для Ozon и Wildberries. "
        "Пиши только по-русски. НЕ используй брендовые названия. НЕ добавляй emoji или иконки. "
        f"Заголовок должен быть примерно {title_target_chars} символов, но строго не длиннее {title_max_chars} символов. "
        f"Описание должно быть от {desc_min_chars} до {desc_max_chars} символов. "
        "Ключевые слова выводи через точку с запятой, с точкой с запятой в конце. "
        "Теги: ровно 30 штук, формат '#тег #тег #тег'. "
        "Верни СТРОГО JSON без markdown и без пояснений."
    )

    selected_names = [os.path.basename(p) for p in image_paths]

    keyword_instruction = ""

    if custom_keywords:
        kw_list = ", ".join(custom_keywords)

        keyword_instruction = (
            f"\n\nОбязательно естественно вписать в description следующие ключевые слова: {kw_list}. "
            "Используй их органично в тексте описания, не простым списком. "
            "Также учти эти слова при формировании keywords."
        )

    user_prompt = (
        f"{category_hint}\n"
        "Определи товар по набору изображений и сгенерируй JSON со следующими полями:\n"
        "title — заголовок товара\n"
        "description — описание товара\n"
        "keywords — ключевые слова через ;\n"
        "tags — 30 тегов через пробел, каждый начинается с #\n\n"
        "Изображения подобраны так: первое фото — главное изображение товара, остальные — дополнительные ракурсы.\n"
        f"Имена файлов: {', '.join(selected_names)}"
        + keyword_instruction
    )

    content = [{"type": "text", "text": user_prompt}]

    image_contents = await build_image_contents(image_paths)
    content.extend(image_contents)

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": content
            },
        ],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }

    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    url = f"{api_host}{api_path}"

    async with ai_async_post("text", session,
        url,
        json=payload,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=request_timeout)
    ) as response:
        raw = await response.text()

        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"HTTP {response.status}: {raw}")

        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]

        return normalize_output(extract_json_object(content), cfg)


# ======================
# 异步任务
# ======================
async def call_with_retries(fn, retries=3):
    last_err = None
    for attempt in range(max(1, int(retries or 1))):
        try:
            return await fn()
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                await asyncio.sleep(min(2 ** attempt, 10))
    raise last_err


async def wait_if_paused(pause_event=None):
    while pause_event is not None and pause_event.is_set():
        await asyncio.sleep(0.2)


async def process_variant(
    session,
    semaphore,
    source_order,
    folder_name,
    sku_path,
    image_paths,
    extra_keywords,
    cfg,
    ui_queue,
    pause_event=None
):
    await wait_if_paused(pause_event)
    sku_display = os.path.basename(sku_path) if sku_path else "(无SKU)"
    variant_name = get_variant_name(sku_path)
    variant_key = build_variant_key(folder_name, sku_path)
    folder_number = get_folder_number(folder_name)
    color_name = get_color_from_sku_name(sku_path)

    ui_queue.put(("log", f"   [处理] 变体: {sku_display}，共 {len(image_paths)} 张图片"))

    if not image_paths:
        ui_queue.put(("progress", 1))

        return {
            "source_order": source_order,
            "编号": folder_number,
            "颜色": color_name,
            "variant_key": variant_key,
            "variant_name": variant_name,
            "folder_name": folder_name,
            "sku_image": sku_display,
            "title": "",
            "description": "",
            "keywords": "",
            "tags": "",
            "error": "no_images"
        }

    async with semaphore:
        await wait_if_paused(pause_event)
        try:
            obj = await call_with_retries(
                lambda: call_api_with_images_async(
                    session,
                    image_paths,
                    cfg,
                    custom_keywords=extra_keywords
                ),
                retries=int(cfg.get("request_retries", 3))
            )

            desc_len = obj.get("_desc_len", 0)

            ui_queue.put(("log", f"   [成功] {folder_name} / {sku_display}，描述长度: {desc_len}"))

            if obj.get("_desc_too_short"):
                ui_queue.put(("log", f"   [警告] {folder_name} / {sku_display} 描述长度偏短，请人工检查"))

            ui_queue.put(("progress", 1))

            return {
                "source_order": source_order,
                "编号": folder_number,
                "颜色": color_name,
                "variant_key": variant_key,
                "variant_name": variant_name,
                "folder_name": folder_name,
                "sku_image": sku_display,
                "title": obj.get("title", ""),
                "description": obj.get("description", ""),
                "keywords": obj.get("keywords", ""),
                "tags": obj.get("tags", ""),
                "error": "",
            }

        except Exception as e:
            ui_queue.put(("log", f"   [错误] {folder_name} / {sku_display}: {e}"))
            ui_queue.put(("progress", 1))

            return {
                "source_order": source_order,
                "编号": folder_number,
                "颜色": color_name,
                "variant_key": variant_key,
                "variant_name": variant_name,
                "folder_name": folder_name,
                "sku_image": sku_display,
                "title": "",
                "description": "",
                "keywords": "",
                "tags": "",
                "error": str(e)
            }


async def async_worker(image_dir, cfg, ui_queue, pause_event=None):
    product_folders = list_product_folders(image_dir)

    if not product_folders:
        ui_queue.put(("error", f"No product folders found under: {image_dir}"))
        return

    all_jobs = []
    max_images_per_product = int(cfg["max_images_per_product"])
    page_keywords = parse_keywords_text(cfg.get("page_keywords", ""))

    if page_keywords:
        ui_queue.put(("log", f"[page keywords] {len(page_keywords)} keywords will be applied to all products"))

    for folder in product_folders:
        folder_name = os.path.basename(folder)
        source_subfolder = str(cfg.get("source_subfolder", "") or "").strip()
        image_folder = folder

        if source_subfolder:
            candidate = os.path.join(folder, source_subfolder)
            if not os.path.isdir(candidate):
                ui_queue.put(("log", f"[skip] {folder_name}: missing image subfolder {source_subfolder}"))
                continue
            image_folder = candidate

        variants = get_product_variants(image_folder, max_images_per_product)
        folder_keywords = read_keywords_from_folder(folder)
        extra_keywords = merge_keywords(page_keywords, folder_keywords)

        if not variants:
            all_jobs.append((len(all_jobs) + 1, folder_name, None, [], extra_keywords))
        else:
            for sku_path, image_paths in variants:
                all_jobs.append((len(all_jobs) + 1, folder_name, sku_path, image_paths, extra_keywords))

    output_xlsx = cfg["output_xlsx"]
    partial_csv = os.path.splitext(output_xlsx)[0] + ".partial.csv"
    resumed_by_key = load_resume_rows(partial_csv, ui_queue)
    resumed_rows = []
    pending_jobs = []
    for job in all_jobs:
        source_order, folder_name, sku_path, image_paths, extra_keywords = job
        variant_key = build_variant_key(folder_name, sku_path)
        resumed = resumed_by_key.get(variant_key)
        if not resumed:
            pending_jobs.append(job)
            continue
        row = dict(resumed)
        row.update({
            "source_order": source_order,
            "编号": get_folder_number(folder_name),
            "颜色": get_color_from_sku_name(sku_path),
            "variant_key": variant_key,
            "variant_name": get_variant_name(sku_path),
            "folder_name": folder_name,
            "sku_image": os.path.basename(sku_path) if sku_path else "(无SKU)",
            "error": "",
        })
        resumed_rows.append(row)

    total = len(all_jobs)
    ui_queue.put(("total", total))
    ui_queue.put(("log", f"[start] product folders: {len(product_folders)}, copywriting tasks: {total}"))
    if resumed_rows:
        ui_queue.put(("log", f"[断点续跑] 已完成 {len(resumed_rows)}，待生成 {len(pending_jobs)}"))
        ui_queue.put(("progress", len(resumed_rows)))

    if total == 0:
        ui_queue.put(("done", cfg["output_xlsx"]))
        return cfg["output_xlsx"]

    concurrency_limit = max(1, int(cfg["concurrency_limit"]))
    semaphore = asyncio.Semaphore(concurrency_limit)
    checkpoint_lock = asyncio.Lock()
    partial_output_path = None

    connector = aiohttp.TCPConnector(limit=concurrency_limit)
    rows = list(resumed_rows)

    async with aiohttp.ClientSession(connector=connector) as session:
        job_queue = asyncio.Queue()
        for job in pending_jobs:
            job_queue.put_nowait(job)

        logged_folders = set()

        async def queue_worker():
            nonlocal partial_output_path
            while True:
                try:
                    source_order, folder_name, sku_path, image_paths, extra_keywords = job_queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                try:
                    await wait_if_paused(pause_event)

                    if folder_name not in logged_folders:
                        logged_folders.add(folder_name)
                        ui_queue.put(("log", f"\n[folder] {folder_name}"))

                        if extra_keywords:
                            preview = ", ".join(extra_keywords[:8])
                            suffix = "..." if len(extra_keywords) > 8 else ""
                            ui_queue.put(("log", f"   [keywords] {len(extra_keywords)}: {preview}{suffix}"))

                    row = await process_variant(
                        session,
                        semaphore,
                        source_order,
                        folder_name,
                        sku_path,
                        image_paths,
                        extra_keywords,
                        cfg,
                        ui_queue,
                        pause_event=pause_event,
                    )
                    # Persist every completed variant. A task can be interrupted at
                    # any time, so a batch-sized checkpoint would lose otherwise
                    # successful copy generated since the previous flush.
                    async with checkpoint_lock:
                        rows.append(row)
                        partial_rows = sorted(rows, key=lambda item: item["source_order"])
                        partial_output_path = save_partial_csv(
                            pd.DataFrame(partial_rows),
                            partial_csv,
                            ui_queue,
                            current_path=partial_output_path,
                        )
                        if partial_output_path:
                            ui_queue.put(("log", f"   [checkpoint] saved: {partial_output_path}"))
                finally:
                    job_queue.task_done()

        worker_count = min(concurrency_limit, len(pending_jobs))
        workers = [asyncio.create_task(queue_worker()) for _ in range(worker_count)]
        await job_queue.join()
        await asyncio.gather(*workers)

    rows.sort(key=lambda item: item["source_order"])
    df = pd.DataFrame(
        rows,
        columns=[
            "source_order",
            "编号",
            "颜色",
            "variant_key",
            "variant_name",
            "folder_name",
            "sku_image",
            "title",
            "description",
            "keywords",
            "tags",
            "error",
        ],
    )

    partial_output_path = save_partial_csv(
        df,
        partial_csv,
        ui_queue,
        current_path=partial_output_path,
    )
    actual_output_xlsx = save_final_xlsx(df, output_xlsx, ui_queue)

    ui_queue.put(("log", f"\n[done] saved: {actual_output_xlsx}"))
    ui_queue.put(("done", actual_output_xlsx))
    return actual_output_xlsx


class ConsoleQueue:
    """Small queue adapter so async_worker can run without the Tk GUI."""
    def __init__(self):
        self.total = 0
        self.done = 0

    def put(self, item):
        msg_type, payload = item
        if msg_type == "log":
            print(payload)
        elif msg_type == "total":
            self.total = int(payload)
            self.done = 0
            print(f"[进度] 0 / {self.total}")
        elif msg_type == "progress":
            self.done += int(payload)
            if self.total:
                print(f"[进度] {self.done} / {self.total}")
        elif msg_type == "done":
            print(f"[完成] {payload}")
        elif msg_type == "error":
            print(f"[错误] {payload}")


def validate_run_config(image_dir, cfg):
    if not image_dir:
        raise ValueError("图片根目录不能为空")
    if not os.path.isdir(image_dir):
        raise ValueError(f"图片根目录不存在: {image_dir}")
    if not cfg.get("api_token"):
        raise ValueError("API Token 不能为空")
    if not cfg.get("api_host"):
        raise ValueError("API Host 不能为空")
    if not cfg.get("api_path", "").startswith("/"):
        raise ValueError("API Path 必须以 / 开头，例如 /v1/chat/completions")
    if not cfg.get("model"):
        raise ValueError("模型名称不能为空")
    if not cfg.get("category_hint"):
        raise ValueError("产品类型/类目提示不能为空")
    if not cfg.get("output_xlsx"):
        raise ValueError("输出 Excel 路径不能为空")

    out_dir = os.path.dirname(cfg["output_xlsx"])
    if out_dir and not os.path.isdir(out_dir):
        raise ValueError(f"输出目录不存在: {out_dir}")

    if int(cfg.get("concurrency_limit", 0)) <= 0:
        raise ValueError("并发数必须大于 0")
    if int(cfg.get("max_images_per_product", 0)) <= 0:
        raise ValueError("每个产品最大图片数必须大于 0")


def run_copywriting(image_dir, cfg=None, save_current_config=False, pause_event=None):
    """Generate copywriting Excel. Can be imported by pipeline.py."""
    cfg = (cfg or load_config()).copy()
    validate_run_config(image_dir, cfg)
    if save_current_config:
        save_config(cfg)
    return asyncio.run(async_worker(image_dir, cfg, ConsoleQueue(), pause_event=pause_event))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="批量生成 OZON 商品文案 Excel")
    parser.add_argument("--config", help="指定 JSON 配置文件；不填则读取默认 config.json")
    parser.add_argument("--image-dir", help="图片根目录，目录下每个一级子文件夹视为一个产品")
    parser.add_argument("--output-xlsx", help="输出 Excel 路径")
    parser.add_argument("--api-host", help="API Host，例如 https://api.gpt.ge")
    parser.add_argument("--api-path", help="API Path，例如 /v1/chat/completions")
    parser.add_argument("--api-token", help="API Token")
    parser.add_argument("--model", help="模型名称")
    parser.add_argument("--category-hint", help="产品类型/类目提示")
    parser.add_argument("--page-keywords", help="应用到所有产品的页面关键词")
    parser.add_argument("--source-subfolder", help="只读取每个产品目录下的指定子目录，例如 _generated")
    parser.add_argument("--max-images-per-product", type=int)
    parser.add_argument("--concurrency-limit", type=int)
    parser.add_argument("--request-timeout", type=int)
    parser.add_argument("--save-config", action="store_true", help="运行前把参数保存到 config.json")
    parser.add_argument("--no-gui", action="store_true", help="未提供 --image-dir 时不打开 GUI")
    return parser.parse_args(argv)


def run_cli(args):
    cfg = load_config_from_file(args.config)
    overrides = {
        "output_xlsx": args.output_xlsx,
        "api_host": args.api_host,
        "api_path": args.api_path,
        "api_token": args.api_token,
        "model": args.model,
        "category_hint": args.category_hint,
        "page_keywords": args.page_keywords,
        "source_subfolder": args.source_subfolder,
        "max_images_per_product": args.max_images_per_product,
        "concurrency_limit": args.concurrency_limit,
        "request_timeout": args.request_timeout,
    }

    for key, value in overrides.items():
        if value is not None:
            cfg[key] = value

    return run_copywriting(args.image_dir, cfg, save_current_config=args.save_config)


# ======================
# GUI
# ======================
class OzonGuiApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Ozon / Wildberries 商品文案生成工具")
        self.root.geometry("1050x820")

        self.cfg = load_config()
        self.ui_queue = queue.Queue()

        self.running = False
        self.total_tasks = 0
        self.done_tasks = 0

        self.build_ui()
        self.load_config_to_ui()

        self.root.after(100, self.poll_queue)

    def build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # 图片目录
        folder_frame = ttk.LabelFrame(main, text="图片目录")
        folder_frame.pack(fill="x", pady=5)

        self.image_dir_var = tk.StringVar()

        ttk.Entry(folder_frame, textvariable=self.image_dir_var).pack(
            side="left",
            fill="x",
            expand=True,
            padx=5,
            pady=8
        )

        ttk.Button(
            folder_frame,
            text="选择文件夹",
            command=self.choose_folder
        ).pack(side="left", padx=5)

        # API 配置
        api_frame = ttk.LabelFrame(main, text="API 配置")
        api_frame.pack(fill="x", pady=5)

        self.api_host_var = tk.StringVar()
        self.api_path_var = tk.StringVar()
        self.api_token_var = tk.StringVar()
        self.model_var = tk.StringVar()

        self.add_labeled_entry(api_frame, "API Host", self.api_host_var, 0, 0)
        self.add_labeled_entry(api_frame, "API Path", self.api_path_var, 1, 0)
        self.add_labeled_entry(api_frame, "API Token", self.api_token_var, 2, 0, show="*")
        self.add_labeled_entry(api_frame, "模型 Model", self.model_var, 3, 0)

        # 商品配置
        product_frame = ttk.LabelFrame(main, text="商品与生成配置")
        product_frame.pack(fill="x", pady=5)

        self.category_hint_var = tk.StringVar()
        self.output_xlsx_var = tk.StringVar()
        self.source_subfolder_var = tk.StringVar()

        self.title_target_var = tk.StringVar()
        self.title_max_var = tk.StringVar()
        self.max_tokens_var = tk.StringVar()
        self.max_images_var = tk.StringVar()
        self.desc_min_var = tk.StringVar()
        self.desc_max_var = tk.StringVar()
        self.concurrency_var = tk.StringVar()
        self.timeout_var = tk.StringVar()

        self.add_labeled_entry(product_frame, "产品类型 / 类目提示", self.category_hint_var, 0, 0)

        self.add_labeled_entry(product_frame, "输出 Excel 路径", self.output_xlsx_var, 1, 0)

        ttk.Button(
            product_frame,
            text="选择保存位置",
            command=self.choose_output
        ).grid(row=1, column=2, padx=5, pady=4)

        self.add_labeled_entry(product_frame, "图片子目录", self.source_subfolder_var, 2, 0)

        self.add_labeled_entry(product_frame, "标题目标字符", self.title_target_var, 3, 0)
        self.add_labeled_entry(product_frame, "标题最大字符", self.title_max_var, 3, 2)

        self.add_labeled_entry(product_frame, "描述最小字符", self.desc_min_var, 4, 0)
        self.add_labeled_entry(product_frame, "描述最大字符", self.desc_max_var, 4, 2)

        self.add_labeled_entry(product_frame, "最大 Tokens", self.max_tokens_var, 5, 0)
        self.add_labeled_entry(product_frame, "每产品最多图片", self.max_images_var, 5, 2)

        self.add_labeled_entry(product_frame, "并发数", self.concurrency_var, 6, 0)
        self.add_labeled_entry(product_frame, "请求超时秒", self.timeout_var, 6, 2)

        for i in range(4):
            product_frame.columnconfigure(i, weight=1)

        # 页面关键词
        keyword_frame = ttk.LabelFrame(
            main,
            text="页面关键词，可直接粘贴关键词，支持逗号、分号、换行分隔"
        )
        keyword_frame.pack(fill="both", pady=5)

        keyword_text_frame = ttk.Frame(keyword_frame)
        keyword_text_frame.pack(fill="both", expand=True, padx=5, pady=5)
        self.page_keywords_text = tk.Text(keyword_text_frame, height=5, wrap=tk.WORD)
        self.page_keywords_text.pack(side=tk.LEFT, fill="both", expand=True)
        keyword_scroll = ttk.Scrollbar(keyword_text_frame, orient=tk.VERTICAL, command=self.page_keywords_text.yview)
        keyword_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.page_keywords_text.configure(yscrollcommand=keyword_scroll.set)

        keyword_btn_frame = ttk.Frame(keyword_frame)
        keyword_btn_frame.pack(fill="x", padx=5, pady=3)

        ttk.Button(
            keyword_btn_frame,
            text="从 TXT 导入关键词",
            command=self.import_keywords_txt
        ).pack(side="left", padx=3)

        ttk.Button(
            keyword_btn_frame,
            text="清空关键词",
            command=self.clear_page_keywords
        ).pack(side="left", padx=3)

        # 操作按钮
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill="x", pady=8)

        self.start_btn = ttk.Button(
            btn_frame,
            text="开始处理",
            command=self.start_process
        )
        self.start_btn.pack(side="left", padx=5)

        ttk.Button(
            btn_frame,
            text="保存配置",
            command=self.save_config_from_ui
        ).pack(side="left", padx=5)

        ttk.Button(
            btn_frame,
            text="清空日志",
            command=self.clear_log
        ).pack(side="left", padx=5)

        # 进度
        progress_frame = ttk.LabelFrame(main, text="进度")
        progress_frame.pack(fill="x", pady=5)

        self.progress_var = tk.DoubleVar(value=0)

        self.progress_bar = ttk.Progressbar(
            progress_frame,
            variable=self.progress_var,
            maximum=100
        )
        self.progress_bar.pack(fill="x", padx=8, pady=8)

        self.progress_label_var = tk.StringVar(value="等待开始")

        ttk.Label(
            progress_frame,
            textvariable=self.progress_label_var
        ).pack(anchor="w", padx=8)

        # 日志
        log_frame = ttk.LabelFrame(main, text="运行日志")
        log_frame.pack(fill="both", expand=True, pady=5)

        log_text_frame = ttk.Frame(log_frame)
        log_text_frame.pack(fill="both", expand=True, padx=5, pady=5)
        self.log_text = tk.Text(log_text_frame, height=18, wrap=tk.WORD)
        self.log_text.pack(side=tk.LEFT, fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_text_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=log_scroll.set)

    def add_labeled_entry(self, parent, label, var, row, col, show=None):
        ttk.Label(parent, text=label).grid(
            row=row,
            column=col,
            sticky="w",
            padx=5,
            pady=4
        )

        entry = ttk.Entry(parent, textvariable=var, show=show)

        entry.grid(
            row=row,
            column=col + 1,
            sticky="ew",
            padx=5,
            pady=4
        )

        parent.columnconfigure(col + 1, weight=1)

        return entry

    def load_config_to_ui(self):
        self.api_host_var.set(self.cfg.get("api_host", ""))
        self.api_path_var.set(self.cfg.get("api_path", ""))
        self.api_token_var.set(self.cfg.get("api_token", ""))
        self.model_var.set(self.cfg.get("model", ""))

        self.category_hint_var.set(self.cfg.get("category_hint", ""))
        self.output_xlsx_var.set(self.cfg.get("output_xlsx", ""))
        self.source_subfolder_var.set(self.cfg.get("source_subfolder", ""))

        self.title_target_var.set(str(self.cfg.get("title_target_chars", 150)))
        self.title_max_var.set(str(self.cfg.get("title_max_chars", 200)))
        self.max_tokens_var.set(str(self.cfg.get("max_tokens", 900)))
        self.max_images_var.set(str(self.cfg.get("max_images_per_product", 5)))
        self.desc_min_var.set(str(self.cfg.get("desc_min_chars", 400)))
        self.desc_max_var.set(str(self.cfg.get("desc_max_chars", 470)))
        self.concurrency_var.set(str(self.cfg.get("concurrency_limit", 5)))
        self.timeout_var.set(str(self.cfg.get("request_timeout", 180)))

        self.page_keywords_text.delete("1.0", tk.END)
        self.page_keywords_text.insert("1.0", self.cfg.get("page_keywords", ""))

    def get_config_from_ui(self):
        cfg = {
            "api_host": self.api_host_var.get().strip(),
            "api_path": self.api_path_var.get().strip(),
            "api_token": self.api_token_var.get().strip(),
            "model": self.model_var.get().strip(),

            "category_hint": self.category_hint_var.get().strip(),
            "page_keywords": self.page_keywords_text.get("1.0", tk.END).strip(),
            "output_xlsx": self.output_xlsx_var.get().strip(),
            "source_subfolder": self.source_subfolder_var.get().strip(),

            "title_target_chars": int(self.title_target_var.get().strip()),
            "title_max_chars": int(self.title_max_var.get().strip()),
            "max_tokens": int(self.max_tokens_var.get().strip()),
            "max_images_per_product": int(self.max_images_var.get().strip()),

            "desc_min_chars": int(self.desc_min_var.get().strip()),
            "desc_max_chars": int(self.desc_max_var.get().strip()),

            "concurrency_limit": int(self.concurrency_var.get().strip()),
            "request_timeout": int(self.timeout_var.get().strip())
        }

        return cfg

    def save_config_from_ui(self):
        try:
            cfg = self.get_config_from_ui()
            save_config(cfg)
            self.cfg = cfg

            messagebox.showinfo("成功", f"配置已保存：\n{CONFIG_PATH}")

        except Exception as e:
            messagebox.showerror("错误", f"保存配置失败：\n{e}")

    def choose_folder(self):
        path = filedialog.askdirectory(title="选择图片根目录")

        if path:
            self.image_dir_var.set(path)

    def choose_output(self):
        path = filedialog.asksaveasfilename(
            title="选择 Excel 保存位置",
            defaultextension=".xlsx",
            filetypes=[("Excel 文件", "*.xlsx")]
        )

        if path:
            self.output_xlsx_var.set(path)

    def import_keywords_txt(self):
        path = filedialog.askopenfilename(
            title="选择关键词 TXT 文件",
            filetypes=[
                ("TXT 文件", "*.txt"),
                ("所有文件", "*.*")
            ]
        )

        if not path:
            return

        try:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except UnicodeDecodeError:
                with open(path, "r", encoding="gbk") as f:
                    content = f.read()

            current = self.page_keywords_text.get("1.0", tk.END).strip()

            if current:
                new_text = current + "\n" + content.strip()
            else:
                new_text = content.strip()

            self.page_keywords_text.delete("1.0", tk.END)
            self.page_keywords_text.insert("1.0", new_text)

            self.log(f"[关键词] 已导入 TXT：{path}")

        except Exception as e:
            messagebox.showerror("错误", f"导入关键词失败：\n{e}")

    def clear_page_keywords(self):
        self.page_keywords_text.delete("1.0", tk.END)

    def clear_log(self):
        self.log_text.delete("1.0", tk.END)

    def log(self, text):
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)

    def validate_before_start(self, image_dir, cfg):
        if not image_dir:
            raise ValueError("请选择图片根目录")

        if not os.path.isdir(image_dir):
            raise ValueError(f"图片根目录不存在：{image_dir}")

        if not cfg["api_token"]:
            raise ValueError("请填写 API Token")

        if not cfg["api_host"]:
            raise ValueError("请填写 API Host")

        if not cfg["api_path"].startswith("/"):
            raise ValueError("API Path 必须以 / 开头，例如 /v1/chat/completions")

        if not cfg["model"]:
            raise ValueError("请填写模型名称")

        if not cfg["category_hint"]:
            raise ValueError("请填写产品类型 / 类目提示")

        if not cfg["output_xlsx"]:
            raise ValueError("请选择输出 Excel 路径")

        out_dir = os.path.dirname(cfg["output_xlsx"])

        if out_dir and not os.path.isdir(out_dir):
            raise ValueError(f"输出目录不存在：{out_dir}")

        if int(cfg["concurrency_limit"]) <= 0:
            raise ValueError("并发数必须大于 0")

        if int(cfg["max_images_per_product"]) <= 0:
            raise ValueError("每产品最多图片数必须大于 0")

    def start_process(self):
        if self.running:
            messagebox.showwarning("提示", "任务正在运行中")
            return

        try:
            image_dir = self.image_dir_var.get().strip()
            cfg = self.get_config_from_ui()

            self.validate_before_start(image_dir, cfg)

            save_config(cfg)
            self.cfg = cfg

        except Exception as e:
            messagebox.showerror("错误", str(e))
            return

        self.running = True
        self.start_btn.config(state="disabled")

        self.total_tasks = 0
        self.done_tasks = 0

        self.progress_var.set(0)
        self.progress_label_var.set("准备开始...")

        self.log("\n==============================")
        self.log("[启动] 开始处理任务")
        self.log(f"[目录] {image_dir}")
        self.log(f"[模型] {cfg['model']}")
        self.log(f"[产品类型] {cfg['category_hint']}")

        page_keywords = parse_keywords_text(cfg.get("page_keywords", ""))

        if page_keywords:
            preview = ", ".join(page_keywords[:10])
            suffix = "..." if len(page_keywords) > 10 else ""
            self.log(f"[页面关键词] {len(page_keywords)} 个：{preview}{suffix}")
        else:
            self.log("[页面关键词] 未填写")

        self.log("==============================")

        t = threading.Thread(
            target=self.thread_worker,
            args=(image_dir, cfg),
            daemon=True
        )

        t.start()

    def thread_worker(self, image_dir, cfg):
        try:
            asyncio.run(async_worker(image_dir, cfg, self.ui_queue))

        except Exception as e:
            self.ui_queue.put(("error", str(e)))

    def poll_queue(self):
        try:
            while True:
                msg_type, payload = self.ui_queue.get_nowait()

                if msg_type == "log":
                    self.log(payload)

                elif msg_type == "total":
                    self.total_tasks = int(payload)
                    self.done_tasks = 0
                    self.progress_var.set(0)

                    if self.total_tasks > 0:
                        self.progress_label_var.set(f"0 / {self.total_tasks}")

                elif msg_type == "progress":
                    self.done_tasks += int(payload)

                    if self.total_tasks > 0:
                        percent = self.done_tasks / self.total_tasks * 100

                        self.progress_var.set(percent)

                        self.progress_label_var.set(
                            f"{self.done_tasks} / {self.total_tasks}，{percent:.1f}%"
                        )

                elif msg_type == "done":
                    self.running = False
                    self.start_btn.config(state="normal")

                    self.progress_var.set(100)
                    self.progress_label_var.set("处理完成")

                    messagebox.showinfo("完成", f"处理完成，文件已保存：\n{payload}")

                elif msg_type == "error":
                    self.running = False
                    self.start_btn.config(state="normal")

                    self.log(f"[错误] {payload}")

                    messagebox.showerror("错误", payload)

        except queue.Empty:
            pass

        self.root.after(100, self.poll_queue)


def main():
    args = parse_args()

    if args.image_dir:
        try:
            run_cli(args)
            return
        except Exception as e:
            print(f"错误: {e}")
            raise SystemExit(1)

    if args.no_gui:
        print("错误: 使用 --no-gui 时必须提供 --image-dir")
        raise SystemExit(1)

    root = tk.Tk()
    app = OzonGuiApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
