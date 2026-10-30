# 设备插件：Linux 串口接入合同 v1

状态：开发中。软件验收基线为 clean 提交 c026f55ef1f405df151f2a318531063a1a890fa6 的 P3 镜像；本合同及后续 P4 改动不继承该镜像验收。

## 范围

提供 Python 标准库的 Linux 非阻塞串口传输，以及复用 canonical CytonPacketParser 的 Cyton 会话。既有 xtos_devices.py fixture API 保持兼容，新接口显式区分超时、取消和断开。不进行自动设备发现，不声称串口描述、固件 banner 或协议匹配能够认证设备身份。

## 串口传输 API

模块 tools/xtos-devices/serial_transport.py：

- LinuxSerialTransport(path)：构造不打开设备、不创建线程。path 为显式绝对字符设备路径；拒绝最终路径符号链接。打开后以 fstat 和 tcgetattr 验证实际 fd。PTY 是软件测试输入。
- open(*, deadline_ns)：绝对 time.monotonic_ns 纳秒期限，必须为正整数且未到期；成功返回 self。使用 O_RDWR|O_NOCTTY|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW、TIOCEXCL，配置 raw 115200 8N1、无软硬流控。记录原 termios。重复 open 拒绝；失败释放本次全部 fd 和独占状态。
- connected：只读布尔属性，表示 fd 已打开，不表示协议匹配、身份认证或物理验收。
- read(max_bytes, *, deadline_ns)：容量为整数 1..132。仅返回非空 bytes；空闲/EAGAIN/EINTR 保持同一绝对期限，超时抛 SerialTimeout，不制造 EOF。poll 的 HUP/ERR/NVAL 或 read 为零为 SerialDisconnected，按 fail-closed 丢弃同时就绪的数据。返回前再次检查取消和期限，不发布已过期字节。
- write_all(data, *, deadline_ns, cancel_sensitive=True)：非空 bytes、长度不超过8192；短写和零进展均受原绝对期限约束；成功返回 None。普通写检查取消；cancel_sensitive=False 只供插件清理时有界发送停止命令使用，仍检查 owner 和期限。不调用无界 tcdrain。
- request_cancel()：唯一允许跨线程调用的方法，唤醒 owner 的 poll，不在该线程关闭 tty。取消是本次连接的锁存状态；尚未 open 或已 close 时为无操作，不污染下一次 open。
- close()：owner 调用，重复关闭无操作；尝试恢复原 termios、释放 TIOCEXCL 并在 finally 关闭 tty 和唤醒 pipe。恢复失败仍释放全部资源并抛 SerialError，由插件保留为 cleanup_error。关闭后可再次 open，使用新 pipe 和取消状态。非 owner 的 I/O/open/close 拒绝且不修改现有会话。
- 异常：SerialError(RuntimeError)、SerialTimeout(SerialError)、SerialCancelled(SerialError)、SerialDisconnected(SerialError)。无效调用参数抛 ValueError，超时和取消不能伪报成功。

期限约束覆盖用户态循环、poll 和重试；不保证任意内核 tty ioctl、驱动阻塞、Python 调度或物理采样延迟具有硬实时上界。

## Cyton 会话

模块 tools/xtos-devices/cyton_serial_plugin.py；显式选择 reset_defaults=True 才允许发起连接。v 会重置设备配置，不承诺恢复先前板卡配置。握手发送 s、v，读取 $$$ 终止的有界 ASCII banner；再发 ? 读取 Board ADS Registers。单次命令最多8192字节，banner正文最多255字节，事务期限为调用者总期限与3000ms上限的较早者。不得逐字节重置期限。

只接纳已读回确认的8通道、全部启用、gain24、250Hz默认配置；验证 CONFIG1 和 CH1SET..CH8SET 的地址、十六进制值及逐位读回一致。拒绝 Daisy、缺失/重复行、未知或不匹配配置。只有通过握手和配置验证后才允许 b。b/s 没有板卡 ACK，写入成功不等于板卡已开始或已停止采集。

统一样本采用现有 DeviceIdentity、Capabilities、Sample，authentication=NOT_CHECKED；protocol_match 单独记录。调用者标签不是唯一物理身份。显式输入来源为 fixture_replay 或 live_device，PTY 必须标 fixture_replay；所有样本保持 no_action、physical_device_evidence=False。时间戳明确为 canonical parser 在主机解码时读取的 monotonic 时间，不假定为 ADC 采集时刻或 tty read 完成时刻，不合成 +1ns。

会话单 owner，有界事件历史，手动重连。每次连接尝试增加 generation、清除旧 parser/配置/匹配状态；stale generation 在任何 I/O 之前拒绝。取消或错误后，由 owner 在独立100ms清理预算内尝试 s（忽略已锁存的取消），随后始终 close；主异常和清理异常分别保留，清理失败不能伪报成功。严格拒绝噪声、坏 footer、重复/乱序/缺号和半包 EOF，不发布该失败 read 中的样本。

## 验证与未验证边界

测试必须实际打开临时 PTY，覆盖空闲、分片、HUP、取消唤醒、短写/无进展、EINTR/EAGAIN及绝对期限、termios恢复和 fd 所有权；使用官方格式的 banner/寄存器会话模拟板卡，比较 canonical parser 样本和独立整数 oracle。保留每个 RED/GREEN 的源码、测试、命令、退出码与日志。

聚焦软件测试不等于完整维护、安装镜像或物理板卡验收。真实 USB 串口权限、无线桥接、固件版本、拔插、持续采集、电极与参考仪器、延迟均为独立门。Ganglion、BLE、网络传输及 GPU/XR 不属于此首切片。
