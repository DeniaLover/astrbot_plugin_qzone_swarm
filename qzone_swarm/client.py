"""QQ空间 HTTP 客户端（每个机器人一个实例，互不共用）。

协议端点取自线上可用实现，并在真实环境中验证过凭据可用性。
本模块不导入 astrbot。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import struct
import time
from typing import Any

from .creds import cookie_header
from .logging_compat import get_logger
from .models import Credentials, FeedComment, FeedItem
from .util import (
    deep_get,
    html_to_text,
    iter_balanced_objects,
    json_loads_loose,
    parse_js_literal,
)

log = get_logger("client")

try:  # httpx 由 AstrBot 本体提供
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
MSGLIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
MSGDETAIL_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6"
REFEEDS_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
DELETE_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6"
FEEDS_MORE_URL = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
LIKE_PROXY_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
LIKE_DIRECT_URL = "https://w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
UPLOAD_IMAGE_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"

REDIRECT_CODES = {301, 302, 303, 307, 308}
LOGIN_HOST_HINTS = ("ptlogin", "ssl.ptlogin", "xui.ptlogin", "ui.ptlogin", "login.qq.com")

MAX_IMAGES = 9


class QzoneError(Exception):
    """QQ空间操作失败。"""

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class QzoneAuthError(QzoneError):
    """登录态失效，需要重新绑定该号的 Cookie。"""


def compute_unikey(appid: int, hostuin: int, fid: str) -> str:
    if appid == 311:
        return f"https://user.qzone.qq.com/{hostuin}/mood/{fid}"
    return f"https://user.qzone.qq.com/{hostuin}/app/{appid}/{fid}"


def topic_id(appid: int, hostuin: int, fid: str, created_at: int = 0) -> str:
    if appid == 311:
        return f"{hostuin}_{fid}__1"
    return f"{hostuin}_{created_at}"


def _unwrap(payload: Any) -> Any:
    """剥掉 {"code":0,"data":{...}} / {"ret":0,...} 这类外壳。"""
    if not isinstance(payload, dict):
        return payload
    for key in ("data", "result", "retdata", "ret_data", "payload"):
        inner = payload.get(key)
        if isinstance(inner, (dict, list)) and inner:
            return inner
    return payload


# QQ空间对部分写请求（尤其是话题不存在或需要人工验证时）不返回 JSON，
# 而是返回一个跨域 iframe 回调页，形如：
#   <script>var cb;try{document.domain="user.qzone.qq.com";cb=frameElement.callback;
#           cb && cb({"code":0,"subcode":-200});}catch(e){}</script>
FRAME_MARKERS = ("document.domain", "frameElement", "callback")


def _looks_like_frame_response(text: str) -> bool:
    if not text:
        return False
    head = text[:600].lower()
    return "<html" in head or "<script" in head or sum(m in text for m in FRAME_MARKERS) >= 2


def parse_frame_payload(text: str) -> Any:
    """从 QQ空间 iframe 回调页里尝试取出真正的 JSON 载荷。

    回调页形如 ``try{...cb({"code":0});}catch(e){}``，
    真正的载荷是**嵌套在内层**的那个对象，所以要用
    :func:`qzone_swarm.util.iter_balanced_objects`（它对每个 '{' 都试一遍）。
    """
    if not _looks_like_frame_response(text):
        return None
    for chunk in iter_balanced_objects(text):
        payload = json_loads_loose(chunk)
        if isinstance(payload, dict) and (
            {"code", "ret", "subcode", "retcode"} & set(payload.keys())
        ):
            return payload
    return None


def _error_code(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    for key in ("ret", "code", "subcode", "retcode", "errno", "errcode"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
    return 0


def _error_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("message", "msg", "errmsg", "error", "submsg", "em"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# --------------------------------------------------------------------------- #
# 图片尺寸（自己解析头部，避免强依赖 Pillow）
# --------------------------------------------------------------------------- #
def image_dimensions(data: bytes) -> tuple[int, int] | None:
    if not data or len(data) < 12:
        return None
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            if len(data) < 24:
                return None
            width, height = struct.unpack(">II", data[16:24])
            return int(width), int(height)
        if data[:2] == b"\xff\xd8":  # JPEG
            index = 2
            length = len(data)
            while index + 9 < length:
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    index += 2
                    continue
                seg_len = struct.unpack(">H", data[index + 2 : index + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack(">HH", data[index + 5 : index + 9])
                    return int(width), int(height)
                index += 2 + seg_len
        if data[:6] in (b"GIF87a", b"GIF89a"):
            width, height = struct.unpack("<HH", data[6:10])
            return int(width), int(height)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                width = int.from_bytes(data[24:27], "little") + 1
                height = int.from_bytes(data[27:30], "little") + 1
                return width, height
    except Exception:
        return None
    return None


def looks_like_image(data: bytes) -> bool:
    if not data or len(data) < 12:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:2] == b"\xff\xd8":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


# --------------------------------------------------------------------------- #
# 响应解析
# --------------------------------------------------------------------------- #
def _to_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip().lstrip("oO")
        if text.lstrip("-").isdigit():
            return int(text)
    return default


def parse_comments(raw: Any, *, hostuin: int = 0) -> list[FeedComment]:
    out: list[FeedComment] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        content = (
            item.get("content")
            or item.get("htmlContent")
            or item.get("richContent")
            or ""
        )
        if isinstance(content, list):
            content = "".join(str(part) for part in content)
        out.append(
            FeedComment(
                comment_id=str(item.get("commentid") or item.get("commentId") or item.get("tid") or item.get("id") or ""),
                uin=_to_int(item.get("uin") or item.get("commentUin")),
                name=str(item.get("name") or item.get("nickname") or ""),
                content=str(content).strip(),
                created_at=float(_to_int(item.get("createTime") or item.get("time") or item.get("create_time"))),
                reply_to_uin=_to_int(item.get("replyUin") or item.get("replyuin")),
                reply_to_name=str(item.get("replyName") or ""),
            )
        )
    return out


def parse_feed_item(item: dict[str, Any], *, default_hostuin: int = 0) -> FeedItem | None:
    if not isinstance(item, dict):
        return None
    fid = str(item.get("tid") or item.get("cellid") or item.get("id") or item.get("fid") or "").strip()
    if not fid:
        return None

    content = item.get("content")
    if not isinstance(content, str):
        content = item.get("htmlContent") if isinstance(item.get("htmlContent"), str) else ""
    # 有些响应把正文放在 rt_con.con 里
    if not content:
        rt_con = item.get("rt_con")
        if isinstance(rt_con, dict):
            content = str(rt_con.get("con") or "")
        elif isinstance(rt_con, str):
            content = rt_con

    comments = parse_comments(item.get("commentlist") or item.get("comments") or [])
    created = _to_int(
        item.get("created_time")
        or item.get("createTime")
        or item.get("createdTime")
        or item.get("abstime")
        or item.get("time")
    )

    pics = item.get("pic") if isinstance(item.get("pic"), list) else []
    image_count = len(pics)
    if not image_count:
        image_count = _to_int(item.get("picnum") or item.get("picNum"))

    like_count = 0
    liked = False
    like_info = item.get("like")
    if isinstance(like_info, list) and like_info:
        first = like_info[0] if isinstance(like_info[0], dict) else {}
        like_count = _to_int(first.get("total") or first.get("likenum") or first.get("num"))
        liked = bool(_to_int(first.get("isliked")))
    elif isinstance(like_info, dict):
        like_count = _to_int(like_info.get("total") or like_info.get("likenum"))
        liked = bool(_to_int(like_info.get("isliked")))
    if not like_count:
        like_count = _to_int(item.get("likenum") or item.get("likeNum"))

    return FeedItem(
        hostuin=_to_int(item.get("uin") or item.get("hostuin") or default_hostuin) or default_hostuin,
        fid=fid,
        appid=_to_int(item.get("appid"), 311) or 311,
        content=str(content or "").strip(),
        created_at=float(created),
        nickname=str(item.get("name") or item.get("nickname") or ""),
        comments=comments,
        curkey=str(item.get("curkey") or ""),
        unikey=str(item.get("unikey") or ""),
        like_count=like_count,
        liked=liked,
        comment_count=_to_int(item.get("cmtnum") or item.get("commentnum"), len(comments)),
        image_count=image_count,
        raw=item,
    )


def parse_feed_entry(entry: dict[str, Any], *, default_hostuin: int = 0) -> FeedItem | None:
    """解析 ``feeds3_html_more``（好友动态）的一条记录。

    这个接口的字段与 ``msglist`` 完全不同（实测确认）：
        * fid 在 ``key``；
        * 作者在 ``uin`` / ``opuin``，昵称在 ``nickname``；
        * 时间在 ``abstime``；
        * 正文**不**在 title/summary（实测恒为空），而是混在 ``html`` 这一大坨
          渲染好的页面里（``<`` 还是 ``\\x3C`` 形式）。

    因此这里把 ``html`` 压成纯文本作为**兜底正文**，够模型判断内容即可；
    真正的干净正文应当再调 :meth:`QzoneClient.detail` 取（有权限时）。
    """
    if not isinstance(entry, dict):
        return None
    fid = str(entry.get("key") or entry.get("tid") or entry.get("fid") or "").strip()
    if not fid:
        return None

    hostuin = _to_int(entry.get("uin") or entry.get("opuin") or entry.get("hostuin")) or default_hostuin
    created_at = float(
        _to_int(entry.get("abstime") or entry.get("feedstime") or entry.get("time"))
    )
    nickname = str(entry.get("nickname") or entry.get("name") or "")

    content = ""
    for field in ("content", "title", "summary", "con"):
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            content = value.strip()
            break
    if not content:
        # 兜底：从渲染好的 html 里榨出可读文本
        content = html_to_text(str(entry.get("html") or ""))
        # 页面上重复出现的作者名/时间前缀意义不大，截断到合理长度
        content = content[:400]

    raw_html = str(entry.get("html") or "")
    like_count = _to_int(entry.get("likecnt") or entry.get("likenum") or entry.get("likeNum"))
    comment_count = _to_int(
        entry.get("commentcnt") or entry.get("relycnt") or entry.get("cmtnum")
    )

    return FeedItem(
        hostuin=hostuin,
        fid=fid,
        appid=_to_int(entry.get("appid"), 311) or 311,
        content=content,
        created_at=created_at,
        nickname=nickname,
        comments=parse_comments(entry.get("commentlist") or []),
        like_count=like_count,
        comment_count=comment_count,
        image_count=raw_html.count("<img") + raw_html.count("\\x3Cimg"),
        raw=entry,
    )


def parse_msglist(payload: Any, *, hostuin: int = 0) -> list[FeedItem]:
    data = _unwrap(payload)
    if not isinstance(data, dict):
        return []
    msglist = (
        data.get("msglist")
        or data.get("msgList")
        or data.get("vFeeds")
        or data.get("feeds")
    )
    if not isinstance(msglist, list):
        return []
    items: list[FeedItem] = []
    for entry in msglist:
        parsed = parse_feed_item(entry, default_hostuin=hostuin)
        if parsed:
            items.append(parsed)
    return items


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class QzoneClient:
    """绑定某一个机器人凭据的 QQ空间客户端。

    一个实例只服务一个 QQ 号；不同机器人必须使用不同实例。
    """

    def __init__(
        self,
        credentials: Credentials,
        *,
        timeout: float = 20.0,
        user_agent: str = "",
        max_retries: int = 3,
    ) -> None:
        if httpx is None:  # pragma: no cover
            raise QzoneError("缺少 httpx 依赖，无法进行 QQ空间 HTTP 请求")
        self.creds = credentials
        self.uin = int(credentials.uin or 0)
        self.gtk = int(credentials.gtk or 0)
        self.timeout = float(timeout)
        self.max_retries = max(1, int(max_retries))
        self.user_agent = user_agent or DEFAULT_UA
        self._client: Any = None
        self.feed_cache: dict[tuple[int, str], FeedItem] = {}

    # -- 基础设施 ----------------------------------------------------------- #
    @property
    def cookie_text(self) -> str:
        return cookie_header(self.creds.cookies)

    def _ensure_client(self) -> Any:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": self.user_agent},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    async def __aenter__(self) -> "QzoneClient":
        self._ensure_client()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def refresh(self, credentials: Credentials) -> None:
        self.creds = credentials
        self.uin = int(credentials.uin or 0)
        self.gtk = int(credentials.gtk or 0)

    def _headers(self, *, referer: str = "", origin: str = "", extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.cookie_text:
            headers["Cookie"] = self.cookie_text
        if referer:
            headers["Referer"] = referer
        if origin:
            headers["Origin"] = origin
        if extra:
            headers.update(extra)
        return headers

    def _params(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        merged: dict[str, Any] = dict(params or {})
        if self.gtk:
            merged.setdefault("g_tk", self.gtk)
        return merged

    def _assert_ready(self) -> None:
        if not self.creds.cookies:
            raise QzoneAuthError("尚未取得该机器人的 QQ空间 Cookie，请先执行绑定")
        if not self.gtk:
            raise QzoneAuthError("该机器人的 Cookie 缺少 p_skey/skey，无法计算 g_tk，请重新绑定")
        if not self.uin:
            raise QzoneAuthError("该机器人的 Cookie 缺少登录 UIN，请重新绑定")

    @staticmethod
    def _is_login_redirect(url: str) -> bool:
        low = (url or "").lower()
        return any(hint in low for hint in LOGIN_HOST_HINTS)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        referer: str = "",
        origin: str = "",
        login_required: bool = True,
        follow_redirects: bool = False,
        max_attempts: int | None = None,
    ) -> Any:
        if login_required:
            self._assert_ready()

        client = self._ensure_client()
        merged_params = self._params(params)
        merged_headers = self._headers(referer=referer, origin=origin, extra=headers)

        attempts = self.max_retries if max_attempts is None else max(1, int(max_attempts))
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await client.request(
                    method,
                    url,
                    params=merged_params,
                    data=data,
                    headers=merged_headers,
                    follow_redirects=follow_redirects,
                )
            except Exception as exc:  # 网络层
                last_exc = exc
                if attempt < attempts:
                    await asyncio.sleep(min(2.0, 0.6 * attempt))
                    continue
                raise QzoneError(f"请求 QQ空间失败：{exc}") from exc

            status = response.status_code
            if status in REDIRECT_CODES:
                location = response.headers.get("location", "")
                if self._is_login_redirect(location):
                    raise QzoneAuthError(
                        "QQ空间登录态已失效（被重定向到登录页），请重新绑定该号",
                        detail={"status": status, "location": location[:200]},
                    )
                if follow_redirects:
                    continue
                raise QzoneError(
                    "QQ空间请求被重定向",
                    detail={"status": status, "location": location[:200]},
                )

            if status >= 400:
                if status in (401, 403):
                    raise QzoneAuthError(
                        f"QQ空间鉴权失败（HTTP {status}），请重新绑定该号",
                        detail={"status": status},
                    )
                if attempt < attempts and status >= 500:
                    await asyncio.sleep(min(2.0, 0.6 * attempt))
                    continue
                raise QzoneError(f"QQ空间返回 HTTP {status}", detail={"status": status})

            text = response.text
            payload = json_loads_loose(text)
            if payload is None:
                # 部分端点（如好友动态 feeds3_html_more）返回的是 JS 对象字面量
                payload = parse_js_literal(text)
            if payload is None:
                # 写接口可能返回跨域 iframe 回调页，再从里面抢救一次
                payload = parse_frame_payload(text)
            if payload is None:
                if "ptlogin" in text.lower() or "请先登录" in text:
                    raise QzoneAuthError("QQ空间登录态已失效，请重新绑定该号")
                if _looks_like_frame_response(text):
                    # 鉴权已通过（否则会是登录跳转或 401），只是这一步没有可用的业务载荷
                    raise QzoneError(
                        "QQ空间返回了 iframe 回调页而非 JSON：通常是目标话题不存在、"
                        "已被删除，或该操作需要人工验证。本次操作未产生任何变更。",
                        detail={"status": status, "body": text[:300]},
                    )
                raise QzoneError("QQ空间返回了无法解析的内容", detail={"status": status, "body": text[:200]})

            code = _error_code(payload)
            if code and code not in (0,):
                message = _error_message(payload) or f"错误码 {code}"
                if code in (-3000,) or "登录" in message or "skey" in message.lower():
                    raise QzoneAuthError(f"QQ空间操作失败：{message}", detail={"code": code})
                raise QzoneError(f"QQ空间操作失败：{message}", detail={"code": code, "payload": payload})

            return payload

        raise QzoneError(f"请求 QQ空间失败：{last_exc}")

    # -- 发布 --------------------------------------------------------------- #
    async def publish(
        self,
        content: str,
        *,
        photos: list[dict[str, Any]] | None = None,
        sync_weibo: bool = False,
    ) -> dict[str, Any]:
        """发一条说说。photos 为 upload_image() 的返回值列表。"""
        self._assert_ready()
        photos = photos or []
        if len(photos) > MAX_IMAGES:
            raise QzoneError(f"一条说说最多 {MAX_IMAGES} 张图片，当前 {len(photos)} 张")

        richval = "\t".join(str(p.get("richval", "")) for p in photos)
        pic_bo = ",".join(str(p.get("pic_bo", "")) for p in photos if p.get("pic_bo"))

        data: dict[str, Any] = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": content,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": 1,
            "to_sign": 0,
            "hostuin": self.uin,
            "code_version": "1",
            "richval": richval,
            "issyncweibo": int(bool(sync_weibo)),
            "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{self.uin}",
        }
        if photos:
            data.update(
                {
                    "richtype": "1",
                    "subrichtype": "1",
                    "pic_bo": pic_bo,
                    "pic_template": "",
                }
            )

        payload = await self._request(
            "POST",
            PUBLISH_URL,
            data=data,
            referer=f"https://user.qzone.qq.com/{self.uin}",
            origin="https://user.qzone.qq.com",
        )
        fid = str(
            deep_get(_unwrap(payload), "tid", "fid", "cellid", default="")
            or deep_get(payload, "tid", "fid", "cellid", default="")
            or ""
        )
        result = dict(payload) if isinstance(payload, dict) else {"raw": payload}
        result.setdefault("fid", fid)
        return result

    async def upload_image(self, data: bytes, *, filename: str = "selfie.png") -> dict[str, Any]:
        """上传一张图片，返回可直接喂给 publish() 的 photo 结构。"""
        self._assert_ready()
        if not data:
            raise QzoneError("图片内容为空")
        if not looks_like_image(data):
            raise QzoneError("待上传内容不是可识别的图片（支持 PNG/JPEG/GIF/WebP）")
        dimensions = image_dimensions(data)
        if dimensions and min(dimensions) < 100:
            raise QzoneError(f"图片尺寸过小：{dimensions[0]}×{dimensions[1]}，至少需要 100×100")

        cookies = self.creds.cookies
        skey = cookies.get("skey") or cookies.get("p_skey") or ""
        p_skey = cookies.get("p_skey") or cookies.get("skey") or ""
        encoded = base64.b64encode(data).decode("ascii")

        payload = await self._request(
            "POST",
            UPLOAD_IMAGE_URL,
            params={"g_tk": self.gtk},
            data={
                "filename": filename,
                "uin": self.uin,
                "skey": skey,
                "zzpaneluin": self.uin,
                "p_uin": self.uin,
                "p_skey": p_skey,
                "qzonetoken": "",
                "uploadtype": "1",
                "albumtype": "7",
                "exttype": "0",
                "refer": "shuoshuo",
                "output_type": "json",
                "charset": "utf-8",
                "output_charset": "utf-8",
                "upload_hd": "1",
                "hd_width": "2048",
                "hd_height": "10000",
                "hd_quality": "96",
                "url": f"{UPLOAD_IMAGE_URL}?g_tk={self.gtk}",
                "base64": "1",
                "picfile": encoded,
                "qzreferrer": f"https://user.qzone.qq.com/{self.uin}",
            },
            referer=f"https://user.qzone.qq.com/{self.uin}",
            origin="https://user.qzone.qq.com",
        )
        return self._photo_from_upload(payload, filename=filename)

    @staticmethod
    def _photo_from_upload(payload: dict[str, Any], *, filename: str = "") -> dict[str, Any]:
        data = _unwrap(payload)
        if not isinstance(data, dict):
            raise QzoneError("图片上传返回格式异常", detail=payload)
        albumid = str(data.get("albumid") or data.get("albumId") or "")
        lloc = str(data.get("lloc") or data.get("LLoc") or "")
        sloc = str(data.get("sloc") or data.get("SLoc") or "")
        if not (albumid and lloc and sloc):
            raise QzoneError("图片上传返回缺少必要字段（albumid/lloc/sloc）", detail=data)
        width = str(data.get("width") or data.get("w") or 0)
        height = str(data.get("height") or data.get("h") or 0)
        photo_type = str(data.get("type") or ("png" if filename.lower().endswith(".png") else "jpg"))
        url = str(data.get("url") or data.get("origin_url") or data.get("pre") or "")
        pic_bo = str(data.get("pic_bo") or data.get("picBo") or "")
        richval = f",{albumid},{lloc},{sloc},{photo_type},{height},{width},,{height},{width}"
        return {
            "albumid": albumid,
            "lloc": lloc,
            "sloc": sloc,
            "type": photo_type,
            "height": height,
            "width": width,
            "url": url,
            "pic_bo": pic_bo,
            "richval": richval,
        }

    async def delete(
        self,
        fid: str,
        *,
        appid: int = 311,
        created_at: int = 0,
    ) -> dict[str, Any]:
        """删除自己的一条说说。

        用途：清理发失败的残帖，或在验证/演练流程里「发一条再撤回」。
        只能删自己号上的说说（topicId 用的是本号 uin）。
        """
        self._assert_ready()
        if not fid:
            raise QzoneError("删除说说缺少 fid")
        payload = await self._request(
            "POST",
            DELETE_URL,
            data={
                "uin": self.uin,
                "topicId": topic_id(appid, self.uin, fid, created_at),
                "feedsType": 0,
                "feedsFlag": 0,
                "feedsKey": fid,
                "feedsAppid": appid,
                "feedsTime": int(created_at or time.time()),
                "fupdate": 1,
                "ref": "feeds",
                "format": "json",
                "qzreferrer": f"https://user.qzone.qq.com/{self.uin}",
            },
            referer=f"https://user.qzone.qq.com/{self.uin}/mood/{fid}",
            origin="https://user.qzone.qq.com",
        )
        self.feed_cache.pop((self.uin, fid), None)
        return payload if isinstance(payload, dict) else {"raw": payload}

    # -- 读取 --------------------------------------------------------------- #
    async def msglist(self, hostuin: int | None = None, *, num: int = 10, pos: int = 0) -> list[FeedItem]:
        """读取某个号自己的说说列表（含评论）。"""
        target = int(hostuin or self.uin)
        payload = await self._request(
            "GET",
            MSGLIST_URL,
            params={
                "uin": target,
                "hostUin": target,
                "pos": max(0, int(pos)),
                "num": max(1, min(int(num), 50)),
                "replynum": 100,
                "callback": "_preloadCallback",
                "code_version": 1,
                "format": "json",
                "need_comment": 1,
                "need_private_comment": 1,
            },
            referer=f"https://user.qzone.qq.com/{target}",
            origin="https://user.qzone.qq.com",
            follow_redirects=True,
        )
        items = parse_msglist(payload, hostuin=target)
        for item in items:
            self.feed_cache[(item.hostuin, item.fid)] = item
        return items

    async def recent_feeds(self, *, page: int = 1, begin_time: int = 0) -> list[FeedItem]:
        """读取好友动态（"全部" feeds）。

        注意：这个接口返回的是 **JS 对象字面量**（不是 JSON），由
        :func:`qzone_swarm.util.parse_js_literal` 处理；条目结构也与
        ``msglist`` 不同，交给 :func:`parse_feed_entry` 解析。
        正文只有 HTML 兜底版本 —— 需要干净正文时再调 :meth:`detail`。
        """
        payload = await self._request(
            "GET",
            FEEDS_MORE_URL,
            params={
                "uin": self.uin,
                "scope": 0,
                "view": 1,
                "filter": "all",
                "flag": 1,
                "applist": "all",
                "pagenum": max(1, int(page)),
                "aisortEndTime": 0,
                "aisortOffset": 0,
                "aisortBeginTime": 0,
                "begintime": max(0, int(begin_time or 0)),
                "format": "json",
                "useutf8": 1,
                "outputhtmlfeed": 1,
            },
            referer=f"https://user.qzone.qq.com/{self.uin}",
            origin="https://user.qzone.qq.com",
            follow_redirects=True,
        )

        data = _unwrap(payload)
        candidates: Any = None
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, list):
                candidates = inner
            else:
                for key in ("feeds", "feedlist", "vFeeds", "data", "msglist"):
                    if isinstance(data.get(key), list):
                        candidates = data[key]
                        break
        elif isinstance(data, list):
            candidates = data

        items: list[FeedItem] = []
        for entry in candidates or []:
            parsed = parse_feed_entry(entry, default_hostuin=self.uin)
            if parsed:
                items.append(parsed)
        for item in items:
            self.feed_cache[(item.hostuin, item.fid)] = item
        return items

    async def detail(self, hostuin: int, fid: str, *, num: int = 30, pos: int = 0) -> FeedItem | None:
        """读取单条说说的详情（含最新评论）。"""
        payload = await self._request(
            "GET",
            MSGDETAIL_URL,
            params={
                "uin": int(hostuin),
                "tid": fid,
                "num": max(1, min(int(num), 100)),
                "pos": max(0, int(pos)),
                "not_trunc_con": 1,
                "format": "json",
            },
            referer=f"https://user.qzone.qq.com/{hostuin}/mood/{fid}",
            origin="https://user.qzone.qq.com",
            follow_redirects=True,
        )
        data = _unwrap(payload)
        entry = data if isinstance(data, dict) else {}
        for key in ("data", "feed", "msg", "shuoshuo"):
            if isinstance(entry.get(key), dict):
                entry = entry[key]
                break
        parsed = parse_feed_item(entry, default_hostuin=int(hostuin))
        if parsed:
            self.feed_cache[(parsed.hostuin, parsed.fid)] = parsed
        return parsed

    # -- 互动 --------------------------------------------------------------- #
    async def comment(
        self,
        hostuin: int,
        fid: str,
        content: str,
        *,
        appid: int = 311,
        private: bool = False,
    ) -> dict[str, Any]:
        """对别人的（或自己的）说说发表评论。"""
        self._assert_ready()
        referer = f"https://user.qzone.qq.com/{hostuin}/mood/{fid}"
        payload = await self._request(
            "POST",
            REFEEDS_URL,
            data={
                "topicId": topic_id(appid, hostuin, fid),
                "uin": self.uin,
                "hostUin": hostuin,
                "feedsType": 100,
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "platformid": 50,
                "format": "fs",
                "ref": "feeds",
                "content": content,
                "private": int(bool(private)),
                "paramstr": "1",
                "isSignIn": "0",
                "richval": "",
                "richtype": "",
                "appid": appid,
                "busi_param": "{}",
                "qzreferrer": referer,
            },
            referer=referer,
            origin="https://user.qzone.qq.com",
        )
        return payload if isinstance(payload, dict) else {"raw": payload}

    async def reply(
        self,
        hostuin: int,
        fid: str,
        comment_id: str,
        comment_uin: int,
        content: str,
        *,
        appid: int = 311,
    ) -> dict[str, Any]:
        """回复某条评论。"""
        self._assert_ready()
        referer = f"https://user.qzone.qq.com/{hostuin}/mood/{fid}"
        payload = await self._request(
            "POST",
            REFEEDS_URL,
            data={
                "topicId": topic_id(appid, hostuin, fid),
                "uin": self.uin,
                "hostUin": hostuin,
                "feedsType": 100,
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "platformid": 50,
                "format": "fs",
                "ref": "feeds",
                "content": content,
                "commentId": comment_id,
                "commentUin": comment_uin,
                "private": 0,
                "paramstr": "1",
                "isSignIn": "0",
                "appid": appid,
                "richval": "",
                "richtype": "",
                "qzreferrer": referer,
            },
            referer=referer,
            origin="https://user.qzone.qq.com",
        )
        return payload if isinstance(payload, dict) else {"raw": payload}

    async def like(self, hostuin: int, fid: str, *, appid: int = 311, like: bool = True) -> dict[str, Any]:
        """点赞 / 取消赞。两个入口依次尝试。"""
        self._assert_ready()
        cached = self.feed_cache.get((hostuin, fid))
        default_key = compute_unikey(appid, hostuin, fid)
        curkey = (cached.curkey if cached and cached.curkey else "") or default_key
        unikey = (cached.unikey if cached and cached.unikey else "") or default_key
        created_at = int(cached.created_at) if cached else 0

        data = {
            "unikey": unikey,
            "curkey": curkey,
            "appid": appid,
            "opuin": self.uin,
            "uin": self.uin,
            "hostuin": hostuin,
            "fid": fid,
            "from": 1,
            "typeid": 0,
            "abstime": created_at,
            "active": 0,
            "fupdate": 1,
            "opr_type": "like" if like else "unlike",
            "format": "purejson",
            "qzreferrer": f"https://user.qzone.qq.com/{hostuin}/mood/{fid}",
        }

        endpoints = (
            (LIKE_PROXY_URL, LIKE_DIRECT_URL) if like else (LIKE_PROXY_URL, LIKE_DIRECT_URL)
        )
        last_exc: Exception | None = None
        attempts: list[str] = []
        for endpoint in endpoints:
            try:
                payload = await self._request(
                    "POST",
                    endpoint,
                    data=data,
                    referer=f"https://user.qzone.qq.com/{hostuin}/mood/{fid}",
                    origin="https://user.qzone.qq.com",
                    follow_redirects=True,
                    max_attempts=1,
                )
                return payload if isinstance(payload, dict) else {"raw": payload}
            except (QzoneError,) as exc:
                if isinstance(exc, QzoneAuthError):
                    raise
                last_exc = exc
                attempts.append(f"{endpoint}: {exc}")
                continue
        raise QzoneError(
            f"点赞失败，所有入口均不可用：{last_exc}",
            detail={"attempts": attempts},
        )

    # -- 辅助 --------------------------------------------------------------- #
    def status_snapshot(self) -> dict[str, Any]:
        return {
            "uin": self.uin,
            "has_cookies": bool(self.creds.cookies),
            "gtk": bool(self.gtk),
            "cookie_items": len(self.creds.cookies),
            "source": self.creds.source,
            "fetched_at": self.creds.fetched_at,
            "now": time.time(),
        }
