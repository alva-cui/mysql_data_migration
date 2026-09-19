"""一次性自测：用假连接验证 SQL 生成、分批、并行调度、DDL 执行顺序、DEFINER 剥离，
再加日志契约与配置校验，全程不需要连真实数据库。

    python tests/selftest.py

全绿才提交；有失败时退出码 1，可直接接进 CI。
"""

from __future__ import annotations

import contextlib
import io
import logging
import logging.handlers
import os
import re
import sys
import tempfile
import threading

import pymysql.err as pymysql_err

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)  # 从任意目录都能 python tests/selftest.py

from mysqlsync import cli, config as cfg, db, ddl, logging_setup, metadata  # noqa: E402
from mysqlsync import report  # noqa: E402
from mysqlsync.channel import Channel  # noqa: E402
from mysqlsync.config import (Endpoint, Options, Settings, load_config, load_databases,  # noqa: E402
                              validate_database_names)
from mysqlsync.errors import ConfigError, FatalError  # noqa: E402
from mysqlsync.logging_setup import log_context, parse_level, setup_logging  # noqa: E402
from mysqlsync.results import DbResult, note_error  # noqa: E402
from mysqlsync.syncer import Syncer  # noqa: E402
import mysqlsync.syncer as syncer_mod  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8", errors="replace")

# 工具自己的输出改道到内存，免得断言过程被同步日志刷屏；日志契约另有 Capture 接
_SINK = io.StringIO()
setup_logging(level="CRITICAL", console=_SINK)

fails = []
total = 0


def check(name, got, want):
    global total
    total += 1
    if got != want:
        fails.append(f"{name}\n     得到 {got!r}\n     期望 {want!r}")


def raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    except Exception:  # noqa: BLE001 - 抛错了类型也算不通过
        return False
    return False


class Capture(logging.Handler):
    def __init__(self, level=logging.DEBUG):
        super().__init__(level)
        self.rows = []
        self.addFilter(logging_setup.ContextFilter())

    def emit(self, record):
        self.rows.append(record)


@contextlib.contextmanager
def capture_logs(level=logging.DEBUG):
    """把日志接出来断言级别与上下文标签——光看屏幕输出测不出日志契约。"""
    root = logging.getLogger()
    old = root.level
    cap = Capture(level)
    root.addHandler(cap)
    root.setLevel(level)
    try:
        yield cap
    finally:
        root.removeHandler(cap)
        root.setLevel(old)


class Cur:
    """假游标。带 connection 是因为真实 pymysql 游标也有这个属性，
    代码里任何 cur.connection.x 都得能用假对象跑通。"""

    def __init__(self, rows=None, desc=None, conn=None):
        self.rows, self.pos, self.calls = rows or [], 0, []
        self.description = desc or []
        self.closed = False
        self.connection = conn

    def execute(self, sql, args=None):
        self.calls.append((sql, args))

    def executemany(self, sql, rows):
        self.calls.append((sql, list(rows)))

    def fetchmany(self, n):
        chunk = self.rows[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        self.pos = len(self.rows)
        return self.rows

    def close(self):
        self.closed = True


class Conn:
    def __init__(self, cur):
        self.cur, self.commits, self.closed = cur, 0, False
        self.cur.conn = self

    def cursor(self, klass=None):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True


# ================================================================ copy_table 分批

def run_copy(rows, batch_rows, batch_bytes, cols=("id", "name")):
    src_cur = Cur(rows=list(rows))
    dst_cur = Cur()
    ch = Channel.__new__(Channel)
    ch.opts = Options(batch_rows=batch_rows)
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
check("copy_table 空表", run_copy([], 1000, 100)[:2], (0, []))
check("copy_table 关闭流式游标", run_copy([("1", "x")], 5, 1000)[3], True)
check(
    "INSERT 语句",
    run_copy([("1", "x")], 5, 1000)[2],
    "INSERT INTO `AG3139`.`t_user` (`id`, `name`) VALUES (%s, %s)",
)
check("SELECT 语句显式列出列名", db.qn("AG3139", "t_user"), "`AG3139`.`t_user`")
check("标识符转义", db.q("a`b"), "`a``b`")
check("无写列（整表皆生成列）时不产生 INSERT",
      run_copy([("1",)], 5, 1000, cols=[])[:2], (0, []))

# ================================================================ DEFINER 剥离

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
    check(f"strip_definer: {sql_in[:34]}", db.strip_definer(sql_in), want)

check("只剥第一处 DEFINER，定义体里的字面量原样保留",
      db.strip_definer("CREATE DEFINER=`root`@`localhost` VIEW `v` AS select 'DEFINER=x' as c"),
      "CREATE VIEW `v` AS select 'DEFINER=x' as c")

# ================================================================ show_create 按列名取值

c = Cur(rows=[("v", "CREATE ALGORITHM=UNDEFINED VIEW `v` AS select 1", "utf8mb4")],
        desc=[("View",), ("Create View",), ("character_set_client",)])
check("show_create 定位 Create View 列",
      db.show_create(c, "SHOW CREATE VIEW", "AG3139", "v"),
      "CREATE ALGORITHM=UNDEFINED VIEW `v` AS select 1")

c = Cur(rows=[("tr", "CREATE TRIGGER `tr` ...", "utf8mb4")],
        desc=[("Trigger",), ("SQL Original Statement",), ("CharacterSetClient",)])
check("show_create 定位 SQL Original Statement 列",
      db.show_create(c, "SHOW CREATE TRIGGER", "AG3139", "tr"), "CREATE TRIGGER `tr` ...")

c = Cur(rows=[("p", "mode", None)], desc=[("Procedure",), ("sql_mode",), ("Create Procedure",)])
check("show_create 定义列值为 NULL 时返回 None",
      db.show_create(c, "SHOW CREATE PROCEDURE", "AG3139", "p"), None)

# ================================================================ 时区偏移与批上限

check("tz_offset +8", db.tz_offset(Cur(rows=[(28800,)])), "+08:00")
check("tz_offset -530", db.tz_offset(Cur(rows=[(-19800,)])), "-05:30")
check("tz_offset 0", db.tz_offset(Cur(rows=[(0,)])), "+00:00")
check("批上限压到目标端 max_allowed_packet 的 60%",
      db.max_batch_bytes(Cur(rows=[(4 * 1024 * 1024,)]), 4 * 1024 * 1024),
      (int(4 * 1024 * 1024 * 0.6), 4 * 1024 * 1024))
check("60% 比配置值还小时取 60%",
      db.max_batch_bytes(Cur(rows=[(16 * 1024 * 1024,)]), 64 * 1024 * 1024)[0],
      int(16 * 1024 * 1024 * 0.6))
check("配置值更小就保留配置值，不放宽",
      db.max_batch_bytes(Cur(rows=[(16 * 1024 * 1024,)]), 1024)[0], 1024)

# ================================================================ 生成列过滤

c = Cur(rows=[
    ("id", ""),
    ("name", ""),
    ("full_name", "VIRTUAL GENERATED"),
    ("amount_tax", "STORED GENERATED"),
    ("created_by_expr", "DEFAULT_GENERATED"),
])
check("insertable_columns 排除生成列、保留表达式默认值列",
      metadata.insertable_columns(c, "AG3139", "t"),
      ["id", "name", "created_by_expr"])

# ================================================================ transfer_tables 并行调度


class FakeChannel:
    created = 0
    log = []
    boom = 99

    def __init__(self, src, dst, opts, tz, batch_bytes):
        if FakeChannel.boom is not None and FakeChannel.created >= FakeChannel.boom:
            raise pymysql_err.OperationalError(2003, "can't connect")
        FakeChannel.created += 1
        self.n = FakeChannel.created
        self.written = 0
        self.src = Conn(Cur())
        self.dst = Conn(Cur())
        FakeChannel.log.append(("open", self.n))

    def copy_table(self, dbname, table, cols):
        with FakeChannel.lock:
            FakeChannel.log.append(("copy", self.n, table))
        if table == "bad":
            raise pymysql_err.OperationalError(2013, "lost connection")
        self.written = len(cols)
        return self.written

    def count(self, dbname, table, on_src=False):
        return 1 if on_src else self.written

    def close(self):
        with FakeChannel.lock:
            FakeChannel.log.append(("close", self.n))


FakeChannel.lock = threading.Lock()
syncer_mod.Channel = FakeChannel


def run_transfer(tables, workers, opts=None, boom=99):
    FakeChannel.created = 0
    FakeChannel.log = []
    FakeChannel.boom = boom
    o = opts or Options(table_workers=workers, verify=False)
    s = Syncer(Endpoint(), Endpoint(), o)
    res = DbResult(name="AG3139")
    items = [(t, ["id"]) for t in tables]
    s.transfer_tables("AG3139", items, res, "+08:00", 1000)
    return res


names = [f"t{i}" for i in range(12)]
res = run_transfer(names, 4)
check("4 通道跑完 12 表，每表恰好一次",
      sorted(x[2] for x in FakeChannel.log if x[0] == "copy"), sorted(names))
check("通道数收敛到 table_workers", max(x[1] for x in FakeChannel.log if x[0] == "open"), 4)
check("所有通道被关闭", len([x for x in FakeChannel.log if x[0] == "close"]), 4)
check("行数按通道累加", res.rows, 12)
check("无错误", res.errors, [])

res = run_transfer([f"t{i}" for i in range(3)], 8)
check("表数少于 table_workers 时不超额建通道", FakeChannel.created, 3)

res = run_transfer(names, 1)
check("table_workers=1 退化为单通道顺序", FakeChannel.created, 1)
check("单通道保持清单顺序",
      [x[2] for x in FakeChannel.log if x[0] == "copy"], names)

res = run_transfer(["a", "bad", "c", "d"], 2)
check("单表失败只记录该表的错误", len([e for e in res.errors if "bad" in e]), 1)
check("失败后其余表仍完成",
      sorted(x[2] for x in FakeChannel.log if x[0] == "copy"), ["a", "bad", "c", "d"])

res = run_transfer(names, 4, boom=2)
check("部分通道建不起来时记录错误", len(res.errors) >= 1, True)
check("建不起来的通道错误必须进 errors（否则会报告成功但少表）",
      any("传输通道建立失败" in e for e in res.errors), True)

res = run_transfer(["bad", "t2"], 1, boom=1)
check("换通道重建失败时记录并停住剩余表",
      any("通道重建失败" in e for e in res.errors), True)
check("重建失败后剩余表不再尝试",
      [x[2] for x in FakeChannel.log if x[0] == "copy"], ["bad"])

o = Options(table_workers=2, verify=True)
res = run_transfer(["a", "b"], 2, opts=o)
check("verify 一致时无错误", res.errors, [])


def short_copy(self, dbname, table, cols):
    self.written = 99
    return 99


FakeChannel.copy_table = short_copy
res = run_transfer(["a"], 1, opts=o)
check("verify 不一致时报行数不符",
      any("行数不符 源 1 / 目标 99" in e for e in res.errors), True)

# ================================================================ 配置与清单

# 自测夹具直接写在代码里：本项目不入库任何含 password 键的 ini（见 AGENTS.md），
# 断言依赖的取值（workers/batch_bytes/sql_mode/含 % 的密码）都在这段字符串里
SAMPLE_INI = """
[source]
host = 10.0.0.11
port = 3306
user = sync_reader
password = p@ss%w0rd
charset = utf8mb4

[target]
host = 10.0.0.12
port = 3306
user = root
password = CHANGE_ME_DST
charset = utf8mb4

[sync]
workers = 4
table_workers = 4
batch_rows = 1000
batch_bytes = 4194304
drop_existing = true
sql_mode =
net_timeout = 3600
"""

with tempfile.NamedTemporaryFile("w", suffix=".ini", encoding="utf-8", delete=False) as fh:
    fixture = fh.name
    fh.write(SAMPLE_INI)
try:
    src, dst, opts = load_config(fixture)
finally:
    os.unlink(fixture)
check("配置 workers", opts.workers, 4)
check("配置 table_workers", opts.table_workers, 4)
check("配置 sql_mode 留空", opts.sql_mode, "")
check("配置 batch_bytes", opts.batch_bytes, 4194304)
check("源端口", src.port, 3306)
check("密码里 % 不被当插值语法吃掉", src.password, "p@ss%w0rd")
listed = load_databases(os.path.join(ROOT, "databases.txt"))
check("库清单条数", len(listed), 100)
check("清单首个", listed[0], "AG3139")
check("清单末个", listed[-1], "AG2928")
check("清单去重", len(set(listed)), 100)
check("清单无系统库", [n for n in listed if n.lower() in cfg.SYSTEM_SCHEMAS], [])
check("库名全部通过合法性校验", [n for n in listed if not cfg.SAFE_NAME_RE.match(n)], [])

with tempfile.TemporaryDirectory() as td:
    def write_ini(body, name="c.ini"):
        path = os.path.join(td, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    BASE = "[source]\nhost = h\n[target]\nhost = t\n[sync]\n"

    def try_load(body):
        return lambda: load_config(write_ini(body))

    check("workers=0 直接报错而不是悄悄当 1 用",
          raises(ConfigError, try_load(BASE + "workers = 0\n")), True)
    check("drop_existing 写成中文值报错",
          raises(ConfigError, try_load(BASE + "drop_existing = 也许\n")), True)
    check("batch_rows 非整数报错并指出键名",
          raises(ConfigError, try_load(BASE + "batch_rows = 一千\n")), True)
    check("batch_bytes 小于 64KB 报错",
          raises(ConfigError, try_load(BASE + "batch_bytes = 1024\n")), True)
    check("端口越界报错",
          raises(ConfigError, try_load("[source]\nhost = h\nport = 99999\n")), True)
    check("host 与 socket 都是空值时报错，不悄悄连本机",
          raises(ConfigError, try_load("[source]\nhost =\n")), True)
    check("缺整节 [target] 也报错而不是连到 127.0.0.1",
          raises(ConfigError, try_load("[source]\nhost = h\n")), True)
    check("配置文件不存在时报错", raises(ConfigError, lambda: load_config("/nope/x.ini")), True)

    os.environ["SYNC_SRC_PASSWORD"] = "来自环境变量"
    try:
        ep_src, _, _ = load_config(write_ini(BASE))
        check("环境变量覆盖配置文件里的密码", ep_src.password, "来自环境变量")
    finally:
        del os.environ["SYNC_SRC_PASSWORD"]

check("拒绝系统库", raises(ConfigError, lambda: validate_database_names(["mysql"])), True)
check("拒绝非法字符库名", raises(ConfigError, lambda: validate_database_names(["bad`name"])), True)
check("合法库名原样通过", validate_database_names(["AG3139"]), ["AG3139"])

ep = Endpoint(host="10.0.0.11", user="sync_reader", password="s3cret")
check("Endpoint repr 屏蔽密码", "s3cret" in repr(ep), False)
check("日志用的端点表示", ep.display, "sync_reader@10.0.0.11:3306")
check("socket 连接的端点表示",
      Endpoint(user="u", socket="/run/mysqld/m.sock").display, "u@unix:/run/mysqld/m.sock")
check("参数快照不夹带密码", "s3cret" in Settings(ep, ep, Options()).snapshot(), False)

# ================================================================ 命令行选择逻辑

args = cli.build_parser().parse_args(["--exclude", "AGb", "--start-from", "AGc"])
check("exclude 与 start-from 生效",
      cli.select_names(["AGa", "AGb", "AGc", "AGd"], args), ["AGc", "AGd"])
check("start-from 不在清单里时报错",
      raises(ConfigError,
             lambda: cli.select_names(["AGa"], cli.build_parser().parse_args(["--start-from", "ZZ"]))),
      True)

opts = Options()
cli.apply_overrides(opts, cli.build_parser().parse_args(["--workers", "9", "--table-workers", "2"]))
check("并发数覆盖生效", (opts.workers, opts.table_workers), (9, 2))
check("并发数覆盖拒绝 0",
      raises(ConfigError,
             lambda: cli.apply_overrides(Options(), cli.build_parser().parse_args(["--workers", "0"]))),
      True)

# ================================================================ 日志契约

with capture_logs() as cap:
    with log_context("AG3139"):
        logging.getLogger("mysqlsync.t").info("建表完成")
    with log_context("AG3139/ch2"):
        logging.getLogger("mysqlsync.t").info("传数据")
check("每条日志带当前库/通道标签", [r.ctx for r in cap.rows], ["AG3139", "AG3139/ch2"])

cap.rows.clear()
with capture_logs() as cap:
    with log_context("AG3139"):
        t = threading.Thread(target=lambda: logging.getLogger("mysqlsync.t").info("子线程"))
        t.start()
        t.join()
check("子线程不继承标签，必须自己设置（通道线程就是这么标自己的）",
      [r.ctx for r in cap.rows], ["main"])

res = DbResult(name="AG3139")
with capture_logs(logging.ERROR) as cap:
    note_error(res, logging.getLogger("mysqlsync.t"), "表 AG3139.t1 创建失败：1142")
check("note_error 进 errors 供汇总报告", res.errors, ["表 AG3139.t1 创建失败：1142"])
check("note_error 同时按 ERROR 级别进日志",
      [(r.levelno, r.getMessage()) for r in cap.rows],
      [(logging.ERROR, "表 AG3139.t1 创建失败：1142")])

check("日志级别拒绝拼错的名字", raises(ValueError, lambda: parse_level("VERBOSE")), True)
check("日志级别接受小写", parse_level("debug"), logging.DEBUG)

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "sync.log")
    setup_logging(level="INFO", log_file=path, console=_SINK)
    logging.getLogger("mysqlsync.t").info("落盘检查")
    for handler in list(logging.getLogger().handlers):
        handler.flush()
    with open(path, encoding="utf-8") as fh:
        line = fh.read()
    parts = line.split()
    check("落盘行带日期与级别（旧版文件 handler 没挂 formatter 时这里全丢）",
          bool(re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO ", line)), True)
    check("落盘行带上下文列", parts[2:5], ["INFO", "main", "落盘检查"])
    check("日志文件走轮转 handler",
          len([h for h in logging.getLogger().handlers
               if isinstance(h, logging.handlers.RotatingFileHandler)]), 1)
    check("未到阈值不切分", os.path.exists(path + ".1"), False)

    rotate = os.path.join(td, "rot.log")
    setup_logging(level="INFO", log_file=rotate, max_bytes=600, backups=2, console=_SINK)
    for i in range(40):
        logging.getLogger("mysqlsync.t").info("第 %d 行 一串会撑大字节的正文内容", i)
    for handler in list(logging.getLogger().handlers):
        handler.flush()
    check("超过 --log-max-mb 就切分", os.path.exists(rotate + ".1"), True)
    check("轮转只保留 --log-backups 份", os.path.exists(rotate + ".3"), False)
    with open(rotate, encoding="utf-8") as fh:
        latest = fh.read()
    check("当前文件仍是最新的日志", "第 39 行" in latest, True)

setup_logging(level="CRITICAL", console=_SINK)
setup_logging(level="CRITICAL", console=_SINK)
check("重复初始化不叠加 handler", len(logging.getLogger().handlers), 1)

# ================================================================ 报告

done = DbResult(name="ag1", tables=3, views=1, rows=100,
                found={"tables": 3, "views": 1}, seconds=1.0)
broken = DbResult(name="ag2", tables=1, rows=5, errors=["表 ag2.t1 创建失败：1142"],
                  found={"tables": 2, "triggers": 1}, seconds=2.0)
totals = report.summarize([done, broken], ["ag3"], 12.0)
check("汇总按对象类别累加已建/源端可见",
      {label: (got, seen) for label, got, seen in totals.objects},
      {"表": (4, 5), "视图": (1, 1), "触发器": (0, 1)})
check("汇总计数", (totals.dbs, totals.ok, totals.failed, totals.rows, totals.errors),
      (2, 1, 1, 105, 1))
check("有失败或有缺失库则退出码 1", totals.exit_code, 1)
check("全部成功且无缺失则退出码 0",
      report.summarize([done], [], 1.0).exit_code, 0)

with capture_logs(logging.INFO) as cap:
    report.log_final([broken], [], 3.0)
messages = [r.getMessage() for r in cap.rows]
check("汇总单行 RESULT 供事后 grep",
      any(re.search(r"RESULT .*退出码=1$", m) for m in messages), True)
check("失败库的明细按 ERROR 落日志",
      any(r.levelno == logging.ERROR and "创建失败" in r.getMessage() for r in cap.rows), True)
check("报告里给出可直接复制的重跑命令",
      any("--only ag2" in m for m in messages), True)
check("每库小结把库名放上下文列而不是消息里",
      [r.ctx for r in cap.rows if r.ctx == "ag2"][:2], ["ag2", "ag2"])

check("字节可读化", (report.human_bytes(4194304), report.human_bytes(900)), ("4.0 MB", "900 B"))

# ================================================================ 建库语句

c = Cur()
ddl.create_schema(c, "AG3139", "utf8mb4", "utf8mb4_0900_ai_ci")
check("drop_existing 默认先 DROP", [x[0] for x in c.calls],
      ["DROP DATABASE IF EXISTS `AG3139`",
       "CREATE DATABASE IF NOT EXISTS `AG3139` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"])

c = Cur()
ddl.create_schema(c, "AG3139", "utf8mb4", None, drop_existing=False)
check("drop_existing=false 不 DROP", [x[0] for x in c.calls],
      ["CREATE DATABASE IF NOT EXISTS `AG3139` DEFAULT CHARACTER SET utf8mb4"])

c = Cur()
ddl.select_schema(c, "AG3139")
check("选中目标库", [x[0] for x in c.calls], ["USE `AG3139`"])

check("函数用 SHOW CREATE FUNCTION", metadata.routine_show_stmt("FUNCTION"),
      "SHOW CREATE FUNCTION")
check("存储过程用 SHOW CREATE PROCEDURE", metadata.routine_show_stmt("PROCEDURE"),
      "SHOW CREATE PROCEDURE")
check("非法字符集名被挡下",
      raises(FatalError, lambda: metadata.schema_charset(Cur(rows=[("utf8mb4; DROP x", "c")],
                                                           desc=[("a",)]), "AG3139")), True)
check("源端没有该库时报错而不是静默空库",
      raises(FatalError, lambda: metadata.schema_charset(Cur(rows=[]), "AG3139")), True)


# ================================================================ sync_database 全流程（假连接）

SRC_EP = Endpoint(host="SRC")
DST_EP = Endpoint(host="DST")

VIEW_DDL = ("CREATE ALGORITHM=UNDEFINED DEFINER=`trader`@`10.1.%` "
            "SQL SECURITY DEFINER VIEW `v1` AS select 1")

SRC_SCRIPT = [
    (r"^SET ", [("a",)], []),
    (r"^SELECT TIMESTAMPDIFF", [("a",)], [(28800,)]),
    (r"SCHEMATA", [("DEFAULT_CHARACTER_SET_NAME",), ("DEFAULT_COLLATION_NAME",)],
     [("utf8mb4", "utf8mb4_0900_ai_ci")]),
    (r"FROM information_schema\.TABLES", [("TABLE_NAME",), ("TABLE_TYPE",)],
     [("t1", "BASE TABLE"), ("t2", "BASE TABLE"), ("v1", "VIEW")]),
    (r"SHOW CREATE TABLE `AG3139`\.`t1`", [("Table",), ("Create Table",)],
     [("t1", "CREATE TABLE `t1` (`id` int NOT NULL, PRIMARY KEY (`id`))")]),
    (r"SHOW CREATE TABLE", [("Table",), ("Create Table",)],
     [("t2", "CREATE TABLE `t2` (`id` int NOT NULL, PRIMARY KEY (`id`))")]),
    (r"FROM information_schema\.COLUMNS", [("COLUMN_NAME",), ("EXTRA",)], [("id", "")]),
    (r"SHOW CREATE VIEW", [("View",), ("Create View",)], [("v1", VIEW_DDL)]),
    (r"FROM information_schema\.TRIGGERS", [("TRIGGER_NAME",)], [("tr1",)]),
    (r"SHOW CREATE TRIGGER", [("Trigger",), ("SQL Original Statement",)],
     [("tr1", "CREATE DEFINER=`root`@`localhost` TRIGGER `tr1` BEFORE INSERT ON `t1` FOR EACH ROW set @a=1")]),
    (r"FROM information_schema\.ROUTINES", [("ROUTINE_NAME",), ("ROUTINE_TYPE",)], [("p1", "PROCEDURE")]),
    (r"SHOW CREATE PROCEDURE", [("Procedure",), ("sql_mode",), ("Create Procedure",)],
     [("p1", "mode", "CREATE DEFINER=`root`@`localhost` PROCEDURE `p1`() SELECT 1")]),
    (r"FROM information_schema\.EVENTS", [("EVENT_NAME",)], []),
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
        self.conn = None

    @property
    def connection(self):
        return self.conn

    def execute(self, sql, args=None):
        self.calls.append(sql)
        sql1 = sql.replace("\n", " ")
        for pat, desc, rows in self.script:
            if re.search(pat, sql1, re.I):
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
        self.cur.conn = self
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
    conn = ScriptConn(SRC_SCRIPT if ep.host == "SRC" else DST_SCRIPT)
    CONNS[ep.host].append(conn)
    return conn


db.connect = fake_connect
Transferred = []
TLock = threading.Lock()


class StubChannel:
    def __init__(self, src, dst, opts, tz, batch_bytes):
        self.tz, self.batch_bytes = tz, batch_bytes
        with TLock:
            Transferred.append(("open", tz, batch_bytes))

    def copy_table(self, dbname, table, cols):
        with TLock:
            Transferred.append(("copy", table, list(cols)))
        return 5

    def count(self, dbname, table, on_src=False):
        return 5

    def close(self):
        pass


syncer_mod.Channel = StubChannel


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
s = Syncer(SRC_EP, DST_EP, Options(table_workers=2, workers=1))
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
check("传输列已排除生成列",
      [x[2] for x in Transferred if x[0] == "copy" and x[1] == "t1"], [["id"]])
check("源端对象计数被记录下来（供逐类核验）",
      {k: v for k, v in res.found.items()},
      {"tables": 2, "views": 1, "triggers": 1, "routines": 1, "events": 0})
check("目标端建起来的依赖对象被计数",
      (res.created_triggers, res.created_routines, res.created_events), (1, 1, 0))

with capture_logs(logging.INFO) as cap:
    s.sync_database("AG3139")
# 两张表可能被同一条通道抢完，所以只断言标签形态，不断言出现了几条通道
ctxs = {r.ctx for r in cap.rows if r.ctx != "main"}
check("每库自己的日志带库名上下文", "AG3139" in ctxs, True)
check("通道日志带「库名/通道号」", any(c.startswith("AG3139/ch") for c in ctxs), True)
check("上下文标签里不带方括号等自造分隔",
      any("[" in c for c in ctxs), False)

# 源端建表语句读不到时，该表必须报错且不参与传输，不能静默丢数据
SRC_SCRIPT.insert(0, (r"SHOW CREATE TABLE `AG3139`\.`t2`", [("Table",), ("Create Table",)],
                      [("t2", "   ")]))
Transferred.clear()
res = s.sync_database("AG3139")
check("建表语句为空时记为错误", any("读不到建表语句" in e for e in res.errors), True)
check("建表失败的表不参与传输", [x[1] for x in Transferred if x[0] == "copy"], ["t1"])
check("只传输成功的表参与计数", res.tables, 1)

# ================================================================ 结果

print("=" * 60)
print(f"{total} 项断言全部通过" if not fails else f"{total} 项断言，{len(fails)} 项失败")
for f in fails:
    print("  x", f)
print("=" * 60)
sys.exit(1 if fails else 0)
