#ifndef XTOS_EEG_EXPORT_H
#define XTOS_EEG_EXPORT_H
#include "eeg_stream.h"
#include <stdio.h>
struct xtos_eeg_export_options {
    const char *device;
    uint64_t session_id, max_receive_age_ns;
    uint32_t window_frames, hop_frames, duration_ms, max_windows;
};
/* Single caller. Invalid metadata writes no bytes; errors are negative errno. */
int xtos_eeg_export_parse(int, char *const *, struct xtos_eeg_export_options *);
/* Format one validated record; return byte count or negative errno. */
int xtos_eeg_export_format(char *, size_t, const struct xtos_eeg_export_options *,
                           const struct xtos_eeg_stream_features *, uint64_t);
int xtos_eeg_export_window(FILE *, const struct xtos_eeg_export_options *,
                           const struct xtos_eeg_stream_features *, uint64_t);
#endif
