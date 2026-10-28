# PyTPS 汤姆逊谱仪程序 —— 日志系统对接镜像

> **源头仓库**：https://github.com/zxc123aa/Pytps （真源，提交历史在那边）
> **本目录性质**：镜像快照，方便日志系统仓库内追溯"上报端到底发了什么"。
> 改 PyTPS 代码请去源头仓库改，改完把 5 个 .py 同步拷贝到这里再提交本仓库。

## 运行位置与职责

- **C 机（IP 117）**：`D:\software\new TPS\Pytps\` 下运行 PyTPS.py（解谱 + 截止能量确认）。
  C 机只发能量，**不建行**（行由 B 机 b_watcher 上报）。

## 与日志系统的对接点（PyTPS.py）

| 位置 | 作用 |
|---|---|
| 顶部常量 `LOG_HELPER_URL / LOG_SERVER_URL / LOG_ENERGY_FIELD` | 本机 helper 8767 / A 机 155:8765 / 能量落列 `tps_h` |
| `rememberCurrentCutoff()` | 「确认截止能量」按钮：取**手动光标**处能量 → 记忆 → 后台线程上报 |
| `reportCutoffToLogSystem(filename, energy)` | ①优先 POST `8767 /api/energy_remote`（body: filename/energy/field），helper 按文件名匹配发次后走标准绑定链；②helper 不可达时兜底直连 A 机 `/api/energy`（用 tif 自身 mtime 作 shot_time） |

## 依赖的日志系统侧行为（变更需两端同步）

- helper `report_shots=false`（C 机模式）：只发能量，永不建行
- helper `match_window_sec=60`：时间窗最近绑定
- A 机 `/api/energy`：**时间最近优先**（TPS 的 No 可能断号错位），精确 No. 仅兜底，
  **绝不改写行已有 No.**；窗口未中返回 no_match

## 版本纪律（防旧进程）

改了 PyTPS.py 或 helper 代码后：**重启对应进程才算生效**。
C 机一键启动脚本会先杀 8767 旧实例再拉新 helper；若手动启动，务必先确认旧进程已退出
（`netstat -ano | findstr 8767`）。
