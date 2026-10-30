/* SPDX-License-Identifier: GPL-2.0 */
#include <stdio.h>
#include "snapshot_shim.h"
#include "../../kernel/linux/drivers/xtos/bci/xtos_bci_snapshot.c"
static unsigned char data[131072];
static struct xtos_bci_ringbuf ring = { .buffer = data, .size = sizeof(data), .lock = PTHREAD_MUTEX_INITIALIZER };
static void create_case(void)
{
    struct xtos_bci_snapshot *s;
    struct xtos_bci_snapshot_header *h;
    ring.read_pos = sizeof(data) - 16; ring.write_pos = ring.read_pos + 72;
    for (unsigned int i = 0; i < 72; i++) data[(ring.read_pos + i) % ring.size] = (unsigned char)(i + 1);
    s = xtos_bci_snapshot_create(&ring, 8, 250, 68, 42);
    assert(!IS_ERR(s));
    assert(s->payload_bytes == 64 && s->mapping_bytes == PAGE_SIZE);
    assert(s->cursor == ring.read_pos && s->token == 42 && !s->consumed);
    h = s->mapping;
    assert(sizeof(*h) == 64);
    assert(h->magic == XTOS_BCI_SNAPSHOT_MAGIC && h->version == 1);
    assert(h->header_size == 64 && h->unit == XTOS_BCI_UNIT_NANOVOLT);
    assert(h->channels == 8 && h->sample_rate_hz == 250 && h->sample_size == 4);
    assert(h->payload_bytes == 64 && h->token == 42 && h->cursor == ring.read_pos);
    assert(!h->reserved0 && !h->reserved1);
    for (unsigned int i = 0; i < 64; i++) assert(((unsigned char *)(h + 1))[i] == i + 1);
    for (size_t i = sizeof(*h) + 64; i < s->mapping_bytes; i++) assert(((unsigned char *)h)[i] == 0);
    memset(data, 0xcc, sizeof(data));
    assert(((unsigned char *)(h + 1))[0] == 1);
    xtos_bci_snapshot_get(s); xtos_bci_snapshot_put(s);
    assert(atomic_load(&host_mappings) == 1);
    xtos_bci_snapshot_put(s);
    assert(!atomic_load(&host_mappings) && !atomic_load(&host_objects));
}

static void bounds_case(void)
{
    struct xtos_bci_snapshot *s;
    unsigned long saved;
    ring.read_pos = 17; ring.write_pos = 17 + 40;
    s = xtos_bci_snapshot_create(&ring, 8, 250, 65536, 1);
    assert(!IS_ERR(s) && s->payload_bytes == 32);
    assert(host_allocation_bytes == s->mapping_bytes);
    xtos_bci_snapshot_put(s);
    assert(PTR_ERR(xtos_bci_snapshot_create(NULL, 8, 250, 64, 1)) == -EINVAL);
    assert(PTR_ERR(xtos_bci_snapshot_create(&ring, 0, 250, 64, 1)) == -EINVAL);
    assert(PTR_ERR(xtos_bci_snapshot_create(&ring, 33, 250, 64, 1)) == -EINVAL);
    assert(PTR_ERR(xtos_bci_snapshot_create(&ring, 8, 0, 64, 1)) == -EINVAL);
    assert(PTR_ERR(xtos_bci_snapshot_create(&ring, 8, 250, 64, 0)) == -EINVAL);
    assert(PTR_ERR(xtos_bci_snapshot_create(&ring, 8, 250, 0, 1)) == -EINVAL);
    assert(PTR_ERR(xtos_bci_snapshot_create(&ring, 8, 250, 63, 1)) == -EINVAL);
    assert(PTR_ERR(xtos_bci_snapshot_create(&ring, 8, 250, 65540, 1)) == -EINVAL);
    assert(PTR_ERR(xtos_bci_snapshot_create(&ring, 8, 250, UINT64_MAX, 1)) == -EINVAL);
    assert(PTR_ERR(xtos_bci_snapshot_create(&ring, 8, 250, 4, 1)) == -EAGAIN);
    ring.write_pos = ring.read_pos + 31;
    assert(PTR_ERR(xtos_bci_snapshot_create(&ring, 8, 250, 32, 1)) == -EAGAIN);
    ring.write_pos = ring.read_pos;
    assert(PTR_ERR(xtos_bci_snapshot_create(&ring, 8, 250, 32, 1)) == -EAGAIN);
    ring.write_pos = ring.read_pos + ring.size + 1;
    assert(PTR_ERR(xtos_bci_snapshot_create(&ring, 8, 250, 32, 1)) == -EOVERFLOW);
    ring.read_pos = ULONG_MAX - 31; ring.write_pos = ring.read_pos + 64;
    saved = ring.read_pos;
    s = xtos_bci_snapshot_create(&ring, 8, 250, 64, UINT64_MAX);
    assert(!IS_ERR(s) && s->cursor == saved && ring.read_pos == saved);
    xtos_bci_snapshot_put(s);
    ring.read_pos = 0; ring.write_pos = 65536;
    s = xtos_bci_snapshot_create(&ring, 3, 1, 65536, 1);
    assert(!IS_ERR(s) && s->payload_bytes == 65532);
    assert(s->mapping_bytes == PAGE_ALIGN(64 + 65532));
    xtos_bci_snapshot_put(s);
    for (int fail = 1; fail <= 2; fail++) {
        host_fail_alloc = fail;
        assert(PTR_ERR(xtos_bci_snapshot_create(&ring, 32, 250, 65536, 1)) == -ENOMEM);
        assert(!atomic_load(&host_objects) && !atomic_load(&host_mappings));
    }
    host_fail_alloc = 0;
    s = xtos_bci_snapshot_create(&ring, 32, 250, 65536, 1);
    assert(!IS_ERR(s) && s->payload_bytes == 65536);
    xtos_bci_snapshot_put(s);
}


static struct xtos_bci_snapshot *fixture(void)
{
    struct xtos_bci_snapshot *s;
    ring.read_pos = 96; ring.write_pos = 160;
    s = xtos_bci_snapshot_create(&ring, 8, 250, 64, 42);
    assert(!IS_ERR(s)); return s;
}
static struct xtos_bci_snapshot_consume request(struct xtos_bci_snapshot *s)
{
    return (struct xtos_bci_snapshot_consume){ .version = 1, .token = s->token, .cursor = s->cursor, .bytes = s->payload_bytes };
}
static void consume_case(void)
{
    struct xtos_bci_snapshot *s = fixture();
    struct xtos_bci_snapshot_consume good = request(s), bad;
    assert(xtos_bci_snapshot_consume(s, &ring, &good) == 0);
    assert(s->consumed && ring.read_pos == 160);
    assert(xtos_bci_snapshot_consume(s, &ring, &good) == -ESTALE);
    xtos_bci_snapshot_put(s);
    s = fixture(); good = request(s);
    for (unsigned int i = 0; i < 8; i++) {
        bad = good;
        switch(i) {
        case 0: bad.version++; break;
        case 1: bad.flags = 1; break;
        case 2: bad.token++; break;
        case 3: bad.cursor++; break;
        case 4: bad.bytes -= 4; break;
        case 5: bad.reserved[0] = 1; break;
        case 6: bad.reserved[1] = 1; break;
        case 7: bad.bytes += 4; break;
        }
        assert(xtos_bci_snapshot_consume(s, &ring, &bad) == -EINVAL);
        assert(!s->consumed && ring.read_pos == 96);
    }
    ring.read_pos += 32;
    assert(xtos_bci_snapshot_consume(s, &ring, &good) == -EOVERFLOW);
    assert(!s->consumed && ring.read_pos == 128);
    assert(((struct xtos_bci_snapshot_header *)s->mapping)->cursor == 96);
    xtos_bci_snapshot_put(s);
}
static struct vm_area_struct area(struct xtos_bci_snapshot *s)
{
    return (struct vm_area_struct){ .vm_start = 0x10000, .vm_end = 0x10000 + s->mapping_bytes, .vm_flags = VM_READ | VM_SHARED | VM_MAYWRITE };
}
static void mapping_case(void)
{
    struct xtos_bci_snapshot *s = fixture();
    struct vm_area_struct v = area(s), clone;
    assert(xtos_bci_snapshot_mmap(s, &v) == 0);
    assert(host_last_remap == s->mapping && host_remap_calls == 1);
    assert(!(v.vm_flags & VM_MAYWRITE) && v.vm_private_data == s && v.vm_ops);
    assert(atomic_load(&s->refs.value) == 2);
    clone = v; clone.vm_ops->open(&clone);
    assert(atomic_load(&s->refs.value) == 3);
    xtos_bci_snapshot_put(s); /* session release; memory is independent of ring */
    ring.buffer = NULL;
    assert(((struct xtos_bci_snapshot_header *)((struct xtos_bci_snapshot *)v.vm_private_data)->mapping)->magic == XTOS_BCI_SNAPSHOT_MAGIC);
    v.vm_ops->close(&v);
    assert(atomic_load(&host_mappings) == 1);
    clone.vm_ops->close(&clone);
    assert(!atomic_load(&host_mappings) && !atomic_load(&host_objects));
}
static void map_args_case(void)
{
    struct xtos_bci_snapshot *s = fixture();
    struct vm_area_struct v;
    for (unsigned int i = 0; i < 6; i++) {
        v = area(s);
        switch(i) {
        case 0: v.vm_pgoff = 1; break;
        case 1: v.vm_end -= PAGE_SIZE; break;
        case 2: v.vm_end += PAGE_SIZE; break;
        case 3: v.vm_flags |= VM_WRITE; break;
        case 4: v.vm_flags &= ~VM_SHARED; break;
        case 5: v.vm_end = v.vm_start - 1; break;
        }
        assert(xtos_bci_snapshot_mmap(s, &v) == -EINVAL);
        assert(!v.vm_ops && !v.vm_private_data && host_remap_calls == 0);
        assert(atomic_load(&s->refs.value) == 1);
    }
    v = area(s); host_remap_error = -EFAULT;
    assert(xtos_bci_snapshot_mmap(s, &v) == -EFAULT);
    assert(!v.vm_ops && !v.vm_private_data && atomic_load(&s->refs.value) == 1);
    host_remap_error = 0;
    xtos_bci_snapshot_put(s);
    assert(!atomic_load(&host_mappings) && !atomic_load(&host_objects));
}


static pthread_barrier_t refs_ready;
static void *close_clone(void *arg)
{
    struct vm_area_struct *v = arg;
    struct xtos_bci_snapshot *s = v->vm_private_data;
    int rc = pthread_barrier_wait(&refs_ready);
    assert(!rc || rc == PTHREAD_BARRIER_SERIAL_THREAD);
    for (int i = 0; i < 10000; i++) {
        xtos_bci_snapshot_get(s);
        assert(((struct xtos_bci_snapshot_header *)s->mapping)->token == 42);
        xtos_bci_snapshot_put(s);
    }
    v->vm_ops->close(v);
    return NULL;
}
static void concurrent_refs_case(void)
{
    struct xtos_bci_snapshot *s = fixture();
    struct vm_area_struct initial = area(s), clones[8];
    pthread_t threads[8];
    assert(!pthread_barrier_init(&refs_ready, NULL, 9));
    assert(!xtos_bci_snapshot_mmap(s, &initial));
    for (unsigned int i = 0; i < 8; i++) {
        clones[i] = initial;
        clones[i].vm_ops->open(&clones[i]);
        assert(!pthread_create(&threads[i], NULL, close_clone, &clones[i]));
    }
    /* All clones exist, but none can access memory until the session and
     * initial VMA references have been released. Last clone owns destruction.
     */
    xtos_bci_snapshot_put(s);
    initial.vm_ops->close(&initial);
    assert(atomic_load(&host_mappings) == 1);
    int rc = pthread_barrier_wait(&refs_ready);
    assert(!rc || rc == PTHREAD_BARRIER_SERIAL_THREAD);
    for (unsigned int i = 0; i < 8; i++) assert(!pthread_join(threads[i], NULL));
    assert(!pthread_barrier_destroy(&refs_ready));
    assert(!atomic_load(&host_mappings) && !atomic_load(&host_objects));
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    if (!strcmp(argv[1], "create")) create_case();
    else if (!strcmp(argv[1], "bounds")) bounds_case();
    else if (!strcmp(argv[1], "consume")) consume_case();
    else if (!strcmp(argv[1], "mapping")) mapping_case();
    else if (!strcmp(argv[1], "map_args")) map_args_case();
    else if (!strcmp(argv[1], "concurrent_refs")) concurrent_refs_case();
    else abort();
    puts(argv[1]);
    return 0;
}
