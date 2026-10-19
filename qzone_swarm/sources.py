"""聊天记录采集与素材取样。

多号隔离的关键：所有缓冲都以 ``platform_id``（AstrBot 平台实例 = 一个 QQ 号）分桶。
A 机器人看到的群消息与 B 机器人看到的群消息是两份独立数据，永远不会互相污染。

本模块不导入 astrbot。
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field

from .logging_compat import get_logger
from .models import ChatMessage

log = get_logger("sources")


@dataclass
class SourceChannel:
    kind: str  # "group" | "private"
    id: str
    label: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id}"


def channel_from(kind: str, cid: str, label: str = "") -> SourceChannel:
    return SourceChannel(kind=kind, id=str(cid).strip(), label=label)


@dataclass
class Material:
    """一次取样得到的素材。"""

    messages: list[ChatMessage] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)
    fingerprints: list[str] = field(default_factory=list)
    window_minutes: int = 0

    @property
    def count(self) -> int:
        return len(self.messages)

    def is_empty(self) -> bool:
        return not self.messages

    def channel_summary(self) -> str:
        return "、".join(self.channels) if self.channels else "无"


class MessageStore:
    """按 (platform_id, channel) 分桶的聊天记录环形缓冲。

    只保留内存中的最近若干条；不做跨号共享，也不写全局状态。
    """

    def __init__(self, *, per_channel_max: int = 300, max_channels: int = 64) -> None:
        self.per_channel_max = max(20, int(per_channel_max))
        self.max_channels = max(4, int(max_channels))
        self._buckets: dict[tuple[str, str, str], deque[ChatMessage]] = {}
        self._used: dict[str, deque[str]] = {}

    # -- 写入 --------------------------------------------------------------- #
    def record(self, message: ChatMessage, *, platform_id: str, kind: str, channel_id: str) -> None:
        """记录一条消息。platform_id 必须来自产生该消息的那个机器人自己的连接。"""
        if not platform_id or not kind or not channel_id:
            return
        key = (platform_id, kind, str(channel_id))
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self.max_channels:
                # 淘汰最久未更新的桶，避免无界增长
                oldest = min(
                    self._buckets.items(),
                    key=lambda kv: kv[1][-1].ts if kv[1] else 0.0,
                )[0]
                self._buckets.pop(oldest, None)
            bucket = deque(maxlen=self.per_channel_max)
            self._buckets[key] = bucket
        bucket.append(message)

    def clear_platform(self, platform_id: str) -> int:
        keys = [k for k in self._buckets if k[0] == platform_id]
        for key in keys:
            self._buckets.pop(key, None)
        return len(keys)

    def prune(self, *, before_ts: float) -> int:
        removed = 0
        for key in list(self._buckets.keys()):
            bucket = self._buckets[key]
            keep = [m for m in bucket if m.ts >= before_ts]
            removed += len(bucket) - len(keep)
            if keep:
                self._buckets[key] = deque(keep, maxlen=self.per_channel_max)
            else:
                self._buckets.pop(key, None)
        return removed

    # -- 读取 --------------------------------------------------------------- #
    def channel_counts(self, platform_id: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for (pid, kind, cid), bucket in self._buckets.items():
            if pid != platform_id:
                continue
            out[f"{kind}:{cid}"] = len(bucket)
        return out

    def last_seen(self, platform_id: str, kind: str, channel_id: str) -> float:
        bucket = self._buckets.get((platform_id, kind, str(channel_id)))
        return bucket[-1].ts if bucket else 0.0

    def collect(
        self,
        platform_id: str,
        channels: list[SourceChannel],
        *,
        window_seconds: float,
        limit: int,
        ignore_users: list[str] | None = None,
        exclude_bot_self: str = "",
    ) -> Material:
        """按时间窗口收集素材，返回按时间升序排列的消息。"""
        cutoff = time.time() - max(60.0, float(window_seconds))
        ignore = {str(u).strip() for u in (ignore_users or []) if str(u).strip()}
        selected: list[ChatMessage] = []
        used_channels: list[str] = []

        for channel in channels:
            bucket = self._buckets.get((platform_id, channel.kind, channel.id))
            if not bucket:
                continue
            hit = False
            for message in bucket:
                if message.ts < cutoff:
                    continue
                if message.sender_id in ignore:
                    continue
                if exclude_bot_self and message.sender_id == exclude_bot_self and message.is_bot:
                    continue
                if not message.text.strip():
                    continue
                selected.append(message)
                hit = True
            if hit:
                used_channels.append(channel.label or channel.key)

        selected.sort(key=lambda m: m.ts)
        if limit > 0 and len(selected) > limit:
            selected = selected[-limit:]

        return Material(
            messages=selected,
            channels=used_channels,
            fingerprints=[m.fingerprint() for m in selected],
            window_minutes=int(max(60.0, float(window_seconds)) // 60),
        )

    # -- 去重 --------------------------------------------------------------- #
    def mark_used(self, platform_id: str, fingerprints: list[str], *, keep: int = 2000) -> None:
        bucket = self._used.setdefault(platform_id, deque(maxlen=max(100, keep)))
        bucket.extend(fingerprints)

    def used_fingerprints(self, platform_id: str) -> set[str]:
        return set(self._used.get(platform_id, ()))

    def filter_unused(self, platform_id: str, material: Material) -> Material:
        """剔除已经用过的消息，避免反复拿同一批记录发说说。"""
        used = self.used_fingerprints(platform_id)
        if not used:
            return material
        fresh = [m for m in material.messages if m.fingerprint() not in used]
        if len(fresh) == len(material.messages):
            return material
        return Material(
            messages=fresh,
            channels=material.channels,
            fingerprints=[m.fingerprint() for m in fresh],
            window_minutes=material.window_minutes,
        )


# --------------------------------------------------------------------------- #
# 素材 -> 提示词文本
# --------------------------------------------------------------------------- #
def material_to_text(material: Material, *, max_chars: int = 6000, timezone_label: str = "") -> str:
    """把素材渲染成给模型看的聊天记录片段。"""
    if material.is_empty():
        return ""
    lines: list[str] = []
    for message in material.messages:
        name = message.sender_name or message.sender_id or "某人"
        text = " ".join(message.text.split())
        if len(text) > 300:
            text = text[:300] + "…"
        stamp = ""
        if message.session_label:
            stamp = f"[{message.session_label}] "
        lines.append(f"{stamp}{name}: {text}")
    text = "\n".join(lines)
    if max_chars and len(text) > max_chars:
        text = text[-max_chars:]
    return text


def summarize_senders(material: Material, *, limit: int = 12) -> str:
    """列出参与聊天的昵称，帮助模型把握"在和谁聊天"。"""
    seen: list[str] = []
    for message in material.messages:
        name = message.sender_name or message.sender_id
        if name and name not in seen:
            seen.append(name)
        if len(seen) >= limit:
            break
    return "、".join(seen)
