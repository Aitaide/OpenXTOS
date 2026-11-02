# Linux 7.3.1 内核迁移

状态：内核升级与本地 Live 镜像切片已验收，后续功能继续开发。验收仅绑定干净提交 `32a8bd3903eda24bd44d7e1a19dea4156014d062` 与下述 candidate-62，不继承旧 candidate-61 或历史 6.6/rt15 的 PASS；后续新增源码不继承该 PASS。源码、构建、guest、完整维护、精确提交与整机镜像分别保留证据。

## 来源与边界

- 上游：Linux 7.3.1 stable；官方源包与签名网址由 `kernel/source-pin.json` 固定。
- 官方归档 SHA256：`fa595aad93734f829c963c2a90ef7c3673224fd7fdae7b49406e397cde2de231`。
- 发布签名主指纹：`647F28654894E3BD457199BE38DBBDC86092693E`，与 kernel.org 公布值交叉核对，实际验签通过。
- 使用主线 `CONFIG_PREEMPT_RT=y`，不将旧 rt15 补丁叠加到新源码，不伪造 `localversion-rt`。
- `kernel/patches/xtos-linux-7.3.1.patch` 由已验签上游与移植后源码生成；30 个补丁目标已从上游原文独立 replay，逐字节匹配且无 offset/fuzz。
- 旧源码及历史补丁保留，用于回退与追溯，不充当新版本验收。

## 已完成的迁移执行

x86_64 已实际完成完整 bzImage、模块链接及生产 BCI 三模块构建。更改保持公有 UAPI/SDK 布局不变，仅适配新 serdev `size_t receive_buf` 与移除的内核 `strncpy` 接口；设备名称零填充和有界复制行为保留。Cyton 原有 68 项行为回归在普通、ASan/UBSan 模式各通过一次；旧 fixture 的回调类型同步到新上游 ABI，不删除旧断言。

x86_64、ARM64 与 RISC-V 64 的真实 Linux QEMU guest 已运行生产 core/buffer/Cyton 模块与合成内核设备，覆盖配置查询、传统与逐帧接口、只读 mmap、SDK、解绑及模块卸载。三个架构完整内核与 modules 均已编译；诊断 BusyBox 由此前对应架构静态构建复用，并不宣称重新编译 BusyBox。

7.3.1 主线不提供旧 RT 补丁的 `/sys/kernel/realtime`。本次 guest 除读取运行中 `/proc/config.gz` 外，编译并加载只读测试模块，调用 `preempt_model_rt()` 验证实际内核 RT 模型为 1。测试模块仅存在于诊断 initramfs，不成为产品设备或实时性能证明。缺旧 sysfs 节点不等于 RT 关闭；显式节点值 0、配置关闭或证据不足均不得通过。

## 已关闭的本地交付门

- 内核 API 移植、源码准入、SBOM、跨架构缓存配置、原生诊断与启动检查均通过独立 SPEC/QUALITY；冻结候选完整维护 133 组、1656 次方法执行，无失败、错误或跳过，AI11 已包含。
- 准确源码提交已私有推送，远端读回一致；另有干净提交 AI11 验证。内核与 BusyBox 复用提交前同候选内容的实际编译产物，相关源码/配置/模块摘要完整绑定；Live SDK 与原生工具重新编译。
- candidate-62 ISO SHA256：`96df3627bf79364192d502453904f0d78f112d3ba83a934bc28c0084a3b0c53f`。三个嵌入载荷完整读回、BIOS/UEFI × CD/USB 四模式与 x86 正常系统启动通过。ARM64/RISC-V 本轮证据为真实内核合成设备 guest，不表示正常整机或实板均已验收。
- 父验收索引 `.build/kernel-7.3.1/acceptance-index.json`，状态 `ACCEPTED_LOCAL_KERNEL_UPGRADE_AND_QEMU_LIVE_IMAGE`；独立收尾报告与最终 `acceptance-readback.json` 均已保留。磁盘上的最终 ISO 在 `.build/kernel-7.3.1/retained-candidate-62-kernel731/images/`。

## 仍开放的功能与外部验收门

- 厂商网络线协议及认证、XR 图形会话和实际应用控制、领域脑电模型效果不在内核升级范围；后续实现按新候选验证。
- 真实目标板、物理串口/BLE、GPU 推理与 RT 共存、真实头显/EEG/EMG、同步校准及持续物理实时负载未验收。
- 当前 i915 与上游 PREEMPT_RT 约束不兼容，Xe 不覆盖所有旧 Intel 核显；不宣称兼容范围不变。
- Secure Boot、物理磁盘部署和完整三架构发布包/公开镜像发布均未由该 ISO 验收关闭。

本次不升级或重启开发主机内核，不写物理磁盘，不改 BIOS/IRQ/电源策略，不公开发布。`XTOS_SCHED` 仍为辅助队列，不是 Linux sched_class；M0 实机收益门未过，不借升级实现或宣称专用调度器。

## 原始证据

忽略目录 `.build/kernel-7.3.1/` 保存官方下载/验签、上游精确 patch replay、真实 Kbuild RED/GREEN、完整构建日志和每次独立 guest 的原始输出。早期 initramfs 缺解释器、禁用 BusyBox --install，以及旧 RT 节点假设造成的失败全部保留，以后续新 attempt 补证，不改写历史。
