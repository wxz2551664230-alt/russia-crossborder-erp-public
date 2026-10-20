from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Iterable

from erp.modules.ozon_follow.legacy.services.alibaba_client import AlibabaApiError, AlibabaNewtonClient


class CloudNewtonError(RuntimeError):
    pass


class CloudNewtonTaskFailed(CloudNewtonError):
    pass


class CloudNewtonTaskTimeout(CloudNewtonError):
    pass


class NewtonTaskStatus(StrEnum):
    INIT = "INIT"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAIT_SKILL = "WAIT_SKILL"
    WAIT_USER = "WAIT_USER"
    END = "END"
    KILL = "KILL"
    ERROR = "ERROR"


TERMINAL_STATUSES = {
    NewtonTaskStatus.END.value,
    NewtonTaskStatus.KILL.value,
    NewtonTaskStatus.ERROR.value,
}


@dataclass(slots=True)
class NewtonChunk:
    type: str
    content: Any
    timestamp: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: Any) -> "NewtonChunk":
        if isinstance(value, str):
            value = _json_or_value(value)
        if not isinstance(value, dict):
            return cls(type="unknown", content=value, raw={"value": value})
        content = _json_or_value(value.get("content"))
        timestamp = value.get("timestamp")
        return cls(
            type=str(value.get("type") or "unknown"),
            content=content,
            timestamp=int(timestamp) if timestamp is not None else None,
            raw=value,
        )


@dataclass(slots=True)
class NewtonTableReference:
    task_id: str
    scene: str
    sub_scene: str
    table_id: str
    stage: str = ""


@dataclass(slots=True)
class NewtonTaskSnapshot:
    task_id: str
    session_id: str
    status: str
    success: bool
    next_index: int
    chunks: list[NewtonChunk]
    error: str = ""
    error_code: str = ""
    eagle_trace_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def needs_user_input(self) -> bool:
        return self.status == NewtonTaskStatus.WAIT_USER.value

    def questions(self) -> list[dict[str, Any]]:
        questions: list[dict[str, Any]] = []
        for chunk in self.chunks:
            if chunk.type == "wait_user" and isinstance(chunk.content, dict):
                value = chunk.content.get("questions", [])
                if isinstance(value, list):
                    questions.extend(item for item in value if isinstance(item, dict))
        return questions


@dataclass(slots=True)
class NewtonTaskResult:
    task_id: str
    session_id: str
    status: str
    chunks: list[NewtonChunk]
    next_index: int
    raw: dict[str, Any]

    def table_references(self) -> list[NewtonTableReference]:
        references: list[NewtonTableReference] = []
        seen: set[tuple[str, str, str, str]] = set()
        for chunk in self.chunks:
            if chunk.type != "complex_table" or not isinstance(chunk.content, dict):
                continue
            scene = str(chunk.content.get("scene") or "")
            sub_scene = str(chunk.content.get("subScene") or "")
            table_id = str(chunk.content.get("id") or "")
            stage = str(chunk.content.get("stage") or "")
            if not scene or not sub_scene or not table_id:
                continue
            key = (scene, sub_scene, table_id, stage)
            if key in seen:
                continue
            seen.add(key)
            references.append(
                NewtonTableReference(
                    task_id=self.task_id,
                    scene=scene,
                    sub_scene=sub_scene,
                    table_id=table_id,
                    stage=stage,
                )
            )
        return references


def _json_or_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _unwrap(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CloudNewtonError("云牛顿接口返回格式不是JSON对象")
    current = payload
    for _ in range(3):
        nested = current.get("result")
        if isinstance(nested, dict) and (
            "success" in nested
            or "taskId" in nested
            or "status" in nested
            or "data" in nested
        ):
            current = nested
            continue
        break
    return current


def _parse_chunks(value: Any) -> list[NewtonChunk]:
    value = _json_or_value(value)
    if value is None:
        return []
    if isinstance(value, dict):
        value = value.get("chunks", [])
        value = _json_or_value(value)
    if not isinstance(value, list):
        return []
    return [NewtonChunk.from_value(item) for item in value]


class CloudNewtonClient:
    """Task-oriented client for the official Cloud Newton OpenAPI."""

    NAMESPACE = "com.alibaba.agent"
    VERSION = 1
    PROCUREMENT_SKILL = "1688-supplychain-procurement"

    def __init__(
        self,
        app_key: str,
        app_secret: str,
        access_token: str,
        timeout: int = 30,
        aop_client: AlibabaNewtonClient | None = None,
    ) -> None:
        self.aop = aop_client or AlibabaNewtonClient(
            app_key=app_key,
            app_secret=app_secret,
            access_token=access_token,
            timeout=timeout,
        )

    def create_task(
        self,
        message: str,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        auto: bool | None = None,
        file_urls: list[str] | None = None,
        file_names: list[str] | None = None,
        model: str | None = None,
        workflow_name: str | None = None,
    ) -> NewtonTaskSnapshot:
        if not message.strip():
            raise CloudNewtonError("云牛顿任务内容不能为空")
        if file_names is not None and file_urls is not None and len(file_names) != len(file_urls):
            raise CloudNewtonError("fileNames必须与fileUrls一一对应")
        params: dict[str, Any] = {"message": message.strip()}
        optional = {
            "sessionId": session_id,
            "taskId": task_id,
            "auto": auto,
            "fileUrls": file_urls,
            "fileNames": file_names,
            "model": model,
            "workflowName": workflow_name,
        }
        params.update({key: value for key, value in optional.items() if value is not None})
        payload = self._call("newtoncloud.task.create", params)
        return self._snapshot(payload)

    def create_procurement_search(
        self,
        message: str,
        *,
        image_urls: list[str] | None = None,
        image_names: list[str] | None = None,
        model: str | None = None,
        session_id: str | None = None,
    ) -> NewtonTaskSnapshot:
        """Creates a safe sourcing task without inquiry or ordering."""
        safe_message = (
            message.strip()
            + f"\n\nforceSkillId: {self.PROCUREMENT_SKILL}"
            + "\n安全边界：只做找品及商品/供应商分析；不要询盘，不要下单，"
            "不要付款，不得推测。"
        )
        return self.create_task(
            safe_message,
            session_id=session_id,
            auto=False,
            file_urls=image_urls,
            file_names=image_names,
            model=model,
        )

    def create_exact_match_verification(
        self,
        message: str,
        *,
        image_urls: list[str],
        image_names: list[str] | None = None,
        model: str | None = None,
    ) -> NewtonTaskSnapshot:
        """Starts a read-only second-pass visual verification task."""
        safe_message = (
            message.strip()
            + "\n\n安全边界：只做商品同款核验；不要询盘，不要下单，不要发送消息。"
        )
        return self.create_task(
            safe_message,
            auto=False,
            file_urls=image_urls,
            file_names=image_names,
            model=model,
        )

    def get_task(
        self,
        task_id: str,
        *,
        from_index: int = 0,
        include_blocks: bool = True,
    ) -> NewtonTaskSnapshot:
        if from_index < 0:
            raise CloudNewtonError("fromIndex不能小于0")
        payload = self._call(
            "newtoncloud.task.get",
            {
                "taskId": task_id,
                "fromIndex": from_index,
                "includeBlocks": include_blocks,
            },
        )
        return self._snapshot(payload, fallback_task_id=task_id, fallback_index=from_index)

    def resume_task(
        self,
        *,
        session_id: str,
        task_id: str,
        selected_data: list[dict[str, Any]],
        scene: str = "open_api",
    ) -> dict[str, Any]:
        if not selected_data:
            raise CloudNewtonError("恢复任务时至少需要一条用户回答")
        return self._call(
            "newtoncloud.task.resume",
            {
                "sessionId": session_id,
                "scene": scene,
                "taskId": task_id,
                "selectedData": selected_data,
            },
        )

    def fetch_table(self, reference: NewtonTableReference) -> dict[str, Any]:
        params: dict[str, Any] = {
            "taskId": reference.task_id,
            "scene": reference.scene,
            "subScene": reference.sub_scene,
            "id": reference.table_id,
        }
        if reference.stage:
            params["stage"] = reference.stage
        return self._call("newtoncloud.task.fetch", params)

    def fetch_table_data(self, reference: NewtonTableReference) -> Any:
        payload = self.fetch_table(reference)
        current = _unwrap(payload)
        data = current.get("data", current)
        return _json_or_value(data)

    def fetch_result_tables(self, result: NewtonTaskResult) -> list[Any]:
        # Newton can return the same table block more than once while it is being
        # filled. Keep the latest block and prefer its embedded data; this both
        # preserves the final incremental result and avoids a later fetch call
        # failing after the task itself has already completed.
        tables: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for chunk in result.chunks:
            if chunk.type != "complex_table" or not isinstance(chunk.content, dict):
                continue
            content = chunk.content
            key = (
                str(content.get("scene") or ""),
                str(content.get("subScene") or ""),
                str(content.get("id") or ""),
                str(content.get("stage") or ""),
            )
            if not all(key[:3]):
                continue
            tables[key] = content

        values: list[Any] = []
        for (scene, sub_scene, table_id, stage), content in tables.items():
            data_filled = bool(content.get("__dataFilled"))
            inline_data = content.get("data")
            if data_filled or inline_data not in (None, ""):
                values.append(_json_or_value(inline_data))
                continue
            values.append(self.fetch_table_data(NewtonTableReference(
                task_id=result.task_id,
                scene=scene,
                sub_scene=sub_scene,
                table_id=table_id,
                stage=stage,
            )))
        return values

    def kill_task(self, task_id: str) -> dict[str, Any]:
        return self._call("newtoncloud.task.kill", {"taskId": task_id})

    def list_models(self) -> dict[str, Any]:
        return self._call("newtoncloud.model.list", {})

    def collect_task(
        self,
        task_id: str,
        *,
        session_id: str = "",
        from_index: int = 0,
        poll_interval: float = 2.0,
        timeout_seconds: float = 300.0,
        on_snapshot: Callable[[NewtonTaskSnapshot], None] | None = None,
    ) -> NewtonTaskResult | NewtonTaskSnapshot:
        started = time.monotonic()
        chunks: list[NewtonChunk] = []
        index = from_index
        latest_raw: dict[str, Any] = {}
        current_session = session_id
        while True:
            if time.monotonic() - started > timeout_seconds:
                raise CloudNewtonTaskTimeout(f"云牛顿任务轮询超时：{task_id}")
            snapshot = self.get_task(task_id, from_index=index)
            if on_snapshot:
                on_snapshot(snapshot)
            chunks.extend(snapshot.chunks)
            index = max(index, snapshot.next_index)
            current_session = snapshot.session_id or current_session
            latest_raw = snapshot.raw

            if snapshot.needs_user_input:
                snapshot.chunks = chunks
                snapshot.next_index = index
                return snapshot
            if snapshot.status == NewtonTaskStatus.END.value:
                return NewtonTaskResult(
                    task_id=task_id,
                    session_id=current_session,
                    status=snapshot.status,
                    chunks=chunks,
                    next_index=index,
                    raw=latest_raw,
                )
            if snapshot.status in {
                NewtonTaskStatus.KILL.value,
                NewtonTaskStatus.ERROR.value,
            }:
                raise CloudNewtonTaskFailed(
                    snapshot.error or f"云牛顿任务结束，状态：{snapshot.status}"
                )
            if poll_interval > 0:
                time.sleep(poll_interval)

    def _call(self, api_name: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = self.aop.call_api(
                namespace=self.NAMESPACE,
                api_name=api_name,
                version=self.VERSION,
                params=params,
                raise_api_errors=False,
            )
        except AlibabaApiError as exc:
            raise CloudNewtonError(str(exc)) from exc
        current = _unwrap(payload)
        if current.get("success") is False:
            message = current.get("error") or current.get("message") or "云牛顿调用失败"
            code = current.get("errorCode") or current.get("code") or "UNKNOWN"
            if code == "UNKNOWN" and "积分不足" in str(message):
                code = "INSUFFICIENT_CREDITS"
            raise CloudNewtonError(f"{code}: {message}")
        return payload

    def _snapshot(
        self,
        payload: dict[str, Any],
        *,
        fallback_task_id: str = "",
        fallback_index: int = 0,
    ) -> NewtonTaskSnapshot:
        current = _unwrap(payload)
        chunks = _parse_chunks(current.get("chunks"))
        if not chunks:
            chunks = _parse_chunks(current.get("data"))
        next_index_value = current.get("nextIndex")
        if next_index_value is None:
            next_index_value = current.get("fromIndex")
        if next_index_value is None:
            next_index_value = fallback_index + len(chunks)
        try:
            next_index = int(next_index_value)
        except (TypeError, ValueError):
            next_index = fallback_index + len(chunks)
        return NewtonTaskSnapshot(
            task_id=str(current.get("taskId") or fallback_task_id),
            session_id=str(current.get("sessionId") or ""),
            status=str(current.get("status") or ""),
            success=bool(current.get("success", True)),
            next_index=next_index,
            chunks=chunks,
            error=str(current.get("error") or current.get("message") or ""),
            error_code=str(current.get("errorCode") or ""),
            eagle_trace_id=str(current.get("eagleTraceId") or ""),
            raw=payload,
        )


def build_selected_data(answers: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    return [
        {"question": question.strip(), "selected": answer.strip()}
        for question, answer in answers
        if question.strip() and answer.strip()
    ]


def build_ozon_sourcing_message(
    *,
    title: str,
    ozon_url: str = "",
    image_url: str = "",
    candidate_count: int = 10,
) -> str:
    count = max(1, min(int(candidate_count), 50))
    title_line = title.strip() or "未提供"
    image_line = image_url.strip() or "未提供"
    return (
        f"请以商品主图为首要证据，在1688寻找{count}个可能的同款货源。\n"
        f"商品主图URL：{image_line}\n"
        f"Ozon商品标题（仅用于确认实际销售主体，不用于替代图片匹配）：{title_line}\n"
        "请先判断图片中实际售卖的主体，区分商品本体与展示道具、包装、内容物、"
        "附件或使用场景；候选商品必须与实际售卖主体及关键外观特征一致。\n"
        "请返回结构化候选商品表格，尽量包含：商品ID、商品链接、标题、主图、"
        "供应商、所在地、起批量、库存、参考价格、全部SKU规格组合、各SKU价格、"
        "阶梯价格、重量和包装尺寸。只搜索和返回候选，不得把相似款声称为同款；"
        "无法获取的字段留空并注明原因，不允许推测。"
    )
