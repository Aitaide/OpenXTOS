#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Enable deb-src entries mirroring installed deb repositories."""
from __future__ import annotations

import sys
import re
from pathlib import Path


def deb822_source_stanzas(text: str) -> list[str]:
    result = []
    for paragraph in (part.strip() for part in text.split("\n\n")):
        if not paragraph:
            continue
        types_line = next((line for line in paragraph.splitlines() if line.lower().startswith("types:")), None)
        if not types_line:
            continue
        if re.search(r"^Enabled:\s*no\s*$", paragraph, re.M | re.I):
            continue
        values = types_line.split(":", 1)[1].split()
        if "deb" not in values or "deb-src" in values:
            continue
        result.append(paragraph.replace(types_line, "Types: deb-src", 1))
    return result


def list_source_lines(text: str) -> list[str]:
    result = []
    for raw in text.splitlines():
        stripped = raw.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("deb-src "):
            continue
        if stripped.startswith("deb "):
            indent = raw[: len(raw) - len(stripped)]
            result.append(indent + "deb-src " + stripped[len("deb ") :])
    return result


def main() -> None:
    if len(sys.argv) > 2:
        raise SystemExit(f"usage: {sys.argv[0]} [APT_SOURCES_DIRECTORY_OR_FILE]")
    target = Path(sys.argv[1]) if len(sys.argv) == 2 else Path("/etc/apt/sources.list.d")
    directory = target.parent if target.is_file() else target
    files = []
    deb822 = []
    legacy = []
    existing_sources = False
    if target.is_file():
        files.append(target)
    elif directory.is_dir():
        files.extend(sorted(directory.iterdir()))
    main_list = directory.parent / "sources.list"
    if not target.is_file() and main_list.is_file():
        files.append(main_list)
    for path in files:
        if path.name in ("openxtos-deb-src.sources", "openxtos-deb-src.list") or not path.is_file():
            continue
        if path.suffix == ".sources":
            text = path.read_text()
            deb822.extend(deb822_source_stanzas(text))
            for paragraph in text.split("\n\n"):
                if not re.search(r"^Enabled:\s*no\s*$", paragraph, re.M | re.I):
                    existing_sources |= bool(re.search(r"^Types:[^\n]*\bdeb-src\b", paragraph, re.M | re.I))
        elif path.suffix == ".list":
            text = path.read_text()
            legacy.extend(list_source_lines(text))
            existing_sources |= bool(re.search(r"^\s*deb-src\s", text, re.M))
    if not deb822 and not legacy:
        if existing_sources:
            print("deb-src entries already enabled")
            return
        raise SystemExit(f"no enabled deb repositories found in {directory}")
    outputs = []
    if deb822:
        output = directory / "openxtos-deb-src.sources"
        output.write_text("\n\n".join(deb822).rstrip() + "\n")
        outputs.append(output)
    if legacy:
        output = directory / "openxtos-deb-src.list"
        output.write_text("\n".join(legacy).rstrip() + "\n")
        outputs.append(output)
    print(f"deb-src entries enabled: deb822={len(deb822)} legacy={len(legacy)} files={','.join(map(str, outputs))}")


if __name__ == "__main__":
    main()
