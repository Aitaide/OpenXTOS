#define _POSIX_C_SOURCE 200809L
#include "bci_stream.h"
#include "xtos/signal.h"
#include "xtos/spsc.h"

#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <semaphore.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* The SDK copies these eight samples plus two private generation words. */
#define QUEUE_WORDS (BCI_CHANNELS + 2)
struct atomic_stats {
    _Atomic uint64_t accepted, processed, dropped_full, invalid, resets,
                     output_errors;
};
struct bci_stream {
    struct xtos_spsc_queue *queue;
    pthread_t worker;
    sem_t ready;
    bci_output_fn output;
    void *opaque;
    struct xtos_bandpass_filter filters[BCI_CHANNELS];
    struct atomic_stats stats;
    _Atomic int closing, error;
    int joined;
    /* Producer-owned ordering and overload history. */
    int have_previous, pending_gap;
    uint64_t generation, sequence, receive_ns;
};

static void reset_filters(struct bci_stream *s)
{
    for (size_t ch = 0; ch < BCI_CHANNELS; ++ch)
        (void)xtos_bandpass_init(&s->filters[ch], 250.0, 5.0, 40.0);
    atomic_fetch_add_explicit(&s->stats.resets, 1, memory_order_relaxed);
}

static void *consume(void *opaque)
{
    struct bci_stream *s = opaque;
    uint64_t generation = 0;
    for (;;) {
        struct bci_output_frame out = {0};
        struct xtos_sample_frame queued;
        struct timespec now;
        int32_t words[QUEUE_WORDS];
        int rc;
        do {
            rc = sem_wait(&s->ready);
        } while (rc != 0 && errno == EINTR);
        if (rc != 0) {
            atomic_store_explicit(&s->error, EIO, memory_order_release);
            return NULL;
        }
        if (xtos_spsc_pop(s->queue, &queued, words, QUEUE_WORDS) != 0) {
            if (atomic_load_explicit(&s->closing, memory_order_acquire))
                return NULL;
            continue;
        }
        out.input.sequence = queued.sequence;
        out.input.receive_ns = queued.timestamp_ns;
        out.input.sample_rate_hz = queued.sample_rate_hz;
        out.input.quality = queued.quality_flags;
        memcpy(out.input.samples_nv, words, sizeof(out.input.samples_nv));
        memcpy(&out.input.generation, words + BCI_CHANNELS,
               sizeof(out.input.generation));
        out.filter_reset = generation != out.input.generation ||
                           (out.input.quality & BCI_QUALITY_GAP) != 0;
        if (out.filter_reset)
            reset_filters(s);
        generation = out.input.generation;
        for (size_t ch = 0; ch < BCI_CHANNELS; ++ch) {
            double value = out.input.samples_nv[ch];
            (void)xtos_bandpass_process(&s->filters[ch], &value,
                                        &out.filtered_nv[ch], 1);
        }
        if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
            atomic_store_explicit(&s->error, EIO, memory_order_release);
            return NULL;
        }
        out.processed_ns = (uint64_t)now.tv_sec * 1000000000u +
                           (uint64_t)now.tv_nsec;
        if (s->output(&out, s->opaque) != 0) {
            atomic_fetch_add_explicit(&s->stats.output_errors, 1,
                                      memory_order_relaxed);
            atomic_store_explicit(&s->error, EIO, memory_order_release);
            return NULL;
        }
        atomic_fetch_add_explicit(&s->stats.processed, 1, memory_order_relaxed);
    }
}

struct bci_stream *bci_stream_create(size_t capacity, bci_output_fn output,
                                     void *opaque)
{
    struct bci_stream *s;
    int rc;
    if (capacity == 0 || capacity >= SEM_VALUE_MAX || output == NULL) {
        errno = EINVAL;
        return NULL;
    }
    s = calloc(1, sizeof(*s));
    if (s == NULL)
        return NULL;
    atomic_init(&s->closing, 0);
    atomic_init(&s->error, 0);
    atomic_init(&s->stats.accepted, 0);
    atomic_init(&s->stats.processed, 0);
    atomic_init(&s->stats.dropped_full, 0);
    atomic_init(&s->stats.invalid, 0);
    atomic_init(&s->stats.resets, 0);
    atomic_init(&s->stats.output_errors, 0);
    s->queue = xtos_spsc_create(capacity, QUEUE_WORDS);
    if (s->queue == NULL) {
        free(s);
        return NULL;
    }
    if (sem_init(&s->ready, 0, 0) != 0) {
        int saved = errno;
        xtos_spsc_destroy(s->queue);
        free(s);
        errno = saved;
        return NULL;
    }
    s->output = output;
    s->opaque = opaque;
    rc = pthread_create(&s->worker, NULL, consume, s);
    if (rc != 0) {
        sem_destroy(&s->ready);
        xtos_spsc_destroy(s->queue);
        free(s);
        errno = rc;
        return NULL;
    }
    return s;
}

int bci_stream_push(struct bci_stream *s, const struct bci_input_frame *input)
{
    struct xtos_sample_frame frame;
    int32_t words[QUEUE_WORDS];
    if (s != NULL && atomic_load_explicit(&s->error, memory_order_acquire)) {
        errno = EIO;
        return -1;
    }
    if (s != NULL && atomic_load_explicit(&s->closing, memory_order_acquire)) {
        errno = EPIPE;
        return -1;
    }
    if (s == NULL || input == NULL || input->generation == 0 ||
        input->receive_ns == 0 || input->sample_rate_hz != 250 ||
        (input->quality & ~(BCI_QUALITY_GAP | BCI_QUALITY_RECOVERED)) != 0 ||
        (s->have_previous &&
         (input->generation < s->generation ||
          (input->generation == s->generation &&
           (input->sequence <= s->sequence || input->receive_ns < s->receive_ns))))) {
        if (s != NULL)
            atomic_fetch_add_explicit(&s->stats.invalid, 1, memory_order_relaxed);
        errno = EINVAL;
        return -1;
    }

    memcpy(words, input->samples_nv, sizeof(input->samples_nv));
    memcpy(words + BCI_CHANNELS, &input->generation, sizeof(input->generation));
    frame = (struct xtos_sample_frame) {
        .timestamp_ns = input->receive_ns, .sequence = input->sequence,
        .channels = QUEUE_WORDS, .sample_rate_hz = input->sample_rate_hz,
        .quality_flags = input->quality, .samples_nv = words
    };
    if (s->have_previous) {
        if (input->generation != s->generation)
            frame.quality_flags |= BCI_QUALITY_RECOVERED;
        else if (input->sequence - s->sequence != 1)
            frame.quality_flags |= BCI_QUALITY_GAP;
    }
    if (s->pending_gap)
        frame.quality_flags |= BCI_QUALITY_GAP | BCI_QUALITY_RECOVERED;
    if (xtos_spsc_push(s->queue, &frame) != 0) {
        if (errno == EAGAIN) {
            s->pending_gap = 1;
            atomic_fetch_add_explicit(&s->stats.dropped_full, 1,
                                      memory_order_relaxed);
        }
        return -1;
    }
    s->pending_gap = 0;
    s->have_previous = 1;
    s->generation = input->generation;
    s->sequence = input->sequence;
    s->receive_ns = input->receive_ns;
    atomic_fetch_add_explicit(&s->stats.accepted, 1, memory_order_relaxed);
    (void)sem_post(&s->ready);
    return 0;
}

int bci_stream_close(struct bci_stream *s)
{
    int rc;
    if (s == NULL) {
        errno = EINVAL;
        return -1;
    }
    if (pthread_equal(pthread_self(), s->worker)) {
        errno = EDEADLK;
        return -1;
    }
    if (!s->joined) {
        atomic_store_explicit(&s->closing, 1, memory_order_release);
        (void)sem_post(&s->ready);
        rc = pthread_join(s->worker, NULL);
        if (rc != 0) {
            errno = rc;
            return -1;
        }
        s->joined = 1;
    }
    rc = atomic_load_explicit(&s->error, memory_order_acquire);
    if (rc != 0) {
        errno = rc;
        return -1;
    }
    return 0;
}

void bci_stream_destroy(struct bci_stream *s)
{
    if (s == NULL || pthread_equal(pthread_self(), s->worker))
        return;
    (void)bci_stream_close(s);
    if (!s->joined)
        return;
    sem_destroy(&s->ready);
    xtos_spsc_destroy(s->queue);
    free(s);
}

void bci_stream_get_stats(const struct bci_stream *s, struct bci_stream_stats *out)
{
    if (s == NULL || out == NULL)
        return;
#define LOAD(name) out->name = atomic_load_explicit(&s->stats.name, memory_order_relaxed)
    LOAD(accepted);
    LOAD(processed);
    LOAD(dropped_full);
    LOAD(invalid);
    LOAD(resets);
    LOAD(output_errors);
#undef LOAD
}
