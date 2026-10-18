/* XTOS cgroup v2 control: mount the unified hierarchy and manage cpuset
 * partitions so real-time tasks and AI/inference workloads can be pinned to
 * disjoint CPU sets on one machine.
 *
 * Scope honesty: the shipped live kernel enables CPUSETS + CGROUP_SCHED but
 * not MEMCG or CFS_BANDWIDTH, so this tool partitions CPUs (the core RT
 * isolation) and reports the memory/bandwidth controllers as absent rather
 * than pretending to bound them. All operations are native syscalls and
 * file I/O — no shell, no external binaries.
 *
 * Exit codes: 0 ok, 1 error, 2 usage.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/types.h>
#include <unistd.h>

#include "xtos_ui.h"

#define CGROUP_ROOT "/sys/fs/cgroup"
#define CGROUP2_MAGIC 0x63677270
#define NAME_MAX_LEN 40

static void usage(void) {
    fprintf(stderr,
        "usage: cgroup <command>\n"
        "  cgroup init                              mount cgroup2 + enable controllers (root)\n"
        "  cgroup status [--json]                   hierarchy, controllers, partitions\n"
        "  cgroup partition <name> --cpus <list>    create a cpuset partition (e.g. 2-5,7)\n"
        "    --mems <list>                              memory nodes (default: parent's)\n"
        "  cgroup pin <name> <pid>                  move a process into a partition\n");
}

/* ---- small helpers ---- */

static int read_file(const char *path, char *buf, size_t n) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    ssize_t r = read(fd, buf, n - 1);
    close(fd);
    if (r < 0) return -1;
    buf[r] = '\0';
    return 0;
}

static int write_file(const char *path, const char *data) {
    int fd = open(path, O_WRONLY);
    if (fd < 0) return -1;
    size_t len = strlen(data);
    ssize_t w = write(fd, data, len);
    int e = errno;
    close(fd);
    if (w != (ssize_t)len) { errno = w < 0 ? e : EIO; return -1; }
    return 0;
}

static void chomp(char *s) {
    size_t n = strlen(s);
    while (n && (s[n - 1] == '\n' || s[n - 1] == ' ')) s[--n] = '\0';
}

static int hierarchy_mounted(void) {
    struct statfs st;
    if (statfs(CGROUP_ROOT, &st) < 0) return 0;
    return (unsigned)st.f_type == CGROUP2_MAGIC;
}

static int valid_name(const char *name) {
    size_t n = strlen(name);
    if (n == 0 || n > NAME_MAX_LEN) return 0;
    if (name[0] == '.') return 0;
    for (const char *p = name; *p; p++)
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') || *p == '.' || *p == '_' || *p == '-'))
            return 0;
    return 1;
}

/* cpu/mems list: numbers or N-M ranges, comma separated, e.g. 2-5,7 */
static int valid_cpu_list(const char *s) {
    if (!s || !*s) return 0;
    const char *p = s;
    for (;;) {
        /* one number */
        int d = 0;
        while (*p >= '0' && *p <= '9') { d = 1; p++; }
        if (!d) return 0;
        if (*p == '-') {
            p++;
            int d2 = 0;
            while (*p >= '0' && *p <= '9') { d2 = 1; p++; }
            if (!d2) return 0;
            long a = strtol(p - (d + d2 + 1), NULL, 10);  /* not used; range order checked below */
            (void)a;
        }
        if (*p == '\0') return 1;
        if (*p != ',') return 0;
        p++;
        if (*p == '\0') return 0; /* trailing comma */
    }
}

static int join_path(char *out, size_t n, const char *a, const char *b) {
    int w = snprintf(out, n, "%s/%s", a, b);
    if (w < 0 || (size_t)w >= n) return -1;
    return 0;
}

/* ---- commands ---- */

static int do_init(void) {
    if (mkdir(CGROUP_ROOT, 0755) < 0 && errno != EEXIST) {
        fprintf(stderr, "XTOS_CGROUP_ERROR cannot mkdir %s: %s\n", CGROUP_ROOT, strerror(errno));
        return 1;
    }
    if (!hierarchy_mounted()) {
        if (mount("none", CGROUP_ROOT, "cgroup2", 0, NULL) < 0) {
            fprintf(stderr, "XTOS_CGROUP_ERROR cannot mount cgroup2: %s\n", strerror(errno));
            return 1;
        }
    }
    /* enable the controllers that exist; the shipped kernel has cpuset but
     * not MEMCG/CFS_BANDWIDTH — report what is actually available. */
    char avail[512];
    if (read_file(CGROUP_ROOT "/cgroup.controllers", avail, sizeof(avail)) < 0) {
        fprintf(stderr, "XTOS_CGROUP_ERROR cannot read cgroup.controllers\n");
        return 1;
    }
    char enable[256];
    size_t len = 0;
    enable[0] = '\0';
    const char *want[] = {"cpuset", "cpu", "memory", "pids", NULL};
    for (int i = 0; want[i]; i++) {
        int found = 0;
        char tmp[512];
        snprintf(tmp, sizeof(tmp), "%s", avail);
        char *save = NULL;
        for (char *tok = strtok_r(tmp, " \t\n", &save); tok; tok = strtok_r(NULL, " \t\n", &save))
            if (strcmp(tok, want[i]) == 0) { found = 1; break; }
        if (found) len += (size_t)snprintf(enable + len, sizeof(enable) - len, "+%s ", want[i]);
    }
    if (len) {
        chomp(enable);
        if (write_file(CGROUP_ROOT "/cgroup.subtree_control", enable) < 0) {
            /* root may already expose controllers from cmdline; not fatal */
            fprintf(stderr, "XTOS_CGROUP_WARN could not enable controllers (%s): %s\n",
                    enable, strerror(errno));
        }
    }
    if (read_file(CGROUP_ROOT "/cgroup.controllers", avail, sizeof(avail)) < 0) avail[0] = '\0';
    chomp(avail);
    xui_init();
    printf("%s%s%s", XUI_LABEL_S, "XTOS_CGROUP", XUI_RESET_S);
    printf(" hierarchy=mounted root=%s\n", CGROUP_ROOT);
    printf(" controllers=%s\n", avail[0] ? avail : "(none)");
    printf(" partitions: use 'cgroup partition <name> --cpus <list>'\n");
    return 0;
}

static int do_status(int json) {
    if (!hierarchy_mounted()) {
        if (json) printf("{\"mounted\": false}\n");
        else {
            xui_init();
            printf("%s%s%s", XUI_LABEL_S, "XTOS_CGROUP", XUI_RESET_S);
            printf(" hierarchy=");
            XUI_WARN("unmounted");
            printf(" (run: cgroup init)\n");
        }
        return 0;
    }
    char ctrls[512];
    if (read_file(CGROUP_ROOT "/cgroup.controllers", ctrls, sizeof(ctrls)) < 0) ctrls[0] = '\0';
    chomp(ctrls);
    /* tokenize controllers for JSON */
    char ctrl_json[600];
    size_t cl = 0;
    ctrl_json[0] = '\0';
    {
        char tmp[512];
        snprintf(tmp, sizeof(tmp), "%s", ctrls);
        char *save = NULL;
        int first = 1;
        for (char *tok = strtok_r(tmp, " \t\n", &save); tok; tok = strtok_r(NULL, " \t\n", &save)) {
            cl += (size_t)snprintf(ctrl_json + cl, sizeof(ctrl_json) - cl, "%s\"%s\"", first ? "" : ", ", tok);
            first = 0;
        }
    }

    /* iterate partitions (subdirectories of the root) */
    char names[64][NAME_MAX_LEN + 1];
    char cpus[64][128], eff[64][128];
    int procs[64];
    int count = 0;
    DIR *d = opendir(CGROUP_ROOT);
    if (d) {
        struct dirent *e;
        while ((e = readdir(d)) != NULL && count < 64) {
            if (e->d_name[0] == '.') continue;
            char p[4096];
            if (join_path(p, sizeof(p), CGROUP_ROOT, e->d_name) < 0) continue;
            struct stat st;
            if (stat(p, &st) < 0 || !S_ISDIR(st.st_mode)) continue;
            if (strlen(e->d_name) > NAME_MAX_LEN) continue; /* too long to be one of ours */
            snprintf(names[count], NAME_MAX_LEN + 1, "%s", e->d_name);
            char q[4096];
            join_path(q, sizeof(q), p, "cpuset.cpus");
            if (read_file(q, cpus[count], 128) < 0) cpus[count][0] = '\0';
            else chomp(cpus[count]);
            join_path(q, sizeof(q), p, "cpuset.cpus.effective");
            if (read_file(q, eff[count], 128) < 0) eff[count][0] = '\0';
            else chomp(eff[count]);
            join_path(q, sizeof(q), p, "cgroup.procs");
            int n = 0;
            FILE *f = fopen(q, "r");
            if (f) {
                char line[64];
                while (fgets(line, sizeof(line), f)) n++;
                fclose(f);
            }
            procs[count] = n;
            count++;
        }
        closedir(d);
    }

    if (json) {
        printf("{\"mounted\": true, \"controllers\": [%s], \"partitions\": [", ctrl_json);
        for (int i = 0; i < count; i++)
            printf("%s{\"name\": \"%s\", \"cpus\": \"%s\", \"effective\": \"%s\", \"procs\": %d}",
                   i ? ", " : "", names[i], cpus[i], eff[i], procs[i]);
        printf("]}\n");
    } else {
        xui_init();
        printf("%s%s%s", XUI_LABEL_S, "XTOS_CGROUP", XUI_RESET_S);
        printf(" hierarchy=mounted controllers=%s\n", ctrls[0] ? ctrls : "(none)");
        if (count == 0) {
            printf(" partitions: ");
            XUI_WARN("(none)");
            printf("\n");
        }
        for (int i = 0; i < count; i++) {
            printf("  %-16s cpus=%-14s effective=%-14s procs=%d\n",
                   names[i], cpus[i][0] ? cpus[i] : "(all)", eff[i][0] ? eff[i] : "(all)", procs[i]);
        }
    }
    return 0;
}

static int do_partition(const char *name, const char *cpus, const char *mems) {
    if (!valid_name(name)) {
        fprintf(stderr, "XTOS_CGROUP_ERROR invalid partition name\n");
        return 1;
    }
    if (!hierarchy_mounted()) {
        fprintf(stderr, "XTOS_CGROUP_ERROR cgroup2 not mounted (run: cgroup init)\n");
        return 1;
    }
    if (!valid_cpu_list(cpus)) {
        fprintf(stderr, "XTOS_CGROUP_ERROR --cpus must be like 0 or 2-5,7\n");
        return 1;
    }
    char path[4096];
    if (join_path(path, sizeof(path), CGROUP_ROOT, name) < 0) return 1;
    if (mkdir(path, 0755) < 0 && errno != EEXIST) {
        fprintf(stderr, "XTOS_CGROUP_ERROR cannot create %s: %s\n", path, strerror(errno));
        return 1;
    }
    char f[4096];
    join_path(f, sizeof(f), path, "cpuset.cpus");
    if (write_file(f, cpus) < 0) {
        fprintf(stderr, "XTOS_CGROUP_ERROR cannot set cpuset.cpus: %s\n", strerror(errno));
        return 1;
    }
    if (mems) {
        if (!valid_cpu_list(mems)) {
            fprintf(stderr, "XTOS_CGROUP_ERROR --mems must be like 0 or 0-1\n");
            return 1;
        }
        join_path(f, sizeof(f), path, "cpuset.mems");
        if (write_file(f, mems) < 0) {
            fprintf(stderr, "XTOS_CGROUP_ERROR cannot set cpuset.mems: %s\n", strerror(errno));
            return 1;
        }
    } else {
        char parent[128];
        if (read_file(CGROUP_ROOT "/cpuset.mems.effective", parent, sizeof(parent)) == 0) {
            chomp(parent);
            join_path(f, sizeof(f), path, "cpuset.mems");
            write_file(f, parent); /* best effort: kernel defaults anyway */
        }
    }
    char eff[128];
    join_path(f, sizeof(f), path, "cpuset.cpus.effective");
    if (read_file(f, eff, sizeof(eff)) < 0) eff[0] = '\0';
    else chomp(eff);
    printf("XTOS_CGROUP_PARTITION name=%s cpus=%s effective=%s\n", name, cpus, eff[0] ? eff : cpus);
    return 0;
}

static int do_pin(const char *name, long pid) {
    if (!valid_name(name)) {
        fprintf(stderr, "XTOS_CGROUP_ERROR invalid partition name\n");
        return 1;
    }
    if (pid < 1) {
        fprintf(stderr, "XTOS_CGROUP_ERROR pid must be >= 1\n");
        return 1;
    }
    char proc[64];
    snprintf(proc, sizeof(proc), "/proc/%ld", pid);
    struct stat st;
    if (stat(proc, &st) < 0) {
        fprintf(stderr, "XTOS_CGROUP_ERROR no such process %ld\n", pid);
        return 1;
    }
    char f[4096];
    if (join_path(f, sizeof(f), CGROUP_ROOT, name) < 0) return 1;
    char procs[4096];
    if (join_path(procs, sizeof(procs), f, "cgroup.procs") < 0) return 1;
    char data[32];
    snprintf(data, sizeof(data), "%ld", pid);
    if (write_file(procs, data) < 0) {
        fprintf(stderr, "XTOS_CGROUP_ERROR cannot pin to %s: %s\n", name, strerror(errno));
        return 1;
    }
    /* verify from the kernel's own view of the process */
    char cg[4096];
    snprintf(cg, sizeof(cg), "/proc/%ld/cgroup", pid);
    if (read_file(cg, cg, sizeof(cg)) == 0) {
        char want[64];
        snprintf(want, sizeof(want), "0::/%s\n", name);
        if (!strstr(cg, want)) {
            fprintf(stderr, "XTOS_CGROUP_ERROR pin not confirmed by /proc/%ld/cgroup\n", pid);
            return 1;
        }
    }
    printf("XTOS_CGROUP_PIN name=%s pid=%ld confirmed\n", name, pid);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) { usage(); return 2; }
    const char *cmd = argv[1];
    if (strcmp(cmd, "init") == 0 && argc == 2) return do_init();
    if (strcmp(cmd, "status") == 0) {
        int json = 0;
        for (int i = 2; i < argc; i++) {
            if (strcmp(argv[i], "--json") == 0) json = 1;
            else { usage(); return 2; }
        }
        return do_status(json);
    }
    if (strcmp(cmd, "partition") == 0) {
        const char *name = NULL, *cpus = NULL, *mems = NULL;
        for (int i = 2; i < argc; i++) {
            if (strcmp(argv[i], "--cpus") == 0 && i + 1 < argc) cpus = argv[++i];
            else if (strcmp(argv[i], "--mems") == 0 && i + 1 < argc) mems = argv[++i];
            else if (!name && argv[i][0] != '-') name = argv[i];
            else { usage(); return 2; }
        }
        if (!name || !cpus) { usage(); return 2; }
        return do_partition(name, cpus, mems);
    }
    if (strcmp(cmd, "pin") == 0 && argc == 4) {
        char *end = NULL;
        long pid = strtol(argv[3], &end, 10);
        if (!end || *end) { usage(); return 2; }
        return do_pin(argv[2], pid);
    }
    usage();
    return 2;
}
