/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef XTOS_BCI_FRAME_UAPI_H
#define XTOS_BCI_FRAME_UAPI_H
#include <linux/types.h>
#include <linux/ioctl.h>
#include "xtos_bci_config.h"

#define XTOS_BCI_FRAME_VERSION 1u
#define XTOS_BCI_FRAME_RECORD_BYTES 224u
#define XTOS_BCI_FRAME_HEADER_BYTES 128u
#define XTOS_BCI_FRAME_CONFIG_BYTES 384u
#define XTOS_BCI_FRAME_RECORDS_OFFSET 512u
#define XTOS_BCI_FRAME_MAX_PAYLOAD 65536u
#define XTOS_BCI_FRAME_MAX_BATCH (XTOS_BCI_FRAME_MAX_PAYLOAD / XTOS_BCI_FRAME_RECORD_BYTES)
#define XTOS_BCI_FRAME_SNAPSHOT_MAGIC 0x58424631u
#define XTOS_BCI_FRAME_CLOCK_MONOTONIC 1u
#ifndef XTOS_BCI_UNIT_NANOVOLT
#define XTOS_BCI_UNIT_NANOVOLT 1u
#endif
#define XTOS_BCI_FRAME_NONBLOCK 1u
#define XTOS_BCI_FRAME_SOURCE_SERDEV 1u
#define XTOS_BCI_FRAME_SOURCE_SYNTHETIC 2u
#define XTOS_BCI_FRAME_QUALITY_CONFIG_VERIFIED 1u
#define XTOS_BCI_FRAME_QUALITY_COUNTER_GAP 2u
#define XTOS_BCI_FRAME_QUALITY_RESYNC 4u
#define XTOS_BCI_FRAME_QUALITY_ADC_RAIL 8u
#define XTOS_BCI_FRAME_QUALITY_CONTINUITY_UNKNOWN 16u
#define XTOS_BCI_FRAME_QUALITY_KNOWN 31u

/* Native endian; all reserved/unused positions zero. Integer nanovolts only.
 * host_rx_ns is CLOCK_MONOTONIC after the callback acquires state_lock, before
 * processing that callback. It is not device acquisition or physical latency.
 */
struct xtos_bci_frame_v1 {
    __u32 version;
    __u32 record_bytes;
    __u32 channels;
    __u32 sample_rate_hz;
    __u32 unit;
    __u32 host_clock;
    __u32 quality_flags;
    __u32 source_flags;
    __aligned_u64 host_rx_ns;
    __aligned_u64 sequence;
    __aligned_u64 stream_generation;
    __aligned_u64 config_generation;
    __u32 device_counter;
    __u32 counter_bits;
    __u32 missing_before;
    __u32 enabled_mask;
    __aligned_u64 reserved[2];
    __s32 samples_nv[32];
};

/* READ input: version, flags NONBLOCK/0, capacity_frames and frames_ptr only.
 * ACQUIRE input: version and capacity_frames only. Other input bytes zero.
 * frames_ptr is a numeric user address; validate width and addition before use.
 */
struct xtos_bci_frame_request {
    __u32 version;
    __u32 flags;
    __u32 capacity_frames;
    __u32 frame_count;
    __aligned_u64 frames_ptr;
    __aligned_u64 cursor;
    __aligned_u64 next_cursor;
    __aligned_u64 overwritten_frames;
    __aligned_u64 token;
    __aligned_u64 stream_generation;
    __u32 record_bytes;
    __u32 header_bytes;
    __aligned_u64 mapping_bytes;
    __aligned_u64 payload_bytes;
    __u32 reserved[10];
    struct xtos_bci_config_v1 config;
};

/* Immutable mapping: header at 0, config at 128, records at 512, zero tail. */
struct xtos_bci_frame_snapshot_header {
    __u32 magic;
    __u32 version;
    __u32 header_bytes;
    __u32 record_bytes;
    __u32 frame_count;
    __u32 flags;
    __u32 unit;
    __u32 host_clock;
    __aligned_u64 payload_bytes;
    __aligned_u64 cursor;
    __aligned_u64 next_cursor;
    __aligned_u64 token;
    __aligned_u64 stream_generation;
    __aligned_u64 config_generation;
    __aligned_u64 overwritten_frames;
    __aligned_u64 mapping_bytes;
    __u32 config_bytes;
    __u32 records_offset;
    __u32 reserved[6];
};
struct xtos_bci_frame_consume {
    __u32 version;
    __u32 flags;
    __aligned_u64 token;
    __aligned_u64 cursor;
    __aligned_u64 frame_count;
    __aligned_u64 stream_generation;
    __aligned_u64 reserved;
};
struct xtos_bci_frame_release {
    __u32 version;
    __u32 flags;
    __aligned_u64 token;
    __aligned_u64 reserved;
};

#define XTOS_BCI_FRAME_READ _IOWR('X', 17, struct xtos_bci_frame_request)
#define XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE _IOWR('X', 18, struct xtos_bci_frame_request)
#define XTOS_BCI_FRAME_SNAPSHOT_CONSUME _IOW('X', 19, struct xtos_bci_frame_consume)
#define XTOS_BCI_FRAME_SNAPSHOT_RELEASE _IOW('X', 20, struct xtos_bci_frame_release)
#endif
