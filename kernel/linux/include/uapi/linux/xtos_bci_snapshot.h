/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef _UAPI_XTOS_BCI_SNAPSHOT_H
#define _UAPI_XTOS_BCI_SNAPSHOT_H
#include <linux/types.h>
#include <linux/ioctl.h>

#define XTOS_BCI_SNAPSHOT_VERSION 1u
#define XTOS_BCI_SNAPSHOT_MAGIC 0x58424349u
#define XTOS_BCI_SNAPSHOT_MAX_PAYLOAD 65536u
#define XTOS_BCI_UNIT_NANOVOLT 1u

/* Fixed-width native-endian UAPI; all reserved fields must be zero. */
struct xtos_bci_snapshot_request {
    __u32 version;
    __u32 flags;
    __aligned_u64 requested_bytes;
    __aligned_u64 mapping_bytes;
    __aligned_u64 payload_bytes;
    __aligned_u64 cursor;
    __aligned_u64 token;
    __u32 channels;
    __u32 sample_rate_hz;
    __aligned_u64 reserved[2];
};
struct xtos_bci_snapshot_header {
    __u32 magic;
    __u32 version;
    __u32 header_size;
    __u32 unit;
    __aligned_u64 payload_bytes;
    __aligned_u64 cursor;
    __aligned_u64 token;
    __u32 channels;
    __u32 sample_rate_hz;
    __u32 sample_size;
    __u32 reserved0;
    __aligned_u64 reserved1;
};
struct xtos_bci_snapshot_consume {
    __u32 version;
    __u32 flags;
    __aligned_u64 token;
    __aligned_u64 cursor;
    __aligned_u64 bytes;
    __aligned_u64 reserved[2];
};
struct xtos_bci_snapshot_release {
    __u32 version;
    __u32 flags;
    __aligned_u64 token;
    __aligned_u64 reserved;
};
#define XTOS_BCI_SNAPSHOT_ACQUIRE _IOWR('X', 13, struct xtos_bci_snapshot_request)
#define XTOS_BCI_SNAPSHOT_CONSUME _IOW('X', 14, struct xtos_bci_snapshot_consume)
#define XTOS_BCI_SNAPSHOT_RELEASE _IOW('X', 15, struct xtos_bci_snapshot_release)
#endif
