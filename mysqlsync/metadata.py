"""源端元数据读取。

清单全部来自 information_schema，而 information_schema 只显示当前账号**有权看到**的对象。
权限给漏时会静默少对象而不是报错，所以这里读到的数量要和目标端建起来的数量并排进报告。
"""

from __future__ import annotations

import logging

from .config import SAFE_NAME_RE
from .errors import FatalError

log = logging.getLogger(__name__)


def existing_schemas(cur) -> set:
    cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA")
    return {row[0] for row in cur.fetchall()}


def schema_charset(cur, dbname):
    cur.execute(
        "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
        "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s",
        (dbname,),
    )
    row = cur.fetchone()
    if not row:
        # 消息里带库名：这条会进 res.errors，汇总报告会脱离上下文列单独打印它
        raise FatalError(f"源实例中不存在数据库 {dbname}")
    charset, collation = row[0], row[1]
    for val in (charset, collation):
        if val and not SAFE_NAME_RE.match(val):
            raise FatalError(f"非法字符集/排序规则名：{val!r}")
    return charset, collation


def list_tables(cur, dbname):
    cur.execute(
        "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
        (dbname,),
    )
    rows = cur.fetchall()
    tables = [r[0] for r in rows if r[1] == "BASE TABLE"]
    views = [r[0] for r in rows if r[1] == "VIEW"]
    others = [r[0] for r in rows if r[1] not in ("BASE TABLE", "VIEW")]
    if others:
        log.warning("跳过 %d 个非表对象：%s", len(others), ", ".join(others[:5]))
    return tables, views


def insertable_columns(cur, dbname, table) -> list:
    """生成列（VIRTUAL/STORED）不可显式写入，必须同时从 SELECT 和 INSERT 列表排除。
    带表达式默认值的列其 EXTRA 是 DEFAULT_GENERATED，不含 VIRTUAL/STORED，属可写列。"""
    cur.execute(
        "SELECT COLUMN_NAME, EXTRA FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
        (dbname, table),
    )
    cols = []
    for name, extra in cur.fetchall():
        up = (extra or "").upper()
        if "GENERATED" in up and ("VIRTUAL" in up or "STORED" in up):
            continue
        cols.append(name)
    return cols


def list_triggers(cur, dbname) -> list:
    cur.execute(
        "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
        "WHERE TRIGGER_SCHEMA = %s ORDER BY TRIGGER_NAME",
        (dbname,),
    )
    return [r[0] for r in cur.fetchall()]


def list_routines(cur, dbname) -> list:
    """返回 [(名称, PROCEDURE|FUNCTION)]。"""
    cur.execute(
        "SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES "
        "WHERE ROUTINE_SCHEMA = %s ORDER BY ROUTINE_NAME",
        (dbname,),
    )
    return [(r[0], r[1]) for r in cur.fetchall()]


def list_events(cur, dbname) -> list:
    cur.execute(
        "SELECT EVENT_NAME FROM information_schema.EVENTS "
        "WHERE EVENT_SCHEMA = %s ORDER BY EVENT_NAME",
        (dbname,),
    )
    return [r[0] for r in cur.fetchall()]


def routine_show_stmt(kind: str) -> str:
    return "SHOW CREATE PROCEDURE" if kind == "PROCEDURE" else "SHOW CREATE FUNCTION"
