/* SPDX-License-Identifier: GPL-2.0 */
/* Execute production implementations with pthread-backed kernel services. */
#include "kernel_shim.h"
#include "../../kernel/linux/include/uapi/linux/xtos_bci_config.h"
#include "../../kernel/linux/drivers/xtos/bci/xtos_bci_buffer.c"
#include "../../kernel/linux/drivers/xtos/bci/xtos_bci_snapshot.c"
#include "../../kernel/linux/drivers/xtos/bci/xtos_bci_frame.c"
#include "../../kernel/linux/drivers/xtos/bci/xtos_bci_frame_snapshot.c"
#include "../../kernel/linux/drivers/xtos/bci/xtos_bci_core.c"
#define XTOS_CYTON_HOST_TEST 1
#include "../../kernel/linux/drivers/xtos/bci/devices/openbci_cyton.c"


static unsigned char board_regs[24] = {0x3e,0x96,0xc0,0xec,2,0x68,0x68,0x68,0x68,0x68,0x68,0x68,0x68,255,255};
static bool missing_reply, missing_row, wrong_ack, no_effect;
static void board_reply(struct serdev_device *s,const void *data,size_t len)
{
    const char *p=data;
    char text[4096]; size_t n=0;
    static const char *names[24]={"ADS_ID","CONFIG1","CONFIG2","CONFIG3","LOFF","CH1SET","CH2SET","CH3SET","CH4SET","CH5SET","CH6SET","CH7SET","CH8SET","BIAS_SENSP","BIAS_SENSN","LOFF_SENSP","LOFF_SENSN","LOFF_FLIP","LOFF_STATP","LOFF_STATN","GPIO","MISC1","MISC2","CONFIG4"};
    if (missing_reply) return;
    if (len==1 && p[0]=='v') {
        for (int i=5;i<13;i++) board_regs[i]=0x68;
        n=snprintf(text,sizeof(text),"OpenBCI V3 8-16 channel\r\nOn Board ADS1299 Device ID: 0x3E\r\nLIS3DH Device ID: 0x33\r\nFirmware: v3.1.2\r\n$$$");
    } else if (len==1 && p[0]=='?') {
        n=snprintf(text,sizeof(text),"Board ADS Registers\r\n");
        for (int i=0;i<24;i++) {
            if (missing_row && i==23) continue;
            unsigned v=board_regs[i];
            n+=snprintf(text+n,sizeof(text)-n,"%s, %02X, %02X",names[i],i,v);
            for (int b=7;b>=0;b--) n+=snprintf(text+n,sizeof(text)-n,", %u",(v>>b)&1);
            n+=snprintf(text+n,sizeof(text)-n,"\r\n");
        }
        n+=snprintf(text+n,sizeof(text)-n,"LIS3DH Registers\r\n$$$");
    } else if (len==9 && p[0]=='x') {
        unsigned ch=p[1]-'1'; assert(ch<8);
        if (!no_effect) {
            board_regs[5+ch]=((p[2]-'0')<<7)|((p[3]-'0')<<4)|((p[6]-'0')<<3)|(p[4]-'0');
            for (int i=13;i<=14;i++) board_regs[i]=(board_regs[i]&~(1u<<ch))|((p[5]-'0')<<ch);
            board_regs[21]=(p[7]-'0')<<5;
        }
        n=snprintf(text,sizeof(text),"Success: Channel set for %c$$$",wrong_ack ? '9':p[1]);
    } else if (len==1) {
        const char *q=strchr("!@#$%^&*",p[0]);
        if (q && !no_effect) board_regs[5+(q-"!@#$%^&*")] &= 0x7f;
        q=strchr("12345678",p[0]);
        if (q && !no_effect) board_regs[5+(q-"12345678")] |= 0x80;
    }
    for (size_t i=0;i<n;i+=7) { size_t part=n-i<7?n-i:7; assert(s->ops->receive_buf(s,(unsigned char *)text+i,part)==(int)part); }
}

int serdev_device_write(struct serdev_device *s,const void *p,size_t n,unsigned long t) { (void)t; assert(!s->closed); board_reply(s,p,n); return n; }

static pthread_mutex_t gate=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed=PTHREAD_COND_INITIALIZER;
static bool paused, resume_thread, reader_waiting, copy_pause, fail_copy;
static unsigned int copy_calls, copy_fail_at;
static _Thread_local bool pause_open;
static _Thread_local bool pause_read_op;
static struct mutex *open_lock;
static void flag_wait(bool *flag)
{
    pthread_mutex_lock(&gate);
    while (!*flag) pthread_cond_wait(&changed,&gate);
    pthread_mutex_unlock(&gate);
}
static void flag_set(bool *flag)
{
    pthread_mutex_lock(&gate);
    *flag=true;
    pthread_cond_broadcast(&changed);
    pthread_mutex_unlock(&gate);
}
static void pause_here(void)
{
    pthread_mutex_lock(&gate);
    paused=true;
    pthread_cond_broadcast(&changed);
    while (!resume_thread) pthread_cond_wait(&changed,&gate);
    pthread_mutex_unlock(&gate);
}
void host_before_lock(struct mutex *m)
{
    if (pause_open && m==open_lock) { pause_open=false; pause_here(); }
    if (pause_read_op && m==open_lock) { pause_read_op=false; pause_here(); }
}
void host_waiting(void) { flag_set(&reader_waiting); }
unsigned long host_copy_to_user(void *d,const void *s,size_t n)
{
	copy_calls++;
	if (copy_pause) { copy_pause=false; pause_here(); }
	if (fail_copy || (copy_fail_at && copy_calls == copy_fail_at)) return n;
    memcpy(d,s,n);
    return 0;
}
struct fixture { struct serdev_device serial; struct inode inode; struct file file; };
static struct xtos_bci_dev *setup(struct fixture *f,bool open_fd)
{
    assert(!openbci_probe(&f->serial));
    struct xtos_bci_dev *dev=f->serial.data;
    f->inode.i_rdev=dev->devt;
    f->inode.i_cdev=dev->cdev;
    if (open_fd) assert(!xtos_bci_open(&f->inode,&f->file));
    return dev;
}
static void teardown(struct fixture *f)
{
    if (f->serial.data) openbci_remove(&f->serial);
    if (f->file.private_data) assert(!xtos_bci_release(&f->inode,&f->file));
}
static void *opening_direct(void *arg) { struct fixture *f=arg; return (void *)(intptr_t)xtos_bci_open(&f->inode,&f->file); }
static void *opening(void *arg)
{
    struct fixture *f=arg;
    pause_open=true;
    return (void *)(intptr_t)xtos_bci_open(&f->inode,&f->file);
}
struct read_job { struct file *file; unsigned char bytes[8192]; ssize_t result; };
static void *reading(void *arg)
{
    struct read_job *j=arg;
    j->result=xtos_bci_read(j->file,(char *)j->bytes,sizeof(j->bytes),NULL);
    return NULL;
}
static void *writing(void *arg)
{
    struct xtos_bci_ringbuf *rb=arg;
    unsigned char bytes[32]={0};
    for (int i=0;i<10000;i++) assert(xtos_bci_ringbuf_write(rb,bytes,32)==32);
    return NULL;
}
int main(int argc,char **argv)
{
    assert(argc==2);
    assert(!xtos_bci_init());
    struct fixture f={0};
    if (!strcmp(argv[1],"cancel_open_reply")) {
        setup(&f,false);
        missing_reply=true;
        pthread_t t;
        assert(!pthread_create(&t,NULL,opening_direct,&f));
        flag_wait(&reader_waiting);
        unsigned long before=jiffies;
        openbci_remove(&f.serial);
        void *result; assert(!pthread_join(t,&result));
        assert((intptr_t)result==-ENODEV);
        assert(jiffies-before < HZ);
        assert(!f.file.private_data && f.serial.closed);
    } else if (!strcmp(argv[1],"legacy_mmap")) {
        setup(&f,true);
        struct vm_area_struct vma={.vm_end=4096};
        assert(xtos_bci_mmap(&f.file,&vma)==-EOPNOTSUPP);
        teardown(&f);
    } else if (!strcmp(argv[1],"retained_fd")) {
        struct xtos_bci_dev *dev=setup(&f,true);
        assert(atomic_load(&driver_module.refs)==1);
        assert(atomic_load(&f.serial.dev.refs)==1);
        openbci_remove(&f.serial);
        assert(dma_frees==0);
        char bytes[32];
        assert(xtos_bci_read(&f.file,bytes,sizeof(bytes),NULL)==-ENODEV);
        assert(xtos_bci_poll(&f.file,NULL)==(EPOLLHUP|EPOLLERR));
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_START_STREAM,0)==-ENODEV);
        assert(dev->disconnected);
        teardown(&f);
        assert(dma_frees==1 && atomic_load(&f.serial.dev.refs)==0);
        /* Release must not transmit through the already closed transport. */
    } else if (!strcmp(argv[1],"open_unregister")) {
        struct xtos_bci_dev *dev=setup(&f,false);
        open_lock=&dev->op_lock;
        pthread_t t;
        assert(!pthread_create(&t,NULL,opening,&f));
        flag_wait(&paused); /* open owns both lifetime and module refs */
        assert(atomic_load(&driver_module.refs)==1);
        openbci_remove(&f.serial);
        assert(!f.file.private_data);
        flag_set(&resume_thread);
        void *result;
        assert(!pthread_join(t,&result));
        assert((intptr_t)result==-ENODEV);
        /* No descriptor dereference via stale inode->i_cdev. */
        f.inode.i_cdev=(void *)(uintptr_t)1;
        assert(xtos_bci_open(&f.inode,&f.file)==-ENODEV);
    } else if (!strcmp(argv[1],"blocked_unbind") || !strcmp(argv[1],"blocked_stop")) {
        setup(&f,true);
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_START_STREAM,0));
        struct read_job j={.file=&f.file};
        pthread_t t;
        assert(!pthread_create(&t,NULL,reading,&j));
        flag_wait(&reader_waiting);
        bool unbind=!strcmp(argv[1],"blocked_unbind");
        if (unbind) openbci_remove(&f.serial);
        else assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_STOP_STREAM,0));
        assert(!pthread_join(t,NULL));
        assert(j.result==(unbind ? -ENODEV : 0));
        teardown(&f);
    } else if (!strcmp(argv[1],"two_devices_ids")) {
        struct fixture all[17]={0};
        for (int i=0;i<16;i++) {
            struct xtos_bci_dev *d=setup(&all[i],false);
            assert(d->dev_id==i);
            if (i) assert(d!=all[i-1].serial.data);
        }
        assert(openbci_probe(&all[16].serial)==-ENOSPC);
        assert(!all[16].serial.data && all[16].serial.closed);
        dev_t old=all[3].inode.i_rdev;
        teardown(&all[3]);
        struct xtos_bci_dev *d=setup(&all[16],true);
        assert(d->dev_id==3 && d->devt==old);
        for (int i=0;i<17;i++) teardown(&all[i]);
        for (int i=0;i<32;i++) { setup(&f,false); teardown(&f); }
    } else if (!strcmp(argv[1],"stale_inode_reuse")) {
        setup(&f,false);
        struct inode retired=f.inode;
        /* A cached VFS inode retains the retired cdev allocation. */
        host_retain_cdev=true;
        teardown(&f);
        host_retain_cdev=false;
        struct fixture replacement={0};
        setup(&replacement,false);
        assert(replacement.inode.i_rdev==retired.i_rdev);
        assert(replacement.inode.i_cdev!=retired.i_cdev);
        struct file stale={0};
        assert(xtos_bci_open(&retired,&stale)==-ENODEV);
        assert(!stale.private_data && !atomic_load(&driver_module.refs));
        assert(!xtos_bci_open(&replacement.inode,&replacement.file));
        teardown(&replacement);
        free(retired.i_cdev);
    } else if (!strcmp(argv[1],"stats_writer")) {
        struct xtos_bci_dev *dev=setup(&f,true);
        pthread_t t;
        assert(!pthread_create(&t,NULL,writing,dev->ringbuf));
        for (int i=0;i<10000;i++) {
            struct xtos_bci_stats stats;
            assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_GET_STATS,(unsigned long)&stats));
            assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_RESET_STATS,0));
        }
        assert(!pthread_join(t,NULL));
        teardown(&f);
    } else if (!strcmp(argv[1],"reopen_empty")) {
        struct xtos_bci_dev *dev=setup(&f,true);
        unsigned char bytes[32]={1};
        assert(xtos_bci_ringbuf_write(dev->ringbuf,bytes,sizeof(bytes))==32);
        assert(!xtos_bci_release(&f.inode,&f.file));
        assert(!xtos_bci_open(&f.inode,&f.file));
        assert(!xtos_bci_ringbuf_available(dev->ringbuf));
        teardown(&f);
    } else if (!strcmp(argv[1],"concurrent_overwrite") || !strcmp(argv[1],"efault")) {
        struct xtos_bci_dev *dev=setup(&f,true);
        struct xtos_bci_ringbuf *rb=dev->ringbuf;
        unsigned char *old=malloc(rb->size), *newer=malloc(rb->size);
        memset(old,0x11,rb->size); memset(newer,0xee,rb->size);
        assert(xtos_bci_ringbuf_write(rb,old,rb->size)==(int)rb->size);
        struct read_job j={.file=&f.file};
        if (!strcmp(argv[1],"efault")) {
            fail_copy=true;
            reading(&j);
            assert(j.result==-EFAULT && rb->read_pos==0);
            fail_copy=false;
            reading(&j);
            assert(j.result==PAGE_SIZE && j.bytes[0]==0x11);
        } else {
            copy_pause=true;
            pthread_t t;
            assert(!pthread_create(&t,NULL,reading,&j));
            flag_wait(&paused); /* user copy has stable ring snapshot */
            assert(xtos_bci_ringbuf_write(rb,newer,rb->size)==(int)rb->size);
            flag_set(&resume_thread);
            assert(!pthread_join(t,NULL));
            assert(j.result==PAGE_SIZE);
            for (int i=0;i<PAGE_SIZE;i++) assert(j.bytes[i]==0x11);
            assert(xtos_bci_ringbuf_available(rb)==rb->size);
            assert(rb->read_pos==rb->size && rb->max_fill==rb->size);
        }
        free(old); free(newer);
        teardown(&f);
    } else { assert(!"unknown case"); }
    assert(atomic_load(&driver_module.refs)==0);
    assert(dma_allocs==dma_frees);
    xtos_bci_exit();
    puts(argv[1]);
    return 0;
}
