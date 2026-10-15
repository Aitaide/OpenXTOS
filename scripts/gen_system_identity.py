#!/usr/bin/env python3
"""Generate deterministic XTOS product identity files for a target rootfs."""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

PRODUCT_NAME = "XTOS"
PRODUCT_LABEL = "XTOS Bate版本"
HOME_URL = "https://github.com/Aitaide/OpenXTOS"
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SAFE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$")
SAFE_TEXT = re.compile(r"^[^\r\n\x00]*$")


def parse_version_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read version file {path}: {exc}") from exc
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        starts_quote = value.startswith('"')
        ends_quote = value.endswith('"')
        if starts_quote != ends_quote:
            raise ValueError(f"unterminated quoted value for {key}")
        if starts_quote:
            if len(value) < 2:
                raise ValueError(f"empty quoted value for {key}")
            value = value[1:-1]
        values[key] = value
    required = ("XTOS_VERSION", "XTOS_RELEASE_NAME", "XTOS_RELEASE_DATE")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ValueError("version file missing: " + ", ".join(missing))
    for key in required:
        if not SAFE_TEXT.fullmatch(values[key]):
            raise ValueError(f"invalid value for {key}")
    if not SAFE_VERSION.fullmatch(values["XTOS_VERSION"]):
        raise ValueError("invalid XTOS_VERSION: expected MAJOR.MINOR.PATCH[-PRERELEASE]")
    return values


def validate_token(value: str, name: str) -> str:
    if not SAFE_TOKEN.fullmatch(value):
        raise ValueError(f"invalid {name}: must match {SAFE_TOKEN.pattern}")
    return value


def validate_text(value: str, name: str) -> str:
    if not SAFE_TEXT.fullmatch(value):
        raise ValueError(f"invalid {name}: newline or NUL is not allowed")
    return value


def quote_os_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def build_outputs(version: dict[str, str], args: argparse.Namespace) -> dict[str, bytes]:
    image_type = validate_token(args.image_type, "image type")
    architecture = validate_text(args.arch, "architecture")
    kernel_release = validate_text(args.kernel_release, "kernel release")
    build_id = validate_text(args.build_id, "build id")
    source_state = validate_token(args.source_state, "source state")
    hardware_profile = validate_text(args.hardware_profile, "hardware profile")
    display_version = f"{version['XTOS_VERSION']} ({version['XTOS_RELEASE_NAME']}; {PRODUCT_LABEL})"
    pretty_name = f"{PRODUCT_LABEL} {version['XTOS_VERSION']}"
    metadata = {
        "architecture": architecture,
        "build_id": build_id,
        "hardware_profile": hardware_profile,
        "image_type": image_type,
        "kernel_release": kernel_release,
        "product_label": PRODUCT_LABEL,
        "product_name": PRODUCT_NAME,
        "release_date": version["XTOS_RELEASE_DATE"],
        "release_name": version["XTOS_RELEASE_NAME"],
        "source_state": source_state,
        "version": version["XTOS_VERSION"],
    }
    os_release = "\n".join(
        [
            f"NAME={quote_os_value(PRODUCT_NAME)}",
            "ID=xtos",
            f"VERSION_ID={quote_os_value(version['XTOS_VERSION'])}",
            f"VERSION={quote_os_value(display_version)}",
            f"PRETTY_NAME={quote_os_value(pretty_name)}",
            "RELEASE_TYPE=development",
            f"HOME_URL={quote_os_value(HOME_URL)}",
            "",
        ]
    )
    xtos_release = "\n".join(
        [
            f"XTOS_PRODUCT={PRODUCT_NAME}",
            f"XTOS_LABEL={PRODUCT_LABEL}",
            f"XTOS_VERSION={version['XTOS_VERSION']}",
            f"XTOS_RELEASE_NAME={version['XTOS_RELEASE_NAME']}",
            f"XTOS_RELEASE_DATE={version['XTOS_RELEASE_DATE']}",
            f"XTOS_IMAGE_TYPE={image_type}",
            f"XTOS_ARCHITECTURE={architecture}",
            f"XTOS_KERNEL_RELEASE={kernel_release}",
            f"XTOS_BUILD_ID={build_id}\n",
        ]
    )
    issue = f"{PRODUCT_LABEL} {version['XTOS_VERSION']}\n"
    motd = (
        f"{PRODUCT_LABEL} {version['XTOS_VERSION']}\n"
        f"image={image_type} arch={architecture} kernel={kernel_release}\n"
    )
    return {
        "os-release": os_release.encode("utf-8"),
        "xtos-release": xtos_release.encode("utf-8"),
        "xtos-build.json": (json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        "issue": issue.encode("utf-8"),
        "motd": motd.encode("utf-8"),
    }


def parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    result = argparse.ArgumentParser(description="Generate deterministic XTOS identity files")
    result.add_argument("--version-file", type=Path, default=root / "VERSION")
    result.add_argument("--output-dir", type=Path, required=True, help="directory receiving identity files")
    result.add_argument("--image-type", default="developer", help="safe image token, for example core or developer")
    result.add_argument("--arch", default="unknown")
    result.add_argument("--kernel-release", default="unknown")
    result.add_argument("--build-id", default="unknown")
    result.add_argument("--source-state", choices=("clean", "dirty", "unknown"), default="unknown")
    result.add_argument("--hardware-profile", default="unknown")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        version = parse_version_file(args.version_file)
        outputs = build_outputs(version, args)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for name, data in outputs.items():
            atomic_write(args.output_dir / name, data)
    except (OSError, ValueError) as exc:
        parser().error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
