"""Agent 执行轨迹（trace span 落库 + 回放）。

问题：一次 Agent 运行结束后，留下的只有一个终态结论。要回答「它当时到底做了什么」——
模型每步看到多少上下文、工具当时返回了什么、哪一步触发了护栏改写、反思为什么熔断——
只能去翻散落的日志，而日志里没有「一条完整运行的时序」。

本模块把每次运行落成**一条结构化轨迹**：

- :class:`TraceRecorder` 由编排循环就地埋点（模型调用 / 工具调用 / 反思 / 护栏 / 收尾），
  每个 span 记录入参出参**摘要**、耗时、稳定错误码与所属步号。
- :class:`TraceStore` 落在 :mod:`app.kvstore`（内存 / Redis 可插拔）：多副本部署切 Redis 后，
  用户在 A 副本跑出的轨迹到 B 副本照样能打开。
- 轨迹是**事故复盘的输入**：:mod:`app.evals.incidents` 只读一份轨迹就能确定性推导出
  回归用例的断言（见该模块），因此轨迹必须留下**原始请求**（``request``），否则无法回放。

两个刻意的取舍：

**一次写**。轨迹在收敛 / 失败时**整体落库一次**，而不是每步落一次。逐 step 落库会把
每轮对话的存储写入放大到 N 倍，而轨迹的价值在「事后完整回看」。代价是进程在运行中被杀
（OOM / 强制重启）时这一条轨迹不落库——此时盘上仍有检查点台账（见 :mod:`app.checkpoints`），
「本轮做了一半就挂了」这个事实由台账承载，两者职责不重叠。

**只存摘要**。完整计划可达数百 KB，逐 span 全量留存会让轨迹体与存储双双失控。因此
``input`` / ``output`` 只保留关键字段与截断后的字符串（见 :func:`compact_payload`）。

存储故障语义沿用 :mod:`app.kvstore`：轨迹是**状态数据**，后端不可用时显式失败（映射 503），
不静默降级成「查不到轨迹」——那会被误读成「这次运行没有轨迹」。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .constants import (
    MAX_TRACE_INDEX,
    MAX_TRACE_SPANS,
    MAX_TRACE_TEXT_CHARS,
    TRACE_KIND_GUARD,
    TRACE_KIND_REFLECT,
    TRACE_KIND_TOOL,
    TRACE_KINDS,
    TRACE_PREFIX,
    TRACE_RUN_ERROR,
    TRACE_RUN_STATUSES,
    TRACE_STATUS_DEGRADED,
    TRACE_STATUS_ERROR,
    TRACE_STATUS_OK,
    TRACE_STATUSES,
)
from .kvstore import KeyValueStore, build_kv_store
from .metrics import METRICS
from .models import AgentTrace, CheckpointRequest, TraceSpan, TraceSummary

logger = logging.getLogger("iteration_planner.trace")

#: 全局索引（用于跨主体排查 / 兜底读取）
_INDEX_ALL = "*"
#: 单会话索引保留条数上限
_MAX_SESSION_INDEX = 10
#: 摘要中列表元素的保留条数（超过部分折叠为 ``…``）
_MAX_LIST_ITEMS = 10


def compact_payload(payload: Any, *, limit: int = MAX_TRACE_TEXT_CHARS, depth: int = 0) -> Any:
    """把任意载荷压成**可存可读的摘要**（递归截断长字符串与长列表）。

    轨迹要能回答「模型当时看到什么、工具当时返回了什么关键字段」，而不是充当数据副本：
    所以这里保留结构（键名仍然可读、可定位），只折叠**体积**。
    """
    if depth > 4:
        return "…（层级过深已省略）"
    if isinstance(payload, str):
        if len(payload) <= limit:
            return payload
        return f"{payload[:limit]}…（共 {len(payload)} 字符）"
    if isinstance(payload, dict):
        return {str(key): compact_payload(value, limit=limit, depth=depth + 1) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        items = [compact_payload(item, limit=limit, depth=depth + 1) for item in payload[:_MAX_LIST_ITEMS]]
        if len(payload) > _MAX_LIST_ITEMS:
            items.append(f"…（另有 {len(payload) - _MAX_LIST_ITEMS} 项）")
        return items
    if isinstance(payload, (int, float, bool)) or payload is None:
        return payload
    return compact_payload(str(payload), limit=limit, depth=depth + 1)


@dataclass
class TraceRecorder:
    """一次编排的轨迹收集器（就地埋点，收敛时整体落库）。"""

    kind: str
    session_id: str = ""
    owner: str = ""
    request: CheckpointRequest | None = None
    max_spans: int = MAX_TRACE_SPANS
    _spans: list[TraceSpan] = field(default_factory=list, repr=False)
    _started: float = field(default_factory=time.time, repr=False)
    _step: int = field(default=0, repr=False)

    def set_step(self, step: int) -> None:
        """标记后续 span 归属的编排步号（步外 span——检索 / 收尾——保持 0）。"""
        self._step = step

    def span(
        self,
        kind: str,
        title: str,
        *,
        status: str = TRACE_STATUS_OK,
        code: str | None = None,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        detail: str = "",
        duration_ms: float = 0.0,
    ) -> None:
        """记一个时间片；超过上限后**静默丢弃**（保留前 N 条，不抛错影响主链路）。"""
        if len(self._spans) >= self.max_spans:
            return
        self._spans.append(
            TraceSpan(
                index=len(self._spans) + 1,
                kind=kind,
                title=title,
                at=round(max(0.0, time.time() - self._started), 3),
                durationMs=max(0, int(duration_ms)),
                status=status,
                code=code,
                step=self._step,
                input=compact_payload(input or {}),
                output=compact_payload(output or {}),
                detail=detail[:400],
            )
        )

    @property
    def spans(self) -> list[TraceSpan]:
        return list(self._spans)

    @property
    def elapsed_ms(self) -> int:
        return max(0, int((time.time() - self._started) * 1000))

    def build(
        self,
        *,
        status: str,
        steps: int,
        plan_id: str | None,
        pending_count: int,
        tool_chain: list[str],
        content: str = "",
        error: str | None = None,
        trace_id: str | None = None,
    ) -> AgentTrace:
        """由收集到的 span 与终态事实装配轨迹对象（纯函数，可离线测试）。"""
        failed_tools = [
            str(span.input.get("tool"))
            for span in self._spans
            if span.kind == TRACE_KIND_TOOL and span.status == TRACE_STATUS_ERROR and span.input.get("tool")
        ]
        guard_count = sum(1 for span in self._spans if span.kind == TRACE_KIND_GUARD)
        reflection_count = sum(1 for span in self._spans if span.kind == TRACE_KIND_REFLECT)
        degraded = any(span.status == TRACE_STATUS_DEGRADED for span in self._spans)
        # 事故信号：编排失败 / 有失败工具 / 有护栏拦截 / 收尾为降级。
        # 由服务端确定性计算，前端只渲染不推断——「什么算事故」必须是可复算的口径。
        has_incident = bool(error) or bool(failed_tools) or guard_count > 0 or degraded
        # 失败路径（编排抛错）拿不到终态结果，步数只能由 span 反推：轨迹要能回答
        # 「它是在第几步崩的」，否则事故复盘时连断点位置都定位不到
        resolved_steps = steps or max((span.step for span in self._spans), default=0)
        return AgentTrace(
            traceId=trace_id or uuid.uuid4().hex,
            sessionId=self.session_id,
            owner=self.owner or "anonymous",
            kind=self.kind,
            status=status if status in TRACE_RUN_STATUSES else TRACE_RUN_ERROR,
            request=self.request,
            spans=self.spans,
            steps=resolved_steps,
            planId=plan_id,
            pendingCount=pending_count,
            toolChain=list(tool_chain),
            failedTools=failed_tools,
            reflectionCount=reflection_count,
            guardCount=guard_count,
            contentPreview=content[:MAX_TRACE_TEXT_CHARS],
            error=error,
            durationMs=self.elapsed_ms,
            createdAt=self._started,
            hasIncident=has_incident,
        )


def summarize_trace(trace: AgentTrace) -> TraceSummary:
    """轨迹 → 列表项（去掉 span 明细，避免列表响应过大）。"""
    return TraceSummary(
        traceId=trace.traceId,
        sessionId=trace.sessionId,
        kind=trace.kind,
        status=trace.status,
        steps=trace.steps,
        planId=trace.planId,
        pendingCount=trace.pendingCount,
        spanCount=len(trace.spans),
        toolChain=list(trace.toolChain),
        failedTools=list(trace.failedTools),
        reflectionCount=trace.reflectionCount,
        guardCount=trace.guardCount,
        durationMs=trace.durationMs,
        createdAt=trace.createdAt,
        hasIncident=trace.hasIncident,
        incidentCaseId=trace.incidentCaseId,
        contentPreview=trace.contentPreview[:200],
    )


class TraceStore:
    """轨迹存储（构建在可插拔键值存储之上）。"""

    def __init__(self, kv: KeyValueStore, *, ttl_seconds: float) -> None:
        self._kv = kv
        self._ttl = ttl_seconds

    @property
    def backend(self) -> str:
        return self._kv.name

    def describe(self) -> dict[str, Any]:
        return {
            "kinds": list(TRACE_KINDS),
            "statuses": list(TRACE_STATUSES),
            "runStatuses": list(TRACE_RUN_STATUSES),
            **self._kv.describe(),
        }

    async def close(self) -> None:
        await self._kv.close()

    # ------------------------------- 读写 -------------------------------

    async def save(self, trace: AgentTrace) -> AgentTrace:
        """写入轨迹并维护主体 / 会话 / 全局索引。"""
        await self._kv.set(self._key(trace.traceId), trace.model_dump(), self._ttl)
        await self._touch_index(self._owner_index_key(trace.owner), trace.traceId, MAX_TRACE_INDEX)
        if trace.owner not in ("anonymous", ""):
            await self._touch_index(self._owner_index_key(_INDEX_ALL), trace.traceId, MAX_TRACE_INDEX)
        if trace.sessionId:
            await self._touch_index(self._session_index_key(trace.sessionId), trace.traceId, _MAX_SESSION_INDEX)
        METRICS.inc_counter(
            "agent_trace_total",
            {"kind": trace.kind, "status": trace.status, "incident": "yes" if trace.hasIncident else "no"},
            help="Agent 执行轨迹落库次数（按类型/终态/是否含事故信号分标签）",
        )
        return trace

    async def get(self, trace_id: str) -> AgentTrace | None:
        raw = await self._kv.get(self._key(trace_id))
        if not isinstance(raw, dict):
            METRICS.inc_counter("agent_trace_operations_total", {"operation": "get", "status": "miss"})
            return None
        try:
            trace = AgentTrace.model_validate(raw)
        except Exception:  # noqa: BLE001 - 脏数据（版本不符）按不存在处理
            logger.warning("执行轨迹 %s 结构非法，已按不存在处理", trace_id)
            METRICS.inc_counter("agent_trace_operations_total", {"operation": "get", "status": "corrupt"})
            return None
        METRICS.inc_counter("agent_trace_operations_total", {"operation": "get", "status": "hit"})
        return trace

    async def list(self, owner: str | None = None, *, limit: int = 50, kind: str | None = None) -> list[AgentTrace]:
        """按主体筛选的轨迹列表（``owner=None`` 表示全局最近轨迹）。"""
        key = self._owner_index_key(owner or _INDEX_ALL)
        ids = await self._read_index(key)
        if not ids and owner:
            # 主体索引缺失（首个轨迹 / 索引被淘汰）：回落全局索引再过滤，宁多读几条
            ids = await self._read_index(self._owner_index_key(_INDEX_ALL))
        found: list[AgentTrace] = []
        for trace_id in ids:
            if len(found) >= limit:
                break
            trace = await self.get(trace_id)
            if trace is None:
                continue
            if owner and trace.owner != owner:
                continue
            if kind and trace.kind != kind:
                continue
            found.append(trace)
        return found

    async def attach_incident_case(self, trace_id: str, case_id: str) -> None:
        """回填「该轨迹已沉淀为回归用例」（失败不打断主流程：用例已经建好了）。"""
        trace = await self.get(trace_id)
        if trace is None:
            return
        trace.incidentCaseId = case_id
        await self.save(trace)

    async def delete(self, trace_id: str) -> None:
        await self._kv.delete(self._key(trace_id))

    # ------------------------------- 内部实现 -------------------------------

    def _key(self, trace_id: str) -> str:
        return f"tr:{trace_id}"

    def _owner_index_key(self, owner: str) -> str:
        return f"index:owner:{owner}"

    def _session_index_key(self, session_id: str) -> str:
        return f"index:session:{session_id}"

    async def _touch_index(self, key: str, trace_id: str, cap: int) -> None:
        ids = await self._read_index(key)
        ids = [trace_id, *[item for item in ids if item != trace_id]][:cap]
        await self._kv.set(key, ids, self._ttl)

    async def _read_index(self, key: str) -> list[str]:
        raw = await self._kv.get(key)
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, str)]


def build_trace_store(
    *,
    driver: str = "auto",
    redis_url: str = "",
    max_entries: int = 1024,
    ttl_seconds: float,
) -> TraceStore:
    """按配置构建轨迹存储（``driver=redis`` 缺依赖 / 缺地址时启动即失败，见 :mod:`app.checkpoints`）。"""
    kv = build_kv_store(
        store="trace",
        driver=driver,
        redis_url=redis_url,
        max_entries=max(max_entries, 256),
        prefix=TRACE_PREFIX,
    )
    return TraceStore(kv, ttl_seconds=ttl_seconds)


__all__ = [
    "TraceRecorder",
    "TraceStore",
    "build_trace_store",
    "compact_payload",
    "summarize_trace",
]
