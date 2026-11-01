/* Read-only kernel identity; no latency or hardware readiness guarantee. */
#ifndef XTOS_RT_STATE_H
#define XTOS_RT_STATE_H

#include <ctype.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/utsname.h>

static inline int xtos_rt_active(void) {
    FILE *f = fopen("/sys/kernel/realtime", "r");
    if (f) {
        unsigned char value[3];
        size_t n = fread(value, 1, sizeof(value), f);
        int failed = ferror(f);
        if (fclose(f) != 0) failed = 1;
        /* Exact legacy 0/1 (optional newline) is authoritative. Everything
         * else fails closed; a banner must never override this node. */
        return !failed && (n == 1 || (n == 2 && value[1] == '\n')) &&
               value[0] == '1';
    }
    if (errno != ENOENT) return 0;

    /* Mainline init/Makefile generates this whitespace-delimited UTS_VERSION
     * token from CONFIG_PREEMPT_RT; version-timestamp.c publishes it through
     * uname.version. Release names and PREEMPT_DYNAMIC are not RT evidence. */
    struct utsname u = {0};
    if (uname(&u) != 0) return 0;
    const char *p = u.version;
    while (*p) {
        while (isspace((unsigned char)*p)) p++;
        const char *start = p;
        while (*p && !isspace((unsigned char)*p)) p++;
        if ((size_t)(p - start) == sizeof("PREEMPT_RT") - 1 &&
            memcmp(start, "PREEMPT_RT", sizeof("PREEMPT_RT") - 1) == 0)
            return 1;
    }
    return 0;
}

#endif
