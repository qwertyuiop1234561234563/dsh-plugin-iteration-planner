"""MCP（Model Context Protocol）服务端：把内部能力暴露为标准 MCP 工具。

把本项目从「一个 Web 应用」变成「一个可被任意 MCP 客户端挂载的能力源」：
Claude Desktop / Cursor / VS Code Copilot / 自研 Agent 只要按 MCP 规范接入，就能直接
调用**同一套**工具（建计划 / 重规划 / 调成员 / 风险体检 / 知识库 / 记忆），无需理解
本项目内部的 HTTP 契约。

对外模块：
- :mod:`app.mcp.server`：协议实现（``initialize`` / ``tools/list`` / ``tools/call``）；
- :mod:`app.mcp.stdio`：stdio 传输（`python -m app.mcp.stdio`）；
- :mod:`app.routes.mcp`：Streamable HTTP 传输（``POST /api/mcp``）。
"""

from .server import McpServer, build_mcp_server, verify_token

__all__ = ["McpServer", "build_mcp_server", "verify_token"]
