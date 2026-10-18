/* update: XTOS system update check and artifact download (C, static).
 *
 * Answers "is this XTOS the latest?" against the signed release index
 * published on every XTOS package source, and downloads release artifacts
 * with hash verification. It is the only networked native tool.
 *
 * Sources are probed in order (no geo-IP): XTOS_UPDATE_MIRRORS is a
 * space-separated mirror list, default "https://packages.xtos.cn
 * https://packages.xtos.si". The first mirror that serves a validly
 * signed index wins; mirrors are tried in order on every fetch, so a
 * broken mirror is skipped naturally. Security never depends on the
 * network path: the index signature is verified against the pinned
 * public key before anything is trusted, and downloads are verified
 * against the signed index sha256.
 *
 * Exit codes: 0 up-to-date / success, 1 update available / mismatch,
 * 2 error (unreachable, bad signature, bad hash).
 *
 * Build: scripts/build_mbedtls.sh (once), then
 *   gcc -O2 -Wall -Wextra -Werror -static \
 *     -DMBEDTLS_CONFIG_FILE='"xtos_mbedtls_config.h"' \
 *     -I tools/xtos-native/common -I tools/xtos-native/vendor/monocypher \
 *     -I tools/xtos-native/vendor/mbedtls/include \
 *     -I tools/xtos-native/vendor/mbedtls \
 *     -o update update.c \
 *     tools/xtos-native/vendor/monocypher/monocypher.c \
 *     tools/xtos-native/vendor/monocypher/monocypher-ed25519.c \
 *     .build/mbedtls/libmbedtls_xtos.a
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/fs.h>
#include <netdb.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include "xtos_ui.h"
#include "xtos_ed25519.h"
#include <mbedtls/entropy.h>
#include <mbedtls/hmac_drbg.h>
#include <mbedtls/md.h>
#include <mbedtls/ssl.h>
#include <mbedtls/x509_crt.h>

#define LINE_BUF 65536
#define URL_MAX 2048
#define NAME_MAX_LEN 256
#define VER_MAX_LEN 64
#define ART_MAX 64
#define MIRROR_MAX 8
#define XTOS_CA_BUNDLE_DEFAULT "/usr/share/xtos/update-ca-bundle.pem"

struct artifact {
    char name[NAME_MAX_LEN];
    char arch[32];
    char kind[32];
    char sha256[72];
    long size;
    char url[URL_MAX];
};

struct index_latest {
    char version[VER_MAX_LEN];
    char release_name[128];
    char channel[64];
    char published[32];
    char notes_url[URL_MAX];
    struct artifact arts[ART_MAX];
    int narts;
};

static char g_pubkey[2048];
static char g_local_release[2048];
static char g_offline_index[2048];
static char g_mirrors[4096];
static int g_timeout = 10;

/* ---- SHA-256 (same vetted implementation as pkg) ---- */
typedef struct { unsigned state[8]; unsigned long long bitlen; unsigned char data[64]; unsigned datalen; } sha256_ctx;

static void sha256_transform(sha256_ctx *ctx, const unsigned char data[]) {
    static const unsigned k[64] = {
        0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
        0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
        0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
        0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
        0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
        0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
        0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
        0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
    unsigned m[64], a,b,c,d,e,f,g,h,t1,t2;
    for (int i = 0; i < 16; i++)
        m[i] = (data[i*4]<<24)|(data[i*4+1]<<16)|(data[i*4+2]<<8)|data[i*4+3];
    for (int i = 16; i < 64; i++) {
        unsigned s0 = ((m[i-15]>>7)|(m[i-15]<<25)) ^ ((m[i-15]>>18)|(m[i-15]<<14)) ^ (m[i-15]>>3);
        unsigned s1 = ((m[i-2]>>17)|(m[i-2]<<15)) ^ ((m[i-2]>>19)|(m[i-2]<<13)) ^ (m[i-2]>>10);
        m[i] = m[i-16] + s0 + m[i-7] + s1;
    }
    a=ctx->state[0];b=ctx->state[1];c=ctx->state[2];d=ctx->state[3];
    e=ctx->state[4];f=ctx->state[5];g=ctx->state[6];h=ctx->state[7];
    for (int i = 0; i < 64; i++) {
        unsigned S1 = ((e>>6)|(e<<26)) ^ ((e>>11)|(e<<21)) ^ ((e>>25)|(e<<7));
        unsigned ch = (e & f) ^ (~e & g);
        t1 = h + S1 + ch + k[i] + m[i];
        unsigned S0 = ((a>>2)|(a<<30)) ^ ((a>>13)|(a<<19)) ^ ((a>>22)|(a<<10));
        unsigned maj = (a & b) ^ (a & c) ^ (b & c);
        t2 = S0 + maj;
        h=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;
    }
    ctx->state[0]+=a;ctx->state[1]+=b;ctx->state[2]+=c;ctx->state[3]+=d;
    ctx->state[4]+=e;ctx->state[5]+=f;ctx->state[6]+=g;ctx->state[7]+=h;
}

static void sha256_init(sha256_ctx *ctx) {
    ctx->datalen = 0; ctx->bitlen = 0;
    ctx->state[0]=0x6a09e667;ctx->state[1]=0xbb67ae85;ctx->state[2]=0x3c6ef372;ctx->state[3]=0xa54ff53a;
    ctx->state[4]=0x510e527f;ctx->state[5]=0x9b05688c;ctx->state[6]=0x1f83d9ab;ctx->state[7]=0x5be0cd19;
}

static void sha256_update(sha256_ctx *ctx, const unsigned char *data, size_t len) {
    for (size_t i = 0; i < len; i++) {
        ctx->data[ctx->datalen] = data[i]; ctx->datalen++;
        if (ctx->datalen == 64) {
            sha256_transform(ctx, ctx->data);
            ctx->bitlen += 512;
            ctx->datalen = 0;
        }
    }
}

static void sha256_final(sha256_ctx *ctx, unsigned char hash[]) {
    size_t i = ctx->datalen;
    if (ctx->datalen < 56) {
        ctx->data[i++] = 0x80;
        while (i < 56) ctx->data[i++] = 0x00;
    } else {
        ctx->data[i++] = 0x80;
        while (i < 64) ctx->data[i++] = 0x00;
        sha256_transform(ctx, ctx->data);
        memset(ctx->data, 0, 56);
    }
    ctx->bitlen += ctx->datalen * 8;
    ctx->data[63] = ctx->bitlen;
    ctx->data[62] = ctx->bitlen >> 8;
    ctx->data[61] = ctx->bitlen >> 16;
    ctx->data[60] = ctx->bitlen >> 24;
    ctx->data[59] = ctx->bitlen >> 32;
    ctx->data[58] = ctx->bitlen >> 40;
    ctx->data[57] = ctx->bitlen >> 48;
    ctx->data[56] = ctx->bitlen >> 56;
    sha256_transform(ctx, ctx->data);
    for (int j = 0; j < 8; j++)
        for (int l = 0; l < 4; l++)
            hash[j*4+l] = (ctx->state[j] >> (24 - l*8)) & 0xff;
}

static int sha256_file(const char *path, char *outhex) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    sha256_ctx ctx; sha256_init(&ctx);
    unsigned char buf[65536];
    ssize_t n;
    while ((n = read(fd, buf, sizeof(buf))) > 0)
        sha256_update(&ctx, buf, (size_t)n);
    close(fd);
    if (n < 0) return -1;
    unsigned char hash[32];
    sha256_final(&ctx, hash);
    for (int i = 0; i < 32; i++)
        sprintf(outhex + i*2, "%02x", hash[i]);
    outhex[64] = '\0';
    return 0;
}

/* Hash the first `limit` bytes of `path` (used for read-back verification of a
 * device that was just written: only the image extent belongs to the image). */
static int sha256_prefix(const char *path, long long limit, char *outhex) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    sha256_ctx ctx; sha256_init(&ctx);
    unsigned char buf[65536];
    long long total = 0;
    ssize_t n;
    while (total < limit && (n = read(fd, buf, sizeof(buf))) > 0) {
        size_t take = (size_t)n;
        if ((long long)take > limit - total) take = (size_t)(limit - total);
        sha256_update(&ctx, buf, take);
        total += (long long)take;
    }
    close(fd);
    if (n < 0 || total != limit) return -1;
    unsigned char hash[32];
    sha256_final(&ctx, hash);
    for (int i = 0; i < 32; i++)
        sprintf(outhex + i*2, "%02x", hash[i]);
    outhex[64] = '\0';
    return 0;
}

/* ---- helpers ---- */
static int json_str(const char *line, const char *key, char *out, size_t outsize) {
    char pattern[256];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(line, pattern);
    if (!p) return 0;
    p = strchr(p + strlen(pattern), ':');
    if (!p) return 0;
    p = strchr(p + 1, '"');
    if (!p) return 0;
    p++;
    const char *end = strchr(p, '"');
    if (!end) return 0;
    size_t len = (size_t)(end - p);
    if (len >= outsize) len = outsize - 1;
    memcpy(out, p, len);
    out[len] = '\0';
    return 1;
}

static int env_or(const char *name, char *out, size_t n, const char *fallback) {
    const char *v = getenv(name);
    if (v && v[0]) {
        if (strlen(v) >= n) return -1;
        strcpy(out, v);
        return 0;
    }
    if (strlen(fallback) >= n) return -1;
    strcpy(out, fallback);
    return 0;
}

/* ---- native HTTP(S) transport (vendored mbedTLS; no curl binary) ---- */
#include <mbedtls/net_sockets.h> /* MBEDTLS_ERR_NET_* codes for the BIO callbacks */
static uint64_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u;
}

struct url_parts {
    char scheme[8];
    char host[256];
    int port;
    char path[URL_MAX];
};

static int parse_url(const char *url, struct url_parts *u) {
    memset(u, 0, sizeof(*u));
    const char *sp = strstr(url, "://");
    if (!sp) return -1;
    size_t slen = (size_t)(sp - url);
    if (slen == 0 || slen >= sizeof(u->scheme)) return -1;
    memcpy(u->scheme, url, slen);
    u->scheme[slen] = '\0';
    const char *hp = sp + 3;
    const char *slash = strchr(hp, '/');
    if (!slash) return -1;
    size_t hlen = (size_t)(slash - hp);
    if (!hlen || hlen >= sizeof(u->host)) return -1;
    const char *colon = memchr(hp, ':', hlen);
    if (colon) {
        size_t hn = (size_t)(colon - hp);
        if (!hn || hn >= sizeof(u->host)) return -1;
        memcpy(u->host, hp, hn);
        u->host[hn] = '\0';
        long p = strtol(colon + 1, NULL, 10);
        if (p <= 0 || p > 65535) return -1;
        u->port = (int)p;
    } else {
        memcpy(u->host, hp, hlen);
        u->host[hlen] = '\0';
        u->port = strcmp(u->scheme, "https") == 0 ? 443 : 80;
    }
    if (strlen(slash) >= sizeof(u->path)) return -1;
    strcpy(u->path, slash); /* always starts with '/' */
    return 0;
}

static int tcp_connect(const char *host, int port, uint64_t deadline) {
    char portstr[8];
    snprintf(portstr, sizeof(portstr), "%d", port);
    struct addrinfo hints, *res = NULL, *rp;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host, portstr, &hints, &res) != 0 || !res) return -1;
    int fd = -1;
    for (rp = res; rp; rp = rp->ai_next) {
        fd = socket(rp->ai_family, rp->ai_socktype, rp->ai_protocol);
        if (fd < 0) continue;
        int fl = fcntl(fd, F_GETFL, 0);
        if (fl >= 0) fcntl(fd, F_SETFL, fl | O_NONBLOCK);
        if (connect(fd, rp->ai_addr, rp->ai_addrlen) == 0) break;
        if (errno != EINPROGRESS) { close(fd); fd = -1; continue; }
        int64_t wait = (int64_t)(deadline - now_ms());
        struct pollfd pfd = { .fd = fd, .events = POLLOUT };
        if (wait <= 0 || poll(&pfd, 1, (int)wait) != 1) { close(fd); fd = -1; continue; }
        int err = 0;
        socklen_t el = sizeof(err);
        if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &el) != 0 || err != 0) { close(fd); fd = -1; continue; }
        break;
    }
    freeaddrinfo(res);
    return fd;
}

/* TLS machinery, lazily initialized from the pinned CA bundle. */
static mbedtls_entropy_context g_ts_entropy;
static mbedtls_hmac_drbg_context g_ts_drbg;
static mbedtls_ssl_config g_ts_conf;
static mbedtls_x509_crt g_ts_cacerts;
static int g_ts_ready;

static int tls_setup(void) {
    if (g_ts_ready) return 0;
    int rc;
    mbedtls_entropy_init(&g_ts_entropy);
    mbedtls_hmac_drbg_init(&g_ts_drbg);
    mbedtls_x509_crt_init(&g_ts_cacerts);
    mbedtls_ssl_config_init(&g_ts_conf);
    const char *pers = "xtos-update";
    if ((rc = mbedtls_hmac_drbg_seed(&g_ts_drbg, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256),
                                     mbedtls_entropy_func, &g_ts_entropy,
                                     (const unsigned char *)pers, strlen(pers))) != 0) goto fail;
    char bundle[4096];
    if (env_or("XTOS_UPDATE_CA_BUNDLE", bundle, sizeof(bundle), XTOS_CA_BUNDLE_DEFAULT) < 0) goto fail;
    if ((rc = mbedtls_x509_crt_parse_file(&g_ts_cacerts, bundle)) < 0) goto fail;
    if ((rc = mbedtls_ssl_config_defaults(&g_ts_conf, MBEDTLS_SSL_IS_CLIENT,
                                          MBEDTLS_SSL_TRANSPORT_STREAM,
                                          MBEDTLS_SSL_PRESET_DEFAULT)) != 0) goto fail;
    mbedtls_ssl_conf_authmode(&g_ts_conf, MBEDTLS_SSL_VERIFY_REQUIRED);
    mbedtls_ssl_conf_ca_chain(&g_ts_conf, &g_ts_cacerts, NULL);
    mbedtls_ssl_conf_rng(&g_ts_conf, mbedtls_hmac_drbg_random, &g_ts_drbg);
    g_ts_ready = 1;
    return 0;
fail:
    return -1;
}

static int tls_send_cb(void *ctx, const unsigned char *buf, size_t len) {
    int fd = *(int *)ctx;
    ssize_t w = write(fd, buf, len);
    if (w >= 0) return (int)w;
    if (errno == EAGAIN || errno == EWOULDBLOCK) return MBEDTLS_ERR_SSL_WANT_WRITE;
    return MBEDTLS_ERR_NET_SEND_FAILED;
}

static int tls_recv_cb(void *ctx, unsigned char *buf, size_t len) {
    int fd = *(int *)ctx;
    ssize_t r = read(fd, buf, len);
    if (r > 0) return (int)r;
    if (r == 0) return MBEDTLS_ERR_SSL_CONN_EOF;
    if (errno == EAGAIN || errno == EWOULDBLOCK) return MBEDTLS_ERR_SSL_WANT_READ;
    return MBEDTLS_ERR_NET_RECV_FAILED;
}

/* Buffered response reader over a plain or TLS socket. */
struct http_reader {
    int fd;
    mbedtls_ssl_context *ssl;
    uint64_t deadline;
    unsigned char buf[16384];
    size_t len, pos;
};

/* 0: data available, 1: EOF, -1: error/timeout */
static int rd_fill(struct http_reader *r) {
    if (r->pos < r->len) return 0;
    for (;;) {
        int64_t wait = (int64_t)(r->deadline - now_ms());
        if (wait <= 0) return -1;
        if (r->ssl) {
            int m = mbedtls_ssl_read(r->ssl, r->buf, sizeof(r->buf));
            if (m == MBEDTLS_ERR_SSL_WANT_READ || m == MBEDTLS_ERR_SSL_WANT_WRITE) {
                struct pollfd pfd = { .fd = r->fd,
                                      .events = (m == MBEDTLS_ERR_SSL_WANT_READ ? POLLIN : POLLOUT) };
                if (poll(&pfd, 1, (int)wait) <= 0) return -1;
                continue;
            }
            if (m == MBEDTLS_ERR_SSL_PEER_CLOSE_NOTIFY) return 1;
            if (m < 0) return -1;
            r->len = (size_t)m;
        } else {
            ssize_t m = read(r->fd, r->buf, sizeof(r->buf));
            if (m == 0) return 1;
            if (m < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                struct pollfd pfd = { .fd = r->fd, .events = POLLIN };
                if (poll(&pfd, 1, (int)wait) <= 0) return -1;
                continue;
            }
            if (m < 0) return -1;
            r->len = (size_t)m;
        }
        r->pos = 0;
        return 0;
    }
}

static int rd_byte(struct http_reader *r) {
    if (rd_fill(r) != 0) return -1;
    return r->buf[r->pos++];
}

/* Read one CRLF/LF-terminated line (without the terminator). */
static int rd_line(struct http_reader *r, char *out, size_t n) {
    size_t used = 0;
    for (;;) {
        int c = rd_byte(r);
        if (c < 0) return -1;
        if (c == '\n') break;
        if (used + 1 < n) out[used++] = (char)c;
    }
    if (used && out[used - 1] == '\r') used--;
    out[used] = '\0';
    return 0;
}

static int rd_exact(struct http_reader *r, FILE *dest, size_t n) {
    while (n) {
        int rc = rd_fill(r);
        if (rc != 0) return -1;
        size_t take = r->len - r->pos;
        if (take > n) take = n;
        if (fwrite(r->buf + r->pos, 1, take, dest) != take) return -1;
        r->pos += take;
        n -= take;
    }
    return 0;
}

static int rd_until_eof(struct http_reader *r, FILE *dest) {
    for (;;) {
        int rc = rd_fill(r);
        if (rc == 1) return 0;
        if (rc != 0) return -1;
        if (fwrite(r->buf + r->pos, 1, r->len - r->pos, dest) != r->len - r->pos) return -1;
        r->pos = r->len;
    }
}

static int tls_handshake(mbedtls_ssl_context *ssl, struct http_reader *r) {
    for (;;) {
        int rc = mbedtls_ssl_handshake(ssl);
        if (rc == 0) break;
        if (rc == MBEDTLS_ERR_SSL_WANT_READ || rc == MBEDTLS_ERR_SSL_WANT_WRITE) {
            int64_t wait = (int64_t)(r->deadline - now_ms());
            struct pollfd pfd = { .fd = r->fd,
                                  .events = (rc == MBEDTLS_ERR_SSL_WANT_READ ? POLLIN : POLLOUT) };
            if (wait <= 0 || poll(&pfd, 1, (int)wait) != 1) return -1;
            continue;
        }
        return -1;
    }
    return mbedtls_ssl_get_verify_result(ssl) == 0 ? 0 : -1;
}

static int http_fetch(struct url_parts *u, FILE *dest, uint64_t deadline) {
    int fd = tcp_connect(u->host, u->port, deadline);
    if (fd < 0) return -1;
    mbedtls_ssl_context ssl;
    int use_tls = strcmp(u->scheme, "https") == 0;
    if (use_tls) {
        if (tls_setup() < 0) { close(fd); return -1; }
        mbedtls_ssl_init(&ssl);
        if (mbedtls_ssl_setup(&ssl, &g_ts_conf) != 0 ||
            mbedtls_ssl_set_hostname(&ssl, u->host) != 0) {
            mbedtls_ssl_free(&ssl);
            close(fd);
            return -1;
        }
        mbedtls_ssl_set_bio(&ssl, &fd, tls_send_cb, tls_recv_cb, NULL);
    }
    struct http_reader r;
    memset(&r, 0, sizeof(r));
    r.fd = fd;
    r.ssl = use_tls ? &ssl : NULL;
    r.deadline = deadline;
    int rc = -1;
    do {
        if (use_tls && tls_handshake(&ssl, &r) < 0) break;
        char req[URL_MAX + 512];
        int rl = snprintf(req, sizeof(req),
                          "GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: xtos-update/1.0\r\n"
                          "Accept: */*\r\nConnection: close\r\n\r\n",
                          u->path, u->host);
        if (rl < 0 || (size_t)rl >= sizeof(req)) break;
        /* write the request */
        {
            const unsigned char *p = (const unsigned char *)req;
            size_t left = (size_t)rl;
            int ok = 1;
            while (left && ok) {
                int64_t wait = (int64_t)(deadline - now_ms());
                if (wait <= 0) { ok = 0; break; }
                ssize_t w;
                if (use_tls) {
                    int m = mbedtls_ssl_write(&ssl, p, left);
                    if (m == MBEDTLS_ERR_SSL_WANT_WRITE || m == MBEDTLS_ERR_SSL_WANT_READ) {
                        struct pollfd pfd = { .fd = fd,
                                              .events = (m == MBEDTLS_ERR_SSL_WANT_WRITE ? POLLOUT : POLLIN) };
                        if (poll(&pfd, 1, (int)wait) <= 0) ok = 0;
                        continue;
                    }
                    if (m <= 0) { ok = 0; break; }
                    w = m;
                } else {
                    w = write(fd, p, left);
                    if (w < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                        struct pollfd pfd = { .fd = fd, .events = POLLOUT };
                        if (poll(&pfd, 1, (int)wait) <= 0) ok = 0;
                        continue;
                    }
                    if (w <= 0) { ok = 0; break; }
                }
                p += w;
                left -= (size_t)w;
            }
            if (!ok) break;
        }
        /* status line must be 200 (HTTP/1.0 or HTTP/1.1) */
        char line[2048];
        if (rd_line(&r, line, sizeof(line)) < 0) break;
        if (strncmp(line, "HTTP/1.", 7) != 0) break;
        long status = strtol(line + 7 + 1, NULL, 10); /* skip "HTTP/1." */
        if ((line[7] != '0' && line[7] != '1') || status != 200) break;
        /* headers */
        long long content_length = -1;
        int chunked = 0;
        int bad = 0;
        for (;;) {
            if (rd_line(&r, line, sizeof(line)) < 0) { bad = 1; break; }
            if (line[0] == '\0') break;
            if (strncasecmp(line, "content-length:", 15) == 0)
                content_length = strtoll(line + 15, NULL, 10);
            else if (strncasecmp(line, "transfer-encoding:", 18) == 0 &&
                     strstr(line, "chunked"))
                chunked = 1;
        }
        if (bad) break;
        /* body */
        if (chunked) {
            for (;;) {
                if (rd_line(&r, line, sizeof(line)) < 0) { bad = 1; break; }
                char *semi = strchr(line, ';');
                if (semi) *semi = '\0';
                unsigned long long clen = strtoull(line, NULL, 16);
                if (clen == 0) {
                    /* trailers until an empty line */
                    for (;;) {
                        if (rd_line(&r, line, sizeof(line)) < 0) { bad = 1; break; }
                        if (line[0] == '\0') break;
                    }
                    break;
                }
                if (rd_exact(&r, dest, (size_t)clen) < 0) { bad = 1; break; }
                /* consume the chunk-terminating CRLF */
                if (rd_byte(&r) < 0 || rd_byte(&r) < 0) { bad = 1; break; }
            }
        } else if (content_length >= 0) {
            if (rd_exact(&r, dest, (size_t)content_length) < 0) break;
        } else {
            if (rd_until_eof(&r, dest) < 0) break;
        }
        if (!bad) rc = 0;
    } while (0);
    if (use_tls) mbedtls_ssl_free(&ssl);
    close(fd);
    return rc;
}

static int fetch_url(const char *url, const char *dest, const char *tmpdir) {
    struct url_parts u;
    if (parse_url(url, &u) < 0) return -1;
    if (strcmp(u.scheme, "http") != 0 && strcmp(u.scheme, "https") != 0) return -1;
    char tpath[4096];
    if (snprintf(tpath, sizeof(tpath), "%s/fetch-XXXXXX", tmpdir) >= (int)sizeof(tpath)) return -1;
    int fd = mkstemp(tpath);
    if (fd < 0) return -1;
    close(fd);
    FILE *out = fopen(tpath, "wb");
    if (!out) { unlink(tpath); return -1; }
    uint64_t deadline = now_ms() + (uint64_t)(g_timeout > 0 ? g_timeout : 30) * 1000u;
    int rc = http_fetch(&u, out, deadline);
    if (fclose(out) != 0) rc = -1;
    if (rc == 0 && rename(tpath, dest) == 0) return 0;
    unlink(tpath);
    return -1;
}

/* ---- version compare: MAJOR.MINOR.PATCH[-prerelease] ----
 * prerelease ordering: no prerelease > any prerelease; dot-separated
 * tokens compared numerically when both numeric, else strcmp; more
 * tokens > fewer when all compared equal. */
struct verpart { long maj, min, pat; char pre[VER_MAX_LEN]; int has_pre; };

static int parse_version(const char *s, struct verpart *v) {
    memset(v, 0, sizeof(*v));
    const char *dash = strchr(s, '-');
    char core[VER_MAX_LEN];
    size_t corelen = dash ? (size_t)(dash - s) : strlen(s);
    if (!corelen || corelen >= sizeof(core)) return -1;
    memcpy(core, s, corelen);
    core[corelen] = '\0';
    char *endp = NULL;
    v->maj = strtol(core, &endp, 10);
    if (!endp || *endp != '.') return -1;
    v->min = strtol(endp + 1, &endp, 10);
    if (!endp || *endp != '.') return -1;
    v->pat = strtol(endp + 1, &endp, 10);
    if (!endp || *endp != '\0') return -1;
    if (dash) {
        if (strlen(dash + 1) >= sizeof(v->pre)) return -1;
        strcpy(v->pre, dash + 1);
        v->has_pre = 1;
    }
    return 0;
}

static int cmp_prerelease(const char *a, const char *b) {
    while (*a || *b) {
        char ta[64] = "", tb[64] = "";
        size_t la = 0, lb = 0;
        while (*a && *a != '.' && la < sizeof(ta) - 1) ta[la++] = *a++;
        while (*b && *b != '.' && lb < sizeof(tb) - 1) tb[lb++] = *b++;
        ta[la] = '\0'; tb[lb] = '\0';
        int numa = (la && strspn(ta, "0123456789") == la) ? 1 : 0;
        int numb = (lb && strspn(tb, "0123456789") == lb) ? 1 : 0;
        int c;
        if (numa && numb) {
            long va = strtol(ta, NULL, 10), vb = strtol(tb, NULL, 10);
            c = va < vb ? -1 : (va > vb ? 1 : 0);
        } else {
            c = strcmp(ta, tb);
        }
        if (c) return c;
        if (*a) a++;
        if (*b) b++;
        if (!*a && *b) return -1;
        if (*a && !*b) return 1;
    }
    return 0;
}

/* returns -1 a<b, 0 equal, 1 a>b */
static int version_cmp(const char *a, const char *b) {
    struct verpart va, vb;
    if (parse_version(a, &va) < 0 || parse_version(b, &vb) < 0) return -2;
    if (va.maj != vb.maj) return va.maj < vb.maj ? -1 : 1;
    if (va.min != vb.min) return va.min < vb.min ? -1 : 1;
    if (va.pat != vb.pat) return va.pat < vb.pat ? -1 : 1;
    if (!va.has_pre && !vb.has_pre) return 0;
    if (!va.has_pre) return 1;   /* final > prerelease */
    if (!vb.has_pre) return -1;
    return cmp_prerelease(va.pre, vb.pre) < 0 ? -1 : (cmp_prerelease(va.pre, vb.pre) > 0 ? 1 : 0);
}

/* ---- local release + arch ---- */
static int read_local_version(char *out, size_t n) {
    FILE *f = fopen(g_local_release, "r");
    if (!f) return -1;
    char line[512];
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "XTOS_VERSION=", 13) == 0) {
            line[strcspn(line, "\n")] = '\0';
            const char *v = line + 13;
            if (strlen(v) >= n) { fclose(f); return -1; }
            strcpy(out, v);
            fclose(f);
            return 0;
        }
    }
    fclose(f);
    return -1;
}

static void local_arch(char *out, size_t n) {
    const char *env = getenv("XTOS_UPDATE_ARCH");
    if (env && env[0]) { snprintf(out, n, "%s", env); return; }
    struct utsname u;
    if (uname(&u) == 0 && strcmp(u.machine, "x86_64") == 0) { snprintf(out, n, "x86_64"); return; }
    if (uname(&u) == 0 && (strncmp(u.machine, "aarch64", 7) == 0)) { snprintf(out, n, "arm64"); return; }
    if (uname(&u) == 0 && (strncmp(u.machine, "riscv64", 7) == 0)) { snprintf(out, n, "riscv64"); return; }
    if (uname(&u) == 0 && strlen(u.machine) < n) { snprintf(out, n, "%s", u.machine); return; }
    snprintf(out, n, "unknown");
}

/* ---- index fetch + verify ---- */
static int verify_index_sig(const char *index_path, const char *sig_path) {
    /* Native ed25519 check against the pinned key (vendored Monocypher).
     * No openssl binary is executed. */
    return xed25519_verify_file(g_pubkey, index_path, sig_path);
}

static int parse_index(const char *path, struct index_latest *latest, const char *expect_kind) {
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    char line[LINE_BUF];
    memset(latest, 0, sizeof(*latest));
    struct artifact *cur = NULL;
    int schema_ok = 0, kind_ok = 0, in_artifacts = 0, rc = 0;
    while (fgets(line, sizeof(line), f)) {
        if (!schema_ok && strstr(line, "\"schema_version\"")) {
            char *p = strchr(line, ':');
            if (!p || strtol(p + 1, NULL, 10) != 1) { rc = -1; break; }
            schema_ok = 1;
            continue;
        }
        if (!kind_ok && strstr(line, "\"kind\"")) {
            char v[64];
            if (!json_str(line, "kind", v, sizeof(v)) || strcmp(v, expect_kind) != 0) { rc = -1; break; }
            kind_ok = 1;
            continue;
        }
        if (strstr(line, "\"artifacts\"")) {
            in_artifacts = strchr(line, '[') && strchr(line, ']') ? 0 : 1;
            continue;
        }
        if (in_artifacts) {
            /* latest-level keys mark the array as closed (empty arrays too) */
            if (strstr(line, "\"channel\"") || strstr(line, "\"commit\"") || strstr(line, "\"notes_url\"")
                || strstr(line, "\"published\"") || strstr(line, "\"release_name\"") || strstr(line, "\"version\"")) {
                in_artifacts = 0;
            } else if (!strchr(line, '"')) {
                if (cur && strchr(line, '}')) cur = NULL;
                else if (strchr(line, ']')) in_artifacts = 0;
                continue;
            } else {
                if (!cur) {
                    if (latest->narts >= ART_MAX) { rc = -1; break; }
                    cur = &latest->arts[latest->narts++];
                    memset(cur, 0, sizeof(*cur));
                }
                if (strstr(line, "\"arch\"")) { if (!json_str(line, "arch", cur->arch, sizeof(cur->arch))) { rc = -1; break; } }
                else if (strstr(line, "\"kind\"")) { if (!json_str(line, "kind", cur->kind, sizeof(cur->kind))) { rc = -1; break; } }
                else if (strstr(line, "\"name\"")) { if (!json_str(line, "name", cur->name, sizeof(cur->name))) { rc = -1; break; } }
                else if (strstr(line, "\"sha256\"")) { if (!json_str(line, "sha256", cur->sha256, sizeof(cur->sha256))) { rc = -1; break; } }
                else if (strstr(line, "\"url\"")) { if (!json_str(line, "url", cur->url, sizeof(cur->url))) { rc = -1; break; } }
                else if (strstr(line, "\"size\"")) { char *p = strstr(line, "\"size\""); p = strchr(p, ':'); if (!p) { rc = -1; break; } cur->size = atol(p + 1); }
                continue;
            }
        }
        if (!latest->version[0]) json_str(line, "version", latest->version, sizeof(latest->version));
        if (!latest->release_name[0]) json_str(line, "release_name", latest->release_name, sizeof(latest->release_name));
        if (!latest->channel[0]) json_str(line, "channel", latest->channel, sizeof(latest->channel));
        if (!latest->published[0]) json_str(line, "published", latest->published, sizeof(latest->published));
        if (!latest->notes_url[0]) json_str(line, "notes_url", latest->notes_url, sizeof(latest->notes_url));
    }
    fclose(f);
    if (rc == 0 && (!schema_ok || !kind_ok || !latest->version[0])) rc = -1;
    return rc;
}

/* ---- update apply: turn a verified download into bootable media ---- */

/* Written by `update download` next to the artifacts; consumed by `apply`.
 * Atomic (tmp+rename), overwrites any previous manifest from older runs. */
static int write_download_manifest(const char *destdir, struct artifact **got, int ngot,
                                   const char *version, const char *channel, const char *arch) {
    char final[4096], tmp[4096];
    if (snprintf(final, sizeof(final), "%s/download-manifest.json", destdir) >= (int)sizeof(final)) return -1;
    if (snprintf(tmp, sizeof(tmp), "%s.download-manifest.tmp", destdir) >= (int)sizeof(tmp)) return -1;
    FILE *f = fopen(tmp, "w");
    if (!f) return -1;
    fprintf(f, "{\n  \"schema_version\": 1,\n  \"kind\": \"xtos-download-manifest\",\n  \"version\": \"%s\",\n"
              "  \"channel\": \"%s\",\n  \"arch\": \"%s\",\n  \"artifacts\": [\n", version, channel, arch);
    for (int i = 0; i < ngot; i++) {
        struct artifact *a = got[i];
        /* One key per line: the device-side parser is line-based (same layout
         * as the signed release index), and "size" must be anchored so a
         * single-line object can never make it read the wrong colon. */
        fprintf(f, "    {\n      \"arch\": \"%s\",\n      \"kind\": \"%s\",\n      \"name\": \"%s\",\n"
                  "      \"sha256\": \"%s\",\n      \"size\": %ld,\n      \"url\": \"%s\"\n    }%s\n",
                a->arch, a->kind, a->name, a->sha256, a->size, a->url, i + 1 < ngot ? "," : "");
    }
    fprintf(f, "  ]\n}\n");
    if (fclose(f) != 0) { unlink(tmp); return -1; }
    if (rename(tmp, final) < 0) { unlink(tmp); return -1; }
    return 0;
}

/* Whole-disk device path whitelist: the same canonical forms xtos-install
 * accepts (plus loop devices, which are legitimate test/tool targets). */
static int valid_whole_disk_path(const char *p) {
    if (strncmp(p, "/dev/sd", 7) == 0) {
        if (!p[7]) return 0;
        for (const char *q = p + 7; *q; q++)
            if (!(*q >= 'a' && *q <= 'z')) return 0;
        return 1;
    }
    if (strncmp(p, "/dev/nvme", 9) == 0) {
        const char *q = p + 9;
        if (!isdigit((unsigned char)*q)) return 0;
        while (isdigit((unsigned char)*q)) q++;
        if (*q != 'n') return 0;
        q++;
        if (!isdigit((unsigned char)*q)) return 0;
        while (isdigit((unsigned char)*q)) q++;
        return *q == '\0';
    }
    if (strncmp(p, "/dev/loop", 9) == 0) {
        if (!p[9]) return 0;
        for (const char *q = p + 9; *q; q++)
            if (!isdigit((unsigned char)*q)) return 0;
        return 1;
    }
    return 0;
}

/* Collect major:minor of the device and every partition of it. Prefix match
 * plus digit/'p'+digit continuation avoids sdX/sdXX false matches. */
static int collect_target_devs(const char *devpath, int (*devs)[2], int max) {
    const char *slash = strrchr(devpath, '/');
    const char *name = slash ? slash + 1 : devpath;
    size_t plen = strlen(name);
    DIR *d = opendir("/sys/class/block");
    if (!d) return -1;
    int n = 0;
    struct dirent *e;
    while ((e = readdir(d))) {
        if (strncmp(e->d_name, name, plen) != 0) continue;
        if (strlen(e->d_name) > plen) {
            char c1 = e->d_name[plen];
            if (!(isdigit((unsigned char)c1) ||
                  (c1 == 'p' && isdigit((unsigned char)e->d_name[plen + 1]))))
                continue;
        }
        char devfile[600], buf[64];
        snprintf(devfile, sizeof(devfile), "/sys/class/block/%s/dev", e->d_name);
        FILE *f = fopen(devfile, "r");
        if (!f) continue;
        int maj, min;
        if (fgets(buf, sizeof(buf), f) && sscanf(buf, "%d:%d", &maj, &min) == 2 && n < max) {
            devs[n][0] = maj;
            devs[n][1] = min;
            n++;
        }
        fclose(f);
    }
    closedir(d);
    return n;
}

/* Refusal reasons for a prospective write target. Returns 0 (allowed) or 1
 * with a human reason. Covers: unknown device, partition targets, mounted
 * device or partitions (the running Live medium is mounted — this check is
 * what refuses re-burning the stick the session is running from), active
 * swap on the device. With xtos.toram=1 the medium is released at boot, so
 * apply --to that same stick becomes the intended self-upgrade path. */
static int target_refusal_reason(const char *devpath, char *why, size_t whyn) {
    int devs[128][2];
    int ndevs = collect_target_devs(devpath, devs, 128);
    if (ndevs <= 0) { snprintf(why, whyn, "no such block device in /sys/class/block"); return 1; }
    char pfile[600];
    const char *name = strrchr(devpath, '/') + 1;
    snprintf(pfile, sizeof(pfile), "/sys/class/block/%s/partition", name);
    struct stat st;
    if (stat(pfile, &st) == 0) { snprintf(why, whyn, "target is a partition, not a whole disk"); return 1; }
    FILE *f = fopen("/proc/self/mountinfo", "r");
    if (f) {
        char line[8192];
        while (fgets(line, sizeof(line), f)) {
            /* field 3 is the major:minor of the mounted filesystem device */
            char *tok = NULL, *devtok = NULL;
            int pos = 0;
            for (tok = strtok(line, " "); tok && pos < 3; tok = strtok(NULL, " "), pos++)
                if (pos == 2) { devtok = tok; break; }
            if (!devtok) continue;
            int maj, min;
            if (sscanf(devtok, "%d:%d", &maj, &min) == 2) {
                for (int i = 0; i < ndevs; i++) {
                    if (devs[i][0] == maj && devs[i][1] == min) {
                        fclose(f);
                        snprintf(why, whyn, "device or one of its partitions is mounted "
                                           "(this also protects the running Live medium)");
                        return 1;
                    }
                }
            }
        }
        fclose(f);
    }
    f = fopen("/proc/swaps", "r");
    if (f) {
        char real[4096], line[1024];
        if (realpath(devpath, real)) {
            while (fgets(line, sizeof(line), f)) {
                char spath[1024] = "", sreal[4096];
                if (sscanf(line, "%1023s", spath) != 1 || strncmp(spath, "Filename", 8) == 0) continue;
                if (realpath(spath, sreal) && strcmp(sreal, real) == 0) {
                    fclose(f);
                    snprintf(why, whyn, "device is in active use as swap");
                    return 1;
                }
            }
        }
        fclose(f);
    }
    return 0;
}

/* Stream the image to the device (4 MiB chunks, short-write safe) and sync. */
static int copy_iso_to_device(const char *iso, const char *dev, long long size) {
    int in = open(iso, O_RDONLY);
    if (in < 0) return -1;
    int out = open(dev, O_WRONLY);
    if (out < 0) { close(in); return -1; }
    static char buf[4 * 1024 * 1024];
    long long total = 0;
    int rc = 0;
    while (total < size) {
        size_t want = (size_t)(size - total);
        if (want > sizeof(buf)) want = sizeof(buf);
        ssize_t r = read(in, buf, want);
        if (r <= 0) { rc = -1; break; }
        ssize_t off = 0;
        while (off < r) {
            ssize_t w = write(out, buf + off, (size_t)(r - off));
            if (w < 0) { rc = -1; break; }
            off += w;
        }
        if (rc) break;
        total += r;
    }
    if (rc == 0 && total != size) rc = -1;
    if (rc == 0 && fdatasync(out) < 0) rc = -1;
    close(in);
    close(out);
    return rc;
}

/* Obtain index+sig into tmpdir from mirrors or offline path. Fills index_path/sig_path.
 * Returns 1 when the files are ours (fetched into tmpdir, caller must unlink),
 * 0 when offline mode borrowed the caller's files (never unlink), -1 on failure. */
static int obtain_index(const char *tmpdir, char *index_path, size_t in_, char *sig_path, size_t sn, char *used_url, size_t un) {
    if (g_offline_index[0]) {
        struct stat st;
        if (stat(g_offline_index, &st) < 0) return -1;
        if (snprintf(index_path, in_, "%s", g_offline_index) >= (int)in_) return -1;
        if (snprintf(sig_path, sn, "%s.sig", g_offline_index) >= (int)sn) return -1;
        if (stat(sig_path, &st) < 0) return -1;
        if (strlen(g_offline_index) + 6 < un) snprintf(used_url, un, "file:%s", g_offline_index);
        return 0;
    }
    char mirrors[4096];
    if (env_or("XTOS_UPDATE_MIRRORS", mirrors, sizeof(mirrors), g_mirrors) < 0) return -1;
    char *list[MIRROR_MAX];
    int nlist = 0;
    char *tok = mirrors;
    while (nlist < MIRROR_MAX) {
        while (*tok == ' ') tok++;
        if (!*tok) break;
        char *end = strchr(tok, ' ');
        if (end) *end = '\0';
        if (*tok) list[nlist++] = tok;
        if (!end) break;
        tok = end + 1;
    }
    for (int i = 0; i < nlist; i++) {
        char url[URL_MAX + 128];
        if (snprintf(url, sizeof(url), "%s/release-index.json", list[i]) >= (int)sizeof(url)) continue;
        if (snprintf(index_path, in_, "%s/release-index.json", tmpdir) >= (int)in_) return -1;
        if (snprintf(sig_path, sn, "%s/release-index.json.sig", tmpdir) >= (int)sn) return -1;
        if (fetch_url(url, index_path, tmpdir) < 0) continue;
        if (snprintf(url, sizeof(url), "%s/release-index.json.sig", list[i]) >= (int)sizeof(url)) continue;
        if (fetch_url(url, sig_path, tmpdir) < 0) continue;
        snprintf(used_url, un, "%s", list[i]);
        return 1;
    }
    return -1;
}

static void usage(void) {
    fprintf(stderr,
        "usage: update <command> [args]\n"
        "  update check                 compare installed XTOS with the signed release index\n"
        "  update download [--dest DIR] download artifacts for this architecture (verified)\n"
        "  update apply --from DIR --to DEV --confirm ERASE-XTOS\n"
        "                               write the verified installer image to a spare whole disk\n"
        "                               (bootable upgrade medium; refuses mounted/running media)\n"
        "  update compare <a> <b>       compare two versions (prints older/equal/newer)\n"
        "Environment: XTOS_UPDATE_MIRRORS (space-separated), XTOS_UPDATE_PUBKEY,\n"
        "  XTOS_UPDATE_LOCAL_RELEASE (default /etc/xtos-release), XTOS_UPDATE_TIMEOUT_SEC,\n"
        "  XTOS_UPDATE_OFFLINE_INDEX (local index path), XTOS_UPDATE_ARCH.\n"
        "Exit codes: 0 up-to-date/success, 1 update available, 2 error.\n");
}

int main(int argc, char **argv) {
    int json = 0, argstart = 1;
    if (argc > 1 && strcmp(argv[1], "--json") == 0) { json = 1; argstart = 2; }
    if (argc <= argstart) { usage(); return 2; }
    if (env_or("XTOS_UPDATE_PUBKEY", g_pubkey, sizeof(g_pubkey), "/usr/share/xtos/repo-public.pem") < 0) return 2;
    if (env_or("XTOS_UPDATE_LOCAL_RELEASE", g_local_release, sizeof(g_local_release), "/etc/xtos-release") < 0) return 2;
    if (env_or("XTOS_UPDATE_MIRRORS", g_mirrors, sizeof(g_mirrors), "https://packages.xtos.cn https://packages.xtos.si") < 0) return 2;
    if (env_or("XTOS_UPDATE_OFFLINE_INDEX", g_offline_index, sizeof(g_offline_index), "") < 0) return 2;
    const char *t = getenv("XTOS_UPDATE_TIMEOUT_SEC");
    if (t && t[0]) g_timeout = atoi(t);
    const char *cmd = argv[argstart];

    if (strcmp(cmd, "compare") == 0 && argc == argstart + 3) {
        int c = version_cmp(argv[argstart + 1], argv[argstart + 2]);
        if (c == -2) { fprintf(stderr, "update: invalid version\n"); return 2; }
        if (json)
            printf("{\"a\": \"%s\", \"b\": \"%s\", \"result\": \"%s\"}\n", argv[argstart + 1], argv[argstart + 2],
                   c < 0 ? "older" : (c > 0 ? "newer" : "equal"));
        else
            printf("%s\n", c < 0 ? "older" : (c > 0 ? "newer" : "equal"));
        return 0;
    }

    if (strcmp(cmd, "check") == 0 && argc == argstart + 1) {
        char local[VER_MAX_LEN];
        if (read_local_version(local, sizeof(local)) < 0) {
            fprintf(stderr, "update: cannot read XTOS_VERSION from %s\n", g_local_release);
            return 2;
        }
        char tmpdir[] = "/tmp/xtos-update-XXXXXX";
        if (!mkdtemp(tmpdir)) { fprintf(stderr, "update: cannot create tmp dir\n"); return 2; }
        char index_path[4096], sig_path[4096], used_url[URL_MAX];
        int fetched = obtain_index(tmpdir, index_path, sizeof(index_path), sig_path, sizeof(sig_path), used_url, sizeof(used_url));
        if (fetched < 0) {
            fprintf(stderr, "update: all mirrors unreachable\n");
            if (json) printf("{\"error\": \"unreachable\"}\n");
            rmdir(tmpdir);
            return 2;
        }
        if (verify_index_sig(index_path, sig_path) < 0) {
            fprintf(stderr, "update: index signature verification failed\n");
            if (json) printf("{\"error\": \"bad signature\"}\n");
            if (fetched) { unlink(index_path); unlink(sig_path); }
            rmdir(tmpdir);
            return 2;
        }
        struct index_latest latest;
        if (parse_index(index_path, &latest, "xtos-release-index") < 0) {
            fprintf(stderr, "update: index malformed\n");
            if (json) printf("{\"error\": \"malformed index\"}\n");
            if (fetched) { unlink(index_path); unlink(sig_path); }
            rmdir(tmpdir);
            return 2;
        }
        if (fetched) { unlink(index_path); unlink(sig_path); }
        rmdir(tmpdir);
        int c = version_cmp(local, latest.version);
        if (c == -2) { fprintf(stderr, "update: version compare failed\n"); return 2; }
        char arch[32];
        local_arch(arch, sizeof(arch));
        int up = (c >= 0);
        if (json) {
            printf("{\"up_to_date\": %s, \"signature\": \"verified\", \"installed_version\": \"%s\", "
                   "\"latest_version\": \"%s\", \"channel\": \"%s\", \"published\": \"%s\", \"arch\": \"%s\", "
                   "\"mirror\": \"%s\", \"notes_url\": \"%s\", \"artifacts\": [",
                   up ? "true" : "false", local, latest.version, latest.channel, latest.published, arch, used_url, latest.notes_url);
            int printed = 0;
            for (int i = 0; i < latest.narts; i++) {
                if (strcmp(latest.arts[i].arch, arch) != 0 && strcmp(latest.arts[i].arch, "all") != 0) continue;
                printf("%s{\"name\": \"%s\", \"kind\": \"%s\", \"size\": %ld, \"sha256\": \"%s\", \"url\": \"%s\"}",
                       printed ? ", " : "", latest.arts[i].name, latest.arts[i].kind,
                       latest.arts[i].size, latest.arts[i].sha256, latest.arts[i].url);
                printed++;
            }
            printf("]}\n");
        } else {
            xui_init();
            XUI_LABEL("Installed: "); printf("%s\n", local);
            XUI_LABEL("Latest:     "); printf("%s (%s, %s)\n", latest.version, latest.release_name, latest.published);
            XUI_LABEL("Signature: "); printf("verified (%s)\n", used_url);
            XUI_LABEL("Channel:   "); printf("%s\n", latest.channel);
            if (up) XUI_OK("up-to-date\n");
            else { XUI_WARN("update available\n"); XUI_DIM("https://xtos.cn/download/\n"); }
        }
        return up ? 0 : 1;
    }

    if (strcmp(cmd, "download") == 0 && (argc == argstart + 1 || argc == argstart + 3)) {
        const char *destdir = NULL;
        if (argc == argstart + 3) {
            if (strcmp(argv[argstart + 1], "--dest") != 0) { usage(); return 2; }
            destdir = argv[argstart + 2];
        } else {
            destdir = ".";
        }
        char tmpdir[] = "/tmp/xtos-update-XXXXXX";
        if (!mkdtemp(tmpdir)) { fprintf(stderr, "update: cannot create tmp dir\n"); return 2; }
        char index_path[4096], sig_path[4096], used_url[URL_MAX];
        int fetched = obtain_index(tmpdir, index_path, sizeof(index_path), sig_path, sizeof(sig_path), used_url, sizeof(used_url));
        if (fetched < 0) {
            fprintf(stderr, "update: all mirrors unreachable\n");
            if (json) printf("{\"error\": \"unreachable\"}\n");
            rmdir(tmpdir);
            return 2;
        }
        if (verify_index_sig(index_path, sig_path) < 0) {
            fprintf(stderr, "update: index signature verification failed\n");
            if (json) printf("{\"error\": \"bad signature\"}\n");
            if (fetched) { unlink(index_path); unlink(sig_path); }
            rmdir(tmpdir);
            return 2;
        }
        struct index_latest latest;
        if (parse_index(index_path, &latest, "xtos-release-index") < 0) {
            fprintf(stderr, "update: index malformed\n");
            if (fetched) { unlink(index_path); unlink(sig_path); }
            rmdir(tmpdir);
            return 2;
        }
        if (fetched) { unlink(index_path); unlink(sig_path); }
        rmdir(tmpdir);
        if (mkdir(destdir, 0755) < 0 && errno != EEXIST) { fprintf(stderr, "update: cannot create %s\n", destdir); return 2; }
        char arch[32];
        local_arch(arch, sizeof(arch));
        char part[4096], final[4096];
        char dlbuf[4096];
        struct artifact *got[ART_MAX];
        if (snprintf(dlbuf, sizeof(dlbuf), "%s/.dl-XXXXXX", destdir) >= (int)sizeof(dlbuf)) { return 2; }
        if (!mkdtemp(dlbuf)) { fprintf(stderr, "update: cannot create download staging dir\n"); return 2; }
        int done = 0;
        for (int i = 0; i < latest.narts; i++) {
            struct artifact *a = &latest.arts[i];
            if (strcmp(a->arch, arch) != 0 && strcmp(a->arch, "all") != 0) continue;
            if (strcmp(a->kind, "checksums") == 0 && a->sha256[0] == '\0') {
                /* checksums without hash still downloaded and counted */
            }
            if (snprintf(final, sizeof(final), "%s/%s", destdir, a->name) >= (int)sizeof(final)) continue;
            if (snprintf(part, sizeof(part), "%s.part", final) >= (int)sizeof(part)) continue;
            if (fetch_url(a->url, part, dlbuf) < 0) { unlink(part); fprintf(stderr, "update: download failed: %s\n", a->name); rmdir(dlbuf); return 2; }
            if (a->sha256[0]) {
                char hex[65];
                struct stat st;
                if (sha256_file(part, hex) < 0 || strcmp(hex, a->sha256) != 0) {
                    unlink(part);
                    fprintf(stderr, "update: hash mismatch: %s\n", a->name);
                    if (json) printf("{\"error\": \"hash mismatch\"}\n");
                    rmdir(dlbuf);
                    return 2;
                }
                if (a->size > 0 && stat(part, &st) == 0 && (long)st.st_size != a->size) {
                    unlink(part);
                    fprintf(stderr, "update: size mismatch: %s\n", a->name);
                    if (json) printf("{\"error\": \"size mismatch\"}\n");
                    rmdir(dlbuf);
                    return 2;
                }
            }
            if (rename(part, final) < 0) { unlink(part); rmdir(dlbuf); return 2; }
            if (!json) printf("%s\n", final);
            got[done] = a;
            done++;
        }
        rmdir(dlbuf);
        if (done > 0 && write_download_manifest(destdir, got, done, latest.version, latest.channel, arch) < 0) {
            fprintf(stderr, "update: cannot write download-manifest.json in %s\n", destdir);
            return 2;
        }
        if (json) printf("{\"downloaded\": %d, \"dest\": \"%s\", \"mirror\": \"%s\"}\n", done, destdir, used_url);
        return done > 0 ? 0 : 2;
    }

    if (strcmp(cmd, "apply") == 0 && argc == argstart + 7) {
        const char *from = NULL, *to = NULL, *token = NULL;
        for (int i = argstart + 1; i < argc; i += 2) {
            if (strcmp(argv[i], "--from") == 0) from = argv[i + 1];
            else if (strcmp(argv[i], "--to") == 0) to = argv[i + 1];
            else if (strcmp(argv[i], "--confirm") == 0) token = argv[i + 1];
            else { usage(); return 2; }
        }
        if (!from || !to || !token || strcmp(token, "ERASE-XTOS") != 0) {
            fprintf(stderr, "update: refusing: --confirm must be exactly ERASE-XTOS\n");
            return 2;
        }
        /* 1. The download manifest is the unit apply consumes. */
        char manifest[4096];
        if (snprintf(manifest, sizeof(manifest), "%s/download-manifest.json", from) >= (int)sizeof(manifest)) return 2;
        struct index_latest dl;
        if (parse_index(manifest, &dl, "xtos-download-manifest") < 0) {
            fprintf(stderr, "update: %s is not a download manifest; run `update download --dest %s` first\n",
                    manifest, from);
            return 2;
        }
        /* 2. Re-verify every artifact against the manifest before any device is opened. */
        struct artifact *iso = NULL;
        for (int i = 0; i < dl.narts; i++) {
            struct artifact *a = &dl.arts[i];
            char path[4096];
            struct stat st;
            if (snprintf(path, sizeof(path), "%s/%s", from, a->name) >= (int)sizeof(path)) return 2;
            if (stat(path, &st) < 0) { fprintf(stderr, "update: missing artifact %s\n", a->name); return 2; }
            if (a->sha256[0]) {
                char hex[65];
                if (sha256_file(path, hex) < 0 || strcmp(hex, a->sha256) != 0) {
                    fprintf(stderr, "update: corrupt artifact (hash mismatch): %s\n", a->name);
                    return 2;
                }
            }
            if (a->size > 0 && (long long)st.st_size != a->size) {
                fprintf(stderr, "update: artifact size mismatch: %s\n", a->name);
                return 2;
            }
            if (strcmp(a->kind, "installer-iso") == 0) iso = a;
        }
        if (!iso || iso->size <= 0 || !iso->sha256[0]) {
            fprintf(stderr, "update: no installer-iso artifact (with size and sha256) in %s\n", manifest);
            return 2;
        }
        /* 3. Target safety: canonical whole-disk path, not mounted, not swap. */
        char real[4096];
        if (!realpath(to, real)) { fprintf(stderr, "update: cannot resolve %s\n", to); return 2; }
        if (strcmp(real, to) != 0) { fprintf(stderr, "update: refusing alias path %s (use %s)\n", to, real); return 2; }
        if (!valid_whole_disk_path(real)) {
            fprintf(stderr, "update: refusing %s: target must be a canonical whole disk (/dev/sdX, /dev/nvmeXnY, /dev/loopN)\n", real);
            return 2;
        }
        struct stat dst;
        if (stat(real, &dst) < 0 || !S_ISBLK(dst.st_mode)) {
            fprintf(stderr, "update: refusing %s: not a block device\n", real);
            return 2;
        }
        char why[512];
        if (target_refusal_reason(real, why, sizeof(why))) {
            fprintf(stderr, "update: refusing %s: %s\n", real, why);
            return 2;
        }
        int tfd = open(real, O_RDONLY);
        unsigned long long cap = 0;
        if (tfd < 0 || ioctl(tfd, BLKGETSIZE64, &cap) < 0) {
            if (tfd >= 0) close(tfd);
            fprintf(stderr, "update: cannot determine the size of %s\n", real);
            return 2;
        }
        close(tfd);
        if (cap < (unsigned long long)iso->size) {
            fprintf(stderr, "update: refusing: %s (%llu bytes) is smaller than the image (%ld bytes)\n",
                    real, cap, iso->size);
            return 2;
        }
        /* 4. Write, then verify by reading back exactly the image extent. */
        char ipath[4096];
        if (snprintf(ipath, sizeof(ipath), "%s/%s", from, iso->name) >= (int)sizeof(ipath)) return 2;
        if (copy_iso_to_device(ipath, real, iso->size) < 0) {
            fprintf(stderr, "update: write to %s failed\n", real);
            return 2;
        }
        char hex[65];
        if (sha256_prefix(real, iso->size, hex) < 0 || strcmp(hex, iso->sha256) != 0) {
            fprintf(stderr, "update: read-back verification failed on %s\n", real);
            return 2;
        }
        if (json) {
            printf("{\"applied\": true, \"version\": \"%s\", \"target\": \"%s\", \"bytes\": %ld, \"sha256\": \"%s\"}\n",
                   dl.version, real, iso->size, iso->sha256);
        } else {
            xui_init();
            XUI_LABEL("Applied:  ");
            printf("%s installer -> %s (%ld bytes)\n", dl.version, real, iso->size);
            XUI_LABEL("Verify:   ");
            printf("read-back sha256 match (%s)\n", iso->sha256);
            XUI_DIM("Reboot from this medium to run the new version.\n"
                    "Labeled XTOS-PERSIST volumes are not touched by apply.\n");
        }
        return 0;
    }

    usage();
    return 2;
}
