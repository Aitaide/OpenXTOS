# XTOS AI Runtime

XTOS is a real-time system first. AI/inference workloads run on it under
one rule: **the inference workload must never take anything away from the
real-time side** — no RT priority, no shared CPUs unless partitioned, no
claims the hardware cannot back.

This document describes what the image actually ships, what is proven, and
what is deliberately not done. (Chinese summary at the bottom.)

## What the live image already contains

The locked 530-package userspace includes the existing CPU inference stack and
GPU API runtimes:

- `python3-onnxruntime` 1.21 + `libonnx` — ONNX model execution
- `numpy`, `scipy`, `pandas`, `matplotlib` — data pipeline
- OpenBLAS / LAPACK / libgomp — the numeric substrate these need
- Mesa Vulkan ICDs + `vulkaninfo` (`mesa-vulkan-drivers`, `vulkan-tools`)
- Mesa Rusticl OpenCL + `clinfo` (`mesa-opencl-icd`, `clinfo`)
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

## GPU: in-tree drivers, userspace APIs, then optimize for RT

GPU support is part of the image, not a roadmap item. The kernel driver is
only one layer; the locked Live Developer userspace also contains Mesa
Vulkan ICDs (`mesa-vulkan-drivers`, `vulkan-tools`) and Mesa Rusticl OpenCL
(`mesa-opencl-icd`, `clinfo`).

- Intel i915 and virtio-gpu are built in; AMD amdgpu (including SI/CIK) and
  NVIDIA nouveau are autoloadable modules. Matching AMD, Intel and nouveau
  firmware comes from the locked image. Boot-time modalias loading is for
  hardware that is present.
- `/dev/dri/cardN` is assigned `root:video 0660`; `/dev/dri/renderDN` is
  `root:render 0660`. The ordinary `xtos` account has supplementary
  membership in those groups. BusyBox mdev applies these permissions during
  coldplug and hotplug. Unknown device nodes are not exposed to `xtos`.
- Start with `hw gpu`, `hw --json gpu`, `vulkaninfo --summary`, and
  `clinfo --list`. `hw gpu` reports node access and discovered driver/IRQ
  details; an API ICD listing is not proof of GPU acceleration. A software
  implementation such as lavapipe is not an accelerated GPU result.
- Mesa Rusticl OpenCL may require an explicit driver selection such as
  `RUSTICL_ENABLE=radeonsi clinfo --list`; this is device/provider-specific.
  Do not export the variable globally or report empty `clinfo` output as
  usable GPU compute.
- ONNX Runtime in this image remains CPU-only; no CUDA, ROCm or Vulkan
  execution provider has been bundled or verified. NVIDIA's proprietary
  out-of-tree/CUDA stack is not included.

For RT coexistence, inspect topology, `rt --json status`, `hw --json gpu`,
and cgroup partitions. Use `rt tune --housekeeping CPU-LIST` to choose
online housekeeping CPUs outside boot-isolated/RT CPUs. It verifies requested
and effective IRQ affinity and reports non-movable/managed IRQs as skipped or
failed; it does not guarantee all interrupts moved. IRQ threads inherit IRQ
affinity on PREEMPT_RT, but ordinary GPU workqueues are not automatically
placed in an AI cpuset or guaranteed to run as `SCHED_OTHER`. Inspect the
actual workers on the target system before asserting placement.

In-guest evidence so far: virtio-gpu really probed in QEMU (DRM node +
driver binding), amdgpu/nouveau modules load, firmware trees exist, and the
boot-time driver autoload path runs. Physical GPU API acceleration, hotplug,
IRQ migration behavior and idle-vs-saturated RT latency remain hardware
acceptance gates; QEMU cannot prove them.


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
GPU 路线：镜像含 i915、AMDGPU、nouveau 和 virtio-gpu 驱动、固件，
以及 Mesa Vulkan/OpenCL 与诊断工具；普通 xtos 用户按 video/render
组权限打开设备。`rt tune --housekeeping CPU-LIST` 可选中断处理核，
读回 requested/effective affinity；不能把 cgroup 挂载当成 GPU 内核
线程隔离证明。实卡 API 加速、GPU 推理后端和负载下的 RT 延迟仍需
分别验收，不能以模块加载或软件 Vulkan 代替。NVIDIA 闭源/CUDA 栈
不在当前镜像内。
