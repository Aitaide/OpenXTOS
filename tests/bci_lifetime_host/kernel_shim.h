/* SPDX-License-Identifier: GPL-2.0 */
#ifndef BCI_KERNEL_SHIM_H
#define BCI_KERNEL_SHIM_H
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/sysmacros.h>
#include <poll.h>
#include <sys/epoll.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include <limits.h>
typedef int32_t s32;
typedef int32_t __s32;
typedef int64_t s64;
typedef uint32_t u32;
typedef uint64_t u64;
typedef uint32_t __u32;
typedef uint8_t __u8;
typedef uint64_t __u64;
#define __aligned_u64 __u64 __attribute__((aligned(8)))
typedef uintptr_t dma_addr_t;
typedef unsigned int __poll_t;
typedef int poll_table;
#define __user
static bool host_compat_syscall;
static inline bool in_compat_syscall(void) { return host_compat_syscall; }
static inline bool access_ok(const void *p,size_t n) { return p && n && (uintptr_t)p <= UINTPTR_MAX-n; }
#define __init
#define __exit
#define GFP_KERNEL 0
#define PAGE_SIZE 4096
#define HZ 100
#define U64_MAX UINT64_MAX
#define min(a,b) ((a) < (b) ? (a) : (b))
#define max(a,b) ((a) > (b) ? (a) : (b))
#define READ_ONCE(x) __atomic_load_n(&(x), __ATOMIC_SEQ_CST)
#define WRITE_ONCE(x,v) __atomic_store_n(&(x), (v), __ATOMIC_SEQ_CST)
#define container_of(p,t,m) ((t *)((char *)(p) - offsetof(t,m)))
#define pr_info(...) ((void)0)
#define pr_err(...) ((void)0)
#define pr_debug(...) ((void)0)
#define pr_debug_ratelimited(...) ((void)0)
#define EXPORT_SYMBOL_GPL(x)
#define MODULE_LICENSE(x)
#define MODULE_AUTHOR(x)
#define MODULE_DESCRIPTION(x)
#define MODULE_VERSION(x)
#define MODULE_DEVICE_TABLE(a,b)
#define module_init(x)
#define module_exit(x)
#define module_serdev_device_driver(x)
struct module { atomic_int refs; bool dying; };
static struct module core_module, driver_module;
#define THIS_MODULE (&driver_module)
static inline bool try_module_get(struct module *m) { if (m && m->dying) return false; if (m) atomic_fetch_add(&m->refs, 1); return true; }
static inline void module_put(struct module *m) { if (m) assert(atomic_fetch_sub(&m->refs, 1) > 0); }
struct mutex { pthread_mutex_t native; };
#define DEFINE_MUTEX(x) struct mutex x = { PTHREAD_MUTEX_INITIALIZER }
void host_before_lock(struct mutex *m);
static inline void mutex_init(struct mutex *m) { assert(!pthread_mutex_init(&m->native, NULL)); }
static inline void mutex_lock(struct mutex *m) { host_before_lock(m); assert(!pthread_mutex_lock(&m->native)); }
static inline int mutex_lock_interruptible(struct mutex *m) { mutex_lock(m); return 0; }
static inline void mutex_unlock(struct mutex *m) { assert(!pthread_mutex_unlock(&m->native)); }
typedef struct mutex spinlock_t;
#define spin_lock_init(x) mutex_init(x)
#define spin_lock_irqsave(x,f) do { (f)=0; mutex_lock(x); } while (0)
#define spin_unlock_irqrestore(x,f) do { (void)(f); mutex_unlock(x); } while (0)
struct list_head { struct list_head *next, *prev; };
#define LIST_HEAD(x) struct list_head x = { &(x), &(x) }
#define INIT_LIST_HEAD(x) do { (x)->next = (x); (x)->prev = (x); } while (0)
static inline void list_add_tail(struct list_head *n, struct list_head *h) { n->prev=h->prev; n->next=h; h->prev->next=n; h->prev=n; }
static inline void list_del_init(struct list_head *n) { n->prev->next=n->next; n->next->prev=n->prev; INIT_LIST_HEAD(n); }
#define list_del(n) list_del_init(n)
#define list_for_each_entry(p,h,m) for (p=container_of((h)->next,__typeof__(*p),m); &p->m != (h); p=container_of(p->m.next,__typeof__(*p),m))
struct kref { atomic_int refs; };
static inline void kref_init(struct kref *k) { atomic_init(&k->refs, 1); }
static inline void kref_get(struct kref *k) { assert(atomic_fetch_add(&k->refs, 1)>0); }
static inline void kref_put(struct kref *k, void (*release)(struct kref *)) { if (atomic_fetch_sub(&k->refs, 1)==1) release(k); }
struct device { atomic_int refs; };
static inline struct device *get_device(struct device *d) { if (d) atomic_fetch_add(&d->refs,1); return d; }
static inline void put_device(struct device *d) { if (d) atomic_fetch_sub(&d->refs,1); }
struct class { int unused; };
struct file;
struct inode;
struct vm_area_struct;
struct vm_operations_struct { void (*open)(struct vm_area_struct *); void (*close)(struct vm_area_struct *); };
struct vm_area_struct { unsigned long vm_start, vm_end, vm_pgoff, vm_flags; void *vm_private_data; const struct vm_operations_struct *vm_ops; };
#define VM_READ 1UL
#define VM_WRITE 2UL
#define VM_SHARED 8UL
#define VM_MAYWRITE 32UL
#define PAGE_ALIGN(n) (((n) + PAGE_SIZE - 1) & ~(PAGE_SIZE - 1))
static inline void vm_flags_clear(struct vm_area_struct *v,unsigned long f) { v->vm_flags &= ~f; }
typedef struct { atomic_uint refs; } refcount_t;
static inline void refcount_set(refcount_t *r,unsigned int n) { atomic_init(&r->refs,n); }
static inline void refcount_inc(refcount_t *r) { assert(atomic_fetch_add(&r->refs,1)>0); }
static inline bool refcount_dec_and_test(refcount_t *r) { unsigned int n=atomic_fetch_sub(&r->refs,1); assert(n); return n==1; }
static atomic_int host_mappings;
static int host_remap_error;
static inline void *vmalloc_user(size_t n) { void *p=calloc(1,n); if (p) atomic_fetch_add(&host_mappings,1); return p; }
static inline void vfree(void *p) { if (p) { atomic_fetch_sub(&host_mappings,1); free(p); } }
static inline int remap_vmalloc_range(struct vm_area_struct *v,void *p,unsigned long off) { assert(v && p && !off); return host_remap_error; }
struct file_operations { struct module *owner; int (*open)(struct inode *,struct file *); int (*release)(struct inode *,struct file *); ssize_t (*read)(struct file *,char *,size_t,loff_t *); __poll_t (*poll)(struct file *,poll_table *); int (*mmap)(struct file *,struct vm_area_struct *); long (*unlocked_ioctl)(struct file *,unsigned int,unsigned long); long (*compat_ioctl)(struct file *,unsigned int,unsigned long); };
struct cdev { int kobj; struct module *owner; const struct file_operations *ops; };
struct inode { dev_t i_rdev; struct cdev *i_cdev; };
struct file { void *private_data; int f_flags; };
static bool host_fail_kzalloc, host_fail_from;
static inline void *kzalloc(size_t n,int flags) { (void)flags; return host_fail_kzalloc ? NULL : calloc(1,n); }
static inline void *kcalloc(size_t n,size_t z,int flags) { (void)flags; return host_fail_kzalloc ? NULL : calloc(n,z); }
static inline void *kmalloc(size_t n,int flags) { (void)flags; return malloc(n); }
#define kfree free
static inline struct cdev *cdev_alloc(void) { return calloc(1,sizeof(struct cdev)); }
static inline void cdev_init(struct cdev *c,const struct file_operations *ops) { c->ops=ops; }
static inline int cdev_add(struct cdev *c,dev_t d,int n) { (void)c; (void)d; (void)n; return 0; }
static bool host_retain_cdev;
static inline void cdev_del(struct cdev *c) { if (!host_retain_cdev) free(c); }
#define MAJOR major
#define MKDEV makedev
#define IS_ERR(p) ((uintptr_t)(p) > (uintptr_t)-4096)
#define PTR_ERR(p) ((long)(p))
#define ERR_PTR(e) ((void *)(intptr_t)(e))
static inline void kobject_put(void *p) { free(p); }
static inline struct device *device_create(struct class *c,void *p,dev_t d,void *data,const char *fmt,...) { (void)c;(void)p;(void)d;(void)data;(void)fmt; static struct device node; return &node; }
static inline void device_destroy(struct class *c,dev_t d) { (void)c;(void)d; }
static inline int alloc_chrdev_region(dev_t *d,int s,int n,const char *name) { (void)s;(void)n;(void)name; *d=makedev(240,0); return 0; }
static inline void unregister_chrdev_region(dev_t d,int n) { (void)d;(void)n; }
static inline struct class *class_create(const char *n) { (void)n; static struct class c; return &c; }
static inline void class_destroy(struct class *c) { (void)c; }
typedef struct { pthread_mutex_t lock; pthread_cond_t cond; unsigned int generation; } wait_queue_head_t;
static inline void init_waitqueue_head(wait_queue_head_t *w) { pthread_mutex_init(&w->lock,NULL); pthread_cond_init(&w->cond,NULL); w->generation=0; }
static inline void wake_up_interruptible(wait_queue_head_t *w) { pthread_mutex_lock(&w->lock); w->generation++; pthread_cond_broadcast(&w->cond); pthread_mutex_unlock(&w->lock); }
void host_waiting(void);
#define wait_event_interruptible(w,condition) ({ int _ret=0; pthread_mutex_lock(&(w).lock); while (!(condition)) { host_waiting(); pthread_cond_wait(&(w).cond,&(w).lock); } pthread_mutex_unlock(&(w).lock); _ret; })

#include <time.h>
#ifndef ERESTARTSYS
#define ERESTARTSYS 512
#endif
static inline unsigned long host_jiffies(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (unsigned long)t.tv_sec*HZ+t.tv_nsec/(1000000000/HZ); }
static inline u64 ktime_get_ns(void) { struct timespec t; assert(!clock_gettime(CLOCK_MONOTONIC,&t)); return (u64)t.tv_sec*1000000000ULL+(u64)t.tv_nsec; }
#define jiffies host_jiffies()
#define time_after_eq(a,b) ((long)((a)-(b))>=0)
#define wait_event_interruptible_timeout(w,c,t) ({ long _r=1; struct timespec _end; clock_gettime(CLOCK_REALTIME,&_end); _end.tv_nsec+=(long)(t)*(1000000000/HZ); _end.tv_sec+=_end.tv_nsec/1000000000; _end.tv_nsec%=1000000000; pthread_mutex_lock(&(w).lock); while (!(c)) { host_waiting(); int _e=pthread_cond_timedwait(&(w).cond,&(w).lock,&_end); if (_e==ETIMEDOUT) { _r=0; break; } assert(!_e); } pthread_mutex_unlock(&(w).lock); _r; })

static inline void poll_wait(struct file *f,wait_queue_head_t *w,poll_table *p) { (void)f;(void)w;(void)p; }
static struct { struct device *device; void *pointer; size_t bytes; } dma_records[64];
static unsigned int dma_allocs,dma_frees;
static inline void *dma_alloc_coherent(struct device *d,size_t n,dma_addr_t *h,int flags) { (void)flags; void *p=malloc(n); for (size_t i=0;i<64;i++) if (!dma_records[i].pointer) { dma_records[i].device=d; dma_records[i].pointer=p; dma_records[i].bytes=n; dma_allocs++; *h=(uintptr_t)p; return p; } abort(); }
static inline void dma_free_coherent(struct device *d,size_t n,void *p,dma_addr_t h) { for (size_t i=0;i<64;i++) if (dma_records[i].pointer==p) { assert(d==dma_records[i].device && n==dma_records[i].bytes && h==(uintptr_t)p); dma_records[i].pointer=NULL; dma_frees++; free(p); return; } abort(); }
static inline int dma_mmap_coherent(struct device *d,struct vm_area_struct *v,void *p,dma_addr_t h,size_t n) { (void)d;(void)v;(void)p;(void)h;(void)n; return 0; }
unsigned long host_copy_to_user(void *dst,const void *src,size_t n);
#define copy_to_user host_copy_to_user
static inline unsigned long copy_from_user(void *d,const void *s,size_t n) { if (host_fail_from) return n; memcpy(d,s,n); return 0; }
static inline void msleep(unsigned int ms) { (void)ms; }
struct serdev_device;
struct serdev_device_ops { int (*receive_buf)(struct serdev_device *,const unsigned char *,size_t); void (*write_wakeup)(struct serdev_device *); };
struct serdev_device { struct device dev; void *data; const struct serdev_device_ops *ops; bool closed; };
struct of_device_id { const char *compatible; };
struct serdev_device_driver { int (*probe)(struct serdev_device *); void (*remove)(struct serdev_device *); struct { const char *name; const struct of_device_id *of_match_table; } driver; };
static inline void serdev_device_set_drvdata(struct serdev_device *s,void *p) { s->data=p; }
static inline void *serdev_device_get_drvdata(struct serdev_device *s) { return s->data; }
static inline void serdev_device_set_client_ops(struct serdev_device *s,const struct serdev_device_ops *o) { s->ops=o; }
static inline int serdev_device_open(struct serdev_device *s) { s->closed=false; return 0; }
static inline void serdev_device_close(struct serdev_device *s) { s->closed=true; }
static inline unsigned int serdev_device_set_baudrate(struct serdev_device *s,unsigned int r) { (void)s; return r; }
static inline void serdev_device_set_flow_control(struct serdev_device *s,bool b) { (void)s;(void)b; }
static inline void serdev_device_write_wakeup(struct serdev_device *s) { (void)s; }
int serdev_device_write(struct serdev_device *s,const void *p,size_t n,unsigned long t);
static inline void *devm_kzalloc(struct device *d,size_t n,int f) { (void)d; return kzalloc(n,f); }
#endif
