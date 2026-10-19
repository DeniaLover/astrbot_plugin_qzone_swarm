# 分发说明

这个包是 `astrbot_plugin_qzone_swarm` 的可分发版本，已经做过脱敏：

* 所有 QQ 号、群号、通知会话 ID 都换成了占位值（10000001 / 20000001 / 30000001 之类）；
* README 里的本机绝对路径换成了 `C:\AstrBot` 这类通用示例；
* **没有包含** `qzone_swarm/scenes.json`（1.6MB 的社区提示词库，版权归各法典作者，
  不允许二次分发）。需要的话在自己机器上跑一次：

```bash
python tools/build_scene_library.py
```

脚本会从上游拉取、过滤 R18/暴力/暗示内容并生成 `scenes.json`。
**没有这个文件插件也能正常跑** —— 只是"场景优先"会退化成让模型自由发挥画面。

* 只带了 `tests/selftest.py`（362 项离线断言，不依赖本机路径）。
  其余测试写死了本机 AstrBot 目录，属于联调工具，没有随包分发。

## 需要你填的

装好后在 AstrBot 插件配置里填：

* 每个机器人的 `platform_id`（要和 AstrBot 里的平台实例 id 一致）
* 人设 `persona_id`（在 AstrBot 人设库里选）
* 发说说 / 互动的时间点
* 聊天素材来源（群号 / 私聊号）
* 通知会话（可选）

自拍/绘画如果要启用，再填「自拍生图」那一栏（NovelAI / 豆包 / 自建 SD 都支持，
详见 README 对应章节）。

---

## 关于第三方内容

本仓库的 **MIT 许可证只覆盖代码本身**。

### 场景库（不随本仓库分发）

`qzone_swarm/scenes.json` 由 `tools/build_scene_library.py` 从
「法典图鉴」<https://novelai.quicktagcloud.com/> 生成，**不在本仓库中**。
该数据集的内容版权归各法典作者——凉夏之夜、叫我千藤就好了、Sol、渡鸦、
戒红所、拉风de茶、解构原典编撰组——所有，**请勿二次分发**，仅限本地自用。

### 角色外观描述

`qzone_swarm/prompts_default.json` 里的角色外观描述，是基于公开角色资料的
二次创作整理，角色版权归米哈游 / 库洛游戏所有。替换成你自己的角色时，
请注意同样的问题。
