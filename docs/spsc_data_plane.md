# 阶段3A：SPSC数据平面

## 目标

在用户态提供固定容量、单生产者/单消费者（SPSC）EEG帧队列，为虚拟回放和后续滤波处理提供确定的边界行为。当前实现不接触内核DMA，也不宣称可直接替代内核设备缓冲区。

## 数据结构

每帧包含：

- `timestamp_ns`：纳秒时间戳，不能为零。
- `sequence`：帧序号。
- `channels`：必须与队列创建时一致。
- `sample_rate_hz`：采样率。
- `quality_flags`：质量标记。
- `samples_nv`：按通道排列的有符号纳伏整数样本。

## 队列规则

- 创建时固定容量和通道数。
- `push`复制帧元数据和样本，不保存调用者指针。
- 空队列`pop`返回`-1/EAGAIN`。
- 满队列`push`返回`-1/EAGAIN`，不覆盖旧数据。
- 通道数、时间戳、输出缓冲容量错误返回`-1/EINVAL`。
- 生产者和消费者通过C11原子head/tail同步。
- 统计计数使用原子计数，避免并发读写数据竞争。

## 验证

```sh
cd sdk/libxtos-rt
make clean && make test
```

实际结果：

```text
./test_rt
PASS assertions=12
./test_spsc
PASS assertions=28
```

额外使用ThreadSanitizer编译并运行SPSC并发测试：

```sh
cc -std=c11 -I./include -Wall -Wextra -Werror -O1 -g \
  -fsanitize=thread -pthread -o test_spsc_tsan \
  tests/test_spsc.c src/spsc.c
./test_spsc_tsan
PASS assertions=28
```

测试覆盖：空队列、满队列、顺序、样本复制、非法帧、通道数错误、输出缓冲区过小、生产者/消费者并发10000帧和统计值。

## 当前限制

这是严格的SPSC模型，不能由多个生产者或多个消费者直接共享。需要多路采集时，应为每个生产者建立独立队列，或另行实现经过测试的MPMC结构。当前尚未连接虚拟EEG回放器，也尚未给出端到端处理延迟数字。
