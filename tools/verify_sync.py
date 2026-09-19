"""只读校验：逐表比对源/目标的结构、行数与全量内容哈希。不写任何数据。

    python tools/verify_sync.py ag1680
    python tools/verify_sync.py ag1680 -c sync_config.ini --log-file logs/verify.log

两端会话都设成同一个数字时区偏移，与同步程序的行为一致，这样 DATETIME/TIMESTAMP
的渲染才可比较。所有结论都走 logging，控制台与 --log-file 看到同一份。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
import time
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pymysql  # noqa: E402

from mysqlsync import db, metadata  # noqa: E402
from mysqlsync.config import load_settings, validate_database_names  # noqa: E402
from mysqlsync.errors import EXIT_FATAL, EXIT_OK, EXIT_SYNC_ISSUES, FatalError  # noqa: E402
from mysqlsync.logging_setup import (MAIN, PRECHECK, REPORT, configure_std_streams,  # noqa: E402
                                     log_context, setup_logging)

log = logging.getLogger("mysqlsync.verify")

MAX_DETAIL = 20  # 逐列/逐表明细的打印上限，超出的降到 DEBUG，避免一处系统性差异刷出几千行
AI_RE = re.compile(r" AUTO_INCREMENT=\d+")
TIME_TYPES = ("date", "datetime", "timestamp")


def log_findings(level: int, label: str, items) -> None:
    """明细行有上限地打出来，完整清单在 DEBUG。"""
    for item in items[:MAX_DETAIL]:
        log.log(level, "%s：%s", label, item)
    extra = len(items) - MAX_DETAIL
    if extra > 0:
        log.log(level, "%s：另有 %d 条未打印，完整清单用 --log-level DEBUG", label, extra)
        for item in items[MAX_DETAIL:]:
            log.debug("%s：%s", label, item)


def norm_ddl(text) -> str:
    # 只忽略自增计数器（与数据无关），其余一字不差必须相同
    return AI_RE.sub("", text or "").strip()


def load_cols(cur, dbname) -> dict:
    """返回 {表: {"all": 全部列, "ins": 可写列}}。

    只看 BASE TABLE：视图列混进来会让零日期/漂移扫描去查视图，触发 definer 权限报错。"""
    cur.execute(
        "SELECT C.TABLE_NAME, C.COLUMN_NAME, C.EXTRA FROM information_schema.COLUMNS C "
        "JOIN information_schema.TABLES T ON T.TABLE_SCHEMA = C.TABLE_SCHEMA "
        "  AND T.TABLE_NAME = C.TABLE_NAME AND T.TABLE_TYPE = 'BASE TABLE' "
        "WHERE C.TABLE_SCHEMA = %s ORDER BY C.TABLE_NAME, C.ORDINAL_POSITION", (dbname,))
    out = {}
    for table, name, extra in cur.fetchall():
        up = (extra or "").upper()
        skip = "GENERATED" in up and ("VIRTUAL" in up or "STORED" in up)
        entry = out.setdefault(table, {"all": [], "ins": []})
        entry["all"].append(name)
        if not skip:
            entry["ins"].append(name)
    return out


def canon(val) -> str:
    if isinstance(val, bytes):
        return val.hex()
    if isinstance(val, bool):
        return f"B{val}"
    return f"{type(val).__name__}:{val}"


def table_hash(cur, dbname, table, cols):
    """按全部列定序后逐行哈希：无主键表按首列排序会有并列行，顺序不定会造成假差异。"""
    h = hashlib.md5()
    order = ", ".join(map(db.q, cols))
    cur.execute(f"SELECT {order} FROM {db.qn(dbname, table)} ORDER BY {order}")
    n = 0
    while True:
        rows = cur.fetchmany(2000)
        if not rows:
            break
        for row in rows:
            h.update("|".join(map(canon, row)).encode("utf-8", "replace"))
            h.update(b"\n")
            n += 1
    return h.hexdigest(), n


# ---------------------------------------------------------------- 各类比对


def compare_object_lists(scur, dcur, dbname):
    """返回 (两端都有的表, 源端视图, 目标端视图, 问题)。表/视图清单复用 metadata 模块，
    非 BASE TABLE/VIEW 的对象会在那里一并告警。"""
    s_tab, s_view = metadata.list_tables(scur, dbname)
    d_tab, d_view = metadata.list_tables(dcur, dbname)
    log.info("对象清单：表 源 %d/目标 %d，视图 源 %d/目标 %d",
             len(s_tab), len(d_tab), len(s_view), len(d_view))
    problems = []
    for label, diff in (("缺失的表", set(s_tab) - set(d_tab)),
                        ("多出的表", set(d_tab) - set(s_tab)),
                        ("缺失的视图", set(s_view) - set(d_view)),
                        ("多出的视图", set(d_view) - set(s_view))):
        if diff:
            log_findings(logging.ERROR, label, sorted(diff))
            problems.append(f"{label} {len(diff)} 个")
    return sorted(set(s_tab) & set(d_tab)), sorted(s_view), sorted(d_view), problems


def compare_structure(scur, dcur, dbname, tables, s_cols, d_cols) -> list:
    problems = []
    ddl_diff, ai_diff, col_diff = [], [], []
    for table in tables:
        src_ddl = db.show_create(scur, "SHOW CREATE TABLE", dbname, table)
        dst_ddl = db.show_create(dcur, "SHOW CREATE TABLE", dbname, table)
        if norm_ddl(src_ddl) != norm_ddl(dst_ddl):
            ddl_diff.append(table)
        a1, a2 = _ai_value(src_ddl), _ai_value(dst_ddl)
        if (a1 is None) != (a2 is None) or (a1 and a2 and a1 > a2):
            ai_diff.append(f"{table} 源{a1}>目标{a2}" if a1 and a2 else table)
        if s_cols[table]["all"] != d_cols[table]["all"]:
            col_diff.append(table)
    log.info("结构：建表语句差异 %d，自增值异常 %d，列清单差异 %d",
             len(ddl_diff), len(ai_diff), len(col_diff))
    log_findings(logging.ERROR, "DDL 不一致", ddl_diff)
    log_findings(logging.WARNING, "自增值单边有值或源端更高", ai_diff)
    log_findings(logging.ERROR, "列清单不一致", col_diff)
    if ddl_diff:
        problems.append(f"DDL 不一致 {len(ddl_diff)} 个")
    if col_diff:
        problems.append(f"列清单不一致 {len(col_diff)} 个")
    return problems


def _ai_value(ddl):
    m = re.search(r"AUTO_INCREMENT=(\d+)", ddl or "")
    return int(m.group(1)) if m else None


def compare_data(scur, dcur, dbname, tables, s_cols, d_cols) -> list:
    bad_rows, bad_hash, total_rows = [], [], 0
    started = time.monotonic()
    for table in tables:
        cols = s_cols[table]["ins"]
        if not cols:
            log.debug("%s 无可写列（整表皆生成列），跳过内容比对", table)
            continue
        src_hash, src_n = table_hash(scur, dbname, table, cols)
        if d_cols[table]["ins"] != cols:
            bad_rows.append(f"{table}(目标列清单不同)")
            continue
        dst_hash, dst_n = table_hash(dcur, dbname, table, cols)
        total_rows += src_n
        if src_n != dst_n:
            bad_rows.append(f"{table} 源{src_n}/目标{dst_n}")
        elif src_hash != dst_hash:
            bad_hash.append(f"{table} 行数同但内容哈希不同 (n={src_n})")
    log.info("数据：比对 %d 表 / %s 行，耗时 %.0fs", len(tables), f"{total_rows:,}",
             time.monotonic() - started)
    log_findings(logging.ERROR, "行数不符", bad_rows)
    log_findings(logging.ERROR, "内容不符", bad_hash)
    if bad_rows:
        problems = [f"行数不符 {len(bad_rows)} 个"]
    else:
        problems = []
    if bad_hash:
        problems.append(f"内容不符 {len(bad_hash)} 个")
    return problems


def compare_views(scur, dcur, dbname, views, d_view) -> list:
    problems = []
    log.info("视图：%d 个", len(views))
    for view in views:
        s_def = _definer(scur, dbname, view)
        d_def = _definer(dcur, dbname, view) if view in d_view else "不存在"
        src_rows = _view_rows(scur, dbname, view)
        dst_rows = _view_rows(dcur, dbname, view)
        if src_rows == dst_rows == "ERR":
            log.info("%s：两端都不可查，源端既有问题、非同步引入（DEFINER=%s）", view, s_def)
        elif src_rows == "ERR":
            log.warning("%s：源端不可查但目标端可查（%s 行），DEFINER 剥离后视图反而可用",
                        view, dst_rows)
        elif dst_rows == "ERR":
            log.error("%s：目标端不可查，同步引入的问题", view)
            problems.append(f"视图 {view} 目标端不可查")
        elif src_rows != dst_rows:
            log.error("%s：行数不符 源%s/目标%s", view, src_rows, dst_rows)
            problems.append(f"视图 {view} 行数不符")
        else:
            log.info("%s：两端一致 %s 行", view, src_rows)
        # 目标端 DEFINER 落到执行账号才是预期，和源端相同说明没剥干净
        if d_def != "不存在" and d_def == s_def:
            log.error("%s：目标端 DEFINER 未被剥离，仍是源账号 %s", view, s_def)
            problems.append(f"视图 {view} DEFINER 未剥离")
    return problems


def _definer(cur, dbname, name):
    cur.execute("SELECT DEFINER FROM information_schema.VIEWS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (dbname, name))
    row = cur.fetchone()
    return f"{row[0]}" if row else "不存在"


def _view_rows(cur, dbname, name):
    """独立 try 包裹两端，否则源端视图本身是坏的就测不到目标端状态。"""
    try:
        cur.execute(f"SELECT COUNT(*) FROM {db.qn(dbname, name)}")
        return int(cur.fetchone()[0])
    except pymysql.MySQLError:
        return "ERR"


def check_zero_dates(scur, dcur, dbname, tables, s_cols, d_cols) -> list:
    """用 CAST(... AS CHAR) 比对，直接写 '0000-00-00' 字面量会在严格模式目标端报 1525。

    只比两端都存在的表，否则单边缺表会算出一堆无意义的差值。"""
    src = _count_zero_dates(scur, dbname, tables, s_cols)
    dst = _count_zero_dates(dcur, dbname, tables, d_cols)
    log.info("零日期值：%d 表参与比对，源 %d 目标 %d", len(tables), src, dst)
    if src == dst:
        return []
    log.error("零日期数量不一致：源 %d 目标 %d", src, dst)
    return ["零日期不一致"]


def _count_zero_dates(cur, dbname, tables, cols) -> int:
    total = 0
    for table in tables:
        if table not in cols:
            continue
        for col in _time_columns(cur, dbname, table):
            cur.execute(f"SELECT COUNT(*) FROM {db.qn(dbname, table)} WHERE "
                        f"CAST({db.q(col)} AS CHAR) LIKE '0000-00-00%'")
            total += int(cur.fetchone()[0] or 0)
    return total


def _time_columns(cur, dbname, table, types=TIME_TYPES) -> list:
    marks = ", ".join(["%s"] * len(types))
    cur.execute(f"SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                f"WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND DATA_TYPE IN ({marks})",
                (dbname, table, *types))
    return [r[0] for r in cur.fetchall()]


def check_time_drift(scur, dcur, dbname, tables) -> list:
    """比对每表时间列的 COUNT 与 SUM(UNIX_TIMESTAMP())，检测时区折算漂移。"""
    diffs = []
    checked = 0
    for table in tables:
        for col in _time_columns(scur, dbname, table):
            checked += 1
            sig = ("SELECT COUNT({0}), IFNULL(SUM(UNIX_TIMESTAMP({0})),0), "
                   "MIN(CAST({0} AS CHAR)), MAX(CAST({0} AS CHAR)) FROM {1}").format(
                db.q(col), db.qn(dbname, table))
            try:
                scur.execute(sig)
                a = scur.fetchone()
                dcur.execute(sig)
                b = dcur.fetchone()
            except pymysql.MySQLError as exc:
                diffs.append(f"{table}.{col} 查询异常 {exc}")
                continue
            if a != b:
                diffs.append(f"{table}.{col} 源{a} 目标{b}")
    log.info("TIMESTAMP/DATETIME epoch 比对：%d 个时间列，差异 %d 处", checked, len(diffs))
    log_findings(logging.ERROR, "时间列漂移", diffs)
    return [f"时间列漂移 {len(diffs)} 处"] if diffs else []


def check_charsets(scur, dcur, dbname, tables) -> list:
    """只比两端都存在的表，单边缺表由「缺失的表」那条报出来，不在这里重复。"""
    sql = ("SELECT TABLE_NAME, COLUMN_NAME, CHARACTER_SET_NAME, COLLATION_NAME "
           "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s "
           "AND CHARACTER_SET_NAME IS NOT NULL ORDER BY TABLE_NAME, ORDINAL_POSITION")
    scur.execute(sql, (dbname,))
    wanted = set(tables)
    src = {(r[0], r[1]): (r[2], r[3]) for r in scur.fetchall() if r[0] in wanted}
    dcur.execute(sql, (dbname,))
    dst = {(r[0], r[1]): (r[2], r[3]) for r in dcur.fetchall()}
    bad = [f"{t}.{c}: 源{val} 目标{dst.get((t, c))}" for (t, c), val in src.items()
           if dst.get((t, c)) != val]
    log.info("字符集：列级比对 %d 列，不一致 %d 处", len(src), len(bad))
    log_findings(logging.ERROR, "字符集不一致", bad)
    return [f"字符集不一致 {len(bad)} 处"] if bad else []


# ---------------------------------------------------------------- 入口


def build_parser():
    ap = argparse.ArgumentParser(
        prog="verify_sync.py",
        description="同步后的只读核验：结构、行数、内容哈希、视图可用性、时间列漂移、字符集",
    )
    ap.add_argument("database", help="要核验的库名")
    ap.add_argument("-c", "--config", default=os.path.join(ROOT, "sync_config.ini"),
                    help="配置文件，默认 %(default)s")
    ap.add_argument("--log-file", dest="log_file", metavar="FILE", help="日志同时写入该文件")
    ap.add_argument("--log-level", dest="log_level", default="INFO",
                    choices=("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"), type=str.upper,
                    help="日志级别，默认 INFO")
    return ap


def verify(args) -> int:
    settings = load_settings(args.config)
    dbname = validate_database_names([args.database])[0]
    src_conn = db.connect(settings.source, autocommit=True)
    dst_conn = db.connect(settings.target, autocommit=True)
    try:
        scur, dcur = src_conn.cursor(), dst_conn.cursor()
        # 两端都取源会话的数字偏移，和同步程序一致；偏移不同则 TIMESTAMP 必然假报警
        tz = db.tz_offset(scur)
        for cur in (scur, dcur):
            cur.execute("SET SESSION time_zone = %s", (tz,))
        with log_context(PRECHECK):
            log.info("比对库 %s｜源 %s｜目标 %s｜两端会话 time_zone=%s",
                     dbname, settings.source.display, settings.target.display, tz)

        with log_context(dbname):
            tables, src_views, dst_views, problems = compare_object_lists(scur, dcur, dbname)
            if not tables and not src_views:
                # 目标端连一张表都没有，后面的哈希/字符集比对只会产出几千行"目标None"，
                # 直接判定为没同步过并结束
                log.error("两端没有可比对的对象，该库大概率还没同步或库名写错，后续检查跳过")
                problems = problems or ["无可比对对象"]
            else:
                s_cols, d_cols = load_cols(scur, dbname), load_cols(dcur, dbname)
                problems += compare_structure(scur, dcur, dbname, tables, s_cols, d_cols)
                problems += compare_data(scur, dcur, dbname, tables, s_cols, d_cols)
                problems += compare_views(scur, dcur, dbname, src_views, set(dst_views))
                problems += check_zero_dates(scur, dcur, dbname, tables, s_cols, d_cols)
                problems += check_time_drift(scur, dcur, dbname, tables)
                problems += check_charsets(scur, dcur, dbname, tables)

        with log_context(REPORT):
            if problems:
                log.error("RESULT 库=%s 问题=%d 类：%s", dbname, len(problems), "; ".join(problems))
                return EXIT_SYNC_ISSUES
            log.info("RESULT 库=%s 问题=0 结构、行数、内容哈希、视图、时间列、字符集全部一致",
                     dbname)
            return EXIT_OK
    finally:
        src_conn.close()
        dst_conn.close()


def main(argv=None) -> int:
    configure_std_streams()
    args = build_parser().parse_args(argv)
    setup_logging(level=args.log_level, log_file=args.log_file)
    try:
        code = verify(args)
    except FatalError as exc:
        log.error("%s", exc)
        return EXIT_FATAL
    except (pymysql.MySQLError, OSError, ValueError) as exc:
        with log_context(MAIN):
            log.error("校验无法继续：%s", exc)
        return EXIT_FATAL
    with log_context(MAIN):
        log.info("进程结束，退出码 %d", code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
