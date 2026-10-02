package com.histion.cuktech;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * MIOT 属性读写 + AD1204U 端口/设置解码（对齐 properties.py / ports.py / collector.py）。
 *
 * 请求形状是硬约束：这台固件只认**正好 15 项、且每一项都真实存在**的读取请求，
 * 少一项多一项都会被静默丢弃，所以 DEFAULT_QUERY 一个字都不能改。
 */
final class Miot {
    private Miot() {}

    /** 常态轮询读的 15 项（顺序与 ad1204u_read_props.DEFAULT_QUERY 一致）。 */
    static final int[][] DEFAULT_QUERY = new int[][]{
            {2, 1}, {2, 2}, {2, 3}, {2, 4},
            {2, 5}, {2, 6}, {2, 7},
            {2, 0x0f}, {2, 0x0d}, {2, 0x15},
            {2, 0x13}, {2, 0x14},
            {2, 0x11}, {2, 0x12},
            {2, 0x10},
    };

    static final String[] PORT_ORDER = {"c1", "c2", "c3", "a"};
    static final String[] PORT_LABEL = {"C1", "C2", "C3", "USB-A"};
    static final int[] PORT_PIID = {1, 2, 3, 4};
    /** {piid, highPort, lowPort} —— 2.17 = C1/C2，2.18 = C3/A。 */
    static final int[][] CAP_PIID = {{0x11, 0, 1}, {0x12, 2, 3}};

    static final Map<Integer, String> PROTOCOL_NAMES = new HashMap<Integer, String>();
    static final Map<Integer, String> MIJIA_PROTOCOLS = new HashMap<Integer, String>();

    static {
        PROTOCOL_NAMES.put(0x01, "pd");
        PROTOCOL_NAMES.put(0x03, "pd");
        PROTOCOL_NAMES.put(0x05, "pd");
        PROTOCOL_NAMES.put(0x06, "pd");
        PROTOCOL_NAMES.put(0x08, "pd_pps");
        PROTOCOL_NAMES.put(0x0a, "pd_fixed");
        PROTOCOL_NAMES.put(0x30, "pd");
        PROTOCOL_NAMES.put(0x60, "usb_a");
        PROTOCOL_NAMES.put(0x70, "usb_a_qc");
        PROTOCOL_NAMES.put(0x80, "pd");

        MIJIA_PROTOCOLS.put(0, "idle");
        MIJIA_PROTOCOLS.put(1, "5V");
        MIJIA_PROTOCOLS.put(2, "5V");
        MIJIA_PROTOCOLS.put(3, "QC");
        MIJIA_PROTOCOLS.put(4, "AFC");
        MIJIA_PROTOCOLS.put(5, "FCP");
        MIJIA_PROTOCOLS.put(6, "SCP");
        MIJIA_PROTOCOLS.put(7, "PD");
        MIJIA_PROTOCOLS.put(8, "PPS");
        MIJIA_PROTOCOLS.put(9, "PPS");
        MIJIA_PROTOCOLS.put(10, "UFCS");
    }

    static final Map<Integer, String> SCREEN_SAVE = new HashMap<Integer, String>();
    static final Map<Integer, String> SCENE_MODE = new HashMap<Integer, String>();

    static {
        SCREEN_SAVE.put(1, "5 分钟");
        SCREEN_SAVE.put(2, "10 分钟");
        SCREEN_SAVE.put(3, "30 分钟");
        SCREEN_SAVE.put(4, "常亮");
        SCREEN_SAVE.put(5, "1 分钟");
        SCENE_MODE.put(1, "AI");
        SCENE_MODE.put(2, "数码生态");
        SCENE_MODE.put(3, "单口");
        SCENE_MODE.put(4, "均衡");
    }

    /** 2.21 协议开关位定义（2026-09-22 真机逐位验证过）。 */
    static final Object[][] PROTO_SWATCHES = {
            {"c1", "pd", "PD", 0}, {"c1", "pps", "PPS", 1}, {"c1", "ufcs", "UFCS", 2},
            {"c2", "pd", "PD", 8}, {"c2", "pps", "PPS", 9}, {"c2", "ufcs", "UFCS", 10},
            {"c3", "ufcs", "UFCS", 16}, {"c3", "scp", "SCP", 17},
            {"a", "ufcs", "UFCS", 24}, {"a", "scp", "SCP", 25},
    };

    /** 可写属性表：{piid, 名称, 类型, min, max, 是否高级}。 */
    static final Object[][] WRITABLE = {
            {0x05, "场景模式", "u8", 1, 4, false},
            {0x06, "息屏时间", "u8", 1, 5, false},
            {0x0d, "设备语言", "u8", 0, 1, false},
            {0x0f, "USB-A 小电流", "u8", 0, 1, false},
            {0x10, "端口开关", "u8", 0, 15, false},
            {0x13, "空闲熄屏", "u8", 0, 1, false},
            {0x14, "屏幕方向锁", "u8", 0, 1, false},
            {0x15, "协议开关掩码", "u32", 0, 0xFFFFFFFFL, false},
            {0x07, "协议控制字 2.7", "u8", 0, 255, true},
            {0x08, "端口关闭设置 2.8", "u8", 0, 255, true},
    };

    // ------------------------------------------------------------------ 编解码

    static byte[] encodeGetProperties(int seq, int[][] tuples) {
        byte[] body = Util.cat(new byte[]{0x33, 0x20}, Util.le(seq, 2),
                new byte[]{0x02, (byte) tuples.length});
        for (int[] t : tuples) body = Util.cat(body, new byte[]{(byte) t[0]}, Util.le(t[1], 2));
        return body;
    }

    static final class Item {
        int siid, piid, status, typeByte, marker;
        long value;
        String raw;
        String type;
    }

    static List<Item> parseResponse(byte[] pt) {
        if (pt.length < 6 || pt[1] != 0x20
                || (pt[0] != (byte) 0x93 && pt[0] != 0x1c && pt[0] != 0x0e)) {
            throw new IllegalStateException("应答头不对: " + Util.hex(java.util.Arrays.copyOfRange(pt, 0, Math.min(6, pt.length))));
        }
        return entries(pt, pt[5] & 0xFF, true);
    }

    private static List<Item> entries(byte[] pt, int count, boolean withStatus) {
        List<Item> out = new ArrayList<Item>();
        int i = 6;
        for (int n = 0; n < count; n++) {
            int prefix = withStatus ? 7 : 5;
            if (i + prefix > pt.length) throw new IllegalStateException("属性项被截断");
            Item it = new Item();
            it.siid = pt[i] & 0xFF;
            it.piid = Util.u16le(pt, i + 1);
            int status = 0, typeByte, marker, valueOffset;
            if (withStatus) {
                status = Util.u16le(pt, i + 3);
                typeByte = pt[i + 5] & 0xFF;
                marker = pt[i + 6] & 0xFF;
                valueOffset = i + 7;
            } else {
                typeByte = pt[i + 3] & 0xFF;
                marker = pt[i + 4] & 0xFF;
                valueOffset = i + 5;
            }
            it.status = status;
            it.typeByte = typeByte;
            it.marker = marker;
            int size;
            if (typeByte == 0x01) { size = 1; it.type = (marker == 0x00) ? "bool" : "u8"; }
            else if (typeByte == 0x02) { size = 2; it.type = "u16"; }
            else if (typeByte == 0x04) { size = 4; it.type = "u32"; }
            else throw new IllegalStateException("未知类型 0x" + Integer.toHexString(typeByte));
            if (valueOffset + size > pt.length) throw new IllegalStateException("属性值被截断");
            byte[] raw = java.util.Arrays.copyOfRange(pt, valueOffset, valueOffset + size);
            it.raw = Util.hex(raw);
            long v = 0;
            for (int k = size - 1; k >= 0; k--) v = (v << 8) | (raw[k] & 0xFFL);
            it.value = v;
            out.add(it);
            i = valueOffset + size;
        }
        return out;
    }

    /** SET 请求：0c 20 <seq> 00 01 <siid> <piid_le2> <type> <marker> <value>。 */
    static byte[] encodeSetProperty(int seq, int siid, int piid, long value, boolean u32, boolean u16) {
        byte[] body = Util.cat(new byte[]{0x0c, 0x20}, Util.le(seq, 2), new byte[]{0x00, 0x01},
                new byte[]{(byte) siid}, Util.le(piid, 2));
        if (u32) body = Util.cat(body, new byte[]{0x04, 0x50}, Util.le((int) (value & 0xFFFFFFFFL), 4));
        else if (u16) body = Util.cat(body, new byte[]{0x02, 0x10}, Util.le((int) (value & 0xFFFFL), 2));
        else body = Util.cat(body, new byte[]{0x01, 0x10, (byte) (value & 0xFF)});
        return body;
    }

    /** MIOT action 调用（形如 24 20 <seq> <siid> <aiid_le2>）。 */
    static byte[] encodeAction(int seq, int siid, int aiid) {
        return Util.cat(new byte[]{0x24, 0x20}, Util.le(seq, 2),
                new byte[]{(byte) siid}, Util.le(aiid, 2));
    }

    // ------------------------------------------------------------------ 解码

    static JSONObject portInfo(String port, long value) {
        int b0 = (int) (value & 0xFF);
        int b1 = (int) ((value >> 8) & 0xFF);
        int b2 = (int) ((value >> 16) & 0xFF);
        int b3 = (int) ((value >> 24) & 0xFF);
        double voltage = b3 / 10.0;
        double current = b2 / 10.0;
        boolean inUse = b0 != 0;
        String proto = "idle";
        if (inUse) {
            proto = PROTOCOL_NAMES.get(b1);
            if (proto == null) proto = "unknown_0x" + Integer.toHexString(b1);
        }
        JSONObject o = new JSONObject();
        try {
            o.put("id", port);
            int idx = java.util.Arrays.asList(PORT_ORDER).indexOf(port);
            o.put("label", idx >= 0 ? PORT_LABEL[idx] : port);
            o.put("in_use", inUse);
            o.put("proto", proto);
            o.put("v", Util.jsRound(voltage, 2));
            o.put("a", Util.jsRound(current, 2));
            o.put("w", Util.jsRound(voltage * current, 2));
            o.put("raw", Util.hex(Util.le((int) (value & 0xFFFFFFFFL), 4)));
        } catch (Exception ignored) { }
        return o;
    }

    /** 2.17/2.18：低 16 位给后一个口、高 16 位给前一个口；每半字 = [协议号][协商功率W]。 */
    static void applyCaps(JSONObject port, long capValue, int piid) {
        for (int[] pair : CAP_PIID) {
            if (pair[0] != piid) continue;
            String high = PORT_ORDER[pair[1]];
            String low = PORT_ORDER[pair[2]];
            String id = port.optString("id");
            if (!id.equals(high) && !id.equals(low)) continue;
            long half = id.equals(high) ? ((capValue >> 16) & 0xFFFF) : (capValue & 0xFFFF);
            long capByte = half & 0xFF;
            try {
                if (capByte != 0) {
                    port.put("cap", (int) capByte);
                    long kind = (half >> 8) & 0xFF;
                    if (kind == 0x07) port.put("kind", "pd_fixed");
                    else if (kind == 0x08) port.put("kind", "pd_pps");
                    long protoNum = ((half >> 8) & 0xFF);
                    if (protoNum != 0) {
                        port.put("proto_num", (int) protoNum);
                        String text = MIJIA_PROTOCOLS.get((int) protoNum);
                        port.put("proto_text", text == null ? JSONObject.NULL : text);
                    }
                }
            } catch (Exception ignored) { }
        }
    }

    static JSONObject decodeProtocolSwitches(long value) {
        JSONObject out = new JSONObject();
        try {
            for (Object[] row : PROTO_SWATCHES) {
                String port = (String) row[0];
                String sw = (String) row[1];
                int bit = (Integer) row[3];
                JSONObject p = out.optJSONObject(port);
                if (p == null) { p = new JSONObject(); out.put(port, p); }
                p.put(sw, ((value >>> bit) & 1) != 0);
            }
        } catch (Exception ignored) { }
        return out;
    }

    static JSONObject decodeSettings(Map<Integer, Item> byPiid) {
        JSONObject s = new JSONObject();
        try {
            Item scene = byPiid.get(0x05);
            Item sst = byPiid.get(0x06);
            Item portCtl = byPiid.get(0x10);
            putOpt(s, "scene_mode", scene);
            putText(s, "scene_mode_text", scene, SCENE_MODE);
            putOpt(s, "screen_save_time", sst);
            putText(s, "screen_save_text", sst, SCREEN_SAVE);
            putOpt(s, "protocol_ctl", byPiid.get(0x07));
            putOpt(s, "device_language", byPiid.get(0x0d));
            putBool(s, "usb_a_always_on", byPiid.get(0x0f));
            putOpt(s, "port_ctl", portCtl);
            if (portCtl != null) {
                JSONObject en = new JSONObject();
                for (int i = 0; i < PORT_ORDER.length; i++) {
                    en.put(PORT_ORDER[i], ((portCtl.value >>> i) & 1) != 0);
                }
                s.put("ports_enabled", en);
            } else {
                s.put("ports_enabled", JSONObject.NULL);
            }
            putBool(s, "screenoff_while_idle", byPiid.get(0x13));
            putBool(s, "screen_dir_lock", byPiid.get(0x14));
            Item ext = byPiid.get(0x15);
            if (ext != null) {
                s.put("protocol_ctl_extend", ext.value);
                s.put("protocol_switches", decodeProtocolSwitches(ext.value));
            } else {
                s.put("protocol_ctl_extend", JSONObject.NULL);
                s.put("protocol_switches", JSONObject.NULL);
            }
        } catch (Exception ignored) { }
        return s;
    }

    private static void putOpt(JSONObject o, String key, Item it) throws Exception {
        if (it == null) o.put(key, JSONObject.NULL);
        else o.put(key, it.value);
    }

    private static void putBool(JSONObject o, String key, Item it) throws Exception {
        if (it == null) o.put(key, JSONObject.NULL);
        else o.put(key, it.value != 0);
    }

    private static void putText(JSONObject o, String key, Item it, Map<Integer, String> table) throws Exception {
        if (it == null) { o.put(key, JSONObject.NULL); return; }
        String t = table.get((int) it.value);
        o.put(key, t == null ? JSONObject.NULL : t);
    }

    // ------------------------------------------------------------------ 一帧状态

    /** 一次标准 15 项读取 → 一帧 state（字段与 collector.py 的 collect_once 一致）。 */
    static JSONObject buildState(List<Item> items, Integer rssi, int rxDup, int rxLag) {
        Map<Integer, Item> byPiid = new HashMap<Integer, Item>();
        for (Item it : items) if (it.siid == 2) byPiid.put(it.piid, it);

        JSONArray ports = new JSONArray();
        double total = 0;
        for (int i = 0; i < PORT_ORDER.length; i++) {
            Item it = byPiid.get(PORT_PIID[i]);
            if (it == null) continue;
            JSONObject p = portInfo(PORT_ORDER[i], it.value);
            for (int[] pair : CAP_PIID) {
                Item src = byPiid.get(pair[0]);
                if (src != null) applyCaps(p, src.value, pair[0]);
            }
            total += p.optDouble("w", 0);
            ports.put(p);
        }

        JSONArray raw = new JSONArray();
        for (Item it : items) {
            JSONObject r = new JSONObject();
            try {
                r.put("piid", it.piid);
                r.put("type", it.type);
                r.put("value", it.value);
                r.put("status", it.status);
                r.put("raw", it.raw);
            } catch (Exception ignored) { }
            raw.put(r);
        }

        JSONObject st = new JSONObject();
        try {
            st.put("t", "state");
            st.put("at", System.currentTimeMillis());
            st.put("ok", true);
            if (rssi != null) st.put("rssi", rssi.intValue());
            st.put("rxDup", rxDup);
            st.put("rxLag", rxLag);
            st.put("total", Util.jsRound(total, 2));
            st.put("ports", ports);
            st.put("settings", decodeSettings(byPiid));
            st.put("raw", raw);
        } catch (Exception ignored) { }
        return st;
    }

    /** 把 15 项里的某一个换成另一个（唯一能问出新属性的请求形状，数量仍是 15）。 */
    static int[][] alt15(int swapOut, int swapIn) {
        List<int[]> out = new ArrayList<int[]>();
        for (int[] t : DEFAULT_QUERY) if (t[1] != swapOut) out.add(t);
        out.add(new int[]{2, swapIn});
        return out.toArray(new int[0][]);
    }

    static JSONArray writableSchema() {
        JSONArray arr = new JSONArray();
        try {
            for (Object[] row : WRITABLE) {
                JSONObject o = new JSONObject();
                o.put("piid", row[0]);
                o.put("name", row[1]);
                o.put("type", row[2]);
                o.put("min", row[3]);
                o.put("max", row[4]);
                o.put("advanced", row[5]);
                arr.put(o);
            }
        } catch (Exception ignored) { }
        return arr;
    }

    static JSONArray switchesSchema() {
        JSONArray arr = new JSONArray();
        try {
            for (Object[] row : PROTO_SWATCHES) {
                JSONObject o = new JSONObject();
                o.put("port", row[0]);
                o.put("sw", row[1]);
                o.put("label", row[2]);
                o.put("bit", row[3]);
                arr.put(o);
            }
        } catch (Exception ignored) { }
        return arr;
    }
}
