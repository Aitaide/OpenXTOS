#ifndef XTOS_RT_IRQ_H
#define XTOS_RT_IRQ_H

#include <ctype.h>
#include <errno.h>
#include <limits.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

#define RT_MAX_CPU_RANGES 256

typedef struct {
    unsigned long first;
    unsigned long last;
} rt_cpu_range;

typedef struct {
    rt_cpu_range ranges[RT_MAX_CPU_RANGES];
    size_t count;
} rt_cpu_list;

static int rt_parse_decimal(const char **cursor, unsigned long *value) {
    const char *p = *cursor;
    unsigned long result = 0;
    size_t digits = 0;

    while (isdigit((unsigned char)*p)) {
        unsigned int digit = (unsigned int)(*p - '0');
        if (result > (ULONG_MAX - digit) / 10UL) {
            errno = ERANGE;
            return -1;
        }
        result = result * 10UL + digit;
        p++;
        digits++;
    }
    if (digits == 0 || result > (unsigned long)INT_MAX) {
        errno = EINVAL;
        return -1;
    }
    *cursor = p;
    *value = result;
    return 0;
}

/* Parse the kernel's strict CPU-list grammar: N[,N[-N]]... . */
static int rt_parse_cpu_list(const char *text, rt_cpu_list *out) {
    const char *p = text;
    size_t i;

    memset(out, 0, sizeof(*out));
    if (!text || *text == '\0') {
        errno = EINVAL;
        return -1;
    }
    for (;;) {
        unsigned long first, last;
        if (out->count >= RT_MAX_CPU_RANGES || rt_parse_decimal(&p, &first) < 0) return -1;
        last = first;
        if (*p == '-') {
            p++;
            if (rt_parse_decimal(&p, &last) < 0 || last < first) {
                errno = EINVAL;
                return -1;
            }
        }
        for (i = 0; i < out->count; i++) {
            if (first <= out->ranges[i].last && last >= out->ranges[i].first) {
                errno = EINVAL;
                return -1;
            }
        }
        out->ranges[out->count].first = first;
        out->ranges[out->count].last = last;
        out->count++;
        if (*p == '\0') break;
        if (*p != ',') {
            errno = EINVAL;
            return -1;
        }
        p++;
        if (*p == '\0') {
            errno = EINVAL;
            return -1;
        }
    }
    return 0;
}

static int rt_cpu_list_subset(const rt_cpu_list *subset, const rt_cpu_list *superset) {
    size_t i;
    for (i = 0; i < subset->count; i++) {
        unsigned long cursor = subset->ranges[i].first;
        while (cursor <= subset->ranges[i].last) {
            size_t j;
            unsigned long covered_until = 0;
            int covered = 0;
            for (j = 0; j < superset->count; j++) {
                if (cursor >= superset->ranges[j].first &&
                    cursor <= superset->ranges[j].last) {
                    covered = 1;
                    covered_until = superset->ranges[j].last;
                    break;
                }
            }
            if (!covered) return 0;
            if (covered_until >= subset->ranges[i].last) break;
            cursor = covered_until + 1UL;
        }
    }
    return 1;
}

static int rt_cpu_lists_equal(const rt_cpu_list *a, const rt_cpu_list *b) {
    return rt_cpu_list_subset(a, b) && rt_cpu_list_subset(b, a);
}

static int rt_cpu_list_overlaps(const rt_cpu_list *a, const rt_cpu_list *b) {
    size_t i, j;
    for (i = 0; i < a->count; i++) {
        for (j = 0; j < b->count; j++) {
            if (a->ranges[i].first <= b->ranges[j].last &&
                b->ranges[j].first <= a->ranges[i].last) return 1;
        }
    }
    return 0;
}

#endif
