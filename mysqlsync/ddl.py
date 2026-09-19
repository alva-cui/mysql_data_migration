"""目标端 DDL 落盘。

调用顺序由 syncer 固定：建库 → USE 选中库 → 建表 → 传数据 → 建视图 → 触发器/例程/事件。
视图和例程提前创建会引用到尚不存在的对象。
"""

from __future__ import annotations

import logging

import pymysql

from . import db, metadata
from .results import DbResult, note_error

log = logging.getLogger(__name__)


def create_schema(cur, dbname, charset, collation, drop_existing: bool = True) -> None:
    if drop_existing:
        cur.execute(f"DROP DATABASE IF EXISTS {db.q(dbname)}")
    stmt = f"CREATE DATABASE IF NOT EXISTS {db.q(dbname)}"
    if charset:
        stmt += f" DEFAULT CHARACTER SET {charset}"
    if collation:
        stmt += f" COLLATE {collation}"
    cur.execute(stmt)


def select_schema(cur, dbname) -> None:
    """SHOW CREATE TABLE / PROCEDURE 的输出不带库名前缀，会话没选中库就执行
    拿来的 DDL 会整库报 1046 No database selected。"""
    cur.execute(f"USE {db.q(dbname)}")


def apply_ddl(cur, sql: str, label: str, res: DbResult) -> bool:
    try:
        cur.execute(sql)
    except pymysql.MySQLError as exc:
        note_error(res, log, f"{label} 创建失败：{exc}")
        return False
    log.debug("%s 已创建", label)
    return True


def create_tables(mcur, wcur, dbname, tables, res: DbResult) -> list:
    """逐表建结构，返回待传输清单 [(表名, 可写列)]。

    建表失败或读不到建表语句的表不进清单，也就不会被传输——宁可报缺失，不可静默少数据。"""
    items = []
    for table in tables:
        label = f"表 {dbname}.{table}"
        ddl = db.show_create(mcur, "SHOW CREATE TABLE", dbname, table)
        if not ddl:
            note_error(res, log, f"{table}: 源端读不到建表语句")
            continue
        if not apply_ddl(wcur, ddl, label, res):
            continue
        items.append((table, metadata.insertable_columns(mcur, dbname, table)))
    return items


def create_views(mcur, wcur, dbname, views, res: DbResult) -> int:
    created = 0
    for view in views:
        label = f"视图 {dbname}.{view}"
        ddl = db.show_create(mcur, "SHOW CREATE VIEW", dbname, view)
        if ddl and apply_ddl(wcur, db.strip_definer(ddl), label, res):
            created += 1
    return created


def create_triggers(mcur, wcur, dbname, names, res: DbResult) -> int:
    created = 0
    for name in names:
        label = f"触发器 {dbname}.{name}"
        ddl = db.show_create(mcur, "SHOW CREATE TRIGGER", dbname, name)
        if ddl and apply_ddl(wcur, db.strip_definer(ddl), label, res):
            created += 1
    return created


def create_routines(mcur, wcur, dbname, routines, res: DbResult) -> int:
    created = 0
    for name, kind in routines:
        ddl = db.show_create(mcur, metadata.routine_show_stmt(kind), dbname, name)
        if not ddl:
            note_error(res, log, f"{name}: 读不到定义，源账号缺 SHOW ROUTINE 权限或不是定义者")
            continue
        label = f"{'存储过程' if kind == 'PROCEDURE' else '函数'} {dbname}.{name}"
        if apply_ddl(wcur, db.strip_definer(ddl), label, res):
            created += 1
    return created


def create_events(mcur, wcur, dbname, names, res: DbResult) -> int:
    created = 0
    for name in names:
        label = f"事件 {dbname}.{name}"
        ddl = db.show_create(mcur, "SHOW CREATE EVENT", dbname, name)
        if ddl and apply_ddl(wcur, db.strip_definer(ddl), label, res):
            created += 1
    return created
