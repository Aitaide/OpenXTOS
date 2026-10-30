# Frame snapshot host tests

The harness includes the production frame ring and frame snapshot C exactly once.
The existing frame shim supplies ring services; the owned snapshot shim adds atomic
reference counts, zeroed allocation, modeled VM flags and remap failure injection.
No snapshot algorithm is copied into the harness.

Eight C test groups cover allocation failures and cleanup; exact descriptor,
header, configuration and record bytes; allocation-time producer displacement,
empty and corrupt-ledger rechecks; consume shape/token/replay/overwrite errors;
owner references; VMA validation/remap/clone ownership; capacity and homogeneous
run boundaries; and inputs protected with mprotect(PROT_READ). Eight cloned VMA
close threads rendezvous after owner/initial-VMA release before final teardown.
The Python test also compiles an independent source export and checks compiler
-MMD project dependencies remain within that export.

The helper requires caller-owned ring lifetime and op_lock serialization of
snapshot capture and consumption. Acquisition must not wait for read_lock when
a blocked reader owns it. RX can publish concurrently. The caller owns family,
disconnect and FD-local token-sequence policy; the helper retains no device,
transport or ring reference. Only consume changes logical ring progress. Shape
errors are EINVAL before token/replay ESTALE; a matching token with different
cursor/count/stream is EINVAL; ring displacement is EOVERFLOW without consume.
The mapping owns only copied CPU memory and persists after owner release.

Run `python3 tests/test_bci_frame_snapshot_kernel.py` from the repository root.
Set `XTOS_SNAPSHOT_SANITIZER=asan` for ASan/UBSan or `tsan` for ThreadSanitizer.
Every build uses a fresh temporary directory. Host tests establish C behavior and
modeled reference/VM policy only. Linux VFS/VM execution, module integration,
transport and physical timing remain separate acceptance gates.
