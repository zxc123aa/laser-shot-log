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
  - 版面：优先名字叫 Sheet4 的表，按 列号头 + 行号列 + 靶位/类型成对合并块 复刻。
  - 映射：只从版面主网格按"靶块"展开 —— 一个靶块 = 编号块(2×2) + 类型块(2×2)，
    覆盖 2 行 × 4 列靶位；块内所有靶位（行号,列号）共享该块的靶类型。
    靶位编号 ≠ 靶块编号：靶位 1-2 在靶块 1-1 内 → 类型 = 靶块 1-1 的类型。
  - 兜底：无版面时退回全表扫描（x-y 标签向右找类型）。
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


def norm_merges(sh):
    """xlrd merged_cells 是 (rlo, rhi, clo, chi)，统一转成 (r1, c1, r2, c2)（排他边界）"""
    return [(rlo, clo, rhi, chi) for (rlo, rhi, clo, chi) in sh.merged_cells]


def fmt_cell(v):
    """单元格文本：浮点整数去掉 .0（Excel 列号/行号 1.0 -> 1）"""
    if v in (None, ""):
        return ""
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    s = str(v).strip()
    if re.match(r"^-?\d+\.0+$", s):
        s = s.split(".")[0]
    return s


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


def block_mapping(layout):
    """靶块几何映射：一个靶块 = 编号块 + 类型块，覆盖 2 行 × 4 列靶位，
    块内所有靶位（行号,列号）共享该块的靶类型。靶位编号 ≠ 靶块编号。"""
    if not layout or layout.get("header_row", -1) < 0:
        return None
    hr = layout["header_row"]
    values = layout["values"]
    colnum = {}                         # 网格列 -> 列号（列号表头行）
    for c in range(layout["ncols"]):
        v = values.get("%d,%d" % (hr, c), "")
        if v.isdigit():
            colnum[c] = int(v)
    mapping = {}
    for lab in layout["labels"]:
        if len(lab) < 7:
            continue                    # 无类型块的标签跳过
        r, c = lab[0], lab[1]
        ts = values.get("%d,%d" % (lab[3], lab[4]), "")
        if not ts:
            continue                    # 类型空 = 未填，不产生映射
        rn = []
        for rr in (r, r + 1):           # 编号块跨 2 行 -> 2 个靶位行号
            v = values.get("%d,0" % rr, "")
            if v.isdigit():
                rn.append(int(v))
        cn = [colnum[cc] for cc in (c, c + 1, c + 2, c + 3)
              if cc in colnum]          # 编号 2 列 + 类型 2 列 = 4 个靶位列号
        if not rn or len(cn) < 4:
            continue
        for rr2 in rn:
            for cc2 in cn:
                mapping["%d-%d" % (rr2, cc2)] = ts
    return mapping or None


def main(xls_path, out_path):
    wb = xlrd.open_workbook(xls_path, formatting_info=True)  # 必须开启才有 merged_cells
    cand = {}          # 靶位 -> [types...]（全表扫描，仅作无版面时的兜底）
    for si in range(wb.nsheets):
        sh = wb.sheet_by_index(si)
        merges = clean_merges(sh, norm_merges(sh))
        for (r, c, pos) in sheet_labels(sh):
            ts, _ = pick_type(sh, r, c, merges)
            if ts:
                cand.setdefault(pos, []).append(ts)
    fallback_map = {p: (types[0] if types else "")
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
        merges = clean_merges(sh, norm_merges(sh))
        mset = set(merges)

        # ---- 主网格定位（模板：标题行 + 列号头 + 靶位/类型成对合并块）----
        # 列号头 = 数字格子最多的一行；主网格 = 头行数字列覆盖的范围（含左侧行号列）
        header_row, best_cnt = -1, 0
        for r in range(sh.nrows):
            cnt = sum(1 for c in range(sh.ncols)
                      if is_num(sh.cell_value(r, c)))
            if cnt > best_cnt:
                header_row, best_cnt = r, cnt
        if best_cnt < 4:
            header_row = -1                    # 不像本模板，退回全表

        if header_row >= 0:
            num_cols = [c for c in range(sh.ncols)
                        if is_num(sh.cell_value(header_row, c))]
            runs, s, p = [], None, None       # 取最长连续段（排除右侧散落数字）
            for c in num_cols:
                if s is None:
                    s = p = c
                elif c == p + 1:
                    p = c
                else:
                    runs.append((s, p)); s = p = c
            if s is not None:
                runs.append((s, p))
            if runs:
                s, e = max(runs, key=lambda t: t[1] - t[0])
                c1, c2 = max(0, s - 1), e + 1
            else:
                c1, c2 = 0, sh.ncols
            data_labels = [(r, c, p) for (r, c, p) in sheet_labels(sh)
                           if r > header_row and c1 <= c < c2]
            nrows, ncols = header_row + 1, min(c2, sh.ncols)
        else:
            header_row = -1
            data_labels = sheet_labels(sh)
            nrows, ncols = sh.nrows, sh.ncols

        # 类型格区块：向右找锚格（类型值 / 空的待填合并块），跳过编号块内部
        def type_region_for(r, c):
            for dc in (1, 2, 3, 4):
                cc = c + dc
                if cc >= ncols:
                    break
                reg = region_of(r, cc, merges)
                if reg is None:
                    reg = (r, cc, r + 1, cc + 1)
                elif (reg[0], reg[1]) != (r, cc):
                    continue                   # 在别的合并块内部（编号块）
                v = sh.cell_value(reg[0], reg[1])
                ts = fmt_cell(v)
                if ts and not is_num(v) and not PAT.match(ts):
                    return ts, reg
                if ts == "":
                    return "", reg             # 空块 = 可补填的类型格
            return "", None

        labels_out = []
        for (r, c, pos) in data_labels:
            _t, reg = type_region_for(r, c)
            if reg:
                labels_out.append([r, c, pos,
                                   reg[0], reg[1], reg[2], reg[3]])
                nrows = max(nrows, reg[2]); ncols = max(ncols, reg[3])
            else:
                labels_out.append([r, c, pos])
            nrows = max(nrows, r + 1)

        in_box = lambda m: (m[0] >= 0 and m[1] >= 0
                            and m[2] <= nrows and m[3] <= ncols)
        merges_out = [m for m in merges if in_box(m)]
        merge_text, values = {}, {}
        for r in range(min(nrows, sh.nrows)):
            for c in range(min(ncols, sh.ncols)):
                ts = fmt_cell(sh.cell_value(r, c))
                if ts:
                    values["%d,%d" % (r, c)] = ts
        for (r1, c1, r2, c2) in merges_out:
            if r1 < sh.nrows and c1 < sh.ncols:
                merge_text["%d,%d,%d,%d" % (r1, c1, r2, c2)] = \
                    fmt_cell(sh.cell_value(r1, c1))
        layout = {"sheet": sh.name, "nrows": nrows, "ncols": ncols,
                  "header_row": header_row,
                  "merges": [list(m) for m in merges_out],
                  "labels": labels_out,
                  "merge_text": merge_text, "values": values}

    # 映射：优先靶块几何展开（只从版面主网格），无版面时退回全表扫描
    mapping = block_mapping(layout)
    if mapping is None:
        mapping = fallback_map

    data = {"source": xls_path, "map": mapping, "layout": layout}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    filled = sum(1 for v in mapping.values() if v)
    print("positions:", len(mapping), "with type:", filled,
          "| layout:", (layout or {}).get("sheet", "none"),
          "merges:", len((layout or {}).get("merges", [])))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
