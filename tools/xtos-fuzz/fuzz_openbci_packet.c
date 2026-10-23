/* SPDX-License-Identifier: Apache-2.0 */
/*
 * Fuzz harness for the OpenBCI Cyton packet-parsing logic.
 *
 * The kernel driver (drivers/xtos/bci/devices/openbci_cyton.c) cannot be
 * executed in user space, so this harness mirrors its parsing state machine
 * and 24-bit fixed-point conversion exactly. A differential bug in the
 * harness logic that contradicts the kernel source means the kernel copy is
 * wrong too — keep the two in sync when the driver changes.
 *
 * Build with -fsanitize=address,undefined and feed it arbitrary byte
 * streams: fuzz_openbci_packet CORPUS_FILE...
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define OPENBCI_PACKET_SIZE 33
#define OPENBCI_START_BYTE 0xA0
#define OPENBCI_NUM_CHANNELS 8
#define OPENBCI_VREF_NV 4500000000LL
#define OPENBCI_GAIN 24

/* Mirror of kernel priv state */
struct harness_priv {
    unsigned char packet_buf[OPENBCI_PACKET_SIZE];
    int packet_pos;
    int receiving;
    unsigned long packets_received;
    unsigned long packets_dropped;
    unsigned long samples_written;
    unsigned long samples_dropped;
};

struct harness_ringbuf {
    int32_t buffer[OPENBCI_NUM_CHANNELS * 64];
    size_t size;            /* capacity in samples */
    size_t write_pos;
    size_t read_pos;
    unsigned long overruns;
};

static void ringbuf_write(struct harness_ringbuf *rb, const int32_t *samples,
                          size_t len_samples)
{
    size_t len = len_samples * sizeof(int32_t);
    size_t rb_bytes = rb->size * sizeof(int32_t);
    size_t fill, avail, pos, to_end;

    if (len > rb_bytes) {
        rb->overruns++;
        return;
    }

    fill = rb->write_pos - rb->read_pos;
    avail = rb_bytes - fill;
    if (len > avail) {
        rb->read_pos += (len - avail);
        rb->overruns++;
    }

    pos = (rb->write_pos * sizeof(int32_t)) % rb_bytes;
    to_end = rb_bytes - pos;
    if (len <= to_end) {
        memcpy((char *)rb->buffer + pos, samples, len);
    } else {
        memcpy((char *)rb->buffer + pos, samples, to_end);
        memcpy(rb->buffer, (const char *)samples + to_end, len - to_end);
    }
    rb->write_pos += len;
}

/* Mirror of openbci_process_packet */
static void process_packet(struct harness_priv *priv,
                           struct harness_ringbuf *rb,
                           const unsigned char *packet)
{
    int32_t samples[OPENBCI_NUM_CHANNELS];
    int i;

    if (packet[0] != OPENBCI_START_BYTE) {
        priv->packets_dropped++;
        return;
    }

    for (i = 0; i < OPENBCI_NUM_CHANNELS; i++) {
        int32_t raw = ((int32_t)packet[1 + i * 3] << 16) |
                      ((int32_t)packet[2 + i * 3] << 8) |
                      ((int32_t)packet[3 + i * 3]);
        if (raw & 0x800000)
            raw |= (int32_t)0xFF000000;
        samples[i] = (int32_t)(((int64_t)raw * OPENBCI_VREF_NV) /
                               ((int64_t)OPENBCI_GAIN * (1LL << 23)));
    }

    ringbuf_write(rb, samples, OPENBCI_NUM_CHANNELS);
    priv->packets_received++;
    priv->samples_written += OPENBCI_NUM_CHANNELS;
}

/* Mirror of openbci_receive_buf byte-stream state machine */
static void receive_bytes(struct harness_priv *priv, struct harness_ringbuf *rb,
                          const unsigned char *data, size_t count)
{
    size_t i;

    if (!priv->receiving)
        return;

    for (i = 0; i < count; i++) {
        unsigned char byte = data[i];

        if (priv->packet_pos == 0 && byte == OPENBCI_START_BYTE) {
            priv->packet_buf[priv->packet_pos++] = byte;
        } else if (priv->packet_pos > 0 &&
                   priv->packet_pos < OPENBCI_PACKET_SIZE) {
            priv->packet_buf[priv->packet_pos++] = byte;
            if (priv->packet_pos == OPENBCI_PACKET_SIZE) {
                process_packet(priv, rb, priv->packet_buf);
                priv->packet_pos = 0;
            }
        } else if (priv->packet_pos >= OPENBCI_PACKET_SIZE) {
            /* Kernel resets with a warning; mirror the reset. */
            priv->packet_pos = 0;
        }
    }
}

/* Property checks run after each input; violations abort with nonzero. */
static int check_invariants(const struct harness_priv *priv)
{
    if (priv->packet_pos < 0 || priv->packet_pos > OPENBCI_PACKET_SIZE)
        return 0;
    return 1;
}

int LLVMFuzzerTestOneInput(const unsigned char *data, size_t size)
{
    struct harness_priv priv;
    struct harness_ringbuf rb;

    memset(&priv, 0, sizeof(priv));
    memset(&rb, 0, sizeof(rb));
    priv.receiving = 1;
    rb.size = 64 * OPENBCI_NUM_CHANNELS;

    /* Feed the stream in randomly-split chunks to also exercise the
     * reassembly path, not only whole packets. */
    size_t off = 0;
    unsigned int step_seed = 0;
    while (off < size) {
        size_t chunk = 1 + ((data[off] + step_seed) % 17);
        if (chunk > size - off)
            chunk = size - off;
        receive_bytes(&priv, &rb, data + off, chunk);
        if (!check_invariants(&priv)) {
            fprintf(stderr, "INVARIANT VIOLATION: packet_pos out of range\n");
            abort();
        }
        step_seed = step_seed * 31 + data[off] + 1;
        off += chunk;
    }

    return 0;
}

/* Standalone (non-libFuzzer) entry: argv = corpus files. */
int main(int argc, char **argv)
{
    int i;
    for (i = 1; i < argc; i++) {
        FILE *f = fopen(argv[i], "rb");
        unsigned char buf[1 << 16];
        size_t n;
        if (!f)
            continue;
        n = fread(buf, 1, sizeof(buf), f);
        fclose(f);
        if (n)
            LLVMFuzzerTestOneInput(buf, n);
    }
    fprintf(stderr, "processed %d corpus file(s)\n", argc - 1);
    return 0;
}
