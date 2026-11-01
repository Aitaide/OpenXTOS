# XTOS 完整系统构建

## 源码范围

仓库直接包含 Linux 7.3.1（主线 PREEMPT_RT）、XTOS 内核组件、BusyBox 1.37.0、用户态 SDK、EEG 工具、测试、开发计划和设计文档。上游版本、官方归档摘要和 XTOS 集成补丁由 `kernel/source-pin.json` 固定。构建不会下载 Linux 或 BusyBox；旧 6.6/rt15 补丁仅保留历史用途，不参与当前构建。迁移状态见 `kernel-7.3.1-migration.md`。

当前完整系统目标是 x86_64 开发系统：可启动的实时 Linux 内核和 initramfs 根文件系统，提供本地命令行、模块加载和 XTOS 用户态工具。它不是桌面发行版、硬盘安装器或医疗产品。ARM64/RISC-V 构建脚本保留，但不代表对应完整系统或真实板卡已验证。

## 构建

Debian/Ubuntu 主机依赖：

```sh
sudo apt-get install build-essential flex bison bc libssl-dev libelf-dev libc6-dev python3 cpio gzip kmod qemu-system-x86
JOBS=2 ./scripts/build_system.sh
./scripts/verify_system.sh
```

内核和 BusyBox 源码在仓库内，GCC/binutils 与静态 libc 开发文件属于主机构建工具链。默认低并发适合内存紧张主机；资源充足时提高 JOBS。

输出：

- `.build/images/xtos-bzImage`：Linux 7.3.1、主线 PREEMPT_RT 与 XTOS 内核组件。
- `.build/images/xtos-rootfs.cpio.gz`：BusyBox、所有配置模块、XTOS SDK 静态库/头文件、EEG 工具和测试程序。
- `.build/images/kernel.config`：实际解析后的内核配置。
- `.build/system-qemu.log`：启动验证日志，不提交到 Git。

可使用 `BUILD_ROOT=/absolute/output/path` 重定向输出。内核和 BusyBox 使用独立输出目录；用户态源码复制到输出目录再编译，避免生成物污染开发源码。

## 启动与自检

```sh
./scripts/run_system.sh
```

启动进入本地 root 开发控制台，不启动网络服务。退出 QEMU 使用 Ctrl-A X。根文件系统在内存中，关机后不保留修改。不要把无认证开发控制台暴露到公共网络。

`verify_system.sh` 使用同一组系统镜像，设置 `xtos.selftest=1`：

1. 检查运行中内核的 RT 证据、`CONFIG_PREEMPT_RT=y` 和 `CONFIG_XTOS_SCHED=y`。7.3.1 主线不提供旧 RT 补丁的 `/sys/kernel/realtime` 节点；不得以节点缺失判定 RT 关闭或伪造节点。状态工具按显式证据判定，证据不足拒绝。
2. 加载 BCI 核心、缓冲区和 OpenBCI 模块并检查 `/sys/module`。
3. 执行 RT 辅助库、SPSC、信号处理和调度队列测试。
4. 执行虚拟 EEG 数据管线和信号处理基准。
5. 只有所有检查成功、QEMU 正常退出且出现 `XTOS_SYSTEM_TEST=PASS` 才返回成功。

## 实际系统架构

```text
QEMU x86_64 / future target hardware
  Linux 7.3.1 + mainline PREEMPT_RT
    standard Linux real-time scheduling
    optional XTOS priority-queue helper (not a replacement sched_class)
    XTOS BCI core + buffer + OpenBCI serdev modules
  initramfs root filesystem
    BusyBox shell / mount / module utilities
    libxtos-bci / libxtos-rt / libxtos-signal
    EEG pipeline / signal benchmark / guest tests
```

`docs/XTOS_Technical_Specification.md`、`docs/scheduler_design.md` 和 `docs/driver_design.md` 保留完整设计；尚未实现的 AI、GPU、AR/VR 等目标仍属于规划，不会用占位实现冒充完成。

## 验证边界

源码构建、QEMU 启动、运行时实时配置、模块加载和虚拟数据测试可以验证。真实 OpenBCI 设备、真实 ARM64/RISC-V 板卡、目标硬件最坏实时延迟和医疗安全仍需单独验证。QEMU 测试不能证明硬实时上界。

## 许可证

Linux/PREEMPT_RT 遵守 `kernel/linux/COPYING`、`kernel/linux/LICENSES/` 和文件级 SPDX 声明。BusyBox 遵守 `userspace/busybox/LICENSE`；上游源码及归档校验值记录在 `userspace/README.md`。XTOS 自有用户态和系统脚本遵守仓库 LICENSE 或文件级声明。
