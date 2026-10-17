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
 * Build: gcc -O2 -Wall -Wextra -Werror -static \
 *   -I tools/xtos-native/common -I tools/xtos-native/vendor/monocypher \
 *   -o update update.c \
 *   tools/xtos-native/vendor/monocypher/monocypher.c \
 *   tools/xtos-native/vendor/monocypher/monocypher-ed25519.c
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <unistd.h>
#include "xtos_ui.h"
#include "xtos_ed25519.h"

#define LINE_BUF 65536
#define URL_MAX 2048
#define NAME_MAX_LEN 256
#define VER_MAX_LEN 64
#define ART_MAX 64
#define MIRROR_MAX 8

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

static int find_in_path(char *out, size_t n, const char *tool) {
    const char *path = getenv("PATH");
    if (!path) return -1;
    const char *p = path;
    while (*p) {
        const char *colon = strchr(p, ':');
        size_t plen = colon ? (size_t)(colon - p) : strlen(p);
        if (plen && plen + strlen(tool) + 2 < n) {
            memcpy(out, p, plen);
            out[plen] = '/';
            strcpy(out + plen + 1, tool);
            if (access(out, X_OK) == 0) return 0;
        }
        if (!colon) break;
        p = colon + 1;
    }
    return -1;
}

static int run_capture_exit(char *const argv[], char *devnull_or_null) {
    (void)devnull_or_null;
    pid_t pid = fork();
    if (pid < 0) return -1;
    if (pid == 0) {
        int null = open("/dev/null", O_WRONLY);
        if (null >= 0) { dup2(null, STDOUT_FILENO); dup2(null, STDERR_FILENO); close(null); }
        execvp(argv[0], argv);
        _exit(127);
    }
    int status = 0;
    if (waitpid(pid, &status, 0) < 0) return -1;
    return (WIFEXITED(status) && WEXITSTATUS(status) == 0) ? 0 : -1;
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
static int fetch_url(const char *url, const char *dest, const char *tmpdir) {
    char curl[4096];
    if (find_in_path(curl, sizeof(curl), "curl") < 0) return -1;
    char tpath[4096];
    if (snprintf(tpath, sizeof(tpath), "%s/fetch-XXXXXX", tmpdir) >= (int)sizeof(tpath)) return -1;
    int fd = mkstemp(tpath);
    if (fd < 0) return -1;
    close(fd);
    char timeoutstr[16];
    snprintf(timeoutstr, sizeof(timeoutstr), "%d", g_timeout);
    char *argv[10] = {curl, "-fsS", "--max-time", timeoutstr, "-o", tpath, "--", (char *)url, NULL};
    if (strncmp(url, "http://", 7) != 0 && strncmp(url, "https://", 8) != 0) { unlink(tpath); return -1; }
    int rc = run_capture_exit(argv, NULL);
    if (rc == 0) {
        if (rename(tpath, dest) < 0) { unlink(tpath); return -1; }
        return 0;
    }
    unlink(tpath);
    return -1;
}

static int verify_index_sig(const char *index_path, const char *sig_path) {
    /* Native ed25519 check against the pinned key (vendored Monocypher).
     * No openssl binary is executed. */
    return xed25519_verify_file(g_pubkey, index_path, sig_path);
}

static int parse_index(const char *path, struct index_latest *latest) {
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
            if (!json_str(line, "kind", v, sizeof(v)) || strcmp(v, "xtos-release-index") != 0) { rc = -1; break; }
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
                else if (strstr(line, "\"size\"")) { char *p = strchr(line, ':'); if (!p) { rc = -1; break; } cur->size = atol(p + 1); }
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
        if (parse_index(index_path, &latest) < 0) {
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
        if (parse_index(index_path, &latest) < 0) {
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
        int done = 0;
        for (int i = 0; i < latest.narts; i++) {
            struct artifact *a = &latest.arts[i];
            if (strcmp(a->arch, arch) != 0 && strcmp(a->arch, "all") != 0) continue;
            if (strcmp(a->kind, "checksums") == 0 && a->sha256[0] == '\0') {
                /* checksums without hash still downloaded and counted */
            }
            if (snprintf(final, sizeof(final), "%s/%s", destdir, a->name) >= (int)sizeof(final)) continue;
            if (snprintf(part, sizeof(part), "%s.part", final) >= (int)sizeof(part)) continue;
            char curl[4096];
            if (find_in_path(curl, sizeof(curl), "curl") < 0) { fprintf(stderr, "update: curl not found\n"); return 2; }
            char timeoutstr[16];
            long big = 3600;
            snprintf(timeoutstr, sizeof(timeoutstr), "%ld", big);
            char *argv2[10] = {curl, "-fsS", "--max-time", timeoutstr, "-o", part, "--", a->url, NULL};
            if (run_capture_exit(argv2, NULL) < 0) { unlink(part); fprintf(stderr, "update: download failed: %s\n", a->name); return 2; }
            if (a->sha256[0]) {
                char hex[65];
                struct stat st;
                if (sha256_file(part, hex) < 0 || strcmp(hex, a->sha256) != 0) {
                    unlink(part);
                    fprintf(stderr, "update: hash mismatch: %s\n", a->name);
                    if (json) printf("{\"error\": \"hash mismatch\"}\n");
                    return 2;
                }
                if (a->size > 0 && stat(part, &st) == 0 && (long)st.st_size != a->size) {
                    unlink(part);
                    fprintf(stderr, "update: size mismatch: %s\n", a->name);
                    if (json) printf("{\"error\": \"size mismatch\"}\n");
                    return 2;
                }
            }
            if (rename(part, final) < 0) { unlink(part); return 2; }
            if (!json) printf("%s\n", final);
            done++;
        }
        if (json) printf("{\"downloaded\": %d, \"dest\": \"%s\", \"mirror\": \"%s\"}\n", done, destdir, used_url);
        return done > 0 ? 0 : 2;
    }

    usage();
    return 2;
}
