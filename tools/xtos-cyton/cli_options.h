/* SPDX-License-Identifier: GPL-2.0 */
#ifndef BCI_CLI_OPTIONS_H
#define BCI_CLI_OPTIONS_H
#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
struct bci_cli_options {
    const char *input, *device, *output;
    unsigned capacity, duration_ms, command_timeout_ms, reconnect_delay_ms, reconnect_attempts;
};
static inline int bci_cli_options_parse(int argc, char **argv, struct bci_cli_options *o)
{
    unsigned seen = 0;
    if (!o || !argv) return -1;
    *o = (struct bci_cli_options){.capacity=256,.duration_ms=30000,.command_timeout_ms=1500,
                                .reconnect_delay_ms=100,.reconnect_attempts=3};
    for (int i=1;i<argc;i+=2) {
        unsigned bit, max=0; unsigned *number=NULL; const char **string=NULL;
        if (i+1>=argc) return -1;
        if (!strcmp(argv[i],"--input")) { bit=1; string=&o->input; }
        else if (!strcmp(argv[i],"--device")) { bit=2; string=&o->device; }
        else if (!strcmp(argv[i],"--output")) { bit=4; string=&o->output; }
        else if (!strcmp(argv[i],"--capacity")) { bit=8; number=&o->capacity; max=65536; }
        else if (!strcmp(argv[i],"--duration-ms")) { bit=16; number=&o->duration_ms; max=86400000; }
        else if (!strcmp(argv[i],"--command-timeout-ms")) { bit=32; number=&o->command_timeout_ms; max=60000; }
        else if (!strcmp(argv[i],"--reconnect-delay-ms")) { bit=64; number=&o->reconnect_delay_ms; max=60000; }
        else if (!strcmp(argv[i],"--reconnect-attempts")) { bit=128; number=&o->reconnect_attempts; max=32; }
        else return -1;
        if (seen&bit) return -1;
        seen|=bit;
        if (string) { if (!argv[i+1][0]) return -1; *string=argv[i+1]; }
        else {
            char *end; unsigned long value;
            if (!argv[i+1][0] || strspn(argv[i+1],"0123456789")!=strlen(argv[i+1])) return -1;
            errno=0; value=strtoul(argv[i+1],&end,10);
            if (errno || *end || value>max || (!value && bit!=128)) return -1;
            *number=(unsigned)value;
        }
    }
    return ((!o->input == !o->device) || !o->output) ? -1 : 0;
}
#endif
