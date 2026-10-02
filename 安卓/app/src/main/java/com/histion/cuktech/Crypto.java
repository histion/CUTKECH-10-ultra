package com.histion.cuktech;

import java.math.BigInteger;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.PrivateKey;
import java.security.SecureRandom;
import java.security.spec.ECGenParameterSpec;
import java.security.spec.ECPoint;
import java.security.spec.ECPublicKeySpec;
import java.util.Arrays;

import javax.crypto.Cipher;
import javax.crypto.KeyAgreement;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * 米家 BLE 标准认证用到的密码学原语（对齐 vendor/cuktech_ble/xiaomi/crypto.py）。
 *
 * Android 的 JCE 不保证提供 AES/CCM（Conscrypt 只给 GCM），所以 CCM 在这里按
 * RFC 3610 用 AES/ECB/NoPadding 手工实现：CBC-MAC(4 字节 tag) + CTR，nonce 12 字节
 * （L=3）、无 AAD 时 flags=0x0A、有 AAD 时 0x4A。已用 Python ``cryptography`` 的
 * AESCCM 生成向量逐条比对通过。
 */
final class Crypto {
    private Crypto() {}

    static final byte[] SETUP_INFO = "mible-setup-info".getBytes();
    static final byte[] LOGIN_INFO = "mible-login-info".getBytes();

    private static final SecureRandom RNG = new SecureRandom();

    static byte[] random(int n) {
        byte[] b = new byte[n];
        RNG.nextBytes(b);
        return b;
    }

    // ------------------------------------------------------------------ HMAC / HKDF

    static byte[] hmacSha256(byte[] key, byte[] data) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(key, "HmacSHA256"));
            return mac.doFinal(data);
        } catch (Exception e) {
            throw new IllegalStateException("hmac failed", e);
        }
    }

    /** HKDF-SHA256（extract + expand），salt 为 null 时按全零处理。 */
    static byte[] hkdf(byte[] ikm, byte[] salt, byte[] info, int length) {
        byte[] prk;
        if (salt == null || salt.length == 0) {
            prk = hmacSha256(new byte[32], ikm);
        } else {
            prk = hmacSha256(salt, ikm);
        }
        byte[] out = new byte[length];
        byte[] t = new byte[0];
        int off = 0;
        byte counter = 1;
        while (off < length) {
            byte[] input = Util.cat(t, info, new byte[]{counter});
            t = hmacSha256(prk, input);
            int n = Math.min(t.length, length - off);
            System.arraycopy(t, 0, out, off, n);
            off += n;
            counter++;
        }
        return out;
    }

    /** 注册密钥派生：token(12) + bind_key(16) + a_key(16)。 */
    static byte[][] deriveRegister(byte[] shared) {
        byte[] out = hkdf(shared, null, SETUP_INFO, 64);
        return new byte[][]{
                Arrays.copyOfRange(out, 0, 12),
                Arrays.copyOfRange(out, 12, 28),
                Arrays.copyOfRange(out, 28, 44),
        };
    }

    /** 登录会话密钥派生：dev_key(16) + app_key(16) + dev_iv(4) + app_iv(4)。 */
    static byte[][] deriveLogin(byte[] token, byte[] appRand, byte[] devRand) {
        byte[] salt = Util.cat(appRand, devRand);
        byte[] out = hkdf(token, salt, LOGIN_INFO, 64);
        return new byte[][]{
                Arrays.copyOfRange(out, 0, 16),
                Arrays.copyOfRange(out, 16, 32),
                Arrays.copyOfRange(out, 32, 36),
                Arrays.copyOfRange(out, 36, 40),
        };
    }

    // ------------------------------------------------------------------ ECDH P-256

    /** 生成 P-256 密钥对，返回 [0]=私钥 [1]=64 字节公钥(X||Y)。 */
    static Object[] generateKeypair() {
        try {
            KeyPairGenerator kpg = KeyPairGenerator.getInstance("EC");
            kpg.initialize(new ECGenParameterSpec("secp256r1"));
            KeyPair kp = kpg.generateKeyPair();
            byte[] pub = publicKeyToBytes(kp.getPublic());
            return new Object[]{kp.getPrivate(), pub};
        } catch (Exception e) {
            throw new IllegalStateException("keypair failed", e);
        }
    }

    static byte[] publicKeyToBytes(java.security.PublicKey key) {
        java.security.interfaces.ECPublicKey ec = (java.security.interfaces.ECPublicKey) key;
        byte[] x = fixed(ec.getW().getAffineX(), 32);
        byte[] y = fixed(ec.getW().getAffineY(), 32);
        return Util.cat(x, y);
    }

    static byte[] ecdhShared(PrivateKey priv, byte[] peerPub64) {
        try {
            java.security.interfaces.ECPrivateKey ec = (java.security.interfaces.ECPrivateKey) priv;
            BigInteger x = new BigInteger(1, Arrays.copyOfRange(peerPub64, 0, 32));
            BigInteger y = new BigInteger(1, Arrays.copyOfRange(peerPub64, 32, 64));
            ECPublicKeySpec spec = new ECPublicKeySpec(new ECPoint(x, y), ec.getParams());
            java.security.PublicKey peer = KeyFactory.getInstance("EC").generatePublic(spec);
            KeyAgreement ka = KeyAgreement.getInstance("ECDH");
            ka.init(priv);
            ka.doPhase(peer, true);
            return ka.generateSecret();
        } catch (Exception e) {
            throw new IllegalStateException("ecdh failed", e);
        }
    }

    private static byte[] fixed(BigInteger v, int size) {
        byte[] raw = v.toByteArray();
        if (raw.length == size) return raw;
        if (raw.length > size) return Arrays.copyOfRange(raw, raw.length - size, raw.length);
        byte[] out = new byte[size];
        System.arraycopy(raw, 0, out, size - raw.length, raw.length);
        return out;
    }

    /** 注册时加密 DID：固定 nonce 0x10..0x1b，AAD="devID"。 */
    static byte[] encryptDid(byte[] aKey, byte[] did) {
        byte[] nonce = new byte[12];
        for (int i = 0; i < 12; i++) nonce[i] = (byte) (16 + i);
        return ccmEncrypt(aKey, nonce, did, "devID".getBytes());
    }

    // ------------------------------------------------------------------ AES-CCM (tag=4)

    private static final int TAG_LEN = 4;

    static byte[] ccmEncrypt(byte[] key, byte[] nonce, byte[] pt, byte[] aad) {
        Cipher ecb = ecb(key, Cipher.ENCRYPT_MODE);
        boolean hasAad = aad != null && aad.length > 0;
        byte[] b0 = new byte[16];
        b0[0] = (byte) ((hasAad ? 0x40 : 0) | (((TAG_LEN - 2) / 2) << 3) | (15 - nonce.length - 1));
        System.arraycopy(nonce, 0, b0, 1, nonce.length);
        int qOff = 1 + nonce.length;
        long len = pt.length;
        for (int i = 0; i < 16 - qOff; i++) {
            b0[16 - 1 - i] = (byte) ((len >>> (8 * i)) & 0xFF);
        }
        byte[] x = block(ecb, b0);
        if (hasAad) {
            byte[] head = new byte[2];
            head[0] = (byte) ((aad.length >>> 8) & 0xFF);
            head[1] = (byte) (aad.length & 0xFF);
            byte[] ab = pad16(Util.cat(head, aad));
            for (int i = 0; i < ab.length; i += 16) {
                x = block(ecb, xor(x, Arrays.copyOfRange(ab, i, i + 16)));
            }
        }
        byte[] pb = pad16(pt);
        for (int i = 0; i < pb.length; i += 16) {
            x = block(ecb, xor(x, Arrays.copyOfRange(pb, i, i + 16)));
        }
        byte[] s0 = block(ecb, ctrBlock(nonce, 0));
        byte[] tag = Arrays.copyOfRange(xor(x, s0), 0, TAG_LEN);

        byte[] ct = new byte[pt.length];
        int idx = 1;
        int off = 0;
        while (off < pt.length) {
            byte[] s = block(ecb, ctrBlock(nonce, idx++));
            int n = Math.min(16, pt.length - off);
            for (int i = 0; i < n; i++) ct[off + i] = (byte) (pt[off + i] ^ s[i]);
            off += n;
        }
        return Util.cat(ct, tag);
    }

    static byte[] ccmDecrypt(byte[] key, byte[] nonce, byte[] frame, byte[] aad) {
        if (frame.length < TAG_LEN) throw new IllegalStateException("ccm frame too short");
        int n = frame.length - TAG_LEN;
        byte[] ct = Arrays.copyOfRange(frame, 0, n);
        byte[] want = Arrays.copyOfRange(frame, n, frame.length);

        Cipher ecb = ecb(key, Cipher.ENCRYPT_MODE);
        byte[] pt = new byte[n];
        int idx = 1;
        int off = 0;
        while (off < n) {
            byte[] s = block(ecb, ctrBlock(nonce, idx++));
            int k = Math.min(16, n - off);
            for (int i = 0; i < k; i++) pt[off + i] = (byte) (ct[off + i] ^ s[i]);
            off += k;
        }

        boolean hasAad = aad != null && aad.length > 0;
        byte[] b0 = new byte[16];
        b0[0] = (byte) ((hasAad ? 0x40 : 0) | (((TAG_LEN - 2) / 2) << 3) | (15 - nonce.length - 1));
        System.arraycopy(nonce, 0, b0, 1, nonce.length);
        int qOff = 1 + nonce.length;
        for (int i = 0; i < 16 - qOff; i++) b0[16 - 1 - i] = (byte) ((n >>> (8 * i)) & 0xFF);
        byte[] x = block(ecb, b0);
        if (hasAad) {
            byte[] head = new byte[2];
            head[0] = (byte) ((aad.length >>> 8) & 0xFF);
            head[1] = (byte) (aad.length & 0xFF);
            byte[] ab = pad16(Util.cat(head, aad));
            for (int i = 0; i < ab.length; i += 16) {
                x = block(ecb, xor(x, Arrays.copyOfRange(ab, i, i + 16)));
            }
        }
        byte[] pb = pad16(pt);
        for (int i = 0; i < pb.length; i += 16) {
            x = block(ecb, xor(x, Arrays.copyOfRange(pb, i, i + 16)));
        }
        byte[] s0 = block(ecb, ctrBlock(nonce, 0));
        byte[] tag = Arrays.copyOfRange(xor(x, s0), 0, TAG_LEN);
        if (!Arrays.equals(tag, want)) throw new IllegalStateException("ccm tag mismatch");
        return pt;
    }

    private static byte[] ctrBlock(byte[] nonce, int i) {
        byte[] a = new byte[16];
        a[0] = (byte) (15 - nonce.length - 1);
        System.arraycopy(nonce, 0, a, 1, nonce.length);
        for (int k = 0; k < 3; k++) a[15 - k] = (byte) ((i >>> (8 * k)) & 0xFF);
        return a;
    }

    private static byte[] pad16(byte[] in) {
        int n = ((in.length + 15) / 16) * 16;
        if (n == in.length && in.length > 0) return in;
        byte[] out = new byte[n];
        System.arraycopy(in, 0, out, 0, in.length);
        return out;
    }

    private static byte[] xor(byte[] a, byte[] b) {
        byte[] out = new byte[a.length];
        for (int i = 0; i < a.length; i++) out[i] = (byte) (a[i] ^ b[i]);
        return out;
    }

    private static Cipher ecb(byte[] key, int mode) {
        try {
            Cipher c = Cipher.getInstance("AES/ECB/NoPadding");
            c.init(mode, new SecretKeySpec(key, "AES"));
            return c;
        } catch (Exception e) {
            throw new IllegalStateException("aes failed", e);
        }
    }

    private static byte[] block(Cipher c, byte[] in) {
        try {
            return c.doFinal(in);
        } catch (Exception e) {
            throw new IllegalStateException("aes block failed", e);
        }
    }
}
