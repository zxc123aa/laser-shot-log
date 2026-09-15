# 部署与一键启动指南

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
