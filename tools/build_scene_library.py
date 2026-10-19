#!/usr/bin/env python3
"""从「法典图鉴」提炼场景提示词库。

数据源：https://novelai.quicktagcloud.com/ 的公开静态数据
        （assets.quicktagcloud.com/data/releases/<release>/share/<codex>.json）

做的事：
  1. 拉最新 release 指针，钉死版本（可复现）
  2. 只取 share/ 下的精简版（不含图，体积小）
  3. **剔除三个 R18 法典**，再按词表 + 分类黑名单过滤擦边内容
  4. 丢掉过短的条目（< 40 字符，像 "swimsuit" 这种撑不起一条说说）
  5. 按 tags 去重，输出紧凑 JSON

版权：法典内容版权归各自作者所有，本站仅作整理索引。
      本脚本产出的库仅供**本地自用**，不再分发；文件里保留署名。

用法：
    python tools/build_scene_library.py                # 联网构建
    python tools/build_scene_library.py --no-proxy     # 不走代理（海外网络）
    python tools/build_scene_library.py --dry-run      # 只统计不落盘
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import httpx
except Exception:  # pragma: no cover
    print("需要 httpx：python -m pip install httpx")
    raise SystemExit(1)

SOURCE_SITE = "https://novelai.quicktagcloud.com/"
DATA_ROOT = "https://assets.quicktagcloud.com/data"
PLUGIN_DIR = Path(__file__).resolve().parent.parent
OUT_PATH = PLUGIN_DIR / "qzone_swarm" / "scenes.json"

# 要收录的法典（codex id -> 展示名）。三个 R18 与画师词典、图包一律不收。
COLLECTIONS: list[tuple[str, str]] = [
    ("composition_style", "构图风格"),
    ("qianteng", "叫我千藤就好了の衣柜"),
    ("kisegaeningyou", "更衣人偶"),
    ("raven_composition", "渡鸦的构图鉴"),
    ("suozhang", "所长N4.5常规法典"),
    ("suozhang_nai5", "所长N5常规法典"),
    ("jiegou_yuandian", "解构原典"),
]

# 明确不收录（供日志说明用）
EXCLUDED_R18 = ["suozhang_r18", "suozhang_nai5_r18", "mengshen_r18"]

# 露骨性内容：命中即丢
NSFW_HARD = (
    "nude", "naked", "nipple", "areola", "penis", "vagina", "pussy", "vaginal",
    "cum", "semen", "erection", "sex", "sexual", "intercourse", "sperm",
    "explicit", "hentai", "nsfw", "r18", "r-18", "xxx", "lewd", "seductive",
    "topless", "bottomless", "undressing", "exposed breasts", "large breasts",
    "huge breasts", "breasts out", "panties", "underwear", "lingerie", "bra ",
    "breasts", "cleavage", "thigh gap", "midriff", "navel focus", "bare ass",
    "bikini", "swimsuit", "micro bikini", "see-through", "transparent clothing",
    "bondage", "shibari", "ahegao", "orgasm", "masturbation", "fetish",
    "footjob", "paizuri", "fellatio", "anal", "or*l", "groin", "crotch",
)

# 暴力/血腥：自拍说说用不上，也不该出现在机器人主页
VIOLENCE = (
    "gun", "pistol", "rifle", "blood", "bloody", "gore", "corpse", "dead body",
    "knife", "stab", "wound", "torture", "severed", "decapitat", "behead",
    "hanging", "suicide", "self-harm", "self harm", "guts", "impale",
    "execution", "massacre", "weapon", "sword", "blade", "dagger",
)

# 暗示性但不至露骨：鉴于矩阵里有 15 岁角色，一并排除
SUGGESTIVE = (
    "cleavage", "playboy bunny", "bunny girl", "garter", "cameltoe",
    "nsfw", "seductive pose", "middle finger", "insult", "gesture",
    "man behind", "boy behind", "two men", "1boy", "male focus",
)

# 分类路径黑名单（法典里的分类名）
CATEGORY_BLOCK = (
    "色色", "r18", "R18", "nsfw", "NSFW", "情趣", "内衣",
    "泳装", "比基尼", "裸", "恋物",
)

# 画师串（编纂者精选画师组）单独收集：这是改画风最有效的手段，
# NovelAI 里用 artist:xxx 的权重串直接把画风拉向特定插画师。
ARTIST_CATEGORY_HINT = ("画师", "artist")
# 只要 NAI5 时期的画师串：跨时期的串在 V5 模型上表现参差（NAI3 时代用 {} 语法、
# NAI4.5 权重习惯也不同），统一用 NAI5 的稳定性最好。
STYLE_ERA_ALLOW = ("NAI5",)

MIN_TAGS_LEN = 12
MIN_TITLE_LEN = 2


def build_client(use_proxy: str | None) -> httpx.Client:
    kwargs: dict = {"timeout": httpx.Timeout(180.0), "headers": {"User-Agent": "Mozilla/5.0"}}
    if use_proxy:
        kwargs["proxy"] = use_proxy
    else:
        kwargs["trust_env"] = False
    return httpx.Client(**kwargs)


def fetch_json(client: httpx.Client, url: str):
    r = client.get(url)
    r.raise_for_status()
    return r.json()


def contains_nsfw(text: str) -> str | None:
    low = text.lower()
    for word in NSFW_HARD:
        if word in low:
            return word
    return None


def unsafe_reason(text: str) -> str | None:
    """返回命中的不安全词（含暴力/暗示），没命中返回 None。"""
    low = text.lower()
    for group, words in (("nsfw", NSFW_HARD), ("violence", VIOLENCE), ("suggestive", SUGGESTIVE)):
        for word in words:
            if word in low:
                return f"{group}:{word}"
    return None


def category_blocked(category: str) -> bool:
    return any(block in category for block in CATEGORY_BLOCK)


def split_description(desc: str) -> tuple[str, str]:
    """"分类路径 | 实际标签" -> (分类路径, 标签)。只切第一个竖线。"""
    text = (desc or "").strip()
    if "|" in text:
        left, _, right = text.partition("|")
        return left.strip(), right.strip()
    return "", text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="http://127.0.0.1:7897", help="代理地址（默认本机 7897）")
    ap.add_argument("--no-proxy", action="store_true", help="不走代理")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args()

    client = build_client(None if args.no_proxy else args.proxy)

    print("① 解析数据源")
    ds = fetch_json(client, f"{SOURCE_SITE.rstrip('/')}/data-source.json")
    base = ds.get("baseUrl") or DATA_ROOT
    pointer = ds.get("pointer") or "current.json"
    current = fetch_json(client, f"{base}/{pointer}")
    release = current["release"]
    rel_url = f"{base}/releases/{release}"
    print(f"   baseUrl={base}")
    print(f"   release={release}  publishedAt={current.get('publishedAt')}")
    manifest = fetch_json(client, f"{rel_url}/manifest.json")

    if not args.no_proxy:
        print(f"   （走代理 {args.proxy}）")

    attribution: list[str] = []
    # 作者信息只存在于 codexes.json（share/ 精简版里没有）
    try:
        codexes = fetch_json(client, f"{rel_url}/codexes.json")
        by_id = {c.get("id"): c for c in codexes if isinstance(c, dict)}
        for codex_id, _label in COLLECTIONS:
            info = by_id.get(codex_id) or {}
            for name in [info.get("author")] + [c.get("name") for c in (info.get("contributors") or [])]:
                if name and str(name).strip() and str(name) not in attribution:
                    attribution.append(str(name).strip())
        print(f"   作者署名 {len(attribution)} 个")
    except Exception as exc:
        print(f"   ⚠ 读 codexes.json 失败（署名会缺失）：{exc}")
    scenes: list[dict] = []
    styles: list[dict] = []
    seen_tags: set[str] = set()
    seen_styles: set[str] = set()
    stats: dict[str, dict] = {}
    dropped = {"nsfw": 0, "category": 0, "short": 0, "dup": 0, "bad": 0}

    for codex_id, label in COLLECTIONS:
        url = f"{rel_url}/share/{codex_id}.json"
        print(f"\n② 拉取 {codex_id}（{label}）")
        if codex_id + ".json" not in manifest["files"] and f"share/{codex_id}.json" not in manifest["files"]:
            print("   ✗ manifest 里没有这个文件，跳过")
            continue
        try:
            data = fetch_json(client, url)
        except Exception as exc:
            print(f"   ✗ 拉取失败：{type(exc).__name__}: {exc}")
            continue

        for author in [data.get("author")] + [c.get("name") for c in (data.get("contributors") or [])]:
            if author and author not in attribution:
                attribution.append(str(author))

        entries = data.get("entries") or {}
        kept = 0
        for entry_id, entry in entries.items():
            if not isinstance(entry, dict):
                dropped["bad"] += 1
                continue
            title = (entry.get("title") or "").strip()
            category, tags = split_description(entry.get("description") or "")
            if not title or len(title) < MIN_TITLE_LEN or not tags:
                dropped["bad"] += 1
                continue
            # 画师串单独收，不进场景池（它们是改画风的，不是画面内容）
            if any(h in category for h in ARTIST_CATEGORY_HINT):
                if STYLE_ERA_ALLOW and not any(e in title.upper() for e in STYLE_ERA_ALLOW):
                    dropped["style_era"] = dropped.get("style_era", 0) + 1
                    continue
                if contains_nsfw(tags) or contains_nsfw(title):
                    dropped["nsfw"] += 1
                    continue
                if len(tags) < 30:
                    dropped["short"] += 1
                    continue
                key = re.sub(r"\s+", " ", tags.lower())
                if key in seen_styles:
                    dropped["dup"] += 1
                    continue
                seen_styles.add(key)
                styles.append({"i": entry_id, "t": title, "g": tags, "c": label})
                continue
            if category_blocked(category):
                dropped["category"] += 1
                continue
            reason = unsafe_reason(tags) or unsafe_reason(title)
            if reason:
                dropped["nsfw"] += 1
                continue
            if len(tags) < MIN_TAGS_LEN:
                dropped["short"] += 1
                continue
            key = re.sub(r"\s+", " ", tags.lower())
            if key in seen_tags:
                dropped["dup"] += 1
                continue
            seen_tags.add(key)
            scenes.append({
                "i": entry_id,
                "t": title,
                "g": tags,           # tags / 提示词
                "c": label,          # 所属法典
                "k": category,       # 法典内部分类
            })
            kept += 1

        stats[codex_id] = {"label": label, "entries": len(entries), "kept": kept}
        print(f"   原始 {len(entries)} 条 → 收录 {kept} 条")

    payload = {
        "schema": 1,
        "version": release,
        "source": SOURCE_SITE,
        "note": (
            "来源：法典图鉴（社区整理的 NovelAI 提示词）。"
            "内容版权归各自作者所有，本站仅作可视化整理与索引。"
            "此文件仅用于本机生图参考，请勿二次分发。"
        ),
        "attribution": attribution,
        "excluded": {"r18": EXCLUDED_R18},
        "styles": styles,
        "scenes": scenes,
    }

    print("\n" + "=" * 70)
    print("统计")
    print("=" * 70)
    for codex_id, info in stats.items():
        print(f"  {info['label']:<22} {info['entries']:>6} → {info['kept']:>6}")
    print(f"  {'合计':<22} {'':>6}   {len(scenes):>6}")
    print()
    print(f"  过滤：露骨 {dropped['nsfw']}  分类黑名单 {dropped['category']}  "
          f"过短 {dropped['short']}  重复 {dropped['dup']}  无效 {dropped['bad']}")
    print(f"  作者署名 {len(attribution)} 个")
    if dropped.get("style_era"):
        print(f"  画师串：按时代过滤掉 {dropped['style_era']} 组（只留 {'/'.join(STYLE_ERA_ALLOW)}）")
    print(f"  画师串 {len(styles)} 组：")
    for s in styles[:6]:
        print(f"     《{s['t']}》 {s['g'][:70]}…")

    if args.dry_run:
        print("\n--dry-run，未写文件")
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    out.write_text(text, encoding="utf-8")
    print(f"\n已写入 {out}  ({len(text) / 1024:.0f} KB, {len(scenes)} 条)")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
