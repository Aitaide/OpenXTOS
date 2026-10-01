# ONNX Runtime CPU 软实时基线

## 范围

`tools/xtos-onnx-baseline/`提供阶段3C第一项的可重复基线：使用固定的3分类ONNX图，在单线程`CPUExecutionProvider`上运行，比较模型输出与固定参考值，并记录推理延迟分位数。

它验证的是用户态CPU推理路径和降级契约，不是GPU硬实时、真实EEG准确率、闭环控制安全或医疗认证。

## 固定输入和模型

- 模型：`tools/xtos-onnx-baseline/fixtures/fixed_classifier.onnx`
- 模型SHA-256：`6e6226cf772934a3f7a398ecb55ffd290503497d91803ff499a7aa28bf90a113`
- 输入形状：`[1, 4]`
- 输入：`[2.0, 0.0, -1.0, 0.0]`
- 输出：3类Softmax概率
- 线程：`intra_op=1`、`inter_op=1`
- 输出误差门槛：最大绝对误差不超过`1e-5`
- 结果：`action_allowed`只在模型加载、质量、置信度和截止时间均通过时出现

模型生成器是`generate_model.py`，只用于重新生成和校验固定fixture；生成模型不依赖PyTorch或TensorFlow。

## 运行

如果主机已经有`onnxruntime`和`numpy`：

```sh
./scripts/benchmark_onnx.sh
```

可以通过`XTOS_ONNX_ROOT`指定隔离依赖目录。目录支持两种布局：Debian解包布局（`usr/lib/python3/dist-packages`和`usr/lib/x86_64-linux-gnu`）以及pip wheel解包布局（Python包根目录和`onnxruntime/capi`）。例如：

```sh
BUILD_ROOT=.build \
XTOS_ONNX_ROOT=.build/onnx-wheel-root \
ITERATIONS=1000 DEADLINE_MS=1000 \
./scripts/benchmark_onnx.sh
```

结果写入`.build/benchmarks/xtos-onnx-cpu-baseline-v1.json`，包括Runtime版本、provider、模型SHA、输入形状、线程数、提交号、CPU标识和`p50/p95/p99/p99.9/max`延迟。

测试入口：

```sh
PYTHONDONTWRITEBYTECODE=1 \
XTOS_ONNX_ROOT=.build/onnx-wheel-root \
python3 -m unittest discover -s tools/xtos-onnx-baseline -p 'test_onnx_baseline.py' -v
```

## 当前验证

本次隔离验证使用官方`onnxruntime` CPU wheel 1.23.2和NumPy 2.2.4；推理使用`CPUExecutionProvider`，固定模型输出最大绝对误差为约`7.45e-09`，5次烟测stderr为空。1000次基准的具体延迟随CPU、负载、虚拟化和Runtime构建变化，以生成的JSON为准。

Debian提供的ONNX Runtime 1.21.0也能完成推理，但其外置`libonnx`组合会在创建会话时输出大量重复schema注册诊断；该路径不作为干净基准证据。基准应优先使用自包含wheel路径。

## 降级语义

所有可识别的运行时异常都输出JSON并返回进程状态0，由上层读取`status`和`safe_action`：

- 依赖缺失或显式禁用：`unverified` / `no_action`
- 输入质量低于0.8：`degraded_input_quality` / `no_action`
- 模型加载失败：`model_load_failed` / `no_action`
- 单次或多次超过截止时间：`timeout` / `no_action`
- 置信度低于阈值：`low_confidence` / `no_action`
- 全部条件通过：`ok` / `action_allowed`

这里的`no_action`是研究基线中的安全非动作结果，不代表任何医疗或设备安全保证。当前仅完成ONNX Runtime CPU基线；PyTorch导出对齐和TensorFlow/TFLite导出对齐仍未完成。
