"""单库同步编排 + 库内表级并行。

一致性语义：每条通道在自己的源连接上开一个一致性快照事务，通道内所有表读到的是同一时刻。
table_workers=1 时单库全表同一快照；>1 时各通道快照独立，单库内跨表不再严格同时。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

import pymysql

from . import db, ddl, metadata
from .channel import Channel
from .config import Endpoint, Options
from .errors import FatalError
from .logging_setup import log_context
from .results import DbResult, note_error

log = logging.getLogger(__name__)


class Syncer:
    def __init__(self, src: Endpoint, dst: Endpoint, opts: Options):
        self.src, self.dst, self.opts = src, dst, opts

    # ---- 单库 ----

    def sync_database(self, dbname) -> DbResult:
        res = DbResult(name=dbname)
        started = time.monotonic()
        with log_context(dbname):
            # connect 走模块属性调用，selftest 才能整体替换成假连接
            meta = db.connect(self.src, autocommit=False)
            conn = db.connect(self.dst, autocommit=False)
            try:
                mcur, wcur = meta.cursor(), conn.cursor()
                tz = db.tz_offset(mcur)
                batch_bytes, packet = db.max_batch_bytes(wcur, self.opts.batch_bytes)
                mcur.execute("SET SESSION max_execution_time = 0")

                charset, collation = metadata.schema_charset(mcur, dbname)
                tables, views = metadata.list_tables(mcur, dbname)
                res.found.update(tables=len(tables), views=len(views))
                log.info("开始同步：表 %d 视图 %d｜源库字符集 %s/%s｜会话 tz=%s"
                         "｜单批上限 %d 行 / %d 字节（max_allowed_packet=%d）",
                         len(tables), len(views), charset, collation, tz,
                         self.opts.batch_rows, batch_bytes, packet)

                self._create_schema(wcur, conn, dbname, charset, collation)
                items = self._create_tables(mcur, wcur, conn, dbname, tables, res)
                self._transfer_data(dbname, items, res, tz, batch_bytes)
                self._create_dependents(mcur, wcur, conn, dbname, views, res)

                conn.commit()
                meta.commit()
            except FatalError as exc:
                note_error(res, log, str(exc))
            except pymysql.MySQLError as exc:
                note_error(res, log, f"元数据阶段失败：{exc}")
                log.debug("元数据阶段失败现场", exc_info=True)
            finally:
                meta.close()
                conn.close()
            res.seconds = time.monotonic() - started
        return res

    def _create_schema(self, wcur, conn, dbname, charset, collation) -> None:
        if self.opts.drop_existing:
            log.info("即将 DROP 重建目标库：%s（原数据不可恢复）", dbname)
        ddl.create_schema(wcur, dbname, charset, collation, self.opts.drop_existing)
        conn.commit()
        ddl.select_schema(wcur, dbname)
        conn.commit()
        log.debug("目标库已建好并选中：%s", dbname)

    def _create_tables(self, mcur, wcur, conn, dbname, tables, res) -> list:
        items = ddl.create_tables(mcur, wcur, dbname, tables, res)
        conn.commit()
        res.tables = len(items)
        log.info("建表完成：成功 %d / 源端 %d，开始传数据", len(items), len(tables))
        return items

    def _create_dependents(self, mcur, wcur, conn, dbname, views, res) -> None:
        """视图、触发器、例程、事件。表和数据的阶段走完才轮到它们，
        提前创建会引用到尚不存在的对象。"""
        opts = self.opts
        if opts.include_views and views:
            res.views = ddl.create_views(mcur, wcur, dbname, views, res)
            conn.commit()
            log.info("视图已创建 %d / %d", res.views, len(views))
        if opts.include_triggers:
            names = metadata.list_triggers(mcur, dbname)
            res.found["triggers"] = len(names)
            if names:
                res.created_triggers = ddl.create_triggers(mcur, wcur, dbname, names, res)
                log.info("触发器已创建 %d / %d", res.created_triggers, len(names))
        if opts.include_routines:
            routines = metadata.list_routines(mcur, dbname)
            res.found["routines"] = len(routines)
            if routines:
                res.created_routines = ddl.create_routines(mcur, wcur, dbname, routines, res)
                log.info("存储过程/函数已创建 %d / %d", res.created_routines, len(routines))
        if opts.include_events:
            names = metadata.list_events(mcur, dbname)
            res.found["events"] = len(names)
            if names:
                res.created_events = ddl.create_events(mcur, wcur, dbname, names, res)
                log.info("定时事件已创建 %d / %d", res.created_events, len(names))
        conn.commit()

    # ---- 库内表级并行 ----

    def _transfer_data(self, dbname, items, res, tz, batch_bytes) -> None:
        if not items:
            log.info("没有需要传输数据的表")
            return
        log.debug("开始传数据：待传 %d 张表", len(items))
        self.transfer_tables(dbname, items, res, tz, batch_bytes)

    def transfer_tables(self, dbname, items, res, tz, batch_bytes) -> None:
        """items 为 [(table, cols)]。起 min(table_workers, 表数) 条通道，每条一个线程，
        都从同一个待办队列取表，于是大表先落地、小表填空，天然负载均衡。"""
        width = max(1, min(self.opts.table_workers, len(items)))
        pending = deque(items)
        started = time.monotonic()
        log.info("传输通道 %d 条并行", width)

        def worker(idx: int) -> None:
            threading.current_thread().name = f"{dbname}-ch{idx}"
            with log_context(f"{dbname}/ch{idx}"):
                ch = self._open_channel(res, tz, batch_bytes)
                if ch is not None:
                    try:
                        self._drain_queue(ch, dbname, res, pending, tz, batch_bytes)
                    finally:
                        ch.close()

        threads = [threading.Thread(target=worker, args=(i + 1,), daemon=True)
                   for i in range(width)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        log.info("数据阶段结束：写入 %s 行 耗时 %.0fs", f"{res.rows:,}", time.monotonic() - started)

    def _open_channel(self, res, tz, batch_bytes):
        try:
            return Channel(self.src, self.dst, self.opts, tz, batch_bytes)
        except pymysql.MySQLError as exc:
            # 通道建不起来必须记进 errors，否则会出现"报告成功但少表"
            note_error(res, log, f"传输通道建立失败，该库有表未同步：{exc}")
            return None

    def _drain_queue(self, ch, dbname, res, pending, tz, batch_bytes) -> None:
        while True:
            try:
                table, cols = pending.popleft()
            except IndexError:
                return
            try:
                self._copy_one(ch, dbname, table, cols, res)
            except pymysql.MySQLError as exc:
                note_error(res, log, f"{table}: 数据拷贝失败 {exc}")
                ch.close()
                # 连接可能已被服务端掐断，换一条干净通道继续跑剩下的表
                rebuilt = self._open_channel(res, tz, batch_bytes)
                if rebuilt is None:
                    note_error(res, log, "通道重建失败，该库剩余表未同步")
                    pending.clear()
                    return
                log.warning("已换新通道，剩余 %d 张表继续", len(pending))
                ch = rebuilt

    def _copy_one(self, ch, dbname, table, cols, res) -> None:
        started = time.monotonic()
        rows = ch.copy_table(dbname, table, cols)
        if self.opts.verify:
            src_n = ch.count(dbname, table, on_src=True)
            dst_n = ch.count(dbname, table)
            if src_n != dst_n:
                note_error(res, log, f"{table}: 行数不符 源 {src_n} / 目标 {dst_n}")
        res.add_rows(rows)
        log.info("%s  %s 行  %.1fs", table, f"{rows:,}", time.monotonic() - started)
