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
#: 多角色协作编排的**步骤**事件：``{role, status, title, detail}``（中间过程可见 / 可中断）
SSE_STEP = "step"
#: 内部标记：编排内部结果事件（不直接下发，由流式入口转换为 ``final``）
SSE_RESULT = "__result"

#: SSE 心跳间隔（秒）：空闲超过该时长即发送注释行，避免代理/网关因空闲断开长连接
SSE_HEARTBEAT_SECONDS = 15.0

# ------------------------ 主动巡检（后台巡检 Agent） ------------------------
# 巡检只做「把引擎已经算出来的事实聚合 + 与上一次快照比对」，不引入任何新算法：
# 进度偏差来自任务状态与燃尽数据，关键路径漂移来自引擎的 criticalPath，
# 资源过载 / 依赖阻塞 / 交付风险来自引擎的 riskList（风险识别唯一事实来源）。

PATROL_SIGNAL_PROGRESS_DRIFT = "progress_drift"  #: 进度偏差（延期任务 / 燃尽落后）
PATROL_SIGNAL_CRITICAL_PATH_DRIFT = "critical_path_drift"  #: 关键路径漂移
PATROL_SIGNAL_RESOURCE_OVERLOAD = "resource_overload"  #: 人员过载
PATROL_SIGNAL_DEPENDENCY_BLOCKED = "dependency_blocked"  #: 依赖阻塞 / 孤立任务
PATROL_SIGNAL_DEADLINE_RISK = "deadline_risk"  #: 交付日期风险（计划完成晚于截止）
PATROL_SIGNALS: tuple[str, ...] = (
    PATROL_SIGNAL_PROGRESS_DRIFT,
    PATROL_SIGNAL_CRITICAL_PATH_DRIFT,
    PATROL_SIGNAL_RESOURCE_OVERLOAD,
    PATROL_SIGNAL_DEPENDENCY_BLOCKED,
    PATROL_SIGNAL_DEADLINE_RISK,
)
#: 巡检信号 → 中文名（前端展示，集中登记避免字符串散落）
PATROL_SIGNAL_LABELS: dict[str, str] = {
    PATROL_SIGNAL_PROGRESS_DRIFT: "进度偏差",
    PATROL_SIGNAL_CRITICAL_PATH_DRIFT: "关键路径漂移",
    PATROL_SIGNAL_RESOURCE_OVERLOAD: "人员过载",
    PATROL_SIGNAL_DEPENDENCY_BLOCKED: "依赖阻塞",
    PATROL_SIGNAL_DEADLINE_RISK: "交付风险",
}

PATROL_SEVERITY_INFO = "info"
PATROL_SEVERITY_WARNING = "warning"
PATROL_SEVERITY_CRITICAL = "critical"
PATROL_SEVERITIES: tuple[str, ...] = (PATROL_SEVERITY_INFO, PATROL_SEVERITY_WARNING, PATROL_SEVERITY_CRITICAL)
#: 严重度排序（用于取「本次巡检最高严重度」与前端排序）
PATROL_SEVERITY_RANK: dict[str, int] = {
    PATROL_SEVERITY_INFO: 0,
    PATROL_SEVERITY_WARNING: 1,
    PATROL_SEVERITY_CRITICAL: 2,
}

#: 巡检发现经 WS 推送的事件类型（与引擎事件 `plan.created|plan.updated` 并列）
WS_EVENT_PATROL_FINDING = "patrol.finding"
#: 巡检发现状态：新出现 / 仍存在 / 已恢复（仅「新出现」与「已恢复」推送，避免刷屏）
PATROL_FINDING_NEW = "new"
PATROL_FINDING_ONGOING = "ongoing"
PATROL_FINDING_RESOLVED = "resolved"

# ------------------------ 迭代复盘 + 学习 ------------------------

#: 复盘结论写回长期记忆时的来源标记（便于在记忆治理页按来源筛选）
RETRO_MEMORY_SOURCE = "retro"
#: 工时偏差校准分桶（按「实际/预估」比值分档，用于估算校准曲线）
RETRO_CALIBRATION_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<0.8", 0.0, 0.8),
    ("0.8-1.0", 0.8, 1.0),
    ("1.0-1.2", 1.0, 1.2),
    ("1.2-1.5", 1.2, 1.5),
    ("≥1.5", 1.5, float("inf")),
)


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


# ------------------------ MCP Server（把内部能力暴露为标准 MCP 工具） ------------------------
# 设计口径（对齐 MCP 2024-11-05 规范）：JSON-RPC 2.0 单一入口，``tools/list`` 声明能力、
# ``tools/call`` 触发执行；传输支持 **stdio**（本地进程集成）与 **Streamable HTTP**
# （远程集成，单端点 POST / JSON-RPC 或 SSE）。工具定义**直接复用** Agent 的 ``TOOL_SPECS``，
# 避免「MCP 一套、Agent 一套」两处口径漂移。

MCP_JSONRPC = "2.0"
MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_METHOD_INITIALIZE = "initialize"
MCP_METHOD_INITIALIZED = "notifications/initialized"
MCP_METHOD_TOOLS_LIST = "tools/list"
MCP_METHOD_TOOLS_CALL = "tools/call"
MCP_METHOD_PING = "ping"
#: JSON-RPC 标准错误码
MCP_ERR_PARSE = -32700
MCP_ERR_INVALID_REQUEST = -32600
MCP_ERR_METHOD_NOT_FOUND = -32601
MCP_ERR_INVALID_PARAMS = -32602
MCP_ERR_INTERNAL = -32603
#: 承载 MCP 会话的请求首部（Streamable HTTP 传输约定）
MCP_SESSION_HEADER = "mcp-session-id"
#: MCP 独有的写确认工具名：读工具即时执行，写工具登记待确认，再由这两个工具查看 / 落库
MCP_TOOL_LIST_PENDING = "list_pending_actions"
MCP_TOOL_CONFIRM_PENDING = "confirm_pending_actions"
#: MCP 会话 ID 前缀（与 Agent 会话隔离，避免两套会话键互相污染）
MCP_SESSION_PREFIX = "mcp-"

# ------------------------ 持久化检查点 + 断点续跑 ------------------------
# 「两阶段写确认」的第一阶段产物原本只活在会话存储里（进程重启即丢）。检查点把它升级为
# **持久化运行台账**：记录一次编排的请求、待确认动作与终态，支持进程重启后继续确认
# （断点续跑）与幂等重放。

CHECKPOINT_KIND_PLAN = "plan"  #: 端到端规划（/agent/plan）
CHECKPOINT_KIND_CHAT = "chat"  #: 会话式对话（/agent/chat）
CHECKPOINT_KINDS: tuple[str, ...] = (CHECKPOINT_KIND_PLAN, CHECKPOINT_KIND_CHAT)

CHECKPOINT_STATUS_RUNNING = "running"  #: 编排进行中（进程崩溃后会停留在此状态，可据此续跑）
CHECKPOINT_STATUS_AWAITING = "awaiting_approval"  #: 已登记待确认写操作，等待用户确认
CHECKPOINT_STATUS_EXECUTED = "executed"  #: 待确认动作已被确认落库
CHECKPOINT_STATUS_COMPLETED = "completed"  #: 续跑完成且**无需确认**（产出的是只读结论）
CHECKPOINT_STATUS_REJECTED = "rejected"  #: 用户驳回，未执行任何写操作
CHECKPOINT_STATUS_ABORTED = "aborted"  #: 主动作废
CHECKPOINT_STATUS_FAILED = "failed"  #: 编排失败（可续跑重试）
CHECKPOINT_STATUSES: tuple[str, ...] = (
    CHECKPOINT_STATUS_RUNNING,
    CHECKPOINT_STATUS_AWAITING,
    CHECKPOINT_STATUS_EXECUTED,
    CHECKPOINT_STATUS_COMPLETED,
    CHECKPOINT_STATUS_REJECTED,
    CHECKPOINT_STATUS_ABORTED,
    CHECKPOINT_STATUS_FAILED,
)
#: 终态集合：进入后不再接受续跑（续跑只会产生新的检查点）
CHECKPOINT_TERMINAL_STATUSES = frozenset(
    {
        CHECKPOINT_STATUS_EXECUTED,
        CHECKPOINT_STATUS_COMPLETED,
        CHECKPOINT_STATUS_REJECTED,
        CHECKPOINT_STATUS_ABORTED,
    }
)
#: 可续跑状态：待确认（继续确认落库）/ 运行中与失败（带原请求重跑）
CHECKPOINT_RESUMABLE_STATUSES = frozenset(
    {CHECKPOINT_STATUS_AWAITING, CHECKPOINT_STATUS_RUNNING, CHECKPOINT_STATUS_FAILED}
)
#: 键值存储前缀（与作业 / 幂等键物理隔离）
CHECKPOINT_PREFIX = "iteration_planner:checkpoint:"

# ------------------------ Agent Evals 评测台（回归门禁） ------------------------
# 双轨打分：**确定性断言**（工具链 / 是否有计划 / 是否登记待确认 / 是否谎报完成）是硬门槛，
# **LLM-as-judge** 只对自然语言结论质量给 0~1 分（模型不可用时仅确定性轨，仍可回归）。
# 任一确定性断言不过 → 该用例判失败，杜绝「用模型给自己打分放水」。

EVAL_OUTCOME_PASS = "pass"
EVAL_OUTCOME_FAIL = "fail"
EVAL_OUTCOME_ERROR = "error"  #: 用例执行本身异常（引擎 / 模型不可用），不计入通过率分子
EVAL_OUTCOMES: tuple[str, ...] = (EVAL_OUTCOME_PASS, EVAL_OUTCOME_FAIL, EVAL_OUTCOME_ERROR)

#: 综合分权重：确定性轨权重更高（模型评分只作补充）
EVAL_WEIGHT_DETERMINISTIC = 0.6
EVAL_WEIGHT_JUDGE = 0.4
#: LLM-as-judge 评分分档（用于把 0~1 连续分映射为可读等级）
EVAL_JUDGE_LEVELS: tuple[tuple[str, float], ...] = (
    ("excellent", 0.9),
    ("good", 0.75),
    ("fair", 0.5),
    ("poor", 0.0),
)

# ------------------------ 自反思自愈执行循环（reflect） ------------------------
# 传统 function calling 循环只会「把错误回填给模型」，是否纠偏、如何纠偏全凭模型心情。
# 本能力增加**结构化反思阶段**：对失败 / 空结果做确定性归因（分类 + 恢复策略），按
# 「同一错误签名」计数做有界重试，超限即熔断并降级为确定性说明，全过程产出可观测轨迹。

SSE_REFLECT = "reflect"  #: 反思事件（前端据此渲染「自我诊断 → 恢复动作」时间线）

#: 反思归因类别（确定性规则产出，不由模型臆造）
REFLECT_CATEGORY_INVALID_ARGS = "invalid_arguments"  #: 入参缺失 / 类型错（可换参重试）
REFLECT_CATEGORY_NOT_FOUND = "not_found"  #: 目标不存在（需先查真实 ID）
REFLECT_CATEGORY_CYCLE = "dependency_cycle"  #: 依赖成环（需改依赖结构）
REFLECT_CATEGORY_ENGINE = "engine_unavailable"  #: 引擎 / 依赖不可达（重试 + 降级）
REFLECT_CATEGORY_FORMAT = "format_error"  #: 模型输出格式错（回填纠偏）
REFLECT_CATEGORY_EMPTY = "empty_result"  #: 工具成功但结果为空（换工具 / 换参数）
REFLECT_CATEGORY_UNKNOWN = "unknown"
REFLECT_CATEGORIES: tuple[str, ...] = (
    REFLECT_CATEGORY_INVALID_ARGS,
    REFLECT_CATEGORY_NOT_FOUND,
    REFLECT_CATEGORY_CYCLE,
    REFLECT_CATEGORY_ENGINE,
    REFLECT_CATEGORY_FORMAT,
    REFLECT_CATEGORY_EMPTY,
    REFLECT_CATEGORY_UNKNOWN,
)
#: 恢复策略（决定回填给模型的纠偏指令形态）
REFLECT_STRATEGY_RETRY = "retry_with_args"  #: 换 / 补参数后重试同一工具
REFLECT_STRATEGY_REROUTE = "switch_tool"  #: 改调其它工具（如先 get_plan 再 update）
REFLECT_STRATEGY_DEGRADE = "degrade"  #: 熔断：不再重试，给出确定性降级说明
REFLECT_STRATEGY_ASK = "ask_user"  #: 信息不足，向用户澄清
REFLECT_STRATEGIES: tuple[str, ...] = (
    REFLECT_STRATEGY_RETRY,
    REFLECT_STRATEGY_REROUTE,
    REFLECT_STRATEGY_DEGRADE,
    REFLECT_STRATEGY_ASK,
)


# ------------------------ Agent 执行追踪与回放（trace） ------------------------
# 排障与评测的共同底座：把「一次运行到底发生了什么」落成**有结构、带耗时的 span**
# （LLM 每一步的请求摘要 / 响应摘要、工具入参出参、反思事件、一致性护栏改写），
# 前端可像调试器一样看瀑布图，并可逐 step 回放历史运行（事故复盘 / 评测失败归因）。

TRACE_KIND_LLM = "llm"  #: 一次模型调用（请求 / 响应摘要 + 耗时）
TRACE_KIND_TOOL = "tool"  #: 一次工具调用（入参 / 出参摘要 + 错误码）
TRACE_KIND_REFLECT = "reflect"  #: 一次自反思事件（归因 + 恢复策略）
TRACE_KIND_GUARD = "guard"  #: 一致性护栏动作（谎报完成 / 无依据状态作答 / 写指令空转 / 输出改写）
TRACE_KIND_CONTEXT = "context"  #: 上下文预算动作（分区裁剪 / 旧轮次压缩）——是常态运维事件，不计入「事故信号」
TRACE_KIND_FINAL = "final"  #: 收敛收尾（终态文本与产出）
TRACE_KINDS: tuple[str, ...] = (
    TRACE_KIND_LLM,
    TRACE_KIND_TOOL,
    TRACE_KIND_REFLECT,
    TRACE_KIND_GUARD,
    TRACE_KIND_CONTEXT,
    TRACE_KIND_FINAL,
)

TRACE_STATUS_OK = "ok"
TRACE_STATUS_ERROR = "error"  #: 该 span 失败（工具报错 / 编排异常）
TRACE_STATUS_DEFERRED = "deferred"  #: 写工具仅登记为待确认，尚未落库
TRACE_STATUS_DEGRADED = "degraded"  #: 降级 / 未解决（如纠正后仍未取得工具支撑）
TRACE_STATUSES: tuple[str, ...] = (
    TRACE_STATUS_OK,
    TRACE_STATUS_ERROR,
    TRACE_STATUS_DEFERRED,
    TRACE_STATUS_DEGRADED,
)

#: 轨迹级终态（与 span 级状态区分：一条轨迹可以「收尾为降级」但内部没有失败 span）
TRACE_RUN_OK = "ok"  #: 正常收敛
TRACE_RUN_PENDING = "pending"  #: 收敛为「待用户确认」（登记了写操作，尚未落库）
TRACE_RUN_DEGRADED = "degraded"  #: 收敛但存在未解决信号（护栏拦截 / 降级说明 / 反思熔断）
TRACE_RUN_ERROR = "error"  #: 编排失败（异常终止）
TRACE_RUN_STATUSES: tuple[str, ...] = (TRACE_RUN_OK, TRACE_RUN_PENDING, TRACE_RUN_DEGRADED, TRACE_RUN_ERROR)

TRACE_PREFIX = "iteration_planner:trace:"
#: 单个 trace 保留的 span 上限：长循环 + 多工具时收敛，避免轨迹体无界增长
MAX_TRACE_SPANS = 200
#: 单主体索引保留条数上限（索引本身不能无限增长）
MAX_TRACE_INDEX = 100
#: span 入参 / 出参摘要中单个字符串的最大长度（回溯看结论，不看全量载荷）
MAX_TRACE_TEXT_CHARS = 600
#: 评测运行产生的轨迹归属主体：评测失败用例要能「一键跳回放」，因此这部分轨迹对
#: 具备 agent:chat 的读者共享（内容只是黄金集输入 + 操作者自己批准入库的回归输入）
TRACE_OWNER_EVALS = "evals"

# ------------------------ 事故 → 回归用例闭环（incident → case） ------------------------
# 一次线上事故的**失败轨迹**应该自动变成一条回归用例，而不是靠人手工补：
# 断言由轨迹**确定性推导**（哪个工具失败了 → 要求不再失败；哪条护栏被触发 → 要求不再
# 触发），用例持久化后随即被评测台与 CI 门禁消费，形成「事故 → 用例 → 门禁」闭环。

CUSTOM_CASE_PREFIX = "iteration_planner:evals:case:"
CUSTOM_CASE_ORIGIN_INCIDENT = "incident"  #: 由失败轨迹确定性推导
CUSTOM_CASE_ORIGIN_MANUAL = "manual"  #: 人工登记（用于补充轨迹无法覆盖的场景）
CUSTOM_CASE_ORIGINS: tuple[str, ...] = (CUSTOM_CASE_ORIGIN_INCIDENT, CUSTOM_CASE_ORIGIN_MANUAL)
#: 自定义用例全局索引键（黄金集之外的增量用例）
CUSTOM_CASE_INDEX = "index:custom-evals"
MAX_CUSTOM_CASES = 200
#: 由轨迹推导出的用例 ID 前缀（便于与黄金集区分）
INCIDENT_CASE_ID_PREFIX = "inc-"

# ------------------------ 输出溯源与置信度标注（provenance） ------------------------
# 模型会写「总工期 42 天」这类**事实性数字**，用户无从判断它来自引擎事实还是模型臆造。
# 本能力把工具返回值、知识库片段、长期记忆摊平成**证据原子**，逐条把结论中的数字声明
# 绑定到来源；未匹配到任何证据的数字声明标记为「未核实」并计数（诚实暴露不确定性）。

PROVENANCE_STATUS_VERIFIED = "verified"
PROVENANCE_STATUS_UNVERIFIED = "unverified"
PROVENANCE_STATUSES: tuple[str, ...] = (PROVENANCE_STATUS_VERIFIED, PROVENANCE_STATUS_UNVERIFIED)
PROVENANCE_SOURCE_TOOL = "tool"  #: 证据来自工具返回值（含 JSON 字段路径）
PROVENANCE_SOURCE_KB = "kb"  #: 证据来自知识库片段
PROVENANCE_SOURCE_MEMORY = "memory"  #: 证据来自长期记忆
PROVENANCE_SOURCES: tuple[str, ...] = (
    PROVENANCE_SOURCE_TOOL,
    PROVENANCE_SOURCE_KB,
    PROVENANCE_SOURCE_MEMORY,
)
#: 证据原子抽取上限（防止整份计划被摊平成上万条原子）
MAX_EVIDENCE_ATOMS = 400
#: 单条证据原子取值的文本上限
MAX_EVIDENCE_VALUE_CHARS = 160
#: 单条声明最多回填的引用数（前端气泡内联展示不宜过密）
MAX_CLAIM_CITATIONS = 4
#: 数字匹配容差：浮点工期经常以「40」对「40.0」出现，按相对误差比对
PROVENANCE_NUMBER_TOLERANCE = 0.01

# ------------------------ 动态上下文预算管理（budget） ------------------------
# 朴素做法是「超预算就从最旧的用户回合开始删」——它会先把「待确认动作」「上一次失败」
# 这类**决定本轮正确性**的内容删掉。本能力改为**分区预算**：先固定占位、再分配弹性区，
# 并回传每个分区的用量明细，让「上下文为什么被裁掉」可解释、可在前端实时观测。

SSE_BUDGET = "budget"  #: 分区预算明细（每步下发，前端渲染预算条）

BUDGET_ZONE_SYSTEM = "system"  #: 系统提示词（固定，永不裁剪）
BUDGET_ZONE_KEEP = "keep"  #: 必保内容：纠偏提示 / 待确认动作结果 / 最近一次失败
BUDGET_ZONE_CURRENT = "current"  #: 本轮用户输入（固定，永不裁剪）
BUDGET_ZONE_RECALL = "recall"  #: 检索增强区块（记忆 / 知识库）：按剩余额度截断
BUDGET_ZONE_HISTORY = "history"  #: 历史用户回合：按衰减压缩
BUDGET_ZONES: tuple[str, ...] = (
    BUDGET_ZONE_SYSTEM,
    BUDGET_ZONE_KEEP,
    BUDGET_ZONE_CURRENT,
    BUDGET_ZONE_RECALL,
    BUDGET_ZONE_HISTORY,
)
BUDGET_ZONE_LABELS: dict[str, str] = {
    BUDGET_ZONE_SYSTEM: "系统提示",
    BUDGET_ZONE_KEEP: "必保上下文",
    BUDGET_ZONE_CURRENT: "本轮输入",
    BUDGET_ZONE_RECALL: "检索增强",
    BUDGET_ZONE_HISTORY: "历史回合",
}
#: 历史回合保留完整内容的最近轮数（更旧的回合被压缩为一行摘要）
BUDGET_KEEP_RECENT_TURNS = 3
#: 压缩摘要单行长度上限
BUDGET_SUMMARY_CHARS = 200


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
    "SSE_STEP",
    "SSE_HEARTBEAT_SECONDS",
    "PATROL_SIGNAL_PROGRESS_DRIFT",
    "PATROL_SIGNAL_CRITICAL_PATH_DRIFT",
    "PATROL_SIGNAL_RESOURCE_OVERLOAD",
    "PATROL_SIGNAL_DEPENDENCY_BLOCKED",
    "PATROL_SIGNAL_DEADLINE_RISK",
    "PATROL_SIGNALS",
    "PATROL_SIGNAL_LABELS",
    "PATROL_SEVERITY_INFO",
    "PATROL_SEVERITY_WARNING",
    "PATROL_SEVERITY_CRITICAL",
    "PATROL_SEVERITIES",
    "PATROL_SEVERITY_RANK",
    "WS_EVENT_PATROL_FINDING",
    "PATROL_FINDING_NEW",
    "PATROL_FINDING_ONGOING",
    "PATROL_FINDING_RESOLVED",
    "RETRO_MEMORY_SOURCE",
    "RETRO_CALIBRATION_BUCKETS",
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
    "MCP_JSONRPC",
    "MCP_PROTOCOL_VERSION",
    "MCP_METHOD_INITIALIZE",
    "MCP_METHOD_INITIALIZED",
    "MCP_METHOD_TOOLS_LIST",
    "MCP_METHOD_TOOLS_CALL",
    "MCP_METHOD_PING",
    "MCP_ERR_PARSE",
    "MCP_ERR_INVALID_REQUEST",
    "MCP_ERR_METHOD_NOT_FOUND",
    "MCP_ERR_INVALID_PARAMS",
    "MCP_ERR_INTERNAL",
    "MCP_SESSION_HEADER",
    "MCP_TOOL_LIST_PENDING",
    "MCP_TOOL_CONFIRM_PENDING",
    "MCP_SESSION_PREFIX",
    "CHECKPOINT_KIND_PLAN",
    "CHECKPOINT_KIND_CHAT",
    "CHECKPOINT_KINDS",
    "CHECKPOINT_STATUS_RUNNING",
    "CHECKPOINT_STATUS_AWAITING",
    "CHECKPOINT_STATUS_EXECUTED",
    "CHECKPOINT_STATUS_COMPLETED",
    "CHECKPOINT_STATUS_REJECTED",
    "CHECKPOINT_STATUS_ABORTED",
    "CHECKPOINT_STATUS_FAILED",
    "CHECKPOINT_STATUSES",
    "CHECKPOINT_TERMINAL_STATUSES",
    "CHECKPOINT_RESUMABLE_STATUSES",
    "CHECKPOINT_PREFIX",
    "EVAL_OUTCOME_PASS",
    "EVAL_OUTCOME_FAIL",
    "EVAL_OUTCOME_ERROR",
    "EVAL_OUTCOMES",
    "EVAL_WEIGHT_DETERMINISTIC",
    "EVAL_WEIGHT_JUDGE",
    "EVAL_JUDGE_LEVELS",
    "SSE_REFLECT",
    "REFLECT_CATEGORY_INVALID_ARGS",
    "REFLECT_CATEGORY_NOT_FOUND",
    "REFLECT_CATEGORY_CYCLE",
    "REFLECT_CATEGORY_ENGINE",
    "REFLECT_CATEGORY_FORMAT",
    "REFLECT_CATEGORY_EMPTY",
    "REFLECT_CATEGORY_UNKNOWN",
    "REFLECT_CATEGORIES",
    "REFLECT_STRATEGY_RETRY",
    "REFLECT_STRATEGY_REROUTE",
    "REFLECT_STRATEGY_DEGRADE",
    "REFLECT_STRATEGY_ASK",
    "REFLECT_STRATEGIES",
    "TRACE_KIND_LLM",
    "TRACE_KIND_TOOL",
    "TRACE_KIND_REFLECT",
    "TRACE_KIND_GUARD",
    "TRACE_KIND_CONTEXT",
    "TRACE_KIND_FINAL",
    "TRACE_KINDS",
    "TRACE_STATUS_OK",
    "TRACE_STATUS_ERROR",
    "TRACE_STATUS_DEFERRED",
    "TRACE_STATUS_DEGRADED",
    "TRACE_STATUSES",
    "TRACE_RUN_OK",
    "TRACE_RUN_PENDING",
    "TRACE_RUN_DEGRADED",
    "TRACE_RUN_ERROR",
    "TRACE_RUN_STATUSES",
    "TRACE_PREFIX",
    "MAX_TRACE_SPANS",
    "MAX_TRACE_INDEX",
    "MAX_TRACE_TEXT_CHARS",
    "TRACE_OWNER_EVALS",
    "CUSTOM_CASE_PREFIX",
    "CUSTOM_CASE_ORIGIN_INCIDENT",
    "CUSTOM_CASE_ORIGIN_MANUAL",
    "CUSTOM_CASE_ORIGINS",
    "CUSTOM_CASE_INDEX",
    "MAX_CUSTOM_CASES",
    "INCIDENT_CASE_ID_PREFIX",
    "PROVENANCE_STATUS_VERIFIED",
    "PROVENANCE_STATUS_UNVERIFIED",
    "PROVENANCE_STATUSES",
    "PROVENANCE_SOURCE_TOOL",
    "PROVENANCE_SOURCE_KB",
    "PROVENANCE_SOURCE_MEMORY",
    "PROVENANCE_SOURCES",
    "MAX_EVIDENCE_ATOMS",
    "MAX_EVIDENCE_VALUE_CHARS",
    "MAX_CLAIM_CITATIONS",
    "PROVENANCE_NUMBER_TOLERANCE",
    "SSE_BUDGET",
    "BUDGET_ZONE_SYSTEM",
    "BUDGET_ZONE_KEEP",
    "BUDGET_ZONE_CURRENT",
    "BUDGET_ZONE_RECALL",
    "BUDGET_ZONE_HISTORY",
    "BUDGET_ZONES",
    "BUDGET_ZONE_LABELS",
    "BUDGET_KEEP_RECENT_TURNS",
    "BUDGET_SUMMARY_CHARS",
    "default_store_path",
    "default_auth_store_path",
]
