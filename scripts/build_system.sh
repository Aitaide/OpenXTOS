#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/.build}"
mkdir -p "$BUILD_ROOT"
BUILD_ROOT="$(cd "$BUILD_ROOT" && pwd)"
KERNEL_DIR="$ROOT/kernel/linux"
KERNEL_OUTPUT="$BUILD_ROOT/kernel-build"
BB_OUTPUT="$BUILD_ROOT/busybox-build"
STAGING="$BUILD_ROOT/userspace-source"
ROOTFS="$BUILD_ROOT/rootfs"
IMAGES="$BUILD_ROOT/images"
JOBS="${JOBS:-2}"
CC="${CC:-gcc}"
for tool in "$CC" make cpio gzip depmod python3; do
    command -v "$tool" >/dev/null || { printf 'error: missing tool %s\n' "$tool" >&2; exit 1; }
done
BUILD_ROOT="$BUILD_ROOT" OUTPUT_DIR="$KERNEL_OUTPUT" JOBS="$JOBS" "$ROOT/scripts/build_kernel.sh"
mkdir -p "$BB_OUTPUT" "$IMAGES"
make -C "$ROOT/userspace/busybox" O="$BB_OUTPUT" KBUILD_DEFCONFIG="$ROOT/system/busybox_defconfig" defconfig
for setting in CONFIG_STATIC=y CONFIG_BUSYBOX=y CONFIG_ASH=y CONFIG_MODPROBE=y; do
    grep -qx "$setting" "$BB_OUTPUT/.config" || { printf 'error: missing BusyBox setting %s\n' "$setting" >&2; exit 1; }
done
make -C "$ROOT/userspace/busybox" O="$BB_OUTPUT" -j"$JOBS" CC="$CC"
"$BB_OUTPUT/busybox" sh -c 'exit 0'
if readelf -l "$BB_OUTPUT/busybox" | grep -q INTERP; then
    printf 'error: BusyBox must be statically linked\n' >&2
    exit 1
fi
# Stage project user space so its Makefiles never generate files in the source tree.
rm -rf "$STAGING" "$ROOTFS"
mkdir -p "$STAGING" "$ROOTFS"
for directory in sdk tools examples; do
    cp -a "$ROOT/$directory" "$STAGING/"
done
mkdir -p "$STAGING/kernel/linux/include/linux"
cp "$KERNEL_DIR/include/linux/xtos_queue.h" "$STAGING/kernel/linux/include/linux/"
make -C "$STAGING/sdk/libxtos-bci" clean all
make -C "$STAGING/sdk/libxtos-rt" clean all
make -C "$STAGING/sdk/libxtos-signal" clean all
make -C "$STAGING/tools/xtos-eeg-pipeline" clean all LDFLAGS='-static -lm'
make -C "$STAGING/tools/xtos-signal-benchmark" clean all LDLIBS='-static -lm'
make -C "$STAGING/sdk/libxtos-rt" test_rt test_spsc CFLAGS='-O2 -Wall -Wextra -Werror -static'
make -C "$STAGING/sdk/libxtos-signal" test_signal CFLAGS='-std=c11 -O2 -Wall -Wextra -Werror -static'
"$CC" -O2 -Wall -Wextra -static -I"$STAGING/sdk/libxtos-bci/include" \
    "$STAGING/examples/bci_test.c" "$STAGING/sdk/libxtos-bci/libxtos-bci.a" -o "$STAGING/examples/bci_test"
cp -a "$ROOT/system/rootfs/." "$ROOTFS/"
mkdir -p "$ROOTFS"/{bin,sbin,dev,etc,proc,sys,tmp,run,root,usr/bin,usr/include/xtos,usr/lib,usr/libexec/xtos,usr/share/xtos/fixtures}
install -m 0755 "$BB_OUTPUT/busybox" "$ROOTFS/bin/busybox"
while IFS= read -r applet; do
    [[ "$applet" == busybox ]] || ln -sf busybox "$ROOTFS/bin/$applet"
done < <("$BB_OUTPUT/busybox" --list)
install -m 0755 "$STAGING/tools/xtos-eeg-pipeline/xtos-eeg-pipeline" "$ROOTFS/usr/bin/"
install -m 0755 "$STAGING/tools/xtos-signal-benchmark/xtos-signal-benchmark" "$ROOTFS/usr/bin/"
install -m 0755 "$STAGING/examples/bci_test" "$ROOTFS/usr/bin/"
for library in bci rt signal; do
    install -m 0644 "$STAGING/sdk/libxtos-$library/libxtos-$library.a" "$ROOTFS/usr/lib/"
    cp -a "$STAGING/sdk/libxtos-$library/include/xtos/." "$ROOTFS/usr/include/xtos/"
done
for test in test_rt test_spsc; do
    install -m 0755 "$STAGING/sdk/libxtos-rt/$test" "$ROOTFS/usr/libexec/xtos/"
done
install -m 0755 "$STAGING/sdk/libxtos-signal/test_signal" "$ROOTFS/usr/libexec/xtos/"
install -m 0644 "$ROOT/tools/xtos-eeg-pipeline/fixtures/valid.csv" "$ROOTFS/usr/share/xtos/fixtures/"
make -C "$KERNEL_DIR" O="$KERNEL_OUTPUT" ARCH=x86 INSTALL_MOD_PATH="$ROOTFS" modules_install
release="$(make -s -C "$KERNEL_DIR" O="$KERNEL_OUTPUT" ARCH=x86 kernelrelease)"
rm -f "$ROOTFS/lib/modules/$release/build" "$ROOTFS/lib/modules/$release/source"
depmod -b "$ROOTFS" "$release"
chmod 0755 "$ROOTFS/init" "$ROOTFS/usr/bin/xtos-selftest"
REPRO_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "$ROOT" log -1 --pretty=%ct 2>/dev/null || date +%s)}"
(
    cd "$ROOTFS"
    find . -print0 | LC_ALL=C sort -z | xargs -0 touch --no-dereference --date="@$REPRO_EPOCH"
    find . -print0 | LC_ALL=C sort -z | cpio --null -o -H newc --owner=0:0 --reproducible 2>/dev/null | gzip -n -9
) > "$IMAGES/xtos-rootfs.cpio.gz"
install -m 0644 "$KERNEL_OUTPUT/arch/x86/boot/bzImage" "$IMAGES/xtos-bzImage"
install -m 0644 "$KERNEL_OUTPUT/.config" "$IMAGES/kernel.config"
printf 'system_build=pass\nkernel=%s\nrootfs=%s\n' "$IMAGES/xtos-bzImage" "$IMAGES/xtos-rootfs.cpio.gz"
