#!/usr/bin/env python3
"""Focused native PureEd25519 model-verification helper regression tests."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "xtos-native" / "modelverify" / "modelverify.c"
COMMON = ROOT / "tools" / "xtos-native" / "common"
MONOCYPHER = ROOT / "tools" / "xtos-native" / "vendor" / "monocypher"


class NativeModelVerifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="xtos-modelverify-test-"))
        cls.addClassCleanup(shutil.rmtree, cls.work, ignore_errors=True)
        cls.binary = cls.work / "modelverify"
        subprocess.run(
            [
                "gcc",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-O2",
                "-static",
                "-I",
                str(COMMON),
                "-I",
                str(MONOCYPHER),
                "-o",
                str(cls.binary),
                str(SRC),
                str(MONOCYPHER / "monocypher.c"),
                str(MONOCYPHER / "monocypher-ed25519.c"),
            ],
            check=True,
        )

        cls.private_key = cls.work / "signing-key.pem"
        subprocess.run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "Ed25519",
                "-out",
                str(cls.private_key),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        der = cls.work / "public.der"
        subprocess.run(
            [
                "openssl",
                "pkey",
                "-in",
                str(cls.private_key),
                "-pubout",
                "-outform",
                "DER",
                "-out",
                str(der),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        der_bytes = der.read_bytes()
        if len(der_bytes) != 44:
            raise AssertionError(f"unexpected Ed25519 SPKI DER length: {len(der_bytes)}")
        cls.public_key = cls.work / "public.raw"
        cls.public_key.write_bytes(der_bytes[-32:])
        cls.public_pem = cls.work / "public.pem"
        subprocess.run(
            [
                "openssl",
                "pkey",
                "-in",
                str(cls.private_key),
                "-pubout",
                "-out",
                str(cls.public_pem),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.wrong_private_key = cls.work / "wrong-signing-key.pem"
        subprocess.run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "Ed25519",
                "-out",
                str(cls.wrong_private_key),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wrong_der = cls.work / "wrong-public.der"
        subprocess.run(
            [
                "openssl",
                "pkey",
                "-in",
                str(cls.wrong_private_key),
                "-pubout",
                "-outform",
                "DER",
                "-out",
                str(wrong_der),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.wrong_public_key = cls.work / "wrong-public.raw"
        cls.wrong_public_key.write_bytes(wrong_der.read_bytes()[-32:])
        cls.message = cls.work / "message"
        cls.message.write_bytes(b"native model verification fixture\n")
        cls.signature = cls.work / "signature"
        subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-inkey",
                str(cls.private_key),
                "-rawin",
                "-in",
                str(cls.message),
                "-out",
                str(cls.signature),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def run_tool(self, *args):
        return subprocess.run(
            [str(self.binary), *map(str, args)],
            capture_output=True,
            timeout=5,
        )

    def run_fixture(self, pubkey=None, message=None, signature=None):
        return self.run_tool(
            "--pubkey",
            self.public_key if pubkey is None else pubkey,
            "--message",
            self.message if message is None else message,
            "--signature",
            self.signature if signature is None else signature,
        )

    def assert_bad_input(self, **files):
        result = self.run_fixture(**files)
        self.assertEqual(result.returncode, 2, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def assert_invalid_signature(self, **files):
        result = self.run_fixture(**files)
        self.assertEqual(result.returncode, 1, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def test_valid_signature_is_silent_success(self):
        result = self.run_fixture()
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def test_bit_flipped_signature_is_invalid(self):
        changed = self.work / "changed-signature"
        signature = bytearray(self.signature.read_bytes())
        signature[0] ^= 0x01
        changed.write_bytes(signature)
        self.assert_invalid_signature(signature=changed)

    def test_unknown_public_key_is_invalid(self):
        self.assert_invalid_signature(pubkey=self.wrong_public_key)

    def test_signature_trailing_byte_is_bad_input(self):
        changed = self.work / "signature-with-trailing-byte"
        changed.write_bytes(self.signature.read_bytes() + bytes([0]))
        self.assert_bad_input(signature=changed)

    def test_signature_short_read_is_bad_input(self):
        changed = self.work / "short-signature"
        changed.write_bytes(self.signature.read_bytes()[:-1])
        self.assert_bad_input(signature=changed)

    def test_public_key_must_be_exactly_32_raw_bytes(self):
        for label, payload in (("short", b"k" * 31), ("trailing", b"k" * 33)):
            with self.subTest(label=label):
                changed = self.work / f"{label}-public-key"
                changed.write_bytes(payload)
                self.assert_bad_input(pubkey=changed)

    def test_pem_public_key_is_rejected(self):
        self.assert_bad_input(pubkey=self.public_pem)

    def test_empty_message_is_bad_input(self):
        empty = self.work / "empty-message"
        empty.write_bytes(b"")
        self.assert_bad_input(message=empty)

    def test_missing_input_is_bad_input(self):
        self.assert_bad_input(pubkey=self.work / "missing-public-key")
        self.assert_bad_input(message=self.work / "missing-message")
        self.assert_bad_input(signature=self.work / "missing-signature")

    def test_symlink_input_is_bad_input(self):
        for label, target, argument in (
            ("public-key", self.public_key, "pubkey"),
            ("message", self.message, "message"),
            ("signature", self.signature, "signature"),
        ):
            with self.subTest(label=label):
                link = self.work / f"{label}-link"
                link.symlink_to(target)
                self.assert_bad_input(**{argument: link})

    def test_fifo_input_is_bad_input_without_blocking(self):
        fifo = self.work / "message-fifo"
        os.mkfifo(fifo)
        self.assert_bad_input(message=fifo)

    def test_oversized_message_is_bad_input(self):
        too_large = self.work / "oversized-message"
        too_large.write_bytes(b"M" * (65536 + 1))
        self.assert_bad_input(message=too_large)

    def test_maximum_message_size_verifies(self):
        message = self.work / "maximum-message"
        message.write_bytes(b"M" * 65536)
        signature = self.work / "maximum-message-signature"
        subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-inkey",
                str(self.private_key),
                "-rawin",
                "-in",
                str(message),
                "-out",
                str(signature),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        result = self.run_fixture(message=message, signature=signature)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def test_one_byte_message_verifies(self):
        message = self.work / "one-byte-message"
        message.write_bytes(b"M")
        signature = self.work / "one-byte-message-signature"
        subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-inkey",
                str(self.private_key),
                "-rawin",
                "-in",
                str(message),
                "-out",
                str(signature),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        result = self.run_fixture(message=message, signature=signature)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def test_wrong_arguments_are_bad_input(self):
        result = self.run_tool("--message", self.message)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        reordered = self.run_tool(
            "--message",
            self.message,
            "--pubkey",
            self.public_key,
            "--signature",
            self.signature,
        )
        self.assertEqual(reordered.returncode, 2)
        self.assertEqual(reordered.stdout, b"")
        self.assertEqual(reordered.stderr, b"")


if __name__ == "__main__":
    unittest.main()
