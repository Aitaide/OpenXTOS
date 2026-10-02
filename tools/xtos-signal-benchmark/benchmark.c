#define _GNU_SOURCE

#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

#include "xtos/signal.h"

#define XTOS_PI 3.141592653589793238462643383279502884
#define DEFAULT_ITERATIONS 1000U
#define WINDOW_LENGTH 256U
#define SAMPLE_RATE_HZ 256U
#define CHANNELS 2U
#define LOW_CUT_HZ 5.0
#define HIGH_CUT_HZ 40.0
#define TARGET_FREQUENCY_HZ 10.0
#define HARMONIC_COUNT 2U
#define NEIGHBOR_BINS 1U
#define P300_BASELINE_SAMPLES 32U
#define P300_WINDOW_START_MS 250U
#define P300_WINDOW_END_MS 500U
#define P300_Z_THRESHOLD 4.0

struct samples {
    double values[CHANNELS][WINDOW_LENGTH];
};

struct timing_samples {
    uint64_t *values_ns;
    size_t count;
};

static uint64_t monotonic_ns(void)
{
    struct timespec value;

    if (clock_gettime(CLOCK_MONOTONIC_RAW, &value) != 0)
        return 0;
    return (uint64_t)value.tv_sec * 1000000000ULL + (uint64_t)value.tv_nsec;
}

static int compare_u64(const void *left, const void *right)
{
    uint64_t a = *(const uint64_t *)left;
    uint64_t b = *(const uint64_t *)right;

    return a > b ? 1 : (a < b ? -1 : 0);
}

static uint64_t percentile(const uint64_t *sorted, size_t count, double fraction)
{
    size_t index;

    if (count == 0)
        return 0;
    index = (size_t)ceil(fraction * (double)count) - 1;
    if (index >= count)
        index = count - 1;
    return sorted[index];
}

static int record_duration(struct timing_samples *samples, uint64_t duration_ns)
{
    uint64_t *new_values;

    new_values = realloc(samples->values_ns,
                          (samples->count + 1) * sizeof(*new_values));
    if (new_values == NULL)
        return -1;
    samples->values_ns = new_values;
    samples->values_ns[samples->count++] = duration_ns;
    return 0;
}

static void initialize_samples(struct samples *samples)
{
    size_t channel;
    size_t index;

    for (channel = 0; channel < CHANNELS; channel++) {
        for (index = 0; index < WINDOW_LENGTH; index++) {
            double phase = 2.0 * XTOS_PI * (double)index / WINDOW_LENGTH;
            samples->values[channel][index] =
                sin(10.0 * phase) + 0.5 * sin(20.0 * phase) +
                0.25 * sin(9.0 * phase) + 0.1 * (double)channel;
            if (channel == 0 && index == 128)
                samples->values[channel][index] += 5.0;
        }
    }
}

static int run_fft(const struct samples *samples)
{
    double power[WINDOW_LENGTH / 2 + 1];

    return xtos_signal_fft_power(samples->values[0], WINDOW_LENGTH,
                                 power, sizeof(power) / sizeof(power[0]));
}

static int run_bandpass(const struct samples *samples)
{
    struct xtos_bandpass_filter filter;
    double output[WINDOW_LENGTH];

    if (xtos_bandpass_init(&filter, SAMPLE_RATE_HZ, LOW_CUT_HZ,
                           HIGH_CUT_HZ) != 0)
        return -1;
    return xtos_bandpass_process(&filter, samples->values[0], output,
                                 WINDOW_LENGTH);
}

static int run_ssvep(const struct samples *samples)
{
    struct xtos_ssvep_feature feature;

    return xtos_signal_ssvep_feature(samples->values[0], WINDOW_LENGTH,
                                     SAMPLE_RATE_HZ, TARGET_FREQUENCY_HZ,
                                     HARMONIC_COUNT, NEIGHBOR_BINS, &feature);
}

static int run_p300(const struct samples *samples)
{
    struct xtos_p300_feature feature;

    if (xtos_signal_p300_feature(samples->values[0], WINDOW_LENGTH,
                                  SAMPLE_RATE_HZ, P300_BASELINE_SAMPLES,
                                  P300_WINDOW_START_MS, P300_WINDOW_END_MS,
                                  P300_Z_THRESHOLD, &feature) != 0)
        return -1;
    return feature.detected ? 0 : -1;
}

static int benchmark_operation(const char *name,
                               unsigned int iterations,
                               const struct samples *samples,
                               int (*operation)(const struct samples *),
                               FILE *output)
{
    struct timing_samples timings = {0};
    struct rusage usage_before;
    struct rusage usage_after;
    uint64_t total_start;
    uint64_t total_end;
    uint64_t *sorted;
    unsigned int iteration;
    int result = 0;

    if (getrusage(RUSAGE_SELF, &usage_before) != 0)
        return -1;
    total_start = monotonic_ns();
    for (iteration = 0; iteration < iterations; iteration++) {
        uint64_t start = monotonic_ns();
        result = operation(samples);
        if (result != 0 || record_duration(&timings,
                                           monotonic_ns() - start) != 0)
            break;
    }
    total_end = monotonic_ns();
    if (getrusage(RUSAGE_SELF, &usage_after) != 0 || result != 0 ||
        timings.count != iterations) {
        free(timings.values_ns);
        return -1;
    }
    sorted = malloc(timings.count * sizeof(*sorted));
    if (sorted == NULL) {
        free(timings.values_ns);
        return -1;
    }
    memcpy(sorted, timings.values_ns, timings.count * sizeof(*sorted));
    qsort(sorted, timings.count, sizeof(*sorted), compare_u64);
    fprintf(output,
            "    \"%s\": {\"p50_ns\": %" PRIu64
            ", \"p95_ns\": %" PRIu64 ", \"p99_ns\": %" PRIu64
            ", \"max_ns\": %" PRIu64 ", \"total_wall_ns\": %" PRIu64
            ", \"user_cpu_us\": %lld, \"system_cpu_us\": %lld"
            ", \"peak_rss_kb\": %ld},\n",
            name, percentile(sorted, timings.count, 0.50),
            percentile(sorted, timings.count, 0.95),
            percentile(sorted, timings.count, 0.99), sorted[timings.count - 1],
            total_end - total_start,
            (long long)(usage_after.ru_utime.tv_sec - usage_before.ru_utime.tv_sec) * 1000000LL +
                (usage_after.ru_utime.tv_usec - usage_before.ru_utime.tv_usec),
            (long long)(usage_after.ru_stime.tv_sec - usage_before.ru_stime.tv_sec) * 1000000LL +
                (usage_after.ru_stime.tv_usec - usage_before.ru_stime.tv_usec),
            usage_after.ru_maxrss);
    free(sorted);
    free(timings.values_ns);
    return 0;
}

int main(int argc, char **argv)
{
    struct samples samples;
    unsigned int iterations = DEFAULT_ITERATIONS;
    const char *output_path = NULL;
    FILE *output;
    char *end;
    int index;

    for (index = 1; index < argc; index++) {
        if (strcmp(argv[index], "--iterations") == 0 && index + 1 < argc) {
            unsigned long parsed = strtoul(argv[++index], &end, 10);
            if (*end != '\0' || parsed == 0 || parsed > UINT32_MAX) {
                fprintf(stderr, "error: invalid iterations\n");
                return 2;
            }
            iterations = (unsigned int)parsed;
        } else if (strcmp(argv[index], "--output") == 0 && index + 1 < argc) {
            output_path = argv[++index];
        } else {
            fprintf(stderr, "usage: %s [--iterations N] --output FILE\n", argv[0]);
            return 2;
        }
    }
    if (output_path == NULL) {
        fprintf(stderr, "usage: %s [--iterations N] --output FILE\n", argv[0]);
        return 2;
    }
    output = fopen(output_path, "w");
    if (output == NULL) {
        fprintf(stderr, "error: cannot create %s: %s\n", output_path, strerror(errno));
        return 2;
    }
    initialize_samples(&samples);
    fprintf(output,
            "{\n  \"benchmark\": \"xtos-signal-baseline-v2\",\n"
            "  \"window_length\": %u,\n  \"sample_rate_hz\": %u,\n"
            "  \"channels\": %u,\n  \"iterations\": %u,\n"
            "  \"threads\": 1,\n  \"compiler\": \"%s\",\n"
            "  \"compiler_flags\": \"%s\",\n  \"operations\": {\n",
            WINDOW_LENGTH, SAMPLE_RATE_HZ, CHANNELS, iterations,
            __VERSION__, "-std=c11 -Wall -Wextra -Werror -O2");
    if (benchmark_operation("fft_power", iterations, &samples, run_fft, output) != 0 ||
        benchmark_operation("bandpass", iterations, &samples, run_bandpass, output) != 0 ||
        benchmark_operation("ssvep_feature", iterations, &samples, run_ssvep, output) != 0 ||
        benchmark_operation("p300_feature", iterations, &samples, run_p300, output) != 0) {
        fclose(output);
        return 1;
    }
    fseek(output, -2, SEEK_CUR);
    fputs("\n  }\n}\n", output);
    fclose(output);
    return 0;
}
