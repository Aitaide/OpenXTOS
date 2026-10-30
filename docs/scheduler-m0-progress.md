# XTOS 调度器 M0 开发记录

状态：此前 M0 源码工具与文档候选曾通过 45 组、540 项（含 AI11）维护测试，无跳过、无失败，证据为 `.build/scheduler-m0/m0-final-accepted-attempt-2f3afac2/report.json`，仅绑定当时版本。本轮 M1 用户态参考工具、脚本和文档另行冻结验收，不继承旧全量结论；范围以 `.build/scheduler-m1/candidate.json`、`.build/scheduler-m1/acceptance.json` 为准。M0 目标裸机和 M1 阶段收益门仍未完成；M2 内核集成未启动。

## 本轮实现

- `docs/scheduler-class-baseline.json` 固定私有开发 HEAD、公开 v1.0.1 对应源码、12 份 Linux/RT/XTOS 源文件及补丁哈希。未改 kernel 或 UAPI。
- 三份旧调度设计/实现/测试报告增加历史状态提示，保留原始记录，不再视为当前主调度器验收。
- `tools/xtos-sched-bench/` 提供 C 周期 CPU workload、原生 policy 设置与读回、竞争进程控制、原始 JSONL、旁车 manifest 及严格分析器。
- 默认绑定实验不放宽 CPU mask 或 policy；DEADLINE 非绑定诊断必须显式选择，结果标记为 diagnostic/noncomparable。
- trace 创建与分析输出均不覆盖既有证据；完整 job 契约、warmup、计时语义、实际 CPU/policy、运行时参数和原始/源码/二进制哈希分别核验。
- runner 验证竞争进程的 readiness、SCHED_OTHER policy、同 CPU affinity、存活及实际 CPU 时间进展，处理超时与退出清理。
- 四个测试文件接入 `scripts/test.sh`，M0 专项合计 42 项通过：文档/基线 3、C 执行 7、runner 14、分析器 18。

## 冻结版本与实际执行

最终冻结源码记录：`.build/scheduler-m0/candidate-frozen.json`，revision `quality-fixes-startup-ownership-and-trace-overlap`。各次结果均绑定相应源码和产物哈希，旧 attempt 不代表新候选验收。

最终 runner 功能验证：`.build/scheduler-m0/runner-final2-d41493ab/index.json`。

- OTHER、FIFO、RR 单 CPU 绑定采集通过，竞争进程实际运行并有 CPU 时间进展。
- 默认 DEADLINE 单 CPU 绑定被 root-domain 准入规则拒绝，未创建成功 trace/manifest，不伪装为性能样本。
- 显式 DEADLINE 继承 CPU mask 的独立诊断通过，不与绑定组作性能排名。
- 每个成功短测 100 jobs，其中前 10 jobs 为 warmup；全部原始数据保留，摘要统计 90 jobs。
- 原始 trace、旁车 manifest 和单独分析输出哈希/摘要一致。
- C 可执行程序另通过 ASan/UBSan 编译与 OTHER 运行验证，证据在 `.build/scheduler-m0/parent-evidence/`。

独立规格审查先发现缺少 `wake_semantics` 校验；已按 RED→GREEN 补齐。随后质量复审发现并修复启动阶段子进程所有权竞态和相邻 job 区间重叠验收缺口。最终质量复审 `.build/scheduler-m0/quality-rereview/report.json` 为 PASS，20 个冻结路径哈希在复审前后均一致。

## 完整维护测试：保留成功和失败

1. 未修改 HEAD 的隔离导出首次维护测试，在既有 EEG→AI 推理超时用例失败：`.build/scheduler-m0/suite-baseline-fb3f81fd/report.json`。
2. 中间候选维护测试结果保留在 `.build/scheduler-m0/maintained-candidate/report.json` 和 `.build/scheduler-m0/maintained-final/report.json`，分别反映旧候选通过及缺陷修复过程中的历史失败，不作为最终候选结论。
3. 最终冻结候选完整维护测试：`.build/scheduler-m0/maintained-final2/report.json`，44 个 unittest 组共 513 项通过，AI 严格测试 11 项、跳过 0，退出码 0，最终 `user_space_tests=pass`。同一轮已核验冻结源码哈希不变。
4. 早期 AI 服务恢复超时的基线对照仍保留于 `.build/scheduler-m0/ai-flake-comparison/report.json`；最终候选维护测试已通过，因此该历史失败不再作为当前候选的阻断项。

## 环境与结论边界

开发宿主为 KVM 上 Linux 6.12.107 PREEMPT_DYNAMIC，并非物理目标机上的 XTOS 6.6.0-rt15 PREEMPT_RT。仓库另保留 2026-10-10 H3C R4700 G3 的历史物理 RT 观察和有限 cyclictest 记录，但它不属于当前 preflight 候选、没有真实周期任务契约，也没有 FIFO/RR/DEADLINE 同条件对照；当前目标机不在本轮运行窗口内。相同 DEADLINE 参数保留完整允许 CPU mask 时可设置；单 CPU affinity 后设置返回 EPERM，先设置再缩小 affinity 返回 EBUSY。Linux DEADLINE 带宽准入以 root domain 为单位，此行为不是 Linux 调度性能差的证据，也不能简单归因于缺少 root 或 chrt。

本轮没有改变 cgroup、sysctl、IRQ、watchdog、BIOS 或其他宿主全局配置。`wake_ns` 只是绝对睡眠返回后的第一次用户态计时，不是内核 runnable/wakeup 事件；该工具不测纯调度开销，也不证明物理设备采集闭环。

## 不得越过的阶段门槛

目前没有批准的真实 workload、量化收益门槛及目标裸机 PREEMPT_RT 的同条件 FIFO/RR/DEADLINE 重复对比，无法决定定制调度类是否必要。用户要求自主继续开发后，先行开展 M1 用户态参考工具；这不批准阶段收益门，不替换 Linux 调度器，不宣称优于 Linux。M2 内核集成未启动。

下一阶段仍需要明确真实任务集、负责人、截止时间/预算依据和目标物理测试机，再按计划进行 Linux 原生策略基线。M1 模型证据见 `docs/scheduler-workloads/m1-model-v1.md` 和 `.build/scheduler-m1/acceptance.json`。历史 AI 超时记录继续保留，不因重跑通过而宣称根因已修复。

## 只读主机预检扩展

新增 `tools/xtos-sched-bench/preflight.py` 与 `tests/test_sched_preflight.py`，采集主机事实而不做配置调整。使用说明见 `docs/scheduler-workloads/target-preflight.md`。已修正 DMI 机型误判裸机、FIFO 阻塞、fixture 与 live 信息混用及 cgroup bind mount 根路径映射，并保留对应回归证据。

父级实测报告为 live_host，检测到 hypervisor，RT sysfs 不可用；只能说明采集器运行正常，不符合裸机 RT 验收前提。扩展候选冻结清单为 `.build/scheduler-m0/preflight-parent/candidate.json`，包含六个新增或修改路径，不覆盖上一轮证据。预检测试已接入 `scripts/test.sh`。

父级相关测试的一次运行在未改动的 runner 测试遇到 `/proc/<pid>/stat` 消失时的 ProcessLookupError，随后复跑通过。日志分别保留为 `preflight-parent/focused.log` 与 `preflight-parent/focused-retry.log`，不能把重跑通过当作该竞态根因已修复。IRQ 目录替换修复后的 65 项相关测试见 `preflight-parent/focused-irq-fix.log`。

独立规格审查曾复现 IRQ 目录验证后被替换为外部链接、泄漏目录名的 SPEC-1；已通过基于目录 FD 的 O_NOFOLLOW 打开与枚举修复。`preflight-spec-rereview/report.json` 确认该问题已关闭、23 项聚焦测试通过、六路径哈希在前后匹配。旧全量执行虽成功，但结束时源码已变化，`preflight-maintained/report.json` 保留为历史结果。

修复版一次完整维护执行了 536 项 unittest（包含 AI 严格测试 11 项），无测试行为失败/跳过；但父级同时更新了本进度文档，AI 的全工作树来源检查拒绝 `source changed during run`。该轮退出码 1，不能当作全量通过，证据保留于 `preflight-maintained-final-4b6ba118/`。后续必须冻结全部维护文件（包括进度文档），只允许写 ignored 证据目录，再做完整维护验证。最终质量与全量结论以台账为准。

质量审查在 `irq-directory-fd-fix` 版本复现 QUALITY-1：fixture 根目录祖先被替换后，可重定向根路径。当前修复版本为 `root-identity-and-ancestor-nofollow-fix`，根路径从 `/` 逐级以 O_NOFOLLOW 打开，并核对构造时记录的设备号/inode；祖先软链接替换、目录替换及后续根身份变化均拒绝，25 项预检测试通过。旧全量进程已为该源码修复主动停止，记录 `preflight-maintained-frozen-06d7949b/cancelled.json`。

当前修复版独立规格与质量复审分别记录在 `preflight-root-spec/report.json` 和 `preflight-quality-rereview/report.json`，均为 PASS，六个候选路径哈希首尾一致。质量复审另执行 11 项补充探针及三次 live 收集，原始越界被拒绝，未见 FD 泄漏。父级实际收集证据为 `preflight-parent/host-root-fix.json`。最终全工作树维护测试由 `preflight-parent/run_final_suite.py` 执行，attempt 路径与结论写入 ignored 台账；本文件在运行前冻结，运行期间不更新。

本轮又补齐了 M0 闭环清单 `docs/scheduler-workloads/m0-closure.md`、目标输入模板 `m0-target-intake.json`，并把旧阶段 4A 的延迟数字明确降级为历史计划值；文档守护测试新增对这些边界的检查。上述文档和测试变化使上一份全量报告成为历史证据，必须在本文件冻结后重新运行维护套件。重新运行只能关闭“源码工具/文档”子门；物理目标机、真实任务和原生策略对照仍须外部证据。

## 用户态参考工具后续开发

按用户自主继续开发的要求，新增 `tools/xtos-sched-model/`，提供整数时间的 EDF/FIFO/RR 及任务共享固定周期预算模型、有限 DBF 诊断和 M0 pinned trace CPU 需求导入。契约与局限见 `docs/scheduler-workloads/m1-model-v1.md`。初期 19 项测试未覆盖 RR 持续优先级、积压 job 共享预算及有限 DBF 不得准入等问题；这些初期报告保留为 superseded，不作为当前验收。最终以当前候选、独立 oracle/SPEC/QUALITY 和完整维护结果为准。

初期候选完整维护在 AI 恢复用例失败：`.build/scheduler-m1/accepted-attempt-86369663/report.json`，29 组 356 项、exit 1、全树未变。未修改 HEAD/current 的 14 份 AI 来源字节对照和复测也出现同一失败，证据在 `.build/scheduler-m1/ai-recovery-comparison-20261025T090043Z-2845150/report.json`。随后只读计时探针 `.build/scheduler-m1/ai-recovery-timing.jsonl` 观察到：250ms 请求先耗尽于真实信任检查，尚未进入 worker.infer，SIGSTOP 旧进程仍存活；旧测试把 alive/not-recovering 误读为恢复完成。

仅修测试装置：在实际进入 worker.infer 后注入 SIGSTOP，保留真实 250ms worker 超时与 kill/reap，增加旧 PID 回收、新 PID 就绪断言；恢复后推理仍要求 3000ms 内成功。生产 AI 源码与 deadline 处理未改。该触发修复的真实四项 focused 测试和独立复测均通过，审查报告为 `.build/scheduler-m1/ai-trigger-review/report.json`。不据此宣称生产 AI 时延上界或通用根因均已修复。

独立 oracle 初版只记录运行中 job 自身耗尽预算，漏掉“成为队首时共享预算已由前一 job 耗尽”的等待；按既定“曾被节流”契约补充 oracle 回归后，192 个策略案例的 CPU 占用、job 时间和统计全部一致。独立质量审查另发现 DBF distinct horizon 多一项、trace 未知字段被忽略、原始时间戳经归零绕过 int64 三处缺陷；均有保留的失败回归，修复后模型/CLI/oracle 47 项通过。最终候选独立增量审查及完整维护结论仍以 `.build/scheduler-m1/candidate.json`、`.build/scheduler-m1/acceptance.json` 和其绑定的原始报告为准；本文件冻结后不以新文字覆盖当前候选的验收来源。

分区 CLI 的不可编码 Unicode 回归现已覆盖：UTF-8 编码在创建输出文件前完成；该回归包含在当前 67 项聚焦测试中。

## 固定 CPU 分区参考工具

后续新增 `partitioned.py` 和独立输入/报告 schema，支持固定 CPU 的多任务逻辑重放、整批按 CPU 预算准入、各 CPU 有限 DBF 诊断及准确求和的统计。所有 CPU 共享每个 policy 的 4096 jobs/100000 events 上限，启动任何重放前验证整批任务与 work profile。没有迁移、共享锁或跨核干扰模型，不作为 Linux SMP 或硬件性能证据。契约与示例见 `docs/scheduler-workloads/partitioned-model-v1.md`、`partitioned-model-input-v1.json`。

分区扩展使上一轮 587 项完整维护通过记录成为历史候选。当前 focused 为 67 项模型/CLI/oracle 与 6 项文档检查；384 条分区逐 CPU 策略时间线对照独立 unit-tick oracle，原 192 个单 CPU 案例仍保留。本轮候选、独立审查和完整维护以 `.build/scheduler-partitioned/candidate.json`、`.build/scheduler-partitioned/acceptance.json` 为准。M0 实机与 M1 真实任务收益门仍未关闭，M2 内核调度类未开始。

本轮缺口闭环新增：`tools/xtos-sched-bench/m0_gate.py` 严格校验原生策略证据；`docs/xtos_scheduler_class_rfc.md` 与 `docs/xtos_scheduler_lifecycle_matrix.md` 记录 M2 前的 sched_class 审查边界；`tools/xtos-eeg-pipeline/device_contract.py` 和 `docs/eeg_device_validation_contract-v1.md` 固定真实设备、断连恢复、丢包和时间戳契约。当前新增工具均为用户态验证器，不把 fixture、QEMU 或当前 KVM 结果升级为实机/医疗/硬实时结论。

## 当前缺口闭环候选

本轮独立审查初次 FAIL 保存在 `.build/scheduler-gap-closure/review-v1/report.json`：M0 的重复运行、缺失指纹、raw/manifest 参数绑定、必填输入与产物四项，EEG 的重复/倒退序号、累计丢包账目、生命周期证据三项。原失败记录保留。修复后 M0 32 项、EEG 18 项、readiness 4 项、基于实际内核 27 个回调的覆盖测试 3 项和文档守护 6 项通过；这些聚焦结果尚不等于完整维护或实机验收。

当前候选、独立复审、完整维护和旧构建产物恢复均绑定 `.build/scheduler-gap-closure/candidate.json`、`.build/scheduler-gap-closure/acceptance-ledger.json` 与对应 maintained attempt。此前 587/608 项报告仍是旧候选历史结果，不能覆盖本轮修改。

M0 只验证证据结构和跨产物一致性，最多输出 ready_for_independent_review；不自动评估 WCET 依据、统计门槛、物理身份或批准 M2。EEG 最多输出 live_device_report_consistent，physical_device_evidence_verified 固定 false。G3 的真实设备 adapter、G4 的真实 workload/物理目标机基线、G5 的内核类及 G6 发布闭环均未因此完成。
