"""Fixture-only Ganglion BLE adapter tests; no hardware or BLE stack."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "xtos-eeg-pipeline"))
sys.path.insert(0, str(ROOT / "tools" / "xtos-devices"))

from ganglion_plugin import GanglionPlugin  # noqa: E402
from ganglion_protocol import GanglionProtocolError  # noqa: E402
from xtos_devices import (  # noqa: E402
    Authentication,
    DeviceIdentity,
    Lifecycle,
    Provenance,
    SimulatedTransport,
    TransportKind,
)

RAW_ZERO = bytes.fromhex("0000000000000000000000000041424344454647")
PACKET_18_ZERO = bytes.fromhex("0100000000000000000000000000000000000012")
PACKET_19_ZERO = bytes.fromhex("6600000000000000000000000000000000000000")


class GanglionPluginTests(unittest.TestCase):
    def make_plugin(self, sessions=None):
        transport = SimulatedTransport(
            sessions if sessions is not None else ((RAW_ZERO,),),
            kind=TransportKind.BLE,
        )
        plugin = GanglionPlugin(
            DeviceIdentity("fixture:ganglion-1", "OpenBCI Ganglion v2.0.1"),
            transport,
            provenance=Provenance.FIXTURE_REPLAY,
        )
        return plugin, transport

    def test_fixture_connect_start_read_publishes_protocol_samples(self):
        plugin, transport = self.make_plugin(((RAW_ZERO, PACKET_18_ZERO, PACKET_19_ZERO),))
        generation = plugin.connect()
        self.assertEqual(plugin.authentication, Authentication.NOT_CHECKED)
        plugin.start(generation)
        seed = plugin.read(generation)
        first = plugin.read(generation)
        second = plugin.read(generation)
        self.assertEqual(seed[0].packet_id, 0)
        self.assertEqual(len(first), 2)
        self.assertEqual(first[0].ring_index, 1)
        self.assertEqual(first[0].sample_index, 0)
        self.assertEqual(second[0].packet_id, 102)
        self.assertEqual(second[0].ring_index, 2)
        for sample in seed + first + second:
            self.assertEqual(sample.identity, plugin.identity)
            self.assertEqual(sample.transport_kind, TransportKind.BLE)
            self.assertEqual(sample.provenance, Provenance.FIXTURE_REPLAY)
            self.assertEqual(sample.action, "no_action")
            self.assertIs(sample.physical_device_evidence, False)
            self.assertEqual(sample.authentication, Authentication.NOT_CHECKED)
        self.assertTrue(transport.connected)
        self.assertEqual(plugin.state, Lifecycle.STREAMING)

    def test_gap_fails_closed_and_reconnect_uses_new_generation(self):
        plugin, transport = self.make_plugin(((RAW_ZERO, PACKET_19_ZERO), (RAW_ZERO,)))
        old = plugin.connect()
        plugin.start(old)
        plugin.read(old)
        with self.assertRaises(GanglionProtocolError):
            plugin.read(old)
        self.assertEqual(plugin.state, Lifecycle.FAILED)
        self.assertFalse(transport.connected)
        new = plugin.connect()
        self.assertNotEqual(old, new)
        plugin.start(new)
        with self.assertRaisesRegex(RuntimeError, "stale"):
            plugin.read(old)
        self.assertEqual(plugin.read(new)[0].packet_id, 0)

    def test_failed_generation_drops_old_controls_before_reconnect(self):
        plugin, transport = self.make_plugin(((bytes([201]) + b"Z", OSError("fixture read failure")),
                                              (RAW_ZERO,)))
        old = plugin.connect()
        plugin.start(old)
        self.assertEqual(plugin.read(old), ())
        self.assertEqual(plugin.controls[0].text, "Z")
        with self.assertRaises(OSError):
            plugin.read(old)
        self.assertEqual(plugin.controls, ())
        new = plugin.connect()
        self.assertNotEqual(old, new)
        self.assertEqual(plugin.controls, ())

    def test_failed_disconnect_cleanup_still_drops_control_queue(self):
        class BrokenDisconnect(SimulatedTransport):
            def disconnect(self):
                super().disconnect()
                raise OSError("cleanup failed")

        transport = BrokenDisconnect(((bytes([201]) + b"Z", OSError("read failed")),),
                                     kind=TransportKind.BLE)
        plugin = GanglionPlugin(DeviceIdentity("fixture:ganglion-2", "Ganglion"),
                                transport, provenance=Provenance.FIXTURE_REPLAY)
        generation = plugin.connect()
        plugin.start(generation)
        plugin.read(generation)
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            plugin.read(generation)
        self.assertEqual(plugin.controls, ())
        self.assertEqual(plugin.state, Lifecycle.FAILED)

    def test_cancel_cleanup_failure_does_not_retain_control_queue(self):
        class BrokenDisconnect(SimulatedTransport):
            def disconnect(self):
                super().disconnect()
                raise OSError("cleanup failed")

        transport = BrokenDisconnect(((bytes([201]) + b"Z",),), kind=TransportKind.BLE)
        plugin = GanglionPlugin(DeviceIdentity("fixture:ganglion-3", "Ganglion"),
                                transport, provenance=Provenance.FIXTURE_REPLAY)
        generation = plugin.connect()
        plugin.start(generation)
        plugin.read(generation)
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            plugin.cancel()
        self.assertEqual(plugin.controls, ())
        self.assertEqual(plugin.state, Lifecycle.FAILED)

    def test_cancel_disconnects_and_clears_protocol_state(self):
        plugin, transport = self.make_plugin(((RAW_ZERO,), (RAW_ZERO,)))
        generation = plugin.connect()
        plugin.start(generation)
        plugin.cancel()
        self.assertEqual(plugin.state, Lifecycle.DISCONNECTED)
        self.assertFalse(transport.connected)
        self.assertFalse(plugin.decoder.seeded)
        with self.assertRaisesRegex(RuntimeError, "streaming"):
            plugin.read(generation)


if __name__ == "__main__":
    unittest.main()
