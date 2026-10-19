"""QQ空间多号矩阵 —— AstrBot 插件入口。

设计目标
--------
让**多个 QQ 号在同一套 AstrBot 里各自独立地**运营自己的 QQ 空间：
各自定时发说说、评论、回复、点赞，各自读各自指定群聊/私聊的聊天记录，
各自用自己的模型与人设。互不顶号、互不串上下文。

隔离实现要点
------------
* 每个机器人 = 一个 AstrBot 平台实例（各自的 OneBot 连接）＋ 一个 :class:`BotRuntime`。
* 每个 runtime 独占：Cookie/g_tk、QzoneClient、状态文件、素材缓冲、串行锁、自拍提供方。
* 取 OneBot 客户端只用自己的 ``platform_id``，绝无全局"当前机器人"。
* 每次 LLM 调用都是无状态的（``contexts=[]``），不存在跨号对话上下文。
* 所有对外动作在每号自己的 ``asyncio.Lock`` 内串行，避免同号并发触发风控。
* 定时槽位带**确定性**随机偏移，天然把多个号错开，不会同一秒集体动作。

本文件是插件里唯一（连同 ``qzone_swarm/bridge.py``）依赖 astrbot 的地方。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Any, AsyncGenerator

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from astrbot.api import AstrBotConfig  # noqa: E402
from astrbot.api.event import AstrMessageEvent, filter  # noqa: E402
from astrbot.api.event.filter import PermissionType  # noqa: E402
from astrbot.api.star import Context, Star  # noqa: E402
from astrbot.core.star.filter import HandlerFilter  # noqa: E402

from qzone_swarm.bridge import AstrBotBridge, log_startup_summary  # noqa: E402
from qzone_swarm.config import GlobalConfig, parse_config  # noqa: E402
from qzone_swarm.logging_compat import get_logger  # noqa: E402
from qzone_swarm.models import ChatMessage  # noqa: E402
from qzone_swarm.runtime import BotRuntime  # noqa: E402
from qzone_swarm.scheduler import describe_schedule, due_slots  # noqa: E402
from qzone_swarm.sources import MessageStore  # noqa: E402
from qzone_swarm.store import BotStore  # noqa: E402
from qzone_swarm.util import now  # noqa: E402

PLUGIN_NAME = "astrbot_plugin_qzone_swarm"
log = get_logger("main")


# --------------------------------------------------------------------------- #
# 被动采集过滤器：永远通过、永不阻断、永不抛错给平台
# --------------------------------------------------------------------------- #
class PassiveCaptureFilter(HandlerFilter):
    """始终放行，用于"只看不做"地采集聊天记录。"""

    def __init__(self, raise_error: bool = False) -> None:
        self.raise_error = raise_error

    def filter(self, event: AstrMessageEvent, cfg: Any) -> bool:
        return True


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #
class QzoneSwarmPlugin(Star):
    """QQ空间多号矩阵。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.raw_config: dict[str, Any] = dict(config) if isinstance(config, dict) else {}
        self.config: GlobalConfig = parse_config(self.raw_config)
        self.messages = MessageStore()
        self.bridge = AstrBotBridge(context, self.config)
        self.runtimes: dict[str, BotRuntime] = {}
        self.data_dir = self._resolve_data_dir()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._last_tick = 0.0
        # 平台实例暂不可用时的限频告警时间戳（platform_id -> ts）
        self._platform_warned_at: dict[str, float] = {}
        self._build_runtimes()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def _resolve_data_dir(self) -> str:
        try:
            from astrbot.core.star import StarTools

            base = StarTools.get_data_dir(PLUGIN_NAME)
            if base is not None:
                path = str(base)
                os.makedirs(path, exist_ok=True)
                return path
        except Exception as exc:
            log.debug("StarTools.get_data_dir 不可用：%s", exc)
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            path = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
            os.makedirs(path, exist_ok=True)
            return path
        except Exception as exc:
            log.debug("get_astrbot_plugin_data_path 不可用：%s", exc)
        fallback = os.path.join(_PLUGIN_DIR, "data")
        os.makedirs(fallback, exist_ok=True)
        return fallback

    def _build_runtimes(self) -> None:
        """按配置重建每号运行单元。已存在的 runtime 若配置未变则保留。"""
        for profile in self.config.bots:
            existing = self.runtimes.get(profile.platform_id)
            if existing is not None:
                existing.profile = profile
                existing.config = self.config
                continue
            store = BotStore(self.data_dir, profile.platform_id).load()
            self.runtimes[profile.platform_id] = BotRuntime(
                profile,
                bridge=self.bridge,
                config=self.config,
                store=store,
                messages=self.messages,
            )
        # 清掉配置里已删除的号
        configured = {p.platform_id for p in self.config.bots}
        for platform_id in list(self.runtimes):
            if platform_id not in configured:
                self.runtimes.pop(platform_id, None)

    async def initialize(self) -> None:
        log_startup_summary(self.config)
        available = self.bridge.platform_ids()
        if not available:
            # 插件是在 AstrBot 启动流程里被加载的，此时平台适配器往往还没起来
            # （本机实测：本插件与 proactive_chat 都会先于平台加载）。
            # 这不是错误 —— 运行期的 _tick 每轮都会重新取一次平台实例。
            log.info(
                "[qzone_swarm] 平台适配器尚未加载（启动早期属正常）；"
                "已配置 %d 个号，将在其上线后自动开始工作。",
                len(self.config.bots),
            )
        else:
            for profile in self.config.bots:
                if profile.platform_id not in available:
                    log.warning(
                        "[qzone_swarm] 机器人「%s」配置的平台实例 %s 不存在；可用实例：%s",
                        profile.name,
                        profile.platform_id,
                        "、".join(available),
                    )
        if not self.config.enabled:
            log.info("[qzone_swarm] 插件总开关为关，定时任务不会启动")
            return
        self._stopping = False
        self._task = asyncio.create_task(self._tick_loop(), name="qzone_swarm_scheduler")
        log.info(
            "[qzone_swarm] 调度已启动：%d 个机器人，轮询间隔 %ds，数据目录 %s",
            len(self.runtimes),
            self.config.tick_seconds,
            self.data_dir,
        )

    async def terminate(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for runtime in self.runtimes.values():
            try:
                await runtime.close()
            except Exception as exc:
                log.debug("关闭 %s 失败：%s", runtime.platform_id, exc)
        log.info("[qzone_swarm] 已停止")

    # ------------------------------------------------------------------ #
    # 调度
    # ------------------------------------------------------------------ #
    async def _tick_loop(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("[qzone_swarm] 调度轮询异常：%s", exc)
            try:
                await asyncio.sleep(max(10, self.config.tick_seconds))
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> None:
        if not self.config.enabled or self._stopping:
            return
        self._last_tick = now()
        current = datetime.now()

        for profile in self.config.enabled_bots():
            runtime = self.runtimes.get(profile.platform_id)
            if runtime is None:
                continue

            # 平台实例还没起来时**不要**往下走 —— 否则到点的槽位会被白白烧掉。
            # 常见于 AstrBot 启动早期：插件先加载、平台适配器后加载。
            # 这里直接跳过，槽位保持未执行，等平台上线后仍会在宽限期内正常触发。
            if self.bridge.get_onebot_client(profile.platform_id) is None:
                self._warn_platform_missing(profile)
                continue

            # ---- 发说说 ----
            if profile.post_enabled and profile.post_times:
                await self._run_slots(runtime, current, kind="post")

            # ---- 互动 ----
            if profile.interact_enabled and profile.interact_times:
                await self._run_slots(runtime, current, kind="interact")

    def _warn_platform_missing(self, profile) -> None:
        """平台暂不可用时的限频提示（同一号 5 分钟最多一次）。

        区分两种成因，便于排查：
            * 平台实例压根没加载（配置里没有 / 还没起来）；
            * 实例在，但 OneBot 连接还没就绪。
        """
        key = profile.platform_id
        last = self._platform_warned_at.get(key, 0.0)
        if now() - last < 300:
            return
        self._platform_warned_at[key] = now()

        loaded = self.bridge.platform_ids()
        if key not in loaded:
            reason = f"平台实例尚未加载（已加载：{'、'.join(loaded) or '无'}）"
        else:
            reason = "平台实例已加载，但 OneBot 连接尚未就绪"
        log.warning(
            "[qzone_swarm] 机器人「%s」的 %s 暂不可用，本轮跳过（不消耗时间点）：%s",
            profile.name,
            key,
            reason,
        )

    async def _run_slots(self, runtime: BotRuntime, current: datetime, *, kind: str) -> None:
        profile = runtime.profile
        if kind == "post":
            times = profile.post_times
            offset = profile.post_offset_minutes
            grace = self.config.post_grace_minutes
            quota_kind, quota_limit = "post", profile.post_max_per_day
        else:
            times = profile.interact_times
            offset = profile.interact_offset_minutes
            grace = self.config.interact_grace_minutes
            quota_kind, quota_limit = "interact", 0  # 互动配额在内部按 like/comment/reply 细分

        fired = {
            key for key in (runtime.store.data.get("fired_slots") or [])
        }
        due = due_slots(
            current,
            times,
            kind=kind,
            platform_id=profile.platform_id,
            fired_keys=fired,
            offset_minutes=offset,
            grace_minutes=grace,
        )
        if not due:
            return

        for slot in due:
            # 先落盘标记，避免本轮执行耗时长导致下一轮重复触发
            runtime.store.mark_slot_fired(slot.key)
            runtime.store.save()

            if quota_kind == "post":
                if quota_limit <= 0:
                    log.info("[%s] 今日发说说配额为 0，跳过 %s", profile.name, slot.key)
                    continue
                if runtime.store.count_today("post") >= quota_limit:
                    log.info(
                        "[%s] 今日发说说已达上限 %d，跳过 %s",
                        profile.name, quota_limit, slot.key,
                    )
                    continue

            log.info("[%s] 触发定时任务 %s", profile.name, slot.describe())
            if kind == "post":
                outcome = await runtime.publish_post(trigger=f"scheduled:{slot.key}")
                if outcome.get("ok") and not outcome.get("dry_run"):
                    runtime.store.bump_today("post")
                    runtime.store.save()
                await self._report(runtime, outcome, kind="发说说")
            else:
                outcome = await runtime.run_interactions(trigger=f"scheduled:{slot.key}")
                await self._report(runtime, outcome, kind="互动")

    async def _report(self, runtime: BotRuntime, outcome: dict[str, Any], *, kind: str) -> None:
        """把结果回报给管理员并记录日志。"""
        profile = runtime.profile
        dry = outcome.get("dry_run")
        prefix = "[演练] " if dry else ""

        if not outcome.get("ok"):
            error = outcome.get("error") or "未知错误"
            runtime.set_last_error(error)
            log.warning("[%s] %s失败：%s", profile.name, kind, error)
            target = self.bridge.resolve_notify_target(profile)
            if target:
                await self.bridge.notify(target, f"⚠️「{profile.name}」{kind}失败：{error}")
            return

        runtime.set_last_error("")
        if kind == "发说说":
            text = outcome.get("content", "")
            log.info("[%s] %s发说说成功：%s", profile.name, prefix, text[:60])
            target = self.bridge.resolve_notify_target(profile)
            if target and not dry:
                await self.bridge.notify(
                    target,
                    f"📝「{profile.name}」已发说说"
                    f"{'（用了聊天素材）' if outcome.get('used_material') else '（人设行动轨迹）'}：\n{text}",
                )
        else:
            actions = outcome.get("actions") or []
            if actions:
                log.info("[%s] %s完成 %d 项互动", profile.name, prefix, len(actions))
                target = self.bridge.resolve_notify_target(profile)
                if target and not dry:
                    await self.bridge.notify(
                        target,
                        f"💬「{profile.name}」完成 {len(actions)} 项互动：\n"
                        + "\n".join(actions[:10]),
                    )

    # ------------------------------------------------------------------ #
    # 聊天记录采集（只看不做）
    # ------------------------------------------------------------------ #
    @filter.custom_filter(PassiveCaptureFilter, False)
    async def on_any_message(self, event: AstrMessageEvent) -> None:
        """被动记录配置来源里的聊天记录，用于之后生成说说。"""
        try:
            self._capture(event)
        except Exception as exc:
            log.debug("[qzone_swarm] 记录聊天消息失败：%s", exc)

    def _capture(self, event: AstrMessageEvent) -> None:
        if not self.config.enabled:
            return
        platform_id = event.get_platform_id()
        runtime = self.runtimes.get(platform_id)
        if runtime is None:
            return
        profile = runtime.profile
        if not (profile.enabled and profile.use_material):
            return

        is_group = not event.is_private_chat()
        kind = "group" if is_group else "private"
        channel_id = str(event.get_group_id() if is_group else event.get_sender_id()).strip()
        if not channel_id:
            return

        # 只采集该号自己配置的来源
        allowed = profile.source_groups if is_group else profile.source_privates
        if channel_id not in {str(x).strip() for x in allowed}:
            return

        text = (event.get_message_str() or "").strip()
        sender_id = str(event.get_sender_id() or "")
        self_id = str(event.get_self_id() or "")
        is_bot = bool(sender_id and self_id and sender_id == self_id)

        if not text and not is_bot:
            return

        self.messages.record(
            ChatMessage(
                ts=now(),
                sender_id=sender_id,
                sender_name=event.get_sender_name() or sender_id,
                text=text,
                session_key=f"{kind}:{channel_id}",
                session_label=f"{'群' if is_group else '私聊'}{channel_id}",
                is_bot=is_bot,
                msg_id=str(getattr(event.message_obj, "message_id", "") or ""),
            ),
            platform_id=platform_id,
            kind=kind,
            channel_id=channel_id,
        )

    # ------------------------------------------------------------------ #
    # 命令
    # ------------------------------------------------------------------ #
    @filter.command_group("qzswarm")
    async def qzswarm(self, event: AstrMessageEvent):
        """QQ空间多号矩阵：多机器人独立发说说/评论/回复/点赞。"""
        yield event.plain_result(
            "QQ空间多号矩阵\n"
            "可用子命令：\n"
            "  /qzswarm status [机器人]     查看运行状态\n"
            "  /qzswarm bind <机器人>       重新获取该号 QQ空间登录态\n"
            "  /qzswarm post <机器人> [dry] 立即发一条说说（dry=只生成不发布）\n"
            "  /qzswarm preview <机器人>    只生成看看会发什么\n"
            "  /qzswarm interact <机器人> [dry] 立即跑一轮互动\n"
            "  /qzswarm enable|disable <机器人>\n"
            "  /qzswarm personas            列出可用人设\n"
            "  /qzswarm sources [机器人]    查看素材来源与缓冲情况\n"
            "  /qzswarm reload              重新载入插件配置\n"
        )

    @filter.permission_type(PermissionType.ADMIN)
    @qzswarm.command("status")
    async def qzswarm_status(self, event: AstrMessageEvent, bot: str = "") -> AsyncGenerator[Any, None]:
        """[管理员] 查看机器人运行状态"""
        targets = self._select_runtimes(bot)
        if not targets:
            yield event.plain_result(self._no_bot_message(bot))
            return
        if not self.config.enabled:
            yield event.plain_result("⚠️ 插件总开关处于关闭状态，定时任务不会执行。")
        current = datetime.now()
        blocks: list[str] = []
        for runtime in targets:
            block = runtime.status_text()
            sched = describe_schedule(
                current,
                runtime.profile.post_times,
                platform_id=runtime.profile.platform_id,
                post_offset=runtime.profile.post_offset_minutes,
                interact_offset=runtime.profile.interact_offset_minutes,
            )
            blocks.append(f"{block}\n  下次：{sched}")
        yield event.plain_result("\n\n".join(blocks))

    @filter.permission_type(PermissionType.ADMIN)
    @qzswarm.command("bind")
    async def qzswarm_bind(self, event: AstrMessageEvent, bot: str = "") -> AsyncGenerator[Any, None]:
        """[管理员] 重新获取指定机器人的 QQ空间登录态"""
        runtime = self._pick_one(bot)
        if runtime is None:
            yield event.plain_result(self._no_bot_message(bot))
            return
        creds = await runtime.ensure_credentials(force=True)
        if creds.valid:
            runtime.store.save()
            yield event.plain_result(f"✅「{runtime.profile.name}」登录态已刷新：\n{creds.summary()}")
        else:
            yield event.plain_result(
                f"❌「{runtime.profile.name}」未能取得可用登录态。\n"
                f"uin={creds.uin or '未知'}　来源={creds.source or '无'}\n"
                "请确认该号的 OneBot 连接在线（协议端需支持 get_credentials/get_cookies），"
                "或在配置里为该号填写 manual_cookie。"
            )

    @filter.permission_type(PermissionType.ADMIN)
    @qzswarm.command("preview")
    async def qzswarm_preview(self, event: AstrMessageEvent, bot: str = "") -> AsyncGenerator[Any, None]:
        """[管理员] 只生成一条说说，不发布"""
        runtime = self._pick_one(bot)
        if runtime is None:
            yield event.plain_result(self._no_bot_message(bot))
            return
        yield event.plain_result(f"⏳ 正在为「{runtime.profile.name}」生成…")
        try:
            draft = await runtime.compose_preview()
            source = "聊天素材" if draft["used_material"] else "人设行动轨迹"
            lines = [
                f"📝「{runtime.profile.name}」预览（来源：{source}）",
                "",
                draft["content"],
            ]
            if draft["used_material"]:
                lines.append("")
                lines.append(f"取材会话：{'、'.join(draft['channels'])}")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"❌ 生成失败：{exc}")

    @filter.permission_type(PermissionType.ADMIN)
    @qzswarm.command("post")
    async def qzswarm_post(self, event: AstrMessageEvent, bot: str = "", dry: str = "") -> AsyncGenerator[Any, None]:
        """[管理员] 立即发一条说说（dry 参数用于演练）"""
        runtime = self._pick_one(bot)
        if runtime is None:
            yield event.plain_result(self._no_bot_message(bot))
            return
        yield event.plain_result(f"⏳ 正在为「{runtime.profile.name}」发布…")
        outcome = await runtime.publish_post(
            trigger="command", dry_run=True if dry.strip().lower() in ("dry", "1", "true") else None
        )
        if outcome.get("ok"):
            tag = "[演练] " if outcome.get("dry_run") else ""
            yield event.plain_result(
                f"✅ {tag}「{runtime.profile.name}」发布成功\n"
                f"来源：{'聊天素材' if outcome.get('used_material') else '人设行动轨迹'}\n"
                f"正文：\n{outcome.get('content', '')}"
                + (f"\n\n⚠️ 自拍：{outcome['selfie_error']}" if outcome.get("selfie_error") else "")
            )
        else:
            yield event.plain_result(f"❌ 发布失败：{outcome.get('error')}")

    @filter.permission_type(PermissionType.ADMIN)
    @qzswarm.command("interact")
    async def qzswarm_interact(self, event: AstrMessageEvent, bot: str = "", dry: str = "") -> AsyncGenerator[Any, None]:
        """[管理员] 立即跑一轮互动（回复/点赞/评论）"""
        runtime = self._pick_one(bot)
        if runtime is None:
            yield event.plain_result(self._no_bot_message(bot))
            return
        yield event.plain_result(f"⏳「{runtime.profile.name}」正在互动…")
        outcome = await runtime.run_interactions(
            trigger="command", dry_run=True if dry.strip().lower() in ("dry", "1", "true") else None
        )
        if not outcome.get("ok"):
            yield event.plain_result(f"❌ 互动失败：{outcome.get('error')}")
            return
        actions = outcome.get("actions") or []
        tag = "[演练] " if outcome.get("dry_run") else ""
        body = "\n".join(actions) if actions else (outcome.get("note") or "本轮没有需要做的互动")
        yield event.plain_result(f"✅ {tag}「{runtime.profile.name}」互动完成：\n{body}")

    @filter.permission_type(PermissionType.ADMIN)
    @qzswarm.command("enable")
    async def qzswarm_enable(self, event: AstrMessageEvent, bot: str = "") -> AsyncGenerator[Any, None]:
        """[管理员] 启用某个机器人（仅本次运行内生效）"""
        runtime = self._pick_one(bot)
        if runtime is None:
            yield event.plain_result(self._no_bot_message(bot))
            return
        runtime.profile.enabled = True
        yield event.plain_result(f"✅「{runtime.profile.name}」已启用（仅本次运行有效）")

    @filter.permission_type(PermissionType.ADMIN)
    @qzswarm.command("disable")
    async def qzswarm_disable(self, event: AstrMessageEvent, bot: str = "") -> AsyncGenerator[Any, None]:
        """[管理员] 停用某个机器人（仅本次运行内生效）"""
        runtime = self._pick_one(bot)
        if runtime is None:
            yield event.plain_result(self._no_bot_message(bot))
            return
        runtime.profile.enabled = False
        yield event.plain_result(f"⏸️「{runtime.profile.name}」已停用（仅本次运行有效）")

    @filter.permission_type(PermissionType.ADMIN)
    @qzswarm.command("personas")
    async def qzswarm_personas(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """[管理员] 列出 AstrBot 中可用的人设"""
        names = await self.bridge.list_personas()
        if not names:
            yield event.plain_result("未读取到任何人设。")
            return
        yield event.plain_result("可用人设：\n" + "\n".join(f"· {n}" for n in names))

    @filter.permission_type(PermissionType.ADMIN)
    @qzswarm.command("sources")
    async def qzswarm_sources(self, event: AstrMessageEvent, bot: str = "") -> AsyncGenerator[Any, None]:
        """[管理员] 查看素材来源与当前缓冲"""
        targets = self._select_runtimes(bot)
        if not targets:
            yield event.plain_result(self._no_bot_message(bot))
            return
        blocks: list[str] = []
        for runtime in targets:
            counts = self.messages.channel_counts(runtime.profile.platform_id)
            sources = [c.key for c in runtime.channels()]
            lines = [f"【{runtime.profile.name}】素材来源：{('、'.join(sources)) or '未配置'}"]
            if counts:
                for key, count in sorted(counts.items()):
                    lines.append(f"  {key}：缓冲 {count} 条")
            else:
                lines.append("  （还没有采集到任何消息）")
            blocks.append("\n".join(lines))
        yield event.plain_result("\n\n".join(blocks))

    @filter.permission_type(PermissionType.ADMIN)
    @qzswarm.command("reload")
    async def qzswarm_reload(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """[管理员] 重新载入插件配置"""
        try:
            self.config = parse_config(self.raw_config)
            self.bridge.config = self.config
            self.bridge.invalidate_persona_cache()
            self._build_runtimes()
            yield event.plain_result(
                f"✅ 配置已重新载入：{len(self.config.enabled_bots())} 个启用的机器人。"
            )
        except Exception as exc:
            yield event.plain_result(f"❌ 重新载入失败：{exc}")

    # ------------------------------------------------------------------ #
    # 命令辅助
    # ------------------------------------------------------------------ #
    def _select_runtimes(self, key: str) -> list[BotRuntime]:
        key = (key or "").strip()
        if not key:
            return list(self.runtimes.values())
        bot = self.config.find_bot(key)
        if bot is not None and bot.platform_id in self.runtimes:
            return [self.runtimes[bot.platform_id]]
        # 退化：按平台 id 直接匹配
        for platform_id, runtime in self.runtimes.items():
            if key.lower() in platform_id.lower() or key in runtime.profile.name:
                return [runtime]
        return []

    def _pick_one(self, key: str) -> BotRuntime | None:
        key = (key or "").strip()
        targets = self._select_runtimes(key)
        if targets:
            return targets[0]
        if not key and len(self.runtimes) == 1:
            return next(iter(self.runtimes.values()))
        return None

    def _no_bot_message(self, key: str) -> str:
        names = [f"{r.profile.name}({pid})" for pid, r in self.runtimes.items()]
        if not names:
            return (
                "❌ 没有配置任何机器人。\n"
                "请在插件配置里添加「机器人」条目，并填写对应的 AstrBot 平台实例 id。"
            )
        if key:
            return f"❌ 找不到机器人「{key}」。已配置：{'、'.join(names)}"
        return f"❌ 请指定机器人。已配置：{'、'.join(names)}"
