"""一次性自测：用假连接验证 SQL 生成、分批、并行调度与 DEFINER 剥离，无需连真实库。"""
import logging
import sys
import pymysql
import mysql_sync as ms

for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.CRITICAL)
fails = []
total = 0


def check(name, got, want):
    global total
    total += 1
    if got != want:
        fails.append(f"{name}\n     得到 {got!r}\n     期望 {want!r}")


class Cur:
    def __init__(self, rows=None, desc=None):
        self.rows, self.pos, self.calls = rows or [], 0, []
        self.description = desc or []
        self.closed = False

    def execute(self, sql, args=None):
        self.calls.append((sql, args))

    def executemany(self, sql, rows):
        self.calls.append((sql, list(rows)))

    def fetchmany(self, n):
        chunk = self.rows[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk

    def fetchone(self):
        return self.rows[0] if self.pos < len(self.rows) else None

    def fetchall(self):
        self.pos = len(self.rows)
        return self.rows

    def close(self):
        self.closed = True


class Conn:
    def __init__(self, cur):
        self.cur, self.commits, self.closed = cur, 0, False

    def cursor(self, klass=None):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True


# ---- copy_table 分批 ----

def run_copy(rows, batch_rows, batch_bytes, cols=("id", "name")):
    src_cur = Cur(rows=list(rows))
    dst_cur = Cur()
    ch = ms.Channel.__new__(ms.Channel)
    ch.opts = ms.Options(batch_rows=batch_rows)
    ch.batch_bytes = batch_bytes
    ch.src = Conn(src_cur)
    ch.dst = Conn(dst_cur)
    total = ch.copy_table("AG3139", "t_user", list(cols))
    first = dst_cur.calls[0][0] if dst_cur.calls else None
    return total, [len(r) for _, r in dst_cur.calls], first, src_cur.closed


check(
    "copy_table 批大小（按行数截断）",
    run_copy([("1", "a" * 10)] * 10, 3, 10_000)[:2],
    (10, [3, 3, 3, 1]),
)
check(
    "copy_table 批大小（按字节截断）",
    run_copy([("1", "a" * 10)] * 10, 1000, 25)[:2],
    (10, [2, 2, 2, 2, 2]),
)
check(
    "copy_table 空表",
    run_copy([], 1000, 100)[:2],
    (0, []),
)
check("copy_table 关闭流式游标", run_copy([("1", "x")], 5, 1000)[3], True)
check(
    "INSERT 语句",
    run_copy([("1", "x")], 5, 1000)[2],
    "INSERT INTO `AG3139`.`t_user` (`id`, `name`) VALUES (%s, %s)",
)
check(
    "SELECT 语句显式列出列名",
    ms.qn("AG3139", "t_user"),
    "`AG3139`.`t_user`",
)
check("标识符转义", ms.q("a`b"), "`a``b`")
check("无写列（整表皆生成列）时不产生 INSERT", run_copy([("1",)], 5, 1000, cols=[])[:2], (0, []))

# ---- DEFINER 剥离 ----

cases = [
    (
        "CREATE ALGORITHM=UNDEFINED DEFINER=`root`@`localhost` SQL SECURITY DEFINER VIEW `v` AS select 1",
        "CREATE ALGORITHM=UNDEFINED SQL SECURITY DEFINER VIEW `v` AS select 1",
    ),
    (
        "CREATE DEFINER='sync'@'10.0.%' PROCEDURE `p`() BEGIN END",
        "CREATE PROCEDURE `p`() BEGIN END",
    ),
    (
        "CREATE DEFINER=root@localhost TRIGGER `tr` BEFORE INSERT ON `t` FOR EACH ROW set @a=1",
        "CREATE TRIGGER `tr` BEFORE INSERT ON `t` FOR EACH ROW set @a=1",
    ),
    (
        "CREATE EVENT `e` ON SCHEDULE EVERY 1 HOUR DO nothing",
        "CREATE EVENT `e` ON SCHEDULE EVERY 1 HOUR DO nothing",
    ),
]
for sql_in, want in cases:
    check(f"strip_definer: {sql_in[:34]}", ms.strip_definer(sql_in), want)

# ---- show_create 按列名取值 ----

c = Cur(rows=[("v", "CREATE ALGORITHM=UNDEFINED VIEW `v` AS select 1", "utf8mb4")],
        desc=[("View",), ("Create View",), ("character_set_client",)])
check("show_create 定位 Create View 列", ms.show_create(c, "SHOW CREATE VIEW", "AG3139", "v"),
      "CREATE ALGORITHM=UNDEFINED VIEW `v` AS select 1")

c = Cur(rows=[("tr", "CREATE TRIGGER `tr` ...", "utf8mb4")],
        desc=[("Trigger",), ("SQL Original Statement",), ("CharacterSetClient",)])
check("show_create 定位 SQL Original Statement 列",
      ms.show_create(c, "SHOW CREATE TRIGGER", "AG3139", "tr"), "CREATE TRIGGER `tr` ...")

c = Cur(rows=[("p", "mode", None)], desc=[("Procedure",), ("sql_mode",), ("Create Procedure",)])
check("show_create 定义列值为 NULL 时返回 None",
      ms.show_create(c, "SHOW CREATE PROCEDURE", "AG3139", "p"), None)

# ---- 时区偏移格式化 ----

check("tz_offset +8", ms.tz_offset(Cur(rows=[(28800,)])), "+08:00")
check("tz_offset -530", ms.tz_offset(Cur(rows=[(-19800,)])), "-05:30")
check("tz_offset 0", ms.tz_offset(Cur(rows=[(0,)])), "+00:00")

# ---- 生成列过滤 ----

c = Cur(rows=[
    ("id", ""),
    ("name", ""),
    ("full_name", "VIRTUAL GENERATED"),
    ("amount_tax", "STORED GENERATED"),
    ("created_by_expr", "DEFAULT_GENERATED"),
])
check("insertable_columns 排除生成列、保留表达式默认值列",
      ms.Syncer.insertable_columns(ms.Syncer.__new__(ms.Syncer), c, "AG3139", "t"),
      ["id", "name", "created_by_expr"])


# ---- transfer_tables 并行调度 ----

class FakeChannel:
    created = 0
    log = []
    lock_none = None

    def __init__(self, syncer, tz, batch_bytes):
        if getattr(FakeChannel, "boom") and FakeChannel.created >= FakeChannel.boom:
            raise pymysql.err.OperationalError(2003, "can't connect")
        FakeChannel.created += 1
        self.n = FakeChannel.created
        self.written = 0
        self.src = Conn(Cur())
        self.dst = Conn(Cur())
        FakeChannel.log.append(("open", self.n))

    def copy_table(self, dbname, table, cols):
        FakeChannel.lock.acquire()
        try:
            FakeChannel.log.append(("copy", self.n, table))
        finally:
            FakeChannel.lock.release()
        if table == "bad":
            raise pymysql.err.OperationalError(2013, "lost connection")
        self.written = len(cols)
        return self.written

    def count(self, dbname, table, on_src=False):
        return 1 if on_src else self.written

    def close(self):
        FakeChannel.lock.acquire()
        try:
            FakeChannel.log.append(("close", self.n))
        finally:
            FakeChannel.lock.release()


FakeChannel.lock = __import__("threading").Lock()
ms.Channel = FakeChannel


def run_transfer(tables, workers, opts=None, boom=99):
    FakeChannel.created = 0
    FakeChannel.log = []
    FakeChannel.boom = boom
    o = opts
    if o is None:
        o = ms.Options(table_workers=workers)
        o.verify = False
    s = ms.Syncer(ms.Endpoint(), ms.Endpoint(), o)
    res = ms.DbResult(name="AG3139")
    items = [(t, ["id"]) for t in tables]
    s.transfer_tables("AG3139", items, res, "+08:00", 1000)
    return res


names = [f"t{i}" for i in range(12)]
res = run_transfer(names, 4)
check("4 通道跑完 12 表，每表恰好一次", sorted(x[2] for x in FakeChannel.log if x[0] == "copy"), sorted(names))
check("通道数收敛到 table_workers", max(x[1] for x in FakeChannel.log if x[0] == "open"), 4)
check("所有通道被关闭", len([x for x in FakeChannel.log if x[0] == "close"]), 4)
check("行数按通道累加", res.rows, 12)
check("无错误", res.errors, [])

res = run_transfer([f"t{i}" for i in range(3)], 8)
check("表数少于 table_workers 时不超额建通道", FakeChannel.created, 3)

res = run_transfer(names, 1)
check("table_workers=1 退化为单通道顺序", FakeChannel.created, 1)
order = [x[2] for x in FakeChannel.log if x[0] == "copy"]
check("单通道保持清单顺序", order, names)

res = run_transfer(["a", "bad", "c", "d"], 2)
check("单表失败只记录错误", len([e for e in res.errors if "bad" in e]), 1)
check("失败后其余表仍完成", sorted(x[2] for x in FakeChannel.log if x[0] == "copy"), ["a", "bad", "c", "d"])

res = run_transfer(names, 4, boom=2)
check("部分通道建不起来时记录错误", len(res.errors) >= 1, True)

o = ms.Options(table_workers=2)
o.verify = True
res = run_transfer(["a", "b"], 2, opts=o)
check("verify 一致时无错误", res.errors, [])
o.verify = True


def short_copy(self, dbname, table, cols):
    self.written = 99
    return 99


FakeChannel.copy_table = short_copy
res = run_transfer(["a"], 1, opts=o)
check("verify 不一致时报行数不符", "行数不符 源 1 / 目标 99" in res.errors[0], True)

# ---- 配置与清单 ----

src, dst, opts = ms.load_config("sync_config.ini")
check("配置 workers", opts.workers, 4)
check("配置 table_workers", opts.table_workers, 4)
check("配置 sql_mode 留空", opts.sql_mode, "")
check("配置 batch_bytes", opts.batch_bytes, 4194304)
check("源端口", src.port, 3306)

names = ms.load_databases("databases.txt")
check("库清单条数", len(names), 100)
check("清单首个", names[0], "AG3139")
check("清单末个", names[-1], "AG2928")
check("清单去重", len(set(names)), 100)
check("清单无系统库", [n for n in names if n.lower() in ms.SYSTEM_SCHEMAS], [])
check("库名全部通过合法性校验", [n for n in names if not ms.SAFE_NAME_RE.match(n)], [])

# ---- 建库语句 ----

s = ms.Syncer(ms.Endpoint(), ms.Endpoint(), ms.Options())
c = Cur()
s.create_schema(c, "AG3139", "utf8mb4", "utf8mb4_0900_ai_ci")
check("drop_existing 默认先 DROP", [x[0] for x in c.calls],
      ["DROP DATABASE IF EXISTS `AG3139`", "CREATE DATABASE IF NOT EXISTS `AG3139` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"])

s2 = ms.Syncer(ms.Endpoint(), ms.Endpoint(), ms.Options(drop_existing=False))
c = Cur()
s2.create_schema(c, "AG3139", "utf8mb4", None)
check("drop_existing=false 不 DROP", [x[0] for x in c.calls],
      ["CREATE DATABASE IF NOT EXISTS `AG3139` DEFAULT CHARACTER SET utf8mb4"])

# ---- sync_database 全流程编排（假连接） ----
import re as _re

SRC_EP = ms.Endpoint(host="SRC")
DST_EP = ms.Endpoint(host="DST")

VIEW_DDL = ("CREATE ALGORITHM=UNDEFINED DEFINER=`trader`@`10.1.%` "
            "SQL SECURITY DEFINER VIEW `v1` AS select 1")

SRC_SCRIPT = [
    (r"^SELECT TIMESTAMPDIFF", [("a",)], [(28800,)]),
    (r"^SET ", [("a",)], []),
    (r"SCHEMATA", [("DEFAULT_CHARACTER_SET_NAME",), ("DEFAULT_COLLATION_NAME",)],
     [("utf8mb4", "utf8mb4_0900_ai_ci")]),
    (r"TABLES", [("TABLE_NAME",), ("TABLE_TYPE",)],
     [("t1", "BASE TABLE"), ("t2", "BASE TABLE"), ("v1", "VIEW")]),
    (r"SHOW CREATE TABLE `AG3139`\.`t1`", [("Table",), ("Create Table",)],
     [("t1", "CREATE TABLE `t1` (`id` int NOT NULL, PRIMARY KEY (`id`))")]),
    (r"SHOW CREATE TABLE", [("Table",), ("Create Table",)],
     [("t2", "CREATE TABLE `t2` (`id` int NOT NULL, PRIMARY KEY (`id`))")]),
    (r"COLUMNS", [("COLUMN_NAME",), ("EXTRA",)], [("id", "")]),
    (r"SHOW CREATE VIEW", [("View",), ("Create View",)], [("v1", VIEW_DDL)]),
    (r"TRIGGERS", [("TRIGGER_NAME",)], [("tr1",)]),
    (r"SHOW CREATE TRIGGER", [("Trigger",), ("SQL Original Statement",)],
     [("tr1", "CREATE DEFINER=`root`@`localhost` TRIGGER `tr1` BEFORE INSERT ON `t1` FOR EACH ROW set @a=1")]),
    (r"ROUTINES", [("ROUTINE_NAME",), ("ROUTINE_TYPE",)], [("p1", "PROCEDURE")]),
    (r"SHOW CREATE PROCEDURE", [("Procedure",), ("sql_mode",), ("Create Procedure",)],
     [("p1", "mode", "CREATE DEFINER=`root`@`localhost` PROCEDURE `p1`() SELECT 1")]),
    (r"EVENTS", [("EVENT_NAME",)], []),
]

DST_SCRIPT = [
    (r"^SELECT @@max_allowed_packet", [("a",)], [(4194304,)]),
    (r"^SET ", [("a",)], []),
    (r"^(DROP|CREATE)", [("a",)], []),
    (r"COUNT", [("a",)], [(1,)]),
]


class ScriptCur:
    def __init__(self, script):
        self.script, self.calls = script, []
        self.desc, self.rows = [("a",)], []

    def execute(self, sql, args=None):
        self.calls.append(sql)
        sql1 = sql.replace("\n", " ")
        for pat, desc, rows in self.script:
            if _re.search(pat, sql1, _re.I):
                self.desc, self.rows = desc, rows
                return
        self.desc, self.rows = [("a",)], []

    def executemany(self, sql, rows):
        self.calls.append(sql)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def close(self):
        pass

    @property
    def description(self):
        return self.desc


class ScriptConn:
    def __init__(self, script):
        self.cur = ScriptCur(script)
        self.commits = 0

    def cursor(self, klass=None):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


CONNS = {"SRC": [], "DST": []}


def fake_connect(ep, autocommit):
    c = ScriptConn(SRC_SCRIPT if ep.host == "SRC" else DST_SCRIPT)
    CONNS[ep.host].append(c)
    return c


ms.connect = fake_connect
Transferred = []
TLock = __import__("threading").Lock()


class StubChannel:
    def __init__(self, syncer, tz, batch_bytes):
        self.tz, self.batch_bytes = tz, batch_bytes
        TLock.acquire()
        try:
            Transferred.append(("open", tz, batch_bytes))
        finally:
            TLock.release()

    def copy_table(self, dbname, table, cols):
        TLock.acquire()
        try:
            Transferred.append(("copy", table, list(cols)))
        finally:
            TLock.release()
        return 5

    def count(self, dbname, table, on_src=False):
        return 5

    def close(self):
        pass


ms.Channel = StubChannel


def kind(sql):
    up = sql.upper()
    if up.startswith("USE "):
        return "USE"
    for key in ("DROP DATABASE", "CREATE DATABASE", "CREATE TRIGGER", "CREATE PROCEDURE",
                "VIEW `V1`", "TABLE `T1`", "TABLE `T2`"):
        if key in up:
            return key
    return "?"


Transferred.clear()
s = ms.Syncer(SRC_EP, DST_EP, ms.Options(table_workers=2, workers=1))
res = s.sync_database("AG3139")
dst_conn = CONNS["DST"][-1]
ddl_seq = [c for c in dst_conn.cur.calls if not c.startswith("SET") and not c.startswith("SELECT")]

check("全流程无错误", res.errors, [])
check(
    "DDL 执行顺序：建库→选中库→建表→建视图→触发器→存储过程",
    [kind(c) for c in ddl_seq],
    ["DROP DATABASE", "CREATE DATABASE", "USE", "TABLE `T1`", "TABLE `T2`",
     "VIEW `V1`", "CREATE TRIGGER", "CREATE PROCEDURE"],
)
# SHOW CREATE TABLE / PROCEDURE 的输出不带库名前缀，会话没 USE 选中库就执行会报
# 1046 No database selected，整库 126 张表全部建不出来
check("建表前已 USE 选中目标库",
      [c for c in ddl_seq if c.startswith("USE ")], ["USE `AG3139`"])
check("两张表都被传输", sorted(x[1] for x in Transferred if x[0] == "copy"), ["t1", "t2"])
check("通道数收敛到 table_workers", len([x for x in Transferred if x[0] == "open"]), 2)
check(
    "通道继承统一时区与按 max_allowed_packet 折算的批上限",
    {x[1:] for x in Transferred if x[0] == "open"},
    {("+08:00", int(4194304 * 0.6))},
)
view_ddl = [c for c in ddl_seq if "VIEW `v1`" in c][0]
check("视图 DEFINER 已剥离", "DEFINER=" in view_ddl, False)
check("视图保留 SQL SECURITY", "SQL SECURITY DEFINER VIEW `v1`" in view_ddl, True)
trig_ddl = [c for c in ddl_seq if "CREATE TRIGGER" in c][0]
check("触发器 DEFINER 已剥离", "DEFINER=" in trig_ddl, False)
check("存储过程 DEFINER 已剥离", "DEFINER=" in [c for c in ddl_seq if "PROCEDURE" in c][0], False)
check("对象计数", (res.tables, res.views, res.rows), (2, 1, 10))
check("传输列已排除生成列", [x[2] for x in Transferred if x[0] == "copy" and x[1] == "t1"], [["id"]])

# 源端建表语句读不到时，该表必须报错且不参与传输，不能静默丢数据
SRC_SCRIPT.insert(0, (r"SHOW CREATE TABLE `AG3139`\.`t2`", [("Table",), ("Create Table",)], [("t2", "   ")]))
Transferred.clear()
res = s.sync_database("AG3139")
check("建表语句为空时记为错误", any("读不到建表语句" in e for e in res.errors), True)
check("建表失败的表不参与传输", [x[1] for x in Transferred if x[0] == "copy"], ["t1"])
check("只传输成功的表参与计数", res.tables, 1)

print("=" * 60)
print(f"{total} 项断言全部通过" if not fails else f"{total} 项断言，{len(fails)} 项失败")
for f in fails:
    print("  x", f)
print("=" * 60)
sys.exit(1 if fails else 0)
