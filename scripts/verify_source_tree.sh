#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
violations=0

while IFS= read -r -d '' path; do
    relative="${path#$ROOT/}"
    case "$relative" in
        kernel/patches/*.patch|userspace/busybox/testsuite/*.zip)
            continue ;;
    esac
    case "$path" in
        *.o|*.ko|*.a|*.so|*.so.*|*.cmd|*.mod|*.mod.c|*.mod.o|*/bzImage|*/vmlinux|*/System.map|*.pyc|*.pyo|*.log|*.tar.xz|*.tar.gz|*.zip|*.stdout|*.tmp|*.out)
            printf 'generated_or_dependency=%s\n' "${path#$ROOT/}"
            violations=$((violations + 1)) ;;
    esac
done < <(git -C "$ROOT" ls-files -z --cached --others --exclude-standard)

while IFS= read -r -d '' path; do
    relative="${path#$ROOT/}"
    case "$relative" in
        kernel/linux/localversion-rt)
            continue ;;
    esac
    case "$path" in
        *.o|*.ko|*.a|*.so|*.so.*|*.cmd|*.mod|*.mod.c|*.mod.o|*/bzImage|*/vmlinux|*/System.map|*.pyc|*.pyo|*.log|*.tar.xz|*.tar.gz|*.zip|*.stdout|*.tmp|*.out|*/Module.symvers|*/modules.order|*/modules.builtin*)
            printf 'generated_in_bundled_kernel=%s\n' "$relative"
            violations=$((violations + 1)) ;;
    esac
done < <(find "$ROOT/kernel/linux" -type f -print0)

if [[ ! -f "$ROOT/kernel/linux/Makefile" ]]; then
    printf 'source_tree=fail missing bundled Linux source\n' >&2
    exit 1
fi
version="$(sed -n -n 's/^VERSION = //p' "$ROOT/kernel/linux/Makefile")"
patchlevel="$(sed -n -n 's/^PATCHLEVEL = //p' "$ROOT/kernel/linux/Makefile")"
sublevel="$(sed -n -n 's/^SUBLEVEL = //p' "$ROOT/kernel/linux/Makefile")"
if [[ "$version.$patchlevel.$sublevel" != "6.6.0" ]]; then
    printf 'source_tree=fail unexpected Linux version=%s.%s.%s\n' "$version" "$patchlevel" "$sublevel" >&2
    exit 1
fi
if [[ ! -s "$ROOT/kernel/linux/localversion-rt" ]] || ! grep -qx -- '-rt15' "$ROOT/kernel/linux/localversion-rt"; then
    printf 'source_tree=fail missing PREEMPT_RT rt15 marker\n' >&2
    exit 1
fi
for required in \
    "$ROOT/kernel/linux/kernel/sched/xtos_sched.c" \
    "$ROOT/kernel/linux/kernel/sched/xtos/Kconfig" \
    "$ROOT/kernel/linux/drivers/xtos/bci/xtos_bci_core.c" \
    "$ROOT/kernel/linux/include/linux/xtos.h" \
    "$ROOT/kernel/xtos_defconfig" \
    "$ROOT/kernel/patches/xtos-linux-6.6.patch"; do
    if [[ ! -s "$required" ]]; then
        printf 'source_tree=fail missing required source=%s\n' "${required#$ROOT/}" >&2
        exit 1
    fi
done

if [[ $violations -ne 0 ]]; then
    printf 'source_tree=fail violations=%d\n' "$violations" >&2
    exit 1
fi
printf 'source_tree=pass linux=6.6.0-rt15 xtos=present\n'