"""Optional Linux Ganglion BLE transport; selection hints are not authentication.

Deadlines bound cooperative backends only: cancellation-resistant third-party
coroutines can exceed them. No physical-device evidence is established here.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import re
import time
import threading

from xtos_devices import Authentication, Capabilities, DeviceIdentity, Lifecycle, LifecycleEvent, Provenance, TransportKind
from ganglion_protocol import GanglionV2Decoder, PROFILE
from ganglion_plugin import GanglionSampleView

SERVICE_UUID = '0000fe84-0000-1000-8000-00805f9b34fb'
NOTIFY_UUID = '2d30c082-f39f-4ce6-923f-3484ea480596'
WRITE_UUID = '2d30c083-f39f-4ce6-923f-3484ea480596'


@dataclass(frozen=True)
class BlueZTarget:
    address: str
    adapter: str

    def __post_init__(self):
        if not isinstance(self.address, str) or not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', self.address):
            raise ValueError('uppercase six-octet colon MAC required without normalization')
        if not isinstance(self.adapter, str) or not re.fullmatch(r'hci[0-9]+', self.adapter):
            raise ValueError('literal hciN adapter required')

    @property
    def path(self):
        return f'/org/bluez/{self.adapter}/dev_{self.address.replace(":", "_")}'


def default_client_factory(target, disconnected_callback):
    """Resolve one explicit cached BlueZ device; never scan or request pairing."""
    import sys
    import importlib.metadata
    if sys.platform != 'linux':
        raise RuntimeError('Ganglion BLE default backend requires Linux BlueZ')
    try:
        version = importlib.metadata.version('bleak')
    except importlib.metadata.PackageNotFoundError as error:
        raise RuntimeError('optional dependency bleak==3.0.1 is required') from error
    if version != '3.0.1':
        raise RuntimeError(f'optional dependency bleak==3.0.1 required; found {version}')
    try:
        from bleak import BleakClient
        from bleak.backends.device import BLEDevice
    except ImportError as error:
        raise RuntimeError('bleak==3.0.1 optional backend dependencies unavailable') from error
    device = BLEDevice(target.address, None, details={'path': target.path, 'props': {}})
    return BleakClient(device, pair=False, bluez={'adapter': target.adapter},
                       disconnected_callback=disconnected_callback)


def _deadline(value):
    if type(value) is not int or value <= 0:
        raise ValueError('absolute positive integer monotonic_ns deadline required')


async def _until(deadline_ns, operation):
    remaining = deadline_ns - time.monotonic_ns()
    if remaining <= 0:
        raise TimeoutError('operation deadline expired')
    async with asyncio.timeout(remaining / 1_000_000_000):
        return await operation()


@dataclass(frozen=True)
class NotificationSnapshot:
    data: bytes
    host_receive_monotonic_ns: int


class GanglionBLETransport:
    kind = TransportKind.BLE
    physical_device_evidence = False
    authentication = Authentication.NOT_CHECKED
    action = 'no_action'

    def __init__(self, target, *, client_factory=None, cleanup_budget_ns=200_000_000):
        if not isinstance(target, BlueZTarget):
            raise ValueError('explicit BlueZTarget required')
        _deadline(cleanup_budget_ns)
        self.target = target
        self._factory = default_client_factory if client_factory is None else client_factory
        self.provenance = Provenance.FIXTURE_REPLAY if client_factory is not None else Provenance.LIVE_DEVICE
        self.cleanup_budget_ns = cleanup_budget_ns
        self.generation = 0
        self._client = self._notify = self._write = None
        self._queue = deque()
        # One bounded callback mailbox and one coalesced loop wake. Its lock
        # protects only admission/accounting, never an await or backend call.
        self._ingress_lock = threading.RLock()
        self._ingress = deque()
        self._ingress_error = None
        self._ingress_generation = 0
        self._ingress_count = 0
        self._drain_scheduled = False
        self._wake = None
        self.failure = None
        self._loop = None
        self._busy = False
        self._reading = False
        self._closed = True
        self._accept_callbacks = False
        self._active = None
        self._cleanup_task = None
        self._cleanup_errors = deque(maxlen=64)
        self._uncertain = False
        self._started = False

    @property
    def cleanup_errors(self):
        return tuple(self._cleanup_errors)

    def _owner(self):
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError('transport belongs to another owner loop')

    def _generation(self, generation):
        self._owner()
        if type(generation) is not int or generation != self.generation:
            raise RuntimeError('stale or invalid generation')

    def _begin(self, *, allow_cleanup=False):
        self._owner()
        if self._busy or (not allow_cleanup and self._cleanup_task is not None and not self._cleanup_task.done()):
            raise RuntimeError('lifecycle busy')
        self._busy = True
        self._active = asyncio.current_task()

    def _end(self):
        self._busy = False
        self._active = None

    def _fail(self, error):
        if self.failure is None:
            self.failure = error
        with self._ingress_lock:
            self._ingress.clear()
            self._ingress_error = None
            self._ingress_count = 0
            self._accept_callbacks = False
        self._queue.clear()
        if self._wake is not None:
            self._wake.set()

    def _callback(self, generation, operation, *args):
        loop = self._loop
        if loop is None:
            return
        try:
            owner = asyncio.get_running_loop() is loop
        except RuntimeError:
            owner = False
        # Account for pending and already-published notifications together.
        # Do not queue a closure/payload in asyncio before admission succeeds.
        with self._ingress_lock:
            if generation != self._ingress_generation or not self._accept_callbacks:
                return
            is_notification = operation == self._notification
            error = None
            if is_notification:
                data = args[1]
                if data is None:
                    error = RuntimeError('malformed notification: expected 1..20 bytes')
                elif self._ingress_count >= 64:
                    error = RuntimeError('notification queue overflow')
            else:
                error = args[0]
            if self._ingress_error is not None:
                return
            if error is not None:
                self._ingress.clear()
                self._ingress_error = error
                self._ingress_count = 0
            else:
                self._ingress.append((generation, operation, args))
                self._ingress_count += 1
            if not owner and not self._drain_scheduled:
                self._drain_scheduled = True
                try:
                    loop.call_soon_threadsafe(self._drain_ingress, True)
                except RuntimeError:
                    self._drain_scheduled = False
                    self._ingress.clear()
                    self._ingress_count = 0
                    if not loop.is_closed():
                        raise
        if owner:
            self._drain_ingress(False)

    def _drain_ingress(self, scheduled):
        # The scheduled flag stays set while delivery runs, so foreign threads
        # cannot fill the loop's ready queue while this callback is draining.
        with self._ingress_lock:
            if self._ingress_error is not None:
                error = self._ingress_error
                self._ingress_error = None
                self._ingress.clear()
                self._ingress_count = 0
                self._fail_after_ingress(error)
            else:
                batch = tuple(self._ingress)
                self._ingress.clear()
                for generation, operation, args in batch:
                    if generation == self.generation and self._accept_callbacks:
                        operation(*args)
            if scheduled:
                self._drain_scheduled = False

    def _fail_after_ingress(self, error):
        # Called with the ingress lock, on the owner loop only.
        if self.failure is None:
            self.failure = error
        self._accept_callbacks = False
        self._queue.clear()
        if self._wake is not None:
            self._wake.set()

    def _notification(self, sender, data, received_ns):
        if self.failure is not None:
            return
        if not isinstance(data, (bytes, bytearray)) or not 1 <= len(data) <= 20:
            self._fail(RuntimeError('malformed notification: expected 1..20 bytes'))
        elif len(self._queue) >= 64:
            self._fail(RuntimeError('notification queue overflow'))
        else:
            self._queue.append(NotificationSnapshot(bytes(data), received_ns))
            self._wake.set()

    def cancel(self):
        """Owner-loop cancellation latch; cleanup occurs in the active await."""
        self._owner()
        self._fail(RuntimeError('transport cancelled'))
        if self._active is not None:
            self._active.cancel()

    async def _cleanup(self):
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return await asyncio.shield(self._cleanup_task)
        async def run():
            with self._ingress_lock:
                self._accept_callbacks = False
                self._ingress.clear()
                self._ingress_error = None
                self._ingress_count = 0
            self._queue.clear()
            if self._wake is not None:
                self._wake.set()
            uncertain = False
            if self._client is not None:
                steps = []
                if self._started and self._write is not None:
                    steps.append(lambda: self._client.write_gatt_char(self._write, b's', response=False))
                if self._notify is not None:
                    steps.append(lambda: self._client.stop_notify(self._notify))
                steps.append(self._client.disconnect)
                for step in steps:
                    try:
                        await _until(time.monotonic_ns() + self.cleanup_budget_ns // len(steps), step)
                    except BaseException as error:
                        uncertain = True
                        self._cleanup_errors.append(f'{type(error).__name__}: {error}')
                try:
                    uncertain = uncertain or bool(self._client.is_connected)
                except Exception as error:
                    uncertain = True
                    self._cleanup_errors.append(f'connection status unavailable: {error}')
            self._uncertain = uncertain
            if not uncertain:
                self._started = False
        self._cleanup_task = asyncio.create_task(run())
        await asyncio.shield(self._cleanup_task)

    async def _cleanup_preserving_failure(self):
        try:
            await self._cleanup()
        except BaseException as error:
            # The owned shielded cleanup continues; reconnect remains fenced.
            self._cleanup_errors.append(f'cleanup wait interrupted: {type(error).__name__}: {error}')

    async def read(self, generation, deadline_ns):
        self._generation(generation)
        _deadline(deadline_ns)
        if self._reading:
            raise RuntimeError('one reader at a time')
        self._reading = True
        try:
            while True:
                self._drain_ingress(False)
                if self.failure is not None:
                    raise self.failure
                if self._closed:
                    raise RuntimeError('transport disconnected')
                if time.monotonic_ns() >= deadline_ns:
                    raise TimeoutError('read deadline expired')
                if self._queue:
                    with self._ingress_lock:
                        # An overflow admitted after the drain must be observed
                        # before publishing queued data as a healthy read.
                        if self._ingress_error is not None:
                            continue
                        snapshot = self._queue.popleft()
                        self._ingress_count -= 1
                        return snapshot
                self._wake.clear()
                await _until(deadline_ns, self._wake.wait)
        except BaseException as error:
            self._fail(error)
            if not self._busy:
                await self._cleanup_preserving_failure()
            raise
        finally:
            self._reading = False

    async def connect(self, deadline_ns):
        self._owner()
        _deadline(deadline_ns)
        self._begin()
        try:
            if not self._closed or self._uncertain or self._reading:
                raise RuntimeError('explicit disconnect and certain cleanup required before reconnect')
            if time.monotonic_ns() >= deadline_ns:
                raise TimeoutError('connect deadline expired')
            self.generation += 1
            generation = self.generation
            self.failure = None
            self._cleanup_errors.clear()
            self._queue.clear()
            self._wake = asyncio.Event()
            self._closed = False
            with self._ingress_lock:
                self._ingress_generation = generation
                self._ingress.clear()
                self._ingress_error = None
                self._ingress_count = 0
                self._accept_callbacks = True
            self._client = self._notify = self._write = None
            def disconnected(client):
                self._callback(generation, self._fail, RuntimeError('backend disconnected'))
            def notification(sender, data):
                received_ns = time.monotonic_ns()
                snapshot = bytes(data) if isinstance(data, (bytes, bytearray)) and 1 <= len(data) <= 20 else None
                self._callback(generation, self._notification, sender, snapshot, received_ns)
            try:
                self._client = self._factory(self.target, disconnected)
                await _until(deadline_ns, self._client.connect)
                services = [s for s in self._client.services if s.uuid == SERVICE_UUID]
                if len(services) != 1:
                    raise RuntimeError('GATT requires one resolved Ganglion service')
                chars = services[0].characteristics
                notify = [c for c in chars if c.uuid == NOTIFY_UUID]
                write = [c for c in chars if c.uuid == WRITE_UUID]
                if len(notify) != 1 or len(write) != 1 or 'notify' not in notify[0].properties or 'write-without-response' not in write[0].properties:
                    raise RuntimeError('GATT requires unique notify and write-without-response characteristics')
                self._notify, self._write = notify[0], write[0]
                await _until(deadline_ns, lambda: self._client.start_notify(self._notify, notification, bluez={'use_start_notify': True}))
                if self.failure is not None:
                    raise self.failure
            except BaseException as error:
                self._fail(error)
                await self._cleanup_preserving_failure()
                raise
            return self.generation
        finally:
            self._end()

    async def write(self, generation, command, deadline_ns):
        """Only pinned start/stop bytes; completion is not firmware acknowledgement."""
        self._generation(generation)
        _deadline(deadline_ns)
        if type(command) is not bytes or command not in (b'b', b's'):
            raise ValueError('unsupported command; only start/stop allowed')
        self._begin()
        try:
            if self.failure is not None:
                raise self.failure
            if self._closed or self._write is None:
                raise RuntimeError('transport disconnected')
            # A cancelled write can already have reached the peripheral.
            if command == b'b':
                self._started = True
            await _until(deadline_ns, lambda: self._client.write_gatt_char(self._write, command, response=False))
            if command == b's':
                self._started = False
            if self.failure is not None:
                raise self.failure
        except BaseException as error:
            self._fail(error)
            await self._cleanup_preserving_failure()
            raise
        finally:
            self._end()

    async def disconnect(self, generation, deadline_ns):
        self._generation(generation)
        _deadline(deadline_ns)
        self._begin(allow_cleanup=True)
        try:
            self._closed = True
            try:
                await self._cleanup()
            except BaseException as error:
                self._fail(error)
                raise
            if self._uncertain:
                self._fail(RuntimeError('backend cleanup uncertain'))
                raise RuntimeError('backend cleanup uncertain: ' + '; '.join(self.cleanup_errors))
            if time.monotonic_ns() >= deadline_ns:
                self._fail(TimeoutError('disconnect deadline expired'))
                raise TimeoutError('disconnect deadline expired; independent cleanup attempted')
        finally:
            self._end()


class GanglionBLESession:
    """Explicit v2.0.1 host session; GATT writes do not prove firmware identity."""

    capabilities = Capabilities(4, 200, 'nanovolt_nominal')
    physical_device_evidence = False
    authentication = Authentication.NOT_CHECKED
    action = 'no_action'
    transport_kind = TransportKind.BLE

    def __init__(self, identity, transport, *, profile):
        if not isinstance(identity, DeviceIdentity) or not isinstance(transport, GanglionBLETransport):
            raise ValueError('explicit DeviceIdentity and GanglionBLETransport required')
        if profile != PROFILE:
            raise ValueError('explicit openbci-ganglion-v2.0.1 profile required')
        self.identity = identity
        self.transport = transport
        self.profile = profile
        self.provenance = transport.provenance
        self.generation = 0
        self.state = Lifecycle.DISCONNECTED
        self.decoder = GanglionV2Decoder(profile=profile)
        self._controls = deque(maxlen=64)
        self._events = deque(maxlen=64)
        self._busy = False
        self._reading = False
        self._read_epoch = 0
        self._event(Lifecycle.DISCONNECTED, 'created')

    @property
    def controls(self):
        return tuple(self._controls)

    @property
    def events(self):
        return tuple(self._events)

    def _event(self, state, reason):
        self.state = state
        self._events.append(LifecycleEvent(state, self.generation, reason,
                                           self.authentication, self.provenance))

    def _check(self, generation):
        self.transport._generation(generation)
        if generation != self.generation:
            raise RuntimeError('stale session generation')

    def _begin(self):
        self.transport._owner()
        if self._busy:
            raise RuntimeError('session lifecycle busy')
        self._busy = True

    def _reset(self):
        self.decoder = GanglionV2Decoder(profile=self.profile)
        self._controls.clear()

    async def _failed(self, error, *, read_epoch=None):
        # Explicit disconnect owns the final lifecycle even if an old reader
        # resumes its failure handler after cleanup or a later connection.
        if read_epoch is not None and read_epoch != self._read_epoch:
            return
        self._reset()
        self._event(Lifecycle.FAILED, type(error).__name__)
        self.transport._fail(error)
        await self.transport._cleanup_preserving_failure()

    async def connect(self, deadline_ns):
        self.transport._owner()
        _deadline(deadline_ns)
        if self.state is not Lifecycle.DISCONNECTED:
            raise RuntimeError('explicit disconnect required before reconnect')
        if self._reading:
            raise RuntimeError('session reader still active')
        self._begin()
        try:
            self._reset()
            try:
                self.generation = await self.transport.connect(deadline_ns)
            except BaseException as error:
                self.generation = self.transport.generation
                await self._failed(error)
                raise
            self._event(Lifecycle.CONNECTED, 'GATT connected; authentication not checked')
            return self.generation
        finally:
            self._busy = False

    async def start(self, generation, deadline_ns):
        self._check(generation)
        _deadline(deadline_ns)
        if self.state is not Lifecycle.CONNECTED:
            raise RuntimeError('session must be connected before start')
        self._begin()
        try:
            try:
                await self.transport.write(generation, b'b', deadline_ns)
                self._event(Lifecycle.STREAMING, 'start write completed; firmware unverified')
            except BaseException as error:
                await self._failed(error)
                raise
        finally:
            self._busy = False

    async def read(self, generation, deadline_ns):
        self._check(generation)
        _deadline(deadline_ns)
        if self.state is not Lifecycle.STREAMING:
            raise RuntimeError('session is not streaming')
        if self._reading:
            raise RuntimeError('one session reader at a time')
        self._reading = True
        read_epoch = self._read_epoch
        try:
            notification = await self.transport.read(generation, deadline_ns)
            if read_epoch != self._read_epoch or generation != self.generation:
                raise RuntimeError('session read invalidated by disconnect')
            batch = self.decoder.feed_notification(notification.data, host_receive_monotonic_ns=notification.host_receive_monotonic_ns)
            self._controls.extend(batch.controls)
            return tuple(self._view(sample) for sample in batch.samples)
        except BaseException as error:
            await self._failed(error, read_epoch=read_epoch)
            raise
        finally:
            self._reading = False

    def _view(self, sample):
        return GanglionSampleView(
            identity=self.identity, transport_kind=self.transport_kind,
            capabilities=self.capabilities, generation=self.generation,
            authentication=self.authentication, provenance=self.provenance,
            sequence=sample.sequence, sample_number=sample.sequence,
            host_receive_monotonic_ns=sample.host_receive_monotonic_ns,
            timestamp_clock=sample.timestamp_clock, channels=sample.channels_nv,
            auxiliary=(bytes([sample.auxiliary_byte]) if sample.auxiliary_valid else b''),
            footer=sample.packet_id, packet_id=sample.packet_id,
            ring_index=sample.ring_index, sample_index=sample.sample_index,
            quality=sample.quality)

    async def disconnect(self, generation, deadline_ns):
        self._check(generation)
        _deadline(deadline_ns)
        self._begin()
        try:
            self._read_epoch += 1
            self._reset()
            try:
                await self.transport.disconnect(generation, deadline_ns)
            except BaseException as error:
                self._event(Lifecycle.FAILED, type(error).__name__)
                raise
            self._event(Lifecycle.DISCONNECTED, 'explicit disconnect completed')
        finally:
            self._busy = False

    def cancel(self):
        self.transport.cancel()
        self._reset()
        self._event(Lifecycle.FAILED, 'cancelled; explicit disconnect required')
