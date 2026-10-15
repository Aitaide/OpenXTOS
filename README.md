# OpenXTOS 1.0.0-beta.1

OpenXTOS is an installable real-time operating system based on Linux 6.6.0 with PREEMPT_RT rt15, XTOS kernel components, BusyBox, and the XTOS SDK. The x86_64 release is a BIOS/UEFI USB installer, not only a QEMU test image. ARM64 boot artifacts are published separately; board support depends on its device tree, firmware, and drivers.

## Release downloads

The source tag triggers a CI release workflow. Once its x86_64 installer and ARM64 bundle builds pass, GitHub Releases hosts the prerelease assets and a checksum list. Asset names include the `v` prefix used by the Git tag.

## Install x86_64

Write the ISO to a USB drive with a raw-image writer (Rufus in DD mode, balenaEtcher, or equivalent), boot the target in BIOS or UEFI mode, and use the local VGA or serial console. The installer does not write a disk automatically.

Before installation, identify the exact whole disk, its serial, and byte capacity. The installer refuses removable media, partitions, mounted disks, active swap, holders, and mismatched identity. Installation irreversibly erases the selected disk and requires all of these values plus the exact `ERASE-XTOS` confirmation:

```sh
lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE,MOUNTPOINTS
blockdev --getsize64 /dev/nvme0n1
cat /proc/cmdline
xtos-install /dev/nvme0n1 'EXACT-SERIAL' EXACT-BYTES ERASE-XTOS
```

Replace the example path, serial, and capacity with values read from the target. Do not copy the example values literally. After installation, remove the USB and boot the installed disk. Public images ship with an empty root `authorized_keys`; remote SSH is not enabled until the owner provisions access. See `system/baremetal/README.zh-CN.md` for the full procedure and safety checks.

## Build from source

Linux, BusyBox, SDK, and integration sources are included in the repository. A Debian/Ubuntu x86_64 host needs the kernel build tools (`build-essential flex bison bc libssl-dev libelf-dev libc6-dev python3 cpio gzip kmod`) and the baremetal image tools (`grub-pc-bin grub-efi-amd64-bin grub2-common liblzo2-dev xorriso mtools dosfstools e2fsprogs fdisk util-linux dropbear-bin rt-tests pciutils iproute2 openssh-client qemu-system-x86 ovmf`).

Build the distributable x86_64 installation ISO and verify its checksum list:

```sh
JOBS=$(nproc) XTOS_BAREMETAL_BUILD_ROOT="$PWD/.build/baremetal-release" bash scripts/build_baremetal.sh --public
(cd .build/baremetal-release/images && sha256sum -c SHA256SUMS-public)
```

The x86 installer embeds host tools (including GRUB, Dropbear, and filesystem utilities). Its `/usr/share/doc/openxtos/` directory contains the project licenses and notices, a binary/source package inventory, and third-party copyright texts. Release downloads include the matching host-package source archives as `*-x86_64-corresponding-source.tar.gz`; preserve that asset when redistributing the ISO.

The public installer is `.build/baremetal-release/images/xtos-installer-public-x86_64.iso`. Build outputs remain under `.build/`; the public build does not generate or embed a maintenance key. Run the file-backed QEMU installer acceptance test with:

```sh
XTOS_BAREMETAL_BUILD_ROOT="$PWD/.build/baremetal-release" \
XTOS_INSTALLER_ISO="$PWD/.build/baremetal-release/images/xtos-installer-public-x86_64.iso" \
  QEMU_TIMEOUT=900 python3 scripts/verify_baremetal.py
```

This exercises virtual BIOS/UEFI install, boot, and persistence only; it is not evidence of physical hardware compatibility or latency.

For ARM64, install `crossbuild-essential-arm64`, `libc6-dev-arm64-cross`, and `qemu-system-arm`, then build and verify:

```sh
JOBS=$(nproc) bash scripts/build_system_arm64.sh
QEMU_TIMEOUT=300 bash scripts/verify_system_arm64.sh
bash scripts/verify_guest_arm64.sh
```

RISC-V 64 is also supported by the source build; install the RISC-V cross toolchain, `libc6-dev-riscv64-cross`, `qemu-system-misc`, and OpenSBI. The current release's installable-media artifact is x86_64; ARM64 is a boot bundle, not a universal board installer.

`XTOS_ABI_SDK_ROOT="$PWD/.build/userspace-source/sdk" ./scripts/check_abi.sh`, `./scripts/verify_source_tree.sh`, and `QEMU_TIMEOUT=300 ./scripts/verify_system.sh` cover ABI, source-tree hygiene, and x86_64 boot verification respectively after `build_system.sh`.

## Components

- `kernel/` — Linux 6.6.0, PREEMPT_RT rt15, and the XTOS integration patch.
- `userspace/` — BusyBox 1.37.0 source.
- `sdk/` — `libxtos-rt`, `libxtos-signal`, and `libxtos-bci`.
- `system/` — root filesystem, boot configuration, and installer.
- `tools/`, `examples/`, `scripts/` — signal tools, examples, builds, and verification.

The XTOS scheduler helper is an optional integration layer; it does not replace Linux's scheduler. BCI device operation requires compatible hardware. QEMU verifies software boot paths, not real-time worst-case guarantees, hardware support, medical safety, or device behavior.

## Licensing and attribution

XTOS user-space SDK, tools, examples, scripts and system files use Apache-2.0 (see `LICENSE`); Linux, PREEMPT_RT changes and BusyBox carry their own GPL-2.0 component licenses (see `LICENSE-NOTICE` and the bundled component license files).

Commercial use remains permitted under the applicable component licenses. Preserve required copyright, license, attribution and modification notices, and meet GPL corresponding-source obligations when distributing covered binaries. See [NOTICE](NOTICE) for project attribution information and [TRADEMARKS.md](TRADEMARKS.md) for the bilingual brand guide.

Clear attribution of the actual upstream version, integrated components, modifications and https://xtos.cn is recommended in an About, system-information or open-source notices screen and accompanying documentation. This is not an additional software-license condition. Logo/co-branding permission is separate; attribution does not imply official certification or endorsement.

Include applicable notices and license texts with downstream artifacts; a logo or website link is not a substitute for GPL or Apache compliance.

## Upstream

OpenXTOS is the public source distribution of XTOS. Its buildable Linux and BusyBox sources, SDK, and installer scripts are included here; private development plans, internal test infrastructure, and hardware-maintenance materials are not part of this repository.
