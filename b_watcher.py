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
import socket
import sys
import time
import urllib.request
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
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


def scan_once(watch_dirs):
    """扫描所有监视目录，返回 [(路径, mtime), ...]（仅新出现的文件路径，不含已登记的）"""
    found = []
    for d in watch_dirs:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                p = os.path.join(root, fn)
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue  # 文件可能正被写入/刚被移走
                found.append((p, mt))
    return found


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


def scan_once(watch_dirs):
    """扫描所有监视目录（保留旧签名兼容），返回 [(路径, mtime), ...]"""
    return scan_once_status(watch_dirs)[0]


def scan_once_status(watch_dirs):
    """扫描所有监视目录，返回 ([(路径, mtime), ...], [失效目录, ...])"""
    found = []
    missing = []
    for d in watch_dirs:
        if not os.path.isdir(d):
            missing.append(d)
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                p = os.path.join(root, fn)
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue  # 文件可能正被写入/刚被移走
                found.append((p, mt))
    return found, missing


def make_payload(machine_name, group):
    files = [{"name": os.path.basename(p),
              "folder": os.path.dirname(p),
              "mtime": mt} for p, mt in group]
    shot_time = datetime.fromtimestamp(min(mt for _p, mt in group)).strftime("%Y-%m-%d %H:%M:%S")
    return {"machine": machine_name, "shot_time": shot_time,
            "files": files,
            "reported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def main():
    # 本机实际配置 config_b.local.json 存在时优先（模板 config_b.json 保持入库）
    cfg = load_json(LOCAL_CONFIG_PATH, None) or load_json(CONFIG_PATH, None)
    if cfg is None:
        save_json(CONFIG_PATH, {
            "watch_dirs": [r"D:\实验数据\谱仪1"],
            "server_url": "http://192.168.1.100:8765",
            "machine_name": socket.gethostname(),
            "scan_interval_sec": 3,
            "group_window_sec": 8,
        })
        print("已生成默认配置 config_b.json，请修改后重新运行。")
        sys.exit(1)

    watch_dirs = cfg["watch_dirs"]
    server_url = cfg["server_url"]
    machine_name = cfg.get("machine_name", socket.gethostname())
    interval = float(cfg.get("scan_interval_sec", 3))
    window = float(cfg.get("group_window_sec", 8))

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
    # 因此：任何"首次纳入监视"的目录，先静默登记其启动时已有的全部文件。
    registered_dirs = set(load_json(REG_DIRS_PATH, []))
    silent_cnt = 0
    for d in watch_dirs:
        ad = os.path.abspath(d)
        if ad in registered_dirs or not os.path.isdir(ad):
            continue
        cnt = 0
        for root, _dirs, files in os.walk(ad):
            for fn in files:
                p = os.path.join(root, fn)
                if p in seen:
                    continue
                try:
                    seen[p] = os.path.getmtime(p)
                except OSError:
                    continue  # 文件正被写入，下一轮按新文件处理
                cnt += 1
        registered_dirs.add(ad)
        silent_cnt += cnt
        log("目录首次纳入监视，静默登记已有文件 %d 个: %s" % (cnt, d))
    if silent_cnt:
        save_json(STATE_PATH, seen)
        log("共静默登记 %d 个历史文件（不上报）" % silent_cnt)
    save_json(REG_DIRS_PATH, sorted(registered_dirs))

    missing_seen = {}   # {目录: 上次告警时间}，同类告警 60 秒节流
    alert_gap = 60.0

    while True:
        try:
            # 1) 补发失败队列
            if pending:
                still = []
                for pl in pending:
                    try:
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
                        pl = make_payload(machine_name, g)
                        try:
                            send_shot(server_url, pl)
                            log("已上报 1 次打靶: %s  (%d 个文件, 首=%s)"
                                % (pl["shot_time"], len(g), g[0][0].split(os.sep)[-1]))
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
