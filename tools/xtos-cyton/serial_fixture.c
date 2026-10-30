/* Host-only PTY runner. No hardware access is made by the test suite. */
#define _POSIX_C_SOURCE 200809L
#include "cyton_serial.h"
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static volatile sig_atomic_t stopped;
static int mode;
/* Linker-only fault injection: production always uses the real write syscall. */
#include <unistd.h>
ssize_t __real_write(int, const void *, size_t);
ssize_t __wrap_write(int fd, const void *buf, size_t n)
{
    static unsigned stops, retried;
    if (n == 1) {
        unsigned char c = *(const unsigned char *)buf;
        if (c == 's') {
            stops++;
            if ((mode == 4 || mode == 6) && stops == 2) return 0;
            if (mode == 11 && stops == 2) { errno = EAGAIN; return -1; }
        }
        if (mode == 7 && c == 'b') return 0;
        if (mode == 5 && c == 'v' && !retried++) { errno = EINTR; return -1; }
        if (mode == 8 && c == 'b' && !retried++) { errno = EAGAIN; return -1; }
    }
    return __real_write(fd, buf, n);
}
static void on_signal(int sig) { (void)sig; stopped = 1; }
static int event(enum cyton_serial_event e, const struct cyton_board_config *c,
                 const unsigned char *p, size_t n, uint64_t g, uint64_t ns, void *v)
{
    (void)v;
    printf("EVENT %d %" PRIu64 " %" PRIu64 " %zu", e, g, ns, n);
    if (e == CYTON_CONNECTED) {
        printf(" MASK %u GAINS", c->enabled_mask);
        for (unsigned i = 0; i < 8; i++) printf(" %u", c->gain[i]);
        printf(" IDHEX ");
        for (size_t i = 0; c->identity[i]; i++) printf("%02x", (unsigned char)c->identity[i]);
    }
    if (e == CYTON_BYTES) {
        printf(" HEX ");
        for (size_t i = 0; i < n; i++) printf("%02x", p[i]);
        if (mode == 1 || mode == 6) { puts(""); fflush(stdout); errno = ENOSPC; return -1; }
        if (mode == 2) stopped = 1;
    }
    if (mode == 9 && e == CYTON_DISCONNECTED) { puts(""); fflush(stdout); errno = ENOSPC; return -1; }
    if (mode == 10 && e == CYTON_CONNECTED) { puts(""); fflush(stdout); errno = EACCES; return -1; }
    puts(""); fflush(stdout);
    return 0;
}
int main(int argc, char **argv)
{
    struct sigaction sa;
    struct cyton_serial_stats s;
    struct cyton_serial_options o;
    if (argc != 7) return 2;
    memset(&sa, 0, sizeof(sa)); sa.sa_handler = on_signal;
    sigaction(SIGINT, &sa, NULL); sigaction(SIGTERM, &sa, NULL);
    o.device = argv[1]; o.duration_ms = (unsigned)strtoul(argv[2], NULL, 10);
    o.command_timeout_ms = (unsigned)strtoul(argv[3], NULL, 10);
    o.reconnect_delay_ms = (unsigned)strtoul(argv[4], NULL, 10);
    o.reconnect_attempts = (unsigned)strtoul(argv[5], NULL, 10);
    mode = atoi(argv[6]);
    int rc = cyton_serial_run(&o, &stopped, event, NULL, &s);
    int error = errno;
    printf("RESULT %d %d %" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64 "\n",
           rc, error, s.connections, s.reconnects, s.bytes, s.disconnects, s.stop_errors);
    return rc == 0 ? 0 : 1;
}
