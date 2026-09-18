# -*- coding: utf-8 -*-
"""xls_target_map.py —— 从打靶靶位映射 xls 抽取 靶位→靶类型 映射表 + Sheet 版面。

用法: python xls_target_map.py <输入.xls> <输出.json>
由 thomson_helper 在 xls 更新时自动调用（需 xlrd，本文件不进主服务进程）。

输出 JSON：
  map     靶位→靶类型（扫全部 sheet，规则见下）
  layout  选一个"靶位网格"sheet 输出版面，供前端 1:1 复刻：
    sheet      表名
    nrows/ncols 版面行列数（0 基）
    merges     合并区 [[r1,c1,r2,c2],...]（r2/c2 为排他边界）
    labels     版面内靶位标签 [[r,c,"2-2"],...]
    owner      靶位→类型格区块 [r1,c1,r2,c2]（合并区或单格）
    merge_text 合并区左上角文本（标题等静态文字）

解析规则（对应"第x次打靶靶位"表，如 Sheet4）：
  - 凡匹配 "x-y" 靶位标签的格子，向右找 1~2 格内第一个"非空、非纯数字、
    且不是另一个靶位标签"的格子作为靶类型（合并格只有左上有值，正好符合）。
  - 同一靶位多处出现时，非空类型优先。
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


def sheet_labels(sh):
    out = []
    for r in range(sh.nrows):
        for c in range(sh.ncols):
            m = PAT.match(str(sh.cell_value(r, c)))
            if m:
                out.append((r, c, m.group(1).replace(" ", "")))
    return out


def region_of(r, c, merges):
    """(r,c) 所在合并区 (r1,c1,r2,c2)（排他边界），不在任何合并区返回 None"""
    for (r1, c1, r2, c2) in merges:
        if r1 <= r < r2 and c1 <= c < c2:
            return (r1, c1, r2, c2)
    return None


def clean_merges(sh, merges):
    """剔除 xlrd 报出的重叠/嵌套脏合并区：除锚格外还有非空格的一律不算合并"""
    def val(r, c):
        if r < sh.nrows and c < sh.ncols:
            v = sh.cell_value(r, c)
            return str(v).strip() if v not in (None, "") else ""
        return ""
    out = []
    for (r1, c1, r2, c2) in merges:
        ok = True
        for r in range(r1, min(r2, sh.nrows)):
            for c in range(c1, min(c2, sh.ncols)):
                if (r, c) != (r1, c1) and val(r, c):
                    ok = False
                    break
            if not ok:
                break
        if ok:
            out.append((r1, c1, r2, c2))
    return out


def pick_type(sh, r, c, merges):
    """标签 (r,c) 的靶类型 + 类型格区块。找不到返回 ("", None)"""
    for dc in (1, 2):
        if c + dc >= sh.ncols:
            break
        t = sh.cell_value(r, c + dc)
        ts = str(t).strip() if t not in (None, "") else ""
        if ts and not is_num(t) and not PAT.match(ts) and not ts.startswith("."):
            reg = region_of(r, c + dc, merges)
            if reg is None:
                reg = (r, c + dc, r + 1, c + dc + 1)   # 单格也算一个区块
            return ts, reg
    return "", None


def main(xls_path, out_path):
    wb = xlrd.open_workbook(xls_path, formatting_info=True)  # 必须开启才有 merged_cells
    cand = {}          # pos -> [types...]（按扫描顺序）
    for si in range(wb.nsheets):
        sh = wb.sheet_by_index(si)
        merges = clean_merges(sh, [tuple(m) for m in sh.merged_cells])
        for (r, c, pos) in sheet_labels(sh):
            ts, _ = pick_type(sh, r, c, merges)
            if ts:
                cand.setdefault(pos, []).append(ts)
    mapping = {p: (types[0] if types else "")
               for p, types in cand.items()}

    # ---- 选版面 sheet：类型格落在合并区内的标签最多者（Sheet4 形态）----
    best_si, best_score, best_labels = None, -1, 0
    for si in range(wb.nsheets):
        sh = wb.sheet_by_index(si)
        merges = clean_merges(sh, [tuple(m) for m in sh.merged_cells])
        labels = sheet_labels(sh)
        if not labels:
            continue
        mset = set(merges)
        score = 0
        for (r, c, _p) in labels:
            _t, reg = pick_type(sh, r, c, merges)
            if reg and tuple(reg) in mset:   # 类型格在合并区内 = Sheet4 形态
                score += 1
        if score > best_score or (score == best_score and len(labels) > best_labels):
            best_si, best_score, best_labels = si, score, len(labels)

    # ---- 选版面 sheet：优先名字叫 Sheet4 的；否则类型格落在合并区内最多者 ----
    best_si, best_score, best_labels = None, -1, 0
    for si in range(wb.nsheets):
        sh = wb.sheet_by_index(si)
        merges = clean_merges(sh, [tuple(m) for m in sh.merged_cells])
        labels = sheet_labels(sh)
        if not labels:
            continue
        mset = set(merges)
        score = 0
        for (r, c, _p) in labels:
            _t, reg = pick_type(sh, r, c, merges)
            if reg and tuple(reg) in mset:   # 类型格在合并区内 = Sheet4 形态
                score += 1
        if score > 0 and re.match(r"^\s*sheet\s*4\s*$", sh.name, re.I):
            best_si, best_score, best_labels = si, score, len(labels)
            break                            # 用户指定模板 = Sheet4，直接采用
        if score > best_score or (score == best_score and len(labels) > best_labels):
            best_si, best_score, best_labels = si, score, len(labels)

    layout = None
    if best_si is not None and best_score > 0:
        sh = wb.sheet_by_index(best_si)
        merges = clean_merges(sh, [tuple(m) for m in sh.merged_cells])
        mset = set(merges)
        nrows = ncols = 0
        labels_out = []
        seen_regions = set()
        for (r, c, pos) in sheet_labels(sh):
            _t, reg = pick_type(sh, r, c, merges)
            if reg is None:
                # 右邻全是空/编号：给一个可补填的空类型格（若该格确实空着）
                c1 = c + 1
                if (c1 < sh.ncols
                        and not str(sh.cell_value(r, c1) or "").strip()
                        and not PAT.match(str(sh.cell_value(r, c1) or ""))
                        and region_of(r, c1, merges) is None):
                    reg = (r, c1, r + 1, c1 + 1)
            if reg:
                labels_out.append([r, c, pos,
                                   reg[0], reg[1], reg[2], reg[3]])
                seen_regions.add(reg)
                nrows = max(nrows, reg[2]); ncols = max(ncols, reg[3])
            else:
                labels_out.append([r, c, pos])
            nrows = max(nrows, r + 1); ncols = max(ncols, c + 1)
        merge_text, values = {}, {}
        for r in range(min(nrows, sh.nrows)):
            for c in range(min(ncols, sh.ncols)):
                v = sh.cell_value(r, c)
                ts = str(v).strip() if v not in (None, "") else ""
                if ts:
                    values["%d,%d" % (r, c)] = ts
        for (r1, c1, r2, c2) in mset | seen_regions:
            if r1 < nrows and c1 < ncols and r1 < sh.nrows and c1 < sh.ncols:
                v = sh.cell_value(r1, c1)
                merge_text["%d,%d,%d,%d" % (r1, c1, r2, c2)] = \
                    str(v).strip() if v not in (None, "") else ""
        layout = {"sheet": sh.name, "nrows": nrows, "ncols": ncols,
                  "merges": [list(m) for m in merges],
                  "labels": labels_out,
                  "merge_text": merge_text, "values": values}

    data = {"source": xls_path, "map": mapping, "layout": layout}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    filled = sum(1 for v in mapping.values() if v)
    print("positions:", len(mapping), "with type:", filled,
          "| layout:", (layout or {}).get("sheet", "none"),
          "merges:", len((layout or {}).get("merges", [])))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
