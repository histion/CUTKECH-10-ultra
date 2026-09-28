"""T02 自测（一）：电量账本 —— 裁剪 / 跨度丢弃 / 会话 / 快照结构。

离线运行：python pyapp/tests/test_energy.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("OK  " + name)
    else:
        FAIL += 1
        print("BAD " + name + (("  → " + detail) if detail else ""))


def sample(at, total, ports=None):
    w = {"c1": 0, "c2": 0, "c3": 0, "a": 0}
    if ports:
        w.update(ports)
    return {"at": at, "total": total, "w": w}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cuk_energy_"))
    os.environ["CUKTECH_HOME"] = str(tmp)
    os.environ.pop("CUKTECH_DATA_DIR", None)
    try:
        from pyapp.state_store import AppState, HISTORY_MAX
        from pyapp.energy_ledger import EnergyLedger, DAYS_KEEP, SESSION_GAP_MS

        # --- push_history 超上限裁剪 ---
        st = AppState()
        for i in range(HISTORY_MAX + 1):
            st.push_history({"at": 1000 + i, "ports": [{"id": "c1", "w": 1}]})
        check("push_history 裁剪到 HISTORY_MAX(%d)" % HISTORY_MAX,
              len(st.history) == HISTORY_MAX, "got %d" % len(st.history))
        check("push_history 保留最新行（at 递增）",
              st.history[-1]["at"] == 1000 + HISTORY_MAX)

        # --- 梯形积分：正常间隔累加 ---
        led = EnergyLedger(path=tmp / "energy.json")
        led.load()
        led.integrate(sample(1000, 10, {"c1": 10}))
        led.integrate(sample(2000, 10, {"c1": 10}))   # gap 1s
        exp = 10.0 * (1000 / 3600000.0)
        check("梯形积分 gap=1s 累加正确",
              abs(led.data["totalWh"] - exp) < 1e-9,
              "got %r want %r" % (led.data["totalWh"], exp))

        # --- 梯形积分：间隔 > 60s 整段丢弃 ---
        led2 = EnergyLedger(path=tmp / "energy2.json")
        led2.load()
        led2.integrate(sample(1000, 10, {"c1": 10}))
        led2.integrate(sample(1000 + SESSION_GAP_MS + 1000, 10, {"c1": 10}))
        check("间隔 > 60s 整段丢弃（totalWh 保持 0）",
              led2.data["totalWh"] == 0, "got %r" % led2.data["totalWh"])
        check("跨度丢弃后仍刷新 lastAt",
              led2.data["lastAt"] == 1000 + SESSION_GAP_MS + 1000)

        # --- 充电会话：有载开启，空载 > 60s 归档 ---
        led3 = EnergyLedger(path=tmp / "energy3.json")
        led3.load()
        led3.integrate(sample(0, 30, {"c1": 30}))          # active → 开会话
        check("有载即开启充电会话", isinstance(led3.data["current"], dict))
        led3.integrate(sample(70000, 0))                   # 空载 > 60s → 归档
        check("空载 > 60s 后会话归档（current 置空）", led3.data["current"] is None)
        check("会话列表长度 1", len(led3.data["sessions"]) == 1,
              str(led3.data["sessions"]))

        # --- snapshot 结构对齐 design §6 ---
        snap = led.snapshot(7)
        check("snapshot 顶层键集合正确",
              set(snap.keys()) == {"ok", "today", "totalWh", "ports", "days",
                                   "current", "sessions", "since", "tracked"},
              str(sorted(snap.keys())))
        check("snapshot.days 长度 = n(7)", len(snap["days"]) == 7)
        check("snapshot.days 由旧到新、含当天",
              snap["days"][-1]["day"] == snap["today"]["day"])
        check("snapshot.today 键 = {day,wh,ports}",
              set(snap["today"].keys()) == {"day", "wh", "ports"})
        check("snapshot.ports 键 = 四口",
              set(snap["ports"].keys()) == {"c1", "c2", "c3", "a"})
        check("snapshot.ok == True", snap["ok"] is True)

        # --- sample_from 静态方法 ---
        s = EnergyLedger.sample_from({"at": 5, "total": 3.5, "w": {"c1": 2}})
        check("sample_from 四口补零 + 数值化",
              s["w"] == {"c1": 2.0, "c2": 0.0, "c3": 0.0, "a": 0.0} and s["total"] == 3.5,
              str(s))

        # --- save 裁剪：天数 400 / 会话 50 ---
        led4 = EnergyLedger(path=tmp / "energy4.json")
        led4.load()
        led4.data["days"] = {"2026-%03d" % i: {"total": 1, "ports": {"c1": 1}}
                             for i in range(DAYS_KEEP + 5)}
        led4.data["sessions"] = [{"start": i} for i in range(60)]
        led4.dirty = True
        led4.save(force=True)
        check("save 裁剪天数到 DAYS_KEEP(400)", len(led4.data["days"]) == DAYS_KEEP,
              "got %d" % len(led4.data["days"]))
        check("save 裁剪会话到 50", len(led4.data["sessions"]) == 50)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 60)
    print("T02(账本) 汇总：通过 %d，失败 %d" % (PASS, FAIL))
    print("结果：" + ("PASS" if FAIL == 0 else "FAIL"))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
