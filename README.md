# QQ空间多号矩阵 · astrbot_plugin_qzone_swarm

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.16-6f5cf2)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-aiocqhttp-00A98F)](metadata.yaml)

> 让**多个 QQ 号在同一套 AstrBot 里各自独立地**运营自己的 QQ 空间 ——
> 各自定时发说说、评论、回复、点赞，各自用自己的素材、模型与人设，**互不顶号、互不串上下文**。

**核心特性**

| | |
| --- | --- |
| 🧩 **多号矩阵** | 每号独立 Cookie / 独立 LLM 上下文 / 独立时间表，确定性错峰不撞车 |
| ✍️ **素材驱动** | 从各自指定的群聊/私聊抓聊天记录当素材；没素材时按人设行动轨迹生成 |
| 🎭 **人设一致** | 说说、评论、回复全部按人设口吻生成，跨号不串味 |
| 🖼️ **自拍配图** | NovelAI / 豆包 Seedream / 自建 SD，**多端点自动回退**，一张 6–11 秒 |
| 🎨 **画风可控** | 场景库选题、画师串按人设分流、可换装换发型 |
| 🛡️ **安全默认** | 默认演练模式，`dry_run_all` 关掉之前不会真的发 |

> 这是一个**全新独立**插件，与已有的 `astrbot_plugin_qzone_ultra` 没有任何代码或配置上的耦合，
> 也不会读写它的任何文件。两者可以同时安装。

---

## 1. 它是怎么做到"多号互不干扰"的

这是本插件与单账号空间插件的根本区别，也是整个设计围绕的核心。

| 维度 | 实现方式 |
| --- | --- |
| **账号身份** | 一个机器人 = 一个 AstrBot 平台实例（各自的 OneBot 连接）。 |
| **登录态** | 每号通过**自己那条连接**调 `get_credentials` 取 Cookie，天然只可能是自己的。**全程不存在任何全局共享 Cookie 或全局"当前机器人"状态。** |
| **Cookie 正确性** | `get_credentials` 优先于 `get_cookies`，配置域 `user.qzone.qq.com` 优先于兜底域。**命中即用，绝不跨调用合并 Cookie。**（QQ 每次请求都会轮换 `p_skey`，且不同域下发不同 `p_skey`；一旦合并就会算出错误的 `g_tk`，读接口可能侥幸能过、写接口必然失败。） |
| **对话上下文** | 每次 LLM 调用都是无状态的（`contexts=[]`）。不存在任何跨号共享的会话历史。 |
| **素材** | 聊天记录缓冲以 `platform_id` 分桶。A 号看到的群消息与 B 号看到的是两份独立数据。 |
| **状态文件** | 每号一个 `bot_<平台实例id>.json`，只读写自己的文件。 |
| **并发** | 每号一把 `asyncio.Lock`，同一号的动作串行，避免风控与状态错乱。 |
| **错峰** | 每个定时槽位带一个**确定性**随机偏移（由 平台id+日期+时间 哈希派生）。既保证多次轮询算出同一时刻（不会反复重掷），又天然把多个号错开。实测四个号 8 个时间点互不相同。 |
| **自我互动防护** | 互动时跳过 `hostuin == 自己的 uin` 的说说；回复自己的评论时跳过自己发的评论。 |

---

## 2. 说说内容从哪来

按你的要求实现为**两级来源**：

1. **优先用聊天素材**：从该号配置的 `source_groups` / `source_privates` 里，
   取最近 `material_window_minutes` 分钟内、最多 `material_max_messages` 条消息，
   交给模型按人设写成一条说说。
2. **素材不足时改走「人设行动轨迹」**：当可用消息少于 `min_material_messages`（默认 6 条）时，
   不硬凑素材，而是让模型代入人设，结合**当前的星期与时段**
   （如「现在是周三 21:30（晚上）」），想想这个角色此刻最可能在做什么、在想什么，
   据此原创一条日常说说。

**去重**：已用过的消息指纹会按号记录（内存 + 落盘），不会反复拿同一批聊天记录发说说。

**评论与回复**同样走人设：模型先判断"以我的人设看到这条会不会想说话"，
再决定是否点赞、写什么评论，并以 JSON 返回；回复自己说说下的留言同理。

---

## 3. 安装与启用

插件已安装到：

```
AstrBot\data\plugins\astrbot_plugin_qzone_swarm\
```

并已预置好配置文件：

```
AstrBot\data\config\astrbot_plugin_qzone_swarm_config.json
```

### ⚠️ 插件页面看不到它？需要触发一次「插件重载」

**AstrBot 只在启动时扫描一次插件目录**：插件列表接口读的是内存里的注册表
（`context.get_all_stars()`），刷新浏览器页面**不会**重新扫描磁盘，
也**没有**文件监听自动重载。

所以刚放进 `data/plugins/` 的新插件必须让 AstrBot 重新扫描一次：

* **推荐**：打开 AstrBot 插件管理页面，点「**重载插件**」（不指定插件名的那种，
  它会清空注册表并重新扫描目录）—— 插件立刻出现，**不用重启**。
* 或者：重启 AstrBot。

> 已核实这条路径可行：`PluginManager.reload(None)` → `load()` → `_get_plugin_modules()`
> → `_get_modules()` 用 `os.listdir` 重新扫描目录，认 `main.py` / `<目录名>.py`。
> 本站插件的目录结构、`metadata.yaml`、版本约束（`>=4.16,<5` 对 4.28.2 成立）
> 与依赖计划（`install_lines=()`，无需 pip）均已逐项验证通过。

下面是一个**四个号**的配置示例（换成你自己的平台实例与人设即可）：

| 机器人 | 平台实例 | QQ | 人设 | 发说说时间 | 互动时间 |
| --- | --- | --- | --- | --- | --- |
| 机器人A | `onebot_1` | 10000001 | 达妮娅 | 12:30、21:00 | 10:00、18:30 |
| 机器人B | `onebot_2` | 10000002 | 西格莉卡 | 11:00、19:30 | 09:30、16:00 |
| 机器人C | `onebot_3` | 10000003 | 风堇 | 10:00、20:30 | 13:00、22:00 |
| 机器人D | `onebot_4` | 10000004 | 莫宁 | 13:30、22:30 | 11:30、20:00 |

四个号的时间点经确定性偏移后**两两不撞车**（实测 8 个时刻互不相同）。
素材来源四者相同：群 `20000001` + 私聊 `30000001`。

> 重灌 `bots` 列表时注意：字段必须以 `_conf_schema.json` 的 bot 模板为准，
> 且每条都要带 `__template_key: "bot"`，否则 WebUI 认不出该用哪个模板渲染。

### ⚠️ 首次启用请先注意：默认是演练模式

配置里 `dry_run_all = true`。这意味着：

* 插件会正常运行、按点触发、抓素材、调模型生成内容、写日志、发通知；
* **但不会真的发布到 QQ 空间，也不会真的点赞/评论。**

这是故意的 —— 让你先确认四个号生成的内容质量与口味，再放开真实发布。

**确认没问题后**，把插件配置里的「全局演练模式」(`dry_run_all`) 关掉即可真实发布。
也可以在 WebUI 里只给某一个号关掉 `dry_run` 单独试水。

启用步骤：
1. 在 AstrBot 插件页确认 `astrbot_plugin_qzone_swarm` 已加载（首次可能需要重载插件）。
2. 用 `/qzswarm status` 确认四个号的登录态都拿到了。
3. 用 `/qzswarm preview 机器人A` 看看生成效果。
4. 满意后关闭 `dry_run_all`。

---

## 4. 命令

全部为管理员命令。

```
/qzswarm status [机器人]        查看运行状态（登录态、时间表、今日配额、下次触发）
/qzswarm bind <机器人>          重新获取该号 QQ空间登录态
/qzswarm preview <机器人>       只生成一条说说看看会发什么（不发布）
/qzswarm post <机器人> [dry]    立即发一条说说；带 dry 则只生成不发布
/qzswarm interact <机器人> [dry] 立即跑一轮互动（回复/点赞/评论）
/qzswarm enable|disable <机器人> 本次运行内启停某个号
/qzswarm personas              列出 AstrBot 里可用的人设名
/qzswarm sources [机器人]       查看素材来源与当前缓冲条数
/qzswarm reload                重新载入插件配置
```

`<机器人>` 可以填机器人名称（`机器人A`）、平台实例 id（`onebot_1`）或人设名。

---

## 5. 配置项说明（WebUI 可视化编辑）

配置分四块，机器人列表用 AstrBot 的「模板列表」，可以自由增删号：

* **机器人列表**：每号一份。核心字段
  * `platform_id` 必填，必须与 AstrBot 平台实例 id 完全一致
  * `persona_id` 选 AstrBot 人设；也可改用 `persona_file`（角色卡文件）或 `persona_prompt`（内联）
  * `provider_id` 该号用哪个模型（留空用全局默认；支持 `提供商id/模型名`）
  * `post_times` / `interact_times` 多个 `HH:MM`
  * `source_groups` / `source_privates` 素材来源
  * `like_max_per_day` / `comment_max_per_day` / `reply_max_per_day` / `post_max_per_day` 每日配额
  * `dry_run` 单独给某个号开演练
  * `manual_cookie` **兜底**，仅当协议端不支持自动取凭据时才填；必须是**这个号自己**的 Cookie
* **自拍生图**：见下节
* **高级设置**：轮询间隔、宽限期、HTTP 超时、UA

**宽限期**（`post_grace_minutes`）值得说明：如果程序停机错过了某个时间点，
超过宽限期就不再补发，避免重启后一次性连发几条。

---

## 6. 自拍生图

接口已经实现，**支持多端点回退**；默认关闭，配好绘画模型后打开即可。

### 分工：模型想画面，提示词库管一致性

```
模型  ──→ 这一段只写「画面内容」：在哪、做什么、什么姿势、表情、周围有什么
          （明确禁止它写发色/瞳色/衣服/画风 —— 写了也会被忽略）

提示词库 ──→ 补上「必须稳定」的部分：
            LoRA 触发词 + 人设外观锚点 + 画面 + `<lora:名:权重>` +
            构图 + 时段光线 + 画风 + 质量词（自动去重）
```

这样同一个角色的自拍**前后一致**，不会被模型每次随口改掉发色瞳色；
换模型或换 API 时也不用重调提示词。

### 提示词库在哪、怎么改

`qzone_swarm/prompts_default.json`（插件自带）：

| 字段 | 作用 |
| --- | --- |
| `quality` | 质量词（masterpiece…） |
| `negative` | 负向词（坏手、水印、低清…） |
| `style` | 画风，多条随机挑一条 |
| `composition` | 构图，多条随机挑一条 |
| `lighting` | 按**时段**取光线词（早晨/上午/中午/下午/晚上/深夜） |
| `persona.<人设>` | 该人设的 `appearance` 外观锚点、`lora`、`lora_weight`、`trigger` |

想改就改这个文件，或在**同目录**放一份 `prompts_local.json` 覆盖（升级不会冲掉），
或在配置里用 `prompt_library` 指向自己的库文件。改完重载插件生效。

### 参考图（立绘 / 三视图）—— 解决「不太像」

**纯文字提示词只能还原发色、瞳色这类显性特征**，脸型、服装剪裁、配饰位置很难说准。
给模型参考图是目前最有效的办法。

用法**零配置**：把图片放进对应人设的文件夹即可。

```
AstrBot/data/plugin_data/astrbot_plugin_qzone_swarm/ref/
├── 达妮娅/      ← 机器人A 的立绘放这里
├── 西格莉卡/    ← 机器人B 的
├── 风堇/        ← 机器人C 的
└── 莫宁/        ← 机器人D 的
```

* 支持 `png` / `jpg` / `jpeg` / `webp`
* 按**文件名排序**取前 `reference_max` 张（默认 4），建议命名：
  `01_立绘.png`、`02_三视图.png`、`03_表情.png`
* 插件会自动把图片**压到最长边 1024 并重编码 JPEG**，再以 base64 传给模型
  （2400×3600 的图压完约 250 KB，避免请求体过大）
* 目录为空时自动跳过，等同于纯文字提示词

配置项：`use_reference`（默认开）、`reference_max`（默认 4）。

> 实测：给一张参考图 + 「保持长相一致，改成雨夜街道撑伞」，出图能稳定保持
> 发色、瞳色、蕾丝帽、黑色蝴蝶结、蓝色泪滴宝石与束腰裙，只换场景。
> 豆包 Seedream 走的是请求体的 `image` 字段（字符串数组，元素为 base64 data URI）；
> `generic` 模式可在 `request_template` 里用 `{reference_images}` 占位。

**本机已备齐四个角色的官方美术**（共 13 张 / 25.3 MB）：

| 角色 | 来源 | 文件 |
| --- | --- | --- |
| 达妮娅 | [鸣潮 Wiki](https://wutheringwaves.fandom.com/wiki/Denia) | 立绘 2048×2099 / 全身 1186×1844 / 角色卡 720×1280 |
| 西格莉卡 | [鸣潮 Wiki](https://wutheringwaves.fandom.com/wiki/Sigrika) | 立绘 2048×1848 / 全身 1748×2400 / 角色卡 1080×1920 |
| 风堇 | [星铁 Wiki](https://honkai-star-rail.fandom.com/wiki/Hyacine) | 立绘 2048×2048 / 全身 850×1510 / 头像 160×160 / 小伊卡 256×256 |
| 莫宁 | [鸣潮 Wiki](https://wutheringwaves.fandom.com/wiki/Mornye) | 立绘 2048×1624 / 全身 1086×1547 / 角色卡 1080×1920 |

> 版权归米哈游 / 库洛游戏所有，仅供本地 AI 生图的角色参考，不再分发。
> 官方**不发布**角色三视图；能拿到的最佳参考是 `Full Sprite`（全身正视站立图），
> 它对生图的价值与三视图相当 —— 模型需要的是"服装的完整正立面"。
>
> 抓取要点（`static.wikia.nocookie.net` 会 **403**，必须带这两个头）：
> `User-Agent: Mozilla/5.0 …` + `Referer: https://<wiki>.fandom.com/`。
> CDN 实际返回的是 **webp**，需要自行转 PNG（插件加载时会再压成 JPEG）。

**⚠️ 豆包的版权审核不是必然触发**：拿官方立绘当参考时，豆包有时返回
`OutputImageSensitiveContentDetected.PolicyViolation`（判定输出涉版权），有时又能过。
被拒时会自动回退到 Gemini 端点（Gemini 不拦）。实测同一批素材：达妮娅过了、莫宁被拦。
**不要试图绕过这个审核** —— 那是刻意规避版权保护。真要在意还原度，正路是本地 SD + LoRA。

### ⚠️ LoRA 与云端绘画 API 是互斥的

**LoRA 只有 SD 类模型能加载。** 豆包 Seedream、Gemini 图像模型都是闭源模型，
**不接受 LoRA** —— 你没法让豆包加载 `wuthering_waves_denia_il`。

所以两条路线的角色一致性来源不同：

| 路线 | 角色怎么像本人 | 需要本地 SD | `lora_enabled` |
| --- | --- | --- | --- |
| 本地 SD / 支持 LoRA 的 SDXL 后端 | LoRA（最像，权重可调） | 要 | `true` |
| 云端 API（豆包 / Gemini） | 提示词库的**外观锚点**（描述文字） | 不要 | **必须 `false`** |

`lora_enabled` 默认 `true`。**用云端 API 时务必关掉** —— 否则提示词里会带
`<lora:xxx:0.8>`，闭源模型会把它当普通文字去画，出图必然是怪物。
关掉后 `<lora:>` 标签和触发词都不再注入，但外观锚点照常保留。

### 四个号的 LoRA 现状（已实地核对过模型文件）

已在本机的两个：

| 机器人 | 人设 | LoRA | 触发词 |
| --- | --- | --- | --- |
| 机器人A | 达妮娅 | ✅ `wuthering_waves_denia_il` (SDXL dim32) | `denia\(wuthering waves\)` |
| 机器人C | 风堇 | ✅ `honkai_star_rail_hyacine_il` (SDXL dim48) | `hyacine (honkai: star rail)` |

**网上有、本机还没下的两个**（都已选定二次元向的 Illustrious 版本，
与默认底模 `novaAnimeXL_ilV190` 同架构）：

| 机器人 | 人设 | 推荐 LoRA | 大小 | Civitai |
| --- | --- | --- | --- | --- |
| 机器人B | 西格莉卡 | `wuthering_waves_sigrika_il`<br>触发词 `sigrika \\(wuthering waves\\)` | 20.6 MB | [2428772](https://civitai.com/models/2428772) |
| 机器人D | 莫宁 | `wuthering_waves_mornye_il`<br>触发词 `mornye \\(wuthering waves\\)` | 40.9 MB | [2199574](https://civitai.com/models/2199574) |

> 这两个的元数据以 `recommended_lora` / `recommended_trigger` / `recommended_base` /
> `download` 存在提示词库里。**下载后把文件改名成上面的名字放进 `models/Lora`，
> 再把 `recommended_lora` / `recommended_trigger` 挪到 `lora` / `trigger` 即可。**
> `lora` 故意留空 —— 填了但文件不存在会让 SD 报错。
>
> 注意 Civitai 下载需要登录/API token（元数据可免认证读，文件不行）。
> 另外那两款 **Anima 底模**的（2635429 / 2408824）不要下，架构不兼容。

每个号也可以用 `selfie_lora` / `selfie_lora_weight` / `selfie_lora_trigger`
**单独覆盖**库里的设置（留空 = 用库里的）。

### 出图比例：默认 3:4 竖图

`size` 默认 `864x1152`（3:4），`aspect_ratio` 默认 `3:4`。要 9:16 就填 `720x1280`。

### 场景库（法典图鉴提炼，`scene_first`）

让自拍不再总是"图书馆窗边"。从[法典图鉴](https://novelai.quicktagcloud.com/)提炼了
**13212 条**人写好的场景/构图/服装提示词，带中文标题与例图分类。

**流程是「先定图、再写文」**：

```
① 从场景库挑一条（如《红砖老巷里仰望繁花》）
② 让模型围绕这个画面写说说  → 「本想顺路记录旧砖墙的受力磨损，抬头却被巷子
                                上方盛开的花打断了计算……」
③ 用该条的场景标签当画面，接上角色名 + 画风 + 质量词
④ 生图 → 发布
```

好处是**图文天然一致**，不会出现"文字说在看书、图里却在打伞"。

**画风目标：半厚涂日系插画**

`style` 与 `quality` 已按这个方向调好：

| 字段 | 内容 |
| --- | --- |
| `style` | `soft painterly anime illustration, luminous soft shading, clean delicate lineart, high-key bright lighting, gentle rim light, glossy highlights, detailed sparkling eyes, delicate pastel palette with vivid accents, translucent light effects, atmospheric glow, dreamy ambience, polished finish` |
| `quality` | `very aesthetic, absurdres, masterpiece, best quality, highly detailed` |
| `negative` 追加 | `flat shading, dull colors, muddy colors, sketch, rough lines, harsh shadows` —— 把平涂/灰调/草图感压掉 |

要点：**柔和过渡 + 清晰线稿**（不是纯平涂赛璐璐）、**明亮通透带轮廓光**、
**大眼高光**、**通透材质与氛围元素**（气泡/花瓣/光点）、粉蓝紫为主的柔和饱和。

**画师串（改画风）**：从法典的「编纂者常用画师组」提炼，**保留 NAI5 / NAI4.5 时期**的 8 组组合串
（另有 382 组单画师词典条目，小概率参与），
存在 `scenes.json` 的 `styles` 里。这是改画风最有效的手段 ——
NovelAI 的 `artist:xxx` 权重串会把画风直接拉向特定插画师。

配置项 `selfie.artist_style`：

| 值 | 含义 |
| --- | --- |
| `random`（默认） | 每次随机一组，并避开最近用过的 6 组 |
| `off` | 不注入画师串（只靠基础画风词） |
| 具体标题（如 `NAI5时期`） | 固定用那一组，支持部分匹配 |

实测同一角色同一场景换画师串，渲染风格差异明显（锐利线稿 / 柔和厚涂 / 平涂赛璐璐）。

> 画师串同样**整段不可拆**（里面是 `0.6::mignon,artist:xxx,...::` 这种权重组），
> 代码里作为不可分割单元追加在提示词末尾。

**换装 / 换发型**：人设外观拆成三段 ——

| 段 | 内容 | 是否可变 |
| --- | --- | --- |
| `identity` | 发色、瞳色、脸、身体特征（机械义肢、声痕） | **永不改变** |
| `hair` | 发型与头饰 | 可换 |
| `outfit` | 服装与配饰 | 可换 |

配置 `selfie.variation`（默认 `0.35`）：约三分之一的概率换一套衣服或换个发型。
服装从「叫我千藤就好了の衣柜」「更衣人偶」两个法典里挑，发型从内置的 7 种里挑。

> 因为 `identity` 段永远保留，换了衣服也认得出是她。
> 实测：达妮娅换成紫色针织衫+百褶裙站在雪松林回眸；莫宁换成「垮掉一代」造型
> （黑高领+贝雷帽+墨镜）走在实验室走廊 —— 两人都还一眼认得出。

**不再局限于自拍**：提示词措辞已从「你正准备拍一张自拍」放宽为
「你要发一张自己出镜的照片」，并明确说明**不必是自拍** —— 可以是别人帮拍、
随手抓拍，或只是恰好出现在画面里。基础画风词也去掉了 `selfie angle`，
改成中性的 `anime illustration, clean lineart, soft shading`。

**特写自动换画布**：`close-up` / `headshot` / `face focus` 类场景会自动把画布
从 `832x1088` 换成 **`832x832` 方图** —— 因为 NAI 在纵版画布上画特写极易切成
上下多格/重复画面（实测 `split screen` / `multiple views` 等负向词压不住），
换方图后就正常了。判断在 `PromptLibrary.shot_size()`。

**重建场景库**（站方更新后重跑即可，脚本会钉死 release 保证可复现）：

```bash
python tools/build_scene_library.py            # 走默认代理 127.0.0.1:7897
python tools/build_scene_library.py --no-proxy # 海外网络
python tools/build_scene_library.py --dry-run  # 只统计不落盘
```

**收录范围**：构图风格、叫我千藤的衣柜、更衣人偶、渡鸦的构图鉴、所长 N4.5/N5 常规法典、解构原典。
**不收**：三个 R18 法典、画师词典、社区图包。

**四层过滤**（缺一层就会出问题，都是实测踩出来的）：

| 层 | 过滤内容 | 效果 |
| --- | --- | --- |
| 1 | 三个 R18 法典 | 整本剔除 |
| 2 | 露骨词表 + 分类黑名单 | 去掉肤色/内衣类分类 |
| 3 | **暴力词表**（gun/blood/gore…） | 去掉「枪判恶魔」这类 |
| 4 | **暗示词表**（cleavage/middle finger…） | 去掉「胸间玩偶」这类 |

再加选题时的**题材过滤**：排除 `1boy` / `furry` / `mecha` 等（库里这类很多，
跟四个女角色放一起会画出怪东西），优先 `1girl`。

> ⚠️ **特写类场景被排除**（`close-up` / `closeup` / `headshot`）：
> NAI 在纵版画布上画特写极易切成上下多格/重复画面，实测
> `split screen` / `multiple views` 等负向词**压不住**，
> 索性从选题池里去掉，交给半身/全身场景。
>
> 另有一条经验：**特写场景不要给完整服装描述**。250 字的服装词会把
> `close-up on face` 压成蹲姿全身 —— 代码里已做成"特写时不注入服装段"。

**版权**：场景库内容版权归各法典作者（凉夏之夜、叫我千藤就好了、Sol、渡鸦、
戒红所、拉风de茶、解构原典编撰组）所有，本机仅作生图参考，**不要再分发**。
场景库文件 `qzone_swarm/scenes.json` 里保留了署名与来源。

### 人体结构错误（三腿/多手）

**真实踩过**：风堇坐秋千那张生成了**三条腿**。原因不是画师串，
而是「坐在秋千上 + 交叉腿」这种高难点姿势，NAI 容易画崩。

处理：

1. **负向词已补强**（`extra legs, three legs, multiple legs, fused legs,
   malformed limbs, disconnected limbs, missing limbs, extra arms, three arms,
   wrong anatomy, extra feet, extra hands`）。实测换成站姿后连出三张全部正常。
2. **姿势越复杂越容易崩** —— 坐姿+交叉腿、多人互动、大幅动态pose 是高风险区；
   站姿、半身、中景最稳。
3. **自动复检已实现但默认关闭**：代码里有 `anatomy_check` + `anatomy_retries`，
   会用视觉模型看一张图判断要不要重画。**但本机现有 key 全都不支持图像输入** ——
   方舟套餐端点对视觉模型返回 `404 UnsupportedModel`，`/api/v3` 返回 `401`，
   本地中转返回空内容或乱码。所以默认关掉，避免白等一轮。
   等你配了可用的视觉模型再打开即可。

### 提示词约束（NAI_Prompt_Constraint_v2）

仓库里随包带了一份 **NAI 提示词架构规范**：`qzone_swarm/NAI_Prompt_Constraint_v2.md`
（9 大模块 + NAI 专属语法 + 负面词规则）。

**它不是整份塞进 system prompt 的** —— 那份规范是给「一次性输出整条 NAI 提示词」
的场景用的，而本插件的分工是「模型只写画面内容，外观/画风/镜头/质量词由
`PromptLibrary` 统一拼装」。直接整份注入会和这个分工冲突（规范要求模型输出
Danbooru 标签串和镜头参数，插件恰恰不让它写这些）。

所以只**取其真正影响出片质量的那一条**：模块 5「画面具体内容」里的
**拒绝站桩**规则，提炼成一段约束块注入「让模型想画面」这一步：

> 必须有明确动作与姿态张力；必须与环境有物理互动（风吹起什么、踩到水花、
> 手里的东西发光、衣角被带起）；必须有动态视觉元素（花瓣、尘埃、水汽、光斑）。
> 构图要让人物和环境产生关系，而不是站在一片空白里。

配置项 `selfie.use_prompt_constraint`（默认开）控制这个块。

同时把规范的**通用基底负面词**并进了 `prompts_default.json` 的 `negative`
（`jpeg artifacts` / `unfinished` / `artistic error` / `scan` / `extra digits` 等）。

### 每张说说都会变（构图 / 画师串 / 服装 / 发型）

连续发说说最怕"永远一个样"。插件会把**用过的选择记进状态文件**，下一张主动避开：

| 维度 | 行为 |
| --- | --- |
| **构图** | **强制换**。场景库只有约 11% 的条目自带镜头说明，其余不写 —— 所以场景没写镜头时会主动注入构图池里的一条，并**避开上一张的镜头类型**（full / medium 交替） |
| **画师串** | **强制换**。避开最近用过的标题，按人设过滤后轮换 |
| **服装** | `variation` 概率换（默认 **0.85**），且避开最近几条穿过的 |
| **发型** | 同上，避开最近几条用过的 |

「最近几条」的窗口由 `no_repeat_window` 控制（默认 **2** = 不和上一条、上上条撞）。
调大更不容易重复，但可选项会被快速抽干；调到 1 就只保证不连续重复。

> 发色瞳色属于 `identity` 固定段，**换装换发型永远不改** —— 无论怎么换都认得出是她。

### 画师串要按人设挑（重要）

有些画师串会把角色**画成熟** —— 实测同一份提示词，风堇用 `NAI5时期` 出来偏成熟、
用 `NAI5时期：2` 才像她本人。原因是前者含 `sos adult` 这类偏成熟向的画师。

所以每个人的画风是**分开配**的，三管齐下：

| 手段 | 字段 | 作用 |
| --- | --- | --- |
| 体型锚点（正向） | `body` | `petite, youthful face, soft rounded features` 之类，压住成熟倾向 |
| 画师串避用 | `style_avoid` | 含这些关键词的画师串不给她用（如 `sos adult`） |
| 专属负向 | `negative_extra` | `mature female, adult woman, voluptuous, tall` |

当前配置：

| 角色 | 体型锚点 | 画师串 |
| --- | --- | --- |
| 达妮娅 | `petite, slender, young woman, soft youthful features` | 只用 `NAI5时期：2` |
| 风堇 | `petite, youthful face, soft rounded features, gentle, slender` | 只用 `NAI5时期：2` |
| 西格莉卡（15 岁） | `teenage girl, petite, youthful face, energetic, slim` | 只用 `NAI5时期：2`，负向更严 |
| 莫宁 | `young adult woman, composed, slender` | 全部可用（成年角色） |

> 想给某个角色换画风，就改她的 `style_avoid`；
> 想整体固定某组，把 `selfie.artist_style` 填成那组的标题。

### 当前在用：NovelAI（`mode: novelai`）

**这是目前效果最好的一条路** —— NAI 本身就是二次元模型，而且**自带 Danbooru 角色知识库**：
只要把角色名（如 `denia (wuthering waves)`）写进提示词，就能直接画出正确的服装与配饰，
**不需要 LoRA、也不需要参考图**。四个角色实测全部对得上。

```jsonc
{
  "mode": "novelai",
  "api_base": "https://image.novelai.net",
  "api_key": "pst-……",
  "model": "nai-diffusion-5-full",   // 或 nai-diffusion-5-curated
  "size": "832x1088",
  "timeout": 300,
  "proxy": "http://127.0.0.1:7897",  // 国内直连必超时，必须走代理
  "lora_enabled": false,             // 无本地 LoRA 时不注入 <lora:>
  "use_trigger": true                // 但要保留角色名 ← 关键
}
```

踩过的坑（全部实测确认）：

| 现象 | 真实原因 |
| --- | --- |
| `500 Internal Server Error` | **`v4_prompt` / `v4_negative_prompt` 缺一个就 500**（不是 400） |
| `500`（请求体完整时） | **宽高必须是 64 的倍数**，`864` 非法；`832×1088` / `896×1152` 可以 |
| `400 model must be a valid enum value` | 打到了老域名 `api.novelai.net`，生成必须用 `image.novelai.net` |
| `400 Please refresh NovelAI.net…` | 查余额也要用 `image.novelai.net/user/subscription` |
| 直连 `ConnectTimeout` | 国内被墙，必须配 `proxy` |
| 返回不是 JSON | **是 ZIP**，内含 `image_0.png`（插件已自动解包） |

**提示词要写进三个地方**（官方结构要求，只写 `input` 不行）：
顶层 `input`、`parameters.prompt`、`v4_prompt.caption.base_caption`。

**免费带**：像素 ≤ 1,048,576、步数 ≤ 28、`n_samples: 1`、不用底图 → **0 Anlas**
（Opus 实测约 10 张后 `usage.percent` 从 100 掉到 99，Anlas 余额 `fixedTrainingStepsLeft` 没动）。
插件会把尺寸自动对齐到 64 的倍数并压进免费带。

**速度**：本机实测 **6–11 秒/张**（对比豆包 55–110 秒）。

> ## ⚠️ 角色名（`use_trigger`）与 LoRA（`lora_enabled`）是两个独立开关
>
> 以前两者绑在一起（关 LoRA 就把触发词也删了），这是错的：
> * **NAI**：角色名是「角色 tag」，**必须留**（实测带与不带的差别很大 ——
>   不带时服装会走样，带了才是官方那套）。
> * **豆包 / Gemini**：反而**建议关掉**角色名，写上去更容易撞版权审核。
> * **本地 SD**：两个都要（角色名 + `<lora:...>`）。

### 已验证可用的一套配置（豆包 Seedream 套餐端点）

本机实测跑通的配置，可直接照抄（`extra_params` 用来关掉右下角「AI生成」水印）：

| 字段 | 值 |
| --- | --- |
| `enabled` | `true` |
| `api_base` | `https://ark.cn-beijing.volces.com/api/**plan**/v3` |
| `model` | `doubao-seedream-5.0-pro` |
| `size` / `aspect_ratio` | `864x1152` / `3:4` |
| `response_path` | `data.0.url` |
| `extra_params` | `{"watermark": false}` |
| `lora_enabled` | `false` |
| `timeout` | `300` |

> ⚠️ **`/api/plan/v3` 与 `/api/v3` 不通用**：套餐（agent plan）的 key 在 `/api/v3` 上
> 一律返回 `401 AuthenticationError`，必须走 `/api/plan/v3`。反过来，`/api/plan/v3`
> 上只有 `doubao-seedream-5.0-pro` 这类套餐模型可用，`doubao-seedream-4-0-*` 会报
> `404 UnsupportedModel`。填之前先确认自己的 key 属于哪一种。
>
> 实测一张 864×1152 约 **55–110 秒**，所以 `timeout` 给到 300 比较安全
> （原来 120 秒会卡在边缘，偶尔超时丢图）。

### 端点回退链

主端点失败（网络/额度/**地区限制**/HTTP 错误）会按顺序尝试 `fallback_providers`，
任一成功即用，日志里会显示「生图由第 N 个端点成功」。与对话模型那边一致。

> 本机实测：Gemini 走 `127.0.0.1:8045` 中转时一直返回
> `400 User location is not supported for the API use` —— 中转本身活着，
> 但它的**出口地区不被 Gemini 支持**，需要在中转侧换节点才能用。
> 所以目前实际是豆包单点在扛，没有真正的兜底。

### 想配 Gemini / 豆包 的绘图 key，选哪个模型？

| 方案 | `api_base` | `model` | `mode` | `response_path` |
| --- | --- | --- | --- | --- |
| **豆包 Seedream**（推荐入门） | `https://ark.cn-beijing.volces.com/api/v3` | `doubao-seedream-4-0-250828` 或 `doubao-seedream-4-5-251128` | `openai_images` | `data.0.url`（默认回 URL，插件会自动下载） |
| **Gemini**（走中转） | 你中转的 `/v1` 地址（如 `http://127.0.0.1:8045/v1`） | `gemini-3-pro-image`（Nano Banana Pro，最贵最强）或 `gemini-2.5-flash-image`（Nano Banana，便宜快） | `openai_images` | `data.0.b64_json` |
| **Gemini 原生 API** | `https://generativelanguage.googleapis.com/v1beta/models/<模型>:generateContent` | — | `generic` + `request_template` | 视响应而定 |

> 豆包的 model id 以 [Volcengine Skill 文档](https://raw.githubusercontent.com/Lychee-AI-Team/Volcengine-Skill/main/volcengine-api/SKILL.md) 为准；
> Gemini 的图像模型名见 [Gemini 图像生成文档](https://ai.google.dev/gemini-api/docs/nanobanana)。
> 两边都要先在各自控制台开通并拿到 key。

**建议**：先用**豆包 Seedream** 当主端点（国内直连、不依赖代理、便宜），
把 Gemini 放 `fallback_providers` 当备选。

### 启用三步

1. 配置「自拍生图」里填 `api_base` / `model` / `api_key`，`enabled` 打开；
   需要备选就在 `fallback_providers` 里加一条（模板列表，可加多个）。
2. 给对应机器人的 `allow_selfie` 打开。
3. 该号发说说时就会：想画面 → 拼提示词 → 生图（带回退）→ 上传 QQ 空间 → 配文发布。

失败会**安静降级**为纯文字发布，不影响主流程。

---

## 7. 代码结构

```
astrbot_plugin_qzone_swarm/
├── main.py                  # 插件入口：命令、调度循环、聊天记录被动采集（依赖 astrbot）
├── metadata.yaml
├── _conf_schema.json        # WebUI 配置定义
├── requirements.txt
├── qzone_swarm/
│   ├── util.py              # 小工具：宽松 JSON、时间、文本清洗
│   ├── models.py            # 数据结构（凭据/聊天记录/说说/机器人配置）
│   ├── creds.py             # OneBot → QQ空间 Cookie/g_tk（多号隔离的核心）
│   ├── client.py            # QQ空间 HTTP 客户端（发/读/评/回/赞/传图/删）
│   ├── sources.py           # 聊天记录缓冲与素材取样（按平台实例分桶）
│   ├── compose.py           # 提示词构造与输出清洗
│   ├── selfie.py            # 绘画模型接口（多端点回退链）
│   ├── promptlib.py         # 提示词库：外观锚点 / LoRA / 画风 / 光线 组装
│   ├── prompts_default.json # 提示词库数据（可编辑；同目录 prompts_local.json 覆盖）
│   ├── store.py             # 每号独立持久化（配额/槽位/去重）
│   ├── scheduler.py         # 多时间点定时（纯函数，确定性错峰）
│   ├── config.py            # 配置解析与钳制
│   ├── logging_compat.py    # 日志兼容层（避开 plugin_tag 崩溃坑，见下）
│   ├── bridge.py            # AstrBot 适配层（平台/模型/人设/通知）
│   └── runtime.py           # 单号运行时：隔离契约的落点
└── tests/
    ├── selftest.py               # 离线自检（不联网，362 项）
    ├── load_check.py             # 在真实 AstrBot 包环境下导入插件，验证装饰器/注册（23 项）
    ├── runtime_smoke.py          # 真实配置 + 真实日志装配 + 模拟 Context，跑通插件自身逻辑（26 项）
    ├── logging_fix_check.py      # 真实日志装配下验证不会踩 plugin_tag 崩溃（16 项）
    ├── live_probe.py             # 联网**只读**自检（不发布任何内容）
    ├── write_path_probe.py       # 写入路径鉴权探针（用不存在的目标，无副作用）
    ├── live_llm_content_test.py  # 联网内容生成验证：真实模型 + 真实人设（会调用模型）
    └── live_publish_test.py      # ⚠️ 真实发布→读回→删除的端到端验证（需 --confirm）
```

`qzone_swarm/` 里**全部模块都不导入 astrbot**（连 `bridge.py` 也只在对 `notify()`
里做了函数内延迟导入），因此可以脱离框架做单元测试。框架耦合只集中在 `main.py`。

### ⚠️ 给后来者：千万不要另起子 logger

这是本插件**实际踩过并导致插件加载失败**的坑，改代码时务必留意。

AstrBot 的 `LogManager.GetLogger("astrbot")` / `get_plugin_logger(name)` 会给
**该 logger 自身**挂上 `_RecordEnricherFilter`（负责往 LogRecord 写 `plugin_tag`、
`short_levelname`、`ansi_prefix` 等字段）以及 `LogQueueHandler`
（WebUI 日志页用，其 Formatter 格式串里含 `%(plugin_tag)s`）。

而 `_RecordEnricherFilter` 是 **logger 级 filter**，标准库只在
「记录**产生于**的那个 logger」上执行 logger 级 filter，
**不会**在冒泡经过祖先时再跑一遍。

于是只要用一个**子 logger** 写日志（例如 `astrbot.plugin.<插件名>.creds`），
就会：filter 不跑 → 记录冒泡到祖先 → 被需要 `%(plugin_tag)s` 的
`LogQueueHandler` 格式化 → 抛

```
ValueError: Formatting field not found in record: 'plugin_tag'
```

这个异常**会直接冒泡回调用处**，把插件加载整个打挂 —— 插件页显示「加载失败」，
错误信息就只有这一句，很难猜到是日志的问题。

**所以**：所有模块必须共用 `qzone_swarm/logging_compat.py` 的 `get_logger()`
（它返回的正是 `astrbot.plugin.astrbot_plugin_qzone_swarm` 这个**确切**名字的
logger，不拼子名）。需要区分模块时靠 AstrBot 格式串里的 `source_file`
（取自文件路径，会显示成 `qzone_swarm.config:302`）；需要区分机器人时用
`get_bot_logger("机器人A")`，它在**消息**里加前缀而不改 logger 名字。

`logging_compat.py` 还额外挂了一个兜底过滤器，自己补齐格式串需要的字段，
这样即使将来 AstrBot 的装配方式变了，**日志也不会再把业务打挂**。
`tests/logging_fix_check.py` 会在真实日志装配下守住这条线。

---

## 8. 如何自检

用 AstrBot 自带的 Python 运行（路径按实际安装调整）：

```powershell
$py = "C:\AstrBot\backend\python\python.exe"
$d  = "C:\AstrBot\data\plugins\astrbot_plugin_qzone_swarm"

# 1) 离线自检：不联网、不碰真实 QQ（362 项）
& $py "$d\tests\selftest.py"

# 2) 加载自检：在真实 AstrBot 环境下导入插件，验证装饰器与 handler 注册（23 项）
& $py "$d\tests\load_check.py"

# 3) 运行时冒烟：真实配置 + 模拟 Context，跑通插件自身逻辑（26 项）
& $py "$d\tests\runtime_smoke.py"

# 4) 日志装配验证：真实 AstrBot 日志环境下不会踩 plugin_tag 崩溃（16 项）
& $py "$d\tests\logging_fix_check.py"

# 5) 联网只读自检：验证凭据获取 + g_tk 算法 + 读取说说（不发布/不评论/不点赞）
& $py "$d\tests\live_probe.py"

# 6) 联网写入路径探针：用不存在的目标验证写接口鉴权（不创建任何内容）
& $py "$d\tests\write_path_probe.py"

# 7) 联网内容生成验证：真实模型 + 真实人设，跑通写说说/评论决策（会真实调用模型）
& $py "$d\tests\live_llm_content_test.py"

# 8) ⚠️ 真实发布→读回→删除（会短暂在真实空间出现，需显式 --confirm）
& $py "$d\tests\live_publish_test.py" --uin 10000003 --confirm
```

> 控制台是 GBK 时中文会显示成乱码。加 `$env:PYTHONIOENCODING='utf-8'` 或重定向到文件查看。
> `tests/live_probe.py` 里的端口与 token 是**用于直连 SnowLuma HTTP API 做对照**的；
> 插件本体并不走这条路径（它走 AstrBot 的平台适配器）。

---

## 9. 已知边界与踩坑记录

> 这一节是开发中踩过的坑，保留下来免得后来人重踩。不含任何具体账号信息。

**怎么自测**

* 离线：`python tests/selftest.py`（不联网、不碰真实 QQ，全部为纯函数与外设断言）。
* 联网只读：截图/发说说前先用 `/qzswarm preview <机器人>` 看生成效果，不产生任何内容。
* 真发：`tests/live_publish_test.py --uin <QQ号> --confirm` 会走「发布 → 读回确认 → 删除 → 再读回」的闭环，
  但**会在空间留下短暂内容**，务必带 `--confirm` 手动触发。

**踩过的坑**

* **好友动态接口不返回 JSON**，而是 JS 对象字面量（键没引号、单引号字符串、`\x3C` 转义、可能套 `_Callback(...)`）。
  直接 `json.loads` 会失败，导致「点赞/评论别人的说说」整条路不通。现由 `util.parse_js_literal()` 处理。
* **好友动态条目结构与 `msglist` 完全不同**：fid 在 `key`、作者在 `uin`/`opuin`、时间在 `abstime`，
  正文 `title`/`summary` 恒为空，内容只混在 `html` 渲染页里。
  因此采用「**动态列表负责发现、详情接口负责内容**」，对方设了权限时保留兜底正文并继续。
* **好友动态天然为空是正常的**：它返回当前账号能看到的好友动态；新号没有好友，列表就是空的。
* **点赞接口对不存在的话题也返回 `succ`**，不能靠返回值判断是否真的生效。
* **评论决策依赖模型回 JSON**，而思维模型的 reasoning token 会吃掉 `max_tokens`，
  回复常在字符串中间被截断。插件用 `util.extract_json_object()` 做稳健解析
  （去代码块包裹、从夹叙文字里提取内层 JSON、对截断的 JSON 补尾修复）。
* **AstrBot 的 provider `key` 可能是列表**（多密钥轮换），直接拼进 `Authorization`
  会得到 `Bearer ['xxx']` 而 401。取列表首项。
* **AstrBot 保存插件配置时写带 BOM 的 UTF-8**。插件本体不受影响（配置由 AstrBot 解析后传入），
  但自己写的脚本读配置要用 `encoding="utf-8-sig"`。
* **插件会先于平台适配器加载**：`initialize()` 时 `platform_manager.platform_insts` 可能是空的，
  这是正常现象。运行期每轮都会重新取平台实例，平台上线后自动开始工作。
* **平台暂不可用时不会消耗定时时间点**：调度每轮先确认该号的 OneBot 连接可用，
  不可用就整轮跳过、槽位保持未执行，等平台起来后在宽限期内照常触发。
* **协议端可能轮换 HTTP `accessToken` 并写回自己的配置**，所以联网测试里不要硬编码 token，
  运行时从协议端配置文件现读，否则过一阵会莫名 401，容易误判成账号坏了。
  插件本体不受影响：它拿凭据走 AstrBot 的 OneBot 连接，不是直连 HTTP 端口。
* **模型提供商需要兜底链**：按「该号 `provider_id` → 插件默认 → 会话默认 →
  AstrBot 的 `fallback_provider_ids`」依次尝试。只调默认提供商、没有兜底的话，
  主提供商一挂整轮就全灭。
* **`is_usable_post()` 不能用高频词做子串黑名单**：`无法`、`作为一个`、`抱歉`
  在正常中文里太常见，会把好端端的说说整条毙掉。现在只拦「模型推脱 / 自报 AI 身份」
  与「把提示词当正文抄回来」两类，原则是宁可放过不可错杀。
* **运行中的插件会在内存里持有状态副本**：每次动作后 `save()` 会把整个 `store.data`
  写回文件，所以外部脚本直接改状态文件会被后续保存覆盖。帖子本身在 QQ 上是真实的，
  只是本地账本会丢。
* 插件**没有实现视频说说**；图片说说只在自拍功能启用后才会用到。
* **聊天记录只保存在内存**（每会话上限 300 条），插件重启后素材缓冲会清空；
  已使用过的消息指纹会落盘，因此不会重复发同样的内容。
