/* SPDX-License-Identifier: GPL-2.0 */
#define _POSIX_C_SOURCE 200809L
#include "bci_diagnostic_cli.h"
#include "bci_diagnostic_options.h"
#include "bci_diagnostic.h"
#include "cyton_diagnostic_serial.h"
#include <fcntl.h>
#include <inttypes.h>
#include <locale.h>
#include <stdio.h>
#include <sys/stat.h>
#include <unistd.h>

static volatile sig_atomic_t stop_requested;
static void request_stop(int signal_number)
{
    (void)signal_number;
    stop_requested = 1;
}

static void string_json(FILE *f, const char *s)
{
    const unsigned char *p = (const unsigned char *)s;
    fputc('"', f);
    for (; *p; ++p) {
        if (*p == '"' || *p == '\\') { fputc('\\', f); fputc(*p, f); }
        else if (*p < 32 || *p >= 127) fprintf(f, "\\u%04x", *p);
        else fputc(*p, f);
    }
    fputc('"', f);
}
static void config_json(FILE *f, const struct xtos_cyton_config *c)
{
    unsigned int i;
    fprintf(f, "{\"register_mask\":%u,\"enabled_mask\":%u,\"gains\":[", c->register_mask, c->enabled_mask);
    for (i = 0; i < 8; ++i) fprintf(f, "%s%u", i ? "," : "", c->gain[i]);
    fputs("],\"registers\":[", f);
    for (i = 0; i < 24; ++i) fprintf(f, "%s%u", i ? "," : "", c->registers[i]);
    fputs("]}", f);
}
static const char *mode_name(enum bci_diagnostic_cli_mode m)
{
    return m == BCI_DIAGNOSTIC_CLI_SHORTED ? "shorted" :
        m == BCI_DIAGNOSTIC_CLI_IMPEDANCE_P ? "impedance-p" : "impedance-n";
}
static const char *restore_name(enum cyton_diagnostic_restoration r)
{
    switch (r) {
    case CYTON_DIAGNOSTIC_RESTORED: return "restored";
    case CYTON_DIAGNOSTIC_RESTORE_FAILED: return "failed";
    case CYTON_DIAGNOSTIC_RESTORE_UNKNOWN: return "unknown";
    default: return "not_attempted";
    }
}
static void report_json(FILE *f, const struct bci_diagnostic_cli_options *o,
                        const struct cyton_diagnostic_result *r,
                        const struct bci_diagnostic_channel_result *s, int valid)
{
    unsigned int i;
    fprintf(f, "{\"schema\":\"xtos.bci.diagnostic\",\"version\":1,\"mode\":\"%s\",\"channel\":%u,\"input\":", mode_name(o->mode), o->channel + 1);
    if (o->mode == BCI_DIAGNOSTIC_CLI_SHORTED) fputs("null", f);
    else fprintf(f, "\"%c\"", o->mode == BCI_DIAGNOSTIC_CLI_IMPEDANCE_P ? 'P' : 'N');
    fputs(",\"firmware\":", f); string_json(f, r->firmware);
    fprintf(f, ",\"result_valid\":%s,\"calibration\":false,\"physical_acceptance\":false,\"actual_gain\":%u,\"before\":", valid ? "true" : "false", r->actual_gain);
    config_json(f, &r->before); fputs(",\"test\":", f); config_json(f, &r->test);
    fputs(",\"after\":", f); config_json(f, &r->after);
    fprintf(f, ",\"frames_requested\":%u,\"settle_frames_requested\":%u,\"frames_observed\":%u,\"settled_frames\":%u,\"sample_count\":%u,\"bad_frames\":%u,\"counter_errors\":%u,\"rail_errors\":%u,\"partial_command\":%u,\"error\":%d,\"cleanup_errno\":%d,\"restoration\":\"%s\",\"first_receive_ns\":%" PRId64 ",\"last_receive_ns\":%" PRId64 ",\"timestamp_clock\":\"host_monotonic\",\"timestamp_resolution_ns\":1000000,\"method\":\"%s\",\"assumptions\":{\"nominal_current_na\":%s,\"nominal_frequency_hz\":%s,\"series_resistance_ohm\":%s,\"current_tolerance_percent\":%s,\"gui_preprocessing\":false,\"input_isolation\":\"caller_responsibility\"},\"raw_nv\":[",
        o->frames, o->settle_frames, r->frames_observed, r->settled_frames, r->sample_count, r->bad_frames, r->counter_errors, r->rail_errors, r->partial_command, r->error, r->cleanup_error, restore_name(r->restoration), r->first_receive_ns, r->last_receive_ns,
        o->mode == BCI_DIAGNOSTIC_CLI_SHORTED ? "selected_population_stats" : "NOMINAL_CURRENT_RMS",
        o->mode == BCI_DIAGNOSTIC_CLI_SHORTED ? "null" : "6",
        o->mode == BCI_DIAGNOSTIC_CLI_SHORTED ? "null" : "31.25",
        o->mode == BCI_DIAGNOSTIC_CLI_SHORTED ? "null" : "2200",
        o->mode == BCI_DIAGNOSTIC_CLI_SHORTED ? "null" : "20");
    for (i = 0; i < r->sample_count; ++i) fprintf(f, "%s%" PRId32, i ? "," : "", r->samples_nv[i]);
    fputs("],\"stats\":", f);
    if (!valid) fputs("null", f);
    else {
        fprintf(f, "{\"count\":%zu,\"mean_nv\":%.17g,\"stddev_nv\":%.17g,\"observed_peak_nv\":%.17g,\"nominal_rms_estimate_ohm\":", s->count, s->mean_nv, s->stddev_nv, s->observed_peak_nv);
        if (s->estimate_present) fprintf(f, "%.17g,\"unclamped_estimate_ohm\":%.17g", s->nominal_rms_estimate_ohm, s->unclamped_estimate_ohm);
        else fputs("null,\"unclamped_estimate_ohm\":null", f);
        fputc('}', f);
    }
    fputs("}\n", f);
}
int bci_diagnostic_cli_main(int argc, char **argv)
{
    struct bci_diagnostic_cli_options o;
    struct cyton_diagnostic_options capture = {0};
    struct cyton_diagnostic_result r;
    struct bci_diagnostic_options numeric = {0};
    struct bci_diagnostic_result result;
    struct bci_diagnostic_frame_nv *frames;
    locale_t locale, previous;
    struct stat st;
    FILE *file;
    int fd, rc, valid, output_error = 0, installed = 0;
    struct sigaction action = {0}, old[3];
    const int signals[3] = { SIGINT, SIGTERM, SIGPIPE };
    unsigned int i;
    if (bci_diagnostic_options_parse(argc, argv, &o) || o.timeout_ms > 3000) {
        fputs("diagnose: invalid arguments\n", stderr); return 2;
    }
    fd = open(o.output, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK, 0600);
    if (fd < 0) { perror("diagnose: output setup"); return 2; }
    if (fstat(fd, &st) || !S_ISREG(st.st_mode)) {
        close(fd); fputs("diagnose: output is not a regular file\n", stderr); return 2;
    }
    file = fdopen(fd, "w");
    if (!file) { close(fd); perror("diagnose: output setup"); return 2; }
    frames = calloc(o.frames, sizeof(*frames));
    locale = newlocale(LC_NUMERIC_MASK, "C", (locale_t)0);
    if (!frames || !locale) {
        free(frames); if (locale) freelocale(locale); fclose(file);
        fputs("diagnose: allocation setup failed\n", stderr); return 2;
    }
    previous = uselocale(locale);
    if (!previous) {
        freelocale(locale); free(frames); fclose(file);
        fputs("diagnose: locale setup failed\n", stderr); return 2;
    }
    stop_requested = 0;
    sigemptyset(&action.sa_mask);
    for (i = 0; i < 3; ++i) {
        action.sa_handler = i == 2 ? SIG_IGN : request_stop;
        if (sigaction(signals[i], &action, &old[i])) break;
        ++installed;
    }
    if (installed != 3) {
        while (installed) { --installed; sigaction(signals[installed], &old[installed], NULL); }
        uselocale(previous); freelocale(locale); free(frames); fclose(file);
        fputs("diagnose: signal setup failed\n", stderr); return 2;
    }
    capture.stop = &stop_requested;
    capture.path = o.device;
    capture.mode = o.mode == BCI_DIAGNOSTIC_CLI_SHORTED ? CYTON_DIAGNOSTIC_SHORTED : CYTON_DIAGNOSTIC_IMPEDANCE;
    capture.channel = o.channel;
    capture.input = o.mode == BCI_DIAGNOSTIC_CLI_IMPEDANCE_N ? 'N' : 'P';
    capture.settle_frames = o.settle_frames; capture.sample_frames = o.frames;
    capture.command_timeout_ms = o.timeout_ms;
    capture.operation_timeout_ms = 3000 + 4 * (o.frames + o.settle_frames) + 32 * o.timeout_ms;
    if (capture.operation_timeout_ms > 120000) capture.operation_timeout_ms = 120000;
    rc = cyton_diagnostic_capture(&capture, &r);
    if (stop_requested && !r.error) r.error = ECANCELED;
    valid = !rc && !r.error && r.restoration == CYTON_DIAGNOSTIC_RESTORED && r.sample_count == o.frames;
    if (valid) {
        for (i = 0; i < r.sample_count; ++i) frames[i].channel_nv[o.channel] = r.samples_nv[i];
        numeric.mode = o.mode == BCI_DIAGNOSTIC_CLI_SHORTED ? BCI_DIAGNOSTIC_SHORTED : BCI_DIAGNOSTIC_NOMINAL_RMS_IMPEDANCE;
        numeric.method = o.mode == BCI_DIAGNOSTIC_CLI_SHORTED ? BCI_DIAGNOSTIC_POPULATION_STATS : BCI_DIAGNOSTIC_NOMINAL_CURRENT_RMS;
        if (o.mode != BCI_DIAGNOSTIC_CLI_SHORTED) {
            numeric.selected_channel = o.channel;
            numeric.source_assumption = BCI_DIAGNOSTIC_SOURCE_CYTON_NOMINAL_6NA;
            numeric.nominal_current_na = 6; numeric.series_resistance_ohm = 2200;
        }
        if (bci_diagnostic_analyze(frames, r.sample_count, &numeric, &result)) { valid = 0; r.error = EINVAL; }
    }
    report_json(file, &o, &r, valid ? &result.channels[o.channel] : NULL, valid);
    if (ferror(file) || fflush(file)) output_error = 1;
    if (fclose(file)) output_error = 1;
    if (output_error) fputs("diagnose: report output failed\n", stderr);
    fprintf(stdout, "{\"result_valid\":%s,\"sample_count\":%u,\"restoration\":\"%s\"}\n", valid && !output_error ? "true" : "false", r.sample_count, restore_name(r.restoration));
    if (ferror(stdout) || fflush(stdout)) { output_error = 1; fputs("diagnose: summary output failed\n", stderr); }
    uselocale(previous); freelocale(locale); free(frames);
    while (installed) {
        --installed;
        if (sigaction(signals[installed], &old[installed], NULL)) {
            output_error = 1; fputs("diagnose: signal restoration failed\n", stderr);
        }
    }
    return valid && !output_error && !stop_requested ? 0 : 3;
}
