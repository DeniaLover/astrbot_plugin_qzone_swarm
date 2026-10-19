"""把 AstrBot 的插件配置转成强类型配置对象。

设计取向：**配置驱动，但不信任配置**。
所有字段都做类型归一与取值钳制，缺项有合理默认，坏项只影响它自己那一个机器人。

本模块不导入 astrbot（只消费 dict）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging_compat import get_logger
from .models import BotProfile
from .selfie import DEFAULT_ASPECT_RATIO, DEFAULT_SIZE, ImageEndpoint, SelfieConfig
from .scheduler import MAX_OFFSET_MINUTES, normalize_times

log = get_logger("config")

DEFAULT_TICK_SECONDS = 30
MIN_TICK_SECONDS = 10
MAX_TICK_SECONDS = 600

PERSONA_FILE_MAX_CHARS = 24000


# --------------------------------------------------------------------------- #
# 取值小工具
# --------------------------------------------------------------------------- #
def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "是", "开", "启用"):
        return True
    if text in ("0", "false", "no", "off", "否", "关", "禁用"):
        return False
    return default


def _as_int(value: Any, default: int, *, low: int | None = None, high: int | None = None) -> int:
    try:
        result = int(float(str(value).strip())) if not isinstance(value, (int, float)) else int(value)
    except Exception:
        result = default
    if low is not None:
        result = max(low, result)
    if high is not None:
        result = min(high, result)
    return result


def _as_float(value: Any, default: float, *, low: float | None = None, high: float | None = None) -> float:
    try:
        result = float(str(value).strip()) if not isinstance(value, (int, float)) else float(value)
    except Exception:
        result = default
    if low is not None:
        result = max(low, result)
    if high is not None:
        result = min(high, result)
    return result


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _as_str_list(value: Any) -> list[str]:
    """接受 list / 逗号或换行分隔的字符串。"""
    out: list[str] = []
    if value is None:
        return out
    items: list[Any]
    if isinstance(value, str):
        text = value.replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",")
        items = text.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    for item in items:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _as_probability(value: Any, default: float) -> float:
    result = _as_float(value, default, low=0.0, high=1.0)
    return result


# --------------------------------------------------------------------------- #
# 全局配置
# --------------------------------------------------------------------------- #
@dataclass
class GlobalConfig:
    enabled: bool = True
    admin_only: bool = True
    notify_admin_umo: str = ""
    default_provider_id: str = ""
    tick_seconds: int = DEFAULT_TICK_SECONDS
    http_timeout: float = 20.0
    user_agent: str = ""
    post_grace_minutes: int = 90
    interact_grace_minutes: int = 90
    dry_run_all: bool = False
    selfie: SelfieConfig = field(default_factory=SelfieConfig)
    bots: list[BotProfile] = field(default_factory=list)

    def enabled_bots(self) -> list[BotProfile]:
        return [b for b in self.bots if b.enabled and b.platform_id]

    def find_bot(self, key: str) -> BotProfile | None:
        """按 名称 / 平台实例 id / uin 查找机器人。"""
        if not key:
            return None
        needle = str(key).strip().lower()
        for bot in self.bots:
            if needle in {
                bot.name.lower(),
                bot.platform_id.lower(),
                bot.persona_id.lower(),
            }:
                return bot
        for bot in self.bots:
            if bot.platform_id.lower().endswith(needle) or needle in bot.name.lower():
                return bot
        return None


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _load_persona_file(path_text: str) -> str:
    if not path_text:
        return ""
    path = Path(path_text.strip().strip('"'))
    if not path.is_file():
        log.warning("人设文件不存在：%s", path)
        return ""
    # utf-8-sig 放前面：带 BOM 的文件若按 utf-8 读，正文开头会混进一个 \ufeff
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = path.read_text(encoding=encoding)
            text = text.lstrip("\ufeff")
            if len(text) > PERSONA_FILE_MAX_CHARS:
                log.info("人设文件 %s 较长（%d 字），截断到 %d 字", path, len(text), PERSONA_FILE_MAX_CHARS)
                text = text[:PERSONA_FILE_MAX_CHARS]
            return text
        except UnicodeDecodeError:
            continue
        except Exception as exc:
            log.warning("读取人设文件 %s 失败：%s", path, exc)
            return ""
    log.warning("人设文件 %s 无法解码（尝试过 utf-8/gbk）", path)
    return ""


def _parse_bot(raw: dict[str, Any], index: int) -> BotProfile | None:
    if not isinstance(raw, dict):
        return None
    platform_id = _as_str(raw.get("platform_id"))
    name = _as_str(raw.get("name")) or platform_id or f"bot{index + 1}"
    if not platform_id:
        log.warning("机器人「%s」没有填写 platform_id，已跳过", name)
        return None

    persona_file = _as_str(raw.get("persona_file"))
    persona_prompt = _as_str(raw.get("persona_prompt"))
    if not persona_prompt and persona_file:
        persona_prompt = _load_persona_file(persona_file)

    # 时间表：post_times / interact_times 允许一个字段配多个时间点
    post_times = normalize_times(raw.get("post_times"))
    interact_times = normalize_times(raw.get("interact_times"))
    if not interact_times and _as_bool(raw.get("interact_follow_post_times"), True):
        # 默认复用发说说时间，减少配置负担
        interact_times = list(post_times)

    profile = BotProfile(
        name=name,
        platform_id=platform_id,
        enabled=_as_bool(raw.get("enabled"), True),
        persona_id=_as_str(raw.get("persona_id")),
        persona_file=persona_file,
        persona_prompt=persona_prompt,
        persona_extra=_as_str(raw.get("persona_extra")),
        provider_id=_as_str(raw.get("provider_id")),
        selfie_provider=_as_str(raw.get("selfie_provider")),
        post_enabled=_as_bool(raw.get("post_enabled"), True),
        post_times=post_times,
        post_offset_minutes=_as_int(
            raw.get("post_offset_minutes"), 30, low=0, high=MAX_OFFSET_MINUTES
        ),
        post_max_per_day=_as_int(raw.get("post_max_per_day"), 3, low=0, high=50),
        use_material=_as_bool(raw.get("use_material"), True),
        material_window_minutes=_as_int(
            raw.get("material_window_minutes"), 720, low=30, high=10080
        ),
        material_max_messages=_as_int(raw.get("material_max_messages"), 200, low=10, high=2000),
        min_material_messages=_as_int(raw.get("min_material_messages"), 6, low=1, high=500),
        allow_selfie=_as_bool(raw.get("allow_selfie"), False),
        selfie_lora=_as_str(raw.get("selfie_lora")),
        selfie_lora_weight=_as_float(raw.get("selfie_lora_weight"), 0.0, low=0.0, high=1.5),
        selfie_lora_trigger=_as_str(raw.get("selfie_lora_trigger")),
        selfie_extra_style=_as_str(raw.get("selfie_extra_style")),
        selfie_negative=_as_str(raw.get("selfie_negative")),
        selfie_size=_as_str(raw.get("selfie_size")),
        source_groups=_as_str_list(raw.get("source_groups")),
        source_privates=_as_str_list(raw.get("source_privates")),
        source_ignore_users=_as_str_list(raw.get("source_ignore_users")),
        interact_enabled=_as_bool(raw.get("interact_enabled"), True),
        interact_times=interact_times,
        interact_offset_minutes=_as_int(
            raw.get("interact_offset_minutes"), 45, low=0, high=MAX_OFFSET_MINUTES
        ),
        like_enabled=_as_bool(raw.get("like_enabled"), True),
        comment_enabled=_as_bool(raw.get("comment_enabled"), True),
        reply_enabled=_as_bool(raw.get("reply_enabled"), True),
        comment_probability=_as_probability(raw.get("comment_probability"), 0.6),
        like_probability=_as_probability(raw.get("like_probability"), 0.8),
        comment_max_per_day=_as_int(raw.get("comment_max_per_day"), 8, low=0, high=200),
        like_max_per_day=_as_int(raw.get("like_max_per_day"), 20, low=0, high=300),
        reply_max_per_day=_as_int(raw.get("reply_max_per_day"), 8, low=0, high=200),
        interact_target_count=_as_int(raw.get("interact_target_count"), 3, low=1, high=20),
        interact_scope=_as_str(raw.get("interact_scope")) or "friends",
        comment_max_length=_as_int(raw.get("comment_max_length"), 60, low=4, high=300),
        interact_own_posts_only=_as_bool(raw.get("interact_own_posts_only"), False),
        dry_run=_as_bool(raw.get("dry_run"), False),
        notify_umo=_as_str(raw.get("notify_umo")),
    )

    # 手动 Cookie 属于该号自己的兜底，只放在 profile 上，绝不共享
    manual_cookie = _as_str(raw.get("manual_cookie"))
    object.__setattr__(profile, "manual_cookie", manual_cookie)
    return profile


def _parse_endpoint(raw: Any, *, label: str = "") -> ImageEndpoint | None:
    """解析一个生图端点（主端点或回退端点）。"""
    if not isinstance(raw, dict):
        return None
    extra = raw.get("extra_params")
    extra_params: dict[str, Any] = extra if isinstance(extra, dict) else {}
    endpoint = ImageEndpoint(
        label=_as_str(raw.get("label")) or label,
        mode=_as_str(raw.get("mode")) or "openai_images",
        api_base=_as_str(raw.get("api_base")),
        api_key=_as_str(raw.get("api_key")),
        model=_as_str(raw.get("model")),
        size=_as_str(raw.get("size")) or DEFAULT_SIZE,
        aspect_ratio=_as_str(raw.get("aspect_ratio")) or DEFAULT_ASPECT_RATIO,
        timeout=_as_float(raw.get("timeout"), 180.0, low=10.0, high=900.0),
        proxy=_as_str(raw.get("proxy")),
        extra_params=extra_params,
        request_template=_as_str(raw.get("request_template")),
        response_path=_as_str(raw.get("response_path")) or "data.0.b64_json",
    )
    return endpoint if endpoint.api_base else None


def _parse_selfie(raw: Any, global_defaults: dict[str, Any]) -> SelfieConfig:
    """解析生图配置。

    兼容旧配置：`enabled/mode/api_base/api_key/model/...` 这些平铺字段
    仍然表示**主端点**；新增 `fallback_providers` 表示回退链。
    """
    if not isinstance(raw, dict):
        raw = {}

    primary = _parse_endpoint(
        {
            "label": _as_str(raw.get("label")) or "主端点",
            "mode": raw.get("mode"),
            "api_base": _as_str(raw.get("api_base"))
            or _as_str(global_defaults.get("selfie_api_base")),
            "api_key": raw.get("api_key"),
            "model": raw.get("model"),
            "size": raw.get("size"),
            "aspect_ratio": raw.get("aspect_ratio"),
            "timeout": raw.get("timeout"),
            "proxy": raw.get("proxy"),
            "extra_params": raw.get("extra_params"),
            "request_template": raw.get("request_template"),
            "response_path": raw.get("response_path"),
        }
    ) or ImageEndpoint()

    fallbacks: list[ImageEndpoint] = []
    raw_fallbacks = raw.get("fallback_providers")
    if isinstance(raw_fallbacks, list):
        for index, item in enumerate(raw_fallbacks):
            endpoint = _parse_endpoint(item, label=f"回退{index + 1}")
            if endpoint is not None:
                fallbacks.append(endpoint)

    return SelfieConfig(
        enabled=_as_bool(raw.get("enabled"), False),
        primary=primary,
        fallbacks=fallbacks,
        prompt_library=_as_str(raw.get("prompt_library")),
        negative_prompt=_as_str(raw.get("negative_prompt")),
        use_reference=_as_bool(raw.get("use_reference"), True),
        scene_first=_as_bool(raw.get("scene_first"), True),
        artist_style=_as_str(raw.get("artist_style")) or "random",
        variation=_as_float(raw.get("variation"), 0.85, low=0.0, high=1.0),
        no_repeat_window=int(_as_float(raw.get("no_repeat_window"), 2.0, low=1.0, high=10.0)),
        use_prompt_constraint=_as_bool(raw.get("use_prompt_constraint"), True),
        anatomy_check=_as_bool(raw.get("anatomy_check"), False),
        anatomy_retries=int(_as_float(raw.get("anatomy_retries"), 1.0, low=0.0, high=3.0)),
        reference_max=int(_as_float(raw.get("reference_max"), 4.0, low=1.0, high=10.0)),
        lora_enabled=_as_bool(raw.get("lora_enabled"), True),
        use_trigger=_as_bool(raw.get("use_trigger"), True),
        lora=_as_str(raw.get("lora")),
        lora_weight=_as_float(raw.get("lora_weight"), 0.0, low=0.0, high=1.5),
        lora_trigger=_as_str(raw.get("lora_trigger")),
    )


def parse_config(raw: Any) -> GlobalConfig:
    """把插件配置 dict 转成 GlobalConfig。"""
    if not isinstance(raw, dict):
        raw = {}

    advanced = raw.get("advanced") if isinstance(raw.get("advanced"), dict) else {}

    config = GlobalConfig(
        enabled=_as_bool(raw.get("enabled"), True),
        admin_only=_as_bool(raw.get("admin_only"), True),
        notify_admin_umo=_as_str(raw.get("notify_admin_umo")),
        default_provider_id=_as_str(raw.get("default_provider_id")),
        tick_seconds=_as_int(
            advanced.get("tick_seconds"),
            DEFAULT_TICK_SECONDS,
            low=MIN_TICK_SECONDS,
            high=MAX_TICK_SECONDS,
        ),
        http_timeout=_as_float(advanced.get("http_timeout"), 20.0, low=5.0, high=180.0),
        user_agent=_as_str(advanced.get("user_agent")),
        post_grace_minutes=_as_int(advanced.get("post_grace_minutes"), 90, low=5, high=1440),
        interact_grace_minutes=_as_int(advanced.get("interact_grace_minutes"), 90, low=5, high=1440),
        dry_run_all=_as_bool(raw.get("dry_run_all"), False),
    )

    config.selfie = _parse_selfie(raw.get("selfie"), raw)

    raw_bots = raw.get("bots")
    bots: list[BotProfile] = []
    if isinstance(raw_bots, list):
        for index, item in enumerate(raw_bots):
            profile = _parse_bot(item, index)
            if profile is None:
                continue
            if any(existing.platform_id == profile.platform_id for existing in bots):
                log.warning("平台实例 %s 重复配置，只保留第一个", profile.platform_id)
                continue
            bots.append(profile)
    config.bots = bots

    log.info(
        "配置载入：总开关=%s 机器人=%d(启用%d) 自拍=%s",
        config.enabled,
        len(bots),
        len(config.enabled_bots()),
        "开" if config.selfie.enabled else "关",
    )
    return config


def profile_from_dict(raw: dict[str, Any]) -> BotProfile | None:
    """供命令/测试直接构造单个 profile。"""
    return _parse_bot(raw, 0)


def bot_manual_cookie(profile: BotProfile) -> str:
    return getattr(profile, "manual_cookie", "") or ""
