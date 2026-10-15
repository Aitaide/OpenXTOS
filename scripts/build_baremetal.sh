#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Build the distributable OpenXTOS x86_64 BIOS/UEFI installer.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
B="${XTOS_BAREMETAL_BUILD_ROOT:-$ROOT/.build/baremetal}"
export BUILD_ROOT="$B" JOBS="${JOBS:-8}"

if [[ "${1:-}" != "--public" ]]; then
    echo "usage: $0 --public" >&2
    exit 2
fi
mkdir -p "$B/driver" "$B/images"
for tool in grub-mkrescue grub-install xorriso dropbear dropbearkey sfdisk mkfs.ext4 mkfs.vfat cyclictest; do
    command -v "$tool" >/dev/null || { echo "error: missing tool $tool" >&2; exit 1; }
done

# Adapt the normal builders inside the isolated output directory. The source
# builders remain unchanged and all generated files stay below .build/.
python3 - "$ROOT" "$B" <<'PY'
import pathlib
import sys

root, build = map(pathlib.Path, sys.argv[1:])
for name in ("build_kernel.sh", "build_system.sh"):
    text = (root / "scripts" / name).read_text()
    text = text.replace(
        'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
        f'ROOT="{root}"',
    )
    if name == "build_kernel.sh":
        text = text.replace(
            '"$ROOT/kernel/xtos_defconfig"',
            '"$ROOT/kernel/xtos_defconfig" "$ROOT/kernel/xtos_baremetal_x86.config"',
        )
    else:
        text = text.replace('"$ROOT/scripts/build_kernel.sh"', f'"{build}/driver/build_kernel.sh"')
        text = text.replace('$ROOT/system/busybox_defconfig', f'{build}/driver/busybox.config')
        text = text.replace('gzip -n -9', 'gzip -n -6')
    target = build / "driver" / name
    target.write_text(text)
    target.chmod(0o755)

config = (root / "system/busybox_defconfig").read_text()
settings = """DD CMP TAR FEATURE_TAR_GNU_EXTENSIONS SWITCH_ROOT MOUNTPOINT
FEATURE_MOUNT_LABEL VOLUMEID FEATURE_VOLUMEID_EXT FEATURE_VOLUMEID_FAT
SHA256SUM FEATURE_MD5_SHA1_SUM_CHECK READLINK FEATURE_READLINK_FOLLOW SED AWK
FEATURE_SH_MATH FEATURE_SH_MATH_64 FEATURE_TEST_64 SETSID IP
FEATURE_IP_ADDRESS FEATURE_IP_LINK FEATURE_IP_ROUTE IFCONFIG
FEATURE_IFCONFIG_STATUS UDHCPC HOSTNAME KILL TOUCH DF FREE CHRT TASKSET LSMOD""".split()
for key in settings:
    config = config.replace(f"# CONFIG_{key} is not set", f"CONFIG_{key}=y")
config = config.replace(
    "CONFIG_FEATURE_SH_STANDALONE=y",
    "# CONFIG_FEATURE_SH_STANDALONE is not set",
).replace(
    "CONFIG_FEATURE_SH_NOFORK=y",
    "# CONFIG_FEATURE_SH_NOFORK is not set",
)
(build / "driver" / "busybox.config").write_text(config)
PY

XTOS_IMAGE_TYPE="${XTOS_IMAGE_TYPE:-developer}" "$B/driver/build_system.sh"

# Add the public installer runtime and leave root SSH authorization empty.
python3 - "$ROOT" "$B" <<'PY'
import pathlib
import re
import shutil
import subprocess
import sys

root, build = map(pathlib.Path, sys.argv[1:])
stage = build / "rootfs"
for name, target in (
    ("init", "init"),
    ("xtos-start", "sbin/xtos-start"),
    ("xtos-install", "usr/sbin/xtos-install"),
    ("udhcpc.script", "etc/udhcpc.script"),
):
    destination = stage / target
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "system/baremetal" / name, destination)
    destination.chmod(0o755)

(stage / "root/.ssh").mkdir(parents=True, exist_ok=True)
(stage / "root/.ssh/authorized_keys").write_text("")
(stage / "etc/shells").write_text("/bin/sh\n")

# Preserve absolute ELF interpreters and shared dependencies for the installer.
def copy_file(path):
    source = pathlib.Path(path)
    destination = stage / str(source).lstrip("/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)

for name in (
    "sfdisk", "mkfs.ext4", "mkfs.vfat", "blkid", "blockdev", "lsblk",
    "grub-install", "grub-probe", "grub-mkimage", "grub-bios-setup",
    "dropbear", "dropbearkey", "cyclictest", "lscpu", "lspci", "ip",
    "chrt", "taskset",
):
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"missing {name}")
    copy_file(path)
    output = subprocess.run(["ldd", path], capture_output=True, text=True, check=False).stdout
    for dependency in re.findall(r"(/[^\s()]+)", output):
        if pathlib.Path(dependency).is_file():
            copy_file(dependency)

for path in ("/usr/lib/grub", "/usr/share/grub", "/etc/mke2fs.conf"):
    source = pathlib.Path(path)
    destination = stage / source.relative_to("/")
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        copy_file(source)
PY

release="$(cat "$B/kernel-build/include/config/kernel.release")"
python3 "$ROOT/scripts/gen_system_identity.py" --output-dir "$B/rootfs/etc" \
    --image-type "${XTOS_IMAGE_TYPE:-developer}" --arch x86_64 --kernel-release "$release" \
    --build-id "${XTOS_BUILD_ID:-unknown}" --source-state "${XTOS_SOURCE_STATE:-unknown}" \
    --hardware-profile "${XTOS_HARDWARE_PROFILE:-unknown}"

python3 - "$ROOT" "$B" <<'PY'
import hashlib
import json
import pathlib
import subprocess
import sys

root, build = map(pathlib.Path, sys.argv[1:])
files = subprocess.check_output(
    ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
).split(b"\0")
source_hash = hashlib.sha256()
for filename in sorted(item for item in files if item):
    path = root / filename.decode()
    if path.is_file():
        source_hash.update(filename + b"\0" + hashlib.sha256(path.read_bytes()).digest())
manifest = {
    "git_head": subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
    "tracked_current_source_sha256": source_hash.hexdigest(),
    "dirty": subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True),
    "kernel_config_sha256": hashlib.sha256((build / "kernel-build/.config").read_bytes()).hexdigest(),
    "installer_variant": "public",
    "ssh": "no embedded key; user provisions keys post-install",
}
(build / "rootfs/etc/xtos-baremetal-build.json").write_text(json.dumps(manifest, indent=2) + "\n")
(build / "images/build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
PY

while IFS= read -r setting; do
    [[ "$setting" == CONFIG_*=* ]] || continue
    if [[ "$setting" == *=n ]]; then
        setting="# ${setting%%=*} is not set"
    fi
    grep -qx "$setting" "$B/kernel-build/.config" || {
        echo "missing $setting" >&2
        exit 1
    }
done < "$ROOT/kernel/xtos_baremetal_x86.config"

rm -rf "$B/installer" "$B/iso"
pack() {
    (cd "$1" && find . -print0 | LC_ALL=C sort -z | \
        cpio --null -o -H newc --owner=0:0 --reproducible 2>/dev/null | gzip -n -6) > "$2"
}
pack "$B/rootfs" "$B/images/boot.cpio.gz"
mkdir -p "$B/installer/payload"
cp -a "$B/rootfs/." "$B/installer/"
tar -C "$B/rootfs" -czf "$B/installer/payload/rootfs.tar.gz" .
cp "$B/images/xtos-bzImage" "$B/installer/payload/bzImage"
cp "$B/images/boot.cpio.gz" "$B/installer/payload/"
(cd "$B/installer/payload" && sha256sum rootfs.tar.gz bzImage boot.cpio.gz > sha256.txt)
pack "$B/installer" "$B/images/installer.cpio.gz"

mkdir -p "$B/iso/boot/grub"
cp "$B/images/xtos-bzImage" "$B/iso/boot/bzImage"
cp "$B/images/installer.cpio.gz" "$B/iso/boot/initramfs.gz"
cat > "$B/iso/boot/grub/grub.cfg" <<'EOF'
set timeout=3
set default=0
serial --unit=0 --speed=115200
terminal_input console serial
terminal_output console serial
menuentry 'OpenXTOS installer (manual installation)' {
 linux /boot/bzImage console=tty0 console=ttyS0,115200 xtos.installer=1
 initrd /boot/initramfs.gz
}
menuentry 'OpenXTOS recovery (installation disabled)' {
 linux /boot/bzImage console=tty0 console=ttyS0,115200
 initrd /boot/initramfs.gz
}
EOF

grub-mkrescue -o "$B/images/xtos-installer-public-x86_64.iso" "$B/iso"
python3 - "$B" <<'PY'
import hashlib
import json
import pathlib
import sys

build = pathlib.Path(sys.argv[1])
manifest_path = build / "images/build-manifest.json"
manifest = json.loads(manifest_path.read_text())
iso = build / "images/xtos-installer-public-x86_64.iso"
manifest["installer_artifact"] = {
    "filename": iso.name,
    "sha256": hashlib.sha256(iso.read_bytes()).hexdigest(),
    "bytes": iso.stat().st_size,
    "variant": "public",
    "ssh": "no embedded key; user provisions keys post-install",
    "hardware_support": "Intel X722 10GBASE-T PCI 8086:37d2; CONFIG_I40E=y",
}
manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
PY
(cd "$B/images" && sha256sum xtos-installer-public-x86_64.iso boot.cpio.gz installer.cpio.gz xtos-bzImage build-manifest.json > SHA256SUMS-public)
echo "XTOS_BAREMETAL_BUILD=PASS public-iso=$B/images/xtos-installer-public-x86_64.iso"
