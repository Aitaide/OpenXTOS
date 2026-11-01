# XTOS Real-Time Operating System

[English](#english) | [中文](#中文)

---

## English

### Overview

XTOS is a research-grade real-time Linux system for brain-computer interface (BCI) and signal processing applications. The current source targets signed upstream Linux 7.3.1 with mainline PREEMPT_RT. Kernel migration, complete-system acceptance and physical timing are separate gates; see `docs/kernel-7.3.1-migration.md` for current evidence and limits.

### System Architecture

**Core Components:**

- **Real-Time Kernel**: Linux 7.3.1 + mainline PREEMPT_RT
  - Full preemption support (`CONFIG_PREEMPT_RT=y`)
  - High-resolution timers (1000 Hz)
  - Optional XTOS scheduler helper (`CONFIG_XTOS_SCHED`); it is not a Linux `sched_class` and does not replace normal Linux task selection

- **BCI Framework**: Kernel-space BCI subsystem
  - XTOS BCI core (`CONFIG_XTOS_BCI=m`)
  - OpenBCI Cyton driver (`CONFIG_XTOS_BCI_OPENBCI=m`)
  - Serial device bus and tty controller built in (`CONFIG_SERIAL_DEV_BUS=y`)

- **User-Space Runtime**:
  - BusyBox 1.37.0 static userland
  - Real-time signal processing SDK
  - ONNX Runtime CPU inference baseline (optional)
  - PyTorch model export and parity validation (optional)
  - TensorFlow/TFLite model export and parity validation (optional)

### Signal Processing Capabilities

XTOS provides deterministic signal processing primitives for real-time applications:

- **Bandpass filtering**: Butterworth 2nd-order IIR (0.5–30 Hz for EEG)
- **SSVEP detection**: FFT-based frequency detection with SNR thresholding
- **P300 event-related potential**: Single-window feature extraction with cross-trial quality gating
- **Lock-free data structures**: SPSC ring buffer for inter-thread communication

All signal processing functions are validated with functional correctness tests, AddressSanitizer/UndefinedBehaviorSanitizer, ThreadSanitizer, and deterministic benchmarks.

### Machine Learning Integration (Optional)

XTOS supports CPU-based model inference for research purposes:

- **ONNX Runtime 1.23.2**: Single-threaded CPU execution provider with numerical error validation and latency profiling
- **PyTorch Export Parity**: Torch 2.6.0 → ONNX → ONNX Runtime with element-wise error and prediction class agreement
- **TensorFlow/TFLite Export Parity**: TensorFlow 2.18.0 → TFLite → comparison with ONNX Runtime output

**Scope Limitations**: The ML baseline represents user-space CPU inference capability only. It does not claim GPU acceleration, hard real-time guarantees for inference, medical-grade accuracy, or closed-loop control safety.

### Real-Time Validation Tools

For deployment on real x86_64 hardware, XTOS provides a complete real-time validation toolkit:

- **System Configuration Checker** (`tools/realtime-test/rt_system_check.sh`): Validates kernel configuration, CPU isolation, clock sources, and generates optimization recommendations
- **Long-Duration Latency Test** (`tools/realtime-test/rt_cyclictest.sh`): 24-hour cyclictest with SCHED_FIFO priority and CPU affinity
- **Results Analysis** (`tools/realtime-test/rt_analyze.py`): Statistical analysis and real-time classification (hard RT < 100 μs)

See `tools/realtime-test/README.md` for usage instructions. QEMU validation results do not represent real hardware latency characteristics.

### Build and Verification

**Prerequisites:**
- x86_64 Linux host with GCC toolchain
- Make, rsync, cpio, gzip
- QEMU (for system verification)

**Build Complete System:**
```bash
JOBS=4 ./scripts/build_system.sh
```

**QEMU Verification:**
```bash
QEMU_TIMEOUT=180 ./scripts/verify_system.sh
```

**User-Space Tests:**
```bash
./scripts/test.sh
```

**Optional ML Baselines:**
```bash
# ONNX Runtime (requires onnxruntime + numpy)
XTOS_ONNX_ROOT=/path/to/onnx-runtime ./scripts/benchmark_onnx.sh

# PyTorch Parity (requires torch + onnx + onnxruntime + numpy + protobuf)
XTOS_TORCH_ROOT=/path/to/torch \
  XTOS_ONNX_ROOT=/path/to/onnx-runtime \
  XTOS_PROTOBUF_ROOT=/path/to/protobuf \
  ./scripts/benchmark_torch_onnx.sh

# TensorFlow/TFLite Parity (requires tensorflow + onnxruntime + numpy)
XTOS_TF_ROOT=/path/to/tensorflow \
  XTOS_ONNX_ROOT=/path/to/onnx-runtime \
  ./scripts/benchmark_tf_onnx.sh
```

### Output Artifacts

After a successful build:
- Kernel image: `.build/images/xtos-bzImage`
- Root filesystem: `.build/images/xtos-rootfs.cpio.gz`
- Kernel config: `.build/images/kernel.config`

### Development

**Source Tree:**
```
xtos/
├── kernel/           # Linux 7.3.1 + mainline PREEMPT_RT + XTOS integration
├── drivers/xtos/     # BCI core, buffer, and device drivers
├── userspace/busybox/ # BusyBox 1.37.0 source
├── system/           # System configuration and rootfs
├── sdk/              # Real-time and signal processing libraries
├── tools/            # Benchmarks and utilities
└── scripts/          # Build and test automation
```

**Contributing:**

See [CONTRIBUTING.md](CONTRIBUTING.md) for development workflow and coding standards.

### Target Platforms

- **Verified in QEMU (full system + in-guest tests)**: x86_64, ARM64, RISC-V 64
- **Real hardware validation**: pending (development boards and OpenBCI device required)

### License

See [LICENSE](LICENSE) for terms.

### Status

Current development phase: **Developer Preview** — three-architecture (x86_64 / ARM64 / RISC-V 64) QEMU-validated system with fuzzing, SBOM, ABI versioning, and reproducible builds (see STATUS.md)

See [STATUS.md](STATUS.md) for detailed verification results.

**Scheduler/device gap-closure boundary**: The XTOS scheduler component remains an optional helper, not a Linux `sched_class`. The M0 evidence gate, scheduler-class RFC/lifecycle matrix, and EEG validation contract are implemented. A userspace OpenBCI Cyton serial capture/parser and eight-channel filter-replay slice is now implemented and PTY-tested; physical device identity, board gain/firmware, device recovery, kernel serdev/DMA integration, real workload and kernel scheduler-class integration remain open stage gates.

---

## 中文

### 概述

XTOS 是面向脑机接口（BCI）和信号处理应用的研究级实时 Linux 系统。当前源码采用经官方签名核验的 Linux 7.3.1 与主线 PREEMPT_RT。内核移植、完整系统验收和物理时延分别验证；当前证据与边界见 `docs/kernel-7.3.1-migration.md`。

### 系统架构

**核心组件：**

- **实时内核**：Linux 7.3.1 + 主线 PREEMPT_RT
  - 完全抢占支持（`CONFIG_PREEMPT_RT=y`）
  - 高精度定时器（1000 Hz）
  - 可选 XTOS 调度辅助层（`CONFIG_XTOS_SCHED`）；它不是 Linux `sched_class`，不会替换 Linux 正常任务选择

- **BCI 框架**：内核空间 BCI 子系统
  - XTOS BCI 核心（`CONFIG_XTOS_BCI=m`）
  - OpenBCI Cyton 驱动（`CONFIG_XTOS_BCI_OPENBCI=m`）
  - 内置串行设备总线及 tty 控制器（`CONFIG_SERIAL_DEV_BUS=y`）

- **用户态运行时**：
  - BusyBox 1.37.0 静态用户空间
  - 实时信号处理 SDK
  - ONNX Runtime CPU 推理基线（可选）
  - PyTorch 模型导出与对齐验证（可选）

### 信号处理能力

XTOS 提供用于实时应用的确定性信号处理原语：

- **带通滤波**：Butterworth 2 阶 IIR（0.5–30 Hz，用于 EEG）
- **SSVEP 检测**：基于 FFT 的频率检测，带 SNR 阈值
- **P300 事件相关电位**：单窗口特征提取与跨试次质量门禁
- **无锁数据结构**：用于线程间通信的 SPSC 环形缓冲区

所有信号处理函数均通过功能正确性测试、AddressSanitizer/UndefinedBehaviorSanitizer、ThreadSanitizer 和确定性基准测试验证。

### 机器学习集成（可选）

XTOS 支持基于 CPU 的模型推理用于研究目的：

- **ONNX Runtime 1.23.2**：单线程 CPU 执行提供者，带数值误差验证和延迟分析
- **PyTorch 导出对齐**：Torch 2.6.0 → ONNX → ONNX Runtime，带逐元素误差与预测类别一致性验证

**范围限制**：机器学习基线仅代表用户态 CPU 推理能力。不宣称 GPU 加速、推理硬实时保证、医疗级准确性或闭环控制安全性。

### 构建与验证

**前置要求：**
- x86_64 Linux 主机，带 GCC 工具链
- Make、rsync、cpio、gzip
- QEMU（用于系统验证）

**构建完整系统：**
```bash
JOBS=4 ./scripts/build_system.sh
```

**QEMU 验证：**
```bash
QEMU_TIMEOUT=180 ./scripts/verify_system.sh
```

**用户态测试：**
```bash
./scripts/test.sh
```

**可选机器学习基线：**
```bash
# ONNX Runtime（需要 onnxruntime + numpy）
XTOS_ONNX_ROOT=/path/to/onnx-runtime ./scripts/benchmark_onnx.sh

# PyTorch 对齐（需要 torch + onnx + onnxruntime + numpy + protobuf）
XTOS_TORCH_ROOT=/path/to/torch \
  XTOS_ONNX_ROOT=/path/to/onnx-runtime \
  XTOS_PROTOBUF_ROOT=/path/to/protobuf \
  ./scripts/benchmark_torch_onnx.sh
```

### 输出产物

构建成功后：
- 内核镜像：`.build/images/xtos-bzImage`
- 根文件系统：`.build/images/xtos-rootfs.cpio.gz`
- 内核配置：`.build/images/kernel.config`

### 开发

**源码树：**
```
xtos/
├── kernel/           # Linux 7.3.1 + 主线 PREEMPT_RT + XTOS 集成
├── drivers/xtos/     # BCI 核心、缓冲区和设备驱动
├── userspace/busybox/ # BusyBox 1.37.0 源码
├── system/           # 系统配置和根文件系统
├── sdk/              # 实时和信号处理库
├── tools/            # 基准测试和实用工具
└── scripts/          # 构建和测试自动化
```

**贡献指南：**

开发流程和编码规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

### 目标平台

- **QEMU 全系统级验证（含 in-guest 测试）**：x86_64、ARM64、RISC-V 64
- **真实硬件验证**：待完成（需要开发板和 OpenBCI 设备）

### 许可证

条款详见 [LICENSE](LICENSE)。

### 状态

当前开发阶段：**Developer Preview** —— 三架构（x86_64 / ARM64 / RISC-V 64）QEMU 验证系统，含 fuzz、SBOM、ABI 版本化与可复现构建（见 STATUS.md）

详细验证结果见 [STATUS.md](STATUS.md)。

**调度/设备缺口闭环边界**：XTOS 调度组件仍为可选辅助层，不是 Linux `sched_class`。M0 证据门禁、sched_class RFC/生命周期矩阵和 EEG 验收契约已实现。新增 OpenBCI Cyton 用户态串口采集/解析及八通道滤波回放切片，并通过 PTY 合成帧测试；真实板卡身份、gain/firmware、设备恢复、内核 serdev/DMA、真实 workload 和原生调度类集成仍待验收。
