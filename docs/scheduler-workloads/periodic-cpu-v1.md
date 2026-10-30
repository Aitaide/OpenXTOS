# 周期 CPU workload 与 trace 契约 v1

状态：M0 工具验证用的合成负载，不是已确认的生产 workload，也不是物理设备或实时性能验收。

## 1. 固定负载

一个被测周期任务，加一个 SCHED_OTHER CPU 竞争进程。默认两者绑定在相同 CPU。每个 job 使用 CLOCK_MONOTONIC 的绝对时间睡眠，在唤醒后执行固定目标 CPU 时间的整数运算，完成后记录结果。采集在内存中进行，运算结束并恢复 SCHED_OTHER 后才写出 JSONL。

当前合成短测契约：

- period：10,000,000 ns。
- relative deadline：8,000,000 ns。
- work：1,000,000 ns CPU 时间。
- DEADLINE runtime：1,200,000 ns CPU 预算；这只是实验参数，不是真实应用 WCET 或可调度性证明。
- duration：1 秒，共 100 jobs。
- warmup：前 10 jobs 保留在 raw trace 中，摘要只统计后 90 jobs。
- OTHER/DEADLINE priority：0；FIFO/RR priority：20。
- CPU：运行前选择 online 且调用者允许的 CPU，示例使用 CPU 2，不在工具中写死。

该负载用于检查策略设置和计时采集路径，不足以证明 Linux 原生策略不能满足 XTOS。真实 workload 的任务集、并发、依赖、IRQ、设备输入、deadline、WCET 估计方法和收益门槛尚需单独确定。

## 2. 调度与 affinity

C workload 内调用 sched_setscheduler 或 sched_setattr，并读回实际 policy、priority 和 DEADLINE runtime/deadline/period。默认显式绑定指定 CPU，读回失败、策略不符或准入拒绝都使运行失败，不自动改用另一种策略。

Linux DEADLINE 带宽准入以 root domain 为单位；允许 CPU 集需要覆盖相应 root domain。当前开发宿主的单 CPU 绑定 DEADLINE 被拒绝：先缩小 affinity 再设置 DEADLINE 返回 EPERM；先设置 DEADLINE 再缩小 affinity 返回 EBUSY。相同参数保留全 CPU mask 可设置成功。此行为不能简单归因于 chrt 或缺少 root，也不能通过关闭 admission control 来绕过。

显式 `--deadline-unpinned` 模式只作诊断：被测任务保留调用者 CPU mask，C 的 CPU 参数为 -1；竞争进程仍绑定 `--cpu`。trace 标记 `affinity_mode=inherited`、`cpu_requested=-1` 并记录实际 CPU mask。该模式不能与单 CPU 绑定组作同条件性能比较。

## 3. 时间含义

- `release_ns`：job 计划释放的 CLOCK_MONOTONIC 绝对时间。
- `wake_ns`：绝对睡眠返回后的第一次用户态计时，不是内核真正 runnable/wakeup 事件。
- `finish_ns`：job 完成运算后的用户态计时。
- `deadline_ns`：job 的 release 加 metadata 中的相对 deadline。
- `work_ns`：合成 CPU 运算目标。
- `cpu_work_ns`：CLOCK_THREAD_CPUTIME_ID 测得的实际 CPU 运算时间，包含测量开销，不能冒充设备处理成本或应用 WCET。

没有内核 sched_wakeup/sched_switch tracepoint 的记录，不能把 wake-release 全部称为纯调度开销，也不能推导 intra-job 完整迁移次数。仅能观察 job 采样点之间的 CPU 变化。

## 4. 严格 JSONL 契约

第一个 record 是 metadata，其后全是按 id 排序的 job。以下是结构示例，不是实测数据：

```json
{"type":"metadata","schema":"xtos-sched-trace-v1","policy":"other","actual_policy":0,"actual_priority":0,"period_ns":10000000,"deadline_ns":8000000,"work_ns":1000000,"runtime_ns":1200000,"duration_s":1,"cpu_requested":2,"clock":"CLOCK_MONOTONIC","warmup_jobs":10,"expected_jobs":100,"affinity_mode":"pinned","cpu_affinity":[2],"wake_semantics":"first_userspace_timestamp_after_absolute_sleep"}
```

```json
{"type":"job","id":0,"release_ns":1000000000,"wake_ns":1000020000,"finish_ns":1001100000,"deadline_ns":1008000000,"work_ns":1000000,"cpu_work_ns":1000500,"cpu":2,"warmup":true}
```

必须验证：metadata 参数与实际 policy 一致、任务参数有效、job 数完整、id 从 0 连续递增、release 间隔匹配 period、deadline 和 work 与 metadata 一致、warmup 必须为 boolean 且位置正确、时间顺序成立、CPU 时间不超过墙钟工作区间、有效 affinity 及绑定 CPU 符合要求。缺失、重复、损坏或非有限值都不作为有效性能样本。解析器输出另存，不能覆盖 raw trace。

## 5. 运行与证据

完整命令、权限、显式诊断模式与解释见 [工具 README](../../tools/xtos-sched-bench/README.md)。所有原始 trace、运行日志、manifest 和摘要保存到独立的 `.build/` attempt 目录，禁止覆盖过去结果。报告以源码/二进制/原始 trace 哈希绑定候选，dirty 开发源码不能只标一个 Git HEAD 便冒充 clean release。

一个短测成功并不关闭阶段。M0 的完整 go/no-go 仍需要代表性真实 workload、量化收益门槛、目标裸机运行匹配 XTOS PREEMPT_RT 镜像、同条件 FIFO/RR/DEADLINE 重复对比与独立评审。当前 KVM Linux 6.12 PREEMPT_DYNAMIC 结果仅属工具功能证据。
