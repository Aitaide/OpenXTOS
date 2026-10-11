/*
 * XTOS BCI User-Space Library Implementation
 *
 * Copyright (C) 2026 XTOS Project
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include "xtos/bci.h"
#include "xtos/bci_uapi.h"

/* Session structure */
struct xtos_bci_session {
	int fd;                     /* Device file descriptor */
	char device_path[256];      /* Device path */
	void *mmap_buffer;          /* mmap'd buffer */
	size_t mmap_size;           /* mmap buffer size */
	bool streaming;             /* Streaming state */
	struct xtos_bci_device_info info;  /* Device info cache */
};

xtos_bci_session_t *xtos_bci_session_create(const char *device_path)
{
	xtos_bci_session_t *session;

	if (!device_path) {
		errno = EINVAL;
		return NULL;
	}

	session = calloc(1, sizeof(*session));
	if (!session)
		return NULL;

	/* Open device */
	session->fd = open(device_path, O_RDWR);
	if (session->fd < 0) {
		free(session);
		return NULL;
	}

	strncpy(session->device_path, device_path, sizeof(session->device_path) - 1);
	session->mmap_buffer = NULL;
	session->mmap_size = 0;
	session->streaming = false;

	/* Cache device info */
	struct xtos_bci_info kinfo;
	if (ioctl(session->fd, XTOS_BCI_GET_INFO, &kinfo) == 0) {
		strncpy(session->info.name, kinfo.name, sizeof(session->info.name));
		session->info.dev_id = kinfo.dev_id;
		session->info.num_channels = kinfo.num_channels;
		session->info.sampling_rate = kinfo.sampling_rate;
		session->info.resolution_bits = kinfo.resolution_bits;
		session->info.input_range_mv = kinfo.input_range_mv;
		session->info.interface = kinfo.interface;
	}

	return session;
}

void xtos_bci_session_destroy(xtos_bci_session_t *session)
{
	if (!session)
		return;

	/* Stop streaming if active */
	if (session->streaming)
		xtos_bci_stop(session);

	/* Unmap buffer */
	if (session->mmap_buffer && session->mmap_size > 0) {
		munmap(session->mmap_buffer, session->mmap_size);
	}

	/* Close device */
	if (session->fd >= 0)
		close(session->fd);

	free(session);
}

int xtos_bci_get_info(xtos_bci_session_t *session,
                      struct xtos_bci_device_info *info)
{
	if (!session || !info) {
		errno = EINVAL;
		return -1;
	}

	memcpy(info, &session->info, sizeof(*info));
	return 0;
}

int xtos_bci_start(xtos_bci_session_t *session)
{
	if (!session) {
		errno = EINVAL;
		return -1;
	}

	if (ioctl(session->fd, XTOS_BCI_START_STREAM) < 0)
		return -1;

	session->streaming = true;
	return 0;
}

int xtos_bci_stop(xtos_bci_session_t *session)
{
	if (!session) {
		errno = EINVAL;
		return -1;
	}

	if (ioctl(session->fd, XTOS_BCI_STOP_STREAM) < 0)
		return -1;

	session->streaming = false;
	return 0;
}

ssize_t xtos_bci_read_samples(xtos_bci_session_t *session,
                             float *buffer,
                             size_t num_samples)
{
    int32_t *raw_samples;
    size_t bytes_needed;
    size_t bytes_read = 0;

    if (!session || !buffer || num_samples == 0) {
        errno = EINVAL;
        return -1;
    }
    if (num_samples > SIZE_MAX / sizeof(*raw_samples)) {
        errno = EOVERFLOW;
        return -1;
    }

    bytes_needed = num_samples * sizeof(*raw_samples);
    raw_samples = malloc(bytes_needed);
    if (!raw_samples)
        return -1;

    while (bytes_read < bytes_needed) {
        ssize_t ret = read(session->fd,
                           (char *)raw_samples + bytes_read,
                           bytes_needed - bytes_read);
        if (ret < 0) {
            if (errno == EINTR)
                continue;
            free(raw_samples);
            return -1;
        }
        if (ret == 0)
            break;
        bytes_read += (size_t)ret;
    }

    if (bytes_read % sizeof(*raw_samples) != 0) {
        free(raw_samples);
        errno = EIO;
        return -1;
    }

    for (size_t index = 0; index < bytes_read / sizeof(*raw_samples); index++)
        buffer[index] = (float)raw_samples[index] / 1000.0f;

    bytes_read /= sizeof(*raw_samples);
    free(raw_samples);
    return (ssize_t)bytes_read;
}

int xtos_bci_set_sampling_rate(xtos_bci_session_t *session, uint32_t rate)
{
	if (!session) {
		errno = EINVAL;
		return -1;
	}

	if (ioctl(session->fd, XTOS_BCI_SET_RATE, &rate) < 0)
		return -1;

	session->info.sampling_rate = rate;
	return 0;
}

int xtos_bci_set_channel(xtos_bci_session_t *session,
                        uint32_t channel,
                        bool enabled,
                        uint32_t gain)
{
	struct xtos_bci_channel_config cfg;

	if (!session) {
		errno = EINVAL;
		return -1;
	}

	cfg.channel = channel;
	cfg.enabled = enabled ? 1 : 0;
	cfg.gain = gain;

	return ioctl(session->fd, XTOS_BCI_SET_CHANNEL, &cfg);
}

int xtos_bci_calibrate(xtos_bci_session_t *session)
{
	if (!session) {
		errno = EINVAL;
		return -1;
	}

	return ioctl(session->fd, XTOS_BCI_CALIBRATE);
}

int xtos_bci_check_impedance(xtos_bci_session_t *session,
                            uint32_t *impedances_kohm,
                            uint32_t num_channels)
{
	uint32_t temp_impedances[32];

	if (!session || !impedances_kohm || num_channels == 0 || num_channels > 32) {
		errno = EINVAL;
		return -1;
	}

	if (ioctl(session->fd, XTOS_BCI_CHECK_IMP, temp_impedances) < 0)
		return -1;

	memcpy(impedances_kohm, temp_impedances, num_channels * sizeof(uint32_t));
	return 0;
}

int xtos_bci_get_stats(xtos_bci_session_t *session,
                      struct xtos_bci_statistics *stats)
{
	struct xtos_bci_stats kstats;

	if (!session || !stats) {
		errno = EINVAL;
		return -1;
	}

	if (ioctl(session->fd, XTOS_BCI_GET_STATS, &kstats) < 0)
		return -1;

	stats->samples_read = kstats.samples_read;
	stats->samples_dropped = kstats.samples_dropped;
	stats->errors = kstats.errors;
	stats->overruns = kstats.overruns;

	return 0;
}

int xtos_bci_reset_stats(xtos_bci_session_t *session)
{
	if (!session) {
		errno = EINVAL;
		return -1;
	}

	return ioctl(session->fd, XTOS_BCI_RESET_STATS);
}

void *xtos_bci_get_mmap_buffer(xtos_bci_session_t *session, size_t *size)
{
	if (!session) {
		errno = EINVAL;
		return NULL;
	}

	/* If already mapped, return existing buffer */
	if (session->mmap_buffer && session->mmap_size > 0) {
		if (size)
			*size = session->mmap_size;
		return session->mmap_buffer;
	}

	/* Calculate buffer size (2 seconds of data) */
	session->mmap_size = session->info.num_channels *
	                     session->info.sampling_rate *
	                     sizeof(float) * 2;

	/* Map the ring buffer */
	session->mmap_buffer = mmap(NULL, session->mmap_size,
	                           PROT_READ, MAP_SHARED,
	                           session->fd, 0);
	if (session->mmap_buffer == MAP_FAILED) {
		session->mmap_buffer = NULL;
		session->mmap_size = 0;
		return NULL;
	}

	if (size)
		*size = session->mmap_size;

	return session->mmap_buffer;
}

int xtos_bci_get_fd(xtos_bci_session_t *session)
{
	if (!session) {
		errno = EINVAL;
		return -1;
	}

	return session->fd;
}
