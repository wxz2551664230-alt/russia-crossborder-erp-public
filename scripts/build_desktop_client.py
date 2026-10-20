"""Build a source-only pilot package from a strict file allowlist."""
import hashlib
import json
import re
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = ROOT / "client/desktop"
    files = ["erp_desktop.py", "desktop_ui.py", "local_worker.py", "local_picker.py", "composition_worker.py", "artwork_worker.py", "ai_worker.py", "wb_worker.py", "order_worker.py", "purchase_submit_worker.py", "purchase_journal.py", "wb_reconcile_worker.py", "wb_history_worker.py", "wb_shop_sync_worker.py", "wb_stock_worker.py", "wb_price_worker.py", "ad_worker.py", "ad_sync_worker.py", "ad_report_worker.py", "ad_preview_worker.py", "ad_execution_worker.py", "ad_reconcile_worker.py", "supplier_gallery_worker.py", "supplier_catalog_worker.py", "us_catalog_worker.py", "catalog_import_worker.py", "pod_import_worker.py", "self_listing_pipeline_worker.py", "store_test_worker.py", "catalog_ozon_batch_worker.py", "catalog_ozon_worker.py", "catalog_ozon_submit_worker.py", "catalog_ozon_followup_worker.py", "catalog_ozon_options_worker.py", "ozon_image_rules.py", "codex_bridge.py", "wb_codex_bridge.py", "amazon_codex_bridge.py", "CODEX-HANDOFF.md", "ozon_paid_transport.py", "worker-requirements.txt", "README.md", "install.ps1", "install.cmd", "repair-shortcut.ps1", "repair-shortcut.cmd"]
    contents = {name:(source/name).read_bytes() for name in files}
    contents['amazon_worker.py']=(source/'amazon_worker.py').read_bytes()
    match = re.search(rb'^VERSION = "(\d+\.\d+\.\d+)"', contents['erp_desktop.py'], re.MULTILINE)
    if not match: raise RuntimeError('Client version is missing')
    version = match.group(1).decode('ascii')
    for path in (ROOT/'backend/erp').rglob('*.py'):
        contents['runtime/'+path.relative_to(ROOT/'backend').as_posix()]=path.read_bytes()
    for name in ['realesrgan-ncnn-vulkan.exe','vcomp140.dll','models/realesrgan-x4plus.bin','models/realesrgan-x4plus.param','ERP-MIGRATION.md']:
        path=ROOT/'tools/realesrgan-ncnn-vulkan'/name
        contents['tools/realesrgan-ncnn-vulkan/'+name]=path.read_bytes()
    manifest = {"version": version, "files": {name: hashlib.sha256(data).hexdigest() for name,data in contents.items()}}
    destination = ROOT / "outputs" / f"erp-desktop-{version}.zip"
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name,data in contents.items():
            archive.writestr(name,data)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"Built {destination.name}; {destination.stat().st_size} bytes; SHA256={hashlib.sha256(destination.read_bytes()).hexdigest()}")


if __name__ == "__main__": main()


