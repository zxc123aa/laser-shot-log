# -*- coding: utf-8 -*-
"""
重频靶系统靶位查询（独立进程用）
================================
由 target_client.py 用带 protobuf 的解释器调起，连接重频靶系统
（smartlink 后端，默认 10.0.23.116:5362），抓一次状态快照后输出
一行 JSON 并退出：

    {"ok": true, "pos": "2-2", "remain": "48", "count": 123,
     "x": -13068.5, "y": -15850.1, "z": 21952.05,
     "moving": false, "estop": false, "ts": "2026-09-15 18:20:00"}

协议（smartlink wire protocol，与 target-system-monitor skill 的 probe.py 同款）：
  1. TCP 连接 host:port
  2. 服务端推一条 varint 分帧 NodeLink（设备/命令/状态目录描述）
  3. 客户端回 "RDY" 三字节
  4. 服务端推全量状态快照，随后每 0.2s 推增量

只读操作，绝不发任何命令（换靶/移动电机等写命令有硬件风险）。
"""

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import link_pb2  # noqa: E402  （与本项目同目录，需 protobuf 3.20+）

DEFAULT_HOST = "10.0.23.116"
DEFAULT_PORT = 5362
SETTLE = 1.2          # 等快照到齐的时间（秒）
POS_PAT = re.compile(r"当前靶位[:：]\s*(\S+)")
DEFOCUS_PAT = re.compile(r"离焦值[:：]\s*(-?\d+(?:\.\d+)?)")

# dev_id=0（PLC五轴电机）里我们关心的 update 名称（精确匹配 e.path 或名称包含）
WANT_PAT = {
    "remain": re.compile(r"剩余靶数"),
    "count": re.compile(r"打靶计数"),
}


def varint_encode(number):
    buf = bytearray()
    while True:
        towrite = number & 0x7F
        number >>= 7
        if number:
            buf.append(towrite | 0x80)
        else:
            buf.append(towrite)
            break
    return buf


async def varint_decode(reader):
    shift = 0
    result = 0
    while True:
        i = (await reader.readexactly(1))[0]
        result |= (i & 0x7F) << shift
        shift += 7
        if not (i & 0x80):
            break
    return result


async def read_frame(reader):
    length = await varint_decode(reader)
    return await reader.readexactly(length)


def out(obj):
    print(json.dumps(obj, ensure_ascii=False))


async def run(host, port):
    result = {"ok": False, "pos": "", "defocus": "", "remain": "",
              "count": None,
              "x": None, "y": None, "z": None, "moving": None,
              "estop": None, "error": "", "ts": datetime.now().strftime(
                  "%Y-%m-%d %H:%M:%S")}
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), 4.0)
        # 1) 目录描述
        desc = await asyncio.wait_for(read_frame(reader), 4.0)
        catalog = link_pb2.NodeLink.FromString(desc)
        # update id -> 名称（按设备分）
        names = {}   # (dev_id, uid) -> path
        for dl in catalog.dev_links:
            for lk in dl.links:
                if lk.type != link_pb2.Link.COMMAND:
                    e = lk
                    path = (e.group + "." + e.name) if e.group else e.name
                    names[(dl.id, e.id)] = path
        # 2) 握手
        writer.write(b"RDY")
        await writer.drain()
        # 3) 收快照（SETTLE 秒内的推送全部合并）
        deadline = asyncio.get_event_loop().time() + SETTLE
        while asyncio.get_event_loop().time() < deadline:
            try:
                buf = await asyncio.wait_for(
                    read_frame(reader), max(0.05, deadline - asyncio.get_event_loop().time()))
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                break
            nl = link_pb2.NodeLink.FromString(buf)
            for dl in nl.dev_links:
                for lk in dl.links:
                    key = (dl.id, lk.id)
                    path = names.get(key, "")
                    args = list(lk.args)
                    if not args:
                        continue
                    val = args[0]
                    if dl.id == 0:
                        m = POS_PAT.search(val)
                        if m:
                            result["pos"] = m.group(1)
                            continue
                        m = DEFOCUS_PAT.search(val)
                        if m:
                            result["defocus"] = m.group(1)
                            continue
                        for tag, pat in WANT_PAT.items():
                            if pat.search(path) or pat.search(val):
                                try:
                                    result[tag] = float(val)
                                    if result[tag] == int(result[tag]):
                                        result[tag] = int(result[tag])
                                except (TypeError, ValueError):
                                    result[tag] = val
                                break
                        else:
                            if "RX.pos" in path:
                                try:
                                    result["rx"] = float(val)
                                except ValueError:
                                    pass
                            elif "RY.pos" in path:
                                try:
                                    result["ry"] = float(val)
                                except ValueError:
                                    pass
                            elif path.endswith("运动状态"):
                                result["moving"] = (val == "0")
                            elif "急停" in path:
                                result["estop"] = (val == "1")
                            elif "X.pos" in path:
                                try:
                                    result["x"] = float(val)
                                except ValueError:
                                    pass
                            elif "Y.pos" in path:
                                try:
                                    result["y"] = float(val)
                                except ValueError:
                                    pass
                            elif "Z.pos" in path:
                                try:
                                    result["z"] = float(val)
                                except ValueError:
                                    pass
        writer.close()
        if result["pos"]:
            result["ok"] = True
        else:
            result["error"] = "快照中未找到'当前靶位'（靶系统后端可能在运行但数据未就绪）"
    except Exception as e:
        result["error"] = repr(e)
    out(result)


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HOST
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT
    try:
        asyncio.run(run(host, port))
    except Exception as e:
        out({"ok": False, "pos": "", "error": repr(e),
             "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


if __name__ == "__main__":
    main()
