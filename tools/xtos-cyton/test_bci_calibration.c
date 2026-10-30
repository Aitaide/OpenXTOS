#include "bci_calibration.h"

#include <errno.h>
#include <limits.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned int cases;
static unsigned int checks;
#define CHECK(expression) do { \
    ++checks; \
    if (!(expression)) { \
        fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__, #expression); \
        exit(1); \
    } \
} while (0)

static struct bci_calibration_options options(void)
{
    struct bci_calibration_options o = {0};
    o.selected_mask = 255;
    o.min_span_nv = 100;
    o.max_residual_nv = 0;
    o.identity.source_kind = BCI_CALIBRATION_SOURCE_FIXTURE;
    memset(o.identity.session_sha256, 'a', 64);
    memset(o.identity.firmware_sha256, 'b', 64);
    strcpy(o.identity.reference_id, "fixture:known-line-v1");
    return o;
}

static void points(struct bci_calibration_point p[3])
{
    static const int scales2[8] = {1, 2, 4, 1, 2, 4, 2, 4};
    for (unsigned int i = 0; i < 3; ++i) {
        for (unsigned int c = 0; c < 8; ++c) {
            int32_t x = ((int32_t)i - 1) * 1000 + (int32_t)c * 2000;
            p[i].observed_nv[c] = x;
            p[i].reference_nv[c] = x * scales2[c] / 2 + (int32_t)c * 17 - 50;
        }
    }
}

static void test_exact_three_level_eight_channel_fit(void)
{
    static const double scales[8] = {0.5, 1, 2, 0.5, 1, 2, 1, 2};
    struct bci_calibration_point p[3], before[3];
    struct bci_calibration_options o = options(), obefore = o;
    struct bci_calibration_coefficients out;
    points(p);
    memcpy(before, p, sizeof(p));
    memset(&out, 0xa5, sizeof(out));
    CHECK(bci_calibration_fit(p, 3, &o, &out) == 0);
    CHECK(out.version == BCI_CALIBRATION_VERSION);
    CHECK(out.valid == 1 && out.selected_mask == 255 && out.point_count == 3);
    CHECK(memcmp(&out.identity, &o.identity, sizeof(o.identity)) == 0);
    CHECK(out.min_span_nv == 100 && out.residual_limit_nv == 0);
    for (unsigned int c = 0; c < 8; ++c) {
        CHECK(out.scale[c] == scales[c]);
        CHECK(out.offset_nv[c] == (double)c * 17 - 50);
        CHECK(out.max_residual_nv[c] == 0);
    }
    CHECK(memcmp(p, before, sizeof(p)) == 0);
    CHECK(memcmp(&o, &obefore, sizeof(o)) == 0);
}

static void fit_rejected(const struct bci_calibration_point *p, size_t n,
                         const struct bci_calibration_options *o, int error)
{
    struct bci_calibration_coefficients out, sentinel;
    struct bci_calibration_point saved_points[BCI_CALIBRATION_MAX_POINTS];
    struct bci_calibration_options saved_options;
    int save_points = p && n >= BCI_CALIBRATION_MIN_POINTS &&
                            n <= BCI_CALIBRATION_MAX_POINTS;
    if (save_points) memcpy(saved_points, p, n * sizeof(*p));
    if (o) memcpy(&saved_options, o, sizeof(*o));
    memset(&out, 0xa5, sizeof(out));
    memcpy(&sentinel, &out, sizeof(out));
    CHECK(bci_calibration_fit(p, n, o, &out) == error);
    CHECK(memcmp(&out, &sentinel, sizeof(out)) == 0);
    if (save_points) CHECK(memcmp(saved_points, p, n * sizeof(*p)) == 0);
    if (o) CHECK(memcmp(&saved_options, o, sizeof(*o)) == 0);
}

static void test_fit_argument_validation_before_point_access(void)
{
    struct bci_calibration_point p[3];
    struct bci_calibration_options o = options();
    struct bci_calibration_coefficients out;
    points(p);
    o.selected_mask = 0;
    fit_rejected(p, 3, &o, -EINVAL);
    o.selected_mask = 256;
    fit_rejected(p, 3, &o, -EINVAL);
    o = options(); o.min_span_nv = 0;
    fit_rejected(p, 3, &o, -EINVAL);
    o = options();
    fit_rejected(NULL, 3, &o, -EINVAL);
    fit_rejected(p, 3, NULL, -EINVAL);
    CHECK(bci_calibration_fit(p, 3, &o, NULL) == -EINVAL);
    /* A three-point object cannot safely supply 65 or SIZE_MAX points. */
    fit_rejected(p, 2, &o, -EINVAL);
    fit_rejected(p, 65, &o, -EINVAL);
    fit_rejected(p, SIZE_MAX, &o, -EINVAL);
    fit_rejected(p, 0, &o, -EINVAL);
    for (unsigned int field = 0; field < 8; ++field) {
        o = options();
        switch (field) {
        case 0: o.identity.source_kind = 0; break;
        case 1: o.identity.reserved[2] = 1; break;
        case 2: o.identity.session_sha256[0] = 'G'; break;
        case 3: o.identity.firmware_sha256[64] = 'b'; break;
        case 4: o.identity.reference_id[0] = '\0'; break;
        case 5: memset(o.identity.reference_id, 'x', sizeof(o.identity.reference_id)); break;
        case 6: o.identity.reference_id[3] = ' '; break;
        default: o.identity.reference_id[127] = 'x'; break;
        }
        fit_rejected(p, 3, &o, -EINVAL);
    }
    o = options();
    memset(&out, 0, sizeof(out));
    memcpy(&out, p, sizeof(p));
    struct bci_calibration_coefficients saved = out;
    CHECK(bci_calibration_fit((const void *)&out, 3, &o, &out) == -EINVAL);
    CHECK(memcmp(&out, &saved, sizeof(out)) == 0);
    union { struct bci_calibration_coefficients c; struct bci_calibration_options o; } alias;
    alias.o = o;
    unsigned char bytes[sizeof(alias)];
    memcpy(bytes, &alias, sizeof(alias));
    CHECK(bci_calibration_fit(p, 3, &alias.o, &alias.c) == -EINVAL);
    CHECK(memcmp(bytes, &alias, sizeof(alias)) == 0);
}

static void test_fit_multilevel_and_gain_rejection(void)
{
    struct bci_calibration_point p[64];
    struct bci_calibration_options o = options();
    struct bci_calibration_coefficients out;
    o.selected_mask = 1;
    points(p);
    p[2].observed_nv[0] = p[0].observed_nv[0];
    fit_rejected(p, 3, &o, -ERANGE);
    points(p);
    p[2].reference_nv[0] = p[0].reference_nv[0];
    fit_rejected(p, 3, &o, -ERANGE);
    points(p);
    for (unsigned int i = 0; i < 3; ++i) p[i].observed_nv[0] = 1;
    fit_rejected(p, 3, &o, -ERANGE);
    points(p); o.min_span_nv = 1001;
    fit_rejected(p, 3, &o, -ERANGE);
    o.min_span_nv = 1000;
    CHECK(bci_calibration_fit(p, 3, &o, &out) == 0);
    for (unsigned int mode = 0; mode < 4; ++mode) {
        for (unsigned int i = 0; i < 3; ++i) {
            int32_t x = ((int32_t)i - 1) * 1000;
            p[i].observed_nv[0] = x;
            p[i].reference_nv[0] = mode == 0 ? -x : mode == 1 ? 0 :
                                   mode == 2 ? x / 8 : x * 5;
        }
        o.min_span_nv = 1;
        fit_rejected(p, 3, &o, -ERANGE);
    }
    /* Inclusive scale endpoints, duplicates allowed, maximum point count. */
    for (unsigned int which = 0; which < 2; ++which) {
        for (unsigned int i = 0; i < 64; ++i) {
            memset(&p[i], 0, sizeof(p[i]));
            p[i].observed_nv[0] = ((int32_t)(i % 3) - 1) * 400;
            p[i].reference_nv[0] = which ? p[i].observed_nv[0] * 4 :
                                                        p[i].observed_nv[0] / 4;
        }
        CHECK(bci_calibration_fit(p, 64, &o, &out) == 0);
        CHECK(out.scale[0] == (which ? 4 : 0.25));
        for (unsigned int c = 1; c < 8; ++c) {
            CHECK(out.scale[c] == 1 && out.offset_nv[c] == 0 &&
                  out.max_residual_nv[c] == 0);
        }
    }
    /* All int32 extremes and large DC with exactly three nearby levels. */
    for (unsigned int i = 0; i < 3; ++i) {
        p[i].observed_nv[0] = i == 0 ? INT32_MIN : i == 1 ? 0 : INT32_MAX;
        p[i].reference_nv[0] = p[i].observed_nv[0];
    }
    o.min_span_nv = UINT32_MAX;
    CHECK(bci_calibration_fit(p, 3, &o, &out) == 0);
    for (unsigned int i = 0; i < 3; ++i) {
        p[i].observed_nv[0] = INT32_MAX - (int32_t)i;
        p[i].reference_nv[0] = INT32_MIN + 2 - (int32_t)i;
    }
    o.min_span_nv = 2;
    CHECK(bci_calibration_fit(p, 3, &o, &out) == 0);
    CHECK(out.scale[0] == 1 && out.offset_nv[0] == -4294967293.0);
}

static void test_apply_selected_channels_and_matching_sources(void)
{
    struct bci_calibration_point p[3];
    struct bci_calibration_options o = options();
    struct bci_calibration_coefficients coeff, saved;
    struct bci_calibration_frame_nv raw = {{-301, -200, -100, 0, 100, 201, 300, 400}};
    struct bci_calibration_frame_nv before = raw, out;
    static const int32_t expected[8] = {-201, -233, -216, 1, 118, 437, 352, 869};
    points(p);
    for (unsigned int source = 1; source <= 2; ++source) {
        o.identity.source_kind = (enum bci_calibration_source_kind)source;
        for (unsigned int mask = 1; mask <= 255; ++mask) {
            o.selected_mask = mask;
            CHECK(bci_calibration_fit(p, 3, &o, &coeff) == 0);
            saved = coeff;
            struct bci_calibration_identity identity_before = o.identity;
            CHECK(bci_calibration_apply(&coeff, &o.identity, &raw, &out) == 0);
            for (unsigned int c = 0; c < 8; ++c)
                CHECK(out.channel_nv[c] == ((mask & (1u << c)) ? expected[c] : raw.channel_nv[c]));
            CHECK(memcmp(&raw, &before, sizeof(raw)) == 0);
            CHECK(memcmp(&coeff, &saved, sizeof(coeff)) == 0);
            CHECK(memcmp(&o.identity, &identity_before, sizeof(o.identity)) == 0);
        }
    }
}

static void apply_rejected(const struct bci_calibration_coefficients *coeff,
                           const struct bci_calibration_identity *id,
                           const struct bci_calibration_frame_nv *raw, int error)
{
    struct bci_calibration_frame_nv out, sentinel;
    struct bci_calibration_frame_nv saved_raw;
    struct bci_calibration_identity saved_id;
    struct bci_calibration_coefficients saved_coeff;
    if (raw) memcpy(&saved_raw, raw, sizeof(*raw));
    if (id) memcpy(&saved_id, id, sizeof(*id));
    if (coeff) memcpy(&saved_coeff, coeff, sizeof(*coeff));
    memset(&out, 0xa5, sizeof(out));
    sentinel = out;
    CHECK(bci_calibration_apply(coeff, id, raw, &out) == error);
    CHECK(memcmp(&out, &sentinel, sizeof(out)) == 0);
    if (raw) CHECK(memcmp(&saved_raw, raw, sizeof(*raw)) == 0);
    if (id) CHECK(memcmp(&saved_id, id, sizeof(*id)) == 0);
    if (coeff) CHECK(memcmp(&saved_coeff, coeff, sizeof(*coeff)) == 0);
}

static void test_apply_identity_and_coefficient_validation(void)
{
    struct bci_calibration_point p[3];
    struct bci_calibration_options o = options();
    struct bci_calibration_coefficients coeff, bad;
    struct bci_calibration_frame_nv raw = {{0}}, out;
    struct bci_calibration_identity id;
    points(p);
    CHECK(bci_calibration_fit(p, 3, &o, &coeff) == 0);
    for (unsigned int i = 0; i < 4; ++i) {
        id = o.identity;
        if (i == 0) id.source_kind = BCI_CALIBRATION_SOURCE_EXTERNAL_REFERENCE;
        if (i == 1) id.session_sha256[0] = 'c';
        if (i == 2) id.firmware_sha256[0] = 'c';
        if (i == 3) strcpy(id.reference_id, "fixture:other-line-v1");
        apply_rejected(&coeff, &id, &raw, -ESTALE);
    }
    id = o.identity; id.session_sha256[0] = 'A';
    apply_rejected(&coeff, &id, &raw, -EINVAL);
    apply_rejected(NULL, &o.identity, &raw, -EINVAL);
    apply_rejected(&coeff, NULL, &raw, -EINVAL);
    apply_rejected(&coeff, &o.identity, NULL, -EINVAL);
    CHECK(bci_calibration_apply(&coeff, &o.identity, &raw, NULL) == -EINVAL);
    for (unsigned int i = 0; i < 22; ++i) {
        int error = -EINVAL;
        bad = coeff;
        switch (i) {
        case 0: bad.version = 0; break;
        case 1: bad.version = 2; break;
        case 2: bad.valid = 0; break;
        case 3: bad.valid = 2; break;
        case 4: bad.selected_mask = 0; break;
        case 5: bad.selected_mask = 256; break;
        case 6: bad.point_count = 2; break;
        case 7: bad.point_count = 65; break;
        case 8: bad.min_span_nv = 0; break;
        case 9: bad.reserved[3] = 1; break;
        case 10: bad.identity.reserved[0] = 1; break;
        case 11: bad.identity.source_kind = 3; break;
        case 12: bad.scale[7] = NAN; break;
        case 13: bad.offset_nv[7] = INFINITY; break;
        case 14: bad.max_residual_nv[7] = NAN; break;
        case 15: bad.scale[7] = -1; error = -ERANGE; break;
        case 16: bad.scale[7] = 0.249; error = -ERANGE; break;
        case 17: bad.scale[7] = 4.001; error = -ERANGE; break;
        case 18: bad.offset_nv[7] = BCI_CALIBRATION_MAX_OFFSET_NV + 1; error = -ERANGE; break;
        case 19: bad.max_residual_nv[7] = -0.1; error = -ERANGE; break;
        case 20: bad.max_residual_nv[7] = 1; error = -ERANGE; break;
        default: bad.identity.reference_id[127] = 'x'; break;
        }
        apply_rejected(&bad, &o.identity, &raw, error);
    }
    o.selected_mask = 1;
    CHECK(bci_calibration_fit(p, 3, &o, &coeff) == 0);
    for (unsigned int i = 0; i < 3; ++i) {
        bad = coeff;
        if (i == 0) bad.scale[7] = 2;
        if (i == 1) bad.offset_nv[7] = 1;
        if (i == 2) { bad.residual_limit_nv = 1; bad.max_residual_nv[7] = 1; }
        apply_rejected(&bad, &o.identity, &raw, -EINVAL);
    }
    struct bci_calibration_frame_nv raw_before = raw;
    CHECK(bci_calibration_apply(&coeff, &o.identity, &raw, &raw) == -EINVAL);
    CHECK(memcmp(&raw, &raw_before, sizeof(raw)) == 0);
    union { struct bci_calibration_frame_nv frame; int32_t words[9]; } partial = {0};
    unsigned char saved[sizeof(partial)];
    memcpy(saved, &partial, sizeof(partial));
    CHECK(bci_calibration_apply(&coeff, &o.identity, &partial.frame,
                               (void *)&partial.words[1]) == -EINVAL);
    CHECK(memcmp(saved, &partial, sizeof(partial)) == 0);
    bad = coeff;
    CHECK(bci_calibration_apply(&coeff, &o.identity, &raw,
                               (void *)&coeff.scale[0]) == -EINVAL);
    CHECK(memcmp(&coeff, &bad, sizeof(coeff)) == 0);
    id = o.identity;
    CHECK(bci_calibration_apply(&coeff, &id, &raw, (void *)&id) == -EINVAL);
    CHECK(memcmp(&id, &o.identity, sizeof(id)) == 0);
    CHECK(bci_calibration_apply(&coeff, &coeff.identity, &raw, &out) == 0);
}

static void test_apply_rounding_and_range_rejection(void)
{
    struct bci_calibration_point p[3];
    struct bci_calibration_options o = options();
    struct bci_calibration_coefficients coeff;
    struct bci_calibration_frame_nv raw = {{0}}, out;
    points(p);
    CHECK(bci_calibration_fit(p, 3, &o, &coeff) == 0);
    /* Fail only on channel seven: earlier results must never be committed. */
    raw.channel_nv[7] = INT32_MAX;
    apply_rejected(&coeff, &o.identity, &raw, -ERANGE);
    raw.channel_nv[7] = INT32_MIN;
    apply_rejected(&coeff, &o.identity, &raw, -ERANGE);
    o.selected_mask = 1;
    for (unsigned int i = 0; i < 3; ++i) {
        p[i].observed_nv[0] = ((int32_t)i - 1) * 100;
        p[i].reference_nv[0] = p[i].observed_nv[0] / 2;
    }
    CHECK(bci_calibration_fit(p, 3, &o, &coeff) == 0);
    for (int32_t x = -5; x <= 5; ++x) {
        raw.channel_nv[0] = x;
        CHECK(bci_calibration_apply(&coeff, &o.identity, &raw, &out) == 0);
        CHECK(out.channel_nv[0] == (x < 0 ? (x - 1) / 2 : (x + 1) / 2));
        CHECK(out.channel_nv[7] == INT32_MIN);
    }
    /* Structurally valid numeric fixtures isolate int32 rounding boundaries. */
    coeff.scale[0] = 1;
    for (unsigned int side = 0; side < 2; ++side) {
        raw.channel_nv[0] = side ? INT32_MAX : INT32_MIN;
        coeff.offset_nv[0] = side ? 0.25 : -0.25;
        CHECK(bci_calibration_apply(&coeff, &o.identity, &raw, &out) == 0);
        CHECK(out.channel_nv[0] == raw.channel_nv[0]);
        coeff.offset_nv[0] = side ? 0.5 : -0.5;
        apply_rejected(&coeff, &o.identity, &raw, -ERANGE);
        coeff.offset_nv[0] = side ? -0.5 : 0.5;
        CHECK(bci_calibration_apply(&coeff, &o.identity, &raw, &out) == 0);
        CHECK(out.channel_nv[0] == raw.channel_nv[0]);
    }
}

static void test_fit_residual_threshold_in_c(void)
{
    struct bci_calibration_point p[3];
    struct bci_calibration_options o = options();
    struct bci_calibration_coefficients out;
    for (unsigned int i = 0; i < 3; ++i)
        for (unsigned int c = 0; c < 8; ++c) {
            p[i].observed_nv[c] = ((int32_t)i - 1) * 100;
            p[i].reference_nv[c] = p[i].observed_nv[c] + (i == 1 ? 3 : 0);
        }
    o.max_residual_nv = 1;
    fit_rejected(p, 3, &o, -ERANGE);
    o.max_residual_nv = 2;
    CHECK(bci_calibration_fit(p, 3, &o, &out) == 0);
    for (unsigned int c = 0; c < 8; ++c)
        CHECK(out.scale[c] == 1 && out.offset_nv[c] == 1 && out.max_residual_nv[c] == 2);
    p[1].reference_nv[7] += 100;
    fit_rejected(p, 3, &o, -ERANGE);
}

static void test_identity_format_boundaries(void)
{
    struct bci_calibration_options o = options();
    struct bci_calibration_point p[3];
    struct bci_calibration_coefficients coeff;
    struct bci_calibration_frame_nv raw = {{0}}, out;
    points(p);
    memset(o.identity.reference_id, '~', 127);
    o.identity.reference_id[127] = 0;
    CHECK(bci_calibration_fit(p, 3, &o, &coeff) == 0);
    CHECK(bci_calibration_apply(&coeff, &o.identity, &raw, &out) == 0);
    for (unsigned int i = 0; i < 5; ++i) {
        struct bci_calibration_options bad = o;
        if (i == 0) bad.identity.reference_id[0] = 0x7f;
        if (i == 1) bad.identity.reference_id[0] = (char)0x80;
        if (i == 2) bad.identity.reference_id[0] = '\n';
        if (i == 3) bad.identity.session_sha256[63] = '\0';
        if (i == 4) bad.identity.firmware_sha256[0] = 'B';
        fit_rejected(p, 3, &bad, -EINVAL);
    }
    o.identity.source_kind = BCI_CALIBRATION_SOURCE_EXTERNAL_REFERENCE;
    CHECK(bci_calibration_fit(p, 3, &o, &coeff) == 0);
    struct bci_calibration_identity fixture = o.identity;
    fixture.source_kind = BCI_CALIBRATION_SOURCE_FIXTURE;
    apply_rejected(&coeff, &fixture, &raw, -ESTALE);
    o.selected_mask = 0x80;
    /* Constant unused channels do not participate in fitting. */
    for (unsigned int i = 0; i < 3; ++i)
        for (unsigned int c = 0; c < 7; ++c) {
            p[i].observed_nv[c] = INT32_MIN;
            p[i].reference_nv[c] = INT32_MAX;
        }
    CHECK(bci_calibration_fit(p, 3, &o, &coeff) == 0);
    CHECK(coeff.scale[7] == 2 && coeff.scale[0] == 1);
}

int main(void)
{
    test_exact_three_level_eight_channel_fit(); ++cases;
    test_fit_argument_validation_before_point_access(); ++cases;
    test_fit_multilevel_and_gain_rejection(); ++cases;
    test_apply_selected_channels_and_matching_sources(); ++cases;
    test_apply_identity_and_coefficient_validation(); ++cases;
    test_apply_rounding_and_range_rejection(); ++cases;
    test_fit_residual_threshold_in_c(); ++cases;
    test_identity_format_boundaries(); ++cases;
    printf("bci_calibration: %u cases, %u checks passed\n", cases, checks);
    return 0;
}
