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

## OpenBCI Cyton 串口采集与回放（新增用户态切片）

协议依据：[OpenBCI Cyton Data Format](https://docs.openbci.com/docs/02Cyton/CytonDataFormat)。当前解析器按 Cyton 33-byte、8 通道、250Hz、24-bit big-endian signed 样本和 `0xC0–0xCF` footer 实现；用户态转换按默认 gain 24 的 integer nV scale 计算。设备可改 gain，因此 manifest 会标注“默认值、需核对板卡配置”，不得把未读取的实际 gain 当成已确认事实。

```sh
# 采集已由板端/人工启动的数据流；输出目录必须不存在
python3 tools/xtos-eeg-pipeline/cyton_capture.py \
  --device /dev/ttyUSB0 --output /path/to/new-capture --duration-s 30

# 显式控制 Cyton stream：发送 b 开流，结束时发送 s
python3 tools/xtos-eeg-pipeline/cyton_capture.py \
  --device /dev/ttyUSB0 --output /path/to/new-capture --duration-s 30 --start-stream

# 校验 raw/frame hash、重新解析原始字节，并把八通道分四对送入 5–40Hz 滤波器
python3 tools/xtos-eeg-pipeline/cyton_replay.py \
  --capture /path/to/new-capture --output /path/to/new-processed

make -C tools/xtos-eeg-pipeline test
```

Capture bundle 含原始 `capture.bin`、`frames.jsonl` 和 `manifest.json`；捕获路径独占创建、大小有上限，丢样、坏包、设备 EOF、容量截断、stream-stop 失败和尾部半包会在 manifest 留下状态与错误。Replay 只接受 `completed` 的无丢样 capture，并重新计算 raw/parser/frame 一致性；partial/error capture 保留调查证据但不进入滤波。现有滤波接口每次处理两通道，所以 replay 逐对处理四次。Cyton 的 33-byte 帧含 sample counter；协议的 AUX/footer 变体可能包含板端时间字段，但本切片只保留其原始字节，不解释为设备时钟。滤波使用从首个 host receive timestamp 起按 250Hz counter 重建的名义网格，不能当设备时钟、端到端延迟或实时性能证据。

这部分使用 PTY 和合成协议包验证软件行为，没有连接真实 OpenBCI。需要实机确认板卡/固件/gain、权限、开关流语义、实际断连恢复、持续吞吐与设备时间戳；真实设备验收报告和物理证明仍按 `eeg_device_validation_contract-v1` 单独归档。BLE、OpenBCI Ganglion、通用 USB 插件、内核 serdev/DMA 生命周期不在此切片内。

system rootfs 和 Developer Live 构建现在会把 `openbci_cyton.py`、`cyton_capture.py`、`cyton_replay.py` 复制到 `/usr/share/xtos/eeg-pipeline/`；回放器默认会查找同目录旁的 C 管线、staged rootfs 的 `/usr/bin/xtos-eeg-pipeline`，以及宿主 `/usr/bin/xtos-eeg-pipeline`。工具要求 Linux、Python 3.10 或更新版本、完整标准库（含 `ctypes`、`termios`）和 libc 的 `renameat2` 符号；原子发布还要求内核/文件系统支持 `RENAME_NOREPLACE`。system rootfs 本身不内置 Python，只执行 source-staged 检查，源码存在不代表可运行。Developer Live 在安装 SDK 后执行 chroot 合成采集/默认回放烟测，结果及 Python/源文件/C 管线哈希写入构建清单的 `cyton_runtime`。C 版 `xtos-eeg-pipeline` 仍是独立的 CSV/SPSC 基线。

构建机检查命令：

```sh
# 精简 system：仅检查源码、C 管线和权限
python3 scripts/verify_cyton_runtime.py --rootfs /path/to/rootfs --mode source-staged
# Live 静态检查：另外要求打包的 Python 解释器
python3 scripts/verify_cyton_runtime.py --rootfs /path/to/rootfs
# 仅对可信、独占的 staged root 执行；需要 root，临时输出位于该 root 的 /run
python3 scripts/verify_cyton_runtime.py --rootfs /path/to/rootfs --smoke
```

默认检查不执行目标程序；`--smoke` 使用目标 Python 导入三个模块，通过匿名 pipe 提供四帧合成包，再用打包的默认回放 CLI 处理八通道并检查滤波文件，临时目录正常退出时删除。它使用构建机内核，不是串口测试、镜像启动或实机性能证据；chroot 不是恶意二进制的隔离沙箱，检查期间 staged tree 应保持不变。历史 package lock/镜像不会被自动改写；新的 profile 契约由新构建单独记录，candidate-54 不含这批功能。

---

## 内核 Cyton 协议与控制路径（初始切片历史边界）

以下段落描述 `.build/cyton-kernel-closure-v1/` 对应的早期 parser/control 切片，不是当前源码功能清单。后续本地软件候选增加了实际配置/gain/固件读回、设备/FD/VMA 生命周期、不可变旧快照和版本化逐帧接口，详见 `STATUS.md`、`bci-frame-uapi-v1.md` 与 `cyton-device-functions.md`；其历史镜像证据仍只绑定各自冻结版本。

内核驱动与 host 回归/fuzz 共用 `kernel/linux/drivers/xtos/bci/xtos_cyton_packet.h`：33-byte 包从 offset 2 解码八通道 signed24，校验 `0xC0–0xCF` footer，以默认 gain 24 转换为 `s32` 纳伏，按 sample counter 检查回绕、重复、逆序和缺口，坏包保留有界候选后缀以重新同步。数据面仍为八个连续的 `s32` 通道值；它没有版本化的帧序号、时间戳和质量元数据，不能当完整实时 EEG 帧接口使用。默认 gain 是转换假设，未读取实际板卡配置。

控制路径只支持 115200 串口可持续传输的 250Hz 模式；更高采样率、校准和阻抗检查明确拒绝。串口写入必须完整，通道启用使用 `!@#$%^&*`、停用使用 `12345678`。控制命令串行化，接收状态/解析器/缓冲区生命周期单独保护；同步 TX 等待期间不持有接收状态锁。serdev descriptor 与锁必须在接收回调可见前初始化，开流前准备解析器，失败路径停止本地接收并尝试停流。

```sh
bash scripts/test_cyton_kernel_parser.sh
python3 tests/test_cyton_driver.py
python3 tests/test_cyton_kernel_integration.py
python3 tests/test_cyton_fuzz.py
make -C tools/xtos-fuzz check
```

维护配置要求 `CONFIG_SERIAL_DEV_BUS=y` 和 `CONFIG_SERIAL_DEV_CTRL_TTYPORT=y`；后者在 bus 为模块时不会被 Kconfig 启用。普通 USB tty 不会自动成为固件枚举的 serdev 设备，用户态串口工具仍是独立接入路径。host shim 直接编译实际驱动源码，验证回调和故障交错；它不能替代内核运行、实际 UART 或物理设备验证。内核模块构建、QEMU 加载与实机门分别记录，当前证据索引为 `.build/cyton-kernel-closure-v1/`。

BCI core 的既有 open FD/unbind、驱动 owner 和 VMA 生命周期，以及 mmap 的分配设备/消费协议尚需单独开发，不能由本轮 parser/control 修正推导热插拔安全。统一八通道实时 SPSC、积压/背压恢复、身份/固件/gain readback 和自动重连仍未实现。新的共享解析器和驱动修改尚未进入 candidate-54 ISO。

## 当前限制

以下 CSV 容量和滤波说明只适用于原有 CSV/replay 工具。当前源码另有原生 `bci` 八通道流和 `eeg-stream` 逐帧窗口生产者，以及 Python `eeg-loop` 消费/模拟反馈；接口和来源边界分别见 `eeg-stream-adapter-v1.md`、`eeg-stream-export-v1.md`、`eeg-stream-process-v1.md`。它们不能从 CSV fixture 结果继承设备或闭环识别验收。

- 当前 CSV/SPSC pipeline 是双通道 fixture path；Cyton 回放通过四份双通道输入复用现有滤波器，不提供统一八通道实时 SPSC 生产者。
- 队列容量为16，满时返回`EAGAIN`，不覆盖旧数据；当前 pipeline 逐帧消费，未触发满队列。
- 已加入确定性信号处理基线：滤波、FFT、P300和SSVEP特征测试。
- P300 当前是单窗口 ERP 特征和阈值基线，尚未形成经真实协议验证的闭环分类器。
- 内核 OpenBCI serdev 驱动仍未完成硬件验收；不可从用户态 Cyton parser 的 fixture 通过推导内核驱动正确。
