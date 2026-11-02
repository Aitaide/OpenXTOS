# XR headless 眼动会话与 Live 集成 v2

状态：native、CLI 与 Live 助手各自冻结版本的组件 SPEC/QUALITY 已通过，当前组合依赖与共享安装接线仍待独立集成审查，未通过整机验收。此前 loader/事件/模拟确认切片的历史验收不授予本轮新增源码；v1 合同保留其原始切片范围。

## 本轮软件功能

- 在可信启动配置中显式指定绝对 loader 路径；只有明确请求时才启用 `XR_MND_headless`、`XR_EXT_eye_gaze_interaction` 及 `XR_KHR_convert_timespec_time`。所有必需扩展必须在创建 instance 前确认；缺项返回 unavailable，而不是创建没有所需能力的成功实例。
- 查询 HMD 系统和眼动支持，调用真实 OpenXR session、action set、POSE_INPUT action、官方 gaze binding、LOCAL reference space 和 gaze action space 接口，attach/sync/query/locate 均使用固定 Khronos ABI。
- headless 不包含 graphics binding、swapchain、渲染或显示。在不支持该扩展的头显上，本路径必须拒绝；不能把 headless 当作全设备通用图形实现。
- 会话状态控制 begin/end；失焦、丢失、事件缺口或参照系/交互配置变化使待用样本失效。所有权必须覆盖部分初始化、销毁失败及重试；尚有子对象时不得卸载 instance 或共享库。

## 时间和质量

`XrEyeGazeSampleTimeEXT.time` 是 runtime 表示该 pose 的时间，可以是 clamped、predicted 或 interpolated，并不必然是原始传感器采集时刻。输出必须保留 query host/XrTime 与 runtime pose XrTime/转换 host time，不能把它们合并成“真实采集时间”。逐点调用时间转换扩展，不缓存永久 offset。时间转换成功也不证明物理同步不确定度或时延已校准。

仅当 action active、位置/姿态全部 VALID/TRACKED、pose 有限且四元数有效时提供数据。缺少有效 pose time 或转换失败必须拒绝。没有额外来源证据时，不能声称 `predicted=false`，也不得把 runtime_unverified 数据直接送入需要“非预测且可信物理输入”的动作门。眼动记录属于敏感数据，启动采样命令必须明确取得用户同意；默认不连接设备、不写全局 XR 配置。

## 有界采样命令

源码安装器已包含 `xr-eye-probe`：显式 `--loader /absolute/path`、`--accept-eye-data`，有界样本数和持续时间，缺 loader/扩展/支持返回非零 unavailable；使用周期休眠而不是忙循环。模拟选项只是调用方标签，不认证动态库或设备来源。命令在报告成功前完成清理，输出错误和未完成清理必须非零。非阻塞 stdout 与一次尽力 stderr 写入约束管道背压；任意第三方 runtime 函数内部阻塞不由用户态时间预算保证，另列运行风险。

所有输出固定 `safe_action=no_action`、`physical_acceptance=false`。没有控制执行器或自动授权；眼动数据的格式和运行成功不能证明权限、身份、跟踪精度或医疗安全。

## Live 交付

`install_xr.py` 的独立源绑定安装器仍是基础。新增 `live_xr.py` 将安装与目标 rootfs 执行闭环连接：校验精确源清单、Python 源映射、payload hash/mode、launcher 解释器/argv，实际在目标根运行 CPython、native 缺 loader 拒绝路径和 harmless demo。只在本操作独占的新 staging 中执行，不初始化生产 authority。

`build_live.py` 在 SquashFS 打包前强制该运行门并写入新 manifest；`verify_live.py` 在新 ISO 内以普通 xtos 用户执行目标 smoke，核对真实退出码、无动作/非物理标记、无 authority 残留。源码测试、chroot、ISO 安装库存、guest 执行和四模式启动是不同门，不能互相替代。

## 验证与尚未关闭项

组件证据位于 `.build/remaining-closure-v2/`，台账为 `acceptance-ledger.json`。native 四项 SPEC 修复在 canonical 上新跑 12/4/16 项，并按精确输入绑定复用非恢复 ASan/UBSan 12/4/16。CLI 组件 QUALITY 在旧 native 冻结依赖上完成；父流程另在修复后的依赖上完成普通、优化及非恢复 ASan/UBSan 各 19 项，不能由此自行授予组合独立审查 PASS。Live 助手组件的实际 chroot 与 12 行库存读回保持其冻结输入范围，不等于新 ISO 或普通用户 VM 运行。

新增行为先保存 RED 的源码、测试、依赖与命令，再实现并保留 GREEN。既有实现中缺失 API 编译 RED、立即通过的附加回归及最终测试与 RED 快照的字节差异按原事实保留，历史严格 TDD 仍为 PARTIAL。native OBS-001：成功清理可能覆盖初始化的主 XR 错误诊断，API 失败返回仍保持非零；该观察未在四项修复中关闭。当前组合独立 SPEC/QUALITY、完整维护和精确干净提交产物验收由父流程继续完成。

图形会话/渲染、真实 runtime/头显、物理 EEG/EMG 联调、可靠设备身份、同步校准、跟踪精度、真实端到端时延、GPU 和持续物理 RT 保持独立未完成项。需要具体型号、runtime/图形绑定、板卡与维护窗口；不以模拟 ABI 结果关闭。

资料：Khronos OpenXR-Docs release-1.1.57 的 `XR_MND_headless`（revision 3）、`XR_EXT_eye_gaze_interaction`（revision 2），与仓库固定头文件对应。
