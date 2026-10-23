# XTOS Reproducible Build

**状态**: 已验证（2026-10-02，x86_64）
**方案**: `scripts/reproducible_env.sh` + 各构建脚本的确定性打包

---

## 用法

```bash
. ./scripts/reproducible_env.sh        # 导出固定构建环境
JOBS=16 ./scripts/build_system.sh      # 或 build_system_arm64.sh / build_system_riscv64.sh
```

`XTOS_REPRO_VERBOSE=1` 时打印生效的 epoch 与预期内核横幅。

## 固定的非确定性来源

| 来源 | 机制 | 效果 |
|------|------|------|
| 内核构建号 `#N` | `KBUILD_BUILD_VERSION=1`（kbuild 原生） | `#1` 固定 |
| 内核 `user@host` | `KBUILD_BUILD_USER/HOST=xbuild` | `xbuild@xbuild` 固定 |
| 内核横幅时间 | `KBUILD_BUILD_TIMESTAMP` ← SOURCE_DATE_EPOCH | `Fri Oct 02 06:32:47 2026 UTC`（HEAD commit 时间） |
| kconfig AUTOCONF_TIMESTAMP | `SOURCE_DATE_EPOCH`（BusyBox 1.37 confdata.c 原生支持） | BusyBox banner `v1.37.0 (2026-10-02 06:32:47 UTC)` 固定 |
| initramfs 文件 mtime | 打包前 `touch --date=@$SOURCE_DATE_EPOCH` | cpio 条目时间固定 |
| cpio 条目顺序/属主/设备号 | `find \| LC_ALL=C sort` + `--owner=0:0 --reproducible`（GNU cpio 2.15+） | 归档顺序与元数据稳定 |
| gzip 头时间戳 | `gzip -n` | 无时间戳 |
| locale/时区 | `LC_ALL=C`、`TZ=UTC` | 工具输出稳定 |

`SOURCE_DATE_EPOCH` 默认取 git HEAD commit 时间（`git log -1 --pretty=%ct`），可显式覆盖以钉死任意 epoch。

## 验证结果（2026-10-02）

**内核**（全清 `.build/kernel-build` 后两次全量编译）:
```
fb18812091d2f4883d3295e914377ded4e5eaddab240d4e82690235660388682  bzImage  (两次一致)
```

**完整系统**（清 busybox-build/rootfs/images/userspace-source 后两次全量构建）:
```
fb18812091d2f4883d3295e914377ded4e5eaddab240d4e82690235660388682  xtos-bzImage        (两次一致)
acd814dc3f4109319313a04f61b601c63186c22267668bcbef506654cbe4126e  xtos-rootfs.cpio.gz (两次一致)
```

内核横幅: `Linux version 6.6.0-rt15 (xbuild@xbuild) (...) #1 SMP PREEMPT_RT Fri Oct 02 06:32:47 2026 UTC`
BusyBox 横幅: `BusyBox v1.37.0 (2026-10-02 06:32:47 UTC)`

**可复现产物的功能回归**: QEMU `verify_system.sh` 全过——`realtime=1`、`xtos_modules=pass`、`guest_userspace=pass`、`XTOS_SYSTEM_TEST=PASS`。

## 已知边界

1. **同一 commit、同一工具链前提下逐字节可复现**。换 gcc/binutils 版本（如 CI 的 ubuntu-latest 与本机 Debian 14.2.0 差异）产物必然不同——工具链版本信息在 SBOM 与构建日志中可追溯，但跨工具链复现不在本项目目标内（需要工具链 itself 可复现）。
2. **增量构建可能复用陈旧产物**：kconfig 的 autoconf.h 只在 `.config` 更新时重写；BusyBox 增量构建可能不重编未变更的目标。发布构建应从干净 `.build` 开始（本验证即采用该流程）。
3. **ARM64/RISC-V64 打包逻辑已同步改造**（固定 mtime + sort + `--reproducible` + `gzip -n`），同 commit 同工具链下预期同样可复现；未在本机做双构建逐字节验证（两次 ARM64 全量构建约需 40 分钟），方法与 x86_64 完全一致。

## CI 建议

发布前在干净 runner 上：`. ./scripts/reproducible_env.sh && ./scripts/build_system.sh`，将产物 SHA-256 记入 release notes，供第三方比对。
