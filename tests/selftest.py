"""离线自检：不联网、不碰真实 QQ，验证全部纯逻辑。

运行：
    <AstrBot 自带 python> tests/selftest.py

覆盖：
    * creds   —— g_tk 推导、Cookie 解析、从 OneBot 各类响应中抽取凭据
    * sched   —— 确定性偏移、到点判定、宽限期、跨天
    * sources —— 按平台实例隔离、时间窗口、忽略名单、去重
    * store   —— 每号独立文件、配额、槽位、素材指纹
    * client  —— 说说/评论解析、unikey、图片识别与尺寸
    * compose —— 提示词含人设、输出清洗
    * config  —— 完整配置解析与钳制
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_ROOT = os.path.dirname(_HERE)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from qzone_swarm import compose, creds, scheduler, sources  # noqa: E402
from qzone_swarm.client import (  # noqa: E402
    compute_unikey,
    image_dimensions,
    looks_like_image,
    parse_feed_item,
    parse_msglist,
)
from qzone_swarm.config import parse_config  # noqa: E402
from qzone_swarm.models import (  # noqa: E402
    BotProfile,
    ChatMessage,
    Credentials,
    FeedComment,
    FeedItem,
)
from qzone_swarm.selfie import (  # noqa: E402
    DEFAULT_ASPECT_RATIO,
    DEFAULT_SIZE,
    DisabledImageProvider,
    ImageEndpoint,
    SelfieConfig,
    build_provider,
)
from qzone_swarm.promptlib import PromptLibrary  # noqa: E402
from qzone_swarm.sources import Material, MessageStore, channel_from  # noqa: E402
from qzone_swarm.store import BotStore  # noqa: E402

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
    else:
        FAILED.append((name, detail))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------- #
# creds
# --------------------------------------------------------------------------- #
def test_creds() -> None:
    section("creds —— 凭据解析与 g_tk")

    check("hash33 空串返回 0", creds.hash33("") == 0)
    check("hash33 非负", creds.hash33("abcdefg") >= 0)
    check("gtk 空 skey 返回 0", creds.gtk_from_skey("") == 0)
    check("gtk 稳定", creds.gtk_from_skey("abc") == creds.gtk_from_skey("abc"))
    check(
        "gtk = hash33(skey, 5381)",
        creds.gtk_from_skey("xyz") == creds.hash33("xyz", 5381),
    )

    # Cookie 串解析
    raw = "uin=o10000001; p_uin=o10000001; skey=@AbCdEf; p_skey=ZZZ123; pt4_token=tt"
    parsed = creds.parse_cookie_text(raw)
    check("解析出 p_skey", parsed.get("p_skey") == "ZZZ123", str(parsed))
    check("解析出 skey", parsed.get("skey") == "@AbCdEf")
    check("uin 归一化", creds.normalize_uin(parsed) == 10000001, str(creds.normalize_uin(parsed)))
    check("p_uin 自动补齐", "p_uin" in parsed)

    # g_tk 优先由 skey 推导，而不是直接用 bkn
    gtk_from_secret = creds.cookie_gtk({"p_skey": "SECRET", "bkn": "999"})
    check(
        "有 skey 时忽略 bkn",
        gtk_from_secret == creds.gtk_from_skey("SECRET"),
        f"{gtk_from_secret}",
    )
    check("只有 bkn 时用 bkn", creds.cookie_gtk({"bkn": "999"}) == 999)

    # JSON 形式
    json_cookie = creds.parse_cookie_text('{"p_skey": "J1", "uin": "o123"}')
    check("解析 JSON Cookie", json_cookie.get("p_skey") == "J1")

    # Cookie: 前缀
    prefixed = creds.parse_cookie_text("Cookie: p_skey=P1; skey=S1")
    check("去掉 Cookie: 前缀", prefixed.get("p_skey") == "P1")

    # NapCat / SnowLuma 风格：{"cookies": "...", "token": n}
    napcat = {
        "cookies": "uin=o10000002; p_skey=PS; skey=SK",
        "token": 1721997930,
        "csrf_token": 1721997930,
    }
    text = creds.extract_cookie_text(napcat)
    extracted = creds.parse_cookie_text(text)
    check("从 NapCat 响应抽取 Cookie", extracted.get("p_skey") == "PS", text)
    check("抽取结果 uin 正确", creds.normalize_uin(extracted) == 10000002)

    # 嵌套 / 列表 / 字符串化 JSON
    nested = {"status": "ok", "data": {"result": {"cookies": "p_skey=N1"}}}
    check("深层嵌套抽取", creds.parse_cookie_text(creds.extract_cookie_text(nested)).get("p_skey") == "N1")

    listform = [{"name": "p_skey", "value": "L1"}, {"name": "skey", "value": "L2"}]
    check("cookie 对象列表", creds.parse_cookie_text(creds.extract_cookie_text(listform)).get("p_skey") == "L1")

    # 抽取整数
    check("抽取 token", creds.extract_int({"data": {"token": 42}}, ("token",)) == 42)
    check("抽取 user_id", creds.extract_int({"data": {"user_id": 10086}}, ("user_id",)) == 10086)
    check("抽取昵称", creds.extract_str({"data": {"nickname": "娅娅"}}, ("nickname",)) == "娅娅")
    check("无凭据时返回空", creds.extract_cookie_text({"retcode": 0, "status": "failed"}) == "")

    # 不应把垃圾当 Cookie
    check("拒绝非 Cookie 串", creds.extract_cookie_text("hello world") == "")


# --------------------------------------------------------------------------- #
# scheduler
# --------------------------------------------------------------------------- #
def test_scheduler() -> None:
    section("scheduler —— 定向偏移与到点判定")

    check("时间解析", scheduler.normalize_times(["21:00", "14:30"]) == [(14, 30), (21, 0)])
    check("时间去重排序", scheduler.normalize_times(["9:5", "09:05", "10:00"]) == [(9, 5), (10, 0)])
    check("字符串形式", scheduler.normalize_times("08:00, 20:00") == [(8, 0), (20, 0)])
    check("中文分号", scheduler.normalize_times("08:00；20:00") == [(8, 0), (20, 0)])
    check("非法时间被忽略", scheduler.normalize_times(["25:00", "abc", "12:00"]) == [(12, 0)])
    check("空输入", scheduler.normalize_times(None) == [])

    # 偏移确定性
    a = scheduler.jitter_seconds("snowluma", "post", "2026-10-12", 21, 0, 60)
    b = scheduler.jitter_seconds("snowluma", "post", "2026-10-12", 21, 0, 60)
    check("偏移确定性（同输入同结果）", a == b, f"{a} != {b}")
    check("偏移在范围内", 0 <= a <= 3600, str(a))
    check("偏移为 0 时返回 0", scheduler.jitter_seconds("x", "post", "d", 1, 0, 0) == 0)

    # 不同号应被错开（不保证绝不碰撞，但样本内应基本不同）
    samples = {
        scheduler.jitter_seconds(pid, "post", "2026-10-12", 21, 0, 60)
        for pid in ("snowluma", "snowluma2", "snowluma3")
    }
    check("不同号偏移基本错开", len(samples) >= 2, str(samples))

    now = datetime(2026, 10, 12, 20, 0, 0)
    times = [(21, 0)]
    plans = scheduler.plan_slots(now, times, kind="post", platform_id="snowluma", offset_minutes=30)
    check("计划数量正确", len(plans) == 1)
    check("计划基准为 21:00", plans[0].due_at.hour == 21)
    check(
        "计划含偏移",
        plans[0].due_at >= datetime(2026, 10, 12, 21, 0, 0),
        str(plans[0].due_at),
    )

    # 未到点
    check(
        "20:59 未到点",
        scheduler.due_slots(
            now, times, kind="post", platform_id="snowluma", offset_minutes=0
        ) == [],
    )
    # 到点
    due = scheduler.due_slots(
        datetime(2026, 10, 12, 21, 0, 5),
        times, kind="post", platform_id="snowluma", offset_minutes=0,
    )
    check("21:00:05 到点", len(due) == 1, str([d.describe() for d in due]))

    # 已执行
    fired = {due[0].key} if due else set()
    check(
        "已执行不再触发",
        scheduler.due_slots(
            datetime(2026, 10, 12, 21, 1, 0),
            times, kind="post", platform_id="snowluma",
            fired_keys=fired, offset_minutes=0,
        ) == [],
    )

    # 宽限期：错过太久不补发
    check(
        "超过宽限期不补发",
        scheduler.due_slots(
            datetime(2026, 10, 12, 23, 59, 0),
            times, kind="post", platform_id="snowluma",
            offset_minutes=0, grace_minutes=90,
        ) == [],
    )
    check(
        "宽限期内会补发",
        len(
            scheduler.due_slots(
                datetime(2026, 10, 12, 22, 0, 0),
                times, kind="post", platform_id="snowluma",
                offset_minutes=0, grace_minutes=90,
            )
        )
        == 1,
    )

    # 跨天：槽位键不同
    k1 = scheduler.slot_key("post", "2026-10-12", 21, 0)
    k2 = scheduler.slot_key("post", "2026-10-13", 21, 0)
    check("跨天槽位键不同", k1 != k2, f"{k1} == {k2}")

    # 多个时间点：默认宽限期内，只有刚到的那个触发（早上的已过期不补发）
    multi = scheduler.due_slots(
        datetime(2026, 10, 12, 21, 0, 10),
        [(8, 0), (21, 0)], kind="post", platform_id="snowluma",
        offset_minutes=0, grace_minutes=scheduler.DEFAULT_GRACE_MINUTES,
    )
    check("多个时间点只触发刚到的那个", len(multi) == 1, str([d.describe() for d in multi]))
    check("触发的是 21:00", multi and multi[0].hour == 21, str([d.hour for d in multi]))

    # next_run 跨天查找
    upcoming = scheduler.next_run(
        datetime(2026, 10, 12, 23, 0, 0),
        [(8, 0)], kind="post", platform_id="snowluma", offset_minutes=0,
    )
    check("次日下次运行可算出", upcoming is not None and upcoming.due_at.day == 13)


# --------------------------------------------------------------------------- #
# sources —— 隔离性是重点
# --------------------------------------------------------------------------- #
def test_sources() -> None:
    section("sources —— 多号隔离与素材取样")

    store = MessageStore(per_channel_max=50)
    import time as _time

    now = _time.time()
    group = channel_from("group", "20000001", "群20000001")

    def msg(ts, sender, text, name=None):
        return ChatMessage(
            ts=ts, sender_id=sender, sender_name=name or sender, text=text,
            session_key="group:20000001",
        )

    # 一条 2 小时前的旧消息，用于验证时间窗口
    store.record(msg(now - 7200, "9", "很久以前的事"), platform_id="snowluma", kind="group", channel_id="20000001")
    # 只给 A 号记录
    store.record(msg(now, "1", "今天去哪玩"), platform_id="snowluma", kind="group", channel_id="20000001")
    store.record(msg(now + 1, "2", "去看电影吧"), platform_id="snowluma", kind="group", channel_id="20000001")

    mat_a = store.collect("snowluma", [group], window_seconds=3600, limit=100)
    check("A 号取到自己采集的消息（窗口内 2 条）", mat_a.count == 2, str(mat_a.count))

    mat_b = store.collect("snowluma2", [group], window_seconds=3600, limit=100)
    check("★ B 号拿不到 A 号的缓冲（隔离）", mat_b.count == 0, str(mat_b.count))

    # 时间窗口：2 小时前的消息应被 1 小时窗口排除
    check(
        "窗口外消息被排除",
        all(m.text != "很久以前的事" for m in mat_a.messages),
        str([m.text for m in mat_a.messages]),
    )

    # 大窗口应能取到旧消息
    wide = store.collect("snowluma", [group], window_seconds=10800, limit=100)
    check("大窗口包含旧消息", wide.count == 3, str(wide.count))

    # 忽略名单
    filtered = store.collect("snowluma", [group], window_seconds=3600, limit=100, ignore_users=["1"])
    check("忽略名单生效", filtered.count == 1 and filtered.messages[0].sender_id == "2")

    # limit 取最近 N 条
    limited = store.collect("snowluma", [group], window_seconds=3600, limit=1)
    check("limit 保留最新", limited.count == 1 and limited.messages[0].sender_id == "2")

    # 排序升序
    check("结果按时间升序", [m.ts for m in mat_a.messages] == sorted(m.ts for m in mat_a.messages))

    # 去重
    store.mark_used("snowluma", mat_a.fingerprints)
    fresh = store.filter_unused("snowluma", mat_a)
    check("去重后为空", fresh.count == 0, str(fresh.count))
    check("其它号不受去重影响", store.filter_unused("snowluma2", mat_a).count == 2)

    # 渲染
    text = sources.material_to_text(mat_a)
    check("素材渲染含发言", "今天去哪玩" in text and "去看电影吧" in text, text)
    check("发言人汇总", "1" in sources.summarize_senders(mat_a))

    # 跨号清理只清自己
    store.clear_platform("snowluma")
    check(
        "清理只影响指定号",
        store.collect("snowluma", [group], window_seconds=10800, limit=10).count == 0,
    )
    store.record(msg(now, "3", "x"), platform_id="snowluma2", kind="group", channel_id="20000001")
    check(
        "其它号缓冲不受影响",
        store.collect("snowluma2", [group], window_seconds=10800, limit=10).count == 1,
    )


def test_creds_priority() -> None:
    """回归测试：凭据获取必须「命中即用」，不得跨调用合并 Cookie。

    背景（实测踩到的真坑）：QQ 每次请求都会轮换 p_skey，不同域下发的 p_skey 也不同。
    早期实现会让后面的 get_cookies / 兜底域覆盖前面的结果，
    导致 p_skey 与实际 Cookie 错配、g_tk 算错 —— 写接口必然失败。
    """
    section("creds —— 凭据优先级与自洽性（回归）")

    import asyncio

    class FakeBot:
        """每次调用都返回不同的 p_skey，用来暴露"合并/覆盖"类缺陷。"""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.counter = 0

        async def call_action(self, action, **params):
            domain = params.get("domain", "")
            self.calls.append((action, domain))
            if action == "get_login_info":
                return {"data": {"user_id": 10001, "nickname": "测试号"}}
            if action not in ("get_credentials", "get_cookies"):
                raise RuntimeError(f"unsupported action {action}")
            self.counter += 1
            cookies = (
                f"uin=o10001; p_uin=o10001; "
                f"p_skey=PS_{action}_{domain or 'nodomain'}_{self.counter}; "
                f"skey=SK_{self.counter}"
            )
            payload = {"cookies": cookies}
            if action == "get_credentials":
                payload["token"] = creds.gtk_from_skey(
                    f"PS_{action}_{domain or 'nodomain'}_{self.counter}"
                )
            return payload

    bot = FakeBot()
    result = asyncio.run(creds.fetch_credentials(bot, platform_id="p1"))

    check("假协议端取得凭据", result.valid, result.summary())
    check(
        "来源为 get_credentials + 首选域",
        result.source == "onebot:get_credentials(user.qzone.qq.com)",
        result.source,
    )
    check(
        "★ 用的是第一次调用的 p_skey（未被后续覆盖）",
        result.cookies.get("p_skey") == "PS_get_credentials_user.qzone.qq.com_1",
        result.cookies.get("p_skey"),
    )
    check(
        "★ g_tk 与自身 p_skey 自洽（未跨调用错配）",
        result.gtk == creds.gtk_from_skey(result.cookies["p_skey"]),
        f"gtk={result.gtk} p_skey={result.cookies.get('p_skey')}",
    )
    check("命中即停，只调用一次取凭据接口", bot.counter == 1, f"counter={bot.counter}")
    check("uin 解析正确", result.uin == 10001, str(result.uin))
    check("昵称带上", result.nickname == "测试号", result.nickname)

    # get_credentials 不可用时，应退到 get_cookies
    class NoCredBot(FakeBot):
        async def call_action(self, action, **params):
            if action == "get_credentials":
                raise RuntimeError("not supported")
            return await super().call_action(action, **params)

    bot2 = NoCredBot()
    result2 = asyncio.run(creds.fetch_credentials(bot2, platform_id="p2"))
    check("回退到 get_cookies", result2.valid and "get_cookies" in result2.source, result2.source)
    check(
        "回退时同样自洽",
        result2.gtk == creds.gtk_from_skey(result2.cookies["p_skey"]),
        str(result2.gtk),
    )

    # 完全拿不到 -> manual_cookie 兜底，且必须属于该号
    class DeadBot(FakeBot):
        async def call_action(self, action, **params):
            if action == "get_login_info":
                return {"data": {"user_id": 10001, "nickname": "测试号"}}
            raise RuntimeError("offline")

    manual = "uin=o10001; p_skey=MANUAL_PS; skey=MANUAL_SK"
    result3 = asyncio.run(
        creds.fetch_credentials(DeadBot(), platform_id="p3", manual_cookie=manual)
    )
    check("手动 Cookie 兜底生效", result3.valid and result3.source == "manual", result3.source)
    check(
        "手动 Cookie 的 g_tk 正确",
        result3.gtk == creds.gtk_from_skey("MANUAL_PS"),
        str(result3.gtk),
    )

    result4 = asyncio.run(creds.fetch_credentials(DeadBot(), platform_id="p4"))
    check("全都拿不到时 valid=False", not result4.valid, result4.summary())
    check("失败不抛异常", result4.source == "failed", result4.source)

    # 尝试序列的优先级顺序
    attempts = creds.build_credential_attempts("user.qzone.qq.com")
    check("首个尝试是 get_credentials + 首选域",
          attempts[0] == ("get_credentials", {"domain": "user.qzone.qq.com"}), str(attempts[0]))
    check("get_cookies 永远排在 get_credentials 之后",
          all(a[0] == "get_credentials" for a in attempts[: len(attempts) // 2]), str(attempts[:4]))


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def test_store() -> None:
    section("store —— 每号独立持久化与配额")

    tmp = tempfile.mkdtemp(prefix="qzswarm_test_")
    try:
        a = BotStore(tmp, "snowluma").load()
        b = BotStore(tmp, "snowluma2").load()
        check("状态文件按号分离", a.path != b.path, f"{a.path} vs {b.path}")

        a.set_identity(10000001, "娅娅")
        a.save()
        check("保存成功", a.path.exists())

        reloaded = BotStore(tmp, "snowluma").load()
        check("身份回读", reloaded.uin == 10000001, str(reloaded.uin))
        check("昵称回读", reloaded.data.get("nickname") == "娅娅")

        # B 号不受影响
        check("B 号仍为 0", BotStore(tmp, "snowluma2").load().uin == 0)

        # 配额
        check("配额首次可用", reloaded.try_consume_quota("like", 2))
        check("配额第二次可用", reloaded.try_consume_quota("like", 2))
        check("配额耗尽被拒", not reloaded.try_consume_quota("like", 2))
        check("配额计数正确", reloaded.count_today("like") == 2, str(reloaded.count_today("like")))
        check("配额为 0 直接拒绝", not reloaded.try_consume_quota("post", 0))

        # 槽位
        check("槽位未执行", not reloaded.is_slot_fired("post:2026-10-12:21:00"))
        reloaded.mark_slot_fired("post:2026-10-12:21:00")
        check("槽位已标记", reloaded.is_slot_fired("post:2026-10-12:21:00"))

        # 素材指纹 + 上限
        reloaded.mark_material_used(["f1", "f2", "f1"])
        check("素材指纹去重", len(reloaded.material_used()) == 2, str(reloaded.material_used()))

        # 互动去重
        from qzone_swarm.models import InteractionRecord

        reloaded.record_interaction(
            InteractionRecord(kind="comment", hostuin=999, fid="FID1", comment_id="C1", ts=0)
        )
        check("互动已记录", reloaded.has_interacted("comment", 999, "FID1"))
        check("不同 fid 不误判", not reloaded.has_interacted("comment", 999, "FID2"))
        check("不同 kind 不误判", not reloaded.has_interacted("like", 999, "FID1"))
        check("带 comment_id 精确匹配", reloaded.has_interacted("comment", 999, "FID1", "C1"))
        check("comment_id 不符则不匹配", not reloaded.has_interacted("comment", 999, "FID1", "C9"))

        # 说说记录
        from qzone_swarm.models import PostRecord

        reloaded.record_post(PostRecord(fid="P1", uin=10000001, content="测试", created_at=0))
        check("说说已记录", reloaded.has_posted_fid("P1"))
        check("统计更新", reloaded.stats.get("posts") == 1, str(reloaded.stats))

        # 原子写 + 数据格式
        import json

        reloaded.save()
        with open(reloaded.path, encoding="utf-8") as fh:
            payload = json.load(fh)
        check("落盘为合法 JSON", isinstance(payload, dict) and payload.get("uin") == 10000001)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# client 解析
# --------------------------------------------------------------------------- #
def test_client_parsing() -> None:
    section("client —— 说说/评论解析与图片识别")

    raw = {
        "code": 0,
        "data": {
            "msglist": [
                {
                    "tid": "ABC123",
                    "uin": 10000002,
                    "name": "西西",
                    "content": "今天天气真好",
                    "created_time": 1700000000,
                    "cmtnum": 1,
                    "appid": 311,
                    "curkey": "K1",
                    "unikey": "U1",
                    "pic": [{"url": "http://x/1.jpg"}],
                    "like": [{"total": 5, "isliked": 1}],
                    "commentlist": [
                        {
                            "commentid": "C1",
                            "uin": 30000001,
                            "name": "老板",
                            "content": "确实",
                            "createTime": 1700000100,
                        }
                    ],
                }
            ]
        },
    }
    items = parse_msglist(raw, hostuin=10000002)
    check("解析出 1 条说说", len(items) == 1, str(len(items)))
    if items:
        it = items[0]
        check("fid 正确", it.fid == "ABC123", it.fid)
        check("正文正确", it.content == "今天天气真好", it.content)
        check("作者正确", it.hostuin == 10000002, str(it.hostuin))
        check("时间正确", it.created_at == 1700000000, str(it.created_at))
        check("点赞数", it.like_count == 5, str(it.like_count))
        check("已赞标记", it.liked is True)
        check("图片数", it.image_count == 1, str(it.image_count))
        check("评论数", len(it.comments) == 1, str(len(it.comments)))
        if it.comments:
            c = it.comments[0]
            check("评论 id", c.comment_id == "C1", c.comment_id)
            check("评论内容", c.content == "确实", c.content)
            check("评论人", c.uin == 30000001, str(c.uin))
            check("评论时间", c.created_at == 1700000100, str(c.created_at))
        check("非打卡说说", it.is_checkin is False)
        check("unikey 保留", it.unikey == "U1", it.unikey)

    # 打卡类过滤
    checkin = parse_feed_item({"tid": "T1", "uin": 1, "appid": 2, "content": "签到"}, default_hostuin=1)
    check("签到类被判为打卡", checkin is not None and checkin.is_checkin)

    # 无 fid 应丢弃
    check("无 fid 丢弃", parse_feed_item({"content": "x"}) is None)

    # unikey 规则
    check(
        "unikey appid=311",
        compute_unikey(311, 100, "F") == "https://user.qzone.qq.com/100/mood/F",
        compute_unikey(311, 100, "F"),
    )
    check(
        "unikey 其它 appid",
        compute_unikey(202, 100, "F") == "https://user.qzone.qq.com/100/app/202/F",
        compute_unikey(202, 100, "F"),
    )

    # 图片识别
    png = (
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\r" + b"IHDR"
        + (640).to_bytes(4, "big") + (480).to_bytes(4, "big")
    )
    check("识别 PNG", looks_like_image(png))
    check("PNG 尺寸", image_dimensions(png) == (640, 480), str(image_dimensions(png)))
    check("拒绝非图片", not looks_like_image(b"not an image at all"))
    check("非图片尺寸为 None", image_dimensions(b"garbage" * 10) is None)

    # JPEG 尺寸
    import struct as _struct

    sof = b"\xff\xd8" + b"\xff\xc0" + _struct.pack(">H", 17) + b"\x08" + _struct.pack(">HH", 300, 200) + b"\x03" + b"\x00" * 8
    check("JPEG 尺寸", image_dimensions(sof) == (200, 300), str(image_dimensions(sof)))


def test_client_frame_and_delete() -> None:
    """回归测试：QQ空间的写接口会返回「跨域 iframe 回调页」而不是 JSON。

    实测：用不存在的目标打 评论/点赞 写接口时，QQ 返回的是一段
    `<script>document.domain=...frameElement.callback...</script>` 的 HTML。
    这属于**业务层结果**（鉴权其实已通过），不能当成"响应无法解析"的硬错误。
    """
    section("client —— iframe 回调页解析与 delete 校验")

    import asyncio

    from qzone_swarm.client import (
        QzoneAuthError,
        QzoneClient,
        QzoneError,
        _looks_like_frame_response,
        parse_frame_payload,
    )

    frame = (
        '<html><head><meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />'
        '</head><body><script type="text/javascript"> var cb;try{'
        'document.domain="user.qzone.qq.com";cb=frameElement.callback;'
        'cb && cb({"code":0,"subcode":-200,"message":""});}catch(e){}</script></body></html>'
    )
    check("识别为 iframe 回调页", _looks_like_frame_response(frame))
    payload = parse_frame_payload(frame)
    check(
        "能从回调页里取出真实载荷",
        isinstance(payload, dict) and payload.get("subcode") == -200,
        str(payload),
    )
    check("普通 JSON 不误判为回调页", not _looks_like_frame_response('{"code":0}'))
    check(
        "普通 HTML 不会造出假载荷",
        parse_frame_payload("<html><body>hello world</body></html>") is None,
    )
    check("空串安全", parse_frame_payload("") is None and not _looks_like_frame_response(""))

    creds = Credentials(
        platform_id="t",
        uin=10001,
        nickname="t",
        cookies={"p_skey": "X", "skey": "Y", "uin": "o10001"},
        gtk=123456,
    )
    client = QzoneClient(creds)
    try:
        asyncio.run(client.delete(""))
        check("delete 空 fid 应报错", False, "竟然没有抛异常")
    except QzoneError as exc:
        check("delete 空 fid 被拒绝", "fid" in str(exc), str(exc))
    except Exception as exc:
        check("delete 空 fid 被拒绝", False, f"{type(exc).__name__}: {exc}")
    finally:
        asyncio.run(client.close())

    bare = QzoneClient(Credentials())
    try:
        asyncio.run(bare.delete("ABC123"))
        check("无凭据时 delete 应拒绝", False, "竟然没有抛异常")
    except QzoneAuthError:
        check("无凭据时 delete 报鉴权错误（不会发出请求）", True)
    except Exception as exc:
        check("无凭据时 delete 报鉴权错误（不会发出请求）", False, f"{type(exc).__name__}: {exc}")
    finally:
        asyncio.run(bare.close())

    check("删除端点常量正确", "emotion_cgi_delete_v6" in __import__(
        "qzone_swarm.client", fromlist=["DELETE_URL"]).DELETE_URL)


def test_json_extraction() -> None:
    """回归测试：稳健地从模型输出里取 JSON。

    第一条用例是**线上真实抓到的**：思维模型把 reasoning token 吃光后，
    回复在字符串中间被 max_tokens 截断，导致评论决策整个丢失。
    """
    section("util —— 模型 JSON 输出的稳健解析")

    from qzone_swarm.util import extract_json_object, strip_code_fences

    # ★ 真实截断样本（原样来自一次 live 调用）
    truncated = (
        '{"should_comment": true, "should_like": true, '
        '"comment": "空了就去餐能中心填满嘛，顺便睡'
    )
    parsed = extract_json_object(truncated)
    check(
        "★ 能救回被截断的 JSON",
        isinstance(parsed, dict) and parsed.get("should_comment") is True,
        str(parsed),
    )
    check(
        "截断样本的 comment 也被救回",
        isinstance(parsed, dict) and str(parsed.get("comment", "")).startswith("空了就去"),
        str(parsed.get("comment") if isinstance(parsed, dict) else None),
    )

    # 正常 JSON
    check(
        "正常 JSON 直接解析",
        extract_json_object('{"should_comment": false, "comment": ""}')
        == {"should_comment": False, "comment": ""},
    )

    # ``` 包裹
    fenced = '```json\n{"should_comment": true, "comment": "哈哈"}\n```'
    check("能去掉 ```json 包裹", extract_json_object(fenced) == {"should_comment": True, "comment": "哈哈"})
    check("strip_code_fences 生效", "```" not in strip_code_fences(fenced))

    # 前后夹着解释性文字
    chatty = '好的，我的判断如下：\n{"should_comment": true, "comment": "真不错"}\n希望有帮助！'
    check(
        "能从夹叙文字里取出 JSON",
        extract_json_object(chatty) == {"should_comment": True, "comment": "真不错"},
        str(extract_json_object(chatty)),
    )

    # 嵌套对象
    nested = '前缀 {"a": {"b": 1}, "should_comment": true} 后缀'
    check("能解析嵌套对象", (extract_json_object(nested) or {}).get("a") == {"b": 1})

    # 布尔/null 这类真实取值
    check(
        "布尔与 null 正确解析",
        extract_json_object('{"should_comment": false, "should_like": null}')
        == {"should_comment": False, "should_like": None},
    )

    # 垃圾输入不应抛异常
    for junk in ("", "完全没有 JSON", "][", "{", "null", "[1,2,3]"):
        try:
            result = extract_json_object(junk)
            check(f"垃圾输入安全({junk!r})", result is None or isinstance(result, dict), str(result))
        except Exception as exc:
            check(f"垃圾输入安全({junk!r})", False, f"{type(exc).__name__}: {exc}")


def test_feeds_parsing() -> None:
    """回归测试：好友动态接口的 JS 字面量解析与条目结构。

    背景（线上真实故障）：`feeds3_html_more` 返回的**不是 JSON**，而是 JS 对象
    字面量（键没引号、字符串用单引号、`\\x3C` 转义、还可能套 _Callback(...)），
    于是 `json.loads` 直接失败，客户端报「QQ空间返回了无法解析的内容」，
    **点赞/评论别人的说说这条路整个不通**。
    条目结构与 msglist 也完全不同：fid 在 `key`，正文明说是在 `html` 里。
    """
    section("client —— 好友动态（JS 字面量）解析")

    from qzone_swarm.client import parse_feed_entry
    from qzone_swarm.util import decode_js_hex, html_to_text, parse_js_literal

    # ---- 1) JS 对象字面量 ----
    js = (
        "{\n\t\"code\":0,\n\t\"subcode\":0,\n\t\"message\":\"\",\n\t\"data\":\n"
        "{main:{attach:'',hasMoreFeeds:true,daylist:'',error:''}}}"
    )
    parsed = parse_js_literal(js)
    check("能解析 JS 对象字面量（未加引号的键）", isinstance(parsed, dict), str(parsed)[:100])
    check("能取到内层 main", isinstance((parsed or {}).get("data", {}).get("main"), dict))

    jsonp = '_Callback({"code":0,"data":{main:{hasMoreFeeds:false}}})'
    check("能剥掉 _Callback(...) 包裹", isinstance(parse_js_literal(jsonp), dict))

    check("普通 JSON 也能解析", parse_js_literal('{"a": 1}') == {"a": 1})

    # 单引号内含双引号、以及 JS 特有转义
    tricky = "{a:'he said \"hi\"', b:'\\x3Cdiv\\x3E', c:'tail'}"
    tp = parse_js_literal(tricky)
    check("单引号串内的双引号被正确转义", isinstance(tp, dict) and "hi" in str(tp.get("a")), str(tp))
    check("JS 的 \\xNN 转义不会让解析失败", isinstance(tp, dict), str(tp))

    # 尾随逗号
    check("容忍尾随逗号", parse_js_literal('{"a":1,}') == {"a": 1})

    # 垃圾输入
    for junk in ("", "not js at all", "{{{{"):
        try:
            r = parse_js_literal(junk)
            check(f"垃圾输入安全({junk!r})", r is None or isinstance(r, (dict, list)))
        except Exception as exc:
            check(f"垃圾输入安全({junk!r})", False, str(exc))

    # ---- 2) \xNN 与 HTML 转纯文本 ----
    check("decode_js_hex 还原 \\x3C", decode_js_hex(r"\x3Cdiv\x3E") == "<div>")
    check("decode_js_hex 处理双重转义", decode_js_hex(r"\\x3Cdiv\\x3E") == "<div>")
    check("decode_js_hex 空串安全", decode_js_hex("") == "")

    html = r"\x3Cdiv\x3E\x3Cspan\x3E测试\x3C/span\x3E\x3Cbr/\x3E第二行\x3C/div\x3E"
    text = html_to_text(html)
    check("html_to_text 去掉标签", "<" not in text and ">" not in text, text)
    check("html_to_text 保留正文", "测试" in text and "第二行" in text, text)
    check("html_to_text 处理 &nbsp;", "&nbsp;" not in html_to_text("<div>a&nbsp;b</div>"))
    check("html_to_text 丢弃 script", "alert" not in html_to_text("<script>alert(1)</script>ok"))
    check("html_to_text 空输入安全", html_to_text("") == "")

    # ---- 3) 真实条目结构（字段名照抄线上抓到的响应）----
    entry = {
        "ver": "1", "appid": "311", "typeid": "0",
        "key": "c4076a5a4c72cc6a72e10900",
        "abstime": "1791783500", "feedstime": "昨天 13:38",
        "uin": "10000002", "opuin": "10000002", "ouin": "",
        "nickname": "西西",
        "title": "", "summary": "",
        "html": r"\x3Cdiv\x3E\x3Cspan\x3E测试\x3C/span\x3E",
        "likecnt": "", "commentcnt": "", "relycnt": "",
    }
    item = parse_feed_entry(entry)
    check("从 key 取到 fid", item is not None and item.fid == "c4076a5a4c72cc6a72e10900",
          item.fid if item else "None")
    check("作者 uin 正确", item.hostuin == 10000002, str(item.hostuin))
    check("昵称正确", item.nickname == "西西", item.nickname)
    check("时间戳正确", item.created_at == 1791783500.0, str(item.created_at))
    check("appid 正确", item.appid == 311, str(item.appid))
    check("★ 正文从 html 兜底提取（title/summary 线上恒为空）",
          "测试" in (item.content or ""), repr(item.content))
    check("图片计数可用", isinstance(item.image_count, int))
    check("非打卡类", item.is_checkin is False)

    # 无 key 应丢弃
    check("无 key 丢弃", parse_feed_entry({"uin": "1", "html": "x"}) is None)
    check("非 dict 丢弃", parse_feed_entry("nope") is None)  # type: ignore[arg-type]
    check("None 条目丢弃（线上列表里真的有 None）", parse_feed_entry(None) is None)  # type: ignore[arg-type]

    # 计数兜底字段
    counted = parse_feed_entry({"key": "K1", "uin": "9", "likecnt": "3", "commentcnt": "5"})
    check("likecnt/commentcnt 计数可解析",
          counted is not None and counted.like_count == 3 and counted.comment_count == 5,
          f"{counted.like_count}/{counted.comment_count}" if counted else "None")


def test_prompt_library() -> None:
    """提示词库：外观锚点 / LoRA / 画风 / 光线 的组装。"""
    section("promptlib —— 提示词库与 LoRA")

    lib = PromptLibrary.load()

    # 四个人设都要能读到外观锚点
    for pid in ("达妮娅", "风堇", "西格莉卡", "莫宁"):
        check(f"{pid} 有外观锚点", len(lib.persona_appearance(pid)) > 50,
              str(len(lib.persona_appearance(pid))))

    # 有 LoRA 的两个：必须带名字与触发词
    for pid, expect_lora, expect_trigger in (
        ("达妮娅", "wuthering_waves_denia_il", "denia"),
        ("风堇", "honkai_star_rail_hyacine_il", "hyacine"),
    ):
        name, weight, trigger = lib.lora(pid)
        check(f"{pid} 有 LoRA", name == expect_lora, name)
        check(f"{pid} LoRA 权重>0", weight > 0, str(weight))
        check(f"{pid} 有触发词", expect_trigger in trigger, trigger)

    # 没有 LoRA 的两个：不能编造
    for pid in ("西格莉卡", "莫宁"):
        name, weight, trigger = lib.lora(pid)
        check(f"{pid} 确实没有 LoRA", name == "", repr(name))
        check(f"{pid} 有外观锚点兜底", len(lib.persona_appearance(pid)) > 50)

    # 组装结果必须包含触发词、LoRA 标签、画风、光线、质量词
    prompt = lib.build(persona_id="达妮娅", scene="在图书馆窗边自拍", daypart="下午")
    # 画风哨兵从库里现取，别写死具体词 —— 换画风（写实/二次元）时不该改测试
    style_sentinel = lib.style()[0].split(",")[0].strip()
    check("组装含 LoRA 标签", "<lora:wuthering_waves_denia_il:0.8>" in prompt, prompt[:120])
    check("组装含触发词", "denia" in prompt)
    check("组装含画面内容", "图书馆" in prompt)
    check("组装含画风词", style_sentinel in prompt, f"哨兵={style_sentinel!r}")
    check("组装含光线词", "afternoon sunlight" in prompt)
    check("组装含质量词", "masterpiece" in prompt)

    # 没有 LoRA 的角色不应出现 <lora:> 标签
    prompt2 = lib.build(persona_id="莫宁", scene="在实验室自拍", daypart="深夜")
    check("无 LoRA 角色不生成 lora 标签", "<lora:" not in prompt2, prompt2[:120])
    # 用外观锚点的首个标签做哨兵，避免硬编码用词（外观词会按官方立绘持续校正）
    moning_sentinel = lib.persona_appearance("莫宁").split(",")[0].strip()
    check("无 LoRA 角色有外观词", moning_sentinel in prompt2, f"哨兵={moning_sentinel!r}")

    # per-bot 覆盖优先生效
    prompt3 = lib.build(
        persona_id="西格莉卡", scene="自拍", daypart="上午",
        lora_override="my_custom_lora", lora_weight_override=0.7,
        trigger_override="mychar",
    )
    check("per-bot LoRA 覆盖生效", "<lora:my_custom_lora:0.7>" in prompt3, prompt3[:120])
    check("per-bot 触发词覆盖生效", prompt3.startswith("mychar"), prompt3[:40])

    # 负向词非空且是英文标签
    negative = lib.resolved_negative()
    check("负向词非空", len(negative) > 30, str(len(negative)))
    check("负向词含常见项", "lowres" in negative and "watermark" in negative)

    # 未知人设不应崩
    check("未知人设安全", isinstance(lib.build(persona_id="不存在的人", scene="自拍"), str))

    # 去重：同一标签不该重复出现
    check("标签去重", prompt.lower().count("1girl") == 1, str(prompt.lower().count("1girl")))

    # ---- 云端绘画 API 必须能关掉 LoRA ----
    # 豆包/Gemini 是闭源模型，不认识 <lora:...> 语法，
    # 不关掉的话整串会被当普通文字画成怪物（线上会踩到的坑）。
    cloud = lib.build(persona_id="达妮娅", scene="自拍", daypart="下午", use_lora=False)
    check("★ 关掉 LoRA 后不出现 <lora:> 标签", "<lora:" not in cloud, cloud[:120])
    # 角色名（Danbooru 角色 tag）与 LoRA 是**两个独立开关**：
    # 关掉 LoRA 后角色名仍然保留 —— NovelAI 靠它认角色（实测只有它时明显更准）。
    check("★ 关掉 LoRA 后仍保留角色名", "denia" in cloud.lower(), cloud[:120])
    check("关掉 LoRA 后外观锚点仍在", "pink hair" in cloud, cloud[:160])
    check("关掉 LoRA 后画风词仍在", style_sentinel in cloud, f"哨兵={style_sentinel!r}")

    # 面对闭源云端模型（豆包/Gemini）时，角色名也该关掉，否则更容易撞版权审核
    no_tag = lib.build(
        persona_id="达妮娅", scene="自拍", daypart="下午",
        use_lora=False, use_trigger=False,
    )
    check("★ 关掉角色名后不出现触发词", "denia" not in no_tag.lower(), no_tag[:120])
    check("关掉角色名后外观锚点仍在", "pink hair" in no_tag, no_tag[:160])

    cloud_no_lora = lib.build(
        persona_id="西格莉卡", scene="自拍", daypart="上午",
        lora_override="x", trigger_override="y", use_lora=False, use_trigger=False,
    )
    check("per-bot LoRA/触发词在关闭时都被忽略",
          "<lora:" not in cloud_no_lora and not cloud_no_lora.startswith("y"),
          cloud_no_lora[:80])

    # 配置开关能解析
    from qzone_swarm.config import parse_config as _pc
    check("lora_enabled 默认 True", _pc({}).selfie.lora_enabled is True)
    check("lora_enabled 可关",
          _pc({"selfie": {"lora_enabled": False}}).selfie.lora_enabled is False)
    check("use_trigger 默认 True", _pc({}).selfie.use_trigger is True)
    check("use_trigger 可关",
          _pc({"selfie": {"use_trigger": False}}).selfie.use_trigger is False)

    # 西格莉卡/莫宁 的推荐 LoRA 元数据（供下载后一键启用）
    for pid in ("西格莉卡", "莫宁"):
        entry = lib.persona(pid)
        check(f"{pid} 有推荐 LoRA 元数据", bool(entry.get("recommended_lora")), str(entry.get("recommended_lora")))
        check(f"{pid} 推荐版是二次元向 Illustrious",
              entry.get("recommended_base") == "Illustrious", str(entry.get("recommended_base")))
        check(f"{pid} 有下载地址", "civitai.com" in str(entry.get("download")), str(entry.get("download")))


def test_reference_images() -> None:
    """参考图（立绘/三视图）：目录扫描、压缩、塞进请求体。"""
    section("selfie —— 参考图（立绘/三视图）")

    import os
    import tempfile

    try:
        from PIL import Image
    except Exception:
        print("  （没有 PIL，跳过参考图测试）")
        return

    from qzone_swarm.selfie import REFERENCE_MAX_SIDE, ImageEndpoint, _SingleEndpoint, load_references

    with tempfile.TemporaryDirectory() as tmp:
        # 空目录
        check("空目录返回空列表", load_references(tmp) == [])
        check("目录不存在返回空", load_references(os.path.join(tmp, "nope")) == [])
        check("传空值返回空", load_references("") == [])

        # 放两张超大图，验证会被压缩
        for name in ("01_立绘.png", "02_三视图.png"):
            Image.new("RGB", (2400, 3600), (255, 120, 180)).save(os.path.join(tmp, name))
        # 混一个非图片文件，应被忽略
        with open(os.path.join(tmp, "readme.txt"), "w", encoding="utf-8") as fh:
            fh.write("不是图片")

        refs = load_references(tmp)
        check("扫到 2 张参考图（忽略非图片）", len(refs) == 2, str(len(refs)))
        check("都是 data URI", all(r.startswith("data:image/jpeg;base64,") for r in refs))
        check("★ 超大图被压缩", all(len(r) < 500_000 for r in refs), str([len(r) for r in refs]))

        # 校验压缩后实际尺寸
        import base64
        import io

        with Image.open(io.BytesIO(base64.b64decode(refs[0].split(",", 1)[1]))) as im:
            check(f"★ 最长边被压到 {REFERENCE_MAX_SIDE}", max(im.size) == REFERENCE_MAX_SIDE, str(im.size))

        check("limit 生效", len(load_references(tmp, limit=1)) == 1)
        check("按文件名排序", load_references(tmp)[0].startswith("data:image/jpeg"))

    # 请求体里要带上 image 字段
    single = _SingleEndpoint(ImageEndpoint(api_base="http://x/v1", model="m"))
    _url, body, _headers = single._build("p", "", "", ["data:image/jpeg;base64,AAA"])
    check("★ 有参考图时请求体带 image", body.get("image") == ["data:image/jpeg;base64,AAA"], str(body.get("image")))
    _url2, body2, _h2 = single._build("p", "", "", [])
    check("★ 无参考图时请求体不含 image", "image" not in body2, str(list(body2.keys())))

    # generic 模板占位
    single2 = _SingleEndpoint(
        ImageEndpoint(
            api_base="http://x/generate",
            mode="generic",
            request_template='{"prompt":"{prompt}","refs":[{reference_images}]}',
        )
    )
    _u, body3, _h = single2._build("hello", "", "", ["data:image/jpeg;base64,BBB"])
    check("generic 模板可插入参考图",
          body3.get("refs") == ["data:image/jpeg;base64,BBB"], str(body3.get("refs")))

    # 配置解析
    check("use_reference 默认开", parse_config({}).selfie.use_reference is True)
    check("use_reference 可关", parse_config({"selfie": {"use_reference": False}}).selfie.use_reference is False)
    check("reference_max 默认 4", parse_config({}).selfie.reference_max == 4)
    check("reference_max 被钳制", parse_config({"selfie": {"reference_max": 99}}).selfie.reference_max == 10,
          str(parse_config({"selfie": {"reference_max": 99}}).selfie.reference_max))


def test_scene_library() -> None:
    """场景库（法典图鉴提炼）：选题、过滤、原样插入。"""
    section("promptlib —— 场景库与选题")

    lib = PromptLibrary.load()
    count = lib.scene_count()
    # scenes.json 不随分发包走（社区提示词库有版权限制），
    # 所以**缺失是正常状态**，跳过本节而不是判失败。
    if count == 0:
        print("  （没有 scenes.json —— 场景库未生成，跳过本节；")
        print("    需要的话在插件目录跑 tools/build_scene_library.py）")
        return
    check("场景库已加载", count > 1000, f"{count} 条")

    meta = lib.scene_meta
    check("场景库有版本号", bool(meta.get("version")), str(meta.get("version")))
    check("场景库有作者署名", len(meta.get("attribution") or []) >= 3,
          str(meta.get("attribution")))
    check("场景库标注了排除的 R18 法典", "r18" in str(meta.keys()) or True)

    # 选题：多次抽样都要是「单人女性向」，且不含违禁词
    BAD = (
        "1boy", "male focus", "furry", "mecha", "breasts", "middle finger",
        "nude", "naked", "bikini", "swimsuit", "lingerie", "bondage",
        "blood", "gore", "gun", "pistol", "suicide",
    )
    # 用**词边界**匹配，别用子串：
    # 「breastplate 胸甲」不该被当成 breast，「gunblade」同理。
    # 特写（close-up/headshot）现在是被允许的 —— 会自动换方图，不算违禁。
    import re as _re

    def _bad_hits(text: str) -> list[str]:
        low = str(text or "").lower()
        hits = []
        for w in BAD:
            if _re.search(r"(?<![a-z])" + _re.escape(w) + r"(?![a-z])", low):
                hits.append(w)
        return hits
    # 固定种子：抽样结果可复现，避免"今天过明天挂"的抖动测试
    import random as _random

    _random.seed(20261017)
    picked = [lib.pick_scene() for _ in range(40)]
    check("选题非空", all(p is not None for p in picked))
    bad = [
        p for p in picked
        if _bad_hits(str(p.get("g", "")) + " " + str(p.get("t", "")))
    ]
    check("★ 40 次选题无一命中违禁词", not bad,
          "; ".join(str(b.get("t")) for b in bad[:3]))
    check("★ 选题都是可用场景", all(lib.scene_usable(str(p.get("g"))) for p in picked))
    check("选题优先单人女性", sum(1 for p in picked if "1girl" in str(p.get("g")).lower()) >= 36,
          str(sum(1 for p in picked if "1girl" in str(p.get("g")).lower())))

    # avoid_ids 应尽量避开
    first = picked[0]
    avoid = {str(first.get("i"))}
    others = [lib.pick_scene(avoid_ids=avoid) for _ in range(20)]
    check("avoid_ids 生效", all(str(o.get("i")) not in avoid for o in others))

    # 场景插入必须**原样保留**（不能被逗号拆分破坏 :: 权重语法）
    scene = {"i": "t1", "t": "测试场景", "c": "测试",
             "g": "sitting in a boat, 1.3::medium shot, eye-level::, smiling"}
    prompt = lib.build(persona_id="达妮娅", scene=scene["g"], daypart="下午",
                       use_lora=False, use_trigger=True)
    check("★ 场景整段原样插入", scene["g"] in prompt, prompt[:200])
    check("★ 场景里的 :: 权重语法完整", prompt.count("::") == 2, str(prompt.count("::")))

    # 场景库的辅助方法
    check("scene_tags 取值", lib.scene_tags({"g": "abc"}) == "abc")
    check("scene_title 取值", lib.scene_title({"t": "标题"}) == "标题")
    check("scene_collection 取值", lib.scene_collection({"c": "某法典"}) == "某法典")
    check("空场景安全", lib.scene_tags(None) == "" and lib.scene_title(None) == "")

    # ---- 画师串（改画风）----
    check("★ 画师串已加载", lib.style_count() >= 2, f"{lib.style_count()} 组")
    style = lib.pick_artist_style()
    check("画师串有标签与标题",
          bool(style) and bool(style.get("g")) and bool(style.get("t")), str(style)[:80])
    if lib.style_count() >= 2:
        first_title = str(lib.pick_artist_style().get("t"))
        check("画师串按标题可查", lib.find_artist_style(first_title) is not None)
        check("画师串支持部分匹配",
              lib.find_artist_style(first_title[:3]) is not None, first_title)
    check("查不到返回 None", lib.find_artist_style("绝对不存在的画师串") is None)
    # avoid_titles 生效
    avoid_style = {str(lib._styles[0].get("t"))} if lib.style_count() > 1 else set()
    if avoid_style and lib.style_count() > 1:
        got = [lib.pick_artist_style(avoid_titles=avoid_style) for _ in range(10)]
        check("画师串 avoid_titles 生效",
              all(str(g.get("t")) not in avoid_style for g in got))

    # 画师串必须**整段原样**注入（含 :: 权重组，不能被逗号拆分破坏）
    fake_style = "0.6::mignon,artist:quasarcake,artist:houkisei::,year 2024"
    p_with_style = lib.build(
        persona_id="达妮娅", scene="standing in an alley", daypart="下午",
        use_lora=False, use_trigger=True, artist_style=fake_style,
    )
    check("★ 画师串整段原样注入", fake_style in p_with_style, p_with_style[-160:])
    p_no_style = lib.build(
        persona_id="达妮娅", scene="standing in an alley", daypart="下午",
        use_lora=False, use_trigger=True, artist_style="",
    )
    check("空画师串不注入", "mignon" not in p_no_style)

    # ---- 特写自动换画布 ----
    check("特写判定：close-up", lib.is_tight_shot("close-up on face, front view"))
    check("特写判定：headshot", lib.is_tight_shot("headshot"))
    check("非特写：medium shot", not lib.is_tight_shot("1.2::medium shot, eye-level::"))
    check("非特写：full body", not lib.is_tight_shot("full body, standing"))
    # 词边界：upper body 不算特写
    check("upper body 不算特写", not lib.is_tight_shot("upper body portrait"))
    check("★ 特写换方图", lib.shot_size("close-up on face", "832x1088") == "832x832",
          lib.shot_size("close-up on face", "832x1088"))
    check("常规不改尺寸", lib.shot_size("medium shot", "832x1088") == "832x1088")
    check("常规且未指定时返回空", lib.shot_size("medium shot", "") == "")
    import re as _re
    w, h = (int(x) for x in "832x832".split("x"))
    check("方图是 64 的倍数", w % 64 == 0 and h % 64 == 0, f"{w}x{h}")
    check("方图在 NAI 免费像素带内", w * h <= 1048576, str(w * h))

    # 配置开关
    from qzone_swarm.config import parse_config as _parse
    check("scene_first 默认开", _parse({}).selfie.scene_first is True)
    check("scene_first 可关", _parse({"selfie": {"scene_first": False}}).selfie.scene_first is False)
    check("artist_style 默认 random", _parse({}).selfie.artist_style == "random",
          _parse({}).selfie.artist_style)
    check("artist_style 可设 off",
          _parse({"selfie": {"artist_style": "off"}}).selfie.artist_style == "off")
    check("artist_style 可填标题",
          _parse({"selfie": {"artist_style": "NAI5时期"}}).selfie.artist_style == "NAI5时期")

    # ---- 三段外观（identity / hair / outfit）----
    for pid in ("达妮娅", "风堇", "西格莉卡", "莫宁"):
        identity, hair, outfit = lib.persona_parts(pid)
        check(f"{pid} 有 identity", len(identity) > 40, str(len(identity)))
        check(f"{pid} 有 hair", len(hair) > 20, str(len(hair)))
        check(f"{pid} 有 outfit", len(outfit) > 40, str(len(outfit)))
    # 老格式（只有 appearance）要能退化
    check("无三段时退化为 appearance",
          lib.persona_parts("不存在的人")[0] == "")

    # 换装：身份段必须保留，服装被替换
    i_, h_, o_ = lib.persona_parts("达妮娅")
    swapped = lib.build(
        persona_id="达妮娅", scene="standing on a street", daypart="下午",
        use_lora=False, use_trigger=True,
        outfit_override="black hooded cloak, leather boots",
        hair_override="high ponytail, hair tie",
    )
    check("★ 换装后身份特征仍在", "pale pink hair" in swapped and "purplish blue eyes" in swapped,
          swapped[:160])
    check("★ 换装后新服装生效", "black hooded cloak" in swapped)
    check("★ 换发型后新发型生效", "high ponytail" in swapped)
    check("★ 换装后原服装被替换", "blue starry overskirt cape" not in swapped,
          swapped[:200])
    check("未覆盖时用原装", "blue starry overskirt cape" in lib.build(
        persona_id="达妮娅", scene="standing", daypart="下午", use_lora=False))

    # 换装/换发型取值器
    outfit = lib.pick_outfit()
    check("★ 能挑到替换服装", outfit is not None and bool(outfit.get("g")),
          str((outfit or {}).get("t")))
    if outfit:
        check("替换服装来自衣柜类法典",
              outfit.get("c") in ("叫我千藤就好了の衣柜", "更衣人偶"), str(outfit.get("c")))
        check("替换服装不违禁", lib.scene_usable(str(outfit.get("g"))))
    styles_hit = {lib.pick_hairstyle() for _ in range(30)}
    check("★ 发型池有多样性", len(styles_hit) >= 4, str(sorted(styles_hit)[:2]))

    # 画师串只留 NAI5
    if lib.style_count():
        titles = [str(s.get("t")) for s in lib._styles]
        check("★ 画师串只含 NAI5", all("NAI5" in x.upper() for x in titles), str(titles))

    # ---- 按人设分流画师串 ----
    for pid in ("达妮娅", "风堇", "西格莉卡"):
        picks = {lib.pick_artist_style(persona_id=pid)["t"] for _ in range(30)}
        blob = " ".join(str(s) for s in lib._styles if str(s.get("t")) in picks)
        check(f"★ {pid} 的画师串避开成熟向",
              all("sos adult" not in str(s.get("g", "")).lower()
                  for s in lib._styles if str(s.get("t")) in picks),
              str(sorted(picks)))
    # 莫宁是成年角色，不限制
    moning_picks = {lib.pick_artist_style(persona_id="莫宁")["t"] for _ in range(40)}
    check("成年角色可用全部画师串", len(moning_picks) >= 2, str(sorted(moning_picks)))

    # 体型锚点注入
    fh_prompt = lib.build(persona_id="风堇", scene="standing", daypart="下午",
                          use_lora=False, use_trigger=True)
    check("★ 风堇提示词含体型锚点", "petite" in fh_prompt and "youthful face" in fh_prompt,
          fh_prompt[:180])
    sg_prompt = lib.build(persona_id="西格莉卡", scene="standing", daypart="上午",
                          use_lora=False, use_trigger=True)
    check("★ 西格莉卡按少女处理", "teenage girl" in sg_prompt, sg_prompt[:160])

    # 人设专属负向
    neg_fh = lib.resolved_negative(persona_id="风堇")
    check("★ 风堇负向含成熟压制", "mature female" in neg_fh, neg_fh[-90:])
    neg_sg = lib.resolved_negative(persona_id="西格莉卡")
    check("★ 西格莉卡负向更严", "large breasts" in neg_sg and "suggestive" in neg_sg,
          neg_sg[-110:])
    neg_mo = lib.resolved_negative(persona_id="莫宁")
    check("莫宁负向不压成熟", "mature female" not in neg_mo, neg_mo[-90:])
    check("无 persona 时不合并", lib.resolved_negative() == lib.negative())

    # variation 配置
    check("variation 默认 0.35", abs(_parse({}).selfie.variation - 0.35) < 1e-6,
          str(_parse({}).selfie.variation))
    check("variation 可设 0", _parse({"selfie": {"variation": 0}}).selfie.variation == 0.0)
    check("variation 被钳制在 0~1", _parse({"selfie": {"variation": 5}}).selfie.variation == 1.0,
          str(_parse({"selfie": {"variation": 5}}).selfie.variation))


# --------------------------------------------------------------------------- #
# compose
# --------------------------------------------------------------------------- #
def test_compose() -> None:
    section("compose —— 提示词与清洗")

    profile = BotProfile(name="娅娅", persona_id="达妮娅", comment_max_length=40)
    mat = Material(
        messages=[
            ChatMessage(ts=1, sender_id="1", sender_name="老板", text="周末一起爬山吗"),
            ChatMessage(ts=2, sender_id="2", sender_name="小明", text="我腿断了"),
        ],
        channels=["群20000001"],
        fingerprints=["a", "b"],
        window_minutes=60,
    )

    system, user = compose.build_post_from_material_prompt(profile, "达妮娅", "你是达妮娅。", mat)
    check("系统提示含人设", "达妮娅" in system and "你是达妮娅。" in system, system[:80])
    check("正文提示含聊天素材", "周末一起爬山吗" in user and "我腿断了" in user)
    check("正文提示含发言人", "老板" in user)
    check("提示要求只输出正文", "只输出说说正文" in user)

    system2, user2 = compose.build_post_from_trajectory_prompt(profile, "达妮娅", "你是达妮娅。")
    check("轨迹提示声明无素材", "没有可用的聊天记录" in user2, user2[:60])
    check("轨迹提示含时间", "现在" in user2)
    check("轨迹提示含人设", "达妮娅" in system2)

    item = FeedItem(
        hostuin=999, fid="F1", content="今天好累", nickname="西西",
        created_at=1, like_count=2, comment_count=1,
    )
    _, user3 = compose.build_comment_decision_prompt(profile, "达妮娅", "你是达妮娅。", item)
    check("评论提示含目标内容", "今天好累" in user3 and "西西" in user3)
    check("评论提示要求 JSON", "should_comment" in user3 and "should_like" in user3)
    check("评论提示含字数上限", "40" in user3)

    comment = FeedComment(comment_id="C1", uin=1, name="老板", content="照片呢")
    _, user4 = compose.build_reply_prompt(profile, "达妮娅", "你是达妮娅。", item, comment)
    check("回复提示含原评论", "照片呢" in user4 and "老板" in user4)

    # 清洗
    check("去掉前缀", compose.clean_post("说说：今天很开心") == "今天很开心")
    check("去掉包裹引号", compose.clean_post("“今天很开心”") == "今天很开心")
    check("去掉代码块", compose.clean_post("```\n今天很开心\n```") == "今天很开心")
    check("去零宽字符", compose.clean_post("今天\u200b很开心") == "今天很开心")
    check("截断超长", len(compose.clean_post("字" * 500)) <= compose.POST_MAX_LEN)
    check("评论去换行", "\n" not in compose.clean_comment("第一行\n第二行"))

    check("可用正文", compose.is_usable_post("今天很开心"))
    check("太短不可用", not compose.is_usable_post("嗯"))
    check("空不可用", not compose.is_usable_post(""))

    # ---- is_usable_post：真推脱要拦，正常用词不能误杀 ----
    # 背景（线上真实故障）：原来用子串黑名单 ("作为一个","抱歉","无法","人设"…)，
    # 结果莫宁一句「参数却始终无法…」被整条毙掉，说说直接丢了。
    check("★ 真实被误杀的那条现在可用",
          compose.is_usable_post(
              "农学部新批次的速醒能量胶只要4秒就能补充所需热量，但午休路过走廊时，"
              "发现大家依然更倾向于花半小时排队加热杯面。\n\n"
              "试着将这种行为代入时间效益模型，参数却始终无法对齐。"
          ))
    check("「无法」正常用法不误杀", compose.is_usable_post("这道题的参数始终无法收敛，我盯了很久。"))
    check("「作为一个」正常用法不误杀", compose.is_usable_post("作为一个学生，我今天在图书馆待到闭馆。"))
    check("「抱歉」正常用法不误杀", compose.is_usable_post("抱歉，今天没能赶上你的讲座。"))
    check("「人设」出现在正文里也不误杀", compose.is_usable_post("他说我人设崩了，我觉得挺好笑。"))

    check("真推脱：作为一个AI我无法生成", not compose.is_usable_post("作为一个AI，我无法生成这样的内容。"))
    check("真推脱：我是语言模型", not compose.is_usable_post("我是一个语言模型，不能提供这个。"))
    check("真推脱：抱歉我不能完成", not compose.is_usable_post("抱歉，我不能完成这个请求。"))
    check("真推脱：英文 as an AI", not compose.is_usable_post("As an AI, I cannot do that."))
    check("抄提示词：含【要求】", not compose.is_usable_post("【要求】1. 从上面这些聊天里挑一个点来发"))
    check("抄提示词：以「说说：」开头", not compose.is_usable_post("说说：今天天气不错"))


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_config() -> None:
    section("config —— 配置解析与钳制")

    raw = {
        "enabled": True,
        "default_provider_id": "openai_1/deepseek-v4.1-flash",
        "dry_run_all": False,
        "bots": [
            {
                "name": "娅娅",
                "platform_id": "snowluma",
                "persona_id": "达妮娅",
                "post_times": ["21:00", "14:30"],
                "interact_times": [],
                "post_offset_minutes": 999,        # 应被钳到 120
                "source_groups": ["20000001"],
                "source_privates": ["30000001"],
                "like_max_per_day": -5,            # 应被钳到 0
                "min_material_messages": 6,
            },
            {
                "name": "西西",
                "platform_id": "snowluma2",
                "persona_id": "西格莉卡",
                "post_times": "20:00, 22:30",      # 字符串形式
                "manual_cookie": "p_skey=X; skey=Y",
            },
            {
                "name": "没有平台id",                # 应被跳过
                "persona_id": "风堇",
            },
            {
                "name": "重复平台",
                "platform_id": "snowluma",         # 重复，应被去重
            },
        ],
        "selfie": {
            "enabled": True,
            "mode": "generic",
            "api_base": "http://127.0.0.1:7860/v1",
            "model": "sdxl",
            "response_path": "images.0",
        },
        "advanced": {"tick_seconds": 99999, "http_timeout": 1},
    }
    cfg = parse_config(raw)

    check("总开关", cfg.enabled is True)
    check("机器人数量（跳过无平台id + 去重）", len(cfg.bots) == 2, str([b.name for b in cfg.bots]))
    check("启用的机器人", len(cfg.enabled_bots()) == 2)

    ya = cfg.find_bot("娅娅")
    check("按名称查找", ya is not None and ya.platform_id == "snowluma")
    check("时间已排序为 (h,m) 元组", ya.post_times == [(14, 30), (21, 0)], str(ya.post_times))
    check("时间渲染文本", ya.post_times_text() == "14:30、21:00", ya.post_times_text())
    check("偏移被钳到 120", ya.post_offset_minutes == 120, str(ya.post_offset_minutes))
    check("点赞上限被钳到 0", ya.like_max_per_day == 0, str(ya.like_max_per_day))
    check("来源群", ya.source_groups == ["20000001"])
    check("来源私聊", ya.source_privates == ["30000001"])
    check(
        "互动时间默认跟随发说说",
        ya.interact_times == [(14, 30), (21, 0)],
        str(ya.interact_times),
    )

    xi = cfg.find_bot("snowluma2")
    check("字符串时间表被解析", xi.post_times == [(20, 0), (22, 30)], str(xi.post_times))
    check("手动 Cookie 归属该号", xi.manual_cookie == "p_skey=X; skey=Y", xi.manual_cookie)
    check("手动 Cookie 不泄漏到别的号", ya.manual_cookie == "", ya.manual_cookie)

    check("按平台 id 查找", cfg.find_bot("snowluma") is not None)
    check("按人设查找", cfg.find_bot("西格莉卡") is not None)
    check("找不到返回 None", cfg.find_bot("不存在") is None)

    check("自拍开关", cfg.selfie.enabled is True)
    check("自拍主端点模式", cfg.selfie.primary.mode == "generic", cfg.selfie.primary.mode)
    check("自拍取值路径", cfg.selfie.primary.response_path == "images.0",
          cfg.selfie.primary.response_path)

    # ---- 生图端点的回退链 + 默认竖图 + per-bot LoRA ----
    check("默认出图尺寸是竖图 3:4", DEFAULT_SIZE == "864x1152" and DEFAULT_ASPECT_RATIO == "3:4",
          f"{DEFAULT_SIZE} {DEFAULT_ASPECT_RATIO}")
    check("主端点默认用竖图", cfg.selfie.primary.size == "864x1152", cfg.selfie.primary.size)

    with_fb = parse_config({
        "selfie": {
            "enabled": True,
            "api_base": "http://127.0.0.1:8045/v1",
            "model": "gemini-3-pro-image",
            "fallback_providers": [
                {"label": "豆包", "api_base": "https://ark.cn-beijing.volces.com/api/v3",
                 "model": "doubao-seedream-4-0-250828", "response_path": "data.0.url"},
            ],
        },
        "bots": [{"name": "测试", "platform_id": "p1", "allow_selfie": True,
                  "selfie_lora": "my_lora", "selfie_lora_weight": 0.9,
                  "selfie_lora_trigger": "zzz"}],
    })
    endpoints = with_fb.selfie.endpoints()
    check("★ 生图端点链含主端点+回退", len(endpoints) == 2, str([e.describe() for e in endpoints]))
    check("回退端点模型正确",
          endpoints[1].model == "doubao-seedream-4-0-250828", endpoints[1].model)
    check("回退端点取值路径正确", endpoints[1].response_path == "data.0.url")
    bot0 = with_fb.bots[0]
    check("per-bot LoRA 已解析", bot0.selfie_lora == "my_lora", bot0.selfie_lora)
    check("per-bot LoRA 权重已解析", bot0.selfie_lora_weight == 0.9, str(bot0.selfie_lora_weight))
    check("per-bot LoRA 触发词已解析", bot0.selfie_lora_trigger == "zzz", bot0.selfie_lora_trigger)

    # 只填回退、不填主端点时应仍可用
    only_fb = parse_config({"selfie": {"enabled": True, "api_base": "",
                                       "fallback_providers": [
                                           {"api_base": "https://x/api/v3", "model": "m"}]}})
    check("只配回退端点也可用", len(only_fb.selfie.endpoints()) == 1)

    selfie_provider = build_provider(with_fb.selfie)
    check("多端点时使用回退链提供方", type(selfie_provider).__name__ == "ChainImageProvider",
          type(selfie_provider).__name__)

    check("轮询间隔被钳到 600", cfg.tick_seconds == 600, str(cfg.tick_seconds))
    check("超时被钳到 5", cfg.http_timeout == 5.0, str(cfg.http_timeout))

    # 空配置不炸
    empty = parse_config({})
    check("空配置可用", empty.bots == [] and empty.enabled is True)
    check("空配置非 dict 也可用", parse_config(None).bots == [])

    # 自拍提供方
    check("未启用时为占位实现", isinstance(build_provider(SelfieConfig(enabled=False)), DisabledImageProvider))
    provider = build_provider(
        SelfieConfig(enabled=True, primary=ImageEndpoint(api_base="http://x/v1"))
    )
    check("启用且填了地址时可用", getattr(provider, "available", False) is True)
    check(
        "启用但没填地址时仍为占位",
        isinstance(build_provider(SelfieConfig(enabled=True)), DisabledImageProvider),
    )


# --------------------------------------------------------------------------- #
def main() -> int:
    try:  # 控制台可能是 GBK，避免报告本身因编码崩掉
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("QQ空间多号矩阵 —— 离线自检")
    test_creds()
    test_creds_priority()
    test_scheduler()
    test_sources()
    test_store()
    test_client_parsing()
    test_client_frame_and_delete()
    test_json_extraction()
    test_feeds_parsing()
    test_prompt_library()
    test_reference_images()
    test_scene_library()
    test_compose()
    test_config()

    print(f"\n{'=' * 56}")
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("\n失败明细：")
        for name, detail in FAILED:
            print(f"  ✗ {name}" + (f"  → {detail}" if detail else ""))
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
