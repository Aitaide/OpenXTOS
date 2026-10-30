# XTOS 后续功能逐项开发计划

> **For Hermes:** 使用 subagent-driven-development 按阶段独占路径实施，每个行为先 RED→GREEN，冻结后 SPEC→QUALITY；用户已授权逐项开发，无需重复索要开发确认。

**Goal:** 依次补齐设备功能、逐帧接口、EEG 推理反馈通路、多设备插件、GPU 后端和 XR 交互，并保留自定义调度类的实机 go/no-go。

**Architecture:** 先把已有 Cyton 采集能力补成具有真实配置来源和完整错误恢复的设备接口，再把完整帧接入有界用户态处理与软实时推理。后续传输和执行后端通过明确的能力契约扩展，测试输入、物理输入和可执行动作始终保留来源与权限标记。

**Tech Stack:** Linux 6.6.0-rt15、C11/pthreads/atomics、版本化 UAPI/SDK、Python 标准库、ONNX Runtime、PTY、host syscall shims、ASan/UBSan/TSan、QEMU。

## 基线与执行顺序

- 开工 HEAD：3f6d63e4b1518e5e923fdb77c90c0d73d8aba382，main，已有大量用户认可的 dirty 改动，完整保留。
- 上一轮 `.build/bci-software-completion/acceptance.json` 为历史验收，candidate-55 保留不动。本计划任何源码改动不追溯套用上一轮 PASS。
- 每个阶段先完成实现、聚焦测试、双重复核和维护；后续阶段只允许先做只读资料调查，不在前一阶段尚未关闭时并行修改依赖代码。
- 证据根 `.build/xtos-roadmap-v1/`；进度及命令/退出码/源码哈希放 ignored 证据目录。磁盘不足时先清点本次可重建冗余，不删除历史镜像、证据或用户文件。
- 主代理拥有共享集成文件（scripts/test.sh、patch、SBOM、STATUS.md、SDK 公共 UAPI），独立实现只修改授予路径；冻结前不运行全局构建或复制移动中的源码。
- 各阶段的 detailed contract 在实施前写入 docs/；严禁将后加测试称为 TDD，严禁用合成结果填实机数字。

## 阶段 1：Cyton 设备功能

### Task 1.1：配置解析共享化

Files：新增 `kernel/linux/drivers/xtos/bci/xtos_cyton_config.h`（与现有共享 packet helper 同目录）；修改 `tools/xtos-cyton/cyton_serial.c`；新增 `tests/test_cyton_config.py`、`tests/cyton_config_host/config_test.c`。

1. 独立测试覆盖官方 Board ADS Registers 的八个 CHnSET 地址、byte/bit 一致性、gain 1/2/4/6/8/12/24、powerdown、重复/缺失/Daisy 拒绝、长度与输出不变。
2. 运行 `python3 -B tests/test_cyton_config.py -v`，缺少共享 header 的编译失败作为入口 RED；之后每项行为先独立失败再实现。
3. 共享纯 C、无浮点/分配/全局可变状态的长度有界解析器；逐行读取不依赖 strtok/libc。失败不修改调用方配置。
4. 用户态串口使用共享解析器，保留原有 PTY 语义；运行 `python3 -B tools/xtos-cyton/test_cyton_serial.py` 和 CLI 回归。

### Task 1.2：内核固件/寄存器命令读回

Files：修改 `kernel/linux/drivers/xtos/bci/devices/openbci_cyton.c`、`xtos_cyton_packet.h`、`tests/cyton_driver_host/{driver_test.c,shim.h}`、`tests/test_cyton_driver.py`；必要时新增局部命令 helper。

1. 增加实际驱动的 host case：多次 receive 分片 text reply、完整 v/?、缺失/坏 gain、超时、同步 TX 内收包、START 前拒绝未知配置、取消与 cleanup，观察真实 RED。
2. 有界应答 buffer 和 interruptible timeout；receive 回调可在采集停止时接收命令应答；state lock 不跨同步 TX/等待。
3. 每次开始采集前读回已知配置，使用各通道实际 gain，signed32 不可表示返回明确降级错误，不裁剪；控制写后读回，不相信写成功等于设备生效。
4. 普通/ASan/UBSan/TSan 和实际内核模块构建；保留模块/旧接口寿命语义。

### Task 1.3：电极阻抗与校准自检合同

Files：新增 `docs/cyton-device-functions.md`；新增测量 helper/test；修改驱动回调、SDK 和 CLI 的精确文件在合同冻结后确定。

1. 查官方 firmware 和 ADS1299 定义，固定命令、lead-off 电流/频率、增益、串联电阻和配置恢复协议，不编造板卡校准命令。
2. 区分内部 test signal 自检、ADC offset 校准与有可溯源参考的物理校准。只实现协议和输入可以真实支持的行为；未知标为不可执行而非返回成功。
3. 先测试已知正弦幅值→阻抗、异常波形/饱和/缺帧拒绝和设置→采样→计算→恢复读回事务；kernel 只用整数。
4. 最终结果标明测量输入、单位和 reference；错误/中断/拔出始终尝试停流与恢复，恢复失败不能产生成功测量。
5. host/PTY 软件可验证流程，不等于真实电极接触或医学精度验收。

### Task 1.3b：外部参考校准模型

Files：新增 `tools/xtos-cyton/bci_calibration.{h,c}`、`test_bci_calibration.c`、`tests/test_bci_calibration.py`。

- 内部SHORTED只给观察统计，不自动生成系数。外部参考模式要求显式提供已知参考nV与对应观测nV、有界多点覆盖、来源类型和误差门槛；线性拟合得到offset/scale和残差，再按显式启用应用。
- 缺少参考、单一电平、反向/奇异增益、超容差残差、溢出、来源不匹配都失败；系数/应用输出错误时不变，保留raw不覆盖。
- 固定可计算的多电平向量和独立Python oracle先RED后实现；合成拟合不标为物理校准通过，实际板卡/参考仪器/采集来源的可追溯身份留独立验证门。

### Task 1.4：配置 UAPI/SDK 与阶段验收

Files：kernel/SDK UAPI、session.c、实际 syscall wrapped SDK tests、scripts/test.sh、patch/SBOM、STATUS。

1. 新增固定宽度版本化配置查询，不改变现有 ioctl 结构大小；SDK 只复制验证后的读回数据。
2. 先失败测成功查询、版本/保留字段、EPROTO/ENODEV、输出不变；32/64-bit layout 单列。
3. 同步内核补丁、SBOM 和维护入口，先独立 SPEC 再 QUALITY，聚焦回归冻结后完整维护。
4. 产物需要新 candidate，不能重新贴标旧 candidate-55。根据空间先验收模块/SDK/CLI，再选择无重复大文件的新镜像构建；镜像门没完成不得写阶段发布完成。

## 阶段 2：版本化逐帧数据面

Files：新 `include/uapi/linux/xtos_bci_frame.h` 及 SDK 镜像、BCI ring/core/snapshot、SDK、tests/bci_kernel/。

- 保持 v1 read/snapshot 兼容；新 ABI 显式包含 host receive 时间域、序号、quality、generation、单位、实际配置引用，设备时间只在协议能证明时提供。
- 先测 layout、帧对齐、sequence wrap、覆盖/缺口、generation、token、解绑及跨 FD/VMA 寿命，再实现；不能从缺少历史 metadata 的旧 ring 猜出逐帧信息。
- 实际 Linux guest 的 VFS/VM/SDK 运行后才关闭接口门。

## 阶段 3：EEG 推理反馈通路

Files：`tools/xtos_eeg_loop/`、新实时窗口/特征模块、`tools/xtos-ai-runtime/`、`tools/xtos_action_gate/`、对应测试。

- 固定容量窗口和代次/缺口重置；特征版本/模型输入形状/通道/单位一致；CPU 运行时现有超时/低质量/低置信度降级保留。
- 真实采集与 fixture 分别携带来源，模型和特征契约须匹配；fixture 始终 no_action。
- 明确动作候选、单次确认、有效期、撤销与重复抑制，先接无危险模拟输出。未受训模型不宣称真实脑电识别准确率。
- 先写窗口黄金输出、推理实际 UDS 往返、断流/过期/重复动作/确认竞争测试，再接通生产链路。

## 阶段 4：多设备插件

Files：新增 `tools/xtos-devices/` 和 transport/driver contracts、测试与安装入口。

- 串口、USB、BLE、网络使用统一 sample/capability/lifecycle 合同，有界帧和取消/重连/身份验证。
- 先把现有 Cyton 适配进去，再按官方已公开协议实现 Ganglion；其他厂商需要明确协议和 SDK 许可，协议缺失不虚构兼容。
- 每个插件分别做协议向量、模拟 transport 和失败恢复；物理 USB/BLE 运行单列。

## 阶段 5：GPU 后端

Files：`tools/xtos-ai-runtime/xtos_ai_runtime/{protocol,worker,server,trust,client}.py`、模型合同/测试、runtime 构建 profile。

- 增加显式 CPU/CUDA/ROCm provider 选择、支持能力和依赖不可用错误；不静默 fallback。
- 独立 worker 的时间预算、队列、取消、重启和 CPU/IRQ 隔离，测试错误与恢复；支持的 provider 和模型签名 claims 同步更新。
- 不能用 provider 枚举或 mock 代替真实 GPU 推理；没有 GPU 可完成代码/CPU 回归，GPU 性能和驱动 RT 共存保持未验证。

## 阶段 6：XR 与多模态

Files：新增 `tools/xtos-xr/`、multimodal contract、模拟 runtime tests。

- OpenXR 在应用层，EEG/眼动/EMG 时间域对齐、质量和 freshness 检查、动作确认/撤销。
- 先模拟事件与 runtime 验证完整流程；厂商设备和真实头显延迟单列，不操作危险执行机构。

## 阶段 7：调度类前置决策

Files/任务以 `plans/xtos-scheduler-class-development-plan.md` 为准。

- 用户态模型和 RFC 不等于 Linux sched_class。M0 真实 workload、预算依据、预登记收益门槛和每策略至少十次有效物理运行后，独立 reviewer 决定停止或进入 M2。
- M0/M1 未批准前不写调度核心原型；若原生策略足够，形成明确 no-custom-scheduler 决策。
- 自定义调度类开发若被批准，按单 CPU→UAPI→SMP/PI/hotplug→跨架构→硬件对照→回退发布逐门执行。

## 最终交付边界

每阶段交付实际可执行代码、测试命令/退出码、独立审查、完整维护和绑定源码/产物哈希的验收记录。正式 clean-source 发布、真实 Cyton/电极、GPU、ARM64/RISC-V 板卡、XR 设备和持续物理延迟分别列验收门。未获明确授权不重启 H3C、改 BIOS/电源、写物理硬盘、提交公开仓库或发布镜像。
