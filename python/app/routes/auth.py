"""访问控制端点（``/api/auth/**``）。

能力：

- ``POST /auth/login``：账号口令登录，成功后下发 httpOnly 会话 Cookie；
- ``POST /auth/logout``：注销当前会话（立即吊销，无需等待过期）；
- ``GET  /auth/session``：查询当前主体（前端据此决定菜单 / 路由可见性）；
- ``GET  /auth/roles``：角色与权限矩阵（前端展示与排障用，**只读**）；
- ``POST /auth/password``：当前账号修改自身口令（校验原口令，改后吊销其它会话）；
- ``GET/POST/PATCH/DELETE /auth/users``：账号管理（**仅 admin**）；
- ``PUT  /auth/users/{id}/plans``：设置访客可见计划白名单（**仅 admin**）。

安全口径：

- 登录失败统一返回 ``401`` 且文案不区分「用户名不存在 / 口令错误」，避免账号枚举；
- 会话 Cookie 为 ``httpOnly``（JS 不可读，天然抵御 XSS 窃取）、``SameSite`` 默认 ``lax``
  （抵御跨站 CSRF；同源 SPA 的常规请求与 XHR 不受影响），生产 HTTPS 下应开启 ``secure``；
- 所有角色 / 权限判定都在服务端完成，前端拿到的仅是「能做什么」的清单。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth.permissions import build_principal
from ..auth.store import AuthStore, validate_password
from ..constants import (
    PERM_USER_MANAGE,
    ROLE_GUEST,
    ROLE_LABELS,
    ROLE_PERMISSIONS,
    ROLE_PLAN_SCOPE,
    ROLES,
)
from ..errors import bad_request, forbidden, unauthorized
from ..models import (
    ChangePasswordRequest,
    LoginRequest,
    PlanAssignmentsRequest,
    UserCreateRequest,
    UserUpdateRequest,
)
from ..state import AppContext

logger = logging.getLogger("iteration_planner.auth")


def _store(request: Request) -> AuthStore:
    ctx: AppContext = request.app.state.ctx
    store = getattr(ctx, "auth", None)
    if store is None:  # pragma: no cover - 仅在访问控制被显式关闭时出现
        raise bad_request("访问控制未启用（AUTH_ENABLED=false），账号接口不可用")
    return store


def _set_session_cookie(response: JSONResponse, ctx: AppContext, token: str) -> None:
    """写入会话 Cookie（httponly + SameSite + 可选 secure）。"""
    response.set_cookie(
        key=ctx.settings.auth_session_cookie,
        value=token,
        max_age=ctx.settings.auth_session_ttl_seconds,
        httponly=True,
        secure=ctx.settings.auth_cookie_secure,
        samesite=ctx.settings.resolved_cookie_samesite,
        path="/",
    )


def _clear_session_cookie(response: JSONResponse, ctx: AppContext) -> None:
    """清除会话 Cookie（属性须与写入时一致，否则部分浏览器不会真正删除）。"""
    response.delete_cookie(
        key=ctx.settings.auth_session_cookie,
        path="/",
        httponly=True,
        secure=ctx.settings.auth_cookie_secure,
        samesite=ctx.settings.resolved_cookie_samesite,
    )


def _require_admin(request: Request) -> None:
    """账号管理类端点的管理员断言（中间件已挡一层，此处为纵深防御）。"""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise unauthorized()
    if not principal.has(PERM_USER_MANAGE):
        raise forbidden("仅管理员可管理账号")


def build_auth_router(base: str) -> APIRouter:
    router = APIRouter(prefix=f"{base}/auth", tags=["auth"])

    @router.post("/login", summary="账号口令登录（下发 httpOnly 会话 Cookie）")
    async def login(payload: LoginRequest, request: Request) -> JSONResponse:
        ctx: AppContext = request.app.state.ctx
        store = _store(request)
        user = await store.verify_login(payload.username, payload.password)
        if user is None:
            logger.warning("登录失败：username=%s client=%s", payload.username, request.client)
            raise unauthorized("用户名或口令不正确")
        token = await store.create_session(user["userId"])
        await store.touch_login(user["userId"])
        principal = build_principal(
            user_id=user["userId"],
            username=user["username"],
            display_name=user["displayName"],
            role=user["role"],
            plan_ids=tuple(await store.plan_ids(user["userId"])),
        )
        response = JSONResponse(content={"data": principal.to_public()})
        _set_session_cookie(response, ctx, token)
        return response

    @router.post("/logout", summary="注销当前会话")
    async def logout(request: Request) -> JSONResponse:
        ctx: AppContext = request.app.state.ctx
        token = request.cookies.get(ctx.settings.auth_session_cookie, "")
        store = getattr(ctx, "auth", None)
        if store is not None and token:
            await store.revoke_session(token)
        response = JSONResponse(content={"data": {"ok": True}})
        _clear_session_cookie(response, ctx)
        return response

    @router.get("/session", summary="查询当前登录主体与权限清单")
    async def session(request: Request) -> dict:
        principal = getattr(request.state, "principal", None)
        if principal is None:
            raise unauthorized()
        return {"data": principal.to_public()}

    @router.get("/roles", summary="角色与权限矩阵（前端展示 / 排障用）")
    async def roles() -> dict:
        return {
            "data": {
                "roles": [
                    {
                        "role": role,
                        "label": ROLE_LABELS.get(role, role),
                        "permissions": sorted(ROLE_PERMISSIONS.get(role, frozenset())),
                        "planScope": ROLE_PLAN_SCOPE.get(role, "all"),
                    }
                    for role in ROLES
                ]
            }
        }

    @router.post("/password", summary="修改当前账号口令")
    async def change_password(payload: ChangePasswordRequest, request: Request) -> dict:
        principal = getattr(request.state, "principal", None)
        if principal is None or principal.machine:
            raise forbidden("机器令牌不支持修改口令")
        store = _store(request)
        verified = await store.verify_login(principal.username, payload.currentPassword)
        if verified is None:
            raise unauthorized("当前口令不正确")
        validate_password(payload.newPassword)
        await store.update_user(principal.user_id, password=payload.newPassword)
        return {"data": {"ok": True}}

    # ------------------------------ 账号管理（仅 admin） ------------------------------

    @router.get("/users", summary="账号清单（仅管理员）")
    async def list_users(request: Request) -> dict:
        _require_admin(request)
        users = await _store(request).list_users()
        return {"data": {"items": users, "total": len(users)}}

    @router.post("/users", summary="新建账号（仅管理员）")
    async def create_user(payload: UserCreateRequest, request: Request) -> dict:
        _require_admin(request)
        store = _store(request)
        user = await store.create_user(
            username=payload.username,
            password=payload.password,
            role=payload.role,
            display_name=payload.displayName or "",
        )
        return {"data": user}

    @router.patch("/users/{user_id}", summary="更新账号（仅管理员）")
    async def update_user(user_id: str, payload: UserUpdateRequest, request: Request) -> dict:
        _require_admin(request)
        if payload.role is None and payload.status is None and payload.displayName is None and payload.password is None:
            raise bad_request("未提供任何可更新字段")
        user = await _store(request).update_user(
            user_id,
            role=payload.role,
            status=payload.status,
            display_name=payload.displayName,
            password=payload.password,
        )
        return {"data": user}

    @router.delete("/users/{user_id}", summary="删除账号（仅管理员）")
    async def delete_user(user_id: str, request: Request) -> dict:
        _require_admin(request)
        principal = getattr(request.state, "principal", None)
        if principal is not None and principal.user_id == user_id:
            raise bad_request("不能删除当前登录账号")
        await _store(request).delete_user(user_id)
        return {"data": {"ok": True}}

    @router.put("/users/{user_id}/plans", summary="设置访客可见计划白名单（仅管理员）")
    async def set_plans(user_id: str, payload: PlanAssignmentsRequest, request: Request) -> dict:
        _require_admin(request)
        store = _store(request)
        user = await store.get_user(user_id)
        if user["role"] != ROLE_GUEST:
            raise bad_request("仅访客（guest）角色需要维护计划白名单")
        plan_ids = await store.set_plan_assignments(user_id, payload.planIds)
        return {"data": {"userId": user_id, "planIds": plan_ids}}

    return router


__all__ = ["build_auth_router"]
