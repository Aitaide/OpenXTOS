/* SPDX-License-Identifier: Apache-2.0 */
/*
 * Fuzz the exact Cyton parser shared with the kernel driver.
 * Build with -fsanitize=address,undefined and feed arbitrary packet streams.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../../kernel/linux/drivers/xtos/bci/xtos_cyton_packet.h"

struct harness_priv {
    xtos_cyton_parser parser;
    unsigned long packets_received;
    unsigned long packets_dropped;
    unsigned long samples_written;
    unsigned long samples_dropped;
};

struct harness_ringbuf {
    int32_t buffer[XTOS_CYTON_CHANNELS * 64];
    size_t size;
    size_t write_pos;
    size_t read_pos;
    unsigned long overruns;
};

static void ringbuf_write(struct harness_ringbuf *rb,
                          const int32_t samples[XTOS_CYTON_CHANNELS])
{
    size_t len = XTOS_CYTON_CHANNELS * sizeof(*samples);
    size_t rb_bytes = rb->size;
    size_t fill = rb->write_pos - rb->read_pos;
    size_t avail = rb_bytes - fill;
    size_t pos, to_end;

    if (len > rb_bytes) {
        rb->overruns++;
        return;
    }
    if (len > avail) {
        rb->read_pos += len - avail;
        rb->overruns++;
    }
    pos = rb->write_pos % rb_bytes;
    to_end = rb_bytes - pos;
    if (len <= to_end) {
        memcpy((char *)rb->buffer + pos, samples, len);
    } else {
        memcpy((char *)rb->buffer + pos, samples, to_end);
        memcpy(rb->buffer, (const char *)samples + to_end, len - to_end);
    }
    rb->write_pos += len;
}

static void receive_bytes(struct harness_priv *priv, struct harness_ringbuf *rb,
                          const unsigned char *data, size_t count)
{
    size_t i;
    int32_t samples[XTOS_CYTON_CHANNELS];

    for (i = 0; i < count; i++) {
        int result = xtos_cyton_parser_feed(&priv->parser, data[i], samples);
        if (result < 0) {
            priv->packets_dropped++;
        } else if (result > 0) {
            ringbuf_write(rb, samples);
            priv->packets_received++;
            priv->samples_written += XTOS_CYTON_CHANNELS;
        }
        if (priv->parser.packet_pos >= XTOS_CYTON_PACKET_SIZE) {
            fprintf(stderr, "INVARIANT VIOLATION: packet_pos out of range\n");
            abort();
        }
    }
}

int LLVMFuzzerTestOneInput(const unsigned char *data, size_t size)
{
    struct harness_priv priv;
    struct harness_ringbuf rb;
    size_t offset = 0;
    unsigned int step_seed = 0;

    memset(&priv, 0, sizeof(priv));
    memset(&rb, 0, sizeof(rb));
    xtos_cyton_parser_init(&priv.parser);
    rb.size = sizeof(rb.buffer);

    while (offset < size) {
        size_t chunk = 1 + ((data[offset] + step_seed) % 17);
        if (chunk > size - offset)
            chunk = size - offset;
        receive_bytes(&priv, &rb, data + offset, chunk);
        step_seed = step_seed * 31 + data[offset] + 1;
        offset += chunk;
    }
    return 0;
}

#ifndef __KERNEL__
int main(int argc, char **argv)
{
    int i;
    for (i = 1; i < argc; i++) {
        FILE *file = fopen(argv[i], "rb");
        unsigned char buffer[1 << 16];
        size_t count;
        if (!file)
            continue;
        count = fread(buffer, 1, sizeof(buffer), file);
        fclose(file);
        if (count)
            LLVMFuzzerTestOneInput(buffer, count);
    }
    fprintf(stderr, "processed %d corpus file(s)\n", argc - 1);
    return 0;
}
#endif
