/* SPDX-License-Identifier: GPL-2.0 */
#ifndef BCI_DIAGNOSTIC_OPTIONS_H
#define BCI_DIAGNOSTIC_OPTIONS_H
#include <errno.h>
#include <stdlib.h>
#include <string.h>

enum bci_diagnostic_cli_mode {
    BCI_DIAGNOSTIC_CLI_SHORTED = 1,
    BCI_DIAGNOSTIC_CLI_IMPEDANCE_P,
    BCI_DIAGNOSTIC_CLI_IMPEDANCE_N
};
struct bci_diagnostic_cli_options {
    const char *device, *output;
    enum bci_diagnostic_cli_mode mode;
    unsigned int channel, frames, settle_frames, timeout_ms;
};
static inline int bci_diagnostic_options_parse(int argc, char **argv,
    struct bci_diagnostic_cli_options *result)
{
    struct bci_diagnostic_cli_options options = {
        .frames = 1000, .settle_frames = 250, .timeout_ms = 3000
    };
    unsigned int seen = 0;
    if (!result || !argv || argc < 2 || (argc % 2) ||
        !argv[1] || strcmp(argv[1], "diagnose")) return -1;
    for (int i = 2; i < argc; i += 2) {
        unsigned int bit;
        if (!argv[i] || !argv[i + 1] || !argv[i + 1][0]) return -1;
        if (!strcmp(argv[i], "--device")) {
            bit = 1; options.device = argv[i + 1];
        } else if (!strcmp(argv[i], "--output")) {
            bit = 2; options.output = argv[i + 1];
        } else if (!strcmp(argv[i], "--mode")) {
            bit = 4;
            if (!strcmp(argv[i + 1], "shorted")) options.mode = BCI_DIAGNOSTIC_CLI_SHORTED;
            else if (!strcmp(argv[i + 1], "impedance-p")) options.mode = BCI_DIAGNOSTIC_CLI_IMPEDANCE_P;
            else if (!strcmp(argv[i + 1], "impedance-n")) options.mode = BCI_DIAGNOSTIC_CLI_IMPEDANCE_N;
            else return -1;
        } else if (!strcmp(argv[i], "--channel")) {
            char *end;
            unsigned long value;
            bit = 8;
            if (strspn(argv[i + 1], "0123456789") != strlen(argv[i + 1])) return -1;
            errno = 0;
            value = strtoul(argv[i + 1], &end, 10);
            if (errno || *end || value < 1 || value > 8) return -1;
            options.channel = (unsigned int)value - 1;
        } else if (!strcmp(argv[i], "--frames") ||
                   !strcmp(argv[i], "--settle-frames") ||
                   !strcmp(argv[i], "--command-timeout-ms")) {
            char *end;
            unsigned long value, minimum, maximum;
            unsigned int *destination;
            if (!strcmp(argv[i], "--frames")) {
                bit = 16; minimum = 32; maximum = 4096; destination = &options.frames;
            } else if (!strcmp(argv[i], "--settle-frames")) {
                bit = 32; minimum = 0; maximum = 1024; destination = &options.settle_frames;
            } else {
                bit = 64; minimum = 1; maximum = 3000; destination = &options.timeout_ms;
            }
            if (strspn(argv[i + 1], "0123456789") != strlen(argv[i + 1])) return -1;
            errno = 0;
            value = strtoul(argv[i + 1], &end, 10);
            if (errno || *end || value < minimum || value > maximum) return -1;
            *destination = (unsigned int)value;
        } else return -1;
        if (seen & bit) return -1;
        seen |= bit;
    }
    if ((seen & 15) != 15) return -1;
    *result = options;
    return 0;
}
#endif
