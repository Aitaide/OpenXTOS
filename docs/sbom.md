# XTOS SBOM

**格式**：CycloneDX 1.5 JSON
**产物**：`docs/sbom.json`（源码冻结后重新生成）

## 覆盖范围

当前清单保留 27 个组件（另加 XTOS 应用本体）：18 个 `required`、9 个 `excluded`。历史 PREEMPT_RT 转为 excluded；新增主线 RT 诊断组件，绑定原生工具和已安装状态脚本。

| required 组件 | 版本 | 许可证 / 来源 |
|---|---|---|
| linux | 7.3.1 | GPL-2.0 WITH Linux-syscall-note；`kernel/linux` |
| xtos-linux-integration | 1.0.1 | GPL-2.0-only；`kernel/patches/xtos-linux-7.3.1.patch` |
| busybox | 1.37.0 | GPL-2.0-only；`userspace/busybox` |
| libxtos-rt / libxtos-bci | 1.0.1 | Apache-2.0；`sdk/` |
| libxtos-signal | 1.1.0 | Apache-2.0；`sdk/` |
| xtos-eeg-pipeline / xtos-signal-benchmark / xtos-eeg-replay | 1.0.1 | Apache-2.0；`tools/` |
| bci | 1.0.1 | GPL-2.0-only；`tools/xtos-cyton` |
| xtos-eeg-stream / xtos-eeg-loop / xtos-action-gate | 1.0.1 | Apache-2.0；`tools/` |
| xtos-ai-runtime | 1.1.0 | Apache-2.0；`tools/xtos-ai-runtime` |
| xtos-device-plugins | 0.1.0 | Apache-2.0；`tools/xtos-devices` |
| xtos-xr | 0.1.0 | Apache-2.0；`tools/xtos-xr` |
| openxr-headers | 1.1.57 | Apache-2.0 选项；固定 Khronos 头文件，无 loader/compositor |
| xtos-rt-diagnostics | 1.0.1 | Apache-2.0；原生 info/rt、共享头及 xtos-rt-status |

`excluded`：
- `preempt-rt` 6.6-rt15：保留旧补丁文件、来源 URL、GPL-2.0-only 和 SHA-256，只作历史记录，不应用到 7.3.1。
- `xtos-fuzz`、`xtos-onnx-baseline`、`xtos-ai-provider-experiment`：开发或可选实验源。
- 宿主隔离基线依赖：onnxruntime 1.23.2、numpy 2.2.4、torch 2.6.0、onnx 1.20.1、tensorflow 2.18.0。

## 内核来源与 RT 归属

- `kernel/source-pin.json` 是内核版本、上游 URL、压缩 archive SHA-256/字节数和当前 XTOS 补丁路径的来源。SBOM 记录 pin 文件自身 SHA-256，以及实际 `Makefile`、`COPYING` 摘要。
- Linux 7.3.1 上游：`https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.3.1.tar.xz`。压缩 archive SHA-256：`fa595aad93734f829c963c2a90ef7c3673224fd7fdae7b49406e397cde2de231`。
- PREEMPT_RT 归属于 Linux 主线源码和 `CONFIG_PREEMPT_RT=y` 配置，不再依赖旧外置 6.6-rt15 补丁。SBOM 的 RT 属性是来源/配置意图，`xtos:rt-runtime-evidence=not-validated`；不能证明已构建、启动或通过实时性验收。
- 当前 XTOS 补丁摘要取自真实文件；测试要求与 pin 的 `xtos_patch_sha256` 相等。补丁组件还逐路径绑定 XTOS 驱动/调度器 C、头文件、UAPI、Kconfig、Makefile 及四个上游接入点；测试文件不作为运行时输入。旧 6.6 XTOS 补丁仍保留在仓库中。

## 依赖与许可证

- XTOS system → 全部 18 个 required 组件；所有 excluded 组件仍有独立依赖节点，但不进入 system 依赖。
- XTOS 内核集成 → linux；EEG stream → libxtos-bci / libxtos-signal；设备插件 → EEG pipeline；XR → OpenXR 头文件 / action gate。
- XTOS 本体及 SDK 为 Apache-2.0；内核侧派生代码和 BusyBox 保持各自 GPL 声明，SDK 与内核通过 UAPI syscall/ioctl 边界隔离。

## 生成与校验

在仓库根目录执行：

```bash
python3 tools/xtos-sbom/gen_sbom.py
python3 tools/xtos-sbom/gen_sbom.py --check
python3 tools/xtos-sbom/test_sbom.py -v
python3 -O tools/xtos-sbom/test_sbom.py -v
```

`--check` 在内存重新生成，剥离时戳、serial、host 和 commit 等变动字段后，比较组件、许可证、来源摘要及依赖图。源码升级后尚未重生成的旧 `docs/sbom.json` 会按预期报告 stale。

## 边界

- 来源固定不等于签名复验：生成器记录 pin 中的来源，测试校验当前文件与 pin 一致；上游 archive 的签名验证属于独立来源核验流程。
- 内核 Makefile/COPYING 与 XTOS 集成输入不是整个 Linux 源码树的逐文件证明；上游固件/DTB 也未逐项拆成组件。
- 宿主 gcc、binutils、make、cpio 等是构建前置，不属于此交付清单。可选依赖记录不证明镜像安装或硬件可用性。
