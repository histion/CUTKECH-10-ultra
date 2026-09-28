"""电量账本：功率 → 电量积分、分日分桶、充电会话、回填与快照。

充电头没有"累计电量"属性可读（只有瞬时 V/A/W），电量只能由本应用对功率做**时间积分**
得到 —— 这正是米家 App 的做法（只在连接期间记录，断开就没数据）。把积分结果按天分桶
存盘，重启后从 ``history.jsonl`` 回填断档，只要应用在跑就不会丢。

等价重写 ``server.js`` 的：
``emptyEnergy/loadEnergy/saveEnergy/touchEnergy/sampleFrom/integrateEnergy/trackSession/
backfillEnergy/energySnapshot``。

关键常量与语义：
  * ``DAYS_KEEP=400`` —— 只保留最近 400 天；
  * ``SESSION_GAP_MS=60000`` —— 空载持续 > 60s 视为一次充电结束；
  * ``ENERGY_SAVE_MS=15000`` —— 脏标记节流保存（15s）；
  * ``SESSION_MIN_W=auto_a.LOAD_W`` —— 与 A 口自动化共用 0.5W 空载门限（避免两处魔法数）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import paths
from .auto_a import LOAD_W
from .state_store import PORTS, day_key, log, map_ports, round3, zero_ports

DAYS_KEEP = 400
SESSION_GAP_MS = 60000
ENERGY_SAVE_MS = 15000
SESSION_MIN_W = LOAD_W

# 充电会话归档上限（save 时裁剪）。
_SESSIONS_KEEP = 50
# 快照里回传的会话条数（最近 N 条倒序）。
_SESSIONS_SNAPSHOT = 20


class EnergyLedger:
    """电量账本（线程安全）。"""

    def __init__(self, path: str | Path | None = None, push_log=None) -> None:
        self.path = Path(path) if path is not None else (paths.data_dir() / "energy.json")
        self.push_log = push_log if callable(push_log) else (lambda _m: None)
        self._lock = threading.RLock()
        self.data: dict = self._empty()
        self.dirty = False
        self._timer: threading.Timer | None = None

    # -------------------------------------------------------------- 初始化

    @staticmethod
    def _empty() -> dict:
        return {
            "version": 1,
            "totalWh": 0,
            "ports": zero_ports(),
            "days": {},
            "lastAt": 0,
            "firstAt": 0,
            "lastSample": None,
            "current": None,
            "sessions": [],
        }

    def load(self) -> None:
        """读盘；任何异常回退空结构。（等价 ``loadEnergy``）"""
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("energy.json 不是对象")
                data = self._empty()
                data.update(raw)
                ports = zero_ports()
                rp = raw.get("ports")
                if isinstance(rp, dict):
                    for pid in PORTS:
                        if pid in rp:
                            ports[pid] = rp[pid]
                data["ports"] = ports
                data["days"] = raw.get("days") if isinstance(raw.get("days"), dict) else {}
                data["sessions"] = raw.get("sessions") if isinstance(raw.get("sessions"), list) else []
                self.data = data
            except Exception:
                self.data = self._empty()

    def save(self, force: bool = False) -> None:
        """落盘：裁剪天数/会话 → 写 JSON。``force=True`` 时即使不脏也写（退出时用）。"""
        with self._lock:
            if not self.dirty and not force:
                return
            self.dirty = False
            data = self.data
            try:
                keys = sorted(data["days"].keys())
                if len(keys) > DAYS_KEEP:
                    for k in keys[: len(keys) - DAYS_KEEP]:
                        del data["days"][k]
                if len(data["sessions"]) > _SESSIONS_KEEP:
                    data["sessions"] = data["sessions"][-_SESSIONS_KEEP:]
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except Exception as exc:  # noqa: BLE001 -- 保存失败不该冒泡
                log("save energy failed: %s" % exc)
            self._cancel_timer()

    def touch(self) -> None:
        """标记脏并安排一次 15s 节流保存（等价 ``touchEnergy``）。"""
        self.dirty = True
        if self._timer is not None:
            return
        timer = threading.Timer(ENERGY_SAVE_MS / 1000.0, self._on_timer)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _on_timer(self) -> None:
        with self._lock:
            self._timer = None
        self.save()

    def _cancel_timer(self) -> None:
        timer = self._timer
        if timer is not None:
            self._timer = None
            try:
                timer.cancel()
            except Exception:
                pass

    # -------------------------------------------------------------- 采样

    @staticmethod
    def sample_from(row: dict) -> dict:
        """历史行 → 积分用采样 ``{at,total,w}``（四口补零、数值化）。"""
        w = zero_ports()
        rw = row.get("w") if isinstance(row, dict) else None
        rw = rw if isinstance(rw, dict) else {}
        for pid in PORTS:
            try:
                w[pid] = float(rw.get(pid) or 0)
            except (TypeError, ValueError):
                w[pid] = 0.0
        at = row.get("at") if isinstance(row, dict) else None
        try:
            total = float(row.get("total") or 0)
        except (TypeError, ValueError):
            total = 0.0
        return {"at": at, "total": total, "w": w}

    def integrate(self, s: dict) -> None:
        """梯形法积分一帧采样（等价 ``integrateEnergy``）。

        ⚠️ 两次采样间隔 > 60s 整段丢弃 —— 中间发生了什么没人知道，宁可少算也不要瞎算。
        无论是否积分，都更新 ``firstAt/lastSample/lastAt`` 并跑一次会话跟踪。
        """
        with self._lock:
            data = self.data
            prev = data.get("lastSample")
            cur_at = s.get("at")
            if (isinstance(prev, dict) and isinstance(prev.get("at"), (int, float))
                    and isinstance(cur_at, (int, float)) and cur_at > prev["at"]):
                gap = cur_at - prev["at"]
                if gap <= SESSION_GAP_MS:
                    self._integrate_step(prev, s, gap)

            if not data.get("firstAt"):
                data["firstAt"] = cur_at
            data["lastSample"] = s
            data["lastAt"] = cur_at
            self.track_session(s)

    def _integrate_step(self, prev: dict, s: dict, gap: float) -> None:
        """一帧梯形积分的纯累加（总账 / 分日桶 / 会话），假定间隔已通过 60s 门限。"""
        data = self.data
        dt_h = gap / 3600000.0

        def _avg(a, b):
            return (max(0.0, float(a or 0)) + max(0.0, float(b or 0))) / 2.0

        d_total = _avg(prev.get("total"), s.get("total")) * dt_h
        ports = zero_ports()
        pw = prev.get("w") if isinstance(prev.get("w"), dict) else {}
        sw = s.get("w") if isinstance(s.get("w"), dict) else {}
        for pid in PORTS:
            ports[pid] = _avg(pw.get(pid), sw.get(pid)) * dt_h

        data["totalWh"] = data.get("totalWh", 0) + d_total
        key = day_key(s.get("at"))
        bucket = data["days"].get(key)
        if not isinstance(bucket, dict):
            bucket = {"total": 0, "ports": zero_ports()}
            data["days"][key] = bucket
        bucket["total"] = bucket.get("total", 0) + d_total
        bports = zero_ports()
        bports.update(bucket.get("ports") if isinstance(bucket.get("ports"), dict) else {})
        bucket["ports"] = bports
        for pid in PORTS:
            data["ports"][pid] = data["ports"].get(pid, 0) + ports[pid]
            bports[pid] = bports.get(pid, 0) + ports[pid]

        current = data.get("current")
        if isinstance(current, dict):
            current["wh"] = current.get("wh", 0) + d_total
            cports = zero_ports()
            cports.update(current.get("ports") if isinstance(current.get("ports"), dict) else {})
            current["ports"] = cports
            for pid in PORTS:
                cports[pid] = cports.get(pid, 0) + ports[pid]
        self.dirty = True

    def track_session(self, s: dict) -> None:
        """充电会话跟踪（等价 ``trackSession``）：有载开启/续期，空载 > 60s 归档。"""
        data = self.data
        try:
            total = float(s.get("total") or 0)
        except (TypeError, ValueError):
            total = 0.0
        active = total > SESSION_MIN_W
        cur_at = s.get("at")

        if active:
            if not isinstance(data.get("current"), dict):
                data["current"] = {"start": cur_at, "wh": 0, "peak": 0,
                                   "ports": zero_ports(), "lastActiveAt": cur_at}
            current = data["current"]
            current["peak"] = max(current.get("peak", 0), total)
            current["lastActiveAt"] = cur_at
            self.dirty = True
            return

        current = data.get("current")
        last_active = current.get("lastActiveAt") if isinstance(current, dict) else None
        if (isinstance(current, dict) and isinstance(last_active, (int, float))
                and isinstance(cur_at, (int, float)) and (cur_at - last_active) > SESSION_GAP_MS):
            data["sessions"].append({
                "start": current.get("start"),
                "end": last_active,
                "ms": last_active - (current.get("start") or last_active),
                "wh": round3(current.get("wh", 0)),
                "peak": round3(current.get("peak", 0)),
                "ports": map_ports(current.get("ports")),
            })
            data["current"] = None
            self.dirty = True
            self.push_log("一次充电结束：%s Wh，峰值 %s W"
                          % (round3(current.get("wh", 0)), round3(current.get("peak", 0))))

    # -------------------------------------------------------------- 回填

    def backfill(self) -> int:
        """启动时把上次退出→本次启动之间 ``history.jsonl`` 里仍留存的采样补算进去。"""
        try:
            hist = self.path.parent / "history.jsonl"
            rows: list = []
            for line in hist.read_text(encoding="utf-8").split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if isinstance(r, dict) and isinstance(r.get("at"), (int, float)):
                    rows.append(r)
        except OSError:
            return 0
        if not rows:
            return 0
        rows.sort(key=lambda r: r["at"])
        cut = self.data.get("lastAt") or 0
        used = 0
        for r in rows:
            s = self.sample_from(r)
            if s["at"] <= cut:
                self.data["lastSample"] = s
                if not self.data.get("firstAt"):
                    self.data["firstAt"] = s["at"]
                continue
            self.integrate(s)
            used += 1
        return used

    # -------------------------------------------------------------- 快照

    def snapshot(self, days) -> dict:
        """``/api/energy?days=N`` 的响应结构（逐字段对齐 design §6 / ``energySnapshot``）。

        * ``days`` 数组由旧到新、长度 = ``n``、含当天；
        * ``sessions`` 最近 20 条倒序（最新在前）；
        * ``since`` = ``firstAt || null``、``tracked`` = ``lastAt || null``。
        """
        try:
            n = max(1, min(90, int(days or 7)))
        except (TypeError, ValueError):
            n = 7
        now_ms = datetime.now().timestamp() * 1000
        today = datetime.fromtimestamp(datetime.now().timestamp()).replace(
            hour=12, minute=0, second=0, microsecond=0)
        days_list = []
        for i in range(n - 1, -1, -1):
            d = today - timedelta(days=i)
            key = d.strftime("%Y-%m-%d")
            bucket = self.data["days"].get(key)
            if isinstance(bucket, dict):
                days_list.append({"day": key, "wh": round3(bucket.get("total")),
                                  "ports": map_ports(bucket.get("ports"))})
            else:
                days_list.append({"day": key, "wh": 0, "ports": zero_ports()})

        current = self.data.get("current")
        current_out = None
        if isinstance(current, dict):
            start = current.get("start") or 0
            last_active = current.get("lastActiveAt") or 0
            current_out = {
                "start": current.get("start"),
                "lastActiveAt": current.get("lastActiveAt"),
                "ms": last_active - start,
                "wh": round3(current.get("wh", 0)),
                "peak": round3(current.get("peak", 0)),
                "ports": map_ports(current.get("ports")),
            }

        return {
            "ok": True,
            "today": days_list[-1] if days_list else {"day": day_key(now_ms), "wh": 0,
                                                      "ports": zero_ports()},
            "totalWh": round3(self.data.get("totalWh", 0)),
            "ports": map_ports(self.data.get("ports")),
            "days": days_list,
            "current": current_out,
            "sessions": list(reversed(self.data["sessions"][-_SESSIONS_SNAPSHOT:])),
            "since": self.data.get("firstAt") or None,
            "tracked": self.data.get("lastAt") or None,
        }


__all__ = ["EnergyLedger", "DAYS_KEEP", "SESSION_GAP_MS", "ENERGY_SAVE_MS", "SESSION_MIN_W"]
