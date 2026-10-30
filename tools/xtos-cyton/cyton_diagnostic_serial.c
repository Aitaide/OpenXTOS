/* SPDX-License-Identifier: GPL-2.0 */
#define _POSIX_C_SOURCE 200809L
#define _DEFAULT_SOURCE
#include "cyton_diagnostic_serial.h"
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
struct transport {
    int fd, cleanup, partial_pending, completing;
    int64_t deadline;
    const struct cyton_diagnostic_options *o;
    struct cyton_diagnostic_result *r;
};
static int64_t now_ms(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) return -1;
    return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
static int fail(int e) { errno = e; return -1; }
static int check(struct transport *t, int64_t d)
{
    if (!t->cleanup && !t->completing && t->o->stop && *t->o->stop) return fail(ECANCELED);
    if (now_ms() >= d) return fail(ETIMEDOUT);
    return 0;
}
static int wait_fd(struct transport *t, short events, int64_t d)
{
    struct pollfd p = { t->fd, events, 0 };
    for (;;) {
        int rc, ms;
        int64_t left;
        if (check(t, d)) return -1;
        left = d - now_ms(); ms = left > 20 ? 20 : (int)left;
        if (ms < 1) ms = 1;
        rc = poll(&p, 1, ms);
        if (rc < 0) { if (errno == EINTR) continue; return -1; }
        if (rc && (p.revents & events)) return 0;
        if (rc && (p.revents & (POLLERR|POLLHUP|POLLNVAL))) return fail(EIO);
    }
}
static int64_t command_deadline(struct transport *t)
{
    int64_t d = now_ms() + t->o->command_timeout_ms;
    return d < t->deadline ? d : t->deadline;
}
static int recover_partial(struct transport *t);
static int send_command(struct transport *t, const char *p, size_t n, int64_t d)
{
    size_t sent = 0;
    if (t->partial_pending && recover_partial(t)) return -1;
    if (n > 1 && d > now_ms() + 800) d = now_ms() + 800;
    while (sent < n) {
        ssize_t rc;
        /* Once a multibyte command has begun, finish its contiguous payload
         * within the original deadline; observe stop again before its reply. */
        t->completing = sent > 0 && n > 1;
        if (wait_fd(t, POLLOUT, d)) goto error;
        rc = write(t->fd, p + sent, n - sent);
        if (rc > 0) { sent += (size_t)rc; continue; }
        if (rc < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) continue;
        if (!rc) errno = EIO;
        goto error;
    }
    t->completing = 0;
    return 0;
error:
    t->completing = 0;
    if (sent && sent < n && n > 1) {
        t->r->partial_command = 1; t->partial_pending = 1;
    }
    return -1;
}
/* Quiet boundary: 40 ms without bytes, with a finite 500 ms outer budget.
 * Draining never implies that current injection is disabled. */
static int drain(struct transport *t)
{
    int64_t end = now_ms() + 500, quiet = now_ms() + 40;
    unsigned char buffer[512];
    if (end > t->deadline) end = t->deadline;
    for (;;) {
        struct pollfd p = { t->fd, POLLIN, 0 };
        ssize_t n;
        int rc;
        int64_t now = now_ms();
        if (check(t, end)) return -1;
        if (now >= quiet) return 0;
        rc = poll(&p, 1, 10);
        if (rc < 0) { if (errno == EINTR) continue; return -1; }
        if (rc && (p.revents & POLLIN)) {
            n = read(t->fd, buffer, sizeof(buffer));
            if (n > 0) quiet = now_ms() + 40;
            else if (!n) return fail(EIO);
            else if (errno != EINTR && errno != EAGAIN) return -1;
        } else if (rc && (p.revents & (POLLHUP|POLLERR|POLLNVAL))) return fail(EIO);
    }
}
static int reply(struct transport *t, char *p, size_t capacity, size_t *length, int64_t d)
{
    size_t used = 0;
    for (;;) {
        ssize_t n;
        if (used + 1 >= capacity) return fail(EMSGSIZE);
        if (wait_fd(t, POLLIN, d)) return -1;
        n = read(t->fd, p + used, 1);
        if (n < 0) { if (errno == EINTR || errno == EAGAIN) continue; return -1; }
        if (!n) return fail(EIO);
        ++used;
        if (used >= 3 && !memcmp(p + used - 3, "$$$", 3)) {
            size_t i;
            *length = used - 3;
            for (i = 0; i < *length; ++i) {
                unsigned char c = (unsigned char)p[i];
                if (c != '\r' && c != '\n' && c != '\t' && (c < 32 || c > 126))
                    return fail(EPROTO);
            }
            p[*length] = 0; return 0;
        }
    }
}
static int exchange(struct transport *t, const char *cmd, char *p, size_t cap, size_t *length)
{
    int64_t d = command_deadline(t);
    if (send_command(t, cmd, strlen(cmd), d)) return -1;
    return reply(t, p, cap, length, d);
}
/* The shared config parser intentionally ignores unrelated rows. Diagnostics
 * require the entire bounded reply to be known Board ADS / optional LIS text.
 * LIS3DH_readAllRegs at the pinned firmware emits: 0xHH HH, addresses
 * 07..0f, 1f..33, 38..3d. Blank lines separate those groups. */
static int query_text(const char *text, size_t length)
{
    static const char * const names[24] = {
        "ADS_ID", "CONFIG1", "CONFIG2", "CONFIG3", "LOFF",
        "CH1SET", "CH2SET", "CH3SET", "CH4SET", "CH5SET", "CH6SET", "CH7SET", "CH8SET",
        "BIAS_SENSP", "BIAS_SENSN", "LOFF_SENSP", "LOFF_SENSN", "LOFF_FLIP",
        "LOFF_STATP", "LOFF_STATN", "GPIO", "MISC1", "MISC2", "CONFIG4"
    };
    size_t offset = 0;
    unsigned int board = 0, ads = 0, lis = 0, previous = 0;
    while (offset < length) {
        size_t start = offset, n, name_length;
        const char *line;
        unsigned int value;
        while (offset < length && text[offset] != '\r' && text[offset] != '\n') ++offset;
        n = offset - start; line = text + start;
        if (offset < length) ++offset;
        if (!n) continue;
        if (xtos_cyton_config_equal(line, n, "Daisy ADS Registers")) return fail(EPROTONOSUPPORT);
        if (!board) {
            if (!xtos_cyton_config_equal(line, n, "Board ADS Registers")) return fail(EPROTO);
            board = 1; continue;
        }
        if (ads < 24) {
            name_length = strlen(names[ads]);
            if (n <= name_length || memcmp(line, names[ads], name_length) ||
                line[name_length] != ',' ||
                xtos_cyton_config_row(line + name_length + 1, line + n, ads, &value))
                return fail(EPROTO);
            ++ads; continue;
        }
        if (!lis) {
            if (!xtos_cyton_config_equal(line, n, "LIS3DH Registers")) return fail(EPROTO);
            lis = 1; continue;
        }
        if (n != 7 || memcmp(line, "0x", 2) || line[4] != ' ' ||
            xtos_cyton_config_hex((unsigned char)line[2]) < 0 ||
            xtos_cyton_config_hex((unsigned char)line[3]) < 0 ||
            xtos_cyton_config_hex((unsigned char)line[5]) < 0 ||
            xtos_cyton_config_hex((unsigned char)line[6]) < 0) return fail(EPROTO);
        value = (unsigned int)(16 * xtos_cyton_config_hex((unsigned char)line[2]) +
                               xtos_cyton_config_hex((unsigned char)line[3]));
        if (!((value >= 0x07 && value <= 0x0f) || (value >= 0x1f && value <= 0x33) ||
              (value >= 0x38 && value <= 0x3d)) || value <= previous) return fail(EPROTO);
        previous = value;
    }
    return board && ads == 24 ? 0 : fail(EPROTO);
}
static int query(struct transport *t, struct xtos_cyton_config *c)
{
    char text[8196];
    size_t length;
    int rc;
    if (exchange(t, "?", text, sizeof(text), &length) || query_text(text, length)) return -1;
    rc = xtos_cyton_config_parse(text, length, c);
    if (rc) return fail(-rc);
    if (c->register_mask != 0xffffffu || c->registers[0] != 0x3e ||
        (c->registers[1] & 7) != 6) return fail(EPROTO);
    return 0;
}
static int version(struct transport *t)
{
    char p[64], *end;
    size_t length;
    unsigned long major;
    const char *s;
    if (exchange(t, "V", p, sizeof(p), &length)) return -1;
    if (p[0] != 'v') return fail(EPROTONOSUPPORT);
    s = p + 1;
    if (*s < '0' || *s > '9') return fail(EPROTO);
    major = strtoul(s, &end, 10);
    if (major < 2 || major > 999 || *end++ != '.') return fail(EPROTONOSUPPORT);
    {
        unsigned int component;
        for (component = 0; component < 2; ++component) {
            if (*end < '0' || *end > '9') return fail(EPROTO);
            while (*end >= '0' && *end <= '9') ++end;
            if (component == 0 && *end++ != '.') return fail(EPROTO);
        }
    }
    if (end != p + length) return fail(EPROTO);
    memcpy(t->r->firmware, p, length + 1);
    return 0;
}
static int preflight(struct transport *t)
{
    const unsigned char *r = t->r->before.registers;
    if (r[13] != r[14] || (r[21] != 0 && r[21] != 0x20)) return fail(EOPNOTSUPP);
    if (t->o->mode == CYTON_DIAGNOSTIC_IMPEDANCE && ((r[4]&15) != 2 || r[17])) return fail(EOPNOTSUPP);
    return 0;
}
static int same_writable(const struct xtos_cyton_config *a,
                          const struct xtos_cyton_config *b)
{
    unsigned int i;
    for (i = 0; i < 24; ++i) {
        unsigned int mask = 255;
        /* ADS1299 Rev.C: LOFF_STATP/N are read-only comparator status.
         * GPIO[3:0] direction (1=input), [7:4] data: ignore only input data.
         * All other bytes including CONFIG2 and unaffected writable bits match. */
        if (i == 18 || i == 19) continue;
        if (i == 20) mask = 15u | ((~a->registers[20] & 15u) << 4);
        if (((a->registers[i] ^ b->registers[i]) & mask) != 0) return 0;
    }
    return 1;
}
static int verify(struct transport *t, const struct xtos_cyton_config *wanted,
                  struct xtos_cyton_config *seen)
{
    if (query(t, seen)) return -1;
    return same_writable(wanted, seen) ? 0 : fail(EPROTO);
}
static int ack(struct transport *t, const char *cmd, const char *kind, unsigned int ch)
{
    char got[128], expected[80];
    size_t length;
    if (exchange(t, cmd, got, sizeof(got), &length)) return -1;
    (void)snprintf(expected, sizeof(expected), "Success: %s set for %u", kind, ch + 1);
    return length != strlen(expected) || memcmp(got, expected, length) ? fail(EPROTO) : 0;
}
static int set_z(struct transport *t, unsigned int ch, unsigned int p, unsigned int n)
{
    char cmd[6];
    (void)snprintf(cmd, sizeof(cmd), "z%u%u%uZ", ch + 1, p, n);
    return ack(t, cmd, "Lead off", ch);
}
static int set_x(struct transport *t, unsigned int ch, unsigned char value,
                  unsigned int bias, unsigned int srb1)
{
    char cmd[10];
    (void)snprintf(cmd, sizeof(cmd), "x%u%u%u%u%u%u%uX", ch + 1,
                   (value >> 7) & 1u, (value >> 4) & 7u, value & 7u,
                   bias & 1u, (value >> 3) & 1u, srb1 & 1u);
    return ack(t, cmd, "Channel", ch);
}
static int disable_z(struct transport *t)
{
    unsigned int i;
    int first = 0;
    for (i = 0; i < 8; ++i) {
        if (set_z(t, i, 0, 0) && !first) first = errno;
        if (first && !t->cleanup) return fail(first);
    }
    return first ? fail(first) : 0;
}
static int prepare_test(struct transport *t)
{
    struct xtos_cyton_config expected = t->r->before, seen;
    unsigned int ch = t->o->channel;
    if (disable_z(t)) return -1;
    expected.registers[15] = expected.registers[16] = 0;
    if (verify(t, &expected, &seen)) return -1;
    expected.registers[5 + ch] = (expected.registers[5 + ch] & 0x70u) | 1u;
    if (t->o->mode == CYTON_DIAGNOSTIC_IMPEDANCE) {
        expected.registers[5 + ch] = 0;
        expected.registers[13] |= (unsigned char)(1u << ch);
        expected.registers[14] |= (unsigned char)(1u << ch);
    }
    expected.registers[21] = 0;
    if (set_x(t, ch, expected.registers[5 + ch], (expected.registers[13] >> ch) & 1u, 0)) return -1;
    if (verify(t, &expected, &t->r->test)) return -1;
    t->r->actual_gain = t->r->test.gain[ch];
    if (t->o->mode == CYTON_DIAGNOSTIC_IMPEDANCE) {
        if (set_z(t, ch, t->o->input == 'P', t->o->input == 'N')) return -1;
        expected.registers[15] = t->o->input == 'P' ? (unsigned char)(1u << ch) : 0;
        expected.registers[16] = t->o->input == 'N' ? (unsigned char)(1u << ch) : 0;
        if (verify(t, &expected, &t->r->test)) return -1;
    }
    return 0;
}
static int capture_frames(struct transport *t)
{
    unsigned char frame[33];
    unsigned int used = 0, previous = 0, have_previous = 0;
    struct cyton_diagnostic_result *r = t->r;
    if (send_command(t, "b", 1, command_deadline(t))) return -1;
    while (r->sample_count < t->o->sample_frames) {
        ssize_t n;
        unsigned int ch;
        int32_t selected = 0;
        int64_t nv;
        if (wait_fd(t, POLLIN, t->deadline)) return -1;
        n = read(t->fd, frame + used, sizeof(frame) - used);
        if (n < 0) { if (errno == EINTR || errno == EAGAIN) continue; return -1; }
        if (!n) return fail(EIO);
        used += (unsigned int)n;
        if (used < sizeof(frame)) continue;
        used = 0; ++r->frames_observed;
        if (frame[0] != 0xa0 || (frame[32] & 0xf0) != 0xc0) {
            ++r->bad_frames; return fail(EPROTO);
        }
        if (have_previous && (unsigned char)(frame[1] - previous) != 1) {
            ++r->counter_errors; return fail(EPROTO);
        }
        previous = frame[1]; have_previous = 1;
        for (ch = 0; ch < 8; ++ch) {
            unsigned int at = 2 + 3 * ch;
            int32_t raw = (int32_t)(((uint32_t)frame[at] << 16) |
                          ((uint32_t)frame[at + 1] << 8) | frame[at + 2]);
            if (raw & 0x800000) raw -= 0x1000000;
            if (raw == 8388607 || raw == -8388608) { ++r->rail_errors; return fail(ERANGE); }
            if (ch == t->o->channel) selected = raw;
        }
        r->last_receive_ns = now_ms() * 1000000;
        if (!r->first_receive_ns) r->first_receive_ns = r->last_receive_ns;
        if (r->settled_frames < t->o->settle_frames) { ++r->settled_frames; continue; }
        nv = (int64_t)selected * INT64_C(4500000000) / ((int64_t)r->actual_gain * 8388607);
        if (nv < INT32_MIN || nv > INT32_MAX) return fail(ERANGE);
        r->samples_nv[r->sample_count++] = (int32_t)nv;
    }
    return 0;
}
static int recover_partial(struct transport *t)
{
    int64_t end = now_ms() + 1200;
    unsigned char buf[512];
    if (!t->partial_pending) return 0;
    if (end > t->deadline) return fail(ETIMEDOUT);
    /* Official firmware loop() polls the 1 s parser timer. Never inject an
     * invented abort/terminator into its partial x/z argument buffer. */
    while (now_ms() < end) {
        struct pollfd p = { t->fd, POLLIN, 0 };
        int rc = poll(&p, 1, 20);
        if (rc < 0) { if (errno == EINTR) continue; return -1; }
        if (rc && (p.revents & POLLIN)) {
            ssize_t n = read(t->fd, buf, sizeof(buf));
            if (n < 0 && errno != EINTR && errno != EAGAIN) return -1;
            if (!n) return fail(EIO);
        } else if (rc && (p.revents & (POLLHUP|POLLERR|POLLNVAL))) return fail(EIO);
    }
    t->partial_pending = 0;
    return 0;
}
static int cleanup(struct transport *t)
{
    unsigned int i;
    int first = 0;
    struct xtos_cyton_config seen;
    t->cleanup = 1; t->deadline = now_ms() + 10000;
    if (recover_partial(t)) {
        t->r->cleanup_error = errno;
        t->r->restoration = CYTON_DIAGNOSTIC_RESTORE_UNKNOWN;
        return -1;
    }
    if (send_command(t, "s", 1, command_deadline(t)) || drain(t)) first = errno;
    if (disable_z(t) && !first) first = errno;
    if (query(t, &seen)) { if (!first) first = errno; }
    else if ((seen.registers[15] || seen.registers[16]) && !first) first = EPROTO;
    /* Restore ALL channels: each x also changes the shared SRB1 setting. */
    for (i = 0; i < 8; ++i)
        if (set_x(t, i, t->r->before.registers[5 + i],
                  (t->r->before.registers[13] >> i) & 1u,
                  (t->r->before.registers[21] >> 5) & 1u) && !first) first = errno;
    {
        struct xtos_cyton_config routed = t->r->before;
        routed.registers[15] = routed.registers[16] = 0;
        if (verify(t, &routed, &seen)) { if (!first) first = errno; }
        /* A failed route restore must not re-enable saved excitation against
         * unverified routing. Keep injection disabled and retain failure. */
        if (!first) {
            for (i = 0; i < 8; ++i)
                if (set_z(t, i, (t->r->before.registers[15] >> i) & 1u,
                          (t->r->before.registers[16] >> i) & 1u) && !first) first = errno;
        }
    }
    if (verify(t, &t->r->before, &t->r->after) && !first) first = errno;
    t->r->cleanup_error = first;
    t->r->restoration = first ? (t->r->after.register_mask ?
        CYTON_DIAGNOSTIC_RESTORE_FAILED : CYTON_DIAGNOSTIC_RESTORE_UNKNOWN) : CYTON_DIAGNOSTIC_RESTORED;
    return first ? fail(first) : 0;
}
int cyton_diagnostic_capture(const struct cyton_diagnostic_options *o,
                             struct cyton_diagnostic_result *r)
{
    struct transport t;
    struct termios term;
    int error = 0;
    if (!r) return fail(EINVAL);
    memset(r, 0, sizeof(*r));
    if (!o || !o->path || o->channel > 7 || o->settle_frames > 1024 ||
        o->sample_frames < 32 || o->sample_frames > 4096 ||
        !o->command_timeout_ms || o->command_timeout_ms > 3000 ||
        !o->operation_timeout_ms || o->operation_timeout_ms > 120000 ||
        (o->mode != CYTON_DIAGNOSTIC_SHORTED && o->mode != CYTON_DIAGNOSTIC_IMPEDANCE) ||
        (o->mode == CYTON_DIAGNOSTIC_IMPEDANCE && o->input != 'P' && o->input != 'N')) {
        r->error = EINVAL; return fail(EINVAL);
    }
    r->source_mode = o->mode; r->selected_channel = o->channel; r->selected_input = o->input;
    memset(&t, 0, sizeof(t)); t.fd = -1; t.o = o; t.r = r;
    t.deadline = now_ms() + o->operation_timeout_ms;
    t.fd = open(o->path, O_RDWR|O_NOCTTY|O_NONBLOCK|O_CLOEXEC);
    if (t.fd < 0) { error = errno; goto done; }
    if (tcgetattr(t.fd, &term) || ioctl(t.fd, TIOCEXCL)) { error = errno; goto done; }
    cfmakeraw(&term); term.c_cflag &= ~(CSIZE|PARENB|CSTOPB|CRTSCTS);
    term.c_cflag |= CS8|CLOCAL|CREAD;
    if (cfsetispeed(&term, B115200) || cfsetospeed(&term, B115200) ||
        tcsetattr(t.fd, TCSANOW, &term)) { error = errno; goto done; }
    if (send_command(&t, "s", 1, command_deadline(&t)) || drain(&t) ||
        version(&t) || query(&t, &r->before) || preflight(&t)) { error = errno; goto done; }
    if (prepare_test(&t) || capture_frames(&t)) error = errno;
    if (cleanup(&t) && !error) error = errno;
done:
    if (t.fd >= 0) close(t.fd);
    r->error = error;
    if (error) return fail(error);
    return 0;
}
