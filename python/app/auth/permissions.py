"""访问控制主体与「角色 / 请求路径 → 权限」解析。

本模块是**权限判定的唯一入口**：角色到权限的映射来自 :mod:`app.constants`
（单一事实来源），请求路径到所需权限的映射集中在此处的规则表里，避免权限散落在各路由。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..constants import (
    PERM_AGENT_CHAT,
    PERM_AGENT_WRITE,
    PERM_AUDIT_READ,
    PERM_KB_READ,
    PERM_KB_WRITE,
    PERM_MEMORY_READ,
    PERM_MEMORY_WRITE,
    PERM_PLAN_READ,
    PERM_PLAN_WRITE,
    PERM_USER_MANAGE,
    PLAN_SCOPE_ALL,
    PLAN_SCOPE_ASSIGNED,
    ROLE_ADMIN,
    ROLE_PERMISSIONS,
    ROLE_PLAN_SCOPE,
)

#: 计划作用域路径：``/plans/<planId>`` 与 ``/workspace/plans/<planId>``（相对 API 前缀）
_PLAN_SCOPED = re.compile(r"^/(?:plans|workspace/plans)/(?P<planId>[^/]+)(?:/|$)")

#: 机器令牌主体（``API_TOKEN``）：视为内部服务账号，拥有全部权限但无用户身份
MACHINE_USER_ID = "machine"

#: 免鉴权端点（``(方法, 相对路径)``）：**仅登录与登出**。
#: 二者不接收业务载荷——登录以口令校验自身作为访问控制，登出对未登录请求幂等，
#: 若在此处强制鉴权会导致「未登录就永远无法登录」的死锁。
PUBLIC_ENDPOINTS: frozenset[tuple[str, str]] = frozenset(
    {("POST", "/auth/login"), ("POST", "/auth/logout")}
)


@dataclass(frozen=True)
class Principal:
    """已通过鉴权的访问主体（人类用户或机器令牌）。"""

    user_id: str
    username: str
    display_name: str
    role: str
    #: guest 可见的计划白名单（其余角色为空，表示不受限）
    plan_ids: tuple[str, ...] = ()
    #: 是否由 ``API_TOKEN``（机器 / CI）鉴权而来
    machine: bool = False
    #: 权限集合在构造时冻结，避免每次判定都重算
    permissions: frozenset[str] = field(default_factory=frozenset)

    @property
    def plan_scope(self) -> str:
        """计划可见范围：``all``（全部）或 ``assigned``（仅白名单）。"""
        return ROLE_PLAN_SCOPE.get(self.role, PLAN_SCOPE_ALL)

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def can_see_plan(self, plan_id: str) -> bool:
        """该主体是否有权访问指定计划（guest 仅限被指派计划）。"""
        if self.plan_scope != PLAN_SCOPE_ASSIGNED:
            return True
        return plan_id in self.plan_ids

    def to_public(self) -> dict[str, object]:
        """对外（前端）暴露的主体信息：**绝不包含口令散列等敏感字段**。"""
        return {
            "userId": self.user_id,
            "username": self.username,
            "displayName": self.display_name,
            "role": self.role,
            "permissions": sorted(self.permissions),
            "planScope": self.plan_scope,
            "planIds": list(self.plan_ids),
            "machine": self.machine,
        }


def permissions_for_role(role: str) -> frozenset[str]:
    """角色 → 权限集合（未知角色按最小权限处理）。"""
    return ROLE_PERMISSIONS.get(role, frozenset())


def build_principal(
    *,
    user_id: str,
    username: str,
    display_name: str,
    role: str,
    plan_ids: tuple[str, ...] = (),
    machine: bool = False,
) -> Principal:
    """按角色装配主体（权限集合在此一次性算好）。"""
    return Principal(
        user_id=user_id,
        username=username,
        display_name=display_name,
        role=role,
        plan_ids=tuple(plan_ids),
        machine=machine,
        permissions=permissions_for_role(role),
    )


def machine_principal() -> Principal:
    """``API_TOKEN`` 对应主体：内部服务账号（等价管理员，但无人类身份）。"""
    return build_principal(
        user_id=MACHINE_USER_ID,
        username=MACHINE_USER_ID,
        display_name="机器令牌",
        role=ROLE_ADMIN,
        machine=True,
    )


def is_public_endpoint(method: str, path: str, base: str) -> bool:
    """是否为免鉴权端点（见 :data:`PUBLIC_ENDPOINTS`）。"""
    if not path.startswith(base):
        return False
    rel = path[len(base) :] or "/"
    return ((method or "GET").upper(), rel) in PUBLIC_ENDPOINTS


def required_permission(method: str, path: str, base: str) -> str | None:
    """把「HTTP 方法 + 请求路径」映射为所需权限。

    :return: 权限标识；``None`` 表示「仅需通过身份鉴权」（如登录 / 会话查询端点，
        其内部再自行判定权限）。
    """
    if not path.startswith(base):
        return None
    rel = path[len(base) :] or "/"
    verb = (method or "GET").upper()

    # 认证端点：登录 / 登出 / 会话查询对未登录开放；用户管理仅管理员可达
    if rel == "/auth/users" or rel.startswith("/auth/users/"):
        return PERM_USER_MANAGE
    if rel.startswith("/auth/"):
        return None
    if rel.startswith("/meta/"):
        return PERM_PLAN_READ
    if rel == "/audit" or rel.startswith("/audit/"):
        return PERM_AUDIT_READ
    if rel.startswith("/agent/"):
        # 写确认属于「真正落库」的动作，单独用 agent:write 区分于只读对话
        if rel.startswith("/agent/confirm"):
            return PERM_AGENT_WRITE
        # 多角色协作会**持久化**一份真实计划（不是临时仿真），按写权限收紧
        if rel.startswith("/agent/collab"):
            return PERM_AGENT_WRITE
        # 主动巡检只读（不改数据），按计划读权限放行
        if rel.startswith("/agent/patrol"):
            return PERM_PLAN_READ
        # What-if 沙盘 / 复盘都是「读计划 + 派生结论」：沙盘的临时计划用完即回收，
        # 与既有 /scenario/compare 同一性质，因此与对话同级（agent:chat）
        return PERM_AGENT_CHAT
    if rel == "/jobs" or rel.startswith("/jobs/"):
        # 作业查询 / 订阅进度是只读；取消作业等价于「放弃这次写」，按写权限收紧
        return PERM_PLAN_READ if verb == "GET" else PERM_PLAN_WRITE
    if rel.startswith("/scenario/"):
        return PERM_PLAN_READ
    if rel == "/kb" or rel.startswith("/kb/"):
        # 检索是只读动作，虽为 POST 也不应要求写权限
        return PERM_KB_READ if verb == "GET" or rel == "/kb/search" else PERM_KB_WRITE
    if rel == "/memories" or rel.startswith("/memories/"):
        return PERM_MEMORY_READ if verb == "GET" or rel == "/memories/search" else PERM_MEMORY_WRITE
    if rel.startswith("/workspace/"):
        if rel == "/workspace/import":
            return PERM_PLAN_WRITE
        if rel == "/workspace/preferences":
            return PERM_PLAN_READ if verb == "GET" else PERM_PLAN_WRITE
        if rel.startswith("/workspace/plans/"):
            return PERM_PLAN_READ if verb == "GET" else PERM_PLAN_WRITE
        return PERM_PLAN_READ
    if rel == "/plans" or rel.startswith("/plans/"):
        # 拓扑校验是只读检查（虽为 POST），按读权限放行
        if rel.endswith("/dag-check"):
            return PERM_PLAN_READ
        return PERM_PLAN_READ if verb == "GET" else PERM_PLAN_WRITE
    # 未登记路径：保守按「计划只读」处理（仍须通过身份鉴权）
    return PERM_PLAN_READ


def plan_id_from_path(path: str, base: str) -> str | None:
    """从计划作用域路径中提取 ``planId``（非计划作用域路径返回 ``None``）。"""
    if not path.startswith(base):
        return None
    rel = path[len(base) :] or "/"
    match = _PLAN_SCOPED.match(rel)
    if not match:
        return None
    return match.group("planId")


__all__ = [
    "MACHINE_USER_ID",
    "PUBLIC_ENDPOINTS",
    "Principal",
    "build_principal",
    "is_public_endpoint",
    "machine_principal",
    "permissions_for_role",
    "plan_id_from_path",
    "required_permission",
]
