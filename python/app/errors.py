"""统一异常模型与错误信封。

与 TypeScript 引擎（``src/api/errors.ts``）保持完全一致的响应契约：

成功：``{"data": ...}``
失败：``{"code": str, "message": str, "details": Any}``

基础错误码（与引擎一致）：``CYCLE_DEPENDENCY``(409) / ``VALIDATION_ERROR``(400) /
``LLM_FORMAT_ERROR``(422) / ``STORAGE_UNAVAILABLE``(503) / ``UNAUTHORIZED``(401)。

Python 对外服务扩展错误码：``ENGINE_UNAVAILABLE``(502) / ``ENGINE_CIRCUIT_OPEN``(503) /
``LLM_UNAVAILABLE``(503) / ``LLM_GATEWAY_ERROR``(502) / ``EMBEDDING_UNAVAILABLE``(503) /
``VECTOR_STORE_UNAVAILABLE``(503) / ``SESSION_STORE_UNAVAILABLE``(503) / ``SESSION_BUSY``(409) /
``RATE_LIMITED``(429) / ``PAYLOAD_TOO_LARGE``(413) / ``SERVICE_UNAVAILABLE``(503) /
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
    message: str = (
        "未配置 LLM 网关，Agent 功能不可用。"
        "免费方案：智谱 GLM-4-Flash（文本模型长期免费、支持流式输出与 Function Calling）——"
        "前往 https://open.bigmodel.cn 免费注册后创建 API Key，然后设置 "
        "LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4、LLM_MODEL=glm-4-flash、LLM_API_KEY=<你的密钥>；"
        "自建无鉴权网关（vLLM / Ollama）只需设置 LLM_BASE_URL 与 LLM_MODEL"
    ),
) -> ApiError:
    return ApiError(503, "LLM_UNAVAILABLE", message)


def llm_gateway_error(message: str, details: Any | None = None) -> ApiError:
    """LLM 网关不可达或返回异常状态。

    与 :func:`bad_gateway`（算法引擎）区分开：调用方与监控需要按**失败的依赖方**分流，
    例如前端在模型侧不可用时降级为内置规则问答，而引擎故障则应原样提示。
    """
    return ApiError(502, "LLM_GATEWAY_ERROR", message, details)


def engine_circuit_open(message: str = "算法引擎连续失败，已触发熔断保护，请稍后重试") -> ApiError:
    """引擎断路器处于开路状态（保护上游不被雪崩拖垮）。"""
    return ApiError(503, "ENGINE_CIRCUIT_OPEN", message)


def embedding_unavailable(message: str = "", *, hint: str = "") -> ApiError:
    """向量化服务不可用（未配置 / 上游不可达）。

    ``hint`` 由调用方按实际配置状态生成（:attr:`Settings.embedding_setup_hint`）并拼接，
    避免在错误文案里写死服务商或写死「待补齐项」——那会把用户引向必然失败的配置。
    """
    detail = message or "未配置向量化模型（Embedding），知识库与长期记忆不可用"
    if hint:
        detail = f"{detail}；{hint}"
    return ApiError(503, "EMBEDDING_UNAVAILABLE", detail)


def embedding_gateway_error(message: str, details: Any | None = None) -> ApiError:
    """向量化上游返回异常状态（与 LLM 网关错误区分，便于按依赖方分流监控）。"""
    return ApiError(502, "EMBEDDING_GATEWAY_ERROR", message, details)


def vector_store_unavailable(message: str, details: Any | None = None) -> ApiError:
    """向量库不可用（目录不可写 / 依赖缺失）。"""
    return ApiError(503, "VECTOR_STORE_UNAVAILABLE", message, details)


def rate_limited(message: str = "请求过于频繁，请稍后重试", retry_after: int | None = None) -> ApiError:
    """触发速率限制（令牌桶耗尽）。"""
    details = {"retryAfterSeconds": retry_after} if retry_after is not None else None
    return ApiError(429, "RATE_LIMITED", message, details)


def service_unavailable(message: str = "服务尚未就绪") -> ApiError:
    """就绪探针未通过 / 正在优雅关机。"""
    return ApiError(503, "SERVICE_UNAVAILABLE", message)


def payload_too_large(message: str, details: Any | None = None) -> ApiError:
    """请求体超过服务端上限。

    由 :class:`app.security.BodySizeLimitMiddleware` 在**读取请求体之前/期间**抛出：
    未做限制时，一个超大 body（尤其是文件上传）会被整体读进内存，足以打爆进程，
    而限流按「请求数/分钟」计量，防不住这一维度。
    """
    return ApiError(413, "PAYLOAD_TOO_LARGE", message, details)


def session_store_unavailable(message: str, details: Any | None = None) -> ApiError:
    """会话存储（Redis）不可用：明确失败，避免「以为存上了其实没存」的隐蔽故障。"""
    return ApiError(503, "SESSION_STORE_UNAVAILABLE", message, details)


def session_busy(message: str = "同一会话仍有请求在处理中，请稍后重试") -> ApiError:
    """同一会话的并发请求超过等待上限（跨副本分布式锁未获取到）。

    这里选择「明确失败」而非「放弃互斥继续执行」：后者会让两个请求并发改写同一份
    会话上下文，造成历史丢失（last-write-wins），比一次可重试的错误更难排查。
    """
    return ApiError(409, "SESSION_BUSY", message)


def internal(message: str = "服务内部错误") -> ApiError:
    return ApiError(500, "INTERNAL_ERROR", message)
