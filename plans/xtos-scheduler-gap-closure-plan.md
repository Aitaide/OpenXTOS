# XTOS 调度缺口闭环开发计划

> **For Hermes:** 按阶段实施；每个代码任务先完成 RED→GREEN，再进入下一任务。真实硬件和真实设备门不得用 fixture 或 QEMU 结果代替。

**目标：** 将 XTOS 从“用户态参考工具已验收、内核辅助队列已编译”推进到一套可复核的 M0 证据门禁、sched_class 集成审查材料和真实设备/硬件验收准备状态。

**架构：** 当前仓库先完成所有不依赖外部设备的工作：严格校验 M0 输入、原生策略 trace/manifest 的同条件证据、完整性与 go/no-go 判定、sched_class hook/lifecycle RFC、状态文档一致性和测试接线。物理目标机、真实 workload、OpenBCI 和实机性能结论保留为外部阻塞门；收到输入后只消费冻结格式，不修改验收口径。

**技术栈：** Python 标准库、现有 `xtos-sched-trace-v1` JSONL、现有 benchmark manifest、Linux 6.6.0-rt15/PREEMPT_RT 源码审查、`unittest`、现有 `scripts/test.sh`。

---

## 阶段门总览

| 阶段 | 本轮交付 | 当前可否完成 |
|---|---|---|
| G0 状态与计划冻结 | 本计划、状态矩阵、旧文档边界测试 | 可以 |
| G1 M0 证据门禁 | intake/schema 校验、trace/manifest 同条件校验、重复次数和预注册门槛字段检查 | 可以完成工具；真实结论与阈值解释须独立复核 |
| G2 sched_class 集成准备 | RFC、lifecycle matrix、UAPI/锁/回退清单和静态审查断言 | 可以 |
| G3 用户态真实设备准备 | EEG 契约验证器、OpenBCI Cyton 33-byte 串口解析/有界采集器、原始包与 decoded JSONL 哈希、八通道成对滤波回放；PTY 自动测试 | Cyton 用户态适配代码可本地完成；真实板卡、增益/固件识别、断连恢复与 USB/BLE/serdev 驱动集成仍待验收 |
| G4 实机 M0/M1 | FIFO/RR/DEADLINE 同条件重复 trace 和独立 go/no-go | 外部阻塞 |
| G5 内核 M2–M3 | 默认关闭 sched_class、单 CPU 生命周期、SMP/PI/hotplug/NOHZ/cgroup | 依赖 G4 go/no-go，当前不启动 |
| G6 发布 M4–M5 | 真实硬件性能、安装/回滚、支持矩阵和发布证据 | 外部阻塞 |

## 执行任务

### Task 1：建立严格 M0 gate 输入和证据模型

**Files:**
- Create: `tools/xtos-sched-bench/m0_gate.py`
- Create: `docs/scheduler-workloads/m0-evidence-v1.md`
- Test: `tests/test_sched_m0_gate.py`
- Modify: `tools/xtos-sched-bench/README.md`
- Modify: `docs/scheduler-workloads/m0-closure.md`

**契约：**
- 只接受 `xtos-scheduler-m0-intake-v1`；目标、运行内核、应用/设备、预算依据、预登记门槛和决策负责人均须填写。
- 每策略至少 `valid_repetitions_per_policy_minimum` 个有效、同条件、pinned trace；该值必须至少为 10。有效 run 必须有不同且唯一的 run identity、raw trace/manifest 不得重复或硬链接复用；失败/拒绝运行保留但不能计入有效样本。
- 只允许现有 `xtos-sched-trace-v1` JSONL；从 trace 重新分析结果必须与 run summary 一致。runner 参数、trace metadata、effective affinity/policy/readback 必须逐字段一致。
- 每 run 必须有 raw trace、summary、per-run preflight、runner/analyzer/binary hash 和受审计的 manifest；所有 raw digest 与实际字节一致。每策略/每 run 必须具备 workload、placement、kernel source/config/image、runner、analyzer、binary identity；缺任一值都拒绝，不允许 `None == None` 通过。
- deadline inherited-mask 诊断不得混入可比组；同一策略组条件指纹必须完全一致，跨策略只允许 policy/priority 参数按预登记协议不同。
- gate 只返回 `evidence_insufficient`、`fixture_only`、`blocked_external_inputs` 或 `ready_for_independent_review`；不得自动返回 `native_sufficient`/stop 或批准 custom scheduler。自动工具不验证物理证明真伪、WCET 论证质量、统计阈值语义或人工决策。

**TDD 验证：**
- 缺字段、空样本、诊断 trace、哈希漂移、混合 CPU placement、重复 run、少于 10 个有效样本先 RED。
- 完整 synthetic evidence fixture GREEN；fixture 结果标记 `fixture_only`，不得标为硬件通过。

### Task 2：完成 sched_class RFC 和生命周期矩阵

**Files:**
- Create: `docs/xtos_scheduler_class_rfc.md`
- Create: `docs/xtos_scheduler_lifecycle_matrix.md`
- Test: `tests/test_scheduler_class_readiness.py`
- Test: `tests/test_sched_class_hooks.py`（直接核对 pinned kernel 的全部 callback）
- Modify: `docs/xtos_scheduler_class_design.md`

**内容：**
- 明确当前 helper 不是 sched_class，RFC 仅为未批准候选。
- 覆盖 stop/dl/rt/xtos/fair/idle 类顺序、每个 sched_class hook、rq/PI lock context、睡眠禁止点、task lifetime、回退路径。
- 覆盖 fork/exit、wake/sleep、enqueue/dequeue、yield、pick/put/set-next、tick/runtime、policy/priority、PI、cgroup、affinity/migration、hotplug、NOHZ、SMP balance、core scheduling、signal、throttle、recovery。
- 明确 UAPI 不复用保留字段、不添加 syscall number；实现前必须评审 policy 编号、权限、errno、fork/exec/reset-on-fork 和 32/64-bit。
- 明确默认关闭、不可卸载、失败回退和原生 PREEMPT_RT fallback。
- 明确 M0 未关闭时禁止进入 M2。

### Task 3：真实设备链路准备契约

**Files:**
- Read: `docs/eeg_user_pipeline.md`, `docs/spsc_data_plane.md`
- Create: `docs/eeg_device_validation_contract-v1.md`
- Test: `tests/test_eeg_device_contract.py`

**内容：**
- 定义 transport、sample timestamp/sequence/channel/rate/unit/quality、丢包、重连、热插拔、设备卸载和端到端时延证据。
- 现有虚拟 CSV/SPSC 只作为 fixture；真实 OpenBCI/USB/BLE/serdev 证据必须单独归档。
- 不在当前无设备环境伪造真实设备通过。

### Task 4：状态矩阵和验收台账统一

**Files:**
- Modify: `STATUS.md`
- Modify: `README.md`
- Modify: `docs/scheduler-m0-progress.md`
- Modify: `docs/scheduler-workloads/m0-closure.md`
- Test: `tests/test_scheduler_docs_currentness.py`

**验收：** 不再把“自定义 XTOS scheduler”“已验证 PREEMPT_RT”“真实硬件验证”写成当前已完成能力；所有状态均链接当前候选和外部阻塞输入。

### Task 5：当前候选增量审查和完整维护

**Files:**
- Create: `.build/scheduler-gap-closure/` ignored evidence only
- Create: `.build/scheduler-gap-closure/run_final_suite.py`，复用已审计的产物保留/恢复逻辑，不改写旧分区报告

**验收：** 冻结全部路径和哈希；独立 SPEC/QUALITY 复核；聚焦测试；完整 `scripts/test.sh`；零跳过、AI 严格清单、源码前后快照一致。旧失败报告保留。

### Task 6：外部输入到位后的 M0/M1 实机闭环

**前置条件：** 填写 `docs/scheduler-workloads/m0-target-intake.json`，提供物理目标机、负责人、镜像/配置哈希、真实任务和预注册门槛。

**步骤：**
1. 只读 preflight 和目标身份复核。
2. 相同 workload/CPU/IRQ/电源条件下运行 FIFO/RR/DEADLINE，各至少 10 次有效样本。
3. 保存所有原始 trace、manifest、summary、失败/拒绝样本。
4. 用 `m0_gate.py` 生成证据完整性和跨产物一致性报告，合格状态为 `ready_for_independent_review`；工具不得自动宣布 `native_sufficient` 或批准 M2。
5. 由非实现者复核后决定停止定制调度器或进入 M2。

**当前结论：** Task 6 及之后不在当前 KVM 开发容器内伪造完成。没有真实 workload 和物理目标机时，正确输出是 `blocked_external_inputs`。

## 内核 Cyton 本地修正切片

本地可完成的增量范围：共享 C 协议 helper → 实际驱动接入 → host 故障交错回归 → 完整内核模块构建 → patch/SBOM 与维护接线。按此顺序核对：

1. `kernel/linux/drivers/xtos/bci/test_cyton_packet.c` 覆盖 offset、signed24/nV、footer、counter 和有界重同步；保留已有 RED，新增行为先测试。
2. `devices/openbci_cyton.c` 在 probe 暴露回调前初始化状态，控制命令串行化，接收 mutex 不跨同步 TX；检查完整写入、正确通道指令、缺口统计和 cleanup 排他。
3. `tests/cyton_driver_host/` 直接包含实际驱动源码，注入 callback-before-open、START 内收包、短写/失败、控制并发和 cleanup 交错；不把 shim 当内核运行。
4. `tools/xtos-fuzz` 复用 helper，语料必须覆盖有效 footer、正负极值和超过环容量且回绕的持续包，ASan/UBSan 检查；维护配置要求 serdev bus 与 TTY controller 内建。
5. 同步 integration patch、SBOM、`scripts/test.sh` 和当前性测试；冻结文件后独立 SPEC→QUALITY 复核，再运行完整维护，证据放 `.build/cyton-kernel-closure-v1/`。

这一切片不关闭真实板卡、Linux 原生 sched_class 或 G4–G6。BCI core 的既有 FD/unbind/VMA 生命周期和 mmap 消费协议属于后续开发；新 ISO 及 QEMU 加载验证分别列门，不以 host 源码回归代替。

## 执行状态与完成口径

Task 1/2/3 的本地工具和报告契约已实现，整体验收绑定 `.build/scheduler-gap-closure/acceptance-ledger.json`，以其记录的候选哈希、独立复核和维护 attempt 为准。初次七项 HIGH 缺口的 FAIL 报告保留，聚焦通过不得覆盖旧失败记录。

G3 本地用户态切片现包含 `tools/xtos-eeg-pipeline/openbci_cyton.py`、`cyton_capture.py`、`cyton_replay.py`：支持 OpenBCI Cyton 33-byte/8-channel/250Hz 协议解析、115200 8N1 串口采集、sample-counter 缺口/包损坏/半包标记、不可覆盖的原始捕获哈希包，以及调用现有 XTOS 2-channel 滤波器处理 4 组通道。PTY fixture 覆盖串口配置、分块接收、包计数回绕、断连与 partial capture、丢样 fail-closed、哈希和滤波链路。system/Live 构建路径已接入 Python 源文件与 `/usr/bin/xtos-eeg-pipeline`，Live profile 现在声明 Python 标准库、解释器和安装路径契约；system rootfs 仍明确不内置 Python；新增 `scripts/verify_cyton_runtime.py` 分别执行 source-staged 静态检查与 Live 运行时检查，Live 构建还要求 chroot 合成采集/默认八通道回放烟测并将结果写入构建清单。chroot 仅使用构建机内核，不表示镜像启动或设备验收。这个切片只实现 Cyton 串口用户态适配；USB/BLE 通用插件、board gain/firmware readback、真实设备 identity/热插拔和端到端时延仍未验证。G4–G6 保持开放；当前 XTOS 调度 helper 仍非 Linux `sched_class`，不得因用户态或虚拟测试通过启动 M2。
