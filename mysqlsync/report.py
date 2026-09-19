"""运行报告。

所有输出都走 logging，不再有 print：控制台看到的每一行都会同样落进 --log-file。
旧版结尾那张汇总表只打标准输出，nohup 之外重定向掉就等于把唯一一份总体结论丢了。

每段报告都是独立的日志记录（不是一整块多行文本），这样按级别、按库名检索都取得到。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .config import Options
from .logging_setup import PLAN, PRECHECK, REPORT, log_context

log = logging.getLogger(__name__)

MAX_DETAIL_PER_DB = 3


def human_bytes(n: Optional[int]) -> str:
    if not n:
        return "0"
    for unit, size in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"


def _flag(value: bool) -> str:
    return "on" if value else "off"


def _pairs_text(pairs) -> str:
    return " ".join(f"{label} {got}/{seen}" for label, got, seen in pairs) or "无对象"


def log_preflight(src_display, dst_display, ver_src, ver_dst, plan_names, collide,
                  missing, case_fixes, opts: Options) -> None:
    """启动摘要。这一段是事后判断"到底动了哪些库、用什么参数动的"的唯一依据。"""
    with log_context(PRECHECK):
        log.info("源端 MySQL %s @ %s → 目标端 MySQL %s @ %s", ver_src, src_display,
                 ver_dst, dst_display)
        if not opts.drop_existing and collide:
            log.warning("drop_existing=false 且目标端已存在 %d 个同名库，建表会报「已存在」；"
                        "这只适合全新目标端", len(collide))
        exist_note = ("无同名库" if not collide else
                      ("将被 DROP 重建" if opts.drop_existing else "不会 DROP，建表会报已存在"))
        log.info("待同步 %d 个库｜目标端已存在 %d 个（%s）｜源端缺失 %d 个",
                 len(plan_names), len(collide), exist_note, len(missing))
        log.info("并发 %d 库 x %d 表 = %d 条传输通道｜峰值连接约 %d 条（源、目标各 %d 条）",
                 opts.workers, opts.table_workers, opts.workers * opts.table_workers,
                 opts.workers * (opts.table_workers + 1) * 2,
                 opts.workers * (opts.table_workers + 1))
        log.info("策略 drop_existing=%s views=%s triggers=%s routines=%s events=%s verify=%s "
                 "sql_mode=%r", _flag(opts.drop_existing), _flag(opts.include_views),
                 _flag(opts.include_triggers), _flag(opts.include_routines),
                 _flag(opts.include_events), _flag(opts.verify), opts.sql_mode)
        log.info("批量 batch_rows=%d batch_bytes=%s net_timeout=%ds",
                 opts.batch_rows, human_bytes(opts.batch_bytes), opts.net_timeout)
        if case_fixes:
            # 逐条 WARNING 会在百库清单里刷屏，聚合成一条，明细降到 DEBUG
            for want, real in case_fixes:
                log.debug("库名大小写按源端纠正：%s → %s", want, real)
            log.warning("库名大小写与源端不一致，已按源端纠正 %d 个：%s", len(case_fixes),
                        ", ".join(f"{a}→{b}" for a, b in case_fixes[:5])
                        + (" …" if len(case_fixes) > 5 else ""))
        if missing:
            log.error("源实例中不存在 %d 个库，未参与同步：%s", len(missing), ", ".join(missing))


def log_plan(names, collide) -> None:
    """--dry-run 的逐库计划。"""
    exists = set(collide)
    with log_context(PLAN):
        log.info("预演计划：%d 个库（本模式不触碰目标端）", len(names))
        for name in names:
            log.info("%s%s", name,
                     "  [目标端已存在，正式跑时将被 DROP 重建]" if name in exists else "")


def log_db_result(res, done: int, total: int) -> None:
    with log_context(res.name):
        line = ("%s (%d/%d) %s 行  耗时 %.0fs"
                % ("完成" if res.ok else "失败", done, total, f"{res.rows:,}", res.seconds))
        pairs = _pairs_text(res.object_pairs())
        level = logging.INFO if res.ok else logging.ERROR
        log.log(level, "%s｜%s", line, pairs)
        if not res.ok:
            log.log(level, "本库错误 %d 条，明细见下方 ERROR 行", len(res.errors))


@dataclass
class Totals:
    dbs: int = 0
    ok: int = 0
    failed: int = 0
    rows: int = 0
    errors: int = 0
    objects: list = field(default_factory=list)
    elapsed: float = 0.0
    missing: int = 0
    exit_code: int = 0


def summarize(results, missing, elapsed) -> Totals:
    merged = {}
    for res in results:
        for label, got, seen in res.object_pairs():
            got0, seen0 = merged.get(label, (0, 0))
            merged[label] = (got0 + got, seen0 + seen)
    failed = [r for r in results if not r.ok]
    return Totals(
        dbs=len(results), ok=len(results) - len(failed), failed=len(failed),
        rows=sum(r.rows for r in results), errors=sum(len(r.errors) for r in failed),
        objects=[(label, got, seen) for label, (got, seen) in merged.items()],
        elapsed=elapsed, missing=len(missing),
        exit_code=1 if failed or missing else 0,
    )


def log_final(results, missing, elapsed) -> Totals:
    """结尾汇总。逐库结果行、失败明细、汇总三部分都进日志。"""
    results = sorted(results, key=lambda r: r.name)
    totals = summarize(results, missing, elapsed)
    with log_context(REPORT):
        log.log(logging.ERROR if totals.exit_code else logging.INFO,
                "RESULT 库=%d 成功=%d 失败=%d %s 行=%d 错误=%d 缺失库=%d 耗时=%.0fs 退出码=%d",
                totals.dbs, totals.ok, totals.failed,
                " ".join(f"{label}={got}/{seen}" for label, got, seen in totals.objects) or "无对象",
                totals.rows, totals.errors, totals.missing, totals.elapsed, totals.exit_code)
    for res in results:
        if res.ok:
            continue
        with log_context(res.name):
            log.error("失败明细 %d 条，前 %d 条：", len(res.errors), MAX_DETAIL_PER_DB)
            for msg in res.errors[:MAX_DETAIL_PER_DB]:
                log.error("- %s", msg)
            if len(res.errors) > MAX_DETAIL_PER_DB:
                log.error("- … 另有 %d 条未打印", len(res.errors) - MAX_DETAIL_PER_DB)
    with log_context(REPORT):
        failed = [r.name for r in results if not r.ok]
        if failed:
            shown = " ".join(failed[:10])
            more = f" …等 {len(failed)} 个库" if len(failed) > 10 else ""
            log.info("只重跑失败库：python mysql_sync.py --yes --only %s%s", shown, more)
        if totals.missing:
            log.info("清单里有 %d 个库在源端不存在，先核对 databases.txt（见 README 第 9 节步骤 0）",
                     totals.missing)
        # 提示语本身不能出现 " ERROR " 这个检索式，否则干净运行的错误计数会平白多 1
        log.info("筛出全部错误行：awk '$3==\"ERROR\"' 该日志文件")
    return totals
