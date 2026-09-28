"""``/api/control`` 各动作的实现（等价重写 ``server.js`` 的 ``setPort``/``setProtocol``/
``setProp``/``handleControl``/``DEFAULTS``）。

为什么这些函数是**同步**的：Python 侧不需要 JS 的 Promise 编排；``bridge.send_command``
本身会**阻塞**等待采集器 ack（由另一线程投递），因此 HTTP 处理线程与自动化工作线程都能
直接调用。这样 ``AutoARunner.evaluate`` 也保持同步、无需事件循环。

校验纪律：所有取值都对着**采集器 hello 里声明的清单**（``writable`` / ``switches``）校验 ——
界面即使被改坏，也写不进清单外的东西。
"""

from __future__ import annotations

import math
import time

# 端口开关的位定义（piid 16 位掩码）：bit0=C1, bit1=C2, bit2=C3, bit3=A。
PORT_BITS = {"c1": 0, "c2": 1, "c3": 2, "a": 3}

# 恢复常用状态：四口全开、场景 AI。倒计时属性在本固件上不存在，不碰。（``server.js`` 同）
DEFAULTS = [
    {"piid": 16, "value": 0x0F, "label": "所有端口开启"},
    {"piid": 5, "value": 1, "label": "场景模式 = AI"},
]

# defaults 每步之间的喘息间隔（ms）。
_DEFAULTS_STEP_MS = 260


def _settings(bridge):
    latest = bridge.state.latest
    s = latest.get("settings") if isinstance(latest, dict) else None
    return s if isinstance(s, dict) else None


def set_port(bridge, port: str, action: str) -> dict:
    """端口开关：读-改-写 piid 16 的位掩码（bit0..3 = c1,c2,c3,a）。

    返回值对齐 ``server.js``：``{ok:true,piid:16,value,readback,name}`` /
    ``{ok:true,piid:16,value,readback,noop:true}`` / ``{ok:false,error}``。
    """
    cur = 0x0F
    s = _settings(bridge)
    if s is not None and isinstance(s.get("port_ctl"), int) and not isinstance(s.get("port_ctl"), bool):
        cur = s["port_ctl"]

    if port == "all":
        nxt = 0x0F if action == "on" else 0x00
    elif port in PORT_BITS:
        bit = 1 << PORT_BITS[port]
        nxt = (cur | bit) if action == "on" else (cur & ~bit)
    else:
        return {"ok": False, "error": "未知端口：" + str(port)}

    if nxt == cur:
        return {"ok": True, "piid": 16, "value": cur, "readback": cur, "noop": True}
    return bridge.send_command({"cmd": "set", "piid": 16, "value": nxt})


def set_protocol(bridge, port: str, sw: str, on: bool) -> dict:
    """协议开关：读-改-写 piid 21（扩展协议控制，u32）的位掩码。

    位定义来自采集器 hello 的 ``switches`` 清单（``collector.py`` 逐位实测过）。
    c1/c2 各有一位置 1 的保留位，这里只翻转目标位、保留位天然不动。
    """
    table = bridge.state.switches if isinstance(bridge.state.switches, list) else []
    hit = None
    for item in table:
        if isinstance(item, dict) and item.get("port") == port and item.get("sw") == sw:
            hit = item
            break
    if hit is None:
        return {"ok": False, "error": "未知协议开关 %s/%s" % (port or "?", sw or "?")}

    s = _settings(bridge)
    cur = s.get("protocol_ctl_extend") if s is not None else None
    if not isinstance(cur, int) or isinstance(cur, bool):
        return {"ok": False, "error": "还没读到协议开关的当前值，等一次轮询后再试"}

    try:
        bit = int(hit.get("bit"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "协议开关位定义缺失"}
    nxt = (cur | (1 << bit)) if on else (cur & ~(1 << bit))
    if nxt == cur:
        return {"ok": True, "piid": 21, "value": cur, "readback": cur, "noop": True}
    return bridge.send_command({"cmd": "set", "piid": 21, "value": nxt & 0xFFFFFFFF})


def _as_int(value):
    """模拟 ``Number(value)`` + ``Number.isInteger``：整数（含布尔、整数字符串）→ int，否则 None。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if isinstance(value, str):
        try:
            n = float(value.strip())
        except (TypeError, ValueError):
            return None
        return int(n) if math.isfinite(n) and n.is_integer() else None
    return None


def set_prop(bridge, piid: int, value, advanced: bool = False) -> dict:
    """单属性写入。取值对着采集器 ``writable`` 清单校验（清单外/越界/非整数一律拒绝）。

    ``advanced=True`` 表示"仅允许高级区属性"（``server.js`` 的 ``onlyAdvanced``）。
    """
    writable = bridge.state.writable if isinstance(bridge.state.writable, list) else []
    spec = None
    for item in writable:
        if isinstance(item, dict) and item.get("piid") == piid:
            spec = item
            break
    if spec is None:
        return {"ok": False, "error": "属性 2.%s 不在可写清单内" % piid}
    if advanced and not spec.get("advanced"):
        return {"ok": False, "error": "属性 2.%s 不属于高级区" % piid}

    n = _as_int(value)
    if n is None:
        return {"ok": False, "error": "取值必须是整数"}

    lo = spec.get("min")
    hi = spec.get("max")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and not (lo <= n <= hi):
        return {"ok": False, "error": "%s 取值需在 %s..%s 之间" % (spec.get("name"), lo, hi)}

    return bridge.send_command({"cmd": "set", "piid": piid, "value": n})


def handle_control(bridge, body: dict | None) -> dict:
    """``/api/control`` 总入口：按 ``action`` 分发。"""
    if not isinstance(body, dict):
        return {"ok": False, "error": "请求体不是合法 JSON"}
    action = body.get("action")

    if action == "port":
        return set_port(bridge, body.get("port"), "on" if body.get("on") else "off")

    if action == "protocol":
        return set_protocol(bridge, body.get("port"), body.get("sw"), bool(body.get("on")))

    # 用"把 2.7 换成 2.8"的 15 项替换形状，把常态轮询读不到的 2.8 读出来。
    if action == "ext":
        return bridge.send_command({"cmd": "ext-read"})

    # MIOT action #1「恢复默认设置」：结构由 collector.py 定义，超时放宽到 30s。
    if action == "reset":
        return bridge.send_command({"cmd": "reset"}, 30000)

    if action == "set":
        return set_prop(bridge, body.get("piid"), body.get("value"), bool(body.get("advanced")))

    if action == "defaults":
        done = []
        for step in DEFAULTS:
            r = set_prop(bridge, step["piid"], step["value"])
            ok = bool(r and r.get("ok"))
            done.append({"piid": step["piid"], "label": step["label"], "ok": ok,
                         "error": (r or {}).get("error") or None})
            if not ok:                       # 任一步失败即中止并返回 done
                return {"ok": False,
                        "error": "%s 失败：%s" % (step["label"], (r or {}).get("error")),
                        "done": done}
            time.sleep(_DEFAULTS_STEP_MS / 1000.0)   # 让设备一条一条喘口气
        return {"ok": True, "done": done}

    return {"ok": False, "error": "未知动作：" + str(action)}


__all__ = ["PORT_BITS", "DEFAULTS", "set_port", "set_protocol", "set_prop", "handle_control"]
