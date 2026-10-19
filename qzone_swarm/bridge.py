"""AstrBot 适配层：把框架能力收敛成插件内部使用的几个方法。

**这是本插件唯一与 astrbot 耦合的模块**（加上 main.py）。
所有"取某个机器人的 OneBot 客户端""调某个号的 LLM""取人设"都从这里走。

关键隔离原则：任何方法都必须显式接收 ``platform_id`` /
``profile``，绝不存在"当前机器人"这类全局隐式状态。
"""

from __future__ import annotations

import asyncio

import httpx
import logging
from typing import Any

from .config import GlobalConfig, bot_manual_cookie
from .creds import fetch_credentials
from .logging_compat import get_logger
from .models import BotProfile, Credentials
from .util import clean_text

log = get_logger("bridge")

LLM_TIMEOUT_SECONDS = 90.0
LLM_MAX_RETRIES = 2


class BridgeError(Exception):
    """适配层错误（找不到平台/模型/人设等）。"""


class AstrBotBridge:
    """封装 AstrBot 的平台/模型/人设/发信能力。"""

    def __init__(self, context: Any, config: GlobalConfig) -> None:
        self.context = context
        self.config = config
        self._persona_cache: dict[str, str] = {}

    # -- 平台 --------------------------------------------------------------- #
    def platform_ids(self) -> list[str]:
        try:
            return [p.meta().id for p in self.context.platform_manager.platform_insts]
        except Exception as exc:
            log.warning("枚举平台实例失败：%s", exc)
            return []

    def platform_label(self, platform_id: str) -> str:
        try:
            inst = self.context.get_platform_inst(platform_id)
            if inst is None:
                return f"{platform_id}(未加载)"
            meta = inst.meta()
            return f"{meta.id}({meta.name})"
        except Exception:
            return platform_id

    def get_onebot_client(self, platform_id: str) -> Any | None:
        """取得 **指定机器人** 的 OneBot 客户端；不同号返回不同客户端。"""
        try:
            inst = self.context.get_platform_inst(platform_id)
        except Exception as exc:
            log.warning("获取平台实例 %s 失败：%s", platform_id, exc)
            return None
        if inst is None:
            return None
        getter = getattr(inst, "get_client", None)
        if callable(getter):
            try:
                return getter()
            except Exception as exc:
                log.warning("平台 %s 的 get_client() 失败：%s", platform_id, exc)
        return getattr(inst, "bot", None)

    async def credentials_for(self, profile: BotProfile) -> Credentials:
        """取该机器人自己的 QQ空间凭据。"""
        client = self.get_onebot_client(profile.platform_id)
        if client is None:
            return Credentials(
                platform_id=profile.platform_id,
                source="missing-platform",
            )
        return await fetch_credentials(
            client,
            platform_id=profile.platform_id,
            manual_cookie=bot_manual_cookie(profile),
        )

    # -- 人设 --------------------------------------------------------------- #
    async def persona_prompt(self, persona_id: str) -> str:
        """按 persona_id 取人设 prompt；找不到返回空串。"""
        if not persona_id:
            return ""
        if persona_id in self._persona_cache:
            return self._persona_cache[persona_id]

        text = ""
        manager = getattr(self.context, "persona_manager", None)
        if manager is not None:
            # 优先 v3 视角
            try:
                persona = manager.get_persona_v3_by_id(persona_id)
                if persona:
                    text = str(persona.get("prompt") or "")
            except Exception as exc:
                log.debug("get_persona_v3_by_id(%s) 失败：%s", persona_id, exc)
            if not text:
                try:
                    persona = await manager.get_persona(persona_id)
                    text = str(getattr(persona, "system_prompt", "") or "")
                except Exception as exc:
                    log.debug("get_persona(%s) 失败：%s", persona_id, exc)

        if not text:
            log.warning("未找到人设「%s」，该机器人将使用机器人名称作为角色名", persona_id)
        self._persona_cache[persona_id] = text
        return text

    def invalidate_persona_cache(self) -> None:
        self._persona_cache.clear()

    async def list_personas(self) -> list[str]:
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return []
        names: list[str] = []
        try:
            for item in getattr(manager, "personas_v3", []) or []:
                name = str(item.get("name") or "")
                if name:
                    names.append(name)
        except Exception as exc:
            log.debug("枚举 personas_v3 失败：%s", exc)
        if not names:
            try:
                for persona in await manager.get_all_personas():
                    names.append(str(persona.persona_id))
            except Exception as exc:
                log.debug("枚举 personas 失败：%s", exc)
        return names

    # -- 模型 --------------------------------------------------------------- #
    def _resolve_provider(self, provider_ref: str) -> tuple[Any, str]:
        """按 id 取提供商，支持 "provider_id" 与 "provider_id/model" 两种写法。

        Returns:
            (provider 或 None, 需要覆盖的 model 名或 "")
        """
        provider_ref = (provider_ref or "").strip()
        model = ""
        if "/" in provider_ref:
            head, _, tail = provider_ref.partition("/")
            if head.strip() and tail.strip():
                provider_ref, model = head.strip(), tail.strip()

        if not provider_ref:
            return None, model

        try:
            provider = self.context.get_provider_by_id(provider_ref)
        except Exception as exc:
            log.warning("get_provider_by_id(%s) 失败：%s", provider_ref, exc)
            return None, model
        if provider is None:
            log.warning("未找到模型提供商 %s，将回退到会话默认提供商", provider_ref)
        return provider, model

    async def _using_provider(self, umo: str = ""):
        getter = getattr(self.context, "get_using_provider_async", None)
        if callable(getter):
            try:
                return await (getter(umo) if umo else getter())
            except TypeError:
                return await getter()
        getter = getattr(self.context, "get_using_provider", None)
        if callable(getter):
            result = getter(umo) if umo else getter()
            if asyncio.iscoroutine(result):
                result = await result
            return result
        return None

    def _fallback_provider_ids(self, umo: str = "") -> list[str]:
        """读取 AstrBot 为该会话配置的**兜底提供商链**。

        AstrBot 自己的 agent 在主提供商失败时会依次尝试
        ``agent_runner.config.model.fallback_provider_ids``。
        本插件也必须照做 —— 否则主提供商（例如本地中转）一挂，
        机器人聊天还能靠兜底继续，发说说却会全军覆没（线上真实踩过）。
        """
        try:
            cfg = self.context.get_config(umo) if umo else self.context.get_config()
        except Exception as exc:
            log.debug("读取会话配置失败（无法获得兜底链）：%s", exc)
            return []
        if not hasattr(cfg, "get"):
            return []
        runner = cfg.get("agent_runner") or {}
        if not isinstance(runner, dict):
            return []
        model_cfg = ((runner.get("config") or {}).get("model")) or {}
        if not isinstance(model_cfg, dict):
            return []
        ids = model_cfg.get("fallback_provider_ids") or []
        if isinstance(ids, str):
            ids = [ids]
        return [str(i).strip() for i in ids if str(i).strip()]

    async def generate(
        self,
        profile: BotProfile,
        *,
        system_prompt: str,
        user_prompt: str,
        umo: str = "",
        purpose: str = "文本生成",
    ) -> str:
        """用该机器人配置的模型生成文本。上下文是**一次性**的，不做任何跨次累积。

        提供商按以下顺序尝试，前一个失败才用下一个（与 AstrBot 自身行为一致）：
            1. 该号单独指定的 ``provider_id``；
            2. 插件全局 ``default_provider_id``；
            3. 会话默认提供商；
            4. AstrBot 配置的 ``fallback_provider_ids`` 兜底链。
        """
        candidates: list[tuple[Any, str]] = []
        seen: set[int] = set()

        def _add(prov: Any, model: str) -> None:
            if prov is None or id(prov) in seen:
                return
            seen.add(id(prov))
            candidates.append((prov, model))

        for ref in (profile.provider_id, self.config.default_provider_id):
            ref = (ref or "").strip()
            if ref:
                prov, model = self._resolve_provider(ref)
                _add(prov, model)

        try:
            _add(await self._using_provider(umo), "")
        except Exception as exc:
            log.debug("获取会话默认提供商失败：%s", exc)

        for ref in self._fallback_provider_ids(umo):
            prov, model = self._resolve_provider(ref)
            _add(prov, model)

        if not candidates:
            raise BridgeError(f"没有可用的模型提供商（{purpose}）")

        last_error: Exception | None = None
        for index, (provider, model) in enumerate(candidates, start=1):
            for attempt in range(1, LLM_MAX_RETRIES + 1):
                try:
                    kwargs: dict[str, Any] = {}
                    if model:
                        kwargs["model"] = model
                    response = await asyncio.wait_for(
                        provider.text_chat(
                            prompt=user_prompt,
                            system_prompt=system_prompt,
                            contexts=[],
                            **kwargs,
                        ),
                        timeout=LLM_TIMEOUT_SECONDS,
                    )
                    text = clean_text(getattr(response, "completion_text", "") or "")
                    if text:
                        if index > 1:
                            log.warning(
                                "[%s] %s 由第 %d 个候选提供商成功（前面 %d 个失败）",
                                profile.platform_id, purpose, index, index - 1,
                            )
                        return text
                    last_error = BridgeError("模型返回了空内容")
                except asyncio.TimeoutError:
                    last_error = BridgeError(f"模型调用超时（{LLM_TIMEOUT_SECONDS:.0f}s）")
                except Exception as exc:
                    last_error = exc
                if attempt < LLM_MAX_RETRIES:
                    await asyncio.sleep(1.2 * attempt)

            name = getattr(provider, "provider_config", {}).get("id") if hasattr(
                provider, "provider_config"
            ) else None
            log.warning(
                "[%s] %s：候选 %d/%d%s 失败，尝试下一个：%s",
                profile.platform_id, purpose, index, len(candidates),
                f"（{name}）" if name else "", last_error,
            )

        raise BridgeError(
            f"{purpose}失败（已尝试 {len(candidates)} 个提供商）：{last_error}"
        )

    async def check_image(
        self,
        profile: BotProfile,
        image_bytes: bytes,
        *,
        question: str,
        mime: str = "image/jpeg",
    ) -> bool | None:
        """让视觉模型看一眼生成的图，判断是否有明显问题。

        Returns:
            True  —— 有问题，应该重画
            False —— 没问题
            None  —— 检查本身失败（没配视觉能力/网络错），调用方应当放行
        """
        if not image_bytes:
            return None
        import base64 as _b64

        data_uri = f"data:{mime};base64,{_b64.b64encode(image_bytes).decode()}"
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            "max_tokens": 12,
            "temperature": 0,
        }
        for prov, model in self._vision_candidates(profile):
            api_base = str((getattr(prov, "provider_config", {}) or {}).get("api_base") or "").rstrip("/")
            if not api_base:
                continue
            key = str((getattr(prov, "provider_config", {}) or {}).get("key") or "")
            body = dict(payload)
            body["model"] = model or str((getattr(prov, "provider_config", {}) or {}).get("model") or "")
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            try:
                client = httpx.AsyncClient(timeout=httpx.Timeout(60.0), trust_env=False)
                try:
                    resp = await client.post(f"{api_base}/chat/completions", json=body, headers=headers)
                finally:
                    await client.aclose()
                if resp.status_code >= 400:
                    log.debug("视觉复检 %s HTTP %s", model, resp.status_code)
                    continue
                text = (
                    (resp.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
                ).strip().upper()
                if not text:
                    continue
                log.info("视觉复检结果：%s", text[:40])
                if "YES" in text:
                    return True
                if "NO" in text:
                    return False
            except Exception as exc:
                log.debug("视觉复检失败（%s）：%s", model, exc)
        return None

    def _vision_candidates(self, profile: BotProfile) -> list[tuple[Any, str]]:
        """视觉复检用的候选提供商（挑支持图像的，优先 Gemini 系）。"""
        out: list[tuple[Any, str]] = []
        refs: list[str] = []
        for ref in (profile.provider_id, self.config.default_provider_id):
            ref = (ref or "").strip()
            if ref:
                refs.append(ref)
        refs.extend(self._fallback_provider_ids())
        for ref in refs:
            prov, model = self._resolve_provider(ref)
            if prov is not None:
                out.append((prov, model))
        return out

    # -- 通知 --------------------------------------------------------------- #
    async def notify(self, umo: str, text: str) -> bool:
        """把运行结果/告警发到指定会话（通常是管理员私聊）。"""
        if not umo or not text:
            return False
        try:
            from astrbot.api.message_components import Plain
            from astrbot.core.message.message_event_result import MessageChain

            await self.context.send_message(umo, MessageChain([Plain(text)]))
            return True
        except Exception as exc:
            log.warning("发送通知到 %s 失败：%s", umo, exc)
            return False

    def resolve_notify_target(self, profile: BotProfile) -> str:
        return (profile.notify_umo or self.config.notify_admin_umo or "").strip()


def log_startup_summary(config: GlobalConfig) -> None:
    for bot in config.bots:
        log.info(
            "机器人 %s -> 平台 %s | 人设=%s | 发说说=%s | 互动=%s",
            bot.name,
            bot.platform_id,
            bot.persona_id or "(未设置)",
            bot.post_times_text() or "关",
            bot.interact_times_text() or "关",
        )
