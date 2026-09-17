# -*- coding: utf-8 -*-
"""
B机端 - 实验数据文件监视器
=========================
原理：
  轮询监视若干数据目录，发现"新出现的文件"即视为一次打靶产生的数据。
  一次打靶会让多台谱仪几乎同时生成文件，因此按时间窗把相邻文件
  归为一组（一次 shot），取组内最早的时间作为打靶时间，上报给 A 机。

特性：
  - 纯 Python 标准库，无需安装任何第三方包
  - 首次运行只登记已有文件、不上报（避免把历史数据当成新实验）
  - 持久化已见文件列表（state_b.json），重启不会重复上报
  - 网络断开时暂存队列（unsent_b.json），恢复后自动补发
  - 支持子目录递归扫描

运行： python b_watcher.py
"""

import json
import os
import re
import socket
import sys
import time
import urllib.request
from datetime import datetime

import target_client

BASE = os.path.dirname(os.path.abspath(__file__))

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
CONFIG_PATH = os.path.join(BASE, "config_b.json")
LOCAL_CONFIG_PATH = os.path.join(BASE, "config_b.local.json")  # 本机实际配置（不入库，优先于 config_b.json）
STATE_PATH = os.path.join(BASE, "state_b.json")
REG_DIRS_PATH = os.path.join(BASE, "state_dirs.json")  # 已"静默登记"过的监视目录清单
PENDING_PATH = os.path.join(BASE, "pending_b.json")  # 已见但未归组的文件缓冲
QUEUE_PATH = os.path.join(BASE, "unsent_b.json")


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


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def group_into_shots(entries, window):
    """按 mtime 排序，相邻间隔 <= window 的文件归为一次打靶。
    返回 ([完整组...], [最后一组（可能还在增长，暂缓上报）])
    """
    if not entries:
        return [], []
    ents = sorted(entries, key=lambda x: x[1])
    groups = []
    cur = [ents[0]]
    for p, mt in ents[1:]:
        if mt - cur[-1][1] <= window:
            cur.append((p, mt))
        else:
            groups.append(cur)
            cur = [(p, mt)]
    # 最后一组：只有当组内最后一个文件已经"安静"超过 window 秒，
    # 才认为这次打靶的数据到齐了，可以上报
    now = time.time()
    if now - cur[-1][1] >= window:
        return groups + [cur], []
    return groups, cur


def send_shot(server_url, payload, timeout=5):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        server_url.rstrip("/") + "/api/shot",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=timeout).read()


def send_detect(helper_url, payload, timeout=5):
    """confirm 模式：把检测到的发次送到本机上报系统（8767）"待确认"列表，
    不直接写 A 机。实验人员在页面上点「确认上报」后才写入。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        helper_url.rstrip("/") + "/api/detect", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=timeout).read()


def send_alert(server_url, machine_name, level, message, timeout=5):
    """向 A 机上报告警（目录失效等），失败不影响主流程"""
    try:
        data = json.dumps({"machine": machine_name, "level": level,
                           "message": message}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            server_url.rstrip("/") + "/api/alert", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=timeout).read()
    except Exception:
        pass  # 告警通道失败不阻塞扫描


# 目录扫描缓存（与 thomson_helper 同款）：{目录: (目录mtime, [(文件路径, mtime), ...], [子目录路径, ...])}
# 逐层校验目录 mtime，没变的层直接用缓存，热扫描从 ~0.4s 降到 ~0.01s
_DIR_CACHE = {}


def scan_once(watch_dirs):
    """扫描所有监视目录（保留旧签名兼容），返回 [(路径, mtime), ...]"""
    return scan_once_status(watch_dirs)[0]


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
                continue  # 文件可能正被写入/刚被移走
    _DIR_CACHE[d] = (mt, files, subs)
    out.extend(files)
    for sub in subs:
        _scan_dir(sub, out)


def scan_once_status(watch_dirs):
    """扫描所有监视目录，返回 ([(路径, mtime), ...], [失效目录, ...])"""
    found = []
    missing = []
    for d in watch_dirs:
        if not os.path.isdir(d):
            missing.append(d)
            continue
        _scan_dir(d, found)
    return found, missing


# 发次号解析：文件名里的编号（shot-3.png / shor_12.tif…），与 thomson_helper 同款
NO_PAT = re.compile(r"(?:shot|shor)[-_ ]?(\d+)", re.IGNORECASE)


def make_payload(machine_name, group, sheet_name=""):
    """sheet_name: ""=A 机默认表(实时打靶); "@date"=按打靶日期自动分表;
    其他=固定写入该表名的表（不存在 A 机自动创建）"""
    files = [{"name": os.path.basename(p),
              "folder": os.path.dirname(p),
              "mtime": mt} for p, mt in group]
    shot_time = datetime.fromtimestamp(min(mt for _p, mt in group)).strftime("%Y-%m-%d %H:%M:%S")
    nums = [int(m.group(1)) for f in files if (m := NO_PAT.search(f["name"]))]
    fields = {"no": min(nums)} if nums else {}
    # 当前靶位/离焦（重频靶系统）。优先用 thomson_helper 轮询线程写的缓存
    # （最多 30s 旧，零延迟）；缓存过期才现场查（限时，不阻塞主流程太久）。
    try:
        d = target_client.get(max_age=30, timeout=6)
        if d.get("pos"):
            fields["target_pos"] = d["pos"]
        if d.get("defocus") != "":
            fields["target_defocus"] = str(d["defocus"])
    except Exception:
        pass  # 靶系统离线时不上靶位字段，不影响打靶上报
    sn = str(sheet_name or "").strip()
    if sn == "@date":
        sn = shot_time[:10]  # 打靶日期 -> 当天日期命名的表
    payload = {"machine": machine_name, "shot_time": shot_time,
               "files": files, "fields": fields,
               "reported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    if sn:
        payload["sheet_name"] = sn
    return payload


def config_mtime():
    """两个配置文件中最新的 mtime（热重载检测用）"""
    mt = 0
    for p in (LOCAL_CONFIG_PATH, CONFIG_PATH):
        try:
            mt = max(mt, os.path.getmtime(p))
        except OSError:
            pass
    return mt


def register_silent_dirs(watch_dirs, seen, registered_dirs):
    """"首次纳入监视"的目录：静默登记其中已有的全部文件（不上报）。
    适用于启动时和运行中热更新增目录。返回本次登记的文件数。"""
    cnt = 0
    for d in watch_dirs:
        ad = os.path.abspath(d)
        if ad in registered_dirs or not os.path.isdir(ad):
            continue
        c = 0
        for root, _dirs, files in os.walk(ad):
            for fn in files:
                p = os.path.join(root, fn)
                if p in seen:
                    continue
                try:
                    seen[p] = os.path.getmtime(p)
                except OSError:
                    continue  # 文件正被写入，下一轮按新文件处理
                c += 1
        registered_dirs.add(ad)
        cnt += c
        log("目录首次纳入监视，静默登记已有文件 %d 个: %s" % (c, d))
    return cnt


def main():
    # 本机实际配置 config_b.local.json 存在时优先（模板 config_b.json 保持入库）
    cfg = load_json(LOCAL_CONFIG_PATH, None) or load_json(CONFIG_PATH, None)
    if cfg is None:
        save_json(CONFIG_PATH, {
            "watch_dirs": [r"D:\实验数据\谱仪1"],
            "server_url": "http://192.168.1.100:8765",
            "machine_name": socket.gethostname(),
            "scan_interval_sec": 1,
            "group_window_sec": 5,
        })
        print("已生成默认配置 config_b.json，请修改后重新运行。")
        sys.exit(1)

    watch_dirs = cfg["watch_dirs"]
    server_url = cfg["server_url"]
    machine_name = cfg.get("machine_name", socket.gethostname())
    interval = float(cfg.get("scan_interval_sec", 3))
    window = float(cfg.get("group_window_sec", 8))
    # 上报目标表：""=默认"实时打靶"；"@date"=按打靶日期自动分表；其他=固定表名
    sheet_name = str(cfg.get("sheet_name", "") or "").strip()
    # 上报模式："direct"=检测到打靶直接上报 A 机（旧行为）；
    # "confirm"=只送到本机 8767 上报系统待确认，人工点「确认上报」才写 A 机
    report_mode = str(cfg.get("report_mode", "direct") or "direct").strip().lower()
    helper_url = str(cfg.get("helper_url", "") or "http://127.0.0.1:8767").strip()

    seen = load_json(STATE_PATH, {})      # {路径: mtime}
    pend = load_json(PENDING_PATH, [])    # 已见但尚未归组上报的 [[路径, mtime], ...]
    pending = load_json(QUEUE_PATH, [])   # 未成功上报的 payload 列表
    if pending:
        log("发现 %d 条未上报记录，将自动补发" % len(pending))

    log("监视目录: %s" % watch_dirs)
    log("上报地址: %s  (本机名: %s)" % (server_url, machine_name))

    first_run = not os.path.exists(STATE_PATH)

    # 新增监视目录静默登记（关键修复）：
    # state 已存在时（非首次运行），config 里新出现的监视目录下的历史文件
    # 会被当成"新文件"全部上报——历史数据就被灌进日志了。
    # 因此：任何"首次纳入监视"的目录（启动时或运行中热更新增），
    # 先静默登记其已有的全部文件。
    registered_dirs = set(load_json(REG_DIRS_PATH, []))
    silent_cnt = register_silent_dirs(watch_dirs, seen, registered_dirs)
    if silent_cnt:
        save_json(STATE_PATH, seen)
        log("共静默登记 %d 个历史文件（不上报）" % silent_cnt)
    save_json(REG_DIRS_PATH, sorted(registered_dirs))
    cfg_mtime = config_mtime()

    missing_seen = {}   # {目录: 上次告警时间}，同类告警 60 秒节流
    alert_gap = 60.0

    while True:
        try:
            # 1) 补发失败队列（按 dst 标记路由：helper=上报系统，a=A 机）
            if pending:
                still = []
                for pl in pending:
                    try:
                        if pl.get("dst") == "helper":
                            send_detect(helper_url, pl)
                        else:
                            send_shot(server_url, pl)
                        log("补发成功: %s" % pl.get("shot_time"))
                    except Exception:
                        still.append(pl)
                if len(still) != len(pending):
                    pending = still
                    save_json(QUEUE_PATH, pending)

            # 1.5) 目录失效告警（P0：目录被改名/卸载时绝不能静默丢数）
            now = time.time()
            for d in watch_dirs:
                gone = not os.path.isdir(d)
                last = missing_seen.get(d, 0)
                if gone and now - last >= alert_gap:
                    msg = "监视目录不存在: %s（该目录下的新数据不会被发现！）" % d
                    log("ERROR: " + msg)
                    missing_seen[d] = now
                    send_alert(server_url, machine_name, "error", msg)
                elif not gone and d in missing_seen:
                    del missing_seen[d]
                    log("目录已恢复: %s" % d)
                    send_alert(server_url, machine_name, "info",
                               "监视目录已恢复: %s" % d)

            # 1.8) 配置热重载：8767 页面"监视目录管理"改了配置，1 秒内自动跟上
            mt = config_mtime()
            if mt != cfg_mtime:
                cfg_mtime = mt
                nc = load_json(LOCAL_CONFIG_PATH, None) or load_json(CONFIG_PATH, None)
                if nc:
                    # 上报目标表热更新
                    ns = str(nc.get("sheet_name", "") or "").strip()
                    if ns != sheet_name:
                        sheet_name = ns
                        log("配置热重载：上报目标表 -> %s"
                            % (ns or "实时打靶(默认)"))
                    # 上报模式热更新
                    nmode = str(nc.get("report_mode", "direct") or "direct").strip().lower()
                    if nmode != report_mode:
                        report_mode = nmode
                        log("配置热重载：上报模式 -> %s"
                            % ("确认后上报（8767 页面点「确认上报」）"
                               if nmode == "confirm" else "直接上报 A 机"))
                    nhu = str(nc.get("helper_url", "") or "").strip()
                    if nhu and nhu != helper_url:
                        helper_url = nhu
                if nc and nc.get("watch_dirs") and nc["watch_dirs"] != watch_dirs:
                    nd = [os.path.normpath(d) for d in nc["watch_dirs"] if d]
                    added = [d for d in nd if d not in watch_dirs]
                    gone = [d for d in watch_dirs if d not in nd]
                    watch_dirs = nd
                    # 新增（含"移除后重新添加"）的目录：一律静默登记现有文件。
                    # 不能依赖 registered_dirs 历史记忆——目录被移除监视期间
                    # 产生的新文件也属"历史数据"，重新纳入时不得上报。
                    c = register_silent_dirs(added, seen, set())
                    registered_dirs.update(os.path.abspath(d) for d in added)
                    if c:
                        save_json(STATE_PATH, seen)
                    save_json(REG_DIRS_PATH, sorted(registered_dirs))
                    log("配置热重载：新增 %s，移除 %s（静默登记 %d 个文件）"
                        % (added or "无", gone or "无", c))

            # 2) 扫描
            entries = scan_once(watch_dirs)

            if first_run:
                for p, mt in entries:
                    seen.setdefault(p, mt)
                save_json(STATE_PATH, seen)
                first_run = False
                log("首次运行：登记已有文件 %d 个（不上报）" % len(seen))
            else:
                new_entries = [(p, mt) for p, mt in entries if p not in seen]
                # pending（上次还没归组的文件）+ 本轮新文件，合并后统一重新分组
                all_new = [tuple(x) for x in pend] + new_entries
                if all_new:
                    log("本轮待处理文件 %d 个（含缓冲 %d）"
                        % (len(all_new), len(pend)))
                    done_groups, hold = group_into_shots(all_new, window)
                    # 全部登记为已见（防止重启后重复上报；A 机另有 UNIQUE 去重兜底）
                    for p, mt in all_new:
                        seen.setdefault(p, mt)
                    pend = [list(x) for x in hold]
                    save_json(STATE_PATH, seen)
                    save_json(PENDING_PATH, pend)
                    for g in done_groups:
                        pl = make_payload(machine_name, g, sheet_name)
                        if report_mode == "confirm":
                            # 确认模式：不直接写 A 机，先送本机 8767 待确认
                            pl["dst"] = "helper"
                            try:
                                send_detect(helper_url, pl)
                                log("已送上报系统待确认: %s  (%d 个文件, 首=%s)"
                                    % (pl["shot_time"], len(g),
                                       g[0][0].split(os.sep)[-1]))
                            except Exception as e:
                                log("上报系统(8767)不可达(%r)，发次暂存队列" % e)
                                pending.append(pl)
                                save_json(QUEUE_PATH, pending)
                        else:
                            try:
                                send_shot(server_url, pl)
                                log("已上报 1 次打靶: %s  (%d 个文件, 首=%s)"
                                    % (pl["shot_time"], len(g),
                                       g[0][0].split(os.sep)[-1]))
                            except Exception as e:
                                log("上报失败(%s)，已暂存队列" % e)
                                pending.append(pl)
                                save_json(QUEUE_PATH, pending)

            time.sleep(interval)
        except KeyboardInterrupt:
            log("手动停止")
            break
        except Exception as e:
            log("循环异常(继续运行): %r" % e)
            time.sleep(interval)


if __name__ == "__main__":
    main()
