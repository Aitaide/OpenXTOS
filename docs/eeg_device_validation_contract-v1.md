# XTOS EEG 设备验收契约 v1

状态：契约已实现，真实设备证据未提供。

验证器：`tools/xtos-eeg-pipeline/device_contract.py`

## 证据模式

- `live_device`：必须来自指定物理设备，包含设备身份、负责人、物理 attestation、原始采集物、驱动/reader 哈希、只读 preflight 和独立 reviewer。
- `fixture_replay`：只验证解析、时间戳、丢包和延迟计算逻辑，输出状态固定为 `fixture_only`，不能作为真实设备或硬实时证据。

## 样本合同

每个 frame 必须包含：

- `sequence`：单调递增序号；缺口必须逐项出现在 `missing_sequences`，并与 `dropped` 一致。
- `timestamp_ns`：设备单调时钟纳秒值，严格递增。
- `channels`、`sample_rate_hz`、`unit`：必须与顶层合同完全一致；内核面对的单位为整数 `nanovolt`。
- `quality`：必须属于顶层 `quality_flags` 声明的设备质量标记。
- `enqueue_ns`、`consume_ns`：只能用于报告队列内延迟，不能冒充设备到消费者的端到端延迟。

验证器拒绝重复/倒退序号、非单调时间戳、单位/通道/采样率漂移、消费早于入队和不一致的丢包账目。

## 生命周期合同

真实设备证据必须记录并验证：

- connect
- disconnect
- reconnect
- hotplug
- device unbind
- 断开后的恢复状态

事件名使用 `connected`、`disconnected`、`reconnected`、`hotplug`、`device_unbind`，事件时间采用 host monotonic ns，必须严格递增；它与设备 timestamp 是两个独立时钟域，不直接相减。

合法状态示例为 connected → disconnected → reconnected → hotplug → reconnected → device_unbind → reconnected；必须最终处于 connected/reconnected，且 `recovery.recovered=true`。所有生命周期 flags 必须是布尔值，不能仅靠 flags 代替事件。

缺少任一路径或成功恢复时，状态为 `blocked_external_inputs`；未知事件、错误顺序、非递增事件时钟拒绝为格式错误。这不是驱动已经支持这些路径的证明。

## 结果边界

`live_device_report_consistent` 只表示声明为 live_device 的报告在结构、样本合同和生命周期上相互一致。验证器不读取/认证物理设备或原始捕获引用，`physical_device_evidence` 与 `physical_device_evidence_verified` 固定为 false。它不表示：

- 医疗安全或医疗级准确率
- PREEMPT_RT 硬实时保证
- 设备到消费者的完整端到端延迟上界
- DMA、USB/BLE/serdev 驱动正确性
- 长时间运行、功耗、热插拔压力或调度器收益

验证器使用独占输出；错误或不可编码报告不得留下部分输出文件。

序号按 uint64 严格递增；重复帧在此 canonical 合同中不允许，duplicates 必须为 0。累计每个实际序号缺口后一次性核对完整 missing_sequences 和 dropped；不能多报、漏报或把 capture 外的序号列为丢失。frame/event/缺失序号清单和输入文件均有容量上限，读取拒绝 FIFO、符号链接和其他非普通文件，避免阻塞。设备重连若重置序号/时钟，应拆成独立 capture，不能通过此 v1 格式伪装为连续帧流。
