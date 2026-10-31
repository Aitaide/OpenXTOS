# Ganglion Linux BLE 源码接口

状态：源码实现及聚焦软件验证；独立复核、完整维护、镜像和物理设备是不同验收门，本文件不授予 P4 阶段验收。

## 适用范围

固定协议为 `openbci-ganglion-v2.0.1`，沿用 `GanglionV2Decoder`，不通过 packet ID 猜测固件版本。现有同步 `GanglionPlugin` 保持 fixture-only，不因新增后端改变行为。

`ganglion_ble.py` 提供异步 `BlueZTarget`、`GanglionBLETransport` 和 `GanglionBLESession`。Linux 后端使用可选 Bleak 3.0.1；导入设备源码不自动导入 Bleak，不安装依赖、不连接设备。后端只连接调用方明确给出的 BlueZ 目标，不调用扫描、配对、取消配对、固件复位或自动重连。

设备必须已在调用方管理的 BlueZ 对象树中可见。地址采用未归一化的六组大写十六进制字节，适配器采用 `hciN`。两者构成明确的 `/org/bluez/hciN/dev_XX_XX_XX_XX_XX_XX` 路径。地址、路径、服务 UUID 和协议匹配都不是身份认证。

## GATT 合同

- 服务：`0000fe84-0000-1000-8000-00805f9b34fb`。
- 通知：`2d30c082-f39f-4ce6-923f-3484ea480596`，要求 notify 属性。
- 写入：`2d30c083-f39f-4ce6-923f-3484ea480596`，要求 write-without-response 属性。
- 在服务内要求唯一匹配，使用解析出的 characteristic 对象，不只按 UUID 寻址。
- 显式选择 BlueZ `StartNotify`，避免可选库版本对通知机制的默认值变化。
- 会话只写 `b` 开始和 `s` 停止，不开放任意固件命令。无应答写入成功不能证明固件已经开始或停止采样。

每次通知是独立不可变字节快照，长度 1–20 bytes，不拆包、不拼接。队列至多 64 条；畸形通知、队列溢出及异常断连清空队列并唤醒等待者，不静默丢包后继续输出正常样本。接收时间在通知回调边界采集，与读取/解码时间分开；不是设备时间或无线端到端延迟。

## 生命周期

所有异步方法由同一 asyncio loop 持有。操作使用绝对 monotonic_ns deadline；相互冲突的生命周期操作拒绝，不能重叠更换客户端。一个会话只允许一个等待中的 reader。disconnect/cancel 必须唤醒 reader。

connect 创建新 generation；通知和断连回调捕获该代次。旧回调、旧令牌及失败代次的数据不能污染重连。只有明确断开并完成清理后才能显式重连，不做自动恢复。

取消或异常时保留首要错误，并以独立清理预算尝试停止通知、断开连接；已启动会话尝试发送停止命令。清理失败明确暴露，不得伪装成完全断开。若第三方 coroutine 不响应取消，不能据此声明绝对执行时限。

## 来源与安全边界

注入测试 client factory 时来源固定为 `fixture_replay`；默认 Linux 后端声明 `live_device` 仅表示选择真实 I/O 路径，不证明设备或固件身份。所有输出保持 `Authentication.NOT_CHECKED`、`no_action`、`physical_device_evidence=False`。四通道、名义 200 Hz 与整数名义纳伏沿用固定协议，不是实测速率或物理校准。

CPU 私有运行时和 Live 基础锁不隐式加入 Bleak/BlueZ。source-only installer 只安装 XTOS 模块，不捆绑可选依赖；默认后端在依赖缺失、版本不符或平台不符时拒绝执行。即使 host 可导入可选库，也不代表 Live ISO 已具备该执行路径。

## 软件验证

从仓库根运行：

```sh
python3 -B -m unittest discover -s tests -p 'test_xtos_ganglion_ble.py' -v
python3 -B -O -m unittest discover -s tests -p 'test_xtos_ganglion_ble.py' -v
python3 -B -m unittest discover -s tests -p 'test_device_serial_install.py' -v
python3 -B -m unittest discover -s tests -p 'test_p4_device_plugin_integration.py' -v
```

GATT client fixture 验证软件控制流，不替代真实 BlueZ 服务、GATT、无线连接、拔插及持续采样。当前阶段不关闭新镜像、guest、实板、物理身份、抖动/延迟、医学或硬实时验收门。

## 参考

- OpenBCI 官方 GATT UUID：https://docs.openbci.com/Ganglion/GanglionDataFormat/
- OpenBCI 命令：https://docs.openbci.com/Ganglion/GanglionSDK/
- 固定 v2.0.1 固件源码：`OpenBCI/OpenBCI_Ganglion_Library`，commit `4f53fdbc730020d6101ec730611fb9da3407f44b`。
- Bleak 使用与断连约定：https://bleak.readthedocs.io/en/stable/usage.html

参考文档可能更新；接收通知的字节合同以固定固件和维护测试为准。
