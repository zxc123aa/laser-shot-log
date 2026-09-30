# -*- coding: utf-8 -*-
"""
从本地账本重建 / 对账 A 机日志表格
==================================
为什么需要它：
  09-30 的表格乱过好几次（shot80 被时间窗合并吃掉、序号错位、发次记录.txt 假发次、
  file_count 不可改），每次都要人工去回收站翻、逐行 /api/field 改，
  tps_h=19.23 属于 shot79 还是 shot80 至今查不清。
  根因是**没有一份"发生过什么"的本地记录**，A 机表一乱就无据可依。

  现在 B 机/C 机每发生一件关键事都会追加写 ledger/YYYY-MM-DD.jsonl（见 ledger.py），
  本工具就是拿这份账本去核对 A 机表、并在你确认后重放修复。

四个子命令：
  report   （默认）只读对账，报告"缺哪些行/多哪些行/哪些能量没绑/fc 不对"
  apply    真的写入 A 机修复（必须 --yes 二次确认）
  pull-a   把 A 机侧账本（PyTPS 直发 A 机的能量）回灌进本地账本
  tif      PNG × tif 时间线离线核对（C 机推来的时间表 vs B 机发次）

安全设计：
  - 默认只读，一个字都不改
  - A 机去重键 = machine+shot_time+first_file+sheet_id，重放幂等：
    同一条重放一百遍也只有一行，绝不会越修越乱
  - file_count 不在 /api/field 白名单（改不了）→ 只能"删行(进回收站)+重建"，
    这一步会明确列出要删哪几行、要 --yes 才执行
  - 全程 urllib 走 ProxyHandler({})，否则内网请求会被系统代理 502

用法：
  py rebuild_from_ledger.py report --day 2026-09-30
  py rebuild_from_ledger.py report                      # 默认今天
  py rebuild_from_ledger.py apply --day 2026-09-30 --yes
  py rebuild_from_ledger.py apply --day 2026-09-30 --yes --only missing
  py rebuild_from_ledger.py pull-a --day 2026-09-30
  py rebuild_from_ledger.py tif --day 2026-09-30
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import ledger  # noqa: E402

# 内网直连：必须绕开系统代理（会话级动态端口会把 10.0.23.x 拖成 502）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DEFAULT_SERVER = "http://10.0.23.155:8765"
ENERGY_KEYS = ("fiber_p_energy", "tps_h", "tps_c6")


# ---------------------------------------------------------------- 基础设施
def _get(url, timeout=20):
    return json.loads(_OPENER.open(url, timeout=timeout).read().decode("utf-8"))


def _post(url, payload, timeout=20):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST")
    return json.loads(_OPENER.open(req, timeout=timeout).read().decode("utf-8"))


def _epoch(s):
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return None


def load_cfg():
    """读本机配置：A 机地址 + 本机机名（判定"哪些行该由我负责"）"""
    out = {"server": DEFAULT_SERVER, "machine": "", "bw_machine": ""}
    server_set = False          # 显式标志位：不能用 ==DEFAULT_SERVER 当哨兵，
    #   因为真实 config 的地址可能恰好等于默认值，哨兵不会翻转 → 模板值会反覆盖
    for fn in ("config_helper.json", "config_b.local.json", "config_b.json"):
        p = os.path.join(BASE, fn)
        if not os.path.exists(p):
            continue
        try:
            c = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        # server_url 优先级：config_helper.json > config_b.local.json > config_b.json
        if c.get("server_url") and not server_set:
            out["server"] = str(c["server_url"]).rstrip("/")
            server_set = True
        # 机名优先级：config_b.local.json > config_b.json（模板只是兜底占位）
        # 必须与 helper 的 BW_MACHINE 一致——helper 只读 config_b.local.json，
        # 否则 report 会用错机名，"可疑多行"检测整个漏判。
        if fn == "config_b.local.json":
            out["bw_machine"] = str(c.get("machine_name", "") or "")
        elif fn == "config_b.json" and not out["bw_machine"]:
            out["bw_machine"] = str(c.get("machine_name", "") or "")
        if fn == "config_helper.json":
            out["machine"] = str(c.get("machine_name", "") or "")
    # report_shot 用的是 BW_MACHINE or MACHINE
    out["report_machine"] = out["bw_machine"] or out["machine"]
    return out


def find_sheet(server, day):
    """按表名找到当天表的 sheet_id（表名可能是 day 本身或"实时打靶"）"""
    sheets = _get(server + "/api/sheets").get("sheets") or []
    for s in sheets:
        if str(s.get("name", "")) == day:
            return s["id"], s.get("name"), s.get("count")
    for s in sheets:
        if str(s.get("exp_date", "")) == day:
            return s["id"], s.get("name"), s.get("count")
    return None, None, None


def fetch_rows(server, sheet_id):
    """分页拉全一张表的行（page_size 最小被服务端钳到 10，用 1000 一次拉完）"""
    rows, page = [], 1
    while True:
        r = _get("%s/api/rows?sheet_id=%d&page=%d&page_size=1000"
                 % (server, sheet_id, page))
        batch = r.get("rows") or []
        rows.extend(batch)
        if page >= int(r.get("pages") or 1) or not batch:
            return rows, int(r.get("total") or len(rows))
        page += 1


# ---------------------------------------------------------------- 账本解析
def build_timeline(events, report_machine):
    """把账本事件还原成"应有的一行行发次"。

    以 report_ok（真的发出去了，含 A 机 row_id）为主，shot_group 补明细
    （target_type、files 的 folder/浮点 mtime）；两者按 shot_time 合并。
    返回 {shot_time: {...}}，另附未上报成功的发次（只有 shot_group）。
    """
    tl = {}
    for e in events:
        ev = e.get("ev")
        st = str(e.get("shot_time", "") or "")
        if not st:
            continue
        if ev in ("shot_group", "shot_group_w", "detect_in", "report_ok"):
            d = tl.setdefault(st, {"shot_time": st, "files": [], "fields": {},
                                   "src": set(), "row_id": None})
            d["src"].add(ev)
            if e.get("no") is not None:
                d.setdefault("no", e["no"])
            for k_src, k_dst in (("target_pos", "target_pos"),
                                 ("target_type", "target_type"),
                                 ("target_defocus", "target_defocus")):
                v = e.get(k_src)
                if v not in ("", None):
                    d["fields"].setdefault(k_dst, v)
            if e.get("file_count") is not None:
                d["file_count"] = e["file_count"]
            # files 明细：优先带 folder/mtime 的那份
            fl = e.get("files") or []
            if fl and (not d["files"]
                       or any(isinstance(f, dict) and f.get("mtime") for f in fl)):
                d["files"] = fl
            if e.get("sheet_name"):
                d["sheet_name"] = e["sheet_name"]
            if ev == "shot_group_w" and e.get("machine"):
                d["machine"] = e["machine"]
            if ev == "report_ok":
                d["machine"] = e.get("machine") or d.get("machine")
                if e.get("row_id") is not None:
                    d["row_id"] = e["row_id"]
                d["reported"] = bool(e.get("ok"))
                for k, v in (e.get("fields") or {}).items():
                    if v not in ("", None):
                        d["fields"][k] = v
    for st, d in tl.items():
        d["src"] = sorted(d["src"])
        d.setdefault("machine", report_machine)
        names = [str((f.get("name") if isinstance(f, dict) else f) or "")
                 for f in d["files"]]
        d["names"] = names
        d["first_file"] = names[0] if names else ""
        d.setdefault("file_count", len(names))
    return tl


def collect_energies(events):
    """收集所有能量事件（本机收到的 + 从 A 机回灌的），按 (shot_time, field) 归组"""
    out = []
    for e in events:
        ev = e.get("ev")
        if ev == "energy_in":
            out.append({"src": "energy_remote", "filename": e.get("filename"),
                        "field": e.get("field"), "energy": e.get("energy"),
                        "shot_time": e.get("matched_shot"),
                        "no": e.get("matched_no"), "basis": e.get("basis"),
                        "iso": e.get("iso"), "stashed": False})
        elif ev == "unmatched_energy":
            out.append({"src": e.get("source") or "energy_remote",
                        "filename": e.get("filename"), "field": e.get("field"),
                        "energy": e.get("energy"), "shot_time": None,
                        "iso": e.get("iso"), "stashed": True,
                        "energy_id": e.get("energy_id")})
        elif ev == "unmatched_bound":
            out.append({"src": "unmatched_bound", "filename": e.get("filename"),
                        "field": e.get("field"), "energy": e.get("energy"),
                        "shot_time": e.get("shot_time"), "row_id": e.get("row_id"),
                        "iso": e.get("iso"), "stashed": False,
                        "energy_id": e.get("energy_id"),
                        "diff_sec": e.get("diff_sec")})
        elif ev == "a_energy_in":
            pl = e.get("payload") or {}
            out.append({"src": "a_server", "filename": None,
                        "field": pl.get("field") or "fiber_p_energy",
                        "energy": pl.get("energy"),
                        "shot_time": pl.get("shot_time"),
                        "no": pl.get("shot_no"), "iso": e.get("iso"),
                        "stashed": False, "client_ip": e.get("client_ip"),
                        # 去重键：pull-a 可能重复拉同一条，靠 A 机原始时刻区分
                        "_k": (e.get("orig_ts") or e.get("ts"),
                               str(pl.get("field")), str(pl.get("energy")),
                               str(pl.get("shot_time")))})
        elif ev == "a_energy_nomatch":
            out.append({"src": "a_server_nomatch", "filename": None,
                        "field": e.get("field"), "energy": e.get("energy"),
                        "shot_time": e.get("shot_time"), "iso": e.get("iso"),
                        "stashed": False, "no_match": True})
    # 补绑成功的覆盖掉同 id 的暂存记录
    bound_ids = {x.get("energy_id") for x in out
                 if x["src"] == "unmatched_bound" and x.get("energy_id")}
    out = [x for x in out
           if not (x.get("stashed") and x.get("energy_id") in bound_ids)]
    # a_energy_in 去重：pull-a 可能重复拉同一条（幂等回灌），按 A 机原始键去重
    seen_k, dedup = set(), []
    for x in out:
        k = x.get("_k")
        if k is not None:
            if k in seen_k:
                continue
            seen_k.add(k)
        dedup.append(x)
    return dedup


# ---------------------------------------------------------------- report
def cmd_report(args):
    cfg = load_cfg()
    server = args.server or cfg["server"]
    day = args.day or datetime.now().strftime("%Y-%m-%d")
    rmachine = cfg["report_machine"]

    events, bad = ledger.read_day(day)
    sid, sname, scount = find_sheet(server, day)
    print("=" * 68)
    print("对账 %s    A机=%s    本机上报机名=%s" % (day, server, rmachine or "?"))
    print("=" * 68)
    if not events:
        print("\n⚠ 本地账本 %s 没有任何记录（%s）"
              % (ledger.path_for(day), "文件不存在" if not os.path.exists(
                  ledger.path_for(day)) else "空文件"))
        print("  原因：账本埋点是刚加的，helper/watcher **重启之后**才开始记。")
        print("  在重启前的发次无法用本工具对账，只能靠 A 机表 + sheet_backup 的 xlsx 快照。")
    else:
        byev = {}
        for e in events:
            byev[str(e.get("ev", "?"))] = byev.get(str(e.get("ev", "?")), 0) + 1
        print("账本事件 %d 条（坏行 %d）：%s"
              % (len(events), bad,
                 "  ".join("%s=%d" % (k, v) for k, v in sorted(byev.items()))))
    if sid is None:
        print("\n⚠ A 机上找不到名为 %s 的表" % day)
        return 2
    rows, total = fetch_rows(server, sid)
    print("A机表 %s (sheet_id=%s)：%d 行（服务端 total=%d）"
          % (sname, sid, len(rows), total))

    tl = build_timeline(events, rmachine)
    if not tl:
        print("\n账本里没有发次事件，无法对账行。可先跑 `pull-a` 回灌 A 机侧能量。")
        return 0

    # ---- A 机行索引（按去重键）----
    a_by_key = {}
    for r in rows:
        k = (str(r.get("machine", "")), str(r.get("shot_time", "")),
             str(r.get("first_file", "")))
        a_by_key.setdefault(k, []).append(r)
    a_by_time = {}
    for r in rows:
        a_by_time.setdefault(str(r.get("shot_time", "")), []).append(r)

    missing, fc_wrong, dup_rows, no_rowid = [], [], [], []
    matched_a = set()
    for st in sorted(tl.keys()):
        d = tl[st]
        key = (str(d.get("machine", "")), st, str(d.get("first_file", "")))
        hit = a_by_key.get(key)
        if not hit:
            # 退一步：只按时间找（first_file 可能因分组差异不同）
            hit = a_by_time.get(st)
        if not hit:
            missing.append(d)
            continue
        r = hit[0]
        matched_a.add(r["id"])
        d["row_id"] = d.get("row_id") or r["id"]
        if not d.get("row_id"):
            no_rowid.append((st, r["id"]))
        if len(hit) > 1:
            dup_rows.append((st, [x["id"] for x in hit]))
        if int(r.get("file_count") or 0) != int(d.get("file_count") or 0):
            fc_wrong.append({"shot_time": st, "no": d.get("no"),
                             "ledger_fc": d.get("file_count"),
                             "a_fc": r.get("file_count"), "row_id": r["id"],
                             "names": d.get("names"), "ledger": d})
    # A 机有、账本没有的本机行（可疑多行）
    extra = []
    for r in rows:
        if str(r.get("machine", "")) != str(rmachine or ""):
            continue                      # 别的机器报的行不归本机账本管
        if r["id"] in matched_a:
            continue
        extra.append(r)

    def head(t, n):
        print("\n[%s] %d 条" % (t, n))

    head("缺行（账本有、A机没有）", len(missing))
    for d in missing[:40]:
        print("   %s no=%s fc=%s files=%s"
              % (d["shot_time"], d.get("no"), d.get("file_count"),
                 ",".join(d.get("names") or [])[:70]))
    if len(missing) > 40:
        print("   ...（另有 %d 条）" % (len(missing) - 40))

    head("file_count 不符（需删行重建）", len(fc_wrong))
    for x in fc_wrong[:40]:
        print("   %s no=%s 行#%s  账本fc=%s  A机fc=%s  账本文件=%s"
              % (x["shot_time"], x["no"], x["row_id"], x["ledger_fc"],
                 x["a_fc"], ",".join(x["names"] or [])[:60]))

    head("可疑多行（A机有、账本无，且机名=本机）", len(extra))
    for r in extra[:40]:
        f = r.get("fields") or {}
        print("   行#%s %s no=%s machine=%s fc=%s ff=%s"
              % (r["id"], r.get("shot_time"), f.get("no"), r.get("machine"),
                 r.get("file_count"), r.get("first_file")))

    head("同键重复行", len(dup_rows))
    for st, ids in dup_rows[:20]:
        print("   %s → 行 %s" % (st, ids))

    # ---- 能量对账 ----
    ens = collect_energies(events)
    unbound, misbound, stashed = [], [], []
    for x in ens:
        if x.get("stashed"):
            stashed.append(x)
            continue
        st = str(x.get("shot_time") or "")
        cand = a_by_time.get(st) or []
        if not cand:
            unbound.append(dict(x, why="A机无该时间的行"))
            continue
        r = cand[0]
        f = r.get("fields") or {}
        fld = x.get("field")
        cur = str(f.get(fld) or "").strip()
        if not cur:
            unbound.append(dict(x, row_id=r["id"], why="行#%s 的 %s 为空" % (r["id"], fld)))
        elif cur != str(x.get("energy") or "").strip():
            misbound.append(dict(x, row_id=r["id"], cur=cur))
    head("能量未绑上", len(unbound))
    for x in unbound[:40]:
        print("   %s %s=%s → %s（%s）"
              % (x.get("iso"), x.get("field"), x.get("energy"),
                 x.get("shot_time") or "?", x.get("why")))
    head("能量值不一致（账本 vs A机）", len(misbound))
    for x in misbound[:40]:
        print("   %s 行#%s %s 账本=%s A机=%s"
              % (x.get("shot_time"), x.get("row_id"), x.get("field"),
                 x.get("energy"), x.get("cur")))
    head("暂存待补绑的能量", len(stashed))
    for x in stashed[:40]:
        print("   %s %s=%s filename=%s"
              % (x.get("iso"), x.get("field"), x.get("energy"), x.get("filename")))

    ok = not (missing or fc_wrong or extra or dup_rows or unbound or stashed)
    print("\n" + "=" * 68)
    print("结论：%s" % ("全绿，账本与 A 机表一致" if ok else
                        "缺行 %d｜fc不符 %d｜可疑多行 %d｜重复 %d｜能量未绑 %d｜暂存 %d"
                        % (len(missing), len(fc_wrong), len(extra),
                           len(dup_rows), len(unbound), len(stashed))))
    if not ok:
        print("修复：py rebuild_from_ledger.py apply --day %s --yes" % day)
        print("      （可加 --only missing|energy|fc 只修一类）")
    print("=" * 68)
    # 记账：对账动作本身也进账本（谁在什么时候核过、结果如何）
    ledger.append({"ev": "reconcile", "mode": "report", "day": day,
                   "sheet_id": sid, "a_rows": len(rows),
                   "ledger_shots": len(tl),
                   "missing": len(missing), "fc_wrong": len(fc_wrong),
                   "extra": len(extra), "dup_rows": len(dup_rows),
                   "unbound": len(unbound), "misbound": len(misbound),
                   "stashed": len(stashed)}, day=day)
    return 0 if ok else 1


# ---------------------------------------------------------------- apply
def _replay_shot(server, d, day):
    """重放一条发次到 A 机 /api/shot（去重键幂等，重复重放只 duplicate）"""
    files = []
    for f in d.get("files") or []:
        if isinstance(f, dict):
            files.append({"name": f.get("name", ""),
                          "folder": f.get("folder", ""),
                          "mtime": f.get("mtime", 0)})
        else:
            files.append({"name": str(f), "mtime": 0})
    if not files:
        files = [{"name": n, "mtime": 0} for n in d.get("names") or []]
    payload = {"machine": d.get("machine", ""), "shot_time": d["shot_time"],
               "files": files, "fields": d.get("fields") or {},
               "reported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "sheet_name": d.get("sheet_name") or day}
    return _post(server + "/api/shot", payload)


def cmd_apply(args):
    cfg = load_cfg()
    server = args.server or cfg["server"]
    day = args.day or datetime.now().strftime("%Y-%m-%d")
    rmachine = cfg["report_machine"]
    if not args.yes:
        print("拒绝执行：apply 会写入/删除 A 机行。确认无误后加 --yes 再跑。")
        print("建议先跑：py rebuild_from_ledger.py report --day %s" % day)
        return 2
    only = set(args.only.split(",")) if args.only else {"missing", "energy", "fc"}

    events, _ = ledger.read_day(day)
    sid, sname, _ = find_sheet(server, day)
    if sid is None:
        print("A 机找不到表 %s，中止" % day)
        return 2
    rows, _ = fetch_rows(server, sid)
    tl = build_timeline(events, rmachine)
    a_by_key, a_by_time = {}, {}
    for r in rows:
        a_by_key.setdefault((str(r.get("machine", "")), str(r.get("shot_time", "")),
                             str(r.get("first_file", ""))), []).append(r)
        a_by_time.setdefault(str(r.get("shot_time", "")), []).append(r)

    actions = []
    print("=== apply %s (sheet %s / id=%s) 只修: %s ==="
          % (day, sname, sid, ",".join(sorted(only))))

    # 1) 缺行重放
    if "missing" in only:
        n = 0
        for st in sorted(tl.keys()):
            d = tl[st]
            key = (str(d.get("machine", "")), st, str(d.get("first_file", "")))
            if a_by_key.get(key) or a_by_time.get(st):
                continue
            try:
                j = _replay_shot(server, d, day)
            except Exception as e:
                print("  ✗ 重放失败 %s: %r" % (st, e))
                actions.append({"act": "replay_fail", "shot_time": st, "err": repr(e)})
                continue
            n += 1
            print("  ✓ 重放缺行 %s no=%s → 行#%s (duplicate=%s)"
                  % (st, d.get("no"), j.get("id"), j.get("duplicate")))
            actions.append({"act": "replay", "shot_time": st, "no": d.get("no"),
                            "row_id": j.get("id"), "duplicate": j.get("duplicate"),
                            "resp": j})
        print("缺行重放 %d 条" % n)

    # 2) 能量补绑
    if "energy" in only:
        ens = collect_energies(events)
        n = 0
        for x in ens:
            if x.get("stashed"):
                print("  - 跳过暂存能量 %s（tif 时间表未到，无法定位发次）"
                      % x.get("filename"))
                continue
            st = str(x.get("shot_time") or "")
            cand = a_by_time.get(st) or []
            if not cand:
                continue
            r = cand[0]
            f = r.get("fields") or {}
            fld = x.get("field")
            cur = str(f.get(fld) or "").strip()
            want = str(x.get("energy") or "").strip()
            if cur == want:
                continue
            try:
                j = _post(server + "/api/energy",
                          {"shot_time": st, "energy": want, "field": fld,
                           "machine": rmachine, "window_sec": 5,
                           "shot_no": x.get("no") or 0, "create": False})
            except Exception as e:
                print("  ✗ 能量补绑失败 %s %s: %r" % (st, fld, e))
                actions.append({"act": "energy_fail", "shot_time": st,
                                "field": fld, "err": repr(e)})
                continue
            n += 1
            print("  ✓ 能量 %s %s=%s → 行#%s (%s)"
                  % (st, fld, want, j.get("matched") or j.get("created"),
                     j.get("error") or "ok"))
            actions.append({"act": "energy", "shot_time": st, "field": fld,
                            "energy": want, "row_id": j.get("matched"),
                            "resp": j})
        print("能量补绑 %d 条" % n)

    # 3) file_count 修复（删行 + 重建）—— 危险，逐条打印
    if "fc" in only:
        todo = []
        for st in sorted(tl.keys()):
            d = tl[st]
            hit = a_by_key.get((str(d.get("machine", "")), st,
                                str(d.get("first_file", "")))) or a_by_time.get(st)
            if not hit:
                continue
            r = hit[0]
            if int(r.get("file_count") or 0) != int(d.get("file_count") or 0):
                todo.append((st, d, r))
        if todo:
            print("\n⚠ file_count 修复要**删行重建**（/api/field 改不了 fc）：")
            for st, d, r in todo:
                print("    行#%s %s no=%s  A机fc=%s → 账本fc=%s（%s）"
                      % (r["id"], st, d.get("no"), r.get("file_count"),
                         d.get("file_count"), ",".join(d.get("names") or [])))
            print("  删的行会进 A 机回收站，可用页面「回收站」或 /api/trash/restore 恢复。")
            if not args.force_fc:
                print("  需要再加 --force-fc 才真的执行（这是唯一会删行的操作）")
            else:
                for st, d, r in todo:
                    keep_fields = dict(r.get("fields") or {})
                    for k, v in (d.get("fields") or {}).items():
                        keep_fields.setdefault(k, v)
                    d2 = dict(d)
                    d2["fields"] = keep_fields      # 保住原行已有的能量/靶位
                    try:
                        _post(server + "/api/row/delete", {"ids": [r["id"]]})
                        j = _replay_shot(server, d2, day)
                        print("  ✓ 行#%s 已删并重建 → 行#%s fc=%s"
                              % (r["id"], j.get("id"), d2.get("file_count")))
                        actions.append({"act": "fc_rebuild", "old_row_id": r["id"],
                                        "shot_time": st, "new_row_id": j.get("id"),
                                        "old_fc": r.get("file_count"),
                                        "new_fc": d2.get("file_count"), "resp": j})
                    except Exception as e:
                        print("  ✗ 行#%s 重建失败: %r（原行已进回收站，请手动恢复！）"
                              % (r["id"], e))
                        actions.append({"act": "fc_rebuild_fail", "old_row_id": r["id"],
                                        "shot_time": st, "err": repr(e)})
        else:
            print("file_count 全部一致，无需重建")

    ledger.append({"ev": "reconcile", "mode": "apply", "day": day,
                   "sheet_id": sid, "only": sorted(only),
                   "actions": actions}, day=day)
    print("\n完成，%d 个动作已记入账本。建议再跑一次 report 复核。" % len(actions))
    return 0


# ---------------------------------------------------------------- pull-a
def cmd_pull_a(args):
    cfg = load_cfg()
    server = args.server or cfg["server"]
    day = args.day or datetime.now().strftime("%Y-%m-%d")
    evs = args.ev or "a_energy_in,a_energy_bind,a_energy_nomatch,a_shot"
    try:
        r = _get("%s/api/ledger?day=%s&ev=%s&limit=%d"
                 % (server, day, evs, args.limit))
    except Exception as e:
        print("拉取 A 机账本失败: %r" % e)
        print("（A 机需已部署本次改动并重启；旧版没有 /api/ledger）")
        return 2
    if not r.get("ok"):
        print("A 机返回: %s" % r.get("error"))
        return 2
    lines = r.get("lines") or []
    print("A 机账本 %s：拉到 %d 条（%s）" % (day, len(lines), evs))
    n = 0
    for e in lines:
        rec = dict(e)
        rec["ev"] = str(e.get("ev", ""))
        rec["pulled_from_a"] = True
        rec["pulled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # ledger.append 会把 ts/iso 刷成回灌时刻 → 把 A 机原始时间另存，
        # 重建/排序时以 orig_ts 为准（事件真正发生的时刻在 A 机那边）
        rec["orig_ts"] = e.get("ts")
        rec["orig_iso"] = e.get("iso")
        if ledger.append(rec, day=day):
            n += 1
    print("已回灌 %d 条进本地账本 %s" % (n, ledger.path_for(day)))
    print("提示：回灌行的 ev 仍以 a_ 开头、带 pulled_from_a=true，与本机原生事件区分。")
    return 0


# ---------------------------------------------------------------- tif
def cmd_tif(args):
    day = args.day or datetime.now().strftime("%Y-%m-%d")
    events, _ = ledger.read_day(day)
    tifs = {}
    for e in events:
        if e.get("ev") == "tif_timeline":
            for f in e.get("files") or []:
                if isinstance(f, dict) and f.get("name"):
                    tifs[str(f["name"]).lower()] = dict(
                        f, machine=e.get("machine"), day=e.get("day"))
    tl = build_timeline(events, load_cfg()["report_machine"])
    print("=" * 74)
    print("tif 时间线核对 %s：账本里 %d 张 tif、%d 个 B机发次" % (day, len(tifs), len(tl)))
    print("=" * 74)
    if not tifs:
        print("\n账本里没有 tif_timeline 事件。")
        print("检查 C 机：config_b.local.json 是否配了 push_tif_timeline_to、是否重启。")
        return 0
    shots = sorted(tl.values(), key=lambda d: d["shot_time"])
    print("\n%-16s %-22s %-6s %-20s %-8s %s"
          % ("tif", "tif时间(mtime)", "B机No", "最近的B机PNG发次", "差(秒)", "判定"))
    print("-" * 74)
    for name in sorted(tifs.keys()):
        t = tifs[name]
        mt = float(t.get("mtime") or 0)
        tis = datetime.fromtimestamp(mt).strftime("%H:%M:%S.%f")[:-4] if mt else "?"
        best, bd = None, None
        for d in shots:
            e = _epoch(d["shot_time"])
            if e is None:
                continue
            dd = abs(e - mt)
            if bd is None or dd < bd:
                best, bd = d, dd
        if best is None:
            print("%-16s %-22s %-6s %-20s %-8s %s"
                  % (name, tis, "-", "（账本无发次）", "-", "?"))
            continue
        verdict = "OK" if bd <= 15 else "⚠超15s窗口"
        print("%-16s %-22s %-6s %-20s %-8.2f %s"
              % (name, tis, best.get("no") or "-", best["shot_time"], bd, verdict))
    print("\n说明：唯一时间基准是 B机 shot PNG 的 mtime；C机发次号不参与匹配。")
    print("     差值 >15s 说明 B/C 机时钟偏差过大或该 tif 没有对应发次，需人工核。")
    return 0


# ---------------------------------------------------------------- main
def main():
    # 公共参数做成 parent parser，让每个子命令都接 --day/--server。
    # 用 default=SUPPRESS：不提供时**不写入 namespace**，避免子命令默认值
    # 把顶层已解析的同名值覆盖成 None（argparse 的经典陷阱）。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--day", default=argparse.SUPPRESS,
                        help="日期 YYYY-MM-DD（默认今天）")
    common.add_argument("--server", default=argparse.SUPPRESS,
                        help="A 机地址（默认读 config）")

    ap = argparse.ArgumentParser(
        description="从本地账本对账/重建 A 机日志表格（默认只读）")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("report", parents=[common], help="只读对账（默认）")

    p = sub.add_parser("apply", parents=[common], help="写入 A 机修复（需 --yes）")
    p.add_argument("--yes", action="store_true", help="确认执行写入")
    p.add_argument("--only", default=argparse.SUPPRESS,
                   help="只修某几类：missing,energy,fc")
    p.add_argument("--force-fc", action="store_true",
                   help="允许 file_count 删行重建（唯一会删行的操作）")

    p = sub.add_parser("pull-a", parents=[common],
                       help="把 A 机侧账本回灌进本地账本")
    p.add_argument("--ev", default=argparse.SUPPRESS,
                   help="要拉的事件类型，逗号分隔")
    p.add_argument("--limit", type=int, default=20000)

    sub.add_parser("tif", parents=[common], help="PNG × tif 时间线核对")

    # 没写子命令时默认 report：前置补上，保证始终是"子命令 在前"的规范形式
    argv = sys.argv[1:]
    if not (argv and argv[0] in ("report", "apply", "pull-a", "tif")):
        argv = ["report"] + argv
    args = ap.parse_args(argv)

    # SUPPRESS 掉的键可能不存在 → 补默认值
    for k, default in (("cmd", "report"), ("day", None), ("server", None),
                       ("only", None), ("ev", None), ("limit", 20000),
                       ("yes", False), ("force_fc", False)):
        if not hasattr(args, k):
            setattr(args, k, default)

    return {"report": cmd_report, "apply": cmd_apply,
            "pull-a": cmd_pull_a, "tif": cmd_tif}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
