#define _GNU_SOURCE
#include "bci_calibration_cli.h"
#include "bci_calibration.h"
#include "bci_sha256.h"
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <locale.h>
#include <math.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

struct bytes { unsigned char *p; size_t n; };
struct text { char *p; size_t n, cap; };
struct args {
    const char *points, *model, *input, *session, *firmware, *reference, *source, *output;
    uint32_t mask, span, residual;
};
static int diagnostic(const char *what, int error, int status)
{
    fprintf(stderr, "bci calibration: %s: %s (errno=%d)\n", what, strerror(error), error);
    return status;
}
static int load(const char *path, size_t limit, struct bytes *b)
{
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) return errno;
    struct stat st;
    int e = 0;
    if (fstat(fd, &st) < 0) e = errno;
    else if (!S_ISREG(st.st_mode)) e = EINVAL;
    else if (st.st_size < 0 || (uintmax_t)st.st_size > limit) e = EFBIG;
    if (!e) {
        b->p = malloc(limit + 1);
        if (!b->p) e = ENOMEM;
    }
    while (!e) {
        ssize_t n = read(fd, b->p + b->n, limit + 1 - b->n);
        if (n < 0) { if (errno == EINTR) continue; e = errno; break; }
        if (!n) break;
        b->n += (size_t)n;
        if (b->n > limit) { e = EFBIG; break; }
    }
    if (close(fd) < 0 && !e) e = errno;
    return e;
}
static int u32(const char *s, uint32_t min, uint32_t max, uint32_t *v)
{
    uint32_t n = 0;
    if (!s || !*s) return EINVAL;
    for (; *s; ++s) {
        if (*s < '0' || *s > '9') return EINVAL;
        unsigned d = (unsigned)(*s - '0');
        if (n > (max - d) / 10 || d > max) return ERANGE;
        n = n * 10 + d;
    }
    if (n < min || n > max) return ERANGE;
    *v = n;
    return 0;
}
static int arguments(int argc, char **argv, int fit, struct args *a)
{
    static const char *const names[] = {"--points", "--model", "--input", "--session-file", "--firmware-file", "--reference-id", "--source", "--output", "--mask", "--min-span-nv", "--max-residual-nv"};
    unsigned seen = 0;
    for (int i = 2; i < argc; i += 2) {
        unsigned j = 0;
        while (j < 11 && strcmp(argv[i], names[j])) ++j;
        if (j == 11 || i + 1 >= argc || (seen & (1u << j))) return EINVAL;
        if ((fit && (j == 1 || j == 2)) || (!fit && (j == 0 || j >= 8))) return EINVAL;
        seen |= 1u << j;
        const char *s = argv[i+1];
        switch (j) {
        case 0: a->points = s; break;
        case 1: a->model = s; break;
        case 2: a->input = s; break;
        case 3: a->session = s; break;
        case 4: a->firmware = s; break;
        case 5: a->reference = s; break;
        case 6: a->source = s; break;
        case 7: a->output = s; break;
        case 8: if (u32(s, 1, 255, &a->mask)) return EINVAL; break;
        case 9: if (u32(s, 1, UINT32_MAX, &a->span)) return EINVAL; break;
        case 10: if (u32(s, 0, UINT32_MAX, &a->residual)) return EINVAL; break;
        }
    }
    return seen == (fit ? 2041u : 254u) ? 0 : EINVAL;
}
/* Strict signed decimal grammar: optional sign followed by ASCII digits only. */
static int csv(const struct bytes *b, unsigned fields, size_t minimum, size_t maximum,
               int32_t *values, size_t *rows)
{
    size_t at = 0, n = 0;
    while (at < b->n) {
        if (n == maximum) return E2BIG;
        for (unsigned c = 0; c < fields; ++c) {
            int negative = 0;
            if (at < b->n && (b->p[at] == '-' || b->p[at] == '+')) negative = b->p[at++] == '-';
            if (at == b->n || b->p[at] < '0' || b->p[at] > '9') return EINVAL;
            uint32_t v = 0, limit = negative ? 2147483648u : 2147483647u;
            while (at < b->n && b->p[at] >= '0' && b->p[at] <= '9') {
                unsigned digit = b->p[at++] - '0';
                if (v > (limit - digit) / 10) return ERANGE;
                v = v * 10 + digit;
            }
            values[n * fields + c] = negative ? (int32_t)(-(int64_t)v) : (int32_t)v;
            unsigned char delimiter = c + 1 == fields ? '\n' : ',';
            if (at == b->n || b->p[at++] != delimiter) return EINVAL;
        }
        ++n;
    }
    if (n < minimum) return EINVAL;
    *rows = n;
    return 0;
}
static int append(struct text *t, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(t->p + t->n, t->cap - t->n, fmt, ap);
    va_end(ap);
    if (n < 0 || (size_t)n >= t->cap - t->n) return EOVERFLOW;
    t->n += (size_t)n;
    return 0;
}
static int identity(const struct args *a, const struct bytes *s, const struct bytes *f,
                    struct bci_calibration_identity *id)
{
    if (!strcmp(a->source, "fixture")) id->source_kind = BCI_CALIBRATION_SOURCE_FIXTURE;
    else if (!strcmp(a->source, "external-reference")) id->source_kind = BCI_CALIBRATION_SOURCE_EXTERNAL_REFERENCE;
    else return EINVAL;
    size_t n = strlen(a->reference);
    if (!n || n >= sizeof(id->reference_id)) return EINVAL;
    for (size_t i = 0; i < n; ++i)
        if ((unsigned char)a->reference[i] < 0x21 || (unsigned char)a->reference[i] > 0x7e) return EINVAL;
    memcpy(id->reference_id, a->reference, n);
    bci_sha256_hex(s->p, s->n, id->session_sha256);
    bci_sha256_hex(f->p, f->n, id->firmware_sha256);
    return 0;
}
static int serialize(const struct bci_calibration_coefficients *c, const char *pairs,
                     uint32_t spans[8][2], struct text *t)
{
    int e = append(t, "bci_calibration_model=1\nversion=%u\nvalid=%u\nselected_mask=%u\npoint_count=%u\nmin_span_nv=%u\nresidual_limit_nv=%u\nsource=%s\nsession_sha256=%s\nfirmware_sha256=%s\nreference_id=%s\npairs_sha256=%s\n", c->version, c->valid, c->selected_mask, c->point_count, c->min_span_nv, c->residual_limit_nv,
        c->identity.source_kind == BCI_CALIBRATION_SOURCE_FIXTURE ? "fixture" : "external-reference", c->identity.session_sha256, c->identity.firmware_sha256, c->identity.reference_id, pairs);
    for (unsigned i = 0; !e && i < 8; ++i)
        e = append(t, "scale_%u=%a\noffset_nv_%u=%a\nmax_residual_nv_%u=%a\nobserved_span_nv_%u=%u\nreference_span_nv_%u=%u\n", i, c->scale[i], i, c->offset_nv[i], i, c->max_residual_nv[i], i, spans[i][0], i, spans[i][1]);
    char digest[65];
    bci_sha256_hex(t->p, t->n, digest);
    if (!e) e = append(t, "sha256=%s\n", digest);
    return e;
}
static int publish(const char *path, const struct text *t)
{
    int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, 0600);
    if (fd < 0) return diagnostic("create output", errno, 2);
    int e = 0;
    size_t at = 0;
    while (at < t->n) {
        ssize_t n = write(fd, t->p + at, t->n - at);
        if (n < 0) { if (errno == EINTR) continue; e = errno; break; }
        if (!n) { e = EIO; break; }
        at += (size_t)n;
    }
    if (!e && fdatasync(fd) < 0) e = errno;
    if (close(fd) < 0 && !e) e = errno;
    return e ? diagnostic("output incomplete (file retained)", e, 3) : 0;
}
static char *field(char **cursor, const char *name)
{
    char *p = *cursor;
    size_t n = strlen(name);
    if (strncmp(p, name, n) || p[n] != '=') return NULL;
    char *end = strchr(p + n + 1, '\n');
    if (!end) return NULL;
    *end = 0;
    *cursor = end + 1;
    return p + n + 1;
}
static int hexhash(const char *s)
{
    if (!s || strlen(s) != 64) return 0;
    for (unsigned i = 0; i < 64; ++i)
        if (!((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'a' && s[i] <= 'f'))) return 0;
    return 1;
}
static int parse_model(const struct bytes *b, struct bci_calibration_coefficients *c)
{
    /* Verify exact immutable bytes before interpreting any model fields. */
    if (b->n < 72 || memchr(b->p, 0, b->n)) return EINVAL;
    size_t prefix = b->n - 72;
    char digest[65];
    bci_sha256_hex(b->p, prefix, digest);
    if (memcmp(b->p + prefix, "sha256=", 7) ||
        memcmp(b->p + prefix + 7, digest, 64) || b->p[b->n - 1] != '\n') return EINVAL;
    char copy[8193];
    memcpy(copy, b->p, prefix); copy[prefix] = 0;
    char *at = copy, *s;
    s = field(&at, "bci_calibration_model");
    if (!s || strcmp(s, "1")) return EINVAL;
    const char *names[] = {"version", "valid", "selected_mask", "point_count", "min_span_nv", "residual_limit_nv"};
    uint32_t *values[] = {&c->version, &c->valid, &c->selected_mask, &c->point_count, &c->min_span_nv, &c->residual_limit_nv};
    for (unsigned i = 0; i < 6; ++i) {
        s = field(&at, names[i]);
        if (!s || u32(s, 0, UINT32_MAX, values[i])) return EINVAL;
    }
    s = field(&at, "source");
    if (!s) return EINVAL;
    if (!strcmp(s, "fixture")) c->identity.source_kind = BCI_CALIBRATION_SOURCE_FIXTURE;
    else if (!strcmp(s, "external-reference")) c->identity.source_kind = BCI_CALIBRATION_SOURCE_EXTERNAL_REFERENCE;
    else return EINVAL;
    s = field(&at, "session_sha256");
    if (!hexhash(s)) return EINVAL;
    memcpy(c->identity.session_sha256, s, 65);
    s = field(&at, "firmware_sha256");
    if (!hexhash(s)) return EINVAL;
    memcpy(c->identity.firmware_sha256, s, 65);
    s = field(&at, "reference_id");
    if (!s || !*s || strlen(s) >= sizeof(c->identity.reference_id)) return EINVAL;
    for (size_t i = 0; s[i]; ++i)
        if ((unsigned char)s[i] < 0x21 || (unsigned char)s[i] > 0x7e) return EINVAL;
    strcpy(c->identity.reference_id, s);
    s = field(&at, "pairs_sha256");
    if (!hexhash(s)) return EINVAL;
    char pairs[65]; memcpy(pairs, s, 65);
    uint32_t spans[8][2] = {{0}};
    for (unsigned i = 0; i < 8; ++i) {
        const char *keys[] = {"scale", "offset_nv", "max_residual_nv"};
        double *v[] = {&c->scale[i], &c->offset_nv[i], &c->max_residual_nv[i]};
        for (unsigned j = 0; j < 3; ++j) {
            char key[40]; snprintf(key, sizeof(key), "%s_%u", keys[j], i);
            s = field(&at, key);
            if (!s || !*s) return EINVAL;
            char *end;
            errno = 0;
            *v[j] = strtod(s, &end);
            if (errno || *end || !isfinite(*v[j])) return EINVAL;
        }
        for (unsigned j = 0; j < 2; ++j) {
            char key[40];
            snprintf(key, sizeof(key), "%s_span_nv_%u", j ? "reference" : "observed", i);
            s = field(&at, key);
            if (!s || u32(s, 0, UINT32_MAX, &spans[i][j])) return EINVAL;
            if ((c->selected_mask & (1u << i)) && spans[i][j] < c->min_span_nv) return ERANGE;
        }
    }
    if (*at) return EINVAL;
    /* Canonical named text is portable; reject aliases, case, unknown fields,
     * alternate float spellings and any trailing bytes, including extra lines. */
    char canonical[8192];
    struct text t = {canonical, 0, sizeof(canonical)};
    int e = serialize(c, pairs, spans, &t);
    if (e || t.n != b->n || memcmp(t.p, b->p, b->n)) return EINVAL;
    return 0;
}
static int apply_command(int argc, char **argv)
{
    struct args a = {0};
    int e = arguments(argc, argv, 0, &a);
    if (e) return diagnostic("arguments", e, 2);
    struct bytes session = {0}, firmware = {0}, model = {0}, input = {0};
    struct bci_calibration_identity id = {0};
    struct bci_calibration_coefficients coeff = {0};
    int32_t *parsed = malloc(4096 * 8 * sizeof(*parsed));
    char *storage = malloc(524289);
    struct text output = {storage, 0, 524289};
    size_t count = 0;
    if (!parsed || !storage) e = ENOMEM;
    if (!e) e = load(a.session, 1048576, &session);
    if (!e) e = load(a.firmware, 1048576, &firmware);
    if (!e) e = identity(&a, &session, &firmware, &id);
    if (!e) e = load(a.model, 8192, &model);
    if (!e) e = parse_model(&model, &coeff);
    if (!e) e = load(a.input, 524288, &input);
    if (!e) e = csv(&input, 8, 1, 4096, parsed, &count);
    for (size_t i = 0; !e && i < count; ++i) {
        struct bci_calibration_frame_nv raw, corrected;
        for (unsigned j = 0; j < 8; ++j) raw.channel_nv[j] = parsed[i*8+j];
        e = -bci_calibration_apply(&coeff, &id, &raw, &corrected);
        for (unsigned j = 0; !e && j < 8; ++j)
            e = append(&output, "%" PRId32 "%c", corrected.channel_nv[j], j == 7 ? '\n' : ',');
    }
    free(parsed); free(session.p); free(firmware.p); free(model.p); free(input.p);
    int status;
    if (e) status = diagnostic("input or apply", e, 2);
    else status = publish(a.output, &output);
    free(storage);
    if (status) return status;
    errno = 0;
    if (printf("{\"operation\":\"apply-calibration\",\"frame_count\":%zu,\"physical_acceptance\":false,\"source_kind_declared\":true}\n", count) < 0 || fflush(stdout) == EOF || ferror(stdout))
        return diagnostic("stdout summary (data retained)", errno ? errno : EIO, 3);
    return 0;
}
static int command(int argc, char **argv)
{
    if (argc >= 2 && !strcmp(argv[1], "apply-calibration")) return apply_command(argc, argv);
    if (argc < 2 || strcmp(argv[1], "calibrate")) return diagnostic("command", EINVAL, 2);
    struct args a = {0};
    int e = arguments(argc, argv, 1, &a);
    if (e) return diagnostic("arguments", e, 2);
    struct bytes session = {0}, firmware = {0}, input = {0};
    struct bci_calibration_options options = {0};
    struct bci_calibration_coefficients coeff = {0};
    struct bci_calibration_point points[64];
    int32_t parsed[64 * 16];
    uint32_t spans[8][2] = {{0}};
    char storage[8192], hash[65];
    struct text output = {storage, 0, sizeof(storage)};
    size_t count = 0;
    e = load(a.session, 1048576, &session);
    if (!e) e = load(a.firmware, 1048576, &firmware);
    if (!e) e = identity(&a, &session, &firmware, &options.identity);
    if (!e) e = load(a.points, 1048576, &input);
    if (!e) e = csv(&input, 16, 3, 64, parsed, &count);
    if (!e) {
        for (size_t i = 0; i < count; ++i)
            for (unsigned c = 0; c < 8; ++c) {
                points[i].observed_nv[c] = parsed[i*16+c];
                points[i].reference_nv[c] = parsed[i*16+8+c];
            }
        options.selected_mask = a.mask;
        options.min_span_nv = a.span;
        options.max_residual_nv = a.residual;
        e = -bci_calibration_fit(points, count, &options, &coeff);
        for (unsigned c = 0; c < 8; ++c)
            for (unsigned j = 0; j < 2; ++j) {
                int32_t lo = INT32_MAX, hi = INT32_MIN;
                for (size_t i = 0; i < count; ++i) {
                    int32_t v = parsed[i * 16 + j * 8 + c];
                    if (v < lo) lo = v;
                    if (v > hi) hi = v;
                }
                spans[c][j] = (uint32_t)((int64_t)hi - lo);
            }
    }
    if (!e) {
        bci_sha256_hex(input.p, input.n, hash);
        e = serialize(&coeff, hash, spans, &output);
    }
    free(session.p); free(firmware.p); free(input.p);
    if (e) return diagnostic("input or calibration", e, 2);
    int status = publish(a.output, &output);
    if (status) return status;
    errno = 0;
    if (printf("{\"operation\":\"calibrate\",\"point_count\":%zu,\"physical_acceptance\":false,\"source_kind_declared\":true}\n", count) < 0 || fflush(stdout) == EOF || ferror(stdout))
        return diagnostic("stdout summary (data retained)", errno ? errno : EIO, 3);
    return 0;
}
int bci_calibration_cli_main(int argc, char **argv)
{
    locale_t loc = newlocale(LC_NUMERIC_MASK, "C", (locale_t)0);
    if (!loc) return diagnostic("C numeric locale", errno, 3);
    locale_t previous = uselocale(loc);
    if (!previous) { int e = errno; freelocale(loc); return diagnostic("numeric locale", e, 3); }
    struct sigaction ignore = {0}, old;
    ignore.sa_handler = SIG_IGN;
    sigemptyset(&ignore.sa_mask);
    if (sigaction(SIGPIPE, &ignore, &old) < 0) {
        int e = errno; uselocale(previous); freelocale(loc); return diagnostic("SIGPIPE", e, 3);
    }
    int status = command(argc, argv);
    if (sigaction(SIGPIPE, &old, NULL) < 0) status = diagnostic("restore SIGPIPE", errno, 3);
    if (!uselocale(previous)) status = diagnostic("restore locale", errno, 3);
    else freelocale(loc);
    return status;
}
