# 生产推理 provider v2 合同

状态：实施中的软件合同。candidate-61 仅验收提交 5cb533e4be467dc7e93bec684aec1367a1bb1784；本合同和后续代码不继承旧镜像 PASS。没有真实 CUDA/AMD GPU 时只完成软件实现、真实 CPU 回归和缺失依赖拒绝，不宣称 GPU 推理或实时性通过。

## 兼容与签名

原八字段 manifest 和 xtos-ai-model-v1 / `XTOS-AI-MODEL-v1\0` 签名域保持 CPU-only。新未签名数据结构增加 `schema=xtos-ai-manifest-v2` 和 execution；它只是结构验证，不授予生产执行权限。生产仍必须取得 ProductionTrustPolicy 签发的 AuthorizedModel。

新签名 envelope 为 xtos-ai-model-v2，域 `XTOS-AI-MODEL-v2\0`；claims 保留原模型字段并增加 execution，min_runtime_version 至少1.1.0。runtime版本升级为1.1.0。签名覆盖 provider、execution 和模型哈希；不能把v1改名为v2、跨签名域重放或把ROCM自动改写成MIGraphX。策略签名、许可证、架构、时效、撤销和防回滚仍使用原有门禁。

## execution 精确对象

必须包含且只包含下列键：

- onnxruntime_version：三个非负整数的规范语义版本，各不超过2147483647。
- python_abi：规范 `cpython-<数字>`，长度不超过64，与实际子解释器cache_tag精确一致。
- device_id：非bool整数；CPU为0，GPU为0..31。
- memory_limit_bytes：CPU为0；GPU为1MiB..64GiB。这只是provider arena上限，不是整个设备内存配额。
- runtime_files：1..32个从onnxruntime包根目录出发的相对文件路径到小写SHA256映射，路径最长256，禁止绝对路径、空段、点段、反斜杠、NUL。至少包含实际 `capi/onnxruntime_pybind11_state*.so`；GPU额外要求对应 `capi/libonnxruntime_providers_cuda.so` 或 `capi/libonnxruntime_providers_migraphx.so`。这些是必须验证的分发文件，不是整个驱动供应链认证。只按已安装路径读取，不能据此import或dlopen任意库。
- cpu_affinity：1..256个升序唯一CPU ID，非bool整数0..4095，必须是子进程实际允许CPU集合的子集；禁止自动扩大。
- scheduler：只接受字符串 SCHED_OTHER。

验证后存入不可变 ExecutionProfile，runtime_files和cpu_affinity用tuple；ModelManifest增加默认None的execution，保留旧构造兼容。

## Provider 与子进程执行

允许 CPUExecutionProvider、CUDAExecutionProvider、MIGraphXExecutionProvider。当前profile拒绝ROCMExecutionProvider，原因provider_deprecated；这是ORT1.23移除旧ROCm EP后的明确兼容限制，不伪造支持。

父进程在spawn前验证profile，子进程重建并验证其形状。先在子进程设置并读回SCHED_OTHER及显式affinity，再导入numpy/onnxruntime和创建线程。不能修改主机IRQ/cgroup、全局环境或CPU隔离策略。v1维持原行为。

v2子进程拒绝继承非空的 `ORT_MIGRAPHX_`、`ORT_CUDA_` 和 `MIGRAPHX_` 配置覆盖环境，防止未签名环境改变provider选项或缓存路径。必须使用已安装onnxruntime包，校验实际ABI、版本与runtime_files的普通非symlink文件和SHA256；任何缺失/不符均失败。这里只证明声明的分发文件一致，不能证明外部GPU驱动可信。

provider选项由受限profile生成，不接受任意字符串、stream指针、allocator地址或外部cache路径：

- CPU：无vendor选项。
- CUDA：device_id、gpu_mem_limit、do_copy_in_default_stream=1、arena_extend_strategy=kSameAsRequested、cudnn_conv_algo_search=DEFAULT。
- MIGraphX：device_id、migraphx_mem_limit、migraphx_fp16_enable=0、migraphx_int8_enable=0、migraphx_exhaustive_tune=0。

GPU在创建session前设置 `session.disable_cpu_ep_fallback=1`，创建后调用disable_fallback，实际get_providers必须精确为单个请求provider；缺少控制接口或读回不符拒绝。smoke和每次infer后重核provider。v2 CPU同样disable_fallback并精确选择。不可用时返回unavailable/no_action，无CPU重试。

原有已校验模型字节、张量元数据、有限float32、零输入smoke、诊断拒绝、deadline/cancel kill+reap保持。ready回执包含实际runtime_version和execution读回；父进程核对后才加载成功。

## 客户端与恢复

推理请求可显式携带expected_provider，只允许上述三个provider；GPU成功响应必须有精确provider pin。旧无pin请求只接受CPU。模型名称、可选model digest和provider都不得冲突；provider pin只能用于infer。服务端在调用worker前拒绝与已授权模型不符的pin；不能通过响应把CPU结果冒充GPU。

每个worker/ticket代次最多两次恢复预约；在现有condition锁下原子计数。初次load不计，失败恢复也消耗预约；排队拒绝、显式shutdown/revocation不触发新恢复。额度耗尽后返回recovery_budget_exhausted/no_action，只有显式load/reload新代次重置。health每模型暴露recovery_limit/used/remaining和末次原因。继续保持一个active和一个queued。

## 验证与包装

新增行为逐增量先真实RED后GREEN，保留源/测试/依赖/日志。v1签名、策略撤销与CPU数值回归不得减少；v2真实签名CPU授权和实际子进程推理、runtime digest/ABI错误、缺GPU、provider漂移、超时取消、恢复额度与reload分别覆盖。fixture backend不冒充GPU。

Live/source-only安装保留全部runtime模块，CPU依赖包和锁文件不变。可选GPU只消费独立且可校验的运营方runtime，不向CPU环境安装vendor包或编造wheel/hash。当前私有CPython3.11；AMD公布cp310/cp312包不能假称兼容cp311。新源码需独立SPEC/QUALITY、完整维护、准确私有提交读回及新的安装产物验收。

资料：https://onnxruntime.ai/docs/execution-providers/ROCm-ExecutionProvider.html （移除与迁移说明）；https://onnxruntime.ai/docs/execution-providers/MIGraphX-ExecutionProvider.html （device/memory/env覆盖）；https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html （显式选项）。
