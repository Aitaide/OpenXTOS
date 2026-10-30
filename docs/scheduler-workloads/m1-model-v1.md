# 用户态调度参考工具 v1

状态：工具候选，最终验证以 `.build/scheduler-m1/acceptance.json` 中的候选哈希、独立审查和完整维护报告为准。M1 阶段的真实任务收益门尚未通过；M0 实机门仍未关闭，M2 内核调度类未开始。

实现路径：`tools/xtos-sched-model/model.py`、`tools/xtos-sched-model/replay.py`。只使用 Python 标准库，不设置调度策略，不启动负载，不改变 CPU/IRQ/BIOS。

## 可执行语义

四种模型都在一个逻辑 CPU 上执行独立、周期释放的串行任务。每个任务可积压多个 job，但只执行最早的未完成 job。释放区间为 `[offset_ns, horizon_ns)`，达到 horizon 后停止新释放，继续执行有限积压直到完成。完成时刻恰好等于 deadline 算按时。

- `edf`：无预算节流的抢占式 EDF；绝对 deadline 最早者先运行，平局按 task name、job index 决定。
- `fifo`：固定实时优先级，高数值优先；同优先级按进入 ready 队列的顺序运行。更高优先级到达可抢占，同优先级到达不抢占。
- `rr`：固定优先级，始终先运行最高优先级的 ready 任务；只在同优先级任务之间按 quantum 轮转。被更高优先级抢占或遇到无关释放时保留剩余时间片。
- `reservation_edf`：实验性的固定日历周期预算模型。每个任务在 `offset + k*period` 重置共享预算 runtime，不累积未用预算；所有积压 job 共享该预算，耗尽时等待下一周期。任务之间用 EDF 选择。这不是 Linux CBS，也不是 `SCHED_DEADLINE` 仿真。

时间边界先处理前一执行区间的完成/时间片耗尽，再加入按 release/name/index 排序的新释放。只在剩余工作因零预算被阻止时记 `budget_exhausted`；正常完成恰好用完预算不计节流。`budget_exhaustions` 是曾被节流的 job 数，不是节流事件总数。`replenishments` 表示该 job 实际执行跨越的预算 epoch 数减一；`replenishment_ns` 是它完成时使用的预算 epoch 起点。非预算模式的预算剩余/epoch 字段为 null。

## 准入和 DBF

原子准入只接受独立、隐式截止期（deadline=period）的任务预算；按每个 CPU 分别用精确分数检查 `sum(runtime/period) <= 1`。这是理想 full-CPU 模型，假设没有更高 Linux 调度类、IRQ、锁阻塞和调度开销。它不等于物理准入、Linux policy 准入或对实际 work 的完成保证。

`deadline < period` 不批准准入，不能用一个有限 horizon 绕过。工具仍可进行诊断 replay，并在报告中明确写 `reservation_admission.status=not_admitted`。有限 DBF 检查全部 `deadline+k*period` 转折点及 horizon；使用 sporadic 需求界，忽略 offset 是保守处理。DBF 结果标 `finite_diagnostic_only`，不证明无限期可调度。FIFO/RR 不因 EDF 利用率结果而被宣称可调度。

## 输入和执行

模型任务契约示例见 [m1-model-input-v1.json](m1-model-input-v1.json)，schema 为 `xtos-scheduler-model-input-v1`。每个 task 包含 name、cpu、runtime_ns、deadline_ns、period_ns；可选字段：

- `work_ns`：每个 job 的 CPU 需求，默认等于 runtime；允许大于预算以观察过载。
- `work_profile_ns`：有限的逐 job CPU 需求数组，长度必须等于 horizon 内释放数；与 work_ns 互斥。
- `priority`：1–99 整数，省略按 1；只影响 FIFO/RR。
- `offset_ns`：首个释放时刻，默认零。

所有时间为有界整数纳秒，bool/float 不作为整数接受。runtime<=deadline<=period。顶层支持 schema、cpu、partition_cpus、horizon_ns、dbf_horizon_ns、tasks，以及仅作标签的 status、production_workload_approved、limitations；这些标签不授予验收资格。未知字段、重复 JSON key、NaN/Inf、非法参数均拒绝。

```sh
python3 tools/xtos-sched-model/replay.py \
  --input docs/scheduler-workloads/m1-model-input-v1.json \
  --output .build/scheduler-m1/new-contract-report.json \
  --policies edf,fifo,rr,reservation_edf --quantum-ns 1000000
```

报告 schema 为 `xtos-scheduler-model-report-v1`，status 固定 `model_only`。保存各 policy 的 job/segment、miss/节流、有限 DBF、理想准入和源码哈希。程序成功退出只表示报告生成成功，包括 `not_admitted` 或有 deadline miss 的诊断；不能解释为准入或阶段通过。

## M0 原始 trace 接入

```sh
python3 tools/xtos-sched-model/replay.py \
  --trace PATH/trace.jsonl \
  --output .build/scheduler-m1/new-trace-replay.json \
  --policies edf,fifo,rr,reservation_edf --quantum-ns 1000000
```

`--trace` 与 `--input` 互斥。复用 `tools/xtos-sched-bench/analyze_trace.py` 对 `xtos-sched-trace-v1` 做严格完整性检查；只接受单 CPU pinned trace，继承 mask 的 DEADLINE 诊断不能混入。逐 job 读取 `cpu_work_ns`，原计划 release 归零，全部 warmup 保留并逐条标记；源 trace 的测量摘要另存 `trace_source`。输入一次有界读取，同一份字节用于解析和 SHA256，原始输入不会被改写。

这是观察到的 CPU 需求的反事实逻辑 replay：不重建原 wake/finish、IRQ、竞争任务或设备路径；CPU 时间含测量开销，不是 WCET。trace schema 有效也不代表生产任务或采集者身份已获认证。旧 KVM trace 仍只能证明工具功能。

## 资源、证据和阶段门

上限：输入 1 MiB，128 tasks/CPUs，4096 jobs，100000 replay events，10000 DBF 转折点，时间值最大 `2^63-1`。超过上限显式失败，不返回截断成功报告。只读打开输入时使用非阻塞、nofollow 和 regular-file 检查；输出必须是不存在的新路径，独占创建防止覆盖竞争写入者，包括悬空链接。

测试入口包含 `tests/test_xtos_sched_model*.py`。独立逐时间单位 oracle 与事件引擎比较完整 CPU 占用和 job 时间；测试输出及候选报告留在 ignored `.build/scheduler-m1/`。

当前只交付参考工具。M1 阶段退出仍需真实任务的可测收益或必要能力，UAPI/内核 RFC、真实任务迁移/阻塞/yield 模型及 Linux 对照不能由上述简化模型推导。若 M0 证实原生策略足够，应交付原生配置/诊断并停止自定义类。M2–M5 仍按原计划阶段门推进。
