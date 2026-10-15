#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for release-only license/source packaging helpers."""
from __future__ import annotations

import contextlib
import io
import re
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import collect_distribution_notices as collector
from enable_deb_src import deb822_source_stanzas, list_source_lines
from install_project_notices import install

REPOSITORY = SCRIPT_DIR.parent


class AptSourceConversionTests(unittest.TestCase):
    def test_explicit_ubuntu_file_excludes_third_party_repositories(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openxtos-apt-test-") as temp:
            folder = Path(temp)
            official = folder / "ubuntu.sources"
            official.write_text("Types: deb\nURIs: http://archive.ubuntu.com/ubuntu\nSuites: noble\nComponents: main\n")
            (folder / "third-party.list").write_text("deb https://packages.example.invalid stable main\n")
            result = subprocess.run([sys.executable, str(SCRIPT_DIR / "enable_deb_src.py"), str(official)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = (folder / "openxtos-deb-src.sources").read_text()
            self.assertIn("archive.ubuntu.com", output)
            self.assertNotIn("packages.example.invalid", output)

    def test_deb822_mirrors_deb_stanza_and_preserves_signed_by(self) -> None:
        source = """Types: deb
URIs: https://mirror.example/debian
Suites: stable stable-updates
Components: main contrib
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
"""
        result = deb822_source_stanzas(source)
        self.assertEqual(len(result), 1)
        self.assertIn("Types: deb-src", result[0])
        self.assertIn("Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg", result[0])
        self.assertNotIn("Types: deb\n", result[0])

    def test_deb822_skips_existing_source_and_non_deb_types(self) -> None:
        source = """Types: deb deb-src
URIs: https://mirror.example/debian
Suites: stable
Components: main

Types: deb
URIs: https://mirror.example/debian
Suites: stable
Components: main
"""
        result = deb822_source_stanzas(source)
        self.assertEqual(len(result), 1)
        self.assertIn("URIs: https://mirror.example/debian", result[0])

    def test_legacy_conversion_skips_comments_and_existing_deb_src(self) -> None:
        source = """# commented repository
deb [arch=amd64 signed-by=/key.gpg] https://mirror.example/debian stable main contrib
deb-src https://mirror.example/debian stable main
"""
        self.assertEqual(
            list_source_lines(source),
            ["deb-src [arch=amd64 signed-by=/key.gpg] https://mirror.example/debian stable main contrib"],
        )


class ProjectNoticeTests(unittest.TestCase):
    def test_common_license_absolute_references_resolve_inside_rootfs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openxtos-notices-test-") as temp:
            rootfs = Path(temp) / "rootfs"
            install(REPOSITORY, rootfs)
            self.assertTrue((rootfs / "usr/share/common-licenses/GPL-3").is_file())
            self.assertTrue((rootfs / "usr/share/common-licenses/LGPL-2.1").is_file())

    def test_installs_project_and_component_licenses_with_tag_source_offer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="openxtos-notices-test-") as temp:
            docroot = install(REPOSITORY, Path(temp) / "rootfs")
            for name in (
                "LICENSE", "LICENSE-NOTICE", "NOTICE", "TRADEMARKS.md",
                "linux-COPYING", "GPL-2.0", "Linux-syscall-note", "busybox-LICENSE", "CORRESPONDING-SOURCE.txt",
            ):
                self.assertTrue((docroot / name).is_file(), name)
            offer = (docroot / "CORRESPONDING-SOURCE.txt").read_text()
            match = re.search(r"^XTOS_VERSION=(.+)$", (REPOSITORY / "VERSION").read_text(), re.M)
            assert match is not None, "VERSION is missing XTOS_VERSION"
            version = match.group(1).strip().strip('"')
            self.assertIn(f"refs/tags/v{version}.tar.gz", offer)
            self.assertIn("x86_64-corresponding-source.tar.gz", offer)


class DistributionOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="openxtos-ownership-test-")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.rootfs = self.folder / "rootfs"
        (self.rootfs / "usr/bin").mkdir(parents=True)
        (self.rootfs / "usr/bin/sh").write_text("packaged shell")
        (self.rootfs / "usr/bin/xtos-built").write_text("project-built executable")
        self.docroot = self.rootfs / "usr/share/doc/openxtos"
        self.docroot.mkdir(parents=True)
        self.manifest = self.folder / "host-files.txt"
        self.owners = {"/usr/bin/sh": "dash"}
        self.metadata = {"dash": "dash\tdash\t1.0\t1.0\n"}
        self.built_using: dict[str, str] = {}
        self.static_built_using: dict[str, str] = {}
        self.addCleanup(mock.patch.stopall)
        self.install_notices = mock.patch.object(collector, "install_project_notices", return_value=self.docroot).start()
        mock.patch.object(collector.subprocess, "run", side_effect=self.query).start()

    def query(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[1] == "-S":
            paths = args[3:]
            output = "".join(f"{self.owners[path]}: {path}\n" for path in paths if path in self.owners)
            missing = [path for path in paths if path not in self.owners]
            errors = "".join(f"dpkg-query: no path found matching pattern {path}\n" for path in missing)
            return subprocess.CompletedProcess(args, int(bool(missing)), output, errors)
        self.assertEqual(args[1], "-W")
        owner = args[-1]
        output = self.metadata[owner]
        if "${Built-Using}" in args[2]:
            output = output.removesuffix("\n") + "\t" + self.built_using.get(owner, "") + "\t" + self.static_built_using.get(owner, "") + "\n"
        return subprocess.CompletedProcess(args, 0, output, "")

    def collect(self, paths: list[Path] | None = None) -> None:
        args = [str(SCRIPT_DIR / "collect_distribution_notices.py"), str(REPOSITORY), str(self.rootfs)]
        if paths is not None:
            self.manifest.write_text("".join(f"{path}\n" for path in paths))
            args.append(str(self.manifest))
        with mock.patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
            collector.main()

    def test_unowned_project_built_rootfs_files_remain_permissive(self) -> None:
        self.collect()
        self.assertIn("dash\t1.0", (self.docroot / "HOST_PACKAGES.tsv").read_text())

    def test_explicit_missing_or_unowned_host_executable_fails(self) -> None:
        path = self.folder / "unowned-executable"
        for exists in (False, True):
            with self.subTest(exists=exists):
                if exists:
                    path.write_text("#!/bin/sh\n")
                    path.chmod(0o755)
                with self.assertRaisesRegex(SystemExit, str(path)):
                    self.collect([path])

    def test_explicit_directory_checks_nested_copied_files(self) -> None:
        directory = self.folder / "grub"
        nested = directory / "i386-pc"
        nested.mkdir(parents=True)
        (nested / "owned.mod").write_text("packaged module")
        unowned = nested / "unowned.mod"
        unowned.write_text("local module")
        for path in (directory, nested, nested / "owned.mod"):
            self.owners[str(path)] = "dash"
        with self.assertRaisesRegex(SystemExit, str(unowned)):
            self.collect([directory])

    def test_explicit_owner_rejects_failed_or_malformed_metadata(self) -> None:
        path = self.folder / "host-tool"
        path.write_text("host executable")
        self.owners[str(path)] = "broken-package"
        bad_metadata = (
            "broken-package\tbroken-source\t1.0\n",
            "\tbroken-source\t1.0\t1.0\n",
            "broken-package\tbroken-source\t1.0\t\n",
            "broken-package\t\t1.0\t1.0\n",
            "broken-package\tbroken-source\t\t1.0\n",
            "broken-package\tinvalid source\t1.0\t1.0\n",
            "broken-package\tbroken-source\tnot-a-version\t1.0\n",
        )
        for output in bad_metadata:
            with self.subTest(output=output):
                self.metadata["broken-package"] = output
                with self.assertRaisesRegex(SystemExit, "metadata.*broken-package"):
                    self.collect([path])
        original = self.query

        def failed_metadata(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[1] == "-W" and args[-1] == "broken-package":
                return subprocess.CompletedProcess(args, 2, "", "database error")
            return original(args, **kwargs)

        with mock.patch.object(collector.subprocess, "run", side_effect=failed_metadata):
            with self.assertRaisesRegex(SystemExit, "broken-package.*metadata"):
                self.collect([path])

    def test_explicit_inventory_excludes_unrelated_rootfs_owners(self) -> None:
        path = self.folder / "cross-libc.a"
        path.write_text("cross compiler runtime")
        self.owners[str(path)] = "cross-runtime"
        self.metadata["cross-runtime"] = "cross-runtime\tcross-toolchain\t1.0\t1.0\n"
        real_is_file = Path.is_file

        def is_file(candidate: Path) -> bool:
            if candidate == Path("/usr/share/doc/cross-runtime/copyright"):
                return True
            return real_is_file(candidate)

        with mock.patch.object(Path, "is_file", is_file), mock.patch.object(collector.shutil, "copy2"):
            self.collect([path])
        inventory = (self.docroot / "HOST_PACKAGES.tsv").read_text()
        self.assertIn("cross-runtime", inventory)
        self.assertNotIn("dash", inventory)

    def test_explicit_ownership_query_errors_are_fatal(self) -> None:
        path = self.folder / "host-tool"
        path.write_text("host executable")
        self.owners[str(path)] = "dash"
        original = self.query

        def broken_query(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            result = original(args, **kwargs)
            if args[1] == "-S":
                return subprocess.CompletedProcess(args, 2, result.stdout, "dpkg database error")
            return result

        with mock.patch.object(collector.subprocess, "run", side_effect=broken_query):
            with self.assertRaisesRegex(SystemExit, "dpkg.*ownership.*error"):
                self.collect([path])

    def test_arm64_artifact_is_passed_to_project_notices(self) -> None:
        self.manifest.write_text("/usr/bin/sh\n")
        args = ["collector", str(REPOSITORY), str(self.rootfs), str(self.manifest), "arm64 bundle"]
        with mock.patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
            collector.main()
        self.install_notices.assert_called_once_with(REPOSITORY, self.rootfs, "arm64 bundle")

    def test_usrmerge_and_executable_symlink_aliases_are_accepted(self) -> None:
        for owner_path in ("/usr/bin/sh", "/bin/sh", "/bin/dash"):
            with self.subTest(owner_path=owner_path):
                self.owners = {owner_path: "dash"}
                self.collect([Path("/usr/bin/sh"), Path("/usr/bin/sh").resolve()])
                self.assertIn("dash\t1.0", (self.docroot / "HOST_PACKAGES.tsv").read_text())

    def test_directory_symlinks_include_nested_target_files(self) -> None:
        directory = self.folder / "grub"
        directory.mkdir()
        target = self.folder / "modules"
        target.mkdir()
        unowned = target / "unowned.mod"
        unowned.write_text("local module")
        link = directory / "platform"
        link.symlink_to(target, target_is_directory=True)
        self.owners.update({str(directory): "dash", str(link): "dash", str(target): "dash"})
        with self.assertRaisesRegex(SystemExit, "unowned.mod"):
            self.collect([directory])

    def test_built_using_sources_include_exact_versions_and_responsible_binary(self) -> None:
        path = self.folder / "host-tool"
        path.write_text("host executable")
        self.owners[str(path)] = "dash"
        self.built_using["dash"] = "lzo2 (= 2.10-3), glibc (= 2.41-11)"
        self.static_built_using["dash"] = "gcc-14 (= 14.2.0-19), lzo2 (= 2.10-3)"
        self.collect([path])
        inventory = (self.docroot / "SOURCE_PACKAGES.tsv").read_text().splitlines()
        self.assertEqual(inventory, [
            "source_package\tsource_version\tbinary_packages",
            "dash\t1.0\tdash",
            "gcc-14\t14.2.0-19\tdash",
            "glibc\t2.41-11\tdash",
            "lzo2\t2.10-3\tdash",
        ])

    def test_malformed_built_using_metadata_fails_closed(self) -> None:
        path = self.folder / "host-tool"
        path.write_text("host executable")
        self.owners[str(path)] = "dash"
        for field in (self.built_using, self.static_built_using):
            for value in ("lzo2", "lzo2 (>= 2.10-3)", "lzo2 (= )", "lzo2 (= 2.10-3),", "lzo2:any (= 2.10-3)"):
                with self.subTest(field=field is self.built_using, value=value):
                    field["dash"] = value
                    with self.assertRaisesRegex(SystemExit, "malformed.*Built-Using.*dash"):
                        self.collect([path])
            field.clear()

    def test_relative_manifest_entry_is_not_silently_skipped(self) -> None:
        with self.assertRaisesRegex(SystemExit, "relative-host-file"):
            self.collect([Path("/usr/bin/sh"), Path("relative-host-file")])


    def test_missing_manifest_fails_instead_of_falling_back_to_rootfs(self) -> None:
        args = ["collector", str(REPOSITORY), str(self.rootfs), str(self.manifest)]
        with mock.patch.object(sys, "argv", args):
            with self.assertRaises(FileNotFoundError):
                collector.main()

    def test_directory_symlink_cycle_fails_without_unbounded_traversal(self) -> None:
        directory = self.folder / "grub"
        directory.mkdir()
        (directory / "cycle").symlink_to(directory, target_is_directory=True)
        with self.assertRaisesRegex(SystemExit, "symlink cycle"):
            self.collect([directory])


    def test_strict_inventory_ignores_unrequested_ownership_matches(self) -> None:
        path = self.folder / "host-tool"
        path.write_text("host executable")
        self.owners[str(path)] = "dash"
        self.metadata["bash"] = "bash\tbash\t1.0\t1.0\n"
        original = self.query

        def extra_match(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            result = original(args, **kwargs)
            if args[1] == "-S":
                result.stdout += "bash: /usr/lib/native-only.so\n"
            return result

        with mock.patch.object(collector.subprocess, "run", side_effect=extra_match):
            self.collect([path])
        self.assertNotIn("bash", (self.docroot / "HOST_PACKAGES.tsv").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
