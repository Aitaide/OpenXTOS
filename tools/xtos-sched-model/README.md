# XTOS 调度参考工具

使用说明、语义、资源上限和阶段边界见 [模型契约](../../docs/scheduler-workloads/m1-model-v1.md)。多 CPU 固定分区入口为 `partitioned.py`，见 [分区重放契约](../../docs/scheduler-workloads/partitioned-model-v1.md)；它组合独立 CPU 时间线，不模拟物理 SMP、迁移或共享锁。

```sh
python3 tools/xtos-sched-model/replay.py \
  --input docs/scheduler-workloads/m1-model-input-v1.json \
  --output .build/scheduler-m1/new-report.json \
  --policies edf,fifo,rr,reservation_edf --quantum-ns 1000000

python3 -m unittest discover -s tests -p 'test_xtos_sched_model*.py' -v
```

也可用 `--trace PATH/trace.jsonl` 导入已验证的 M0 单 CPU pinned trace 的逐 job CPU 需求。输出固定为 `model_only`，必须使用新路径；成功生成报告不代表任务准入或阶段验收。

`reservation_edf` 是任务共享的固定周期预算模型，不是 Linux CBS/SCHED_DEADLINE。EDF/FIFO/RR 是零开销参考语义，不是内核行为或性能测量。
