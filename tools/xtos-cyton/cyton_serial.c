#define _DEFAULT_SOURCE
#define _POSIX_C_SOURCE 200809L
#include "cyton_serial.h"
#include "../../kernel/linux/drivers/xtos/bci/xtos_cyton_config.h"
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdbool.h>
#include <string.h>
#include <sys/stat.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define NS_MS UINT64_C(1000000)
#define REPLY_CAP 8192u
#define CHUNK_CAP 4096u

static uint64_t clock_ns(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t) != 0) return 0;
    return (uint64_t)t.tv_sec * UINT64_C(1000000000) + (uint64_t)t.tv_nsec;
}
static uint64_t earlier(uint64_t a, uint64_t b) { return a < b ? a : b; }
static int fail(int e) { errno = e; return -1; }
/* All waits use the original absolute deadline, including EINTR/EAGAIN. */
static int ready(int fd, short events, uint64_t end, volatile sig_atomic_t *stop)
{
    for (;;) {
        struct pollfd p = {fd, events, 0};
        uint64_t now = clock_ns();
        if (*stop) return fail(ECANCELED);
        if (now >= end) return fail(ETIMEDOUT);
        uint64_t left = (end - now + NS_MS - 1) / NS_MS;
        int ms = left > 10 ? 10 : (int)left;
        int rc = poll(fd < 0 ? NULL : &p, fd < 0 ? 0 : 1, ms);
        if (rc < 0) { if (errno == EINTR) continue; return -1; }
        if (!rc) continue;
        if (p.revents & events) return 0;
        if (p.revents & (POLLHUP | POLLERR | POLLNVAL)) return fail(EIO);
    }
}
static int command(int fd, unsigned char c, uint64_t end, volatile sig_atomic_t *stop)
{
    for (;;) {
        if (ready(fd, POLLOUT, end, stop) != 0) return -1;
        ssize_t n = write(fd, &c, 1);
        if (n == 1) return 0;
        if (n < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) continue;
        return n < 0 ? -1 : fail(EIO);
    }
}
static int reply(int fd, char *out, size_t cap, uint64_t end, volatile sig_atomic_t *stop)
{
    size_t n = 0;
    for (;;) {
        unsigned char c;
        if (ready(fd, POLLIN, end, stop) != 0) return -1;
        ssize_t r = read(fd, &c, 1);
        if (r < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) continue;
        if (r <= 0) return r < 0 ? -1 : fail(EIO);
        if (c != '\r' && c != '\n' && (c < 32 || c > 126)) return fail(EPROTO);
        if (n + 1 >= cap) return fail(EMSGSIZE);
        out[n++] = (char)c;
        if (n >= 3 && !memcmp(out + n - 3, "$$$", 3)) {
            out[n - 3] = '\0';
            return 0;
        }
    }
}
static int identity_valid(const char *s)
{
    /* Firmware versions differ in the "On Board" prefix, not the chip ID.
     * Source: official CytonSDK and OpenBCI_Cyton_Library::boardReset(). */
    const char *board = "OpenBCI V3 8-16 channel";
    size_t n = strlen(board);
    if (strncmp(s, board, n) || (s[n] != '\r' && s[n] != '\n')) return fail(EPROTO);
    char lines[256];
    memcpy(lines, s, strlen(s) + 1);
    unsigned ids = 0;
    char *save = NULL;
    for (char *line = strtok_r(lines, "\r\n", &save); line; line = strtok_r(NULL, "\r\n", &save)) {
        if (strstr(line, "On Daisy ADS1299")) return fail(EPROTONOSUPPORT);
        if (!strstr(line, "ADS1299")) continue;
        if (strcmp(line, "ADS1299 Device ID: 0x3E") && strcmp(line, "ADS1299 Device ID: 0x3e") &&
            strcmp(line, "On Board ADS1299 Device ID: 0x3E") && strcmp(line, "On Board ADS1299 Device ID: 0x3e"))
            return fail(EPROTO);
        ids++;
    }
    return ids == 1 ? 0 : fail(EPROTO);
}
static int configuration(const char *text, struct cyton_board_config *config)
{
    struct xtos_cyton_config parsed;
    int rc = xtos_cyton_config_parse(text, strlen(text), &parsed);
    unsigned int ch;

    if (rc) return fail(-rc);
    for (ch = 0; ch < 8; ++ch) config->gain[ch] = parsed.gain[ch];
    config->enabled_mask = parsed.enabled_mask;
    return 0;
}
static int open_tty(const char *path)
{
    struct stat st;
    struct termios t;
    int fd = open(path, O_RDWR | O_NOCTTY | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) return -1;
    if (fstat(fd, &st)) { int e = errno; close(fd); return fail(e); }
    if (!S_ISCHR(st.st_mode)) { close(fd); return fail(ENOTTY); }
    if (tcgetattr(fd, &t)) { int e = errno; close(fd); return fail(e); }
    cfmakeraw(&t);
    t.c_cflag &= ~(PARENB | CSTOPB | CSIZE | CRTSCTS);
    t.c_cflag |= CS8 | CLOCAL | CREAD;
    t.c_cc[VMIN] = 0; t.c_cc[VTIME] = 0;
    if (cfsetispeed(&t, B115200) || cfsetospeed(&t, B115200) || tcsetattr(fd, TCSANOW, &t) || tcflush(fd, TCIOFLUSH)) {
        int e = errno; close(fd); return fail(e);
    }
    return fd;
}
static int probe(int fd, const struct cyton_serial_options *o, uint64_t end,
                 volatile sig_atomic_t *stop, struct cyton_board_config *config)
{
    char text[REPLY_CAP];
    uint64_t d = earlier(end, clock_ns() + (uint64_t)o->command_timeout_ms * NS_MS);
    memset(config, 0, sizeof(*config));
    if (command(fd, 's', d, stop) || command(fd, 'v', d, stop) || reply(fd, text, sizeof(text), d, stop)) return -1;
    size_t n = strlen(text);
    if (n >= sizeof(config->identity)) return fail(EMSGSIZE);
    if (identity_valid(text)) return -1;
    memcpy(config->identity, text, n + 1);
    d = earlier(end, clock_ns() + (uint64_t)o->command_timeout_ms * NS_MS);
    if (command(fd, '?', d, stop) || reply(fd, text, sizeof(text), d, stop)) return -1;
    return configuration(text, config);
}
static int notify(cyton_serial_callback cb, enum cyton_serial_event event,
                  const struct cyton_board_config *config, const unsigned char *p,
                  size_t n, uint64_t generation, void *context)
{
    errno = 0;
    if (cb(event, config, p, n, generation, clock_ns(), context)) return fail(errno ? errno : EIO);
    return 0;
}
int cyton_serial_run(const struct cyton_serial_options *o, volatile sig_atomic_t *stop,
                     cyton_serial_callback cb, void *context, struct cyton_serial_stats *stats)
{
    struct cyton_serial_stats local = {0};
    struct cyton_board_config config;
    char first_identity[256] = {0};
    unsigned retries = 0;
    int error = 0, cleanup_error = 0;
    if (!stats) stats = &local;
    memset(stats, 0, sizeof(*stats));
    if (!o || !o->device || !*o->device || !stop || !cb || !o->duration_ms || !o->command_timeout_ms)
        return fail(EINVAL);
    uint64_t end = clock_ns() + (uint64_t)o->duration_ms * NS_MS;
    while (!*stop && clock_ns() < end) {
        int fd = open_tty(o->device);
        bool disconnected = false, connected = false;
        if (fd < 0) {
            error = errno;
            if (!stats->connections) break;
            disconnected = true;
        } else {
            if (probe(fd, o, end, stop, &config)) error = errno;
            else if (*first_identity && strcmp(first_identity, config.identity)) error = ESTALE;
            else {
                if (!*first_identity) memcpy(first_identity, config.identity, sizeof(first_identity));
                stats->connections++;
                if (stats->connections > 1) stats->reconnects++;
                connected = true;
                if (notify(cb, CYTON_CONNECTED, &config, NULL, 0, stats->connections, context)) error = errno;
                else if (command(fd, 'b', earlier(end, clock_ns() + (uint64_t)o->command_timeout_ms * NS_MS), stop)) error = errno;
                else {
                    while (!*stop && clock_ns() < end) {
                        unsigned char bytes[CHUNK_CAP];
                        if (ready(fd, POLLIN, end, stop)) {
                            if (errno != ECANCELED && errno != ETIMEDOUT) { error = errno; disconnected = true; }
                            break;
                        }
                        ssize_t n = read(fd, bytes, sizeof(bytes));
                        if (n < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) continue;
                        if (n <= 0) { error = n < 0 ? errno : EIO; disconnected = true; break; }
                        stats->bytes += (uint64_t)n;
                        if (notify(cb, CYTON_BYTES, &config, bytes, (size_t)n, stats->connections, context)) { error = errno; break; }
                    }
                }
            }
            /* Even on stop/deadline/callback failure, attempt s without waiting.
             * A command is one byte, so 0 is a short write and is an error. */
            unsigned char s = 's';
            ssize_t n = write(fd, &s, 1);
            if (n != 1) {
                stats->stop_errors++;
                if (!cleanup_error) cleanup_error = n < 0 ? errno : EIO;
            }
            close(fd);
            if (disconnected && connected) {
                stats->disconnects++;
                if (notify(cb, CYTON_DISCONNECTED, &config, NULL, 0, stats->connections, context)) {
                    if (!error) error = errno;
                    disconnected = false;
                }
            }
        }
        if (!disconnected) break;
        if (*stop || clock_ns() >= end || retries >= o->reconnect_attempts) break;
        retries++;
        uint64_t delay_end = earlier(end, clock_ns() + (uint64_t)o->reconnect_delay_ms * NS_MS);
        if (ready(-1, 0, delay_end, stop) && errno == ECANCELED) break;
        if (*stop || clock_ns() >= end) break;
        error = 0;
    }
    if (error == ECANCELED && *stop) error = 0;
    if (!error) error = cleanup_error;
    return error ? fail(error) : 0;
}
