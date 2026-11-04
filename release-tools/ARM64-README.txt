OpenXTOS v1.0.2 ARM64 boot bundle

Contents: Linux 7.3.1 Image, core initramfs, device trees, resolved configuration,
project and third-party notices. This is the core system, not the x86 developer
Live environment, a desktop, or a universal ARM board installer.

From the parent directory:
  qemu-system-aarch64 -machine virt -cpu cortex-a57 -m 2G -smp 2 \
    -kernel arm64/Image -initrd arm64/initramfs-arm64.cpio.gz \
    -append 'console=ttyAMA0 panic=1' -nographic

Physical boards require matching firmware, device tree and hardware validation.
QEMU results do not establish hardware real-time latency or medical safety.
Secure Boot is not supported by this bundle.

The matching corresponding-source archive is
openxtos-v1.0.2-arm64-corresponding-source.tar.gz. It includes exact project
source, release packaging recipe and the corresponding distribution sources.
Verify SHA256SUMS.txt before use; checksums are not independent signatures.
