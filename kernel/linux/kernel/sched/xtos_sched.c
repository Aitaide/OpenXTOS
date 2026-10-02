// SPDX-License-Identifier: GPL-2.0
/*
 * XTOS scheduler helper.
 *
 * This is deliberately not a sched_class.  It provides an opt-in, locked
 * O(1) priority queue that an integration layer may use without changing
 * Linux's scheduler selection, task_struct layout, or syscall ABI.
 */

#include <linux/atomic.h>
#include <linux/cpumask.h>
#include <linux/errno.h>
#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/ktime.h>
#include <linux/module.h>
#include <linux/percpu.h>
#include <linux/sched.h>
#include <linux/spinlock.h>
#include <linux/xtos.h>

#include "xtos_prio.h"

static DEFINE_PER_CPU(struct xtos_rq, xtos_runqueues);
static atomic64_t xtos_total_enqueues;
static atomic64_t xtos_total_dequeues;
static atomic64_t xtos_total_picks;

static void xtos_remove_locked(struct xtos_rq *rq, struct xtos_task *task)
{
	int ret;

	ret = xtos_queue_dequeue(&rq->queues, &task->queue_node,
				 task->xtos_priority);
		if (WARN_ON_ONCE(ret))
			return;

	if (task->last_enqueue) {
		task->wait_time += ktime_get_ns() - task->last_enqueue;
		task->last_enqueue = 0;
	}
	task->on_rq = false;
	task->rq = NULL;
}

struct xtos_rq *xtos_rq_for_cpu(int cpu)
{
	if (cpu < 0 || cpu >= nr_cpu_ids || !cpu_possible(cpu))
		return NULL;
	return &per_cpu(xtos_runqueues, cpu);
}
EXPORT_SYMBOL_GPL(xtos_rq_for_cpu);

void init_xtos_rq(struct xtos_rq *rq)
{
	if (!rq)
		return;

	spin_lock_init(&rq->lock);
	xtos_queue_set_init(&rq->queues);
	rq->curr = NULL;
	rq->total_switches = 0;
	rq->avg_latency_ns = 0;
	rq->last_schedule_time = 0;
}
EXPORT_SYMBOL_GPL(init_xtos_rq);

int enqueue_task_xtos(struct xtos_rq *rq, struct xtos_task *task)
{
	unsigned long flags;
	int ret;

	if (!rq || !task || !task->initialized)
		return -EINVAL;
	if (!XTOS_PRIO_VALID(task->xtos_priority))
		return -EINVAL;

	spin_lock_irqsave(&task->state_lock, flags);
	spin_lock(&rq->lock);
	if (task->on_rq) {
		ret = -EBUSY;
		goto out;
	}
	ret = xtos_queue_enqueue(&rq->queues, &task->queue_node,
					task->xtos_priority);
	if (ret)
		goto out;

	task->on_rq = true;
	task->rq = rq;
	task->last_enqueue = ktime_get_ns();
	atomic64_inc(&xtos_total_enqueues);
out:
	spin_unlock(&rq->lock);
	spin_unlock_irqrestore(&task->state_lock, flags);
	return ret;
}
EXPORT_SYMBOL_GPL(enqueue_task_xtos);

int dequeue_task_xtos(struct xtos_rq *rq, struct xtos_task *task)
{
	unsigned long flags;
	int ret = 0;

	if (!rq || !task || !task->initialized)
		return -EINVAL;

	spin_lock_irqsave(&task->state_lock, flags);
	spin_lock(&rq->lock);
	if (!task->on_rq || task->rq != rq) {
		ret = -ENOENT;
		goto out;
	}
	xtos_remove_locked(rq, task);
	atomic64_inc(&xtos_total_dequeues);
out:
	spin_unlock(&rq->lock);
	spin_unlock_irqrestore(&task->state_lock, flags);
	return ret;
}
EXPORT_SYMBOL_GPL(dequeue_task_xtos);

struct xtos_task *pick_next_task_xtos(struct xtos_rq *rq)
{
	unsigned long flags;
	struct xtos_queue_node *node;
	struct xtos_task *task = NULL;

	if (!rq)
		return NULL;

	spin_lock_irqsave(&rq->lock, flags);
	node = xtos_queue_pick(&rq->queues);
	if (node) {
		task = container_of(node, struct xtos_task, queue_node);
		atomic64_inc(&xtos_total_picks);
	}
	spin_unlock_irqrestore(&rq->lock, flags);

	return task;
}
EXPORT_SYMBOL_GPL(pick_next_task_xtos);

int xtos_task_init(struct xtos_task *xt, struct task_struct *task)
{
	if (!xt)
		return -EINVAL;

	memset(xt, 0, sizeof(*xt));
	spin_lock_init(&xt->state_lock);
	xt->task = task;
	xt->xtos_priority = XTOS_PRIO_NORMAL;
	xt->base_priority = XTOS_PRIO_NORMAL;
	xt->cpu_affinity = XTOS_CPU_AFFINITY_NONE;
	xt->initialized = true;
	return 0;
}
EXPORT_SYMBOL_GPL(xtos_task_init);

void xtos_task_destroy(struct xtos_task *xt)
{
	unsigned long flags;
	struct xtos_rq *rq;
	struct task_struct *task;

	if (!xt || !xt->initialized)
		return;

	spin_lock_irqsave(&xt->state_lock, flags);
	rq = xt->rq;
	if (rq) {
		spin_lock(&rq->lock);
		if (xt->on_rq)
			xtos_remove_locked(rq, xt);
		spin_unlock(&rq->lock);
	}
	task = xt->task;
	xt->task = NULL;
	xt->initialized = false;
	spin_unlock_irqrestore(&xt->state_lock, flags);

	/* The task pointer is non-owning; its lifetime belongs to the caller. */
	(void)task;
}
EXPORT_SYMBOL_GPL(xtos_task_destroy);

int xtos_set_priority(struct xtos_task *xt, int priority)
{
	unsigned long flags;
	struct xtos_rq *rq;
	int old_priority;
	int ret;

	if (!xt || !xt->initialized || !XTOS_PRIO_VALID(priority))
		return -EINVAL;

	spin_lock_irqsave(&xt->state_lock, flags);
	old_priority = xt->xtos_priority;
	if (old_priority == priority) {
		spin_unlock_irqrestore(&xt->state_lock, flags);
		return 0;
	}

	rq = xt->rq;
	if (!rq) {
		xt->xtos_priority = priority;
		xt->base_priority = priority;
		spin_unlock_irqrestore(&xt->state_lock, flags);
		return 0;
	}

	spin_lock(&rq->lock);
	if (!xt->on_rq) {
		ret = -ENOENT;
		goto out;
	}
	ret = xtos_queue_dequeue(&rq->queues, &xt->queue_node, old_priority);
	if (ret)
		goto out;
	xt->xtos_priority = priority;
	xt->base_priority = priority;
	ret = xtos_queue_enqueue(&rq->queues, &xt->queue_node, priority);
	if (WARN_ON_ONCE(ret))
		xt->xtos_priority = old_priority;
out:
	spin_unlock(&rq->lock);
	spin_unlock_irqrestore(&xt->state_lock, flags);
	return ret;
}
EXPORT_SYMBOL_GPL(xtos_set_priority);

int xtos_set_cpu_affinity(struct xtos_task *xt, int cpu)
{
	unsigned long flags;

	if (!xt || !xt->initialized)
		return -EINVAL;
	if (cpu != XTOS_CPU_AFFINITY_NONE &&
	    (cpu < 0 || cpu >= nr_cpu_ids || !cpu_possible(cpu)))
		return -EINVAL;

	spin_lock_irqsave(&xt->state_lock, flags);
	/* Advisory metadata only; this helper does not migrate Linux tasks. */
	xt->cpu_affinity = cpu;
	spin_unlock_irqrestore(&xt->state_lock, flags);
	return 0;
}
EXPORT_SYMBOL_GPL(xtos_set_cpu_affinity);

int xtos_set_deadline(struct xtos_task *xt, __u64 deadline_ns)
{
	unsigned long flags;

	if (!xt || !xt->initialized)
		return -EINVAL;

	spin_lock_irqsave(&xt->state_lock, flags);
	xt->deadline_ns = deadline_ns;
	spin_unlock_irqrestore(&xt->state_lock, flags);
	return 0;
}
EXPORT_SYMBOL_GPL(xtos_set_deadline);

void xtos_get_stats(struct xtos_task *xt, struct xtos_stats *stats)
{
	unsigned long flags;

	if (!stats)
		return;
	memset(stats, 0, sizeof(*stats));
	if (!xt || !xt->initialized)
		return;

	spin_lock_irqsave(&xt->state_lock, flags);
	stats->total_runtime_ns = xt->total_runtime;
	stats->wait_time_ns = xt->wait_time;
	stats->context_switches = xt->context_switches;
	stats->preempt_count = xt->preempt_count;
	stats->deadline_misses = xt->deadline_misses;
	spin_unlock_irqrestore(&xt->state_lock, flags);
}
EXPORT_SYMBOL_GPL(xtos_get_stats);

static int __init xtos_sched_init(void)
{
	int cpu;

	for_each_possible_cpu(cpu)
		init_xtos_rq(&per_cpu(xtos_runqueues, cpu));

	atomic64_set(&xtos_total_enqueues, 0);
	atomic64_set(&xtos_total_dequeues, 0);
	atomic64_set(&xtos_total_picks, 0);
	pr_info("XTOS: optional O(1) scheduler helper initialized; Linux sched_class unchanged\n");
	return 0;
}
module_init(xtos_sched_init);

static void __exit xtos_sched_exit(void)
{
	pr_info("XTOS: scheduler helper unloaded\n");
}
module_exit(xtos_sched_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("XTOS Development Team");
MODULE_DESCRIPTION("XTOS optional O(1) scheduler helper");
