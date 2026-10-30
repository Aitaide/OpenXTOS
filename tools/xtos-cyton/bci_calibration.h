#ifndef XTOS_BCI_CALIBRATION_H
#define XTOS_BCI_CALIBRATION_H

#include <stddef.h>
#include <stdint.h>

#define BCI_CALIBRATION_VERSION 1u
#define BCI_CALIBRATION_CHANNELS 8u
#define BCI_CALIBRATION_MIN_POINTS 3u
#define BCI_CALIBRATION_MAX_POINTS 64u
#define BCI_CALIBRATION_SHA256_SIZE 65u
#define BCI_CALIBRATION_REFERENCE_SIZE 128u
#define BCI_CALIBRATION_MIN_SCALE 0.25
#define BCI_CALIBRATION_MAX_SCALE 4.0
#define BCI_CALIBRATION_MAX_OFFSET_NV 10737418240.0

enum bci_calibration_source_kind {
    BCI_CALIBRATION_SOURCE_FIXTURE = 1,
    BCI_CALIBRATION_SOURCE_EXTERNAL_REFERENCE = 2
};

struct bci_calibration_identity {
    enum bci_calibration_source_kind source_kind;
    uint32_t reserved[3];
    char session_sha256[BCI_CALIBRATION_SHA256_SIZE];
    char firmware_sha256[BCI_CALIBRATION_SHA256_SIZE];
    char reference_id[BCI_CALIBRATION_REFERENCE_SIZE];
};
struct bci_calibration_point {
    int32_t observed_nv[BCI_CALIBRATION_CHANNELS];
    int32_t reference_nv[BCI_CALIBRATION_CHANNELS];
};
struct bci_calibration_options {
    uint32_t selected_mask;
    uint32_t min_span_nv;
    uint32_t max_residual_nv;
    struct bci_calibration_identity identity;
};
struct bci_calibration_coefficients {
    uint32_t version;
    uint32_t valid;
    uint32_t selected_mask;
    uint32_t point_count;
    uint32_t min_span_nv;
    uint32_t residual_limit_nv;
    uint32_t reserved[4];
    struct bci_calibration_identity identity;
    double scale[BCI_CALIBRATION_CHANNELS];
    double offset_nv[BCI_CALIBRATION_CHANNELS];
    double max_residual_nv[BCI_CALIBRATION_CHANNELS];
};
struct bci_calibration_frame_nv {
    int32_t channel_nv[BCI_CALIBRATION_CHANNELS];
};

/* Offline C11 arithmetic, fixed storage, no I/O, allocation or mutable globals.
 * Link with -lm. This is a source API, not a wire format: serialize named fields.
 * Fit: reference_nv = scale * observed_nv + offset_nv. Each selected channel
 * needs >=3 distinct observed AND reference levels; BOTH spans must be >= the
 * explicit min_span_nv (>0). npoints is [3,64], mask is [1,255]. Duplicate
 * pairs are permitted and carry their ordinary least-squares weight.
 *
 * Means and centered covariance/variance are computed in long double; output
 * scale/offset are double, scale in [0.25,4] inclusive, |offset| <= 10737418240
 * nV. max_residual_nv is max absolute error of these stored coefficients,
 * evaluated in long double over supplied pairs; it must be <= the explicit
 * uint32 max_residual_nv (zero means exact). Comparison uses the unrounded
 * long-double residual. Inactive channels are canonical scale=1, offset=0,
 * residual=0. All int32 inputs, including limits, are valid numerical inputs.
 *
 * Identity: source_kind must be FIXTURE or EXTERNAL_REFERENCE; reserved words
 * must be zero. SHA strings are exactly 64 LOWERCASE hexadecimal characters
 * followed by NUL. reference_id is 1..127 printable non-space ASCII characters
 * (0x21..0x7e), then NUL with every remaining byte zero. These identifiers are
 * caller declarations only: neither SHA spelling nor source_kind authenticates
 * hardware, source bytes, reference traceability, quality or physical accuracy.
 * The controller must hash/bind the actual sources and validate acquisition.
 * No hardware-accepted field exists; valid=1 means structurally valid fit only.
 *
 * Apply is explicit opt-in. The full valid identity must match exactly,
 * including source_kind; FIXTURE coefficients cannot apply to an externally
 * declared physical/reference source. Each selected channel is transformed
 * using stored doubles in long-double arithmetic, rounded to nearest integer
 * with halfway cases AWAY FROM ZERO (roundl), then checked against int32 range.
 * Overflow fails (never clamps); inactive raw channels pass through unchanged.
 * Coefficient metadata, all numeric fields, inactive canonical values, reserved
 * words and identity are revalidated. Structural validation cannot detect a
 * tamper that replaces one valid coefficient/identity with another valid one;
 * authenticity and storage integrity are the controller's responsibility.
 *
 * Return 0 on success, negative errno on failure (errno is not set):
 * -EINVAL null, invalid count/options/identity/metadata/nonfinite coefficient,
 *         or any output overlap with input storage;
 * -ERANGE insufficient levels/span, singular variance, invalid fitted gain,
 *         residual limit exceeded, out-of-contract coefficient or apply overflow;
 * -ESTALE structurally valid apply identity differs from coefficient identity.
 * Bounds and nulls are checked before point access. Callers provide complete
 * readable input objects/arrays and writable output objects; inputs may share
 * read-only storage. Output must be disjoint from ALL inputs, including partial
 * overlap. Every failure preserves every output byte; inputs are immutable.
 * Successful writes commit only after all selected channels pass. No lifetime,
 * concurrency or alignment validation of caller storage is possible here.
 */
int bci_calibration_fit(const struct bci_calibration_point *points,
                        size_t npoints,
                        const struct bci_calibration_options *options,
                        struct bci_calibration_coefficients *out);
int bci_calibration_apply(const struct bci_calibration_coefficients *coefficients,
                          const struct bci_calibration_identity *identity,
                          const struct bci_calibration_frame_nv *raw,
                          struct bci_calibration_frame_nv *out);

#endif
