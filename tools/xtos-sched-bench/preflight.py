#!/usr/bin/env python3
"""Read-only PREEMPT_RT target-host preflight collection.

The collector deliberately reports observations and availability rather than
changing scheduler, CPU, cgroup, IRQ, power, or firmware state.  It is kept
separate from the benchmark runner so a preflight cannot tune the host by
accident.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
from pathlib import PurePosixPath
from pathlib import Path
import re
import stat
import sys
from typing import Iterable, Optional
import zlib


SCHEMA = "xtos-sched-preflight-v1"
MAX_TEXT_BYTES = 256 * 1024
MAX_BINARY_BYTES = 4 * 1024 * 1024
MAX_CPU_ID = 1_000_000
MAX_CPU_LIST = 1_000_001
MAX_IRQ_ENTRIES = 100_000
MAX_CPU_USAGE_ENTRIES = 100_000

_CPU_PART = re.compile(r"\A([0-9]+)(?:-([0-9]+))?\Z")
_HEX_MASK = re.compile(r"\A(?:0[xX])?[0-9a-fA-F,]+\Z")


class _UnsafePath(ValueError):
    """A fixture path would escape the injected filesystem root."""


def _fact(status: str, value=None, source: Optional[str] = None,
          error: Optional[str] = None) -> dict:
    """Build a fact with a stable key order and explicit availability."""
    return {
        "status": status,
        "value": value,
        "source": source,
        "error": error,
    }


def _unknown(source: Optional[str], error: str = "not_found") -> dict:
    return _fact("unknown", None, source, error)


def _error(source: Optional[str], error: str) -> dict:
    return _fact("error", None, source, error)


def parse_cpu_list(value: str, *, allow_empty: bool = False) -> list[int]:
    """Parse a Linux CPU list without accepting malformed ranges."""
    if not isinstance(value, str):
        raise ValueError("malformed_cpu_range")
    text = value.strip()
    if not text:
        if allow_empty:
            return []
        raise ValueError("malformed_cpu_range")

    result: set[int] = set()
    parts = text.split(",")
    for part in parts:
        match = _CPU_PART.fullmatch(part.strip())
        if match is None:
            raise ValueError("malformed_cpu_range")
        start = int(match.group(1), 10)
        end = int(match.group(2) or match.group(1), 10)
        if start > MAX_CPU_ID or end > MAX_CPU_ID or start > end:
            raise ValueError("malformed_cpu_range")
        if end - start + 1 > MAX_CPU_LIST or len(result) + end - start + 1 > MAX_CPU_LIST:
            raise ValueError("cpu_range_too_large")
        result.update(range(start, end + 1))
    return sorted(result)


def _decode_mountinfo_path(value: str) -> str:
    """Decode the three octal escapes permitted in mountinfo paths."""
    return (value.replace(r"\040", " ")
                 .replace(r"\011", "\t")
                 .replace(r"\134", "\\"))


class Collector:
    """Collect bounded, read-only host facts from *root*.

    ``root`` is an injectable filesystem root for deterministic fixtures.  The
    optional ``allowed_cpus`` is an in-process diagnostic override; normal
    callers should leave it unset so the process affinity is obtained from
    ``os.sched_getaffinity``.
    """

    def __init__(self, root: os.PathLike[str] | str = "/",
                 allowed_cpus: Optional[Iterable[int] | str] = None):
        try:
            self.root = Path(root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"invalid root: {exc}") from exc
        self._allowed_override = allowed_cpus
        self.fixture_mode = self.root != Path("/")
        # Reopen without following any root ancestor, and remember the selected
        # directory identity. No persistent FD is retained by Collector.
        fd = self._open_root()
        try:
            info = os.fstat(fd)
            self._root_identity = (info.st_dev, info.st_ino)
        finally:
            os.close(fd)

    def _open_root(self) -> int:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for part in self.root.parts[1:]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY |
                                  os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            info = os.fstat(fd)
            if (hasattr(self, "_root_identity") and
                    (info.st_dev, info.st_ino) != self._root_identity):
                raise OSError("collector root identity changed")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _safe_path(self, absolute_path: str) -> Path:
        if not isinstance(absolute_path, str) or not absolute_path.startswith("/"):
            raise _UnsafePath("paths must be absolute collector paths")
        if "\x00" in absolute_path:
            raise _UnsafePath("NUL in collector path")
        relative = absolute_path.lstrip("/")
        candidate = self.root.joinpath(*relative.split("/"))
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise _UnsafePath(absolute_path) from exc
        return resolved

    def _open_resolved(self, path: Path, flags: int) -> int:
        """Open a previously resolved path without following replacements."""
        parts = path.relative_to(self.root).parts
        directory_fd = self._open_root()
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY |
                                  os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            return os.open(parts[-1], flags | os.O_CLOEXEC | os.O_NOFOLLOW,
                           dir_fd=directory_fd)
        finally:
            os.close(directory_fd)

    def _read_bytes(self, absolute_path: str,
                    limit: int = MAX_BINARY_BYTES) -> tuple[Optional[bytes], Optional[str]]:
        try:
            path = self._safe_path(absolute_path)
            # Resolve legitimate proc/sys links, then refuse substitutions at
            # every opened component. Nonblocking open rejects FIFOs before
            # a writer is present; validate the descriptor before reading.
            fd = self._open_resolved(path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return None, "not_regular"
                data = stream.read(limit + 1)
            if len(data) > limit:
                return None, "read_limit_exceeded"
            return data, None
        except _UnsafePath:
            return None, "unsafe_path"
        except FileNotFoundError:
            return None, "not_found"
        except PermissionError:
            return None, "permission_denied"
        except IsADirectoryError:
            return None, "not_regular"
        except OSError:
            return None, "read_error"

    def _read_text(self, absolute_path: str,
                   limit: int = MAX_TEXT_BYTES) -> tuple[Optional[str], Optional[str]]:
        data, error = self._read_bytes(absolute_path, limit)
        if error is not None:
            return None, error
        assert data is not None
        try:
            return data.decode("utf-8"), None
        except UnicodeDecodeError:
            return None, "invalid_utf8"

    def _directory(self, absolute_path: str) -> tuple[Optional[Path], Optional[str]]:
        try:
            path = self._safe_path(absolute_path)
            if not path.is_dir():
                return None, "not_directory"
            return path, None
        except _UnsafePath:
            return None, "unsafe_path"
        except FileNotFoundError:
            return None, "not_found"
        except PermissionError:
            return None, "permission_denied"
        except OSError:
            return None, "read_error"

    def _collect_uname(self) -> dict:
        if self.fixture_mode:
            return {"release": "unknown", "machine": "unknown"}
        try:
            info = os.uname()
            release = info.release if isinstance(info.release, str) else "unknown"
            machine = info.machine if isinstance(info.machine, str) else "unknown"
            return {"release": release or "unknown", "machine": machine or "unknown"}
        except (AttributeError, OSError):
            return {"release": "unknown", "machine": "unknown"}

    def _collect_virtualization(self) -> dict:
        # Positive markers are intentionally preferred.  Lack of a marker is
        # not treated as proof of physical hardware, so it remains unknown.
        hypervisor_type, error = self._read_text("/sys/hypervisor/type", 128)
        if error is None and hypervisor_type and hypervisor_type.strip():
            return {
                "status": "available",
                "value": "true",
                "source": "/sys/hypervisor/type",
                "error": None,
            }

        cpuinfo, cpuinfo_error = self._read_text("/proc/cpuinfo", MAX_TEXT_BYTES)
        if cpuinfo_error is None and cpuinfo is not None:
            if re.search(r"(?m)(?:^|\s)hypervisor(?:\s|$)", cpuinfo, re.IGNORECASE):
                return {
                    "status": "available",
                    "value": "true",
                    "source": "/proc/cpuinfo:hypervisor_flag",
                    "error": None,
                }

        dmi_paths = (
            "/sys/devices/virtual/dmi/id/product_name",
            "/sys/class/dmi/id/product_name",
            "/sys/devices/virtual/dmi/id/sys_vendor",
        )
        virtual_markers = (
            "kvm", "qemu", "virtualbox", "vmware", "microsoft corporation",
            "hyper-v", "xen", "bochs", "parallels", "bhyve", "virtual machine",
        )

        for path in dmi_paths:
            value, dmi_error = self._read_text(path, 1024)
            if dmi_error is not None or value is None:
                continue
            normalized = value.strip().lower()
            if any(marker in normalized for marker in virtual_markers):
                return {
                    "status": "available",
                    "value": "true",
                    "source": path,
                    "error": None,
                }


        observed_errors = [error, cpuinfo_error]
        if all(item in {"not_found", "permission_denied"} for item in observed_errors):
            reason = "markers_unavailable"
        else:
            reason = "no_reliable_marker"
        return {
            "status": "unknown",
            "value": "unknown",
            "source": "unavailable",
            "error": reason,
        }

    def _collect_realtime(self) -> dict:
        path = "/sys/kernel/realtime"
        value, error = self._read_text(path, 128)
        if error is not None:
            return _unknown(path, error)
        assert value is not None
        normalized = value.strip()
        if normalized not in {"0", "1"}:
            return _error(path, "malformed_realtime_value")
        return _fact("available", normalized, path, None)

    def _collect_allowed(self) -> dict:
        if self._allowed_override is not None:
            try:
                if isinstance(self._allowed_override, str):
                    values = parse_cpu_list(self._allowed_override)
                else:
                    values_set: set[int] = set()
                    for cpu in self._allowed_override:
                        if isinstance(cpu, bool) or not isinstance(cpu, int):
                            raise ValueError("malformed_allowed_cpus")
                        if cpu < 0 or cpu > MAX_CPU_ID:
                            raise ValueError("malformed_allowed_cpus")
                        values_set.add(cpu)
                    values = sorted(values_set)
                if not values:
                    raise ValueError("empty_allowed_cpus")
                return _fact("available", values, "collector:allowed_cpus", None)
            except (TypeError, ValueError) as exc:
                return _error("collector:allowed_cpus", str(exc))

        if self.fixture_mode:
            return _unknown("collector:allowed_cpus", "fixture_affinity_not_provided")
        try:
            values = sorted(int(cpu) for cpu in os.sched_getaffinity(0))
            if any(cpu < 0 or cpu > MAX_CPU_ID for cpu in values):
                return _error("os.sched_getaffinity", "malformed_allowed_cpus")
            return _fact("available", values, "os.sched_getaffinity", None)
        except (AttributeError, OSError, TypeError, ValueError):
            return _unknown("os.sched_getaffinity", "unavailable")

    def _collect_online(self) -> dict:
        path = "/sys/devices/system/cpu/online"
        value, error = self._read_text(path, 4096)
        if error is not None:
            return _unknown(path, error)
        assert value is not None
        try:
            parsed = parse_cpu_list(value)
        except ValueError as exc:
            return _error(path, str(exc))
        return _fact("available", parsed, path, None)

    def _collect_cpu(self) -> dict:
        online = self._collect_online()
        allowed = self._collect_allowed()
        if online["status"] == "available" and allowed["status"] == "available":
            intersection = _fact(
                "available",
                sorted(set(online["value"]) & set(allowed["value"])),
                "online∩allowed",
                None,
            )
        elif online["status"] == "error":
            intersection = _error("online∩allowed", online["error"])
        elif allowed["status"] == "error":
            intersection = _error("online∩allowed", allowed["error"])
        else:
            intersection = _unknown("online∩allowed", "inputs_unavailable")
        return {"online": online, "allowed": allowed, "intersection": intersection}

    @staticmethod
    def _parse_cmdline_cpu_option(value: str) -> list[int]:
        if not isinstance(value, str) or not value:
            raise ValueError("malformed_cpu_range")
        result: set[int] = set()
        accepted_flags = {"managed_irq", "domain", "nohz", "off", "strict"}
        for item in value.split(","):
            item = item.strip()
            if item.startswith("cpus:"):
                item = item[6:]
            if item in accepted_flags:
                continue
            match = _CPU_PART.fullmatch(item)
            if match is None:
                raise ValueError("malformed_cpu_range")
            start = int(match.group(1), 10)
            end = int(match.group(2) or match.group(1), 10)
            if start > MAX_CPU_ID or end > MAX_CPU_ID or start > end:
                raise ValueError("malformed_cpu_range")
            if len(result) + end - start + 1 > MAX_CPU_LIST:
                raise ValueError("cpu_range_too_large")
            result.update(range(start, end + 1))
        if not result:
            raise ValueError("malformed_cpu_range")
        return sorted(result)

    def _collect_cmdline(self) -> dict:
        path = "/proc/cmdline"
        value, error = self._read_text(path, MAX_TEXT_BYTES)
        option_names = ("isolated", "nohz_full", "housekeeping")
        if error is not None:
            return {
                "status": "unknown",
                "isolated": _unknown(path + ":isolcpus", error),
                "nohz_full": _unknown(path + ":nohz_full", error),
                "housekeeping": _unknown(path + ":housekeeping", error),
                "source": path,
                "error": error,
            }
        assert value is not None
        raw_options: dict[str, list[str]] = {
            "isolated": [], "nohz_full": [], "housekeeping": []
        }
        option_keys = {
            "isolated": "isolcpus",
            "nohz_full": "nohz_full",
            "housekeeping": "housekeeping",
        }
        for token in value.split():
            if "=" not in token:
                continue
            key, option_value = token.split("=", 1)
            for result_key, expected_key in option_keys.items():
                if key == expected_key:
                    raw_options[result_key].append(option_value)

        facts = {}
        had_error = False
        for result_key in option_names:
            option_path = path + ":" + option_keys[result_key]
            values = raw_options[result_key]
            if not values:
                facts[result_key] = _unknown(option_path, "option_absent")
                continue
            if len(values) != 1:
                facts[result_key] = _error(option_path, "duplicate_option")
                had_error = True
                continue
            try:
                parsed = self._parse_cmdline_cpu_option(values[0])
            except ValueError as exc:
                facts[result_key] = _error(option_path, str(exc))
                had_error = True
            else:
                facts[result_key] = _fact("available", parsed, option_path, None)

        return {
            "status": "partial" if had_error else "available",
            "isolated": facts["isolated"],
            "nohz_full": facts["nohz_full"],
            "housekeeping": facts["housekeeping"],
            "source": path,
            "error": "malformed_option" if had_error else None,
        }

    def _find_cgroup2_mount(self) -> tuple[Optional[str], Optional[str], Optional[bool]]:
        self._cgroup_mount_root = "/"
        mountinfo, mount_error = self._read_text("/proc/self/mountinfo", MAX_TEXT_BYTES)
        if mount_error is None and mountinfo is not None:
            saw_cgroup_mount = False
            for line in mountinfo.splitlines():
                if " - " not in line:
                    continue
                pre, post = line.split(" - ", 1)
                pre_parts = pre.split()
                post_parts = post.split()
                if len(pre_parts) < 5 or len(post_parts) < 1:
                    continue
                if post_parts[0] == "cgroup2":
                    self._cgroup_mount_root = _decode_mountinfo_path(pre_parts[3])
                    mountpoint = _decode_mountinfo_path(pre_parts[4])
                    return mountpoint, None, True
                if post_parts[0] == "cgroup":
                    saw_cgroup_mount = True
            if saw_cgroup_mount:
                return None, None, False
            return None, "cgroup2_not_mounted", None
        if mount_error is not None:
            _, error = self._read_text("/sys/fs/cgroup/cgroup.controllers", MAX_TEXT_BYTES)
            if error is None:
                # Discovery without mountinfo cannot establish a bind root.
                # Root-relative lookup below is a best-effort observation only.
                return "/sys/fs/cgroup", None, True
        return None, mount_error or "not_found", None

    def _collect_cgroup(self) -> dict:
        mountpoint, mount_error, v2_value = self._find_cgroup2_mount()
        if v2_value is True:
            # Keep mount discovery provenance generic; mountpoint names may
            # contain deployment-specific or otherwise sensitive identifiers.
            v2 = _fact("available", True, "cgroup2:mount", None)
        elif v2_value is False:
            v2 = _fact("available", False, "/proc/self/mountinfo", None)
        else:
            v2 = _unknown("/sys/fs/cgroup", mount_error or "unavailable")

        # Do not echo a cgroup pathname from /proc/self/cgroup.  It may carry
        # deployment-specific names, and the report only needs the fact and a
        # stable source label.
        effective_source = "cgroup2:cpuset.cpus.effective"
        if v2_value is not True or mountpoint is None:
            effective = _unknown(effective_source, "cgroup_v2_unavailable")
        else:
            cgroup_data, cgroup_error = self._read_text("/proc/self/cgroup", MAX_TEXT_BYTES)
            if cgroup_error is not None:
                effective = _unknown(effective_source, cgroup_error)
            else:
                assert cgroup_data is not None
                unified_path = None
                for line in cgroup_data.splitlines():
                    fields = line.split(":", 2)
                    if len(fields) == 3 and fields[0] == "0":
                        unified_path = fields[2]
                        break
                if unified_path is None:
                    effective = _error(effective_source, "unified_cgroup_path_missing")
                elif not unified_path.startswith("/") or "\x00" in unified_path:
                    effective = _error(effective_source, "unsafe_cgroup_path")
                else:
                    components = [part for part in unified_path.split("/") if part]
                    if any(part in {".", ".."} for part in components):
                        effective = _error(effective_source, "unsafe_cgroup_path")
                    else:
                        mount_root = PurePosixPath(self._cgroup_mount_root)
                        membership = PurePosixPath(unified_path)
                        if (not mount_root.is_absolute() or ".." in mount_root.parts or
                                not membership.is_relative_to(mount_root)):
                            return {
                                "status": "error", "v2": v2,
                                "effective_cpuset": _error(effective_source, "cgroup_mount_root_mismatch"),
                                "source": "/sys/fs/cgroup", "error": "unavailable",
                            }
                        components = list(membership.relative_to(mount_root).parts)
                        candidate = mountpoint.rstrip("/")
                        if components:
                            candidate += "/" + "/".join(components)
                        candidate += "/cpuset.cpus.effective"
                        value, read_error = self._read_text(candidate, 4096)
                        if read_error is not None:
                            effective = _unknown(effective_source, read_error)
                        else:
                            assert value is not None
                            try:
                                parsed = parse_cpu_list(value, allow_empty=True)
                            except ValueError as exc:
                                effective = _error(effective_source, str(exc))
                            else:
                                effective = _fact("available", parsed, effective_source, None)

        if v2["status"] == "error" or effective["status"] == "error":
            status = "error"
        elif v2["status"] == "available" and v2["value"] is False:
            status = "available"
        elif v2["status"] == "available" and effective["status"] == "available":
            status = "available"
        elif v2["status"] == "available" or effective["status"] == "available":
            status = "partial"
        else:
            status = "unknown"
        return {
            "status": status,
            "v2": v2,
            "effective_cpuset": effective,
            "source": "/sys/fs/cgroup",
            "error": None if status in {"available", "partial"} else "unavailable",
        }

    def _collect_governor(self, allowed: dict) -> dict:
        per_cpu: dict[str, dict] = {}
        if allowed["status"] != "available":
            return {
                "status": "unknown",
                "per_cpu": per_cpu,
                "source": "/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor",
                "error": "allowed_cpus_unavailable",
            }
        for cpu in allowed["value"]:
            path = f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor"
            value, error = self._read_text(path, 128)
            if error is not None:
                per_cpu[str(cpu)] = _unknown(path, error)
            elif value is None or not value.strip():
                per_cpu[str(cpu)] = _error(path, "empty_governor")
            else:
                normalized = value.strip()
                if "\n" in normalized or "\x00" in normalized:
                    per_cpu[str(cpu)] = _error(path, "malformed_governor")
                else:
                    per_cpu[str(cpu)] = _fact("available", normalized, path, None)

        statuses = [item["status"] for item in per_cpu.values()]
        if statuses and all(item == "available" for item in statuses):
            status = "available"
        elif any(item == "available" for item in statuses):
            status = "partial"
        elif any(item == "error" for item in statuses):
            status = "error"
        else:
            status = "unknown"
        return {
            "status": status,
            "per_cpu": per_cpu,
            "source": "/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor",
            "error": None if status in {"available", "partial"} else "unavailable",
        }

    @staticmethod
    def _parse_affinity_list(value: str) -> list[int]:
        return parse_cpu_list(value)

    @staticmethod
    def _parse_affinity_mask(value: str) -> list[int]:
        normalized = value.strip()
        if not _HEX_MASK.fullmatch(normalized):
            raise ValueError("malformed_irq_affinity")
        normalized = normalized.replace(",", "")
        try:
            mask = int(normalized, 16)
        except ValueError as exc:
            raise ValueError("malformed_irq_affinity") from exc
        result = []
        bit = 0
        while mask:
            if mask & 1:
                result.append(bit)
            mask >>= 1
            bit += 1
            if bit > MAX_CPU_ID:
                raise ValueError("irq_affinity_too_large")
        if not result:
            raise ValueError("malformed_irq_affinity")
        return result

    def _collect_irq_affinity(self, allowed: dict) -> dict:
        source = "/proc/irq"
        directory, error = self._directory(source)
        if error is not None:
            return {
                "status": "unknown",
                "irqs_seen": 0,
                "affinity_read": 0,
                "malformed": 0,
                "unreadable": 0,
                "single_cpu": 0,
                "multi_cpu": 0,
                "cpu_usage": {},
                "outside_allowed": None,
                "source": source,
                "error": error,
            }
        assert directory is not None
        truncated = False
        directory_fd = None
        try:
            # Bound directory enumeration as well as individual file reads.
            entries = []
            directory_fd = self._open_resolved(directory, os.O_RDONLY | os.O_DIRECTORY)
            with os.scandir(directory_fd) as iterator:
                for index, entry in enumerate(iterator):
                    if index >= MAX_IRQ_ENTRIES:
                        truncated = True
                        break
                    entries.append(entry.name)
        except OSError:
            return {
                "status": "error",
                "irqs_seen": 0,
                "affinity_read": 0,
                "malformed": 0,
                "unreadable": 0,
                "single_cpu": 0,
                "multi_cpu": 0,
                "cpu_usage": {},
                "outside_allowed": None,
                "source": source,
                "error": "directory_read_error",
            }
        finally:
            if directory_fd is not None:
                os.close(directory_fd)

        irq_names = sorted(name for name in entries if name.isdigit())
        usage: dict[int, int] = {}
        irqs_seen = len(irq_names)
        affinity_read = 0
        malformed = 0
        unreadable = 0
        single_cpu = 0
        multi_cpu = 0
        outside_allowed = 0 if allowed["status"] == "available" else None
        allowed_set = set(allowed["value"]) if allowed["status"] == "available" else set()

        for irq_name in irq_names:
            list_path = f"/proc/irq/{irq_name}/smp_affinity_list"
            mask_path = f"/proc/irq/{irq_name}/smp_affinity"
            value, read_error = self._read_text(list_path, 4096)
            cpus = None
            if read_error is None and value is not None:
                try:
                    cpus = self._parse_affinity_list(value)
                except ValueError:
                    malformed += 1
                    continue
            elif read_error == "not_found":
                mask_value, mask_error = self._read_text(mask_path, 4096)
                if mask_error is not None or mask_value is None:
                    unreadable += 1
                    continue
                try:
                    cpus = self._parse_affinity_mask(mask_value)
                except ValueError:
                    malformed += 1
                    continue
            else:
                unreadable += 1
                continue
            if not cpus:
                malformed += 1
                continue
            affinity_read += 1
            if len(cpus) == 1:
                single_cpu += 1
            else:
                multi_cpu += 1
            for cpu in cpus:
                if len(usage) < MAX_CPU_USAGE_ENTRIES or cpu in usage:
                    usage[cpu] = usage.get(cpu, 0) + 1
            if outside_allowed is not None and not set(cpus).issubset(allowed_set):
                outside_allowed += 1

        if malformed or unreadable:
            status = "partial"
        elif truncated:
            status = "partial"
        else:
            status = "available"
        return {
            "status": status,
            "irqs_seen": irqs_seen,
            "affinity_read": affinity_read,
            "malformed": malformed,
            "unreadable": unreadable,
            "single_cpu": single_cpu,
            "multi_cpu": multi_cpu,
            "cpu_usage": {str(cpu): usage[cpu] for cpu in sorted(usage)},
            "outside_allowed": outside_allowed,
            "source": source,
            "error": (
                "truncated" if truncated else
                ("malformed_affinity" if malformed else
                 ("unreadable_affinity" if unreadable else None))
            ),
        }

    def _collect_scheduler_config(self, release: str) -> dict:
        proc_path = "/proc/config.gz"
        proc_data, proc_error = self._read_bytes(proc_path, MAX_BINARY_BYTES)
        if proc_error is None and proc_data is not None:
            try:
                stream = gzip.GzipFile(fileobj=io.BytesIO(proc_data), mode="rb")
                decompressed = stream.read(MAX_BINARY_BYTES + 1)
                stream.close()
                if len(decompressed) > MAX_BINARY_BYTES:
                    raise ValueError("config_too_large")
            except (OSError, EOFError, ValueError, zlib.error):
                proc_fact = _error(proc_path, "invalid_gzip")
            else:
                proc_fact = _fact("available", True, proc_path, None)
        else:
            proc_fact = _unknown(proc_path, proc_error or "not_found")

        boot_path = f"/boot/config-{release}" if release != "unknown" else None
        boot_fact = None
        if boot_path is None:
            boot_fact = _unknown("/boot/config-<uname.release>", "uname_unavailable")
        else:
            boot_data, boot_error = self._read_bytes(boot_path, MAX_BINARY_BYTES)
            if boot_error is None and boot_data is not None:
                boot_fact = _fact("available", True, boot_path, None)
            else:
                # Some stripped images retain a generic /boot/config file.
                generic_path = "/boot/config"
                generic_data, generic_error = self._read_bytes(generic_path, MAX_BINARY_BYTES)
                if generic_error is None and generic_data is not None:
                    boot_fact = _fact("available", True, generic_path, None)
                else:
                    boot_fact = _unknown(boot_path, boot_error or generic_error or "not_found")

        if proc_fact["status"] == "available" or boot_fact["status"] == "available":
            status = "available"
            source = proc_fact["source"] if proc_fact["status"] == "available" else boot_fact["source"]
            error = None
        elif proc_fact["status"] == "error" or boot_fact["status"] == "error":
            status = "error"
            source = proc_path
            error = proc_fact["error"] if proc_fact["status"] == "error" else boot_fact["error"]
        else:
            status = "unknown"
            source = None
            error = "not_found"
        return {
            "status": status,
            "proc_config_gz": proc_fact,
            "boot_config": boot_fact,
            "source": source,
            "error": error,
        }

    def _collect_cap_sys_nice(self) -> dict:
        path = "/proc/self/status"
        value, error = self._read_text(path, MAX_TEXT_BYTES)
        if error is not None:
            return _unknown(path + ":CapEff", error)
        assert value is not None
        cap_eff = None
        for line in value.splitlines():
            if line.startswith("CapEff:"):
                cap_eff = line.split(":", 1)[1].strip()
                break
        if cap_eff is None:
            return _unknown(path + ":CapEff", "field_absent")
        if not re.fullmatch(r"[0-9a-fA-F]+", cap_eff):
            return _error(path + ":CapEff", "malformed_capability")
        try:
            effective = int(cap_eff, 16)
        except ValueError:
            return _error(path + ":CapEff", "malformed_capability")
        return _fact("available", bool(effective & (1 << 23)), path + ":CapEff", None)

    def collect(self) -> dict:
        uname = self._collect_uname()
        cpu = self._collect_cpu()
        allowed = cpu["allowed"]
        return {
            "schema": SCHEMA,
            "collection_mode": ("fixture_filesystem" if self.fixture_mode else
                                "diagnostic_override" if self._allowed_override is not None else
                                "live_host"),
            "uname": uname,
            "virtualization": self._collect_virtualization(),
            "realtime": self._collect_realtime(),
            "cpu": cpu,
            "cmdline": self._collect_cmdline(),
            "cgroup": self._collect_cgroup(),
            "governor": self._collect_governor(allowed),
            "irq_affinity": self._collect_irq_affinity(allowed),
            "scheduler_config": self._collect_scheduler_config(uname["release"]),
            "cap_sys_nice": self._collect_cap_sys_nice(),
        }


def _write_report(report: dict, output: Optional[str], pretty: bool) -> None:
    if pretty:
        encoded = json.dumps(report, ensure_ascii=True, indent=2, sort_keys=False) + "\n"
    else:
        encoded = json.dumps(report, ensure_ascii=True, separators=(",", ":"), sort_keys=False) + "\n"
    if output is None:
        sys.stdout.write(encoded)
        return
    # mode='x' is intentional: a preflight report must never replace an
    # existing artifact, including a symlink or hard-linked path.
    with open(output, "x", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Collect read-only XTOS scheduler preflight facts")
    parser.add_argument("--root", default="/", help="filesystem root for diagnostics or fixtures")
    parser.add_argument("--output", help="create this JSON report path exclusively")
    parser.add_argument("--pretty", action="store_true", help="indent the JSON report")
    args = parser.parse_args(argv)
    try:
        report = Collector(args.root).collect()
        _write_report(report, args.output, args.pretty)
    except (OSError, ValueError, TypeError) as exc:
        print(f"preflight: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
