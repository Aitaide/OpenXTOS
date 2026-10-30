// SPDX-License-Identifier: GPL-2.0
#include "xtos_bci_snapshot.h"
#ifndef XTOS_BCI_SNAPSHOT_HOST_TEST
#include <linux/err.h>
#include <linux/mm.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/vmalloc.h>
#include <linux/xtos_bci.h>
#endif

struct xtos_bci_snapshot *
xtos_bci_snapshot_create(struct xtos_bci_ringbuf *rb, u32 channels,
			 u32 rate, u64 requested, u64 token)
{
	struct xtos_bci_snapshot_header *header;
	struct xtos_bci_snapshot *s;
	unsigned long flags;
	size_t frame, capacity, available, payload, pos, first;
	int error;

	if (!rb || !rb->buffer || !rb->size || !channels || channels > 32 ||
	    !rate || !token || !requested || requested % sizeof(u32) ||
	    requested > XTOS_BCI_SNAPSHOT_MAX_PAYLOAD)
		return ERR_PTR(-EINVAL);
	frame = channels * sizeof(u32);
	capacity = (size_t)requested / frame * frame;
	if (!capacity)
		return ERR_PTR(-EAGAIN);
	spin_lock_irqsave(&rb->lock, flags);
	available = rb->write_pos - rb->read_pos;
	spin_unlock_irqrestore(&rb->lock, flags);
	if (available > rb->size)
		return ERR_PTR(-EOVERFLOW);
	payload = min(capacity, available) / frame * frame;
	if (!payload)
		return ERR_PTR(-EAGAIN);
	s = kzalloc(sizeof(*s), GFP_KERNEL);
	if (!s)
		return ERR_PTR(-ENOMEM);
	/* Allocation can sleep; the operation mutex stabilizes ring lifetime.
	 * Recheck availability and take the actual source cursor after allocation.
	 */
	s->mapping = vmalloc_user(PAGE_ALIGN(sizeof(*header) + payload));
	if (!s->mapping) {
		kfree(s);
		return ERR_PTR(-ENOMEM);
	}
	spin_lock_irqsave(&rb->lock, flags);
	available = rb->write_pos - rb->read_pos;
	if (available > rb->size) {
		error = -EOVERFLOW;
		goto unlock_error;
	}
	if (available < payload) {
		error = -EAGAIN;
		goto unlock_error;
	}
	s->cursor = rb->read_pos;
	pos = rb->read_pos % rb->size;
	first = min(payload, rb->size - pos);
	header = s->mapping;
	memcpy(header + 1, (char *)rb->buffer + pos, first);
	memcpy((char *)(header + 1) + first, rb->buffer, payload - first);
	spin_unlock_irqrestore(&rb->lock, flags);

	s->mapping_bytes = PAGE_ALIGN(sizeof(*header) + payload);
	s->token = token;
	s->payload_bytes = payload;
	refcount_set(&s->refs, 1);
	header->magic = XTOS_BCI_SNAPSHOT_MAGIC;
	header->version = XTOS_BCI_SNAPSHOT_VERSION;
	header->header_size = sizeof(*header);
	header->unit = XTOS_BCI_UNIT_NANOVOLT;
	header->payload_bytes = payload;
	header->cursor = s->cursor;
	header->token = token;
	header->channels = channels;
	header->sample_rate_hz = rate;
	header->sample_size = sizeof(u32);
	return s;

unlock_error:
	spin_unlock_irqrestore(&rb->lock, flags);
	vfree(s->mapping);
	kfree(s);
	return ERR_PTR(error);
}

int xtos_bci_snapshot_consume(struct xtos_bci_snapshot *s,
			    struct xtos_bci_ringbuf *rb,
			    const struct xtos_bci_snapshot_consume *request)
{
	unsigned long flags;
	int error = 0;

	if (!s || !rb || !request ||
	    request->version != XTOS_BCI_SNAPSHOT_VERSION || request->flags ||
	    request->reserved[0] || request->reserved[1] ||
	    request->token != s->token || request->cursor != s->cursor ||
	    request->bytes != s->payload_bytes)
		return -EINVAL;
	spin_lock_irqsave(&rb->lock, flags);
	if (s->consumed)
		error = -ESTALE;
	else if ((u64)rb->read_pos != s->cursor)
		error = -EOVERFLOW;
	else {
		rb->read_pos += s->payload_bytes;
		s->consumed = true;
	}
	spin_unlock_irqrestore(&rb->lock, flags);
	return error;
}

static void xtos_bci_snapshot_vma_open(struct vm_area_struct *vma)
{
	xtos_bci_snapshot_get(vma->vm_private_data);
}

static void xtos_bci_snapshot_vma_close(struct vm_area_struct *vma)
{
	xtos_bci_snapshot_put(vma->vm_private_data);
}

static const struct vm_operations_struct xtos_bci_snapshot_vm_ops = {
	.open = xtos_bci_snapshot_vma_open,
	.close = xtos_bci_snapshot_vma_close,
};

int xtos_bci_snapshot_mmap(struct xtos_bci_snapshot *s,
			   struct vm_area_struct *vma)
{
	int error;

	if (!s || !vma || vma->vm_pgoff ||
	    vma->vm_end <= vma->vm_start ||
	    vma->vm_end - vma->vm_start != s->mapping_bytes ||
	    (vma->vm_flags & VM_WRITE) || !(vma->vm_flags & VM_SHARED))
		return -EINVAL;
	vm_flags_clear(vma, VM_MAYWRITE);
	error = remap_vmalloc_range(vma, s->mapping, 0);
	if (error)
		return error;
	/* mmap does not call open for the initial VMA. Own that reference here;
	 * fork/split clones are subsequently accounted through vm_ops.open.
	 */
	xtos_bci_snapshot_get(s);
	vma->vm_private_data = s;
	vma->vm_ops = &xtos_bci_snapshot_vm_ops;
	return 0;
}

void xtos_bci_snapshot_get(struct xtos_bci_snapshot *s)
{
	refcount_inc(&s->refs);
}

void xtos_bci_snapshot_put(struct xtos_bci_snapshot *s)
{
	if (s && refcount_dec_and_test(&s->refs)) {
		vfree(s->mapping);
		kfree(s);
	}
}
