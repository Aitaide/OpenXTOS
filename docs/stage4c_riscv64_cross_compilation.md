# 阶段 4C：RISC-V 64 交叉编译与 QEMU 验证

**状态**: 已完成 ✅
**日期**: 2026-10-02
**目标**: 为 XTOS 实时系统添加 RISC-V 64 (RV64GC) 架构支持，达到与 x86_64/ARM64 对等的 QEMU 系统级验证

---

## 目标

实现 XTOS 系统的 RISC-V 64 交叉编译能力，在 QEMU 中完成与 x86_64/ARM64 对等的系统级验证（RT 内核 + XTOS 内核组件 + BusyBox + XTOS 用户态库 + 静态测试二进制）。这完成了开发计划（Rev.2）阶段 4 第 3 条的 QEMU 部分："RISC-V：QEMU 启动和用户态库构建，再寻找真实板卡"。

## 架构支持

- Linux 6.6.0-rt15 补丁集**原生包含 RISC-V PREEMPT_RT 支持**（`arch/riscv/Kconfig` 中 `select ARCH_SUPPORTS_RT`，补丁为 riscv 打了 21 处修改）
- XTOS 内核组件（`kernel/sched/xtos_sched.c`、`drivers/xtos/bci/*`）为架构无关代码（内核态浮点已在前置修复中整型化）
- 主机已具备：`riscv64-linux-gnu-gcc 14.2.0`、`qemu-system-riscv64 10.0.13`、OpenSBI 1.6 固件（`/usr/share/qemu/opensbi-riscv64-generic-fw_dynamic.bin`）

## 实现内容

### 1. RISC-V 64 交叉编译脚本

**文件**: `scripts/build_system_riscv64.sh`

与 ARM64 版结构一致的 5 阶段：

1. **内核**: `riscv defconfig` + PREEMPT_RT + 合并 `kernel/xtos_defconfig`（XTOS_SCHED=y / XTOS_BCI=m / XTOS_BCI_OPENBCI=m / HZ_1000）+ QEMU virt 外设（16550 UART 控制台、virtio-mmio），config 生效校验
2. **BusyBox**: 静态构建（禁用 x86 SHA 硬加速和 tc）
3. **XTOS 库**: libxtos-rt / libxtos-signal / libxtos-bci 交叉编译为静态库，用 `riscv64-linux-gnu-objdump` 校验归档架构
4. **测试工具**: test_rt / test_spsc（-lxtos 全家桶）、bci_test 示例、test_signal（对齐 x86_64 guest 内容）
5. **Initramfs**: CPIO+gzip 打包，BusyBox init

### 2. QEMU 验证脚本

- `scripts/verify_system_riscv64.sh`: 基础启动验证（QEMU `virt` + `rv64` + OpenSBI `fw_dynamic`，检查内核启动、OpenSBI、PREEMPT_RT（`-rtN` 版本串）、Init 启动、无 panic；输出 `.build/qemu-riscv64-output.txt`）
- `scripts/verify_guest_riscv64.sh`: in-guest 系统验证（加载 XTOS 内核模块、运行全部测试二进制，方法同 ARM64）

### 3. RISC-V 特定配置

```
CONFIG_SERIAL_8250=y                # 16550 UART (QEMU virt 0x10000000)
CONFIG_SERIAL_8250_CONSOLE=y        # 控制台
CONFIG_SERIAL_EARLYCON=y
CONFIG_SERIAL_OF_PLATFORM=y         # 设备树枚举
CONFIG_VIRTIO_MMIO=y                # virtio-mmio 总线
CONFIG_VIRTIO_BLK=y / NET / CONSOLE
CONFIG_HW_RANDOM_VIRTIO=y
CONFIG_DRM_VIRTIO_GPU=y
```

控制台参数 `console=ttyS0`（16550），与 ARM64 的 `ttyAMA0`（PL011）不同。

启动链: OpenSBI (M-mode) → Linux 6.6.0-rt15 (S-mode) → init (U-mode)。

## 验证结果（2026-10-02）

**基础启动验证**（`./scripts/verify_system_riscv64.sh`，5 通过 / 0 失败）:
- ✓ 内核启动（Linux 6.6.0-rt15）
- ✓ OpenSBI 固件
- ✓ PREEMPT_RT 启用（rt kernel release）
- ✓ Init 启动（BusyBox init → shell）
- ✓ 无内核 panic

**In-guest 系统验证**（`./scripts/verify_guest_riscv64.sh`，15 通过 / 0 失败）:
- ✓ OpenSBI 固件启动
- ✓ PREEMPT_RT active（/sys/kernel/realtime=1）
- ✓ CONFIG_PREEMPT_RT=y（ikconfig）
- ✓ XTOS 调度器编译进内核
- ✓ 三个 XTOS 内核模块真实加载（xtos_bci_core / xtos_bci_buffer / openbci_cyton，经 /sys/module 存在性确认）
- ✓ test_rt / test_spsc / test_signal 在 guest 内全部通过
- ✓ 正常 poweroff、无 panic

**构建产物**:
- 内核 `.build/kernel-install-riscv64/Image`（22MB，6.6.0-rt15，PREEMPT_RT + XTOS_SCHED + XTOS_BCI）
- initramfs `.build/initramfs-riscv64.cpio.gz`（6.9MB，含 BusyBox + 测试二进制 + XTOS 模块）
- rootfs `.build/rootfs-riscv64/`

**x86_64 回归**: 内核态浮点整型化（UAPI 整数化）改动后，`./scripts/test.sh` 用户态全量测试通过（含 C/Python 信号对照），无回归。

## 构建过程中修复的问题

1. **objdump 架构串**: riscv64 objdump 输出 `architecture: riscv:rv64`（带冒号子架构），与 ARM64 风格（`architecture: aarch64`）不同。归档校验匹配串修正。
2. 内核态浮点问题（`-mgeneral-regs-only`）在 ARM64 阶段已从源头修复（UAPI 整型化），RISC-V 构建直接受益，未再出现。

## 已知限制

1. QEMU TCG 仿真非实时，延迟数据无意义
2. RISC-V RT 在 6.6-rt15 属较新支持，真实板卡（VisionFive 2、HiFive Unmatched 等）行为未验证
3. QEMU virt 是 RV64GC 通用配置，真实板卡需平台特定 DTB 和启动方式（U-Boot）

## 下一步

1. ✅ 创建构建/验证脚本
2. ✅ 完整构建（内核 + BusyBox + XTOS 库 + 测试二进制）
3. ✅ QEMU 基础验证（5/5）
4. ✅ In-guest 系统验证（15/15）
5. ✅ 文档更新
6. ⏳ 提交代码
7. ⏳ 真实 RISC-V 板卡验证（待硬件）
