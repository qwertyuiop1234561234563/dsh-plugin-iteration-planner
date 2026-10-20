"""MCP stdio 传输：把本项目作为本地子进程接入任意 MCP 客户端。

用法（在客户端配置里填一条命令即可）::

    {
      "mcpServers": {
        "iteration-planner": {
          "command": "python",
          "args": ["-m", "app.mcp.stdio"],
          "cwd": "<repo>/python"
        }
      }
    }

协议按 MCP 规范：**stdin / stdout 上跑换行分隔的 JSON-RPC**。因此本进程有一条铁律——
**stdout 只允许出现协议帧**，任何日志、警告都必须写 stderr（见 ``_configure_logging``）；
一旦有第三方库往 stdout 打了一行 INFO，客户端看到的就是「非法 JSON-RPC」，整个通道报废。

**为什么 stdio 子进程不装配知识库 / 长期记忆**：Chroma 的持久化客户端不支持多进程
并发访问同一个目录。主服务已经常驻打开了向量库，再让每个客户端各起一个 stdio 子进程去
打开同一目录，会把向量库置于损坏风险中。所以 stdio 传输只暴露**引擎类**能力（建计划 /
重规划 / 任务增删改 / DAG / 关键路径 / 风险 / 报表），需要知识库与记忆检索时请改用
Streamable HTTP 端点（``POST /api/mcp``）——它跑在主服务进程内，与知识库共用同一实例。
工具清单据此**主动裁剪**（不声明必定报错的工具）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from ..agent.schemas import RETRIEVAL_TOOLS, TOOL_SPECS
from ..agent.tools import ToolDispatcher
from ..config import get_settings
from ..engine_client import EngineClient
from ..mcp.server import McpServer
from ..resilience import CircuitBreaker, RetryPolicy
from ..sessions import build_session_store

logger = logging.getLogger("iteration_planner.mcp.stdio")

#: 核心（引擎类）工具：排除检索类，理由见模块文档
_CORE_TOOLS: frozenset[str] = frozenset(
    spec["function"]["name"] for spec in TOOL_SPECS
) - RETRIEVAL_TOOLS


def _configure_logging() -> None:
    """把本进程的全部日志强制写到 stderr（stdout 留给 JSON-RPC）。"""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


async def _serve(server: McpServer, *, stdin: Any, stdout: Any) -> None:
    """逐行读取 JSON-RPC 并回写响应（一次一行，保持帧边界）。"""
    loop = asyncio.get_running_loop()
    session_id: str | None = None
    while True:
        line = await loop.run_in_executor(None, stdin.readline)
        if not line:
            logger.info("stdin 关闭，MCP stdio 会话结束")
            return
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            # 解析失败按 JSON-RPC 规范回 -32700（id 未知，置 null）
            await _write(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON 解析失败"}})
            continue
        response, session_id = await server.handle(message, session_id=session_id)
        if response is not None:
            await _write(stdout, response)


async def _write(stdout: Any, payload: Any) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    stdout.flush()


async def run_stdio() -> int:
    """启动 stdio 传输（阻塞直到 stdin 关闭）。"""
    _configure_logging()
    settings = get_settings()
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
    store = build_session_store(
        driver=settings.session_store,
        max_sessions=settings.agent_max_sessions,
        ttl_seconds=settings.agent_session_ttl_seconds,
        redis_url=settings.redis_url,
        lock_ttl_seconds=settings.agent_session_lock_ttl_seconds,
        lock_wait_seconds=settings.agent_session_lock_wait_seconds,
    )
    await store.start()
    server = McpServer(
        dispatcher=ToolDispatcher(engine),
        store=store,
        server_name=settings.mcp_server_name,
        max_result_chars=settings.mcp_max_result_chars,
        available_tools=_CORE_TOOLS,
    )
    logger.info(
        "MCP stdio 已就绪：engine=%s tools=%d（检索类工具需改用 POST %s/mcp）",
        settings.engine_base_url,
        len(server.tool_names),
        settings.api_base_path,
    )
    try:
        await _serve(server, stdin=sys.stdin, stdout=sys.stdout)
    finally:
        await store.close()
        await engine.aclose()
    return 0


def main() -> int:
    """``python -m app.mcp.stdio`` 入口。"""
    try:
        return asyncio.run(run_stdio())
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        return 130


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
