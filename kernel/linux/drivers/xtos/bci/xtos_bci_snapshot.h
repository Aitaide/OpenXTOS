/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _XTOS_BCI_SNAPSHOT_H
#define _XTOS_BCI_SNAPSHOT_H
#ifdef XTOS_BCI_SNAPSHOT_HOST_TEST
#include "snapshot_shim.h"
#else
#include <linux/types.h>
#include <linux/refcount.h>
#include <uapi/linux/xtos_bci_snapshot.h>
#endif

struct xtos_bci_ringbuf;
struct vm_area_struct;

/* Owns only immutable CPU memory; never retains a transport or DMA object. */
struct xtos_bci_snapshot {
	refcount_t refs;
	void *mapping;
	size_t mapping_bytes;
	u64 cursor;
	u64 token;
	u64 payload_bytes;
	bool consumed;
};

/* Caller holds its device operation mutex throughout create/consume and checks
 * connection/session state. Ring contents and read_pos are protected internally.
 * requested_bytes is nonzero, signed32-aligned and <= MAX_PAYLOAD; channels is
 * 1..32, rate and token are nonzero. Payload is rounded down to complete frames.
 * Create returns one owned reference or ERR_PTR; it never consumes the ring.
 */
struct xtos_bci_snapshot *
xtos_bci_snapshot_create(struct xtos_bci_ringbuf *rb, u32 channels,
			 u32 sample_rate_hz, u64 requested_bytes, u64 token);
void xtos_bci_snapshot_get(struct xtos_bci_snapshot *snapshot);
void xtos_bci_snapshot_put(struct xtos_bci_snapshot *snapshot);
int xtos_bci_snapshot_consume(struct xtos_bci_snapshot *snapshot,
			    struct xtos_bci_ringbuf *rb,
			    const struct xtos_bci_snapshot_consume *request);
/* Caller owns a reference until return; success adds the initial VMA reference.
 * Subsequent VMA clones acquire their own reference through vm_ops.open.
 */
int xtos_bci_snapshot_mmap(struct xtos_bci_snapshot *snapshot,
			   struct vm_area_struct *vma);
#endif
