"""配置：sync_config.ini 解析、库清单解析、库名校验。

新增配置项要同时改三处：本文件、sync_config.ini.example、README 第 4 节的表格。
"""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import asdict, dataclass, field

from .errors import ConfigError

# 这些库一律不参与同步，出现在清单里直接拒绝，防止误 DROP 目标实例的权限表
SYSTEM_SCHEMAS = frozenset({"mysql", "information_schema", "performance_schema", "sys"})
SAFE_NAME_RE = re.compile(r"^[\w$\-. ]+$", re.UNICODE)

MIN_BATCH_BYTES = 64 * 1024  # 再小就没有批量意义，且低于协议层常用的下限
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


@dataclass
class Endpoint:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    # repr=False：日志里会 %r 打配置快照，密码不能跟着漏出去
    password: str = field(default="", repr=False)
    socket: str = ""
    charset: str = "utf8mb4"

    @property
    def display(self) -> str:
        """唯一允许出现在日志里的端点表示，不含密码。"""
        where = f"unix:{self.socket}" if self.socket else f"{self.host}:{self.port}"
        return f"{self.user}@{where}"


@dataclass
class Options:
    workers: int = 4
    table_workers: int = 4
    batch_rows: int = 1000
    batch_bytes: int = 4 * 1024 * 1024
    drop_existing: bool = True
    include_views: bool = True
    include_triggers: bool = True
    include_routines: bool = True
    include_events: bool = True
    verify: bool = False
    sql_mode: str = ""
    net_timeout: int = 3600

    def snapshot(self) -> str:
        """DEBUG 用的参数快照。含密码的字段都在 Endpoint 上，这里全是行为参数。"""
        return ", ".join(f"{k}={v}" for k, v in sorted(asdict(self).items()))


@dataclass
class Settings:
    source: Endpoint
    target: Endpoint
    options: Options
    config_path: str = ""

    def snapshot(self) -> str:
        return (f"config={self.config_path} source={self.source.display} "
                f"target={self.target.display} {self.options.snapshot()}")


# ---------------------------------------------------------------- 解析


def _raw(cp: configparser.RawConfigParser, section: str, key: str) -> str:
    return cp.get(section, key, fallback="").strip()


def _int(cp, section, key, default, lo=None, hi=None) -> int:
    val = _raw(cp, section, key)
    if not val:
        return default
    try:
        num = int(val)
    except ValueError:
        raise ConfigError(f"[{section}] {key} 必须是整数，当前 {val!r}") from None
    _require_range(section, key, num, lo, hi)
    return num


def _require_range(section, key, num, lo, hi) -> None:
    if lo is not None and num < lo:
        raise ConfigError(f"[{section}] {key} 不能小于 {lo}，当前 {num}")
    if hi is not None and num > hi:
        raise ConfigError(f"[{section}] {key} 不能大于 {hi}，当前 {num}")


def _bool(cp, section, key, default) -> bool:
    val = _raw(cp, section, key)
    low = val.lower()
    if not val:
        return default
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise ConfigError(f"[{section}] {key} 只能是 true/false，当前 {val!r}")


def _endpoint(cp, section, env_prefix) -> Endpoint:
    host = _raw(cp, section, "host")
    socket = _raw(cp, section, "socket")
    if not host and not socket:
        # 整节缺失、或只写了 host = 空值都会走到这里。不能静默落到 127.0.0.1，
        # 那会让人以为连的是远端实例
        raise ConfigError(f"[{section}] 必须显式填写 host 或 socket，不能整节缺失或留空")
    return Endpoint(
        host=host or "127.0.0.1",
        port=_int(cp, section, "port", 3306, 1, 65535),
        user=_raw(cp, section, "user") or "root",
        # 环境变量优先，方便把密码挡在配置文件之外
        password=os.environ.get(f"{env_prefix}_PASSWORD") or _raw(cp, section, "password"),
        socket=socket,
        charset=_raw(cp, section, "charset") or "utf8mb4",
    )


def _options(cp) -> Options:
    sec = "sync"
    return Options(
        workers=_int(cp, sec, "workers", 4, 1),
        table_workers=_int(cp, sec, "table_workers", 4, 1),
        batch_rows=_int(cp, sec, "batch_rows", 1000, 1),
        batch_bytes=_int(cp, sec, "batch_bytes", 4 * 1024 * 1024, MIN_BATCH_BYTES),
        drop_existing=_bool(cp, sec, "drop_existing", True),
        include_views=_bool(cp, sec, "include_views", True),
        include_triggers=_bool(cp, sec, "include_triggers", True),
        include_routines=_bool(cp, sec, "include_routines", True),
        include_events=_bool(cp, sec, "include_events", True),
        verify=_bool(cp, sec, "verify", False),
        sql_mode=_raw(cp, sec, "sql_mode"),
        net_timeout=_int(cp, sec, "net_timeout", 3600, 1),
    )


def load_config(path: str):
    """兼容旧调用：返回 (source, target, options) 三元组。"""
    # 用 RawConfigParser：密码里出现 % 不能被当成插值语法
    cp = configparser.RawConfigParser()
    if not cp.read(path, encoding="utf-8"):
        # 相对路径会按当前工作目录解析，把实际试过的位置一起报出来，免得只看到文件名猜
        raise ConfigError(f"配置文件不存在或不可读：{path}（{os.path.abspath(path)}）")
    return _endpoint(cp, "source", "SYNC_SRC"), _endpoint(cp, "target", "SYNC_DST"), _options(cp)


def load_settings(path: str) -> Settings:
    src, dst, opts = load_config(path)
    return Settings(source=src, target=dst, options=opts, config_path=path)


def load_databases(path: str) -> list[str]:
    names: list[str] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip().strip('"').strip("'")
                if line and line not in names:  # 去重，保持清单原有顺序
                    names.append(line)
    except OSError as exc:
        raise ConfigError(f"库清单读取失败：{path}（{exc}）") from None
    return names


def validate_database_names(names: list[str]) -> list[str]:
    """非法字符与系统库都在这里挡掉。数据库名会被拼进标识符，不能指望转义兜住笔误。"""
    bad = [n for n in names if not SAFE_NAME_RE.match(n)]
    if bad:
        raise ConfigError(f"数据库名含非法字符：{bad}")
    blocked = [n for n in names if n.lower() in SYSTEM_SCHEMAS]
    if blocked:
        raise ConfigError(f"拒绝同步系统库：{blocked}")
    return names
