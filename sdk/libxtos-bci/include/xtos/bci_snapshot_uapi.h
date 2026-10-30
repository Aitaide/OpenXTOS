/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef XTOS_BCI_SNAPSHOT_UAPI_H
#define XTOS_BCI_SNAPSHOT_UAPI_H
#include <stdint.h>
#include <sys/ioctl.h>

#define XTOS_BCI_SNAPSHOT_VERSION 1u
#define XTOS_BCI_SNAPSHOT_MAGIC 0x58424349u
#define XTOS_BCI_SNAPSHOT_MAX_PAYLOAD 65536u
#define XTOS_BCI_UNIT_NANOVOLT 1u

/* Fixed-width native-endian UAPI; all reserved fields must be zero. */
struct xtos_bci_snapshot_request {
    uint32_t version;
    uint32_t flags;
    uint64_t requested_bytes;
    uint64_t mapping_bytes;
    uint64_t payload_bytes;
    uint64_t cursor;
    uint64_t token;
    uint32_t channels;
    uint32_t sample_rate_hz;
    uint64_t reserved[2];
};
struct xtos_bci_snapshot_header {
    uint32_t magic;
    uint32_t version;
    uint32_t header_size;
    uint32_t unit;
    uint64_t payload_bytes;
    uint64_t cursor;
    uint64_t token;
    uint32_t channels;
    uint32_t sample_rate_hz;
    uint32_t sample_size;
    uint32_t reserved0;
    uint64_t reserved1;
};
struct xtos_bci_snapshot_consume {
    uint32_t version;
    uint32_t flags;
    uint64_t token;
    uint64_t cursor;
    uint64_t bytes;
    uint64_t reserved[2];
};
struct xtos_bci_snapshot_release {
    uint32_t version;
    uint32_t flags;
    uint64_t token;
    uint64_t reserved;
};
#define XTOS_BCI_SNAPSHOT_ACQUIRE _IOWR('X', 13, struct xtos_bci_snapshot_request)
#define XTOS_BCI_SNAPSHOT_CONSUME _IOW('X', 14, struct xtos_bci_snapshot_consume)
#define XTOS_BCI_SNAPSHOT_RELEASE _IOW('X', 15, struct xtos_bci_snapshot_release)
#endif
