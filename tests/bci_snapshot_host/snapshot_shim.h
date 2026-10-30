/* SPDX-License-Identifier: GPL-2.0 */
#ifndef XTOS_BCI_SNAPSHOT_HOST_SHIM_H
#define XTOS_BCI_SNAPSHOT_HOST_SHIM_H
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <linux/types.h>
#include "../../kernel/linux/include/uapi/linux/xtos_bci_snapshot.h"
typedef uint32_t u32;
typedef uint64_t u64;
typedef pthread_mutex_t spinlock_t;
typedef struct { atomic_uint value; } refcount_t;
#define spin_lock_irqsave(lock, flags) do { (flags) = 0; assert(!pthread_mutex_lock(lock)); } while (0)
#define spin_unlock_irqrestore(lock, flags) do { (void)(flags); assert(!pthread_mutex_unlock(lock)); } while (0)
static inline void refcount_set(refcount_t *r, unsigned int v) { atomic_init(&r->value, v); }
static inline void refcount_inc(refcount_t *r) { assert(atomic_fetch_add(&r->value, 1) > 0); }
static inline bool refcount_dec_and_test(refcount_t *r) { unsigned int n = atomic_fetch_sub(&r->value, 1); assert(n); return n == 1; }
#define ERR_PTR(e) ((void *)(intptr_t)(e))
#define PTR_ERR(p) ((long)(intptr_t)(p))
#define IS_ERR(p) ((uintptr_t)(p) >= (uintptr_t)-4095)
#define GFP_KERNEL 0
#define PAGE_SIZE 4096UL
#define PAGE_ALIGN(n) (((n) + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1))
#define min(a, b) ((a) < (b) ? (a) : (b))
#define VM_READ 0x01UL
#define VM_WRITE 0x02UL
#define VM_SHARED 0x08UL
#define VM_MAYWRITE 0x20UL
struct vm_area_struct;
struct vm_operations_struct {
    void (*open)(struct vm_area_struct *);
    void (*close)(struct vm_area_struct *);
};
struct vm_area_struct {
    unsigned long vm_start, vm_end, vm_pgoff, vm_flags;
    void *vm_private_data;
    const struct vm_operations_struct *vm_ops;
};
static inline void vm_flags_clear(struct vm_area_struct *v, unsigned long flags) { v->vm_flags &= ~flags; }
struct xtos_bci_ringbuf {
    void *buffer;
    size_t size;
    unsigned long read_pos, write_pos;
    spinlock_t lock;
};
static atomic_uint host_objects, host_mappings;
static int host_fail_alloc, host_remap_error, host_remap_calls;
static void *host_last_remap;
static size_t host_allocation_bytes;
static inline void *kzalloc(size_t n, int flags) {
    void *p; (void)flags;
    if (host_fail_alloc == 1) return NULL;
    p = calloc(1, n); if (p) atomic_fetch_add(&host_objects, 1); return p;
}
static inline void kfree(void *p) { if (p) { atomic_fetch_sub(&host_objects, 1); free(p); } }
static inline void *vmalloc_user(size_t n) {
    void *p; assert(n && !(n % PAGE_SIZE)); host_allocation_bytes = n;
    if (host_fail_alloc == 2) return NULL;
    p = calloc(1, n); if (p) atomic_fetch_add(&host_mappings, 1); return p;
}
static inline void vfree(void *p) { if (p) { atomic_fetch_sub(&host_mappings, 1); free(p); } }
static inline int remap_vmalloc_range(struct vm_area_struct *v, void *p, unsigned long off) {
    assert(v && p && !off); host_remap_calls++; host_last_remap = p; return host_remap_error;
}
#endif
