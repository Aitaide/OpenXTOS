// SPDX-License-Identifier: GPL-2.0
/* Guest-only CPU synthetic producer. No transport or physical device access.
 * Build externally as bci_frame_fixture.o (copy this file under that name).
 * All producer callbacks are serialized by the core's device op_lock.
 */
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/dma-mapping.h>
#include <linux/ktime.h>
#include <linux/limits.h>
#include <linux/sched.h>
#include <linux/slab.h>
#include <linux/xtos_bci.h>
#include <uapi/linux/xtos_bci_frame.h>

#define FRAME_BURST 32u
struct frame_device {
    struct xtos_bci_dev dev;
    struct xtos_bci_config_v1 config;
    u64 stream_generation;
};
static struct platform_device *platform;
static struct frame_device *fixture;
static DEFINE_MUTEX(detach_lock);
static int unplugged;

static int frame_init(struct xtos_bci_dev *dev)
{
    int ret;

    if (!dev->ringbuf)
        dev->ringbuf = xtos_bci_ringbuf_alloc(&platform->dev, 16384);
    if (!dev->ringbuf)
        return -ENOMEM;
    ret = xtos_bci_device_frames_enable(dev, 500);
    return ret;
}

static int frame_stop(struct xtos_bci_dev *dev)
{
    WRITE_ONCE(dev->streaming, false);
    if (dev->ringbuf)
        wake_up_interruptible(&dev->ringbuf->wait);
    xtos_bci_device_frames_wake(dev);
    return 0;
}

static int frame_start(struct xtos_bci_dev *dev)
{
    struct frame_device *f = container_of(dev, struct frame_device, dev);
    unsigned int i, ch;
    int ret;

    if (READ_ONCE(dev->streaming))
        return 0;
    if (f->stream_generation == U64_MAX || f->config.generation == U64_MAX)
        return -EOVERFLOW;
    /* Synthetic verified commit; unchanged values still get a new identity. */
    f->stream_generation++;
    f->config.generation++;
    WRITE_ONCE(dev->acquisition_error, 0);
    WRITE_ONCE(dev->streaming, true);
    for (i = 0; i < FRAME_BURST; i++) {
        struct xtos_bci_frame_v1 record = {
            .version = XTOS_BCI_FRAME_VERSION,
            .record_bytes = XTOS_BCI_FRAME_RECORD_BYTES,
            .channels = 8, .sample_rate_hz = 250,
            .unit = XTOS_BCI_UNIT_NANOVOLT,
            .host_clock = XTOS_BCI_FRAME_CLOCK_MONOTONIC,
            .quality_flags = XTOS_BCI_FRAME_QUALITY_CONFIG_VERIFIED,
            .source_flags = XTOS_BCI_FRAME_SOURCE_SYNTHETIC,
            .host_rx_ns = ktime_get_ns(),
            .stream_generation = f->stream_generation,
            .config_generation = f->config.generation,
            .device_counter = i % 256, .counter_bits = 8,
            .enabled_mask = 255,
        };
        for (ch = 0; ch < 8; ch++) {
            s32 value = (i + 1) * 1000 + ch;

            record.samples_nv[ch] = ch & 1 ? -value : value;
        }
        /* Independent views receive exactly the same eight signed samples. */
        ret = xtos_bci_device_frame_publish(dev, &record, &f->config);
        if (ret)
            goto fault;
        ret = xtos_bci_ringbuf_write(dev->ringbuf, record.samples_nv,
                                    8 * sizeof(record.samples_nv[0]));
        if (ret != (int)(8 * sizeof(record.samples_nv[0]))) {
            ret = ret < 0 ? ret : -EIO;
            goto fault;
        }
    }
    return 0;
fault:
    WRITE_ONCE(dev->acquisition_error, ret);
    frame_stop(dev);
    return ret;
}

static int frame_get_config(struct xtos_bci_dev *dev,
                            struct xtos_bci_config_v1 *out)
{
    struct frame_device *f = container_of(dev, struct frame_device, dev);

    *out = f->config;
    return 0;
}
static void frame_cleanup(struct xtos_bci_dev *dev) { frame_stop(dev); }
static void frame_release(struct xtos_bci_dev *dev)
{
    kfree(container_of(dev, struct frame_device, dev));
}
static const struct xtos_bci_device_ops frame_ops = {
    .init = frame_init, .cleanup = frame_cleanup,
    .start_stream = frame_start, .stop_stream = frame_stop,
    .get_config = frame_get_config,
};

static void detach(void)
{
    mutex_lock(&detach_lock);
    if (fixture) {
        struct frame_device *f = fixture;

        fixture = NULL;
        xtos_bci_unregister_device(&f->dev);
        /* Drop transport-owner reference; open files and VMAs may still live. */
        xtos_bci_put_device(&f->dev);
    }
    mutex_unlock(&detach_lock);
}
static int set_unplug(const char *text, const struct kernel_param *param)
{
    int value, ret = kstrtoint(text, 0, &value);

    if (ret || (value != 0 && value != 1))
        return ret ? ret : -EINVAL;
    ret = param_set_int(text, param);
    if (!ret && value)
        detach();
    return ret;
}
static const struct kernel_param_ops unplug_ops = {
    .set = set_unplug, .get = param_get_int,
};
module_param_cb(unplug, &unplug_ops, &unplugged, 0600);

static int __init frame_module_init(void)
{
    unsigned int ch;
    int ret;

    if (unplugged)
        return -EINVAL;
    platform = platform_device_register_simple("xtos-bci-frame-fixture", -1, NULL, 0);
    if (IS_ERR(platform))
        return PTR_ERR(platform);
    ret = dma_coerce_mask_and_coherent(&platform->dev, DMA_BIT_MASK(32));
    if (ret)
        goto platform_error;
    fixture = kzalloc(sizeof(*fixture), GFP_KERNEL);
    if (!fixture) {
        ret = -ENOMEM;
        goto platform_error;
    }
    xtos_bci_device_init(&fixture->dev);
    fixture->dev.ops = &frame_ops;
    fixture->dev.owner = THIS_MODULE;
    fixture->dev.release = frame_release;
    fixture->dev.num_channels = 8;
    fixture->dev.sampling_rate = 250;
    fixture->dev.resolution_bits = 24;
    fixture->dev.input_range_mv = 4500;
    strscpy(fixture->dev.name, "Synthetic frame fixture", sizeof(fixture->dev.name));
    fixture->config = (struct xtos_bci_config_v1) {
        .version = XTOS_BCI_CONFIG_VERSION, .channels = 8,
        .sample_rate_hz = 250, .enabled_mask = 255, .generation = 1,
        .capabilities = XTOS_BCI_CONFIG_READBACK | XTOS_BCI_FIRMWARE_READBACK,
    };
    fixture->config.registers[0] = 0x3e;
    fixture->config.registers[1] = 0x96;
    for (ch = 0; ch < 8; ch++) {
        fixture->config.gain[ch] = 24;
        fixture->config.registers[5 + ch] = 0x68;
    }
    strscpy(fixture->config.firmware, "Synthetic 8 channels",
            sizeof(fixture->config.firmware));
    /* Both allocations are sleepable and complete before cdev publication. */
    ret = frame_init(&fixture->dev);
    if (!ret)
        ret = xtos_bci_register_device(&fixture->dev);
    if (!ret)
        return 0;
    xtos_bci_put_device(&fixture->dev);
    fixture = NULL;
platform_error:
    platform_device_unregister(platform);
    return ret;
}
static void __exit frame_module_exit(void)
{
    detach();
    platform_device_unregister(platform);
}
module_init(frame_module_init);
module_exit(frame_module_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("CPU synthetic BCI frame Linux guest acceptance fixture");
