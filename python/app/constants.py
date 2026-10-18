"""Python 侧统一常量（枚举 / 限额），避免魔法值与字符串散落各处。

约定（与项目硬约束一致）：
- 业务算法口径（任务类型 / 状态 / 优先级 / 缓冲规则 / 风险规则）的**唯一事实来源**
  仍是 TypeScript 引擎 ``src/constants/enums.ts``，Python 通过 ``GET /api/meta/enums``
  取值，**不在本文件复制**；
- 本文件只登记「Python 原生治理端点」新增的枚举与限额（人工风险登记册 / 数据导入 /
  配置备份 / 工作区存储），保证同一份取值只维护一次。
"""

from __future__ import annotations

import os
from pathlib import Path

# ------------------------------ 访问控制：角色 / 权限 ------------------------------
# 设计口径（对齐 Plane / OpenProject / Vikunja / Focalboard 的共同做法）：
# **固定少量角色 + 权限表驱动**，权限一律在服务端判定，前端只按权限清单做展示收敛。
#
#   admin  控制台管理员：除业务读写外，唯一可管理用户与指派（含 guest 计划白名单）
#   member 项目成员：计划与治理数据的读写、Agent 对话与写确认
#   viewer 只读观察者：全量只读（可对话但不可确认写操作）
#   guest  外部访客：只读，且**仅能看到被指派的计划**，看不到知识库 / 记忆 / 审计

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLE_VIEWER = "viewer"
ROLE_GUEST = "guest"
#: 全部角色（顺序即权限由高到低，用于展示与校验）
ROLES: tuple[str, ...] = (ROLE_ADMIN, ROLE_MEMBER, ROLE_VIEWER, ROLE_GUEST)
#: 角色 → 中文名（前端展示；与 :data:`ROLES` 一一对应）
ROLE_LABELS: dict[str, str] = {
    ROLE_ADMIN: "管理员",
    ROLE_MEMBER: "成员",
    ROLE_VIEWER: "只读",
    ROLE_GUEST: "访客",
}

#: 权限标识（``资源:动作``）——后端唯一的判定依据
PERM_USER_MANAGE = "user:manage"
PERM_PLAN_READ = "plan:read"
PERM_PLAN_WRITE = "plan:write"
PERM_AGENT_CHAT = "agent:chat"
PERM_AGENT_WRITE = "agent:write"
PERM_KB_READ = "kb:read"
PERM_KB_WRITE = "kb:write"
PERM_MEMORY_READ = "memory:read"
PERM_MEMORY_WRITE = "memory:write"
PERM_AUDIT_READ = "audit:read"

#: 全部权限标识（测试与文档遍历用）
PERMISSIONS: tuple[str, ...] = (
    PERM_USER_MANAGE,
    PERM_PLAN_READ,
    PERM_PLAN_WRITE,
    PERM_AGENT_CHAT,
    PERM_AGENT_WRITE,
    PERM_KB_READ,
    PERM_KB_WRITE,
    PERM_MEMORY_READ,
    PERM_MEMORY_WRITE,
    PERM_AUDIT_READ,
)

#: 角色 → 权限集合（单一事实来源，新增角色 / 权限只改这里）
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_ADMIN: frozenset(PERMISSIONS),
    ROLE_MEMBER: frozenset(
        (
            PERM_PLAN_READ,
            PERM_PLAN_WRITE,
            PERM_AGENT_CHAT,
            PERM_AGENT_WRITE,
            PERM_KB_READ,
            PERM_KB_WRITE,
            PERM_MEMORY_READ,
            PERM_MEMORY_WRITE,
            PERM_AUDIT_READ,
        )
    ),
    ROLE_VIEWER: frozenset(
        (
            PERM_PLAN_READ,
            PERM_AGENT_CHAT,
            PERM_KB_READ,
            PERM_MEMORY_READ,
            PERM_AUDIT_READ,
        )
    ),
    ROLE_GUEST: frozenset((PERM_PLAN_READ,)),
}

#: 计划可见范围：全部计划 / 仅被指派的计划（guest）
PLAN_SCOPE_ALL = "all"
PLAN_SCOPE_ASSIGNED = "assigned"
#: 角色 → 计划可见范围
ROLE_PLAN_SCOPE: dict[str, str] = {
    ROLE_ADMIN: PLAN_SCOPE_ALL,
    ROLE_MEMBER: PLAN_SCOPE_ALL,
    ROLE_VIEWER: PLAN_SCOPE_ALL,
    ROLE_GUEST: PLAN_SCOPE_ASSIGNED,
}

#: 账号状态：正常 / 停用（停用后会话立即失效且无法登录）
USER_STATUS_ACTIVE = "active"
USER_STATUS_DISABLED = "disabled"
USER_STATUSES: tuple[str, ...] = (USER_STATUS_ACTIVE, USER_STATUS_DISABLED)

#: 用户名长度与字符集限制（同时用于登录与创建，避免大小写 / 空白导致的歧义）
USERNAME_MIN_LEN = 3
USERNAME_MAX_LEN = 64
#: 口令长度下限与上限（上限防止 scrypt 被超长输入拖慢）
PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 200
#: 显示名长度上限
DISPLAY_NAME_MAX_LEN = 64
#: 单个访客可被指派的计划数量上限
MAX_PLAN_ASSIGNMENTS = 200

# ------------------------------ 数据导入（/api/workspace/import） ------------------------------

#: 单次导入的最大行数（防止一次请求打爆引擎的重规划流水线）
IMPORT_MAX_ROWS = 500
#: 任务名称最大长度
IMPORT_NAME_MAX_LEN = 200
#: 导入模式：追加到现有计划 / 由导入行新建计划
IMPORT_MODE_APPEND = "append"
IMPORT_MODE_NEW = "new"
IMPORT_MODES: tuple[str, ...] = (IMPORT_MODE_APPEND, IMPORT_MODE_NEW)

# ------------------------------ 人工风险登记册 ------------------------------

#: 严重度取值
ISSUE_SEVERITIES: tuple[str, ...] = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
#: 发生概率取值
ISSUE_PROBABILITIES: tuple[str, ...] = ("LOW", "MEDIUM", "HIGH")
#: 处置状态取值
ISSUE_STATUSES: tuple[str, ...] = ("OPEN", "MITIGATING", "MONITORING", "CLOSED", "ACCEPTED")
#: 仍视为「未关闭」的状态（用于密度与未关闭计数）
ISSUE_OPEN_STATUSES: tuple[str, ...] = ("OPEN", "MITIGATING", "MONITORING")
#: 高关注严重度（用于质量看板的高危未关闭计数）
ISSUE_HIGH_SEVERITIES: tuple[str, ...] = ("HIGH", "CRITICAL")

# ------------------------------ 知识库 / 向量检索 ------------------------------

#: Chroma 集合名（分块正文 / 长期记忆各一，物理隔离避免互相污染）
KB_COLLECTION = "kb_chunks"
MEMORY_COLLECTION = "memories"

#: 支持解析的文档扩展名（其余后缀按纯文本尽力解析，二进制则明确报错）
KB_DOCUMENT_EXTENSIONS: tuple[str, ...] = (
    ".md",
    ".markdown",
    ".txt",
    ".csv",
    ".json",
    ".html",
    ".htm",
    ".pdf",
    ".docx",
)

#: 长期记忆类型（与前端 / 模型抽取口径一致）
MEMORY_TYPES: tuple[str, ...] = ("fact", "preference", "decision", "constraint", "task", "other")
#: 记忆类型 → 中文名（前端展示）
MEMORY_TYPE_LABELS: dict[str, str] = {
    "fact": "事实",
    "preference": "偏好",
    "decision": "决策",
    "constraint": "约束",
    "task": "任务",
    "other": "其他",
}
#: 记忆重要度取值（high 优先注入上下文）
MEMORY_IMPORTANCE: tuple[str, ...] = ("high", "medium", "low")
#: 记忆状态：启用（参与召回）/ 停用（保留但不参与）
MEMORY_STATUS_ACTIVE = "active"
MEMORY_STATUS_DISABLED = "disabled"
MEMORY_STATUSES: tuple[str, ...] = (MEMORY_STATUS_ACTIVE, MEMORY_STATUS_DISABLED)


def default_chroma_dir() -> str:
    """默认向量库持久化目录（可用环境变量 ``CHROMA_DIR`` 覆盖）。

    与工作区存储同放 ``.data`` 目录：运行期数据不进源码目录，且该目录已在
    ``.gitignore`` 中忽略，避免把向量库二进制误提交。
    """
    override = os.environ.get("CHROMA_DIR")
    if override:
        return override
    return str(Path(__file__).resolve().parent.parent / ".data" / "chroma")


# ------------------------------ 排期口径 ------------------------------

#: 单日标准工时（与引擎 ``src/constants/enums.ts`` 的 ``SCHEDULE.WORK_HOURS_PER_DAY`` 对齐）
WORK_HOURS_PER_DAY = 8

# ------------------------------ SSE 流式输出（/api/agent/chat/stream） ------------------------------

#: SSE 事件名（前后端共用契约，集中登记避免字符串散落）
SSE_META = "meta"  #: 会话 ID 与本轮检索增强上下文（知识库引用 / 长期记忆）
SSE_DELTA = "delta"  #: 助手文本增量（打字机式追加）
SSE_TOOL_START = "tool_start"  #: 开始调用某个工具
SSE_TOOL_RESULT = "tool_result"  #: 工具调用结果（只读工具即时执行；写工具为「待确认」）
SSE_PENDING = "pending"  #: 待用户确认的写操作清单
SSE_FINAL = "final"  #: 终态事件，携带完整结果（工具明细 / 计划 / 引用 / 记忆）
SSE_ERROR = "error"  #: 流式过程中的错误（已建立连接后以事件形式下发）
#: 内部标记：编排内部结果事件（不直接下发，由流式入口转换为 ``final``）
SSE_RESULT = "__result"

#: SSE 心跳间隔（秒）：空闲超过该时长即发送注释行，避免代理/网关因空闲断开长连接
SSE_HEARTBEAT_SECONDS = 15.0

# ------------------------ 异步作业（长耗时写链路） ------------------------

#: 作业 SSE 事件名（与 ``/api/jobs/{id}/stream`` 契约共用）
SSE_PROGRESS = "progress"  #: 阶段 / 进度推进（阶段名 + 人类可读说明）
SSE_DONE = "done"  #: 作业成功结束，携带 planId 等终态信息

#: 作业状态机：pending → running → succeeded | failed | cancelled
JOB_STATUS_PENDING = "pending"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
JOB_STATUSES: tuple[str, ...] = (
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
)
#: 终态集合：进入终态后不再接受取消等写操作（避免「已成功后又被标记取消」）
JOB_TERMINAL_STATUSES = frozenset({JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED})

#: 作业种类（决定审计标签与前端文案；集中登记避免字符串散落）
JOB_KIND_PLAN_CREATE = "plan.create"
JOB_KIND_PLAN_REPLAN = "plan.replan"
JOB_KINDS: tuple[str, ...] = (JOB_KIND_PLAN_CREATE, JOB_KIND_PLAN_REPLAN)

#: 作业阶段（进度事件的 ``phase``：前端据此显示进度文案，集中登记避免字符串散落）
JOB_PHASE_QUEUED = "queued"  #: 已受理，等待执行
JOB_PHASE_ENGINE = "engine"  #: 已提交算法引擎计算（本阶段最耗时）
JOB_PHASE_SETTLED = "settled"  #: 已收敛为终态
JOB_PHASES: tuple[str, ...] = (JOB_PHASE_QUEUED, JOB_PHASE_ENGINE, JOB_PHASE_SETTLED)

#: 异步请求约定首部（RFC 7240）：``Prefer: respond-async`` 表示「不要把连接挂到计算结束」
PREFER_HEADER = "prefer"
PREFER_RESPOND_ASYNC = "respond-async"

#: 幂等键首部（IETF draft-ietf-httpapi-idempotency-key-header-07 约定）
IDEMPOTENCY_KEY_HEADER = "idempotency-key"
#: 幂等键长度上限（字符）：过长的键会被当作存储滥用面，直接拒绝
IDEMPOTENCY_KEY_MAX_LEN = 255

#: 作业请求体上限（字节）：作业记录会整体序列化进状态存储，必须防「大 body × 多作业」
#: 把存储撑爆（全局 32 MiB 的 body 上限是给普通接口的，不适合当作状态载荷）
JOB_MAX_REQUEST_BYTES = 4 * 1024 * 1024
#: 作业结果体上限（字节）：超过则只保留状态码与 planId，客户端按 planId 自行拉取详情
JOB_MAX_RESULT_BYTES = 256 * 1024
#: 单个作业保留的进度条数上限（只保留最近 N 条，避免长任务把记录越写越大）
JOB_MAX_PROGRESS_ITEMS = 50

# ------------------------------ 工作区存储限额 ------------------------------

#: 单个计划可保存的基线快照数量上限
MAX_BASELINES_PER_PLAN = 50
#: 单个计划的人工风险登记册条目上限
MAX_ISSUES_PER_PLAN = 500
#: 配置备份（preferences）单个 profile 的最大字节数
MAX_PREFERENCE_BYTES = 256 * 1024
#: 配置备份默认 profile
DEFAULT_PROFILE = "default"
#: 工作区存储文件结构版本（后续结构升级时用于兼容判断）
WORKSPACE_STORE_VERSION = 1


def default_store_path() -> str:
    """默认工作区存储文件位置（可用环境变量 ``WORKSPACE_STORE_PATH`` 覆盖）。

    存放在 Python 包外的 ``.data`` 目录，避免把运行期数据混入源码目录。
    """
    override = os.environ.get("WORKSPACE_STORE_PATH")
    if override:
        return override
    return str(Path(__file__).resolve().parent.parent / ".data" / "workspace-store.json")


def default_auth_store_path() -> str:
    """默认账号 / 会话库位置（可用环境变量 ``AUTH_STORE_PATH`` 覆盖）。

    与工作区存储同放 ``.data`` 目录：运行期数据不进源码目录，且该目录已在
    ``.gitignore`` 中忽略，避免把账号口令散列误提交。
    """
    override = os.environ.get("AUTH_STORE_PATH")
    if override:
        return override
    return str(Path(__file__).resolve().parent.parent / ".data" / "auth.db")


__all__ = [
    "ROLE_ADMIN",
    "ROLE_MEMBER",
    "ROLE_VIEWER",
    "ROLE_GUEST",
    "ROLES",
    "ROLE_LABELS",
    "PERM_USER_MANAGE",
    "PERM_PLAN_READ",
    "PERM_PLAN_WRITE",
    "PERM_AGENT_CHAT",
    "PERM_AGENT_WRITE",
    "PERM_KB_READ",
    "PERM_KB_WRITE",
    "PERM_MEMORY_READ",
    "PERM_MEMORY_WRITE",
    "PERM_AUDIT_READ",
    "PERMISSIONS",
    "ROLE_PERMISSIONS",
    "PLAN_SCOPE_ALL",
    "PLAN_SCOPE_ASSIGNED",
    "ROLE_PLAN_SCOPE",
    "USER_STATUS_ACTIVE",
    "USER_STATUS_DISABLED",
    "USER_STATUSES",
    "USERNAME_MIN_LEN",
    "USERNAME_MAX_LEN",
    "PASSWORD_MIN_LEN",
    "PASSWORD_MAX_LEN",
    "DISPLAY_NAME_MAX_LEN",
    "MAX_PLAN_ASSIGNMENTS",
    "IMPORT_MAX_ROWS",
    "IMPORT_NAME_MAX_LEN",
    "IMPORT_MODE_APPEND",
    "IMPORT_MODE_NEW",
    "IMPORT_MODES",
    "KB_COLLECTION",
    "MEMORY_COLLECTION",
    "KB_DOCUMENT_EXTENSIONS",
    "MEMORY_TYPES",
    "MEMORY_TYPE_LABELS",
    "MEMORY_IMPORTANCE",
    "MEMORY_STATUS_ACTIVE",
    "MEMORY_STATUS_DISABLED",
    "MEMORY_STATUSES",
    "default_chroma_dir",
    "ISSUE_SEVERITIES",
    "ISSUE_PROBABILITIES",
    "ISSUE_STATUSES",
    "ISSUE_OPEN_STATUSES",
    "ISSUE_HIGH_SEVERITIES",
    "WORK_HOURS_PER_DAY",
    "MAX_BASELINES_PER_PLAN",
    "MAX_ISSUES_PER_PLAN",
    "MAX_PREFERENCE_BYTES",
    "DEFAULT_PROFILE",
    "WORKSPACE_STORE_VERSION",
    "SSE_META",
    "SSE_DELTA",
    "SSE_TOOL_START",
    "SSE_TOOL_RESULT",
    "SSE_PENDING",
    "SSE_FINAL",
    "SSE_ERROR",
    "SSE_RESULT",
    "SSE_HEARTBEAT_SECONDS",
    "SSE_PROGRESS",
    "SSE_DONE",
    "JOB_STATUS_PENDING",
    "JOB_STATUS_RUNNING",
    "JOB_STATUS_SUCCEEDED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_CANCELLED",
    "JOB_STATUSES",
    "JOB_TERMINAL_STATUSES",
    "JOB_KIND_PLAN_CREATE",
    "JOB_KIND_PLAN_REPLAN",
    "JOB_KINDS",
    "JOB_PHASE_QUEUED",
    "JOB_PHASE_ENGINE",
    "JOB_PHASE_SETTLED",
    "JOB_PHASES",
    "PREFER_HEADER",
    "PREFER_RESPOND_ASYNC",
    "IDEMPOTENCY_KEY_HEADER",
    "IDEMPOTENCY_KEY_MAX_LEN",
    "JOB_MAX_REQUEST_BYTES",
    "JOB_MAX_RESULT_BYTES",
    "JOB_MAX_PROGRESS_ITEMS",
    "default_store_path",
    "default_auth_store_path",
]
