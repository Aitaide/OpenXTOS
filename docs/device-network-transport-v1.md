# 设备网络传输：显式 TCP 字节接口 v1

状态：显式 TCP 字节传输已在 clean 提交 5cb533e4be467dc7e93bec684aec1367a1bb1784 完成源码与 candidate-61 本地镜像软件验收。真实 loopback、九模块安装、普通用户 guest 和四模式启动证据分别保存；这不代表厂商设备协议、认证或物理采样验收。后续源码与本状态更新不继承 candidate-61 PASS。

## 范围

提供 Python 标准库非阻塞 TCP 客户端字节传输，不定义厂商设备线协议，不增加设备发现、DNS、自动重连、无线或路由配置。真实 socket 的 loopback 测试属于软件传输验证；远端硬件采样、身份和物理延迟分别验收。接口独立于既有 fixture-only Transport Protocol，不把原来的同步模拟接口改为阻塞网络 I/O。

TCP 本身不认证或加密对端；调用者必须在其自主管理的网络中显式选择端点。连接成功只表示 TCP 已建立，不表示设备协议、板卡身份或动作权限通过。后续面向不可信网络的认证会话需另立合同，本切片不接入生产动作通路。

## API

模块 tools/xtos-devices/network_transport.py：

- NetworkError(RuntimeError)、NetworkTimeout(NetworkError)、NetworkCancelled(NetworkError)、NetworkDisconnected(NetworkError)。参数错误为 ValueError。异常类别独立于串口，不能伪造 EOF 或成功。
- TCPTransport(address, port)：构造不执行 I/O、不创建线程。address 必须是无 zone 的 IPv4/IPv6 数字地址，拒绝主机名、空白、NUL；port 为非 bool 整数 1..65535。严格验证后才创建任何 socket。无 DNS、连接或扫描。
- open(*, deadline_ns)：使用正整数 time.monotonic_ns 绝对期限；过期拒绝。创建非阻塞、不可继承 socket，connect_ex 后通过 poll 和 SO_ERROR 确认连接。失败关闭本次 socket/唤醒 pipe，不发布 connected；不自动重试另一地址。重复 open 拒绝。
- connected：只读布尔属性，表示会话 socket 已发布；不是物理连接健康或认证证明。
- read(max_bytes, *, deadline_ns)：max_bytes 为非 bool 整数 1..132。成功返回非空 bytes；空闲、EINTR、EAGAIN 使用原绝对期限。recv 返回零或终态 socket/poll 错误抛 NetworkDisconnected。poll 同时带终态错误及数据时 fail-closed，不发布该批数据。返回前再次检查期限与取消。
- write_all(data, *, deadline_ns)：data 是非空 bytes，最多8192字节。短写、无进展、EINTR/EAGAIN 都受同一绝对期限约束。成功返回 None；send 为零视为断开，不自旋。返回前再次检查取消和期限。
- request_cancel()：唯一允许跨线程调用的方法。取消锁存于本会话，用非阻塞 pipe 唤醒 owner；最多三次 EINTR 写重试。poll 至多50ms检查一次锁存，避免 wake 写失败后等到整个调用期限。尚未 open 或已 close 是无操作，不污染下一次 open。请求不得触碰已关闭后被 OS 复用的 fd。
- close()：owner 调用；重复关闭无操作。先在与 request_cancel 共用的资源寿命锁内撤下连接和 pipe，再独立尝试关闭全部本次资源。即使一项关闭失败也继续释放其他项并抛 NetworkError。关闭后允许重新 open，新会话不能接收到旧取消、旧 fd 或旧数据。
- open/read/write_all/close 只允许单个 owner；成功打开后由该线程持有，非 owner 操作明确拒绝且不改变当前会话。open 进行期间不支持另一个线程操作；request_cancel 的唤醒保证覆盖已发布会话，构建中的连接仍由调用者的原期限约束。

read/write 的错误和超时不自动重连；调用者必须显式 close 后重新 open。调用者必须把失败操作已接收或已发送的前缀视为不确定，不能重放命令假设 exactly-once。模块不缓存 payload、不创建后台 reader、不生成 Sample 或伪造认证/provenance。

## 验证门

1. 测试先于实现，逐增量保留 RED/GREEN 的源码、测试、直接依赖、命令、退出码和原始日志。缺模块入口失败只证明 API 缺失，后续边界分别保留行为 RED。
2. 实际 IPv4 loopback listener 验证分片/容量、双向传输、EOF、空闲期限、取消和手动重新 open；IPv6 在环境支持时单列，不把跳过算通过。不访问外部网络。
3. 在真实 socket 边界注入短写/EINTR/EAGAIN、期限越界和 SO_ERROR；控制调度验证取消写与 close 的 fd 复用边界，检查异常、线程退出及资源释放，不让 test cleanup 掩盖泄漏。
4. normal 与 python -O 全部聚焦回归通过，独立 SPEC 后 QUALITY。完整维护、source-only installer、已安装模块运行、新 ISO/guest 和物理设备分别记录，不把聚焦 PASS 当成 P4 完成。
