"""Agent 观测与治理四能力的测试：执行轨迹 / 事故→回归用例 / 输出溯源 / 上下文预算。

四者共用一条主张：**Agent 的行为必须可观测、可复算、可回归**。

- 执行轨迹把「它当时做了什么」落成 span 时序，事故复盘不再靠翻散落的日志；
- 事故轨迹按**确定性规则**推导回归用例（不靠人事后写断言），一经登记即自动进门禁；
- 输出溯源把结论里的数字绑回证据，无据的**诚实标「未核实」**而不是假装有依据；
- 上下文预算按分区分配并回传明细，「为什么被裁」必须可解释。

全部用例无外部依赖（内存存储 + 桩 LLM + 桩引擎），且不写入真实持久化数据。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.agent.budget import SUMMARY_PREFIX, allocate_context, classify_zones, estimate_chars
from app.agent.provenance import analyze_provenance, collect_evidence, extract_numbers
from app.constants import (
    BUDGET_ZONE_CURRENT,
    BUDGET_ZONE_HISTORY,
    BUDGET_ZONE_KEEP,
    BUDGET_ZONE_RECALL,
    BUDGET_ZONE_SYSTEM,
    CUSTOM_CASE_ORIGIN_INCIDENT,
    CUSTOM_CASE_ORIGIN_MANUAL,
    MAX_CUSTOM_CASES,
    PROVENANCE_SOURCE_KB,
    PROVENANCE_SOURCE_MEMORY,
    PROVENANCE_SOURCE_TOOL,
    PROVENANCE_STATUS_UNVERIFIED,
    PROVENANCE_STATUS_VERIFIED,
    TRACE_KIND_GUARD,
    TRACE_KIND_REFLECT,
    TRACE_KIND_TOOL,
    TRACE_RUN_ERROR,
    TRACE_RUN_OK,
    TRACE_STATUS_DEGRADED,
    TRACE_STATUS_ERROR,
    TRACE_STATUS_OK,
)
from app.errors import ApiError
from app.evals.custom import CustomCaseStore, build_custom_case_store
from app.evals.golden import GOLDEN_CASES, cases_for
from app.evals.incidents import derive_case, replay_payload
from app.evals.service import EvalService
from app.kvstore import InMemoryKV
from app.models import (
    AgentRunResult,
    AgentTrace,
    CheckpointRequest,
    EvalCaseSpec,
    EvalRunRequest,
    MemoryItem,
    ToolCallRecord,
    TraceSpan,
)
from app.traces import TraceRecorder, TraceStore, build_trace_store, compact_payload, summarize_trace

from tests.conftest import ScriptedLlm, auth, login, tool_call


# ------------------------------- 夹具 -------------------------------


def _trace_store() -> TraceStore:
    return TraceStore(InMemoryKV(store="trace", max_entries=64), ttl_seconds=300.0)


def _case_store(*, max_cases: int = MAX_CUSTOM_CASES) -> CustomCaseStore:
    return CustomCaseStore(
        InMemoryKV(store="evals-custom-case", max_entries=64), ttl_seconds=300.0, max_cases=max_cases
    )


def _request(*, kind: str = "chat", message: str = "给 p1 补一个任务", plan_id: str | None = "p1") -> CheckpointRequest:
    return CheckpointRequest(kind=kind, message=message, planId=plan_id)


def _failed_trace(*, trace_id: str = "a" * 32, owner: str = "u-1", kind: str = "chat") -> AgentTrace:
    """一条含事故信号的轨迹：工具失败 + 护栏拦截 + 反思已归因。"""
    return AgentTrace(
        traceId=trace_id,
        owner=owner,
        kind=kind,
        status=TRACE_RUN_ERROR,
        request=_request(kind=kind),
        spans=[
            TraceSpan(
                index=1,
                kind=TRACE_KIND_TOOL,
                title="task_add",
                status=TRACE_STATUS_ERROR,
                code="NOT_FOUND",
                input={"tool": "task_add"},
            ),
            TraceSpan(index=2, kind=TRACE_KIND_GUARD, title="谎报完成拦截", status=TRACE_STATUS_OK),
            TraceSpan(
                index=3,
                kind=TRACE_KIND_REFLECT,
                title="自反思",
                status=TRACE_STATUS_DEGRADED,
                output={"recovered": False},
            ),
        ],
        steps=2,
        failedTools=["task_add"],
        guardCount=1,
        reflectionCount=1,
        hasIncident=True,
    )


# ================= 能力一：执行轨迹 =================


def test_compact_payload_keeps_shape_but_folds_volume() -> None:
    """轨迹存的是**摘要**而非数据副本：结构可定位，体积必须收敛。"""
    payload = {"planId": "p1", "requirement": "长" * 800, "taskList": [{"taskId": f"t{i}"} for i in range(30)]}
    folded = compact_payload(payload, limit=10)

    assert folded["planId"] == "p1"
    assert folded["requirement"].startswith("长" * 10) and "共 800 字符" in folded["requirement"]
    assert "另有 20 项" in folded["taskList"][-1]


def test_recorder_build_derives_incident_signals_deterministically() -> None:
    """事故判定必须由服务端确定性计算（前端只渲染不推断），否则「什么算事故」口径会漂移。"""
    recorder = TraceRecorder(kind="chat", owner="u-1", request=_request())
    recorder.span(TRACE_KIND_TOOL, "task_add", status=TRACE_STATUS_ERROR, input={"tool": "task_add"})
    recorder.span(TRACE_KIND_GUARD, "谎报完成拦截")
    recorder.span(TRACE_KIND_REFLECT, "自反思", status=TRACE_STATUS_DEGRADED)

    trace = recorder.build(status=TRACE_RUN_OK, steps=0, plan_id=None, pending_count=0, tool_chain=["task_add"])

    assert trace.failedTools == ["task_add"]
    assert trace.guardCount == 1
    assert trace.reflectionCount == 1
    assert trace.hasIncident is True
    # 失败路径拿不到终态步数：必须由 span 反推，否则事故复盘连「崩在第几步」都定位不到
    assert trace.steps == 0
    assert len(trace.traceId) == 32

    clean = TraceRecorder(kind="chat", owner="u-1", request=_request())
    clean.span(TRACE_KIND_TOOL, "get_plan", input={"tool": "get_plan"})
    assert clean.build(status=TRACE_RUN_OK, steps=1, plan_id="p1", pending_count=0, tool_chain=["get_plan"]).hasIncident is False


def test_trace_store_roundtrip_and_owner_isolation() -> None:
    async def _run() -> None:
        store = _trace_store()
        await store.save(_failed_trace(trace_id="a" * 32, owner="u-1"))
        await store.save(_failed_trace(trace_id="b" * 32, owner="u-2"))

        loaded = await store.get("a" * 32)
        assert loaded is not None
        assert loaded.failedTools == ["task_add"]
        assert loaded.request is not None and loaded.request.message == "给 p1 补一个任务"

        # 属主隔离：绝不能从列表里借到他人轨迹
        mine = await store.list("u-1")
        assert [item.traceId for item in mine] == ["a" * 32]
        assert await store.list("u-1") == [item for item in mine]  # 保序稳定
        assert all(item.owner != "u-2" for item in mine)
        assert await store.list("u-other") == []

        summary = summarize_trace(loaded)
        assert summary.spanCount == 3 and summary.guardCount == 1
        assert summary.hasIncident is True and summary.contentPreview == ""

        await store.delete("a" * 32)
        assert await store.get("a" * 32) is None

    asyncio.run(_run())


def test_trace_store_treats_corrupt_payload_as_missing() -> None:
    """脏数据（版本不符）按「不存在」处理，而不是让读取链路整条炸掉。"""

    async def _run() -> None:
        kv = InMemoryKV(store="trace", max_entries=8)
        store = TraceStore(kv, ttl_seconds=60.0)
        await kv.set("tr:" + "c" * 32, {"unexpected": "shape"}, 60)
        assert await store.get("c" * 32) is None

    asyncio.run(_run())


def test_build_trace_store_rejects_redis_without_url_and_defaults_to_memory() -> None:
    """显式选 Redis 却缺地址时启动即失败：多副本共享轨迹的价值全建立在跨副本可见之上。"""
    with pytest.raises(RuntimeError):
        build_trace_store(driver="redis", redis_url="", ttl_seconds=60)
    store = build_trace_store(driver="auto", redis_url="", ttl_seconds=60)
    assert store.backend == "memory"
    assert "runStatuses" in store.describe()


# ================= 能力二：事故 → 回归用例闭环 =================


def test_derive_case_maps_trace_facts_to_assertions() -> None:
    """推导规则只依据轨迹里的客观事实：同一工具失败 → 不得再失败；护栏被触发 → 必须归零。"""
    case = derive_case(_failed_trace(), note="线上事故 INC-42")

    assert case.origin == CUSTOM_CASE_ORIGIN_INCIDENT
    assert case.originTraceId == "a" * 32
    assert case.case.id == "inc-" + "a" * 8  # 幂等：同一条轨迹重复沉淀覆盖同一条用例
    assert case.case.input["message"] == "给 p1 补一个任务"
    expect = case.case.expect
    assert expect["noFalseCompletion"] is True
    assert expect["noFailedTools"] is True
    assert expect["maxGuards"] == 0
    # 断言必须**可复核**：每条为什么被选出来都要有理由
    assert case.rationale and len(case.rationale) == len(expect)
    # 未恢复的失败不得被推导成「必须恢复」——那会让门禁以一次失败为基线而永远红
    assert "reflectionRecovered" not in expect
    assert expect["reflectionCategories"] == ["not_found"]


def test_derive_case_rejects_trace_without_original_request() -> None:
    """没有原始请求的轨迹无法回放，自然也无法沉淀成可重跑的用例——必须显式报错。"""
    with pytest.raises(ApiError) as excinfo:
        derive_case(AgentTrace(traceId="d" * 32, kind="chat", status=TRACE_RUN_ERROR))
    assert excinfo.value.code == "VALIDATION_ERROR"


def test_derive_case_reflection_recovered_when_trace_shows_recovery() -> None:
    """事后回填了 ``recovered`` 的轨迹：该自愈能力必须被写进断言，防止修复把它悄悄删掉。"""
    trace = _failed_trace()
    trace.spans[2].output = {"recovered": True}
    case = derive_case(trace)
    assert case.case.expect["reflectionRecovered"] is True


def test_replay_payload_preserves_original_request() -> None:
    payload = replay_payload(_failed_trace())
    assert payload["kind"] == "chat"
    assert payload["input"] == {"message": "给 p1 补一个任务", "planId": "p1"}
    assert payload["traceId"] == "a" * 32
    assert payload["status"] == TRACE_RUN_ERROR


def test_custom_case_store_lifecycle_and_same_id_overwrite() -> None:
    async def _run() -> None:
        store = _case_store()
        spec = EvalCaseSpec(id="inc-abc", title="回归 A", kind="chat", input={"message": "x"}, expect={"noFalseCompletion": True})
        first = await store.save(case=spec, origin=CUSTOM_CASE_ORIGIN_INCIDENT, owner="u-1", rationale=["r1"])

        assert first.createdAt > 0 and first.included is True
        assert await store.count() == 1
        assert [item.case.id for item in await store.list()] == ["inc-abc"]
        assert [item.id for item in await store.specs()] == ["inc-abc"]

        # 同名覆盖：同一条事故重新推导出更准的断言时应更新同一条，而不是留下两条互相矛盾
        spec.expect["maxGuards"] = 0
        await store.save(case=spec, origin=CUSTOM_CASE_ORIGIN_MANUAL, owner="u-1")
        assert await store.count() == 1
        reloaded = await store.get("inc-abc")
        assert reloaded is not None and reloaded.origin == CUSTOM_CASE_ORIGIN_MANUAL
        assert reloaded.case.expect["maxGuards"] == 0

        assert await store.delete("inc-abc") is True
        assert await store.delete("inc-abc") is False
        assert await store.count() == 0

    asyncio.run(_run())


def test_custom_case_store_rejects_golden_id_and_enforces_cap() -> None:
    """黄金集是基线不可覆盖；达上限必须**拒绝新增并报错**，而不是静默丢弃旧用例。"""

    async def _run() -> None:
        store = _case_store(max_cases=1)
        with pytest.raises(ApiError) as golden:
            await store.save(case=EvalCaseSpec(id=GOLDEN_CASES[0].id, title="x", kind="chat"), owner="u-1")
        assert golden.value.code == "VALIDATION_ERROR"

        await store.save(case=EvalCaseSpec(id="inc-one", title="x", kind="chat"), owner="u-1")
        with pytest.raises(ApiError) as overflow:
            await store.save(case=EvalCaseSpec(id="inc-two", title="y", kind="chat"), owner="u-1")
        assert overflow.value.code == "VALIDATION_ERROR"
        # 覆盖已存在的用例不受上限约束
        await store.save(case=EvalCaseSpec(id="inc-one", title="x2", kind="chat"), owner="u-1")
        assert await store.count() == 1

    asyncio.run(_run())


def test_build_custom_case_store_rejects_redis_without_url() -> None:
    with pytest.raises(RuntimeError):
        build_custom_case_store(driver="redis", redis_url="", ttl_seconds=60)
    assert build_custom_case_store(driver="auto", redis_url="", ttl_seconds=60).backend == "memory"


def test_eval_service_executes_custom_cases_so_the_gate_can_see_them() -> None:
    """沉淀进 store 的用例必须真的被执行：否则「自动纳入 CI 门禁」只是一句空话。"""

    class _Runner:
        available = True
        model_name = "stub"

        async def run_chat(self, session_id: str, message: str, **kwargs):
            return AgentRunResult(content="好的。")

    store = _case_store()
    asyncio.run(
        store.save(
            case=EvalCaseSpec(
                id="inc-gate", title="回归", kind="chat", input={"message": "x"}, expect={"noFalseCompletion": True}
            ),
            origin=CUSTOM_CASE_ORIGIN_INCIDENT,
            owner="u-1",
        )
    )

    service = EvalService(runner=_Runner(), dispatcher=None, custom_cases=store)
    suite = asyncio.run(service.run(EvalRunRequest(caseIds=["inc-gate"], judge=False)))

    assert suite.total == 1
    assert suite.cases[0].caseId == "inc-gate"
    described = asyncio.run(service.describe())
    assert described["customCaseCount"] == 1


def test_cases_for_merges_custom_cases_and_keeps_golden_priority() -> None:
    """沉淀了但不执行等于没沉淀：自定义用例必须能进入运行集，且不得顶掉黄金集基线。"""
    custom = EvalCaseSpec(id="inc-x", title="回归", kind="chat", input={"message": "x"}, expect={})
    merged = cases_for(extra=(custom,))
    assert [case.id for case in merged] == [case.id for case in GOLDEN_CASES] + ["inc-x"]

    # 与黄金集同 ID 的自定义用例必须让位于黄金集（去重口径唯一，避免门禁覆盖面被悄悄改写）
    shadow = EvalCaseSpec(id=GOLDEN_CASES[0].id, title="影子", kind="chat")
    merged_ids = [case.id for case in cases_for(extra=(shadow,))]
    assert merged_ids.count(GOLDEN_CASES[0].id) == 1
    assert [case.title for case in cases_for(extra=(shadow,))][0] == GOLDEN_CASES[0].title

    # kinds 过滤同样作用于合并后的集合
    only = cases_for(kinds=["reflection"], extra=(custom,))
    assert all(case.kind == "reflection" for case in only)
    assert "inc-x" not in {case.id for case in only}


# ================= 能力三：输出溯源与置信度标注 =================


def test_extract_numbers_dedupes_and_keeps_order() -> None:
    assert extract_numbers("共 12 个任务，总工期 42 天，已用 12 天") == ["12", "42"]
    assert extract_numbers("覆盖率 1,250 与 1250 等价") == ["1,250", "1250"]


def test_analyze_provenance_marks_unverified_numbers_honestly() -> None:
    """无据的数字必须标「未核实」并计数，而不是假装有依据——「结论有多可信」得能被算出来。"""
    evidence = collect_evidence([ToolCallRecord(name="schedule_calc", arguments={}, ok=True, result={"totalDurationHours": 42})])
    report = analyze_provenance("总工期 42 天。建议新增 7 个任务。", evidence)

    assert report.totalClaims == 2
    assert report.verified == 1 and report.unverified == 1
    assert report.confidence == 0.5
    assert report.sources[PROVENANCE_SOURCE_TOOL] == 1

    verified_claim, unverified_claim = report.claims
    assert verified_claim.status == PROVENANCE_STATUS_VERIFIED
    assert verified_claim.citations[0].source == PROVENANCE_SOURCE_TOOL
    assert verified_claim.citations[0].ref == "schedule_calc.totalDurationHours"
    assert unverified_claim.status == PROVENANCE_STATUS_UNVERIFIED
    assert "找不到来源" in unverified_claim.detail


def test_analyze_provenance_ignores_prose_without_numbers() -> None:
    """无数字的抒情句不参与溯源：把它算进分母只会稀释「未核实」这个信号。"""
    report = analyze_provenance("建议尽快推进。", collect_evidence([]))
    assert report.totalClaims == 0 and report.unverified == 0 and report.confidence == 1.0


def test_collect_evidence_covers_tools_kb_and_memory() -> None:
    atoms = collect_evidence(
        [ToolCallRecord(name="get_plan", arguments={}, ok=True, result={"totalEstimatedHours": 8})],
        references=[{"chunkId": "kb-1", "title": "工时制度", "text": "单日上限 8 小时"}],
        memories=[MemoryItem(memoryId="m-1", type="preference", text="偏好 PostgreSQL")],
    )
    assert {atom.source for atom in atoms} == {PROVENANCE_SOURCE_TOOL, PROVENANCE_SOURCE_KB, PROVENANCE_SOURCE_MEMORY}
    assert any(atom.ref == "kb-1" and atom.label == "知识库片段·工时制度" for atom in atoms)
    assert any(atom.ref == "m-1" and atom.label == "长期记忆·preference" for atom in atoms)


# ================= 能力四：动态上下文预算管理 =================


def test_classify_zones_separates_workspace_from_history() -> None:
    """分区边界由确定性规则划定：纠偏提示属于**必保工作区**，绝不能因为它 role=user 就被当历史删掉。"""
    messages = [
        {"role": "system", "content": "你是规划助手"},
        {"role": "system", "content": "知识库：单日上限 8 小时"},
        {"role": "user", "content": "第一轮问题"},
        {"role": "assistant", "content": "第一轮回答"},
        {"role": "user", "content": "自我诊断：上一步工具失败，请改用其它入参重试"},
        {"role": "tool", "content": '{"ok":true}', "tool_call_id": "c1"},
        {"role": "user", "content": "第二轮问题"},
    ]
    zones = classify_zones(messages, ("自我诊断：",))
    assert zones == [
        BUDGET_ZONE_SYSTEM,
        BUDGET_ZONE_RECALL,
        BUDGET_ZONE_HISTORY,
        BUDGET_ZONE_HISTORY,
        BUDGET_ZONE_KEEP,
        BUDGET_ZONE_KEEP,
        BUDGET_ZONE_CURRENT,
    ]


def test_allocate_context_trims_recall_zone_before_touching_history() -> None:
    """弹性额度不够时先动**检索增强区**：它是一次性的，删它不会破坏本轮工作区。"""
    messages = [
        {"role": "system", "content": "S" * 10},
        {"role": "system", "content": "R" * 200},
        {"role": "system", "content": "R" * 200},
        {"role": "user", "content": "本轮问题"},
    ]
    report = allocate_context(messages, limit=220)

    assert report.overBudget is False
    assert any("检索增强区超出额度" in note for note in report.notes)
    assert messages[0]["content"] == "S" * 10  # 系统提示词（行为契约）不可动
    assert messages[-1]["content"] == "本轮问题"  # 本轮输入不可动
    assert sum(1 for item in messages if item["content"].startswith("R")) == 0


def test_allocate_context_compresses_old_turns_into_one_summary_line() -> None:
    """历史回合按衰减压缩：旧轮次折叠为一行摘要，仍保留「早先聊过什么」的线索。"""
    messages = [
        {"role": "system", "content": "S" * 10},
        {"role": "user", "content": "A" * 300},
        {"role": "assistant", "content": "B" * 300},
        {"role": "user", "content": "C" * 300},
        {"role": "assistant", "content": "D" * 300},
        {"role": "user", "content": "本轮问题"},
    ]
    report = allocate_context(messages, limit=800, keep_recent_turns=0, summary_chars=80)

    assert report.overBudget is False
    assert any("已压缩为一行摘要" in note for note in report.notes)
    assert messages[0]["content"] == "S" * 10
    assert messages[-1]["content"] == "本轮问题"
    # 确定性压缩：只截断与拼接，不做模型改写
    summary = next(item for item in messages if item["role"] == "system" and item["content"].startswith(SUMMARY_PREFIX))
    assert "共 2 轮" in summary["content"]
    history = next(item for item in report.zones if item.zone == BUDGET_ZONE_HISTORY)
    assert history.compressed is True and history.messages == 1


def test_allocate_context_drops_oldest_turns_when_compression_is_not_enough() -> None:
    """压缩仍超预算时只能逐轮丢弃历史——固定区与必保区是行为契约，不可动。"""
    messages = [{"role": "system", "content": "S"}]
    for index in range(6):
        messages.append({"role": "user", "content": f"问{index}" + "x" * 60})
        messages.append({"role": "assistant", "content": "答" + "y" * 60})
    messages.append({"role": "user", "content": "本轮"})

    report = allocate_context(messages, limit=200, keep_recent_turns=3, summary_chars=20)

    assert report.used <= 200
    assert any("丢弃最旧的" in note for note in report.notes)
    assert messages[-1]["content"] == "本轮"


def test_allocate_context_is_statistics_only_when_limit_disabled() -> None:
    """``limit<=0`` 表示关闭预算管理：此时只统计用量、绝不改动上下文。"""
    messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}]
    report = allocate_context(messages, limit=0)
    assert messages == [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}]
    assert report.limit == 0 and report.overBudget is False
    assert report.used == estimate_chars(messages)
    assert report.notes == []


def test_allocate_context_clears_flexible_zones_when_fixed_zone_exceeds_budget() -> None:
    """固定区独占预算时必须**清空弹性区**：留着半截历史与「已超预算」同时成立只会让超限静默存在。"""
    messages = [
        {"role": "system", "content": "S" * 300},
        {"role": "system", "content": "R" * 200},
        {"role": "user", "content": "旧问题" + "x" * 80},
        {"role": "assistant", "content": "旧回答"},
        {"role": "user", "content": "本轮问题"},
    ]
    report = allocate_context(messages, limit=200, keep_recent_turns=1)

    assert any("固定区" in note and "已全部清空" in note for note in report.notes)
    assert not any(item["content"].startswith("R" * 10) for item in messages)
    assert "x" not in "".join(item["content"] for item in messages)
    assert messages[-1]["content"] == "本轮问题"


# ================= HTTP 全链路 =================


def _readonly_chat_llm() -> ScriptedLlm:
    """只读问答：一次 get_plan + 一段带数字的结论（无写操作，轨迹为正常收敛）。"""
    return ScriptedLlm(
        [
            {"role": "assistant", "content": None, "tool_calls": [tool_call("get_plan", {"planId": "p1"}, "c1")]},
            {"role": "assistant", "content": "计划 p1 共 1 个任务，预计总工期 8 小时。"},
        ]
    )


def test_chat_run_records_trace_provenance_and_budget(make_client) -> None:
    """一次对话必须同时留下：可回放的轨迹、可点开的溯源、可解释的预算明细。"""
    with make_client(llm=_readonly_chat_llm()) as c:
        data = c.post("/api/agent/chat", json={"message": "p1 有哪些任务"}, headers=auth()).json()["data"]

        trace_id = data["traceId"]
        assert trace_id and len(trace_id) == 32

        provenance = data["provenance"]
        assert provenance is not None and provenance["totalClaims"] >= 1
        assert provenance["sources"][PROVENANCE_SOURCE_TOOL] > 0

        budget = data["budget"]
        assert budget is not None and budget["limit"] > 0
        zones = {item["zone"] for item in budget["zones"]}
        assert {BUDGET_ZONE_SYSTEM, BUDGET_ZONE_CURRENT} <= zones
        assert budget["overBudget"] is False

        listed = c.get("/api/agent/traces", headers=auth()).json()["data"]
        assert listed["enabled"] is True and listed["backend"] == "memory"
        assert listed["total"] == 1
        item = listed["items"][0]
        assert item["traceId"] == trace_id
        assert item["spanCount"] >= 3 and item["hasIncident"] is False
        assert item["toolChain"] == ["get_plan"]
        assert item["status"] == TRACE_RUN_OK

        detail = c.get(f"/api/agent/traces/{trace_id}", headers=auth()).json()["data"]
        assert [span["kind"] for span in detail["spans"]][:2] == ["llm", "tool"]
        assert detail["spans"][0]["durationMs"] >= 0
        assert detail["spans"][-1]["kind"] == "final"
        assert detail["request"]["message"] == "p1 有哪些任务"

        replay = c.get(f"/api/agent/traces/{trace_id}/replay", headers=auth()).json()["data"]
        assert replay["kind"] == "chat"
        assert replay["input"] == {"message": "p1 有哪些任务"}
        assert replay["hasIncident"] is False


def test_trace_endpoints_are_owner_isolated_and_404_on_malformed_id(make_client) -> None:
    """轨迹含原始需求与工具返回数据：非属主必须 404（而非 403），格式非法亦然。"""
    with make_client(llm=_readonly_chat_llm()) as c:
        trace_id = c.post("/api/agent/chat", json={"message": "p1 有哪些任务"}, headers=auth()).json()["data"]["traceId"]

        login(c)  # 切换为人类主体

        assert c.get("/api/agent/traces").json()["data"]["items"] == []
        assert c.get(f"/api/agent/traces/{trace_id}").status_code == 404
        assert c.get(f"/api/agent/traces/{trace_id}/replay").status_code == 404
        for bad in ("not-an-id", "a" * 31, "z" * 32):
            response = c.get(f"/api/agent/traces/{bad}", headers=auth())
            assert response.status_code == 404
            assert response.json()["code"] == "TRACE_NOT_FOUND"


def test_trace_disabled_returns_503_instead_of_empty_list(make_client) -> None:
    """轨迹未启用时端点显式报「未启用」，而不是静默返回空列表（空列表会被误读成「没有轨迹」）。"""
    with make_client(overrides={"agent_trace_enabled": False}) as c:
        response = c.get("/api/agent/traces", headers=auth())
        assert response.status_code == 503
        assert response.json()["code"] == "TRACE_STORE_UNAVAILABLE"


def test_incident_http_sinks_trace_into_case_and_feeds_gate(make_client) -> None:
    """事故 → 用例 → 生效集 的完整链路：沉淀后必须立刻能看到它已进入门禁覆盖面。"""
    with make_client() as c:
        ctx = c.app.state.ctx
        trace = _failed_trace(trace_id="f" * 32, owner="machine", kind="plan")
        trace.planId = "p-inc"
        asyncio.run(ctx.traces.save(trace))  # type: ignore[union-attr]

        created = c.post("/api/evals/incidents", json={"traceId": "f" * 32, "note": "来自线上事故"}, headers=auth())
        assert created.status_code == 200, created.text
        data = created.json()["data"]
        case_id = "inc-" + "f" * 8
        assert data["case"]["id"] == case_id
        assert data["case"]["expect"]["noFailedTools"] is True
        assert data["traceId"] == "f" * 32
        # 黄金集 + 这一条自定义用例：沉淀必须立刻体现在生效集里（否则等于没沉淀）
        assert data["effectiveCaseCount"] == len(GOLDEN_CASES) + 1

        listed = c.get("/api/evals/cases", headers=auth()).json()["data"]
        assert listed["total"] == 1
        assert listed["items"][0]["origin"] == "incident"
        assert listed["items"][0]["originTraceId"] == "f" * 32
        assert listed["items"][0]["rationale"]

        effective = c.get("/api/evals/cases/effective", headers=auth()).json()["data"]
        assert case_id in effective["customIds"]

        # 回填：同一事故不得被反复沉淀成多条
        detail = c.get(f"/api/agent/traces/{'f' * 32}", headers=auth()).json()["data"]
        assert detail["incidentCaseId"] == case_id

        # 非属主沉淀他人事故：404（不泄露轨迹存在性）
        login(c)
        assert c.post("/api/evals/incidents", json={"traceId": "f" * 32}).status_code == 404
        assert c.delete(f"/api/evals/cases/{case_id}").status_code == 404


def test_incident_http_requires_existing_trace(make_client) -> None:
    """轨迹不存在 / ID 格式非法时统一 404，且不创建任何用例。"""
    with make_client() as c:
        assert c.post("/api/evals/incidents", json={"traceId": "not-an-id"}, headers=auth()).status_code == 404
        assert c.post("/api/evals/incidents", json={"traceId": "b" * 32}, headers=auth()).status_code == 404
        assert c.get("/api/evals/cases", headers=auth()).json()["data"]["total"] == 0


def test_incident_case_can_be_deleted_from_gate(make_client) -> None:
    with make_client() as c:
        asyncio.run(c.app.state.ctx.traces.save(_failed_trace(trace_id="e" * 32, owner="machine")))
        case_id = c.post("/api/evals/incidents", json={"traceId": "e" * 32}, headers=auth()).json()["data"]["case"]["id"]

        deleted = c.delete(f"/api/evals/cases/{case_id}", headers=auth())
        assert deleted.status_code == 200 and deleted.json()["data"]["deleted"] is True
        assert c.get("/api/evals/cases", headers=auth()).json()["data"]["total"] == 0
        assert c.get("/api/evals/cases/effective", headers=auth()).json()["data"]["customIds"] == []
