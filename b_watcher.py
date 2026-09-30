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
    # 落盘（原来只 print，关窗即失）→ logs/b_watcher_日期.log
    if ledger is not None:
        try:
            ledger.log_line("b_watcher", msg)
        except Exception:
            pass


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
        return _split_groups_by_no(groups + [cur]), []
    return groups, cur


def _split_groups_by_no(groups):
    """连拍拆分：相机缓冲集中落盘时，连着的几发会挤进同一个安静窗口被并成
    一组。按文件名编号拆回各自发次；解析不出编号的文件挂到时间最近的编号组。"""
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


def send_tif_timeline(b_helper_url, machine_name, payload, timeout=4):
    """C 机角色：把本组 tif 的时间表（name + 浮点 mtime + folder）推给 B 机 helper。

    为什么必须有这一步：PyTPS 报能量时只给 {"filename":"shor_79.tif","energy":"19.23"}，
    **不带任何时间戳**；而 B 机监视的是 shotNN.PNG，两边文件名对不上。
    B 机只有拿到 tif 的 mtime，才能用"哪个 B机 PNG 发次时间最近"来定位这一发
    （唯一时间基准 = B机 PNG mtime）。

    失败只记日志、绝不抛出——推时间表失败不能拖垮 C 机自己的扫描/上报。"""
    try:
        files = [{"name": f.get("name", ""),
                  "folder": f.get("folder", ""),
                  "mtime": f.get("mtime", 0)}
                 for f in (payload.get("files") or [])]
        if not files:
            return False
        body = {"machine": machine_name,
                "day": str(payload.get("shot_time", ""))[:10],
                "files": files}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            b_helper_url.rstrip("/") + "/api/tif_timeline", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        r = json.loads(urllib.request.urlopen(req, timeout=timeout)
                       .read().decode("utf-8"))
        led({"ev": "tif_push", "to": b_helper_url, "machine": machine_name,
             "day": body["day"], "count": len(files), "files": files,
             "ok": bool(r.get("ok")), "bound_now": r.get("bound_now"),
             "resp": r}, day=body["day"] or None)
        log("已推 tif 时间表给 B 机: %d 条（%s）%s"
            % (len(files), body["day"],
               "，顺带补绑 %s 条暂存能量" % r.get("bound_now")
               if r.get("bound_now") else ""))
        return True
    except Exception as e:
        log("推 tif 时间表失败(%r)——不影响本机扫描，B 机将只能按 A 机时间窗匹配" % e)
        led({"ev": "tif_push", "to": b_helper_url, "machine": machine_name,
             "ok": False, "err": repr(e)},
            day=str(payload.get("shot_time", ""))[:10] or None)
        return False


# 目录扫描缓存（与 thomson_helper 同款）：{目录: (目录mtime, [(文件路径, mtime), ...], [子目录路径, ...])}
# 逐层校验目录 mtime，没变的层直接用缓存，热扫描从 ~0.4s 降到 ~0.01s
_DIR_CACHE = {}


# ---------------- 靶类型映射 → A 机日志 自动对账 ----------------
# 8767 保存映射时有即时单次同步；本函数是兜底的周期对账：
# A 机保存瞬间掉线 / 日志行先于映射产生 / 历史遗漏，都会在下一轮自动补上。

_GARBAGE_TYPES = ("nan", "none", "null", "0", "0.0")   # 一律视为"未填"
_SHEET_ID_CACHE = {}      # {日期: sheet_id}，A 机不会重建同一天的表，可缓存
_recon_state = {"last": 0.0, "err_logged": 0.0}   # 节流


def _recon_get_json(url, timeout=6):
    return json.loads(urllib.request.urlopen(url, timeout=timeout)
                      .read().decode("utf-8"))


def _recon_post_field(server_url, rid, field, value, rev, timeout=6):
    data = json.dumps({"id": rid, "field": field, "value": value,
                       "rev": rev}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        server_url.rstrip("/") + "/api/field", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=timeout)
                      .read().decode("utf-8"))


def _recon_sheet_id(server_url, day):
    """日期 → A 机日志表 id（按表名精确匹配，带缓存）"""
    if day in _SHEET_ID_CACHE:
        return _SHEET_ID_CACHE[day]
    j = _recon_get_json(server_url.rstrip("/") + "/api/sheets", timeout=8)
    sheets = j.get("sheets") if isinstance(j, dict) else j
    sheets = sheets or (j if isinstance(j, list) else [])
    for s in sheets:
        if str(s.get("name", "")) == day:
            _SHEET_ID_CACHE[day] = s.get("id")
            return _SHEET_ID_CACHE[day]
    return None


def reconcile_target_map(server_url, helper_url):
    """对账一轮（覆盖模式）：A 机当天日志行的 target_type 与映射表不一致的
    → 一律改成映射值（含补空、纠正之前映射错误的行）。
    映射表里没有该靶位时：只清 'nan' 等垃圾值，不动真实类型。
    返回补写行数；helper/A 机不可达返回 -1。"""
    try:
        j = _recon_get_json(helper_url.rstrip("/") + "/api/targetmap")
    except Exception:
        return -1
    mapping = {str(k).strip(): str(v).strip()
               for k, v in (j.get("map") or {}).items()
               if str(v or "").strip()
               and str(v).strip().lower() not in _GARBAGE_TYPES}
    day = str(j.get("date") or "").strip() or \
        datetime.now().strftime("%Y-%m-%d")
    server = server_url.rstrip("/")
    try:
        sid = _recon_sheet_id(server, day)
        if not sid:
            return 0                     # A 机还没有当天的表，无需对账
        url_rows = server + "/api/rows?sheet_id=%s&page=1&page_size=2000" % sid
        for attempt in (0, 1):           # 第 2 轮 = rev 冲突后重取整表重试
            rows = _recon_get_json(url_rows, timeout=8).get("rows") or []
            todo = []
            for r in rows:
                f = r.get("fields") or {}
                pos = str(f.get("target_pos") or "").strip()
                cur = str(f.get("target_type") or "").strip()
                if not pos:
                    continue
                t = mapping.get(pos)
                if t is not None:
                    if cur != t:         # 覆盖：不一致就改成映射值
                        todo.append((r, t))
                elif cur.lower() in ("nan", "none", "null"):
                    todo.append((r, ""))  # 映射也没有 → 只清垃圾值
            if not todo:
                return 0
            done = 0
            for r, t in todo:
                try:
                    res = _recon_post_field(server, r["id"], "target_type",
                                            t, r.get("rev"))
                    if res.get("ok"):
                        done += 1
                except Exception:
                    pass
            if done == len(todo):
                return done
            if attempt == 0 and done < len(todo):
                continue                 # 有冲突 → 重取行再补剩下的
            return done
    except Exception:
        return -1
    return 0


def maybe_reconcile(server_url, helper_url, interval_sec):
    """主循环里调用：节流到 interval_sec 一次；异常静默（只做低频日志）"""
    now = time.time()
    if now - _recon_state["last"] < interval_sec:
        return
    _recon_state["last"] = now
    n = reconcile_target_map(server_url, helper_url)
    if n > 0:
        log("靶类型对账：已自动补写 A 机日志 %d 行" % n)
    elif n == -1 and now - _recon_state["err_logged"] >= 600:
        _recon_state["err_logged"] = now   # 10 分钟最多提示一次不可达
        log("靶类型对账：helper/A 机暂不可达，下轮重试")


def scan_once(watch_dirs):
    """扫描所有监视目录（保留旧签名兼容），返回 [(路径, mtime), ...]"""
    return scan_once_status(watch_dirs)[0]


# 只监测的文件后缀（小写带点，如 {".tif", ".png"}）。
# 由配置 watch_exts 决定；为空 = 监测全部文件（旧行为）。
# 发次记录只应由图片（谱仪/TPS 落盘）触发，其他文件（日志/临时文件）
# 不得产生发次。
_SCAN_EXTS = set()


def _match_ext(name):
    if not _SCAN_EXTS:
        return True
    return os.path.splitext(name)[1].lower() in _SCAN_EXTS


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
        elif _match_ext(n):       # 后缀过滤：只监测配置的文件类型（默认只看图片）
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

# 非发次文件（谱仪目录里的记录/日志类文件）：不参与发次分组，防止刷假发次
# 规则：① 名为"发次记录*"的文件；② 所有 .txt/.log/.ini/.tmp/.csv/.xls/.xlsx/.json
# ⚠️ 这是「应急回退」用的黑名单。默认走白名单（见下），因为监视目录里还有
#    大量其他相机的 PNG（高倍靶前-*.PNG、315远场-Snapshot-*.PNG…），
#    黑名单挡不住它们 → 会被当发次并抢走编号（09-30 之乱）。
_META_FILE_RE = re.compile(
    r"发次记录[^/\\]*$|\.(txt|log|ini|tmp|csv|xls|xlsx|json)$", re.IGNORECASE)

# 白名单：只有发次命名的文件才可能建条目（与 thomson_helper 同款规则）。
# config 里可用 shot_file_patterns 覆盖；shot_file_mode="blacklist" 应急回退。
DEFAULT_SHOT_PATTERNS = [
    r"^shot[-_ ]?\d+\.(png|tif|tiff|dat|raw|jpg|jpeg|bmp)$",   # B机 shot79.PNG
    r"^shor[-_ ]?\d+\.(png|tif|tiff|dat|raw|jpg|jpeg|bmp)$",   # C机 shor_79.tif
]
_PAT_CACHE = {}
_IGNORED_SEEN = set()
_CUR_CFG = {}      # main() 里指向当前生效的 config（热重载时同步更新）


def _is_meta_file(path):
    import os as _os
    return bool(_META_FILE_RE.search(_os.path.basename(str(path))))


def _shot_filter():
    """返回 (mode, 白名单正则)；按 config 内容缓存，支持热重载。"""
    cfg = _CUR_CFG
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
    name = os.path.basename(str(path))
    mode, rx = _shot_filter()
    if mode == "blacklist":
        return not _is_meta_file(name)
    return bool(rx.match(name))


def note_ignored(files):
    """被白名单挡掉的文件：每类命名只提示一次 + 写账本（留证据）。"""
    if not files:
        return
    fresh = []
    for p, mt in files:
        name = os.path.basename(str(p))
        sig = re.sub(r"\d+", "N", name).lower()
        if sig in _IGNORED_SEEN:
            continue
        _IGNORED_SEEN.add(sig)
        fresh.append((name, sig))
    if not fresh:
        return
    for name, _sig in fresh[:5]:
        log("已忽略非发次文件: %s（不建条目；如需纳入请改 config 的 "
            "shot_file_patterns）" % name)
    if len(fresh) > 5:
        log("…另有 %d 类非发次文件被忽略" % (len(fresh) - 5))
    led({"ev": "file_ignored_w", "count": len(files),
         "kinds": [{"name": n, "pattern": s} for n, s in fresh[:20]],
         "mode": _shot_filter()[0]})


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
    # ---- 账本：b_watcher 侧发次（带 folder + 浮点 mtime，这是 helper 侧丢掉的明细）----
    led({"ev": "shot_group_w", "shot_time": shot_time, "machine": machine_name,
         "no": fields.get("no"), "target_pos": fields.get("target_pos", ""),
         "target_defocus": fields.get("target_defocus", ""),
         "sheet_name": sn, "file_count": len(files), "files": files},
        day=shot_time[:10] or None)
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
    global _CUR_CFG
    _CUR_CFG = cfg          # 白名单过滤读它（热重载时同步更新）
    interval = float(cfg.get("scan_interval_sec", 3))
    window = float(cfg.get("group_window_sec", 8))
    # 上报目标表：""=默认"实时打靶"；"@date"=按打靶日期自动分表；其他=固定表名
    sheet_name = str(cfg.get("sheet_name", "") or "").strip()
    # 上报模式："direct"=检测到打靶直接上报 A 机（旧行为）；
    # "confirm"=只送到本机 8767 上报系统待确认，人工点「确认上报」才写 A 机
    # "timeline"=**C机专用**：只把文件时间表推给 B 机，本机永不向 A 机建行
    #            （行由 B 机按 PNG mtime 建；C 机发次号无参考意义）
    report_mode = str(cfg.get("report_mode", "direct") or "direct").strip().lower()
    helper_url = str(cfg.get("helper_url", "") or "http://127.0.0.1:8767").strip()
    # C机把 tif 时间表推给哪台 B 机 helper（空 = 不推，保持旧行为）
    tif_push_url = str(cfg.get("push_tif_timeline_to", "") or "").strip()
    # 靶类型映射 → A 机日志 自动对账周期（秒），0 = 关闭
    recon_sec = float(cfg.get("map_reconcile_sec", 90) or 0)
    # 只监测的文件后缀（如 [".tif", ".png"]）；为空 = 全部文件（旧行为）
    exts = cfg.get("watch_exts") or []
    _SCAN_EXTS.update(str(e).strip().lower() for e in exts if str(e).strip())

    seen = load_json(STATE_PATH, {})      # {路径: mtime}
    pend = load_json(PENDING_PATH, [])    # 已见但尚未归组上报的 [[路径, mtime], ...]
    pending = load_json(QUEUE_PATH, [])   # 未成功上报的 payload 列表
    if pending:
        log("发现 %d 条未上报记录，将自动补发" % len(pending))

    log("监视目录: %s" % watch_dirs)
    log("监测文件类型: %s"
        % (", ".join(sorted(_SCAN_EXTS)) if _SCAN_EXTS else "全部文件"))
    log("上报地址: %s  (本机名: %s)" % (server_url, machine_name))
    log("上报模式: %s%s" % (
        {"confirm": "确认后上报（8767 页面点「确认上报」）",
         "timeline": "timeline（C机：只推时间表给 B 机，不建行）"}.get(
            report_mode, "直接上报 A 机"),
        "；tif 时间表推送 -> %s" % tif_push_url if tif_push_url else ""))

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
                    # 发次识别白名单热更新（shot_file_patterns / shot_file_mode）
                    _old_mode, _old_rx = _shot_filter()
                    _CUR_CFG = nc
                    _new_mode, _new_rx = _shot_filter()
                    if (_new_mode, _new_rx.pattern) != (_old_mode, _old_rx.pattern):
                        _DIR_CACHE.clear()     # 缓存是按旧规则过滤的结果，必须作废
                        _IGNORED_SEEN.clear()  # 让"已忽略"提示按新规则重报一次
                        log("配置热重载：发次识别 -> %s / %s"
                            % (_new_mode, _new_rx.pattern))
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
                    # C机 tif 时间表推送目标热更新（置空 = 关闭推送）
                    ntp = str(nc.get("push_tif_timeline_to", "") or "").strip()
                    if ntp != tif_push_url:
                        tif_push_url = ntp
                        log("配置热重载：tif 时间表推送 -> %s"
                            % (ntp or "关闭"))
                    # 监测文件类型热更新
                    n_exts = {str(e).strip().lower()
                              for e in (nc.get("watch_exts") or [])
                              if str(e).strip()}
                    if n_exts != _SCAN_EXTS:
                        _SCAN_EXTS.clear()
                        _SCAN_EXTS.update(n_exts)
                        _DIR_CACHE.clear()  # 缓存里是按旧后缀过滤的结果，必须作废
                        log("配置热重载：监测文件类型 -> %s"
                            % (", ".join(sorted(_SCAN_EXTS))
                               if _SCAN_EXTS else "全部文件"))
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

            # 1.9) 靶类型映射 → A 机日志 周期对账：日志行靶类型与映射
            #      不一致的自动覆盖/补齐（纠正历史错误映射 + 补空）
            if recon_sec > 0:
                maybe_reconcile(server_url, helper_url, recon_sec)

            # 2) 扫描
            entries = scan_once(watch_dirs)

            if first_run:
                for p, mt in entries:
                    seen.setdefault(p, mt)
                save_json(STATE_PATH, seen)
                first_run = False
                log("首次运行：登记已有文件 %d 个（不上报）" % len(seen))
            else:
                new_entries = []
                ignored = []
                for p, mt in entries:
                    if p in seen:
                        continue
                    if is_shot_file(p):        # 白名单：只有发次命名才进分组
                        new_entries.append((p, mt))
                    else:
                        ignored.append((p, mt))
                        seen[p] = mt           # 只登记，绝不建条目/参与补号
                if ignored:
                    note_ignored(ignored)
                    save_json(STATE_PATH, seen)
                # pending（上次还没归组的文件）+ 本轮新文件，合并后统一重新分组
                all_new = [tuple(x) for x in pend if is_shot_file(x[0])] \
                    + new_entries
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
                        # ---- C机角色：把本组 tif 时间表推给 B 机（集中匹配）----
                        # PyTPS 的能量只带 tif 文件名不带时间；B 机拿到 tif 的
                        # 浮点 mtime 后才能"找时间最近的 B机 shot PNG 发次"。
                        # 失败只记日志，绝不阻塞上报主流程。
                        if tif_push_url:
                            send_tif_timeline(tif_push_url, machine_name, pl)
                        if report_mode == "timeline":
                            # C机专用：时间表已推，本机不向 A 机建行、不送待确认
                            log("timeline 模式：只推时间表，不建行: %s (%d 个文件)"
                                % (pl["shot_time"], len(g)))
                        elif report_mode == "confirm":
                            # 确认模式：不直接写 A 机，先送本机 8767 待确认
                            pl["dst"] = "helper"
                            try:
                                send_detect(helper_url, pl)
                                log("已送上报系统待确认: %s  (%d 个文件, 首=%s)"
                                    % (pl["shot_time"], len(g),
                                       g[0][0].split(os.sep)[-1]))
                                led({"ev": "report_send", "dst": "helper",
                                     "shot_time": pl["shot_time"],
                                     "first_file": (pl.get("files") or [{}])[0].get("name"),
                                     "file_count": len(g), "ok": True, "err": ""},
                                    day=pl["shot_time"][:10] or None)
                            except Exception as e:
                                log("上报系统(8767)不可达(%r)，发次暂存队列" % e)
                                pending.append(pl)
                                save_json(QUEUE_PATH, pending)
                                led({"ev": "report_send", "dst": "helper",
                                     "shot_time": pl["shot_time"],
                                     "first_file": (pl.get("files") or [{}])[0].get("name"),
                                     "file_count": len(g), "ok": False,
                                     "err": repr(e), "queued": True},
                                    day=pl["shot_time"][:10] or None)
                        else:
                            try:
                                send_shot(server_url, pl)
                                log("已上报 1 次打靶: %s  (%d 个文件, 首=%s)"
                                    % (pl["shot_time"], len(g),
                                       g[0][0].split(os.sep)[-1]))
                                led({"ev": "report_send", "dst": "a",
                                     "shot_time": pl["shot_time"],
                                     "first_file": (pl.get("files") or [{}])[0].get("name"),
                                     "file_count": len(g), "ok": True, "err": ""},
                                    day=pl["shot_time"][:10] or None)
                            except Exception as e:
                                log("上报失败(%s)，已暂存队列" % e)
                                pending.append(pl)
                                save_json(QUEUE_PATH, pending)
                                led({"ev": "report_send", "dst": "a",
                                     "shot_time": pl["shot_time"],
                                     "first_file": (pl.get("files") or [{}])[0].get("name"),
                                     "file_count": len(g), "ok": False,
                                     "err": repr(e), "queued": True},
                                    day=pl["shot_time"][:10] or None)

            time.sleep(interval)
        except KeyboardInterrupt:
            log("手动停止")
            break
        except Exception as e:
            log("循环异常(继续运行): %r" % e)
            time.sleep(interval)


if __name__ == "__main__":
    main()
