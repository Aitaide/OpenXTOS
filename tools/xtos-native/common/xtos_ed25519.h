/* xtos_ed25519.h — pinned-key ed25519 signature verification without any
 * external crypto binary. Backed by the vendored Monocypher ed25519 module
 * (BSD-2-Clause OR CC0-1.0, see vendor/monocypher/LICENCE.md); no openssl
 * process is executed at runtime.
 *
 * Public key sources: PEM SubjectPublicKeyInfo (exactly the format published
 * on the package mirrors), or a raw 32-byte key file. Signatures: 64-byte
 * detached RFC 8032 PureEdDSA — byte-compatible with the signatures the
 * offline release tooling produces via `openssl pkeyutl -rawin`.
 */
#ifndef XTOS_ED25519_H
#define XTOS_ED25519_H

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "monocypher-ed25519.h"

#define XED_MSG_MAX (4 * 1024 * 1024)

static int xed_b64_val(int c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

/* Load a 32-byte ed25519 public key from a PEM SPKI file or a raw file. */
static int xed25519_load_pub(const char *path, uint8_t out[32]) {
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    uint8_t buf[8192];
    size_t n = fread(buf, 1, sizeof(buf), f);
    int trailing = fgetc(f);
    fclose(f);
    if (trailing != EOF) return -1; /* file larger than the buffer: refuse */
    if (n == 32) {
        memcpy(out, buf, 32);
        return 0;
    }
    /* PEM SubjectPublicKeyInfo; the DER is exactly 44 bytes:
     * 30 2a 30 05 06 03 2b 65 70 03 21 00 <32-byte key>   (OID 1.3.101.112) */
    static const char begin[] = "-----BEGIN PUBLIC KEY-----";
    static const char end[] = "-----END PUBLIC KEY-----";
    char *text = (char *)buf;
    text[n] = '\0';
    char *b = strstr(text, begin);
    if (!b) return -1;
    b += strlen(begin);
    char *e = strstr(b, end);
    if (!e) return -1;
    /* base64: 4 chars -> 3 bytes, '=' pads only at the tail of a quad */
    uint8_t der[128];
    size_t dn = 0;
    int quad[4], quad_len = 0, finished = 0;
    for (char *p = b; p < e && dn + 3 <= sizeof(der); p++) {
        if (*p == '\n' || *p == '\r' || *p == ' ' || *p == '\t') continue;
        int v;
        if (*p == '=') {
            if (quad_len < 2) return -1;
            v = -1;
        } else {
            if (finished) return -1;
            v = xed_b64_val((unsigned char)*p);
            if (v < 0) return -1;
        }
        quad[quad_len++] = v;
        if (quad_len == 4) {
            quad_len = 0;
            der[dn++] = (uint8_t)((quad[0] << 2) | (quad[1] >> 4));
            if (quad[2] >= 0) der[dn++] = (uint8_t)(((quad[1] & 0x0f) << 4) | (quad[2] >> 2));
            if (quad[3] >= 0) der[dn++] = (uint8_t)(((quad[2] & 0x03) << 6) | quad[3]);
            if (quad[2] < 0 && quad[3] >= 0) return -1; /* '=' not trailing */
            if (quad[2] < 0 || quad[3] < 0) finished = 1;
        }
    }
    if (quad_len != 0) return -1; /* truncated base64 */
    static const uint8_t spki[12] = {0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00};
    if (dn != 44 || memcmp(der, spki, sizeof(spki)) != 0) return -1;
    memcpy(out, der + 12, 32);
    return 0;
}

/* Verify a detached 64-byte signature over a whole file. 0 = valid. */
static int xed25519_verify_file(const char *pubkey_path, const char *msg_path,
                                const char *sig_path) {
    uint8_t pub[32];
    if (xed25519_load_pub(pubkey_path, pub) < 0) return -1;
    FILE *fs = fopen(sig_path, "rb");
    if (!fs) return -1;
    uint8_t sig[64];
    size_t sn = fread(sig, 1, sizeof(sig), fs);
    int extra = fgetc(fs);
    fclose(fs);
    if (sn != sizeof(sig) || extra != EOF) return -1;
    FILE *fm = fopen(msg_path, "rb");
    if (!fm) return -1;
    uint8_t *msg = malloc(XED_MSG_MAX);
    if (!msg) { fclose(fm); return -1; }
    size_t mn = fread(msg, 1, XED_MSG_MAX, fm);
    int mextra = fgetc(fm);
    fclose(fm);
    if (mextra != EOF) { free(msg); return -1; } /* message too large: refuse */
    int rc = crypto_ed25519_check(sig, pub, msg, mn);
    free(msg);
    return rc == 0 ? 0 : -1;
}

#endif /* XTOS_ED25519_H */
