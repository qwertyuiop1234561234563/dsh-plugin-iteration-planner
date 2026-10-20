"""黄金评测集：一组「需求 → 期望事实」的固定用例，作为 Agent 行为的回归基线。

用例选取原则（对齐 Bedrock / LangSmith 评测集的取舍）：

- **覆盖真实失败模式**，而不是覆盖 API 表面。每条用例都对应一种我们真的踩过或在生产
  Agent 上常见的故障：把写指令当查询空转、猜到不存在的 taskId、编造排期数字、
  失败后无脑重试把步数烧光、引擎不可用时假装算出了结果。
- **断言只写可复算的事实**。``content`` 上的断言只用于「必须/不得出现某关键词」这类
  低歧义检查；「好不好」交给 LLM-as-judge。
- **自建夹具 + 自清理**。需要计划的用例用 ``dependsOn`` 复用前置用例产出的 planId，
  跑完回收计划 —— 评测绝不能污染真实数据集，否则跑几轮之后用户会发现多出一堆测试计划。

``kind=plan`` / ``kind=chat`` 的用例真实驱动 Agent（需要配置 LLM）。未配置 LLM 时评测服务
会把这类用例记为 ``error``（而不是 ``fail``），并给出说明：**环境不具备 ≠ Agent 退化**，
把它算进失败率会让门禁在没接模型的环境里必然红。
``kind=reflection`` 的用例是**确定性演练**（脚本化模型 + 桩调度器），不需要真实 LLM，
因此哪怕在 CI 的纯离线环境里也能稳定验证自反思自愈链路。
"""

from __future__ import annotations

from ..models import EvalCaseSpec

#: 黄金集
GOLDEN_CASES: tuple[EvalCaseSpec, ...] = (
    EvalCaseSpec(
        id="plan-order-center",
        title="端到端规划：订单中心迭代拆解",
        kind="plan",
        input={
            "requirement": "为订单中心完成 v2.1 迭代：数据库分库改造、订单状态机重构、对账补偿任务、灰度发布方案。",
            "iterationDeadline": "2026-11-15",
            "devMemberCount": 4,
        },
        expect={"hasPlan": True, "minTasks": 1, "maxSteps": 8, "noFalseCompletion": True},
        rubric=(
            "评估结论是否基于工具返回的真实计划（给出任务拆解与依赖关系），"
            "是否明确关键路径或风险，而不是泛泛而谈的方法论。"
        ),
    ),
    EvalCaseSpec(
        id="chat-critical-path",
        title="工具选择：读取计划的关键路径",
        kind="chat",
        dependsOn="plan-order-center",
        input={"message": "请读取这个计划并给出关键路径与总工期。", "planId": "$dep"},
        expect={
            "mustCallTools": ["schedule_calc"],
            "noPending": True,
            "noFailedTools": True,
            "noFalseCompletion": True,
        },
        rubric="评估是否用工具返回的真实任务与工期作答，是否说明了关键路径的构成。",
        cleanup=False,
    ),
    EvalCaseSpec(
        id="chat-write-deferred",
        title="两阶段确认：写指令必须登记为待确认而非直接落库",
        kind="chat",
        dependsOn="plan-order-center",
        input={"message": "把「数据库设计」这个任务的预估工时调整为 12 小时。", "planId": "$dep"},
        expect={"requiresPending": True, "maxSteps": 8},
        rubric="评估是否正确识别了目标任务（未凭猜测的 ID 调用）并说明改动内容与影响。",
        cleanup=False,
    ),
    EvalCaseSpec(
        id="reflection-recover-not-found",
        title="自反思：目标不存在时换真实 ID 恢复",
        kind="reflection",
        drill="not_found",
        expect={
            "reflectionCategories": ["not_found"],
            "reflectionRecovered": True,
            "maxReflections": 1,
        },
    ),
    EvalCaseSpec(
        id="reflection-circuit-break",
        title="自反思熔断：同一失败反复出现时降级而非空转",
        kind="reflection",
        drill="engine_unavailable",
        expect={
            "reflectionCategories": ["engine_unavailable"],
            "reflectionStrategies": ["degrade"],
            "maxReflections": 3,
            "reflectionRecovered": False,
        },
    ),
)

_BY_ID: dict[str, EvalCaseSpec] = {case.id: case for case in GOLDEN_CASES}


def case_by_id(case_id: str) -> EvalCaseSpec | None:
    return _BY_ID.get(case_id)


def cases_for(
    case_ids: list[str] | None = None,
    kinds: list[str] | None = None,
    *,
    limit: int | None = None,
) -> list[EvalCaseSpec]:
    """按 ID / 类型筛选用例（缺省返回全集），并按 ``limit`` 截断。

    筛选后按 :data:`GOLDEN_CASES` 的**原顺序**返回：``dependsOn`` 依赖「前置用例先执行」，
    顺序必须稳定，不能因为调用方传参顺序不同而改变。
    """
    selected = list(GOLDEN_CASES)
    if case_ids:
        wanted = set(case_ids)
        selected = [case for case in selected if case.id in wanted]
    if kinds:
        wanted_kinds = set(kinds)
        selected = [case for case in selected if case.kind in wanted_kinds]
    if limit is not None:
        selected = selected[: max(0, limit)]
    return selected


__all__ = ["GOLDEN_CASES", "case_by_id", "cases_for"]
