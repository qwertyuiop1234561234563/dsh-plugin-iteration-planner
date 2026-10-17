"""工具调度层。

Python Agent 的每个工具都通过 HTTP 调用 TypeScript 引擎（唯一算法事实来源），
不在 Python 侧重写任何算法。工具名与引擎 REST 能力一一对应。

**检索类工具例外**：``kb_search`` / ``kb_list_documents`` / ``memory_search`` 不走上游引擎，
而是直接读取本进程的知识库与长期记忆（**只读**，不产生任何写副作用），
用于 agentic RAG（模型按需自行检索资料与历史约定）。
"""

from __future__ import annotations

from typing import Any

from ..engine_client import EngineClient
from ..errors import ApiError, bad_request, embedding_unavailable


def _require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise bad_request(f"工具入参缺少必填字段：{key}")
    return value


def _optional_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    return value if isinstance(value, str) and value else None


def _require_int(args: dict[str, Any], key: str) -> int:
    value = args.get(key)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise bad_request(f"工具入参 {key} 必须为整数") from None
    if parsed <= 0:
        raise bad_request(f"工具入参 {key} 必须大于 0")
    return parsed


class ToolDispatcher:
    """把 LLM 的 function call 翻译为对引擎 REST 的调用（检索类工具直连本地知识库）。"""

    def __init__(self, engine: EngineClient, *, knowledge: Any | None = None, memory: Any | None = None) -> None:
        self._engine = engine
        self._knowledge = knowledge
        self._memory = memory

    async def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        """执行工具并返回 canonical JSON 结果（供回填模型）。"""
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise bad_request(f"未知工具：{name}")
        return await handler(args)

    # ------------------------------ 计划级 ------------------------------

    async def _tool_plan_iteration(self, args: dict[str, Any]) -> Any:
        """编排入口：一次调用跑完拆解 → 缓冲 → DAG → AOE → 风险 → 燃尽。"""
        payload: dict[str, Any] = {
            "requirement": _require_str(args, "requirement"),
            "iterationDeadline": _require_str(args, "iterationDeadline"),
            "devMemberCount": _require_int(args, "devMemberCount"),
        }
        start = _optional_str(args, "iterationStartDate")
        if start:
            payload["iterationStartDate"] = start
        return await self._engine.request("POST", "/plans", json=payload)

    async def _tool_replan_all(self, args: dict[str, Any]) -> Any:
        plan_id = _require_str(args, "planId")
        payload: dict[str, Any] = {}
        for field in ("iterationDeadline", "iterationStartDate"):
            value = _optional_str(args, field)
            if value:
                payload[field] = value
        if args.get("devMemberCount") is not None:
            payload["devMemberCount"] = _require_int(args, "devMemberCount")
        return await self._engine.request("POST", f"/plans/{plan_id}/replan", json=payload)

    async def _tool_get_plan(self, args: dict[str, Any]) -> Any:
        return await self._engine.request("GET", f"/plans/{_require_str(args, 'planId')}")

    async def _tool_list_plans(self, _args: dict[str, Any]) -> Any:
        return await self._engine.request("GET", "/plans")

    # ------------------------------ 任务级 ------------------------------

    async def _tool_task_add(self, args: dict[str, Any]) -> Any:
        plan_id = _require_str(args, "planId")
        draft = {k: v for k, v in args.items() if k != "planId" and v is not None}
        return await self._engine.request("POST", f"/plans/{plan_id}/tasks", json=draft)

    async def _tool_task_update(self, args: dict[str, Any]) -> Any:
        plan_id = _require_str(args, "planId")
        task_id = _require_str(args, "taskId")
        payload = {"updateField": _require_str(args, "updateField"), "newValue": args.get("newValue")}
        return await self._engine.request("PATCH", f"/plans/{plan_id}/tasks/{task_id}", json=payload)

    async def _tool_task_delete(self, args: dict[str, Any]) -> Any:
        plan_id = _require_str(args, "planId")
        task_id = _require_str(args, "taskId")
        return await self._engine.request("DELETE", f"/plans/{plan_id}/tasks/{task_id}")

    async def fetch_tasks(self, plan_id: str) -> list[dict[str, Any]]:
        """读取计划当前的任务明细（供 :class:`AgentRunner` 在登记待确认写操作前校验目标）。

        仅返回 ``taskList``：调用方需要的是「计划里到底有哪些 taskId」，整份计划（含风险、
        燃尽等）没有用处。
        """
        plan = await self._engine.request("GET", f"/plans/{plan_id}")
        tasks = (plan or {}).get("taskList") if isinstance(plan, dict) else None
        return list(tasks) if isinstance(tasks, list) else []

    # ---------------------- 视图 / 校验 / 报表（只读或校验） ----------------------

    async def _tool_dag_build(self, args: dict[str, Any]) -> Any:
        return await self._engine.request("GET", f"/plans/{_require_str(args, 'planId')}/graph")

    async def _tool_dag_check(self, args: dict[str, Any]) -> Any:
        return await self._engine.request("POST", f"/plans/{_require_str(args, 'planId')}/dag-check")

    async def _tool_schedule_calc(self, args: dict[str, Any]) -> Any:
        return await self._engine.request("GET", f"/plans/{_require_str(args, 'planId')}/critical-path")

    async def _tool_risk_scan(self, args: dict[str, Any]) -> Any:
        return await self._engine.request("GET", f"/plans/{_require_str(args, 'planId')}/risks")

    async def _tool_report_generate(self, args: dict[str, Any]) -> Any:
        return await self._engine.request("GET", f"/plans/{_require_str(args, 'planId')}/report")

    # ---------------------- 知识库 / 长期记忆（只读，本地检索） ----------------------

    def _require_knowledge(self) -> Any:
        if self._knowledge is None:
            raise embedding_unavailable("知识库未启用（未配置向量化模型），无法检索文档")
        return self._knowledge

    def _require_memory(self) -> Any:
        if self._memory is None:
            raise embedding_unavailable("长期记忆未启用（未配置向量化模型），无法召回记忆")
        return self._memory

    async def _tool_kb_search(self, args: dict[str, Any]) -> Any:
        knowledge = self._require_knowledge()
        top_k = args.get("topK")
        hits = await knowledge.search(_require_str(args, "query"), top_k=int(top_k) if top_k else None)
        return {"count": len(hits), "hits": hits}

    async def _tool_kb_list_documents(self, _args: dict[str, Any]) -> Any:
        knowledge = self._require_knowledge()
        documents = await knowledge.list_documents()
        return {"count": len(documents), "documents": documents}

    async def _tool_memory_search(self, args: dict[str, Any]) -> Any:
        memory = self._require_memory()
        items = await memory.recall(_require_str(args, "query"))
        return {"count": len(items), "memories": items}


__all__ = ["ToolDispatcher", "ApiError"]