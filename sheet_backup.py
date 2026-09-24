#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sheet_backup.py —— B 机端表格自动备份（纯标准库，Excel 文件由 A 机生成）

功能：
  - 每 INTERVAL_SEC 秒查一次 A 机 /api/sheets
  - 某张表记录数(count)变化 / 新表出现 → 立即导出该表 xlsx
  - 每 FULL_REFRESH_MIN 分钟无条件全量重导一次（覆盖单元格编辑类的小改动）
  - 保存到 <BASE>/shotlist/<今天日期>/<表名>.xlsx，跨天自动建新日期文件夹
  - A 机断连自动重试，不影响本地已有备份
"""

import json
import os
import re
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))


def _load_helper_port():
    """上报系统(8767 靶类型映射页)端口，从 config_helper.json 读，默认 8767"""
    try:
        c = json.load(open(os.path.join(BASE, "config_helper.json"),
                           encoding="utf-8"))
        return int(c.get("helper_port") or 8767)
    except Exception:
        return 8767

def _bypass_proxy_for_lan():
    """实验室内网地址不走系统代理（http_proxy 是会话级动态端口，会把
    10.0.23.x 内网请求拖死）。把 A 机/靶系统主机名加入 NO_PROXY。"""
    from urllib.parse import urlparse as _up
    hosts = {"127.0.0.1", "localhost"}
    for cfg in ("config_b.local.json", "config_helper.json"):
        p = os.path.join(BASE, cfg)
        if not os.path.exists(p):
            continue
        try:
            c = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for key in ("server_url",):
            u = c.get(key)
            if u:
                h = _up(str(u)).hostname
                if h:
                    hosts.add(h)
        tm = c.get("target_monitor") or {}
        for key in ("url", "server"):
            u = tm.get(key)
            if u:
                h = _up(str(u)).hostname
                if h:
                    hosts.add(h)
    cur = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    for h in sorted(hosts):
        if h and h not in cur:
            cur = (cur + "," + h) if cur else h
    os.environ["NO_PROXY"] = cur
    os.environ["no_proxy"] = cur


_bypass_proxy_for_lan()
SERVER_URL = "http://10.0.23.155:8765"
OUT_ROOT = os.path.join(BASE, "shotlist")
INTERVAL_SEC = 30
FULL_REFRESH_MIN = 30
STATE_PATH = os.path.join(BASE, "backup_state.json")
HELPER_PORT = _load_helper_port()
TTM_HIST_DIR = os.path.join(OUT_ROOT, "靶类型映射_历史")

# 内网直连，绕过系统代理（代理可能拦截内网/返回 502）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def http_get(path, timeout=15):
    req = urllib.request.Request(SERVER_URL.rstrip("/") + path,
                                 headers={"User-Agent": "sheet-backup"})
    with _opener.open(req, timeout=timeout) as r:
        return r.read()


def safe_name(name):
    return re.sub(r'[\\/:*?"<>|]', "_", (name or "sheet")).strip() or "sheet"


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


class FileLockedError(Exception):
    """目标 xlsx 被其他程序占用（如正被打开预览/编辑）"""


def _swap_in(tmp, path):
    """把 tmp 原子移入 path。

    常规走 os.replace；若目标被预览/杀软以内存映射等特殊方式占用
    （表现为 os.replace 报 WinError 5，但重命名/读写正常），则把旧文件
    改名让位后再移入——重命名不受这类占用影响。
    """
    try:
        os.replace(tmp, path)
        return
    except PermissionError:
        pass
    stale = path + ".stale"
    try:
        if os.path.exists(stale):
            os.remove(stale)
    except OSError:
        pass  # 旧 .stale 删不掉就覆盖它
    try:
        os.rename(path, stale)     # 旧文件让位（重命名不受内存映射占用影响）
        os.replace(tmp, path)      # 目标已不存在，纯移动必成功
    except OSError:
        raise FileLockedError(path)
    try:
        os.remove(stale)
    except OSError:
        pass  # 删不掉就留给下轮清理


def export_sheet(sheet_id, name):
    data = http_get("/export.xlsx?sheet_id=%d" % sheet_id)
    day_dir = os.path.join(OUT_ROOT, time.strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, safe_name(name) + ".xlsx")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    _swap_in(tmp, path)
    log("已备份: %s (%d KB)" % (path, len(data) // 1024))


def try_export(sheet, state, pending):
    """导出单张表；成功返回 True。失败不抛出，只登记待重试。"""
    sid = str(sheet["id"])
    try:
        export_sheet(sheet["id"], sheet["name"])
        state.setdefault("sheets", {})[sid] = \
            {"name": sheet["name"], "count": sheet.get("count")}
        if sid in pending:
            pending.discard(sid)
            log("占用解除，补写成功: %s.xlsx" % safe_name(sheet["name"]))
        return True
    except FileLockedError:
        if sid not in pending:
            pending.add(sid)
            log("目标 xlsx 被占用(可能正被打开预览/编辑): %s.xlsx — "
                "新数据已暂存 .tmp，文件关闭后 30 秒内自动补写"
                % safe_name(sheet["name"]))
        return False
    except Exception as e:
        log("导出表 %s 失败: %r (下轮重试)" % (sheet.get("name"), e))
        return False


def backup_target_type(state):
    """备份上报系统生效的靶类型映射表（xls 基表 + 手动覆盖合并后的最终版，
    即上报时写进 A 机 target_type 列的那张表）。

    - 每天一份最新快照：<OUT_ROOT>/<今天日期>/靶类型映射.json
    - 每次内容变更追加一份历史版：<OUT_ROOT>/靶类型映射_历史/YYYYMMDD_HHMMSS.json
    - 内容没变不重复写；上报页离线只提示一次，不影响表格备份。
    成功/有变化返回 True（调用方据此落盘 state）。
    """
    url = "http://127.0.0.1:%d/api/targetmap" % HELPER_PORT
    try:
        with _opener.open(urllib.request.Request(
                url, headers={"User-Agent": "sheet-backup"}),
                timeout=10) as r:
            data = json.load(r)
    except Exception as e:
        if not state.get("ttm_down"):
            log("靶类型映射页(127.0.0.1:%d)不可达: %r（表格备份不受影响）"
                % (HELPER_PORT, e))
        state["ttm_down"] = True
        return False
    if not data.get("ok"):
        return False
    if state.pop("ttm_down", None):
        log("靶类型映射页已恢复可达")
    import hashlib
    snap = {"saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "title": data.get("title"),
            "map": data.get("map") or {},
            "overrides": data.get("overrides") or {},
            "layout": data.get("layout")}
    h = hashlib.md5(json.dumps(snap, ensure_ascii=False, sort_keys=True,
                               default=str).encode("utf-8")).hexdigest()
    today = time.strftime("%Y-%m-%d")
    force = state.get("ttm_day") != today      # 跨天即使没变也写当天快照
    if h == state.get("ttm_hash") and not force:
        return False
    day_dir = os.path.join(OUT_ROOT, today)
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, "靶类型映射.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False, indent=1))
    _swap_in(tmp, path)
    if h != state.get("ttm_hash"):             # 内容真的变了才记历史版
        os.makedirs(TTM_HIST_DIR, exist_ok=True)
        hist = os.path.join(TTM_HIST_DIR,
                            time.strftime("%Y%m%d_%H%M%S") + ".json")
        with open(hist, "w", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False, indent=1))
        log("靶类型映射有变更，已备份: %s（%d 个靶位，历史版 %s）"
            % (path, len(snap["map"]), os.path.basename(hist)))
    else:
        log("靶类型映射当日快照已更新: %s" % path)
    state["ttm_hash"] = h
    state["ttm_day"] = today
    return True


def main():
    log("备份目标: %s" % OUT_ROOT)
    log("A 机地址: %s (每 %ds 检查, 每 %dmin 全量重导)" %
        (SERVER_URL, INTERVAL_SEC, FULL_REFRESH_MIN))
    state = load_state()
    last_full = state.get("last_full", 0)
    pending = set(state.get("pending", []))  # 被占用/失败待补写的表 id
    log("靶类型映射备份源: http://127.0.0.1:%d/api/targetmap" % HELPER_PORT)
    while True:
        try:
            sheets = json.loads(http_get("/api/sheets"))["sheets"]
            changed = False
            # 1) 变化检测（新表 / 记录数变化 / 改名 / 待补写）
            for s in sheets:
                prev = state.get("sheets", {}).get(str(s["id"]))
                if prev is None or prev.get("count") != s.get("count") \
                        or prev.get("name") != s.get("name") \
                        or str(s["id"]) in pending:
                    if try_export(s, state, pending):
                        changed = True
            # 2) 全量重导（捕获单元格编辑类改动），单表失败不影响其余表
            now = time.time()
            if now - last_full > FULL_REFRESH_MIN * 60:
                for s in sheets:
                    try_export(s, state, pending)
                last_full = now
                state["last_full"] = last_full
                log("全量重导完成 (%d 张表%s)" % (
                    len(sheets),
                    "，%d 张被占用待补写" % len(pending) if pending else ""))
                changed = True
            if pending:
                state["pending"] = sorted(pending)
                changed = True  # 每轮都重试待补写表，状态持续落盘
            elif "pending" in state:
                del state["pending"]
                changed = True
            # 3) 上报系统生效靶类型映射表备份（独立于 A 机，失败不影响上面）
            if backup_target_type(state):
                changed = True
            if changed:
                save_state(state)
        except Exception as e:
            log("A 机暂时不可达或出错: %r (将继续重试)" % e)
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
