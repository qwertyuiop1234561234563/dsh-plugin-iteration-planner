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


__all__ = [
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
    "default_store_path",
]
