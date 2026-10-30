# Independent P2 frame ring

The host test includes the production `xtos_bci_frame.c` unchanged. Only
`XTOS_BCI_FRAME_HOST_TEST` selects pthread locks/condition wakeups and counted
calloc/free services. There is no copied ring implementation or no-op lock.

Run from the repository root, selecting a new no-overwrite evidence label:

```
python3 tests/test_bci_frame_ring.py --label local-normal
python3 tests/test_bci_frame_ring.py --label local-asan --mode asan
python3 tests/test_bci_frame_ring.py --label local-tsan --mode tsan
```

The twelve executable groups cover allocation failures and capacity bounds;
capacity-three modulo/overwrite; 39 invalid binary record/config mutations;
immutable generation identity and equal callback times; u64 cursor/overwrite
exhaustion; homogeneous peek, bounded output and unchanged failure outputs;
commit races; discard and bounded explicit cancellation wake; 20,000 concurrent
publications with exact consumed+overwritten accounting; maximum batches and
source/stream splits; protected read-only producer inputs; all seven gain
profiles, disabled channels, persistent timestamp and maximum identities.
Concurrency and later boundary cases verify existing behavior; they are not
relabeled as newly observed RED. The preserved entry missing-file compilation
failure is distinguished from subsequent executable assertion RED evidence.

## Parent integration API

Include `xtos_bci_frame.h`. Allocate in sleepable initialization with capacity
1..4096 (the parent selects Cyton 500). Free only after all producers, consumers,
pollers and waiters are excluded; ownership is external. Allocation returns an
ERR_PTR on EINVAL/ENOMEM. No ring operation allocates after initialization.

`publish(ring, const record, const config)` validates binary Cyton metadata,
assigns sequence under one spinlock, copies the complete record and all 384
config bytes, and advances lifetime cursors atomically. The caller keeps input
bytes stable for the duration of the call. Input sequence must be zero.
Zero/invalid metadata, backward stream/config generations, backward host time
within the same stream, or ANY changed config bytes at the same generation
return EINVAL without mutation or wake. Generations can increase with queued
older runs retained. Current identity survives consumption and discard. The
ring cannot know an externally reserved generation before its first published
frame; producer admission must enforce the currently authorized reservation.
The final nonzero u64 identity is representable; subsequent increment must be
refused by its owner rather than wrapped. Cursor/sequence and overwrite ledger
exhaustion return EOVERFLOW before touching slots.

`peek(ring, capacity, records, out)` uses caller-owned kernel storage for
1..292 records; no user pointer or allocation enters the ring lock. It captures
one run with exact config bytes, stream, source, clock/rate/channels/units,
cursors and cumulative overwrite under one lock. It zero-initializes unused
output descriptor fields; core subsequently supplies flags, frames_ptr, token
or mapping fields. Empty returns EAGAIN; invalid/overflow leaves both outputs
unchanged. Buffers must be valid, writable, nonoverlapping, and external to the
ring. This is a kernel helper, not a user-address validator.

`commit(ring, saved_cursor, saved_count)` requires exact current cursor and a
positive count <=292 within the available homogeneous run. Cursor mismatch or
addition overflow returns EOVERFLOW without consuming replacement frames;
invalid count or a mixed run returns EINVAL. The parent serializes consumers
and retains the exact successful peek descriptor across usercopy. This helper
does not authorize tokens or prove that a caller previously peeked.

`discard_unread` advances read to write without resetting sequence, current
identity, timestamp or overwrite counts. `available` takes the ring lock and
returns a boolean. `ring->wait` is the wait/poll queue. Publish wakes it after
unlock; `wake` lets core signal stop, fault, cancellation or disconnect. The
ring owns no lifecycle status: wait conditions include parent-owned terminal
state. Never wait, usercopy, take a mutex or invoke transport callbacks under
the frame lock; never nest it with the legacy ring lock.

## Evidence limits

The frozen contract remains `docs/bci-frame-uapi-v1.md`. Exact files, dependency
hashes, commands, raw exits and log hashes are in
`.build/xtos-roadmap-v1/p2-ring/implementation.json`. This is isolated source
and host-service evidence only. Driver/core integration, Kbuild/module build,
real kernel spinlock/waitqueue execution, Linux VFS/VM and compat syscalls,
SDK, new independent SPEC/QUALITY review, full maintenance, new artifact,
physical device and sustained RT acceptance remain separate parent gates.
No P1 historical acceptance or P2 contract-only review is implementation approval.
