# OpenXTOS x86_64 安装镜像

本说明对应公开发布的 BIOS/UEFI 混合启动 ISO。镜像不包含预置 SSH 密钥；安装后需由系统所有者自行配置 `authorized_keys`，此之前 Dropbear 不会启动。Secure Boot 不支持。系统启动时不会自动写入任何磁盘。

## 构建与验证

Debian/Ubuntu 主机除 Linux/BusyBox 常规编译依赖外，还需安装以下软件包：

```sh
sudo apt-get install build-essential flex bison bc libssl-dev libelf-dev libc6-dev python3 cpio gzip kmod \
  grub-pc-bin grub-efi-amd64-bin grub2-common liblzo2-dev xorriso mtools dosfstools e2fsprogs fdisk util-linux \
  dropbear-bin rt-tests pciutils iproute2 openssh-client qemu-system-x86 ovmf
```

构建公开安装镜像、校验哈希，并用文件型 QEMU 磁盘验证 BIOS/UEFI 安装、启动和重启：

```sh
JOBS=$(nproc) XTOS_BAREMETAL_BUILD_ROOT="$PWD/.build/baremetal-release" bash scripts/build_baremetal.sh --public
(cd .build/baremetal-release/images && sha256sum -c SHA256SUMS-public)
XTOS_BAREMETAL_BUILD_ROOT="$PWD/.build/baremetal-release" \
XTOS_INSTALLER_ISO="$PWD/.build/baremetal-release/images/xtos-installer-public-x86_64.iso" \
  QEMU_TIMEOUT=900 python3 scripts/verify_baremetal.py
```

输出和证据保存在 `.build/baremetal-release/`。验证器仅会覆盖该目录下的文件型 QEMU 磁盘，不会打开或写入主机的物理块设备。QEMU TCG 结果不能证明物理硬件兼容、网络可用或最坏延迟。

## 制作启动 U 盘

公开 ISO 路径：`.build/baremetal-release/images/xtos-installer-public-x86_64.iso`。用 Rufus 的 DD 模式、balenaEtcher 或同类工具将 ISO 按原始镜像写入 U 盘；此操作会清空所选 U 盘。启动前先校验 ISO 哈希。目标机须关闭 Secure Boot，并可选择 BIOS 或 UEFI 启动；VGA 与串口控制台均可使用。

## 安装到目标磁盘

安装器不会自动修改目标盘。先在 XTOS 控制台读取目标整盘的设备路径、序列号和字节容量：

```sh
lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE,MOUNTPOINTS
blockdev --getsize64 /dev/nvme0n1
cat /proc/cmdline
```

只有在确认目标身份和数据已备份后，才运行安装命令。安装会不可恢复地清除指定磁盘，必须提供准确的整盘路径、完整序列号、精确字节容量、安装器启动模式，并输入固定确认词 `ERASE-XTOS`：

```sh
xtos-install /dev/nvme0n1 '完整序列号' 精确字节容量 ERASE-XTOS
```

示例中的设备路径不是默认安全目标，务必替换为现场读到的实际值。安装器会拒绝可移动设备、分区、已挂载设备、活动 swap、被占用的设备以及身份不匹配的磁盘，并在写入前再次检查。安装完成后关机、拔除 U 盘，再从已安装磁盘启动。

公开镜像的 `/root/.ssh/authorized_keys` 为空，因此不会开放远程 SSH；所有者须在本地控制台配置密钥后再启用远程维护。镜像内 `/usr/share/doc/openxtos/` 附有项目许可证、NOTICE、宿主软件包清单及第三方版权声明；正式 Release 同时提供与宿主二进制版本对应的源码归档，分发 ISO 时应一并保留对应源码。启动后 `/sys/kernel/realtime` 应为 `1`，`xtos-selftest` 用于检查内核模块和用户态测试。该最小开发系统不含加密磁盘、Secure Boot、生产升级服务或完整账户管理。物理硬件、网络和延迟仍需在目标设备上另行验证。
