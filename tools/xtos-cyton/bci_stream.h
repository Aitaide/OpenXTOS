#ifndef BCI_STREAM_H
#define BCI_STREAM_H

#include <stddef.h>
#include <stdint.h>

#define BCI_CHANNELS 8
#define BCI_QUALITY_GAP 1u
#define BCI_QUALITY_RECOVERED 2u

struct bci_input_frame {
    uint64_t sequence, generation, receive_ns;
    uint32_t sample_rate_hz, quality;
    int32_t samples_nv[8];
};
struct bci_output_frame {
    struct bci_input_frame input;
    uint64_t processed_ns;
    uint32_t filter_reset;
    double filtered_nv[8];
};
struct bci_stream;
typedef int (*bci_output_fn)(const struct bci_output_frame *, void *);

/*
 * One producer caller, one internal consumer. Fixed 250 Hz / 5-40 Hz filters.
 * capacity counts queued frames, excluding a frame currently in the callback.
 * All engine allocation happens in create; callback/user allocation is external.
 * callback runs on the consumer and MUST return in bounded time. No mechanism
 * can safely terminate an arbitrary stuck callback. Its frame pointer is valid
 * only until it returns. Nonzero return stops output, records EIO, and causes
 * subsequent pushes/close to fail with EIO; queued frames are then abandoned.
 */
struct bci_stream *bci_stream_create(size_t capacity, bci_output_fn, void *);
/*
 * Copies input. Full queue: -1/EAGAIN, increments dropped_full for each rejected
 * push attempt, never overwrites, and marks the next accepted frame GAP and
 * RECOVERED. Ordering is checked against the last accepted frame: generation
 * must be nonzero and never decrease. Within a generation sequence strictly
 * increases and nonzero receive_ns never decreases (batch times may be equal). A new
 * generation may restart sequence/time; receive time is never a nominal grid.
 * Missing sequences gain GAP; generation changes gain RECOVERED. Only the two
 * defined quality bits are accepted. Invalid input returns EINVAL. Closed
 * streams return EPIPE, failed streams EIO. An in-flight push may finish before
 * a concurrent callback failure is observed. The producer never waits for space.
 */
int bci_stream_push(struct bci_stream *, const struct bci_input_frame *);
/*
 * Stop the producer before close/destroy. One lifecycle caller only; no
 * concurrent close/destroy, and no destroy concurrent with any API call.
 * get_stats may run concurrently with push/consumer/close, but not destroy.
 * close drains successful output and joins; repeated close returns the same
 * outcome. Calling close from the callback returns EDEADLK. destroy from the
 * callback is forbidden (defensively returns without freeing). destroy(NULL)
 * is harmless. close(NULL) returns EINVAL. destroy also closes if necessary.
 * The first frame, generation changes, sequence gaps and GAP reset all eight
 * filters. processed counts successful callbacks; resets counts reset frames
 * including an output frame whose callback fails. Counters are atomic snapshots
 * individually, not a transaction across fields; inspect after close for totals.
 * receive_ns is host receive time; processed_ns is CLOCK_MONOTONIC before the
 * callback, nondecreasing across outputs. Neither is physical board latency.
 */
int bci_stream_close(struct bci_stream *);
void bci_stream_destroy(struct bci_stream *);
struct bci_stream_stats {
    uint64_t accepted, processed, dropped_full, invalid, resets, output_errors;
};
void bci_stream_get_stats(const struct bci_stream *, struct bci_stream_stats *);

#endif
