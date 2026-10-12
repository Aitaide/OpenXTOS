# XTOS Live 可选持久化

状态：已完成（2026-10-12，提交 ffdb752d1）；三靴 QEMU 循环通过，默认 RAM-only 路径在含持久化代码的当前源码上回归通过（candidate-21 bios-cd）。

## 设计

Live 系统默认 RAM-only：系统镜像（SquashFS 只读层 + tmpfs 写层）重启即丢失所有变更。持久化是**显式可选**的，不改变镜像不可变性质：

1. 用户用 `persist setup <分区> --confirm`（或对已有 ext4 用 `persist marker <分区>`）把一个专用分区格式化为 ext4、打上 `XTOS-PERSIST` 卷标、写入 `XTOS_PERSIST_ID` 标记（内容 `XTOS-PERSIST-1`）。
2. 用 `xtos.persist=XTOS-PERSIST` 内核命令行启动（测试变体 ISO 由 `build_live.py --kernel-extra` 生成）。
3. initramfs 的 `/init` 扫描块设备，找到卷标匹配**且**带标记的 ext4，挂到 `/run/persist`，把 `/home` 和 `/root` 以 bind mount 覆盖 overlay 上的同名目录。其余路径（系统、软件包、/etc、/usr）保持不可变。

不满足条件（无标记、卷标不符、超时）时打印 `XTOS_LIVE_PERSIST ... continuing RAM-only` 并按 RAM-only 继续，绝不挂载未授权的分区。

## 安全边界

- 只有带 `XTOS_PERSIST_ID` 标记的分区才会被接受——不会误挂任意同卷标分区或用户 U 盘。
- `persist setup` 需要 `--confirm`，格式化前有显式销毁警告；拒绝非块设备、路径穿越和已挂载目标。
- 持久范围仅 `/home` 与 `/root`；系统镜像保持只读，介质写测试仍然必须失败。
- SSH host key 仍然每次启动在 RAM 中生成（保持"每靴新身份"性质）；持久化的只是 authorized_keys 内容。

## 实现要点（防回归）

- **设备探测必须带重试**：NVMe 命名空间可能在 `/init` 启动之后才注册（dmesg 出现 `Ignoring bogus Namespace Identifiers` 且无 nvme0n1 注册行是正常现象，以 `/sys/class/block` 为准）。init 用 30 秒有界重试循环 + `mdev -s`，超时降级 RAM-only。
- **bind mount 必须在 `mount --move /run /newroot/run` 之前执行**：move 之后旧 `/run` 路径在当前命名空间不存在，bind 源路径 ENOENT。
- **authorized_keys 种子用 `[ ! -s ]`（缺失或为空都补种）并跟随 `sync`**：异常掉电可能提交 inode 但丢失数据，留下 0 字节文件；它会让 root 的公钥认证静默失效（dropbear 正常应答，root 始终 publickey denied）。补种只填补空缺，不覆盖非空的用户内容。
- `/home/xtos` 首靴从镜像骨架 `cp -a` 复制，uid 1000 保持。

## 验证（2026-10-12）

`tests/test_live_persistence.py`，candidate-20p2（iso sha256 `230fc3f4cfdb22e9d4fa650718b64fb49c6dbf563d2f27a817c336e7ecc54f1d`），64MB ext4 测试盘：

| 检查 | 结果 |
|---|---|
| boot1 `persistence=1`、持久卷识别为 `/dev/nvme0n1` | PASS |
| authorized_keys 自动补种（81 字节） | PASS |
| 写入 `/root/PERSIST-PROOF` 后干净关机 | PASS |
| boot2 `persistence=1`、证明文件跨重启逐字节恢复 | PASS |
| boot3 `/home/xtos` 绑定 + 可写 | PASS |
| 干净关机 ×3（`sync; poweroff -f`），磁盘 label/marker/标记文件完好 | PASS |

默认路径回归：candidate-21（同一源码、无 `--kernel-extra`）bios-cd 20 项检查全部通过，内部哨兵盘字节级不变。

## 边界与限制

- QEMU TCG 验证；物理 NVMe/SATA/USB 持久卷未实测。
- 持久卷不做加密；数据明文存储在分区上。
- `fsck` 未集成：异常断电后的 ext4 一致性依赖内核日志恢复；`fsck.ext4` 不在 Live 镜像内。
- 持久卷容量无监控/告警；写满表现与普通 ext4 相同。
- U 盘持久化（介质同时是启动盘和持久盘）未实现——持久卷必须是独立分区。
