# 固定 CPU 分区逻辑重放 v1

状态：开发候选，源码验收以 `.build/scheduler-partitioned/acceptance.json` 为准。M0 实机与 M1 真实任务收益门未关闭，M2 内核调度类未开始。

这是多个独立逻辑 CPU 的组合模型，不是 Linux SMP 模拟器，也不设置主机 affinity 或调度策略。每个任务固定属于一个 CPU，各 CPU 从相同逻辑零点独立运行。不存在迁移、负载均衡、跨核共享锁、PI、共享缓存、IRQ 和跨核干扰，因此不能推导物理并行性能或跨核完成时限。

## 执行

    python3 tools/xtos-sched-model/partitioned.py \
      --input docs/scheduler-workloads/partitioned-model-input-v1.json \
      --output .build/scheduler-partitioned/new-report.json \
      --policies edf,fifo,rr,reservation_edf --quantum-ns 1000000

输出路径必须是不存在的新文件。程序在独占创建前完成报告序列化和 UTF-8 编码；因此不可编码的 JSON 字符会以错误退出且不留下输出文件。入口只读取任务契约，不接受多份独立采集的 trace 来拼造同步多 CPU 测量。

## 输入

schema 为 `xtos-scheduler-partitioned-input-v1`。顶层只接受 schema、partition_cpus、horizon_ns、tasks 和可选 dbf_horizon_ns；未知字段、重复 key、NaN/Inf 均拒绝。

- partition_cpus：非空、无重复的非负整数 CPU 列表，报告排序输出；允许部分 CPU 没有分配任务。
- tasks：全局非空，task name 全局唯一，cpu 必须属于 partition。任务字段、整数时间约束及四种调度语义沿用 [单 CPU 工具契约](m1-model-v1.md)。CPU 编号只是逻辑标签，不表示主机存在该 CPU。
- horizon_ns：所有 CPU 共同的新 job 释放截止点。每个 CPU 在 horizon 后继续清空自己的有限积压，不会给其他 CPU 消耗逻辑时间。
- dbf_horizon_ns：可选有限诊断窗口；任一任务 deadline<period 时必填，不会据此准入。

## 准入、诊断和报告

原子准入覆盖整个任务集，按 CPU 分别计算精确预算分数。某 CPU 预算利用率超过 1，即使全分区平均利用率低也不准入；一个 CPU 被拒绝则整批不准入。实际 work 超过预算仍可诊断重放；理想预算准入不保证每个 job 按时。

DBF 独立按 CPU 检查，标记 `finite_diagnostic_only`；它不把不同 CPU 的需求相加后与单个 CPU 供给比较，也不批准受约束截止期任务。为控制计算量，先用“各任务 deadline+k*period 枚举次数之和 + 活跃 CPU 数”保守限制全局枚举量，最多 10000；重复转折点或已包含 horizon 可能使输入比实际 distinct 点上限更早被拒绝。这是明确的资源限制，不是不可调度结论。

报告 schema 为 `xtos-scheduler-partitioned-report-v1`，status 固定 `model_only`。保存：

- 排序的 partition_cpus、active_cpus、unused_cpus。active 表示有任务分配，不要求 horizon 内实际释放 job。
- 各 policy 下按 CPU 排序的 jobs、segments、miss 与节流统计；CPU 内字段与单 CPU 模型相同。
- 全局 job_count、deadline_misses、budget_exhaustions，均是各 CPU 结果的整数求和，不计算跨 CPU 百分位数或把并行 CPU 时间误当墙钟时间。
- 理想预算准入、各 CPU 的有限 DBF、输入同字节 SHA256 与 model.py/replay.py/partitioned.py 来源哈希。

返回码 0 只代表报告生成成功，报告中仍可有 `not_admitted`、deadline miss；不能解释为产品、硬件或阶段通过。任何模型错误均不发布报告。

## 资源和验证边界

输入最多 1 MiB，全局最多 128 tasks/128 CPU，时间值最大 `2^63-1`。每一种 policy 的所有 CPU 合计最多 4096 jobs、100000 replay events；不是每个 CPU 各享一份上限。空闲跳时也计一个事件，超过上限显式失败而非截断成功。所有 CPU 的任务和 work profile 在任何 replay 开始前校验；最多四种 policy。

输入复用有界 nonblocking/nofollow regular-file 读取，同一份字节用于解析与哈希；输出使用独占创建，保留既有文件、悬空链接和竞争写入者。

测试由 `scripts/test.sh` 的 `test_xtos_sched_model*.py` discovery 自动包含。分区测试将独立 unit-tick oracle 应用于每个 CPU 的完整占用与 job 记录；这是逻辑语义验证，不是实机收益证明。
