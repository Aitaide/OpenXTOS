/* SPDX-License-Identifier: GPL-2.0 */
#ifndef XTOS_BCI_FRAME_RING_H
#define XTOS_BCI_FRAME_RING_H
#ifdef XTOS_BCI_FRAME_HOST_TEST
#include "../../../../../tests/bci_frame_host/frame_shim.h"
#else
#include <linux/spinlock.h>
#include <linux/wait.h>
#include <uapi/linux/xtos_bci_frame.h>
#endif
#define XTOS_BCI_FRAME_RING_MAX 4096u
struct xtos_bci_frame_slot {
    struct xtos_bci_frame_v1 record;
    struct xtos_bci_config_v1 config;
};
/* Caller owns lifetime. All mutable state except capacity/slots/wait is locked. */
struct xtos_bci_frame_ring {
    spinlock_t lock;
    wait_queue_head_t wait;
    struct xtos_bci_frame_slot *slots;
    u32 capacity;
    u64 read_cursor, write_cursor, overwritten_frames;
    u64 last_stream_generation, last_host_rx_ns;
    struct xtos_bci_config_v1 last_config;
};
struct xtos_bci_frame_ring *xtos_bci_frame_ring_alloc(u32 capacity);
void xtos_bci_frame_ring_free(struct xtos_bci_frame_ring *ring);
int xtos_bci_frame_ring_publish(struct xtos_bci_frame_ring *ring,
    const struct xtos_bci_frame_v1 *record, const struct xtos_bci_config_v1 *config);
/* Output-only descriptor; caller stages user fields later. Invalid/empty leaves
 * descriptor AND record buffer unchanged. At most 292 records; no allocation. */
int xtos_bci_frame_ring_peek(struct xtos_bci_frame_ring *ring, u32 capacity,
    struct xtos_bci_frame_v1 *records, struct xtos_bci_frame_request *out);
/* Exact current cursor, positive count <=292 within ONE homogeneous run.
 * Core serializes consumers and retains the peek descriptor across usercopy. */
int xtos_bci_frame_ring_commit(struct xtos_bci_frame_ring *ring, u64 cursor, u32 count);
void xtos_bci_frame_ring_discard_unread(struct xtos_bci_frame_ring *ring);
bool xtos_bci_frame_ring_available(struct xtos_bci_frame_ring *ring);
void xtos_bci_frame_ring_wake(struct xtos_bci_frame_ring *ring);
#endif
