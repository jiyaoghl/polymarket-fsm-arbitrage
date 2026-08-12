import logging
import sys
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from datetime import datetime

import paths


class ColoredFormatter(logging.Formatter):
    """
    彩色日志格式化器，便于在控制台区分不同级别的日志。
    """

    # ANSI 颜色代码
    COLORS = {
        'DEBUG': '\033[36m',      # 青色
        'INFO': '\033[32m',       # 绿色
        'WARNING': '\033[33m',    # 黄色
        'ERROR': '\033[31m',      # 红色
        'CRITICAL': '\033[35m',   # 紫色
        'RESET': '\033[0m',       # 重置
    }

    def __init__(self, fmt=None, datefmt=None, use_color=True):
        super().__init__(fmt, datefmt)
        self.use_color = use_color

    def format(self, record):
        log_color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        record.levelname = f"{log_color}{record.levelname:<8}{self.COLORS['RESET']}"
        return super().format(record)


def setup_logger(name="poly_bot", log_file: str | None = None, level=logging.INFO):
    """
    设置日志系统。

    Args:
        name: 日志名称
        log_file: 日志文件路径
        level: 日志级别

    Returns:
        配置好的 logger 实例
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    # 日志格式
    detailed_formatter = logging.Formatter(
        '%(asctime)s | %(name)s | %(levelname)s | '
        '%(filename)s:%(lineno)d | %(funcName)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    simple_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 控制台输出（彩色）
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(ColoredFormatter(
        '%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        datefmt='%H:%M:%S',
        use_color=True
    ))
    logger.addHandler(ch)

    if log_file is None:
        paths.logs_dir().mkdir(parents=True, exist_ok=True)
        log_file = str(paths.logs_dir() / "trade.log")

    # 详细日志文件（带轮转）
    detailed_log_path = log_file.replace('.log', '_detailed.log')
    dh = RotatingFileHandler(
        detailed_log_path,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    dh.setLevel(logging.DEBUG)
    dh.setFormatter(detailed_formatter)
    logger.addHandler(dh)

    # 主日志文件（按时间轮转）
    th = TimedRotatingFileHandler(
        log_file,
        when='D',
        interval=1,
        backupCount=7,
        encoding='utf-8'
    )
    th.setLevel(logging.INFO)
    th.setFormatter(simple_formatter)
    logger.addHandler(th)

    # 错误日志单独文件
    error_handler = RotatingFileHandler(
        log_file.replace('.log', '_error.log'),
        maxBytes=10*1024*1024,
        backupCount=3,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(detailed_formatter)
    logger.addHandler(error_handler)

    return logger


# 创建全局 logger 实例
logger = setup_logger()


def get_logger(name: str = None) -> logging.Logger:
    """
    获取子模块 logger。

    Args:
        name: 子模块名称

    Returns:
        子模块 logger 实例
    """
    if name:
        child_logger = logging.getLogger(f"poly_bot.{name}")
        child_logger.setLevel(logging.INFO)
        return child_logger
    return logger


def log_trade_event(logger_instance, event_type: str, **kwargs):
    """
    记录交易事件的辅助函数。

    Args:
        logger_instance: logger 实例
        event_type: 事件类型 (ORDER, FILL, CANCEL, ERROR 等)
        **kwargs: 事件详情
    """
    extra_data = " | ".join(f"{k}={v}" for k, v in kwargs.items())
    logger_instance.info(f"[{event_type}] {extra_data}")


def log_exception(logger_instance, message: str, exc: Exception = None):
    """
    记录异常信息的辅助函数。

    Args:
        logger_instance: logger 实例
        message: 错误消息
        exc: 异常对象（可选）
    """
    if exc:
        logger_instance.exception(f"{message}: {exc}")
    else:
        logger_instance.error(message, exc_info=True)

import logging
import sys
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from datetime import datetime

import paths


class ColoredFormatter(logging.Formatter):
    """
    彩色日志格式化器，便于在控制台区分不同级别的日志。
    """

    # ANSI 颜色代码
    COLORS = {
        "DEBUG": "\033[36m",      # 青色
        "INFO": "\033[32m",       # 绿色
        "WARNING": "\033[33m",    # 黄色
        "ERROR": "\033[31m",      # 红色
        "CRITICAL": "\033[35m",   # 紫色
        "RESET": "\033[0m",       # 重置
    }

    def __init__(self, fmt=None, datefmt=None, use_color=True):
        super().__init__(fmt, datefmt)
        self.use_color = use_color

    def format(self, record):
        log_color = self.COLORS.get(record.levelname, self.COLORS["RESET"])
        record.levelname = f"{log_color}{record.levelname:<8}{self.COLORS['RESET']}"
        return super().format(record)


def setup_logger(name="poly_bot", log_file: str | None = None, level=logging.INFO):
    """
    设置日志系统。

    Args:
        name: 日志名称
        log_file: 日志文件路径
        level: 日志级别

    Returns:
        配置好的 logger 实例
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    # 日志格式
    detailed_formatter = logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)s | "
        "%(filename)s:%(lineno)d | %(funcName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    simple_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台输出（彩色）
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(
        ColoredFormatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
            use_color=True,
        )
    )
    logger.addHandler(ch)

    if log_file is None:
        paths.logs_dir().mkdir(parents=True, exist_ok=True)
        log_file = str(paths.logs_dir() / "trade.log")

    # 详细日志文件（带轮转）
    detailed_log_path = log_file.replace(".log", "_detailed.log")
    dh = RotatingFileHandler(
        detailed_log_path,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8",
    )
    dh.setLevel(logging.DEBUG)
    dh.setFormatter(detailed_formatter)
    logger.addHandler(dh)

    # 主日志文件（按时间轮转）
    th = TimedRotatingFileHandler(
        log_file,
        when="D",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    th.setLevel(logging.INFO)
    th.setFormatter(simple_formatter)
    logger.addHandler(th)

    # 错误日志单独文件
    error_handler = RotatingFileHandler(
        log_file.replace(".log", "_error.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(detailed_formatter)
    logger.addHandler(error_handler)

    return logger


# 创建全局 logger 实例
logger = setup_logger()


def get_logger(name: str = None) -> logging.Logger:
    """
    获取子模块 logger。

    Args:
        name: 子模块名称

    Returns:
        子模块 logger 实例
    """
    if name:
        child_logger = logging.getLogger(f"poly_bot.{name}")
        child_logger.setLevel(logging.INFO)
        return child_logger
    return logger


def log_trade_event(logger_instance, event_type: str, **kwargs):
    """
    记录交易事件的辅助函数。

    Args:
        logger_instance: logger 实例
        event_type: 事件类型 (ORDER, FILL, CANCEL, ERROR 等)
        **kwargs: 事件详情
    """
    extra_data = " | ".join(f"{k}={v}" for k, v in kwargs.items())
    logger_instance.info(f"[{event_type}] {extra_data}")


def log_exception(logger_instance, message: str, exc: Exception = None):
    """
    记录异常信息的辅助函数。

    Args:
        logger_instance: logger 实例
        message: 错误消息
        exc: 异常对象（可选）
    """
    if exc:
        logger_instance.exception(f"{message}: {exc}")
    else:
        logger_instance.error(message, exc_info=True)

