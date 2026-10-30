/* SPDX-License-Identifier: GPL-2.0 */
#include "xtos_bci_frame_snapshot.h"
#ifndef XTOS_BCI_FRAME_SNAPSHOT_HOST_TEST
#include <linux/err.h>
#include <linux/mm.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/vmalloc.h>
#endif
static void frame_snapshot_vma_open(struct vm_area_struct *vma)
{
    xtos_bci_frame_snapshot_get(vma->vm_private_data);
}
static void frame_snapshot_vma_close(struct vm_area_struct *vma)
{
    xtos_bci_frame_snapshot_put(vma->vm_private_data);
}
static const struct vm_operations_struct frame_snapshot_vm_ops = {
    .open = frame_snapshot_vma_open,
    .close = frame_snapshot_vma_close,
};
int xtos_bci_frame_snapshot_mmap(struct xtos_bci_frame_snapshot *s,
    struct vm_area_struct *vma)
{
    int error;

    if (!s || !vma || vma->vm_pgoff || vma->vm_end <= vma->vm_start ||
        vma->vm_end - vma->vm_start != s->mapping_bytes ||
        (vma->vm_flags & VM_WRITE) || !(vma->vm_flags & VM_SHARED))
        return -EINVAL;
    vm_flags_clear(vma, VM_MAYWRITE);
    error = remap_vmalloc_range(vma, s->mapping, 0);
    if (error)
        return error;
    /* The initial mmap does not invoke vm_ops.open. */
    xtos_bci_frame_snapshot_get(s);
    vma->vm_private_data = s;
    vma->vm_ops = &frame_snapshot_vm_ops;
    return 0;
}
void xtos_bci_frame_snapshot_get(struct xtos_bci_frame_snapshot *s)
{
    refcount_inc(&s->refs);
}
void xtos_bci_frame_snapshot_put(struct xtos_bci_frame_snapshot *s)
{
    if (s && refcount_dec_and_test(&s->refs)) {
        vfree(s->mapping);
        kfree(s);
    }
}
int xtos_bci_frame_snapshot_consume(struct xtos_bci_frame_snapshot *s,
    struct xtos_bci_frame_ring *ring, const struct xtos_bci_frame_consume *request)
{
    int error;

    if (!s || !ring || !request ||
        request->version != XTOS_BCI_FRAME_VERSION || request->flags ||
        request->reserved || !request->token || !request->frame_count ||
        request->frame_count > XTOS_BCI_FRAME_MAX_BATCH)
        return -EINVAL;
    if (request->token != s->descriptor.token || s->consumed)
        return -ESTALE;
    if (request->cursor != s->descriptor.cursor ||
        request->frame_count != s->descriptor.frame_count ||
        request->stream_generation != s->descriptor.stream_generation)
        return -EINVAL;
    error = xtos_bci_frame_ring_commit(ring, s->descriptor.cursor,
        s->descriptor.frame_count);
    if (!error)
        s->consumed = true;
    return error;
}
struct xtos_bci_frame_snapshot *xtos_bci_frame_snapshot_create(
    struct xtos_bci_frame_ring *ring, u32 capacity, u64 token)
{
    struct xtos_bci_frame_snapshot *s;
    struct xtos_bci_frame_snapshot_header *header;
    struct xtos_bci_frame_v1 *records;
    int error;

    if (!ring || !capacity || capacity > XTOS_BCI_FRAME_MAX_BATCH || !token)
        return ERR_PTR(-EINVAL);
    s = kzalloc(sizeof(*s), GFP_KERNEL);
    if (!s)
        return ERR_PTR(-ENOMEM);
    /* Allocate for the requested capacity before sampling any ring state. */
    s->mapping = vmalloc_user(PAGE_ALIGN(XTOS_BCI_FRAME_RECORDS_OFFSET +
        (size_t)capacity * XTOS_BCI_FRAME_RECORD_BYTES));
    if (!s->mapping) {
        kfree(s);
        return ERR_PTR(-ENOMEM);
    }
    error = xtos_bci_frame_ring_peek(ring, capacity,
        (struct xtos_bci_frame_v1 *)((char *)s->mapping +
            XTOS_BCI_FRAME_RECORDS_OFFSET), &s->descriptor);
    if (error) {
        vfree(s->mapping);
        kfree(s);
        return ERR_PTR(error);
    }
    s->mapping_bytes = PAGE_ALIGN(XTOS_BCI_FRAME_RECORDS_OFFSET +
        (size_t)s->descriptor.payload_bytes);
    s->descriptor.token = token;
    s->descriptor.mapping_bytes = s->mapping_bytes;
    header = s->mapping;
    records = (struct xtos_bci_frame_v1 *)((char *)s->mapping +
        XTOS_BCI_FRAME_RECORDS_OFFSET);
    header->magic = XTOS_BCI_FRAME_SNAPSHOT_MAGIC;
    header->version = s->descriptor.version;
    header->header_bytes = s->descriptor.header_bytes;
    header->record_bytes = s->descriptor.record_bytes;
    header->frame_count = s->descriptor.frame_count;
    header->unit = records[0].unit;
    header->host_clock = records[0].host_clock;
    header->payload_bytes = s->descriptor.payload_bytes;
    header->cursor = s->descriptor.cursor;
    header->next_cursor = s->descriptor.next_cursor;
    header->token = s->descriptor.token;
    header->stream_generation = s->descriptor.stream_generation;
    header->config_generation = s->descriptor.config.generation;
    header->overwritten_frames = s->descriptor.overwritten_frames;
    header->mapping_bytes = s->descriptor.mapping_bytes;
    header->config_bytes = XTOS_BCI_FRAME_CONFIG_BYTES;
    header->records_offset = XTOS_BCI_FRAME_RECORDS_OFFSET;
    memcpy((char *)s->mapping + XTOS_BCI_FRAME_HEADER_BYTES,
        &s->descriptor.config, sizeof(s->descriptor.config));
    refcount_set(&s->refs, 1);
    return s;
}
