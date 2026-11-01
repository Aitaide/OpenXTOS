"""Validated local-model manifest and fail-closed response protocol."""

from __future__ import annotations

import hashlib
import math
import re
import struct
from dataclasses import dataclass
from numbers import Real
from pathlib import Path
from typing import Any

_SCHEMA = "xtos-ai-runtime-v1"
_MANIFEST_KEYS = {"name", "version", "model_path", "sha256", "provider", "threads", "input", "output"}
_TENSOR_KEYS = {"name", "dtype", "shape"}
_THREAD_KEYS = {"intra_op", "inter_op"}


class ManifestError(ValueError):
    """A model manifest or its model-file binding is invalid."""


class RequestError(ValueError):
    """A value cannot be accepted by the inference protocol."""


@dataclass(frozen=True, slots=True)
class TensorSpec:
    """Validated tensor metadata exposed on a bound manifest."""

    name: str
    dtype: str
    shape: tuple[int, int]


@dataclass(frozen=True, slots=True)
class ThreadSpec:
    """Validated ONNX Runtime thread settings exposed on a manifest."""

    intra_op: int
    inter_op: int


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    """Signed provider configuration, detached from caller-owned JSON."""

    onnxruntime_version: str
    python_abi: str
    device_id: int
    memory_limit_bytes: int
    runtime_files: tuple[tuple[str, str], ...]
    cpu_affinity: tuple[int, ...]
    scheduler: str


def validate_execution_profile(provider: str, raw: Any) -> ExecutionProfile:
    """Validate provider configuration structurally, without filesystem access."""
    if provider == "ROCMExecutionProvider":
        raise ManifestError("provider_deprecated")
    if type(provider) is not str or provider not in ("CPUExecutionProvider", "CUDAExecutionProvider", "MIGraphXExecutionProvider"):
        raise ManifestError("provider_unsupported")
    _keys(raw, {"onnxruntime_version", "python_abi", "device_id", "memory_limit_bytes",
                "runtime_files", "cpu_affinity", "scheduler"}, "execution")
    version = raw["onnxruntime_version"]
    if (type(version) is not str or len(version) > 32 or
            not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version) or
            any(int(part) > 2_147_483_647 for part in version.split("."))):
        raise ManifestError("execution.onnxruntime_version must be canonical semver")
    abi = raw["python_abi"]
    if type(abi) is not str or len(abi) > 64 or not re.fullmatch(r"cpython-[1-9][0-9]*", abi):
        raise ManifestError("execution.python_abi must be canonical cpython ABI")
    gpu = provider != "CPUExecutionProvider"
    _integer(raw["device_id"], "execution.device_id", 0, 31 if gpu else 0)
    _integer(raw["memory_limit_bytes"], "execution.memory_limit_bytes", 1048576 if gpu else 0,
             68719476736 if gpu else 0)
    if type(raw["scheduler"]) is not str or raw["scheduler"] != "SCHED_OTHER":
        raise ManifestError("execution.scheduler must be SCHED_OTHER")
    files = raw["runtime_files"]
    if type(files) is not dict or not 1 <= len(files) <= 32:
        raise ManifestError("execution.runtime_files must contain 1..32 files")
    for path, digest in files.items():
        if (type(path) is not str or not 1 <= len(path) <= 256 or
                path.startswith("/") or "\\" in path or "\x00" in path or
                any(part in ("", ".", "..") for part in path.split("/"))):
            raise ManifestError("execution.runtime_files path must be a safe relative path")
        if type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ManifestError("execution.runtime_files digest must be lowercase SHA256")
    if not any(re.fullmatch(r"capi/onnxruntime_pybind11_state[^/]*\.so", path) for path in files):
        raise ManifestError("execution.runtime_files requires pybind library")
    vendor_file = {"CUDAExecutionProvider": "capi/libonnxruntime_providers_cuda.so",
                   "MIGraphXExecutionProvider": "capi/libonnxruntime_providers_migraphx.so"}.get(provider)
    if vendor_file is not None and vendor_file not in files:
        raise ManifestError("execution.runtime_files requires requested provider library")
    cpus = raw["cpu_affinity"]
    if type(cpus) is not list or not 1 <= len(cpus) <= 256:
        raise ManifestError("execution.cpu_affinity must contain 1..256 CPU IDs")
    for cpu in cpus:
        _integer(cpu, "execution.cpu_affinity CPU", 0, 4095)
    if cpus != sorted(set(cpus)):
        raise ManifestError("execution.cpu_affinity must be sorted and unique")
    return ExecutionProfile(raw["onnxruntime_version"], raw["python_abi"], raw["device_id"],
                            raw["memory_limit_bytes"], tuple(sorted(raw["runtime_files"].items())),
                            tuple(raw["cpu_affinity"]), raw["scheduler"])


@dataclass(frozen=True, slots=True)
class ModelManifest:
    """Immutable, validated model metadata and tensor contract."""

    name: str
    version: str
    model_path: Path
    sha256: str
    provider: str
    intra_op_threads: int
    inter_op_threads: int
    input_name: str
    input_dtype: str
    input_shape: tuple[int, int]
    output_name: str
    output_dtype: str
    output_shape: tuple[int, int]
    execution: ExecutionProfile | None = None

    @property
    def input(self) -> TensorSpec:
        return TensorSpec(self.input_name, self.input_dtype, self.input_shape)

    @property
    def output(self) -> TensorSpec:
        return TensorSpec(self.output_name, self.output_dtype, self.output_shape)

    @property
    def threads(self) -> ThreadSpec:
        return ThreadSpec(self.intra_op_threads, self.inter_op_threads)

    @property
    def intra_op(self) -> int:
        return self.intra_op_threads

    @property
    def inter_op(self) -> int:
        return self.inter_op_threads


def _keys(value: Any, expected: set[str], label: str) -> None:
    if type(value) is not dict or any(type(key) is not str for key in value) or set(value) != expected:
        raise ManifestError(f"{label} must contain exactly {sorted(expected)!r}")


def _string(value: Any, label: str, error: type[ValueError] = ManifestError) -> str:
    if type(value) is not str or not value:
        raise error(f"{label} must be a non-empty string")
    return value


def _integer(value: Any, label: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ManifestError(f"{label} must be an integer in [{low}, {high}]")
    return value


def _shape(raw: Any, label: str) -> tuple[int, int]:
    if type(raw) is not list or len(raw) != 2:
        raise ManifestError(f"{label}.shape must have rank 2")
    dims = tuple(_integer(d, f"{label}.shape dimension", 1, 65536) for d in raw)
    if dims[0] > 64 or dims[0] * dims[1] > 65536:
        raise ManifestError(f"{label}.shape exceeds batch or element limits")
    return dims


def _tensor(raw: Any, label: str) -> tuple[str, str, tuple[int, int]]:
    _keys(raw, _TENSOR_KEYS, label)
    name = _string(raw["name"], f"{label}.name")
    dtype = _string(raw["dtype"], f"{label}.dtype")
    if dtype != "float32":
        raise ManifestError(f"{label}.dtype must be float32")
    return name, dtype, _shape(raw["shape"], label)


def _model_file(root_value: Any, path_value: Any) -> Path:
    if type(path_value) is not str or not path_value:
        raise ManifestError("model_path must be a non-empty string")
    try:
        root = Path(root_value).resolve()
        candidate = Path(path_value)
        candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
        candidate.relative_to(root)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ManifestError("model_path must remain inside model_root") from exc
    if not root.is_dir():
        raise ManifestError("model_root must be an existing directory")
    if not candidate.is_file():
        raise ManifestError("model_path must be an existing regular file")
    return candidate


def validate_manifest(raw: Any, model_root: str | Path) -> ModelManifest:
    """Validate exact schema, policy bounds, root containment, and SHA-256."""
    v2 = type(raw) is dict and raw.get("schema") == "xtos-ai-manifest-v2"
    _keys(raw, _MANIFEST_KEYS | {"schema", "execution"} if v2 else _MANIFEST_KEYS, "manifest")
    name = _string(raw["name"], "name")
    version = _string(raw["version"], "version")
    provider = _string(raw["provider"], "provider")
    if not v2:
        if provider != "CPUExecutionProvider":
            raise ManifestError("provider must be CPUExecutionProvider")
    execution = validate_execution_profile(provider, raw["execution"]) if v2 else None

    _keys(raw["threads"], _THREAD_KEYS, "threads")
    intra = _integer(raw["threads"]["intra_op"], "threads.intra_op", 1, 64)
    inter = _integer(raw["threads"]["inter_op"], "threads.inter_op", 1, 64)
    in_name, in_dtype, in_shape = _tensor(raw["input"], "input")
    out_name, out_dtype, out_shape = _tensor(raw["output"], "output")

    expected = _string(raw["sha256"], "sha256")
    if len(expected) != 64 or any(c not in "0123456789abcdefABCDEF" for c in expected):
        raise ManifestError("sha256 must be a 64-character hexadecimal digest")
    path = _model_file(model_root, raw["model_path"])
    digest = hashlib.sha256()
    try:
        with path.open("rb") as model_file:
            for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ManifestError("model_path cannot be read") from exc
    actual = digest.hexdigest()
    if actual != expected.lower():
        raise ManifestError("sha256 does not match model_path")

    return ModelManifest(
        name, version, path, actual, provider, intra, inter,
        in_name, in_dtype, in_shape, out_name, out_dtype, out_shape, execution,
    )


def _matrix(value: Any, shape: tuple[int, int], label: str) -> list[list[float]]:
    if type(value) is not list or len(value) != shape[0]:
        raise RequestError(f"{label} must have shape {shape}")
    result = []
    for row in value:
        if type(row) is not list or len(row) != shape[1]:
            raise RequestError(f"{label} must have shape {shape}")
        values = []
        for item in row:
            if isinstance(item, bool) or not isinstance(item, Real):
                raise RequestError(f"{label} values must be numbers")
            try:
                number = float(item)
                if not math.isfinite(number):
                    raise RequestError(f"{label} values must be finite")
                number = struct.unpack(">f", struct.pack(">f", number))[0]
            except (OverflowError, struct.error, TypeError, ValueError) as exc:
                if isinstance(exc, RequestError):
                    raise
                raise RequestError(f"{label} value is outside finite float32 range") from exc
            if not math.isfinite(number):
                raise RequestError(f"{label} value is outside finite float32 range")
            values.append(number)
        result.append(values)
    return result


def validate_input(manifest: ModelManifest, value: Any) -> list[list[float]]:
    """Return a shape-checked matrix normalized to finite float32 numbers."""
    if not isinstance(manifest, ModelManifest):
        raise RequestError("manifest must be a ModelManifest")
    return _matrix(value, manifest.input_shape, "input")


def validate_output(manifest: ModelManifest, value: Any) -> list[list[float]]:
    """Return a shape-checked matrix normalized to finite float32 numbers."""
    if not isinstance(manifest, ModelManifest):
        raise RequestError("manifest must be a ModelManifest")
    return _matrix(value, manifest.output_shape, "output")


def _request_id(value: Any) -> str:
    if type(value) is not str or not 1 <= len(value) <= 128:
        raise RequestError("id must be a string of length 1..128")
    return value


def safe_result(request_id: Any, status: Any, reason: Any, model: Any = None) -> dict[str, Any]:
    """Construct a response that always denies action and omits output."""
    request_id = _request_id(request_id)
    status = _string(status, "status", RequestError)
    reason = _string(reason, "reason", RequestError)
    if model is not None and type(model) is not str:
        raise RequestError("model must be a string or null")
    return {
        "schema": _SCHEMA, "id": request_id, "status": status,
        "safe_action": "no_action", "reason": reason, "model": model, "output": None,
    }


def success_result(request_id: Any, manifest: ModelManifest, output: Any, latency_ms: Any) -> dict[str, Any]:
    """Authorize action only for a valid output and finite non-negative latency."""
    request_id = _request_id(request_id)
    checked_output = validate_output(manifest, output)
    if isinstance(latency_ms, bool) or not isinstance(latency_ms, Real):
        raise RequestError("latency_ms must be finite and non-negative")
    try:
        latency = float(latency_ms)
    except (OverflowError, TypeError, ValueError) as exc:
        raise RequestError("latency_ms must be finite and non-negative") from exc
    if not math.isfinite(latency) or latency < 0:
        raise RequestError("latency_ms must be finite and non-negative")
    return {
        "schema": _SCHEMA, "id": request_id, "status": "success",
        "safe_action": "action_allowed", "reason": None, "model": manifest.name,
        "provider": manifest.provider, "output": checked_output, "latency_ms": latency,
    }
