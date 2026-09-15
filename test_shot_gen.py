#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_shot_gen.py -- fake shot generator for pipeline testing (B machine)

Generates 1KB files named shot-N (like real DAQ output) inside a test
subfolder of the b_watcher watch dir. b_watcher scans recursively, so
each new file = one reported shot on the A machine sheet.

Usage:
  py test_shot_gen.py                 manual mode: Enter = fire 1 shot
  py test_shot_gen.py --every 15      auto mode: fire 1 shot every 15s
  py test_shot_gen.py --dir D:\\some\\path   custom output dir

Keys (manual mode): Enter = 1 shot | number = N shots | a = auto 15s | q = quit
"""

import argparse
import os
import re
import sys
import threading
import time

DEFAULT_DIR = r"D:\data_main\Target_Front\2026\_test_shots"
NO_PAT = re.compile(r"(?:shot|shor)[-_ ]?(\d+)", re.IGNORECASE)

_next = [1]
_lock = threading.Lock()


def scan_next(outdir):
    """continue numbering after the max shot-N already in outdir"""
    mx = 0
    if os.path.isdir(outdir):
        for fn in os.listdir(outdir):
            m = NO_PAT.search(fn)
            if m:
                mx = max(mx, int(m.group(1)))
    _next[0] = mx + 1


def fire(outdir, kb=1):
    with _lock:
        n = _next[0]
        _next[0] += 1
    path = os.path.join(outdir, "shot-%d.png" % n)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + os.urandom(1024 - 8))
    print("[%s] fired: %s (%d bytes)" % (
        time.strftime("%H:%M:%S"), path, 1024), flush=True)


def auto_loop(outdir, every):
    while True:
        time.sleep(every)
        fire(outdir)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--every", type=int, default=0,
                    help="auto-fire interval in seconds (0 = manual mode)")
    args = ap.parse_args()

    os.makedirs(args.dir, exist_ok=True)
    scan_next(args.dir)
    print("fake shot generator")
    print("  output dir : %s" % args.dir)
    print("  next shot  : shot-%d" % _next[0])
    print("  mode       : %s" % ("auto every %ds" % args.every
                                if args.every else "manual (Enter=1 shot, N=shots, a=auto, q=quit)"))
    print("  watch it   : A machine sheet or http://10.0.23.155:8765", flush=True)

    if args.every > 0:
        auto_loop(args.dir, args.every)
        return

    auto = [False]
    def key_loop():
        while True:
            line = sys.stdin.readline().strip().lower()
            if line == "q":
                os._exit(0)
            elif line == "a":
                auto[0] = not auto[0]
                print("auto mode %s (15s)" % ("ON" if auto[0] else "OFF"), flush=True)
            elif line.isdigit():
                for _ in range(int(line)):
                    fire(args.dir)
            elif line == "":
                fire(args.dir)

    t = threading.Thread(target=key_loop, daemon=True)
    t.start()
    last_auto = 0.0
    while True:
        time.sleep(1)
        if auto[0] and time.time() - last_auto >= 15:
            last_auto = time.time()
            fire(args.dir)


if __name__ == "__main__":
    main()
