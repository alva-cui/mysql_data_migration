"""异常与退出码。

除 CLI 外，模块一律不 sys.exit：致命问题抛 FatalError 子类，由 cli.main 统一转成
一条 ERROR 日志 + 退出码，这样原因既进控制台也进日志文件。
"""

EXIT_OK = 0
EXIT_SYNC_ISSUES = 1  # 跑完了，但有库/表没搬成功，或清单里有库在源端不存在
EXIT_FATAL = 2        # 没跑起来：配置、清单、命令行参数、预检连接不合法
EXIT_INTERRUPTED = 130  # Ctrl-C


class FatalError(RuntimeError):
    """无法继续，原因已经写成给人看的话。"""


class ConfigError(FatalError):
    """配置文件 / 库清单 / 命令行参数不合法。"""


class PreflightError(FatalError):
    """预检阶段就失败：连不上两端、清单里没有一个库可同步。"""


class Aborted(FatalError):
    """用户在 DROP 确认处拒绝，或非交互环境拿不到确认。"""
