# XTOS Real-Time Hardware Testing Guide

**目标**: 在真实 x86_64 硬件上进行长时间实时性能验证

**版本**: 1.0  
**日期**: 2026-10-01

---

## 概述

本工具包用于在真实硬件上验证 XTOS 实时系统的延迟特性。QEMU 环境不能提供可信的实时性能指标，真实硬件测试是阶段 4A 的必要步骤。

## 工具清单

```
tools/realtime-test/
├── rt_system_check.sh    系统配置检查和优化建议
├── rt_cyclictest.sh      长时间 cyclictest 延迟测试
├── rt_analyze.py         结果分析和报告生成
└── README.md             本文档
```

## 前置要求

### 硬件

- x86_64 服务器或工作站（非虚拟机）
- 推荐：多核 CPU（≥4 核），至少 2 个核心可用于隔离
- 推荐：8GB+ 内存

### 软件

```bash
# Debian/Ubuntu
apt-get install rt-tests linux-tools-common util-linux

# Fedora/RHEL
dnf install rt-tests

# 验证安装
cyclictest --help
```

### 内核

使用 XTOS 构建的 PREEMPT_RT 内核（Linux 6.6.0-rt15）或发行版提供的 RT 内核。

---

## 使用流程

### 第一步：系统配置检查

在真实硬件上运行系统检查脚本：

```bash
cd /path/to/xtos/tools/realtime-test
chmod +x rt_system_check.sh
sudo ./rt_system_check.sh
```

**输出示例**:

```
[2026-10-01 10:00:00] XTOS Real-Time System Configuration Check
[2026-10-01 10:00:00] ==========================================
[2026-10-01 10:00:00] Host: rt-server-01
[2026-10-01 10:00:00] Date: 2026-10-01

[2026-10-01 10:00:00] === Kernel Configuration ===
[2026-10-01 10:00:00] Kernel version: 6.6.0-rt15
[2026-10-01 10:00:00] ✓ PREEMPT_RT enabled
[2026-10-01 10:00:00] ✓ NO_HZ_FULL enabled for cores: 2-15
[2026-10-01 10:00:00] ✓ CPU isolation: 2-15

[2026-10-01 10:00:00] === CPU Configuration ===
[2026-10-01 10:00:00] CPU model: Intel(R) Xeon(R) Silver 4214R
[2026-10-01 10:00:00] Total CPUs: 24
[2026-10-01 10:00:00] ✓ Performance governor active (recommended for RT)
[2026-10-01 10:00:00] ✓ Bare metal detected
```

**检查项目**:

- ✓ PREEMPT_RT 内核
- ✓ CPU 隔离配置（`isolcpus`, `nohz_full`）
- ✓ 性能模式频率调节器
- ✓ 裸机环境（非虚拟化）
- ✓ THP 和 swap 禁用
- ✓ irqbalance 禁用

### 第二步：应用推荐配置

根据检查脚本的建议优化系统（需要重启）：

#### 内核参数

编辑 `/etc/default/grub`，在 `GRUB_CMDLINE_LINUX` 中添加：

```
isolcpus=domain,managed_irq,2-15 nohz_full=2-15 rcu_nocbs=2-15
transparent_hugepage=never intel_pstate=disable processor.max_cstate=1
intel_idle.max_cstate=0
```

更新 GRUB 并重启：

```bash
sudo update-grub
sudo reboot
```

#### 运行时配置

```bash
# 设置性能模式
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor

# 停止 IRQ 平衡
sudo systemctl stop irqbalance
sudo systemctl disable irqbalance

# 禁用看门狗
echo 0 | sudo tee /proc/sys/kernel/watchdog

# 禁用 swap
sudo swapoff -a

# 禁用透明大页
echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled
```

### 第三步：运行延迟测试

**24 小时标准测试**（推荐）：

```bash
cd /path/to/xtos/tools/realtime-test
chmod +x rt_cyclictest.sh
sudo ./rt_cyclictest.sh \
    --duration 86400 \
    --threads 2 \
    --affinity 2,3 \
    --priority 80
```

**1 小时快速测试**：

```bash
sudo ./rt_cyclictest.sh \
    --duration 3600 \
    --threads 2 \
    --affinity 2,3 \
    --priority 80
```

**参数说明**:

- `--duration`: 测试时长（秒）
- `--threads`: 线程数（建议 2-4）
- `--affinity`: CPU 亲和性，使用隔离的核心
- `--priority`: SCHED_FIFO 优先级（1-99，推荐 80）
- `--interval`: 定时器间隔（微秒，默认 1000）

**测试期间注意事项**:

1. **不要**运行其他高负载任务
2. **避免**频繁的磁盘 I/O 和网络活动
3. **可以**用 SSH 监控，但避免交互式操作
4. 长时间测试建议在 `screen` 或 `tmux` 中运行

**输出文件**:

```
results/
├── cyclictest_20261001_100000.log     原始日志
├── cyclictest_20261001_100000.json    JSON 统计
└── cyclictest_20261001_100000.hist    延迟直方图
```

### 第四步：分析结果

```bash
chmod +x rt_analyze.py
./rt_analyze.py results/cyclictest_20261001_100000.json
```

**报告示例**:

```
======================================================================
XTOS Real-Time Latency Test Report
======================================================================

Test Configuration:
  Timestamp:     2026-10-01T10:00:00+08:00
  Duration:      86400s (24.0h)
  Priority:      80 (SCHED_FIFO)
  Threads:       2
  Interval:      1000us

System:
  Hostname:      rt-server-01
  Kernel:        6.6.0-rt15
  CPU:           Intel(R) Xeon(R) Silver 4214R

Per-Thread Latency (microseconds):

  Thread   PID      Cycles      Min      Avg      Max    Range
  ----------------------------------------------------------------
       0   12345   86400000        2        4       15       13
       1   12346   86400000        2        3       12       10

System-Wide Summary:
  Worst-case latency:  15 us
  Best-case latency:   2 us
  Mean average:        3.50 us
  Total cycles:        172,800,000

Real-Time Classification:
  ✓ Hard real-time capable (max: 15 us < 100 us)

Notes:
  - These results are environment-specific
  - Real hardware with proper tuning required for guarantees
```

**报告文件**: `results/cyclictest_20261001_100000.report.txt`

---

## 结果解读

### 延迟指标

- **Min**: 最小延迟（理论最优）
- **Avg**: 平均延迟（典型性能）
- **Max**: 最大延迟（最坏情况，**最关键**）
- **Range**: 延迟抖动范围

### 实时性分类

| 最大延迟 | 分类 | 适用场景 |
|---------|------|---------|
| < 100 us | 硬实时 | BCI、工业控制、医疗设备 |
| < 1 ms | 软实时 | 音视频处理、游戏、AR/VR |
| ≥ 1 ms | 非实时 | 通用计算 |

### 常见问题

**最大延迟过高（> 100 us）**:

1. 检查是否在虚拟机中运行（虚拟化会显著降低 RT 性能）
2. 验证 PREEMPT_RT 内核和 CPU 隔离配置
3. 检查 `irqbalance`、THP、swap 是否已禁用
4. 查看 `/proc/interrupts` 中断是否固定在非隔离核心
5. 运行时是否有其他高负载进程

**不同线程延迟差异大**:

- 检查 CPU 亲和性设置
- 验证各核心负载均衡

---

## 压力测试（可选）

在另一终端运行负载生成器，验证系统抗干扰能力：

```bash
# 编译压力
stress-ng --cpu 4 --io 2 --vm 1 --vm-bytes 1G --timeout 3600s

# 或使用 hackbench
hackbench -l 10000 -g 10
```

实时测试应**在压力下**仍保持低延迟。

---

## 数据归档

完成测试后，将结果提交到仓库的本地 `docs/` 目录（**不推送到 GitHub**）：

```bash
cd /path/to/xtos
mkdir -p docs/rt-validation-results
cp tools/realtime-test/results/cyclictest_*.{json,report.txt,hist} \
   docs/rt-validation-results/

# 记录硬件信息
cat > docs/rt-validation-results/hardware_$(date +%Y%m%d).txt <<EOF
Hostname: $(hostname)
CPU: $(lscpu | grep 'Model name' | cut -d: -f2- | xargs)
Memory: $(free -h | awk '/^Mem:/ {print $2}')
Kernel: $(uname -r)
Test date: $(date --iso-8601=seconds)
EOF
```

---

## 下一步

完成真实硬件验证后：

1. 更新 `STATUS.md` 记录验证结果
2. 更新开发计划标记阶段 4A 完成
3. 继续阶段 4B：ARM64 交叉编译和 QEMU 验证

---

## 参考资料

- [Real-Time Linux Wiki](https://wiki.linuxfoundation.org/realtime/start)
- [cyclictest 文档](https://wiki.linuxfoundation.org/realtime/documentation/howto/tools/cyclictest)
- [PREEMPT_RT HOWTO](https://wiki.linuxfoundation.org/realtime/documentation/howto/applications/preemptrt_setup)
- XTOS 开发计划：`plans/XTOS_Next_Development_Plan_Rev2.md`
