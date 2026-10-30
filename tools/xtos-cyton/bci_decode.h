/* SPDX-License-Identifier: GPL-2.0 */
#ifndef BCI_DECODE_H
#define BCI_DECODE_H
#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <string.h>
#include "../../kernel/linux/drivers/xtos/bci/xtos_cyton_packet.h"

/* Actual per-channel gain; reject unrepresentable signed nV, never clip. */
static inline int bci_decode_gains(const uint8_t packet[33], const uint32_t gain[8], int32_t output[8])
{
    int32_t decoded[8];
    if (!packet || !gain || !output || packet[0] != 0xa0 || (packet[32] & 0xf0) != 0xc0) {
        errno = EINVAL;
        return -1;
    }
    for (unsigned ch = 0; ch < 8; ch++) {
        int32_t raw = ((int32_t)packet[2 + ch*3] << 16) |
                      ((int32_t)packet[3 + ch*3] << 8) | packet[4 + ch*3];
        int64_t nv;
        switch (gain[ch]) {
        case 1: case 2: case 4: case 6: case 8: case 12: case 24: break;
        default: errno = EINVAL; return -1;
        }
        if (raw & 0x800000) raw -= 0x1000000;
        nv = (int64_t)raw * INT64_C(4500000000) / ((int64_t)gain[ch] * 8388607);
        if (nv < INT32_MIN || nv > INT32_MAX) { errno = ERANGE; return -1; }
        decoded[ch] = (int32_t)nv;
    }
    memcpy(output, decoded, sizeof(decoded));
    return 0;
}
#endif
