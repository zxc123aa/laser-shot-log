# -*- coding: utf-8 -*-
"""
单机演示 / 单机部署
==================
只有一台电脑也能跑全套：A 机（日志服务器）和 B 机（数据监视器）
就是本机上的两个进程，server_url 填 127.0.0.1 即可。

双击 one_pc_test.bat 运行本脚本，它会：
  1. 在 one_pc_test_run\\ 目录里隔离地启动 A 机 + B 机
  2. 自动打开浏览器 http://127.0.0.1:8765/ 看实时日志表格
  3. 按提示按回车，模拟"激光打靶"（谱仪1/谱仪2 落盘文件）
  4. 几秒后网页上自动出现对应的打靶日志
  5. 结束时自动清理所有测试产物

真实部署（单机）：不跑本脚本，直接双击 start_a_server.bat + start_b_watcher.bat，
把 config_b.json 的 server_url 写成 http://127.0.0.1:8765、
watch_dirs 写成真实的数据目录即可。
"""

import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
RUN = os.path.join(BASE, "one_pc_test_run")   # 所有测试产物隔离在此目录


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def main():
    py = sys.executable
    no_browser = "--no-browser" in sys.argv

    # ---------- 1. 准备隔离测试环境 ----------
    if os.path.exists(RUN):
        try:
            shutil.rmtree(RUN)
        except PermissionError:
            log("旧测试目录被占用，请先关闭相关进程后重试")
            sys.exit(1)
    data = os.path.join(RUN, "test_data")
    s1 = os.path.join(data, "谱仪1")
    s2 = os.path.join(data, "谱仪2")
    os.makedirs(s1)
    os.makedirs(s2)
    # 一条"历史旧文件"，验证首启不把旧数据当新实验
    with open(os.path.join(s1, "历史数据_昨天.dat"), "w") as f:
        f.write("old")

    shutil.copy(os.path.join(BASE, "a_server.py"), RUN)
    shutil.copy(os.path.join(BASE, "b_watcher.py"), RUN)
    with open(os.path.join(RUN, "config_b.json"), "w", encoding="utf-8") as f:
        json.dump({
            "watch_dirs": [s1, s2],
            "server_url": "http://127.0.0.1:8765",
            "machine_name": "单机测试-B",
            "scan_interval_sec": 1,
            "group_window_sec": 4,
        }, f, ensure_ascii=False, indent=2)

    # ---------- 2. 启动 A 机（日志服务器）----------
    log("启动 A 机（日志服务器 :8765）...")
    srv = subprocess.Popen([py, "a_server.py"], cwd=RUN)
    time.sleep(1.5)

    # ---------- 3. 启动 B 机（数据监视器）----------
    log("启动 B 机（数据目录监视器）...")
    wch = subprocess.Popen([py, "b_watcher.py"], cwd=RUN)
    time.sleep(2)

    log("=" * 56)
    log("A 机 + B 机 已在本机同时运行（单机 = 双进程）")
    log("浏览器打开  http://127.0.0.1:8765/  看实时打靶日志")
    log("=" * 56)
    if not no_browser:
        webbrowser.open("http://127.0.0.1:8765/")

    # ---------- 4. 交互式模拟打靶 ----------
    try:
        input("\n>> 按回车模拟【第 1 次打靶】(谱仪1+谱仪2 同时落盘)...")
        with open(os.path.join(s1, "spec1_shot001.dat"), "w") as f:
            f.write("data")
        time.sleep(1)
        with open(os.path.join(s2, "spec2_shot001.dat"), "w") as f:
            f.write("data")
        log("第 1 枪已落盘 → 约 5 秒后网页出现 1 条日志（2 个文件合并）")
        time.sleep(6)

        input("\n>> 按回车模拟【第 2 次打靶】(仅谱仪1落盘)...")
        with open(os.path.join(s1, "spec1_shot002.dat"), "w") as f:
            f.write("data")
        log("第 2 枪已落盘 → 约 5 秒后网页再出现 1 条日志")
        time.sleep(6)

        input("\n>> 按回车结束演示（自动清理）...")
    except (KeyboardInterrupt, EOFError):
        pass

    # ---------- 5. 清理 ----------
    log("停止进程并清理...")
    for p in (wch, srv):
        try:
            p.terminate()
        except Exception:
            pass
    time.sleep(1)
    try:
        shutil.rmtree(RUN)
        log("已清理测试目录 one_pc_test_run")
    except PermissionError:
        log("测试目录 one_pc_test_run 未删净，可手动删除（无副作用）")


if __name__ == "__main__":
    main()
