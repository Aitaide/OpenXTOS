"""PTY Cyton handshake tests; no physical device acceptance."""
import importlib
import importlib.util
import os
from pathlib import Path
import pty
import select
import sys
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools/xtos-eeg-pipeline'))
sys.path.insert(0, str(ROOT / 'tools/xtos-devices'))

NAMES = ('ADS_ID', 'CONFIG1', 'CONFIG2', 'CONFIG3', 'LOFF',
         'CH1SET', 'CH2SET', 'CH3SET', 'CH4SET', 'CH5SET', 'CH6SET', 'CH7SET', 'CH8SET',
         'BIAS_SENSP', 'BIAS_SENSN', 'LOFF_SENSP', 'LOFF_SENSN', 'LOFF_FLIP',
         'LOFF_STATP', 'LOFF_STATN', 'GPIO', 'MISC1', 'MISC2', 'CONFIG4')
BANNER = (b'OpenBCI V3 8-16 channel\r\nOn Board ADS1299 Device ID: 0x3E\r\n'
          b'LIS3DH Device ID: 0x33\r\nFirmware: v3.1.2\r\n$$$')


def registers():
    values = [0x3e, 0x96, 0xd0, 0xec, 0x02] + [0x68] * 8 + [0] * 11
    return (b'\r\nBoard ADS Registers\r\n' + b''.join(
        (f'{name}, {i:02X}, {value:02X}, ' + ', '.join(f'{value:08b}') + '\r\n').encode()
        for i, (name, value) in enumerate(zip(NAMES, values))) + b'$$$')


def packet(counter, values=(-8388608, -1, 0, 1, 123456, -654321, 8388607, 42), footer=0xcf):
    return (bytes((0xa0, counter))
            + b''.join((value & 0xffffff).to_bytes(3, 'big') for value in values)
            + b'\x01\x02\x03\x04\x05\x06' + bytes((footer,)))


class Board:
    """Finite PTY peer; responds only to the four explicit Cyton commands."""
    def __init__(self, *, banner=BANNER, reply=None, stream=b''):
        self.banner = banner
        self.reply = registers() if reply is None else reply
        self.stream = stream

    def __enter__(self):
        self.master, self.slave = pty.openpty()
        self.path = os.ttyname(self.slave)
        os.set_blocking(self.master, False)
        self.stop = threading.Event()
        self.commands = bytearray()
        self.errors = []
        self.worker = threading.Thread(target=self.serve, daemon=True)
        self.worker.start()
        return self

    def serve(self):
        try:
            while not self.stop.is_set():
                if not select.select([self.master], [], [], .02)[0]:
                    continue
                try:
                    data = os.read(self.master, 64)
                except BlockingIOError:
                    continue
                if not data:
                    return
                for byte in data:
                    self.commands.append(byte)
                    response = (self.banner if byte == ord('v') else self.reply if byte == ord('?')
                                else self.stream if byte == ord('b') else b'')
                    # Fragment command replies at a real descriptor boundary.
                    for pos in range(0, len(response), 13):
                        fragment = memoryview(response[pos:pos+13])
                        while fragment and not self.stop.is_set():
                            try:
                                size = os.write(self.master, fragment)
                                fragment = fragment[size:]
                            except BlockingIOError:
                                select.select([], [self.master], [], .02)
        except BaseException as exc:
            if not self.stop.is_set():
                self.errors.append(exc)

    def disconnect(self):
        self.stop.set()
        self.worker.join(2)
        if self.worker.is_alive():
            raise RuntimeError('PTY peer did not stop before disconnect')
        os.close(self.master)
        self.master = -1

    def __exit__(self, *unused):
        self.stop.set()
        self.worker.join(2)
        try:
            if self.worker.is_alive():
                raise RuntimeError('PTY test peer did not stop')
            if self.errors:
                raise self.errors[0]
        finally:
            if self.master >= 0:
                os.close(self.master)
            os.close(self.slave)


class CytonSerialPluginTests(unittest.TestCase):
    def test_real_pty_samples_reuse_canonical_parser_and_preserve_fixture_provenance(self):
        module = importlib.import_module('cyton_serial_plugin')
        devices = importlib.import_module('xtos_devices')
        serial = importlib.import_module('serial_transport')
        canonical = importlib.import_module('openbci_cyton')
        wire = packet(255) + packet(0)
        with Board(stream=wire) as board:
            plugin = module.CytonSerialPlugin(
                devices.DeviceIdentity('fixture:pty-samples', 'OpenBCI Cyton'),
                serial.LinuxSerialTransport(board.path), reset_defaults=True,
                provenance=devices.Provenance.FIXTURE_REPLAY)
            try:
                generation = plugin.connect(deadline_ns=time.monotonic_ns()+2_000_000_000)
                plugin.start(generation, deadline_ns=time.monotonic_ns()+1_000_000_000)
                self.assertTrue(hasattr(plugin, 'read'), 'canonical serial sample read API missing')
                samples = []
                until = time.monotonic_ns()+1_000_000_000
                while len(samples) < 2:
                    samples.extend(plugin.read(generation, deadline_ns=until))
                expected = canonical.CytonPacketParser(monotonic_ns=iter((100, 101)).__next__).feed(wire)
                self.assertIsInstance(plugin.parser, canonical.CytonPacketParser)
                for sample, frame in zip(samples, expected):
                    self.assertIsInstance(sample, devices.Sample)
                    self.assertEqual((sample.sequence, sample.sample_number, sample.channels,
                                      sample.auxiliary, sample.footer),
                                     (frame.sequence, frame.sample_number, frame.channels_nv,
                                      frame.auxiliary, frame.footer))
                    self.assertEqual(sample.generation, generation)
                    self.assertEqual(sample.authentication, devices.Authentication.NOT_CHECKED)
                    self.assertEqual(sample.provenance, devices.Provenance.FIXTURE_REPLAY)
                    self.assertEqual(sample.action, 'no_action')
                    self.assertFalse(sample.physical_device_evidence)
                self.assertEqual(samples[1].sequence, samples[0].sequence+1)
                self.assertLess(samples[0].host_receive_monotonic_ns, samples[1].host_receive_monotonic_ns)
            finally:
                plugin.close()

    def test_reconnect_resets_partial_parser_and_stale_token_has_no_io(self):
        module = importlib.import_module('cyton_serial_plugin')
        devices = importlib.import_module('xtos_devices')
        serial = importlib.import_module('serial_transport')
        with Board(stream=packet(10)[:9]) as board:
            transport = serial.LinuxSerialTransport(board.path)
            plugin = module.CytonSerialPlugin(devices.DeviceIdentity('fixture:reconnect', 'Cyton'),
                transport, reset_defaults=True, provenance=devices.Provenance.FIXTURE_REPLAY)
            try:
                old = plugin.connect(deadline_ns=time.monotonic_ns()+2_000_000_000)
                plugin.start(old, deadline_ns=time.monotonic_ns()+1_000_000_000)
                self.assertEqual(plugin.read(old, deadline_ns=time.monotonic_ns()+1_000_000_000), ())
                self.assertGreater(plugin.parser.buffered_bytes, 0)
                plugin.close()
                board.stream = packet(0)
                new = plugin.connect(deadline_ns=time.monotonic_ns()+2_000_000_000)
                self.assertEqual(new, old+1)
                self.assertEqual(plugin.parser.buffered_bytes, 0,
                                 'old partial parser crossed reconnect generation')
                plugin.start(new, deadline_ns=time.monotonic_ns()+1_000_000_000)
                original = transport.read
                touched = []
                def observed(*args, **kwargs):
                    touched.append(True)
                    return original(*args, **kwargs)
                transport.read = observed
                with self.assertRaisesRegex(module.CytonSessionError, 'generation'):
                    plugin.read(old, deadline_ns=time.monotonic_ns()+1_000_000_000)
                self.assertFalse(touched)
                samples = []
                while not samples:
                    samples.extend(plugin.read(new, deadline_ns=time.monotonic_ns()+1_000_000_000))
                self.assertEqual((samples[0].sequence, samples[0].generation), (0, new))
            finally:
                plugin.close()

    def test_cancel_interrupts_owner_read_and_closes_without_device_authentication(self):
        module = importlib.import_module('cyton_serial_plugin')
        devices = importlib.import_module('xtos_devices')
        serial = importlib.import_module('serial_transport')
        with Board() as board:
            transport = serial.LinuxSerialTransport(board.path)
            plugin = module.CytonSerialPlugin(devices.DeviceIdentity('fixture:cancel', 'Cyton'),
                transport, reset_defaults=True, provenance=devices.Provenance.FIXTURE_REPLAY)
            worker = None
            try:
                generation = plugin.connect(deadline_ns=time.monotonic_ns()+2_000_000_000)
                plugin.start(generation, deadline_ns=time.monotonic_ns()+1_000_000_000)
                self.assertTrue(hasattr(plugin, 'request_cancel'), 'session cancellation API missing')
                worker = threading.Thread(target=lambda: (time.sleep(.02), plugin.request_cancel()))
                worker.start()
                before = time.monotonic()
                with self.assertRaises(module.CytonSessionError) as caught:
                    plugin.read(generation, deadline_ns=time.monotonic_ns()+1_500_000_000)
                self.assertIsInstance(caught.exception.primary, serial.SerialCancelled)
                self.assertLess(time.monotonic()-before, .75)
                self.assertFalse(transport.connected)
                self.assertFalse(plugin.protocol_match)
                self.assertIsNone(plugin.configuration)
                self.assertEqual(plugin.parser.buffered_bytes, 0)
                self.assertEqual(plugin.state, devices.Lifecycle.FAILED)
            finally:
                if worker is not None:
                    worker.join(1)
                    self.assertFalse(worker.is_alive())
                plugin.close()

    def test_cancel_during_parser_publication_discards_samples_and_partial_state(self):
        module = importlib.import_module('cyton_serial_plugin')
        devices = importlib.import_module('xtos_devices')
        serial = importlib.import_module('serial_transport')
        with Board(stream=packet(1)+packet(2)[:5]) as board:
            transport = serial.LinuxSerialTransport(board.path)
            plugin = module.CytonSerialPlugin(devices.DeviceIdentity('fixture:publication', 'Cyton'),
                transport, reset_defaults=True, provenance=devices.Provenance.FIXTURE_REPLAY)
            try:
                generation = plugin.connect(deadline_ns=time.monotonic_ns()+2_000_000_000)
                plugin.start(generation, deadline_ns=time.monotonic_ns()+1_000_000_000)
                original = plugin.parser.feed
                def after_feed(data):
                    frames = original(data)
                    if frames:
                        plugin.request_cancel()
                    return frames
                plugin.parser.feed = after_feed
                with self.assertRaises(module.CytonSessionError,
                                       msg='cancelled parser output was published') as caught:
                    while True:
                        result = plugin.read(generation, deadline_ns=time.monotonic_ns()+1_000_000_000)
                        self.assertFalse(result, 'cancelled parser output was published')
                self.assertIsInstance(caught.exception.primary, serial.SerialCancelled)
                self.assertFalse(transport.connected)
                self.assertEqual(plugin.parser.buffered_bytes, 0)
            finally:
                plugin.close()

    def test_lifecycle_events_are_bounded_and_preserve_generation_and_provenance(self):
        module = importlib.import_module('cyton_serial_plugin')
        devices = importlib.import_module('xtos_devices')
        serial = importlib.import_module('serial_transport')
        with Board() as board:
            plugin = module.CytonSerialPlugin(devices.DeviceIdentity('fixture:events', 'Cyton'),
                serial.LinuxSerialTransport(board.path), reset_defaults=True,
                provenance=devices.Provenance.FIXTURE_REPLAY)
            self.assertTrue(hasattr(plugin, 'events'), 'bounded lifecycle events missing')
            for _ in range(24):
                generation = plugin.connect(deadline_ns=time.monotonic_ns()+2_000_000_000)
                plugin.start(generation, deadline_ns=time.monotonic_ns()+1_000_000_000)
                plugin.close()
            self.assertEqual(len(plugin.events), 64)
            self.assertEqual(plugin.events[-1].state, devices.Lifecycle.DISCONNECTED)
            self.assertEqual(plugin.events[-1].generation, generation)
            self.assertTrue(all(event.authentication is devices.Authentication.NOT_CHECKED and
                                event.provenance is devices.Provenance.FIXTURE_REPLAY
                                for event in plugin.events))

    def test_expired_active_read_stops_and_releases_session(self):
        module = importlib.import_module('cyton_serial_plugin')
        devices = importlib.import_module('xtos_devices')
        serial = importlib.import_module('serial_transport')
        with Board() as board:
            transport = serial.LinuxSerialTransport(board.path)
            plugin = module.CytonSerialPlugin(devices.DeviceIdentity('fixture:expired', 'Cyton'),
                transport, reset_defaults=True, provenance=devices.Provenance.FIXTURE_REPLAY)
            try:
                generation = plugin.connect(deadline_ns=time.monotonic_ns()+2_000_000_000)
                plugin.start(generation, deadline_ns=time.monotonic_ns()+1_000_000_000)
                with self.assertRaises(module.CytonSessionError):
                    plugin.read(generation, deadline_ns=1)
                self.assertFalse(transport.connected, 'expired active session retained tty')
                self.assertEqual(plugin.state, devices.Lifecycle.FAILED)
                self.assertFalse(plugin.protocol_match)
            finally:
                plugin.close()

    def test_invalid_handshake_never_starts_and_retains_primary_with_cleanup_failure(self):
        module = importlib.import_module('cyton_serial_plugin')
        devices = importlib.import_module('xtos_devices')
        serial = importlib.import_module('serial_transport')
        cases = ((BANNER.replace(b'v3.1.2', b'v3.1.3'), registers()),
                 (BANNER+b'junk', registers()), (BANNER, registers().replace(b'CH8SET', b'CH9SET')),
                 (b'A'*256+b'$$$', registers()))
        for banner, reply in cases:
            with self.subTest(banner=banner[:24]), Board(banner=banner, reply=reply) as board:
                transport = serial.LinuxSerialTransport(board.path)
                plugin = module.CytonSerialPlugin(devices.DeviceIdentity('fixture:invalid', 'Cyton'),
                    transport, reset_defaults=True, provenance=devices.Provenance.FIXTURE_REPLAY)
                original = transport.close
                def close_then_error():
                    original()
                    raise serial.SerialError('test cleanup error after real close')
                transport.close = close_then_error
                try:
                    with self.assertRaises(module.CytonSessionError) as caught:
                        plugin.connect(deadline_ns=time.monotonic_ns()+1_000_000_000)
                    self.assertIsNotNone(caught.exception.primary)
                    self.assertIsNotNone(caught.exception.cleanup_error)
                    self.assertFalse(transport.connected)
                    self.assertFalse(plugin.protocol_match)
                    self.assertEqual(plugin.state, devices.Lifecycle.FAILED)
                    self.assertNotIn(ord('b'), board.commands)
                finally:
                    transport.close = original
                    plugin.close()

    def test_bad_stream_and_partial_eof_fail_closed_without_publishing(self):
        module = importlib.import_module('cyton_serial_plugin')
        devices = importlib.import_module('xtos_devices')
        serial = importlib.import_module('serial_transport')
        for wire in (b'noise'+packet(1), packet(1, footer=0), packet(1)+packet(1),
                     packet(1)+packet(3)):
            with self.subTest(wire=wire[:5]), Board() as board:
                transport = serial.LinuxSerialTransport(board.path)
                plugin = module.CytonSerialPlugin(devices.DeviceIdentity('fixture:badstream', 'Cyton'),
                    transport, reset_defaults=True, provenance=devices.Provenance.FIXTURE_REPLAY)
                try:
                    generation = plugin.connect(deadline_ns=time.monotonic_ns()+1_000_000_000)
                    plugin.start(generation, deadline_ns=time.monotonic_ns()+1_000_000_000)
                    # Real descriptor read returns this bounded invalid batch at once.
                    os.write(board.master, wire)
                    with self.assertRaises(module.CytonSessionError):
                        plugin.read(generation, deadline_ns=time.monotonic_ns()+1_000_000_000)
                    self.assertFalse(transport.connected)
                    self.assertEqual(plugin.state, devices.Lifecycle.FAILED)
                finally:
                    plugin.close()

    def test_query_timeout_and_cancel_remain_bounded_and_clear_admission(self):
        module = importlib.import_module('cyton_serial_plugin')
        devices = importlib.import_module('xtos_devices')
        serial = importlib.import_module('serial_transport')
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled), Board(reply=b'') as board:
                transport = serial.LinuxSerialTransport(board.path)
                plugin = module.CytonSerialPlugin(devices.DeviceIdentity('fixture:query', 'Cyton'),
                    transport, reset_defaults=True, provenance=devices.Provenance.FIXTURE_REPLAY)
                worker = None
                if cancelled:
                    worker = threading.Thread(target=lambda: (time.sleep(.05), plugin.request_cancel()))
                    worker.start()
                started = time.monotonic()
                try:
                    with self.assertRaises(module.CytonSessionError) as caught:
                        plugin.connect(deadline_ns=time.monotonic_ns()+150_000_000)
                    self.assertLess(time.monotonic()-started, .75)
                    self.assertFalse(transport.connected)
                    self.assertFalse(plugin.protocol_match)
                    self.assertEqual(plugin.generation, 1)
                    self.assertIsInstance(caught.exception.primary,
                                          serial.SerialCancelled if cancelled else serial.SerialTimeout)
                finally:
                    if worker is not None:
                        worker.join(1)
                        self.assertFalse(worker.is_alive())
                    plugin.close()

    def test_cancel_at_profile_publication_refuses_connected_admission(self):
        module = importlib.import_module('cyton_serial_plugin')
        devices = importlib.import_module('xtos_devices')
        serial = importlib.import_module('serial_transport')
        with Board() as board:
            transport = serial.LinuxSerialTransport(board.path)
            plugin = module.CytonSerialPlugin(devices.DeviceIdentity('fixture:admission', 'Cyton'),
                transport, reset_defaults=True, provenance=devices.Provenance.FIXTURE_REPLAY)
            original = module.parse_registers
            def cancel_after_parse(data):
                config = original(data)
                plugin.request_cancel()
                return config
            try:
                with patch.object(module, 'parse_registers', cancel_after_parse):
                    with self.assertRaises(module.CytonSessionError,
                                           msg='cancelled profile admitted as connected') as caught:
                        plugin.connect(deadline_ns=time.monotonic_ns()+1_000_000_000)
                self.assertIsInstance(caught.exception.primary, serial.SerialCancelled)
                self.assertFalse(transport.connected)
                self.assertFalse(plugin.protocol_match)
            finally:
                plugin.close()

    def test_half_packet_then_real_pty_hangup_preserves_primary_and_cleanup_error(self):
        module = importlib.import_module('cyton_serial_plugin')
        devices = importlib.import_module('xtos_devices')
        serial = importlib.import_module('serial_transport')
        with Board(stream=packet(1)[:7]) as board:
            transport = serial.LinuxSerialTransport(board.path)
            plugin = module.CytonSerialPlugin(devices.DeviceIdentity('fixture:hangup', 'Cyton'),
                transport, reset_defaults=True, provenance=devices.Provenance.FIXTURE_REPLAY)
            try:
                generation = plugin.connect(deadline_ns=time.monotonic_ns()+1_000_000_000)
                plugin.start(generation, deadline_ns=time.monotonic_ns()+1_000_000_000)
                self.assertEqual(plugin.read(generation, deadline_ns=time.monotonic_ns()+1_000_000_000), ())
                board.disconnect()
                with self.assertRaises(module.CytonSessionError) as caught:
                    plugin.read(generation, deadline_ns=time.monotonic_ns()+1_000_000_000)
                self.assertIsInstance(caught.exception.primary, serial.SerialDisconnected)
                self.assertIsNotNone(caught.exception.cleanup_error)
                self.assertFalse(transport.connected)
                self.assertEqual(plugin.parser.buffered_bytes, 0)
                self.assertEqual(plugin.state, devices.Lifecycle.FAILED)
            finally:
                plugin.close()

    def test_explicit_reset_required_and_foreign_owner_cannot_touch_session(self):
        module = importlib.import_module('cyton_serial_plugin')
        devices = importlib.import_module('xtos_devices')
        serial = importlib.import_module('serial_transport')
        with Board() as board:
            transport = serial.LinuxSerialTransport(board.path)
            identity = devices.DeviceIdentity('fixture:owner', 'Cyton')
            with self.assertRaises(ValueError):
                module.CytonSerialPlugin(identity, transport, reset_defaults=False,
                                         provenance=devices.Provenance.FIXTURE_REPLAY)
            self.assertFalse(transport.connected)
            plugin = module.CytonSerialPlugin(identity, transport, reset_defaults=True,
                                             provenance=devices.Provenance.FIXTURE_REPLAY)
            try:
                generation = plugin.connect(deadline_ns=time.monotonic_ns()+1_000_000_000)
                errors = []
                def foreign():
                    for operation in (lambda: plugin.start(generation, deadline_ns=time.monotonic_ns()+1_000_000_000),
                                      lambda: plugin.read(generation, deadline_ns=time.monotonic_ns()+1_000_000_000),
                                      plugin.close):
                        try:
                            operation()
                        except module.CytonSessionError as error:
                            errors.append(str(error))
                worker = threading.Thread(target=foreign)
                worker.start()
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors), 3)
                self.assertTrue(transport.connected)
                self.assertEqual(plugin.state, devices.Lifecycle.CONNECTED)
                self.assertNotIn(ord('b'), board.commands)
            finally:
                plugin.close()

    def test_pty_cannot_be_labelled_live_device(self):
        module = importlib.import_module('cyton_serial_plugin')
        devices = importlib.import_module('xtos_devices')
        serial = importlib.import_module('serial_transport')
        with Board() as board:
            transport = serial.LinuxSerialTransport(board.path)
            with self.assertRaises(ValueError, msg='PTY accepted live-device provenance'):
                module.CytonSerialPlugin(devices.DeviceIdentity('caller:label', 'Cyton'),
                    transport, reset_defaults=True, provenance=devices.Provenance.LIVE_DEVICE)
            self.assertFalse(transport.connected)

    def test_pty_handshake_readback_precedes_stream_and_keeps_authentication_unchecked(self):
        self.assertIsNotNone(importlib.util.find_spec('cyton_serial_plugin'),
                             'real serial Cyton plugin API missing')
        module = importlib.import_module('cyton_serial_plugin')
        devices = importlib.import_module('xtos_devices')
        transport_module = importlib.import_module('serial_transport')
        with Board() as board:
            transport = transport_module.LinuxSerialTransport(board.path)
            plugin = module.CytonSerialPlugin(
                devices.DeviceIdentity('fixture:pty-cyton', 'OpenBCI Cyton'), transport,
                reset_defaults=True, provenance=devices.Provenance.FIXTURE_REPLAY)
            try:
                generation = plugin.connect(deadline_ns=time.monotonic_ns()+2_000_000_000)
                self.assertEqual(plugin.state, devices.Lifecycle.CONNECTED)
                self.assertTrue(plugin.protocol_match)
                self.assertEqual(plugin.configuration.gains, (24,)*8)
                self.assertEqual(plugin.configuration.sample_rate_hz, 250)
                self.assertEqual(plugin.authentication, devices.Authentication.NOT_CHECKED)
                self.assertNotIn(ord('b'), board.commands)
                plugin.start(generation, deadline_ns=time.monotonic_ns()+1_000_000_000)
                self.assertEqual(plugin.state, devices.Lifecycle.STREAMING)
                self.assertFalse(plugin.physical_device_evidence)
            finally:
                plugin.close()
        self.assertTrue(bytes(board.commands).startswith(b'sv?'))


class CytonSpecRegressionTests(unittest.TestCase):
    def make(self, board):
        module = importlib.import_module('cyton_serial_plugin')
        devices = importlib.import_module('xtos_devices')
        serial = importlib.import_module('serial_transport')
        transport = serial.LinuxSerialTransport(board.path)
        plugin = module.CytonSerialPlugin(devices.DeviceIdentity('fixture:spec', 'Cyton'),
            transport, reset_defaults=True, provenance=devices.Provenance.FIXTURE_REPLAY)
        return plugin, transport, module, devices, serial

    def test_fragmented_register_trailing_bytes_refuse_admission(self):
        with Board(reply=registers()+b'junk') as board:
            plugin, transport, module, devices, serial = self.make(board)
            original = transport.read
            reads = []
            def fragmented(max_bytes, **kwargs):
                data = original(1, **kwargs)
                reads.append(data)
                return data
            transport.read = fragmented
            try:
                with self.assertRaises(module.CytonSessionError,
                                       msg='one-byte EOT admitted pending trailing junk') as caught:
                    plugin.connect(deadline_ns=time.monotonic_ns()+1_500_000_000)
                self.assertIn('trailing', str(caught.exception.primary))
                self.assertTrue(reads, 'real descriptor fragmentation did not run')
                self.assertTrue(all(len(data) == 1 for data in reads))
                self.assertFalse(transport.connected)
                self.assertFalse(plugin.protocol_match)
                self.assertIsNone(plugin.configuration)
                self.assertEqual(plugin.state, devices.Lifecycle.FAILED)
                self.assertNotIn(ord('b'), board.commands)
            finally:
                plugin.close()

    def test_pending_bytes_before_start_refuse_stream_command(self):
        with Board() as board:
            plugin, transport, module, devices, serial = self.make(board)
            try:
                generation = plugin.connect(deadline_ns=time.monotonic_ns()+1_500_000_000)
                os.write(board.master, b'junk')
                writes = []
                original = transport.write_all
                def observed(data, **kwargs):
                    writes.append(data)
                    return original(data, **kwargs)
                transport.write_all = observed
                with self.assertRaises(module.CytonSessionError,
                                       msg='pending bytes allowed stream command'):
                    plugin.start(generation, deadline_ns=time.monotonic_ns()+1_000_000_000)
                self.assertNotIn(b'b', writes)
                self.assertFalse(transport.connected)
                self.assertFalse(plugin.protocol_match)
            finally:
                plugin.close()

    def test_expired_current_start_uses_independent_cleanup_budget(self):
        with Board() as board:
            plugin, transport, module, devices, serial = self.make(board)
            try:
                generation = plugin.connect(deadline_ns=time.monotonic_ns()+1_500_000_000)
                writes = []
                original = transport.write_all
                def observed(data, **kwargs):
                    writes.append((data, kwargs, time.monotonic_ns()))
                    return original(data, **kwargs)
                transport.write_all = observed
                # Invalid generation remains an admission precondition with no I/O.
                with self.assertRaisesRegex(module.CytonSessionError, 'generation'):
                    plugin.start(generation-1, deadline_ns=1)
                self.assertEqual(writes, [])
                self.assertTrue(transport.connected)
                self.assertIsNotNone(plugin.configuration)
                with self.assertRaises(module.CytonSessionError) as caught:
                    plugin.start(generation, deadline_ns=1)
                self.assertFalse(transport.connected, 'expired current start retained tty')
                self.assertIsInstance(caught.exception.primary, module.CytonSessionError)
                self.assertIn('deadline', str(caught.exception.primary))
                self.assertEqual(plugin.state, devices.Lifecycle.FAILED)
                self.assertFalse(plugin.protocol_match)
                self.assertIsNone(plugin.configuration)
                self.assertIsNone(plugin.firmware)
                self.assertEqual(len(writes), 1)
                data, kwargs, observed_ns = writes[0]
                self.assertEqual(data, b's')
                self.assertIs(kwargs['cancel_sensitive'], False)
                self.assertGreater(kwargs['deadline_ns'], observed_ns)
                self.assertLessEqual(kwargs['deadline_ns']-observed_ns, 100_000_000)
            finally:
                plugin.close()

    def test_old_cancel_latch_finishes_before_cleanup_and_reconnect(self):
        with Board() as board:
            plugin, transport, module, devices, serial = self.make(board)
            entered, release, cleanup_entered, close_entered = (threading.Event() for _ in range(4))
            errors, close_order = [], []
            original_close = transport.close
            original_cancel, original_cleanup = transport.request_cancel, plugin._cleanup
            worker = releaser = None
            try:
                old = plugin.connect(deadline_ns=time.monotonic_ns()+1_500_000_000)
                plugin.start(old, deadline_ns=time.monotonic_ns()+1_000_000_000)
                def delayed_cancel():
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError('cancel barrier timed out')
                    original_cancel()
                def observed_cleanup():
                    cleanup_entered.set()
                    return original_cleanup()
                def observed_close():
                    close_order.append(release.is_set())
                    close_entered.set()
                    return original_close()
                def cancel():
                    try:
                        plugin.request_cancel()
                    except BaseException as error:
                        errors.append(error)
                def resume():
                    if not cleanup_entered.wait(1):
                        errors.append(RuntimeError('owner never entered cleanup'))
                    # Broken code reaches close; protected cleanup waits for cancel.
                    close_entered.wait(.1)
                    release.set()
                transport.request_cancel = delayed_cancel
                transport.close = observed_close
                plugin._cleanup = observed_cleanup
                worker = threading.Thread(target=cancel)
                worker.start()
                self.assertTrue(entered.wait(1), 'real transport cancellation boundary missed')
                # A single pending byte drives the publication-cancel boundary
                # without a fragmented peer response leaking into the next open.
                self.assertEqual(os.write(board.master, b'\xa0'), 1)
                releaser = threading.Thread(target=resume)
                releaser.start()
                with self.assertRaises(module.CytonSessionError) as caught:
                    while True:
                        plugin.read(old, deadline_ns=time.monotonic_ns()+1_000_000_000)
                self.assertIsInstance(caught.exception.primary, serial.SerialCancelled)
                releaser.join(1)
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertFalse(releaser.is_alive())
                self.assertFalse(errors)
                self.assertEqual(close_order, [True],
                                 'old cancellation crossed tty cleanup before its transport latch')
                self.assertFalse(transport.connected)
                new = plugin.connect(deadline_ns=time.monotonic_ns()+1_500_000_000)
                self.assertEqual(new, old+1)
                self.assertFalse(plugin._cancel.is_set())
                self.assertFalse(transport._cancelled)
                plugin.start(new, deadline_ns=time.monotonic_ns()+1_000_000_000)
                self.assertEqual(plugin.state, devices.Lifecycle.STREAMING)
            finally:
                release.set()
                for thread in (worker, releaser):
                    if thread:
                        thread.join(2)
                transport.request_cancel = original_cancel
                transport.close = original_close
                plugin._cleanup = original_cleanup
                plugin.close()


if __name__ == '__main__':
    unittest.main()
