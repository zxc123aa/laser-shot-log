# -*- coding: utf-8 -*-
r"""
靶位采集客户端（thomson_helper / b_watcher 共用，纯标准库）
==========================================================
重频靶系统（10.0.23.116:5362）需要 protobuf 才能对话，而 B 机的
helper/watcher 跑在零依赖的解释器上——所以实际连接受一个小子进程：
target_client 用"带 protobuf 的解释器"调起 target_query.py，
解析它输出的一行 JSON。

缓存策略（关键，保证打靶行创建时零延迟）：
  - thomson_helper 起一个后台线程每 5 秒 poll 一次，
    结果写 state_target.json（供 b_watcher 直接读，不用再起子进程）
  - get() 优先读新鲜缓存（默认 30s 内），过期才现场查询（限时）
  - 靶系统离线时 pos 为空字符串，不影响打靶主流程

解释器探测顺序（可被 config 的 target_monitor.python 覆盖）：
  1. WorkBuddy managed venv（protobuf 已装）
  2. D:\Program Files\Python312\python.exe
"""

import json
import os
import subprocess
import threading
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
QUERY_SCRIPT = os.path.join(BASE, "target_query.py")
CACHE_PATH = os.path.join(BASE, "state_target.json")

# 配置来源：config_helper.json / config_b.local.json 的 "target_monitor" 键
_CFG_FILES = [os.path.join(BASE, "config_helper.json"),
              os.path.join(BASE, "config_b.local.json")]
_CFG_LOCK = threading.Lock()
_CFG = None

EMPTY = {"ok": False, "pos": "", "defocus": "", "remain": "", "count": None,
         "x": None, "y": None, "z": None, "error": "尚未查询",
         "ts": ""}

_query_lock = threading.Lock()   # 同一时刻只允许一个子进程在跑
_py_lock = threading.Lock()
_py_exe = None


def _load_cfg():
    global _CFG
    with _CFG_LOCK:
        if _CFG is not None:
            return _CFG
        cfg = {"enabled": True, "host": "10.0.23.116", "port": 5362,
               "python": ""}
        for p in _CFG_FILES:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    tm = json.load(f).get("target_monitor")
                if isinstance(tm, dict):
                    cfg.update({k: v for k, v in tm.items() if v is not None})
                    break
            except Exception:
                continue
        _CFG = cfg
        return cfg


def _has_protobuf(exe):
    try:
        r = subprocess.run([exe, "-c", "import google.protobuf"],
                           capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:
        return False


def find_python():
    """找一个带 protobuf 的解释器（结果缓存）"""
    global _py_exe
    with _py_lock:
        if _py_exe:
            return _py_exe
        cfg = _load_cfg()
        cands = []
        if cfg.get("python"):
            cands.append(cfg["python"])
        cands += [
            r"C:\Users\CLAPA\.workbuddy\binaries\python\envs\default\Scripts\python.exe",
            r"D:\Program Files\Python312\python.exe",
        ]
        for exe in cands:
            if exe and os.path.isfile(exe) and _has_protobuf(exe):
                _py_exe = exe
                return exe
        return None


def _fresh(d, max_age):
    if not d.get("ts"):
        return False
    try:
        t = datetime.strptime(d["ts"], "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return False
    return (time.time() - t) <= max_age


def read_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return dict(EMPTY)


def query(timeout=8.0):
    """现场查一次（子进程），返回 dict。失败时 ok=False、pos=''"""
    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        d = dict(EMPTY)
        d["error"] = "靶位采集已停用(target_monitor.enabled=false)"
        return d
    exe = find_python()
    if not exe:
        d = dict(EMPTY)
        d["error"] = "找不到带 protobuf 的 Python（可在 config 里配 target_monitor.python）"
        return d
    with _query_lock:
        try:
            r = subprocess.run(
                [exe, QUERY_SCRIPT, cfg.get("host", "10.0.23.116"),
                 str(cfg.get("port", 5362))],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            line = (r.stdout or "").strip().splitlines()
            if line:
                d = json.loads(line[-1])
                if isinstance(d, dict):
                    d.setdefault("ok", False)
                    d.setdefault("pos", "")
                    return d
        except Exception as e:
            d = dict(EMPTY)
            d["error"] = repr(e)
            d["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return d
    d = dict(EMPTY)
    d["error"] = "子进程无输出"
    d["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return d


def get(max_age=30, timeout=8.0):
    """打靶上报用：缓存新鲜就直接用（零延迟），否则现场查"""
    d = read_cache()
    if _fresh(d, max_age):
        return d
    d = query(timeout=timeout)
    if d.get("ok"):
        _write_cache(d)
    return d


def _write_cache(d):
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass


def poll():
    """后台轮询线程用：查一次并写缓存，返回结果"""
    d = query()
    if d.get("ok") or d.get("ts"):
        _write_cache(d)
    return d


def pos_for_shot(max_age=30, timeout=6.0):
    """发次上报入口：返回靶位字符串（拿不到就是 ''）"""
    return get(max_age=max_age, timeout=timeout).get("pos", "") or ""
