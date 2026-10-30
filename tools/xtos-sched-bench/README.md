# XTOS 调度基准工具（M0）

本工具用于验证 Linux 原生调度策略的设置、读回、周期任务运行及 trace 采集。它不是 XTOS 自定义调度器，也不能单独证明硬实时性能或优于 Linux。

## 两类实验必须分开

1. 默认绑定实验：被测任务与一个 SCHED_OTHER 竞争进程使用指定 CPU，策略为 OTHER、FIFO、RR 或 DEADLINE。所有参数和 CPU 放置一致才能讨论对比。若 DEADLINE 准入失败，本次实验失败，不自动改变 affinity、策略或预算。
2. 显式非绑定 DEADLINE 诊断：指定 `--deadline-unpinned`，被测任务保留调用者 CPU mask；竞争进程仍在 `--cpu` 指定的 CPU。该实验只验证 DEADLINE 设置、读回和采集路径，不能与单 CPU 绑定组作性能比较。

Linux 的 DEADLINE 带宽准入以 root domain 为单位。允许 CPU mask 不覆盖 root domain 时，可能在设置策略时返回 EPERM，或在策略生效后缩小 affinity 时返回 EBUSY。不要把这类拒绝一概解释为缺少 root/CAP_SYS_NICE，也不要关闭准入控制来使测试通过。当前 XTOS 内核源码可在 `kernel/linux/kernel/sched/core.c` 的 DEADLINE affinity 校验中核查该约束。

## 构建与测试

在仓库根目录执行：

```sh
make -C tools/xtos-sched-bench all
make -C tools/xtos-sched-bench test
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_scheduler_docs_currentness.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_sched_bench_runner.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_sched_trace_parser.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_sched_preflight.py
```

二进制输出到 `.build/xtos-sched-bench/periodic`。`make test` 编译独立的测试二进制并验证 OTHER、参数拒绝、现有 trace 保留、亲和性边界和完整记录，不要求设置特权实时策略。

## M0 gate 与证据判定

原生 FIFO/RR/DEADLINE 的真实证据必须先填写 `docs/scheduler-workloads/m0-target-intake.json`，再运行：

```sh
python3 tools/xtos-sched-bench/m0_gate.py \
  docs/scheduler-workloads/m0-target-intake.json \
  .build/scheduler-results/evidence/index.json \
  --output .build/scheduler-results/m0-gate.json
```

`m0_gate.py` 要求协议指定的原生策略组各至少 10 个不同执行的有效、同条件、固定 CPU trace；拒绝 inherited-mask DEADLINE 诊断、哈希漂移、混合 placement 和不完整 manifest。工具校验机器可验证的输入和跨产物一致性，完整报告最多为 `ready_for_independent_review`。`fixture_only`、`blocked_external_inputs`、`evidence_insufficient` 均不得写成硬件通过。人工 reviewer 仍需验证物理平台、workload、预算/WCET 依据、预注册统计阈值及 go/no-go；确认原生策略足够时停止专用 sched_class。工具不自动批准停止或进入 M2。

详细索引格式、per-run preflight 和原始 manifest 保留规则见 [M0 证据契约](../../docs/scheduler-workloads/m0-evidence-v1.md)。intake 的 evidence_root 必须指向 INDEX 所在目录；相对路径以 intake 文件目录为基准，不能照搬示例中的未填写模板直接宣称通过。

## 只读主机预检

```sh
mkdir -p .build/scheduler-preflight/new-attempt
python3 tools/xtos-sched-bench/preflight.py --pretty \
  --output .build/scheduler-preflight/new-attempt/host.json
```

输出为 `xtos-sched-preflight-v1` JSON；指定输出已存在时退出码为 2，既有文件保持原样。退出码 0 只表示报告生成成功，不表示目标满足裸机或 PREEMPT_RT 验收。无需 root，不安装依赖，不运行 benchmark，不改变系统配置。

报告包括内核身份、虚拟化正向信号、RT sysfs、online/allowed CPU 及交集、已识别 CPU 启动选项、cgroup v2/cpuset、governor、请求 IRQ affinity 汇总、内核配置文件可读性和有效 CAP_SYS_NICE。读取失败或缺少信息保留为 unknown/error；没有虚拟化标记或机型看似物理服务器都不构成裸机证明。IRQ 汇总来自 `smp_affinity*` 请求值，不代表 effective IRQ placement；内核配置可读也不等于 `CONFIG_PREEMPT_RT=y`。

`--root <目录>` 仅用于 fixture/离线诊断，报告显式标记 `collection_mode=fixture_filesystem`，不混入运行采集器进程的 uname/affinity；此报告不能作为目标主机验收。正常采集标记 `live_host`。主机快照并非原子采集，不能据此推断 DEADLINE root domain 或准入一定成功。

详细采集与后续实机门槛见 [主机预检说明](../../docs/scheduler-workloads/target-preflight.md)。

## 运行示例

以下 CPU 2 只是示例，必须换成调用者实际允许且 online 的 CPU。使用从未存在的新 trace 文件名；不要覆盖已有证据。

```sh
python3 tools/xtos-sched-bench/run_trial.py \
  --policy fifo --priority 20 --cpu 2 \
  --period-ns 10000000 --deadline-ns 8000000 \
  --work-ns 1000000 --runtime-ns 1200000 \
  --duration-s 1 --warmup-jobs 10 \
  --trace .build/scheduler-results/fifo-new-attempt.jsonl
```

RR 使用 `--policy rr --priority 20`；OTHER 使用 `--policy other --priority 0`；DEADLINE 使用 `--policy deadline --priority 0`。默认不放宽 affinity。需要检查非绑定路径时，明确加入 `--deadline-unpinned` 并使用独立诊断文件名。

```sh
python3 tools/xtos-sched-bench/analyze_trace.py \
  .build/scheduler-results/fifo-new-attempt.jsonl \
  --output .build/scheduler-results/fifo-new-attempt-summary.json
```

## 计时含义

- `release_ns`：绝对周期的计划释放时间。
- `wake_ns`：绝对睡眠返回后的第一次用户态计时，不是内核真正 runnable 时刻。
- `finish_ns`：本 job 运算完成后的用户态计时。
- `cpu_work_ns`：CLOCK_THREAD_CPUTIME_ID 记录的运算 CPU 时间，与墙钟响应时间分开。
- `work_ns`：合成运算的目标 CPU 时间，不是真实应用 WCET。
- `runtime_ns`：DEADLINE 的 CPU 预算，必须留出运行时和测量开销；预算耗尽导致的延后也必须保留在原始数据中。

该工具没有内核 sched_wakeup/sched_switch tracepoint 数据，不能从用户态记录反推出纯调度开销、设备到应用的全链路时延或可证明的最坏时延。

## 证据与阶段边界

原始 C JSONL 不应被 runner 或分析器改写。环境、源码与二进制哈希、实际参数和进程验证放在旁车 manifest；分析摘要与原始 trace 分开存放。失败、未完成、warmup 和诊断模式不得伪装成有效性能样本。

当前开发宿主已观测为 KVM 上的 Linux 6.12 PREEMPT_DYNAMIC，不是目标裸机上的 XTOS 6.6.0-rt15 PREEMPT_RT。本机运行最多验证工具功能，不关闭 M0 的实机基线门槛。真正的 Linux 原生策略缺口及自定义 sched_class 的开发必要性仍待代表性 workload、同条件 PREEMPT_RT 实机对比与独立评审。
