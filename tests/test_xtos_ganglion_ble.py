"""Host fake-GATT lifecycle tests; never opens a radio."""
import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'tools/xtos-devices'), str(ROOT/'tools/xtos-eeg-pipeline')]
import ganglion_ble as ble
from xtos_devices import Authentication, DeviceIdentity, Lifecycle, Provenance


def deadline():
    return time.monotonic_ns() + 1_000_000_000


class TargetTests(unittest.TestCase):
    def test_entry_module(self):
        self.assertTrue(ble.__doc__)

    def test_default_factory_linux_exact_version_no_scan(self):
        target = ble.BlueZTarget('AA:BB:CC:DD:EE:01', 'hci0')
        captured = []
        device_type = lambda *a, **kw: captured.append(('device', a, kw)) or object()
        client_type = lambda *a, **kw: captured.append(('client', a, kw)) or object()
        modules = {'bleak': SimpleNamespace(BleakClient=client_type), 'bleak.backends.device': SimpleNamespace(BLEDevice=device_type)}
        with patch.dict(sys.modules, modules), patch('importlib.metadata.version', return_value='3.0.1'), patch('sys.platform', 'linux'):
            callback = lambda c: None
            ble.default_client_factory(target, callback)
        self.assertEqual(captured[0], ('device', (target.address, None), {'details': {'path': target.path, 'props': {}}}))
        self.assertEqual(captured[1][2], {'pair': False, 'bluez': {'adapter': 'hci0'}, 'disconnected_callback': callback})
        with patch('sys.platform', 'darwin'), self.assertRaisesRegex(RuntimeError, 'Linux'):
            ble.default_client_factory(target, callback)
        with patch('sys.platform', 'linux'), patch('importlib.metadata.version', return_value='3.0.0'), self.assertRaisesRegex(RuntimeError, '3.0.1'):
            ble.default_client_factory(target, callback)

    def test_target_literal_validation_and_frozen_path(self):
        target = ble.BlueZTarget('AA:BB:CC:DD:EE:01', 'hci12')
        self.assertEqual(target.path, '/org/bluez/hci12/dev_AA_BB_CC_DD_EE_01')
        with self.assertRaises(FrozenInstanceError):
            target.address = '11:22:33:44:55:66'
        for address in ('aa:BB:CC:DD:EE:01', 'AA-BB-CC-DD-EE-01', ' AA:BB:CC:DD:EE:01', None):
            with self.subTest(address=address), self.assertRaises(ValueError):
                ble.BlueZTarget(address, 'hci0')
        for adapter in ('HCI0', '../hci0', 'hci', 'hci0\n', None):
            with self.subTest(adapter=adapter), self.assertRaises(ValueError):
                ble.BlueZTarget('AA:BB:CC:DD:EE:01', adapter)


class FakeClient:
    def __init__(self, target, disconnected_callback):
        self.target = target
        self.disconnected_callback = disconnected_callback
        self.is_connected = False
        self.calls = []
        self.notify = SimpleNamespace(uuid='2d30c082-f39f-4ce6-923f-3484ea480596', properties=['notify'])
        self.write = SimpleNamespace(uuid='2d30c083-f39f-4ce6-923f-3484ea480596', properties=['write-without-response'])
        self.services = [SimpleNamespace(uuid='0000fe84-0000-1000-8000-00805f9b34fb', characteristics=[self.notify, self.write])]
        self.hold = None
        self.stop_error = None
        self.disconnect_error = None

    async def connect(self):
        self.calls.append('connect')
        self.is_connected = True
        if self.hold:
            await self.hold.wait()

    async def start_notify(self, characteristic, callback, **kwargs):
        self.calls.append(('notify', characteristic, kwargs))
        self.callback = callback

    async def stop_notify(self, characteristic):
        self.calls.append(('stop_notify', characteristic))
        if self.stop_error:
            raise self.stop_error

    async def disconnect(self):
        self.calls.append('disconnect')
        if self.disconnect_error:
            raise self.disconnect_error
        self.is_connected = False
        self.disconnected_callback(self)

    async def write_gatt_char(self, characteristic, data, *, response):
        self.calls.append(('write', characteristic, data, response))

    def emit(self, data):
        self.callback(self.notify, data)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def make(self, configure=None, **kwargs):
        self.clients = []
        def factory(target, disconnected_callback):
            client = FakeClient(target, disconnected_callback)
            if configure:
                configure(client)
            self.clients.append(client)
            return client
        transport = ble.GanglionBLETransport(ble.BlueZTarget('AA:BB:CC:DD:EE:01', 'hci0'), client_factory=factory, **kwargs)
        return transport

    async def test_callbacks_from_non_owner_thread_are_marshaled_and_snapshotted(self):
        import threading
        transport = self.make()
        generation = await transport.connect(deadline())
        client = self.clients[-1]
        errors, times = [], []
        def emit():
            try:
                data = bytearray(b'abc')
                times.append(time.monotonic_ns())
                client.emit(data)
                times.append(time.monotonic_ns())
                data[:] = b'xyz'
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=emit)
        reader = None
        try:
            worker.start()
            worker.join(.2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(transport._queue), 0, 'foreign callback mutated owner queue')
            snapshot = await asyncio.wait_for(transport.read(generation, deadline()), .3)
            self.assertEqual(snapshot.data, b'abc')
            self.assertLessEqual(times[0], snapshot.host_receive_monotonic_ns)
            self.assertLessEqual(snapshot.host_receive_monotonic_ns, times[1])
            reader = asyncio.create_task(transport.read(generation, deadline()))
            await asyncio.sleep(0)
            await asyncio.wait_for(asyncio.to_thread(client.disconnected_callback, client), .3)
            with self.assertRaisesRegex(RuntimeError, 'backend disconnected'):
                await asyncio.wait_for(reader, .3)
            self.assertFalse(client.is_connected)
        finally:
            worker.join(.2)
            if reader is not None and not reader.done():
                reader.cancel()
                await asyncio.wait_for(asyncio.gather(reader, return_exceptions=True), .3)
            await asyncio.wait_for(transport.disconnect(generation, deadline()), .3)

    async def test_connect_timeout_is_cooperative_bounded_and_cleans(self):
        entered, release = asyncio.Event(), asyncio.Event()
        def configure(client):
            original = client.connect
            async def held_connect():
                await original()
                entered.set()
                await release.wait()
            client.connect = held_connect
        transport = self.make(configure, cleanup_budget_ns=20_000_000)
        task = asyncio.create_task(transport.connect(time.monotonic_ns() + 20_000_000))
        try:
            await asyncio.wait_for(entered.wait(), .2)
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(task, .3)
            self.assertIsInstance(transport.failure, TimeoutError)
            self.assertFalse(self.clients[-1].is_connected)
            self.assertTrue(transport._cleanup_task.done())
            self.assertFalse(transport._busy)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), .3)
            await asyncio.wait_for(transport.disconnect(transport.generation, deadline()), .3)

    async def test_start_notify_failure_preserves_primary_during_cleanup_errors(self):
        primary = OSError('start notify failed')
        async def fail_notify(*args, **kwargs):
            raise primary
        def configure(client):
            client.start_notify = fail_notify
            client.stop_error = OSError('cleanup stop failed')
        transport = self.make(configure)
        try:
            with self.assertRaises(OSError) as caught:
                await asyncio.wait_for(transport.connect(deadline()), .3)
            self.assertIs(caught.exception, primary)
            self.assertIs(transport.failure, primary)
            self.assertFalse(self.clients[-1].is_connected)
            self.assertTrue(transport._cleanup_task.done())
            self.assertIn('cleanup stop failed', '; '.join(transport.cleanup_errors))
        finally:
            self.clients[-1].stop_error = None
            await asyncio.wait_for(transport.disconnect(transport.generation, deadline()), .3)

    async def test_write_timeout_and_task_cancel_clean_stop_and_preserve_primary(self):
        for mode in ('timeout', 'task_cancel'):
            with self.subTest(mode=mode):
                transport = self.make(cleanup_budget_ns=20_000_000)
                generation = await transport.connect(deadline())
                client = self.clients[-1]
                entered, release = asyncio.Event(), asyncio.Event()
                original = client.write_gatt_char
                async def held_write(characteristic, data, *, response):
                    await original(characteristic, data, response=response)
                    if data == b'b':
                        entered.set()
                        await release.wait()
                client.write_gatt_char = held_write
                task = asyncio.create_task(transport.write(generation, b'b', time.monotonic_ns() + 20_000_000 if mode == 'timeout' else deadline()))
                try:
                    await asyncio.wait_for(entered.wait(), .2)
                    if mode == 'task_cancel':
                        task.cancel()
                    with self.assertRaises(TimeoutError if mode == 'timeout' else asyncio.CancelledError) as caught:
                        await asyncio.wait_for(task, .3)
                    self.assertIs(transport.failure, caught.exception)
                    self.assertIn(('write', client.write, b's', False), client.calls)
                    self.assertFalse(client.is_connected)
                    self.assertTrue(transport._cleanup_task.done())
                    self.assertFalse(transport._busy)
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), .3)
                    await asyncio.wait_for(transport.disconnect(generation, deadline()), .3)

    async def test_owner_cancel_during_connect_latches_and_cleans(self):
        entered, release = asyncio.Event(), asyncio.Event()
        def configure(client):
            original = client.connect
            async def held_connect():
                await original()
                entered.set()
                await release.wait()
            client.connect = held_connect
        transport = self.make(configure)
        task = asyncio.create_task(transport.connect(deadline()))
        try:
            await asyncio.wait_for(entered.wait(), .2)
            transport.cancel()
            primary = transport.failure
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, .3)
            self.assertIs(transport.failure, primary)
            self.assertRegex(str(primary), 'cancelled')
            self.assertFalse(self.clients[-1].is_connected)
            self.assertTrue(transport._cleanup_task.done())
            self.assertFalse(transport._busy)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), .3)
            await asyncio.wait_for(transport.disconnect(transport.generation, deadline()), .3)

    async def test_foreign_burst_admission_is_bounded_before_owner_loop_runs(self):
        import threading
        transport = self.make()
        generation = await transport.connect(deadline())
        client = self.clients[-1]
        loop = asyncio.get_running_loop()
        original = loop.call_soon_threadsafe
        scheduled = []
        errors = []
        def schedule(callback, *args, **kwargs):
            scheduled.append(callback)
            return original(callback, *args, **kwargs)
        def burst():
            try:
                for _ in range(4096):
                    client.emit(bytes(20))
            except BaseException as error:
                errors.append(error)
        with patch.object(loop, 'call_soon_threadsafe', side_effect=schedule):
            worker = threading.Thread(target=burst)
            worker.start()
            worker.join(1)
            self.assertFalse(worker.is_alive(), 'callback ingress blocked')
            self.assertEqual(errors, [])
            self.assertLessEqual(len(scheduled), 1,
                                 'unbounded owner-loop callback handles retained payloads')
        with self.assertRaisesRegex(RuntimeError, 'overflow'):
            await transport.read(generation, deadline())
        self.assertFalse(client.is_connected)
        await transport.disconnect(generation, deadline())

    async def test_foreign_capacity_boundary_retains_order_and_snapshots(self):
        import threading
        transport = self.make()
        generation = await transport.connect(deadline())
        client = self.clients[-1]
        errors = []
        def burst():
            try:
                for i in range(64):
                    data = bytearray([i])
                    client.emit(data)
                    data[0] = 255
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=burst)
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        for i in range(64):
            self.assertEqual((await transport.read(generation, deadline())).data, bytes([i]))
        await transport.disconnect(generation, deadline())

    async def test_ingress_capacity_counts_owner_queue_and_foreign_mailbox_together(self):
        import threading
        transport = self.make()
        generation = await transport.connect(deadline())
        client = self.clients[-1]
        for i in range(63):
            client.emit(bytes([i]))
        worker = threading.Thread(target=lambda: (client.emit(b'last'), client.emit(b'overflow')))
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        with transport._ingress_lock:
            self.assertLessEqual(len(transport._queue) + len(transport._ingress), 64)
        with self.assertRaisesRegex(RuntimeError, 'overflow'):
            await transport.read(generation, deadline())
        self.assertEqual(len(transport._queue), 0)
        await transport.disconnect(generation, deadline())

    async def test_stale_foreign_flood_does_not_schedule_or_poison_new_generation(self):
        import threading
        transport = self.make()
        old_generation = await transport.connect(deadline())
        old = self.clients[-1]
        await transport.disconnect(old_generation, deadline())
        generation = await transport.connect(deadline())
        current = self.clients[-1]
        loop = asyncio.get_running_loop()
        original = loop.call_soon_threadsafe
        scheduled = []
        def schedule(callback, *args, **kwargs):
            scheduled.append(callback)
            return original(callback, *args, **kwargs)
        def flood():
            for _ in range(4096):
                old.emit(bytes(20))
                old.disconnected_callback(old)
        with patch.object(loop, 'call_soon_threadsafe', side_effect=schedule):
            worker = threading.Thread(target=flood)
            worker.start()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(scheduled, [])
        current.emit(b'current')
        self.assertEqual((await transport.read(generation, deadline())).data, b'current')
        self.assertIsNone(transport.failure)
        await transport.disconnect(generation, deadline())

    async def test_notification_snapshots_are_immutable_bounded_and_timestamped(self):
        transport = self.make()
        generation = await transport.connect(deadline())
        client = self.clients[-1]
        data = bytearray(b'abc')
        before = time.monotonic_ns()
        client.emit(data)
        after = time.monotonic_ns()
        data[:] = b'xyz'
        snapshot = await transport.read(generation, deadline())
        self.assertEqual(snapshot.data, b'abc')
        self.assertLessEqual(before, snapshot.host_receive_monotonic_ns)
        self.assertLessEqual(snapshot.host_receive_monotonic_ns, after)
        with self.assertRaises(FrozenInstanceError):
            snapshot.data = b'other'
        for i in range(64):
            client.emit(bytes([i]))
        for i in range(64):
            self.assertEqual((await transport.read(generation, deadline())).data, bytes([i]))
        await transport.disconnect(generation, deadline())

    async def test_notification_malformed_overflow_disconnect_are_terminal(self):
        for fault in ('empty', 'oversize', 'type', 'overflow', 'disconnect'):
            with self.subTest(fault=fault):
                transport = self.make()
                generation = await transport.connect(deadline())
                client = self.clients[-1]
                client.emit(b'queued')
                if fault == 'overflow':
                    for _ in range(64):
                        client.emit(b'x')
                elif fault == 'disconnect':
                    client.is_connected = False
                    client.disconnected_callback(client)
                else:
                    client.emit({'empty': b'', 'oversize': b'x'*21, 'type': [1]}[fault])
                with self.assertRaisesRegex(RuntimeError, 'notification|disconnected'):
                    await transport.read(generation, deadline())
                await transport.disconnect(generation, deadline())

    async def test_service_validation_refuses_missing_duplicate_wrong_properties(self):
        def missing(c): c.services = []
        def duplicate_service(c): c.services *= 2
        def duplicate_notify(c): c.services[0].characteristics.append(c.notify)
        def wrong_notify(c): c.notify.properties = ['indicate']
        def wrong_write(c): c.write.properties = ['write']
        for configure in (missing, duplicate_service, duplicate_notify, wrong_notify, wrong_write):
            with self.subTest(case=configure.__name__):
                transport = self.make(configure)
                with self.assertRaisesRegex(RuntimeError, 'GATT'):
                    await transport.connect(deadline())
                self.assertFalse(self.clients[-1].is_connected)

    async def test_generation_fences_old_callbacks_and_stale_side_effects(self):
        transport = self.make()
        first = await transport.connect(deadline())
        old = self.clients[-1]
        with self.assertRaisesRegex(RuntimeError, 'disconnect'):
            await transport.connect(deadline())
        await transport.disconnect(first, deadline())
        second = await transport.connect(deadline())
        current = self.clients[-1]
        old.emit(b'old')
        old.disconnected_callback(old)
        current.emit(b'new')
        self.assertEqual((await transport.read(second, deadline())).data, b'new')
        before = list(current.calls)
        for operation in (transport.read, transport.disconnect):
            with self.assertRaisesRegex(RuntimeError, 'generation'):
                await operation(first, deadline())
        self.assertEqual(current.calls, before)
        await transport.disconnect(second, deadline())

    async def test_lifecycle_concurrency_refuses_and_disconnect_wakes_one_reader(self):
        gate = asyncio.Event()
        transport = self.make(lambda c: setattr(c, 'hold', gate))
        connecting = asyncio.create_task(transport.connect(deadline()))
        await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError, 'busy'):
            await asyncio.wait_for(transport.connect(deadline()), 0.05)
        gate.set()
        generation = await connecting
        reader = asyncio.create_task(transport.read(generation, deadline()))
        await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError, 'reader'):
            await transport.read(generation, deadline())
        await transport.disconnect(generation, deadline())
        with self.assertRaisesRegex(RuntimeError, 'disconnected'):
            await reader

    async def test_deadlines_validate_and_timeout_cleans_backend(self):
        transport = self.make()
        for invalid in (0, -1, True, 1.5, None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await transport.connect(invalid)
        self.assertEqual(self.clients, [])
        generation = await transport.connect(deadline())
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(transport.read(generation, time.monotonic_ns()+2_000_000), .1)
        self.assertFalse(self.clients[-1].is_connected)
        with self.assertRaisesRegex(RuntimeError, 'disconnect'):
            await transport.connect(deadline())
        await transport.disconnect(generation, deadline())

    async def test_cancel_latches_read_and_connect_task_cancellation_cleans(self):
        transport = self.make()
        generation = await transport.connect(deadline())
        reader = asyncio.create_task(transport.read(generation, deadline()))
        await asyncio.sleep(0)
        transport.cancel()
        with self.assertRaisesRegex(RuntimeError, 'cancel'):
            await reader
        self.assertFalse(self.clients[-1].is_connected)
        await transport.disconnect(generation, deadline())
        gate = asyncio.Event()
        transport = self.make(lambda c: setattr(c, 'hold', gate))
        task = asyncio.create_task(transport.connect(deadline()))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.clients[-1].is_connected)

    async def test_cleanup_errors_preserve_primary_and_block_reconnect(self):
        transport = self.make()
        generation = await transport.connect(deadline())
        client = self.clients[-1]
        client.stop_error = OSError('stop failed')
        client.disconnect_error = OSError('disconnect failed')
        client.emit(b'')
        with self.assertRaisesRegex(RuntimeError, 'malformed notification'):
            await transport.read(generation, deadline())
        self.assertEqual(len(transport.cleanup_errors), 2)
        self.assertIn('disconnect', client.calls)
        with self.assertRaisesRegex(RuntimeError, 'disconnect|cleanup'):
            await transport.connect(deadline())
        with self.assertRaisesRegex(RuntimeError, 'cleanup|malformed'):
            await transport.disconnect(generation, deadline())
        client.stop_error = client.disconnect_error = None
        await transport.disconnect(generation, deadline())
        self.assertIsNotNone(transport.failure)
        new = await transport.connect(deadline())
        await transport.disconnect(new, deadline())

    async def test_expired_disconnect_still_attempts_cleanup(self):
        transport = self.make()
        generation = await transport.connect(deadline())
        with self.assertRaises(TimeoutError):
            await transport.disconnect(generation, time.monotonic_ns()-1)
        self.assertFalse(self.clients[-1].is_connected)

    async def test_cross_loop_refuses_without_backend_side_effects(self):
        transport = self.make()
        generation = await transport.connect(deadline())
        before = list(self.clients[-1].calls)
        def other_loop():
            return asyncio.run(transport.disconnect(generation, deadline()))
        with self.assertRaisesRegex(RuntimeError, 'loop'):
            await asyncio.to_thread(other_loop)
        self.assertEqual(self.clients[-1].calls, before)
        await transport.disconnect(generation, deadline())

    async def test_primary_notification_failure_survives_cleanup_wait_cancellation(self):
        transport = self.make()
        generation = await transport.connect(deadline())
        client = self.clients[-1]
        entered = asyncio.Event()
        release = asyncio.Event()
        async def held_stop(characteristic):
            entered.set()
            await release.wait()
        client.stop_notify = held_stop
        client.emit(b'')
        reader = asyncio.create_task(transport.read(generation, deadline()))
        await entered.wait()
        reader.cancel()
        with self.assertRaisesRegex(RuntimeError, 'malformed notification'):
            await reader
        release.set()
        await transport.disconnect(generation, deadline())
        self.assertFalse(client.is_connected)

    async def test_disconnect_cancel_retains_primary_and_blocks_until_cleanup_finishes(self):
        transport = self.make()
        generation = await transport.connect(deadline())
        client = self.clients[-1]
        entered = asyncio.Event()
        release = asyncio.Event()
        original = client.stop_notify
        async def held_stop(characteristic):
            entered.set()
            await release.wait()
            await original(characteristic)
        client.stop_notify = held_stop
        task = asyncio.create_task(transport.disconnect(generation, deadline()))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsInstance(transport.failure, asyncio.CancelledError)
        with self.assertRaisesRegex(RuntimeError, 'busy|cleanup'):
            await transport.connect(deadline())
        release.set()
        await transport.disconnect(generation, deadline())
        self.assertFalse(client.is_connected)

    async def test_deadline_expiring_during_disconnect_is_reported_after_cleanup(self):
        transport = self.make()
        generation = await transport.connect(deadline())
        client = self.clients[-1]
        original = client.stop_notify
        async def delayed_stop(characteristic):
            await asyncio.sleep(.01)
            await original(characteristic)
        client.stop_notify = delayed_stop
        with self.assertRaises(TimeoutError):
            await transport.disconnect(generation, time.monotonic_ns()+1_000_000)
        self.assertFalse(client.is_connected)

    async def test_read_task_cancellation_runs_bounded_cleanup(self):
        transport = self.make(cleanup_budget_ns=20_000_000)
        generation = await transport.connect(deadline())
        client = self.clients[-1]
        async def blocked_stop(characteristic):
            await asyncio.Event().wait()
        client.stop_notify = blocked_stop
        reader = asyncio.create_task(transport.read(generation, deadline()))
        await asyncio.sleep(0)
        reader.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reader
        self.assertFalse(client.is_connected)
        self.assertTrue(transport.cleanup_errors)
        self.assertIsInstance(transport.failure, asyncio.CancelledError)
        client.stop_notify = FakeClient.stop_notify.__get__(client)
        await transport.disconnect(generation, deadline())

    async def test_restricted_write_uses_characteristic_no_response_and_stops_on_cleanup(self):
        transport = self.make()
        generation = await transport.connect(deadline())
        client = self.clients[-1]
        with self.assertRaisesRegex(ValueError, 'command'):
            await transport.write(generation, b'?', deadline())
        with self.assertRaisesRegex(RuntimeError, 'generation'):
            await transport.write(generation-1, b'b', deadline())
        await transport.write(generation, b'b', deadline())
        self.assertIn(('write', client.write, b'b', False), client.calls)
        with self.assertRaises(TimeoutError):
            await transport.disconnect(generation, time.monotonic_ns()-1)
        self.assertIn(('write', client.write, b's', False), client.calls)
        self.assertLess(client.calls.index(('write', client.write, b's', False)), client.calls.index('disconnect'))

    async def test_gatt_connect_exact_characteristics_and_provenance(self):
        transport = self.make()
        self.assertEqual(self.clients, [])
        self.assertIs(transport.provenance, Provenance.FIXTURE_REPLAY)
        generation = await transport.connect(deadline())
        client = self.clients[-1]
        self.assertEqual(generation, 1)
        self.assertEqual(client.calls[1], ('notify', client.notify, {'bluez': {'use_start_notify': True}}))
        self.assertIs(transport.authentication, Authentication.NOT_CHECKED)
        self.assertFalse(transport.physical_device_evidence)
        self.assertEqual(transport.action, 'no_action')
        await transport.disconnect(generation, deadline())
        self.assertFalse(client.is_connected)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **kwargs):
        self.clients = []
        def factory(target, disconnected_callback):
            client = FakeClient(target, disconnected_callback)
            self.clients.append(client)
            return client
        transport = ble.GanglionBLETransport(ble.BlueZTarget('AA:BB:CC:DD:EE:01', 'hci0'), client_factory=factory)
        return ble.GanglionBLESession(DeviceIdentity('ganglion-fixture', 'Ganglion'), transport,
                                      profile='openbci-ganglion-v2.0.1', **kwargs)

    async def test_disconnect_does_not_become_failed_when_old_reader_finishes(self):
        session = self.make()
        generation = await session.connect(deadline())
        await session.start(generation, deadline())
        entered, release = asyncio.Event(), asyncio.Event()
        original = session._failed
        async def held_failed(error, **kwargs):
            entered.set()
            await asyncio.wait_for(release.wait(), .5)
            await original(error, **kwargs)
        reader = None
        try:
            with patch.object(session, '_failed', held_failed):
                reader = asyncio.create_task(session.read(generation, deadline()))
                await asyncio.sleep(0)
                await asyncio.wait_for(session.disconnect(generation, deadline()), .3)
                await asyncio.wait_for(entered.wait(), .3)
                release.set()
                with self.assertRaisesRegex(RuntimeError, 'disconnected'):
                    await asyncio.wait_for(reader, .3)
            self.assertIs(session.state, Lifecycle.DISCONNECTED)
            self.assertIs(session.events[-1].state, Lifecycle.DISCONNECTED)
            newer = await session.connect(deadline())
            await session.start(newer, deadline())
            self.clients[-1].emit(b'\x00' + bytes(12) + b'ABCDEFG')
            sample, = await session.read(newer, deadline())
            self.assertEqual(sample.generation, newer)
            self.assertTrue(session.decoder.seeded)
        finally:
            release.set()
            if reader is not None and not reader.done():
                reader.cancel()
                await asyncio.wait_for(asyncio.gather(reader, return_exceptions=True), .3)
            await asyncio.wait_for(session.disconnect(session.generation, deadline()), .3)

    async def test_connect_refuses_until_disconnected_session_reader_finishes(self):
        session = self.make()
        generation = await session.connect(deadline())
        await session.start(generation, deadline())
        entered, release = asyncio.Event(), asyncio.Event()
        original = session._failed
        async def held_failed(error, **kwargs):
            entered.set()
            await asyncio.wait_for(release.wait(), .5)
            await original(error, **kwargs)
        reader = None
        try:
            with patch.object(session, '_failed', held_failed):
                reader = asyncio.create_task(session.read(generation, deadline()))
                await asyncio.sleep(0)
                await asyncio.wait_for(session.disconnect(generation, deadline()), .3)
                await asyncio.wait_for(entered.wait(), .3)
                decoder, events = session.decoder, session.events
                with self.assertRaisesRegex(RuntimeError, 'reader|busy'):
                    await asyncio.wait_for(session.connect(deadline()), .3)
                self.assertIs(session.decoder, decoder)
                self.assertEqual(session.events, events)
                self.assertEqual(session.generation, generation)
                release.set()
                with self.assertRaisesRegex(RuntimeError, 'disconnected'):
                    await asyncio.wait_for(reader, .3)
            newer = await session.connect(deadline())
            await session.start(newer, deadline())
            self.clients[-1].emit(b'\x00' + bytes(12) + b'ABCDEFG')
            sample, = await session.read(newer, deadline())
            self.assertEqual(sample.generation, newer)
            self.assertTrue(session.decoder.seeded)
            self.assertIsNone(session.transport.failure)
        finally:
            release.set()
            if reader is not None and not reader.done():
                reader.cancel()
                await asyncio.wait_for(asyncio.gather(reader, return_exceptions=True), .3)
            await asyncio.wait_for(session.disconnect(session.generation, deadline()), .3)

    async def test_controls_are_bounded_in_streaming_state(self):
        session = self.make()
        generation = await session.connect(deadline())
        await session.start(generation, deadline())
        for _ in range(70):
            self.clients[-1].emit(b'\xc912Z')
            await session.read(generation, deadline())
        self.assertEqual(len(session.controls), 64)
        await session.disconnect(generation, deadline())

    async def test_decoder_failure_disconnects_and_invalidates_session(self):
        session = self.make()
        generation = await session.connect(deadline())
        await session.start(generation, deadline())
        self.clients[-1].emit(b'\xff')
        with self.assertRaises(ValueError):
            await session.read(generation, deadline())
        self.assertIs(session.state, Lifecycle.FAILED)
        self.assertFalse(self.clients[-1].is_connected)
        await session.disconnect(generation, deadline())

    async def test_controls_failure_reset_reconnect_and_bounded_events(self):
        session = self.make()
        generation = await session.connect(deadline())
        with self.assertRaisesRegex(RuntimeError, 'streaming'):
            await session.read(generation, deadline())
        await session.start(generation, deadline())
        client = self.clients[-1]
        for _ in range(70):
            client.emit(b'\xc912Z')
            self.assertEqual(await session.read(generation, deadline()), ())
        self.assertEqual(len(session.controls), 64)
        self.assertEqual(session.controls[-1].units, 'unverified')
        client.emit(b'\x00'+bytes(12)+b'ABCDEFG')
        await session.read(generation, deadline())
        client.emit(b'\xff')
        with self.assertRaisesRegex(ValueError, 'unknown'):
            await session.read(generation, deadline())
        self.assertEqual(session.controls, ())
        self.assertFalse(session.decoder.seeded)
        self.assertIs(session.state, Lifecycle.FAILED)
        self.assertFalse(client.is_connected)
        await session.disconnect(generation, deadline())
        newer = await session.connect(deadline())
        self.assertGreater(newer, generation)
        self.assertEqual(session.controls, ())
        await session.start(newer, deadline())
        self.clients[-1].emit(b'\x01'+bytes(19))
        with self.assertRaisesRegex(ValueError, 'seed'):
            await session.read(newer, deadline())
        await session.disconnect(newer, deadline())
        for _ in range(22):
            newer = await session.connect(deadline())
            await session.start(newer, deadline())
            await session.disconnect(newer, deadline())
        self.assertEqual(len(session.events), 64)

    async def test_session_cancel_resets_controls_and_wakes_read(self):
        session = self.make()
        generation = await session.connect(deadline())
        await session.start(generation, deadline())
        self.clients[-1].emit(b'\xc912Z')
        await session.read(generation, deadline())
        reader = asyncio.create_task(session.read(generation, deadline()))
        await asyncio.sleep(0)
        session.cancel()
        self.assertEqual(session.controls, ())
        with self.assertRaisesRegex(RuntimeError, 'cancel'):
            await reader
        self.assertFalse(self.clients[-1].is_connected)
        await session.disconnect(generation, deadline())

    async def test_session_refuses_wrong_profile_duplicate_start_and_stale_token(self):
        session = self.make()
        with self.assertRaises(ValueError):
            ble.GanglionBLESession(session.identity, session.transport, profile='v3')
        generation = await session.connect(deadline())
        before = list(self.clients[-1].calls)
        with self.assertRaisesRegex(RuntimeError, 'generation'):
            await session.start(generation-1, deadline())
        self.assertEqual(self.clients[-1].calls, before)
        await session.start(generation, deadline())
        with self.assertRaisesRegex(RuntimeError, 'connected'):
            await session.start(generation, deadline())
        await session.disconnect(generation, deadline())

    async def test_session_sample_uses_original_callback_time_and_metadata(self):
        session = self.make()
        generation = await session.connect(deadline())
        await session.start(generation, deadline())
        client = self.clients[-1]
        before = time.monotonic_ns()
        client.emit(b'\x00' + bytes(12) + b'ABCDEFG')
        after = time.monotonic_ns()
        await asyncio.sleep(.001)
        sample, = await session.read(generation, deadline())
        from ganglion_plugin import GanglionSampleView
        self.assertIsInstance(sample, GanglionSampleView)
        self.assertEqual(sample.channels, (0, 0, 0, 0))
        self.assertEqual(sample.generation, generation)
        self.assertIs(sample.provenance, Provenance.FIXTURE_REPLAY)
        self.assertIs(sample.authentication, Authentication.NOT_CHECKED)
        self.assertFalse(sample.physical_device_evidence)
        self.assertEqual(sample.action, 'no_action')
        self.assertLessEqual(before, sample.host_receive_monotonic_ns)
        self.assertLessEqual(sample.host_receive_monotonic_ns, after)
        with self.assertRaises(FrozenInstanceError):
            sample.generation = 7
        await session.disconnect(generation, deadline())
        self.assertIn(('write', client.write, b'b', False), client.calls)
        self.assertIn(('write', client.write, b's', False), client.calls)
        self.assertIs(session.state, Lifecycle.DISCONNECTED)


if __name__ == '__main__':
    unittest.main()