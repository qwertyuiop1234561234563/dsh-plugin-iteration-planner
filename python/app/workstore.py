"""工作区治理数据存储：基线快照 / 人工风险登记册 / 配置备份。

为什么选择「单文件 JSON + 原子替换」而不是数据库：
这三类数据都是**辅助治理数据**（KB 级），写入频率低、无并发写热点；单文件既能满足
单机可靠性（临时文件 + ``os.replace`` 原子替换，进程中断也不会留下半写文件），
又便于直接放到共享盘做**人工迁移 / 备份**，同时不给插件引入 Redis / 数据库等额外基础设施依赖。

并发模型：进程内 ``asyncio.Lock`` 串行化读写（覆盖全部「读改写」路径），读取走内存缓存
（启动时预热，缺失则首次访问懒加载）。**该存储面向单副本部署**：多副本同时写同一文件
没有跨进程锁保护，多副本场景请改用共享数据库或由网关侧统一收口。
文件损坏时**降级为空数据并告警**，保证治理功能不因存储异常而整体不可用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from .constants import (
    MAX_BASELINES_PER_PLAN,
    MAX_ISSUES_PER_PLAN,
    WORKSPACE_STORE_VERSION,
)
from .errors import bad_request, not_found

logger = logging.getLogger("iteration_planner.workspace")

#: 各集合的默认结构
_EMPTY: dict[str, Any] = {
    "version": WORKSPACE_STORE_VERSION,
    "baselines": {},
    "issues": {},
    "preferences": {},
}


class WorkspaceStore:
    """工作区治理数据的持久化存储（进程内单例）。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] | None = None

    # ------------------------------- 生命周期 -------------------------------

    async def start(self) -> None:
        """预热：把存储文件读入内存（不存在则视为空）。"""
        async with self._lock:
            self._data = await asyncio.to_thread(self._load)

    async def close(self) -> None:
        """关机：把内存态落盘，避免未持久化的变更丢失。"""
        async with self._lock:
            if self._data is not None:
                await asyncio.to_thread(self._write, self._data)

    # ------------------------------- 基线快照 -------------------------------

    async def list_baselines(self, plan_id: str) -> list[dict[str, Any]]:
        async with self._lock:
            items = self._bucket("baselines").get(plan_id) or []
            return [dict(item) for item in items]

    async def get_baseline(self, plan_id: str, baseline_id: str) -> dict[str, Any]:
        async with self._lock:
            for item in self._bucket("baselines").get(plan_id) or []:
                if item.get("baselineId") == baseline_id:
                    return dict(item)
        raise not_found(f"基线快照不存在：{baseline_id}")

    async def add_baseline(self, plan_id: str, record: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            items = self._bucket("baselines").setdefault(plan_id, [])
            if len(items) >= MAX_BASELINES_PER_PLAN:
                raise bad_request(f"单个计划最多保留 {MAX_BASELINES_PER_PLAN} 个基线快照，请先删除历史快照")
            items.append(dict(record))
            await asyncio.to_thread(self._write, self._data)
            return dict(record)

    async def delete_baseline(self, plan_id: str, baseline_id: str) -> None:
        async with self._lock:
            items = self._bucket("baselines").get(plan_id) or []
            remaining = [item for item in items if item.get("baselineId") != baseline_id]
            if len(remaining) == len(items):
                raise not_found(f"基线快照不存在：{baseline_id}")
            self._bucket("baselines")[plan_id] = remaining
            await asyncio.to_thread(self._write, self._data)

    # ------------------------------ 风险登记册 ------------------------------

    async def list_issues(self, plan_id: str) -> list[dict[str, Any]]:
        async with self._lock:
            items = self._bucket("issues").get(plan_id) or []
            return [dict(item) for item in items]

    async def add_issue(self, plan_id: str, record: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            items = self._bucket("issues").setdefault(plan_id, [])
            if len(items) >= MAX_ISSUES_PER_PLAN:
                raise bad_request(f"单个计划最多登记 {MAX_ISSUES_PER_PLAN} 条风险，请先关闭 / 删除历史条目")
            items.append(dict(record))
            await asyncio.to_thread(self._write, self._data)
            return dict(record)

    async def update_issue(self, plan_id: str, issue_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            items = self._bucket("issues").get(plan_id) or []
            for index, item in enumerate(items):
                if item.get("issueId") != issue_id:
                    continue
                merged = {**item, **patch}
                items[index] = merged
                await asyncio.to_thread(self._write, self._data)
                return dict(merged)
        raise not_found(f"风险条目不存在：{issue_id}")

    async def delete_issue(self, plan_id: str, issue_id: str) -> None:
        async with self._lock:
            items = self._bucket("issues").get(plan_id) or []
            remaining = [item for item in items if item.get("issueId") != issue_id]
            if len(remaining) == len(items):
                raise not_found(f"风险条目不存在：{issue_id}")
            self._bucket("issues")[plan_id] = remaining
            await asyncio.to_thread(self._write, self._data)

    # ------------------------------- 配置备份 -------------------------------

    async def get_preferences(self, profile: str) -> dict[str, Any] | None:
        async with self._lock:
            stored = self._bucket("preferences").get(profile)
            return dict(stored) if isinstance(stored, dict) else None

    async def put_preferences(self, profile: str, blob: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            self._bucket("preferences")[profile] = dict(blob)
            await asyncio.to_thread(self._write, self._data)
            return dict(blob)

    async def delete_preferences(self, profile: str) -> None:
        async with self._lock:
            bucket = self._bucket("preferences")
            if profile in bucket:
                bucket.pop(profile)
                await asyncio.to_thread(self._write, self._data)

    # -------------------------------- 内部实现 --------------------------------

    def _bucket(self, name: str) -> dict[str, Any]:
        data = self._ensure_loaded()
        bucket = data.get(name)
        if not isinstance(bucket, dict):
            bucket = {}
            data[name] = bucket
        return bucket

    def _ensure_loaded(self) -> dict[str, Any]:
        if self._data is None:
            self._data = self._load()
        return self._data

    def _load(self) -> dict[str, Any]:
        path = Path(self._path)
        if not path.is_file():
            return json.loads(json.dumps(_EMPTY))
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("工作区存储文件不可读，已降级为空数据（path=%s）：%s", self._path, exc)
            return json.loads(json.dumps(_EMPTY))
        if not isinstance(raw, dict):
            logger.warning("工作区存储文件结构异常（非对象），已降级为空数据（path=%s）", self._path)
            return json.loads(json.dumps(_EMPTY))
        merged = json.loads(json.dumps(_EMPTY))
        for key in ("baselines", "issues", "preferences"):
            value = raw.get(key)
            if isinstance(value, dict):
                merged[key] = value
        return merged

    def _write(self, data: dict[str, Any]) -> None:
        """原子落盘：先写临时文件，再 ``os.replace`` 覆盖，避免半写文件。"""
        path = Path(self._path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(f"{path.name}.tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, path)
        except OSError as exc:
            # 持久化失败不应中断业务：内存态仍然正确，仅告警
            logger.error("工作区存储写入失败（path=%s）：%s", self._path, exc)


__all__ = ["WorkspaceStore"]
