"""自定义（回归）评测用例存储：让「事故 → 用例 → 门禁」形成闭环。

黄金集（:mod:`app.evals.golden`）覆盖的是**已知的通用失败模式**；真实事故却总是具体的：
「某个需求下 ``task_update`` 连续两次 NOT_FOUND」「某次回答触发了谎报完成护栏」。
这类事故若只靠人事后补用例，最迟两次迭代就会被忘掉。

因此本模块提供**持久化**的自定义用例存储：

- 用例默认由失败轨迹**确定性推导**（见 :mod:`app.evals.incidents`），不依赖人写断言，
  避免「事故复盘 → 用例」之间的人工损耗。
- 存储落在 :mod:`app.kvstore`（内存 / Redis 可插拔）：多副本部署切 Redis 后，一个副本上
  沉淀的用例在另一个副本上照样生效，CI 门禁（``python -m app.evals.gate``）也能读到。
- 用例一经登记即被评测台 ``cases_for()`` 合并进运行集，从而**自动纳入 CI 门禁**：
  修好之后若再次回归，门禁会红。

数量上限（``EVALS_MAX_CUSTOM_CASES``）是刻意保留的：无上限追加会让评测时长随事故数量
线性增长，最终团队会为了「等评测跑完」而开始忽略它——那等于把门禁废掉。达到上限时本模块
**拒绝新增并明确报错**，而不是静默丢弃旧用例。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..constants import (
    CUSTOM_CASE_INDEX,
    CUSTOM_CASE_ORIGIN_INCIDENT,
    CUSTOM_CASE_ORIGINS,
    CUSTOM_CASE_PREFIX,
    MAX_CUSTOM_CASES,
)
from ..errors import bad_request
from ..kvstore import KeyValueStore, build_kv_store
from ..metrics import METRICS
from ..models import CustomEvalCase, EvalCaseSpec

logger = logging.getLogger("iteration_planner.evals.custom")


class CustomCaseStore:
    """自定义用例存储（构建在可插拔键值存储之上）。"""

    def __init__(self, kv: KeyValueStore, *, ttl_seconds: float, max_cases: int = MAX_CUSTOM_CASES) -> None:
        self._kv = kv
        self._ttl = ttl_seconds
        self._max_cases = max(1, max_cases)

    @property
    def backend(self) -> str:
        return self._kv.name

    def describe(self) -> dict[str, Any]:
        return {
            "origins": list(CUSTOM_CASE_ORIGINS),
            "maxCases": self._max_cases,
            **self._kv.describe(),
        }

    async def close(self) -> None:
        await self._kv.close()

    # ------------------------------- 读写 -------------------------------

    async def save(
        self,
        *,
        case: EvalCaseSpec,
        origin: str = CUSTOM_CASE_ORIGIN_INCIDENT,
        origin_trace_id: str | None = None,
        note: str = "",
        owner: str = "",
        rationale: list[str] | None = None,
    ) -> CustomEvalCase:
        """登记（或覆盖）一条自定义用例。

        同名 ID 覆盖是刻意允许的：同一条轨迹重复沉淀、或事故后重新推导出更准确的断言，
        都应当更新同一条用例，而不是在集合里留下两条内容相近、结论可能互相矛盾的用例。
        """
        if case.id in _golden_ids():
            raise bad_request(f"用例 ID {case.id} 与黄金集冲突，请换一个 ID（黄金集是基线，不可被覆盖）")
        existing = await self.get(case.id)
        if existing is not None:
            logger.info("自定义用例 %s 已存在（来源 %s），本次覆盖更新", case.id, existing.origin)
        elif await self.count() >= self._max_cases:
            raise bad_request(
                f"自定义用例已达上限 {self._max_cases} 条：请先删除不再需要的用例，"
                f"或调大 EVALS_MAX_CUSTOM_CASES（无上限追加会让评测时长失控）"
            )

        item = CustomEvalCase(
            case=case,
            origin=origin if origin in CUSTOM_CASE_ORIGINS else CUSTOM_CASE_ORIGIN_INCIDENT,
            originTraceId=origin_trace_id,
            note=note,
            owner=owner,
            createdAt=time.time(),
            rationale=list(rationale or []),
        )
        await self._kv.set(self._key(case.id), item.model_dump(), self._ttl)
        await self._touch_index(case.id)
        METRICS.inc_counter("evals_custom_case_total", {"operation": "save", "origin": item.origin})
        return item

    async def get(self, case_id: str) -> CustomEvalCase | None:
        raw = await self._kv.get(self._key(case_id))
        if not isinstance(raw, dict):
            return None
        try:
            return CustomEvalCase.model_validate(raw)
        except Exception:  # noqa: BLE001 - 脏数据按不存在处理
            logger.warning("自定义用例 %s 结构非法，已按不存在处理", case_id)
            return None

    async def list(self, *, limit: int = 200) -> list[CustomEvalCase]:
        """按登记顺序（最近优先）返回用例。"""
        found: list[CustomEvalCase] = []
        for case_id in await self._read_index():
            if len(found) >= limit:
                break
            item = await self.get(case_id)
            if item is not None:
                found.append(item)
        return found

    async def specs(self, *, limit: int = 200) -> tuple[EvalCaseSpec, ...]:
        """取出用例定义，供评测台与 CI 门禁合并进运行集（保序：最近登记的在前）。"""
        return tuple(item.case for item in await self.list(limit=limit))

    async def count(self) -> int:
        return len(await self._read_index())

    async def delete(self, case_id: str) -> bool:
        if await self.get(case_id) is None:
            return False
        await self._kv.delete(self._key(case_id))
        await self._write_index([item for item in await self._read_index() if item != case_id])
        METRICS.inc_counter("evals_custom_case_total", {"operation": "delete", "origin": "any"})
        return True

    # ------------------------------- 内部实现 -------------------------------

    def _key(self, case_id: str) -> str:
        return f"case:{case_id}"

    async def _read_index(self) -> list[str]:
        raw = await self._kv.get(CUSTOM_CASE_INDEX)
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, str)]

    async def _write_index(self, ids: list[str]) -> None:
        await self._kv.set(CUSTOM_CASE_INDEX, ids[: self._max_cases], self._ttl)

    async def _touch_index(self, case_id: str) -> None:
        ids = [case_id, *[item for item in await self._read_index() if item != case_id]]
        await self._write_index(ids)


#: 黄金集 ID（延迟导入：``custom`` 与 ``golden`` 之间无循环依赖，此处只为避免模块级 import
#: 顺序耦合——黄金集是静态常量，取一次缓存即可）
_GOLDEN_ID_CACHE: set[str] | None = None


def _golden_ids() -> set[str]:
    global _GOLDEN_ID_CACHE
    if _GOLDEN_ID_CACHE is None:
        from .golden import GOLDEN_CASES

        _GOLDEN_ID_CACHE = {case.id for case in GOLDEN_CASES}
    return _GOLDEN_ID_CACHE


def build_custom_case_store(
    *,
    driver: str = "auto",
    redis_url: str = "",
    max_entries: int = 512,
    ttl_seconds: float,
    max_cases: int = MAX_CUSTOM_CASES,
) -> CustomCaseStore:
    """按配置构建自定义用例存储（``driver=redis`` 缺依赖 / 缺地址时启动即失败）。"""
    kv = build_kv_store(
        store="evals-custom-case",
        driver=driver,
        redis_url=redis_url,
        max_entries=max(max_entries, 128),
        prefix=CUSTOM_CASE_PREFIX,
    )
    return CustomCaseStore(kv, ttl_seconds=ttl_seconds, max_cases=max_cases)


__all__ = ["CustomCaseStore", "build_custom_case_store"]
