# M0 证据索引与验证契约 v1

状态：本地证据工具；不批准物理硬件、业务收益或 M2。

工具：`python3 tools/xtos-sched-bench/m0_gate.py INTAKE INDEX --output NEW_REPORT`

## 输入与来源

INTAKE 采用 `xtos-scheduler-m0-intake-v1`，模板见 [m0-target-intake.json](m0-target-intake.json)。目标身份、运行内核、真实 workload/设备、预算依据、配置变更/回滚、预登记比较协议、独立 reviewer 和决策负责人须填写。每个 task_set 项最少包含 name、period_ns、deadline_ns、runtime_ns、cpu；前三个时间值为正整数，runtime <= deadline <= period，cpu 为非负整数。此结构不验证 WCET 论证或实际业务路径，仍由 reviewer 复核。

INDEX 位于 evidence root，schema 为 `xtos-scheduler-m0-evidence-index-v1`，顶层包含：

- `fixture_only`：必须显式使用布尔值。测试、回放和合成数据使用 true。
- `common`：完整、非空的 kernel_source_commit（40 位小写 hex）、kernel_config_sha256、kernel_image_sha256、workload_sha256、placement_sha256、runner_sha256、analyzer_sha256（其余为 64 位小写 hex）。内核三项必须与 intake 一致。
- `samples`：键为 fifo、rr、deadline，每组是运行条目数组。失败/拒绝样本留在数组中、status 使用 failed/rejected，不计入有效次数。

每个声称有效的条目包含：

    status: valid
    run_id: 该索引中唯一的非空字符串
    raw_trace: evidence root 内路径
    manifest: evidence root 内路径
    conditions: 与 common 七项字段一致
    preflight_report: 此次运行单独保存的预检文件路径
    preflight_sha256: 对应 64 位小写 hex 摘要

INTAKE 的 evidence.evidence_root 相对于 intake 文件目录解析；必须等于 INDEX 所在目录。文件路径可以是根内绝对路径或根内相对路径，解析后越出根即拒绝。输入只能是有界普通文件；文件读取按 descriptor 验证类型和大小，FIFO/目录等不得阻塞验证过程。

## 现有 runner 兼容

保留 `run_trial.py` 生成的 `xtos-sched-manifest-v1` 原始文件，不修改它来补齐验收字段。run_id、conditions、preflight 引用由 INDEX 提供。现有 manifest 的 trace_metadata、runner_args、source_sha256.binary、trace_summary、source_unchanged_during_trial、CPU affinity/readback 会被核对。兼容带顶层 binary_sha256、run_id、status、preflight 引用的扩展 manifest，但不得与 INDEX 或嵌套字段冲突。

raw trace 的哈希与解析使用同一份快照；重新运行现有 analyzer 的 summarize，不信任拷贝的 summary。manifest.trace_metadata 必须与原始第一条记录一致；工作参数、实际优先级、CPU 请求、effective affinity 和 competitor CPU 放置必须相符。每个有效 run 的 raw、manifest、preflight 哈希进入 `verified_artifacts`。

预检文件必须匹配 SHA，schema 为 xtos-sched-preflight-v1，运行内核身份与 intake 相同、realtime=1，目标 CPU 在 online/allowed 中。非 fixture 要求 live_host；fixture_filesystem 仅用于 fixture_only。显式虚拟化信号阻止物理结果就绪。缺少 hypervisor 信号不证明裸机：unknown 仍需要独立人员核对物理 attestation，工具不会把它改成 bare metal。

## 比较与重复规则

协议 required_native_policies 精确接受 SCHED_FIFO、SCHED_RR、SCHED_FIFO_or_SCHED_RR、SCHED_DEADLINE，并须包含至少一个 RT 组和 DEADLINE。OR 允许其中一个完整组，不强制 FIFO/RR 两者。每组至少 valid_repetitions_per_policy_minimum 次有效样本，该数至少为 10。

拒绝重复 run_id、raw/manifest inode、raw digest 和重复 preflight inode；复制/硬链接同一 trace 不算多次运行。每个有效组的 workload/period/deadline/budget/duration/warmup/CPU 与来源身份指纹必须一致，优先级在同策略内一致；跨策略比较剔除策略名与策略优先级。DEADLINE inherited-mask 是诊断，不能混入可比组。

此版本只接受单 task periodic trace。它可以验证该格式的产物，不能证明 periodic.c 已代表 intake 中任意真实业务任务；真实应用尚须开发 workload adapter，并由独立 reviewer 验证任务映射。文件摘要和唯一引用只提供一致性检查，不认证操作人或证明每次执行真的发生。

## 输出与限制

- blocked_external_inputs：缺少目标、内核或 workload 必填输入。
- evidence_insufficient：证据格式、来源、重复次数或一致性不满足。
- fixture_only：结构一致的 fixture，不是实机性能。
- ready_for_independent_review：结构与跨产物一致性满足，待人工复核。

`hardware_gate_accepted` 和 `claims_custom_scheduler_superiority` 固定 false；go_no_go 仅为 blocked 或 pending_independent_review。即使 intake 填 native_sufficient，也不自动批准停止或开发自定义 sched_class。工具不评估自然语言阈值、统计置信区间、物理 attestation 真伪或 WCET 论证，不替代独立决策签字。

JSON 拒绝重复 key 和非有限数字；JSON 文件上限 8 MiB，raw trace 64 MiB、100000 jobs，每策略最多 1000 entries。结果先序列化并 UTF-8 编码，再独占创建新输出，已有文件/硬链接/符号链接不能被覆盖。
