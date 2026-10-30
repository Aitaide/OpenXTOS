/* SPDX-License-Identifier: GPL-2.0 */
#include <stdio.h>
#include <stdlib.h>
#include "../../kernel/linux/drivers/xtos/bci/xtos_cyton_config.h"

int main(int argc, char **argv)
{
    unsigned char buffer[8193];
    struct xtos_cyton_config output, sentinel;
    size_t n = fread(buffer, 1, sizeof(buffer), stdin);
    char *input = malloc(n ? n : 1);
    unsigned int i;
    int rc;

    if (!input || ferror(stdin)) return 2;
    memcpy(input, buffer, n);
    memset(&output, 0xA5, sizeof(output));
    memcpy(&sentinel, &output, sizeof(output));
    rc = xtos_cyton_config_parse(argc > 1 && !strcmp(argv[1], "null-input") ? NULL : input,
                                 n, argc > 1 && !strcmp(argv[1], "null-output") ? NULL : &output);
    printf("%d %d %d %u %u", rc, !memcmp(&output, &sentinel, sizeof(output)),
           !memcmp(input, buffer, n), output.enabled_mask, output.register_mask);
    for (i = 0; i < 8; ++i) printf(" %u", output.gain[i]);
    for (i = 0; i < 24; ++i) printf(" %u", output.registers[i]);
    putchar('\n');
    free(input);
    return 0;
}
