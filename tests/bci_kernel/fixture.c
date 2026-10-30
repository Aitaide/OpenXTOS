// SPDX-License-Identifier: GPL-2.0
/* Synthetic guest-only BCI device; never connects physical hardware. */
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/dma-mapping.h>
#include <linux/slab.h>
#include <linux/xtos_bci.h>
#include <linux/xtos_bci_config.h>

static struct platform_device *platform;
static struct xtos_bci_dev *fixture;
static int unplugged;
static int test_init(struct xtos_bci_dev *dev)
{
    if (!dev->ringbuf)
        dev->ringbuf=xtos_bci_ringbuf_alloc(&platform->dev,16384);
    return dev->ringbuf?0:-ENOMEM;
}
static int test_start(struct xtos_bci_dev *dev)
{
    s32 samples[16];
    int i;
    for(i=0;i<16;i++) samples[i]=i+1;
    WRITE_ONCE(dev->streaming,true);
    return xtos_bci_ringbuf_write(dev->ringbuf,samples,sizeof(samples))<0?-EIO:0;
}
static int test_stop(struct xtos_bci_dev *dev)
{
    WRITE_ONCE(dev->streaming,false);
    if(dev->ringbuf) wake_up_interruptible(&dev->ringbuf->wait);
    return 0;
}
static void test_cleanup(struct xtos_bci_dev *dev) { test_stop(dev); }
static void test_release(struct xtos_bci_dev *dev) { kfree(dev); }
static int test_get_config(struct xtos_bci_dev *dev, struct xtos_bci_config_v1 *config)
{
    unsigned int i;
    (void)dev;
    *config=(struct xtos_bci_config_v1){.version=XTOS_BCI_CONFIG_VERSION,
        .channels=8,.sample_rate_hz=250,.enabled_mask=255,.generation=1,
        .capabilities=XTOS_BCI_CONFIG_READBACK|XTOS_BCI_FIRMWARE_READBACK};
    config->registers[0]=0x3e;config->registers[1]=0x96;
    for(i=0;i<8;i++) {config->gain[i]=24;config->registers[5+i]=0x68;}
    strscpy(config->firmware,"Synthetic fixture; not physical firmware",sizeof(config->firmware));
    return 0;
}
static const struct xtos_bci_device_ops ops={.init=test_init,.start_stream=test_start,.stop_stream=test_stop,.cleanup=test_cleanup,.get_config=test_get_config};
static void detach(void)
{
    if(fixture) { xtos_bci_unregister_device(fixture);xtos_bci_put_device(fixture);fixture=NULL; }
}
static int set_unplug(const char *text,const struct kernel_param *param)
{
    int ret=param_set_int(text,param);
    if(!ret && unplugged) detach();
    return ret;
}
static const struct kernel_param_ops unplug_ops={.set=set_unplug,.get=param_get_int};
module_param_cb(unplug,&unplug_ops,&unplugged,0600);
static int __init test_module_init(void)
{
    int ret;
    platform=platform_device_register_simple("xtos-bci-fixture",-1,NULL,0);
    if(IS_ERR(platform)) return PTR_ERR(platform);
    ret=dma_coerce_mask_and_coherent(&platform->dev,DMA_BIT_MASK(32));
    if(ret) goto platform_error;
    fixture=kzalloc(sizeof(*fixture),GFP_KERNEL);
    if(!fixture) { ret=-ENOMEM;goto platform_error; }
    xtos_bci_device_init(fixture);
    fixture->ops=&ops;fixture->owner=THIS_MODULE;fixture->release=test_release;
    strscpy(fixture->name,"Synthetic BCI test",sizeof(fixture->name));
    fixture->num_channels=8;fixture->sampling_rate=250;fixture->resolution_bits=24;fixture->input_range_mv=4500;
    ret=xtos_bci_register_device(fixture);
    if(!ret) return 0;
    xtos_bci_put_device(fixture);fixture=NULL;
platform_error:
    platform_device_unregister(platform);return ret;
}
static void __exit test_module_exit(void) { detach();platform_device_unregister(platform); }
module_init(test_module_init);
module_exit(test_module_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Synthetic BCI lifetime/snapshot guest acceptance fixture");
