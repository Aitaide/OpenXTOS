# XTOS BCI驱动框架设计文档

## 概述

XTOS BCI驱动框架提供统一的EEG/脑电设备抽象层，支持多种商用和开源EEG硬件，通过插件化架构实现设备无关的应用开发。

---

## 设计目标

### 功能目标
- **通用性**: 支持20+主流EEG设备
- **低延迟**: 采集到内核缓冲区<2ms
- **高吞吐**: 支持8kHz采样率
- **热插拔**: 设备动态发现和移除
- **零拷贝**: DMA直接到用户空间

### 支持设备清单

| 厂商 | 型号 | 通道数 | 接口 | 优先级 |
|------|------|--------|------|--------|
| OpenBCI | Cyton | 8 | 串口/蓝牙 | P0 (首批) |
| OpenBCI | Ganglion | 4 | 蓝牙 | P0 |
| Emotiv | EPOC+ | 14 | USB/蓝牙 | P1 |
| Emotiv | Insight | 5 | 蓝牙 | P1 |
| NeuroSky | MindWave | 1 | 蓝牙 | P1 |
| Muse | Muse S | 4 | 蓝牙 | P1 |
| g.tec | g.USBamp | 16 | USB | P2 |
| ANT Neuro | eego | 64 | 网络 | P2 |
| BrainProducts | actiCHamp | 32 | USB | P2 |

---

## 架构设计

### 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                      用户空间应用                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │  BCI应用     │  │  校准工具    │  │  信号可视化  │      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
└─────────┼──────────────────┼──────────────────┼─────────────┘
          │                  │                  │
          └──────────────────┴──────────────────┘
                             │
          ┌──────────────────▼──────────────────────┐
          │   libxtos-bci (用户空间库)              │
          │   - 设备枚举和打开                      │
          │   - 数据流读取                          │
          │   - 参数配置                            │
          │   - mmap零拷贝                          │
          └──────────────────┬──────────────────────┘
                             │ ioctl/mmap
          ┌──────────────────▼──────────────────────┐
          │       XTOS BCI核心驱动                   │
          │  ┌────────────────────────────────────┐ │
          │  │  xtos_bci_core.c                   │ │
          │  │  - 设备注册/枚举                   │ │
          │  │  - 环形缓冲区管理                  │ │
          │  │  - 字符设备接口 (/dev/xtosX)       │ │
          │  └───────────┬────────────────────────┘ │
          │              │                           │
          │  ┌───────────▼────────────────────────┐ │
          │  │  协议层 (protocols/)               │ │
          │  │  ┌──────────┐  ┌──────────┐       │ │
          │  │  │ serial   │  │ bluetooth│       │ │
          │  │  └──────────┘  └──────────┘       │ │
          │  │  ┌──────────┐  ┌──────────┐       │ │
          │  │  │   USB    │  │ network  │       │ │
          │  │  └──────────┘  └──────────┘       │ │
          │  └───────────┬────────────────────────┘ │
          │              │                           │
          │  ┌───────────▼────────────────────────┐ │
          │  │  设备驱动层 (devices/)             │ │
          │  │  ┌──────────────┐ ┌──────────────┐│ │
          │  │  │openbci_cyton │ │emotiv_epoc   ││ │
          │  │  └──────────────┘ └──────────────┘│ │
          │  │  ┌──────────────┐ ┌──────────────┐│ │
          │  │  │   muse_s     │ │neurosky_mw   ││ │
          │  │  └──────────────┘ └──────────────┘│ │
          │  └────────────────────────────────────┘ │
          └─────────────────────────────────────────┘
                             │
          ┌──────────────────▼──────────────────────┐
          │      硬件层 (EEG设备)                    │
          │  串口/USB/蓝牙/网络                      │
          └─────────────────────────────────────────┘
```

---

## 核心数据结构

### 设备描述符

```c
/* include/linux/xtos_bci.h */

struct xtos_bci_device_ops;

/* BCI设备结构 */
struct xtos_bci_dev {
    /* 设备标识 */
    char name[32];              // 设备名称 (如 "OpenBCI Cyton")
    int dev_id;                 // 设备ID (唯一)
    
    /* 硬件参数 */
    int num_channels;           // 通道数
    int sampling_rate;          // 采样率 (Hz)
    int resolution_bits;        // ADC分辨率 (位)
    float input_range_mv;       // 输入范围 (毫伏)
    
    /* 连接信息 */
    enum xtos_bci_interface {
        XTOS_BCI_SERIAL,
        XTOS_BCI_USB,
        XTOS_BCI_BLUETOOTH,
        XTOS_BCI_NETWORK
    } interface;
    
    void *hw_private;           // 硬件私有数据
    
    /* 操作函数 */
    const struct xtos_bci_device_ops *ops;
    
    /* 环形缓冲区 */
    struct xtos_bci_ringbuf *ringbuf;
    
    /* 字符设备 */
    struct cdev cdev;
    struct device *device;
    int major, minor;
    
    /* 统计信息 */
    unsigned long samples_read;
    unsigned long samples_dropped;
    unsigned long errors;
    
    /* 状态 */
    bool opened;
    bool streaming;
    
    struct list_head list;      // 全局设备链表
};

/* 设备操作函数表 */
struct xtos_bci_device_ops {
    /* 初始化和清理 */
    int (*init)(struct xtos_bci_dev *dev);
    void (*cleanup)(struct xtos_bci_dev *dev);
    
    /* 数据采集 */
    int (*start_stream)(struct xtos_bci_dev *dev);
    int (*stop_stream)(struct xtos_bci_dev *dev);
    int (*read_samples)(struct xtos_bci_dev *dev, 
                        float *buffer, 
                        size_t num_samples);
    
    /* 配置 */
    int (*set_sampling_rate)(struct xtos_bci_dev *dev, int rate);
    int (*set_channel_enable)(struct xtos_bci_dev *dev, 
                              int channel, 
                              bool enable);
    int (*set_gain)(struct xtos_bci_dev *dev, 
                    int channel, 
                    int gain);
    
    /* 校准 */
    int (*calibrate)(struct xtos_bci_dev *dev);
    int (*impedance_check)(struct xtos_bci_dev *dev, 
                          float *impedances);
};
```

### 环形缓冲区

```c
/* 零拷贝环形缓冲区 */
struct xtos_bci_ringbuf {
    void *buffer;               // DMA缓冲区
    size_t size;                // 缓冲区大小 (字节)
    
    unsigned long write_pos;    // 写指针
    unsigned long read_pos;     // 读指针
    
    spinlock_t lock;            // 自旋锁
    wait_queue_head_t wait;     // 等待队列
    
    /* 性能统计 */
    unsigned long overruns;     // 溢出次数
    unsigned long max_fill;     // 最大填充量
};

/* 分配环形缓冲区 */
struct xtos_bci_ringbuf *xtos_bci_ringbuf_alloc(size_t size)
{
    struct xtos_bci_ringbuf *rb;
    
    rb = kzalloc(sizeof(*rb), GFP_KERNEL);
    if (!rb)
        return NULL;
    
    /* 分配连续物理内存 (用于DMA) */
    rb->buffer = dma_alloc_coherent(NULL, size, 
                                   &rb->dma_handle, 
                                   GFP_KERNEL);
    if (!rb->buffer) {
        kfree(rb);
        return NULL;
    }
    
    rb->size = size;
    rb->write_pos = 0;
    rb->read_pos = 0;
    
    spin_lock_init(&rb->lock);
    init_waitqueue_head(&rb->wait);
    
    return rb;
}

/* 写入数据 */
int xtos_bci_ringbuf_write(struct xtos_bci_ringbuf *rb, 
                          const void *data, 
                          size_t len)
{
    unsigned long flags;
    size_t avail;
    
    spin_lock_irqsave(&rb->lock, flags);
    
    /* 计算可用空间 */
    avail = rb->size - (rb->write_pos - rb->read_pos);
    
    if (len > avail) {
        /* 缓冲区满，丢弃最旧数据 */
        rb->read_pos += (len - avail);
        rb->overruns++;
    }
    
    /* 循环写入 */
    size_t pos = rb->write_pos % rb->size;
    size_t to_end = rb->size - pos;
    
    if (len <= to_end) {
        memcpy(rb->buffer + pos, data, len);
    } else {
        memcpy(rb->buffer + pos, data, to_end);
        memcpy(rb->buffer, data + to_end, len - to_end);
    }
    
    rb->write_pos += len;
    
    spin_unlock_irqrestore(&rb->lock, flags);
    
    /* 唤醒等待读取的进程 */
    wake_up_interruptible(&rb->wait);
    
    return len;
}
```

---

## 设备注册机制

### 注册宏

```c
/* 设备自动注册宏 */
#define XTOS_BCI_DEVICE_REGISTER(name, ops_ptr) \
    static int __init name##_init(void) { \
        return xtos_bci_register_device(&name##_dev, ops_ptr); \
    } \
    static void __exit name##_exit(void) { \
        xtos_bci_unregister_device(&name##_dev); \
    } \
    module_init(name##_init); \
    module_exit(name##_exit);

/* 使用示例 */
static struct xtos_bci_device_ops openbci_ops = {
    .init = openbci_init,
    .cleanup = openbci_cleanup,
    .start_stream = openbci_start_stream,
    .stop_stream = openbci_stop_stream,
    .read_samples = openbci_read_samples,
    .set_sampling_rate = openbci_set_sampling_rate,
};

XTOS_BCI_DEVICE_REGISTER(openbci_cyton, &openbci_ops);
```

---

## 具体设备驱动示例

### OpenBCI Cyton驱动

```c
/* drivers/xtos/bci/devices/openbci_cyton.c */

#include <linux/serial.h>
#include <linux/xtos_bci.h>

/* OpenBCI私有数据 */
struct openbci_priv {
    struct serial_struct serial;
    int fd;
    
    /* 数据包解析 */
    unsigned char packet_buf[33];  // OpenBCI数据包33字节
    int packet_pos;
    
    /* 工作队列 (用于异步读取) */
    struct work_struct read_work;
};

/* OpenBCI数据包格式
 * Byte 0: 起始字节 (0xA0)
 * Byte 1-24: 8通道 × 3字节/通道 (24位ADC)
 * Byte 25-30: 辅助数据
 * Byte 31-32: 结束字节 (0xC0, 0xC1-0xCF循环)
 */

/* 初始化设备 */
static int openbci_init(struct xtos_bci_dev *dev)
{
    struct openbci_priv *priv;
    int ret;
    
    priv = kzalloc(sizeof(*priv), GFP_KERNEL);
    if (!priv)
        return -ENOMEM;
    
    dev->hw_private = priv;
    
    /* 打开串口 */
    priv->fd = serial_open(dev->serial_port, B115200);
    if (priv->fd < 0) {
        kfree(priv);
        return priv->fd;
    }
    
    /* 发送复位命令 */
    serial_write(priv->fd, "v", 1);  // 请求版本
    msleep(100);
    
    /* 分配环形缓冲区 (1秒数据) */
    dev->ringbuf = xtos_bci_ringbuf_alloc(
        dev->num_channels * dev->sampling_rate * sizeof(float)
    );
    
    /* 初始化工作队列 */
    INIT_WORK(&priv->read_work, openbci_read_worker);
    
    return 0;
}

/* 启动数据流 */
static int openbci_start_stream(struct xtos_bci_dev *dev)
{
    struct openbci_priv *priv = dev->hw_private;
    
    /* 发送开始命令 */
    serial_write(priv->fd, "b", 1);  // 'b' = begin streaming
    
    /* 启动读取工作队列 */
    schedule_work(&priv->read_work);
    
    dev->streaming = true;
    return 0;
}

/* 读取工作函数 */
static void openbci_read_worker(struct work_struct *work)
{
    struct openbci_priv *priv = container_of(work, 
                                             struct openbci_priv, 
                                             read_work);
    struct xtos_bci_dev *dev = /* ... */;
    unsigned char byte;
    
    while (dev->streaming) {
        /* 读取一个字节 */
        if (serial_read(priv->fd, &byte, 1) != 1)
            continue;
        
        /* 解析数据包 */
        if (priv->packet_pos == 0 && byte == 0xA0) {
            /* 起始字节 */
            priv->packet_buf[priv->packet_pos++] = byte;
        } else if (priv->packet_pos > 0 && priv->packet_pos < 33) {
            priv->packet_buf[priv->packet_pos++] = byte;
            
            /* 数据包完整 */
            if (priv->packet_pos == 33) {
                openbci_process_packet(dev, priv->packet_buf);
                priv->packet_pos = 0;
            }
        }
    }
}

/* 处理数据包 */
static void openbci_process_packet(struct xtos_bci_dev *dev, 
                                   unsigned char *packet)
{
    float samples[8];
    int i;
    
    /* 转换24位ADC值为浮点数 (微伏) */
    for (i = 0; i < 8; i++) {
        int32_t raw = (packet[1 + i*3] << 16) |
                      (packet[2 + i*3] << 8) |
                      packet[3 + i*3];
        
        /* 符号扩展 */
        if (raw & 0x800000)
            raw |= 0xFF000000;
        
        /* 转换为微伏 (OpenBCI增益=24, Vref=4.5V) */
        samples[i] = raw * (4.5 / 24.0 / (float)(1 << 23)) * 1e6;
    }
    
    /* 写入环形缓冲区 */
    xtos_bci_ringbuf_write(dev->ringbuf, samples, sizeof(samples));
    
    dev->samples_read += 8;
}

/* 停止数据流 */
static int openbci_stop_stream(struct xtos_bci_dev *dev)
{
    struct openbci_priv *priv = dev->hw_private;
    
    dev->streaming = false;
    
    /* 等待工作队列完成 */
    cancel_work_sync(&priv->read_work);
    
    /* 发送停止命令 */
    serial_write(priv->fd, "s", 1);  // 's' = stop streaming
    
    return 0;
}
```

---

## 字符设备接口

### 文件操作

```c
/* drivers/xtos/bci/xtos_bci_core.c */

static const struct file_operations xtos_bci_fops = {
    .owner = THIS_MODULE,
    .open = xtos_bci_open,
    .release = xtos_bci_release,
    .read = xtos_bci_read,
    .poll = xtos_bci_poll,
    .mmap = xtos_bci_mmap,
    .unlocked_ioctl = xtos_bci_ioctl,
};

/* 打开设备 */
static int xtos_bci_open(struct inode *inode, struct file *filp)
{
    struct xtos_bci_dev *dev;
    int ret;
    
    dev = container_of(inode->i_cdev, struct xtos_bci_dev, cdev);
    
    if (dev->opened)
        return -EBUSY;  // 只允许一个进程打开
    
    /* 初始化设备 */
    ret = dev->ops->init(dev);
    if (ret)
        return ret;
    
    filp->private_data = dev;
    dev->opened = true;
    
    return 0;
}

/* 读取数据 */
static ssize_t xtos_bci_read(struct file *filp, 
                            char __user *buf, 
                            size_t count, 
                            loff_t *ppos)
{
    struct xtos_bci_dev *dev = filp->private_data;
    struct xtos_bci_ringbuf *rb = dev->ringbuf;
    size_t avail, to_copy;
    int ret;
    
    /* 等待数据可用 */
    ret = wait_event_interruptible(rb->wait, 
                                   rb->write_pos > rb->read_pos);
    if (ret)
        return ret;
    
    /* 计算可读取量 */
    avail = rb->write_pos - rb->read_pos;
    to_copy = min(count, avail);
    
    /* 拷贝到用户空间 */
    size_t pos = rb->read_pos % rb->size;
    size_t to_end = rb->size - pos;
    
    if (to_copy <= to_end) {
        if (copy_to_user(buf, rb->buffer + pos, to_copy))
            return -EFAULT;
    } else {
        if (copy_to_user(buf, rb->buffer + pos, to_end))
            return -EFAULT;
        if (copy_to_user(buf + to_end, rb->buffer, to_copy - to_end))
            return -EFAULT;
    }
    
    rb->read_pos += to_copy;
    
    return to_copy;
}

/* mmap零拷贝 */
static int xtos_bci_mmap(struct file *filp, struct vm_area_struct *vma)
{
    struct xtos_bci_dev *dev = filp->private_data;
    struct xtos_bci_ringbuf *rb = dev->ringbuf;
    unsigned long size = vma->vm_end - vma->vm_start;
    
    if (size > rb->size)
        return -EINVAL;
    
    /* 映射DMA缓冲区到用户空间 */
    return dma_mmap_coherent(dev->device, vma, 
                            rb->buffer, 
                            rb->dma_handle, 
                            size);
}
```

### ioctl命令

```c
/* include/uapi/linux/xtos_bci.h */

#define XTOS_BCI_IOC_MAGIC 'X'

/* 设备信息 */
#define XTOS_BCI_GET_INFO       _IOR(XTOS_BCI_IOC_MAGIC, 1, struct xtos_bci_info)

/* 数据流控制 */
#define XTOS_BCI_START_STREAM   _IO(XTOS_BCI_IOC_MAGIC, 2)
#define XTOS_BCI_STOP_STREAM    _IO(XTOS_BCI_IOC_MAGIC, 3)

/* 参数配置 */
#define XTOS_BCI_SET_RATE       _IOW(XTOS_BCI_IOC_MAGIC, 4, int)
#define XTOS_BCI_SET_CHANNELS   _IOW(XTOS_BCI_IOC_MAGIC, 5, uint32_t)

/* 校准 */
#define XTOS_BCI_CALIBRATE      _IO(XTOS_BCI_IOC_MAGIC, 6)
#define XTOS_BCI_CHECK_IMP      _IOR(XTOS_BCI_IOC_MAGIC, 7, float[32])

/* ioctl处理 */
static long xtos_bci_ioctl(struct file *filp, 
                          unsigned int cmd, 
                          unsigned long arg)
{
    struct xtos_bci_dev *dev = filp->private_data;
    
    switch (cmd) {
    case XTOS_BCI_START_STREAM:
        return dev->ops->start_stream(dev);
        
    case XTOS_BCI_STOP_STREAM:
        return dev->ops->stop_stream(dev);
        
    case XTOS_BCI_SET_RATE: {
        int rate;
        if (copy_from_user(&rate, (void __user *)arg, sizeof(rate)))
            return -EFAULT;
        return dev->ops->set_sampling_rate(dev, rate);
    }
    
    case XTOS_BCI_CALIBRATE:
        return dev->ops->calibrate(dev);
        
    default:
        return -ENOTTY;
    }
}
```

---

## 用户空间库

### libxtos-bci API

```c
/* libxtos-bci/session.h */

typedef struct xtos_bci_session xtos_bci_session_t;

/* 创建会话 */
xtos_bci_session_t *xtos_bci_session_create(const char *device_name);

/* 销毁会话 */
void xtos_bci_session_destroy(xtos_bci_session_t *session);

/* 启动数据流 */
int xtos_bci_start(xtos_bci_session_t *session);

/* 停止数据流 */
int xtos_bci_stop(xtos_bci_session_t *session);

/* 读取样本 (阻塞) */
int xtos_bci_read_samples(xtos_bci_session_t *session, 
                         float *buffer, 
                         size_t num_samples);

/* 零拷贝访问 */
void *xtos_bci_get_mmap_buffer(xtos_bci_session_t *session, 
                              size_t *size);
```

### 使用示例

```c
#include <xtos/bci.h>

int main() {
    /* 创建会话 */
    xtos_bci_session_t *session = xtos_bci_session_create("OpenBCI Cyton");
    if (!session) {
        perror("Failed to create session");
        return 1;
    }
    
    /* 配置采样率 */
    xtos_bci_set_sampling_rate(session, 250);
    
    /* 启动数据流 */
    xtos_bci_start(session);
    
    /* 读取数据 */
    float samples[8];
    while (1) {
        int ret = xtos_bci_read_samples(session, samples, 8);
        if (ret > 0) {
            /* 处理数据 */
            process_eeg_data(samples, 8);
        }
    }
    
    /* 清理 */
    xtos_bci_stop(session);
    xtos_bci_session_destroy(session);
    
    return 0;
}
```

---

## 性能优化

### 1. DMA零拷贝

```c
/* 使用mmap避免数据拷贝 */
void *buffer = xtos_bci_get_mmap_buffer(session, &size);

/* 直接访问内核缓冲区 */
float *samples = (float *)buffer;
for (int i = 0; i < num_samples; i++) {
    process_sample(samples[i]);
}
```

### 2. 中断合并

```c
/* 批量处理减少中断频率 */
#define BATCH_SIZE 32  // 每32个样本触发一次中断

static irqreturn_t eeg_interrupt_handler(int irq, void *dev_id)
{
    struct xtos_bci_dev *dev = dev_id;
    
    if (dev->sample_count >= BATCH_SIZE) {
        openbci_process_batch(dev);
        dev->sample_count = 0;
    }
    
    return IRQ_HANDLED;
}
```

### 3. CPU亲和性

```c
/* 将中断绑定到专用CPU */
int cpu = 1;  // 使用CPU 1处理EEG中断
irq_set_affinity_hint(dev->irq, cpumask_of(cpu));
```

---

## 实现计划

### M3 (第3个月): 核心框架
- [ ] xtos_bci_core.c - 设备注册和管理
- [ ] xtos_bci_buffer.c - 环形缓冲区
- [ ] 字符设备接口

### M3 (第3个月): 首批设备驱动
- [ ] OpenBCI Cyton (串口)
- [ ] OpenBCI Ganglion (蓝牙)
- [ ] NeuroSky MindWave (蓝牙)

### M4 (第4个月): 更多设备
- [ ] Emotiv EPOC+ (USB)
- [ ] Muse S (蓝牙)
- [ ] 通用USB HID设备

---

**最后更新**: 自动生成  
**维护者**: XTOS开发团队
