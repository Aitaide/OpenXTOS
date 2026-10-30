#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include "xtos/bci.h"
#include "xtos/bci_config_uapi.h"
#include "xtos/bci_uapi.h"

_Static_assert(sizeof(struct xtos_bci_config_v1) == 384, "config size");
_Static_assert(offsetof(struct xtos_bci_config_v1, generation) == 32, "generation offset");
_Static_assert(offsetof(struct xtos_bci_config_v1, gain) == 40, "gain offset");
_Static_assert(offsetof(struct xtos_bci_config_v1, registers) == 72, "register offset");
_Static_assert(offsetof(struct xtos_bci_config_v1, firmware) == 96, "firmware offset");
_Static_assert(offsetof(struct xtos_bci_config_v1, reserved) == 352, "reserved offset");
_Static_assert(_IOC_NR(XTOS_BCI_GET_CONFIG) == 16, "config ioctl number");
_Static_assert(_IOC_SIZE(XTOS_BCI_GET_CONFIG) == 384, "config ioctl size");

static struct xtos_bci_config_v1 response;
static int ioctl_error;
static unsigned int mutation, calls, cases;
static const uint32_t gains[] = {1, 2, 4, 6, 8, 12, 24};

int __wrap_open(const char *path, int flags, ...)
{
    assert(strcmp(path, "/mock/bci") == 0 && flags == O_RDWR);
    return 42;
}

int __wrap_close(int fd)
{
    assert(fd == 42);
    return 0;
}

ssize_t __wrap_read(int fd, void *buffer, size_t size)
{
    (void)fd; (void)buffer; (void)size;
    errno = EIO;
    return -1;
}

int __wrap_ioctl(int fd, unsigned long request, ...)
{
    assert(fd == 42);
    va_list ap;
    va_start(ap, request);
    void *arg = va_arg(ap, void *);
    va_end(ap);
    if (request == XTOS_BCI_GET_INFO) {
        struct xtos_bci_info *info = arg;
        memset(info, 0, sizeof(*info));
        return 0;
    }
    assert(request == XTOS_BCI_GET_CONFIG);
    struct xtos_bci_config_v1 *config = arg;
    struct xtos_bci_config_v1 expected = {0};
    expected.version = XTOS_BCI_CONFIG_VERSION;
    assert(memcmp(config, &expected, sizeof(expected)) == 0);
    calls++;
    if (ioctl_error) {
        config->generation = 1234;
        errno = ioctl_error;
        return -1;
    }
    *config = response;
    switch (mutation) {
    case 1: config->version++; break;
    case 2: config->flags = 1; break;
    case 3: config->channels = 7; break;
    case 4: config->sample_rate_hz = 200; break;
    case 5: config->enabled_mask = 256; break;
    case 6: config->capabilities |= 32; break;
    case 7: config->capabilities &= ~XTOS_BCI_CONFIG_READBACK; break;
    case 8: config->capabilities &= ~XTOS_BCI_FIRMWARE_READBACK; break;
    case 9: config->reserved0[0] = 1; break;
    case 10: config->reserved0[1] = 1; break;
    case 11: config->generation = 0; break;
    case 12: config->gain[3] = 3; break;
    case 13: config->registers[8] ^= 0x10; break;
    case 14: config->registers[9] ^= 0x80; break;
    case 15: config->registers[1] = (config->registers[1] & ~7u) | 5u; break;
    case 16: memset(config->firmware, 'x', sizeof(config->firmware)); break;
    case 17: config->firmware[0] = '\0'; break;
    case 18: config->firmware[0] = (char)0x80; break;
    case 19: config->reserved[0] = 1; break;
    case 20: config->reserved[7] = 1; break;
    case 21: config->enabled_mask &= ~1u; break;
    case 22: config->registers[5] = 0x70; break;
    }
    return 0;
}

static void valid_response(void)
{
    memset(&response, 0, sizeof(response));
    response.version = XTOS_BCI_CONFIG_VERSION;
    response.channels = 8;
    response.sample_rate_hz = 250;
    response.enabled_mask = 0xff;
    response.capabilities = XTOS_BCI_CONFIG_READBACK | XTOS_BCI_FIRMWARE_READBACK;
    response.generation = 9;
    response.registers[1] = 0x96;
    for (unsigned int channel = 0; channel < 8; channel++) {
        unsigned int gain_index = channel % 7;
        response.gain[channel] = gains[gain_index];
        response.registers[5 + channel] = (uint8_t)(gain_index << 4);
    }
    memcpy(response.firmware, "OpenBCI-Cyton\r\nrev1", sizeof("OpenBCI-Cyton\r\nrev1"));
}

static xtos_bci_session_t *create(void)
{
    xtos_bci_session_t *session = xtos_bci_session_create("/mock/bci");
    assert(session);
    return session;
}

static void success(void)
{
    valid_response(); mutation = 0; ioctl_error = 0;
    xtos_bci_session_t *session = create();
    struct xtos_bci_config_v1 out;
    memset(&out, 0xa5, sizeof(out));
    unsigned int before = calls;
    assert(xtos_bci_get_config(session, &out) == 0);
    assert(calls == before + 1);
    assert(memcmp(&out, &response, sizeof(out)) == 0);
    xtos_bci_session_destroy(session); cases++;
}

static void all_channels_gain24(void)
{
    valid_response(); mutation = 0; ioctl_error = 0;
    response.capabilities = 31;
    response.enabled_mask = 0x7f;
    for (unsigned int channel = 0; channel < 8; channel++) {
        response.gain[channel] = 24;
        response.registers[5 + channel] = 0x60;
    }
    response.registers[12] |= 0x80;
    xtos_bci_session_t *session = create();
    struct xtos_bci_config_v1 out;
    memset(&out, 0, sizeof(out));
    assert(xtos_bci_get_config(session, &out) == 0);
    assert(out.enabled_mask == 0x7f && out.capabilities == 31);
    for (unsigned int channel = 0; channel < 8; channel++)
        assert(out.gain[channel] == 24);
    xtos_bci_session_destroy(session); cases++;
}

static void invalid_response(unsigned int which)
{
    valid_response(); mutation = which; ioctl_error = 0;
    xtos_bci_session_t *session = create();
    struct xtos_bci_config_v1 out, original;
    memset(&out, 0xa5, sizeof(out)); original = out;
    assert(xtos_bci_get_config(session, &out) == -1 && errno == EPROTO);
    assert(memcmp(&out, &original, sizeof(out)) == 0);
    xtos_bci_session_destroy(session); cases++;
}

static void syscall_error_and_arguments(void)
{
    valid_response(); mutation = 0;
    xtos_bci_session_t *session = create();
    struct xtos_bci_config_v1 out, original;
    memset(&out, 0x5a, sizeof(out)); original = out;
    ioctl_error = ENODEV;
    assert(xtos_bci_get_config(session, &out) == -1 && errno == ENODEV);
    assert(memcmp(&out, &original, sizeof(out)) == 0);
    ioctl_error = 0;
    assert(xtos_bci_get_config(NULL, &out) == -1 && errno == EINVAL);
    assert(memcmp(&out, &original, sizeof(out)) == 0);
    assert(xtos_bci_get_config(session, NULL) == -1 && errno == EINVAL);
    assert(memcmp(&out, &original, sizeof(out)) == 0);
    xtos_bci_session_destroy(session); cases++;
}

int main(void)
{
    success(); all_channels_gain24();
    for (mutation = 1; mutation <= 22; mutation++)
        invalid_response(mutation);
    syscall_error_and_arguments();
    printf("config_sdk_cases=%u pass\n", cases);
    return 0;
}
