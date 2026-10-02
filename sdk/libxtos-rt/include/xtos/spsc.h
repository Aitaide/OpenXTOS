#ifndef XTOS_SPSC_H
#define XTOS_SPSC_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

struct xtos_sample_frame {
    uint64_t timestamp_ns;
    uint64_t sequence;
    uint32_t channels;
    uint32_t sample_rate_hz;
    uint32_t quality_flags;
    const int32_t *samples_nv;
};

struct xtos_spsc_stats {
    uint64_t pushed;
    uint64_t popped;
    uint64_t rejected_full;
    uint64_t rejected_invalid;
    uint64_t overwritten;
};

struct xtos_spsc_queue;

/* Create a fixed-capacity queue. Capacity is the maximum number of frames. */
struct xtos_spsc_queue *xtos_spsc_create(size_t capacity, uint32_t channels);
void xtos_spsc_destroy(struct xtos_spsc_queue *queue);

/* Push copies one frame and its channel samples into queue-owned storage. */
int xtos_spsc_push(struct xtos_spsc_queue *queue,
                   const struct xtos_sample_frame *frame);

/* Pop copies one frame and samples into caller-provided storage. */
int xtos_spsc_pop(struct xtos_spsc_queue *queue,
                  struct xtos_sample_frame *frame,
                  int32_t *samples_nv,
                  size_t samples_capacity);

void xtos_spsc_get_stats(const struct xtos_spsc_queue *queue,
                         struct xtos_spsc_stats *stats);
size_t xtos_spsc_size(const struct xtos_spsc_queue *queue);
size_t xtos_spsc_capacity(const struct xtos_spsc_queue *queue);

#ifdef __cplusplus
}
#endif

#endif
