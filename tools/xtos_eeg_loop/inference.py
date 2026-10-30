"""One-window CPU tensor transport. Successful inference grants no authority."""
from dataclasses import asdict
import hashlib
import json
import math
import re
import time

from .feature_binding import BindingError, prepare_inference
from xtos_ai_runtime.client import request


def _rejected(request_id, reason):
    identity = request_id if type(request_id) is str and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', request_id) else None
    return {'status': 'rejected', 'safe_action': 'no_action', 'reason': reason,
            'request_id': identity, 'output': None}


def infer_window(binding, record, socket_path, request_id, deadline_ms, *, now_ns=time.monotonic_ns):
    """Send a validated immutable window to the pinned local CPU model."""
    try:
        started = now_ns()
        prepared = prepare_inference(binding, record, started, request_id, deadline_ms)
    except (BindingError, OSError, ValueError, TypeError, OverflowError):
        return _rejected(request_id, 'invalid_features')
    try:
        response = request(socket_path, prepared.request_document(), timeout=min(300, deadline_ms/1000+0.25))
    except TimeoutError:
        return _rejected(request_id, 'deadline_exceeded')
    except (OSError, ValueError, TypeError, OverflowError):
        return _rejected(request_id, 'inference_unavailable')
    if response['status'] != 'success' or response['safe_action'] != 'action_allowed':
        return _rejected(request_id, 'inference_refused')
    try:
        completed = now_ns()
    except (OSError, ValueError, TypeError, OverflowError):
        return _rejected(request_id, 'invalid_clock')
    if type(completed) is not int or not started <= completed <= (1 << 64)-1:
        return _rejected(request_id, 'invalid_clock')
    if completed - started >= deadline_ms * 1_000_000:
        return _rejected(request_id, 'deadline_exceeded')
    if completed - prepared.first_host_rx_ns > prepared.max_receive_age_ns:
        return _rejected(request_id, 'stale_result')
    output = response['output']
    if (len(output) != 1 or len(output[0]) != 3 or
            any(not math.isfinite(value) or not 0 <= value <= 1 for value in output[0]) or
            abs(math.fsum(output[0])-1.0) > 1e-5):
        return _rejected(request_id, 'invalid_probabilities')
    digest = hashlib.sha256(json.dumps(asdict(prepared), sort_keys=True,
                            separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest()
    return {'status': 'transport_verified', 'safe_action': 'no_action', 'reason': 'transport_only',
            'request_id': prepared.request_id, 'model': prepared.model,
            'model_sha256': prepared.expected_model_sha256, 'source_kind': prepared.source_kind,
            'session_id': prepared.session_id, 'stream_generation': prepared.stream_generation,
            'config_generation': prepared.config_generation, 'window_id': prepared.window_id,
            'barrier_id': prepared.barrier_id, 'feature_digest': digest,
            'completed_at_ns': completed, 'output': [list(response['output'][0])]}
