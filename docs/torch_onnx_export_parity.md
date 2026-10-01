# PyTorch 到 ONNX 输出对齐基线

## 范围

本基线验证固定 PyTorch eager 模型导出为 ONNX 后，使用 ONNX Runtime `CPUExecutionProvider` 推理的输出是否与 PyTorch eager 输出一致。它是阶段 3C 的用户态 CPU 对齐测试，不是 GPU 硬实时验证、真实 EEG 准确率验证、闭环控制安全验证或医疗认证。

## 固定契约

- Torch 模型：`Linear(4, 3) + Softmax(dim=1)`。
- 权重：前三个输入分别映射到三个输出，偏置为零。
- 输入：`[[2.0, 0.0, -1.0, 0.0]]`，形状 `[1, 4]`，`float32`。
- 输出：三类概率，形状 `[1, 3]`。
- 导出：PyTorch ONNX exporter，opset 13。
- Runtime：只使用 `CPUExecutionProvider`。
- 线程：Torch intra-op/inter-op 均为 1；ONNX Runtime intra-op/inter-op 均为 1。
- 对齐门槛：最大绝对误差 `<= 1e-5`，平均绝对误差 `<= 1e-6`，预测类别索引一致。
- 结果模型：导出的 ONNX 文件写入 `.build/benchmarks/`，不提交到仓库。

## 运行

依赖是可选的，推荐在隔离目录中提供 Torch、ONNX、ONNX Runtime、NumPy 和匹配的 Protobuf：

```sh
XTOS_TORCH_ROOT=.build/torch-root \
XTOS_ONNX_ROOT=.build/onnx-wheel-root \
XTOS_PROTOBUF_ROOT=.build/onnx-wheel-root \
./scripts/benchmark_torch_onnx.sh
```

结果写入：

```text
.build/benchmarks/xtos-torch-onnx-parity-v1.json
.build/benchmarks/torch_classifier.onnx
```

工具也可以直接执行：

```sh
XTOS_TORCH_ROOT=.build/torch-root \
XTOS_ONNX_ROOT=.build/onnx-wheel-root \
XTOS_PROTOBUF_ROOT=.build/onnx-wheel-root \
python3 tools/xtos-onnx-baseline/torch_export.py \
  --output .build/benchmarks/torch_classifier.onnx
```

## 结果和降级

成功结果必须包含：

- Torch 和 ONNX Runtime 版本；
- `CPUExecutionProvider`；
- 输入/输出形状和线程配置；
- 导出模型 SHA-256；
- Torch/ONNX 输出、最大/平均绝对误差；
- 两边的预测类别索引；
- 当前 Git 提交标识。

Torch、ONNX、ONNX Runtime、NumPy 或匹配 Protobuf 缺失时，结果为 `status=unverified`、`safe_action=no_action`。导出失败或输出不一致时不能标记为 `action_allowed`。

## 已验证边界

本基线已在隔离环境中真实执行 Torch eager、ONNX 导出和 ONNX Runtime CPU 推理的端到端对齐。该证据只覆盖固定模型、固定输入和 x86_64 用户态环境；真实模型、真实 EEG、GPU、目标硬件最坏延迟、长时间压力和医疗安全仍未验证。
