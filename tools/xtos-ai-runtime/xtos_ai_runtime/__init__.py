"""Public exports for the XTOS AI runtime protocol."""

RUNTIME_VERSION = "1.1.0"

from .protocol import (
    ExecutionProfile,
    ManifestError,
    ModelManifest,
    RequestError,
    safe_result,
    success_result,
    validate_input,
    validate_execution_profile,
    validate_manifest,
    validate_output,
)

__all__ = [
    "ExecutionProfile",
    "ManifestError",
    "ModelManifest",
    "RequestError",
    "safe_result",
    "success_result",
    "validate_input",
    "validate_execution_profile",
    "validate_manifest",
    "validate_output",
]
