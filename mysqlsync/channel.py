"""传输通道：一对源/目标连接，自带一致性快照事务。一个库起 table_workers 条。"""

from __future__ import annotations

import logging
import time

from pymysql.cursors import SSCursor

from . import db
from .config import Endpoint, Options

log = logging.getLogger(__name__)


class Channel:
    def __init__(self, src: Endpoint, dst: Endpoint, opts: Options, tz: str, batch_bytes: int):
        self.opts = opts
        self.batch_bytes = batch_bytes
        self.tz = tz
        t0 = time.monotonic()
        self.src = db.connect(src, autocommit=False)
        self.dst = db.connect(dst, autocommit=False)

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
        log.debug("通道就绪 快照已开 time_zone=%s sql_mode=%r 单批上限=%d 字节 耗时 %.2fs",
                  tz, opts.sql_mode, batch_bytes, time.monotonic() - t0)

    def close(self) -> None:
        for conn in (self.src, self.dst):
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 收尾关不掉就算了，不能盖掉真正的错误
                pass

    def count(self, dbname, table, on_src=False) -> int:
        cur = self.src.cursor() if on_src else self.dst.cursor()
        return int(db.scalar(cur, f"SELECT COUNT(*) FROM {db.qn(dbname, table)}") or 0)

    def copy_table(self, dbname, table, cols) -> int:
        """流式读 + 分批写，不在内存里堆整表。返回写入行数。

        cols 是两端共用的一份列清单（生成列已排除），SELECT 与 INSERT 都用它，
        各查各的会报 The value specified for generated column is not allowed。"""
        if not cols:
            log.debug("%s 无可写列（整表皆为生成列），不传数据", table)
            return 0
        batch_rows = self.opts.batch_rows
        select_sql = f"SELECT {', '.join(map(db.q, cols))} FROM {db.qn(dbname, table)}"
        insert_sql = "INSERT INTO {} ({}) VALUES ({})".format(
            db.qn(dbname, table),
            ", ".join(map(db.q, cols)),
            ", ".join(["%s"] * len(cols)),
        )
        log.debug("%s 开始传输 可写列 %d：%s", table, len(cols), select_sql)
        total = 0
        wcur = self.dst.cursor()
        scur = self.src.cursor(SSCursor)
        scur.execute(select_sql)
        buf, size = [], 0

        def flush():
            nonlocal total, buf, size
            wcur.executemany(insert_sql, buf)
            self.dst.commit()
            total += len(buf)
            log.debug("%s 落盘 %d 行 约 %d 字节 累计 %d 行", table, len(buf), size, total)
            buf, size = [], 0

        while True:
            rows = scur.fetchmany(batch_rows)
            if not rows:  # SSCursor 取到空列表即结果集耗尽
                break
            for row in rows:
                if buf and (len(buf) >= batch_rows or size + db.row_bytes(row) > self.batch_bytes):
                    flush()
                buf.append(row)
                size += db.row_bytes(row)
        if buf:
            flush()
        # SSCursor 的结果集必须在同一连接上耗尽后再关，否则 pymysql 会报
        # packet sequence 错乱；close 前不能在这条源连接上插别的查询
        scur.close()
        log.debug("%s 传输完成 %d 行", table, total)
        return total
