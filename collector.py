"""CUKTECH AD1204U BLE collector.

Runs as a child process of server.js and streams one JSON object per line on
stdout.  The Node side owns history, the UI and the HTTP API; this process owns
nothing but the Bluetooth link.

Line types
    {"t":"hello","v":"1.1","address":...,"writable":[...]}  startup handshake
    {"t":"link","state":"scanning|auth|connected|...","msg":...}
    {"t":"state","at":<ms>,"ok":true,"rssi":-29,"ports":[...],
     "settings":{...},"raw":[...],"total":<W>}
    {"t":"ack","id":<echo>,"ok":true,"piid":16,"value":15,"readback":15}
    {"t":"sweep","rows":[...],"found":[...],"services":[...],"vendor":[...]}
    {"t":"error","msg":...}                               non-fatal problem

Commands arrive on stdin, one JSON object per line:

    {"cmd":"set","id":7,"piid":16,"value":15}    write one allowlisted property
    {"cmd":"read"}                                re-read now (no reply payload)
    {"cmd":"ping"}                                liveness probe
    {"cmd":"quit"}                                stop the collector

Everything goes to stdout; diagnostics that a human reads go to stderr and are
echoed by the server into the app log.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "vendor"))

from ad1204u_read_props import (  # noqa: E402  (needs sys.path set up first)
    DEFAULT_QUERY,
    encode_get_properties,
    parse_response,
)

# Python on Windows would otherwise pick the OEM code page for stdout and we
# would ship mojibake to the Node parent.
try:
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8", newline="\n")
except Exception:  # noqa: BLE001
    pass

# 诊断开关：CUKTECH_BLE_DEBUG=1 时把 MIOT 库的 DEBUG 日志（每一帧的进出、计数器、
# 重传、丢帧）吐到 stderr —— server.js 会把 stderr 收进应用日志，这样"连接为什么
# 断"就有据可查。默认关闭，免得正常运行时刷屏。
if os.environ.get("CUKTECH_BLE_DEBUG") == "1":
    import logging

    logging.basicConfig(
        level=logging.DEBUG,
        format="ble %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def note(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


async def find_with_rssi(address: str, timeout: float):
    """Locate the charger and grab its signal strength.

    Two things matter here:
      * BleakScanner.find_device_by_address() returns a device whose .rssi is
        None on the Windows backend, so read the value off the advertisement.
      * BleakScanner.discover() always waits out its whole timeout, which made
        connecting take ~45s.  Scan in short chunks and return the moment the
        charger shows up (usually 2-8s).
    """
    from bleak import BleakScanner

    want = address.upper()
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None, None
        found = await BleakScanner.discover(timeout=min(8.0, max(2.0, left)), return_adv=True)
        for dev, adv in found.values():
            if dev.address.upper() == want:
                return dev, getattr(adv, "rssi", None)
        if time.monotonic() >= deadline:
            return None, None


# --------------------------------------------------------------- 属性解读表

# screen_save_time (2.6)
#
# 三个第三方实现给过两套互斥的取值表，这里用**有插件侧证据**的那一套：
# cuktech-ble-server 的 RELEASE_NOTES 记录了「依据米家插件逆向确认 PIID 6 实际
# 编码为 1=5分钟, 2=10分钟, 3=30分钟, 4=常亮, 5=1分钟（value 5 即 1 分钟，
# value 0 非法）」，cuktech-ble-controller 的两处映射也与之吻合。
# ha-cuk-ble 文档里的 0/1/2/3/4 那一版是旧的，不要再用。
SCREEN_SAVE = {1: "5 分钟", 2: "10 分钟", 3: "30 分钟", 4: "常亮", 5: "1 分钟"}

# scene_mode (2.5) — cuktech-ble-controller 的能力声明
SCENE_MODE = {1: "AI", 2: "数码生态", 3: "单口", 4: "均衡"}

# --------------------------------------------------------- 协议开关（2.21，u32）
#
# 位定义与官方规格表 2.21「扩展协议控制 protocol-ctl-extend(u32)」吻合，来自
# cuktech-ble-server 的 state.py（PROTOCOL_SWITCH_BITS / encode_protocol_extend），
# 并且 **2026-09-22 在真机上逐位验证过**：
#   读回 2.21 = 0x03030F0F（四口协议全开，与出厂默认一致）
#   把 A 口 UFCS 的 bit24 翻掉 → 回读 0x02030F0F；还原 → 回读 0x03030F0F
# 每组字节的低 4 位里，c1/c2 的第 4 位是**固定为 1 的保留位**，读改写时必须原样保留，
# 否则等于把保留位写坏。
#   c1Flags = bits 7..0    bit0=PD  bit1=PPS  bit2=UFCS  bit3=保留(1)
#   c2Flags = bits 15..8   bit8=PD  bit9=PPS  bit10=UFCS bit11=保留(1)
#   c3Flags = bits 23..16  bit16=UFCS bit17=SCP
#   aFlags  = bits 31..24  bit24=UFCS bit25=SCP
PROTO_PIID = 0x15
PROTO_SWITCH_BITS = {
    "c1": {"pd": 0, "pps": 1, "ufcs": 2},
    "c2": {"pd": 8, "pps": 9, "ufcs": 10},
    "c3": {"ufcs": 16, "scp": 17},
    "a": {"ufcs": 24, "scp": 25},
}
PROTO_RESERVED_MASK = (1 << 3) | (1 << 11)      # c1/c2 的保留位，读改写时保住
PROTO_SWATCHES = (("c1", "pd", "PD"), ("c1", "pps", "PPS"), ("c1", "ufcs", "UFCS"),
                  ("c2", "pd", "PD"), ("c2", "pps", "PPS"), ("c2", "ufcs", "UFCS"),
                  ("c3", "ufcs", "UFCS"), ("c3", "scp", "SCP"),
                  ("a", "ufcs", "UFCS"), ("a", "scp", "SCP"))

# 2.17 / 2.18 高字节的米家协议号（cuktech-ble-server 固件逆向得出的编制）
MIJIA_PROTOCOLS = {0: "idle", 1: "5V", 2: "5V", 3: "QC", 4: "AFC", 5: "FCP",
                   6: "SCP", 7: "PD", 8: "PPS", 9: "PPS", 10: "UFCS"}


def decode_protocol_switches(value: int) -> dict:
    """把 2.21 的 u32 拆成 {端口: {协议: bool}}。"""
    if not isinstance(value, int):
        return {}
    return {
        port: {sw: bool(value & (1 << bit)) for sw, bit in bits.items()}
        for port, bits in PROTO_SWITCH_BITS.items()
    }


def protocol_switch_schema() -> list:
    """给界面用的开关清单（顺序固定）。"""
    return [{"port": p, "sw": s, "label": lab, "bit": PROTO_SWITCH_BITS[p][s]}
            for p, s, lab in PROTO_SWATCHES]

PORT_ORDER = ("c1", "c2", "c3", "a")
PORT_LABEL = {"c1": "C1", "c2": "C2", "c3": "C3", "a": "USB-A"}

# 2.1..2.4 hold the per-port power word; 2.17/2.18 hold the negotiated PDO cap.
PORT_PIID = {"c1": 1, "c2": 2, "c3": 3, "a": 4}
CAP_PIID = ((0x11, "c1", "c2"), (0x12, "c3", "a"))

# --------------------------------------------------------------- 可写属性表
#
# 只列出**语义已经查清**的属性。每一项都是 (piid, 名称, 宽度, 最小值, 最大值)。
# 宽度决定写入帧里的 type 字节（u8→0x01 / u16→0x02 / u32→0x04）。
#
# 两点刻意的选择：
#   * 15/19/20 在 MIOT 规格里声明为 bool，但参考实现（同为实机写入）统一按
#     u8 + marker 0x10 发送并且工作正常；这里跟着走，避免 bool marker(0x00)
#     在写入方向是否被接受这个未验证的问题。
#   * **倒计时 2.9..2.12 不开放** —— 2026-09-22 用 --probe 的 GET 形状矩阵实测钉死：
#     设备只认「正好 15 项、且每一项都真实存在」的请求。
#         我们那 15 项                -> 正常应答 15 条
#         去掉 2.7（14 项）           -> 超时
#         把 2.7 换成 2.8（仍 15 项） -> 正常应答   ← 2.8 真的存在
#         把 2.7 换成 2.9/0A/0B/0C    -> 全部超时   ← 这四项不存在
#         15 项 + 2.8（16 项）        -> 超时
#         只问 2.8..2.12（5 项）      -> 超时
#         只问 3 项                   -> 超时
#     ⇒ 这台固件没有四口倒计时（官方规格表列了它们，但没有下发到本固件）。
#       别把它们加回来，除非换过固件并重新跑 --probe。
WRITABLE = {
    0x05: ("场景模式", "u8", 1, 4),
    0x06: ("息屏时间", "u8", 1, 5),
    0x0D: ("设备语言", "u8", 0, 1),
    0x0F: ("USB-A 小电流", "u8", 0, 1),
    0x10: ("端口开关", "u8", 0, 15),
    0x13: ("空闲熄屏", "u8", 0, 1),
    0x14: ("屏幕方向锁", "u8", 0, 1),
    # 协议开关（2.21，u32 位掩码）。语义已逐位实测，界面有专门的开关面板走它，
    # 不再当"原始数值"用。
    0x15: ("协议开关掩码", "u32", 0, 0xFFFFFFFF),
    # 下面两项语义未查清，只在界面的「高级」区里以原始数值暴露。
    # 2.8 能用「把 2.7 换出去」的 15 项形状读出来（见 --probe 结论），但常态轮询
    # 里没有它，所以界面上按"写着试试"处理。
    0x07: ("协议控制字 2.7", "u8", 0, 255),
    0x08: ("端口关闭设置 2.8", "u8", 0, 255),
}
ADVANCED_PIIDS = frozenset({0x07, 0x08})


def decode_settings(items: dict[int, dict]) -> dict:
    """Human-facing settings block.  Unknown semantics are marked as such
    rather than guessed at."""

    def val(piid: int):
        it = items.get(piid)
        return None if it is None else it["value"]

    def boolean(piid: int):
        v = val(piid)
        return None if v is None else bool(v)

    sst = val(0x06)
    scene = val(0x05)
    portctl = val(0x10)
    enabled = None
    if isinstance(portctl, int):
        enabled = {p: bool(portctl & (1 << i)) for i, p in enumerate(PORT_ORDER)}

    return {
        "scene_mode": scene,
        "scene_mode_text": SCENE_MODE.get(scene) if isinstance(scene, int) else None,
        "screen_save_time": sst,
        "screen_save_text": SCREEN_SAVE.get(sst) if isinstance(sst, int) else None,
        "protocol_ctl": val(0x07),
        "device_language": val(0x0D),
        "usb_a_always_on": boolean(0x0F),
        "port_ctl": portctl,
        "ports_enabled": enabled,
        "screenoff_while_idle": boolean(0x13),
        "screen_dir_lock": boolean(0x14),
        "protocol_ctl_extend": val(0x15),
        "protocol_switches": decode_protocol_switches(val(0x15)),
    }


async def collect_once(session, encode_get_properties, parse_response, decode_port_info,
                       decode_pdo_caps, decode_pdo_kind, rssi) -> dict:
    request = encode_get_properties(seq=0x001B, tuples=DEFAULT_QUERY)
    _seq, ok, items = parse_response(await session.send_request(request))
    by_piid = {it["piid"]: it for it in items if it["siid"] == 2}

    ports = []
    total = 0.0
    for pid in PORT_ORDER:
        it = by_piid.get(PORT_PIID[pid])
        if it is None:
            continue
        info = decode_port_info(pid, it["value"])
        cap = None
        kind = None
        proto_num = None
        for cap_piid, high_port, low_port in CAP_PIID:
            src = by_piid.get(cap_piid)
            if src is None:
                continue
            caps = decode_pdo_caps(src["value"], high_port=high_port, low_port=low_port)
            if pid in caps:
                cap = caps[pid]
                kind = decode_pdo_kind(src["value"], high_port=high_port,
                                       low_port=low_port).get(pid)
                # 2.17/2.18 每个半字是 [协议号][协商功率(W)]，高 16 位给前一个口。
                half = ((src["value"] >> 16) & 0xFFFF) if pid == high_port \
                    else (src["value"] & 0xFFFF)
                if half & 0xFF:
                    proto_num = ((half >> 8) & 0xFF) or None
        total += info.power_w
        ports.append({
            "id": pid,
            "label": PORT_LABEL[pid],
            "in_use": bool(info.in_use),
            "proto": info.protocol_name,
            "proto_num": proto_num,
            "proto_text": MIJIA_PROTOCOLS.get(proto_num) if proto_num else None,
            "v": round(info.voltage_v, 2),
            "a": round(info.current_a, 2),
            "w": round(info.power_w, 2),
            "cap": cap,
            "kind": kind,
            "raw": it["raw"],
        })

    return {
        "t": "state",
        "at": int(time.time() * 1000),
        "ok": ok == 0x03,
        "rssi": rssi,
        # 链路层重传 / 乱序计数（正常会缓慢增长；涨得飞快说明 ACK 通道大面积丢包）
        "rxDup": getattr(session, "rx_duplicates", 0),
        "rxLag": getattr(session, "rx_out_of_order", 0),
        "total": round(total, 2),
        "ports": ports,
        "settings": decode_settings(by_piid),
        "raw": [
            {"piid": it["piid"], "type": it["type"], "value": it["value"],
             "status": it["status"], "raw": it["raw"]}
            for it in items
        ],
    }


# --------------------------------------------------------------- 命令通道

FULL_PIIDS = frozenset(p for _s, p in DEFAULT_QUERY)


async def read_full(session) -> dict:
    """标准 15 项读取。**这是本机唯一可靠的读法** ——
    单属性 GET 在这台固件上必然超时（--probe 实测），所以任何"写后回读"都必须
    走这一条，否则 readback 恒为 None 而被误判成成功。"""
    req = encode_get_properties(seq=session.next_sequence(), tuples=list(DEFAULT_QUERY))
    _seq, _ok, items = parse_response(await session.send_request(req))
    return {it["piid"]: it["value"] for it in items if it["siid"] == 2}


async def read_alt(session) -> dict:
    """用「把 2.7 换成 2.8」的 15 项形状，把常态轮询里没有的 2.8 读出来。"""
    req = encode_get_properties(seq=session.next_sequence(), tuples=alt15(0x07, 0x08))
    _seq, _ok, items = parse_response(await session.send_request(req))
    return {it["piid"]: it["value"] for it in items if it["siid"] == 2}


async def invoke_action(session, siid: int, aiid: int) -> bytes:
    """调用 MIOT action（形如 `24 20 <seq> <siid> <aiid_le2>`，应答 `66 20 ...`）。"""
    seq = session.next_sequence()
    body = (b"\x24\x20" + (seq & 0xFFFF).to_bytes(2, "little")
            + bytes([siid]) + (aiid & 0xFFFF).to_bytes(2, "little"))
    return await session.send_request(body)


async def run_command(session, cmd: dict) -> dict:
    """执行一条来自界面的命令，返回要回给 Node 的 ack 对象。

    校验全部在这里做：不在 WRITABLE 清单里的 piid 一律拒绝，取值也做范围限制，
    这样即使上层界面被改坏也无法往设备里写奇怪的东西。
    """
    from cuktech_ble.xiaomi.properties import set_property

    cid = cmd.get("id")
    kind = cmd.get("cmd")
    ack = {"t": "ack", "id": cid, "cmd": kind}

    if kind in ("ping", "read"):
        return dict(ack, ok=True)

    if kind == "ext-read":
        try:
            vals = await read_alt(session)
            return dict(ack, ok=True, values={("2.%02x" % k): v for k, v in vals.items()},
                        note="用 15 项替换形状读到的扩展属性（本机只有 2.8 是新的）")
        except Exception as exc:  # noqa: BLE001
            note("ext-read failed: %s: %s" % (type(exc).__name__, exc))
            return dict(ack, ok=False, error="%s：%s" % (type(exc).__name__, exc))

    if kind == "reset":
        try:
            raw = await invoke_action(session, 2, 1)
            return dict(ack, ok=True, note="已调用「恢复默认设置」，回帧 %s" % raw.hex())
        except Exception as exc:  # noqa: BLE001
            note("reset action failed: %s: %s" % (type(exc).__name__, exc))
            return dict(ack, ok=False, error="%s：%s" % (type(exc).__name__, exc))

    if kind != "set":
        return dict(ack, ok=False, error="未知命令 %r" % (kind,))

    piid = cmd.get("piid")
    value = cmd.get("value")
    spec = WRITABLE.get(piid) if isinstance(piid, int) else None
    if spec is None:
        return dict(ack, ok=False, error="属性 2.%s 不在可写清单内" % (piid,))
    name, width, lo, hi = spec
    if isinstance(value, bool):
        value = int(value)
    if not isinstance(value, int):
        return dict(ack, ok=False, piid=piid, name=name, error="value 必须是整数")
    if not lo <= value <= hi:
        return dict(ack, ok=False, piid=piid, name=name,
                    error="%s 取值需在 %d..%d 之间" % (name, lo, hi))

    kwargs = {}
    if width == "u32":
        kwargs["u32"] = True
    elif width == "u16":
        kwargs["u16"] = True

    try:
        await set_property(session, 2, piid, value, verify=False, **kwargs)
    except Exception as exc:  # noqa: BLE001
        note("set 2.%d failed: %s: %s" % (piid, type(exc).__name__, exc))
        return dict(ack, ok=False, piid=piid, name=name,
                    error="%s：%s" % (type(exc).__name__, exc))

    # 回读必须走标准 15 项：单属性 GET 在这台固件上必超时，拿不到东西时返回 None
    # 会被上层误判成成功（这正是老版本夸下海口说"回读一致"的原因）。
    if piid not in FULL_PIIDS:
        return dict(ack, ok=True, piid=piid, name=name, value=value, readback=None,
                    note="该属性不在标准 15 项里，本机无法回读验证")
    try:
        readback = (await read_full(session)).get(piid)
    except Exception as exc:  # noqa: BLE001
        note("read-back 2.%d failed: %s: %s" % (piid, type(exc).__name__, exc))
        return dict(ack, ok=False, piid=piid, name=name, value=value, readback=None,
                    error="写入已发出，但回读失败：%s" % exc)

    ok = readback == value
    return dict(ack, ok=ok, piid=piid, name=name, value=value, readback=readback,
                error=None if ok else "写入已确认，但回读为 %r（可能被设备规整过）" % (readback,))


def writable_schema() -> list:
    """给界面用的可写属性表 + 协议开关清单。"""
    return [
        {"piid": p, "name": v[0], "type": v[1], "min": v[2], "max": v[3],
         "advanced": p in ADVANCED_PIIDS}
        for p, v in sorted(WRITABLE.items())
    ]


def switches_schema() -> list:
    return protocol_switch_schema()


async def _stdin_reader(q: asyncio.Queue) -> None:
    """把父进程写进 stdin 的 JSON 行推进队列。

    Windows 上 asyncio 没法给管道挂 reader，所以借一个线程阻塞读 stdin。
    stdin 被关闭（父进程退出）时安静地结束。
    """
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
        except Exception as exc:  # noqa: BLE001
            note("stdin reader stopped: %s" % exc)
            return
        if not line:
            note("stdin closed; 命令通道关闭")
            return
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001
            emit({"t": "ack", "ok": False, "error": "命令不是合法 JSON"})
            continue
        if isinstance(obj, dict):
            await q.put(obj)


# --------------------------------------------------------------- 隐藏属性扫描
#
# 用来回答「充电头温度能不能读」。做法和 ha-cuk-ble 的 reverse-engineering 笔记
# 一致：认证之后把整段未知 piid 一次性问一遍，设备会对每个 piid 单独回 status ——
# 已知的 0x0000，未知的固件 not-found 0xf05f。顺手把 GATT 全表列出来，并试探
# 厂商服务 0xaf00 下的可读特征（那是 MIOT 之外唯一的线索）。

SWEEP_LO = 0x16
SWEEP_HI = 0x40
VENDOR_PREFIX = "0000af"


def _gatt_table(client) -> list:
    out = []
    for svc in client.services:
        entry = {"uuid": str(svc.uuid), "description": svc.description, "chars": []}
        for ch in svc.characteristics:
            entry["chars"].append({"uuid": str(ch.uuid), "props": sorted(ch.properties)})
        out.append(entry)
    return out


async def sweep_hidden(client, session, lo: int = SWEEP_LO, hi: int = SWEEP_HI,
                       batch: int = 6) -> dict:
    """把 siid=2 的未知 piid 问一遍，顺便把 GATT 表列出来。

    ⚠ 这台设备的一个硬脾气（2026-09-22 实测钉死）：**它只认自己那套完整属性表**。
    少于 15 个已知属性的请求一律**静默丢弃**（表现为 MiSessionError timeout，不是
    返回 not-found）——
        [15 个已知属性]        → 正常应答，15 条 status=0
        [3 个已知属性]         → 超时
        [1 个已知属性]         → 超时
    所以枚举未知属性的唯一办法是**在完整表后面追加**要问的 piid，再看回答里给不给
    它们状态；只发未知属性的批次是永远问不出东西的（早期版本就是这么白跑的）。
    """
    rows, found = [], []
    dropped = 0
    base = list(DEFAULT_QUERY)
    base_piids = {p for _siid, p in base}

    # 对照读取：三种请求形状各试一次，把上面那条前提钉进证据里
    control = {"ok": False, "items": 0, "attempts": []}
    for _label, _tuples in (
        ("full", list(base)),
        ("full+1", list(base) + [(2, 0x16)]),
        ("three", [(2, 1), (2, 2), (2, 0x10)]),
    ):
        try:
            req = encode_get_properties(seq=0x001B, tuples=_tuples)
            _cseq, _cok, citems = parse_response(await session.send_request(req))
            got = {it["piid"] for it in citems if it["siid"] == 2}
            control["attempts"].append({
                "label": _label, "sent": len(_tuples), "got": len(citems),
                "extra_echoed": sorted(got - base_piids),
                "extra_status": {("2.%d" % it["piid"]): it["status"]
                                 for it in citems if it["piid"] not in base_piids},
            })
            if _label == "full" and not control["ok"]:
                control.update(ok=True, items=len(citems))
        except Exception as exc:  # noqa: BLE001
            control["attempts"].append({"label": _label, "sent": len(_tuples), "got": 0,
                                        "error": "%s: %s" % (type(exc).__name__, exc)})
            note("sweep 对照读取 %s 失败：%s" % (_label, exc))
        await asyncio.sleep(0.25)

    for start in range(lo, hi + 1, batch):
        pids = list(range(start, min(hi, start + batch - 1) + 1))
        try:
            request = encode_get_properties(
                seq=0x001C + (start - lo) // batch,
                tuples=list(base) + [(2, p) for p in pids],
            )
            _seq, _ok, items = parse_response(await session.send_request(request))
            by_piid = {it["piid"]: it for it in items if it["siid"] == 2}
        except Exception as exc:  # noqa: BLE001
            note("sweep batch 0x%02x..0x%02x failed: %s" % (pids[0], pids[-1], exc))
            dropped += 1
            for p in pids:
                rows.append({"piid": p, "status": None,
                             "note": "整批未应答（%s）" % type(exc).__name__})
            await asyncio.sleep(0.3)
            continue

        for p in pids:
            it = by_piid.get(p)
            if it is None:
                rows.append({"piid": p, "status": None,
                             "note": "设备已应答但表里没有它 —— 即该属性不存在"})
                continue
            row = {"piid": p, "type": it["type"], "value": it["value"],
                   "status": it["status"], "raw": it["raw"]}
            rows.append(row)
            if it["status"] == 0x0000:
                found.append(row)
        await asyncio.sleep(0.12)

    vendor = []
    for svc in client.services:
        if not str(svc.uuid).lower().startswith(VENDOR_PREFIX):
            continue
        for ch in svc.characteristics:
            entry = {"uuid": str(ch.uuid), "props": sorted(ch.properties), "read": None}
            if "read" in ch.properties:
                try:
                    entry["read"] = bytes(await client.read_gatt_char(ch)).hex()
                except Exception as exc:  # noqa: BLE001
                    entry["read"] = "ERR %s: %s" % (type(exc).__name__, exc)
            vendor.append(entry)

    # 厂商服务里唯一可能带数据的通道：AF08 只支持 notify（不能读）。订阅几秒，
    # 看设备会不会主动推点什么 —— 这是 MIOT 之外最后一条没探过的线索。
    pushed = []
    for svc in client.services:
        if not str(svc.uuid).lower().startswith(VENDOR_PREFIX):
            continue
        for ch in svc.characteristics:
            if not ({"notify", "indicate"} & set(ch.properties)):
                continue
            got: list = []

            def _cb(_handle, data, _got=got):
                try:
                    _got.append(bytes(data).hex())
                except Exception:  # noqa: BLE001
                    pass

            try:
                await client.start_notify(ch, _cb)
            except Exception as exc:  # noqa: BLE001
                pushed.append({"uuid": str(ch.uuid), "error": "%s: %s" % (type(exc).__name__, exc)})
                continue
            await asyncio.sleep(6)
            try:
                await client.stop_notify(ch)
            except Exception:  # noqa: BLE001
                pass
            pushed.append({"uuid": str(ch.uuid), "count": len(got), "samples": got[:8]})

    answered_rows = [r for r in rows if r.get("status") is not None]
    missing_rows = [r for r in rows
                    if r.get("status") is None and "不存在" in (r.get("note") or "")]
    return {"t": "sweep", "ok": True, "lo": lo, "hi": hi, "batch": batch,
            "answered": len(answered_rows), "missing": len(missing_rows),
            "dropped": dropped, "control": control, "rows": rows, "found": found,
            "services": _gatt_table(client), "vendor": vendor, "notify": pushed}


# --------------------------------------------------------------- 属性探针
#
# 为什么还要这段：官方 MIoT 规格表
#   urn:miot-spec-v2:device:fitting:0000A0D1:njcuk-ad1204:2
# 明明列着 `2.8 端口关闭设置(u8)` 与 `2.9..2.12 四口倒计时(u16, 0..1440 分钟)`，
# 第三方 cuktech-ble-server 也把它们放进「可写 PIID」并做了倒计时页面；但我们
# 早先判它们「不存在」，而当初那两条证据都站不住：
#   * 隐藏扫描发的是「已知押运员 + 未知属性」的**不完整集**，本来就违反固件
#     「必须发完整已知集」的约束，被整批丢弃是必然的 —— 推不出属性不存在；
#   * 写入实验很可能发生在 u16 编码修好之前（用 u8 去发 uint16 属性必然超时）。
# 另外 set_property() 的回读走的是单属性 GET，拿不到时返回 None，而 run_command
# 把 None 当成成功 —— 所以它**不能**用来判定属性是否存在。
#
# 这里改用两条独立证据：
#   1. GET 形状矩阵：把几种 15 项的请求都发一遍，看设备到底认哪几种；
#   2. SET 后的 0x0c Result 回显（走 notification_callback）。带两个对照 ——
#      已知可写的 2.6（回显必须出现）与必然不存在的 2.0x30（回显必须不出现），
#      这样「有回显」才真的是成功的证据。

KAIRUI_15: tuple[tuple[int, int], ...] = (
    (2, 0x05), (2, 0x06), (2, 0x08), (2, 0x09), (2, 0x0A), (2, 0x0B), (2, 0x0C),
    (2, 0x0D), (2, 0x0F), (2, 0x10), (2, 0x11), (2, 0x12), (2, 0x13), (2, 0x14),
    (2, 0x15),
)

# 2026-09-22 第一轮 probe 已经把「请求形状」这条钉死了（见下面 GET 矩阵）：设备只认
# **正好 15 项、且每一项都真实存在**的请求 —— 14 项超时、16 项超时、换成任一不存在的
# piid 也超时，所以「15 项」这个数量本身就是硬约束，靠替换来问未知属性是问不出来的。
# 结论：2.8 存在，2.9..2.12 不存在（倒计时在这台固件上确实没有）。
#
# 第二轮要回答的是**写入能不能验证**。`set_property()` 的回读走单属性 GET，那条路必
# 然超时，于是 readback 恒为 None 而 run_command 把 None 判成成功 —— 等于没验证。
# 好在我们那 15 项里就带着 2.6 / 2.7 / 2.16 / 2.21，所以正确做法是：
# **写完之后再用标准 15 项读回来比对**。协议开关（2.21）的位翻转/还原也用这个办法验。


def alt15(swap_out: int, swap_in: int) -> list:
    """把 15 项里的某个 piid 换成另一个，数量仍是 15（唯一能问出新属性的形状）。"""
    return [t for t in DEFAULT_QUERY if t != (2, swap_out)] + [(2, swap_in)]


def probe_get_sets() -> list:
    base = list(DEFAULT_QUERY)
    no7 = [t for t in base if t != (2, 0x07)]
    out = [("ours15", base), ("ours14（去掉 2.7）", no7)]
    for extra in (0x08, 0x09, 0x0A, 0x0B, 0x0C):
        out.append(("把 2.7 换成 2.%02x（15 项）" % extra, no7 + [(2, extra)]))
    out.append(("第三方那套 15 项", list(KAIRUI_15)))
    out.append(("ours15 + 2.08（16 项）", base + [(2, 0x08)]))
    out.append(("只问 2.8..2.12（5 项）", [(2, p) for p in (0x08, 0x09, 0x0A, 0x0B, 0x0C)]))
    out.append(("只问 3 项", [(2, 1), (2, 2), (2, 0x10)]))
    return out


def _decode_any(pt: bytes) -> list[dict]:
    """严格解析器只认 0x93 头；设备换了应答形状时要能看出来，而不是误判成超时。"""
    try:
        _seq, _ok, items = parse_response(pt)
        return [dict(it, opcode=pt[:2].hex()) for it in items]
    except Exception:  # noqa: BLE001
        pass
    from cuktech_ble.xiaomi.properties import parse_response as tolerant

    return [
        {"siid": it.siid, "piid": it.piid, "status": it.status,
         "type": {0x01: "u8", 0x02: "u16", 0x04: "u32"}.get(it.type_byte, "?"),
         "value": it.value, "raw": "", "opcode": pt[:2].hex()}
        for it in tolerant(pt)
    ]


def _decode_push(pt: bytes) -> list[dict]:
    from cuktech_ble.xiaomi.properties import parse_notification

    return [
        {"siid": it.siid, "piid": it.piid, "value": it.value,
         "type": {0x01: "u8", 0x02: "u16", 0x04: "u32"}.get(it.type_byte, "?"),
         "raw": pt.hex()}
        for it in parse_notification(pt)
    ]


async def probe_properties(session) -> dict:
    from cuktech_ble.xiaomi.properties import set_property

    pushes: list = []

    def _on_push(pt: bytes):
        try:
            pushes.extend(_decode_push(pt))
        except Exception as exc:  # noqa: BLE001
            pushes.append({"raw": pt.hex(), "note": "%s（不是属性通知）" % type(exc).__name__})

    session._notification_callback = _on_push

    current: dict = {}
    try:
        req = encode_get_properties(seq=0x0020, tuples=list(DEFAULT_QUERY))
        current = {it["piid"]: it["value"]
                   for it in _decode_any(await session.send_request(req))
                   if it["siid"] == 2}
    except Exception as exc:  # noqa: BLE001
        note("probe 基线读取失败：%s" % exc)
    await asyncio.sleep(0.3)

    gets = []
    for i, (label, tuples) in enumerate(probe_get_sets()):
        entry = {"label": label, "sent": len(tuples)}
        try:
            req = encode_get_properties(seq=(0x0040 + i) & 0xFFFF, tuples=list(tuples))
            items = [it for it in _decode_any(await session.send_request(req))
                     if it["siid"] == 2]
            entry.update(ok=True, got=len(items),
                         opcode=items[0]["opcode"] if items else None,
                         entries=[{"piid": it["piid"], "status": it["status"],
                                   "value": it["value"], "type": it["type"]}
                                  for it in items])
        except Exception as exc:  # noqa: BLE001
            entry.update(ok=False, error="%s：%s" % (type(exc).__name__, exc))
        gets.append(entry)
        note("probe GET [%s] -> %s" % (label, "OK %d 项" % entry["got"]
                                       if entry.get("ok") else entry.get("error")))
        await asyncio.sleep(0.35)

    writes = []
    next_seq = [0x0040 + len(gets)]

    def nseq() -> int:
        next_seq[0] = (next_seq[0] + 1) & 0xFFFF
        return next_seq[0]

    async def read15(tuples=None) -> dict:
        req = encode_get_properties(seq=nseq(), tuples=list(tuples or DEFAULT_QUERY))
        items = [it for it in _decode_any(await session.send_request(req))
                 if it["siid"] == 2]
        return {it["piid"]: it["value"] for it in items}

    async def try_write(piid: int, value: int, **kw) -> None:
        await set_property(session, 2, piid, value, verify=False, **kw)
        await asyncio.sleep(1.0)

    # 1) 2.8 存不存在、值是多少 —— 只能靠「把 2.7 换出去」这种仍为 15 项的形状来问
    try:
        alt = await read15(alt15(0x07, 0x08))
        writes.append({"step": "读 2.8（15 项替换形状）", "ok": True,
                       "value": alt.get(0x08),
                       "piids": ["2.%02x" % p for p in sorted(alt)]})
        note("probe 2.8 = %r" % (alt.get(0x08),))
    except Exception as exc:  # noqa: BLE001
        writes.append({"step": "读 2.8", "ok": False,
                       "error": "%s：%s" % (type(exc).__name__, exc)})
    await asyncio.sleep(0.4)

    # 2) 写入通道 sanity：回写原值，再用标准 15 项读回来比对
    for piid, label, kw in ((0x06, "息屏时间（回写原值）", {}),
                            (0x15, "协议开关（回写原值）", {"u32": True})):
        want = current.get(piid)
        if want is None:
            writes.append({"step": "写 2.%02x" % piid, "skipped": "基线里读不到，不写"})
            continue
        row = {"step": "写 2.%02x %s" % (piid, label), "want": want}
        try:
            await try_write(piid, want, **kw)
            got = (await read15()).get(piid)
            row.update(ok=(got == want), readback=got)
        except Exception as exc:  # noqa: BLE001
            row.update(ok=False, error="%s：%s" % (type(exc).__name__, exc))
        writes.append(row)
        note("probe 写 2.%02x -> readback=%r（want %r）" % (piid, row.get("readback"), want))
        await asyncio.sleep(0.3)

    # 3) 协议开关的位真的能改吗 —— 翻一下 A 口 UFCS（bit24）再还原。
    #    只在 A 口空闲时做，免得打断正在充电的设备。
    cur21 = current.get(0x15)
    a_raw = current.get(4)
    a_idle = isinstance(a_raw, int) and (a_raw & 0xFF) == 0
    if not a_idle:
        writes.append({"step": "翻转 A 口 UFCS 位", "skipped": "A 口在用，不动它"})
    elif not isinstance(cur21, int):
        writes.append({"step": "翻转 A 口 UFCS 位", "skipped": "基线读不到 2.21"})
    else:
        target = cur21 ^ (1 << 24)
        row = {"step": "翻转 A 口 UFCS 位",
               "before": "0x%08x" % cur21, "written": "0x%08x" % target}
        try:
            await try_write(0x15, target, u32=True)
            mid = (await read15()).get(0x15)
            row.update(ok=(mid == target),
                       readback=None if mid is None else "0x%08x" % mid,
                       a_ufcs_now=None if mid is None else bool(mid & (1 << 24)))
        except Exception as exc:  # noqa: BLE001
            row.update(ok=False, error="%s：%s" % (type(exc).__name__, exc))
        writes.append(row)
        note("probe 翻转 -> readback=%r" % (row.get("readback"),))

        row2 = {"step": "还原 2.21", "want": "0x%08x" % cur21}
        try:
            await try_write(0x15, cur21, u32=True)
            back = (await read15()).get(0x15)
            row2.update(ok=(back == cur21),
                        readback=None if back is None else "0x%08x" % back)
        except Exception as exc:  # noqa: BLE001
            row2.update(ok=False, error="%s：%s" % (type(exc).__name__, exc))
        writes.append(row2)
        note("probe 还原 -> readback=%r" % (row2.get("readback"),))

    return {"t": "probe", "ok": True, "current": current, "gets": gets,
            "writes": writes, "pushes": pushes[-60:]}


async def link_loop(args) -> int:
    from bleak import BleakClient, BleakScanner
    from cuktech_ble.xiaomi import MiAuthClient
    from cuktech_ble.xiaomi.session import MiSession
    from cuktech_ble.ports import (
        decode_pdo_caps,
        decode_pdo_kind,
        decode_port_info,
    )

    token = bytes.fromhex(json.loads(Path(args.token_file).read_text(encoding="utf-8"))["token_hex"])

    attempt = 0
    while True:
        attempt += 1
        try:
            emit({"t": "link", "state": "scanning", "attempt": attempt,
                  "msg": "正在搜索充电器 %s" % args.address})
            device, rssi = await find_with_rssi(args.address, args.scan_timeout)
            if device is None:
                raise RuntimeError("没有发现该蓝牙地址；充电器是否已通电？米家 App 是否占用了连接？")

            emit({"t": "link", "state": "connecting", "attempt": attempt,
                  "rssi": rssi, "msg": "已发现，正在建立 BLE 连接"})

            async with BleakClient(device, timeout=60) as client:
                emit({"t": "link", "state": "auth", "attempt": attempt, "msg": "正在完成 MiOT 认证"})
                auth = None
                keys = None
                last = None
                for i in range(4):
                    auth = MiAuthClient(client, timeout=15, bluez_start_notify=False)
                    try:
                        await auth.subscribe(upnp=False)
                        await auth.greet()
                        await auth.subscribe_upnp()
                        keys = await auth.login(token)
                        break
                    except Exception as exc:  # noqa: BLE001
                        last = exc
                        note("login attempt %d failed: %s" % (i + 1, exc))
                        try:
                            await auth.unsubscribe()
                        except Exception:  # noqa: BLE001
                            pass
                        await asyncio.sleep(1.5)
                if keys is None:
                    raise last if last else RuntimeError("认证失败")

                session = MiSession(auth, keys, timeout=args.req_timeout)
                await session.subscribe()
                attempt = 0
                emit({"t": "link", "state": "connected", "msg": "已连接并认证", "rssi": rssi})

                if args.probe:
                    note("probe: GET 形状矩阵 + 属性写入回显测试")
                    try:
                        result = await probe_properties(session)
                        emit(result)
                        if args.probe_out:
                            Path(args.probe_out).write_text(
                                json.dumps(result, ensure_ascii=False, indent=1),
                                encoding="utf-8")
                            note("probe 结果已写入 %s" % args.probe_out)
                    except Exception as exc:  # noqa: BLE001
                        emit({"t": "error", "msg": "探针失败：%s: %s" % (type(exc).__name__, exc)})
                    try:
                        await session.unsubscribe()
                        await auth.unsubscribe()
                    except Exception:  # noqa: BLE001
                        pass
                    return 0

                if args.sweep:
                    lo, hi = int(args.sweep_lo), int(args.sweep_hi)
                    note("sweep: 扫描 siid=2 piid 0x%02x..0x%02x" % (lo, hi))
                    try:
                        emit(await sweep_hidden(client, session, lo=lo, hi=hi))
                    except Exception as exc:  # noqa: BLE001
                        emit({"t": "error", "msg": "扫描失败：%s: %s" % (type(exc).__name__, exc)})
                    try:
                        await session.unsubscribe()
                        await auth.unsubscribe()
                    except Exception:  # noqa: BLE001
                        pass
                    return 0

                cmds: asyncio.Queue = asyncio.Queue()
                stdin_task = asyncio.create_task(_stdin_reader(cmds))
                try:
                    while True:
                        t0 = time.monotonic()
                        try:
                            state = await collect_once(
                                session, encode_get_properties, parse_response,
                                decode_port_info, decode_pdo_caps, decode_pdo_kind, rssi,
                            )
                            emit(state)
                        except Exception as exc:  # noqa: BLE001
                            note("read failed: %s: %s" % (type(exc).__name__, exc))
                            emit({"t": "error", "msg": "读取失败：%s" % exc})
                            raise
                        if args.once:
                            break

                        # 采样间隔内若收到命令就立刻执行（写操作和读取都在
                        # 同一个协程里串行发生，所以不会有请求撞车）；
                        # 执行完回头再采集一次，界面马上就能看到结果。
                        spent = time.monotonic() - t0
                        try:
                            cmd = await asyncio.wait_for(
                                cmds.get(), timeout=max(0.2, args.interval - spent)
                            )
                        except asyncio.TimeoutError:
                            continue
                        if not isinstance(cmd, dict):
                            continue
                        if cmd.get("cmd") in ("quit", "exit"):
                            return 0
                        try:
                            emit(await run_command(session, cmd))
                        except Exception as exc:  # noqa: BLE001
                            note("command failed: %s: %s" % (type(exc).__name__, exc))
                            emit({"t": "ack", "id": cmd.get("id"), "ok": False,
                                  "error": "%s：%s" % (type(exc).__name__, exc)})
                finally:
                    stdin_task.cancel()
                    try:
                        await session.unsubscribe()
                        await auth.unsubscribe()
                    except Exception:  # noqa: BLE001
                        pass

                if args.once:
                    return 0

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            back = min(30, 2 * min(attempt, 10))
            note("link error: %s: %s (retry in %ss)" % (type(exc).__name__, exc, back))
            emit({"t": "link", "state": "reconnecting", "attempt": attempt,
                  "msg": "%s（%ss 后重试）" % (exc, back)})
            await asyncio.sleep(back)


def main() -> int:
    ap = argparse.ArgumentParser(description="AD1204U BLE collector (JSONL on stdout)")
    ap.add_argument("--address", required=True)
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--interval", type=float, default=1.5)
    ap.add_argument("--scan-timeout", type=float, default=45)
    ap.add_argument("--req-timeout", type=float, default=10)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--sweep", action="store_true",
                    help="认证后扫描 siid=2 的隐藏 piid 并列出 GATT 表，然后退出")
    ap.add_argument("--probe", action="store_true",
                    help="认证后跑属性探针（GET 形状矩阵 + 倒计时写入回显），然后退出")
    ap.add_argument("--probe-out", default="",
                    help="把探针结果写成 JSON 文件（PowerShell 重定向会折行/改编码，走文件最稳）")
    ap.add_argument("--sweep-lo", type=lambda x: int(x, 0), default=SWEEP_LO,
                    help="扫描起始 piid（支持 0x 前缀）")
    ap.add_argument("--sweep-hi", type=lambda x: int(x, 0), default=SWEEP_HI,
                    help="扫描结束 piid（支持 0x 前缀）")
    args = ap.parse_args()

    emit({"t": "hello", "v": "1.3", "address": args.address, "pid": os.getpid(),
          "writable": writable_schema(), "switches": switches_schema()})
    try:
        return asyncio.run(link_loop(args))
    except KeyboardInterrupt:
        return 0
    finally:
        emit({"t": "bye"})


if __name__ == "__main__":
    sys.exit(main())
