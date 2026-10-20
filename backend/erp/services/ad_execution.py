"""Reviewed live execution; external writes are never automatically replayed."""
from __future__ import annotations

import json
from datetime import timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from erp.core.errors import AppError, ConflictError, NotFoundError
from erp.core.config import get_settings
from erp.core.jsonutil import dumps, loads
from erp.core.security import vault
from erp.integrations.ozon_performance import OzonPerformanceClient
from erp.models import AdAction, AdCampaign, AdCampaignSku, AdExecutionBatch, AdSku, Store, utc_now
from erp.services.ad_optimization import analyze_store


def _client(store: Store) -> OzonPerformanceClient:
    secrets = vault.decrypt(store.secrets_encrypted)
    return OzonPerformanceClient(str(secrets.get("performance_client_id") or ""), str(secrets.get("performance_client_secret") or ""))


def _plan(session: Session, store_id: str, action: str, ids: list[str]) -> dict:
    from erp.api.routes.ad_optimization import _selected_execution_rows, _execution_candidates
    rows = _selected_execution_rows(session, store_id, ids)
    if len(rows) != len(set(ids)):
        raise ConflictError("部分 SKU 不存在或不属于所选店铺")
    candidates = _execution_candidates(session, store_id, action, ids)
    items = []
    for state, sku, metrics, proposal in candidates:
        cap = loads(state.financial_check_json, {}).get("effective_break_even_drr")
        if cap is None:
            cap = loads(state.financial_check_json, {}).get("break_even_drr")
        target = proposal.get("proposed_target_drr")
        if action != "STOP_ADS_CANDIDATE" and (target is None or cap is None or not 0 < float(target) <= float(cap)):
            continue
        if not str(proposal.get("campaign_id") or ""):
            continue
        items.append({"state_id": state.id, "ad_sku_id": sku.id, "sku_id": sku.sku_id,
                      "name": sku.name, "campaign_id": str(proposal["campaign_id"]),
                      "previous_target_drr": proposal.get("current_target_drr"), "proposed_target_drr": target,
                      "break_even_drr": cap, "reason": state.reason,
                      "evidence": loads(state.evidence_json, []), "metrics": metrics})
    allowed = {item["state_id"] for item in items}
    return {"state_ids": sorted(set(ids)), "items": sorted(items, key=lambda row: row["state_id"]),
            "blocked": [{"state_id": state.id, "sku_id": sku.sku_id, "risk_flags": metrics.get("risk_flags", [])}
                        for state, sku, metrics in rows if state.id not in allowed]}


def _signature(plan: dict) -> str:
    # Includes facts, evidence and proposed writes, but ignores analysis run IDs.
    rows = [{key: row[key] for key in ("state_id", "campaign_id", "previous_target_drr", "proposed_target_drr", "break_even_drr", "reason", "evidence")}
            for row in plan["items"]]
    return json.dumps(rows, sort_keys=True, ensure_ascii=False)


def preview(session: Session, store_id: str, request, actor: str) -> dict:
    if get_settings().runtime_mode == 'server':
        raise ConflictError('广告正式执行预览须由发起人的电脑计算，云端桥接尚未接通')
    store = session.get(Store, store_id)
    if not store or not store.enabled or store.platform != "ozon":
        raise ConflictError("店铺不存在、已停用或不是 Ozon 店铺")
    if session.scalar(select(AdExecutionBatch.id).where(AdExecutionBatch.active_store == store_id)):
        raise ConflictError("该店铺有正在执行或结果待核对的广告批次，请先核对")
    analyze_store(session, store_id)
    plan = _plan(session, store_id, request.action, request.state_ids)
    if not plan["items"]:
        raise ConflictError("所选候选均未通过执行安全门", details=plan["blocked"])
    batch = AdExecutionBatch(store_id=store_id, action=request.action, actor=actor,
                             preview_json=dumps(plan), expires_at=utc_now() + timedelta(minutes=10))
    session.add(batch); session.commit()
    return {"approval_id": batch.id, "store_name": store.name, "action": batch.action,
            "expires_at": batch.expires_at, "items": plan["items"], "blocked": plan["blocked"], "mode": "live_reviewed"}


def _batch(session: Session, identity: str, store_id: str) -> AdExecutionBatch:
    batch = session.get(AdExecutionBatch, identity)
    if not batch or batch.store_id != store_id:
        raise NotFoundError("广告审批批次不存在")
    return batch


def summary(batch: AdExecutionBatch) -> dict:
    results = loads(batch.results_json, [])
    counts = {status: sum(row["status"] == status for row in results) for status in ("executed", "blocked", "failed", "unknown", "executing")}
    return {"mode": "live_reviewed", "approval_id": batch.id, "status": batch.status,
            "action": batch.action, "requested": len(results), "succeeded": counts["executed"],
            "failed": counts["failed"], "blocked": counts["blocked"],
            "unknown": counts["unknown"] + counts["executing"], "results": results}


def _apply_confirmed(session: Session, batch: AdExecutionBatch, item: dict):
    config = session.scalar(select(AdCampaignSku).join(AdCampaign, AdCampaign.id == AdCampaignSku.ad_campaign_id)
                            .where(AdCampaign.store_id == batch.store_id, AdCampaign.campaign_id == item["campaign_id"], AdCampaignSku.sku_id == item["sku_id"]))
    if config:
        config.last_change_time = utc_now()
        if batch.action == "STOP_ADS_CANDIDATE":
            config.status = "REMOVED_BY_ERP"
        else:
            config.previous_target_drr = config.target_drr
            config.target_drr = item["proposed_target_drr"]
            sku = session.get(AdSku, item["ad_sku_id"])
            if sku:
                sku.target_drr = item["proposed_target_drr"]


def execute(session: Session, store_id: str, request, actor: str) -> dict:
    if get_settings().runtime_mode == 'server':
        raise ConflictError('广告正式执行须由发起人的电脑完成，云端桥接尚未接通')
    if not request.confirmed or not request.approval_id:
        raise ConflictError("请先预览本次变更并确认具体批次")
    batch = _batch(session, request.approval_id, store_id)
    plan = loads(batch.preview_json, {})
    if batch.actor != actor or batch.action != request.action or plan.get("state_ids") != sorted(set(request.state_ids)):
        raise ConflictError("审批与本次请求不一致，请重新预览")
    if batch.status != "preview":
        return summary(batch)  # Idempotent response; never submit a second time.
    if batch.expires_at.replace(tzinfo=timezone.utc) <= utc_now():
        raise ConflictError("审批预览已过期，请重新预览")
    store = session.get(Store, store_id)
    if not store or not store.enabled:
        raise ConflictError("店铺已停用")
    client = _client(store)  # Credential failures occur before consuming approval.
    try:
        claimed = session.execute(update(AdExecutionBatch).where(AdExecutionBatch.id == batch.id, AdExecutionBatch.status == "preview")
                                  .values(status="executing", active_store=store_id)).rowcount
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ConflictError("该店铺已有广告批次正在执行或待核对") from None
    session.refresh(batch)
    if not claimed:
        return summary(batch)
    try:
        analyze_store(session, store_id)
        current = _plan(session, store_id, request.action, request.state_ids)
        if _signature(current) != _signature(plan):
            raise ConflictError("分析依据或目标值已变化，请重新预览并审批")
    except Exception:
        batch.status = "stale"; batch.active_store = None; session.commit()
        raise
    results = [{**row, "status": "blocked"} for row in plan["blocked"]]
    results += [{"sku_id": item["sku_id"], "campaign_id": item["campaign_id"], "status": "not_attempted"} for item in plan["items"]]
    batch.results_json = dumps(results); session.commit()
    for item in plan["items"]:
        audit = AdAction(ad_sku_id=item["ad_sku_id"], action="STOP_ADS_CANDIDATE" if batch.action == "STOP_ADS_CANDIDATE" else "INCREASE_TARGET_DRR_CANDIDATE",
                         status="executing", previous_target_drr=item["previous_target_drr"], proposed_target_drr=item["proposed_target_drr"],
                         reason=item["reason"], before_metrics_json=dumps({"approval_id": batch.id, "approved_by": actor, "proposal": item}), decided_at=utc_now())
        session.add(audit); session.flush()
        result = next(row for row in results if row.get("campaign_id") == item["campaign_id"] and row["sku_id"] == item["sku_id"])
        result.update(audit_id=audit.id, status="executing")
        batch.results_json = dumps(results); session.commit()
        # Intent is durable before any external side effect.
        try:
            if batch.action == "STOP_ADS_CANDIDATE":
                upstream = client.delete_campaign_products(item["campaign_id"], [item["sku_id"]])
            else:
                upstream = client.update_campaign_products(item["campaign_id"], [{"sku": item["sku_id"], "targetCir": round(float(item["proposed_target_drr"]) * 100, 2)}])
            audit.status = result["status"] = "executed"
            audit.after_metrics_json = dumps({"upstream": upstream, "executed_at": utc_now().isoformat()})
            _apply_confirmed(session, batch, item)
        except Exception as error:
            # Even an upstream error may follow acceptance. Read-only reconciliation
            # is mandatory; no automatic replay of this or a new batch for the store.
            audit.status = result["status"] = "unknown"
            result["error"] = "平台结果待核对，请使用核对按钮；系统不会自动重发"
            audit.after_metrics_json = dumps({"error_type": type(error).__name__, "message": str(error)[:1000]})
        batch.results_json = dumps(results); session.commit()
        if result["status"] == "unknown":
            break
    batch.status = "unknown" if any(r["status"] in {"unknown", "executing"} for r in results) else "completed"
    if batch.status == "completed":
        batch.active_store = None
    batch.results_json = dumps(results); session.commit()
    if any(row["status"] == "executed" for row in results):
        try:
            analyze_store(session, store_id)
        except Exception:
            session.rollback()  # Execution result remains durable if analysis fails.
    return summary(batch)


def reconcile(session: Session, identity: str, store_id: str) -> dict:
    if get_settings().runtime_mode == 'server':
        raise ConflictError('广告平台结果核对须由发起人的电脑完成，云端桥接尚未接通')
    batch = _batch(session, identity, store_id)
    if batch.status not in {"unknown", "executing"}:
        return summary(batch)
    # Avoid racing an in-flight HTTP request. Stalled executions are inspectable
    # after the upstream timeout plus a conservative margin.
    if batch.status == "executing" and (utc_now() - batch.updated_at.replace(tzinfo=timezone.utc)).total_seconds() < 300:
        raise ConflictError("批次仍可能正在执行，请稍后核对")
    client = _client(session.get(Store, store_id))
    plan = loads(batch.preview_json, {}); results = loads(batch.results_json, [])
    for result in results:
        if result["status"] not in {"unknown", "executing"}:
            continue
        item = next(row for row in plan["items"] if row["sku_id"] == result["sku_id"] and row["campaign_id"] == result["campaign_id"])
        products = client.campaign_products(item["campaign_id"])
        remote = next((row for row in products if str(row.get("sku")) == item["sku_id"]), None)
        matched = remote is None if batch.action == "STOP_ADS_CANDIDATE" else (
            remote is not None and remote.get("targetCir") is not None
            and abs(float(remote["targetCir"]) / 100 - float(item["proposed_target_drr"])) < 0.00001)
        if matched:
            result["status"] = "executed"; result.pop("error", None)
            audit = session.get(AdAction, result["audit_id"])
            audit.status = "executed"; audit.after_metrics_json = dumps({"reconciled_at": utc_now().isoformat(), "confirmed_from_platform": True})
            _apply_confirmed(session, batch, item)
    unresolved = any(r["status"] in {"unknown", "executing"} for r in results)
    batch.status = "unknown" if unresolved else "completed"
    if not unresolved:
        batch.active_store = None
    batch.results_json = dumps(results); session.commit()
    if not unresolved:
        try:
            analyze_store(session, store_id)
        except Exception:
            session.rollback()
    return summary(batch)
