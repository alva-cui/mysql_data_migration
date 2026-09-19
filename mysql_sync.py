#!/usr/bin/env python3
"""兼容既有写法的入口壳：python mysql_sync.py --yes

逻辑都在同级的 mysqlsync/ 包里（模块划分见 mysqlsync/__init__.py 的文档）。
用法、并行模型与一致性语义见 README；改代码前先看 AGENTS.md。

    python mysql_sync.py --dry-run     # 先看计划
    python mysql_sync.py --yes         # 正式同步
    python -m mysqlsync --dry-run      # 等价写法

要能在只读检出目录、或以绝对路径从别处调用时也能 import 到包，
先把脚本所在目录补进 sys.path。
"""

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from mysqlsync.cli import main  # noqa: E402  必须等 sys.path 就位

if __name__ == "__main__":
    raise SystemExit(main())
