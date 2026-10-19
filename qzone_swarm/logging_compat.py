"""日志兼容层 —— 专门躲开 AstrBot 日志装配里的一个坑。

背景（本插件踩过，导致插件加载直接失败）
----------------------------------------
AstrBot 的 ``LogManager.GetLogger("astrbot")`` 与 ``get_plugin_logger(name)``
会给**该 logger 自身**挂上：

    * ``_RecordEnricherFilter``：负责往 LogRecord 上写 ``plugin_tag``、
      ``short_levelname``、``astrbot_version_tag``、``source_file`` 等字段；
    * ``_LoguruInterceptHandler``（转发到 loguru）；
    * ``LogQueueHandler``：WebUI 日志页用的处理器，其 Formatter 的格式串里
      含 ``%(plugin_tag)s`` / ``%(ansi_prefix)s`` 等字段。

关键点：``_RecordEnricherFilter`` 是 **logger 级 filter**，而标准库 logging
只在「记录**产生于**的那个 logger」上执行 logger 级 filter
（``logging.Logger.handle`` → ``self.filter(record)``），
**不会**在冒泡经过祖先 logger 时再执行一遍。

于是只要用一个**子 logger** 打日志，例如 ``astrbot.plugin.<插件名>.creds``：
filter 不跑 → 记录一路冒到祖先 → 被需要 ``%(plugin_tag)s`` 的
``LogQueueHandler`` 格式化 → 抛

    ValueError: Formatting field not found in record: 'plugin_tag'

这个异常会**直接冒泡回调用处**（logging 只在 emit 内部兜底一部分错误），
把插件加载流程整个打挂 —— 表现就是插件页显示「加载失败」。

结论
----
本插件所有模块**必须共用 AstrBot 为它创建的那个 logger**，其名字恰好是
``astrbot.plugin.<插件名>``，**不能另起子 logger**。

另外这里还挂了一个兜底过滤器：即使将来 AstrBot 的装配方式变化、
enrich 过滤器不再执行，我们自己也会把格式串需要的字段补齐，
确保**日志永远不会再把业务打挂**。本模块不导入 astrbot。
"""

from __future__ import annotations

import logging
import os

PLUGIN_NAME = "astrbot_plugin_qzone_swarm"
"""须与 metadata.yaml 的 name 一致，AstrBot 的插件 logger 名由它决定。"""

PLUGIN_LOGGER_NAME = f"astrbot.plugin.{PLUGIN_NAME}"

_SHORT_LEVELS = {
    "DEBUG": "DBUG",
    "INFO": "INFO",
    "WARNING": "WARN",
    "ERROR": "ERRO",
    "CRITICAL": "CRIT",
}


def _short_level(level_name: str) -> str:
    return _SHORT_LEVELS.get(level_name, level_name[:4].upper())


def _source_file(pathname: str | None) -> str:
    if not pathname:
        return "unknown"
    return f"{os.path.basename(os.path.dirname(pathname))}." f"{os.path.basename(pathname).replace('.py', '')}"


class _FallbackEnricher(logging.Filter):
    """兜底补齐 AstrBot 日志格式串需要的字段（已存在则保持原值）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "plugin_tag", None):
            record.plugin_tag = f"[{PLUGIN_NAME}]"
        if not getattr(record, "short_levelname", None):
            record.short_levelname = _short_level(record.levelname)
        if getattr(record, "astrbot_version_tag", None) is None:
            record.astrbot_version_tag = ""
        if not getattr(record, "source_file", None):
            record.source_file = _source_file(record.pathname)
        if getattr(record, "source_line", None) is None:
            record.source_line = record.lineno
        if getattr(record, "ansi_prefix", None) is None:
            record.ansi_prefix = ""
        if getattr(record, "ansi_reset", None) is None:
            record.ansi_reset = ""
        return True


_installed = False


def get_logger(module_suffix: str = "") -> logging.Logger:
    """返回本插件共用的 logger（AstrBot 的插件 logger）。

    Args:
        module_suffix: 仅供调用处可读，**不会**拼进 logger 名字 ——
            一旦拼进去就变成子 logger，会触发上面描述的崩溃。
            模块归属由 AstrBot 格式串里的 ``source_file``（取自文件路径）体现。
    """
    global _installed
    logger = logging.getLogger(PLUGIN_LOGGER_NAME)
    if not _installed:
        _installed = True
        logger.addFilter(_FallbackEnricher())
    return logger


class BotLogger(logging.LoggerAdapter):
    """给某个机器人加前缀的日志适配器，保持 logger 名字不变。"""

    def process(self, msg, kwargs):  # noqa: ANN001, ANN201
        bot = (self.extra or {}).get("bot", "?")
        return f"[{bot}] {msg}", kwargs


def get_bot_logger(bot_name: str) -> logging.LoggerAdapter:
    """按机器人名取一个带前缀的日志器（同一个底层 logger）。"""
    return BotLogger(get_logger("runtime"), {"bot": bot_name})
