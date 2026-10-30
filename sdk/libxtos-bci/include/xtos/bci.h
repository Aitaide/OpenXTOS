/*
 * XTOS BCI User-Space Library
 *
 * User-space API for accessing BCI devices
 * Copyright (C) 2026 XTOS Project
 */

#ifndef _XTOS_BCI_H
#define _XTOS_BCI_H

#include <stdint.h>
#include <stdbool.h>
#include <sys/types.h>
#include "bci_config_uapi.h"
#include "bci_snapshot_uapi.h"
#include "bci_frame_uapi.h"

/* Integer frame views. A session must be externally serialized. READ makes no
 * allocations: two maximum-batch buffers are reserved at session creation.
 * Output descriptors and previous valid read bytes are unchanged on failure.
 * Read views expire on the next successful frame read, release, or destroy.
 * Retain data by copying it; do not modify or unmap session-owned pointers.
 */
struct xtos_bci_frame_batch {
    const struct xtos_bci_frame_v1 *records;
    uint32_t frame_count;
    uint64_t cursor, next_cursor, overwritten_frames, stream_generation;
    struct xtos_bci_config_v1 config;
};
struct xtos_bci_frame_snapshot {
    const void *mapping;
    size_t mapping_bytes;
    const struct xtos_bci_frame_snapshot_header *header;
    struct xtos_bci_frame_batch batch;
};

#ifdef __cplusplus
extern "C" {
#endif

/* Opaque session handle */
typedef struct xtos_bci_session xtos_bci_session_t;

/* capacity 1..292, flags 0/NONBLOCK; 0 success, -1/errno. No fallback for
 * unsupported kernels. EPROTO rejects kernel metadata; EOVERFLOW rejects
 * unrepresentable addresses/lengths. Snapshot families exclude each other and
 * frame reads while a token (including pending cleanup) is owned.
 */
int xtos_bci_frame_read(xtos_bci_session_t *session, uint32_t capacity_frames,
                        uint32_t flags, struct xtos_bci_frame_batch *out);
int xtos_bci_frame_snapshot_acquire(xtos_bci_session_t *session,
                                    uint32_t capacity_frames,
                                    struct xtos_bci_frame_snapshot *out);
/* Consume the entire acquired run; mapping stays readable, replay propagates. */
int xtos_bci_frame_snapshot_consume(xtos_bci_session_t *session);
/* Unmap first, then release. Any failure retains remaining ownership for retry;
 * consume is unavailable once unmapped. Release is allowed after disconnect.
 * With no frame snapshot returns 0. Destroy attempts cleanup even if STOP fails.
 */
int xtos_bci_frame_snapshot_release(xtos_bci_session_t *session);

/* Device information */
struct xtos_bci_device_info {
	char name[32];              /* Device name */
	uint32_t dev_id;            /* Device ID */
	uint32_t num_channels;      /* Number of channels */
	uint32_t sampling_rate;     /* Sampling rate (Hz) */
	uint32_t resolution_bits;   /* ADC resolution */
	uint32_t input_range_mv;    /* Input range (mV, integer) */
	uint32_t interface;         /* Interface type */
};

/* Channel configuration */
struct xtos_bci_channel_cfg {
	uint32_t channel;           /* Channel index */
	bool enabled;               /* Enable/disable */
	uint32_t gain;              /* Gain setting */
};

/* Statistics */
struct xtos_bci_statistics {
	uint64_t samples_read;      /* Total samples read */
	uint64_t samples_dropped;   /* Samples dropped */
	uint64_t errors;            /* Error count */
	uint64_t overruns;          /* Ring buffer overruns */
};

/**
 * xtos_bci_session_create - Create a new BCI session
 * @device_path: Path to device node (e.g., "/dev/xtos0")
 *
 * Returns: Session handle on success, NULL on failure
 */
xtos_bci_session_t *xtos_bci_session_create(const char *device_path);

/**
 * xtos_bci_session_destroy - Destroy a BCI session
 * @session: Session handle
 */
void xtos_bci_session_destroy(xtos_bci_session_t *session);

/**
 * xtos_bci_get_info - Get device information
 * @session: Session handle
 * @info: Pointer to info structure (output)
 *
 * Returns: 0 on success, -1 on failure
 */
int xtos_bci_get_info(xtos_bci_session_t *session,
                      struct xtos_bci_device_info *info);

/** Query a validated v1 configuration. On failure, out remains unchanged. */
int xtos_bci_get_config(xtos_bci_session_t *session,
                        struct xtos_bci_config_v1 *out);

/**
 * xtos_bci_start - Start data streaming
 * @session: Session handle
 *
 * Returns: 0 on success, -1 on failure
 */
int xtos_bci_start(xtos_bci_session_t *session);

/**
 * xtos_bci_stop - Stop data streaming
 * @session: Session handle
 *
 * Returns: 0 on success, -1 on failure
 */
int xtos_bci_stop(xtos_bci_session_t *session);

/**
 * xtos_bci_read_samples - Read samples (blocking)
 * @session: Session handle
 * @buffer: Output buffer for samples in microvolts
 * @num_samples: Number of samples to read
 *
 * Returns: Number of samples read, or -1 on failure
 */
ssize_t xtos_bci_read_samples(xtos_bci_session_t *session,
                             float *buffer,
                             size_t num_samples);

/**
 * xtos_bci_set_sampling_rate - Configure sampling rate
 * @session: Session handle
 * @rate: Sampling rate in Hz
 *
 * Returns: 0 on success, -1 on failure
 */
int xtos_bci_set_sampling_rate(xtos_bci_session_t *session, uint32_t rate);

/**
 * xtos_bci_set_channel - Configure channel
 * @session: Session handle
 * @channel: Channel index
 * @enabled: Enable/disable flag
 * @gain: Gain setting
 *
 * Returns: 0 on success, -1 on failure
 */
int xtos_bci_set_channel(xtos_bci_session_t *session,
                        uint32_t channel,
                        bool enabled,
                        uint32_t gain);

/**
 * xtos_bci_calibrate - Run calibration
 * @session: Session handle
 *
 * Returns: 0 on success, -1 on failure
 */
int xtos_bci_calibrate(xtos_bci_session_t *session);

/**
 * xtos_bci_check_impedance - Check electrode impedance
 * @session: Session handle
 * @impedances_kohm: Output array for impedance values (kOhms, integer)
 * @num_channels: Number of channels
 *
 * Returns: 0 on success, -1 on failure
 */
int xtos_bci_check_impedance(xtos_bci_session_t *session,
                            uint32_t *impedances_kohm,
                            uint32_t num_channels);

/**
 * xtos_bci_get_stats - Get statistics
 * @session: Session handle
 * @stats: Pointer to stats structure (output)
 *
 * Returns: 0 on success, -1 on failure
 */
int xtos_bci_get_stats(xtos_bci_session_t *session,
                      struct xtos_bci_statistics *stats);

/**
 * xtos_bci_reset_stats - Reset statistics
 * @session: Session handle
 *
 * Returns: 0 on success, -1 on failure
 */
int xtos_bci_reset_stats(xtos_bci_session_t *session);

/**
 * Immutable native-endian signed 32-bit nanovolt snapshot view.
 * Pointers belong to the session and expire on release/destroy. Consume leaves
 * the mapping readable. Calls on one session must be externally serialized;
 * do not munmap the view or issue snapshot ioctls through get_fd yourself.
 */
struct xtos_bci_snapshot {
	const void *mapping;
	size_t mapping_bytes;
	const struct xtos_bci_snapshot_header *header;
	const int32_t *payload;
	size_t payload_bytes;
};

/**
 * Acquire at most requested_bytes (nonzero, multiple of four), capped by the
 * v1 UAPI to 64 KiB and rounded down to whole channel frames by the kernel.
 * result must be non-NULL; it is zeroed on failure. The mapped header is fully
 * validated before returning any pointers. Only one snapshot may be held.
 * Returns 0 or -1/errno: EINVAL for bad arguments, EBUSY if already held,
 * EPROTO for malformed metadata, EOVERFLOW for unrepresentable mapping size,
 * or the original ioctl/mmap errno. Failed acquisition releases the token;
 * if cleanup release fails, retry snapshot_release before acquiring again.
 */
int xtos_bci_snapshot_acquire(xtos_bci_session_t *session,
                              size_t requested_bytes,
                              struct xtos_bci_snapshot *result);

/**
 * Consume exactly the acquired payload using its original token and cursor.
 * Returns 0 or -1/errno; no snapshot gives ENOENT. Errors (including kernel
 * replay/overwrite/unbind errors) propagate unchanged. Does not auto-release.
 */
int xtos_bci_snapshot_consume(xtos_bci_session_t *session);

/**
 * Unmap locally, then release the acquired token. With no snapshot returns 0.
 * On munmap failure retains mapping/token for retry. On ioctl failure the
 * mapping is already invalid: retains token for retry except ENODEV/ENOENT,
 * which terminate local ownership. All failures return -1 and preserve errno.
 * Destroy also performs this cleanup even if stopping the stream fails.
 */
int xtos_bci_snapshot_release(xtos_bci_session_t *session);

/**
 * xtos_bci_get_mmap_buffer - Refuse unsafe unversioned mmap
 * @session: Session handle
 * @size: Optional output, always set to zero
 *
 * Returns NULL, errno EOPNOTSUPP (EINVAL for NULL session). Use snapshots.
 */
void *xtos_bci_get_mmap_buffer(xtos_bci_session_t *session, size_t *size);

/**
 * xtos_bci_get_fd - Get file descriptor for poll/select
 * @session: Session handle
 *
 * Returns: File descriptor, or -1 on failure
 */
int xtos_bci_get_fd(xtos_bci_session_t *session);

#ifdef __cplusplus
}
#endif

#endif /* _XTOS_BCI_H */
