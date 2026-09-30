# XTOS阶段2验证报告

**日期**: 2026-09-30
**范围**: PREEMPT_RT、x86_64启动、虚拟EEG数据校验

## 已验证

- Linux 6.6.0-rt15源码树的真实`CONFIG_PREEMPT_RT`配置可用。
- 独立输出目录构建成功：`kernel/preempt-rt-build/`。
- 配置包含`CONFIG_PREEMPT_RT=y`，未启用`CONFIG_PREEMPT_DYNAMIC`、`CONFIG_PREEMPT_NONE`或`CONFIG_PREEMPT_VOLUNTARY`。
- x86_64 `bzImage`构建成功，大小12,494,208 bytes。
- QEMU启动成功，内核报告`SMP PREEMPT_RT`。
- QEMU运行时`/sys/kernel/realtime=1`。
- QEMU通过`/proc/config.gz`确认`CONFIG_PREEMPT_RT=y`。
- QEMU中`cyclictest`运行10秒：Min 85µs、Avg 256µs、Max 9202µs。
- 宿主机`cyclictest`运行10秒：Min 6µs、Avg 14µs、Max 50µs；这是宿主机`PREEMPT_DYNAMIC`内核，不是XTOS RT内核。
- 虚拟EEG CSV校验工具通过3个自动测试。
- 正常fixture返回JSON摘要；非单调时间戳和错误列数正确返回非零状态。

## 不能宣称

- QEMU延迟不代表真实硬件实时性。
- 尚未在目标x86_64、ARM64或RISC-V板卡上测量。
- 尚未完成真实EEG设备采集。
- 尚未完成CPU隔离、IRQ亲和性、负载、长时间稳定性和`rtla timerlat`测试。
- `CONFIG_XTOS_SCHED`仍是可选辅助组件，不是Linux主调度类。

## 证据文件

- `docs/preempt_rt_validation.md`
- `kernel/preempt-rt-build/.config`
- `kernel/preempt-rt-build/build.log`
- `kernel/preempt-rt-build/qemu.log`
- `kernel/preempt-rt-build/host-cyclictest.log`
- `tools/xtos-eeg-replay/replay.py`
- `tools/xtos-eeg-replay/test_replay.py`
- `docs/virtual_eeg_replay_test_output.txt`

## 下一开发门

1. 将虚拟EEG数据接入用户态采集/处理链路，增加滤波、时间戳和环形缓冲测试。
2. 建立`libxtos-rt`用户态运行时，封装线程优先级、CPU亲和性、锁页内存和调度策略，并在非特权环境提供明确错误。
3. 完成真实OpenBCI硬件接入前的驱动生命周期、卸载、并发和回放压力测试。
4. 将虚拟EEG数据接入用户态采集/处理链路，增加滤波、时间戳和环形缓冲测试。
5. 建立`libxtos-rt`用户态运行时，封装线程优先级、CPU亲和性、锁页内存和调度策略，并在非特权环境提供明确错误。
6. 再开始ONNX/PyTorch/TensorFlow推理适配；推理任务先定义为软实时，并设计超时降级路径。
