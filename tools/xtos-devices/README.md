# Phase 4 device plugins

Status: focused software slice, not phase acceptance. The source-only device package now contains the bounded Linux serial transport and the pinned Cyton protocol/session adapter. It is not a kernel serdev driver, a physical-device identity system, or a hard-real-time guarantee.

The installed source package is deliberately separate from the existing fixture-only `xtos_devices.py` contract. It contains:

- `serial_transport.py`: explicit Linux tty path, nonblocking raw 115200 8N1, TIOCEXCL, absolute deadlines, bounded reads/writes, owner-only close, cross-thread cancellation wakeup, and full fd/termios cleanup.
- `cyton_protocol.py`: text-only validation of the pinned Cyton v3.1.2 banner and the fixed default Board ADS register profile.
- `cyton_serial_plugin.py`: explicit `reset_defaults=True` session, `s`/`v`/`?` profile readback before `b`, canonical `openbci_cyton.CytonPacketParser` reuse, generation fencing, cancellation, bounded lifecycle events, and fail-closed cleanup.
- `openbci_cyton.py`: the existing canonical packet parser, included as a source dependency rather than copied or reimplemented.

The first accepted profile is intentionally narrow: eight enabled channels, gain 24, 250 Hz, ADS ID `0x3E`, and CONFIG1 `0x96`. `v` resets board settings; the adapter does not promise to restore prior board configuration. `b` and `s` have no board acknowledgement, so a successful write is not reported as proof that acquisition started or stopped.

Every sample records `fixture_replay` or an explicitly selected live provenance, `Authentication.NOT_CHECKED`, `no_action`, and `physical_device_evidence=False`. PTYs are fixture evidence. A tty path and firmware banner do not authenticate a physical board. Real USB/UART permissions, board identity, firmware compatibility, unplug/replug, sustained sampling, electrode/reference measurements, and physical latency remain unverified.

Focused commands:

```sh
python3 -B -m unittest discover -s tests -p 'test_xtos_devices.py' -v
python3 -B -m unittest discover -s tests -p 'test_xtos_serial_transport.py' -v
python3 -B -m unittest discover -s tests -p 'test_xtos_cyton_protocol.py' -v
python3 -B -m unittest discover -s tests -p 'test_xtos_cyton_serial_plugin.py' -v
python3 -B -O -m unittest discover -s tests -p 'test_xtos_serial_transport.py' -v
python3 -B -O -m unittest discover -s tests -p 'test_xtos_cyton_protocol.py' -v
python3 -B -O -m unittest discover -s tests -p 'test_xtos_cyton_serial_plugin.py' -v
```

The shared maintenance runner and source-only Live installer are separate gates. A source package can be imported with the locked runtime, but that does not prove it entered an ISO or that a physical board works. No credentials, signing keys, fixtures, test evidence, or authority state are installed.
