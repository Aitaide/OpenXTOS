#!/usr/bin/env python3
"""Tests for the XTOS SBOM generator (tools/xtos-sbom).

Validates the generated CycloneDX document: required fields, real source
hashes, upstream archive digest, complete dependency graph, and license
coverage for every shipped component.
"""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def generate(out: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(HERE / "gen_sbom.py"), "--out", str(out)],
        capture_output=True, text=True, check=True)
    assert "sbom written" in result.stdout
    return json.loads(out.read_text())


class SbomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.bom = generate(Path(cls.tmp.name) / "sbom.json")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_format_and_metadata(self):
        self.assertEqual(self.bom["bomFormat"], "CycloneDX")
        self.assertEqual(self.bom["specVersion"], "1.5")
        self.assertIn("serialNumber", self.bom)
        meta = self.bom["metadata"]
        self.assertIn("timestamp", meta)
        self.assertEqual(meta["component"]["name"], "xtos")
        self.assertTrue(meta["properties"])

    def test_component_inventory(self):
        names = {c["name"] for c in self.bom["components"]}
        for required in ("linux", "preempt-rt", "xtos-linux-integration",
                         "busybox", "libxtos-rt", "libxtos-signal",
                         "libxtos-bci", "xtos-eeg-pipeline"):
            self.assertIn(required, names, f"{required} missing from SBOM")
        required_comps = [c for c in self.bom["components"]
                          if c.get("scope") == "required"]
        self.assertEqual(len(required_comps), 11)  # system + 10 shipped

    def test_every_component_declares_license(self):
        for comp in self.bom["components"]:
            licenses = comp.get("licenses", [])
            self.assertTrue(licenses, f"{comp['name']} has no license")
            self.assertIn("license", licenses[0])

    def test_kernel_hash_is_real(self):
        kernel = next(c for c in self.bom["components"]
                      if c["name"] == "linux")
        real = hashlib.sha256(
            (ROOT / "kernel/linux/Makefile").read_bytes()).hexdigest()
        contents = {h["content"] for h in kernel["hashes"]}
        self.assertIn(real, contents)

    def test_busybox_upstream_digest_recorded(self):
        busybox = next(c for c in self.bom["components"]
                       if c["name"] == "busybox")
        props = {p["name"]: p["value"] for p in busybox["properties"]}
        self.assertEqual(props["xtos:upstream-archive-sha256"],
                         "3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4")

    def test_dependency_graph_covers_all_components(self):
        deps = {d["ref"]: d["dependsOn"] for d in self.bom["dependencies"]}
        refs = {c["bom-ref"] for c in self.bom["components"]}
        self.assertTrue(refs.issubset(deps.keys()),
                        "components without dependency nodes")
        system_ref = self.bom["metadata"]["component"]["bom-ref"]
        required_refs = {c["bom-ref"] for c in self.bom["components"]
                         if c.get("scope") == "required"
                         and c["bom-ref"] != system_ref}
        self.assertEqual(set(deps[system_ref]), required_refs)

    def test_optional_ml_deps_are_excluded_scope(self):
        excluded = [c for c in self.bom["components"]
                    if c.get("scope") == "excluded"]
        names = {c["name"] for c in excluded}
        for optional in ("onnxruntime", "numpy", "torch", "tensorflow"):
            self.assertIn(optional, names)
        for comp in excluded:
            self.assertNotIn(comp["bom-ref"],
                             self.bom["dependencies"][0]["dependsOn"])


if __name__ == "__main__":
    unittest.main()
