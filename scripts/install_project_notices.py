#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Install OpenXTOS project license and corresponding-source notices in a rootfs."""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path


def install(source_root: Path, rootfs: Path, artifact: str = "x86_64 installer") -> Path:
    docroot = rootfs / "usr/share/doc/openxtos"
    docroot.mkdir(parents=True, exist_ok=True)
    for name in ("LICENSE", "LICENSE-NOTICE", "NOTICE", "TRADEMARKS.md"):
        shutil.copy2(source_root / name, docroot / name)
    shutil.copy2(source_root / "kernel/linux/COPYING", docroot / "linux-COPYING")
    shutil.copy2(source_root / "kernel/linux/LICENSES/preferred/GPL-2.0", docroot / "GPL-2.0")
    shutil.copy2(source_root / "kernel/linux/LICENSES/exceptions/Linux-syscall-note", docroot / "Linux-syscall-note")
    shutil.copy2(source_root / "userspace/busybox/LICENSE", docroot / "busybox-LICENSE")
    common_licenses = Path("/usr/share/common-licenses")
    if common_licenses.is_dir():
        shutil.copytree(common_licenses, rootfs / "usr/share/common-licenses", dirs_exist_ok=True)
        shutil.copytree(common_licenses, docroot / "third-party-common-licenses", dirs_exist_ok=True)
    version_text = (source_root / "VERSION").read_text()
    match = re.search(r"^XTOS_VERSION=(.+)$", version_text, re.M)
    if not match:
        raise SystemExit("VERSION is missing XTOS_VERSION")
    version = match.group(1).strip().strip('"')
    tag = f"v{version}"
    if artifact == "arm64 bundle":
        offer = (
            f"OpenXTOS release source: https://github.com/Aitaide/OpenXTOS/archive/refs/tags/{tag}.tar.gz\n"
            "This ARM64 bundle contains the OpenXTOS kernel, PREEMPT_RT integration, BusyBox and user-space runtime.\n"
            "BusyBox and the user-space executables use the statically linked GNU C Library (LGPL-2.1-or-later) and the GCC runtime with its Runtime Library Exception. Exact cross-runtime package versions, copyrights, and Built-Using source inputs are recorded in HOST_PACKAGES.tsv, SOURCE_PACKAGES.tsv and third-party-copyright/.\n"
            f"The matching project and runtime sources, including cross-toolchain build material, are supplied in openxtos-{tag}-arm64-corresponding-source.tar.gz beside the boot bundle. Retain this asset and the notices when redistributing. The project sources and build scripts permit rebuilding/relinking the executables with a modified library.\n"
        )
    else:
        offer = (
            f"OpenXTOS release source: https://github.com/Aitaide/OpenXTOS/archive/refs/tags/{tag}.tar.gz\n"
            "The matching source archive contains the Linux kernel, PREEMPT_RT integration, BusyBox, and XTOS source for this system bundle.\n"
            f"The x86_64 installer also contains host binaries from Debian/Ubuntu packages. Their exact source packages are listed in SOURCE_PACKAGES.tsv; corresponding .dsc and source archives are published as openxtos-{tag}-x86_64-corresponding-source.tar.gz in the same release.\n"
            "The source archives and copyright notices should accompany the ISO when redistributed; follow the applicable licenses and preserve their required notices.\n"
        )
    (docroot / "CORRESPONDING-SOURCE.txt").write_text(offer)
    return docroot


def main() -> None:
    if len(sys.argv) not in (3, 4):
        raise SystemExit(f"usage: {sys.argv[0]} SOURCE_ROOT ROOTFS [ARTIFACT]")
    artifact = sys.argv[3] if len(sys.argv) == 4 else "x86_64 installer"
    print(f"project_notices=pass path={install(Path(sys.argv[1]), Path(sys.argv[2]), artifact)}")


if __name__ == "__main__":
    main()
