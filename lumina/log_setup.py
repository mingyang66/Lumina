"""统一日志：文件轮转 +（有控制台时）stderr 回显。

为什么要它：诊断信息原本散在 22 处 ``print`` 里。pythonw 启动时 stdout 是
``None``，这些输出直接进了黑洞；从终端启动时又和输入法之类第三方往 stderr
写的内容混在同一个文件里（实测根目录 ``debug.log`` 98 KB，没有一行是本项目的）。
改成 logging 之后，降级/崩溃信息落在 ``%LOCALAPPDATA%\\Lumina\\logs\\lumina.log``，
按大小轮转，弹窗里说不清的故障可以事后查。

约定：模块里用 ``logging.getLogger(__name__)``（即 ``lumina.xxx``），handler 挂在
``lumina`` 这个父 logger 上，靠传播收集，不需要每个模块各自配置。
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOGGER_NAME = "lumina"
FILE_NAME = "lumina.log"
MAX_BYTES = 1 << 20
BACKUP_COUNT = 3
FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
CONSOLE_FORMAT = "[lumina] %(message)s"

_configured = False
_log_path = None


def log_directory():
    """日志目录：优先 LOCALAPPDATA，与配置/数据库的落点保持一致。"""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".lumina")
    return os.path.join(base, "Lumina", "logs")


def log_path(path=None):
    if path:
        return os.path.abspath(path)
    return _log_path or os.path.join(log_directory(), FILE_NAME)


def configure(path=None, level=logging.INFO, console=None):
    """安装 handler（幂等）。返回日志文件路径。

    ``console=None`` 时按 ``sys.stderr`` 是否存在决定是否回显：pythonw 下没有
    stderr，硬挂 StreamHandler 会在每条日志上抛异常。
    """
    global _configured, _log_path
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    if _configured:
        return log_path(path)

    target = log_path(path)
    _log_path = target
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        handler = RotatingFileHandler(
            target, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
    except OSError:
        # 只读环境/权限不足时不能因为日志把应用拦下来。
        handler = None
    if handler is not None:
        handler.setFormatter(logging.Formatter(FILE_FORMAT))
        logger.addHandler(handler)

    if console is None:
        console = sys.stderr is not None
    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(CONSOLE_FORMAT))
        logger.addHandler(stream)

    _configured = True
    return target
