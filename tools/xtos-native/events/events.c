/* events: XTOS AR/VR event-source simulation (C, static).
 * Stage-4 item 5: "先模拟事件，再用真实OpenXR设备验证" — this is the
 * simulation half. Emits deterministic timestamped pose/button/axis events
 * on stdout (or a FIFO) in the same wire shape a real OpenXR-polling source
 * will use, so downstream consumers can be built and tested before hardware.
 *
 * Event format (one JSON object per line):
 *   {"t_ns":<monotonic ns>,"src":"sim","type":"pose"|"button"|"axis",
 *    "controller":0..3,"x":...,"y":...,"z":...,"w":...,
 *    "button":<id>,"down":0|1,"axis":<id>,"value":...}
 *
 * Determinism: --seed drives all values; wall clock is never mixed in except
 * the monotonic timestamp (t_ns), which consumers must treat as separate from
 * the deterministic payload (documented in README).
 */
#define _GNU_SOURCE
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* xorshift64*: deterministic, no libc rand state sharing. */
static uint64_t rng_state;
static uint64_t rng_next(void) {
    uint64_t x = rng_state;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    rng_state = x;
    return x * 0x2545F4914F6CDD1DULL;
}
static double rng_unit(void) { return (double)(rng_next() >> 11) / 9007199254740992.0; }

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static void emit_pose(uint64_t t, int controller) {
    double x = rng_unit() * 2.0 - 1.0, y = rng_unit() * 2.0 - 1.0, z = rng_unit() * 2.0 - 1.0;
    double w = rng_unit();
    printf("{\"t_ns\":%" PRIu64 ",\"src\":\"sim\",\"type\":\"pose\",\"controller\":%d,"
           "\"x\":%.6f,\"y\":%.6f,\"z\":%.6f,\"w\":%.6f}\n", t, controller, x, y, z, w);
}
static void emit_button(uint64_t t, int controller, int button, int down) {
    printf("{\"t_ns\":%" PRIu64 ",\"src\":\"sim\",\"type\":\"button\",\"controller\":%d,"
           "\"button\":%d,\"down\":%d}\n", t, controller, button, down);
}
static void emit_axis(uint64_t t, int controller, int axis) {
    double value = rng_unit() * 2.0 - 1.0;
    printf("{\"t_ns\":%" PRIu64 ",\"src\":\"sim\",\"type\":\"axis\",\"controller\":%d,"
           "\"axis\":%d,\"value\":%.6f}\n", t, controller, axis, value);
}

static void usage(void) {
    fprintf(stderr,
        "usage: events [--seed N] [--count N] [--interval-us N] [--controllers N]\n"
        "  Emits deterministic simulated AR/VR events, one JSON line each.\n"
        "  --seed          xorshift64* seed (default 1)\n"
        "  --count         number of events (default 100; 0 = unlimited)\n"
        "  --interval-us   microseconds between events (default 10000 = 100Hz)\n"
        "  --controllers   simulated controllers (default 2)\n");
}

int main(int argc, char **argv) {
    uint64_t seed = 1;
    long count = 100, interval_us = 10000;
    int controllers = 2;
    for (int i = 1; i < argc; i++) {
        const char *v = (i + 1 < argc) ? argv[i + 1] : NULL;
        if (!strcmp(argv[i], "--seed") && v) { seed = strtoull(v, NULL, 0); i++; }
        else if (!strcmp(argv[i], "--count") && v) { count = strtol(v, NULL, 0); i++; }
        else if (!strcmp(argv[i], "--interval-us") && v) { interval_us = strtol(v, NULL, 0); i++; }
        else if (!strcmp(argv[i], "--controllers") && v) { controllers = atoi(v); i++; }
        else { usage(); return 2; }
    }
    if (argc == 1) { usage(); return 2; }
    if (seed == 0) seed = 1;
    if (controllers < 1 || controllers > 4 || interval_us < 0) { usage(); return 2; }
    rng_state = seed;

    for (long n = 0; count == 0 || n < count; n++) {
        uint64_t t = now_ns();
        int controller = (int)(rng_next() % (uint64_t)controllers);
        switch (rng_next() % 10) {
            case 0: case 1: case 2: case 3: case 4:
                emit_pose(t, controller); break;
            case 5: case 6:
                emit_button(t, controller, (int)(rng_next() % 8), (int)(rng_next() & 1)); break;
            default:
                emit_axis(t, controller, (int)(rng_next() % 4)); break;
        }
        if (interval_us) {
            struct timespec sleep_ts = { interval_us / 1000000, (interval_us % 1000000) * 1000 };
            nanosleep(&sleep_ts, NULL);
        }
    }
    return 0;
}
