"""从 OneBot 协议端获取 **某一个机器人** 的 QQ空间登录态。

设计要点（多号隔离的核心）：

* 每个机器人在 AstrBot 里是一个独立的平台实例（各自的 aiocqhttp 连接）。
  本模块只接受"这个机器人自己的" bot 客户端，因此拿到的 Cookie 天然是它自己的，
  不会也不可能读到别的号的凭据。
* SnowLuma 实现了 OneBot 的 ``get_credentials``（返回 cookie 串 + bkn），
  以及常见的 ``get_cookies``。两者都找不到时才判定为需要重新绑定。
* 拿不到 Cookie 时**绝不回退到全局共享配置**——那正是"串号/顶号"的根源。

本模块不导入 astrbot。
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from typing import Any

from .logging_compat import get_logger
from .models import Credentials
from .util import now

log = get_logger("creds")

DEFAULT_DOMAIN = "user.qzone.qq.com"
DOMAIN_FALLBACKS = ("user.qzone.qq.com", "qzone.qq.com", "h5.qzone.qq.com", "mobile.qzone.qq.com")

COOKIE_ACTIONS = ("get_credentials", "get_cookies")
LOGIN_INFO_ACTIONS = ("get_login_info",)

COOKIE_SECRET_KEYS = ("p_skey", "skey", "pskey", "skey2")
COOKIE_GTK_KEYS = ("g_tk", "gtk", "bkn", "csrf_token")
COOKIE_KEY_ALIASES = {
    "p_skey": "p_skey",
    "pskey": "p_skey",
    "gtk": "g_tk",
    "bkn": "g_tk",
    "csrf_token": "g_tk",
}

# OneBot 各实现对 Cookie 命名不完全一致，这里只保留 QQ空间真正会用到的字段。
COOKIE_NAME_ALLOWLIST = {
    "uin", "p_uin", "skey", "p_skey", "pt4_token", "pt_key", "pt_login_sig",
    "clientkey", "superkey", "qzonetoken", "qm_keyst", "qm_sid", "o_cookie",
    "uin_cookie", "skey2", "rv2", "ptcz", "lskey", "ldw", "g_tk", "gtk",
    "bkn", "csrf_token", "pskey", "qqmusic_key", "ptui_loginuin", "luin",
}
COOKIE_META_KEYS = {
    "domain", "path", "expires", "max_age", "secure", "httponly", "ret",
    "code", "status", "message", "msg", "error", "errno", "errcode",
    "success", "token", "cookies_str",
}

_COOKIE_PAIR_RE = re.compile(
    r"\b(?:uin|p_uin|skey|p_skey|skey2|p_skey|pskey|pt4_token|pt_key|clientkey|"
    r"superkey|qzonetoken|qm_keyst|qm_sid|o_cookie|uin_cookie|rv2|ptcz|lskey|ldw|"
    r"g_tk|gtk|bkn|csrf_token|ptui_loginuin|luin)\s*=",
    re.I,
)

ACTION_TIMEOUT_SECONDS = 8.0


# --------------------------------------------------------------------------- #
# Cookie / g_tk 基础运算
# --------------------------------------------------------------------------- #
def hash33(key: str, phash: int = 0) -> int:
    """腾讯通用的 hash33，用于由 skey 推导 g_tk。"""
    for char in key:
        phash += (phash << 5) + ord(char)
    return 0x7FFFFFFF & phash


def gtk_from_skey(skey: str | None) -> int:
    if not skey:
        return 0
    return hash33(skey, phash=5381)


def normalize_cookie_fields(cookies: dict[str, Any]) -> dict[str, str]:
    """归一化 Cookie 键名，并补全 uin/p_uin 别名。"""
    normalized: dict[str, str] = {}
    for key, value in (cookies or {}).items():
        if value in (None, ""):
            continue
        original = str(key).strip()
        if not original:
            continue
        cookie_value = str(value).strip().strip('"')
        if not cookie_value:
            continue
        alias_key = original.lower().replace("-", "_")
        canonical = COOKIE_KEY_ALIASES.get(alias_key, original)
        normalized.setdefault(original, cookie_value)
        normalized.setdefault(canonical, cookie_value)

    if "uin" in normalized and "p_uin" not in normalized:
        normalized["p_uin"] = normalized["uin"]
    if "p_uin" in normalized and "uin" not in normalized:
        normalized["uin"] = normalized["p_uin"]
    return normalized


def parse_cookie_text(cookie_text: str) -> dict[str, str]:
    """解析 Cookie 头 / JSON 对象 / OneBot 返回的 cookie 串。"""
    if not cookie_text:
        return {}
    text = str(cookie_text).strip()
    if not text:
        return {}
    if text.startswith("{") or text.startswith("["):
        try:
            payload = json.loads(text)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            return normalize_cookie_fields(payload)
        if isinstance(payload, list):
            merged: dict[str, str] = {}
            for item in payload:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("key")
                    if name:
                        merged.setdefault(str(name), item.get("value"))
            return normalize_cookie_fields(merged)

    text = text.replace("\n", ";").replace("\r", ";")
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()

    cookies: dict[str, str] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"')
        if key:
            cookies[key] = value
    return normalize_cookie_fields(cookies)


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def normalize_uin(cookies: dict[str, str], override: int | None = None) -> int:
    if override:
        return int(override)
    for key in ("uin", "p_uin", "ptui_loginuin", "luin"):
        candidate = cookies.get(key)
        if not candidate:
            continue
        cleaned = str(candidate).strip().lstrip("oO")
        if cleaned.isdigit():
            return int(cleaned)
    return 0


def cookie_gtk(cookies: dict[str, str]) -> int:
    """优先由 skey 推导，其次使用协议端直接给出的 bkn。"""
    normalized = normalize_cookie_fields(cookies)
    for key in COOKIE_SECRET_KEYS:
        value = normalized.get(key)
        if value:
            return gtk_from_skey(value)
    for key in COOKIE_GTK_KEYS:
        value = normalized.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text.isdigit():
            return int(text)
    return 0


def has_auth_cookie(cookies: dict[str, str]) -> bool:
    normalized = normalize_cookie_fields(cookies)
    return any(normalized.get(k) for k in ("p_skey", "skey", "skey2"))


# --------------------------------------------------------------------------- #
# 从 OneBot 响应里抽取信息（各协议端结构差异很大，尽量宽容）
# --------------------------------------------------------------------------- #
def _is_cookie_string(value: str) -> bool:
    text = value.strip()
    return bool(text) and "=" in text and bool(_COOKIE_PAIR_RE.search(text))


def _mapping_to_cookie_text(mapping: dict[str, Any]) -> str:
    items: list[str] = []
    for key, value in mapping.items():
        name = str(key).strip()
        if not name:
            continue
        lower = name.lower()
        if lower in COOKIE_META_KEYS:
            continue
        if lower not in COOKIE_NAME_ALLOWLIST:
            continue
        if value in (None, ""):
            continue
        if isinstance(value, (str, int, float, bool)):
            items.append(f"{name}={value}")
    return "; ".join(items)


def _cookie_pair_object_to_text(value: Any) -> str:
    """处理 ``{"name": "p_skey", "value": "..."}`` 形态的单条 Cookie。

    部分 OneBot 实现的 ``get_cookies`` 返回的是这种对象（或对象列表）。
    """
    if not isinstance(value, dict):
        return ""
    name = value.get("name") or value.get("key") or value.get("Name")
    cookie_value = value.get("value")
    if cookie_value is None:
        cookie_value = value.get("Value")
    if not name or cookie_value in (None, ""):
        return ""
    normalized = str(name).strip()
    if not normalized or normalized.lower() in COOKIE_META_KEYS:
        return ""
    if isinstance(cookie_value, (dict, list, tuple)):
        return ""
    return f"{normalized}={cookie_value}"


def _merge_cookie_texts(parts: list[str]) -> str:
    merged: dict[str, str] = {}
    for part in parts:
        if not part:
            continue
        for key, value in parse_cookie_text(part).items():
            merged.setdefault(key, value)
    return cookie_header(merged) if merged else ""


def extract_cookie_text(payload: Any, *, _depth: int = 0, _seen: set[int] | None = None) -> str:
    """从任意 OneBot 响应结构中抽取 Cookie 串。"""
    if _seen is None:
        _seen = set()
    if payload is None or _depth > 8:
        return ""
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except Exception:
            return ""
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return ""
        if text.startswith("{") or text.startswith("["):
            try:
                return extract_cookie_text(json.loads(text), _depth=_depth + 1, _seen=_seen)
            except Exception:
                return ""
        return text if _is_cookie_string(text) else ""
    if isinstance(payload, (list, tuple)):
        parts = [extract_cookie_text(i, _depth=_depth + 1, _seen=_seen) for i in payload]
        return _merge_cookie_texts(parts)
    if not isinstance(payload, dict):
        return ""

    obj_id = id(payload)
    if obj_id in _seen:
        return ""
    _seen.add(obj_id)

    # 单条 Cookie 对象：{"name": "p_skey", "value": "..."}
    pair_text = _cookie_pair_object_to_text(payload)
    if pair_text:
        return pair_text

    # NapCat / SnowLuma 风格：{"cookies": "a=b; c=d", "token": 123, "csrf_token": 123}
    mapped = _mapping_to_cookie_text(payload)
    parts: list[str] = []
    if mapped:
        parts.append(mapped)

    for key in ("cookies", "cookie", "cookie_text", "cookie_str", "data", "result",
                "retdata", "ret_data", "payload", "response"):
        if key in payload:
            text = extract_cookie_text(payload.get(key), _depth=_depth + 1, _seen=_seen)
            if text:
                parts.append(text)

    for value in payload.values():
        if isinstance(value, (dict, list, tuple, str, bytes)):
            text = extract_cookie_text(value, _depth=_depth + 1, _seen=_seen)
            if text:
                parts.append(text)
    return _merge_cookie_texts(parts)


def extract_int(payload: Any, keys: tuple[str, ...], *, _depth: int = 0) -> int:
    """递归查找第一个整数字段，用于取 token / uin / user_id。"""
    if payload is None or _depth > 8:
        return 0
    if isinstance(payload, bool):
        return 0
    if isinstance(payload, (int, float)):
        value = int(payload)
        return value if value > 0 else 0
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except Exception:
            return 0
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return 0
        if text.startswith("{") or text.startswith("["):
            try:
                return extract_int(json.loads(text), keys, _depth=_depth + 1)
            except Exception:
                return 0
        cleaned = text.lstrip("oO")
        return int(cleaned) if cleaned.isdigit() else 0
    if isinstance(payload, (list, tuple)):
        for item in payload:
            value = extract_int(item, keys, _depth=_depth + 1)
            if value:
                return value
        return 0
    if not isinstance(payload, dict):
        return 0

    for key in keys:
        if key in payload:
            value = extract_int(payload.get(key), keys, _depth=_depth + 1)
            if value:
                return value
    for value in payload.values():
        if isinstance(value, (dict, list, tuple, str, bytes, int, float)):
            found = extract_int(value, keys, _depth=_depth + 1)
            if found:
                return found
    return 0


def extract_str(payload: Any, keys: tuple[str, ...], *, _depth: int = 0) -> str:
    if payload is None or _depth > 8:
        return ""
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except Exception:
            return ""
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return extract_str(json.loads(text), keys, _depth=_depth + 1)
            except Exception:
                return ""
        return text
    if isinstance(payload, (list, tuple)):
        for item in payload:
            found = extract_str(item, keys, _depth=_depth + 1)
            if found:
                return found
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        if key in payload:
            found = extract_str(payload.get(key), keys, _depth=_depth + 1)
            if found:
                return found
    for value in payload.values():
        if isinstance(value, (dict, list, tuple, str, bytes)):
            found = extract_str(value, keys, _depth=_depth + 1)
            if found:
                return found
    return ""


# --------------------------------------------------------------------------- #
# OneBot action 调用
# --------------------------------------------------------------------------- #
async def call_action(bot: Any, action: str, **params: Any) -> Any:
    """调用 OneBot action，兼容 aiocqhttp 的多种调用入口。

    只使用传入的 ``bot``（即某一个机器人自己的连接），因此不存在跨号风险。
    """
    errors: list[str] = []

    async def _maybe_await(result: Any) -> Any:
        if inspect.isawaitable(result):
            return await result
        return result

    # 1) aiocqhttp.CQHttp.call_action(action, **params)
    caller = getattr(bot, "call_action", None)
    if callable(caller):
        try:
            return await _maybe_await(caller(action, **params))
        except TypeError as exc:
            errors.append(f"call_action(action, **params): {exc}")
        except Exception:
            raise

    # 2) bot.api.call_action / bot.api(action, **params)
    for owner_attr in ("api", "client", "bot", "protocol", "adapter"):
        owner = getattr(bot, owner_attr, None)
        if owner is None:
            continue
        nested = getattr(owner, "call_action", None)
        if callable(nested):
            try:
                return await _maybe_await(nested(action, **params))
            except TypeError as exc:
                errors.append(f"{owner_attr}.call_action: {exc}")
            except Exception:
                raise
        if callable(owner):
            try:
                return await _maybe_await(owner(action, **params))
            except TypeError as exc:
                errors.append(f"{owner_attr}(action, **params): {exc}")
            except Exception:
                raise

    # 3) bot(action, **params)
    if callable(bot):
        try:
            return await _maybe_await(bot(action, **params))
        except TypeError as exc:
            errors.append(f"bot(action, **params): {exc}")

    raise AttributeError(
        f"协议端未暴露可用的 action 调用入口（{action}）；尝试记录：{errors}"
    )


async def _call_with_timeout(bot: Any, action: str, **params: Any) -> Any:
    import asyncio

    return await asyncio.wait_for(call_action(bot, action, **params), timeout=ACTION_TIMEOUT_SECONDS)


def iter_domains(configured: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for domain in [configured, *DOMAIN_FALLBACKS]:
        domain = (domain or "").strip()
        if not domain or domain in seen:
            continue
        seen.add(domain)
        out.append(domain)
    return out


async def fetch_login_info(bot: Any) -> tuple[int, str]:
    """返回 (uin, 昵称)。"""
    for action in LOGIN_INFO_ACTIONS:
        try:
            payload = await _call_with_timeout(bot, action)
        except Exception as exc:
            log.debug("get_login_info 失败: %s", exc)
            continue
        uin = extract_int(payload, ("user_id", "uin", "qq", "account"))
        nickname = extract_str(payload, ("nickname", "nick", "name"))
        if uin:
            return uin, nickname
    return 0, ""


def build_credential_attempts(domain: str) -> list[tuple[str, dict[str, Any]]]:
    """按优先级生成 (action, kwargs) 尝试序列。

    优先级理由（实测结论）：
      * ``get_credentials`` 会同时返回 Cookie 与协议端自算的 bkn，
        且它的 p_skey 与 bkn 自洽 —— 是最可信的来源。
      * ``get_cookies`` 返回的 p_skey 与 ``get_credentials`` 不同、且不带 bkn，
        因此只作为兜底。
      * 域的顺序同样重要：不同域 QQ 会下发**不同**的 p_skey，
        ``user.qzone.qq.com`` 才是 QQ空间真正要用的那个。
    """
    attempts: list[tuple[str, dict[str, Any]]] = []
    for action in COOKIE_ACTIONS:
        for candidate in iter_domains(domain):
            attempts.append((action, {"domain": candidate}))
        attempts.append((action, {}))
    return attempts


async def fetch_credentials(
    bot: Any,
    *,
    platform_id: str = "",
    domain: str = DEFAULT_DOMAIN,
    manual_cookie: str = "",
) -> Credentials:
    """获取该机器人的 QQ空间凭据。

    关键正确性约束（踩过的坑，勿改）：

    1. **命中即用，绝不跨调用合并 Cookie。**
       QQ 每次请求都会轮换 p_skey，把两次调用的 Cookie 合并起来
       会出现「p_skey 来自这一次、skey 来自上一次」的错配，
       由此算出的 g_tk 是错的 —— 读接口可能侥幸能过，写接口必然失败。
    2. **``get_credentials`` 优先于 ``get_cookies``；配置域优先于兜底域。**
       否则会被后一次调用覆盖成错误域的 p_skey。

    Args:
        bot: 该机器人自己的 OneBot 客户端（AstrBot 平台适配器 get_client()）。
        platform_id: AstrBot 平台实例 id，仅用于日志/归属标识。
        domain: Cookie 域，默认 user.qzone.qq.com。
        manual_cookie: 手动填写的 Cookie 覆盖值（仅当自动获取全失败时使用），
            属于该机器人自己的配置项，不是全局共享值。

    Returns:
        Credentials；失败时 ``valid`` 为 False。
    """
    uin, nickname = await fetch_login_info(bot)

    for action, kwargs in build_credential_attempts(domain):
        label = f"{action}({kwargs.get('domain') or '无域'})"
        try:
            payload = await _call_with_timeout(bot, action, **kwargs)
        except Exception as exc:
            log.debug("[%s] %s 失败：%s", platform_id or "?", label, exc)
            continue

        text = extract_cookie_text(payload)
        if not text:
            continue

        # 单次调用的 Cookie 视为一个整体，不再与其它调用合并
        cookies = dict(parse_cookie_text(text))
        if not cookies:
            continue

        if not normalize_uin(cookies) and uin:
            cookies["uin"] = f"o{uin}"
            cookies.setdefault("p_uin", f"o{uin}")

        if not has_auth_cookie(cookies):
            log.debug("[%s] %s 返回的 Cookie 不含 skey/p_skey，继续尝试", platform_id or "?", label)
            continue

        gtk = cookie_gtk(cookies)
        if not gtk:
            log.debug("[%s] %s 有 Cookie 但算不出 g_tk，继续尝试", platform_id or "?", label)
            continue

        log.info(
            "[%s] 取得 QQ空间凭据：来源=%s uin=%s Cookie项=%d g_tk=%d",
            platform_id or "?", label, normalize_uin(cookies) or uin, len(cookies), gtk,
        )
        return Credentials(
            platform_id=platform_id,
            uin=normalize_uin(cookies) or uin,
            nickname=nickname,
            cookies=cookies,
            gtk=gtk,
            source=f"onebot:{label}",
            fetched_at=now(),
        )

    # 自动获取全失败 -> 允许使用该号自己的手动 Cookie
    if manual_cookie.strip():
        cookies = dict(parse_cookie_text(manual_cookie))
        if cookies:
            if not normalize_uin(cookies) and uin:
                cookies["uin"] = f"o{uin}"
                cookies.setdefault("p_uin", f"o{uin}")
            gtk = cookie_gtk(cookies)
            if has_auth_cookie(cookies) and gtk:
                log.info("[%s] 使用手动 Cookie（uin=%s g_tk=%d）", platform_id or "?", uin, gtk)
                return Credentials(
                    platform_id=platform_id,
                    uin=normalize_uin(cookies) or uin,
                    nickname=nickname,
                    cookies=cookies,
                    gtk=gtk,
                    source="manual",
                    fetched_at=now(),
                )
            log.warning("[%s] 手动 Cookie 缺少 p_skey/skey，无法计算 g_tk", platform_id or "?")

    log.warning(
        "[%s] 未能取得可用的 QQ空间凭据（uin=%s）。"
        "请确认协议端在线且支持 get_credentials / get_cookies。",
        platform_id or "?", uin or "未知",
    )
    return Credentials(platform_id=platform_id, uin=uin, nickname=nickname, source="failed")
