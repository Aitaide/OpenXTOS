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
    if (json) {
        printf("{\"utc\": \"%s\", \"synchronized\": false, \"source\": \"%s\",",
               utc, has_success ? "chronyd-one-shot" : "unsynchronized");
        if (last_server[0]) printf(" \"last_server\": \"%s\",", last_server);
        if (last_result[0]) printf(" \"last_result\": \"%s\",", last_result);
        printf(" \"warning\": \"Wall time is unverified; use monotonic time for elapsed durations.\"}\n");
    } else {
        xui_init();
        printf("%s%s%s", XUI_LABEL_S, "XTOS_CLOCK", XUI_RESET_S);
        printf(" utc=%s synchronized=", utc);
        if (has_success) XUI_OK("true");
        else XUI_WARN("false");
        printf(" source=%s", has_success ? "chronyd-one-shot" : "unsynchronized");
        if (last_server[0]) printf(" last_server=%s", last_server);
        if (last_result[0]) printf(" last_result=%s", last_result);
        printf("\n");
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
        "  clock status [--json]     report wall time and last sync attempt\n"
        "  clock sync <server>       one-shot NTP sync (root only)\n"
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
