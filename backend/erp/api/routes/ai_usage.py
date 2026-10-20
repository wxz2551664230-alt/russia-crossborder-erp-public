from fastapi import APIRouter, Query
from erp.core.ai_usage import report

router = APIRouter(prefix="/ai-usage", tags=["ai-usage"])


@router.get("")
def usage(job_id: list[str] = Query(default=[], max_length=200)) -> dict:
    """Per-task request counts and estimated costs. Empty id selects unassigned calls."""
    return report(job_id or [""])
