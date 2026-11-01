# XR 与多模态应用层 v1

状态：P6 实施合同。P5 软件切片已验收提交 0ee8b87f367e1cd59e996d4d5bf0bf91ef0e2e21，证据 `.build/p5-provider-next/acceptance-index.json`。本合同不继承其测试或 candidate-61 镜像 PASS。

## 交付与边界

交付独立 C OpenXR loader 适配器及 `xr-probe`、Python `xtos_xr` 有界多模态与模拟反馈 CLI、可重现维护测试和 source-only 安装归档。OpenXR 保持应用层；不修改内核、IRQ、设备配置、active_runtime.json 或全局 XR_RUNTIME_JSON。第三方头文件固定 Khronos OpenXR-SDK release-1.1.57 并保留授权、网址、SHA256；不捆绑 loader、vendor runtime 或图形会话。实际 loader 缺失返回明确 unavailable/no_action；fixture .so 是编译后的注入 runtime ABI 测试，不是头显或 Khronos 合规证据。

本切片不实现图形绑定、xrCreateSession、渲染、眼动 action/space 的实际采集；这些是未实现的目标设备集成门。Python 消费的 eye_gaze 记录仅验证明确的样本语义，不能称真实 eye tracker 已接通。没有已校准设备时，不把 host receive 时间写成采集时间。所有输出和确认回执固定 safe_action=no_action、physical_acceptance=false；不调用 OS actuator、runner 或任意外部执行器。

## Native loader 合同

仅由可信启动配置提供固定绝对 loader 路径，不能从数据记录选择 dlopen 目标。启用 XR_NO_PROTOTYPES 和 POSIX time 支持；通过 xrGetInstanceProcAddr 解析核心函数。枚举扩展数量和名称有界（最多128），只请求已枚举的 XR_KHR_convert_timespec_time；未启用不得查扩展函数。明确应用/引擎名和 OpenXR 1.0 API，创建唯一 instance；缺符号、runtime unavailable、create失败、函数解析失败和初始化后故障都要清理 owned instance/library，close 幂等。

xrPollEvent 返回 XR_EVENT_UNAVAILABLE 是空结果，不伪造事件。最多256事件/次CLI；保留 numeric event type、session state 和 lost count，只报告明确解码字段。unknown、events_lost、instance_loss、session/reference-space/interaction-profile变化都推进 barrier，失效 pending 工作；负 runtime 结果同样失效。sequence 单调，实例身份用 generation 标识。未知 next 链不解引用。

XrTime 是 runtime epoch 的正 int64 纳秒，不能与 CLOCK_MONOTONIC 原值直接相减。只有已启用的 XR_KHR_convert_timespec_time 函数允许转换，按每个时间点调用并验证返回码、timespec 范围和溢出；不缓存永久 offset。缺少转换时明确拒绝跨时钟 join。C API保留原始时间、转换结果和对应错误，CLI与测试不能把模拟映射当物理同步精度。

## Python sample 与 join

精确 schema=xtos.multimodal.sample.v1，对象仅含 modality、source_id、source_kind、clock_domain、source_timestamp_ns、host_observed_ns、sequence、generation、quality、values 及 schema。modality 为 eeg/eye_gaze/emg；source_kind 为 live_device/fixture_replay/simulated_runtime。标签是未认证 metadata，不能授予动作权限。source_id 为有界 ASCII 标识，sequence/generation 为非 bool 正 uint64；时间为非 bool 正 int64，values 为1..32有限 float32 可表示数值。先复制为不可变快照，再校验保留值；未知字段和质量位拒绝。

quality 精确对象 valid/tracked/predicted，值必须 bool；三者分别为 true/true/false 才可 join。eye_gaze 必须提供 runtime 样本时刻，零/预测/失踪跟踪均拒绝。host_monotonic_ns 可直接检查；openxr_time 和 emg_device_time 仅可使用显式注入的可信 converter，返回精确 converted_host_ns、uncertainty_ns、conversion_id。无 converter/输出畸形/缺ID/不确定度越界拒绝。不接受样本自报的转换结果，不根据标签推导 offset。

每模态最多保留一个不可变最新样本；总体3个。不建立无界队列。相同源/代次的 sequence 必须连续且时间不倒退；重复或旧代次拒绝。换源、代次变化、断号、畸形/低质/过期/未来时间、runtime fault/barrier、设备断开及显式 revoke 清空 pending/candidate，并推进 barrier。允许重建后重新采样，旧 candidate 不得恢复。

策略由构造器固定：max_age_ns、max_skew_ns、max_uncertainty_ns 和 candidate_ttl_ns，均正整数有上界；等于 age/skew 上界接受，超过1拒绝；观察时刻不晚于当前hostclock，转换结果不晚于观察时刻。join 必须三模态齐全且在同一当前 barrier，所有年龄与不确定度有效、skew不超界。每个新输入和 join/confirm 都重新检查 freshness；时钟倒退拒绝。成功仅返回不可变 simulation candidate，保存三个样本、转换身份和 barrier，过期时间不超过最早样本 freshness 截止及 TTL。

## 确认与撤销

复用 xtos_action_gate.ActionGate；approval_request 精确绑定 candidate 身份和内容到固定 `/usr/libexec/xtos/xr-feedback-sim` argv 以及完整既有 sandbox schema。CLI demo 只创建操作拥有临时 FixtureStore；不初始化 TrustedStore。用户/系统生产确认必须使用既有真实 authority，actor 字符串不作身份。

确认只接受 exact pending candidate。不持有 joiner 锁等待 store I/O；ActionGate 消耗 nonce 后，内部固定内存 callback 持锁重新检查 closed、对象身份、barrier、每个样本 freshness 和有效期，先销毁 pending 再生成 simulated/no_action receipt。不存在可注入 executor 参数。重复确认/nonce、参数变化、过期、revoke、close及等待 consume 时发生 barrier 均拒绝，无模拟回执。确认与失效竞争由 owner 锁串行；重复 close 幂等，cleanup不移除调用方文件。

## 集成与验收

Native 与 Python 独占路径分别 tracer RED→GREEN；保留每个增量的源、测试、依赖、命令、退出码和日志，不补造历史。C 真实编译和 ASan/UBSan 分列；Python 普通/优化及 gate 真实 FixtureStore、确定性交错测试。后续只读 SPEC 再 QUALITY；父负责脚本、SBOM、安装和文档。完整维护冻结后执行，计数从 discovery/原始日志核算。精确私有提交/远端读回、干净源码安装归档及实际 demo 后才接受本软件切片。新的整机镜像、实际 loader/runtime/头显/眼动 action与space、物理 EEG/EMG、时延/精度/GPU/硬实时仍各自保持未实现或未验证，不混用。

资料：Khronos OpenXR XrTime、xrPollEvent、XR_KHR_convert_timespec_time 与 XR_EXT_eye_gaze_interaction 官方规范；SDK pin 和许可见同目录依赖记录。
