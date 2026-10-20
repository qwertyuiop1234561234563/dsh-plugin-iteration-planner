"""断点续跑：在**持久化检查点**上继续一次被中断的编排。

三条续跑语义，对应「两阶段写确认」在不同中断点上的正确恢复方式：

============================  ==================================================
``confirm``                   台账处于 ``awaiting_approval``：把待确认动作落库。
                              落库失败的动作会**留在 ledger 里**，可再次确认（审批可驳回重试）。
``retry``                     台账处于 ``running`` / ``failed``（进程崩溃或引擎故障）：
                              用台账里保存的**原始请求**重跑，而不是要求用户重新描述需求。
``abort``                     主动作废：丢弃待确认动作，不执行任何写入。
============================  ==================================================

两条容易写错的约束，这里显式保证：

1. **确认必须幂等**：重复点「确认」（网络重试、双击、多副本并发）不得二次写库。已进入终态
   的台账再次确认，直接返回**上次的快照**并标记 ``replayed=True``，而不是重新执行。
2. **不得绕过会话账本**：从台账确认落库后，同一会话的待确认槽位必须同步收敛为「仍未执行的
   动作」。否则会话路径的 ``/agent/confirm`` 会把同一批动作**再执行一次**，造成重复写。

重跑路径复用 :meth:`AgentRunner.run_plan` / :meth:`AgentRunner.run_chat`，并传 ``record=False``
让编排不另建台账——台账由本模块统一持有与更新，避免「重跑一次多出一条台账」。
"""

from __future__ import annotations

import logging

from ..checkpoints import CheckpointStore
from ..constants import (
    CHECKPOINT_KIND_CHAT,
    CHECKPOINT_KIND_PLAN,
    CHECKPOINT_STATUS_ABORTED,
    CHECKPOINT_STATUS_AWAITING,
    CHECKPOINT_STATUS_COMPLETED,
    CHECKPOINT_STATUS_EXECUTED,
    CHECKPOINT_STATUS_FAILED,
    CHECKPOINT_STATUS_REJECTED,
    CHECKPOINT_STATUS_RUNNING,
    CHECKPOINT_TERMINAL_STATUSES,
)
from ..errors import (
    ApiError,
    bad_request,
    checkpoint_not_resumable,
)
from ..metrics import METRICS
from ..models import AgentCheckpoint, CheckpointActionResult, PendingAction, ToolCallRecord
from ..sessions import SessionStore
from .runner import AgentRunner

logger = logging.getLogger("iteration_planner.checkpoint.resume")


class CheckpointResumer:
    """在检查点台账上执行续跑动作。"""

    def __init__(
        self,
        *,
        store: CheckpointStore,
        runner: AgentRunner,
        sessions: SessionStore,
        max_attempts: int = 3,
    ) -> None:
        self._store = store
        self._runner = runner
        self._sessions = sessions
        self._max_attempts = max(1, max_attempts)

    @property
    def store(self) -> CheckpointStore:
        return self._store

    async def resume(
        self,
        checkpoint: AgentCheckpoint,
        *,
        action: str = "auto",
        action_ids: list[str] | None = None,
        reject: bool = False,
    ) -> CheckpointActionResult:
        """执行一次续跑动作（``auto`` 按当前状态自动分流）。"""
        if action == "abort":
            # 必须把 reject / action_ids 一并透传：否则「作废指定动作」会被静默升级成
            # 「作废全部待确认动作」，把调用方只想丢弃的那一条变成丢掉一整批
            return await self._abort(checkpoint, rejected=reject, action_ids=action_ids)

        resolved = action
        if resolved == "auto":
            resolved = "confirm" if checkpoint.status == CHECKPOINT_STATUS_AWAITING else "retry"
        if resolved == "confirm":
            return await self._confirm(checkpoint, action_ids=action_ids, reject=reject)
        if resolved == "retry":
            if checkpoint.status == CHECKPOINT_STATUS_AWAITING:
                # 重跑会产出**全新**的一批待确认动作，直接覆盖等于静默丢弃用户尚未批复的改动。
                # 要求调用方显式表达「先丢弃再重跑」，把破坏性动作变成一次有意识的选择。
                raise checkpoint_not_resumable("该台账仍有未批复的改动：请先确认 / 驳回顾改动，或传 reject=true 丢弃后重跑")
            return await self._retry(checkpoint)
        raise bad_request(f"未知的续跑动作：{action}")

    # ------------------------------- 确认落库 -------------------------------

    async def _confirm(
        self,
        checkpoint: AgentCheckpoint,
        *,
        action_ids: list[str] | None,
        reject: bool,
    ) -> CheckpointActionResult:
        if checkpoint.status in CHECKPOINT_TERMINAL_STATUSES:
            # 幂等：已终态的台账再次确认，返回上次快照而非重新执行
            METRICS.inc_counter("checkpoint_resume_total", {"action": "confirm", "outcome": "replayed"})
            return CheckpointActionResult(
                checkpoint=checkpoint,
                executed=checkpoint.executed,
                remainingActions=checkpoint.pendingActions,
                planId=checkpoint.planId,
                content=checkpoint.content,
                replayed=True,
            )
        if checkpoint.status != CHECKPOINT_STATUS_AWAITING:
            raise checkpoint_not_resumable(f"台账当前状态为 {checkpoint.status}，无法执行确认")

        if reject:
            return await self._abort(checkpoint, rejected=True, action_ids=action_ids)

        wanted = set(action_ids) if action_ids is not None else None
        if wanted is not None:
            unknown = sorted(wanted - {item.actionId for item in checkpoint.pendingActions})
            if unknown:
                raise bad_request(f"待确认动作不存在：{'、'.join(unknown[:5])}")
        selected = [item for item in checkpoint.pendingActions if wanted is None or item.actionId in wanted]
        unselected = [item for item in checkpoint.pendingActions if wanted is not None and item.actionId not in wanted]
        if not selected:
            raise bad_request("没有符合条件的待确认动作")

        records, plan_id = await self._runner.execute_actions(checkpoint.sessionId, selected)
        # 失败的动作留在台账里，供「驳回重试」：一次引擎抖动不该让用户重新编排一遍
        retryable = [
            item for item, record in zip(selected, records) if not record.ok
        ]
        remaining = unselected + retryable
        checkpoint.executed = [*checkpoint.executed, *records]
        checkpoint.pendingActions = remaining
        checkpoint.status = CHECKPOINT_STATUS_AWAITING if remaining else CHECKPOINT_STATUS_EXECUTED
        checkpoint.planId = plan_id or checkpoint.planId
        await self._store.save(checkpoint)
        # 同步会话账本：否则会话路径的「确认」会把同一批动作再执行一遍（重复写）
        await self._sync_session(checkpoint)
        METRICS.inc_counter("checkpoint_resume_total", {"action": "confirm", "outcome": "executed"})
        return CheckpointActionResult(
            checkpoint=checkpoint,
            executed=records,
            remainingActions=remaining,
            planId=checkpoint.planId,
            content=checkpoint.content,
        )

    # ------------------------------- 按原请求重跑 -------------------------------

    async def _retry(self, checkpoint: AgentCheckpoint) -> CheckpointActionResult:
        if checkpoint.attempt >= self._max_attempts:
            # 有界重跑：无限重跑只会把一次故障放大成持续故障（每次都真打引擎 / 模型）
            METRICS.inc_counter("checkpoint_resume_total", {"action": "retry", "outcome": "exhausted"})
            raise checkpoint_not_resumable(f"续跑次数已达上限（{self._max_attempts} 次），请重新发起请求")

        checkpoint.attempt += 1
        checkpoint.status = CHECKPOINT_STATUS_RUNNING
        checkpoint.error = None
        await self._store.save(checkpoint)

        request = checkpoint.request
        try:
            if request.kind == CHECKPOINT_KIND_PLAN:
                result = await self._runner.run_plan(
                    request.message,
                    iteration_deadline=request.iterationDeadline,
                    iteration_start_date=request.iterationStartDate,
                    dev_member_count=request.devMemberCount,
                    owner=checkpoint.owner,
                    record=False,
                )
            else:
                result = await self._runner.run_chat(
                    checkpoint.sessionId,
                    request.message,
                    defer_writes=request.deferWrites,
                    plan_id=request.planId,
                    owner=checkpoint.owner,
                    record=False,
                )
        except ApiError as exc:
            checkpoint.status = CHECKPOINT_STATUS_FAILED
            checkpoint.error = f"{exc.code}：{exc.message}"[:500]
            await self._store.save(checkpoint)
            METRICS.inc_counter("checkpoint_resume_total", {"action": "retry", "outcome": "failed"})
            raise

        checkpoint.content = result.content
        checkpoint.planId = result.planId or checkpoint.planId
        checkpoint.steps = result.steps
        checkpoint.toolCalls = result.toolCalls
        checkpoint.reflections = result.reflections
        checkpoint.executed = [record for record in result.toolCalls if record.ok]
        checkpoint.pendingActions = result.pendingActions
        checkpoint.status = CHECKPOINT_STATUS_AWAITING if result.pendingActions else CHECKPOINT_STATUS_COMPLETED
        await self._store.save(checkpoint)
        await self._sync_session(checkpoint)
        METRICS.inc_counter("checkpoint_resume_total", {"action": "retry", "outcome": "ok"})
        return CheckpointActionResult(
            checkpoint=checkpoint,
            executed=list(checkpoint.executed),
            remainingActions=list(checkpoint.pendingActions),
            planId=checkpoint.planId,
            content=checkpoint.content,
        )

    # ------------------------------- 作废 ----------------------------------

    async def _abort(
        self,
        checkpoint: AgentCheckpoint,
        *,
        rejected: bool = False,
        action_ids: list[str] | None = None,
    ) -> CheckpointActionResult:
        """作废台账：丢弃待确认动作（``rejected=True`` 表示这是用户主动驳回）。"""
        if checkpoint.status in CHECKPOINT_TERMINAL_STATUSES:
            METRICS.inc_counter("checkpoint_resume_total", {"action": "abort", "outcome": "replayed"})
            return CheckpointActionResult(
                checkpoint=checkpoint,
                executed=checkpoint.executed,
                remainingActions=[],
                planId=checkpoint.planId,
                content=checkpoint.content,
                replayed=True,
            )

        if rejected and action_ids:
            # 只驳回部分动作：其余动作继续留在台账中等待确认
            wanted = set(action_ids)
            unknown = sorted(wanted - {item.actionId for item in checkpoint.pendingActions})
            if unknown:
                raise bad_request(f"待确认动作不存在：{'、'.join(unknown[:5])}")
            checkpoint.pendingActions = [
                item for item in checkpoint.pendingActions if item.actionId not in wanted
            ]
            records: list[ToolCallRecord] = []
            checkpoint.status = (
                CHECKPOINT_STATUS_AWAITING if checkpoint.pendingActions else CHECKPOINT_STATUS_REJECTED
            )
        else:
            checkpoint.pendingActions = []
            checkpoint.status = CHECKPOINT_STATUS_REJECTED if rejected else CHECKPOINT_STATUS_ABORTED
            records = []
        await self._store.save(checkpoint)
        await self._sync_session(checkpoint)
        METRICS.inc_counter(
            "checkpoint_resume_total",
            {"action": "abort", "outcome": "rejected" if rejected else "aborted"},
        )
        return CheckpointActionResult(
            checkpoint=checkpoint,
            executed=records,
            remainingActions=list(checkpoint.pendingActions),
            planId=checkpoint.planId,
            content=checkpoint.content,
        )

    async def _sync_session(self, checkpoint: AgentCheckpoint) -> None:
        """把台账的最新「未执行动作」同步回会话槽位（见模块文档第 2 条约束）。"""
        if not checkpoint.sessionId:
            return
        await self._sessions.set_pending(
            checkpoint.sessionId, [item.model_dump() for item in checkpoint.pendingActions]
        )


__all__ = ["CheckpointResumer"]
