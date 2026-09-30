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
     - 窗口内找不到发次（B 机还没建这一行 / C 机能量比 B 机 PNG 早到）
       → 状态"无匹配发次"，能量**本地留存**（state_helper.json 的 unmatched
       列表 + 账本），等对应发次行建好后自动/手动重绑。
     - ⚠️ 能量**永远不会**单独建行、更不会新建表格：一行 = 一个 B 机
       shot PNG 发次，这是全系统唯一建行入口（旧的"补录独立记录"已废弃，
       A 机 /api/energy 也不再支持 create）。

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

try:
    import ledger          # 本地账本（灾后重建的唯一真源），见 ledger.py
except Exception:          # 账本模块缺失/损坏也绝不能影响打靶主流程
    ledger = None


def led(ev, day=None):
    """写一条账本事件（静默失败）。所有埋点统一走这里。"""
    if ledger is None:
        return
    try:
        ledger.append(ev, day=day)
    except Exception:
        pass


BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config_helper.json")


def _bypass_proxy_for_lan():
    """实验室内网地址不走系统代理。

    WorkBuddy/每个 shell 会话会注入动态 http_proxy（端口常变，如 52759），
    urllib 默认经代理访问会把 10.0.23.x 内网请求拖死/拦截。
    这里把本服务和靶系统的主机名加入 NO_PROXY，进程内立即生效。"""
    hosts = {"127.0.0.1", "localhost"}
    try:
        from urllib.parse import urlparse as _up
        for cfg in ("config_helper.json", "config_b.local.json"):
            p = os.path.join(BASE, cfg)
            if os.path.exists(p):
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
    except Exception:
        pass
    cur = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    for h in sorted(hosts):
        if h and h not in cur:
            cur = (cur + "," + h) if cur else h
    os.environ["NO_PROXY"] = cur
    os.environ["no_proxy"] = cur


_bypass_proxy_for_lan()

# ---------- 靶位 → 靶类型 映射（来自"第x次打靶靶位"xls，如 D:\怀柔实验规范平台\shotlist20260917.xls） ----------
TTM_JSON = os.path.join(BASE, "target_type_map.json")
TTM_STATE = {"xls_mtime": None, "map": {}, "layout": None,
             "next_check": 0.0, "lock": threading.RLock()}
# RLock：锁内可再调 get_target_type_map


def _ttm_cfg():
    """配置 target_type_map: {"xls": xls文件或目录, "python": 带xlrd的解释器}
    xls 填目录时自动取目录里最新的 shotlist*.xls（映射表每天一份新文件）。"""
    c = (CFG or {}).get("target_type_map") or {}
    return (str(c.get("xls") or "").strip(),
            str(c.get("python") or "D:/anaconda/python.exe").strip())


def _ttm_pick_xls(p):
    if not p:
        return ""
    if os.path.isdir(p):
        cands = [os.path.join(p, f) for f in os.listdir(p)
                 if f.lower().startswith("shotlist")
                 and f.lower().endswith(".xls")]
        return max(cands, key=os.path.getmtime) if cands else ""
    return p


def get_target_type_map(force=False):
    """取 靶位→靶类型 映射（60s 节流；xls 变新后自动重转 JSON）。"""
    xls_cfg, pyexe = _ttm_cfg()
    now = time.time()
    with TTM_STATE["lock"]:
        if not force and now < TTM_STATE["next_check"]:
            return TTM_STATE["map"]
        TTM_STATE["next_check"] = now + 60
        xls = _ttm_pick_xls(xls_cfg)
        if xls and os.path.exists(xls):
            mt = os.path.getmtime(xls)
            if mt != TTM_STATE["xls_mtime"] or not os.path.exists(TTM_JSON):
                try:
                    import subprocess
                    r = subprocess.run(
                        [pyexe, os.path.join(BASE, "xls_target_map.py"),
                         xls, TTM_JSON],
                        capture_output=True, timeout=60)
                    if r.returncode == 0:
                        TTM_STATE["xls_mtime"] = mt
                        log("靶类型映射已更新: %s" % os.path.basename(xls))
                    else:
                        log("靶类型映射转换失败: %s"
                            % r.stderr.decode("utf-8", "replace")[-200:])
                except Exception as e:
                    log("靶类型映射转换异常: %r" % e)
        elif xls_cfg and not xls:
            log("靶类型映射：找不到 shotlist*.xls（配置目录里没有）")
        m, layout = {}, None
        try:
            d = json.load(open(TTM_JSON, encoding="utf-8"))
            m = d.get("map", {})
            layout = d.get("layout")
        except Exception:
            pass
        mb = _ttm_manual_base()      # 已固化：页面上的表就是基表
        if mb and mb.get("map") and not _xls_newer_than(mb["solidified_at"]):
            m = mb["map"]
        elif mb and _xls_newer_than(mb["solidified_at"]):
            global _TTM_BASE_YIELD_LOGGED
            if not _TTM_BASE_YIELD_LOGGED:
                _TTM_BASE_YIELD_LOGGED = True
                log("检测到更新的 shotlist xls，固化基表已让位（新装盘日自动换新表）")
        TTM_STATE["map"] = m
        TTM_STATE["layout"] = layout
        return m


def _clean_type_val(v):
    """靶类型垃圾值防护：xls 空单元格常被读成 'Nan'/'nan'（float 转 str），
    'None'/'null'/'0' 同理，一律视为未填（空字符串）。"""
    s = str(v or "").strip()
    if s.lower() in ("nan", "none", "null", "0", "0.0"):
        return ""
    return s


def lookup_target_type(pos, day=None):
    """靶位 → 靶类型。优先读按日期绑定的当天表（target_types/<日期>.json）；
    日报表机制异常时回退旧系统（手动覆盖 + xls 基表）。"""
    pos = str(pos or "").strip()
    if not pos:
        return ""
    try:
        dm = get_daily_target_map(day)
        return _clean_type_val((dm.get("map") or {}).get(pos))
    except Exception as e:
        log("日报表查询异常(回退旧映射): %r" % e)
    o = _ttm_overrides()
    if pos in o:                       # 手动修改优先（含清空 = 置空）
        return _clean_type_val(o[pos])
    return _clean_type_val(get_target_type_map().get(pos, ""))


TTM_OVERRIDES_PATH = os.path.join(BASE, "target_type_overrides.json")
_TTM_OVR = {"mtime": None, "data": {}}

TTM_TITLE_PATH = os.path.join(BASE, "target_type_title.json")   # 映射表标题（可改日期）

# 固化基表：页面上"以当前表为准"后，生效映射整体存这里，xls 不再参与
TTM_BASE_MANUAL_PATH = os.path.join(BASE, "target_type_base_manual.json")
_TTM_BASE_MANUAL = {"mtime": None, "data": None}
_TTM_BASE_YIELD_LOGGED = False


def _ttm_manual_base():
    """固化的基表 {"solidified_at": epoch, "map": {...}}；无则 None（mtime 缓存）"""
    try:
        mt = os.path.getmtime(TTM_BASE_MANUAL_PATH)
    except OSError:
        _TTM_BASE_MANUAL["data"] = None
        _TTM_BASE_MANUAL["mtime"] = None
        return None
    if mt != _TTM_BASE_MANUAL["mtime"]:
        try:
            d = json.load(open(TTM_BASE_MANUAL_PATH, encoding="utf-8"))
            _TTM_BASE_MANUAL["data"] = {
                "solidified_at": float(d.get("solidified_at") or 0),
                "map": dict(d.get("map") or {})}
        except Exception as e:
            log("固化基表读取失败（忽略）: %r" % e)
            _TTM_BASE_MANUAL["data"] = None
        _TTM_BASE_MANUAL["mtime"] = mt
    return _TTM_BASE_MANUAL["data"]


def _xls_newer_than(ts):
    """当前选中的 shotlist xls 是否比固化时间新（新装盘日 → xls 重新接管基表）"""
    xls = _ttm_pick_xls(_ttm_cfg()[0])
    try:
        return bool(xls) and os.path.exists(xls) and os.path.getmtime(xls) > ts
    except OSError:
        return False


def _ttm_title():
    """靶类型映射表大标题（如"第x次打靶靶位 20260617"），手动编辑优先"""
    try:
        with open(TTM_TITLE_PATH, encoding="utf-8") as f:
            return str((json.load(f) or {}).get("title") or "")
    except Exception:
        return ""


def _save_ttm_title(t):
    try:
        with open(TTM_TITLE_PATH, "w", encoding="utf-8") as f:
            json.dump({"title": t}, f, ensure_ascii=False, indent=1)
        return True
    except Exception as e:
        log("靶类型标题保存失败: %r" % e)
        return False


def set_match_window(sec):
    """页面可调的能量绑定窗口（秒），持久化到 config_helper.json"""
    global MATCH_WINDOW
    try:
        w = float(sec)
    except (TypeError, ValueError):
        return {"ok": False, "error": "无效数值"}
    if not (3 <= w <= 300):
        return {"ok": False, "error": "窗口需在 3~300 秒之间"}
    MATCH_WINDOW = w
    try:
        CFG["match_window_sec"] = w
        save_json(CONFIG_PATH, CFG)
    except Exception as e:
        log("绑定窗口保存失败: %r" % e)
    log("绑定窗口调整为 ±%gs" % w)
    bump_ver()
    return {"ok": True, "window": w}


def _ttm_overrides():
    """手动维护的 靶位→靶类型 覆盖（持久化 target_type_overrides.json，mtime 缓存）"""
    try:
        mt = os.path.getmtime(TTM_OVERRIDES_PATH)
    except OSError:
        if _TTM_OVR["mtime"] is not None:
            _TTM_OVR["mtime"], _TTM_OVR["data"] = None, {}
        return {}
    if mt != _TTM_OVR["mtime"]:
        try:
            _TTM_OVR["data"] = json.load(
                open(TTM_OVERRIDES_PATH, encoding="utf-8"))
            _TTM_OVR["mtime"] = mt
        except Exception:
            pass
    return _TTM_OVR["data"]


def _save_ttm_overrides(o):
    try:
        json.dump(o, open(TTM_OVERRIDES_PATH, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        _TTM_OVR["mtime"] = None      # 下次读取强制重载
        return True
    except Exception as e:
        log("靶类型覆盖保存失败: %r" % e)
        return False


def effective_target_map():
    """自动(xls)映射 + 手动覆盖 合并后的生效映射"""
    m = dict(get_target_type_map())
    m.update(_ttm_overrides())
    return m


def get_target_type_layout():
    """Sheet 版面（含合并区块），无则 None"""
    get_target_type_map()
    return TTM_STATE.get("layout")


# ---------- 按日期绑定的靶类型表（每天一张，与当天日志表同日期） ----------
# 存储：target_types\YYYY-MM-DD.json  {"date","title","map","seeded_from","updated"}
# 规则：页面上编辑哪格 → 直接写入当天这份文件；上报时按发次日期读对应日期的表；
#       当天文件不存在时自动播种（新 xls > 最近日报表 > 旧全局映射），不再回读旧表。
TTM_DAILY_DIR = os.path.join(BASE, "target_types")
_TTM_DAILY = {}          # day -> {"mtime":…, "data":…}


def _ttm_day_str(day=None):
    """'2026-09-29' / '20260929' / None(今天) → 'YYYY-MM-DD'"""
    if not day:
        return datetime.now().strftime("%Y-%m-%d")
    s = str(day).strip()[:10].replace("/", "-").replace(".", "-")
    if re.match(r"^\d{8}$", s):
        s = "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
    return s


def _bound_day():
    """靶类型表跟随「上报表格绑定」：绑定到具体日期表（含 9-31 这类测试
    日期）时，对话框编辑的就是那张日期表；绑定 "@date"/默认 → 编辑今天的表。"""
    sn = str((CFG or {}).get("sheet_name", "") or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", sn):
        return sn
    return _ttm_day_str()


def _is_test_or_future_date(s):
    """日期型绑定是否允许：今天/未来/日历上不存在的日期(如 09-31, 测试用) → 允许；
    真实历史日期 → 不允许（历史表仅供查看）。"""
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return True                       # 日历上不存在的日期 → 测试日期
    return d >= datetime.now().date()


def _ttm_daily_path(day):
    return os.path.join(TTM_DAILY_DIR, _ttm_day_str(day) + ".json")


def _seed_daily_map(day):
    """当天表不存在时播种：
    1) 已有历史日报表：配置目录里的 shotlist xls 比它新（新装盘日落盘）→ 用 xls
       全表；否则沿用最近一天的日报表（靶没换接着用，相对 day 最多回看 14 天）；
    2) 没有任何历史日报表（首次启用）→ 旧系统页面那张表（固化基表+手动覆盖）
       原样接管，保证"页面上看到的"和"今天起生效的"是同一张。"""
    day = _ttm_day_str(day)
    prev, prev_day, prev_mt = None, None, 0.0
    try:
        d0 = datetime.strptime(day, "%Y-%m-%d")
        for back in range(1, 15):
            pd = (d0 - timedelta(days=back)).strftime("%Y-%m-%d")
            p = _ttm_daily_path(pd)
            if os.path.exists(p):
                prev = json.load(open(p, encoding="utf-8"))
                prev_day, prev_mt = pd, os.path.getmtime(p)
                break
    except Exception:
        pass
    if prev and prev.get("map"):
        xls = _ttm_pick_xls(_ttm_cfg()[0])
        xls_mt = 0.0
        try:
            if xls and os.path.exists(xls):
                xls_mt = os.path.getmtime(xls)
        except OSError:
            pass
        if xls_mt and xls_mt > prev_mt:          # 新装盘日的 xls 接管
            m = dict(get_target_type_map(force=True))
            if m:
                return {"date": day,
                        "title": prev.get("title") or _ttm_title(),
                        "map": m,
                        "seeded_from": "xls:" + os.path.basename(xls)}
        return {"date": day, "title": prev.get("title", ""),
                "map": dict(prev["map"]), "seeded_from": "daily:" + prev_day}
    m = dict(get_target_type_map())
    m.update(_ttm_overrides())
    return {"date": day, "title": _ttm_title(), "map": m,
            "seeded_from": "legacy"}


def get_daily_target_map(day=None):
    """取某天的靶类型表 dict（date/title/map/…）；文件不存在则播种并落盘。"""
    day = _ttm_day_str(day)
    with TTM_STATE["lock"]:
        p = _ttm_daily_path(day)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            mt = None
        if mt is None:
            data = _seed_daily_map(day)
            data["map"] = {k: _clean_type_val(v)
                           for k, v in (data.get("map") or {}).items()
                           if _clean_type_val(v)}
            _save_daily_map(day, data)
            log("靶类型日报表已建立: %s（播种自 %s，%d 个靶位）"
                % (day, data.get("seeded_from"), len(data.get("map") or {})))
            return data
        c = _TTM_DAILY.get(day)
        if c and c.get("mtime") == mt and c.get("data") is not None:
            return c["data"]
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            log("靶类型日报表读取失败 %s: %r" % (day, e))
            data = {"date": day, "title": "", "map": {}}
        # 读盘即清洗：历史文件里的 'Nan' 等垃圾值一律清掉（自动愈合）
        data["map"] = {k: _clean_type_val(v)
                       for k, v in (data.get("map") or {}).items()
                       if _clean_type_val(v)}
        _TTM_DAILY[day] = {"mtime": mt, "data": data}
        return data


def _save_daily_map(day, data):
    """日报表落盘（JSON）+ 同步导出 CSV（utf-8-sig，Excel 可直接打开）。"""
    day = _ttm_day_str(day)
    try:
        os.makedirs(TTM_DAILY_DIR, exist_ok=True)
        data = dict(data)
        data["date"] = day
        data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_ttm_daily_path(day), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        _TTM_DAILY.pop(day, None)          # 强制下次重载
        _export_daily_csv(day, data)
        return True
    except Exception as e:
        log("靶类型日报表保存失败 %s: %r" % (day, e))
        return False


def _export_daily_csv(day, data):
    """同步导出 shotlist/<日期>/靶类型_<日期>.csv，便于 Excel 查看/存档"""
    try:
        outdir = os.path.join(BASE, "shotlist", day)
        os.makedirs(outdir, exist_ok=True)

        def keyf(k):
            try:
                a, b = str(k).split("-")
                return (int(a), int(b))
            except Exception:
                return (9999, 9999)
        lines = ["靶位,靶类型"]
        for k in sorted((data.get("map") or {}).keys(), key=keyf):
            v = str(data["map"].get(k) or "").replace(",", "，")
            lines.append("%s,%s" % (k, v))
        name = "靶类型_%s.csv" % day.replace("-", "")
        with open(os.path.join(outdir, name), "w", encoding="utf-8-sig") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        log("靶类型CSV导出失败(忽略): %r" % e)


STATE_PATH = os.path.join(BASE, "state_helper.json")
B_LOCAL_CONFIG_PATH = os.path.join(BASE, "config_b.local.json")  # b_watcher 本机配置（监视目录变更需同步给它）

# 生产数据写保护开关：只有 main() 真正从磁盘 load 过 STATE 后才置 True。
# 测试/脚本 `import thomson_helper` 时它恒为 False → save_state() 直接 no-op，
# 绝不会覆盖生产 state_helper.json（2026-10-01 清空事故的根治）。
_STATE_LOADED = False
_WARNED = set()          # 同类告警只打一次，避免刷屏


def _warn_once(msg):
    if msg in _WARNED:
        return
    _WARNED.add(msg)
    log("⚠ " + msg)


# RLock：save_state() 在调用方已持锁时也会被调用，必须可重入
_state_lock = threading.RLock()
STATE = {"seen": {}, "pend": [], "shots": [], "queue": [], "no_seq": {},
         "forming_shot": None, "trash": [], "unmatched": []}
# "unmatched"：PyTPS 报来但当时找不到对应发次的能量（暂存待补绑，绝不丢弃）
STATE_VER = 0          # 页面 SSE 推送用的状态版本号：shots/queue 一变就 +1

# 重频靶系统实时状态（后台线程每 5s 刷新；靶位/离焦随 SSE 推到页面）
TARGET = dict(target_client.EMPTY)
# 靶位轮询历史：[(epoch, pos, defocus), ...]——发次转正比打靶晚 15~20s，
# 挂靶位时取最接近打靶时刻的历史样本，而不是"当前值"（靶可能已被移走）
TARGET_HIST = []
TARGET_HIST_MAX = 240        # 5s 一次 × 240 = 20 分钟历史

# ---------------- C 机 tif 时间线 + 未命中能量暂存 ----------------
# 为什么需要：PyTPS 只发 {filename: "shor_79.tif", energy: "19.23"}，**不带时间戳**；
# 而 C 机的 tif 不在 B 机的监视目录里，B 机按文件名根本找不到对应发次
# （09-30 实测：B 机 /api/energy_remote 零调用，77 条 tps_h 全靠 A 机时间窗猜）。
# 解法（用户定调）：C 机把 tif 时间表（name + 浮点 mtime）推给 B 机，
# B 机用 tif 时间去找**时间最近的 B 机 shot PNG 发次** —— 唯一时间基准 = PNG mtime。
TIF_TIMELINE = {}          # {day: {tif名小写: {"name","folder","mtime","machine"}}}
TIF_LOCK = threading.Lock()


def _epoch(s):
    """'YYYY-MM-DD HH:MM:SS' → 浮点 epoch；失败返回 None"""
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return None


def tif_add(day, files, machine=""):
    """登记 C 机推来的 tif 时间表（同名覆盖为最新 mtime）。返回新增条数。"""
    n = 0
    with TIF_LOCK:
        d = TIF_TIMELINE.setdefault(str(day), {})
        for f in files or []:
            if not isinstance(f, dict):
                continue
            nm = str(f.get("name", "")).strip()
            if not nm:
                continue
            d[nm.lower()] = {"name": nm,
                             "folder": str(f.get("folder", "")),
                             "mtime": float(f.get("mtime") or 0),
                             "machine": str(machine or f.get("machine", ""))}
            n += 1
    return n


def tif_lookup(fn):
    """按 tif 文件名查时间线 → {"name","folder","mtime","machine"} 或 None"""
    key = os.path.basename(str(fn or "")).strip().lower()
    if not key:
        return None
    with TIF_LOCK:
        for day in sorted(TIF_TIMELINE.keys(), reverse=True):
            hit = TIF_TIMELINE[day].get(key)
            if hit:
                return dict(hit, day=day)
    return None


def nearest_shot_by_ts(ts, window=None):
    """给定浮点时间戳，找 B 机发次里时间最近的一发（基准 = PNG mtime 推出的
    shot_time）。超出 window（默认 MATCH_WINDOW）视为未命中，返回 None。"""
    w = MATCH_WINDOW if window is None else float(window)
    best, bd = None, None
    with _state_lock:
        cands = list(STATE.get("shots") or [])
    for s in cands:
        e = _epoch(s.get("shot_time"))
        if e is None:
            continue
        d = abs(e - ts)
        if bd is None or d < bd:
            best, bd = s, d
    if best is not None and bd is not None and bd <= w:
        return best, bd
    return None, bd


def _nearest_tif(shot_time):
    """给定发次时间，从 C 机 tif 时间线里找最近的一张（账本 energy_match 的佐证：
    证明"这一发对应哪个 tif"，而不是只靠 A 机时间窗猜）。"""
    e = _epoch(shot_time)
    if e is None:
        return None
    day = str(shot_time)[:10]
    best, bd = None, None
    with TIF_LOCK:
        pool = list((TIF_TIMELINE.get(day) or {}).values())
    for t in pool:
        mt = float(t.get("mtime") or 0)
        if not mt:
            continue
        d = abs(mt - e)
        if bd is None or d < bd:
            best, bd = t, d
    if best is None:
        return None
    return {"name": best.get("name"), "mtime": best.get("mtime"),
            "machine": best.get("machine"),
            "diff_sec": round(bd, 3) if bd is not None else None}


def _stash_unmatched_energy(fn, energy, field, source):
    """能量到达但当前找不到对应发次 → 暂存（绝不丢弃）。
    存进 STATE["unmatched"]（随 state_helper.json 落盘，重启不丢），
    同时写账本 unmatched_energy。返回 energy_id。

    归档日期 = **到达当天**（此刻还不知道该能量属于哪一发，没有归属日可用）。
    补绑成功后 flush_unmatched_energy 会把结果同时写进这个到达日，
    跨零点打靶时两边都能查到完整闭环。"""
    eid = "e-%d-%s" % (int(time.time() * 1000),
                       os.path.basename(str(fn or "")).lower()[:24])
    rec = {"id": eid, "filename": str(fn or ""), "energy": str(energy),
           "field": str(field or "tps_h"), "source": str(source or ""),
           "stored_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "stored_ts": time.time(),
           "stored_day": datetime.now().strftime("%Y-%m-%d")}
    with _state_lock:
        STATE.setdefault("unmatched", []).append(rec)
        bump_ver()
        save_state()
    led({"ev": "unmatched_energy", "energy_id": eid, "filename": rec["filename"],
         "energy": rec["energy"], "field": rec["field"], "source": rec["source"],
         "stored_at": rec["stored_at"]}, day=rec["stored_day"])
    log("能量暂存待补绑: %s = %s（%s，未找到对应发次）"
        % (rec["filename"], rec["energy"], rec["field"]))
    return eid


def flush_unmatched_energy():
    """把暂存的未命中能量补绑：用 C 机 tif 时间表把 tif 名解析成时间，
    再找时间最近的 B 机 PNG 发次（唯一基准），直接 POST A 机 /api/energy。
    在新发次转正后、以及收到 tif 时间表后各调一次。返回补绑成功条数。"""
    with _state_lock:
        pending = list(STATE.get("unmatched") or [])
    if not pending:
        return 0
    done, bound = [], 0
    for rec in pending:
        t = tif_lookup(rec.get("filename"))
        if not t or not t.get("mtime"):
            continue                       # tif 时间表还没到，下次再试
        shot, diff = nearest_shot_by_ts(float(t["mtime"]))
        if shot is None:
            continue                       # B 机还没建这一发的组，下次再试
        st = shot.get("shot_time")
        payload = {"shot_time": st, "energy": rec.get("energy"),
                   "field": rec.get("field") or "tps_h",
                   "machine": BW_MACHINE or MACHINE,
                   "window_sec": MATCH_WINDOW,
                   "shot_no": shot.get("no") or 0, "create": False}
        try:
            j = http_post_json(SERVER_URL.rstrip("/") + "/api/energy", payload)
        except Exception as e:
            led({"ev": "unmatched_retry_fail", "energy_id": rec.get("id"),
                 "filename": rec.get("filename"), "err": repr(e)},
                day=str(st)[:10] or None)
            continue
        ok = bool(j.get("ok")) and (j.get("matched") is not None
                                    or j.get("created") is not None)
        if not ok:
            continue
        bound += 1
        done.append(rec.get("id"))
        with _state_lock:
            e = norm_energies(shot)
            e[rec.get("field") or "tps_h"] = rec.get("energy")
            shot["energies"] = e
            shot["status"] = "sent"
            if j.get("matched") is not None:
                shot["row_id"] = j.get("matched")
            shot["info"] = "能量补绑（%s → %s，tif %s 差%.1fs）" % (
                rec.get("filename"), st, t.get("name"), diff or 0)
        save_state()
        _bound_rec = {"ev": "unmatched_bound", "energy_id": rec.get("id"),
                      "filename": rec.get("filename"), "energy": rec.get("energy"),
                      "field": rec.get("field"), "shot_time": st,
                      "shot_no": shot.get("no"),
                      "row_id": j.get("matched") if j.get("matched") is not None else j.get("created"),
                      "tif_name": t.get("name"), "tif_mtime": t.get("mtime"),
                      "diff_sec": round(diff, 3) if diff is not None else None,
                      "waited_sec": round(time.time() - float(rec.get("stored_ts") or 0), 1),
                      "a_resp": j}
        # 写进发次归属日（数据属于哪天，重建就读哪天）
        led(dict(_bound_rec), day=str(st)[:10] or None)
        # 跨零点打靶时，暂存记在"到达日"、补绑记在"归属日"会分家 →
        # 两个日期不同时，到达日也补一份，保证任一天单独看都有完整闭环
        _sday = str(rec.get("stored_day") or "")
        if _sday and _sday != str(st)[:10]:
            led(dict(_bound_rec, cross_day_from=_sday), day=_sday)
        log("能量补绑成功: %s = %s → %s（tif %s）"
            % (rec.get("filename"), rec.get("energy"), st, t.get("name")))
    if done:
        with _state_lock:
            STATE["unmatched"] = [r for r in STATE.get("unmatched", [])
                                  if r.get("id") not in done]
            bump_ver()
            save_state()
    return bound


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
            if not d.get("error"):
                TARGET_HIST.append((time.time(), d.get("pos") or "",
                                    d.get("defocus") or ""))
                del TARGET_HIST[:-TARGET_HIST_MAX]
        if changed:
            bump_ver()   # 靶位/离焦变化也推给页面
        time.sleep(interval)


# 发现新文件时的实时查询节流（forming 期间文件陆续落盘会反复触发）
_LIVE_TGT = {"t": 0.0, "lock": threading.Lock()}


def live_fetch_target(timeout=3.0, min_gap=3.0):
    """发现新文件 → 立即实时查一次靶位（子进程，几百 ms）。
    结果进 TARGET_HIST（供 attach_target 取打靶时刻最近样本）并更新当前缓存。
    min_gap 节流，避免文件陆续落盘时子进程轰炸。"""
    now = time.time()
    with _LIVE_TGT["lock"]:
        if now - _LIVE_TGT["t"] < min_gap:
            return None
        _LIVE_TGT["t"] = now
    try:
        d = target_client.query(timeout=timeout)
    except Exception:
        return None
    with _state_lock:
        TARGET.update(d)
        TARGET_HIST.append((time.time(), d.get("pos") or "",
                            d.get("defocus") or ""))
        del TARGET_HIST[:-TARGET_HIST_MAX]
    return d


# live_fetch_target 的异步版：主循环发现新文件时调用它，**绝不阻塞扫描**。
# 同步版会在靶系统离线时卡住 3s（实测），这段时间主循环完全不扫描——
# 这就是"说好百 ms、实际十几秒"的真凶之一。改成后台线程跑，主循环立即返回。
_LIVE_INFLIGHT = threading.Event()   # 已有查询在飞就不再叠加


def live_fetch_target_async(timeout=3.0, min_gap=3.0):
    """非阻塞触发一次实时靶位查询。结果稍后进 TARGET_HIST；
    发次转正时若查询还没回来，attach_target 自动降级用最近的历史样本。"""
    now = time.time()
    with _LIVE_TGT["lock"]:
        if now - _LIVE_TGT["t"] < min_gap:
            return False                       # 节流窗口内，跳过
        if _LIVE_INFLIGHT.is_set():
            return False                       # 上一次还在飞，不叠加
        _LIVE_TGT["t"] = now
        _LIVE_INFLIGHT.set()

    def _run():
        try:
            live_fetch_target(timeout=timeout, min_gap=0)
        except Exception as e:
            log("异步实时靶位查询异常: %r" % e)
        finally:
            _LIVE_INFLIGHT.clear()
    threading.Thread(target=_run, daemon=True).start()
    return True


def bump_ver():
    global STATE_VER
    STATE_VER += 1
SERVER_URL = ""
MACHINE = ""
WATCH_DIRS = []
# 能量字段清单（顺序即页面显示顺序）：117 等任一台电脑打开 8767 页面，
# 都可同时上传 闪烁光纤能量 / TPS: H+ / TPS: C6，分别写入 A 机对应列。
DEFAULT_ENERGY_FIELDS = [
    {"key": "fiber_p_energy", "label": "闪烁光纤能量", "ph": "如 2.35"},
    {"key": "tps_h",          "label": "TPS: H+能量",  "ph": "如 12.5"},
    {"key": "tps_c6",         "label": "TPS: C6能量",  "ph": "如 35"},
]
ENERGY_FIELD = "fiber_p_energy"   # 旧单字段（兼容保留，取第一个 key）
ENERGY_FIELDS = [dict(DEFAULT_ENERGY_FIELDS[0])]
MATCH_WINDOW = 15.0


def efield_keys():
    return [f["key"] for f in ENERGY_FIELDS]


def ekey0():
    return ENERGY_FIELDS[0]["key"]


def norm_energies(s):
    """把一条发次的能量统一成 energies 字典（兼容旧版单 energy 字符串）"""
    e = s.get("energies")
    if not isinstance(e, dict):
        e = {}
    legacy = str(s.get("energy") or "").strip()
    if legacy and not str(e.get(ekey0()) or "").strip():
        e[ekey0()] = legacy
    s["energies"] = e
    return e


def energy_cell(s):
    """一行发次的全部能量，紧凑展示（回收站 / 导出用）"""
    e = norm_energies(s)
    parts = []
    for f in ENERGY_FIELDS:
        v = str(e.get(f["key"]) or "").strip()
        if v:
            parts.append("%s:%s" % (f["label"].replace("能量", ""), v))
    return "  ".join(parts)
AUTO_REPORT = True
REPORT_SHOTS = True   # false=C机模式：只发能量，永不向 A 机建行
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
    # ══════════ 生产数据写保护（2026-10-01 事故的根治） ══════════
    # 事故：AI 的测试进程 `import thomson_helper` 后调用了会触发 save_state()
    # 的函数 → 把内存里的**空 STATE** 写进 state_helper.json，
    # 覆盖了 289 条发次 + 14216 条 seen（靠运行实例内存才抢回来）。
    # 两道锁，缺一都可能再次清空生产数据：
    #   锁1 _STATE_LOADED：只有 main() 真正从磁盘恢复过状态，才允许写盘。
    #        → 测试/脚本 import 模块后绝不可能覆盖生产文件。
    #   锁2 空数据保护：STATE 全空而磁盘已有数据时拒绝覆盖。
    #        → 即使 load 失败/config 读错，也不会把库清空。
    # 确需强制写空（如彻底重置）：设环境变量 LSL_FORCE_EMPTY_STATE=1。
    if not _STATE_LOADED and os.environ.get("LSL_FORCE_EMPTY_STATE") != "1":
        return
    with _state_lock:
        # _files（文件明细 name/folder/path/浮点mtime）只活在内存里给账本用，
        # 不落盘：state_helper.json 已 1.7MB，再塞 289 发 × 明细会翻几倍，
        # 而账本 ledger/<day>.jsonl 才是这份明细的持久归宿（追加、不可变）。
        slim = dict(STATE)
        for key in ("shots", "trash"):
            if isinstance(slim.get(key), list):
                slim[key] = [{k: v for k, v in s.items() if k != "_files"}
                             if isinstance(s, dict) else s for s in slim[key]]
        fm = slim.get("forming_shot")
        if isinstance(fm, dict):
            slim["forming_shot"] = {k: v for k, v in fm.items()
                                    if k != "_files"}
        if os.environ.get("LSL_FORCE_EMPTY_STATE") != "1":
            empty_now = (not slim.get("shots") and not slim.get("seen"))
            if empty_now:
                try:
                    old = json.load(open(STATE_PATH, encoding="utf-8"))
                except Exception:
                    old = {}
                if old.get("shots") or old.get("seen"):
                    _warn_once(
                        "拒绝把空状态写进 state_helper.json（磁盘已有 "
                        "%d 发/%d seen）。如确需清空，先停 helper 并设 "
                        "LSL_FORCE_EMPTY_STATE=1。"
                        % (len(old.get("shots") or []),
                           len(old.get("seen") or {})))
                    return
        save_json(STATE_PATH, slim)


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)
    # 落盘：原来只 print，能量绑定/上报失败的痕迹关窗即失
    # （09-30 的 tps_h=19.23 归属查不清就是这么来的）→ logs/thomson_helper_日期.log
    if ledger is not None:
        try:
            ledger.log_line("thomson_helper", msg)
        except Exception:
            pass


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


# ---------------- 发次文件识别：白名单（默认，09-30 教训） ----------------
# 旧做法是黑名单（只排除 .txt/.log/.xls/发次记录*），但监视父目录
# D:\data_main\Target_Front\2026 下实测有 **1517 个非发次 PNG**：
#   高倍靶前-20260109-161102-761-...PNG / 315远场-Snapshot-...PNG /
#   15B 高倍靶前-TF20260424-...PNG / M1回光测量-Snapshot-...PNG …
# 这些一旦新落盘就被当成"发次"，而且文件名里没有 shot 编号 → 解析不出 no
# → 走"自动补号"抢走真发次的序号，整表条目错位（09-30 shot21/22/79/80 之乱）。
#
# 新规则：**只有文件名匹配发次命名规则的才可能建条目**，其余一律只登记不处理。
# 现场遇到没收进来的新相机命名时，改 config 的 shot_file_patterns 即可，
# 无需改代码；实在来不及可把 shot_file_mode 设成 "blacklist" 应急回退。
_META_FILE_RE = re.compile(
    r"发次记录[^/\\]*$|\.(txt|log|ini|tmp|csv|xls|xlsx|json)$", re.IGNORECASE)

DEFAULT_SHOT_PATTERNS = [
    r"^shot[-_ ]?\d+\.(png|tif|tiff|dat|raw|jpg|jpeg|bmp)$",   # B机 shot79.PNG
    r"^shor[-_ ]?\d+\.(png|tif|tiff|dat|raw|jpg|jpeg|bmp)$",   # C机 shor_79.tif
]

_PAT_CACHE = {}        # {(mode, patterns元组): 编译好的 re} —— 支持热重载
_IGNORED_SEEN = set()  # 已提示过"被忽略"的文件名模式，避免每 200ms 刷屏


def is_meta_file(path):
    return bool(_META_FILE_RE.search(os.path.basename(str(path))))


def _shot_filter():
    """返回 (mode, 白名单正则)。config 改了自动重编译（缓存按配置签名）。"""
    cfg = CFG or {}
    pats = cfg.get("shot_file_patterns") or DEFAULT_SHOT_PATTERNS
    if not isinstance(pats, (list, tuple)) or not pats:
        pats = DEFAULT_SHOT_PATTERNS
    mode = str(cfg.get("shot_file_mode", "whitelist")
               or "whitelist").strip().lower()
    if mode not in ("whitelist", "blacklist"):
        mode = "whitelist"
    key = (mode, tuple(str(x) for x in pats))
    rx = _PAT_CACHE.get(key)
    if rx is None:
        try:
            rx = re.compile("|".join(pats), re.IGNORECASE)
        except re.error as e:
            log("shot_file_patterns 正则非法(%r)，回退默认白名单" % e)
            rx = re.compile("|".join(DEFAULT_SHOT_PATTERNS), re.IGNORECASE)
        _PAT_CACHE[key] = rx
    return mode, rx


def is_shot_file(path):
    """这个文件能不能建发次条目？白名单模式下：只有匹配发次命名的才行。"""
    name = os.path.basename(str(path))
    mode, rx = _shot_filter()
    if mode == "blacklist":        # 应急回退：旧行为（只排黑名单）
        return not is_meta_file(name)
    return bool(rx.match(name))


def _shot_filter_desc():
    """启动日志用：当前发次识别规则的人话描述。"""
    mode, rx = _shot_filter()
    if mode == "blacklist":
        return ("黑名单(应急模式，不推荐)",
                "除 .txt/.log/.xls/发次记录* 外都当发次")
    return ("白名单", rx.pattern)


def note_ignored(files):
    """被白名单挡掉的新文件：每类命名只提示一次 + 写账本（留证据）。
    现场如果看到"某真发次被忽略了"，把它的命名加进 config 的
    shot_file_patterns 即可，不必改代码。"""
    if not files:
        return
    fresh = []
    for p, mt in files:
        name = os.path.basename(str(p))
        sig = re.sub(r"\d+", "N", name).lower()     # 数字归一 → 同类只报一次
        if sig in _IGNORED_SEEN:
            continue
        _IGNORED_SEEN.add(sig)
        fresh.append((name, sig, mt))
    if not fresh:
        return
    for name, sig, mt in fresh[:5]:
        log("已忽略非发次文件: %s（不建条目；如需纳入请改 config 的 "
            "shot_file_patterns）" % name)
    if len(fresh) > 5:
        log("…另有 %d 类非发次文件被忽略" % (len(fresh) - 5))
    led({"ev": "file_ignored", "count": len(files),
         "kinds": [{"name": n, "pattern": s} for n, s, _m in fresh[:20]],
         "mode": _shot_filter()[0]})


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
        return _split_groups_by_no(groups + [cur]), []
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


def _split_groups_by_no(groups):
    """连拍拆分：相机缓冲常把连着的几发集中落盘，整批挤进同一个安静窗口，
    按旧逻辑会被并成一行。这里按文件名编号把不同发的文件拆回各自的组；
    解析不出编号的文件挂到时间最近的编号组（避免自动补号抢号）。"""
    out = []
    for g in groups:
        by = {}
        for p, mt in g:
            m = NO_PAT.search(os.path.basename(p))
            by.setdefault(int(m.group(1)) if m else None, []).append((p, mt))
        if len(by) <= 1:
            out.append(g)
            continue
        anon = by.pop(None, None)
        keys = sorted(by)
        if anon:
            by[keys[0]] = by[keys[0]] + anon
        out.extend(by[k] for k in keys)
    return out


def make_shot(group):
    names = [os.path.basename(p) for p, _mt in sorted(group, key=lambda x: x[1])]
    st = datetime.fromtimestamp(min(mt for _p, mt in group)).strftime(
        "%Y-%m-%d %H:%M:%S")
    # _files：完整文件明细（name + folder + **浮点** mtime）。
    # 为什么必须留浮点 mtime：A 机表的 shot_time 只有秒级，同秒内多发无法排序；
    # 而 files 上报时原本被写成 mtime:0（见 report_shot），信息全丢。
    # 账本靠这个明细才能在事后精确重建"哪个文件属于哪一发、先后顺序如何"。
    detail = [{"name": os.path.basename(p),
               "folder": os.path.dirname(p),
               "path": p,
               "mtime": mt} for p, mt in sorted(group, key=lambda x: x[1])]
    return {"shot_time": st, "files": names, "file_count": len(group),
            "no": parse_shot_no(names),
            "target": "", "defocus": "",
            "energy": "", "energies": {}, "status": "pending", "info": "",
            "row_id": None, "reported": False, "_files": detail}


def attach_target(shot):
    """挂靶位/离焦：取打靶时刻（shot_time，即谱仪落盘时间）最接近的
    轮询历史样本（±30s 内），避免转正晚 15~20s 时靶位已被移走；
    无历史或超出范围时回落当前缓存（旧行为）。"""
    try:
        t0 = datetime.strptime(shot["shot_time"],
                               "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        t0 = 0.0
    with _state_lock:
        best = None
        if t0 > 0:
            for (ts, pos, dfc) in TARGET_HIST:
                dt = abs(ts - t0)
                if best is None or dt < best[0]:
                    best = (dt, pos, dfc)
        if best is not None and best[0] <= 30:
            shot["target"] = best[1] or ""
            shot["defocus"] = best[2] or ""
        else:
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


def _ttm_pan_label_set():
    """版面里的全部靶盘号标签集合（如 {"1-1","6-2","8-4",...}，65 个）。
    注意：版面 X-Y 标签是靶盘号，不是靶位！"""
    lay = TTM_STATE.get("layout")
    if not lay:
        return set()
    return {a[2] for a in (lay.get("labels") or []) if len(a) >= 3}


def _ttm_pan_expand(pos):
    """靶盘号 → 覆盖的 8 个靶位（盘行 b → 行 2b-1、2b；槽 p → 列 4p-3 ~ 4p）。
    不是盘号标签则原样返回 [pos]。"""
    if pos not in _ttm_pan_label_set():
        return [pos]
    a = pos.split("-")
    b, p = int(a[0]), int(a[1])
    return ["%d-%d" % (r, c)
            for r in (2 * b - 1, 2 * b)
            for c in (4 * p - 3, 4 * p - 2, 4 * p - 1, 4 * p)]


_A_SYNC_SHEET = {"day": None, "id": None}     # A 机日期表 id 缓存


def _a_sheet_id(day, server):
    """A 机上以日期命名的表 id（当天日志表）"""
    if _A_SYNC_SHEET["day"] == day and _A_SYNC_SHEET["id"]:
        return _A_SYNC_SHEET["id"]
    try:
        j = json.loads(urllib.request.urlopen(
            server.rstrip("/") + "/api/sheets", timeout=6).read()
            .decode("utf-8"))
        sheets = (j.get("sheets") if isinstance(j, dict) else None) \
            or (j.get("rows") if isinstance(j, dict) else None) \
            or (j if isinstance(j, list) else [])
        for s in sheets:
            if str(s.get("name", "")) == day:
                _A_SYNC_SHEET.update(day=day, id=s.get("id"))
                return _A_SYNC_SHEET["id"]
    except Exception as e:
        log("靶类型同步：取 A 机表列表失败 %r" % e)
    return None


def _sync_types_to_a(day, changed):
    """后台线程：把 {靶位: 新类型} 同步到 A 机当天日志行。
    新类型非空 → 覆盖该靶位所有日志行的 target_type；
    新类型为空 → 只清 'nan' 类垃圾值，不动人工填过的类型。"""
    if not changed:
        return
    server = SERVER_URL or "http://10.0.23.155:8765"
    try:
        sid = _a_sheet_id(day, server)
        if not sid:
            log("靶类型同步：A 机没有 %s 表，跳过" % day)
            return
        url_rows = (server.rstrip("/")
                    + "/api/rows?sheet_id=%s&page=1&page_size=2000" % sid)
        for attempt in (0, 1):
            rows = json.loads(urllib.request.urlopen(url_rows, timeout=8)
                              .read().decode("utf-8")).get("rows") or []
            pending = []
            for r in rows:
                f = r.get("fields") or {}
                pos = str(f.get("target_pos") or "").strip()
                if pos not in changed:
                    continue
                new_t = changed[pos]
                cur_t = str(f.get("target_type") or "").strip()
                if new_t:
                    if cur_t != new_t:          # 覆盖为映射值
                        pending.append((r, new_t))
                elif cur_t.lower() in ("nan", "none", "null"):
                    pending.append((r, ""))     # 只清垃圾值
            if not pending:
                return
            ok_all = True
            for r, val in pending:
                try:
                    j = http_post_json(server.rstrip("/") + "/api/field",
                                       {"id": r["id"], "field": "target_type",
                                        "value": val, "rev": r.get("rev")},
                                       timeout=6)
                    if not j.get("ok"):
                        ok_all = False          # rev 冲突等 → 重取行再试
                except Exception:
                    ok_all = False
            if ok_all:
                log("靶类型已自动同步到 A 机日志 %d 行（%s）"
                    % (len(pending), day))
                return
        log("靶类型同步 A 机：重试后仍有冲突，%d 处未落" % len(pending))
    except Exception as e:
        log("靶类型同步 A 机异常: %r" % e)


def api_targetmap_set(pos, ttype, positions=None, day=None):
    """编辑靶类型 → 直接写入按日期绑定的当天表（target_types/<日期>.json）。
    positions 为合并块内的全部靶位（一次保存整块）；type 为空 → 从当天表删除该格。
    旧页面只发一个靶盘号（如 12-2）→ 自动展开为覆盖的 8 个靶位（批量映射）。"""
    if positions is None:
        positions = [pos] if pos else []
    if not isinstance(positions, list) or not positions:
        return {"ok": False, "error": "缺少靶位"}
    ttype = _clean_type_val(ttype)     # '0'/'Nan' 等垃圾值一律视为未填，防止覆盖入库
    if len(ttype) > 60:
        return {"ok": False, "error": "靶类型太长（≤60 字符）"}
    pos_ok = []
    for p in positions:
        p = str(p or "").strip()
        if re.match(r"^\d{1,3}-\d{1,3}$", p):
            pos_ok.append(p)
    if not pos_ok:
        return {"ok": False, "error": "靶位格式应为 行-列，如 2-2"}
    # 靶盘号批量展开：单个盘号（旧页面只发标签）→ 8 个靶位
    pan_key = None
    if len(pos_ok) == 1 and pos_ok[0] in _ttm_pan_label_set() \
            and len(pos_ok[0].split("-")) == 2:
        pan_key = pos_ok[0]
        pos_ok = _ttm_pan_expand(pan_key)
    day = _ttm_day_str(day)
    with TTM_STATE["lock"]:
        dm = dict(get_daily_target_map(day))
        m = dict(dm.get("map") or {})
        old = {p: (m.get(p) or "") for p in pos_ok}   # 同步用：改动前的值
        for p in pos_ok:
            if ttype:
                m[p] = ttype
            else:
                m.pop(p, None)             # 清空 = 从当天表删除
        if pan_key:                        # 盘号键同步写/删（兼容旧页面显示）
            if ttype:
                m[pan_key] = ttype
            else:
                m.pop(pan_key, None)
        dm["map"] = m
        ok = _save_daily_map(day, dm)
    if ok:
        bump_ver()                          # SSE 推送 → 页面 ttype 实时刷新
        changed = {p: ttype for p in pos_ok if old.get(p, "") != ttype}
        if changed:                         # 后台把改动同步到 A 机日志行
            threading.Thread(target=_sync_types_to_a, args=(day, changed),
                             daemon=True).start()
        log("靶类型[%s]: %s = %s（%d 个靶位）"
            % (day,
               ",".join(pos_ok[:5]) + ("…" if len(pos_ok) > 5 else ""),
               ttype or "（清空）", len(pos_ok)))
    return {"ok": ok, "type": ttype, "positions": pos_ok, "date": day,
            "effective": lookup_target_type(pos_ok[0], day)}


def api_targetmap_solidify():
    """以页面当前生效内容为准，整体另存为绑定日期的日报表（target_types/<日期>.json）。
    新架构下每次编辑都已直接写当天表，此按钮用于把旧系统(xls+覆盖)内容一次性抓进来。"""
    day = _bound_day()
    with TTM_STATE["lock"]:
        merged = dict(get_target_type_map())
        merged.update(_ttm_overrides())
        data = {"solidified_at": time.time(),
                "solidified_str": time.strftime("%Y-%m-%d %H:%M:%S"),
                "title": _ttm_title(),
                "map": merged}
        try:
            json.dump(data, open(TTM_BASE_MANUAL_PATH, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        except Exception as e:
            log("固化基表写入失败: %r" % e)
            return {"ok": False, "error": str(e)}
        _TTM_BASE_MANUAL["mtime"] = None    # 强制下次重载
        TTM_STATE["map"] = merged           # 立即生效（不等 60s 节流）
        TTM_STATE["next_check"] = 0
    bump_ver()
    log("靶类型映射已固化为基表：%d 个靶位（来源=页面当前表，xls 不再参与）"
        % len(merged))
    return {"ok": True, "count": len(merged)}


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
    """把发次列表写成最小合法 xlsx（zip + inlineStr）。
    列：No. / 发次时间 / 图片数 / 图片文件 / 靶位 / 离焦 / 各能量列 / 状态 / 备注"""
    import io
    import zipfile
    cols = (["No.", "发次时间", "图片数", "图片文件", "靶位", "离焦"] +
            [f["label"] for f in ENERGY_FIELDS] + ["状态", "备注"])

    def row_xml(rn, values):
        cells = []
        for i, v in enumerate(values):
            col = chr(ord("A") + i)
            cells.append('<c r="%s%d" t="inlineStr"><is><t xml:space="preserve">'
                         "%s</t></is></c>" % (col, rn, _xml_esc(v)))
        return '<row r="%d">%s</row>' % (rn, "".join(cells))

    body = [row_xml(1, cols)]
    for n, s in enumerate(shots, 2):
        e = norm_energies(s)
        body.append(row_xml(n, [
            s.get("no") or "", s.get("shot_time") or "",
            s.get("file_count") or 0, " ".join(s.get("files") or []),
            s.get("target") or "", s.get("defocus") or "",
            *[str(e.get(f["key"]) or "") for f in ENERGY_FIELDS],
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
    # 兜底过滤（b_watcher 侧已过滤，这里再挡一次）：只收发次命名文件。
    # 白名单模式下的意义：即使有人改了 config_b.local.json 的 watch_exts
    # 把 .txt/.PNG 全放进来，这里也不会让它变成发次条目。
    raw_files = payload.get("files") or []
    files = [f for f in raw_files
             if is_shot_file(f.get("name", "") if isinstance(f, dict)
                             else str(f))]
    if not files:
        return {"ok": True, "merged": False, "ignored": True}
    names = [(f.get("name", "") if isinstance(f, dict) else str(f))
             for f in files]
    # 保留 b_watcher 送来的完整明细（name + folder + 浮点 mtime）。
    # 原来这里只取 names 就把 folder/mtime 全丢了 → 账本无从重建文件时间线。
    detail = [{"name": (f.get("name", "") if isinstance(f, dict) else str(f)),
               "folder": (f.get("folder", "") if isinstance(f, dict) else ""),
               "mtime": (f.get("mtime", 0) if isinstance(f, dict) else 0)}
              for f in files]
    flds = payload.get("fields") or {}
    with _state_lock:
        shot = next((s for s in STATE["shots"]
                     if _time_diff(s["shot_time"], st) <= 2.0), None)
        merged = shot is not None
        if shot is None:
            shot = {"shot_time": st, "files": names, "file_count": len(files),
                    "no": None, "target": "", "defocus": "", "energy": "",
                    "energies": {}, "status": "pending", "info": "",
                    "row_id": None, "reported": False, "_files": detail}
            STATE["shots"].insert(0, shot)
        if len(names) > shot.get("file_count", 0):
            shot["files"] = names
            shot["file_count"] = len(files)
        if detail and not shot.get("_files"):
            shot["_files"] = detail
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
    # ---- 账本：b_watcher 送来的原始发次（含 folder/mtime 明细）----
    led({"ev": "detect_in", "shot_time": st, "no": flds.get("no"),
         "merged": merged, "file_count": len(files), "files": detail,
         "fields": flds,
         "src_machine": str(payload.get("machine", ""))},
        day=st[:10] or None)
    return {"ok": True, "merged": merged}


def _report_day(shot_time):
    """发次写哪张表，靶类型就读哪天的表（表与映射必须同日期）：
    绑定固定日期表（含 09-31 这类测试日期）→ 该日期；
    "@date" / 默认"实时打靶" → 按发次日期。"""
    sn = str((CFG or {}).get("sheet_name", "") or "").strip()
    if sn and sn != "@date" and re.match(r"^\d{4}-\d{2}-\d{2}$", sn):
        return sn
    return str(shot_time)[:10]


def report_shot(shot):
    """把发次上报给 A 机（人工点「确认上报」后才会走到这里）。
    machine 用 b_watcher 的机名：A 机按 machine+时间+首文件去重，
    与 b_watcher 旧直报记录对齐后不会产生重复行。已填的各能量一并写入该行。

    **返回 A 机的完整响应 dict**（不再是 bool）：调用方要拿 id/rev 回写
    shot["row_id"]/shot["reported"]，账本也要记 row_id 才能事后核对。
    历史坑：这里只 return bool(j.get("ok"))，导致 auto_report 直报成功后
    无法回写，09-30 的 128 发有 127 条一直显示 pending / row_id=null。"""
    # 文件明细：优先用 _files 里的**真实浮点 mtime + folder**，
    # 退回旧行为（mtime:0）时 A 机行的 folder 是空的、同秒多发无法排序。
    detail = shot.get("_files") or []
    by_name = {str(d.get("name", "")): d for d in detail}
    files = []
    for n in shot["files"]:
        d = by_name.get(str(n)) or {}
        files.append({"name": n,
                      "folder": d.get("folder", ""),
                      "mtime": d.get("mtime", 0)})
    fields = {"no": shot["no"]} if shot.get("no") is not None else {}
    if shot.get("target"):
        fields["target_pos"] = shot["target"]       # A 机表已有"靶位"列
        tt = lookup_target_type(shot["target"],     # 读行所写表对应日期的映射
                                _report_day(shot["shot_time"]))
        if tt:
            fields["target_type"] = tt              # A 机表已有"靶类型"列
    if shot.get("defocus") != "":
        fields["target_defocus"] = str(shot["defocus"])  # A 机表"靶离焦"列
    e = norm_energies(shot)
    for key in efield_keys():
        v = str(e.get(key) or "").strip()
        if v:
            fields[key] = v                # fiber_p_energy / tps_h / tps_c6 …
    payload = {"machine": BW_MACHINE or MACHINE, "shot_time": shot["shot_time"],
               "files": files, "fields": fields, "reported_at":
               datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    # 上报目标表：""=默认"实时打靶"；"@date"=按打靶日期自动分表；其他=固定表名
    sn = str((CFG or {}).get("sheet_name", "") or "").strip()
    if sn == "@date":
        sn = str(shot["shot_time"])[:10]
    if sn:
        payload["sheet_name"] = sn
    day = str(shot["shot_time"])[:10] or None
    try:
        j = http_post_json(SERVER_URL.rstrip("/") + "/api/shot", payload)
    except Exception as ex:
        # 上报失败也要落账：否则"这一发到底报没报出去"事后无从判断
        led({"ev": "report_fail", "shot_time": shot["shot_time"],
             "no": shot.get("no"), "machine": payload["machine"],
             "sheet_name": payload.get("sheet_name", ""),
             "file_count": shot.get("file_count"),
             "files": detail or files,
             "fields": fields, "err": repr(ex)}, day=day)
        raise
    led({"ev": "report_ok", "shot_time": shot["shot_time"],
         "no": shot.get("no"), "machine": payload["machine"],
         "sheet_name": payload.get("sheet_name", ""),
         "file_count": shot.get("file_count"),
         "files": detail or files, "fields": fields,
         "reported_at": payload["reported_at"],
         "ok": bool(j.get("ok")),
         "row_id": j.get("id"), "rev": j.get("rev"),
         "duplicate": j.get("duplicate"),
         "merged_into": j.get("merged_into"),
         "a_resp": j}, day=day)
    return j


def retry_queue():
    """补发断网期间没送出去的发次上报。
    ⚠️ 这是**阻塞**函数：每条 http_post_json timeout=6s，队列 N 条最坏 N×6s。
    只能由 retry_loop() 后台线程调用，**绝不能在 monitor_loop 主循环里同步调**
    （09-30 的教训：主循环被它卡住十几秒，页面看着像"监测延迟 10s"）。"""
    if not STATE["queue"]:
        return
    with _state_lock:
        todo = list(STATE["queue"])      # 取快照再发，避免长时间持锁
    todo_ids = {id(pl) for pl in todo}   # 用对象身份区分，避免 dict 值相等误删
    still = []
    for pl in todo:
        try:
            j = http_post_json(SERVER_URL.rstrip("/") + "/api/shot", pl)
            log("补发成功: %s" % pl.get("shot_time"))
            led({"ev": "report_retry", "shot_time": pl.get("shot_time"),
                 "ok": bool(j.get("ok")), "row_id": j.get("id"),
                 "duplicate": j.get("duplicate"),
                 "merged_into": j.get("merged_into"), "err": ""},
                day=str(pl.get("shot_time", ""))[:10] or None)
        except Exception as ex:
            still.append(pl)
            led({"ev": "report_retry", "shot_time": pl.get("shot_time"),
                 "ok": False, "err": repr(ex)},
                day=str(pl.get("shot_time", ""))[:10] or None)
    with _state_lock:
        # 保留"补发期间主循环新塞进来的"（不在 todo 快照里的）+ 本轮没发成功的。
        # 绝不整体覆盖 STATE["queue"]，否则会把补发期间新增的失败条目弄丢。
        newcomers = [pl for pl in STATE["queue"] if id(pl) not in todo_ids]
        STATE["queue"] = newcomers + still
        bump_ver()
        save_state()


_RETRY_LOCK = threading.Lock()


def retry_loop(gap=30.0):
    """独立后台线程：周期性补发失败队列。
    与 monitor_loop 解耦——A 机不通时补发线程慢慢等，扫描照常百 ms 级跑。"""
    while True:
        try:
            if STATE.get("queue") and _RETRY_LOCK.acquire(blocking=False):
                try:
                    n0 = len(STATE["queue"])
                    retry_queue()
                    n1 = len(STATE["queue"])
                    if n1 != n0:
                        log("补发线程: 队列 %d → %d" % (n0, n1))
                finally:
                    _RETRY_LOCK.release()
        except Exception as e:
            log("补发线程异常(继续): %r" % e)
        time.sleep(gap)


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


def set_sheet_binding(name):
    """8767 页面切换上报目标表：写 config_helper.json 并同步 config_b.local.json
    （b_watcher 检测到配置 mtime 变化会自动热重载，无需重启）。"""
    sn = str(name or "").strip()
    if sn not in ("", "@date") and re.search(r'[\\/:*?"<>|]', sn):
        return {"ok": False, "error": "表名含非法字符 \\ / : * ? \" < > |"}
    if sn not in ("", "@date") and re.match(r"^\d{4}-\d{2}-\d{2}$", sn) \
            and not _is_test_or_future_date(sn):
        return {"ok": False,
                "error": "历史日期表仅供查看：绑定「按打靶日期自动分表」即可，"
                         "每天自动写入当天日期命名的表"
                         "（今天/未来/测试日期可绑定）"}
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
            # ⚠️ retry_queue() 不在这里同步调了——它每条 http_post_json
            # timeout=6s，A 机 IP 不通时队列有 N 条就阻塞 N×6s，主循环这期间
            # 完全不扫描（"说好百 ms、实际十几秒"的真凶之一）。已移到独立
            # 后台线程 retry_loop()，与扫描解耦。
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
            new_entries = []
            ignored = []
            for p, mt in entries:
                if p in STATE["seen"]:
                    continue
                # 白名单：只有发次命名（shot79.PNG / shor_79.tif）才进发次检测。
                # 其余（高倍靶前-*.PNG、315远场-Snapshot-*.PNG、发次记录.txt…）
                # 只登记 seen，绝不建条目、绝不参与归组与补号。
                if is_shot_file(p):
                    new_entries.append((p, mt))
                else:
                    ignored.append((p, mt))
                    STATE["seen"][p] = mt
            if ignored:
                note_ignored(ignored)
            if new_entries:
                # 关键：发现新文件立刻实时取靶位（不是等转正后的"当前值"）。
                # 异步版：靶系统离线时不会阻塞主循环 3s（实测同步版会卡）。
                # 结果稍后进 TARGET_HIST；发次转正时若还没回来，
                # attach_target 自动降级用最近的历史样本（最多 30s 旧）。
                live_fetch_target_async()
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
                                dup["_files"] = shot.get("_files") or dup.get("_files")
                            elif not dup.get("_files") and shot.get("_files"):
                                dup["_files"] = shot["_files"]   # 保留明细（b_watcher 那条没带）
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
                    # ---- 账本：发次落盘（唯一时间基准 = PNG 浮点 mtime）----
                    # 记下完整明细：no/靶位/靶类型/离焦/目标表/files(name+folder+浮点mtime)。
                    # 表格再乱，靠这一条就能重建"这一发是什么、什么时候、哪些文件"。
                    _tt = ""
                    if shot.get("target"):
                        try:
                            _tt = lookup_target_type(shot["target"], day) or ""
                        except Exception:
                            _tt = ""
                    led({"ev": "shot_group", "shot_time": shot["shot_time"],
                         "no": shot.get("no"),
                         "target_pos": shot.get("target", ""),
                         "target_type": _tt,
                         "target_defocus": shot.get("defocus", ""),
                         "sheet_name": day,
                         "file_count": shot.get("file_count"),
                         "files": shot.get("_files") or []}, day=day)
                    if AUTO_REPORT:
                        try:
                            j = report_shot(shot)
                            log("已上报日志系统: %s" % shot["shot_time"])
                            # 回写 row_id/reported：否则页面永远显示"待确认"，
                            # 且 helper 自己的 shots 无法与 A 机行交叉核对（09-30 的坑）
                            if isinstance(j, dict) and j.get("ok"):
                                rid = j.get("id") or j.get("merged_into")
                                with _state_lock:
                                    tgt = dup if dup is not None else shot
                                    tgt["reported"] = True
                                    if rid is not None:
                                        tgt["row_id"] = rid
                                    if tgt.get("status") == "pending":
                                        tgt["status"] = "sent"
                                    bump_ver()
                                    save_state()
                        except Exception as e:
                            with _state_lock:
                                # 补发队列也带真实明细（原来 mtime:0、无 folder，
                                # 补发出的行 folder 为空、同秒多发无法排序）
                                _fd = shot.get("_files") or []
                                _by = {str(d.get("name", "")): d for d in _fd}
                                STATE["queue"].append({
                                    "machine": MACHINE,
                                    "shot_time": shot["shot_time"],
                                    "fields": {"no": shot["no"]}
                                              if shot.get("no") is not None else {},
                                    "sheet_name": day,
                                    "reported_at": datetime.now().strftime(
                                        "%Y-%m-%d %H:%M:%S"),
                                    "files": [{"name": n,
                                               "folder": (_by.get(str(n)) or {}).get("folder", ""),
                                               "mtime": (_by.get(str(n)) or {}).get("mtime", 0)}
                                              for n in shot["files"]]})
                                bump_ver()
                                save_state()
                            log("发次上报失败(%r)，已入补发队列" % e)
            # 新发次转正后，尝试补绑之前暂存的未命中能量
            # （PyTPS 的能量可能比 B 机建组先到 → 先暂存，等这一刻对上号）
            if done:
                try:
                    flush_unmatched_energy()
                except Exception as e:
                    log("补绑暂存能量异常: %r" % e)
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

# ---- UI 模板已抽离到 ui/helper.html（独立文件，git 合并只碰它、不碰本 .py，
#      根治"A机改了UI、合并B机推送时被旧UI覆盖"）。按 mtime 缓存：
#      改模板即生效，无需重启进程；文件缺失时返回显式错误页而不是崩。----
_UI_PATH = os.path.join(BASE, 'ui/helper.html')
_UI_CACHE = {"mtime": None, "html": None}


def _load_ui_html():
    """读 UI 模板（mtime 缓存）。绝不在这里抛异常打断 HTTP 通道。"""
    try:
        mt = os.path.getmtime(_UI_PATH)
        if _UI_CACHE["mtime"] != mt or _UI_CACHE["html"] is None:
            with open(_UI_PATH, encoding="utf-8") as f:
                _UI_CACHE["html"] = f.read()
            _UI_CACHE["mtime"] = mt
        return _UI_CACHE["html"]
    except OSError as e:
        return ("<!DOCTYPE html><meta charset=utf-8>"
                "<h2>UI 模板缺失</h2><p>%s 读不到: %r</p>"
                "<p>请 git pull 补齐 ui/ 目录。</p>"
                % (_UI_PATH, e))


class Handler(BaseHTTPRequestHandler):

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")   # 页面/接口禁缓存，改版即生效
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _page(self):
        # 模板变量：CFG_SERVER/CFG_WINDOW 必须生成合法 JS 字面量（带引号/数字）
        efs = [dict(f, ph=f.get("ph") or "如 2.35") for f in ENERGY_FIELDS]
        html = (_load_ui_html()
                .replace("CFG_SERVER", json.dumps(SERVER_URL))
                .replace("CFG_MACHINE", json.dumps(MACHINE))
                .replace("CFG_DIRS", json.dumps("；".join(WATCH_DIRS)))
                .replace("CFG_EFIELDS_JSON", json.dumps(efs, ensure_ascii=False))
                .replace("CFG_WINDOW", str(int(MATCH_WINDOW))))
        self._send(200, html, "text/html; charset=utf-8")

    def _snapshot(self):
        # 绑定到哪张表就显示哪天的映射（日报表 target_types/<日期>.json）。
        # ⚠️ 必须走 get_daily_target_map()：effective_target_map() 是**无参**
        # 函数，09-30 这里误传了 _bound_day() → 每次快照抛 TypeError →
        # /api/local 500 且 SSE 连接被打断，页面失去实时推送（表现为"十几秒
        # 才动一次"，实际是轮询兜底）。任何异常都必须降级成空映射，
        # **绝不允许把整条页面通道带崩**。
        try:
            ttm = get_daily_target_map(_bound_day()).get("map") or {}
        except Exception as e:
            log("快照取靶类型映射失败(降级为空): %r" % e)
            ttm = {}
        with _state_lock:
            shots = []
            for s in STATE["shots"]:
                d = dict(s)
                d.pop("_files", None)   # 文件明细只给账本用，别撑大 /api/local(已1.6MB)
                if not d.get("ttype"):
                    d["ttype"] = ttm.get(str(d.get("target") or "").strip(), "")
                shots.append(d)
            fm = STATE.get("forming_shot")
            if fm:
                fm = dict(fm)
                fm.pop("_files", None)
                fm["ttype"] = ttm.get(str(fm.get("target") or "").strip(), "")
            dirs = list(WATCH_DIRS)     # 随快照下发：目录变更所有页面实时同步
            tgt = dict(TARGET)          # 靶位/离焦实时状态
        out = ([fm] if fm else []) + shots   # "检测中"行置顶
        return json.dumps(
            {"ok": True, "shots": out[:200], "server": SERVER_URL,
             "queue": len(STATE["queue"]), "dirs": dirs, "target": tgt},
            ensure_ascii=False)

    def do_GET(self):
        if urlparse(self.path).path == "/":
            self._page()
        elif urlparse(self.path).path == "/api/local":
            self._send(200, self._snapshot())
        elif urlparse(self.path).path == "/api/ledger_stats":
            self.api_ledger_stats()
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
        elif urlparse(self.path).path == "/api/targetmap":
            day = _bound_day()            # 跟随「上报表格绑定」的日期
            dm = get_daily_target_map(day)
            self._send(200, json.dumps(
                {"ok": True, "date": day,
                 "map": dm.get("map") or {},
                 "overrides": {},               # 日报表即最终值，无覆盖层
                 "title": dm.get("title") or "",
                 "seeded_from": dm.get("seeded_from", ""),
                 "layout": get_target_type_layout()},
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
        elif urlparse(self.path).path == "/api/energy_remote":
            self.api_energy_remote()
        elif urlparse(self.path).path == "/api/tif_timeline":
            self.api_tif_timeline()
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
        elif urlparse(self.path).path == "/api/targetmap":
            p = self._json_body()
            self._send(200, json.dumps(
                api_targetmap_set(p.get("pos"), p.get("type"),
                                  p.get("positions"), p.get("day")),
                ensure_ascii=False))
        elif urlparse(self.path).path == "/api/targetmap_solidify":
            self._send(200, json.dumps(api_targetmap_solidify(),
                                       ensure_ascii=False))
        elif urlparse(self.path).path == "/api/targetmap_title":
            p = self._json_body()
            t = str(p.get("title") or "").strip()
            day = p.get("day") or _bound_day()
            with TTM_STATE["lock"]:
                dm = dict(get_daily_target_map(day))
                dm["title"] = t
                ok = _save_daily_map(day, dm)
            self._send(200, json.dumps(
                {"ok": ok, "title": t, "date": day},
                ensure_ascii=False))
        elif urlparse(self.path).path == "/api/matchwindow":
            p = self._json_body()
            self._send(200, json.dumps(
                set_match_window(p.get("sec")), ensure_ascii=False))
        else:
            self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def api_bind(self, p=None):
        """「确认上报 / 发送能量」统一入口（两步走）：
        第 1 步（人工确认）：该发次尚未上报过 → 才把打靶行写入 A 机
        （机器+时间+首文件去重，重复确认不会产生重复行；可无能量只确认发次）。
        第 2 步（能量绑定）：填了任一能量 → 每个能量列独立调 /api/energy
        （fiber_p_energy / tps_h / tps_c6 …），按时间窗绑定/覆盖对应列。"""
        if p is None:                     # 允许 energy_remote 代入已解析的 body
            p = self._json_body()
        st = str(p.get("shot_time", "")).strip()
        create = bool(p.get("create"))
        energies = p.get("energies")
        if not isinstance(energies, dict):
            energies = {}
        legacy = str(p.get("energy", "")).strip()      # 兼容旧客户端单 energy
        # 外部程序（PyTPS 等）可用 field 指定能量列（tps_h/tps_c6…），
        # 不带 field 时按旧约定落第一列（fiber_p_energy）
        fld_in = str(p.get("field") or "").strip()
        if legacy and not str(energies.get(ekey0()) or "").strip():
            energies[fld_in if fld_in in efield_keys() else ekey0()] = legacy
        energies = {str(k): str(v).strip() for k, v in energies.items()
                    if str(v).strip() and k in efield_keys()}
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
        if not energies and shot.get("reported"):
            return self._send(200, json.dumps(
                {"ok": False, "error": "已上报过：填入能量后可重发/覆盖",
                 "already_reported": True}))
        if energies:
            e = norm_energies(shot)
            e.update(energies)
            shot["energy"] = str(e.get(ekey0()) or "")   # 兼容旧展示字段
        bump_ver()

        # 第 1 步：确认上报——打靶行写入 A 机（带靶位/离焦/No.，含能量如有）
        if not shot.get("reported"):
            if not REPORT_SHOTS:
                # C机模式：行由 B 机上报，本机只发能量（第 2 步继续）
                shot["reported"] = True
                shot["info"] = "C机模式：不建行，仅能量绑定"
                save_state()
                log("C机模式：跳过建行 %s（行由 B 机上报）" % st)
                if not energies:
                    return self._send(200, json.dumps(
                        {"ok": True, "c_mode": True,
                         "message": "C机不建行（B机负责）；填入能量后即按时间最近绑定"},
                        ensure_ascii=False))
            else:
                try:
                    report_shot(shot)
                except Exception as e:
                    shot["status"], shot["info"] = "error", "上报日志系统失败"
                    save_state()
                    log("确认上报失败: %s %r" % (st, e))
                    return self._send(200, json.dumps(
                        {"ok": False, "error": "connect_failed", "message": repr(e)},
                        ensure_ascii=False))
                shot["reported"] = True
                shot["info"] = ("打靶行已上报（含能量）" if energies
                                else "打靶行已上报，能量待填")
                save_state()
                log("确认上报: %s%s" % (st, ("（" + energy_cell(shot) + "）")
                                         if energies else ""))
                if not energies:
                    bump_ver()
                    return self._send(200, json.dumps(
                        {"ok": True, "confirmed": True}, ensure_ascii=False))

        # 第 2 步：能量绑定（每个能量列独立调 /api/energy，窗口内命中，
        # 重发可覆盖修正；带 create 时第一列会补录独立记录，后续列
        # 因补录行 shot_time 精确命中同一行，不会产生多条记录）
        results = []   # [(字段label, A机返回), ...]
        for key in efield_keys():
            v = str(norm_energies(shot).get(key) or "").strip()
            if not v:
                continue
            payload = {"shot_time": st, "energy": v, "field": key,
                       "machine": BW_MACHINE or MACHINE,
                       "window_sec": MATCH_WINDOW,
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
            lab = next((f["label"] for f in ENERGY_FIELDS
                        if f["key"] == key), key)
            results.append((lab, j))
            # ---- 账本：每列能量绑定结果（含 A 机命中到哪一行、时间差、匹配依据）----
            # 09-30 查不清 19.23 属于哪一发，就是因为这一步没留痕。
            led({"ev": "energy_bind", "shot_time": st, "field": key,
                 "energy": v, "shot_no": payload.get("shot_no"),
                 "window_sec": MATCH_WINDOW,
                 "result": ("matched" if (j.get("ok") and j.get("matched") is not None)
                            else "created" if (j.get("ok") and j.get("created") is not None)
                            else "no_match" if j.get("error") == "no_match"
                            else "error"),
                 "row_id": j.get("matched") if j.get("matched") is not None else j.get("created"),
                 "rev": j.get("rev"),
                 "matched_time": j.get("matched_time"),
                 "diff_sec": j.get("diff_sec"),
                 "by_no": j.get("by_no"),
                 "no_mismatch": j.get("no_mismatch"),
                 "nearest": (j.get("nearest") or {}).get("shot_time"),
                 "a_resp": j}, day=st[:10] or None)
        # 汇总各能量列结果 → 单一状态
        oks, nms, infos, first_id = [], [], [], None
        for lab, j in results:
            if j.get("ok") and j.get("matched") is not None:
                oks.append(j)
                if first_id is None:
                    first_id = j.get("matched")
                tag = "按No." if j.get("by_no") else "差%.1fs" % j.get("diff_sec", 0)
                infos.append("%s→%s (%s)" % (lab, j.get("matched_time", ""), tag))
            elif j.get("ok") and j.get("created") is not None:
                oks.append(j)
                if first_id is None:
                    first_id = j.get("created")
                infos.append("%s→补录 #%s" % (lab, j.get("created")))
            elif j.get("error") == "no_match":
                nms.append(j)
                near = (j.get("nearest") or {}).get("shot_time", "无记录")
                infos.append("%s→窗口内无发次｜最近: %s" % (lab, near))
            else:
                infos.append("%s→%s" % (lab, j.get("error") or
                                        j.get("message") or "未知错误"))
        if results and len(oks) == len(results):
            shot["status"] = "sent"
            shot["row_id"] = first_id
        elif results and len(nms) == len(results):
            shot["status"] = "no_match"
        elif results:
            shot["status"] = "error"
        shot["info"] = "；".join(infos) if infos else shot.get("info", "")
        save_state()
        log("能量绑定[%s]: %s %s → %s" %
            (shot["status"], st, energy_cell(shot), shot["info"]))
        # ---- 账本：能量匹配总览（哪个发次、命中行、时间差、依据；附 C机tif 佐证）----
        if results:
            _tif = _nearest_tif(st)      # 用 C 机推来的 tif 时间线找最近的一张
            led({"ev": "energy_match", "shot_time": st,
                 "shot_no": shot.get("no"),
                 "matched_row_id": first_id,
                 "status": shot.get("status"),
                 "energies": dict(norm_energies(shot)),
                 "basis": ("tif" if _tif else "time_window"),
                 "tif_name": (_tif or {}).get("name"),
                 "tif_diff_sec": (_tif or {}).get("diff_sec"),
                 "info": shot["info"]}, day=st[:10] or None)
        # 页面提示用第一个能量列的原始返回（完整字段），逐列细节见表格"状态"列
        self._send(200, json.dumps(results[0][1] if results else
                                   {"ok": True, "confirmed": True},
                                   ensure_ascii=False))

    def api_energy_remote(self):
        """外部程序（PyTPS 等）按文件名报能量：
        {"filename": "shor_79.tif", "energy": "19.23"}

        匹配三级（唯一时间基准 = B机 shot PNG 的 mtime）：
          1) 本机发次里就有这个文件名（C机自己跑 helper 时的旧行为）；
          2) **C机推来的 tif 时间表**：用 tif 的 mtime 找时间最近的 B机 PNG 发次
             —— 这才是 B 机能对上 C 机 tif 的唯一途径（B 机发次里只有 shotNN.PNG，
                按文件名精确匹配永远失败，09-30 的能量就是这么丢的）；
          3) 都没有 → **暂存不丢**，等 tif 时间表到达或发次建组后自动补绑。
        """
        p = self._json_body()
        fn = str(p.get("filename", "")).strip()
        energy = str(p.get("energy", "")).strip()
        field = str(p.get("field") or "").strip() or "tps_h"
        if not fn or not energy:
            return self._send(200, json.dumps(
                {"ok": False, "error": "filename/energy 不能为空"},
                ensure_ascii=False))
        fn_low = os.path.basename(fn).lower()
        basis, tdiff = "", None
        with _state_lock:
            shot = next((s for s in STATE["shots"]
                         if any(str(f).lower() == fn_low
                                for f in (s.get("files") or []))), None)
            if shot is None:
                fm = STATE.get("forming_shot")
                if fm and any(str(f).lower() == fn_low
                              for f in (fm.get("files") or [])):
                    shot = fm
        if shot is not None:
            basis = "filename"
        else:
            # 第 2 级：靠 C 机推来的 tif 时间表，用 tif mtime 找最近的 PNG 发次
            t = tif_lookup(fn)
            if t and t.get("mtime"):
                shot, tdiff = nearest_shot_by_ts(float(t["mtime"]))
                if shot is not None:
                    basis = "tif"
        # ---- 账本：能量到达（进函数就记，命中与否都记；附来源与匹配依据）----
        led({"ev": "energy_in", "source": "energy_remote", "filename": fn,
             "energy": energy, "field": field, "raw": p,
             "basis": basis or ("tif_time_out_of_window" if tif_lookup(fn) else ""),
             "tif_mtime": (tif_lookup(fn) or {}).get("mtime"),
             "tif_shot_diff_sec": round(tdiff, 3) if tdiff is not None else None,
             "matched_shot": (shot or {}).get("shot_time"),
             "matched_no": (shot or {}).get("no")},
            day=str((shot or {}).get("shot_time", ""))[:10] or None)
        if shot is None:
            # 未命中：暂存，稍后补绑（不丢）
            eid = _stash_unmatched_energy(fn, energy, field, "energy_remote")
            return self._send(200, json.dumps(
                {"ok": False, "error": "shot_not_found", "stashed": eid,
                 "message": "helper 未找到含 %s 的发次（tif 时间表未到或发次尚未建组），已暂存待补绑" % fn},
                ensure_ascii=False))
        p = {"shot_time": shot["shot_time"], "energy": energy,
             "field": field, "create": False}
        log("远程能量上报: %s = %s（发次 %s，依据 %s%s）"
            % (fn, energy, shot.get("no") if shot.get("no") is not None
               else "?", basis,
               ("差%.1fs" % tdiff) if tdiff is not None else ""))
        self.api_bind(p)

    def api_tif_timeline(self):
        """C 机把本地 shor_N.tif 的时间表推过来（b_watcher 的 push_tif_timeline_to）。
        B 机据此把"tif 名"翻译成"时间"，再找时间最近的 B 机 PNG 发次——
        唯一时间基准始终是 B 机 PNG 的 mtime，C 机发次号不参与匹配。
        body: {"machine":"C机-DAQ-01","day":"2026-09-30",
               "files":[{"name":"shor_12.tif","folder":"D:\\data117\\TPS\\...","mtime":1790000000.1}]}
        收到后立刻尝试补绑之前暂存的未命中能量。"""
        p = self._json_body()
        machine = str(p.get("machine", "")).strip()
        files = p.get("files") or []
        if not isinstance(files, list):
            files = []
        # day 缺省按 tif mtime 推；跨零点的发次由调用方显式给
        day = str(p.get("day", "")).strip()
        if not day and files:
            try:
                day = datetime.fromtimestamp(
                    float(files[0].get("mtime") or 0)).strftime("%Y-%m-%d")
            except Exception:
                day = datetime.now().strftime("%Y-%m-%d")
        n = tif_add(day, files, machine)
        led({"ev": "tif_timeline", "machine": machine, "day": day, "count": n,
             "files": files}, day=day)
        log("收到 C机 tif 时间表: %s %d 条（%s）" % (machine or "?", n, day))
        # tif 时间表一到，之前暂存的能量可能就能对上号了 → 立刻补绑
        bound = 0
        try:
            bound = flush_unmatched_energy()
        except Exception as e:
            log("补绑暂存能量异常: %r" % e)
        with TIF_LOCK:
            total = sum(len(v) for v in TIF_TIMELINE.values())
        self._send(200, json.dumps(
            {"ok": True, "n": n, "day": day, "total": total,
             "bound_now": bound}, ensure_ascii=False))

    def api_ledger_stats(self):
        """GET /api/ledger_stats?day=YYYY-MM-DD —— 体检本机账本（只读）。"""
        q = parse_qs(urlparse(self.path).query)
        day = (q.get("day") or [datetime.now().strftime("%Y-%m-%d")])[0]
        out = {"ok": True}
        if ledger is None:
            out.update(error="ledger 模块不可用")
        else:
            out.update(ledger.stats(day))
            with _state_lock:
                out["unmatched_pending"] = len(STATE.get("unmatched") or [])
            with TIF_LOCK:
                out["tif_days"] = {d: len(v) for d, v in TIF_TIMELINE.items()}
        self._send(200, json.dumps(out, ensure_ascii=False))


CFG = {}


def main():
    global SERVER_URL, MACHINE, WATCH_DIRS, ENERGY_FIELDS, MATCH_WINDOW, AUTO_REPORT, BW_MACHINE, CFG, REPORT_SHOTS
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
            "energy_fields": [{"key": f["key"], "label": f["label"]}
                              for f in DEFAULT_ENERGY_FIELDS],
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
    # 能量字段清单：默认 闪烁光纤 / TPS: H+ / TPS: C6 三列，可在
    # config_helper.json 的 energy_fields 里增删（key=A 机列字段名）
    efs = CFG.get("energy_fields")
    if isinstance(efs, list) and efs:
        fl = []
        for it in efs:
            if isinstance(it, dict) and it.get("key"):
                fl.append({"key": str(it["key"]),
                           "label": str(it.get("label") or it["key"])})
            elif isinstance(it, str):
                fl.append({"key": it, "label": it})
        if fl:
            ENERGY_FIELDS = fl
    ENERGY_FIELD = ekey0()   # 兼容旧展示
    AUTO_REPORT = bool(CFG.get("auto_report", True))
    # C机模式：report_shots=false → 本机永不向 A 机建行（行由 B 机上报），
    # 只做能量绑定（/api/energy 按时间最近匹配已有行）
    REPORT_SHOTS = bool(CFG.get("report_shots", True))
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
            STATE.setdefault("unmatched", [])  # 旧状态文件没有暂存能量字段
            for s in STATE["shots"]:       # 旧单 energy 字符串 → energies 字典
                norm_energies(s)
            for s in STATE.get("trash", []):
                norm_energies(s)
            n_pending = sum(1 for s in STATE["shots"] if s["status"] != "sent")
        log("已恢复状态: %d 条发次记录（其中 %d 条待绑定能量）"
            % (len(STATE["shots"]), n_pending))
    # 生产数据写保护：到这里说明是真正跑起来的实例（main() 已执行、
    # 已尝试从磁盘恢复），授予 save_state() 写盘权限。
    # 测试/脚本 import 模块不会执行 main()，_STATE_LOADED 恒为 False，
    # save_state() 对它们是 no-op → 再也不会覆盖生产 state_helper.json。
    global _STATE_LOADED
    _STATE_LOADED = True
    log("已授予状态写盘权限（_STATE_LOADED=True）")

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
    # 补发线程：与扫描解耦，A 机不通时它自己慢慢等，绝不拖慢监测
    rt = threading.Thread(target=retry_loop,
                          args=(float(CFG.get("retry_gap_sec", 30)),),
                          daemon=True)
    rt.start()
    log("补发线程已启动（每 %gs 检查一次失败队列，不阻塞扫描）"
        % float(CFG.get("retry_gap_sec", 30)))
    log("发次识别: %s / %s" % _shot_filter_desc())
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        log("汤姆逊能量上报页面: http://127.0.0.1:%d" % port)
        srv.serve_forever()
    except KeyboardInterrupt:
        log("手动停止")


if __name__ == "__main__":
    main()
