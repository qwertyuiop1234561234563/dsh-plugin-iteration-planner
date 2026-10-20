"""持久化检查点 + 断点续跑测试。

分三层：

1. **台账存储**：``CheckpointStore`` 的读写、主体 / 会话索引与裁剪、脏数据兜底；
2. **续跑语义**：``CheckpointResumer`` 的确认（部分成功可重试）、幂等重放、有界重跑、作废；
3. **HTTP 链路**：登记 → 列表 → 快照 → 确认落库 → 重复确认不二次写库，以及属主隔离。

全部用例无外部依赖（桩调度器 + 桩引擎 + 内存存储），且不写入真实持久化数据。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent.resume import CheckpointResumer
from app.checkpoints import CheckpointStore, build_checkpoint_store
from app.constants import (
    CHECKPOINT_KIND_CHAT,
    CHECKPOINT_KIND_PLAN,
    CHECKPOINT_STATUS_ABORTED,
    CHECKPOINT_STATUS_AWAITING,
    CHECKPOINT_STATUS_COMPLETED,
    CHECKPOINT_STATUS_EXECUTED,
    CHECKPOINT_STATUS_FAILED,
    CHECKPOINT_STATUS_REJECTED,
    CHECKPOINT_STATUS_RUNNING,
)
from app.errors import ApiError
from app.kvstore import InMemoryKV
from app.models import (
    AgentCheckpoint,
    AgentRunResult,
    CheckpointRequest,
    PendingAction,
    ReflectionEvent,
    ToolCallRecord,
)
from app.sessions import InMemorySessionStore

from tests.conftest import ScriptedLlm, auth, login, tool_call


# ------------------------------- 夹具 -------------------------------


def _kv() -> InMemoryKV:
    """独立的内存存储：每个用例一份，绝不共享（否则用例之间互相污染）。"""
    return InMemoryKV(store="checkpoint", max_entries=64)


def _store(kv: InMemoryKV | None = None) -> CheckpointStore:
    return CheckpointStore(kv or _kv(), ttl_seconds=300.0)


def _action(action_id: str, tool: str = "task_add", plan_id: str | None = "p1") -> PendingAction:
    return PendingAction(
        actionId=action_id,
        tool=tool,
        label=f"新增任务（{action_id}）",
        arguments={"planId": plan_id or "p1", "taskName": "补写接口用例", "estimatedHours": 4},
        planId=plan_id,
    )


def _record(tool: str = "task_add", *, ok: bool = True, plan_id: str = "p1") -> ToolCallRecord:
    return ToolCallRecord(
        name=tool,
        arguments={},
        ok=ok,
        error=None if ok else "引擎拒绝",
        result={"planId": plan_id} if ok else None,
    )


async def _awaiting(
    store: CheckpointStore,
    *,
    owner: str = "u-1",
    session: str = "s-1",
    actions: list[PendingAction] | None = None,
    kind: str = CHECKPOINT_KIND_CHAT,
) -> AgentCheckpoint:
    return await store.create(
        session_id=session,
        owner=owner,
        kind=kind,
        request=CheckpointRequest(kind=kind, message="补一个任务", planId="p1"),
        status=CHECKPOINT_STATUS_AWAITING,
        pending_actions=[item.model_dump() for item in (actions if actions is not None else [_action("a1")])],
        content="准备新增任务。",
    )


class _StubRunner:
    """续跑用桩编排器：只暴露 ``CheckpointResumer`` 真正会调用的三个入口。"""

    def __init__(
        self,
        *,
        records: list[ToolCallRecord] | None = None,
        plan_id: str | None = "p1",
        result: AgentRunResult | None = None,
        error: ApiError | None = None,
    ) -> None:
        self.records = records if records is not None else [_record()]
        self.plan_id = plan_id
        self.result = result
        self.error = error
        self.execute_calls: list[tuple[str, list[str]]] = []
        self.run_calls: list[tuple[str, tuple, dict]] = []

    async def execute_actions(self, session_id: str, actions: list[PendingAction]):
        self.execute_calls.append((session_id, [item.actionId for item in actions]))
        return self.records, self.plan_id

    async def run_plan(self, message: str, **kwargs):
        self.run_calls.append(("plan", (message,), kwargs))
        if self.error is not None:
            raise self.error
        return self.result or AgentRunResult(content="重跑完成（plan）", planId="p1")

    async def run_chat(self, session_id: str, message: str, **kwargs):
        self.run_calls.append(("chat", (session_id, message), kwargs))
        if self.error is not None:
            raise self.error
        return self.result or AgentRunResult(content="重跑完成（chat）", planId="p1")


def _resumer(
    store: CheckpointStore,
    runner: _StubRunner,
    sessions: InMemorySessionStore | None = None,
    *,
    max_attempts: int = 3,
) -> tuple[CheckpointResumer, InMemorySessionStore]:
    resolved = sessions or InMemorySessionStore()
    return (
        CheckpointResumer(store=store, runner=runner, sessions=resolved, max_attempts=max_attempts),
        resolved,
    )


# ------------------------------- 台账存储 -------------------------------


def test_store_roundtrip_and_indexes() -> None:
    async def _run() -> None:
        store = _store()
        first = await _awaiting(store, session="s-1")
        second = await _awaiting(store, session="s-2")

        # 读回必须与写入一致（含 PendingAction 结构）
        loaded = await store.get(first.checkpointId)
        assert loaded is not None
        assert loaded.status == CHECKPOINT_STATUS_AWAITING
        assert loaded.pendingActions[0].tool == "task_add"
        assert loaded.resumable is True

        # 列表按「最近优先」，且只返回本主体的台账
        mine = await store.list("u-1")
        assert [item.checkpointId for item in mine] == [second.checkpointId, first.checkpointId]
        assert await store.list("u-other") == []

        # 会话索引只关心最近一次（用于「按会话续跑」）
        latest = await store.latest_for_session("s-1")
        assert latest is not None and latest.checkpointId == first.checkpointId
        assert await store.latest_for_session("s-missing") is None

        # 待确认计数跨台账累计（UI 提示「有未落库的改动」）
        assert await store.pending_count("s-1") == 1

        await store.delete(first.checkpointId)
        assert await store.get(first.checkpointId) is None

    asyncio.run(_run())


def test_store_list_falls_back_to_global_index_when_owner_index_lost() -> None:
    """主体索引被 LRU 淘汰时，宁可多读几条也不能让用户「查不到自己刚登记的台账」。"""

    async def _run() -> None:
        kv = _kv()
        store = _store(kv)
        checkpoint = await _awaiting(store, owner="u-1")

        await kv.delete("index:owner:u-1")  # 模拟索引键过期 / 被淘汰

        assert [item.checkpointId for item in await store.list("u-1")] == [checkpoint.checkpointId]
        # 但过滤仍然严格：全局索引里没有他人台账可借
        assert await store.list("u-other") == []

    asyncio.run(_run())


def test_store_treats_corrupt_payload_as_missing() -> None:
    """版本不符的脏数据按「不存在」处理，由调用方重建，而不是让整条链路炸掉。"""

    async def _run() -> None:
        kv = _kv()
        store = _store(kv)
        await kv.set("cp:deadbeef", {"unexpected": "shape"}, 60)
        assert await store.get("deadbeef") is None

    asyncio.run(_run())


def test_store_records_plan_checkpoint_without_session_index() -> None:
    """端到端规划没有会话：不得建出 ``index:session:`` 空键（否则索引被无意义地撑大）。"""

    async def _run() -> None:
        kv = _kv()
        store = _store(kv)
        checkpoint = await store.create(
            session_id="",
            owner="u-1",
            kind=CHECKPOINT_KIND_PLAN,
            request=CheckpointRequest(kind=CHECKPOINT_KIND_PLAN, message="订单中心 v2.1"),
            status=CHECKPOINT_STATUS_FAILED,
            error="ENGINE_UNAVAILABLE：算法引擎暂不可用",
        )
        assert await kv.get("index:session:") is None
        loaded = await store.get(checkpoint.checkpointId)
        assert loaded is not None and loaded.resumable is True and loaded.sessionId == ""

    asyncio.run(_run())


def test_build_checkpoint_store_rejects_redis_without_url() -> None:
    """显式选 Redis 但缺 URL 时启动即失败：断点续跑的价值全建立在跨副本共享台账之上。"""
    with pytest.raises(RuntimeError):
        build_checkpoint_store(driver="redis", redis_url="", ttl_seconds=60)


def test_build_checkpoint_store_defaults_to_memory() -> None:
    store = build_checkpoint_store(driver="auto", redis_url="", ttl_seconds=60)
    assert store.backend == "memory"
    assert "statuses" in store.describe()


# ------------------------------- 续跑：确认落库 -------------------------------


def test_confirm_executes_actions_and_flags_terminal() -> None:
    async def _run() -> None:
        store = _store()
        checkpoint = await _awaiting(store, actions=[_action("a1"), _action("a2")])
        sessions = InMemorySessionStore()
        await sessions.set_pending(checkpoint.sessionId, [item.model_dump() for item in checkpoint.pendingActions])

        runner = _StubRunner(records=[_record("task_add"), _record("task_add")])
        resumer, sessions = _resumer(store, runner, sessions)
        result = await resumer.resume(checkpoint, action="confirm")

        assert runner.execute_calls == [(checkpoint.sessionId, ["a1", "a2"])]
        assert len(result.executed) == 2
        assert result.remainingActions == []
        assert result.replayed is False

        reloaded = await store.get(checkpoint.checkpointId)
        assert reloaded is not None
        assert reloaded.status == CHECKPOINT_STATUS_EXECUTED
        assert reloaded.resumable is False
        assert reloaded.planId == "p1"

        # 关键约束：会话槽位必须同步清空，否则会话路径的「确认」会把同一批动作再执行一遍
        assert await sessions.get_pending(checkpoint.sessionId) is None

    asyncio.run(_run())


def test_confirm_is_idempotent_when_already_terminal() -> None:
    """重复确认（双击 / 网络重试 / 多副本并发）必须返回上次快照，绝不二次写库。"""

    async def _run() -> None:
        store = _store()
        checkpoint = await _awaiting(store)
        runner = _StubRunner()
        resumer, _ = _resumer(store, runner)

        first = await resumer.resume(checkpoint, action="confirm")
        second = await resumer.resume(await store.get(checkpoint.checkpointId), action="confirm")

        assert len(runner.execute_calls) == 1
        assert first.replayed is False
        assert second.replayed is True
        assert second.executed == first.executed
        assert second.remainingActions == []

    asyncio.run(_run())


def test_confirm_partial_failure_keeps_action_for_reapproval() -> None:
    """一条失败不该让用户重新编排：失败动作留在台账里，可直接再次确认。"""

    async def _run() -> None:
        store = _store()
        checkpoint = await _awaiting(store, actions=[_action("a1"), _action("a2")])
        runner = _StubRunner(records=[_record("task_add", ok=True), _record("task_add", ok=False)])
        resumer, sessions = _resumer(store, runner)

        result = await resumer.resume(checkpoint, action="confirm")

        assert [item.ok for item in result.executed] == [True, False]
        assert [item.actionId for item in result.remainingActions] == ["a2"]
        reloaded = await store.get(checkpoint.checkpointId)
        assert reloaded is not None and reloaded.status == CHECKPOINT_STATUS_AWAITING
        assert reloaded.resumable is True
        # 会话槽位收敛为「仍未执行的动作」，而非全部清空
        assert [item["actionId"] for item in await sessions.get_pending(checkpoint.sessionId)] == ["a2"]

    asyncio.run(_run())


def test_confirm_subset_and_reject_semantics() -> None:
    async def _run() -> None:
        store = _store()
        checkpoint = await _awaiting(store, actions=[_action("a1"), _action("a2"), _action("a3")])
        runner = _StubRunner(records=[_record("task_add")])
        resumer, _ = _resumer(store, runner)

        partial = await resumer.resume(checkpoint, action="confirm", action_ids=["a1"])
        assert runner.execute_calls == [(checkpoint.sessionId, ["a1"])]
        assert [item.actionId for item in partial.remainingActions] == ["a2", "a3"]

        # 未知 actionId 必须显式报错，而不是「静默确认了其余动作」
        with pytest.raises(ApiError) as excinfo:
            await resumer.resume(await store.get(checkpoint.checkpointId), action="confirm", action_ids=["nope"])
        assert excinfo.value.code == "VALIDATION_ERROR"

        # 驳回：丢弃待确认动作、不执行任何写入
        rejected = await resumer.resume(await store.get(checkpoint.checkpointId), action="confirm", reject=True)
        assert runner.execute_calls == [(checkpoint.sessionId, ["a1"])]
        assert rejected.executed == []
        assert rejected.remainingActions == []
        assert rejected.checkpoint.status == CHECKPOINT_STATUS_REJECTED

    asyncio.run(_run())


def test_confirm_reject_of_subset_keeps_other_actions() -> None:
    """驳回选中的动作：其余动作仍留在台账里等确认，而不是被一并作废。"""

    async def _run() -> None:
        store = _store()
        checkpoint = await _awaiting(store, actions=[_action("a1"), _action("a2")])
        runner = _StubRunner()
        resumer, _ = _resumer(store, runner)

        result = await resumer.resume(checkpoint, action="confirm", reject=True, action_ids=["a1"])

        assert runner.execute_calls == []
        assert result.checkpoint.status == CHECKPOINT_STATUS_AWAITING
        assert [item.actionId for item in result.remainingActions] == ["a2"]

        # 显式作废同样不得把「只作废 a2」静默升级为「作废全部」
        refused = await resumer.resume(
            await store.get(checkpoint.checkpointId), action="abort", reject=True, action_ids=["a2"]
        )
        assert refused.checkpoint.status == CHECKPOINT_STATUS_REJECTED
        assert refused.remainingActions == []

    asyncio.run(_run())


def test_confirm_on_non_awaiting_checkpoint_is_rejected() -> None:
    """运行中 / 失败的台账不能「确认」：它根本没有待确认动作可执行。"""

    async def _run() -> None:
        store = _store()
        checkpoint = await _awaiting(store)
        checkpoint.status = CHECKPOINT_STATUS_RUNNING
        await store.save(checkpoint)
        resumer, _ = _resumer(store, _StubRunner())

        with pytest.raises(ApiError) as excinfo:
            await resumer.resume(checkpoint, action="confirm")
        assert excinfo.value.code == "CHECKPOINT_NOT_RESUMABLE"

    asyncio.run(_run())


# ------------------------------- 续跑：按原请求重跑 -------------------------------


def test_retry_reruns_plan_from_stored_request() -> None:
    """重跑用台账里保存的**原始请求**：用户不需要重新描述一遍需求。"""

    async def _run() -> None:
        store = _store()
        checkpoint = await store.create(
            session_id="",
            owner="u-1",
            kind=CHECKPOINT_KIND_PLAN,
            request=CheckpointRequest(
                kind=CHECKPOINT_KIND_PLAN,
                message="订单中心 v2.1",
                iterationDeadline="2026-10-30",
                devMemberCount=3,
            ),
            status=CHECKPOINT_STATUS_FAILED,
            error="ENGINE_UNAVAILABLE：算法引擎暂不可用",
        )
        runner = _StubRunner(
            result=AgentRunResult(
                content="规划完成",
                planId="p9",
                toolCalls=[_record("plan_iteration", plan_id="p9")],
                steps=2,
                reflections=[ReflectionEvent(index=1, step=1, category="engine_unavailable", strategy="retry_with_args")],
            )
        )
        resumer, _ = _resumer(store, runner)
        result = await resumer.resume(checkpoint, action="retry")

        assert result.replayed is False
        assert runner.run_calls[0][0] == "plan"
        assert runner.run_calls[0][1] == ("订单中心 v2.1",)
        kwargs = runner.run_calls[0][2]
        assert kwargs["dev_member_count"] == 3
        assert kwargs["iteration_deadline"] == "2026-10-30"
        # 台账由 resumer 统一持有：编排不得另建台账，否则「重跑一次多出一条」
        assert kwargs["record"] is False

        reloaded = await store.get(checkpoint.checkpointId)
        assert reloaded is not None
        assert reloaded.attempt == 2
        assert reloaded.status == CHECKPOINT_STATUS_COMPLETED
        assert reloaded.planId == "p9"
        assert [event.category for event in reloaded.reflections] == ["engine_unavailable"]

    asyncio.run(_run())


def test_retry_keeps_awaiting_when_new_pending_actions_appear() -> None:
    async def _run() -> None:
        store = _store()
        checkpoint = await _awaiting(store)
        checkpoint.status = CHECKPOINT_STATUS_FAILED
        checkpoint.pendingActions = []
        await store.save(checkpoint)

        runner = _StubRunner(
            result=AgentRunResult(
                content="准备新增任务。",
                pendingActions=[_action("b1")],
                planId="p1",
            )
        )
        resumer, sessions = _resumer(store, runner)
        result = await resumer.resume(checkpoint, action="retry")

        assert result.checkpoint.status == CHECKPOINT_STATUS_AWAITING
        assert [item.actionId for item in result.remainingActions] == ["b1"]
        assert [item["actionId"] for item in await sessions.get_pending(checkpoint.sessionId)] == ["b1"]

    asyncio.run(_run())


def test_retry_is_bounded_and_marks_failure() -> None:
    async def _run() -> None:
        store = _store()
        checkpoint = await _awaiting(store)
        checkpoint.status = CHECKPOINT_STATUS_FAILED
        checkpoint.pendingActions = []
        await store.save(checkpoint)
        resumer, _ = _resumer(store, _StubRunner(error=ApiError(503, "ENGINE_UNAVAILABLE", "算法引擎暂不可用")), max_attempts=2)

        with pytest.raises(ApiError) as excinfo:
            await resumer.resume(checkpoint, action="retry")
        assert excinfo.value.code == "ENGINE_UNAVAILABLE"
        failed = await store.get(checkpoint.checkpointId)
        assert failed is not None and failed.status == CHECKPOINT_STATUS_FAILED
        assert failed.error and failed.error.startswith("ENGINE_UNAVAILABLE")

        # 已达上限：再次重跑必须拒绝，避免把一次故障放大成持续故障
        with pytest.raises(ApiError) as exhausted:
            await resumer.resume(failed, action="retry")
        assert exhausted.value.code == "CHECKPOINT_NOT_RESUMABLE"

    asyncio.run(_run())


def test_retry_on_awaiting_checkpoint_requires_explicit_reject() -> None:
    """有待批复改动时重跑会**静默丢弃**用户还没批的改动，因此必须显式表达。"""

    async def _run() -> None:
        store = _store()
        checkpoint = await _awaiting(store)
        resumer, _ = _resumer(store, _StubRunner())

        with pytest.raises(ApiError) as excinfo:
            await resumer.resume(checkpoint, action="retry")
        assert excinfo.value.code == "CHECKPOINT_NOT_RESUMABLE"
        assert "reject" in excinfo.value.message

    asyncio.run(_run())


def test_abort_discards_pending_without_executing() -> None:
    async def _run() -> None:
        store = _store()
        checkpoint = await _awaiting(store)
        runner = _StubRunner()
        resumer, sessions = _resumer(store, runner)

        result = await resumer.resume(checkpoint, action="abort")

        assert runner.execute_calls == []
        assert result.executed == []
        assert result.remainingActions == []
        assert result.checkpoint.status == CHECKPOINT_STATUS_ABORTED
        assert await sessions.get_pending(checkpoint.sessionId) is None

        # 已终态再作废：幂等返回，不重复计数
        again = await resumer.resume(await store.get(checkpoint.checkpointId), action="abort")
        assert again.replayed is True

    asyncio.run(_run())


def test_auto_action_routes_by_status() -> None:
    async def _run() -> None:
        store = _store()
        awaiting = await _awaiting(store)
        runner = _StubRunner()
        resumer, _ = _resumer(store, runner)

        confirmed = await resumer.resume(awaiting, action="auto")
        assert confirmed.checkpoint.status == CHECKPOINT_STATUS_EXECUTED

        failed = await _awaiting(store, session="s-2")
        failed.status = CHECKPOINT_STATUS_FAILED
        failed.pendingActions = []
        await store.save(failed)

        retried = await resumer.resume(failed, action="auto")
        assert retried.checkpoint.status == CHECKPOINT_STATUS_COMPLETED

    asyncio.run(_run())


# ------------------------------- HTTP 链路 -------------------------------


def _pending_chat_llm() -> ScriptedLlm:
    return ScriptedLlm(
        [
            {
                "role": "assistant",
                "content": "准备新增任务。",
                "tool_calls": [tool_call("task_add", {"planId": "p1", "taskName": "补写接口用例", "estimatedHours": 4}, "c1")],
            },
            {"role": "assistant", "content": "已登记待确认。"},
        ]
    )


def test_write_chat_registers_durable_checkpoint_and_resumes(make_client) -> None:
    """端到端：登记台账 → 列表 / 快照可见 → 确认落库 → 重复确认不二次写库。"""
    with make_client(llm=_pending_chat_llm()) as c:
        chat = c.post(
            "/api/agent/chat",
            json={"message": "给 p1 补一个任务", "deferWrites": True, "planId": "p1"},
            headers=auth(),
        )
        assert chat.status_code == 200, chat.text
        data = chat.json()["data"]
        checkpoint_id = data["checkpointId"]
        assert checkpoint_id and len(checkpoint_id) == 32
        assert data["pendingActions"][0]["tool"] == "task_add"

        listed = c.get("/api/agent/checkpoints", headers=auth()).json()["data"]
        assert listed["enabled"] is True and listed["backend"] == "memory"
        assert [item["checkpointId"] for item in listed["items"]] == [checkpoint_id]
        assert listed["items"][0]["pendingCount"] == 1
        assert listed["items"][0]["resumable"] is True

        snapshot = c.get(f"/api/agent/checkpoints/{checkpoint_id}", headers=auth()).json()["data"]
        assert snapshot["status"] == "awaiting_approval"
        assert snapshot["request"]["message"] == "给 p1 补一个任务"
        assert snapshot["request"]["planId"] == "p1"

        confirmed = c.post(
            f"/api/agent/checkpoints/{checkpoint_id}/resume", json={"action": "confirm"}, headers=auth()
        )
        assert confirmed.status_code == 200, confirmed.text
        payload = confirmed.json()["data"]
        assert payload["replayed"] is False
        assert payload["remainingActions"] == []
        assert payload["executed"][0]["ok"] is True
        assert payload["checkpoint"]["status"] == "executed"

        # 幂等：再来一次不会写出第二份
        replayed = c.post(
            f"/api/agent/checkpoints/{checkpoint_id}/resume", json={"action": "confirm"}, headers=auth()
        ).json()["data"]
        assert replayed["replayed"] is True

        after = c.get(f"/api/agent/checkpoints/{checkpoint_id}", headers=auth()).json()["data"]
        assert after["pendingActions"] == []
        assert len(after["executed"]) == 1


def test_readonly_chat_does_not_create_checkpoint(make_client) -> None:
    """只读问答不留台账：它既没有待确认动作，也没有需要重跑的半成品。"""
    llm = ScriptedLlm(
        [
            {"role": "assistant", "content": None, "tool_calls": [tool_call("get_plan", {"planId": "p1"}, "c1")]},
            {"role": "assistant", "content": "计划 p1 共 1 个任务。"},
        ]
    )
    with make_client(llm=llm) as c:
        data = c.post("/api/agent/chat", json={"message": "p1 有哪些任务"}, headers=auth()).json()["data"]
        assert data["checkpointId"] is None
        assert c.get("/api/agent/checkpoints", headers=auth()).json()["data"]["items"] == []


def test_checkpoint_unknown_and_malformed_ids_both_return_404(make_client) -> None:
    with make_client() as c:
        for path in ("/api/agent/checkpoints/" + "a" * 32, "/api/agent/checkpoints/not-an-id"):
            response = c.get(path, headers=auth())
            assert response.status_code == 404, response.text
            assert response.json()["code"] == "CHECKPOINT_NOT_FOUND"
        deleted = c.delete("/api/agent/checkpoints/not-an-id", headers=auth())
        assert deleted.status_code == 404


def test_checkpoints_are_isolated_by_owner(make_client) -> None:
    """非属主一律 404（而非 403）：否则可借状态码差异探测「别人是否有待确认的改动」。"""
    with make_client(llm=_pending_chat_llm()) as c:
        data = c.post("/api/agent/chat", json={"message": "给 p1 补一个任务"}, headers=auth()).json()["data"]
        checkpoint_id = data["checkpointId"]

        login(c)  # 切换为人类主体（引导管理员），Cookie 覆盖机器令牌

        assert c.get(f"/api/agent/checkpoints/{checkpoint_id}").status_code == 404
        assert c.get("/api/agent/checkpoints").json()["data"]["items"] == []
        assert c.post(f"/api/agent/checkpoints/{checkpoint_id}/resume", json={"action": "confirm"}).status_code == 404


def test_checkpoint_delete_discards_pending_without_executing(make_client) -> None:
    with make_client(llm=_pending_chat_llm()) as c:
        data = c.post("/api/agent/chat", json={"message": "给 p1 补一个任务"}, headers=auth()).json()["data"]
        checkpoint_id = data["checkpointId"]

        response = c.delete(f"/api/agent/checkpoints/{checkpoint_id}", headers=auth())
        assert response.status_code == 200, response.text
        assert response.json()["data"]["deleted"] is True

        # 已移除且会话槽位已清空：不会残留「幽灵待确认」
        assert c.get(f"/api/agent/checkpoints/{checkpoint_id}", headers=auth()).status_code == 404


def test_checkpoint_resume_rejects_unknown_action(make_client) -> None:
    """``action`` 白名单由模型层强制：非法动作进不了续跑分支，而是标准校验错误。"""
    with make_client(llm=_pending_chat_llm()) as c:
        data = c.post("/api/agent/chat", json={"message": "给 p1 补一个任务"}, headers=auth()).json()["data"]
        response = c.post(
            f"/api/agent/checkpoints/{data['checkpointId']}/resume",
            json={"action": "explode"},
            headers=auth(),
        )
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"
