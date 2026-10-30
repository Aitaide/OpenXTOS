#ifndef XTOS_EEG_WINDOW_H
#define XTOS_EEG_WINDOW_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

#define XTOS_EEG_WINDOW_VERSION 1u
#define XTOS_EEG_MAX_CHANNELS 8u
#define XTOS_EEG_MAX_WINDOW_FRAMES 4096u
#define XTOS_EEG_FEATURE_MEAN_RMS_V1 1u
#define XTOS_EEG_UNIT_NANOVOLT 1u
#define XTOS_EEG_CLOCK_MONOTONIC 1u
#define XTOS_EEG_SOURCE_SERDEV 1u
#define XTOS_EEG_SOURCE_SYNTHETIC 2u
#define XTOS_EEG_CONFIG_VERIFIED 1u
#define XTOS_EEG_COUNTER_GAP 2u
#define XTOS_EEG_RESYNC 4u
#define XTOS_EEG_ADC_RAIL 8u
#define XTOS_EEG_CONTINUITY_UNKNOWN 16u
#define XTOS_EEG_QUALITY_KNOWN 31u
#define XTOS_EEG_PUSH_ACCEPTED 0
#define XTOS_EEG_PUSH_RESET_ACCEPTED 1
#define XTOS_EEG_PUSH_DROPPED 2

struct xtos_eeg_window_options {
    uint32_t version, channels, sample_rate_hz, window_frames, hop_frames;
    uint64_t max_receive_age_ns;
};
struct xtos_eeg_sample {
    uint32_t version, channels, sample_rate_hz, unit, host_clock;
    uint32_t quality_flags, source_flags, enabled_mask, missing_before;
    uint64_t session_id, stream_generation, config_generation, sequence, host_rx_ns;
    int32_t samples_nv[XTOS_EEG_MAX_CHANNELS];
};
struct xtos_eeg_features {
    uint32_t version, feature_kind, feature_count, channels, sample_rate_hz;
    uint32_t source_flags, enabled_mask, frame_count;
    uint64_t window_id, session_id, stream_generation, config_generation;
    uint64_t first_sequence, last_sequence, first_host_rx_ns, last_host_rx_ns;
    double values_uv[2*XTOS_EEG_MAX_CHANNELS];
};
/* Caller-owned fixed storage; use functions instead of mutating internals.
 * No allocation or device I/O occurs. Receive timestamps may be equal.
 * Invalid input preserves state; a production adapter must treat it as a barrier.
 * One owner or external synchronization is required. Input/output storage must
 * not overlap the window state; reset preserves the successful window-id fence.
 */
struct xtos_eeg_window {
    struct xtos_eeg_window_options options;
    uint32_t initialized, count, next, since_emit, have_identity;
    uint32_t source_flags, enabled_mask;
    uint64_t window_id, session_id, stream_generation, config_generation;
    uint64_t last_sequence, last_host_rx_ns;
    uint64_t receive_ns[XTOS_EEG_MAX_WINDOW_FRAMES];
    int32_t samples_nv[XTOS_EEG_MAX_WINDOW_FRAMES][XTOS_EEG_MAX_CHANNELS];
};
int xtos_eeg_window_init(struct xtos_eeg_window *, const struct xtos_eeg_window_options *);
int xtos_eeg_window_push(struct xtos_eeg_window *, const struct xtos_eeg_sample *);
int xtos_eeg_window_features(struct xtos_eeg_window *, uint64_t now_ns, struct xtos_eeg_features *);
void xtos_eeg_window_reset(struct xtos_eeg_window *);
#ifdef __cplusplus
}
#endif
#endif
