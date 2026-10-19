"""多时间点定时（纯函数，便于离线测试）。

设计：
    * 每个机器人有自己的时间表（可以配多个 HH:MM）。
    * 每个槽位带一个**确定性**的随机偏移（由 平台id+日期+时间 派生），
      这样同一槽位在多次轮询中算出的触发时刻是稳定的，不会反复重掷；
      同时天然把多个机器人错开，避免几号在同一秒一起动作。
    * 触发窗口有宽限期：程序停机错过太久就不再补发，避免重启后连发。
    * 槽位以 日期+时间 为键记录已执行，天然跨天重置。

本模块不导入 astrbot。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from .logging_compat import get_logger

from .util import parse_hhmm

log = get_logger("scheduler")

DEFAULT_GRACE_MINUTES = 90
MAX_OFFSET_MINUTES = 120


@dataclass
class SlotPlan:
    """一个到点的调度槽位。"""

    key: str
    kind: str
    hour: int
    minute: int
    due_at: datetime
    jitter_seconds: int = 0

    def describe(self) -> str:
        return f"{self.kind}@{self.hour:02d}:{self.minute:02d}(+{self.jitter_seconds // 60}min)"


def normalize_times(values: object) -> list[tuple[int, int]]:
    """把配置里的时间列表转成 [(h, m)]，去重并排序。

    支持 "HH:MM"、列表、以及逗号/分号/顿号分隔的字符串。
    """
    raw: list[str] = []
    if values is None:
        return []
    if isinstance(values, str):
        for chunk in values.replace("；", ",").replace(";", ",").replace("、", ",").split(","):
            if chunk.strip():
                raw.append(chunk.strip())
    elif isinstance(values, (list, tuple, set)):
        for item in values:
            if isinstance(item, str):
                for chunk in item.replace("；", ",").replace(";", ",").replace("、", ",").split(","):
                    if chunk.strip():
                        raw.append(chunk.strip())
            else:
                raw.append(str(item).strip())
    else:
        raw.append(str(values).strip())

    parsed: set[tuple[int, int]] = set()
    for text in raw:
        result = parse_hhmm(text)
        if result is None:
            log.warning("忽略无法解析的时间项：%r（应为 HH:MM）", text)
            continue
        parsed.add(result)
    return sorted(parsed)


def _seed_digest(*parts: object) -> int:
    payload = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def jitter_seconds(
    platform_id: str,
    kind: str,
    date_key: str,
    hour: int,
    minute: int,
    offset_minutes: int,
) -> int:
    """确定性偏移：同一个槽位每次算出的值都一样。"""
    offset_minutes = max(0, min(int(offset_minutes or 0), MAX_OFFSET_MINUTES))
    if offset_minutes <= 0:
        return 0
    span = offset_minutes * 60
    digest = _seed_digest(platform_id, kind, date_key, hour, minute)
    return int(digest % (span + 1))


def slot_key(kind: str, date_key: str, hour: int, minute: int) -> str:
    return f"{kind}:{date_key}:{hour:02d}:{minute:02d}"


def plan_slots(
    now: datetime,
    times: list[tuple[int, int]],
    *,
    kind: str,
    platform_id: str,
    offset_minutes: int = 0,
) -> list[SlotPlan]:
    """算出"今天"全部槽位的计划触发时刻。"""
    date_key = now.strftime("%Y-%m-%d")
    plans: list[SlotPlan] = []
    for hour, minute in times:
        base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        jitter = jitter_seconds(platform_id, kind, date_key, hour, minute, offset_minutes)
        plans.append(
            SlotPlan(
                key=slot_key(kind, date_key, hour, minute),
                kind=kind,
                hour=hour,
                minute=minute,
                due_at=base + timedelta(seconds=jitter),
                jitter_seconds=jitter,
            )
        )
    return plans


def due_slots(
    now: datetime,
    times: list[tuple[int, int]],
    *,
    kind: str,
    platform_id: str,
    fired_keys: set[str] | None = None,
    offset_minutes: int = 0,
    grace_minutes: int = DEFAULT_GRACE_MINUTES,
) -> list[SlotPlan]:
    """返回当前真正应该执行的槽位（已扣除已执行、过早、过期的）。"""
    fired = fired_keys or set()
    grace = timedelta(minutes=max(1, int(grace_minutes)))
    out: list[SlotPlan] = []
    for plan in plan_slots(now, times, kind=kind, platform_id=platform_id, offset_minutes=offset_minutes):
        if plan.key in fired:
            continue
        if now < plan.due_at:
            continue
        if now - plan.due_at > grace:
            # 停机太久，直接跳过以免重启后补发一堆
            continue
        out.append(plan)
    return out


def skipped_slots(
    now: datetime,
    times: list[tuple[int, int]],
    *,
    kind: str,
    platform_id: str,
    fired_keys: set[str] | None = None,
    offset_minutes: int = 0,
    grace_minutes: int = DEFAULT_GRACE_MINUTES,
) -> list[SlotPlan]:
    """已过期且不会再执行的槽位（用于标记，避免每轮重复计算）。"""
    fired = fired_keys or set()
    grace = timedelta(minutes=max(1, int(grace_minutes)))
    out: list[SlotPlan] = []
    for plan in plan_slots(now, times, kind=kind, platform_id=platform_id, offset_minutes=offset_minutes):
        if plan.key in fired:
            continue
        if now >= plan.due_at and now - plan.due_at > grace:
            out.append(plan)
    return out


def next_run(
    now: datetime,
    times: list[tuple[int, int]],
    *,
    kind: str,
    platform_id: str,
    offset_minutes: int = 0,
) -> SlotPlan | None:
    """下一次将要执行的槽位（跨天查找），用于状态展示。"""
    candidates: list[SlotPlan] = []
    for day_offset in (0, 1):
        reference = now + timedelta(days=day_offset)
        for plan in plan_slots(reference, times, kind=kind, platform_id=platform_id, offset_minutes=offset_minutes):
            if plan.due_at > now:
                candidates.append(plan)
    if not candidates:
        return None
    return min(candidates, key=lambda p: p.due_at)


def describe_schedule(
    now: datetime,
    times: list[tuple[int, int]],
    *,
    platform_id: str,
    post_offset: int = 0,
    interact_offset: int = 0,
) -> str:
    """给状态命令用的一行摘要。"""
    if not times:
        return "未配置时间"
    parts: list[str] = []
    for kind, offset in (("post", post_offset), ("interact", interact_offset)):
        upcoming = next_run(now, times, kind=kind, platform_id=platform_id, offset_minutes=offset)
        if upcoming:
            parts.append(f"{kind} → {upcoming.due_at.strftime('%m-%d %H:%M')}")
    return " | ".join(parts) if parts else "未配置时间"
