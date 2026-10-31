# Optional ONNX provider experiment

状态：已实现源码及注入运行时验证；这是开发工具，不是签名模型授权、生产 worker、Live 镜像或 GPU 硬件验收。

`tools/xtos-ai-provider-experiment/provider_experiment.py` 要求调用方显式指定 `CPUExecutionProvider`、`CUDAExecutionProvider` 或 `ROCMExecutionProvider`。它检查模型字节、张量名称/类型/形状、运行时已注册 provider、精确 provider 选择、零输入 smoke 和实际输入输出，并在所有成功结果中固定返回 `safe_action=no_action`。

运行时没有请求的 provider 时直接失败；不会自动选择 CPU，也不会把 CUDA/ROCm 失败静默回退为 CPU。运行时没有 `disable_fallback()`、provider 切换、张量不匹配、输出错误或导入缺失同样失败。实验器不读取或签发 `AuthorizedModel`，不修改 `tools/xtos-ai-runtime/xtos_ai_runtime/{protocol,trust,worker,client,server}.py` 的 CPU-only 合同。

当前主机的锁定 ONNX Runtime 1.23.2 只报告 `AzureExecutionProvider` 和 `CPUExecutionProvider`；主机是 QEMU 虚拟机，没有 NVIDIA 工具或可证明的 CUDA/ROCm 执行设备。因此 CPU provider 可做软件功能验证，CUDA/ROCm 仅验证不可用时的拒绝路径，不能报告 GPU 推理通过、GPU 性能或实时性。

源码属于 SBOM `excluded` 开发组件，不进入 Live 的七文件签名 CPU runtime allowlist，也不进入 source-only AI runtime 安装包。测试通过共享维护入口执行，但不改变生产 AI provider 或模型信任策略。

该实验入口同步执行，不继承生产 worker 的进程隔离、取消、超时 kill/reap 或恢复机制，不提供执行时限或资源上限保证。调用方必须提供可信的运行时依赖；注入测试 backend 不是真实 provider 或硬件证据。读取的模型快照会重新校验 SHA-256，随后把同一不可变字节对象交给 session；路径变化不能替换本次已验证输入。

从仓库根执行无运行时依赖的回归：

    python3 -B -m unittest discover -s tests -p 'test_ai_provider_experiment.py' -v
    python3 -B -O -m unittest discover -s tests -p 'test_ai_provider_experiment.py' -v
