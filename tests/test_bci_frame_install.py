"""Exercise SDK packaging from fresh copies, including installed-only clients."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "sdk/libxtos-bci"
HEADERS = {
    "bci.h", "bci_uapi.h", "bci_config_uapi.h", "bci_snapshot_uapi.h",
    "bci_frame_uapi.h", "bci_version.h",
}
BINARIES = {"test_samples", "test_snapshot", "test_snapshot_uapi", "test_config", "test_frame"}
FRAME_SYMBOLS = {
    "xtos_bci_frame_read", "xtos_bci_frame_snapshot_acquire",
    "xtos_bci_frame_snapshot_consume", "xtos_bci_frame_snapshot_release",
}
CLIENT = r'''#include <xtos/bci.h>
#include <xtos/bci_version.h>
#include <errno.h>
#include <stddef.h>
#include <stdio.h>
_Static_assert(sizeof(struct xtos_bci_config_v1) == 384, "config ABI");
_Static_assert(sizeof(struct xtos_bci_snapshot_header) == 64, "snapshot ABI");
_Static_assert(sizeof(struct xtos_bci_frame_v1) == 224, "frame ABI");
_Static_assert(sizeof(struct xtos_bci_frame_snapshot_header) == 128, "frame header ABI");
_Static_assert(offsetof(struct xtos_bci_frame_v1, samples_nv) == 96, "frame samples");
#define INVALID(call) do { errno = 0; if ((call) != -1 || errno != EINVAL) return __LINE__; } while (0)
int main(void)
{
    struct xtos_bci_config_v1 config = {0};
    struct xtos_bci_snapshot snapshot = {0};
    struct xtos_bci_frame_batch batch = {0};
    struct xtos_bci_frame_snapshot frame_snapshot = {0};
    INVALID(xtos_bci_frame_read(NULL, 1, 0, &batch));
    INVALID(xtos_bci_frame_snapshot_acquire(NULL, 1, &frame_snapshot));
    INVALID(xtos_bci_frame_snapshot_consume(NULL));
    INVALID(xtos_bci_frame_snapshot_release(NULL));
    INVALID(xtos_bci_get_config(NULL, &config));
    INVALID(xtos_bci_snapshot_acquire(NULL, 4, &snapshot));
    INVALID(xtos_bci_snapshot_consume(NULL));
    INVALID(xtos_bci_snapshot_release(NULL));
    puts("installed_frame_client=pass null_api_cases=8");
    return 0;
}
'''


class FrameInstallTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("TMPDIR", "/root/.hermes/cache/scratch"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="bci-frame-install-", dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.tree = self.work / "sdk"
        shutil.copytree(SDK, self.tree, ignore=shutil.ignore_patterns(
            "*.o", "*.a", "*.so*", "__pycache__", *BINARIES))
        # The legacy fixture alone uses /tmp. Rewrite only the copied pathname.
        fixture = self.tree / "tests/test_samples.c"
        fixture.write_text(fixture.read_text().replace(
            "/tmp/xtos-bci-samples-XXXXXX", str(self.work / "samples-XXXXXX")))
        self.env = os.environ.copy()
        for name in ("CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "LIBRARY_PATH",
                     "LD_LIBRARY_PATH", "LD_PRELOAD"):
            self.env.pop(name, None)
        self.cc = shlex.split(os.environ.get("CC", "cc"))
        if os.environ.get("BCI_SDK_SANITIZE") == "1":
            self.cc += ["-fsanitize=address,undefined", "-fno-sanitize-recover=all",
                        "-fno-omit-frame-pointer", "-no-pie"]
        self.make = ["make", "CC=" + shlex.join(self.cc),
                     "CFLAGS=-std=c11 -Wall -Wextra -Werror -O1 -g -fPIC -I./include"]

    def run_checked(self, command, cwd=None):
        result = subprocess.run(command, cwd=cwd or self.work, env=self.env,
                                text=True, capture_output=True, timeout=90)
        print("$ " + shlex.join(command), flush=True)
        print(result.stdout + result.stderr, end="", flush=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def record_artifacts(self, paths):
        print("artifact_sha256=" + json.dumps({
            str(path.relative_to(self.work)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
        }, sort_keys=True), flush=True)

    def test_install_clients_and_uninstall(self):
        dest = self.work / "installed"
        self.run_checked(self.make + ["install", "DESTDIR=" + str(dest)], self.tree)
        include = dest / "usr/include"
        lib = dest / "usr/lib"
        self.assertEqual({p.name for p in (include / "xtos").iterdir()}, HEADERS,
                         "installed public headers must include transitive frame UAPI")
        for name in HEADERS:
            self.assertEqual((include / "xtos" / name).read_bytes(),
                             (SDK / "include/xtos" / name).read_bytes(), name)
        self.assertEqual({p.name for p in lib.iterdir()}, {
            "libxtos-bci.a", "libxtos-bci.so", "libxtos-bci.so.1", "libxtos-bci.so.1.0"})
        self.assertEqual(os.readlink(lib / "libxtos-bci.so"), "libxtos-bci.so.1")
        self.assertEqual(os.readlink(lib / "libxtos-bci.so.1"), "libxtos-bci.so.1.0")
        for name in ("libxtos-bci.a", "libxtos-bci.so.1.0"):
            self.assertFalse((lib / name).is_symlink())
            self.assertEqual((lib / name).read_bytes(), (self.tree / name).read_bytes())
        self.record_artifacts([lib / "libxtos-bci.a", lib / "libxtos-bci.so.1.0"])
        for command in (["nm", "-D", "--defined-only", str(lib / "libxtos-bci.so.1.0")],
                        ["nm", "--defined-only", str(lib / "libxtos-bci.a")]):
            symbols = {line.split()[-1] for line in self.run_checked(command).splitlines() if line.split()}
            self.assertTrue(FRAME_SYMBOLS <= symbols, FRAME_SYMBOLS - symbols)
            self.assertTrue({"xtos_bci_get_config", "xtos_bci_snapshot_release"} <= symbols)
        client = self.work / "client.c"
        client.write_text(CLIENT)
        for mode in ("shared", "static"):
            binary = self.work / ("client-" + mode)
            dep = self.work / (mode + ".d")
            command = self.cc + ["-std=c11", "-Wall", "-Wextra", "-Werror",
                                 "-I" + str(include), "-MMD", "-MF", str(dep), str(client)]
            if mode == "shared":
                command += ["-L" + str(lib), "-lxtos-bci", "-Wl,-rpath," + str(lib)]
            else:
                command += [str(lib / "libxtos-bci.a")]
            self.run_checked(command + ["-o", str(binary)])
            self.record_artifacts([client, binary, dep])
            dependencies = shlex.split(dep.read_text().replace("\\\n", " ").split(":", 1)[1])
            self.assertEqual({Path(p).name for p in dependencies if p.endswith(".h")},
                             HEADERS - {"bci_uapi.h"})
            for path in dependencies:
                if path.endswith(".h"):
                    self.assertTrue(Path(path).resolve().is_relative_to(include.resolve()), path)
            self.assertIn("installed_frame_client=pass null_api_cases=8",
                          self.run_checked([str(binary)]))
            if mode == "shared":
                dynamic = self.run_checked(["readelf", "-d", str(binary)])
                self.assertIn("[libxtos-bci.so.1]", dynamic)
                self.assertIn(str(lib), dynamic)
        cxx = shlex.split(os.environ.get("CXX", "c++"))
        if shutil.which(cxx[0]):
            source = self.work / "header.cpp"
            source.write_text('#include <xtos/bci.h>\n#include <xtos/bci_version.h>\n'
                              'static_assert(sizeof(xtos_bci_frame_v1) == 224, "frame ABI");\n'
                              'int main() { return 0; }\n')
            self.run_checked(cxx + ["-std=c++11", "-Wall", "-Wextra", "-Werror",
                                    "-I" + str(include), "-c", str(source),
                                    "-o", str(self.work / "header.o")])
            print("installed_cpp_header=pass", flush=True)
        else:
            print("installed_cpp_header=skip compiler_unavailable", flush=True)
        sentinels = [include / "xtos/unrelated.h", lib / "unrelated.keep"]
        for path in sentinels:
            path.write_bytes(b"preserve unrelated installation\n")
        source_bytes = {p.relative_to(self.tree): p.read_bytes() for p in self.tree.rglob("*")
                        if p.is_file() and p.suffix in {".c", ".h"}}
        self.run_checked(self.make + ["uninstall", "DESTDIR=" + str(dest)], self.tree)
        self.assertEqual({p.name for p in (include / "xtos").iterdir()}, {"unrelated.h"})
        self.assertEqual({p.name for p in lib.iterdir()}, {"unrelated.keep"})
        for path in sentinels:
            self.assertEqual(path.read_bytes(), b"preserve unrelated installation\n")
        for path, contents in source_bytes.items():
            self.assertEqual((self.tree / path).read_bytes(), contents)
        print("frame_install_uninstall=pass headers=6 exports=4 shared=pass static=pass", flush=True)

    def test_make_test_and_clean(self):
        self.assertFalse(any((self.tree / "tests" / name).exists() for name in BINARIES))
        output = self.run_checked(self.make + ["test"], self.tree)
        for marker in ("frame_sdk_cases=521 pass", "snapshot_sdk_cases=", "snapshot_uapi=pass",
                       "BCI sample conversion: PASS", "config_sdk_cases=25 pass"):
            self.assertIn(marker, output)
        for name in BINARIES:
            self.assertTrue((self.tree / "tests" / name).is_file(), name)
        self.record_artifacts([self.tree / "tests" / name for name in sorted(BINARIES)])
        sentinel = self.tree / "tests/unrelated.keep"
        sentinel.write_bytes(b"preserve unrelated build file\n")
        self.run_checked(self.make + ["clean"], self.tree)
        for name in BINARIES:
            self.assertFalse((self.tree / "tests" / name).exists(), name)
        self.assertFalse((self.tree / "src/session.o").exists())
        self.assertEqual(sentinel.read_bytes(), b"preserve unrelated build file\n")
        self.assertTrue((self.tree / "tests/test_frame.c").is_file())
        print("frame_make_test_clean=pass binaries=5", flush=True)


if __name__ == "__main__":
    unittest.main()
