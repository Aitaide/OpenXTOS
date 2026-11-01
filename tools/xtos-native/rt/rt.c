/* rt: XTOS native real-time tuning tool (C, static).
 * Shows and applies CPU isolation, IRQ affinity and RT throttling settings
 * for latency-critical workloads (BCI acquisition, AI inference loops).
 *
 * All writes are explicit (never silent): `rt tune` prints each requested and
 * effective IRQ affinity. With no housekeeping option it preserves the
 * historical CPU 0 default; an explicit CPU list is validated first.
 * Settings are volatile (RAM) — reboot restores defaults.
 *
 * Build: gcc -O2 -Wall -Wextra -Werror -static -o rt rt.c
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/stat.h>
#include "xtos_ui.h"
#include "xtos_rt_state.h"
#include "rt_irq.h"

#define LINE_MAX_LEN 512

static char *read_first_line(const char *path, char *buf, size_t size) {
    buf[0] = '\0';
    FILE *f = fopen(path, "r");
    if (!f) return buf;
    if (fgets(buf, (int)size, f)) {
        buf[strcspn(buf, "\n")] = '\0';
        size_t len = strlen(buf);
        while (len > 0 && isspace((unsigned char)buf[len-1])) buf[--len] = '\0';
    }
    fclose(f);
    return buf;
}

static long read_int(const char *path) {
    char buf[LINE_MAX_LEN];
    read_first_line(path, buf, sizeof(buf));
    return atol(buf);
}

static int read_rt_runtime(long *value) {
    char buf[LINE_MAX_LEN], *end;
    FILE *f = fopen("/proc/sys/kernel/sched_rt_runtime_us", "r");
    if (!f) return -1;
    if (!fgets(buf, sizeof(buf), f)) {
        int saved = ferror(f) && errno ? errno : EIO;
        fclose(f);
        errno = saved;
        return -1;
    }
    int extra = fgetc(f);
    int saved = ferror(f) ? (errno ? errno : EIO) : 0;
    if (fclose(f) != 0 && !saved) saved = errno ? errno : EIO;
    if (saved || extra != EOF) {
        errno = saved ? saved : EINVAL;
        return -1;
    }
    errno = 0;
    long parsed = strtol(buf, &end, 10);
    if (errno || end == buf) { errno = EINVAL; return -1; }
    while (isspace((unsigned char)*end)) end++;
    if (*end || parsed < -1) { errno = EINVAL; return -1; }
    *value = parsed;
    return 0;
}

static int write_str(const char *path, const char *value) {
    size_t len = strlen(value);
    int fd = open(path, O_WRONLY);
    if (fd < 0) return -1;
    ssize_t n = write(fd, value, len);
    int saved = errno;
    int close_rc = close(fd);
    int close_errno = errno;
    if (n < 0) { errno = saved; return -1; }
    if ((size_t)n != len) { errno = EIO; return -1; }
    if (close_rc < 0) { errno = close_errno; return -1; }
    return 0;
}

static int ncpus_online(void) {
    char text[LINE_MAX_LEN];
    rt_cpu_list online;
    size_t i;
    unsigned long total = 0;

    read_first_line("/sys/devices/system/cpu/online", text, sizeof(text));
    if (rt_parse_cpu_list(text, &online) == 0) {
        for (i = 0; i < online.count; i++) {
            total += online.ranges[i].last - online.ranges[i].first + 1UL;
        }
        if (total <= (unsigned long)INT_MAX) return (int)total;
    }
    return (int)sysconf(_SC_NPROCESSORS_ONLN);
}

static int read_cpu_list_file(const char *path, rt_cpu_list *list, int allow_empty) {
    char text[LINE_MAX_LEN];
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    if (!fgets(text, sizeof(text), f)) {
        int read_error = ferror(f);
        int empty_file = feof(f) && !read_error;
        int saved = read_error ? (errno ? errno : EIO) : EIO;
        fclose(f);
        if (allow_empty && empty_file) {
            memset(list, 0, sizeof(*list));
            return 0;
        }
        errno = saved;
        return -1;
    }
    size_t len = strlen(text);
    if (len && text[len - 1] == '\n') text[--len] = '\0';
    int extra = fgetc(f);
    int read_error = ferror(f);
    fclose(f);
    if (read_error || extra != EOF) {
        errno = read_error && errno ? errno : EOVERFLOW;
        return -1;
    }
    if (text[0] == '\0' && allow_empty) {
        memset(list, 0, sizeof(*list));
        return 0;
    }
    return rt_parse_cpu_list(text, list);
}

static int cpu_list_text(const rt_cpu_list *list, char *buf, size_t size) {
    size_t i;
    size_t used = 0;
    for (i = 0; i < list->count; i++) {
        int n = snprintf(buf + used, size - used, "%s%lu", i ? "," : "",
                         list->ranges[i].first);
        if (n < 0 || (size_t)n >= size - used) return -1;
        used += (size_t)n;
        if (list->ranges[i].last != list->ranges[i].first) {
            n = snprintf(buf + used, size - used, "-%lu", list->ranges[i].last);
            if (n < 0 || (size_t)n >= size - used) return -1;
            used += (size_t)n;
        }
    }
    return 0;
}

static int validate_housekeeping(const char *text, rt_cpu_list *housekeeping) {
    rt_cpu_list online;
    rt_cpu_list isolated;

    if (rt_parse_cpu_list(text, housekeeping) < 0) {
        fprintf(stderr, "rt: invalid housekeeping CPU list '%s'\n", text);
        return -1;
    }
    if (read_cpu_list_file("/sys/devices/system/cpu/online", &online, 0) < 0) {
        fprintf(stderr, "rt: cannot read online CPU list\n");
        return -1;
    }
    if (!rt_cpu_list_subset(housekeeping, &online)) {
        fprintf(stderr, "rt: housekeeping CPUs must be online\n");
        return -1;
    }
    if (read_cpu_list_file("/sys/devices/system/cpu/isolated", &isolated, 1) < 0) {
        fprintf(stderr, "rt: cannot read boot-isolated CPU list\n");
        return -1;
    }
    if (rt_cpu_list_overlaps(housekeeping, &isolated)) {
        fprintf(stderr, "rt: housekeeping CPUs overlap boot-isolated CPUs\n");
        return -1;
    }
    return 0;
}

/* ---- show ---- */

static int show_status(int json) {
    int ncpu = ncpus_online();

    long rt_runtime_us = read_int("/proc/sys/kernel/sched_rt_runtime_us");
    long rt_period_us = read_int("/proc/sys/kernel/sched_rt_period_us");

    /* isolated CPUs from /sys/devices/system/cpu/isolated */
    char isolated[LINE_MAX_LEN] = "";
    read_first_line("/sys/devices/system/cpu/isolated", isolated, sizeof(isolated));

    int realtime = xtos_rt_active();

    if (json) {
        printf("{\n");
        printf("  \"realtime_kernel\": %s,\n", realtime == 1 ? "true" : "false");
        printf("  \"online_cpus\": %d,\n", ncpu);
        printf("  \"isolated_cpus\": \"%s\",\n", isolated[0] ? isolated : "");
        printf("  \"rt_runtime_us\": %ld,\n", rt_runtime_us);
        printf("  \"rt_period_us\": %ld,\n", rt_period_us);
        printf("  \"rt_throttling\": \"%s\",\n",
               rt_runtime_us < 0 ? "disabled (-1)" :
               rt_runtime_us == rt_period_us ? "off (runtime==period)" : "enabled");
        printf("  \"irq_affinity\": [\n");
    } else {
        xui_init();
        XUI_TITLE("XTOS Real-time Status\n");
        XUI_LABEL("Kernel:     ");
        if (realtime == 1) XUI_OK("PREEMPT_RT active\n");
        else XUI_BAD("not realtime!\n");
        XUI_LABEL("CPUs:       "); printf("%d online, isolated: %s\n", ncpu,
               isolated[0] ? isolated : "none");
        XUI_LABEL("RT budget:  "); printf("runtime %ld us / period %ld us → throttling ",
               rt_runtime_us, rt_period_us);
        if (rt_runtime_us < 0) XUI_OK("disabled\n");
        else if (rt_runtime_us == rt_period_us) XUI_OK("off\n");
        else XUI_WARN("ON\n");
        XUI_LABEL("IRQ affinity (non-housekeeping CPUs):\n");
    }

    /* IRQ list with affinity */
    DIR *d = opendir("/proc/irq");
    int first = 1;
    int shown = 0;
    if (d) {
        struct dirent *e;
        while ((e = readdir(d))) {
            if (!isdigit((unsigned char)e->d_name[0])) continue;
            char ppath[600], aff[LINE_MAX_LEN] = "";
            snprintf(ppath, sizeof(ppath), "/proc/irq/%s/affinity_hint", e->d_name);
            read_first_line(ppath, aff, sizeof(aff));
            snprintf(ppath, sizeof(ppath), "/proc/irq/%s", e->d_name);
            /* default affinity = /proc/irq/default_smp_affinity */
            char name[LINE_MAX_LEN] = "";
            snprintf(ppath, sizeof(ppath), "/proc/irq/%s", e->d_name);
            DIR *sd = opendir(ppath);
            if (sd) { closedir(sd); }
            snprintf(ppath, sizeof(ppath), "/sys/kernel/irq/%s/actions", e->d_name);
            read_first_line(ppath, name, sizeof(name));
            snprintf(ppath, sizeof(ppath), "/proc/irq/%s/smp_affinity_list", e->d_name);
            char smp[LINE_MAX_LEN] = "";
            read_first_line(ppath, smp, sizeof(smp));
            if (json)
                printf("%s    {\"irq\": %s, \"affinity\": \"%s\", \"action\": \"%s\"}",
                       first ? "" : ",\n", e->d_name, smp, name);
            else if (name[0])
                printf("  irq %-4s %-8s %s\n", e->d_name, smp, name);
            first = 0;
            shown++;
        }
        closedir(d);
    }
    if (json) printf("\n  ]\n}\n");
    else if (!shown) printf("  (no named IRQs)\n");

    /* Recommended section */
    if (!json) {
        printf("\n");
        XUI_TITLE("Recommendation:\n");
        if (realtime != 1)
            XUI_BAD("  Kernel is NOT PREEMPT_RT — check the booted image.\n");
        if (rt_runtime_us > 0 && rt_runtime_us < rt_period_us)
            XUI_WARN("  rt tune would set runtime=-1 (disable throttling) for "
                   "deterministic RT loops.\n");
        if (!isolated[0] && ncpu >= 4)
            XUI_DIM("  No isolated CPUs. For BCI acquisition: boot with "
                   "isolcpus=domain,managed_irq,N-M nohz_full=N-M "
                   "(keep 0-%d for housekeeping).\n",
                   ncpu/2 - 1);
    }
    return 0;
}

/* ---- tune (volatile, reversible) ---- */

static int irq_number_name(const char *name) {
    const unsigned char *p = (const unsigned char *)name;
    if (!*p) return 0;
    while (*p) {
        if (!isdigit(*p)) return 0;
        p++;
    }
    return 1;
}

static int irq_affinity_path(char *path, size_t size, const char *irq,
                             const char *leaf) {
    int n = snprintf(path, size, "/proc/irq/%s/%s", irq, leaf);
    return n >= 0 && (size_t)n < size ? 0 : -1;
}

static int read_affinity(const char *path, rt_cpu_list *list) {
    char text[LINE_MAX_LEN];
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    text[0] = '\0';
    if (!fgets(text, sizeof(text), f)) {
        int saved = ferror(f) ? errno : EIO;
        fclose(f);
        errno = saved;
        return -1;
    }
    fclose(f);
    text[strcspn(text, "\n")] = '\0';
    return rt_parse_cpu_list(text, list);
}

static int effective_affinity_ok(const rt_cpu_list *effective,
                                 const rt_cpu_list *housekeeping) {
    return effective->count > 0 && rt_cpu_list_subset(effective, housekeeping);
}

static int apply_tune(const rt_cpu_list *housekeeping) {
    int changes = 0;
    int verified = 0;
    int failures = 0;
    int skipped = 0;
    char cpus[LINE_MAX_LEN];

    if (geteuid() != 0) {
        fprintf(stderr, "rt: tune requires root\n");
        return 1;
    }
    if (cpu_list_text(housekeeping, cpus, sizeof(cpus)) < 0) {
        fprintf(stderr, "rt: housekeeping CPU list too long\n");
        return 1;
    }

    /* 1. Disable RT throttling: runtime = -1 */
    long rt_runtime;
    if (read_rt_runtime(&rt_runtime) < 0) {
        fprintf(stderr, "rt: cannot read rt_runtime_us: %s\n", strerror(errno));
        failures++;
    } else if (rt_runtime != -1) {
        if (write_str("/proc/sys/kernel/sched_rt_runtime_us", "-1") == 0) {
            if (read_rt_runtime(&rt_runtime) < 0) {
                fprintf(stderr, "rt: cannot read rt_runtime_us after write: %s\n",
                        strerror(errno));
                failures++;
            } else if (rt_runtime == -1) {
                printf("RT_TUNE rt_runtime_us=-1 (throttling disabled)\n");
                changes++;
            } else {
                fprintf(stderr, "rt: rt_runtime_us readback mismatch\n");
                failures++;
            }
        } else {
            fprintf(stderr, "rt: cannot set rt_runtime_us: %s\n", strerror(errno));
            failures++;
        }
    } else {
        printf("RT_TUNE rt_runtime_us already -1\n");
    }

    /* 2. Request and verify IRQ affinity on the selected housekeeping CPUs. */
    DIR *d = opendir("/proc/irq");
    if (!d) {
        fprintf(stderr, "rt: cannot enumerate IRQs: %s\n", strerror(errno));
        failures++;
    } else {
        struct dirent *e;
        while ((e = readdir(d))) {
            char requested_path[600], effective_path[600];
            rt_cpu_list requested, effective;
            int write_failed = 0;
            int needs_write;

            if (!irq_number_name(e->d_name)) continue;
            if (irq_affinity_path(requested_path, sizeof(requested_path), e->d_name,
                                  "smp_affinity_list") < 0 ||
                irq_affinity_path(effective_path, sizeof(effective_path), e->d_name,
                                  "effective_affinity_list") < 0) {
                fprintf(stderr, "RT_TUNE irq %s failed: path too long\n", e->d_name);
                failures++;
                continue;
            }
            if (read_affinity(requested_path, &requested) < 0 ||
                read_affinity(effective_path, &effective) < 0) {
                printf("RT_TUNE irq %s skipped: affinity readback unavailable\n", e->d_name);
                skipped++;
                continue;
            }
            needs_write = !rt_cpu_lists_equal(&requested, housekeeping);
            if (needs_write) {
                if (write_str(requested_path, cpus) < 0) {
                    write_failed = 1;
                }
                if (read_affinity(requested_path, &requested) < 0 ||
                    read_affinity(effective_path, &effective) < 0) {
                    printf("RT_TUNE irq %s failed: requested=unavailable effective=unavailable\n",
                           e->d_name);
                    failures++;
                    continue;
                }
            }
            if (write_failed || !rt_cpu_lists_equal(&requested, housekeeping) ||
                !effective_affinity_ok(&effective, housekeeping)) {
                char requested_text[LINE_MAX_LEN] = "unavailable";
                char effective_text[LINE_MAX_LEN] = "unavailable";
                if (cpu_list_text(&requested, requested_text, sizeof(requested_text)) < 0)
                    strcpy(requested_text, "unavailable");
                if (cpu_list_text(&effective, effective_text, sizeof(effective_text)) < 0)
                    strcpy(effective_text, "unavailable");
                printf("RT_TUNE irq %s failed: requested=\"%s\" effective=\"%s\"%s\n",
                       e->d_name,
                       requested_text,
                       effective_text,
                       write_failed ? " (write rejected or short)" : " (readback mismatch)");
                failures++;
            } else {
                char requested_text[LINE_MAX_LEN] = "unavailable";
                char effective_text[LINE_MAX_LEN] = "unavailable";
                if (cpu_list_text(&requested, requested_text, sizeof(requested_text)) < 0)
                    strcpy(requested_text, "unavailable");
                if (cpu_list_text(&effective, effective_text, sizeof(effective_text)) < 0)
                    strcpy(effective_text, "unavailable");
                printf("RT_TUNE irq %s requested=\"%s\" effective=\"%s\"\n",
                       e->d_name, requested_text, effective_text);
                verified++;
                if (needs_write) changes++;
            }
        }
        closedir(d);
    }

    /* 3. Stop kworkers on non-zero CPUs is not runtime-tunable (boot param);
     * report the guidance instead. */
    printf("RT_TUNE housekeeping_cpus=%s verified=%d changes=%d skipped=%d failed=%d\n",
           cpus, verified, changes, skipped, failures);
    printf("RT_TUNE for CPU isolation add to kernel cmdline: isolcpus=domain,managed_irq,N-M nohz_full=N-M rcu_nocbs=N-M\n");
    return failures ? 1 : 0;
}

static void usage(void) {
    fprintf(stderr,
        "usage: rt <command>\n"
        "  rt status            real-time status + recommendations\n"
        "  rt tune [--housekeeping CPU-LIST]\n"
        "                       move movable IRQs to housekeeping CPUs (default: 0)\n"
        "  rt cyclictest [sec]  quick latency check via cyclictest (default 10s)\n"
        "  --json               machine-readable output (status only)\n");
}

int main(int argc, char **argv) {
    int json = 0, argstart = 1;
    if (argc > 1 && strcmp(argv[1], "--json") == 0) { json = 1; argstart = 2; }
    if (argc <= argstart) { usage(); return 2; }
    const char *cmd = argv[argstart];

    if (strcmp(cmd, "status") == 0) return show_status(json);

    if (strcmp(cmd, "tune") == 0) {
        const char *housekeeping_arg = "0";
        rt_cpu_list housekeeping;
        int housekeeping_seen = 0;
        int i;
        for (i = argstart + 1; i < argc; i++) {
            if (strcmp(argv[i], "--housekeeping") == 0 && i + 1 < argc &&
                !housekeeping_seen) {
                housekeeping_arg = argv[++i];
                housekeeping_seen = 1;
            } else {
                fprintf(stderr, "rt: invalid tune argument '%s'\n", argv[i]);
                return 2;
            }
        }
        int realtime = xtos_rt_active();
        if (realtime != 1) {
            fprintf(stderr, "rt: refusing to tune a non-RT kernel\n");
            return 1;
        }
        if (validate_housekeeping(housekeeping_arg, &housekeeping) < 0) return 2;
        return apply_tune(&housekeeping);
    }

    if (strcmp(cmd, "cyclictest") == 0) {
        const char *secs = argc > argstart + 1 ? argv[argstart + 1] : "10";
        char cmdbuf[256];
        int ncpu = ncpus_online();
        int nthreads = ncpu > 4 ? 4 : (ncpu > 1 ? ncpu - 1 : 1);
        snprintf(cmdbuf, sizeof(cmdbuf),
                 "cyclictest --default-system -m -a -t %d -p 80 -i 1000 -h 100 -D %ss -q 2>&1 | tail -4",
                 nthreads, secs);
        printf("RT_CYCLICTEST duration=%ss threads=%d\n", secs, nthreads);
        return system(cmdbuf) == 0 ? 0 : 1;
    }

    fprintf(stderr, "rt: unknown command %s\n", cmd);
    usage();
    return 2;
}
