# XTOS 当前状态

最后更新：2026-10-01

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
- 阶段3C 当前未完成 TensorFlow/TFLite 导出对齐。
- 阶段3C 的官方 CPU wheel 运行证据使用 ONNX Runtime 1.23.2、NumPy 2.2.4；Debian 1.21.0 依赖路径保留为探测结果，不作为干净基准。

系统范围为可启动的 x86_64 开发系统，不是桌面发行版或硬盘安装器。XTOS 调度组件是辅助层，未替代 Linux 的常规任务调度。P300 当前是单窗口研究基线和跨试次质量门禁，不是分类器。真实 OpenBCI 设备、ARM64/RISC-V 板卡、目标硬件最坏延迟和医疗安全均未验证。QEMU 结果不能作为硬件实时性能保证。

入口：`scripts/build_system.sh`、`scripts/verify_system.sh`、`scripts/run_system.sh`。详见 `docs/system_build.md`；开发任务、计划及架构设计继续保留在 `plans/` 和 `docs/`。

## 历史状态记录

以下历史文件和段落保留用于追溯，不代表当前实现和验证结论。旧记录中的 `/root/xtos-project`、阶段编号、待办项和性能数字均是历史上下文；当前状态以上文和 `docs/system_build.md` 为准。

- `docs/` 保留技术规格、调度器设计、驱动设计、信号处理、系统构建和验证边界。
- `plans/` 保留开发计划和后续任务。
- `FINAL_STATUS_SUMMARY.md`、`PROJECT_STATUS.md`、`TASK_SUMMARY.txt` 等保留为历史开发记录。