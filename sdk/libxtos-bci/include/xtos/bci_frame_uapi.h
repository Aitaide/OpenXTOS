/* Independent installed frame ABI; no Linux internal headers. */
#ifndef XTOS_BCI_FRAME_UAPI_H
#define XTOS_BCI_FRAME_UAPI_H
#include <stdint.h>
#include <sys/ioctl.h>
#include "bci_config_uapi.h"
typedef uint64_t xtos_bci_frame_u64 __attribute__((aligned(8)));

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
    uint32_t version;
    uint32_t record_bytes;
    uint32_t channels;
    uint32_t sample_rate_hz;
    uint32_t unit;
    uint32_t host_clock;
    uint32_t quality_flags;
    uint32_t source_flags;
    xtos_bci_frame_u64 host_rx_ns;
    xtos_bci_frame_u64 sequence;
    xtos_bci_frame_u64 stream_generation;
    xtos_bci_frame_u64 config_generation;
    uint32_t device_counter;
    uint32_t counter_bits;
    uint32_t missing_before;
    uint32_t enabled_mask;
    xtos_bci_frame_u64 reserved[2];
    int32_t samples_nv[32];
};

/* READ input: version, flags NONBLOCK/0, capacity_frames and frames_ptr only.
 * ACQUIRE input: version and capacity_frames only. Other input bytes zero.
 * frames_ptr is a numeric user address; validate width and addition before use.
 */
struct xtos_bci_frame_request {
    uint32_t version;
    uint32_t flags;
    uint32_t capacity_frames;
    uint32_t frame_count;
    xtos_bci_frame_u64 frames_ptr;
    xtos_bci_frame_u64 cursor;
    xtos_bci_frame_u64 next_cursor;
    xtos_bci_frame_u64 overwritten_frames;
    xtos_bci_frame_u64 token;
    xtos_bci_frame_u64 stream_generation;
    uint32_t record_bytes;
    uint32_t header_bytes;
    xtos_bci_frame_u64 mapping_bytes;
    xtos_bci_frame_u64 payload_bytes;
    uint32_t reserved[10];
    struct xtos_bci_config_v1 config;
};

/* Immutable mapping: header at 0, config at 128, records at 512, zero tail. */
struct xtos_bci_frame_snapshot_header {
    uint32_t magic;
    uint32_t version;
    uint32_t header_bytes;
    uint32_t record_bytes;
    uint32_t frame_count;
    uint32_t flags;
    uint32_t unit;
    uint32_t host_clock;
    xtos_bci_frame_u64 payload_bytes;
    xtos_bci_frame_u64 cursor;
    xtos_bci_frame_u64 next_cursor;
    xtos_bci_frame_u64 token;
    xtos_bci_frame_u64 stream_generation;
    xtos_bci_frame_u64 config_generation;
    xtos_bci_frame_u64 overwritten_frames;
    xtos_bci_frame_u64 mapping_bytes;
    uint32_t config_bytes;
    uint32_t records_offset;
    uint32_t reserved[6];
};
struct xtos_bci_frame_consume {
    uint32_t version;
    uint32_t flags;
    xtos_bci_frame_u64 token;
    xtos_bci_frame_u64 cursor;
    xtos_bci_frame_u64 frame_count;
    xtos_bci_frame_u64 stream_generation;
    xtos_bci_frame_u64 reserved;
};
struct xtos_bci_frame_release {
    uint32_t version;
    uint32_t flags;
    xtos_bci_frame_u64 token;
    xtos_bci_frame_u64 reserved;
};

#define XTOS_BCI_FRAME_READ _IOWR('X', 17, struct xtos_bci_frame_request)
#define XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE _IOWR('X', 18, struct xtos_bci_frame_request)
#define XTOS_BCI_FRAME_SNAPSHOT_CONSUME _IOW('X', 19, struct xtos_bci_frame_consume)
#define XTOS_BCI_FRAME_SNAPSHOT_RELEASE _IOW('X', 20, struct xtos_bci_frame_release)
#endif
