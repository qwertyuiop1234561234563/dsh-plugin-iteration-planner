"""轻量指标注册表（纯标准库，输出 Prometheus 文本格式）。

为什么不用 ``prometheus_client``：对外服务要尽量少的运行时依赖与攻击面，
而网关所需指标（计数器 / 仪表 / 直方图）用不到 100 行即可自洽实现。

暴露路径：``GET /metrics``（见 ``routes/system.py``）。指标前缀统一为
``iteration_planner_``，便于与引擎指标区分。

标签基数控制：HTTP 请求指标使用**路由模板**（如 ``/api/plans/{plan_id}``）而非
真实路径，避免 ``planId`` 造成时间序列爆炸。
"""

from __future__ import annotations

import threading
from typing import Iterable

_PREFIX = "iteration_planner_"

#: 直方图默认分桶（秒）
DEFAULT_BUCKETS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _label_key(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _format_labels(labels: dict[str, str] | None) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    """线程安全的指标收集器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: name -> ("counter"|"gauge"|"histogram", help, buckets)
        self._meta: dict[str, tuple[str, str, tuple[float, ...]]] = {}
        #: (name, labelkey) -> float（counter / gauge）
        self._simple: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        #: (name, labelkey) -> {"buckets": {le: count}, "sum": float, "count": int}
        self._hist: dict[tuple[str, tuple[tuple[str, str], ...]], dict] = {}

    # ------------------------------- 写入接口 -------------------------------

    def inc_counter(self, name: str, labels: dict[str, str] | None = None, value: float = 1.0, help: str = "") -> None:
        self._declare(name, "counter", help)
        key = (name, _label_key(labels))
        with self._lock:
            self._simple[key] = self._simple.get(key, 0.0) + value

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None, help: str = "") -> None:
        self._declare(name, "gauge", help)
        key = (name, _label_key(labels))
        with self._lock:
            self._simple[key] = float(value)

    def observe(
        self,
        name: str,
        value: float,
        labels: dict[str, str] | None = None,
        *,
        buckets: Iterable[float] = DEFAULT_BUCKETS,
        help: str = "",
    ) -> None:
        self._declare(name, "histogram", help, tuple(buckets))
        key = (name, _label_key(labels))
        with self._lock:
            entry = self._hist.get(key)
            if entry is None:
                entry = {"buckets": {le: 0 for le in tuple(buckets)}, "sum": 0.0, "count": 0}
                self._hist[key] = entry
            entry["sum"] += value
            entry["count"] += 1
            for le in entry["buckets"]:
                if value <= le:
                    entry["buckets"][le] += 1

    # ------------------------------- 读取接口 -------------------------------

    def render(self) -> str:
        """渲染为 Prometheus 文本格式（``text/plain; version=0.0.4``）。"""
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._meta):
                kind, help_text, _buckets = self._meta[name]
                full = _PREFIX + name
                lines.append(f"# HELP {full} {help_text or name}")
                lines.append(f"# TYPE {full} {kind}")
                if kind == "histogram":
                    lines.extend(self._render_histogram(full, name))
                else:
                    for (series_name, labelkey), value in sorted(self._simple.items()):
                        if series_name != name:
                            continue
                        lines.append(f"{full}{_format_labels(dict(labelkey))} {_fmt_num(value)}")
        return "\n".join(lines) + "\n"

    def _render_histogram(self, full: str, name: str) -> list[str]:
        out: list[str] = []
        for (series_name, labelkey), entry in sorted(self._hist.items()):
            if series_name != name:
                continue
            labels = dict(labelkey)
            cumulative = 0
            for le in sorted(entry["buckets"]):
                cumulative = entry["buckets"][le]
                bucket_labels = dict(labels)
                bucket_labels["le"] = _fmt_num(le)
                out.append(f"{full}_bucket{_format_labels(bucket_labels)} {cumulative}")
            inf_labels = dict(labels)
            inf_labels["le"] = "+Inf"
            out.append(f"{full}_bucket{_format_labels(inf_labels)} {entry['count']}")
            out.append(f"{full}_sum{_format_labels(labels)} {_fmt_num(entry['sum'])}")
            out.append(f"{full}_count{_format_labels(labels)} {entry['count']}")
        return out

    # ------------------------------- 内部实现 -------------------------------

    def _declare(self, name: str, kind: str, help_text: str, buckets: tuple[float, ...] | None = None) -> None:
        with self._lock:
            existing = self._meta.get(name)
            if existing is None:
                self._meta[name] = (kind, help_text, buckets or ())
            elif help_text and not existing[1]:
                self._meta[name] = (kind, help_text, existing[2])


def _fmt_num(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


#: 进程级单例（供各模块共享）
METRICS = Metrics()

__all__ = ["Metrics", "METRICS", "DEFAULT_BUCKETS"]