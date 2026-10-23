/* clock: XTOS native clock status and bounded one-shot NTP sync (C, static).
 * Reports honest wall-clock state; explicit 'sync' subcommand does one NTP set.
 * Build: gcc -O2 -Wall -Wextra -Werror -static -o xtos-clock xtos-clock.c
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <time.h>
#include <fcntl.h>
#include <signal.h>
#include <ctype.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include "xtos_ui.h"

#define CHRONYD_PATH "/usr/sbin/chronyd"
#define STATE_PATH   "/run/clock.json"
/* Resident NTP (xtos-start launches chronyd with /etc/chrony/chrony.conf).
 * The pidfile path and chronyc path are overridable so tests can point the
 * tool at fixture files and stubs, like the persist tool does. */
#define CHRONY_PIDFILE_DEFAULT "/run/chrony/chronyd.pid"
#define CHRONYC_PATH_DEFAULT   "/usr/bin/chronyc"
#define MAX_SERVER_LEN 254
#define MAX_TIMEOUT 30

static int validate_server(const char *server) {
    size_t len = strlen(server);
    if (!server || len == 0 || len > MAX_SERVER_LEN || strchr(server, '%'))
        return -1;
    /* Accept IPv4, IPv6, or hostname (alphanumeric, hyphens, dots) */
    for (const char *p = server; *p; p++) {
        if (!isalnum((unsigned char)*p) && *p != '.' && *p != '-' && *p != ':' && *p != '[' && *p != ']')
            return -1;
    }
    return 0;
}

static void write_state(const char *server, const char *result, const char *error, double elapsed) {
    FILE *f = fopen(STATE_PATH, "w");
    if (!f) return;
    fprintf(f, "{\"server\": \"%s\", \"result\": \"%s\"", server, result);
    if (error) fprintf(f, ", \"error\": \"%s\"", error);
    fprintf(f, ", \"elapsed_seconds\": %.3f}\n", elapsed);
    fclose(f);
}

static void pidfile_path(char *out, size_t n) {
    const char *env = getenv("XTOS_CLOCK_PIDFILE");
    if (env && env[0]) snprintf(out, n, "%s", env);
    else snprintf(out, n, "%s", CHRONY_PIDFILE_DEFAULT);
}

static void chronyc_path(char *out, size_t n) {
    const char *env = getenv("XTOS_CHRONYC_PATH");
    if (env && env[0]) snprintf(out, n, "%s", env);
    else snprintf(out, n, "%s", CHRONYC_PATH_DEFAULT);
}

/* pid of the resident chronyd, or -1 when it is not running. The pid must
 * point at a live /proc entry; a stale pidfile is treated as "not running". */
static long resident_pid(void) {
    char path[4096];
    pidfile_path(path, sizeof(path));
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    long pid = -1;
    if (fscanf(f, "%ld", &pid) != 1) pid = -1;
    fclose(f);
    if (pid <= 0) return -1;
    char proc[64];
    snprintf(proc, sizeof(proc), "/proc/%ld", pid);
    struct stat st;
    if (stat(proc, &st) < 0) return -1;
    return pid;
}

/* Run a fixed-argv helper capturing its stdout (no shell anywhere). */
static int run_capture(const char *path, char *const argv[], char *out, size_t out_n) {
    int pfd[2];
    if (pipe(pfd) < 0) return -1;
    pid_t pid = fork();
    if (pid == 0) {
        close(pfd[0]);
        dup2(pfd[1], STDOUT_FILENO);
        close(pfd[1]);
        execv(path, argv);
        _exit(127);
    }
    if (pid < 0) { close(pfd[0]); close(pfd[1]); return -1; }
    close(pfd[1]);
    size_t n = 0;
    ssize_t r;
    while (n + 1 < out_n && (r = read(pfd[0], out + n, out_n - 1 - n)) > 0) n += (size_t)r;
    close(pfd[0]);
    int status = 0;
    waitpid(pid, &status, 0);
    out[n] = '\0';
    return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

/* True only when the resident daemon is actively tracking a source.
 * `chronyc tracking` prints a "System time" table row when locked; anything
 * else (501 Not synchronised, 506 Cannot talk, missing binary, ...) means
 * no continuous synchronization — the tool never claims otherwise. */
static int resident_tracking(int *tracking) {
    *tracking = 0;
    char exe[4096];
    chronyc_path(exe, sizeof(exe));
    if (access(exe, X_OK) != 0) return 0;
    char buf[4096];
    char *argv[] = {(char *)"chronyc", (char *)"tracking", NULL};
    if (run_capture(exe, argv, buf, sizeof(buf)) < 0) return 0;
    if (strstr(buf, "System time") && !strstr(buf, "Not synchronised")) *tracking = 1;
    return 0;
}

static int do_status(int json) {
    time_t now = time(NULL);
    struct tm tm_utc;
    gmtime_r(&now, &tm_utc);
    char utc[64];
    strftime(utc, sizeof(utc), "%Y-%m-%dT%H:%M:%S+00:00", &tm_utc);

    /* Read last sync state */
    char last_server[MAX_SERVER_LEN + 1] = "";
    char last_result[32] = "";
    FILE *f = fopen(STATE_PATH, "r");
    if (f) {
        char line[512];
        while (fgets(line, sizeof(line), f)) {
            char *p = strstr(line, "\"server\"");
            if (p) {
                p = strchr(p + 9, '"');
                if (p) { p++; char *e = strchr(p, '"');
                    if (e && (size_t)(e - p) <= MAX_SERVER_LEN) { memcpy(last_server, p, e - p); last_server[e - p] = '\0'; } }
            }
            p = strstr(line, "\"result\"");
            if (p) {
                p = strchr(p + 9, '"');
                if (p) { p++; char *e = strchr(p, '"');
                    if (e && (size_t)(e - p) < sizeof(last_result)) { memcpy(last_result, p, e - p); last_result[e - p] = '\0'; } }
            }
        }
        fclose(f);
    }

    int has_success = strcmp(last_result, "success") == 0;
    /* Honest synchronization claim: only the resident chronyd daemon tracking
     * a live source counts. A successful one-shot set is reported as history,
     * never as "synchronized" — a single step is not continuous sync. */
    long rpid = resident_pid();
    int tracking = 0;
    if (rpid > 0) resident_tracking(&tracking);
    int synced = (rpid > 0 && tracking);
    const char *source = rpid > 0 ? "chronyd-resident" : (has_success ? "chronyd-one-shot" : "unsynchronized");
    if (json) {
        printf("{\"utc\": \"%s\", \"synchronized\": %s, \"source\": \"%s\"",
               utc, synced ? "true" : "false", source);
        if (rpid > 0) printf(", \"resident_pid\": %ld", rpid);
        if (last_server[0]) printf(", \"last_server\": \"%s\"", last_server);
        if (last_result[0]) printf(", \"last_result\": \"%s\"", last_result);
        if (!synced)
            printf(", \"warning\": \"Wall time is unverified; use monotonic time for elapsed durations.\"");
        printf("}\n");
    } else {
        xui_init();
        printf("%s%s%s", XUI_LABEL_S, "XTOS_CLOCK", XUI_RESET_S);
        printf(" utc=%s synchronized=", utc);
        if (synced) XUI_OK("true");
        else XUI_WARN("false");
        printf(" source=%s", source);
        if (rpid > 0) printf(" resident_pid=%ld", rpid);
        if (last_server[0]) printf(" last_server=%s", last_server);
        if (last_result[0]) printf(" last_result=%s", last_result);
        printf("\n");
        if (!synced)
            XUI_WARN("  warning: Wall time is unverified; use monotonic time for elapsed durations.\n");
    }
    return 0;
}

static int do_sync(const char *server, int timeout) {
    if (validate_server(server) < 0) {
        fprintf(stderr, "XTOS_CLOCK_ERROR invalid time server\n");
        return 1;
    }
    if (timeout < 1 || timeout > MAX_TIMEOUT) {
        fprintf(stderr, "XTOS_CLOCK_ERROR timeout must be 1..%d seconds\n", MAX_TIMEOUT);
        return 1;
    }
    long rpid = resident_pid();
    if (rpid > 0) {
        /* The resident daemon owns synchronization; a second chronyd instance
         * would fight it. Report honestly instead of failing mysteriously. */
        printf("XTOS_CLOCK_SYNC {\"result\": \"resident-chronyd-active\", \"resident_pid\": %ld, "
               "\"note\": \"continuous synchronization in progress; see clock status\"}\n", rpid);
        return 0;
    }
    if (access(CHRONYD_PATH, X_OK) != 0) {
        fprintf(stderr, "XTOS_CLOCK_ERROR chronyd not available at %s\n", CHRONYD_PATH);
        return 1;
    }

    /* Write temporary chronyd config */
    char confpath[] = "/run/chrony-XXXXXX.conf";
    int fd = mkstemps(confpath, 5);
    if (fd < 0) {
        fprintf(stderr, "XTOS_CLOCK_ERROR cannot create temp config\n");
        return 1;
    }
    dprintf(fd, "server %s iburst\nuser root\n", server);
    close(fd);

    struct timespec start, end;
    clock_gettime(CLOCK_MONOTONIC, &start);

    pid_t pid = fork();
    if (pid == 0) {
        /* Child: run chronyd -q (exits after one sync) */
        char timeout_str[8];
        snprintf(timeout_str, sizeof(timeout_str), "%d", timeout);
        execl(CHRONYD_PATH, "chronyd", "-q", "-t", timeout_str, "-f", confpath, (char *)NULL);
        _exit(127);
    }
    if (pid < 0) {
        unlink(confpath);
        fprintf(stderr, "XTOS_CLOCK_ERROR fork failed\n");
        return 1;
    }

    /* Parent: wait with timeout */
    int status = 0;
    time_t deadline = time(NULL) + timeout + 2;
    while (waitpid(pid, &status, WNOHANG) == 0) {
        if (time(NULL) > deadline) {
            kill(pid, SIGKILL);
            waitpid(pid, &status, 0);
            clock_gettime(CLOCK_MONOTONIC, &end);
            double elapsed = (end.tv_sec - start.tv_sec) + (end.tv_nsec - start.tv_nsec) / 1e9;
            write_state(server, "failed", "chronyd exceeded timeout", elapsed);
            printf("XTOS_CLOCK_SYNC {\"server\": \"%s\", \"result\": \"failed\", \"error\": \"chronyd exceeded timeout\", \"elapsed_seconds\": %.3f}\n",
                   server, elapsed);
            unlink(confpath);
            return 1;
        }
        usleep(100000); /* 100ms */
    }

    clock_gettime(CLOCK_MONOTONIC, &end);
    double elapsed = (end.tv_sec - start.tv_sec) + (end.tv_nsec - start.tv_nsec) / 1e9;
    unlink(confpath);

    int rc = WIFEXITED(status) ? WEXITSTATUS(status) : 1;
    if (rc == 0) {
        write_state(server, "success", NULL, elapsed);
        printf("XTOS_CLOCK_SYNC {\"server\": \"%s\", \"result\": \"success\", \"elapsed_seconds\": %.3f}\n", server, elapsed);
        return 0;
    } else {
        char errbuf[32];
        snprintf(errbuf, sizeof(errbuf), "chronyd exit=%d", rc);
        write_state(server, "failed", errbuf, elapsed);
        printf("XTOS_CLOCK_SYNC {\"server\": \"%s\", \"result\": \"failed\", \"error\": \"%s\", \"elapsed_seconds\": %.3f}\n",
               server, errbuf, elapsed);
        return 1;
    }
}

static void usage(void) {
    fprintf(stderr,
        "usage: clock <command>\n"
        "  clock status [--json]     report wall time and sync state\n"
        "  clock sync <server>       one-shot NTP sync (root only); refused\n"
        "                             while the resident chronyd is running\n"
        "    --timeout <seconds>          1-30 seconds (default 15)\n");
}

int main(int argc, char **argv) {
    if (argc < 2) { usage(); return 2; }

    if (strcmp(argv[1], "status") == 0) {
        int json = (argc > 2 && strcmp(argv[2], "--json") == 0);
        return do_status(json);
    }

    if (strcmp(argv[1], "sync") == 0) {
        if (geteuid() != 0) {
            fprintf(stderr, "XTOS_CLOCK_ERROR clock sync requires root; run sudo clock sync SERVER\n");
            return 1;
        }
        const char *server = NULL;
        int timeout = 15;
        for (int i = 2; i < argc; i++) {
            if (strcmp(argv[i], "--timeout") == 0 && i + 1 < argc) {
                timeout = atoi(argv[++i]);
            } else if (!server && argv[i][0] != '-') {
                server = argv[i];
            } else {
                usage(); return 2;
            }
        }
        if (!server) { usage(); return 2; }
        return do_sync(server, timeout);
    }

    fprintf(stderr, "clock: unknown command %s\n", argv[1]);
    usage();
    return 2;
}
