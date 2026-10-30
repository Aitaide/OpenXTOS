/* SPDX-License-Identifier: GPL-2.0 */
#ifndef XTOS_BCI_FRAME_SNAPSHOT_SHIM_H
#define XTOS_BCI_FRAME_SNAPSHOT_SHIM_H
#include "../bci_frame_host/frame_shim.h"
#define PAGE_SIZE 4096ul
#define PAGE_ALIGN(n) (((n) + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1))
typedef struct { atomic_uint value; } refcount_t;
static inline void refcount_set(refcount_t *r, unsigned int n) { atomic_init(&r->value,n); }
static inline void refcount_inc(refcount_t *r) { assert(atomic_fetch_add(&r->value,1)>0); }
static inline bool refcount_dec_and_test(refcount_t *r) { unsigned int n=atomic_fetch_sub(&r->value,1); assert(n); return n==1; }
#define VM_WRITE 1ul
#define VM_SHARED 2ul
#define VM_MAYWRITE 4ul
struct vm_area_struct;
struct vm_operations_struct { void (*open)(struct vm_area_struct *); void (*close)(struct vm_area_struct *); };
struct vm_area_struct { unsigned long vm_start, vm_end, vm_pgoff, vm_flags; void *vm_private_data; const struct vm_operations_struct *vm_ops; };
static void (*allocation_hook)(void);
static int vm_fail, vm_live, vm_frees, remap_error, remap_calls;
static size_t vm_allocated;
static inline void *vmalloc_user(size_t n) { void *p; vm_allocated=n; if(allocation_hook) allocation_hook(); if(vm_fail) return NULL; p=calloc(1,n); if(p) vm_live++; return p; }
static inline void vfree(void *p) { if(p) { vm_live--; vm_frees++; free(p); } }
#define kzalloc(n,f) kcalloc(1,n,f)
static inline void vm_flags_clear(struct vm_area_struct *v, unsigned long f) { v->vm_flags &= ~f; }
static inline int remap_vmalloc_range(struct vm_area_struct *v, void *p, unsigned long off) { assert(p && !off && !(v->vm_flags & VM_MAYWRITE)); remap_calls++; return remap_error; }
#endif
