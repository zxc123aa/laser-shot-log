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
