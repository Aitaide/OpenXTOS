#ifndef XTOS_RT_H
#define XTOS_RT_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define XTOS_RT_NO_CPU (-1)

struct xtos_rt_config {
    int policy;
    int priority;
    int cpu;
};

/* Apply a scheduler policy and optional CPU affinity to the calling thread. */
int xtos_rt_apply(const struct xtos_rt_config *config);

/* Set the calling thread's Linux scheduler policy and priority. */
int xtos_rt_set_scheduler(int policy, int priority);

/* Restrict the calling thread to one logical CPU. */
int xtos_rt_set_affinity(int cpu);

/* Lock an existing memory range; caller owns the range. */
int xtos_rt_lock_memory(void *address, size_t length);

/* Allocate page-aligned locked memory owned by the caller. */
void *xtos_rt_alloc_locked(size_t length);

/* Unlock and release memory allocated by xtos_rt_alloc_locked. */
int xtos_rt_free_locked(void *address, size_t length);

/* Read the calling thread's effective Linux policy and priority. */
int xtos_rt_get_current(int *policy, int *priority);

#ifdef __cplusplus
}
#endif

#endif
