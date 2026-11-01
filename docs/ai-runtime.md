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

- AI control-host tests live in an isolated Python 3.11 CPU-only environment. The lock file `tools/xtos-onnx-baseline/requirements-ai.lock` pins NumPy, ONNX, ONNX Runtime, PyTorch CPU, TensorFlow CPU, Keras and protobuf with hashes. Provision with `uv venv --python 3.11 .build/ai-acceptance/venv` then `uv pip sync --python .build/ai-acceptance/venv/bin/python --require-hashes tools/xtos-onnx-baseline/requirements-ai.lock`. Use an AVX-capable host for native framework execution, or run `XTOS_AI_PYTHON=scripts/ai-python-qemu.sh bash scripts/test.sh` when the host CPU lacks AVX/AVX2. QEMU functional parity is not a performance benchmark. The strict AI entry rejects missing suites, failures and any skipped tests, and writes `.build/ai-acceptance/results.json`; without `XTOS_AI_PYTHON`, the default full suite retains its optional-dependency degradation path.
- `tests/test_ai_acceptance.py` verifies that the strict AI gate rejects missing suites, failures and unittest skips. `scripts/test_ai.py` records test results, runtime/package versions and source provenance. `tf_export.py` keeps TensorFlow/Keras converter chatter on stderr so stdout remains a single machine-readable JSON document.

## Strict acceptance and source identity

Development tests may run from a changed checkout. Release acceptance is stricter:
commit reviewed source, use a detached clean worktree, and run both commands with
that worktree's complete `HEAD` SHA:

```sh
SHA=$(git rev-parse HEAD)
.build/ai-acceptance/venv/bin/python scripts/test_ai.py \
  --require-clean --expected-source "$SHA" \
  --report .build/ai-acceptance/results.json
python3 scripts/verify_ai_acceptance.py \
  --report .build/ai-acceptance/results.json --expected-source "$SHA"
```

On a control host without the required CPU instructions, replace the first
Python command with `scripts/ai-python-qemu.sh`; keep the verifier on the host.
This tests CPU functionality under emulation, not physical latency.

The report records start/end source snapshots (including nonignored untracked
source), model and dependency-lock hashes, selected suite and runner-source
hashes, actual executed test IDs, per-suite counts and the seven direct package
versions. The verifier derives the expected test inventory from the current
suite source without importing frameworks. It rejects missing, duplicate or
skipped tests, changed inputs, stale/dirty source, wrong test roots and versions
that differ from `requirements-ai.lock`. It also compares input bytes with the
committed Git objects, not only `git status`.

The `ai-acceptance` CI job installs the hash lock into its own Python 3.11 venv,
runs the strict runner and verifier for the exact checkout SHA, and retains
actual report/log files even on failure. This is consistency verification of
trusted runner output, not a cryptographic signature of reports or evidence of
task accuracy, GPU acceleration or hardware safety.

For a Live claim, AI source acceptance alone is insufficient. Build with
`build_live.py --require-clean --expected-source "$SHA"` and all documented
input arguments, boot the exact candidate using `verify_live.py`, then run:

```sh
python3 scripts/verify_live_acceptance.py \
  --candidate .build/live/<new-candidate> \
  --evidence .build/live/<four-mode-evidence> \
  --expected-source "$SHA" --report .build/live/<acceptance-report>.json
```

The complete Live acceptance requires BIOS/UEFI × CD/USB evidence. It checks
both staged files and the kernel/initramfs/rootfs actually embedded in the ISO.
A previous candidate's report cannot validate a new ISO or a newer source SHA.
Physical GPU/BCI/camera, sustained load and Secure Boot remain separate gates.

## Local unified inference service

The service is a local-only, fail-closed control plane. The original P1 contract
below remains the v1 CPU baseline. The provider-aware v2 extension is defined
in [ai-provider-runtime-v2.md](ai-provider-runtime-v2.md); its source, runtime,
installed-image and physical GPU acceptance remain separate gates.

- `tools/xtos-ai-runtime/xtos_ai_runtime/protocol.py` accepts an exact versioned
  manifest schema, a root-contained regular ONNX file, a matching SHA-256,
  bounded float32 rank-2 tensors and explicit thread counts. The original
  eight-field v1 manifest remains CPU-only; v2 adds a provider execution
  profile. Structural validation and a digest alone do not authorize execution:
  production still requires the versioned publisher signature and trust policy.
- `worker.py` starts ONNX Runtime only in a separate process group. It validates
  provider and tensor metadata, performs a smoke inference, rejects any runtime
  diagnostics, and terminates/reaps the child on deadline or cancellation.
- `server.py` exposes JSON-lines over an exclusive `AF_UNIX` socket only. It
  supports `list`, `load`, `reload`, `unload`, `health`, `infer` and `cancel`.
  A staged worker is health-checked before the active pointer changes; failed
  staging leaves the old worker active. The registry is bounded to four models,
  and inference is bounded to one active plus one queued request; excess work
  returns `busy` and `safe_action=no_action`.
- `client.py` checks request size, finite JSON, response schema, request ID,
  and the success/no-action contract. `python -m xtos_ai_runtime` runs the
  service when the package directory is on `PYTHONPATH`.

Example control-host smoke test (the locked AI Python must be selected
explicitly):

```sh
export PYTHONPATH="$PWD/tools/xtos-ai-runtime"
export XTOS_TEST_AI_PYTHON="$PWD/scripts/ai-python-qemu.sh"
for suite in test_ai_runtime.py test_ai_service.py test_ai_client.py; do
  python3 -B -m unittest discover -s tests -p "$suite" -v || exit 1
done
for suite in test_ai_service_real.py test_ai_worker.py; do
  python3 -B -m unittest discover -s tests -p "$suite" -v || exit 1
done
```

The first loop is fixture/control-plane evidence; the second runs the fixed
ONNX model through the locked runtime and is CPU functional evidence, not GPU,
real-time worst-case, domain-accuracy, medical-safety or physical-device
acceptance. The service is not yet an agent/action executor: every rejected,
invalid, unavailable, timeout or cancelled path returns `no_action`.

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
