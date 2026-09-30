#define _GNU_SOURCE
#include <errno.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "xtos/rt.h"

static int assertions;

#define CHECK(condition) do { \
    assertions++; \
    if (!(condition)) { \
        fprintf(stderr, "FAIL %s:%d: %s (errno=%d: %s)\n", \
                __FILE__, __LINE__, #condition, errno, strerror(errno)); \
        return 1; \
    } \
} while (0)

static int test_current_state(void)
{
    int policy;
    int priority;

    CHECK(xtos_rt_get_current(&policy, &priority) == 0);
    CHECK(policy == SCHED_OTHER || policy == SCHED_FIFO || policy == SCHED_RR);
    CHECK(priority >= 0);
    return 0;
}

static int test_normal_policy_and_affinity(void)
{
    struct xtos_rt_config config = {
        .policy = SCHED_OTHER,
        .priority = 0,
        .cpu = XTOS_RT_NO_CPU,
    };
    int cpu;

    CHECK(xtos_rt_apply(&config) == 0);
    cpu = sched_getcpu();
    CHECK(cpu >= 0);
    CHECK(xtos_rt_set_affinity(cpu) == 0);
    return 0;
}

static int test_invalid_arguments(void)
{
    errno = 0;
    CHECK(xtos_rt_apply(NULL) == -1 && errno == EINVAL);
    errno = 0;
    CHECK(xtos_rt_set_affinity(-1) == -1 && errno == EINVAL);
    errno = 0;
    CHECK(xtos_rt_lock_memory(NULL, 1) == -1 && errno == EINVAL);
    errno = 0;
    CHECK(xtos_rt_alloc_locked(0) == NULL && errno == EINVAL);
    return 0;
}

static int test_realtime_permission_boundary(void)
{
    int result;
    int error;

    errno = 0;
    result = xtos_rt_set_scheduler(SCHED_FIFO, 10);
    error = errno;
    if (result == 0) {
        CHECK(xtos_rt_set_scheduler(SCHED_OTHER, 0) == 0);
        return 0;
    }
    CHECK(error == EPERM || error == EACCES || error == EINVAL);
    return 0;
}

static int test_locked_memory(void)
{
    void *memory;
    int error;

    memory = xtos_rt_alloc_locked(4096);
    if (memory != NULL) {
        memset(memory, 0x5a, 4096);
        CHECK(xtos_rt_free_locked(memory, 4096) == 0);
        return 0;
    }
    error = errno;
    CHECK(error == EPERM || error == EAGAIN || error == ENOMEM);
    return 0;
}

int main(void)
{
    if (test_current_state() != 0 ||
        test_normal_policy_and_affinity() != 0 ||
        test_invalid_arguments() != 0 ||
        test_realtime_permission_boundary() != 0 ||
        test_locked_memory() != 0)
        return 1;

    printf("PASS assertions=%d\n", assertions);
    return 0;
}
