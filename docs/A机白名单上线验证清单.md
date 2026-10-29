# A 机白名单上线验证清单（2026-10-30）

> 背景：`211d62b` 给 A 机 `/api/field` 加了 TT_WRITERS 白名单（写 target_type 只允许
> B机 10.0.23.157；127.0.0.1 本地豁免；C机 10.0.23.117 直接拒 + `a_field_reject` 留证）。
> **代码已入库但 A 机进程还是旧的（PID 60576，22:34 启动）→ 白名单未生效。**

## 第 0 步：重启（A 机操作员，唯一手动步骤）

双击桌面「激光打靶日志系统-部署.bat」（自带 git pull + 杀旧 + 起新）。

## 第 1 步：确认新进程真的起来了（A 机 agent）

```
netstat -ano | findstr ":8765 " | findstr LISTENING     # 记下新 PID
wmic process where processid=<PID> get creationdate      # 启动时间应晚于重启时刻
```

（或用 python ctypes GetProcessTimes；注意返回的是 UTC，+8 换算本地。）

## 第 2 步：确认白名单上线（关键实锤）

C机对账约 90s 一拍会继续尝试写 target_type，白名单生效后会被拦：

```
curl "http://127.0.0.1:8765/api/ledger?day=2026-10-30&ev=a_field_reject"
```

- **重启后 ≤2 分钟内出现 `a_field_reject` 记录** → 白名单生效实锤（rejected_value
  应为 ch50nm 10° 之类，client_ip=10.0.23.117）；
- 长时间 0 条也说明 117 已停写（可能已被 B/C 侧修掉），两种结果都算通过，
  以 A 机进程启动时间晚于代码 mtime 为准。

## 第 3 步：确认 10-29 清理成果保持

1000 行（3-10 空发）已于 10-30 00:46 清空 + `target_type_lock=true`
（备份 `backup/target_type_fix_20261029.json`，a_repair 事件 1000 条）：

```
curl "http://127.0.0.1:8765/api/rows?sheet=2026-10-29" | 抽查 no=186.. 的 target_type 应为空
```

## 第 4 步：能量链路（C 机侧待办）

账本实锤：**10-28 有 54 条能量写入，10-29 起全天 0 条** → C机 PyTPS 能量上报链路
停了。C机排查恢复后，验证：

```
curl "http://127.0.0.1:8765/api/ledger?day=<日期>&ev=a_energy_in"
```

重新出现即恢复（注意：能量走 /api/energy，不受白名单影响）。
