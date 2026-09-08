# -*- coding: utf-8 -*-
"""
汤姆逊谱仪 · 能量上报辅助软件（B 机端）
=======================================
场景：
  B 机上的汤姆逊谱仪生成图片文件。一次打靶 → 一个（组）图片文件落盘，
  图片生成时间 = 该发次的打靶时间。实验人员离线解谱后得到"闪烁光纤能量"，
  再把能量绑定到对应发次，发给 A 机的实验日志系统。

工作方式：
  1. 监视汤姆逊图片目录，按时间窗分组：一组图片 = 一次发次，
     组内最早 mtime = 发次时间（与 b_watcher 同款分组逻辑）。
  2. 每个发次自动上报给 A 机 /api/shot（写入日志表，可关 auto_report）。
  3. 本机启动一个小页面 (http://127.0.0.1:8767)，按发次列出待填能量；
     解谱完成后填入能量点"发送"→ A 机 /api/energy 按时间窗口自动绑定
     到对应发次行（写 fiber_p_energy 列）。
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
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config_helper.json")
STATE_PATH = os.path.join(BASE, "state_helper.json")

# RLock：save_state() 在调用方已持锁时也会被调用，必须可重入
_state_lock = threading.RLock()
STATE = {"seen": {}, "pend": [], "shots": [], "queue": []}
SERVER_URL = ""
MACHINE = ""
WATCH_DIRS = []
ENERGY_FIELD = "fiber_p_energy"
MATCH_WINDOW = 15.0
AUTO_REPORT = True
# 展示/绑定状态：pending 待输入 | sent 已绑定 | no_match 无匹配 | error 发送失败


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

def scan_all(dirs):
    """扫描全部监视目录，返回 ([(路径, mtime), ...], [失效目录, ...])"""
    found, missing = [], []
    for d in dirs:
        if not os.path.isdir(d):
            missing.append(d)
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                p = os.path.join(root, fn)
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue  # 文件可能正被写入
                found.append((p, mt))
    return found, missing


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


def make_shot(group):
    names = [os.path.basename(p) for p, _mt in sorted(group, key=lambda x: x[1])]
    st = datetime.fromtimestamp(min(mt for _p, mt in group)).strftime(
        "%Y-%m-%d %H:%M:%S")
    return {"shot_time": st, "files": names, "file_count": len(group),
            "energy": "", "status": "pending", "info": "", "row_id": None}


def report_shot(shot):
    """把发次上报给 A 机（与 b_watcher 同一入口，A 端按 machine+时间+首文件去重）"""
    files = [{"name": n, "mtime": 0} for n in shot["files"]]
    payload = {"machine": MACHINE, "shot_time": shot["shot_time"],
               "files": files, "reported_at":
               datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
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
            save_state()


def monitor_loop(watch_dirs, interval, window):
    seen_first = not os.path.exists(STATE_PATH)
    if seen_first:
        with _state_lock:
            if not STATE["seen"]:
                entries, _missing = scan_all(watch_dirs)
                for p, mt in entries:
                    STATE["seen"][p] = mt
                save_state()
        log("首次运行：登记已有图片 %d 个（不上报）" % len(STATE["seen"]))
    log("监视目录: %s" % watch_dirs)
    log("日志系统: %s   本机页面: http://127.0.0.1:%d"
        % (SERVER_URL, CFG.get("helper_port", 8767)))
    missing_seen = {}      # {目录: 上次告警时间}，60 秒节流
    while True:
        try:
            retry_queue()
            entries, missing = scan_all(watch_dirs)
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
            if all_new:
                done, hold = group_into_shots(all_new, window)
                with _state_lock:
                    for p, mt in all_new:
                        STATE["seen"].setdefault(p, mt)
                    STATE["pend"] = [list(x) for x in hold]
                save_state()
                for g in done:
                    shot = make_shot(g)
                    with _state_lock:
                        STATE["shots"].insert(0, shot)
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
                                save_state()
                            log("发次上报失败(%r)，已入补发队列" % e)
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
  .tbtn{background:#2c3e50;color:#fff;border:none;border-radius:5px;padding:6px 14px;
        font-size:13px;cursor:pointer;font-family:inherit}
  .tbtn:hover{background:#3d5875}
  .tbtn.orange{background:#d35400}
  .tbtn.orange:hover{background:#e67e22}
  .st{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px}
  .st.pending{background:#f6e8c8;color:#8a6d3b}
  .st.sent{background:#d4edda;color:#256029}
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
  <span>监视目录：<b id="dirs">-</b></span>
  <span>待填能量 <b id="npending">0</b> 发</span>
  <span>绑定窗口 ±<b id="win">-</b>s</span>
  <span>能量写入列：<b id="efield">-</b></span>
</div>
<div class="wrap">
  <table>
    <thead><tr>
      <th>发次时间</th><th>图片数</th><th>图片文件</th>
      <th>闪烁光纤能量</th><th>状态</th><th>操作</th>
    </tr></thead>
    <tbody id="tb"></tbody>
  </table>
</div>
<div id="foot">图片生成时间 = 发次时间 ｜ 解谱后填入能量点"发送"，按时间窗口自动绑定 A 机日志对应发次 ｜ 能量可"重发"覆盖</div>
<div id="toast"></div>
<script>
var SHOTS = [], LASTJSON = "", T = null;
function toast(s){
  var t = document.getElementById("toast");
  t.textContent = s; t.style.display = "block";
  clearTimeout(T); T = setTimeout(function(){ t.style.display = "none"; }, 2600);
}
function stLabel(s){
  return {pending:"待输入", sent:"已绑定", no_match:"无匹配发次",
          error:"发送失败"}[s] || s;
}
function render(){
  var tb = document.getElementById("tb");
  if (!SHOTS.length){
    tb.innerHTML = "<tr><td colspan=6 class='empty'>暂未检测到发次——等待谱仪图片落盘…</td></tr>";
  } else {
    var h = "";
    SHOTS.forEach(function(s, i){
      h += "<tr data-i='" + i + "'>";
      h += "<td class='t'>" + s.shot_time + "</td>";
      h += "<td>" + s.file_count + "</td>";
      h += "<td class='files' title='" + s.files.join("  ") + "'>" +
           (s.files[0] || "-") + (s.file_count > 1 ? " 等" + s.file_count + "个" : "") + "</td>";
      h += "<td><input class='e' data-i='" + i + "' value='" +
           (s.energy || "").replace(/'/g,"&#39;") +
           "' placeholder='如 2.35' onkeydown='if(event.key===\"Enter\")send(" + i + ",false)'></td>";
      var cls = s.status || "pending";
      h += "<td><span class='st " + cls + "'>" + stLabel(cls) + "</span>" +
           (s.info ? "<div style='color:#999;font-size:11px;margin-top:2px'>" + s.info + "</div>" : "") + "</td>";
      h += "<td>";
      if (s.status === "no_match"){
        h += "<button class='tbtn orange' onclick='send(" + i + ",true)'>补录</button> ";
      } else {
        h += "<button class='tbtn' onclick='send(" + i + ",false)'>" +
             (s.status === "sent" ? "重发" : "发送") + "</button> ";
      }
      h += "</td></tr>";
    });
    tb.innerHTML = h;
  }
  var np = SHOTS.filter(function(s){ return s.status !== "sent"; }).length;
  document.getElementById("npending").textContent = np;
}
function refresh(){
  fetch("/api/local").then(function(r){ return r.json(); }).then(function(j){
    document.getElementById("dot").className = "dot ok";
    var s = JSON.stringify(j.shots);
    if (s !== LASTJSON){
      var focused = document.activeElement;
      var typing = focused && focused.classList && focused.classList.contains("e");
      if (!typing){          // 正在输入时不重绘，避免打断
        SHOTS = j.shots; LASTJSON = s; render();
      }
    }
  }).catch(function(){
    document.getElementById("dot").className = "dot bad";
  });
}
function send(i, create){
  var inp = document.querySelector("input.e[data-i='" + i + "']");
  var v = (inp ? inp.value : SHOTS[i].energy).trim();
  if (!v){ toast("先填能量再发送"); return; }
  fetch("/api/bind", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({shot_time: SHOTS[i].shot_time, energy: v, create: create})})
    .then(function(r){ return r.json(); })
    .then(function(j){
      if (j.ok && (j.matched !== undefined)){
        toast("已绑定到发次 " + j.matched_time + "（差 " + (+j.diff_sec).toFixed(1) + "s）");
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
refresh(); setInterval(refresh, 4000);
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

    def do_GET(self):
        if urlparse(self.path).path == "/":
            self._page()
        elif urlparse(self.path).path == "/api/local":
            with _state_lock:
                shots = [dict(s) for s in STATE["shots"]]
            self._send(200, json.dumps(
                {"ok": True, "shots": shots[:200], "server": SERVER_URL,
                 "queue": len(STATE["queue"])}, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def do_POST(self):
        if urlparse(self.path).path == "/api/bind":
            self.api_bind()
        else:
            self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def api_bind(self):
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
        if not energy:
            return self._send(200, json.dumps(
                {"ok": False, "error": "能量值不能为空"}))
        shot["energy"] = energy
        payload = {"shot_time": st, "energy": energy, "field": ENERGY_FIELD,
                   "machine": MACHINE, "window_sec": MATCH_WINDOW,
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
            shot["info"] = "→ %s (差%.1fs)" % (
                j.get("matched_time", ""), j.get("diff_sec", 0))
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
    global SERVER_URL, MACHINE, WATCH_DIRS, ENERGY_FIELD, MATCH_WINDOW, AUTO_REPORT, CFG
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

    st = load_json(STATE_PATH, None)
    if st:
        with _state_lock:
            STATE.update(st)
            n_pending = sum(1 for s in STATE["shots"] if s["status"] != "sent")
        log("已恢复状态: %d 条发次记录（其中 %d 条待绑定能量）"
            % (len(STATE["shots"]), n_pending))

    def _thread_exc(args):
        log("线程异常退出: %r" % args.exc_value)
    threading.excepthook = _thread_exc
    t = threading.Thread(target=monitor_loop,
                         args=(dirs, interval, window), daemon=True)
    t.start()
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        log("汤姆逊能量上报页面: http://127.0.0.1:%d" % port)
        srv.serve_forever()
    except KeyboardInterrupt:
        log("手动停止")


if __name__ == "__main__":
    main()
