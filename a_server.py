# -*- coding: utf-8 -*-
"""
A机端 - 实验打靶日志系统（主控机）
==================================
架构：前后端分离
  后端  : HTTP JSON API + SQLite（表格 sheets / 记录 shots 两级结构）
  前端  : 单页应用（原生 JS，服务端只吐一个 HTML 壳，数据全走 API）

功能
  - 多表格管理：每个实验日/实验轮次一张表，可新建 / 重命名 / 删除
  - 记录浏览：服务端搜索、排序、分页
  - 记录编辑：WPS 风格单元格直编（悬浮输入框，表格不缩放）
  - 行级操作：手动新增行、勾选批量删除、单行删除
  - B 机上报：/api/shot 自动写入"实时打靶"表（可指定 sheet_name）
  - 导出：当前表 xlsx / CSV；全部表 xlsx（每表一个工作表）

依赖：标准库运行；xlsx 导出需 openpyxl（可选）。
运行： python a_server.py
"""

import csv
import html
import io
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT") or 8765)  # 8765 被占用时可用 PORT=8766 启动
DB_PATH = os.path.join(BASE, "shots.db")
COLS_PATH = os.path.join(BASE, "shotlist_cols.json")
LIVE_SHEET = "实时打靶"          # B 机上报默认写入的表
SORTABLE_SQL = {"shot_time", "machine", "file_count", "first_file", "id"}

DEFAULT_COLS = [{"key": "target_type", "name": "靶类型", "width": 110},
                {"key": "note", "name": "备注", "width": 180}]

try:
    with open(COLS_PATH, "r", encoding="utf-8") as f:
        COLS = json.load(f)
except Exception:
    COLS = DEFAULT_COLS
COL_KEYS = {c["key"] for c in COLS}

# 访问白名单（config_a.json）：空 = 不限制；非空 = 只允许列表里的 IP/网段前缀
try:
    with open(os.path.join(BASE, "config_a.json"), "r", encoding="utf-8") as f:
        _CFG_A = json.load(f)
except Exception:
    _CFG_A = {}
ALLOW_IPS = [str(x) for x in _CFG_A.get("allow_ips", [])]

_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """建库 + 旧库迁移（sheets 表 / sheet_id 列 / 去 UNIQUE 约束）"""
    with _db_lock, db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sheets (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL UNIQUE,
                exp_date   TEXT DEFAULT '',
                note       TEXT DEFAULT '',
                created_at TEXT
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                shot_time   TEXT NOT NULL,
                machine     TEXT,
                file_count  INTEGER,
                first_file  TEXT,
                folder      TEXT,
                fields      TEXT DEFAULT '{}',
                reported_at TEXT,
                created_at  TEXT
            )""")
        for col, ddl in (("fields", "TEXT DEFAULT '{}'"),
                         ("sheet_id", "INTEGER"),
                         ("rev", "INTEGER DEFAULT 1")):
            try:
                conn.execute("ALTER TABLE shots ADD COLUMN %s %s" % (col, ddl))
            except sqlite3.OperationalError:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trash (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                orig_id    INTEGER,
                sheet_id   INTEGER,
                sheet_name TEXT,
                shot_time  TEXT,
                machine    TEXT,
                file_count INTEGER,
                first_file TEXT,
                folder     TEXT,
                fields     TEXT DEFAULT '{}',
                reported_at TEXT,
                created_at TEXT,
                deleted_at TEXT
            )""")
        # 旧库迁移：去掉 UNIQUE(machine, shot_time)——同秒多发打靶会被静默吞掉；
        # 去重改为 api_shot 里的显式检查（machine+shot_time+first_file）
        uniq = [r for r in conn.execute("PRAGMA index_list(shots)")
                if r["origin"] == "u"]
        if uniq:
            conn.execute("ALTER TABLE shots RENAME TO shots_old")
            conn.execute("""
                CREATE TABLE shots (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    shot_time   TEXT NOT NULL,
                    machine     TEXT,
                    file_count  INTEGER,
                    first_file  TEXT,
                    folder      TEXT,
                    fields      TEXT DEFAULT '{}',
                    reported_at TEXT,
                    created_at  TEXT,
                    sheet_id    INTEGER
                )""")
            conn.execute("""
                INSERT INTO shots
                SELECT id, shot_time, machine, file_count, first_file,
                       folder, fields, reported_at, created_at, sheet_id
                FROM shots_old""")
            conn.execute("DROP TABLE shots_old")
        # 旧数据迁移：把没有归属的记录挂到一张历史表
        n_free = conn.execute(
            "SELECT COUNT(*) c FROM shots WHERE sheet_id IS NULL").fetchone()["c"]
        if n_free:
            first = conn.execute(
                "SELECT MIN(shot_time) t FROM shots WHERE sheet_id IS NULL"
            ).fetchone()["t"] or ""
            day = first[:10] if len(first) >= 10 else "历史数据"
            name = day if not conn.execute(
                "SELECT 1 FROM sheets WHERE name=?", (day,)).fetchone() else "历史数据"
            cur = conn.execute(
                "INSERT INTO sheets(name, exp_date, note, created_at) VALUES (?,?,?,?)",
                (name, day, "系统升级时自动归档的旧记录",
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.execute("UPDATE shots SET sheet_id=? WHERE sheet_id IS NULL",
                         (cur.lastrowid,))
        # 预建实时表
        if not conn.execute("SELECT 1 FROM sheets WHERE name=?", (LIVE_SHEET,)).fetchone():
            conn.execute(
                "INSERT INTO sheets(name, exp_date, note, created_at) VALUES (?,?,?,?)",
                (LIVE_SHEET, datetime.now().strftime("%Y-%m-%d"),
                 "B 机上报的打靶记录自动写入此表",
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


BACKUP_DIR = os.path.join(BASE, "backup")
BACKUP_KEEP = 7          # 保留最近 7 份
BACKUP_INTERVAL = 6 * 3600   # 每 6 小时自动备份一次


def backup_now():
    """用 SQLite backup API 做一致性快照，超量淘汰最旧的"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    fname = "shots_%s.db" % datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, fname)
    with _db_lock, db() as src, sqlite3.connect(dst) as dstc:
        src.backup(dstc)
    olds = sorted(os.listdir(BACKUP_DIR))
    for name in olds[:-BACKUP_KEEP]:
        try:
            os.remove(os.path.join(BACKUP_DIR, name))
        except OSError:
            pass
    return dst


def _backup_loop():
    while True:
        time.sleep(BACKUP_INTERVAL)
        try:
            p = backup_now()
            print("[backup] 已备份: %s" % p, flush=True)
        except Exception as e:
            print("[backup] 备份失败: %r" % e, flush=True)


def get_sheet(conn, name):
    return conn.execute("SELECT * FROM sheets WHERE name=?", (name,)).fetchone()


def sort_key(v):
    """排序键：数字按数值，其余按小写字符串，空值排最后"""
    if v is None or v == "":
        return (2, 0, "")
    try:
        return (0, float(v), "")
    except (TypeError, ValueError):
        return (1, 0, str(v).lower())


def _order_clause(sort, direction):
    """SQL 排序子句。sort 已在调用方校验过白名单；语义与 sort_key 对齐：
    纯数值按数值排、文本按字典序、空值排最后。"""
    d = "DESC" if direction == "DESC" else "ASC"
    if sort in ("id", "machine", "file_count", "first_file"):
        return "ORDER BY %s %s, id %s" % (sort, d, d)
    if sort in COL_KEYS:
        # key 来自 shotlist_cols.json 且已过 COL_KEYS 白名单，无注入面
        e = ("json_extract(CASE WHEN json_valid(fields) THEN fields ELSE '{}' END,"
             "'$.%s')" % sort)
        return ("ORDER BY"
                " CASE WHEN {e} IS NULL OR TRIM(CAST({e} AS TEXT))='' THEN 2"
                " WHEN CAST({e} AS TEXT) GLOB '*[^0-9.eE+-]*' THEN 1"
                " ELSE 0 END {d},"
                " CAST({e} AS REAL) {d}, CAST({e} AS TEXT) {d}, id {d}").format(e=e, d=d)
    return "ORDER BY shot_time %s, id %s" % (d, d)


NUM_RE = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?$")


def norm_shot_time(s):
    """时间格式校验/归一化：接受 日期、日期+时分、日期+时分秒（/ 或 - 分隔），
    统一返回 YYYY-MM-DD HH:MM:SS"""
    s = str(s or "").strip()
    if not s:
        raise ValueError("时间不能为空")
    s2 = s.replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s2, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise ValueError("时间格式应为 YYYY-MM-DD HH:MM:SS（收到: %s）" % s)


def to_number(v):
    """导出用：纯数字字符串才转数值，避免 '1_000'、'1e3类伪数值' 误转"""
    if isinstance(v, str) and NUM_RE.match(v.strip()) and v.strip():
        try:
            f = float(v)
            return int(f) if f == int(f) and "." not in v and "e" not in v.lower() else f
        except (ValueError, OverflowError):
            return v
    return v


# ---------------- 前端页面（单页应用外壳） ----------------

PAGE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>实验打靶日志系统</title>
<style>
  *{box-sizing:border-box}
  body{font-family:"Microsoft YaHei",sans-serif;margin:0;background:#f0f2f5;color:#222;
       display:flex;height:100vh;overflow:hidden}
  /* ---------- 侧栏：表格列表 ---------- */
  aside{width:225px;background:#243342;color:#dfe6ee;display:flex;flex-direction:column;flex-shrink:0}
  .logo{padding:16px 16px 12px;font-size:16px;font-weight:bold;border-bottom:1px solid #31445a}
  .logo small{display:block;font-weight:normal;color:#8fa4bb;font-size:11px;margin-top:3px}
  #sheetlist{flex:1;overflow:auto;padding:8px 0}
  .sitem{padding:9px 16px;cursor:pointer;font-size:13px;display:flex;justify-content:space-between;
         align-items:center;border-left:3px solid transparent}
  .sitem:hover{background:#2c3e50}
  .sitem.on{background:#2c3e50;border-left-color:#e67e22;color:#fff}
  .sitem .cnt{background:#3a4f66;border-radius:10px;padding:1px 8px;font-size:11px;color:#bcd0e4}
  .sitem .d{color:#8fa4bb;font-size:11px;margin-left:6px}
  aside .foot{padding:10px;border-top:1px solid #31445a}
  .fbtn{display:block;width:100%;background:#2e4a68;color:#fff;border:none;border-radius:5px;
        padding:8px;font-size:13px;margin-top:6px;cursor:pointer;text-align:center;text-decoration:none}
  .fbtn:hover{background:#3a5a80}
  .fbtn.orange{background:#d35400}
  .fbtn.orange:hover{background:#e67e22}
  /* ---------- 主区 ---------- */
  main{flex:1;display:flex;flex-direction:column;overflow:hidden}
  #head{background:#fff;padding:12px 20px 10px;border-bottom:1px solid #e2e4e8}
  #head h1{font-size:18px;margin:0;display:inline-block}
  #head .meta{color:#888;font-size:12px;margin-left:12px}
  #head button{background:none;border:1px solid #ccc;border-radius:4px;font-size:12px;
               padding:2px 10px;cursor:pointer;color:#666;margin-left:8px}
  .toolbar{background:#fff;padding:9px 20px;border-bottom:1px solid #e2e4e8;display:flex;
           align-items:center;gap:8px;flex-wrap:wrap}
  .toolbar input{padding:6px 10px;border:1px solid #d5d9de;border-radius:5px;width:230px;font-size:13px}
  .toolbar select{padding:6px;border:1px solid #d5d9de;border-radius:5px;font-size:13px}
  .tbtn{background:#2c3e50;color:#fff;border:none;border-radius:5px;padding:7px 14px;
        font-size:13px;cursor:pointer}
  .tbtn:hover{background:#3d5875}
  .tbtn.red{background:#c0392b}
  .tbtn.red:hover{background:#e74c3c}
  .tbtn.green{background:#27ae60}
  .tbtn.green:hover{background:#2ecc71}
  #info{color:#888;font-size:12px;margin-left:auto}
  .wrap{flex:1;overflow:auto;background:#fff;margin:0}
  table{border-collapse:separate;border-spacing:0;width:max-content;min-width:100%;font-size:13px}
  th{background:#2c3e50;color:#fff;padding:8px 10px;text-align:left;font-weight:normal;
     white-space:nowrap;position:sticky;top:0;z-index:3;cursor:pointer;user-select:none}
  th:hover{background:#3d5875}
  th .arr{color:#e67e22;margin-left:3px}
  th.fh{padding:4px 6px}
  th.fh input{width:100%;box-sizing:border-box;padding:3px 6px;
    border:1px solid #5a7a9a;border-radius:3px;font-size:11px;background:#3a5068;color:#fff}
  th.fh input::placeholder{color:#9db4cc}
  td{border-bottom:1px solid #eceef1;border-right:1px solid #f2f3f5;padding:6px 10px;
     white-space:nowrap;vertical-align:middle}
  tr:hover td{background:#f2f7ff}
  .t{font-family:Consolas,monospace}
  td.ed{cursor:text;position:relative;background:#fff}
  td.ed:hover{background:#eef5ff;box-shadow:inset 0 0 0 1px #9fc3e8;z-index:1}
  td.ed:empty::before{content:attr(data-ph);color:#c0c4cc}
  td.ed input{position:absolute;left:0;top:0;width:100%;height:100%;box-sizing:border-box;
    border:none;outline:none;font:inherit;background:#fff8dc;padding:6px 10px;margin:0}
  td.ck{text-align:center;width:34px}
  td.op button{background:none;border:none;color:#c0392b;cursor:pointer;font-size:12px}
  .empty{padding:50px;text-align:center;color:#999}
  .pager{background:#fff;border-top:1px solid #e2e4e8;padding:8px 20px;display:flex;
         align-items:center;gap:12px;font-size:13px}
  .pager button{padding:5px 14px;border:1px solid #d5d9de;background:#fff;border-radius:5px;
                cursor:pointer;font-size:13px}
  .pager button:disabled{color:#bbb;cursor:default}
  .toast{position:fixed;top:18px;left:50%;transform:translateX(-50%);background:#2c3e50;color:#fff;
         padding:8px 22px;border-radius:20px;font-size:13px;z-index:99;box-shadow:0 2px 8px rgba(0,0,0,.25)}
  #banner{background:#c0392b;color:#fff;padding:7px 20px;font-size:13px;display:none;white-space:pre-wrap}
  #banner.info{background:#d48806}
  .mask{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:50;display:none;
        align-items:center;justify-content:center}
  .panel{background:#fff;border-radius:8px;width:860px;max-width:92vw;max-height:82vh;
         display:flex;flex-direction:column;box-shadow:0 8px 30px rgba(0,0,0,.25)}
  .panel .ph{padding:12px 18px;border-bottom:1px solid #e5e7ea;font-size:15px;display:flex;
             align-items:center}
  .panel .ph button{margin-left:auto}
  .panel .pb{overflow:auto;padding:0 0 10px}
  .panel table{border-collapse:collapse;width:100%;font-size:12px}
  .panel th{background:#f2f4f6;padding:7px 10px;text-align:left;position:static}
  .panel td{border-bottom:1px solid #eef0f2;padding:6px 10px;white-space:nowrap}
  .panel .lk{color:#2471a3;cursor:pointer;margin-right:10px}
  .panel .lk.red{color:#c0392b}
  .panel .empty{padding:30px;text-align:center;color:#999}
  @media print{
    body{display:block;height:auto;overflow:visible;background:#fff}
    aside,.toolbar,.pager,#banner,.mask,.toast{display:none !important}
    #head{border:none;padding:4px 0}
    .wrap{overflow:visible}
    table{width:100%;font-size:11px}
    th{background:#eee !important;color:#000 !important;position:static}
    .frow,th.ck,td.ck,td.op,th:last-child{display:none}
  }
</style>
</head>
<body>
<aside>
  <div class="logo">打靶日志系统<small>BLAC 实验数据 · 内网</small></div>
  <div id="sheetlist"></div>
  <div class="foot">
    <button class="fbtn orange" onclick="newSheet()">＋ 新建表格</button>
    <button class="fbtn" onclick="openTrash()">回收站</button>
    <a class="fbtn" href="/export.xlsx" id="exportAll">导出全部 (xlsx)</a>
  </div>
</aside>
<main>
  <div id="banner"></div>
  <div id="head">
    <h1 id="sheetName">-</h1><span class="meta" id="sheetMeta"></span>
    <button onclick="renameSheet()">重命名</button>
  </div>
  <div class="toolbar">
    <input id="q" placeholder="搜索：时间 / 靶类型 / 任意列内容…" oninput="qChange()">
    <select id="psize" onchange="psizeChange()">
      <option>50</option><option>100</option><option>200</option><option>500</option>
    </select>
    <button class="tbtn green" onclick="addRow()">＋ 新增行</button>
    <button class="tbtn red" onclick="delSelected()">删除选中</button>
    <button class="tbtn" onclick="doExport('xlsx')">导出 Excel</button>
    <button class="tbtn" onclick="doExport('csv')">导出 CSV</button>
    <button class="tbtn" onclick="window.print()" title="打印当前页表格">打印</button>
    <span id="info"></span>
  </div>
  <div class="wrap" id="wrap"></div>
  <div class="pager">
    <button id="prev" onclick="pageMove(-1)">‹ 上一页</button>
    <span id="pginfo"></span>
    <button id="next" onclick="pageMove(1)">下一页 ›</button>
  </div>
</main>
<div class="mask" id="trashMask" onclick="if(event.target===this)closeTrash()">
  <div class="panel">
    <div class="ph">回收站（删除的记录在这里，可恢复）
      <button class="tbtn red" onclick="purgeTrash()">清空回收站</button>
      <button onclick="closeTrash()">关闭</button></div>
    <div class="pb" id="trashBody"></div>
  </div>
</div>
<script>
var S = {sheets: [], cur: null, page: 1, q: "", f: {}, ftimer: null,
         sort: "", dir: "asc", timer: null, editing: false, checked: new Set()};
var COL_OPTS = {};

function toast(m){
  var t = document.createElement("div"); t.className = "toast"; t.textContent = m;
  document.body.appendChild(t); setTimeout(function(){t.remove();}, 1800);
}
function api(path, body, cb, fail){
  fetch(path, body ? {method:"POST", cache:"no-store", headers:{"Content-Type":"application/json"},
        body: JSON.stringify(body)} : {cache:"no-store"})
    .then(function(r){ return r.json().then(function(j){
        if(!j.ok && !j.conflict) throw j.error||"请求失败"; return j; }); })
    .then(cb).catch(function(e){ toast("出错: " + e); if (fail) fail(e); });
}
/* ---------- 勾选状态（跨自动刷新保留） ---------- */
function syncCkall(){
  var ck = document.getElementById("ckall"); if (!ck) return;
  var boxes = document.querySelectorAll("#tb td.ck input"), n = 0;
  boxes.forEach(function(c){ if (c.checked) n++; });
  ck.checked = boxes.length > 0 && n === boxes.length;
  ck.indeterminate = n > 0 && n < boxes.length;
}
/* 行复选框变化 → 记入 S.checked（事件委托，重渲染后依然有效） */
document.addEventListener("change", function(e){
  if (e.target && e.target.matches && e.target.matches("#tb td.ck input")){
    var tr = e.target.closest("tr");
    if (!tr || !tr.dataset.id) return;
    if (e.target.checked) S.checked.add(+tr.dataset.id);
    else S.checked.delete(+tr.dataset.id);
    syncCkall();
  }
});

/* ---------- 状态持久化（URL hash）：刷新后回到原表格/页码/搜索/排序 ---------- */
function saveHash(){
  var fkeys = Object.keys(S.f).filter(function(k){return S.f[k];});
  var h = "#sheet=" + (S.cur||"") + "&page=" + S.page + "&q=" + encodeURIComponent(S.q) +
          "&sort=" + encodeURIComponent(S.sort) + "&dir=" + S.dir +
          "&f=" + encodeURIComponent(JSON.stringify(
            fkeys.reduce(function(o,k){o[k]=S.f[k];return o;},{})));
  if (location.hash !== h) history.replaceState(null, "", h);
}
function loadHash(){
  var m = /sheet=(\d+)/.exec(location.hash); if (m) S.cur = +m[1];
  m = /page=(\d+)/.exec(location.hash);      if (m) S.page = +m[1];
  m = /q=([^&]*)/.exec(location.hash);       if (m) { S.q = decodeURIComponent(m[1]);
                                                       document.getElementById("q").value = S.q; }
  m = /sort=([^&]*)/.exec(location.hash);    if (m) S.sort = decodeURIComponent(m[1]);
  m = /dir=(asc|desc)/.exec(location.hash);  if (m) S.dir = m[1];
  m = /f=([^&]*)/.exec(location.hash);
  if (m){ try{ S.f = JSON.parse(decodeURIComponent(m[1])) || {}; }catch(_){ S.f = {}; } }
}

/* ---------- 表格（实验日）列表 ---------- */
function loadSheets(){
  api("/api/sheets", null, function(j){
    S.sheets = j.sheets;
    if (!S.cur || !j.sheets.some(function(x){return x.id===S.cur;}))
      S.cur = j.sheets.length ? j.sheets[0].id : null;
    renderSheets(); loadRows();
  });
}
function renderSheets(){
  var el = document.getElementById("sheetlist"); el.innerHTML = "";
  S.sheets.forEach(function(s){
    var d = document.createElement("div");
    d.className = "sitem" + (s.id === S.cur ? " on" : "");
    d.innerHTML = "<span>" + esc(s.name) +
      (s.exp_date ? " <span class='d'>" + esc(s.exp_date) + "</span>" : "") +
      "</span><span class='cnt'>" + s.count + "</span>";
    d.onclick = function(){ if (S.cur !== s.id){ S.cur = s.id; S.page = 1; S.q = "";
      document.getElementById("q").value = ""; S.sort = ""; S.dir = "asc"; S.checked.clear();
      renderSheets(); loadRows(); } };
    el.appendChild(d);
  });
}
function newSheet(){
  var name = prompt("新表格名称（建议用实验日期，如 2026-09-07）：", todayStr());
  if (!name) return;
  var note = prompt("备注（可空）：", "") || "";
  api("/api/sheets", {name:name.trim(), note:note}, function(j){
    S.cur = j.id; S.checked.clear(); loadSheets(); toast("表格已创建");
  });
}
function renameSheet(){
  var s = S.sheets.filter(function(x){return x.id===S.cur;})[0]; if (!s) return;
  var name = prompt("修改表格名称：", s.name); if (!name) return;
  api("/api/sheets/update", {id:s.id, name:name.trim()}, function(j){
    loadSheets(); toast("已重命名");
  });
}
function delSheet(){
  var s = S.sheets.filter(function(x){return x.id===S.cur;})[0]; if (!s) return;
  if (!confirm("删除表格【" + s.name + "】及其全部 " + s.count + " 条记录？\n（记录会进入回收站，可恢复；表格本身不可恢复）")) return;
  api("/api/sheets/delete", {id:s.id}, function(j){
    S.cur = null; S.checked.clear(); loadSheets(); toast("已删除（记录可在回收站恢复）");
  });
}
function todayStr(){
  var d = new Date();
  return d.getFullYear() + "-" + ("0"+(d.getMonth()+1)).slice(-2) + "-" + ("0"+d.getDate()).slice(-2);
}
function esc(s){ return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

/* ---------- 记录列表 ---------- */
function qChange(){ clearTimeout(S.timer); S.timer = setTimeout(function(){ S.q = document.getElementById("q").value.trim(); S.page = 1; S.checked.clear(); loadRows(); }, 300); }
function psizeChange(){ S.page = 1; S.checked.clear(); loadRows(); }
function pageMove(d){ S.page += d; S.checked.clear(); loadRows(); }

function loadRows(){
  if (!S.cur) { document.getElementById("wrap").innerHTML = ""; saveHash(); return; }
  var ps = document.getElementById("psize").value;
  var url = "/api/rows?sheet_id=" + S.cur + "&page=" + S.page + "&page_size=" + ps +
            "&q=" + encodeURIComponent(S.q) + "&sort=" + S.sort + "&dir=" + S.dir;
  Object.keys(S.f).forEach(function(k){
    if (S.f[k]) url += "&f_" + k + "=" + encodeURIComponent(S.f[k]);
  });
  var af = document.activeElement, afKey = (af && af.dataset) ? af.dataset.f : null;
  api(url, null, function(j){
    S.page = j.page;
    var keep = document.getElementById("wrap").scrollTop;  // 保留滚动位置（自动刷新用）
    renderHead(); renderRows(j.rows);
    document.getElementById("wrap").scrollTop = keep;
    if (afKey){   // 恢复筛选输入框焦点与光标位置
      var el = document.querySelector(".frin[data-f='" + afKey + "']");
      if (el){ el.focus(); try{ el.setSelectionRange(el.value.length, el.value.length); }catch(_){} }
    }
    var from = j.total ? (j.page-1)*j.page_size + 1 : 0;
    var to = Math.min(j.total, j.page*j.page_size);
    document.getElementById("info").textContent = "共 " + j.total + " 条";
    document.getElementById("pginfo").textContent =
      j.total ? ("第 " + from + "–" + to + " 条 / 共 " + j.total + " 条 ｜ 第 " + j.page + "/" + j.pages + " 页") : "无记录";
    document.getElementById("prev").disabled = j.page <= 1;
    document.getElementById("next").disabled = j.page >= j.pages;
    var s = S.sheets.filter(function(x){return x.id===S.cur;})[0];
    if (s){
      document.getElementById("sheetName").textContent = s.name;
      document.getElementById("sheetMeta").textContent =
        (s.exp_date ? "实验日期 " + s.exp_date + " ｜ " : "") +
        (s.note ? s.note + " ｜ " : "") + s.count + " 条记录";
    }
    bindEdit();
    saveHash();
  });
}
function renderHead(){
  var h = "<table><thead><tr><th class='ck'><input type=checkbox id=ckall></th><th data-k='shot_time'>时间</th>";
  COLS.forEach(function(c){ h += "<th data-k='" + c.key + "'>" + esc(c.name) + "</th>"; });
  h += "<th data-k='machine'>来源</th><th data-k='file_count'>文件数</th><th data-k='first_file'>首个文件</th><th>操作</th></tr>";
  /* 列级筛选行 */
  h += "<tr class='frow'><th class='ck'></th>";
  ["shot_time"].concat(COLS.map(function(c){return c.key;})).concat(["machine","first_file"]).forEach(function(k){
    h += "<th class='fh'><input class='frin' data-f='" + k + "' placeholder='筛选' value='" +
         esc(S.f[k]||"") + "'></th>";
  });
  h += "<th></th></tr></thead><tbody id=tb></tbody></table>";
  document.getElementById("wrap").innerHTML = h;
  document.querySelectorAll("th[data-k]").forEach(function(th){
    if (th.dataset.k === S.sort) th.innerHTML += "<span class='arr'>" + (S.dir==="asc"?"▲":"▼") + "</span>";
    th.onclick = function(){
      if (S.sort === th.dataset.k) S.dir = S.dir==="asc" ? "desc" : "asc";
      else { S.sort = th.dataset.k; S.dir = "asc"; }
      S.page = 1; S.checked.clear(); loadRows();
    };
  });
  document.querySelectorAll(".frin").forEach(function(inp){
    inp.addEventListener("input", function(){
      clearTimeout(S.ftimer);
      S.ftimer = setTimeout(function(){
        S.f[inp.dataset.f] = inp.value.trim(); S.page = 1; S.checked.clear(); loadRows();
      }, 400);
    });
    inp.addEventListener("keydown", function(e){
      if (e.key === "Enter"){ clearTimeout(S.ftimer);
        S.f[inp.dataset.f] = inp.value.trim(); S.page = 1; S.checked.clear(); loadRows(); }
    });
  });
  document.getElementById("ckall").onchange = function(){
    var on = this.checked;
    document.querySelectorAll("#tb td.ck input").forEach(function(c){
      c.checked = on;
      var id = +c.closest("tr").dataset.id;
      if (on) S.checked.add(id); else S.checked.delete(id);
    });
    this.indeterminate = false;
  };
}
function renderRows(rows){
  var tb = document.getElementById("tb");
  if (!rows.length){ tb.innerHTML = "<tr><td colspan=" + (COLS.length + 6) +
    " class='empty'>没有匹配的记录</td></tr>";
    var ck0 = document.getElementById("ckall");
    if (ck0){ ck0.checked = false; ck0.indeterminate = false; }
    return; }
  var h = "";
  rows.forEach(function(r){
    h += "<tr data-id='" + r.id + "' data-rev='" + (r.rev||1) + "'><td class='ck'><input type=checkbox></td>";
    h += "<td class='t ed' data-f='shot_time' data-ph='点击填写'>" + esc(r.shot_time) + "</td>";
    COLS.forEach(function(c){
      h += "<td class='ed' style='min-width:" + c.width + "px' data-f='" + c.key +
           "' data-ph='点击填写'>" + esc(r.fields[c.key] || "") + "</td>";
    });
    h += "<td>" + esc(r.machine || "-") + "</td><td>" + (r.file_count||0) + "</td>";
    h += "<td class='t' title='" + esc(r.folder||"") + "'>" + esc(r.first_file || "-") + "</td>";
    h += "<td class='op'><button onclick=delRow(" + r.id + ")>删除</button></td></tr>";
  });
  tb.innerHTML = h;
  /* 恢复勾选状态（8 秒自动刷新重渲染后勾选不丢） */
  document.querySelectorAll("#tb td.ck input").forEach(function(c){
    var tr = c.closest("tr");
    if (tr && S.checked.has(+tr.dataset.id)) c.checked = true;
  });
  syncCkall();
}
function edCells(){ return Array.prototype.slice.call(document.querySelectorAll("td.ed")); }
/* 按行串行的单元格写入队列：同一条记录的多次写按顺序执行，
   每次都带该行最新 rev，避免并发写被乐观锁误判冲突 */
var rowQ = {};
function setCell(cell, v){
  var tr = cell.closest("tr");
  var key = tr.dataset.id;
  var next = function(){   // 队列推进：成功/失败/冲突都要走，否则同行后续写入全部卡死
    var q = rowQ[key];
    if (q && q.length) q.shift()(); else delete rowQ[key];
  };
  var task = function(){
    cell.textContent = v;
    api("/api/field", {id:+key, field:cell.dataset.f, value:v, rev:+tr.dataset.rev||1},
        function(j){
          if (j.conflict){ cell.textContent = j.value; tr.dataset.rev = j.rev; }
          else tr.dataset.rev = j.rev;
          next();
        }, next);
  };
  if (rowQ[key]) rowQ[key].push(task);
  else { rowQ[key] = []; task(); }
}
function bindEdit(){
  var pitch = COLS.length + 1;   // 每行可编辑单元格数：时间 + 全部列
  document.querySelectorAll("td.ed").forEach(function(td){
    td.addEventListener("click", function(){
      if (S.editing) return;
      S.editing = true;
      var old = td.textContent;
      var sizer = document.createElement("span");
      sizer.style.visibility = "hidden"; sizer.textContent = old;
      td.textContent = ""; td.appendChild(sizer);
      var opts = COL_OPTS[td.dataset.f];
      var inp;
      if (opts){   // 配置了 options 的列 → 下拉选择
        inp = document.createElement("select");
        inp.style.cssText = "position:absolute;left:0;top:0;width:100%;height:100%;" +
          "border:none;outline:none;font:inherit;background:#fff8dc;padding:6px 4px;" +
          "margin:0;box-sizing:border-box";
        [""].concat(opts).forEach(function(o){
          var op = document.createElement("option");
          op.value = o; op.textContent = o || "(空)";
          inp.appendChild(op);
        });
        inp.value = opts.indexOf(old) >= 0 ? old : "";
      } else {
        inp = document.createElement("input");
        inp.value = old;
      }
      td.appendChild(inp);
      inp.focus();
      if (inp.tagName === "INPUT") inp.select();
      var done = false;
      function save(){
        if (done) return; done = true; S.editing = false;
        var v = inp.value.trim();
        var tr = td.closest("tr");
        td.textContent = v;
        api("/api/field", {id: +tr.dataset.id, field: td.dataset.f, value: v,
            rev: +tr.dataset.rev||1}, function(j){
          if (j.conflict){
            td.textContent = j.value; tr.dataset.rev = j.rev;
            toast("该单元格刚被他人修改过，已显示最新值");
          } else {
            tr.dataset.rev = j.rev;
            toast("已保存");
          }
        });
      }
      inp.addEventListener("keydown", function(e){
        if (e.key === "Enter") inp.blur();
        if (e.key === "Tab"){          // Tab / Shift+Tab 在单元格间跳转
          e.preventDefault();
          var cells = edCells();
          var i = cells.indexOf(td);
          var nx = cells[i + (e.shiftKey ? -1 : 1)];
          inp.blur();                  // 先保存当前格
          if (nx) nx.click();
          return;
        }
        if (e.key === "Escape"){ done = true; S.editing = false; td.textContent = old; }
      });
      if (inp.tagName === "INPUT"){
        inp.addEventListener("paste", function(e){   // Excel 式多行粘贴
          var text = (e.clipboardData || window.clipboardData).getData("text");
          if (!text || text.indexOf("\n") < 0) return;   // 单行照常粘贴
          e.preventDefault();
          done = true; S.editing = false;   // 先关闭 blur→save 通道，防止竞态覆盖
          var lines = text.replace(/\r/g, "").split("\n");
          if (lines.length && lines[lines.length-1] === "") lines.pop();
          var cells = edCells();
          var i0 = cells.indexOf(td), n = 0;
          for (var li = 0; li < lines.length; li++){
            var vals = lines[li].split("\t");
            for (var vi = 0; vi < vals.length; vi++){
              var cell = cells[i0 + li*pitch + vi];
              if (!cell) break;
              setCell(cell, vals[vi].trim());
              n++;
            }
          }
          if (inp.parentNode) inp.parentNode.removeChild(inp);  // 退出编辑态
          if (n) toast("已粘贴 " + n + " 个单元格");
        });
      }
      inp.addEventListener("blur", save);
      if (inp.tagName === "SELECT") inp.addEventListener("change", function(){ inp.blur(); });
    });
  });
}

/* ---------- 行操作 ---------- */
function addRow(){
  api("/api/row", {sheet_id: S.cur}, function(j){ toast("已新增一行"); loadSheets(); loadRows(); });
}
function delRow(id){
  if (!confirm("删除这条记录？")) return;
  api("/api/row/delete", {ids:[id]}, function(j){ S.checked.delete(id); toast("已移入回收站"); loadSheets(); loadRows(); });
}
function delSelected(){
  var ids = [];
  document.querySelectorAll("#tb tr").forEach(function(tr){
    var c = tr.querySelector("td.ck input");
    if (c && c.checked) ids.push(+tr.dataset.id);
  });
  if (!ids.length){ toast("先勾选要删除的行"); return; }
  if (!confirm("删除选中的 " + ids.length + " 条记录？\n（会移入回收站，可在回收站恢复）")) return;
  api("/api/row/delete", {ids:ids}, function(j){
    S.checked.clear();
    toast("已移入回收站 " + ids.length + " 条"); loadSheets(); loadRows();
  });
}

/* ---------- 导出 ---------- */
function doExport(kind){
  window.open("/export." + kind + "?sheet_id=" + S.cur, "_blank");
}

/* ---------- 回收站 ---------- */
function openTrash(){
  document.getElementById("trashMask").style.display = "flex";
  api("/api/trash", null, function(j){
    var b = document.getElementById("trashBody");
    if (!j.rows.length){ b.innerHTML = "<div class='empty'>回收站是空的</div>"; return; }
    var h = "<table><tr><th>删除时间</th><th>原表格</th><th>时间</th><th>来源</th><th>首个文件</th><th>操作</th></tr>";
    j.rows.forEach(function(r){
      h += "<tr data-id='" + r.id + "'><td class='t'>" + esc(r.deleted_at) + "</td><td>" +
           esc(r.sheet_name || "-") + "</td><td class='t'>" + esc(r.shot_time) + "</td><td>" +
           esc(r.machine || "-") + "</td><td class='t'>" + esc(r.first_file || "-") +
           "</td><td><span class='lk' onclick=restoreOne(" + r.id + ")>恢复</span>" +
           "<span class='lk red' onclick=trashOne(" + r.id + ")>彻底删除</span></td></tr>";
    });
    b.innerHTML = h + "</table>";
  });
}
function closeTrash(){ document.getElementById("trashMask").style.display = "none"; }
function restoreOne(id){
  api("/api/trash/restore", {ids:[id]}, function(j){ toast("已恢复 " + j.restored + " 条");
    openTrash(); loadSheets(); });
}
function trashOne(id){
  if (!confirm("彻底删除这条记录？将无法恢复！")) return;
  api("/api/trash/delete", {ids:[id]}, function(j){ openTrash(); });
}
function purgeTrash(){
  if (!confirm("清空回收站？全部记录将永久删除！")) return;
  api("/api/trash/delete", {}, function(j){ toast("回收站已清空"); openTrash(); loadSheets(); });
}

/* ---------- 告警横幅（B 机目录失效等） ---------- */
function loadAlerts(){
  fetch("/api/alerts", {cache:"no-store"}).then(function(r){ return r.json(); }).then(function(j){
    var b = document.getElementById("banner");
    if (!j.alerts || !j.alerts.length){ b.style.display = "none"; return; }
    var a = j.alerts[0];
    b.textContent = "⚠ [" + a.machine + "] " + a.message + "  (" + a.at + ")" +
                    (j.alerts.length > 1 ? "  —— 共 " + j.alerts.length + " 条告警" : "");
    b.className = a.level === "error" ? "" : "info";
    b.style.display = "block";
  }).catch(function(){});
}

/* ---------- 自动刷新：4 秒轮询（编辑中/回收站打开/页面隐藏时暂停） ---------- */
setInterval(function(){
  if (S.editing || document.hidden) return;
  if (document.getElementById("trashMask").style.display === "flex") return;
  if (document.querySelector("td.ed input, td.ed select")) return;
  loadSheets(); loadAlerts();
}, 4000);
/* 切回标签页/恢复窗口时立即刷新，不等下一个 4 秒节拍 */
document.addEventListener("visibilitychange", function(){
  if (document.hidden || S.editing) return;
  if (document.getElementById("trashMask").style.display === "flex") return;
  loadSheets(); loadAlerts();
});
window.addEventListener("focus", function(){
  if (S.editing) return;
  if (document.getElementById("trashMask").style.display === "flex") return;
  loadSheets();
});

/* ---------- 启动 ---------- */
var COLS = __COLS__;
COLS.forEach(function(c){ if (c.options) COL_OPTS[c.key] = c.options; });
loadHash();
loadSheets();
loadAlerts();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _ok(self, **kw):
        self._send(200, json.dumps(dict(ok=True, **kw), ensure_ascii=False),
                   "application/json")

    def _err(self, msg, code=400):
        self._send(code, json.dumps({"ok": False, "error": str(msg)}),
                   "application/json")

    def _check_ip(self):
        """白名单校验：本机环回始终放行；allow_ips 非空时只放行匹配的 IP/网段前缀"""
        if not ALLOW_IPS:
            return True
        ip = self.client_address[0] or ""
        if ip in ("127.0.0.1", "::1", "localhost"):
            return True
        for rule in ALLOW_IPS:
            rule = rule.strip()
            if not rule:
                continue
            if ip == rule or ip.startswith(rule if rule.endswith(".") else rule + "."):
                return True
        return False

    # ---------- GET ----------
    def do_GET(self):
        if not self._check_ip():
            return self._err("IP 不在访问白名单内（config_a.json）", 403)
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/":
                self.page_index()
            elif url.path == "/api/sheets":
                self.api_sheets()
            elif url.path == "/api/rows":
                self.api_rows(q)
            elif url.path == "/api/alerts":
                self.api_alerts()
            elif url.path == "/api/trash":
                self.api_trash()
            elif url.path == "/export.csv":
                self.export_csv(q)
            elif url.path == "/export.xlsx":
                self.export_xlsx(q)
            else:
                self._err("not found", 404)
        except Exception as e:
            self._err(e)

    # ---------- POST ----------
    def do_POST(self):
        if not self._check_ip():
            return self._err("IP 不在访问白名单内（config_a.json）", 403)
        url = urlparse(self.path)
        routes = {
            "/api/shot": self.api_shot,
            "/api/energy": self.api_energy,
            "/api/field": self.api_field,
            "/api/row": self.api_row_add,
            "/api/row/delete": self.api_row_delete,
            "/api/sheets": self.api_sheet_create,
            "/api/sheets/update": self.api_sheet_update,
            "/api/sheets/delete": self.api_sheet_delete,
            "/api/alert": self.api_alert,
            "/api/trash/restore": self.api_trash_restore,
            "/api/trash/delete": self.api_trash_delete,
        }
        fn = routes.get(url.path)
        if fn:
            try:
                fn()
            except Exception as e:
                self._err(e)
        else:
            self._err("not found", 404)

    # ---------- 表格管理 ----------
    def api_sheets(self):
        with _db_lock, db() as conn:
            rows = conn.execute("""
                SELECT s.*, COUNT(sh.id) count FROM sheets s
                LEFT JOIN shots sh ON sh.sheet_id = s.id
                GROUP BY s.id ORDER BY s.exp_date DESC, s.id DESC""").fetchall()
        self._ok(sheets=[dict(r) for r in rows])

    def api_sheet_create(self):
        p = self._json_body()
        name = str(p.get("name", "")).strip()
        if not name:
            return self._err("表格名称不能为空")
        with _db_lock, db() as conn:
            if get_sheet(conn, name):
                return self._err("已存在同名表格: %s" % name)
            cur = conn.execute(
                "INSERT INTO sheets(name, exp_date, note, created_at) VALUES (?,?,?,?)",
                (name, str(p.get("exp_date", "") or name[:10]),
                 str(p.get("note", "")),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            sid = cur.lastrowid
        self._ok(id=sid)

    def api_sheet_update(self):
        p = self._json_body()
        sid = int(p["id"])
        name = str(p.get("name", "")).strip()
        with _db_lock, db() as conn:
            row = conn.execute("SELECT * FROM sheets WHERE id=?", (sid,)).fetchone()
            if not row:
                return self._err("表格不存在")
            if name and name != row["name"] and get_sheet(conn, name):
                return self._err("已存在同名表格: %s" % name)
            conn.execute("UPDATE sheets SET name=?, exp_date=?, note=? WHERE id=?",
                         (name or row["name"],
                          str(p.get("exp_date", row["exp_date"])),
                          str(p.get("note", row["note"])), sid))
        self._ok()

    def api_sheet_delete(self):
        p = self._json_body()
        sid = int(p["id"])
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _db_lock, db() as conn:
            row = conn.execute("SELECT * FROM sheets WHERE id=?", (sid,)).fetchone()
            if not row:
                return self._err("表格不存在")
            # 表内记录全部移入回收站（可通过回收站恢复）
            for r in conn.execute("SELECT * FROM shots WHERE sheet_id=?", (sid,)):
                conn.execute("""
                    INSERT INTO trash(orig_id, sheet_id, sheet_name, shot_time,
                        machine, file_count, first_file, folder, fields,
                        reported_at, created_at, deleted_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r["id"], r["sheet_id"], row["name"], r["shot_time"],
                     r["machine"], r["file_count"], r["first_file"], r["folder"],
                     r["fields"], r["reported_at"], r["created_at"], now))
            conn.execute("DELETE FROM shots WHERE sheet_id=?", (sid,))
            conn.execute("DELETE FROM sheets WHERE id=?", (sid,))
        self._ok()

    # ---------- 记录 ----------
    def api_rows(self, q):
        """服务端分页/搜索/排序/列级筛选，全部在 SQL 侧完成（万行级无压力）"""
        sid = int(q.get("sheet_id", ["0"])[0])
        page = max(1, int(q.get("page", ["1"])[0]))
        page_size = min(1000, max(10, int(q.get("page_size", ["50"])[0])))
        kw = q.get("q", [""])[0].strip()
        sort = q.get("sort", [""])[0]
        direction = "DESC" if q.get("dir", ["asc"])[0] == "desc" else "ASC"
        # 列级筛选：f_<key>=关键词（key 白名单校验）
        col_filters = []
        for k, vs in q.items():
            if not k.startswith("f_"):
                continue
            key, v = k[2:], vs[0].strip()
            if v and (key in COL_KEYS or key in ("shot_time", "machine", "first_file")):
                col_filters.append((key, v))

        with _db_lock, db() as conn:
            where = ["sheet_id=?"]
            params = [sid]
            if kw:
                like = "%" + kw + "%"
                where.append("(shot_time LIKE ? OR machine LIKE ? OR first_file LIKE ? "
                             "OR folder LIKE ? OR fields LIKE ?)")
                params += [like] * 5
            for key, v in col_filters:
                if key in COL_KEYS:
                    where.append("CAST(json_extract(CASE WHEN json_valid(fields) "
                                 "THEN fields ELSE '{}' END,'$.%s') AS TEXT) LIKE ?" % key)
                else:
                    where.append("%s LIKE ?" % key)
                params.append("%" + v + "%")
            wsql = " WHERE " + " AND ".join(where)

            total = conn.execute(
                "SELECT COUNT(*) c FROM shots" + wsql, params).fetchone()["c"]
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM shots" + wsql + " " +
                _order_clause(sort, direction) + " LIMIT ? OFFSET ?",
                params + [page_size, (page - 1) * page_size]).fetchall()]

        for r in rows:
            r["rev"] = r.get("rev") or 1
            try:
                r["fields"] = json.loads(r.get("fields") or "{}")
            except Exception:
                r["fields"] = {}
        self._ok(total=total, page=page, pages=pages, page_size=page_size,
                 rows=rows)

    def api_row_add(self):
        p = self._json_body()
        sid = int(p.get("sheet_id", 0))
        with _db_lock, db() as conn:
            if not conn.execute("SELECT 1 FROM sheets WHERE id=?", (sid,)).fetchone():
                return self._err("表格不存在")
            st = norm_shot_time(p.get("shot_time") or
                                datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            conn.execute("""
                INSERT INTO shots(shot_time, machine, file_count, first_file,
                                  folder, fields, reported_at, created_at, sheet_id)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (st,
                 None,  # 手动行 machine 置 NULL：与 B 机去重逻辑互不干扰
                 0, "", "",
                 json.dumps(p.get("fields", {}), ensure_ascii=False), "manual",
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sid))
        self._ok()

    def api_row_delete(self):
        """删除 = 移入回收站，可在回收站恢复或彻底清除"""
        p = self._json_body()
        ids = [int(i) for i in p.get("ids", [])]
        if not ids:
            return self._err("未指定记录")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _db_lock, db() as conn:
            for rid in ids:
                row = conn.execute(
                    "SELECT sh.*, s.name sname FROM shots sh "
                    "LEFT JOIN sheets s ON s.id = sh.sheet_id WHERE sh.id=?",
                    (rid,)).fetchone()
                if row:
                    conn.execute("""
                        INSERT INTO trash(orig_id, sheet_id, sheet_name, shot_time,
                            machine, file_count, first_file, folder, fields,
                            reported_at, created_at, deleted_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (row["id"], row["sheet_id"], row["sname"], row["shot_time"],
                         row["machine"], row["file_count"], row["first_file"],
                         row["folder"], row["fields"], row["reported_at"],
                         row["created_at"], now))
                    conn.execute("DELETE FROM shots WHERE id=?", (rid,))
        self._ok(deleted=len(ids))

    # ---------- 回收站 ----------
    def api_trash(self):
        with _db_lock, db() as conn:
            rows = conn.execute(
                "SELECT * FROM trash ORDER BY deleted_at DESC, id DESC "
                "LIMIT 500").fetchall()
        self._ok(rows=[dict(r) for r in rows])

    def api_trash_restore(self):
        p = self._json_body()
        ids = [int(i) for i in p.get("ids", [])]
        if not ids:
            return self._err("未指定记录")
        restored = 0
        with _db_lock, db() as conn:
            for tid in ids:
                row = conn.execute("SELECT * FROM trash WHERE id=?", (tid,)).fetchone()
                if not row:
                    continue
                # 原表可能已被删除：按记录里的表名找回/重建
                sheet_id = row["sheet_id"]
                if sheet_id is None or not conn.execute(
                        "SELECT 1 FROM sheets WHERE id=?", (sheet_id,)).fetchone():
                    sname = row["sheet_name"] or "恢复数据"
                    sh = get_sheet(conn, sname)
                    if not sh:
                        cur = conn.execute(
                            "INSERT INTO sheets(name, note, created_at) VALUES (?,?,?)",
                            (sname, "恢复记录时自动重建",
                             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        sheet_id = cur.lastrowid
                    else:
                        sheet_id = sh["id"]
                conn.execute("""
                    INSERT INTO shots(shot_time, machine, file_count, first_file,
                                      folder, fields, reported_at, created_at, sheet_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (row["shot_time"], row["machine"], row["file_count"],
                     row["first_file"], row["folder"], row["fields"],
                     row["reported_at"], row["created_at"], sheet_id))
                conn.execute("DELETE FROM trash WHERE id=?", (tid,))
                restored += 1
        self._ok(restored=restored)

    def api_trash_delete(self):
        """彻底清除回收站记录"""
        p = self._json_body()
        ids = [int(i) for i in p.get("ids", [])]
        with _db_lock, db() as conn:
            if ids:
                conn.executemany("DELETE FROM trash WHERE id=?", [(i,) for i in ids])
            else:
                conn.execute("DELETE FROM trash")  # 不带 ids = 清空回收站
        self._ok()

    def api_field(self):
        """单元格级编辑（带乐观锁）：shot_time 或 shotlist_cols.json 白名单字段。
        请求可带 rev（该行当前版本号）；服务端版本不一致 → 返回 conflict + 最新值，
        避免多人编辑互相覆盖。"""
        p = self._json_body()
        sid = int(p["id"])
        field = p["field"]
        val = str(p.get("value", "")).strip()
        rev = p.get("rev")
        with _db_lock, db() as conn:
            row = conn.execute(
                "SELECT fields, rev FROM shots WHERE id=?", (sid,)).fetchone()
            if row is None:
                return self._err("记录不存在")
            cur_rev = row["rev"] or 1
            if rev is not None and int(rev) != cur_rev:
                cur = conn.execute(
                    "SELECT * FROM shots WHERE id=?", (sid,)).fetchone()
                try:
                    f = json.loads(cur["fields"] or "{}")
                except Exception:
                    f = {}
                return self._send(200, json.dumps(
                    {"ok": False, "conflict": True, "rev": cur["rev"] or 1,
                     "field": field,
                     "value": cur["shot_time"] if field == "shot_time"
                              else f.get(field, "")},
                    ensure_ascii=False), "application/json")
            new_rev = cur_rev + 1
            if field == "shot_time":
                conn.execute("UPDATE shots SET shot_time=?, rev=? WHERE id=?",
                             (norm_shot_time(val), new_rev, sid))
            elif field in COL_KEYS:
                flds = json.loads(row["fields"] or "{}")
                flds[field] = val
                conn.execute("UPDATE shots SET fields=?, rev=? WHERE id=?",
                             (json.dumps(flds, ensure_ascii=False), new_rev, sid))
            else:
                return self._err("非法字段: %s" % field)
        self._ok(rev=new_rev)

    def api_shot(self):
        """B 机上报入口。可选 sheet_name / sheet_id 指定写入的表，默认"实时打靶"。
        去重键 = machine + shot_time + first_file（同秒不同文件的多发打靶都能入库）。"""
        p = self._json_body()
        shot_time = p["shot_time"]
        machine = p.get("machine", "")
        files = p.get("files", [])
        files_sorted = sorted(files, key=lambda f: f.get("mtime", 0))
        first = files_sorted[0] if files_sorted else {}
        first_name = first.get("name", "")
        with _db_lock, db() as conn:
            if p.get("sheet_id"):
                sh = conn.execute("SELECT id FROM sheets WHERE id=?",
                                  (int(p["sheet_id"]),)).fetchone()
            else:
                sname = p.get("sheet_name") or LIVE_SHEET
                sh = get_sheet(conn, sname)
                if not sh:
                    cur = conn.execute(
                        "INSERT INTO sheets(name, exp_date, note, created_at) "
                        "VALUES (?,?,?,?)",
                        (sname, shot_time[:10], "B 机上报自动创建",
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    sh = conn.execute("SELECT id FROM sheets WHERE id=?",
                                      (cur.lastrowid,)).fetchone()
            # 显式去重：同一来源、同一时间、同一首个文件 = 重复上报
            dup = conn.execute(
                "SELECT 1 FROM shots WHERE machine IS ? AND shot_time=? "
                "AND first_file IS ? AND sheet_id=?",
                (machine, shot_time, first_name, sh["id"])).fetchone()
            if not dup:
                conn.execute("""
                    INSERT INTO shots
                    (shot_time, machine, file_count, first_file, folder,
                     fields, reported_at, created_at, sheet_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (shot_time, machine, len(files),
                     first_name, first.get("folder", ""),
                     json.dumps(p.get("fields", {}), ensure_ascii=False),
                     p.get("reported_at", ""),
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sh["id"]))
        self._ok(duplicate=bool(dup))

    def api_energy(self):
        """汤姆逊谱仪能量上报绑定。
        按 shot_time 在匹配窗口内找时间最近的一条发次记录，把能量写入
        指定列（默认 fiber_p_energy「闪烁光纤质子能量」）。
        - 找到：更新该行并返回 matched（含行 id / 匹配时间 / 时间差）。
        - 窗口内没有发次：返回 ok:false + error:no_match + nearest（全库最近一条），
          调用方可带 create:true 强制补录一条独立记录（写入"实时打靶"表）。
        能量可重复发送（覆盖更新），便于解谱修正后重报。"""
        p = self._json_body()
        st = norm_shot_time(p.get("shot_time"))
        energy = str(p.get("energy", "")).strip()
        if not energy:
            return self._err("能量值不能为空")
        field = p.get("field") or "fiber_p_energy"
        if field not in COL_KEYS:
            return self._err("非法字段: %s" % field)
        try:
            shot_no = int(p.get("shot_no") or 0)
        except (TypeError, ValueError):
            shot_no = 0
        try:
            window = abs(float(p.get("window_sec", 15)))
        except (TypeError, ValueError):
            window = 15.0
        t0 = datetime.strptime(st, "%Y-%m-%d %H:%M:%S")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _db_lock, db() as conn:
            lo = (t0 - timedelta(seconds=window)).strftime("%Y-%m-%d %H:%M:%S")
            hi = (t0 + timedelta(seconds=window)).strftime("%Y-%m-%d %H:%M:%S")
            cands = conn.execute(
                "SELECT * FROM shots WHERE shot_time BETWEEN ? AND ?",
                (lo, hi)).fetchall()
            if cands:
                def _diff(r):
                    d = datetime.strptime(
                        r["shot_time"], "%Y-%m-%d %H:%M:%S") - t0
                    return abs(d.total_seconds())
                best = min(cands, key=_diff)
                flds = json.loads(best["fields"] or "{}")
                flds[field] = energy
                if shot_no:
                    flds["no"] = shot_no
                conn.execute(
                    "UPDATE shots SET fields=?, rev=COALESCE(rev,1)+1 WHERE id=?",
                    (json.dumps(flds, ensure_ascii=False), best["id"]))
                return self._ok(matched=best["id"], matched_time=best["shot_time"],
                                diff_sec=_diff(best), sheet_id=best["sheet_id"],
                                field=field, energy=energy)
            if shot_no:
                # 发次号兜底：时间窗没匹配到，但带了 No. → 绑定当天第 No. 条记录
                day = st[:10]
                rows = conn.execute(
                    "SELECT * FROM shots WHERE shot_time LIKE ? ORDER BY shot_time",
                    (day + "%",)).fetchall()
                if 0 < shot_no <= len(rows):
                    best = rows[shot_no - 1]
                    flds = json.loads(best["fields"] or "{}")
                    flds[field] = energy
                    flds["no"] = shot_no
                    conn.execute(
                        "UPDATE shots SET fields=?, rev=COALESCE(rev,1)+1 WHERE id=?",
                        (json.dumps(flds, ensure_ascii=False), best["id"]))
                    d = datetime.strptime(
                        best["shot_time"], "%Y-%m-%d %H:%M:%S") - t0
                    return self._ok(matched=best["id"], matched_time=best["shot_time"],
                                    diff_sec=abs(d.total_seconds()),
                                    sheet_id=best["sheet_id"], field=field,
                                    energy=energy, by_no=True)
            if p.get("create"):
                sh = get_sheet(conn, LIVE_SHEET)
                cur = conn.execute("""
                    INSERT INTO shots(shot_time, machine, file_count, first_file,
                                      folder, fields, reported_at, created_at, sheet_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (st, str(p.get("machine") or "thomson"), 0, "", "",
                     json.dumps({field: energy}, ensure_ascii=False),
                     "thomson_helper", now, sh["id"]))
                return self._ok(created=cur.lastrowid, sheet_id=sh["id"],
                                shot_time=st)
            near = conn.execute(
                "SELECT id, shot_time, machine FROM shots "
                "ORDER BY ABS(julianday(shot_time) - julianday(?)) LIMIT 1",
                (st,)).fetchone()
            return self._send(200, json.dumps(
                {"ok": False, "error": "no_match", "window_sec": window,
                 "nearest": dict(near) if near else None,
                 "message": "匹配窗口(%gs)内没有发次记录" % window},
                ensure_ascii=False), "application/json")

    # ---------- 告警（B 机目录失效等） ----------
    _alerts = []          # 内存告警队列，重启清空
    _alerts_lock = threading.Lock()

    def api_alert(self):
        p = self._json_body()
        with Handler._alerts_lock:
            Handler._alerts.insert(0, {
                "machine": str(p.get("machine", "")),
                "level": str(p.get("level", "error")),
                "message": str(p.get("message", "")),
                "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            del Handler._alerts[20:]
        self._ok()

    def api_alerts(self):
        self._send(200, json.dumps(
            {"ok": True, "alerts": Handler._alerts}, ensure_ascii=False),
            "application/json")

    # ---------- 导出 ----------
    def _export_rows(self, q):
        """返回 [(表名, [记录...]), ...]；sheet_id 指定则只导一张表"""
        sid = int(q.get("sheet_id", ["0"])[0]) if q else 0
        with _db_lock, db() as conn:
            if sid:
                sheets = conn.execute("SELECT * FROM sheets WHERE id=?", (sid,)).fetchall()
            else:
                sheets = conn.execute(
                    "SELECT * FROM sheets ORDER BY exp_date DESC, id DESC").fetchall()
            out = []
            for s in sheets:
                rows = conn.execute(
                    "SELECT * FROM shots WHERE sheet_id=? ORDER BY shot_time ASC, id ASC",
                    (s["id"],)).fetchall()
                recs = []
                for r in rows:
                    d = dict(r)
                    try:
                        d["fields"] = json.loads(d.get("fields") or "{}")
                    except Exception:
                        d["fields"] = {}
                    recs.append(d)
                out.append((s["name"], recs))
        return out

    def export_csv(self, q):
        buf = io.StringIO()
        buf.write("\ufeff")  # BOM，Excel/WPS 打开中文不乱码
        w = csv.writer(buf)
        head = ["表格", "时间"] + [c.get("export_name", c["name"]) for c in COLS] + \
               ["来源机器", "文件数", "首个文件"]
        w.writerow(head)
        for sname, rows in self._export_rows(q):
            for r in rows:
                w.writerow([sname, r["shot_time"]] +
                           [r["fields"].get(c["key"], "") for c in COLS] +
                           [r["machine"], r["file_count"], r["first_file"]])
        fname = "shotlist_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S")
        self._send(200, buf.getvalue().encode("utf-8-sig"),
                   "text/csv; charset=utf-8",
                   {"Content-Disposition": "attachment; filename=%s" % fname})

    def export_xlsx(self, q):
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font, PatternFill
        except ImportError:
            return self._send(500, "导出 xlsx 需要 openpyxl 库：pip install openpyxl（或先用 CSV 导出）")
        data = self._export_rows(q)
        if not data:
            return self._err("没有可导出的表格")
        wb = Workbook()
        wb.remove(wb.active)
        hfill = PatternFill("solid", fgColor="2C3E50")
        hfont = Font(color="FFFFFF")
        for sname, rows in data:
            ws = wb.create_sheet(title=(sname or "sheet")[:31])
            names = [c.get("export_name", c["name"]) for c in COLS]
            widths = [18] + [c.get("width", 80) for c in COLS]
            head = ["时间"] + names + ["来源机器", "文件数", "首个文件"]
            for j, (h, wd) in enumerate(zip(head, widths + [90, 60, 220]), 1):
                c = ws.cell(row=1, column=j, value=h)
                c.fill = hfill
                c.font = hfont
                c.alignment = Alignment(horizontal="center")
                ws.column_dimensions[c.column_letter].width = max(10, wd / 7.0)
            for i, r in enumerate(rows, 2):
                ws.cell(row=i, column=1, value=r["shot_time"])
                for j, c in enumerate(COLS, 2):
                    ws.cell(row=i, column=j, value=to_number(r["fields"].get(c["key"], "")))
                n = len(COLS)
                ws.cell(row=i, column=n + 2, value=r["machine"])
                ws.cell(row=i, column=n + 3, value=r["file_count"])
                ws.cell(row=i, column=n + 4, value=r["first_file"])
            ws.freeze_panes = "A2"
        bio = io.BytesIO()
        wb.save(bio)
        fname = "shotlist_%s.xlsx" % datetime.now().strftime("%Y%m%d_%H%M%S")
        self._send(200, bio.getvalue(),
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   {"Content-Disposition": "attachment; filename=%s" % fname})

    # ---------- 页面 ----------
    def page_index(self):
        self._send(200, PAGE.replace("__COLS__", json.dumps(COLS, ensure_ascii=False)))

    def log_message(self, fmt, *args):
        pass  # 静默访问日志


def main():
    init_db()
    # 启动时先备份一次，再由后台线程定时备份
    try:
        print("[backup] 启动备份: %s" % backup_now())
    except Exception as e:
        print("[backup] 启动备份失败: %r" % e)
    threading.Thread(target=_backup_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("实验日志系统已启动: http://0.0.0.0:%d" % PORT)
    print("表格/记录/回收站三级结构 ｜ 列数: %d (shotlist_cols.json) ｜ B机上报默认写入[%s]表"
          % (len(COLS), LIVE_SHEET))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("已停止")


if __name__ == "__main__":
    main()
