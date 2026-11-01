/* SPDX-License-Identifier: GPL-2.0 */
#ifndef CYTON_DRIVER_HOST_SHIM_H
#define CYTON_DRIVER_HOST_SHIM_H
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <stdatomic.h>
typedef int64_t s64;
typedef uint64_t u64;
typedef uint32_t __u32;
typedef uint8_t __u8;
typedef unsigned long long __u64;
#define __aligned_u64 __u64 __attribute__((aligned(8)))
#include "../../kernel/linux/include/uapi/linux/xtos_bci_config.h"
#include "../../kernel/linux/include/uapi/linux/xtos_bci_frame.h"
u64 ktime_get_ns(void);
typedef int32_t s32;
typedef uint32_t __u32;
#define HZ 100
#define GFP_KERNEL 0
#define THIS_MODULE NULL
void host_after_write(const void *address);
#define WRITE_ONCE(x,v) do { __atomic_store_n(&(x),(v),__ATOMIC_SEQ_CST); host_after_write(&(x)); } while (0)
#define READ_ONCE(x) __atomic_load_n(&(x),__ATOMIC_SEQ_CST)
#define XTOS_BCI_SERIAL 0
#define pr_info(...) ((void)0)
#define pr_err(...) ((void)0)
#define MODULE_DEVICE_TABLE(a,b)
#define MODULE_LICENSE(x)
#define MODULE_AUTHOR(x)
#define MODULE_DESCRIPTION(x)
#define MODULE_VERSION(x)
#define module_serdev_device_driver(x) static void *host_driver_reference __attribute__((unused)) = &(x)
struct mutex { pthread_mutex_t native; unsigned int initialized; };
static inline void mutex_init(struct mutex *m)
{
    pthread_mutexattr_t attr;
    assert(!m->initialized);
    assert(!pthread_mutexattr_init(&attr));
    assert(!pthread_mutexattr_settype(&attr, PTHREAD_MUTEX_ERRORCHECK));
    assert(!pthread_mutex_init(&m->native, &attr));
    assert(!pthread_mutexattr_destroy(&attr));
    m->initialized = 1;
}
void host_before_lock(struct mutex *m);
static inline void mutex_lock(struct mutex *m)
{
    host_before_lock(m);
    assert(m->initialized);
    assert(!pthread_mutex_lock(&m->native));
}
void host_before_unlock(struct mutex *m);
static inline void mutex_unlock(struct mutex *m)
{
    host_before_unlock(m);
    assert(m->initialized);
    assert(!pthread_mutex_unlock(&m->native));
}
typedef struct mutex spinlock_t;
#define spin_lock_init(x) mutex_init(x)
#define spin_lock_irqsave(x,f) do { (f) = 0; mutex_lock(x); } while (0)
#define spin_unlock_irqrestore(x,f) do { (void)(f); mutex_unlock(x); } while (0)
static inline void msleep(unsigned int ms) { (void)ms; }
typedef struct { pthread_mutex_t lock; pthread_cond_t cond; } wait_queue_head_t;
static inline void init_waitqueue_head(wait_queue_head_t *w) { pthread_condattr_t a; assert(!pthread_condattr_init(&a)); assert(!pthread_condattr_setclock(&a,CLOCK_MONOTONIC)); pthread_mutex_init(&w->lock,NULL); pthread_cond_init(&w->cond,&a); assert(!pthread_condattr_destroy(&a)); }
static inline void wake_up_interruptible(wait_queue_head_t *w) { pthread_mutex_lock(&w->lock); pthread_cond_broadcast(&w->cond); pthread_mutex_unlock(&w->lock); }

#include <time.h>
#ifndef ERESTARTSYS
#define ERESTARTSYS 512
#endif
static bool host_fixed_clock, host_clock_after_tx;
static unsigned long host_clock, host_tx_ticks;
static long host_wait_result=1;
static unsigned long host_wait_budget;
static unsigned int host_wait_calls;
static inline unsigned long host_jiffies(void) { if (host_fixed_clock) { unsigned long now=host_clock; if (host_clock_after_tx) host_clock+=2; return now; } struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (unsigned long)t.tv_sec*HZ+t.tv_nsec/(1000000000/HZ); }
#define jiffies host_jiffies()
#define time_after_eq(a,b) ((long)((a)-(b))>=0)
void host_wait_enter(void);
#define wait_event_interruptible_timeout(w,c,t) ({ long _r=host_wait_result; host_wait_budget=(t); host_wait_calls++; assert(host_wait_budget<=3*HZ); if (_r==1) { struct timespec _end; clock_gettime(CLOCK_MONOTONIC,&_end); _end.tv_nsec+=(long)host_wait_budget*(1000000000/HZ); _end.tv_sec+=_end.tv_nsec/1000000000; _end.tv_nsec%=1000000000; pthread_mutex_lock(&(w).lock); while (!(c)) { host_wait_enter(); int _e=pthread_cond_timedwait(&(w).cond,&(w).lock,&_end); if (_e==ETIMEDOUT) { _r=0; break; } assert(!_e); } pthread_mutex_unlock(&(w).lock); } _r; })

struct device { int unused; };
struct serdev_device;
struct serdev_device_ops {
    size_t (*receive_buf)(struct serdev_device *, const unsigned char *, size_t);
    void (*write_wakeup)(struct serdev_device *);
};
struct serdev_device { struct device dev; void *data; const struct serdev_device_ops *ops; };
struct of_device_id { const char *compatible; };
struct serdev_device_driver {
    int (*probe)(struct serdev_device *);
    void (*remove)(struct serdev_device *);
    struct { const char *name; const struct of_device_id *of_match_table; } driver;
};
struct xtos_bci_ringbuf { size_t size; unsigned int writes; s32 last[8]; wait_queue_head_t wait; };
struct xtos_bci_dev;
struct xtos_bci_device_ops {
    void (*cancel)(struct xtos_bci_dev *);
    int (*get_config)(struct xtos_bci_dev *, struct xtos_bci_config_v1 *);
    int (*set_channel_config)(struct xtos_bci_dev *, int, bool, int);
    int (*init)(struct xtos_bci_dev *);
    void (*cleanup)(struct xtos_bci_dev *);
    int (*start_stream)(struct xtos_bci_dev *);
    int (*stop_stream)(struct xtos_bci_dev *);
    int (*set_sampling_rate)(struct xtos_bci_dev *, int);
    int (*set_channel_enable)(struct xtos_bci_dev *, int, bool);
    int (*calibrate)(struct xtos_bci_dev *);
    int (*impedance_check)(struct xtos_bci_dev *, __u32 *);
};
struct xtos_bci_dev {
    char name[32];
    int num_channels, sampling_rate, resolution_bits, input_range_mv, interface;
    void *hw_private;
    const struct xtos_bci_device_ops *ops;
    void *owner;
    void (*release)(struct xtos_bci_dev *);
    struct xtos_bci_ringbuf *ringbuf;
    unsigned long samples_read, samples_dropped, errors;
    int acquisition_error;
    bool opened, streaming, disconnected;
    spinlock_t lock;
};
static inline void *kzalloc(size_t n, int flags) { (void)flags; return calloc(1,n); }
#define kfree free
static inline void xtos_bci_device_init(struct xtos_bci_dev *d) { spin_lock_init(&d->lock); }
static inline void xtos_bci_put_device(struct xtos_bci_dev *d) { d->release(d); }
static inline void *devm_kzalloc(struct device *dev, size_t n, int flags)
{ (void)dev; (void)flags; return calloc(1, n); }
static inline void serdev_device_set_drvdata(struct serdev_device *s, void *p) { s->data = p; }
static inline void *serdev_device_get_drvdata(struct serdev_device *s) { return s->data; }
static inline void serdev_device_set_client_ops(struct serdev_device *s, const struct serdev_device_ops *ops) { s->ops = ops; }
static inline unsigned int serdev_device_set_baudrate(struct serdev_device *s, unsigned int rate) { (void)s; return rate; }
static inline void serdev_device_set_flow_control(struct serdev_device *s, bool flow) { (void)s; (void)flow; }
static inline void serdev_device_write_wakeup(struct serdev_device *s) { (void)s; }
int serdev_device_open(struct serdev_device *s);
void serdev_device_close(struct serdev_device *s);
int serdev_device_write(struct serdev_device *s, const void *data, size_t len, unsigned long timeout);
int xtos_bci_register_device(struct xtos_bci_dev *dev);
void xtos_bci_unregister_device(struct xtos_bci_dev *dev);
struct xtos_bci_ringbuf *xtos_bci_ringbuf_alloc(struct device *dev, size_t size);
void xtos_bci_ringbuf_free(struct device *dev, struct xtos_bci_ringbuf *rb);
int xtos_bci_ringbuf_write(struct xtos_bci_ringbuf *rb, const void *data, size_t len);
int xtos_bci_device_frames_enable(struct xtos_bci_dev *dev, unsigned int capacity_frames);
int xtos_bci_device_frame_publish(struct xtos_bci_dev *dev, const struct xtos_bci_frame_v1 *frame, const struct xtos_bci_config_v1 *config);
void xtos_bci_device_frames_wake(struct xtos_bci_dev *dev);
#endif
