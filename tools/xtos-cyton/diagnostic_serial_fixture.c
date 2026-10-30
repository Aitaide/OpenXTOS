/* SPDX-License-Identifier: GPL-2.0 */
#define _POSIX_C_SOURCE 200809L
#include "cyton_diagnostic_serial.h"
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <time.h>
static volatile sig_atomic_t stopped;
static int fault, partial;
static void interrupt(int signo) { (void)signo; stopped = 1; }
ssize_t __real_write(int fd, const void *data, size_t length);
ssize_t __wrap_write(int fd, const void *data, size_t length)
{
    const char *p = data;
    if (fault && length == 9 && p[0] == 'x' && !partial) {
        partial = 1;
        return __real_write(fd, data, 3);
    }
    if (partial == 1 && fault == 2) {
        partial = 2; errno = EIO; return -1;
    }
    return __real_write(fd, data, length);
}
static void snapshot(const struct xtos_cyton_config *c)
{
    unsigned int i;
    putchar('[');
    for (i = 0; i < 24; ++i) printf("%s%u", i ? "," : "", c->registers[i]);
    putchar(']');
}
int main(int argc, char **argv)
{
    struct cyton_diagnostic_options o;
    struct cyton_diagnostic_result *r;
    unsigned int i;
    int rc, saved;
    if (argc != 10) return 2;
    memset(&o, 0, sizeof(o));
    o.path = argv[1]; o.mode = (enum cyton_diagnostic_mode)atoi(argv[2]);
    o.channel = (unsigned int)atoi(argv[3]); o.input = argv[4][0];
    o.settle_frames = (unsigned int)atoi(argv[5]);
    o.sample_frames = (unsigned int)atoi(argv[6]);
    o.command_timeout_ms = (unsigned int)atoi(argv[7]);
    o.operation_timeout_ms = (unsigned int)atoi(argv[8]);
    fault = atoi(argv[9]); o.stop = &stopped;
    signal(SIGINT, interrupt); signal(SIGTERM, interrupt);
    r = calloc(1, sizeof(*r)); if (!r) return 2;
    rc = cyton_diagnostic_capture(&o, r); saved = errno;
    printf("{\"rc\":%d,\"errno\":%d,\"error\":%d,\"cleanup\":%d,\"cleanup_error\":%d,"
           "\"frames\":%u,\"settled\":%u,\"samples\":%u,\"bad_frames\":%u,\"gaps\":%u,\"rails\":%u,"
           "\"before_mask\":%u,\"test_mask\":%u,\"after_mask\":%u,\"before\":",
           rc, saved, r->error, r->restoration, r->cleanup_error, r->frames_observed,
           r->settled_frames, r->sample_count, r->bad_frames, r->counter_errors, r->rail_errors,
           r->before.register_mask, r->test.register_mask, r->after.register_mask);
    snapshot(&r->before); printf(",\"test\":"); snapshot(&r->test);
    printf(",\"after\":"); snapshot(&r->after); printf(",\"nv\":[");
    for (i = 0; i < r->sample_count; ++i) printf("%s%d", i ? "," : "", r->samples_nv[i]);
    puts("]}"); free(r); return rc ? 1 : 0;
}
