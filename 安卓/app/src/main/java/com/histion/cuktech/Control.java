package com.histion.cuktech;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * /api/control 各动作（对齐 pyapp/control.py）：端口开关、协议开关、单属性写入、
 * 扩展读取、恢复出厂、恢复常用默认。取值一律对着采集器声明的清单校验。
 */
final class Control {
    private Control() {}

    static final int[] PORT_BITS = {0, 1, 2, 3};        // c1,c2,c3,a
    static final Object[][] DEFAULTS = {
            {16, 0x0F, "所有端口开启"},
            {5, 1, "场景模式 = AI"},
    };

    private static JSONObject settings(AppState state) {
        JSONObject latest = state.latest;
        return latest == null ? null : latest.optJSONObject("settings");
    }

    static JSONObject setPort(AutoA.Bridge bridge, AppState state, String port, boolean on) {
        int cur = 0x0F;
        JSONObject s = settings(state);
        if (s != null && s.has("port_ctl")) {
            Object v = s.opt("port_ctl");
            if (v instanceof Integer || v instanceof Long) cur = ((Number) v).intValue();
        }
        int next;
        if ("all".equals(port)) {
            next = on ? 0x0F : 0x00;
        } else {
            int idx = java.util.Arrays.asList(Miot.PORT_ORDER).indexOf(port);
            if (idx < 0) return err("未知端口：" + port);
            int bit = 1 << idx;
            next = on ? (cur | bit) : (cur & ~bit);
        }
        if (next == cur) {
            JSONObject o = new JSONObject();
            try {
                o.put("ok", true); o.put("piid", 16); o.put("value", cur);
                o.put("readback", cur); o.put("noop", true);
            } catch (Exception ignored) { }
            return o;
        }
        JSONObject cmd = new JSONObject();
        try { cmd.put("cmd", "set"); cmd.put("piid", 16); cmd.put("value", next); } catch (Exception ignored) { }
        return bridge.sendCommand(cmd, 15000);
    }

    static JSONObject setProtocol(AutoA.Bridge bridge, AppState state, String port, String sw, boolean on) {
        JSONArray table = state.switches;
        int bit = -1;
        if (table != null) {
            for (int i = 0; i < table.length(); i++) {
                JSONObject it = table.optJSONObject(i);
                if (it == null) continue;
                if (port.equals(it.optString("port")) && sw.equals(it.optString("sw"))) {
                    bit = it.optInt("bit", -1);
                    break;
                }
            }
        }
        if (bit < 0) return err("未知协议开关 " + port + "/" + sw);

        JSONObject s = settings(state);
        long cur;
        if (s == null || !s.has("protocol_ctl_extend")) return err("还没读到协议开关的当前值，等一次轮询后再试");
        cur = s.optLong("protocol_ctl_extend", 0);
        long next = on ? (cur | (1L << bit)) : (cur & ~(1L << bit));
        if (next == cur) {
            JSONObject o = new JSONObject();
            try {
                o.put("ok", true); o.put("piid", 21); o.put("value", cur);
                o.put("readback", cur); o.put("noop", true);
            } catch (Exception ignored) { }
            return o;
        }
        JSONObject cmd = new JSONObject();
        try { cmd.put("cmd", "set"); cmd.put("piid", 21); cmd.put("value", next & 0xFFFFFFFFL); } catch (Exception ignored) { }
        return bridge.sendCommand(cmd, 15000);
    }

    /** 模拟 JS ``Number(v)`` + ``Number.isInteger``。 */
    static Integer asInt(Object value) {
        if (value == null) return null;
        if (value instanceof Boolean) return ((Boolean) value) ? 1 : 0;
        if (value instanceof Integer) return (Integer) value;
        if (value instanceof Long) return (int) (long) (Long) value;
        if (value instanceof Double || value instanceof Float) {
            double d = ((Number) value).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) return null;
            if (d != Math.floor(d)) return null;
            return (int) d;
        }
        if (value instanceof String) {
            String s = ((String) value).trim();
            if (s.isEmpty()) return null;
            try {
                double d = Double.parseDouble(s);
                if (Double.isNaN(d) || Double.isInfinite(d)) return null;
                if (d != Math.floor(d)) return null;
                return (int) d;
            } catch (Exception e) {
                return null;
            }
        }
        return null;
    }

    static JSONObject setProp(AutoA.Bridge bridge, AppState state, Object piidObj, Object valueObj, boolean advanced) {
        Integer piid = asInt(piidObj);
        if (piid == null) return err("属性号非法");
        JSONArray writable = state.writable;
        JSONObject spec = null;
        if (writable != null) {
            for (int i = 0; i < writable.length(); i++) {
                JSONObject it = writable.optJSONObject(i);
                if (it != null && it.optInt("piid", -1) == piid) { spec = it; break; }
            }
        }
        if (spec == null) return err("属性 2." + piid + " 不在可写清单内");
        if (advanced && !spec.optBoolean("advanced", false)) return err("属性 2." + piid + " 不属于高级区");

        Integer n = asInt(valueObj);
        if (n == null) return err("取值必须是整数");
        long lo = spec.optLong("min", 0);
        long hi = spec.optLong("max", 255);
        if (n < lo || n > hi) return err(spec.optString("name") + " 取值需在 " + lo + ".." + hi + " 之间");

        JSONObject cmd = new JSONObject();
        try { cmd.put("cmd", "set"); cmd.put("piid", piid.intValue()); cmd.put("value", n.intValue()); } catch (Exception ignored) { }
        return bridge.sendCommand(cmd, 15000);
    }

    static JSONObject handleControl(AutoA.Bridge bridge, AppState state, JSONObject body) {
        if (body == null) return err("请求体不是合法 JSON");
        String action = body.optString("action");
        if ("port".equals(action)) {
            return setPort(bridge, state, body.optString("port"), body.optBoolean("on"));
        }
        if ("protocol".equals(action)) {
            return setProtocol(bridge, state, body.optString("port"), body.optString("sw"), body.optBoolean("on"));
        }
        if ("ext".equals(action)) {
            JSONObject cmd = new JSONObject();
            try { cmd.put("cmd", "ext-read"); } catch (Exception ignored) { }
            return bridge.sendCommand(cmd, 15000);
        }
        if ("reset".equals(action)) {
            JSONObject cmd = new JSONObject();
            try { cmd.put("cmd", "reset"); } catch (Exception ignored) { }
            return bridge.sendCommand(cmd, 30000);
        }
        if ("set".equals(action)) {
            return setProp(bridge, state, body.opt("piid"), body.opt("value"), body.optBoolean("advanced"));
        }
        if ("defaults".equals(action)) {
            JSONArray done = new JSONArray();
            for (Object[] step : DEFAULTS) {
                JSONObject r = setProp(bridge, state, step[0], step[1], false);
                boolean ok = r != null && r.optBoolean("ok", false);
                JSONObject rec = new JSONObject();
                try {
                    rec.put("piid", step[0]);
                    rec.put("label", step[2]);
                    rec.put("ok", ok);
                    rec.put("error", ok ? JSONObject.NULL : r.optString("error", null));
                } catch (Exception ignored) { }
                done.put(rec);
                if (!ok) {
                    JSONObject out = new JSONObject();
                    try {
                        out.put("ok", false);
                        out.put("error", step[2] + " 失败：" + r.optString("error", "未知"));
                        out.put("done", done);
                    } catch (Exception ignored) { }
                    return out;
                }
                try { Thread.sleep(260); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
            }
            JSONObject out = new JSONObject();
            try { out.put("ok", true); out.put("done", done); } catch (Exception ignored) { }
            return out;
        }
        return err("未知动作：" + action);
    }

    static JSONObject err(String msg) {
        JSONObject o = new JSONObject();
        try { o.put("ok", false); o.put("error", msg); } catch (Exception ignored) { }
        return o;
    }
}
