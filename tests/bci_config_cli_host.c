#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "xtos/bci.h"
#include "xtos/bci_uapi.h"
#include "bci_config_cli.h"

int __wrap_open(const char *path, int flags, ...)
{
    assert(!strcmp(path, "/mock/bci") && flags == O_RDWR);
    return 42;
}
int __wrap_close(int fd) { assert(fd == 42); return 0; }
int __wrap_ioctl(int fd, unsigned long command, ...)
{
    assert(fd == 42);
    va_list args;
    va_start(args, command);
    void *data = va_arg(args, void *);
    va_end(args);
    if (command == XTOS_BCI_GET_INFO) {
        memset(data, 0, sizeof(struct xtos_bci_info));
        return 0;
    }
    assert(command == XTOS_BCI_GET_CONFIG);
    struct xtos_bci_config_v1 *cfg = data, request = {.version = 1};
    assert(!memcmp(cfg, &request, sizeof(request)));
    if (getenv("CONFIG_CLI_QUERY_ERROR")) { errno = ENODEV; return -1; }
    *cfg = (struct xtos_bci_config_v1){.version = 1, .channels = 8,
        .sample_rate_hz = 250, .enabled_mask = 255, .capabilities = 7,
        .generation = UINT64_C(123456789012345)};
    cfg->registers[0] = 0x3e;
    cfg->registers[1] = 0x96;
    for (unsigned int i = 0; i < 8; i++) {
        cfg->gain[i] = 24; cfg->registers[5 + i] = 0x68;
    }
    strcpy(cfg->firmware, "OpenBCI V3 8-16 channel\r\nFirmware: \"quoted\\version\"\r\n");
    if (getenv("CONFIG_CLI_INVALID")) cfg->gain[0] = 3;
    return 0;
}
int main(int argc, char **argv) { return bci_config_cli_main(argc, argv); }
