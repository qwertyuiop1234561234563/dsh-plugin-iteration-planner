"""主动巡检 Agent（后台定时巡检 + 增量预警）。

参考同类企业项目（AutoGPT 的 background monitor / Dify 的告警订阅）的做法：把「等待用户提问」
变成「系统主动发现问题并推送」。实现要点：

1. **只巡检、不改数据**：巡检只调 ``GET /plans/{id}``，读引擎算好的事实（任务状态、关键路径、
   风险清单、燃尽末点），不做任何写操作，也**不重算任何算法**；
2. **增量比对**：每轮把「本轮信号集合」与「上一轮快照」比对，只有**新出现**与**已恢复**才推送，
   避免同一个问题每分钟刷屏（并叠加 ``patrol_alert_cooldown_seconds`` 冷却去重）；
3. **多副本安全**：巡检是「每副本都跑」的幂等任务——同一计划可能在多个副本被同时巡检，
   但推送的是**告警事件**而非写操作，重复告警由冷却窗口 + 前端去重兜底，不构成数据竞争；
4. **失败不阻断**：引擎不可达 / 单计划读取失败只计数与告警，不中断整轮巡检（其余计划继续）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import Any

from .agent.analytics import RISK_MARKERS, critical_path, progress_stats, task_map, task_names
from .constants import (
    PATROL_FINDING_NEW,
    PATROL_FINDING_ONGOING,
    PATROL_FINDING_RESOLVED,
    PATROL_SEVERITY_CRITICAL,
    PATROL_SEVERITY_INFO,
    PATROL_SEVERITY_RANK,
    PATROL_SEVERITY_WARNING,
    PATROL_SIGNAL_CRITICAL_PATH_DRIFT,
    PATROL_SIGNAL_DEADLINE_RISK,
    PATROL_SIGNAL_DEPENDENCY_BLOCKED,
    PATROL_SIGNAL_LABELS,
    PATROL_SIGNAL_PROGRESS_DRIFT,
    PATROL_SIGNAL_RESOURCE_OVERLOAD,
    PATROL_SIGNALS,
    WS_EVENT_PATROL_FINDING,
)
from .engine_client import EngineClient
from .errors import ApiError
from .metrics import METRICS
from .ws import WsBroadcaster

logger = logging.getLogger("iteration_planner.patrol")

#: 进度落后多少天开始告警（派生指标，口径见 README）
PROGRESS_DRIFT_WARN_DAYS = 1
#: 关键路径上未完成且无责任人的任务达到该数量即告警
UNASSIGNED_CRITICAL_WARN = 1
#: 关键路径指纹的 key 前缀（仅用于区分「状态指针」与「问题实体」，见 run_once）
_PATH_KEY_PREFIX = "path:"


class PatrolService:
    """后台巡检：定时读取可见计划 → 聚合异常信号 → 与上一轮快照比对 → WS 推送增量。"""

    def __init__(
        self,
        engine: EngineClient,
        broadcaster: WsBroadcaster,
        *,
        interval_seconds: float,
        max_plans: int,
        cooldown_seconds: float,
        escalate_severity: str = PATROL_SEVERITY_CRITICAL,
        finding_ttl_seconds: float = 86_400.0,
        max_findings: int = 200,
        plan_ids: frozenset[str] | None = None,
    ) -> None:
        self._engine = engine
        self._broadcaster = broadcaster
        self._interval = max(5.0, float(interval_seconds))
        self._max_plans = max(1, int(max_plans))
        self._cooldown = max(0.0, float(cooldown_seconds))
        self._escalate = escalate_severity
        self._finding_ttl = max(0.0, float(finding_ttl_seconds))
        self._max_findings = max(1, int(max_findings))
        #: 巡检范围：``None`` 表示全部计划（生产默认）；测试可注入固定集合实现确定性
        self._plan_ids = plan_ids
        #: planId → {(signal, key): 最近上报时间}，用于冷却去重
        self._last_alert: dict[str, dict[tuple[str, str], float]] = {}
        #: planId → 上一轮信号指纹集合（用于识别「新出现 / 已恢复」）
        self._snapshot: dict[str, set[tuple[str, str]]] = {}
        #: planId → 上一轮关键路径指纹（**单独记录**：漂移是「与上次比对」的结论，
        #: 不能从信号集合里推断，否则会把「首次记录到一个未指派的路径任务」误判成路径漂移）
        self._last_path: dict[str, str] = {}
        #: 当前留存的发现：``(planId, signal, key)`` → 发现。**按信号去重**——
        #: 同一问题反复检出时更新同一行（状态转 ``ongoing``、刷新时间），而不是每轮插一条。
        #: 插事件式追加会让「当前问题列表」被同一个问题刷屏（默认冷却 30 分钟，一天最多 48 条
        #: 同一问题的重复行，还会把其它问题挤出 ``max_findings`` 上限）；同样地，问题恢复后
        #: 旧行若不更新就会一直停在 ``new``，与 WS 已下发的「已恢复」通知自相矛盾。
        self._findings: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_run_at: float | None = None
        self._last_summary: dict[str, Any] = {}

    # ------------------------------- 生命周期 -------------------------------

    def start(self) -> None:
        """启动周期巡检（首轮**延迟一个周期**执行，避免启动瞬间集中打引擎）。"""
        if self._task is None:
            self._task = asyncio.create_task(self._run_forever(), name="patrol-agent")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run_forever(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            if self._stop.is_set():
                return
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 后台任务异常必须记录，不能静默丢失
                METRICS.inc_counter("patrol_runs_total", {"outcome": "failed"})
                logger.exception("巡检轮次出现未预期异常（继续下一轮）")

    # ------------------------------- 巡检主体 -------------------------------

    async def run_once(self, plan_ids: frozenset[str] | None = None) -> dict[str, Any]:
        """执行一轮巡检；返回本轮摘要（供 ``POST /api/agent/patrol/run`` 与测试使用）。"""
        scope = plan_ids if plan_ids is not None else self._plan_ids
        started = time.monotonic()
        plans, authoritative = await self._load_plans(scope)
        self._apply_retention()
        created: list[dict[str, Any]] = []
        resolved: list[dict[str, Any]] = []
        errors: list[str] = []
        for plan in plans:
            plan_id = str(plan.get("planId") or "")
            if not plan_id:
                continue
            try:
                signals = self._signals(plan)
            except Exception:  # noqa: BLE001 - 单计划分析失败不影响其它计划
                errors.append(plan_id)
                logger.exception("巡检信号聚合失败（planId=%s）", plan_id)
                continue
            current = {(signal, key) for signal, _severity, _title, key, _detail in signals}
            previous = self._snapshot.get(plan_id, set())
            for signal, severity, title, key, detail in signals:
                status = PATROL_FINDING_NEW if (signal, key) not in previous else PATROL_FINDING_ONGOING
                if status == PATROL_FINDING_ONGOING and not self._should_alert(plan_id, signal, key):
                    continue
                finding = self._record(plan, signal, severity, title, key, detail, status)
                created.append(finding)
                await self._publish(finding)
            for signal, key in previous - current:
                if signal == PATROL_SIGNAL_CRITICAL_PATH_DRIFT and key.startswith(_PATH_KEY_PREFIX):
                    # 关键路径指纹是「当前状态指针」而非问题实体：路径变化时旧指纹自然消失，
                    # 不该被判定为「漂移已恢复」（那是语义错误，会误导用户以为风险解除）
                    continue
                finding = self._record(
                    plan,
                    signal,
                    PATROL_SEVERITY_INFO,
                    f"{PATROL_SIGNAL_LABELS.get(signal, signal)}已恢复",
                    key,
                    "本轮巡检未再检出该异常",
                    PATROL_FINDING_RESOLVED,
                )
                resolved.append(finding)
                await self._publish(finding)
            self._snapshot[plan_id] = current

        if authoritative:
            self._prune_unlisted({str(plan.get("planId") or "") for plan in plans})

        self._last_run_at = time.time()
        duration_ms = int((time.monotonic() - started) * 1000)
        summary = {
            "plans": len(plans),
            "signals": len(created) + len(resolved),
            "alerts": len(created),
            "resolved": len(resolved),
            "errors": errors,
            "durationMs": duration_ms,
            "at": self._last_run_at,
        }
        self._last_summary = summary
        METRICS.inc_counter("patrol_runs_total", {"outcome": "ok"})
        for finding in created:
            METRICS.inc_counter(
                "patrol_findings_total",
                {"signal": str(finding["signal"]), "severity": str(finding["severity"])},
            )
        logger.info(
            "巡检完成：计划 %d 个，新告警 %d 条，恢复 %d 条，耗时 %dms",
            len(plans),
            len(created),
            len(resolved),
            duration_ms,
        )
        return summary

    async def _load_plans(self, scope: frozenset[str] | None) -> tuple[list[dict[str, Any]], bool]:
        """读取待巡检计划。

        :return: ``(计划列表, 清单是否权威)``。清单来自 ``GET /plans`` 时权威（可用于清理已删除
            计划的快照）；引擎不可达或调用方显式给定范围时不权威，绝不据此清状态——否则一次
            引擎抖动就会把「持续存在的问题」重置成全量重报。
        """
        authoritative = scope is None
        if scope is not None:
            candidates = sorted(scope)[: self._max_plans]
        else:
            try:
                listed = await self._engine.request("GET", "/plans")
            except ApiError as exc:
                METRICS.inc_counter("patrol_runs_total", {"outcome": "engine_unavailable"})
                logger.warning("巡检无法列出计划：%s", exc.message)
                return [], False
            rows = listed if isinstance(listed, list) else (listed or {}).get("items") or []
            candidates = [
                str(row.get("planId"))
                for row in rows
                if isinstance(row, dict) and row.get("planId")
            ][: self._max_plans]
        plans: list[dict[str, Any]] = []
        for plan_id in candidates:
            try:
                detail = await self._engine.request("GET", f"/plans/{plan_id}")
            except ApiError as exc:
                logger.warning("巡检读取计划失败（planId=%s）：%s", plan_id, exc.message)
                continue
            if isinstance(detail, dict):
                plans.append(detail)
        return plans, authoritative

    def _prune_unlisted(self, listed: set[str]) -> None:
        """清理不在清单里的计划状态（计划已删除）：避免长期运行后内存状态无界增长。"""
        for plan_id in list(self._snapshot):
            if plan_id not in listed:
                self._snapshot.pop(plan_id, None)
                self._last_path.pop(plan_id, None)
                self._last_alert.pop(plan_id, None)
        # 已删除计划的留存发现同样要清掉：否则「当前问题列表」会挂着不存在计划的陈旧告警
        # （最长滞留 ``finding_ttl``），前端与运维都会把它当成仍然存在的问题。
        for slot in [slot for slot in self._findings if slot[0] not in listed]:
            self._findings.pop(slot, None)

    # ------------------------------- 信号聚合 -------------------------------

    def _signals(self, plan: dict[str, Any]) -> list[tuple[str, str, str, str, str]]:
        """把引擎事实聚合为 ``(signal, severity, title, key, detail)`` 列表。

        ``key`` 是**信号指纹**（用于跨轮识别同一问题），例如具体任务 ID 或关键路径集合摘要。
        """
        stats = progress_stats(plan)
        names = task_map(plan)
        out: list[tuple[str, str, str, str, str]] = []

        # 1) 进度偏差：状态为 DELAYED 的任务，或时间进度显著领先于工时完成比例
        for task in plan.get("taskList") or []:
            if isinstance(task, dict) and str(task.get("status")) == "DELAYED":
                label = str(task.get("assignee") or "未指派")
                out.append(
                    (
                        PATROL_SIGNAL_PROGRESS_DRIFT,
                        PATROL_SEVERITY_WARNING,
                        f"任务延期：{task.get('taskName')}",
                        f"delayed:{task.get('taskId')}",
                        f"责任人 {label}，预估 {task.get('estimatedHours')}h，请确认新排期",
                    )
                )
        drift = stats.get("driftDays")
        if isinstance(drift, int) and drift >= PROGRESS_DRIFT_WARN_DAYS:
            out.append(
                (
                    PATROL_SIGNAL_PROGRESS_DRIFT,
                    PATROL_SEVERITY_WARNING,
                    f"整体进度落后约 {drift} 天",
                    "ratio",
                    f"时间已过 {stats.get('elapsedRatio')}，工时完成 {stats.get('doneRatio')}",
                )
            )

        # 2) 关键路径漂移：与**上一轮记录的关键路径**比对（首次巡检只记录基线，不算漂移）
        critical = critical_path(plan)
        plan_id = str(plan.get("planId") or "")
        if critical:
            key = f"{_PATH_KEY_PREFIX}{','.join(critical)}"
            previous_path = self._last_path.get(plan_id)
            self._last_path[plan_id] = key
            if previous_path and previous_path != key:
                out.append(
                    (
                        PATROL_SIGNAL_CRITICAL_PATH_DRIFT,
                        PATROL_SEVERITY_WARNING,
                        "关键路径发生变化",
                        key,
                        f"当前关键路径：{' → '.join(task_names(plan, critical)[:8])}",
                    )
                )
            unassigned = stats.get("unassignedCriticalTasks") or []
            if len(unassigned) >= UNASSIGNED_CRITICAL_WARN:
                out.append(
                    (
                        PATROL_SIGNAL_CRITICAL_PATH_DRIFT,
                        PATROL_SEVERITY_WARNING,
                        f"{len(unassigned)} 个关键路径任务未指派责任人",
                        f"unassigned:{','.join(sorted(unassigned))}",
                        "关键路径任务无人负责会直接威胁交付日期",
                    )
                )

        # 3) 资源过载 / 4) 依赖阻塞 / 5) 交付风险：分类引擎风险清单（识别规则仍在引擎）
        for risk in plan.get("riskList") or []:
            if not isinstance(risk, dict):
                continue
            description = str(risk.get("description") or "")
            risk_id = str(risk.get("riskId") or description[:24])
            marker = next((name for text, name in RISK_MARKERS.items() if text in description), None)
            if marker == "overload":
                signal, severity = PATROL_SIGNAL_RESOURCE_OVERLOAD, PATROL_SEVERITY_WARNING
            elif marker in ("cycle", "isolated"):
                signal, severity = PATROL_SIGNAL_DEPENDENCY_BLOCKED, PATROL_SEVERITY_WARNING
            elif marker == "critical_delay":
                signal, severity = PATROL_SIGNAL_DEADLINE_RISK, PATROL_SEVERITY_CRITICAL
            else:
                signal, severity = PATROL_SIGNAL_DEADLINE_RISK, PATROL_SEVERITY_INFO
            if str(risk.get("level")).upper() == "HIGH" and severity == PATROL_SEVERITY_WARNING:
                severity = PATROL_SEVERITY_CRITICAL
            out.append((signal, severity, description, f"risk:{risk_id}", str(risk.get("suggest") or "")))

        # 4b) 阻塞任务（状态事实，比风险清单更直接）
        for name in stats.get("blockedTasks") or []:
            out.append(
                (
                    PATROL_SIGNAL_DEPENDENCY_BLOCKED,
                    PATROL_SEVERITY_WARNING,
                    f"任务阻塞：{name}",
                    f"blocked:{name}",
                    "请确认前置依赖是否已就绪",
                )
            )

        # 5b) 交付风险：引擎推算完成日期晚于截止日期
        offset = stats.get("deadlineOffsetDays")
        if isinstance(offset, int) and offset > 0:
            out.append(
                (
                    PATROL_SIGNAL_DEADLINE_RISK,
                    PATROL_SEVERITY_CRITICAL,
                    f"预计交付晚于截止 {offset} 天",
                    f"deadline+{offset}",
                    f"引擎推算完成日期 {stats.get('plannedEndDate')}，截止 {plan.get('iterationDeadline')}",
                )
            )
        return out

    def _should_alert(self, plan_id: str, signal: str, key: str) -> bool:
        """冷却判定：同一 ``(signal, key)`` 在冷却窗口内只推送一次。"""
        if self._cooldown <= 0:
            return True
        last = self._last_alert.setdefault(plan_id, {}).get((signal, key))
        return last is None or (time.time() - last) >= self._cooldown

    # ------------------------------- 发现记录 -------------------------------

    def _record(
        self,
        plan: dict[str, Any],
        signal: str,
        severity: str,
        title: str,
        key: str,
        detail: str,
        status: str,
    ) -> dict[str, Any]:
        plan_id = str(plan.get("planId") or "")
        now = time.time()
        if status != PATROL_FINDING_RESOLVED:
            self._last_alert.setdefault(plan_id, {})[(signal, key)] = now
        finding = {
            "findingId": f"ptf_{uuid.uuid4().hex[:12]}",
            "planId": plan_id,
            "planRequirement": str(plan.get("rawRequirement") or "")[:120],
            "signal": signal,
            "signalLabel": PATROL_SIGNAL_LABELS.get(signal, signal),
            "severity": severity,
            "title": title,
            "detail": detail,
            "status": status,
            "escalated": PATROL_SEVERITY_RANK.get(severity, 0) >= PATROL_SEVERITY_RANK.get(self._escalate, 2),
            "at": now,
        }
        slot = (plan_id, signal, key)
        existing = self._findings.get(slot)
        if status == PATROL_FINDING_RESOLVED and existing is None:
            # 恢复通知只对「前端确实展示过」的问题有意义：从未记录过的问题无需展示「已恢复」，
            # 但仍作为一次性事件返回并推送（保持与 WS 通知一致）。
            return finding
        if existing is not None:
            # 同一信号重复检出 / 转恢复：**就地更新同一行**，保留 findingId 让前端列表稳定，
            # 只刷新状态、严重度、描述与时间（避免同一问题在列表里堆成多行）
            existing.update(
                {
                    "severity": severity,
                    "title": title,
                    "detail": detail,
                    "status": status,
                    "escalated": finding["escalated"],
                    "at": now,
                    "planRequirement": finding["planRequirement"],
                }
            )
            return existing
        self._findings[slot] = finding
        self._evict_oldest()
        return finding

    def _evict_oldest(self) -> None:
        """超出上限时按时间淘汰最旧发现（保持「当前问题列表」有界）。"""
        overflow = len(self._findings) - self._max_findings
        if overflow <= 0:
            return
        oldest = sorted(self._findings.items(), key=lambda item: float(item[1].get("at") or 0))[:overflow]
        for slot, _finding in oldest:
            self._findings.pop(slot, None)

    def _apply_retention(self) -> None:
        """按 TTL 清理过期发现（避免长期运行后列表无限增长）。"""
        if self._finding_ttl <= 0:
            return
        threshold = time.time() - self._finding_ttl
        for slot in [slot for slot, item in self._findings.items() if float(item.get("at") or 0) < threshold]:
            self._findings.pop(slot, None)
        # 冷却表同样按 TTL 清理，避免 planId 维度无限增长
        for plan_id in list(self._last_alert):
            entries = {key: at for key, at in self._last_alert[plan_id].items() if at >= threshold}
            if entries:
                self._last_alert[plan_id] = entries
            else:
                self._last_alert.pop(plan_id, None)

    async def _publish(self, finding: dict[str, Any]) -> None:
        """推送巡检发现（携带 planId，使受限连接也能按可见范围正确过滤）。"""
        try:
            await self._broadcaster.broadcast({"type": WS_EVENT_PATROL_FINDING, "at": finding["at"], **finding})
            METRICS.inc_counter("patrol_alerts_total", {"outcome": "published"})
        except Exception:  # noqa: BLE001 - 广播失败不影响巡检本身
            METRICS.inc_counter("patrol_alerts_total", {"outcome": "failed"})
            logger.exception("巡检告警推送失败（findingId=%s）", finding.get("findingId"))

    # ------------------------------- 只读查询 -------------------------------

    def findings(self, plan_ids: frozenset[str] | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
        """当前留存发现（每个 ``(planId, signal, key)`` 一行；按严重度、时间倒序）。

        ``plan_ids`` 用于按可见范围过滤（越权计划不下发）。
        """
        rows = list(self._findings.values())
        if plan_ids is not None:
            rows = [item for item in rows if str(item.get("planId")) in plan_ids]
        ordered = sorted(
            rows,
            key=lambda item: (PATROL_SEVERITY_RANK.get(str(item.get("severity")), 0), float(item.get("at") or 0)),
            reverse=True,
        )
        return ordered[: max(1, limit)]

    def status(self) -> dict[str, Any]:
        """巡检运行状态（供前端展示「巡检是否在工作」）。"""
        return {
            "running": self.running,
            "lastRunAt": self._last_run_at,
            "lastSummary": self._last_summary,
            "retainedFindings": len(self._findings),
            "trackedPlans": len(self._snapshot),
            "signals": list(PATROL_SIGNALS),
        }


__all__ = [
    "PatrolService",
    "PROGRESS_DRIFT_WARN_DAYS",
    "UNASSIGNED_CRITICAL_WARN",
    "PATROL_SIGNAL_LABELS",
]
