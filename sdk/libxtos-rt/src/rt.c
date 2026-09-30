#define _GNU_SOURCE
#include <errno.h>
#include <sched.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>

#include "xtos/rt.h"

int xtos_rt_set_scheduler(int policy, int priority)
{
    int min_priority;
    int max_priority;
    struct sched_param parameter;

    min_priority = sched_get_priority_min(policy);
    max_priority = sched_get_priority_max(policy);
    if (min_priority == -1 || max_priority == -1)
        return -1;
    if (priority < min_priority || priority > max_priority) {
        errno = EINVAL;
        return -1;
    }

    parameter.sched_priority = priority;
    return sched_setscheduler(0, policy, &parameter);
}

int xtos_rt_set_affinity(int cpu)
{
    cpu_set_t set;

    if (cpu < 0 || cpu >= CPU_SETSIZE) {
        errno = EINVAL;
        return -1;
    }
    CPU_ZERO(&set);
    CPU_SET((unsigned int)cpu, &set);
    return sched_setaffinity(0, sizeof(set), &set);
}

int xtos_rt_apply(const struct xtos_rt_config *config)
{
    if (config == NULL) {
        errno = EINVAL;
        return -1;
    }
    if (xtos_rt_set_scheduler(config->policy, config->priority) < 0)
        return -1;
    if (config->cpu != XTOS_RT_NO_CPU && xtos_rt_set_affinity(config->cpu) < 0)
        return -1;
    return 0;
}

int xtos_rt_lock_memory(void *address, size_t length)
{
    if (address == NULL || length == 0) {
        errno = EINVAL;
        return -1;
    }
    return mlock(address, length);
}

void *xtos_rt_alloc_locked(size_t length)
{
    long page_size;
    void *address = NULL;
    int error;

    if (length == 0) {
        errno = EINVAL;
        return NULL;
    }
    page_size = sysconf(_SC_PAGESIZE);
    if (page_size <= 0) {
        errno = ENOSYS;
        return NULL;
    }
    error = posix_memalign(&address, (size_t)page_size, length);
    if (error != 0) {
        errno = error;
        return NULL;
    }
    if (mlock(address, length) < 0) {
        error = errno;
        free(address);
        errno = error;
        return NULL;
    }
    return address;
}

int xtos_rt_free_locked(void *address, size_t length)
{
    int result;
    int error = 0;

    if (address == NULL || length == 0) {
        errno = EINVAL;
        return -1;
    }
    result = munlock(address, length);
    if (result < 0)
        error = errno;
    free(address);
    if (error != 0) {
        errno = error;
        return -1;
    }
    return 0;
}

int xtos_rt_get_current(int *policy, int *priority)
{
    struct sched_param parameter;
    int current_policy;

    if (policy == NULL || priority == NULL) {
        errno = EINVAL;
        return -1;
    }
    current_policy = sched_getscheduler(0);
    if (current_policy < 0)
        return -1;
    if (sched_getparam(0, &parameter) < 0)
        return -1;
    *policy = current_policy;
    *priority = parameter.sched_priority;
    return 0;
}
