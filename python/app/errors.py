"""统一异常模型与错误信封。

与 TypeScript 引擎（``src/api/errors.ts``）保持完全一致的响应契约：

成功：``{"data": ...}``
失败：``{"code": str, "message": str, "details": Any}``

基础错误码（与引擎一致）：``CYCLE_DEPENDENCY``(409) / ``VALIDATION_ERROR``(400) /
``LLM_FORMAT_ERROR``(422) / ``STORAGE_UNAVAILABLE``(503) / ``UNAUTHORIZED``(401) /
``FORBIDDEN``(403) / ``NOT_FOUND``(404)。

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
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        #: 需要随响应下发的附加首部（如 ``Proxy-Status`` / ``Retry-After``）。
        #: 由 :func:`app.main._register_exception_handlers` 写入响应，避免各路由重复处理。
        self.headers = headers or {}

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


def forbidden(message: str = "当前账号无权执行该操作") -> ApiError:
    """已通过身份鉴权，但角色权限不足（与 401 区分：重登无法解决，须调整角色 / 指派）。"""
    return ApiError(403, "FORBIDDEN", message)


def not_found(message: str) -> ApiError:
    return ApiError(404, "NOT_FOUND", message)


def bad_gateway(message: str = "算法引擎暂不可达，请稍后重试", details: Any | None = None) -> ApiError:
    """内部算法引擎不可达 / 返回异常。

    默认文案为**统一通用文案**：不得泄漏上游连接细节（httpx 异常类名）、引擎地址或原始
    异常信息；这些细节只记录到服务端日志（见 :meth:`EngineClient._transport_error`）。
    """
    return ApiError(502, "ENGINE_UNAVAILABLE", message, details)


def gateway_timeout(message: str = "算法引擎处理超时，请稍后重试", details: Any | None = None) -> ApiError:
    """算法引擎**可达但在超时内未返回**（与 :func:`bad_gateway` 严格区分）。

    RFC 9110 语义：502 表示「作为网关收到了无效响应」（连不上 / 协议错），504 才是
    「未能在时限内从上游收到响应」。二者混用会让调用方与监控误判为「引擎挂了」，
    进而触发无意义的排障方向（真实原因是引擎**在跑**、只是慢），也无法据此区分
    「可重试的慢」与「不可达」。

    随响应附带 IANA Proxy-Status 首部（``error=http_response_timeout``），让上游代理与
    可观测系统按标准语义识别本次失败原因。
    """
    return ApiError(
        504,
        "ENGINE_TIMEOUT",
        message,
        details,
        headers={"Proxy-Status": 'iteration_planner; error="http_response_timeout"'},
    )


def idempotency_conflict(message: str = "相同 Idempotency-Key 的请求仍在处理中，请稍后重试") -> ApiError:
    """同键请求正在处理中（RFC/draft-07：不得并发执行同一幂等键）。"""
    return ApiError(409, "IDEMPOTENCY_IN_PROGRESS", message, headers={"Retry-After": "1"})


def idempotency_mismatch(message: str = "该 Idempotency-Key 已被用于不同的请求内容") -> ApiError:
    """同键但请求指纹不同：复用键会造成语义歧义，必须拒绝（Stripe / draft-07 口径）。"""
    return ApiError(422, "IDEMPOTENCY_KEY_REUSE", message)


def job_not_found(message: str = "作业不存在或已过期") -> ApiError:
    """作业不存在 / 已过期（过期与不存在统一回报，避免探测作业是否存在）。"""
    return ApiError(404, "JOB_NOT_FOUND", message)


def job_conflict(message: str = "作业已处于终态，无法重复操作") -> ApiError:
    """对已进入终态（succeeded / failed）的作业再次取消等。"""
    return ApiError(409, "JOB_CONFLICT", message)


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


def job_store_unavailable(message: str = "作业存储暂不可用，请稍后重试") -> ApiError:
    """作业存储不可用：必须显式失败。

    作业是**状态资源**：若此处降级放行，会出现「已返回 202 + jobId，但该作业并不存在」
    的假成功，客户端会永远轮询不到结果且无法察觉。宁可让提交明确失败（可重试）。
    """
    return ApiError(503, "JOB_STORE_UNAVAILABLE", message)


def session_busy(message: str = "同一会话仍有请求在处理中，请稍后重试") -> ApiError:
    """同一会话的并发请求超过等待上限（跨副本分布式锁未获取到）。

    这里选择「明确失败」而非「放弃互斥继续执行」：后者会让两个请求并发改写同一份
    会话上下文，造成历史丢失（last-write-wins），比一次可重试的错误更难排查。
    """
    return ApiError(409, "SESSION_BUSY", message)


def internal(message: str = "服务内部错误") -> ApiError:
    return ApiError(500, "INTERNAL_ERROR", message)


def checkpoint_not_found(message: str = "检查点不存在或已过期") -> ApiError:
    """检查点台账不存在。

    与 :func:`not_found` 分开：前端需要据此区分「计划不存在」与「台账过期」，
    两者给用户的下一步指引完全不同（前者去查计划，后者需重新发起编排）。
    """
    return ApiError(404, "CHECKPOINT_NOT_FOUND", message)


def checkpoint_store_unavailable(message: str = "检查点存储暂不可用，请稍后重试") -> ApiError:
    """检查点存储（Redis / 内存）不可用：必须显式失败。

    台账是**状态数据**，静默降级会让「登记了待确认动作」变成假象——用户之后点确认
    只会得到 404，且无法判断是过期还是根本没存上。
    """
    return ApiError(503, "CHECKPOINT_STORE_UNAVAILABLE", message)


def checkpoint_not_resumable(message: str = "该检查点已终态，无法续跑") -> ApiError:
    """对已终态（已执行 / 已驳回 / 已作废）的台账再次续跑 —— 属于语义冲突。"""
    return ApiError(409, "CHECKPOINT_NOT_RESUMABLE", message)
