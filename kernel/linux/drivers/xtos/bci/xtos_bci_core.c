// SPDX-License-Identifier: GPL-2.0
/*
 * XTOS BCI Core Driver
 * 
 * Core framework for BCI device registration and management
 * Copyright (C) 2026 XTOS Project
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/fs.h>
#include <linux/cdev.h>
#include <linux/device.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/poll.h>
#include <linux/dma-mapping.h>
#include <linux/xtos_bci.h>
#include <uapi/linux/xtos_bci.h>

#define XTOS_BCI_NAME "xtos_bci"
#define XTOS_BCI_MAX_DEVICES 16

/* Global state */
static dev_t xtos_bci_devt;
static struct class *xtos_bci_class;
static DEFINE_MUTEX(device_list_lock);
static LIST_HEAD(device_list);
static int next_dev_id = 0;

/* Character device operations */
static int xtos_bci_open(struct inode *inode, struct file *filp)
{
	struct xtos_bci_dev *dev;
	unsigned long flags;
	int ret;

	dev = container_of(inode->i_cdev, struct xtos_bci_dev, cdev);

	spin_lock_irqsave(&dev->lock, flags);
	if (dev->opened) {
		spin_unlock_irqrestore(&dev->lock, flags);
		return -EBUSY;  /* Only one process can open */
	}
	dev->opened = true;
	spin_unlock_irqrestore(&dev->lock, flags);

	/* Initialize device */
	if (dev->ops->init) {
		ret = dev->ops->init(dev);
		if (ret) {
			dev->opened = false;
			return ret;
		}
	}

	filp->private_data = dev;
	pr_info("xtos_bci: Device %s opened\n", dev->name);

	return 0;
}

static int xtos_bci_release(struct inode *inode, struct file *filp)
{
	struct xtos_bci_dev *dev = filp->private_data;
	unsigned long flags;

	/* Stop streaming if active */
	if (dev->streaming && dev->ops->stop_stream)
		dev->ops->stop_stream(dev);

	/* Cleanup device */
	if (dev->ops->cleanup)
		dev->ops->cleanup(dev);

	spin_lock_irqsave(&dev->lock, flags);
	dev->opened = false;
	spin_unlock_irqrestore(&dev->lock, flags);

	pr_info("xtos_bci: Device %s closed\n", dev->name);

	return 0;
}

static ssize_t xtos_bci_read(struct file *filp, 
                            char __user *buf, 
                            size_t count, 
                            loff_t *ppos)
{
	struct xtos_bci_dev *dev = filp->private_data;
	struct xtos_bci_ringbuf *rb = dev->ringbuf;
	size_t avail, to_copy;
	unsigned long flags;
	int ret;

	if (!rb)
		return -EINVAL;

	/* Wait for data available */
	if (filp->f_flags & O_NONBLOCK) {
		spin_lock_irqsave(&rb->lock, flags);
		avail = rb->write_pos - rb->read_pos;
		spin_unlock_irqrestore(&rb->lock, flags);
		
		if (avail == 0)
			return -EAGAIN;
	} else {
		ret = wait_event_interruptible(rb->wait, 
		                               rb->write_pos > rb->read_pos);
		if (ret)
			return ret;
	}

	spin_lock_irqsave(&rb->lock, flags);

	/* Calculate available data */
	avail = rb->write_pos - rb->read_pos;
	to_copy = min(count, avail);

	if (to_copy > 0) {
		size_t pos = rb->read_pos % rb->size;
		size_t to_end = rb->size - pos;

		spin_unlock_irqrestore(&rb->lock, flags);

		/* Copy to user space */
		if (to_copy <= to_end) {
			if (copy_to_user(buf, rb->buffer + pos, to_copy))
				return -EFAULT;
		} else {
			if (copy_to_user(buf, rb->buffer + pos, to_end))
				return -EFAULT;
			if (copy_to_user(buf + to_end, rb->buffer, to_copy - to_end))
				return -EFAULT;
		}

		spin_lock_irqsave(&rb->lock, flags);
		rb->read_pos += to_copy;
		spin_unlock_irqrestore(&rb->lock, flags);

		return to_copy;
	}

	spin_unlock_irqrestore(&rb->lock, flags);
	return 0;
}

static __poll_t xtos_bci_poll(struct file *filp, poll_table *wait)
{
	struct xtos_bci_dev *dev = filp->private_data;
	struct xtos_bci_ringbuf *rb = dev->ringbuf;
	__poll_t mask = 0;
	unsigned long flags;

	if (!rb)
		return EPOLLERR;

	poll_wait(filp, &rb->wait, wait);

	spin_lock_irqsave(&rb->lock, flags);
	if (rb->write_pos > rb->read_pos)
		mask |= EPOLLIN | EPOLLRDNORM;
	spin_unlock_irqrestore(&rb->lock, flags);

	return mask;
}

static int xtos_bci_mmap(struct file *filp, struct vm_area_struct *vma)
{
	struct xtos_bci_dev *dev = filp->private_data;
	struct xtos_bci_ringbuf *rb = dev->ringbuf;
	unsigned long size = vma->vm_end - vma->vm_start;

	if (!rb)
		return -EINVAL;

	if (size > rb->size)
		return -EINVAL;

	/* Map DMA buffer to user space */
	return dma_mmap_coherent(dev->device, vma, 
	                        rb->buffer, 
	                        rb->dma_handle, 
	                        size);
}

static long xtos_bci_ioctl(struct file *filp, 
                          unsigned int cmd, 
                          unsigned long arg)
{
	struct xtos_bci_dev *dev = filp->private_data;
	void __user *argp = (void __user *)arg;
	int ret = 0;

	switch (cmd) {
	case XTOS_BCI_GET_INFO: {
		struct xtos_bci_info info;
		
		strncpy(info.name, dev->name, sizeof(info.name));
		info.dev_id = dev->dev_id;
		info.num_channels = dev->num_channels;
		info.sampling_rate = dev->sampling_rate;
		info.resolution_bits = dev->resolution_bits;
		info.input_range_mv = dev->input_range_mv;
		info.interface = dev->interface;
		
		if (copy_to_user(argp, &info, sizeof(info)))
			return -EFAULT;
		break;
	}

	case XTOS_BCI_START_STREAM:
		if (!dev->ops->start_stream)
			return -EOPNOTSUPP;
		ret = dev->ops->start_stream(dev);
		break;

	case XTOS_BCI_STOP_STREAM:
		if (!dev->ops->stop_stream)
			return -EOPNOTSUPP;
		ret = dev->ops->stop_stream(dev);
		break;

	case XTOS_BCI_SET_RATE: {
		u32 rate;
		
		if (copy_from_user(&rate, argp, sizeof(rate)))
			return -EFAULT;
		
		if (!dev->ops->set_sampling_rate)
			return -EOPNOTSUPP;
		ret = dev->ops->set_sampling_rate(dev, rate);
		break;
	}

	case XTOS_BCI_SET_CHANNEL: {
		struct xtos_bci_channel_config cfg;
		
		if (copy_from_user(&cfg, argp, sizeof(cfg)))
			return -EFAULT;
		
		if (!dev->ops->set_channel_enable)
			return -EOPNOTSUPP;
		ret = dev->ops->set_channel_enable(dev, cfg.channel, cfg.enabled);
		break;
	}

	case XTOS_BCI_CALIBRATE:
		if (!dev->ops->calibrate)
			return -EOPNOTSUPP;
		ret = dev->ops->calibrate(dev);
		break;

	case XTOS_BCI_CHECK_IMP: {
		__u32 impedances_kohm[32];
		
		if (!dev->ops->impedance_check)
			return -EOPNOTSUPP;
		
		ret = dev->ops->impedance_check(dev, impedances_kohm);
		if (ret)
			return ret;
		
		if (copy_to_user(argp, impedances_kohm, sizeof(impedances_kohm)))
			return -EFAULT;
		break;
	}

	case XTOS_BCI_GET_STATS: {
		struct xtos_bci_stats stats;
		unsigned long flags;
		
		spin_lock_irqsave(&dev->lock, flags);
		stats.samples_read = dev->samples_read;
		stats.samples_dropped = dev->samples_dropped;
		stats.errors = dev->errors;
		stats.overruns = dev->ringbuf ? dev->ringbuf->overruns : 0;
		spin_unlock_irqrestore(&dev->lock, flags);
		
		if (copy_to_user(argp, &stats, sizeof(stats)))
			return -EFAULT;
		break;
	}

	case XTOS_BCI_RESET_STATS: {
		unsigned long flags;
		
		spin_lock_irqsave(&dev->lock, flags);
		dev->samples_read = 0;
		dev->samples_dropped = 0;
		dev->errors = 0;
		if (dev->ringbuf)
			dev->ringbuf->overruns = 0;
		spin_unlock_irqrestore(&dev->lock, flags);
		break;
	}

	default:
		return -ENOTTY;
	}

	return ret;
}

static const struct file_operations xtos_bci_fops = {
	.owner = THIS_MODULE,
	.open = xtos_bci_open,
	.release = xtos_bci_release,
	.read = xtos_bci_read,
	.poll = xtos_bci_poll,
	.mmap = xtos_bci_mmap,
	.unlocked_ioctl = xtos_bci_ioctl,
};

/* Device registration API */
int xtos_bci_register_device(struct xtos_bci_dev *dev)
{
	int ret;

	if (!dev || !dev->ops)
		return -EINVAL;

	mutex_lock(&device_list_lock);

	/* Assign device ID */
	dev->dev_id = next_dev_id++;

	/* Allocate device number */
	dev->devt = MKDEV(MAJOR(xtos_bci_devt), dev->dev_id);

	/* Initialize character device */
	cdev_init(&dev->cdev, &xtos_bci_fops);
	dev->cdev.owner = THIS_MODULE;

	ret = cdev_add(&dev->cdev, dev->devt, 1);
	if (ret) {
		pr_err("xtos_bci: Failed to add cdev: %d\n", ret);
		goto err_cdev;
	}

	/* Create device node */
	dev->device = device_create(xtos_bci_class, NULL, dev->devt, dev,
	                           "xtos%d", dev->dev_id);
	if (IS_ERR(dev->device)) {
		ret = PTR_ERR(dev->device);
		pr_err("xtos_bci: Failed to create device: %d\n", ret);
		goto err_device;
	}

	/* Initialize state */
	spin_lock_init(&dev->lock);
	dev->opened = false;
	dev->streaming = false;
	dev->samples_read = 0;
	dev->samples_dropped = 0;
	dev->errors = 0;

	/* Add to device list */
	list_add_tail(&dev->list, &device_list);

	mutex_unlock(&device_list_lock);

	pr_info("xtos_bci: Registered device %s (ID %d) as /dev/xtos%d\n",
	        dev->name, dev->dev_id, dev->dev_id);

	return 0;

err_device:
	cdev_del(&dev->cdev);
err_cdev:
	mutex_unlock(&device_list_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(xtos_bci_register_device);

void xtos_bci_unregister_device(struct xtos_bci_dev *dev)
{
	if (!dev)
		return;

	mutex_lock(&device_list_lock);

	/* Remove from device list */
	list_del(&dev->list);

	/* Destroy device node */
	device_destroy(xtos_bci_class, dev->devt);

	/* Remove character device */
	cdev_del(&dev->cdev);

	mutex_unlock(&device_list_lock);

	pr_info("xtos_bci: Unregistered device %s (ID %d)\n",
	        dev->name, dev->dev_id);
}
EXPORT_SYMBOL_GPL(xtos_bci_unregister_device);

/* Module initialization */
static int __init xtos_bci_init(void)
{
	int ret;

	/* Allocate character device region */
	ret = alloc_chrdev_region(&xtos_bci_devt, 0, XTOS_BCI_MAX_DEVICES,
	                         XTOS_BCI_NAME);
	if (ret) {
		pr_err("xtos_bci: Failed to allocate chrdev region: %d\n", ret);
		return ret;
	}

	/* Create device class */
	xtos_bci_class = class_create(XTOS_BCI_NAME);
	if (IS_ERR(xtos_bci_class)) {
		ret = PTR_ERR(xtos_bci_class);
		pr_err("xtos_bci: Failed to create class: %d\n", ret);
		goto err_class;
	}

	pr_info("xtos_bci: Core driver initialized (major %d)\n",
	        MAJOR(xtos_bci_devt));

	return 0;

err_class:
	unregister_chrdev_region(xtos_bci_devt, XTOS_BCI_MAX_DEVICES);
	return ret;
}

static void __exit xtos_bci_exit(void)
{
	class_destroy(xtos_bci_class);
	unregister_chrdev_region(xtos_bci_devt, XTOS_BCI_MAX_DEVICES);

	pr_info("xtos_bci: Core driver unloaded\n");
}

module_init(xtos_bci_init);
module_exit(xtos_bci_exit);

MODULE_LICENSE("GPL v2");
MODULE_AUTHOR("XTOS Project");
MODULE_DESCRIPTION("XTOS BCI Core Driver Framework");
MODULE_VERSION("1.0");
