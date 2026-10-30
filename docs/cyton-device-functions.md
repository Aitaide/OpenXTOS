# Cyton 设备功能与配置合同

## 配置查询 v1

新增 ioctl 16 `XTOS_BCI_GET_CONFIG`，使用固定 384-byte `struct xtos_bci_config_v1`，kernel/SDK 各有独立可安装头。现有 ioctl 和快照 v1 不改号/大小。

字段顺序：version、flags、channels、sample_rate_hz、enabled_mask、capabilities、reserved0[2]（均 u32）；generation（u64，offset32）；gain[8]（u32，offset40）；ADS registers[24]（u8，offset72）；firmware[256]（char，offset96）；reserved[8]（u32，offset352）。输出全部 padding/reserved 清零。

调用方发送 version=1，其余字段必须为零；内核先验证完整输入，设备回调只接受本地 zeroed output。输出 version=1、flags=0、channels=8、rate=250、enabled_mask<=255、generation>0、gain 各为1/2/4/6/8/12/24，firmware 必须 NUL 终止。registers 全部24个为实际读回，CHnSET 与 gain/enabled 一致；CONFIG1 的采样率与声明一致。firmware 是固件 banner，不是唯一物理 ID。只返回已成功完整验证的配置，未验证返回 ENODATA，断开返回 ENODEV。查询本身不重置板卡；非流状态可按明确 callback 合同刷新，流状态返回最后已验证配置。

capabilities：CONFIG_READBACK=1、FIRMWARE_READBACK=2、GAIN_CONTROL=4、IMPEDANCE=8、SELF_TEST=16。只声明实际实现且有验证路径的能力，未知位拒绝。配置成功提交时 generation 递增且不重用；传输失败或配置无法读回时清 valid 状态，禁止继续输出假设有效的帧。

SDK `xtos_bci_get_config(session, struct xtos_bci_config_v1 *out)`：NULL参数 EINVAL；内部零初始化请求，只设置版本；ioctl失败 errno 原样；输出结构、保留字段、flags、通道、rate、generation、增益、enable、register echo、字符串严格验证；非法 EPROTO，out 始终保持原字节。实际成功后一次性复制。一个session调用仍要求调用方串行化。

## 命令与采样边界

- 用户态既有 s/v/?/b 与串口停止语义保留。kernel serdev 增加有界文本应答；初始化 callback state 在 serdev open 前完成。
- control_lock 串行事务，state_lock 只保护短期接收/配置提交，不跨同步 TX、超时等待；命令响应与流二进制有明确状态。
- 开始采集前必须获取已验证配置，不得用 gain24 当实际读回。按各通道 gain 做整数 nV 转换；超出 s32 的样本拒绝而非裁剪。
- 写配置后的应答与寄存器读回必须一致；原API中的gain字段不能继续静默忽略。未知配置/恢复失败进入失效状态。
- acquisition_error 保存负 errno。故障时停止发布并唤醒 ring 等待者，poll 返回 EPOLLERR；先允许读完已有数据，空 ring 后 read 返回该故障，不伪装 EOF。成功的新初始化/START 才清故障，STOP 不抹除故障。

## 初始化和复位

官方 firmware commit `24e1c4269b60c3bec6d831bca03cae42f267b3c6` 的 `v` 会复位外设/配置，并非无副作用身份查询。现有用户态 bci 的 s/v/? 明确为 reset-default 采集模式；本阶段不改变这个已公开行为但更新文档。新内核 open 允许显式 reset-default 初始化（s/v/?），每次 START 前只刷新 ?，不得再次 v 清掉用户已设置的配置。V 返回固件版本，不能等价替代完整芯片身份 banner。关闭后 reopen 重新初始化，旧 ring 存在不代表配置有效。

## 校准与阻抗

权威资料保存在 `.build/xtos-roadmap-v1/protocol-research.json`；指向官方 firmware/GUI 固定 commit 和 TI ADS1299 Rev.C。`xCPGIBRSX` 为9字节通道配置；`zCpnZ` 为5字节激励掩码配置。完整多字节命令的 ACK 必须精确匹配，随后 ? 核对寄存器。停止流 s 不会关闭激励，部分命令需等待固件超时后再恢复，不能把 s 当任意字节位置的 abort。

固件不存在返回修正系数的校准命令。CALIBRATE 旧接口不改造成虚假成功；先增加有版本结果的 SHORTED 观察自检（逐通道 x...X，保留 CONFIG2），报告均值/方差/饱和/样本数和恢复结果，不生成或应用校正系数。真实校准能力继续独立列为需要可溯源参考的开发/验证门。全局 0/-/=/p/[/] 会修改 CONFIG2，未证明能够恢复时不得使用。

阻抗路径要求完整24寄存器快照，CONFIG1 250Hz，LOFF 低4位为2（名义6nA/31.25Hz），LOFF_FLIP 已知；可还原的对称 BIAS_SENSP/N 和 MISC1 的 SRB1 配置。独占会话，先关闭所有lead-off，再逐个指定通道/端子设置 gain1/NORMAL/bias included/SRB断开，激励单一P或N。有限窗口读取真实数字帧，拒绝缺口/坏帧/饱和/断连；采用明确命名的名义电流 RMS 估计，记录参考/串联电阻2200Ω假设，不将GUI公式命名为校准值。清理须独立有限deadline，不受取消标志阻断；先停流和关闭激励、恢复全部通道/共享SRB1/原lead-off，最后读取全部可写寄存器比较；任何恢复不一致使结果失败、禁止继续采集。

测量应先在用户态受控串口事务实现，kernel 回调不得因只发送命令就返回阻抗数值。没有对应内核有界采样/结果实现时 capabilities 不宣布 IMPEDANCE/SELF_TEST，旧 ioctl 不虚报支持。后续可通过同一有版本诊断结果实现内核路径。

软件测试的模拟电极/正弦输入仅证明计算与事务逻辑。名义激励电流 TI 规格±20%，参考回路、滤波和硬件修订影响估计；真实电极阻抗、校准精度、板卡安全与真实时延属于独立实机验收。
