"""Agent Evals 评测台：黄金集 + 双轨打分 + CI 回归门禁。

**为什么需要评测台**：Agent 的行为（是否选对工具、是否把失败说成成功、是否凭空编造排期）
用单元测试很难断言——它依赖模型的临场选择。没有评测集，任何一次提示词微调、模型切换、
工具增减都只能靠人肉点几下「看起来还行」，回归风险完全不可控。

本模块提供三层能力：

1. **黄金集**（:mod:`app.evals.golden`）：需求 → 期望工具链 / 期望结论的固定用例；
2. **双轨打分**（:mod:`app.evals.assertions` + :mod:`app.evals.judge`）：
   确定性断言是**硬门槛**（不过即判失败），LLM-as-judge 只对自然语言结论质量补充打分；
3. **门禁**（:class:`app.evals.service.EvalService`）：汇总通过率 / 平均分并与阈值比对，
   可直接作为流水线退出码依据（``python -m app.evals.gate``）。

设计对齐 OpenAI Evals / LangSmith 的「数据集 + 打分器 + 回归门禁」三段式，但刻意**不采用
纯模型打分**：模型给自己打分容易被「像模像样的空话」骗过，而「有没有真的调引擎」「有没有
把待确认动作登记下来」这类事实只有确定性断言能可靠判定。
"""

from .golden import GOLDEN_CASES, case_by_id, cases_for
from .service import EvalService, build_eval_service

__all__ = ["GOLDEN_CASES", "case_by_id", "cases_for", "EvalService", "build_eval_service"]
