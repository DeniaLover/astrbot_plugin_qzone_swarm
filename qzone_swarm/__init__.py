"""QQ空间多号矩阵（Qzone Swarm）。

本包内的模块 **不导入 astrbot**，以便离线单元测试。
所有与 AstrBot 的耦合都集中在 ``bridge.py`` / ``runtime.py`` / ``main.py``。

职责划分：
    - ``models``      数据结构
    - ``util``        通用小工具
    - ``creds``       OneBot -> QQ空间 Cookie/g_tk（每个机器人一份，互不共用）
    - ``client``      QQ空间 HTTP 客户端（发/读/评/回/赞）
    - ``sources``     聊天记录采集与素材取样
    - ``compose``     提示词构造与模型输出清洗
    - ``selfie``      绘画模型接口（当前为可配置占位实现）
    - ``store``       每号独立的持久化状态
    - ``scheduler``   多时间点定时（纯函数，便于测试）
"""

__version__ = "0.1.0"
