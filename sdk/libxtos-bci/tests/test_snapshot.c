#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include "xtos/bci.h"
#include "xtos/bci_uapi.h"

static int cases, map_calls, unmap_calls, release_calls, consume_calls, close_calls;
static int acquire_error, map_error, unmap_error, release_error, consume_error;
static int stop_error, disconnected, consumed, active, null_mapping;
static unsigned mutation;
static uint64_t next_token = 10;
static struct xtos_bci_snapshot_request reply;
static struct xtos_bci_snapshot_header header;
static void *live_mapping;
static size_t live_size;
static char events[1024];
static size_t event_count;
void *__real_mmap(void *, size_t, int, int, int, off_t);
int __real_munmap(void *, size_t);
static void event(char ch) { assert(event_count < sizeof(events)-1); events[event_count++] = ch; }
static void reset(void)
{
    assert(!live_mapping && !active);
    map_calls = unmap_calls = release_calls = consume_calls = close_calls = 0;
    acquire_error = map_error = unmap_error = release_error = consume_error = 0;
    stop_error = disconnected = consumed = null_mapping = 0;
    mutation = 0;
    memset(events, 0, sizeof(events)); event_count = 0;
}
int __wrap_open(const char *path, int flags, ...)
{
    assert(strcmp(path, "/mock/bci") == 0 && flags == O_RDWR);
    return 42;
}
int __wrap_close(int fd)
{
    assert(fd == 42 && !live_mapping);
    close_calls++; active = 0; event('C'); return 0;
}
int __wrap_ioctl(int fd, unsigned long op, ...)
{
    assert(fd == 42);
    if (op == XTOS_BCI_START_STREAM) return 0;
    if (op == XTOS_BCI_STOP_STREAM) {
        event('S');
        if (stop_error) { errno = stop_error; return -1; }
        return 0;
    }
    va_list ap;
    va_start(ap, op);
    void *arg = va_arg(ap, void *);
    va_end(ap);
    if (op == XTOS_BCI_GET_INFO) {
        struct xtos_bci_info *info = arg;
        memset(info, 0, sizeof(*info)); info->num_channels = 8; info->sampling_rate = 250;
        return 0;
    }
    if (op == XTOS_BCI_SNAPSHOT_ACQUIRE) {
        struct xtos_bci_snapshot_request *r = arg;
        assert(r->version == 1 && !r->flags && r->requested_bytes &&
               !r->mapping_bytes && !r->payload_bytes && !r->cursor && !r->token &&
               !r->channels && !r->sample_rate_hz && !r->reserved[0] && !r->reserved[1]);
        if (acquire_error) { errno = acquire_error; return -1; }
        assert(!active);
        active = 1; consumed = 0;
        r->mapping_bytes = 4096; r->payload_bytes = 64; r->cursor = 256;
        r->token = ++next_token; r->channels = 8; r->sample_rate_hz = 250;
        switch (mutation) {
        case 20: r->version++; break;
        case 21: r->flags = 1; break;
        case 22: r->requested_bytes++; break;
        case 23: r->mapping_bytes = 0; break;
        case 24: r->mapping_bytes = 32; break;
        case 25: r->mapping_bytes++; break;
        case 26: r->mapping_bytes = UINT64_MAX; break;
        case 27: r->payload_bytes = 0; break;
        case 28: r->payload_bytes = 65540; break;
        case 29: r->payload_bytes = 96; break;
        case 30: r->channels = 0; break;
        case 31: r->channels = 33; break;
        case 32: r->sample_rate_hz = 0; break;
        case 33: r->token = 0; break;
        case 34: r->reserved[0] = 1; break;
        case 35: r->reserved[1] = 1; break;
        case 36: r->payload_bytes = 60; break;
        case 37: r->mapping_bytes = 8192; break;
        }
        reply = *r;
        memset(&header, 0, sizeof(header));
        header.magic = XTOS_BCI_SNAPSHOT_MAGIC; header.version = 1;
        header.header_size = sizeof(header); header.unit = XTOS_BCI_UNIT_NANOVOLT;
        header.payload_bytes = r->payload_bytes; header.cursor = r->cursor;
        header.token = r->token; header.channels = r->channels;
        header.sample_rate_hz = r->sample_rate_hz; header.sample_size = 4;
        switch (mutation) {
        case 1: header.magic++; break;
        case 2: header.version++; break;
        case 3: header.header_size = 0; break;
        case 4: header.header_size = UINT32_MAX; break;
        case 5: header.unit++; break;
        case 6: header.sample_size = 8; break;
        case 7: header.payload_bytes++; break;
        case 8: header.payload_bytes = UINT64_MAX; break;
        case 9: header.cursor++; break;
        case 10: header.token++; break;
        case 11: header.channels++; break;
        case 12: header.sample_rate_hz++; break;
        case 13: header.reserved0 = 1; break;
        case 14: header.reserved1 = 1; break;
        case 15: header.header_size = 68; break;
        }
        return 0;
    }
    if (op == XTOS_BCI_SNAPSHOT_CONSUME) {
        const struct xtos_bci_snapshot_consume *c = arg;
        consume_calls++;
        assert(active && c->version == 1 && !c->flags && c->token == reply.token &&
               c->cursor == reply.cursor && c->bytes == reply.payload_bytes &&
               !c->reserved[0] && !c->reserved[1]);
        if (disconnected || consume_error || consumed) {
            errno = disconnected ? ENODEV : consume_error ? consume_error : EALREADY;
            return -1;
        }
        consumed = 1; return 0;
    }
    if (op == XTOS_BCI_SNAPSHOT_RELEASE) {
        const struct xtos_bci_snapshot_release *r = arg;
        release_calls++; event('R');
        assert(active && r->version == 1 && !r->flags && r->token == reply.token && !r->reserved);
        if (disconnected || release_error) {
            errno = disconnected ? ENODEV : release_error;
            if (errno == ENOENT) active = 0;
            return -1;
        }
        active = 0; return 0;
    }
    errno = ENOTTY; return -1;
}
void *__wrap_mmap(void *addr, size_t size, int prot, int flags, int fd, off_t offset)
{
    assert(!addr && size == reply.mapping_bytes && prot == PROT_READ &&
           flags == MAP_SHARED && fd == 42 && !offset && active && !live_mapping);
    map_calls++;
    if (map_error) { errno = map_error; return MAP_FAILED; }
    if (null_mapping) { live_size = size; return NULL; }
    void *p = __real_mmap(NULL, size, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0);
    assert(p != MAP_FAILED);
    memcpy(p, &header, sizeof(header));
    int32_t values[16] = {1250, -2500};
    memcpy((char *)p + sizeof(header), values, sizeof(values));
    assert(mprotect(p, size, PROT_READ) == 0);
    live_mapping = p; live_size = size;
    return p;
}
int __wrap_munmap(void *addr, size_t size)
{
    assert(addr == live_mapping && size == live_size);
    unmap_calls++; event('U');
    if (unmap_error) { errno = unmap_error; return -1; }
    if (null_mapping) { assert(!addr); live_size = 0; return 0; }
    int ret = __real_munmap(addr, size);
    assert(ret == 0); live_mapping = NULL; live_size = 0; return ret;
}
static xtos_bci_session_t *create(void)
{
    xtos_bci_session_t *s = xtos_bci_session_create("/mock/bci"); assert(s); return s;
}
static void legacy(void)
{
    reset(); xtos_bci_session_t *s = create(); size_t size = 123;
    assert(!xtos_bci_get_mmap_buffer(s, &size) && errno == EOPNOTSUPP);
    assert(!size && !map_calls); xtos_bci_session_destroy(s); cases++;
}
static void success(void)
{
    reset(); xtos_bci_session_t *s = create(); struct xtos_bci_snapshot view;
    assert(xtos_bci_snapshot_acquire(s, 64, &view) == 0);
    assert(view.mapping == live_mapping && view.mapping_bytes == 4096);
    assert(view.header == view.mapping && view.header->token == reply.token);
    assert((const char *)view.payload == (const char *)view.mapping + 64);
    assert(view.payload_bytes == 64 && view.payload[0] == 1250 && view.payload[1] == -2500);
    assert(xtos_bci_snapshot_consume(s) == 0);
    assert(view.payload[0] == 1250);
    assert(xtos_bci_snapshot_consume(s) == -1 && errno == EALREADY);
    assert(consume_calls == 2);
    assert(xtos_bci_snapshot_release(s) == 0);
    assert(unmap_calls == 1 && release_calls == 1 && !live_mapping && !active);
    assert(xtos_bci_snapshot_release(s) == 0 && release_calls == 1);
    xtos_bci_session_destroy(s); assert(close_calls == 1); cases++;
}
static void validation(unsigned which)
{
    reset(); mutation = which;
    xtos_bci_session_t *s = create(); struct xtos_bci_snapshot view;
    memset(&view, 0xa5, sizeof(view));
    assert(xtos_bci_snapshot_acquire(s, 64, &view) == -1);
    assert(errno == EPROTO || errno == EOVERFLOW);
    struct xtos_bci_snapshot zero = {0};
    assert(memcmp(&view, &zero, sizeof(view)) == 0);
    assert(!live_mapping && release_calls == 1 && !active);
    assert(map_calls == (which < 20 ? 1 : 0));
    assert(unmap_calls == map_calls);
    mutation = 0;
    assert(xtos_bci_snapshot_acquire(s, 64, &view) == 0);
    assert(xtos_bci_snapshot_release(s) == 0);
    xtos_bci_session_destroy(s); cases++;
}
static void bad_args(void)
{
    reset(); xtos_bci_session_t *s = create(); struct xtos_bci_snapshot v;
    assert(xtos_bci_snapshot_acquire(NULL, 64, &v) == -1 && errno == EINVAL);
    assert(xtos_bci_snapshot_acquire(s, 64, NULL) == -1 && errno == EINVAL);
    assert(xtos_bci_snapshot_acquire(s, 0, &v) == -1 && errno == EINVAL);
    assert(xtos_bci_snapshot_acquire(s, 3, &v) == -1 && errno == EINVAL);
    assert(xtos_bci_snapshot_consume(NULL) == -1 && errno == EINVAL);
    assert(xtos_bci_snapshot_release(NULL) == -1 && errno == EINVAL);
    assert(xtos_bci_snapshot_consume(s) == -1 && errno == ENOENT);
    assert(xtos_bci_snapshot_release(s) == 0 && !release_calls);
    size_t n = 1;
    assert(!xtos_bci_get_mmap_buffer(NULL, &n) && errno == EINVAL && !n);
    xtos_bci_session_destroy(NULL);
    xtos_bci_session_destroy(s); cases++;
}
static void syscall_errors(unsigned which)
{
    reset(); xtos_bci_session_t *s = create(); struct xtos_bci_snapshot v, extra;
    if (which == 0) {
        acquire_error = EAGAIN;
        assert(xtos_bci_snapshot_acquire(s, 64, &v) == -1 && errno == EAGAIN);
        assert(!map_calls && !release_calls); acquire_error = 0;
    } else if (which == 1 || which == 2) {
        map_error = ENOMEM; release_error = which == 2 ? EINTR : 0;
        assert(xtos_bci_snapshot_acquire(s, 64, &v) == -1 && errno == ENOMEM);
        assert(!live_mapping && !unmap_calls && release_calls == 1);
        if (which == 2) {
            assert(xtos_bci_snapshot_acquire(s, 64, &extra) == -1 && errno == EBUSY);
            release_error = 0;
            assert(xtos_bci_snapshot_release(s) == 0);
        }
        map_error = 0;
    } else if (which == 3) {
        mutation = 1; release_error = EIO;
        assert(xtos_bci_snapshot_acquire(s, 64, &v) == -1 && errno == EPROTO);
        assert(!live_mapping && unmap_calls == 1 && release_calls == 1);
        mutation = 0; release_error = 0;
        assert(xtos_bci_snapshot_release(s) == 0);
    }
    assert(xtos_bci_snapshot_acquire(s, 64, &v) == 0);
    assert(xtos_bci_snapshot_acquire(s, 64, &extra) == -1 && errno == EBUSY);
    if (which == 4) {
        consume_error = EOVERFLOW;
        assert(xtos_bci_snapshot_consume(s) == -1 && errno == EOVERFLOW);
        assert(v.payload[0] == 1250); consume_error = 0;
    } else if (which == 5) {
        unmap_error = EIO;
        assert(xtos_bci_snapshot_release(s) == -1 && errno == EIO);
        assert(live_mapping && !release_calls && v.payload[0] == 1250);
        unmap_error = 0;
    } else if (which == 6) {
        release_error = EINTR;
        assert(xtos_bci_snapshot_release(s) == -1 && errno == EINTR);
        assert(!live_mapping && active);
        assert(xtos_bci_snapshot_consume(s) == -1 && errno == ENOENT);
        assert(xtos_bci_snapshot_acquire(s, 64, &extra) == -1 && errno == EBUSY);
        release_error = 0;
    } else if (which == 7) {
        release_error = ENOENT;
        assert(xtos_bci_snapshot_release(s) == -1 && errno == ENOENT);
        assert(!live_mapping && !active);
        release_error = 0;
    } else if (which == 8) {
        disconnected = 1;
        assert(xtos_bci_snapshot_consume(s) == -1 && errno == ENODEV);
        assert(v.payload[0] == 1250);
        assert(xtos_bci_snapshot_release(s) == -1 && errno == ENODEV);
        assert(!live_mapping && release_calls == 1);
        assert(xtos_bci_snapshot_release(s) == 0 && release_calls == 1);
        xtos_bci_session_destroy(s); cases++; return;
    }
    assert(xtos_bci_snapshot_release(s) == 0);
    assert(!live_mapping && !active);
    xtos_bci_session_destroy(s); cases++;
}
static void destroy_cleanup(int unbind)
{
    reset(); xtos_bci_session_t *s = create(); struct xtos_bci_snapshot v;
    assert(xtos_bci_start(s) == 0);
    assert(xtos_bci_snapshot_acquire(s, 64, &v) == 0);
    disconnected = unbind; stop_error = unbind ? ENODEV : EIO;
    xtos_bci_session_destroy(s);
    assert(strcmp(events, "SURC") == 0);
    assert(close_calls == 1 && release_calls == 1 && !live_mapping && !active);
    cases++;
}
static void large_request(void)
{
    reset(); xtos_bci_session_t *s = create(); struct xtos_bci_snapshot v;
    /* The kernel caps requested bytes; SDK must not multiply this value. */
    assert(xtos_bci_snapshot_acquire(s, SIZE_MAX - 3, &v) == 0);
    assert(v.payload_bytes == 64);
    assert(xtos_bci_snapshot_release(s) == 0);
    xtos_bci_session_destroy(s); cases++;
}
static void address_zero(void)
{
    reset(); xtos_bci_session_t *s = create(); struct xtos_bci_snapshot v;
    null_mapping = 1;
    assert(xtos_bci_snapshot_acquire(s, 64, &v) == -1 && errno == EPROTO);
    assert(unmap_calls == 1 && release_calls == 1 && !active && !live_size);
    struct xtos_bci_snapshot zero = {0};
    assert(!memcmp(&v, &zero, sizeof(v)));
    null_mapping = 0;
    assert(xtos_bci_snapshot_acquire(s, 64, &v) == 0);
    assert(xtos_bci_snapshot_release(s) == 0);
    xtos_bci_session_destroy(s); cases++;
}
static void repeated(void)
{
    reset(); xtos_bci_session_t *s = create(); struct xtos_bci_snapshot v;
    uint64_t token = 0;
    for (int i = 0; i < 100; i++) {
        assert(xtos_bci_snapshot_acquire(s, 64, &v) == 0);
        assert(v.header->token > token); token = v.header->token;
        assert(xtos_bci_snapshot_consume(s) == 0);
        assert(xtos_bci_snapshot_release(s) == 0);
    }
    assert(map_calls == 100 && unmap_calls == 100 && release_calls == 100);
    xtos_bci_session_destroy(s); cases++;
}
int main(void)
{
    legacy(); success(); bad_args(); repeated(); large_request(); address_zero();
    for (unsigned i = 0; i <= 8; i++) syscall_errors(i);
    destroy_cleanup(0); destroy_cleanup(1);
    for (unsigned i = 1; i <= 15; i++) validation(i);
    for (unsigned i = 20; i <= 37; i++) validation(i);
    printf("snapshot_sdk_cases=%d pass\n", cases); return 0;
}
