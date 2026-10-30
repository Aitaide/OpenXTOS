#include "bci_calibration.h"

#include <errno.h>
#include <math.h>
#include <string.h>

static int overlaps(const void *a, size_t na, const void *b, size_t nb)
{
    uintptr_t x = (uintptr_t)a, y = (uintptr_t)b;
    return x <= y ? y - x < na : x - y < nb;
}

static int valid_sha256(const char value[BCI_CALIBRATION_SHA256_SIZE])
{
    for (unsigned int i = 0; i < 64; ++i)
        if (!((value[i] >= '0' && value[i] <= '9') ||
              (value[i] >= 'a' && value[i] <= 'f')))
            return 0;
    return value[64] == '\0';
}

static int valid_identity(const struct bci_calibration_identity *id)
{
    if (id->source_kind != BCI_CALIBRATION_SOURCE_FIXTURE &&
        id->source_kind != BCI_CALIBRATION_SOURCE_EXTERNAL_REFERENCE)
        return 0;
    for (unsigned int i = 0; i < 3; ++i)
        if (id->reserved[i])
            return 0;
    if (!valid_sha256(id->session_sha256) || !valid_sha256(id->firmware_sha256))
        return 0;
    size_t end = 0;
    while (end < BCI_CALIBRATION_REFERENCE_SIZE && id->reference_id[end]) {
        unsigned char ch = (unsigned char)id->reference_id[end];
        if (ch < 0x21 || ch > 0x7e)
            return 0;
        ++end;
    }
    if (end == 0 || end == BCI_CALIBRATION_REFERENCE_SIZE)
        return 0;
    for (; end < BCI_CALIBRATION_REFERENCE_SIZE; ++end)
        if (id->reference_id[end])
            return 0;
    return 1;
}

static int has_levels_and_span(const struct bci_calibration_point *points,
                               size_t npoints, unsigned int channel,
                               int reference, uint32_t minimum_span)
{
    int32_t levels[3] = {0};
    unsigned int distinct = 0;
    int32_t minimum = INT32_MAX, maximum = INT32_MIN;
    for (size_t i = 0; i < npoints; ++i) {
        int32_t x = reference ? points[i].reference_nv[channel] :
                               points[i].observed_nv[channel];
        if (x < minimum) minimum = x;
        if (x > maximum) maximum = x;
        if (distinct < 3) {
            unsigned int j = 0;
            while (j < distinct && levels[j] != x) ++j;
            if (j == distinct) levels[distinct++] = x;
        }
    }
    return distinct == 3 && (int64_t)maximum - minimum >= minimum_span;
}

int bci_calibration_fit(const struct bci_calibration_point *points,
                        size_t npoints,
                        const struct bci_calibration_options *options,
                        struct bci_calibration_coefficients *out)
{
    if (!points || !options || !out || npoints < BCI_CALIBRATION_MIN_POINTS ||
        npoints > BCI_CALIBRATION_MAX_POINTS)
        return -EINVAL;
    if (overlaps(out, sizeof(*out), points, npoints * sizeof(*points)) ||
        overlaps(out, sizeof(*out), options, sizeof(*options)))
        return -EINVAL;
    if (!options->selected_mask || options->selected_mask > 255 ||
        !options->min_span_nv || !valid_identity(&options->identity))
        return -EINVAL;
    struct bci_calibration_coefficients result = {0};
    result.version = BCI_CALIBRATION_VERSION;
    result.valid = 1;
    result.selected_mask = options->selected_mask;
    result.point_count = (uint32_t)npoints;
    result.min_span_nv = options->min_span_nv;
    result.residual_limit_nv = options->max_residual_nv;
    result.identity = options->identity;
    for (unsigned int c = 0; c < BCI_CALIBRATION_CHANNELS; ++c) {
        long double mx = 0, my = 0, xx = 0, xy = 0;
        result.scale[c] = 1;
        if (!(options->selected_mask & (1u << c)))
            continue;
        if (!has_levels_and_span(points, npoints, c, 0, options->min_span_nv) ||
            !has_levels_and_span(points, npoints, c, 1, options->min_span_nv))
            return -ERANGE;
        for (size_t i = 0; i < npoints; ++i) {
            mx += points[i].observed_nv[c];
            my += points[i].reference_nv[c];
        }
        mx /= npoints;
        my /= npoints;
        for (size_t i = 0; i < npoints; ++i) {
            long double dx = points[i].observed_nv[c] - mx;
            long double dy = points[i].reference_nv[c] - my;
            xx += dx * dx;
            xy += dx * dy;
        }
        if (!(xx > 0) || !isfinite(xx) || !isfinite(xy))
            return -ERANGE;
        long double scale = xy / xx;
        if (!isfinite(scale) || scale < BCI_CALIBRATION_MIN_SCALE ||
            scale > BCI_CALIBRATION_MAX_SCALE)
            return -ERANGE;
        result.scale[c] = (double)scale;
        result.offset_nv[c] = (double)(my - scale * mx);
        if (!isfinite(result.offset_nv[c]) ||
            fabs(result.offset_nv[c]) > BCI_CALIBRATION_MAX_OFFSET_NV)
            return -ERANGE;
        long double maximum = 0;
        for (size_t i = 0; i < npoints; ++i) {
            long double residual = fabsl((long double)result.scale[c] *
                points[i].observed_nv[c] + result.offset_nv[c] -
                points[i].reference_nv[c]);
            if (residual > maximum)
                maximum = residual;
        }
        if (!isfinite(maximum) || maximum > options->max_residual_nv)
            return -ERANGE;
        result.max_residual_nv[c] = (double)maximum;
    }
    memcpy(out, &result, sizeof(result));
    return 0;
}

static int validate_coefficients(const struct bci_calibration_coefficients *coeff)
{
    if (coeff->version != BCI_CALIBRATION_VERSION || coeff->valid != 1 ||
        !coeff->selected_mask || coeff->selected_mask > 255 ||
        coeff->point_count < BCI_CALIBRATION_MIN_POINTS ||
        coeff->point_count > BCI_CALIBRATION_MAX_POINTS || !coeff->min_span_nv ||
        !valid_identity(&coeff->identity))
        return -EINVAL;
    for (unsigned int i = 0; i < 4; ++i)
        if (coeff->reserved[i])
            return -EINVAL;
    for (unsigned int c = 0; c < BCI_CALIBRATION_CHANNELS; ++c) {
        if (!isfinite(coeff->scale[c]) || !isfinite(coeff->offset_nv[c]) ||
            !isfinite(coeff->max_residual_nv[c]))
            return -EINVAL;
        if (!(coeff->selected_mask & (1u << c))) {
            if (coeff->scale[c] != 1 || coeff->offset_nv[c] != 0 ||
                coeff->max_residual_nv[c] != 0)
                return -EINVAL;
        } else if (coeff->scale[c] < BCI_CALIBRATION_MIN_SCALE ||
                   coeff->scale[c] > BCI_CALIBRATION_MAX_SCALE ||
                   fabs(coeff->offset_nv[c]) > BCI_CALIBRATION_MAX_OFFSET_NV ||
                   coeff->max_residual_nv[c] < 0 ||
                   coeff->max_residual_nv[c] > coeff->residual_limit_nv) {
            return -ERANGE;
        }
    }
    return 0;
}

static int same_identity(const struct bci_calibration_identity *a,
                         const struct bci_calibration_identity *b)
{
    /* Compare fields, never unspecified structure padding. */
    return a->source_kind == b->source_kind &&
           memcmp(a->session_sha256, b->session_sha256,
                  BCI_CALIBRATION_SHA256_SIZE) == 0 &&
           memcmp(a->firmware_sha256, b->firmware_sha256,
                  BCI_CALIBRATION_SHA256_SIZE) == 0 &&
           memcmp(a->reference_id, b->reference_id,
                  BCI_CALIBRATION_REFERENCE_SIZE) == 0;
}

int bci_calibration_apply(const struct bci_calibration_coefficients *coefficients,
                          const struct bci_calibration_identity *identity,
                          const struct bci_calibration_frame_nv *raw,
                          struct bci_calibration_frame_nv *out)
{
    if (!coefficients || !identity || !raw || !out)
        return -EINVAL;
    if (overlaps(out, sizeof(*out), coefficients, sizeof(*coefficients)) ||
        overlaps(out, sizeof(*out), identity, sizeof(*identity)) ||
        overlaps(out, sizeof(*out), raw, sizeof(*raw)))
        return -EINVAL;
    int status = validate_coefficients(coefficients);
    if (status)
        return status;
    if (!valid_identity(identity))
        return -EINVAL;
    if (!same_identity(&coefficients->identity, identity))
        return -ESTALE;
    struct bci_calibration_frame_nv result = *raw;
    for (unsigned int c = 0; c < BCI_CALIBRATION_CHANNELS; ++c) {
        if (coefficients->selected_mask & (1u << c)) {
            long double rounded = roundl((long double)coefficients->scale[c] *
                raw->channel_nv[c] + coefficients->offset_nv[c]);
            if (!isfinite(rounded) || rounded < INT32_MIN || rounded > INT32_MAX)
                return -ERANGE;
            result.channel_nv[c] = (int32_t)rounded;
        }
    }
    memcpy(out, &result, sizeof(result));
    return 0;
}
