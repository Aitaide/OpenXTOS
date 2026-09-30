#include <errno.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "xtos/spsc.h"

struct xtos_spsc_slot {
    struct xtos_sample_frame frame;
    int32_t *samples_nv;
};

struct xtos_spsc_atomic_stats {
    _Atomic uint64_t pushed;
    _Atomic uint64_t popped;
    _Atomic uint64_t rejected_full;
    _Atomic uint64_t rejected_invalid;
    _Atomic uint64_t overwritten;
};

struct xtos_spsc_queue {
    size_t capacity;
    uint32_t channels;
    struct xtos_spsc_slot *slots;
    _Atomic size_t head;
    _Atomic size_t tail;
    struct xtos_spsc_atomic_stats stats;
};

struct xtos_spsc_queue *xtos_spsc_create(size_t capacity, uint32_t channels)
{
    struct xtos_spsc_queue *queue;
    size_t index;

    if (capacity == 0 || channels == 0) {
        errno = EINVAL;
        return NULL;
    }
    queue = calloc(1, sizeof(*queue));
    if (queue == NULL)
        return NULL;
    queue->slots = calloc(capacity, sizeof(*queue->slots));
    if (queue->slots == NULL) {
        free(queue);
        return NULL;
    }
    for (index = 0; index < capacity; index++) {
        queue->slots[index].samples_nv = calloc(channels, sizeof(int32_t));
        if (queue->slots[index].samples_nv == NULL) {
            while (index > 0)
                free(queue->slots[--index].samples_nv);
            free(queue->slots);
            free(queue);
            return NULL;
        }
    }
    queue->capacity = capacity;
    queue->channels = channels;
    atomic_init(&queue->head, 0);
    atomic_init(&queue->tail, 0);
    return queue;
}

void xtos_spsc_destroy(struct xtos_spsc_queue *queue)
{
    size_t index;

    if (queue == NULL)
        return;
    for (index = 0; index < queue->capacity; index++)
        free(queue->slots[index].samples_nv);
    free(queue->slots);
    free(queue);
}

int xtos_spsc_push(struct xtos_spsc_queue *queue,
                   const struct xtos_sample_frame *frame)
{
    size_t head;
    size_t tail;
    struct xtos_spsc_slot *slot;

    if (queue == NULL || frame == NULL || frame->samples_nv == NULL ||
        frame->channels != queue->channels || frame->timestamp_ns == 0) {
        if (queue != NULL)
            atomic_fetch_add_explicit(&queue->stats.rejected_invalid, 1,
                                       memory_order_relaxed);
        errno = EINVAL;
        return -1;
    }
    head = atomic_load_explicit(&queue->head, memory_order_relaxed);
    tail = atomic_load_explicit(&queue->tail, memory_order_acquire);
    if (head - tail >= queue->capacity) {
        atomic_fetch_add_explicit(&queue->stats.rejected_full, 1,
                                   memory_order_relaxed);
        errno = EAGAIN;
        return -1;
    }
    slot = &queue->slots[head % queue->capacity];
    slot->frame = *frame;
    memcpy(slot->samples_nv, frame->samples_nv,
           queue->channels * sizeof(*frame->samples_nv));
    atomic_store_explicit(&queue->head, head + 1, memory_order_release);
    atomic_fetch_add_explicit(&queue->stats.pushed, 1, memory_order_relaxed);
    return 0;
}

int xtos_spsc_pop(struct xtos_spsc_queue *queue,
                  struct xtos_sample_frame *frame,
                  int32_t *samples_nv,
                  size_t samples_capacity)
{
    size_t head;
    size_t tail;
    struct xtos_spsc_slot *slot;

    if (queue == NULL || frame == NULL || samples_nv == NULL ||
        samples_capacity < queue->channels) {
        errno = EINVAL;
        return -1;
    }
    tail = atomic_load_explicit(&queue->tail, memory_order_relaxed);
    head = atomic_load_explicit(&queue->head, memory_order_acquire);
    if (tail == head) {
        errno = EAGAIN;
        return -1;
    }
    slot = &queue->slots[tail % queue->capacity];
    *frame = slot->frame;
    frame->samples_nv = samples_nv;
    memcpy(samples_nv, slot->samples_nv,
           queue->channels * sizeof(*samples_nv));
    atomic_store_explicit(&queue->tail, tail + 1, memory_order_release);
    atomic_fetch_add_explicit(&queue->stats.popped, 1, memory_order_relaxed);
    return 0;
}

void xtos_spsc_get_stats(const struct xtos_spsc_queue *queue,
                         struct xtos_spsc_stats *stats)
{
    if (queue == NULL || stats == NULL)
        return;
    stats->pushed = atomic_load_explicit(&queue->stats.pushed,
                                         memory_order_relaxed);
    stats->popped = atomic_load_explicit(&queue->stats.popped,
                                         memory_order_relaxed);
    stats->rejected_full = atomic_load_explicit(&queue->stats.rejected_full,
                                                memory_order_relaxed);
    stats->rejected_invalid = atomic_load_explicit(&queue->stats.rejected_invalid,
                                                   memory_order_relaxed);
    stats->overwritten = atomic_load_explicit(&queue->stats.overwritten,
                                              memory_order_relaxed);
}

size_t xtos_spsc_size(const struct xtos_spsc_queue *queue)
{
    size_t head;
    size_t tail;

    if (queue == NULL)
        return 0;
    head = atomic_load_explicit(&queue->head, memory_order_acquire);
    tail = atomic_load_explicit(&queue->tail, memory_order_acquire);
    return head - tail;
}

size_t xtos_spsc_capacity(const struct xtos_spsc_queue *queue)
{
    return queue == NULL ? 0 : queue->capacity;
}
