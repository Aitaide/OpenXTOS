# candidate-63 本地软件镜像验收

状态：`ACCEPTED_LOCAL_SOFTWARE_IMAGE_ONLY`。验收日期：2026-11-02。

本记录只覆盖源码提交 `3ec7b643b78a253238f2d3331ae0c5c251ee5693` 与下列 x86_64 ISO。记录本说明或后续研究文档的新提交不会改变该镜像的源码身份，也不自动获得该镜像的验收结论。

## 产物身份

| 项目 | 实际值 |
| --- | --- |
| 候选 | candidate-63-xr-tls |
| 源码提交 | `3ec7b643b78a253238f2d3331ae0c5c251ee5693` |
| 构建前后源码状态 | clean，提交一致 |
| 内核 | `7.3.1-xtos-live.1`，主线 PREEMPT_RT |
| ISO | `xtos-developer-live-x86_64.iso` |
| ISO 字节数 | 1066926080 |
| ISO SHA256 | `af96f99c2a4663c101b116f42bb34fea76b0a08f35fca4578eb1c931dedad8c5` |
| 内核配置 SHA256 | `5d4732aed8de08d8b23676f4aaa18e9213ba2ea2795128ac576d5dbd96b82000` |

持久产物位于开发目录 `.build/remaining-closure-v2/retained-candidate-63-xr-tls/images/`。ISO、完整运行日志和原始审查记录保留在本机忽略目录，没有作为 Git 文件上传；本文件是仓库中的验收摘要。

## 已执行并通过的检查

1. 完整维护运行 145 组、1748 次测试方法执行，零失败、错误和跳过，其中已包含 AI 11 项。这是执行次数，不声称为互不重复的方法数。
2. 对上述干净提交另行执行严格 AI 11 项验收，通过源码边界和依赖检查。
3. 从干净提交构建新 ISO，读回实际 ISO 内的内核、initramfs 与完整 SquashFS，核对实际载荷与构建清单。
4. A4 实际 QEMU 启动覆盖 BIOS-CD、UEFI-CD、BIOS-USB、UEFI-USB。检查启动就绪、RAM overlay、只读启动介质、SDK、开发用户、SSH 仅公钥认证、XR 安装库存和哨兵磁盘未改写。
5. 在 BIOS-CD 来宾的普通 `xtos` 用户（UID1000）下，已安装 TLS 本机回环六项、来宾编译器构建的显式模拟 OpenXR ABI 与已安装眼动探针，在正常和 `-O` 模式均通过。错误哈希在目标模块导入前被拒绝。这些应用工作负载没有在另外三种模式重复执行。
6. 维护中的 `verify_live_acceptance.py` 对同一 ISO 与四模式证据执行成功；经独立审查和单字段增量审查的本地收尾校验器最终退出码为 0。
7. 父任务封存最终验收、封存记录与 334 项留存文件索引，逐项重新核验文件大小和 SHA256；台账只关闭本地镜像门，外部输入门保持开放。

SDK、Live 原生工具与 XR 探针从当前源码重新编译。内核、模块与静态 BusyBox 使用经过相关源码、配置和实际产物校验的未变输入复用，不表述为本轮重新编译全部系统组件。

## 最终证据绑定

路径均相对 `.build/remaining-closure-v2/`，原文件保持不变。

| 文件 | SHA256 |
| --- | --- |
| `local-software-image-acceptance.json` | `db3224cad742a9a70b1f49b1f1d44f049b42ed5f9ab01ab73348853a029feee6` |
| `local-software-image-seal.json` | `39b0ef8444323a977bf8823b5a1dbb4da905271a5b8e341ced379d7f7c4b779e` |
| `local-software-image-evidence-index.json` | `5f7f42fcb49fa2e35f5b4c45201afcfff8b0751c1ecfb3d68227bc94c1681783` |
| `local-software-image-final-readback.json` | `5b0ad20790ca1c02ddee1a2bd5ab96834d41f1cfd2749745c9e7d4205e009004` |
| `guest-matrix-a4-report.json` | `6a7017069dbdaceb9a788b1061e8bf1169cc84e0fef8ca9c3b9b6899c634482e` |
| `candidate63-live-acceptance-a4.json` | `d06c0ffa90c94a6b1172c28cf83225e7cbc6f6b1e03af12bbcf9e3bce35ccfbd` |

## 限制与后续工作

这是本地软件镜像和 QEMU 功能验收，不是安全认证或硬件长期稳定性保证。TLS 对端和 OpenXR ABI 为显式测试夹具，结果保持 `no_action`、`physical_acceptance=false`。厂商线协议与认证、真实 XR 图形会话与头显、物理 EEG/GPU、持续目标硬件实时负载、模型效果、M0 决策和 Secure Boot 均未由本验收关闭。

保留维护 A1 环境跳过、来宾 A1/A2 SSH/串口失败、串口清理原失败与复核中断、A3 测试目录权限失败，以及收尾器错误字段名的真实回归。原报告没有改写为成功。组合 SPEC 的语义结论通过，但保留 `PARTIAL_BUDGET_OVERRUN` 的过程状态；部分窄审查也超出请求时限。历史 TDD 和过程限制不能由最终 PASS 抹除。

当前安全、稳定性和剩余开发评估另见 `docs/production-readiness-and-next-development.md`。该评估提出的措施与测试目标不是已完成结果。
