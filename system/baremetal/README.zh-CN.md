# OpenXTOS Public Installer Build and Test Guide

This directory contains the public x86_64 installer runtime. It has no embedded SSH key; until the installed owner provisions `authorized_keys`, the `xtos-start` script leaves Dropbear stopped. Secure Boot is unsupported. No automatic disk writes occur at boot.

## Build and verify

Install the normal Linux/BusyBox toolchain and Debian/Ubuntu packages:

```sh
sudo apt-get install build-essential flex bison bc libssl-dev libelf-dev libc6-dev python3 cpio gzip kmod \
  grub-pc-bin grub-efi-amd64-bin grub2-common xorriso mtools dosfstools e2fsprogs fdisk util-linux \
  dropbear-bin rt-tests pciutils iproute2 openssh-client qemu-system-x86 ovmf
```

Build the public installer, verify generated checksums, and run the file-backed BIOS/UEFI QEMU acceptance test:

```sh
JOBS=$(nproc) XTOS_BAREMETAL_BUILD_ROOT="$PWD/.build/baremetal-release" bash scripts/build_baremetal.sh --public
(cd .build/baremetal-release/images && sha256sum -c SHA256SUMS-public)
XTOS_BAREMETAL_BUILD_ROOT="$PWD/.build/baremetal-release" \
XTOS_INSTALLER_ISO="$PWD/.build/baremetal-release/images/xtos-installer-public-x86_64.iso" \
  QEMU_TIMEOUT=900 python3 scripts/verify_baremetal.py
```

The public installer is `.build/baremetal-release/images/xtos-installer-public-x86_64.iso`.

Build products and evidence stay under `.build/baremetal-release/`. The verifier overwrites only `.build/baremetal-release/evidence/test-nvme.raw`, a file-backed QEMU disk. It does not open or write host block devices. Results under QEMU TCG do not establish physical hardware support, network operation, or worst-case latency.

## USB and installation

The public ISO is `.build/baremetal-release/images/xtos-installer-public-x86_64.iso`. Write it as a raw image to a USB drive with Rufus in DD mode, balenaEtcher, or equivalent; this erases the USB drive. Boot in BIOS or UEFI mode with Secure Boot disabled and use the local VGA or serial console. Always verify the ISO checksum before use.

The installer will not touch a target disk automatically. Identify the exact whole device and its serial and byte capacity on the XTOS console:

```sh
lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE,MOUNTPOINTS
blockdev --getsize64 /dev/nvme0n1
cat /proc/cmdline
```

Only then run the command using actual target values. Installation irreversibly destroys the selected disk and requires the exact path, exact serial, exact byte capacity, installer boot mode, and literal `ERASE-XTOS` token:

```sh
xtos-install /dev/nvme0n1 'EXACT-SERIAL' EXACT-BYTES ERASE-XTOS
```

The installer rejects removable media, partitions, mounted devices, active swap, active holders, and identity mismatches; it rechecks immediately before writing. Replace the examples with values read from the target and never assume `/dev/nvme0n1` is safe. On completion, power off, remove the USB, and boot the installed disk. The public image leaves `/root/.ssh/authorized_keys` empty, so remote SSH remains disabled until an owner provisions a key locally.

After boot, `/sys/kernel/realtime` should contain `1`; `xtos-selftest` checks modules and userspace. The minimal image does not provide encrypted storage, Secure Boot, production update services, or account management. Real hardware and latency require separate validation.
