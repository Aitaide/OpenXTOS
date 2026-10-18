#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
B="${XTOS_BAREMETAL_BUILD_ROOT:-$ROOT/.build/baremetal}"
export BUILD_ROOT="$B" JOBS="${JOBS:-8}"
mkdir -p "$B/driver" "$B/images"
PUBLIC=0
if [[ "${1:-}" == "--public" ]]; then PUBLIC=1; else mkdir -p "$B/keys"; fi
for tool in grub-mkrescue grub-install xorriso dropbear dropbearkey sfdisk mkfs.ext4 mkfs.vfat cyclictest ssh-keygen; do command -v "$tool" >/dev/null; done
if [[ "$PUBLIC" -eq 0 && ! -s "$B/keys/maintenance" ]]; then ssh-keygen -q -t ed25519 -N '' -f "$B/keys/maintenance" -C xtos-private-maintenance; fi
# Adapt existing builders inside the isolated output directory; source builders stay unchanged.
python3 - "$ROOT" "$B" <<'PY'
import sys,pathlib
r,b=map(pathlib.Path,sys.argv[1:])
for name in ('build_kernel.sh','build_system.sh'):
 s=(r/'scripts'/name).read_text()
 s=s.replace('ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',f'ROOT="{r}"')
 if name=='build_kernel.sh':
  s=s.replace('"$ROOT/kernel/xtos_defconfig"','"$ROOT/kernel/xtos_defconfig" "$ROOT/kernel/xtos_baremetal_x86.config"')
 else:
  s=s.replace('"$ROOT/scripts/build_kernel.sh"',f'"{b}/driver/build_kernel.sh"')
  s=s.replace('$ROOT/system/busybox_defconfig',f'{b}/driver/busybox.config').replace('gzip -n -9','gzip -n -6')
 (b/'driver'/name).write_text(s)
 (b/'driver'/name).chmod(0o755)
s=(r/'system/busybox_defconfig').read_text()
settings='DD CMP TAR FEATURE_TAR_GNU_EXTENSIONS SWITCH_ROOT MOUNTPOINT FEATURE_MOUNT_LABEL VOLUMEID FEATURE_VOLUMEID_EXT FEATURE_VOLUMEID_FAT SHA256SUM FEATURE_MD5_SHA1_SUM_CHECK READLINK FEATURE_READLINK_FOLLOW SED AWK FEATURE_SH_MATH FEATURE_SH_MATH_64 FEATURE_TEST_64 SETSID IP FEATURE_IP_ADDRESS FEATURE_IP_LINK FEATURE_IP_ROUTE IFCONFIG FEATURE_IFCONFIG_STATUS UDHCPC HOSTNAME KILL TOUCH DF FREE CHRT TASKSET LSMOD'.split()
for k in settings: s=s.replace(f'# CONFIG_{k} is not set',f'CONFIG_{k}=y')
# Use external tools where BusyBox standalone lookup would bypass copied executables.
s=s.replace('CONFIG_FEATURE_SH_STANDALONE=y','# CONFIG_FEATURE_SH_STANDALONE is not set').replace('CONFIG_FEATURE_SH_NOFORK=y','# CONFIG_FEATURE_SH_NOFORK is not set')
(b/'driver/busybox.config').write_text(s)
PY
XTOS_IMAGE_TYPE="${XTOS_IMAGE_TYPE:-developer}" "$B/driver/build_system.sh"
# --public: distributable installer without an embedded maintenance key.
# SSH remains disabled until the installed owner provisions authorized_keys.
python3 - "$ROOT" "$B" "$PUBLIC" <<'PY'
import pathlib,sys,subprocess,shutil,re,json,hashlib
r,b=map(pathlib.Path,sys.argv[1:3]); root=b/'rootfs'; public=sys.argv[3]
for name,target in [('init','init'),('xtos-start','sbin/xtos-start'),('xtos-install','usr/sbin/xtos-install'),('udhcpc.script','etc/udhcpc.script')]:
 p=root/target;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(r/'system/baremetal'/name,p);p.chmod(0o755)
(root/'root/.ssh').mkdir(parents=True,exist_ok=True)
if public=='1':
    # Public image: NO embedded key. Console login + user-supplied keys only;
    # dropbear simply does not start until the installer sets one up.
    (root/'root/.ssh/authorized_keys').write_text('')
else:
    shutil.copy2(b/'keys/maintenance.pub',root/'root/.ssh/authorized_keys')
(root/'etc/shells').write_text('/bin/sh\n')
# Preserve absolute ELF interpreter paths and every shared dependency, including symlink targets.
def copy(p):
 p=pathlib.Path(p); dst=root/str(p).lstrip('/');dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dst)
for name in ['sfdisk','mkfs.ext4','mkfs.vfat','blkid','blockdev','lsblk','grub-install','grub-probe','grub-mkimage','grub-bios-setup','dropbear','dropbearkey','cyclictest','lscpu','lspci','ip','chrt','taskset']:
 p=shutil.which(name)
 if not p: raise RuntimeError('missing '+name)
 copy(p)
 out=subprocess.run(['ldd',p],capture_output=True,text=True).stdout
 for dep in re.findall(r'(/[^\s()]+)',out):
  if pathlib.Path(dep).is_file(): copy(dep)
for p in ['/usr/lib/grub','/usr/share/grub','/etc/mke2fs.conf']:
 src=pathlib.Path(p);dst=root/p.lstrip('/')
 if src.is_dir(): shutil.copytree(src,dst,dirs_exist_ok=True)
 else: copy(src)
PY
# Optional XTOS_* metadata is passed literally after all private rootfs additions.
release="$(cat "$B/kernel-build/include/config/kernel.release")"
python3 "$ROOT/scripts/gen_system_identity.py" --output-dir "$B/rootfs/etc" \
    --image-type "${XTOS_IMAGE_TYPE:-developer}" --arch x86_64 --kernel-release "$release" \
    --build-id "${XTOS_BUILD_ID:-unknown}" --source-state "${XTOS_SOURCE_STATE:-unknown}" \
    --hardware-profile "${XTOS_HARDWARE_PROFILE:-unknown}"
python3 - "$ROOT" "$B" "$PUBLIC" <<'PY'
import pathlib,sys,subprocess,json,hashlib
r,b=map(pathlib.Path,sys.argv[1:3]); root=b/'rootfs'; public=sys.argv[3]
files=subprocess.check_output(['git','-C',str(r),'ls-files','-z','--cached','--others','--exclude-standard']).split(b'\0')
h=hashlib.sha256()
for f in sorted(x for x in files if x):
 p=r/f.decode()
 if p.is_file(): h.update(f+b'\0'+hashlib.sha256(p.read_bytes()).digest())
manifest={'git_head':subprocess.check_output(['git','-C',str(r),'rev-parse','HEAD'],text=True).strip(),'tracked_current_source_sha256':h.hexdigest(),'dirty':subprocess.check_output(['git','-C',str(r),'status','--porcelain'],text=True),'kernel_config_sha256':hashlib.sha256((b/'kernel-build/.config').read_bytes()).hexdigest()}
if public != '1':
    manifest['maintenance_public_key_sha256']=hashlib.sha256((b/'keys/maintenance.pub').read_bytes()).hexdigest()
# Keep private provenance separate from the generated product identity.
(root/'etc/xtos-baremetal-build.json').write_text(json.dumps(manifest,indent=2)+'\n')
(b/'images/build-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
PY
# Check all requested kernel settings after Kconfig resolution.
while IFS= read -r setting; do [[ "$setting" == CONFIG_*=* ]] || continue; if [[ "$setting" == *=n ]]; then setting="# ${setting%%=*} is not set"; fi; grep -qx "$setting" "$B/kernel-build/.config" || { echo "missing $setting" >&2; exit 1; }; done < "$ROOT/kernel/xtos_baremetal_x86.config"
pack() { (cd "$1"; find . -print0 | LC_ALL=C sort -z | cpio --null -o -H newc --owner=0:0 --reproducible 2>/dev/null | gzip -n -6) > "$2"; }
pack "$B/rootfs" "$B/images/boot.cpio.gz"
mkdir -p "$B/installer/payload"
cp -a "$B/rootfs/." "$B/installer/"
tar -C "$B/rootfs" -czf "$B/installer/payload/rootfs.tar.gz" .
cp "$B/images/xtos-bzImage" "$B/installer/payload/bzImage"
cp "$B/images/boot.cpio.gz" "$B/installer/payload/"
(cd "$B/installer/payload"; sha256sum rootfs.tar.gz bzImage boot.cpio.gz > sha256.txt)
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
menuentry 'XTOS installer / maintenance (manual installation)' {
 linux /boot/bzImage console=tty0 console=ttyS0,115200 xtos.installer=1
 initrd /boot/initramfs.gz
}
menuentry 'XTOS recovery (installation disabled)' {
 linux /boot/bzImage console=tty0 console=ttyS0,115200
 initrd /boot/initramfs.gz
}
EOF
if [[ "$PUBLIC" -eq 1 ]]; then
    grub-mkrescue -o "$B/images/xtos-installer-public-x86_64.iso" "$B/iso"
    python3 - "$B" <<'PY'
import pathlib,sys,json,hashlib
b=pathlib.Path(sys.argv[1]); p=b/'images/build-manifest.json'
manifest=json.loads(p.read_text())
iso=b/'images/xtos-installer-public-x86_64.iso'
manifest.pop('maintenance_public_key_sha256',None)
manifest['installer_artifact']={'filename':iso.name,'sha256':hashlib.sha256(iso.read_bytes()).hexdigest(),'bytes':iso.stat().st_size,'variant':'public','ssh':'no embedded key; user provisions keys post-install','hardware_support':'Intel X722 10GBASE-T PCI 8086:37d2; CONFIG_I40E=y'}
p.write_text(json.dumps(manifest,indent=2)+'\n')
PY
    (cd "$B/images"; sha256sum xtos-installer-public-x86_64.iso boot.cpio.gz installer.cpio.gz xtos-bzImage build-manifest.json > SHA256SUMS-public)
    echo "XTOS_BAREMETAL_BUILD=PASS public-iso=$B/images/xtos-installer-public-x86_64.iso"
    exit 0
fi
grub-mkrescue -o "$B/images/xtos-private-usb.iso" "$B/iso"
# Distinct hardware-recovery artifact; retain the standard name for existing verifiers.
cp "$B/images/xtos-private-usb.iso" "$B/images/xtos-private-usb-x722.iso"
python3 - "$B" <<'PY'
import pathlib,sys,json,hashlib
b=pathlib.Path(sys.argv[1]); p=b/'images/build-manifest.json'
manifest=json.loads(p.read_text())
iso=b/'images/xtos-private-usb-x722.iso'
manifest['installer_artifact']={'filename':iso.name,'sha256':hashlib.sha256(iso.read_bytes()).hexdigest(),'bytes':iso.stat().st_size,'hardware_support':'Intel X722 10GBASE-T PCI 8086:37d2; CONFIG_I40E=y'}
p.write_text(json.dumps(manifest,indent=2)+'\n')
PY
(cd "$B/images"; sha256sum xtos-private-usb.iso xtos-private-usb-x722.iso boot.cpio.gz installer.cpio.gz xtos-bzImage build-manifest.json > SHA256SUMS)
echo "XTOS_BAREMETAL_BUILD=PASS iso=$B/images/xtos-private-usb-x722.iso"
