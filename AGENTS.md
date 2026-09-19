# 项目说明：MySQL 整库同步脚本

单文件运维工具，把源实例的一批数据库整体拷到目标实例。交付对象是在跳板机上执行的 DBA/运维，
不是 Web 应用——全局规则里的前后端职责隔离、后端 ORM 查询规范在此不适用。

## 验证

没有真实 MySQL 时，唯一的验证手段是自测，改动后必须跑：

```bash
python selftest.py
```

它用假连接覆盖 SQL 生成、分批、并行调度、DDL 执行顺序、DEFINER 剥离等 60 项断言。
全绿才提交（失败时退出码为 1）。这个脚本无法验证真实表结构兼容性，涉及线上行为时先拿单个库
`--only` 试跑。

## 提交与配置

- `sync_config.ini` 含明文密码，已被 `.gitignore` 排除，**只提交 `sync_config.ini.example`**
- 新增配置项要同时改三处：`sync_config.ini.example`、`Options` 数据类、README 第 4 节的表格。
  这三处极易漂移，改一处就必须改齐
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

## 范围边界

明确不做，不要以"完善"为名加进来：

- 不引入 `mysqldump` / `mysql` 客户端或任何 `subprocess` 调用，环境上通常没有
- 不做增量同步、表级 diff、断点续传到表粒度；重跑就是整库 DROP 重建
- 不同步数据库账号与库表级授权（已确认的需求边界）
- 不改服务端全局变量（含 `event_scheduler`），只在会话内设置
- 不支持 MySQL 5.7 及更低版本，两端固定 8.0+，不要写兼容分支
