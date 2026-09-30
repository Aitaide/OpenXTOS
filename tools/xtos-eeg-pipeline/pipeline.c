#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "xtos/spsc.h"
#include "xtos/signal.h"

#define HEADER "# xtos-eeg-v1,channels=2,sample_rate_hz=250,unit=nanovolt"
#define FILTERED_HEADER "# xtos-filtered-v1,channels=2,sample_rate_hz=250,unit=nanovolt"
#define CHANNELS 2U
#define INTERVAL_TOLERANCE 0.01

struct pipeline_stats {
    uint64_t frames_in;
    uint64_t frames_out;
    uint64_t malformed;
    uint64_t interval_anomalies;
    uint64_t timestamp_anomalies;
    uint64_t *latencies_ns;
    size_t latency_count;
};

static uint64_t now_ns(void)
{
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC_RAW, &value) != 0)
        return 0;
    return (uint64_t)value.tv_sec * 1000000000ULL + (uint64_t)value.tv_nsec;
}

static int add_latency(struct pipeline_stats *stats, uint64_t value)
{
    uint64_t *new_values;
    size_t new_capacity;

    new_capacity = stats->latency_count + 1;
    new_values = realloc(stats->latencies_ns, new_capacity * sizeof(*new_values));
    if (new_values == NULL)
        return -1;
    stats->latencies_ns = new_values;
    stats->latencies_ns[stats->latency_count++] = value;
    return 0;
}

static int compare_u64(const void *left, const void *right)
{
    uint64_t a = *(const uint64_t *)left;
    uint64_t b = *(const uint64_t *)right;
    return a > b ? 1 : (a < b ? -1 : 0);
}

static uint64_t percentile(const struct pipeline_stats *stats, double fraction)
{
    size_t index;
    if (stats->latency_count == 0)
        return 0;
    index = (size_t)ceil(fraction * (double)stats->latency_count) - 1;
    if (index >= stats->latency_count)
        index = stats->latency_count - 1;
    return stats->latencies_ns[index];
}

static int parse_row(const char *line, uint64_t *timestamp_ns, int32_t samples[CHANNELS])
{
    unsigned long long timestamp;
    double first;
    double second;
    char extra;
    int fields;

    fields = sscanf(line, " %llu , %lf , %lf %c", &timestamp, &first, &second, &extra);
    if (fields != 3 || !isfinite(first) || !isfinite(second) ||
        first < (double)INT32_MIN || first > (double)INT32_MAX ||
        second < (double)INT32_MIN || second > (double)INT32_MAX)
        return -1;
    *timestamp_ns = (uint64_t)timestamp;
    samples[0] = (int32_t)llround(first);
    samples[1] = (int32_t)llround(second);
    return 0;
}

static int parse_options(int argc, char **argv, double *low_cut_hz,
                         double *high_cut_hz, const char **input_path,
                         const char **filtered_path)
{
    int index = 1;

    *low_cut_hz = 0.0;
    *high_cut_hz = 0.0;
    *filtered_path = NULL;
    if (index >= argc || strcmp(argv[index], "--input") != 0 || index + 1 >= argc)
        return -1;
    *input_path = argv[index + 1];
    index += 2;
    while (index < argc) {
        char *end;
        if (strcmp(argv[index], "--bandpass") == 0 && index + 2 < argc) {
            *low_cut_hz = strtod(argv[index + 1], &end);
            if (*end != '\0')
                return -1;
            *high_cut_hz = strtod(argv[index + 2], &end);
            if (*end != '\0' || !isfinite(*low_cut_hz) || !isfinite(*high_cut_hz))
                return -1;
            index += 3;
        } else if (strcmp(argv[index], "--dump-filtered") == 0 && index + 1 < argc) {
            *filtered_path = argv[index + 1];
            index += 2;
        } else {
            return -1;
        }
    }
    if ((*low_cut_hz == 0.0) != (*high_cut_hz == 0.0))
        return -1;
    if (*low_cut_hz == 0.0 && *filtered_path != NULL)
        return -1;
    if (*low_cut_hz != 0.0 && *filtered_path == NULL)
        return -1;
    return 0;
}

static int write_filtered(FILE *output, uint64_t timestamp_ns,
                          const double samples[CHANNELS])
{
    return fprintf(output, "%" PRIu64 ",%.17g,%.17g\n", timestamp_ns,
                   samples[0], samples[1]) < 0 ? -1 : 0;
}

static void print_summary(const struct pipeline_stats *stats,
                          const struct xtos_spsc_stats *queue_stats)
{
    uint64_t *sorted = NULL;
    size_t bytes = stats->latency_count * sizeof(*sorted);
    if (bytes != 0) {
        sorted = malloc(bytes);
        if (sorted != NULL) {
            memcpy(sorted, stats->latencies_ns, bytes);
            qsort(sorted, stats->latency_count, sizeof(*sorted), compare_u64);
        }
    }
    printf("{\"status\":\"%s\",\"frames_in\":%" PRIu64
           ",\"frames_out\":%" PRIu64
           ",\"interval_anomalies\":%" PRIu64
           ",\"timestamp_anomalies\":%" PRIu64
           ",\"rejected_full\":%" PRIu64
           ",\"p50_latency_ns\":%" PRIu64
           ",\"p95_latency_ns\":%" PRIu64
           ",\"p99_latency_ns\":%" PRIu64 "}\n",
           (stats->malformed || stats->interval_anomalies ||
            stats->timestamp_anomalies || queue_stats->rejected_full) ? "degraded" : "ok",
           stats->frames_in, stats->frames_out, stats->interval_anomalies,
           stats->timestamp_anomalies, queue_stats->rejected_full,
           sorted ? percentile(&(struct pipeline_stats){
               .latencies_ns = sorted, .latency_count = stats->latency_count}, 0.50) : 0,
           sorted ? percentile(&(struct pipeline_stats){
               .latencies_ns = sorted, .latency_count = stats->latency_count}, 0.95) : 0,
           sorted ? percentile(&(struct pipeline_stats){
               .latencies_ns = sorted, .latency_count = stats->latency_count}, 0.99) : 0);
    free(sorted);
}

int main(int argc, char **argv)
{
    FILE *input;
    FILE *filtered_output = NULL;
    char *line = NULL;
    const char *input_path;
    const char *filtered_path;
    double low_cut_hz;
    double high_cut_hz;
    size_t line_capacity = 0;
    ssize_t line_length;
    uint64_t previous_timestamp = 0;
    uint64_t sequence = 0;
    uint64_t expected_interval = 4000000ULL;
    struct pipeline_stats stats = {0};
    struct xtos_spsc_stats queue_stats;
    struct xtos_spsc_queue *queue;
    size_t capacity = 16;
    int32_t input_samples[CHANNELS];
    int32_t output_samples[CHANNELS];
    struct xtos_bandpass_filter filters[CHANNELS];
    double filtered_samples[CHANNELS];
    int filter_enabled = 0;
    int exit_code = 0;

    if (parse_options(argc, argv, &low_cut_hz, &high_cut_hz,
                      &input_path, &filtered_path) != 0) {
        fprintf(stderr, "usage: %s --input FILE [--bandpass LOW HIGH --dump-filtered FILE]\n",
                argv[0]);
        return 2;
    }
    filter_enabled = low_cut_hz != 0.0;
    if (filter_enabled) {
        size_t index;
        for (index = 0; index < CHANNELS; index++) {
            if (xtos_bandpass_init(&filters[index], 250.0, low_cut_hz,
                                   high_cut_hz) != 0) {
                fprintf(stderr, "error: invalid bandpass configuration\n");
                return 2;
            }
        }
    }
    input = fopen(input_path, "r");
    if (input == NULL) {
        fprintf(stderr, "error: cannot open %s: %s\n", input_path, strerror(errno));
        return 2;
    }
    if (filter_enabled) {
        filtered_output = fopen(filtered_path, "w");
        if (filtered_output == NULL) {
            fprintf(stderr, "error: cannot create %s: %s\n", filtered_path, strerror(errno));
            fclose(input);
            return 2;
        }
        fprintf(filtered_output, "%s\n", FILTERED_HEADER);
    }
    line_length = getline(&line, &line_capacity, input);
    if (line_length < 0 || strncmp(line, HEADER, strlen(HEADER)) != 0) {
        fprintf(stderr, "error: invalid EEG header\n");
        fclose(input);
        free(line);
        return 2;
    }
    queue = xtos_spsc_create(capacity, CHANNELS);
    if (queue == NULL) {
        fprintf(stderr, "error: queue allocation failed: %s\n", strerror(errno));
        fclose(input);
        free(line);
        return 2;
    }
    while ((line_length = getline(&line, &line_capacity, input)) >= 0) {
        struct xtos_sample_frame frame;
        struct xtos_sample_frame output;
        uint64_t timestamp_ns;
        uint64_t start_ns;
        uint64_t end_ns;
        uint64_t interval;
        int result;

        if (line_length == 0 || line[0] == '\n')
            continue;
        if (parse_row(line, &timestamp_ns, input_samples) != 0) {
            stats.malformed++;
            exit_code = 2;
            fprintf(stderr, "error: malformed data row\n");
            break;
        }
        if (previous_timestamp != 0) {
            if (timestamp_ns <= previous_timestamp) {
                stats.timestamp_anomalies++;
                fprintf(stderr, "error: timestamp is not strictly increasing\n");
                exit_code = 3;
                break;
            }
            interval = timestamp_ns - previous_timestamp;
            if (interval < expected_interval * (1.0 - INTERVAL_TOLERANCE) ||
                interval > expected_interval * (1.0 + INTERVAL_TOLERANCE)) {
                stats.interval_anomalies++;
                exit_code = 3;
            }
        }
        frame.timestamp_ns = timestamp_ns;
        frame.sequence = sequence++;
        frame.channels = CHANNELS;
        frame.sample_rate_hz = 250;
        frame.quality_flags = 0;
        frame.samples_nv = input_samples;
        start_ns = now_ns();
        result = xtos_spsc_push(queue, &frame);
        if (result != 0) {
            fprintf(stderr, "error: queue push failed: %s\n", strerror(errno));
            exit_code = 3;
            break;
        }
        result = xtos_spsc_pop(queue, &output, output_samples, CHANNELS);
        end_ns = now_ns();
        if (result != 0 || output.sequence != frame.sequence ||
            output_samples[0] != input_samples[0] || output_samples[1] != input_samples[1]) {
            fprintf(stderr, "error: queue data integrity failure\n");
            exit_code = 3;
            break;
        }
        if (add_latency(&stats, end_ns - start_ns) != 0) {
            fprintf(stderr, "error: latency allocation failed\n");
            exit_code = 2;
            break;
        }
        if (filter_enabled) {
            size_t index;
            for (index = 0; index < CHANNELS; index++) {
                double input_value = (double)output_samples[index];
                if (xtos_bandpass_process(&filters[index], &input_value,
                                          &filtered_samples[index], 1) != 0) {
                    fprintf(stderr, "error: bandpass processing failed\n");
                    exit_code = 3;
                    break;
                }
            }
            if (exit_code != 0 || write_filtered(filtered_output, timestamp_ns,
                                                  filtered_samples) != 0) {
                fprintf(stderr, "error: filtered output failed\n");
                exit_code = 2;
                break;
            }
        }
        stats.frames_in++;
        stats.frames_out++;
        previous_timestamp = timestamp_ns;
    }
    fclose(input);
    if (filtered_output != NULL)
        fclose(filtered_output);
    free(line);
    xtos_spsc_get_stats(queue, &queue_stats);
    if (stats.frames_in == 0 && exit_code == 0)
        exit_code = 2;
    print_summary(&stats, &queue_stats);
    free(stats.latencies_ns);
    xtos_spsc_destroy(queue);
    return exit_code;
}
