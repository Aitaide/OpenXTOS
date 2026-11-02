# XTOS 生产准备度与剩余开发

更新日期：2026-11-02

本文基于源码提交 `3ec7b643b78a253238f2d3331ae0c5c251ee5693`、candidate-63 本地软件镜像验收和定向只读研究编写。本文提出的措施和测试窗口是后续目标，不能当作已经完成的安全或稳定性验收。

## 当前结论

candidate-63 适合作为受控开发环境和 QEMU 功能基线。完整维护、干净提交 AI 验收、精确 ISO 载荷、四种 QEMU 启动模式和普通用户软件工作负载均已有机器可核验记录。镜像 SHA256 为 `af96f99c2a4663c101b116f42bb34fea76b0a08f35fca4578eb1c931dedad8c5`，源码提交为 `3ec7b643b78a253238f2d3331ae0c5c251ee5693`。

公网长期部署目前应判定为 NO-GO。原因不是 QEMU 功能失败，而是已经确认的安全闭环缺口和未完成的运行证据：当前防火墙工具没有把链挂到 Netfilter hook，更新 apply 不重新验证发布签名，Secure Boot 和启动信任链未启用，更新没有事务切换与自动回滚，且没有目标硬件长稳窗口。原始失败、修复和复核记录均保留在 `.build/remaining-closure-v2/`；本文件不改写它们。

可以这样理解当前保障等级：

- 开发和受控实验：有较好的功能和可复现性基线，可以使用；网络应由外部隔离设备保护，持久数据和更新应人工审查。
- 封闭实验室物理部署：有条件可行，但必须先按目标硬件补做 SSH、外部防火墙、掉电恢复和长稳测试。
- 公网或自动更新生产部署：当前不具备已验证的交付条件。建议先关闭 P0 安全和更新缺口，再做目标环境验收。

## 已存在的保护

- Live 构建区分 `root`、普通 `xtos` 用户和 `runner` 受限账户；sudoers 没有给 `xtos` 提权。
- 没有打包公钥时 SSH 保持关闭；启用时 Dropbear 使用公钥认证并关闭密码认证和转发。相关路径是 `scripts/build_live.py:60-81,397-400,453-460` 与 `system/live/xtos-ssh:8-32`。
- AI 信任代码具备签名、模型哈希、撤销和有限恢复预算的组件保护；AI worker 的恢复预算不能扩大为整机恢复保证。实际生产入口、服务 UID 和 worker 沙箱仍需单独验收。
- Live 根介质只读，RAM overlay 默认易失；持久卷探测、标记、fsck 超时和 RAM-only 降级已有代码路径。它们不能替代随机掉电一致性测试。
- 现有代码和测试已有 Landlock、seccomp、模型信任和故障恢复组件；必须确认真实服务启动路径确实使用这些组件。

这些保护说明的是代码路径和受控验收边界，不等于目标机器的完整运行时安全状态。

## 已确认的 P0 缺口

### 1. 防火墙没有形成有效默认拒绝策略

`tools/xtos-native/fw/fw.c:238-282` 创建的 input/output 链只有名称，没有 `NFTA_CHAIN_HOOK`、hook 类型、优先级或默认策略。`fw status` 只根据表是否存在输出 enabled。candidate-63 来宾实际记录为 `XTOS_FW_STATUS enabled=false`；执行 enable 后记录的策略仍是 `policy=accept`。`system/live/xtos-start:67-101` 还会启动网卡和 DHCP，但没有在网络服务前安装可信过滤策略。

应先明确网络模型，再实现原子安装：loopback、已建立连接、必要 DHCP/ICMP 和受限管理源地址单独放行，其余入站默认拒绝。启动失败时不得输出对外就绪状态。验收必须在隔离 guest 中读取实际 inet base chain 的 hook、priority、policy 和规则计数，并分别验证允许与拒绝连接；不使用主机网络代替测试。

### 2. update apply 丢失发布签名信任链

`tools/xtos-native/update/update.c:1039-1041` 在 download/check 路径验证索引签名，但 `apply` 从 `download-manifest.json` 读取版本和 artifact，见 `:1183-1215`，没有重新调用 `verify_index_sig`。它随后按这个本地清单计算哈希并整盘写入，见 `:1248-1257`。如果下载目录和清单在管理员授权 apply 前被替换，哈希只证明文件与攻击者替换的清单一致。

应保留原始签名索引和签名文件，apply 重新使用受信任公钥验证，并把发布版本、架构、通道、路径、大小和 SHA256 绑定在同一个签名声明内。校验应使用稳定文件描述符，避免校验后路径或符号链接替换。之后再补 A/B 或明确的救援/人工换介质模型，保证写入中断不会留下被标为成功的半成品。

## P1 安全和维护缺口

1. 启动信任链只有同介质 `rootfs.sha256`。它能发现介质内文件损坏，不能证明发布者；Secure Boot 在构建清单中是 `unverified/unsupported`。目标是 bootloader、kernel、initramfs、rootfs 的签名链和密钥轮换演练。
2. SSH 构建时把同一公钥写入 `root` 和 `xtos`，且 host key 放在 `/run`，每次重启会变化。生产应禁止 root 远程登录，使用实名普通管理账户、独立紧急密钥、管理网限制和持久受保护的 host key/证书。
3. 包安装允许在未提供发布者公钥时只做完整性校验。生产系统根必须强制签名、可信发布者和吊销策略；用户域的自带包必须与系统可信根隔离。
4. candidate-63 的实际内核配置尚未完成生产加固：`CONFIG_FORTIFY_SOURCE=n`、`CONFIG_HARDENED_USERCOPY=n`、`CONFIG_MODULE_SIG=n`、`CONFIG_SECURITY_LOCKDOWN_LSM=n`、`CONFIG_DM_VERITY=n`、`CONFIG_SECURITY_DMESG_RESTRICT=n`，`CONFIG_MEMCG=n`。这些值来自实际镜像 `kernel.config`，不是根据文档推断。
5. AI worker 当前可通过 Python `subprocess.Popen` 继承服务的 UID/GID 和环境；组件级 runner 具备 Landlock/seccomp，但所审 worker 启动路径没有自动接入。生产应使用专用非 root UID、清洁环境、NoNewPrivs、文件和网络隔离、资源上限，并读回实际 worker 的 UID、CapEff、seccomp 和文件系统边界。
6. Debian 锁定包仍需要持续安全更新流程。当前候选包含 Dropbear `2025.89-1~deb13u1`；Debian Security Tracker 将 trixie 该版本列在 CVE-2026-3706 的 vulnerable 项，unstable 修复记录为 `2026.90-1`。同一页面将其重要性列为 unimportant，并明确上游质疑 SSH 协议中的可利用性；本研究没有证明可利用的 SSH 攻击，也不能据此判定整机高危。这个案例说明仍需按发行版完整版本、补丁和实际调用路径持续分诊，不能用锁文件或上游版本号替代漏洞管理。本轮未完成整机、私有 AI 运行时和静态链接库的全面漏洞扫描，不能宣称零漏洞。

## 稳定性和可维护性缺口

### P0：先补更新、掉电和长稳

- 更新当前是整盘写入和范围读回校验，不是事务升级；没有 A/B、启动成功确认、失败计数或自动回滚。需要在隔离块设备中注入短写、零写、EIO、ENOSPC、同步失败、替换竞争和中断电源，证明系统只能进入完整旧版本、完整新版本或明确救援态。
- 持久化已有 marker、fsck 超时和 RAM-only 降级，但现有干净重启测试不能代表随机掉电。需要验证 `fsync` 前后、rename 前后、密钥补种、fsck 中断和挂载阶段的恢复；目标是已确认提交的数据逐字节恢复，未提交数据允许丢失但不得破坏其他已提交数据。
- 当前没有 candidate-63 目标硬件的 72 小时或 7 天连续运行证据。应先由负责人确定目标机器、镜像、RT 任务、允许丢帧/丢包、延迟分位数、恢复时间和温度/功耗条件，再执行 soak。建议 72 小时 smoke-soak、随后 7 天实际 EEG/AI/RT 负载；这些是 proposed 目标，不是当前结论。

### P1：服务、资源和故障取证

- PID1 已有 getty、串口服务和有公钥时的 SSH respawn；不能因此声称所有基础服务有统一健康闭环。syslog、klog、mdev、chronyd 是一次性启动，缺少统一就绪撤销、失败预算和资源泄漏验收。
- AI worker 有每代最多两次恢复预算，这是已有保护，不能扩张为整机恢复。还需验证真实安装态 worker 的 SIGKILL、主服务崩溃、并发撤销、OOM 和孤儿进程/FD。
- 内核配置虽然有 `CONFIG_WATCHDOG=y`，但本候选没有形成已验收的硬件 watchdog、喂狗、hang 判据、pstore 或 boot-loop 限制闭环。应在目标硬件和隔离 guest 分开测试应用 hang、PID1 异常、kernel panic 和连续启动失败，并保留前一 boot 的故障摘要。
- `CONFIG_MEMCG=n`。cgroup v2 挂载和 cpuset 不能替代内存控制器。需要先决定是否启用 MEMCG，再测试内存、PID、FD、tmpfs 字节/inode 和持久卷 85%/95%/100% 场景，确认管理通道和日志不会跟着业务一起耗尽。

### P2：跨架构和持续维护

ARM64、RISC-V 64 已有当前内核组件构建和合成 guest 证据，但 candidate-63 的正式镜像验收是 x86_64。三架构的完整 Live 用户态、更新、持久化、设备接入和目标板长稳不能由历史 QEMU 记录继承。正式支持某个架构前，应建立 source/config/build/normal boot/diagnostic guest/full Live/board/soak 的独立矩阵。

## 剩余功能开发顺序

### 第一阶段：安全和维护底座

1. 防火墙 base chain、默认策略、启动 fail-closed 和隔离 guest 网络验收。
2. 更新签名链、反回滚高水位、写盘事务/救援策略和故障注入。
3. 生产启动身份：root 远程登录关闭、管理接口限制、host key 持久化/轮换、包签名强制。
4. 安全内核配置、最小服务权限和 AI worker 实际沙箱。
5. 为 target hardware 确定 watchdog、pstore、远程日志和响应窗口。

### 第二阶段：选择一个真实业务纵向闭环

优先在已有 bounded TCP/TLS、Cyton/Ganglion 软件切片和签名 provider 上接入一个明确目标。必须先拿到厂商协议/SDK、设备身份和认证状态机、合法测试向量或可复核录制、EEG 任务和许可数据。没有这些输入，不应编造通用厂商会话或真实模型效果。

### 第三阶段：按实际产品选择 XR/GPU 或调度器

- 若优先 XR/GPU，先锁定头显、OpenXR runtime、图形 API、GPU/驱动、ORT/CPython ABI，再实现 graphics binding、swapchain 和 frame loop。headless ABI 模拟不覆盖这些功能。
- 若优先实时调度，先完成 M0 真实 workload、原生 FIFO/RR/DEADLINE 同条件基线和预注册收益/回归阈值。只有原生策略确实不足且负责人批准，才进入自定义 `sched_class`；当前 XTOS scheduler helper 不是 Linux scheduler class。

### 当前不应优先做的内容

真实执行器、内核诊断 API 和更多通用抽象都依赖具体产品目标。没有目标设备、协议、数据和责任人时，扩大代码表面积只会增加维护风险。

## 需要负责人先确定的三个输入

1. 真实功能闭环：设备/固件/接口、厂商协议或 SDK、EEG 任务、许可数据和模型目标。
2. XR/GPU 组合：头显型号、Linux runtime/扩展、图形 API、GPU/驱动/ORT/CPython ABI，或明确暂不做 XR/GPU。
3. 物理目标与实时目标：目标板/架构、维护和回退负责人、实际任务 period/deadline/budget、CPU/IRQ 放置、收益和回归阈值。

在这三个输入确定前，平台层的最优动作是完成防火墙、更新信任、启动身份和故障恢复底座，而不是继续增加未绑定设备的功能。

## 证据位置

- 本地镜像验收摘要：`docs/candidate-63-local-validation.md`
- 最终封存：`.build/remaining-closure-v2/local-software-image-seal.json`
- 最终验收：`.build/remaining-closure-v2/local-software-image-acceptance.json`
- 留存文件索引：`.build/remaining-closure-v2/local-software-image-evidence-index.json`
- 安全定向研究：`.build/remaining-closure-v2/next-development-research-v1/security-surface.json`
- 稳定性研究：`.build/remaining-closure-v2/next-development-research-v1/stability-gaps.json`
- 功能开发研究：`.build/remaining-closure-v2/next-development-research-v1/functional-backlog.json`
- 父级确认的两个安全缺口：`.build/remaining-closure-v2/next-development-research-v1/parent-confirmed-security-findings.json`

外部参考（2026-11-02 读取）：

- https://www.debian.org/security/faq
- https://security-tracker.debian.org/tracker/source-package/openssl
- https://security-tracker.debian.org/tracker/source-package/dropbear
- https://security-tracker.debian.org/tracker/CVE-2026-3706
- https://www.kernel.org/category/releases.html

Debian 会向旧版包回移安全修复，应以发行版完整版本和安全状态分诊。源码签名证明上游来源，不保证版本长期获得维护；当前 7.3.1 基线也需要明确补丁跟踪和升级节奏，不自动假设 LTS 身份。本研究未改变已验收内核。外部页面不能替代 XTOS 最终镜像的供应链扫描。Git SSH 访问和远端 ref 可用；本次环境没有 gh 或 GitHub API 凭据，无法核实 Actions 实际结果，不将 workflow 定义写成远端 CI 通过。
