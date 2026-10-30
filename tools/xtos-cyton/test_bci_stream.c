#define _POSIX_C_SOURCE 200809L
#include "bci_stream.h"
#include <assert.h>
#include <errno.h>
#include <math.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#ifdef BCI_TEST_ALLOC_GUARD
static pthread_t allocation_owner;
static _Atomic unsigned worker_allocations;
static void note_allocation(void)
{
    if (!pthread_equal(pthread_self(), allocation_owner))
        atomic_fetch_add_explicit(&worker_allocations, 1, memory_order_relaxed);
}
void *__real_malloc(size_t);
void *__real_calloc(size_t, size_t);
void *__real_realloc(void *, size_t);
void *__wrap_malloc(size_t n) { note_allocation(); return __real_malloc(n); }
void *__wrap_calloc(size_t n, size_t m) { note_allocation(); return __real_calloc(n, m); }
void *__wrap_realloc(void *p, size_t n) { note_allocation(); return __real_realloc(p, n); }
#endif

struct capture {
    struct bci_output_frame frames[1024];
    size_t count;
    pthread_t producer, consumer;
};
static int collect(const struct bci_output_frame *frame, void *opaque)
{
    struct capture *c = opaque;
    assert(c->count < 1024);
    c->consumer = pthread_self();
    c->frames[c->count++] = *frame;
    return 0;
}
static struct bci_input_frame make_frame(uint64_t sequence)
{
    struct bci_input_frame f = { .sequence = sequence, .generation = 1,
        .receive_ns = 1000000000 + sequence * 4000000, .sample_rate_hz = 250 };
    for (size_t ch = 0; ch < BCI_CHANNELS; ++ch)
        f.samples_nv[ch] = (int32_t)((ch + 1) * 1000);
    return f;
}
static void test_copy_thread_drain(void)
{
    struct capture c = {0};
    struct bci_stream_stats stats;
    struct bci_input_frame f = make_frame(0);
    struct bci_stream *s = bci_stream_create(2, collect, &c);
    assert(s != NULL);
    c.producer = pthread_self();
    assert(bci_stream_push(s, &f) == 0);
    memset(&f, 0, sizeof(f));
    assert(bci_stream_close(s) == 0);
    assert(c.count == 1 && !pthread_equal(c.producer, c.consumer));
    assert(c.frames[0].input.samples_nv[7] == 8000);
    assert(c.frames[0].filter_reset == 1 && c.frames[0].processed_ns != 0);
    bci_stream_get_stats(s, &stats);
    assert(stats.accepted == 1 && stats.processed == 1 && stats.resets == 1);
    assert(bci_stream_close(s) == 0);
    bci_stream_destroy(s);
}
/* Independent transposed-direct-form oracle, coefficients precomputed using
 * k=tan(pi*f/fs), the bilinear Butterworth form (not the SDK recurrence). */
static double oracle_step(double state[2], double x, const double b[5])
{
    double y = b[0] * x + state[0];
    state[0] = b[1] * x - b[3] * y + state[1];
    state[1] = b[2] * x - b[4] * y;
    return y;
}
static void check_oracle(const struct capture *c)
{
    static const double hp[5] = {0.914969144113082722, -1.82993828822616544,
        0.914969144113082722, -1.82269492519630827, 0.837181651256022619};
    static const double lp[5] = {0.145323883877042426, 0.290647767754084851,
        0.145323883877042426, -0.671029090774096226, 0.252324626282265874};
    double state[BCI_CHANNELS][2][2] = {{{0}}};
    for (size_t i = 0; i < c->count; ++i) {
        if (c->frames[i].filter_reset)
            memset(state, 0, sizeof(state));
        for (size_t ch = 0; ch < BCI_CHANNELS; ++ch) {
            double x = c->frames[i].input.samples_nv[ch];
            double y = oracle_step(state[ch][1],
                oracle_step(state[ch][0], x, hp), lp);
            assert(fabs(y - c->frames[i].filtered_nv[ch]) <
                   0.00001 + 0.000000001 * fabs(y));
        }
        if (i != 0)
            assert(c->frames[i].processed_ns >= c->frames[i - 1].processed_ns);
    }
}
static void test_eight_channel_reference(void)
{
    struct capture c = {0};
    struct bci_stream_stats stats;
    struct bci_stream *s = bci_stream_create(512, collect, &c);
    assert(s != NULL);
    for (uint64_t i = 0; i < 512; ++i) {
        struct bci_input_frame f = make_frame(i);
        for (size_t ch = 0; ch < BCI_CHANNELS; ++ch)
            f.samples_nv[ch] = (int32_t)(100000 * sin((double)i *
                (double)(ch + 1) * 0.13) + (double)(ch * 123));
        if (i == 0) {
            f.samples_nv[0] = INT32_MIN;
            f.samples_nv[7] = INT32_MAX;
        }
        assert(bci_stream_push(s, &f) == 0);
        memset(&f, 0, sizeof(f));
    }
    assert(bci_stream_close(s) == 0);
    assert(c.count == 512);
    check_oracle(&c);
    bci_stream_get_stats(s, &stats);
    assert(stats.accepted == 512 && stats.processed == 512 && stats.resets == 1);
    bci_stream_destroy(s);
}
static void test_gap_generation_and_batch_time(void)
{
    struct capture c = {0};
    struct bci_stream *s = bci_stream_create(8, collect, &c);
    struct bci_input_frame f = make_frame(10);
    assert(s != NULL && bci_stream_push(s, &f) == 0);
    ++f.sequence; /* multiple frames from one read share host receive time */
    assert(bci_stream_push(s, &f) == 0);
    f.sequence += 2;
    assert(bci_stream_push(s, &f) == 0);
    ++f.sequence;
    f.quality = BCI_QUALITY_GAP;
    assert(bci_stream_push(s, &f) == 0);
    f = make_frame(0);
    f.generation = 2;
    f.receive_ns = 1;
    assert(bci_stream_push(s, &f) == 0);
    ++f.sequence;
    f.quality = BCI_QUALITY_RECOVERED;
    assert(bci_stream_push(s, &f) == 0);
    assert(bci_stream_close(s) == 0 && c.count == 6);
    assert(c.frames[0].filter_reset == 1);
    assert(c.frames[1].filter_reset == 0);
    assert(c.frames[2].filter_reset == 1);
    assert(c.frames[2].input.quality & BCI_QUALITY_GAP);
    assert(c.frames[3].filter_reset == 1);
    assert(c.frames[4].filter_reset == 1);
    assert(c.frames[4].input.quality & BCI_QUALITY_RECOVERED);
    assert(c.frames[5].filter_reset == 0);
    check_oracle(&c);
    bci_stream_destroy(s);
}
static void expect_invalid(struct bci_stream *s, const struct bci_input_frame *f)
{
    errno = 0;
    assert(bci_stream_push(s, f) == -1 && errno == EINVAL);
}
static void test_invalid_and_ordering(void)
{
    struct capture c = {0};
    struct bci_stream_stats stats;
    struct bci_input_frame base = make_frame(255), f;
    struct bci_stream *s;
    assert(bci_stream_create(0, collect, &c) == NULL && errno == EINVAL);
    assert(bci_stream_create(1, NULL, &c) == NULL && errno == EINVAL);
    assert(bci_stream_create(SIZE_MAX, collect, &c) == NULL && errno == EINVAL);
    assert(bci_stream_close(NULL) == -1 && errno == EINVAL);
    bci_stream_destroy(NULL);
    expect_invalid(NULL, &base);
    s = bci_stream_create(16, collect, &c);
    assert(s != NULL);
    expect_invalid(s, NULL);
    f = base; f.receive_ns = 0; expect_invalid(s, &f);
    f = base; f.generation = 0; expect_invalid(s, &f);
    f = base; f.sample_rate_hz = 249; expect_invalid(s, &f);
    f = base; f.quality = 4; expect_invalid(s, &f);
    assert(bci_stream_push(s, &base) == 0);
    expect_invalid(s, &base); /* duplicate */
    f = base; --f.sequence; expect_invalid(s, &f);
    f = base; ++f.sequence; --f.receive_ns; expect_invalid(s, &f);
    f = base; f.sequence = 0; expect_invalid(s, &f); /* raw counter wrap invalid */
    f = base; ++f.sequence; assert(bci_stream_push(s, &f) == 0);
    f.generation = 3; f.sequence = 0; f.receive_ns = 1;
    assert(bci_stream_push(s, &f) == 0);
    f.generation = 2; expect_invalid(s, &f); /* stale generation */
    f.generation = 3; f.sequence = UINT64_MAX;
    assert(bci_stream_push(s, &f) == 0);
    f.sequence = 0; expect_invalid(s, &f); /* no uint64 wrap within generation */
    f.generation = UINT64_MAX;
    assert(bci_stream_push(s, &f) == 0);
    f.generation = 1; expect_invalid(s, &f);
    assert(bci_stream_close(s) == 0 && c.count == 5);
    bci_stream_get_stats(s, &stats);
    assert(stats.invalid == 12 && stats.accepted == 5 && stats.processed == 5);
    bci_stream_get_stats(NULL, &stats);
    bci_stream_get_stats(s, NULL);
    f = make_frame(0);
    assert(bci_stream_push(s, &f) == -1 && errno == EPIPE);
    bci_stream_destroy(s);
}
struct gate {
    struct capture capture;
    pthread_mutex_t lock;
    pthread_cond_t changed;
    int released, fail;
};
static void init_gate(struct gate *g)
{
    memset(g, 0, sizeof(*g));
    assert(pthread_mutex_init(&g->lock, NULL) == 0);
    assert(pthread_cond_init(&g->changed, NULL) == 0);
}
static int gated_collect(const struct bci_output_frame *f, void *opaque)
{
    struct gate *g = opaque;
    assert(pthread_mutex_lock(&g->lock) == 0);
    collect(f, &g->capture);
    assert(pthread_cond_broadcast(&g->changed) == 0);
    while (g->capture.count == 1 && !g->released)
        assert(pthread_cond_wait(&g->changed, &g->lock) == 0);
    assert(pthread_mutex_unlock(&g->lock) == 0);
    return g->fail;
}
static void wait_seen(struct gate *g, size_t count)
{
    struct timespec deadline;
    assert(clock_gettime(CLOCK_REALTIME, &deadline) == 0);
    deadline.tv_sec += 5;
    assert(pthread_mutex_lock(&g->lock) == 0);
    while (g->capture.count < count)
        assert(pthread_cond_timedwait(&g->changed, &g->lock, &deadline) == 0);
    assert(pthread_mutex_unlock(&g->lock) == 0);
}
static void release_gate(struct gate *g)
{
    assert(pthread_mutex_lock(&g->lock) == 0);
    g->released = 1;
    assert(pthread_cond_broadcast(&g->changed) == 0);
    assert(pthread_mutex_unlock(&g->lock) == 0);
}
static void destroy_gate(struct gate *g)
{
    assert(pthread_mutex_destroy(&g->lock) == 0);
    assert(pthread_cond_destroy(&g->changed) == 0);
}
static void test_overload_recovery(size_t capacity, int retry_same)
{
    struct gate g;
    struct bci_stream_stats stats;
    struct bci_stream *s;
    struct bci_input_frame f = make_frame(0);
    init_gate(&g);
    s = bci_stream_create(capacity, gated_collect, &g);
    assert(s != NULL && bci_stream_push(s, &f) == 0);
    wait_seen(&g, 1); /* consumer holds first frame, queue is empty */
    for (size_t i = 1; i <= capacity; ++i) {
        f = make_frame(i);
        assert(bci_stream_push(s, &f) == 0);
        memset(&f, 0, sizeof(f));
    }
    for (size_t i = 1; i <= 3; ++i) {
        f = make_frame(capacity + (retry_same ? 1 : i));
        assert(bci_stream_push(s, &f) == -1 && errno == EAGAIN);
    }
    bci_stream_get_stats(s, &stats);
    assert(stats.accepted == capacity + 1 && stats.dropped_full == 3);
    release_gate(&g);
    wait_seen(&g, capacity + 1);
    f = make_frame(capacity + (retry_same ? 1u : 4u));
    assert(bci_stream_push(s, &f) == 0);
    assert(bci_stream_close(s) == 0);
    assert(g.capture.count == capacity + 2);
    for (size_t i = 0; i <= capacity; ++i) {
        assert(g.capture.frames[i].input.sequence == i);
        assert(g.capture.frames[i].input.samples_nv[7] == 8000);
    }
    assert(g.capture.frames[capacity + 1].input.sequence ==
           capacity + (retry_same ? 1u : 4u));
    assert(g.capture.frames[capacity + 1].input.quality ==
           (BCI_QUALITY_GAP | BCI_QUALITY_RECOVERED));
    assert(g.capture.frames[capacity + 1].filter_reset == 1);
    check_oracle(&g.capture);
    bci_stream_get_stats(s, &stats);
    assert(stats.processed == capacity + 2 && stats.resets == 2 &&
           stats.dropped_full == 3 && stats.invalid == 0);
    assert(bci_stream_close(s) == 0);
    bci_stream_destroy(s);
    destroy_gate(&g);
}
static void test_callback_failure(void)
{
    struct gate g;
    struct bci_stream_stats stats;
    struct bci_input_frame f = make_frame(0);
    struct bci_stream *s;
    init_gate(&g);
    g.fail = 17;
    s = bci_stream_create(2, gated_collect, &g);
    assert(s != NULL && bci_stream_push(s, &f) == 0);
    wait_seen(&g, 1);
    f = make_frame(1); assert(bci_stream_push(s, &f) == 0);
    f = make_frame(2); assert(bci_stream_push(s, &f) == 0);
    release_gate(&g);
    {
        struct timespec now, deadline;
        assert(clock_gettime(CLOCK_MONOTONIC, &deadline) == 0);
        deadline.tv_sec += 5;
        f = make_frame(3);
        for (;;) {
            assert(bci_stream_push(s, &f) == -1);
            if (errno == EIO)
                break;
            assert(errno == EAGAIN);
            assert(clock_gettime(CLOCK_MONOTONIC, &now) == 0);
            assert(now.tv_sec < deadline.tv_sec);
            sched_yield();
        }
    }
    assert(bci_stream_close(s) == -1 && errno == EIO);
    assert(bci_stream_close(s) == -1 && errno == EIO);
    f = make_frame(3);
    assert(bci_stream_push(s, &f) == -1 && errno == EIO);
    bci_stream_get_stats(s, &stats);
    assert(stats.accepted == 3 && stats.processed == 0 && stats.output_errors == 1);
    assert(g.capture.count == 1);
    bci_stream_destroy(s);
    destroy_gate(&g);
}
static void test_empty_close_destroy_drain(void)
{
    struct capture c = {0};
    struct bci_stream *s = bci_stream_create(1, collect, &c);
    assert(s != NULL && bci_stream_close(s) == 0);
    assert(bci_stream_close(s) == 0 && c.count == 0);
    bci_stream_destroy(s);
    s = bci_stream_create(3, collect, &c);
    assert(s != NULL);
    for (uint64_t i = 0; i < 3; ++i) {
        struct bci_input_frame f = make_frame(i);
        assert(bci_stream_push(s, &f) == 0);
    }
    bci_stream_destroy(s);
    assert(c.count == 3);
}
struct self_close {
    struct bci_stream *stream;
    int called;
};
static int close_from_callback(const struct bci_output_frame *f, void *opaque)
{
    struct self_close *c = opaque;
    (void)f;
    assert(bci_stream_close(c->stream) == -1 && errno == EDEADLK);
    c->called++;
    return 0;
}
static void test_self_close_rejected(void)
{
    struct self_close c = {0};
    struct bci_input_frame f = make_frame(0);
    c.stream = bci_stream_create(1, close_from_callback, &c);
    assert(c.stream != NULL && bci_stream_push(c.stream, &f) == 0);
    assert(bci_stream_close(c.stream) == 0 && c.called == 1);
    bci_stream_destroy(c.stream);
}
struct sustained {
    uint64_t count;
};
static int count_ordered(const struct bci_output_frame *f, void *opaque)
{
    struct sustained *c = opaque;
    assert(f->input.sequence == c->count++);
    for (size_t ch = 0; ch < BCI_CHANNELS; ++ch)
        assert(f->input.samples_nv[ch] == (int32_t)(f->input.sequence + ch));
    return 0;
}
static void test_sustained_wrap_and_atomic_stats(void)
{
    struct sustained c = {0};
    struct bci_stream_stats stats;
    struct bci_stream *s = bci_stream_create(3, count_ordered, &c);
    uint64_t rejected = 0;
    assert(s != NULL);
    for (uint64_t i = 0; i < 10000; ++i) {
        struct bci_input_frame f = make_frame(i);
        for (size_t ch = 0; ch < BCI_CHANNELS; ++ch)
            f.samples_nv[ch] = (int32_t)(i + ch);
        while (bci_stream_push(s, &f) != 0) {
            assert(errno == EAGAIN);
            ++rejected;
            sched_yield();
        }
        bci_stream_get_stats(s, &stats);
        assert(stats.accepted == i + 1 && stats.processed <= i + 1);
        assert(stats.dropped_full == rejected);
        memset(&f, 0, sizeof(f));
    }
    assert(bci_stream_close(s) == 0 && c.count == 10000);
    bci_stream_get_stats(s, &stats);
    assert(stats.accepted == 10000 && stats.processed == 10000);
    assert(stats.dropped_full == rejected && stats.output_errors == 0);
    bci_stream_destroy(s);
}
int main(void)
{
#ifdef BCI_TEST_ALLOC_GUARD
    allocation_owner = pthread_self();
#endif
    test_copy_thread_drain();
    test_eight_channel_reference();
    test_gap_generation_and_batch_time();
    test_invalid_and_ordering();
    test_overload_recovery(1, 0);
    test_overload_recovery(2, 0);
    test_overload_recovery(3, 0);
    test_overload_recovery(8, 0);
    test_overload_recovery(3, 1);
    test_callback_failure();
    test_empty_close_destroy_drain();
    test_self_close_rejected();
    test_sustained_wrap_and_atomic_stats();
#ifdef BCI_TEST_ALLOC_GUARD
    assert(atomic_load_explicit(&worker_allocations, memory_order_relaxed) == 0);
#endif
    puts("PASS 13 stream tests");
    return 0;
}
