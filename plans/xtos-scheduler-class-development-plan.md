# XTOS 专用调度类开发计划

> **For Hermes:** 按阶段实施；每项内核改动先做规格符合性审查，再做代码质量审查。未经阶段退出条件批准，不进入下一阶段。

**目标：** 在保留 Linux PREEMPT_RT 及原生调度类的前提下，为明确选择加入的 XTOS 周期任务实现可测、可回退的内核调度策略；只有在裸机对照证据达到预登记门槛后，才声称它优于 Linux 原生策略。

**架构：** XTOS policy 作为独立 Linux `sched_class`，初始候选次序为 `stop > deadline > RT > XTOS > fair > idle`，不接管未选 XTOS policy 的任务。先建模并与 `SCHED_FIFO/RR`、`SCHED_DEADLINE` 对照，再按单 CPU、SMP/PREEMPT_RT、硬件验证逐步集成；任何阶段未证明用户价值或安全性都可以停止并使用 Linux 原生调度。

**技术栈：** Linux 6.6.0-rt15 / PREEMPT_RT、内核 C、Kconfig/Kbuild、sched_attr UAPI、KUnit/内核自测、QEMU、裸机 cyclictest 与 XTOS 周期 workload trace。

---

## 当前状态与执行约束

执行记录：pre-closeout 工具候选曾通过 25 项 preflight、67 项相关测试及 538 项维护测试，结果绑定 `.build/scheduler-m0/preflight-final-accepted-attempt-e5c58bfb/report.json`；后续收尾候选通过 69 项聚焦、45 组 540 项维护测试（含 AI11、零跳过），结果绑定 `.build/scheduler-m0/m0-final-accepted-attempt-2f3afac2/report.json`。这些均为历史候选，不能覆盖本轮新增源码。当前收尾候选的完整维护结果以验收台账和 attempt 报告为准。本轮按用户“自己开发并完成”的授权先行开发 M1 用户态参考模型工具，明确其整数逻辑时间、有限 DBF 诊断及非 Linux CBS 边界；阶段收益门尚未通过，M2 内核调度类未开始。M0 产品/实机 go-no-go 仍未完成：缺真实任务契约、预算依据、预注册收益门槛及当前候选目标机原生策略对照。仓库 2026-10-10 H3C 物理 RT 记录只作历史支持证据。见 [M0 闭环清单](../docs/scheduler-workloads/m0-closure.md)、[目标输入模板](../docs/scheduler-workloads/m0-target-intake.json)、[参考模型](../docs/scheduler-workloads/m1-model-v1.md)。本轮工具验证以 `.build/scheduler-m1/candidate.json` 和 `.build/scheduler-m1/acceptance.json` 为准，不绕过实机及阶段收益门进入 M2。

- 计划基线为公开 OpenXTOS v1.0.1 source SHA `3894b4f90ba48e05061019ce6603929feced48b2`。唯一开发目录 `/root/xtos-development/xtos` 当前 HEAD 可能比公开 tag 更新；开工时必须重新记录 HEAD、clean/dirty 状态、内核 SHA 和 patch SHA，并确认当前活动分支及目标版本。
- 现有 `kernel/linux/kernel/sched/xtos_sched.c` 是辅助优先级队列，不是 scheduler class；原审计 `docs/scheduler_integration_audit.md` 是现状依据。
- `docs/scheduler_design.md`、`docs/scheduler_implementation.md`、`docs/scheduler_test_report.md` 含过期的“已实现主调度器/系统调用/硬实时目标”叙述。先标记历史或纠正事实，不得照抄为当前规格。
- 当前 Linux 6.6 树没有 sched_ext；若选择升级/移植 sched_ext，另建计划，不能混入本计划无界扩张。
- 构建必须使用独立 `BUILD_ROOT`/Kbuild 输出目录；不得覆盖 `kernel/linux/.config` 或改动用户既有工作。完整系统脚本可清理 `$BUILD_ROOT` 的 staging/rootfs，运行前核实该目录确为本次独占的可重建产物目录。
- 没有物理性能结论前，只报告功能/模型/QEMU验证；不写“更好于 Linux”“硬实时保证”“<10us”之类未经证实指标。
- 所有 RT benchmark 都不得静默改 BIOS、电源 governor、IRQ routing、swap 或 watchdog。变更需单独授权并保存前后配置证据。

## 里程碑和退出决策

| 阶段 | 交付 | 可进入下一阶段的条件 |
|---|---|---|
| M0 规格与基线 | workload、指标、Linux 对照和当前状态清单 | [M0 闭环清单](../docs/scheduler-workloads/m0-closure.md)的外部输入已由负责人填写；真实任务契约和收益/回归门槛预注册；目标裸机 PREEMPT_RT 上两种 Linux 原生策略实测并由独立审查；若原生策略无缺口则形成停止定制调度类的决策 |
| M1 可执行算法模型 | trace replay、准入/预算/过载模型及对照报告 | 所有模型性质测试通过；XTOS 候选对目标 workload 有可测收益或独有必要能力 |
| M2 单 CPU 内核原型 | 默认关闭的 XTOS sched_class、ABI、生命周期/回退测试 | 静态顺序正确；核心路径、预算、PI/安全边界审查通过；x86 QEMU 完整系统可启动 |
| M3 SMP + RT | affinity/migration/hotplug/NOHZ/cgroup/PI 完成 | stress、lockdep 与可用 sanitizer 测试通过；不出现饿死/卡死/预算失控；多架构构建通过 |
| M4 裸机对照 | 可复现 benchmark 和原始 traces | 目标机重复测试达到预先登记的收益与无回归门槛；独立评审通过 |
| M5 实验发布 | opt-in 包、部署/回滚指南、支持矩阵 | 用户能确认实际策略生效且可回退；所有性能声明链接原始证据并限定 workload/硬件 |

M0/M1 若 Linux `SCHED_DEADLINE`/FIFO 已满足目标，结论为“不开发 sched_class”，把收益交付到 libxtos-rt 配置、CPU 隔离、IRQ 放置或诊断工具。该停止决定是有效交付，不算开发失败。

## 任务清单

### Task 1：固定仓库状态和事实基线

**Objective:** 让后续设计基于准确、可追溯的源码和构建状态。

**Files:**
- Read: `docs/scheduler_integration_audit.md`
- Read: `docs/system_build.md`
- Read: `kernel/patches/xtos-linux-6.6.patch`
- Read: `kernel/linux/kernel/sched/xtos_sched.c`
- Read: `kernel/linux/kernel/sched/xtos/Kconfig`
- Create: `docs/scheduler-class-baseline.json`

**步骤：**
1. 记录 `git rev-parse HEAD`、branch、`git status --porcelain`、Linux commit/tag、PREEMPT_RT patch SHA、XTOS patch SHA。
2. 确认 Linux 的 `__pick_next_task()`、`struct sched_class`、链接类顺序、sched_attr 校验代码位置和当前 class set。
3. 对照辅助实现及内核客体自检，明确当前配置/build/boot/module/test各自真实状态。
4. JSON 只记绝对或仓库相对文件、commit/hash、命令、证据路径、状态；不从旧日志继承未重新验证的成功项。

**验收：** baseline 可由脚本读取并与 Git/source 文件哈希匹配；不会把 object compile 或 CONFIG=y 描述成 sched_class 接管。

### Task 2：修正历史设计文档的状态标记

**Objective:** 防止旧设计图/状态被误读为已实现代码。

**Files:**
- Modify: `docs/scheduler_design.md`
- Modify: `docs/scheduler_implementation.md`
- Modify: `docs/scheduler_test_report.md`
- Read: `docs/scheduler_integration_audit.md`

**步骤：**
1. 写文档测试/检查脚本，断言三份文档开头存在“历史设计/过期状态，当前实现见 scheduler integration audit”的显著提示。
2. 先运行检查并确认失败。
3. 在保留历史细节的前提下加状态提示，删除任何会被读成当前性能承诺的醒目摘要，但不销毁历史证据。
4. 运行检查和 `git diff --check`。

**验收：** 三份旧文档仍可追溯，但首页摘要不再把 15 级优先级、EEG/GPU scheduler、系统调用或硬实时指标写成当前功能。

### Task 3：定义目标工作负载与 Linux 原生基线

**Objective:** 用实测确认 Linux 原生策略存在什么具体、可量化的缺口。

**Files:**
- Create: `docs/scheduler-workloads/README.md`
- Create: `docs/scheduler-workloads/<workload>.json`
- Create: `tools/xtos-sched-bench/` workload runner、trace schema 与 README
- Test: `tests/test_sched_periodic.py`、`tests/test_sched_bench_runner.py`、`tests/test_sched_trace_parser.py`、`tests/test_sched_preflight.py`

**步骤：**
1. 选择一个有真实需求负责人的代表性周期 workload；不用空泛的“BCI/AI/AR”标签代替契约。定义 release、period、deadline、WCET/预算来源、CPU、IRQ/设备输入、优先级和 overload 结果。
2. 实现稳定的 workload generator 与 monotonic-clock trace schema；每条 job 记录 release、实际 runnable、start、finish、deadline、budget、CPU、migration、miss reason。
3. 对同一个 workload 分别运行 PREEMPT_RT `SCHED_FIFO/RR` 和 `SCHED_DEADLINE`。保存内核配置、参数、硬件、CPU/IRQ placement、运行时长、全部原始 trace 和 runner SHA。
4. 同一目标至少重复 10 轮并先登记延迟/违约指标、允许回归幅度与置信区间方法；先测裸机，不把 QEMU 作为延迟 baseline。

**验收：** 无漏 job/重复 ID；monotonic 时间线一致；解析器能从 raw trace 重算所有汇总值；Linux 原生策略是否有缺口得到具体复现。若无缺口，停止 M0/M1 并形成“使用 Linux 原生策略”的决策记录。

### Task 4：确定 XTOS policy 合同和 admission 模型

**Objective:** 在内核接入前定义并验证可执行的调度语义。

**Files:**
- Create: `docs/xtos_scheduler_class_design.md`（上层架构已定，开工时补 workload 结果）
- Create: `tools/xtos-sched-model/`
- Create: `tests/test_xtos_sched_model.py`
- Modify: `docs/scheduler-workloads/README.md`

**步骤：**
1. 定义每任务 runtime/deadline/period、per-CPU partition、budget debit/replenish、tie-break、yield/sleep、missed deadline 和 overload semantics。初始 admission 模型只接受独立周期且 deadline=period；deadline<period 必须先有 DBF/供给界分析与反例测试，不得仅凭总利用率准入。纳入更高 Linux DL/RT 类、IRQ、阻塞和调度开销对可用供给的影响。
2. 以整数纳秒实现 reference model；不在 Linux 内核路径使用浮点或动态无界容器。
3. 写模型测试：非法参数、恰好准入、超限拒绝、并发 admission 不超卖、任务阻塞/唤醒、多个相同 deadline、budget exhaustion/replenish、过载和迁移失败。
4. 对照 EDF/CBS 数学性质和真实 trace；确定 workload 是否真的需要 XTOS policy 特有行为。
5. 评审 UAPI：检查 Linux 6.6 所有 `SCHED_*` policy 值及保留范围、`sched_attr` 字段、兼容 ABI、权限和 reset/fork 语义。无编号/ABI方案前不加常量或 syscall。

**验收：** 所有模型性质可重复；参数与计时单位有文档；审查明确说明已有 Linux `SCHED_DEADLINE` 为什么不能满足目标，或记录 no-go。

### Task 5：形成内核集成 RFC 与 lock/lifecycle 表

**Objective:** 在代码前评估 sched_class 集成的完整内核工作量与安全责任。

**Files:**
- Create: `docs/xtos_scheduler_class_rfc.md`
- Create: `docs/xtos_scheduler_lifecycle_matrix.md`
- Review: `kernel/linux/kernel/sched/sched.h`
- Review: `kernel/linux/kernel/sched/core.c`
- Review: `kernel/linux/kernel/sched/rt.c`
- Review: `kernel/linux/kernel/sched/deadline.c`
- Review: `kernel/linux/include/asm-generic/vmlinux.lds.h`
- Review: `kernel/linux/include/uapi/linux/sched.h`

**步骤：**
1. 为 sched_class 的每一个当前内核 hook 记录所需行为、rq/pi lock context、可睡眠性、task lifetime 和测试。
2. 为 fork/exit、wake/sleep、enqueue/dequeue、policy 变更、yield、tick、PI、cgroup、affinity/migration、hotplug、NOHZ、SMP balance、core scheduling、信号、throttle、empty queue 和 recovery 填满 lifecycle matrix。
3. 明确类顺序候选 `stop > dl > rt > xtos > fair > idle`，分析 core-scheduling/fast paths 和回退；要求链接符号/真实 guest readback 验证。
4. 选择原子 ABI：优先现有 `sched_setattr/getattr`；提出策略编号分配、参数有效性、权限、errno、fork/exec、32/64-bit、旧用户程序行为。由内核维护评审确认后才实施。
5. 将辅助组件的队列 API 与内核 rq 实际队列分离评估；不能将现有 `xtos_rq` 与 Linux `struct rq` 混为一体或双重入队。
6. 由至少一名熟悉 scheduler core/PREEMPT_RT 的 reviewer 签字。若 hook/lifecycle 任何关键项无安全设计，返回模型阶段，不写集成代码。

**验收：** RFC 逐项覆盖所有当前 hook、policy 和退回路径；维护成本、Linux rebase 责任人、升级测试明确。

### Task 6：创建默认关闭的 sched_class 骨架

**Objective:** 不改现有默认调度行为，证明新的静态类可正确构建并呈现在指定次序。

**Files:**
- Modify: `kernel/patches/xtos-linux-6.6.patch`
- Modify: `kernel/linux/include/uapi/linux/sched.h`（仅完成 ABI 评审后）
- Modify: `kernel/linux/kernel/sched/core.c`
- Modify: `kernel/linux/kernel/sched/sched.h`
- Create/Modify: `kernel/linux/kernel/sched/xtos_class.c`
- Modify: `kernel/linux/kernel/sched/Makefile`
- Modify: `kernel/linux/kernel/sched/xtos/Kconfig`
- Test: 新增 scheduler KUnit/自测文件

**TDD 步骤：**
1. 写失败的 KUnit/自测，要求 policy 创建的 task 报告 XTOS class；default/RT/DL task 保持原 class；空 XTOS 队列时公平类仍能运行。
2. 用新独立 `BUILD_ROOT` 运行 focused test，确认新预期失败且原系统已有配置不变。
3. 添加静态 sched_class 定义、默认关闭 `bool XTOS_SCHED_CLASS` Kconfig 与最少 policy 映射；不修改旧 helper 以让它虚报接管。
4. 测试 class 链接顺序、policy/getattr 读回、普通任务不变和空队列回退。
5. 构建 RT-enabled 与 XTOS-class-disabled 两套内核，并对 `vmlinux`/class symbols 做可重复检查。

**验收：** `CONFIG_XTOS_SCHED_CLASS=n` 与旧配置行为不变；开启时仅显式选择的测试任务进入 XTOS 类；基础骨架不能作为实时性能证据。

### Task 7：实现单 CPU 队列、预算与任务选择

**Objective:** 在单 CPU、固定 affinity 的实验配置中实现通过模型验证的选择策略。

**Files:**
- Modify: `kernel/linux/kernel/sched/xtos_class.c`
- Modify: `kernel/linux/kernel/sched/core.c`
- Modify: scheduler KUnit/自测
- Modify: `tools/xtos-sched-bench/` trace instrumentation

**TDD 步骤：**
1. 写失败测试：EDF 次序/tie-break、enqueue/dequeue、sleep/wake、runtime debit、budget=0 throttle、replenish、deadline miss 记账和 fair fallback。
2. 先在单 CPU QEMU kernel 自测确认失败原因确为未实现行为。
3. 实现固定容量或有证明界限的 runqueue 数据结构；hot path 不分配内存、不输出日志、不做除法以外未经 profiling 的重工作。
4. 实现预算计时与 throttle timer，明确 hrtimer 锁上下文、并发取消和退出销毁顺序。
5. 运行 KUnit、PREEMPT_RT lockdep/调度 stress 与 exact-image QEMU 自检。

**验收：** 容量边界、运行队列计数、budget 和回退恒等式始终成立；任何负载下 idle/fair 管理任务仍可进展；未证明 scheduler worst-case runtime 之前不得发布硬实时声明。

### Task 8：完成 UAPI 与用户态控制面

**Objective:** 提供标准、权限明确且可读回的 opt-in policy 接口。

**Files:**
- Modify: `kernel/linux/include/uapi/linux/sched.h`
- Modify: `kernel/linux/kernel/sched/core.c`
- Modify: `sdk/libxtos-rt/include/xtos/rt.h`
- Modify: `sdk/libxtos-rt/src/` 对应 scheduler wrapper
- Test: `sdk/libxtos-rt/tests/` 与 `tests/test_sched_uapi.py`

**步骤：**
1. 以测试固定 `sched_setattr/getattr` 序列化、policy 变更、权限、无效组合、`errno` 和 attr size 向前/向后兼容行为。
2. 实现最小 API，默认不更改调用线程 policy；切换只有调用者明确请求，读取当前策略可确认实际状态。
3. 保持旧 libc/kernel 无法识别 XTOS policy 时明示 `EINVAL/EOPNOTSUPP`，不静默降级为“XTOS 已启用”。
4. 测试 fork/exec/reset-on-fork 和控制面失败时线程留在旧 Linux policy。

**验收：** 新旧用户程序兼容；无权调用显式失败；strategy readback 与实际 `p->sched_class` 一致；no silent fallback claims。

### Task 9：SMP、安全语义与恢复

**Objective:** 让选择加入的任务在多核 PREEMPT_RT 环境下不破坏 rq 和系统生命周期。

**Files:**
- Modify: `kernel/linux/kernel/sched/xtos_class.c`
- Modify: `kernel/linux/kernel/sched/core.c`
- Modify: `kernel/linux/kernel/sched/rt.c` / `deadline.c` only where RFC justifies
- Modify: lifecycle tests and `scripts/verify_system.sh`

**步骤：**
1. 为所有共享状态固定锁序，记录每把锁的 raw/spin/rtmutex 类型、irq/preempt 状态和 nested rq 关系。
2. 写失败测试覆盖 affinity change、迁移、CPU online/offline、任务退出、policy 变更竞争和 sched-domain interaction。
3. 按 RFC 实施 select_task_rq、migration callbacks、rq online/offline、balance 与容量回收；不可支持路径安全拒绝或回退。
4. 对 PI 依赖作专项验证；不能让更高优先级 Linux RT task 被 XTOS 锁持有者无界阻塞。明确 sched_class 是否支持 PI 及不足时拒绝的 ABI。
5. 测试 cgroup RT/CPU controller 交互、NOHZ/tickless、SCHED_CORE（开启/关闭）、IRQ threads、过载和恢复。
6. 使用 KASAN/KCSAN（可用时）、lockdep、RCU stall detector、CPU hotplug loop、fork/exit storms 和长时并发 stress；保存 trace 与完整配置。

**验收：** 无 lockdep/KASAN/KCSAN 已知缺陷；task 不重复或遗失；offline CPU 不遗留 runnable task；关停/过载可在有界时间内恢复；不受支持行为有稳定 errno。

### Task 10：跨架构构建与 QEMU 功能门

**Objective:** 验证精确 build artifact 的配置和状态转换，不将虚拟时序误作硬件性能。

**Files:**
- Modify: `scripts/build_kernel.sh`
- Modify: `scripts/build_system.sh`
- Modify: `scripts/verify_system.sh`
- Modify: `scripts/verify_guest_arm64.sh`
- Modify: `scripts/verify_guest_riscv64.sh`
- Modify: `docs/system_build.md`

**步骤：**
1. 为 opt-in 与 opt-out 两种配置构建到独立目录，记录 `.config` SHA、compiler、source SHA、kernel SHA 和 image SHA。
2. x86_64 QEMU 启动 exact image；guest 检查 `/sys/kernel/realtime=1`、运行配置、class order、XTOS task policy/readback、普通任务/Linux RT/DL 任务边界、budget exhaustion 和恢复。
3. ARM64 和 RISC-V64 至少完成源码编译及 QEMU 启动/ABI 行为测试；若 Linux 6.6 RT 架构支持、工具链或测试资源不全，标记不支持而非移除 PREEMPT_RT。
4. 同时验证 XTOS class disabled 的默认镜像完整启动。

**验收：** opt-in 与 opt-out 的结果均可复现；QEMU 结果仅列为功能/启动证据，不作为延迟 superiority evidence。

### Task 11：目标硬件 A/B 性能和可靠性评测

**Objective:** 在相同条件下判断目标策略是否优于 Linux 原生 policy。

**Files:**
- Modify/Create: `tools/realtime-test/xtos_sched_cyclictest.sh`
- Modify/Create: `tools/xtos-sched-bench/`
- Create: `docs/scheduler-results/<device>-<date>/`
- Test: `tests/test_sched_trace_parser.py`

**步骤：**
1. 预先冻结指标、主指标、最小有意义收益和不回归门槛；不在看到结果后改口径。
2. 同一台裸机交替运行 FIFO/RR、DEADLINE、XTOS 组，配置与工作负载完全一致；至少 10 次有效运行，报告全部样本、失败样本和统计区间。
3. 运行 idle、代表 workload、CPU/memory/I/O stress、中断负载和超载恢复场景；记录 deadline misses、p50/p95/p99/p99.9/max、吞吐、CPU/调度开销、温度/频率、功耗（传感器可用时）。
4. 运行既有 24h cyclictest 基础验证和代表 workload 的长时任务验证；提供 JSON/raw traces、hardware inventory、BIOS/内核配置、仪器及分析器版本。
5. 由与实现者独立的 reviewer 复核原始 trace 和计算脚本。

**验收：** 只允许作 workload/hardware/version 条件化陈述。若优越性未达到门槛、有明显公平性/吞吐/功耗退化或置信区间重叠，结果应记录为“不优于/证据不足”，不继续扩大声明。

### Task 12：实验发布与回滚文档

**Objective:** 让 XTOS class 初始默认关闭、可观察、可禁用且发布范围明确。

**Files:**
- Modify: `docs/XTOS_Technical_Specification.md`
- Modify: `docs/system_build.md`
- Create: `docs/xtos_scheduler_class_admin.md`
- Modify: `STATUS.md`
- Modify: `README.md`

**步骤：**
1. 文档列出已验证内核 SHA、配置、目标 workload/CPU、完整构建与回退内核、启用方式、policy readback、故障诊断、禁用方法和限制。
2. 默认 defconfig 保持 XTOS class disabled，除非发布评审另行决定。
3. 仅把已经过门槛的 workload、CPU/架构和结果写进公开性能说明；区分编译、QEMU、实机和物理设备验证。
4. 将过期 scheduler 设计文档标记为历史，并链接当前设计、审计和实测报告。
5. 跑 `scripts/test.sh`、全量构建、精确镜像 QEMU 验证、ABI/跨架构测试；保存输出到 `.build/` 证据树，不把大日志/镜像放进源码仓库。

**验收：** 新机器可以仅按文档构建默认 Linux PREEMPT_RT 镜像；用户可确认策略状态并回退；任何性能声明均有对应报告。

## 建议初始时间表

- M0：1–2 周。Linux baseline/workload 评审、老文档更正、需求冻结。
- M1：2–3 周。trace runner、Linux A/B 原生策略与算法模型。
- M2：3–6 周。RFC、UAPI 审查和单 CPU kernel class prototype；时间受 scheduler reviewer availability 影响。
- M3：4–8 周。SMP、PI、迁移、hotplug、cgroup/NOHZ 及测试闭环。
- M4：至少 2–4 周测试窗口，另需真实目标设备可用；长时间测试与环境排期可能延长。
- M5：1–2 周。独立复核、实验发布、回滚演练。

这是依赖条件满足后的粗估，不是交付承诺。最主要不确定项为定制调度策略能否胜过 `SCHED_DEADLINE`、内核 scheduler/PREEMPT_RT 维护审查资源、物理硬件和可控中断/固件条件。

## 全局测试与质量门

每阶段至少运行针对该阶段的 focused tests。所有 kernel 改动须运行：

```sh
scripts/prepare_linux.sh
BUILD_ROOT="$PWD/.build/scheduler-<stage>-<config>" JOBS=2 scripts/build_system.sh
```

系统级 exact image 验证：

```sh
BUILD_ROOT="$PWD/.build/scheduler-<stage>-<config>" \
  QEMU_TIMEOUT=180 scripts/verify_system.sh
```

涉及用户态 SDK/测试入口时运行：

```sh
scripts/test.sh
```

每阶段检查：

```sh
git diff --check
git status --short
sha256sum "$BUILD_ROOT/kernel-build/.config" \
  "$BUILD_ROOT/kernel-build/arch/x86/boot/bzImage"
```

`build_kernel.sh` 会重新配置指定 output，并 `prepare_linux.sh` 会验证源码/补丁版本；不要对已有重要 `$BUILD_ROOT` 直接运行，先确认目标目录归本任务独占。不要修改或覆盖 `/root/xtos-development/xtos/.build/openxtos-work` 发布快照。

## 风险、依赖与未决决定

- **关键 go/no-go：** 选定 workload 的 Linux 原生 FIFO/RR 或 DEADLINE 是否已经足够？M1 前不应作肯定假设。
- **UAPI：** XTOS policy number、`sched_attr` 参数兼容性需 Linux 内核维护者级评审；不能凭本地测试宣布 ABI 固化。
- **调度语义：** per-CPU EDF/CBS 是否比 Linux `SCHED_DEADLINE` 更适合 XTOS workload，必须测量和论证。
- **类优先级：** 当前候选让 Linux DL/RT 高于 XTOS；如果需求认为这无法满足，就要重新做系统级风险分析，而非静默重排 class。
- **优先级继承：** scheduler policy 和 PI 交互复杂，任何遗漏都会引入 inversion 或优先级计算错误。
- **物理平台：** 没有明确裸机 target 和可控 BIOS/IRQ/固件时无法完成性能 superiority 门槛。
- **维护成本：** scheduler core patch 每次 Linux/RT rebase 都须重验；需指定长期维护责任人。
- **sched_ext：** 上游 Linux 6.12+ 提供 sched_ext；XTOS 6.6 RT 如欲使用，另立升级/移植和 PREEMPT_RT 兼容性项目，不与直接 sched_class 方案同时混做。
