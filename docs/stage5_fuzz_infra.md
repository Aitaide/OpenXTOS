# 阶段 5（部分）：Fuzz 测试基础设施

**状态**: 已完成（fuzz 项）✅
**日期**: 2026-10-02
**目标**: 开发计划（Rev.2）阶段 5 中 "fuzz测试：CSV、设备包、ioctl和用户态解析器" 的落地

---

## 范围

主机无 libFuzzer/clang/AFL（仅 gcc 14.2.0），采用**确定性结构化变异 fuzz + ASan/UBSan harness**方案：无外部依赖、可复现、接入现有测试体系。

## 实现

**目录**: `tools/xtos-fuzz/`

### 1. Harness（C，ASan+UBSan，-fno-sanitize-recover=all）

- **`fuzz_csv_parse.c`** — 覆盖三块用户态解析/处理面：
  - `xtos-eeg-pipeline` 的 `parse_row` 镜像（`sscanf(" %llu , %lf , %lf %c")` + isfinite + int32 范围检查 + llround）
  - `xtos_bandpass_init/process` 敌意截止频率参数
  - `xtos_spsc` push/pop 往返不变量
- **`fuzz_openbci_packet.c`** — OpenBCI Cyton 内核驱动解析逻辑的精确用户态镜像：
  - 33 字节包状态机（`openbci_receive_buf` 字节流重组，含乱序切块驱动）
  - 24 位符号扩展 + 定点纳伏转换（`openbci_process_packet`）
  - 环形缓冲镜像写（`xtos_bci_ringbuf_write` 覆盖/回绕语义）
  - 不变量检查：`packet_pos ∈ [0, 33]`
  - 注意：驱动源码变更时需同步此镜像

### 2. 语料生成与变异（`gen_corpus.py`）

- 固定种子确定性生成（默认 seed=20261002），同一输入必然同一语料（有单测断言字节级一致）
- 领域感知种子：CSV 行语法（int/double 组合、nan/inf/溢出/十六进制/负数）、OpenBCI 包结构（起始字节/边界值/截断流）
- 变异算子：位翻转、token 注入/插入、截断、区域复制、拼接、语法感知行合成（含 `2147483647.5`、`-2147483648.5` 等 llround 边界值）
- `--check` 模式供 CI 快速校验语料

### 3. 驱动（`Makefile`）

- `make fuzz` — 全量跑（默认 200 轮，`FUZZ_ROUNDS` 可调）
- `make check` — 轻量校验（40 轮），接入 `scripts/test.sh`
- harness 运行强制 `UBSAN_OPTIONS=halt_on_error=1`

### 4. 测试（`test_fuzz.py`）

- 语料生成确定性（两轮生成字节级一致）
- `--check` 计数模式
- `make check` 全流程（构建+语料+运行无 crash）
- 已接入 `scripts/test.sh` 与 `.gitignore`

## 验证结果（2026-10-02）

- **全量 fuzz**: CSV 2018 + 包 2009 = 4027 个变异输入，两个 harness 全部通过（无 crash、无 ASan/UBSan 报告、无不变量违反）
- **有效性自证**: 向包解析镜像注入越界写（`packet_buf[40]`）后，坏版本被 UBSan 立即捕获退出非 0，好版本照常通过——证明 harness 检测能力真实有效，非"永远绿灯"
- **回归**: `./scripts/test.sh` 全量通过（`fuzz_check=pass` + 17 项既有测试无回归）

## 边界与已知限制

1. 无覆盖率引导，探索效率低于 libFuzzer/AFL；靠语法感知变异补偿
2. OpenBCI harness 是内核驱动的**镜像实现**而非驱动本体——内核态 serdev 无法在用户空间执行；驱动改动必须同步镜像（文件头有注释警示）
3. ioctl 接口的 fuzz 需要真实设备节点路径（`/dev/xtos0` 仅在硬件 probe 成功后存在），QEMU 无设备，归入真实硬件验证阶段
4. 语料每次重新生成，不入库（.gitignore 排除），由种子保证可复现

## 下一步

1. ✅ CSV/设备包/用户态解析器 fuzz
2. ⏳ 阶段 5 其余项: CI 跨架构矩阵、SBOM、reproducible build、ABI 版本化
3. ⏳ 真实硬件到位后: OpenBCI 串口 probe + ioctl 面 fuzz + cyclictest 24h
