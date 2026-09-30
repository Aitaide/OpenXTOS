// SPDX-License-Identifier: GPL-2.0
/*
 * XTOS BCI Ring Buffer Implementation
 * 
 * Zero-copy DMA ring buffer for high-throughput data streaming
 * Copyright (C) 2026 XTOS Project
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/slab.h>
#include <linux/dma-mapping.h>
#include <linux/spinlock.h>
#include <linux/wait.h>
#include <linux/xtos_bci.h>

/**
 * xtos_bci_ringbuf_alloc - Allocate a DMA-backed ring buffer
 * @dev: Device for DMA allocation
 * @size: Buffer size in bytes (should be power of 2)
 *
 * Allocates a physically contiguous DMA buffer for zero-copy access
 * Returns: Pointer to ring buffer structure or NULL on failure
 */
struct xtos_bci_ringbuf *xtos_bci_ringbuf_alloc(struct device *dev, size_t size)
{
	struct xtos_bci_ringbuf *rb;

	if (!dev || size == 0)
		return NULL;

	rb = kzalloc(sizeof(*rb), GFP_KERNEL);
	if (!rb)
		return NULL;

	/* Allocate DMA coherent memory */
	rb->buffer = dma_alloc_coherent(dev, size, &rb->dma_handle, GFP_KERNEL);
	if (!rb->buffer) {
		pr_err("xtos_bci: Failed to allocate DMA buffer (%zu bytes)\n", size);
		kfree(rb);
		return NULL;
	}

	rb->size = size;
	rb->write_pos = 0;
	rb->read_pos = 0;
	rb->overruns = 0;
	rb->max_fill = 0;

	spin_lock_init(&rb->lock);
	init_waitqueue_head(&rb->wait);

	pr_debug("xtos_bci: Allocated ring buffer: %zu bytes (DMA: 0x%llx)\n",
	         size, (unsigned long long)rb->dma_handle);

	return rb;
}
EXPORT_SYMBOL_GPL(xtos_bci_ringbuf_alloc);

/**
 * xtos_bci_ringbuf_free - Free a ring buffer
 * @dev: Device used for DMA allocation
 * @rb: Ring buffer to free
 */
void xtos_bci_ringbuf_free(struct device *dev, struct xtos_bci_ringbuf *rb)
{
	if (!rb)
		return;

	if (rb->buffer)
		dma_free_coherent(dev, rb->size, rb->buffer, rb->dma_handle);

	kfree(rb);

	pr_debug("xtos_bci: Freed ring buffer\n");
}
EXPORT_SYMBOL_GPL(xtos_bci_ringbuf_free);

/**
 * xtos_bci_ringbuf_write - Write data to ring buffer
 * @rb: Ring buffer
 * @data: Data to write
 * @len: Length of data in bytes
 *
 * Writes data to the ring buffer. If buffer is full, oldest data is discarded.
 * This function is safe to call from interrupt context.
 *
 * Returns: Number of bytes written, or negative error code
 */
int xtos_bci_ringbuf_write(struct xtos_bci_ringbuf *rb, 
                           const void *data, 
                           size_t len)
{
	unsigned long flags;
	size_t avail, fill;

	if (!rb || !data || len == 0)
		return -EINVAL;

	if (len > rb->size)
		return -E2BIG;

	spin_lock_irqsave(&rb->lock, flags);

	/* Calculate available space */
	fill = rb->write_pos - rb->read_pos;
	avail = rb->size - fill;

	/* Track maximum fill level */
	if (fill > rb->max_fill)
		rb->max_fill = fill;

	/* If buffer full, advance read pointer (discard oldest data) */
	if (len > avail) {
		rb->read_pos += (len - avail);
		rb->overruns++;
		pr_debug_ratelimited("xtos_bci: Ring buffer overflow, discarding %zu bytes\n",
		                     len - avail);
	}

	/* Write data with wrap-around */
	size_t pos = rb->write_pos % rb->size;
	size_t to_end = rb->size - pos;

	if (len <= to_end) {
		/* Single contiguous write */
		memcpy(rb->buffer + pos, data, len);
	} else {
		/* Split write (wraps around) */
		memcpy(rb->buffer + pos, data, to_end);
		memcpy(rb->buffer, data + to_end, len - to_end);
	}

	rb->write_pos += len;

	spin_unlock_irqrestore(&rb->lock, flags);

	/* Wake up waiting readers */
	wake_up_interruptible(&rb->wait);

	return len;
}
EXPORT_SYMBOL_GPL(xtos_bci_ringbuf_write);

/**
 * xtos_bci_ringbuf_read - Read data from ring buffer
 * @rb: Ring buffer
 * @data: Destination buffer
 * @len: Maximum bytes to read
 *
 * Reads up to len bytes from the ring buffer.
 *
 * Returns: Number of bytes read, or negative error code
 */
int xtos_bci_ringbuf_read(struct xtos_bci_ringbuf *rb, 
                          void *data, 
                          size_t len)
{
	unsigned long flags;
	size_t avail, to_copy;

	if (!rb || !data || len == 0)
		return -EINVAL;

	spin_lock_irqsave(&rb->lock, flags);

	/* Calculate available data */
	avail = rb->write_pos - rb->read_pos;
	to_copy = min(len, avail);

	if (to_copy > 0) {
		size_t pos = rb->read_pos % rb->size;
		size_t to_end = rb->size - pos;

		/* Read data with wrap-around */
		if (to_copy <= to_end) {
			/* Single contiguous read */
			memcpy(data, rb->buffer + pos, to_copy);
		} else {
			/* Split read (wraps around) */
			memcpy(data, rb->buffer + pos, to_end);
			memcpy(data + to_end, rb->buffer, to_copy - to_end);
		}

		rb->read_pos += to_copy;
	}

	spin_unlock_irqrestore(&rb->lock, flags);

	return to_copy;
}
EXPORT_SYMBOL_GPL(xtos_bci_ringbuf_read);

/**
 * xtos_bci_ringbuf_available - Get number of bytes available for reading
 * @rb: Ring buffer
 *
 * Returns: Number of bytes available, or 0 if empty
 */
size_t xtos_bci_ringbuf_available(struct xtos_bci_ringbuf *rb)
{
	unsigned long flags;
	size_t avail;

	if (!rb)
		return 0;

	spin_lock_irqsave(&rb->lock, flags);
	avail = rb->write_pos - rb->read_pos;
	spin_unlock_irqrestore(&rb->lock, flags);

	return avail;
}
EXPORT_SYMBOL_GPL(xtos_bci_ringbuf_available);

MODULE_LICENSE("GPL v2");
MODULE_AUTHOR("XTOS Project");
MODULE_DESCRIPTION("XTOS BCI Ring Buffer Implementation");
