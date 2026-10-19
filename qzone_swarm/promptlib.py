"""提示词库：把「稳定风格 + 人设外观锚点 + LoRA」和「模型现想的画面」拼成最终提示词。

设计意图
--------
让模型只负责**想画面**（场景 / 动作 / 情绪 / 和说说呼应的小细节），
而画风、质量词、负面词、角色外观这些"要稳定"的部分由库提供。
这样：
    * 同一个角色的自拍前后一致（不会这次粉发下次黑发）；
    * 有 LoRA 的角色必定带上触发词；
    * 没 LoRA 的角色靠一段固定外观词兜住；
    * 换模型 / 换 API 时不用重调提示词。

库文件是普通 JSON，放在插件目录 `qzone_swarm/prompts_default.json`，
也可以在同目录放 `prompts_local.json` 覆盖（不会被升级覆盖）。
本模块不导入 astrbot。
"""

from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path
from typing import Any

from .logging_compat import get_logger

log = get_logger("promptlib")

_HERE = Path(__file__).resolve().parent
DEFAULT_PATH = _HERE / "prompts_default.json"
LOCAL_PATH = _HERE / "prompts_local.json"
# 场景库（从「法典图鉴」提炼，由 tools/build_scene_library.py 生成）
SCENES_PATH = _HERE / "scenes.json"

# 场景选题时的题材/性别过滤：库里含大量 1boy、兽人、机甲、怪物条目，
# 与矩阵里这四个女性角色放一起会画出完全不对的东西。
SCENE_BLOCK_TAGS = (
    "1boy", "male focus", "2boys", "multiple boys", "futanari",
    "furry", "anthro", "mecha", "robot", "monster", "kaiju",
    "orc", "goblin", "tentacle",
    # 库里有、但不适合画在机器人主页上的：不雅手势 / 低俗动作
    "middle finger", "insult", "gesture", "playboy bunny",
)
SCENE_PREFER_TAG = "1girl"

# 可以拿来「换衣服」的法典（衣柜、更衣人偶都是服装向的）
WARDROBE_COLLECTIONS = ("叫我千藤就好了の衣柜", "更衣人偶")

# 可换的发型。注意**只换造型、不动发色**（发色在 identity 里），
# 而且都保持长发，避免和 identity 里的 "very long hair" 打架。
HAIRSTYLES = (
    "twintails, hair ribbon",
    "high ponytail, hair tie",
    "single braid over shoulder, side braid",
    "loose long hair, hair down",
    "messy bun, hair clip",
    "half updo, hair ornament",
    "side ponytail, hair bow",
)
# 特写类镜头：这类场景给全服装描述会反噬 —— 模型非要把整套衣服画出来，
# 实测「close-up on face」会被 250 字服装词压成蹲姿全身。
TIGHT_SHOT_MARKERS = ("close-up", "closeup", "close up", "headshot", "face focus")
# 特写改用方图：NAI 在纵版画布上画 close-up 极易切成上下多格/重复画面
# （实测 split screen / multiple views 等负向词压不住），换成方图后就正常了。
# 实测会把**脸**画坏的画师串：额头出现异常亮带、眼睛塌陷。
# 与「画得成熟」不同，这是结构崩坏，任何角色都不该用。
STYLE_BLOCK_TITLES = frozenset({
    "NAI4.5时期：2",
})

# 832x832 = 64 的倍数、约 69 万像素，稳稳在免费带内。
TIGHT_SHOT_SIZE = "832x832"
# 词边界匹配男性词：不能简单用子串，否则 "female" 里的 "male" 会被误判
_MALE_RE = re.compile(r"\b(?:man|men|boy|boys|male)\b")

# 纵深补充：库文件缺字段时的兜底
_FALLBACK_LIBRARY: dict[str, Any] = {
    "quality": "masterpiece, best quality, highres",
    "negative": "lowres, bad anatomy, bad hands, watermark, text, worst quality, low quality",
    "style": ["smartphone selfie, natural lighting, realistic"],
    "composition": ["upper body portrait, looking at viewer"],
    "lighting": {
        "早晨": "soft morning sunlight",
        "上午": "bright daylight",
        "中午": "harsh midday sun",
        "下午": "afternoon sunlight",
        "晚上": "warm indoor lighting",
        "深夜": "dim night, cool blue tones",
    },
    "persona": {},
}


def _merge(base: dict, override: dict) -> dict:
    """浅合并 + persona 深合并。"""
    result = dict(base)
    for key, value in override.items():
        if key == "persona" and isinstance(value, dict) and isinstance(result.get(key), dict):
            merged_persona = dict(result[key])
            for pid, cfg in value.items():
                if isinstance(cfg, dict) and isinstance(merged_persona.get(pid), dict):
                    merged = dict(merged_persona[pid])
                    merged.update(cfg)
                    merged_persona[pid] = merged
                else:
                    merged_persona[pid] = cfg
            result["persona"] = merged_persona
        else:
            result[key] = value
    return result


class PromptLibrary:
    """提示词库。"""

    def __init__(
        self,
        data: dict[str, Any],
        *,
        source: str = "default",
        scenes: list[dict[str, Any]] | None = None,
        scene_meta: dict[str, Any] | None = None,
    ) -> None:
        self.data = data or {}
        self.source = source
        self._scenes: list[dict[str, Any]] = scenes or []
        self._styles: list[dict[str, Any]] = (scene_meta or {}).get("_styles") or []
        self.scene_meta: dict[str, Any] = scene_meta or {}

    # -- 加载 --------------------------------------------------------------- #
    @classmethod
    def load(cls, extra_path: str | Path | None = None) -> "PromptLibrary":
        """加载提示词库（默认库 + 可选本地覆盖 + 配置指定文件）。"""
        data: dict[str, Any] = {}
        sources: list[str] = []

        for path in (DEFAULT_PATH, LOCAL_PATH):
            loaded = cls._read(path)
            if loaded:
                data = _merge(data, loaded)
                sources.append(path.name)

        if extra_path:
            loaded = cls._read(Path(extra_path))
            if loaded:
                data = _merge(data, loaded)
                sources.append(Path(extra_path).name)

        if not data:
            log.warning("提示词库为空，使用内置兜底")
            data = dict(_FALLBACK_LIBRARY)

        # 补齐缺失字段
        for key, value in _FALLBACK_LIBRARY.items():
            data.setdefault(key, value)

        lib = cls(data, source="+".join(sources) or "builtin")
        log.info(
            "提示词库已加载（来源 %s）：风格 %d 条，人设 %d 个",
            lib.source, len(lib.style()), len(data.get("persona") or {}),
        )

        # 场景库是可选的：缺失时插件照常工作，只是选题退回"让模型自己想"
        doc = cls._read(SCENES_PATH) or {}
        scenes = [s for s in (doc.get("scenes") or []) if isinstance(s, dict) and s.get("g")]
        lib._scenes = scenes
        lib._styles = [s for s in (doc.get("styles") or []) if isinstance(s, dict) and s.get("g")]
        lib.scene_meta = {
            "version": doc.get("version", ""),
            "source": doc.get("source", ""),
            "attribution": doc.get("attribution") or [],
            "note": doc.get("note", ""),
        }
        if scenes:
            log.info(
                "场景库已加载：%d 条 + 画师串 %d 组（版本 %s）",
                len(scenes), len(lib._styles), doc.get("version") or "?",
            )
        else:
            log.info("没有场景库（scenes.json 缺失或为空），选题将退化为让模型自由发挥")
        return lib

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            if not path.is_file():
                return None
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            log.warning("读取提示词库 %s 失败：%s", path, exc)
            return None

    # -- 取词 --------------------------------------------------------------- #
    def quality(self) -> str:
        return str(self.data.get("quality") or "")

    def negative(self) -> str:
        return str(self.data.get("negative") or "")

    def style(self) -> list[str]:
        value = self.data.get("style") or []
        return [str(v) for v in value if str(v).strip()]

    def composition(self) -> list[str]:
        value = self.data.get("composition") or []
        return [str(v) for v in value if str(v).strip()]

    def lighting(self, daypart: str) -> str:
        table = self.data.get("lighting") or {}
        return str(table.get(daypart) or table.get("晚上") or "")

    def persona(self, persona_id: str) -> dict[str, Any]:
        table = self.data.get("persona") or {}
        cfg = table.get(persona_id)
        if isinstance(cfg, dict):
            return cfg
        # 退一步：按包含关系找（防止人设名带后缀）
        for key, value in table.items():
            if isinstance(value, dict) and (key in persona_id or persona_id in key):
                return value
        return {}

    def persona_appearance(self, persona_id: str) -> str:
        return str(self.persona(persona_id).get("appearance") or "")

    def persona_parts(self, persona_id: str) -> tuple[str, str, str]:
        """返回 (identity, hair, outfit)。

        新版人设把外观拆成三段：identity 不变（发色/瞳色/身体特征），
        hair 与 outfit 可换。老格式没有这三段时退化成
        ``(appearance, "", "")``，行为与以前一致。
        """
        cfg = self.persona(persona_id)
        identity = str(cfg.get("identity") or "").strip()
        hair = str(cfg.get("hair") or "").strip()
        outfit = str(cfg.get("outfit") or "").strip()
        if not identity:
            return str(cfg.get("appearance") or "").strip(), "", ""
        return identity, hair, outfit

    def pick_hairstyle(self, *, avoid: set[str] | None = None) -> str:
        pool = [h for h in HAIRSTYLES if h not in (avoid or set())] or list(HAIRSTYLES)
        return random.choice(pool)

    def pick_outfit(
        self, *, avoid_ids: set[str] | None = None, min_len: int = 40
    ) -> dict[str, Any] | None:
        """从衣柜类法典里挑一套替换服装。"""
        avoid = avoid_ids or set()
        pool = [
            s for s in self._scenes
            if s.get("c") in WARDROBE_COLLECTIONS
            and s.get("i") not in avoid
            and self.scene_usable(str(s.get("g") or ""))
            and len(str(s.get("g") or "")) >= min_len
        ]
        if not pool:
            return None
        weights = [max(1.0, float(len(str(s.get("g") or "")))) for s in pool]
        return random.choices(pool, weights=weights, k=1)[0]

    def lora(self, persona_id: str) -> tuple[str, float, str]:
        """返回 (lora 名, 权重, 触发词)。"""
        cfg = self.persona(persona_id)
        name = str(cfg.get("lora") or "").strip()
        try:
            weight = float(cfg.get("lora_weight") or 0.0)
        except (TypeError, ValueError):
            weight = 0.0
        trigger = str(cfg.get("trigger") or "").strip()
        return name, weight, trigger

    # -- 场景库 ------------------------------------------------------------- #
    def scene_count(self) -> int:
        return len(self._scenes)

    def has_scenes(self) -> bool:
        return bool(self._scenes)

    def pick_scene(
        self,
        *,
        avoid_ids: set[str] | None = None,
        min_len: int = 60,
        collections: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """挑一条场景。

        * ``avoid_ids``：最近用过的 id，避开以免重复（用尽了就允许重复）
        * ``min_len``：太短的条目（单个服装词）撑不起一条说说，尽量不用
        * ``collections``：只从这些法典里挑
        * 权重按标签长度加权 —— 内容越丰富越容易被选中

        会滤掉**性别/题材不符**的条目：库里有大量 `1boy`、兽人、机甲、怪物场景，
        跟矩阵里这四个女孩子放到一起会直接画出怪东西。
        """
        if not self._scenes:
            return None
        avoid = avoid_ids or set()

        pool = [
            s for s in self._scenes
            if s.get("i") not in avoid
            and (not collections or s.get("c") in collections)
            and self.scene_usable(str(s.get("g") or ""))
        ]
        if not pool:
            pool = [
                s for s in self._scenes
                if not collections or s.get("c") in collections
            ]
        if not pool:
            pool = list(self._scenes)

        # 优先「单人女性」场景：体型/服装信息最贴合自拍
        single_girl = [s for s in pool if SCENE_PREFER_TAG in str(s.get("g") or "").lower()]
        if single_girl:
            pool = single_girl

        # 先只要够长的；都不够长再放宽
        rich = [s for s in pool if len(str(s.get("g") or "")) >= min_len]
        if rich:
            pool = rich

        weights = [max(1.0, float(len(str(s.get("g") or ""))) ** 1.2) for s in pool]
        return random.choices(pool, weights=weights, k=1)[0]

    # -- 画师串（改画风） ---------------------------------------------------- #
    def style_count(self) -> int:
        return len(self._styles)

    def pick_artist_style(
        self,
        *,
        avoid_titles: set[str] | None = None,
        persona_id: str = "",
    ) -> dict[str, Any] | None:
        """随机挑一组画师串（编纂者精选的组合，比单画师稳）。

        会按**人设**过滤：有些画师串画出来的角色偏成熟，
        用在「少女模样」的角色上就不对（实测风堇会被画成熟）。
        每份人设可以用 ``style_avoid`` 声明避用的画师串关键词。
        """
        if not self._styles:
            return None
        avoid = avoid_titles or set()
        cfg = self.persona(persona_id) if persona_id else {}
        banned = [str(x).lower() for x in (cfg.get("style_avoid") or []) if str(x).strip()]

        def _ok(style: dict[str, Any]) -> bool:
            title = str(style.get("t"))
            if title in avoid or title in STYLE_BLOCK_TITLES:
                return False
            blob = (str(style.get("t", "")) + " " + str(style.get("g", ""))).lower()
            return not any(b in blob for b in banned)

        pool = [s for s in self._styles if _ok(s)]
        if not pool:
            # 全被排掉时退回"只按 avoid_titles 过滤"，否则人物会完全没画风
            pool = [
                s for s in self._styles
                if str(s.get("t")) not in avoid and str(s.get("t")) not in STYLE_BLOCK_TITLES
            ] or list(self._styles)

        # 组合画师串（编纂者精选的多人组，``k="bundle"``）比单画师稳得多，
        # 优先用它；只有小概率抽单画师词典，给画风留一点意外性。
        bundles = [s for s in pool if s.get("k") != "single"]
        singles = [s for s in pool if s.get("k") == "single"]
        if bundles and (not singles or random.random() < 0.85):
            return random.choice(bundles)
        if singles:
            return random.choice(singles)
        return random.choice(pool)

    def find_artist_style(self, name: str) -> dict[str, Any] | None:
        """按标题找画师串（允许部分匹配）。"""
        target = (name or "").strip().lower()
        if not target:
            return None
        for s in self._styles:
            if str(s.get("t", "")).strip().lower() == target:
                return s
        for s in self._styles:
            if target in str(s.get("t", "")).strip().lower():
                return s
        return None

    # -- 构图与画布 ---------------------------------------------------------- #
    @staticmethod
    def is_tight_shot(scene_tags: str) -> bool:
        low = str(scene_tags or "").lower()
        return any(m in low for m in TIGHT_SHOT_MARKERS)

    def shot_size(self, scene_tags: str, base: str = "") -> str:
        """特写场景改用方图，其余沿用 base（留空则由端点默认）。"""
        if self.is_tight_shot(scene_tags):
            return TIGHT_SHOT_SIZE
        return base

    @staticmethod
    def scene_usable(tags: str) -> bool:
        """判断某条场景是否适合给这四个女性角色用。

        用**词边界**匹配男女词，避免 ``\bmale\b`` 误伤 ``female``。
        """
        low = str(tags or "").lower()
        if any(block in low for block in SCENE_BLOCK_TAGS):
            return False
        return not _MALE_RE.search(low)


    def scene_tags(self, scene: dict[str, Any] | None) -> str:
        return str((scene or {}).get("g") or "").strip()

    def scene_title(self, scene: dict[str, Any] | None) -> str:
        return str((scene or {}).get("t") or "").strip()

    def scene_collection(self, scene: dict[str, Any] | None) -> str:
        return str((scene or {}).get("c") or "").strip()

    # -- 组装 --------------------------------------------------------------- #
    def build(
        self,
        *,
        persona_id: str,
        scene: str,
        daypart: str = "晚上",
        composition: str = "",
        lora_override: str = "",
        lora_weight_override: float | None = None,
        trigger_override: str = "",
        use_lora: bool = True,
        use_trigger: bool = True,
        include_composition: bool = True,
        artist_style: str = "",
        outfit_override: str = "",
        hair_override: str = "",
        negative_override: str = "",
        extra_style: str = "",
    ) -> str:
        """把「库里的稳定部分」和「模型现想的画面」拼成最终正向提示词。

        Args:
            persona_id: 人设 id（决定外观锚点与 LoRA）。
            scene: 模型想的画面（场景/动作/情绪），可以是一段自然语言或标签。
            daypart: 时段，用于挑光线词。
            composition: 构图词；留空时从库里随机挑一条。
            lora_override / lora_weight_override / trigger_override:
                该号在插件配置里单独指定的 LoRA，优先于提示词库。
            use_lora: **是否注入 ``<lora:...>`` 标签**。用云端绘画 API
                （豆包 Seedream / Gemini 等闭源模型）时必须设 False ——
                那些是闭源模型，不认识这个语法，会把整串当普通文字画成怪物。
            use_trigger: **是否注入角色名（Danbooru 角色 tag）**，与 LoRA 独立。
                NovelAI 自带角色知识库，把 ``denia (wuthering waves)`` 写进提示词
                就能直接出对角色（实测比只给外观词准得多）；本地 SD 也要它。
                但面对闭源云端模型时建议关掉 —— 写角色名反而更容易撞版权审核。
            negative_override: 只在调用方需要时用；负向另见 :meth:`negative`。
            extra_style: 额外风格词（比如"胶片质感"）。
        """
        # 角色名（Danbooru 角色 tag）：与 LoRA 独立开关
        trigger = (trigger_override or "").strip() if use_trigger else ""
        if use_trigger and not trigger:
            _name, _weight, lib_trigger = self.lora(persona_id)
            trigger = lib_trigger
        identity, hair, outfit = self.persona_parts(persona_id)
        # 体型/年龄感：放在 identity 之后，用来压住"画师串把角色画成熟"的倾向
        body = str(self.persona(persona_id).get("body") or "").strip()
        if body:
            identity = f"{identity}, {body}" if identity else body
        if hair_override.strip():
            hair = hair_override.strip()
        if outfit_override.strip():
            outfit = outfit_override.strip()
        # 三段齐全时按 identity + hair + outfit 拼；老格式退化成整段 appearance
        appearance = (
            ", ".join(x for x in (identity, hair, outfit) if x) if identity else identity
        )

        scene_text = (scene or "").strip()
        tight_shot = any(m in scene_text.lower() for m in TIGHT_SHOT_MARKERS)
        if tight_shot and appearance:
            # 特写只要脸：保留角色名让 NAI 认出是谁，服装描述整段丢掉。
            appearance = ""

        # 顺序讲究：触发词/角色名 → 外观 → **画面** → LoRA → 构图/光线/画风/质量
        head: list[str] = []
        if trigger:
            head.append(trigger)
        if appearance:
            head.append(appearance)

        tail: list[str] = []

        lora_name = (lora_override or "").strip() if use_lora else ""
        weight = lora_weight_override
        if use_lora and not lora_name:
            lora_name, lib_weight, _lib_trigger = self.lora(persona_id)
            weight = weight if weight is not None else lib_weight
        if use_lora:
            if weight is None or weight <= 0:
                weight = 0.8
            # LoRA 语法交给 WebUI 处理；这里写成标准 <lora:name:weight>
            if lora_name:
                tail.append(f"<lora:{lora_name}:{round(float(weight), 2)}>")

        # 场景库的条目自带镜头/构图说明（例如 1.2::medium shot, from a low angle::），
        # 这时**不能再叠一条随机构图词** —— 两者会打架，实测会把「脸部特写」画成蹲姿全身。
        comp = (composition or "").strip() if include_composition else ""
        if include_composition and not comp:
            options = self.composition()
            comp = random.choice(options) if options else ""
        if comp:
            tail.append(comp)

        light = self.lighting(daypart)
        if light:
            tail.append(light)

        styles = self.style()
        if styles:
            tail.append(random.choice(styles))
        if extra_style.strip():
            tail.append(extra_style.strip())

        quality = self.quality()
        if quality:
            tail.append(quality)

        # 去重（保留首次出现的顺序）
        #
        # ⚠️ **场景段落不参与拆分去重**：场景库里的标签含 NovelAI 权重语法，
        # 例如 ``1.3::medium shot, eye-level::`` —— 按逗号切开再拼回去虽然大多能复原，
        # 但一旦某一片被判定为重复而删掉，``::`` 配对就断了，整段权重失效。
        # 所以场景作为**不可分割的整体**插在中间。
        seen: set[str] = set()

        def _dedup(chunks: list[str]) -> list[str]:
            out: list[str] = []
            for chunk in chunks:
                for piece in str(chunk).split(","):
                    piece = piece.strip()
                    if not piece or piece.lower() in seen:
                        continue
                    seen.add(piece.lower())
                    out.append(piece)
            return out

        before = _dedup(head)
        after = _dedup(tail)
        pieces = before + ([scene_text] if scene_text else []) + after
        # 画师串同样是**整段不可拆**（里面是 0.6::mignon,artist:xxx,...:: 这种权重组），
        # 按逗号切开去重会打断 :: 配对，所以整体追加在最后。
        if artist_style.strip():
            pieces.append(artist_style.strip())
        return ", ".join(pieces)

    def resolved_negative(self, override: str = "", persona_id: str = "") -> str:
        base = (override or "").strip() or self.negative()
        extra = ""
        if persona_id:
            extra = str(self.persona(persona_id).get("negative_extra") or "").strip()
        if extra and extra.lower() not in base.lower():
            return f"{base}, {extra}"
        return base
