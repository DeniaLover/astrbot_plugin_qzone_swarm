"""通用小工具：JSON 宽松解析、时间、文本清洗。"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from typing import Any

_CALLBACK_RE = re.compile(r"^[^(]*\((?P<body>.*)\)[;\s]*$", re.S)


def now() -> float:
    return time.time()


def today_key(ts: float | None = None) -> str:
    """本地日期键 YYYY-MM-DD，用于每日配额统计。"""
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime("%Y-%m-%d")


def parse_hhmm(text: str) -> tuple[int, int] | None:
    """解析 "HH:MM" -> (hour, minute)；非法返回 None。"""
    if not text:
        return None
    text = str(text).strip()
    m = re.fullmatch(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def slot_datetime(day: datetime, hour: int, minute: int) -> datetime:
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def json_loads_loose(text: str) -> Any:
    """解析可能带 JSONP 回调包装的响应体。"""
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = _CALLBACK_RE.match(text)
    if m:
        try:
            return json.loads(m.group("body").strip())
        except Exception:
            return None
    # 退而求其次：截取第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            return None
    return None


_JSONP_RE = re.compile(r"^\s*[\w$.\[\]'\"]*\s*\((.*)\)\s*;?\s*$", re.S)
_UNQUOTED_KEY_RE = re.compile(r"([{,]\s*)([A-Za-z_$][A-Za-z0-9_$]*)\s*:")
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


# JSON 合法的转义字符（\u 后面跟 4 位十六进制）
_VALID_JSON_ESCAPES = set('"\\/bfnrtu')


def _normalize_js_quotes(text: str) -> str:
    """把 JS 字面量里的字符串统一成**合法 JSON** 的字符串。

    需要处理的情况（都来自真实响应）：
      * 单引号字符串 -> 双引号；
      * 单引号串内部的裸 ``"`` -> ``\\"``；
      * JS 里合法但 JSON 不合法的转义（如 ``\\x41``）-> 转成字面反斜杠 ``\\\\x41``，
        否则 ``json.loads`` 会报 ``Invalid \\escape``；
      * 字符串内部的**裸换行/制表符** -> ``\\n`` / ``\\t``（JSON 不允许裸控制字符）；
      * 结尾孤立的 ``\\`` -> ``\\\\``。
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char not in ("'", '"'):
            out.append(char)
            i += 1
            continue

        quote = char
        out.append('"')  # JSON 一律双引号
        i += 1
        while i < n:
            ch = text[i]
            if ch == "\\" and i + 1 < n:
                nxt = text[i + 1]
                if nxt == quote:
                    # 转义的是定界符本身 -> 字面量
                    out.append('\\"' if nxt == '"' else "'")
                elif nxt in _VALID_JSON_ESCAPES:
                    out.append("\\" + nxt)
                else:
                    # JS 合法、JSON 非法 -> 退化成字面反斜杠
                    out.append("\\\\" + nxt)
                i += 2
                continue
            if ch == quote:
                out.append('"')
                i += 1
                break
            if ch == "\n":
                out.append("\\n")
                i += 1
                continue
            if ch == "\r":
                out.append("\\r")
                i += 1
                continue
            if ch == "\t":
                out.append("\\t")
                i += 1
                continue
            if ch == '"':
                out.append('\\"')
                i += 1
                continue
            if ch == "\\":
                # 末尾孤立的反斜杠
                out.append("\\\\")
                i += 1
                continue
            out.append(ch)
            i += 1
    return "".join(out)


def parse_js_literal(text: str) -> Any:
    """解析 QQ空间那种「长得像 JSON 但其实是 JS 对象字面量」的响应。

    实测 ``feeds3_html_more``（好友动态）返回的就是这种：

        {
            "code":0, ...,
            "data":
        {main:{attach:'',searchtype :'',hasMoreFeeds:false,daylist:'',...}}
        }

    与 JSON 的差别：键没引号、字符串用单引号、有时外面套 ``_Callback(...)``、
    偶尔有尾随逗号。``json.loads`` 直接吃会失败，于是互动功能整个读不到动态。

    Returns:
        解析出的对象；失败返回 None。
    """
    if not text:
        return None
    raw = strip_code_fences(text)

    candidates = [raw]
    # 去掉 jsonp / callback 包裹：_Callback({...}) 或 callback({...});
    match = _JSONP_RE.match(raw)
    if match:
        candidates.insert(0, match.group(1).strip())

    for candidate in candidates:
        for attempt in (
            candidate,
            _TRAILING_COMMA_RE.sub(r"\1", _UNQUOTED_KEY_RE.sub(r'\1"\2":', _normalize_js_quotes(candidate))),
        ):
            try:
                return json.loads(attempt)
            except Exception:
                continue
        # 把 JS 的 undefined 换成 null 再试一次
        patched = re.sub(r"\bundefined\b", "null", attempt)
        try:
            return json.loads(patched)
        except Exception:
            continue
    return None


_HTML_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_BR_RE = re.compile(r"<br\s*/?>", re.I)
_JS_HEX_RE = re.compile(r"\\x([0-9a-fA-F]{2})")
_WS_RE = re.compile(r"[ \t\u00a0]+")
_HTML_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
}


def decode_js_hex(text: str) -> str:
    r"""把 JS 风格的字面转义 ``\x3C`` / ``\x22`` 还原成真正的字符。

    QQ空间返回的 ``html`` 字段里，``<`` 与 ``"`` 常常就是这种形式
    （经过 JSON 合法化之后会变成 ``\\x3C``，这里一并处理）。
    """
    if not text:
        return ""
    # 先处理被我们转义过的 \\x（字面反斜杠 + x）
    text = text.replace("\\\\x", "\\x")
    return _JS_HEX_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


def html_to_text(html: str) -> str:
    """把 QQ空间那种渲染好的 HTML 片段压成可读纯文本。

    仅用于兜底（例如详情接口因权限拿不到时），产出会夹带一些界面文案，
    但足以让模型判断"这条说说在讲什么"。
    """
    if not html:
        return ""
    text = decode_js_hex(html)
    text = _HTML_SCRIPT_RE.sub(" ", text)
    text = _HTML_BR_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub(" ", text)
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def deep_get(payload: Any, *keys: str, default: Any = None) -> Any:
    """在嵌套 dict 中按顺序取第一个存在的键。"""
    if not isinstance(payload, dict):
        return default
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return default


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*|\s*```\s*$")


def strip_code_fences(text: str) -> str:
    """去掉模型爱加的 ``` 包裹。"""
    if not text:
        return ""
    cleaned = text.strip()
    if "```" in cleaned:
        cleaned = _FENCE_RE.sub("", cleaned)
    return cleaned.strip()


def iter_balanced_objects(text: str, *, max_scan: int = 20000):
    """从任意文本里抽出**所有**花括号配平的 {...} 片段（含嵌套的内层对象）。

    从每个 '{' 起都试一遍，而不是只取最外层 —— 因为模型/服务端常常把真正的
    JSON 嵌在别的东西里（例如 `cb({"code":0})`），只取最外层会拿到一段无法解析的
    外文而失败。
    """
    text = text[:max_scan]
    length = len(text)
    for start in range(length):
        if text[start] != "{":
            continue
        depth = 0
        in_string = False
        quote = ""
        escaped = False
        for index in range(start, length):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    in_string = False
                continue
            if char in ('"', "'"):
                in_string = True
                quote = char
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : index + 1]
                    break


# 截断修复时依次尝试的补尾
_REPAIR_SUFFIXES = ('"', '"}', '"]}', '"}}', "}", "]}")


def _try_json(text: str) -> dict | None:
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def extract_json_object(text: str) -> dict | None:
    """从模型输出里稳健地取出一个 JSON 对象。

    比 ``json_loads_loose`` 多做三件事，都是实际踩过的：
      1. 去掉 ``` 代码块包裹；
      2. 在夹杂着解释性文字的回复里，找出**内层**那个配平的 JSON；
      3. **修复被 max_tokens 截断的 JSON** —— 思维模型会吃掉大量
         reasoning token，回复常在字符串中间被切断（例如
         ``{"comment": "空了就去吃点`` ），此时补上收尾引号与花括号即可救回。

    Returns:
        解析出的 dict；实在救不回来返回 None。
    """
    if not text:
        return None
    cleaned = strip_code_fences(text)

    direct = _try_json(cleaned)
    if direct is not None:
        return direct

    # 先试完整配平的内层对象
    for chunk in iter_balanced_objects(cleaned):
        found = _try_json(chunk)
        if found is not None:
            return found

    # 再试截断修复：从第一个 '{' 开始，逐种方式补尾
    start = cleaned.find("{")
    if start >= 0:
        tail = cleaned[start:]
        for suffix in _REPAIR_SUFFIXES:
            found = _try_json(tail + suffix)
            if found is not None:
                return found
        # 也可能断在数组/对象多层里：截到最后一个 '}' 再补
        last = tail.rfind("}")
        if last > 0:
            for suffix in _REPAIR_SUFFIXES:
                found = _try_json(tail[: last + 1] + suffix)
                if found is not None:
                    return found

    return None


def clean_text(text: str, *, max_len: int = 0) -> str:
    """去掉首尾空白、多余空行与零宽字符。"""
    if not text:
        return ""
    text = str(text).replace("\u200b", "").replace("\ufeff", "").replace("\u2060", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if max_len and len(text) > max_len:
        text = text[:max_len].rstrip()
    return text


def strip_quotes(text: str) -> str:
    """去掉模型常见的包裹引号 / 代码块 / "说说：" 前缀。"""
    if not text:
        return ""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    text = re.sub(r"^(说说|正文|内容|评论|回复)\s*[:：]\s*", "", text)
    pairs = (('"', '"'), ("'", "'"), ("“", "”"), ("「", "」"), ("《", "》"))
    for left, right in pairs:
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            inner = text[len(left) : -len(right)]
            if left not in inner and right not in inner:
                text = inner.strip()
            break
    return text.strip()


def humanize_age(created_at: float, *, reference: float | None = None) -> str:
    """把时间戳转成"3小时前"这类相对时间，喂给模型当上下文。"""
    if not created_at:
        return ""
    delta = (reference if reference is not None else time.time()) - float(created_at)
    if delta < 0:
        delta = 0
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)}分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)}小时前"
    if delta < 86400 * 7:
        return f"{int(delta // 86400)}天前"
    return datetime.fromtimestamp(created_at).strftime("%m-%d")


def clock_text(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime("%H:%M")


def weekday_text(ts: float | None = None) -> str:
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    dt = datetime.fromtimestamp(ts if ts is not None else time.time())
    return names[dt.weekday()]


def daypart_text(ts: float | None = None) -> str:
    """一天中的时段，用于让"行动轨迹"更自然。"""
    hour = datetime.fromtimestamp(ts if ts is not None else time.time()).hour
    if hour < 5:
        return "深夜"
    if hour < 9:
        return "早晨"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 22:
        return "晚上"
    return "深夜"


def within_days(days: int) -> float:
    return time.time() - timedelta(days=max(0, days)).total_seconds()
