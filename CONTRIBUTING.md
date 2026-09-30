# Contributing to XTOS

## Source tree rules

- Keep the complete bundled `kernel/linux` and `userspace/busybox` source trees, root filesystem overlay, and XTOS kernel integration in the development repository.
- Preserve development tasks, plans, architecture documents, and upstream licenses; mark historical reports rather than deleting them.
- Do not commit `.build`, `kernel/preempt-rt-build`, kernel configurations, kernel images, object files, shared libraries, test binaries, logs, or generated reports.
- Update `kernel/patches/xtos-linux-6.6.patch` when the XTOS integration changes, and keep the bundled tree buildable from a clean output directory.
- Do not claim hardware, real-time, medical, or model-accuracy results without the exact reproducible evidence.

## Before committing

```sh
./scripts/test.sh
git diff --check
./scripts/verify_source_tree.sh
git status --short
```

## Commit policy

Use short imperative commit subjects with one scope, for example:

```text
Add deterministic EEG signal baseline
Fix OpenBCI serdev teardown
Update PREEMPT_RT validation notes
```

Generated outputs belong in local evidence directories, not in Git. Keep public documentation concise and distinguish source compilation, kernel boot, VM validation, and real hardware validation.
