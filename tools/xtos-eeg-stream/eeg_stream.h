#ifndef XTOS_EEG_STREAM_H
#define XTOS_EEG_STREAM_H
#include <xtos/bci.h>
#include <xtos/eeg_window.h>

#ifdef __cplusplus
extern "C" {
#endif
/* Internal P3 tool API: caller-owned, externally serialized, no overlapping
 * inputs/outputs/state. No allocation, START/STOP or descriptor ownership.
 * Batch pointers must refer to their declared number of readable records.
 */
struct xtos_eeg_stream {
    struct xtos_eeg_window window;
    uint64_t session_id, barrier_id;
    uint32_t initialized, exhausted;
};
struct xtos_eeg_stream_features {
    uint64_t barrier_id;
    struct xtos_eeg_features features;
};
int xtos_eeg_stream_init(struct xtos_eeg_stream *, const struct xtos_eeg_window_options *, uint64_t session_id);
/* 0 accepted (possibly with continuity barriers), 1 disabled-channel batch
 * dropped, otherwise negative errno. Malformed batch clears all accumulation.
 */
int xtos_eeg_stream_accept_batch(struct xtos_eeg_stream *, const struct xtos_bci_frame_batch *);
int xtos_eeg_stream_features(struct xtos_eeg_stream *, uint64_t now_ns, struct xtos_eeg_stream_features *);
int xtos_eeg_stream_barrier(struct xtos_eeg_stream *);
int xtos_eeg_stream_rebind(struct xtos_eeg_stream *, uint64_t session_id);
int xtos_eeg_stream_read(struct xtos_eeg_stream *, xtos_bci_session_t *, uint32_t capacity_frames);
#ifdef __cplusplus
}
#endif
#endif
