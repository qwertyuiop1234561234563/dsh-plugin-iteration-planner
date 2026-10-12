"""统一异常模型与错误信封。

与 TypeScript 引擎（``src/api/errors.ts``）保持完全一致的响应契约：

成功：``{"data": ...}``
失败：``{"code": str, "message": str, "details": Any}``

基础错误码（与引擎一致）：``CYCLE_DEPENDENCY``(409) / ``VALIDATION_ERROR``(400) /
``LLM_FORMAT_ERROR``(422) / ``STORAGE_UNAVAILABLE``(503) / ``UNAUTHORIZED``(401)。

Python 对外服务扩展错误码：``ENGINE_UNAVAILABLE``(502) / ``ENGINE_CIRCUIT_OPEN``(503) /
``LLM_UNAVAILABLE``(503) / ``RATE_LIMITED``(429) / ``SERVICE_UNAVAILABLE``(503) /
``AGENT_MAX_STEPS``(422) / ``INTERNAL_ERROR``(500)。
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """可预期的业务 API 异常，携带 HTTP 状态码与稳定错误码。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    def to_body(self) -> dict[str, Any]:
        """转换为标准错误响应体。"""
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            body["details"] = self.details
        return body


# ------------------------------- 常用构造器 -------------------------------


def bad_request(message: str, details: Any | None = None) -> ApiError:
    return ApiError(400, "VALIDATION_ERROR", message, details)


def unauthorized(message: str = "缺少或无效的访问令牌") -> ApiError:
    return ApiError(401, "UNAUTHORIZED", message)


def not_found(message: str) -> ApiError:
    return ApiError(404, "NOT_FOUND", message)


def bad_gateway(message: str, details: Any | None = None) -> ApiError:
    """内部算法引擎不可达 / 返回异常。"""
    return ApiError(502, "ENGINE_UNAVAILABLE", message, details)


def llm_unavailable(
    message: str = "未配置 LLM 网关，Agent 功能不可用；请设置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL",
) -> ApiError:
    return ApiError(503, "LLM_UNAVAILABLE", message)


def engine_circuit_open(message: str = "算法引擎连续失败，已触发熔断保护，请稍后重试") -> ApiError:
    """引擎断路器处于开路状态（保护上游不被雪崩拖垮）。"""
    return ApiError(503, "ENGINE_CIRCUIT_OPEN", message)


def rate_limited(message: str = "请求过于频繁，请稍后重试", retry_after: int | None = None) -> ApiError:
    """触发速率限制（令牌桶耗尽）。"""
    details = {"retryAfterSeconds": retry_after} if retry_after is not None else None
    return ApiError(429, "RATE_LIMITED", message, details)


def service_unavailable(message: str = "服务尚未就绪") -> ApiError:
    """就绪探针未通过 / 正在优雅关机。"""
    return ApiError(503, "SERVICE_UNAVAILABLE", message)


def internal(message: str = "服务内部错误") -> ApiError:
    return ApiError(500, "INTERNAL_ERROR", message)