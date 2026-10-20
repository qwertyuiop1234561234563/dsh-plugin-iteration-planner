"""评测门禁 CLI：``python -m app.evals.gate``（CI 里以退出码判定放行）。

为什么需要一个 CLI，而不是只留一个 HTTP 端点：**门禁必须能被流水线直接消费**。
CI 只需要一个数字（0 / 1），让流水线额外去起服务、登录、发请求、解析 JSON、再自己算一遍
阈值，等于把门禁逻辑抄到了流水线里——两处阈值迟早不一致，而「门禁失准」比「没有门禁」更糟。

本 CLI 直接装配与线上同一套组件（引擎客户端 + 工具调度器 + 编排器 + 评测服务），
不经过 HTTP 层，因此**没有鉴权 / 路由 / 网关超时的干扰**，跑出来的就是评测结论本身。

用法::

    python -m app.evals.gate                    # 跑全部黄金集，按门禁退出 0 / 1
    python -m app.evals.gate --kind reflection   # 只跑确定性反思演练（无需 LLM）
    python -m app.evals.gate --case chat-critical-path --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from ..agent import AgentRunner, OpenAiCompatibleClient, ToolDispatcher
from ..config import get_settings
from ..engine_client import EngineClient
from ..evals import build_eval_service
from ..models import EvalRunRequest
from ..resilience import CircuitBreaker, RetryPolicy
from ..sessions import build_session_store

logger = logging.getLogger("iteration_planner.evals.gate")


def _build_service(settings: Any) -> tuple[Any, Any, Any]:
    """装配「引擎 → 调度器 → 编排器 → 评测服务」。

    刻意与 ``app.main`` 的装配保持同样的参数口径（重试 / 断路器 / 会话存储），
    但**不复用 FastAPI 应用**：门禁不需要 HTTP 层，也不需要知识库 / 长期记忆
    （评测用例只打引擎与编排逻辑），少一层依赖就少一处环境坑。
    """
    engine = EngineClient(
        settings.engine_base_url,
        settings.api_base_path,
        settings.engine_timeout_seconds,
        connect_timeout=settings.engine_connect_timeout_seconds,
        max_connections=settings.engine_max_connections,
        max_keepalive_connections=settings.engine_max_keepalive_connections,
        retry=RetryPolicy(
            attempts=settings.engine_retry_attempts,
            base_delay=settings.engine_retry_base_delay,
            max_delay=settings.engine_retry_max_delay,
        ),
        breaker=CircuitBreaker(
            failure_threshold=settings.engine_breaker_threshold,
            recovery_timeout=settings.engine_breaker_recovery_seconds,
        ),
    )
    llm = None
    if settings.llm_enabled:
        llm = OpenAiCompatibleClient(
            settings.llm_base_url,
            settings.llm_api_key,
            settings.llm_model,
            settings.llm_timeout_seconds,
            retry=RetryPolicy(
                attempts=settings.llm_retry_attempts,
                base_delay=settings.llm_retry_base_delay,
                max_delay=max(settings.llm_retry_base_delay * 4, 2.0),
            ),
        )
    store = build_session_store(
        driver=settings.session_store,
        max_sessions=settings.agent_max_sessions,
        ttl_seconds=settings.agent_session_ttl_seconds,
        redis_url=settings.redis_url,
        lock_ttl_seconds=settings.agent_session_lock_ttl_seconds,
        lock_wait_seconds=settings.agent_session_lock_wait_seconds,
    )
    dispatcher = ToolDispatcher(engine)
    runner = AgentRunner(
        llm,
        dispatcher,
        max_steps=settings.agent_max_steps,
        temperature=settings.agent_temperature,
        store=store,
        max_context_chars=settings.agent_max_context_chars,
        kb_auto_inject=False,
        reflect_enabled=settings.agent_reflect_enabled,
        reflect_max_retries=settings.agent_reflect_max_retries,
    )
    return build_eval_service(settings, runner=runner, dispatcher=dispatcher), engine, llm


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="app.evals.gate", description="Agent 评测门禁（CI 退出码判定）")
    parser.add_argument("--case", action="append", default=[], help="只跑指定用例 id（可重复）")
    parser.add_argument("--kind", action="append", default=[], help="按类型过滤：plan / chat / reflection")
    parser.add_argument("--no-judge", action="store_true", help="关闭 LLM-as-judge，只用确定性断言打分")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出完整结果（供流水线归档）")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.evals_enabled:
        print("评测台未启用（EVALS_ENABLED=false）：门禁无法给出结论", file=sys.stderr)
        return 1
    service, engine, llm = _build_service(settings)
    try:
        request = EvalRunRequest(
            caseIds=args.case or None,
            kinds=args.kind or None,
            judge=False if args.no_judge else None,
        )
        suite = await service.run(request)
    finally:
        await engine.aclose()
        if isinstance(llm, OpenAiCompatibleClient):
            await llm.aclose()

    if args.json:
        print(json.dumps(suite.model_dump(), ensure_ascii=False, indent=2))
    else:
        print(
            f"评测完成：共 {suite.total} 例，通过 {suite.passed}，失败 {suite.failed}，"
            f"异常 {suite.errored}；通过率 {suite.passRate:.0%}，平均分 {suite.avgScore:.2f}"
        )
        for case in suite.cases:
            mark = {"pass": "PASS", "fail": "FAIL", "error": "ERR "}.get(case.outcome, case.outcome)
            print(f"  [{mark}] {case.caseId}  score={case.score:.2f}  steps={case.steps}")
        for note in suite.notes:
            print(f"  说明：{note}")
        if suite.gate is not None:
            for reason in suite.gate.reasons:
                print(f"  门禁：{reason}", file=sys.stderr)
    return 0 if (suite.gate and suite.gate.passed) else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())


__all__ = ["main"]
