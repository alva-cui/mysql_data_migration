"""日志初始化与并发上下文标签。

约定：运行期输出一律走 logging，不再有 print。控制台与 --log-file 两个 handler 挂同一个
formatter，所以落盘的行一定带「日期 时间 级别 上下文」——旧版文件 handler 没挂 formatter，
落盘只剩消息正文，事后既不知道几点发生的、也分不清级别。

每条记录带一个上下文标签（main / precheck / 库名 / 库名/通道号），100 个库并行时
交错的上万行仍能按库、按通道拆开看。级别由 --log-level 控制，DEBUG 会把每条 SQL、
每个批次、每个会话参数打进日志。
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import logging.handlers
import os
import sys
import time
from typing import IO, Iterator, Optional

TEXT_FORMAT = "%(asctime)s %(levelname)-5s %(ctx)-16s %(message)s"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LEVEL = logging.INFO
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_BACKUPS = 5
LEVEL_NAMES = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")

# 上下文标签的固定取值；库名/通道号在 syncer 里拼
MAIN = "main"
PRECHECK = "precheck"
PLAN = "plan"
REPORT = "report"

_current_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("mysqlsync_ctx", default=MAIN)


class ContextFilter(logging.Filter):
    """给每条记录补上 ctx 字段，供 TEXT_FORMAT 使用。挂在 handler 上，覆盖所有 logger。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.ctx = _current_ctx.get()
        return True


@contextlib.contextmanager
def log_context(label: str) -> Iterator[None]:
    """让本段代码内的日志都带上 label。新线程不继承，需在线程入口自己设置。"""
    token = _current_ctx.set(label)
    try:
        yield
    finally:
        _current_ctx.reset(token)


def configure_std_streams() -> None:
    """Windows 控制台默认 GBK，不先切 UTF-8 的话 --help、日志和报错都会乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def parse_level(value) -> int:
    name = str(value).strip().upper()
    if name.isdigit():
        return int(name)
    if name not in LEVEL_NAMES:
        raise ValueError(f"日志级别只接受 {' / '.join(LEVEL_NAMES)} 或数字，当前 {value!r}")
    return logging.getLevelName(name)


def make_formatter(utc: bool = False) -> logging.Formatter:
    formatter = logging.Formatter(TEXT_FORMAT, TIME_FORMAT)
    if utc:
        # 只换时区基准，格式不变；日志里额外带 %z 偏移会把对齐拉长，故不加
        formatter.converter = time.gmtime
    return formatter


def setup_logging(level=DEFAULT_LEVEL, log_file: Optional[str] = None,
                  max_bytes: int = DEFAULT_MAX_BYTES, backups: int = DEFAULT_BACKUPS,
                  utc: bool = False, console: Optional[IO] = None) -> logging.Logger:
    """可重复调用：每次都先摘掉旧 handler，不会因为重复初始化而日志翻倍。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()  # StreamHandler.close() 只摘 handler，不会关掉 sys.stdout 本身

    formatter = make_formatter(utc)
    ctx_filter = ContextFilter()

    console_handler = logging.StreamHandler(console if console is not None else sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(ctx_filter)
    root.addHandler(console_handler)

    if log_file:
        parent = os.path.dirname(os.path.abspath(log_file))
        if parent:
            os.makedirs(parent, exist_ok=True)  # 让 --log-file logs/xxx.log 直接可用
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, mode="a", maxBytes=max_bytes, backupCount=backups,
            encoding="utf-8", delay=True,  # 写出第一行前不创建文件
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(ctx_filter)
        root.addHandler(file_handler)

    root.setLevel(parse_level(level))
    # 级别同时钉在每个 handler 上：selftest 为断言日志会临时抬高 root.level，
    # 不钉住就会把工具自己的输出一起刷到屏幕上
    for handler in root.handlers:
        handler.setLevel(root.level)
    # 第三方只跟 WARNING 以上；pymysql 在 DEBUG 下会把每条 SQL 重打一遍，和我们的
    # DEBUG 行重复
    logging.getLogger("pymysql").setLevel(max(root.level, logging.WARNING))
    return root
