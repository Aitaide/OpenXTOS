# Virtual EEG Replay

## CSV format

The first line must be exactly:

```text
# xtos-eeg-v1,channels=2,sample_rate_hz=250,unit=nanovolt
```

Each following row has exactly three comma-separated columns:

```text
timestamp_ns,ch0_nv,ch1_nv
```

Timestamps are integer nanoseconds and must be strictly increasing. At 250 Hz,
consecutive samples must be 4,000,000 ns apart, with 1% tolerance. Channel
values are finite numbers in nanovolts.

## Check

From the repository root:

```sh
python3 tools/xtos-eeg-replay/replay.py \
  --input tools/xtos-eeg-replay/fixtures/fixture.csv --check
```

A valid input prints a JSON summary. Invalid headers, column counts, timestamps,
or sampling intervals print an error and return a non-zero status.

## Test result

Command:

```sh
python3 -m unittest discover -s tools/xtos-eeg-replay -p 'test_replay.py' -v
```

The command passed all 3 tests covering one valid fixture, non-monotonic
timestamps, and an incorrect column count. Full output is in
`docs/virtual_eeg_replay_test_output.txt`.

This is a user-space CSV validation/replay utility. It is not a kernel device
and does not provide hardware validation.
