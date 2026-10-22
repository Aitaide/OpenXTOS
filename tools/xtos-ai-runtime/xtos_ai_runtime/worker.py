"""Killable single-request ONNX worker. No inference runs in the supervisor."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
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
    ModelManifest, RequestError, safe_result, success_result, validate_input, validate_output,
)

from xtos_ai_runtime.trust import AuthorizedModel

MAX_LINE = 65536
MAX_DIAGNOSTICS = 8192


class WorkerError(RuntimeError):
    """The process could not load a validated, diagnostic-clean runtime."""


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
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)
            if self._stderr_thread.is_alive():
                raise WorkerError("stderr collector did not stop")
            self._stderr_thread = None
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
                result = self._exchange({"op": "load", "manifest": raw}, expires, cancel)
                self._check(expires, cancel)
                if result.get("status") != "ready" or result.get("provider") != self.manifest.provider:
                    raise WorkerError(str(result.get("reason", "load failed")))
                if result.get("model_sha256") != self.manifest.sha256:
                    raise WorkerError("worker model hash mismatch")
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
        manifest = ModelManifest(**raw)
        if document["op"] != "load":
            raise ValueError("load must be first")
        model = manifest.model_path.read_bytes()
        if hashlib.sha256(model).hexdigest() != manifest.sha256:
            raise ValueError("model bytes changed after validation")
        import numpy as np
        import onnxruntime as ort
        if manifest.provider not in ort.get_available_providers():
            raise ValueError("requested provider is unavailable")
        options = ort.SessionOptions()
        options.intra_op_num_threads = manifest.intra_op_threads
        options.inter_op_num_threads = manifest.inter_op_threads
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        session = ort.InferenceSession(model, sess_options=options, providers=[manifest.provider])
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
        _reply({"status": "ready", "provider": manifest.provider,
                "model_sha256": manifest.sha256, "runtime_version": ort.__version__})
    except Exception as exc:
        _reply({"status": "error", "reason": type(exc).__name__ + ": " + str(exc)[:2000]})
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
            result = session.run([manifest.output_name],
                                 {manifest.input_name: np.asarray(value, dtype=np.float32)})[0]
            output = validate_output(manifest, result.tolist())
            _reply({"status": "ok", "id": request_id, "output": output})
        except Exception as exc:
            _reply({"status": "error", "id": request_id, "reason": type(exc).__name__})


if __name__ == "__main__":
    if sys.argv[1:] != ["--child"]:
        raise SystemExit("worker is an internal child entry point")
    raise SystemExit(_child())
