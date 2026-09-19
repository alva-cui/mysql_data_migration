"""跨 MySQL 实例整库同步（结构 + 数据），不依赖 mysqldump。

模块划分：
    config        配置文件、库清单、库名校验
    db            SQL 文本与会话工具（标识符转义、时区折算、DEFINER 剥离）
    metadata      源端元数据读取
    ddl           目标端 DDL 落盘
    channel       传输通道：一致性快照 + 分批流式拷贝
    syncer        单库编排与库内表级并行
    runner        库级并行调度
    report        运行报告（控制台与日志文件同一份）
    logging_setup 日志初始化与并发上下文标签
    cli           命令行入口
"""

import sys

try:
    import pymysql  # noqa: F401  只在这里探测一次，好让缺依赖时是一句人话而不是 traceback
except ImportError:
    sys.exit("缺少依赖，请先执行：python -m pip install pymysql")

__version__ = "1.0.0"
