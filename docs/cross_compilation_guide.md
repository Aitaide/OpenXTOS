# 交叉编译工具链配置文档

## 概述

本文档介绍了 XTOS 项目中 ARM64 和 RISC-V 交叉编译环境的配置和使用方法。

## 已安装的工具链

### ARM64 工具链
- **编译器**: `aarch64-linux-gnu-gcc` (GCC 14.2.0)
- **二进制工具**: `binutils-aarch64-linux-gnu`
- **C库开发文件**: `libc6-dev-arm64-cross`
- **Linux头文件**: `linux-libc-dev-arm64-cross`

### RISC-V 工具链
- **编译器**: `riscv64-linux-gnu-gcc` (GCC 14.2.0)
- **二进制工具**: `binutils-riscv64-linux-gnu`
- **C库开发文件**: `libc6-dev-riscv64-cross`
- **Linux头文件**: `linux-libc-dev-riscv64-cross`

### QEMU 虚拟化环境
- **版本**: QEMU 10.0.13
- **ARM64 模拟器**: `qemu-system-aarch64`
- **RISC-V 模拟器**: `qemu-system-riscv64`
- **其他架构**: `qemu-system-x86`, `qemu-system-arm`, `qemu-system-misc`

## 工具链验证

### 验证编译器安装

```bash
# 检查 ARM64 编译器
aarch64-linux-gnu-gcc --version

# 检查 RISC-V 编译器
riscv64-linux-gnu-gcc --version

# 检查 QEMU ARM64
qemu-system-aarch64 --version

# 检查 QEMU RISC-V
qemu-system-riscv64 --version
```

### 测试编译

项目根目录下提供了测试程序：
- `/root/hello_arm64.c` - ARM64 测试程序
- `/root/hello_riscv.c` - RISC-V 测试程序

编译测试：
```bash
# ARM64 编译测试
aarch64-linux-gnu-gcc -o hello_arm64 hello_arm64.c
file hello_arm64

# RISC-V 编译测试
riscv64-linux-gnu-gcc -o hello_riscv hello_riscv.c
file hello_riscv
```

## 使用交叉编译脚本

### 脚本位置
`/root/xtos-project/scripts/cross_compile.sh`

### 用法

```bash
./scripts/cross_compile.sh <架构> <源文件> [输出文件名]
```

**参数说明**：
- `<架构>`: `arm64`, `aarch64`, `riscv64`, 或 `riscv`
- `<源文件>`: 要编译的 C 源文件
- `[输出文件名]`: 可选，输出二进制文件名（默认：源文件名_架构）

### 示例

```bash
# 编译 ARM64 程序
./scripts/cross_compile.sh arm64 hello.c my_program_arm64

# 编译 RISC-V 程序
./scripts/cross_compile.sh riscv64 hello.c my_program_riscv
```

### 脚本功能
- ✓ 自动检测编译器是否安装
- ✓ 彩色输出提示信息
- ✓ 显示编译后的二进制文件信息
- ✓ 显示文件大小
- ✓ 错误处理和友好提示

## 内核交叉编译

### ARM64 内核编译

```bash
cd /path/to/linux-source

# 生成默认配置
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- defconfig

# 编译内核
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -j$(nproc)

# 编译设备树
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- dtbs
```

### RISC-V 内核编译

```bash
cd /path/to/linux-source

# 生成默认配置
make ARCH=riscv CROSS_COMPILE=riscv64-linux-gnu- defconfig

# 编译内核
make ARCH=riscv CROSS_COMPILE=riscv64-linux-gnu- -j$(nproc)

# 编译设备树
make ARCH=riscv CROSS_COMPILE=riscv64-linux-gnu- dtbs
```

## QEMU 使用指南

### ARM64 虚拟机

```bash
# 启动 ARM64 虚拟机（基本示例）
qemu-system-aarch64 \
    -M virt \
    -cpu cortex-a57 \
    -m 2048 \
    -kernel Image \
    -append "console=ttyAMA0" \
    -nographic
```

### RISC-V 虚拟机

```bash
# 启动 RISC-V 虚拟机（基本示例）
qemu-system-riscv64 \
    -M virt \
    -m 2048 \
    -kernel Image \
    -append "console=ttyS0" \
    -nographic
```

### QEMU 常用参数

- `-M`: 指定机器类型（`virt` 是通用虚拟平台）
- `-cpu`: 指定 CPU 型号
- `-m`: 内存大小（MB）
- `-kernel`: 内核镜像路径
- `-append`: 内核启动参数
- `-nographic`: 无图形界面，使用串口
- `-drive`: 添加磁盘镜像
- `-netdev`: 配置网络设备

## 环境变量

推荐在 `~/.bashrc` 或项目 Makefile 中设置：

```bash
# ARM64 环境变量
export ARCH=arm64
export CROSS_COMPILE=aarch64-linux-gnu-

# RISC-V 环境变量
export ARCH=riscv
export CROSS_COMPILE=riscv64-linux-gnu-
```

## 常见问题

### 1. 编译器找不到头文件

确保安装了对应架构的开发包：
```bash
apt-get install libc6-dev-arm64-cross libc6-dev-riscv64-cross
```

### 2. 链接错误

检查是否安装了交叉编译的库文件，可能需要：
```bash
apt-get install libgcc-*-dev-arm64-cross
apt-get install libgcc-*-dev-riscv64-cross
```

### 3. QEMU 启动失败

- 检查内核镜像路径是否正确
- 确认 QEMU 版本支持目标架构
- 查看 QEMU 输出的错误信息

### 4. 交叉编译的程序无法在 x86 上运行

这是正常的！交叉编译的程序只能在目标架构上运行：
- ARM64 程序需要在 ARM64 硬件或 QEMU ARM64 虚拟机中运行
- RISC-V 程序需要在 RISC-V 硬件或 QEMU RISC-V 虚拟机中运行

可以使用 QEMU 用户模式模拟器：
```bash
# ARM64 程序
qemu-aarch64 -L /usr/aarch64-linux-gnu ./hello_arm64

# RISC-V 程序
qemu-riscv64 -L /usr/riscv64-linux-gnu ./hello_riscv
```

## 工具链测试清单

### ✓ 已完成的测试

1. **编译器安装验证**
   - ✓ ARM64 GCC 14.2.0 安装成功
   - ✓ RISC-V GCC 14.2.0 安装成功

2. **简单程序编译**
   - ✓ ARM64 Hello World 编译成功
   - ✓ RISC-V Hello World 编译成功
   - ✓ 使用 `file` 命令验证目标架构正确

3. **QEMU 安装**
   - ✓ qemu-system-aarch64 10.0.13 安装成功
   - ✓ qemu-system-riscv64 10.0.13 安装成功

4. **交叉编译脚本**
   - ✓ 脚本创建完成
   - ✓ ARM64 编译测试通过
   - ✓ RISC-V 编译测试通过

### 待完成的任务

1. **内核编译测试**
   - 需要完整的 Linux 内核源码树
   - 建议使用独立的构建目录进行测试

2. **QEMU 启动测试**
   - 需要编译好的内核镜像
   - 需要根文件系统镜像（可选）

## 参考资料

- [ARM64 架构文档](https://developer.arm.com/documentation/)
- [RISC-V 规范](https://riscv.org/specifications/)
- [Linux 内核交叉编译指南](https://www.kernel.org/doc/html/latest/kbuild/index.html)
- [QEMU 文档](https://www.qemu.org/docs/master/)
- [Debian 交叉编译 Wiki](https://wiki.debian.org/CrossCompiling)

## 版本信息

- **文档版本**: 1.0
- **创建日期**: 2026-09-30
- **工具链版本**: GCC 14.2.0
- **QEMU 版本**: 10.0.13
- **操作系统**: Debian Trixie (testing)
