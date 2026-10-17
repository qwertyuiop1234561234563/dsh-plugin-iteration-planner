"""FastAPI 依赖：读取当前访问主体、按需断言权限。

鉴权中间件已把主体挂到 ``request.state.principal``（见 :func:`app.main._register_auth`）；
本模块的依赖只做「取用 + 断言」，避免各路由重复实现权限判断。
"""

from __future__ import annotations

from fastapi import Request

from ..errors import forbidden, unauthorized
from .permissions import Principal


def current_principal(request: Request) -> Principal:
    """取当前主体；缺失（未通过身份鉴权）即 401。"""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise unauthorized()
    return principal


def require_permission(permission: str):
    """生成一个断言指定权限的依赖（不满足即 403）。"""

    def _dependency(request: Request) -> Principal:
        principal = current_principal(request)
        if not principal.has(permission):
            raise forbidden(f"当前角色（{principal.role}）无「{permission}」权限")
        return principal

    return _dependency


__all__ = ["current_principal", "require_permission"]
