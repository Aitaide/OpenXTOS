#ifndef XTOS_SIGNAL_H
#define XTOS_SIGNAL_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

struct xtos_signal_validation {
    uint64_t expected_interval_ns;
    uint64_t min_interval_ns;
    uint64_t max_interval_ns;
    size_t interval_anomalies;
    size_t timestamp_anomalies;
};

int xtos_signal_validate_timestamps(const uint64_t *timestamps_ns,
                                    size_t count,
                                    uint32_t sample_rate_hz,
                                    uint64_t tolerance_ns,
                                    struct xtos_signal_validation *result);

int xtos_signal_correct_timestamps(uint64_t start_timestamp_ns,
                                   uint32_t sample_rate_hz,
                                   uint64_t *timestamps_ns,
                                   size_t count);

/* Returns one-sided squared DFT magnitude for bins 0..n/2. */
int xtos_signal_fft_power(const double *input,
                          size_t n,
                          double *power,
                          size_t power_count);

struct xtos_notch_filter {
    double b0;
    double b1;
    double b2;
    double a1;
    double a2;
    double x1;
    double x2;
    double y1;
    double y2;
};

int xtos_notch_init(struct xtos_notch_filter *filter,
                    double sample_rate_hz,
                    double notch_frequency_hz,
                    double quality_factor);

int xtos_notch_process(struct xtos_notch_filter *filter,
                       const double *input,
                       double *output,
                       size_t count);

/* A fourth-order band-pass baseline: second-order high-pass then low-pass. */
struct xtos_bandpass_filter {
    struct xtos_notch_filter highpass;
    struct xtos_notch_filter lowpass;
};

int xtos_bandpass_init(struct xtos_bandpass_filter *filter,
                       double sample_rate_hz,
                       double low_cut_hz,
                       double high_cut_hz);

int xtos_bandpass_process(struct xtos_bandpass_filter *filter,
                          const double *input,
                          double *output,
                          size_t count);

struct xtos_ssvep_feature {
    double signal_power;
    double noise_power;
    double snr;
    double snr_db;
    size_t target_bin;
    size_t harmonics_used;
};

int xtos_signal_ssvep_feature(const double *input,
                              size_t n,
                              uint32_t sample_rate_hz,
                              double target_frequency_hz,
                              size_t harmonic_count,
                              size_t neighbor_bins,
                              struct xtos_ssvep_feature *result);

/*
 * Research P300 ERP baseline: baseline_samples precede the stimulus, and
 * the detection window is specified in milliseconds after that stimulus.
 * The returned latency is relative to the stimulus; input remains unchanged.
 */
struct xtos_p300_feature {
    double baseline_mean;
    double baseline_stddev;
    double peak_amplitude;
    double peak_latency_ms;
    double peak_z_score;
    double window_energy;
    size_t peak_index;
    int detected;
};

int xtos_signal_p300_feature(const double *input,
                             size_t n,
                             uint32_t sample_rate_hz,
                             size_t baseline_samples,
                             uint32_t window_start_ms,
                             uint32_t window_end_ms,
                             double detection_z_threshold,
                             struct xtos_p300_feature *result);

struct xtos_p300_quality {
    size_t trials_total;
    size_t trials_valid;
    size_t trials_detected;
    size_t trials_rejected;
    double detection_rate;
    double mean_peak_z_score;
    double mean_peak_latency_ms;
    double peak_latency_stddev_ms;
    int passed;
};

int xtos_signal_p300_quality_gate(const struct xtos_p300_feature *trials,
                                  size_t trial_count,
                                  size_t min_valid_trials,
                                  double min_detection_rate,
                                  double min_mean_z_score,
                                  double max_latency_stddev_ms,
                                  struct xtos_p300_quality *result);

#ifdef __cplusplus
}
#endif

#endif
