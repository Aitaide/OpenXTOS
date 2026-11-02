# XTOS XR applications

当前为应用层与模拟流程软件切片，按 `docs/xr-multimodal-v1.md` 验证。OpenXR 头文件固定 Khronos OpenXR-SDK release-1.1.57；许可及原始文件摘要见 `vendor/openxr/provenance.json`。系统没有自动安装或选择 loader/runtime。

从仓库根目录执行 Python 模拟流程：

    PYTHONPATH=tools/xtos-xr:tools python3 -B -m xtos_xr --demo

该命令在当前 HOME 下创建并清理私有临时 FixtureStore，送入三个明确模拟样本，经过 join、真实 ActionGate fixture nonce 消耗和固定内存 callback 后输出 JSON。所有回执固定 `runtime_mode=simulated`、`safe_action=no_action`、`physical_acceptance=false`；不初始化生产 authority，不运行外部执行器。

构建 native probe：

    make -C tools/xtos-xr all

输出 `.build/xr/xr-probe` 和 `.build/xr/xr-eye-probe`。只有可信运营配置才能提供绝对 loader 路径：

    .build/xr/xr-probe --loader /absolute/operator-selected/libopenxr_loader.so.1

没有 loader/runtime 时返回非零 unavailable/no_action。`xr-probe` 仅创建 instance、读取有界事件并使用已枚举的 timespec 扩展逐点转换。

新增 `xr-eye-probe` 通过显式 opt-in 的 `XR_MND_headless`、`XR_EXT_eye_gaze_interaction` 和 timespec 扩展建立真实 API 会话、pose action 与 space。运行前必须明确同意眼动数据访问：

    .build/xr/xr-eye-probe --loader /absolute/operator-selected/libopenxr_loader.so.1 --accept-eye-data --max-samples 1 --duration-ms 1000

扩展、HMD 或眼动能力不满足时拒绝；没有图形渲染。命令最多采样64条、持续5000毫秒，只有取得请求的全部样本且完成清理才报告 complete；部分采样、中断、清理或输出失败均非零。周期休眠和非阻塞 stdout 约束合作式循环与管道背压，不能约束第三方 runtime 内部阻塞或物理实时延迟。输出失败后的 stderr 诊断只做一次非阻塞写入；管道写满可省略诊断，仍非零退出。输出分别保留 query time 和 runtime pose time，后者可能由预测、插值或clamp产生，不能冒称原始采集时间。`--simulated` 只标注调用方选择的测试库，不能据此证明库或设备来源。两种命令均保持 no_action、非物理验收；新增会话尚处独立复核/集成中，见 `docs/xr-headless-live-v2.md`。

聚焦维护：

    python3 -B -m unittest discover -s tests -p test_xr_native.py -v
    XTOS_XR_SANITIZE=1 python3 -B -m unittest discover -s tests -p test_xr_native.py -v
    python3 -B -m unittest discover -s tests -p test_xr_multimodal.py -v
    python3 -B -m unittest discover -s tests -p test_xr_feedback.py -v
    python3 -B -m unittest discover -s tests -p test_xr_runtime.py -v
    python3 -B -m unittest discover -s tests -p test_xr_install.py -v
    python3 -B -m unittest discover -s tests -p test_xr_integration.py -v

Native测试把真实 Khronos ABI 的编译注入库标记为模拟，结果不代表 Khronos 合规或头显验收。每次在 `.build/p6-xr-next/native-tests/` 保留新子目录；可用 `XTOS_XR_TEST_OUTPUT` 指定独立输出父目录，避免覆盖已有证据。

`xtos_xr.runtime_events.RuntimeFence(session).accept(record)` 消费有界 native probe 对象。已知/未知事件、事件上限、代次变化或无效记录会撤销已有多模态候选；它只连接控制面，不把JSON conversion记录当可信样本转换器。compiled fixture端到端测试验证256事件输出实际进入该fence。runtime generation仅在同一probe/adapter进程生命周期内有意义；上层重启应显式调用session.barrier，不能把不同进程的同号generation当同一实例。

安装到已存在、归调用方拥有的 staging 目录：

    python3 -B scripts/install_xr.py --rootfs /absolute/staging/root

安装 native `xr-probe`、`xr-eye-probe`、Python应用与既有 ActionGate源码、固定入口及 hash manifest，不包含loader/runtime/测试/设备凭据。Python入口固定 `/usr/bin/python3`；`live_xr.py` 在新 staging 中实际运行目标Python、两种native命令的缺loader拒绝路径及无动作demo，同时核对精确源清单、payload与launcher。重复安装只接受精确相同字节和权限；冲突保留调用方文件。Live构建已接线，新的精确提交ISO仍须完成完整维护、库存读回、普通用户guest与四模式启动，旧镜像不覆盖新增代码。

未关闭的范围：图形绑定/渲染、vendor runtime/真实头显、物理EEG/EMG、同步不确定度校准、跟踪精度、GPU和物理实时延迟。headless native、CLI及Live助手已各自通过冻结组件审查；修复后依赖的最终组合审查与新镜像验收仍在进行。sample中的live_device标签只是未认证metadata，不能授予生产动作权限。
