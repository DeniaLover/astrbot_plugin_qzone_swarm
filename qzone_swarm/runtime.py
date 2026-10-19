"""单机器人运行时：一个 QQ 号的完整业务闭环。

**隔离契约**（本插件的核心承诺）：

1. 一个 :class:`BotRuntime` 只服务一个 AstrBot 平台实例（= 一个 QQ 号）。
2. 它独占自己的 Cookie/g_tk、QzoneClient、状态文件、素材去重表、串行锁、自拍提供方。
3. 所有 LLM 调用都是一次性的（``contexts=[]``），不存在跨号共享的对话上下文。
4. 取 OneBot 客户端时只用自己的 ``platform_id``，因此不可能读到别的号的登录态。
5. 任何对外动作都在 ``self._lock`` 内串行执行，避免同号并发导致的风控与状态错乱。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from .bridge import AstrBotBridge, BridgeError
from .client import QzoneAuthError, QzoneClient, QzoneError
from .compose import (
    clean_comment,
    clean_post,
    is_usable_post,
    build_comment_decision_prompt,
    build_post_from_material_prompt,
    build_post_from_trajectory_prompt,
    build_post_from_scene_prompt,
    build_reply_prompt,
    build_selfie_caption_prompt,
    build_selfie_prompt_prompt,
)
from .config import GlobalConfig
from .logging_compat import get_bot_logger, get_logger
from .models import BotProfile, Credentials, FeedItem, InteractionRecord, PostRecord
from .promptlib import PromptLibrary
from .selfie import ImageProvider, SelfieConfig, build_provider, load_references
from .sources import Material, MessageStore, SourceChannel, channel_from
from .store import BotStore
from .util import clean_text, extract_json_object, now

log = get_logger("runtime")

CREDS_TTL_SECONDS = 30 * 60


class BotRuntime:
    """一个机器人的运行单元。"""

    def __init__(
        self,
        profile: BotProfile,
        *,
        bridge: AstrBotBridge,
        config: GlobalConfig,
        store: BotStore,
        messages: MessageStore,
        selfie_provider: ImageProvider | None = None,
        prompt_library: PromptLibrary | None = None,
    ) -> None:
        self.profile = profile
        self.bridge = bridge
        self.config = config
        self.store = store
        self.messages = messages
        self.log = get_bot_logger(profile.name)

        self._lock = asyncio.Lock()
        self._client: QzoneClient | None = None
        self._creds: Credentials | None = None
        self._creds_at: float = 0.0
        self._selfie: ImageProvider = selfie_provider or build_provider(config.selfie)
        # 提示词库：外观锚点 / LoRA / 画风 / 光线 / 构图都由它统一提供
        self._prompt_lib = prompt_library or PromptLibrary.load(
            config.selfie.prompt_library or None
        )
        self._last_error: str = ""

    # ------------------------------------------------------------------ #
    # 基础
    # ------------------------------------------------------------------ #
    @property
    def platform_id(self) -> str:
        return self.profile.platform_id

    @property
    def uin(self) -> int:
        return self.store.uin or (self._creds.uin if self._creds else 0)

    def channels(self) -> list[SourceChannel]:
        out: list[SourceChannel] = []
        for kind, cid in self.profile.enabled_sources():
            label = f"{'群' if kind == 'group' else '私聊'}{cid}"
            out.append(channel_from(kind, cid, label))
        return out

    def _effective_dry_run(self, override: bool | None = None) -> bool:
        if override is not None:
            return bool(override)
        return bool(self.profile.dry_run or self.config.dry_run_all)

    async def ensure_credentials(self, *, force: bool = False) -> Credentials:
        """确保该号凭据可用；必要时（重新）从它自己的 OneBot 连接获取。"""
        fresh_enough = (
            self._creds is not None
            and self._creds.valid
            and not force
            and (now() - self._creds_at) < CREDS_TTL_SECONDS
        )
        if fresh_enough:
            return self._creds  # type: ignore[return-value]

        creds = await self.bridge.credentials_for(self.profile)
        if creds.valid:
            self._creds = creds
            self._creds_at = now()
            self.store.set_identity(creds.uin, creds.nickname)
            if self._client is None:
                self._client = QzoneClient(
                    creds,
                    timeout=self.config.http_timeout,
                    user_agent=self.config.user_agent,
                )
            else:
                self._client.refresh(creds)
            self.log.info("凭据就绪：%s", creds.summary())
        else:
            self.log.warning("凭据不可用：uin=%s 来源=%s", creds.uin or "?", creds.source or "无")
            if self._creds is not None and self._creds.valid:
                # 旧的还能用就别急着丢
                return self._creds
            self._creds = creds
        return creds

    async def acquire_client(self) -> QzoneClient:
        creds = await self.ensure_credentials()
        if not creds.valid:
            raise QzoneAuthError(
                f"机器人「{self.profile.name}」尚未取得可用的 QQ空间登录态"
                f"（uin={creds.uin or '未知'}，来源={creds.source or '无'}）。"
                "请确认该号的 OneBot 连接在线，或在配置里为该号填写 manual_cookie。"
            )
        assert self._client is not None
        return self._client

    async def persona(self) -> tuple[str, str]:
        """返回 (角色名, 人设文本)。"""
        name = self.profile.resolved_persona_name()
        prompt = self.profile.persona_prompt
        if not prompt and self.profile.persona_id:
            prompt = await self.bridge.persona_prompt(self.profile.persona_id)
        return name, prompt

    # ------------------------------------------------------------------ #
    # 素材
    # ------------------------------------------------------------------ #
    def collect_material(self):
        if not self.profile.use_material:
            return None
        channels = self.channels()
        if not channels:
            return None
        material = self.messages.collect(
            self.platform_id,
            channels,
            window_seconds=self.profile.material_window_minutes * 60,
            limit=self.profile.material_max_messages,
            ignore_users=self.profile.source_ignore_users,
            exclude_bot_self=str(self.uin or ""),
        )
        material = self.messages.filter_unused(self.platform_id, material)
        # 叠加状态文件里记录的历史指纹（跨重启仍然去重）
        used = self.store.material_used()
        if used:
            fresh = [m for m in material.messages if m.fingerprint() not in used]
            if len(fresh) != len(material.messages):
                material = Material(
                    messages=fresh,
                    channels=material.channels,
                    fingerprints=[m.fingerprint() for m in fresh],
                    window_minutes=material.window_minutes,
                )
        return material

    # ------------------------------------------------------------------ #
    # 发说说
    # ------------------------------------------------------------------ #
    async def compose_post(self) -> dict[str, Any]:
        """只生成内容，不发布。返回 dict(content/material/image_prompt/scene/...)。"""
        persona_name, persona_prompt = await self.persona()
        material = self.collect_material()
        used_material = bool(
            material
            and material.count >= max(1, self.profile.min_material_messages)
        )

        # ---- 场景优先 ---------------------------------------------------- #
        # 如果这次要发自拍，就先从场景库挑一张画面，**先定图再写文**，
        # 这样说说和配图天然对得上（反过来容易图文两张皮）。
        scene: dict[str, Any] | None = None
        if self._scene_first_enabled():
            scene = self._pick_scene()
            if scene is not None:
                title = self._prompt_lib.scene_title(scene)
                self.log.info(
                    "场景优先：选中《%s》（%s）", title, self._prompt_lib.scene_collection(scene)
                )
                system, user = build_post_from_scene_prompt(
                    self.profile,
                    persona_name,
                    persona_prompt,
                    title,
                    self._prompt_lib.scene_tags(scene),
                    self._prompt_lib.scene_collection(scene),
                )
                raw = await self.bridge.generate(
                    self.profile, system_prompt=system, user_prompt=user, purpose="写说说"
                )
                content = clean_post(raw)
                if not is_usable_post(content):
                    # 场景路线失败就退回常规路线，不要因此丢掉整次发布
                    self.log.warning("围绕场景生成的说说不可用，退回常规路线")
                    scene = None
                else:
                    return {
                        "content": content,
                        "material": None,
                        "used_material": False,
                        "channels": [],
                        "image_prompt": "",
                        "image_bytes": b"",
                        "selfie_error": "",
                        "scene": scene,
                    }

        if used_material:
            system, user = build_post_from_material_prompt(
                self.profile, persona_name, persona_prompt, material
            )
            self.log.info(
                "使用聊天素材：%d 条，来自 %s", material.count, material.channel_summary()
            )
        else:
            system, user = build_post_from_trajectory_prompt(
                self.profile, persona_name, persona_prompt
            )
            if material and material.count:
                self.log.info(
                    "素材不足（%d < %d），改用人设行动轨迹",
                    material.count,
                    self.profile.min_material_messages,
                )
            else:
                self.log.info("没有素材，使用人设行动轨迹")

        raw = await self.bridge.generate(
            self.profile,
            system_prompt=system,
            user_prompt=user,
            purpose="写说说",
        )
        content = clean_post(raw)
        if not is_usable_post(content):
            raise BridgeError(f"模型产出的说说不可用：{raw[:80]!r}")

        return {
            "content": content,
            "material": material if used_material else None,
            "used_material": used_material,
            "channels": material.channels if used_material else [],
            "image_prompt": "",
            "image_bytes": b"",
            "selfie_error": "",
            "scene": None,
        }

    # -- 场景优先 ---------------------------------------------------------- #
    def _scene_first_enabled(self) -> bool:
        """只有「真要发自拍」且场景库有货时才走场景优先。"""
        if not (self.profile.allow_selfie and self.config.selfie.enabled):
            return False
        if not getattr(self.config.selfie, "scene_first", True):
            return False
        if not getattr(self._selfie, "available", False):
            return False
        return self._prompt_lib.has_scenes()

    def _recent_scene_ids(self) -> set[str]:
        used = self.store.data.get("scenes_used") or []
        return {str(x) for x in used[-40:]}

    def _remember_scene(self, scene: dict[str, Any] | None) -> None:
        if not scene:
            return
        scene_id = str(scene.get("i") or "").strip()
        if not scene_id:
            return
        used = list(self.store.data.get("scenes_used") or [])
        used.append(scene_id)
        self.store.data["scenes_used"] = used[-80:]
        try:
            self.store.save()
        except Exception as exc:
            self.log.debug("记录场景使用失败：%s", exc)

    async def _check_anatomy(
        self, image: bytes, mime: str, attempt: int, retries: int
    ) -> bool | None:
        """让视觉模型判断这张图有没有人体结构错误。返回 True 表示需要重画。"""
        question = (
            "Look at this anime illustration carefully. "
            "Are there clear anatomical errors: extra/missing/fused limbs, "
            "more than two legs or two arms, or a limb attached at a wrong place? "
            "Answer with exactly one word: YES or NO."
        )
        try:
            verdict = await self.bridge.check_image(
                self.profile, image, question=question, mime=mime or "image/jpeg"
            )
        except Exception as exc:
            self.log.debug("人体复检异常（放行）：%s", exc)
            return None
        if verdict is True:
            self.log.info("人体复检：不合格")
        elif verdict is False:
            self.log.info("人体复检：通过")
        else:
            self.log.debug("人体复检没拿到结论（放行）")
        return verdict

    def _recent_style_titles(self) -> set[str]:
        used = self.store.data.get("style_used") or []
        return {str(x) for x in used[-6:]}

    def _remember_style(self, name: str) -> None:
        if not name:
            return
        used = list(self.store.data.get("style_used") or [])
        used.append(name)
        self.store.data["style_used"] = used[-20:]
        try:
            self.store.save()
        except Exception:
            pass

    def _pick_scene(self) -> dict[str, Any] | None:
        """挑场景；连续多次撞库就放宽限制。"""
        scene = self._prompt_lib.pick_scene(avoid_ids=self._recent_scene_ids())
        if scene is not None:
            self._remember_scene(scene)
        return scene

    async def _maybe_render_selfie(self, draft: dict[str, Any]) -> None:
        """按需生成自拍图并改写配文。当前生图未部署时安静跳过。

        分工：模型只想**画面内容**（场景/动作/情绪），
        外观锚点、LoRA、画风、光线、构图、质量词由 :class:`PromptLibrary` 统一拼装。
        """
        if not (self.profile.allow_selfie and self.config.selfie.enabled):
            return
        if not getattr(self._selfie, "available", False):
            draft["selfie_error"] = "自拍生图未配置（没有可用的生图端点）"
            self.log.info("跳过自拍：生图提供方不可用")
            return

        persona_name, persona_prompt = await self.persona()
        try:
            # 1) 画面：优先用 compose_post 阶段已经选好的场景；
            #    没有场景库或没选场景时，才让模型自己想。
            chosen_scene = draft.get("scene") or None
            if chosen_scene is not None:
                scene = self._prompt_lib.scene_tags(chosen_scene)
                self.log.info(
                    "自拍画面来自场景库：《%s》",
                    self._prompt_lib.scene_title(chosen_scene),
                )
            else:
                system, user = build_selfie_prompt_prompt(
                    self.profile, persona_name, persona_prompt,
                    latest_post=draft.get("content", ""),
                )
                scene = clean_text(
                    await self.bridge.generate(
                        self.profile,
                        system_prompt=system,
                        user_prompt=user,
                        purpose="想自拍画面",
                    ),
                    max_len=600,
                )
            if not scene:
                draft["selfie_error"] = "模型没有给出画面描述"
                return

            # 2) 用提示词库把「稳定部分」拼进来
            from .util import daypart_text

            persona_id = self.profile.persona_id or persona_name

            # 画师串：改画风最有效的手段（NovelAI 的 artist:xxx 权重串）
            artist_text = ""
            style_mode = str(getattr(self.config.selfie, "artist_style", "random") or "random").strip()
            if style_mode.lower() not in ("", "off", "none", "0", "false"):
                if style_mode.lower() in ("random", "auto"):
                    chosen_style = self._prompt_lib.pick_artist_style(
                        avoid_titles=self._recent_style_titles(),
                        persona_id=self.profile.persona_id,
                    )
                else:
                    chosen_style = self._prompt_lib.find_artist_style(style_mode)
                if chosen_style is not None:
                    artist_text = self._prompt_lib.scene_tags(chosen_style)
                    self.log.info(
                        "画师串：《%s》", self._prompt_lib.scene_title(chosen_style)
                    )
                elif style_mode.lower() not in ("random", "auto"):
                    self.log.warning("找不到画师串 %r，本次不注入", style_mode)

            # 换装 / 换发型：让角色别永远一套衣服（identity 段永远保留，
            # 所以发色瞳色不会变，认得出还是她）
            try:
                variation = float(getattr(self.config.selfie, "variation", 0.35) or 0.0)
            except (TypeError, ValueError):
                variation = 0.0
            outfit_override = ""
            hair_override = ""
            if variation > 0 and random.random() < variation:
                if random.random() < 0.65:
                    outfit_scene = self._prompt_lib.pick_outfit(
                        avoid_ids=self._recent_scene_ids()
                    )
                    if outfit_scene is not None:
                        outfit_override = self._prompt_lib.scene_tags(outfit_scene)
                        self.log.info(
                            "换装：《%s》", self._prompt_lib.scene_title(outfit_scene)
                        )
                if random.random() < 0.55:
                    hair_override = self._prompt_lib.pick_hairstyle()
                    self.log.info("换发型：%s", hair_override)

            use_lora = bool(getattr(self.config.selfie, "lora_enabled", True))
            use_trigger = bool(getattr(self.config.selfie, "use_trigger", True))
            full_prompt = self._prompt_lib.build(
                persona_id=persona_id,
                scene=scene,
                daypart=daypart_text(),
                lora_override=self.profile.selfie_lora,
                lora_weight_override=(
                    self.profile.selfie_lora_weight
                    if self.profile.selfie_lora_weight > 0 else None
                ),
                trigger_override=self.profile.selfie_lora_trigger,
                use_lora=use_lora,
                use_trigger=use_trigger,
                # 场景库条目自带镜头说明，别再叠随机构图词
                include_composition=(chosen_scene is None),
                artist_style=artist_text,
                outfit_override=outfit_override,
                hair_override=hair_override,
                extra_style=self.profile.selfie_extra_style,
            )
            negative = self._prompt_lib.resolved_negative(
                self.profile.selfie_negative, persona_id=self.profile.persona_id
            )
            lora_name, lora_weight, lib_trigger = self._prompt_lib.lora(persona_id)
            self.log.info(
                "自拍提示词：人设=%s 角色tag=%s LoRA=%s 场景 %d 字 → 完整 %d 字",
                persona_id,
                (self.profile.selfie_lora_trigger or lib_trigger) if use_trigger else "关",
                ((self.profile.selfie_lora or lora_name) if use_lora else "关") or "(无)",
                len(scene),
                len(full_prompt),
            )

            # 3) 生图（提供方内部有端点回退链）
            #
            # 参考图（立绘/三视图）：<数据目录>/ref/<人设名>/ 里放什么就用什么。
            # 这是"像不像"的关键 —— 纯文字提示词只能还原发色瞳色，
            # 给参考图后模型能对齐脸型、服装剪裁、配饰位置。
            references: list[str] = []
            if getattr(self.config.selfie, "use_reference", True):
                ref_dir = self.store.data_dir / "ref" / persona_id
                references = load_references(
                    ref_dir,
                    limit=int(getattr(self.config.selfie, "reference_max", 4) or 4),
                )
                if references:
                    self.log.info("使用参考图 %d 张（%s）", len(references), ref_dir.name)
                elif ref_dir.is_dir():
                    self.log.info("参考图目录存在但没有图片：%s", ref_dir)
            effective_prompt = full_prompt
            if references:
                effective_prompt = (
                    "参考图中是同一个角色。保持她的长相、脸型、发色、瞳色、"
                    "服装款式与配饰完全一致，只改变场景、动作和表情。\n" + full_prompt
                )

            # 特写场景自动换方图（纵版画布上画 close-up 会被切成多格）
            base_size = self.profile.selfie_size or ""
            use_size = self._prompt_lib.shot_size(scene, base_size)
            if use_size != base_size:
                self.log.info("特写场景，画布改为 %s", use_size)

            # 生成 + 人体结构复检（不合格重画）
            retries = 0
            if getattr(self.config.selfie, "anatomy_check", True):
                retries = int(getattr(self.config.selfie, "anatomy_retries", 1) or 0)
            attempt = 0
            while True:
                result = await self._selfie.generate(
                    effective_prompt,
                    negative=negative,
                    size=use_size,
                    references=references,
                )
                if not result.ok or not result.image_bytes:
                    break
                if attempt >= retries:
                    break
                verdict = await self._check_anatomy(result.image_bytes, result.mime, attempt, retries)
                if verdict is not True:
                    break
                attempt += 1
                self.log.warning("第 %d 张人体结构有问题，重画（%d/%d）", attempt, attempt, retries)

            if not result.ok or not result.image_bytes:
                draft["selfie_error"] = result.error or "生图失败"
                self.log.warning("自拍生图失败：%s", draft["selfie_error"])
                return

            # 4) 配文用「场景」而不是整段提示词，读起来才像人话
            system, user = build_selfie_caption_prompt(
                self.profile, persona_name, persona_prompt, scene
            )
            caption = clean_post(
                await self.bridge.generate(
                    self.profile,
                    system_prompt=system,
                    user_prompt=user,
                    purpose="写自拍配文",
                )
            )
            draft["image_prompt"] = full_prompt
            draft["image_scene"] = scene
            draft["image_bytes"] = result.image_bytes
            if is_usable_post(caption):
                draft["content"] = caption
            self.log.info(
                "自拍生成成功：%d 字节（端点 %s）", len(result.image_bytes), result.provider
            )
        except Exception as exc:
            draft["selfie_error"] = str(exc)
            self.log.warning("自拍流程异常（已忽略，按纯文字发布）：%s", exc)

    async def publish_post(
        self,
        *,
        trigger: str = "manual",
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """完整发一条说说。返回结果 dict，绝不抛出业务异常。"""
        async with self._lock:
            result: dict[str, Any] = {
                "ok": False,
                "dry_run": self._effective_dry_run(dry_run),
                "trigger": trigger,
                "content": "",
                "error": "",
            }
            try:
                self.store.load()
                await self.ensure_credentials()

                draft = await self.compose_post()
                await self._maybe_render_selfie(draft)
                result["content"] = draft["content"]
                result["used_material"] = draft["used_material"]
                result["channels"] = draft["channels"]
                result["selfie_error"] = draft.get("selfie_error", "")

                if result["dry_run"]:
                    result["ok"] = True
                    result["note"] = "演练模式：未真正发布"
                    self.log.info("[演练] 将发布：%s", draft["content"])
                    return result

                client = await self.acquire_client()
                photos: list[dict[str, Any]] = []
                if draft["image_bytes"]:
                    try:
                        photos.append(
                            await client.upload_image(draft["image_bytes"], filename="selfie.png")
                        )
                    except Exception as exc:
                        self.log.warning("自拍上传失败，改为纯文字发布：%s", exc)
                        result["selfie_error"] = f"上传失败：{exc}"

                payload = await client.publish(draft["content"], photos=photos)
                fid = str(payload.get("fid") or "")

                self.store.record_post(
                    PostRecord(
                        fid=fid,
                        uin=self.uin,
                        content=draft["content"],
                        created_at=now(),
                        material_sessions=draft["channels"],
                        used_material=bool(draft["used_material"]),
                        selfie_prompt=draft.get("image_prompt", ""),
                        selfie_used=bool(photos),
                    )
                )
                if draft["used_material"] and draft["material"] is not None:
                    self.store.mark_material_used(draft["material"].fingerprints)
                    self.messages.mark_used(self.platform_id, draft["material"].fingerprints)
                self.store.save()

                result["ok"] = True
                result["fid"] = fid
                self.log.info("发说说成功 fid=%s 素材=%s", fid or "?", result["used_material"])
            except Exception as exc:
                result["error"] = self._describe_error(exc)
                self.store.bump_error()
                self.store.save()
                self.log.warning("发说说失败：%s", result["error"])
            return result

    # ------------------------------------------------------------------ #
    # 互动：回复自己的评论 + 点赞/评论别人的说说
    # ------------------------------------------------------------------ #
    async def _reply_own_comments(self, client: QzoneClient, result: dict[str, Any]) -> None:
        if not self.profile.reply_enabled:
            return
        try:
            items = await client.msglist(num=10)
        except Exception as exc:
            self.log.warning("读取自己的说说失败：%s", exc)
            return

        for item in items:
            if not item.comments:
                continue
            for comment in item.comments:
                if not comment.comment_id:
                    continue
                if comment.uin and comment.uin == self.uin:
                    continue  # 自己说的话不用回
                if self.store.has_interacted("reply", item.hostuin or self.uin, item.fid, comment.comment_id):
                    continue
                if not self.store.try_consume_quota("reply", self.profile.reply_max_per_day):
                    self.log.info("今日回复配额已用完")
                    return
                if random.random() > 0.9:  # 不是每条都回，留点人味
                    self.store.bump_today("reply", amount=-1)
                    continue

                persona_name, persona_prompt = await self.persona()
                system, user = build_reply_prompt(
                    self.profile, persona_name, persona_prompt, item, comment
                )
                try:
                    text = clean_comment(
                        await self.bridge.generate(
                            self.profile,
                            system_prompt=system,
                            user_prompt=user,
                            purpose="回复评论",
                        ),
                        max_len=self.profile.comment_max_length,
                    )
                except Exception as exc:
                    self.log.warning("生成回复失败：%s", exc)
                    self.store.bump_today("reply", amount=-1)
                    continue
                if not text:
                    self.store.bump_today("reply", amount=-1)
                    continue

                if result["dry_run"]:
                    result["actions"].append(f"[演练] 回复 {comment.name}：{text}")
                    continue
                try:
                    await client.reply(
                        item.hostuin or self.uin,
                        item.fid,
                        comment.comment_id,
                        comment.uin,
                        text,
                        appid=item.appid,
                    )
                    self.store.record_interaction(
                        InteractionRecord(
                            kind="reply",
                            hostuin=item.hostuin or self.uin,
                            fid=item.fid,
                            comment_id=comment.comment_id,
                            content=text,
                            ts=now(),
                        )
                    )
                    result["actions"].append(f"回复 {comment.name}：{text}")
                    self.log.info("已回复 %s：%s", comment.name, text)
                except Exception as exc:
                    self.log.warning("回复失败：%s", exc)
                    self.store.bump_today("reply", amount=-1)

    async def _interact_with_others(self, client: QzoneClient, result: dict[str, Any]) -> None:
        if not (self.profile.like_enabled or self.profile.comment_enabled):
            return
        if self.profile.interact_own_posts_only:
            return

        try:
            if self.profile.interact_scope == "self":
                candidates = [i for i in await client.msglist(num=10) if i.comments]
            else:
                candidates = await client.recent_feeds()
        except Exception as exc:
            self.log.warning("读取动态失败：%s", exc)
            return

        # 只互动别人的说说，且跳过打卡/签到类
        targets = [
            item for item in candidates
            if item.fid
            and item.hostuin
            and item.hostuin != self.uin
            and not item.is_checkin
        ]
        if not targets:
            self.log.info("没有可互动的目标")
            return

        random.shuffle(targets)
        handled = 0
        for item in targets:
            if handled >= self.profile.interact_target_count:
                break

            already_liked = item.liked or self.store.has_interacted("like", item.hostuin, item.fid)
            already_commented = self.store.has_interacted("comment", item.hostuin, item.fid)

            need_like = (
                self.profile.like_enabled
                and not already_liked
                and random.random() <= self.profile.like_probability
            )
            need_comment = (
                self.profile.comment_enabled
                and not already_commented
                and random.random() <= self.profile.comment_probability
            )

            if not need_like and not need_comment:
                continue

            comment_text = ""
            should_like = need_like
            if need_comment:
                # 好友动态接口只能给出 HTML 兜底正文，且可能截断；
                # 这里尽量用详情接口换成干净正文（无权限时保持原样，不报错）。
                if item.content and len(item.content) > 200:
                    try:
                        detailed = await client.detail(item.hostuin, item.fid)
                        if detailed is not None and detailed.content.strip():
                            item = detailed
                            self.log.debug("已用详情接口刷新 fid=%s 的正文", item.fid)
                    except Exception as exc:
                        self.log.debug("详情接口不可用（保持用动态摘要）：%s", exc)

                persona_name, persona_prompt = await self.persona()
                system, user = build_comment_decision_prompt(
                    self.profile, persona_name, persona_prompt, item
                )
                decision: dict[str, Any] = {}
                try:
                    raw = await self.bridge.generate(
                        self.profile,
                        system_prompt=system,
                        user_prompt=user,
                        purpose="写评论",
                    )
                    payload = extract_json_object(raw)
                    if isinstance(payload, dict):
                        decision = payload
                    else:
                        self.log.warning("评论决策返回的不是可解析的 JSON，本轮跳过评论：%r", raw[:120])
                except Exception as exc:
                    self.log.warning("评论决策失败：%s", exc)

                if decision:
                    should_like = bool(decision.get("should_like", should_like)) and self.profile.like_enabled
                    if decision.get("should_comment") is False:
                        comment_text = ""
                    else:
                        comment_text = clean_comment(
                            str(decision.get("comment") or ""),
                            max_len=self.profile.comment_max_length,
                        )

            acted = False

            if should_like and not already_liked:
                if self.store.try_consume_quota("like", self.profile.like_max_per_day):
                    if result["dry_run"]:
                        result["actions"].append(f"[演练] 点赞 {item.nickname or item.hostuin}")
                        acted = True
                    else:
                        try:
                            await client.like(item.hostuin, item.fid, appid=item.appid)
                            self.store.record_interaction(
                                InteractionRecord(
                                    kind="like", hostuin=item.hostuin, fid=item.fid, ts=now()
                                )
                            )
                            result["actions"].append(f"点赞 {item.nickname or item.hostuin}")
                            acted = True
                        except Exception as exc:
                            self.log.warning("点赞失败：%s", exc)
                            self.store.bump_today("like", amount=-1)
                    await asyncio.sleep(1.5)

            if comment_text and not already_commented:
                if self.store.try_consume_quota("comment", self.profile.comment_max_per_day):
                    if result["dry_run"]:
                        result["actions"].append(
                            f"[演练] 评论 {item.nickname or item.hostuin}：{comment_text}"
                        )
                        acted = True
                    else:
                        try:
                            await client.comment(
                                item.hostuin, item.fid, comment_text, appid=item.appid
                            )
                            self.store.record_interaction(
                                InteractionRecord(
                                    kind="comment",
                                    hostuin=item.hostuin,
                                    fid=item.fid,
                                    content=comment_text,
                                    ts=now(),
                                )
                            )
                            result["actions"].append(
                                f"评论 {item.nickname or item.hostuin}：{comment_text}"
                            )
                            acted = True
                        except Exception as exc:
                            self.log.warning("评论失败：%s", exc)
                            self.store.bump_today("comment", amount=-1)
                    await asyncio.sleep(2.0)

            if acted:
                handled += 1
                self.store.save()
                if not result["dry_run"]:
                    await asyncio.sleep(3.0 + random.random() * 4.0)

    async def run_interactions(
        self,
        *,
        trigger: str = "manual",
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        """跑一轮互动（回复 + 点赞 + 评论）。"""
        async with self._lock:
            result: dict[str, Any] = {
                "ok": False,
                "dry_run": self._effective_dry_run(dry_run),
                "trigger": trigger,
                "actions": [],
                "error": "",
            }
            try:
                self.store.load()
                await self.ensure_credentials()
                client = await self.acquire_client()

                await self._reply_own_comments(client, result)
                await self._interact_with_others(client, result)

                self.store.save()
                result["ok"] = True
                if not result["actions"]:
                    result["note"] = "本轮没有需要做的互动"
            except Exception as exc:
                result["error"] = self._describe_error(exc)
                self.store.bump_error()
                self.store.save()
                self.log.warning("互动失败：%s", result["error"])
            return result

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def _describe_error(self, exc: Exception) -> str:
        if isinstance(exc, QzoneAuthError):
            self._creds = None
            return f"登录态问题：{exc}"
        if isinstance(exc, (QzoneError, BridgeError)):
            return str(exc)
        return f"{type(exc).__name__}: {exc}"

    async def status(self) -> dict[str, Any]:
        creds = self._creds
        snapshot = self.store.summary()
        client_info: dict[str, Any] = {}
        if self._client is not None:
            client_info = self._client.status_snapshot()
        return {
            "name": self.profile.name,
            "platform_id": self.platform_id,
            "enabled": self.profile.enabled,
            "persona": self.profile.resolved_persona_name(),
            "provider": self.profile.provider_id or self.config.default_provider_id or "(会话默认)",
            "post_times": list(self.profile.post_times),
            "interact_times": list(self.profile.interact_times),
            "sources": [c.key for c in self.channels()],
            "uin": self.uin,
            "credentials": creds.summary() if creds else "未获取",
            "client": client_info,
            "store": snapshot,
            "selfie": "开" if (self.profile.allow_selfie and self.config.selfie.enabled) else "关",
            "dry_run": self._effective_dry_run(),
            "buffer": self.messages.channel_counts(self.platform_id),
        }

    async def compose_preview(self) -> dict[str, Any]:
        """只生成一条说说，不发布（供 /qzswarm preview 使用）。"""
        async with self._lock:
            self.store.load()
            await self.ensure_credentials()
            return await self.compose_post()

    def set_last_error(self, text: str) -> None:
        self._last_error = text or ""

    def status_text(self) -> str:
        store = self.store.summary()
        today = store.get("today") or {}
        stats = store.get("stats") or {}
        creds = self._creds
        lines = [
            f"【{self.profile.name}】平台={self.platform_id} QQ={self.uin or '未绑定'}"
            f"{'（已停用）' if not self.profile.enabled else ''}",
            f"  人设：{self.profile.resolved_persona_name()}"
            f"　模型：{self.profile.provider_id or self.config.default_provider_id or '会话默认'}",
            f"  登录态：{creds.summary() if creds else '未获取'}",
            f"  发说说时间：{self.profile.post_times_text() or '关闭'}"
            f"（+{self.profile.post_offset_minutes}分内随机）",
            f"  互动时间：{self.profile.interact_times_text() or '关闭'}"
            f"（+{self.profile.interact_offset_minutes}分内随机）",
            f"  今日：说说 {today.get('post', 0)}/{self.profile.post_max_per_day}"
            f"　评论 {today.get('comment', 0)}/{self.profile.comment_max_per_day}"
            f"　赞 {today.get('like', 0)}/{self.profile.like_max_per_day}"
            f"　回复 {today.get('reply', 0)}/{self.profile.reply_max_per_day}",
            f"  累计：说说 {stats.get('posts', 0)}　评论 {stats.get('comments', 0)}"
            f"　赞 {stats.get('likes', 0)}　回复 {stats.get('replies', 0)}"
            f"　错误 {stats.get('errors', 0)}",
            f"  素材来源：{'、'.join(c.key for c in self.channels()) or '未配置'}"
            f"　缓冲：{sum(self.messages.channel_counts(self.platform_id).values())} 条",
            f"  自拍：{'开' if (self.profile.allow_selfie and self.config.selfie.enabled) else '关'}"
            f"　演练：{'是' if self._effective_dry_run() else '否'}",
        ]
        if self._last_error:
            lines.append(f"  最近错误：{self._last_error}")
        return "\n".join(lines)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
        try:
            await self._selfie.close()
        except Exception:
            pass
        try:
            self.store.save()
        except Exception:
            pass
