"""绘画模型（自拍）接口：**支持多端点回退**。

当前状态：可对接真实生图模型，但需要填 `api_base` / `model` / `api_key`。
未配置时安静降级为纯文字发布（不影响发说说主流程）。

支持的对接形态：
    * ``openai_images`` —— OpenAI /v1/images/generations 兼容接口
      （OpenAI、本地 WebUI/SD API 网关、**豆包 Seedream**（Ark 就是这套）、
      以及多数自建中转都属这一类）
    * ``generic``       —— 自定义 JSON 接口，用 ``request_template`` / ``response_path`` 指定
      （适合 Gemini 原生 ``:generateContent`` 这类非 OpenAI 形状的接口）

**回退链**：主端点失败（网络/额度/地区限制/HTTP 错误）会自动按顺序尝试
``fallbacks`` 里的端点，任何一个成功即用。这与对话模型那边的兜底逻辑一致 ——
线上真实踩过的坑：本地中转一挂，如果只有单点，自拍就整个发不出去。

**默认出图比例是 3:4 竖图**（864×1152），适合手机自拍与 QQ 空间展示。

本模块不导入 astrbot。
"""

from __future__ import annotations

import base64
import io
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging_compat import get_logger

log = get_logger("selfie")

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

# 默认出图尺寸：3:4 竖图（正常手机自拍比例）
DEFAULT_SIZE = "864x1152"
DEFAULT_ASPECT_RATIO = "3:4"
# NovelAI 要求宽高必须是 64 的倍数，864 会被判非法（直接 500）。
# 832x1088 = 64 倍数、比例 0.765（≈3:4）、约 90 万像素，
# 稳稳落在 Opus 免费带（≤1,048,576 像素、≤28 步、单张）以内。
DEFAULT_NAI_SIZE = "832x1088"
NAI_FREE_PIXELS = 1048576


def nai_round(value: int) -> int:
    """把尺寸对齐到 64 的倍数（NovelAI 的硬要求）。"""
    return max(64, int(round(value / 64.0)) * 64)


def nai_safe_size(size: str, *, limit: int = NAI_FREE_PIXELS) -> tuple[int, int]:
    """把 "宽x高" 规整成 NovelAI 能接受、且仍在免费像素带内的尺寸。"""
    try:
        width_s, _, height_s = str(size or "").lower().partition("x")
        width, height = int(width_s), int(height_s)
    except Exception:
        width, height = 832, 1088
    width, height = nai_round(width), nai_round(height)
    # 超免费带就等比缩到带内（再对齐一次）
    if width * height > limit:
        scale = (limit / float(width * height)) ** 0.5
        width, height = nai_round(int(width * scale)), nai_round(int(height * scale))
        while width * height > limit:
            width, height = nai_round(width - 64), nai_round(height - 64)
    return width, height


@dataclass
class ImageEndpoint:
    """一个生图端点（主端点或回退端点）。"""

    label: str = ""
    mode: str = "openai_images"  # openai_images | generic | novelai | disabled
    api_base: str = ""
    api_key: str = ""
    model: str = ""
    size: str = DEFAULT_SIZE
    aspect_ratio: str = DEFAULT_ASPECT_RATIO
    timeout: float = 180.0
    # 该端点专用的代理（NovelAI 在国内必须走代理；留空则直连）
    proxy: str = ""
    extra_params: dict[str, Any] = field(default_factory=dict)
    request_template: str = ""
    response_path: str = "data.0.b64_json"

    @property
    def configured(self) -> bool:
        return bool(self.api_base.strip()) and self.mode != "disabled"

    def describe(self) -> str:
        name = self.label or self.model or self.api_base or "?"
        return f"{name}({self.mode})"


@dataclass
class SelfieConfig:
    """整体生图配置。"""

    enabled: bool = False
    primary: ImageEndpoint = field(default_factory=ImageEndpoint)
    fallbacks: list[ImageEndpoint] = field(default_factory=list)
    prompt_library: str = ""
    negative_prompt: str = ""
    # 参考图：把立绘/三视图放进 <数据目录>/ref/<人设名>/ 即可自动使用
    use_reference: bool = True
    reference_max: int = 4
    # 场景优先：先从场景库挑画面，再围绕它写说说（图文天然一致）
    scene_first: bool = True
    # 画师串（改画风）：off=不注入 / random=每次随机一组 / 填标题=固定用那一组
    artist_style: str = "random"
    # 换装/换发型概率（0 = 永远用设定里的原装，1 = 每张都换）
    variation: float = 0.85
    # 「不许连续重复」的窗口：最近几张用过的不再抽（构图/画师串/服装/发型都适用）
    no_repeat_window: int = 2
    # 把模型给的中文画面转写成 NAI 英文标签串（不转写会把中文直接喂给 NAI）
    translate_scene: bool = True
    # 给「想画面」这一步加上画面张力约束（源自 NAI_Prompt_Constraint_v2.md）
    use_prompt_constraint: bool = True
    # 出图后让视觉模型复检人体结构，不合格就重画。
    # ⚠️ 需要有**支持图像输入的**模型；本机现有 key（方舟套餐 / 中转）都不支持，
    #    所以默认关闭。没有视觉能力时打开只会白等一轮。
    anatomy_check: bool = False
    anatomy_retries: int = 1
    # 是否注入 LoRA。用云端绘画 API（豆包/Gemini 等闭源模型）时必须关掉 ——
    # 它们不认识 <lora:...> 语法，会把整串当文字画成怪物。
    lora_enabled: bool = True
    # 是否注入角色名（Danbooru 角色 tag）。与 LoRA 独立：
    # NovelAI 自带角色知识库，写 "denia (wuthering waves)" 就能直接出对角色；
    # 但面对闭源云端模型时建议关掉（写角色名更容易撞版权审核）。
    use_trigger: bool = True
    # 该号没在提示词库里登记时使用的默认 LoRA（一般留空）
    lora: str = ""
    lora_weight: float = 0.0
    lora_trigger: str = ""

    def endpoints(self) -> list[ImageEndpoint]:
        """返回按优先级排列的可用端点。"""
        chain: list[ImageEndpoint] = []
        if self.primary.configured:
            chain.append(self.primary)
        for item in self.fallbacks:
            if item.configured:
                chain.append(item)
        return chain


@dataclass
class SelfieResult:
    ok: bool = False
    image_bytes: bytes = b""
    prompt: str = ""
    provider: str = ""
    error: str = ""
    mime: str = "image/png"


def _dig(payload: Any, path: str) -> Any:
    """按 "a.b.0.c" 取值。"""
    if not path:
        return None
    current = payload
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, list):
            if part.lstrip("-").isdigit():
                index = int(part)
                current = current[index] if -len(current) <= index < len(current) else None
            else:
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _decode_image(value: Any) -> tuple[bytes, str]:
    """把模型返回的 base64 / data URI 变成图片字节（http URL 交给调用方下载）。"""
    if not value:
        return b"", "image/png"
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("http://", "https://")):
            return b"", "image/png"
        if text.startswith("data:image"):
            header, _, data = text.partition(",")
            mime = "image/png"
            if "image/jpeg" in header or "image/jpg" in header:
                mime = "image/jpeg"
            elif "image/webp" in header:
                mime = "image/webp"
            try:
                return base64.b64decode(data), mime
            except Exception:
                return b"", "image/png"
        try:
            return base64.b64decode(text), "image/png"
        except Exception:
            return b"", "image/png"
    return b"", "image/png"


class ImageProvider:
    """生图提供方接口。想接入新模型，继承它并实现 generate()。"""

    name = "base"

    def __init__(self, config: SelfieConfig) -> None:
        self.config = config

    @property
    def available(self) -> bool:
        return bool(self.config.endpoints())

    async def generate(
        self,
        prompt: str,
        *,
        negative: str = "",
        size: str = "",
        references: list[str] | None = None,
    ) -> SelfieResult:
        return SelfieResult(ok=False, prompt=prompt, provider=self.name, error="未实现")

    async def close(self) -> None:
        return None


class DisabledImageProvider(ImageProvider):
    """未部署生图模型时的占位实现：明确地什么都不做。"""

    name = "disabled"

    @property
    def available(self) -> bool:
        return False

    async def generate(
        self,
        prompt: str,
        *,
        negative: str = "",
        size: str = "",
        references: list[str] | None = None,
    ) -> SelfieResult:
        return SelfieResult(
            ok=False, prompt=prompt, provider=self.name,
            error="自拍生图未启用（尚未部署绘画模型）",
        )


class _SingleEndpoint:
    """单个端点的请求执行器。"""

    def __init__(self, endpoint: ImageEndpoint) -> None:
        self.endpoint = endpoint
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            if httpx is None:  # pragma: no cover
                raise RuntimeError("缺少 httpx 依赖")
            kwargs: dict[str, Any] = {"timeout": httpx.Timeout(self.endpoint.timeout)}
            if self.endpoint.proxy.strip():
                # 该端点必须走代理（NovelAI 等境外服务）
                kwargs["proxy"] = self.endpoint.proxy.strip()
            else:
                kwargs["trust_env"] = False
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    def _build_novelai(
        self,
        prompt: str,
        negative: str,
        size: str,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """构造 NovelAI V4/V5 的 /ai/generate-image 请求体。

        踩过的坑（都实测过）：
          * 提示词必须写进**三个地方**：顶层 ``input``、``parameters.prompt``、
            ``v4_prompt.caption.base_caption``；
          * ``v4_prompt`` 和 ``v4_negative_prompt`` **缺任何一个都直接 500**；
          * 宽高必须是 **64 的倍数**，864 会被判非法 → 500；
          * 返回是 **ZIP**（内含 image_0.png），不是 JSON；
          * 国内必须走代理，直连超时。
        """
        ep = self.endpoint
        base = ep.api_base.rstrip("/")
        url = base if base.endswith("/generate-image") else f"{base}/ai/generate-image"
        headers = {"Content-Type": "application/json"}
        if ep.api_key:
            headers["Authorization"] = f"Bearer {ep.api_key}"

        width, height = nai_safe_size(size or ep.size or DEFAULT_NAI_SIZE)
        params: dict[str, Any] = {
            "params_version": 4,
            "width": width,
            "height": height,
            "scale": 5.0,
            "sampler": "k_euler_ancestral",
            "steps": 28,          # 28 步以内 + 单张 + 像素不超限 = 0 Anlas
            "seed": 0,
            "n_samples": 1,
            "ucPreset": 3,
            "qualityToggle": False,
            "sm": False,
            "sm_dyn": False,
            "dynamic_thresholding": False,
            "controlnet_strength": 1,
            "legacy": False,
            "add_original_image": False,
            "cfg_rescale": 0,
            "noise_schedule": "karras",
            "legacy_v3_extend": False,
            "uncond_scale": 1,
            "negative_prompt": negative or "",
            "prompt": prompt,
            "reference_image_multiple": [],
            "reference_information_extracted_multiple": [],
            "reference_strength_multiple": [],
            "extra_noise_seed": 0,
            "v4_prompt": {
                "use_coords": False,
                "use_order": False,
                "caption": {"base_caption": prompt, "char_captions": []},
            },
            "v4_negative_prompt": {
                "use_coords": False,
                "use_order": False,
                "caption": {"base_caption": negative or "", "char_captions": []},
            },
        }
        params.update(ep.extra_params or {})
        body = {
            "input": prompt,
            "model": ep.model or "nai-diffusion-5-full",
            "action": "generate",
            "parameters": params,
        }
        return url, body, headers

    def _build(
        self,
        prompt: str,
        negative: str,
        size: str,
        references: list[str] | None = None,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        ep = self.endpoint
        if ep.mode == "novelai":
            # NovelAI 不吃参考图（要 Vibe Transfer，另说），先按纯提示词走
            return self._build_novelai(prompt, negative, size)

        base = ep.api_base.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if ep.api_key:
            headers["Authorization"] = f"Bearer {ep.api_key}"

        use_size = (size or ep.size or DEFAULT_SIZE).strip()
        refs = [r for r in (references or []) if r]

        if ep.mode == "generic" and ep.request_template:
            template = ep.request_template
            # 用 JSON 转义后的形态替换，避免提示词里的引号破坏 JSON
            template = template.replace("{prompt}", json.dumps(prompt, ensure_ascii=False)[1:-1])
            template = template.replace(
                "{negative_prompt}", json.dumps(negative, ensure_ascii=False)[1:-1]
            )
            template = template.replace("{size}", use_size)
            template = template.replace("{model}", ep.model)
            template = template.replace(
                "{reference_images}",
                json.dumps(refs, ensure_ascii=False)[1:-1] if refs else "",
            )
            try:
                body = json.loads(template)
            except Exception as exc:
                raise RuntimeError(f"request_template 不是合法 JSON：{exc}") from exc
            url = base
        else:
            url = base if base.endswith("/images/generations") else f"{base}/images/generations"
            body = {
                "model": ep.model or "image-model",
                "prompt": prompt,
                "n": 1,
                "size": use_size,
                "response_format": "b64_json",
            }
            if negative:
                body["negative_prompt"] = negative
            body.update(ep.extra_params or {})
            # 参考图：豆包 Seedream 用 `image` 字段（字符串数组，元素是 url 或 base64 data URI）
            if refs:
                body["image"] = refs
        return url, body, headers

    async def run(
        self,
        prompt: str,
        *,
        negative: str = "",
        size: str = "",
        references: list[str] | None = None,
    ) -> SelfieResult:
        ep = self.endpoint
        try:
            url, body, headers = self._build(prompt, negative, size, references)
            client = self._ensure_client()
            response = await client.post(url, json=body, headers=headers)
            if response.status_code >= 400:
                hint = ""
                if response.status_code == 500 and ep.mode == "novelai":
                    hint = (
                        "（NovelAI 的 500 通常是请求体问题：v4_prompt 缺失，"
                        "或宽高不是 64 的倍数）"
                    )
                return SelfieResult(
                    ok=False, prompt=prompt, provider=ep.describe(),
                    error=f"HTTP {response.status_code}: {response.text[:200]}{hint}",
                )
            # NovelAI 返回的是 ZIP（内含 image_0.png），不是 JSON
            if response.content[:2] == b"PK":
                import io as _io
                import zipfile as _zip

                try:
                    with _zip.ZipFile(_io.BytesIO(response.content)) as archive:
                        names = [n for n in archive.namelist() if not n.endswith("/")]
                        if not names:
                            return SelfieResult(
                                ok=False, prompt=prompt, provider=ep.describe(),
                                error="NovelAI 返回的 ZIP 是空的",
                            )
                        payload = archive.read(names[0])
                except Exception as exc:
                    return SelfieResult(
                        ok=False, prompt=prompt, provider=ep.describe(),
                        error=f"解包 NovelAI 的 ZIP 失败：{exc}",
                    )
                if not payload:
                    return SelfieResult(ok=False, prompt=prompt, provider=ep.describe(),
                                        error="解包后图片为空")
                mime = "image/png" if payload[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
                return SelfieResult(ok=True, image_bytes=payload, prompt=prompt,
                                    provider=ep.describe(), mime=mime)
            payload = response.json()
        except Exception as exc:
            return SelfieResult(ok=False, prompt=prompt, provider=ep.describe(),
                                error=f"请求失败：{type(exc).__name__}: {exc}")

        value = _dig(payload, ep.response_path)
        if not value:
            for path in ("data.0.b64_json", "data.0.url", "images.0", "image",
                         "data.0.image", "result.image"):
                value = _dig(payload, path)
                if value:
                    break
        if not value:
            return SelfieResult(ok=False, prompt=prompt, provider=ep.describe(),
                                error=f"响应里取不到图片（response_path={ep.response_path}）")

        data, mime = _decode_image(value)
        if not data and isinstance(value, str) and value.startswith("http"):
            try:
                client = self._ensure_client()
                download = await client.get(value)
                download.raise_for_status()
                data = download.content
                mime = download.headers.get("content-type", "image/png").split(";")[0]
            except Exception as exc:
                return SelfieResult(ok=False, prompt=prompt, provider=ep.describe(),
                                    error=f"下载生图结果失败：{exc}")
        if not data:
            return SelfieResult(ok=False, prompt=prompt, provider=ep.describe(), error="生图结果为空")
        return SelfieResult(ok=True, image_bytes=data, prompt=prompt, provider=ep.describe(), mime=mime)


class ChainImageProvider(ImageProvider):
    """按优先级依次尝试多个端点，第一个成功的即用。"""

    name = "chain"

    def __init__(self, config: SelfieConfig) -> None:
        super().__init__(config)
        self._singles = [_SingleEndpoint(ep) for ep in config.endpoints()]

    @property
    def available(self) -> bool:
        return bool(self._singles)

    async def generate(
        self,
        prompt: str,
        *,
        negative: str = "",
        size: str = "",
        references: list[str] | None = None,
    ) -> SelfieResult:
        if not self._singles:
            return SelfieResult(
                ok=False, prompt=prompt, provider=self.name,
                error="自拍生图未配置（没有可用的生图端点）",
            )
        errors: list[str] = []
        for index, single in enumerate(self._singles, start=1):
            result = await single.run(prompt, negative=negative, size=size, references=references)
            if result.ok:
                if index > 1:
                    log.warning(
                        "生图由第 %d 个端点成功（前面 %d 个失败）：%s",
                        index, index - 1, single.endpoint.describe(),
                    )
                return result
            errors.append(f"{single.endpoint.describe()}: {result.error}")
            log.warning(
                "生图端点 %d/%d 失败（%s）：%s",
                index, len(self._singles), single.endpoint.describe(), result.error,
            )
        return SelfieResult(
            ok=False, prompt=prompt, provider=self.name,
            error="所有生图端点都失败：" + " | ".join(errors),
        )

    async def close(self) -> None:
        for single in self._singles:
            await single.close()


def build_provider(config: SelfieConfig | None) -> ImageProvider:
    """根据配置构造生图提供方；未启用时返回明确的占位实现。"""
    if config is None or not config.enabled:
        return DisabledImageProvider(config or SelfieConfig())
    if not config.endpoints():
        return DisabledImageProvider(config)
    return ChainImageProvider(config)


# --------------------------------------------------------------------------- #
# 参考图（立绘 / 三视图）
# --------------------------------------------------------------------------- #
REFERENCE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
# 送进模型的参考图最长边（像素）。太大只会拖慢上传并可能触发接口体积限制，
# 1024 已经足够让模型看清发色、瞳色、服装细节。
REFERENCE_MAX_SIDE = 1024
REFERENCE_JPEG_QUALITY = 88


def _downscale_to_data_uri(path: Any, max_side: int = REFERENCE_MAX_SIDE) -> str:
    """把本地图片压到 data URI（base64）。

    优先用 PIL 缩放重编码；没装 PIL 时退回原图直接 base64。
    """
    path = Path(path)
    raw = path.read_bytes()
    suffix = path.suffix.lower()

    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            width, height = im.size
            longest = max(width, height)
            if longest > max_side:
                scale = max_side / float(longest)
                im = im.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    Image.LANCZOS,
                )
            buffer = io.BytesIO()
            im.save(buffer, format="JPEG", quality=REFERENCE_JPEG_QUALITY, optimize=True)
            payload = buffer.getvalue()
        return "data:image/jpeg;base64," + base64.b64encode(payload).decode()
    except Exception as exc:
        log.warning("压缩参考图 %s 失败（改用原图）：%s", path.name, exc)
        if suffix == ".png":
            mime = "image/png"
        elif suffix == ".webp":
            mime = "image/webp"
        else:
            mime = "image/jpeg"
        return f"data:{mime};base64," + base64.b64encode(raw).decode()


def load_references(
    directory: Any,
    *,
    limit: int = 4,
    max_side: int = REFERENCE_MAX_SIDE,
) -> list[str]:
    """扫描某个目录，把里面所有图片转成可传给模型的 data URI 列表。

    约定：**每个角色一个文件夹**，把立绘、三视图直接丢进去即可，无需配置。
    文件名按字典序取前 ``limit`` 张（建议命名成 01_立绘.png / 02_三视图.png …）。
    """
    if not directory:
        return []
    base = Path(directory)
    if not base.is_dir():
        return []
    files = sorted(
        (p for p in base.iterdir() if p.is_file() and p.suffix.lower() in REFERENCE_EXTENSIONS),
        key=lambda p: p.name,
    )
    if not files:
        return []
    if len(files) > limit:
        log.info(
            "参考图目录 %s 有 %d 张，只取前 %d 张（其余忽略）",
            base.name, len(files), limit,
        )
        files = files[:limit]
    refs: list[str] = []
    for path in files:
        try:
            refs.append(_downscale_to_data_uri(path, max_side))
        except Exception as exc:
            log.warning("读取参考图 %s 失败：%s", path.name, exc)
    if refs:
        log.info("已加载参考图 %d 张（目录 %s）", len(refs), base.name)
    return refs
