# Cyton driver host regression

Run from the repository root:

```sh
python3 -m unittest discover -s tests -p test_cyton_driver.py -v
```

`driver_test.c` includes the actual production driver and its shared packet parser.
`shim.h` substitutes kernel services with pthread locks and deterministic serial/ring
fault injection. The tests never operate a physical device.

Cases cover callback-before-character-open; START receive inside synchronous TX;
missing ringbuffer; repeated START/STOP; all channel enable/disable commands;
full/short/error command writes; unsupported calibration/impedance/high rates;
split frames, counter wrap, multiple gaps, duplicates and backwards counters;
ringbuffer errors; failed START rollback/stop; reopen; blocked control TX versus
receive/cleanup; and cleanup versus an in-flight ringbuffer write. Every serial TX
asserts the parser/receive state lock is unowned.

START and STOP are idempotent successes without redundant commands. A failed START
excludes subsequent receive and attempts STOP while preserving the START error.
An unsuccessful STOP still excludes host receive; no hardware-stop claim follows.
Gap loss counts channel samples once; rejected parser candidates increment errors
without inventing a known missing-sample count. Only 250 Hz is supported on this
115200-baud transport. Channel commands follow https://docs.openbci.com/Cyton/CytonSDK/.

Evidence under `.build/cyton-kernel-closure-v1/driver/` includes original compiler
RED, callback-before-open SIGSEGV RED, behavioral RED logs and final green logs.
`red-failed_start.log` is a positive control (it already passed after the START fix),
not defect RED evidence. Historical logs refer to earlier test/source revisions.

P2 frame producer cases capture every production bridge record and its exact
384-byte config, without emulating frame-ring cursor/snapshot behavior. They
check callback-time sampling under state_lock (equal batch timestamps and final
fragment time), raw ADC endpoints, modulo gaps despite saturated parser totals,
resynchronization, retained generations, failed publication before legacy writes,
START failure/cancellation, and terminal wakes. Sequence is zero at this bridge;
the core assigns lifetime sequence. Production initialization requests 500 slots.
A legacy write failure cannot mutate an already-published immutable frame.
The conversion_fault case now requires START to return the synchronous RX error
(-ERANGE); its remaining fault assertions and all 51 original cases are retained.
Evidence and source/dependency hashes: `.build/xtos-roadmap-v1/p2-driver/`.

Scope is driver source and executable host simulation. Kernel module integration
is a separate gate. The existing BCI-core open-FD, unbind and VMA lifetime design
is outside these tests; passing them does not establish hotplug or hardware safety.
