/* SPDX-License-Identifier: GPL-2.0 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "bci_diagnostic_options.h"

int main(int argc, char **argv)
{
    assert(argc == 2);
    struct bci_diagnostic_cli_options options;
    if (!strcmp(argv[1], "valid")) {
        char *args[] = {"bci", "diagnose", "--device", "/dev/fixture",
                        "--mode", "impedance-p", "--channel", "8",
                        "--output", "new.json"};
        assert(!bci_diagnostic_options_parse(10, args, &options));
        assert(options.mode == BCI_DIAGNOSTIC_CLI_IMPEDANCE_P);
        assert(options.channel == 7 && options.frames == 1000);
        assert(options.settle_frames == 250 && options.timeout_ms == 3000);
        assert(!strcmp(options.device, "/dev/fixture"));
        assert(!strcmp(options.output, "new.json"));
    } else if (!strcmp(argv[1], "invalid")) {
        static const char *bad[][10] = {
            {"bci", "diagnose", "--device", "tty", "--mode", "shorted", "--channel", "1", "--output", NULL},
            {"bci", "diagnose", "--device", "tty", "--mode", "shorted", "--output", "x", NULL, NULL},
            {"bci", "diagnose", "--device", "", "--mode", "shorted", "--channel", "1", "--output", "x"},
            {"bci", "diagnose", "--device", "tty", "--mode", "shorted", "--channel", "1junk", "--output", "x"},
            {"bci", "diagnose", "--device", "tty", "--mode", "shorted", "--channel", "9", "--output", "x"},
            {"bci", "diagnose", "--device", "tty", "--mode", "shorted", "--channel", "-1", "--output", "x"},
            {"bci", "diagnose", "--device", "tty", "--mode", "shorted", "--channel", "4294967297", "--output", "x"}
        };
        for (unsigned int i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
            char *args[10];
            for (unsigned int j = 0; j < 10; j++) args[j] = (char *)bad[i][j];
            memset(&options, 0x5a, sizeof(options));
            struct bci_diagnostic_cli_options sentinel = options;
            assert(bci_diagnostic_options_parse(i == 0 ? 9 : i == 1 ? 8 : 10,
                                                 args, &options) == -1);
            assert(!memcmp(&sentinel, &options, sizeof(options)));
        }
    } else if (!strcmp(argv[1], "transport_timeout_bound")) {
        char *args[] = {"bci", "diagnose", "--device", "tty", "--mode", "shorted",
                        "--channel", "1", "--output", "x", "--command-timeout-ms", "3000"};
        assert(!bci_diagnostic_options_parse(12, args, &options));
        assert(options.timeout_ms == 3000);
        const char *bad[] = {"3001", "60000", "4294967295"};
        for (unsigned int i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
            args[11] = (char *)bad[i];
            struct bci_diagnostic_cli_options sentinel = options;
            assert(bci_diagnostic_options_parse(12, args, &options) == -1);
            assert(!memcmp(&options, &sentinel, sizeof(options)));
        }
    } else if (!strcmp(argv[1], "window_bounds")) {
        char *args[] = {"bci", "diagnose", "--device", "tty", "--mode", "shorted",
                        "--channel", "1", "--output", "x", "--frames", "32",
                        "--settle-frames", "0", "--command-timeout-ms", "1"};
        assert(!bci_diagnostic_options_parse(16, args, &options));
        assert(options.frames == 32 && !options.settle_frames && options.timeout_ms == 1);
        args[11] = "4096"; args[13] = "1024"; args[15] = "3000";
        assert(!bci_diagnostic_options_parse(16, args, &options));
        assert(options.frames == 4096 && options.settle_frames == 1024 && options.timeout_ms == 3000);
        static const char *invalid[] = {"31", "4097", "-1", "1x", "4294967296"};
        for (unsigned int i = 0; i < sizeof(invalid) / sizeof(invalid[0]); i++) {
            args[11] = (char *)invalid[i];
            assert(bci_diagnostic_options_parse(16, args, &options) == -1);
        }
        args[11] = "32"; args[13] = "1025";
        assert(bci_diagnostic_options_parse(16, args, &options) == -1);
        args[13] = "0"; args[15] = "0";
        assert(bci_diagnostic_options_parse(16, args, &options) == -1);
    } else {
        return 2;
    }
    puts(argv[1]);
    return 0;
}
