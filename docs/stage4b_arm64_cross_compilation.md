# 阶段 4B：ARM64 交叉编译与 QEMU 验证

**状态**: 已完成 ✅  
**日期**: 2026-10-02  
**目标**: 为 XTOS 实时系统添加 ARM64/AArch64 架构支持

---

## 目标

实现 XTOS 系统的 ARM64 交叉编译能力，使其可在 ARM 嵌入式平台、单板计算机和服务器上运行。

## 完成内容

### 1. ARM64 交叉编译脚本

**文件**: `scripts/build_system_arm64.sh`

**功能**:
- 使用 `aarch64-linux-gnu-gcc` 工具链交叉编译完整系统
- 构建 Linux 6.6.0-rt15 ARM64 内核（启用 PREEMPT_RT）
- 交叉编译 BusyBox 1.37.0（静态链接）
- 交叉编译 XTOS 用户态库（`libxtos-rt`、`libxtos-signal`、`libxtos-bci`）
- 生成 ARM64 initramfs

**构建阶段**:
1. 内核：ARM64 defconfig + PREEMPT_RT + Virtio 驱动
2. BusyBox：静态构建
3. XTOS 库：交叉编译为静态库
4. Init 和测试工具：静态链接的 ARM64 二进制
5. Initramfs：打包 CPIO 归档

### 2. QEMU 验证脚本

**文件**: `scripts/verify_system_arm64.sh`

**功能**:
- 使用 QEMU `virt` 机器启动 ARM64 内核
- Cortex-A57 CPU 仿真
- 验证内核启动、PREEMPT_RT、init 进程
- 检测内核 panic
- 生成验证报告

**QEMU 配置**:
- 机器：`virt` (ARM Virtual Machine)
- CPU：`cortex-a57` (ARMv8-A)
- 内存：2GB
- 核心数：2
- 串口：`ttyAMA0` (PL011 UART)
- 外设：Virtio 块设备、网络、控制台

### 3. RT 配置工具

**文件**: `scripts/enable_rt_config.sh`

**功能**:
- 为任意内核 `.config` 文件启用 PREEMPT_RT
- 使用内核自带的 `scripts/config` 工具或 sed 回退
- 禁用不兼容的抢占模式（`PREEMPT_NONE`、`PREEMPT_VOLUNTARY`）

### 4. 文档

**文件**: `docs/arm64_cross_compilation.md`

**内容**:
- ARM64 交叉编译流程
- 工具链依赖
- 构建产物说明
- QEMU 验证方法
- 真实硬件部署计划
- 故障排查指南

## 当前状态

**已完成**: ARM64 交叉编译与 QEMU 验证全部通过（2026-10-02）

**QEMU 验证结果**（`./scripts/verify_system_arm64.sh`，5 通过 / 0 失败）:
- ✓ 内核启动（Linux 6.6.0-rt15，aarch64）
- ✓ ARM64 架构（physical CPU 0x411fd070 = Cortex-A57）
- ✓ PREEMPT_RT 启用（rt kernel release）
- ✓ Init 进程启动（BusyBox init → shell）
- ✓ 无内核 panic

**XTOS 用户态测试**（QEMU virt 内实际执行，initramfs 内静态二进制）:
- `test_rt`: PASS（12 assertions）
- `test_spsc`: PASS（28 assertions）

**In-guest 系统验证**（`./scripts/verify_guest_arm64.sh`，14 通过 / 0 失败，2026-10-02 第二轮）:
- ✓ PREEMPT_RT active（/sys/kernel/realtime=1）+ CONFIG_PREEMPT_RT=y（ikconfig）
- ✓ XTOS 调度器编译进内核（XTOS_SCHED=y，含 HZ_1000）
- ✓ 三个 XTOS 内核模块在 ARM64 guest 内真实加载（xtos_bci_core / xtos_bci_buffer / openbci_cyton，经 /sys/module 存在性确认）
- ✓ test_rt（12）/ test_spsc（28）/ test_signal（181 assertions）全部通过
- ✓ 正常 poweroff、无 panic

至此 ARM64 与 x86_64 的 QEMU 验证内容对齐（此前 ARM64 内核未启用 XTOS 组件，模块项为 ⚠ 跳过）。

**已验证**:
- 内核: `.build/kernel-install-arm64/Image`（34MB，6.6.0-rt15，PREEMPT_RT + XTOS_SCHED + XTOS_BCI=m + HZ_1000）
- Initramfs: `.build/initramfs-arm64.cpio.gz`（72MB，含 BusyBox + test_rt/test_spsc/test_signal/bci_test + XTOS 内核模块）
- DTBs: `.build/kernel-install-arm64/dtbs/`
- 工具链: aarch64-linux-gnu-gcc 14.2.0
- QEMU: qemu-system-aarch64 10.0.13

## 构建过程中修复的问题

1. **Stage 4 链接失败（libxtos-bci 架构不匹配）**: 首次构建时 sdk 树中残留 x86_64 的 `libxtos-bci.a`，导致 Stage 4 测试程序静态链接失败。修复：确保 Stage 3 以 `CC=aarch64-linux-gnu-gcc` 全量重编三个库，并在脚本中加入 aarch64 归档格式校验（非 aarch64 直接报错退出）。
2. **test_rt / test_spsc 缺失于 initramfs**: 01:22 打包的 initramfs 早于库修复，不含测试二进制。修复：重新链接两个测试并重新打包。
3. **验证脚本 PREEMPT_RT 误报**: 6.6-rt15 内核启动横幅不再打印 "PREEMPT_RT" 字样（显示为 `#1 SMP PREEMPT`）。修复：改为匹配 `Linux version …-rtN` 版本字符串；RT 状态另经 `include/config/auto.conf` 中 `CONFIG_PREEMPT_RT=y` 确认。
4. **验证脚本 set -e 中止**: `((PASS++))` 在 PASS=0 时返回非零，`set -e` 下导致检查面板输出一列即退出。修复：改为 `PASS=$((PASS+1))` 算术展开。
5. **initramfs 内 `/init` 与 `init=` 参数**: initramfs 存在 `/init` 时内核忽略 `init=` 参数。RT 自动化检查通过临时将 `/init` 指向 `rt_check.sh` 完成（验证后已还原为 BusyBox init；正式化后由 `scripts/verify_guest_arm64.sh` + `system/rootfs/xtos-guest-check` 取代）。
6. **内核态浮点编译失败（`-mgeneral-regs-only`）**: ARM64/RISC-V 内核禁用浮点指令，BCI 驱动的 float 类型（阻抗数组、输入量程）无法编译，XTOS_BCI 无法在 ARM64 启用。根治：内核态与 UAPI 整型化（阻抗 `__u32` kOhm、量程 `__u32` mV），ioctl 编号不变（float 与 u32 同为 4 字节），用户态 sdk/session.c/bci_test.c 同步修改；x86_64 用户态全量测试无回归。
7. **`file` 命令判别归档不可靠**: file 5.46 对静态库只报 "current ar archive" 不含架构。归档校验改为交叉 objdump（ARM64: `architecture: aarch64`；RISC-V: `architecture: riscv:rv64`）。

## ARM64 特定配置

### 内核驱动

相比 x86_64 构建，ARM64 内核启用了：

```
CONFIG_SERIAL_AMBA_PL011=y           # ARM PL011 UART
CONFIG_SERIAL_AMBA_PL011_CONSOLE=y   # 控制台支持
CONFIG_VIRTIO_MMIO=y                 # 内存映射 Virtio
CONFIG_VIRTIO_BLK=y                  # Virtio 块设备
CONFIG_VIRTIO_NET=y                  # Virtio 网络
CONFIG_VIRTIO_CONSOLE=y              # Virtio 控制台
CONFIG_HW_RANDOM_VIRTIO=y            # 虚拟 RNG
CONFIG_DRM_VIRTIO_GPU=y              # Virtio GPU
```

禁用了 x86 特定选项：
```
CONFIG_X86_INTEL_LPSS=n
CONFIG_X86_PLATFORM_DEVICES=n
```

### 静态链接

所有用户态二进制都使用静态链接，避免动态库依赖：

```bash
# BusyBox
CONFIG_STATIC=y

# XTOS 测试工具
aarch64-linux-gnu-gcc -static ...
```

### PREEMPT_RT 状态

ARM64 内核完整支持 PREEMPT_RT：
- `CONFIG_PREEMPT_RT=y`
- 高分辨率定时器
- 线程化中断处理

真实硬件上的实时性能取决于硬件中断延迟和时钟源质量（QEMU 中无法验证）。

## 验证标准

### QEMU 验证（自动化）

**必需检查**:
- ✓ 内核启动
- ✓ ARM64 架构检测
- ✓ PREEMPT_RT 启用
- ✓ Init 进程启动
- ✓ 无内核 panic

**可选检查**（可能需要适配）:
- XTOS BCI 模块加载
- XTOS 调度器激活
- BusyBox 初始化
- 系统测试标记

### 真实硬件验证（未来）

**目标平台**:
- Raspberry Pi 4/5 (BCM2711/BCM2712)
- NVIDIA Jetson Nano/Xavier (Tegra)
- RK3588 单板机（Orange Pi 5、Rock 5）
- ARM 服务器平台（Ampere Altra、AWS Graviton）

**硬件特定需求**:
- 目标板设备树 blob (DTB)
- Bootloader 配置（U-Boot、GRUB）
- 存储设备驱动（SD/MMC、NVMe）
- 真实硬件实时验证（裸机 cyclictest）

## 已知限制

1. **QEMU 非实时**: 虚拟时钟源（`arm,armv8-timer`）无法提供确定性延迟
2. **无硬件验证**: ARM64 构建仅在 QEMU 中验证
3. **模块兼容性**: XTOS BCI 驱动可能需要适配 ARM 串口硬件
4. **设备树**: 使用通用 `virt` DTB；真实板卡需平台特定 DTB

## 下一步

1. ✅ 创建 ARM64 交叉编译脚本
2. ✅ 创建 QEMU 验证脚本
3. ✅ 创建 RT 配置工具
4. ✅ 创建技术文档
5. ✅ 构建完成（内核 + BusyBox + XTOS 库 + 测试二进制）
6. ✅ QEMU 验证（5/5 通过，test_rt 12 断言 / test_spsc 28 断言全部 PASS）
7. ✅ 分析验证结果（见上文"构建过程中修复的问题"）
8. ⏳ 提交代码到仓库
9. ⏳ 准备真实硬件测试计划

## 阻塞项

- **无真实硬件**: 目前无 ARM64 开发板进行裸机验证（QEMU 中无法验证真实中断延迟）

## 构建产物

```
.build/
├── kernel-build-arm64/          # 内核构建目录
├── kernel-install-arm64/
│   ├── Image                    # 内核镜像（主要）
│   └── dtbs/                    # 设备树 blob
├── busybox-build-arm64/         # BusyBox 构建
├── rootfs-arm64/                # 根文件系统
│   ├── bin/busybox              # 静态 BusyBox
│   ├── sbin/init                # Init 二进制
│   ├── lib/modules/             # 内核模块
│   └── usr/lib/                 # XTOS 静态库
└── initramfs-arm64.cpio.gz      # 打包的 initramfs
```

## 参考资料

- ARM64 Linux 内核: https://www.kernel.org/doc/html/latest/arch/arm64/
- QEMU ARM 系统仿真: https://www.qemu.org/docs/master/system/target-arm.html
- Debian ARM64 交叉编译: https://wiki.debian.org/CrossCompiling
- ARM 上的 PREEMPT_RT: https://wiki.linuxfoundation.org/realtime/start
