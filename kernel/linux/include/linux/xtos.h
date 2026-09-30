/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_XTOS_H
#define _LINUX_XTOS_H

#include <linux/types.h>

#define XTOS_PRIO_MIN 0
#define XTOS_PRIO_MAX 14
#define XTOS_NR_PRIORITIES 15
#define XTOS_CPU_AFFINITY_NONE (-1)

/* Metadata passed between a caller and the optional XTOS helper. */
struct xtos_attr {
	__s32 priority;
	__s32 cpu_affinity;
	__u64 deadline_ns;
	__u8 eeg_sensitive;
	__u8 __reserved[7];
};

struct xtos_stats {
	__u64 total_runtime_ns;
	__u64 wait_time_ns;
	__u64 context_switches;
	__u64 preempt_count;
	__u64 deadline_misses;
};

#ifdef __KERNEL__

#include <linux/sched.h>
#include <linux/spinlock_types.h>
#include <linux/xtos_queue.h>

struct xtos_rq;

/*
 * XTOS task state is an opt-in control block.  It is not embedded in
 * task_struct and does not change Linux's active sched_class.
 */
struct xtos_task {
	struct task_struct *task; /* non-owning reference supplied by the caller */
	int xtos_priority;
	int base_priority;
	__u64 deadline_ns;
	int cpu_affinity;

	bool eeg_sensitive;
	__u64 last_eeg_event;
	int eeg_boost_level;

	__u64 total_runtime;
	__u64 wait_time;
	__u64 last_enqueue;
	__u64 context_switches;
	__u64 preempt_count;
	__u64 deadline_misses;

	struct xtos_queue_node queue_node;
	struct xtos_rq *rq;
	spinlock_t state_lock;
	bool on_rq;
	bool initialized;
};

struct xtos_rq {
	spinlock_t lock;
	struct xtos_queue_set queues;
	struct xtos_task *curr;
	__u64 total_switches;
	__u64 avg_latency_ns;
	__u64 last_schedule_time;
};

struct xtos_rq *xtos_rq_for_cpu(int cpu);
void init_xtos_rq(struct xtos_rq *rq);
int enqueue_task_xtos(struct xtos_rq *rq, struct xtos_task *task);
int dequeue_task_xtos(struct xtos_rq *rq, struct xtos_task *task);
struct xtos_task *pick_next_task_xtos(struct xtos_rq *rq);

int xtos_task_init(struct xtos_task *xt, struct task_struct *task);
void xtos_task_destroy(struct xtos_task *xt);
int xtos_set_priority(struct xtos_task *xt, int priority);
int xtos_set_cpu_affinity(struct xtos_task *xt, int cpu);
int xtos_set_deadline(struct xtos_task *xt, __u64 deadline_ns);
void xtos_get_stats(struct xtos_task *xt, struct xtos_stats *stats);

#endif /* __KERNEL__ */

#endif /* _LINUX_XTOS_H */
