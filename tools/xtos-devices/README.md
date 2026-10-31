# Phase 4 device plugins

Status: focused software slices, not phase acceptance and not P4 phase acceptance. The source-only device package contains the bounded Linux serial transport, pinned Cyton adapter, an offline Ganglion v2.0.1 codec and fixture adapter, and the optional `ganglion_ble.py` Linux BlueZ/Bleak async transport/session. The optional BLE source implementation has focused host/fixture coverage; independent review, complete maintenance, physical BLE, image and guest acceptance are separate gates. It is not a kernel serdev driver, physical-device identity system, or hard-real-time guarantee.

The installed source package is deliberately separate from the existing fixture-only `xtos_devices.py` contract. It contains:

- `serial_transport.py`: explicit Linux tty path, nonblocking raw 115200 8N1, TIOCEXCL, absolute deadlines, bounded reads/writes, owner-only close, cross-thread cancellation wakeup, and full fd/termios cleanup.
- `cyton_protocol.py`: text-only validation of the pinned Cyton v3.1.2 banner and the fixed default Board ADS register profile.
- `cyton_serial_plugin.py`: explicit `reset_defaults=True` session, `s`/`v`/`?` profile readback before `b`, canonical `openbci_cyton.CytonPacketParser` reuse, generation fencing, cancellation, bounded lifecycle events, and fail-closed cleanup.
- `ganglion_protocol.py`: explicit v2.0.1 raw-seed and 18/19-bit delta decoder, bounded impedance/verbose text controls, sequence-gap invalidation and nominal integer-nV values. Compressed deltas quantize and accumulate error; no full24 reconstruction claim.
- `ganglion_plugin.py`: fixture-only BLE-kind adapter over the offline decoder. It performs no scan, pairing, connection to BLE hardware, or authentication.
- `ganglion_ble.py`: optional single-loop async Linux BLE transport and session, with explicit BlueZ target, absolute deadlines, bounded notification queue, generation-fenced callbacks and fail-closed cleanup. It requires caller-provisioned Bleak 3.0.1 for the default backend; no automatic scan, pairing, arbitrary commands or authentication. Injected GATT clients always remain fixture evidence. See [the BLE contract](../../docs/ganglion-linux-ble.md).
- `openbci_cyton.py`: the existing canonical packet parser, included as a source dependency rather than copied or reimplemented.

Official v2.0.1 encoder methods were compiled on the host and compared byte-for-byte with an independent bitword oracle. Maintained fixtures preserve boundary vectors and two source-composed 100-packet streams. This proves neither a decoder against actual device traffic nor BLE, firmware identity, reconnect or physical acquisition. The 18-bit frame's final byte is an accelerometer value only at ring slots 1, 2 and 3 modulo 10; at other slots firmware leaves a stale value, so this adapter reports it as absent. Impedance ASCII is kept opaque because source and documentation disagree on its units. The documentation's reported sample-rate enum is distinct from an independently verified run-time board rate, which is not claimed here.

The Cyton profile is intentionally narrow: eight enabled channels, gain 24, 250 Hz, ADS ID `0x3E`, and CONFIG1 `0x96`. `v` resets board settings; the adapter does not promise to restore prior board configuration. `b` and `s` have no board acknowledgement, so a successful write is not reported as proof that acquisition started or stopped. Ganglion samples use 200 samples/second as the version-pinned source's declared enum, not measured timing evidence.

Every sample records `fixture_replay` or an explicitly selected live provenance, `Authentication.NOT_CHECKED`, `no_action`, and `physical_device_evidence=False`. PTYs are fixture evidence. A tty path and firmware banner do not authenticate a physical board. Real USB/UART permissions, board identity, firmware compatibility, unplug/replug, sustained sampling, electrode/reference measurements, and physical latency remain unverified.

Focused commands:

```sh
python3 -B -m unittest discover -s tests -p 'test_xtos_devices.py' -v
python3 -B -m unittest discover -s tests -p 'test_xtos_serial_transport.py' -v
python3 -B -m unittest discover -s tests -p 'test_xtos_cyton_protocol.py' -v
python3 -B -m unittest discover -s tests -p 'test_xtos_cyton_serial_plugin.py' -v
python3 -B -m unittest discover -s tests -p 'test_xtos_ganglion_protocol.py' -v
python3 -B -m unittest discover -s tests -p 'test_xtos_ganglion_plugin.py' -v
python3 -B -m unittest discover -s tests -p 'test_xtos_ganglion_stream.py' -v
python3 -B -m unittest discover -s tests -p 'test_xtos_ganglion_ble.py' -v
python3 -B -O -m unittest discover -s tests -p 'test_xtos_serial_transport.py' -v
python3 -B -O -m unittest discover -s tests -p 'test_xtos_cyton_protocol.py' -v
python3 -B -O -m unittest discover -s tests -p 'test_xtos_cyton_serial_plugin.py' -v
python3 -B -O -m unittest discover -s tests -p 'test_xtos_ganglion_protocol.py' -v
python3 -B -O -m unittest discover -s tests -p 'test_xtos_ganglion_plugin.py' -v
python3 -B -O -m unittest discover -s tests -p 'test_xtos_ganglion_stream.py' -v
python3 -B -O -m unittest discover -s tests -p 'test_xtos_ganglion_ble.py' -v
```

The shared maintenance runner and source-only Live installer are separate gates. A source package can be imported with the locked runtime, but that does not prove it entered an ISO or that a physical board works. No credentials, signing keys, fixtures, test evidence, or authority state are installed.
