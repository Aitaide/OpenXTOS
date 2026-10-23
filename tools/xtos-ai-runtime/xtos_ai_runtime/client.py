"""Strict synchronous client for the XTOS AF_UNIX JSON-lines protocol."""
from __future__ import annotations

import json
import math
import socket
import struct
from pathlib import Path
from typing import Any

from .server import MAX_REQUEST_BYTES, SCHEMA, parse_request_line


def _validate_output(value: Any) -> None:
    if type(value) is not list or not 1 <= len(value) <= 64:
        raise ValueError("response output must be a bounded rank-2 tensor")
    if type(value[0]) is not list or not value[0]:
        raise ValueError("response output must be a non-empty matrix")
    width = len(value[0])
    if len(value) * width > 65536:
        raise ValueError("response output exceeds tensor limits")
    for row in value:
        if type(row) is not list or len(row) != width:
            raise ValueError("response output must be rectangular")
        for item in row:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise ValueError("response output must contain numbers")
            try:
                number = struct.unpack(">f", struct.pack(">f", float(item)))[0]
            except (OverflowError, struct.error, TypeError, ValueError) as exc:
                raise ValueError("response output is outside float32 range") from exc
            if not math.isfinite(number):
                raise ValueError("response output must be finite float32")


def request(socket_path: str | Path, document: dict[str, Any], timeout: float = 2.0) -> dict[str, Any]:
    if type(document) is not dict:
        raise ValueError("request must be a JSON object")
    request_id = document.get("id")
    if type(request_id) is not str or not 1 <= len(request_id) <= 128:
        raise ValueError("request id must be a string of length 1..128")
    expected_model = document.get("model")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("timeout must be finite and in (0, 300]")
    try:
        payload = (json.dumps(document, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("request is not finite JSON data") from exc
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("request exceeds size limit")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(float(timeout))
        connection.connect(str(socket_path))
        connection.sendall(payload)
        chunks = bytearray()
        while len(chunks) <= MAX_REQUEST_BYTES:
            chunk = connection.recv(min(8192, MAX_REQUEST_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if b"\n" in chunks:
                break
    response = parse_request_line(bytes(chunks))
    if response.get("schema") != SCHEMA or response.get("id") != request_id:
        raise ValueError("response schema or request identity mismatch")
    if response.get("safe_action") not in ("no_action", "action_allowed"):
        raise ValueError("response safe_action is invalid")
    status = response.get("status")
    allowed_statuses = {"ok", "success", "invalid", "unavailable", "busy", "timeout", "cancelled", "error"}
    if type(status) is not str or status not in allowed_statuses:
        raise ValueError("response status is invalid")
    if "reason" not in response or "output" not in response:
        raise ValueError("response lacks required safe-result fields")
    reason = response["reason"]
    if reason is not None and (type(reason) is not str or not reason):
        raise ValueError("response reason is invalid")
    for field in ("model", "provider"):
        if field in response and response[field] is not None and (
                type(response[field]) is not str or not response[field]):
            raise ValueError(f"response {field} is invalid")
    if response.get("provider") not in (None, "CPUExecutionProvider"):
        raise ValueError("response provider is invalid")
    if "latency_ms" in response:
        latency = response["latency_ms"]
        try:
            valid_latency = (type(latency) in (int, float)
                             and math.isfinite(float(latency)) and latency >= 0)
        except (OverflowError, TypeError, ValueError):
            valid_latency = False
        if not valid_latency:
            raise ValueError("response latency is invalid")
    if response["safe_action"] == "action_allowed":
        if document.get("op") != "infer" or status != "success" or reason is not None:
            raise ValueError("success response violates safe-result contract")
        if type(response.get("model")) is not str or not response["model"]:
            raise ValueError("success response lacks model identity")
        if type(expected_model) is not str or not expected_model:
            raise ValueError("infer request requires a non-empty model identity for action_allowed")
        if response["model"] != expected_model:
            raise ValueError("success response model identity mismatch")
        if response.get("provider") != "CPUExecutionProvider" or "latency_ms" not in response:
            raise ValueError("success response lacks provider or latency")
        _validate_output(response["output"])
    elif (expected_model is not None and response.get("model") is not None
          and response["model"] != expected_model):
        raise ValueError("no_action response model identity mismatch")
    elif status == "success" or response["output"] is not None:
        raise ValueError("no_action response must not expose success or output")
    elif status != "ok" and reason is None:
        raise ValueError("failed response must include a reason")
    return response
