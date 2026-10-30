# P2 frame-to-window adapter v1

This P3 slice connects the existing P2 integer-frame SDK to the reviewed portable EEG window. It is a CPU data adapter; it does not authorize feedback, load a model, own device start/stop, or establish hardware timing.

## Ownership and API

`tools/xtos-eeg-stream/eeg_stream.h` defines caller-owned fixed storage. One caller must serialize access. Inputs and outputs must not overlap state. Initialize with an explicit nonzero caller session ID and window options for the current P2 8-channel, 250 Hz contract. Window size, hop and whole-window receive-age limit remain explicit operator inputs. No allocation, device open, STOP or close occurs in adapter calls; the existing SDK owns its session and preallocated read storage.

`accept_batch` consumes an actual `xtos_bci_frame_batch`. Validate the entire bounded batch before pushing its first sample. Verify P2 configuration version, flags/reserved fields, readback capabilities, register/gain/enabled-mask consistency and bounded firmware identity. Verify cursor arithmetic, record version/size, units, clock, generations, source, mask, sequence, counter/missing flags, reserved/unused positions, homogeneous source and nondecreasing callback receive observations. Copy eight nanovolt samples without float conversion. Never substitute legacy float reads, default gain or fixture metadata for missing P2 data. A zero-enabled-channel configuration is valid P2 metadata but yields no feature window.

`read` calls actual `xtos_bci_frame_read` with NONBLOCK and capacity 1..292, without owning START/STOP. EAGAIN/EWOULDBLOCK leaves state unchanged. Any other read failure invalidates accumulated data and advances the barrier, returning the original negative errno. Null caller arguments or invalid capacity return EINVAL without issuing a read or changing state. The caller must notify the adapter of stop/disconnect/cancellation through `barrier` if it occurs outside this read path.

A clean zero-frame SDK result must have zero cursor/next/stream/overwrite metadata and a zero configuration; the records pointer is ignored. It advances the barrier, clears the window and returns ENODATA. A malformed batch advances the barrier, clears the window and returns EPROTO; no valid prefix may survive a malformed tail. Caller output bytes remain unchanged on every failed feature extraction.

## Continuity and invalidation

The adapter starts with barrier ID 1. Valid window continuity resets, dropped samples, explicit barriers, clean EOF, malformed input, and nontransient read errors advance it. A valid current sample can seed the new window according to the portable primitive contract. A changed all-zero enabled mask drops the batch and clears accumulation. Sequence gaps and quality faults never produce a feature spanning the boundary. Equal receive timestamps remain valid; host callback time is not device acquisition time.

Successful extraction returns a copied `xtos_eeg_features` plus the current barrier ID. It retains the exact source flag, mask, session/stream/config/window IDs, sequences and receive observations. The adapter must also advance the barrier when the window rejects a stale extraction. Extraction eligibility and numerical mapping remain `xtos.eeg.mean-rms.v1`: eight means followed by eight RMS values in microvolts, no hidden filter. SYNTHETIC remains SYNTHETIC. SERDEV describes the driver source flag, not authenticated physical identity or trained-model accuracy.

Explicit rebind requires a strictly newer nonzero caller session ID, clears all accumulation and advances the barrier while preserving successful window IDs. It prevents replacement sessions from inheriting old windows. A malformed rebind leaves state unchanged. Barrier overflow clears accumulation and permanently exhausts this adapter instance; return EOVERFLOW and never wrap or publish more features. Create a new instance only as an explicit new lifecycle.

## Verification boundary

Test real adapter and signal/BCI SDK translation units, wrapping only open/close/ioctl for a deterministic transport. A real disposable descriptor backs the session. Cover golden 8-channel numerical output, source preservation, bad-tail atomicity, all configuration/record invariants, generation/gap/quality barriers, clean EOF, transient versus terminal read errors, session rebind, age rejection and barrier overflow under strict compiler warnings and ASan/UBSan. This host fixture is not real Cyton acquisition or a real kernel ioctl run. Existing P2 guest evidence remains historical. Build/package, real guest adapter, feature/model binding, actual CPU UDS inference and single-confirmation harmless feedback are separate following slices.
