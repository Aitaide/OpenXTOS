/* xtos_ustar.h — self-contained tar reader (no external tar binary).
 *
 * Header-only static implementation. Understands:
 *   - POSIX ustar headers (name + prefix, magic "ustar"),
 *   - GNU long-name/long-link meta members ('L' / 'K'),
 *   - PAX per-file extended headers ('x': path= and size= records),
 *   - PAX global headers ('g': parsed and deliberately ignored — the
 *     caller's listing validator stays the audit authority),
 *   - end-of-archive (first all-zero header; the tail must be zeros),
 *   - stored-block alignment and per-member data padding.
 *
 * Rejects outright: hard links ('1'), symlinks ('2'), character/block/fifo
 * devices ('3'/'4'/'6'), contiguous ('7'), volume/dump entries, base-256
 * size encodings (>8 GiB members), bad header checksums, duplicate member
 * names, and any name that is absolute, contains ".."/empty components,
 * control bytes or NUL, or exceeds the length caps.
 *
 * Name POLICY (payload/, xpkg/ prefixes, shadow rules, entry caps) stays in
 * the consumer: this module only enforces generic path safety and produces
 * a `tar -t`-style listing that the consumer validates.
 */
#ifndef XTOS_USTAR_H
#define XTOS_USTAR_H

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define XTAR_NAME_MAX 1024
#define XTAR_META_MAX (1024 * 1024)
#define XTAR_BLOCK    512

/* Member classification */
#define XTAR_REG     0   /* regular file */
#define XTAR_DIR     1   /* directory */
#define XTAR_END     2   /* end-of-archive marker seen */
#define XTAR_BAD     3   /* hard-rejected member (links/devices/unknown) */

struct xtar_pending {
    char path[XTAR_NAME_MAX];
    long long size;
    int has_path;
    int has_size;
};

/* Parse a non-negative octal header field. Rejects base-256 encodings. */
static long long xtar_octal(const char *p, int n) {
    int i = 0;
    while (i < n && p[i] == ' ') i++;
    if (i >= n) return -1;
    if (p[i] & 0x80) return -1; /* base-256: unsupported (>8 GiB members) */
    long long v = 0;
    for (; i < n; i++) {
        if (p[i] == 0 || p[i] == ' ') break; /* NUL/space padding ends the field */
        if (p[i] < '0' || p[i] > '7') return -1;
        v = v * 8 + (p[i] - '0');
        if (v > (1LL << 40)) return -1; /* absurd size */
    }
    return v;
}

/* Generic path safety: relative, no empty/./.. components, no control
 * bytes, sane lengths. A single trailing slash (directory entries) is
 * allowed and left in place. */
static int xtar_name_ok(const char *name) {
    size_t len = strlen(name);
    if (!len || len > 1000) return 0;
    if (name[0] == '/') return 0;
    if (len > 1 && name[len - 1] == '/') len--; /* trailing slash for dirs */
    const char *p = name;
    while (p < name + len) {
        const char *slash = memchr(p, '/', (size_t)(name + len - p));
        size_t clen = slash ? (size_t)(slash - p) : (size_t)(name + len - p);
        if (clen == 0) return 0;
        if (clen >= 256) return 0;
        if (p[0] == '.' && (clen == 1 || (clen == 2 && p[1] == '.'))) return 0;
        for (size_t i = 0; i < clen; i++)
            if ((unsigned char)p[i] < 32 || (unsigned char)p[i] == 0x7f) return 0;
        p += clen + (slash ? 1 : 0);
    }
    return 1;
}

/* Read exactly n bytes. Returns 0 ok, -1 EOF/short. */
static int xtar_read_exact(FILE *f, void *buf, size_t n) {
    return fread(buf, 1, n, f) == n ? 0 : -1;
}

/* Skip n bytes of member data plus padding to the next 512-byte block. */
static int xtar_skip_data(FILE *f, long long size) {
    long long pad = (-size) & (XTAR_BLOCK - 1);
    long long total = size + pad;
    unsigned char buf[XTAR_BLOCK];
    while (total > 0) {
        size_t take = total > (long long)sizeof(buf) ? sizeof(buf) : (size_t)total;
        if (xtar_read_exact(f, buf, take) < 0) return -1;
        total -= (long long)take;
    }
    return 0;
}

/* Parse one PAX extended-header blob into pending overrides. */
static int xtar_pax_parse(const unsigned char *data, long long size,
                          struct xtar_pending *pend) {
    if (size < 0 || size > XTAR_META_MAX) return -1;
    size_t pos = 0;
    char record[2048];
    while (pos < (size_t)size) {
        /* "<len> key=value\n" with len counting the whole record */
        long rlen = 0;
        size_t j = pos;
        while (j < (size_t)size && data[j] >= '0' && data[j] <= '9') {
            rlen = rlen * 10 + (data[j] - '0');
            if (rlen > 2048) return -1;
            j++;
        }
        if (j == pos || j >= (size_t)size || data[j] != ' ') return -1;
        if (rlen <= 0 || pos + (size_t)rlen > (size_t)size) return -1;
        size_t body = pos + (j + 1 - pos);
        size_t blen = (size_t)rlen - (j + 1 - pos);
        if (blen >= sizeof(record)) return -1;
        memcpy(record, data + body, blen);
        record[blen] = '\0';
        if (blen && record[blen - 1] == '\n') record[blen - 1] = '\0';
        if (strncmp(record, "path=", 5) == 0) {
            const char *v = record + 5;
            if (strlen(v) >= sizeof(pend->path)) return -1;
            strcpy(pend->path, v);
            pend->has_path = 1;
        } else if (strncmp(record, "size=", 5) == 0) {
            char *end = NULL;
            long long v = strtoll(record + 5, &end, 10);
            if (!end || *end != '\0' || v < 0) return -1;
            pend->size = v;
            pend->has_size = 1;
        }
        pos += (size_t)rlen;
    }
    return 0;
}

struct xtar_member {
    char name[XTAR_NAME_MAX]; /* effective name; dirs keep the trailing '/' */
    long long size;
    unsigned mode;            /* raw mode bits as stored */
    int type;                 /* XTAR_* */
};

/* Read the next REAL member from the tar spool, transparently consuming
 * meta members ('L', 'K', 'x', 'g'). Returns 0 and fills m, or -1 on any
 * hard format error. m->type == XTAR_END at end of archive. */
static int xtar_next(FILE *f, struct xtar_member *m) {
    struct xtar_pending pend;
    memset(&pend, 0, sizeof(pend));
    memset(m, 0, sizeof(*m));
    unsigned char h[XTAR_BLOCK];
    for (;;) {
        if (xtar_read_exact(f, h, XTAR_BLOCK) < 0) return -1;
        int all_zero = 1;
        for (int i = 0; i < XTAR_BLOCK; i++)
            if (h[i]) { all_zero = 0; break; }
        if (all_zero) {
            /* end of archive: remaining bytes must be zeros */
            int c;
            while ((c = getc(f)) != EOF)
                if (c != 0) return -1;
            m->type = XTAR_END;
            return 0;
        }
        /* checksum: bytes 148..155 hold octal of the header with that
         * field replaced by eight spaces */
        long long want = xtar_octal((const char *)h + 148, 8);
        if (want < 0) return -1;
        unsigned long sum = 0;
        for (int i = 0; i < XTAR_BLOCK; i++)
            sum += (i >= 148 && i < 156) ? ' ' : h[i];
        if (sum != (unsigned long)want) return -1;
        if (memcmp(h + 257, "ustar", 5) != 0) return -1;
        long long size = xtar_octal((const char *)h + 124, 12);
        long long mode = xtar_octal((const char *)h + 100, 8);
        if (size < 0 || mode < 0 || mode > 07777) return -1;
        char typeflag = (char)h[156];
        /* effective name: prefix + '/' + name, then pending overrides.
         * Exactly-100-byte names without a NUL terminator are legal
         * (GNU tar); the buffer copy below always NUL-terminates. */
        char raw[2 * 256];
        size_t nlen = strnlen((const char *)h, 100);
        size_t plen = strnlen((const char *)h + 345, 155);
        size_t at = 0;
        if (plen) {
            memcpy(raw + at, h + 345, plen);
            at += plen;
            raw[at++] = '/';
        }
        if (nlen > sizeof(raw) - at - 1) return -1;
        memcpy(raw + at, h, nlen);
        at += nlen;
        raw[at] = '\0';
        int meta = 0;
        switch (typeflag) {
        case 'L': case 'K': case 'x': case 'g':
            meta = 1;
            break;
        default:
            break;
        }
        if (!meta) {
            const char *eff = pend.has_path ? pend.path : raw;
            long long eff_size = pend.has_size ? pend.size : size;
            if (strlen(eff) >= sizeof(m->name)) return -1;
            strcpy(m->name, eff);
            m->size = eff_size;
            m->mode = (unsigned)mode;
            if (typeflag == '0' || typeflag == '\0') { m->type = XTAR_REG; return 0; }
            if (typeflag == '5') {
                m->type = XTAR_DIR;
                /* ensure the trailing slash convention like tar -t output */
                size_t l = strlen(m->name);
                if (l && m->name[l - 1] != '/' && l + 1 < sizeof(m->name)) {
                    m->name[l] = '/';
                    m->name[l + 1] = '\0';
                }
                return 0;
            }
            /* '1','2','3','4','6','7' and anything else: consume the data
             * block so the stream stays parseable, then report the reject */
            if (xtar_skip_data(f, size) < 0) return -1;
            m->type = XTAR_BAD;
            m->size = 0;
            return 0;
        }
        /* meta member: consume its data and apply overrides */
        if (size < 0 || size > XTAR_META_MAX) return -1;
        unsigned char *meta_data = malloc((size_t)size + 1);
        if (!meta_data) return -1;
        if (xtar_read_exact(f, meta_data, (size_t)size) < 0) { free(meta_data); return -1; }
        meta_data[size] = 0;
        long long pad = (-size) & (XTAR_BLOCK - 1);
        unsigned char padbuf[XTAR_BLOCK];
        if (pad && xtar_read_exact(f, padbuf, (size_t)pad) < 0) { free(meta_data); return -1; }
        int rc = 0;
        if (typeflag == 'L') {
            const char *v = (const char *)meta_data;
            if (strlen(v) >= sizeof(pend.path)) rc = -1;
            else strcpy(pend.path, v);
            pend.has_path = 1;
        } else if (typeflag == 'x') {
            rc = xtar_pax_parse(meta_data, size, &pend);
        } /* 'K' and 'g' are consumed and ignored */
        if (!rc && typeflag == 'L' && !xtar_name_ok(pend.path)) rc = -1;
        if (!rc && typeflag == 'x' && pend.has_path && !xtar_name_ok(pend.path)) rc = -1;
        free(meta_data);
        if (rc) return -1;
    }
}

/* Build a `tar -t`-style listing: one effective name per line (dirs with
 * trailing slash). Enforces entry and total-size caps and rejects duplicate
 * member names. Caller frees *list_out. */
static int xtar_list(const char *spool, char **list_out, size_t *list_len,
                     int max_entries, size_t max_list) {
    FILE *f = fopen(spool, "rb");
    if (!f) return -1;
    struct xtar_member m;
    char *buf = malloc(8192);
    size_t used = 0, cap = 8192;
    if (!buf) { fclose(f); return -1; }
    int entries = 0, rc = -1;
    while (xtar_next(f, &m) == 0) {
        if (m.type == XTAR_END) { rc = 0; break; }
        if (m.type == XTAR_BAD) { rc = -1; break; }
        if (entries >= max_entries) { rc = -1; break; }
        if (!xtar_name_ok(m.name)) { rc = -1; break; }
        size_t nlen = strlen(m.name);
        /* duplicate member names are rejected (tar would silently overwrite) */
        int dup = 0;
        {
            size_t i = 0;
            while (i < used) {
                const char *nl = memchr(buf + i, '\n', used - i);
                size_t l = (size_t)(nl - (buf + i));
                if (l == nlen && memcmp(buf + i, m.name, nlen) == 0) { dup = 1; break; }
                i += l + 1;
            }
        }
        if (dup) { rc = -1; break; }
        if (used + nlen + 1 > max_list || used + nlen + 1 > cap) {
            if (used + nlen + 1 > max_list || cap >= max_list) { rc = -1; break; }
            size_t ncap = cap * 2;
            char *grown = realloc(buf, ncap);
            if (!grown) { rc = -1; break; }
            buf = grown;
            cap = ncap;
        }
        memcpy(buf + used, m.name, nlen);
        used += nlen;
        buf[used++] = '\n';
        if (m.type == XTAR_DIR) {
            if (m.size > 0 && xtar_skip_data(f, m.size) < 0) { rc = -1; break; }
        } else {
            /* member data was NOT consumed by xtar_next for REG members;
             * skip it here to keep the stream in sync */
            if (xtar_skip_data(f, m.size) < 0) { rc = -1; break; }
        }
        entries++;
    }
    fclose(f);
    if (rc == 0) {
        *list_out = buf;
        *list_len = used;
    } else {
        free(buf);
    }
    return rc;
}

/* mkdir -p semantics for stage/<name>; parents get 0755. */
static int xtar_mkpath(const char *stage, const char *rel_dir) {
    char path[8192];
    size_t sl = strlen(stage), rl = strlen(rel_dir);
    if (sl + rl + 2 > sizeof(path)) return -1;
    memcpy(path, stage, sl);
    path[sl] = '/';
    memcpy(path + sl + 1, rel_dir, rl + 1);
    /* strip trailing slashes */
    while (rl > 0 && path[sl + 1 + rl - 1] == '/') { path[sl + 1 + rl - 1] = '\0'; rl--; }
    for (size_t i = sl + 1; path[i]; i++) {
        if (path[i] == '/') {
            path[i] = '\0';
            if (mkdir(path, 0755) < 0 && errno != EEXIST) return -1;
            path[i] = '/';
        }
    }
    if (mkdir(path, 0755) < 0 && errno != EEXIST) return -1;
    return 0;
}

/* Extract members under `stage`. The listing pass already validated policy;
 * this pass re-checks generic name safety, creates dirs, writes regular
 * files with O_CREAT|O_EXCL and applies stored mode bits (setuid bits are
 * applied so the consumer's post-extract scan can reject them). */
static int xtar_extract(const char *spool, const char *stage) {
    FILE *f = fopen(spool, "rb");
    if (!f) return -1;
    struct xtar_member m;
    int rc = -1;
    unsigned char iobuf[65536];
    while (xtar_next(f, &m) == 0) {
        if (m.type == XTAR_END) { rc = 0; break; }
        if (m.type == XTAR_BAD) break;
        if (!xtar_name_ok(m.name)) break;
        char rel[XTAR_NAME_MAX];
        size_t nl = strlen(m.name);
        if (m.type == XTAR_DIR && nl && m.name[nl - 1] == '/') nl--; /* strip dir slash */
        if (nl >= sizeof(rel)) break;
        memcpy(rel, m.name, nl);
        rel[nl] = '\0';
        if (m.type == XTAR_DIR) {
            if (xtar_mkpath(stage, rel) < 0) break;
            char full[8192];
            if (snprintf(full, sizeof(full), "%s/%s", stage, rel) >= (int)sizeof(full)) break;
            if (chmod(full, (mode_t)(m.mode & 07777)) < 0) break;
            if (m.size > 0 && xtar_skip_data(f, m.size) < 0) break;
            continue;
        }
        /* regular member: create parents, then O_EXCL file */
        const char *last = strrchr(rel, '/');
        if (last) {
            char parent[XTAR_NAME_MAX];
            size_t pl = (size_t)(last - rel);
            if (pl >= sizeof(parent)) break;
            memcpy(parent, rel, pl);
            parent[pl] = '\0';
            if (xtar_mkpath(stage, parent) < 0) break;
        }
        char full[8192];
        if (snprintf(full, sizeof(full), "%s/%s", stage, rel) >= (int)sizeof(full)) break;
        int fd = open(full, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
        if (fd < 0) break;
        long long left = m.size;
        int ok = 1;
        while (left > 0) {
            size_t take = left > (long long)sizeof(iobuf) ? sizeof(iobuf) : (size_t)left;
            if (xtar_read_exact(f, iobuf, take) < 0) { ok = 0; break; }
            if (write(fd, iobuf, take) != (ssize_t)take) { ok = 0; break; }
            left -= (long long)take;
        }
        if (ok && fchmod(fd, (mode_t)(m.mode & 07777)) < 0) ok = 0;
        if (close(fd) < 0) ok = 0;
        long long pad = (-m.size) & (XTAR_BLOCK - 1);
        if (ok && pad) {
            unsigned char padbuf[XTAR_BLOCK];
            if (xtar_read_exact(f, padbuf, (size_t)pad) < 0) ok = 0;
        }
        if (!ok) break;
    }
    fclose(f);
    return rc;
}

#endif /* XTOS_USTAR_H */
