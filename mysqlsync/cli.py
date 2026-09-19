"""命令行入口：参数解析、预检、破坏面确认、调度与退出码。

模块本身不 sys.exit：致命问题抛 FatalError 子类，由 main() 转成一条 ERROR 日志 + 退出码，
原因才会同时出现在控制台和 --log-file 里。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field

import pymysql

from . import __version__, db, metadata, report
from .config import load_databases, load_settings, validate_database_names
from .errors import (EXIT_FATAL, EXIT_INTERRUPTED, EXIT_OK, EXIT_SYNC_ISSUES, Aborted,
                     ConfigError, FatalError, PreflightError)
from .logging_setup import (DEFAULT_BACKUPS, DEFAULT_MAX_BYTES, LEVEL_NAMES, MAIN,
                            configure_std_streams, log_context, setup_logging)
from .runner import run_databases
from .syncer import Syncer

log = logging.getLogger(__name__)


@dataclass
class Plan:
    names: list
    missing: list = field(default_factory=list)
    collide: list = field(default_factory=list)
    case_fixes: list = field(default_factory=list)
    ver_src: str = ""
    ver_dst: str = ""


def _int_at_least(lo: int):
    """命令行整数参数的下限校验。0/负数的轮转参数会静默变成"永不切分"，比报错更难查。
    argparse 会自动在消息前面补上参数名。"""
    def parse(value):
        try:
            num = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"必须是整数，当前 {value!r}") from None
        if num < lo:
            raise argparse.ArgumentTypeError(f"不能小于 {lo}，当前 {num}")
        return num

    return parse


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="mysql_sync.py",
        description="MySQL 整库同步（结构 + 数据），支持库级与库内表级并行",
        epilog="先跑 --dry-run 看清要动哪些库；正式跑用 --yes 跳过交互确认。",
    )
    ap.add_argument("-c", "--config", default="sync_config.ini", help="配置文件，默认 %(default)s")
    ap.add_argument("-d", "--databases", default="databases.txt", help="库清单文件，默认 %(default)s")
    ap.add_argument("--only", nargs="+", metavar="DB", help="只同步这些库，忽略清单文件")
    ap.add_argument("--exclude", nargs="*", default=[], metavar="DB", help="从清单中剔除这些库")
    ap.add_argument("--workers", type=int, help="覆盖配置里的并发库数")
    ap.add_argument("--table-workers", dest="table_workers", type=int,
                    help="覆盖配置里的库内并发表数")
    ap.add_argument("--start-from", dest="start_from", metavar="DB",
                    help="从清单中该库开始，之前的库跳过（断点续跑）")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="只做预检并打印计划，不碰目标端")
    ap.add_argument("--yes", action="store_true", help="跳过 DROP 交互确认，无人值守时用")
    ap.add_argument("--log-file", dest="log_file", metavar="FILE",
                    help="日志同时写入该文件（带轮转，父目录会自动创建）")
    ap.add_argument("--log-level", dest="log_level", default="INFO",
                    choices=LEVEL_NAMES, type=str.upper, help="日志级别，默认 INFO")
    ap.add_argument("--log-max-mb", dest="log_max_mb",
                    type=_int_at_least(1),
                    default=DEFAULT_MAX_BYTES // (1024 * 1024),
                    help="单个日志文件达到该大小就轮转，默认 %(default)s MB")
    ap.add_argument("--log-backups", dest="log_backups",
                    type=_int_at_least(0), default=DEFAULT_BACKUPS,
                    help="轮转保留几个历史文件，默认 %(default)s；0 = 只留当前文件")
    ap.add_argument("--log-dateutc", dest="log_dateutc", action="store_true",
                    help="日志时间戳用 UTC（跳板机与数据库时区不一致时用）")
    ap.add_argument("-V", "--version", action="version", version=f"mysqlsync {__version__}")
    return ap


def main(argv=None) -> int:
    configure_std_streams()
    args = build_parser().parse_args(argv)
    setup_logging(level=args.log_level, log_file=args.log_file,
                  max_bytes=args.log_max_mb * 1024 * 1024, backups=args.log_backups,
                  utc=args.log_dateutc)
    log.debug("日志就绪 level=%s file=%s 单文件上限=%dMB 保留=%d UTC=%s",
              args.log_level, args.log_file or "（未写文件）", args.log_max_mb,
              args.log_backups, args.log_dateutc)
    log.info("执行命令：%s", " ".join(sys.argv if argv is None else ["mysql_sync.py", *argv]))
    try:
        code = run(args)
    except FatalError as exc:
        log.error("%s", exc)
        return EXIT_FATAL
    except KeyboardInterrupt:
        log.error("已中断（Ctrl-C）。已同步完成的库不会回滚，重跑用 --start-from 或 --only 补剩余的库")
        return EXIT_INTERRUPTED
    except Exception:  # noqa: BLE001 - 崩溃也要留下完整现场
        log.exception("未预期的异常，进程中止")
        return EXIT_FATAL
    log.info("进程结束，退出码 %d", code)
    return code


def run(args) -> int:
    """一次完整执行：读配置 → 选库 → 预检 → 确认 → 同步。返回退出码。"""
    settings = load_settings(args.config)
    opts = settings.options
    apply_overrides(opts, args)
    log.debug("参数快照 %s", settings.snapshot())

    names = list(args.only) if args.only else load_databases(args.databases)
    validate_database_names(names)
    names = select_names(names, args)
    plan = preflight(settings, names)
    report.log_preflight(settings.source.display, settings.target.display,
                         plan.ver_src, plan.ver_dst, plan.names, plan.collide,
                         plan.missing, plan.case_fixes, opts)

    if args.dry_run:
        report.log_plan(plan.names, plan.collide)
        return EXIT_SYNC_ISSUES if plan.missing else EXIT_OK

    confirm_destructive(settings, plan, args)
    return sync_all(settings, plan)


def apply_overrides(opts, args) -> None:
    for attr, value in (("workers", args.workers), ("table_workers", args.table_workers)):
        if value is None:
            continue
        if value < 1:
            raise ConfigError(f"--{attr.replace('_', '-')} 不能小于 1，当前 {value}")
        setattr(opts, attr, value)


def select_names(names, args) -> list:
    skip = {x.lower() for x in args.exclude}
    names = [n for n in names if n.lower() not in skip]
    if args.start_from:
        key = args.start_from.lower()
        idx = next((i for i, n in enumerate(names) if n.lower() == key), None)
        if idx is None:
            raise ConfigError(f"--start-from {args.start_from} 不在待同步列表中")
        names = names[idx:]
    if not names:
        raise ConfigError("待同步数据库列表为空")
    return names


def server_info(ep):
    conn = db.connect(ep, autocommit=True)
    try:
        cur = conn.cursor()
        version = db.scalar(cur, "SELECT VERSION()")
        schemas = metadata.existing_schemas(cur)
        lctn = db.scalar(cur, "SELECT @@lower_case_table_names")
        return version, schemas, lctn
    finally:
        conn.close()


def preflight(settings, names) -> Plan:
    try:
        ver_src, src_schemas, lctn_src = server_info(settings.source)
        ver_dst, dst_schemas, lctn_dst = server_info(settings.target)
    except (pymysql.MySQLError, OSError, ValueError) as exc:
        # ValueError：密码含非 latin-1 字符时 pymysql 在发握手包前就会抛
        raise PreflightError(f"预检连接失败：{exc}") from None
    if str(lctn_src) != str(lctn_dst):
        # 大小写敏感策略不一致时，目标端会把库表名按自己的规则改写，事后极难发现
        raise PreflightError(
            f"两端 lower_case_table_names 不一致（源 {lctn_src} / 目标 {lctn_dst}），"
            "先对齐目标实例的 my.cnf 再同步")

    # 清单里的大小写可能和实例中的真实库名不同，一律以源端为准
    by_lower = {n.lower(): n for n in src_schemas}
    resolved, missing, case_fixes = [], [], []
    for name in names:
        if name in src_schemas:
            resolved.append(name)
        elif name.lower() in by_lower:
            real = by_lower[name.lower()]
            case_fixes.append((name, real))
            resolved.append(real)
        else:
            missing.append(name)
    resolved = list(dict.fromkeys(resolved))
    if not resolved:
        raise PreflightError(f"没有可同步的数据库：清单里的 {len(missing)} 个在源实例都不存在")
    return Plan(names=resolved, missing=missing, case_fixes=case_fixes,
                collide=[n for n in resolved if n in dst_schemas],
                ver_src=ver_src, ver_dst=ver_dst)


def confirm_destructive(settings, plan, args) -> None:
    """破坏面确认。--yes 之外还要读 stdin，非交互环境必须明确失败而不是崩在 EOFError。"""
    if args.yes or not (settings.options.drop_existing and plan.collide):
        return
    with log_context(MAIN):
        shown = ", ".join(plan.collide[:20]) + (" …" if len(plan.collide) > 20 else "")
        log.warning("即将 DROP 并重建目标端已存在的 %d 个库，原数据不可恢复：%s",
                    len(plan.collide), shown)
        try:
            answer = input("确认继续？输入 yes：")
        except EOFError:
            raise Aborted("非交互环境（nohup / 管道）下拿不到确认，请显式加 --yes") from None
        if answer.strip().lower() != "yes":
            raise Aborted("已取消，未触碰目标端")


def sync_all(settings, plan) -> int:
    opts = settings.options
    syncer = Syncer(settings.source, settings.target, opts)
    results = []
    total = len(plan.names)
    started = time.monotonic()

    def on_result(res):
        # on_result 由调度线程按完成顺序串行调用，不需要额外加锁
        results.append(res)
        report.log_db_result(res, len(results), total)

    run_databases(plan.names, opts.workers, syncer.sync_database, on_result)
    return report.log_final(results, plan.missing, time.monotonic() - started).exit_code
