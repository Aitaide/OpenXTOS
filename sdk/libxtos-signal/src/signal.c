#include <errno.h>
#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

#include "xtos/signal.h"

#define XTOS_PI 3.141592653589793238462643383279502884

static int is_power_of_two(size_t value)
{
    return value != 0 && (value & (value - 1)) == 0;
}

int xtos_signal_validate_timestamps(const uint64_t *timestamps_ns,
                                    size_t count,
                                    uint32_t sample_rate_hz,
                                    uint64_t tolerance_ns,
                                    struct xtos_signal_validation *result)
{
    uint64_t expected_interval;
    size_t index;
    int failed = 0;

    if (timestamps_ns == NULL || result == NULL || count == 0 ||
        sample_rate_hz == 0) {
        errno = EINVAL;
        return -1;
    }
    expected_interval = (uint64_t)llround(1000000000.0 / sample_rate_hz);
    result->expected_interval_ns = expected_interval;
    result->min_interval_ns = UINT64_MAX;
    result->max_interval_ns = 0;
    result->interval_anomalies = 0;
    result->timestamp_anomalies = 0;
    for (index = 1; index < count; index++) {
        uint64_t interval;
        uint64_t difference;
        if (timestamps_ns[index] <= timestamps_ns[index - 1]) {
            result->timestamp_anomalies++;
            failed = 1;
            continue;
        }
        interval = timestamps_ns[index] - timestamps_ns[index - 1];
        if (interval < result->min_interval_ns)
            result->min_interval_ns = interval;
        if (interval > result->max_interval_ns)
            result->max_interval_ns = interval;
        difference = interval > expected_interval ?
                     interval - expected_interval : expected_interval - interval;
        if (difference > tolerance_ns) {
            result->interval_anomalies++;
            failed = 1;
        }
    }
    if (count == 1) {
        result->min_interval_ns = 0;
        result->max_interval_ns = 0;
    }
    return failed;
}

int xtos_signal_correct_timestamps(uint64_t start_timestamp_ns,
                                   uint32_t sample_rate_hz,
                                   uint64_t *timestamps_ns,
                                   size_t count)
{
    size_t index;
    uint64_t interval;

    if (timestamps_ns == NULL || count == 0 || sample_rate_hz == 0) {
        errno = EINVAL;
        return -1;
    }
    interval = (uint64_t)llround(1000000000.0 / sample_rate_hz);
    for (index = 0; index < count; index++)
        timestamps_ns[index] = start_timestamp_ns + interval * index;
    return 0;
}

static void fft(double *real, double *imag, size_t n)
{
    size_t i;
    size_t j = 0;

    for (i = 1; i < n; i++) {
        size_t bit = n >> 1;
        while (j & bit) {
            j ^= bit;
            bit >>= 1;
        }
        j ^= bit;
        if (i < j) {
            double value = real[i];
            real[i] = real[j];
            real[j] = value;
            value = imag[i];
            imag[i] = imag[j];
            imag[j] = value;
        }
    }
    for (size_t length = 2; length <= n; length <<= 1) {
        double angle = -2.0 * XTOS_PI / (double)length;
        double step_real = cos(angle);
        double step_imag = sin(angle);
        for (size_t offset = 0; offset < n; offset += length) {
            double twiddle_real = 1.0;
            double twiddle_imag = 0.0;
            size_t half = length >> 1;
            for (size_t k = 0; k < half; k++) {
                size_t even = offset + k;
                size_t odd = even + half;
                double odd_real = real[odd] * twiddle_real - imag[odd] * twiddle_imag;
                double odd_imag = real[odd] * twiddle_imag + imag[odd] * twiddle_real;
                double even_real = real[even];
                double even_imag = imag[even];
                real[even] = even_real + odd_real;
                imag[even] = even_imag + odd_imag;
                real[odd] = even_real - odd_real;
                imag[odd] = even_imag - odd_imag;
                {
                    double value = twiddle_real * step_real - twiddle_imag * step_imag;
                    twiddle_imag = twiddle_real * step_imag + twiddle_imag * step_real;
                    twiddle_real = value;
                }
            }
        }
    }
}

int xtos_signal_fft_power(const double *input,
                          size_t n,
                          double *power,
                          size_t power_count)
{
    double *real;
    double *imag;
    size_t index;
    size_t expected_power_count;

    if (input == NULL || power == NULL || !is_power_of_two(n)) {
        errno = EINVAL;
        return -1;
    }
    expected_power_count = n / 2 + 1;
    if (power_count < expected_power_count) {
        errno = EINVAL;
        return -1;
    }
    real = calloc(n, sizeof(*real));
    imag = calloc(n, sizeof(*imag));
    if (real == NULL || imag == NULL) {
        free(real);
        free(imag);
        errno = ENOMEM;
        return -1;
    }
    for (index = 0; index < n; index++)
        real[index] = input[index];
    fft(real, imag, n);
    for (index = 0; index < expected_power_count; index++)
        power[index] = real[index] * real[index] + imag[index] * imag[index];
    free(real);
    free(imag);
    return 0;
}

int xtos_notch_init(struct xtos_notch_filter *filter,
                    double sample_rate_hz,
                    double notch_frequency_hz,
                    double quality_factor)
{
    double omega;
    double alpha;
    double cosine;
    double normalization;

    if (filter == NULL || sample_rate_hz <= 0.0 ||
        notch_frequency_hz <= 0.0 || notch_frequency_hz >= sample_rate_hz / 2.0 ||
        quality_factor <= 0.0) {
        errno = EINVAL;
        return -1;
    }
    omega = 2.0 * XTOS_PI * notch_frequency_hz / sample_rate_hz;
    alpha = sin(omega) / (2.0 * quality_factor);
    cosine = cos(omega);
    normalization = 1.0 + alpha;
    filter->b0 = 1.0 / normalization;
    filter->b1 = -2.0 * cosine / normalization;
    filter->b2 = 1.0 / normalization;
    filter->a1 = -2.0 * cosine / normalization;
    filter->a2 = (1.0 - alpha) / normalization;
    filter->x1 = 0.0;
    filter->x2 = 0.0;
    filter->y1 = 0.0;
    filter->y2 = 0.0;
    return 0;
}

int xtos_notch_process(struct xtos_notch_filter *filter,
                       const double *input,
                       double *output,
                       size_t count)
{
    size_t index;

    if (filter == NULL || input == NULL || output == NULL) {
        errno = EINVAL;
        return -1;
    }
    for (index = 0; index < count; index++) {
        double input_value = input[index];
        double value = filter->b0 * input_value + filter->b1 * filter->x1 +
                       filter->b2 * filter->x2 - filter->a1 * filter->y1 -
                       filter->a2 * filter->y2;
        output[index] = value;
        filter->x2 = filter->x1;
        filter->x1 = input_value;
        filter->y2 = filter->y1;
        filter->y1 = value;
    }
    return 0;
}

static int init_biquad(struct xtos_notch_filter *filter,
                       double sample_rate_hz,
                       double cutoff_hz,
                       int highpass)
{
    const double quality_factor = 0.70710678118654752440;
    double omega;
    double sine;
    double cosine;
    double alpha;
    double normalization;

    omega = 2.0 * XTOS_PI * cutoff_hz / sample_rate_hz;
    sine = sin(omega);
    cosine = cos(omega);
    alpha = sine / (2.0 * quality_factor);
    normalization = 1.0 + alpha;
    if (highpass) {
        filter->b0 = (1.0 + cosine) / (2.0 * normalization);
        filter->b1 = -(1.0 + cosine) / normalization;
        filter->b2 = (1.0 + cosine) / (2.0 * normalization);
    } else {
        filter->b0 = (1.0 - cosine) / (2.0 * normalization);
        filter->b1 = (1.0 - cosine) / normalization;
        filter->b2 = (1.0 - cosine) / (2.0 * normalization);
    }
    filter->a1 = -2.0 * cosine / normalization;
    filter->a2 = (1.0 - alpha) / normalization;
    filter->x1 = 0.0;
    filter->x2 = 0.0;
    filter->y1 = 0.0;
    filter->y2 = 0.0;
    return 0;
}

int xtos_bandpass_init(struct xtos_bandpass_filter *filter,
                       double sample_rate_hz,
                       double low_cut_hz,
                       double high_cut_hz)
{
    if (filter == NULL || sample_rate_hz <= 0.0 || low_cut_hz <= 0.0 ||
        low_cut_hz >= high_cut_hz || high_cut_hz >= sample_rate_hz / 2.0) {
        errno = EINVAL;
        return -1;
    }
    init_biquad(&filter->highpass, sample_rate_hz, low_cut_hz, 1);
    init_biquad(&filter->lowpass, sample_rate_hz, high_cut_hz, 0);
    return 0;
}

int xtos_bandpass_process(struct xtos_bandpass_filter *filter,
                          const double *input,
                          double *output,
                          size_t count)
{
    if (filter == NULL || input == NULL || output == NULL) {
        errno = EINVAL;
        return -1;
    }
    xtos_notch_process(&filter->highpass, input, output, count);
    xtos_notch_process(&filter->lowpass, output, output, count);
    return 0;
}

static int is_ssvep_target_bin(size_t candidate,
                               size_t target_bin,
                               size_t harmonic_count)
{
    size_t harmonic;

    for (harmonic = 1; harmonic <= harmonic_count; harmonic++) {
        if (target_bin > SIZE_MAX / harmonic)
            return 0;
        if (candidate == target_bin * harmonic)
            return 1;
    }
    return 0;
}

int xtos_signal_p300_feature(const double *input,
                             size_t n,
                             uint32_t sample_rate_hz,
                             size_t baseline_samples,
                             uint32_t window_start_ms,
                             uint32_t window_end_ms,
                             double detection_z_threshold,
                             struct xtos_p300_feature *result)
{
    double baseline_sum = 0.0;
    double baseline_mean;
    double variance_sum = 0.0;
    double peak_amplitude;
    double peak_z_score;
    double window_energy = 0.0;
    uint64_t window_start_offset;
    uint64_t window_end_offset;
    uint64_t window_start;
    uint64_t window_end;
    size_t index;
    size_t peak_index;

    if (input == NULL || result == NULL || sample_rate_hz == 0 ||
        baseline_samples < 2 || baseline_samples >= n ||
        window_start_ms >= window_end_ms || detection_z_threshold <= 0.0 ||
        !isfinite(detection_z_threshold)) {
        errno = EINVAL;
        return -1;
    }
    window_start_offset = ((uint64_t)window_start_ms * sample_rate_hz + 500U) / 1000U;
    window_end_offset = ((uint64_t)window_end_ms * sample_rate_hz + 500U) / 1000U;
    if (window_start_offset >= window_end_offset ||
        window_start_offset > SIZE_MAX - baseline_samples ||
        window_end_offset > SIZE_MAX - baseline_samples) {
        errno = EINVAL;
        return -1;
    }
    window_start = baseline_samples + window_start_offset;
    window_end = baseline_samples + window_end_offset;
    if (window_start >= window_end || window_end > n) {
        errno = EINVAL;
        return -1;
    }
    for (index = 0; index < baseline_samples; index++) {
        if (!isfinite(input[index])) {
            errno = EINVAL;
            return -1;
        }
        baseline_sum += input[index];
    }
    baseline_mean = baseline_sum / (double)baseline_samples;
    for (index = 0; index < baseline_samples; index++) {
        double centered = input[index] - baseline_mean;
        variance_sum += centered * centered;
    }
    result->baseline_mean = baseline_mean;
    result->baseline_stddev = sqrt(variance_sum / (double)baseline_samples);
    if (!(result->baseline_stddev > 0.0) || !isfinite(result->baseline_stddev)) {
        errno = EDOM;
        return -1;
    }
    peak_index = (size_t)window_start;
    peak_amplitude = input[peak_index] - baseline_mean;
    for (index = (size_t)window_start; index < (size_t)window_end; index++) {
        double centered;
        if (!isfinite(input[index])) {
            errno = EINVAL;
            return -1;
        }
        centered = input[index] - baseline_mean;
        window_energy += centered * centered;
        if (centered > peak_amplitude) {
            peak_amplitude = centered;
            peak_index = index;
        }
    }
    peak_z_score = peak_amplitude / result->baseline_stddev;
    result->peak_amplitude = peak_amplitude;
    result->peak_latency_ms = 1000.0 * (double)(peak_index - baseline_samples) /
                              (double)sample_rate_hz;
    result->peak_z_score = peak_z_score;
    result->window_energy = window_energy;
    result->peak_index = peak_index;
    result->detected = peak_z_score >= detection_z_threshold ? 1 : 0;
    return 0;
}

static int p300_feature_is_valid(const struct xtos_p300_feature *trial)
{
    double expected_z_score;
    double z_tolerance;

    if (!isfinite(trial->baseline_mean) ||
        !isfinite(trial->baseline_stddev) || trial->baseline_stddev <= 0.0 ||
        !isfinite(trial->peak_amplitude) ||
        !isfinite(trial->peak_latency_ms) || trial->peak_latency_ms < 0.0 ||
        !isfinite(trial->peak_z_score) ||
        !isfinite(trial->window_energy) || trial->window_energy < 0.0 ||
        (trial->detected != 0 && trial->detected != 1))
        return 0;
    expected_z_score = trial->peak_amplitude / trial->baseline_stddev;
    z_tolerance = 1e-9 * fmax(1.0, fabs(expected_z_score));
    if (fabs(trial->peak_z_score - expected_z_score) > z_tolerance)
        return 0;
    if (trial->window_energy + 1e-12 <
        trial->peak_amplitude * trial->peak_amplitude)
        return 0;
    return 1;
}

int xtos_signal_p300_quality_gate(const struct xtos_p300_feature *trials,
                                  size_t trial_count,
                                  size_t min_valid_trials,
                                  double min_detection_rate,
                                  double min_mean_z_score,
                                  double max_latency_stddev_ms,
                                  struct xtos_p300_quality *result)
{
    double z_sum = 0.0;
    double latency_sum = 0.0;
    double latency_mean;
    double latency_variance = 0.0;
    size_t index;

    if (trials == NULL || result == NULL || trial_count == 0 ||
        min_valid_trials == 0 ||
        min_detection_rate < 0.0 || min_detection_rate > 1.0 ||
        !isfinite(min_detection_rate) || !isfinite(min_mean_z_score) ||
        !isfinite(max_latency_stddev_ms) || max_latency_stddev_ms < 0.0) {
        errno = EINVAL;
        return -1;
    }
    result->trials_total = trial_count;
    result->trials_valid = 0;
    result->trials_detected = 0;
    result->trials_rejected = 0;
    result->detection_rate = 0.0;
    result->mean_peak_z_score = 0.0;
    result->mean_peak_latency_ms = 0.0;
    result->peak_latency_stddev_ms = 0.0;
    result->passed = 0;
    for (index = 0; index < trial_count; index++) {
        const struct xtos_p300_feature *trial = &trials[index];
        if (!p300_feature_is_valid(trial)) {
            result->trials_rejected++;
            continue;
        }
        result->trials_valid++;
        if (trial->detected)
            result->trials_detected++;
        z_sum += trial->peak_z_score;
        latency_sum += trial->peak_latency_ms;
    }
    if (result->trials_valid == 0) {
        return 0;
    }
    result->detection_rate = (double)result->trials_detected /
                             (double)result->trials_valid;
    result->mean_peak_z_score = z_sum / (double)result->trials_valid;
    result->mean_peak_latency_ms = latency_sum / (double)result->trials_valid;
    latency_mean = result->mean_peak_latency_ms;
    for (index = 0; index < trial_count; index++) {
        const struct xtos_p300_feature *trial = &trials[index];
        if (!p300_feature_is_valid(trial))
            continue;
        {
            double difference = trial->peak_latency_ms - latency_mean;
            latency_variance += difference * difference;
        }
    }
    result->peak_latency_stddev_ms = sqrt(latency_variance /
                                          (double)result->trials_valid);
    result->passed = result->trials_rejected == 0 &&
                     result->trials_valid >= min_valid_trials &&
                     result->detection_rate >= min_detection_rate &&
                     result->mean_peak_z_score >= min_mean_z_score &&
                     result->peak_latency_stddev_ms <= max_latency_stddev_ms;
    return 0;
}

int xtos_signal_ssvep_feature(const double *input,
                              size_t n,
                              uint32_t sample_rate_hz,
                              double target_frequency_hz,
                              size_t harmonic_count,
                              size_t neighbor_bins,
                              struct xtos_ssvep_feature *result)
{
    double *power;
    size_t power_count;
    size_t target_bin;
    size_t harmonic;
    size_t noise_count = 0;
    double signal_power = 0.0;
    double noise_power = 0.0;

    if (input == NULL || result == NULL || sample_rate_hz == 0 ||
        target_frequency_hz <= 0.0 ||
        target_frequency_hz >= (double)sample_rate_hz / 2.0 ||
        harmonic_count == 0 || neighbor_bins == 0 || !is_power_of_two(n)) {
        errno = EINVAL;
        return -1;
    }
    power_count = n / 2 + 1;
    target_bin = (size_t)llround(target_frequency_hz * (double)n /
                                  (double)sample_rate_hz);
    if (target_bin == 0 || target_bin >= power_count) {
        errno = EINVAL;
        return -1;
    }
    power = calloc(power_count, sizeof(*power));
    if (power == NULL) {
        errno = ENOMEM;
        return -1;
    }
    if (xtos_signal_fft_power(input, n, power, power_count) != 0) {
        free(power);
        return -1;
    }
    for (harmonic = 1; harmonic <= harmonic_count; harmonic++) {
        size_t bin;
        if (target_bin > (power_count - 1) / harmonic) {
            free(power);
            errno = EINVAL;
            return -1;
        }
        bin = target_bin * harmonic;
        signal_power += power[bin];
    }
    for (harmonic = 1; harmonic <= harmonic_count; harmonic++) {
        size_t center = target_bin * harmonic;
        size_t offset;
        for (offset = 1; offset <= neighbor_bins; offset++) {
            size_t lower = center >= offset ? center - offset : 0;
            size_t upper = center + offset;
            if (center >= offset && lower != 0 && lower < power_count &&
                !is_ssvep_target_bin(lower, target_bin, harmonic_count)) {
                noise_power += power[lower];
                noise_count++;
            }
            if (upper > center && upper < power_count &&
                !is_ssvep_target_bin(upper, target_bin, harmonic_count)) {
                noise_power += power[upper];
                noise_count++;
            }
        }
    }
    free(power);
    if (noise_count == 0 || noise_power <= 0.0) {
        errno = EDOM;
        return -1;
    }
    noise_power /= (double)noise_count;
    result->signal_power = signal_power;
    result->noise_power = noise_power;
    result->snr = signal_power / noise_power;
    result->snr_db = 10.0 * log10(result->snr);
    result->target_bin = target_bin;
    result->harmonics_used = harmonic_count;
    return 0;
}
