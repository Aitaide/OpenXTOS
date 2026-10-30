/* SPDX-License-Identifier: GPL-2.0 */
#define main lifetime_main
#include "../bci_lifetime_host/lifetime_test.c"
#undef main
#include "../../kernel/linux/include/uapi/linux/xtos_bci_config.h"

static unsigned int config_calls;
static unsigned int channel_calls;
static int fixture_channel(struct xtos_bci_dev *dev, int channel,
                           bool enabled, int gain)
{
    (void)dev;
    assert(channel == 3 && enabled && gain == 12);
    channel_calls++;
    return 0;
}
static int config_result;
static int fixture_config(struct xtos_bci_dev *dev,
                          struct xtos_bci_config_v1 *config)
{
    (void)dev;
    config_calls++;
    if (config_result)
        return config_result;
    config->version = 1;
    config->channels = 8;
    config->sample_rate_hz = 250;
    config->enabled_mask = 255;
    config->capabilities = XTOS_BCI_CONFIG_READBACK | XTOS_BCI_FIRMWARE_READBACK;
    config->generation = 9;
    for (unsigned int i = 0; i < 8; i++) {
        config->gain[i] = 24;
        config->registers[5 + i] = 0x68;
    }
    config->registers[0] = 0x3e;
    config->registers[1] = 0x96;
    strcpy(config->firmware, "OpenBCI V3 8-16 channel\r\n");
    return 0;
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    assert(!xtos_bci_init());
    struct fixture f = {0};
    struct xtos_bci_dev *dev = setup(&f, true);
    if (!strcmp(argv[1], "config_unsupported")) {
        struct xtos_bci_device_ops ops = *dev->ops;
        ops.get_config = NULL;
        struct xtos_bci_config_v1 config = {.version = 1};
        dev->ops = &ops;
        assert(xtos_bci_ioctl(&f.file, XTOS_BCI_GET_CONFIG,
                              (unsigned long)&config) == -EOPNOTSUPP);
        dev->ops = &openbci_ops;
    } else if (!strcmp(argv[1], "config_readback")) {
        struct xtos_bci_device_ops ops = *dev->ops;
        ops.get_config = NULL;
        struct xtos_bci_config_v1 config = {.version = 1};
        ops.get_config = fixture_config;
        dev->ops = &ops;
        assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_GET_CONFIG,
                              (unsigned long)&config));
        assert(config_calls == 1 && config.generation == 9);
        assert(config.gain[7] == 24 && config.enabled_mask == 255);
        assert(!strcmp(config.firmware, "OpenBCI V3 8-16 channel\r\n"));
        dev->ops = &openbci_ops;
    } else if (!strcmp(argv[1], "config_invalid_request")) {
        struct xtos_bci_device_ops ops = *dev->ops;
        ops.get_config = fixture_config;
        dev->ops = &ops;
        for (unsigned int i = 0; i < sizeof(struct xtos_bci_config_v1); i++) {
            struct xtos_bci_config_v1 config = {.version = 1};
            if (!i)
                config.version = 2;
            else if (i >= sizeof(config.version))
                ((unsigned char *)&config)[i] = 1;
            else
                continue;
            struct xtos_bci_config_v1 original = config;
            assert(xtos_bci_ioctl(&f.file, XTOS_BCI_GET_CONFIG,
                                  (unsigned long)&config) == -EINVAL);
            assert(!config_calls && !memcmp(&config, &original, sizeof(config)));
        }
        dev->ops = &openbci_ops;
    } else if (!strcmp(argv[1], "gain_not_silently_ignored")) {
        struct xtos_bci_device_ops ops = *dev->ops;
        ops.set_channel_config = NULL;
        dev->ops = &ops;
        struct xtos_bci_channel_config config = {
            .channel = 0, .enabled = 1, .gain = 12
        };
        assert(xtos_bci_ioctl(&f.file, XTOS_BCI_SET_CHANNEL,
                              (unsigned long)&config) == -EOPNOTSUPP);
        dev->ops = &openbci_ops;
    } else if (!strcmp(argv[1], "channel_atomic_callback")) {
        struct xtos_bci_device_ops ops = *dev->ops;
        struct xtos_bci_channel_config config = {
            .channel = 3, .enabled = 1, .gain = 12
        };
        ops.set_channel_config = fixture_channel;
        dev->ops = &ops;
        assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_SET_CHANNEL,
                              (unsigned long)&config));
        assert(channel_calls == 1);
        dev->ops = &openbci_ops;
    } else if (!strcmp(argv[1], "acquisition_fault_drains_then_errors")) {
        unsigned char bytes[32] = {0}, out[32];
        assert(xtos_bci_ringbuf_write(dev->ringbuf, bytes, sizeof(bytes)) == 32);
        WRITE_ONCE(dev->acquisition_error, -ERANGE);
        WRITE_ONCE(dev->streaming, false);
        __poll_t events = xtos_bci_poll(&f.file, NULL);
        assert(events & EPOLLERR);
        assert(events & EPOLLIN);
        assert(xtos_bci_read(&f.file, (char *)out, sizeof(out), NULL) == 32);
        assert(xtos_bci_read(&f.file, (char *)out, sizeof(out), NULL) == -ERANGE);
    } else if (!strcmp(argv[1], "invalid_channel_request")) {
        struct xtos_bci_device_ops ops = *dev->ops;
        ops.set_channel_config = fixture_channel;
        dev->ops = &ops;
        for (unsigned int i = 0; i < 5; i++) {
            struct xtos_bci_channel_config config = {
                .channel = 3, .enabled = 1, .gain = 12
            };
            if (i == 0) config.enabled = 2;
            if (i == 1) config.channel = 8;
            if (i == 2) config.channel = UINT32_MAX;
            if (i == 3) config.gain = 3;
            if (i == 4) config.gain = UINT32_MAX;
            assert(xtos_bci_ioctl(&f.file, XTOS_BCI_SET_CHANNEL,
                                  (unsigned long)&config) == -EINVAL);
            assert(!channel_calls);
        }
        dev->ops = &openbci_ops;
    } else {
        abort();
    }
    teardown(&f);
    assert(dma_allocs == dma_frees);
    assert(!atomic_load(&driver_module.refs));
    xtos_bci_exit();
    puts(argv[1]);
    return 0;
}
