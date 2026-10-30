# M0 调度基线闭环状态

状态：pre-closeout 工具候选已完成源码工具验收；当前收尾候选的完整维护结果以验收台账和 attempt 报告为准。M0 产品/实机 go/no-go 尚未完成，阻塞于目标机、真实工作负载与决策门槛输入。

## pre-closeout 候选已完成且有证据

- 只读目标主机预检工具和路径竞态修复：`tools/xtos-sched-bench/preflight.py`；pre-closeout 冻结 revision 为 `root-identity-and-ancestor-nofollow-fix`。
- pre-closeout 候选的 25 项 preflight 专项测试、67 项调度工具相关测试通过；SPEC 与 QUALITY 的针对性独立复审均 PASS。
- pre-closeout 候选的冻结开发工作树维护测试通过：45 组、538 项 unittest（含 AI 严格测试 11 项），0 跳过，exit 0，`user_space_tests=pass`；源快照运行前后及复核时一致。该结果只绑定保存的 pre-closeout 候选，不覆盖本轮新增收尾文档和测试。
- pre-closeout 证据索引：`.build/scheduler-m0/preflight-acceptance.json`；全量报告：`.build/scheduler-m0/preflight-final-accepted-attempt-e5c58bfb/report.json`。
- 收尾候选 `root-identity-and-ancestor-nofollow-fix+m0-closeout+hardware-history-scope` 曾通过 69 项聚焦及 45 组 540 项全量维护测试，报告为 `.build/scheduler-m0/m0-final-accepted-attempt-2f3afac2/report.json`。该结果仅绑定当时版本。本轮新增参考工具与文档变更另以 `.build/scheduler-m1/candidate.json`、`.build/scheduler-m1/acceptance.json` 及对应 attempt 为准；不继承旧全量结果。
- 既有周期 runner 仅验证工具行为，使用 synthetic `periodic-cpu-v1`，不能代替生产任务的执行时间、WCET、设备/IRQ 路径或业务截止时间证据。

## M0 整体仍未关闭

本环境实测为 KVM 虚拟机，运行 Linux 6.12 PREEMPT_DYNAMIC，`/sys/kernel/realtime` 不存在；不是 XTOS Linux 6.6.0-rt15 PREEMPT_RT 目标物理机。已有阶段 4A 文档也记录缺少真实硬件。没有确认的生产任务负责人、真实任务契约、实测预算来源、预注册收益/回归门槛，也没有目标物理机上对 FIFO/RR 与 DEADLINE 的同条件重复原始 trace。因此：

仓库另有 2026-10-10 的历史 H3C UniServer R4700 G3 物理观察：`.build/baremetal/evidence/hardware-observation-2026-10-10.json` 记录了目标身份、`6.6.0-rt15`、`realtime=1`；`.build/baremetal/evidence/hardware-cyclictest-parent.json` 记录了有限的两 CPU cyclictest 和普通负载观察。这证明曾有物理 RT 运行环境，但只是旧的有限观察，不是当前候选、真实任务或 FIFO/RR/DEADLINE 同条件对照。2026-10-25T07:00:53Z 的只读 BMC 快照记录关机，见 `.build/scheduler-m0/current-target-readonly.json`；该时刻观察不代表此后实时电源状态，本轮未确认可复用的运行窗口。

- 不把当前 KVM 数据称为 RT 性能基线。
- 不把 DEADLINE 准入拒绝解释为 Linux 性能缺口。
- 不批准 M0 go/no-go；M1 仅开展用户态参考工具开发，阶段收益门尚未通过。M2 内核调度类保持未开始。
- 若两种 Linux 原生策略满足任务需求，结论应为不开发专用调度类。

## 关闭 M0 必须取得的输入和证据

1. 由用户/项目负责人指定一台物理目标机及其负责人，提供可复核的机型、CPU/NUMA/SMT、固件/BIOS、电源状态及安全维护窗口；选择由操作者在现场执行，或另行提供合规的远程访问方式。不要在聊天中粘贴密码/私钥。
2. 在该目标机运行冻结的 XTOS PREEMPT_RT 镜像；归档源码、resolved config、镜像哈希、`uname`、`/sys/kernel/realtime` 和独立回退启动方法。
3. 指定一项真实代表性任务，提供负责人、实际应用/设备、输入采集或回放、任务依赖、release/period/deadline、CPU/IRQ 放置、过载处理，以及 WCET/budget 的测量依据。没有真实应用/设备及依据时不能用 synthetic workload 代填。
4. 在查看比较结果之前登记主指标、最小有意义收益、可容忍的各项回归、deadline-miss 规则、重复/预热、run 次序和统计区间方法。最低要求每种原生策略各 10 次有效、可重算的同条件运行；失败及准入拒绝亦保留。
5. 在相同目标机、镜像、任务输入、CPU/IRQ 放置与环境条件下分别运行 SCHED_FIFO/RR 和 SCHED_DEADLINE；保存原始 trace、manifest、配置和分析哈希，并由实现者之外的审查者复核。工具当前的合成 runner 不能证明真实应用的路径已正确接入；收到实际任务后需先确认/开发可复现的 workload adapter，再进行这些性能运行。
6. 根据预先登记的门槛形成“原生策略足够 / 原生策略存在可复现缺口 / 证据不足”之一的 go/no-go 结论；只有确认缺口与用户价值后，才考虑 M1。

## 输入模板

复制并填写 [`m0-target-intake.json`](m0-target-intake.json)。`null` 和空数组表示尚未提供，不代表通过。配置变更默认为禁止；任何系统级优化须另行授权，并保存变更前后状态和回滚方法。

当前收尾候选的完整维护结果以验收台账和 attempt 报告为准；即使源码工具/文档子门关闭，也不等于 M0 产品/实机 go/no-go 通过。下一步性能闭环需要上述外部实体（目标设备、任务应用/数据、门槛及负责人）；单靠本 KVM 开发容器无法取得或代替这些证据。
