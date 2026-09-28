"""Xiaomi-cloud QR login for the CUKTECH AD1204U.

    login.py qr    [--address MAC]                  -> data/qr.png + data/qr-state.json
    login.py poll  [--address MAC] [--wait 280]     -> data/cuktech.token

Both subcommands print ONE json object on stdout so server.js can parse the
result without screen-scraping.  Everything else goes to stderr.

Only the QR path is implemented: no password, no 2FA, nothing is stored beyond
the 12-byte BLE bind key that the charger needs.
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

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", newline="\n")
    except Exception:  # noqa: BLE001
        pass

# 数据目录：单文件 exe 版由引导脚本（src/sea-main.js）通过 CUKTECH_DATA_DIR
# 指定到 %LOCALAPPDATA% 下的固定位置，必须与 server.js 的 DATA 落点一致，
# 否则 server.js 读不到这里生成的二维码/登录态。便携版不设该环境变量时，
# 仍回退到项目目录下的 data/（与 server.js 的 APP/data 一致）。
DATA = Path(os.environ.get("CUKTECH_DATA_DIR") or (HERE / "data"))
STATE_FILE = DATA / "qr-state.json"
TOKEN_FILE = DATA / "cuktech.token"
QR_PNG = DATA / "qr.png"

REGIONS = ["cn", "sg", "i2", "de", "us", "tw", "ru", "in"]

# Errors that mean "stop", everything else is retried until the deadline.
# HTTP 403 shows up once Xiaomi has invalidated the ticket; retrying it just
# produces hundreds of identical log lines.
TERMINAL = ("code=", "missing location", "serviceToken", "redirect failed",
            "HTTP 400", "HTTP 403", "HTTP 404")

# 便携包要发给别人的时候，对方不可能先知道充电头的 MAC，所以支持「auto」：
# 直接在账号设备列表里凭型号/名字认出 AD1204U。
AD1204_HINTS = ("njcuk.fitting.ad1204", "njcuk.fitting.1204e")


def _valid_token(raw: str) -> str | None:
    """设备列表里的 token 字段可能是 12 或 16 字节；BLE 只用前 12 字节。"""
    t = (raw or "").strip().lower()
    if len(t) not in (24, 32):
        return None
    try:
        bytes.fromhex(t)
    except ValueError:
        return None
    return t[:24]


def _looks_like_ad1204(dev: dict) -> bool:
    model = str(dev.get("model", "")).lower()
    name = str(dev.get("name", "")).lower()
    if any(h in model for h in AD1204_HINTS):
        return True
    return "ad1204" in model or "ad1204" in name


def out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def note(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _ensure_data() -> None:
    """确保数据目录存在。

    exe 版的数据目录在 %LOCALAPPDATA%\\cuktech10ultra\\data，首次扫码登录时
    这个目录可能还没有；qr / poll 两条路径都要在写文件前把它建出来，否则
    ``Path.write_text`` 会因父目录不存在而抛 FileNotFoundError。
    便携版这行等价于原来的 ``DATA.mkdir(...)``（DATA == HERE/data）。
    """
    DATA.mkdir(parents=True, exist_ok=True)


async def cmd_qr(args) -> int:
    import aiohttp
    from cuktech_ble.xiaomi_cloud import _fresh_user_agent, start_qr_login

    _ensure_data()
    async with aiohttp.ClientSession() as session:
        qr = await start_qr_login(session)
        png_ok = False
        try:
            async with session.get(
                qr.qr_image_url,
                headers={"User-Agent": _fresh_user_agent()},
                cookies=qr.cookies,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                blob = await r.read()
                if r.status == 200 and len(blob) > 200:
                    QR_PNG.write_bytes(blob)
                    png_ok = True
                else:
                    note("qr png fetch: HTTP %s, %d bytes" % (r.status, len(blob)))
        except Exception as exc:  # noqa: BLE001
            note("qr png fetch failed: %s: %s" % (type(exc).__name__, exc))

    STATE_FILE.write_text(json.dumps({
        "qr_image_url": qr.qr_image_url,
        "login_url": qr.login_url,
        "lp_url": qr.lp_url,
        "timeout_seconds": qr.timeout_seconds,
        "device_id": qr.device_id,
        "cookies": qr.cookies,
        "created_at": time.time(),
    }, indent=2), encoding="utf-8")

    out({"ok": True, "png": str(QR_PNG) if png_ok else None,
         "timeout": qr.timeout_seconds, "stateFile": str(STATE_FILE)})
    return 0


async def _wait_for_scan(session, qr, max_wait: float, verbose: bool):
    from cuktech_ble.xiaomi_cloud import CloudError, QRLoginPending, poll_qr_login

    deadline = time.monotonic() + max_wait
    rounds = 0
    while True:
        rounds += 1
        try:
            return await poll_qr_login(session, qr)
        except QRLoginPending:
            if time.monotonic() >= deadline:
                raise CloudError("二维码登录超时（%ss）" % int(max_wait)) from None
            if verbose:
                note("  round %d: 尚未扫码，剩余 %ds"
                     % (rounds, int(deadline - time.monotonic())))
            await asyncio.sleep(1.0)
        except CloudError as exc:
            msg = str(exc)
            if any(m in msg for m in TERMINAL):
                raise
            if time.monotonic() >= deadline:
                raise CloudError("二维码登录超时（%ss，最后错误：%s）" % (int(max_wait), msg)) from exc
            note("  transient: %s" % msg)
            await asyncio.sleep(2.0)


async def cmd_poll(args) -> int:
    import aiohttp
    from cuktech_ble.xiaomi_cloud import (
        QRLogin, CloudError, find_token_by_mac, list_devices,
    )

    # poll 成功后会写 TOKEN_FILE；exe 版首次登录时数据目录可能尚不存在，
    # 这里先确保目录就绪，避免 write_text 因缺少父目录而失败。
    _ensure_data()

    if not STATE_FILE.exists():
        out({"ok": False, "error": "还没有生成二维码，请先执行 qr"})
        return 2
    st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    age = int(time.time() - float(st.get("created_at", time.time())))
    note("ticket age %ss" % age)

    qr = QRLogin(
        qr_image_url=st["qr_image_url"],
        login_url=st["login_url"],
        lp_url=st["lp_url"],
        timeout_seconds=st["timeout_seconds"],
        device_id=st["device_id"],
        cookies=st["cookies"],
    )

    async with aiohttp.ClientSession() as session:
        try:
            auth = await _wait_for_scan(session, qr, float(args.wait), args.verbose)
        except CloudError as exc:
            out({"ok": False, "error": str(exc)})
            return 1
        note("login OK user_id=%s" % auth.user_id)

        want = str(args.address or "").strip()
        auto = (not want) or want.lower() == "auto"
        regions = [r.strip() for r in args.regions.split(",")] if args.regions else REGIONS
        hit = None          # (region, mac, token, device_count)
        candidates = []     # auto 模式下认出来的所有充电头，回传给上层排查用
        for region in regions:
            try:
                devices = await list_devices(session, auth, region)
            except Exception as exc:  # noqa: BLE001
                note("  [%s] list failed: %s" % (region, exc))
                continue
            note("  [%s] %d device(s)" % (region, len(devices)))

            if auto:
                for dev in devices:
                    if not _looks_like_ad1204(dev):
                        continue
                    mac = str(dev.get("mac", "")).strip()
                    token = _valid_token(str(dev.get("token", "")))
                    note("  candidate: %s %s mac=%s token_ok=%s"
                         % (dev.get("name"), dev.get("model"), mac, bool(token)))
                    if not token:
                        continue
                    candidates.append({"name": dev.get("name"), "model": dev.get("model"), "mac": mac})
                    if hit is None:
                        hit = (region, mac, token, len(devices))
            else:
                try:
                    token = find_token_by_mac(devices, want)
                except CloudError as exc:
                    note("  [%s] token problem: %s" % (region, exc))
                    token = None
                if token:
                    hit = (region, want, token, len(devices))
            if hit:
                break

    if hit is None:
        if auto:
            out({"ok": False,
                 "error": "账号里没有找到酷态科 AD1204U —— 请确认已用米家 App 添加这台充电头、"
                          "并且它插着电在线；如果设备是别人分享给你的，需要先在米家里接受共享"})
        else:
            out({"ok": False, "error": "账号里没有找到蓝牙地址为 %s 的设备" % want})
        return 1

    region, mac, token, count = hit
    TOKEN_FILE.write_text(json.dumps({
        "address": mac, "token_hex": token, "region": region,
    }), encoding="utf-8")
    out({"ok": True, "region": region, "mac": mac, "devices": count,
         "candidates": candidates,
         "tokenLen": len(token), "tokenFile": str(TOKEN_FILE)})
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("qr")
    p.set_defaults(fn=cmd_qr)

    p = sub.add_parser("poll")
    p.add_argument("--address", default="",
                   help="设备 MAC；留空或 auto 则在账号设备列表里自动认出 AD1204U")
    p.add_argument("--wait", type=int, default=280)
    p.add_argument("--regions", default="")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(fn=cmd_poll)

    args = ap.parse_args()
    try:
        return asyncio.run(args.fn(args))
    except Exception as exc:  # noqa: BLE001
        note("FATAL: %s: %s" % (type(exc).__name__, exc))
        out({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
