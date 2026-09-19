"""SQL 文本与会话工具：标识符转义、连接、时区折算、SHOW CREATE 取列、DEFINER 剥离。"""

from __future__ import annotations

import re
from typing import Optional

import pymysql

from .config import Endpoint


def q(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def qn(db: str, obj: str) -> str:
    return q(db) + "." + q(obj)


def connect(ep: Endpoint, autocommit: bool):
    """建立连接。syncer/channel 一律通过 db.connect 调用，selftest 才能整体换成假连接。"""
    kw = dict(
        user=ep.user,
        password=ep.password,
        charset=ep.charset,
        autocommit=autocommit,
        connect_timeout=15,
        read_timeout=None,
        write_timeout=None,
    )
    if ep.socket:
        kw["unix_socket"] = ep.socket
    else:
        kw.update(host=ep.host, port=ep.port)
    return pymysql.connect(**kw)


def scalar(cur, sql, args=None):
    cur.execute(sql, args)
    row = cur.fetchone()
    return row[0] if row else None


def tz_offset(cur) -> str:
    """取源会话当前渲染 TIMESTAMP 所用的偏移。两端设成同一偏移后，源端读出的
    DATETIME 写回目标才会落到同一个 epoch，TIMESTAMP 列才不会随目标时区漂移。

    只能用数字偏移，不能用 Asia/Shanghai 这类命名时区——目标端通常没导入
    mysql.time_zone 表。"""
    sec = int(scalar(cur, "SELECT TIMESTAMPDIFF(SECOND, UTC_TIMESTAMP(), NOW())") or 0)
    sign = "-" if sec < 0 else "+"
    sec = abs(sec)
    return f"{sign}{sec // 3600:02d}:{sec % 3600 // 60:02d}"


DDL_COL_RE = re.compile(r"^Create ", re.I)


def show_create(cur, stmt, db, name) -> Optional[str]:
    """SHOW CREATE 各对象类型的结果列名不同，按列名定位而不是固定下标。"""
    cur.execute(f"{stmt} {qn(db, name)}")
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    if not row:
        return None
    for i, col in enumerate(cols):
        if DDL_COL_RE.match(col) or col == "SQL Original Statement":
            val = row[i]
            if val and val.strip():
                return val
    return None


# DEFINER 指向源实例的账号，目标端通常没有这个用户，必须剥掉让它落到 CURRENT_USER
DEFINER_RE = re.compile(
    r"DEFINER\s*=\s*(?:`[^`]*`|'[^']*'|\"[^\"]*\"|[\w$\-.%]+)"
    r"(?:\s*@\s*(?:`[^`]*`|'[^']*'|\"[^\"]*\"|[\w$\-.%]*))?\s*",
    re.IGNORECASE,
)


def strip_definer(sql: str) -> str:
    """只替换第一处 DEFINER=。只有开头那个子句是定义者声明；改成全量替换会连
    视图/例程定义体里出现的 DEFINER=xxx 字面量一起吃掉。SQL SECURITY DEFINER
    不含等号，不受影响，会原样保留。"""
    return DEFINER_RE.sub("", sql, count=1)


def row_bytes(row) -> int:
    return sum(len(c) if isinstance(c, (str, bytes)) else 20 for c in row)


def max_batch_bytes(cur, requested: int):
    """返回 (实际单批字节上限, 目标端 max_allowed_packet)。

    压到 60% 是刻意的一层折算，配置值直通会撞 MySQL server has gone away。"""
    packet = int(scalar(cur, "SELECT @@max_allowed_packet") or 4 * 1024 * 1024)
    return min(requested, int(packet * 0.6)), packet
