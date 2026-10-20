from __future__ import annotations

import argparse
import json
from pathlib import Path

from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.models import CatalogImportRun
from erp.services.config_resolution import build_job_snapshot
from erp.services.job_handlers import _catalog  # noqa: F401
from erp.services.jobs import enqueue_job, execute_job, get_job


def main() -> int:
    parser = argparse.ArgumentParser(description="将 POD 历史产品原地索引到统一产品库")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只校验数据库结构和数量")
    mode.add_argument("--execute", action="store_true", help="执行可重复的原地索引")
    parser.add_argument("--database", type=Path, default=None, help="POD product_library.db 路径")
    args = parser.parse_args()
    source = (args.database or (get_settings().legacy_composer_root / "data" / "product_library.db")).resolve()
    with SessionLocal() as session:
        run = CatalogImportRun(kind="pod_history", source_path=str(source), dry_run=bool(args.dry_run))
        session.add(run); session.flush()
        job = enqueue_job(
            session,
            job_type="catalog_pod_import",
            payload={"import_run_id": run.id},
            config_snapshot=build_job_snapshot(session),
            idempotency_key=f"catalog_pod_import:{run.id}",
            steps=[("products", "校验并索引 POD 产品、SKU 和资产")],
            max_attempts=1,
        )
        run.job_id = job.id; session.commit()
        execute_job(session, get_job(session, job.id))
        final = get_job(session, job.id)
        session.refresh(run)
        print(json.dumps({
            "job_id": final.id,
            "status": final.status,
            "progress": final.progress,
            "error": final.error_message,
            "counts": json.loads(run.counts_json or "{}"),
            "report": json.loads(run.report_json or "{}"),
        }, ensure_ascii=False, indent=2))
        return 0 if final.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
