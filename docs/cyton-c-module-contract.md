# Cyton C 模块接口冻结（实现前）

目录 `tools/xtos-cyton/`；用户 CLI 为 `bci`，父集成者负责 `main.c`、Makefile、安装和全局测试。

## Stream engine（独占 bci_stream.h / bci_stream.c / test_bci_stream.c）

```
#define BCI_CHANNELS 8
#define BCI_QUALITY_GAP 1u
#define BCI_QUALITY_RECOVERED 2u
struct bci_input_frame {
    uint64_t sequence, generation, receive_ns;
    uint32_t sample_rate_hz, quality;
    int32_t samples_nv[8];
};
struct bci_output_frame {
    struct bci_input_frame input;
    uint64_t processed_ns;
    uint32_t filter_reset;
    double filtered_nv[8];
};
struct bci_stream;  /* one producer caller, one internal consumer thread */
typedef int (*bci_output_fn)(const struct bci_output_frame *, void *);
struct bci_stream *bci_stream_create(size_t capacity, bci_output_fn, void *);
int bci_stream_push(struct bci_stream *, const struct bci_input_frame *);
int bci_stream_close(struct bci_stream *); /* drain/join, error propagated */
void bci_stream_destroy(struct bci_stream *); /* after producer stopped */
struct bci_stream_stats { uint64_t accepted, processed, dropped_full, invalid, resets, output_errors; };
void bci_stream_get_stats(const struct bci_stream *, struct bci_stream_stats *);
```

固定 250Hz、5–40Hz，八个独立滤波器。push 不阻塞满队列，返回 -1/errno=EAGAIN，不覆盖旧帧。generation/sequence 单调且同代次 receive_ns 不得倒退（同一接收批次可相同）；入队后 caller 可覆写原 frame。generation 或 sequence 缺口/quality GAP 重置滤波。所有内存初始化时分配，工作线程不分配；stats 原子。输出 callback 是消费者线程，慢消费者会造成有界丢新帧；关闭要求 callback 有界返回，不允许谎称能终止任意 callback。

## Serial transport（独占 cyton_serial.h / cyton_serial.c / test_cyton_serial.py；可新建 fixture C）

```
struct cyton_board_config {
    char identity[256];       /* actual bounded v response, no fabricated ID */
    uint32_t gain[8];         /* decoded CHnSET bits, whitelist 1,2,4,6,8,12,24 */
    uint32_t enabled_mask;    /* actual powerdown bit */
};
struct cyton_serial_options {
    const char *device;
    unsigned duration_ms, command_timeout_ms, reconnect_delay_ms, reconnect_attempts;
};
enum cyton_serial_event { CYTON_CONNECTED, CYTON_BYTES, CYTON_DISCONNECTED };
typedef int (*cyton_serial_callback)(enum cyton_serial_event,
    const struct cyton_board_config *, const unsigned char *, size_t,
    uint64_t generation, uint64_t receive_ns, void *);
struct cyton_serial_stats { uint64_t connections, reconnects, bytes, disconnects, stop_errors; };
int cyton_serial_run(const struct cyton_serial_options *,
    volatile sig_atomic_t *stop, cyton_serial_callback, void *, struct cyton_serial_stats *);
```

API 完整定义需 include signal/stdint/stddef。接受实际字符 tty（含部署常用 by-id symlink），open 后 fstat/termios 验证；不读普通文件/FIFO。bounded poll 非阻塞，duration 总预算，stop flag 检查；连接后先 s 停流，再 v 等待 $$$，? 读取 ADS1299 CH1SET–CH8SET 后解析，校验 identity 包含真实 Cyton/ADS1299 标识。协议依据以官方 SDK / BrainFlow 源码核实，文本 fixture 不自造格式。gain/identity 不完整明确失败；只有真实读回可 CONNECTED 并发 b。断开事件后有限重连，重新 probe/config，identity 变化拒绝，不拼接半包；generation 从1递增。每连接 finally 尽力 s，停止失败统计并返回错误。callback 返回非0退出并停流；无设备操作，PTY 测试。

父 main 在 CYTON_CONNECTED 重置共享 packet parser，保存 config。BYTES 中完整解码 raw ADC 并按各通道实际 gain 换算 nV，样本 sequence 按接受 counter 增量生成；GAP 和 generation 进入 engine，receive_ns 是 host monotonic 收到包时钟，不用名义采样网格冒充板时钟。filtered JSONL 每帧保留8通道、代次和质量。所有结果 physical_evidence=false。
