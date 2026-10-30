#include "bci_diagnostic.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

static unsigned int cases;
static struct bci_diagnostic_options shorted(void)
{
    const struct bci_diagnostic_options o = {
        BCI_DIAGNOSTIC_SHORTED, BCI_DIAGNOSTIC_POPULATION_STATS,
        BCI_DIAGNOSTIC_SOURCE_NONE, 0, 0, 0
    };
    return o;
}

static void test_eight_channel_dc(void)
{
    struct bci_diagnostic_frame_nv frames[32];
    struct bci_diagnostic_result out;
    struct bci_diagnostic_options options = shorted();
    size_t i, c;
    for (i = 0; i < 32; ++i)
        for (c = 0; c < 8; ++c)
            frames[i].channel_nv[c] = (int32_t)c * 100 - 300;
    assert(bci_diagnostic_analyze(frames, 32, &options, &out) == 0);
    for (c = 0; c < 8; ++c) {
        assert(out.channels[c].count == 32);
        assert(out.channels[c].mean_nv == (double)c * 100 - 300);
        assert(out.channels[c].stddev_nv == 0);
        assert(out.channels[c].observed_peak_nv == 0);
        assert(out.channels[c].estimate_present == 0);
        assert(out.channels[c].nominal_rms_estimate_ohm == 0);
        assert(out.channels[c].unclamped_estimate_ohm == 0);
    }
    ++cases;
}

static void test_sine_population_dc_removed(void)
{
    static const int32_t wave[8] = {0,7071,10000,7071,0,-7071,-10000,-7071};
    struct bci_diagnostic_frame_nv frames[32];
    struct bci_diagnostic_result out;
    struct bci_diagnostic_options options = shorted();
    size_t i, c;
    for (i = 0; i < 32; ++i)
        for (c = 0; c < 8; ++c)
            frames[i].channel_nv[c] = wave[i % 8] + (int32_t)c * 100000;
    assert(bci_diagnostic_analyze(frames, 32, &options, &out) == 0);
    for (c = 0; c < 8; ++c) {
        assert(fabs(out.channels[c].mean_nv - (double)c * 100000) < 1e-8);
        assert(fabs(out.channels[c].stddev_nv - sqrt(49999520.5)) < 1e-7);
        assert(fabs(out.channels[c].observed_peak_nv - 10000) < 1e-8);
    }
    ++cases;
}

static struct bci_diagnostic_options impedance(unsigned int channel)
{
    const struct bci_diagnostic_options o = {
        BCI_DIAGNOSTIC_NOMINAL_RMS_IMPEDANCE,
        BCI_DIAGNOSTIC_NOMINAL_CURRENT_RMS,
        BCI_DIAGNOSTIC_SOURCE_CYTON_NOMINAL_6NA, channel, 6, 2200
    };
    return o;
}

static void test_selected_channel_nominal_rms(void)
{
    static const int32_t wave[8] = {0,7071,10000,7071,0,-7071,-10000,-7071};
    struct bci_diagnostic_frame_nv frames[32];
    struct bci_diagnostic_result out;
    unsigned int selected;
    size_t i, c;
    for (i = 0; i < 32; ++i)
        for (c = 0; c < 8; ++c)
            frames[i].channel_nv[c] = 3 * wave[i % 8] + 123456;
    for (selected = 0; selected < 8; ++selected) {
        struct bci_diagnostic_options options = impedance(selected);
        const double estimate = sqrt(2.0) * 3.0 * sqrt(49999520.5) / 6.0 - 2200.0;
        assert(bci_diagnostic_analyze(frames, 32, &options, &out) == 0);
        assert(out.assumptions.mode == options.mode);
        assert(out.assumptions.method == options.method);
        assert(out.assumptions.source_assumption == options.source_assumption);
        assert(out.assumptions.selected_channel == selected);
        assert(out.assumptions.nominal_current_na == 6);
        assert(out.assumptions.series_resistance_ohm == 2200);
        for (c = 0; c < 8; ++c) {
            assert(out.channels[c].estimate_present == (c == selected));
            assert(fabs(out.channels[c].nominal_rms_estimate_ohm -
                        (c == selected ? estimate : 0)) < 1e-7);
            assert(fabs(out.channels[c].unclamped_estimate_ohm -
                        (c == selected ? estimate : 0)) < 1e-7);
        }
        ++cases;
    }
}

static void test_zero_and_low_signal_clamp(void)
{
    struct bci_diagnostic_frame_nv frames[32] = {{{0}}};
    struct bci_diagnostic_options options = impedance(7);
    struct bci_diagnostic_result out;
    size_t i;
    assert(bci_diagnostic_analyze(frames, 32, &options, &out) == 0);
    assert(out.channels[7].estimate_present == 1);
    assert(out.channels[7].unclamped_estimate_ohm == -2200);
    assert(out.channels[7].nominal_rms_estimate_ohm == 0);
    ++cases;
    for (i = 0; i < 32; ++i)
        frames[i].channel_nv[7] = 7654321 + (i % 2 ? 100 : -100);
    assert(bci_diagnostic_analyze(frames, 32, &options, &out) == 0);
    assert(out.channels[7].mean_nv == 7654321);
    assert(out.channels[7].stddev_nv == 100);
    assert(out.channels[7].unclamped_estimate_ohm < 0);
    assert(out.channels[7].nominal_rms_estimate_ohm == 0);
    ++cases;
}

static void test_int32_extremes_maximum_count(void)
{
    struct bci_diagnostic_frame_nv frames[BCI_DIAGNOSTIC_MAX_FRAMES];
    struct bci_diagnostic_result out;
    struct bci_diagnostic_options options = shorted();
    size_t i, c;
    for (i = 0; i < BCI_DIAGNOSTIC_MAX_FRAMES; ++i)
        for (c = 0; c < 8; ++c)
            frames[i].channel_nv[c] = i % 2 ? INT32_MAX : INT32_MIN;
    assert(bci_diagnostic_analyze(frames, BCI_DIAGNOSTIC_MAX_FRAMES, &options, &out) == 0);
    for (c = 0; c < 8; ++c) {
        assert(out.channels[c].count == BCI_DIAGNOSTIC_MAX_FRAMES);
        assert(fabs(out.channels[c].mean_nv + 0.5) < 1e-6);
        assert(fabs(out.channels[c].stddev_nv - 2147483647.5) < 1e-6);
        assert(fabs(out.channels[c].observed_peak_nv - 2147483647.5) < 1e-6);
    }
    ++cases;
    for (i = 0; i < 32; ++i)
        for (c = 0; c < 8; ++c)
            frames[i].channel_nv[c] = INT32_MAX - (int32_t)(i % 2);
    assert(bci_diagnostic_analyze(frames, 32, &options, &out) == 0);
    assert(out.channels[0].mean_nv == 2147483646.5);
    assert(fabs(out.channels[0].stddev_nv - 0.5) < 1e-8);
    ++cases;
}

static void rejects(const struct bci_diagnostic_frame_nv *frames, size_t n,
                    const struct bci_diagnostic_options *options)
{
    struct bci_diagnostic_result out, before;
    memset(&out, 0xA5, sizeof(out));
    memcpy(&before, &out, sizeof(out));
    assert(bci_diagnostic_analyze(frames, n, options, &out) == BCI_DIAGNOSTIC_INVALID);
    assert(memcmp(&before, &out, sizeof(out)) == 0);
    ++cases;
}

static void test_rejection_and_input_preservation(void)
{
    struct bci_diagnostic_frame_nv frames[32] = {{{0}}}, before[32];
    struct bci_diagnostic_options options = shorted(), saved;
    struct bci_diagnostic_result out;
    static const size_t counts[] = {0, 1, 31, 4097, SIZE_MAX};
    size_t i;
    memcpy(before, frames, sizeof(frames));
    saved = options;
    assert(bci_diagnostic_analyze(frames, 32, &options, &out) == 0);
    assert(memcmp(frames, before, sizeof(frames)) == 0);
    assert(memcmp(&options, &saved, sizeof(options)) == 0);
    ++cases;
    for (i = 0; i < sizeof(counts) / sizeof(counts[0]); ++i)
        rejects(frames, counts[i], &options);
    rejects(NULL, 32, &options);
    rejects(frames, 32, NULL);
    assert(bci_diagnostic_analyze(frames, 32, &options, NULL) == BCI_DIAGNOSTIC_INVALID);
    ++cases;
    options.mode = (enum bci_diagnostic_mode)0;
    rejects(frames, 32, &options);
    options.mode = (enum bci_diagnostic_mode)99;
    rejects(frames, 32, &options);
    options = shorted(); options.method = BCI_DIAGNOSTIC_NOMINAL_CURRENT_RMS;
    rejects(frames, 32, &options);
    options = shorted(); options.source_assumption = BCI_DIAGNOSTIC_SOURCE_CYTON_NOMINAL_6NA;
    rejects(frames, 32, &options);
    options = shorted(); options.selected_channel = 1;
    rejects(frames, 32, &options);
    options = shorted(); options.nominal_current_na = 6;
    rejects(frames, 32, &options);
    options = shorted(); options.series_resistance_ohm = 2200;
    rejects(frames, 32, &options);
    options = impedance(0); options.method = BCI_DIAGNOSTIC_POPULATION_STATS;
    rejects(frames, 32, &options);
    options = impedance(0); options.method = (enum bci_diagnostic_method)99;
    rejects(frames, 32, &options);
    options = impedance(0); options.source_assumption = BCI_DIAGNOSTIC_SOURCE_NONE;
    rejects(frames, 32, &options);
    options = impedance(0); options.source_assumption = (enum bci_diagnostic_source_assumption)99;
    rejects(frames, 32, &options);
    options = impedance(8);
    rejects(frames, 32, &options);
    options = impedance(0); options.nominal_current_na = 0;
    rejects(frames, 32, &options);
    options = impedance(0); options.nominal_current_na = 24;
    rejects(frames, 32, &options);
    options = impedance(0); options.series_resistance_ohm = 0;
    rejects(frames, 32, &options);
    options = impedance(0); options.series_resistance_ohm = 2201;
    rejects(frames, 32, &options);
}

int main(void)
{
    test_eight_channel_dc();
    test_sine_population_dc_removed();
    test_selected_channel_nominal_rms();
    test_zero_and_low_signal_clamp();
    test_int32_extremes_maximum_count();
    test_rejection_and_input_preservation();
    printf("bci_diagnostic: %u cases passed\n", cases);
    return 0;
}
