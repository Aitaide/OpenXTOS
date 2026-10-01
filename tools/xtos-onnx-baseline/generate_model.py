#!/usr/bin/env python3
"""Generate the deterministic ONNX CPU baseline fixture."""

from __future__ import annotations

import argparse
from pathlib import Path

import onnx
from onnx import TensorProto, helper


def build_model(path: Path) -> None:
    input_info = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 4])
    output_info = helper.make_tensor_value_info("probabilities", TensorProto.FLOAT, [1, 3])
    weights = helper.make_tensor(
        "weights",
        TensorProto.FLOAT,
        [4, 3],
        [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
    )
    bias = helper.make_tensor("bias", TensorProto.FLOAT, [3], [0.0, 0.0, 0.0])
    nodes = [
        helper.make_node("MatMul", ["input", "weights"], ["matmul"]),
        helper.make_node("Add", ["matmul", "bias"], ["logits"]),
        helper.make_node("Softmax", ["logits"], ["probabilities"], axis=1),
    ]
    graph = helper.make_graph(
        nodes,
        "xtos_fixed_classifier",
        [input_info],
        [output_info],
        initializer=[weights, bias],
    )
    model = helper.make_model(
        graph,
        producer_name="xtos-onnx-baseline",
        opset_imports=[helper.make_opsetid("", 13)],
        ir_version=9,
    )
    onnx.checker.check_model(model)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(model.SerializeToString())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build_model(args.output)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
