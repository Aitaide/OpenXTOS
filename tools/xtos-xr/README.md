# XTOS XR applications

当前为应用层与模拟流程软件切片，按 `docs/xr-multimodal-v1.md` 验证。OpenXR 头文件固定 Khronos OpenXR-SDK release-1.1.57；许可及原始文件摘要见 `vendor/openxr/provenance.json`。系统没有自动安装或选择 loader/runtime。

从仓库根目录执行 Python 模拟流程：

    PYTHONPATH=tools/xtos-xr:tools python3 -B -m xtos_xr --demo

该命令在当前 HOME 下创建并清理私有临时 FixtureStore，送入三个明确模拟样本，经过 join、真实 ActionGate fixture nonce 消耗和固定内存 callback 后输出 JSON。所有回执固定 `runtime_mode=simulated`、`safe_action=no_action`、`physical_acceptance=false`；不初始化生产 authority，不运行外部执行器。

构建 native probe：

    make -C tools/xtos-xr all

输出 `.build/xr/xr-probe`。只有可信运营配置才能提供绝对 loader 路径：

    .build/xr/xr-probe --loader /absolute/operator-selected/libopenxr_loader.so.1

没有 loader/runtime 时返回非零 unavailable/no_action。probe 仅创建 instance、读取有界事件并可使用已枚举的 timespec 扩展逐点转换；没有图形会话、渲染或真实 eye action/space 采集。`--simulated` 只标注调用方选择的测试库，不能据此证明库来自真实设备。

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

安装 native `xr-probe`、Python应用源码与既有 ActionGate源码、两个固定入口及 hash manifest，不包含loader/runtime/测试/设备凭据。Python入口固定 `/usr/bin/python3`；目标需独立满足Python标准库和native动态链接闭包。安装副本测试会实际执行模块demo；隔离chroot sentinel另验证生成launcher选用的解释器和argv，不能把sentinel当实际Python执行。重复安装只接受精确相同字节和权限；冲突拒绝而保留调用方文件。该 source-only 安装路径不自动进入 Live ISO。

未关闭的范围：图形绑定/会话、真实眼动action与space、vendor runtime/头显、物理EEG/EMG、同步不确定度校准、跟踪精度、GPU和物理实时延迟。sample中的live_device标签只是未认证metadata，不能授予生产动作权限。
