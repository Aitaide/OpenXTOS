# XTOS P0→P2 AI/SI 平台实施计划

> **For Hermes:** 先执行 P0，完成并核验一个阶段后再进入下一阶段；每个代码任务遵守 RED→GREEN→回归，物理硬件任务不能用 QEMU 证据替代。

目标：把当前 XTOS 从“可验证的实时 Linux + CPU AI 基线”逐步推进为具有可复现发布证据、统一推理运行时、安全资源边界、真实数据闭环和可审计运维能力的 AI 承载系统；不把操作系统本身宣称为已经具备 SI 能力。

架构边界：P0 先解决当前提交、AI 证据、候选镜像和 CI 的 provenance 问题。P1 建立模型服务/数据闭环/隔离执行控制面，保持 RT 核与 AI 租户分离。P2 再接入实卡加速、多架构 runtime、持久化/回滚/审计和真实硬件。训练框架、向量数据库、Agent 编排不进入实时内核，作为受控用户态应用或第三方 xpkg。

基线：HEAD `5f5b88b3b26677bf99d2107bdc8c2fce9a2484d8`，分支 `main`，远端 `origin/main` 与 HEAD 一致；现有 candidate-44 使用旧源码 `633457100`，不可作为当前候选证据。

## P0：可复现、可验收、可发布

### P0.1 当前 HEAD 严格 AI 证据

文件：
- `scripts/test_ai.py`
- `scripts/ai-python-qemu.sh`
- `tools/xtos-onnx-baseline/requirements-ai.lock`
- `.github/workflows/ci.yml`
- 新增测试：`tests/test_ai_provenance.py`

行为：
- 复制当前 HEAD 到干净临时 worktree，使用锁定依赖环境运行严格 AI 测试。
- `results.json` 必须记录准确 `source_commit=HEAD`、`source_state=clean`、依赖锁 SHA、模型 SHA、Python/包版本、测试套件和结果。
- 报告中的 source commit、模型哈希和依赖锁哈希由脚本实时计算，禁止硬编码。
- 11 项 AI 测试必须运行，`skipped=[]`、`failures=[]`、`errors=[]`。

RED：先在测试中伪造 dirty/旧 source commit/缺少 lock hash，确认验证器拒绝。
GREEN：实现 provenance 字段和校验。
验证：
```sh
python3 tests/test_ai_provenance.py -v
XTOS_AI_PYTHON=scripts/ai-python-qemu.sh bash scripts/test.sh
```

### P0.2 CI 严格门禁

文件：`.github/workflows/ci.yml`，必要时新增 `scripts/ci_ai_acceptance.sh`。

行为：
- CI 不把普通可选依赖路径当 AI 验收路径。
- 新增 isolated AI job：按 lock 安装、执行 `scripts/test_ai.py`、校验报告 provenance、校验 `git status` clean。
- CI 只接受当前 checkout SHA，缺失套件、任何 skip、失败、错误、锁文件变化或报告 SHA 漂移都失败。
- 普通 userspace job 可保留无 AI 依赖的 degradation 测试，但报告中明确 optional skip，不得成为发布绿灯。

验证：
```sh
python3 -m py_compile scripts/test_ai.py scripts/verify_ai_acceptance.py tests/test_ai_provenance.py
python3 tests/test_ci_ai.py -v
```

### P0.3 当前 HEAD Live 候选

输入必须从当前 HEAD 干净快照解析，不复用 candidate-44 的源码或证据。输出使用新的 `.build/live/candidate-p0-*` 和 `.build/live/evidence-p0-*`。

门禁：
- build manifest `source_git_head == HEAD`、`source_state == clean`。
- ISO、kernel、rootfs、initramfs 哈希全部读取并交叉验证。
- `scripts/verify_live.py` BIOS/UEFI × CD/USB 四模式通过。
- 证据绑定 ISO SHA；ready、SSH、公钥-only、内部盘 sentinel、模块和 userspace selftest 全通过。
- Secure Boot、实体 GPU、实体 BCI、最坏延迟仍单独列为 unverified。

### P0 完成定义

P0 只有在以下全部满足后完成：
- 当前 HEAD 严格 AI 11/11，0 skip。
- CI 配置包含严格 AI 门禁。
- 当前 HEAD 新候选 manifest 与证据 SHA 一致。
- 四模式 QEMU 通过。
- `git diff --check`、SBOM freshness、完整 `scripts/test.sh` 通过。

## P1：统一推理与安全实时承载

### P1.1 推理服务契约

建议新目录：`tools/xtos-ai-runtime/`。

先写测试和 JSON schema，再实现：
- `model list/load/unload/reload/health`
- 模型名称、版本、架构、输入输出 schema、模型 SHA-256、provider、线程数
- 单请求/批处理上限、队列深度、背压、取消和超时
- 错误输出、NaN/Inf、shape/type 不匹配全部 `no_action`
- 模型切换采用 staged load + health check + atomic active pointer
- 旧模型在新模型未通过校验前继续服务

实现可以先用 Unix domain socket，不能把 HTTP 暴露到默认网络面。每个 worker 可被杀死，调用方在 deadline 内得到安全结果。

### P1.2 真实数据闭环

现有能力与缺口：
- `tools/xtos-eeg-pipeline/pipeline.c` 只接受 `xtos-eeg-v1` 的 CSV：2 通道、250 Hz、nanovolt；内部可构造含时间戳/序号的帧并带通，但质量 flags 当前为 0。采样间隔异常会标 degraded，却仍可能继续输出帧，不能直接当质量门禁。
- `tools/xtos-eeg-replay/replay.py` 是文件回放校验，不是实时采集；现有 LSL outlet/inlet 回环为合成 1 通道、100 Hz 数据，与 pipeline 契约不同，不能标作 EEG。
- `sdk/libxtos-bci/src/session.c` 把 int32 原始值除以 1000 转为 float；在核实驱动原始物理单位、样本顺序、通道/时间戳来源之前，不把它接到 nV pipeline，也不从 FIFO fixture 推断真实设备协议。
- `tools/xtos-onnx-baseline` 固定 ONNX fixture 要求 `[1,4]` 输入，现无经过验证的 EEG 特征映射；不得把测试模型的成功分类写成真实 EEG 识别。

首个软件纵切先用清楚标记的 fixture / 本机 LSL 回环验证传输和安全边界，绝不宣称真实采集：
1. 定义帧契约：`timestamp_ns`、单调 `sequence`、`channel_count`、`sample_rate_hz`、`unit`、`quality_flags`、`source_kind`。fixture 使用 `source_kind=fixture`；LSL 必须验证 stream channel count/rate/unit 元数据，不匹配则拒绝，不做猜测换算。
2. RED 测试覆盖缺字段/错单位、乱序/丢帧、间隔超差、低质量和设备断连；GREEN 质量门禁对无效帧 fail-closed，统计计数保留拒绝原因，禁止送入推理。
3. 先打通 fixture → 带通预处理 → P1.1 Unix-socket client 的无执行 smoke。直到 `[1,4]` 特征映射经模型契约定义并有确定性测试，fixture 推理结果都强制标记 `no_action`；不把 `action_allowed` 当作 BCI 决策。
4. 传输层增加有限队列、背压、请求 ID/序列关联、deadline/cancel；模型不可用、超时、损坏输出、响应错配均返回 `no_action`。
5. LSL/CSV 软件回环通过后，再单独实现 USB/BCI 输入适配器；必须先由真实设备协议证据确认采样格式、单位和时间戳来源。

每个纵切最多触碰 3 个 pipeline 代码/构建文件，先写红测，再最小实现；不得改 P1.1 服务协议文件。计划测试优先扩展 `tools/xtos-eeg-pipeline/test_pipeline.py` 并用 `make -C tools/xtos-eeg-pipeline test` 验证。

物理验收：当前控制机未发现 `/dev/ttyUSB*`、`/dev/ttyACM*`、`/dev/video*` 或 `/dev/serial/by-id/*`，且 `/sys/class/video4linux` 不存在。真实 OpenBCI、USB camera 的普通用户权限、通道/单位/采样率、帧丢失、热拔插恢复和端到端延迟均保持未验证；LSL localhost 回环及 fixture 不得冒充硬件闭环。

### P1.3 资源隔离与可取消执行

内核/工具：
- `tools/xtos-native/cgroup/cgroup.c`
- `system/live/xtos-start`
- live kernel config

先补验证测试，再决定启用 `MEMCG`/CPU bandwidth/IO controller 的配置成本：
- 建立 RT/AI cpuset，读回 `cpuset.cpus.effective` 和 `cpuset.cpus.partition`。
- 验证所有推理线程、子进程和 worker 不越界。
- 明确 IRQ、kworker、GPU workqueue 不自动随 cgroup 迁移，逐项读回。
- 推理在可杀死 worker 或隔离进程内运行；超时不依赖同步调用返回后才判断。

### P1.4 AI/Agent 安全执行

新增受控 runner，默认：
- 独立非特权 UID/GID
- `PR_SET_NO_NEW_PRIVS`
- seccomp allowlist
- mount/user/pid/network namespace（按需要最小启用）
- 文件根和可写目录 allowlist
- 出站网络默认拒绝，按模型/工具显式放行
- CPU、内存、进程数和打开文件限制
- 高风险工具调用需要人工确认

xpkg 内容签名与模型签名分开；模型 manifest 必须绑定模型哈希、格式、provider、最小 runtime 版本和许可证。

### P1 完成定义

至少一个代表性 ONNX 模型通过统一服务，实际使用请求 schema、模型注册、切换、背压、超时、崩溃恢复和资源隔离；真实/硬件输入链路至少完成一个设备闭环，其他设备明确未验证。

## P2：加速器、多架构、生产运维和硬件

### P2.1 实卡 GPU 与多架构

- 明确支持矩阵：GPU 型号、驱动、固件、Mesa/CUDA/ROCm/ORT provider、架构。
- 必须执行代表性模型，证明 provider 选中硬件而不是 llvmpipe/software fallback。
- 普通用户 device access、热插拔、GPU IRQ、GPU worker 和 RT workload 同时测量。
- ARM64/RISC-V 若宣称 AI 支持，必须有对应 lock、runtime、模型执行和 guest/board 证据；仅 kernel QEMU boot 不够。

### P2.2 持久化、更新回滚和密钥

- 模型/应用放在明确版本化持久卷，不与系统 overlay 混淆。
- 签名模型清单默认强制；密钥离线保管、轮换、撤销、过期策略可测试。
- `update apply` 必须验证签名索引/版本，而不是只信本地 download manifest。
- 增加 anti-rollback 版本计数器和 A/B 或等价事务更新；断电注入后能启动新或旧版本，持久数据不损坏。
- Secure Boot/TPM measured boot/module signature 作为物理发布门禁，不用 QEMU 结果替代。

### P2.3 可观测性与漏洞门禁

- 服务健康、队列长度、推理耗时、超时、拒绝、模型版本、provider、资源使用均输出结构化指标。
- 关键审计日志持久化或远程导出，带时间、主体、结果、模型/包哈希，具完整性保护。
- 每个镜像 SBOM 覆盖 kernel、patch、locked userspace、firmware、AI runtime、model metadata。
- CI 增加 CVE 扫描；严重漏洞阻断发布，例外要有责任人、理由和到期日。
- 所有支持架构执行严格 guest/module 测试，不能只做 basic boot。

### P2.4 真实硬件长稳

- OpenBCI：协议握手、真实采样、丢包、时钟、热拔插、恢复。
- Camera/GPU：真实设备采集、驱动重载、设备断连/重连、实际推理。
- RT：目标硬件 24h+，空载/AI/磁盘/网络/GPU/传感器组合负载，记录 BIOS、C-state、IRQ、NUMA、镜像哈希。
- 报告 worst-case 时延但不把有限观测写成数学保证；医疗安全另行认证。

## 总执行规则

每个子任务都按：
1. 先写一个最小失败测试；
2. 运行确认失败原因正确；
3. 写最小实现；
4. 运行 focused test 和完整回归；
5. 冻结源码后重建产物；
6. 读取回 exact artifact/evidence/ref；
7. 提交并推送前再核验工作树、SHA 和远端。

任何依赖实卡、真实传感器、Secure Boot、TPM、远端 LLM 或物理机的项目，在条件未提供前保持 pending，不用模拟结果宣称完成。
