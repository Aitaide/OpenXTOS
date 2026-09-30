/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _LINUX_XTOS_QUEUE_H
#define _LINUX_XTOS_QUEUE_H

/*
 * Small, intrusive O(1) priority queue core shared by the kernel helper and
 * the user-space unit test.  The caller provides any required locking.
 */
#ifdef __KERNEL__
#include <linux/errno.h>
#else
#include <errno.h>
#include <stddef.h>
#endif

#define XTOS_QUEUE_NR_PRIORITIES 15

struct xtos_queue;

struct xtos_queue_node {
	struct xtos_queue_node *prev;
	struct xtos_queue_node *next;
	struct xtos_queue *owner;
};

struct xtos_queue {
	struct xtos_queue_node *head;
	struct xtos_queue_node *tail;
};

struct xtos_queue_set {
	struct xtos_queue queue[XTOS_QUEUE_NR_PRIORITIES];
	unsigned long bitmap;
	unsigned int nr_running;
};

static inline int xtos_queue_prio_valid(int priority)
{
	return priority >= 0 && priority < XTOS_QUEUE_NR_PRIORITIES;
}

static inline void xtos_queue_set_init(struct xtos_queue_set *set)
{
	unsigned int i;

	for (i = 0; i < XTOS_QUEUE_NR_PRIORITIES; i++) {
		set->queue[i].head = NULL;
		set->queue[i].tail = NULL;
	}
	set->bitmap = 0;
	set->nr_running = 0;
}

static inline int xtos_queue_enqueue(struct xtos_queue_set *set,
					 struct xtos_queue_node *node, int priority)
{
	struct xtos_queue *queue;

	if (!set || !node || !xtos_queue_prio_valid(priority))
		return -EINVAL;
	if (node->owner)
		return -EBUSY;

	queue = &set->queue[priority];
	node->prev = queue->tail;
	node->next = NULL;
	node->owner = queue;
	if (queue->tail)
		queue->tail->next = node;
	else
		queue->head = node;
	queue->tail = node;
	set->bitmap |= 1UL << priority;
	set->nr_running++;

	return 0;
}

static inline int xtos_queue_dequeue(struct xtos_queue_set *set,
					 struct xtos_queue_node *node, int priority)
{
	struct xtos_queue *queue;

	if (!set || !node || !xtos_queue_prio_valid(priority))
		return -EINVAL;

	queue = &set->queue[priority];
	if (node->owner != queue)
		return -ENOENT;

	if (node->prev)
		node->prev->next = node->next;
	else
		queue->head = node->next;
	if (node->next)
		node->next->prev = node->prev;
	else
		queue->tail = node->prev;

	node->prev = NULL;
	node->next = NULL;
	node->owner = NULL;
	if (!queue->head)
		set->bitmap &= ~(1UL << priority);
	if (set->nr_running)
		set->nr_running--;

	return 0;
}

static inline struct xtos_queue_node *xtos_queue_pick(struct xtos_queue_set *set)
{
	unsigned int priority;

	if (!set)
		return NULL;

	while (set->bitmap) {
		priority = (unsigned int)__builtin_ctzl(set->bitmap);
		if (set->queue[priority].head)
			return set->queue[priority].head;
		/* Repair a stale bit and continue to the next priority. */
		set->bitmap &= ~(1UL << priority);
	}

	return NULL;
}

#endif /* _LINUX_XTOS_QUEUE_H */
