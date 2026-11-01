# XTOS 专用调度类开发设计

状态：设计候选 v0.1，尚未实现
当前基线源码：Linux 7.3.1，PREEMPT_RT 主线内置，精确 source pin 和源码哈希见 `scheduler-class-baseline.json`。
本次为 `source_inspection_only`，不代表 build、guest 或 physical 验收。M0/M1 物理目标机 go/no-go 未通过，M2 未开始。
历史参考：OpenXTOS v1.0.1 / Linux 6.6.0-rt15，提交 `3894b4f90ba48e05061019ce6603929feced48b2`；原 baseline 原字节保存在 `history/scheduler-class-baseline-6.6.0-rt15.json`。旧 RT patch 和 `localversion-rt` 不属于当前版本依赖。

## 1. 目标

在不移除 Linux 调度核心、PREEMPT_RT、`SCHED_DEADLINE`、`SCHED_FIFO/RR`、普通公平调度和 idle/stop 类的前提下，为有明确周期、预算、截止时间和 CPU 分区约束的 XTOS 任务实现独立调度策略。

“替换 Linux 调度器”在本设计中严格限定为：应用显式选择 XTOS 调度策略后，该任务由 XTOS 的 Linux `sched_class` 实现选取；未选择该策略的任务仍由原有 Linux 调度类管理。它不是用另一套内核取代 Linux scheduler core，也不是取消 Linux 调度类体系。

候选调度类次序：

```text
stop > deadline > RT (FIFO/RR) > XTOS > fair > ext > idle
```

当前 SCHED_DATA 实际段次序是 stop > dl > rt > fair > ext > idle，helper 未注册类。ext 的链接存在取决于 CONFIG_SCHED_CLASS_EXT，运行参与还取决于 scx 状态；候选必须审查 fair/ext 与 fast path。此位置使 stop、`SCHED_DEADLINE` 和 `SCHED_FIFO/RR` 任务保持高于 XTOS 类；XTOS 策略高于普通 fair 类。必须通过 Linux 链接脚本和运行时测试验证真实次序，不能仅凭源码声明推断。若工作负载要求 XTOS 抢占 Linux RT 类，须另立安全论证和需求评审，不纳入当前方案。

## 2. 当前实现事实

当前 M0 未关闭，M2 未开始；因此本文是设计候选，不代表当前实现。当前树的 `CONFIG_XTOS_SCHED` 是 tristate、默认关闭的可选辅助组件；`kernel/sched/xtos_sched.c` 提供独立的优先级队列和显式控制块 API；补丁注释明确说明它不是 `sched_class`。Linux 正常的任务选择路径没有调用 XTOS `pick_next_task_xtos()`，也没有将任务唤醒、入队、抢占、tick 或 CPU 迁移交给 XTOS。因此启用该配置、编出内核或加载模块，都不能证明 XTOS 正在调度 Linux 任务。

现有审计 `docs/scheduler_integration_audit.md` 记录：辅助组件编译和完整内核构建通过，但主调度集成和启动验证未完成。旧文件 `docs/scheduler_design.md`、`docs/scheduler_implementation.md`、`docs/scheduler_test_report.md` 中关于“已实现主调度器、系统调用、15 级实时优先级、EEG/GPU 调度”的说法与当前源码不符；这些内容不得作为实现依据或性能证据。实施本设计时应将它们标为历史/过期设计，保留审计追溯链。

## 3. 设计原则

1. 保留 Linux PREEMPT_RT 与全部既有调度类；XTOS 是选择加入的调度策略，不接管未选择的任务。
2. 不先改 `task_struct`，不新增架构 syscall number。优先扩展已有 `sched_setattr()` / `sched_getattr()` 路径和 `sched_attr` 参数；UAPI 数值、权限和 ABI 向后兼容性须先审计并经内核评审。
3. 内核路径只用有界操作；不做浮点、内存分配、文件/网络 I/O、模型推理或 EEG 分析。事件检测和策略计算位于用户态，内核只执行已验证的任务参数。
4. 没有 admission control、预算耗尽行为和过载策略，就不能声称提供实时保证。不能以 O(1) 队列、平均延迟或 QEMU `cyclictest` 代替此类证明。
5. 先证明 XTOS 策略对确定工作负载相对 Linux 原生策略有可重复的业务收益，再决定是否承担 sched_class 集成和长期 rebase 成本。
6. QEMU 用于功能、启动和状态迁移验证，不用于证明真实硬件延迟上界或调度器优越性。

## 4. 策略候选与准入

### 4.1 初始候选

先在用户态确定性模型中比较以下策略，不直接将旧 15 级优先级队列移入调度热路径：

- Linux `SCHED_FIFO/RR`：固定优先级参考。
- Linux `SCHED_DEADLINE`：EDF/CBS 参考。
- XTOS 分区任务候选：per-CPU EDF 排队，按绝对截止时间选择；每任务以 runtime/period 预算限制 CPU 使用，预算耗尽后节流至 replenishment。固定优先级/固定分区仅作为对照候选。

是否采用 XTOS 分区 EDF/CBS，须由 trace replay 和实机基线决定。Linux 已有 `SCHED_DEADLINE`，因此“也实现 EDF/CBS”本身不构成 XTOS 优势。只有当 XTOS 的明确任务契约、分区、过载/事件处理或可观测性在既定工作负载上带来可复现收益，才进入内核实现；否则采用 Linux 原生策略并停止定制 sched_class 阶段。

### 4.2 任务契约

候选 UAPI 使用 `sched_attr` 中经定义的 runtime、deadline、period 字段，并通过 affinity/cpuset 定义允许 CPU。初始契约至少包含：

- `runtime_ns > 0`
- `deadline_ns > 0`
- `period_ns >= deadline_ns`
- `runtime_ns <= deadline_ns`
- 可用 CPU mask 非空，所有 CPU online 且允许任务运行
- 单调时钟和预算耗尽/补充语义固定

现有字段是否足够表达 XTOS 的分区与事件模型，需在实现前做 ABI 审查。不要私自复用 `sched_attr` 保留字段或当前 Linux 7.3.1 中已有/保留的 policy ID。若标准字段不能完整表达契约，应先写 UAPI 提案并进行兼容性评审。

### 4.3 Admission control

按 CPU 分区执行原子化 admission。以下利用率条件仅是必要容量检查，不能作为一般约束截止期任务的充分准入证明：

```text
每个 CPU: Σ(runtime_i / period_i) <= U_xtos_cpu
0 < U_xtos_cpu < 1
```

当 `deadline < period` 时，利用率合格仍可能存在短窗口 demand overload；必须进行经验证的需求界函数（DBF）/供给界分析，并纳入更高调度类、IRQ、锁阻塞与调度开销。初始算法模型应先限制 `deadline = period` 的独立周期任务，其他模型默认拒绝，直到专门的可调度性分析及反例测试完成。即使 `deadline = period`，低于 Linux DL/RT 类的 XTOS 类也不能在有无界高优先级干扰时承诺截止时间。

`U_xtos_cpu` 不是预设性能承诺。它须由 IRQ/内核保留预算、目标硬件和长时间测试确定并有配置来源。校验任务参数、CPU mask、迁移和热插拔时，避免并发 admission 超卖；超限必须明确返回失败，不能先接纳再寄希望于运行时调度。若无法对共享中断、不可抢占临界区或外部设备延迟给出上界，必须把保证表述为有条件的实验结果，而非硬实时保证。

## 5. Linux 内核集成范围

Linux 7.3.1 的 `struct sched_class` 包含多项生命周期和 SMP 钩子；完整实现不止 `pick_next_task`。候选工作面包括：

- `kernel/sched/xtos_sched.c`：策略队列、预算、时钟、节流和每 CPU runqueue。
- `kernel/sched/sched.h`、`kernel/sched/core.c`：policy 映射、任务入/出队、状态改变、调度切换和属性更新。
- `kernel/sched/rt.c`、`kernel/sched/deadline.c`：与既有更高调度类、PI 和预算语义的边界审查；避免未经分析的行为改变。
- `kernel/sched/Makefile` 与 `kernel/sched/xtos/Kconfig`：新 sched_class 需要静态链接；从 tristate helper 改为依赖明确的 `bool` 内核选项，默认关闭，不能作为可卸载 module 实现。
- `include/asm-generic/vmlinux.lds.h`：类优先级链接顺序。
- `include/uapi/linux/sched.h`、调度 syscall 校验和用户态 SDK：新增策略常量及参数解释；实现前确认编号、errno、权限、fork/exec/reset-on-fork 和 32/64-bit ABI。
- `kernel/sched/debug.c`、tracepoints 或受控 debugfs：仅在证明必要时添加最小可观测性；不得让诊断接口成为未经保护的可变调度输入。
- `tests/`、`scripts/test.sh`、`scripts/build_kernel.sh`、`scripts/verify_system.sh`：集成可重复测试和实际 guest 自检。

选择 XTOS 策略的任务需有明确转换行为。未指定 XTOS 策略的任务、管理任务和启动任务保持原类。XTOS 类队列为空时必须正确回退到下一个 Linux 类。预算耗尽、无合法 CPU、CPU offline、参数变更失败和用户态退出均须定义行为。调度类编译进内核后不支持安全卸载；禁用策略应通过默认关闭配置和明确的运行时选择控制，并保留可启动的原生 PREEMPT_RT 基线内核。

### 生命周期覆盖清单

实现评审必须逐项说明并测试：fork/exit、wake/sleep、enqueue/dequeue、yield、抢占判定、`pick_next_task`、put/set next、tick/runtime 记账、nice/policy/priority 更新、PI/优先级继承、cgroup 语义、affinity 更新、CPU migration、CPU hotplug、NOHZ/tickless、SMP load balancing、core scheduling、signal/stop/continue、throttling、系统恢复和策略禁用。对不支持的功能明确拒绝或回退，不能留下隐式半支持状态。

## 6. 验证策略

### 6.1 功能与安全

- 对 admission 公式、EDF tie-break、budget 消耗/补充、超时、过载、节流和回退写模型级测试。
- 加入 KUnit/内核调度自测及用户态 `sched_setattr/getattr` ABI 测试；参数非法、权限不足、fork 继承、迁移、CPU offline 和竞争修改均须覆盖。
- QEMU 启动精确构建产物，检查 `uname`、`/sys/kernel/realtime=1`、实际 `CONFIG_XTOS_SCHED_CLASS=y`、策略读回、类选择和回退；至少测试 x86_64，之后测试 ARM64/RISC-V 构建与支持范围。
- 在 PREEMPT_RT 下用 lockdep、KASAN、KCSAN（工具链允许时）和压力自测覆盖并发路径；验证锁序和 rq 锁约束，不能在 rq 锁持有时睡眠或分配内存。
- 系统压力下同时运行更高优先级 DL/RT、XTOS 周期任务和 fair 负载，验证类次序、RT 不被 XTOS 抢占、预算耗尽不饿死 fair、关停/故障时系统可恢复。

### 6.2 基线和优越性

在同一台裸机、同一 BIOS/固件、内核配置、CPU/IRQ 亲和性、电源设置、温度/频率条件和输入 trace 下，对比：

1. 当前 Linux 7.3.1 + `SCHED_FIFO/RR`。
2. 当前 Linux 7.3.1 + `SCHED_DEADLINE`。
3. 候选 XTOS policy。

每个结果记录源 SHA、内核配置/镜像 SHA、硬件标识、CPU topology、IRQ placement、测试脚本与版本、运行时长、任务参数、负载、raw trace 和分析程序 SHA。至少观察 deadline miss、p50/p95/p99/p99.9/max release-to-run latency、job completion latency、吞吐、调度/锁开销、CPU 利用率、overload/recovery、功耗/温度影响。性能结论按预先登记的业务门槛和测量噪声判断，报告重复次数、置信区间和失败样本；不得只挑最好一轮或只报平均值。真实硬件长时间测试是性能结论的必要门槛；既有 24 小时 `tools/realtime-test/rt_cyclictest.sh` 可作为基础，但需新增与周期任务契约匹配的 workload benchmark，且不应静默修改主机电源或 BIOS 配置。

## 7. 开发阶段与退出条件

1. **规格/基线**：清理旧设计文档的现状冲突，冻结任务模型、代表性 workload 和量化收益门槛。若 Linux 原生策略已经满足需求，停止自定义 sched_class。
2. **可执行模型**：用户态模拟/trace replay 实现候选和原生策略对照；测试 admission、预算、过载、抖动和 CPU 分区。模型结果只作算法筛选，不作内核性能证明。
3. **内核单 CPU 原型**：在默认关闭配置下增加专用策略和最小 ABI；先验证类次序、单 CPU 生命周期、budget 和回退。不得先加入 EEG 事件学习、GPU 协调或预测模型。
4. **SMP 与 PREEMPT_RT**：处理多 CPU 锁、迁移、PI、cgroup、CPU hotplug、NOHZ 和系统恢复；完善 sanitizer/自测/QEMU。
5. **裸机对照**：使用注册的 workload 与两种 Linux baseline 测试；性能和稳定性通过评审后才能形成“对指定 workload 更好”的声明。
6. **实验发布**：默认保持关闭，公开限定支持的 CPU/内核/任务模型、禁用及回滚方法、trace 与已知限制。普通 Linux 任务继续使用 Linux 原生调度类。

## 8. 风险与决策

- Linux scheduler 是高耦合并发核心。新增类会形成长期维护分支，并在每次 Linux/RT rebase 时要求重新审查 class hooks、锁和 PREEMPT_RT 语义。
- `SCHED_FIFO/RR` 与 `SCHED_DEADLINE` 已提供可用 RT 模型；XTOS 必须有 workload-specific 证据，不能因“自有调度器”本身增加风险。
- 固定优先级/O(1) 查找不等于低调度延迟或正确的过载控制；数据结构复杂度不代表端到端保证。
- 目前目标硬件延迟和真实 OpenBCI acquisition 未验证；BCI/AI/GPU/医学安全均不能由该内核类推出。
- 当前 7.3.1 树含 `kernel/sched/ext/` 和 `CONFIG_SCHED_CLASS_EXT`，该选项依赖 BPF_SYSCALL、BPF_JIT、DEBUG_INFO_BTF，无显式 default y。源码存在不能证明已启用、已附着 BPF scheduler 或具备 PREEMPT_RT 性能；是否采用 sched_ext 需独立设计和实机比较。
- 完整 30 项 callback 及 guard 见 RFC/生命周期矩阵。当前 `pick_task` 是无条件 callback，core 的 `pick_next_task` 不是 callback；wakeup_preempt、switching_from/to、get_prio、reweight_task 和 bool dequeue、u64 oldprio、next/rq_flags 参数都需重新审查。

## 9. 参考资料

- Linux 当前基线审计：`docs/scheduler_integration_audit.md`
- PREEMPT_RT 验证边界：`docs/preempt_rt_validation.md`
- 当前系统构建与运行架构：`docs/system_build.md`
- 上游调度类结构：`kernel/linux/kernel/sched/sched.h`
- 调度类选取路径：`kernel/linux/kernel/sched/core.c`
- 链接顺序：`kernel/linux/include/asm-generic/vmlinux.lds.h`
- policy UAPI：`kernel/linux/include/uapi/linux/sched.h`
- 上游 PREEMPT_RT 原理：<https://docs.kernel.org/core-api/real-time/theory.html>
- 上游 `SCHED_DEADLINE`：<https://docs.kernel.org/scheduler/sched-deadline.html>
- 上游 sched_ext：<https://docs.kernel.org/scheduler/sched-ext.html>
