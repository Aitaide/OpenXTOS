# 调度基线目标主机预检

状态：M0 的只读采集准备。报告生成成功不代表目标裸机或 PREEMPT_RT 性能验收通过。

## 运行

环境：Linux、Python 3.9 或更新版本。工作目录为 XTOS 仓库根目录；无需 root 和第三方 Python 包。目录与报告文件名须按每次采集分别保存。

```sh
mkdir -p .build/scheduler-preflight/target-01
python3 tools/xtos-sched-bench/preflight.py --pretty \
  --output .build/scheduler-preflight/target-01/host.json
python3 tests/test_sched_preflight.py -v
```

`--output` 不覆盖既有文件，成功写出退出码为 0，输出已存在或调用失败时为 2。缺失或无法读取的事实保留为 unknown/error，不使整个报告自动变成测试失败。运行前不用安装包、调整 IRQ、改变 cgroup 或写 sysctl。

`--root <目录>` 是 fixture/离线诊断入口，输出 `collection_mode=fixture_filesystem`。该模式不读取采集器所在机器的 uname 和 allowed affinity 来冒充目标信息；程序接口可为测试显式传入 allowed_cpus。正常主机采集为 `live_host`；显式覆盖 live affinity 的程序调用为 `diagnostic_override`，均不得混为正式性能样本。

## 字段解释

- schema：`xtos-sched-preflight-v1`。
- virtualization：有明确虚拟化信号时为字符串 `true`；无可靠正向信号为 `unknown`。机型名称和未检出 hypervisor 都不证明裸机，应另附目标资产/固件与现场启动证据。
- realtime：`/sys/kernel/realtime` 的读取结果。为 `1` 是运行中 RT 内核的重要证据，但仍须核对 exact image、内核配置和来源 SHA。
- cpu：online、调用进程 allowed affinity 及交集；不能据此推导 scheduler root domain。CPU 编号不连续也应保持原样。
- cmdline：仅提取 isolcpus/nohz_full/housekeeping 可识别 CPU 参数，不导出整条启动命令，避免无关敏感参数泄漏。不识别的 CPU 语法返回错误而非猜测。
- cgroup：v2 可见性及当前 membership 对应的 cpuset.cpus.effective；不设置 partition、controller 或 membership。命名空间和 mount root 可能使信息不可见，保留 unknown。
- governor：每个 allowed CPU 的可读 governor。缺少 cpufreq 不自动视为 performance。
- irq_affinity：请求 affinity 的聚合，不保存全部 IRQ 名称与路径，不代表 effective affinity、IRQ-thread policy 或隔离配置已正确。
- scheduler_config：配置文件可读及 gzip 有效性，不对配置内容作完整语义验收；generic /boot/config 不证明与正在运行的内核相同。
- cap_sys_nice：当前进程有效 capability bit。为 true 不证明策略一定被准入，仍受 root domain、cgroup 和其他限制影响。

普通事实使用 status/value/source/error；不同事实类型的汇总字段分别保留。所有输入按字节上限读取，非普通文件不作为事实，读取目录也有条目上限；截断或失败不能解释为“配置正确”。快照由多次只读操作组成，不保证采集期间系统状态不变。

## 实机基线前仍需确认

1. 指定物理目标机及负责人，记录 CPU/NUMA、固件、BIOS、电源与温度条件；不使用虚拟机结果替代。
2. 确认正在运行的 XTOS PREEMPT_RT exact image、源码 SHA、配置与镜像哈希，并保留独立可启动回退内核。
3. 冻结代表性真实 workload 的任务集、release/period/deadline、CPU 预算来源、设备/IRQ、依赖关系与过载结果。现有 periodic-cpu-v1 仅是工具合成测试。
4. 预登记主要指标、允许回归、收益门槛、至少 10 轮重复与统计方法，再开始同条件 FIFO/RR/DEADLINE 对照。
5. 明确 CPU/root-domain/IRQ 放置；若 DEADLINE 准入拒绝保留失败，不关闭 admission、不自动扩大 mask。配置变更须有单独授权和前后证据。

预检工具不运行长测、不自动做 go/no-go，不实现 M1 模型或 M2 内核类。若原生策略已满足真实需求，可以停止定制调度类开发。
