# XTOS scheduler integration audit

Date: 2026-09-30
Kernel tree: Linux 6.6.0-rt15 (`make kernelrelease`)

## Three definitive results

1. **源码编译：通过。** `kernel/sched/xtos_sched.c` 在
   `CONFIG_XTOS_SCHED=y` 下成功编译，最终对象文件实际存在；用户态 O(1)
   队列测试为 101 assertions、0 failures。`CONFIG_XTOS_SCHED=n` 时该源码
   不参与构建。
2. **完整内核构建：通过。** 已实际执行 `make olddefconfig` 和
   `make -j$(nproc) bzImage`。当前配置为 `CONFIG_XTOS_SCHED=y`，命令返回
   0，生成的 `arch/x86/boot/bzImage` 实际存在，大小 12,513,952 bytes；
   `vmlinux` 实际存在，大小 48,812,496 bytes。内核版本为
   `6.6.0-rt15`。
3. **真正主调度集成/启动验证：未完成。** XTOS **没有替换 Linux
   `sched_class`**，没有接入 Linux 正常任务选择路径；也没有在 VM 或真实
   目标机启动该 bzImage。因此不能声称 XTOS 已成为主调度器，也不能声称
   已完成启动验证或达到实时延迟目标。

## Scope and integration decision

XTOS is integrated as an optional scheduler helper, not as a Linux `sched_class`.
`CONFIG_XTOS_SCHED` defaults to `n`; when disabled, `kernel/sched/xtos_sched.c`
is not compiled. When enabled, the helper is built into the kernel (or can be
selected as a module) and exposes a locked, intrusive O(1) priority queue API.
Linux's active scheduler classes, `task_struct`, normal system-call ABI, and
RT scheduler are unchanged. No claim is made that XTOS controls Linux task
selection or that the latency targets are achieved.

## Existing-code audit and fixes

The previous implementation was not safe to integrate as a scheduler:

- It was unconditionally added with `obj-y`, so every kernel build compiled it.
- It declared `SYSCALL_DEFINE*` entry points without architecture syscall-table
  integration or a UAPI contract.
- It kept task pointers in a PID hash without a lifetime protocol and returned
  pointers after dropping the hash lock; it also leaked newly allocated task
  blocks and could double-remove list/hash nodes.
- `find_task_by_vpid()` was used without the appropriate tasklist/RCU lifetime
  discipline.
- Queue state, priority, affinity, and statistics were accessed without a
  consistent lock; `task_cpu()` could change concurrently, and a task could be
  dequeued from the wrong per-CPU queue.
- The run queue bitmap and list nodes were separate from any Linux scheduler
  state, so the code did not actually participate in scheduling.
- `__ffs()` was used without a type/width abstraction, and priority validation
  occurred after queue state assumptions. Queue counters could underflow.
- `subsys_initcall()` was paired with module metadata but there was no safe
  unload lifecycle.
- The old kernel test was a module test that linked a scheduler implementation
  into a module and used kernel-only headers; it was not a user-space unit test.

The replacement removes the pseudo-syscalls/hash/list global registry, keeps the
control block explicitly owned by the caller, initializes all fields, validates
state transitions, and uses `state_lock` plus run-queue locking for helper
operations. CPU affinity and deadlines are explicitly advisory metadata; this
component does not migrate tasks or enforce deadlines.

## Files changed or created for this integration

- `kernel/linux/init/Kconfig`: sources XTOS Kconfig.
- `kernel/linux/kernel/sched/xtos/Kconfig`: adds `CONFIG_XTOS_SCHED`, default
  `n`, with built-in/module support.
- `kernel/linux/kernel/sched/Makefile`: uses `obj-$(CONFIG_XTOS_SCHED)`.
- `kernel/linux/kernel/sched/xtos_sched.c`: optional helper implementation.
- `kernel/linux/kernel/sched/xtos_prio.h`: priority constants and validation.
- `kernel/linux/include/linux/xtos.h`: kernel API and control structures.
- `kernel/linux/include/linux/xtos_queue.h`: shared queue core used by kernel
  code and user-space tests.
- `tests/scheduler/test_priority_queue.c`: user-space queue tests.
- `tests/scheduler/Makefile`: standalone test build/run rules.
- `tests/scheduler/test-output.txt`: captured unit-test output.
- `docs/xtos_*_output.txt`: captured Kconfig/build command output.

No files outside scheduler-related source/configuration, tests, and audit/build
documentation were intentionally modified. Existing unrelated worktree changes
were left untouched.

## Commands and real results

### User-space queue tests

```text
$ cd /root/xtos-project/tests/scheduler
$ make clean && make test
cc -std=c11 -O2 -Wall -Wextra -Werror -o test_priority_queue test_priority_queue.c
./test_priority_queue
XTOS queue tests: 101 assertions, 0 failures
RESULT: PASS
```

The test covers empty queues, priority boundaries, all 15 priorities, FIFO
within a priority, arbitrary removal, duplicate enqueue rejection, invalid
priority rejection, bitmap repair, and running-count integrity.

### Kconfig and complete kernel builds

```text
$ cd /root/xtos-project/kernel/linux
$ make olddefconfig
# configuration written to .config

$ scripts/config --disable XTOS_SCHED
$ make olddefconfig
$ make -j$(nproc) bzImage
Kernel: arch/x86/boot/bzImage is ready  (#3)

$ scripts/config --enable XTOS_SCHED
$ make olddefconfig
$ make -j$(nproc) bzImage
Kernel: arch/x86/boot/bzImage is ready  (#4)
```

The enabled build finished with `CONFIG_XTOS_SCHED=y`; the resulting
`arch/x86/boot/bzImage` and `vmlinux` were checked on disk after the build.
`nm vmlinux` shows the XTOS helper symbols `xtos_rq_for_cpu` and
`xtos_set_priority`. The exact command logs are retained under `docs/`.

An attempted standalone `M=kernel/sched CONFIG_XTOS_SCHED=m modules` build
compiled the source but failed at modpost because the standalone invocation had
no complete `Module.symvers` dependency set. This is recorded in
`docs/xtos_module_build_output.txt`; it does not affect the verified built-in
`bzImage` builds.

## Integration status

- Source compilation: **PASS** with `CONFIG_XTOS_SCHED=y`; also PASS with the
  option disabled (the source is omitted).
- Complete kernel build: **PASS**; `olddefconfig` and `make -j$(nproc) bzImage`
  returned success and produced the on-disk image.
- Main scheduler replacement/integration: **NOT DONE BY DESIGN**. This is an
  optional helper; Linux's `sched_class` and task selection path are unchanged.
- User-space core tests: **PASS**, 101 assertions, 0 failures.
- Bootable-image artifact generation: **PASS** (`bzImage` generated).
- Actual boot/startup verification: **NOT PERFORMED**. No VM or target boot was
  run, so this audit does not claim that the image boots.
- RT behavior: **NOT CLAIMED**. The tree identifies as `6.6.0-rt15`, but this
  component does not replace or hook the PREEMPT_RT scheduler.
