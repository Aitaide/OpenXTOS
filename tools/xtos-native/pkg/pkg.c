/* pkg: XTOS native package inventory + integrity verification (C, static).
 * Reads /usr/share/xtos/userspace-manifest.json. Read-only queries plus
 * sha256 verification of installed files against the manifest, and xpkg
 * application package install/remove/list/verify/info (pkg app ...).
 * Build: gcc -O2 -Wall -Wextra -Werror -static -o pkg pkg.c
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <ctype.h>
#include <stdint.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/stat.h>
#include <sys/types.h>
#include "xtos_ui.h"
#include "xtos_gzip.h"
#include "xtos_ustar.h"

/* Minimal SHA-256 (public-domain style implementation) */
typedef struct { uint32_t state[8]; uint64_t bitlen; uint8_t data[64]; uint32_t datalen; } sha256_ctx;

static void sha256_transform(sha256_ctx *ctx, const uint8_t data[]) {
    static const uint32_t k[64] = {
        0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
        0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
        0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
        0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
        0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
        0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
        0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
        0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
    uint32_t m[64], a,b,c,d,e,f,g,h,t1,t2;
    for (int i = 0; i < 16; i++)
        m[i] = (data[i*4]<<24)|(data[i*4+1]<<16)|(data[i*4+2]<<8)|data[i*4+3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ((m[i-15]>>7)|(m[i-15]<<25)) ^ ((m[i-15]>>18)|(m[i-15]<<14)) ^ (m[i-15]>>3);
        uint32_t s1 = ((m[i-2]>>17)|(m[i-2]<<15)) ^ ((m[i-2]>>19)|(m[i-2]<<13)) ^ (m[i-2]>>10);
        m[i] = m[i-16] + s0 + m[i-7] + s1;
    }
    a=ctx->state[0];b=ctx->state[1];c=ctx->state[2];d=ctx->state[3];
    e=ctx->state[4];f=ctx->state[5];g=ctx->state[6];h=ctx->state[7];
    for (int i = 0; i < 64; i++) {
        uint32_t S1 = ((e>>6)|(e<<26)) ^ ((e>>11)|(e<<21)) ^ ((e>>25)|(e<<7));
        uint32_t ch = (e & f) ^ (~e & g);
        t1 = h + S1 + ch + k[i] + m[i];
        uint32_t S0 = ((a>>2)|(a<<30)) ^ ((a>>13)|(a<<19)) ^ ((a>>22)|(a<<10));
        uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
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

static void sha256_update(sha256_ctx *ctx, const uint8_t *data, size_t len) {
    for (size_t i = 0; i < len; i++) {
        ctx->data[ctx->datalen] = data[i]; ctx->datalen++;
        if (ctx->datalen == 64) {
            sha256_transform(ctx, ctx->data);
            ctx->bitlen += 512;
            ctx->datalen = 0;
        }
    }
}

static void sha256_final(sha256_ctx *ctx, uint8_t hash[]) {
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
    uint8_t buf[65536];
    ssize_t n;
    while ((n = read(fd, buf, sizeof(buf))) > 0)
        sha256_update(&ctx, buf, (size_t)n);
    close(fd);
    if (n < 0) return -1;
    uint8_t hash[32];
    sha256_final(&ctx, hash);
    for (int i = 0; i < 32; i++)
        sprintf(outhex + i*2, "%02x", hash[i]);
    outhex[64] = '\0';
    return 0;
}

/* ---- Manifest model ---- */
#define MANIFEST_PATH "/usr/share/xtos/userspace-manifest.json"
#define LINE_BUF 65536

struct manifest_file {
    char path[512];
    char sha256[72];
    long size;
    int type; /* 0=file 1=directory */
};

struct manifest_pkg {
    char name[256];
    char version[128];
    char arch[32];
    char sha256[72];
    char source_url[512];
    long deb_size;
    int file_start; /* index into pool */
    int nfiles;
};

#define FILE_POOL_MAX 262144
static struct manifest_file file_pool[FILE_POOL_MAX];
static int pool_used = 0;
static struct manifest_pkg pkgs[512];
static int npkgs = 0;
static char g_arch[32] = "";
static char g_lock_sha[128] = "";
static char g_overrides[64][512];
static int g_n_overrides = 0;

static void load_overrides(void) {
    FILE *f = fopen("/usr/share/xtos/pkg-overrides.txt", "r");
    if (!f) return;
    char line[512];
    while (g_n_overrides < 64 && fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\n")] = '\0';
        if (line[0] == '/') {
            snprintf(g_overrides[g_n_overrides], 512, "%s", line);
            g_n_overrides++;
        }
    }
    fclose(f);
}

static int is_overridden(const char *path) {
    for (int i = 0; i < g_n_overrides; i++)
        if (strcmp(g_overrides[i], path) == 0) return 1;
    return 0;
}

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
    /* decode \uXXXX escapes to UTF-8 (BMP only) */
    size_t o = 0;
    for (size_t i = 0; i < len && o + 4 < outsize; ) {
        if (p[i] == '\\' && p[i+1] == 'u' && i + 6 <= len) {
            unsigned cp = 0;
            int valid = 1;
            for (int k = 0; k < 4; k++) {
                char c = p[i + 2 + k];
                cp <<= 4;
                if (c >= '0' && c <= '9') cp |= (unsigned)(c - '0');
                else if (c >= 'a' && c <= 'f') cp |= (unsigned)(c - 'a' + 10);
                else if (c >= 'A' && c <= 'F') cp |= (unsigned)(c - 'A' + 10);
                else { valid = 0; break; }
            }
            if (valid && cp < 0x80) {
                out[o++] = (char)cp;
            } else if (valid && cp < 0x800) {
                out[o++] = (char)(0xC0 | (cp >> 6));
                out[o++] = (char)(0x80 | (cp & 0x3F));
            } else if (valid) {
                out[o++] = (char)(0xE0 | (cp >> 12));
                out[o++] = (char)(0x80 | ((cp >> 6) & 0x3F));
                out[o++] = (char)(0x80 | (cp & 0x3F));
            } else {
                out[o++] = p[i];
            }
            i += valid ? 6 : 1;
        } else {
            out[o++] = p[i++];
        }
    }
    out[o] = '\0';
    return 1;
}

/* Parse the manifest: only the TOP-LEVEL packages array (skip lock.packages).
 * Manifest layout (indent=2, sort_keys):
 *   lock.packages[] entries have "name" but NO "files"; top-level entries have
 *   "files" then "package". We track depth by brace counting to distinguish. */
static int load_manifest(void) {
    FILE *f = fopen(MANIFEST_PATH, "r");
    if (!f) return -1;
    char line[LINE_BUF];
    int depth = 0;
    int in_lock = 0, lock_depth = -1;
    int in_toppkg = 0, in_files = 0, in_pkgmeta = 0;
    struct manifest_pkg *cur = NULL;
    struct manifest_file *cfile = NULL;

    while (fgets(line, sizeof(line), f)) {
        /* track brace depth */
        for (char *c = line; *c; c++) {
            if (*c == '{') depth++;
            else if (*c == '}') depth--;
        }
        if (strstr(line, "\"lock\"")) { in_lock = 1; lock_depth = depth; continue; }
        if (in_lock && depth < lock_depth) { in_lock = 0; }

        if (!in_lock && strstr(line, "\"packages\"")) {
            in_toppkg = 1;
            cur = NULL;
            continue;
        }
        if (!in_toppkg) {
            if (!g_arch[0]) json_str(line, "architecture", g_arch, sizeof(g_arch));
            if (!g_lock_sha[0]) json_str(line, "lock_sha256", g_lock_sha, sizeof(g_lock_sha));
            continue;
        }

        if (strstr(line, "\"files\"")) {
            /* each top-level entry starts with its files array */
            if (npkgs < 512) {
                cur = &pkgs[npkgs++];
                cur->file_start = pool_used;
                cur->nfiles = 0;
            }
            in_files = 1; in_pkgmeta = 0; cfile = NULL;
            continue;
        }
        if (strstr(line, "\"package\"")) { in_pkgmeta = 1; in_files = 0; continue; }

        if (in_files) {
            if (strstr(line, "\"path\"") && cur) {
                if (pool_used < FILE_POOL_MAX) {
                    cfile = &file_pool[pool_used++];
                    memset(cfile, 0, sizeof(*cfile));
                    json_str(line, "path", cfile->path, sizeof(cfile->path));
                    cur->nfiles++;
                } else {
                    cfile = NULL; /* pool full: stop recording this package's files */
                }
            } else if (strstr(line, "\"type\"") && cfile) {
                char t[32] = "";
                json_str(line, "type", t, sizeof(t));
                cfile->type = strcmp(t, "directory") == 0 ? 1 : (strcmp(t, "symlink") == 0 ? 2 : 0);
            } else if (strstr(line, "\"sha256\"") && cfile) {
                json_str(line, "sha256", cfile->sha256, sizeof(cfile->sha256));
            } else if (strstr(line, "\"size\"") && cfile) {
                char *p = strchr(line, ':');
                if (p) cfile->size = atol(p + 1);
            } else if (strchr(line, ']') && !strchr(line, '[') && strstr(line, "      ]")) {
                in_files = 0;
            }
        }
        if (in_pkgmeta && cur) {
            if (strstr(line, "\"name\"")) {
                json_str(line, "name", cur->name, sizeof(cur->name));
            }
            else if (strstr(line, "\"version\"") && cur)
                json_str(line, "version", cur->version, sizeof(cur->version));
            else if (strstr(line, "\"architecture\"") && cur)
                json_str(line, "architecture", cur->arch, sizeof(cur->arch));
            else if (strstr(line, "\"sha256\"") && cur)
                json_str(line, "sha256", cur->sha256, sizeof(cur->sha256));
            else if (strstr(line, "\"source_url\"") && cur)
                json_str(line, "source_url", cur->source_url, sizeof(cur->source_url));
            else if (strstr(line, "\"size\"") && cur) {
                char *p = strchr(line, ':');
                if (p) cur->deb_size = atol(p + 1);
            }
            else if (strchr(line, '}') && strstr(line, "      }")) {
                in_pkgmeta = 0;
            }
        }
    }
    fclose(f);
    return npkgs > 0 ? 0 : -1;
}

/* ---- xpkg application packages (pkg app ...) ----
 * xpkg v1: tar.gz with payload/ + xpkg/{manifest.json,SHA256SUMS}.
 * Security model: listing-first validation (no traversal, no shadow prefixes),
 * post-extract type scan (regular files only, nlink==1, no setuid), then
 * full sha256 verification before anything is moved into place. */
#define APP_ROOT_DEFAULT "/opt/xtos-apps"
#define APP_REGISTRY "registry.json"
#define APP_NAME_MAX 64
#define APP_VER_MAX 64
#define APP_PATH_MAX 1024
#define APP_MAX_ENTRIES 65536
#define APP_TAR_LIST_MAX (16 * 1024 * 1024)
#define APP_MANIFEST_MAX (1024 * 1024)
#define APP_REGISTRY_MAX 256

#include <dirent.h>
#include <sys/file.h>
#include <sys/wait.h>
#include <time.h>

struct app_entry {
    char path[APP_PATH_MAX];
    char sha256[72];
    long size;
};

struct app_meta {
    char name[APP_NAME_MAX + 1];
    char version[APP_VER_MAX + 1];
    char architecture[32];
    char summary[256];
    char license[64];
    char vendor[128];
    char xtos_min_version[64];
};

struct app_reg {
    char name[APP_NAME_MAX + 1];
    char version[APP_VER_MAX + 1];
    char xpkg_sha256[72];
    char installed_utc[24];
    long files;
    long bytes;
};

static int app_copy(char *out, size_t n, const char *s) {
    size_t len = strlen(s);
    if (!len || len + 1 > n) return -1;
    memcpy(out, s, len + 1);
    return 0;
}

static int app_join2(char *out, size_t n, const char *a, const char *b) {
    size_t la = strlen(a), lb = strlen(b);
    if (!la || !lb || la + lb + 2 > n) return -1;
    memcpy(out, a, la);
    out[la] = '/';
    memcpy(out + la + 1, b, lb + 1);
    return 0;
}

static int app_join3(char *out, size_t n, const char *a, const char *b, const char *c) {
    size_t la = strlen(a), lb = strlen(b), lc = strlen(c);
    if (!la || !lb || !lc || la + lb + lc + 3 > n) return -1;
    memcpy(out, a, la);
    out[la] = '/';
    memcpy(out + la + 1, b, lb);
    out[la + lb + 1] = '/';
    memcpy(out + la + lb + 2, c, lc + 1);
    return 0;
}

static const char *app_root(char *buf, size_t n) {
    const char *env = getenv("XTOS_APP_ROOT");
    if (env && env[0] == '/' && app_copy(buf, n, env) == 0) return buf;
    if (app_copy(buf, n, APP_ROOT_DEFAULT) != 0) buf[0] = '\0';
    return buf;
}

static int app_token_name(const char *s) {
    size_t len = strlen(s);
    if (!len || len > APP_NAME_MAX) return 0;
    if (!(islower((unsigned char)s[0]) || isdigit((unsigned char)s[0]))) return 0;
    for (size_t i = 0; i < len; i++)
        if (!(islower((unsigned char)s[i]) || isdigit((unsigned char)s[i]) || s[i] == '-')) return 0;
    return 1;
}

static int app_token_version(const char *s) {
    size_t len = strlen(s);
    if (!len || len > APP_VER_MAX) return 0;
    for (size_t i = 0; i < len; i++) {
        char c = s[i];
        if (!(isalnum((unsigned char)c) || c == '.' || c == '+' || c == '~' || c == '-')) return 0;
    }
    return 1;
}

/* Archive member names: only payload/ or xpkg/ roots, sane components, bounded. */
static int app_member_ok(const char *name) {
    size_t len = strlen(name);
    if (!len || len >= APP_PATH_MAX) return 0;
    if (name[0] == '/') return 0;
    if (!(strncmp(name, "payload/", 8) == 0 || strncmp(name, "xpkg/", 5) == 0)) return 0;
    if (name[len - 1] == '/') len--; /* directory member */
    const char *p = name;
    while (p < name + len) {
        const char *slash = memchr(p, '/', (size_t)(name + len - p));
        size_t clen = slash ? (size_t)(slash - p) : (size_t)(name + len - p);
        if (clen == 0 || clen >= 256) return 0;
        if (p[0] == '.' && (clen == 1 || (clen == 2 && p[1] == '.'))) return 0;
        for (size_t i = 0; i < clen; i++)
            if ((unsigned char)p[i] < 32) return 0;
        p += clen + (slash ? 1 : 0);
    }
    return 1;
}

struct app_anc {
    const char *s;
    size_t len;
};

static int app_cmp_anc(const void *a, const void *b) {
    const struct app_anc *x = a, *y = b;
    size_t m = x->len < y->len ? x->len : y->len;
    int c = memcmp(x->s, y->s, m);
    if (c) return c;
    return x->len < y->len ? -1 : (x->len > y->len ? 1 : 0);
}

static int app_anc_contains(const struct app_anc *sorted, int n, const char *name) {
    size_t nlen = strlen(name);
    int lo = 0, hi = n - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        size_t m = nlen < sorted[mid].len ? nlen : sorted[mid].len;
        int c = memcmp(name, sorted[mid].s, m);
        if (!c) {
            if (nlen == sorted[mid].len) return 1;
            c = nlen < sorted[mid].len ? -1 : 1;
        }
        if (c < 0) hi = mid - 1; else lo = mid + 1;
    }
    return 0;
}

/* Listing validation: every name sane; no entry may double as a directory
 * prefix (blocks symlink/hardlink shadows used for extraction escapes). */
static int app_listing_validate(const char *list, size_t len) {
    if (!len || len > APP_TAR_LIST_MAX) return -1;
    char *pool = malloc(len + 1);
    if (!pool) return -1;
    memcpy(pool, list, len);
    pool[len] = '\0';
    char **names = calloc(APP_MAX_ENTRIES + 1, sizeof(char *));
    struct app_anc *anc = malloc(256 * sizeof(struct app_anc));
    int n_anc = 0, cap_anc = 256, count = 0, bad = 1;
    if (!names || !anc) goto out;
    bad = 0;
    char *cursor = pool;
    while (cursor < pool + len && !bad) {
        char *nl = strchr(cursor, '\n');
        if (nl) *nl = '\0';
        if (*cursor) {
            if (!app_member_ok(cursor) || count >= APP_MAX_ENTRIES) { bad = 1; break; }
            names[count++] = cursor;
            for (char *s = strchr(cursor, '/'); s; s = strchr(s + 1, '/')) {
                if (n_anc >= cap_anc) {
                    if (cap_anc >= 4 * APP_MAX_ENTRIES) { bad = 1; break; }
                    cap_anc *= 2;
                    struct app_anc *grown = realloc(anc, (size_t)cap_anc * sizeof(struct app_anc));
                    if (!grown) { bad = 1; break; }
                    anc = grown;
                }
                anc[n_anc].s = cursor;
                anc[n_anc].len = (size_t)(s - cursor);
                n_anc++;
            }
        }
        if (!nl) break;
        cursor = nl + 1;
    }
    if (!bad && count == 0) bad = 1;
    if (!bad) {
        qsort(anc, (size_t)n_anc, sizeof(struct app_anc), app_cmp_anc);
        for (int i = 0; i < count; i++)
            if (app_anc_contains(anc, n_anc, names[i])) { bad = 1; break; }
    }
out:
    free(pool);
    free(names);
    free(anc);
    return bad ? -1 : 0;
}

static int app_cmp_entries(const void *a, const void *b) {
    return strcmp(((const struct app_entry *)a)->path, ((const struct app_entry *)b)->path);
}

/* Post-extract scan: regular files only, single link, no special mode bits. */
static int app_scan_tree(const char *rootdir, const char *rel,
                          struct app_entry **out, int *count, int *cap, int depth) {
    if (depth > 64) return -1;
    char full[8192];
    if (*rel ? app_join2(full, sizeof(full), rootdir, rel) < 0
             : app_copy(full, sizeof(full), rootdir) < 0) return -1;
    DIR *dir = opendir(full);
    if (!dir) return -1;
    struct dirent *de;
    int rc = 0;
    while ((de = readdir(dir))) {
        if (strcmp(de->d_name, ".") == 0 || strcmp(de->d_name, "..") == 0) continue;
        char child[APP_PATH_MAX];
        if (*rel ? app_join2(child, sizeof(child), rel, de->d_name) < 0
                 : app_copy(child, sizeof(child), de->d_name) < 0) { rc = -1; break; }
        char childfull[8192];
        if (app_join2(childfull, sizeof(childfull), full, de->d_name) < 0) { rc = -1; break; }
        struct stat st;
        if (lstat(childfull, &st) < 0) { rc = -1; break; }
        if (S_ISDIR(st.st_mode)) {
            if (app_scan_tree(rootdir, child, out, count, cap, depth + 1) < 0) { rc = -1; break; }
        } else if (S_ISREG(st.st_mode)) {
            if (st.st_nlink != 1 || (st.st_mode & 07000)) { rc = -1; break; }
            if (*count >= APP_MAX_ENTRIES) { rc = -1; break; }
            if (*count >= *cap) {
                int newcap = *cap ? *cap * 2 : 64;
                struct app_entry *grown = realloc(*out, (size_t)newcap * sizeof(struct app_entry));
                if (!grown) { rc = -1; break; }
                *out = grown;
                *cap = newcap;
            }
            if (app_copy((*out)[*count].path, APP_PATH_MAX, child) < 0) { rc = -1; break; }
            (*out)[*count].sha256[0] = '\0';
            (*out)[*count].size = (long)st.st_size;
            (*count)++;
        } else {
            rc = -1; /* symlink, fifo, socket, device: forbidden */
            break;
        }
    }
    closedir(dir);
    return rc;
}

static int app_hex64(const char *s) {
    for (int i = 0; i < 64; i++)
        if (!isxdigit((unsigned char)s[i])) return 0;
    return 1;
}

/* Parse xpkg/SHA256SUMS: "<64 hex>  <path>" (sha256sum compatible). */
static int app_parse_sums(const char *path, struct app_entry **out, int *count, int *cap) {
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    char line[APP_PATH_MAX + 128];
    int rc = 0;
    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\n")] = '\0';
        if (line[64] != ' ' || line[65] != ' ' || !app_hex64(line)) { rc = -1; break; }
        const char *p = line + 66;
        if (strncmp(p, "payload/", 8) != 0 || !app_member_ok(p)) { rc = -1; break; }
        if (*count >= *cap) {
            int newcap = *cap ? *cap * 2 : 64;
            struct app_entry *grown = realloc(*out, (size_t)newcap * sizeof(struct app_entry));
            if (!grown) { rc = -1; break; }
            *out = grown;
            *cap = newcap;
        }
        memcpy((*out)[*count].sha256, line, 64);
        (*out)[*count].sha256[64] = '\0';
        if (app_copy((*out)[*count].path, APP_PATH_MAX, p) < 0) { rc = -1; break; }
        (*out)[*count].size = -1;
        (*count)++;
    }
    fclose(f);
    if (rc == 0 && *count == 0) rc = -1;
    return rc;
}

/* Parse xpkg/manifest.json (pretty-printed, one key per line, as produced by
 * the xpkg tooling and enforced by the publish gate). */
static int app_parse_manifest(const char *path, struct app_meta *meta,
                              struct app_entry **out, int *count, int *cap) {
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    char line[LINE_BUF];
    int in_files = 0;
    struct app_entry *cfile = NULL;
    int rc = 0;
    memset(meta, 0, sizeof(*meta));
    while (fgets(line, sizeof(line), f)) {
        if (strstr(line, "\"files\"")) { in_files = 1; cfile = NULL; continue; }
        if (in_files) {
            if (strstr(line, "\"path\"")) {
                if (*count >= *cap) {
                    int newcap = *cap ? *cap * 2 : 64;
                    struct app_entry *grown = realloc(*out, (size_t)newcap * sizeof(struct app_entry));
                    if (!grown) { rc = -1; break; }
                    *out = grown;
                    *cap = newcap;
                }
                cfile = &(*out)[(*count)++];
                cfile->path[0] = '\0';
                cfile->sha256[0] = '\0';
                cfile->size = -1;
                if (!json_str(line, "path", cfile->path, sizeof(cfile->path))) { rc = -1; break; }
            } else if (strstr(line, "\"sha256\"") && cfile) {
                if (!json_str(line, "sha256", cfile->sha256, sizeof(cfile->sha256))) { rc = -1; break; }
            } else if (strstr(line, "\"size\"") && cfile) {
                char *p = strchr(line, ':');
                if (!p) { rc = -1; break; }
                cfile->size = atol(p + 1);
            } else if (strstr(line, "]")) {
                in_files = 0;
            }
            continue;
        }
        if (!meta->name[0]) json_str(line, "name", meta->name, sizeof(meta->name));
        if (!meta->version[0]) json_str(line, "version", meta->version, sizeof(meta->version));
        if (!meta->architecture[0]) json_str(line, "architecture", meta->architecture, sizeof(meta->architecture));
        if (!meta->summary[0]) json_str(line, "summary", meta->summary, sizeof(meta->summary));
        if (!meta->license[0]) json_str(line, "license", meta->license, sizeof(meta->license));
        if (!meta->vendor[0]) json_str(line, "vendor", meta->vendor, sizeof(meta->vendor));
        if (!meta->xtos_min_version[0]) json_str(line, "xtos_min_version", meta->xtos_min_version, sizeof(meta->xtos_min_version));
    }
    fclose(f);
    if (rc == 0 && (!meta->name[0] || !meta->version[0] || *count == 0)) rc = -1;
    return rc;
}

static void usage(void) {
    fprintf(stderr,
        "usage: pkg <command>\n"
        "  pkg list [filter]       list installed packages\n"
        "  pkg show <name>         package details (version, files, size, source)\n"
        "  pkg files <name>        list files owned by a package\n"
        "  pkg verify <name>       verify installed files against manifest sha256\n"
        "  pkg verify --all        verify every package (full integrity check)\n"
        "  pkg size                total installed payload size\n"
        "  pkg summary             count + lock identity\n"
        "  pkg app install <file.xpkg>  verify and install an xpkg application\n"
        "  pkg app upgrade <file.xpkg>  verify and upgrade an installed application\n"
        "  pkg app remove <name>        remove an installed application\n"
        "  pkg app list                list installed applications\n"
        "  pkg app verify <name>        re-verify installed application files\n"
        "  pkg app info <name>          show application manifest details\n"
        "  --json                  machine-readable output\n");
}

static struct manifest_pkg *find_pkg(const char *name) {
    for (int i = 0; i < npkgs; i++)
        if (strcmp(pkgs[i].name, name) == 0) return &pkgs[i];
    return NULL;
}

static int cmd_list(int json, const char *filter) {
    xui_init();
    if (!json) printf("%s%-32s %-28s %-8s%s %s\n", XUI_LABEL_S, "NAME", "VERSION", "ARCH", XUI_RESET_S, "FILES");
    else printf("[\n");
    int count = 0;
    for (int i = 0; i < npkgs; i++) {
        if (filter && !strstr(pkgs[i].name, filter)) continue;
        if (json)
            printf("%s  {\"name\": \"%s\", \"version\": \"%s\", \"arch\": \"%s\", \"files\": %d, \"deb_bytes\": %ld}",
                   count ? ",\n" : "", pkgs[i].name, pkgs[i].version, pkgs[i].arch,
                   pkgs[i].nfiles, pkgs[i].deb_size);
        else
            printf("%-32s %-28s %-8s %d\n", pkgs[i].name, pkgs[i].version, pkgs[i].arch, pkgs[i].nfiles);
        count++;
    }
    if (json) printf("\n]\n");
    else if (!count && filter) printf("  (no packages matching \"%s\")\n", filter);
    return 0;
}

static int cmd_show(int json, const char *name) {
    struct manifest_pkg *p = find_pkg(name);
    if (!p) {
        fprintf(stderr, "pkg: package %s not found\n", name);
        return 1;
    }
    long total = 0;
    int nfiles = 0;
    for (int i = 0; i < p->nfiles; i++)
        if (file_pool[p->file_start + i].type == 0) { total += file_pool[p->file_start + i].size; nfiles++; }
    if (json) {
        printf("{\n");
        printf("  \"name\": \"%s\",\n", p->name);
        printf("  \"version\": \"%s\",\n", p->version);
        printf("  \"architecture\": \"%s\",\n", p->arch);
        printf("  \"deb_sha256\": \"%s\",\n", p->sha256);
        printf("  \"deb_bytes\": %ld,\n", p->deb_size);
        printf("  \"installed_files\": %d,\n", nfiles);
        printf("  \"installed_bytes\": %ld,\n", total);
        printf("  \"source_url\": \"%s\"\n", p->source_url);
        printf("}\n");
    } else {
        xui_init();
        XUI_LABEL("Package:   "); printf("%s\n", p->name);
        XUI_LABEL("Version:   "); printf("%s\n", p->version);
        XUI_LABEL("Arch:      "); printf("%s\n", p->arch);
        XUI_LABEL("Deb size:  "); printf("%ld bytes (sha256 %s)\n", p->deb_size, p->sha256);
        XUI_LABEL("Installed: "); printf("%d files, %ld bytes (%.1f MB)\n", nfiles, total, total / 1048576.0);
        XUI_LABEL("Source:    "); printf("%s\n", p->source_url);
    }
    return 0;
}

static int cmd_files(const char *name) {
    struct manifest_pkg *p = find_pkg(name);
    if (!p) {
        fprintf(stderr, "pkg: package %s not found\n", name);
        return 1;
    }
    for (int i = 0; i < p->nfiles; i++)
        printf("%c %10ld  %s\n", file_pool[p->file_start + i].type == 1 ? 'd' : '-',
               file_pool[p->file_start + i].size, file_pool[p->file_start + i].path);
    return 0;
}

static int cmd_verify(const char *name, int json) {
    int start = 0, end = npkgs;
    struct manifest_pkg *one = NULL;
    if (strcmp(name, "--all") != 0) {
        one = find_pkg(name);
        if (!one) {
            fprintf(stderr, "pkg: package %s not found\n", name);
            return 1;
        }
    }
    int checked = 0, missing = 0, corrupt = 0, overridden = 0;
    if (one) {
        for (int i = 0; i < one->nfiles; i++) {
            struct manifest_file *fl = &file_pool[one->file_start + i];
            if (fl->type != 0) continue;
            char hex[65];
            checked++;
            if (sha256_file(fl->path, hex) < 0) { missing++; printf("MISSING %s\n", fl->path); continue; }
            if (strcmp(hex, fl->sha256) != 0) {
                if (is_overridden(fl->path)) { overridden++; }
                else { corrupt++; printf("CORRUPT %s\n", fl->path); }
            }
        }
    } else {
        for (int pi = start; pi < end; pi++) {
            for (int i = 0; i < pkgs[pi].nfiles; i++) {
                struct manifest_file *fl = &file_pool[pkgs[pi].file_start + i];
                if (fl->type != 0) continue;
                char hex[65];
                checked++;
                if (sha256_file(fl->path, hex) < 0) { missing++; printf("MISSING %s\n", fl->path); continue; }
                if (strcmp(hex, fl->sha256) != 0) {
                    if (is_overridden(fl->path)) { overridden++; }
                    else { corrupt++; printf("CORRUPT %s\n", fl->path); }
                }
            }
        }
    }
    if (json)
        printf("{\"checked\": %d, \"missing\": %d, \"corrupt\": %d, \"overridden\": %d, \"ok\": %d}\n",
               checked, missing, corrupt, overridden, checked - missing - corrupt - overridden);
    else {
        xui_init();
        printf("%s%s%s checked=%d ok=", XUI_LABEL_S, "PKG_VERIFY", XUI_RESET_S, checked);
        if (missing + corrupt == 0) XUI_OK("%d", checked - missing - corrupt - overridden);
        else XUI_BAD("%d", checked - missing - corrupt - overridden);
        printf(" missing=%d corrupt=%d overridden=%d\n", missing, corrupt, overridden);
    }
    return (missing + corrupt) > 0 ? 1 : 0;
}

static int cmd_size(void) {
    long total = 0; long debs = 0;
    int files = 0;
    for (int i = 0; i < npkgs; i++) {
        debs += pkgs[i].deb_size;
        for (int j = 0; j < pkgs[i].nfiles; j++)
            if (file_pool[pkgs[i].file_start + j].type == 0) { total += file_pool[pkgs[i].file_start + j].size; files++; }
    }
    printf("XTOS_PKG_SIZE packages=%d installed_files=%d installed_bytes=%ld (%.1f MB) deb_bytes=%ld (%.1f MB)\n",
           npkgs, files, total, total / 1048576.0, debs, debs / 1048576.0);
    return 0;
}

static int cmd_summary(void) {
    xui_init();
    long total = 0;
    int files = 0;
    for (int i = 0; i < npkgs; i++)
        for (int j = 0; j < pkgs[i].nfiles; j++)
            if (file_pool[pkgs[i].file_start + j].type == 0) { total += file_pool[pkgs[i].file_start + j].size; files++; }
    XUI_TITLE("XTOS Package Summary\n");
    XUI_LABEL("Architecture:  "); printf("%s\n", g_arch);
    XUI_LABEL("Packages:      "); printf("%d\n", npkgs);
    XUI_LABEL("Files:         "); printf("%d (%.1f MB installed)\n", files, total / 1048576.0);
    XUI_LABEL("Lock SHA256:   "); XUI_DIM("%s\n", g_lock_sha);
    XUI_LABEL("Source:        "); XUI_DIM("%s\n", MANIFEST_PATH);
    return 0;
}

/* ---- app registry (approot/registry.json, flock on registry.lock) ---- */
static int app_registry_load(const char *approot, struct app_reg *regs, int max) {
    char path[8192];
    if (app_join2(path, sizeof(path), approot, APP_REGISTRY) < 0) return -1;
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    char line[LINE_BUF];
    int n = 0, in_apps = 0, cur = -1, bad = 0;
    while (fgets(line, sizeof(line), f)) {
        if (strstr(line, "\"schema_version\"")) {
            char *p = strchr(line, ':');
            if (!p || strtol(p + 1, NULL, 10) != 1) { bad = 1; break; }
            continue;
        }
        if (strstr(line, "\"apps\"")) { in_apps = 1; continue; }
        if (!in_apps) continue;
        const char *q1 = strchr(line, '"');
        const char *q2 = q1 ? strchr(q1 + 1, '"') : NULL;
        if (q1 && q2 && strstr(line, "{")) {
            if (n >= max) { bad = 1; break; }
            size_t len = (size_t)(q2 - q1 - 1);
            if (!len || len > APP_NAME_MAX) { bad = 1; break; }
            cur = n++;
            memcpy(regs[cur].name, q1 + 1, len);
            regs[cur].name[len] = '\0';
            regs[cur].version[0] = '\0';
            regs[cur].xpkg_sha256[0] = '\0';
            regs[cur].installed_utc[0] = '\0';
            regs[cur].files = 0;
            regs[cur].bytes = 0;
            continue;
        }
        if (cur >= 0) {
            if (strstr(line, "\"version\"")) json_str(line, "version", regs[cur].version, sizeof(regs[cur].version));
            else if (strstr(line, "\"xpkg_sha256\"")) json_str(line, "xpkg_sha256", regs[cur].xpkg_sha256, sizeof(regs[cur].xpkg_sha256));
            else if (strstr(line, "\"installed_utc\"")) json_str(line, "installed_utc", regs[cur].installed_utc, sizeof(regs[cur].installed_utc));
            else if (strstr(line, "\"files\"")) { char *p = strchr(line, ':'); if (p) regs[cur].files = atol(p + 1); }
            else if (strstr(line, "\"bytes\"")) { char *p = strchr(line, ':'); if (p) regs[cur].bytes = atol(p + 1); }
            else if (strchr(line, '}')) cur = -1;
        } else if (strchr(line, '}')) {
            in_apps = 0;
        }
    }
    fclose(f);
    if (bad) return -1;
    for (int i = 0; i < n; i++)
        if (!regs[i].version[0] || !regs[i].xpkg_sha256[0]) return -1;
    return n;
}

static int app_cmp_regs(const void *a, const void *b) {
    return strcmp(((const struct app_reg *)a)->name, ((const struct app_reg *)b)->name);
}

static int app_registry_save(const char *approot, struct app_reg *regs, int n) {
    char path[8192], tmp[8192];
    if (app_join2(path, sizeof(path), approot, APP_REGISTRY) < 0) return -1;
    if (app_join2(tmp, sizeof(tmp), approot, APP_REGISTRY ".tmp") < 0) return -1;
    FILE *f = fopen(tmp, "w");
    if (!f) return -1;
    fprintf(f, "{\n  \"schema_version\": 1,\n  \"apps\": {");
    if (n == 0) {
        fprintf(f, "}\n");
    } else {
        qsort(regs, (size_t)n, sizeof(struct app_reg), app_cmp_regs);
        for (int i = 0; i < n; i++)
            fprintf(f, "%s\n    \"%s\": {\n      \"bytes\": %ld,\n      \"files\": %ld,\n      \"installed_utc\": \"%s\",\n      \"version\": \"%s\",\n      \"xpkg_sha256\": \"%s\"\n    }",
                    i ? "," : "", regs[i].name, regs[i].bytes, regs[i].files,
                    regs[i].installed_utc, regs[i].version, regs[i].xpkg_sha256);
        fprintf(f, "\n  }\n");
    }
    fprintf(f, "}\n");
    if (fflush(f) != 0 || ferror(f) || fsync(fileno(f)) != 0) { fclose(f); return -1; }
    fclose(f);
    return rename(tmp, path) < 0 ? -1 : 0;
}

static int app_registry_lock(const char *approot) {
    char path[8192];
    if (app_join2(path, sizeof(path), approot, "registry.lock") < 0) return -1;
    int fd = open(path, O_CREAT | O_RDWR, 0644);
    if (fd < 0) return -1;
    if (flock(fd, LOCK_EX) < 0) { close(fd); return -1; }
    return fd;
}

static int app_rm_rf(const char *path, int depth) {
    if (depth > 64) return -1;
    struct stat st;
    if (lstat(path, &st) < 0) return errno == ENOENT ? 0 : -1;
    if (!S_ISDIR(st.st_mode)) return unlink(path) < 0 ? -1 : 0;
    DIR *dir = opendir(path);
    if (!dir) return -1;
    struct dirent *de;
    int rc = 0;
    char child[8192];
    while ((de = readdir(dir))) {
        if (strcmp(de->d_name, ".") == 0 || strcmp(de->d_name, "..") == 0) continue;
        if (snprintf(child, sizeof(child), "%s/%s", path, de->d_name) >= (int)sizeof(child)) { rc = -1; break; }
        if (app_rm_rf(child, depth + 1) < 0) { rc = -1; break; }
    }
    closedir(dir);
    if (rc == 0 && rmdir(path) < 0 && errno != ENOENT) rc = -1;
    return rc;
}

static void app_utc_now(char *out, size_t n) {
    time_t now = time(NULL);
    struct tm utc;
    gmtime_r(&now, &utc);
    strftime(out, n, "%Y-%m-%dT%H:%M:%SZ", &utc);
}

static int app_find_reg(struct app_reg *regs, int n, const char *name) {
    for (int i = 0; i < n; i++)
        if (strcmp(regs[i].name, name) == 0) return i;
    return -1;
}

/* ---- app commands ---- */

/* Shared install/upgrade pipeline: stage and fully verify an xpkg under
 * approot. On success the CALLER owns the staging directory named in
 * `stage` (activate it with rename() or remove it with app_rm_rf()).
 * On failure the staging dir is already removed and `err` explains why. */
struct app_stage {
    struct app_meta meta;
    char xpkg_sha[72];
    int n_payload;
    long total_bytes;
};

static int app_stage_verify(const char *file, const char *approot,
                            char *stage, size_t stage_n, struct app_stage *st,
                            char *err, size_t err_n) {
    struct app_entry *files = NULL, *sums = NULL, *mfiles = NULL;
    int n_files = 0, cap_files = 0, n_sums = 0, cap_sums = 0, n_mfiles = 0, cap_mfiles = 0;
    int staged = 0;
    int rc = -1;
    char spool[8192];
    char *list = NULL;
    size_t list_len = 0;
    spool[0] = '\0';
    if (access(file, R_OK) != 0) { snprintf(err, err_n, "pkg: cannot read %s", file); goto out; }
    if (mkdir(approot, 0755) < 0 && errno != EEXIST) { snprintf(err, err_n, "pkg: cannot create %s", approot); goto out; }
    if (sha256_file(file, st->xpkg_sha) < 0) { snprintf(err, err_n, "pkg: cannot hash %s", file); goto out; }
    if (app_join2(stage, stage_n, approot, ".stage-XXXXXX") < 0) { snprintf(err, err_n, "pkg: app root path too long"); goto out; }
    if (!mkdtemp(stage)) { snprintf(err, err_n, "pkg: cannot create staging dir"); goto out; }
    staged = 1;
    /* Native decompress: gzip member(s) verified (CRC32/ISIZE) to a spool
     * tar inside the app root. No external binary is executed. */
    if (app_join2(spool, sizeof(spool), approot, ".spool-XXXXXX") < 0) {
        snprintf(err, err_n, "pkg: app root path too long"); goto out;
    }
    {
        int sfd = mkstemp(spool);
        if (sfd < 0) { snprintf(err, err_n, "pkg: cannot create spool file"); goto out; }
        FILE *xin = fopen(file, "rb");
        FILE *xout = NULL;
        if (xin) xout = fdopen(sfd, "wb");
        if (!xin || !xout) {
            if (xout) fclose(xout); else close(sfd);
            if (xin) fclose(xin);
            snprintf(err, err_n, "pkg: cannot list %s", file); goto out;
        }
        int gok = xz_gzip_to_file(xin, xout);
        int cok = fclose(xout);
        fclose(xin);
        if (gok < 0 || cok != 0) {
            snprintf(err, err_n, "pkg: cannot list %s", file); goto out;
        }
    }
    char *list2 = NULL;
    if (xtar_list(spool, &list2, &list_len, APP_MAX_ENTRIES, APP_TAR_LIST_MAX) < 0) {
        free(list2);
        snprintf(err, err_n, "pkg: cannot list %s", file); goto out;
    }
    list = list2;
    if (app_listing_validate(list, list_len) < 0) {
        snprintf(err, err_n, "pkg: archive listing is unsafe (traversal, shadow or non-xpkg member)"); goto out;
    }
    free(list); list = NULL;
    if (xtar_extract(spool, stage) < 0) {
        snprintf(err, err_n, "pkg: extraction failed"); goto out;
    }
    unlink(spool);
    spool[0] = '\0';
    if (app_scan_tree(stage, "", &files, &n_files, &cap_files, 0) < 0) {
        snprintf(err, err_n, "pkg: archive contains forbidden entry types (symlink/hardlink/device/setuid)"); goto out;
    }
    qsort(files, (size_t)n_files, sizeof(struct app_entry), app_cmp_entries);
    char metapath[8192];
    struct stat mst;
    if (app_join2(metapath, sizeof(metapath), stage, "xpkg/manifest.json") < 0
        || stat(metapath, &mst) < 0 || mst.st_size > APP_MANIFEST_MAX) {
        snprintf(err, err_n, "pkg: xpkg/manifest.json missing or oversized"); goto out;
    }
    if (app_join2(metapath, sizeof(metapath), stage, "xpkg/SHA256SUMS") < 0
        || app_parse_sums(metapath, &sums, &n_sums, &cap_sums) < 0) {
        snprintf(err, err_n, "pkg: xpkg/SHA256SUMS missing or malformed"); goto out;
    }
    if (app_join2(metapath, sizeof(metapath), stage, "xpkg/manifest.json") < 0
        || app_parse_manifest(metapath, &st->meta, &mfiles, &n_mfiles, &cap_mfiles) < 0) {
        snprintf(err, err_n, "pkg: xpkg/manifest.json malformed"); goto out;
    }
    if (!app_token_name(st->meta.name) || strcmp(st->meta.name, "apps") == 0) {
        snprintf(err, err_n, "pkg: invalid app name in manifest"); goto out;
    }
    if (!app_token_version(st->meta.version)) {
        snprintf(err, err_n, "pkg: invalid app version in manifest"); goto out;
    }
    if (strcmp(st->meta.architecture, "x86_64") != 0 && strcmp(st->meta.architecture, "all") != 0) {
        snprintf(err, err_n, "pkg: unsupported app architecture '%s'", st->meta.architecture); goto out;
    }
    st->n_payload = 0;
    st->total_bytes = 0;
    for (int i = 0; i < n_files; i++) {
        if (strncmp(files[i].path, "payload/", 8) == 0) { st->n_payload++; st->total_bytes += files[i].size; }
        else if (strcmp(files[i].path, "xpkg/SHA256SUMS") != 0 && strcmp(files[i].path, "xpkg/manifest.json") != 0) {
            snprintf(err, err_n, "pkg: unexpected archive member '%s'", files[i].path); goto out;
        }
    }
    if (st->n_payload == 0 || st->n_payload != n_sums || st->n_payload != n_mfiles) {
        snprintf(err, err_n, "pkg: payload/manifest/sums file sets differ"); goto out;
    }
    qsort(sums, (size_t)n_sums, sizeof(struct app_entry), app_cmp_entries);
    qsort(mfiles, (size_t)n_mfiles, sizeof(struct app_entry), app_cmp_entries);
    int pi = 0;
    for (int i = 0; i < n_files; i++) {
        if (strncmp(files[i].path, "payload/", 8) != 0) continue;
        if (strcmp(files[i].path, sums[pi].path) != 0 || strcmp(files[i].path, mfiles[pi].path) != 0) {
            snprintf(err, err_n, "pkg: file list mismatch at '%s'", files[i].path); goto out;
        }
        if (strcmp(sums[pi].sha256, mfiles[pi].sha256) != 0 || mfiles[pi].size != files[i].size) {
            snprintf(err, err_n, "pkg: size/hash mismatch in manifest for '%s'", files[i].path); goto out;
        }
        char full[8192], hex[65];
        if (app_join2(full, sizeof(full), stage, files[i].path) < 0
            || sha256_file(full, hex) < 0 || strcmp(hex, sums[pi].sha256) != 0) {
            snprintf(err, err_n, "pkg: payload hash mismatch for '%s'", files[i].path); goto out;
        }
        pi++;
    }
    rc = 0;
    staged = 0; /* success: caller owns the stage dir */
out:
    free(list);
    free(files); free(sums); free(mfiles);
    if (spool[0]) unlink(spool);
    if (staged) app_rm_rf(stage, 0);
    return rc;
}

static int cmd_app_install(int json, const char *file) {
    char rootbuf[8192];
    const char *approot = app_root(rootbuf, sizeof(rootbuf));
    if (geteuid() != 0 && !getenv("XTOS_APP_ROOT")) {
        fprintf(stderr, "pkg: app install requires root (or set XTOS_APP_ROOT)\n");
        return 1;
    }
    char stage[8192], err[4096];
    struct app_stage st;
    memset(&st, 0, sizeof(st));
    struct app_reg regs[APP_REGISTRY_MAX];
    int lockfd = -1;
    int rc = 1;
    int staged = 0;
    if (app_stage_verify(file, approot, stage, sizeof(stage), &st, err, sizeof(err)) < 0) {
        fprintf(stderr, "%s\n", err);
        return 1;
    }
    staged = 1;
    lockfd = app_registry_lock(approot);
    if (lockfd < 0) { fprintf(stderr, "pkg: cannot lock app registry\n"); goto cleanup; }
    int n_regs = app_registry_load(approot, regs, APP_REGISTRY_MAX);
    if (n_regs < 0) { fprintf(stderr, "pkg: registry is corrupt\n"); goto cleanup; }
    if (app_find_reg(regs, n_regs, st.meta.name) >= 0) {
        fprintf(stderr, "pkg: app '%s' already installed (remove it first)\n", st.meta.name); goto cleanup;
    }
    char namedir[8192], target[8192];
    if (app_join2(namedir, sizeof(namedir), approot, st.meta.name) < 0
        || app_join2(target, sizeof(target), namedir, st.meta.version) < 0) {
        fprintf(stderr, "pkg: install path too long\n"); goto cleanup;
    }
    struct stat tst;
    if (stat(target, &tst) == 0) {
        fprintf(stderr, "pkg: install target already exists: %s\n", target); goto cleanup;
    }
    if (mkdir(namedir, 0755) < 0 && errno != EEXIST) {
        fprintf(stderr, "pkg: cannot create %s\n", namedir); goto cleanup;
    }
    if (chmod(stage, 0755) < 0 || rename(stage, target) < 0) {
        fprintf(stderr, "pkg: cannot activate install\n"); goto cleanup;
    }
    staged = 0;
    struct app_reg *slot = &regs[n_regs++];
    snprintf(slot->name, sizeof(slot->name), "%s", st.meta.name);
    snprintf(slot->version, sizeof(slot->version), "%s", st.meta.version);
    snprintf(slot->xpkg_sha256, sizeof(slot->xpkg_sha256), "%s", st.xpkg_sha);
    slot->files = st.n_payload;
    slot->bytes = st.total_bytes;
    app_utc_now(slot->installed_utc, sizeof(slot->installed_utc));
    if (app_registry_save(approot, regs, n_regs) < 0) {
        fprintf(stderr, "pkg: cannot update registry\n"); goto cleanup;
    }
    flock(lockfd, LOCK_UN); close(lockfd); lockfd = -1;
    rc = 0;
    if (json)
        printf("{\"status\": \"installed\", \"name\": \"%s\", \"version\": \"%s\", \"files\": %d, \"bytes\": %ld, \"xpkg_sha256\": \"%s\", \"path\": \"%s\"}\n",
               st.meta.name, st.meta.version, st.n_payload, st.total_bytes, st.xpkg_sha, target);
    else {
        xui_init();
        printf("%s%s%s XTOS_PKG_APP_INSTALL name=%s version=%s files=%d bytes=%ld status=%s", XUI_LABEL_S, "PKG_APP", XUI_RESET_S,
               st.meta.name, st.meta.version, st.n_payload, st.total_bytes, "installed");
        XUI_OK("\n");
    }
cleanup:
    if (lockfd >= 0) { flock(lockfd, LOCK_UN); close(lockfd); }
    if (staged) app_rm_rf(stage, 0);
    return rc;
}

static int cmd_app_remove(int json, const char *name) {
    char rootbuf[8192];
    const char *approot = app_root(rootbuf, sizeof(rootbuf));
    if (geteuid() != 0 && !getenv("XTOS_APP_ROOT")) {
        fprintf(stderr, "pkg: app remove requires root (or set XTOS_APP_ROOT)\n");
        return 1;
    }
    if (!app_token_name(name)) { fprintf(stderr, "pkg: invalid app name '%s'\n", name); return 2; }
    int lockfd = app_registry_lock(approot);
    if (lockfd < 0) { fprintf(stderr, "pkg: cannot lock app registry\n"); return 1; }
    struct app_reg regs[APP_REGISTRY_MAX];
    int n = app_registry_load(approot, regs, APP_REGISTRY_MAX);
    int rc = 1;
    if (n < 0) { fprintf(stderr, "pkg: registry is corrupt\n"); goto out; }
    int idx = app_find_reg(regs, n, name);
    if (idx < 0) { fprintf(stderr, "pkg: app '%s' is not installed\n", name); goto out; }
    char target[8192], namedir[8192];
    if (app_join3(target, sizeof(target), approot, name, regs[idx].version) < 0
        || app_join2(namedir, sizeof(namedir), approot, name) < 0) {
        fprintf(stderr, "pkg: remove path too long\n"); goto out;
    }
    if (app_rm_rf(target, 0) < 0) { fprintf(stderr, "pkg: cannot remove %s\n", target); goto out; }
    rmdir(namedir); /* best effort: keep other versions if any */
    for (int i = idx; i < n - 1; i++) regs[i] = regs[i + 1];
    if (app_registry_save(approot, regs, n - 1) < 0) { fprintf(stderr, "pkg: cannot update registry\n"); goto out; }
    rc = 0;
    if (json)
        printf("{\"status\": \"removed\", \"name\": \"%s\", \"version\": \"%s\"}\n", name, regs[idx].version);
    else {
        xui_init();
        printf("%s%s%s XTOS_PKG_APP_REMOVE name=%s version=%s status=%s", XUI_LABEL_S, "PKG_APP", XUI_RESET_S,
               name, regs[idx].version, "removed");
        XUI_OK("\n");
    }
out:
    flock(lockfd, LOCK_UN); close(lockfd);
    return rc;
}

/* Upgrade = install pipeline + atomic version replacement. Ordering keeps
 * every crash window recoverable: payload rename happens first, the registry
 * write is the commit point (rolled back by removing the new payload), and
 * the old payload is dropped only after the commit. A stale target directory
 * left by an interrupted upgrade is cleaned up on the next attempt. */
static int cmd_app_upgrade(int json, const char *file) {
    char rootbuf[8192];
    const char *approot = app_root(rootbuf, sizeof(rootbuf));
    if (geteuid() != 0 && !getenv("XTOS_APP_ROOT")) {
        fprintf(stderr, "pkg: app upgrade requires root (or set XTOS_APP_ROOT)\n");
        return 1;
    }
    char stage[8192], err[4096];
    struct app_stage st;
    memset(&st, 0, sizeof(st));
    struct app_reg regs[APP_REGISTRY_MAX];
    int lockfd = -1;
    int rc = 1;
    int staged = 0;
    if (app_stage_verify(file, approot, stage, sizeof(stage), &st, err, sizeof(err)) < 0) {
        fprintf(stderr, "%s\n", err);
        return 1;
    }
    staged = 1;
    lockfd = app_registry_lock(approot);
    if (lockfd < 0) { fprintf(stderr, "pkg: cannot lock app registry\n"); goto cleanup; }
    int n_regs = app_registry_load(approot, regs, APP_REGISTRY_MAX);
    if (n_regs < 0) { fprintf(stderr, "pkg: registry is corrupt\n"); goto cleanup; }
    int idx = app_find_reg(regs, n_regs, st.meta.name);
    if (idx < 0) { fprintf(stderr, "pkg: app '%s' is not installed\n", st.meta.name); goto cleanup; }
    char oldver[APP_VER_MAX + 1];
    snprintf(oldver, sizeof(oldver), "%s", regs[idx].version);
    if (strcmp(st.meta.version, oldver) == 0) {
        fprintf(stderr, "pkg: app '%s' already at version %s\n", st.meta.name, oldver); goto cleanup;
    }
    char namedir[8192], target[8192], oldtarget[8192];
    if (app_join2(namedir, sizeof(namedir), approot, st.meta.name) < 0
        || app_join2(target, sizeof(target), namedir, st.meta.version) < 0
        || app_join2(oldtarget, sizeof(oldtarget), namedir, oldver) < 0) {
        fprintf(stderr, "pkg: upgrade path too long\n"); goto cleanup;
    }
    struct stat tst;
    if (stat(target, &tst) == 0) {
        /* not the registered version: leftover of an interrupted upgrade */
        if (app_rm_rf(target, 0) < 0) {
            fprintf(stderr, "pkg: cannot clean stale target %s\n", target); goto cleanup;
        }
    }
    if (mkdir(namedir, 0755) < 0 && errno != EEXIST) {
        fprintf(stderr, "pkg: cannot create %s\n", namedir); goto cleanup;
    }
    if (chmod(stage, 0755) < 0 || rename(stage, target) < 0) {
        fprintf(stderr, "pkg: cannot activate upgrade\n"); goto cleanup;
    }
    staged = 0;
    snprintf(regs[idx].version, sizeof(regs[idx].version), "%s", st.meta.version);
    snprintf(regs[idx].xpkg_sha256, sizeof(regs[idx].xpkg_sha256), "%s", st.xpkg_sha);
    regs[idx].files = st.n_payload;
    regs[idx].bytes = st.total_bytes;
    app_utc_now(regs[idx].installed_utc, sizeof(regs[idx].installed_utc));
    if (app_registry_save(approot, regs, n_regs) < 0) {
        fprintf(stderr, "pkg: cannot update registry\n");
        /* rollback: the registered old version stays authoritative */
        app_rm_rf(target, 0);
        goto cleanup;
    }
    /* committed: drop the old payload (best effort) */
    if (app_rm_rf(oldtarget, 0) < 0)
        fprintf(stderr, "pkg: warning: old version directory remains at %s\n", oldtarget);
    flock(lockfd, LOCK_UN); close(lockfd); lockfd = -1;
    rc = 0;
    if (json)
        printf("{\"status\": \"upgraded\", \"name\": \"%s\", \"version\": \"%s\", \"from_version\": \"%s\", \"files\": %d, \"bytes\": %ld, \"xpkg_sha256\": \"%s\", \"path\": \"%s\"}\n",
               st.meta.name, st.meta.version, oldver, st.n_payload, st.total_bytes, st.xpkg_sha, target);
    else {
        xui_init();
        printf("%s%s%s XTOS_PKG_APP_UPGRADE name=%s version=%s from_version=%s files=%d bytes=%ld status=%s", XUI_LABEL_S, "PKG_APP", XUI_RESET_S,
               st.meta.name, st.meta.version, oldver, st.n_payload, st.total_bytes, "upgraded");
        XUI_OK("\n");
    }
cleanup:
    if (lockfd >= 0) { flock(lockfd, LOCK_UN); close(lockfd); }
    if (staged) app_rm_rf(stage, 0);
    return rc;
}

static int cmd_app_list(int json) {
    char rootbuf[8192];
    const char *approot = app_root(rootbuf, sizeof(rootbuf));
    struct app_reg regs[APP_REGISTRY_MAX];
    int n = app_registry_load(approot, regs, APP_REGISTRY_MAX);
    if (n < 0) { fprintf(stderr, "pkg: registry is corrupt\n"); return 1; }
    if (!json) {
        xui_init();
        printf("%s%-24s %-24s %-8s %12s%s %s\n", XUI_LABEL_S, "APP", "VERSION", "FILES", "BYTES", XUI_RESET_S, "STATUS");
    } else {
        printf("[\n");
    }
    int printed = 0;
    for (int i = 0; i < n; i++) {
        char target[8192];
        if (app_join3(target, sizeof(target), approot, regs[i].name, regs[i].version) < 0) {
            fprintf(stderr, "pkg: installed path too long\n");
            return 1;
        }
        const char *status = access(target, F_OK) == 0 ? "installed" : "missing";
        if (json)
            printf("%s  {\"name\": \"%s\", \"version\": \"%s\", \"files\": %ld, \"bytes\": %ld, \"status\": \"%s\", \"installed_utc\": \"%s\"}",
                   printed ? ",\n" : "", regs[i].name, regs[i].version, regs[i].files, regs[i].bytes, status, regs[i].installed_utc);
        else
            printf("%-24s %-24s %-8ld %12ld %s\n", regs[i].name, regs[i].version, regs[i].files, regs[i].bytes, status);
        printed++;
    }
    /* report unregistered directories on disk */
    DIR *dir = opendir(approot);
    if (dir) {
        struct dirent *de;
        while ((de = readdir(dir))) {
            if (de->d_name[0] == '.' || strcmp(de->d_name, "registry.json") == 0 || strcmp(de->d_name, "registry.lock") == 0) continue;
            char namedir[8192];
            if (app_join2(namedir, sizeof(namedir), approot, de->d_name) < 0) continue;
            struct stat st;
            if (stat(namedir, &st) != 0 || !S_ISDIR(st.st_mode)) continue;
            if (app_find_reg(regs, n, de->d_name) >= 0) continue;
            if (json)
                printf("%s  {\"name\": \"%s\", \"version\": \"\", \"files\": 0, \"bytes\": 0, \"status\": \"unregistered\", \"installed_utc\": \"\"}",
                       printed ? ",\n" : "", de->d_name);
            else
                printf("%-24s %-24s %-8d %12ld %s\n", de->d_name, "?", 0, 0L, "unregistered");
            printed++;
        }
        closedir(dir);
    }
    if (json) printf("\n]\n");
    return 0;
}

static int cmd_app_verify(int json, const char *name, const char *version) {
    char rootbuf[8192];
    const char *approot = app_root(rootbuf, sizeof(rootbuf));
    if (!app_token_name(name)) { fprintf(stderr, "pkg: invalid app name '%s'\n", name); return 2; }
    struct app_reg regs[APP_REGISTRY_MAX];
    int n = app_registry_load(approot, regs, APP_REGISTRY_MAX);
    if (n < 0) { fprintf(stderr, "pkg: registry is corrupt\n"); return 1; }
    int idx = app_find_reg(regs, n, name);
    char verbuf[APP_VER_MAX + 1];
    if (!version) {
        if (idx < 0) { fprintf(stderr, "pkg: app '%s' is not installed\n", name); return 1; }
        version = regs[idx].version;
    }
    snprintf(verbuf, sizeof(verbuf), "%s", version);
    char target[8192];
    if (app_join3(target, sizeof(target), approot, name, verbuf) < 0) {
        fprintf(stderr, "pkg: installed path too long\n");
        return 1;
    }
    char sums_path[8192];
    if (app_join2(sums_path, sizeof(sums_path), target, "xpkg/SHA256SUMS") < 0) {
        fprintf(stderr, "pkg: installed app has no readable SHA256SUMS\n");
        return 1;
    }
    struct app_entry *sums = NULL;
    int n_sums = 0, cap_sums = 0;
    if (app_parse_sums(sums_path, &sums, &n_sums, &cap_sums) < 0) {
        fprintf(stderr, "pkg: installed app has no readable SHA256SUMS\n");
        return 1;
    }
    qsort(sums, (size_t)n_sums, sizeof(struct app_entry), app_cmp_entries);
    int checked = 0, missing = 0, corrupt = 0, extra = 0;
    struct app_entry *disk = NULL;
    int n_disk = 0, cap_disk = 0;
    if (app_scan_tree(target, "payload", &disk, &n_disk, &cap_disk, 0) == 0) {
        /* scan_tree with rel="payload" yields target-relative "payload/..." paths */
        qsort(disk, (size_t)n_disk, sizeof(struct app_entry), app_cmp_entries);
        int si = 0, di = 0;
        while (si < n_sums || di < n_disk) {
            int c = si < n_sums && di < n_disk ? strcmp(sums[si].path, disk[di].path) : (si < n_sums ? -1 : 1);
            if (c == 0) {
                checked++;
                char full[8192], hex[65];
                if (app_join2(full, sizeof(full), target, sums[si].path) < 0
                    || sha256_file(full, hex) < 0 || strcmp(hex, sums[si].sha256) != 0) { corrupt++; printf("CORRUPT %s\n", sums[si].path); }
                si++; di++;
            } else if (c < 0) {
                missing++; printf("MISSING %s\n", sums[si].path); si++;
            } else {
                extra++; printf("EXTRA %s\n", disk[di].path); di++;
            }
        }
    } else {
        missing = n_sums;
    }
    free(sums); free(disk);
    if (json)
        printf("{\"name\": \"%s\", \"version\": \"%s\", \"checked\": %d, \"missing\": %d, \"corrupt\": %d, \"extra\": %d, \"ok\": %d}\n",
               name, verbuf, checked, missing, corrupt, extra, checked - missing - corrupt - extra);
    else {
        xui_init();
        printf("%s%s%s XTOS_PKG_APP_VERIFY checked=%d ok=", XUI_LABEL_S, "PKG_APP", XUI_RESET_S, checked);
        if (missing + corrupt + extra == 0) XUI_OK("%d", checked - missing - corrupt - extra);
        else XUI_BAD("%d", checked - missing - corrupt - extra);
        printf(" missing=%d corrupt=%d extra=%d\n", missing, corrupt, extra);
    }
    return (missing + corrupt + extra) > 0 ? 1 : 0;
}

static int cmd_app_info(int json, const char *name) {
    char rootbuf[8192];
    const char *approot = app_root(rootbuf, sizeof(rootbuf));
    if (!app_token_name(name)) { fprintf(stderr, "pkg: invalid app name '%s'\n", name); return 2; }
    struct app_reg regs[APP_REGISTRY_MAX];
    int n = app_registry_load(approot, regs, APP_REGISTRY_MAX);
    if (n < 0) { fprintf(stderr, "pkg: registry is corrupt\n"); return 1; }
    int idx = app_find_reg(regs, n, name);
    if (idx < 0) { fprintf(stderr, "pkg: app '%s' is not installed\n", name); return 1; }
    char target[8192], metapath[8192];
    if (app_join3(target, sizeof(target), approot, name, regs[idx].version) < 0
        || app_join2(metapath, sizeof(metapath), target, "xpkg/manifest.json") < 0) {
        fprintf(stderr, "pkg: installed path too long\n");
        return 1;
    }
    struct app_meta meta;
    struct app_entry *mfiles = NULL;
    int n_mfiles = 0, cap_mfiles = 0;
    if (app_parse_manifest(metapath, &meta, &mfiles, &n_mfiles, &cap_mfiles) < 0) {
        fprintf(stderr, "pkg: installed manifest unreadable\n");
        return 1;
    }
    free(mfiles);
    if (json) {
        printf("{\"name\": \"%s\", \"version\": \"%s\", \"architecture\": \"%s\", \"license\": \"%s\", \"vendor\": \"%s\", \"xtos_min_version\": \"%s\", \"files\": %ld, \"bytes\": %ld, \"installed_utc\": \"%s\", \"xpkg_sha256\": \"%s\"}\n",
               meta.name, meta.version, meta.architecture, meta.license, meta.vendor, meta.xtos_min_version,
               regs[idx].files, regs[idx].bytes, regs[idx].installed_utc, regs[idx].xpkg_sha256);
    } else {
        xui_init();
        XUI_LABEL("App:        "); printf("%s\n", meta.name);
        XUI_LABEL("Version:    "); printf("%s\n", meta.version);
        XUI_LABEL("Arch:       "); printf("%s\n", meta.architecture);
        XUI_LABEL("License:    "); printf("%s\n", meta.license);
        XUI_LABEL("Vendor:     "); printf("%s\n", meta.vendor);
        XUI_LABEL("Min XTOS:   "); printf("%s\n", meta.xtos_min_version);
        XUI_LABEL("Summary:    "); printf("%s\n", meta.summary);
        XUI_LABEL("Installed:  "); printf("%ld files, %ld bytes\n", regs[idx].files, regs[idx].bytes);
        XUI_LABEL("Since:      "); XUI_DIM("%s\n", regs[idx].installed_utc);
        XUI_LABEL("Xpkg sha:   "); XUI_DIM("%s\n", regs[idx].xpkg_sha256);
    }
    return 0;
}

static int cmd_app(int json, int argc, char **argv) {
    if (argc < 1) {
        fprintf(stderr,
            "usage: pkg app <command>\n"
            "  pkg app install <file.xpkg>   verify and install an application package\n"
            "  pkg app upgrade <file.xpkg>   verify and upgrade an installed application\n"
            "  pkg app remove <name>         remove an installed application\n"
            "  pkg app list                  list installed applications\n"
            "  pkg app verify <name>         re-verify installed application files\n"
            "  pkg app info <name>           show application manifest details\n");
        return 2;
    }
    if (strcmp(argv[0], "install") == 0 && argc == 2) return cmd_app_install(json, argv[1]);
    if (strcmp(argv[0], "upgrade") == 0 && argc == 2) return cmd_app_upgrade(json, argv[1]);
    if (strcmp(argv[0], "remove") == 0 && argc == 2) return cmd_app_remove(json, argv[1]);
    if (strcmp(argv[0], "list") == 0 && argc == 1) return cmd_app_list(json);
    if (strcmp(argv[0], "verify") == 0 && (argc == 2 || argc == 3)) return cmd_app_verify(json, argv[1], argc == 3 ? argv[2] : NULL);
    if (strcmp(argv[0], "info") == 0 && argc == 2) return cmd_app_info(json, argv[1]);
    fprintf(stderr, "pkg: unknown app command\n");
    return 2;
}

int main(int argc, char **argv) {
    int json = 0;
    int argstart = 1;
    if (argc > 1 && strcmp(argv[1], "--json") == 0) { json = 1; argstart = 2; }
    if (argc <= argstart) { usage(); return 2; }
    if (strcmp(argv[argstart], "app") == 0)
        return cmd_app(json, argc - argstart - 1, argv + argstart + 1);
    if (load_manifest() < 0) {
        fprintf(stderr, "pkg: cannot read %s\n", MANIFEST_PATH);
        return 1;
    }
    load_overrides();
    const char *cmd = argv[argstart];
    if (strcmp(cmd, "list") == 0)
        return cmd_list(json, argc > argstart + 1 ? argv[argstart + 1] : NULL);
    if (strcmp(cmd, "show") == 0) {
        if (argc <= argstart + 1) { usage(); return 2; }
        return cmd_show(json, argv[argstart + 1]);
    }
    if (strcmp(cmd, "files") == 0) {
        if (argc <= argstart + 1) { usage(); return 2; }
        return cmd_files(argv[argstart + 1]);
    }
    if (strcmp(cmd, "verify") == 0) {
        if (argc <= argstart + 1) { usage(); return 2; }
        return cmd_verify(argv[argstart + 1], json);
    }
    if (strcmp(cmd, "size") == 0) return cmd_size();
    if (strcmp(cmd, "summary") == 0) return cmd_summary();
    fprintf(stderr, "pkg: unknown command %s\n", cmd);
    usage();
    return 2;
}
