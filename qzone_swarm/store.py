"""每个机器人一份的持久化状态。

文件布局::

    <plugin_data>/astrbot_plugin_qzone_swarm/
        bot_<platform_id>.json

每个机器人只读写自己的文件。任何跨号共享的写入都视为缺陷。
本模块不导入 astrbot。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .logging_compat import get_logger
from .models import InteractionRecord, PostRecord
from .util import today_key

log = get_logger("store")

STATE_VERSION = 1
MAX_POSTS_KEPT = 100
MAX_INTERACTIONS_KEPT = 500
MAX_MATERIAL_FINGERPRINTS = 3000
MAX_FIRED_KEYS = 400


def _safe_name(text: str) -> str:
    keep = [c if (c.isalnum() or c in "-_.") else "_" for c in str(text)]
    name = "".join(keep).strip("._")
    return name or "unknown"


class BotStore:
    """单个机器人的状态容器（内存 + 落盘）。"""

    def __init__(self, data_dir: str | os.PathLike[str], platform_id: str) -> None:
        self.data_dir = Path(data_dir)
        self.platform_id = str(platform_id)
        self.path = self.data_dir / f"bot_{_safe_name(self.platform_id)}.json"
        self.data: dict[str, Any] = self._defaults()
        self._dirty = False

    def _defaults(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "platform_id": self.platform_id,
            "uin": 0,
            "nickname": "",
            "posts": [],
            "interactions": [],
            "daily": {},
            "fired_slots": [],
            "material_fingerprints": [],
            "last_post_at": 0.0,
            "last_interact_at": 0.0,
            "stats": {"posts": 0, "comments": 0, "replies": 0, "likes": 0, "errors": 0},
        }

    # -- 读写 --------------------------------------------------------------- #
    def load(self) -> "BotStore":
        try:
            if self.path.exists():
                with self.path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                if isinstance(payload, dict):
                    base = self._defaults()
                    base.update(payload)
                    self.data = base
        except Exception as exc:
            log.warning("[%s] 读取状态文件失败（将重建）：%s", self.platform_id, exc)
        return self

    def save(self, *, force: bool = False) -> bool:
        if not self._dirty and not force:
            return False
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            # 原子写：先写临时文件再替换，避免半截文件
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=str(self.data_dir),
                prefix=self.path.name + ".", suffix=".tmp", delete=False,
            )
            try:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            os.replace(handle.name, self.path)
            self._dirty = False
            return True
        except Exception as exc:
            log.error("[%s] 保存状态文件失败：%s", self.platform_id, exc)
            return False

    def mark_dirty(self) -> None:
        self._dirty = True

    # -- 身份 --------------------------------------------------------------- #
    def set_identity(self, uin: int, nickname: str = "") -> None:
        if self.data.get("uin") != int(uin or 0):
            self.data["uin"] = int(uin or 0)
            self._dirty = True
        if nickname and self.data.get("nickname") != nickname:
            self.data["nickname"] = nickname
            self._dirty = True

    @property
    def uin(self) -> int:
        return int(self.data.get("uin") or 0)

    # -- 说说 --------------------------------------------------------------- #
    def record_post(self, record: PostRecord) -> None:
        posts = self.data.setdefault("posts", [])
        posts.append(
            {
                "fid": record.fid,
                "uin": record.uin,
                "content": record.content,
                "created_at": record.created_at or time.time(),
                "material_sessions": record.material_sessions,
                "used_material": record.used_material,
                "selfie_prompt": record.selfie_prompt,
                "selfie_used": record.selfie_used,
            }
        )
        if len(posts) > MAX_POSTS_KEPT:
            del posts[:-MAX_POSTS_KEPT]
        self.data["last_post_at"] = time.time()
        self._bump_stat("posts")
        self._dirty = True

    def recent_posts(self, *, limit: int = 10) -> list[dict[str, Any]]:
        posts = self.data.get("posts") or []
        return list(posts[-max(1, int(limit)):])

    def has_posted_fid(self, fid: str) -> bool:
        return any(p.get("fid") == fid for p in (self.data.get("posts") or []))

    # -- 互动 --------------------------------------------------------------- #
    def record_interaction(self, record: InteractionRecord) -> None:
        interactions = self.data.setdefault("interactions", [])
        interactions.append(
            {
                "kind": record.kind,
                "hostuin": record.hostuin,
                "fid": record.fid,
                "comment_id": record.comment_id,
                "content": record.content,
                "ts": record.ts or time.time(),
            }
        )
        if len(interactions) > MAX_INTERACTIONS_KEPT:
            del interactions[:-MAX_INTERACTIONS_KEPT]
        if record.kind in ("comment", "reply", "like"):
            self._bump_stat(f"{record.kind}s")
        self.data["last_interact_at"] = time.time()
        self._dirty = True

    def has_interacted(self, kind: str, hostuin: int, fid: str, comment_id: str = "") -> bool:
        for item in reversed(self.data.get("interactions") or []):
            if item.get("kind") != kind:
                continue
            if int(item.get("hostuin") or 0) != int(hostuin):
                continue
            if str(item.get("fid") or "") != str(fid):
                continue
            if comment_id and str(item.get("comment_id") or "") != str(comment_id):
                continue
            return True
        return False

    def interacted_fids(self, kind: str) -> set[tuple[int, str]]:
        out: set[tuple[int, str]] = set()
        for item in self.data.get("interactions") or []:
            if item.get("kind") == kind:
                out.add((int(item.get("hostuin") or 0), str(item.get("fid") or "")))
        return out

    # -- 每日配额 ----------------------------------------------------------- #
    def _daily_bucket(self, day: str | None = None) -> dict[str, int]:
        day = day or today_key()
        daily = self.data.setdefault("daily", {})
        bucket = daily.get(day)
        if not isinstance(bucket, dict):
            bucket = {}
            daily[day] = bucket
        # 只保留最近 7 天
        if len(daily) > 7:
            for key in sorted(daily.keys())[:-7]:
                daily.pop(key, None)
        return bucket

    def count_today(self, kind: str, *, day: str | None = None) -> int:
        return int(self._daily_bucket(day).get(kind, 0) or 0)

    def bump_today(self, kind: str, *, day: str | None = None, amount: int = 1) -> int:
        bucket = self._daily_bucket(day)
        bucket[kind] = int(bucket.get(kind, 0) or 0) + int(amount)
        self._dirty = True
        return bucket[kind]

    def try_consume_quota(self, kind: str, limit: int, *, day: str | None = None) -> bool:
        """配额够就占用一格并返回 True。"""
        if limit <= 0:
            return False
        bucket = self._daily_bucket(day)
        used = int(bucket.get(kind, 0) or 0)
        if used >= limit:
            return False
        bucket[kind] = used + 1
        self._dirty = True
        return True

    # -- 调度槽位 ----------------------------------------------------------- #
    def is_slot_fired(self, key: str) -> bool:
        return key in set(self.data.get("fired_slots") or [])

    def mark_slot_fired(self, key: str) -> None:
        fired = self.data.setdefault("fired_slots", [])
        if key not in fired:
            fired.append(key)
        if len(fired) > MAX_FIRED_KEYS:
            del fired[:-MAX_FIRED_KEYS]
        self._dirty = True

    # -- 素材去重 ----------------------------------------------------------- #
    def material_used(self) -> set[str]:
        return set(self.data.get("material_fingerprints") or [])

    def mark_material_used(self, fingerprints: list[str]) -> None:
        if not fingerprints:
            return
        bucket = self.data.setdefault("material_fingerprints", [])
        existing = set(bucket)
        for item in fingerprints:
            if item not in existing:
                bucket.append(item)
                existing.add(item)
        if len(bucket) > MAX_MATERIAL_FINGERPRINTS:
            del bucket[:-MAX_MATERIAL_FINGERPRINTS]
        self._dirty = True

    # -- 统计 --------------------------------------------------------------- #
    def _bump_stat(self, key: str, amount: int = 1) -> None:
        stats = self.data.setdefault("stats", {})
        stats[key] = int(stats.get(key, 0) or 0) + int(amount)
        self._dirty = True

    def bump_error(self) -> None:
        self._bump_stat("errors")

    @property
    def stats(self) -> dict[str, int]:
        return dict(self.data.get("stats") or {})

    def summary(self) -> dict[str, Any]:
        return {
            "uin": self.uin,
            "nickname": self.data.get("nickname") or "",
            "posts_kept": len(self.data.get("posts") or []),
            "interactions_kept": len(self.data.get("interactions") or []),
            "today": dict(self._daily_bucket()),
            "stats": self.stats,
            "last_post_at": self.data.get("last_post_at") or 0.0,
            "last_interact_at": self.data.get("last_interact_at") or 0.0,
        }
