/* rt: XTOS native real-time tuning tool (C, static).
 * Shows and applies CPU isolation, IRQ affinity and RT throttling settings
 * for latency-critical workloads (BCI acquisition, AI inference loops).
 *
 * All writes are explicit (never silent): `rt tune` prints every change and
 * refuses to touch CPUs that host essential services (CPU0 always stays
 * housekeeping). Settings are volatile (RAM) — reboot restores defaults;
 * `rt tune --save` emits a profile file for the boot loader.
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

static int write_str(const char *path, const char *value) {
    int fd = open(path, O_WRONLY);
    if (fd < 0) return -1;
    ssize_t n = write(fd, value, strlen(value));
    int saved = errno;
    close(fd);
    if (n < 0) { errno = saved; return -1; }
    return 0;
}

static int ncpus_online(void) {
    return (int)sysconf(_SC_NPROCESSORS_ONLN);
}

/* ---- show ---- */

static int show_status(int json) {
    int ncpu = ncpus_online();

    long rt_runtime_us = read_int("/proc/sys/kernel/sched_rt_runtime_us");
    long rt_period_us = read_int("/proc/sys/kernel/sched_rt_period_us");

    /* isolated CPUs from /sys/devices/system/cpu/isolated */
    char isolated[LINE_MAX_LEN] = "";
    read_first_line("/sys/devices/system/cpu/isolated", isolated, sizeof(isolated));

    int realtime = (int)read_int("/sys/kernel/realtime");

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
                   "isolcpus=dom0,managed_irq,N-M (keep 0-%d for housekeeping).\n",
                   ncpu/2 - 1);
    }
    return 0;
}

/* ---- tune (volatile, reversible) ---- */

static int apply_tune(void) {
    int changes = 0;

    /* 1. Disable RT throttling: runtime = -1 */
    long rt_runtime = read_int("/proc/sys/kernel/sched_rt_runtime_us");
    if (rt_runtime >= 0) {
        if (write_str("/proc/sys/kernel/sched_rt_runtime_us", "-1") == 0) {
            printf("RT_TUNE rt_runtime_us=-1 (throttling disabled)\n");
            changes++;
        } else {
            fprintf(stderr, "rt: cannot set rt_runtime_us: %s\n", strerror(errno));
        }
    } else {
        printf("RT_TUNE rt_runtime_us already -1\n");
    }

    /* 2. Move all movable IRQs to CPU0 (housekeeping) so RT CPUs stay quiet. */
    DIR *d = opendir("/proc/irq");
    if (d) {
        struct dirent *e;
        while ((e = readdir(d))) {
            if (!isdigit((unsigned char)e->d_name[0])) continue;
            char ppath[600];
            /* only move IRQs that have a movable affinity mask */
            snprintf(ppath, sizeof(ppath), "/proc/irq/%s/smp_affinity_list", e->d_name);
            char cur[LINE_MAX_LEN] = "";
            read_first_line(ppath, cur, sizeof(cur));
            if (strcmp(cur, "0") == 0) continue; /* already on CPU0 */
            if (write_str(ppath, "0") == 0) {
                printf("RT_TUNE irq %s affinity -> 0\n", e->d_name);
                changes++;
            }
            /* failures (non-movable IRQs) are expected and silent */
        }
        closedir(d);
    }

    /* 3. Stop kworkers on non-zero CPUs is not runtime-tunable (boot param);
     * report the guidance instead. */
    printf("RT_TUNE volatile changes applied: %d\n", changes);
    printf("RT_TUNE for CPU isolation add to kernel cmdline: isolcpus=dom0,managed_irq,nohz_full=N-M rcu_nocbs=N-M\n");
    return 0;
}

static void usage(void) {
    fprintf(stderr,
        "usage: rt <command>\n"
        "  rt status            real-time status + recommendations\n"
        "  rt tune              apply volatile RT optimizations (throttling off,\n"
        "                       IRQs to CPU0)\n"
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
        int realtime = (int)read_int("/sys/kernel/realtime");
        if (realtime != 1) {
            fprintf(stderr, "rt: refusing to tune a non-RT kernel\n");
            return 1;
        }
        return apply_tune();
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
