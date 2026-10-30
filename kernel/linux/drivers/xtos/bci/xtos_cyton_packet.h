/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _XTOS_CYTON_PACKET_H
#define _XTOS_CYTON_PACKET_H

#ifdef __KERNEL__
#include <linux/string.h>
#include <linux/types.h>
typedef u8 xtos_cyton_u8;
typedef s32 xtos_cyton_s32;
typedef s64 xtos_cyton_s64;
#else
#include <stdint.h>
#include <string.h>
typedef uint8_t xtos_cyton_u8;
typedef int32_t xtos_cyton_s32;
typedef int64_t xtos_cyton_s64;
#endif

#define XTOS_CYTON_PACKET_SIZE 33
#define XTOS_CYTON_CHANNELS 8
#define XTOS_CYTON_START_BYTE 0xA0
#define XTOS_CYTON_VREF_NV 4500000000LL
#define XTOS_CYTON_GAIN 24
#define XTOS_CYTON_SCALE_DENOMINATOR \
    ((xtos_cyton_s64)XTOS_CYTON_GAIN * (((xtos_cyton_s64)1 << 23) - 1))
#define OPENBCI_PACKET_SIZE XTOS_CYTON_PACKET_SIZE
#define OPENBCI_NUM_CHANNELS XTOS_CYTON_CHANNELS
#define OPENBCI_START_BYTE XTOS_CYTON_START_BYTE

typedef char xtos_cyton_s32_must_be_4_bytes[sizeof(xtos_cyton_s32) == 4 ? 1 : -1];

typedef struct xtos_cyton_parser {
    xtos_cyton_u8 packet[XTOS_CYTON_PACKET_SIZE];
    unsigned int packet_pos;
    xtos_cyton_u8 last_counter;
    unsigned int counter_valid;
    /* Saturates at the maximum unsigned long rather than losing history. */
    unsigned long missing_samples;
} xtos_cyton_parser;

/* A null parser is a no-op, matching the other helpers' null safety. */
static inline void xtos_cyton_parser_init(xtos_cyton_parser *parser)
{
    if (!parser)
        return;
    parser->packet_pos = 0;
    parser->last_counter = 0;
    parser->counter_valid = 0;
    parser->missing_samples = 0;
}

/* Decode the atomic packet. On failure, leave the caller's output untouched. */
static inline int xtos_cyton_decode_packet(
    const xtos_cyton_u8 packet[XTOS_CYTON_PACKET_SIZE],
    xtos_cyton_s32 output[XTOS_CYTON_CHANNELS])
{
    xtos_cyton_s32 decoded[XTOS_CYTON_CHANNELS];
    unsigned int channel;

    if (!packet || !output || packet[0] != XTOS_CYTON_START_BYTE ||
        (packet[32] & 0xF0) != 0xC0)
        return -1;

    for (channel = 0; channel < XTOS_CYTON_CHANNELS; ++channel) {
        unsigned int offset = 2 + channel * 3;
        xtos_cyton_s32 raw = ((xtos_cyton_s32)packet[offset] << 16) |
                             ((xtos_cyton_s32)packet[offset + 1] << 8) |
                             (xtos_cyton_s32)packet[offset + 2];
        xtos_cyton_s64 numerator;
        xtos_cyton_s64 magnitude;

        if (raw & 0x800000)
            raw -= (xtos_cyton_s32)1 << 24;
        numerator = (xtos_cyton_s64)raw * XTOS_CYTON_VREF_NV;
        magnitude = numerator < 0 ? -numerator : numerator;
        magnitude /= XTOS_CYTON_SCALE_DENOMINATOR;
        decoded[channel] = (xtos_cyton_s32)(numerator < 0 ? -magnitude : magnitude);
    }

    memcpy(output, decoded, sizeof(decoded));
    return 0;
}

/* Feed one byte. Return 1 for a decoded frame, 0 for incomplete/noise, or -1
 * for a rejected full candidate. A malformed frame retains only its bounded
 * suffix beginning at the next start byte so later frames can resynchronize. */
static inline int xtos_cyton_parser_feed(
    xtos_cyton_parser *parser, xtos_cyton_u8 byte,
    xtos_cyton_s32 output[XTOS_CYTON_CHANNELS])
{
    xtos_cyton_s32 decoded[XTOS_CYTON_CHANNELS];
    xtos_cyton_u8 counter;
    unsigned int index;

    if (!parser || !output)
        return -1;
    if (parser->packet_pos == 0) {
        if (byte != XTOS_CYTON_START_BYTE)
            return 0;
        parser->packet[parser->packet_pos++] = byte;
        return 0;
    }
    if (parser->packet_pos >= XTOS_CYTON_PACKET_SIZE) {
        parser->packet_pos = 0;
        return -1;
    }
    parser->packet[parser->packet_pos++] = byte;
    if (parser->packet_pos < XTOS_CYTON_PACKET_SIZE)
        return 0;
    if (xtos_cyton_decode_packet(parser->packet, decoded) == 0) {
        counter = parser->packet[1];
        if (parser->counter_valid) {
            unsigned int delta = (counter - parser->last_counter) & 0xff;

            if (delta == 0 || delta >= 0x80) {
                parser->packet_pos = 0;
                return -1;
            }
            if (delta > 1) {
                unsigned long missing = delta - 1;
                unsigned long maximum = ~0UL;

                if (parser->missing_samples > maximum - missing)
                    parser->missing_samples = maximum;
                else
                    parser->missing_samples += missing;
            }
        }
        parser->last_counter = counter;
        parser->counter_valid = 1;
        parser->packet_pos = 0;
        memcpy(output, decoded, sizeof(decoded));
        return 1;
    }

    for (index = 1; index < XTOS_CYTON_PACKET_SIZE; ++index) {
        if (parser->packet[index] == XTOS_CYTON_START_BYTE)
            break;
    }
    if (index == XTOS_CYTON_PACKET_SIZE) {
        parser->packet_pos = 0;
    } else {
        unsigned int remaining = XTOS_CYTON_PACKET_SIZE - index;
        memmove(parser->packet, parser->packet + index, remaining);
        parser->packet_pos = remaining;
    }
    return -1;
}

#endif /* _XTOS_CYTON_PACKET_H */
