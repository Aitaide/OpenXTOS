# BCI frame data plane v1

Status: P2 implementation contract. P1's accepted source/ISO remains frozen in its acceptance index; the current development tree is a new candidate. This contract reserves the new API below; implementation, module/SDK/guest and image acceptance are separate gates.

## Scope and compatibility

The legacy byte ring, read(), snapshot-v1 commands 13–15, and config-v1 command 16 retain their existing layout and behavior. The new frame ring is independent: consuming either view does not consume the other. It contains only frames published with provenance by an enabled producer; metadata is never reconstructed from old byte-ring contents. A device without a frame producer returns EOPNOTSUPP.

The first profile is Cyton, eight signed integer nanovolt channels at 250 Hz, verified config-v1 (384 bytes), and eight-bit wire counter. Thirty-two sample positions are reserved in the public record, but channels 8–31 are zero and no additional device profile is claimed. Config generations describe successful actual register readback, not a unique board identity or physical calibration.

## Fixed-width native-endian ABI

New headers: kernel `include/uapi/linux/xtos_bci_frame.h`; independent SDK `xtos/bci_frame_uapi.h`. All u64 fields are explicitly eight-byte aligned, including in the SDK on 32-bit targets. No native pointers, long, enum, bool or packed structures. Reserved fields and unused sample positions are zero. Version is 1, units NANOVOLT=1, host clock MONOTONIC=1 (Linux CLOCK_MONOTONIC), maximum payload 65536 bytes, maximum batch 292 records.

Record `xtos_bci_frame_v1`, 224 bytes, alignment 8:

| Offset | Field |
| --- | --- |
| 0,4,8,12 | u32 version, record_bytes, channels, sample_rate_hz |
| 16,20,24,28 | u32 unit, host_clock, quality_flags, source_flags |
| 32,40,48,56 | aligned u64 host_rx_ns, sequence, stream_generation, config_generation |
| 64,68,72,76 | u32 device_counter, counter_bits, missing_before, enabled_mask |
| 80 | aligned u64 reserved[2] |
| 96 | s32 samples_nv[32] |

Quality bits: CONFIG_VERIFIED=1, COUNTER_GAP=2, RESYNC=4, ADC_RAIL=8, CONTINUITY_UNKNOWN=16; unknown bits reject. CONFIG_VERIFIED is mandatory for this profile. COUNTER_GAP corresponds exactly to missing_before>0, a bounded modulo inference from an accepted counter delta2..127. Counter255→0 with delta1 is normal. Repeated/backward/ambiguous candidates are rejected; subsequent publication carries RESYNC and CONTINUITY_UNKNOWN until that indication is published. ADC_RAIL is an observed raw +/-24-bit endpoint, not a physical quality diagnosis. Parser noise/rejected-candidate history may set RESYNC; consumer overwrite is reported separately.

Source bits: SERDEV=1, SYNTHETIC=2, exactly one. SERDEV describes the kernel transport path and is not hardware acceptance. Synthetic fixture timestamps/counters remain labeled SYNTHETIC. No device timestamp is exposed by this version; accepted AUX/footer bytes are not treated as a proven device-time protocol.

`host_rx_ns` is sampled with ktime_get_ns() after the receive callback acquires its state mutex and before it processes that callback's bytes. Every frame completed by that callback uses the same sample. It is host callback processing time in CLOCK_MONOTONIC, not UART interrupt, board acquisition or end-to-end latency. Within a stream it is nondecreasing; equal values are valid. Sampling under the existing state lock avoids inventing order by clamping concurrent ingress timestamps.

Request `xtos_bci_frame_request`, 512 bytes, alignment 8:

| Offset | Field |
| --- | --- |
| 0,4,8,12 | u32 version, flags, capacity_frames, frame_count |
| 16,24,32,40,48,56 | aligned u64 frames_ptr, cursor, next_cursor, overwritten_frames, token, stream_generation |
| 64,68 | u32 record_bytes, header_bytes |
| 72,80 | aligned u64 mapping_bytes, payload_bytes |
| 88 | u32 reserved[10] |
| 128 | struct xtos_bci_config_v1 config |

`XTOS_BCI_FRAME_READ = _IOWR('X',17,request)`; SNAPSHOT_ACQUIRE=18 (_IOWR, same request); SNAPSHOT_CONSUME=19 (_IOW, consume); SNAPSHOT_RELEASE=20 (_IOW, release). Commands 1–16 are not renumbered or reused.

READ input permits only version1, flags0 or NONBLOCK=1, capacity_frames1..292, and nonzero numeric frames_ptr. All other bytes, including config, start zero. The numeric user address is validated before conversion; reject pointer+length overflow and nonzero upper32 bits for compat callers. A bad descriptor returns EINVAL before mutation. ACQUIRE input permits only version and capacity; flags and frames_ptr are zero. Neither API silently clamps an oversized capacity.

On success READ returns frame_count whole records, descriptor config and one homogeneous stream/config run; payload_bytes=frame_count*224, record_bytes224, header_bytes128, mapping_bytes0, token0. Empty stopped clean returns frame_count0 and otherwise zero output fields except version/capacity/frames_ptr/flags. Empty active nonblocking returns EAGAIN; blocking waits without op_lock. Buffered frames drain before saved acquisition error; empty faulted ring returns that error. ENODEV takes precedence after disconnect. Snapshot acquire is always nonblocking, returning EAGAIN when empty active/stopped clean and saved error when empty faulted.

Successful ACQUIRE returns version1, flags0, the unchanged requested capacity, frames_ptr0, frame_count1..capacity, record_bytes224, header_bytes128, and a nonzero FD-local token. For both READ and ACQUIRE, next_cursor=cursor+frame_count, payload_bytes=frame_count*224, and record[i].sequence=cursor+i+1. ACQUIRE mapping_bytes=PAGE_ALIGN(512+payload_bytes). Descriptor and mapped header agree on frame_count, record_bytes, header_bytes, cursor, next_cursor, token, stream_generation, overwritten_frames, payload_bytes and mapping_bytes. The inline descriptor config equals all384 mapped config bytes; config.generation equals the header and every record's config_generation, while stream_generation equals every record's stream_generation. Each record's unit and host_clock equal the mapped header. Core/SDK tests must mutate these equalities independently and reject a mismatch.

Each delivered record agrees with config generation/rate/channels/enabled mask, with the run's stream generation, and has a strictly increasing lifetime sequence. Config bytes are retained per frame slot and returned inline in every batch. A later successful readback or START cannot relabel an old record. Config references are scoped to the live descriptor incarnation plus stream/config generations, not a reusable numeric dev_id.

## Cursors, loss and publication

Allocate the CPU-owned bounded ring in sleepable initialization, before publication; no allocation in RX. Production Cyton capacity is 500 complete slots. Internal capacities1..4096 are valid, including non-powers-of-two; physical slot selection uses modulo. Each slot holds the complete record plus exact384-byte verified configuration. One ring spinlock publishes the slot before its write cursor advances, and captures records/config/cursors/overwrite count together. No usercopy, mutex, wait or driver callback under the ring spinlock. Never nest frame and legacy ring locks.

Logical u64 cursors count frames from0. Sequence assigned by the frame ring is cursor+1 for each successful lifetime publication; neither START nor reopen resets it. A new START admission reserves the next nonzero stream generation before enabling RX, even if synchronous TX later fails; an already-active idempotent START does not reserve another generation. Stream generation is never reused. Preserve existing config-v1 device-lifetime generation semantics: every successful verified configuration commit increments that existing counter, including unchanged readbacks committed by START. Failed readback invalidates the current configuration without changing the config bytes or generation already retained in frame slots. Do not introduce a separately reset frame configuration counter. Records queued across START/config changes remain queued and are split at exact config/stream boundaries. Reopen discards unread frame slots like legacy open, but does not reset identities or cumulative overwrite. A failed publish does not partially replace a slot. Refuse u64 cursor/sequence/stream-generation/config-generation exhaustion with terminal EOVERFLOW rather than wrapping identities.

Overwrite removes complete oldest slots only, advancing the read cursor and a cumulative u64 displaced-unread-frame count. This ledger is independent of wire counter loss and RESET_STATS; it does not count explicit consumer progress or reopen discard. Capture it consistently in each successful batch. If that counter cannot advance without wrap, fail publication with EOVERFLOW. Failed usercopy leaves the logical consumer unadvanced; overwrite can independently advance it. After both descriptor and payload copies, READ commits only if the saved cursor remains current, otherwise EOVERFLOW without rewinding or consuming replacement frames. EFAULT/EOVERFLOW can leave partial user memory changes; caller discards failed output, rather than relying on impossible memory rollback.

## Immutable snapshot family

Mapped header `xtos_bci_frame_snapshot_header`, 128 bytes:

| Offset | Field |
| --- | --- |
| 0,4,8,12 | u32 magic=0x58424631, version, header_bytes128, record_bytes224 |
| 16,20,24,28 | u32 frame_count, flags0, unit, host_clock |
| 32,40,48,56 | aligned u64 payload_bytes, cursor, next_cursor, token |
| 64,72,80,88 | aligned u64 stream_generation, config_generation, overwritten_frames, mapping_bytes |
| 96,100 | u32 config_bytes384, records_offset512 |
| 104 | u32 reserved[6] |

Mapping: immutable header at0, config at128, complete records at512; page-rounded, zero tail. Limit only record payload to64KiB. One snapshot family per open file: legacy-v1 and frame-v1 mutually exclusive, share the never-reused FD-local token sequence, and block ordinary read and FRAME_READ while held. mmap dispatches the active family; wrong-family ioctls cannot consume/release it. Existing v1 errors remain compatible. New family valid-shape wrong/replayed token returns ESTALE; malformed shape EINVAL; acquisition while held EBUSY. FD-local ownership is not a cryptographic cross-FD identity guarantee.

`xtos_bci_frame_consume`,48 bytes: u32 version/flags0; aligned u64 token,cursor,frame_count,stream_generation,reserved0. Consume once, exactly the acquired run; cursor changed by overwrite returns EOVERFLOW, no replacement consumption. After disconnect consume returns ENODEV. `xtos_bci_frame_release`,24 bytes: u32 version/flags0; aligned u64 token,reserved0. Release owns only CPU snapshot memory and is allowed after disconnect; no transport access. Acquired mappings remain immutable after consume/release/close/unbind and survive fork/split through owned VMA references. Offset0, exact mapping size, read-only MAP_SHARED only; reject write/PRIVATE and remove MAYWRITE before remap.

## SDK and lifecycle

SDK keeps old float/read/snapshot behavior. New integer frame APIs preallocate separate session-owned staging and published buffers, each with capacity for at most292 records (at most584 records in aggregate), at session creation or explicit pre-stream reserve. Neither buffer grows per read. An ioctl writes only staging storage; validate the complete descriptor/config/records there before copying to or swapping with published storage and exposing a const view. A failed ioctl or metadata validation leaves both the caller's view descriptor and all bytes reachable through its previous valid view unchanged. Tests must save that view and its bytes, force each failure, and compare both afterward. Externally serialize a session as before. Views expire at next successful frame read/release/destroy; callers retaining data copy it. Reject unsupported kernel families explicitly. Unmap-first snapshot release retries preserve token ownership on errors, and mmap(NULL) is not MAP_FAILED but is refused safely before dereference. Only installed SDK headers/libraries may be used by clients.

Lock order remains read_lock → op_lock → driver control_lock → driver state_lock; RX takes state_lock then a single ring lock, never op/control lock. Frame waiters release op_lock before sleeping and wake on publication, stop, fault, disconnect and snapshot state changes. Core final device put owns ring freeing; FD/device/module/DMA lifetime order remains unchanged. RX/control admission samples state under its writer lock and releases state before synchronous TX. Cancel wakes pending control work before unregister waits op_lock. No change makes current RX hard-IRQ-safe; it already uses a mutex.

## Closing gates

Genuine RED→GREEN behavior slices, independent SPEC then QUALITY, installed SDK and strict native32/native64 ABI compile checks, ordinary/ASan/UBSan and applicable TSan, unchanged legacy controls, module build, new synthetic Linux VFS/VM client, full maintenance and a new source-bound artifact. Compile-only32-bit evidence is distinct from compat syscall execution. Fixture, serdev-path, hardware timestamp, physical EEG, accuracy, sustained latency and medical acceptance remain separate.
