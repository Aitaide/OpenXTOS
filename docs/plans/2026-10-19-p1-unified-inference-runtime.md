# P1.1 Unified Inference Runtime Implementation Plan

> For Hermes: implement this plan task-by-task with test-driven development and independent review before integration.

Goal: provide a local Unix-domain-socket inference service that validates model manifests and inputs, stages models before atomic activation, bounds work, and returns safe no_action responses for every failure or deadline path.

Architecture: a small Python service in `tools/xtos-ai-runtime/xtos_ai_runtime/` owns a registry of hash-bound ONNX manifests and a serializable active worker plus a staged candidate worker during reload. Workers load ONNX Runtime in a killable child process so an inference deadline or cancellation terminates the worker instead of waiting for a synchronous call to return. The control plane is JSON-lines over AF_UNIX only; it is not an HTTP or default-network service. Bound the first implementation to batch size 1, one active request and at most one queued request; return busy/no_action beyond that. The first provider is CPUExecutionProvider with explicit thread counts.

Scope: model hash and path validation, schema/type/finite-value validation, list/load/unload/reload/health, atomic active-model switching, bounded in-flight requests, deadline/cancel restart, runtime-diagnostic capture, and a real fixed ONNX model integration test. GPU, camera, BCI acquisition, persistent model storage and production key revocation remain separate gates.

---

### Task 1: Define the protocol and validation tests

Files:
- Create: `tests/test_ai_runtime.py`
- Create: `tools/xtos-ai-runtime/xtos_ai_runtime/__init__.py`
- Create: `tools/xtos-ai-runtime/xtos_ai_runtime/protocol.py`

Write tests first for manifest fields, root-contained model paths, SHA-256, input shape/type/finite checks, response schema, and safe failure mapping. Run `python3 -m unittest tests.test_ai_runtime -v`; expected RED because the runtime modules do not exist.

### Task 2: Implement model registry and worker boundary

Files:
- Create: `tools/xtos-ai-runtime/xtos_ai_runtime/worker.py`
- Create: `tools/xtos-ai-runtime/xtos_ai_runtime/registry.py`

Implement only the behavior covered by Task 1. Use `multiprocessing.get_context("spawn")`, a pipe, explicit worker startup handshake, and captured stderr. A nonempty runtime diagnostic is a failed load when strict diagnostics are enabled. A worker timeout or cancellation terminates and replaces that worker. Do not allow a failed staged model to replace the active model.

Run the focused validation tests and confirm GREEN.

### Task 3: Implement AF_UNIX JSON-lines server/client

Files:
- Create: `tools/xtos-ai-runtime/xtos_ai_runtime/server.py`
- Create: `tools/xtos-ai-runtime/xtos_ai_runtime/client.py`
- Modify: `tests/test_ai_runtime.py`

Implement `list`, `load`, `unload`, `reload`, `health`, `infer`, and `cancel`. Use exclusive-safe socket setup, a bounded semaphore for in-flight work, and explicit response fields: `schema`, `id`, `status`, `safe_action`, `reason`, `model`, `provider`, `output`, `latency_ms`, and `diagnostics` as applicable. Every rejected, busy, invalid, unavailable, cancelled or timed-out inference returns `safe_action=no_action`; only validated finite output may return `action_allowed`.

Run the complete focused runtime suite and verify no socket is exposed on TCP.

### Task 4: Exercise the real ONNX fixture and strict diagnostics

Files:
- Modify: `tests/test_ai_runtime.py`
- Create: `tools/xtos-ai-runtime/xtos_ai_runtime/model-manifest.example.json`
- Modify: `docs/ai-runtime.md`

Use `tools/xtos-onnx-baseline/fixtures/fixed_classifier.onnx`, its exact SHA-256, and the locked isolated AI Python for the integration test. Cover successful inference, atomic reload, invalid input, deadline safe result, cancellation/restart, and backpressure. Record the Debian image's existing duplicate-schema diagnostic as an explicit rejected-runtime condition if reproduced; do not suppress stderr or convert it into a pass.

Run:
- `python3 -m unittest tests.test_ai_runtime -v`
- the locked-environment integration command documented by the test
- `git diff --check`

### Task 5: Independent review and integration gates

Freeze the owned paths, run a spec reviewer then a quality reviewer, fix only blocking findings, and rerun the focused suite. The parent integration owner then runs the complete maintained userspace suite, source/SBOM gates, and a clean-source P1 candidate test before committing. Do not claim P1 completion from host unit tests alone; keep image, physical hardware, GPU and real BCI evidence separate.
