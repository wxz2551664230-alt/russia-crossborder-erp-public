import argparse
import csv
import json
from json import JSONDecodeError
import mimetypes
import os
import re
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import quote


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
APP_DIR = Path(__file__).resolve().parent
WORK_DIR = APP_DIR.parent if APP_DIR.name == "程序文件" else APP_DIR
CONFIG_DIR = WORK_DIR / "配置文件"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "oss_config.json"


def natural_sort_key(value):
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(value))]


def get_variant_name(file_name):
    stem = Path(str(file_name)).stem.strip()
    if stem.casefold().startswith("cover_"):
        stem = stem[len("cover_"):]
    return stem or "default"


def get_folder_number(product_folder):
    text = str(product_folder or "").strip()
    match = re.search(r"\d+", text)
    if not match:
        return text
    return match.group(0).zfill(3)


def get_color_from_sku_name(file_name):
    stem = get_variant_name(file_name)
    if not stem or stem == "default":
        return ""

    parts = [part.strip() for part in re.split(r"[-_]", stem) if part.strip()]
    if len(parts) >= 3 and parts[0].casefold() == "sku":
        return parts[-1]
    if len(parts) >= 2 and parts[0].casefold().startswith("sku"):
        return parts[-1]
    if stem.casefold().startswith("sku") and len(stem) > 3:
        return stem[3:].strip("-_ ")
    return parts[-1] if len(parts) > 1 else stem


def build_variant_key(product_folder, file_name):
    return f"{str(product_folder).strip()}::{get_variant_name(file_name)}"


def is_variant_cover_file(file_name):
    stem = Path(str(file_name)).stem.casefold()
    return stem.startswith("cover_sku") or stem.startswith("sku")


@dataclass
class OssUploadConfig:
    region: str = ""
    endpoint: str = ""
    bucket: str = ""
    public_base_url: str = ""
    access_key_id: str = ""
    access_key_secret: str = ""
    object_prefix: str = "ozon"
    source_subfolder: str = "_generated"
    only_suffix: str = "_watermarked"
    max_workers: int = 4
    upload_retries: int = 3
    output_csv_threshold: int = 1000


def load_config(path=None):
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(
            f"OSS config not found: {config_path}. "
            f"Copy oss_config.example.json to oss_config.json and fill it first."
        )

    raw = config_path.read_text(encoding="utf-8-sig").strip()
    if not raw:
        raise ValueError(f"OSS config is empty: {config_path}")

    try:
        data = json.loads(raw)
    except JSONDecodeError as exc:
        raise ValueError(f"OSS config is not valid JSON: {config_path}. {exc}") from exc

    cfg = OssUploadConfig()
    for key, value in data.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    validate_config(cfg)
    return cfg


def save_example_config(path):
    config_path = Path(path)
    cfg = OssUploadConfig(
        region="cn-hangzhou",
        endpoint="oss-cn-hangzhou.aliyuncs.com",
        bucket="your-bucket-name",
        public_base_url="https://your-bucket-name.oss-cn-hangzhou.aliyuncs.com",
        access_key_id="",
        access_key_secret="",
        object_prefix="ozon",
        source_subfolder="_generated",
        only_suffix="",
        output_csv_threshold=1000,
    )
    config_path.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path


def validate_config(cfg):
    if not cfg.region:
        raise ValueError("OSS region is required, for example cn-hangzhou.")
    if not cfg.bucket:
        raise ValueError("OSS bucket is required.")
    if not cfg.endpoint and not cfg.public_base_url:
        raise ValueError("Either endpoint or public_base_url is required.")


def normalize_endpoint(endpoint):
    endpoint = (endpoint or "").strip().rstrip("/")
    endpoint = endpoint.replace("https://", "").replace("http://", "")
    return endpoint


def build_public_base_url(cfg):
    if cfg.public_base_url:
        return cfg.public_base_url.rstrip("/")

    endpoint = normalize_endpoint(cfg.endpoint)
    return f"https://{cfg.bucket}.{endpoint}"


def quote_url_path_preserve_unicode(value):
    """Encode unsafe ASCII URL characters while keeping Unicode text readable."""
    safe_ascii = "/-._~"
    parts = []
    for char in str(value):
        if ord(char) > 127 or char.isalnum() or char in safe_ascii:
            parts.append(char)
        else:
            parts.append(quote(char, safe=""))
    return "".join(parts)


def build_public_url(cfg, object_key):
    base_url = build_public_base_url(cfg)
    return f"{base_url}/{quote_url_path_preserve_unicode(object_key)}"


def build_object_key(root_dir, file_path, object_prefix, source_subfolder=None):
    root = Path(root_dir).resolve()
    path = Path(file_path).resolve()
    rel_parts = list(path.relative_to(root).parts)

    # Local generated images live under each product's source subfolder, but
    # OSS paths should expose product/image directly.
    source_subfolder = str(source_subfolder or "").strip().strip("/\\")
    if source_subfolder and source_subfolder in rel_parts:
        rel_parts.pop(rel_parts.index(source_subfolder))

    rel = Path(*rel_parts).as_posix()

    prefix = (object_prefix or "").strip().strip("/")
    if prefix:
        return f"{prefix}/{rel}"
    return rel


def is_target_image(path, only_suffix=None):
    path = Path(path)
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return False
    if only_suffix:
        return path.stem.endswith(only_suffix)
    return True


def iter_upload_files(root_dir, source_subfolder=None, only_suffix=None):
    root = Path(root_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"Root dir does not exist: {root}")

    def yield_images(image_dir):
        for path in sorted(image_dir.rglob("*"), key=lambda p: natural_sort_key(p.as_posix())):
            if path.is_file() and is_target_image(path, only_suffix):
                yield path

    if source_subfolder:
        source_subfolder = str(source_subfolder).strip().strip("/\\")
        image_dirs = []

        # Support selecting the generated folder directly.
        if root.name == source_subfolder:
            image_dirs.append(root)

        # Support selecting a single product folder that contains _generated.
        direct_image_dir = root / source_subfolder
        if direct_image_dir.is_dir():
            image_dirs.append(direct_image_dir)

        # Support selecting the batch root that contains product folders.
        for product_dir in sorted(
            (p for p in root.iterdir() if p.is_dir()),
            key=lambda p: natural_sort_key(p.name),
        ):
            image_dir = product_dir / source_subfolder
            if image_dir.is_dir():
                image_dirs.append(image_dir)

        seen_dirs = set()
        for image_dir in image_dirs:
            key = str(image_dir.resolve())
            if key in seen_dirs:
                continue
            seen_dirs.add(key)
            yield from yield_images(image_dir)
    else:
        yield from yield_images(root)


def create_client(cfg):
    try:
        import alibabacloud_oss_v2 as oss
    except ImportError as exc:
        raise RuntimeError(
            "Missing OSS SDK. Install it with: pip install alibabacloud-oss-v2"
        ) from exc

    ensure_oss_credentials_in_process_env(cfg)

    credentials_provider = oss.credentials.EnvironmentVariableCredentialsProvider()
    client_cfg = oss.config.load_default()
    client_cfg.credentials_provider = credentials_provider
    client_cfg.region = cfg.region

    if cfg.endpoint:
        client_cfg.endpoint = cfg.endpoint

    return oss, oss.Client(client_cfg)


def read_windows_user_environment(name):
    if os.name != "nt":
        return ""

    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value or "").strip()
    except Exception:
        return ""


def ensure_oss_credentials_in_process_env(cfg=None):
    config_access_key_id = str(getattr(cfg, "access_key_id", "") or "").strip()
    config_access_key_secret = str(getattr(cfg, "access_key_secret", "") or "").strip()

    access_key_id = config_access_key_id or os.environ.get("OSS_ACCESS_KEY_ID", "").strip()
    access_key_secret = config_access_key_secret or os.environ.get("OSS_ACCESS_KEY_SECRET", "").strip()

    if config_access_key_id:
        os.environ["OSS_ACCESS_KEY_ID"] = config_access_key_id
    if config_access_key_secret:
        os.environ["OSS_ACCESS_KEY_SECRET"] = config_access_key_secret

    if not access_key_id:
        access_key_id = read_windows_user_environment("OSS_ACCESS_KEY_ID")
        if access_key_id:
            os.environ["OSS_ACCESS_KEY_ID"] = access_key_id

    if not access_key_secret:
        access_key_secret = read_windows_user_environment("OSS_ACCESS_KEY_SECRET")
        if access_key_secret:
            os.environ["OSS_ACCESS_KEY_SECRET"] = access_key_secret

    if not access_key_id or not access_key_secret:
        raise RuntimeError(
            "OSS credentials are missing. Fill access_key_id and access_key_secret "
            "in oss_config.json, or set OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET."
        )


def upload_one_file(oss_module, client, cfg, root_dir, file_path, dry_run=False):
    file_path = Path(file_path)
    object_key = build_object_key(root_dir, file_path, cfg.object_prefix, cfg.source_subfolder)
    content_type, _ = mimetypes.guess_type(str(file_path))
    public_url = build_public_url(cfg, object_key)

    product_folder = get_product_folder(root_dir, file_path, cfg.source_subfolder)
    variant_name = get_variant_name(file_path.name) if is_variant_cover_file(file_path.name) else ""
    row = {
        "product_folder": product_folder,
        "编号": get_folder_number(product_folder),
        "颜色": get_color_from_sku_name(file_path.name) if variant_name else "",
        "variant_key": build_variant_key(product_folder, file_path.name) if variant_name else "",
        "variant_name": variant_name,
        "file_name": file_path.name,
        "local_path": str(file_path),
        "object_key": object_key,
        "url": public_url,
        "content_type": content_type or "",
        "status_code": "",
        "request_id": "",
        "etag": "",
        "uploaded": False,
        "error": "",
    }

    if dry_run:
        row["uploaded"] = "dry_run"
        return row

    with file_path.open("rb") as body:
        result = client.put_object(oss_module.PutObjectRequest(
            bucket=cfg.bucket,
            key=object_key,
            body=body,
        ))

    row["status_code"] = getattr(result, "status_code", "")
    row["request_id"] = getattr(result, "request_id", "")
    row["etag"] = getattr(result, "etag", "")
    row["uploaded"] = True
    return row


def failed_upload_row(cfg, root_dir, file_path, error):
    file_path = Path(file_path)
    object_key = build_object_key(root_dir, file_path, cfg.object_prefix, cfg.source_subfolder)
    content_type, _ = mimetypes.guess_type(str(file_path))
    product_folder = get_product_folder(root_dir, file_path, cfg.source_subfolder)
    variant_name = get_variant_name(file_path.name) if is_variant_cover_file(file_path.name) else ""
    return {
        "product_folder": product_folder,
        "编号": get_folder_number(product_folder),
        "颜色": get_color_from_sku_name(file_path.name) if variant_name else "",
        "variant_key": build_variant_key(product_folder, file_path.name) if variant_name else "",
        "variant_name": variant_name,
        "file_name": file_path.name,
        "local_path": str(file_path),
        "object_key": object_key,
        "url": build_public_url(cfg, object_key),
        "content_type": content_type or "",
        "status_code": "",
        "request_id": "",
        "etag": "",
        "uploaded": False,
        "error": str(error),
    }


def upload_one_file_with_retry(oss_module, client, cfg, root_dir, file_path, dry_run=False):
    last_err = None
    for attempt in range(max(1, int(cfg.upload_retries or 1))):
        try:
            return upload_one_file(oss_module, client, cfg, root_dir, file_path, dry_run=dry_run)
        except Exception as exc:
            last_err = exc
            if attempt < int(cfg.upload_retries or 1) - 1:
                time.sleep(min(2 ** attempt, 10))
    return failed_upload_row(cfg, root_dir, file_path, last_err)


def get_product_folder(root_dir, file_path, source_subfolder=None):
    root = Path(root_dir).resolve()
    path = Path(file_path).resolve()
    rel_parts = path.relative_to(root).parts

    source_subfolder = str(source_subfolder or "").strip().strip("/\\")
    if source_subfolder:
        if root.name == source_subfolder:
            return root.parent.name
        if rel_parts and rel_parts[0] == source_subfolder:
            return root.name
        if len(rel_parts) >= 2 and rel_parts[1] == source_subfolder:
            return rel_parts[0]

    if len(rel_parts) >= 2:
        return rel_parts[0]
    return root.name


def build_product_rows(image_rows):
    grouped = defaultdict(list)
    for row in image_rows:
        grouped[row["product_folder"]].append(row)

    product_rows = []
    source_order = 0
    for product_folder, rows in sorted(grouped.items(), key=lambda item: natural_sort_key(item[0])):
        rows = sorted(rows, key=lambda x: natural_sort_key(x["file_name"]))
        cover_rows = [
            row for row in rows
            if is_variant_cover_file(row["file_name"])
        ]

        if cover_rows:
            attachment_rows = [row for row in rows if row not in cover_rows]
        else:
            cover_rows = rows[:1]
            attachment_rows = rows[1:]

        attachment_urls = "\n".join(row["url"] for row in attachment_rows)
        attachment_files = "\n".join(row["file_name"] for row in attachment_rows)

        for index, cover_row in enumerate(cover_rows, start=1):
            source_order += 1
            if is_variant_cover_file(cover_row["file_name"]):
                variant_name = get_variant_name(cover_row["file_name"])
            else:
                variant_name = "default"
            product_rows.append({
                "source_order": source_order,
                "编号": get_folder_number(product_folder),
                "颜色": get_color_from_sku_name(cover_row["file_name"]),
                "产品文件夹": product_folder,
                "SKU": variant_name,
                "主图链接": cover_row["url"],
                "附图链接": attachment_urls,
                "主图文件": cover_row["file_name"],
                "附图文件": attachment_files,
                "variant_key": f"{str(product_folder).strip()}::{variant_name}",
                "variant_name": variant_name,
                "product_folder": product_folder,
                "variant_index": index,
                "variant_count": len(cover_rows),
                "main_image_file": cover_row["file_name"],
                "main_image_url": cover_row["url"],
                "additional_image_files": attachment_files,
                "additional_image_urls": attachment_urls,
                "additional_image_count": len(attachment_rows),
            })
    return product_rows


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_upload_details_output_path(path):
    path = Path(path)
    suffix = path.suffix or ".csv"
    return path.with_name(f"{path.stem}_upload_details{suffix}")


def write_xlsx(path, image_rows, product_rows):
    try:
        import pandas as pd
        from openpyxl.styles import Alignment
    except ImportError as exc:
        raise RuntimeError(
            "Writing .xlsx requires pandas and openpyxl. Use --output-csv or install them."
        ) from exc

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(product_rows).to_excel(writer, index=False, sheet_name="images")
        pd.DataFrame(product_rows).to_excel(writer, index=False, sheet_name="products")
        pd.DataFrame(image_rows).to_excel(writer, index=False, sheet_name="upload_details")

        product_sheet = writer.book["images"]
        wrap_columns = {"附图文件", "附图链接", "additional_image_files", "additional_image_urls"}
        for cell in product_sheet[1]:
            if cell.value in wrap_columns:
                col_letter = cell.column_letter
                product_sheet.column_dimensions[col_letter].width = 60
                for column_cell in product_sheet[col_letter]:
                    column_cell.alignment = Alignment(wrap_text=True, vertical="top")

        for row in product_sheet.iter_rows():
            for cell in row:
                if cell.alignment != Alignment(wrap_text=True, vertical="top"):
                    cell.alignment = Alignment(vertical="top")

        for column_cells in product_sheet.columns:
            header = column_cells[0].value
            if header in {"主图链接", "主图文件", "main_image_url", "main_image_file"}:
                product_sheet.column_dimensions[column_cells[0].column_letter].width = 45
            elif header in {"产品文件夹", "SKU", "product_folder", "variant_index", "variant_count", "additional_image_count"}:
                product_sheet.column_dimensions[column_cells[0].column_letter].width = 18
            elif header in {"附图链接", "附图文件"}:
                product_sheet.column_dimensions[column_cells[0].column_letter].width = 60

        details_sheet = writer.book["upload_details"]
        for column_cells in details_sheet.columns:
            header = column_cells[0].value
            if header in {"local_path", "object_key", "url"}:
                details_sheet.column_dimensions[column_cells[0].column_letter].width = 45


def write_outputs(output_path, image_rows):
    output = Path(output_path)
    for row in image_rows:
        product_folder = str(row.get("product_folder", ""))
        file_name = str(row.get("file_name", ""))
        is_cover = is_variant_cover_file(file_name)
        row["编号"] = row.get("编号") or get_folder_number(product_folder)
        row["颜色"] = row.get("颜色") or (get_color_from_sku_name(file_name) if is_cover else "")
        row["variant_name"] = get_variant_name(file_name) if is_cover else ""
        row["variant_key"] = build_variant_key(product_folder, file_name) if is_cover else ""
    image_rows.sort(
        key=lambda row: (
            natural_sort_key(row.get("product_folder", "")),
            natural_sort_key(row.get("file_name", "")),
        )
    )
    product_rows = build_product_rows(image_rows)

    if output.suffix.lower() == ".xlsx":
        write_xlsx(output, image_rows, product_rows)
        return {"images": output, "products": output}

    details_output = build_upload_details_output_path(output)
    write_csv(output, product_rows)
    write_csv(details_output, image_rows)
    return {"images": output, "products": output, "upload_details": details_output}


def is_uploaded_value(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def normalize_history_row(row):
    normalized = {}
    for key, value in dict(row).items():
        if value is None:
            normalized[key] = ""
        elif isinstance(value, float) and value != value:
            normalized[key] = ""
        else:
            normalized[key] = value
    return normalized


def read_history_rows(path):
    path = Path(path)
    if not path.exists():
        return []

    if path.suffix.lower() == ".csv":
        with path.open("r", newline="", encoding="utf-8-sig") as f:
            rows = [normalize_history_row(row) for row in csv.DictReader(f)]
        if rows and not any(str(row.get("object_key", "")).strip() for row in rows):
            details_path = build_upload_details_output_path(path)
            if details_path.exists():
                with details_path.open("r", newline="", encoding="utf-8-sig") as f:
                    return [normalize_history_row(row) for row in csv.DictReader(f)]
        return rows

    if path.suffix.lower() == ".xlsx":
        try:
            import pandas as pd
        except ImportError:
            return []
        frame = None
        for sheet_name in ("upload_details", "images"):
            try:
                candidate = pd.read_excel(path, sheet_name=sheet_name)
            except ValueError:
                continue
            if "object_key" in candidate.columns:
                frame = candidate
                break
        if frame is None:
            frame = pd.read_excel(path)
        return [normalize_history_row(row) for row in frame.to_dict("records")]

    return []


def load_uploaded_history(paths):
    rows = []
    uploaded_by_key = {}
    for path in paths:
        for row in read_history_rows(path):
            rows.append(row)
            object_key = str(row.get("object_key", "")).strip()
            if object_key and is_uploaded_value(row.get("uploaded")):
                uploaded_by_key[object_key] = row
    return rows, uploaded_by_key


def effective_output_path(output_path, file_count, cfg):
    output = Path(output_path)
    threshold = max(0, int(getattr(cfg, "output_csv_threshold", 0) or 0))
    if output.suffix.lower() == ".xlsx" and threshold and file_count >= threshold:
        return output.with_suffix(".csv")
    return output


def wait_if_paused(pause_event=None):
    while pause_event is not None and pause_event.is_set():
        time.sleep(0.2)


def batch_upload_images(root_dir, cfg=None, output_path=None, dry_run=False, log_func=print, pause_event=None):
    cfg = cfg or load_config()
    requested_output_path = Path(output_path or Path(root_dir) / "oss_image_urls.xlsx")

    files = list(iter_upload_files(
        root_dir,
        source_subfolder=cfg.source_subfolder,
        only_suffix=cfg.only_suffix,
    ))

    if not files:
        raise RuntimeError(
            f"No upload images found under {root_dir}. "
            f"source_subfolder={cfg.source_subfolder!r}, only_suffix={cfg.only_suffix!r}"
        )

    output_path = effective_output_path(requested_output_path, len(files), cfg)
    if output_path != requested_output_path:
        log_func(f"Large batch detected ({len(files)} files), using CSV output: {output_path}")

    history_paths = {requested_output_path, output_path}
    _, uploaded_by_key = load_uploaded_history(history_paths)

    skipped_rows = []
    upload_files = []
    for file_path in files:
        object_key = build_object_key(root_dir, file_path, cfg.object_prefix, cfg.source_subfolder)
        if object_key in uploaded_by_key:
            history_row = dict(uploaded_by_key[object_key])
            history_row["url"] = build_public_url(cfg, object_key)
            skipped_rows.append(history_row)
        else:
            upload_files.append(file_path)

    log_func(f"Found images: {len(files)}")
    if skipped_rows:
        log_func(f"Skip already uploaded: {len(skipped_rows)}")
    if not upload_files:
        outputs = write_outputs(output_path, skipped_rows)
        log_func(f"Image table: {outputs['images']}")
        log_func(f"Product table: {outputs['products']}")
        return skipped_rows, outputs

    oss_module = None
    client = None
    if not dry_run:
        oss_module, client = create_client(cfg)

    rows = list(skipped_rows)
    max_workers = max(1, int(cfg.max_workers or 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        pending = {}
        file_iter = iter(upload_files)
        submitted = 0
        completed = 0

        def submit_next():
            nonlocal submitted
            wait_if_paused(pause_event)
            try:
                file_path = next(file_iter)
            except StopIteration:
                return False
            future = executor.submit(
                upload_one_file_with_retry,
                oss_module,
                client,
                cfg,
                root_dir,
                file_path,
                dry_run,
            )
            pending[future] = file_path
            submitted += 1
            return True

        for _ in range(min(max_workers, len(upload_files))):
            submit_next()

        while pending:
            done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            if not done:
                continue

            for future in done:
                file_path = pending.pop(future)
                completed += 1
                index = len(skipped_rows) + completed
                total = len(files)
                try:
                    row = future.result()
                except Exception as exc:
                    row = failed_upload_row(cfg, root_dir, file_path, exc)
                rows.append(row)
                status = "OK" if row.get("uploaded") else "FAILED"
                log_func(f"[{index}/{total}] {status}: {file_path}")

            while len(pending) < max_workers and submitted < len(upload_files):
                if not submit_next():
                    break

    outputs = write_outputs(output_path, rows)
    log_func(f"Image table: {outputs['images']}")
    log_func(f"Product table: {outputs['products']}")
    return rows, outputs


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Batch upload OZON images to Aliyun OSS.")
    parser.add_argument("--init-config", help="Create an OSS config template at this path and exit.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="OSS config JSON path.")
    parser.add_argument("--root-dir", help="Product root directory.")
    parser.add_argument("--output", help="Output .xlsx or .csv path. Default: root-dir/oss_image_urls.xlsx")
    parser.add_argument("--region", help="Override OSS region.")
    parser.add_argument("--endpoint", help="Override OSS endpoint.")
    parser.add_argument("--bucket", help="Override OSS bucket.")
    parser.add_argument("--public-base-url", help="Override public image base URL.")
    parser.add_argument("--access-key-id", help="Override OSS AccessKey ID.")
    parser.add_argument("--access-key-secret", help="Override OSS AccessKey Secret.")
    parser.add_argument("--object-prefix", help="Override OSS object key prefix.")
    parser.add_argument("--source-subfolder", help="Override source subfolder, for example _generated.")
    parser.add_argument("--only-suffix", help="Only upload files whose stem ends with this suffix. Use empty string to disable.")
    parser.add_argument("--max-workers", type=int, help="Concurrent upload workers, default 4.")
    parser.add_argument("--upload-retries", type=int, help="Retries per file, default 3.")
    parser.add_argument("--output-csv-threshold", type=int, help="Use CSV output when file count reaches this threshold.")
    parser.add_argument("--check-credentials", action="store_true", help="Check whether OSS AccessKey environment variables are visible.")
    parser.add_argument("--dry-run", action="store_true", help="Do not upload, only generate URL tables.")
    return parser.parse_args(argv)


def config_from_args(args):
    cfg = load_config(args.config)
    overrides = {
        "region": args.region,
        "endpoint": args.endpoint,
        "bucket": args.bucket,
        "public_base_url": args.public_base_url,
        "access_key_id": args.access_key_id,
        "access_key_secret": args.access_key_secret,
        "object_prefix": args.object_prefix,
        "source_subfolder": args.source_subfolder,
        "max_workers": args.max_workers,
        "upload_retries": args.upload_retries,
        "output_csv_threshold": args.output_csv_threshold,
    }
    for key, value in overrides.items():
        if value is not None:
            setattr(cfg, key, value)

    if args.only_suffix is not None:
        cfg.only_suffix = args.only_suffix or ""

    validate_config(cfg)
    return cfg


def main():
    args = parse_args()

    if args.init_config:
        path = save_example_config(args.init_config)
        print(f"Created config template: {path}")
        return

    if args.check_credentials:
        cfg = config_from_args(args)
        ensure_oss_credentials_in_process_env(cfg)
        print("OSS_ACCESS_KEY_ID: OK")
        print("OSS_ACCESS_KEY_SECRET: OK")
        return

    if not args.root_dir:
        raise SystemExit("Error: --root-dir is required.")

    cfg = config_from_args(args)
    output_path = args.output or Path(args.root_dir) / "oss_image_urls.xlsx"
    batch_upload_images(args.root_dir, cfg=cfg, output_path=output_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
