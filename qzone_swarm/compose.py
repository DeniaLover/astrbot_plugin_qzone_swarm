"""提示词构造与模型输出清洗。

这里集中所有「怎么让模型写出像人设本人发的说说/评论/回复」的规则，
方便单独调整而不动业务逻辑。本模块不导入 astrbot。
"""

from __future__ import annotations

import logging
import re

from .logging_compat import get_logger
from .models import BotProfile, FeedItem
from .sources import Material, material_to_text, summarize_senders
from .util import clean_text, clock_text, daypart_text, humanize_age, strip_quotes, weekday_text

log = get_logger("compose")

POST_MIN_LEN = 4
POST_MAX_LEN = 280
COMMENT_MAX_LEN_DEFAULT = 60


# --------------------------------------------------------------------------- #
# 公用的人设块
# --------------------------------------------------------------------------- #
def persona_block(persona_name: str, persona_prompt: str, extra: str = "") -> str:
    parts = [
        f"你正在以「{persona_name}」这个角色本人的身份运营自己的 QQ 空间。",
        "以下是你的人设，必须严格遵守；人设里没写的东西不要凭空发明。",
        "",
        persona_prompt.strip() if persona_prompt.strip() else "（没有人设文本，请保持自然、口语化的中文表达。）",
    ]
    if extra.strip():
        parts += ["", "【额外要求】", extra.strip()]
    parts += [
        "",
        "【通用写作纪律】",
        "1. 用第一人称，像真人随手发动态，不要写解说，不要自称 AI。",
        "2. 不要提到「人设」「提示词」「模型」「说说」这些字眼。",
        "3. 不要输出 Markdown 标记、编号列表或解释性文字，只输出正文本身。",
        "4. 不要编造人设里没有的重大事件（生死、恋爱、职业变动等）。",
        "5. 语言自然口语化，允许网络化表达，但不要滥用表情符号。",
    ]
    return "\n".join(parts)


def _time_context() -> str:
    return f"现在是{weekday_text()} {clock_text()}（{daypart_text()}）。"


# --------------------------------------------------------------------------- #
# 说说：有素材
# --------------------------------------------------------------------------- #
def build_post_from_material_prompt(
    profile: BotProfile,
    persona_name: str,
    persona_prompt: str,
    material: Material,
) -> tuple[str, str]:
    """基于聊天记录写说说，返回 (system_prompt, user_prompt)。"""
    system = persona_block(persona_name, persona_prompt, profile.persona_extra)
    chat = material_to_text(material, max_chars=6000)
    people = summarize_senders(material)

    user = "\n".join(
        [
            "【任务】发一条 QQ 空间说说。",
            "",
            _time_context(),
            "",
            "下面是你最近参与或关注到的聊天记录（来自你所在的群聊或私聊）：",
            "----- 聊天记录开始 -----",
            chat,
            "----- 聊天记录结束 -----",
            "",
            f"这些聊天里的人：{people or '（暂无）'}",
            "",
            "【要求】",
            "1. 从上面这些聊天里挑一个你真觉得有意思、或者跟你有关系的点来发。",
            "2. 像当事人发朋友圈那样有感而发，不要复述聊天记录，也不要逐条总结。",
            "3. 不要点名批评或暴露别人的隐私，不要把聊天内容原样抄一遍。",
            "4. 不要用「大家」「各位」这种像公告的口吻。",
            f"5. 长度控制在 {POST_MIN_LEN}~{POST_MAX_LEN} 字，通常一两句话就够。",
            "6. 只输出说说正文，不要任何前后缀。",
        ]
    )
    return system, user


# --------------------------------------------------------------------------- #
# 说说：没有素材 -> 按人设的行动轨迹
# --------------------------------------------------------------------------- #
def build_post_from_scene_prompt(
    profile: BotProfile,
    persona_name: str,
    persona_prompt: str,
    scene_title: str,
    scene_tags: str,
    collection: str = "",
) -> tuple[str, str]:
    """围绕**已经选定的画面**写一条说说。

    与 ``build_post_from_trajectory_prompt`` 的区别是方向相反：
    这里是「图先定、文后写」，所以说说天然和配图对得上 ——
    不会出现"文字说在看书、图里却在打伞"这种情况。

    画面来自场景库（社区整理的提示词法典），标题是中文，标签是英文 Danbooru 风。
    只把**标题**给模型当画面锚点，标签作为辅助理解，并要求它别照抄英文。
    """
    system = persona_block(persona_name, persona_prompt, profile.persona_extra)

    hint = (scene_tags or "").strip()
    if len(hint) > 420:
        hint = hint[:420] + "…"

    lines = [
        "【任务】你刚拍了一张照片，准备发 QQ 空间，围绕**这张照片**写一条说说。",
        "",
        _time_context(),
        "",
        f"【这张照片拍的画面】《{scene_title}》",
    ]
    if collection:
        lines.append(f"（出处分类：{collection}）")
    if hint:
        lines += [
            f"画面细节（英文标签，**只用来帮你理解画面，绝对不要照抄英文**）：{hint}",
        ]
    lines += [
        "",
        "【要求】",
        "1. 用你人设的口吻，像随手发的日常，不是作文。",
        "2. **说说要能呼应这张照片**：为什么拍它、当时在做什么、看到/听到了什么、什么心情。",
        "3. 不要描述自己的长相穿着，也不要写「今天自拍」「配图是」这类标签式的话。",
        "4. 不要编造人设里没有的重大剧情。",
        f"5. 长度 {POST_MIN_LEN}~{POST_MAX_LEN} 字，通常一两句话。",
        "6. 只输出说说正文。",
    ]
    return system, "\n".join(lines)


def build_post_from_trajectory_prompt(
    profile: BotProfile,
    persona_name: str,
    persona_prompt: str,
) -> tuple[str, str]:
    """没有聊天素材时，让人设按「这个时间点它大概在做什么」来写。"""
    system = persona_block(persona_name, persona_prompt, profile.persona_extra)

    user = "\n".join(
        [
            "【任务】发一条 QQ 空间说说。",
            "",
            _time_context(),
            "",
            "现在没有可用的聊天记录作为素材。请你代入这个角色，",
            "想想在**这个星期几、这个时间点**，以她/他的生活轨迹最可能正在做什么、",
            "心里在想什么，然后据此发一条自然的日常说说。",
            "",
            "【要求】",
            "1. 内容要贴着人设的身份、生活环境和性格，符合她/他此刻的作息。",
            "2. 写具体的日常片段（正在做的事、看到的东西、一点小情绪），不要空泛。",
            "3. 不要写成日记体流水账，也不要喊口号。",
            "4. 不要编造人设里没有的重大剧情转折。",
            f"5. 长度控制在 {POST_MIN_LEN}~{POST_MAX_LEN} 字，通常一两句话。",
            "6. 只输出说说正文，不要任何前后缀。",
        ]
    )
    return system, user


# --------------------------------------------------------------------------- #
# 自拍生图提示词 / 配文
# --------------------------------------------------------------------------- #
# 从 qzone_swarm/NAI_Prompt_Constraint_v2.md 的「模块 5：画面具体内容」提炼。
# 完整规范是给「一次性输出整条 NAI 提示词」的场景用的；本插件的分工是
# 模型只写画面内容、外观与画风由 PromptLibrary 统一拼装，所以只取其中
# 「拒绝站桩、要有事件与物理反馈」这一条真正影响出片质量的规则。
SCENE_DYNAMICS_BLOCK = (
    "【画面必须「有事发生」】\n"
    "不要写成原地站着摆拍。下面三类至少体现两项：\n"
    "1. 明确的动作与姿态张力 —— 正在做一件具体的事，身体有倾向、有动势；\n"
    "2. 人物与环境的物理互动 —— 风吹起什么、踩到水花、手里的东西发光、\n"
    "   光斑落在身上、衣角被带起；\n"
    "3. 动态视觉元素 —— 飘落的花瓣、飞舞的尘埃、水汽、旋转的落叶、光斑。\n"
    "构图要让人物和周围环境产生关系，而不是站在一片空白里。"
)


def build_selfie_prompt_prompt(
    profile: BotProfile,
    persona_name: str,
    persona_prompt: str,
    *,
    latest_post: str = "",
    use_constraint: bool = True,
) -> tuple[str, str]:
    """让模型想「这一张自拍拍的是什么画面」。

    注意分工：**外观、画风、质量词、LoRA 都由提示词库负责**（见 ``promptlib``），
    模型只负责"这一刻想拍什么"——场景、动作、情绪、手里拿着什么、和谁在一起。
    这样同一个角色的自拍前后一致，不会被模型每次随口改掉发色瞳色。
    """
    system = persona_block(persona_name, persona_prompt, profile.persona_extra)
    lines = [
        "【任务】你要发一张自己出镜的照片到 QQ 空间，描述**这张照片拍的是什么画面**。",
        "",
        _time_context(),
        "",
    ]
    if latest_post.strip():
        lines += [
            "你刚发的说说：",
            latest_post.strip(),
            "让这张照片和上面这条说说的内容呼应上。",
            "",
        ]
    lines += [
        "【只写画面内容】",
        "1. 你在哪儿、正在做什么、什么姿势。**不必是自拍** —— 可以是别人帮你拍、"
        "随手抓拍，也可以是你恰好出现在画面里；视角也不必是举着手机对镜。",
        "2. 周围有什么值得入镜的东西（书、杯子、猫、街景…），以及你的表情和心情。",
        "3. 用一段连贯的中文描述，不要分点，不要加引号。",
        "",
        "【不要写】",
        "发色、瞳色、衣服款式等外貌特征，也不要写画风、画质、镜头参数 ——",
        "那些由系统统一控制，你写了会被忽略。",
        "",
    ]
    if use_constraint:
        lines += [SCENE_DYNAMICS_BLOCK, ""]
    lines.append("只输出画面描述本身。")
    return system, "\n".join(lines)


def build_selfie_caption_prompt(
    profile: BotProfile,
    persona_name: str,
    persona_prompt: str,
    image_prompt: str,
) -> tuple[str, str]:
    """给自拍配一句说说正文。"""
    system = persona_block(persona_name, persona_prompt, profile.persona_extra)
    user = "\n".join(
        [
            "【任务】你刚发了一张自己出镜的照片到 QQ 空间，写一句配文。",
            "",
            _time_context(),
            "",
            f"这张照片的画面描述：{image_prompt.strip()}",
            "",
            "【要求】",
            "1. 简短自然，像随手发的，一两句话。",
            "2. 不要描述长相，也不要写「今天自拍」这种直白标签。",
            f"3. 长度 {POST_MIN_LEN}~{POST_MAX_LEN} 字。",
            "4. 只输出配文本身。",
        ]
    )
    return system, user


# --------------------------------------------------------------------------- #
# 评论 / 回复
# --------------------------------------------------------------------------- #
def build_comment_decision_prompt(
    profile: BotProfile,
    persona_name: str,
    persona_prompt: str,
    item: FeedItem,
) -> tuple[str, str]:
    """先判断要不要评论、怎么评论，输出 JSON。"""
    system = persona_block(persona_name, persona_prompt, profile.persona_extra)
    author = item.nickname or str(item.hostuin)
    body = clean_text(item.content, max_len=400) or "（这条说说只有图片，没有文字）"

    user = "\n".join(
        [
            "【任务】你在刷 QQ 空间，看到了下面这条动态。",
            "",
            _time_context(),
            "",
            f"发布者：{author}",
            f"发布时间：{humanize_age(item.created_at) or '刚刚'}",
            f"内容：{body}",
            f"（已有 {item.like_count} 个赞、{item.comment_count} 条评论）",
            "",
            "【要求】",
            "1. 判断以你的人设，看到这条动态会不会想说话。",
            "2. 如果想说，写一句符合你性格的评论；冷淡、毒舌或热情都可以，但要贴人设。",
            "3. 评论要针对内容本身，不要泛泛地夸一句。",
            "4. 不要提到点赞或评论这些词，不要自报身份。",
            f"5. 评论不超过 {profile.comment_max_length or COMMENT_MAX_LEN_DEFAULT} 个字，通常一句话。",
            "",
            "【输出格式】只输出一个 JSON 对象，不要任何其他文字：",
            '{"should_comment": true, "should_like": true, "comment": "评论内容"}',
            "should_comment / should_like 用 true 或 false；不评论时 comment 留空字符串。",
        ]
    )
    return system, user


def build_reply_prompt(
    profile: BotProfile,
    persona_name: str,
    persona_prompt: str,
    item: FeedItem,
    comment,
) -> tuple[str, str]:
    """回复别人在自己说说下的评论。"""
    system = persona_block(persona_name, persona_prompt, profile.persona_extra)
    who = getattr(comment, "name", "") or str(getattr(comment, "uin", ""))
    own = clean_text(item.content, max_len=300) or "（只有图片）"
    said = clean_text(getattr(comment, "content", ""), max_len=200)

    user = "\n".join(
        [
            "【任务】有人在你发的 QQ 空间说说下面留言了，你要回一句。",
            "",
            _time_context(),
            "",
            f"你的说说：{own}",
            f"{who} 留言：{said}",
            "",
            "【要求】",
            "1. 用你的人设口吻自然回应这个人，像是在跟自己认识的人说话。",
            "2. 承接对方的话，不要答非所问，也不要只回一句谢谢。",
            "3. 不要提到留言或回复这些词。",
            f"4. 不超过 {profile.comment_max_length or COMMENT_MAX_LEN_DEFAULT} 个字，通常一句话。",
            "5. 只输出回复内容本身。",
        ]
    )
    return system, user


# --------------------------------------------------------------------------- #
# 输出清洗
# --------------------------------------------------------------------------- #
def clean_post(text: str, *, max_len: int = POST_MAX_LEN) -> str:
    cleaned = strip_quotes(clean_text(text, max_len=0))
    cleaned = clean_text(cleaned, max_len=max_len)
    return cleaned


def clean_comment(text: str, *, max_len: int = COMMENT_MAX_LEN_DEFAULT) -> str:
    cleaned = strip_quotes(clean_text(text, max_len=0))
    cleaned = cleaned.replace("\n", " ")
    cleaned = clean_text(cleaned, max_len=max_len)
    return cleaned


# 只在**整句像是模型在推脱 / 自报 AI 身份**时才判为不可用。
#
# ⚠️ 千万不要拿「无法」「作为一个」「抱歉」这类高频词做子串黑名单 ——
#    人设正常说话就会用到。线上真实踩过：莫宁写了一句
#    「试着将这种行为代入时间效益模型，参数却始终无法…」，
#    被「无法」这个词整条毙掉，一条好好的说说就这么丢了。
_REFUSAL_PATTERNS = (
    r"作为(一个)?\s*(ai|人工智能|语言模型|智能助手)",
    r"我是(一个)?\s*(ai|人工智能|语言模型)",
    r"语言模型",
    r"无法(生成|提供|完成|满足|协助|回答|撰写|输出)",
    r"不能(生成|提供|完成|满足|协助|撰写|输出)",
    r"(抱歉|对不起)\s*[，,]\s*我",
    r"as an ai\b",
    r"\bi (cannot|can't|am unable)\b",
)

# 模型把提示词当正文抄了回来
_PROMPT_ECHO_PATTERNS = (
    r"【要求】",
    r"【任务】",
    r"只输出说说正文",
    r"^\s*(说说|正文|内容)\s*[:：]",
)

_REFUSAL_RE = re.compile("|".join(_REFUSAL_PATTERNS), re.IGNORECASE)
_PROMPT_ECHO_RE = re.compile("|".join(_PROMPT_ECHO_PATTERNS), re.IGNORECASE)


def is_usable_post(text: str) -> bool:
    """判断模型产出的是不是一条可以发出去的说说。

    判定原则：**宁可放过，不可错杀**。只拦两类东西：

    1. 模型在推脱或自报 AI 身份（"作为一个AI我无法生成…"）；
    2. 把提示词当正文抄了回来（"【要求】…"）。

    「无法」「作为一个」「抱歉」这类词在正常中文里太常见，
    **绝不能**单独作为黑名单 —— 那会把好端端的说说毙掉。
    """
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < POST_MIN_LEN:
        return False
    if _PROMPT_ECHO_RE.search(stripped):
        return False
    if _REFUSAL_RE.search(stripped):
        return False
    return True

