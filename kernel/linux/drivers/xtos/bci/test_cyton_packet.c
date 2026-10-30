/* SPDX-License-Identifier: Apache-2.0 */
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "xtos_cyton_packet.h"

#define CHECK(condition, message) do { \
    if (!(condition)) { \
        fprintf(stderr, "%s:%d: %s\n", __func__, __LINE__, message); \
        return 1; \
    } \
} while (0)

static void set_channel(uint8_t *packet, unsigned int channel, int32_t raw)
{
    uint32_t bits = (uint32_t)raw & UINT32_C(0xffffff);
    unsigned int offset = 2 + channel * 3;

    packet[offset] = (uint8_t)(bits >> 16);
    packet[offset + 1] = (uint8_t)(bits >> 8);
    packet[offset + 2] = (uint8_t)bits;
}

static void make_packet(uint8_t *packet, uint8_t counter, int32_t raw)
{
    unsigned int channel;

    memset(packet, 0, XTOS_CYTON_PACKET_SIZE);
    packet[0] = 0xa0;
    packet[1] = counter;
    for (channel = 0; channel < XTOS_CYTON_CHANNELS; ++channel)
        set_channel(packet, channel, raw + (int32_t)channel);
    packet[32] = 0xc0;
}

/* Independent rational oracle: cancel gain 24 against the 4.5 V reference.
 * Input is the generated signed value, never a second decode of packet bytes.
 * C signed division supplies the specified truncation toward zero. */
static int32_t reference_nv(int32_t raw)
{
    return (int32_t)(((int64_t)raw * INT64_C(187500000)) / INT64_C(8388607));
}

static void sentinel(int32_t *output)
{
    memset(output, 0x5a, XTOS_CYTON_CHANNELS * sizeof(*output));
}

/* Every byte that does not publish a frame must preserve all output bytes. */
static int feed_frame(xtos_cyton_parser *parser, const uint8_t *packet,
                      int32_t *output)
{
    unsigned int index;
    int result = 0;

    for (index = 0; index < XTOS_CYTON_PACKET_SIZE; ++index) {
        int32_t before[XTOS_CYTON_CHANNELS];

        memcpy(before, output, sizeof(before));
        result = xtos_cyton_parser_feed(parser, packet[index], output);
        if ((result <= 0 && memcmp(before, output, sizeof(before)) != 0) ||
            (index < XTOS_CYTON_PACKET_SIZE - 1 && result != 0) ||
            parser->packet_pos >= XTOS_CYTON_PACKET_SIZE)
            return -2;
    }
    return result;
}

static int test_offsets(void)
{
    uint8_t packet[XTOS_CYTON_PACKET_SIZE];
    int32_t output[XTOS_CYTON_CHANNELS];
    unsigned int channel;

    make_packet(packet, 0xa5, 1);
    memset(packet + 26, 0xff, 6); /* AUX must never be an EEG channel. */
    CHECK(xtos_cyton_decode_packet(packet, output) == 0, "valid frame rejected");
    for (channel = 0; channel < XTOS_CYTON_CHANNELS; ++channel)
        CHECK(output[channel] == reference_nv((int32_t)channel + 1),
              "counter/channel/AUX offset or scale incorrect");
    return 0;
}

static int test_signed_endpoints(void)
{
    static const int32_t raw[] = {-8388608, 8388607, -1, 1, 0, -2, 2, -4194304};
    static const int32_t expected[] = {
        -187500022, 187500000, -22, 22, 0, -44, 44, -93750011
    };
    uint8_t packet[XTOS_CYTON_PACKET_SIZE];
    int32_t output[XTOS_CYTON_CHANNELS];
    unsigned int channel;

    make_packet(packet, 1, 0);
    for (channel = 0; channel < XTOS_CYTON_CHANNELS; ++channel)
        set_channel(packet, channel, raw[channel]);
    CHECK(xtos_cyton_decode_packet(packet, output) == 0, "signed frame rejected");
    CHECK(memcmp(output, expected, sizeof(expected)) == 0,
          "signed endpoint, sign extension or truncation incorrect");
    return 0;
}

static int test_all_footer_types(void)
{
    uint8_t packet[XTOS_CYTON_PACKET_SIZE];
    int32_t output[XTOS_CYTON_CHANNELS];
    xtos_cyton_parser parser;
    unsigned int type;

    xtos_cyton_parser_init(&parser);
    for (type = 0; type < 16; ++type) {
        make_packet(packet, (uint8_t)type, 10);
        packet[32] = (uint8_t)(0xc0 + type);
        CHECK(xtos_cyton_decode_packet(packet, output) == 0, "footer type rejected");
        CHECK(feed_frame(&parser, packet, output) == 1, "stream footer type rejected");
        CHECK(output[7] == reference_nv(17), "footer changed decoded EEG");
    }
    CHECK(parser.missing_samples == 0, "footer variants introduced gaps");
    return 0;
}

static int test_invalid_footer_atomicity(void)
{
    uint8_t packet[XTOS_CYTON_PACKET_SIZE];
    int32_t output[XTOS_CYTON_CHANNELS], before[XTOS_CYTON_CHANNELS];
    unsigned int footer;

    make_packet(packet, 1, 0);
    sentinel(output);
    memcpy(before, output, sizeof(before));
    for (footer = 0; footer <= 255; ++footer) {
        if ((footer & 0xf0) == 0xc0)
            continue;
        packet[32] = (uint8_t)footer;
        CHECK(xtos_cyton_decode_packet(packet, output) == -1, "bad footer accepted");
        CHECK(memcmp(output, before, sizeof(before)) == 0, "bad footer wrote output");
    }
    return 0;
}

static int test_invalid_start_atomicity(void)
{
    uint8_t packet[XTOS_CYTON_PACKET_SIZE];
    int32_t output[XTOS_CYTON_CHANNELS], before[XTOS_CYTON_CHANNELS];
    unsigned int start;

    make_packet(packet, 1, 0);
    sentinel(output);
    memcpy(before, output, sizeof(before));
    for (start = 0; start <= 255; ++start) {
        if (start == 0xa0)
            continue;
        packet[0] = (uint8_t)start;
        CHECK(xtos_cyton_decode_packet(packet, output) == -1, "bad start accepted");
        CHECK(memcmp(output, before, sizeof(before)) == 0, "bad start wrote output");
    }
    return 0;
}

static int test_noise_and_split(void)
{
    uint8_t packet[XTOS_CYTON_PACKET_SIZE];
    int32_t output[XTOS_CYTON_CHANNELS], before[XTOS_CYTON_CHANNELS];
    xtos_cyton_parser parser;
    unsigned int byte, split, index;

    make_packet(packet, 5, -10);
    for (split = 1; split < XTOS_CYTON_PACKET_SIZE; ++split) {
        xtos_cyton_parser_init(&parser);
        sentinel(output);
        memcpy(before, output, sizeof(before));
        for (byte = 0; byte <= 255; ++byte) {
            if (byte != 0xa0)
                CHECK(xtos_cyton_parser_feed(&parser, (uint8_t)byte, output) == 0,
                      "noise rejected as a full candidate");
        }
        for (index = 0; index < split; ++index)
            CHECK(xtos_cyton_parser_feed(&parser, packet[index], output) == 0,
                  "partial frame published");
        CHECK(parser.packet_pos == split, "partial frame state lost");
        CHECK(memcmp(output, before, sizeof(before)) == 0, "noise/partial wrote output");
        for (; index < XTOS_CYTON_PACKET_SIZE; ++index) {
            int expected = index == XTOS_CYTON_PACKET_SIZE - 1 ? 1 : 0;
            CHECK(xtos_cyton_parser_feed(&parser, packet[index], output) == expected,
                  "split frame not completed exactly once");
        }
        CHECK(output[0] == reference_nv(-10), "split frame data incorrect");
    }
    return 0;
}

static int test_bad_footer_recovery(void)
{
    uint8_t stream[XTOS_CYTON_PACKET_SIZE * 2];
    int32_t output[XTOS_CYTON_CHANNELS], before[XTOS_CYTON_CHANNELS];
    xtos_cyton_parser parser;
    unsigned int index, frames = 0, rejected = 0;

    make_packet(stream, 2, 10);
    stream[32] = 0xb0;
    make_packet(stream + XTOS_CYTON_PACKET_SIZE, 3, 20);
    sentinel(output);
    xtos_cyton_parser_init(&parser);
    for (index = 0; index < sizeof(stream); ++index) {
        int result;
        memcpy(before, output, sizeof(before));
        result = xtos_cyton_parser_feed(&parser, stream[index], output);
        if (result <= 0)
            CHECK(memcmp(before, output, sizeof(before)) == 0, "recovery wrote output early");
        frames += result == 1;
        rejected += result == -1;
    }
    CHECK(frames == 1 && rejected == 1, "bad footer recovery counts incorrect");
    CHECK(output[0] == reference_nv(20), "recovered data incorrect");
    CHECK(parser.last_counter == 3 && parser.missing_samples == 0,
          "malformed first frame affected counter state");
    return 0;
}

static int test_embedded_start_recovery(void)
{
    uint8_t stream[XTOS_CYTON_PACKET_SIZE * 2];
    int32_t output[XTOS_CYTON_CHANNELS], before[XTOS_CYTON_CHANNELS];
    unsigned int offset, index;

    /* An interrupted frame prefix followed by a real frame. Every possible
     * embedded-start position must preserve enough suffix to recover it. */
    for (offset = 1; offset < XTOS_CYTON_PACKET_SIZE; ++offset) {
        xtos_cyton_parser parser;
        unsigned int frames = 0, rejected = 0;

        memset(stream, 0x55, sizeof(stream));
        stream[0] = 0xa0;
        make_packet(stream + offset, 7, 10);
        xtos_cyton_parser_init(&parser);
        sentinel(output);
        for (index = 0; index < offset + XTOS_CYTON_PACKET_SIZE; ++index) {
            int result;
            memcpy(before, output, sizeof(before));
            result = xtos_cyton_parser_feed(&parser, stream[index], output);
            if (result <= 0)
                CHECK(memcmp(before, output, sizeof(before)) == 0,
                      "embedded-start rejection changed output");
            CHECK(parser.packet_pos < XTOS_CYTON_PACKET_SIZE, "unbounded retained suffix");
            frames += result == 1;
            rejected += result == -1;
        }
        CHECK(frames == 1 && rejected == 1, "embedded-start frame not recovered");
        CHECK(output[0] == reference_nv(10), "embedded-start recovery data incorrect");
    }
    return 0;
}

static int test_multiple_embedded_starts(void)
{
    uint8_t stream[XTOS_CYTON_PACKET_SIZE + 10];
    int32_t output[XTOS_CYTON_CHANNELS];
    xtos_cyton_parser parser;
    unsigned int index, frames = 0, rejected = 0;

    memset(stream, 0x55, sizeof(stream));
    stream[0] = 0xa0;
    stream[3] = 0xa0; /* False suffix candidate before the genuine start. */
    make_packet(stream + 10, 8, 10);
    sentinel(output);
    xtos_cyton_parser_init(&parser);
    for (index = 0; index < sizeof(stream); ++index) {
        int32_t before[XTOS_CYTON_CHANNELS];
        int result;
        memcpy(before, output, sizeof(before));
        result = xtos_cyton_parser_feed(&parser, stream[index], output);
        if (result <= 0)
            CHECK(memcmp(before, output, sizeof(before)) == 0, "false suffix wrote output");
        frames += result == 1;
        rejected += result == -1;
    }
    CHECK(frames == 1 && rejected == 2, "multiple suffix resynchronization failed");
    CHECK(output[7] == reference_nv(17), "multiple suffix recovery data incorrect");
    return 0;
}

static int test_counter_wrap(void)
{
    uint8_t packet[XTOS_CYTON_PACKET_SIZE];
    int32_t output[XTOS_CYTON_CHANNELS] = {0};
    xtos_cyton_parser parser;

    xtos_cyton_parser_init(&parser);
    make_packet(packet, 255, 1);
    CHECK(feed_frame(&parser, packet, output) == 1, "counter 255 not accepted");
    CHECK(parser.counter_valid && parser.last_counter == 255, "counter 255 not recorded");
    make_packet(packet, 0, 2);
    CHECK(feed_frame(&parser, packet, output) == 1, "counter wrap not accepted");
    CHECK(parser.last_counter == 0 && parser.missing_samples == 0, "wrap caused false loss");
    CHECK(output[0] == reference_nv(2), "wrap output incorrect");
    return 0;
}

static int test_counter_gap(void)
{
    uint8_t packet[XTOS_CYTON_PACKET_SIZE];
    int32_t output[XTOS_CYTON_CHANNELS] = {0};
    xtos_cyton_parser parser;

    xtos_cyton_parser_init(&parser);
    make_packet(packet, 10, 1);
    CHECK(feed_frame(&parser, packet, output) == 1, "gap seed failed");
    make_packet(packet, 13, 2);
    CHECK(feed_frame(&parser, packet, output) == 1, "gap frame rejected");
    CHECK(parser.missing_samples == 2, "gap loss count incorrect");
    make_packet(packet, 17, 3);
    CHECK(feed_frame(&parser, packet, output) == 1, "second gap rejected");
    CHECK(parser.missing_samples == 5 && parser.last_counter == 17,
          "gap losses not accumulated");
    return 0;
}

static int test_all_counter_deltas(void)
{
    static const uint8_t seeds[] = {0, 10, 127, 255};
    uint8_t packet[XTOS_CYTON_PACKET_SIZE];
    unsigned int seed, delta;

    for (seed = 0; seed < sizeof(seeds) / sizeof(seeds[0]); ++seed) {
        for (delta = 0; delta <= 255; ++delta) {
            xtos_cyton_parser parser;
            int32_t output[XTOS_CYTON_CHANNELS] = {0}, before[XTOS_CYTON_CHANNELS];
            int accepted = delta > 0 && delta < 128;

            xtos_cyton_parser_init(&parser);
            make_packet(packet, seeds[seed], 10);
            CHECK(feed_frame(&parser, packet, output) == 1, "delta seed failed");
            memcpy(before, output, sizeof(before));
            make_packet(packet, (uint8_t)(seeds[seed] + delta), 20);
            CHECK(feed_frame(&parser, packet, output) == (accepted ? 1 : -1),
                  "duplicate/backward/half-range delta policy incorrect");
            CHECK(parser.missing_samples == (accepted ? delta - 1 : 0),
                  "rejected delta affected missing samples");
            CHECK(parser.last_counter == (accepted ? packet[1] : seeds[seed]),
                  "rejected delta advanced counter");
            if (!accepted) {
                CHECK(memcmp(output, before, sizeof(before)) == 0,
                      "duplicate/backward frame wrote output");
                make_packet(packet, (uint8_t)(seeds[seed] + 1), 30);
                CHECK(feed_frame(&parser, packet, output) == 1,
                      "valid frame after rejected delta failed");
                CHECK(parser.missing_samples == 0, "rejection corrupted subsequent gap");
            }
        }
    }
    return 0;
}

static int test_wrapped_gap(void)
{
    uint8_t packet[XTOS_CYTON_PACKET_SIZE];
    int32_t output[XTOS_CYTON_CHANNELS] = {0};
    xtos_cyton_parser parser;

    xtos_cyton_parser_init(&parser);
    make_packet(packet, 254, 1);
    CHECK(feed_frame(&parser, packet, output) == 1, "wrapped gap seed failed");
    make_packet(packet, 2, 2);
    CHECK(feed_frame(&parser, packet, output) == 1, "wrapped gap rejected");
    CHECK(parser.missing_samples == 3 && parser.last_counter == 2,
          "wrapped gap count incorrect");
    return 0;
}

static int test_bad_footer_preserves_counter(void)
{
    uint8_t packet[XTOS_CYTON_PACKET_SIZE];
    int32_t output[XTOS_CYTON_CHANNELS] = {0};
    xtos_cyton_parser parser;

    xtos_cyton_parser_init(&parser);
    make_packet(packet, 10, 1);
    CHECK(feed_frame(&parser, packet, output) == 1, "footer counter seed failed");
    make_packet(packet, 11, 2);
    packet[32] = 0xb0;
    CHECK(feed_frame(&parser, packet, output) == -1, "bad footer not rejected");
    CHECK(parser.last_counter == 10 && parser.missing_samples == 0,
          "bad footer advanced counter");
    make_packet(packet, 12, 3);
    CHECK(feed_frame(&parser, packet, output) == 1, "post-footer frame failed");
    CHECK(parser.missing_samples == 1, "bad footer loss not counted on recovery");
    return 0;
}

static int test_random_signed24_oracle(void)
{
    uint32_t state = UINT32_C(0x384bc217);
    uint8_t packet[XTOS_CYTON_PACKET_SIZE];
    int32_t output[XTOS_CYTON_CHANNELS], raw[XTOS_CYTON_CHANNELS];
    unsigned int frame, channel;

    for (frame = 0; frame < 4096; ++frame) {
        make_packet(packet, (uint8_t)frame, 0);
        packet[32] = (uint8_t)(0xc0 | (frame & 15));
        for (channel = 0; channel < XTOS_CYTON_CHANNELS; ++channel) {
            state ^= state << 13;
            state ^= state >> 17;
            state ^= state << 5;
            raw[channel] = (int32_t)(state % UINT32_C(16777216)) - 8388608;
            set_channel(packet, channel, raw[channel]);
        }
        CHECK(xtos_cyton_decode_packet(packet, output) == 0, "random frame rejected");
        for (channel = 0; channel < XTOS_CYTON_CHANNELS; ++channel)
            CHECK(output[channel] == reference_nv(raw[channel]), "independent scale mismatch");
    }
    return 0;
}

static int test_missing_samples_saturates(void)
{
    uint8_t packet[XTOS_CYTON_PACKET_SIZE];
    int32_t output[XTOS_CYTON_CHANNELS] = {0};
    xtos_cyton_parser parser;

    xtos_cyton_parser_init(&parser);
    make_packet(packet, 10, 1);
    CHECK(feed_frame(&parser, packet, output) == 1, "overflow seed failed");
    parser.missing_samples = ULONG_MAX - 1;
    make_packet(packet, 12, 2);
    CHECK(feed_frame(&parser, packet, output) == 1, "exact-limit gap rejected");
    CHECK(parser.missing_samples == ULONG_MAX, "exact loss limit incorrect");
    make_packet(packet, 15, 3);
    CHECK(feed_frame(&parser, packet, output) == 1, "overflow gap rejected");
    CHECK(parser.missing_samples == ULONG_MAX, "loss counter wrapped instead of saturating");
    make_packet(packet, 16, 4);
    CHECK(feed_frame(&parser, packet, output) == 1, "frame after saturation rejected");
    CHECK(parser.missing_samples == ULONG_MAX && parser.last_counter == 16,
          "saturation damaged counter progress");
    return 0;
}

static int test_reinitialization(void)
{
    xtos_cyton_parser parser;
    uint8_t packet[XTOS_CYTON_PACKET_SIZE];
    int32_t output[XTOS_CYTON_CHANNELS] = {0};

    memset(&parser, 0xff, sizeof(parser));
    xtos_cyton_parser_init(&parser);
    CHECK(parser.packet_pos == 0 && parser.counter_valid == 0 &&
          parser.last_counter == 0 && parser.missing_samples == 0,
          "initialization did not reset logical state");
    make_packet(packet, 200, 1);
    CHECK(feed_frame(&parser, packet, output) == 1, "reset did not permit arbitrary first counter");
    return 0;
}

static int test_null_safety(void)
{
    xtos_cyton_parser parser, before_parser;
    uint8_t packet[XTOS_CYTON_PACKET_SIZE];
    int32_t output[XTOS_CYTON_CHANNELS], before[XTOS_CYTON_CHANNELS];

    make_packet(packet, 1, 0);
    sentinel(output);
    memcpy(before, output, sizeof(before));
    CHECK(xtos_cyton_decode_packet(NULL, output) == -1, "null packet not rejected");
    CHECK(xtos_cyton_decode_packet(packet, NULL) == -1, "null decode output not rejected");
    CHECK(xtos_cyton_decode_packet(NULL, NULL) == -1, "null decode arguments not rejected");
    CHECK(xtos_cyton_parser_feed(NULL, 0xa0, output) == -1, "null parser not rejected");
    memset(&parser, 0, sizeof(parser));
    xtos_cyton_parser_init(&parser);
    CHECK(xtos_cyton_parser_feed(&parser, 0xa0, output) == 0, "null test seed failed");
    memcpy(&before_parser, &parser, sizeof(parser));
    CHECK(xtos_cyton_parser_feed(&parser, 1, NULL) == -1, "null stream output not rejected");
    CHECK(memcmp(&parser, &before_parser, sizeof(parser)) == 0, "null output mutated parser");
    CHECK(memcmp(output, before, sizeof(before)) == 0, "null argument wrote output");
    xtos_cyton_parser_init(NULL);
    return 0;
}

int main(void)
{
    static const struct {
        const char *name;
        int (*run)(void);
    } tests[] = {
        {"offsets", test_offsets},
        {"signed_endpoints", test_signed_endpoints},
        {"all_footer_types", test_all_footer_types},
        {"invalid_footer_atomicity", test_invalid_footer_atomicity},
        {"invalid_start_atomicity", test_invalid_start_atomicity},
        {"noise_and_split", test_noise_and_split},
        {"bad_footer_recovery", test_bad_footer_recovery},
        {"embedded_start_recovery", test_embedded_start_recovery},
        {"multiple_embedded_starts", test_multiple_embedded_starts},
        {"counter_wrap", test_counter_wrap},
        {"counter_gap", test_counter_gap},
        {"all_counter_deltas", test_all_counter_deltas},
        {"wrapped_gap", test_wrapped_gap},
        {"bad_footer_preserves_counter", test_bad_footer_preserves_counter},
        {"random_signed24_oracle", test_random_signed24_oracle},
        {"missing_samples_saturates", test_missing_samples_saturates},
        {"reinitialization", test_reinitialization},
        {"null_safety", test_null_safety},
    };
    size_t index, passed = 0;
    const size_t cases = sizeof(tests) / sizeof(tests[0]);

    setvbuf(stdout, NULL, _IONBF, 0);
    for (index = 0; index < cases; ++index) {
        int result = tests[index].run();
        printf("case=%s result=%s\n", tests[index].name, result ? "fail" : "pass");
        passed += result == 0;
    }
    printf("cyton_packet_test=%s cases=%zu passed=%zu failed=%zu\n",
           passed == cases ? "pass" : "fail", cases, passed, cases - passed);
    return passed == cases ? 0 : 1;
}
