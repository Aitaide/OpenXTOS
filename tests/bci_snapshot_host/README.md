# Snapshot helper host execution

Run `python3 tests/test_bci_snapshot_kernel.py -v` from the repository root.
The C harness includes the actual `xtos_bci_snapshot.c` with
`XTOS_BCI_SNAPSHOT_HOST_TEST`. Only kernel services (allocation, refcounts,
locks and VMA remapping) are substituted. UAPI declarations are the real header.

ASan/UBSan: `SNAPSHOT_BINARY=snapshot-asan SNAPSHOT_CFLAGS="-fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer -no-pie" python3 tests/test_bci_snapshot_kernel.py -v`.

TSan: `SNAPSHOT_BINARY=snapshot-tsan SNAPSHOT_CFLAGS="-fsanitize=thread -no-pie" python3 tests/test_bci_snapshot_kernel.py -v`.

The shim uses pthread mutexes and C11 atomic reference accounting. It validates
wrapped source copies, payload/header/padding, request bounds, allocation failure,
exact cursor consumption and replay, readonly flag policy, VMA reference ownership,
and failed remap cleanup. Concurrent cloned-VMA releases are barrier-controlled
until session and initial VMA references have gone. It does not exercise Linux
spinlocks, real vmalloc mappings, mprotect, fd teardown, unbind, or DMA/hardware.
Those require the independent integrated kernel/guest gate.

Caller API: hold the device operation mutex and validate connection/session
state around create/consume. Create returns one session-owned reference or
ERR_PTR. `get` requires an existing live reference. `put` releases a reference.
Successful mmap owns one initial VMA reference, clones add references through
vm_ops.open, and close releases each VMA reference. No helper object retains the
ring or transport. Request bytes must be nonzero, <=65536 and 4-byte aligned;
channels are 1..32, rate and token are nonzero. Whole channel frames are copied;
insufficient data returns EAGAIN. Bad consume fields return EINVAL, replay ESTALE,
and a changed ring cursor EOVERFLOW, with no mutation on failure.
