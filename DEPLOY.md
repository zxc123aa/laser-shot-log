# 部署与一键启动指南

## 机器分工与防旧进程规范（2026-10-28 定稿，红线必读）

**机器分工（谁干什么）**

| 机器 | 跑什么 | 职责 |
|---|---|---|
| **A 机 10.0.23.155** | `start_a_server.bat` | 日志总表服务(8765)，唯一建行入口（api_shot）；能量按**时间最近**绑定，绝不改写行 No. |
| **B 机 DESKTOP-6RGKBS8** | `start_real.bat` | `b_watcher` 监视谱仪 PNG **建行上报**（confirm 制）+ sheet_backup + 8767 上报确认页 |
| **C 机 10.0.23.117** | 桌面`C机-一键启动.bat` | **只跑 helper(8767) + sheet_backup，不跑 b_watcher**。行全由 B 机建；C 机只做 PyTPS 截止能量上报 |

**防旧进程三条纪律（target_type 覆盖事故的教训，10-23 旧 helper 跑了 5 天旧代码）**

1. **改代码/配置 = 必须重启对应进程**。helper/b_watcher/a_server 都是常驻进程，
   `git pull` 不重启等于没更新，旧逻辑会一直跑到天荒地老。
2. **启动脚本先杀旧实例**：一键启动 bat 一律"先杀后启"（按端口/窗口标题 taskkill），
   保证跑起来的永远是刚 pull 的代码。不要用"已在运行就跳过"的旧逻辑。
3. **版本可核对**：helper 暴露 `GET /api/ver` 返回 `code_ver`（如 `2026-10-28.d9c185b`），
   改 helper 代码时必须同步更新 `CODE_VER` 常量；健康检查用它与 git HEAD 比对，
   不一致 = 旧进程，立即重启。

**C 机额外红线**

- C 机不建行：`config_helper.json` 保持 `report_shots: false`、`report_mode` 无关紧要
  （b_watcher 已从 C 机退役，勿再启动）
- PyTPS 截止能量 → `8767 /api/energy_remote`（按文件名匹配）→ A 机时间最近绑定；
  能量落 `tps_h` 列。对接代码镜像在本仓库 `pytps/`（真源仓库 zxc123aa/Pytps）

## 温馨提示：target_type 污染与 C 机数据防线（2026-10-28 排查复盘）

**C 机"永不建行"的三重防线**（改动任一处前先想清楚）

1. `config_helper.json` → `report_shots: false`：helper 建行分支硬性跳过，直接标记
   reported 转入能量绑定
2. 补发队列（state_helper.json 的 `queue`）必须为空——若 C 机曾跑过 direct 模式，
   队列里残留的旧报文被 retry 重发时会绕过开关直接建行。检查命令：
   `python -c "import json;print(len(json.load(open('state_helper.json'))['queue']))"`
3. A 机 `/api/energy` 无 create 入口（只有"绑定已有行 / no_match"两种结局）；
   建行唯一路径是 `/api/shot`，C 机代码不存在调用它的活分支

**target_types/ 日报表的三个特性（踩过坑的）**

- **播种只在文件缺失时发生**：改坏/想重置某天的表 → 直接删掉那个 json，
  下次访问自动重播（xls 比最近日报表新 → xls 接管；否则沿用最近一天；全无 → legacy 基表+当前 overrides）
- **冻结表会向后传染**：播种回看"最近一天"，一个 15 天没更新的 stale 表会一路
  传给之后每一天。发现 `seeded_from: legacy` 且多天字节相同 → 就是它
- **清理前必须备份**：2026-10-28 清理的 18 个污染表在 `backup/target_types_stale_20261028/`，
  可对照/回滚

**排障速查**

- 核对 helper 是否旧进程：`curl --noproxy "*" http://127.0.0.1:8767/api/ver`
- 触发当日表重播：`curl --noproxy "*" http://127.0.0.1:8767/api/targetmap`
- 遗留事项：A 机行上被旧覆盖逻辑写坏的 target_type，需 A 机 agent 用 map 修复
  历史（git 7108884 / 50449fb 那批）+ 回收站核对回填

## 常用地址（固定，内网任何电脑浏览器直接打开）

| 系统 | 地址 | 用途 |
|---|---|---|
| **实验日志系统（A 机）** | http://10.0.23.155:8765/ | 查看/编辑打靶日志总表 |
| **能量上报确认页（本机 157）** | http://10.0.23.157:8767/ | 117 等电脑填 闪烁光纤 / TPS 能量、确认上报发次 |

> 上报页 8767 已放行防火墙入站；本机重启后双击 `start_real.bat` 即恢复，
> 地址不变。能量列在 `config_helper.json` 的 `energy_fields` 里增删。

## 一键启动（本机已部署的情况下）

| 角色 | 双击 | 启动内容 |
|---|---|---|
| **B 机（数据机）** | `start_real.bat` | b_watcher 打靶监视 + sheet_backup 表格备份 + 能量填报页(8767)，三个最小化窗口 |
| B 机（只跑备份） | `start_backup.bat` | 仅表格备份 |
| **A 机（主控服务器）** | `start_a_server.bat` | a_server.py，页面 http://<A机IP>:8765 |

> 测试 / 演示工具全部在 `test\` 子目录（`start_all.bat` 演示模式、`start_simulator.bat`
> 发次模拟器、`start_test_gen.bat` 假发次生成器、`one_pc_test.bat` 单机演示），
> 实验时别开。

- 所有 bat 都用 `py` 启动器，新电脑只要有 Python 3.8+（含 tkinter 更佳）即可
- B 机首次部署：复制 `config_b.local.json`（模板见 `config_b.json`），改 `watch_dirs`（谱仪落盘目录）和 `server_url`（A 机地址）
- 汤姆逊能量页首次启动会生成 `config_helper.json`，改好 `server_url` 后重跑
- ⚠ b_watcher 千万**不要双开**（两个实例会把同一发重复上报两行）

## 新电脑从零部署（3 步）

```
1. git clone https://github.com/zxc123aa/laser-shot-log.git D:\software\laser-shot-log
2. 照 config_b.json 复制出 config_b.local.json，填真实目录和 A 机 IP
3. 双击 start_real.bat
```

靶位采集（连 116 电脑）额外需要：一个装了 `protobuf==3.20.3` 的 Python，
路径填到 `config_helper.json` 的 `target_monitor.python`。

## 更新（所有电脑统一）

改了代码的电脑：`git add -A && git commit -m "..." && git push`
其他电脑：`git pull` 然后重启对应服务（关掉旧窗口重新双击 bat）。

> 配置文件 `config_*_local*.json` 与运行状态 `*_state*.json`、`backup_state.json`
> 不入 git，各机器互不干扰。

## 本机（DESKTOP-6RGKBS8）修复记录（2026-09-15）

- 全部 bat 改用 `py` 启动器（旧电脑写死的路径已清除），注释改 ASCII 防乱码
- 新增 `sheet_backup.py`：每 30s 自动备份所有表格到 `shotlist\<日期>\*.xlsx`；
  单表被占用不影响其他表，占用解除自动补写
- `thomson_helper.py`：tkinter 探测路径修复（浏览按钮可用）
- `a_server.py`：告警横幅新增"清除告警"按钮（POST /api/alerts/clear）

---

## 📨 给 A 机 agent 的话（2026-09-16，来自 B 机 DESKTOP-6RGKBS8）

接管 A 机（10.0.23.155）前请先读这段。

**1. 先更新**
`git pull` 到 ≥ `b906a18`，关掉旧窗口重新双击 `start_a_server.bat`。
更新后你才有：告警"清除"按钮、`/api/sheets` 表格代理接口、最新的字段合并逻辑。

**2. 上报流程已改：现在是"人工确认制"（重要）**
B 机的 b_watcher **不再自动上报**（`report_mode: "confirm"`）。发次检测到后先进
B 机本机 8767 页面的"待确认"列表，**实验人员点「确认上报」后才会调你的
`/api/shot`**——所以表里出现新行 = 有人确认过，没有"莫名冒出的行"才是正常状态。
- 确认时可以**没有能量**（先建行，能量事后经 `/api/energy` 绑定/覆盖）
- B 机 8767 不在线时发次暂存在 B 机队列，恢复后自动补投

**3. 去重规则（没变，但要对齐理解）**
machine + shot_time + 首文件名 唯一。确认上报行的 machine 统一用 b_watcher 的
机名（`DESKTOP-6RGKBS8`），与旧直报记录同键，重复确认不会产生重复行。

**4. 表格绑定由 B 机控制**
B 机 8767 页面可随时切换 `sheet_name`（空=实时打靶 / `@date`=按打靶日分表 /
固定表名），`/api/shot` 带 sheet_name 你就自动建表，无需 A 机侧配置。

**5. 靶位/离焦字段**
来自 10.0.23.116:5362 重频靶系统（需 `protobuf==3.20.3`，路径配在 B 机
`config_helper.json`）。靶系统离线时行里缺这两列是正常的，不是丢数据。

**6. 待办与红线**
- ⚠ **测试数据别动**：实时打靶表 12 条 + 2026-09-15 表 2 条（shot-13/14，
  9-15 深夜测试），等用户明确说"清理测试数据"才能删
- B 机本地 Excel 备份由 B 机的 `sheet_backup.py` 负责，A 机不用跑
- 告警横幅若堆着旧的"监视目录不存在"，用新按钮清掉即可
- 千万别在 B 机双开任何启动 bat（同一发会报两行）

**7. 各机一键入口（写进桌面快捷方式了）**
- B 机：`start_real.bat`（监视 + 备份 + 8767 上报确认页）
- A 机：`start_a_server.bat`（就这一个，前后端一体）

有疑问先看本文件上方《部署与一键启动指南》和 README，再问用户。
