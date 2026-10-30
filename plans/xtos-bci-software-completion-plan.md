# XTOS BCI 剩余软件功能完成计划

> **For Hermes:** 使用 subagent-driven-development 分配独占路径，逐项 RED→GREEN，冻结后 SPEC→QUALITY 复核和完整维护。禁止设备/实机证据替代。

**Goal:** 补齐 BCI 设备生命周期、版本化数据面、Cyton 八通道流式处理、配置读取和断线重连，并集成可运行构建。

**Architecture:** 内核以引用计数管理设备、打开文件和映射，解绑后禁止新操作并唤醒等待者；版本化帧和只读映射配显式消费控制。用户态新增 C 版采集工具，串口生产者使用共享解析器和固定容量 SPSC，消费者维护八通道滤波状态；连接代次变化和丢样/过载必须重置滤波并标记降级。板卡文本响应有界解析，实际读取 gain 后逐通道换算；未知配置不输出可用样本。

**Tech Stack:** Linux 6.6.0-rt15、C11/pthreads/atomics、现有 SPSC/信号库、PTY、unittest、ASan/UBSan/TSan、QEMU。

## 完成范围

1. 内核 core/device/ring 生命周期与版本化帧：设备注册先初始化，open/close/unbind/blocked read/owner refs，取消全局单例；read 不跨锁复制易覆盖内存。只读 mmap 持有 buffer 引用，原始分配设备/长度一致；定义信息/消费 ABI，旧不完整接口明确拒绝。先写真实 C 或 KUnit/guest reproducer，再修复。
2. 八通道实时引擎：固定内存 SPSC，生产者收包与消费者滤波真正独立；记录 sequence/generation/host receive 与处理时间、nV、quality、队列/协议损失；队列满拒绝新帧，恢复后显式缺口和滤波重置。不把软件排队时间称为设备端到端延迟。
3. C 串口命令与配置：115200 8N1，完整写/超时/停止，v/寄存器读取，gain/通道状态/固件身份有界验证；默认假设与实际读回区分，未知不允许有效输出。重连限制次数/总时长、重新识别与配置、清空旧数据并递增代次；停止请求中断等待。
4. 维护、SDK/系统/Live 安装与文档：新增代码必须编译执行；更新 patch/SBOM/SDK UAPI；测试实际安装路径与断连恢复。冻结后独立审查，完整维护和新内核 QEMU。新 ISO 按磁盘/锁定依赖情况构建新 candidate，保留 candidate-54。

## 可复现验收

- 生命周期：open vs unregister、FD 持有时解绑、阻塞读唤醒、close 与 mmap 生命周期、映射不可写/越界、并发读取/覆盖、ID 重用、两设备并存；精确 errno 和资源归零。
- 实时引擎：所有八通道与独立滤波 oracle、真正双线程、环满和恢复、计数回绕/坏包/重复、generation 重置、关闭排空、有界输出；ASan/UBSan/TSan。
- 串口：PTY 分块响应/收包、错误或缺少 gain/identity 拒绝、短写/超时/拔出、稳定路径重开、身份变化拒绝、SIGINT/TERM 清理、停止命令失败保留。
- 集成：所有 named test 进入维护入口；最终报告绑定源码/配置/二进制和候选哈希。声明测试执行次数与独立用例分别统计。

## 外部验收门

物理 EEG/真实 Cyton、实机持续延迟、M0/M1 原生调度策略对比、Linux 原生 sched_class go/no-go、医疗或真实反馈闭环继续保留为外部门。不会为了完结本地软件把这些门改成通过，也不在无授权时操作 H3C 电源、BIOS 或硬盘。
