# -*- coding: utf-8 -*-
"""
历史 shotlist 导入工具
======================
把实验室现有的 shotlist .xls 导入本系统的 shots.db 作为历史记录。

用法：
  python import_xls.py <xls文件路径> [日期YYYY-MM-DD] [--sheet 表格名称]
  日期不填时自动从文件名里的 8 位数字推断（如 shotlist20251128 → 2025-11-28）；
  表格名称不填时默认用日期作为表格名（同名表已存在则直接写入该表）。

依赖：xlrd（读取 .xls）—— pip install xlrd
表头自动识别：按 shotlist_cols.json 里的列名匹配（重名列按出现顺序依次对应）。
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime

import xlrd

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "shots.db")
COLS_PATH = os.path.join(BASE, "shotlist_cols.json")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    args = sys.argv[1:]
    xls_path = args[0]
    sheet_name = None
    if "--sheet" in args:
        i = args.index("--sheet")
        if i + 1 < len(args):
            sheet_name = args[i + 1]

    m = re.search(r"(\d{8})", os.path.basename(xls_path))
    if len(args) >= 2 and not args[1].startswith("--"):
        date = args[1]
    elif m:
        d = m.group(1)
        date = "%s-%s-%s" % (d[:4], d[4:6], d[6:8])
    else:
        date = ""
        print("警告：文件名里没找到日期，时间列将只保留原始时刻")
    if not sheet_name:
        sheet_name = date or "历史导入"
    print("目标表格: %s" % sheet_name)

    with open(COLS_PATH, "r", encoding="utf-8") as f:
        cols = json.load(f)

    # 表头名 -> key 的映射（重名按出现顺序依次分配）
    by_name = {}
    for c in cols:
        by_name.setdefault(c["name"], []).append(c["key"])
        en = c.get("export_name")
        if en and en != c["name"]:
            by_name.setdefault(en, []).append(c["key"])

    book = xlrd.open_workbook(xls_path)
    sh = book.sheet_by_index(0)
    headers = [str(sh.cell_value(0, j)).strip() for j in range(sh.ncols)]
    # 表头列 -> key（按出现顺序消费重名）
    col_map = {}
    for j, h in enumerate(headers):
        keys = by_name.get(h)
        if keys:
            key = keys.pop(0)
            col_map[j] = key
    print("表头映射: %d/%d 列" % (len(col_map), len([h for h in headers if h])))
    unmapped = [h for j, h in enumerate(headers) if h and j not in col_map]
    if unmapped:
        print("未映射的表头（将忽略）:", unmapped)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # 确保表结构存在（sheets/trash 表、sheet_id 列、去 UNIQUE 迁移）
    try:
        from a_server import init_db
        init_db()
    except Exception as e:
        print("init_db 兜底执行失败(%r)，尝试最小迁移" % e)
        try:
            conn.execute("ALTER TABLE shots ADD COLUMN fields TEXT DEFAULT '{}'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE shots ADD COLUMN sheet_id INTEGER")
        except sqlite3.OperationalError:
            pass
    # 归入指定表格（不存在则按名创建）
    sh_row = conn.execute("SELECT id FROM sheets WHERE name=?", (sheet_name,)).fetchone()
    if sh_row:
        sheet_id = sh_row["id"]
    else:
        sheet_id = conn.execute(
            "INSERT INTO sheets(name, exp_date, note, created_at) VALUES (?,?,?,?)",
            (sheet_name, date,
             "import_xls.py 导入",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))).lastrowid
    inserted = skipped = 0
    for i in range(1, sh.nrows):
        vals = [sh.cell_value(i, j) for j in range(sh.ncols)]
        row = [str(v).strip() if not isinstance(v, float) else
               (str(int(v)) if v == int(v) else str(v))
               for v in vals]
        if not any(row):
            skipped += 1
            continue
        tcell = row[0] if row else ""
        # xlrd 日期：Excel 序列值（>1 含日期；<1 纯时刻）
        if isinstance(vals[0], float) and vals[0] > 0:
            try:
                y, mo, d, hh, mi, ss = xlrd.xldate_as_tuple(vals[0], book.datemode)[:6]
                if y:
                    shot_time = "%04d-%02d-%02d %02d:%02d:%02d" % (y, mo, d, hh, mi, ss)
                else:
                    shot_time = ("%s %02d:%02d:%02d" % (date, hh, mi, ss)).strip()
            except Exception:
                shot_time = ("%s %s" % (date, tcell)).strip() if date else tcell
        else:
            shot_time = ("%s %s" % (date, tcell)).strip() if date else tcell
        if not shot_time:
            skipped += 1
            continue
        fields = {}
        for j, key in col_map.items():
            if j < len(row) and row[j] != "":
                fields[key] = row[j]
        # 显式去重（库已无 UNIQUE 约束）：同表、同来源、同时间视为重复
        if conn.execute(
                "SELECT 1 FROM shots WHERE sheet_id=? AND machine=? AND shot_time=?",
                (sheet_id, "历史导入", shot_time)).fetchone():
            skipped += 1
            continue
        conn.execute("""
            INSERT INTO shots
            (shot_time, machine, file_count, first_file, folder, fields,
             reported_at, created_at, sheet_id)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (shot_time, "历史导入", 0, "", "",
             json.dumps(fields, ensure_ascii=False), "历史导入",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sheet_id))
        inserted += 1
    conn.commit()
    conn.close()
    print("完成：导入 %d 行，跳过 %d 行（空行/重复）" % (inserted, skipped))


if __name__ == "__main__":
    main()
