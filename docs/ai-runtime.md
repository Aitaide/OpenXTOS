# XTOS AI Runtime

XTOS is a real-time system first. AI/inference workloads run on it under
one rule: **the inference workload must never take anything away from the
real-time side** — no RT priority, no shared CPUs unless partitioned, no
claims the hardware cannot back.

This document describes what the image actually ships, what is proven, and
what is deliberately not done. (Chinese summary at the bottom.)

## What the live image already contains

The locked 362-package userspace includes a complete CPU inference stack
with no additions:

- `python3-onnxruntime` 1.21 + `libonnx` — ONNX model execution
- `numpy`, `scipy`, `pandas`, `matplotlib` — data pipeline
- OpenBLAS / LAPACK / libgomp — the numeric substrate these need
- `cyclictest` (rt-tests) — the RT latency probe
- Python 3.13 with venv support

Vision-model inference (detection/classification/segmentation style
networks) runs on this stack as-is: the guest-side proof loaded a
convolutional ONNX graph (conv-relu-pool-gemm, opset 17) and matched
host-computed golden outputs to within 2e-06.

Vision INPUT is part of the locked image since lock v5 (517 packages):
`ffmpeg` (7.1.x) + `v4l-utils` for decode/capture tooling, and the live
kernel ships the V4L2 camera stack as MODULES (`MEDIA_SUPPORT=m`,
`VIDEO_DEV=m`, `USB_VIDEO_CLASS=m` behind `MEDIA_CAMERA_SUPPORT=y`) so no
media code touches the boot path until a camera appears. In-guest chain
evidence: H.264 decode to pixels, ONNX inference on the decoded frame,
cross-verified against the control host (maxdiff 4.8e-07); `uvcvideo` and
friends load on demand. Physical camera capture is a hardware step — QEMU
has no UVC device model, so the absence of /dev/video* under emulation is
recorded rather than hidden.

## LLM inference: llama.cpp as a third-party xpkg

LLM inference is not baked into the image. It installs through the normal
third-party application path:

1. `llama.cpp` is built statically on the control host and packaged with
   `make_xpkg.py --sign <vendor ed25519 key>`.
2. `pkg app install engine.xpkg --pubkey <vendor public key>` verifies the
   signature (key -> manifest -> payload) and installs under
   `/opt/xtos-apps/<name>/<version>/payload/`.
3. Model weights live on an `XTOS-PERSIST` volume or any mounted medium —
   never in the image (the live overlay is tmpfs; a multi-GB weight file
   would consume RAM). Verify the file's sha256 before loading.
4. The engine runs as a plain `SCHED_OTHER` process. It never gets RT
   priority; it is the thing the RT side must be protected *from*.

Proven in-guest: a signed llamacpp xpkg installed with signature
verification, loaded Qwen2.5-0.5B-Instruct Q4_K_M (491 MB), and generated
coherent text on the CPU. Throughput figures are hardware-bound; QEMU TCG
proof-of-pipeline numbers (seconds per token) are not product numbers.
On the control host's native CPU the same binary+model sustains ~18.5
tok/s with 8 threads; a server-class machine scales accordingly.

Model size ladder (Q4_K_M, CPU): 0.5B ≈ 0.5 GB, 7-8B ≈ 4.5-5.5 GB,
14B ≈ 9 GB. Plan RAM for weights + activations + page cache, and keep the
model on persistent storage, mmap-loaded.

## CPU isolation: the `cgroup` tool

`cgroup` is a native XTOS tool (no shell, no external binaries) that
manages the cgroup2 unified hierarchy:

```
cgroup init                          mount cgroup2, enable controllers
cgroup status [--json]               hierarchy, controllers, partitions
cgroup partition ai --cpus 2-7      create a cpuset partition
cgroup partition rt --cpus 0-1
cgroup pin ai <pid>                  move a process into a partition
```

The boot sequence mounts the hierarchy automatically. The intended layout on
an N-core machine: RT cores own the control loops (and nothing else runs
there); AI/inference cores run the models; the kernel has `CPUSETS` and
`CGROUP_SCHED` enabled for exactly this.

Scope honesty: the shipped kernel does **not** enable `MEMCG` or
`CFS_BANDWIDTH`, so memory.max bounds and CPU bandwidth quotas do not exist
in this build; `cgroup status` reports the controllers that are actually
present. If inference memory containment becomes a requirement, enabling
MEMCG is a kernel-config change (with its RT cost to evaluate) — not a
userspace fix.

The in-guest isolation check proves the mechanism end to end: partitions
created, hog processes actually moved (verified via `/proc/<pid>/status`
Cpus_allowed_list), latency probe running while load is partitioned away,
and the same load unpinned for comparison. Numbers under QEMU TCG are
emulation-bound; the meaningful comparison is on physical hardware.

## GPU: in-tree drivers yes, then optimize for RT

GPU acceleration IS part of the roadmap. The boundary is the driver's
SOURCE, not GPU-vs-RT:

- In-tree open drivers (i915, AMDGPU, Nouveau) are kernel components:
  image-based upgrade integrity is preserved, and their interrupt
  handlers and kernel threads are MANAGEABLE for RT. The optimization
  playbook: pin GPU IRQs to non-RT cores (irq affinity), keep driver
  threads SCHED_OTHER inside the AI cgroup partition, place the inference
  process in the same partition, then A/B cyclictest with the GPU idle vs
  saturated. That is the "GPU + RT coexistence" work.
- NVIDIA's CLOSED out-of-tree stack (nvidia.ko + CUDA userspace) is the
  hard constraint: hundreds of MB outside the kernel tree break
  image-based upgrades, closed-source interrupt behavior cannot be
  audited or pinned. If target hardware demands NVIDIA, that decision
  gets revisited explicitly with the costs stated — it does not slide in.

Which path the hardware forces depends on the GPU present on the target
machines (Intel iGPU → i915; AMD → AMGPU; NVIDIA dGPU → the hard row).
Physical-hardware measurement (IRQ pinning, cyclictest A/B under GPU load)
is the acceptance gate; QEMU cannot prove GPU latency behavior.

## Remote inference: the production gateway

Heavy-generation work goes to a remote OpenAI-compatible gateway (New API)
on the project's own server. Proven in-guest on a really-booted live
system: TLS egress with real certificate validation (Let's Encrypt,
default trust store), authenticated `GET /v1/models` against the production
gateway (Bearer key, catalog parsed), and the gateway rejecting a bad key
with 401. The same HTTP+Bearer+JSON path carries chat/completions once a
text model is reachable.

Two deployment gaps live on the GATEWAY side, not in XTOS (recorded so
nobody mistakes them for system limitations):

1. The gateway currently has no TLS front (plain HTTP on its IP). Put the
   existing HTTPS nginx in front of it before production use.
2. Its channel catalog is video-generation only (seedance family); a
   chat-capable LLM channel is a one-action gateway configuration change.
   XTOS needs zero changes for either.

Inference-cost honesty: the proof stops at the catalog call. Spending
quota on an actual generation is a business decision, not an engineering
milestone — the transport, auth and parsing are identical.

## Rules for AI workloads on XTOS

1. Inference processes run `SCHED_OTHER`, pinned to their partition.
2. Weights never live in the image or the tmpfs overlay; persist volumes
   or mounted media only, sha256-verified before load.
3. RT tasks keep their cores: verify with `cgroup status` +
   `Cpus_allowed_list` before trusting any number.
4. Every capability claim comes from an in-guest proof, not a datasheet.
5. Engine binaries arrive as signed xpkgs; the trust chain is the same
   one the package system already enforces.

---

## 中文摘要

XTOS 以实时为本，AI 推理是"受管租户"：镜像自带 ONNX 运行时与
Python 科学栈（视觉模型即装即用）；LLM 推理引擎走第三方签名 xpkg
安装，模型权重放持久卷、加载前校验哈希；`cgroup` 原生工具负责
CPU 分区——实时核与推理核物理分开，推理进程永远普通调度优先级。
GPU 路线：内核内开源驱动（i915/AMDGPU）做，然后做 RT 共存优化
（GPU 中断钉非实时核、驱动线程入 AI 分区、实测 A/B）；NVIDIA 闭源
out-of-tree 栈是唯一硬约束，硬件若强制 NVIDIA 需显式决策。所有
能力声明以真机实测为准。
