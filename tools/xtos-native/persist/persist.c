/* persist: XTOS opt-in persistence management (C, static).
 * Live boots are RAM-only by default. Users who want their /home and /root to
 * survive reboots run `persist setup` ONCE on a dedicated partition:
 *   1. mkfs.ext4 with a fixed XTOS label
 *   2. write the XTOS_PERSIST_ID marker (the boot init only accepts marked partitions)
 * and then boot with xtos.persist=<label> on the kernel command line.
 *
 * Commands:
 *   persist status          show current persistence state + labeled candidates
 *   persist setup <dev>     DANGEROUS: mkfs.ext4 + label + marker on <dev> (--confirm)
 *   persist marker <dev>    write only the marker on an existing ext4 (no format)
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/wait.h>

#define PERSIST_LABEL "XTOS-PERSIST"
#define PERSIST_MARKER "XTOS-PERSIST-1"

static int run_cmd(const char *path, const char *arg) {
    pid_t pid = fork();
    if (pid < 0) return -1;
    if (pid == 0) {
        execl(path, path, arg, (char *)NULL);
        _exit(127);
    }
    int status;
    waitpid(pid, &status, 0);
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

static int mountpoint_is(const char *dir) {
    /* /proc/self/mounts contains a line starting with the dir */
    FILE *f = fopen("/proc/self/mounts", "r");
    if (!f) return 0;
    char line[1024];
    size_t dlen = strlen(dir);
    int found = 0;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, dir, dlen) == 0 && (line[dlen] == ' ' || line[dlen] == '\t')) {
            found = 1;
            break;
        }
    }
    fclose(f);
    return found;
}

static int valid_device(const char *dev) {
    /* canonical whole-device or partition path under /dev/, no traversal */
    if (strncmp(dev, "/dev/", 5) != 0) return 0;
    if (strstr(dev, "..")) return 0;
    struct stat st;
    if (stat(dev, &st) < 0) return 0;
    return S_ISBLK(st.st_mode);
}

static int cmd_status(void) {
    printf("XTOS Persist Status\n");
    if (mountpoint_is("/run/persist")) {
        printf("Mode:      ACTIVE (mounted at /run/persist)\n");
        printf("Home/root: persistent (bind-mounted)\n");
    } else {
        printf("Mode:      RAM-only (persistence=0)\n");
        printf("Home/root: volatile (lost on reboot)\n");
    }
    /* list labeled candidate partitions */
    printf("Candidates:\n");
    DIR *d = opendir("/sys/class/block");
    int n = 0;
    if (d) {
        struct dirent *e;
        char dev[512], line[512];
        while ((e = readdir(d))) {
            if (e->d_name[0] == '.') continue;
            snprintf(dev, sizeof(dev), "/dev/%s", e->d_name);
            char cmd[600];
            snprintf(cmd, sizeof(cmd), "/usr/libexec/xtos/busybox blkid %s 2>/dev/null", dev);
            FILE *p = popen(cmd, "r");
            if (!p) continue;
            if (fgets(line, sizeof(line), p)) {
                char *nl = strchr(line, '\n');
                if (nl) *nl = '\0';
                /* busybox blkid prints the full ID line: extract LABEL="..." */
                char want[128];
                snprintf(want, sizeof(want), "LABEL=\"%s\"", PERSIST_LABEL);
                if (strstr(line, want)) {
                    printf("  %s label=%s\n", dev, PERSIST_LABEL);
                    n++;
                }
            }
            pclose(p);
        }
        closedir(d);
    }
    if (!n) printf("  (none; run: persist setup <partition> --confirm)\n");
    printf("Enable:    add xtos.persist=%s to the kernel command line\n", PERSIST_LABEL);
    return 0;
}

static int cmd_setup(const char *dev, int confirm, int marker_only) {
    if (!valid_device(dev)) {
        fprintf(stderr, "persist: %s is not a block device under /dev/\n", dev);
        return 1;
    }
    if (!confirm) {
        fprintf(stderr, "persist: setup DESTROYS ALL DATA on %s\n", dev);
        fprintf(stderr, "persist: rerun with --confirm to proceed\n");
        return 2;
    }
    if (mountpoint_is("/run/persist")) {
        fprintf(stderr, "persist: refusing: a persistence volume is mounted\n");
        return 1;
    }
    /* refuse the Live medium itself: it is mounted ro; also refuse if the device
     * currently backs the running root (we cannot see that easily — mkfs will
     * fail on the mounted ISO anyway because it is iso9660). */
    if (!marker_only) {
        printf("PERSIST_FORMAT device=%s label=%s\n", dev, PERSIST_LABEL);
        int rc = run_cmd("/sbin/mkfs.ext4", dev);
        /* mkfs.ext4 needs -L; use a shell for that */
        if (rc == 127) {
            fprintf(stderr, "persist: mkfs.ext4 not available in this image\n");
            return 1;
        }
        if (rc != 0) {
            fprintf(stderr, "persist: mkfs.ext4 failed (%d)\n", rc);
            return 1;
        }
        /* label via tune2fs */
        char cmd[600];
        snprintf(cmd, sizeof(cmd), "/sbin/tune2fs -L %s %s >/dev/null 2>&1", PERSIST_LABEL, dev);
        if (system(cmd) != 0) {
            fprintf(stderr, "persist: warning: could not set label; persist marker <dev> may still work\n");
        }
    }
    /* write marker: mount temporarily */
    const char *probe = "/tmp/xtos-persist-probe";
    mkdir(probe, 0755);
    char mcmd[700];
    snprintf(mcmd, sizeof(mcmd), "mount -t ext4 -o rw %s %s", dev, probe);
    if (system(mcmd) != 0) {
        fprintf(stderr, "persist: cannot mount %s to write marker\n", dev);
        return 1;
    }
    char markerpath[600];
    snprintf(markerpath, sizeof(markerpath), "%s/XTOS_PERSIST_ID", probe);
    FILE *f = fopen(markerpath, "w");
    if (!f) {
        fprintf(stderr, "persist: cannot write marker\n");
        system("umount " "/tmp/xtos-persist-probe");
        return 1;
    }
    fprintf(f, "%s\n", PERSIST_MARKER);
    fclose(f);
    /* seed skeleton dirs */
    {
        char subdir[600];
        snprintf(subdir, sizeof(subdir), "%s/home", probe);
        mkdir(subdir, 0755);
        snprintf(subdir, sizeof(subdir), "%s/root", probe);
        mkdir(subdir, 0700);
    }
    char ucmd[600];
    snprintf(ucmd, sizeof(ucmd), "umount %s", probe);
    system(ucmd);
    printf("PERSIST_SETUP device=%s label=%s marker=%s\n", dev, PERSIST_LABEL, PERSIST_MARKER);
    printf("PERSIST_NEXT add xtos.persist=%s to the kernel command line\n", PERSIST_LABEL);
    return 0;
}

static void usage(void) {
    fprintf(stderr,
        "usage: persist <command>\n"
        "  persist status                 current persistence state + candidates\n"
        "  persist setup <dev> --confirm  format <dev> as the persistence volume\n"
        "  persist marker <dev>           add marker to an existing ext4 volume\n"
        "\n"
        "Persistence covers /home and /root only. The system image stays immutable.\n"
        "Boot with xtos.persist=XTOS-PERSIST to activate.\n");
}

int main(int argc, char **argv) {
    if (argc < 2) { usage(); return 2; }
    if (strcmp(argv[1], "status") == 0) return cmd_status();
    if (strcmp(argv[1], "setup") == 0 || strcmp(argv[1], "marker") == 0) {
        if (argc < 3) { usage(); return 2; }
        int confirm = (argc > 3 && strcmp(argv[argc-1], "--confirm") == 0);
        return cmd_setup(argv[2], confirm, strcmp(argv[1], "marker") == 0);
    }
    fprintf(stderr, "persist: unknown command %s\n", argv[1]);
    usage();
    return 2;
}
