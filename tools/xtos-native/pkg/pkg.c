/* pkg: XTOS native package inventory + integrity verification (C, static).
 * Reads /usr/share/xtos/userspace-manifest.json. Read-only queries plus
 * sha256 verification of installed files against the manifest.
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

int main(int argc, char **argv) {
    int json = 0;
    int argstart = 1;
    if (argc > 1 && strcmp(argv[1], "--json") == 0) { json = 1; argstart = 2; }
    if (argc <= argstart) { usage(); return 2; }
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
