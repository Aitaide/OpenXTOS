/* SPDX-License-Identifier: GPL-2.0 */
#include "xtos_bci_frame.h"
#ifndef XTOS_BCI_FRAME_HOST_TEST
#include <linux/err.h>
#include <linux/limits.h>
#include <linux/sched.h>
#include <linux/slab.h>
#include <linux/string.h>
#endif
struct xtos_bci_frame_ring *xtos_bci_frame_ring_alloc(u32 capacity)
{
    struct xtos_bci_frame_ring *ring;
    if (!capacity || capacity > XTOS_BCI_FRAME_RING_MAX)
        return ERR_PTR(-EINVAL);
    ring = kcalloc(1, sizeof(*ring), GFP_KERNEL);
    if (!ring)
        return ERR_PTR(-ENOMEM);
    ring->slots = kcalloc(capacity, sizeof(*ring->slots), GFP_KERNEL);
    if (!ring->slots) {
        kfree(ring);
        return ERR_PTR(-ENOMEM);
    }
    ring->capacity = capacity;
    spin_lock_init(&ring->lock);
    init_waitqueue_head(&ring->wait);
    return ring;
}
void xtos_bci_frame_ring_free(struct xtos_bci_frame_ring *ring)
{
    if (!ring)
        return;
#ifdef XTOS_BCI_FRAME_HOST_TEST
    assert(!pthread_mutex_destroy(&ring->lock));
    assert(!pthread_mutex_destroy(&ring->wait.lock));
    assert(!pthread_cond_destroy(&ring->wait.cond));
#endif
    kfree(ring->slots);
    kfree(ring);
}
/* Pure binary validation mirrors SDK config_valid; no ASCII reply parser. */
static bool frame_input_valid(const struct xtos_bci_frame_v1 *f,
    const struct xtos_bci_config_v1 *c)
{
    static const u32 gains[] = {1, 2, 4, 6, 8, 12, 24};
    const char *end;
    u32 i, code;
    if (!f || !c || f->version != 1 || f->record_bytes != 224 ||
        f->channels != 8 || f->sample_rate_hz != 250 || f->unit != 1 ||
        f->host_clock != 1 || (f->quality_flags & ~31u) ||
        !(f->quality_flags & 1u) || (f->source_flags != 1 && f->source_flags != 2) ||
        !f->host_rx_ns || f->sequence || !f->stream_generation ||
        !f->config_generation || f->counter_bits != 8 || f->device_counter > 255 ||
        f->missing_before > 126 || !!(f->quality_flags & 2u) != !!f->missing_before ||
        f->reserved[0] || f->reserved[1])
        return false;
    for (i = 8; i < 32; i++)
        if (f->samples_nv[i])
            return false;
    if (c->version != 1 || c->flags || c->channels != 8 || c->sample_rate_hz != 250 ||
        c->enabled_mask > 255 || f->enabled_mask != c->enabled_mask ||
        !c->generation || c->generation != f->config_generation ||
        (c->capabilities & ~31u) || (c->capabilities & 3u) != 3 ||
        c->reserved0[0] || c->reserved0[1] || (c->registers[1] & 7u) != 6)
        return false;
    for (i = 0; i < 8; i++) {
        if (c->reserved[i])
            return false;
        code = (c->registers[5+i] >> 4) & 7u;
        if (code >= 7 || c->gain[i] != gains[code] ||
            !!(c->registers[5+i] & 0x80u) == !!(c->enabled_mask & (1u << i)))
            return false;
    }
    end = memchr(c->firmware, 0, sizeof(c->firmware));
    if (!end || end == c->firmware)
        return false;
    for (i = 0; c->firmware+i < end; i++) {
        unsigned char ch = (unsigned char)c->firmware[i];
        if (ch != '\r' && ch != '\n' && (ch < 0x20 || ch > 0x7e))
            return false;
    }
    return true;
}
static bool frame_run_equal(const struct xtos_bci_frame_slot *a,
    const struct xtos_bci_frame_slot *b)
{
    return a->record.stream_generation == b->record.stream_generation &&
        a->record.source_flags == b->record.source_flags &&
        a->record.host_clock == b->record.host_clock &&
        a->record.channels == b->record.channels &&
        a->record.sample_rate_hz == b->record.sample_rate_hz &&
        a->record.unit == b->record.unit && !memcmp(&a->config, &b->config, sizeof(a->config));
}
int xtos_bci_frame_ring_peek(struct xtos_bci_frame_ring *ring, u32 capacity,
    struct xtos_bci_frame_v1 *records, struct xtos_bci_frame_request *out)
{
    unsigned long flags;
    u32 count = 0, i;
    const struct xtos_bci_frame_slot *first;
    struct xtos_bci_frame_request result = {0};
    int error = 0;
    if (!ring || !records || !out || !capacity || capacity > XTOS_BCI_FRAME_MAX_BATCH)
        return -EINVAL;
    spin_lock_irqsave(&ring->lock, flags);
    if (ring->read_cursor > ring->write_cursor ||
        ring->write_cursor - ring->read_cursor > ring->capacity) {
        error = -EOVERFLOW;
        goto unlock_peek;
    }
    if (ring->read_cursor == ring->write_cursor) {
        error = -EAGAIN;
        goto unlock_peek;
    }
    first = &ring->slots[ring->read_cursor % ring->capacity];
    while (count < capacity && count < ring->write_cursor - ring->read_cursor &&
        frame_run_equal(first, &ring->slots[(ring->read_cursor + count) % ring->capacity]))
        count++;
    result.version = 1;
    result.capacity_frames = capacity;
    result.frame_count = count;
    result.cursor = ring->read_cursor;
    result.next_cursor = ring->read_cursor + count;
    result.overwritten_frames = ring->overwritten_frames;
    result.stream_generation = first->record.stream_generation;
    result.record_bytes = XTOS_BCI_FRAME_RECORD_BYTES;
    result.header_bytes = XTOS_BCI_FRAME_HEADER_BYTES;
    result.payload_bytes = (u64)count * XTOS_BCI_FRAME_RECORD_BYTES;
    result.config = first->config;
    for (i = 0; i < count; i++)
        records[i] = ring->slots[(ring->read_cursor + i) % ring->capacity].record;
    *out = result;
unlock_peek:
    spin_unlock_irqrestore(&ring->lock, flags);
    return error;
}
int xtos_bci_frame_ring_commit(struct xtos_bci_frame_ring *ring, u64 cursor, u32 count)
{
    unsigned long flags;
    u32 i;
    int error = 0;
    const struct xtos_bci_frame_slot *first;
    if (!ring || !count || count > XTOS_BCI_FRAME_MAX_BATCH)
        return -EINVAL;
    spin_lock_irqsave(&ring->lock, flags);
    if (ring->read_cursor > ring->write_cursor ||
        ring->write_cursor - ring->read_cursor > ring->capacity ||
        cursor != ring->read_cursor || cursor > U64_MAX - count) {
        error = -EOVERFLOW;
        goto unlock_commit;
    }
    if (count > ring->write_cursor - cursor) {
        error = -EINVAL;
        goto unlock_commit;
    }
    first = &ring->slots[cursor % ring->capacity];
    for (i = 1; i < count; i++)
        if (!frame_run_equal(first, &ring->slots[(cursor+i) % ring->capacity])) {
            error = -EINVAL;
            goto unlock_commit;
        }
    ring->read_cursor = cursor + count;
unlock_commit:
    spin_unlock_irqrestore(&ring->lock, flags);
    return error;
}
void xtos_bci_frame_ring_discard_unread(struct xtos_bci_frame_ring *ring)
{
    unsigned long flags;
    if (!ring)
        return;
    spin_lock_irqsave(&ring->lock, flags);
    ring->read_cursor = ring->write_cursor;
    spin_unlock_irqrestore(&ring->lock, flags);
}
bool xtos_bci_frame_ring_available(struct xtos_bci_frame_ring *ring)
{
    unsigned long flags;
    bool available;
    if (!ring)
        return false;
    spin_lock_irqsave(&ring->lock, flags);
    available = ring->read_cursor < ring->write_cursor;
    spin_unlock_irqrestore(&ring->lock, flags);
    return available;
}
void xtos_bci_frame_ring_wake(struct xtos_bci_frame_ring *ring)
{
    if (ring)
        wake_up_interruptible(&ring->wait);
}
static bool frame_ledger_valid(const struct xtos_bci_frame_ring *r)
{
    return r->read_cursor <= r->write_cursor &&
        r->write_cursor - r->read_cursor <= r->capacity;
}
int xtos_bci_frame_ring_publish(struct xtos_bci_frame_ring *ring,
    const struct xtos_bci_frame_v1 *record, const struct xtos_bci_config_v1 *config)
{
    unsigned long flags;
    struct xtos_bci_frame_slot *slot;
    int error = 0;
    if (!ring || !frame_input_valid(record, config))
        return -EINVAL;
    spin_lock_irqsave(&ring->lock, flags);
    if (!frame_ledger_valid(ring) || ring->write_cursor == U64_MAX ||
        (ring->write_cursor - ring->read_cursor == ring->capacity &&
         ring->overwritten_frames == U64_MAX)) {
        error = -EOVERFLOW;
        goto unlock;
    }
    if (record->stream_generation < ring->last_stream_generation ||
        config->generation < ring->last_config.generation ||
        (config->generation == ring->last_config.generation &&
         memcmp(config, &ring->last_config, sizeof(*config))) ||
        (record->stream_generation == ring->last_stream_generation &&
         record->host_rx_ns < ring->last_host_rx_ns)) {
        error = -EINVAL;
        goto unlock;
    }
    slot = &ring->slots[ring->write_cursor % ring->capacity];
    slot->record = *record;
    slot->record.sequence = ring->write_cursor + 1;
    slot->config = *config;
    if (ring->write_cursor - ring->read_cursor == ring->capacity) {
        ring->read_cursor++;
        ring->overwritten_frames++;
    }
    ring->last_stream_generation = record->stream_generation;
    ring->last_host_rx_ns = record->host_rx_ns;
    ring->last_config = *config;
    ring->write_cursor++;
unlock:
    spin_unlock_irqrestore(&ring->lock, flags);
    if (!error)
        wake_up_interruptible(&ring->wait);
    return error;
}
