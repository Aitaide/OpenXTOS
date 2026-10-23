/* xtos_gzip.h — self-contained gzip (RFC 1952) + DEFLATE (RFC 1951) reader.
 *
 * Header-only static implementation (adapted from the public-domain puff.c
 * reference design by Mark Adler). The consumer compiles a single
 * translation unit that #includes this file — no external binary, no
 * third-party shared library.
 *
 * Guarantees: verifies the gzip magic/CM/flags, skips FEXTRA/FNAME/FCOMMENT/
 * FHCRC fields, decodes all block types (stored, fixed, dynamic), verifies
 * the per-member CRC32 and ISIZE trailer, and accepts concatenated members
 * plus zero padding. Anything else (premature end, bad codes, bad trailer,
 * non-zero trailing garbage) is rejected.
 *
 * Output goes to a FILE* through a 64 KiB buffer that keeps a 32 KiB LZ77
 * history window, so arbitrary back-references are correct.
 */
#ifndef XTOS_GZIP_H
#define XTOS_GZIP_H

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define XZ_MAXBITS 15
#define XZ_HIST    32768u
#define XZ_BUF     65536u

struct xz_huff {
    short count[XZ_MAXBITS + 1];
    short *symbol;
};

struct xz {
    FILE *in;
    FILE *out;
    int bitbuf;
    int bitcnt;
    int err;                 /* sticky: 1 unexpected end of input, 2 output error */
    uint32_t crc;
    uint64_t total;
    unsigned char outbuf[XZ_BUF + 64];
    size_t outpos;           /* bytes pending in outbuf (history included) */
    short lensym[288 + 4];
    short distsym[32];
    short clensym[19];
    struct xz_huff lencode, distcode, clencode;
    short lengths[288 + 32];
};

/* CRC-32 (ISO 3309 / ITU-T V.42), right-shift polynomial 0xEDB88320. */
static uint32_t xz_crc_tab[256];
static int xz_crc_ready;

static void xz_crc_init(void) {
    for (uint32_t n = 0; n < 256; n++) {
        uint32_t c = n;
        for (int k = 0; k < 8; k++)
            c = (c & 1) ? 0xEDB88320u ^ (c >> 1) : c >> 1;
        xz_crc_tab[n] = c;
    }
    xz_crc_ready = 1;
}

static uint32_t xz_crc_update(uint32_t crc, const unsigned char *buf, size_t len) {
    if (!xz_crc_ready) xz_crc_init();
    uint32_t c = crc;
    for (size_t i = 0; i < len; i++)
        c = xz_crc_tab[(c ^ buf[i]) & 0xff] ^ (c >> 8);
    return c;
}

/* Flush ALL pending output at the end of a deflate stream. */
static int xz_flush_all(struct xz *s) {
    if (s->outpos && fwrite(s->outbuf, 1, s->outpos, s->out) != s->outpos) { s->err = 2; return -1; }
    s->crc = xz_crc_update(s->crc, s->outbuf, s->outpos);
    s->total += s->outpos;
    s->outpos = 0;
    return 0;
}

static int xz_put(struct xz *s, unsigned char b) {
    if (s->outpos == XZ_BUF) {
        if (fwrite(s->outbuf, 1, XZ_BUF - XZ_HIST, s->out) != XZ_BUF - XZ_HIST) { s->err = 2; return -1; }
        s->crc = xz_crc_update(s->crc, s->outbuf, XZ_BUF - XZ_HIST);
        s->total += XZ_BUF - XZ_HIST;
        memmove(s->outbuf, s->outbuf + (XZ_BUF - XZ_HIST), XZ_HIST);
        s->outpos = XZ_HIST;
    }
    s->outbuf[s->outpos++] = b;
    return 0;
}

/* need <= 16 bits */
static int xz_bits(struct xz *s, int need) {
    long val = s->bitbuf;
    while (s->bitcnt < need) {
        int c = getc(s->in);
        if (c == EOF) { s->err = 1; return 0; }
        val |= (long)c << s->bitcnt;
        s->bitcnt += 8;
    }
    s->bitbuf = (int)(val >> need);
    s->bitcnt -= need;
    return (int)(val & ((1L << need) - 1));
}

/* Build a canonical Huffman decode table (counting-sort, puff style).
 * Returns the number of incomplete codes (0 = complete, <0 = oversubscribed). */
static int xz_construct(struct xz_huff *h, const short *length, int n) {
    int symbol, len, left;
    short offs[XZ_MAXBITS + 1];
    for (len = 0; len <= XZ_MAXBITS; len++) h->count[len] = 0;
    for (symbol = 0; symbol < n; symbol++) h->count[length[symbol]]++;
    if (h->count[0] == n) return 0;
    left = 1;
    for (len = 1; len <= XZ_MAXBITS; len++) {
        left <<= 1;
        left -= h->count[len];
        if (left < 0) return left;
    }
    offs[1] = 0;
    for (len = 1; len < XZ_MAXBITS; len++) offs[len + 1] = offs[len] + h->count[len];
    for (symbol = 0; symbol < n; symbol++)
        if (length[symbol]) h->symbol[offs[length[symbol]]++] = symbol;
    return left;
}

static int xz_decode(struct xz *s, const struct xz_huff *h) {
    int len, code, first, count, index;
    code = first = index = 0;
    for (len = 1; len <= XZ_MAXBITS; len++) {
        if (s->err) return -1;
        code |= xz_bits(s, 1);
        if (s->err) return -1;
        count = h->count[len];
        if (code - count < first) return h->symbol[index + (code - first)];
        index += count;
        first += count;
        first <<= 1;
        code <<= 1;
    }
    return -1; /* ran out of codes */
}

static int xz_stored(struct xz *s) {
    unsigned len;
    s->bitbuf = 0;
    s->bitcnt = 0;
    int b0 = getc(s->in), b1 = getc(s->in), b2 = getc(s->in), b3 = getc(s->in);
    if (b0 == EOF || b1 == EOF || b2 == EOF || b3 == EOF) { s->err = 1; return -1; }
    len = (unsigned)b0 | ((unsigned)b1 << 8);
    if (b2 != (int)(~len & 0xff) || b3 != (int)((~len >> 8) & 0xff)) return -1;
    while (len--) {
        int c = getc(s->in);
        if (c == EOF) { s->err = 1; return -1; }
        if (xz_put(s, (unsigned char)c) < 0) return -1;
    }
    return 0;
}

static int xz_fixed(struct xz *s) {
    int symbol;
    for (symbol = 0; symbol < 144; symbol++) s->lengths[symbol] = 8;
    for (; symbol < 256; symbol++) s->lengths[symbol] = 9;
    for (; symbol < 280; symbol++) s->lengths[symbol] = 7;
    for (; symbol < 288; symbol++) s->lengths[symbol] = 8;
    s->lencode.symbol = s->lensym;
    if (xz_construct(&s->lencode, s->lengths, 288) < 0) return -1;
    for (symbol = 0; symbol < 30; symbol++) s->lengths[symbol] = 5;
    s->distcode.symbol = s->distsym;
    if (xz_construct(&s->distcode, s->lengths, 30) < 0) return -1;
    return 0;
}

static int xz_dynamic(struct xz *s) {
    static const short order[19] = {16,17,18,0,8,7,9,6,10,5,11,4,12,3,13,2,14,1,15};
    int nlen = xz_bits(s, 5) + 257, ndist = xz_bits(s, 5) + 1, ncode = xz_bits(s, 4) + 4;
    if (s->err) return -1;
    if (nlen > 286 || ndist > 30) return -1;
    for (int index = 0; index < ncode; index++) s->lengths[order[index]] = (short)xz_bits(s, 3);
    if (s->err) return -1;
    for (int index = ncode; index < 19; index++) s->lengths[order[index]] = 0;
    s->clencode.symbol = s->clensym;
    int err = xz_construct(&s->clencode, s->lengths, 19);
    if (err < 0) return -1;
    int index = 0;
    while (index < nlen + ndist) {
        int symbol = xz_decode(s, &s->clencode);
        if (s->err || symbol < 0) return -1;
        if (symbol < 16) {
            s->lengths[index++] = (short)symbol;
        } else {
            int len = 0, rep;
            if (symbol == 16) {
                if (index == 0) return -1;
                len = s->lengths[index - 1];
                rep = 3 + xz_bits(s, 2);
            } else if (symbol == 17) {
                rep = 3 + xz_bits(s, 3);
            } else {
                rep = 11 + xz_bits(s, 7);
            }
            if (s->err) return -1;
            if (index + rep > nlen + ndist) return -1;
            while (rep--) s->lengths[index++] = (short)len;
        }
    }
    if (s->lengths[256] == 0) return -1; /* no end-of-block code */
    s->lencode.symbol = s->lensym;
    err = xz_construct(&s->lencode, s->lengths, nlen);
    if (err && (err < 0 || nlen != s->lencode.count[0] + s->lencode.count[1])) return -1;
    s->distcode.symbol = s->distsym;
    err = xz_construct(&s->distcode, s->lengths + nlen, ndist);
    if (err && (err < 0 || ndist != s->distcode.count[0] + s->distcode.count[1])) return -1;
    return 0;
}

static int xz_codes(struct xz *s) {
    static const short lens[29] = {3,4,5,6,7,8,9,10,11,13,15,17,19,23,27,31,35,43,51,59,67,83,99,115,131,163,195,227,258};
    static const short lext[29] = {0,0,0,0,0,0,0,0,1,1,1,1,2,2,2,2,3,3,3,3,4,4,4,4,5,5,5,5,0};
    static const short dists[30] = {1,2,3,4,5,7,9,13,17,25,33,49,65,97,129,193,257,385,513,769,1025,1537,2049,3073,4097,6145,8193,12289,16385,24577};
    static const short dext[30] = {0,0,0,0,1,1,2,2,3,3,4,4,5,5,6,6,7,7,8,8,9,9,10,10,11,11,12,12,13,13};
    int symbol;
    do {
        if (s->err) return -1;
        symbol = xz_decode(s, &s->lencode);
        if (s->err || symbol < 0) return -1;
        if (symbol < 256) {
            if (xz_put(s, (unsigned char)symbol) < 0) return -1;
        } else if (symbol > 256) {
            symbol -= 257;
            if (symbol >= 29) return -1;
            int len = lens[symbol] + xz_bits(s, lext[symbol]);
            if (s->err) return -1;
            int dist_sym = xz_decode(s, &s->distcode);
            if (s->err || dist_sym < 0) return -1;
            if (dist_sym >= 30) return -1;
            size_t dist = (size_t)dists[dist_sym] + (size_t)xz_bits(s, dext[dist_sym]);
            if (s->err) return -1;
            if (dist == 0 || dist > s->outpos) return -1; /* before start of output */
            while (len--) {
                unsigned char b = s->outbuf[s->outpos - dist];
                if (xz_put(s, b) < 0) return -1;
            }
        }
    } while (symbol != 256); /* end-of-block */
    return 0;
}

static int xz_blocks(struct xz *s) {
    int last, type;
    do {
        last = xz_bits(s, 1);
        if (s->err) return -1;
        type = xz_bits(s, 2);
        if (s->err) return -1;
        if (type == 0) {
            if (xz_stored(s) < 0) return -1;
        } else if (type == 1) {
            if (xz_fixed(s) < 0) return -1;
            if (xz_codes(s) < 0) return -1;
        } else if (type == 2) {
            if (xz_dynamic(s) < 0) return -1;
            if (xz_codes(s) < 0) return -1;
        } else {
            return -1; /* reserved block type */
        }
    } while (!last);
    s->bitbuf = 0;
    s->bitcnt = 0;
    return 0;
}

/* Parse one gzip member header. */
static int xz_gz_header(struct xz *s) {
    unsigned char h[10];
    if (fread(h, 1, 10, s->in) != 10) return -1;
    if (h[0] != 0x1f || h[1] != 0x8b || h[2] != 8) return -1; /* magic + deflate */
    unsigned flg = h[3];
    if (flg & 0xE0) return -1; /* reserved flags */
    if (flg & 4) { /* FEXTRA */
        int x0 = getc(s->in), x1 = getc(s->in);
        if (x0 == EOF || x1 == EOF) return -1;
        int skip = x0 | (x1 << 8);
        while (skip--) if (getc(s->in) == EOF) return -1;
    }
    if (flg & 8) { while (getc(s->in) > 0) {} } /* FNAME */
    if (flg & 16) { while (getc(s->in) > 0) {} } /* FCOMMENT */
    if (flg & 2) { if (getc(s->in) == EOF || getc(s->in) == EOF) return -1; } /* FHCRC */
    return 0;
}

/* Decompress the whole gzip stream from s->in into s->out.
 * Returns 0 on success, -1 on any malformed input or output error. */
static int xz_gzip_to_file(FILE *in, FILE *out) {
    struct xz s;
    memset(&s, 0, sizeof(s));
    s.in = in;
    s.out = out;
    for (;;) {
        if (xz_gz_header(&s) < 0) return -1;
        s.crc = 0xffffffffu; /* raw CRC register; conditioned at the compare */
        s.total = 0;
        if (xz_blocks(&s) < 0) return -1;
        if (xz_flush_all(&s) < 0) return -1;
        unsigned char trailer[8];
        if (fread(trailer, 1, 8, s.in) != 8) return -1;
        uint32_t want_crc = (uint32_t)trailer[0] | ((uint32_t)trailer[1] << 8) | ((uint32_t)trailer[2] << 16) | ((uint32_t)trailer[3] << 24);
        uint32_t want_len = (uint32_t)trailer[4] | ((uint32_t)trailer[5] << 8) | ((uint32_t)trailer[6] << 16) | ((uint32_t)trailer[7] << 24);
        if ((s.crc ^ 0xffffffffu) != want_crc || (uint32_t)(s.total & 0xffffffffu) != want_len) return -1;
        int p0 = getc(s.in);
        if (p0 == EOF) break;
        if (p0 == 0x1f) {
            int p1 = getc(s.in);
            if (p1 == EOF) return -1;
            if (p1 != 0x8b) return -1;
            ungetc(p1, s.in);
            ungetc(p0, s.in);
            s.bitbuf = 0;
            s.bitcnt = 0;
            continue; /* concatenated member */
        }
        if (p0 != 0) return -1; /* trailing garbage */
        for (;;) { /* allow zero padding only */
            int c = getc(s.in);
            if (c == EOF) break;
            if (c != 0) return -1;
        }
        break;
    }
    if (fflush(out) != 0) return -1;
    return 0;
}

#endif /* XTOS_GZIP_H */
