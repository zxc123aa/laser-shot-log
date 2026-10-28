# -*- coding: utf-8 -*-
"""
2026-10-28 target_type 污染修复（A 机一次性工具）

原则：靶类型以**上报系统（B机 157 /api/shot）最后上报值为唯一真相**。
从账本重放每行写入链重建"B 机真值"：
  - a_shot insert       → fields.target_type（B 机初始上报）
  - a_shot merged(含 target_type) → B 机合并补填（值未知，由下一条 a_field 的 old 揭示）
  - a_field client_ip=10.0.23.157 → B 机自己的修正，接受为新真值
  - a_field 其他 IP(117/旧对账)   → 污染，忽略

处置：
  - DB 当前值 == B 机真值          → 不动
  - B 机真值非空 且 DB 值不同      → 恢复真值（走 /api/field，审计留痕 client_ip=127.0.0.1）
  - B 机真值为空 且 DB 当前值非空  → 清空 + target_type_lock=true（直写 DB + 补 a_repair 账本事件；
                                     锁只挡 117 对账回填，不挡 B 机 /api/shot 合并补空）
另：删除 -Thomson 裸行 id=1763（走 /api/row/delete，进回收站可恢复）。
执行前全量备份到 backup/target_type_fix_20261028.json。
"""
import json, sqlite3, urllib.request, os
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(BASE, "ledger_a", "2026-10-28.jsonl")
DB = os.path.join(BASE, "shots.db")
BACKUP = os.path.join(BASE, "backup", "target_type_fix_20261028.json")
SERVER = "http://127.0.0.1:8765"
DAY = "2026-10-28"
B_IP = "10.0.23.157"


def http_post(path, payload, timeout=6):
    req = urllib.request.Request(
        SERVER + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def main():
    # 1) 账本 → 每行事件时间线
    evs = {}          # id -> list of (ts, kind, payload)
    inserts = {}      # id -> insert fields
    for ln in open(LEDGER, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
        except Exception:
            continue
        rid = e.get("id")
        if rid is None:
            continue
        if e.get("ev") == "a_shot" and e.get("action") == "insert":
            inserts[rid] = (e.get("fields") or {})
            evs.setdefault(rid, []).append((e.get("ts") or 0, "insert", e))
        elif e.get("ev") == "a_shot" and e.get("action") == "merged":
            if "target_type" in (e.get("merged_fields") or []):
                evs.setdefault(rid, []).append((e.get("ts") or 0, "merged", e))
        elif e.get("ev") == "a_field" and e.get("field") == "target_type":
            evs.setdefault(rid, []).append((e.get("ts") or 0, "field", e))

    # 2) 今天 DB 行
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, shot_time, fields, rev, machine FROM shots "
        "WHERE shot_time LIKE ?", (DAY + "%",)).fetchall()

    plan = []
    for r in rows:
        rid = r["id"]
        f = json.loads(r["fields"] or "{}")
        cur = str(f.get("target_type") or "").strip()
        fld_evs = sorted(evs.get(rid, []), key=lambda x: x[0])

        # 重放 B 机真值
        ins = inserts.get(rid)
        if ins is None:
            plan.append({"id": rid, "no": f.get("no"), "pos": f.get("target_pos"),
                         "before": cur, "truth": "?", "action": "SKIP(无insert账本)"})
            continue
        truth = ins.get("target_type")
        truth_known = truth is not None
        cur_truth = str(truth or "").strip()
        pending_merge = False
        for ts, kind, e in fld_evs:
            if kind == "merged":
                pending_merge = True          # B 机合并补填，值待下一条 a_field.old 揭示
            elif kind == "field":
                if pending_merge:
                    cur_truth = str(e.get("old") or "").strip()
                    pending_merge = False
                if (e.get("client_ip") or "") == B_IP:
                    cur_truth = str(e.get("new") or "").strip()
        # B 机真值 = cur_truth；insert 明确报了空串也算空
        if not truth_known and not any(k == "field" for _, k, _ in fld_evs) \
                and not pending_merge:
            cur_truth = ""                    # insert 没报 target_type 且从未被写过

        if cur == cur_truth:
            action = "OK"
        elif cur_truth:
            action = "RESTORE"
        else:
            action = "CLEAR+LOCK"
        plan.append({"id": rid, "no": f.get("no"), "pos": f.get("target_pos"),
                     "before": cur, "truth": cur_truth, "action": action,
                     "machine": r["machine"]})

    # 3) 备份
    os.makedirs(os.path.dirname(BACKUP), exist_ok=True)
    snap = [{"id": p["id"], "no": p["no"], "pos": p["pos"],
             "target_type_before": p["before"], "truth": p["truth"],
             "action": p["action"]} for p in plan]
    with open(BACKUP, "w", encoding="utf-8") as fh:
        json.dump({"day": DAY, "generated": datetime.now().isoformat(),
                   "rows": snap}, fh, ensure_ascii=False, indent=1)
    print("备份 →", BACKUP, "(%d 行)" % len(snap))

    # 4) 执行
    n_restore = n_clear = n_del = 0
    for p in plan:
        if p["action"] == "RESTORE":
            j = http_post("/api/field", {"id": p["id"], "field": "target_type",
                                         "value": p["truth"]})
            if j.get("ok"):
                n_restore += 1
            else:
                print("  ! 恢复失败 id", p["id"], j)
        elif p["action"] == "CLEAR+LOCK":
            with sqlite3.connect(DB) as c:
                row = c.execute("SELECT fields FROM shots WHERE id=?",
                                (p["id"],)).fetchone()
                flds = json.loads(row[0] or "{}")
                flds["target_type"] = ""
                flds["target_type_lock"] = "true"
                c.execute("UPDATE shots SET fields=?, rev=COALESCE(rev,1)+1 "
                          "WHERE id=?",
                          (json.dumps(flds, ensure_ascii=False), p["id"]))
            # 补审计事件（直写 DB 不经过 api_field，手动落账保持链路完整）
            with open(LEDGER, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ev": "a_repair", "id": p["id"], "field": "target_type",
                    "old": p["before"], "new": "", "lock": True,
                    "reason": "2026-10-28 污染修复: B机真值为空(空发/未报), "
                              "清空+上锁防对账回填",
                    "client_ip": "127.0.0.1",
                    "iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                    ensure_ascii=False) + "\n")
            n_clear += 1

    # 5) 删 -Thomson 裸行 id=1763
    with sqlite3.connect(DB) as c:
        r = c.execute("SELECT machine, fields FROM shots WHERE id=1763").fetchone()
    if r and "Thomson" in (r[0] or ""):
        j = http_post("/api/row/delete", {"ids": [1763]})
        if j.get("ok"):
            n_del = 1
            with open(LEDGER, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ev": "a_repair", "id": 1763, "action": "row_delete",
                    "reason": "删除 -Thomson 裸行(能量层违规建行, 仅含no=100)",
                    "client_ip": "127.0.0.1",
                    "iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                    ensure_ascii=False) + "\n")
        else:
            print("  ! 删除 1763 失败", j)
    else:
        print("  id1763 不存在或非 -Thomson 行，跳过删除")

    # 6) 汇总
    from collections import Counter
    cnt = Counter(p["action"] for p in plan)
    print("计划分类:", dict(cnt))
    print("执行: 恢复 %d 行, 清空+上锁 %d 行, 删除裸行 %d 行" %
          (n_restore, n_clear, n_del))
    print("\n── 清空+上锁(待 B 机重发补值) ──")
    for p in plan:
        if p["action"] == "CLEAR+LOCK":
            print("  id=%s no=%s pos=%s before=%r" %
                  (p["id"], p["no"], p["pos"], p["before"]))
    print("\n── 恢复(以 B 机上报值为准) ──")
    for p in plan:
        if p["action"] == "RESTORE":
            print("  id=%s no=%s pos=%s %r → %r" %
                  (p["id"], p["no"], p["pos"], p["before"], p["truth"]))


if __name__ == "__main__":
    main()
