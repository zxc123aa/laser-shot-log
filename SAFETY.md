# 开发安全规范（laser-shot-log）

> 每一条规则背后都是一次真实事故。规则不是建议，是**硬约束**：
> 违反其中带 ⛔ 标记的条目 = 禁止 push；`check_all.py` 门禁会拦下大部分。
>
> 事故档案：2026-09-29 盘号键污染映射表 / 双进程抢 8767；2026-09-30
> `_snapshot` TypeError 打断 SSE、跨机合并吃掉连发、能量误建行争议；
> 2026-10-01 测试进程把空 STATE 写进生产 state_helper.json（289 发次被清）、
> 监视目录 1517 个非发次 PNG 险被误识别、A 机 UI 被 B 机旧版合并覆盖。

---

## 0. 提交前门禁（每次 push 前必跑）⛔

```bash
python check_all.py        # 退出码 0 才许 push；-v 看可疑项明细
```

四段检查：语法+编译 → 函数签名核对（调用点可见性感知，不会误报
`subprocess.run`）→ 20 条安全不变量 → 冒烟 import（账本目录指临时路径，
绝不写生产数据）。**任何 FAIL 必须修复后才许 push，没有例外。**

新增业务规则时，同步在 `check_invariants()` 里加一条断言——
规范只有变成代码才算数，写在文档里的规范会被遗忘。

---

## 1. 多机协作（A/B/C 三机共用一个仓库）

### 1.1 分支纪律 ⛔
- **禁止 `git rebase`**（本机两次 rebase 挂起且损毁 .git）。整合远程只用
  `git fetch` + `git merge --no-edit`。
- push 被拒 → 先 fetch 看远程新提交 → merge → 重跑门禁 → 再 push。
- 部署口径：**以 B 机为准 push，其他机 pull**。A/C 机本地改动必须先推给
  B 机合并，不许各自直接改同一文件的同一段。

### 1.2 UI 与代码分离 ⛔（根治"A 机 UI 被 B 机旧版覆盖"）
- 前端模板是**独立文件**：`ui/index.html`（A 机主页面）、
  `ui/helper.html`（B 机 8767 页面）。改 UI 只许改这两个文件。
- **禁止把 HTML 内嵌回 .py**（门禁 3.8 会拦：出现 `HELP_PAGE = r"""` /
  `PAGE = r"""` 即 FAIL）。内嵌 4 万字符字面量 = 任何 .py 的合并都会
  连坐 UI，两台机各改一处必炸。
- .py 侧通过 `_load_ui_html()` 读取（mtime 缓存，改模板即生效不必重启；
  文件缺失降级为错误页，绝不抛异常打断 HTTP 通道）。
- 占位符是契约：helper 页 `CFG_SERVER/CFG_MACHINE/CFG_DIRS/
  CFG_EFIELDS_JSON/CFG_WINDOW`；A 机页 `__COLS__`。改占位符必须同时改
  .py 里的 `.replace(...)` 调用与 ui 文件，两边一起提交。

### 1.3 进程纪律 ⛔
- 重启前先 `tasklist` 查重：双 watcher = 重复扫描，双 helper = 抢 8767 端口
  且"新代码看似在跑、实际服务的是旧进程"（09-29 实测）。
- 重启后必须做**行为级验证**（如 POST 一个 `nan` 看是否被清洗），
  只看端口在线不算数。
- 本机（B 机）服务重启一律由用户双击 `start_real.bat` /
  `start_a_server.bat`；AI 沙箱起的进程会随会话回收，禁止代起。

---

## 2. 业务不变量（门禁第 3 段固化，违反 ⛔）

### 2.1 发次条目唯一基准
- **一个 `shotxxx.png` 文件 = 一条 shot 记录**。B 机绑定目录里 PNG 的
  **浮点 mtime 是全系统唯一时间基准**；C 机 tif 时间线、能量绑定、
  重建对账全部对齐到它。
- 同机连发**永不合并**；跨机合并默认关闭（`shot_merge_sec=0`，
  09-30 的 shot79/80 被吃就是合并干的）。改非 0 需要三机同时知情。

### 2.2 发次文件白名单 ⛔
- 只有匹配 `shot\d+.png` / `shor\d+.tif` 等白名单模式的文件才建条目
  （`is_shot_file()`，helper 与 b_watcher 双端一致，config 可调）。
- 监视目录是**共享目录**（实测有 1517 个高倍靶前/远场 PNG），黑名单
  永远挡不住新干扰文件，白名单才是一劳永逸。新增合法命名 → 改
  `config_b.json` 的 `shot_file_patterns`，不许放宽成黑名单模式。
- A 机 `/api/shot` 入口有第二道防线（`ENFORCE_SHOT_FILE`），
  files 为空或文件名不合白名单 → 记账 `a_shot_reject` 并拒绝建行。

### 2.3 能量永不建行、永不建表 ⛔
- C 机 TPS 能量只**绑定**到既有 shot 行（时间窗最近优先，No. 精确匹配
  兜底）。`api_energy` 体内禁止出现 INSERT / CREATE（门禁 3.1）。
- 未命中 → 暂存 `STATE.unmatched`，等 tif 时间线/发次建组后自动补绑。
  前端只有"重试绑定"，没有"补录建表"。
- TPS 可能触发失败/断号：能量缺失只影响该行能量列，**绝不允许**因此
  多建、少建、合并任何行。

### 2.4 行号与 rev
- 行号是身份，**永不改写**。字段更新走 `/api/field {id, field, value, rev}`
  乐观锁；每写一个字段 rev 就变，逐字段写必须每字段重取行。
- `file_count` 不可改；rebuild apply 改 fc 需 `--force-fc` 删行重建。

### 2.5 Nan 垃圾值
- 页面填 `nan` = "空/未填"（操作员约定）。`_clean_type_val()` 四处生效
  （lookup / 日报表读盘 / 播种 / api_targetmap_set），前后端同款。

---

## 3. 生产数据保护 ⛔（10-01 空盘事故教训）

- **任何测试/调试进程禁止 import 后触发写盘路径**。`save_state()` 有双重
  保护：`_STATE_LOADED` 标志（import 不置位 → 拒绝写）+ 空数据保护
  （内存为空而盘上非空 → 拒绝覆盖）。逃生门 `LSL_FORCE_EMPTY_STATE=1`
  只在明确要清盘时用，用完即关。
- 测试脚本必须：账本指临时目录（`LSL_LEDGER_DIR=<tmp>`）、state 指临时
  路径、绝不 chdir 到生产目录后调 save。`check_all.py` 的冒烟段就是范例。
- **动生产文件前先备份**；备份文件名带日期后缀（如
  `state_helper.json.EMPTY_BY_AI_20261001`），保留为证据，不许静删。
- 恢复手段优先级：运行中实例内存 > ledger 重放 > backup/ 快照。
  10-01 就是用运行中实例的 `/api/ignore` + `/api/trash restore` 闭环
  找回 289 发次的。

---

## 4. 时序预算（"百 ms 级监测"是硬指标）⛔

- `monitor_loop` 单轮扫描预算 **< 100ms**（实测冷缓存 48ms / 热 0.3ms）。
  主循环内**禁止**：同步 HTTP（含 `retry_queue()`、同步
  `live_fetch_target()`）、`subprocess.run`、任何秒级阻塞。
  网络类工作一律后台线程/异步（`retry_loop(gap=30)`、
  `live_fetch_target_async()` + `_LIVE_INFLIGHT` 防叠加）。门禁 3.6 用
  AST 调用节点检查（注释里提到函数名不算）。
- 页面实时性依赖 SSE：`/api/events` 与 `/api/local` 共用的 `_snapshot()`
  **绝不允许抛异常**（09-30 一个 TypeError 就让页面退化成十几秒轮询，
  被当成"监测延迟"）。取数必须 try/except 降级为空。
- 改扫描/推送链路后，必须实测单轮耗时并在提交说明里写数字。

---

## 5. 证据规则（"重建表格证据不足"的根治）

- **ledger 是唯一真源**：`ledger/YYYY-MM-DD.jsonl`（B/C 机）+
  `ledger_a/`（A 机）。只追加、永不重写，flush+fsync+线程锁。
  每个业务动作都要有对应事件（shot_group / detect_in / report_* /
  energy_* / tif_timeline / a_* / reconcile）。
- 新增任何"改变 A 机表格"的代码路径，必须**同 PR 内**加对应 ledger 埋点，
  否则将来无法对账 = 证据不足 = 禁止合入。
- 重建走 `rebuild_from_ledger.py`：`report`（只读对账，默认）→
  人工核对差异 → `apply --yes`（重放，A 机去重键幂等）。
  跨机能量用 `pull-a` 回灌。不允许绕过账本手工改表。
- 拒绝也是证据：A 机拒收的发次记 `a_shot_reject`，被忽略的文件记
  `note_ignored`，不许静默丢弃。

---

## 6. 靶类型映射维护（09-29 盘号/靶位混淆教训）

- 键必须是**靶位 ID（行-列）**，一个键一个靶位；盘号只是页面标签，
  单发盘号时后端自动展开成 8 个靶位（盘行 N → 行 2N-1/2N；槽 P →
  列 4P-3~4P）。
- 填值/修值永远**按盘为单位**：盘内任一位置有值 → 传播到盘内全部空格；
  盘内出现多个不同值 → 停下问用户，不自动裁决。
- 生效映射 = `target_types/日期.json` 唯一本体；周期对账
  （`reconcile_target_map`，90s）以映射覆盖 A 机日志行的不一致值
  （用户明确要求的覆盖语义），映射没有的靶位只清 nan。
- 绑定测试日期表后**测完必须切回 @date**，否则真实发次全进测试表。

---

## 7. AI 开发会话守则

- 本机 bash/PowerShell PATH 异常：一律用 Python 全路径；git 用
  PortableGit 全路径且经 `subprocess` 调用；本地 urllib 要
  `ProxyHandler({})` 绕系统代理。
- 改 `thomson_helper.py` / `b_watcher.py` / `a_server.py` / `ui/*` 后：
  跑门禁 → 提醒用户重启对应 bat → 用户重启后做行为级验证，三步缺一不可。
- 每次实质改动记入 `.workbuddy/memory/YYYY-MM-DD.md`（append-only）。
- 大规模重构（如 UI 抽离）必须：改动前 .bak 备份 → 脚本化迁移（禁手抄）
  → 字节级回读比对 → 行为级验证 → 门禁 → 单独 commit。
