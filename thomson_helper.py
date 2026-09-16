# -*- coding: utf-8 -*-
"""
汤姆逊谱仪 · 能量上报辅助软件（B 机端）
=======================================
场景：
  B 机上的汤姆逊谱仪生成图片文件。一次打靶 → 一个（组）图片文件落盘，
  图片生成时间 = 该发次的打靶时间。实验人员离线解谱后得到"闪烁光纤能量"，
  再把能量绑定到对应发次，发给 A 机的实验日志系统。

工作方式：
  1. 监视谱仪/数据目录，按时间窗分组：一组文件 = 一次发次，
     组内最早 mtime = 该发次的打靶时间（与 b_watcher 同款分组逻辑）。
  2. 发次先进本页面"待确认"列表（b_watcher confirm 模式也会把检测到的
     发次送到这里），绝不自动写 A 机；实验人员点「确认上报」后才写入
     （auto_report=true 的旧直报模式仍保留，可通过配置切回）。
  3. 本机页面 http://127.0.0.1:8767：确认上报（可先不填能量）→ 打靶行
     写入 A 机；解谱后填入能量点"发送"→ A 机 /api/energy 绑定/覆盖能量列。
  4. 绑定问题兜底：
     - 发送失败 → 状态"发送失败"，可重发；
     - 窗口内找不到发次（如 A 机漏记）→ 状态"无匹配发次"，
       页面显示 A 机最近一条记录时间，可点"补录"强行建一条独立记录。

特性：
  - 纯 Python 标准库，零第三方依赖
  - 发次与能量状态持久化（state_helper.json），重启不丢
  - 已见图片登记（防重启重复上报）、断网自动排队补发
  - 能量可重复发送（覆盖更新），解谱修正后重报即可

运行： python thomson_helper.py   （首次运行自动生成 config_helper.json）
"""

import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import target_client

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config_helper.json")
STATE_PATH = os.path.join(BASE, "state_helper.json")
B_LOCAL_CONFIG_PATH = os.path.join(BASE, "config_b.local.json")  # b_watcher 本机配置（监视目录变更需同步给它）

# RLock：save_state() 在调用方已持锁时也会被调用，必须可重入
_state_lock = threading.RLock()
STATE = {"seen": {}, "pend": [], "shots": [], "queue": [], "no_seq": {},
         "forming_shot": None, "trash": []}
STATE_VER = 0          # 页面 SSE 推送用的状态版本号：shots/queue 一变就 +1

# 重频靶系统实时状态（后台线程每 5s 刷新；靶位/离焦随 SSE 推到页面）
TARGET = dict(target_client.EMPTY)


def target_poll_loop(interval=5.0):
    """后台轮询重频靶系统：当前靶位 / 离焦值等，结果写 state_target.json
    （b_watcher 上报时直接读缓存，零延迟）。靶系统离线不影响打靶主流程。"""
    global TARGET
    while True:
        try:
            d = target_client.poll()
        except Exception as e:
            d = dict(target_client.EMPTY)
            d["error"] = repr(e)
            d["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        changed = (d.get("pos") != TARGET.get("pos") or
                   d.get("defocus") != TARGET.get("defocus") or
                   d.get("ok") != TARGET.get("ok"))
        with _state_lock:
            TARGET = d
        if changed:
            bump_ver()   # 靶位/离焦变化也推给页面
        time.sleep(interval)


def bump_ver():
    global STATE_VER
    STATE_VER += 1
SERVER_URL = ""
MACHINE = ""
WATCH_DIRS = []
ENERGY_FIELD = "fiber_p_energy"
MATCH_WINDOW = 15.0
AUTO_REPORT = True
BW_MACHINE = ""   # b_watcher 的机名（读 config_b.local.json）：确认上报时与
                  # b_watcher 旧直报记录对齐，A 机去重才不会产生重复行
# 展示/绑定状态：pending 待确认 | sent 已绑定 | no_match 无匹配 | error 发送失败


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def save_state():
    with _state_lock:
        save_json(STATE_PATH, STATE)


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def http_post_json(url, payload, timeout=6):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read()
                      .decode("utf-8"))


# ---------------- 监视线程 ----------------

# 目录扫描缓存：{目录: (目录mtime, [(文件路径, mtime), ...], [子目录路径, ...])}
# 逐层校验目录 mtime：没变的层直接用缓存文件列表，只重扫有变化的层。
# 新建/删除文件会更新其所在目录的 mtime，所以任何深度的新文件都能发现，
# 而不必每次 stat 全部历史文件（TPS 有 9000+ 文件，裸扫一轮 ~0.4s）
_DIR_CACHE = {}


def scan_all(dirs):
    """扫描全部监视目录，返回 ([(路径, mtime), ...], [失效目录, ...])"""
    found, missing = [], []
    for d in dirs:
        if not os.path.isdir(d):
            missing.append(d)
            continue
        _scan_dir(d, found)
    return found, missing


def _scan_dir(d, out):
    try:
        mt = os.path.getmtime(d)
    except OSError:
        _DIR_CACHE.pop(d, None)
        return
    c = _DIR_CACHE.get(d)
    if c is not None and c[0] == mt:
        out.extend(c[1])          # 本层无增删改：文件列表直接用缓存
        for sub in c[2]:
            _scan_dir(sub, out)   # 但每层子目录仍要各自校验
        return
    files, subs = [], []
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        p = os.path.join(d, n)
        if os.path.isdir(p):
            subs.append(p)
        else:
            try:
                files.append((p, os.path.getmtime(p)))
            except OSError:
                pass              # 文件可能正被写入
    _DIR_CACHE[d] = (mt, files, subs)
    out.extend(files)
    for sub in subs:
        _scan_dir(sub, out)


def group_into_shots(entries, window):
    """相邻间隔 <= window 的文件归为一次发次。
    返回 (已完成组, 最后一组——需安静超过 window 才算到齐)"""
    if not entries:
        return [], []
    ents = sorted(entries, key=lambda x: x[1])
    groups, cur = [], [ents[0]]
    for p, mt in ents[1:]:
        if mt - cur[-1][1] <= window:
            cur.append((p, mt))
        else:
            groups.append(cur)
            cur = [(p, mt)]
    if time.time() - cur[-1][1] >= window:
        return groups + [cur], []
    return groups, cur


# 发次号解析：文件名里的编号（shot-3.png / SHOR_12.tif / shot7.dat…）
NO_PAT = re.compile(r"(?:shot|shor)[-_ ]?(\d+)", re.IGNORECASE)


def parse_shot_no(names):
    """从一组文件名解析发次号；多个取最小值；解析不出返回 None（由调用方补号）"""
    nums = []
    for n in names:
        m = NO_PAT.search(n)
        if m:
            nums.append(int(m.group(1)))
    return min(nums) if nums else None


def make_shot(group):
    names = [os.path.basename(p) for p, _mt in sorted(group, key=lambda x: x[1])]
    st = datetime.fromtimestamp(min(mt for _p, mt in group)).strftime(
        "%Y-%m-%d %H:%M:%S")
    return {"shot_time": st, "files": names, "file_count": len(group),
            "no": parse_shot_no(names),
            "target": "", "defocus": "",
            "energy": "", "status": "pending", "info": "", "row_id": None,
            "reported": False}


def attach_target(shot):
    """把当前靶位/离焦值（后台轮询缓存，最多 5s 旧）挂到发次上"""
    with _state_lock:
        shot["target"] = TARGET.get("pos", "") or ""
        shot["defocus"] = TARGET.get("defocus", "") or ""
    return shot


def _time_diff(a, b):
    """两个 'YYYY-MM-DD HH:MM:SS' 的秒差（绝对值）；解析失败返回极大值"""
    try:
        return abs((datetime.strptime(a, "%Y-%m-%d %H:%M:%S")
                    - datetime.strptime(b, "%Y-%m-%d %H:%M:%S")).total_seconds())
    except Exception:
        return 1e9


def api_ignore_shot(shot_time):
    """忽略一条发次：从待确认列表移入回收站（不写 A 机，可在回收站恢复）。
    用于历史残留 / 误检的清理。已上报过的发次同样允许移入回收站。"""
    st = str(shot_time or "").strip()
    with _state_lock:
        removed = [s for s in STATE["shots"] if s["shot_time"] == st]
        if removed:
            STATE["shots"] = [s for s in STATE["shots"]
                              if s["shot_time"] != st]
            trash = STATE.setdefault("trash", [])
            for s in removed:           # 新的在前
                trash.insert(0, dict(s))
            del trash[500:]             # 回收站最多保留 500 条
            STATE["forming_shot"] = None
            bump_ver()
            save_state()
    if removed:
        log("忽略发次(入回收站): %s" % st)
    return {"ok": True, "removed": len(removed)}


def api_shots_clear(mode):
    """批量清理待确认列表（移除的进回收站，可恢复）：
    sent = 只移除已绑定/已失败确认的发次（默认，保守）；
    all  = 清空整个列表（不含正在形成的发次）。"""
    with _state_lock:
        if mode == "all":
            removed = [s for s in STATE["shots"]
                       if s["status"] != "forming"]
        else:
            removed = [s for s in STATE["shots"]
                       if s["status"] in ("sent", "error")]
        if removed:
            keep = set(id(s) for s in removed)
            STATE["shots"] = [s for s in STATE["shots"]
                              if id(s) not in keep]
            trash = STATE.setdefault("trash", [])
            for s in removed:           # 新的在前
                trash.insert(0, dict(s))
            del trash[500:]
            bump_ver()
            save_state()
    log("清理发次列表[%s]: 移除 %d 条（已入回收站），剩 %d 条" %
        (mode, len(removed), len(STATE["shots"])))
    return {"ok": True, "removed": len(removed),
            "left": len(STATE["shots"])}


def api_trash_act(p):
    """回收站操作：act = restore 恢复回列表 / delete 彻底删除一条 / clear 清空"""
    act = str(p.get("act", "")).strip()
    st = str(p.get("shot_time", "")).strip()
    with _state_lock:
        trash = STATE.setdefault("trash", [])
        if act == "restore":
            hit = [s for s in trash if s["shot_time"] == st]
            if not hit:
                return {"ok": False, "error": "回收站里没有这条记录"}
            if not any(x["shot_time"] == st for x in STATE["shots"]):
                STATE["shots"].insert(0, dict(hit[0]))
                bump_ver()
            trash[:] = [x for x in trash if x["shot_time"] != st]
            save_state()
            log("回收站恢复: %s" % st)
            return {"ok": True}
        if act == "delete":
            n0 = len(trash)
            trash[:] = [x for x in trash if x["shot_time"] != st]
            if len(trash) != n0:
                save_state()
            return {"ok": True, "removed": n0 - len(trash)}
        if act == "clear":
            n = len(trash)
            trash[:] = []
            save_state()
            return {"ok": True, "removed": n}
    return {"ok": False, "error": "未知操作"}


# ---------------- Excel 导出（纯标准库手写 xlsx，无 openpyxl 依赖） ----------------

_ST_LABEL = {"pending": "待确认", "sent": "已绑定", "no_match": "无匹配发次",
             "error": "发送失败", "forming": "检测中"}


def _xml_esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def build_xlsx(shots):
    """把发次列表写成最小合法 xlsx（zip + inlineStr），9 列。
    列：No. / 发次时间 / 图片数 / 图片文件 / 靶位 / 离焦 / 能量 / 状态 / 备注"""
    import io
    import zipfile
    cols = ["No.", "发次时间", "图片数", "图片文件", "靶位", "离焦",
            "能量", "状态", "备注"]

    def row_xml(rn, values):
        cells = []
        for i, v in enumerate(values):
            col = chr(ord("A") + i)
            cells.append('<c r="%s%d" t="inlineStr"><is><t xml:space="preserve">'
                         "%s</t></is></c>" % (col, rn, _xml_esc(v)))
        return '<row r="%d">%s</row>' % (rn, "".join(cells))

    body = [row_xml(1, cols)]
    for n, s in enumerate(shots, 2):
        body.append(row_xml(n, [
            s.get("no") or "", s.get("shot_time") or "",
            s.get("file_count") or 0, " ".join(s.get("files") or []),
            s.get("target") or "", s.get("defocus") or "",
            s.get("energy") or "",
            _ST_LABEL.get(s.get("status"), s.get("status") or ""),
            s.get("info") or ""]))
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/'
             'spreadsheetml/2006/main"><sheetData>%s</sheetData></worksheet>'
             % "".join(body))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/'
                   '2006/content-types">'
                   '<Default Extension="rels" ContentType="application/'
                   'vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType='
                   '"application/vnd.openxmlformats-officedocument.'
                   'spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/worksheets/sheet1.xml" '
                   'ContentType="application/vnd.openxmlformats-officedocument.'
                   'spreadsheetml.worksheet+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats'
                   '.org/officeDocument/2006/relationships/officeDocument" '
                   'Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/'
                   'spreadsheetml/2006/main" xmlns:r="http://schemas.'
                   'openxmlformats.org/officeDocument/2006/relationships">'
                   '<sheets><sheet name="上报记录" sheetId="1" r:id="rId1"/>'
                   '</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats'
                   '.org/officeDocument/2006/relationships/worksheet" '
                   'Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def ingest_detect(payload):
    """/api/detect：接收 b_watcher（confirm 模式）送来的发次，进入"待确认"列表。
    这里只是登记，绝不直接写 A 机——必须等实验人员在页面点「确认上报」。
    按 shot_time（±2s）去重合并：同一发次 b_watcher / 本页监视都可能发现。"""
    st = str(payload.get("shot_time", "")).strip()
    if not st:
        return {"ok": False, "error": "shot_time 为空"}
    files = payload.get("files") or []
    names = [(f.get("name", "") if isinstance(f, dict) else str(f))
             for f in files]
    flds = payload.get("fields") or {}
    with _state_lock:
        shot = next((s for s in STATE["shots"]
                     if _time_diff(s["shot_time"], st) <= 2.0), None)
        merged = shot is not None
        if shot is None:
            shot = {"shot_time": st, "files": names, "file_count": len(files),
                    "no": None, "target": "", "defocus": "", "energy": "",
                    "status": "pending", "info": "", "row_id": None,
                    "reported": False}
            STATE["shots"].insert(0, shot)
        if len(names) > shot.get("file_count", 0):
            shot["files"] = names
            shot["file_count"] = len(files)
        if flds.get("no") is not None:
            shot["no"] = flds["no"]
        if flds.get("target_pos") and not shot.get("target"):
            shot["target"] = flds["target_pos"]
        tdf = flds.get("target_defocus")
        if tdf not in ("", None) and shot.get("defocus", "") == "":
            shot["defocus"] = str(tdf)
        bump_ver()
        save_state()
    log("收到待确认发次: %s (%d 个文件%s)"
        % (st, len(files), "，已合并同名发次" if merged else ""))
    return {"ok": True, "merged": merged}


def report_shot(shot, energy=""):
    """把发次上报给 A 机（人工点「确认上报」后才会走到这里）。
    machine 用 b_watcher 的机名：A 机按 machine+时间+首文件去重，
    与 b_watcher 旧直报记录对齐后不会产生重复行。能量一并写入该行。"""
    files = [{"name": n, "mtime": 0} for n in shot["files"]]
    fields = {"no": shot["no"]} if shot.get("no") is not None else {}
    if shot.get("target"):
        fields["target_pos"] = shot["target"]       # A 机表已有"靶位"列
    if shot.get("defocus") != "":
        fields["target_defocus"] = str(shot["defocus"])  # A 机表"靶离焦"列
    if energy:
        fields[ENERGY_FIELD] = energy
    payload = {"machine": BW_MACHINE or MACHINE, "shot_time": shot["shot_time"],
               "files": files, "fields": fields, "reported_at":
               datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    # 上报目标表：""=默认"实时打靶"；"@date"=按打靶日期自动分表；其他=固定表名
    sn = str((CFG or {}).get("sheet_name", "") or "").strip()
    if sn == "@date":
        sn = str(shot["shot_time"])[:10]
    if sn:
        payload["sheet_name"] = sn
    j = http_post_json(SERVER_URL.rstrip("/") + "/api/shot", payload)
    return bool(j.get("ok"))


def retry_queue():
    """补发断网期间没送出去的发次上报"""
    if not STATE["queue"]:
        return
    still = []
    for pl in STATE["queue"]:
        try:
            http_post_json(SERVER_URL.rstrip("/") + "/api/shot", pl)
            log("补发成功: %s" % pl.get("shot_time"))
        except Exception:
            still.append(pl)
    if len(still) != len(STATE["queue"]):
        with _state_lock:
            STATE["queue"] = still
            bump_ver()
            save_state()


def _norm_dir(d):
    """目录路径规范化：去引号/空白，补全尾斜杠（'D:' -> 'D:\\'）"""
    d = str(d).strip().strip('"')
    if len(d) == 2 and d[1] == ":":
        d += "\\"
    return os.path.normpath(d)


# ---------------- 原生"选择文件夹"对话框（网页浏览按钮调用） ----------------
# 浏览器安全限制拿不到真实路径，但 helper 就跑在本机：用子进程弹
# 系统 tkinter 文件夹对话框，选完把真实路径传回页面。
_PICK_STATE = {"state": "idle", "path": "", "error": ""}  # idle|pending|done|canceled
_PICK_LOCK = threading.Lock()
_PICK_PY = None       # 缓存带 tkinter 的解释器（managed 3.13 没有，系统 3.10 有）


def _find_tk_python():
    """找一个能 import tkinter 的 Python 解释器"""
    global _PICK_PY
    if _PICK_PY:
        return _PICK_PY
    import subprocess
    cands = (r"D:\Program Files\Python310\python.exe",
             r"D:\anaconda\python.exe",
             r"D:\Python\python.exe",
             r"C:\Windows\py.exe",
             sys.executable)
    for exe in cands:
        try:
            r = subprocess.run([exe, "-c", "import tkinter; print('ok')"],
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                _PICK_PY = exe
                return exe
        except Exception:
            continue
    return None


def _pick_dir_worker():
    """弹本机文件夹选择对话框（子进程，避免阻塞/依赖 tkinter）"""
    import subprocess
    try:
        exe = _find_tk_python()
        if not exe:
            with _PICK_LOCK:
                _PICK_STATE.update(state="canceled", path="",
                                   error="本机没有可用的 tkinter 环境，请手动输入路径")
            return
        code = ("import tkinter as tk; from tkinter import filedialog;"
                "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True);"
                "r.focus_force();"
                "p = filedialog.askdirectory(title='选择要监视的文件夹', mustexist=True);"
                "r.destroy(); print(p)")
        r = subprocess.run([exe, "-c", code], capture_output=True, text=True,
                           timeout=600,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        p = (r.stdout or "").strip()
        with _PICK_LOCK:
            if p:
                _PICK_STATE.update(state="done", path=os.path.normpath(p), error="")
            else:
                _PICK_STATE.update(state="canceled", path="", error="已取消")
    except Exception as e:
        with _PICK_LOCK:
            _PICK_STATE.update(state="canceled", path="", error=repr(e))


def pick_dir_start():
    """开始一次选择（同一时刻只允许一个对话框）"""
    with _PICK_LOCK:
        if _PICK_STATE["state"] == "pending":
            return {"ok": False, "error": "已有选择窗口打开，请先完成或取消"}
        _PICK_STATE.update(state="pending", path="", error="")
    threading.Thread(target=_pick_dir_worker, daemon=True).start()
    return {"ok": True, "state": "pending"}


def pick_dir_status():
    with _PICK_LOCK:
        return {"ok": True, **_PICK_STATE}


def set_watch_dirs(new_dirs):
    """8767 页面"监视目录管理"入口：增删目录即时生效。
    新目录静默登记已有文件（历史数据不会被当成新发次上报）；
    同步写回 config_helper.json，并同步 config_b.local.json
    （b_watcher 检测到配置变更会自动热重载）。"""
    dirs, uniq = [], set()
    for d in new_dirs:
        nd = _norm_dir(d)
        if nd and nd not in uniq:
            dirs.append(nd)
            uniq.add(nd)
    if not dirs:
        return {"ok": False, "error": "至少需要保留一个监视目录"}
    with _state_lock:
        old = list(WATCH_DIRS)
        added = [d for d in dirs if d not in WATCH_DIRS]
        removed = [d for d in WATCH_DIRS if d not in dirs]
        silent = 0
        for d in added:                   # 新目录：先静默登记，避免历史文件误报
            if os.path.isdir(d):
                for root, _ds, fs in os.walk(d):
                    for fn in fs:
                        p = os.path.join(root, fn)
                        try:
                            STATE["seen"].setdefault(p, os.path.getmtime(p))
                        except OSError:
                            continue
                        silent += 1
        for d in removed:                 # 删除目录：顺手清理扫描缓存
            pre = d.rstrip("\\/") + os.sep
            for k in [k for k in _DIR_CACHE if k == d or k.startswith(pre)]:
                _DIR_CACHE.pop(k, None)
        WATCH_DIRS[:] = dirs              # 原地替换，监视循环无需重启
        if silent:
            save_state()
        CFG["watch_dirs"] = dirs
        save_json(CONFIG_PATH, CFG)
        bl = load_json(B_LOCAL_CONFIG_PATH, None)
        if bl and "watch_dirs" in bl:     # 让 b_watcher（上报 A 机）保持同目录
            # b_watcher 可能还监视着 8767 页面之外的目录（如 DAQ 目录）：
            # 只同步页面管理的目录，b_watcher 独有的目录原样保留不被误删
            old_set = {os.path.normpath(x) for x in old}
            keep = [os.path.normpath(x) for x in bl["watch_dirs"]
                    if x and os.path.normpath(x) not in old_set
                    and os.path.normpath(x) not in uniq]
            bl["watch_dirs"] = dirs + keep
            save_json(B_LOCAL_CONFIG_PATH, bl)
    bump_ver()
    if added:
        log("新增监视目录: %s（静默登记 %d 个已有文件）" % (added, silent))
    if removed:
        log("移除监视目录: %s" % removed)
    return {"ok": True, "added": added, "removed": removed,
            "dirs": [{"path": d, "exists": os.path.isdir(d)} for d in dirs]}


def sheet_label(sn):
    """绑定值 -> 页面展示文案"""
    if sn == "@date":
        return "按日期自动分表"
    return sn or "实时打靶(默认)"


def get_sheet_binding():
    """8767 页面"上报表格绑定"数据：当前绑定值 + A 机已有表格列表"""
    cur = str((CFG or {}).get("sheet_name", "") or "").strip()
    sheets = []
    try:
        req = urllib.request.Request(
            SERVER_URL.rstrip("/") + "/api/sheets",
            headers={"Accept": "application/json"})
        j = json.loads(urllib.request.urlopen(req, timeout=6).read()
                       .decode("utf-8"))
        sheets = [{"id": s.get("id"), "name": s.get("name"),
                   "count": s.get("count"),
                   "view_url": SERVER_URL.rstrip("/") + "/#sheet=" +
                               str(s.get("id"))} for s in j.get("sheets", [])]
    except Exception:
        pass  # A 机暂不可达：下拉框只显示特殊选项，不影响改绑定
    return {"ok": True, "sheet_name": cur, "label": sheet_label(cur),
            "sheets": sheets}


_SRV_ROWS = {"t": 0.0, "name": "__init__", "rows": [], "label": "", "ok": True}


def get_server_rows(sheet_name, date=None):
    """拉取绑定表在 A 机的已上报记录（只读视图，5s 缓存）。
    绑定为 @date 时按 date 参数（默认今天）解析到对应日期表，
    页面日期切换器选 0915 就看 0915 的表。"""
    sn = str(sheet_name or "").strip()
    now = time.time()
    key = sn + "|" + str(date or "")
    with _state_lock:
        if _SRV_ROWS["name"] == key and now - _SRV_ROWS["t"] < 5:
            return dict(_SRV_ROWS)
    label = sheet_label(sn)
    rows, ok, view_url = [], True, ""
    try:
        if sn == "@date":
            name = str(date or "").strip() or datetime.now().strftime("%Y-%m-%d")
            label = name + "（按日期自动）"
        elif sn:
            name = sn
        else:
            name = "实时打靶"
        base = SERVER_URL.rstrip("/")
        req = urllib.request.Request(base + "/api/sheets",
                                     headers={"Accept": "application/json"})
        j = json.loads(urllib.request.urlopen(req, timeout=6).read()
                       .decode("utf-8"))
        sid = next((s.get("id") for s in j.get("sheets", [])
                    if s.get("name") == name), None)
        if sid is not None:
            view_url = base + "/#sheet=" + str(sid)
            req2 = urllib.request.Request(
                base + "/api/rows?sheet_id=%s&page=1&page_size=200"
                       "&q=&sort=shot_time&dir=desc" % sid,
                headers={"Accept": "application/json"})
            j2 = json.loads(urllib.request.urlopen(req2, timeout=6).read()
                            .decode("utf-8"))
            for r in j2.get("rows", []):
                f = r.get("fields") or {}
                if isinstance(f, str):
                    try:
                        f = json.loads(f)
                    except Exception:
                        f = {}
                rows.append({
                    "shot_time": r.get("shot_time", ""),
                    "no": f.get("no", ""), "target": f.get("target_pos", ""),
                    "defocus": f.get("target_defocus", ""),
                    "energy": f.get(ENERGY_FIELD, ""),
                    "file_count": r.get("file_count", 0),
                    "first_file": r.get("first_file", "")})
    except Exception:
        ok = False
    with _state_lock:
        _SRV_ROWS.update(t=now, name=key, rows=rows, label=label, ok=ok,
                         view_url=view_url)
    return {"ok": ok, "label": label, "rows": rows, "view_url": view_url}


def set_sheet_binding(name):
    """8767 页面切换上报目标表：写 config_helper.json 并同步 config_b.local.json
    （b_watcher 检测到配置 mtime 变化会自动热重载，无需重启）。"""
    sn = str(name or "").strip()
    if sn not in ("", "@date") and re.search(r'[\\/:*?"<>|]', sn):
        return {"ok": False, "error": "表名含非法字符 \\ / : * ? \" < > |"}
    with _state_lock:
        CFG["sheet_name"] = sn
        save_json(CONFIG_PATH, CFG)
        bl = load_json(B_LOCAL_CONFIG_PATH, None)
        if bl:
            bl["sheet_name"] = sn
            save_json(B_LOCAL_CONFIG_PATH, bl)
    log("上报目标表切换为: %s" % sheet_label(sn))
    return {"ok": True, "sheet_name": sn, "label": sheet_label(sn)}


def monitor_loop(interval, window):
    """监视循环：每轮取 WATCH_DIRS 快照，页面改目录后下一轮立即生效"""
    seen_first = not os.path.exists(STATE_PATH)
    if seen_first:
        with _state_lock:
            if not STATE["seen"]:
                entries, _missing = scan_all(WATCH_DIRS)
                for p, mt in entries:
                    STATE["seen"][p] = mt
                save_state()
        log("首次运行：登记已有图片 %d 个（不上报）" % len(STATE["seen"]))
    log("监视目录: %s" % WATCH_DIRS)
    log("日志系统: %s   本机页面: http://127.0.0.1:%d"
        % (SERVER_URL, CFG.get("helper_port", 8767)))
    missing_seen = {}      # {目录: 上次告警时间}，60 秒节流
    while True:
        try:
            retry_queue()
            dirs = list(WATCH_DIRS)       # 快照：页面改目录后下一轮即生效
            _t0 = time.time()
            entries, missing = scan_all(dirs)
            if time.time() - _t0 > 0.5:
                log("警告: 扫描耗时 %.2fs（正常应 <0.1s）" % (time.time() - _t0))
            now = time.time()
            for d in missing:
                if now - missing_seen.get(d, 0) >= 60:
                    log("警告: 监视目录不存在 %s（新数据不会被发现！）" % d)
                    missing_seen[d] = now
            for d in list(missing_seen):
                if d not in missing:
                    del missing_seen[d]
                    log("目录已恢复: %s" % d)
            new_entries = [(p, mt) for p, mt in entries
                           if p not in STATE["seen"]]
            all_new = [tuple(x) for x in STATE["pend"]] + new_entries
            # 即时行：有未到齐的文件（含刚落盘还没过安静期的）立刻上表显示
            forming = None
            if all_new:
                done, hold = group_into_shots(all_new, window)
                if hold:
                    forming = attach_target(make_shot(hold))
                    forming["status"] = "forming"
                    forming["info"] = "检测中…（文件可能还没到齐）"
                with _state_lock:
                    for p, mt in all_new:
                        STATE["seen"].setdefault(p, mt)
                    STATE["pend"] = [list(x) for x in hold]
                save_state()
                for g in done:
                    shot = attach_target(make_shot(g))
                    # 发次号：文件名能解析就用解析值；否则按当天顺序自动补号
                    day = shot["shot_time"][:10]
                    with _state_lock:
                        seq = STATE.setdefault("no_seq", {})
                        if shot.get("no") is None:
                            shot["no"] = seq.get(day, 0) + 1
                        seq[day] = max(seq.get(day, 0), shot["no"])
                        # 去重合并：b_watcher(confirm 模式)可能已把同一发次
                        # 送进待确认列表，±2s 内视为同一次打靶，只保留一条
                        dup = next((s for s in STATE["shots"]
                                    if _time_diff(s["shot_time"],
                                                  shot["shot_time"]) <= 2.0),
                                   None)
                        if dup is not None:
                            if shot.get("file_count", 0) > dup.get("file_count", 0):
                                dup["files"] = shot["files"]
                                dup["file_count"] = shot["file_count"]
                            if shot.get("target") and not dup.get("target"):
                                dup["target"] = shot["target"]
                            if shot.get("defocus", "") != "" and \
                                    dup.get("defocus", "") == "":
                                dup["defocus"] = shot["defocus"]
                            if shot.get("no") is not None and dup.get("no") is None:
                                dup["no"] = shot["no"]
                        else:
                            STATE["shots"].insert(0, shot)
                        STATE["forming_shot"] = None
                        bump_ver()
                        save_state()
                    log("检测到发次: %s (%d 个文件)" %
                        (shot["shot_time"], shot["file_count"]))
                    if AUTO_REPORT:
                        try:
                            report_shot(shot)
                            log("已上报日志系统: %s" % shot["shot_time"])
                        except Exception as e:
                            with _state_lock:
                                STATE["queue"].append({
                                    "machine": MACHINE,
                                    "shot_time": shot["shot_time"],
                                    "files": [{"name": n, "mtime": 0}
                                              for n in shot["files"]]})
                                bump_ver()
                                save_state()
                            log("发次上报失败(%r)，已入补发队列" % e)
            # forming 行有变化（新出现/文件数增加/转正）就推送
            with _state_lock:
                prev = STATE.get("forming_shot")
                changed = (json.dumps(forming, sort_keys=True, default=str) !=
                           json.dumps(prev, sort_keys=True, default=str))
                if changed:
                    STATE["forming_shot"] = forming
            if changed:
                bump_ver()
                if forming:
                    log("检测到新文件: %s（即时显示，安静 %gs 后转正）"
                        % (forming["files"][0], window))
            time.sleep(interval)
        except Exception as e:
            log("监视循环异常(继续运行): %r" % e)
            time.sleep(interval)


# ---------------- 本机页面（视觉与 A 机主系统同一套风格） ----------------

HELP_PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>打靶日志系统 · 谱仪上报</title>
<style>
  *{box-sizing:border-box}
  body{font-family:"Microsoft YaHei",sans-serif;margin:0;background:#f0f2f5;color:#222;
       display:flex;flex-direction:column;height:100vh;overflow:hidden}
  /* ---------- 顶栏（与主系统侧栏同色系） ---------- */
  #top{background:#243342;color:#dfe6ee;display:flex;align-items:center;
       padding:0 20px;flex-shrink:0;height:52px}
  #top .logo{font-size:16px;font-weight:bold}
  #top .logo small{font-weight:normal;color:#8fa4bb;font-size:11px;margin-left:10px}
  #top .right{margin-left:auto;font-size:12px;color:#8fa4bb;display:flex;
              align-items:center;gap:8px}
  .dot{width:9px;height:9px;border-radius:50%;background:#666;display:inline-block}
  .dot.ok{background:#2ecc71}.dot.bad{background:#e74c3c}
  /* ---------- 信息条（同主系统 toolbar） ---------- */
  #bar{background:#fff;padding:9px 20px;border-bottom:1px solid #e2e4e8;font-size:13px;
       color:#666;display:flex;gap:18px;flex-wrap:wrap;flex-shrink:0}
  #bar b{color:#2c3e50}
  /* ---------- 表格（同主系统数据表） ---------- */
  .wrap{flex:1;overflow:auto;background:#fff}
  table{border-collapse:separate;border-spacing:0;width:max-content;min-width:100%;
        font-size:13px}
  th{background:#2c3e50;color:#fff;padding:8px 10px;text-align:left;font-weight:normal;
     white-space:nowrap;position:sticky;top:0;z-index:3}
  td{border-bottom:1px solid #eceef1;border-right:1px solid #f2f3f5;padding:6px 10px;
     white-space:nowrap;vertical-align:middle}
  tr:hover td{background:#f2f7ff}
  td.t{font-family:Consolas,monospace}
  .files{color:#888;font-size:12px;max-width:360px;overflow:hidden;
         text-overflow:ellipsis;max-width:360px}
  input.e{width:110px;padding:6px 8px;border:1px solid #d5d9de;border-radius:5px;
          font-size:13px;font-family:inherit}
  input.e:focus{outline:none;border-color:#9fc3e8;background:#fff8dc}
  input.n{width:54px;padding:6px 6px;border:1px solid #d5d9de;border-radius:5px;
          font-size:13px;text-align:center;font-family:Consolas,monospace}
  input.n:focus{outline:none;border-color:#9fc3e8;background:#fff8dc}
  .tbtn{background:#2c3e50;color:#fff;border:none;border-radius:5px;padding:6px 14px;
        font-size:13px;cursor:pointer;font-family:inherit}
  .tbtn:hover{background:#3d5875}
  .tbtn.orange{background:#d35400}
  .tbtn.orange:hover{background:#e67e22}
  .st{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px}
  .st.pending{background:#f6e8c8;color:#8a6d3b}
  .st.sent{background:#d4edda;color:#256029}
  .st.forming{background:#e2e3fe;color:#3d3d8f}
  .st.no_match{background:#f8d7da;color:#721c24}
  .st.error{background:#f8d7da;color:#721c24}
  .empty{padding:50px;text-align:center;color:#999}
  /* ---------- 底部状态（同主系统 pager） ---------- */
  #foot{background:#fff;border-top:1px solid #e2e4e8;padding:8px 20px;font-size:13px;
        color:#888;flex-shrink:0}
  .toast{position:fixed;top:18px;left:50%;transform:translateX(-50%);background:#2c3e50;
         color:#fff;padding:8px 22px;border-radius:20px;font-size:13px;display:none;
         box-shadow:0 2px 8px rgba(0,0,0,.25);z-index:99}
  @media print{
    body{display:block;height:auto;overflow:visible;background:#fff}
    #top,#bar,#foot,.toast{display:none !important}
    .wrap{overflow:visible}
    th{background:#eee !important;color:#000 !important;position:static}
  }
</style>
</head>
<body>
<div id="top">
  <span class="logo">打靶日志系统<small>谱仪上报终端 · BLAC 实验数据 · 内网</small></span>
  <span class="right">
    <span id="machine"></span>
    <span class="dot" id="dot"></span><span id="srv"></span>
  </span>
</div>
<div id="bar">
  <span>监视目录：<b id="dirs" style="cursor:pointer;border-bottom:1px dotted #888"
        onclick="openDirs()" title="点击管理监视目录">-</b></span>
  <span>上报表格：<b id="sheetName" style="cursor:pointer;border-bottom:1px dotted #888"
        onclick="openSheet()" title="点击选择打靶上报写入的表格">-</b></span>
  <span>待填能量 <b id="npending">0</b> 发</span>
  <span>绑定窗口 ±<b id="win">-</b>s</span>
  <span>能量写入列：<b id="efield">-</b></span>
  <span>当前靶位：<b id="tgt">…</b><span id="tgtF" style="color:#888;font-size:12px"></span></span>
  <span style="margin-left:auto">
    <button onclick="clearShots('sent')" style="padding:3px 10px;cursor:pointer;
      border:1px solid #ccd;border-radius:6px;background:#fff">清理已绑定</button>
    <button onclick="clearShots('all')" style="padding:3px 10px;cursor:pointer;
      border:1px solid #ecc;border-radius:6px;background:#fff;color:#c33">清空列表</button>
    <button onclick="openTrash()" style="padding:3px 10px;cursor:pointer;
      border:1px solid #ccd;border-radius:6px;background:#fff">回收站</button>
    <button onclick="window.open('/export.xlsx')" style="padding:3px 10px;cursor:pointer;
      border:1px solid #9c8;border-radius:6px;background:#f4fbf4;
      color:#2a7">导出Excel</button>
  </span>
</div>
<div class="wrap">
  <table>
    <thead><tr>
      <th>No.</th><th>发次时间</th><th>图片数</th><th>图片文件</th>
      <th>靶位/离焦</th>
      <th>闪烁光纤能量</th><th>状态</th><th>操作</th>
    </tr></thead>
    <tbody id="tb"></tbody>
  </table>
</div>
<div class="wrap" style="margin-top:14px">
  <div id="srvHead" style="font-size:13px;font-weight:bold;margin-bottom:6px;
       display:flex;align-items:center;gap:8px;flex-wrap:wrap">
    <span id="srvTitle">已上报记录（来自 A 机，只读）</span>
    <span style="font-weight:normal;font-size:12px;color:#888">查看日期：
      <input type="date" id="srvDate" onchange="loadSrv()"
             style="padding:2px 6px;border:1px solid #d5d8dc;border-radius:6px">
    </span>
    <a id="srvView" target="_blank" style="font-weight:normal;font-size:12px;
       color:#3498db">在 A 机页面打开 →</a>
  </div>
  <table>
    <thead><tr>
      <th>发次时间</th><th>No.</th><th>靶位</th><th>离焦</th><th>能量</th><th>图片数</th><th>首个文件</th>
    </tr></thead>
    <tbody id="srvtb"></tbody>
  </table>
</div>
<div id="foot">打靶需在页面点「确认上报」后才写入日志系统 ｜ 旧/误发次点"忽略"进回收站（右上可恢复） ｜ 右上「导出Excel」备份当前列表 ｜ No. 自动取自文件名，可手动修正</div>
<div id="toast"></div>
<div id="dirMask" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);
     z-index:50;align-items:center;justify-content:center"
     onclick="if(event.target===this)closeDirs()">
  <div style="background:#fff;border-radius:12px;width:600px;max-width:92vw;
       padding:20px 24px;max-height:80vh;overflow:auto">
    <div style="display:flex;align-items:center;margin-bottom:6px">
      <b style="font-size:15px">监视目录管理</b>
      <span style="margin-left:auto;cursor:pointer;color:#999;font-size:20px;
            line-height:1" onclick="closeDirs()">✕</span>
    </div>
    <div style="font-size:12px;color:#888;margin-bottom:12px">
      点"浏览…"会弹出本机文件夹选择窗口（真实路径自动填入）；也可直接手动输入。
      新增目录会静默登记其中已有文件（历史数据不会误报为新发次）；删除即时生效，
      并自动同步给打靶监测（b_watcher），两侧目录始终一致。
    </div>
    <div id="dirList"></div>
    <div style="display:flex;gap:8px;margin-top:12px">
      <input id="newDir" style="flex:1;padding:7px 10px;border:1px solid #d5d9de;
             border-radius:6px;font-size:13px;font-family:Consolas,monospace"
             placeholder="输入目录，如 D:\data117\TPS 或 D:\实验数据\XXX"
             onkeydown="if(event.key==='Enter')addDir()">
      <button class="tbtn" id="btnBrowse" onclick="browseDir()">浏览…</button>
      <button class="tbtn" onclick="addDir()">添加</button>
    </div>
  </div>
</div>
<div id="sheetMask" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);
     z-index:50;align-items:center;justify-content:center"
     onclick="if(event.target===this)closeSheet()">
  <div style="background:#fff;border-radius:12px;width:520px;max-width:92vw;
       padding:20px 24px;max-height:80vh;overflow:auto">
    <div style="display:flex;align-items:center;margin-bottom:6px">
      <b style="font-size:15px">上报表格绑定</b>
      <span style="margin-left:auto;cursor:pointer;color:#999;font-size:20px;
            line-height:1" onclick="closeSheet()">✕</span>
    </div>
    <div style="font-size:12px;color:#888;margin-bottom:12px">
      选择打靶记录写入的表格：切换后立即生效（含打靶上报与能量页自动上报），
      b_watcher 同步跟随，无需重启。表格不存在时 A 机会自动创建。
    </div>
    <div id="sheetList"></div>
  </div>
</div>
<div id="trashMask" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);
     z-index:50;align-items:center;justify-content:center"
     onclick="if(event.target===this)closeTrash()">
  <div style="background:#fff;border-radius:12px;width:640px;max-width:92vw;
       padding:20px 24px;max-height:80vh;overflow:auto">
    <div style="display:flex;align-items:center;margin-bottom:6px">
      <b style="font-size:15px">回收站（忽略/清理的发次都在这里，可恢复）</b>
      <span style="margin-left:auto;cursor:pointer;color:#999;font-size:20px;
            line-height:1" onclick="closeTrash()">✕</span>
    </div>
    <div style="font-size:12px;color:#888;margin-bottom:12px">
      「忽略」和「清理/清空列表」只是把发次移到这里，不写日志系统也不丢数据；
      可单条「恢复」回待确认列表，或「彻底删除」永久移除（无法恢复）。
    </div>
    <div style="margin-bottom:10px">
      <button onclick="clearTrash()" style="padding:3px 10px;cursor:pointer;
        border:1px solid #ecc;border-radius:6px;background:#fff;color:#c33">清空回收站</button>
    </div>
    <div id="trashList"></div>
  </div>
</div>
<script>
var SHOTS = [], LASTJSON = "", T = null;
function toast(s){
  var t = document.getElementById("toast");
  t.textContent = s; t.style.display = "block";
  clearTimeout(T); T = setTimeout(function(){ t.style.display = "none"; }, 2600);
}
function stLabel(s){
  return {pending:"待确认", sent:"已绑定", no_match:"无匹配发次",
          error:"发送失败", forming:"检测中…"}[s] || s;
}
function render(){
  var tb = document.getElementById("tb");
  if (!SHOTS.length){
    tb.innerHTML = "<tr><td colspan=8 class='empty'>暂未检测到发次——等待谱仪图片落盘…</td></tr>";
  } else {
    var h = "";
    SHOTS.forEach(function(s, i){
      h += "<tr data-i='" + i + "'>";
      h += "<td><input class='n' data-i='" + i + "' value='" +
           (s.no != null ? s.no : "") + "'></td>";
      h += "<td class='t'>" + s.shot_time + "</td>";
      h += "<td>" + s.file_count + "</td>";
      h += "<td class='files' title='" + s.files.join("  ") + "'>" +
           (s.files[0] || "-") + (s.file_count > 1 ? " 等" + s.file_count + "个" : "") + "</td>";
      var tgt = s.target || "-";
      if (s.defocus !== "" && s.defocus != null) tgt += " <span style='color:#888'>离焦 " + s.defocus + "</span>";
      h += "<td style='white-space:nowrap'>" + tgt + "</td>";
      h += "<td><input class='e' data-i='" + i + "' value='" +
           (s.energy || "").replace(/'/g,"&#39;") +
           "' placeholder='如 2.35' onkeydown='if(event.key===\"Enter\")send(" + i + ",false)'></td>";
      var cls = s.status || "pending";
      h += "<td><span class='st " + cls + "'>" + stLabel(cls) + "</span>" +
           (s.info ? "<div style='color:#999;font-size:11px;margin-top:2px'>" + s.info + "</div>" : "") + "</td>";
      h += "<td>";
      if (s.status === "forming"){
        h += "<span style='color:#999;font-size:12px'>等待文件到齐…</span>";
      } else if (s.status === "no_match"){
        h += "<button class='tbtn orange' onclick='send(" + i + ",true)'>补录</button> ";
      } else if (!s.reported){
        h += "<button class='tbtn green' onclick='send(" + i + ",false)'>确认上报</button> ";
      } else {
        h += "<button class='tbtn' onclick='send(" + i + ",false)'>" +
             (s.status === "sent" ? "重发" : "发送") + "</button> ";
      }
      if (s.status !== "forming"){
        h += "<button class='tbtn' style='background:#fff;color:#999;border-color:#ddd' " +
             "onclick='ignoreShot(" + i + ")' title='从列表移除，不写日志系统'>忽略</button>";
      }
      h += "</td></tr>";
    });
    tb.innerHTML = h;
  }
  var np = SHOTS.filter(function(s){ return s.status !== "sent"; }).length;
  document.getElementById("npending").textContent = np;
}
function apply(j){
  document.getElementById("dot").className = "dot ok";
  var s = JSON.stringify(j.shots);
  if (s !== LASTJSON){
    var focused = document.activeElement;
    var typing = focused && focused.classList &&
                 (focused.classList.contains("e") || focused.classList.contains("n"));
    if (!typing){          // 正在输入时不重绘，避免打断
      SHOTS = j.shots; LASTJSON = s; render();
    }
  }
  if (j.dirs){            // 目录变更 → 信息条实时更新（其他标签页也同步）
    var d = j.dirs.join("；") || "-";
    var el = document.getElementById("dirs");
    if (el.textContent !== d){
      el.textContent = d;
      if (document.getElementById("dirMask").style.display === "flex") openDirs();
    }
  }
  if (j.target){          // 重频靶系统实时状态 → 信息条
    var t = j.target, e2 = document.getElementById("tgt"),
        e3 = document.getElementById("tgtF");
    if (t.ok && t.pos){
      e2.textContent = t.pos;
      e2.style.color = "#2c3e50";
      e3.textContent = t.defocus !== "" ? "（离焦 " + t.defocus + "）" : "";
    } else {
      e2.textContent = "离线";
      e2.style.color = "#c0392b";
      e3.textContent = "";
    }
  }
}
/* ---------- 监视目录管理面板 ---------- */
var DIRS = [];
function openDirs(){
  document.getElementById("dirMask").style.display = "flex";
  fetch("/api/watchdirs", {cache:"no-store"}).then(function(r){ return r.json(); })
  .then(function(j){ DIRS = j.dirs || []; renderDirs(); });
}
function closeDirs(){ document.getElementById("dirMask").style.display = "none"; }
function renderDirs(){
  var h = "";
  DIRS.forEach(function(d, i){
    h += "<div style='display:flex;align-items:center;gap:8px;padding:7px 10px;" +
         "border:1px solid #e6e8eb;border-radius:6px;margin-bottom:6px;font-size:13px;" +
         "font-family:Consolas,monospace'>";
    h += "<span style='flex:1;word-break:break-all'>" + d.path + "</span>";
    if (!d.exists)
      h += "<span style='color:#8a6100;background:#fff3cd;border-radius:4px;" +
           "padding:2px 8px;font-size:11px;font-family:inherit'>目录不存在</span>";
    h += "<button class='tbtn' style='background:#c0392b;padding:4px 12px' " +
         "onclick='delDir(" + i + ")'>删除</button></div>";
  });
  document.getElementById("dirList").innerHTML =
    h || "<div style='color:#999;font-size:13px'>（无监视目录）</div>";
}
function addDir(){
  var inp = document.getElementById("newDir");
  var v = inp.value.trim();
  if (!v){ toast("请输入目录路径"); return; }
  saveDirs(DIRS.map(function(d){ return d.path; }).concat([v]));
  inp.value = "";
}
function delDir(i){
  var rest = DIRS.map(function(d){ return d.path; });
  rest.splice(i, 1);
  saveDirs(rest);
}
function saveDirs(list){
  fetch("/api/watchdirs", {method:"POST", cache:"no-store",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({dirs: list})})
  .then(function(r){ return r.json(); }).then(function(j){
    if (!j.ok){ toast(j.error || "保存失败"); return; }
    DIRS = j.dirs || []; renderDirs();
    document.getElementById("dirs").textContent =
      DIRS.map(function(d){ return d.path; }).join("；") || "-";
    if ((j.added||[]).length) toast("已添加并静默登记：" + j.added.join("、"));
    if ((j.removed||[]).length) toast("已移除：" + j.removed.join("、"));
  }).catch(function(){ toast("保存失败（网络错误）"); });
}
/* 浏览按钮：后端弹系统文件夹对话框，轮询取回真实路径后自动添加 */
function browseDir(){
  var btn = document.getElementById("btnBrowse");
  fetch("/api/pickdir", {method:"POST", cache:"no-store"})
  .then(function(r){ return r.json(); }).then(function(j){
    if (!j.ok){ toast(j.error || "无法打开选择窗口"); return; }
    toast("请在弹出的窗口中选择要监视的文件夹…");
    btn.disabled = true;
    var n = 0;
    var timer = setInterval(function(){
      fetch("/api/pickdir", {cache:"no-store"})
      .then(function(r){ return r.json(); }).then(function(j){
        if (j.state === "done"){
          clearInterval(timer); btn.disabled = false;
          document.getElementById("newDir").value = j.path;
          addDir();
        } else if (j.state === "canceled"){
          clearInterval(timer); btn.disabled = false;
          if (j.error && j.error !== "已取消") toast(j.error);
        }
      }).catch(function(){});
      if (++n > 1200){ clearInterval(timer); btn.disabled = false; }
    }, 500);
  }).catch(function(){ toast("无法打开选择窗口"); });
}
/* ---------- 上报表格绑定面板 ---------- */
var CUR_SHEET = "";
function sheetLabel(sn){
  return sn === "@date" ? "按日期自动分表" : (sn || "实时打靶(默认)");
}
function loadSheetBinding(){
  fetch("/api/sheetname", {cache:"no-store"}).then(function(r){ return r.json(); })
  .then(function(j){
    if (!j.ok) return;
    CUR_SHEET = j.sheet_name || "";
    document.getElementById("sheetName").textContent = j.label || sheetLabel(CUR_SHEET);
  }).catch(function(){});
}
function openSheet(){
  document.getElementById("sheetMask").style.display = "flex";
  document.getElementById("sheetList").innerHTML =
    "<div style='color:#999;font-size:13px'>加载中…</div>";
  fetch("/api/sheetname", {cache:"no-store"}).then(function(r){ return r.json(); })
  .then(function(j){
    if (!j.ok){ toast("读取失败"); return; }
    CUR_SHEET = j.sheet_name || "";
    renderSheet(j.sheets || []);
  }).catch(function(){
    document.getElementById("sheetList").innerHTML =
      "<div style='color:#c0392b;font-size:13px'>连接失败，请稍后重试</div>";
  });
}
function closeSheet(){ document.getElementById("sheetMask").style.display = "none"; }
function renderSheet(sheets){
  var h = "<div style='font-size:11px;color:#888;background:#f6f8fa;border-radius:6px;" +
          "padding:6px 10px;margin-bottom:8px'>点卡片＝切换上报目标表，页面下方「已上报记录」立即跟着换成该表；点「查看 →」＝在 A 机页面打开该表</div>";
  h += sheetRow("@date", "按打靶日期自动分表", "每天打靶自动写入当天日期命名的表（如 2026-09-15），不存在自动创建");
  h += sheetRow("", "实时打靶（默认表）", "所有打靶集中写这一张固定表");
  if (sheets.length){
    h += "<div style='font-size:11px;color:#999;margin:10px 0 6px'>—— A 机已有表格 ——</div>";
    sheets.forEach(function(s, i){
      h += sheetRow(s.name, s.name, s.count + " 条记录", "@dateornull_" + i, s.view_url);
    });
  } else {
    h += "<div style='font-size:11px;color:#999;margin:10px 0 4px'>（A 机表格列表获取失败，仅显示常用选项）</div>";
  }
  document.getElementById("sheetList").innerHTML = h;
}
function sheetRow(val, title, sub, key, viewUrl){
  var sel = (CUR_SHEET === val) ? "border:2px solid #2c3e50;background:#f2f7ff" :
            "border:1px solid #e6e8eb";
  var view = viewUrl ? " <span onclick='event.stopPropagation();window.open(\"" +
             viewUrl + "\")' style='color:#2471a3;font-size:11px;cursor:pointer;" +
             "text-decoration:underline;margin-left:6px'>查看 →</span>" : "";
  return "<div onclick='selectSheet(this)' data-v=\"" + val.replace(/"/g,"&quot;") +
         "\" style='" + sel + ";border-radius:8px;padding:9px 12px;margin-bottom:6px;" +
         "cursor:pointer'><div style='font-size:13px;font-weight:bold'>" + title + view +
         (CUR_SHEET === val ? " <span style='color:#2ecc71;font-size:12px'>✓ 当前</span>" : "") +
         "</div><div style='font-size:11px;color:#888;margin-top:2px'>" + sub + "</div></div>";
}
function selectSheet(el){
  var v = el.getAttribute("data-v");
  fetch("/api/sheetname", {method:"POST", cache:"no-store",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({sheet_name: v})})
  .then(function(r){ return r.json(); }).then(function(j){
    if (!j.ok){ toast(j.error || "保存失败"); return; }
    CUR_SHEET = j.sheet_name || "";
    document.getElementById("sheetName").textContent = j.label;
    toast("上报表格已切换为：" + j.label);
    loadSrv();     // 下方"已上报记录"跟着切换到该表
    openSheet();   // 重新渲染列表高亮
  }).catch(function(){ toast("保存失败（网络错误）"); });
}
function tesc(s){ return String(s==null?"":s).replace(/&/g,"&amp;")
  .replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
function openTrash(){
  document.getElementById("trashMask").style.display = "flex";
  fetch("/api/trash", {cache:"no-store"}).then(function(r){ return r.json(); })
  .then(function(j){
    var b = document.getElementById("trashList");
    var list = j.trash || [];
    if (!list.length){
      b.innerHTML = "<div style='color:#999;padding:24px 0;text-align:center'>" +
                    "回收站是空的</div>";
      return;
    }
    var h = "<table style='width:100%;border-collapse:collapse;font-size:13px'>";
    h += "<tr style='text-align:left;color:#888;font-size:12px'>" +
         "<th style='padding:4px 6px'>发次时间</th><th>No.</th><th>图片</th>" +
         "<th>能量</th><th>状态</th><th>操作</th></tr>";
    list.forEach(function(s){
      h += "<tr style='border-top:1px solid #eee'>" +
           "<td style='padding:5px 6px;white-space:nowrap'>" + tesc(s.shot_time) + "</td>" +
           "<td>" + (s.no || "-") + "</td><td>" + (s.file_count || 0) + "</td>" +
           "<td>" + tesc(s.energy || "") + "</td><td>" + stLabel(s.status) + "</td>" +
           "<td style='white-space:nowrap'>" +
           "<span style='color:#2471a3;cursor:pointer;text-decoration:underline' " +
           "onclick='trashAct(\"" + s.shot_time + "\",\"restore\")'>恢复</span> " +
           "<span style='color:#c0392b;cursor:pointer;text-decoration:underline' " +
           "onclick='trashAct(\"" + s.shot_time + "\",\"delete\")'>彻底删除</span>" +
           "</td></tr>";
    });
    b.innerHTML = h + "</table>";
  }).catch(function(){
    document.getElementById("trashList").innerHTML =
      "<div style='color:#c0392b;font-size:13px'>读取回收站失败</div>";
  });
}
function closeTrash(){ document.getElementById("trashMask").style.display = "none"; }
function trashAct(st, act){
  if (act === "delete" &&
      !confirm("彻底删除该发次（" + st + "）？将无法恢复！")) return;
  fetch("/api/trash", {method:"POST", cache:"no-store",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({act: act, shot_time: st})})
  .then(function(r){ return r.json(); }).then(function(j){
    if (!j.ok){ toast(j.error || "操作失败"); return; }
    toast(act === "restore" ? "已恢复到待确认列表" :
          act === "clear" ? "回收站已清空" : "已彻底删除");
    openTrash(); refresh(); LASTJSON = "";
  }).catch(function(){ toast("操作失败（网络错误）"); });
}
function clearTrash(){
  if (!confirm("清空回收站？里面的发次将永久删除，无法恢复！")) return;
  trashAct("", "clear");
}
function loadSrv(){
  var d = document.getElementById("srvDate").value || "";
  fetch("/api/serverrows?date=" + encodeURIComponent(d), {cache:"no-store"})
  .then(function(r){ return r.json(); })
  .then(function(j){
    document.getElementById("srvTitle").textContent =
      "「" + j.label + "」已上报记录（" + j.rows.length + " 条）" +
      (j.ok ? "" : "（A 机暂不可达）");
    document.getElementById("srvView").href = j.view_url || "#";
    var tb = document.getElementById("srvtb");
    if (!j.rows.length){
      tb.innerHTML = "<tr><td colspan='7' style='color:#999;padding:10px'>" +
                     "该表还没有记录</td></tr>";
      return;
    }
    var h = "";
    j.rows.forEach(function(r){
      h += "<tr><td style='white-space:nowrap'>" + tesc(r.shot_time) + "</td>" +
           "<td>" + (r.no === 0 || r.no ? r.no : "-") + "</td>" +
           "<td>" + tesc(r.target || "") + "</td>" +
           "<td>" + tesc(r.defocus || "") + "</td>" +
           "<td>" + tesc(r.energy || "") + "</td>" +
           "<td>" + (r.file_count || 0) + "</td>" +
           "<td style='color:#888;font-size:12px'>" + tesc(r.first_file || "") +
           "</td></tr>";
    });
    tb.innerHTML = h;
  }).catch(function(){});
}
function refresh(){
  fetch("/api/local", {cache:"no-store"}).then(function(r){ return r.json(); }).then(apply)
  .catch(function(){
    document.getElementById("dot").className = "dot bad";
  });
}
/* 实时通道：服务器有新发次/状态变化立刻推送 */
var ES = null;
function connectSSE(){
  if (!window.EventSource) return;
  try { ES = new EventSource("/api/events"); } catch(e){ return; }
  ES.onmessage = function(ev){
    try { apply(JSON.parse(ev.data)); } catch(e){}
  };
  ES.onerror = function(){ document.getElementById("dot").className = "dot bad"; };
}
function ignoreShot(i){
  var s = SHOTS[i];
  var msg = s.reported
    ? ("该发次已在日志系统里（列表移除不影响已有记录）。忽略 " + s.shot_time + " ？")
    : ("忽略 " + s.shot_time + " ？\n只从本列表移除，不会写入日志系统。");
  if (!confirm(msg)) return;
  fetch("/api/ignore", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({shot_time: s.shot_time})})
    .then(function(r){ return r.json(); })
    .then(function(j){ toast(j.removed ? "已忽略" : "未找到该发次"); refresh(); LASTJSON = ""; })
    .catch(function(e){ toast("请求失败: " + e); });
}
function clearShots(mode){
  var msg = mode === "all"
    ? "清空整个发次列表？\n（正在检测中的发次会保留；日志系统里已有的记录不受影响）"
    : "清理所有已绑定/发送失败的发次？\n（未确认的发次保留）";
  if (!confirm(msg)) return;
  fetch("/api/shots_clear", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({mode: mode})})
    .then(function(r){ return r.json(); })
    .then(function(j){ toast("已移除 " + j.removed + " 条，剩 " + j.left + " 条"); refresh(); LASTJSON = ""; })
    .catch(function(e){ toast("请求失败: " + e); });
}
function send(i, create){
  var inp = document.querySelector("input.e[data-i='" + i + "']");
  var v = (inp ? inp.value : SHOTS[i].energy).trim();
  var s = SHOTS[i];
  if (!v && s.reported && !create){ toast("先填能量再发送"); return; }
  var ninp = document.querySelector("input.n[data-i='" + i + "']");
  var no = parseInt((ninp ? ninp.value : SHOTS[i].no), 10) || 0;
  fetch("/api/bind", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({shot_time: SHOTS[i].shot_time, energy: v,
                          shot_no: no, create: create})})
    .then(function(r){ return r.json(); })
    .then(function(j){
      if (j.ok && j.confirmed){
        toast("已确认上报：打靶行已写入日志系统（能量待填）");
      } else if (j.ok && (j.matched !== undefined)){
        toast(j.by_no ? ("时间窗未命中，已按 No." + no + " 绑定到 " + j.matched_time)
                      : ("已绑定到发次 " + j.matched_time + "（差 " + (+j.diff_sec).toFixed(1) + "s）"));
      } else if (j.ok && j.created){
        toast("已补录独立记录 #" + j.created);
      } else if (j.error === "no_match"){
        var n = j.nearest ? ("最近一条: " + j.nearest.shot_time) : "日志系统暂无记录";
        toast("无匹配发次（±" + j.window_sec + "s）｜" + n + "，可点\"补录\"");
      } else {
        toast("出错: " + (j.message || j.error || "未知错误"));
      }
      refresh(); LASTJSON = "";
    })
    .catch(function(e){ toast("请求失败: " + e); refresh(); LASTJSON = ""; });
}
document.getElementById("machine").textContent = "本机: " + CFG_MACHINE;
document.getElementById("srv").textContent = CFG_SERVER;
document.getElementById("win").textContent = CFG_WINDOW;
document.getElementById("dirs").textContent = CFG_DIRS;
document.getElementById("efield").textContent = CFG_FIELD;
refresh(); connectSSE(); setInterval(refresh, 15000);   // SSE 实时推送，15s 轮询仅作兜底
loadSheetBinding(); loadSrv(); setInterval(loadSrv, 10000);
if (location.hash === "#dirs") openDirs();   // URL 直达目录管理面板
/* 切回标签页/窗口聚焦时立即刷新，不等下一个 4 秒节拍 */
document.addEventListener("visibilitychange", function(){ if (!document.hidden) refresh(); });
window.addEventListener("focus", refresh);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _page(self):
        # 模板变量：CFG_SERVER/CFG_WINDOW 必须生成合法 JS 字面量（带引号/数字）
        html = (HELP_PAGE
                .replace("CFG_SERVER", json.dumps(SERVER_URL))
                .replace("CFG_MACHINE", json.dumps(MACHINE))
                .replace("CFG_DIRS", json.dumps("；".join(WATCH_DIRS)))
                .replace("CFG_FIELD", json.dumps(ENERGY_FIELD))
                .replace("CFG_WINDOW", str(int(MATCH_WINDOW))))
        self._send(200, html, "text/html; charset=utf-8")

    def _snapshot(self):
        with _state_lock:
            shots = [dict(s) for s in STATE["shots"]]
            fm = STATE.get("forming_shot")
            dirs = list(WATCH_DIRS)     # 随快照下发：目录变更所有页面实时同步
            tgt = dict(TARGET)          # 靶位/离焦实时状态
        out = ([dict(fm)] if fm else []) + shots   # "检测中"行置顶
        return json.dumps(
            {"ok": True, "shots": out[:200], "server": SERVER_URL,
             "queue": len(STATE["queue"]), "dirs": dirs, "target": tgt},
            ensure_ascii=False)

    def do_GET(self):
        if urlparse(self.path).path == "/":
            self._page()
        elif urlparse(self.path).path == "/api/local":
            self._send(200, self._snapshot())
        elif urlparse(self.path).path == "/api/events":
            self.sse_events()
        elif urlparse(self.path).path == "/api/watchdirs":
            self._send(200, json.dumps(
                {"ok": True, "dirs": [{"path": d, "exists": os.path.isdir(d)}
                                       for d in WATCH_DIRS]}, ensure_ascii=False))
        elif urlparse(self.path).path == "/api/pickdir":
            self._send(200, json.dumps(pick_dir_status(), ensure_ascii=False))
        elif urlparse(self.path).path == "/api/sheetname":
            self._send(200, json.dumps(get_sheet_binding(), ensure_ascii=False))
        elif urlparse(self.path).path == "/api/trash":
            with _state_lock:
                t = [dict(s) for s in STATE.get("trash", [])]
            self._send(200, json.dumps({"ok": True, "trash": t},
                                       ensure_ascii=False))
        elif urlparse(self.path).path == "/api/serverrows":
            q = parse_qs(urlparse(self.path).query)
            self._send(200, json.dumps(
                get_server_rows((CFG or {}).get("sheet_name", ""),
                                (q.get("date") or [None])[0]),
                ensure_ascii=False))
        elif urlparse(self.path).path == "/export.xlsx":
            with _state_lock:
                shots = [dict(s) for s in STATE["shots"]]
            data = build_xlsx(shots)
            name = ("report_shots_%s.xlsx"
                    % datetime.now().strftime("%Y%m%d_%H%M%S"))
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument."
                             "spreadsheetml.sheet")
            self.send_header("Content-Disposition",
                             "attachment; filename=%s" % name)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def sse_events(self):
        """SSE 实时推送：状态版本号一变就推全量快照，页面毫秒级出新发次"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = -1
        last_beat = time.time()
        try:
            while True:
                with _state_lock:
                    ver = STATE_VER
                if ver != last:
                    last = ver
                    self.wfile.write(
                        ("data: " + self._snapshot() + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                    last_beat = time.time()
                elif time.time() - last_beat >= 15:
                    self.wfile.write(b": ping\n\n")   # 心跳，防代理断连
                    self.wfile.flush()
                    last_beat = time.time()
                time.sleep(0.3)
        except Exception:
            pass   # 客户端断开，结束本次连接线程

    def do_POST(self):
        if urlparse(self.path).path == "/api/bind":
            self.api_bind()
        elif urlparse(self.path).path == "/api/detect":
            p = self._json_body()
            self._send(200, json.dumps(ingest_detect(p), ensure_ascii=False))
        elif urlparse(self.path).path == "/api/watchdirs":
            p = self._json_body()
            self._send(200, json.dumps(
                set_watch_dirs(p.get("dirs") or []), ensure_ascii=False))
        elif urlparse(self.path).path == "/api/pickdir":
            self._send(200, json.dumps(pick_dir_start(), ensure_ascii=False))
        elif urlparse(self.path).path == "/api/sheetname":
            p = self._json_body()
            self._send(200, json.dumps(
                set_sheet_binding(p.get("sheet_name")), ensure_ascii=False))
        elif urlparse(self.path).path == "/api/ignore":
            p = self._json_body()
            self._send(200, json.dumps(
                api_ignore_shot(p.get("shot_time")), ensure_ascii=False))
        elif urlparse(self.path).path == "/api/shots_clear":
            p = self._json_body()
            self._send(200, json.dumps(
                api_shots_clear(str(p.get("mode", "sent"))), ensure_ascii=False))
        elif urlparse(self.path).path == "/api/trash":
            p = self._json_body()
            self._send(200, json.dumps(api_trash_act(p), ensure_ascii=False))
        else:
            self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def api_bind(self):
        """「确认上报 / 发送能量」统一入口（两步走）：
        第 1 步（人工确认）：该发次尚未上报过 → 才把打靶行写入 A 机
        （机器+时间+首文件去重，重复确认不会产生重复行；可无能量只确认发次）。
        第 2 步（能量绑定）：填了能量 → /api/energy 按时间窗绑定/覆盖能量列。"""
        p = self._json_body()
        st = str(p.get("shot_time", "")).strip()
        energy = str(p.get("energy", "")).strip()
        create = bool(p.get("create"))
        with _state_lock:
            shot = next((s for s in STATE["shots"]
                         if s["shot_time"] == st), None)
        if shot is None:
            return self._send(200, json.dumps(
                {"ok": False, "error": "shot_not_found"}))
        try:
            no_in = int(p.get("shot_no") or 0)
        except (TypeError, ValueError):
            no_in = 0
        if no_in:
            shot["no"] = no_in          # 手动改过 No. 以页面为准
        if not energy and shot.get("reported"):
            return self._send(200, json.dumps(
                {"ok": False, "error": "已上报过：填入能量后可重发/覆盖",
                 "already_reported": True}))
        if energy:
            shot["energy"] = energy
        bump_ver()

        # 第 1 步：确认上报——打靶行写入 A 机（带靶位/离焦/No.，含能量如有）
        if not shot.get("reported"):
            try:
                report_shot(shot, energy=energy)
            except Exception as e:
                shot["status"], shot["info"] = "error", "上报日志系统失败"
                save_state()
                log("确认上报失败: %s %r" % (st, e))
                return self._send(200, json.dumps(
                    {"ok": False, "error": "connect_failed", "message": repr(e)},
                    ensure_ascii=False))
            shot["reported"] = True
            shot["info"] = "打靶行已上报" + ("（含能量）" if energy else "，能量待填")
            save_state()
            log("确认上报: %s%s" % (st, "（能量 %s）" % energy if energy else ""))
            if not energy:
                bump_ver()
                return self._send(200, json.dumps(
                    {"ok": True, "confirmed": True}, ensure_ascii=False))

        # 第 2 步：能量绑定（窗口内命中刚上报的行，重发可覆盖修正）
        payload = {"shot_time": st, "energy": energy, "field": ENERGY_FIELD,
                   "machine": BW_MACHINE or MACHINE, "window_sec": MATCH_WINDOW,
                   "shot_no": no_in or (shot.get("no") or 0),
                   "create": create}
        try:
            j = http_post_json(SERVER_URL.rstrip("/") + "/api/energy", payload)
        except urllib.error.HTTPError as e:
            try:
                j = json.loads(e.read().decode("utf-8"))
            except Exception:
                j = {"ok": False, "error": "HTTP %s" % e.code}
        except Exception as e:
            shot["status"], shot["info"] = "error", "连接日志系统失败"
            save_state()
            return self._send(200, json.dumps(
                {"ok": False, "error": "connect_failed", "message": repr(e)},
                ensure_ascii=False))
        if j.get("ok") and j.get("matched") is not None:
            shot["status"] = "sent"
            shot["row_id"] = j.get("matched")
            tag = "按No." if j.get("by_no") else "差%.1fs" % j.get("diff_sec", 0)
            shot["info"] = "→ %s (%s)" % (j.get("matched_time", ""), tag)
        elif j.get("ok") and j.get("created") is not None:
            shot["status"] = "sent"
            shot["row_id"] = j.get("created")
            shot["info"] = "已补录独立记录 #%s" % j.get("created")
        elif j.get("error") == "no_match":
            shot["status"] = "no_match"
            near = j.get("nearest") or {}
            shot["info"] = ("窗口±%gs内无发次｜最近: %s"
                            % (j.get("window_sec", MATCH_WINDOW),
                               near.get("shot_time", "无记录")))
        else:
            shot["status"] = "error"
            shot["info"] = str(j.get("error") or j.get("message") or "未知错误")
        save_state()
        log("能量绑定[%s]: %s = %s → %s" %
            (shot["status"], st, energy, shot["info"]))
        self._send(200, json.dumps(j, ensure_ascii=False))


CFG = {}


def main():
    global SERVER_URL, MACHINE, WATCH_DIRS, ENERGY_FIELD, MATCH_WINDOW, AUTO_REPORT, BW_MACHINE, CFG
    CFG = load_json(CONFIG_PATH, None)
    if CFG is None:
        save_json(CONFIG_PATH, {
            "watch_dirs": [r"D:\实验数据\汤姆逊谱仪"],
            "server_url": "http://192.168.1.100:8765",
            "machine_name": socket.gethostname() + "-Thomson",
            "scan_interval_sec": 2,
            "group_window_sec": 8,
            "match_window_sec": 15,
            "helper_port": 8767,
            "energy_field": "fiber_p_energy",
            "auto_report": True,
        })
        print("已生成默认配置 config_helper.json，请修改 watch_dirs / server_url 后重新运行。")
        sys.exit(1)

    # watch_dirs 列表（推荐）；兼容旧的单目录 thomson_dir 写法
    dirs = CFG.get("watch_dirs") or [CFG.get("thomson_dir")]
    dirs = [d for d in dirs if d]
    SERVER_URL = CFG.get("server_url", "http://127.0.0.1:8765")
    MACHINE = CFG.get("machine_name", socket.gethostname() + "-Thomson")
    WATCH_DIRS = dirs
    interval = float(CFG.get("scan_interval_sec", 2))
    window = float(CFG.get("group_window_sec", 8))
    MATCH_WINDOW = float(CFG.get("match_window_sec", 15))
    ENERGY_FIELD = CFG.get("energy_field", "fiber_p_energy")
    AUTO_REPORT = bool(CFG.get("auto_report", True))
    port = int(CFG.get("helper_port", 8767))
    # b_watcher 机名：确认上报的行用它做 machine，与 b_watcher 去重键对齐
    try:
        BW_MACHINE = (load_json(B_LOCAL_CONFIG_PATH, {}) or {}).get(
            "machine_name", "") or ""
    except Exception:
        BW_MACHINE = ""

    st = load_json(STATE_PATH, None)
    if st:
        with _state_lock:
            STATE.update(st)
            STATE["forming_shot"] = None   # 上次运行残留的"检测中"行不恢复
            STATE.setdefault("trash", [])  # 旧状态文件没有回收站字段
            n_pending = sum(1 for s in STATE["shots"] if s["status"] != "sent")
        log("已恢复状态: %d 条发次记录（其中 %d 条待绑定能量）"
            % (len(STATE["shots"]), n_pending))

    def _thread_exc(args):
        log("线程异常退出: %r" % args.exc_value)
    threading.excepthook = _thread_exc
    t = threading.Thread(target=monitor_loop,
                         args=(interval, window), daemon=True)
    t.start()
    tt = threading.Thread(target=target_poll_loop, daemon=True)
    tt.start()
    log("靶位采集线程已启动（重频靶系统 %s:%s，每 5s 刷新）" %
        (target_client._load_cfg().get("host", "10.0.23.116"),
         target_client._load_cfg().get("port", 5362)))
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        log("汤姆逊能量上报页面: http://127.0.0.1:%d" % port)
        srv.serve_forever()
    except KeyboardInterrupt:
        log("手动停止")


if __name__ == "__main__":
    main()
