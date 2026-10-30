/* SPDX-License-Identifier: GPL-2.0 */
#ifndef CYTON_DIAGNOSTIC_SERIAL_H
#define CYTON_DIAGNOSTIC_SERIAL_H
#include <signal.h>
#include <stdint.h>
#include "../../kernel/linux/drivers/xtos/bci/xtos_cyton_config.h"
/* Internal host API, not UAPI. Call serially with exclusive device ownership.
 * No estimator/calibration verdict is produced. Nominal 4.5 V conversion uses
 * signed counts * 4500000000 / (actual gain * 8388607), truncating toward zero.
 * SHORTED disconnects SRB2 and SRB1; CONFIG2 is preserved. */
enum cyton_diagnostic_mode { CYTON_DIAGNOSTIC_SHORTED = 1, CYTON_DIAGNOSTIC_IMPEDANCE = 2 };
enum cyton_diagnostic_restoration {
    CYTON_DIAGNOSTIC_NOT_ATTEMPTED = 0, CYTON_DIAGNOSTIC_RESTORED = 1,
    CYTON_DIAGNOSTIC_RESTORE_FAILED = 2, CYTON_DIAGNOSTIC_RESTORE_UNKNOWN = 3
};
struct cyton_diagnostic_options {
    const char *path;
    enum cyton_diagnostic_mode mode;
    unsigned int channel; /* zero based, 0..7 */
    char input; /* impedance: exactly P or N */
    unsigned int settle_frames; /* 0..1024 */
    unsigned int sample_frames; /* 32..4096 */
    unsigned int command_timeout_ms; /* 1..3000 */
    unsigned int operation_timeout_ms; /* 1..120000, excludes cleanup */
    const volatile sig_atomic_t *stop;
};
struct cyton_diagnostic_result {
    struct xtos_cyton_config before, test, after;
    char firmware[64];
    enum cyton_diagnostic_mode source_mode;
    unsigned int selected_channel;
    char selected_input;
    unsigned int actual_gain;
    unsigned int frames_observed, settled_frames, sample_count;
    unsigned int bad_frames, counter_errors, rail_errors;
    unsigned int partial_command;
    int error, cleanup_error;
    enum cyton_diagnostic_restoration restoration;
    int64_t first_receive_ns, last_receive_ns;
    int32_t samples_nv[4096]; /* only the selected channel, caller-owned */
};
/* 0 only for a complete clean window AND verified restoration, else -1 with
 * original errno retained. Result is initialized and partial evidence retained.
 * Cleanup has its own 10 s deadline and ignores stop. No reconnect. */
int cyton_diagnostic_capture(const struct cyton_diagnostic_options *options,
                             struct cyton_diagnostic_result *result);
#endif
