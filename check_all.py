#!/usr/bin/env python
"""check_all.py — 提交/推送前的一键安全门禁（纯静态，绝不写生产数据）。

为什么需要它：2026-09-30/10-01 连续踩坑——_snapshot() 把无参函数
effective_target_map() 当有参调用（语法合法、py_compile 也过），运行到那行
才抛 TypeError，直接打断 SSE 页面推送；能量入口一度能建行、跨机合并吃掉连发；
测试进程 import 模块触发 save_state() 覆盖了生产 state_helper.json。
这些 ast.parse / py_compile 全查不出来。本门禁把"安全不变量"固化成断言，
push 前跑一遍，任何一条 FAIL 都不许推。

用法：
    python check_all.py            # 全量检查，退出码 0=通过 1=有错
    python check_all.py -v         # 额外打印跨文件同名函数的可疑调用

检查项：
  1. 语法 + 字节码编译（所有核心 .py）
  2. 函数签名核对（调用点参数个数/关键字与被调函数签名是否相符）
  3. 安全不变量（能量永不建行、跨机合并默认关、状态写保护、发次白名单）
  4. 冒烟 import（关键模块可加载、关键函数存在）——只 import 不调用写盘函数
"""
import ast
import os
import py_compile
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))

# 核心文件：语法/签名/冒烟都查这些
CORE = ["a_server.py", "b_watcher.py", "thomson_helper.py", "ledger.py",
        "rebuild_from_ledger.py", "sheet_backup.py", "target_client.py",
        "target_query.py"]
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".workbuddy",
             "_atest", "_rtest", "_htest", "backup", "shotlist"}

VERBOSE = "-v" in sys.argv
_fail = []
_warn = []


def _p(name):
    return os.path.join(BASE, name)


# ---------------------------------------------------------------- 1. 语法/编译
def check_syntax():
    print("== 1. 语法 + 字节码编译 ==")
    tmp = tempfile.gettempdir()
    for f in CORE:
        path = _p(f)
        if not os.path.exists(path):
            _warn.append("缺文件 %s" % f)
            continue
        try:
            ast.parse(open(path, encoding="utf-8").read())
            py_compile.compile(path, cfile=os.path.join(tmp, f + "c"),
                               doraise=True)
            print("   OK   %s" % f)
        except SyntaxError as e:
            print("   FAIL %s  line %s: %s" % (f, e.lineno, e.msg))
            _fail.append("语法错误 %s:%s %s" % (f, e.lineno, e.msg))
        except Exception as e:
            print("   FAIL %s  编译: %r" % (f, e))
            _fail.append("编译失败 %s: %r" % (f, e))


# ---------------------------------------------------------------- 2. 签名核对
def _build_index(paths):
    """{函数名: [{file,line,lo,hi,posnames,kwnames,kwrest,async}]}
    只收录**模块顶层 def**——嵌套函数在外部根本不可见，
    收进来只会制造跨文件假阳性。"""
    idx = {}
    for path in paths:
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except Exception:
            continue
        fn = os.path.basename(path)
        for node in tree.body:                   # 顶层 only
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            a = node.args
            allpos = list(a.posonlyargs) + list(a.args)
            names = [x.arg for x in allpos]
            if names and names[0] in ("self", "cls"):
                names = names[1:]              # 方法调用不含 self
            npos = len(names)
            lo = max(0, npos - len(a.defaults))
            idx.setdefault(node.name, []).append({
                "file": fn, "line": node.lineno,
                "lo": lo, "hi": (None if a.vararg else npos),
                "posnames": set(names),
                "kwnames": {k.arg for k in a.kwonlyargs},
                "kwrest": a.kwarg is not None,
                "async": isinstance(node, ast.AsyncFunctionDef)})
    return idx


def _call_ok(node, defs):
    """调用点能否匹配任一定义？（关键字传给位置参数是合法的，别误报）"""
    if any(isinstance(x, ast.Starred) for x in node.args):
        return True, None                       # f(*args) 无法静态判定
    nargs = len(node.args)
    kws = {kw.arg for kw in node.keywords if kw.arg}
    has_kwstar = any(kw.arg is None for kw in node.keywords)  # f(**d)
    for d in defs:
        if d["hi"] is not None and nargs > d["hi"]:
            continue                            # 位置参数超上限
        covered = nargs + len(kws & d["posnames"])
        if covered < d["lo"]:
            continue                            # 必填参数没给够
        if not d["kwrest"] and not has_kwstar:
            if not kws <= (d["posnames"] | d["kwnames"]):
                continue                        # 有未知关键字
        return True, d
    return False, None


def check_signatures():
    """只核对**调用点确实可见**的函数定义，杜绝误报：
    - 裸名调用 f(...)：先匹配同文件定义；再看本文件 `from X import f`
      指向的定义；都不可见则跳过（可能是内建/未导入的第三方名）。
    - 属性调用 mod.f(...)：只有 mod 是本仓库模块且 `import mod` 在本文件
      出现时才核对——否则 subprocess.run 之类会被当成 target_query.run
      （09-30 版门禁的 6 条误报就是这么来的）。
    - 匹配到多个可见定义时降为可疑（-v 才显示），不算 FAIL。
    """
    print("== 2. 函数签名核对 ==")
    paths = [_p(f) for f in CORE if os.path.exists(_p(f))]
    per_file = {}                                # {文件名: idx}
    for path in paths:
        per_file[os.path.basename(path)] = _build_index([path])
    core_mods = {os.path.splitext(f)[0]: f for f in CORE}   # 模块名→文件名

    bad = 0
    for path in paths:
        rel = os.path.basename(path)
        try:
            src = open(path, encoding="utf-8").read()
            tree = ast.parse(src)
        except Exception:
            continue
        own = per_file[rel]
        # 本文件的导入可见性（记录**本地别名** → 定义所在文件名）
        imported = {}          # 裸名: from X import a as b
        imported_mods = {}     # 模块本地名: import X [as Y] → X.py 文件名
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in core_mods:
                for al in node.names:
                    imported[al.asname or al.name] = core_mods[node.module]
            elif isinstance(node, ast.Import):
                for al in node.names:
                    if al.name in core_mods:
                        imported_mods[al.asname or al.name] = core_mods[al.name]

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fnc = node.func
            visible = None      # 本调用点可见的定义列表
            if isinstance(fnc, ast.Name):
                name = fnc.id
                if name in own:
                    visible = own[name]
                elif name in imported:
                    visible = per_file[imported[name]].get(name)
            elif isinstance(fnc, ast.Attribute):
                name = fnc.attr
                base = fnc.value
                if isinstance(base, ast.Name) and base.id in imported_mods:
                    visible = per_file[imported_mods[base.id]].get(name)
            if not visible:
                continue
            ok, _ = _call_ok(node, visible)
            if ok:
                continue
            msg = ("%s:%d  %s(%d 位置参数) 期望 %s  定义于 %s"
                   % (rel, node.lineno, name, len(node.args),
                      ", ".join(
                          ("%d" % d["lo"] if d["hi"] == d["lo"]
                           else ("%d+" % d["lo"] if d["hi"] is None
                                 else "%d-%d" % (d["lo"], d["hi"])))
                          for d in visible),
                      "; ".join("%s:%d" % (d["file"], d["line"])
                                for d in visible)))
            if len(visible) > 1:
                _warn.append("[同名多定义] " + msg)
                if VERBOSE:
                    print("   ???? " + msg)
            else:
                print("   FAIL " + msg)
                _fail.append("签名不匹配(运行时TypeError) " + msg)
                bad += 1
    if bad == 0:
        print("   OK   可见范围内无确定性签名错误")


# ---------------------------------------------------------------- 3. 安全不变量
def _read(f):
    return open(_p(f), encoding="utf-8").read()


def _func_body(src, name):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def _func_calls(src, name):
    """函数体内**实际调用**的函数名集合（AST Call 节点）。
    用这个而不是字符串匹配——注释里写着 "retry_queue()" 也会被
    字符串命中（09-30 版门禁的 7 条误报里有 1 条就是这么来的）。"""
    calls = set()
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            target = node
            break
    if target is None:
        return calls
    for node in ast.walk(target):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name):
            calls.add(f.id)
        elif isinstance(f, ast.Attribute):
            calls.add(f.attr)
    return calls


def check_invariants():
    print("== 3. 安全不变量 ==")

    def ck(cond, ok_msg, fail_msg):
        if cond:
            print("   OK   " + ok_msg)
        else:
            print("   FAIL " + fail_msg)
            _fail.append(fail_msg)

    a = _read("a_server.py")
    h = _read("thomson_helper.py")
    w = _read("b_watcher.py")
    ui_h = _read("ui/helper.html")

    # 3.1 能量永不建行：api_energy 体内不得出现 INSERT
    body = _func_body(a, "api_energy")
    ck("INSERT" not in body.upper(),
       "A机 api_energy 无 INSERT（能量永不建行/建表）",
       "A机 api_energy 出现了 INSERT —— 能量绝不允许建行！")

    # 3.2 跨机合并默认关闭
    ck('shot_merge_sec", 0' in a or "shot_merge_sec', 0" in a,
       "A机 SHOT_MERGE_SEC 默认 0（跨机合并默认关）",
       "A机 跨机合并默认值不是 0 —— 连发会被吃掉！")
    ck("if not dup and SHOT_MERGE_SEC > 0" in a,
       "A机 合并分支受 SHOT_MERGE_SEC>0 守卫",
       "A机 合并分支没有 SHOT_MERGE_SEC>0 守卫")

    # 3.3 A机入口发次白名单
    ck("ENFORCE_SHOT_FILE" in a and "SHOT_FILE_RE" in a,
       "A机 api_shot 有发次文件白名单（第二道防线）",
       "A机 缺发次文件白名单校验")

    # 3.4 状态写保护：save_state 受 _STATE_LOADED 守卫
    ck("if not _STATE_LOADED" in h,
       "helper save_state 有 _STATE_LOADED 写保护",
       "helper save_state 缺写保护 —— 测试进程会覆盖生产数据！")
    ck("_STATE_LOADED = True" in h,
       "helper main() 里置位 _STATE_LOADED",
       "helper main() 没有置位 _STATE_LOADED（写保护会让正常运行也存不了盘）")

    # 3.5 发次识别白名单（helper + watcher 都要有）
    ck("def is_shot_file" in h, "helper 有 is_shot_file 白名单",
       "helper 缺 is_shot_file")
    ck("def is_shot_file" in w, "b_watcher 有 is_shot_file 白名单",
       "b_watcher 缺 is_shot_file")

    # 3.6 主循环不得同步调用阻塞函数（AST 调用节点，注释里提到不算）
    mcalls = _func_calls(h, "monitor_loop")
    ck("retry_queue" not in mcalls,
       "monitor_loop 不再同步调 retry_queue（已移后台线程）",
       "monitor_loop 里还有同步 retry_queue() 调用 —— 会阻塞扫描 N×6s！")
    ck("live_fetch_target" not in mcalls,
       "monitor_loop 用异步靶位查询（不阻塞扫描）",
       "monitor_loop 里还有同步 live_fetch_target() 调用 —— 靶系统离线会卡 3s！")
    ck("live_fetch_target_async" in mcalls,
       "monitor_loop 确实在调 live_fetch_target_async",
       "monitor_loop 没调 live_fetch_target_async —— 页面靶位不会刷新？")

    # 3.7 _snapshot 不得再误传参给 effective_target_map
    snap = _func_body(h, "_snapshot")
    ck("effective_target_map(_bound_day())" not in snap,
       "_snapshot 不再误调 effective_target_map(参数)（P0 已修）",
       "_snapshot 又出现 effective_target_map(_bound_day()) —— SSE 会断链！")

    # 3.9 表绑定过期保护（2026-10-23 事故：09-30 测试绑定忘切回，挂 23 天）
    ck("def _binding_expired_check" in h,
       "helper 有绑定过期自动保护（_binding_expired_check）",
       "helper 缺 _binding_expired_check —— 测试绑定忘切回会一直挂旧表！")
    ck("_binding_expired_check" in _func_calls(h, "monitor_loop"),
       "monitor_loop 每轮调用绑定过期检查",
       "monitor_loop 没调 _binding_expired_check —— 过期保护失效！")
    ck('"expired": _binding_is_expired' in h,
       "helper /api/sheetname 与 /api/targetmap 返回 expired 标志",
       "helper API 响应缺 expired 标志 —— 前端无法显示过期警告！")

    # 3.10 monitor_loop 的 done 必须每轮无条件初始化
    # （2026-10-23 事故：done 只在 if all_new: 块内赋值，空轮 UnboundLocalError
    #   每轮抛出，SSE 推送/能量补绑全被跳过，页面静止 31 分钟）
    mloop_src = _func_body(h, "monitor_loop")
    ck("done = []" in mloop_src,
       "monitor_loop 的 done 有无条件初始化（空轮不炸）",
       "monitor_loop 的 done 缺无条件初始化 —— 空轮 UnboundLocalError 会让页面静止！")

    # 3.11 日报表播种回看不得静默失败
    # （2026-10-28 事故：_seed_daily_map 用了未导入的 timedelta，NameError 被
    #   except:pass 吞掉 → 回看永远失败 → 每天午夜新表误走 legacy（09-30 旧
    #   overrides 灌入），10-24~10-28 五张表全被污染）
    ck("from datetime import datetime, timedelta" in h,
       "helper 导入了 timedelta（播种回看依赖它）",
       "helper 缺 timedelta 导入 —— 播种回看会 NameError，每天误走 legacy！")
    seed_src = _func_body(h, "_seed_daily_map")
    ck("timedelta(days=" in seed_src and "except Exception as e" in seed_src,
       "_seed_daily_map 回看异常有日志（不静默吞）",
       "_seed_daily_map 回看异常被静默吞掉 —— 播种选路错误无法察觉！")

    # 3.12 靶类型机理防线（2026-10-28 神秘写入方事故闭环，用户要求"从机理上修复"）
    # a) 上报带溯源：A 机每行能查到类型值出自哪张表哪一代（继承链污染可定位）
    ck("def _tt_provenance" in h and '"tt_src"' in h,
       "helper 上报带 tt_src 溯源（seeded_from+updated）",
       "helper 缺 tt_src 溯源 —— 错值无法定位到播种代际！")
    # b) 对账版本门：helper 无 /api/ver 版本标记（=旧代码）→ 拒绝对账写入
    recon_src = _func_body(w, "reconcile_target_map")
    ck('"/api/ver"' in recon_src,
       "b_watcher 对账前校验 helper /api/ver（旧代码拒绝写入，fail-safe）",
       "b_watcher 对账缺 /api/ver 版本门 —— 旧 helper 的不可信映射会被写进 A 机！")
    # c) 违规写入审计 watchdog：脚本式批量写靶类型自动报警
    ck("def audit_target_type_writes" in w and '"tt_audit"' in w
       and "maybe_audit" in _func_calls(w, "main"),
       "b_watcher 有靶类型违规审计 watchdog 且主循环在调（tt_audit）",
       "b_watcher 缺 tt_audit watchdog 或主循环没调 —— 批量覆盖无人报警！")

    # 3.13 历史回看走日志系统（2026-10-30 用户报"10-28 内容全没了"实为
    #      页面查看日期只过滤本地留痕；本地留痕会被重启/换天清空）
    ck('"/api/a_rows"' in h and "def api_a_rows" in h
       and "renderHistInto" in ui_h and "loadHist" in ui_h,
       "helper 有 /api/a_rows 历史回看代理 + 页面 loadHist/renderHistInto（本地无留痕自动从A机回显）",
       "helper 缺历史回看链路 —— 换天后查历史日期会误显示\"当天没有发次记录\"！")

    # 3.8 UI 与代码分离（根治 git merge 覆盖：改 UI 只碰 ui/*.html，不碰 .py）
    ck("def _load_ui_html" in h and "_UI_PATH" in h,
       "helper UI 已抽离到 ui/helper.html（_load_ui_html 加载）",
       "helper 又把 UI 内嵌回 .py 了 —— git 合并会互相覆盖 UI！")
    ck("def _load_ui_html" in a and "_UI_PATH" in a,
       "A机 UI 已抽离到 ui/index.html（_load_ui_html 加载）",
       "A机 又把 UI 内嵌回 .py 了 —— git 合并会互相覆盖 UI！")
    # 巨型内嵌 HTML 字面量不得复活（HELP_PAGE/PAGE = r\"\"\"<!DOCTYPE）
    ck('HELP_PAGE = r"""' not in h,
       "helper 无巨型内嵌 HELP_PAGE 字面量",
       "helper 出现了内嵌 HELP_PAGE = r\"\"\"<!DOCTYPE —— UI 又被塞回代码！")
    ck('PAGE = r"""' not in a,
       "A机 无巨型内嵌 PAGE 字面量",
       "A机 出现了内嵌 PAGE = r\"\"\"<!DOCTYPE —— UI 又被塞回代码！")
    # ui 模板文件必须存在且带占位符
    for uif, ph in [(os.path.join("ui", "helper.html"), "CFG_SERVER"),
                    (os.path.join("ui", "index.html"), "__COLS__")]:
        p = _p(uif)
        if not os.path.exists(p):
            _fail.append("缺 UI 模板文件 %s" % uif)
            print("   FAIL 缺 UI 模板文件 %s" % uif)
        else:
            body = open(p, encoding="utf-8").read()
            ck(ph in body and body.lstrip().startswith("<!DOCTYPE html>"),
               "%s 存在且含占位符 %s" % (uif, ph),
               "%s 内容异常（缺占位符 %s 或非 HTML）" % (uif, ph))


# ---------------------------------------------------------------- 4. 冒烟 import
def check_smoke():
    print("== 4. 冒烟 import（只加载不调用写盘函数）==")
    # 账本目录指向临时，绝不写生产 ledger
    os.environ["LSL_LEDGER_DIR"] = os.path.join(tempfile.gettempdir(),
                                                "_lsl_check_ledger")
    if BASE not in sys.path:
        sys.path.insert(0, BASE)
    cwd = os.getcwd()
    os.chdir(BASE)
    try:
        import ledger
        ck = all(hasattr(ledger, x) for x in
                 ["append", "log_line", "read_day", "iter_days", "stats"])
        print("   %s   ledger" % ("OK  " if ck else "FAIL"))
        if not ck:
            _fail.append("ledger 缺关键函数")

        import thomson_helper as H
        need = ["is_shot_file", "live_fetch_target_async", "retry_loop",
                "save_state", "report_shot", "make_shot", "_shot_filter",
                "_load_ui_html"]
        miss = [x for x in need if not hasattr(H, x)]
        print("   %s   thomson_helper%s"
              % ("OK  " if not miss else "FAIL",
                 ("（缺 %s）" % miss) if miss else ""))
        if miss:
            _fail.append("thomson_helper 缺 %s" % miss)
        # 写保护默认关（import 不置位）
        if getattr(H, "_STATE_LOADED", True):
            _fail.append("thomson_helper._STATE_LOADED import 后应为 False")
            print("   FAIL _STATE_LOADED import 后不是 False")
        else:
            print("   OK   _STATE_LOADED import 后为 False（写保护生效）")

        import b_watcher as W
        miss = [x for x in ["is_shot_file", "send_tif_timeline", "led"]
                if not hasattr(W, x)]
        print("   %s   b_watcher%s"
              % ("OK  " if not miss else "FAIL",
                 ("（缺 %s）" % miss) if miss else ""))
        if miss:
            _fail.append("b_watcher 缺 %s" % miss)

        import a_server as A
        miss = [x for x in ["SHOT_MERGE_SEC", "SHOT_FILE_RE",
                            "ENFORCE_SHOT_FILE", "aled", "_load_ui_html"]
                if not hasattr(A, x)]
        print("   %s   a_server%s"
              % ("OK  " if not miss else "FAIL",
                 ("（缺 %s）" % miss) if miss else ""))
        if miss:
            _fail.append("a_server 缺 %s" % miss)
        if getattr(A, "SHOT_MERGE_SEC", -1) != 0:
            _fail.append("a_server.SHOT_MERGE_SEC 运行时不是 0")
            print("   FAIL SHOT_MERGE_SEC=%r（应为 0）" % A.SHOT_MERGE_SEC)

        import rebuild_from_ledger as R
        miss = [x for x in ["cmd_report", "cmd_apply", "cmd_pull_a", "cmd_tif"]
                if not hasattr(R, x)]
        print("   %s   rebuild_from_ledger%s"
              % ("OK  " if not miss else "FAIL",
                 ("（缺 %s）" % miss) if miss else ""))
        if miss:
            _fail.append("rebuild_from_ledger 缺 %s" % miss)
    except Exception as e:
        print("   FAIL 冒烟 import 抛异常: %r" % e)
        _fail.append("冒烟 import 异常 %r" % e)
    finally:
        os.chdir(cwd)


# ---------------------------------------------------------------- main
def main():
    print("门禁检查开始（仓库 %s）\n" % BASE)
    check_syntax()
    print()
    check_signatures()
    print()
    check_invariants()
    print()
    check_smoke()
    print("\n" + "=" * 56)
    if _warn:
        print("可疑（不阻断，%d 条）：" % len(_warn))
        for x in _warn[:20]:
            print("  - " + x)
        if len(_warn) > 20:
            print("  …另 %d 条（加 -v 看签名类明细）" % (len(_warn) - 20))
    if _fail:
        print("❌ 门禁未通过，%d 条必须修复后才许 push：" % len(_fail))
        for x in _fail:
            print("  ✗ " + x)
        return 1
    print("✅ 门禁全部通过，可以提交/推送。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
