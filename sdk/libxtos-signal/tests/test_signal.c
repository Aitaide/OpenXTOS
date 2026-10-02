#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "xtos/signal.h"

#define XTOS_TEST_PI 3.141592653589793238462643383279502884

static int assertions;

#define CHECK(condition) do { \
    assertions++; \
    if (!(condition)) { \
        fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition); \
        return 1; \
    } \
} while (0)

static int nearly_equal(double actual, double expected, double tolerance)
{
    return fabs(actual - expected) <= tolerance;
}

static int test_timestamp_validation(void)
{
    const uint64_t valid[] = {
        1000000000ULL, 1004000000ULL, 1008000000ULL, 1012000000ULL
    };
    const uint64_t bad_interval[] = {
        1000000000ULL, 1004000000ULL, 1012000000ULL, 1016000000ULL
    };
    const uint64_t bad_order[] = {
        1000000000ULL, 1004000000ULL, 1003000000ULL
    };
    struct xtos_signal_validation result;

    CHECK(xtos_signal_validate_timestamps(valid, 4, 250, 10000, &result) == 0);
    CHECK(result.interval_anomalies == 0);
    CHECK(result.timestamp_anomalies == 0);
    CHECK(result.expected_interval_ns == 4000000ULL);
    CHECK(result.min_interval_ns == 4000000ULL);
    CHECK(result.max_interval_ns == 4000000ULL);

    CHECK(xtos_signal_validate_timestamps(bad_interval, 4, 250, 10000, &result) == 1);
    CHECK(result.interval_anomalies == 1);
    CHECK(result.timestamp_anomalies == 0);

    CHECK(xtos_signal_validate_timestamps(bad_order, 3, 250, 10000, &result) == 1);
    CHECK(result.timestamp_anomalies == 1);
    return 0;
}

static int test_timestamp_correction(void)
{
    uint64_t timestamps[5];
    const uint64_t expected[] = {
        1000000000ULL, 1003906250ULL, 1007812500ULL,
        1011718750ULL, 1015625000ULL
    };
    size_t index;

    CHECK(xtos_signal_correct_timestamps(1000000000ULL, 256, timestamps, 5) == 0);
    for (index = 0; index < 5; index++)
        CHECK(timestamps[index] == expected[index]);
    CHECK(xtos_signal_correct_timestamps(1000000000ULL, 0, timestamps, 5) == -1);
    CHECK(xtos_signal_correct_timestamps(1000000000ULL, 256, timestamps, 0) == -1);
    return 0;
}

static int test_fft_impulse_and_sine(void)
{
    const double impulse[8] = {1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0};
    double sine[8];
    double power[5];
    size_t index;

    CHECK(xtos_signal_fft_power(impulse, 8, power, 5) == 0);
    for (index = 0; index < 5; index++)
        CHECK(nearly_equal(power[index], 1.0, 1e-10));

    for (index = 0; index < 8; index++)
        sine[index] = sin(2.0 * XTOS_TEST_PI * (double)index / 8.0);
    CHECK(xtos_signal_fft_power(sine, 8, power, 5) == 0);
    CHECK(power[0] < 1e-10);
    CHECK(power[1] > 15.9 && power[1] < 16.1);
    CHECK(power[2] < 1e-10);
    CHECK(power[3] < 1e-10);
    CHECK(power[4] < 1e-10);
    CHECK(xtos_signal_fft_power(sine, 7, power, 5) == -1);
    CHECK(xtos_signal_fft_power(sine, 8, power, 4) == -1);
    return 0;
}

static int test_notch_filter(void)
{
    struct xtos_notch_filter filter;
    double input[1000];
    double output[1000];
    double input_energy = 0.0;
    double output_energy = 0.0;
    size_t index;

    CHECK(xtos_notch_init(&filter, 250.0, 50.0, 20.0) == 0);
    for (index = 0; index < 1000; index++) {
        input[index] = sin(2.0 * XTOS_TEST_PI * 50.0 * (double)index / 250.0);
        input_energy += input[index] * input[index];
    }
    CHECK(xtos_notch_process(&filter, input, output, 1000) == 0);
    for (index = 200; index < 1000; index++)
        output_energy += output[index] * output[index];
    CHECK(output_energy < input_energy * 0.02);
    CHECK(xtos_notch_init(&filter, 250.0, 0.0, 20.0) == -1);
    CHECK(xtos_notch_init(&filter, 250.0, 50.0, 0.0) == -1);
    return 0;
}

static double rms_after_settling(const double *values, size_t start, size_t count)
{
    double energy = 0.0;
    size_t index;

    for (index = start; index < count; index++)
        energy += values[index] * values[index];
    return sqrt(energy / (double)(count - start));
}

static int run_bandpass_tone(double frequency_hz, double *rms)
{
    struct xtos_bandpass_filter filter;
    double input[2000];
    double output[2000];
    size_t index;

    CHECK(xtos_bandpass_init(&filter, 250.0, 5.0, 40.0) == 0);
    for (index = 0; index < 2000; index++)
        input[index] = sin(2.0 * XTOS_TEST_PI * frequency_hz *
                            (double)index / 250.0);
    CHECK(xtos_bandpass_process(&filter, input, output, 2000) == 0);
    *rms = rms_after_settling(output, 500, 2000);
    return 0;
}

static int test_bandpass_filter(void)
{
    double low_rms;
    double pass_rms;
    double high_rms;

    CHECK(run_bandpass_tone(1.0, &low_rms) == 0);
    CHECK(run_bandpass_tone(10.0, &pass_rms) == 0);
    CHECK(run_bandpass_tone(80.0, &high_rms) == 0);
    CHECK(pass_rms > 0.30);
    CHECK(low_rms < pass_rms * 0.25);
    CHECK(high_rms < pass_rms * 0.25);

    {
        struct xtos_bandpass_filter filter;
        CHECK(xtos_bandpass_init(&filter, 250.0, 0.0, 40.0) == -1);
        CHECK(xtos_bandpass_init(&filter, 250.0, 5.0, 5.0) == -1);
        CHECK(xtos_bandpass_init(&filter, 250.0, 5.0, 125.0) == -1);
    }
    return 0;
}

static int test_bandpass_in_place(void)
{
    struct xtos_bandpass_filter expected_filter;
    struct xtos_bandpass_filter in_place_filter;
    double expected[64];
    double in_place[64];
    size_t index;

    CHECK(xtos_bandpass_init(&expected_filter, 250.0, 5.0, 40.0) == 0);
    CHECK(xtos_bandpass_init(&in_place_filter, 250.0, 5.0, 40.0) == 0);
    for (index = 0; index < 64; index++) {
        expected[index] = sin(2.0 * XTOS_TEST_PI * 10.0 * (double)index / 250.0);
        in_place[index] = expected[index];
    }
    CHECK(xtos_bandpass_process(&expected_filter, expected, expected, 64) == 0);
    CHECK(xtos_bandpass_process(&in_place_filter, in_place, in_place, 64) == 0);
    for (index = 0; index < 64; index++)
        CHECK(nearly_equal(in_place[index], expected[index], 1e-14));
    return 0;
}

static int test_p300_feature(void)
{
    double input[256] = {0.0};
    struct xtos_p300_feature feature;
    size_t index;

    for (index = 0; index < 32; index++)
        input[index] = (index & 1U) ? 1.0 : -1.0;
    input[128] = 5.0;
    CHECK(xtos_signal_p300_feature(input, 256, 256, 32, 250, 500, 4.0,
                                   &feature) == 0);
    CHECK(nearly_equal(feature.baseline_mean, 0.0, 1e-12));
    CHECK(nearly_equal(feature.baseline_stddev, 1.0, 1e-12));
    CHECK(nearly_equal(feature.peak_amplitude, 5.0, 1e-12));
    CHECK(nearly_equal(feature.peak_latency_ms, 375.0, 1e-12));
    CHECK(nearly_equal(feature.peak_z_score, 5.0, 1e-12));
    CHECK(nearly_equal(feature.window_energy, 25.0, 1e-12));
    CHECK(feature.peak_index == 128);
    CHECK(feature.detected == 1);

    input[128] = 2.0;
    CHECK(xtos_signal_p300_feature(input, 256, 256, 32, 250, 500, 4.0,
                                   &feature) == 0);
    CHECK(feature.detected == 0);
    CHECK(xtos_signal_p300_feature(input, 256, 256, 0, 250, 500, 4.0,
                                   &feature) == -1);
    CHECK(xtos_signal_p300_feature(input, 256, 256, 32, 500, 250, 4.0,
                                   &feature) == -1);
    CHECK(xtos_signal_p300_feature(input, 256, 256, 32, 250, 500, 0.0,
                                   &feature) == -1);
    CHECK(xtos_signal_p300_feature(input, 256, 256, 200, 250, 500, 4.0,
                                   &feature) == -1);
    {
        double flat[256] = {0.0};
        CHECK(xtos_signal_p300_feature(flat, 256, 256, 32, 250, 500, 4.0,
                                       &feature) == -1);
    }
    return 0;
}

static int test_p300_quality_gate(void)
{
    const struct xtos_p300_feature trials[] = {
        {0.0, 1.0, 5.0, 350.0, 5.0, 25.0, 100, 1},
        {0.0, 1.0, 4.5, 360.0, 4.5, 20.25, 100, 1},
        {0.0, 1.0, 4.2, 340.0, 4.2, 17.64, 100, 1},
        {0.0, 1.0, 2.0, 355.0, 2.0, 4.0, 100, 0},
    };
    struct xtos_p300_quality quality;

    CHECK(xtos_signal_p300_quality_gate(trials, 4, 5, 0.75, 3.5, 10.0,
                                        &quality) == 0);
    CHECK(quality.trials_total == 4);
    CHECK(quality.trials_valid == 4);
    CHECK(quality.passed == 0);
    CHECK(xtos_signal_p300_quality_gate(trials, 4, 4, 0.75, 3.5, 10.0,
                                        &quality) == 0);
    CHECK(quality.trials_total == 4);
    CHECK(quality.trials_valid == 4);
    CHECK(quality.trials_detected == 3);
    CHECK(quality.trials_rejected == 0);
    CHECK(nearly_equal(quality.detection_rate, 0.75, 1e-12));
    CHECK(nearly_equal(quality.mean_peak_z_score, 3.925, 1e-12));
    CHECK(nearly_equal(quality.mean_peak_latency_ms, 351.25, 1e-12));
    CHECK(nearly_equal(quality.peak_latency_stddev_ms, 7.39509972887452, 1e-12));
    CHECK(quality.passed == 1);

    CHECK(xtos_signal_p300_quality_gate(trials, 4, 4, 0.80, 3.5, 10.0,
                                        &quality) == 0);
    CHECK(quality.passed == 0);
    CHECK(xtos_signal_p300_quality_gate(trials, 4, 4, 0.75, 3.5, 5.0,
                                        &quality) == 0);
    CHECK(quality.passed == 0);
    {
        struct xtos_p300_feature invalid[4];
        memcpy(invalid, trials, sizeof(invalid));
        invalid[2].peak_z_score = NAN;
        CHECK(xtos_signal_p300_quality_gate(invalid, 4, 3, 0.5, 2.0, 20.0,
                                            &quality) == 0);
        CHECK(quality.trials_valid == 3);
        CHECK(quality.trials_rejected == 1);
        CHECK(quality.passed == 0);
    }
    {
        struct xtos_p300_feature inconsistent[4];
        memcpy(inconsistent, trials, sizeof(inconsistent));
        inconsistent[1].peak_z_score = 3.0;
        inconsistent[2].window_energy = 1.0;
        CHECK(xtos_signal_p300_quality_gate(inconsistent, 4, 2, 0.5, 2.0, 20.0,
                                            &quality) == 0);
        CHECK(quality.trials_valid == 2);
        CHECK(quality.trials_rejected == 2);
        CHECK(quality.passed == 0);
    }
    {
        struct xtos_p300_feature invalid[4];
        memset(invalid, 0, sizeof(invalid));
        CHECK(xtos_signal_p300_quality_gate(invalid, 4, 1, 0.5, 2.0, 20.0,
                                            &quality) == 0);
        CHECK(quality.trials_valid == 0);
        CHECK(quality.trials_rejected == 4);
        CHECK(quality.passed == 0);
    }
    CHECK(xtos_signal_p300_quality_gate(trials, 4, 0, 0.5, 2.0, 20.0,
                                        &quality) == -1);
    CHECK(xtos_signal_p300_quality_gate(trials, 4, 4, 1.1, 2.0, 20.0,
                                        &quality) == -1);
    CHECK(xtos_signal_p300_quality_gate(NULL, 4, 4, 0.5, 2.0, 20.0,
                                        &quality) == -1);
    return 0;
}

static int test_ssvep_feature(void)
{
    double input[256];
    struct xtos_ssvep_feature feature;
    size_t index;

    for (index = 0; index < 256; index++) {
        double phase = 2.0 * XTOS_TEST_PI * (double)index / 256.0;
        input[index] = sin(10.0 * phase) + 0.5 * sin(20.0 * phase) +
                       0.25 * sin(9.0 * phase);
    }
    CHECK(xtos_signal_ssvep_feature(input, 256, 256, 10.0, 2, 1,
                                    &feature) == 0);
    CHECK(feature.target_bin == 10);
    CHECK(feature.harmonics_used == 2);
    CHECK(nearly_equal(feature.signal_power, 20480.0, 1e-6));
    CHECK(nearly_equal(feature.noise_power, 256.0, 1e-6));
    CHECK(nearly_equal(feature.snr, 80.0, 1e-9));
    CHECK(nearly_equal(feature.snr_db, 10.0 * log10(80.0), 1e-9));
    CHECK(xtos_signal_ssvep_feature(input, 255, 256, 10.0, 2, 1,
                                    &feature) == -1);
    CHECK(xtos_signal_ssvep_feature(input, 256, 256, 0.0, 2, 1,
                                    &feature) == -1);
    CHECK(xtos_signal_ssvep_feature(input, 256, 256, 10.0, 0, 1,
                                    &feature) == -1);
    CHECK(xtos_signal_ssvep_feature(input, 256, 256, 100.0, 2, 1,
                                    &feature) == -1);
    return 0;
}

int main(void)
{
    if (test_timestamp_validation() != 0 ||
        test_timestamp_correction() != 0 ||
        test_fft_impulse_and_sine() != 0 ||
        test_notch_filter() != 0 ||
        test_bandpass_filter() != 0 ||
        test_bandpass_in_place() != 0 ||
        test_p300_feature() != 0 ||
        test_p300_quality_gate() != 0 ||
        test_ssvep_feature() != 0)
        return 1;
    printf("PASS assertions=%d\n", assertions);
    return 0;
}
