# Native BCI command

`bci` is a C serial producer/consumer tool. The serial session reads firmware identity and all eight ADS1299 channel settings before starting, then feeds the shared Cyton protocol parser into a fixed-capacity SPSC and eight independent 5–40Hz filters at 250Hz.

```
make -C tools/xtos-cyton
./tools/xtos-cyton/bci --device /dev/serial/by-id/DEVICE --output /path/new.jsonl --duration-ms 30000
./tools/xtos-cyton/bci --input synthetic.bin --output /path/new-fixture.jsonl
```

Output is create-only. Each JSONL record contains generation, sequence, host receive time, processing time, raw signed nV, eight filtered values and quality. Summary is one JSON document on stdout. Exit 0 means a clean software run, 3 means degraded/empty/error acquisition, 2 means invalid options/setup. SIGINT/TERM requests a bounded serial stop; no physical-device acceptance follows from successful software processing.

Fixture mode assumes gain24 explicitly. Serial mode requires actual channel register readback after an explicit `s`, `v`, `?` reset-default initialization on each connection. The firmware `v` command resets board/peripheral settings; it is not a passive identity query and this acquisition mode does not preserve a caller's pre-open configuration. Diagnostic sessions use a separate non-reset snapshot/restore transaction. Values that cannot fit signed32 nanovolts are rejected, never clipped. A frame with gaps, reconnects or queue loss resets filter history. Queue full rejects new frames; it never overwrites a consumer-owned frame. Host receive time may be shared by several packets in one read; it is not board acquisition time or proof of device-to-consumer latency.

Capacity, input bytes and output frame count are bounded. The output callback may be delayed by storage, so backpressure produces explicit loss rather than unbounded allocation. The tool is a soft-real-time implementation; no hardware, medical or EEG-classifier accuracy claim is made.

The production streaming engine, serial transport and CLI have separate test suites. PTY/synthetic tests do not establish physical board identity, radio behavior or target PREEMPT_RT latency.

## Configuration-preserving diagnostics

```
bci diagnose --device /dev/serial/by-id/DEVICE --mode shorted --channel 1 --output new-shorted.json
bci diagnose --device /dev/serial/by-id/DEVICE --mode impedance-p --channel 1 --output new-impedance-p.json
bci diagnose --device /dev/serial/by-id/DEVICE --mode impedance-n --channel 1 --output new-impedance-n.json
```

Each diagnostic opens an exclusive non-resetting serial session, snapshots all 24 ADS registers, verifies the requested temporary setup, collects one selected channel, then independently stops and restores the original writable configuration. Use only with an isolated input arrangement appropriate for the board and reference circuit. Options bound the retained window to 32–4096 frames, settling to 0–1024 frames and each command to 1–3000 ms; defaults are 1000, 250 and 3000 respectively. Official register replies are deliberately paced, so a short command timeout can reject an otherwise valid board.

JSON records the actual gain, before/test/after registers, selected raw samples, population statistics, errors and restoration result. SHORTED observes offset/noise without generating correction coefficients. Impedance-P/N uses the named nominal-current RMS estimate with 6 nA, 31.25 Hz and a 2200 ohm series assumption; this is not a traceable calibration. Missing/malformed/rail frames, interruption, disconnect or unverified restoration return status 3 and withhold valid statistics while retaining partial evidence. STOP alone does not disable lead-off current; cleanup explicitly disables excitation before restoring routing and re-enables saved excitation only after verified routing restoration. No automatic stream restart follows.

## Configuration and explicit offline calibration

`bci config --device /dev/xtos0` queries the kernel device's last verified configuration through the SDK. It emits the versioned gain/register/firmware snapshot as JSON; unsupported or unvalidated devices fail explicitly. The firmware string is not a unique board identity.

Offline calibration requires caller-supplied paired measurements in nanovolts. Each points CSV line contains eight observed signed-int32 values followed by eight known-reference values, with a final newline. Provide 3–64 pairs spanning at least three distinct levels in each selected channel. `--mask` selects channel bits 0–7; minimum observed/reference span and maximum fit residual are mandatory.

```
bci calibrate --points pairs.csv --session-file session.txt --firmware-file firmware.txt --reference-id reference-001 --source external-reference --mask 255 --min-span-nv 1000 --max-residual-nv 100 --output new.model
bci apply-calibration --model new.model --input raw.csv --session-file session.txt --firmware-file firmware.txt --reference-id reference-001 --source external-reference --output corrected.csv
```

Apply input has eight signed-int32 nV values per newline-terminated line, at most 4096 frames. Application is explicit and leaves the original raw file untouched. The model records exact-byte SHA-256 digests of the session descriptor, firmware description and paired CSV, named coefficients, measured spans, thresholds and an integrity digest. Apply requires identical context-file contents, source kind and reference ID. Changed, malformed or overflowing input fails before creating output. Both commands create new files only; incomplete writes return failure and retain the created partial file for inspection.

`--source fixture` labels synthetic supplied data. `external-reference` is a caller declaration; digests establish byte binding and model integrity, not authenticity, traceability or physical calibration acceptance. No acquisition command or shorted self-test automatically applies these coefficients. Exact fitted coefficients use a versioned named-text format with C-locale hexadecimal floating-point values, not an in-memory structure dump.
