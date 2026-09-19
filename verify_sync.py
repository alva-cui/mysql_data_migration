"""只读校验：逐表比对源/目标的结构、行数与全量内容哈希。不写任何数据。

两端会话都设成同一个数字时区偏移，与 mysql_sync.py 的行为一致，
这样 DATETIME/TIMESTAMP 的渲染才可比较。
"""
import hashlib
import re
import sys

import pymysql

import mysql_sync as ms

DB = sys.argv[1] if len(sys.argv) > 1 else "ts0002"
AI_RE = re.compile(r" AUTO_INCREMENT=\d+")


def norm_ddl(text):
    # 只忽略自增计数器（与数据无关），其余一字不差必须相同
    return AI_RE.sub("", text).strip()


def load_cols(cur, dbname):
    # 只看 BASE TABLE：视图列混进来会让零日期/漂移扫描去查视图，触发 definer 权限报错
    cur.execute(
        "SELECT C.TABLE_NAME, C.COLUMN_NAME, C.EXTRA FROM information_schema.COLUMNS C "
        "JOIN information_schema.TABLES T ON T.TABLE_SCHEMA = C.TABLE_SCHEMA "
        "  AND T.TABLE_NAME = C.TABLE_NAME AND T.TABLE_TYPE = 'BASE TABLE' "
        "WHERE C.TABLE_SCHEMA = %s ORDER BY C.TABLE_NAME, C.ORDINAL_POSITION", (dbname,))
    out = {}
    for t, name, extra in cur.fetchall():
        up = (extra or "").upper()
        skip = "GENERATED" in up and ("VIRTUAL" in up or "STORED" in up)
        out.setdefault(t, {"all": [], "ins": []})
        out[t]["all"].append(name)
        if not skip:
            out[t]["ins"].append(name)
    return out


def canon(val):
    if isinstance(val, bytes):
        return val.hex()
    if isinstance(val, bool):
        return f"B{val}"
    return f"{type(val).__name__}:{val}"


def table_hash(cur, dbname, table, cols):
    """按全部列定序后逐行哈希：无主键表按首列排序会有并列行，顺序不定会造成假差异。"""
    h = hashlib.md5()
    order = ", ".join(map(ms.q, cols))
    cur.execute(f"SELECT {', '.join(map(ms.q, cols))} FROM {ms.qn(dbname, table)} "
                f"ORDER BY {order}")
    n = 0
    while True:
        rows = cur.fetchmany(2000)
        if not rows:
            break
        for row in rows:
            h.update("|".join(map(canon, row)).encode("utf-8", "replace"))
            h.update(b"\n")
            n += 1
    return h.hexdigest(), n


def main():
    src, dst, opts = ms.load_config("sync_config.ini")
    sc = ms.connect(src, autocommit=True)
    dc = ms.connect(dst, autocommit=True)
    # 数据量小，两端都用带缓冲游标，避免未耗尽结果集导致的假失败
    scur, dcur = sc.cursor(), dc.cursor()
    tz = ms.tz_offset(sc.cursor())
    for cur in (scur, dcur):
        cur.execute("SET SESSION time_zone = %s", (tz,))
    print(f"比对库 {DB}，两端会话 time_zone = {tz}")

    def tables_of(cur):
        cur.execute("SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %s", (DB,))
        rows = cur.fetchall()
        return ({r[0] for r in rows if r[1] == "BASE TABLE"},
                {r[0] for r in rows if r[1] == "VIEW"})

    s_tab, s_view = tables_of(scur)
    d_tab, d_view = tables_of(dcur)
    print(f"表: 源 {len(s_tab)} 目标 {len(d_tab)}   视图: 源 {len(s_view)} 目标 {len(d_view)}")
    problems = []
    for label, a, b in (("缺失的表", s_tab - d_tab, None), ("多出的表", d_tab - s_tab, None),
                        ("缺失的视图", s_view - d_view, None), ("多出的视图", d_view - s_view, None)):
        if a:
            problems.append(f"{label}: {sorted(a)}")

    s_cols = load_cols(scur, DB)
    d_cols = load_cols(dcur, DB)

    # ---- 结构比对 ----
    ddl_diff, ai_diff, col_diff = [], [], []
    for t in sorted(s_tab & d_tab):
        sd = ms.show_create(scur, "SHOW CREATE TABLE", DB, t)
        dd = ms.show_create(dcur, "SHOW CREATE TABLE", DB, t)
        if norm_ddl(sd) != norm_ddl(dd):
            ddl_diff.append(t)
        a1 = re.search(r"AUTO_INCREMENT=(\d+)", sd)
        a2 = re.search(r"AUTO_INCREMENT=(\d+)", dd)
        if (a1 and not a2) or (a2 and not a1):
            ai_diff.append(t)
        elif a1 and a2 and int(a1.group(1)) > int(a2.group(1)):
            ai_diff.append(f"{t} 源{a1.group(1)}>目标{a2.group(1)}")
        if s_cols[t]["all"] != d_cols[t]["all"]:
            col_diff.append(t)
    print(f"\n结构：建表语句差异 {len(ddl_diff)}  自增值异常 {len(ai_diff)}  列清单差异 {len(col_diff)}")
    for t in ddl_diff[:10]:
        print(f"  !! DDL 不一致: {t}")
    for x in ai_diff[:10]:
        print(f"  !! 自增值: {x}")
    for t in col_diff[:10]:
        print(f"  !! 列不一致: {t}")
    problems += [f"DDL 不一致的表 {len(ddl_diff)} 个" if ddl_diff else "",
                 f"列清单不一致 {len(col_diff)} 个" if col_diff else ""]

    # ---- 行数 + 内容哈希 ----
    bad_rows, bad_hash, total_rows = [], [], 0
    for t in sorted(s_tab & d_tab):
        cols = s_cols[t]["ins"]
        if not cols:
            continue
        sh, sn = table_hash(scur, DB, t, cols)
        if d_cols[t]["ins"] != cols:
            bad_rows.append(f"{t}(目标列清单不同)")
            continue
        dh, dn = table_hash(dcur, DB, t, cols)
        total_rows += sn
        if sn != dn:
            bad_rows.append(f"{t} 源{sn}/目标{dn}")
        elif sh != dh:
            bad_hash.append(f"{t} 行数同但内容哈希不同 (n={sn})")
    print(f"\n数据：比对 {len(s_tab & d_tab)} 表 / {total_rows:,} 行")
    print(f"  行数不符 {len(bad_rows)}: {bad_rows[:10]}")
    print(f"  内容不符 {len(bad_hash)}: {bad_hash[:10]}")
    problems += [f"行数不符 {len(bad_rows)}" if bad_rows else "",
                 f"内容不符 {len(bad_hash)}" if bad_hash else ""]

    # ---- 视图可用性与 DEFINER ----
    print("\n视图：")
    for v in sorted(s_view):
        sdef = _definer(scur, DB, v)
        ddef = _definer(dcur, DB, v) if v in d_view else "不存在"
        sn = _view_rows(scur, DB, v)
        dn = _view_rows(dcur, DB, v)
        if sn == dn == "ERR":
            state = "两端都不可查（源端既有问题，非同步引入）"
        elif sn == "ERR":
            state = f"仅源端可报错：目标端可查({dn} 行)，DEFINER 剥离后视图反而可用"
        elif dn == "ERR":
            state = "!! 目标端视图不可查（同步引入）"
            problems.append(f"视图 {v} 目标端不可查")
        elif sn != dn:
            state = f"!! 行数不符 源{sn}/目标{dn}"
            problems.append(f"视图 {v} 行数不符")
        else:
            state = f"OK 两端一致({sn} 行)"
        print(f"  {v}: 源DEFINER={sdef} 目标DEFINER={ddef} 行数 源{sn}/目标{dn} -> {state}")
        if ddef != "不存在" and ddef == sdef:
            print("     !! 目标端 DEFINER 未被剥离，仍是源账号")
            problems.append(f"视图 {v} DEFINER 未剥离")

    # ---- 零日期 / TIMESTAMP 漂移 ----
    print("\n零日期与时间漂移：")
    z_src, z_dst = _zero_dates(scur, DB, s_cols), _zero_dates(dcur, DB, d_cols)
    print(f"  零日期值：源 {z_src} 目标 {z_dst} -> {'OK' if z_src == z_dst else '!! 不一致'}")
    if z_src != z_dst:
        problems.append("零日期不一致")
    drift = _ts_drift(scur, dcur, DB, s_cols)
    print(f"  TIMESTAMP/DATETIME epoch 比对：差异 {len(drift)} 处 {drift[:5]}")
    if drift:
        problems.append(f"时间列漂移 {len(drift)} 处")

    # ---- latin1 表字符集 ----
    print("\n字符集：")
    l1 = _charset_mismatch(scur, dcur, DB)
    print(f"  列级字符集/排序规则不一致 {len(l1)} 处 {l1[:8]}")
    if l1:
        problems.append(f"字符集不一致 {len(l1)} 处")

    print("\n" + "=" * 66)
    problems = [p for p in problems if p]
    if problems:
        print(f"发现 {len(problems)} 类问题：")
        for p in problems:
            print("  -", p)
    else:
        print(f"全部一致：{len(s_tab)} 表 / {total_rows:,} 行 / {len(s_view)} 视图 内容与结构均相同")
    sc.close(); dc.close()
    return 1 if problems else 0


def _definer(cur, dbname, name):
    cur.execute("SELECT DEFINER FROM information_schema.VIEWS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (dbname, name))
    r = cur.fetchone()
    return f"{r[0]}" if r else "不存在"


def _view_rows(cur, dbname, name):
    """独立 try 包裹两端，否则源端视图本身是坏的就测不到目标端状态。"""
    try:
        cur.execute(f"SELECT COUNT(*) FROM {ms.qn(dbname, name)}")
        return int(cur.fetchone()[0])
    except pymysql.MySQLError:
        return "ERR"


def _zero_dates(cur, dbname, cols):
    """用 CAST(... AS CHAR) 比对，直接写 '0000-00-00' 字面量会在严格模式目标端报 1525。"""
    total = 0
    for t, meta in cols.items():
        cur.execute(f"SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    f"WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
                    f"AND DATA_TYPE IN ('date','datetime','timestamp')", (dbname, t))
        for (col,) in cur.fetchall():
            cur.execute(f"SELECT COUNT(*) FROM {ms.qn(dbname, t)} WHERE "
                        f"CAST({ms.q(col)} AS CHAR) LIKE '0000-00-00%'")
            total += int(cur.fetchone()[0] or 0)
    return total


def _ts_drift(scur, dcur, dbname, cols):
    """抽样比对每表时间列的 SUM(UNIX_TIMESTAMP()) 与 COUNT，检测时区折算漂移。"""
    diffs = []
    for t in sorted(cols):
        scur.execute(f"SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                     f"WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
                     f"AND DATA_TYPE IN ('datetime','timestamp','date')", (dbname, t))
        for (col,) in scur.fetchall():
            sig = ("SELECT COUNT(%s), IFNULL(SUM(UNIX_TIMESTAMP(%s)),0), "
                   "MIN(CAST(%s AS CHAR)), MAX(CAST(%s AS CHAR)) FROM %s")
            sql = sig % (ms.q(col), ms.q(col), ms.q(col), ms.q(col), ms.qn(dbname, t))
            try:
                scur.execute(sql)
                a = scur.fetchone()
                dcur.execute(sql)
                b = dcur.fetchone()
            except pymysql.MySQLError as exc:
                diffs.append(f"{t}.{col} 查询异常 {exc}")
                continue
            if a != b:
                diffs.append(f"{t}.{col} 源{a} 目标{b}")
    return diffs


def _charset_mismatch(scur, dcur, dbname):
    sql = ("SELECT TABLE_NAME, COLUMN_NAME, CHARACTER_SET_NAME, COLLATION_NAME "
           "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s "
           "AND CHARACTER_SET_NAME IS NOT NULL ORDER BY TABLE_NAME, ORDINAL_POSITION")
    scur.execute(sql, (dbname,))
    a = scur.fetchall()
    dcur.execute(sql, (dbname,))
    b = dcur.fetchall()
    am, bm = {(r[0], r[1]): (r[2], r[3]) for r in a}, {(r[0], r[1]): (r[2], r[3]) for r in b}
    return [f"{k[0]}.{k[1]}: 源{am[k]} 目标{bm.get(k)}" for k in am if bm.get(k) != am[k]]


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
