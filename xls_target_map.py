# -*- coding: utf-8 -*-
"""xls_target_map.py —— 从打靶靶位映射 xls 抽取 靶位→靶类型 映射表。

用法: python xls_target_map.py <输入.xls> <输出.json>
由 thomson_helper 在 xls 更新时自动调用（需 xlrd，本文件不进主服务进程）。

解析规则（对应"第x次打靶靶位"表，如 Sheet4）：
  - 扫描全部 sheet 的全部单元格，凡匹配 "x-y" 靶位标签的（如 2-2、13-5），
    向右找 1~2 格内第一个"非空、非纯数字、且不是另一个靶位标签"的格子
    作为靶类型（如 ch50nm 10°、针尖）。
  - 合并单元格只有左上角有值，右侧空格即合并尾巴，正好符合"向右找"。
  - 同一靶位多处出现（如 Sheet3 只有靶位无类型）时，非空类型优先。
  - Sheet1 这类打靶日志里的靶位（21-9 等）映射不到类型就留空，不报错。
"""
import json
import re
import sys

import xlrd

PAT = re.compile(r"^\s*(\d+\s*-\s*\d+)\s*$")


def is_num(v):
    if isinstance(v, float) and v == int(v):
        return True
    return bool(re.match(r"^\s*-?\d+(\.\d+)?\s*$", str(v)))


def main(xls_path, out_path):
    wb = xlrd.open_workbook(xls_path)
    cand = {}          # pos -> [types...]（按扫描顺序）
    for si in range(wb.nsheets):
        sh = wb.sheet_by_index(si)
        for r in range(sh.nrows):
            for c in range(sh.ncols):
                m = PAT.match(str(sh.cell_value(r, c)))
                if not m:
                    continue
                pos = m.group(1).replace(" ", "")
                for dc in (1, 2):        # 右邻、右二（合并单元格尾巴）
                    if c + dc >= sh.ncols:
                        break
                    t = sh.cell_value(r, c + dc)
                    ts = str(t).strip() if t not in (None, "") else ""
                    if (ts and not is_num(t) and not PAT.match(ts)
                            and not ts.startswith(".")):
                        cand.setdefault(pos, []).append(ts)
                        break
    mapping = {p: (types[0] if types else "")
               for p, types in cand.items()}
    data = {"source": xls_path, "map": mapping}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    filled = sum(1 for v in mapping.values() if v)
    print("positions:", len(mapping), "with type:", filled)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
