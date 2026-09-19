"""库级并行调度。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .logging_setup import log_context
from .results import DbResult

log = logging.getLogger(__name__)


def run_databases(names, workers: int, sync_one: Callable, on_result: Callable) -> None:
    """按 workers 并发跑库。单库抛出的任何异常都就地记成该库的错误，不拖垮整批。"""
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="db") as pool:
        futures = {pool.submit(sync_one, name): name for name in names}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001 - 一个库的崩溃不该带走整批
                res = DbResult(name=name, errors=[f"未捕获异常：{exc!r}"])
                with log_context(name):
                    log.exception("未捕获异常，该库中止")
            on_result(res)
