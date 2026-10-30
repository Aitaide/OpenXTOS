// SPDX-License-Identifier: Apache-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/sched/types.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

struct sample {
    uint64_t id;
    uint64_t release_ns;
    uint64_t wake_ns;
    uint64_t finish_ns;
    uint64_t work_ns;
    uint64_t cpu_work_ns;
    int cpu;
    int warmup;
};

static uint64_t clock_ns(clockid_t clock_id)
{
    struct timespec ts;
    if (clock_gettime(clock_id, &ts) != 0) {
        perror("clock_gettime");
        exit(2);
    }
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static int sleep_until(uint64_t target_ns)
{
    struct timespec ts = {
        .tv_sec = (time_t)(target_ns / 1000000000ULL),
        .tv_nsec = (long)(target_ns % 1000000000ULL),
    };
    int rc;
    do {
        rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL);
    } while (rc == EINTR);
    if (rc != 0) {
        errno = rc;
        perror("clock_nanosleep");
        return -1;
    }
    return 0;
}

static uint64_t burn_for_cpu_ns(uint64_t duration_ns)
{
    uint64_t start = clock_ns(CLOCK_THREAD_CPUTIME_ID);
    uint64_t end = start + duration_ns;
    volatile uint64_t value = 1;
    while (clock_ns(CLOCK_THREAD_CPUTIME_ID) < end) {
        for (unsigned i = 0; i < 64; ++i)
            value = value * 2862933555777941757ULL + 3037000493ULL;
    }
    (void)value;
    return clock_ns(CLOCK_THREAD_CPUTIME_ID) - start;
}

static int parse_u64(const char *text, uint64_t *value)
{
    char *end = NULL;
    unsigned long long parsed;
    if (!text[0])
        return -1;
    for (const char *p = text; *p; p++)
        if (*p < '0' || *p > '9')
            return -1;
    errno = 0;
    parsed = strtoull(text, &end, 10);
    if (errno || !end || *end || end == text)
        return -1;
    *value = (uint64_t)parsed;
    return 0;
}

static int requested_policy(const char *name)
{
    if (strcmp(name, "other") == 0)
        return SCHED_OTHER;
    if (strcmp(name, "fifo") == 0)
        return SCHED_FIFO;
    if (strcmp(name, "rr") == 0)
        return SCHED_RR;
    if (strcmp(name, "deadline") == 0)
        return SCHED_DEADLINE;
    return -1;
}

static int apply_policy(const char *name, int priority, uint64_t runtime,
                        uint64_t deadline, uint64_t period)
{
    int policy = requested_policy(name);
    if (policy < 0) {
        errno = EINVAL;
        return -1;
    }
    if (policy == SCHED_DEADLINE) {
        struct sched_attr attr = {
            .size = sizeof(attr),
            .sched_policy = SCHED_DEADLINE,
            .sched_runtime = runtime,
            .sched_deadline = deadline,
            .sched_period = period,
        };
        return (int)syscall(SYS_sched_setattr, 0, &attr, 0);
    }
    {
        struct sched_param param = { .sched_priority = policy == SCHED_OTHER ? 0 : priority };
        return sched_setscheduler(0, policy, &param);
    }
}

static void pin_cpu(int cpu)
{
    cpu_set_t set;
    cpu_set_t actual;
    CPU_ZERO(&actual);
    if (sched_getaffinity(0, sizeof(actual), &actual) != 0 || !CPU_ISSET(cpu, &actual)) {
        fprintf(stderr, "requested CPU is outside inherited affinity\n");
        exit(2);
    }
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set) != 0) {
        perror("sched_setaffinity");
        exit(2);
    }
    CPU_ZERO(&actual);
    if (sched_getaffinity(0, sizeof(actual), &actual) != 0 ||
        !CPU_ISSET(cpu, &actual) || CPU_COUNT(&actual) != 1) {
        fprintf(stderr, "CPU affinity readback mismatch\n");
        exit(2);
    }
}

int main(int argc, char **argv)
{
    const char *trace_path;
    const char *policy;
    char *priority_end = NULL;
    char *cpu_end = NULL;
    long priority;
    long cpu_long;
    uint64_t period, deadline, work, runtime, duration, warmup;
    int cpu;
    int requested_cpu;
    cpu_set_t effective_affinity;
    int expected_policy;
    int actual_policy;
    int actual_priority;
    struct sched_param actual_param;
    uint64_t run_ns;
    uint64_t jobs64;
    uint64_t base;
    uint64_t end;
    size_t capacity;
    size_t count = 0;
    struct sample *samples;
    FILE *out;

    if (argc != 11) {
        fprintf(stderr, "usage: %s TRACE POLICY PRIORITY PERIOD_NS DEADLINE_NS WORK_NS RUNTIME_NS DURATION_S CPU WARMUP_JOBS\n", argv[0]);
        return 2;
    }
    trace_path = argv[1];
    policy = argv[2];
    if (argv[3][0] < '0' || argv[3][0] > '9') {
        fprintf(stderr, "invalid priority token\n");
        return 2;
    }
    priority = strtol(argv[3], &priority_end, 10);
    if (!argv[3][0] || priority_end == argv[3] || !priority_end || *priority_end || priority < 0 || priority > 99 ||
        parse_u64(argv[4], &period) || parse_u64(argv[5], &deadline) ||
        parse_u64(argv[6], &work) || parse_u64(argv[7], &runtime) ||
        parse_u64(argv[8], &duration) || parse_u64(argv[10], &warmup) ||
        period == 0 || deadline == 0 || work == 0 || runtime == 0 || duration == 0 ||
        work > runtime || runtime > deadline || deadline > period ||
        warmup > 1000000 || duration > 3600) {
        fprintf(stderr, "invalid task contract or timing argument\n");
        return 2;
    }
    expected_policy = requested_policy(policy);
    if (expected_policy < 0 ||
        ((expected_policy == SCHED_OTHER || expected_policy == SCHED_DEADLINE) && priority != 0) ||
        ((expected_policy == SCHED_FIFO || expected_policy == SCHED_RR) && priority < 1)) {
        fprintf(stderr, "invalid scheduler policy\n");
        return 2;
    }
    if ((argv[9][0] < '0' || argv[9][0] > '9') && strcmp(argv[9], "-1") != 0) {
        fprintf(stderr, "invalid CPU token\n");
        return 2;
    }
    cpu_long = strtol(argv[9], &cpu_end, 10);
    if (!argv[9][0] || cpu_end == argv[9] || !cpu_end || *cpu_end || cpu_long < -1 || cpu_long >= CPU_SETSIZE ||
        (cpu_long == -1 && expected_policy != SCHED_DEADLINE)) {
        fprintf(stderr, "invalid CPU id\n");
        return 2;
    }
    cpu = (int)cpu_long;
    requested_cpu = cpu;
    if (expected_policy == SCHED_DEADLINE && (priority != 0 || runtime >= period)) {
        fprintf(stderr, "invalid SCHED_DEADLINE priority/runtime/period\n");
        return 2;
    }

    run_ns = duration * 1000000000ULL;
    if (run_ns / 1000000000ULL != duration || period > run_ns || work > 100000000ULL) {
        fprintf(stderr, "invalid duration, period, or work bound\n");
        return 2;
    }
    jobs64 = run_ns / period + (run_ns % period != 0);
    if (jobs64 + 2 > 200000 || jobs64 > SIZE_MAX / sizeof(*samples) || warmup >= jobs64) {
        fprintf(stderr, "requested job count or warmup exceeds trace bound\n");
        return 2;
    }
    if (cpu >= 0)
        pin_cpu(cpu);
    CPU_ZERO(&effective_affinity);
    if (sched_getaffinity(0, sizeof(effective_affinity), &effective_affinity) != 0) {
        perror("read effective affinity");
        return 2;
    }
    if (apply_policy(policy, (int)priority, runtime, deadline, period) != 0) {
        perror("set requested scheduler policy");
        return 2;
    }
    actual_policy = sched_getscheduler(0);
    if (actual_policy != expected_policy || sched_getparam(0, &actual_param) != 0) {
        fprintf(stderr, "scheduler policy readback mismatch\n");
        return 2;
    }
    actual_priority = actual_param.sched_priority;
    if ((actual_policy == SCHED_FIFO || actual_policy == SCHED_RR) && actual_priority != (int)priority) {
        fprintf(stderr, "real-time priority readback mismatch\n");
        return 2;
    }
    if (actual_policy == SCHED_DEADLINE) {
        struct sched_attr actual = { .size = sizeof(actual) };
        if (syscall(SYS_sched_getattr, 0, &actual, sizeof(actual), 0) != 0 ||
            actual.sched_runtime != runtime || actual.sched_deadline != deadline ||
            actual.sched_period != period) {
            fprintf(stderr, "SCHED_DEADLINE parameter readback mismatch\n");
            return 2;
        }
    }

    capacity = (size_t)jobs64;
    samples = calloc(capacity, sizeof(*samples));
    if (!samples) {
        perror("calloc");
        return 2;
    }

    base = clock_ns(CLOCK_MONOTONIC) + 50000000ULL;
    end = base + run_ns;
    while (base + count * period < end && count < capacity) {
        uint64_t release = base + count * period;
        uint64_t wake;
        uint64_t cpu_work;
        if (sleep_until(release) != 0) {
            free(samples);
            return 2;
        }
        wake = clock_ns(CLOCK_MONOTONIC);
        cpu_work = burn_for_cpu_ns(work);
        samples[count] = (struct sample){
            .id = count,
            .release_ns = release,
            .wake_ns = wake,
            .finish_ns = clock_ns(CLOCK_MONOTONIC),
            .work_ns = work,
            .cpu_work_ns = cpu_work,
            .cpu = sched_getcpu(),
            .warmup = count < warmup,
        };
        count++;
    }

    /* Do not perform report I/O at a real-time priority. */
    {
        struct sched_param normal = { .sched_priority = 0 };
        if (sched_setscheduler(0, SCHED_OTHER, &normal) != 0) {
            perror("restore normal scheduler before report I/O");
            free(samples);
            return 2;
        }
    }
    out = fopen(trace_path, "wx");
    if (!out) {
        perror("fopen trace");
        free(samples);
        return 2;
    }
    fprintf(out,
        "{\"type\":\"metadata\",\"schema\":\"xtos-sched-trace-v1\",\"policy\":\"%s\",\"actual_policy\":%d,\"actual_priority\":%d,\"period_ns\":%llu,\"deadline_ns\":%llu,\"work_ns\":%llu,\"runtime_ns\":%llu,\"duration_s\":%llu,\"cpu_requested\":%d,\"clock\":\"CLOCK_MONOTONIC\",\"warmup_jobs\":%llu,",
        policy, actual_policy, actual_priority, (unsigned long long)period,
        (unsigned long long)deadline, (unsigned long long)work,
        (unsigned long long)runtime, (unsigned long long)duration, requested_cpu, (unsigned long long)warmup);
    fprintf(out, "\"expected_jobs\":%zu,\"affinity_mode\":\"%s\",\"cpu_affinity\":[",
            capacity, requested_cpu < 0 ? "inherited" : "pinned");
    int separator = 0;
    for (int i = 0; i < CPU_SETSIZE; ++i) {
        if (CPU_ISSET(i, &effective_affinity)) {
            fprintf(out, "%s%d", separator ? "," : "", i);
            separator = 1;
        }
    }
    fprintf(out, "],\"wake_semantics\":\"first_userspace_timestamp_after_absolute_sleep\"}\n");
    for (size_t i = 0; i < count; ++i) {
        const struct sample *sample = &samples[i];
        fprintf(out,
            "{\"type\":\"job\",\"id\":%llu,\"release_ns\":%llu,\"wake_ns\":%llu,\"finish_ns\":%llu,\"deadline_ns\":%llu,\"work_ns\":%llu,\"cpu_work_ns\":%llu,\"cpu\":%d,\"warmup\":%s}\n",
            (unsigned long long)sample->id,
            (unsigned long long)sample->release_ns,
            (unsigned long long)sample->wake_ns,
            (unsigned long long)sample->finish_ns,
            (unsigned long long)(sample->release_ns + deadline),
            (unsigned long long)sample->work_ns,
            (unsigned long long)sample->cpu_work_ns,
            sample->cpu,
            sample->warmup ? "true" : "false");
    }
    int write_error = ferror(out);
    if (fclose(out) != 0 || write_error) {
        perror("write trace");
        free(samples);
        return 2;
    }
    free(samples);
    return 0;
}
