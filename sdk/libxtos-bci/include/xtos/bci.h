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

#ifdef __cplusplus
extern "C" {
#endif

/* Opaque session handle */
typedef struct xtos_bci_session xtos_bci_session_t;

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
 * @buffer: Output buffer for samples
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
 * xtos_bci_get_mmap_buffer - Get mmap buffer for zero-copy access
 * @session: Session handle
 * @size: Pointer to store buffer size (output)
 *
 * Returns: Pointer to mmap'd buffer, or NULL on failure
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
