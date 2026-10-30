/* SPDX-License-Identifier: GPL-2.0 */
#ifndef XTOS_BCI_FRAME_SNAPSHOT_H
#define XTOS_BCI_FRAME_SNAPSHOT_H
#include "xtos_bci_frame.h"
#ifdef XTOS_BCI_FRAME_SNAPSHOT_HOST_TEST
#include "../../../../../tests/bci_frame_snapshot_host/snapshot_shim.h"
#else
#include <linux/refcount.h>
#include <linux/types.h>
#endif
struct vm_area_struct;
struct xtos_bci_frame_snapshot {
    refcount_t refs;
    void *mapping;
    size_t mapping_bytes;
    struct xtos_bci_frame_request descriptor;
    bool consumed;
};
/* Caller retains ring lifetime and serializes capture/consume using op_lock.
 * Do not wait for a blocked reader's read_lock during snapshot acquisition.
 * RX publication remains independent under the frame ring lock.
 */
struct xtos_bci_frame_snapshot *xtos_bci_frame_snapshot_create(
    struct xtos_bci_frame_ring *ring, u32 capacity, u64 token);
void xtos_bci_frame_snapshot_get(struct xtos_bci_frame_snapshot *s);
void xtos_bci_frame_snapshot_put(struct xtos_bci_frame_snapshot *s);
int xtos_bci_frame_snapshot_consume(struct xtos_bci_frame_snapshot *s,
    struct xtos_bci_frame_ring *ring, const struct xtos_bci_frame_consume *request);
int xtos_bci_frame_snapshot_mmap(struct xtos_bci_frame_snapshot *s,
    struct vm_area_struct *vma);
#endif
