package com.histion.cuktech;

import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * 小米云扫码登录 + 设备列表（对齐 vendor/cuktech_ble/xiaomi_cloud.py）。
 *
 * 只用二维码登录这条路：拿二维码 → 长轮询等授权 → 用 serviceToken 换取设备列表里的
 * 12 字节 BLE 密钥。请求签名是 RC4 + SHA1/SHA256，全部用标准库实现，不引第三方依赖。
 */
final class XiaomiCloud {
    private XiaomiCloud() {}

    static final String[] REGIONS = {"cn", "sg", "i2", "de", "us", "tw", "ru", "in"};
    private static final String[] HINTS = {"njcuk.fitting.ad1204", "njcuk.fitting.1204e"};

    static class CloudError extends Exception {
        CloudError(String m) { super(m); }
    }

    static class Pending extends Exception {
        Pending() { super("pending"); }
    }

    static final class QRLogin {
        String qrImageUrl, loginUrl, lpUrl, deviceId;
        int timeout = 300;
        Map<String, String> cookies = new LinkedHashMap<String, String>();
    }

    static final class CloudAuth {
        String userId, cUserId, ssecurity, passToken, serviceToken, deviceId;
    }

    // ------------------------------------------------------------------ 小工具

    private static String freshUserAgent() {
        Random r = new Random();
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 13; i++) sb.append((char) ('A' + r.nextInt(26)));
        return "Android-7.1.1-1.0.0-ONEPLUS A3010-136-" + sb + " APP/xiaomi.smarthome APPV/62830";
    }

    /** 随机字母串（大小写混合，与 Python string.ascii_letters 一致）。 */
    private static String randomLetters(int n) {
        Random r = new Random();
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < n; i++) {
            int k = r.nextInt(52);
            sb.append((char) (k < 26 ? 'a' + k : 'A' + (k - 26)));
        }
        return sb.toString();
    }

    /** RC4 密钥流（前 1024 字节丢弃，与小米云实现一致）。 */
    private static byte[] rc4(byte[] key, byte[] data) {
        int[] s = new int[256];
        for (int i = 0; i < 256; i++) s[i] = i;
        int j = 0;
        for (int i = 0; i < 256; i++) {
            j = (j + s[i] + (key[i % key.length] & 0xFF)) & 0xFF;
            int t = s[i]; s[i] = s[j]; s[j] = t;
        }
        int i = 0, k = 0;
        for (int n = 0; n < 1024; n++) {
            i = (i + 1) & 0xFF;
            k = (k + s[i]) & 0xFF;
            int t = s[i]; s[i] = s[k]; s[k] = t;
        }
        byte[] out = new byte[data.length];
        for (int n = 0; n < data.length; n++) {
            i = (i + 1) & 0xFF;
            k = (k + s[i]) & 0xFF;
            int t = s[i]; s[i] = s[k]; s[k] = t;
            out[n] = (byte) (data[n] ^ s[(s[i] + s[k]) & 0xFF]);
        }
        return out;
    }

    private static String rc4B64(String keyB64, String payload) {
        byte[] key = Base64.decode(keyB64, Base64.DEFAULT);
        return Base64.encodeToString(rc4(key, payload.getBytes()), Base64.NO_WRAP | Base64.DEFAULT).trim();
    }

    private static String b64(byte[] b) {
        return Base64.encodeToString(b, Base64.NO_WRAP);
    }

    private static byte[] sha1(byte[] data) {
        try {
            return MessageDigest.getInstance("SHA-1").digest(data);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static byte[] sha256(byte[] data) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(data);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    private static String signedNonce(String ssecurity, String nonce) {
        byte[] a = Base64.decode(ssecurity, Base64.DEFAULT);
        byte[] b = Base64.decode(nonce, Base64.DEFAULT);
        byte[] both = new byte[a.length + b.length];
        System.arraycopy(a, 0, both, 0, a.length);
        System.arraycopy(b, 0, both, a.length, b.length);
        return b64(sha256(both));
    }

    private static String encSignature(String url, String method, String signedNonce,
                                       LinkedHashMap<String, String> params) {
        String path = url.split("com")[1].replace("/app/", "/");
        StringBuilder sb = new StringBuilder();
        sb.append(method.toUpperCase()).append('&').append(path);
        for (Map.Entry<String, String> e : params.entrySet()) {
            sb.append('&').append(e.getKey()).append('=').append(e.getValue());
        }
        sb.append('&').append(signedNonce);
        return b64(sha1(sb.toString().getBytes()));
    }

    private static String stripJsonp(String text) throws Exception {
        String t = text.replace("&&&START&&&", "").trim();
        return t;
    }

    // ------------------------------------------------------------------ HTTP

    private static final class Resp {
        int status;
        String body;
        /** 原始字节（设备列表的应答是 RC4 后的 base64，用字节做 base64 解码最稳）。 */
        byte[] raw = new byte[0];
        Map<String, String> cookies = new LinkedHashMap<String, String>();
        /** cookie → 设置它的主机（serviceToken 要挑 api.io.mi.com 那一枚）。 */
        Map<String, String> cookieHosts = new LinkedHashMap<String, String>();
        String finalUrl;
    }

    private static Resp http(String url, String method, Map<String, String> cookies,
                             Map<String, String> headers, int timeoutMs, boolean follow) throws Exception {
        Resp r = new Resp();
        String current = url;
        for (int hop = 0; hop < 8; hop++) {
            HttpURLConnection conn = (HttpURLConnection) new URL(current).openConnection();
            conn.setConnectTimeout(timeoutMs);
            conn.setReadTimeout(timeoutMs);
            conn.setRequestMethod(method);
            conn.setInstanceFollowRedirects(false);
            conn.setRequestProperty("User-Agent", freshUserAgent());
            if (headers != null) {
                for (Map.Entry<String, String> e : headers.entrySet()) conn.setRequestProperty(e.getKey(), e.getValue());
            }
            if (cookies != null && !cookies.isEmpty()) {
                StringBuilder sb = new StringBuilder();
                for (Map.Entry<String, String> e : cookies.entrySet()) {
                    if (sb.length() > 0) sb.append("; ");
                    sb.append(e.getKey()).append('=').append(e.getValue());
                }
                conn.setRequestProperty("Cookie", sb.toString());
            }
            conn.connect();
            r.status = conn.getResponseCode();
            String host = "";
            try { host = new URL(current).getHost(); } catch (Exception ignored) { }
            for (Map.Entry<String, List<String>> e : conn.getHeaderFields().entrySet()) {
                String key = e.getKey();
                if (key == null || !"Set-Cookie".equalsIgnoreCase(key)) continue;
                for (String c : e.getValue()) {
                    String kv = c.split(";")[0];
                    int eq = kv.indexOf('=');
                    if (eq > 0) {
                        String name = kv.substring(0, eq).trim();
                        r.cookies.put(name, kv.substring(eq + 1).trim());
                        r.cookieHosts.put(name, host);
                    }
                }
            }
            InputStream is = r.status >= 400 ? conn.getErrorStream() : conn.getInputStream();
            if (is != null) {
                ByteArrayOutputStream bos = new ByteArrayOutputStream();
                byte[] buf = new byte[8192];
                int n;
                while ((n = is.read(buf)) > 0) bos.write(buf, 0, n);
                is.close();
                r.raw = bos.toByteArray();
                r.body = new String(r.raw, "UTF-8");
            }
            String loc = conn.getHeaderField("Location");
            conn.disconnect();
            r.finalUrl = current;
            if (follow && loc != null && (r.status == 301 || r.status == 302 || r.status == 303 || r.status == 307)) {
                current = new URL(new URL(current), loc).toString();
                continue;
            }
            return r;
        }
        return r;
    }

    /** 抓二维码图片字节（带登录态 cookie）。 */
    static byte[] fetch(String url, Map<String, String> cookies, int timeoutMs) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setConnectTimeout(timeoutMs);
        conn.setReadTimeout(timeoutMs);
        conn.setRequestMethod("GET");
        conn.setRequestProperty("User-Agent", freshUserAgent());
        if (cookies != null && !cookies.isEmpty()) {
            StringBuilder sb = new StringBuilder();
            for (Map.Entry<String, String> e : cookies.entrySet()) {
                if (sb.length() > 0) sb.append("; ");
                sb.append(e.getKey()).append('=').append(e.getValue());
            }
            conn.setRequestProperty("Cookie", sb.toString());
        }
        conn.connect();
        InputStream is = conn.getInputStream();
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = is.read(buf)) > 0) bos.write(buf, 0, n);
        is.close();
        conn.disconnect();
        return bos.toByteArray();
    }

    // ------------------------------------------------------------------ 二维码

    static QRLogin startQrLogin() throws CloudError {
        try {
            String deviceId = randomLetters(16);
            LinkedHashMap<String, String> cookies = new LinkedHashMap<String, String>();
            cookies.put("sdkVersion", "accountsdk-18.8.15");
            cookies.put("deviceId", deviceId);

            String url = "https://account.xiaomi.com/longPolling/loginUrl"
                    + "?_qrsize=480"
                    + "&qs=" + URLEncoder.encode("%3Fsid%3Dxiaomiio%26_json%3Dtrue", "UTF-8")
                    + "&bizDeviceType="
                    + "&callback=" + URLEncoder.encode("https://sts.api.io.mi.com/sts", "UTF-8")
                    + "&_hasLogo=false&theme=&needTheme=false&showActiveX=false"
                    + "&serviceParam=" + URLEncoder.encode("{\"checkSafePhone\":false,\"checkSafeAddress\":false,\"lsrp_score\":0.0}", "UTF-8")
                    + "&sid=xiaomiio&_locale=en_GB&_dc=" + System.currentTimeMillis();
            Resp r = http(url, "GET", cookies, null, 30000, true);
            if (r.status != 200) throw new CloudError("二维码申请失败：HTTP " + r.status);
            JSONObject data = new JSONObject(stripJsonp(r.body == null ? "" : r.body));
            QRLogin qr = new QRLogin();
            qr.qrImageUrl = data.getString("qr");
            qr.loginUrl = data.optString("loginUrl", "");
            qr.lpUrl = data.getString("lp");
            qr.timeout = data.optInt("timeout", 300);
            qr.deviceId = deviceId;
            qr.cookies.put("sdkVersion", "accountsdk-18.8.15");
            qr.cookies.put("deviceId", deviceId);
            return qr;
        } catch (CloudError e) {
            throw e;
        } catch (Exception e) {
            throw new CloudError("二维码申请失败：" + e.getMessage());
        }
    }

    /** 轮询一次长连接；未扫码抛 Pending，终态错误抛 CloudError。 */
    static CloudAuth pollQrLogin(QRLogin qr) throws CloudError, Pending {
        Resp r;
        try {
            r = http(qr.lpUrl, "GET", qr.cookies, null, 45000, true);
            if (r.status != 200) throw new CloudError("扫码轮询失败：HTTP " + r.status);
        } catch (CloudError e) {
            throw e;
        } catch (Exception e) {
            // 长轮询被服务端挂断 = 还没扫码，ticket 仍然有效
            throw new Pending();
        }
        JSONObject data;
        try {
            data = new JSONObject(stripJsonp(r.body == null ? "" : r.body));
        } catch (Exception e) {
            throw new CloudError("扫码轮询返回不是 JSON");
        }
        int code = data.optInt("code", 0);
        if (code == 401 || code == 700 || code == 70016) throw new Pending();
        if (data.has("code") && code != 0) throw new CloudError("扫码登录失败：code=" + code + " desc=" + data.optString("desc"));
        if (!data.has("ssecurity")) throw new Pending();
        String location = data.optString("location", "");
        if (location.isEmpty()) throw new CloudError("扫码登录缺少 location");

        try {
            Resp r2 = http(location, "GET", qr.cookies, null, 30000, true);
            if (r2.status >= 400) throw new CloudError("登录跳转失败：HTTP " + r2.status);
            // 跳转链上可能有多枚 serviceToken，取 api.io.mi.com 那一枚（对齐 aiohttp
            // 的 cookie_jar.filter_cookies(final_url)）。
            String token = null;
            for (Map.Entry<String, String> e : r2.cookieHosts.entrySet()) {
                if ("serviceToken".equals(e.getKey())
                        && e.getValue() != null && e.getValue().endsWith("api.io.mi.com")) {
                    token = r2.cookies.get("serviceToken");
                    break;
                }
            }
            if (token == null || token.isEmpty()) token = r2.cookies.get("serviceToken");
            if (token == null || token.isEmpty()) throw new CloudError("登录后没有拿到 serviceToken");
            CloudAuth auth = new CloudAuth();
            auth.userId = data.optString("userId", "");
            auth.cUserId = data.optString("cUserId", "");
            auth.ssecurity = data.getString("ssecurity");
            auth.passToken = data.optString("passToken", "");
            auth.serviceToken = token;
            auth.deviceId = qr.deviceId;
            return auth;
        } catch (CloudError e) {
            throw e;
        } catch (Exception e) {
            throw new CloudError("登录跳转失败：" + e.getMessage());
        }
    }

    // ------------------------------------------------------------------ 设备列表

    private static String apiBase(String region) {
        return "https://" + ("cn".equals(region) ? "" : region + ".") + "api.io.mi.com/app";
    }

    static List<JSONObject> listDevices(CloudAuth auth, String region) throws CloudError {
        try {
            String url = apiBase(region) + "/home/device_list";
            String data = "{\"getVirtualModel\":true,\"getHuamiDevices\":1}";
            long millis = System.currentTimeMillis();
            byte[] nonceBytes = new byte[12];
            byte[] rnd = Crypto.random(8);
            System.arraycopy(rnd, 0, nonceBytes, 0, 8);
            int ticks = (int) (millis / 60000);
            nonceBytes[8] = (byte) ((ticks >>> 24) & 0xFF);
            nonceBytes[9] = (byte) ((ticks >>> 16) & 0xFF);
            nonceBytes[10] = (byte) ((ticks >>> 8) & 0xFF);
            nonceBytes[11] = (byte) (ticks & 0xFF);
            String nonce = b64(nonceBytes);
            String sn = signedNonce(auth.ssecurity, nonce);

            LinkedHashMap<String, String> params = new LinkedHashMap<String, String>();
            params.put("data", data);
            params.put("rc4_hash__", encSignature(url, "POST", sn, params));
            LinkedHashMap<String, String> enc = new LinkedHashMap<String, String>();
            for (Map.Entry<String, String> e : params.entrySet()) {
                enc.put(e.getKey(), rc4B64(sn, e.getValue()));
            }
            enc.put("signature", encSignature(url, "POST", sn, enc));
            enc.put("ssecurity", auth.ssecurity);
            enc.put("_nonce", nonce);

            StringBuilder qs = new StringBuilder();
            for (Map.Entry<String, String> e : enc.entrySet()) {
                if (qs.length() > 0) qs.append('&');
                qs.append(URLEncoder.encode(e.getKey(), "UTF-8")).append('=')
                        .append(URLEncoder.encode(e.getValue(), "UTF-8"));
            }
            LinkedHashMap<String, String> headers = new LinkedHashMap<String, String>();
            headers.put("Accept-Encoding", "identity");
            headers.put("Content-Type", "application/x-www-form-urlencoded");
            headers.put("x-xiaomi-protocal-flag-cli", "PROTOCAL-HTTP2");
            headers.put("MIOT-ENCRYPT-ALGORITHM", "ENCRYPT-RC4");
            LinkedHashMap<String, String> cookies = new LinkedHashMap<String, String>();
            cookies.put("userId", auth.userId);
            cookies.put("serviceToken", auth.serviceToken);
            cookies.put("yetAnotherServiceToken", auth.serviceToken);
            cookies.put("locale", "en_GB");
            cookies.put("timezone", "GMT+00:00");
            cookies.put("is_daylight", "0");
            cookies.put("dst_offset", "0");
            cookies.put("channel", "MI_APP_STORE");

            Resp r = http(url + "?" + qs, "POST", cookies, headers, 30000, true);
            if (r.status != 200) {
                throw new CloudError("HTTP " + r.status + " "
                        + (r.body == null ? "" : r.body.trim().substring(0, Math.min(120, r.body.trim().length()))));
            }
            // 应答体是「RC4(signedNonce, 明文) 之后再做 base64」——两步都要解，
            // 只解 key 不解 body 会得到乱码（上一版就是这么错的）。
            byte[] cipher;
            try {
                cipher = Base64.decode(new String(r.raw, "ISO-8859-1").trim(), Base64.DEFAULT);
            } catch (Exception e) {
                cipher = Base64.decode((r.body == null ? "" : r.body).trim(), Base64.DEFAULT);
            }
            byte[] plain = rc4(Base64.decode(sn, Base64.DEFAULT), cipher);
            JSONObject resp = new JSONObject(new String(plain, "UTF-8"));
            JSONObject result = resp.optJSONObject("result");
            if (result == null) {
                String head = new String(plain, "UTF-8");
                throw new CloudError("返回结构异常：" + head.substring(0, Math.min(120, head.length())));
            }
            JSONArray list = result.optJSONArray("list");
            List<JSONObject> out = new ArrayList<JSONObject>();
            if (list != null) {
                for (int i = 0; i < list.length(); i++) {
                    JSONObject o = list.optJSONObject(i);
                    if (o != null) out.add(o);
                }
            }
            return out;
        } catch (CloudError e) {
            throw e;
        } catch (Exception e) {
            throw new CloudError("设备列表失败：" + e.getMessage());
        }
    }

    static String normalizeMac(String mac) {
        return (mac == null ? "" : mac).replace(":", "").replace("-", "").toLowerCase();
    }

    static boolean looksLikeAd1204(JSONObject dev) {
        if (dev == null) return false;
        String model = dev.optString("model", "").toLowerCase();
        String name = dev.optString("name", "").toLowerCase();
        for (String h : HINTS) if (model.contains(h)) return true;
        return model.contains("ad1204") || name.contains("ad1204");
    }

    /** 设备列表里的 token 可能是 12 或 16 字节，BLE 只用前 12 字节（24 个 hex 字符）。 */
    static String validToken(String raw) {
        if (raw == null) return null;
        String t = raw.trim().toLowerCase();
        if (t.length() != 24 && t.length() != 32) return null;
        for (int i = 0; i < t.length(); i++) {
            if (Character.digit(t.charAt(i), 16) < 0) return null;
        }
        return t.substring(0, 24);
    }

    static String findTokenByMac(List<JSONObject> devices, String mac) throws CloudError {
        String target = normalizeMac(mac);
        String invalid = null;
        for (JSONObject dev : devices) {
            if (!target.equals(normalizeMac(dev.optString("mac", "")))) continue;
            String token = validToken(dev.optString("token", ""));
            if (token == null) { invalid = "匹配到的设备 BLE 密钥长度/格式不对"; continue; }
            return token;
        }
        if (invalid != null) throw new CloudError(invalid);
        return null;
    }
}
