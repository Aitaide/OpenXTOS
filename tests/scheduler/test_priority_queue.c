// SPDX-License-Identifier: GPL-2.0
/* User-space tests for the shared XTOS O(1) priority queue core. */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <linux/xtos_queue.h>

struct test_task {
	int id;
	int priority;
	struct xtos_queue_node node;
};

static unsigned int tests_run;
static unsigned int tests_failed;

#define CHECK(condition) do { \
		tests_run++; \
		if (!(condition)) { \
			fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition); \
			tests_failed++; \
			return 1; \
		} \
	} while (0)

static void task_init(struct test_task *task, int id, int priority)
{
	memset(task, 0, sizeof(*task));
	task->id = id;
	task->priority = priority;
}

static struct test_task *node_to_task(struct xtos_queue_node *node)
{
	return (struct test_task *)((char *)node - offsetof(struct test_task, node));
}

static int test_empty_and_boundaries(void)
{
	struct xtos_queue_set set;
	struct test_task low, high;

	xtos_queue_set_init(&set);
	CHECK(set.bitmap == 0);
	CHECK(set.nr_running == 0);
	CHECK(xtos_queue_pick(&set) == NULL);

	task_init(&low, 1, XTOS_QUEUE_NR_PRIORITIES - 1);
	task_init(&high, 2, 0);
	CHECK(xtos_queue_enqueue(&set, &low.node, low.priority) == 0);
	CHECK(xtos_queue_enqueue(&set, &high.node, high.priority) == 0);
	CHECK(node_to_task(xtos_queue_pick(&set))->id == high.id);
	CHECK(xtos_queue_dequeue(&set, &high.node, high.priority) == 0);
	CHECK(node_to_task(xtos_queue_pick(&set))->id == low.id);
	CHECK(xtos_queue_dequeue(&set, &low.node, low.priority) == 0);
	CHECK(xtos_queue_pick(&set) == NULL);
	CHECK(set.bitmap == 0 && set.nr_running == 0);
	CHECK(xtos_queue_enqueue(&set, &low.node, -1) == -EINVAL);
	CHECK(xtos_queue_enqueue(&set, &low.node, XTOS_QUEUE_NR_PRIORITIES) == -EINVAL);
	return 0;
}

static int test_priority_order_and_fifo(void)
{
	struct xtos_queue_set set;
	struct test_task tasks[XTOS_QUEUE_NR_PRIORITIES];
	struct test_task fifo[4];
	unsigned int i;

	xtos_queue_set_init(&set);
	for (i = 0; i < XTOS_QUEUE_NR_PRIORITIES; i++) {
		task_init(&tasks[i], (int)i, XTOS_QUEUE_NR_PRIORITIES - 1 - (int)i);
		CHECK(xtos_queue_enqueue(&set, &tasks[i].node, tasks[i].priority) == 0);
	}
	for (i = 0; i < XTOS_QUEUE_NR_PRIORITIES; i++) {
		struct xtos_queue_node *node = xtos_queue_pick(&set);
		CHECK(node != NULL);
		CHECK(node_to_task(node)->priority == (int)i);
		CHECK(xtos_queue_dequeue(&set, node, node_to_task(node)->priority) == 0);
	}
	CHECK(set.nr_running == 0 && set.bitmap == 0);

	for (i = 0; i < 4; i++) {
		task_init(&fifo[i], (int)i, 7);
		CHECK(xtos_queue_enqueue(&set, &fifo[i].node, fifo[i].priority) == 0);
	}
	for (i = 0; i < 4; i++) {
		struct xtos_queue_node *node = xtos_queue_pick(&set);
		CHECK(node_to_task(node)->id == (int)i);
		CHECK(xtos_queue_dequeue(&set, node, 7) == 0);
	}
	return 0;
}

static int test_integrity_and_arbitrary_dequeue(void)
{
	struct xtos_queue_set set;
	struct test_task tasks[3];
	unsigned int i;

	xtos_queue_set_init(&set);
	for (i = 0; i < 3; i++) {
		task_init(&tasks[i], (int)i, 5);
		CHECK(xtos_queue_enqueue(&set, &tasks[i].node, 5) == 0);
	}
	CHECK(xtos_queue_enqueue(&set, &tasks[0].node, 5) == -EBUSY);
	CHECK(xtos_queue_dequeue(&set, &tasks[1].node, 4) == -ENOENT);
	CHECK(xtos_queue_dequeue(&set, &tasks[1].node, 5) == 0);
	CHECK(set.nr_running == 2);
	CHECK(node_to_task(xtos_queue_pick(&set))->id == 0);
	CHECK(xtos_queue_dequeue(&set, &tasks[0].node, 5) == 0);
	CHECK(node_to_task(xtos_queue_pick(&set))->id == 2);
	CHECK(xtos_queue_dequeue(&set, &tasks[2].node, 5) == 0);

	/* A stale bit must not hide a valid lower-priority queue. */
	task_init(&tasks[0], 10, 10);
	CHECK(xtos_queue_enqueue(&set, &tasks[0].node, 10) == 0);
	set.bitmap |= 1UL << 2;
	CHECK(node_to_task(xtos_queue_pick(&set))->id == 10);
	CHECK((set.bitmap & (1UL << 2)) == 0);
	CHECK(xtos_queue_dequeue(&set, &tasks[0].node, 10) == 0);
	return 0;
}

int main(void)
{
	int rc = 0;

	rc |= test_empty_and_boundaries();
	rc |= test_priority_order_and_fifo();
	rc |= test_integrity_and_arbitrary_dequeue();
	printf("XTOS queue tests: %u assertions, %u failures\n", tests_run, tests_failed);
	if (rc || tests_failed) {
		printf("RESULT: FAIL\n");
		return EXIT_FAILURE;
	}
	printf("RESULT: PASS\n");
	return EXIT_SUCCESS;
}
