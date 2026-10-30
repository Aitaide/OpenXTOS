# XTOS调度器设计文档

> **历史设计，未实现。** 本文是早期设想，不是当前实现证明。当前代码没有 XTOS `sched_class`、EEG/GPU 调度或下文目标延迟。请以 [`scheduler_integration_audit.md`](scheduler_integration_audit.md) 为当前实现状态依据。


## 概述

XTOS调度器是针对脑机接口(BCI)和AI推理优化的实时任务调度系统，在Linux PREEMPT_RT基础上增加EEG感知调度和GPU抢占支持。

---

## 设计目标

### 性能指标
- 最大调度延迟: <10μs (硬实时)
- 平均调度延迟: <5μs
- 调度抖动: <1μs
- EEG信号处理延迟: <15ms (端到端)
- AI推理响应时间: <20ms (CPU) / <5ms (GPU)

### 核心特性
1. **多级优先级队列** (15级)
2. **EEG事件感知调度** (P300/SSVEP触发)
3. **预测性任务调度** (基于历史延迟)
4. **GPU抢占式调度** (1ms粒度)
5. **CPU亲和性绑定** (硬绑定到核心)

---

## 架构设计

### 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                    用户空间应用                               │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │ BCI App  │  │ AR渲染   │  │ AI推理   │  │ 医疗设备 │   │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘   │
└───────┼─────────────┼─────────────┼─────────────┼──────────┘
        │             │             │             │
        └─────────────┴─────────────┴─────────────┘
                      │
        ┌─────────────▼────────────────────────────────────────┐
        │           libxtos-rt (实时调度API)                   │
        │  set_priority()  bind_cpu()  set_deadline()         │
        └─────────────┬────────────────────────────────────────┘
                      │ syscall
        ┌─────────────▼────────────────────────────────────────┐
        │              XTOS内核调度器                           │
        │  ┌────────────────────────────────────────────────┐  │
        │  │  XTOS核心调度器 (xtos_sched.c)                 │  │
        │  │  - 15级优先级队列                              │  │
        │  │  - O(1)调度算法                                │  │
        │  │  - CPU亲和性管理                               │  │
        │  └────────────┬───────────────────────────────────┘  │
        │               │                                       │
        │  ┌────────────▼───────────────────────────────────┐  │
        │  │  EEG感知调度器 (xtos_eeg_aware.c)             │  │
        │  │  - P300事件检测与优先级提升                   │  │
        │  │  - 预测性调度 (LSTM/统计模型)                 │  │
        │  │  - 动态时间片调整                             │  │
        │  └────────────┬───────────────────────────────────┘  │
        │               │                                       │
        │  ┌────────────▼───────────────────────────────────┐  │
        │  │  GPU调度器 (xtos_gpu_sched.c)                 │  │
        │  │  - GPU任务队列管理                            │  │
        │  │  - 抢占式切换 (NVIDIA MPS/AMD ROCm)          │  │
        │  │  - 延迟预测与优化                             │  │
        │  └────────────────────────────────────────────────┘  │
        └───────────────────────────────────────────────────────┘
                        │
        ┌───────────────▼───────────────────────────────────────┐
        │         硬件层 (CPU + GPU + EEG设备)                  │
        └───────────────────────────────────────────────────────┘
```

---

## 核心组件设计

### 1. XTOS核心调度器 (xtos_sched.c)

#### 优先级分级

```c
/* 实时优先级 (0-9): 硬实时任务 */
#define XTOS_PRIO_CRITICAL    0  // 紧急中断处理
#define XTOS_PRIO_EEG_SIGNAL  1  // EEG信号处理
#define XTOS_PRIO_BCI_EVENT   2  // BCI事件响应
#define XTOS_PRIO_AR_RENDER   3  // AR/VR渲染
#define XTOS_PRIO_AI_URGENT   4  // 紧急AI推理
#define XTOS_PRIO_DEVICE_IO   5  // 医疗设备I/O
#define XTOS_PRIO_RT_HIGH     6  // 高优先级实时
#define XTOS_PRIO_RT_MED      7  // 中优先级实时
#define XTOS_PRIO_RT_LOW      8  // 低优先级实时
#define XTOS_PRIO_RT_MIN      9  // 最低实时优先级

/* 常规优先级 (10-14): 软实时和批处理 */
#define XTOS_PRIO_NORMAL      10 // 普通用户任务
#define XTOS_PRIO_BATCH_HIGH  11 // 高优先级批处理
#define XTOS_PRIO_BATCH_MED   12 // 中优先级批处理
#define XTOS_PRIO_BATCH_LOW   13 // 低优先级批处理
#define XTOS_PRIO_IDLE        14 // 空闲任务
```

#### 数据结构

```c
/* XTOS任务控制块扩展 */
struct xtos_task {
    struct task_struct *task;     // Linux task_struct指针
    int xtos_priority;            // XTOS优先级 (0-14)
    unsigned long deadline_ns;    // 截止时间 (纳秒)
    int cpu_affinity;             // CPU绑定 (-1=不绑定)
    
    /* EEG感知调度字段 */
    bool eeg_sensitive;           // 是否对EEG事件敏感
    unsigned long last_eeg_event; // 上次EEG事件时间
    int eeg_boost_level;          // EEG触发的临时优先级提升
    
    /* 统计信息 */
    unsigned long total_runtime;  // 总运行时间
    unsigned long wait_time;      // 等待时间
    unsigned int preempt_count;   // 被抢占次数
    
    struct list_head queue_node;  // 队列链表节点
};

/* XTOS运行队列 */
struct xtos_rq {
    spinlock_t lock;              // 自旋锁
    
    /* 15级优先级队列 */
    struct list_head queue[15];
    unsigned long queue_bitmap;   // 位图快速查找
    
    /* 当前运行任务 */
    struct xtos_task *curr;
    
    /* 统计信息 */
    unsigned long total_switches; // 上下文切换次数
    unsigned long avg_latency_ns; // 平均调度延迟
};
```

#### 核心算法: O(1)调度

```c
/* 选择下一个要运行的任务 - O(1)复杂度 */
static struct xtos_task *pick_next_task_xtos(struct xtos_rq *rq)
{
    int prio;
    struct xtos_task *next;
    
    /* 使用位图快速找到最高优先级非空队列 */
    prio = __ffs(rq->queue_bitmap);  // Find First Set
    if (prio < 0)
        return NULL;  // 所有队列为空
    
    /* 从队列头取出任务 */
    next = list_first_entry(&rq->queue[prio], 
                           struct xtos_task, 
                           queue_node);
    list_del(&next->queue_node);
    
    /* 更新位图 */
    if (list_empty(&rq->queue[prio]))
        rq->queue_bitmap &= ~(1UL << prio);
    
    return next;
}

/* 将任务加入队列 */
static void enqueue_task_xtos(struct xtos_rq *rq, struct xtos_task *task)
{
    int prio = task->xtos_priority;
    
    /* 加入对应优先级队列的尾部 */
    list_add_tail(&task->queue_node, &rq->queue[prio]);
    
    /* 更新位图 */
    rq->queue_bitmap |= (1UL << prio);
}
```

---

### 2. EEG感知调度器 (xtos_eeg_aware.c)

#### P300事件触发机制

```c
/* P300事件处理 */
void xtos_handle_p300_event(enum p300_direction dir)
{
    struct xtos_task *task;
    unsigned long flags;
    
    spin_lock_irqsave(&xtos_rq.lock, flags);
    
    /* 遍历所有EEG敏感任务 */
    list_for_each_entry(task, &eeg_sensitive_tasks, eeg_node) {
        if (task->eeg_sensitive) {
            /* 临时提升优先级 */
            task->eeg_boost_level = 3;  // 提升3级
            task->last_eeg_event = ktime_get_ns();
            
            /* 重新入队到更高优先级 */
            dequeue_task_xtos(&xtos_rq, task);
            task->xtos_priority = max(0, task->xtos_priority - 3);
            enqueue_task_xtos(&xtos_rq, task);
        }
    }
    
    spin_unlock_irqrestore(&xtos_rq.lock, flags);
    
    /* 触发调度器 */
    schedule();
}
```

#### 预测性调度

```c
/* 基于历史数据预测任务执行时间 */
static unsigned long predict_runtime(struct xtos_task *task)
{
    /* 指数加权移动平均 (EWMA) */
    unsigned long alpha = 80;  // 80%权重给历史
    unsigned long predicted;
    
    predicted = (alpha * task->avg_runtime + 
                (100 - alpha) * task->last_runtime) / 100;
    
    return predicted;
}

/* 动态调整时间片 */
static void adjust_timeslice(struct xtos_task *task)
{
    unsigned long predicted = predict_runtime(task);
    
    if (predicted < 1000000) {  // <1ms
        task->timeslice = 2000000;  // 给2ms
    } else if (predicted < 5000000) {  // <5ms
        task->timeslice = 10000000;  // 给10ms
    } else {
        task->timeslice = 20000000;  // 给20ms
    }
}
```

---

### 3. GPU调度器 (xtos_gpu_sched.c)

#### GPU任务队列

```c
struct xtos_gpu_task {
    int priority;                 // 继承CPU任务优先级
    unsigned long deadline_ns;    // 截止时间
    
    /* GPU资源 */
    void *model;                  // AI模型指针
    void *input_buffer;           // 输入数据
    void *output_buffer;          // 输出数据
    size_t input_size;
    size_t output_size;
    
    /* 回调函数 */
    void (*callback)(void *result, void *userdata);
    void *userdata;
    
    /* 状态 */
    enum {
        GPU_TASK_PENDING,
        GPU_TASK_RUNNING,
        GPU_TASK_COMPLETED
    } state;
    
    struct list_head node;
};

/* GPU任务队列 */
static struct list_head gpu_task_queue;
static spinlock_t gpu_queue_lock;
static struct xtos_gpu_task *current_gpu_task;
```

#### 抢占式GPU调度

```c
/* 提交GPU任务 */
int xtos_gpu_submit_task(struct xtos_gpu_task *task)
{
    unsigned long flags;
    
    spin_lock_irqsave(&gpu_queue_lock, flags);
    
    /* 检查当前GPU任务优先级 */
    if (current_gpu_task && 
        task->priority < current_gpu_task->priority) {
        
        /* 抢占当前任务 */
        if (gpu_preempt_supported()) {
            xtos_gpu_preempt(current_gpu_task);
            
            /* 将被抢占任务重新入队 */
            list_add(&current_gpu_task->node, &gpu_task_queue);
            
            /* 立即运行高优先级任务 */
            current_gpu_task = task;
            xtos_gpu_launch(task);
        } else {
            /* 不支持抢占，加入队列等待 */
            list_add_tail(&task->node, &gpu_task_queue);
        }
    } else {
        /* 加入队列 */
        list_add_tail(&task->node, &gpu_task_queue);
        
        /* 如果GPU空闲，立即启动 */
        if (!current_gpu_task) {
            current_gpu_task = task;
            xtos_gpu_launch(task);
        }
    }
    
    spin_unlock_irqrestore(&gpu_queue_lock, flags);
    
    return 0;
}

/* NVIDIA GPU抢占 (使用MPS) */
static void xtos_gpu_preempt_nvidia(struct xtos_gpu_task *task)
{
    /* 使用CUDA MPS的抢占API */
    cudaStreamQuery(task->stream);
    cudaStreamSynchronize(task->stream);
    
    /* 保存GPU上下文 */
    save_gpu_context(task);
}

/* AMD GPU抢占 (使用ROCm) */
static void xtos_gpu_preempt_amd(struct xtos_gpu_task *task)
{
    /* 使用ROCm的抢占机制 */
    hipStreamQuery(task->stream);
    hipStreamSynchronize(task->stream);
    
    save_gpu_context(task);
}
```

---

## 系统调用接口

### 用户空间API

```c
/* libxtos-rt/priority.h */

/* 设置XTOS优先级 */
int xtos_set_priority(pid_t pid, int priority);

/* 获取XTOS优先级 */
int xtos_get_priority(pid_t pid);

/* 设置CPU亲和性 (硬绑定) */
int xtos_set_cpu_affinity(pid_t pid, int cpu);

/* 设置截止时间 (纳秒) */
int xtos_set_deadline(pid_t pid, unsigned long deadline_ns);

/* 标记任务为EEG敏感 */
int xtos_set_eeg_sensitive(pid_t pid, bool sensitive);

/* 提交GPU任务 */
int xtos_gpu_submit(struct xtos_gpu_task *task);
```

### 示例代码

```c
#include <xtos/rt.h>

int main() {
    /* 设置为高优先级实时任务 */
    xtos_set_priority(getpid(), XTOS_PRIO_BCI_EVENT);
    
    /* 绑定到CPU 2 (避免与其他任务竞争) */
    xtos_set_cpu_affinity(getpid(), 2);
    
    /* 标记为EEG敏感 (P300事件会提升优先级) */
    xtos_set_eeg_sensitive(getpid(), true);
    
    /* 主循环 */
    while (1) {
        process_bci_data();
    }
    
    return 0;
}
```

---

## 性能优化策略

### 1. CPU隔离

```bash
# 内核启动参数
isolcpus=2,3,4,5  # 隔离CPU 2-5用于实时任务
nohz_full=2,3,4,5 # 禁用这些CPU的tick中断
rcu_nocbs=2,3,4,5 # RCU回调不在这些CPU执行
```

### 2. 中断亲和性

```c
/* 将EEG设备中断绑定到专用CPU */
echo 1 > /proc/irq/IRQ_NUM/smp_affinity
```

### 3. 内存锁定

```c
/* 锁定内存防止页面交换 */
mlockall(MCL_CURRENT | MCL_FUTURE);
```

### 4. 高精度时钟

```c
/* 使用CLOCK_MONOTONIC_RAW (不受NTP影响) */
struct timespec ts;
clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
```

---

## 测试与验证

### 单元测试

```c
/* tests/scheduler_test.c */

/* 测试优先级队列 */
void test_priority_queue() {
    struct xtos_rq rq;
    struct xtos_task tasks[10];
    
    /* 初始化 */
    init_xtos_rq(&rq);
    
    /* 插入不同优先级的任务 */
    for (int i = 0; i < 10; i++) {
        tasks[i].xtos_priority = i;
        enqueue_task_xtos(&rq, &tasks[i]);
    }
    
    /* 验证调度顺序 */
    for (int i = 0; i < 10; i++) {
        struct xtos_task *next = pick_next_task_xtos(&rq);
        assert(next->xtos_priority == i);
    }
}

/* 测试EEG事件优先级提升 */
void test_eeg_boost() {
    struct xtos_task task;
    task.xtos_priority = 5;
    task.eeg_sensitive = true;
    
    /* 触发P300事件 */
    xtos_handle_p300_event(P300_LEFT);
    
    /* 验证优先级提升 */
    assert(task.xtos_priority == 2);  // 提升了3级
}
```

### 性能基准测试

```bash
# cyclictest - 测试调度延迟
cyclictest -p 99 -m -n -i 200 -l 100000

# 预期结果:
# Min: 2us
# Avg: 5us
# Max: 10us
```

---

## 实现计划

### 第1阶段 (M2: 第2个月)
- [ ] 实现XTOS核心调度器 (xtos_sched.c)
- [ ] 实现优先级队列和O(1)算法
- [ ] 系统调用接口
- [ ] 单元测试

### 第2阶段 (M3: 第3个月)
- [ ] 实现EEG感知调度器 (xtos_eeg_aware.c)
- [ ] P300事件处理
- [ ] 预测性调度算法
- [ ] 集成测试

### 第3阶段 (M4-M5: 第4-5个月)
- [ ] 实现GPU调度器 (xtos_gpu_sched.c)
- [ ] NVIDIA MPS集成
- [ ] AMD ROCm集成
- [ ] 端到端性能测试

---

## 参考资料

1. Linux内核文档: kernel/sched/rt.c
2. PREEMPT_RT官方文档
3. "Real-Time Systems Design and Analysis" - Phillip A. Laplante
4. CUDA MPS文档
5. AMD ROCm文档

---

**最后更新**: 自动生成  
**维护者**: XTOS开发团队
