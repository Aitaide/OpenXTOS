/* SPDX-License-Identifier: GPL-2.0 */
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>
#include "xtos/bci.h"
#include "bci_config_cli.h"

static void json_string(const char *text)
{
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)text; *p; p++) {
        if (*p == '"' || *p == '\\') { putchar('\\'); putchar(*p); }
        else if (*p < 0x20) printf("\\u%04x", *p);
        else putchar(*p);
    }
    putchar('"');
}

int bci_config_cli_main(int argc, char **argv)
{
    struct xtos_bci_config_v1 config;
    xtos_bci_session_t *session;
    if (argc != 4 || !argv || !argv[1] || !argv[2] || !argv[3] || !argv[3][0] ||
        strcmp(argv[1], "config") || strcmp(argv[2], "--device")) {
        fprintf(stderr, "usage: bci config --device /dev/xtosN\n");
        return 2;
    }
    session = xtos_bci_session_create(argv[3]);
    if (!session) { perror("bci config: open"); return 3; }
    if (xtos_bci_get_config(session, &config)) {
        int saved = errno;
        xtos_bci_session_destroy(session);
        errno = saved;
        perror("bci config: query");
        return 3;
    }
    xtos_bci_session_destroy(session);
    printf("{\"schema\":\"xtos-bci-config-v1\",\"version\":%" PRIu32
           ",\"channels\":%" PRIu32 ",\"sample_rate_hz\":%" PRIu32
           ",\"enabled_mask\":%" PRIu32 ",\"capabilities\":%" PRIu32
           ",\"generation\":%" PRIu64 ",\"gain\":[", config.version,
           config.channels, config.sample_rate_hz, config.enabled_mask,
           config.capabilities, config.generation);
    for (unsigned int i = 0; i < 8; i++) printf("%s%" PRIu32, i ? "," : "", config.gain[i]);
    printf("],\"registers\":[");
    for (unsigned int i = 0; i < 24; i++) printf("%s%u", i ? "," : "", config.registers[i]);
    printf("],\"firmware\":");
    json_string(config.firmware);
    printf(",\"physical_acceptance\":false}\n");
    if (fflush(stdout) == EOF || ferror(stdout)) {
        fprintf(stderr, "bci config: output failed\n");
        return 3;
    }
    return 0;
}
