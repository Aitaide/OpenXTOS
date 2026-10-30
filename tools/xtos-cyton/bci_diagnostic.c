#include "bci_diagnostic.h"

#include <math.h>
#include <string.h>

int bci_diagnostic_analyze(const struct bci_diagnostic_frame_nv *frames,
                           size_t nframes,
                           const struct bci_diagnostic_options *options,
                           struct bci_diagnostic_result *out)
{
    struct bci_diagnostic_result result = {0};
    size_t c;
    if (frames == NULL || options == NULL || out == NULL ||
        nframes < BCI_DIAGNOSTIC_MIN_FRAMES ||
        nframes > BCI_DIAGNOSTIC_MAX_FRAMES)
        return BCI_DIAGNOSTIC_INVALID;
    if (options->mode == BCI_DIAGNOSTIC_SHORTED) {
        if (options->method != BCI_DIAGNOSTIC_POPULATION_STATS ||
            options->source_assumption != BCI_DIAGNOSTIC_SOURCE_NONE ||
            options->selected_channel != 0 || options->nominal_current_na != 0 ||
            options->series_resistance_ohm != 0)
            return BCI_DIAGNOSTIC_INVALID;
    } else if (options->mode == BCI_DIAGNOSTIC_NOMINAL_RMS_IMPEDANCE) {
        if (options->method != BCI_DIAGNOSTIC_NOMINAL_CURRENT_RMS ||
            options->source_assumption != BCI_DIAGNOSTIC_SOURCE_CYTON_NOMINAL_6NA ||
            options->selected_channel >= BCI_DIAGNOSTIC_CHANNELS ||
            options->nominal_current_na != BCI_DIAGNOSTIC_NOMINAL_CURRENT_NA ||
            options->series_resistance_ohm != BCI_DIAGNOSTIC_SERIES_RESISTANCE_OHM)
            return BCI_DIAGNOSTIC_INVALID;
    } else {
        return BCI_DIAGNOSTIC_INVALID;
    }
    result.assumptions = *options;
    for (c = 0; c < BCI_DIAGNOSTIC_CHANNELS; ++c) {
        long double mean = 0.0L, m2 = 0.0L, peak = 0.0L;
        size_t i;
        for (i = 0; i < nframes; ++i) {
            const long double x = (long double)frames[i].channel_nv[c];
            const long double delta = x - mean;
            mean += delta / (long double)(i + 1);
            m2 += delta * (x - mean);
        }
        for (i = 0; i < nframes; ++i) {
            const long double deviation = fabsl((long double)frames[i].channel_nv[c] - mean);
            if (deviation > peak)
                peak = deviation;
        }
        result.channels[c].count = nframes;
        result.channels[c].mean_nv = (double)mean;
        result.channels[c].stddev_nv = (double)sqrtl(m2 / (long double)nframes);
        result.channels[c].observed_peak_nv = (double)peak;
    }
    if (options->mode == BCI_DIAGNOSTIC_NOMINAL_RMS_IMPEDANCE) {
        struct bci_diagnostic_channel_result *channel =
            &result.channels[options->selected_channel];
        channel->unclamped_estimate_ohm = (double)(sqrtl(2.0L) *
            (long double)channel->stddev_nv /
            (long double)options->nominal_current_na -
            (long double)options->series_resistance_ohm);
        channel->nominal_rms_estimate_ohm =
            fmax(0.0, channel->unclamped_estimate_ohm);
        channel->estimate_present = 1;
    }
    memcpy(out, &result, sizeof(result));
    return BCI_DIAGNOSTIC_OK;
}
