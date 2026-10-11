# XTOS private bare-metal installation

Goal: Install and verify the private XTOS development system on the H3C R4700 G3 without publishing OpenXTOS changes.

Architecture: Boot a BIOS/UEFI GRUB ISO containing the exact PREEMPT_RT kernel and a recovery/installer initramfs. The installer explicitly checks model/serial/capacity and excludes mounted/removable/ROM disks. Install one identified disk with GPT BIOS boot, EFI system and ext4 root partitions. Firmware fallback boots GRUB from the EFI partition; the initramfs mounts the ext4 root by UUID and switch_root runs the same private kernel/rootfs with persistent storage.

Existing authorization: The user permits clearing disks on the H3C server BMC 192.168.100.99. This is NOT permission to overwrite development-host disks. Physical writes happen only from an identifiable installer boot on the target, after identity verification; do not guess nvme0n1 versus nvme1n1. Retain the other disk unless its separate use is necessary.

1. Inventory through IPMI/SOL and Redfish. Read exact serial, firmware, memory/CPU, NIC, Secure Boot and available virtual media. Do not treat H3C documentation example disk IDs as real inventory.
2. Add private bare-metal kernel fragment with NVMe, ixgbe, GPT, ext4, EFI, VGA/SOL and sufficient CPU support. Keep normal QEMU build unchanged.
3. Add tests first for boot-media files, identity checks, no implicit writes, source/output isolation, partition handling and root UUID. Extend BusyBox into a separate hardware configuration for init, switch_root, networking, tar and hashes.
4. Implement a bounded explicit installer and offline payload assembly. Include mkfs/sfdisk/GRUB dependencies, key-only Dropbear, cyclictest and minimal system inspection tools. No unauthenticated network shell or plaintext login password.
5. Build current private sources into a dedicated .build/baremetal folder; generate payload manifest, actual build/source identity, recovery ISO and EFI fallback loader.
6. In QEMU boot ISO, enumerate emulated NVMe with fixed serial, install to this test disk, power off, remove ISO and boot the disk. Require realtime=1, modules/SDK/selftest PASS, persistence after reboot and nonblank console. Test rejection cases without destructive writes.
7. Export only ISO over read-only NFS to the BMC; mount using H3C VmmControl. Set one-time CD boot, restart the authorized test machine, observe SOL.
8. Read actual target disks/model/serial/bytes and host motherboard identity. Choose one installation disk conservatively; check serial/capacity again immediately before partitioning. Save evidence. Reboot from disk without media and verify current kernel, RT config, storage and network.
9. Run short cyclictest first, then controlled CPU/I/O load. Treat finite test as observed latency, not hard-RT guarantee. Long tests require managed logs and process supervision. Record tested duration and outstanding BCI hardware/no-device boundaries.

Recovery: Keep installer ISO available and never modify BMC networking. Preserve original kernel/source worktree and OpenXTOS. Record private source/test diffs separately from pre-existing BCI work. Do not push public repositories.
