#!/usr/bin/env python3
"""Reconstruct the tiny deterministic 16-input ONNX transport fixture.

Requires the development-only pinned ONNX/NumPy environment. This arithmetic
model has no trained parameters and supplies no EEG task or action authority.
"""
from pathlib import Path
import argparse
import hashlib
import json
import sys


def model_bytes():
    """Return deterministic opset17/IR9 MatMul+Add+Softmax model bytes."""
    import numpy as np
    from onnx import TensorProto, checker, helper, numpy_helper

    weights = np.array([[((i + 1) % 5 - 2) / 256.0,
                         ((i + 2) % 7 - 3) / 256.0,
                         ((i + 3) % 11 - 5) / 256.0]
                        for i in range(16)], dtype=np.float32)
    bias = np.array([0.125, -0.125, 0.0], dtype=np.float32)
    graph = helper.make_graph([
        helper.make_node('MatMul', ['features_uv', 'weights'], ['linear']),
        helper.make_node('Add', ['linear', 'bias'], ['logits']),
        helper.make_node('Softmax', ['logits'], ['probabilities'], axis=1),
    ], 'xtos-mean-rms-transport-fixture',
        [helper.make_tensor_value_info('features_uv', TensorProto.FLOAT, [1, 16])],
        [helper.make_tensor_value_info('probabilities', TensorProto.FLOAT, [1, 3])],
        [numpy_helper.from_array(weights, 'weights'), numpy_helper.from_array(bias, 'bias')])
    model = helper.make_model(graph, producer_name='XTOS transport fixture',
                              opset_imports=[helper.make_opsetid('', 17)])
    model.ir_version = 9
    checker.check_model(model)
    return model.SerializeToString()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        data = model_bytes()
        # Serialize before create-only publication; existing paths are preserved.
        with args.output.open('xb') as stream:
            stream.write(data)
        print(json.dumps({'sha256': hashlib.sha256(data).hexdigest(),
                          'bytes': len(data), 'safe_action': 'no_action',
                          'source_kind': 'fixture'}, sort_keys=True))
    except (OSError, ValueError, ImportError) as exc:
        print(f'fixture generation failed: {type(exc).__name__}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
