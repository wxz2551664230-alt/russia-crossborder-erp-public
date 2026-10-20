from __future__ import annotations

import argparse
import json
from pathlib import Path

from erp.modules.ozon_follow.legacy.services.ozon_gallery import scrape_ozon_gallery_browser


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--ready", required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    result_path = Path(args.result)
    ready_path = Path(args.ready)
    try:
        def progress(message: str) -> None:
            ready_path.parent.mkdir(parents=True, exist_ok=True)
            temp_ready_path = ready_path.with_name(ready_path.name + ".tmp")
            temp_ready_path.write_text(message, encoding="utf-8")
            temp_ready_path.replace(ready_path)

        gallery = scrape_ozon_gallery_browser(
            args.url, timeout=max(120, args.timeout), on_progress=progress
        )
        payload = {"title": gallery.title, "images": gallery.images, "source": gallery.source}
    except Exception as exc:
        payload = {"error": str(exc)}
    result_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = result_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(result_path)


if __name__ == "__main__":
    main()
