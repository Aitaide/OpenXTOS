# XTOS 当前状态

最后更新：2026-10-12

唯一开发目录：`/root/xtos-development/xtos`。

仓库直接包含 Linux 6.6.0 + PREEMPT_RT rt15、XTOS 内核组件、BusyBox 1.37.0、根文件系统、SDK、工具、测试、开发计划和架构设计。构建不下载基础源码，输出隔离在 `.build/`。

当前已验证：

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

## 历史状态记录

以下历史文件和段落保留用于追溯，不代表当前实现和验证结论。旧记录中的 `/root/xtos-project`、阶段编号、待办项和性能数字均是历史上下文；当前状态以上文和 `docs/system_build.md` 为准。

- `docs/` 保留技术规格、调度器设计、驱动设计、信号处理、系统构建和验证边界。
- `plans/` 保留开发计划和后续任务。
- `FINAL_STATUS_SUMMARY.md`、`PROJECT_STATUS.md`、`TASK_SUMMARY.txt` 等保留为历史开发记录。