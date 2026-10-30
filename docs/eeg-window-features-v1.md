# EEG streaming window and feature contract v1

Scope: P3 bounded CPU preprocessing baseline, beside the existing signal primitives. This document and implementation do not change the accepted P2 ABI. A later adapter must validate actual P2 records/configuration and explicitly populate the normalized sample described here. No classification accuracy, hardware timing or action authorization follows from a feature vector.

## Storage and numerical mapping

- Caller-owned C state, no dynamic allocation in initialization, push, reset or feature extraction.
- Channels 1..8; window 2..4096 frames; hop 1..window frames; declared sample rate 1..65535 Hz. Configuration includes an explicit positive maximum receive-age in nanoseconds.
- Each normalized sample declares v1, channel count/rate, integer nanovolts, CLOCK_MONOTONIC receive observation, source, quality, enabled mask, sequence, and nonzero session/stream/config identities. A caller-owned session identity separates device reopen/replacement from reused generation counters; it is not a physical device credential.
- Source is exactly SERDEV or SYNTHETIC. Metadata values correspond to P2 source and quality flags. The later adapter must not fabricate SERDEV or configuration verification for fixture input.
- `xtos.eeg.mean-rms.v1`: means for channels 0..channels-1, followed by RMS for those channels, in microvolts. Disabled-channel positions are zero and the explicit mask remains part of the vector's model contract. Frame count and source/config/stream/session identities accompany every vector. No filter is silently applied.
- Accumulate signed nanovolt sums in int64 and squared values in long double, divide by the actual complete window count and convert nV to microvolts. RMS includes DC. This version does not claim a classifier or spectral features.

## Continuity and emission

- Only a complete homogeneous window emits. First window needs exactly N accepted samples; after successful emission, at least hop new accepted samples are required. If the consumer does not extract immediately, the pending vector coalesces to the latest full window. Duplicate extraction without new eligibility returns `-EAGAIN` and preserves caller output.
- Session, stream, config generation, source or enabled-mask changes clear accumulated samples, then seed a new window with the valid current sample. A forward sequence gap also clears and seeds a new window. No output crosses those boundaries.
- Duplicate/backward sequence within the same identity clears pending accumulation and drops that sample while retaining the last accepted sequence fence. A later strictly newer sample may start again. Sequence arithmetic must not wrap.
- Quality must include CONFIG_VERIFIED with no unknown bits. COUNTER_GAP, RESYNC, ADC_RAIL, CONTINUITY_UNKNOWN or nonzero missing-before clears accumulation and drops the flagged sample. The next clean sample starts a new complete window.
- Receive timestamps may be equal: several samples can arrive in one callback. They must not decrease within the same identity. The receive clock is not a device clock and is not used to enforce an artificial 4 ms per-frame interval.
- Extract with an explicit current CLOCK_MONOTONIC `now_ns`. Future receive observation or age of the oldest frame greater than configured maximum returns `-ESTALE`, clears the pending accumulation and preserves caller output. The boundary age equal to maximum remains valid. Window IDs increase on successful emission and never reset during ordinary resets; overflow rejects without publishing output.
- Malformed arguments/metadata return `-EINVAL` and leave state and output unchanged. A production adapter must treat such a failure as a stream barrier before any later action candidate. Valid discontinuities return explicit reset/drop statuses.

## Integration and evidence boundary

P3a delivers the portable C window and deterministic golden/continuity/quality/freshness tests first. SDK/build/installed-header closure follows as a separate integration slice. P3b connects actual P2 frame sessions; P3c binds model input shape, feature version, channels, mask, rate, units and provenance to the existing signed CPU UDS runtime; P3d adds harmless simulated feedback through the existing one-time human approval gate. Fixture and synthetic input always remain `no_action`. Generation changes, faults and stale output must revoke pending feedback. Physical Cyton, trained-model accuracy, medical safety and hard-real-time latency remain separate evidence gates.
