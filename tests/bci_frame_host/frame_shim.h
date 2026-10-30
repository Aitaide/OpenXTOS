/* SPDX-License-Identifier: GPL-2.0 */
#ifndef XTOS_BCI_FRAME_HOST_SHIM_H
#define XTOS_BCI_FRAME_HOST_SHIM_H
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <linux/types.h>
#include "../../kernel/linux/include/uapi/linux/xtos_bci_frame.h"
typedef uint32_t u32;
typedef uint64_t u64;
typedef pthread_mutex_t spinlock_t;
typedef struct { pthread_mutex_t lock; pthread_cond_t cond; unsigned int wakes; } wait_queue_head_t;
#define U64_MAX UINT64_MAX
#define spin_lock_init(l) assert(!pthread_mutex_init(l, NULL))
#define spin_lock_irqsave(l,f) do { (f)=0; assert(!pthread_mutex_lock(l)); } while (0)
#define spin_unlock_irqrestore(l,f) do { (void)(f); assert(!pthread_mutex_unlock(l)); } while (0)
static inline void init_waitqueue_head(wait_queue_head_t *q)
{ assert(!pthread_mutex_init(&q->lock,NULL)); assert(!pthread_cond_init(&q->cond,NULL)); q->wakes=0; }
static inline void wake_up_interruptible(wait_queue_head_t *q)
{ assert(!pthread_mutex_lock(&q->lock)); q->wakes++; assert(!pthread_cond_broadcast(&q->cond)); assert(!pthread_mutex_unlock(&q->lock)); }
#define GFP_KERNEL 0
#define ERR_PTR(e) ((void *)(intptr_t)(e))
#define PTR_ERR(p) ((long)(intptr_t)(p))
#define IS_ERR(p) ((uintptr_t)(p) >= (uintptr_t)-4095)
static int host_alloc_fail, host_alloc_calls, host_alloc_live;
static inline void *kcalloc(size_t n, size_t s, int flags)
{ void *p; (void)flags; host_alloc_calls++; if(host_alloc_calls==host_alloc_fail) return NULL; p=calloc(n,s); if(p) host_alloc_live++; return p; }
static inline void kfree(void *p) { if(p) { host_alloc_live--; free(p); } }
#endif
