/* Opt-in ext4 persistence for /home and /root only. No shell is invoked.
 * setup formats a dedicated, unused partition; marker only writes its marker.
 * Both writes require root and an exact trailing --confirm argument.
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/fs.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/sysmacros.h>
#include <sys/wait.h>
#include <unistd.h>
#include <limits.h>
#include "xtos_ui.h"

/* Compile-time roots allow isolated proc/sys fixtures. Device validation and
 * destructive syscalls are never relaxed in the production binary. */
#ifndef XTOS_PROC_ROOT
#define XTOS_PROC_ROOT "/proc"
#endif
#ifndef XTOS_SYS_ROOT
#define XTOS_SYS_ROOT "/sys"
#endif
#ifndef XTOS_DEV_ROOT
#define XTOS_DEV_ROOT "/dev"
#endif
#ifndef XTOS_TMP_ROOT
#define XTOS_TMP_ROOT "/run"
#endif
#ifndef XTOS_MKFS_PATH
#define XTOS_MKFS_PATH "/usr/sbin/mkfs.ext4"
#endif
#define PERSIST_LABEL "XTOS-PERSIST"
#define PERSIST_MARKER "XTOS-PERSIST-1"
#define MARKER_FILE "XTOS_PERSIST_ID"

struct mount_entry {
    dev_t dev;
    char root[PATH_MAX];
    char point[PATH_MAX];
};
struct mounts {
    struct mount_entry *entries;
    size_t count;
};
struct block_entry {
    dev_t dev;
    char node[PATH_MAX];
};
struct blocks {
    struct block_entry *entries;
    size_t count;
};
struct device {
    int fd;
    dev_t dev;
    char path[PATH_MAX];
    char node[PATH_MAX];
    char parent[PATH_MAX];
};

static int fail(const char *message) {
    fprintf(stderr, "persist: %s\n", message);
    return -1;
}

static int path_join(char *out, const char *a, const char *b) {
    int n = snprintf(out, PATH_MAX, "%s/%s", a, b);
    if (n < 0 || n >= PATH_MAX) return fail("path too long");
    return 0;
}

static int beneath(const char *path, const char *base) {
    size_t n = strlen(base);
    return strncmp(path, base, n) == 0 && (path[n] == '/' || path[n] == '\0');
}

static int related(const char *a, const char *b) {
    return beneath(a, b) || beneath(b, a);
}

static int traversal(const char *path) {
    const char *p = path;
    while (*p) {
        while (*p == '/') p++;
        const char *start = p;
        while (*p && *p != '/') p++;
        if (p - start == 2 && start[0] == '.' && start[1] == '.') return 1;
    }
    return 0;
}

/* mountinfo and swaps use octal escapes for whitespace and backslashes. */
static int unescape(char *out, const char *in) {
    size_t n = 0;
    while (*in) {
        unsigned char c = (unsigned char)*in++;
        if (c == '\\') {
            if (strlen(in) < 3 || in[0] < '0' || in[0] > '7' ||
                in[1] < '0' || in[1] > '7' || in[2] < '0' || in[2] > '7')
                return fail("invalid proc path escape");
            unsigned int value = (unsigned int)(in[0] - '0') * 64U +
                                 (unsigned int)(in[1] - '0') * 8U +
                                 (unsigned int)(in[2] - '0');
            if (!value || value > 255) return fail("invalid proc path escape");
            c = (unsigned char)value;
            in += 3;
        }
        if (n + 1 >= PATH_MAX) return fail("proc path too long");
        out[n++] = (char)c;
    }
    out[n] = '\0';
    return 0;
}

static int parse_dev(const char *text, dev_t *dev) {
    unsigned int ma, mi;
    char extra;
    if (sscanf(text, "%u:%u %c", &ma, &mi, &extra) != 2) return -1;
    *dev = makedev(ma, mi);
    return major(*dev) == ma && minor(*dev) == mi ? 0 : -1;
}

static int read_line(const char *path, char *out, size_t size) {
    FILE *f = fopen(path, "re");
    if (!f) return -1;
    int rc = fgets(out, (int)size, f) ? 0 : -1;
    if (!rc && !strchr(out, '\n') && !feof(f)) rc = -1;
    if (ferror(f)) rc = -1;
    if (fclose(f) != 0) rc = -1;
    if (!rc) out[strcspn(out, "\r\n")] = '\0';
    return rc;
}

static int load_mounts(struct mounts *mounts) {
    FILE *f = fopen(XTOS_PROC_ROOT "/self/mountinfo", "re");
    if (!f) return fail("cannot read mountinfo; refusing unknown mount state");
    char *line = NULL;
    size_t cap = 0;
    int rc = 0;
    while (getline(&line, &cap, f) >= 0) {
        char *save = NULL;
        char *id = strtok_r(line, " \t\n", &save);
        char *parent = strtok_r(NULL, " \t\n", &save);
        char *device = strtok_r(NULL, " \t\n", &save);
        char *root = strtok_r(NULL, " \t\n", &save);
        char *point = strtok_r(NULL, " \t\n", &save);
        char *options = strtok_r(NULL, " \t\n", &save);
        struct mount_entry entry;
        if (!id || !parent || !device || !root || !point || !options ||
            parse_dev(device, &entry.dev) < 0 || unescape(entry.root, root) < 0 ||
            unescape(entry.point, point) < 0 || entry.point[0] != '/') {
            rc = fail("malformed mountinfo; refusing unknown mount state");
            break;
        }
        char *token;
        do { token = strtok_r(NULL, " \t\n", &save); }
        while (token && strcmp(token, "-") != 0);
        if (!token || !strtok_r(NULL, " \t\n", &save) ||
            !strtok_r(NULL, " \t\n", &save) || !strtok_r(NULL, " \t\n", &save)) {
            rc = fail("malformed mountinfo; refusing unknown mount state");
            break;
        }
        struct mount_entry *next = realloc(mounts->entries, (mounts->count + 1) * sizeof(entry));
        if (!next) { rc = fail("out of memory"); break; }
        mounts->entries = next;
        mounts->entries[mounts->count++] = entry;
    }
    if (ferror(f)) rc = fail("cannot finish reading mountinfo");
    free(line);
    if (fclose(f) != 0) rc = fail("cannot close mountinfo");
    if (!rc && !mounts->count) rc = fail("empty mountinfo; refusing unknown mount state");
    return rc;
}

static const struct mount_entry *mountpoint(const struct mounts *mounts, const char *point) {
    /* The last visible record handles stacked mounts at the same mountpoint. */
    const struct mount_entry *found = NULL;
    for (size_t i = 0; i < mounts->count; i++)
        if (strcmp(mounts->entries[i].point, point) == 0) found = &mounts->entries[i];
    return found;
}

static int load_blocks(struct blocks *blocks) {
    struct dirent **names = NULL;
    int count = scandir(XTOS_SYS_ROOT "/class/block", &names, NULL, alphasort);
    if (count < 0) return fail("cannot enumerate sysfs block devices");
    int rc = 0;
    for (int i = 0; i < count; i++) {
        if (!rc && names[i]->d_name[0] != '.') {
            char path[PATH_MAX], devfile[PATH_MAX], text[128];
            struct block_entry entry;
            if (path_join(path, XTOS_SYS_ROOT "/class/block", names[i]->d_name) < 0 ||
                !realpath(path, entry.node) || path_join(devfile, entry.node, "dev") < 0 ||
                read_line(devfile, text, sizeof(text)) < 0 || parse_dev(text, &entry.dev) < 0) {
                rc = fail("cannot identify sysfs block device");
            } else {
                struct block_entry *next = realloc(blocks->entries, (blocks->count + 1) * sizeof(entry));
                if (!next) rc = fail("out of memory");
                else {
                    blocks->entries = next;
                    blocks->entries[blocks->count++] = entry;
                }
            }
        }
        free(names[i]);
    }
    free(names);
    return rc;
}

static int partition(const char *node) {
    char path[PATH_MAX], text[128], extra;
    unsigned long n;
    if (path_join(path, node, "partition") < 0) return 0;
    return read_line(path, text, sizeof(text)) == 0 &&
           sscanf(text, "%lu %c", &n, &extra) == 1 && n > 0;
}

static int device_open(struct device *device, const char *input) {
    if (!beneath(input, XTOS_DEV_ROOT) || traversal(input) ||
        !realpath(input, device->path) || !beneath(device->path, XTOS_DEV_ROOT))
        return fail("target must resolve to a block device under /dev/ without traversal");
    device->fd = open(device->path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (device->fd < 0) return fail("cannot open target device");
    struct stat st;
    if (fstat(device->fd, &st) < 0 || !S_ISBLK(st.st_mode))
        return fail("target is not a block device");
    device->dev = st.st_rdev;
    char id[64], path[PATH_MAX], devfile[PATH_MAX], text[128];
    dev_t sysdev;
    snprintf(id, sizeof(id), "%u:%u", major(device->dev), minor(device->dev));
    if (path_join(path, XTOS_SYS_ROOT "/dev/block", id) < 0 ||
        !realpath(path, device->node) || path_join(devfile, device->node, "dev") < 0 ||
        read_line(devfile, text, sizeof(text)) < 0 || parse_dev(text, &sysdev) < 0 || sysdev != device->dev)
        return fail("target does not match its sysfs device identity");
    if (!partition(device->node)) return fail("target must be a dedicated partition, not a whole disk");
    memcpy(device->parent, device->node, strlen(device->node) + 1);
    char *slash = strrchr(device->parent, '/');
    if (!slash) return fail("cannot identify target parent disk");
    *slash = '\0';
    if (flock(device->fd, LOCK_EX | LOCK_NB) < 0) return fail("target is locked by another operation");
    uint64_t bytes = 0;
    int readonly = 1;
    if (ioctl(device->fd, BLKGETSIZE64, &bytes) < 0 || !bytes ||
        ioctl(device->fd, BLKROGET, &readonly) < 0)
        return fail("cannot determine target capacity/read-only state");
    if (readonly) return fail("target is read-only");
    return 0;
}

static int dev_related(dev_t dev, const struct device *device, const struct blocks *blocks) {
    if (dev == device->dev) return 1;
    for (size_t i = 0; i < blocks->count; i++)
        if (blocks->entries[i].dev == dev && related(blocks->entries[i].node, device->node)) return 1;
    return 0;
}

static int check_swaps(const struct device *device, const struct blocks *blocks) {
    FILE *f = fopen(XTOS_PROC_ROOT "/swaps", "re");
    if (!f) return fail("cannot read active swap state");
    char *line = NULL;
    size_t cap = 0;
    int rc = 0;
    if (getline(&line, &cap, f) < 0 || strncmp(line, "Filename", 8) != 0)
        rc = fail("malformed active swap state");
    while (!rc && getline(&line, &cap, f) >= 0) {
        char *save = NULL, path[PATH_MAX];
        char *name = strtok_r(line, " \t\n", &save);
        if (!name || !strtok_r(NULL, " \t\n", &save) || !strtok_r(NULL, " \t\n", &save) ||
            !strtok_r(NULL, " \t\n", &save) || !strtok_r(NULL, " \t\n", &save) || unescape(path, name) < 0) {
            rc = fail("malformed active swap state");
            break;
        }
        struct stat st;
        if (stat(path, &st) < 0) { rc = fail("cannot identify active swap backing device"); break; }
        if (dev_related(S_ISBLK(st.st_mode) ? st.st_rdev : st.st_dev, device, blocks))
            rc = fail("target or an ancestor/descendant backs active swap");
    }
    if (ferror(f)) rc = fail("cannot finish reading active swap state");
    free(line);
    if (fclose(f) != 0) rc = fail("cannot close active swap state");
    return rc;
}

static int check_unused(const struct device *device) {
    struct mounts mounts = {0};
    struct blocks blocks = {0};
    int rc = -1;
    struct stat st;
    if (stat(device->path, &st) < 0 || !S_ISBLK(st.st_mode) || st.st_rdev != device->dev)
        return fail("target device identity changed");
    if (load_mounts(&mounts) < 0 || load_blocks(&blocks) < 0) goto out;
    if (mountpoint(&mounts, "/run/persist")) {
        fail("a persistence volume is already mounted");
        goto out;
    }
    for (size_t i = 0; i < mounts.count; i++) {
        const struct mount_entry *m = &mounts.entries[i];
        /* Same-medium persistence: the mounted Live partition itself and any
         * partition of a whole-disk Live medium (superfloppy/loop image) stay
         * protected. A DIFFERENT partition of a partitioned Live disk is a
         * valid target: the running system never writes it, and xtos.toram
         * further decouples the session from the medium. */
        if (beneath(m->point, "/run/live-media") || beneath(m->point, "/run/live-lower")) {
            if (m->dev == device->dev) {
                fail("target is the mounted Live medium itself");
                goto out;
            }
            int contained = 0;
            for (size_t j = 0; j < blocks.count; j++) {
                if (blocks.entries[j].dev == m->dev &&
                    related(blocks.entries[j].node, device->node)) {
                    contained = 1;
                    break;
                }
            }
            if (contained) {
                fail("target belongs to the mounted whole-disk Live medium");
                goto out;
            }
            continue; /* sibling partition of a partitioned Live disk: allowed */
        }
        if (dev_related(m->dev, device, &blocks)) {
            fail("target or an ancestor/descendant is mounted");
            goto out;
        }
    }
    for (size_t i = 0; i < blocks.count; i++) {
        if (!related(blocks.entries[i].node, device->node)) continue;
        char path[PATH_MAX];
        if (path_join(path, blocks.entries[i].node, "holders") < 0) goto out;
        DIR *d = opendir(path);
        if (!d) { fail("cannot inspect block device holders"); goto out; }
        struct dirent *e;
        int held = 0;
        errno = 0;
        while ((e = readdir(d))) {
            if (strcmp(e->d_name, ".") != 0 && strcmp(e->d_name, "..") != 0) { held = 1; break; }
        }
        int readerr = errno;
        int closeerr = closedir(d);
        if (held || readerr || closeerr) {
            fail(held ? "target or an ancestor/descendant has active holders" : "cannot read block device holders");
            goto out;
        }
    }
    if (check_swaps(device, &blocks) < 0) goto out;
    /* The kernel's exclusive-open check supplements (never replaces) proc/sys
     * checks, and is released before mkfs opens the device itself. */
    int exclusive = open(device->path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK | O_EXCL);
    if (exclusive < 0) { fail("target is busy or cannot be claimed exclusively"); goto out; }
    if (fstat(exclusive, &st) < 0 || !S_ISBLK(st.st_mode) || st.st_rdev != device->dev) {
        close(exclusive);
        fail("target device identity changed during exclusive check");
        goto out;
    }
    if (close(exclusive) < 0) { fail("cannot close exclusive device probe"); goto out; }
    rc = 0;
out:
    free(mounts.entries);
    free(blocks.entries);
    return rc;
}

static int ext4_label(int fd) {
    unsigned char sb[1024];
    ssize_t n;
    do { n = pread(fd, sb, sizeof(sb), 1024); } while (n < 0 && errno == EINTR);
    /* ext2/ext3 share the magic; require the ext4 extents incompat feature. */
    if (n != (ssize_t)sizeof(sb) || sb[0x38] != 0x53 || sb[0x39] != 0xef || !(sb[0x60] & 0x40))
        return 0;
    char label[17];
    memcpy(label, sb + 0x78, 16);
    label[16] = '\0';
    return strcmp(label, PERSIST_LABEL) == 0;
}

static int format_device(const struct device *device) {
    pid_t pid = fork();
    if (pid < 0) return fail("cannot fork mkfs.ext4");
    if (!pid) {
        char *const args[] = {"mkfs.ext4", "-F", "-L", PERSIST_LABEL, (char *)device->path, NULL};
        char *const env[] = {"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL=C", NULL};
        execve(XTOS_MKFS_PATH, args, env);
        _exit(127);
    }
    int status;
    pid_t waited;
    do { waited = waitpid(pid, &status, 0); } while (waited < 0 && errno == EINTR);
    if (waited != pid) return fail("cannot wait for mkfs.ext4");
    if (!WIFEXITED(status)) return fail("mkfs.ext4 terminated by signal");
    if (WEXITSTATUS(status) == 127) return fail("mkfs.ext4 not available in this image");
    if (WEXITSTATUS(status)) return fail("mkfs.ext4 failed");
    return 0;
}

static int directory_at(int fd, const char *name, mode_t mode) {
    if (mkdirat(fd, name, mode) < 0 && errno != EEXIST) return fail("cannot create persistence directory");
    int child = openat(fd, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (child < 0) return fail("persistence directory is inaccessible or a symlink");
    int rc = fsync(child);
    if (close(child) < 0) rc = -1;
    return rc < 0 ? fail("cannot sync persistence directory") : 0;
}

static int write_marker(int dirfd) {
    struct stat st;
    if (fstatat(dirfd, MARKER_FILE, &st, AT_SYMLINK_NOFOLLOW) == 0) {
        if (!S_ISREG(st.st_mode) || st.st_nlink != 1) return fail("existing marker is not a private regular file");
    } else if (errno != ENOENT) return fail("cannot inspect existing marker");
    if (directory_at(dirfd, "home", 0755) < 0 || directory_at(dirfd, "root", 0700) < 0) return -1;
    /* Never alter root/.ssh or overwrite the user's authorized_keys. The boot
     * init seeds its image key only when authorized_keys is missing or empty. */
    char temporary[80];
    snprintf(temporary, sizeof(temporary), ".XTOS_PERSIST_ID.tmp.%ld", (long)getpid());
    int fd = openat(dirfd, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0644);
    if (fd < 0) return fail("cannot create temporary marker");
    const char contents[] = PERSIST_MARKER "\n";
    size_t done = 0;
    int rc = 0;
    while (done < sizeof(contents) - 1) {
        ssize_t n = write(fd, contents + done, sizeof(contents) - 1 - done);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { rc = fail("cannot write marker"); break; }
        done += (size_t)n;
    }
    if (!rc && fsync(fd) < 0) rc = fail("cannot fsync marker");
    if (close(fd) < 0) rc = fail("cannot close marker");
    if (!rc && renameat(dirfd, temporary, dirfd, MARKER_FILE) < 0) rc = fail("cannot install marker");
    if (rc) unlinkat(dirfd, temporary, 0);
    if (!rc && (fsync(dirfd) < 0 || syncfs(dirfd) < 0)) rc = fail("cannot sync persistence filesystem");
    return rc;
}

static int mount_and_mark(const struct device *device) {
    char probe[PATH_MAX];
    if (path_join(probe, XTOS_TMP_ROOT, "xtos-persist.XXXXXX") < 0) return -1;
    if (!mkdtemp(probe)) return fail("cannot create private mount directory");
    int rc = -1;
    if (mount(device->path, probe, "ext4", MS_NODEV | MS_NOSUID | MS_NOEXEC, NULL) < 0) {
        fail("cannot mount target as ext4 to write marker");
        if (rmdir(probe) < 0) fail("cannot remove private mount directory");
        return -1;
    }
    int fd = open(probe, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) fail("cannot open mounted persistence directory");
    else {
        rc = write_marker(fd);
        if (close(fd) < 0) rc = fail("cannot close persistence directory");
    }
    int unmounted;
    do { unmounted = umount2(probe, 0); } while (unmounted < 0 && errno == EINTR);
    if (unmounted < 0) {
        fprintf(stderr, "persist: cannot unmount %s; volume remains mounted, manual cleanup required\n", probe);
        return -1;
    }
    if (rmdir(probe) < 0) rc = fail("cannot remove private mount directory");
    return rc;
}

#ifndef XTOS_CRYPTSETUP_PATH
#define XTOS_CRYPTSETUP_PATH "/usr/sbin/cryptsetup"
#endif

/* LUKS magic "LUKS\xba\xbe" + version 2 at offset 6. */
static int luks_detected(int fd) {
    unsigned char hdr[8];
    ssize_t n;
    do { n = pread(fd, hdr, sizeof(hdr), 0); } while (n < 0 && errno == EINTR);
    return n == (ssize_t)sizeof(hdr) && hdr[0] == 'L' && hdr[1] == 'U' && hdr[2] == 'K' &&
           hdr[3] == 'S' && hdr[4] == 0xba && hdr[5] == 0xbe && (hdr[6] == 1 || hdr[6] == 2);
}

static int run_cryptsetup(char *const args[]) {
    pid_t pid = fork();
    if (pid < 0) return fail("cannot fork cryptsetup");
    if (!pid) {
        char *const env[] = {"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL=C", NULL};
        execve(XTOS_CRYPTSETUP_PATH, args, env);
        _exit(127);
    }
    int status;
    pid_t waited;
    do { waited = waitpid(pid, &status, 0); } while (waited < 0 && errno == EINTR);
    if (waited != pid) return fail("cannot wait for cryptsetup");
    if (!WIFEXITED(status)) return fail("cryptsetup terminated by signal");
    if (WEXITSTATUS(status) == 127) return fail("cryptsetup not available in this image");
    if (WEXITSTATUS(status)) return fail("cryptsetup failed");
    return 0;
}

/* Open a LUKS volume as /dev/mapper/<name>. The passphrase comes from the
 * controlling terminal: cryptsetup reads it interactively. We never accept
 * passphrases as argv (process listings would leak them). */
static int luks_open(const char *device, const char *name) {
    char dev_buf[PATH_MAX], name_buf[64];
    snprintf(dev_buf, sizeof(dev_buf), "%s", device);
    snprintf(name_buf, sizeof(name_buf), "%s", name);
    char *const open_args[] = {"cryptsetup", "open", "--type", "luks", dev_buf, name_buf, NULL};
    return run_cryptsetup(open_args);
}

static int luks_close(const char *name) {
    char name_buf[64];
    snprintf(name_buf, sizeof(name_buf), "%s", name);
    char *const close_args[] = {"cryptsetup", "close", name_buf, NULL};
    return run_cryptsetup(close_args);
}

static int cmd_luks(const char *input, const char *action) {
    /* persist luks <dev> open|close --confirm: explicit gate on the mapping
     * operation too, because opening changes the device-visible state. */
    if (geteuid() != 0) { fail("luks requires root"); return 1; }
    struct device device = {.fd = -1};
    int rc = 1;
    if (device_open(&device, input) < 0 || check_unused(&device) < 0) goto out;
    if (!luks_detected(device.fd)) {
        fail("target does not carry a LUKS header");
        goto out;
    }
    char name[64];
    snprintf(name, sizeof(name), "xtos-persist-%d", (int)(major(device.dev) * 1000 + minor(device.dev) % 1000));
    if (strcmp(action, "open") == 0) {
        if (luks_open(device.path, name) < 0) goto out;
        printf("PERSIST_LUKS device=%s action=open mapper=/dev/mapper/%s\n", device.path, name);
        printf("PERSIST_NEXT persist marker /dev/mapper/%s --confirm (then boot normally)\n", name);
    } else {
        if (luks_close(name) < 0) goto out;
        printf("PERSIST_LUKS device=%s action=close mapper=/dev/mapper/%s\n", device.path, name);
    }
    rc = 0;
out:
    if (device.fd >= 0 && close(device.fd) < 0) { fail("cannot close target device"); rc = 1; }
    return rc;
}

static int cmd_setup(const char *input, int marker_only) {
    if (geteuid() != 0) { fail("setup and marker require root"); return 1; }
    struct device device = {.fd = -1};
    int rc = 1;
    if (device_open(&device, input) < 0 || check_unused(&device) < 0) goto out;
    if (!marker_only) {
        printf("PERSIST_FORMAT device=%s label=%s\n", device.path, PERSIST_LABEL);
        fflush(stdout);
        if (format_device(&device) < 0) goto out;
        /* Re-read occupancy after the formatter, before the writable mount. */
        if (check_unused(&device) < 0) goto out;
    }
    if (!ext4_label(device.fd)) {
        fail("target must be ext4 with label XTOS-PERSIST");
        goto out;
    }
    if (mount_and_mark(&device) < 0) goto out;
    rc = 0;
out:
    if (device.fd >= 0 && close(device.fd) < 0) { fail("cannot close target device"); rc = 1; }
    if (!rc) {
        printf("PERSIST_SETUP device=%s label=%s marker=%s\n", device.path, PERSIST_LABEL, PERSIST_MARKER);
        printf("PERSIST_NEXT add xtos.persist=%s to the kernel command line\n", PERSIST_LABEL);
    }
    return rc;
}

static void json_string(const char *s) {
    putchar('"');
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') printf("\\%c", c);
        else if (c < 0x20 || c >= 0x7f) printf("\\u%04x", c);
        else putchar(c);
    }
    putchar('"');
}

static int cmd_status(int json) {
    struct mounts mounts = {0};
    struct blocks blocks = {0};
    char **candidates = NULL;
    int *candidate_luks = NULL;
    uint64_t *candidate_bytes = NULL;
    size_t count = 0;
    int rc = 1;
    if (load_mounts(&mounts) < 0 || load_blocks(&blocks) < 0) goto out;
    for (size_t i = 0; i < blocks.count; i++) {
        if (!partition(blocks.entries[i].node)) continue;
        char devpath[PATH_MAX];
        const char *name = strrchr(blocks.entries[i].node, '/');
        if (!name || path_join(devpath, XTOS_DEV_ROOT, name + 1) < 0) goto out;
        int fd = open(devpath, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
        if (fd < 0) continue; /* Unprivileged status may not read every device. */
        struct stat st;
        int luks = luks_detected(fd);
        int match = fstat(fd, &st) == 0 && S_ISBLK(st.st_mode) &&
                    st.st_rdev == blocks.entries[i].dev && (ext4_label(fd) || luks);
        uint64_t bytes = 0;
        if (match && ioctl(fd, BLKGETSIZE64, &bytes) < 0) { close(fd); match = 0; }
        if (close(fd) < 0) { fail("cannot close candidate device"); goto out; }
        if (!match) continue;
        char **next = realloc(candidates, (count + 1) * sizeof(*candidates));
        int *next_luks = realloc(candidate_luks, (count + 1) * sizeof(*candidate_luks));
        uint64_t *next_bytes = realloc(candidate_bytes, (count + 1) * sizeof(*candidate_bytes));
        if (!next || !next_luks || !next_bytes) { fail("out of memory"); goto out; }
        candidates = next;
        candidate_luks = next_luks;
        candidate_bytes = next_bytes;
        candidates[count] = strdup(devpath);
        if (!candidates[count]) { fail("out of memory"); goto out; }
        candidate_luks[count] = luks;
        candidate_bytes[count] = bytes;
        count++;
    }
    const struct mount_entry *persist = mountpoint(&mounts, "/run/persist");
    const struct mount_entry *home = mountpoint(&mounts, "/home");
    const struct mount_entry *root = mountpoint(&mounts, "/root");
    char home_root[PATH_MAX], root_root[PATH_MAX];
    int home_ok = 0, root_ok = 0;
    if (persist) {
        const char *base = strcmp(persist->root, "/") == 0 ? "" : persist->root;
        if (path_join(home_root, base, "home") < 0 || path_join(root_root, base, "root") < 0) goto out;
        home_ok = home && home->dev == persist->dev && strcmp(home->root, home_root) == 0;
        root_ok = root && root->dev == persist->dev && strcmp(root->root, root_root) == 0;
    }
    int active = persist && home_ok && root_ok;
    long long avail_mb = -1, total_mb = -1;
    int capacity_pct = -1;
    if (persist) {
        struct statfs vfs;
        if (statfs("/run/persist", &vfs) == 0 && vfs.f_blocks) {
            total_mb = (long long)((unsigned long long)vfs.f_blocks * vfs.f_bsize / (1024 * 1024));
            avail_mb = (long long)((unsigned long long)vfs.f_bavail * vfs.f_bsize / (1024 * 1024));
            capacity_pct = (int)(100 * ((unsigned long long)vfs.f_blocks - vfs.f_bfree) / (unsigned long long)vfs.f_blocks);
        }
    }
    if (json) {
        printf("{\"label\":\"%s\",\"marker\":\"%s\",\"active\":%s,\"persistence\":%d,"
               "\"mounted\":%s,\"home_persistent\":%s,\"root_persistent\":%s,\"candidates\":[",
               PERSIST_LABEL, PERSIST_MARKER, active ? "true" : "false", active,
               persist ? "true" : "false", home_ok ? "true" : "false", root_ok ? "true" : "false");
        for (size_t i = 0; i < count; i++) {
            if (i) putchar(',');
            printf("{\"device\":"); json_string(candidates[i]);
            if (candidate_luks[i]) printf(",\"luks\":true");
            else printf(",\"label\":\"%s\",\"filesystem\":\"ext4\"", PERSIST_LABEL);
            printf(",\"bytes\":%llu", (unsigned long long)candidate_bytes[i]);
            putchar('}');
        }
        printf("],\"scope\":[\"/home\",\"/root\"]");
        if (active) printf(",\"usage_percent\":%d,\"total_mb\":%lld,\"avail_mb\":%lld", capacity_pct, total_mb, avail_mb);
        printf("}\n");
    } else {
        xui_init();
        XUI_TITLE("XTOS Persist Status\n");
        XUI_LABEL("Mode:      ");
        if (active) XUI_OK("ACTIVE (persistence=%d)\n", active);
        else if (persist) XUI_WARN("INCOMPLETE (persistence=%d)\n", active);
        else XUI_DIM("RAM-only (persistence=%d)\n", active);
        XUI_LABEL("Home/root: ");
        printf("%s/%s\n", home_ok ? "persistent" : "volatile", root_ok ? "persistent" : "volatile");
        if (active && capacity_pct >= 0) {
            XUI_LABEL("Usage:     ");
            printf("%d%% (%lldMB free of %lldMB)\n", capacity_pct, avail_mb, total_mb);
            if (capacity_pct >= 95) XUI_BAD("WARNING:   persistence volume is >=95%% full; writes may fail\n");
            else if (capacity_pct >= 85) XUI_WARN("WARNING:   persistence volume is >=85%% full\n");
        }
        XUI_LABEL("Candidates:\n");
        for (size_t i = 0; i < count; i++) {
            printf("  %s ", candidates[i]);
            if (candidate_luks[i]) XUI_ACC("luks=1 (run: persist luks <dev> open --confirm)");
            else printf("label=%s", PERSIST_LABEL);
            printf(" size=%lluMB\n", (unsigned long long)(candidate_bytes[i] / (1024 * 1024)));
        }
        if (!count) XUI_DIM("  (none; run: persist setup <partition> --confirm)\n");
        XUI_LABEL("Enable:    ");
        XUI_ACC("add xtos.persist=%s to the kernel command line\n", PERSIST_LABEL);
    }
    rc = 0;
out:
    for (size_t i = 0; i < count; i++) free(candidates[i]);
    free(candidates);
    free(candidate_luks);
    free(candidate_bytes);
    free(mounts.entries);
    free(blocks.entries);
    return rc;
}

static void usage(void) {
    fprintf(stderr,
        "usage: persist status [--json]\n"
        "       persist setup <partition> --confirm\n"
        "       persist marker <partition> --confirm\n"
        "       persist luks <partition> open|close --confirm\n"
        "setup DESTROYS ALL DATA; marker writes only to existing ext4 labeled XTOS-PERSIST.\n"
        "Both writes require root, an unused partition, and explicit --confirm.\n"
        "Persistence covers /home and /root only. The system image stays immutable.\n"
        "Boot with xtos.persist=XTOS-PERSIST to activate.\n");
}

int main(int argc, char **argv) {
    if (argc >= 2 && strcmp(argv[1], "status") == 0) {
        if (argc == 2) return cmd_status(0);
        if (argc == 3 && strcmp(argv[2], "--json") == 0) return cmd_status(1);
    } else if (argc >= 2 && (strcmp(argv[1], "setup") == 0 || strcmp(argv[1], "marker") == 0)) {
        if (argc == 4 && strcmp(argv[3], "--confirm") == 0)
            return cmd_setup(argv[2], strcmp(argv[1], "marker") == 0);
        if (argc == 3) fprintf(stderr, "persist: explicit --confirm is required\n");
    } else if (argc == 5 && strcmp(argv[1], "luks") == 0 &&
               (strcmp(argv[3], "open") == 0 || strcmp(argv[3], "close") == 0) &&
               strcmp(argv[4], "--confirm") == 0) {
        return cmd_luks(argv[2], argv[3]);
    } else if (argc == 4 && strcmp(argv[1], "luks") == 0 &&
               (strcmp(argv[3], "open") == 0 || strcmp(argv[3], "close") == 0)) {
        fprintf(stderr, "persist: explicit --confirm is required\n");
    }
    usage();
    return 2;
}
