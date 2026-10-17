"""访问控制（认证 + 授权 + 账号存储）。

对外职责：
- :mod:`app.auth.store`：账号 / 会话 / 计划指派的持久化（sqlite3）；
- :mod:`app.auth.permissions`：角色 → 权限、请求路径 → 所需权限的单一映射；
- :mod:`app.auth.guard`：FastAPI 依赖（取当前主体 / 断言权限）。

**所有授权判定都在服务端完成**，前端仅依据 ``/api/auth/session`` 返回的权限清单做展示收敛。
"""

from .guard import current_principal, require_permission
from .permissions import (
    MACHINE_USER_ID,
    PUBLIC_ENDPOINTS,
    Principal,
    build_principal,
    is_public_endpoint,
    machine_principal,
    permissions_for_role,
    plan_id_from_path,
    required_permission,
)
from .store import AuthStore

__all__ = [
    "AuthStore",
    "MACHINE_USER_ID",
    "PUBLIC_ENDPOINTS",
    "Principal",
    "build_principal",
    "current_principal",
    "is_public_endpoint",
    "machine_principal",
    "permissions_for_role",
    "plan_id_from_path",
    "require_permission",
    "required_permission",
]
