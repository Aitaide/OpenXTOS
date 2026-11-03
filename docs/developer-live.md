# XTOS Developer Live 镜像

状态：candidate-10 已通过 QEMU 四模式（BIOS/UEFI × CD/USB）验证，命令名已简化，包含六个 XTOS 原生 C 工具（含 xtos-fw 防火墙）、串口控制台历史和 XTOS 品牌。这是私有测试候选，不是正式发布版，未部署到物理服务器。

## 镜像信息

- 文件：`.build/live/candidate-10/images/xtos-developer-live-x86_64.iso`（344 MB）
- ISO SHA256：`00502fdde3e7d51e0414280fa26d46258737da13eca48f1fc3d89556b23bc325`
- XTOS 原生 C 工具（6个，静态编译无运行时依赖）：
  - `info`：系统信息（版本/内核/realtime/网络/构建标识）
  - `net`：网络管理（show/up/down/addr add/addr flush）
  - `hw`：硬件诊断（PCI/USB/链路状态/系统摘要）
  - `clock`：时钟状态 + 一次性 NTP 同步（替换 Python 版）
  - `pkg`：包清单查询（summary/list/show，456 个包条目）
  - `fw`：防火墙管理（status/enable/disable，原生 Netfilter Netlink 直写）
- 内核：`6.6.0-rt15-xtos-live.1`（PREEMPT_RT，SquashFS/OverlayFS 内建）
- 用户空间：228 个锁定 Debian 包（含 Python 3.13/pip/venv、Go 1.24、GCC 14、nano/vi、GNU wget、dropbear、sudo、chrony）
- 存储：只读 Live 介质 + tmpfs 内存写层，`persistence=0`，介质不可写，无安装器
- SSH：仅公钥认证（`-s -g` 禁用所有密码方式），host key 每次启动在 RAM 中新生成
- 时钟：如实报告"未同步"；`xtos-clock sync SERVER` 显式一次性同步
- 版本显示：XTOS Bate版本 1.0.2（Stable 2，2026-11-03）

## 构建与验证命令

```sh
# 重建（需要 T04 的 live-developer 用户空间、live 内核、live BusyBox）
bash scripts/build_live.sh \
  --userspace-root .build/userspace/live-developer-x86_64 \
  --kernel-build .build/live-kernel-x86_64 \
  --busybox .build/live-busybox-x86_64/busybox \
  --output-dir .build/live/candidate-NEW \
  --authorized-key <公钥文件>

# 四模式验证（QEMU，文件级 ISO/USB + 哨兵磁盘）
python3 scripts/verify_live.py \
  --candidate .build/live/candidate-NEW \
  --key <对应私钥> \
  --evidence .build/live/evidence-NEW \
  --modes bios-cd,uefi-cd,bios-usb,uefi-usb
```

## 已验证内容（每模式均通过）

| 检查 | 结果 |
|---|---|
| BIOS/UEFI 固件 + CD/USB 介质四种组合启动 | PASS |
| 介质只读（写入被拒绝） | PASS |
| 内部磁盘字节级不变（哨兵标记首尾完整） | PASS |
| realtime=1、无 xtos.installer、无 xtos-install | PASS |
| overlay/tmpfs/squashfs 挂载布局 | PASS |
| root 与 xtos(uid 1000) 公钥 SSH 登录 | PASS |
| 密码/键盘交互认证被服务器拒绝（仅提供 publickey） | PASS |
| host key 与串口可信号道一致（known_hosts 严格校验） | PASS |
| SDK 自检（xtos-selftest，含模块加载） | PASS |
| 开发者用户写文件、python venv+pip check、ssl/sqlite3/ctypes/lzma、go version | PASS |
| 串口控制台上下键回溯历史（bios-cd 模式实测） | PASS |
| 控制台/登录界面显示 XTOS Bate 品牌（bios-cd 模式实测） | PASS |
| info 原生 C 工具（版本/realtime/网络/JSON，bios-cd SSH 实测） | PASS |
| net 原生 C 工具（接口列表/MTU/地址，bios-cd SSH 实测） | PASS |
| hw 原生 C 工具（PCI/块设备/网络/摘要，bios-cd SSH 实测） | PASS |
| clock 原生 C 工具（时钟状态/JSON，SSH 实测） | PASS |
| pkg 原生 C 工具（456 包/锁哈希，bios-cd SSH 实测） | PASS |
| fw 原生防火墙（status→enable→status→disable→status 完整循环，QEMU 6.6 内核实测） | PASS |
| 时钟状态不谎称已同步 | PASS |

证据：`.build/live/evidence-03b/results.json`、各模式 `.serial.log`、`.result.json`、`.known_hosts`、`spec-review.json`。

## 边界与限制

- QEMU TCG 验证，非物理硬件部署；H3C 服务器与现有 U 盘未改动
- Secure Boot 未验证、不支持
- 原生防火墙已实现（fw：netlink 原生 enable/disable/status + nft CLI 规则面，QEMU 6.6 实测）；物理机策略部署未验证
- copy-to-RAM 已实现：`xtos.toram=1` 启动时把 squashfs 复制进内存并释放介质（init 校验哈希后才 umount）；默认（非 toram）U 盘介质仍不可拔出
- locale 已修：镜像内置 localedef 编译的 C.utf8，bash/setlocale 警告已消除
- 系统日志：busybox syslogd/klogd RAM 环形缓冲（/run/log/messages，256KiB×2 轮转，tmpfs 随每次启动清空；不写持久卷）
- 常驻时钟：xtos-start 启动 chronyd 守护（/etc/chrony/chrony.conf）；`clock status` 仅在 chronyc tracking 证实追踪时才报 synchronized=true，一次性 sync 只记历史不冒充连续同步；`clock sync` 在守护运行时明确让位
- 上下键历史和品牌验证仅在 bios-cd 模式做了串口实测；其他三种模式走相同的 console 代码路径
- 独立规格审查由主会话直接执行（子代理提供方余额耗尽），全量回归通过

## 下一步

1. 在 H3C 服务器上用独立 U 盘/虚拟介质实机启动验证（需用户配合插入介质或使用 HDM 虚拟光驱）
2. 原生防火墙（T13–T16）
3. locale 数据打包，消除 console 警告
