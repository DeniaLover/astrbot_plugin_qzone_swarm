"""数据结构定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Credentials:
    """某一个机器人的 QQ空间登录态。**绝不在机器人之间共用**。"""

    platform_id: str = ""
    uin: int = 0
    nickname: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    gtk: int = 0
    source: str = ""
    fetched_at: float = 0.0

    @property
    def valid(self) -> bool:
        return bool(self.uin and self.gtk and self.cookies)

    def cookie_header(self) -> str:
        from .creds import cookie_header

        return cookie_header(self.cookies)

    def summary(self) -> str:
        """不含任何机密值的可读摘要，可安全写日志。"""
        if not self.cookies:
            return "未绑定"
        keys = ",".join(sorted(self.cookies.keys()))
        return (
            f"uin={self.uin} 昵称={self.nickname or '?'} "
            f"cookie项={len(self.cookies)} g_tk={'有' if self.gtk else '无'} "
            f"来源={self.source or '-'} [{keys}]"
        )


@dataclass
class ChatMessage:
    """一条被采集到的聊天记录。"""

    ts: float
    sender_id: str
    sender_name: str
    text: str
    session_key: str = ""
    session_label: str = ""
    is_bot: bool = False
    msg_id: str = ""

    def fingerprint(self) -> str:
        return self.msg_id or f"{self.session_key}:{self.ts:.3f}:{self.sender_id}:{self.text[:24]}"


@dataclass
class FeedComment:
    comment_id: str = ""
    uin: int = 0
    name: str = ""
    content: str = ""
    created_at: float = 0.0
    reply_to_uin: int = 0
    reply_to_name: str = ""


@dataclass
class FeedItem:
    """一条说说。"""

    hostuin: int = 0
    fid: str = ""
    appid: int = 311
    content: str = ""
    created_at: float = 0.0
    nickname: str = ""
    comments: list[FeedComment] = field(default_factory=list)
    curkey: str = ""
    unikey: str = ""
    like_count: int = 0
    liked: bool = False
    comment_count: int = 0
    image_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_checkin(self) -> bool:
        return self.appid not in (311, 0)


@dataclass
class PostRecord:
    """本号发出的说说记录。"""

    fid: str = ""
    uin: int = 0
    content: str = ""
    created_at: float = 0.0
    material_sessions: list[str] = field(default_factory=list)
    used_material: bool = False
    selfie_prompt: str = ""
    selfie_used: bool = False


@dataclass
class InteractionRecord:
    """已经做过的互动，用于去重与配额。"""

    kind: str = ""  # like / comment / reply
    hostuin: int = 0
    fid: str = ""
    comment_id: str = ""
    content: str = ""
    ts: float = 0.0


@dataclass
class BotProfile:
    """单个机器人的全部配置。每个机器人一份，互不影响。"""

    name: str = ""
    platform_id: str = ""
    enabled: bool = True

    # 人设
    persona_id: str = ""
    persona_file: str = ""
    persona_prompt: str = ""
    persona_extra: str = ""

    # 模型
    provider_id: str = ""
    selfie_provider: str = ""

    # 说说
    post_enabled: bool = True
    # 时间表统一存成 [(hour, minute)]，排序去重后由 config 归一化。
    post_times: list[tuple[int, int]] = field(default_factory=list)
    post_offset_minutes: int = 0
    post_max_per_day: int = 3
    use_material: bool = True
    material_window_minutes: int = 720
    material_max_messages: int = 200
    min_material_messages: int = 6
    allow_selfie: bool = False
    # 该号专用的 LoRA（留空则用提示词库里按人设登记的）
    selfie_lora: str = ""
    selfie_lora_weight: float = 0.0
    selfie_lora_trigger: str = ""
    selfie_extra_style: str = ""
    selfie_negative: str = ""
    selfie_size: str = ""
    post_visibility: str = "public"

    # 素材来源（群聊/私聊）
    source_groups: list[str] = field(default_factory=list)
    source_privates: list[str] = field(default_factory=list)
    source_ignore_users: list[str] = field(default_factory=list)

    # 互动
    interact_enabled: bool = True
    interact_times: list[tuple[int, int]] = field(default_factory=list)
    interact_offset_minutes: int = 0
    like_enabled: bool = True
    comment_enabled: bool = True
    reply_enabled: bool = True
    comment_probability: float = 0.6
    like_probability: float = 0.8
    comment_max_per_day: int = 8
    like_max_per_day: int = 20
    reply_max_per_day: int = 8
    interact_target_count: int = 3
    interact_scope: str = "friends"  # friends | self
    comment_max_length: int = 60
    interact_own_posts_only: bool = False

    # 安全
    dry_run: bool = False
    notify_umo: str = ""

    def resolved_persona_name(self) -> str:
        return self.persona_id or self.name or self.platform_id or "默认"

    @staticmethod
    def format_times(times: list[tuple[int, int]] | None) -> str:
        """把 [(h, m)] 渲染成 "14:30、21:00"，供日志与状态展示。"""
        if not times:
            return ""
        return "、".join(f"{int(h):02d}:{int(m):02d}" for h, m in times)

    def post_times_text(self) -> str:
        return self.format_times(self.post_times)

    def interact_times_text(self) -> str:
        return self.format_times(self.interact_times)

    def enabled_sources(self) -> list[tuple[str, str]]:
        """返回 [(kind, id)]，kind 为 group / private。"""
        out: list[tuple[str, str]] = []
        for gid in self.source_groups:
            gid = str(gid).strip()
            if gid:
                out.append(("group", gid))
        for uid in self.source_privates:
            uid = str(uid).strip()
            if uid:
                out.append(("private", uid))
        return out
