package com.histion.cuktech;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/** 小工具：字节/十六进制、时间、JS 风格四舍五入。 */
final class Util {
    private Util() {}

    static final char[] HEX = "0123456789abcdef".toCharArray();

    static String hex(byte[] b) {
        if (b == null) return "";
        StringBuilder sb = new StringBuilder(b.length * 2);
        for (byte x : b) { sb.append(HEX[(x >> 4) & 0xF]); sb.append(HEX[x & 0xF]); }
        return sb.toString();
    }

    static byte[] unhex(String s) {
        if (s == null) return new byte[0];
        s = s.trim();
        int n = s.length() / 2;
        byte[] out = new byte[n];
        for (int i = 0; i < n; i++) {
            out[i] = (byte) ((Character.digit(s.charAt(i * 2), 16) << 4)
                    | Character.digit(s.charAt(i * 2 + 1), 16));
        }
        return out;
    }

    static byte[] cat(byte[]... parts) {
        int n = 0;
        for (byte[] p : parts) n += p.length;
        byte[] out = new byte[n];
        int i = 0;
        for (byte[] p : parts) { System.arraycopy(p, 0, out, i, p.length); i += p.length; }
        return out;
    }

    static byte[] le(int value, int width) {
        byte[] out = new byte[width];
        for (int i = 0; i < width; i++) out[i] = (byte) ((value >>> (8 * i)) & 0xFF);
        return out;
    }

    static byte[] be(int value, int width) {
        byte[] out = new byte[width];
        for (int i = 0; i < width; i++) out[width - 1 - i] = (byte) ((value >>> (8 * i)) & 0xFF);
        return out;
    }

    static int u16le(byte[] b, int off) {
        return (b[off] & 0xFF) | ((b[off + 1] & 0xFF) << 8);
    }

    static long u32le(byte[] b, int off) {
        return (b[off] & 0xFFL) | ((b[off + 1] & 0xFFL) << 8) | ((b[off + 2] & 0xFFL) << 16)
                | ((b[off + 3] & 0xFFL) << 24);
    }

    static boolean eq(byte[] a, byte[] b) {
        if (a == null || b == null || a.length != b.length) return false;
        for (int i = 0; i < a.length; i++) if (a[i] != b[i]) return false;
        return true;
    }

    /** JS ``Math.round(v * 10^d) / 10^d``（半值向上，不是银行家舍入）。 */
    static double jsRound(double v, int digits) {
        if (Double.isNaN(v) || Double.isInfinite(v)) return 0.0;
        double scale = Math.pow(10, digits);
        return Math.floor(v * scale + 0.5) / scale;
    }

    static double round3(double v) { return jsRound(v, 3); }

    static String dayKey(long tsMs) {
        return new SimpleDateFormat("yyyy-MM-dd", Locale.CHINA).format(new Date(tsMs));
    }

    static String hms() {
        return new SimpleDateFormat("HH:mm:ss", Locale.CHINA).format(new Date());
    }

    static String isoNow() {
        return new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.CHINA).format(new Date());
    }

    /** 把字节数组切成不大于 chunk 的片（与 Python ``_chunk_parcel`` 一致）。 */
    static List<byte[]> chunk(byte[] data, int size) {
        List<byte[]> out = new ArrayList<byte[]>();
        if (data.length == 0) { out.add(new byte[0]); return out; }
        for (int i = 0; i < data.length; i += size) {
            int n = Math.min(size, data.length - i);
            byte[] part = new byte[n];
            System.arraycopy(data, i, part, 0, n);
            out.add(part);
        }
        return out;
    }

    static String str(Object o) { return o == null ? "" : String.valueOf(o); }

    /** 安全取 long（JSONObject 里的数字可能是 int/Integer/Long/Double）。 */
    static long asLong(Object o, long dflt) {
        if (o instanceof Number) return ((Number) o).longValue();
        if (o instanceof String) {
            try { return Long.parseLong(((String) o).trim()); } catch (Exception ignored) {}
        }
        return dflt;
    }

    static double asDouble(Object o, double dflt) {
        if (o instanceof Number) return ((Number) o).doubleValue();
        if (o instanceof String) {
            try { return Double.parseDouble(((String) o).trim()); } catch (Exception ignored) {}
        }
        return dflt;
    }

    static int asInt(Object o, int dflt) {
        if (o instanceof Number) return ((Number) o).intValue();
        if (o instanceof String) {
            try { return Integer.parseInt(((String) o).trim()); } catch (Exception ignored) {}
        }
        return dflt;
    }

    static String pad2(int v) {
        String s = Integer.toString(v);
        return s.length() >= 2 ? s : "0" + s;
    }

    static String hexByte(int v) {
        String s = Integer.toHexString(v & 0xFF);
        return s.length() >= 2 ? s : "0" + s;
    }
}
