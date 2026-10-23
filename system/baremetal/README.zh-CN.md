# XTOS 私有裸机安装介质

仅用于开发测试。当前平台是 Linux 6.6.0-rt15 + XTOS 私有工作树。Secure Boot 不支持，需要在目标机器固件中关闭；这里不自动修改固件或 BMC。QEMU 验证不能替代物理服务器驱动、固件及延迟验证。

## 构建与验证

Debian 构建依赖：常规 XTOS 内核/BusyBox 编译工具，以及 grub-pc-bin、grub-efi-amd64-bin、grub2-common、xorriso、mtools、dosfstools、e2fsprogs、fdisk、util-linux、dropbear-bin、rt-tests、pciutils、iproute2、openssh-client、qemu-system-x86、ovmf。

```
bash scripts/build_baremetal.sh
python3 scripts/verify_baremetal.py
# 在前一条已生成测试磁盘后，实测安装介质、持久系统及重启后的 SSH
python3 scripts/verify_baremetal_ssh.py
```

输出隔离在 `.build/baremetal/`；不会修改正常系统镜像目录。镜像为 `.build/baremetal/images/xtos-private-usb.iso`，哈希清单为同目录 `SHA256SUMS`，实际私有源码与内核配置标识为 `build-manifest.json`。校验时进入 images 目录运行 `sha256sum -c SHA256SUMS`。

维护 SSH 密钥由构建器生成：`.build/baremetal/keys/maintenance` 是私钥，只保留在控制端，绝不能上传镜像下载目录、提交仓库或发到聊天中；只有 `.pub` 公钥进入镜像。持有该私钥的人能够以 root 登录安装介质和安装后的系统。该镜像包含私有源码的编译结果，应限制分发范围。

## 制作 USB

使用 Rufus 的 DD 模式、balenaEtcher，或其他支持原始 ISO 写入的工具，将 ISO 写入一个确认可以清空的 U 盘。写入会清空 U 盘，务必核对设备型号和容量。仅复制 ISO 文件到普通 FAT 分区不能使其启动。此流程不在开发主机自动写任何物理磁盘。

将 U 盘插到服务器，选择一次性 USB 启动。介质同时提供 BIOS 和 UEFI 启动。默认进入安装/维护环境，**不会自动格式化目标磁盘**。VGA 和串口 ttyS0（115200）均提供控制台。等待屏幕显示 `XTOS_READY=1` 和 `XTOS_DHCP=网卡:地址`。

同一网络中有 DHCP 时可远程接管：

```
ssh -i /控制端绝对路径/maintenance root@目标DHCP地址
```

仅允许公钥登录，密码登录和 SSH 转发关闭。QEMU 实测记录位于 `.build/baremetal/evidence/ssh-results.json` 和 `ssh-*.ssh.log` / `ssh-*.rejection.log`，转发仅绑定控制端 localhost；这不代表物理网络已经接通。首次连接核对主机公钥指纹；安装环境重启会重新生成临时主机密钥，安装后首次生成的主机密钥保存在磁盘。若无 DHCP，需先在本地控制台配置业务网卡 IP；不要修改 BMC 管理网口。

## X722 网卡恢复镜像

目标服务器已通过物理 USB 启动并显示 `XTOS_READY=1`。实际 PCI 枚举为：

- `18:00.0`、`18:00.1`：Intel `8086:10fb`，使用 `ixgbe`；旧镜像已有 `eth0`、`eth1`，当时均为 `NO-CARRIER`。
- `3d:00.0`、`3d:00.1`：Intel X722 10GBASE-T `8086:37d2`，需要 `i40e`。旧镜像实际解析的内核配置为 `# CONFIG_I40E is not set`，因此这两个端口缺少驱动。

恢复构建在内核中内建 `CONFIG_I40E=y` 和其 `AUXILIARY_BUS` 依赖，输出 `.build/baremetal/images/xtos-private-usb-x722.iso`。`build-manifest.json` 的 `installer_artifact` 记录该文件名、大小和 SHA256；`SHA256SUMS` 同时覆盖新文件和验证器使用的标准文件名。原工作镜像与原校验清单保存在 `images/preserved-91275bf7a6677ccd4d8b9a71f9e5f195815217ac44ffea023e3a24596c37edb0/`，恢复构建继续使用原维护密钥。

将新 ISO 写入确认可清空的 USB 后，用一次性 USB 启动重新检查：

```
lspci -nnk
ip -br link
for nic in /sys/class/net/*; do
    echo "$nic"
    readlink -f "$nic/device"
    readlink -f "$nic/device/driver"
done
```

按 PCI 地址确认 X722 已绑定 `i40e`，不要假设其接口名称固定为 `eth2`、`eth3`。再检查对应端口的 carrier、IP 和 `XTOS_DHCP` 输出；若无 DHCP，核对业务网线、交换机及地址配置。旧 `ixgbe` 端口的 `NO-CARRIER` 不能证明 X722 接线状态。新增驱动与 QEMU 安装/SSH 验证不代表物理网络已恢复；仍需新镜像在目标服务器上启动确认。本修复不修改 BMC 管理网口或目标磁盘。

## 手动确认并安装

先查看目标身份，不能猜测 `/dev/nvme0n1` 是应该清空的盘：

```
lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE,MOUNTPOINTS
blockdev --getsize64 /dev/nvme0n1
cat /proc/cmdline
```

仅支持一个明确指定的整块 NVMe 或 SCSI/SATA 磁盘，至少 2 GiB；拒绝可移动设备、分区、已挂载分区、活动 swap、有 holders 的设备以及不匹配的身份。序列号使用 lsblk 显示值（去掉首尾空格），容量为十进制字节。串口及 SSH 控制台执行同一命令：

```
xtos-install /dev/明确目标盘 '完整序列号' 十进制字节数 ERASE-XTOS
```

这条命令会不可恢复地清空该盘分区表和文件系统。安装前重新核对；不要并行执行磁盘操作。只有 `xtos.installer=1` 的启动环境允许执行；恢复菜单和磁盘安装后的系统拒绝执行。安装器校验离线负载 SHA256，并在第一次写入之前再次读取身份与占用状态。

成功标记为 `XTOS_INSTALL=PASS`。安装创建 GPT BIOS boot、256 MiB EFI FAT32 和其余 ext4 根分区；写入 BIOS GRUB 与 `EFI/BOOT/BOOTX64.EFI` 回退加载器，不写 EFI NVRAM。根文件系统由 UUID 查找。完成后执行 `sync; poweroff -f`，拔出 U 盘，在固件启动菜单选择已安装磁盘。如果固件不能发现回退路径，需要手动选择该 EFI 文件；不会在此自动更改固件启动顺序。

## 启动后检查

```
cat /sys/kernel/realtime
xtos-selftest
lscpu
lspci
lsblk
ip addr
cyclictest --help
```

`realtime` 应为 `1`，自测应报告 `xtos_modules=pass` 和 `guest_userspace=pass`。实际 OpenBCI 采集仍需独立硬件验证。cyclictest 在 QEMU 中的结果仅代表虚拟环境，不是物理服务器最坏延迟保证。系统是最小开发环境，尚无生产升级、加密磁盘、账户管理或完整发行版维护服务。
