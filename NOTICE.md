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
