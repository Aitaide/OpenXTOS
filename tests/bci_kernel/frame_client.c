/* SPDX-License-Identifier: GPL-2.0 */
/* Real Linux guest client for bci_frame_fixture. --selftest opens no device.
 * Default destroys the synthetic device at the end. --sdk-detach exercises
 * SDK detach separately after the guest reloads bci_frame_fixture.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>
#include "xtos/bci.h"
#include "xtos/bci_uapi.h"

#define BURST 32u
#define DEVICE "/dev/xtos0"
#define UNPLUG "/sys/module/bci_frame_fixture/parameters/unplug"
_Static_assert(sizeof(struct xtos_bci_frame_v1) == 224, "frame ABI");
_Static_assert(sizeof(struct xtos_bci_frame_request) == 512, "request ABI");
_Static_assert(sizeof(struct xtos_bci_config_v1) == 384, "config ABI");
_Static_assert(sizeof(struct xtos_bci_frame_snapshot_header) == 128, "header ABI");
_Static_assert(sizeof(struct xtos_bci_frame_consume) == 48, "consume ABI");
_Static_assert(sizeof(struct xtos_bci_frame_release) == 24, "release ABI");
#define CHECK(x) do { if (!(x)) { \
    fprintf(stderr, "bci_frame_guest line=%d check=%s errno=%d (%s)\n", \
            __LINE__, #x, errno, strerror(errno)); return 1; } } while (0)
#define ERROR(call, error) CHECK((call) == -1 && errno == (error))

static int zero_bytes(const void *p, size_t size)
{
    const unsigned char *b = p;
    for (size_t i = 0; i < size; i++)
        if (b[i]) return 0;
    return 1;
}
static int32_t sample(unsigned int i, unsigned int ch)
{
    int32_t value = (int32_t)((i + 1) * 1000 + ch);
    return ch & 1 ? -value : value;
}
static int config_ok(const struct xtos_bci_config_v1 *c)
{
    struct xtos_bci_config_v1 expected = {
        .version = 1, .channels = 8, .sample_rate_hz = 250,
        .enabled_mask = 255, .capabilities = 3, .generation = c->generation,
    };
    expected.registers[0] = 0x3e;
    expected.registers[1] = 0x96;
    for (unsigned int ch = 0; ch < 8; ch++) {
        expected.gain[ch] = 24;
        expected.registers[5 + ch] = 0x68;
    }
    strcpy(expected.firmware, "Synthetic 8 channels");
    return c->generation && !memcmp(c, &expected, sizeof(expected));
}
static int records_ok(const struct xtos_bci_frame_request *r,
                      const struct xtos_bci_frame_v1 *records)
{
    if (r->version != 1 || r->capacity_frames != BURST ||
        (r->flags & ~XTOS_BCI_FRAME_NONBLOCK) ||
        !r->frame_count || r->frame_count > BURST ||
        r->frame_count > r->capacity_frames || !config_ok(&r->config) ||
        !r->stream_generation || r->cursor > UINT64_MAX - r->frame_count ||
        r->next_cursor != r->cursor + r->frame_count ||
        r->record_bytes != 224 || r->header_bytes != 128 ||
        r->payload_bytes != (uint64_t)r->frame_count * 224 ||
        r->overwritten_frames || !zero_bytes(r->reserved, sizeof(r->reserved)))
        return 0;
    for (unsigned int i = 0; i < r->frame_count; i++) {
        const struct xtos_bci_frame_v1 *f = records + i;
        if (f->version != 1 || f->record_bytes != 224 || f->channels != 8 ||
            f->sample_rate_hz != 250 || f->unit != 1 || f->host_clock != 1 ||
            f->quality_flags != XTOS_BCI_FRAME_QUALITY_CONFIG_VERIFIED ||
            f->source_flags != XTOS_BCI_FRAME_SOURCE_SYNTHETIC ||
            !f->host_rx_ns || f->sequence != r->cursor + i + 1 ||
            f->stream_generation != r->stream_generation ||
            f->config_generation != r->config.generation ||
            f->device_counter != i % 256 || f->counter_bits != 8 ||
            f->missing_before || f->enabled_mask != 255 ||
            !zero_bytes(f->reserved, sizeof(f->reserved)) ||
            !zero_bytes(f->samples_nv + 8, 24 * sizeof(int32_t)) ||
            (i && f->host_rx_ns < records[i - 1].host_rx_ns))
            return 0;
        for (unsigned int ch = 0; ch < 8; ch++)
            if (f->samples_nv[ch] != sample(i, ch)) return 0;
    }
    return 1;
}
static int mapping_ok(const struct xtos_bci_frame_request *r, const void *map)
{
    const unsigned char *b = map;
    const struct xtos_bci_frame_snapshot_header *h = map;
    long page = sysconf(_SC_PAGESIZE);
    uint64_t used = 512 + r->payload_bytes;
    struct xtos_bci_frame_snapshot_header expected = {
        .magic = XTOS_BCI_FRAME_SNAPSHOT_MAGIC, .version = 1,
        .header_bytes = 128, .record_bytes = 224, .frame_count = r->frame_count,
        .unit = 1, .host_clock = 1, .payload_bytes = r->payload_bytes,
        .cursor = r->cursor, .next_cursor = r->next_cursor, .token = r->token,
        .stream_generation = r->stream_generation,
        .config_generation = r->config.generation,
        .overwritten_frames = r->overwritten_frames,
        .mapping_bytes = r->mapping_bytes, .config_bytes = 384,
        .records_offset = 512,
    };
    return page > 0 && r->version == 1 && !r->flags && !r->frames_ptr && r->token &&
        r->mapping_bytes == ((used + (uint64_t)page - 1) / (uint64_t)page) * (uint64_t)page &&
        !memcmp(h, &expected, sizeof(expected)) &&
        !memcmp(b + 128, &r->config, sizeof(r->config)) &&
        zero_bytes(b + used, (size_t)(r->mapping_bytes - used)) &&
        records_ok(r, (const struct xtos_bci_frame_v1 *)(b + 512));
}
static struct xtos_bci_frame_request read_request(struct xtos_bci_frame_v1 *frames)
{
    return (struct xtos_bci_frame_request) {
        .version = 1, .flags = XTOS_BCI_FRAME_NONBLOCK,
        .capacity_frames = BURST, .frames_ptr = (uintptr_t)frames,
    };
}
static struct xtos_bci_frame_request acquire_request(void)
{
    return (struct xtos_bci_frame_request) {.version = 1, .capacity_frames = BURST};
}
static struct xtos_bci_frame_consume consume_request(const struct xtos_bci_frame_request *r)
{
    return (struct xtos_bci_frame_consume) {
        .version = 1, .token = r->token, .cursor = r->cursor,
        .frame_count = r->frame_count, .stream_generation = r->stream_generation,
    };
}
static int get_config(int fd, struct xtos_bci_config_v1 *c)
{
    *c = (struct xtos_bci_config_v1) {.version = 1};
    CHECK(ioctl(fd, XTOS_BCI_GET_CONFIG, c) == 0 && config_ok(c));
    return 0;
}
static int legacy_pattern(int fd, unsigned int bursts)
{
    int32_t values[BURST * 8];
    for (unsigned int n = 0; n < bursts; n++) {
        CHECK(read(fd, values, sizeof(values)) == (ssize_t)sizeof(values));
        for (unsigned int i = 0; i < BURST; i++)
            for (unsigned int ch = 0; ch < 8; ch++)
                CHECK(values[i * 8 + ch] == sample(i, ch));
    }
    return 0;
}
static int rejected_request(int fd, unsigned long op,
                            struct xtos_bci_frame_request bad, int error)
{
    struct xtos_bci_frame_request before = bad;
    ERROR(ioctl(fd, op, &bad), error);
    CHECK(!memcmp(&bad, &before, sizeof(bad)));
    return 0;
}
static int malformed(int fd, struct xtos_bci_frame_v1 *frames)
{
    struct xtos_bci_frame_request base = read_request(frames), bad;
    /* Every non-input byte, including all reserved/config bytes. */
    for (size_t i = 12; i < sizeof(base); i++) {
        if (i >= 16 && i < 24) continue;
        bad = base;
        ((unsigned char *)&bad)[i] = 1;
        CHECK(rejected_request(fd, XTOS_BCI_FRAME_READ, bad, EINVAL) == 0);
    }
    bad = base; bad.version = 2;
    CHECK(rejected_request(fd, XTOS_BCI_FRAME_READ, bad, EINVAL) == 0);
    bad = base; bad.flags = 2;
    CHECK(rejected_request(fd, XTOS_BCI_FRAME_READ, bad, EINVAL) == 0);
    bad = base; bad.capacity_frames = 0;
    CHECK(rejected_request(fd, XTOS_BCI_FRAME_READ, bad, EINVAL) == 0);
    bad = base; bad.capacity_frames = XTOS_BCI_FRAME_MAX_BATCH + 1;
    CHECK(rejected_request(fd, XTOS_BCI_FRAME_READ, bad, EINVAL) == 0);
    bad = base; bad.frames_ptr = 0;
    CHECK(rejected_request(fd, XTOS_BCI_FRAME_READ, bad, EINVAL) == 0);
    bad = base; bad.frames_ptr = UINT64_MAX - 1;
    CHECK(rejected_request(fd, XTOS_BCI_FRAME_READ, bad, EOVERFLOW) == 0);
    base = acquire_request();
    for (size_t i = 4; i < sizeof(base); i++) {
        if (i >= 8 && i < 12) continue;
        bad = base; ((unsigned char *)&bad)[i] = 1;
        CHECK(rejected_request(fd, XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE, bad, EINVAL) == 0);
    }
    bad = base; bad.capacity_frames = 0;
    CHECK(rejected_request(fd, XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE, bad, EINVAL) == 0);
    bad = base; bad.capacity_frames = XTOS_BCI_FRAME_MAX_BATCH + 1;
    CHECK(rejected_request(fd, XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE, bad, EINVAL) == 0);
    return 0;
}
static int direct_read(int fd, uint64_t *last_sequence)
{
    struct xtos_bci_frame_v1 frames[BURST];
    struct xtos_bci_frame_request r = read_request(frames), before = r;
    struct xtos_bci_config_v1 initial, started, current;
    CHECK(get_config(fd, &initial) == 0);
    CHECK(ioctl(fd, XTOS_BCI_FRAME_READ, &r) == 0 && !memcmp(&r, &before, sizeof(r)));
    r = acquire_request();
    ERROR(ioctl(fd, XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE, &r), EAGAIN);
    CHECK(ioctl(fd, XTOS_BCI_START_STREAM) == 0);
    CHECK(get_config(fd, &started) == 0 && started.generation == initial.generation + 1);
    CHECK(ioctl(fd, XTOS_BCI_START_STREAM) == 0);
    CHECK(get_config(fd, &current) == 0 && !memcmp(&current, &started, sizeof(current)));
    memset(frames, 0xa5, sizeof(frames));
    struct xtos_bci_frame_v1 sentinels[BURST];
    memcpy(sentinels, frames, sizeof(frames));
    CHECK(malformed(fd, frames) == 0 && !memcmp(frames, sentinels, sizeof(frames)));
    long page = sysconf(_SC_PAGESIZE);
    CHECK(page > 0);
    void *guard = mmap(NULL, (size_t)page * 2, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    CHECK(guard != MAP_FAILED);
    r = read_request(frames); r.frames_ptr = (uintptr_t)guard;
    CHECK(rejected_request(fd, XTOS_BCI_FRAME_READ, r, EFAULT) == 0);
    /* One writable record followed by a protected page: partial usercopy may
     * change memory, but must leave the frame consumer cursor unadvanced. */
    CHECK(mprotect(guard, (size_t)page, PROT_READ | PROT_WRITE) == 0);
    r = read_request(frames); r.capacity_frames = 2;
    r.frames_ptr = (uintptr_t)((char *)guard + page - 224);
    CHECK(rejected_request(fd, XTOS_BCI_FRAME_READ, r, EFAULT) == 0);
    CHECK(munmap(guard, (size_t)page * 2) == 0);
    r = read_request(frames);
    CHECK(ioctl(fd, XTOS_BCI_FRAME_READ, &r) == 0);
    CHECK(r.frame_count == BURST && records_ok(&r, frames) && !r.token && !r.mapping_bytes);
    CHECK(!memcmp(&r.config, &started, sizeof(started)));
    CHECK(legacy_pattern(fd, 1) == 0); /* Frame consumption did not drain bytes. */
    uint64_t cursor = r.next_cursor, stream = r.stream_generation;
    r = read_request(frames); before = r;
    ERROR(ioctl(fd, XTOS_BCI_FRAME_READ, &r), EAGAIN);
    CHECK(!memcmp(&r, &before, sizeof(r)));
    CHECK(ioctl(fd, XTOS_BCI_STOP_STREAM) == 0);
    CHECK(ioctl(fd, XTOS_BCI_FRAME_READ, &r) == 0 && !memcmp(&r, &before, sizeof(r)));
    /* Queue two START runs before reading, proving retained configuration splits. */
    CHECK(ioctl(fd, XTOS_BCI_START_STREAM) == 0);
    CHECK(get_config(fd, &started) == 0);
    CHECK(ioctl(fd, XTOS_BCI_STOP_STREAM) == 0);
    CHECK(ioctl(fd, XTOS_BCI_START_STREAM) == 0);
    CHECK(get_config(fd, &current) == 0 && current.generation == started.generation + 1);
    for (unsigned int n = 0; n < 2; n++) {
        r = read_request(frames);
        CHECK(ioctl(fd, XTOS_BCI_FRAME_READ, &r) == 0 && r.frame_count == BURST);
        CHECK(records_ok(&r, frames) && r.cursor == cursor && r.stream_generation == ++stream);
        CHECK(!memcmp(&r.config, n ? &current : &started, sizeof(current)));
        cursor = r.next_cursor;
    }
    CHECK(legacy_pattern(fd, 2) == 0);
    CHECK(ioctl(fd, XTOS_BCI_STOP_STREAM) == 0);
    *last_sequence = cursor;
    return 0;
}
static int direct_snapshot(int fd, uint64_t previous_sequence)
{
    struct xtos_bci_frame_v1 frames[BURST];
    struct xtos_bci_snapshot_request old = {.version = 1, .requested_bytes = BURST * 32};
    CHECK(ioctl(fd, XTOS_BCI_START_STREAM) == 0);
    CHECK(ioctl(fd, XTOS_BCI_SNAPSHOT_ACQUIRE, &old) == 0 && old.token);
    struct xtos_bci_frame_request r = acquire_request(), rd = read_request(frames);
    ERROR(ioctl(fd, XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE, &r), EBUSY);
    ERROR(ioctl(fd, XTOS_BCI_FRAME_READ, &rd), EBUSY);
    int32_t bytes[8];
    ERROR(read(fd, bytes, sizeof(bytes)), EBUSY);
    struct xtos_bci_frame_consume wrong = {.version = 1, .token = old.token,
        .frame_count = 1, .stream_generation = 1};
    struct xtos_bci_frame_release release = {.version = 1, .token = old.token};
    ERROR(ioctl(fd, XTOS_BCI_FRAME_SNAPSHOT_CONSUME, &wrong), ESTALE);
    ERROR(ioctl(fd, XTOS_BCI_FRAME_SNAPSHOT_RELEASE, &release), ESTALE);
    struct xtos_bci_snapshot_consume oc = {.version = 1, .token = old.token,
        .cursor = old.cursor, .bytes = old.payload_bytes};
    struct xtos_bci_snapshot_release orelease = {.version = 1, .token = old.token};
    CHECK(ioctl(fd, XTOS_BCI_SNAPSHOT_CONSUME, &oc) == 0);
    CHECK(ioctl(fd, XTOS_BCI_SNAPSHOT_RELEASE, &orelease) == 0);
    struct pollfd ready = {.fd = fd, .events = POLLIN};
    CHECK(poll(&ready, 1, 0) == 1 && (ready.revents & POLLIN));
    r = acquire_request();
    CHECK(ioctl(fd, XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE, &r) == 0 && r.token == old.token + 1);
    CHECK(r.frame_count == BURST && r.cursor == previous_sequence);
    size_t length = (size_t)r.mapping_bytes;
    long page = sysconf(_SC_PAGESIZE);
    CHECK(page > 0 && length > (size_t)page);
    CHECK(mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0) == MAP_FAILED && errno == EINVAL);
    CHECK(mmap(NULL, length, PROT_READ, MAP_PRIVATE, fd, 0) == MAP_FAILED && errno == EINVAL);
    CHECK(mmap(NULL, length + (size_t)page, PROT_READ, MAP_SHARED, fd, 0) == MAP_FAILED && errno == EINVAL);
    CHECK(mmap(NULL, length - (size_t)page, PROT_READ, MAP_SHARED, fd, 0) == MAP_FAILED && errno == EINVAL);
    CHECK(mmap(NULL, length, PROT_READ, MAP_SHARED, fd, page) == MAP_FAILED && errno == EINVAL);
    void *map = mmap(NULL, length, PROT_READ, MAP_SHARED, fd, 0);
    void *map2 = mmap(NULL, length, PROT_READ, MAP_SHARED, fd, 0);
    CHECK(map != MAP_FAILED && map2 != MAP_FAILED && mapping_ok(&r, map) && mapping_ok(&r, map2));
    unsigned char *saved = malloc(length);
    CHECK(saved); memcpy(saved, map, length);
    ERROR(mprotect(map, length, PROT_READ | PROT_WRITE), EACCES);
    /* Splitting and restoring permissions invokes real Linux VMA lifecycle. */
    CHECK(mprotect((char *)map + page, length - (size_t)page, PROT_NONE) == 0);
    CHECK(!memcmp(map, saved, (size_t)page));
    CHECK(mprotect((char *)map + page, length - (size_t)page, PROT_READ) == 0);
    CHECK(!memcmp(map, saved, length));
    ERROR(ioctl(fd, XTOS_BCI_SNAPSHOT_CONSUME, &oc), ESTALE);
    orelease.token = r.token;
    ERROR(ioctl(fd, XTOS_BCI_SNAPSHOT_RELEASE, &orelease), ESTALE);
    old = (struct xtos_bci_snapshot_request) {.version = 1, .requested_bytes = 32};
    ERROR(ioctl(fd, XTOS_BCI_SNAPSHOT_ACQUIRE, &old), EBUSY);
    rd = read_request(frames);
    ERROR(ioctl(fd, XTOS_BCI_FRAME_READ, &rd), EBUSY);
    ERROR(read(fd, bytes, sizeof(bytes)), EBUSY);
    struct xtos_bci_frame_consume consume = consume_request(&r), bad = consume;
    bad.flags = 1;
    ERROR(ioctl(fd, XTOS_BCI_FRAME_SNAPSHOT_CONSUME, &bad), EINVAL);
    bad = consume; bad.reserved = 1;
    ERROR(ioctl(fd, XTOS_BCI_FRAME_SNAPSHOT_CONSUME, &bad), EINVAL);
    CHECK(ioctl(fd, XTOS_BCI_FRAME_SNAPSHOT_CONSUME, &consume) == 0);
    ERROR(ioctl(fd, XTOS_BCI_FRAME_SNAPSHOT_CONSUME, &consume), ESTALE);
    release.token = r.token;
    CHECK(ioctl(fd, XTOS_BCI_FRAME_SNAPSHOT_RELEASE, &release) == 0);
    ERROR(ioctl(fd, XTOS_BCI_FRAME_SNAPSHOT_RELEASE, &release), ESTALE);
    CHECK(!memcmp(map, saved, length) && !memcmp(map2, saved, length));
    CHECK(ioctl(fd, XTOS_BCI_STOP_STREAM) == 0 && ioctl(fd, XTOS_BCI_START_STREAM) == 0);
    CHECK(!memcmp(map, saved, length)); /* New config cannot relabel old bytes. */
    CHECK(close(fd) == 0);
    int other = open(DEVICE, O_RDWR | O_NONBLOCK);
    CHECK(other == -1 && errno == EBUSY); /* VMA retains file, not final release. */
    CHECK(munmap(map2, length) == 0 && !memcmp(map, saved, length));
    CHECK(munmap(map, length) == 0);
    free(saved);
    fd = open(DEVICE, O_RDWR | O_NONBLOCK);
    CHECK(fd >= 0 && ioctl(fd, XTOS_BCI_START_STREAM) == 0);
    rd = read_request(frames);
    CHECK(ioctl(fd, XTOS_BCI_FRAME_READ, &rd) == 0 && records_ok(&rd, frames));
    CHECK(rd.cursor > r.next_cursor && rd.stream_generation > r.stream_generation &&
          rd.config.generation > r.config.generation);
    CHECK(close(fd) == 0);
    return 0;
}
static struct xtos_bci_frame_request batch_request(const struct xtos_bci_frame_batch *b)
{
    return (struct xtos_bci_frame_request) {
        .version = 1, .capacity_frames = BURST, .frame_count = b->frame_count,
        .cursor = b->cursor, .next_cursor = b->next_cursor,
        .overwritten_frames = b->overwritten_frames, .stream_generation = b->stream_generation,
        .record_bytes = 224, .header_bytes = 128,
        .payload_bytes = (uint64_t)b->frame_count * 224, .config = b->config,
    };
}
static int sdk_normal(void)
{
    xtos_bci_session_t *s = xtos_bci_session_create(DEVICE);
    CHECK(s && xtos_bci_start(s) == 0);
    struct xtos_bci_frame_batch batch;
    CHECK(xtos_bci_frame_read(s, BURST, XTOS_BCI_FRAME_NONBLOCK, &batch) == 0);
    struct xtos_bci_frame_request r = batch_request(&batch);
    CHECK(batch.frame_count == BURST && records_ok(&r, batch.records));
    struct xtos_bci_frame_batch prior = batch;
    unsigned char saved[BURST * 224];
    memcpy(saved, batch.records, sizeof(saved));
    ERROR(xtos_bci_frame_read(s, BURST, XTOS_BCI_FRAME_NONBLOCK, &batch), EAGAIN);
    CHECK(!memcmp(&batch, &prior, sizeof(batch)) && !memcmp(saved, prior.records, sizeof(saved)));
    CHECK(xtos_bci_stop(s) == 0);
    CHECK(xtos_bci_frame_read(s, BURST, 0, &batch) == 0 && batch.frame_count == 0);
    CHECK(xtos_bci_start(s) == 0);
    struct xtos_bci_frame_snapshot snapshot;
    CHECK(xtos_bci_frame_snapshot_acquire(s, BURST, &snapshot) == 0);
    r = batch_request(&snapshot.batch); r.token = snapshot.header->token;
    r.mapping_bytes = snapshot.mapping_bytes;
    CHECK(mapping_ok(&r, snapshot.mapping));
    CHECK(r.cursor == prior.next_cursor && r.stream_generation > prior.stream_generation &&
          r.config.generation > prior.config.generation);
    struct xtos_bci_snapshot legacy;
    ERROR(xtos_bci_snapshot_acquire(s, 32, &legacy), EBUSY);
    struct xtos_bci_frame_v1 frames[BURST];
    struct xtos_bci_frame_request rd = read_request(frames);
    ERROR(ioctl(xtos_bci_get_fd(s), XTOS_BCI_FRAME_READ, &rd), EBUSY);
    CHECK(xtos_bci_frame_snapshot_consume(s) == 0);
    ERROR(xtos_bci_frame_snapshot_consume(s), ESTALE);
    CHECK(mapping_ok(&r, snapshot.mapping));
    CHECK(xtos_bci_frame_snapshot_release(s) == 0);
    /* Public legacy SDK remains available after new-family cleanup. */
    CHECK(xtos_bci_snapshot_acquire(s, 32, &legacy) == 0);
    for (unsigned int ch = 0; ch < 8; ch++) CHECK(legacy.payload[ch] == sample(0, ch));
    CHECK(xtos_bci_snapshot_consume(s) == 0 && xtos_bci_snapshot_release(s) == 0);
    CHECK(xtos_bci_stop(s) == 0);
    xtos_bci_session_destroy(s);
    return 0;
}
static int unplug(void)
{
    int fd = open(UNPLUG, O_WRONLY);
    CHECK(fd >= 0 && write(fd, "1", 1) == 1 && close(fd) == 0);
    return 0;
}
static int detach_direct(void)
{
    int fd = open(DEVICE, O_RDWR | O_NONBLOCK);
    CHECK(fd >= 0 && ioctl(fd, XTOS_BCI_START_STREAM) == 0);
    struct xtos_bci_frame_request r = acquire_request();
    CHECK(ioctl(fd, XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE, &r) == 0);
    size_t length = (size_t)r.mapping_bytes;
    void *map = mmap(NULL, length, PROT_READ, MAP_SHARED, fd, 0);
    CHECK(map != MAP_FAILED && mapping_ok(&r, map));
    unsigned char *saved = malloc(length);
    CHECK(saved); memcpy(saved, map, length);
    CHECK(unplug() == 0);
    struct xtos_bci_frame_v1 frames[BURST];
    struct xtos_bci_frame_request rd = read_request(frames), acq = acquire_request();
    CHECK(rejected_request(fd, XTOS_BCI_FRAME_READ, rd, ENODEV) == 0);
    CHECK(rejected_request(fd, XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE, acq, ENODEV) == 0);
    struct xtos_bci_frame_consume consume = consume_request(&r);
    ERROR(ioctl(fd, XTOS_BCI_FRAME_SNAPSHOT_CONSUME, &consume), ENODEV);
    struct xtos_bci_config_v1 config = {.version = 1}, before = config;
    ERROR(ioctl(fd, XTOS_BCI_GET_CONFIG, &config), ENODEV);
    CHECK(!memcmp(&config, &before, sizeof(config)));
    ERROR(ioctl(fd, XTOS_BCI_START_STREAM), ENODEV);
    int32_t bytes[8];
    ERROR(read(fd, bytes, sizeof(bytes)), ENODEV);
    struct pollfd pollfd = {.fd = fd, .events = POLLIN};
    CHECK(poll(&pollfd, 1, 0) == 1 &&
          (pollfd.revents & (POLLHUP | POLLERR)) == (POLLHUP | POLLERR));
    struct xtos_bci_snapshot_release old = {.version = 1, .token = r.token};
    ERROR(ioctl(fd, XTOS_BCI_SNAPSHOT_RELEASE, &old), ENODEV);
    struct xtos_bci_frame_release release = {.version = 1, .token = r.token};
    CHECK(ioctl(fd, XTOS_BCI_FRAME_SNAPSHOT_RELEASE, &release) == 0);
    CHECK(!memcmp(map, saved, length));
    pid_t child = fork();
    CHECK(child >= 0);
    if (!child) {
        int ok = !memcmp(map, saved, length) && close(fd) == 0;
        ok = ok && !memcmp(map, saved, length) && munmap(map, length) == 0;
        _exit(ok ? 0 : 1);
    }
    int status;
    CHECK(waitpid(child, &status, 0) == child && WIFEXITED(status) && WEXITSTATUS(status) == 0);
    CHECK(!memcmp(map, saved, length) && close(fd) == 0 && !memcmp(map, saved, length));
    CHECK(munmap(map, length) == 0);
    free(saved);
    return 0;
}
static int detach_sdk(void)
{
    xtos_bci_session_t *s = xtos_bci_session_create(DEVICE);
    CHECK(s && xtos_bci_start(s) == 0);
    struct xtos_bci_frame_snapshot snapshot;
    CHECK(xtos_bci_frame_snapshot_acquire(s, BURST, &snapshot) == 0);
    struct xtos_bci_frame_request r = batch_request(&snapshot.batch);
    r.token = snapshot.header->token; r.mapping_bytes = snapshot.mapping_bytes;
    CHECK(mapping_ok(&r, snapshot.mapping));
    CHECK(unplug() == 0);
    ERROR(xtos_bci_frame_snapshot_consume(s), ENODEV);
    CHECK(mapping_ok(&r, snapshot.mapping));
    struct xtos_bci_config_v1 cfg, before;
    memset(&cfg, 0xa5, sizeof(cfg)); before = cfg;
    ERROR(xtos_bci_get_config(s, &cfg), ENODEV);
    CHECK(!memcmp(&cfg, &before, sizeof(cfg)));
    CHECK(xtos_bci_frame_snapshot_release(s) == 0);
    struct xtos_bci_frame_batch batch;
    ERROR(xtos_bci_frame_read(s, BURST, XTOS_BCI_FRAME_NONBLOCK, &batch), ENODEV);
    ERROR(xtos_bci_frame_snapshot_acquire(s, BURST, &snapshot), ENODEV);
    xtos_bci_session_destroy(s);
    return 0;
}
static int selftest(void)
{
    /* Independent expected configuration and signed-pattern oracle, CPU only. */
    struct xtos_bci_config_v1 c = {.version = 1, .channels = 8,
        .sample_rate_hz = 250, .enabled_mask = 255, .capabilities = 3, .generation = 1};
    c.registers[0] = 0x3e; c.registers[1] = 0x96;
    for (unsigned int ch = 0; ch < 8; ch++) { c.gain[ch] = 24; c.registers[ch + 5] = 0x68; }
    strcpy(c.firmware, "Synthetic 8 channels");
    CHECK(config_ok(&c));
    struct xtos_bci_frame_v1 frames[BURST] = {0};
    for (unsigned int i = 0; i < BURST; i++) {
        frames[i] = (struct xtos_bci_frame_v1) {
            .version = 1, .record_bytes = 224, .channels = 8,
            .sample_rate_hz = 250, .unit = 1, .host_clock = 1,
            .quality_flags = 1, .source_flags = 2, .host_rx_ns = 100 + i,
            .sequence = 41 + i, .stream_generation = 2, .config_generation = 1,
            .device_counter = i, .counter_bits = 8, .enabled_mask = 255,
        };
        for (unsigned int ch = 0; ch < 8; ch++)
            frames[i].samples_nv[ch] = sample(i, ch);
    }
    struct xtos_bci_frame_request r = {
        .version = 1, .capacity_frames = BURST, .frame_count = BURST,
        .cursor = 40, .next_cursor = 40 + BURST, .stream_generation = 2,
        .record_bytes = 224, .header_bytes = 128, .payload_bytes = BURST * 224,
        .config = c,
    };
    CHECK(records_ok(&r, frames));
    frames[3].sequence++; CHECK(!records_ok(&r, frames)); frames[3].sequence--;
    frames[7].samples_nv[31] = 1; CHECK(!records_ok(&r, frames)); frames[7].samples_nv[31] = 0;
    frames[9].source_flags = 1; CHECK(!records_ok(&r, frames)); frames[9].source_flags = 2;
    frames[2].host_rx_ns = 1; CHECK(!records_ok(&r, frames)); frames[2].host_rx_ns = 102;
    frames[1].samples_nv[0]++; CHECK(!records_ok(&r, frames)); frames[1].samples_nv[0]--;
    r.config.gain[0] = 1; CHECK(!records_ok(&r, frames)); r.config.gain[0] = 24;
    CHECK(records_ok(&r, frames));
    long page = sysconf(_SC_PAGESIZE);
    CHECK(page > 0);
    size_t length = ((512 + (size_t)r.payload_bytes + (size_t)page - 1) /
                     (size_t)page) * (size_t)page;
    unsigned char *map = calloc(1, length);
    CHECK(map);
    r.token = 7; r.mapping_bytes = length;
    struct xtos_bci_frame_snapshot_header *h = (void *)map;
    *h = (struct xtos_bci_frame_snapshot_header) {
        .magic = 0x58424631, .version = 1, .header_bytes = 128,
        .record_bytes = 224, .frame_count = BURST, .unit = 1, .host_clock = 1,
        .payload_bytes = BURST * 224, .cursor = 40, .next_cursor = 40 + BURST,
        .token = 7, .stream_generation = 2, .config_generation = 1,
        .mapping_bytes = length, .config_bytes = 384, .records_offset = 512,
    };
    memcpy(map + 128, &c, sizeof(c)); memcpy(map + 512, frames, sizeof(frames));
    CHECK(mapping_ok(&r, map));
    h->token++; CHECK(!mapping_ok(&r, map)); h->token--;
    map[128 + 40] ^= 1; CHECK(!mapping_ok(&r, map)); map[128 + 40] ^= 1;
    CHECK(length > 512 + r.payload_bytes);
    map[length - 1] = 1; CHECK(!mapping_ok(&r, map)); map[length - 1] = 0;
    CHECK(mapping_ok(&r, map)); free(map);
    c.gain[0] = 1; CHECK(!config_ok(&c));
    CHECK(sample(0, 0) == 1000 && sample(0, 1) == -1001 && sample(31, 7) == -32007);
    puts("BCI_FRAME_CLIENT_SELFTEST=PASS");
    return 0;
}
int main(int argc, char **argv)
{
    if (argc == 2 && !strcmp(argv[1], "--selftest")) return selftest();
    if (argc == 2 && !strcmp(argv[1], "--sdk-detach")) {
        CHECK(detach_sdk() == 0);
        puts("BCI_FRAME_SDK_DETACH_GUEST=PASS");
        return 0;
    }
    if (argc != 1) { fprintf(stderr, "usage: %s [--selftest|--sdk-detach]\n", argv[0]); return 2; }
    int fd = open(DEVICE, O_RDWR | O_NONBLOCK);
    uint64_t sequence;
    CHECK(fd >= 0 && direct_read(fd, &sequence) == 0);
    CHECK(direct_snapshot(fd, sequence) == 0);
    puts("BCI_FRAME_DIRECT_GUEST=PASS");
    CHECK(sdk_normal() == 0);
    puts("BCI_FRAME_SDK_GUEST=PASS");
    CHECK(detach_direct() == 0);
    puts("BCI_FRAME_DETACH_GUEST=PASS");
    puts("BCI_FRAME_GUEST=PASS");
    return 0;
}
