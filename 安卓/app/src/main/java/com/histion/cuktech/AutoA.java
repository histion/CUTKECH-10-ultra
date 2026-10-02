package com.histion.cuktech;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * A 口自动化（对齐 pyapp/auto_a.py）：C1/C2/C3 任一有负载 → 自动开 USB-A + 小电流；
 * 三口全空 → 自动关。带迟滞（0.2~0.5W 保持当前）、连续 2 帧确认、最小 3s 间隔、
 * 同一目标最多写 3 次。
 */
final class AutoA {
    static final double LOAD_W = 0.5;
    static final double RELEASE_W = 0.2;
    static final String[] C_PORTS = {"c1", "c2", "c3"};
    static final int LOW_CURRENT_PIID = 0x0F;
    static final long AUTO_MIN_INTERVAL_MS = 3000;
    static final int AUTO_DEBOUNCE_SAMPLES = 2;
    static final int AUTO_MAX_WRITES = 3;

    private AutoA() {}

    /** 写设备的窄接口（由 CollectorBridge 实现）。 */
    interface Bridge {
        JSONObject sendCommand(JSONObject cmd, long timeoutMs);
    }

    /** 推 UI 日志的窄接口。 */
    interface Sink {
        void push(String msg);
    }

    static final class Decision {
        String band;
        boolean cLoaded, cAllReleased, aLoaded;
        boolean targetA, targetLow;
        Boolean curA, curLow;
        boolean needA, needLow, changed;
        String reason;
    }

    static double portW(JSONObject latest, String id) {
        if (latest == null) return 0;
        JSONArray ports = latest.optJSONArray("ports");
        if (ports == null) return 0;
        for (int i = 0; i < ports.length(); i++) {
            JSONObject p = ports.optJSONObject(i);
            if (p != null && id.equals(p.optString("id"))) return p.optDouble("w", 0);
        }
        return 0;
    }

    static Decision compute(JSONObject latest) {
        Decision d = new Decision();
        boolean cLoaded = false, cAllReleased = true;
        for (String p : C_PORTS) {
            double w = portW(latest, p);
            if (w > LOAD_W) cLoaded = true;
            if (w > RELEASE_W) cAllReleased = false;
        }
        d.cLoaded = cLoaded;
        d.cAllReleased = cAllReleased;
        d.aLoaded = portW(latest, "a") > LOAD_W;

        JSONObject s = latest == null ? null : latest.optJSONObject("settings");
        Boolean curA = null, curLow = null;
        if (s != null) {
            JSONObject en = s.optJSONObject("ports_enabled");
            if (en != null && en.has("a")) curA = en.optBoolean("a", false);
            if (s.has("usb_a_always_on")) curLow = s.optBoolean("usb_a_always_on", false);
        }
        d.curA = curA;
        d.curLow = curLow;
        boolean known = curA != null && curLow != null;

        if (cLoaded) { d.targetA = true; d.targetLow = true; d.band = "load"; }
        else if (cAllReleased) { d.targetA = false; d.targetLow = false; d.band = "idle"; }
        else if (known) { d.targetA = curA; d.targetLow = curLow; d.band = "hold"; }
        else { d.targetA = false; d.targetLow = false; d.band = "idle-no-hysteresis"; }

        d.needA = curA == null || (curA.booleanValue() != d.targetA);
        d.needLow = curLow == null || (curLow.booleanValue() != d.targetLow);
        d.changed = d.needA || d.needLow;

        if ("load".equals(d.band)) d.reason = "C 口有负载 → 目标：USB-A 开 + 小电流开";
        else if ("idle".equals(d.band)) d.reason = d.aLoaded
                ? "C 口空闲（A 口有负载）→ 目标：USB-A 关 + 小电流关"
                : "C 口空闲 → 目标：USB-A 关 + 小电流关";
        else if ("hold".equals(d.band)) d.reason = "C 口功率处于迟滞区间（" + RELEASE_W + "~" + LOAD_W + "W）→ 保持当前状态";
        else d.reason = "读不到当前状态 → 退化为按负载判定";
        return d;
    }

    static final class Budget {
        String lastKey;
        int attempts;
        String pausedKey;
        boolean allow;
        boolean pause;
        Budget next;

        Budget allow(String key, int maxWrites) {
            Budget out = new Budget();
            String lk = lastKey;
            int att = attempts;
            String pk = pausedKey;
            if (!key.equals(lk)) { lk = null; att = 0; pk = null; }

            Budget next = new Budget();
            if (key.equals(pk)) {
                out.allow = false; out.pause = false;
                next.lastKey = lk; next.attempts = att; next.pausedKey = pk;
            } else {
                int na = att + 1;
                if (na > maxWrites) {
                    out.allow = false; out.pause = true;
                    next.lastKey = key; next.attempts = att; next.pausedKey = key;
                } else {
                    out.allow = true; out.pause = false;
                    next.lastKey = key; next.attempts = na; next.pausedKey = null;
                }
            }
            out.next = next;
            return out;
        }
    }

    // ------------------------------------------------------------------ 运行态

    static final class Runner {
        private final AppState state;
        private final ConfigStore config;
        private final Bridge bridge;
        private final Sink sink;

        private volatile boolean busy;
        private volatile long lastActionAt;
        private String pendingKey;
        private int pendingCount;
        private Budget budget = new Budget();
        private Thread worker;

        Runner(AppState state, ConfigStore config, Bridge bridge, Sink sink) {
            this.state = state;
            this.config = config;
            this.bridge = bridge;
            this.sink = sink;
        }

        void resetCounters() {
            pendingKey = null;
            pendingCount = 0;
            budget = new Budget();
        }

        void requestEvaluate() {
            if (worker != null && worker.isAlive()) return;
            worker = new Thread(new Runnable() {
                @Override public void run() {
                    try {
                        evaluate();
                    } catch (Throwable t) {
                        if (sink != null) sink.push("A口自动化：判定出错 — " + t.getMessage());
                    }
                }
            }, "autoA");
            worker.setDaemon(true);
            worker.start();
        }

        void evaluate() {
            if (!config.autoA()) return;
            JSONObject link = state.snapshotLink();
            if (!"connected".equals(link.optString("state"))) return;
            JSONObject latest = state.latest;
            if (latest == null) return;
            if (busy) return;

            Decision d = compute(latest);
            if (!d.changed) { resetCounters(); return; }

            String key = d.targetA ? "on" : "off";
            if (key.equals(pendingKey)) pendingCount++;
            else { pendingKey = key; pendingCount = 1; }
            if (pendingCount < AUTO_DEBOUNCE_SAMPLES) return;

            long now = System.currentTimeMillis();
            if (now - lastActionAt < AUTO_MIN_INTERVAL_MS) return;

            Budget plan = budget.allow(key, AUTO_MAX_WRITES);
            budget = plan.next;
            if (plan.pause) {
                if (sink != null) sink.push("A口自动化：连续 " + AUTO_MAX_WRITES + " 次写入后状态仍未变化，暂停重试");
                return;
            }
            if (!plan.allow) return;
            apply(d);
        }

        private void apply(Decision d) {
            busy = true;
            lastActionAt = System.currentTimeMillis();
            String head;
            if (d.targetA) head = "A口自动化：C 口出现负载 → 开启 USB-A + 小电流";
            else if (d.aLoaded) head = "A口自动化：C 口空闲，按自动化关闭 USB-A（此时 A 口有负载）";
            else head = "A口自动化：C 口空闲 → 关闭 USB-A + 小电流";

            try {
                JSONObject r1, r2;
                String n1, n2;
                if (d.targetA) {
                    r1 = Control.setPort(bridge, state, "a", true); n1 = "USB-A";
                    r2 = Control.setProp(bridge, state, LOW_CURRENT_PIID, 1, false); n2 = "小电流";
                } else {
                    r1 = Control.setProp(bridge, state, LOW_CURRENT_PIID, 0, false); n1 = "小电流";
                    r2 = Control.setPort(bridge, state, "a", false); n2 = "USB-A";
                }
                StringBuilder bad = new StringBuilder();
                if (r1 == null || !r1.optBoolean("ok", false)) {
                    bad.append(n1).append("（").append(r1 == null ? "无响应" : r1.optString("error", "未知")).append("）");
                }
                if (r2 == null || !r2.optBoolean("ok", false)) {
                    if (bad.length() > 0) bad.append("；");
                    bad.append(n2).append("（").append(r2 == null ? "无响应" : r2.optString("error", "未知")).append("）");
                }
                if (sink != null) sink.push(bad.length() > 0 ? head + " 失败：" + bad : head);
            } catch (Throwable t) {
                if (sink != null) sink.push(head + " 失败：" + t.getMessage());
            } finally {
                busy = false;
                pendingKey = null;
                pendingCount = 0;
            }
        }
    }
}
