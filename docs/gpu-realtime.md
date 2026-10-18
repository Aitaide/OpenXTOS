# GPU workloads and real-time housekeeping

## Quick-start checks

The live profile includes Mesa Vulkan ICDs, `vulkaninfo`, Mesa Rusticl OpenCL,
and `clinfo`, in addition to the kernel drivers and locked GPU firmware.
Run as the ordinary `xtos` account:

```sh
hw gpu
hw --json gpu
vulkaninfo --summary
clinfo --list
```

`hw gpu` reports real DRM driver names from the device ioctl rather than
confusing a PCI transport such as `virtio-pci` with `virtio_gpu`. It reports
which nodes this user can open and the GPU IRQs exposed by sysfs. The command
is read-only; it does not assert inference readiness or optimal RT settings.

Primary DRM nodes use `root:video 0660`; render-only nodes use
`root:render 0660` (XTOS allocates render GID 1001). The developer account has
both supplementary groups, including on the local console. BusyBox mdev
applies the same access policy during coldplug and later netlink events.
Unknown devices, raw disks and physical memory remain root-only.

Rusticl may require an explicit driver opt-in, for example
`RUSTICL_ENABLE=radeonsi clinfo --list`. Do not export this globally or treat
an empty platform listing as usable OpenCL compute. Software Vulkan
(lavapipe/llvmpipe) is a loader/fallback test, not GPU acceleration. The
image's ONNX Runtime remains CPU-only; GPU execution providers, NVIDIA's
proprietary driver and CUDA are not part of this image.

Reproduce the QEMU check with a fresh evidence directory:

```sh
python3 scripts/verify_live_gpu.py --candidate .build/live/candidate-NEW \
  --key .build/live/keys/verification --evidence .build/live/evidence-NEW-gpu
```

It verifies virtual DRM nodes, SSH and local-console supplementary groups,
node permission restoration after an explicit virtual add uevent, API
loader/software fallback, module loading, and IRQ configuration readbacks.
At least one GPU IRQ must have matching requested/effective readbacks. The
report separately retains unmoved and unavailable IRQs and the native tuning
exit status; a partial result is not a claim that all GPU interrupts moved.
This is not a physical GPU unplug/replug, acceleration or latency test.

## IRQ housekeeping

Run tuning from a root shell, for example key-authenticated root SSH. The
ordinary `xtos` account is not granted passwordless sudo.

XTOS can request IRQ routing to a selected housekeeping CPU set with:

```sh
rt tune --housekeeping 0,2-3
```

The CPU list must be strictly formatted, online, and disjoint from CPUs isolated at boot. With no option, `rt tune` keeps the historical CPU 0 default. The command validates this before writing either the RT budget or any IRQ affinity.

`rt tune` writes each IRQ's requested affinity and then reads back both `smp_affinity_list` (requested) and `effective_affinity_list` (kernel-applied). A nonempty effective subset of the requested housekeeping CPUs is valid: an IRQ controller may select just one CPU. The output reports the actual readbacks, not a claim that every requested CPU is used. Managed or otherwise unavailable IRQs are reported as skipped or failed; success does not mean every IRQ moved. A failed RT budget write or short write is an error and returns nonzero.

On PREEMPT_RT, IRQ threads inherit the IRQ affinity constraints. This does not automatically classify ordinary GPU workqueues or other kernel workers as `SCHED_OTHER`, nor does it put every GPU worker into a selected cpuset. Workqueue placement and GPU driver behavior require a separate runtime measurement on the target kernel and hardware.

For boot-time CPU isolation, keep the parameters distinct and use the kernel's `domain` spelling, for example:

```text
isolcpus=domain,managed_irq,N-M nohz_full=N-M rcu_nocbs=N-M
```

The `nohz_full` setting is a separate parameter. QEMU measurements remain virtualization evidence; they do not establish a worst-case GPU or IRQ latency bound on physical hardware.
