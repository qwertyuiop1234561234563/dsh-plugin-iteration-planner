"""账号 / 会话 / 计划指派存储（纯标准库 ``sqlite3``，不引入额外依赖）。

为何选 SQLite 而不是沿用工作区那套「单文件 JSON」：账号数据存在**读多写少但写点分散**
（登录刷新 ``last_seen``、会话增删、指派变更）的特征，用数据库的事务与唯一索引能天然避免
「读改写」竞态（例如同名账号并发创建）；单文件即可满足单副本部署，无需额外基础设施。

会话安全口径：Cookie 里只放**不透明随机令牌**，落库的是它的 ``sha256`` 摘要——
即便数据库被读走，也无法据此伪造会话（攻击者拿到的是摘要，回放上行需要原像）。
令牌随时可删，因此「即时吊销 / 踢下线」是 O(1) 的。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import sqlite3
import string
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from ..constants import (
    DISPLAY_NAME_MAX_LEN,
    MAX_PLAN_ASSIGNMENTS,
    PASSWORD_MAX_LEN,
    PASSWORD_MIN_LEN,
    ROLE_ADMIN,
    ROLES,
    USER_STATUS_ACTIVE,
    USERNAME_MAX_LEN,
    USERNAME_MIN_LEN,
)
from ..errors import bad_request, not_found
from .passwords import hash_password, verify_password

logger = logging.getLogger("iteration_planner.auth")

#: 用户名允许的字符集（大小写不敏感唯一，但保留原始大小写用于展示）
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]+$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    username_key  TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,
    status        TEXT NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    last_login_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS plan_assignments (
    user_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    PRIMARY KEY (user_id, plan_id)
);
"""

#: 缺账号时用于「抵消时序差」的哑散列（懒初始化，避免拖慢模块导入）
_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


def normalize_username(username: str) -> str:
    """去除首尾空白（大小写保留，唯一性用大小写不敏感键保证）。"""
    return (username or "").strip()


def validate_username(username: str) -> str:
    cleaned = normalize_username(username)
    if not (USERNAME_MIN_LEN <= len(cleaned) <= USERNAME_MAX_LEN):
        raise bad_request(f"用户名长度须为 {USERNAME_MIN_LEN}-{USERNAME_MAX_LEN} 个字符")
    if not _USERNAME_RE.match(cleaned):
        raise bad_request("用户名仅允许字母、数字与 . _ @ - 四种符号")
    return cleaned


def validate_password(password: str) -> str:
    if not (PASSWORD_MIN_LEN <= len(password or "") <= PASSWORD_MAX_LEN):
        raise bad_request(f"口令长度须为 {PASSWORD_MIN_LEN}-{PASSWORD_MAX_LEN} 个字符")
    if not any(ch.isalpha() for ch in password) or not any(ch.isdigit() for ch in password):
        raise bad_request("口令需同时包含字母与数字")
    return password


def validate_display_name(display_name: str, fallback: str) -> str:
    cleaned = (display_name or "").strip() or fallback
    if len(cleaned) > DISPLAY_NAME_MAX_LEN:
        raise bad_request(f"显示名不能超过 {DISPLAY_NAME_MAX_LEN} 个字符")
    return cleaned


def validate_role(role: str) -> str:
    if role not in ROLES:
        raise bad_request(f"角色非法，可选：{' / '.join(ROLES)}")
    return role


def _now() -> float:
    return time.time()


def _random_password(length: int = 16) -> str:
    """生成满足 :func:`validate_password` 的随机引导口令。

    不能直接采用 ``secrets.token_urlsafe(12)``：其 16 个字符全部落在字母上的概率约
    ``(54/64)**16 ≈ 6.6%``，此时不含数字、会被校验拒绝，导致「首次启动随机引导」在约
    十五分之一的部署上直接失败。这里改为**断言式生成**：先取随机串，不满足「同时含字母与
    数字」就重取（期望迭代次数 ≈ 1.07，几乎不会循环）。
    """
    alphabet = string.ascii_letters + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(ch.isdigit() for ch in candidate) and any(ch.isalpha() for ch in candidate):
            return candidate


def _iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _public_user(row: sqlite3.Row) -> dict[str, Any]:
    """用户对外视图：**绝不包含口令散列**。"""
    return {
        "userId": row["user_id"],
        "username": row["username"],
        "displayName": row["display_name"],
        "role": row["role"],
        "status": row["status"],
        "createdAt": _iso(row["created_at"]),
        "updatedAt": _iso(row["updated_at"]),
        "lastLoginAt": _iso(row["last_login_at"]),
    }


class AuthStore:
    """账号数据的持久化存储（进程内单例，全量操作经 ``asyncio.Lock`` 串行化）。"""

    def __init__(self, path: str, *, session_ttl_seconds: int = 8 * 3600) -> None:
        self._path = path
        self._ttl = max(300, int(session_ttl_seconds))
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------- 生命周期 -------------------------------

    async def start(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._open)

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                conn, self._conn = self._conn, None
                await asyncio.to_thread(conn.close)

    def _open(self) -> None:
        memory = self._path in ("", ":memory:")
        conn = sqlite3.connect(":memory:" if memory else self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if not memory:
            # WAL 提升读写并发并降低写锁冲突；内存库不支持，失败也不影响正确性
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:  # pragma: no cover - 取决于底层文件系统
                pass
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        logger.info("账号存储就绪：%s", "内存（仅本次进程有效）" if memory else self._path)

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:  # pragma: no cover - 未 start() 即访问属编程错误
            raise RuntimeError("AuthStore 尚未启动（缺少 lifespan 调用）")
        return self._conn

    # ------------------------------- 引导管理员 -------------------------------

    async def ensure_bootstrap_admin(self, username: str, password: str) -> str | None:
        """首个管理员引导：仅在**库中没有任何账号**时创建。

        :return: 配置未提供口令而随机生成时返回该口令（调用方负责以告警日志提示运维），
            否则返回 ``None``。
        """
        async with self._lock:
            return await asyncio.to_thread(self._bootstrap, username, password)

    def _bootstrap(self, username: str, password: str) -> str | None:
        conn = self._db()
        if conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] > 0:
            return None
        name = validate_username(username or "admin")
        generated: str | None = None
        if not password:
            generated = _random_password()
            password = generated
        else:
            # 运维显式配置的口令同样按账号口令规则校验，避免引导出一个弱口令账号
            validate_password(password)
        now = _now()
        conn.execute(
            "INSERT INTO users (user_id, username, username_key, display_name, password_hash, role, status,"
            " created_at, updated_at, last_login_at) VALUES (?,?,?,?,?,?,?,?,?,NULL)",
            (
                uuid.uuid4().hex,
                name,
                name.lower(),
                "管理员",
                hash_password(password),
                ROLE_ADMIN,
                USER_STATUS_ACTIVE,
                now,
                now,
            ),
        )
        conn.commit()
        return generated

    # ------------------------------- 账号读写 -------------------------------

    async def count_admins(self) -> int:
        async with self._lock:
            row = await asyncio.to_thread(
                lambda: self._db()
                .execute(f"SELECT COUNT(*) AS n FROM users WHERE role=? AND status=?", (ROLE_ADMIN, USER_STATUS_ACTIVE))
                .fetchone()
            )
            return int(row["n"])

    async def list_users(self) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._list_users)

    def _list_users(self) -> list[dict[str, Any]]:
        rows = self._db().execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
        users = [_public_user(row) for row in rows]
        for user in users:
            user["planIds"] = self._assignments(user["userId"])
        return users

    async def get_user(self, user_id: str) -> dict[str, Any]:
        async with self._lock:
            row = await asyncio.to_thread(self._get_row, user_id)
        if row is None:
            raise not_found(f"账号不存在：{user_id}")
        payload = _public_user(row)
        payload["planIds"] = await self.plan_ids(user_id)
        return payload

    def _get_row(self, user_id: str) -> sqlite3.Row | None:
        return self._db().execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    async def create_user(self, *, username: str, password: str, role: str, display_name: str = "") -> dict[str, Any]:
        name = validate_username(username)
        validate_password(password)
        validate_role(role)
        label = validate_display_name(display_name, name)
        async with self._lock:
            return await asyncio.to_thread(self._insert_user, name.lower(), name, label, password, role)

    def _insert_user(self, key: str, name: str, label: str, password: str, role: str) -> dict[str, Any]:
        conn = self._db()
        if conn.execute("SELECT 1 FROM users WHERE username_key=?", (key,)).fetchone():
            raise bad_request(f"用户名已存在：{name}")
        now = _now()
        user_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO users (user_id, username, username_key, display_name, password_hash, role, status,"
            " created_at, updated_at, last_login_at) VALUES (?,?,?,?,?,?,?,?,?,NULL)",
            (user_id, name, key, label, hash_password(password), role, USER_STATUS_ACTIVE, now, now),
        )
        conn.commit()
        return _public_user(conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())

    async def update_user(
        self,
        user_id: str,
        *,
        role: str | None = None,
        status: str | None = None,
        display_name: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._update_user, user_id, role, status, display_name, password)

    def _update_user(
        self,
        user_id: str,
        role: str | None,
        status: str | None,
        display_name: str | None,
        password: str | None,
    ) -> dict[str, Any]:
        conn = self._db()
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            raise not_found(f"账号不存在：{user_id}")
        sets: list[str] = []
        values: list[Any] = []
        if role is not None:
            validate_role(role)
            self._guard_last_admin(row, role=role, status=status)
            sets.append("role=?")
            values.append(role)
        if status is not None:
            if status not in ("active", "disabled"):
                raise bad_request("账号状态非法，可选：active / disabled")
            self._guard_last_admin(row, role=role, status=status)
            sets.append("status=?")
            values.append(status)
        if display_name is not None:
            sets.append("display_name=?")
            values.append(validate_display_name(display_name, row["username"]))
        if password is not None:
            sets.append("password_hash=?")
            values.append(hash_password(validate_password(password)))
        if not sets:
            raise bad_request("未提供任何可更新字段")
        sets.append("updated_at=?")
        values.append(_now())
        values.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE user_id=?", values)
        # 角色 / 状态或口令变更后，既有会话立即失效（避免降权后仍持旧权限）
        if role is not None or status is not None or password is not None:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.commit()
        return _public_user(conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone())

    def _guard_last_admin(self, row: sqlite3.Row, *, role: str | None, status: str | None) -> None:
        """禁止把最后一个可用管理员降权或停用（否则控制台将永久失去管理入口）。"""
        if row["role"] != ROLE_ADMIN or row["status"] != USER_STATUS_ACTIVE:
            return
        next_role = role if role is not None else row["role"]
        next_status = status if status is not None else row["status"]
        if next_role == ROLE_ADMIN and next_status == USER_STATUS_ACTIVE:
            return
        count = self._db().execute(
            "SELECT COUNT(*) AS n FROM users WHERE role=? AND status=? AND user_id<>?",
            (ROLE_ADMIN, USER_STATUS_ACTIVE, row["user_id"]),
        ).fetchone()["n"]
        if count == 0:
            raise bad_request("系统必须保留至少一名启用状态的管理员")

    async def delete_user(self, user_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._delete_user, user_id)

    def _delete_user(self, user_id: str) -> None:
        conn = self._db()
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            raise not_found(f"账号不存在：{user_id}")
        self._guard_last_admin(row, role=None, status="disabled")
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM plan_assignments WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        conn.commit()

    async def set_plan_assignments(self, user_id: str, plan_ids: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in plan_ids or []:
            value = str(item).strip()
            if value and value not in seen:
                seen.add(value)
                cleaned.append(value)
        if len(cleaned) > MAX_PLAN_ASSIGNMENTS:
            raise bad_request(f"单个账号最多指派 {MAX_PLAN_ASSIGNMENTS} 个计划")
        async with self._lock:
            return await asyncio.to_thread(self._set_assignments, user_id, cleaned)

    def _set_assignments(self, user_id: str, plan_ids: list[str]) -> list[str]:
        conn = self._db()
        if conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone() is None:
            raise not_found(f"账号不存在：{user_id}")
        conn.execute("DELETE FROM plan_assignments WHERE user_id=?", (user_id,))
        conn.executemany(
            "INSERT INTO plan_assignments (user_id, plan_id) VALUES (?,?)",
            [(user_id, plan_id) for plan_id in plan_ids],
        )
        conn.commit()
        return plan_ids

    async def plan_ids(self, user_id: str) -> list[str]:
        async with self._lock:
            return await asyncio.to_thread(self._assignments, user_id)

    def _assignments(self, user_id: str) -> list[str]:
        rows = self._db().execute(
            "SELECT plan_id FROM plan_assignments WHERE user_id=? ORDER BY plan_id ASC", (user_id,)
        ).fetchall()
        return [row["plan_id"] for row in rows]

    # ------------------------------- 登录 / 会话 -------------------------------

    async def verify_login(self, username: str, password: str) -> dict[str, Any] | None:
        """校验账号口令；失败一律返回 ``None``（不区分「不存在」与「口令错」）。"""
        async with self._lock:
            return await asyncio.to_thread(self._verify_login, normalize_username(username), password or "")

    def _verify_login(self, username: str, password: str) -> dict[str, Any] | None:
        row = self._db().execute("SELECT * FROM users WHERE username_key=?", (username.lower(),)).fetchone()
        if row is None:
            # 对不存在的账号也做一次等价耗时的校验，避免用时序差枚举出有效用户名
            verify_password(password, _dummy_hash())
            return None
        matched = verify_password(password, row["password_hash"])
        if not matched or row["status"] != USER_STATUS_ACTIVE:
            return None
        return _public_user(row)

    async def touch_login(self, user_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._touch_login, user_id)

    def _touch_login(self, user_id: str) -> None:
        conn = self._db()
        now = _now()
        conn.execute("UPDATE users SET last_login_at=?, updated_at=? WHERE user_id=?", (now, now, user_id))
        conn.commit()

    async def create_session(self, user_id: str) -> str:
        """创建会话并返回**不透明令牌**（落库的是其 sha256 摘要）。"""
        token = secrets.token_urlsafe(32)
        async with self._lock:
            await asyncio.to_thread(self._insert_session, user_id, token)
        return token

    def _insert_session(self, user_id: str, token: str) -> None:
        conn = self._db()
        now = _now()
        conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen_at) VALUES (?,?,?,?,?)",
            (_token_hash(token), user_id, now, now + self._ttl, now),
        )
        conn.commit()

    async def load_session(self, token: str) -> dict[str, Any] | None:
        """按会话令牌解析用户（校验过期与账号状态），同时刷新 ``last_seen``。"""
        if not token:
            return None
        async with self._lock:
            return await asyncio.to_thread(self._load_session, token)

    def _load_session(self, token: str) -> dict[str, Any] | None:
        conn = self._db()
        now = _now()
        key = _token_hash(token)
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.user_id = s.user_id"
            " WHERE s.token_hash=? AND s.expires_at>? AND u.status=?",
            (key, now, USER_STATUS_ACTIVE),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (now, key))
        conn.commit()
        return _public_user(row)

    async def revoke_session(self, token: str) -> None:
        if not token:
            return
        async with self._lock:
            await asyncio.to_thread(self._delete_session, token)

    def _delete_session(self, token: str) -> None:
        conn = self._db()
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
        conn.commit()

    async def revoke_user_sessions(self, user_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._revoke_user_sessions, user_id)

    def _revoke_user_sessions(self, user_id: str) -> None:
        conn = self._db()
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.commit()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


__all__ = [
    "AuthStore",
    "normalize_username",
    "validate_display_name",
    "validate_password",
    "validate_role",
    "validate_username",
]
