"""结构化日志 + 请求关联 ID（correlation id）。

企业级排障的核心诉求是「一次请求的全链路可关联」：
- 中间件为每个请求分配 / 透传 ``X-Request-Id``，写入 contextvar；
- JSON 日志在每条记录中自动携带该 ID；
- 该 ID 同时透传给内部算法引擎（``x-request-id`` 请求头）与前端（响应头），
  从而把「Python 网关日志 ↔ 引擎审计日志 ↔ 前端报错」串成一条链。
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

#: 当前请求的关联 ID（无请求上下文时为空串）
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

#: 标准 LogRecord 内置属性，序列化时需排除，避免噪音
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
}


class JsonFormatter(logging.Formatter):
    """把日志记录序列化为单行 JSON（便于采集到 ELK / Loki 等）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["requestId"] = request_id
        # 允许通过 logging.info(..., extra={"extra_fields": {...}}) 追加结构化字段
        extra_fields = getattr(record, "extra_fields", None)
        if isinstance(extra_fields, dict):
            payload.update(extra_fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "info", *, force: bool = False) -> None:
    """安装 JSON 日志处理器。

    :param force: 为 True 时替换已有根处理器（服务启动时使用）；否则仅在无处理器时安装。
    """
    root = logging.getLogger()
    if root.handlers and not force:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers[:] = [handler]
    root.setLevel(_resolve_level(level))
    # uvicorn 的 access logger 会产生重复噪音，交由我们的中间件统一记录
    logging.getLogger("uvicorn.access").disabled = True


def _resolve_level(level: str) -> int:
    resolved = logging.getLevelName(level.upper())
    return resolved if isinstance(resolved, int) else logging.INFO


__all__ = ["JsonFormatter", "configure_logging", "request_id_var"]