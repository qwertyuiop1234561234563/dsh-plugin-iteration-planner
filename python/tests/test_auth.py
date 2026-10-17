"""访问控制测试：口令散列 / 权限矩阵 / 账号与会话存储 / HTTP 与 WS 鉴权 / 访客计划作用域。

覆盖「登录 + 角色区分」的端到端行为，并重点回归几处容易出错的边界：

- 最后一个启用管理员不得被降权 / 停用 / 删除（否则控制台永久失去管理入口）；
- 登录失败不区分「账号不存在 / 口令错误」（防账号枚举）；
- 改角色 / 停用 / 改口令后既有会话立即失效（防「降权后仍持旧权限」）；
- 访客越权访问计划返回 **404** 而非 403（不泄露计划是否存在）；
- 访客看计划**列表**时必须按白名单过滤（列表不在计划作用域路径上，仅靠中间件拦不住）。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth import (
    MACHINE_USER_ID,
    AuthStore,
    build_principal,
    is_public_endpoint,
    machine_principal,
    permissions_for_role,
    plan_id_from_path,
    required_permission,
)
from app.auth.passwords import hash_password, verify_password
from app.auth.store import _random_password, validate_password, validate_role, validate_username
from app.constants import (
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
    PLAN_SCOPE_ASSIGNED,
    ROLE_ADMIN,
    ROLE_GUEST,
    ROLE_MEMBER,
    ROLE_VIEWER,
    ROLES,
)
from app.errors import ApiError
from app.ws import WsBroadcaster
from tests.conftest import BOOTSTRAP_PASSWORD, BOOTSTRAP_USERNAME, auth, login

SESSION_COOKIE = "ip_session"
BASE = "/api"


# ------------------------------- 口令散列 -------------------------------


def test_password_hash_is_salted_and_verifiable():
    first = hash_password("Secret12345")
    second = hash_password("Secret12345")
    assert first != second  # 每次调用新盐
    assert first.startswith("scrypt$")
    assert verify_password("Secret12345", first) is True
    assert verify_password("Secret12345", second) is True
    assert verify_password("Secret12346", first) is False


def test_password_verify_rejects_malformed_encoding():
    assert verify_password("x", "") is False
    assert verify_password("x", "plain-text") is False
    assert verify_password("x", "scrypt$notanint$8$1$AAAA$BBBB") is False
    assert verify_password("x", "bcrypt$16384$8$1$AAAA$BBBB") is False


def test_random_bootstrap_password_always_satisfies_policy():
    """随机引导口令必须**每次都**过校验。

    历史实现用 ``secrets.token_urlsafe(12)``：其 16 个字符全落在字母上的概率约 6.6%，
    此时不含数字、被 ``validate_password`` 拒绝，导致「首次启动」在约十五分之一的部署上
    直接失败（且是启动期异常，排查成本高）。这里以 300 次抽样回归该缺陷。
    """
    for _ in range(300):
        candidate = _random_password()
        assert validate_password(candidate) == candidate
        assert any(ch.isdigit() for ch in candidate) and any(ch.isalpha() for ch in candidate)
    assert _random_password(length=8) != _random_password(length=8)


async def test_bootstrap_generates_password_when_config_absent():
    store = AuthStore(":memory:")
    await store.start()
    try:
        generated = await store.ensure_bootstrap_admin(BOOTSTRAP_USERNAME, "")
        assert generated is not None
        assert validate_password(generated) == generated
        # 打印出的口令必须真能登录（否则运维拿到的是一次性废码）
        assert await store.verify_login(BOOTSTRAP_USERNAME, generated) is not None
    finally:
        await store.close()


async def test_bootstrap_rejects_weak_configured_password():
    """运维显式配置的弱口令须被拒绝，不能引导出一个不满足策略的管理员。"""
    store = AuthStore(":memory:")
    await store.start()
    try:
        with pytest.raises(ApiError):
            await store.ensure_bootstrap_admin(BOOTSTRAP_USERNAME, "allletters")
        assert await store.count_admins() == 0
    finally:
        await store.close()


# ------------------------------- 权限矩阵与路径映射 -------------------------------


def test_role_permission_matrix_is_least_privilege():
    assert permissions_for_role(ROLE_ADMIN) == frozenset(
        {
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
        }
    )
    # 成员可读写但不得管理账号
    assert PERM_USER_MANAGE not in permissions_for_role(ROLE_MEMBER)
    assert PERM_PLAN_WRITE in permissions_for_role(ROLE_MEMBER)
    # 只读角色：可读、可对话，但不得写入任何业务数据
    viewer = permissions_for_role(ROLE_VIEWER)
    assert PERM_PLAN_READ in viewer and PERM_AGENT_CHAT in viewer
    assert PERM_PLAN_WRITE not in viewer and PERM_AGENT_WRITE not in viewer
    assert PERM_KB_WRITE not in viewer and PERM_MEMORY_WRITE not in viewer
    # 访客：仅计划只读
    assert permissions_for_role(ROLE_GUEST) == frozenset({PERM_PLAN_READ})
    assert permissions_for_role("unknown-role") == frozenset()
    assert set(ROLES) == {ROLE_ADMIN, ROLE_MEMBER, ROLE_VIEWER, ROLE_GUEST}


def test_required_permission_mapping():
    assert required_permission("GET", f"{BASE}/plans", BASE) == PERM_PLAN_READ
    assert required_permission("POST", f"{BASE}/plans", BASE) == PERM_PLAN_WRITE
    assert required_permission("DELETE", f"{BASE}/plans/p1", BASE) == PERM_PLAN_WRITE
    # 拓扑校验虽为 POST 但属只读检查
    assert required_permission("POST", f"{BASE}/plans/p1/dag-check", BASE) == PERM_PLAN_READ
    assert required_permission("POST", f"{BASE}/agent/chat", BASE) == PERM_AGENT_CHAT
    assert required_permission("POST", f"{BASE}/agent/confirm", BASE) == PERM_AGENT_WRITE
    assert required_permission("GET", f"{BASE}/kb", BASE) == PERM_KB_READ
    assert required_permission("POST", f"{BASE}/kb/search", BASE) == PERM_KB_READ  # 检索只读
    assert required_permission("POST", f"{BASE}/kb/documents", BASE) == PERM_KB_WRITE
    assert required_permission("GET", f"{BASE}/memories", BASE) == PERM_MEMORY_READ
    assert required_permission("POST", f"{BASE}/memories", BASE) == PERM_MEMORY_WRITE
    assert required_permission("GET", f"{BASE}/audit", BASE) == PERM_AUDIT_READ
    assert required_permission("POST", f"{BASE}/workspace/import", BASE) == PERM_PLAN_WRITE
    assert required_permission("GET", f"{BASE}/workspace/preferences", BASE) == PERM_PLAN_READ
    # 账号管理端点仅管理员；登录 / 会话查询只要求「已鉴权」
    assert required_permission("GET", f"{BASE}/auth/users", BASE) == PERM_USER_MANAGE
    assert required_permission("PATCH", f"{BASE}/auth/users/u1", BASE) == PERM_USER_MANAGE
    assert required_permission("POST", f"{BASE}/auth/login", BASE) is None
    assert required_permission("GET", f"{BASE}/auth/session", BASE) is None
    # 未登记路径保守按计划只读（仍须身份鉴权）
    assert required_permission("POST", f"{BASE}/unknown", BASE) == PERM_PLAN_READ
    assert required_permission("GET", "/metrics", BASE) is None  # 非 API 前缀不参与判定


def test_plan_scope_path_and_public_endpoints():
    assert plan_id_from_path(f"{BASE}/plans/p1", BASE) == "p1"
    assert plan_id_from_path(f"{BASE}/plans/p1/tasks/t2", BASE) == "p1"
    assert plan_id_from_path(f"{BASE}/workspace/plans/p2", BASE) == "p2"
    assert plan_id_from_path(f"{BASE}/plans", BASE) is None
    assert plan_id_from_path(f"{BASE}/kb", BASE) is None
    assert is_public_endpoint("POST", f"{BASE}/auth/login", BASE) is True
    assert is_public_endpoint("POST", f"{BASE}/auth/logout", BASE) is True
    assert is_public_endpoint("GET", f"{BASE}/auth/session", BASE) is False
    assert is_public_endpoint("POST", f"{BASE}/plans", BASE) is False


def test_principal_machine_and_guest_scope():
    machine = machine_principal()
    assert machine.machine is True
    assert machine.user_id == MACHINE_USER_ID
    assert machine.has(PERM_USER_MANAGE) is True
    assert machine.can_see_plan("anything") is True

    guest = build_principal(
        user_id="u1", username="guest1", display_name="访客", role=ROLE_GUEST, plan_ids=("p1",)
    )
    assert guest.plan_scope == PLAN_SCOPE_ASSIGNED
    assert guest.can_see_plan("p1") is True
    assert guest.can_see_plan("p2") is False

    public = guest.to_public()
    assert public["planIds"] == ["p1"]
    assert public["permissions"] == [PERM_PLAN_READ]
    assert "password" not in json.dumps(public).lower()


# ------------------------------- 账号 / 会话存储 -------------------------------


def test_username_and_password_validation():
    assert validate_username("  Alice  ") == "Alice"
    with pytest.raises(ApiError):
        validate_username("ab")  # 太短
    with pytest.raises(ApiError):
        validate_username("bad name!")  # 非法字符
    assert validate_password("Good12345") == "Good12345"
    with pytest.raises(ApiError):
        validate_password("allletters")  # 缺数字
    with pytest.raises(ApiError):
        validate_password("12345678")  # 缺字母
    with pytest.raises(ApiError):
        validate_password("Ab1")  # 太短
    assert validate_role(ROLE_VIEWER) == ROLE_VIEWER
    with pytest.raises(ApiError):
        validate_role("root")


async def test_store_user_crud_and_duplicate_username():
    store = AuthStore(":memory:")
    await store.start()
    try:
        assert await store.ensure_bootstrap_admin(BOOTSTRAP_USERNAME, BOOTSTRAP_PASSWORD) is None
        # 二次引导无效：库里已有账号
        assert await store.ensure_bootstrap_admin("other", "Other12345") is None
        assert await store.count_admins() == 1

        bob = await store.create_user(username="Bob", password="Bob12345", role=ROLE_MEMBER)
        assert bob["role"] == ROLE_MEMBER and bob["status"] == "active"
        assert bob["displayName"] == "Bob"
        # 同名（大小写不敏感）拒绝
        with pytest.raises(ApiError):
            await store.create_user(username="bob", password="Bob12345", role=ROLE_MEMBER)
        # 未指定显示名时回落到用户名
        named = await store.create_user(
            username="carol", password="Carol12345", role=ROLE_VIEWER, display_name="卡罗尔"
        )
        assert named["displayName"] == "卡罗尔"

        updated = await store.update_user(bob["userId"], display_name="小明")
        assert updated["displayName"] == "小明"
        with pytest.raises(ApiError):
            await store.update_user(bob["userId"], status="frozen")  # 非法状态
        with pytest.raises(ApiError):
            await store.update_user(bob["userId"])  # 无字段
        with pytest.raises(ApiError):
            await store.get_user("nope")

        assert len(await store.list_users()) == 3
        await store.delete_user(bob["userId"])
        with pytest.raises(ApiError):
            await store.delete_user(bob["userId"])
        assert len(await store.list_users()) == 2
    finally:
        await store.close()


async def test_store_guards_last_active_admin():
    store = AuthStore(":memory:")
    await store.start()
    try:
        await store.ensure_bootstrap_admin(BOOTSTRAP_USERNAME, BOOTSTRAP_PASSWORD)
        admin_id = (await store.list_users())[0]["userId"]

        with pytest.raises(ApiError) as info:
            await store.update_user(admin_id, role=ROLE_MEMBER)
        assert "管理员" in str(info.value.message)
        with pytest.raises(ApiError):
            await store.update_user(admin_id, status="disabled")
        with pytest.raises(ApiError):
            await store.delete_user(admin_id)

        # 新增第二名管理员后，原管理员可被降权
        second = await store.create_user(username="root2", password="Root12345", role=ROLE_ADMIN)
        assert await store.count_admins() == 2
        demoted = await store.update_user(admin_id, role=ROLE_MEMBER)
        assert demoted["role"] == ROLE_MEMBER
        # 剩下一名仍受保护
        with pytest.raises(ApiError):
            await store.delete_user(second["userId"])
    finally:
        await store.close()


async def test_store_login_and_session_lifecycle():
    store = AuthStore(":memory:")
    await store.start()
    try:
        await store.ensure_bootstrap_admin(BOOTSTRAP_USERNAME, BOOTSTRAP_PASSWORD)
        # 口令错误 / 账号不存在一律 None（不区分原因）
        assert await store.verify_login(BOOTSTRAP_USERNAME, "Wrong12345") is None
        assert await store.verify_login("nobody", BOOTSTRAP_PASSWORD) is None
        user = await store.verify_login(BOOTSTRAP_USERNAME.lower(), BOOTSTRAP_PASSWORD)
        assert user is not None and user["userId"]

        token = await store.create_session(user["userId"])
        loaded = await store.load_session(token)
        assert loaded is not None and loaded["userId"] == user["userId"]
        assert await store.load_session("") is None
        assert await store.load_session("forged-token") is None

        # 停用后既有会话立即不可用（JOIN 过滤 status）
        store._db().execute("UPDATE users SET status='disabled' WHERE user_id=?", (user["userId"],))
        store._db().commit()
        assert await store.load_session(token) is None
        assert await store.verify_login(BOOTSTRAP_USERNAME, BOOTSTRAP_PASSWORD) is None

        # 恢复启用并重新登录，验证登出 / 过期
        await store.update_user(user["userId"], status="active")
        token = await store.create_session(user["userId"])
        await store.revoke_session(token)
        assert await store.load_session(token) is None

        token = await store.create_session(user["userId"])
        store._db().execute("UPDATE sessions SET expires_at=? WHERE user_id=?", (time.time() - 1, user["userId"]))
        store._db().commit()
        assert await store.load_session(token) is None

        token = await store.create_session(user["userId"])
        await store.revoke_user_sessions(user["userId"])
        assert await store.load_session(token) is None
    finally:
        await store.close()


async def test_store_sensitive_change_revokes_sessions():
    store = AuthStore(":memory:")
    await store.start()
    try:
        await store.ensure_bootstrap_admin(BOOTSTRAP_USERNAME, BOOTSTRAP_PASSWORD)
        member = await store.create_user(username="dave", password="Dave12345", role=ROLE_MEMBER)

        token = await store.create_session(member["userId"])
        assert await store.load_session(token) is not None
        await store.update_user(member["userId"], role=ROLE_VIEWER)  # 降权 → 吊销
        assert await store.load_session(token) is None

        token = await store.create_session(member["userId"])
        await store.update_user(member["userId"], password="Dave67890")
        assert await store.load_session(token) is None
        assert await store.verify_login("dave", "Dave67890") is not None
        # 仅改显示名不吊销会话
        token = await store.create_session(member["userId"])
        await store.update_user(member["userId"], display_name="戴夫")
        assert await store.load_session(token) is not None
    finally:
        await store.close()


async def test_store_plan_assignment_normalization():
    store = AuthStore(":memory:")
    await store.start()
    try:
        await store.ensure_bootstrap_admin(BOOTSTRAP_USERNAME, BOOTSTRAP_PASSWORD)
        guest = await store.create_user(username="gina", password="Gina12345", role=ROLE_GUEST)

        assigned = await store.set_plan_assignments(guest["userId"], ["p2", " p1 ", "p2", ""])
        assert assigned == ["p2", "p1"]  # 去空白、去重、保序
        assert await store.plan_ids(guest["userId"]) == ["p1", "p2"]  # 读取时按 plan_id 排序
        # 全量覆盖语义：再次设置即替换
        assert await store.set_plan_assignments(guest["userId"], []) == []
        assert await store.plan_ids(guest["userId"]) == []

        with pytest.raises(ApiError):
            await store.set_plan_assignments(guest["userId"], [f"p{i}" for i in range(201)])
        with pytest.raises(ApiError):
            await store.set_plan_assignments("nope", ["p1"])

        # 删除账号一并清理指派
        await store.set_plan_assignments(guest["userId"], ["p1"])
        await store.delete_user(guest["userId"])
        assert await store.plan_ids(guest["userId"]) == []
    finally:
        await store.close()


# ------------------------------- HTTP 鉴权与授权 -------------------------------


def test_api_requires_authentication(make_client):
    with make_client() as c:
        denied = c.get(f"{BASE}/plans")
        assert denied.status_code == 401
        assert denied.json()["code"] == "UNAUTHORIZED"
        # 探测端点 / 登录端点在免鉴权范围内
        assert c.get("/healthz").status_code == 200
        assert c.get(f"{BASE}/auth/roles").status_code == 401  # 非公开端点仍需鉴权


def test_machine_token_is_available_when_auth_enabled(make_client):
    with make_client() as c:
        assert c.get(f"{BASE}/plans", headers=auth()).status_code == 200
        principal = c.get(f"{BASE}/auth/session", headers=auth()).json()["data"]
        assert principal["machine"] is True
        assert principal["userId"] == MACHINE_USER_ID
        assert PERM_USER_MANAGE in principal["permissions"]


def test_auth_disabled_opens_api_and_ws(make_client):
    with make_client(overrides={"auth_enabled": False}) as c:
        assert c.get(f"{BASE}/plans").status_code == 200
        assert c.get("/api/health").json()["python"]["authEnabled"] is False


def test_login_sets_httponly_cookie_and_exposes_principal(make_client):
    with make_client() as c:
        response = c.post(
            f"{BASE}/auth/login", json={"username": BOOTSTRAP_USERNAME, "password": BOOTSTRAP_PASSWORD}
        )
        assert response.status_code == 200
        set_cookie = " ".join(response.headers.get_list("set-cookie")).lower()
        assert "httponly" in set_cookie and f"{SESSION_COOKIE}=" in set_cookie and "samesite=lax" in set_cookie

        data = response.json()["data"]
        assert data["username"] == BOOTSTRAP_USERNAME
        assert data["role"] == ROLE_ADMIN
        assert data["planScope"] == "all"
        assert "password" not in json.dumps(data).lower()

        session = c.get(f"{BASE}/auth/session")
        assert session.status_code == 200 and session.json()["data"]["role"] == ROLE_ADMIN

        roles = c.get(f"{BASE}/auth/roles").json()["data"]["roles"]
        assert {item["role"] for item in roles} == set(ROLES)


def test_login_failure_is_generic_401(make_client):
    with make_client() as c:
        wrong = c.post(f"{BASE}/auth/login", json={"username": BOOTSTRAP_USERNAME, "password": "Wrong12345"})
        unknown = c.post(f"{BASE}/auth/login", json={"username": "ghost", "password": "Wrong12345"})
        assert wrong.status_code == unknown.status_code == 401
        # 文案一致，避免账号枚举
        assert wrong.json()["message"] == unknown.json()["message"]


def test_logout_revokes_session_immediately(make_client):
    with make_client() as c:
        login(c)
        assert c.get(f"{BASE}/auth/session").status_code == 200
        assert c.post(f"{BASE}/auth/logout").status_code == 200
        assert c.get(f"{BASE}/auth/session").status_code == 401


def test_role_gates_user_management(make_client):
    with make_client() as c:
        login(c)
        created = c.post(
            f"{BASE}/auth/users",
            json={"username": "vicky", "password": "Vicky12345", "role": ROLE_VIEWER},
        )
        assert created.status_code == 200
        viewer_id = created.json()["data"]["userId"]

        # 切换到只读账号：能读计划，但管理账号被拒
        login(c, "vicky", "Vicky12345")
        assert c.get(f"{BASE}/plans").status_code == 200
        denied = c.get(f"{BASE}/auth/users")
        assert denied.status_code == 403
        assert denied.json()["code"] == "FORBIDDEN"
        assert c.post(f"{BASE}/auth/users", json={"username": "x1", "password": "Xxxxx12345"}).status_code == 403

        # 切回管理员可管理
        login(c)
        assert c.get(f"{BASE}/auth/users").json()["data"]["total"] == 2
        assert c.get(f"{BASE}/auth/users").status_code == 200
        deleted = c.delete(f"{BASE}/auth/users/{viewer_id}")
        assert deleted.status_code == 200
        # 不能删除当前登录账号
        admin_id = c.get(f"{BASE}/auth/users").json()["data"]["items"][0]["userId"]
        assert c.delete(f"{BASE}/auth/users/{admin_id}").status_code == 400


def test_change_password_verifies_current_and_revokes_session(make_client):
    with make_client() as c:
        login(c)
        wrong = c.post(
            f"{BASE}/auth/password", json={"currentPassword": "Wrong12345", "newPassword": "Newpass12345"}
        )
        assert wrong.status_code == 401
        assert c.get(f"{BASE}/auth/session").status_code == 200

        ok = c.post(
            f"{BASE}/auth/password", json={"currentPassword": BOOTSTRAP_PASSWORD, "newPassword": "Newpass12345"}
        )
        assert ok.status_code == 200
        # 改口令后当前会话被吊销
        assert c.get(f"{BASE}/auth/session").status_code == 401
        # 新口令可登录，旧口令不可
        assert c.post(f"{BASE}/auth/login", json={"username": BOOTSTRAP_USERNAME, "password": BOOTSTRAP_PASSWORD}).status_code == 401
        assert c.post(f"{BASE}/auth/login", json={"username": BOOTSTRAP_USERNAME, "password": "Newpass12345"}).status_code == 200

        # 机器令牌不支持改口令
        machine = c.post(
            f"{BASE}/auth/password",
            json={"currentPassword": BOOTSTRAP_PASSWORD, "newPassword": "Newpass12345"},
            headers=auth(),
        )
        assert machine.status_code == 403


def test_guest_sees_only_assigned_plans(make_client):
    with make_client() as c:
        login(c)
        guest = c.post(
            f"{BASE}/auth/users",
            json={"username": "gina", "password": "Gina12345", "role": ROLE_GUEST},
        ).json()["data"]
        guest_id = guest["userId"]
        # 非访客不得维护计划白名单
        member = c.post(
            f"{BASE}/auth/users", json={"username": "molly", "password": "Molly12345", "role": ROLE_MEMBER}
        ).json()["data"]
        assert c.put(f"{BASE}/auth/users/{member['userId']}/plans", json={"planIds": ["p1"]}).status_code == 400

        # 未指派任何计划：列表为空、单计划 404
        login(c, "gina", "Gina12345")
        assert c.get(f"{BASE}/plans").json()["data"] == []
        assert c.get(f"{BASE}/plans/p1").status_code == 404

        # 指派 p1 后：可见 p1，仍不可见 p2
        login(c)
        assert c.put(f"{BASE}/auth/users/{guest_id}/plans", json={"planIds": ["p1"]}).status_code == 200
        login(c, "gina", "Gina12345")
        visible = c.get(f"{BASE}/plans").json()["data"]
        assert [item["planId"] for item in visible] == ["p1"]
        assert c.get(f"{BASE}/plans/p1").status_code == 200
        assert c.get(f"{BASE}/plans/p2").status_code == 404
        # 读权限可用，写操作被角色拦（403）
        assert c.post(f"{BASE}/plans", json={"requirement": "x"}).status_code == 403


# ------------------------------- WebSocket 鉴权与作用域 -------------------------------


def test_ws_handshake_accepts_session_cookie(make_client):
    with make_client() as c:
        login(c)
        token = c.cookies.get(SESSION_COOKIE)
        assert token
        c.cookies.clear()  # 只以显式 Cookie 头握手，避免与 Cookie jar 重复
        with c.websocket_connect("/ws", headers={"cookie": f"{SESSION_COOKIE}={token}"}) as ws:
            assert ws.receive_json()["type"] == "connected"


def test_ws_handshake_rejects_anonymous_when_auth_enabled(make_client):
    from fastapi import WebSocketDisconnect

    with make_client() as c:
        with pytest.raises(WebSocketDisconnect) as info:
            with c.websocket_connect("/ws"):
                pass
        assert info.value.code == 1008


def test_ws_guest_gets_connected_frame_without_plan_events(make_client):
    """访客（仅被指派 p1）也能完成握手，且握手帧不含任何计划信息。"""
    with make_client() as c:
        login(c)
        guest = c.post(
            f"{BASE}/auth/users",
            json={"username": "gina", "password": "Gina12345", "role": ROLE_GUEST},
        ).json()["data"]
        c.put(f"{BASE}/auth/users/{guest['userId']}/plans", json={"planIds": ["p1"]})
        login(c, "gina", "Gina12345")
        token = c.cookies.get(SESSION_COOKIE)
        c.cookies.clear()
        with c.websocket_connect("/ws", headers={"cookie": f"{SESSION_COOKIE}={token}"}) as ws:
            frame = ws.receive_json()
            assert frame["type"] == "connected"
            assert "planId" not in frame and "planIds" not in frame


class _FakeWebSocket:
    """仅记录被下发文本的假连接（用于广播过滤断言）。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.accepted = False
        self.closed_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_code = code

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


def test_ws_broadcast_and_replay_respect_plan_scope():
    broadcaster = WsBroadcaster(replay_size=5)

    async def scenario() -> tuple[_FakeWebSocket, _FakeWebSocket]:
        guest = _FakeWebSocket()
        admin = _FakeWebSocket()
        await broadcaster.connect(guest, plan_ids=frozenset({"p1"}))
        await broadcaster.connect(admin)
        await broadcaster.broadcast({"type": "plan.created", "planId": "p2"})
        await broadcaster.broadcast({"type": "plan.created", "planId": "p1"})
        await broadcaster.broadcast({"type": "plan.deleted"})  # 无 planId：受限连接也不应收到
        return guest, admin

    guest, admin = asyncio.run(scenario())
    # 访客只收到被指派计划的事件，无 planId 的事件同样过滤
    assert [item.get("planId") for item in guest.sent] == ["p1"]
    assert len(admin.sent) == 3

    assert broadcaster.replay_payloads(plan_ids=frozenset({"p1"})) == [
        {"type": "plan.created", "planId": "p1"}
    ]
    assert len(broadcaster.replay_payloads()) == 3
