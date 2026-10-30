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
#include <linux/compat.h>
#include <linux/xtos_bci.h>
#include <uapi/linux/xtos_bci.h>
#include "xtos_bci_snapshot.h"
#include "xtos_bci_frame.h"
#include "xtos_bci_frame_snapshot.h"

#define XTOS_BCI_NAME "xtos_bci"
#define XTOS_BCI_MAX_DEVICES 16

/* Global state */
static dev_t xtos_bci_devt;
static struct class *xtos_bci_class;
static DEFINE_MUTEX(device_list_lock);
static LIST_HEAD(device_list);
static bool device_ids[XTOS_BCI_MAX_DEVICES];

/* op_lock protects snapshot ownership and the never-reused token sequence. */
struct xtos_bci_file {
	struct xtos_bci_dev *dev;
	struct xtos_bci_snapshot *snapshot;
	struct xtos_bci_frame_snapshot *frame_snapshot;
	u64 next_token;
};

static bool xtos_bci_has_snapshot(struct xtos_bci_file *ctx)
{
	return READ_ONCE(ctx->snapshot) || READ_ONCE(ctx->frame_snapshot);
}

static void xtos_bci_wake_readers(struct xtos_bci_dev *dev)
{
	if (dev->ringbuf)
		wake_up_interruptible(&dev->ringbuf->wait);
	xtos_bci_device_frames_wake(dev);
}

/* Validate the numeric address before conversion, including compat upper bits. */
static int xtos_bci_frame_read_input(struct xtos_bci_frame_request *input)
{
	struct xtos_bci_frame_request expected = {0};
	u64 bytes;

	if (input->version != XTOS_BCI_FRAME_VERSION ||
	    input->flags & ~XTOS_BCI_FRAME_NONBLOCK || !input->capacity_frames ||
	    input->capacity_frames > XTOS_BCI_FRAME_MAX_BATCH || !input->frames_ptr)
		return -EINVAL;
	expected.version = input->version;
	expected.flags = input->flags;
	expected.capacity_frames = input->capacity_frames;
	expected.frames_ptr = input->frames_ptr;
	if (memcmp(input, &expected, sizeof(expected)))
		return -EINVAL;
	bytes = (u64)input->capacity_frames * XTOS_BCI_FRAME_RECORD_BYTES;
	if (input->frames_ptr > (u64)ULONG_MAX - bytes ||
	    (in_compat_syscall() && input->frames_ptr > 0xffffffffULL - bytes))
		return -EOVERFLOW;
	if (!access_ok((void __user *)(unsigned long)input->frames_ptr, bytes))
		return -EFAULT;
	return 0;
}

static long xtos_bci_frame_read_ioctl(struct file *filp, unsigned long arg)
{
	struct xtos_bci_file *ctx = filp->private_data;
	struct xtos_bci_dev *dev = ctx->dev;
	struct xtos_bci_frame_ring *ring = READ_ONCE(dev->frames);
	struct xtos_bci_frame_request input, output;
	struct xtos_bci_frame_v1 *records;
	int ret;

	if (READ_ONCE(dev->disconnected))
		return -ENODEV;
	if (!ring)
		return -EOPNOTSUPP;
	if (copy_from_user(&input, (void __user *)arg, sizeof(input)))
		return -EFAULT;
	ret = xtos_bci_frame_read_input(&input);
	if (ret)
		return ret;
	ret = mutex_lock_interruptible(&dev->read_lock);
	if (ret)
		return ret;
	if (!(input.flags & XTOS_BCI_FRAME_NONBLOCK) && !(filp->f_flags & O_NONBLOCK)) {
		ret = wait_event_interruptible(ring->wait,
			READ_ONCE(dev->disconnected) || READ_ONCE(dev->acquisition_error) ||
			xtos_bci_has_snapshot(ctx) || !READ_ONCE(dev->streaming) ||
			xtos_bci_frame_ring_available(ring));
		if (ret)
			goto unlock_read;
	}
	mutex_lock(&dev->op_lock);
	if (READ_ONCE(dev->disconnected)) {
		ret = -ENODEV;
		goto unlock_op;
	}
	if (xtos_bci_has_snapshot(ctx)) {
		ret = -EBUSY;
		goto unlock_op;
	}
	if (!xtos_bci_frame_ring_available(ring)) {
		ret = READ_ONCE(dev->acquisition_error);
		if (ret)
			goto unlock_op;
		if (READ_ONCE(dev->streaming)) {
			ret = -EAGAIN;
			goto unlock_op;
		}
		ret = copy_to_user((void __user *)arg, &input, sizeof(input)) ? -EFAULT : 0;
		goto unlock_op;
	}
	records = kmalloc((size_t)input.capacity_frames * sizeof(*records), GFP_KERNEL);
	if (!records) {
		ret = -ENOMEM;
		goto unlock_op;
	}
	ret = xtos_bci_frame_ring_peek(ring, input.capacity_frames, records, &output);
	if (!ret) {
		output.flags = input.flags;
		output.frames_ptr = input.frames_ptr;
		if (copy_to_user((void __user *)(unsigned long)input.frames_ptr,
				 records, output.payload_bytes) ||
		    copy_to_user((void __user *)arg, &output, sizeof(output)))
			ret = -EFAULT;
		else
			ret = xtos_bci_frame_ring_commit(ring, output.cursor, output.frame_count);
	}
	kfree(records);
unlock_op:
	mutex_unlock(&dev->op_lock);
unlock_read:
	mutex_unlock(&dev->read_lock);
	return ret;
}

void xtos_bci_device_init(struct xtos_bci_dev *dev)
{
	kref_init(&dev->ref);
	mutex_init(&dev->op_lock);
	mutex_init(&dev->read_lock);
	spin_lock_init(&dev->lock);
	INIT_LIST_HEAD(&dev->list);
	dev->dev_id = -1;
	dev->disconnected = false;
	dev->registered = false;
	dev->opened = false;
	dev->streaming = false;
	dev->acquisition_error = 0;
}
EXPORT_SYMBOL_GPL(xtos_bci_device_init);

int xtos_bci_device_frames_enable(struct xtos_bci_dev *dev, unsigned int capacity)
{
	struct xtos_bci_frame_ring *ring;

	if (!dev || !capacity || capacity > XTOS_BCI_FRAME_RING_MAX)
		return -EINVAL;
	if (READ_ONCE(dev->disconnected))
		return -ENODEV;
	if (dev->frames)
		return dev->frames->capacity == capacity ? 0 : -EINVAL;
	ring = xtos_bci_frame_ring_alloc(capacity);
	if (IS_ERR(ring))
		return PTR_ERR(ring);
	WRITE_ONCE(dev->frames, ring);
	return 0;
}
EXPORT_SYMBOL_GPL(xtos_bci_device_frames_enable);

int xtos_bci_device_frame_publish(struct xtos_bci_dev *dev,
				const struct xtos_bci_frame_v1 *frame,
				const struct xtos_bci_config_v1 *config)
{
	struct xtos_bci_frame_ring *ring;

	if (!dev || READ_ONCE(dev->disconnected))
		return -ENODEV;
	ring = READ_ONCE(dev->frames);
	if (!ring)
		return -EOPNOTSUPP;
	return xtos_bci_frame_ring_publish(ring, frame, config);
}
EXPORT_SYMBOL_GPL(xtos_bci_device_frame_publish);

void xtos_bci_device_frames_wake(struct xtos_bci_dev *dev)
{
	if (dev)
		xtos_bci_frame_ring_wake(READ_ONCE(dev->frames));
}
EXPORT_SYMBOL_GPL(xtos_bci_device_frames_wake);

void xtos_bci_get_device(struct xtos_bci_dev *dev)
{
	kref_get(&dev->ref);
}
EXPORT_SYMBOL_GPL(xtos_bci_get_device);

static void xtos_bci_destroy(struct kref *ref)
{
	struct xtos_bci_dev *dev = container_of(ref, struct xtos_bci_dev, ref);

	xtos_bci_ringbuf_free(NULL, dev->ringbuf);
	xtos_bci_frame_ring_free(dev->frames);
	if (dev->release)
		dev->release(dev);
}

void xtos_bci_put_device(struct xtos_bci_dev *dev)
{
	kref_put(&dev->ref, xtos_bci_destroy);
}
EXPORT_SYMBOL_GPL(xtos_bci_put_device);

/* Character device operations */
static int xtos_bci_open(struct inode *inode, struct file *filp)
{
	struct xtos_bci_dev *dev, *found = NULL;
	struct module *owner;
	struct xtos_bci_file *ctx;
	int ret;

	/* Compare cdev identity without dereferencing a retired inode's pointer. */
	mutex_lock(&device_list_lock);
	list_for_each_entry(dev, &device_list, list) {
		if (dev->devt == inode->i_rdev && dev->cdev == inode->i_cdev &&
		    !dev->disconnected) {
			if (try_module_get(dev->owner)) {
				xtos_bci_get_device(dev);
				found = dev;
			}
			break;
		}
	}
	mutex_unlock(&device_list_lock);
	if (!found)
		return -ENODEV;
	dev = found;
	owner = dev->owner;
	ctx = kzalloc(sizeof(*ctx), GFP_KERNEL);
	if (!ctx) {
		xtos_bci_put_device(dev);
		module_put(owner);
		return -ENOMEM;
	}
	ctx->dev = dev;
	ctx->next_token = 1;
	mutex_lock(&dev->op_lock);
	if (READ_ONCE(dev->disconnected)) {
		ret = -ENODEV;
		goto out;
	}
	if (dev->opened) {
		ret = -EBUSY;
		goto out;
	}
	ret = dev->ops->init ? dev->ops->init(dev) : 0;
	if (!ret) {
		if (dev->ringbuf) {
			unsigned long flags;

			spin_lock_irqsave(&dev->ringbuf->lock, flags);
			dev->ringbuf->read_pos = dev->ringbuf->write_pos;
			spin_unlock_irqrestore(&dev->ringbuf->lock, flags);
		}
		dev->opened = true;
		xtos_bci_frame_ring_discard_unread(dev->frames);
		filp->private_data = ctx;
	}
out:
	mutex_unlock(&dev->op_lock);
	if (ret) {
		kfree(ctx);
		xtos_bci_put_device(dev);
		module_put(owner);
	}
	return ret;
}

static int xtos_bci_release(struct inode *inode, struct file *filp)
{
	struct xtos_bci_file *ctx = filp->private_data;
	struct xtos_bci_dev *dev = ctx->dev;
	struct module *owner = dev->owner;

	mutex_lock(&dev->op_lock);
	xtos_bci_snapshot_put(ctx->snapshot);
	ctx->snapshot = NULL;
	xtos_bci_frame_snapshot_put(ctx->frame_snapshot);
	ctx->frame_snapshot = NULL;
	if (!READ_ONCE(dev->disconnected) && dev->ops->cleanup)
		dev->ops->cleanup(dev);
	WRITE_ONCE(dev->streaming, false);
	xtos_bci_wake_readers(dev);
	dev->opened = false;
	mutex_unlock(&dev->op_lock);
	filp->private_data = NULL;
	kfree(ctx);
	/* Destructor executes in owner code, before releasing the module pin. */
	xtos_bci_put_device(dev);
	module_put(owner);
	return 0;
}

/* Ring storage stays attached until final device put, including after unbind. */
static ssize_t xtos_bci_read(struct file *filp, char __user *buf,
			     size_t count, loff_t *ppos)
{
	struct xtos_bci_file *ctx = filp->private_data;
	struct xtos_bci_dev *dev = ctx->dev;
	struct xtos_bci_ringbuf *rb = dev->ringbuf;
	size_t avail, copied, pos, first;
	unsigned long flags, snapshot;
	void *bounce;
	int ret;

	if (READ_ONCE(dev->disconnected))
		return -ENODEV;
	if (xtos_bci_has_snapshot(ctx))
		return -EBUSY;
	if (!count)
		return 0;
	if (!rb)
		return -ENODEV;
	ret = mutex_lock_interruptible(&dev->read_lock);
	if (ret)
		return ret;
	if (xtos_bci_has_snapshot(ctx)) {
		ret = -EBUSY;
		goto out_read;
	}
	/* Never pin op_lock while sleeping: STOP/unbind must wake this wait. */
	if (!(filp->f_flags & O_NONBLOCK)) {
		ret = wait_event_interruptible(rb->wait,
			READ_ONCE(dev->disconnected) ||
			READ_ONCE(dev->acquisition_error) ||
			xtos_bci_has_snapshot(ctx) ||
			!READ_ONCE(dev->streaming) ||
			xtos_bci_ringbuf_available(rb));
		if (ret)
			goto out_read;
	}
	mutex_lock(&dev->op_lock);
	if (READ_ONCE(dev->disconnected)) {
		ret = -ENODEV;
		goto out_op;
	}
	if (xtos_bci_has_snapshot(ctx)) {
		ret = -EBUSY;
		goto out_op;
	}
	avail = xtos_bci_ringbuf_available(rb);
	if (!avail) {
		ret = READ_ONCE(dev->acquisition_error);
		if (ret)
			goto out_op;
		ret = READ_ONCE(dev->streaming) ? -EAGAIN : 0;
		goto out_op;
	}
	copied = min(min(count, avail), (size_t)PAGE_SIZE);
	bounce = kmalloc(copied, GFP_KERNEL);
	if (!bounce) {
		ret = -ENOMEM;
		goto out_op;
	}
	spin_lock_irqsave(&rb->lock, flags);
	copied = min(copied, (size_t)(rb->write_pos - rb->read_pos));
	snapshot = rb->read_pos;
	pos = snapshot % rb->size;
	first = min(copied, rb->size - pos);
	memcpy(bounce, rb->buffer + pos, first);
	memcpy(bounce + first, rb->buffer, copied - first);
	spin_unlock_irqrestore(&rb->lock, flags);
	/* Faulting copies see stable bytes, never an overwriteable ring pointer. */
	if (copy_to_user(buf, bounce, copied)) {
		ret = -EFAULT; /* This read consumes nothing; producer may still overrun. */
	} else {
		spin_lock_irqsave(&rb->lock, flags);
		/* Producer overwrite may already have advanced read_pos. Never rewind
		 * it, nor consume bytes beyond the snapshot returned to this caller. */
		if (rb->read_pos - snapshot < copied)
			rb->read_pos = snapshot + copied;
		spin_unlock_irqrestore(&rb->lock, flags);
		ret = copied;
	}
	kfree(bounce);
out_op:
	mutex_unlock(&dev->op_lock);
out_read:
	mutex_unlock(&dev->read_lock);
	return ret;
}

static __poll_t xtos_bci_poll(struct file *filp, poll_table *wait)
{
	struct xtos_bci_file *ctx = filp->private_data;
	struct xtos_bci_dev *dev = ctx->dev;
	struct xtos_bci_ringbuf *rb = dev->ringbuf;
	__poll_t mask = 0;
	unsigned long flags;

	if (!rb && !READ_ONCE(dev->frames))
		return EPOLLERR;

	if (rb)
		poll_wait(filp, &rb->wait, wait);
	if (READ_ONCE(dev->frames)) {
		poll_wait(filp, &dev->frames->wait, wait);
		if (xtos_bci_frame_ring_available(dev->frames))
			mask |= EPOLLIN | EPOLLRDNORM;
	}
	if (READ_ONCE(dev->disconnected))
		return EPOLLHUP | EPOLLERR;
	if (READ_ONCE(dev->acquisition_error))
		mask |= EPOLLERR;

	if (rb) {
		spin_lock_irqsave(&rb->lock, flags);
		if (rb->write_pos != rb->read_pos)
			mask |= EPOLLIN | EPOLLRDNORM;
		spin_unlock_irqrestore(&rb->lock, flags);
	}
	if (!READ_ONCE(dev->streaming))
		mask |= EPOLLHUP;

	return mask;
}

static int xtos_bci_mmap(struct file *filp, struct vm_area_struct *vma)
{
	struct xtos_bci_file *ctx = filp->private_data;
	struct xtos_bci_dev *dev = ctx->dev;
	int ret;

	mutex_lock(&dev->op_lock);
	if (READ_ONCE(dev->disconnected))
		ret = -ENODEV;
	else if (ctx->frame_snapshot)
		ret = xtos_bci_frame_snapshot_mmap(ctx->frame_snapshot, vma);
	else if (!ctx->snapshot)
		ret = -EOPNOTSUPP;
	else
		ret = xtos_bci_snapshot_mmap(ctx->snapshot, vma);
	mutex_unlock(&dev->op_lock);
	return ret;
}

static long xtos_bci_ioctl_locked(struct file *filp,
                          unsigned int cmd, 
                          unsigned long arg)
{
	struct xtos_bci_file *ctx = filp->private_data;
	struct xtos_bci_dev *dev = ctx->dev;
	void __user *argp = (void __user *)arg;
	int ret = 0;

	switch (cmd) {
	case XTOS_BCI_FRAME_SNAPSHOT_RELEASE: {
		struct xtos_bci_frame_release input;
		struct xtos_bci_frame_snapshot *s = ctx->frame_snapshot;

		if (!dev->frames)
			return -EOPNOTSUPP;
		if (copy_from_user(&input, argp, sizeof(input)))
			return -EFAULT;
		if (input.version != XTOS_BCI_FRAME_VERSION || input.flags ||
		    input.reserved || !input.token)
			return -EINVAL;
		if (!s || input.token != s->descriptor.token)
			return -ESTALE;
		WRITE_ONCE(ctx->frame_snapshot, NULL);
		xtos_bci_frame_snapshot_put(s);
		xtos_bci_wake_readers(dev);
		break;
	}
	case XTOS_BCI_FRAME_SNAPSHOT_CONSUME: {
		struct xtos_bci_frame_consume input;

		if (!dev->frames)
			return -EOPNOTSUPP;
		if (copy_from_user(&input, argp, sizeof(input)))
			return -EFAULT;
		if (input.version != XTOS_BCI_FRAME_VERSION || input.flags ||
		    input.reserved || !input.token || !input.frame_count ||
		    input.frame_count > XTOS_BCI_FRAME_MAX_BATCH)
			return -EINVAL;
		if (!ctx->frame_snapshot)
			return -ESTALE;
		return xtos_bci_frame_snapshot_consume(ctx->frame_snapshot, dev->frames, &input);
	}
	case XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE: {
		struct xtos_bci_frame_request input, expected = {0};
		struct xtos_bci_frame_snapshot *s;
		struct xtos_bci_frame_ring *ring = READ_ONCE(dev->frames);

		if (!ring)
			return -EOPNOTSUPP;
		if (ctx->snapshot || ctx->frame_snapshot)
			return -EBUSY;
		if (copy_from_user(&input, argp, sizeof(input)))
			return -EFAULT;
		if (input.version != XTOS_BCI_FRAME_VERSION || !input.capacity_frames ||
		    input.capacity_frames > XTOS_BCI_FRAME_MAX_BATCH)
			return -EINVAL;
		expected.version = input.version;
		expected.capacity_frames = input.capacity_frames;
		if (memcmp(&input, &expected, sizeof(input)))
			return -EINVAL;
		if (!ctx->next_token)
			return -EOVERFLOW;
		if (!xtos_bci_frame_ring_available(ring)) {
			ret = READ_ONCE(dev->acquisition_error);
			return ret ? ret : -EAGAIN;
		}
		s = xtos_bci_frame_snapshot_create(ring, input.capacity_frames, ctx->next_token);
		if (IS_ERR(s))
			return PTR_ERR(s);
		if (copy_to_user(argp, &s->descriptor, sizeof(s->descriptor))) {
			xtos_bci_frame_snapshot_put(s);
			return -EFAULT;
		}
		ctx->next_token++;
		WRITE_ONCE(ctx->frame_snapshot, s);
		xtos_bci_wake_readers(dev);
		break;
	}
	case XTOS_BCI_GET_CONFIG: {
		struct xtos_bci_config_v1 input, output = { 0 };

		if (!dev->ops->get_config)
			return -EOPNOTSUPP;
		if (copy_from_user(&input, argp, sizeof(input)))
			return -EFAULT;
		if (input.version != XTOS_BCI_CONFIG_VERSION)
			return -EINVAL;
		input.version = 0;
		if (memcmp(&input, &output, sizeof(input)))
			return -EINVAL;
		ret = dev->ops->get_config(dev, &output);
		if (ret)
			return ret;
		if (copy_to_user(argp, &output, sizeof(output)))
			return -EFAULT;
		return 0;
	}
	case XTOS_BCI_SNAPSHOT_ACQUIRE: {
		struct xtos_bci_snapshot_request input, output = { 0 };
		struct xtos_bci_snapshot *s;

		if (xtos_bci_has_snapshot(ctx))
			return -EBUSY;
		if (copy_from_user(&input, argp, sizeof(input)))
			return -EFAULT;
		if (input.version != XTOS_BCI_SNAPSHOT_VERSION || input.flags ||
		    input.reserved[0] || input.reserved[1] ||
		    !input.requested_bytes || input.requested_bytes % sizeof(u32) ||
		    input.requested_bytes > XTOS_BCI_SNAPSHOT_MAX_PAYLOAD)
			return -EINVAL;
		if (!ctx->next_token)
			return -EOVERFLOW;
		s = xtos_bci_snapshot_create(dev->ringbuf, dev->num_channels,
				dev->sampling_rate, input.requested_bytes, ctx->next_token);
		if (IS_ERR(s))
			return PTR_ERR(s);
		output.version = XTOS_BCI_SNAPSHOT_VERSION;
		output.requested_bytes = input.requested_bytes;
		output.mapping_bytes = s->mapping_bytes;
		output.payload_bytes = s->payload_bytes;
		output.cursor = s->cursor;
		output.token = s->token;
		output.channels = dev->num_channels;
		output.sample_rate_hz = dev->sampling_rate;
		if (copy_to_user(argp, &output, sizeof(output))) {
			xtos_bci_snapshot_put(s);
			return -EFAULT;
		}
		ctx->next_token++;
		WRITE_ONCE(ctx->snapshot, s);
		xtos_bci_wake_readers(dev);
		break;
	}
	case XTOS_BCI_SNAPSHOT_CONSUME: {
		struct xtos_bci_snapshot_consume input;

		if (copy_from_user(&input, argp, sizeof(input)))
			return -EFAULT;
		if (!ctx->snapshot)
			return -ESTALE;
		return xtos_bci_snapshot_consume(ctx->snapshot, dev->ringbuf, &input);
	}
	case XTOS_BCI_SNAPSHOT_RELEASE: {
		struct xtos_bci_snapshot_release input;
		struct xtos_bci_snapshot *s = ctx->snapshot;

		if (copy_from_user(&input, argp, sizeof(input)))
			return -EFAULT;
		if (input.version != XTOS_BCI_SNAPSHOT_VERSION || input.flags ||
		    input.reserved || !input.token)
			return -EINVAL;
		if (!s)
			return -ESTALE;
		if (input.token != s->token)
			return -EINVAL;
		WRITE_ONCE(ctx->snapshot, NULL);
		xtos_bci_snapshot_put(s);
		xtos_bci_wake_readers(dev);
		break;
	}
	case XTOS_BCI_GET_INFO: {
		struct xtos_bci_info info = { 0 };
		
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
		if (dev->num_channels <= 0 ||
		    cfg.channel >= (u32)dev->num_channels || cfg.enabled > 1)
			return -EINVAL;
		switch (cfg.gain) {
		case 0: /* Preserve gain for legacy enable-only requests. */
		case 1: case 2: case 4: case 6: case 8: case 12: case 24:
			break;
		default:
			return -EINVAL;
		}
		if (dev->ops->set_channel_config) {
			ret = dev->ops->set_channel_config(dev, cfg.channel,
						cfg.enabled, cfg.gain);
			break;
		}
		if (!dev->ops->set_channel_enable)
			return -EOPNOTSUPP;
		/* A nonzero gain must not be silently ignored by legacy drivers. */
		if (cfg.gain)
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
		__u32 impedances_kohm[32] = { 0 };
		
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
		spin_unlock_irqrestore(&dev->lock, flags);
		stats.overruns = 0;
		if (dev->ringbuf) {
			spin_lock_irqsave(&dev->ringbuf->lock, flags);
			stats.overruns = dev->ringbuf->overruns;
			spin_unlock_irqrestore(&dev->ringbuf->lock, flags);
		}
		
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
		spin_unlock_irqrestore(&dev->lock, flags);
		if (dev->ringbuf) {
			spin_lock_irqsave(&dev->ringbuf->lock, flags);
			dev->ringbuf->overruns = 0;
			spin_unlock_irqrestore(&dev->ringbuf->lock, flags);
		}
		break;
	}

	default:
		return -ENOTTY;
	}

	return ret;
}

static long xtos_bci_ioctl(struct file *filp, unsigned int cmd, unsigned long arg)
{
	struct xtos_bci_file *ctx = filp->private_data;
	struct xtos_bci_dev *dev = ctx->dev;
	long ret;

	if (cmd == XTOS_BCI_FRAME_READ)
		return xtos_bci_frame_read_ioctl(filp, arg);
	mutex_lock(&dev->op_lock);
	ret = READ_ONCE(dev->disconnected) && cmd != XTOS_BCI_FRAME_SNAPSHOT_RELEASE ? -ENODEV :
		xtos_bci_ioctl_locked(filp, cmd, arg);
	if (cmd == XTOS_BCI_STOP_STREAM)
		xtos_bci_wake_readers(dev);
	mutex_unlock(&dev->op_lock);
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
#ifdef CONFIG_COMPAT
	/* Fixed-width, explicitly 64-bit-aligned UAPI; no embedded pointers. */
	.compat_ioctl = compat_ptr_ioctl,
#endif
};

/* Device registration API */
int xtos_bci_register_device(struct xtos_bci_dev *dev)
{
	int id, ret;

	if (!dev || !dev->ops || !dev->release)
		return -EINVAL;
	mutex_lock(&device_list_lock);
	if (dev->registered || dev->disconnected) {
		ret = -EINVAL;
		goto out;
	}
	for (id = 0; id < XTOS_BCI_MAX_DEVICES; id++)
		if (!device_ids[id])
			break;
	if (id == XTOS_BCI_MAX_DEVICES) {
		ret = -ENOSPC;
		goto out;
	}
	dev->dev_id = id;
	dev->devt = MKDEV(MAJOR(xtos_bci_devt), id);
	/* VFS references to a deleted cdev must not retain embedded descriptor
	 * memory. cdev_alloc owns its own kobject release, independently. */
	dev->cdev = cdev_alloc();
	if (!dev->cdev) {
		ret = -ENOMEM;
		goto out;
	}
	dev->cdev->ops = &xtos_bci_fops;
	dev->cdev->owner = THIS_MODULE;
	ret = cdev_add(dev->cdev, dev->devt, 1);
	if (ret) {
		kobject_put(&dev->cdev->kobj);
		goto out;
	}
	dev->device = device_create(xtos_bci_class, NULL, dev->devt, dev,
				    "xtos%d", id);
	if (IS_ERR(dev->device)) {
		ret = PTR_ERR(dev->device);
		cdev_del(dev->cdev);
		goto out;
	}
	device_ids[id] = true;
	xtos_bci_get_device(dev);
	dev->registered = true;
	list_add_tail(&dev->list, &device_list);
	ret = 0;
out:
	mutex_unlock(&device_list_lock);
	return ret;
}
EXPORT_SYMBOL_GPL(xtos_bci_register_device);

void xtos_bci_unregister_device(struct xtos_bci_dev *dev)
{
	if (!dev)
		return;
	mutex_lock(&device_list_lock);
	if (!dev->registered) {
		mutex_unlock(&device_list_lock);
		return;
	}
	WRITE_ONCE(dev->disconnected, true);
	dev->registered = false;
	list_del_init(&dev->list);
	device_destroy(xtos_bci_class, dev->devt);
	cdev_del(dev->cdev);
	device_ids[dev->dev_id] = false;
	mutex_unlock(&device_list_lock);
	if (dev->ops && dev->ops->cancel)
		dev->ops->cancel(dev);
	/* read never sleeps under op_lock; take it before inspecting a ring that
	 * a concurrent first open may still be publishing. */
	mutex_lock(&dev->op_lock);
	/* Cleanup must quiesce receive before returning. Transport remains open
	 * until the caller returns from unregister; release(fd) cannot call it. */
	if (dev->ops->cleanup)
		dev->ops->cleanup(dev);
	WRITE_ONCE(dev->streaming, false);
	xtos_bci_wake_readers(dev);
	mutex_unlock(&dev->op_lock);
	xtos_bci_put_device(dev);
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
