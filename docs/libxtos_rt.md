# libxtos-rt

`libxtos-rt` is the first user-space runtime layer for XTOS real-time workloads on Linux. It intentionally wraps existing Linux primitives instead of claiming to replace the kernel scheduler.

## API

- `xtos_rt_set_scheduler`: set the calling thread's scheduler policy and priority.
- `xtos_rt_set_affinity`: bind the calling thread to one logical CPU.
- `xtos_rt_lock_memory`: lock an existing range with `mlock`.
- `xtos_rt_alloc_locked` / `xtos_rt_free_locked`: allocate and release page-aligned locked memory.
- `xtos_rt_apply`: apply a scheduler configuration and optional CPU affinity.
- `xtos_rt_get_current`: read the effective policy and priority.

The scheduler policy is supplied using Linux constants such as `SCHED_OTHER`, `SCHED_FIFO`, and `SCHED_RR`. `XTOS_RT_NO_CPU` disables the affinity step.

## Build and test

```sh
cd sdk/libxtos-rt
make clean
make test
make
```

Verified result:

```text
PASS assertions=12
```

The test covers current scheduler state, normal-policy application, affinity, invalid arguments, realtime permission boundaries, and locked-memory behavior. On an unprivileged process, `SCHED_FIFO` may correctly fail with `EPERM` or `EACCES`; that is reported as a tested permission boundary, not hidden.

## Scope

This is a Linux user-space wrapper. It does not provide a new scheduler, hard-real-time guarantees, GPU scheduling, or medical safety. Actual deterministic latency still depends on `CONFIG_PREEMPT_RT`, hardware, IRQ topology, CPU power policy, permissions, and workload configuration.
