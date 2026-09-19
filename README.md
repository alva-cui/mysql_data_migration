# MySQL 整库同步脚本

把源实例中的一批数据库（表结构 + 数据 + 视图/触发器/存储过程/定时事件）整体搬到另一台
MySQL 实例。**纯 Python 实现，不依赖 `mysqldump` / `mysql` 客户端**，只需要能连上两端网络。

- 目标端：MySQL 8.0+（源端同为 8.0+）
- 一次配置，之后就是 `python mysql_sync.py --yes`
- 支持库级 + 库内表级双层并行
- 单库失败不影响其他库，可单独重跑
- 控制台与 `--log-file` 是同一份输出，每行带时间戳/级别/库与通道上下文，按级别、按库名直接 grep

---

## 1. 环境准备

```bash
python -m pip install -r requirements.txt     # 只有 pymysql 一个第三方依赖
```

Python 3.8 以上即可。逻辑放在 `mysqlsync/` 包里，根目录的 `mysql_sync.py` 是入口壳，
`python mysql_sync.py …` 与 `python -m mysqlsync …` 完全等价。

## 2. 三步跑起来

```bash
# ① 填连接信息
cp sync_config.ini.example sync_config.ini
vi sync_config.ini                    # 改 [source] / [target] 的 host/user/password

# ② 核对库清单：仓库里带的 100 个 AG 库名是上一批环境留下的，
#    换环境必须按第 9 节步骤 0 从源实例重新生成，否则全部报「源实例中不存在」
vi databases.txt

# ③ 先预检，看清楚要动哪些库，这一步不碰目标端
python mysql_sync.py --dry-run --log-file logs/dryrun.log

# ④ 正式同步
python mysql_sync.py --yes --log-file logs/sync.log
```

`--log-file` 的父目录会自动创建，日志按 50 MB 轮转保留 5 份，不用担心跑一整夜把盘写满。

`--dry-run` 会做三件事：连两端、比对版本与 `lower_case_table_names`、检查清单里的库在源端
是否都存在（大小写不一致会按源端真实库名纠正并告警）。**建议每次都先跑一遍。**

不加 `--yes` 时，若目标端存在同名库，脚本会列出会被 DROP 的库并要求输入 `yes` 确认。

上面只是把脚本跑起来。真要搬几十个上百个库，按第 9 节的流程走，搬完务必做第 10 节的核验——
**报告「完成」只代表脚本没报错，不代表对象齐全**。

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

### 权限给不够会「静默少对象」，而不是报错

脚本的待同步清单全部来自源端 `information_schema`，而 `information_schema` 只显示当前账号
**有权看到**的对象。所以：

- 源账号只覆盖了部分库 → 其余库在预检就报「源实例中不存在」，会 `exit 1`，这种失败是响的；
- 源账号缺 `SHOW ROUTINE` → 不是自己定义的存储过程/函数在 `ROUTINES` 里根本查不到，
  于是一条错误都不报、目标端就是没有这些过程（只有显式读到空定义时才报「读不到定义」）；
- 源账号缺 `TRIGGER` / `EVENT` → 同理，触发器和事件会被当成「该库没有这类对象」。

这三种情况结束时都打印「成功」。第 10 节的逐类计数比对就是用来兜住它的。

## 4. 配置文件 `sync_config.ini`

用环境变量覆盖密码，可以让密码完全不落盘：

```bash
SYNC_SRC_PASSWORD='源库密码' SYNC_DST_PASSWORD='目标库密码' python mysql_sync.py --yes
```

> 该文件即将包含明文密码，仓库里只提交 `.example`，真身已在 `.gitignore` 中。

所有取值在启动时校验：非整数的 `batch_rows`、越界的 `port`、写成「也许」的布尔值都会以一句
指向键名的错误直接退出（退出码 2），不会被悄悄当成默认值。`[source]`/`[target]` 两节必须显式
填 `host` 或 `socket`——留空不会被默认成本机 3306。

日志相关的开关（文件、级别、轮转、UTC）**不放进配置文件**，全部走命令行，见第 5 节。

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
    --log-file FILE        日志同时写入该文件（父目录自动创建，按大小轮转）
    --log-level LEVEL      CRITICAL/ERROR/WARNING/INFO/DEBUG，默认 INFO
    --log-max-mb N         单个日志文件多大之后轮转，默认 50
    --log-backups N        轮转保留几个历史文件，默认 5
    --log-dateutc          时间戳改用 UTC（跳板机与数据库时区不一致时用）
-V, --version              打印版本后退出
```

输出全部走同一套 logging：控制台看到什么，`--log-file` 里就是什么，每行都是
`日期 时间 级别 上下文 消息`。上下文列在并发时用来定位是谁说的——`precheck`、`plan`、
`report`、库名、`库名/通道号`。`--log-level DEBUG` 会额外打出每条 SQL、每个批次的行数与
字节数、每个会话参数和整份参数快照。

退出码：

| 码 | 含义 |
| --- | --- |
| `0` | 全部成功，且清单里没有源端不存在的库 |
| `1` | 跑完了，但有库/有表失败，或清单里有库在源端不存在（`--dry-run` 发现缺失也是 1） |
| `2` | 没跑起来：配置文件、库清单、参数、预检连接、`lower_case_table_names` 不一致、或在 DROP 确认处取消 |
| `130` | 被 Ctrl-C 中断。已同步完成的库不回滚，用 `--start-from` / `--only` 接着跑 |

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
- 服务端配置（`my.cnf`、`innodb_buffer_pool_size` 等）。其中 `lower_case_table_names`
  虽然不搬，预检会比对两端取值，不一致就直接退出（退出码 2）——两边规则不同时目标端会按
  自己的规则改写库表名，事后极难发现
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

单个库失败不会中断整批。结束时打出一段报告（同时进日志文件），列出每个失败库的前 3 条错误，
并给出可以直接复制粘贴的重跑命令：

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
| `1046 No database selected`（整库建表全失败，只有视图能建上） | 用的是修复前的版本。`SHOW CREATE TABLE` 输出不带库名前缀，脚本必须在建库后 `USE` 选中目标库；拉最新代码即可 |
| `两端 lower_case_table_names 不一致` | 预检主动拦下的。两边大小写规则不同会把目标端库表名按目标规则改写，先改目标实例 `my.cnf` 里的这个参数并重启对齐，再同步 |
| `Access denied for user 'sync_reader'` | 缺权限。逐项核对第 3 节的 GRANT，特别是 `PROCESS` 和 `SHOW ROUTINE` |
| `源实例中不存在 N 个库` | 清单名字写错，或源端大小写不同（大小写不同只会告警并按源端纠正，不会报缺失） |
| `MySQL server has gone away` / `Packet too large` | `batch_rows` / `batch_bytes` 调小，或把目标端 `max_allowed_packet` 调大 |
| `Unknown collation: 'utf8mb4_0900_ai_ci'` | 目标端实际低于 8.0.1 |
| `Incorrect datetime value: '0000-00-00'` | 配置里 `sql_mode` 被手工填成了严格模式，清空该项 |
| `Data too long for column` | 源库历史脏数据比目标列定义宽，只能改目标列或清理源数据 |
| `Table ... doesn't exist`（建视图失败） | 视图跨库引用了不在清单里的库。把被引用的库也加进清单 |
| `读不到定义，源账号缺 SHOW ROUTINE 权限` | 见第 3 节；不需要例程就把 `include_routines` 设为 `false` |
| 大库跑一半连接被重置 | 防火墙/负载均衡掐长连接。调小 `workers` 和 `table_workers`，或调大目标端 `max_allowed_packet` 减小批次体积 |
| 日志中文乱码 | Windows 控制台默认 GBK，入口已把标准输出切到 UTF-8；若仍乱码，`chcp 65001` 或改用 `--log-file` 后用编辑器看 |

## 9. 正式迁移操作流程（几十上百个库）

### 步骤 0 · 清单和账号先对准

`databases.txt` 必须是**源实例上真实存在**的库名。拿别的环境留下的旧清单直接跑，结果是
「源实例中不存在 N 个库」然后 `exit 1`，一个字节都不会动。重新生成：

```sql
SELECT SCHEMA_NAME FROM information_schema.SCHEMATA
 WHERE SCHEMA_NAME NOT IN ('mysql','information_schema','performance_schema','sys')
 ORDER BY SCHEMA_NAME;
```

源账号必须**一个账号覆盖全部待同步库**。"每个库各用自己的单库账号轮一遍"不可取，理由见
第 3 节的「静默少对象」。

### 步骤 1 · 预检

```bash
python mysql_sync.py --dry-run --log-file logs/dryrun.log
```

`precheck` 那一段是逐字要看的：`待同步 N 个库` 的 N 要等于你心里的数量，`源端缺失` 必须是 0，
`并发 … x … = … 条传输通道｜峰值连接约 … 条` 要和两端 `max_connections` 对得上。
紧接着 `plan` 那段逐库列出哪些目标端库将被 DROP 重建——这是唯一一处能事前看到破坏面的地方。
预检发现缺失库时退出码是 1，可以接进流水线。

### 步骤 2 · 单库金丝雀

```bash
python mysql_sync.py --yes --only 某个库 --workers 1 --table-workers 1 \
    --log-file logs/canary.log --log-level DEBUG
```

挑库时要挑**确实带触发器 / 存储过程 / 事件**的。空的或非空的库走的是完全不同的代码路径，
拿一个只有表的库试跑，等于没测。跑完立刻做第 10 节核验。

`--log-level DEBUG` 在这一步值得开：每条 SELECT/INSERT、每个批次、每个会话参数都会打出来，
出问题时能直接看出是哪条语句、哪一批。

### 步骤 3 · 全量

```bash
nohup python mysql_sync.py --yes --log-file logs/sync-all.log >/dev/null 2>&1 &
tail -f logs/sync-all.log
```

结论都在日志文件里，标准输出丢不丢都无所谓了——报告、失败明细、退出码提示全部走 logging，
控制台与 `--log-file` 是同一份内容。事后查问题按列检索（每行固定 5 列：
`日期 时间 级别 上下文 消息`，所以级别是 `$3`、上下文是 `$4`）：

```bash
grep ' RESULT ' logs/sync-all.log            # 一行看全局：库数/成功/失败/各类对象/行数/退出码
awk '$3=="ERROR"' logs/sync-all.log | head   # 失败明细（按级别列精确筛，不会被正文里的 ERROR 骗到）
awk '$3=="ERROR"{n++} END{print n+0}' logs/sync-all.log   # 非 0 就是有东西没搬上
awk '$4=="ag3137"' logs/sync-all.log         # 某个库从预检到收尾的完整过程（上下文列）
awk '$4=="ag3137/ch3"' logs/sync-all.log     # 某库某一条传输通道单独摘出来
```

用 `awk` 而不是 `grep ' ERROR '` 筛级别：正文里可能带 MySQL 原文的 `ERROR 1062` 之类字样，
按列筛才不会误报。

日志按 `--log-max-mb`（默认 50 MB）轮转，超过就切 `logs/sync-all.log.1`…，最多留
`--log-backups` 份（默认 5），跑一百个库也不会把盘写满。

### 步骤 4 · 并发怎么定

先算连接预算：`workers × (table_workers + 1) × 2`，源和目标各占一半，必须小于两端
`max_connections` 和同步账号的 `max_user_connections`。

| 情况 | 配置 | 理由 |
| --- | --- | --- |
| 上百个中小库 | `--workers 8 --table-workers 1` | 每库 100+ 张表的建表 DDL 是**单连接串行**执行的，库多时这才是瓶颈，库内并行帮不上忙；`table_workers=1` 还顺带保证了单库跨表同一快照 |
| 一两个超大库 | `--workers 1 --table-workers 8~16` | 让库内部并行，避免长尾 |
| 源是生产主库、怕压垮 | `--workers 2 --table-workers 2`，配置里 `batch_rows = 500` | 减小单批体积与源端扫描压力 |

### 步骤 5 · 失败重跑

见第 7 节。重跑某个库是重新 `DROP` 整库再建，不是补差异。

## 10. 同步后的核验清单（必做）

脚本只会报告「我执行的过程中没报错」，它不会知道源端有没有东西是你压根没让它看见的。
下面这几条才是交付依据。

**先跑自动核验**，再逐条人工确认：

```bash
python tools/verify_sync.py 库名 --log-file logs/verify-库名.log
```

只读，不写任何数据。逐表比对建表语句、行数、全量内容哈希、视图可查性与 DEFINER、
时间列 epoch 漂移、列级字符集；明细最多打 20 条，其余降到 DEBUG，有问题时退出码 1。
它一次只管一个库，百库规模挑重点库跑。

**① 逐类对象数两端比对**——防静默少对象，最重要的一条：

```sql
-- 两端各跑一次，结果必须逐行相同（前缀按实际库名改）
SELECT TABLE_SCHEMA, TABLE_TYPE, COUNT(*) FROM information_schema.TABLES
 WHERE TABLE_SCHEMA LIKE 'AG%' GROUP BY 1,2 ORDER BY 1;
SELECT ROUTINE_SCHEMA, ROUTINE_TYPE, COUNT(*) FROM information_schema.ROUTINES
 WHERE ROUTINE_SCHEMA LIKE 'AG%' GROUP BY 1,2 ORDER BY 1;
SELECT TRIGGER_SCHEMA, COUNT(*) FROM information_schema.TRIGGERS
 WHERE TRIGGER_SCHEMA LIKE 'AG%' GROUP BY 1 ORDER BY 1;
SELECT EVENT_SCHEMA, COUNT(*) FROM information_schema.EVENTS
 WHERE EVENT_SCHEMA LIKE 'AG%' GROUP BY 1 ORDER BY 1;
```

关键技巧：源端那一侧**用同步账号跑一遍、再用高权限账号跑一遍**。两个结果不一样，就说明
同步账号权限给漏了，而差出来的那些对象在目标端就是不存在——脚本不会为此报任何错。

同步报告里每库的 `表 125/125 触发器 1/1` 就是「目标端已建 / 源端可见」两个数，`RESULT` 行
是全量汇总。两个数相等只说明"看见的都建上了"，看不见的那部分仍然要靠上面这条双账号比对。

**② 行数。** 要留交付凭证就把配置里 `verify = true` 打开（每表拷完做一次两端 `COUNT(*)`，
代价是源端多一遍全表扫描，中小库几秒可接受）；或事后对重点表抽样 `SELECT COUNT(*)`。

**③ 时间列不漂移。** 两端会话都先 `SET SESSION time_zone = '+08:00'`（用数字偏移，和脚本
行为一致），再比 `MIN`/`MAX` 与 `SUM(UNIX_TIMESTAMP(col))`。不设就比，会因两端默认时区不同
而假报警。

**④ 字符集没被"顺手改掉"。** 脚本原样重放建表语句，源库的 latin1/gbk 表在目标端仍是
latin1/gbk。想确认：

```sql
SELECT TABLE_NAME, TABLE_COLLATION FROM information_schema.TABLES
 WHERE TABLE_SCHEMA = '库名' AND TABLE_COLLATION NOT LIKE 'utf8mb4%';
```

**⑤ 视图 DEFINER 变成目标执行账号是预期行为。** 副作用值得知道：源端因为 definer 账号已被
删除而根本查不动的坏视图，剥掉 DEFINER 落到目标执行账号后**反而能查了**。两端视图结果不一致
时先想到这条，别当同步 bug 报。

**⑥ 事件搬过去了但不会跑。** 脚本不改服务端全局开关，要事件真跑起来，自己在目标端执行
`SET PERSIST event_scheduler = ON;`。

**⑦ 本来就不搬的东西**（别在核验时才发现）：数据库账号与库表级授权、`my.cnf` 与服务端参数
（含 `lower_case_table_names`）、binlog/GTID 位点（拷贝后目标端是全新起点，接不回原复制拓扑）、
非 `BASE TABLE`/`VIEW` 的对象。

## 11. 自测

没有真实两端环境时，用假连接验证 SQL 生成、分批、并行调度、DDL 顺序、日志契约与配置校验：

```bash
python tests/selftest.py
```

覆盖 121 项断言：批大小按行数/字节双重截断、流式游标正确关闭、标识符转义、`DEFINER` 只剥
第一处、生成列过滤、时区偏移折算、`max_allowed_packet` 60% 折算、通道数收敛、单表失败后换
通道续跑、通道建不起来必须记进 errors、整库
`建库 → 选中库 → 建表 → 建视图 → 触发器 → 存储过程` 的执行顺序，以及建表前必须已 `USE`
选中目标库（漏了会整库报 1046 No database selected）。

另有两组是这次拆包后新增的护栏：

- **日志契约**：落盘行必须带日期/级别/上下文列（旧版文件 handler 没挂 formatter，落盘只剩
  消息正文）、并发时每库每通道各带自己的标签、错误只从 `note_error` 一个入口同时进
  `errors` 与 ERROR 日志、`--log-file` 走轮转 handler 且真按大小切分、重复初始化不叠加 handler。
- **配置与密码**：非法取值（`workers=0`、`drop_existing=也许`、端口越界、host 留空）必须
  报错而不是悄悄用默认值；密码不出现在 `repr`、参数快照、`Endpoint.display` 任何一处。

全绿才提交（有失败时退出码为 1，可直接接进 CI）。这个脚本验证不了真实表结构兼容性，
涉及线上行为时先拿单个库 `--only` 试跑（第 9 节步骤 2）。

## 12. 文件说明

| 路径 | 用途 |
| --- | --- |
| `mysql_sync.py` | 入口壳。保留 `python mysql_sync.py` 这个既有写法，逻辑不在这里 |
| `mysqlsync/` | 同步逻辑包，按职责分模块：`config`（配置/清单/库名校验）、`db`（SQL 与会话工具）、`metadata`（源端元数据）、`ddl`（目标端 DDL）、`channel`（传输通道）、`syncer`（单库编排 + 表级并行）、`runner`（库级并行）、`report`（报告）、`logging_setup`（日志与上下文）、`cli`（参数/预检/退出码）、`errors`（异常与退出码）、`results`（单库结果与错误记账） |
| `tools/verify_sync.py` | 只读核验脚本，复用 `mysqlsync` 与同一套日志 |
| `tests/selftest.py` | 无凭据自测，不需要连数据库。假配置夹具写在文件内的 `SAMPLE_INI` 里，不落盘 |
| `AGENTS.md` | 项目约束与核心不变量，改代码前先看 |
| `sync_config.ini.example` | 配置模板，复制成 `sync_config.ini` 后填写 |
| `databases.txt` | 待同步库清单。仓库里带的是历史环境留下的 100 个 AG 库名，换环境先按第 9 节步骤 0 重新生成 |
| `requirements.txt` | 唯一第三方依赖 pymysql |
| `logs/` | 运行日志约定落这里，不入库 |
| `.gitignore` | 已排除 `sync_config.ini`、`logs/`、`*.log`、`__pycache__` |
| `.gitattributes` | 强制 LF 换行，保证脚本能在 Linux 上直接执行 |
