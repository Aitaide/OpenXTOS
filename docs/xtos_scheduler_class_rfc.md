# XTOS sched_class 集成 RFC 候选

状态：RFC 候选，未批准实施。当前 Linux 7.3.1，本次仅 `source_inspection_only`，不提供 build、guest 或 physical 验收。M0/M1 物理目标机 go/no-go 未通过，M2 保持未开始。PREEMPT_RT 已内置，旧 RT patch 与 `localversion-rt` 只属于历史基线。

## 目的和决策门

当前 `kernel/linux/kernel/sched/xtos_sched.c` 是可选的优先级队列 helper，不是当前 Linux `sched_class`，不参与 Linux 的任务选择路径。本文是进入 M2 前的审查材料，不是已实现功能声明。M0 真实 workload、物理 PREEMPT_RT 基线和预注册收益门未关闭前，结论只能是 `BLOCKED`，不得修改调度核心。

进入实现前必须同时具备：

1. `m0_gate.py` 对真实目标机 FIFO/RR/DEADLINE 证据给出可复核结果。
2. 由负责人登记的 workload、WCET/budget、CPU/IRQ 放置和回归门槛完整存在。
3. 独立 reviewer 确认 Linux 原生策略存在可复现缺口，且不是把 DEADLINE root-domain 准入拒绝误判为性能缺陷。
4. 内核 scheduler/PREEMPT_RT 维护者审查本文和生命周期矩阵。

若原生策略满足需求，决策为停止专用 sched_class，转而交付 `libxtos-rt`、CPU/IRQ 配置和诊断工具。

## 候选类位置和范围

候选顺序仅为待验证假设：

```text
stop > deadline > rt > xtos > fair > ext > idle
```

当前 SCHED_DATA 的实际段次序为 stop > dl > rt > fair > ext > idle，尚无 XTOS 类。ext 受 `CONFIG_SCHED_CLASS_EXT` 控制，active-class 遍历还受 scx 状态影响；上述插入位置仅是候选，必须审查 fair/ext 切换和 fast path。

XTOS 只服务显式选择的任务；普通、管理、IRQ、`SCHED_FIFO/RR` 和 `SCHED_DEADLINE` 任务继续由 Linux 原生类管理。XTOS 不得抢占更高类，不改变既有 Linux 类的语义。空 XTOS 队列必须回退到下一类。

当前设计采用固定 CPU 分区和有界任务预算。用户态 `partitioned.py` 是逻辑参考模型，不是 Linux SMP 模拟器；其结果不能直接证明内核类的时序或硬件收益。固定日历 reservation 也不等同于 Linux CBS/SCHED_DEADLINE。

## 必须审查的内核边界

| 边界 | 实施前问题 | 必须证据 |
|---|---|---|
| `pick_next_task`/类顺序 | 链接顺序和空队列回退是否真实生效 | exact build、guest readback、KUnit |
| rq lock | 每个 enqueue/dequeue/pick 的锁、IRQ 和抢占上下文是什么 | lock table、lockdep |
| PI | XTOS 任务持锁时更高 RT/DL 任务如何避免无界阻塞 | PI 专项测试和拒绝边界 |
| tick/runtime | 预算记账、节流 timer、取消和退出顺序是否有界 | KUnit、压力、故障恢复 |
| affinity/migration | 分区任务能否迁移，拒绝和 readback 的 errno 是什么 | affinity race、CPU offline |
| cgroup/NOHZ | 控制器和 tickless CPU 的供给语义是什么 | 配置矩阵和 guest 测试 |
| fork/exec/exit | policy、reset-on-fork、task lifetime 如何处理 | UAPI 生命周期测试 |
| recovery | 参数错误、队列损坏、CPU offline、模块/镜像回退如何保证启动 | 故障注入和 fallback image |

内核热路径不得分配内存、做 I/O、运行模型推理或依赖无界容器；默认配置必须关闭。

## UAPI 和安全边界

不新增架构 syscall number，不私自复用 `sched_attr` 保留字段，不在 ABI 评审前固定 policy number。需逐项定义：

- `sched_setattr/getattr` 的字段、版本和 32/64 位布局
- 权限、CAP_SYS_NICE、非法组合及 errno
- fork、exec、reset-on-fork、线程组和 signal 行为
- affinity、CPU hotplug、deadline/runtime/period 的原子更新
- 用户态 readback 必须证明实际策略，不得把请求成功当作 class 生效

失败必须保持旧 Linux policy 或明确返回错误；不得静默降级后报告“XTOS 已启用”。

## 完整 callback 集成检查表

目标内核 callback 名单及锁/上下文分析见 [lifecycle matrix](xtos_scheduler_lifecycle_matrix.md)。该表从当前树 `kernel/linux/kernel/sched/sched.h` 的 `struct sched_class` 提取字段，并由 `tests/test_sched_class_hooks.py` 自动比较，避免 RFC 只写概念生命周期而漏掉实际 hook。7.3.1 的 `pick_task`、`balance` 和 affinity/migration 字段均无 CONFIG_SMP 声明 guard；运行语义仍需按 UP/SMP 验证。仅下列两项 callback 有声明 guard；`CONFIG_UCLAMP_TASK` 控制的是非 callback 字段。`pick_next_task` 是 core 路径名，不再是 struct callback；旧 `check_preempt_curr` 对应当前 `wakeup_preempt`。

当前 callback 共 30 项：

- 无条件：`enqueue_task`、`dequeue_task`、`yield_task`、`yield_to_task`、`wakeup_preempt`、`balance`、`pick_task`、`put_prev_task`、`set_next_task`、`select_task_rq`、`migrate_task_rq`、`task_woken`、`set_cpus_allowed`、`rq_online`、`rq_offline`、`find_lock_rq`、`task_tick`、`task_fork`、`task_dead`、`switching_from`、`switched_from`、`switching_to`、`switched_to`、`get_prio`、`prio_changed`、`reweight_task`、`get_rr_interval`、`update_curr`
- CONFIG_FAIR_GROUP_SCHED：`task_change_group`
- CONFIG_SCHED_CORE：`task_is_throttled`

需逐项审查实际签名：dequeue 返回 bool；pick 接收 rq_flags、允许 RETRY_TASK 重试；put_prev 接收 next；get_prio/oldprio 使用 u64；reweight 接收 const load_weight。

上述清单只是结构覆盖，不等于实现已支持；当前所有 callback 仍为 BLOCKED，M0 关闭前不得进入 M2。

## 实施顺序和否决条件

1. RFC 和生命周期矩阵评审通过。
2. 默认关闭的静态骨架；不支持安全卸载，不可卸载，不做 module 形式的 sched_class。
3. 单 CPU QEMU 功能测试：类顺序、policy readback、预算、回退、普通任务不变。
4. PREEMPT_RT lockdep/KASAN/KCSAN（可用时）和调度压力测试。
5. SMP、PI、affinity、migration、hotplug、NOHZ、cgroup 和 recovery。
6. 重新在真实目标机与 FIFO/RR/DEADLINE 做同条件对照。

以下任一条件成立即停止实施并回到模型/原生策略：M0 未关闭、无独立审查、PI/lifecycle 关键项无解、原生策略已满足需求、或无法提供可回退的启动镜像。

## 当前结论

`BLOCKED_BEFORE_M2`。本文没有声称 sched_class 已实现，也没有提供硬实时或 superiority 结论。
