"""CPU-only phase-4 plugin contract tests; no physical device evidence."""
import importlib
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "xtos-eeg-pipeline"))
sys.path.insert(0, str(ROOT / "tools" / "xtos-devices"))

from openbci_cyton import CytonPacketParser


def packet(counter=7, values=(-8388608, -1, 0, 1, 123456, -654321, 8388607, 42)):
    """Independent wire fixture, not a duplicate decoder."""
    return (bytes((0xA0, counter))
            + b"".join((value & 0xFFFFFF).to_bytes(3, "big") for value in values)
            + b"\x01\x02\x03\x04\x05\x06\xcf")


class DeviceTests(unittest.TestCase):
    def devices(self):
        self.assertIsNotNone(importlib.util.find_spec("xtos_devices"),
                             "unified plugin contract is missing")
        return importlib.import_module("xtos_devices")

    def make_plugin(self, sessions=None, **kwargs):
        d = self.devices()
        self.assertTrue(hasattr(d, "SimulatedTransport"), "simulated transport is missing")
        self.assertTrue(hasattr(d, "CytonPlugin"), "canonical Cyton plugin is missing")
        transport = d.SimulatedTransport(
            sessions if sessions is not None else ((packet(),),), **kwargs)
        plugin = d.CytonPlugin(d.DeviceIdentity("fixture:cyton-1", "OpenBCI Cyton"),
                               transport, monotonic_ns=iter(range(100, 1000)).__next__)
        return d, transport, plugin

    def test_cyton_connect_start_read_converts_canonical_frame_without_physical_claim(self):
        d, transport, plugin = self.make_plugin()
        registry = d.PluginRegistry()
        registry.register("cyton", d.CytonPlugin)
        self.assertIs(registry.lookup("cyton"), d.CytonPlugin)
        self.assertEqual(plugin.state, d.Lifecycle.DISCONNECTED)
        generation = plugin.connect()
        self.assertEqual(plugin.authentication, d.Authentication.ACCEPTED)
        plugin.start(generation)
        samples = plugin.read(generation)
        self.assertEqual(len(samples), 1)
        sample = samples[0]
        expected = CytonPacketParser(monotonic_ns=lambda: 100).feed(packet())[0]
        self.assertIsInstance(plugin.parser, CytonPacketParser)
        self.assertEqual((sample.sequence, sample.sample_number, sample.host_receive_monotonic_ns,
                          sample.channels, sample.auxiliary, sample.footer, sample.timestamp_clock),
                         (expected.sequence, expected.sample_number, expected.host_receive_monotonic_ns,
                          expected.channels_nv, expected.auxiliary, expected.footer, expected.timestamp_clock))
        self.assertEqual(sample.identity, plugin.identity)
        self.assertEqual(sample.capabilities, d.Capabilities(8, 250, "nanovolt"))
        self.assertEqual(sample.transport_kind, d.TransportKind.SERIAL)
        self.assertEqual(sample.generation, generation)
        self.assertEqual(sample.authentication, d.Authentication.ACCEPTED)
        self.assertEqual(sample.provenance, d.Provenance.FIXTURE_REPLAY)
        self.assertEqual(sample.action, "no_action")
        self.assertIs(sample.physical_device_evidence, False)
        self.assertIs(plugin.physical_device_evidence, False)
        self.assertEqual([event.state for event in plugin.events],
                         [d.Lifecycle.DISCONNECTED, d.Lifecycle.CONNECTED, d.Lifecycle.STREAMING])
        self.assertEqual(plugin.events[-1].generation, generation)
        self.assertEqual(plugin.events[-1].provenance, sample.provenance)
        self.assertTrue(transport.connected)

    def test_malformed_or_eof_transport_fails_closed_before_publishing_samples(self):
        cases = ((packet()[:-1] + b"\x00",), (b"noise" + packet(),),
                 (), (packet()[:9], b""), ("not bytes",), (OSError("broken fixture"),),
                 (packet() + packet(),), (packet(7) + packet(9),))
        for chunks in cases:
            with self.subTest(chunks=repr(chunks)):
                d, transport, plugin = self.make_plugin((chunks,))
                generation = plugin.connect()
                plugin.start(generation)
                with self.assertRaisesRegex(RuntimeError, "malformed|eof|transport"):
                    for _ in range(max(1, len(chunks))):
                        plugin.read(generation)
                self.assertEqual(plugin.state, d.Lifecycle.FAILED)
                self.assertFalse(transport.connected)
                self.assertEqual(plugin.events[-1].state, d.Lifecycle.FAILED)
                with self.assertRaisesRegex(RuntimeError, "streaming"):
                    plugin.read(generation)

    def test_cancel_disconnects_partial_stream_and_reconnect_rejects_stale_generation(self):
        d, transport, plugin = self.make_plugin(((packet()[:10],), (packet(0),)))
        old = plugin.connect()
        plugin.start(old)
        self.assertEqual(plugin.read(old), ())
        self.assertTrue(hasattr(plugin, "cancel"), "cancellation is missing")
        plugin.cancel()
        self.assertEqual(plugin.state, d.Lifecycle.DISCONNECTED)
        self.assertFalse(transport.connected)
        self.assertEqual(plugin.authentication, d.Authentication.NOT_CHECKED)
        self.assertEqual(plugin.events[-1].reason, "cancelled")
        with self.assertRaisesRegex(RuntimeError, "streaming"):
            plugin.read(old)
        new = plugin.connect()
        self.assertEqual(new, old + 1)
        with self.assertRaisesRegex(RuntimeError, "generation"):
            plugin.start(old)
        plugin.start(new)
        with self.assertRaisesRegex(RuntimeError, "generation"):
            plugin.read(old)
        self.assertEqual(plugin.state, d.Lifecycle.STREAMING)
        fresh = plugin.read(new)
        self.assertEqual([(s.sequence, s.generation) for s in fresh], [(0, new)])
        plugin.cancel()
        plugin.cancel()
        self.assertEqual(plugin.state, d.Lifecycle.DISCONNECTED)

    def test_authentication_failure_blocks_streaming_and_can_recover(self):
        d, transport, plugin = self.make_plugin(((packet(),), (packet(1),)))
        transport.authentication = d.Authentication.REJECTED
        with self.assertRaisesRegex(RuntimeError, "authentication"):
            plugin.connect()
        self.assertEqual(plugin.authentication, d.Authentication.REJECTED)
        self.assertEqual(plugin.state, d.Lifecycle.FAILED)
        self.assertFalse(transport.connected)
        self.assertEqual(plugin.events[-1].authentication, d.Authentication.REJECTED)
        with self.assertRaisesRegex(RuntimeError, "authentication"):
            plugin.start(plugin.generation)
        with self.assertRaisesRegex(RuntimeError, "streaming"):
            plugin.read(plugin.generation)
        rejected = plugin.generation
        transport.authentication = d.Authentication.ACCEPTED
        generation = plugin.connect()
        self.assertEqual(generation, rejected + 1)
        plugin.start(generation)
        self.assertEqual(plugin.read(generation)[0].authentication, d.Authentication.ACCEPTED)

    def test_bounded_reads_accept_capacity_and_reject_over_capacity_before_decode(self):
        d = self.devices()
        at_capacity = b"".join(packet(n) for n in range(4))
        self.assertEqual(len(at_capacity), d.MAX_READ_BYTES)
        _, _, plugin = self.make_plugin(((at_capacity,),))
        generation = plugin.connect()
        plugin.start(generation)
        self.assertEqual(len(plugin.read(generation)), 4)
        _, transport, plugin = self.make_plugin(((at_capacity + b"x",),))
        generation = plugin.connect()
        plugin.start(generation)
        with self.assertRaisesRegex(RuntimeError, "capacity"):
            plugin.read(generation)
        self.assertEqual(plugin.parser.stats.bytes_received, 0)
        self.assertFalse(transport.connected)
        self.assertEqual(plugin.state, d.Lifecycle.FAILED)
        for bound in (0, -1, True, d.MAX_READ_BYTES + 1):
            transport = d.SimulatedTransport(((b"a",),))
            transport.connect()
            with self.assertRaisesRegex(ValueError, "capacity"):
                transport.read(bound)
        transport = d.SimulatedTransport(((b"ab",),))
        transport.connect()
        with self.assertRaisesRegex(RuntimeError, "capacity"):
            transport.read(1)

    def test_lifecycle_rejects_active_reconnect_and_failed_restart_until_reconnect(self):
        d, transport, plugin = self.make_plugin(((b"noise",), (packet(1),)))
        with self.assertRaisesRegex(RuntimeError, "authentication"):
            plugin.start(0)
        generation = plugin.connect()
        with self.assertRaisesRegex(RuntimeError, "disconnect"):
            plugin.connect()
        self.assertEqual(plugin.generation, generation)
        plugin.start(generation)
        with self.assertRaisesRegex(RuntimeError, "connected"):
            plugin.start(generation)
        with self.assertRaisesRegex(RuntimeError, "malformed"):
            plugin.read(generation)
        with self.assertRaisesRegex(RuntimeError, "connected"):
            plugin.start(generation)
        self.assertFalse(transport.connected)
        fresh = plugin.connect()
        self.assertEqual(fresh, generation + 1)
        plugin.start(fresh)
        self.assertEqual(plugin.read(fresh)[0].sample_number, 1)
        for invalid in (True, float(fresh), fresh - 1):
            with self.assertRaisesRegex(RuntimeError, "generation"):
                plugin.read(invalid)

    def test_injected_transport_contract_enforces_fixture_provenance_and_read_bound(self):
        d = self.devices()
        self.assertTrue(hasattr(d, "Transport"), "unified transport contract is missing")
        self.assertTrue(hasattr(d.Transport, "read"))
        self.assertTrue(hasattr(d.Transport, "disconnect"))
        class Oversized(d.SimulatedTransport):
            def read(self, max_bytes):
                self.requested = max_bytes
                return b"".join(packet(n) for n in range(5))
        transport = Oversized(())
        plugin = d.CytonPlugin(d.DeviceIdentity("fixture:oversize", "Cyton"), transport)
        generation = plugin.connect()
        plugin.start(generation)
        with self.assertRaisesRegex(RuntimeError, "capacity"):
            plugin.read(generation)
        self.assertEqual(transport.requested, d.MAX_READ_BYTES)
        self.assertEqual(plugin.parser.stats.bytes_received, 0)
        self.assertFalse(transport.connected)
        transport = d.SimulatedTransport(())
        transport.provenance = d.Provenance.LIVE_DEVICE
        with self.assertRaisesRegex(ValueError, "fixture"):
            d.CytonPlugin(d.DeviceIdentity("live:unverified", "Cyton"), transport)

    def test_existing_canonical_split_wrap_vectors_across_simulated_transport_kinds(self):
        # Compatibility verification: expected to pass immediately, not a new TDD RED.
        d = self.devices()
        raw = packet(255) + packet(0)
        for kind in d.TransportKind:
            with self.subTest(kind=kind):
                _, _, plugin = self.make_plugin(((raw[:1], raw[1:40], raw[40:]),), kind=kind)
                generation = plugin.connect()
                plugin.start(generation)
                samples = tuple(sample for _ in range(3) for sample in plugin.read(generation))
                self.assertEqual([sample.sequence for sample in samples], [255, 256])
                self.assertEqual([sample.transport_kind for sample in samples], [kind, kind])
                self.assertTrue(all(sample.provenance is d.Provenance.FIXTURE_REPLAY
                                    and sample.action == "no_action"
                                    and sample.physical_device_evidence is False for sample in samples))

    def test_registry_rejects_duplicate_names_and_unknown_lookup(self):
        d = self.devices()
        self.assertTrue(hasattr(d, "PluginRegistry"), "plugin registry is missing")
        registry = d.PluginRegistry()
        factory = lambda transport: transport
        registry.register("cyton", factory)
        self.assertIs(registry.lookup("cyton"), factory)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            registry.register("cyton", lambda transport: None)
        self.assertIs(registry.lookup("cyton"), factory)
        with self.assertRaisesRegex(KeyError, "unknown"):
            registry.lookup("ganglion")

    def test_unified_contract_carries_identity_capability_lifecycle_and_provenance(self):
        d = self.devices()
        self.assertEqual({kind.value for kind in d.TransportKind},
                         {"serial", "usb", "ble", "network"})
        self.assertEqual({source.value for source in d.Provenance},
                         {"fixture_replay", "live_device"})
        identity = d.DeviceIdentity("fixture:cyton-1", "OpenBCI Cyton")
        capability = d.Capabilities(8, 250, "nanovolt")
        event = d.LifecycleEvent(d.Lifecycle.DISCONNECTED, 0, "created",
                                 d.Authentication.NOT_CHECKED, d.Provenance.FIXTURE_REPLAY)
        self.assertEqual((identity.device_id, identity.model),
                         ("fixture:cyton-1", "OpenBCI Cyton"))
        self.assertEqual((capability.channels, capability.sample_rate_hz, capability.unit),
                         (8, 250, "nanovolt"))
        self.assertEqual((event.state, event.generation, event.reason, event.authentication,
                          event.provenance),
                         (d.Lifecycle.DISCONNECTED, 0, "created", d.Authentication.NOT_CHECKED,
                          d.Provenance.FIXTURE_REPLAY))
        self.assertTrue(hasattr(d.DevicePlugin, "read"))
    def test_exceptional_disconnect_and_connect_paths_fail_closed(self):
        d = self.devices()

        class FaultyTransport(d.SimulatedTransport):
            def __init__(self, sessions, *, connect_error=None, disconnect_error=False):
                super().__init__(sessions)
                self.connect_error = connect_error
                self.disconnect_error = disconnect_error
                self.disconnect_attempts = 0

            def connect(self):
                if self.connect_error is not None:
                    self.connected = True
                    raise self.connect_error
                return super().connect()

            def disconnect(self):
                self.disconnect_attempts += 1
                self.connected = False
                if self.disconnect_error:
                    raise RuntimeError("disconnect failed")
                return super().disconnect()

        connect_transport = FaultyTransport((), connect_error=RuntimeError("connect failed"),
                                            disconnect_error=True)
        connect_plugin = d.CytonPlugin(d.DeviceIdentity("fixture:connect", "Cyton"),
                                       connect_transport)
        with self.assertRaisesRegex(RuntimeError, "connect") as connect_failure:
            connect_plugin.connect()
        self.assertIsInstance(connect_failure.exception.__cause__, RuntimeError)
        self.assertEqual(str(connect_failure.exception.__cause__), "connect failed")
        self.assertIn("disconnect failed", str(connect_failure.exception))
        self.assertEqual(connect_plugin.state, d.Lifecycle.FAILED)
        self.assertEqual(connect_plugin.events[-1].state, d.Lifecycle.FAILED)
        self.assertEqual(connect_plugin.events[-1].authentication, d.Authentication.NOT_CHECKED)
        self.assertEqual(connect_plugin.events[-1].reason, "transport_cleanup_failed")
        self.assertGreaterEqual(connect_transport.disconnect_attempts, 1)

        successful_cleanup_transport = FaultyTransport((), connect_error=RuntimeError("connect clean"))
        successful_cleanup_plugin = d.CytonPlugin(
            d.DeviceIdentity("fixture:connect-clean", "Cyton"), successful_cleanup_transport)
        with self.assertRaisesRegex(RuntimeError, "connect clean") as clean_failure:
            successful_cleanup_plugin.connect()
        self.assertIsInstance(clean_failure.exception.__cause__, RuntimeError)
        self.assertEqual(successful_cleanup_plugin.events[-1].reason, "transport_connect_failed")
        self.assertEqual(successful_cleanup_plugin.events[-1].authentication,
                         d.Authentication.NOT_CHECKED)

        for previous_authentication in (d.Authentication.ACCEPTED, d.Authentication.REJECTED):
            with self.subTest(previous_authentication=previous_authentication):
                reconnect_transport = FaultyTransport(((b"noise",),))
                reconnect_plugin = d.CytonPlugin(
                    d.DeviceIdentity("fixture:reconnect", "Cyton"), reconnect_transport)
                reconnect_transport.authentication = previous_authentication
                if previous_authentication is d.Authentication.REJECTED:
                    with self.assertRaisesRegex(RuntimeError, "authentication"):
                        reconnect_plugin.connect()
                else:
                    accepted_generation = reconnect_plugin.connect()
                    reconnect_plugin.start(accepted_generation)
                    with self.assertRaisesRegex(RuntimeError, "malformed"):
                        reconnect_plugin.read(accepted_generation)
                old_generation = reconnect_plugin.generation
                reconnect_transport.connect_error = RuntimeError("reconnect failed")
                with self.assertRaisesRegex(RuntimeError, "reconnect failed") as reconnect_failure:
                    reconnect_plugin.connect()
                self.assertIsInstance(reconnect_failure.exception.__cause__, RuntimeError)
                self.assertEqual(reconnect_plugin.generation, old_generation + 1)
                self.assertEqual(reconnect_plugin.events[-1].authentication,
                                 d.Authentication.NOT_CHECKED)
                self.assertEqual(reconnect_plugin.events[-1].reason,
                                 "transport_connect_failed")


        read_transport = FaultyTransport(((b"noise",),), disconnect_error=True)
        read_plugin = d.CytonPlugin(d.DeviceIdentity("fixture:read", "Cyton"),
                                    read_transport)
        generation = read_plugin.connect()
        read_plugin.start(generation)
        with self.assertRaisesRegex(RuntimeError, "transport"):
            read_plugin.read(generation)
        self.assertEqual(read_plugin.state, d.Lifecycle.FAILED)
        self.assertEqual(read_plugin.events[-1].state, d.Lifecycle.FAILED)
        self.assertFalse(read_transport.connected)

        cancel_transport = FaultyTransport(((packet(),),), disconnect_error=True)
        cancel_plugin = d.CytonPlugin(d.DeviceIdentity("fixture:cancel", "Cyton"),
                                      cancel_transport)
        cancel_generation = cancel_plugin.connect()
        with self.assertRaisesRegex(RuntimeError, "cancel"):
            cancel_plugin.cancel()
        self.assertEqual(cancel_plugin.state, d.Lifecycle.FAILED)
        self.assertEqual(cancel_plugin.authentication, d.Authentication.NOT_CHECKED)
        self.assertEqual(cancel_plugin.generation, cancel_generation)
        self.assertEqual(cancel_plugin.events[-1].state, d.Lifecycle.FAILED)


if __name__ == "__main__":
    unittest.main()
