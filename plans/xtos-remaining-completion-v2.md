# XTOS 剩余能力闭环计划 v2

> For Hermes: Use subagent-driven-development and test-driven-development; the parent owns shared integration, acceptance and GitHub publication.

Goal: 把剩余软件实现、安装与镜像交付补齐，并用真实目标设备独立验收物理能力；不把局部软件完成改写成全部完成。
Architecture: 在已验收 Linux 7.3.1 上增加应用层会话和受控运行依赖；保留 CPU runtime 锁、旧 UAPI 和普通 Linux 调度。每个软件增量按行为 RED→GREEN、独立 SPEC→QUALITY、完整维护、干净提交和新产物分门验收。
Tech Stack: C11、Python、固定 Khronos OpenXR 1.1.57 头文件、Linux PREEMPT_RT、PTY/真实 loopback、编译 ABI fixture、QEMU。

## 基线

- 已验收源码：`32a8bd3903eda24bd44d7e1a19dea4156014d062`。
- candidate-62 ISO SHA256：`96df3627bf79364192d502453904f0d78f112d3ba83a934bc28c0084a3b0c53f`。
- 该快照仅验收内核升级和本地 Live 镜像切片。旧产物及验收记录保留；本计划后续源码不继承其 PASS。
- 当前本机为 KVM，只有模拟显卡/USB tablet，没有可用于物理闭环的脑电板卡、GPU 加速节点或头显。历史 H3C 记录不提供当前运行窗口或新内核物理性能证据。

## 交付清单与退出条件

| 项目 | 已有能力 | 本轮必须补的工作 | 外部前置条件 |
|---|---|---|---|
| 网络设备 | 显式地址 TCP 字节读写和取消/期限 | 指定厂商协议解帧、会话认证、设备状态与恢复 | 厂商/型号/协议或 SDK 许可；未给定时不得宣称通用兼容 |
| XR 采样 | instance/loader/事件/逐点时钟转换 | 实际 session、eye action/space、样本质量与生命周期；先做显式 headless 扩展路径 | vendor runtime 支持的扩展与目标头显 |
| XR 图形 | 尚无图形会话和渲染 | 选择目标图形 API，绑定设备、swapchain、frame 循环与实际显示 | 具体 GPU/runtime/头显；headless 不替代本项 |
| 多模态应用反馈 | 对齐、失效撤销、单次确认、模拟回执 | 受限无危险应用反馈通路、权限与来源验证；合成输入仍不授予实际动作 | 明确应用接口和可信来源，危险执行器不在默认授权内 |
| Cyton/Ganglion | 固定协议、Linux 串口/BLE 软件会话 | 目标依赖包装、板卡持续采样、断连/拔插恢复、校准与身份核对 | 真实板卡/固件/参考仪器；不能把 PTY/GATT fixture 当实机 |
| GPU 推理 | 签名 CPU/CUDA/MIGraphX profile、隔离及有限恢复 | 兼容可选 runtime 包、真实 GPU infer、驱动与 RT 共存测量 | 实卡及匹配驱动/runtime；不能改 CPU 锁或伪造 wheel |
| EEG 模型 | 窗口特征与 CPU 推理通路 | 确定任务、合法数据、训练/独立评估、个体校准、完整闭环 | 任务/数据/板卡；固定算术模型不等于分类器 |
| RT/多架构 | 新内核构建与 QEMU 功能 | 目标机持续负载/尾延迟/截止期与过载恢复，ARM64/RISC-V 实板 | 当前目标机、维护窗口、workload 与预算 |
| 调度类 | 原生策略、参考模型、RFC/完整生命周期矩阵 | 完成 M0 原生策略至少十轮同条件对照与独立 go/no-go；只有批准后才推进 M2 | 真实任务、WCET/预算、收益和回归阈值；原生足够则明确不做定制 |
| 安全启动/发布 | 私有源码及本地 ISO | 目标 Secure Boot 链、恢复方案、三架构发布包装与下载读回 | 签名/enrollment 方案和操作授权；不得自行替换机器信任根 |
| 新镜像 | XR 独立安装器 | Live 构建集成、真实目标解释器/ELF 验证、普通用户执行、精确新 ISO 四模式 | 空间与正确依赖锁；旧 ISO 不能重新贴标签 |

## 第一增量：headless 眼动会话

Files:
- Modify: `tools/xtos-xr/include/xtos_xr/runtime_adapter.h`, `tools/xtos-xr/src/runtime_adapter.c`。
- Create: `tools/xtos-xr/include/xtos_xr/eye_session.h`, `tools/xtos-xr/src/eye_session.c`。
- Test: `tests/test_xr_eye_session.py`, `tools/xtos-xr/tests/native/fixture_eye_runtime.c`, `tools/xtos-xr/tests/native/test_eye_session.c`。

1. 先测试显式请求 `XR_MND_headless`、`XR_EXT_eye_gaze_interaction` 的实例扩展准入，缺项在创建前拒绝；保留旧 `xtos_xr_open` 行为。
2. 用固定 Khronos ABI 编译模拟共享库，分别测试系统能力、session/action/action space/reference space 所有权和逐处初始化失败。保留真实行为 RED，再实现最小 GREEN。
3. 测 READY/begin、STOPPING/end、失焦/丢失 fencing，以及资源销毁失败可见性；会话未释放时不得卸载父 instance/library。
4. 实际 sync/locate 调用严格保留 query time 与 `XrEyeGazeSampleTimeEXT.sampleTime`。只在 active、有效 tracked pose、有限数值和真实正采样时刻下输出；逐点转换采样时间，不冒称校准后的物理同步精度。
5. 普通和 ASan/UBSan 非恢复模式验证，再运行不改断言的旧 native 测试。新功能仍标无动作/非物理验收。图形会话保留独立门，不以 headless 关闭。

## 第二增量：Live XR 执行闭环

Files:
- Create: `scripts/live_xr.py`, `tests/test_live_xr.py`, `tests/fixtures/live_xr_guest_check.py`。
- Parent integration after freeze: `scripts/build_live.py`, `scripts/verify_live.py`, `scripts/install_xr.py`, `tools/xtos-xr/Makefile`, `scripts/test.sh`, `tools/xtos-sbom/gen_sbom.py` 及相关测试。

1. 先以冻结小型源码 export 写行为测试，不能在并行 native 文件写入时构建它们。
2. 验证安装后精确 inventory/hash/mode、目标真实 Python 执行 harmless demo、native 缺 loader 的显式 unavailable/no_action 路径；只用解释器 sentinel 不算真实运行。
3. 目标依赖缺失、文件被改、symlink/FIFO、权限冲突要在成功验收前拒绝。安装不创建生产 authority 或修改全局 runtime。
4. 两个实现冻结后父集成、新增测试进入维护、更新 SBOM、独立 SPEC 再 QUALITY。
5. 完整维护只在最终候选冻结后运行；构建新干净提交 ISO，完整载荷读回、已安装 workload、普通用户与四模式启动分别验收。

## 执行和报告

本轮证据目录 `.build/remaining-closure-v2/`；逐增量保留命令、真实退出码、源码/依赖/日志摘要。共享 checkout 仅父提交。工作过程中状态为开发中，聚焦测试通过不关闭整体交付。厂商资料、硬件、模型任务等无法自行取得的输入须明确列阻塞，但不停止其他可独立实现的软件工作。禁止伪造硬件/数据、制造历史 RED、放宽旧断言、公开发布或无授权重启/写物理盘。
