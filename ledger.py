# -*- coding: utf-8 -*-
"""
本地账本（ledger）—— 灾后重建的唯一真源
=====================================
为什么需要它：
  A 机表格曾多次混乱（发次被时间窗合并吃掉、序号错位、txt 假发次、
  file_count 不可改）。事后只能靠回收站/人工记忆去猜，
  shot79/80 的能量归属至今查不清。
  根因是**本地没有一份"发生过什么"的不可变记录**。

这个模块做的事：
  把每一件关键事实（发次落盘、上报成功/失败、能量到达、能量匹配结果、
  C 机 tif 时间表、对账动作）按天追加写进 `ledger/YYYY-MM-DD.jsonl`。

JSONL = JSON Lines，纯文本，**每行一个独立 JSON 对象，只追加、永不改写**。
  - 进程被杀最多丢正在写的那一行（不像 SQLite 可能损坏整库）
  - 记事本 / grep 直接可读
  - rebuild_from_ledger.py 逐行读回放即可重建 A 机表格
  - 纯标准库，零依赖

与 state_helper.json 的区别（很重要）：
  state_helper.json 是**可变快照**，会被 api_shots_clear 清空、被 save_json 整体重写，
  且 shots 条目不含 mtime/folder/reported_at —— 它证明不了"发生过什么"。
  账本是**追加日志**，写下的行永不被修改，才是重建的依据。

用法：
    import ledger
    ledger.append({"ev": "shot_group", "no": 21, "files": [...]})   # 永不抛异常

线程安全：helper 是多线程（monitor 线程 + HTTP handler 线程 + 靶位轮询线程），
模块级锁串行化写入；每条都 flush + fsync，保证掉电/被杀不丢已确认的行。

目录覆盖：环境变量 LSL_LEDGER_DIR（A 机侧用 ledger_a，与 B 机分开）。
"""

import json
import os
import socket
import threading
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))

#: 账本目录。A 机跑 a_server.py 时用 ledger_a/，与 B 机的 ledger/ 分开，
#: 避免两台机器的账本混在一个目录里（重建时分不清 host 归属）。
LEDGER_DIR = os.environ.get("LSL_LEDGER_DIR") or os.path.join(BASE, "ledger")

_lock = threading.Lock()

_HOST = ""
try:
    _HOST = socket.gethostname()
except Exception:
    _HOST = "unknown"


def day_of(ts=None):
    """账本按哪天归档。默认=现在；给 ts(浮点 epoch) 则按该时刻。

    注意：按**发次时间**归档更合理时，调用方应显式传 day= 参数，
    因为跨零点补发/延迟匹配的事件，其"发生时刻"与"数据归属日"可能不同。
    """
    dt = datetime.fromtimestamp(ts) if ts else datetime.now()
    return dt.strftime("%Y-%m-%d")


def path_for(day):
    return os.path.join(LEDGER_DIR, "%s.jsonl" % day)


def append(ev, day=None):
    """追加一条账本事件。**永不抛异常**（记账失败绝不能拖垮打靶主流程）。

    ev   : dict，必须含 "ev"（事件类型）；其余字段自定义。
    day  : 归档日期 "YYYY-MM-DD"；None = 按当前时刻。
           跨零点补发/延迟匹配的事件建议显式传数据的归属日。

    自动补的公共字段（调用方同名值会被覆盖，保证一致性）：
      ts    浮点 epoch（time.time()）——**最高精度时间，重建排序用它**
      iso   本地秒级 "%Y-%m-%d %H:%M:%S"（给人看 / 对 A 机 shot_time）
      host  本机机名
    """
    try:
        if not isinstance(ev, dict):
            return False
        now = time.time()
        rec = dict(ev)
        rec["ts"] = round(now, 3)
        rec["iso"] = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
        rec.setdefault("host", _HOST)
        d = day or day_of(now)
        p = path_for(d)
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        with _lock:
            os.makedirs(LEDGER_DIR, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass  # 某些文件系统/网络盘不支持 fsync，退化为 flush
        return True
    except Exception:
        return False


def read_day(day):
    """读某天账本，返回事件列表。坏行跳过并计数（返回 (events, bad_lines)）。"""
    p = path_for(day)
    out, bad = [], 0
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    bad += 1  # 被杀时可能留下半行，跳过不致命
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return out, bad


def iter_days(reverse=True):
    """列出账本里已有的日期（"YYYY-MM-DD"）。reverse=True 最新在前。"""
    days = []
    try:
        for n in os.listdir(LEDGER_DIR):
            if n.endswith(".jsonl") and len(n) == 16:  # YYYY-MM-DD.jsonl
                days.append(n[:-6])
    except Exception:
        return []
    return sorted(days, reverse=reverse)


def stats(day):
    """某天账本的事件类型计数（体检用）。"""
    evs, bad = read_day(day)
    c = {}
    for e in evs:
        k = str(e.get("ev", "?"))
        c[k] = c.get(k, 0) + 1
    return {"day": day, "total": len(evs), "bad_lines": bad, "by_ev": c}


# ---------------------------------------------------------------- 文本日志落盘
# 为什么要有这个：thomson_helper.py / b_watcher.py 的 log() 原本只 print 到控制台，
# 不写文件。仓库里那个 thomson_helper.log 是 2026-09-17 的死文件。
# 后果：能量绑定、上报失败这些关键痕迹只活在 cmd 窗口里，**关窗即失** ——
# 09-30 的 tps_h=19.23 到底属于 shot79 还是 shot80，就是这么查不清的。
# 现在 log() 除了 print 还追加写 logs/<name>_YYYY-MM-DD.log，按天分文件便于清理。

LOG_DIR = os.path.join(BASE, "logs")
_log_lock = threading.Lock()


def log_line(name, msg, ts=None):
    """追加一行文本日志到 logs/<name>_YYYY-MM-DD.log。**永不抛异常**。

    name : 日志名，如 "thomson_helper" / "b_watcher" / "a_server"
    msg  : 已格式化好的消息（调用方自己决定要不要带时间前缀）
    ts   : 指定时刻（浮点 epoch）；None = 现在。跨零点补发时可显式传。
    """
    try:
        d = datetime.fromtimestamp(ts) if ts else datetime.now()
        p = os.path.join(LOG_DIR, "%s_%s.log" % (name, d.strftime("%Y-%m-%d")))
        line = "[%s] %s\n" % (d.strftime("%Y-%m-%d %H:%M:%S"), msg)
        with _log_lock:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    # 自检：python ledger.py [日期]
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else day_of()
    print("LEDGER_DIR =", LEDGER_DIR)
    print("days       =", iter_days())
    print("stats      =", json.dumps(stats(d), ensure_ascii=False, indent=1))
    evs, bad = read_day(d)
    print("--- %s 最后 5 条 ---" % d)
    for e in evs[-5:]:
        print(json.dumps(e, ensure_ascii=False)[:220])
