# 信号处理基准

## 目的

`tools/xtos-signal-benchmark/`为当前C信号处理实现提供可重复的用户态基准入口。它记录固定窗口下的FFT功率谱、带通滤波和SSVEP特征三条路径的时间分布，同时记录CPU时间和峰值RSS。

该基准用于比较同一主机、同一编译参数和同一提交下的变化。结果不能推导PREEMPT_RT硬实时上界，也不能代表真实EEG采集端到端延迟。

## 固定输入

- 窗口长度：256点
- 采样率：256Hz
- 通道数：2，当前基准操作使用第1通道
- 带通：5–40Hz
- SSVEP：10Hz基频、2个谐波、每侧1个邻频
- 默认迭代次数：1000
- 线程数：1
- 编译参数：`-std=c11 -Wall -Wextra -Werror -O2`

输入由程序内固定公式生成，包含10Hz、20Hz、9Hz成分和通道偏置；不读取外部文件。

## 运行

```sh
./scripts/benchmark_signal.sh
```

结果写入`.build/benchmarks/xtos-signal-baseline-v1.json`，并包含Git提交、内核、CPU型号和编译器元数据。改变迭代次数：

```sh
ITERATIONS=10000 ./scripts/benchmark_signal.sh
```

每个操作输出p50、p95、p99、最大墙钟耗时、总墙钟耗时、用户CPU时间、系统CPU时间和峰值RSS。性能数字随CPU、虚拟化、调度和系统负载变化。

## 当前主机示例

一次1000次运行的结果曾记录为：

- FFT功率谱：p50约5.5微秒
- 带通：p50约1.7微秒
- SSVEP特征：p50约5.6微秒

这些数字只用于说明输出尺度，不作为跨主机承诺；完整原始JSON不进入Git。