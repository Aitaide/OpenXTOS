# XTOS SBOM

**状态**: 生效（2026-10-02）
**格式**: CycloneDX 1.5 JSON
**文件**: `docs/sbom.json`（入库的发布产物，随源码更新再生成）

---

## 覆盖范围

**required（随系统交付，10 项 + XTOS 应用本体）：**

| 组件 | 版本 | 许可证 | 来源 |
|------|------|--------|------|
| linux | 6.6.0 (-rt15 应用后) | GPL-2.0 WITH Linux-syscall-note | kernel/linux（vendored，hash 锚定 Makefile/COPYING） |
| preempt-rt | 6.6-rt15 | GPL-2.0-only | kernel/patches/preempt-rt-6.6-rt15.patch |
| xtos-linux-integration | 1.0.1 | GPL-2.0-only | kernel/patches/xtos-linux-6.6.patch（调度辅助 + BCI 驱动 + UAPI） |
| busybox | 1.37.0 | GPL-2.0-only | userspace/busybox（上游 archive SHA-256 记录在案） |
| libxtos-rt / libxtos-signal / libxtos-bci | 1.0.1 | Apache-2.0 | sdk/（源文件 SHA-256） |
| xtos-eeg-pipeline / xtos-signal-benchmark / xtos-eeg-replay | 1.0.1 | Apache-2.0 | tools/ |

**excluded（记录在案但不随系统交付，7 项）：**
- xtos-fuzz（开发期 fuzz 工具）
- xtos-onnx-baseline 及其宿主侧 Python 依赖：onnxruntime 1.23.2 (MIT)、numpy 2.2.4 (BSD-3-Clause)、torch 2.6.0 (BSD-3-Clause)、onnx 1.20.1 (Apache-2.0)、tensorflow 2.18.0 (Apache-2.0)——隔离环境运行的可选基线，不进系统镜像

## 许可证合规要点

- XTOS 项目本体: Apache-2.0（仓库 LICENSE）
- 内核 + PREEMPT_RT + XTOS 内核补丁: GPL-2.0（内核侧派生代码必须 GPL-2.0 兼容；xtos-linux patch 中的新文件全部标注 `SPDX-License-Identifier: GPL-2.0`）
- BusyBox: GPL-2.0-only（静态链接进 initramfs，符合其分发要求）
- Apache-2.0 的 sdk 库与 GPL-2.0 的内核组件通过 UAPI 边界隔离（syscall/ioctl），不构成链接合并作品

## 生成与校验

```bash
# 重新生成（源码变动后）
python3 tools/xtos-sbom/gen_sbom.py

# 与入库版本比对（CI / test.sh 使用；剥离时戳/serial/host 后全量比对）
python3 tools/xtos-sbom/gen_sbom.py --check
```

- 组件清单、许可证、源文件 SHA-256、上游 archive 摘要、依赖图全部由 `test_sbom.py`（7 项断言）验证，已接入 `scripts/test.sh`
- 依赖图：xtos system → 10 个 required 组件；excluded 组件有独立节点但不入 system 依赖
- 每个 required 组件带源文件 SHA-256，源码漂移会导致 `--check` 失败并要求再生成

## 已知边界

- SBOM 覆盖仓库内组件；宿主工具链（gcc、binutils、make、cpio 等）是构建前置而非交付物，不在清单内（与 userspace/README.md 的口径一致）
- 内核树内含的上游第三方固件/DTB 数据不在组件粒度上逐一列出（跟随 linux 组件整体许可声明）
