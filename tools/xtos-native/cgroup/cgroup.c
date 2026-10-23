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
#include <limits.h>
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

#ifndef CGROUP_ROOT
#ifdef XTOS_CGROUP_ROOT
#define CGROUP_ROOT XTOS_CGROUP_ROOT
#else
#define CGROUP_ROOT "/sys/fs/cgroup"
#endif
#endif
#define CGROUP2_MAGIC 0x63677270
#define NAME_MAX_LEN 40
#define FILE_VALUE_MAX 4096

static void usage(void) {
    fprintf(stderr,
        "usage: cgroup <command>\n"
        "  cgroup init                              mount cgroup2 + enable controllers (root)\n"
        "  cgroup status [--json]                   hierarchy, controllers, partitions\n"
        "  cgroup partition <name> --cpus <list> [--partition=<type>]\n"
        "                                            type: member (default), root, isolated\n"
        "    --mems <list>                              memory nodes (default: parent's)\n"
        "  cgroup pin <name> <pid>                  move a process into a partition\n");
}

/* ---- small helpers ---- */

static int read_file(const char *path, char *buf, size_t n) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
#ifdef XTOS_CGROUP_TEST_MODE
#ifdef XTOS_CGROUP_TEST_PIN_READBACK_FAIL
    /* Test-only fault injection for the pin readback contract. */
    if (strncmp(path, "/proc/", 6) == 0 && strstr(path, "/cgroup") != NULL) {
        close(fd);
        errno = EIO;
        return -1;
    }
#endif
#endif
    ssize_t r = read(fd, buf, n - 1);
    close(fd);
    if (r < 0) return -1;
#ifdef XTOS_CGROUP_TEST_MODE
#ifdef XTOS_CGROUP_TEST_SHORT_PARTITION_READBACK
    /* Test-only fault injection for the partition readback contract. */
    if (r > 0 && strstr(path, "/cpuset.cpus.partition") != NULL) r = 1;
#endif
#endif
    buf[r] = '\0';
    return 0;
}

#ifdef XTOS_CGROUP_TEST_MODE
#ifdef XTOS_CGROUP_TEST_CANONICAL_READBACK
static int canonical_cpu_list(const char *input, char *output, size_t n);
#endif

#if !defined(XTOS_CGROUP_TEST_KEEP_EFFECTIVE) || defined(XTOS_CGROUP_TEST_CANONICAL_READBACK) || defined(XTOS_CGROUP_TEST_POST_EFFECTIVE)
static int fixture_leaf_is(const char *path, const char *leaf) {
    const char *slash = strrchr(path, '/');
    return slash && strcmp(slash + 1, leaf) == 0;
}
#endif

#if !defined(XTOS_CGROUP_TEST_KEEP_EFFECTIVE) || defined(XTOS_CGROUP_TEST_POST_EFFECTIVE)
static void fixture_effective_write(const char *path, const char *value) {
    char effective[PATH_MAX];
    const char *slash = strrchr(path, '/');
    if (!slash) return;
    size_t prefix = (size_t)(slash - path) + 1;
    const char leaf[] = "cpuset.cpus.effective";
    if (prefix > sizeof(effective) - sizeof(leaf)) return;
    memcpy(effective, path, prefix);
    memcpy(effective + prefix, leaf, sizeof(leaf));
    int fd = open(effective, O_WRONLY | O_TRUNC);
    if (fd >= 0) {
        (void)write(fd, value, strlen(value));
        close(fd);
    }
}
#endif
#endif

static int write_file(const char *path, const char *data) {
#ifdef XTOS_CGROUP_TEST_MODE
    int flags = O_WRONLY | O_TRUNC;
#ifdef XTOS_CGROUP_TEST_CANONICAL_READBACK
    char canonical[FILE_VALUE_MAX];
    if ((fixture_leaf_is(path, "cpuset.cpus") || fixture_leaf_is(path, "cpuset.mems")) &&
        canonical_cpu_list(data, canonical, sizeof(canonical)) == 0)
        data = canonical;
#endif
#else
    int flags = O_WRONLY;
#endif
    int fd = open(path, flags);
    if (fd < 0) return -1;
    size_t len = strlen(data);
    ssize_t w = write(fd, data, len);
    int e = errno;
    close(fd);
    if (w != (ssize_t)len) { errno = w < 0 ? e : EIO; return -1; }
#ifdef XTOS_CGROUP_TEST_MODE
    /* Compile-time regular-file fixtures only; no runtime production hooks. */
#ifndef XTOS_CGROUP_TEST_KEEP_EFFECTIVE
    if (fixture_leaf_is(path, "cpuset.cpus")) fixture_effective_write(path, data);
#endif
#ifdef XTOS_CGROUP_TEST_POST_EFFECTIVE
    if (fixture_leaf_is(path, "cpuset.cpus.partition"))
        fixture_effective_write(path, XTOS_CGROUP_TEST_POST_EFFECTIVE);
#endif
#endif
    return 0;
}

static void chomp(char *s) {
    size_t n = strlen(s);
    while (n && (s[n - 1] == '\n' || s[n - 1] == '\r' || s[n - 1] == ' ' || s[n - 1] == '\t'))
        s[--n] = '\0';
}

static void json_string(const char *s) {
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        if (*p == '"' || *p == '\\') printf("\\%c", *p);
        else if (*p == '\n') printf("\\n");
        else if (*p == '\r') printf("\\r");
        else if (*p == '\t') printf("\\t");
        else if (*p < 0x20) printf("\\u%04x", *p);
        else putchar(*p);
    }
    putchar('"');
}

static int hierarchy_mounted(void) {
#ifdef XTOS_CGROUP_TEST_MODE
    /* Compile-time fixture hook; the shipped binary always uses statfs. */
    return access(CGROUP_ROOT "/cgroup.controllers", R_OK) == 0;
#else
    struct statfs st;
    if (statfs(CGROUP_ROOT, &st) < 0) return 0;
    return (unsigned)st.f_type == CGROUP2_MAGIC;
#endif
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

struct cpu_range {
    unsigned long start;
    unsigned long end;
};

static int compare_cpu_range(const void *a, const void *b) {
    const struct cpu_range *left = a, *right = b;
    if (left->start != right->start) return left->start < right->start ? -1 : 1;
    return (left->end > right->end) - (left->end < right->end);
}

/* Work is proportional to interval count, never to CPU IDs or range widths. */
static size_t merge_cpu_ranges(struct cpu_range *ranges, size_t count) {
    if (!count) return 0;
    qsort(ranges, count, sizeof(*ranges), compare_cpu_range);
    size_t used = 1;
    for (size_t i = 1; i < count; i++) {
        struct cpu_range *last = &ranges[used - 1];
        if (ranges[i].start <= last->end ||
            (last->end != ULONG_MAX && ranges[i].start == last->end + 1)) {
            if (ranges[i].end > last->end) last->end = ranges[i].end;
        } else ranges[used++] = ranges[i];
    }
    return used;
}

/* cpu/mems list: numbers or N-M ranges, comma separated, e.g. 2-5,7 */
static int parse_cpu_list(const char *s, struct cpu_range **out, size_t *count) {
    struct cpu_range *ranges = NULL;
    size_t used = 0, capacity = 0;
    const char *p = s;

    if (!s || !*s) return 0;
    for (;;) {
        char *end = NULL;
        unsigned long start, finish;
        if (*p < '0' || *p > '9') goto invalid;
        errno = 0;
        start = strtoul(p, &end, 10);
        if (errno == ERANGE || end == p) goto invalid;
        p = end;
        finish = start;
        if (*p == '-') {
            p++;
            if (*p < '0' || *p > '9') goto invalid;
            errno = 0;
            finish = strtoul(p, &end, 10);
            if (errno == ERANGE || end == p || finish < start) goto invalid;
            p = end;
        }
        if (used == capacity) {
            if (capacity > SIZE_MAX / 2 / sizeof(*ranges)) goto invalid;
            size_t next = capacity ? capacity * 2 : 8;
            struct cpu_range *grown = realloc(ranges, next * sizeof(*ranges));
            if (!grown) goto invalid;
            ranges = grown;
            capacity = next;
        }
        ranges[used++] = (struct cpu_range){start, finish};
        if (*p == '\0') break;
        if (*p != ',') goto invalid;
        p++;
        if (*p == '\0') goto invalid;
    }
    *out = ranges;
    *count = merge_cpu_ranges(ranges, used);
    return 1;

invalid:
    free(ranges);
    return 0;
}

static int valid_cpu_list(const char *s) {
    struct cpu_range *ranges = NULL;
    size_t count = 0;
    int valid = parse_cpu_list(s, &ranges, &count);
    free(ranges);
    return valid;
}

static int cpu_list_subset(const char *requested, const char *parent) {
    struct cpu_range *want = NULL, *available = NULL;
    size_t want_count = 0, available_count = 0;
    int ok = 0;

    if (!parse_cpu_list(requested, &want, &want_count) ||
        !parse_cpu_list(parent, &available, &available_count))
        goto done;
    ok = 1;
    size_t j = 0;
    for (size_t i = 0; i < want_count; i++) {
        while (j < available_count && available[j].end < want[i].start) j++;
        if (j == available_count || want[i].start < available[j].start ||
            want[i].end > available[j].end) { ok = 0; break; }
    }

done:
    free(want);
    free(available);
    return ok;
}

static int cpu_list_subset_owned(const char *requested, const char *parent,
                                 const char *owned) {
    /* Both source masks are bounded kernel-file values. Concatenation forms a
     * union which the ordinary interval parser will sort and merge. */
    char available[FILE_VALUE_MAX * 2];
    int n = snprintf(available, sizeof(available), "%s%s%s", parent,
                     *parent && *owned ? "," : "", owned);
    return n >= 0 && (size_t)n < sizeof(available) &&
           cpu_list_subset(requested, available);
}

static int cpu_lists_equal(const char *left, const char *right) {
    return cpu_list_subset(left, right) && cpu_list_subset(right, left);
}

#if defined(XTOS_CGROUP_TEST_MODE) && defined(XTOS_CGROUP_TEST_CANONICAL_READBACK)
static int canonical_cpu_list(const char *input, char *output, size_t n) {
    struct cpu_range *ranges = NULL;
    size_t count = 0, used = 0;
    if (!parse_cpu_list(input, &ranges, &count)) return -1;
    for (size_t i = 0; i < count; i++) {
        int w;
        if (ranges[i].start == ranges[i].end)
            w = snprintf(output + used, n - used, "%s%lu", i ? "," : "", ranges[i].start);
        else
            w = snprintf(output + used, n - used, "%s%lu-%lu", i ? "," : "",
                         ranges[i].start, ranges[i].end);
        if (w < 0 || (size_t)w >= n - used) { free(ranges); return -1; }
        used += (size_t)w;
    }
    free(ranges);
    return 0;
}
#endif

static int valid_partition_type(const char *type) {
    return strcmp(type, "member") == 0 || strcmp(type, "root") == 0 ||
           strcmp(type, "isolated") == 0;
}

static int join_path(char *out, size_t n, const char *a, const char *b) {
    int w = snprintf(out, n, "%s/%s", a, b);
    if (w < 0 || (size_t)w >= n) return -1;
    return 0;
}

static int read_value(const char *path, const char *leaf, char *out, size_t n) {
    char file[PATH_MAX];
    if (join_path(file, sizeof(file), path, leaf) < 0 || read_file(file, out, n) < 0)
        return -1;
    chomp(out);
    return 0;
}

#ifdef XTOS_CGROUP_TEST_MODE
static int fixture_create_file(const char *path, const char *leaf, const char *value) {
    char file[PATH_MAX];
    if (join_path(file, sizeof(file), path, leaf) < 0) return -1;
    int fd = open(file, O_WRONLY | O_CREAT | O_EXCL, 0644);
    if (fd < 0) return -1;
    size_t len = strlen(value);
    int ok = write(fd, value, len) == (ssize_t)len ? 0 : -1;
    int saved = errno;
    if (close(fd) < 0) ok = -1;
    errno = saved;
    return ok;
}

static int fixture_init_child(const char *path) {
    if (fixture_create_file(path, "cpuset.cpus", "\n") < 0 ||
        fixture_create_file(path, "cpuset.mems", "\n") < 0 ||
        fixture_create_file(path, "cpuset.cpus.partition", "member\n") < 0 ||
        fixture_create_file(path, "cgroup.procs", "") < 0) return -1;
#ifndef XTOS_CGROUP_TEST_MISSING_EFFECTIVE
    if (fixture_create_file(path, "cpuset.cpus.effective", "") < 0) return -1;
#endif
    return 0;
}

static void fixture_remove_child(const char *path) {
    static const char *const files[] = {
        "cpuset.cpus", "cpuset.mems", "cpuset.cpus.effective",
        "cpuset.cpus.partition", "cgroup.procs"
    };
    char file[PATH_MAX];
    for (size_t i = 0; i < sizeof(files) / sizeof(files[0]); i++) {
        if (join_path(file, sizeof(file), path, files[i]) == 0) (void)unlink(file);
    }
    (void)rmdir(path);
}
#endif

/* A root's granted mask is its own usable CPUs plus the CPU masks held by
 * valid direct child partition roots. Compare interval unions, never expand a
 * range into one entry per CPU. */
static int effective_cpus_match(const char *path, const char *requested,
                                const char *effective, const char *type) {
    struct cpu_range *want = NULL, *all = NULL;
    size_t want_count = 0, all_count = 0;
    DIR *dir = NULL;
    int ok = 0;
    if (!parse_cpu_list(requested, &want, &want_count)) goto done;
    if (strcmp(type, "member") == 0) {
        ok = cpu_lists_equal(requested, effective);
        goto done;
    }
    if (*effective && !parse_cpu_list(effective, &all, &all_count)) goto done;
    dir = opendir(path);
    if (!dir) goto done;
    for (;;) {
        errno = 0;
        struct dirent *entry = readdir(dir);
        if (!entry) {
            if (errno) goto done;
            break;
        }
        if (entry->d_name[0] == '.') continue;
        char child[PATH_MAX], state[128], child_cpus[FILE_VALUE_MAX];
        struct stat st;
        if (join_path(child, sizeof(child), path, entry->d_name) < 0 ||
            lstat(child, &st) < 0) goto done;
        if (!S_ISDIR(st.st_mode)) continue;
        if (read_value(child, "cpuset.cpus.partition", state, sizeof(state)) < 0) goto done;
        if (strcmp(state, "root") != 0 && strcmp(state, "isolated") != 0) continue;
        struct cpu_range *child_ranges = NULL;
        size_t child_count = 0;
        if (read_value(child, "cpuset.cpus", child_cpus, sizeof(child_cpus)) < 0 ||
            !parse_cpu_list(child_cpus, &child_ranges, &child_count)) {
            free(child_ranges);
            goto done;
        }
        if (!cpu_list_subset(child_cpus, requested)) {
            free(child_ranges);
            goto done;
        }
        if (child_count > SIZE_MAX / sizeof(*all) - all_count) {
            free(child_ranges);
            goto done;
        }
        struct cpu_range *grown = realloc(all,
            (all_count + child_count) * sizeof(*all));
        if (!grown && child_count) {
            free(child_ranges);
            goto done;
        }
        all = grown;
        memcpy(all + all_count, child_ranges, child_count * sizeof(*all));
        all_count += child_count;
        free(child_ranges);
    }
    if (all_count) qsort(all, all_count, sizeof(*all), compare_cpu_range);
    for (size_t i = 1; i < all_count; i++)
        if (all[i].start <= all[i - 1].end) goto done;
    all_count = merge_cpu_ranges(all, all_count);
    if (all_count != want_count) goto done;
    ok = 1;
    for (size_t i = 0; i < all_count; i++) {
        if (all[i].start != want[i].start || all[i].end != want[i].end) {
            ok = 0;
            break;
        }
    }
done:
    if (dir) (void)closedir(dir);
    free(want);
    free(all);
    return ok;
}

#ifdef XTOS_CGROUP_TEST_MODE
static void cleanup_created_child(const char *path) {
    fixture_remove_child(path);
}
#else
static void cleanup_created_child(const char *path) {
    (void)rmdir(path);
}
#endif

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

    char root_effective[FILE_VALUE_MAX];
    char root_partition[FILE_VALUE_MAX];
    if (read_file(CGROUP_ROOT "/cpuset.cpus.effective", root_effective, sizeof(root_effective)) < 0)
        root_effective[0] = '\0';
    else chomp(root_effective);
    /* The root cgroup is always a partition root and has no partition file. */
    snprintf(root_partition, sizeof(root_partition), "root");

    /* iterate partitions (subdirectories of the root) */
    char names[64][NAME_MAX_LEN + 1];
    char cpus[64][FILE_VALUE_MAX], eff[64][FILE_VALUE_MAX];
    char partition[64][FILE_VALUE_MAX];
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
            if (read_file(q, cpus[count], sizeof(cpus[count])) < 0) cpus[count][0] = '\0';
            else chomp(cpus[count]);
            join_path(q, sizeof(q), p, "cpuset.cpus.effective");
            if (read_file(q, eff[count], sizeof(eff[count])) < 0) eff[count][0] = '\0';
            else chomp(eff[count]);
            join_path(q, sizeof(q), p, "cpuset.cpus.partition");
            if (read_file(q, partition[count], sizeof(partition[count])) < 0)
                partition[count][0] = '\0';
            else chomp(partition[count]);
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
        printf("{\"mounted\": true, \"controllers\": [%s], ", ctrl_json);
        printf("\"root\": {\"name\": \"\", \"cpus\": ");
        json_string("");
        printf(", \"effective\": ");
        json_string(root_effective);
        printf(", \"partition\": ");
        json_string(root_partition);
        printf(", \"procs\": 0}, \"partitions\": [");
        for (int i = 0; i < count; i++) {
            printf("%s{\"name\": ", i ? ", " : "");
            json_string(names[i]);
            printf(", \"cpus\": ");
            json_string(cpus[i]);
            printf(", \"effective\": ");
            json_string(eff[i]);
            printf(", \"partition\": ");
            json_string(partition[i]);
            printf(", \"procs\": %d}", procs[i]);
        }
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
            printf("  %-16s cpus=%-14s effective=%-14s partition=%-26s procs=%d\n",
                   names[i], cpus[i][0] ? cpus[i] : "(all)", eff[i][0] ? eff[i] : "(all)",
                   partition[i][0] ? partition[i] : "(unknown)", procs[i]);
        }
    }
    return 0;
}

static int do_partition(const char *name, const char *cpus, const char *mems,
                        const char *type) {
    char parent_cpus[FILE_VALUE_MAX], parent_mems[FILE_VALUE_MAX];
    char path[PATH_MAX], file[PATH_MAX], effective[FILE_VALUE_MAX], actual_type[128];
    char owned[FILE_VALUE_MAX] = "", existing_cpus[FILE_VALUE_MAX];
    int created = 0;
    struct stat st;
    if (!valid_name(name) || !valid_partition_type(type)) {
        fprintf(stderr, "XTOS_CGROUP_ERROR invalid partition name or type\n");
        return 1;
    }
    if (!valid_cpu_list(cpus) || (mems && !valid_cpu_list(mems))) {
        fprintf(stderr, "XTOS_CGROUP_ERROR invalid CPU or memory-node list\n");
        return 1;
    }
    if (!hierarchy_mounted()) {
        fprintf(stderr, "XTOS_CGROUP_ERROR cgroup2 not mounted (run: cgroup init)\n");
        return 1;
    }
    if (read_value(CGROUP_ROOT, "cpuset.cpus.effective", parent_cpus, sizeof(parent_cpus)) < 0 ||
        read_value(CGROUP_ROOT, "cpuset.mems.effective", parent_mems, sizeof(parent_mems)) < 0) {
        fprintf(stderr, "XTOS_CGROUP_ERROR cannot read parent effective CPU/memory-node set\n");
        return 1;
    }
    if (join_path(path, sizeof(path), CGROUP_ROOT, name) < 0) return 1;
    /* Parent effective excludes CPUs already lent to an existing valid target
     * partition. Read and verify that target before granting an ownership
     * exception; member/invalid partitions never get this exception. */
    if (lstat(path, &st) == 0) {
        if (!S_ISDIR(st.st_mode) ||
            read_value(path, "cpuset.cpus", existing_cpus, sizeof(existing_cpus)) < 0 ||
            read_value(path, "cpuset.cpus.effective", effective, sizeof(effective)) < 0 ||
            read_value(path, "cpuset.cpus.partition", actual_type, sizeof(actual_type)) < 0) {
            fprintf(stderr, "XTOS_CGROUP_ERROR cannot read existing CPU/partition readback\n");
            return 1;
        }
        if (strcmp(actual_type, "root") == 0 || strcmp(actual_type, "isolated") == 0) {
            if (!effective_cpus_match(path, existing_cpus, effective, actual_type)) {
                fprintf(stderr, "XTOS_CGROUP_ERROR existing partition effective CPU mismatch\n");
                return 1;
            }
            memcpy(owned, existing_cpus, strlen(existing_cpus) + 1);
        }
    } else if (errno != ENOENT) {
        fprintf(stderr, "XTOS_CGROUP_ERROR cannot inspect target partition: %s\n", strerror(errno));
        return 1;
    }
    if (!cpu_list_subset_owned(cpus, parent_cpus, owned) || !valid_cpu_list(parent_mems) ||
        (mems && !cpu_list_subset(mems, parent_mems))) {
        fprintf(stderr, "XTOS_CGROUP_ERROR request is outside parent effective CPU/memory-node set\n");
        return 1;
    }
    if (mkdir(path, 0755) == 0) {
        created = 1;
#ifdef XTOS_CGROUP_TEST_MODE
        if (fixture_init_child(path) < 0) goto preflight_failed;
#endif
    } else if (errno != EEXIST) {
        fprintf(stderr, "XTOS_CGROUP_ERROR cannot create %s: %s\n", path, strerror(errno));
        return 1;
    }
    /* Memory nodes must be set before CPUs. Failed reconfiguration is nonzero,
     * not an automatic rollback claim for existing cgroups. */
    if (join_path(file, sizeof(file), path, "cpuset.mems") < 0 ||
        write_file(file, mems ? mems : parent_mems) < 0 ||
        read_file(file, effective, sizeof(effective)) < 0) {
        fprintf(stderr, "XTOS_CGROUP_ERROR cannot set/read cpuset.mems: %s\n", strerror(errno));
        goto preflight_failed;
    }
    chomp(effective);
    if (!cpu_lists_equal(effective, mems ? mems : parent_mems)) {
        fprintf(stderr, "XTOS_CGROUP_ERROR memory-node readback mismatch\n");
        goto preflight_failed;
    }
    if (join_path(file, sizeof(file), path, "cpuset.cpus") < 0 ||
        write_file(file, cpus) < 0 || read_file(file, effective, sizeof(effective)) < 0) {
        fprintf(stderr, "XTOS_CGROUP_ERROR cannot set/read cpuset.cpus: %s\n", strerror(errno));
        goto preflight_failed;
    }
    chomp(effective);
    if (!cpu_lists_equal(effective, cpus)) {
        fprintf(stderr, "XTOS_CGROUP_ERROR requested CPU readback mismatch\n");
        goto preflight_failed;
    }
    /* Verify the usable mask before any partition state change, using the
     * current type (a root may already have delegated CPUs to child roots). */
    if (read_value(path, "cpuset.cpus.effective", effective, sizeof(effective)) < 0 ||
        read_value(path, "cpuset.cpus.partition", actual_type, sizeof(actual_type)) < 0) {
        fprintf(stderr, "XTOS_CGROUP_ERROR cannot read pre-partition effective CPU/type readback\n");
        goto preflight_failed;
    }
    const char *current_type = (strcmp(actual_type, "root") == 0 ||
                                strcmp(actual_type, "isolated") == 0) ? actual_type : "member";
    if (!effective_cpus_match(path, cpus, effective, current_type)) {
        fprintf(stderr, "XTOS_CGROUP_ERROR pre-partition effective CPU readback mismatch\n");
        goto preflight_failed;
    }
    if (join_path(file, sizeof(file), path, "cpuset.cpus.partition") < 0 ||
        write_file(file, type) < 0 || read_file(file, actual_type, sizeof(actual_type)) < 0) {
        fprintf(stderr, "XTOS_CGROUP_ERROR cannot set/read partition type: %s\n", strerror(errno));
        return 1;
    }
    chomp(actual_type);
    if (strcmp(actual_type, type) != 0) {
        fprintf(stderr, "XTOS_CGROUP_ERROR partition readback mismatch: requested=%s actual=%s\n", type, actual_type);
        return 1;
    }
    if (read_value(path, "cpuset.cpus.effective", effective, sizeof(effective)) < 0 ||
        !effective_cpus_match(path, cpus, effective, actual_type)) {
        fprintf(stderr, "XTOS_CGROUP_ERROR effective CPU readback mismatch after partition change\n");
        return 1;
    }
    printf("XTOS_CGROUP_PARTITION name=%s cpus=%s effective=%s partition=%s parent_effective=%s\n",
           name, cpus, effective, actual_type, parent_cpus);
    return 0;

preflight_failed:
    if (created) cleanup_created_child(path);
    return 1;
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
    if (read_file(cg, cg, sizeof(cg)) != 0) {
        fprintf(stderr, "XTOS_CGROUP_ERROR cannot read /proc/%ld/cgroup: %s\n",
                pid, strerror(errno));
        return 1;
    }
    char want[64];
    snprintf(want, sizeof(want), "0::/%s\n", name);
    if (!strstr(cg, want)) {
        fprintf(stderr, "XTOS_CGROUP_ERROR pin not confirmed by /proc/%ld/cgroup\n", pid);
        return 1;
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
        const char *name = NULL, *cpus = NULL, *mems = NULL, *type = "member";
        for (int i = 2; i < argc; i++) {
            if (strcmp(argv[i], "--cpus") == 0 && i + 1 < argc) cpus = argv[++i];
            else if (strcmp(argv[i], "--mems") == 0 && i + 1 < argc) mems = argv[++i];
            else if (strcmp(argv[i], "--partition") == 0 && i + 1 < argc) type = argv[++i];
            else if (strncmp(argv[i], "--partition=", 12) == 0 && argv[i][12] != '\0') type = argv[i] + 12;
            else if (!name && argv[i][0] != '-') name = argv[i];
            else { usage(); return 2; }
        }
        if (!name || !cpus) { usage(); return 2; }
        if (!valid_partition_type(type)) { usage(); return 2; }
        return do_partition(name, cpus, mems, type);
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
