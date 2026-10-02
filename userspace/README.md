# Bundled user-space sources

`busybox/` contains the unmodified BusyBox 1.37.0 release source, licensed under GPL-2.0. See `busybox/LICENSE` and its file-level notices.

Upstream source: https://busybox.net/downloads/busybox-1.37.0.tar.bz2
Archive SHA-256: 3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4

The archive is not required for building and is not committed. The source is committed directly so a system build does not download Linux or BusyBox. Build configuration is maintained in `system/busybox_defconfig`; build output is isolated under `.build/`.

The host C compiler, static libc development files, binutils, make, Python, cpio, gzip, and Linux build dependencies remain host prerequisites. They are not vendored toolchains.
