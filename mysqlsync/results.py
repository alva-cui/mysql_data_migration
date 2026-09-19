"""单库同步结果与错误记账。

错误只有一个入口 note_error：同时进 DbResult.errors（供结尾汇总报告统计）和 ERROR 级日志
（供事后按级别检索）。分开写迟早会漂移成「报告说有错、日志里没有」或反之。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field


@dataclass
class DbResult:
    name: str
    # created_*：真正在目标端建起来的对象数（tables/views 保留原名，历史报告与 selftest 依赖）
    tables: int = 0
    views: int = 0
    created_triggers: int = 0
    created_routines: int = 0
    created_events: int = 0
    rows: int = 0
    seconds: float = 0.0
    errors: list = field(default_factory=list)
    # 源端看到的各类对象总数。和 created_* 并排打印，就是 README 第 10 节要的
    # 「逐类计数比对」，用于兜住权限不足导致的静默少对象
    found: dict = field(default_factory=dict)
    # 同一库内多条通道并发累加，计数器与错误列表都要过这把锁
    lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False,
                                compare=False)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add_rows(self, n: int) -> None:
        with self.lock:
            self.rows += n

    def object_pairs(self) -> list:
        """[(标签, 已建, 源端可见)]，只保留两端至少有一边非零的类别。"""
        pairs = [
            ("表", self.tables, self.found.get("tables", 0)),
            ("视图", self.views, self.found.get("views", 0)),
            ("触发器", self.created_triggers, self.found.get("triggers", 0)),
            ("例程", self.created_routines, self.found.get("routines", 0)),
            ("事件", self.created_events, self.found.get("events", 0)),
        ]
        return [(label, got, seen) for label, got, seen in pairs if got or seen]


def note_error(res: DbResult, logger: logging.Logger, msg: str) -> None:
    with res.lock:
        res.errors.append(msg)
    logger.error("%s", msg)
