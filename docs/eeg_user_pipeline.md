# 虚拟EEG用户态数据管线

## 范围

`tools/xtos-eeg-pipeline/`把虚拟EEG CSV逐帧转换为XTOS统一样本结构，写入固定容量SPSC队列，再由消费者取出并校验样本完整性。它是用户态测试管线，不是内核设备，也不等同于真实EEG采集。

## 命令

```sh
cd tools/xtos-eeg-pipeline
make clean && make test
./xtos-eeg-pipeline --input fixtures/valid.csv
```

程序输出JSON摘要，错误和异常说明写入stderr。退出码：

- `0`：输入完整，队列无拒绝。
- `2`：文件、格式或内存错误。
- `3`：时间戳异常、采样间隔异常或队列数据完整性错误。

## 实际测试

```text
Ran 3 tests ... OK
```

正常fixture实际摘要：

```text
status=ok
frames_in=4
frames_out=4
interval_anomalies=0
timestamp_anomalies=0
rejected_full=0
p50_latency_ns=68
p95_latency_ns=1210
p99_latency_ns=1210
```

本次延迟只表示当前空载进程中`push`、`pop`和校验的用户态测量，受CPU、调度和测量方法影响，不是端到端EEG延迟，也不是PREEMPT_RT硬实时指标。

异常fixture验证：

- `bad_interval.csv`：4帧均通过队列，采样间隔异常1次，状态`degraded`，退出码3。
- `bad_order.csv`：检测到非单调时间戳，状态`degraded`，退出码3。

## 当前限制

- 当前是单生产者/单消费者模型。
- 队列容量为16，满时返回`EAGAIN`，不覆盖旧数据；本次管线逐帧消费，未触发满队列。
- 尚未加入滤波、FFT、P300或SSVEP特征处理。
- 尚未连接真实EEG、内核DMA、USB、BLE或OpenBCI物理串口。
