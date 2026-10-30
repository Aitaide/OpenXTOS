/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _XTOS_CYTON_CONFIG_H
#define _XTOS_CYTON_CONFIG_H

#ifdef __KERNEL__
#include <linux/types.h>
#include <linux/string.h>
#include <linux/errno.h>
#else
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <errno.h>
#endif

struct xtos_cyton_config {
    unsigned int gain[8], enabled_mask;
    unsigned char registers[24];
    unsigned int register_mask;
};

static inline int xtos_cyton_config_equal(const char *s, size_t n,
                                         const char *literal)
{
    size_t length = strlen(literal);
    return n == length && !memcmp(s, literal, length);
}

static inline void xtos_cyton_config_space(const char **p, const char *end)
{
    while (*p < end && (**p == ' ' || **p == '\t')) ++*p;
}

static inline int xtos_cyton_config_hex(unsigned char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

static inline int xtos_cyton_config_field(const char **p, const char *end,
                                         unsigned int *value)
{
    int a, b;
    xtos_cyton_config_space(p, end);
    if ((size_t)(end - *p) < 2) return -EPROTO;
    a = xtos_cyton_config_hex((unsigned char)(*p)[0]);
    b = xtos_cyton_config_hex((unsigned char)(*p)[1]);
    if (a < 0 || b < 0) return -EPROTO;
    *p += 2;
    xtos_cyton_config_space(p, end);
    if (*p == end || *(*p)++ != ',') return -EPROTO;
    *value = (unsigned int)(a * 16 + b);
    return 0;
}

static inline int xtos_cyton_config_row(const char *p, const char *end,
                                       unsigned int expected, unsigned int *value)
{
    unsigned int address, bit;
    if (xtos_cyton_config_field(&p, end, &address) ||
        xtos_cyton_config_field(&p, end, value) || address != expected)
        return -EPROTO;
    for (bit = 0; bit < 8; ++bit) {
        xtos_cyton_config_space(&p, end);
        if (p == end || *p++ != (char)('0' + ((*value >> (7 - bit)) & 1u)))
            return -EPROTO;
        xtos_cyton_config_space(&p, end);
        if (bit < 7 && (p == end || *p++ != ',')) return -EPROTO;
    }
    return p == end ? 0 : -EPROTO;
}

/* Parse official Cyton RREGS/printRegisterName/printHex output:
 * https://github.com/OpenBCI/OpenBCI_Cyton_Library/blob/master/OpenBCI_32bit_Library.cpp
 * Input is immutable, 1..8192 bytes, excluding $$$ and any NUL terminator.
 * Only the eight Board CHnSET rows are mandatory. Optional ADS rows retain
 * their address-indexed bytes and mask; unrelated LIS3DH rows are ignored.
 * Commit the local snapshot only after full validation; errors leave result
 * unchanged. No allocation, floating point, or mutable static state is used.
 */
static inline int xtos_cyton_config_parse(const char *text, size_t length,
                                         struct xtos_cyton_config *result)
{
    static const unsigned int gains[] = {1, 2, 4, 6, 8, 12, 24};
    static const char * const names[24] = {
        "ADS_ID", "CONFIG1", "CONFIG2", "CONFIG3", "LOFF",
        "CH1SET", "CH2SET", "CH3SET", "CH4SET", "CH5SET", "CH6SET", "CH7SET", "CH8SET",
        "BIAS_SENSP", "BIAS_SENSN", "LOFF_SENSP", "LOFF_SENSN", "LOFF_FLIP",
        "LOFF_STATP", "LOFF_STATN", "GPIO", "MISC1", "MISC2", "CONFIG4"
    };
    struct xtos_cyton_config parsed = { {0}, 0, {0}, 0 };
    size_t offset = 0;
    unsigned int header_seen = 0, board = 0;

    if (!text || !result || !length) return -EINVAL;
    if (length > 8192u) return -EMSGSIZE;
    {
        size_t i;
        for (i = 0; i < length; ++i)
            if (text[i] != '\r' && text[i] != '\n' && text[i] != '\t' &&
                ((unsigned char)text[i] < 32 || (unsigned char)text[i] > 126))
                return -EPROTO;
    }
    while (offset < length) {
        size_t start = offset, n;
        const char *line;
        unsigned int ch, value, code, address;
        size_t name_length = 0;
        while (offset < length && text[offset] != '\r' && text[offset] != '\n') ++offset;
        n = offset - start;
        line = text + start;
        if (offset < length) ++offset;
        {
            size_t i;
            for (i = 0; i + 19 <= n; ++i)
                if (!memcmp(line + i, "Daisy ADS Registers", 19)) return -EPROTONOSUPPORT;
        }
        if (xtos_cyton_config_equal(line, n, "Board ADS Registers")) {
            if (header_seen) return -EPROTO;
            header_seen = 1;
            board = 1;
            continue;
        }
        if (xtos_cyton_config_equal(line, n, "LIS3DH Registers")) {
            board = 0;
            continue;
        }
        if (n >= 2 && !memcmp(line, "CH", 2) &&
            (!board || n < 7 || line[2] < '1' || line[2] > '8' ||
             memcmp(line + 3, "SET,", 4))) return -EPROTO;
        if (!board) continue;
        while (name_length < n && line[name_length] != ',') ++name_length;
        for (address = 0; address < 24; ++address)
            if (xtos_cyton_config_equal(line, name_length, names[address])) break;
        if (address == 24) continue;
        if (name_length == n || (parsed.register_mask & (1u << address)) ||
            xtos_cyton_config_row(line + name_length + 1, line + n, address, &value))
            return -EPROTO;
        if (address >= 5 && address <= 12) {
            ch = address - 5;
            code = (value >> 4) & 7u;
            if (code == 7) return -EPROTO;
            parsed.gain[ch] = gains[code];
            if (!(value & 0x80u)) parsed.enabled_mask |= 1u << ch;
        }
        parsed.registers[address] = (unsigned char)value;
        parsed.register_mask |= 1u << address;
    }
    if (!header_seen || (parsed.register_mask & 0x1FE0u) != 0x1FE0u) return -EPROTO;
    *result = parsed;
    return 0;
}
#endif
