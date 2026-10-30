#ifndef XTOS_BCI_DIAGNOSTIC_H
#define XTOS_BCI_DIAGNOSTIC_H

#include <stddef.h>
#include <stdint.h>

#define BCI_DIAGNOSTIC_CHANNELS 8u
#define BCI_DIAGNOSTIC_MIN_FRAMES 32u
#define BCI_DIAGNOSTIC_MAX_FRAMES 4096u
#define BCI_DIAGNOSTIC_NOMINAL_CURRENT_NA 6u
#define BCI_DIAGNOSTIC_SERIES_RESISTANCE_OHM 2200u

enum bci_diagnostic_mode {
    BCI_DIAGNOSTIC_SHORTED = 1,
    BCI_DIAGNOSTIC_NOMINAL_RMS_IMPEDANCE = 2
};
enum bci_diagnostic_method {
    BCI_DIAGNOSTIC_POPULATION_STATS = 1,
    BCI_DIAGNOSTIC_NOMINAL_CURRENT_RMS = 2
};
enum bci_diagnostic_source_assumption {
    BCI_DIAGNOSTIC_SOURCE_NONE = 0,
    BCI_DIAGNOSTIC_SOURCE_CYTON_NOMINAL_6NA = 1
};
enum bci_diagnostic_status {
    BCI_DIAGNOSTIC_OK = 0,
    BCI_DIAGNOSTIC_INVALID = -1
};

struct bci_diagnostic_frame_nv {
    int32_t channel_nv[BCI_DIAGNOSTIC_CHANNELS];
};
struct bci_diagnostic_options {
    enum bci_diagnostic_mode mode;
    enum bci_diagnostic_method method;
    enum bci_diagnostic_source_assumption source_assumption;
    unsigned int selected_channel; /* Zero-based; SHORTED requires zero. */
    uint32_t nominal_current_na;
    uint32_t series_resistance_ohm;
};
struct bci_diagnostic_channel_result {
    size_t count;
    double mean_nv;
    double stddev_nv;
    double observed_peak_nv;
    double nominal_rms_estimate_ohm;
    double unclamped_estimate_ohm;
    int estimate_present;
};
struct bci_diagnostic_result {
    struct bci_diagnostic_options assumptions;
    struct bci_diagnostic_channel_result channels[BCI_DIAGNOSTIC_CHANNELS];
};

/* Offline, stateless, bounded arithmetic on externally quality-validated nV
 * samples; no allocation, device I/O, filtering, calibration, or source claim.
 * All int32_t values are accepted. Conversion from raw ADC counts and raw24
 * rail/gap/saturation checks belong to the controller, before this function.
 * nframes must be [32,4096], and frames must contain nframes complete frames.
 * Input/options/output storage must be disjoint. Inputs are never modified;
 * invalid arguments return INVALID and leave every output byte unchanged.
 *
 * Every success computes all eight channels: count=N, mean=sum(x)/N,
 * stddev=sqrt(sum((x-mean)^2)/N) (population, NOT N-1), and observed_peak_nv
 * =max(abs(x-mean)). Welford long-double accumulation uses fixed memory.
 *
 * SHORTED accepts only POPULATION_STATS/SOURCE_NONE, selected_channel=0,
 * current=0, series=0. It returns observations, no hardware pass/fail verdict.
 * NOMINAL_RMS_IMPEDANCE accepts only NOMINAL_CURRENT_RMS,
 * SOURCE_CYTON_NOMINAL_6NA, selected_channel in [0,7], current=6 nA,
 * series=2200 ohm. Only that channel has estimate_present=1:
 *   unclamped = sqrt(2)*population_stddev_nv/6 - 2200 [ohm]
 *   nominal_rms_estimate_ohm = max(0, unclamped).
 * Other channels have zero estimate fields/flag, including SHORTED mode.
 * Zero signal is valid; DC is removed. No detrend/window/filter is applied.
 * Unsupported modes, methods, currents, series or source assumptions fail.
 *
 * The controller must establish gain1/NORMAL, 250 SPS, nominal 31.25 Hz
 * excitation, single declared P or N input, known LOFF_FLIP/reference/bias
 * topology, complete readback, clean settled frames, and verified restoration.
 * This helper cannot establish those facts from samples. The copied assumption
 * fields describe the requested arithmetic, not verified physical provenance.
 * sqrt(2) is the named nominal-current RMS convention, not proof of the
 * excitation waveform or a frequency-selective impedance measurement.
 * Reference: docs/cyton-device-functions.md; research/provenance in
 * .build/xtos-roadmap-v1/protocol-research.md. Formula provenance:
 * OpenBCI_GUI/DataProcessing.pde, commit
 * e23869e7b5cc621e733d8fa0d81f05d477264306 (nominal 6 nA/2200 ohm).
 * No GUI display-data preprocessing is reproduced; this helper applies none.
 * Nominal current tolerance (TI specifies +/-20%), filtering, return paths and
 * approximate series subtraction remain uncorrected. No coefficients, medical
 * verdict, physical
 * impedance accuracy, or hardware acceptance is implied. Link with -lm.
 */
int bci_diagnostic_analyze(const struct bci_diagnostic_frame_nv *frames,
                           size_t nframes,
                           const struct bci_diagnostic_options *options,
                           struct bci_diagnostic_result *out);

#endif
