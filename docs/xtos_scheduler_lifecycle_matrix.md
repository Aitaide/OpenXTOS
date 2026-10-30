# XTOS sched_class 生命周期审查矩阵

状态：审查矩阵，未进入 M2。

判定值只有 `PASS`、`FAIL`、`BLOCKED`。当前每一行均为 `BLOCKED`，因为 M0 真实任务门尚未关闭，RFC 尚未获内核 scheduler/PREEMPT_RT reviewer 批准。实现者不得用“代码已编译”替代生命周期证据。

覆盖基线：仓库当前 vendored Linux 6.6.0-rt15 的 `kernel/linux/kernel/sched/sched.h` 中 `struct sched_class`。自动断言由 `tests/test_sched_class_hooks.py` 读取该结构的 callback 字段，并要求 RFC 与本矩阵均逐项列出。更新内核基线时必须重跑覆盖测试并复核新字段；本矩阵不声称涵盖未来 kernel callbacks。

| Callback | 编译条件 | 必须定义的行为 | 锁/上下文风险 | 最低测试与证据 | 当前判定 | 负责人 |
|---|---|---|---|---|---|---|
| `enqueue_task` | always | runnable 入队恰好一次，wake/preempt 语义 | rq lock、IRQ 禁止睡眠 | queue invariant、wake stress、lockdep | BLOCKED | scheduler reviewer |
| `dequeue_task` | always | dequeue 对 sleep/exit/migrate 可重复安全 | rq lock、计数不下溢 | state transition、exit race | BLOCKED | scheduler reviewer |
| `yield_task` | always | yield 后公平位置和预算规则 | rq lock、yield/抢占相互作用 | yield KUnit | BLOCKED | scheduler reviewer |
| `yield_to_task` | always | 目标 task 合法性及拒绝条件 | task lifetime、rq lock | valid/invalid target tests | BLOCKED | scheduler reviewer |
| `check_preempt_curr` | always | 相对 dl/rt/XTOS/fair 的抢占关系 | rq lock、class order | mixed-class preempt matrix | BLOCKED | scheduler reviewer |
| `pick_next_task` | always | 选择下一个任务和空队列回退 | rq lock、core fast path | exact guest readback、empty queue | BLOCKED | scheduler reviewer |
| `put_prev_task` | always | 保存执行状态/剩余 budget | rq lock、current task 生命周期 | switch-out state tests | BLOCKED | scheduler reviewer |
| `set_next_task` | always | 初始化/续用 task 执行状态 | rq lock、first 标记语义 | switch-in/requeue tests | BLOCKED | scheduler reviewer |
| `balance` | `CONFIG_SMP` | 是否禁止跨分区 balance及返回语义 | rq lock、rq_flags、IPI | SMP balance on/off tests | BLOCKED | SMP reviewer |
| `select_task_rq` | `CONFIG_SMP` | CPU 选择、固定分区和拒绝策略 | affinity/task_cpu 竞态 | allowed-mask/hotplug tests | BLOCKED | SMP reviewer |
| `pick_task` | `CONFIG_SMP` | core-sched 查询与 pick_next 一致 | rq lock、core scheduling | SCHED_CORE on/off tests | BLOCKED | scheduler reviewer |
| `migrate_task_rq` | `CONFIG_SMP` | migration 时队列/时间状态更新 | 双 rq 锁、task_cpu 竞态 | migration/CPU-offline tests | BLOCKED | SMP reviewer |
| `task_woken` | `CONFIG_SMP` | remote wake/负载唤醒行为 | remote rq、IPI、抢占 | remote wake tests | BLOCKED | SMP reviewer |
| `set_cpus_allowed` | `CONFIG_SMP` | mask 更新及无法满足分区时的拒绝 | affinity_context、双 rq 锁 | concurrent affinity tests | BLOCKED | SMP reviewer |
| `rq_online` | `CONFIG_SMP` | CPU 上线时本地队列初始化 | hotplug serialization | CPU online loop | BLOCKED | SMP reviewer |
| `rq_offline` | `CONFIG_SMP` | CPU 下线前 drain/拒绝 runnable task | hotplug lock、rq drain | offline with runnable tasks | BLOCKED | SMP reviewer |
| `find_lock_rq` | `CONFIG_SMP` | 迁移目标 rq 定位/锁转移 | rq lock handoff、死锁 | lockdep migration tests | BLOCKED | SMP reviewer |
| `task_tick` | always | tick accounting、RR quantum或预算更新 | IRQ/tick context、不得睡眠 | tick/budget exhaustion tests | BLOCKED | RT reviewer |
| `task_fork` | always | 子 task policy/reset-on-fork/私有状态 | task init 生命周期 | fork/reset-on-fork tests | BLOCKED | UAPI reviewer |
| `task_dead` | always | task 退出时队列/计时器/引用释放 | 不能残留 timer/task ref | exit storm/leak check | BLOCKED | scheduler reviewer |
| `switched_from` | always | 切出本类后的状态回收 | rq lock 可被放下；pi_lock 串行 | policy-switch race | BLOCKED | scheduler reviewer |
| `switched_to` | always | 切入本类后的运行状态初始化 | rq/PI lock 顺序 | policy-switch/readback tests | BLOCKED | scheduler reviewer |
| `prio_changed` | always | 参数变更后的队列重排/抢占 | rq lock、PI 竞态 | concurrent priority update | BLOCKED | UAPI reviewer |
| `get_rr_interval` | always | 返回一致 RR interval，或明确不适用 | read-only contract | API readback test | BLOCKED | UAPI reviewer |
| `update_curr` | always | 墙钟/CPU time、budget 精确扣减 | rq lock、tickless accounting | exact budget boundary | BLOCKED | RT reviewer |
| `task_change_group` | `CONFIG_FAIR_GROUP_SCHED` | group/cgroup 变更是否支持/拒绝 | css/task lifetime | group move tests | BLOCKED | kernel reviewer |
| `task_is_throttled` | `CONFIG_SCHED_CORE` | core-sched 查询与 throttle 状态一致 | core rq lock、读写同步 | SCHED_CORE/throttle tests | BLOCKED | kernel reviewer |

## 非 callback 但强制覆盖的外部生命周期

| 路径 | 必须定义的行为 | 最低测试与证据 | 当前判定 |
|---|---|---|---|
| fork/exec/exit | policy 继承、reset-on-fork、ABI 和资源释放 | UAPI + stress | BLOCKED |
| wake/sleep/signal | task state、唤醒抢占、stop/continue | lifecycle stress | BLOCKED |
| PI | 高优先级 RT/DL 任务不被无界阻塞 | rtmutex 专项分析 | BLOCKED |
| cgroup/NOHZ | 资源控制、tickless CPU 预算来源 | 配置/guest matrix | BLOCKED |
| affinity/migration/hotplug | task 不丢失、CPU 下线安全、非法迁移有 errno | SMP/hotplug loop | BLOCKED |
| throttling/recovery | budget=0、replenish、过载和回退 | fault injection | BLOCKED |

## 交付前强制附件

- RFC 评审意见和签字人身份
- Linux 6.6.0-rt15 精确源码/patch/config 哈希
- 默认关闭和 opt-in 的 exact build
- KUnit、lockdep、KASAN/KCSAN（可用时）原始日志
- x86_64 QEMU 启动和策略 readback 证据
- ARM64/RISC-V 支持范围与构建/启动证据
- 真实目标机 FIFO/RR/DEADLINE 对照和独立 review
- 回退镜像、禁用方法和失败 attempt

在这些附件齐全前，矩阵保持 `BLOCKED`，不允许把 M2 或 M3 标记为完成。
