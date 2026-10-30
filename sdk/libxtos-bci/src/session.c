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
#include <stddef.h>
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
	struct xtos_bci_snapshot_request snapshot;
	bool snapshot_active;
	bool snapshot_valid;
	bool streaming;             /* Streaming state */
	struct xtos_bci_device_info info;  /* Device info cache */
	struct xtos_bci_frame_v1 *frame_staging, *frame_published;
	struct xtos_bci_frame_request frame_snapshot;
	bool frame_active, frame_valid;
	void *frame_mapping;
	size_t frame_mapping_size;
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

	session->frame_staging = calloc(XTOS_BCI_FRAME_MAX_BATCH,
	                                sizeof(*session->frame_staging));
	session->frame_published = calloc(XTOS_BCI_FRAME_MAX_BATCH,
	                                  sizeof(*session->frame_published));
	if (!session->frame_staging || !session->frame_published) {
		int saved_errno = errno;
		close(session->fd);
		free(session->frame_staging);
		free(session->frame_published);
		free(session);
		errno = saved_errno;
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

	/* Release local mapping and token even if STOP failed or device unbound. */
	xtos_bci_snapshot_release(session);
	xtos_bci_frame_snapshot_release(session);

	/* Close device */
	if (session->fd >= 0)
		close(session->fd);

	free(session->frame_staging);
	free(session->frame_published);
	free(session);
}

static struct xtos_bci_frame_batch frame_batch(
    const struct xtos_bci_frame_request *r, const struct xtos_bci_frame_v1 *records)
{
    struct xtos_bci_frame_batch b = {0};
    b.records = records; b.frame_count = r->frame_count;
    b.cursor = r->cursor; b.next_cursor = r->next_cursor;
    b.overwritten_frames = r->overwritten_frames;
    b.stream_generation = r->stream_generation; b.config = r->config;
    return b;
}

static int frame_request_valid(const struct xtos_bci_frame_request *r,
                               uint32_t capacity, uint32_t flags,
                               uint64_t pointer, int snapshot);
static int frame_records_valid(const struct xtos_bci_frame_request *r,
                               const struct xtos_bci_frame_v1 *records);

int xtos_bci_frame_read(xtos_bci_session_t *s, uint32_t capacity,
                        uint32_t flags, struct xtos_bci_frame_batch *out)
{
    struct xtos_bci_frame_request r = {0};
    if (!s || !out || !capacity || capacity > XTOS_BCI_FRAME_MAX_BATCH ||
        (flags & ~XTOS_BCI_FRAME_NONBLOCK)) { errno = EINVAL; return -1; }
    if (s->snapshot_active || s->frame_active) { errno = EBUSY; return -1; }
    if ((uintptr_t)s->frame_staging > UINTPTR_MAX -
        (size_t)capacity * sizeof(*s->frame_staging)) { errno = EOVERFLOW; return -1; }
    r.version = XTOS_BCI_FRAME_VERSION; r.flags = flags;
    r.capacity_frames = capacity; r.frames_ptr = (uintptr_t)s->frame_staging;
    if (ioctl(s->fd, XTOS_BCI_FRAME_READ, &r) < 0) return -1;
    if (frame_request_valid(&r, capacity, flags, (uintptr_t)s->frame_staging, 0) < 0 ||
        frame_records_valid(&r, s->frame_staging) < 0) return -1;
    memcpy(s->frame_published, s->frame_staging, (size_t)r.payload_bytes);
    *out = frame_batch(&r, s->frame_published);
    return 0;
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

static int config_valid(const struct xtos_bci_config_v1 *config)
{
	static const uint32_t allowed_gains[] = {1, 2, 4, 6, 8, 12, 24};
	const char *firmware_end;

	if (config->version != XTOS_BCI_CONFIG_VERSION || config->flags ||
	    config->channels != 8 || config->sample_rate_hz != 250 ||
	    config->enabled_mask > 0xff || config->generation == 0 ||
	    (config->capabilities & ~0x1fu) ||
	    (config->capabilities & (XTOS_BCI_CONFIG_READBACK |
	                             XTOS_BCI_FIRMWARE_READBACK)) !=
	        (XTOS_BCI_CONFIG_READBACK | XTOS_BCI_FIRMWARE_READBACK) ||
	    config->reserved0[0] || config->reserved0[1]) {
		errno = EPROTO;
		return -1;
	}
	for (size_t i = 0; i < sizeof(config->reserved) / sizeof(config->reserved[0]); i++) {
		if (config->reserved[i]) {
			errno = EPROTO;
			return -1;
		}
	}
	if ((config->registers[1] & 0x07u) != 0x06u) {
		errno = EPROTO;
		return -1;
	}
	for (size_t channel = 0; channel < 8; channel++) {
		uint8_t channel_register = config->registers[5 + channel];
		int gain_matches = 0;

		for (size_t code = 0; code < sizeof(allowed_gains) / sizeof(allowed_gains[0]); code++) {
			if (config->gain[channel] == allowed_gains[code] &&
			    ((channel_register >> 4) & 0x07u) == code) {
				gain_matches = 1;
				break;
			}
		}
		if (!gain_matches ||
		    !!(channel_register & 0x80u) ==
		        !!(config->enabled_mask & (1u << channel))) {
			errno = EPROTO;
			return -1;
		}
	}
	firmware_end = memchr(config->firmware, '\0', sizeof(config->firmware));
	if (!firmware_end || firmware_end == config->firmware) {
		errno = EPROTO;
		return -1;
	}
	for (const unsigned char *p = (const unsigned char *)config->firmware;
	     p < (const unsigned char *)firmware_end; p++) {
		if (*p != '\r' && *p != '\n' && (*p < 0x20 || *p > 0x7e)) {
			errno = EPROTO;
			return -1;
		}
	}
	return 0;
}

static int bytes_zero(const void *p, size_t n)
{
    const unsigned char *b = p;
    for (size_t i = 0; i < n; i++) if (b[i]) return 0;
    return 1;
}

static int frame_request_valid(const struct xtos_bci_frame_request *r,
                               uint32_t capacity, uint32_t flags,
                               uint64_t pointer, int snapshot)
{
    if (snapshot && (r->mapping_bytes > SIZE_MAX || r->mapping_bytes > PTRDIFF_MAX)) {
        errno = EOVERFLOW; return -1;
    }
    if (r->version != XTOS_BCI_FRAME_VERSION || r->flags != flags ||
        r->capacity_frames != capacity || r->frames_ptr != pointer ||
        r->frame_count > capacity || !bytes_zero(r->reserved, sizeof(r->reserved)))
        goto protocol;
    if (!r->frame_count && !snapshot) {
        struct xtos_bci_frame_request expected = {0};
        expected.version = XTOS_BCI_FRAME_VERSION; expected.flags = flags;
        expected.capacity_frames = capacity; expected.frames_ptr = pointer;
        if (memcmp(r, &expected, sizeof(expected))) goto protocol;
        return 0;
    }
    if (!r->frame_count || !r->stream_generation ||
        r->record_bytes != sizeof(struct xtos_bci_frame_v1) ||
        r->header_bytes != sizeof(struct xtos_bci_frame_snapshot_header) ||
        r->payload_bytes != (uint64_t)r->frame_count * sizeof(struct xtos_bci_frame_v1) ||
        r->cursor > UINT64_MAX - r->frame_count ||
        r->next_cursor != r->cursor + r->frame_count) goto protocol;
    if (snapshot) {
        long page = sysconf(_SC_PAGESIZE);
        uint64_t used = XTOS_BCI_FRAME_RECORDS_OFFSET + r->payload_bytes;
        if (page <= 0 || !r->token || r->mapping_bytes < used ||
            r->mapping_bytes % (uint64_t)page ||
            r->mapping_bytes - used >= (uint64_t)page) goto protocol;
    } else if (r->mapping_bytes || r->token) goto protocol;
    return config_valid(&r->config);
protocol:
    errno = EPROTO; return -1;
}

static int frame_records_valid(const struct xtos_bci_frame_request *r,
                               const struct xtos_bci_frame_v1 *records)
{
    for (uint32_t i = 0; i < r->frame_count; i++) {
        const struct xtos_bci_frame_v1 *f = records + i;
        if (f->version != XTOS_BCI_FRAME_VERSION || f->record_bytes != sizeof(*f) ||
            f->channels != r->config.channels || f->sample_rate_hz != r->config.sample_rate_hz ||
            f->unit != XTOS_BCI_UNIT_NANOVOLT || f->host_clock != XTOS_BCI_FRAME_CLOCK_MONOTONIC ||
            (f->quality_flags & ~XTOS_BCI_FRAME_QUALITY_KNOWN) ||
            !(f->quality_flags & XTOS_BCI_FRAME_QUALITY_CONFIG_VERIFIED) ||
            (f->source_flags != XTOS_BCI_FRAME_SOURCE_SERDEV &&
             f->source_flags != XTOS_BCI_FRAME_SOURCE_SYNTHETIC) ||
            !f->host_rx_ns || f->sequence != r->cursor + i + 1 ||
            f->stream_generation != r->stream_generation ||
            f->config_generation != r->config.generation ||
            f->enabled_mask != r->config.enabled_mask || f->counter_bits != 8 ||
            f->device_counter > 255 || f->missing_before > 126 ||
            !!(f->quality_flags & XTOS_BCI_FRAME_QUALITY_COUNTER_GAP) != !!f->missing_before ||
            !bytes_zero(f->reserved, sizeof(f->reserved)) ||
            !bytes_zero(f->samples_nv + 8, 24 * sizeof(f->samples_nv[0])) ||
            (i && (f->host_rx_ns < records[i-1].host_rx_ns ||
                   f->source_flags != records[0].source_flags))) {
            errno = EPROTO; return -1;
        }
    }
    return 0;
}

static int frame_mapping_valid(const void *mapping,
                               const struct xtos_bci_frame_request *r)
{
    const struct xtos_bci_frame_snapshot_header *h = mapping;
    const unsigned char *b = mapping;
    const struct xtos_bci_frame_v1 *records =
        (const struct xtos_bci_frame_v1 *)(b + XTOS_BCI_FRAME_RECORDS_OFFSET);
    size_t used = XTOS_BCI_FRAME_RECORDS_OFFSET + (size_t)r->payload_bytes;
    if (h->magic != XTOS_BCI_FRAME_SNAPSHOT_MAGIC || h->version != r->version ||
        h->header_bytes != r->header_bytes || h->record_bytes != r->record_bytes ||
        h->frame_count != r->frame_count || h->flags ||
        h->unit != XTOS_BCI_UNIT_NANOVOLT || h->host_clock != XTOS_BCI_FRAME_CLOCK_MONOTONIC ||
        h->payload_bytes != r->payload_bytes || h->cursor != r->cursor ||
        h->next_cursor != r->next_cursor || h->token != r->token ||
        h->stream_generation != r->stream_generation ||
        h->config_generation != r->config.generation ||
        h->overwritten_frames != r->overwritten_frames || h->mapping_bytes != r->mapping_bytes ||
        h->config_bytes != sizeof(r->config) || h->records_offset != XTOS_BCI_FRAME_RECORDS_OFFSET ||
        !bytes_zero(h->reserved, sizeof(h->reserved)) ||
        memcmp(b + XTOS_BCI_FRAME_HEADER_BYTES, &r->config, sizeof(r->config)) ||
        !bytes_zero(b + used, (size_t)r->mapping_bytes - used)) {
        errno = EPROTO; return -1;
    }
    return frame_records_valid(r, records);
}

int xtos_bci_frame_snapshot_release(xtos_bci_session_t *s)
{
    struct xtos_bci_frame_release release = {0};
    if (!s) { errno = EINVAL; return -1; }
    if (s->frame_mapping_size) {
        if (munmap(s->frame_mapping, s->frame_mapping_size) < 0) return -1;
        s->frame_mapping = NULL; s->frame_mapping_size = 0;
    }
    s->frame_valid = false;
    if (!s->frame_active) return 0;
    release.version = XTOS_BCI_FRAME_VERSION; release.token = s->frame_snapshot.token;
    if (ioctl(s->fd, XTOS_BCI_FRAME_SNAPSHOT_RELEASE, &release) < 0) return -1;
    s->frame_active = false; memset(&s->frame_snapshot, 0, sizeof(s->frame_snapshot));
    return 0;
}

int xtos_bci_frame_snapshot_acquire(xtos_bci_session_t *s, uint32_t capacity,
                                    struct xtos_bci_frame_snapshot *out)
{
    struct xtos_bci_frame_request r = {0};
    struct xtos_bci_frame_snapshot result = {0};
    void *mapping;
    if (!s || !out || !capacity || capacity > XTOS_BCI_FRAME_MAX_BATCH) {
        errno = EINVAL; return -1;
    }
    if (s->snapshot_active || s->frame_active) { errno = EBUSY; return -1; }
    r.version = XTOS_BCI_FRAME_VERSION; r.capacity_frames = capacity;
    if (ioctl(s->fd, XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE, &r) < 0) return -1;
    /* Ownership starts before trusting a single returned metadata byte. */
    s->frame_snapshot = r; s->frame_active = true;
    if (frame_request_valid(&r, capacity, 0, 0, 1) < 0) goto fail;
    mapping = mmap(NULL, (size_t)r.mapping_bytes, PROT_READ, MAP_SHARED, s->fd, 0);
    if (mapping == MAP_FAILED) goto fail;
    s->frame_mapping = mapping; s->frame_mapping_size = (size_t)r.mapping_bytes;
    if (!mapping) { errno = EPROTO; goto fail; }
    if ((uintptr_t)mapping > UINTPTR_MAX - (size_t)r.mapping_bytes) {
        errno = EOVERFLOW; goto fail;
    }
    if (frame_mapping_valid(mapping, &r) < 0) goto fail;
    result.mapping = mapping; result.mapping_bytes = s->frame_mapping_size;
    result.header = mapping;
    result.batch = frame_batch(&r, (const struct xtos_bci_frame_v1 *)
                               ((const char *)mapping + XTOS_BCI_FRAME_RECORDS_OFFSET));
    s->frame_valid = true; *out = result; return 0;
fail:
    {
        int saved_errno = errno;
        xtos_bci_frame_snapshot_release(s); errno = saved_errno; return -1;
    }
}

int xtos_bci_frame_snapshot_consume(xtos_bci_session_t *s)
{
    struct xtos_bci_frame_consume c = {0};
    if (!s) { errno = EINVAL; return -1; }
    if (!s->frame_valid) { errno = ENOENT; return -1; }
    c.version = XTOS_BCI_FRAME_VERSION; c.token = s->frame_snapshot.token;
    c.cursor = s->frame_snapshot.cursor; c.frame_count = s->frame_snapshot.frame_count;
    c.stream_generation = s->frame_snapshot.stream_generation;
    return ioctl(s->fd, XTOS_BCI_FRAME_SNAPSHOT_CONSUME, &c);
}

int xtos_bci_get_config(xtos_bci_session_t *session,
                        struct xtos_bci_config_v1 *out)
{
	struct xtos_bci_config_v1 config;

	if (!session || !out) {
		errno = EINVAL;
		return -1;
	}
	memset(&config, 0, sizeof(config));
	config.version = XTOS_BCI_CONFIG_VERSION;
	if (ioctl(session->fd, XTOS_BCI_GET_CONFIG, &config) < 0)
		return -1;
	if (config_valid(&config) < 0)
		return -1;
	memcpy(out, &config, sizeof(config));
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
    if (session->frame_active) { errno = EBUSY; return -1; }
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

/* Validate lengths before mmap or dereferencing even the fixed header. */
static int snapshot_request_valid(const struct xtos_bci_snapshot_request *r,
                                  size_t requested_bytes)
{
	long page_size = sysconf(_SC_PAGESIZE);
	uint64_t used;

	if (r->mapping_bytes > SIZE_MAX || r->mapping_bytes > PTRDIFF_MAX) {
		errno = EOVERFLOW;
		return -1;
	}
	if (page_size <= 0 || r->version != XTOS_BCI_SNAPSHOT_VERSION ||
	    r->flags || r->requested_bytes != requested_bytes ||
	    r->reserved[0] || r->reserved[1] || !r->token ||
	    !r->channels || !r->sample_rate_hz ||
	    !r->payload_bytes || r->payload_bytes > requested_bytes ||
	    r->payload_bytes > XTOS_BCI_SNAPSHOT_MAX_PAYLOAD ||
	    r->payload_bytes % ((uint64_t)r->channels * sizeof(int32_t))) {
		errno = EPROTO;
		return -1;
	}
	used = sizeof(struct xtos_bci_snapshot_header) + r->payload_bytes;
	if (r->mapping_bytes < used || r->mapping_bytes % (uint64_t)page_size ||
	    r->mapping_bytes - used >= (uint64_t)page_size) {
		errno = EPROTO;
		return -1;
	}
	return 0;
}

static int snapshot_header_valid(const struct xtos_bci_snapshot_header *h,
                                 const struct xtos_bci_snapshot_request *r)
{
	if (h->magic != XTOS_BCI_SNAPSHOT_MAGIC ||
	    h->version != XTOS_BCI_SNAPSHOT_VERSION ||
	    h->header_size != sizeof(*h) || h->unit != XTOS_BCI_UNIT_NANOVOLT ||
	    h->sample_size != sizeof(int32_t) || h->reserved0 || h->reserved1 ||
	    h->payload_bytes != r->payload_bytes || h->cursor != r->cursor ||
	    h->token != r->token || h->channels != r->channels ||
	    h->sample_rate_hz != r->sample_rate_hz) {
		errno = EPROTO;
		return -1;
	}
	return 0;
}

int xtos_bci_snapshot_acquire(xtos_bci_session_t *session,
                              size_t requested_bytes,
                              struct xtos_bci_snapshot *result)
{
	struct xtos_bci_snapshot_request request = {0};
	void *mapping;

	if (result)
		memset(result, 0, sizeof(*result));
	if (!session || !result || !requested_bytes || requested_bytes % 4) {
		errno = EINVAL;
		return -1;
	}
	if (session->snapshot_active || session->frame_active) {
		errno = EBUSY;
		return -1;
	}
	request.version = XTOS_BCI_SNAPSHOT_VERSION;
	request.requested_bytes = requested_bytes;
	if (ioctl(session->fd, XTOS_BCI_SNAPSHOT_ACQUIRE, &request) < 0)
		return -1;
	session->snapshot = request;
	session->snapshot_active = true;
	if (snapshot_request_valid(&request, requested_bytes) < 0)
		goto fail;
	mapping = mmap(NULL, (size_t)request.mapping_bytes, PROT_READ,
	               MAP_SHARED, session->fd, 0);
	if (mapping == MAP_FAILED)
		goto fail;
	session->mmap_buffer = mapping;
	session->mmap_size = (size_t)request.mapping_bytes;
	/* A successful address-zero mapping cannot be dereferenced in C. Size
	 * still records ownership so the normal failure path unmaps address 0. */
	if (!mapping) {
		errno = EPROTO;
		goto fail;
	}
	if (snapshot_header_valid(mapping, &request) < 0)
		goto fail;
	session->snapshot_valid = true;
	result->mapping = mapping;
	result->mapping_bytes = session->mmap_size;
	result->header = mapping;
	result->payload = (const int32_t *)((const char *)mapping +
	                                    result->header->header_size);
	result->payload_bytes = (size_t)request.payload_bytes;
	return 0;

fail:
	{
		int saved_errno = errno;
		xtos_bci_snapshot_release(session);
		errno = saved_errno;
		return -1;
	}
}

int xtos_bci_snapshot_consume(xtos_bci_session_t *session)
{
	struct xtos_bci_snapshot_consume consume = {0};

	if (!session) {
		errno = EINVAL;
		return -1;
	}
	if (!session->snapshot_valid) {
		errno = ENOENT;
		return -1;
	}
	consume.version = XTOS_BCI_SNAPSHOT_VERSION;
	consume.token = session->snapshot.token;
	consume.cursor = session->snapshot.cursor;
	consume.bytes = session->snapshot.payload_bytes;
	return ioctl(session->fd, XTOS_BCI_SNAPSHOT_CONSUME, &consume);
}

int xtos_bci_snapshot_release(xtos_bci_session_t *session)
{
	struct xtos_bci_snapshot_release release = {0};
	int ret;

	if (!session) {
		errno = EINVAL;
		return -1;
	}
	if (session->mmap_size) {
		if (munmap(session->mmap_buffer, session->mmap_size) < 0)
			return -1;
		session->mmap_buffer = NULL;
		session->mmap_size = 0;
	}
	session->snapshot_valid = false;
	if (!session->snapshot_active)
		return 0;
	release.version = XTOS_BCI_SNAPSHOT_VERSION;
	release.token = session->snapshot.token;
	ret = ioctl(session->fd, XTOS_BCI_SNAPSHOT_RELEASE, &release);
	if (ret == 0 || errno == ENODEV || errno == ENOENT) {
		session->snapshot_active = false;
		memset(&session->snapshot, 0, sizeof(session->snapshot));
	}
	return ret;
}

void *xtos_bci_get_mmap_buffer(xtos_bci_session_t *session, size_t *size)
{
	if (size)
		*size = 0;
	errno = session ? EOPNOTSUPP : EINVAL;
	return NULL;
}

int xtos_bci_get_fd(xtos_bci_session_t *session)
{
	if (!session) {
		errno = EINVAL;
		return -1;
	}

	return session->fd;
}
