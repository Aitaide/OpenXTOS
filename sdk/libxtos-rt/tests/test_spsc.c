#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "xtos/spsc.h"

static int assertions;

#define CHECK(condition) do { \
    assertions++; \
    if (!(condition)) { \
        fprintf(stderr, "FAIL %s:%d: %s (errno=%d: %s)\n", \
                __FILE__, __LINE__, #condition, errno, strerror(errno)); \
        return 1; \
    } \
} while (0)

static struct xtos_sample_frame make_frame(uint64_t sequence,
                                           int32_t *samples)
{
    struct xtos_sample_frame frame = {
        .timestamp_ns = 1000000000ULL + sequence * 4000000ULL,
        .sequence = sequence,
        .channels = 2,
        .sample_rate_hz = 250,
        .quality_flags = 0,
        .samples_nv = samples,
    };
    samples[0] = (int32_t)sequence;
    samples[1] = -(int32_t)sequence;
    return frame;
}

static int test_empty_full_and_order(void)
{
    struct xtos_spsc_queue *queue = xtos_spsc_create(2, 2);
    struct xtos_sample_frame frame;
    struct xtos_spsc_stats stats;
    int32_t input[2];
    int32_t output[2];

    CHECK(queue != NULL);
    CHECK(xtos_spsc_capacity(queue) == 2);
    CHECK(xtos_spsc_size(queue) == 0);
    errno = 0;
    CHECK(xtos_spsc_pop(queue, &frame, output, 2) == -1 && errno == EAGAIN);

    frame = make_frame(1, input);
    CHECK(xtos_spsc_push(queue, &frame) == 0);
    frame = make_frame(2, input);
    CHECK(xtos_spsc_push(queue, &frame) == 0);
    errno = 0;
    frame = make_frame(3, input);
    CHECK(xtos_spsc_push(queue, &frame) == -1 && errno == EAGAIN);
    CHECK(xtos_spsc_size(queue) == 2);

    CHECK(xtos_spsc_pop(queue, &frame, output, 2) == 0);
    CHECK(frame.sequence == 1 && output[0] == 1 && output[1] == -1);
    CHECK(xtos_spsc_pop(queue, &frame, output, 2) == 0);
    CHECK(frame.sequence == 2 && output[0] == 2 && output[1] == -2);
    CHECK(xtos_spsc_size(queue) == 0);
    xtos_spsc_get_stats(queue, &stats);
    CHECK(stats.pushed == 2 && stats.popped == 2);
    CHECK(stats.rejected_full == 1 && stats.rejected_invalid == 0);
    xtos_spsc_destroy(queue);
    return 0;
}

static int test_invalid_frames(void)
{
    struct xtos_spsc_queue *queue = xtos_spsc_create(2, 2);
    struct xtos_sample_frame frame;
    int32_t samples[2] = {0, 0};

    CHECK(queue != NULL);
    errno = 0;
    CHECK(xtos_spsc_push(queue, NULL) == -1 && errno == EINVAL);
    frame = make_frame(1, samples);
    frame.timestamp_ns = 0;
    errno = 0;
    CHECK(xtos_spsc_push(queue, &frame) == -1 && errno == EINVAL);
    frame = make_frame(1, samples);
    frame.channels = 1;
    errno = 0;
    CHECK(xtos_spsc_push(queue, &frame) == -1 && errno == EINVAL);
    frame = make_frame(1, samples);
    errno = 0;
    CHECK(xtos_spsc_pop(queue, &frame, samples, 1) == -1 && errno == EINVAL);
    xtos_spsc_destroy(queue);
    return 0;
}

struct concurrent_context {
    struct xtos_spsc_queue *queue;
    size_t count;
    int failed;
};

static void *producer(void *opaque)
{
    struct concurrent_context *context = opaque;
    size_t sequence;
    int32_t samples[2];

    for (sequence = 1; sequence <= context->count; sequence++) {
        struct xtos_sample_frame frame = make_frame(sequence, samples);
        while (xtos_spsc_push(context->queue, &frame) != 0) {
            if (errno != EAGAIN) {
                context->failed = 1;
                return NULL;
            }
            sched_yield();
        }
    }
    return NULL;
}

static void *consumer(void *opaque)
{
    struct concurrent_context *context = opaque;
    size_t expected;
    int32_t samples[2];

    for (expected = 1; expected <= context->count; expected++) {
        struct xtos_sample_frame frame;
        while (xtos_spsc_pop(context->queue, &frame, samples, 2) != 0) {
            if (errno != EAGAIN) {
                context->failed = 1;
                return NULL;
            }
            sched_yield();
        }
        if (frame.sequence != expected || samples[0] != (int32_t)expected ||
            samples[1] != -(int32_t)expected) {
            context->failed = 1;
            return NULL;
        }
    }
    return NULL;
}

static int test_spsc_concurrency(void)
{
    struct concurrent_context context;
    struct xtos_spsc_stats stats;
    pthread_t producer_thread;
    pthread_t consumer_thread;

    context.queue = xtos_spsc_create(64, 2);
    context.count = 10000;
    context.failed = 0;
    CHECK(context.queue != NULL);
    CHECK(pthread_create(&producer_thread, NULL, producer, &context) == 0);
    CHECK(pthread_create(&consumer_thread, NULL, consumer, &context) == 0);
    CHECK(pthread_join(producer_thread, NULL) == 0);
    CHECK(pthread_join(consumer_thread, NULL) == 0);
    CHECK(context.failed == 0);
    CHECK(xtos_spsc_size(context.queue) == 0);
    xtos_spsc_get_stats(context.queue, &stats);
    CHECK(stats.pushed == context.count && stats.popped == context.count);
    xtos_spsc_destroy(context.queue);
    return 0;
}

int main(void)
{
    if (test_empty_full_and_order() != 0 ||
        test_invalid_frames() != 0 ||
        test_spsc_concurrency() != 0)
        return 1;
    printf("PASS assertions=%d\n", assertions);
    return 0;
}
