#!/usr/bin/env python3
"""跨 MySQL 实例整库同步：结构 + 数据，不依赖 mysqldump。

用法：
    pip install pymysql
    编辑 sync_config.ini（连接信息）与 databases.txt（库清单）
    python mysql_sync.py --dry-run     # 先看计划
    python mysql_sync.py --yes         # 正式同步

并行模型（两层）：
    workers        —— 同时同步几个库
    table_workers  —— 单个库内同时传几张表
    总并发传输 = workers x table_workers，峰值连接数在启动日志里会打印。

一致性语义：每条通道在自己的源连接上开一个一致性快照事务，通道内所有表读到的是
同一时刻的数据。table_workers=1 时单库全表同一快照；>1 时各通道快照独立，单库内
跨表不再严格同时，需在业务低峰或停写下执行才严谨。
"""

from __future__ import annotations

import argparse
import configparser
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

try:
    import pymysql
    from pymysql.cursors import SSCursor
except ImportError:
    sys.exit("缺少依赖，请先执行：pip install pymysql")

# Windows 控制台默认 GBK，不先切 UTF-8 的话 --help、日志和 sys.exit 的报错都会乱码
for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

# 这些库一律不参与同步，出现在清单里直接拒绝，防止误 DROP 目标实例的权限表
SYSTEM_SCHEMAS = {"mysql", "information_schema", "performance_schema", "sys"}
SAFE_NAME_RE = re.compile(r"^[\w$\-. ]+$", re.UNICODE)
# DEFINER 指向源实例的账号，目标端通常没有这个用户，必须剥掉让它落到 CURRENT_USER
DEFINER_RE = re.compile(
    r"DEFINER\s*=\s*(?:`[^`]*`|'[^']*'|\"[^\"]*\"|[\w$\-.%]+)"
    r"(?:\s*@\s*(?:`[^`]*`|'[^']*'|\"[^\"]*\"|[\w$\-.%]*))?\s*",
    re.IGNORECASE,
)
DDL_COL_RE = re.compile(r"^Create ", re.I)


def q(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def qn(db: str, obj: str) -> str:
    return q(db) + "." + q(obj)


@dataclass
class Endpoint:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    socket: str = ""
    charset: str = "utf8mb4"


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


@dataclass
class DbResult:
    name: str
    tables: int = 0
    views: int = 0
    rows: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 配置读取


def _int(cp, section, key, default):
    val = cp.get(section, key, fallback="").strip()
    return int(val) if val else default


def _bool(cp, section, key, default):
    val = cp.get(section, key, fallback="").strip()
    return val.lower() in ("1", "true", "yes", "on") if val else default


def _endpoint(cp, section, env_prefix):
    return Endpoint(
        host=cp.get(section, "host", fallback="127.0.0.1").strip(),
        port=_int(cp, section, "port", 3306),
        user=cp.get(section, "user", fallback="root").strip(),
        # 环境变量优先，方便把密码挡在配置文件之外
        password=os.environ.get(f"{env_prefix}_PASSWORD")
        or cp.get(section, "password", fallback="").strip(),
        socket=cp.get(section, "socket", fallback="").strip(),
        charset=cp.get(section, "charset", fallback="utf8mb4").strip(),
    )


def load_config(path):
    # 用 RawConfigParser：密码里出现 % 不能被当成插值语法
    cp = configparser.RawConfigParser()
    if not cp.read(path, encoding="utf-8"):
        sys.exit(f"配置文件不存在：{path}")
    sec = "sync"
    opts = Options(
        workers=max(1, _int(cp, sec, "workers", 4)),
        table_workers=max(1, _int(cp, sec, "table_workers", 4)),
        batch_rows=max(1, _int(cp, sec, "batch_rows", 1000)),
        batch_bytes=max(64 * 1024, _int(cp, sec, "batch_bytes", 4 * 1024 * 1024)),
        drop_existing=_bool(cp, sec, "drop_existing", True),
        include_views=_bool(cp, sec, "include_views", True),
        include_triggers=_bool(cp, sec, "include_triggers", True),
        include_routines=_bool(cp, sec, "include_routines", True),
        include_events=_bool(cp, sec, "include_events", True),
        verify=_bool(cp, sec, "verify", False),
        sql_mode=cp.get(sec, "sql_mode", fallback="").strip(),
        net_timeout=_int(cp, sec, "net_timeout", 3600),
    )
    return _endpoint(cp, "source", "SYNC_SRC"), _endpoint(cp, "target", "SYNC_DST"), opts


def load_databases(path):
    names = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip().strip('"').strip("'")
            if line and line not in names:
                names.append(line)
    return names


# ---------------------------------------------------------------- 连接与工具


def connect(ep: Endpoint, autocommit: bool):
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
    DATETIME 写回目标才会落到同一个 epoch，TIMESTAMP 列才不会随目标时区漂移。"""
    sec = int(scalar(cur, "SELECT TIMESTAMPDIFF(SECOND, UTC_TIMESTAMP(), NOW())") or 0)
    sign = "-" if sec < 0 else "+"
    sec = abs(sec)
    return f"{sign}{sec // 3600:02d}:{sec % 3600 // 60:02d}"


def show_create(cur, stmt, db, name):
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


def strip_definer(sql: str) -> str:
    return DEFINER_RE.sub("", sql, count=1)


def row_bytes(row) -> int:
    return sum(len(c) if isinstance(c, (str, bytes)) else 20 for c in row)


# ---------------------------------------------------------------- 传输通道


class Channel:
    """一对源/目标连接，自带一致性快照事务。一个库起 table_workers 条。"""

    def __init__(self, syncer: "Syncer", tz: str, batch_bytes: int):
        opts = self.opts = syncer.opts
        self.batch_bytes = batch_bytes
        self.src = connect(syncer.src, autocommit=False)
        self.dst = connect(syncer.dst, autocommit=False)

        scur = self.src.cursor()
        scur.execute("SET SESSION time_zone = %s", (tz,))
        # 源实例若配了全局 max_execution_time，会掐断大表的长 SELECT
        scur.execute("SET SESSION max_execution_time = 0")
        scur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")

        wcur = self.dst.cursor()
        wcur.execute("SET SESSION time_zone = %s", (tz,))
        wcur.execute("SET SESSION sql_mode = %s", (opts.sql_mode,))
        wcur.execute("SET SESSION foreign_key_checks = 0")
        wcur.execute("SET SESSION unique_checks = 0")
        wcur.execute("SET SESSION net_read_timeout = %s", (opts.net_timeout,))
        wcur.execute("SET SESSION net_write_timeout = %s", (opts.net_timeout,))

    def close(self):
        for conn in (self.src, self.dst):
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 收尾关不掉就算了
                pass

    def count(self, dbname, table, on_src=False) -> int:
        cur = self.src.cursor() if on_src else self.dst.cursor()
        return int(scalar(cur, f"SELECT COUNT(*) FROM {qn(dbname, table)}") or 0)

    def copy_table(self, dbname, table, cols) -> int:
        """流式读 + 分批写，不在内存里堆整表。返回写入行数。"""
        if not cols:
            return 0
        batch_rows = self.opts.batch_rows
        select_sql = f"SELECT {', '.join(map(q, cols))} FROM {qn(dbname, table)}"
        insert_sql = "INSERT INTO {} ({}) VALUES ({})".format(
            qn(dbname, table),
            ", ".join(map(q, cols)),
            ", ".join(["%s"] * len(cols)),
        )
        total = 0
        wcur = self.dst.cursor()
        scur = self.src.cursor(SSCursor)
        scur.execute(select_sql)
        buf, size = [], 0
        while True:
            rows = scur.fetchmany(batch_rows)
            if not rows:  # SSCursor 取到空列表即结果集耗尽
                break
            for row in rows:
                if buf and (len(buf) >= batch_rows or size + row_bytes(row) > self.batch_bytes):
                    wcur.executemany(insert_sql, buf)
                    self.dst.commit()
                    total += len(buf)
                    buf, size = [], 0
                buf.append(row)
                size += row_bytes(row)
        if buf:
            wcur.executemany(insert_sql, buf)
            self.dst.commit()
            total += len(buf)
        scur.close()
        return total


# ---------------------------------------------------------------- 同步主体


class Syncer:
    def __init__(self, src: Endpoint, dst: Endpoint, opts: Options):
        self.src, self.dst, self.opts = src, dst, opts
        self.log = logging.getLogger("sync")

    # ---- 元数据 ----

    def schema_meta(self, cur, dbname):
        cur.execute(
            "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
            "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s",
            (dbname,),
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError("源实例中不存在该数据库")
        cs, coll = row[0], row[1]
        for val in (cs, coll):
            if val and not SAFE_NAME_RE.match(val):
                raise RuntimeError(f"非法字符集/排序规则名：{val!r}")
        return cs, coll

    def list_tables(self, cur, dbname):
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
            self.log.warning("[%s] 跳过 %d 个非表对象：%s", dbname, len(others), ", ".join(others[:5]))
        return tables, views

    def insertable_columns(self, cur, dbname, table):
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

    # ---- DDL ----

    def create_schema(self, cur, dbname, charset, collation):
        if self.opts.drop_existing:
            cur.execute(f"DROP DATABASE IF EXISTS {q(dbname)}")
        stmt = f"CREATE DATABASE IF NOT EXISTS {q(dbname)}"
        if charset:
            stmt += f" DEFAULT CHARACTER SET {charset}"
        if collation:
            stmt += f" COLLATE {collation}"
        cur.execute(stmt)

    def run_ddl(self, wcur, sql, label, res, dbname) -> bool:
        try:
            wcur.execute(sql)
            return True
        except pymysql.MySQLError as exc:
            msg = f"{label} 创建失败：{exc}"
            res.errors.append(msg)
            self.log.error("[%s] %s", dbname, msg)
            return False

    # ---- 库内表级并行 ----

    def transfer_tables(self, dbname, items, res, tz, batch_bytes):
        """items 为 [(table, cols)]。起 min(table_workers, 表数) 条通道，每条一个线程，
        都从同一个待办队列取表，于是大表先落地、小表填空，天然负载均衡。"""
        width = max(1, min(self.opts.table_workers, len(items)))
        pending = deque(items)
        lock = threading.Lock()
        dead_channels = []

        def on_error(msg):
            with lock:
                res.errors.append(msg)
            self.log.error("[%s] %s", dbname, msg)

        def work(ch, table, cols):
            t0 = time.time()
            rows = ch.copy_table(dbname, table, cols)
            if self.opts.verify:
                src_n = ch.count(dbname, table, on_src=True)
                dst_n = ch.count(dbname, table)
                if src_n != dst_n:
                    on_error(f"{table}: 行数不符 源 {src_n} / 目标 {dst_n}")
            with lock:
                res.rows += rows
            self.log.info("[%s] %s  %s 行  %.1fs", dbname, table, f"{rows:,}", time.time() - t0)

        def loop():
            try:
                ch = Channel(self, tz, batch_bytes)
            except pymysql.MySQLError as exc:
                dead_channels.append(str(exc))
                return
            try:
                while True:
                    try:
                        table, cols = pending.popleft()
                    except IndexError:
                        break
                    try:
                        work(ch, table, cols)
                    except pymysql.MySQLError as exc:
                        on_error(f"{table}: 数据拷贝失败 {exc}")
                        ch.close()
                        # 连接可能已被服务端掐断，换一条干净通道继续跑剩下的表
                        try:
                            ch = Channel(self, tz, batch_bytes)
                        except pymysql.MySQLError as exc2:
                            on_error(f"通道重建失败，该库剩余表未同步：{exc2}")
                            pending.clear()
                            return
            finally:
                ch.close()

        threads = [threading.Thread(target=loop, daemon=True) for _ in range(width)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for err in dead_channels:
            on_error(f"传输通道建立失败，该库有表未同步：{err}")

    # ---- 单库同步 ----

    def sync_database(self, dbname) -> DbResult:
        res = DbResult(name=dbname)
        t0 = time.time()
        opts = self.opts
        meta = connect(self.src, autocommit=False)
        ddl = connect(self.dst, autocommit=False)
        try:
            mcur, wcur = meta.cursor(), ddl.cursor()
            tz = tz_offset(mcur)
            mp = int(scalar(wcur, "SELECT @@max_allowed_packet") or 4194304)
            batch_bytes = min(opts.batch_bytes, int(mp * 0.6))
            mcur.execute("SET SESSION max_execution_time = 0")

            charset, collation = self.schema_meta(mcur, dbname)
            tables, views = self.list_tables(mcur, dbname)
            self.log.info("[%s] 表 %d 视图 %d，开始建结构", dbname, len(tables), len(views))

            self.create_schema(wcur, dbname, charset, collation)
            ddl.commit()

            items = []
            for table in tables:
                table_ddl = show_create(mcur, "SHOW CREATE TABLE", dbname, table)
                if not table_ddl:
                    res.errors.append(f"{table}: 源端读不到建表语句")
                    continue
                if not self.run_ddl(wcur, table_ddl, f"表 {dbname}.{table}", res, dbname):
                    continue
                items.append((table, self.insertable_columns(mcur, dbname, table)))
            ddl.commit()
            res.tables = len(items)

            if items:
                self.transfer_tables(dbname, items, res, tz, batch_bytes)

            if opts.include_views and views:
                for view in views:
                    vddl = show_create(mcur, "SHOW CREATE VIEW", dbname, view)
                    if vddl and self.run_ddl(
                        wcur, strip_definer(vddl), f"视图 {dbname}.{view}", res, dbname
                    ):
                        res.views += 1
                ddl.commit()

            # 表和视图都就位后再建触发器/存储过程/事件，避免引用到还不存在的对象
            self.copy_other_objects(mcur, wcur, dbname, res)
            ddl.commit()
            meta.commit()
        except pymysql.MySQLError as exc:
            res.errors.append(f"元数据阶段失败：{exc}")
            self.log.error("[%s] 元数据阶段失败：%s", dbname, exc)
        finally:
            meta.close()
            ddl.close()
        res.seconds = time.time() - t0
        return res

    def copy_other_objects(self, mcur, wcur, dbname, res):
        opts = self.opts
        if opts.include_triggers:
            mcur.execute(
                "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA = %s ORDER BY TRIGGER_NAME",
                (dbname,),
            )
            for (name,) in mcur.fetchall():
                ddl = show_create(mcur, "SHOW CREATE TRIGGER", dbname, name)
                if ddl:
                    self.run_ddl(wcur, strip_definer(ddl), f"触发器 {dbname}.{name}", res, dbname)
        if opts.include_routines:
            mcur.execute(
                "SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES "
                "WHERE ROUTINE_SCHEMA = %s ORDER BY ROUTINE_NAME",
                (dbname,),
            )
            for name, kind in mcur.fetchall():
                stmt = "SHOW CREATE PROCEDURE" if kind == "PROCEDURE" else "SHOW CREATE FUNCTION"
                ddl = show_create(mcur, stmt, dbname, name)
                if not ddl:
                    res.errors.append(f"{name}: 读不到定义，源账号缺 SHOW ROUTINE 权限或不是定义者")
                    continue
                label = "存储过程" if kind == "PROCEDURE" else "函数"
                self.run_ddl(wcur, strip_definer(ddl), f"{label} {dbname}.{name}", res, dbname)
        if opts.include_events:
            mcur.execute(
                "SELECT EVENT_NAME FROM information_schema.EVENTS "
                "WHERE EVENT_SCHEMA = %s ORDER BY EVENT_NAME",
                (dbname,),
            )
            for (name,) in mcur.fetchall():
                ddl = show_create(mcur, "SHOW CREATE EVENT", dbname, name)
                if ddl:
                    self.run_ddl(wcur, strip_definer(ddl), f"事件 {dbname}.{name}", res, dbname)


# ---------------------------------------------------------------- 调度


def run(names, workers, fn, on_result):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, name): name for name in names}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                on_result(fut.result())
            except Exception as exc:  # noqa: BLE001 - 单库崩溃不该拖垮整批
                logging.getLogger("sync").error("[%s] %r", name, exc, exc_info=True)
                on_result(DbResult(name=name, errors=[f"未捕获异常：{exc!r}"]))


def setup_logging(log_file):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    if log_file:
        logging.getLogger().addHandler(logging.FileHandler(log_file, encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser(
        description="MySQL 整库同步（结构 + 数据），支持库级与表级并行",
    )
    ap.add_argument("-c", "--config", default="sync_config.ini", help="配置文件，默认 %(default)s")
    ap.add_argument("-d", "--databases", default="databases.txt", help="库清单文件，默认 %(default)s")
    ap.add_argument("--only", nargs="+", metavar="DB", help="只同步这些库，忽略清单文件")
    ap.add_argument("--exclude", nargs="*", default=[], metavar="DB", help="从清单中剔除这些库")
    ap.add_argument("--workers", type=int, help="覆盖配置里的并发库数")
    ap.add_argument("--table-workers", type=int, help="覆盖配置里的库内并发表数")
    ap.add_argument("--start-from", metavar="DB", help="从清单中该库开始，之前的库跳过（断点续跑）")
    ap.add_argument("--dry-run", action="store_true", help="只做预检并打印计划，不碰目标端")
    ap.add_argument("--yes", action="store_true", help="跳过 DROP 交互确认，无人值守时用")
    ap.add_argument("--log-file", help="日志同时追加写入该文件")
    args = ap.parse_args()

    setup_logging(args.log_file)
    log = logging.getLogger("sync")

    src, dst, opts = load_config(args.config)
    if args.workers:
        opts.workers = max(1, args.workers)
    if args.table_workers:
        opts.table_workers = max(1, args.table_workers)

    names = args.only or load_databases(args.databases)
    if bad := [n for n in names if not SAFE_NAME_RE.match(n)]:
        sys.exit(f"数据库名含非法字符：{bad}")
    if blocked := [n for n in names if n.lower() in SYSTEM_SCHEMAS]:
        sys.exit(f"拒绝同步系统库：{blocked}")

    skip = {x.lower() for x in args.exclude}
    names = [n for n in names if n.lower() not in skip]
    if args.start_from:
        key = args.start_from.lower()
        idx = next((i for i, n in enumerate(names) if n.lower() == key), None)
        if idx is None:
            sys.exit(f"--start-from {args.start_from} 不在待同步列表中")
        names = names[idx:]
    if not names:
        sys.exit("待同步数据库列表为空")

    try:
        probe = connect(src, autocommit=True)
        cur = probe.cursor()
        ver_src = scalar(cur, "SELECT VERSION()")
        cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA")
        real_names = {r[0] for r in cur.fetchall()}
        probe.close()
        probe = connect(dst, autocommit=True)
        cur = probe.cursor()
        ver_dst = scalar(cur, "SELECT VERSION()")
        cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA")
        target_exists = {r[0] for r in cur.fetchall()}
        probe.close()
    except (pymysql.MySQLError, OSError, ValueError) as exc:
        # ValueError：密码含非 latin-1 字符时 pymysql 在发握手包前就会抛
        sys.exit(f"预检连接失败：{exc}")

    # 清单里的大小写可能和实例中的真实库名不同，一律以源端为准
    by_lower = {n.lower(): n for n in real_names}
    resolved, missing = [], []
    for name in names:
        if name in real_names:
            resolved.append(name)
        elif name.lower() in by_lower:
            real = by_lower[name.lower()]
            log.warning("库名大小写与源端不一致，按源端处理：%s → %s", name, real)
            resolved.append(real)
        else:
            missing.append(name)
    names = list(dict.fromkeys(resolved))
    if missing:
        log.error("源实例中不存在 %d 个库：%s", len(missing), ", ".join(missing))
    if not names:
        sys.exit("没有可同步的数据库")

    collide = [n for n in names if n in target_exists]
    log.info("源 %s @ %s → 目标 %s @ %s", ver_src, src.host or src.socket, ver_dst, dst.host or dst.socket)
    log.info(
        "待同步 %d 个库（%d 个目标端已存在）｜并发 %d 库 x %d 表｜峰值连接约 %d 条（源、目标各占一半）",
        len(names), len(collide), opts.workers, opts.table_workers,
        opts.workers * (opts.table_workers + 1) * 2,
    )

    if args.dry_run:
        for name in names:
            print(f"{name}{'  [目标端已存在，将被 DROP 重建]' if name in collide else ''}")
        if missing:
            print(f"\n源端缺失 {len(missing)} 个：{', '.join(missing)}")
        return

    if opts.drop_existing and collide and not args.yes:
        print(f"\n即将 DROP 并重建目标端已存在的 {len(collide)} 个库，原数据不可恢复：")
        print(", ".join(collide[:20]) + (" ..." if len(collide) > 20 else ""))
        if input("\n确认继续？输入 yes：").strip().lower() != "yes":
            sys.exit("已取消")

    syncer = Syncer(src, dst, opts)
    results: list[DbResult] = []
    lock = threading.Lock()
    done = 0
    t0 = time.time()

    def on_result(res):
        nonlocal done
        with lock:
            done += 1
            results.append(res)
            log.info(
                "(%d/%d) [%s] %s  表 %d 视图 %d %s 行  %.0fs%s",
                done, len(names), res.name, "失败" if res.errors else "完成",
                res.tables, res.views, f"{res.rows:,}", res.seconds,
                f"  错误 {len(res.errors)} 条" if res.errors else "",
            )

    run(names, opts.workers, syncer.sync_database, on_result)
    elapsed = time.time() - t0

    results.sort(key=lambda r: r.name)
    failed = [r for r in results if r.errors]
    print("\n" + "=" * 72)
    print(f"总耗时 {elapsed:.0f}s   库 {len(results)}   成功 {len(results) - len(failed)}   失败 {len(failed)}")
    print(
        f"表 {sum(r.tables for r in results)}   视图 {sum(r.views for r in results)}"
        f"   行 {sum(r.rows for r in results):,}"
    )
    if failed:
        print("\n失败明细：")
        for r in failed:
            print(f"  {r.name}  ({len(r.errors)} 条)")
            for msg in r.errors[:3]:
                print(f"    - {msg}")
            if len(r.errors) > 3:
                print(f"    ... 另有 {len(r.errors) - 3} 条")
        print("\n重跑失败库：python mysql_sync.py --yes --only 库名1 库名2")
        print("从某库续跑：  python mysql_sync.py --yes --start-from 库名")
    print("=" * 72)
    sys.exit(1 if failed or missing else 0)


if __name__ == "__main__":
    main()
