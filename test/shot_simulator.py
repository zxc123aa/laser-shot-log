# -*- coding: utf-8 -*-
"""
发次模拟器（B机测试工具）
==========================
模拟 TPS 谱仪按日期目录落盘：每点一次"打一发"，在
  <基础目录>\\<YYYY>\\<YYYYMMDD>\\
下生成 shot-N 的 PNG 图片（自动递增发次号）。

b_watcher 会把相邻 ≤8 秒的文件归为同一发，因此：
  - 一发内的多张图会在 1 秒内写完（归为一发）
  - 连续两次"打一发"请间隔 8 秒以上，否则会被合并成一发
  - "自动连打"模式默认间隔 15 秒，已避开该限制

纯标准库（tkinter / zlib / struct），无第三方依赖。
运行： python shot_simulator.py   或双击 start_simulator.bat
"""

import json
import os
import struct
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
import zlib

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE, "simulator_state.json")
DEFAULT_BASE_DIR = r"D:\data117\TPS"


# ---------- PNG 生成（纯标准库） ----------

def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def make_shot_png(shot_no, width=160, height=120):
    """生成一张带发次条纹标识的渐变测试图（RGB PNG）"""
    rows = []
    bars = shot_no % 8  # 用底部条纹数量粗略标识发次
    for y in range(height):
        row = b"\x00"  # filter: none
        for x in range(width):
            r = (x * 255) // width
            g = (y * 255) // height
            b = 120
            if y >= height - 16 and bars and (x // (width // 8)) < bars:
                r, g, b = 255, 40, 40  # 底部红色条纹 = 发次模 8
            row += bytes((r, g, b))
        rows.append(row)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
            + _chunk(b"IEND", b""))


# ---------- 发次号持久化 ----------

def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"shot_no": 0}


def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_PATH)


# ---------- GUI ----------

class SimulatorApp:
    def __init__(self, root):
        self.root = root
        root.title("发次模拟器 - TPS 谱仪落盘测试")
        root.geometry("660x520")
        self.state = load_state()
        self.auto_on = False
        self._build()

    # ----- 界面 -----
    def _build(self):
        pad = {"padx": 8, "pady": 4}

        top = ttk.LabelFrame(self.root, text="落盘设置")
        top.pack(fill="x", **pad)

        ttk.Label(top, text="基础目录:").grid(row=0, column=0, sticky="e", padx=6, pady=4)
        self.dir_var = tk.StringVar(value=DEFAULT_BASE_DIR)
        ttk.Entry(top, textvariable=self.dir_var, width=46).grid(row=0, column=1, padx=4, pady=4)
        ttk.Label(top, text="(自动按 年\\日期 建子目录)").grid(row=0, column=2, sticky="w")

        ttk.Label(top, text="实验日期:").grid(row=1, column=0, sticky="e", padx=6, pady=4)
        self.date_var = tk.StringVar(value=datetime.now().strftime("%Y%m%d"))
        ttk.Entry(top, textvariable=self.date_var, width=14).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(top, text="(默认今天，可改成任意日期如 20260901，写入 基础目录\\年\\日期)").grid(row=1, column=2, sticky="w")

        ttk.Label(top, text="目标目录:").grid(row=2, column=0, sticky="e", padx=6, pady=4)
        self.target_label = ttk.Label(top, foreground="#06c", text=self._target_dir())
        self.target_label.grid(row=2, column=1, columnspan=2, sticky="w", padx=4)
        self.dir_var.trace_add("write", lambda *_: self._refresh_target())
        self.date_var.trace_add("write", lambda *_: self._refresh_target())

        ttk.Label(top, text="每发文件数:").grid(row=3, column=0, sticky="e", padx=6, pady=4)
        self.nfiles_var = tk.IntVar(value=1)
        spin = ttk.Spinbox(top, from_=1, to=5, width=6, textvariable=self.nfiles_var)
        spin.grid(row=3, column=1, sticky="w", padx=4)
        ttk.Label(top, text="(1 发写多张图仍归为一发)").grid(row=3, column=2, sticky="w")

        mid = ttk.LabelFrame(self.root, text="发次控制")
        mid.pack(fill="x", **pad)

        self.shot_label = ttk.Label(
            mid, text="下一发: shot-%d" % (self.state["shot_no"] + 1),
            font=("Microsoft YaHei", 14, "bold"))
        self.shot_label.pack(pady=4)

        btns = ttk.Frame(mid)
        btns.pack(pady=4)
        self.fire_btn = ttk.Button(btns, text="⚡ 打一发", command=self.fire_once)
        self.fire_btn.pack(side="left", padx=6)

        self.auto_btn = ttk.Button(btns, text="▶ 自动连打 (15s)",
                                   command=self.toggle_auto)
        self.auto_btn.pack(side="left", padx=6)

        self.reset_btn = ttk.Button(btns, text="发次号归零", command=self.reset_shot)
        self.reset_btn.pack(side="left", padx=6)

        ttk.Label(mid, foreground="#a00",
                  text="提示：相邻 ≤8 秒落盘的文件会并作同一发；两次打一发请间隔 8 秒以上").pack()

        logf = ttk.LabelFrame(self.root, text="落盘日志")
        logf.pack(fill="both", expand=True, **pad)
        self.logbox = ScrolledText(logf, height=14, font=("Consolas", 9))
        self.logbox.pack(fill="both", expand=True)
        self._log("就绪。基础目录: %s" % DEFAULT_BASE_DIR)

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.logbox.insert("end", "[%s] %s\n" % (ts, msg))
        self.logbox.see("end")

    # ----- 动作 -----
    def _target_dir(self):
        """计算目标目录（不创建）"""
        raw = "".join(ch for ch in self.date_var.get() if ch.isdigit())[:8]
        if len(raw) != 8:
            raw = datetime.now().strftime("%Y%m%d")  # 日期无效时回退到今天
        return os.path.join(self.dir_var.get().strip(), raw[:4], raw)

    def _refresh_target(self):
        self.target_label.config(text=self._target_dir())

    def _today_dir(self):
        d = self._target_dir()
        os.makedirs(d, exist_ok=True)
        return d

    def fire_once(self):
        shot = self.state["shot_no"] + 1
        try:
            n = max(1, int(self.nfiles_var.get()))
        except Exception:
            n = 1
        d = self._today_dir()
        written = []
        try:
            for i in range(1, n + 1):
                base = "shot-%d" % shot if n == 1 else "shot-%d_%d" % (shot, i)
                fn = os.path.join(d, base + ".png")
                if os.path.exists(fn):  # 同名已存在（发次号归零重测）时加时间后缀
                    fn = os.path.join(d, "%s_%s.png"
                                      % (base, datetime.now().strftime("%H%M%S")))
                with open(fn, "wb") as f:
                    f.write(make_shot_png(shot))
                written.append(fn)
                time.sleep(0.15)  # 轻微错开 mtime，同发内仍远小于 8 秒窗口
        except Exception as e:
            self._log("写盘失败: %s" % e)
            return
        self.state["shot_no"] = shot
        save_state(self.state)
        self.shot_label.config(text="下一发: shot-%d" % (shot + 1))
        self._log("shot-%d 已落盘 %d 个文件:" % (shot, n))
        for p in written:
            self._log("    %s" % p)

    def toggle_auto(self):
        self.auto_on = not self.auto_on
        if self.auto_on:
            self.auto_btn.config(text="■ 停止连打")
            self.fire_btn.state(["disabled"])
            self._log("自动连打启动（间隔 15 秒）")
            self._auto_loop()
        else:
            self.auto_btn.config(text="▶ 自动连打 (15s)")
            self.fire_btn.state(["!disabled"])
            self._log("自动连打已停止")

    def _auto_loop(self):
        if not self.auto_on:
            return
        self.fire_once()
        self.root.after(15000, self._auto_loop)

    def reset_shot(self):
        self.state["shot_no"] = 0
        save_state(self.state)
        self.shot_label.config(text="下一发: shot-1")
        self._log("发次号已归零（注意：不影响 A 机已有记录）")


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    SimulatorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
