"""Killable single-request ONNX worker. No inference runs in the supervisor."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import importlib
import stat
import json
import math
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xtos_ai_runtime.protocol import (
    ExecutionProfile, ModelManifest, RequestError, safe_result, success_result,
    validate_execution_profile, validate_input, validate_output,
)

from xtos_ai_runtime.trust import AuthorizedModel

MAX_LINE = 65536
MAX_DIAGNOSTICS = 8192


class WorkerError(RuntimeError):
    """The process could not load a validated, diagnostic-clean runtime."""

    def __init__(self, message, reason="worker_failed"):
        super().__init__(message)
        self.reason = reason


def _profile_raw(profile):
    """Rebuild JSON shape from the immutable, locally held profile."""
    if type(profile) is not ExecutionProfile:
        raise ValueError("execution must be ExecutionProfile")
    if (type(profile.runtime_files) is not tuple or
            any(type(pair) is not tuple or len(pair) != 2 for pair in profile.runtime_files) or
            type(profile.cpu_affinity) is not tuple):
        raise ValueError("invalid immutable execution shape")
    raw = asdict(profile)
    raw["runtime_files"] = dict(profile.runtime_files)
    if len(raw["runtime_files"]) != len(profile.runtime_files):
        raise ValueError("duplicate runtime file")
    raw["cpu_affinity"] = list(profile.cpu_affinity)
    return raw


def _wire_profile(provider, raw):
    """Child accepts only asdict's pair-list transport, then revalidates."""
    if type(raw) is not dict:
        raise ValueError("invalid execution transport")
    raw = dict(raw)
    pairs = raw.get("runtime_files")
    if (type(pairs) is not list or any(type(p) is not list or len(p) != 2 or
                                     type(p[0]) is not str for p in pairs)):
        raise ValueError("invalid runtime file transport")
    raw["runtime_files"] = dict(pairs)
    if len(raw["runtime_files"]) != len(pairs):
        raise ValueError("duplicate runtime file transport")
    # JSON affinity must already be a list; never coerce arbitrary input.
    return validate_execution_profile(provider, raw)


def _provider_options(provider, profile):
    if provider == "CPUExecutionProvider":
        return {}
    if provider == "CUDAExecutionProvider":
        return {"device_id": str(profile.device_id), "gpu_mem_limit": str(profile.memory_limit_bytes),
                "do_copy_in_default_stream": "1", "arena_extend_strategy": "kSameAsRequested",
                "cudnn_conv_algo_search": "DEFAULT"}
    if provider == "MIGraphXExecutionProvider":
        return {"device_id": str(profile.device_id), "migraphx_mem_limit": str(profile.memory_limit_bytes),
                "migraphx_fp16_enable": "0", "migraphx_int8_enable": "0",
                "migraphx_exhaustive_tune": "0"}
    raise ValueError("provider unsupported")


def _place_child(profile):
    if any(value and key.startswith(("ORT_MIGRAPHX_", "ORT_CUDA_", "MIGRAPHX_"))
           for key, value in os.environ.items()):
        raise WorkerError("unsigned runtime environment override", "runtime_environment_override")
    requested = set(profile.cpu_affinity)
    if not requested <= os.sched_getaffinity(0):
        raise WorkerError("requested CPUs are not allowed", "cpu_affinity_unavailable")
    os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
    os.sched_setaffinity(0, requested)
    if os.sched_getscheduler(0) != os.SCHED_OTHER or os.sched_getaffinity(0) != requested:
        raise WorkerError("CPU placement readback mismatch", "execution_mismatch")


def _runtime_hash(path):
    """Open every ancestor without following links, including package parents."""
    path = Path(path).absolute()
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = new
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("runtime file must be regular")
            digest = hashlib.sha256()
            for chunk in iter(lambda: os.read(fd, 1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def _runtime_receipt(ort, provider, profile):
    if sys.implementation.cache_tag != profile.python_abi:
        raise WorkerError("Python ABI mismatch", "runtime_abi_mismatch")
    if ort.__version__ != profile.onnxruntime_version:
        raise WorkerError("ONNX Runtime version mismatch", "runtime_version_mismatch")
    # Check availability before vendor-file verification for useful CPU-only refusals.
    if provider not in ort.get_available_providers():
        raise WorkerError("requested provider is unavailable", "provider_unavailable")
    root = Path(ort.__file__).absolute().parent
    actual_pybind = Path(importlib.import_module("onnxruntime.capi.onnxruntime_pybind11_state").__file__).absolute()
    try:
        pybind_relative = actual_pybind.relative_to(root).as_posix()
    except ValueError as exc:
        raise WorkerError("pybind is outside installed package", "runtime_file_mismatch") from exc
    files = dict(profile.runtime_files)
    if (not pybind_relative.startswith("capi/onnxruntime_pybind11_state") or
            not pybind_relative.endswith(".so") or pybind_relative not in files):
        raise WorkerError("actual pybind is not pinned", "runtime_file_mismatch")
    actual = {}
    try:
        # The package origin itself and all ancestors must also be regular/no-link.
        _runtime_hash(Path(ort.__file__).absolute())
        for name, expected in profile.runtime_files:
            actual[name] = _runtime_hash(root / name)
            if actual[name] != expected:
                raise ValueError("runtime digest mismatch")
    except (OSError, ValueError) as exc:
        raise WorkerError("runtime file verification failed: " + str(exc), "runtime_file_mismatch") from exc
    return {"onnxruntime_version": ort.__version__, "python_abi": sys.implementation.cache_tag,
            "device_id": profile.device_id, "memory_limit_bytes": profile.memory_limit_bytes,
            "runtime_files": actual, "cpu_affinity": sorted(os.sched_getaffinity(0)),
            "scheduler": "SCHED_OTHER" if os.sched_getscheduler(0) == os.SCHED_OTHER else "unknown",
            "provider_options": _provider_options(provider, profile)}


def _check_session(session, provider, expected_options):
    if session.get_providers() != [provider]:
        raise WorkerError("provider readback mismatch", "provider_mismatch")
    readback = session.get_provider_options()
    if type(readback) is not dict or set(readback) != {provider}:
        raise WorkerError("provider option readback mismatch", "provider_mismatch")
    actual = readback[provider]
    if (type(actual) is not dict or (provider == "CPUExecutionProvider" and actual != {}) or
            any(actual.get(key) != value for key, value in expected_options.items())):
        raise WorkerError("provider option drift", "provider_mismatch")
    return {key: actual[key] for key in expected_options}


def _create_v2_session(ort, model, options, provider, profile):
    expected = _provider_options(provider, profile)
    # ORT 1.23 rejects disable_cpu_ep_fallback together with an explicit CPU EP.
    # CPU still has exactly one EP and Python fallback disabled; the session
    # config switch is required only for GPU graph partitioning.
    if provider != "CPUExecutionProvider":
        options.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
    session = ort.InferenceSession(model, sess_options=options, providers=[(provider, expected)])
    session.disable_fallback()
    _check_session(session, provider, expected)
    return session


class _Cancelled(Exception):
    pass


class Worker:
    def __init__(self, authorized_model: AuthorizedModel, python_command=None, load_timeout=30):
        if not isinstance(authorized_model, AuthorizedModel):
            raise TypeError("Worker requires AuthorizedModel")
        self.authorized_model = authorized_model
        self.manifest = authorized_model.manifest
        self._deny_reason = "model_revoked"
        self.command = list(python_command or [sys.executable])
        if not self.command or any(not isinstance(s, str) or not s for s in self.command):
            raise ValueError("python_command must be a nonempty argv list")
        if not math.isfinite(load_timeout) or not 0 < load_timeout <= 300:
            raise ValueError("invalid load_timeout")
        self.load_timeout = load_timeout
        self.process = None
        self._diagnostic_bytes = bytearray()
        self._diagnostic_lock = threading.Lock()
        self._stderr_read_lock = threading.Lock()
        self._stderr_thread = None
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._closed = False
        self._last_diagnostics = ""

    @property
    def pid(self):
        return self.process.pid if self.process is not None else None

    @property
    def alive(self):
        return self.process is not None and self.process.poll() is None

    def diagnostics(self):
        if self._closed:
            return self._last_diagnostics
        with self._diagnostic_lock:
            return bytes(self._diagnostic_bytes).decode(errors="replace")

    def _reset_diagnostics(self):
        with self._diagnostic_lock:
            self._diagnostic_bytes.clear()

    def _read_stderr(self, stream):
        try:
            fd = stream.fileno()
            while True:
                if not select.select([fd], [], [], .05)[0]:
                    continue
                if not self._read_diagnostic_chunk(fd):
                    return
        except (OSError, ValueError):
            return

    def _read_diagnostic_chunk(self, fd):
        # A collector must publish bytes before another reader can observe an
        # empty pipe; otherwise the success check can miss in-flight bytes.
        with self._stderr_read_lock:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                return True
            if chunk:
                with self._diagnostic_lock:
                    self._diagnostic_bytes.extend(chunk)
                    if len(self._diagnostic_bytes) > MAX_DIAGNOSTICS:
                        del self._diagnostic_bytes[:-MAX_DIAGNOSTICS]
            return bool(chunk)

    def _stderr_barrier(self):
        """Confirm an empty pipe or publish pending diagnostics synchronously."""
        self._read_diagnostic_chunk(self.process.stderr.fileno())

    def _stop(self):
        process = self.process
        if process is None:
            return
        streams = (process.stdin, process.stdout, process.stderr)
        if all(stream is None or stream.closed for stream in streams):
            return
        pid = process.pid
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=.15)
        except subprocess.TimeoutExpired:
            pass
        # The leader may have exited while a descendant still owns the
        # group's pipes.  Do not use leader reaping as group reaping.
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        try:
            if self._stderr_thread is not None:
                # Thread allocation does not transfer started-thread ownership.
                # A failed start has no joinable thread and must not mask the
                # primary startup error or prevent descriptor release.
                if self._stderr_thread.ident is not None:
                    self._stderr_thread.join(timeout=1)
                if self._stderr_thread.is_alive():
                    raise WorkerError("stderr collector did not stop")
                self._stderr_thread = None
        finally:
            for stream in streams:
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass


    def invalidate(self, reason):
        # Publish denial before waiting for an in-progress exchange/start lock.
        self._deny_reason = reason
        self.authorized_model.deny_event.set()
        self.close()

    def close(self):
        with self._lock:
            if not self._closed:
                self._stop()
                self._last_diagnostics = self.diagnostics()
                self._closed = True

    def _check(self, expires, cancel):
        if self.authorized_model.deny_event.is_set():
            raise WorkerError(self._deny_reason)
        if cancel is not None and cancel.is_set():
            raise _Cancelled()
        if time.monotonic() >= expires:
            raise TimeoutError()
        if not self.alive:
            raise WorkerError("worker process exited")

    def _exchange(self, document, expires, cancel=None):
        payload = (json.dumps(document, allow_nan=False, separators=(",", ":")) + "\n").encode()
        if len(payload) > MAX_LINE:
            raise RequestError("worker message exceeds size limit")
        remaining = memoryview(payload)
        while remaining:
            self._check(expires, cancel)
            if select.select([], [self.process.stdin], [], min(.02, max(0, expires-time.monotonic())))[1]:
                try:
                    written = os.write(self.process.stdin.fileno(), remaining)
                    remaining = remaining[written:]
                except BlockingIOError:
                    continue
        while True:
            self._check(expires, cancel)
            if b"\n" in self._buffer:
                line, _, rest = self._buffer.partition(b"\n")
                self._buffer = bytearray(rest)
                if len(line) > MAX_LINE:
                    raise WorkerError("oversized worker response")
                result = json.loads(line)
                if type(result) is not dict:
                    raise WorkerError("invalid worker response")
                return result
            if len(self._buffer) > MAX_LINE:
                raise WorkerError("oversized worker response")
            if select.select([self.process.stdout], [], [], min(.02, max(0, expires-time.monotonic())))[0]:
                chunk = os.read(self.process.stdout.fileno(), 8192)
                if not chunk:
                    raise WorkerError("worker output closed")
                self._buffer.extend(chunk)

    def start(self, cancel=None):
        with self._lock:
            if self.authorized_model.deny_event.is_set():
                raise WorkerError(self._deny_reason)
            if self._closed:
                raise WorkerError("worker is closed")
            if self.process is not None:
                if self.alive:
                    raise WorkerError("worker is already started")
                self._stop()
                self.process = None
                self._buffer.clear()
                self._reset_diagnostics()
            environment = dict(os.environ, PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
                               OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
            expires = time.monotonic() + self.load_timeout
            try:
                expected_execution = None
                if self.manifest.execution is not None:
                    profile_raw = _profile_raw(self.manifest.execution)
                    validate_execution_profile(self.manifest.provider, profile_raw)
                    expected_execution = {**profile_raw,
                                          "provider_options": _provider_options(self.manifest.provider, self.manifest.execution)}
                if cancel is not None and cancel.is_set():
                    raise _Cancelled()
                self.process = subprocess.Popen(
                    [*self.command, "-u", str(Path(__file__).resolve()), "--child"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=environment, start_new_session=True, bufsize=0,
                )
                os.set_blocking(self.process.stdin.fileno(), False)
                os.set_blocking(self.process.stdout.fileno(), False)
                self._stderr_thread = threading.Thread(
                    target=self._read_stderr, args=(self.process.stderr,), daemon=True,
                )
                os.set_blocking(self.process.stderr.fileno(), False)
                self._stderr_thread.start()
                raw = asdict(self.manifest)
                raw["model_path"] = str(raw["model_path"])
                if raw["execution"] is None:
                    del raw["execution"]  # Preserve old child/load wire compatibility.
                result = self._exchange({"op": "load", "manifest": raw}, expires, cancel)
                self._check(expires, cancel)
                if result.get("status") != "ready" or result.get("provider") != self.manifest.provider:
                    raise WorkerError(str(result.get("reason", "load failed")),
                                      reason=str(result.get("reason_code", "worker_failed")))
                if result.get("model_sha256") != self.manifest.sha256:
                    raise WorkerError("worker model hash mismatch")
                if expected_execution is not None:
                    actual_execution = result.get("execution")
                    try:
                        if type(actual_execution) is not dict or set(actual_execution) != set(expected_execution):
                            raise ValueError("execution receipt fields mismatch")
                        actual_profile = {key: value for key, value in actual_execution.items()
                                          if key != "provider_options"}
                        validate_execution_profile(self.manifest.provider, actual_profile)
                        if type(actual_execution["provider_options"]) is not dict:
                            raise ValueError("execution provider options mismatch")
                    except (TypeError, ValueError) as exc:
                        raise WorkerError("worker execution receipt mismatch", "execution_mismatch") from exc
                    if (actual_execution != expected_execution or
                            result.get("runtime_version") != expected_execution["onnxruntime_version"]):
                        raise WorkerError("worker execution receipt mismatch", "execution_mismatch")
                self._stderr_barrier()
                if self.diagnostics():
                    raise WorkerError("runtime emitted diagnostics: " + self.diagnostics()[-2000:])
                return result
            except (_Cancelled, TimeoutError) as exc:
                self._stop()
                reason = "startup cancelled" if isinstance(exc, _Cancelled) else "startup timeout"
                raise WorkerError(reason) from exc
            except WorkerError:
                self._stop()
                raise
            except Exception as exc:
                self._stop()
                raise WorkerError("startup failed: " + str(exc)[:2000]) from exc

    def restart(self, cancel=None):
        """Reload the same hash-bound model after a killed or crashed child."""
        return self.start(cancel=cancel)

    def infer(self, request_id, value, deadline_ms, cancel=None):
        if self.authorized_model.deny_event.is_set():
            return safe_result(request_id, "unavailable", self._deny_reason, self.manifest.name)
        started = time.monotonic()
        if type(request_id) is not str or not 1 <= len(request_id) <= 128:
            raise RequestError("id must be a string of length 1..128")
        if isinstance(deadline_ms, bool) or not isinstance(deadline_ms, (int, float)) or not math.isfinite(deadline_ms) or not 0 < deadline_ms <= 300000:
            return safe_result(request_id, "invalid", "invalid_deadline", self.manifest.name)
        expires = started + deadline_ms/1000
        try:
            normalized = validate_input(self.manifest, value)
        except RequestError:
            return safe_result(request_id, "invalid", "invalid_input", self.manifest.name)
        if not self._lock.acquire(blocking=False):
            return safe_result(request_id, "busy", "worker_busy", self.manifest.name)
        try:
            if self.authorized_model.deny_event.is_set():
                return safe_result(request_id, "unavailable", self._deny_reason, self.manifest.name)
            if self._closed or not self.alive:
                self._stop()
                return safe_result(request_id, "unavailable", "worker_unavailable", self.manifest.name)
            result = self._exchange({"op": "infer", "id": request_id, "input": normalized}, expires, cancel)
            self._check(expires, cancel)
            self._stderr_barrier()
            if self.diagnostics():
                raise WorkerError("runtime diagnostics")
            if result.get("id") != request_id or result.get("status") != "ok":
                raise WorkerError("worker result mismatch or execution error")
            return success_result(request_id, self.manifest, result.get("output"), (time.monotonic()-started)*1000)
        except (_Cancelled, TimeoutError) as exc:
            self._stop()
            code = "cancelled" if isinstance(exc, _Cancelled) else "timeout"
            return safe_result(request_id, code, code, self.manifest.name)
        except (OSError, ValueError, WorkerError) as exc:
            self._stop()
            result = safe_result(request_id, "error", "worker_failed", self.manifest.name)
            result["diagnostics"] = self.diagnostics()[-2000:] or type(exc).__name__
            return result
        finally:
            self._lock.release()


def _reply(document):
    sys.stdout.write(json.dumps(document, allow_nan=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _child():
    """Execute only a hash-bound in-memory graph, never an arbitrary command."""
    try:
        load_line = sys.stdin.buffer.readline(MAX_LINE + 1)
        if len(load_line) > MAX_LINE or not load_line.endswith(b"\n"):
            raise ValueError("invalid worker load message")
        document = json.loads(load_line)
        raw = document["manifest"]
        raw["model_path"] = Path(raw["model_path"])
        raw["input_shape"] = tuple(raw["input_shape"])
        raw["output_shape"] = tuple(raw["output_shape"])
        if raw.get("execution") is not None:
            raw["execution"] = _wire_profile(raw["provider"], raw["execution"])
        manifest = ModelManifest(**raw)
        if document["op"] != "load":
            raise ValueError("load must be first")
        model = manifest.model_path.read_bytes()
        if hashlib.sha256(model).hexdigest() != manifest.sha256:
            raise ValueError("model bytes changed after validation")
        if manifest.execution is not None:
            _place_child(manifest.execution)
        import numpy as np
        import onnxruntime as ort
        receipt = None
        if manifest.execution is not None:
            receipt = _runtime_receipt(ort, manifest.provider, manifest.execution)
        if manifest.provider not in ort.get_available_providers():
            raise ValueError("requested provider is unavailable")
        options = ort.SessionOptions()
        options.intra_op_num_threads = manifest.intra_op_threads
        options.inter_op_num_threads = manifest.inter_op_threads
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        session = (_create_v2_session(ort, model, options, manifest.provider, manifest.execution)
                   if manifest.execution is not None else
                   ort.InferenceSession(model, sess_options=options, providers=[manifest.provider]))
        inputs, outputs = session.get_inputs(), session.get_outputs()
        if len(inputs) != 1 or len(outputs) != 1 or session.get_providers() != [manifest.provider]:
            raise ValueError("model/provider contract mismatch")
        for meta, name, shape in ((inputs[0], manifest.input_name, manifest.input_shape),
                                  (outputs[0], manifest.output_name, manifest.output_shape)):
            if meta.name != name or meta.type != "tensor(float)" or tuple(meta.shape) != shape:
                raise ValueError("model tensor metadata mismatch")
        smoke = session.run([manifest.output_name],
                            {manifest.input_name: np.zeros(manifest.input_shape, dtype=np.float32)})[0]
        validate_output(manifest, smoke.tolist())
        if receipt is not None:
            receipt["provider_options"] = _check_session(session, manifest.provider, receipt["provider_options"])
        _reply({"status": "ready", "provider": manifest.provider,
                "model_sha256": manifest.sha256, "runtime_version": ort.__version__,
                **({"execution": receipt} if receipt is not None else {})})
    except Exception as exc:
        _reply({"status": "error", "reason": type(exc).__name__ + ": " + str(exc)[:2000],
                "reason_code": getattr(exc, "reason", "worker_failed")})
        return 1
    while True:
        line = sys.stdin.buffer.readline(MAX_LINE + 1)
        if not line:
            return 0
        if len(line) > MAX_LINE or not line.endswith(b"\n"):
            return 1
        request_id = None
        try:
            request = json.loads(line)
            request_id = request["id"]
            if request["op"] != "infer":
                raise ValueError("unsupported worker operation")
            value = validate_input(manifest, request["input"])
            if receipt is not None:
                _check_session(session, manifest.provider, receipt["provider_options"])
            result = session.run([manifest.output_name],
                                 {manifest.input_name: np.asarray(value, dtype=np.float32)})[0]
            output = validate_output(manifest, result.tolist())
            if receipt is not None:
                _check_session(session, manifest.provider, receipt["provider_options"])
            _reply({"status": "ok", "id": request_id, "output": output})
        except Exception as exc:
            _reply({"status": "error", "id": request_id, "reason": type(exc).__name__})


if __name__ == "__main__":
    if sys.argv[1:] != ["--child"]:
        raise SystemExit("worker is an internal child entry point")
    raise SystemExit(_child())
