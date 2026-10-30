# 调度 workload 索引

M0 当前闭环状态及未关闭的外部输入见 [M0 调度基线闭环](m0-closure.md)；可填写目标机、真实 workload 与比较门槛表见 [M0 intake 模板](m0-target-intake.json)。模板中的 `null`/空数组表示待提供，不能作为通过证据。

基准采集工具提供 [periodic-cpu-v1](periodic-cpu-v1.md)，用于功能测试。它是合成单任务 CPU workload，不是已经批准的 XTOS 生产负载，也不证明必须开发自定义调度器。

M1 用户态参考工具见 [m1-model-v1](m1-model-v1.md)，输入 fixture 见 [m1-model-input-v1.json](m1-model-input-v1.json)。可 replay 理想 EDF、FIFO、RR 和任务共享固定周期预算 reservation_edf，也可导入 M0 pinned trace 的逐 job CPU 需求。该工具不仿真 Linux CBS，不批准 M1 阶段收益门，也不提供内核或物理性能证据。

多 CPU 固定分区逻辑重放见 [partitioned-model-v1](partitioned-model-v1.md)，示例输入见 [partitioned-model-input-v1.json](partitioned-model-input-v1.json)。仅组合固定 CPU 的独立时间线，不模拟迁移、共享锁或物理跨核干扰。

OpenBCI Cyton 用户态串口适配切片见 [EEG 用户管线](../eeg_user_pipeline.md) 与 `tools/xtos-eeg-pipeline/{openbci_cyton.py,cyton_capture.py,cyton_replay.py}`：PTTY fixture 验证 33-byte/250Hz/8-channel packet parser、115200 8N1 bounded capture、counter/loss/error 状态、原始哈希和八通道滤波 replay。此状态仅表示本地用户态代码通过合成帧测试；真实设备 identity/gain/firmware、热插拔恢复、USB/BLE 插件和内核 serdev/DMA 仍未验证。声明式真实设备验收格式见 [eeg_device_validation_contract-v1](../eeg_device_validation_contract-v1.md)，不能以此契约或 PTY 测试替代物理设备证据。

M0 证据索引与现有 runner 兼容格式见 [m0-evidence-v1](m0-evidence-v1.md)。校验器最多输出 ready_for_independent_review，不能自动关闭物理 M0 或批准 sched_class 实施。

测试定义参数见 [periodic-cpu-v1.json](periodic-cpu-v1.json)。这些参数用于复现短测；实现不能把该文件的示例 CPU 当成任何机器都可用的 CPU。

进入 M0 完整基线验收前，必须确认：

1. 代表性真实应用、负责人及任务依赖图。
2. 任务 release、period、deadline、CPU budget/WCET 依据、设备/IRQ 和超载处置。
3. 指定裸机、匹配的 XTOS PREEMPT_RT 源码/镜像、CPU/IRQ/NUMA/电源条件。
4. Linux FIFO/RR 和 DEADLINE 在同条件下的缺口；不把策略配置错误或 root-domain 准入拒绝解释为 Linux 性能不足。
5. 预先登记的主指标、允许回归、重复次数和统计分析规则。

在此之前不允许根据 KVM 冒烟结果宣称优于 Linux，也不进入 M2 内核调度类集成。
