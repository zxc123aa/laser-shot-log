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

# A 机侧账本：记录"谁在什么时候报了什么"。
# 为什么 A 机也要记：PyTPS 可能**直发 A 机 /api/energy**（09-30 实测：B 机 helper
# 零调用，但 A 机表有 77 条 tps_h）——这种情况下 A 机是唯一的记账点。
# 与 B 机账本分目录（ledger_a/），B 机可通过 GET /api/ledger 拉回去对账。
os.environ.setdefault("LSL_LEDGER_DIR", os.path.join(BASE, "ledger_a"))
try:
    import ledger as _ledger
except Exception:
    _ledger = None


def aled(ev, day=None):
    """写一条 A 机账本事件（静默失败，绝不影响入库主流程）。"""
    if _ledger is None:
        return
    try:
        _ledger.append(ev, day=day)
    except Exception:
        pass
LIVE_SHEET = "实时打靶"          # B 机上报默认写入的表

# ---- 跨机合并：默认关闭（09-30 的 shot79/80 被吃就是它干的）----
# 曾经的语义：不同机器上报、时间差 <= SHOT_MERGE_SEC 的两次上报并入同一行，
# 并把 file_count 累加。它的前提是"C 机会自己建行"，而现行口径是
# **C 机永不建行**（report_mode=timeline，只推 tif 时间表给 B 机做能量匹配）。
# 前提没了，合并逻辑只剩坏处：连发被吃、file_count 失真、能量归属查不清。
# 因此默认 0 = 关闭。除非明确知道自己在做什么，不要打开。
try:
    with open(os.path.join(BASE, "config_a.json"), "r", encoding="utf-8") as _f:
        _CFG_A = json.load(_f)
except Exception:
    _CFG_A = {}
SHOT_MERGE_SEC = float(_CFG_A.get("shot_merge_sec", 0) or 0)

# ---- 建行的文件必须匹配发次命名（第二道防线）----
# 现场出现过：新建的 txt / 其他相机的 PNG 被当成发次上报进来，抢走编号、
# 污染整表。B 机侧已改白名单；A 机再挡一次，防止某台机 config 配错。
# 可用 config_a.json 的 shot_file_patterns 覆盖；enforce_shot_file=false 可关。
_DEFAULT_SHOT_PATTERNS = [
    r"^(shot|shor)[-_ ]?\d+\.(png|tif|tiff|dat|raw|jpg|jpeg|bmp)$",
]
_pats = _CFG_A.get("shot_file_patterns") or _DEFAULT_SHOT_PATTERNS
if not isinstance(_pats, (list, tuple)) or not _pats:
    _pats = _DEFAULT_SHOT_PATTERNS
try:
    SHOT_FILE_RE = re.compile("|".join(_pats), re.IGNORECASE)
except re.error:
    SHOT_FILE_RE = re.compile(_DEFAULT_SHOT_PATTERNS[0], re.IGNORECASE)
ENFORCE_SHOT_FILE = bool(_CFG_A.get("enforce_shot_file", True))

SORTABLE_SQL = {"shot_time", "machine", "file_count", "first_file", "id"}

DEFAULT_COLS = [{"key": "target_type", "name": "靶类型", "width": 110},
                {"key": "note", "name": "备注", "width": 180}]

try:
    with open(COLS_PATH, "r", encoding="utf-8") as f:
        COLS = json.load(f)
except Exception:
    COLS = DEFAULT_COLS
COL_KEYS = {c["key"] for c in COLS}

# 访问白名单（config_a.json，_CFG_A 已在上方读取）：
# 空 = 不限制；非空 = 只允许列表里的 IP/网段前缀
ALLOW_IPS = [str(x) for x in _CFG_A.get("allow_ips", [])]

_db_lock = threading.Lock()

# 数据版本号：任意 POST（增删改行/表/回收站/告警等）成功后 +1，
# SSE 端点 /api/events 据此在数据变化时立刻通知页面刷新
DATA_VER = 0


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


def _diff_key(t0):
    """行时间与 t0 的绝对秒差（min 的 key 函数）"""
    def _f(r):
        d = datetime.strptime(r["shot_time"], "%Y-%m-%d %H:%M:%S") - t0
        return abs(d.total_seconds())
    return _f


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

# ---- UI 模板已抽离到 ui/index.html（独立文件，git 合并只碰它、不碰本 .py，
#      根治"A机改了UI、合并B机推送时被旧UI覆盖"）。按 mtime 缓存：
#      改模板即生效，无需重启进程；文件缺失时返回显式错误页而不是崩。----
_UI_PATH = os.path.join(BASE, 'ui/index.html')
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
            elif url.path == "/api/events":
                self.sse_events()
            elif url.path == "/api/trash":
                self.api_trash()
            elif url.path == "/api/ledger":
                self.api_ledger(q)
            elif url.path == "/api/ledger_stats":
                self.api_ledger_stats(q)
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
            "/api/alerts/clear": self.api_alerts_clear,
            "/api/trash/restore": self.api_trash_restore,
            "/api/trash/delete": self.api_trash_delete,
        }
        fn = routes.get(url.path)
        if fn:
            global DATA_VER
            try:
                fn()
                DATA_VER += 1   # 数据有变，SSE 通知页面
            except Exception as e:
                self._err(e)
        else:
            self._err("not found", 404)

    def sse_events(self):
        """SSE：数据版本变化时推一条 ping，页面收到立即刷新（毫秒级出新一发）"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = DATA_VER
        last_beat = time.time()
        try:
            while True:
                if DATA_VER != last:
                    last = DATA_VER
                    self.wfile.write(b"data: changed\n\n")
                    self.wfile.flush()
                    last_beat = time.time()
                elif time.time() - last_beat >= 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.time()
                time.sleep(0.4)
        except Exception:
            pass   # 客户端断开

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
        # ---- 入口校验：绝不接受"不像发次"的上报（第二道防线）----
        # B 机侧已有白名单，这里再挡一次：某台机 config 配错 / 旧版本未更新 /
        # 有人手工 curl，都不能污染日志表。拒绝时写账本留证据，不静默吞掉。
        if not files_sorted or not first_name:
            aled({"ev": "a_shot_reject", "reason": "empty_files",
                  "machine": machine, "shot_time": shot_time,
                  "client_ip": self.client_address[0]},
                 day=str(shot_time)[:10] or None)
            return self._err("files 为空：无法建行（一行必须对应一个 shot 文件）")
        if ENFORCE_SHOT_FILE:
            bad = [f.get("name", "") for f in files_sorted
                   if not SHOT_FILE_RE.match(str(f.get("name", "")))]
            if bad:
                aled({"ev": "a_shot_reject", "reason": "not_shot_file",
                      "machine": machine, "shot_time": shot_time,
                      "names": bad[:10], "client_ip": self.client_address[0]},
                     day=str(shot_time)[:10] or None)
                return self._err(
                    "文件名不符合发次命名规则，拒绝建行: %s"
                    "（如确需纳入，改 config_a.json 的 shot_file_patterns，"
                    "或把 enforce_shot_file 设为 false）" % bad[:3])
        new_id, drev = None, None   # 建行/去重两条路径共用，供末尾统一返回
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
                "SELECT id, fields FROM shots WHERE machine IS ? AND shot_time=? "
                "AND first_file IS ? AND sheet_id=?",
                (machine, shot_time, first_name, sh["id"])).fetchone()
            # 跨机器同发次合并 —— **默认关闭**（SHOT_MERGE_SEC=0）。
            # 现行口径：C 机永不建行（只推 tif 时间表给 B 机做能量匹配），
            # 所以"跨机合并"已无使用场景；留着只会像 09-30 那样吃掉连发。
            # 一个 shot 文件 = 一条记录，同机/跨机都不合并。
            near = None
            if not dup and SHOT_MERGE_SEC > 0:
                cand = conn.execute(
                    "SELECT id, fields, sheet_id, shot_time, machine FROM shots WHERE "
                    "ABS(julianday(shot_time) - julianday(?)) * 86400 <= ? "
                    "ORDER BY (sheet_id=? ) DESC, "
                    "ABS(julianday(shot_time) - julianday(?)) LIMIT 1",
                    (shot_time, SHOT_MERGE_SEC, sh["id"], shot_time)).fetchone()
                if cand and (cand["machine"] or "") != (machine or ""):
                    near = cand
            if not dup and near:
                try:
                    flds = json.loads(near["fields"] or "{}")
                except Exception:
                    flds = {}
                merged = {k: v for k, v in (p.get("fields") or {}).items()
                          if v not in ("", None) and not flds.get(k)}
                if merged:
                    flds.update(merged)
                nrow = conn.execute(
                    "UPDATE shots SET fields=?, file_count=file_count+?, "
                    "rev=COALESCE(rev,1)+1 WHERE id=?",
                    (json.dumps(flds, ensure_ascii=False), len(files), near["id"]))
                mrev = conn.execute("SELECT rev FROM shots WHERE id=?",
                                    (near["id"],)).fetchone()
                aled({"ev": "a_shot", "action": "merged",
                      "id": near["id"],
                      "rev": (mrev["rev"] if mrev else None),
                      "merged_into": near["id"],
                      "machine": machine, "shot_time": shot_time,
                      "first_file": first_name, "file_count": len(files),
                      "sheet_id": near["sheet_id"],
                      "merged_fields": sorted(merged.keys()),
                      "client_ip": self.client_address[0]},
                     day=shot_time[:10] or None)
                # id 一并返回：B 机要把它回写进 shot["row_id"] 和账本，
                # 否则事后无法知道"这一发对应 A 机哪一行"（09-30 的坑）
                self._ok(merged_into=near["id"], id=near["id"],
                         rev=(mrev["rev"] if mrev else None),
                         sheet_id=near["sheet_id"])
                return
            if not dup:
                cur = conn.execute("""
                    INSERT INTO shots
                    (shot_time, machine, file_count, first_file, folder,
                     fields, reported_at, created_at, sheet_id)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (shot_time, machine, len(files),
                     first_name, first.get("folder", ""),
                     json.dumps(p.get("fields", {}), ensure_ascii=False),
                     p.get("reported_at", ""),
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sh["id"]))
                new_id = cur.lastrowid
                aled({"ev": "a_shot", "action": "insert", "id": new_id, "rev": 1,
                      "machine": machine, "shot_time": shot_time,
                      "first_file": first_name, "folder": first.get("folder", ""),
                      "file_count": len(files), "sheet_id": sh["id"],
                      "sheet_name": p.get("sheet_name", ""),
                      "fields": p.get("fields", {}),
                      "reported_at": p.get("reported_at", ""),
                      "client_ip": self.client_address[0]},
                     day=shot_time[:10] or None)
            else:
                # 重复上报：字段合并——已存在的行里"空/缺失"的字段用新值补上
                # （helper 与 b_watcher 都会上报同一次打靶，谁先到谁建行；
                #   后到的可能带着先到者没有的数据，如靶位/离焦，不能直接丢弃）
                try:
                    flds = json.loads(dup["fields"] or "{}")
                except Exception:
                    flds = {}
                merged = {k: v for k, v in (p.get("fields") or {}).items()
                          if v not in ("", None) and not flds.get(k)}
                if merged:
                    flds.update(merged)
                    conn.execute(
                        "UPDATE shots SET fields=?, rev=COALESCE(rev,1)+1 "
                        "WHERE id=?",
                        (json.dumps(flds, ensure_ascii=False), dup["id"]))
                drev = conn.execute("SELECT rev FROM shots WHERE id=?",
                                    (dup["id"],)).fetchone()
                new_id = dup["id"]
                aled({"ev": "a_shot", "action": "duplicate", "id": dup["id"],
                      "rev": (drev["rev"] if drev else None),
                      "machine": machine, "shot_time": shot_time,
                      "first_file": first_name, "file_count": len(files),
                      "sheet_id": sh["id"],
                      "merged_fields": sorted(merged.keys()),
                      "client_ip": self.client_address[0]},
                     day=shot_time[:10] or None)
        self._ok(duplicate=bool(dup), id=new_id,
                 rev=(drev["rev"] if (dup and drev) else 1))

    def api_energy(self):
        """汤姆逊谱仪能量上报绑定。
        匹配优先级（绝不覆盖已有发次号——序号只能来自 B 机文件名解析）：
        1. 时间窗内最近的一条发次（TPS 的 No 可能断号错位，时间才最可靠；
           只在目标行没有 No. 时补写，绝不改写；行 No 与 shot_no 不一致时
           在返回中带 no_mismatch 提示）；
        2. 窗口未中且带 shot_no：精确匹配当天 fields.no == shot_no 的行兜底；
        3. 都没中：返回 no_match。⚠️ **能量绝不建行/建表**——一行只能由
           B 机的 shot PNG 发次建立，能量只能绑到已存在的行；调用方
           （B 机 helper）会把这条能量暂存起来，等对应发次行出现后再重绑。
        能量可重复发送（覆盖更新），便于解谱修正后重报。"""
        p = self._json_body()
        st = norm_shot_time(p.get("shot_time"))
        energy = str(p.get("energy", "")).strip()
        # ---- A 机账本：能量到达（原始 payload + 来源 IP）----
        # PyTPS 若直发 A 机（09-30 实测即如此），这里是唯一的记账点：
        # 没有这条记录，事后就无法回答"这个能量是谁、什么时候、报给哪一行的"。
        aled({"ev": "a_energy_in", "payload": p,
              "client_ip": self.client_address[0],
              "shot_time": st, "energy": energy,
              "field": p.get("field") or "fiber_p_energy"},
             day=str(st)[:10] or None)
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
        day = st[:10]
        with _db_lock, db() as conn:
            def _bind(best, by_no=False):
                flds = json.loads(best["fields"] or "{}")
                flds[field] = energy
                if shot_no and not str(flds.get("no") or "").strip():
                    flds["no"] = shot_no       # 仅补空，绝不覆盖已有序号
                conn.execute(
                    "UPDATE shots SET fields=?, rev=COALESCE(rev,1)+1 WHERE id=?",
                    (json.dumps(flds, ensure_ascii=False), best["id"]))
                d = datetime.strptime(
                    best["shot_time"], "%Y-%m-%d %H:%M:%S") - t0
                extra = {}
                if not by_no and shot_no and str(flds.get("no") or "") \
                        not in ("", str(shot_no)):
                    extra["no_mismatch"] = {
                        "row_no": flds.get("no"), "sent_no": shot_no,
                        "message": "时间最近行 No.%s 与上报 No.%d 不一致，"
                                   "已按行写入能量、未改动行号"
                                   % (flds.get("no"), shot_no)}
                rrow = conn.execute("SELECT rev, machine, first_file, file_count "
                                    "FROM shots WHERE id=?", (best["id"],)).fetchone()
                newrev = rrow["rev"] if rrow else None
                # ---- A 机账本：能量绑定结果（命中哪行、差几秒、按时间还是按No.）----
                aled({"ev": "a_energy_bind", "row_id": best["id"],
                      "rev": newrev, "field": field, "energy": energy,
                      "matched_time": best["shot_time"],
                      "diff_sec": round(abs(d.total_seconds()), 3),
                      "by_no": by_no, "shot_no_sent": shot_no,
                      "row_no": flds.get("no"),
                      "row_machine": (rrow["machine"] if rrow else ""),
                      "row_first_file": (rrow["first_file"] if rrow else ""),
                      "row_file_count": (rrow["file_count"] if rrow else None),
                      "no_mismatch": bool(extra.get("no_mismatch")),
                      "window_sec": window,
                      "client_ip": self.client_address[0]},
                     day=str(best["shot_time"])[:10] or None)
                return self._ok(matched=best["id"],
                                matched_time=best["shot_time"],
                                diff_sec=abs(d.total_seconds()),
                                sheet_id=best["sheet_id"], rev=newrev,
                                field=field, energy=energy, **extra)

            # 1) 时间窗内最近行（TPS No 可能断号错位，时间是最可靠锚点）
            lo = (t0 - timedelta(seconds=window)).strftime("%Y-%m-%d %H:%M:%S")
            hi = (t0 + timedelta(seconds=window)).strftime("%Y-%m-%d %H:%M:%S")
            cands = conn.execute(
                "SELECT * FROM shots WHERE shot_time BETWEEN ? AND ?",
                (lo, hi)).fetchall()
            if cands:
                return _bind(min(cands, key=_diff_key(t0)))

            # 2) 窗口未中 + 带 shot_no：当天精确 No. 匹配兜底（行可能迟到落库）
            if shot_no:
                rows = conn.execute(
                    "SELECT * FROM shots WHERE shot_time LIKE ? ORDER BY shot_time",
                    (day + "%",)).fetchall()
                exact = [r for r in rows
                         if str(json.loads(r["fields"] or "{}")
                                .get("no") or "") == str(shot_no)]
                if exact:
                    return _bind(min(exact, key=_diff_key(t0)), by_no=True)

            # 3) 都没中：不猜"第 N 条"（合并/缺行时必然绑错），
            #    直接返回 no_match 交给调用方重试或补录
            near = conn.execute(
                "SELECT id, shot_time, machine FROM shots "
                "ORDER BY ABS(julianday(shot_time) - julianday(?)) LIMIT 1",
                (st,)).fetchone()
            # ---- A 机账本：能量未命中（这条能量没绑上任何行，必须留痕）----
            aled({"ev": "a_energy_nomatch", "shot_time": st, "energy": energy,
                  "field": field, "shot_no_sent": shot_no,
                  "window_sec": window,
                  "nearest": dict(near) if near else None,
                  "client_ip": self.client_address[0]},
                 day=str(st)[:10] or None)
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

    def api_alerts_clear(self):
        with Handler._alerts_lock:
            Handler._alerts.clear()
        self._ok()

    # ---------- A 机账本（供 B 机拉取回灌对账） ----------
    def api_ledger(self, q):
        """GET /api/ledger?day=YYYY-MM-DD&ev=a_energy_in&since=...&limit=N

        把 A 机侧账本吐给 B 机。为什么需要：PyTPS 可能直发 A 机 /api/energy，
        B 机根本不知道这条能量存在过（09-30 的 77 条 tps_h 就是这么"凭空"出现的）。
        B 机定期拉这个接口，就能把 A 机收到的能量回灌进自己的账本，
        重建时两边才对得齐。只读，不改任何数据。"""
        if _ledger is None:
            return self._send(200, json.dumps(
                {"ok": False, "error": "ledger 模块不可用"},
                ensure_ascii=False), "application/json")
        day = (q.get("day") or [datetime.now().strftime("%Y-%m-%d")])[0]
        want_ev = {e for e in (q.get("ev") or [""])[0].split(",") if e}
        since = (q.get("since") or [""])[0].strip()
        try:
            limit = int((q.get("limit") or ["20000"])[0])
        except (TypeError, ValueError):
            limit = 20000
        evs, bad = _ledger.read_day(day)
        out = []
        for e in evs:
            if want_ev and str(e.get("ev", "")) not in want_ev:
                continue
            if since and str(e.get("iso", "")) < since:
                continue
            out.append(e)
            if len(out) >= limit:
                break
        self._send(200, json.dumps(
            {"ok": True, "day": day, "count": len(out), "bad_lines": bad,
             "host": os.environ.get("LSL_LEDGER_DIR", ""), "lines": out},
            ensure_ascii=False), "application/json")

    def api_ledger_stats(self, q):
        """GET /api/ledger_stats?day=... —— A 机账本体检（只读）。"""
        if _ledger is None:
            return self._send(200, json.dumps(
                {"ok": False, "error": "ledger 模块不可用"},
                ensure_ascii=False), "application/json")
        day = (q.get("day") or [datetime.now().strftime("%Y-%m-%d")])[0]
        self._send(200, json.dumps(
            dict(_ledger.stats(day), ok=True, days=_ledger.iter_days()),
            ensure_ascii=False), "application/json")

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
        self._send(200, _load_ui_html().replace("__COLS__", json.dumps(COLS, ensure_ascii=False)))

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
