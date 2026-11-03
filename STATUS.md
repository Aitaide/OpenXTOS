# XTOS 当前状态

最后更新：2026-11-03

本文件保留各历史候选的验收范围。当前工作树的源码实现、维护测试、镜像验收和物理硬件验收分别记录；历史镜像的 PASS 不自动覆盖本次提交。

唯一开发目录：`/root/xtos-development/xtos`。

源码基线已迁移为官方签名核验的 Linux 7.3.1 与主线 PREEMPT_RT；版本和补丁来源见 `kernel/source-pin.json`。x86_64、ARM64、RISC-V 64 完整内核与生产 BCI 三模块均已实际构建；三个架构的合成设备 QEMU 已验证运行中 RT 模型、配置查询、旧/逐帧 ioctl、只读 mmap、SDK、解绑与模块卸载。该内核升级的精确提交、本地 Live 镜像与完整维护已完成验收，准确范围见下一节。后续功能开发正在进行，不把既有快照的 PASS 直接授予新源码。

## candidate-64 安全加固已验收镜像与当前交付范围

- 本轮安全加固源码提交 `5455b2fe09dbc2b9d6933e9bc6000eb5fc600892`（17 个文件）经独立增量 SPEC/QUALITY 复核（防火墙链健康与规则漂移检测、更新严格整数 size 与签名快照重验、显式失败恢复、启动门、测试观察器修正），并推送私有 main（fast-forward `fc03edfd2..5455b2fe0`，远端 `refs/heads/main` 读回一致）。
- 完整维护：148 组、1790 次测试方法执行，仅 2 项显式 opt-in loop 写盘跳过，AI 验收 11/11 零跳过；主目录与隔离副本源码运行前后逐字节核验未变。a2–a5 历史失败记录全部原样保留。
- candidate-64-hardened：ISO SHA256 `f836c27d78f0430de9867f271305cbf19a90971ba348bfb6d35132db6d72bbed`（1,067,184,128 字节）。由加固内核 `7.3.1-xtos-live.1`（`kernel/xtos_defconfig` 加固项经 builder 校验）与该 clean 提交按 `--require-clean` 构建；manifest 的 `source_git_head/source_state/iso_sha256` 与实测逐字一致。
- 四模式限定安全冒烟 PASS（BIOS/UEFI × CD/USB）：串口防火墙守卫与就绪标记在位；普通 xtos 用户 UID 1000 登录且 `xtos-rt-status` 输出 1；root SSH 公钥拒绝（exit 255，stderr 含 `Permission denied` 与 `publickey`）；非特权 `fw status` 拒绝（exit 1，`fw: requires root`）；每模式哨盘强制关机后未变。检查器 22 项控制器回归普通/优化模式各通过，并经独立 SPEC/QUALITY 复核（6 个反例探针全部被拒）。
- 更新失败恢复：生产版 `update` 在 QEMU 来宾 `/dev/vdb` 注入虚拟块写 EIO 后正确记录 journal 并拒绝继续；同一虚拟磁盘重启后 `recover` 重新验签、完整重写并读回 SHA256 与已签名产物一致，旁路哨盘未变。该证据限于虚拟块设备与正常重启路径，不覆盖突然掉电、物理盘或 A/B 自动回滚。
- 证据与报告封存 `.build/production-hardening-v1/`（`full-maintenance-a6/`、`candidate-64-security-smoke-a1/`、`checker-review-a1/`、`update-block-guest-a1/`、各 SPEC/QUALITY 增量报告）。本状态为验收后的记录更新；新提交不改变已验收镜像的源码身份，后续新增源码不继承该 PASS。干净关机、旧版四模式全功能验收、物理设备（Cyton/BLE/GPU/头显）、厂商协议会话、持续硬实时、模型效果、Secure Boot 与公开发布仍单列未验收。

## candidate-63 已验收镜像与当前交付范围

- 本轮软件源码提交 `3ec7b643b78a253238f2d3331ae0c5c251ee5693` 已完成组件与组合审查、145 组/1748 次完整维护执行（AI11 已包含，零失败/错误/跳过）、干净提交严格 AI11，以及新 candidate-63 的精确镜像验收。
- ISO SHA256：`af96f99c2a4663c101b116f42bb34fea76b0a08f35fca4578eb1c931dedad8c5`。BIOS/UEFI × CD/USB 四种实际 QEMU 启动通过；在 BIOS-CD 普通用户下，已安装 TLS 六项、显式模拟 OpenXR ABI 编译与眼动探针 normal/-O 通过，错误哈希在导入前拒绝。应用工作负载未在其他三种模式重复运行。
- 最终状态 `ACCEPTED_LOCAL_SOFTWARE_IMAGE_ONLY`，封存 `.build/remaining-closure-v2/local-software-image-seal.json`；最终父读回核验了 334 项留存文件。仓库摘要见 `docs/candidate-63-local-validation.md`，原始镜像和证据保留在本机忽略目录，不作为 Git 文件上传。
- SDK、原生 Live 工具与 XR 探针重新编译；内核、模块和静态 BusyBox 经相关输入与产物哈希验证复用，不宣称重新编译全部系统。
- 本状态及后续研究文档是验收后的记录更新；新提交不改变已验收镜像的源码身份，不把该镜像 PASS 自动授予后来提交或新功能。物理设备、厂商协议/运行时、XR 图形、GPU、持续硬实时、模型效果、Secure Boot 和公开发布仍单列。
- 安全与长期稳定性研究见 `docs/production-readiness-and-next-development.md`。已确认防火墙链未挂接过滤入口、更新 apply 未重新验证发布签名；已有功能验收不构成公网生产安全认证。下一阶段优先处理这两项和掉电/更新恢复、目标硬件长稳。

## Linux 7.3.1 历史已验收快照

- 已验收源码提交：`32a8bd3903eda24bd44d7e1a19dea4156014d062`；验收状态 `ACCEPTED_LOCAL_KERNEL_UPGRADE_AND_QEMU_LIVE_IMAGE`，索引 `.build/kernel-7.3.1/acceptance-index.json`，独立收尾与最终读回分别保留。
- 完整维护：133 组、1656 次测试方法执行，零失败、错误和跳过；其中已包含 AI11，另有干净提交 AI11 验证。该计数是执行次数，不冒称互不重复的方法数。
- candidate-62：ISO SHA256 `96df3627bf79364192d502453904f0d78f112d3ba83a934bc28c0084a3b0c53f`，持久产物 `.build/kernel-7.3.1/retained-candidate-62-kernel731/images/xtos-developer-live-x86_64.iso`。实际内核、initramfs、rootfs 完整读回与 BIOS/UEFI × CD/USB 四模式通过；另有 x86 正常系统启动。ARM64/RISC-V 本次运行证据是合成设备 guest，不能扩张为实板验收。
- 内核和 BusyBox 是提交前相同候选内容的实际编译产物，在干净提交中按完整相关输入哈希绑定复用；Live SDK 与原生工具重新编译。不将复用表述为整机所有组件都重新编译。
- 后续新增源码不继承该 PASS。XR headless 会话/眼动采样、TLS 和 Live 安装执行路径的后续本地软件验收见上方 candidate-63；真实图形和设备路径仍须各自验收。
- 尚未关闭：厂商网络线协议/认证、真实 XR 图形会话及实际应用控制、领域脑电模型效果、物理板卡/GPU/头显联调与持续 RT，以及 M0 实机决策约束下尚未实现的自定义 `sched_class`。缺硬件与缺实现分开记录；模拟输入继续 `no_action`。
- i915 在当前上游 7.3.1 的 PREEMPT_RT 约束下不可启用，已启用的 Xe 不覆盖全部旧 Intel 核显。Secure Boot、实体部署和公开镜像发布不属于本次 PASS。原 FAIL/PARTIAL/RED 保留在 `.build/kernel-7.3.1/`。

本轮新增功能的验收记录在 `.build/remaining-closure-v2/acceptance-ledger.json`。headless 眼动 native 四项修复、CLI 非阻塞输出、XR Live 安装执行助手及可选离线 BLE 运行依赖均已通过各自冻结版本的 SPEC/QUALITY。TLS 生产、夹具 SPEC 与组件 QUALITY、共享安装接线和最终组合语义审查通过，随后完成上述 candidate-63 的维护、干净提交和精确镜像门。组合 SPEC 的过程状态保留 `PARTIAL_BUDGET_OVERRUN`；历史 TDD PARTIAL、native OBS-001 诊断覆盖观察、验证端失败及复核中断均保留。BLE 仍是显式可选目标安装，不进入默认 Live 镜像；不得将组件执行计数或合成运行扩张为无线、真实头显或长期硬件验收。

下列早期构建、跨架构和镜像记录均保留其原始内核与源码范围，不代表当前 7.3.1 候选通过。BusyBox、根文件系统、SDK、工具和测试继续在仓库维护，输出隔离在 `.build/`。

当前已验证：

- 历史缺口闭环的本地交付为 M0 证据验证器、sched_class RFC/完整 callback 审查矩阵、EEG 设备报告契约和 OpenBCI Cyton 用户态串口切片（33-byte parser、115200 8N1 bounded capture、hash-bound raw/frame bundle、八通道滤波 replay）。system/Live 构建路径已接入这批用户态源文件；Live profile 声明 Python 运行时契约，system rootfs 明确不内置 Python。构建检查区分 source-staged 与运行时模式，Live 新构建强制执行 chroot 合成采集/回放烟测并记录构建清单。该切片当时尚未重建ISO，后续clean-source candidate-59的当前验收见下文阶段3记录。代码路径由PTY/合成包测试，真实设备验收仍待完成；scheduler acceptance以 `.build/scheduler-gap-closure/acceptance-ledger.json` 绑定的候选源码哈希、独立审查和维护 attempt 为准。
- `m0_gate.py` 要求协议指定的原生策略组各至少 10 个不同执行、同条件、固定 CPU trace，拒绝诊断 DEADLINE、哈希漂移和不完整证据。最多返回 ready_for_independent_review，hardware_gate_accepted 固定 false；当前 `m0-target-intake.json` 仍是 `needs_operator_input`，因此 M0 实机门未关闭。
- `device_contract.py` 与 Cyton capture/replay 验证器已覆盖样本序号/时间戳/单位/丢包/报告一致性；新增的 OpenBCI Cyton adapter 仅是通过 PTY 和合成协议帧验证的用户态代码。真实板卡身份、实际 gain/firmware、物理 USB/UART、断连恢复、DMA/内核 serdev 和端到端链路仍未验证。
- `scripts/build_system.sh` 完成 x86_64 内核、模块、BusyBox 和 initramfs 构建。
- `scripts/verify_system.sh` 启动同一组镜像，输出 `realtime=1`、`xtos_modules=pass`、`guest_userspace=pass`、`XTOS_SYSTEM_TEST=PASS`；本轮 guest 还通过 181 项信号断言和 101 项 XTOS 队列断言。
- 用户态测试、181 项信号处理断言、固定 P300 跨试次 fixture、Python 对照测试和 SPSC 检查通过。
- 信号处理与 SPSC 的 ASan/UBSan 检查通过；SPSC 的 ThreadSanitizer 检查通过。
- `xtos-signal-baseline-v2` 已加入 P300 ERP 特征路径，并记录 FFT、带通、SSVEP、P300 的延迟分位数、CPU 时间和峰值 RSS。
- P300 跨试次质量门禁已用固定 JSON fixture 验证有效/拒绝试次、检测率、z 分数和延迟离散度。
- 阶段3C ONNX Runtime CPU基线已完成：固定 ONNX 模型、CPU provider、输出误差、延迟分位数和输入质量/超时/低置信度/模型加载失败降级测试通过。
- 阶段3C PyTorch eager 到 ONNX 导出对齐已完成：固定模型和输入在隔离 Torch 2.6.0+debian、ONNX 1.20.1、ONNX Runtime 1.23.2、NumPy 2.2.4 环境中完成端到端导出、推理、误差和预测类别一致性验证；CPU provider、单线程配置生效，实测最大绝对误差为 `0.0`。
- 阶段3D TensorFlow/TFLite 导出对齐已完成：TensorFlow 2.18.0 → TFLite → ONNX Runtime CPU 端到端验证通过，固定模型和输入在隔离 Debian TensorFlow 2.18.0、ONNX Runtime 1.23.2、NumPy 2.2.4 环境中完成导出、推理、误差和预测类别一致性验证；CPU provider、单线程配置生效，实测最大绝对误差为 `6.9e-08`；工具 `tf_export.py`、测试 `test_tf_onnx_parity.py`、基准脚本 `benchmark_tf_onnx.sh` 和文档 `docs/tf_tflite_onnx_parity.md` 已创建并接入 `scripts/test.sh`。
- 阶段3C/3D 的官方 CPU wheel 运行证据使用 ONNX Runtime 1.23.2、NumPy 2.2.4；Debian 1.21.0 依赖路径保留为探测结果，不作为干净基准。
- 阶段4A 真实硬件 RT 验证工具包已准备：`tools/realtime-test/` 包含系统配置检查（`rt_system_check.sh`）、长时间 cyclictest 测试（`rt_cyclictest.sh`）、结果分析工具（`rt_analyze.py`）和完整使用文档（`README.md`）；工具包已在 QEMU 环境验证功能正确性，等待真实 x86_64 硬件部署和 24 小时延迟测试。
- 阶段4B ARM64 与 x86_64 全面对齐：内核合并 `xtos_defconfig`（PREEMPT_RT + XTOS_SCHED + XTOS_BCI + HZ_1000），`verify_guest_arm64.sh` 在 QEMU guest 内加载全部 XTOS 内核模块并运行测试二进制（14/14 通过）。
- 阶段4C RISC-V 64 从零完成：交叉构建 + OpenSBI 启动验证 5/5 + in-guest 验证 15/15（模块加载、test_rt/test_spsc/test_signal 全过）。
- 阶段5 工程化发布的核心项已完成：fuzz 基础设施（`tools/xtos-fuzz/`，4027 变异输入通过 + 有效性自证）、CI 跨架构矩阵（x86 系统级 + ARM64/RISC-V 交叉构建）、ABI 版本化（三库版本头 + `check_abi.sh`）、SBOM（CycloneDX 1.5，`docs/sbom.json` + `--check` 防漂移）、可复现构建（`scripts/reproducible_env.sh`，x86_64 两次干净全量构建逐字节一致，QEMU 回归通过）。
- Live 开发者镜像（私有，2026-10-11/12）：`scripts/build_live.py` 产出 SquashFS+overlay 的 Live ISO（BIOS/UEFI × CD/USB 四模式，`scripts/verify_live.py` 全部通过）；镜像内含锁定 Debian 用户态（live-developer v2/v3：numpy/scipy/pandas/matplotlib/onnxruntime、WiFi 固件、perf/trace-cmd）、六个静态原生系统工具（`fw`/`net`/`info`/`hw`/`clock`/`pkg`）及 `rt`（RT 状态/调优/cyclictest 包装）与 `persist`。
- 可选持久化（2026-10-12）：内核命令行 `xtos.persist=XTOS-PERSIST` + 带 `XTOS_PERSIST_ID` 标记的 ext4 卷，仅对 `/home` 与 `/root` 生效（bind mount 覆盖 overlay），系统镜像保持不可变。`tests/test_live_persistence.py` 三靴 QEMU 循环通过：两靴 `persistence=1`、`/root` 证明文件跨重启逐字节恢复、`/home/xtos` 绑定与首靴种子正常、SSH 管理密钥自动补种（含掉电后 0 字节自愈）。默认 RAM-only 路径已在含持久化代码的当前源码上回归（candidate-21 bios-cd，20 项检查全过，内部盘 sentinel 未被写入）。
- 私有裸机安装器与 USB 物理运行时验证证据保留在 `.build/baremetal/`；Secure Boot 与物理硬件延迟未验证。

系统范围为可启动的 x86_64 开发系统，不是桌面发行版或硬盘安装器。XTOS 调度组件是辅助层，未替代 Linux 的常规任务调度。P300 当前是单窗口研究基线和跨试次质量门禁，不是分类器。真实 OpenBCI 设备、ARM64/RISC-V 板卡、目标硬件最坏延迟和医疗安全均未验证。QEMU 结果不能作为硬件实时性能保证。

入口：`scripts/build_system.sh`、`scripts/verify_system.sh`、`scripts/run_system.sh`。详见 `docs/system_build.md`；开发任务、计划及架构设计继续保留在 `plans/` 和 `docs/`。

## 本轮内核 Cyton 收敛

共享 C packet helper、driver 控制/接收路径、host sanitizer 回归、有效 footer/长流 fuzz、serdev Kconfig、integration patch 与 SBOM 的第一轮本地切片已完成；历史验收在 `.build/cyton-kernel-closure-v1/`。该验收不覆盖后续生命周期、快照和原生工具改动。

`plans/xtos-bci-software-completion-plan.md` 的上一轮本地软件交付已完成：设备引用/解绑安全与阻塞读唤醒、C 八通道生产者/消费者、用户态实际 gain/通道状态/固件信息读回、有限重连及停止、只读不可变快照 UAPI/SDK、原生 `bci` 命令和 system/Live 安装。验收索引 `.build/bci-software-completion/acceptance.json` 状态 ACCEPTED_LOCAL_SOFTWARE，记录66组857次执行、零失败/跳过（含11项AI）、实际VFS/VMA合成内核设备guest、新system与candidate-55四模式启动。该结果只绑定索引内的历史源码快照；本节更新或后续功能修改不能借用它作为新候选验收。

内核传统 `read` 仍返回 s32 纳伏；快照 v1 提供带版本/单位/通道/速率/游标/token 的不可变批次头及完整帧对齐 payload，不包含每帧设备时间、sequence 或质量字段。用户态 `bci` JSONL 提供 host receive/processing 时间、sequence、generation、quality、逐通道 gain 和固件信息；固件 banner 不是唯一物理板卡身份。candidate-54 与上一轮candidate-55均保留；candidate-55是dirty源码私有诊断镜像，未作为clean-source正式发布版验收。

当前按 `plans/xtos-remaining-development-plan.md` 逐项开发。阶段1的冻结本地软件候选已验收，索引 `.build/xtos-roadmap-v1/phase1-acceptance.json` 为 `ACCEPTED_LOCAL_SOFTWARE`：共享寄存器解析、内核实际配置与增益读回、配置 UAPI/SDK、非复位 SHORTED/P/N 阻抗诊断和外部参考拟合/显式应用完成组件与集成 SPEC/QUALITY 审查；完整维护80组1077次执行、零失败/跳过（包含11项AI），源码快照与34项既有产物恢复一致。新candidate-56镜像的实际已安装命令、三模式PTY、合成Linux设备配置/SDK/旧快照VMA与解绑路径、校准拟合/应用及BIOS/UEFI×CD/USB四模式通过。配置结构的32位独立布局编译与原生布局比较通过；这不代表32位 compat_ioctl 已运行。

阶段1验收只绑定索引记录的冻结源码与candidate-56；本状态更新和后续阶段改动不追溯套用该PASS。candidate-56为dirty源码私有诊断镜像，继承已锁定用户态与early initramfs，重新构建当前内核/模块/SDK/CLI；不是clean-source正式发布验收。证据与原始失败/修复记录位于 `.build/xtos-roadmap-v1/`。阻抗名义估计、SHORTED观察自检和外部参考校准分别报告；固件v会复位配置，s停止采集不关闭lead-off电流。校准文件哈希只绑定输入字节和模型完整性，不证明参考仪器真实性或物理精度。真实Cyton/电极与硬件时延保持未验证。

阶段2的精确冻结本地软件版本已验收，索引 `.build/xtos-roadmap-v1/phase2-acceptance.json` 为 `ACCEPTED_LOCAL_SOFTWARE`，绑定源码快照 `bf860f3b7a0c7d70b81502649e84dbb499b3edf58b9437d504030cd313f129a1` 和 candidate-57 ISO `936fbca0ae047f714135d8550dbf2d6d5ae7866bbf80d9a68fd3f8e1d74d16d0`。逐帧版本化 UAPI/SDK、独立帧 ring、配置/流代次与质量元数据、只读不可变帧快照以及旧 read/snapshot 兼容已完成组件和集成 SPEC/QUALITY。完整维护82组1139次执行、零失败/跳过（11项AI已包含），34个既有产物叶节点精确恢复；实际Linux VFS/VM合成设备、镜像内已安装SDK的逐帧读/映射/fork/解绑/重载、旧接口、三种PTY诊断和离线校准以及BIOS/UEFI×CD/USB四模式通过。独立收尾审计核验481项输入哈希。candidate-57继续为dirty源码私有诊断镜像，显式继承已锁定用户态与early initramfs，并重新构建当前内核/模块/SDK/CLI；不代表clean-source正式发布、真实32位compat syscall、当前ARM64/RISC-V运行或物理设备验收。

阶段2的PASS仅覆盖索引记录的历史冻结源码和ISO。阶段3已在精确 clean 提交 `c026f55ef1f405df151f2a318531063a1a890fa6` 关闭本地软件验收，索引 `.build/continuation-c026f55ef/acceptance-index.json` 为 `ACCEPTED_LOCAL_SOFTWARE`。完整维护99组1310次执行、零失败/跳过（11项AI已包含），新编译内核及31个模块、维护配置BusyBox、SDK和原生工具；明确复用并校验锁定用户态、私有CPU运行时和liblsl。candidate-59 ISO `9e9b24eb50076e3f700d2e61c4e635bed7ab9856c4ac011dec38e175369378cb` 已完成完整嵌入载荷读回、镜像内已安装SDK的旧/逐帧read与snapshot/mmap、9个窗口case、原生VFS导出到两个签名16特征CPU/UDS数值窗、13个生命周期/模拟反馈方法（零跳过），以及BIOS/UEFI×CD/USB四模式启动。持久镜像在 `.build/clean-source-c026f55ef/.build/retained-candidate-59-clean-eeg/images/`；独立收尾审计核验22条输入记录和10项关闭criteria后由父代理验收。合成输入始终 `no_action`，算术ONNX fixture不代表经训练的脑电识别模型；CLI终态失效测试不单独作为CLI端到端成功推理证据。

上述P3 PASS仅绑定该提交与ISO；本状态更新和P4新改动不继承此PASS，真实板卡、物理延迟和公开发行仍单列。P4已进入真实Linux串口传输与受限Cyton默认配置接入开发；现有同步内存fixture适配保持兼容。P4在聚焦测试、独立审查、完整维护及安装产物门全部完成前保持 `in_progress`。

多设备插件保留同步内存fixture接口，并新增Linux非阻塞串口传输、固定Cyton v3.1.2默认配置解析和复用canonical decoder的会话适配。Cyton源码检查点 `47c8bc8299b7bc8e78672acd6035b6a94084284c` 已提交私有main，完整维护103组1374次执行、零失败/跳过。Ganglion v2.0.1离线codec及fixture-only适配器检查点 `0ab8a4cfd35c2f71e2e5b4753887c97421d57c8b` 已提交私有main；修复固件ASCII/LF/空207/严格206长度和跨代控制队列问题，独立增量SPEC/QUALITY通过，完整维护106组1396次执行、零失败/跳过（包含11项AI）。官方函数主机编译向量与204通知/404样本回放属于源码/合成证据，不是BLE抓包。

Ganglion Linux BLE 异步传输与会话的源码检查点 `894d7321f6412ac31b5299e4ec2e3345171bbc00` 已提交私有main。入口通知、跨线程投递、代次隔离与断连/reader竞争已完成独立SPEC增量和QUALITY复核；完整维护107组1435次执行、零失败/跳过（包含11项AI）。安装源码在私有CPython3.11.15执行36项fixture测试通过，真实Bleak3.0.1经独立D-Bus模拟服务完成XTOS会话；不代表无线或镜像验收。安装器只安装源码，不自动捆绑Bleak或修改CPU运行时/Live基础锁。

可选 provider 实验器 `tools/xtos-ai-provider-experiment/provider_experiment.py` 已在源码检查点 `41683a1411d262ded48735f2978113fe66857f90` 完成独立复核及完整维护，并提交私有 main。显式选择 CPU/CUDA/ROCm，拒绝缺失 provider 和静默回退，校验实际载入的不可变模型字节、张量和零输入 smoke，成功数值结果固定 `safe_action=no_action`。签名模型信任与生产 worker 仍严格 CPU-only；实验器为 SBOM excluded 开发源，不进入 Live 七文件 runtime。主机仅完成实际 CPU 推理；CUDA/ROCm 实际执行、性能和实时性保持未验证。

P4 已安装设备镜像切片已在准确 clean 提交 `41683a1411d262ded48735f2978113fe66857f90` 验收，索引 `.build/p4-image-41683a141/acceptance-index.json` 为 `ACCEPTED_LOCAL_SOFTWARE_IMAGE_SLICE`。完整维护108组1456次执行，零失败/跳过（含11项AI），另一次严格 clean-source 11项AI通过。candidate-60 ISO `72073a052bbfc8a60b1df3b7200fbfd2e51e502e205a1803723cd572b531638f` 完整嵌入载荷读回、八个已安装模块路径/哈希、普通xtos用户normal与优化模式各5项设备case、错误哈希导入前拒绝及BIOS/UEFI×CD/USB四模式均通过。SDK/原生工具为本提交重新编译，内核/31模块和BusyBox按未变相关源树及实际产物绑定复用c026f55ef原编译；不宣称fresh kernel。持久ISO位于 `.build/clean-source-41683a141/.build/retained-candidate-60-p4-devices/images/`，19项验收输入及9项保存文件已由父代理读回。四次测试端早期失败保留，最终a5通过后才写验收索引。

该PASS仅绑定上述历史提交和ISO，后续网络源码不继承它。显式IP/端口TCP字节传输随后在准确提交 `5cb533e4be467dc7e93bec684aec1367a1bb1784` 完成独立SPEC/QUALITY、完整维护111组1476次执行（含11项AI）、零失败/跳过，已私有提交并读回main。源码验收索引 `.build/p4-network-next/acceptance-index.json`；历史TDD快照覆盖PARTIAL保留，不补造RED。

同一clean提交的candidate-61已完成本地已安装TCP镜像验收，索引 `.build/p4-image-5cb533e4b/acceptance-index.json` 为 `ACCEPTED_LOCAL_SOFTWARE_IMAGE_SLICE`。ISO `2a98b39468e575eeb444eae8053b462dae1b382425ea079d00c656ef158ebc53` 的三个嵌入载荷、九个设备模块路径/哈希、普通xtos用户normal与优化模式各6项case（含真实IPv4 TCP loopback）、错误哈希导入前拒绝、严格clean-source11项AI及BIOS/UEFI×CD/USB四模式通过；sentinel均未变。SDK/原生工具重新编译，内核/31模块与BusyBox继续按相关源树、配置和实际产物绑定复用c026f55ef，不宣称fresh kernel。持久ISO在 `.build/clean-source-5cb533e4b/.build/retained-candidate-61-p4-tcp/images/`。guest测试工具的合法短读误判原FAIL、独立修复与增量PASS均保留。

P4已支持的本地软件交付矩阵在 `.build/p4-image-5cb533e4b/p4-delivery-matrix.json`；未知厂商网络线协议和认证会话仍是未实现项，不能改称只有硬件未验证。PTY、注入GATT和guest loopback保持 `fixture_replay`、`physical_device_evidence=false`、`no_action`。物理USB/UART、BLE持续采样/断连恢复、设备身份和物理时延未验收，P4整体不宣称完成。

P5生产签名provider的软件切片已在准确提交 `0ee8b87f367e1cd59e996d4d5bf0bf91ef0e2e21` 验收并私有推送，索引 `.build/p5-provider-next/acceptance-index.json`。保留v1 CPU-only，v2签名绑定显式CPU/CUDA/MIGraphX、runtime分发文件/ABI和子进程CPU放置；有限恢复每代次最多两次，缺GPU拒绝且不回退。组件和共享SPEC/QUALITY通过；完整维护118组1515次执行、零失败/错误/跳过（包含11项AI），另一次干净提交严格AI11项、安装副本实际签名CPU3项通过。source-only七模块归档已读回；本验收没有新整机ISO，candidate-61仍只绑定原提交。原worker/server/preflight失败与离线测试超时、后续真实回归和独立修复均保留；历史TDD限制未改写。CUDA/MIGraphX实际推理、性能、外部驱动供应链与RT共存未验证，CPU依赖锁不变。

P6 应用层 OpenXR loader、逐点转换、事件 barrier 与多模态模拟确认软件切片，已在精确提交 `155beef4e3eadb0824bf4dd7fd3ead73765b4bb2` 验收并私有推送，索引 `.build/p6-xr-next/acceptance-index.json`。SPEC/QUALITY、124 组 1563 次完整维护（含 AI11，零跳过）、干净提交 AI11/安装5/控制桥4，以及 35 文件安装归档解包执行均通过。所有模拟回执 no_action；历史 TDD PARTIAL 和原始失败保留。该验收不覆盖本次内核升级、新整机、图形会话或真实眼动 action/space；真实头显、校准、物理延迟仍未验证。自定义 `sched_class` 受实机前置决策门约束，尚未实现。

## 历史状态记录

以下历史文件和段落保留用于追溯，不代表当前实现和验证结论。旧记录中的 `/root/xtos-project`、阶段编号、待办项和性能数字均是历史上下文；当前状态以上文和 `docs/system_build.md` 为准。

- `docs/` 保留技术规格、调度器设计、驱动设计、信号处理、系统构建和验证边界。
- `plans/` 保留开发计划和后续任务。
- `FINAL_STATUS_SUMMARY.md`、`PROJECT_STATUS.md`、`TASK_SUMMARY.txt` 等保留为历史开发记录。