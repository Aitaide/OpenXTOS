# Phase-4 device plugin tracer slice

Status: **focused_pass_not_stage_acceptance**. This is a synchronous, CPU-only,
in-memory adapter, not a physical driver or device-authentication implementation.
It performs no serial/USB/BLE/network I/O, subprocess execution, credential handling,
or actuator operation. Serial, USB, BLE and network are contract labels only.

## Small API

`xtos_devices.py` exports immutable `DeviceIdentity`, `Capabilities`,
`LifecycleEvent` and `Sample` dataclasses; `TransportKind`, `Provenance`,
`Lifecycle`, `Authentication` enums; structural `Transport`/`DevicePlugin`
protocols; `PluginRegistry`, `SimulatedTransport` and `CytonPlugin`.

- `registry.register(name, factory)` rejects duplicates without replacing the old
  factory. `lookup(name)` rejects unknown names; no auto-discovery/import/I/O.
- `SimulatedTransport(sessions, kind=TransportKind.SERIAL)` consumes one supplied
  session on each connection. A session contains byte chunks, or exception objects
  for deterministic failure injection. Fixture callers must supply finite,
  nonblocking in-memory iterables, not I/O-performing callbacks/generators.
  `authentication` is an injected enum result (default `ACCEPTED`), not proof of
  board identity, credentials or cryptography.
- `CytonPlugin(identity, transport, monotonic_ns=...)` accepts an injected fixture
  transport. `connect()` returns a new generation, including authentication-rejected
  connection attempts. Active reconnect is refused; reconnect after cancellation
  or failure consumes the next session and resets the canonical parser.
- `start(generation)` requires accepted authentication and `CONNECTED` state.
  `read(generation)` requires `STREAMING`; stale/non-integer generations are
  rejected before touching the transport or the current session.
- Each read requests at most `MAX_READ_BYTES` (132 bytes). Over-capacity replies
  are refused before parser feed, even if an injected transport ignores the bound.
  At most four complete samples can be returned by one call. A valid partial packet
  returns an empty tuple; `b''` is terminal EOF, not a would-block indication.
- Malformed data, invalid footer, noise, duplicate/out-of-order counters, sequence
  gaps, EOF (including partial EOF), clock/parser exceptions and transport exceptions
  close the session, emit `FAILED`, and publish no samples from that failing read.
  Previously returned samples are not retroactively revoked.
- `cancel()` disconnects, clears parser state, emits `DISCONNECTED`, and permits
  reconnect. Calls are single-owner/synchronous: cancellation occurs between bounded
  calls, not by interrupting a blocking thread or a physical device.
- `events` is session lifecycle history with generation, authentication result and
  provenance. Samples preserve canonical sequence, sample number, host-monotonic
  receive timestamp/clock, channel nanovolts, AUX bytes and footer. Receive time is
  not device acquisition time or evidence of synchronization/physical latency.
- The enum distinguishes `fixture_replay` and `live_device`, but this implementation
  accepts **only fixture replay**. Every emitted sample is `no_action` with
  `physical_device_evidence=False`, irrespective of transport kind.

The canonical decoder is imported as `openbci_cyton.CytonPacketParser` and its
`CytonFrame` is converted without a second decoder. Provide the existing
`tools/xtos-eeg-pipeline` directory on `sys.path`/`PYTHONPATH`; tests do this
explicitly. No source in that directory was changed or copied into runtime code.

## Focused verification

From the repository root (stdlib only):

```sh
python3 -B -m unittest discover -s tests -p 'test_xtos_devices.py' -v
python3 -B -O -m unittest discover -s tests -p 'test_xtos_devices.py' -v
```

The final inventory is **11 unittest methods**, with additional subcases inside
methods. Nine scenarios were added before their corresponding implementation and
observed RED; contract/registry/Cyton entry REDs identify missing APIs, not proof
that every downstream assertion independently failed. Cancellation and injected
transport scenarios retain additional intermediate behavioral REDs. The tenth
split-packet/counter-wrap/all-kind compatibility method passed immediately and is
**verification-only, not a strict-TDD claim**. Raw evidence also preserves malformed
input REDs with uncaught TypeErrors, separately from the intended behavioral failures.

`evidence/` is locally ignored and retains each run's raw log, exact command,
exit/count/method inventory and SHA-256-bound pre-run snapshots of the test,
runtime (when present) and unchanged canonical dependency. The optional local
`evidence/run_suite.py` recorder is retained in this development checkout; it is
ignored and is not a shipped or fresh-checkout entrypoint. Use the discovery
commands above when that helper is absent.

When the local recorder is available, this shell block generates fresh labels
and records the current normal and optimized inventories:

```sh
run_label=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
python3 -B tools/xtos-devices/evidence/run_suite.py "current-normal-$run_label"
python3 -B -O tools/xtos-devices/evidence/run_suite.py "current-optimized-$run_label"
```

Each invocation records 11 methods. Labels are exclusive directories; rerun the
whole block to generate new labels. The retained `final-normal` and
`final-optimized` directories are historical 10-method evidence and do not
represent the current suite. Snapshots are historical evidence, not new
maintained decoder implementations.

The lifecycle repair records `transport_connect_failed` when connect cleanup
succeeds and `transport_cleanup_failed` only when cleanup itself raises. The
FAILED lifecycle event reasons are exactly `transport_connect_failed` and
`transport_cleanup_failed`. A new connection resets authentication to
`NOT_CHECKED` before transport connect.

The focused suite is wired into `scripts/test.sh`. SBOM records this directory
as an excluded source component because it is fixture-only and is not installed
as a physical device driver.

Not attempted: full maintained suite,
image build, QEMU, physical serial/USB/BLE/network transports, Ganglion, real
identity/authentication, concurrency, medical/RT or phase acceptance. Historical
`.build/xtos-roadmap-v1/` evidence remains outside this slice.
