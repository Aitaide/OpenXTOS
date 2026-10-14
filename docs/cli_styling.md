# Stage: 命令行界面美化（TTY-aware CLI styling）

状态：已完成
日期：2026-10-14

## 目标

给 XTOS 全部原生命令行工具（info / net / hw / clock / pkg / fw / rt / persist）加上颜色与样式，同时保证机器可读输出与自动化验证完全不受影响。

## 设计：tools/xtos-native/common/xtos_ui.h

单一共享头文件，三个硬性规则：

1. **只有交互式 TTY 才着色**。`isatty(STDOUT_FILENO)` + `TERM` 检查（空或 `dumb` 即关闭）。管道、重定向、SSH 非交互命令、`--json` 流全部输出与纯文本逐字节一致 —— `XTOS_FW_STATUS`、`PREEMPT_RT active`、`PKG_VERIFY` 等脚本标记与全部 verify_live.py 检查不受影响。
2. **只用基本 16 色 SGR 码**（30-37/90-97），不用 256 色/truecolor。从 vt100 到现代终端渲染一致。
3. **零依赖、可被任何工具 include**。静态编译，无 libc 依赖（isatty 除外）。

样式词汇（刻意精简）：

| 宏 | 效果 | 用途 |
|---|---|---|
| `XUI_TITLE` | 粗体+亮青 | 工具/节标题 |
| `XUI_LABEL` | 粗体 | 字段名（Kernel: 等） |
| `XUI_OK` | 粗体+亮绿 | 正常/活跃值（up、ACTIVE、true） |
| `XUI_WARN` | 粗体+亮黄 | 警告 |
| `XUI_BAD` | 粗体+亮红 | 错误/失效值 |
| `XUI_DIM` | 暗淡 | 次要信息（哈希、提示） |
| `XUI_ACC` | 亮洋红 | 命令提示、行动项 |

两种用法：`XUI_LABEL("Kernel: ")` 变参宏（带 printf 格式检查），或 `XUI_LABEL_S` 原始码串用于嵌入更宽的 printf。

## 改动清单

- `tools/xtos-native/common/xtos_ui.h`：新增共享头。
- 8 个工具的文本输出路径（JSON 路径全部未动）：info、net、hw、clock、pkg、fw、rt、persist。events 是纯 JSONL 数据流，刻意不改。
- `scripts/build_live.py`：`build_native_tools()` 增加 `-I tools/xtos-native/common`；`/etc/motd` 登录横幅换为 UTF-8 块字符 XTOS logo（亮青）+ 粗体标题 + 暗淡命令行 —— motd 只在交互登录时由 /etc/profile cat，SSH 命令路径永不渲染。PS1 原有着色保留。`write()` 助手改为显式 `encoding='utf-8'`：可复现构建环境 `LC_ALL=C` 下 `write_text` 默认编码是 ASCII，UTF-8 logo 会直接 UnicodeEncodeError。控制台 shell 是 LANG=C.UTF-8，块字符可正确解码。
- 测试编译命令同步加 `-I`（test_native_tools_fw_extended/net_route/pkg/rt/persist.py）。
- 新增 `tests/test_native_tools_ui.py` 并入 `scripts/test.sh`。

## 列对齐注意（构建过程中修复的问题）

info/net 的接口行原来是 `%-10s %-6s` 两列。着色 "up"/"down" 时若不带填充会破坏列对齐且造成管道输出回归。修法是把填充直接放进着色字面量（`"up    "` / `"down  "`），保持管道输出与旧版逐字节一致 —— 这一点已用 HEAD 版本二进制逐字节 diff 验证（Memory 行的数值抖动除外，那是实时值）。

## 验证（2026-10-14）

- 8 个工具 `gcc -O2 -Wall -Wextra -Werror -static -I common` 零警告。
- 管道输出与 HEAD 构建的旧二进制逐字节一致（info/net 修复填充后全量 diff 通过；Memory/uptime 为实时值不在比较范围）。
- pty（TERM=xterm-256color）下全部 styled 命令输出 SGR 码；TERM=dumb 下零转义。
- `--json` 在 TTY 上仍为纯 JSON（json.loads 通过）。
- 既有测试套件全绿：test_native_tools_rt / net_route / events / persist / pkg / fw_extended / live_build / live_runtime。
- 新增 test_native_tools_ui.py：build 8 工具 + pipe-plain + pipe-stable + tty-color + tty-dumb + json-pure + verify_live 标记存活性，ALL PASS。
- candidate-25 完整镜像 bios-cd QEMU 验证（含全部 SSH 标记检查）：见 evidence-25。
