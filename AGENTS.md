# 项目说明：MySQL 整库同步

运维工具，把源实例的一批数据库整体拷到目标实例。交付对象是在跳板机上执行的 DBA/运维，
不是 Web 应用——全局规则里的前后端职责隔离、后端 ORM 查询规范在此不适用。

逻辑在 `mysqlsync/` 包里按职责分模块（划分见 `mysqlsync/__init__.py` 的文档）。根目录的
`mysql_sync.py` 只是入口壳，为的是保住 `python mysql_sync.py` 这个既有写法；
`python -m mysqlsync` 与之等价。**不要把它改回单文件，也不要在入口壳里写逻辑。**
调用方有两个：`tools/verify_sync.py`（只读核验）与 `tests/selftest.py`（假连接自测），
它们都直接把包当模块用，入口路径动了它们就断。

## 验证

改动后必须跑：

```bash
python tests/selftest.py
```

它用假连接覆盖 SQL 生成、分批、并行调度、DDL 执行顺序、DEFINER 剥离、日志契约、配置校验等
121 项断言，全绿才提交（失败时退出码为 1）。

它验证不了真实表结构兼容性。本机配了两套实例可直接真跑：源 `127.0.0.1:3306`（里面有个
现成的杂活库 `ag1680`，126 表 / 1 视图 / 178 个生成列与表达式默认值列），目标
`127.0.0.1:13306`，连接信息在 `sync_config.ini`。涉及线上行为时先按 README 第 9 节步骤 2
拿单个库 `--only` 试跑，再用 `python tools/verify_sync.py 库名` 核验。

## 提交与配置

- `sync_config.ini` 含明文密码，已被 `.gitignore` 排除，**只提交 `sync_config.ini.example`**。
  同理，任何带 `password =` 键的 ini 都不要为了测试而提交进仓库——自测的假配置以
  `SAMPLE_INI` 字符串形式写在 `tests/selftest.py` 里，运行时临时落盘
- 新增配置项要同时改三处：`sync_config.ini.example`、`Options` 数据类、README 第 4 节的表格。
  这三处极易漂移，改一处就必须改齐
- `selftest.py` 里的假游标是按需匹配 SQL 的，给同步流程**新增任何一条查询**都要同步补
  `SRC_SCRIPT` / `DST_SCRIPT` 的 pattern，否则新查询会被当成"读到空结果"而静默失效
- `.gitattributes` 强制 LF，脚本要在 Linux 上直接 `./mysql_sync.py` 执行

## 容易改坏的核心设计

以下都是刻意决策，动之前先确认自己理解为什么：

- **生成列必须同时从 `SELECT` 和 `INSERT` 的列清单里排除**（`insertable_columns`）。
  两边共用一份列清单是有意为之，改成各查各的会报 `The value specified for generated
  column is not allowed`。带表达式默认值的列 `EXTRA` 是 `DEFAULT_GENERATED`，属可写列，
  不要误排。
- **SSCursor 的结果集必须在同一连接上耗尽**，否则 pymysql 会报 packet sequence 错乱。
  `copy_table` 里 `scur.close()` 前不能在同一源连接上插入其他查询。
- **两端会话 `time_zone` 必须设成同一个数字偏移**（`tz_offset`），不能用 `Asia/Shanghai`
  这类命名时区——目标端通常没导入 `mysql.time_zone` 表。偏移不一致时 `TIMESTAMP` 列会整体漂移。
- **`strip_definer` 只替换第一处 `DEFINER=`**（`count=1`）。只有开头那个子句是定义者声明；
  改成全量替换会连视图/例程定义体里出现的 `DEFINER=xxx` 字面量一起吃掉（实测会把字符串常量清空）。
  `SQL SECURITY DEFINER` 不含等号，不受影响，必须原样保留。
- **DDL 顺序固定**：建库 → 建表 → 传数据 → 建视图 → 触发器/存储过程/事件。视图和例程提前
  创建会引用到尚不存在的对象。
- **建库后必须 `USE` 选中目标库，再执行 `SHOW CREATE` 拿来的 DDL**。`SHOW CREATE TABLE` /
  `PROCEDURE` 的输出不带库名前缀，会话没选中库就执行会整库报 `1046 No database selected`；
  而 `SHOW CREATE VIEW` 是带前缀的，所以只有视图能建上——症状最容易看漏。selftest 的假游标
  不校验默认库，这个坑只有真跑或靠"建表前已 USE 选中目标库"那条断言才兜得住。
- **`batch_bytes` 实际会再压到目标端 `max_allowed_packet` 的 60%**，不是配置值直通。
  去掉这层折算就会撞上 `MySQL server has gone away`。
- **`table_workers = 1` 是"单库内跨表同一快照"的唯一保证**，也是所有通道各自事务的根源。
  调整默认值等于修改对用户的语义承诺，需同步更新 README 第 6 节。
- **导入期 `sql_mode` 默认空字符串是有意的**，为了让源库历史遗留的零日期能原样写入。
- 单表拷贝失败后**换新通道继续**，而不是中断整库；通道建立失败必须记进 `res.errors`，
  否则会出现"报告成功但少表"。
- **错误只有一个入口 `results.note_error`**：它同时写 `res.errors`（汇总报告据此统计）和
  ERROR 日志。自己 `res.errors.append(...)` 或只 `log.error` 都会让两边漂移成
  "报告说有错、日志里没有"。
- **日志必须有上下文列**。库级与通道级线程入口各自 `with log_context(...)`（`syncer.py` 里的
  `dbname` 与 `dbname/chN`）；contextvar 不被子线程继承，忘了设就只剩一条无法定位的裸消息。
  两个 handler 都必须挂 formatter 和 `ContextFilter`——文件 handler 不挂 formatter 就是
  README 第 9 节记过的那个坑（落盘只剩消息正文，`grep ERROR` 搜不到东西）。
- **库名不再往消息正文里塞**。旧版每条日志手写 `[dbname]` 前缀，现在由上下文列统一带；
  两处都写就是重复。但 `res.errors` 里的文字仍要自带 `dbname.object`，因为汇总报告会脱离
  上下文单独打印它们。
- **连接与通道的构造是 selftest 的替换点**：一律写成 `db.connect(...)`（模块属性调用）和
  `Channel(...)`（syncer 模块全局名），假连接/假通道靠替换这两个名字注入。改成
  `from .db import connect` 后直接调 `connect(...)`，selftest 的补丁会失效。
  `Channel(src, dst, opts, tz, batch_bytes)` 的签名与 `tests/selftest.py` 里的假通道一一对应，
  加参数要同步改假通道。
- **密码只能出现在 `Endpoint.password`**。它带 `repr=False`，日志要端点就用
  `Endpoint.display`（`user@host:port`）。把 `Options`/`Endpoint` 整个 `%r` 出来是安全的，
  把连接 kwargs 字典直接打出来不是。
- **退出码是交付语义的一部分**（`errors.py` 里的常量）：0 干净、1 跑完但有失败或有缺失库、
  2 根本没跑起来（含在 DROP 确认处取消）、130 Ctrl-C。改动要同步 README 第 5 节的表。
- **预检比两端的 `lower_case_table_names`，不一致直接拦死**。这是刻意的硬失败：规则不同时
  目标端会按自己的规则改写库表名，搬完看不出来。不要为了"灵活"把它降级成告警或加开关。

## 范围边界

明确不做，不要以"完善"为名加进来：

- 不引入 `mysqldump` / `mysql` 客户端或任何 `subprocess` 调用，环境上通常没有
- 不做增量同步、表级 diff、断点续传到表粒度；重跑就是整库 DROP 重建
- 不同步数据库账号与库表级授权（已确认的需求边界）
- 不改服务端全局变量（含 `event_scheduler`），只在会话内设置
- 不支持 MySQL 5.7 及更低版本，两端固定 8.0+，不要写兼容分支
- 不引入 `loguru`/`structlog`/`click` 之类的新依赖，运行时依赖只有 pymysql；
  日志用标准库 `logging`，命令行用 `argparse`
- 日志开关（文件、级别、轮转、UTC）只做命令行参数，不要挪进 `sync_config.ini`。
  放进去就要同时改三处（见上一节），而它们属于"这一次怎么跑"而不是"这套环境是什么"
