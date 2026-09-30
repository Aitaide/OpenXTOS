#!/usr/bin/env bash
# Build and validate an independent x86_64 PREEMPT_RT kernel configuration.
# All configuration, compilation, initramfs, and QEMU output stays under .build/.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
KERNEL_SRC="${KERNEL_SRC:-$PROJECT_ROOT/kernel/linux}"
BUILD_ROOT="${BUILD_ROOT:-$PROJECT_ROOT/.build}"
BUILD_DIR="${PREEMPT_RT_BUILD_DIR:-$BUILD_ROOT/preempt-rt-build}"
LOG="$BUILD_DIR/build.log"
QEMU_LOG="$BUILD_DIR/qemu.log"
INITRAMFS_DIR="$BUILD_DIR/initramfs"
INITRAMFS="$BUILD_DIR/initramfs.cpio.gz"
JOBS="${JOBS:-4}"

mkdir -p "$BUILD_DIR"
exec > >(tee -a "$LOG") 2>&1

printf '%s\n' "=== independent PREEMPT_RT validation build ==="
printf 'source=%s\noutput=%s\njobs=%s\n' "$KERNEL_SRC" "$BUILD_DIR" "$JOBS"

"$PROJECT_ROOT/scripts/prepare_linux.sh"
if [[ ! -x "$KERNEL_SRC/scripts/config" ]]; then
    printf '%s\n' "error: scripts/config is missing" >&2
    exit 1
fi

# Seed only the independent output configuration from the repository fragment.
if [[ ! -f "$BUILD_DIR/.config" ]]; then
    make -C "$KERNEL_SRC" O="$BUILD_DIR" ARCH=x86 defconfig
    KCONFIG_CONFIG="$BUILD_DIR/.config" "$KERNEL_SRC/scripts/kconfig/merge_config.sh" -m "$BUILD_DIR/.config" \
        "$PROJECT_ROOT/kernel/xtos_defconfig"
fi

# Explicitly select the RT model and request runtime config visibility.
"$KERNEL_SRC/scripts/config" --file "$BUILD_DIR/.config" \
    --enable EXPERT \
    --enable PREEMPT_RT \
    --disable PREEMPT_NONE \
    --disable PREEMPT_VOLUNTARY \
    --disable PREEMPT \
    --disable PREEMPT_DYNAMIC \
    --enable IKCONFIG \
    --enable IKCONFIG_PROC \
    --set-str LOCALVERSION "-xtos-rt-validation"

make -C "$KERNEL_SRC" O="$BUILD_DIR" ARCH=x86 olddefconfig
printf '%s\n' "=== resolved preemption configuration ==="
grep -E '^(CONFIG_(EXPERT|PREEMPT_RT|PREEMPT_NONE|PREEMPT_VOLUNTARY|PREEMPT|PREEMPT_DYNAMIC|ARCH_SUPPORTS_RT|IKCONFIG|IKCONFIG_PROC|HIGH_RES_TIMERS|HZ|XTOS_SCHED|XTOS_BCI))' "$BUILD_DIR/.config"

printf '%s\n' "=== building x86_64 bzImage and XTOS modules ==="
make -C "$KERNEL_SRC" O="$BUILD_DIR" ARCH=x86 -j"$JOBS" bzImage modules
if [[ ! -s "$BUILD_DIR/arch/x86/boot/bzImage" ]]; then
    printf '%s\n' "error: bzImage was not produced" >&2
    exit 1
fi
printf '%s\n' "=== build result ==="
file "$BUILD_DIR/arch/x86/boot/bzImage"
stat -c 'bzImage_bytes=%s' "$BUILD_DIR/arch/x86/boot/bzImage"
strings "$BUILD_DIR/vmlinux" | grep -E 'SMP PREEMPT_RT|PREEMPT_RT' | head -5 || true
printf '%s\n' "build_complete=1"

if ! command -v qemu-system-x86_64 >/dev/null || ! command -v cpio >/dev/null || ! command -v busybox >/dev/null; then
    printf '%s\n' 'qemu_validation=unavailable (qemu-system-x86_64, cpio, or busybox missing)'
    exit 0
fi

printf '%s\n' "=== preparing QEMU initramfs ==="
rm -rf "$INITRAMFS_DIR"
mkdir -p "$INITRAMFS_DIR"/{bin,dev,etc,proc,sys}
mkdir -p "$INITRAMFS_DIR/dev/shm"
cp "$(command -v busybox)" "$INITRAMFS_DIR/bin/busybox"
if [[ -x "$(command -v cyclictest)" ]]; then
    cp "$(command -v cyclictest)" "$INITRAMFS_DIR/bin/cyclictest"
fi
copy_dynamic_deps() {
    local binary="$1" lib
    ldd "$binary" | awk '{for (i = 1; i <= NF; i++) if ($i ~ /^\//) print $i}' | sort -u | while read -r lib; do
        [[ -n "$lib" && -f "$lib" ]] || continue
        mkdir -p "$INITRAMFS_DIR$(dirname "$lib")"
        cp -L "$lib" "$INITRAMFS_DIR$lib"
    done
}
copy_dynamic_deps "$INITRAMFS_DIR/bin/busybox"
[[ -x "$INITRAMFS_DIR/bin/cyclictest" ]] && copy_dynamic_deps "$INITRAMFS_DIR/bin/cyclictest"
ln -sf busybox "$INITRAMFS_DIR/bin/sh"
ln -sf busybox "$INITRAMFS_DIR/bin/mount"
ln -sf busybox "$INITRAMFS_DIR/bin/mkdir"
ln -sf busybox "$INITRAMFS_DIR/bin/chmod"
ln -sf busybox "$INITRAMFS_DIR/bin/cat"
ln -sf busybox "$INITRAMFS_DIR/bin/grep"
ln -sf busybox "$INITRAMFS_DIR/bin/zcat"
ln -sf busybox "$INITRAMFS_DIR/bin/uname"
ln -sf busybox "$INITRAMFS_DIR/bin/poweroff"

cat > "$INITRAMFS_DIR/init" <<'EOF'
#!/bin/sh
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mkdir -p /dev/shm
chmod 1777 /dev/shm
mount -t tmpfs -o mode=1777 tmpfs /dev/shm 2>/dev/null || true
printf '%s\n' '--- XTOS PREEMPT_RT QEMU validation ---'
printf 'uname: '; uname -a
printf 'sysfs realtime: '; cat /sys/kernel/realtime 2>/dev/null || printf '%s\n' unavailable
printf 'config PREEMPT_RT: '; zcat /proc/config.gz 2>/dev/null | grep -E '^CONFIG_PREEMPT_RT=' || printf '%s\n' unavailable
printf 'config PREEMPT_DYNAMIC: '; zcat /proc/config.gz 2>/dev/null | grep -E '^CONFIG_PREEMPT_DYNAMIC=' || printf '%s\n' unset
printf '%s\n' '--- QEMU cyclictest, virtualized, 10 seconds ---'
if [ -x /bin/cyclictest ]; then
    /bin/cyclictest --default-system -q -D 10 -i 1000 -p 80 -t 1 || \
        /bin/cyclictest --default-system -q -D 10 -i 1000 -p 0 -t 1
else
    printf '%s\n' 'cyclictest unavailable in guest'
fi
printf '%s\n' '--- end validation ---'
poweroff -f
EOF
chmod 0755 "$INITRAMFS_DIR/init"
(
    cd "$INITRAMFS_DIR"
    find . -print0 | cpio --null -o -H newc 2>/dev/null | gzip -9
) > "$INITRAMFS"

printf '%s\n' "=== booting with QEMU ==="
set +e
timeout --foreground "${QEMU_TIMEOUT:-45}" \
    qemu-system-x86_64 \
    -machine pc -cpu max -m 1024 -smp 2 \
    -kernel "$BUILD_DIR/arch/x86/boot/bzImage" \
    -initrd "$INITRAMFS" \
    -append 'console=ttyS0 rdinit=/init loglevel=7' \
    -nographic -no-reboot > "$QEMU_LOG" 2>&1
QEMU_RC=$?
set -e
cat "$QEMU_LOG"
printf 'qemu_exit=%s\n' "$QEMU_RC"

if grep -q 'config PREEMPT_RT: CONFIG_PREEMPT_RT=y' "$QEMU_LOG" && \
   grep -q 'sysfs realtime: 1' "$QEMU_LOG" && \
   grep -q -- '--- end validation ---' "$QEMU_LOG"; then
    printf '%s\n' 'qemu_validation=pass'
else
    printf '%s\n' 'qemu_validation=fail'
    exit 1
fi