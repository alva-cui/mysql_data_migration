# MySQL 整库同步脚本

把源实例中的一批数据库（表结构 + 数据 + 视图/触发器/存储过程/定时事件）整体搬到另一台
MySQL 实例。**纯 Python 实现，不依赖 `mysqldump` / `mysql` 客户端**，只需要能连上两端网络。

- 目标端：MySQL 8.0+（源端同为 8.0+）
- 一次配置，之后就是 `python mysql_sync.py --yes`
- 支持库级 + 库内表级双层并行
- 单库失败不影响其他库，可单独重跑

---

## 1. 环境准备

```bash
python -m pip install pymysql
```

Python 3.8 以上即可。脚本本身只有一个第三方依赖。

## 2. 三步跑起来

```bash
# ① 填连接信息
cp sync_config.ini.example sync_config.ini
vi sync_config.ini                    # 改 [source] / [target] 的 host/user/password

# ② 确认库清单（本项目已内置 100 个 AG 库）
vi databases.txt

# ③ 先预检，看清楚要动哪些库，这一步不碰目标端
python mysql_sync.py --dry-run

# ④ 正式同步
python mysql_sync.py --yes --log-file sync.log
```

`--dry-run` 会做三件事：连两端、比对版本、检查清单里的库在源端是否都存在（大小写不一致会
按源端真实库名纠正并告警）。**建议每次都先跑一遍。**

不加 `--yes` 时，若目标端存在同名库，脚本会列出会被 DROP 的库并要求输入 `yes` 确认。

## 3. 同步账号需要哪些权限

### 源端（只读）

```sql
CREATE USER 'sync_reader'@'10.0.0.%' IDENTIFIED BY '强密码';

-- 库名通配，一次覆盖 AG3139 / AG3138 ... 全部业务库
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT ON `AG%`.* TO 'sync_reader'@'10.0.0.%';

-- PROCESS：读 information_schema.EVENTS、以及库级一致性快照
GRANT PROCESS ON *.* TO 'sync_reader'@'10.0.0.%';

-- SHOW ROUTINE：MySQL 8 的动态权限。不给的话只能看到自己是定义者的例程，
-- 存储过程/函数会被跳过并在结果里报 "读不到定义"
GRANT SHOW ROUTINE ON *.* TO 'sync_reader'@'10.0.0.%';
```

只想同步表和数据、不要存储过程/函数时，`SHOW ROUTINE` 可以不给，把配置里的
`include_routines` 设为 `false` 即可消掉那几条报错。

### 目标端（读写）

```sql
CREATE USER 'sync_writer'@'10.0.0.%' IDENTIFIED BY '强密码';

-- 含 CREATE / DROP / ALTER / INSERT / CREATE VIEW / CREATE ROUTINE / EVENT / TRIGGER
GRANT ALL PRIVILEGES ON `AG%`.* TO 'sync_writer'@'10.0.0.%';
```

不需要 `SUPER`。脚本会主动剥掉视图/触发器/例程/事件 DDL 里的 `DEFINER=...`，让它们落在
当前执行账号上，因此目标账号不必具备设置任意 DEFINER 的权限。

## 4. 配置文件 `sync_config.ini`

用环境变量覆盖密码，可以让密码完全不落盘：

```bash
SYNC_SRC_PASSWORD='源库密码' SYNC_DST_PASSWORD='目标库密码' python mysql_sync.py --yes
```

> 该文件即将包含明文密码，仓库里只提交 `.example`，真身已在 `.gitignore` 中。

### `[source]` / `[target]`

| 键 | 说明 |
| --- | --- |
| `host` / `port` | TCP 连接。默认端口 3306 |
| `socket` | 填了就改走 Unix socket，`host`/`port` 被忽略（仅本机部署时可用） |
| `user` / `password` | 见上节权限；密码可被 `SYNC_SRC_PASSWORD` / `SYNC_DST_PASSWORD` 覆盖。注意 pymysql 握手要求密码可 latin-1 编码，含中文的密码请用环境变量传 |
| `charset` | 连接字符集，保持 `utf8mb4`。两端会自动做字符集转换，源库哪怕是 gbk/latin1 也能正确落到 utf8mb4 |

### `[sync]` 并发

两层并行，互不干扰：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `workers` | 4 | 同时同步几个**库** |
| `table_workers` | 4 | 单个库内同时传几张**表**（= 该库开几条传输通道） |

```
总并发传输 = workers × table_workers
峰值连接数 = workers × (table_workers + 1) × 2     源、目标各占一半
```

`+1` 是每个库额外占一条读元数据、执行建表 DDL 的连接。启动日志会把峰值连接数打印出来，
调大之前先确认两端的 `max_connections` 和同步账号的 `max_user_connections` 够不够。

| 场景 | 建议 |
| --- | --- |
| 100 个库、大小比较均匀 | `workers=4`，`table_workers=4` |
| 库很多但都是小表 | `workers=8`，`table_workers=1`（省连接数） |
| 只有 1 个超大库 | `workers=1`，`table_workers=8~16` |
| 源库是生产主库、怕压垮 | `workers=2`，`table_workers=2`，`batch_rows=500` |

### `[sync]` 批量与策略

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `batch_rows` | 1000 | 单批 `INSERT` 行数上限 |
| `batch_bytes` | 4194304 | 单批字节上限，还会自动压到目标端 `max_allowed_packet` 的 60% |
| `drop_existing` | true | `true` = 目标端同名库先 `DROP` 再整库重建。设 `false` 则不 DROP，此时目标端已存在该库会在建表时报「表已存在」 |
| `include_views` | true | 视图定义（不含数据） |
| `include_triggers` | true | 触发器 |
| `include_routines` | true | 存储过程与函数，需源端 `SHOW ROUTINE` 权限 |
| `include_events` | true | 定时事件 |
| `verify` | false | 每表拷完做一次两端 `COUNT(*)` 比对。源端要多一次全表扫描，千万行表上明显拖慢，只在需要交付凭证时开 |
| `sql_mode` | 空 | 导入期目标会话的 `sql_mode`。留空即完全宽松，让源库历史遗留的 `'0000-00-00'` 零日期能被原样写入 |
| `net_timeout` | 3600 | 目标会话的 `net_read_timeout` / `net_write_timeout`，防止大批次传输被服务端掐断 |

### `databases.txt`

一行一个库名，`#` 开头为注释，空行忽略，重复项自动去重。`mysql` /
`information_schema` / `performance_schema` / `sys` 属于系统库，出现在清单里会直接拒绝执行。

## 5. 命令行参数

```
-c, --config FILE          配置文件，默认 sync_config.ini
-d, --databases FILE       库清单文件，默认 databases.txt
    --only DB [DB ...]     只同步这些库，忽略清单文件
    --exclude DB [DB ...]  从清单中剔除这些库
    --workers N            覆盖配置里的并发库数
    --table-workers N      覆盖配置里的库内并发表数
    --start-from DB        从清单中该库开始，之前的库跳过（断点续跑）
    --dry-run              只预检并打印计划，不碰目标端
    --yes                  跳过 DROP 交互确认，无人值守时用
    --log-file FILE        日志同时追加写入文件
```

退出码：`0` 全部成功；`1` 有库失败或清单里有库在源端不存在。

## 6. 一致性与边界（务必读一遍）

**这不是主从复制，是一次性快照拷贝。** 同步过程中源库仍在写入时，目标端拿到的是「通道快照
建立那一刻」的数据，之后源库的变更不会跟过来。

- 每条传输通道在自己的源连接上执行 `START TRANSACTION WITH CONSISTENT SNAPSHOT`，通道内所有
  表读的是同一时刻。`table_workers=1` 时整个库共用一个快照，跨表严格同时（订单表和订单明细
  表不会错位）。
- `table_workers>1` 时各通道快照独立，**单库内跨表不再严格同时**。要跨表一致就把某一个大库
  单独用 `--only 库名 --table-workers 1` 跑。
- 严格意义上的一致性只能在业务停写或低峰期保证。要评估窗口，先跑一次 `--dry-run` 加
  `verify=true` 估时。
- 快照事务会让源端 undo 历史增长。单个大库跑几小时时，注意监控源端
  `SHOW ENGINE INNODB STATUS` 里的 history list length。

**同步不到的东西**（需要时请手工处理）：

- 数据库账号与库表级授权 —— 本次按需求明确不同步
- 服务端配置（`my.cnf`、`innodb_buffer_pool_size`、大小写敏感 `lower_case_table_names`）
- binlog 位点、GTID —— 拷贝后目标端是全新起点，不能直接接回原复制拓扑
- 表空间、数据文件等物理层内容（逻辑拷贝，非物理拷贝）
- 非 `BASE TABLE` / `VIEW` 的对象，会打告警并跳过
- 目标端 `event_scheduler` 默认不运行，脚本不改全局开关。需要事件真的跑起来，
  自己在目标端执行 `SET PERSIST event_scheduler = ON;`

**几个已知的处理细节**：

- 生成列（`VIRTUAL` / `STORED GENERATED`）会自动从 `SELECT` 和 `INSERT` 列表中排除，
  否则会报 `The value specified for generated column is not allowed`。带表达式默认值的列
  不受影响，正常写入。
- 两端会话被强制设成同一个**数字**时区偏移（如 `+08:00`），这样源端读出的 `TIMESTAMP` 写回
  目标才会落到同一个 epoch。用数字偏移而不是 `Asia/Shanghai` 这类命名时区，是为了不依赖
  目标端事先导入 `mysql.time_zone` 时区表。
- 外键和唯一性检查在导入会话内关闭，所以建表顺序、数据写入顺序不会触发约束失败。
- `CREATE DATABASE` 沿用源库的默认字符集与排序规则。

## 7. 失败处理与续跑

单个库失败不会中断整批。结束时打印汇总表，并列出每个失败库的前 3 条错误：

```bash
# 只重跑失败的几个库
python mysql_sync.py --yes --only AG3137 AG3126

# 中途断了，从某个库继续（按 databases.txt 的顺序）
python mysql_sync.py --yes --start-from AG3105

# 某个库内部只有几张表报错，先单独把它的对象类型关掉定位问题
python mysql_sync.py --yes --only AG3105 --table-workers 1
```

重跑某个库会重新 `DROP` 再建整库，不做表级增量。

## 8. 常见问题

| 报错 | 原因与处理 |
| --- | --- |
| `Access denied for user 'sync_reader'` | 缺权限。逐项核对第 3 节的 GRANT，特别是 `PROCESS` 和 `SHOW ROUTINE` |
| `源实例中不存在 N 个库` | 清单名字写错，或源端大小写不同（大小写不同只会告警并按源端纠正，不会报缺失） |
| `MySQL server has gone away` / `Packet too large` | `batch_rows` / `batch_bytes` 调小，或把目标端 `max_allowed_packet` 调大 |
| `Unknown collation: 'utf8mb4_0900_ai_ci'` | 目标端实际低于 8.0.1 |
| `Incorrect datetime value: '0000-00-00'` | 配置里 `sql_mode` 被手工填成了严格模式，清空该项 |
| `Data too long for column` | 源库历史脏数据比目标列定义宽，只能改目标列或清理源数据 |
| `Table ... doesn't exist`（建视图失败） | 视图跨库引用了不在清单里的库。把被引用的库也加进清单 |
| `读不到定义，源账号缺 SHOW ROUTINE 权限` | 见第 3 节；不需要例程就把 `include_routines` 设为 `false` |
| 大库跑一半连接被重置 | 防火墙/负载均衡掐长连接。调小 `workers` 和 `table_workers`，或调大目标端 `max_allowed_packet` 减小批次体积 |
| 日志中文乱码 | Windows 控制台默认 GBK，脚本已在 `main()` 里切到 UTF-8；若仍乱码，`chcp 65001` 或改用 `--log-file` |

## 9. 自测

没有真实两端环境时，可以用假连接验证 SQL 生成、分批、并行调度与 DDL 顺序：

```bash
python selftest.py
```

覆盖 59 项断言：批大小按行数/字节双重截断、流式游标正确关闭、标识符转义、`DEFINER` 剥离的
四种形态、生成列过滤、时区偏移折算、通道数收敛、单表失败后换通道续跑、以及整库
`建库 → 建表 → 建视图 → 触发器 → 存储过程` 的执行顺序。有失败时退出码为 1，可直接接进 CI。

## 10. 文件说明

| 文件 | 用途 |
| --- | --- |
| `mysql_sync.py` | 同步主程序 |
| `AGENTS.md` | 项目约束与核心不变量，改代码前先看 |
| `sync_config.ini.example` | 配置模板，复制成 `sync_config.ini` 后填写 |
| `databases.txt` | 待同步库清单（当前内置 100 个 AG 库） |
| `selftest.py` | 无凭据自测，不需要连数据库 |
| `.gitignore` | 已排除 `sync_config.ini`、日志、`__pycache__` |
| `.gitattributes` | 强制 LF 换行，保证脚本能在 Linux 上直接执行 |
