#!/usr/bin/env python3
"""Fixture-only tests for the native cgroup cpuset partition slice.

The test compiles cgroup.c with a compile-time fixture root and a test-only
hierarchy hook. It never opens or mutates the host cgroup hierarchy.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "xtos-native" / "cgroup" / "cgroup.c"
QA = "xtos-cgroup-qa"


def write(path: Path, content: str) -> None:
    path.write_text(content)


def prepare_child(
    root: Path,
    name: str,
    effective: str | None = "0-3",
    cpus: str = "",
    mems: str = "",
    partition: str = "member",
) -> Path:
    child = root / name
    child.mkdir()
    write(child / "cpuset.cpus", cpus + "\n")
    if effective is not None:
        write(child / "cpuset.cpus.effective", effective + "\n")
    write(child / "cpuset.mems", mems + "\n")
    write(child / "cpuset.cpus.partition", partition + "\n")
    write(child / "cgroup.procs", "")
    return child


def compile_binary(td: Path, fixture: Path, suffix: str, *extra: str) -> Path:
    binary = td / f"cgroup-{suffix}"
    root_define = f'-DXTOS_CGROUP_ROOT="{fixture}"'
    subprocess.run(
        [
            "gcc",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-static",
            root_define,
            "-DXTOS_CGROUP_TEST_MODE=1",
            *extra,
            "-I",
            str(ROOT / "tools" / "xtos-native" / "common"),
            "-o",
            str(binary),
            str(SRC),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return binary


def compile_range_probe(td: Path) -> Path:
    binary = td / "cgroup-range-probe"
    source = f'''\
#define main cgroup_tool_main
#include "{SRC}"
#undef main

int main(void) {{
    printf("%d %d %d %d %d %d %d\\n",
           cpu_lists_equal("0-1", "0,1"),
           cpu_lists_equal("0-3", "0-1,2-3"),
           cpu_lists_equal("3,1-2,2-3,0", "0-3"),
           cpu_lists_equal("0-1,1-3", "0-3"),
           cpu_list_subset("0-3", "0,1,2,3"),
           cpu_list_subset("0-1,3", "0-3"),
           cpu_list_subset("0-4", "0-3"));
    char maximum[64], whole[128], split[128], overflow[128];
    snprintf(maximum, sizeof(maximum), "%lu", ULONG_MAX);
    snprintf(whole, sizeof(whole), "0-%lu", ULONG_MAX);
    snprintf(split, sizeof(split), "0-%lu,%lu", ULONG_MAX - 1, ULONG_MAX);
    snprintf(overflow, sizeof(overflow), "%s0", maximum);
    printf("%d %d %d %d %d\\n",
           cpu_lists_equal(whole, split),
           cpu_list_subset(maximum, whole),
           cpu_list_subset("0-3", "0-1,3"),
           valid_cpu_list(overflow),
           valid_cpu_list("3-1"));
    return 0;
}}
'''
    subprocess.run(
        [
            "gcc",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-static",
            "-I",
            str(ROOT / "tools" / "xtos-native" / "common"),
            # The included CLI is renamed in this probe translation unit;
            # avoid unrelated GCC path-inlining warnings without disabling them.
            "-fno-inline",
            "-x",
            "c",
            "-o",
            str(binary),
            "-",
        ],
        input=source,
        check=True,
        capture_output=True,
        text=True,
    )
    return binary


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory(prefix="xtos-cgroup-fixture-") as td_name:
        td = Path(td_name)
        fixture = td / "cgroup"
        fixture.mkdir()
        # fixture-only is a guard: a build that ignores the compile-time root
        # must stop before any command that could touch /sys/fs/cgroup.
        write(fixture / "cgroup.controllers", "cpuset fixture-only\n")
        write(fixture / "cgroup.subtree_control", "cpuset\n")
        write(fixture / "cpuset.cpus.effective", "0-3\n")
        write(fixture / "cpuset.mems.effective", "0\n")
        child = prepare_child(fixture, QA)

        range_probe = compile_range_probe(td)
        range_result = subprocess.run(
            [str(range_probe)], capture_output=True, text=True, check=False, timeout=10
        )
        expected_ranges = ["1", "1", "1", "1", "1", "1", "0", "1", "1", "0", "0", "0"]
        if range_result.returncode != 0 or range_result.stdout.split() != expected_ranges:
            failures.append(
                "CPU range set semantics: "
                f"rc={range_result.returncode} stdout={range_result.stdout!r} "
                f"stderr={range_result.stderr!r} expected={expected_ranges!r}"
            )
        else:
            print("  PASS CPU range set semantics")

        binary = compile_binary(td, fixture, "normal")

        def run(binary_path: Path, *args: str, expect: int = 0):
            result = subprocess.run(
                [str(binary_path), *args],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != expect:
                failures.append(
                    f"{' '.join(args)}: rc={result.returncode} expected {expect}\n"
                    f"stdout={result.stdout}stderr={result.stderr}"
                )
            return result

        # Guard the fixture root before invoking any mutating command.
        status = run(binary, "status", "--json")
        try:
            data = json.loads(status.stdout)
            assert data["mounted"] is True
            assert "fixture-only" in data["controllers"]
            assert data["root"]["partition"] == "root"
            assert data["root"]["effective"] == "0-3"
            print("  PASS compile-time fixture root")
        except Exception as exc:
            failures.append(
                f"fixture root guard failed: {exc}: stdout={status.stdout!r}"
            )
            print("  STOP fixture root guard failed; no host mutation attempted")
            print(f"FAIL: {len(failures)}")
            for failure in failures:
                print("  " + failure)
            return 1

        # Default must remain a member; it must not create an isolated root.
        result = run(binary, "partition", QA, "--cpus", "1")
        if result.returncode == 0:
            if child.joinpath("cpuset.cpus").read_text().strip() != "1":
                failures.append("default partition did not write cpuset.cpus")
            if child.joinpath("cpuset.cpus.partition").read_text().strip() != "member":
                failures.append("default partition did not read back member")
            if "parent_effective=0-3" not in result.stdout:
                failures.append(f"parent effective missing from output: {result.stdout!r}")
            if "partition=member" not in result.stdout:
                failures.append(f"member type missing from output: {result.stdout!r}")
            else:
                print("  PASS default member write/readback")

        # A missing effective read must fail before the partition type changes.
        missing_binary = compile_binary(
            td,
            fixture,
            "missing-effective",
            "-DXTOS_CGROUP_TEST_MISSING_EFFECTIVE=1",
        )
        missing_existing = prepare_child(
            fixture, "missing-effective", effective=None, cpus="1", partition="member"
        )
        missing_result = run(
            missing_binary,
            "partition",
            "missing-effective",
            "--cpus",
            "1",
            "--partition",
            "root",
            expect=1,
        )
        if missing_existing.joinpath("cpuset.cpus.partition").read_text().strip() != "member":
            failures.append("missing-effective changed an existing child partition")
        else:
            print("  PASS missing effective preserves member state")

        fresh_missing = fixture / "missing-effective-new"
        run(
            missing_binary,
            "partition",
            fresh_missing.name,
            "--cpus",
            "1",
            "--partition",
            "root",
            expect=1,
        )
        if fresh_missing.exists():
            failures.append("missing-effective request left a newly created child directory")
        else:
            print("  PASS missing effective does not leave new child")

        # Preserve a fixture's effective value across the CPU write to exercise
        # missing/mismatched reads independently of the ordinary echo hook.
        keep_effective_binary = compile_binary(
            td, fixture, "keep-effective", "-DXTOS_CGROUP_TEST_KEEP_EFFECTIVE=1"
        )
        mismatched = prepare_child(
            fixture, "mismatch-effective", effective="0", cpus="1"
        )
        run(
            keep_effective_binary, "partition", mismatched.name,
            "--cpus", "1", "--partition=root", expect=1,
        )
        if mismatched.joinpath("cpuset.cpus.partition").read_text().strip() != "member":
            failures.append("mismatched effective changed partition before preflight")

        unreadable = prepare_child(fixture, "unreadable-effective", cpus="1")
        unreadable_effective = unreadable / "cpuset.cpus.effective"
        unreadable_effective.unlink()
        unreadable_effective.mkdir()  # read(2) fails with EISDIR, including as root.
        run(
            keep_effective_binary, "partition", unreadable.name,
            "--cpus", "1", "--partition=root", expect=1,
        )
        if unreadable.joinpath("cpuset.cpus.partition").read_text().strip() != "member":
            failures.append("unreadable effective changed partition before preflight")

        # The kernel canonicalizes both requested CPU and memory-node masks.
        canonical_binary = compile_binary(
            td, fixture, "canonical", "-DXTOS_CGROUP_TEST_CANONICAL_READBACK=1"
        )
        canonical_child = prepare_child(fixture, "canonical")
        write(fixture / "cpuset.mems.effective", "0,1\n")
        run(
            canonical_binary, "partition", canonical_child.name,
            "--cpus", "0,1", "--mems", "0,1", "--partition=root",
        )
        for control in ("cpuset.cpus", "cpuset.mems", "cpuset.cpus.effective"):
            if canonical_child.joinpath(control).read_text().strip() != "0-1":
                failures.append(f"canonical {control} readback fixture was not applied")
        write(fixture / "cpuset.mems.effective", "0\n")

        # A post-type read must still be checked. For a leaf it must equal the
        # request; a root with a valid child partition excludes that child's CPUs.
        post_binary = compile_binary(
            td, fixture, "post-effective", '-DXTOS_CGROUP_TEST_POST_EFFECTIVE="0-1"'
        )
        post_leaf = prepare_child(fixture, "post-mismatch")
        run(
            post_binary, "partition", post_leaf.name,
            "--cpus", "0-3", "--partition=root", expect=1,
        )
        for partition_type in ("root", "isolated"):
            post_root = prepare_child(fixture, f"post-{partition_type}")
            prepare_child(post_root, "descendant", effective="2-3", cpus="2-3", partition="root")
            run(
                post_binary, "partition", post_root.name,
                "--cpus", "0-3", f"--partition={partition_type}",
            )

        # Explicit root and isolated types are accepted and read back.
        run(binary, "partition", QA, "--cpus", "2", "--partition=root")
        if child.joinpath("cpuset.cpus.partition").read_text().strip() != "root":
            failures.append("root partition type not written")
        else:
            print("  PASS explicit root partition")
        run(binary, "partition", QA, "--cpus", "3", "--partition", "isolated")
        if child.joinpath("cpuset.cpus.partition").read_text().strip() != "isolated":
            failures.append("isolated partition type not written")
        else:
            print("  PASS explicit isolated partition")

        # Requested CPUs must be within the parent's effective set.
        before = child.joinpath("cpuset.cpus").read_text()
        run(binary, "partition", QA, "--cpus", "4", expect=1)
        if child.joinpath("cpuset.cpus").read_text() != before:
            failures.append("out-of-parent CPU request changed child cpuset")
        else:
            print("  PASS parent effective subset validation")
        run(binary, "partition", QA, "--cpus", "2-,5", expect=1)

        # Existing partition roots may retain CPUs removed from the parent's
        # effective set. They can be reconfigured with those already-owned CPUs,
        # but a new child must not receive them.
        parent_effective = fixture / "cpuset.cpus.effective"
        saved_parent_effective = parent_effective.read_text()
        write(parent_effective, "0-1,3\n")
        existing_member = prepare_child(
            fixture, "existing-root-to-member", effective="2", cpus="2", partition="root"
        )
        existing_root = prepare_child(
            fixture, "existing-root-repeat", effective="2", cpus="2", partition="root"
        )
        existing_isolated = prepare_child(
            fixture, "existing-isolated-repeat", effective="2", cpus="2", partition="isolated"
        )
        run(
            binary,
            "partition",
            existing_member.name,
            "--cpus",
            "2",
            "--partition",
            "member",
        )
        run(
            binary,
            "partition",
            existing_root.name,
            "--cpus",
            "2",
            "--partition=root",
        )
        run(
            binary,
            "partition",
            existing_isolated.name,
            "--cpus",
            "2",
            "--partition",
            "isolated",
        )
        for existing, expected in (
            (existing_member, "member"),
            (existing_root, "root"),
            (existing_isolated, "isolated"),
        ):
            if existing.joinpath("cpuset.cpus.partition").read_text().strip() != expected:
                failures.append(
                    f"existing {expected} reconfiguration did not complete for {existing.name}"
                )
        new_outside = fixture / "new-outside-existing-owner"
        run(binary, "partition", new_outside.name, "--cpus", "2", expect=1)
        if new_outside.exists():
            failures.append("new child received CPU outside parent effective set")
        elif all(
            existing.joinpath("cpuset.cpus.partition").read_text().strip() == expected
            for existing, expected in (
                (existing_member, "member"),
                (existing_root, "root"),
                (existing_isolated, "isolated"),
            )
        ):
            print("  PASS existing partition CPU ownership reconfiguration")
        write(parent_effective, saved_parent_effective)

        # All parent CPUs may have been delegated already. An empty *valid*
        # parent effective mask must not prevent its existing owner demotion.
        write(parent_effective, "\n")
        empty_parent_owner = prepare_child(
            fixture, "empty-parent-owner", effective="2", cpus="2", partition="root"
        )
        run(
            binary, "partition", empty_parent_owner.name,
            "--cpus", "2", "--partition=member",
        )
        if empty_parent_owner.joinpath("cpuset.cpus.partition").read_text().strip() != "member":
            failures.append("empty parent effective mask prevented existing owner demotion")
        empty_parent_new = fixture / "empty-parent-new"
        run(binary, "partition", empty_parent_new.name, "--cpus", "2", expect=1)
        if empty_parent_new.exists():
            failures.append("empty parent effective mask allowed an unowned new child")
        write(parent_effective, saved_parent_effective)

        # The root cgroup has no partition file; missing parent effective CPUs
        # must be a hard error rather than a guessed/all-CPU fallback.
        root_effective = fixture / "cpuset.cpus.effective"
        saved_effective = root_effective.read_text()
        root_effective.unlink()
        missing = run(binary, "partition", "missing-parent", "--cpus", "1", expect=1)
        if (fixture / "missing-parent").exists():
            failures.append("partition was created without parent effective CPUs")
        if "parent" not in (missing.stderr + missing.stdout).lower():
            failures.append("missing parent error did not name the parent constraint")
        else:
            print("  PASS root-parent constraint")
        write(root_effective, saved_effective)

        # Status must expose the kernel-style invalid state caused by hotplug
        # or another external topology change.
        write(child / "cpuset.cpus.partition", "isolated invalid (hotplug)\n")
        status = run(binary, "status", "--json")
        try:
            data = json.loads(status.stdout)
            entry = next(p for p in data["partitions"] if p["name"] == QA)
            assert entry["partition"] == "isolated invalid (hotplug)"
            assert entry["effective"] == "3"
            print("  PASS status partition type/effective JSON")
        except Exception as exc:
            failures.append(f"status partition JSON: {exc}: {status.stdout!r}")

        # A failed partition-file write must fail instead of reporting success.
        failed = prepare_child(fixture, "failed-write")
        (failed / "cpuset.cpus.partition").unlink()
        (failed / "cpuset.cpus.partition").mkdir()
        run(binary, "partition", "failed-write", "--cpus", "1", expect=1)
        print("  PASS partition write failure")

        # Compile a second fixture-only binary whose test hook shortens the
        # partition readback; the command must reject the mismatch.
        short_binary = compile_binary(
            td,
            fixture,
            "short-readback",
            "-DXTOS_CGROUP_TEST_SHORT_PARTITION_READBACK=1",
        )
        short_child = prepare_child(fixture, "short-readback")
        short_result = run(short_binary, "partition", "short-readback", "--cpus", "1", expect=1)
        if "readback" not in (short_result.stderr + short_result.stdout).lower():
            failures.append("short partition readback error was not reported")
        else:
            print("  PASS short partition readback failure")

        # pin: a /proc/<pid>/cgroup read failure must fail closed instead of
        # skipping the confirmation and still printing "confirmed".
        pin_child = prepare_child(fixture, "qa-pin")
        own_pid = str(os.getpid())
        pin_fail_binary = compile_binary(
            td,
            fixture,
            "pin-readback-fail",
            "-DXTOS_CGROUP_TEST_PIN_READBACK_FAIL=1",
        )
        pin_fail = run(pin_fail_binary, "pin", "qa-pin", own_pid, expect=1)
        if "confirmed" in pin_fail.stdout:
            failures.append("pin readback failure still printed confirmed")
        elif pin_child.joinpath("cgroup.procs").read_text().strip() != own_pid:
            failures.append("pin readback-failure run did not record the pid write")
        else:
            print("  PASS pin readback failure fails closed")

        # pin: a mismatching real /proc readback must fail closed too (the
        # fixture name can never be this process's actual unified cgroup path).
        pin_mismatch = run(binary, "pin", "qa-pin", own_pid, expect=1)
        if "not confirmed" not in (pin_mismatch.stderr + pin_mismatch.stdout):
            failures.append("pin mismatch readback error was not reported")
        else:
            print("  PASS pin mismatch readback fails closed")

        # Usage validation remains focused and side-effect free.
        run(binary, "partition", "--cpus", "1", expect=2)
        run(binary, "partition", QA, "--cpus", "1", "--partition=bogus", expect=2)
        run(binary, "bogus", expect=2)
        print("  PASS usage validation")

    if failures:
        print(f"FAIL: {len(failures)}")
        for failure in failures:
            print("  " + failure)
        return 1
    print("ALL PASS (fixture-only; host cgroup hierarchy untouched)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
