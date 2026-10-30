/* SPDX-License-Identifier: GPL-2.0 */
/* Reuse services/fixtures; every operation executes the production C. */
#define main lifetime_main
#include "../bci_lifetime_host/lifetime_test.c"
#undef main

static struct xtos_bci_snapshot_request request(void)
{
    struct xtos_bci_snapshot_request r={.version=XTOS_BCI_SNAPSHOT_VERSION,
                                         .requested_bytes=64};
    return r;
}
static long acquire(struct fixture *f,struct xtos_bci_snapshot_request *r)
{
    return xtos_bci_ioctl(&f->file,XTOS_BCI_SNAPSHOT_ACQUIRE,(unsigned long)r);
}
static long release_snapshot(struct fixture *f,u64 token)
{
    struct xtos_bci_snapshot_release r={.version=1,.token=token};
    return xtos_bci_ioctl(&f->file,XTOS_BCI_SNAPSHOT_RELEASE,(unsigned long)&r);
}
static struct vm_area_struct mapping(u64 bytes)
{
    struct vm_area_struct v={.vm_start=4096,.vm_end=4096+bytes,
                            .vm_flags=VM_SHARED|VM_READ|VM_MAYWRITE};
    return v;
}
static void assert_mapping(struct vm_area_struct *v)
{
    struct xtos_bci_snapshot *s=v->vm_private_data;
    const struct xtos_bci_snapshot_header *h=s->mapping;
    assert(h->magic==XTOS_BCI_SNAPSHOT_MAGIC && h->version==1);
    assert(h->payload_bytes==64 && h->channels==8 && h->sample_size==4);
    const unsigned char *p=(const unsigned char *)(h+1);
    for (int i=0;i<64;i++) assert(p[i]==0x41);
    for (size_t i=sizeof(*h)+64;i<s->mapping_bytes;i++)
        assert(((unsigned char *)s->mapping)[i]==0);
}
static void *read_before_op(void *arg)
{
    pause_read_op=true;
    return reading(arg);
}
int main(int argc,char **argv)
{
    assert(argc==2);
    assert(!xtos_bci_init());
    struct fixture f={0};
    struct xtos_bci_dev *dev=setup(&f,true);
    unsigned char bytes[64]; memset(bytes,0x41,sizeof(bytes));
    assert(xtos_bci_ringbuf_write(dev->ringbuf,bytes,sizeof(bytes))==64);
    struct xtos_bci_snapshot_request r=request();
    if (!strcmp(argv[1],"acquire")) {
        r.mapping_bytes=r.payload_bytes=r.cursor=r.token=UINT64_MAX;
        r.channels=r.sample_rate_hz=UINT32_MAX;
        assert(acquire(&f,&r)==0);
        assert(r.token==1 && r.mapping_bytes==4096 && r.payload_bytes==64);
        assert(r.channels==8 && r.sample_rate_hz==250);
        assert(!r.flags && !r.reserved[0] && !r.reserved[1]);
        assert(r.requested_bytes==64 && dev->ringbuf->read_pos==0);
        assert(acquire(&f,&r)==-EBUSY);
    } else if (!strcmp(argv[1],"acquire_validation")) {
        for (int i=0;i<8;i++) {
            r=request();
            switch(i) {
            case 0: r.version=2; break;
            case 1: r.flags=1; break;
            case 2: r.reserved[0]=1; break;
            case 3: r.reserved[1]=1; break;
            case 4: r.requested_bytes=0; break;
            case 5: r.requested_bytes=3; break;
            case 6: r.requested_bytes=65540; break;
            case 7: r.requested_bytes=UINT64_MAX; break;
            }
            assert(acquire(&f,&r)==-EINVAL);
            assert(!atomic_load(&host_mappings) && !dev->ringbuf->read_pos);
        }
        r=request(); r.requested_bytes=4;
        assert(acquire(&f,&r)==-EAGAIN);
        r=request(); r.requested_bytes=65536;
        assert(!acquire(&f,&r) && r.payload_bytes==64);
    } else if (!strcmp(argv[1],"copy_from_failure")) {
        host_fail_from=true;
        assert(acquire(&f,&r)==-EFAULT);
        host_fail_from=false;
        assert(!acquire(&f,&r));
        host_fail_from=true;
        assert(release_snapshot(&f,r.token)==-EFAULT);
        struct xtos_bci_snapshot_consume c={.version=1,.token=r.token,
                                            .cursor=r.cursor,.bytes=r.payload_bytes};
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_SNAPSHOT_CONSUME,(unsigned long)&c)==-EFAULT);
        host_fail_from=false;
        assert(dev->ringbuf->read_pos==0 && atomic_load(&host_mappings)==1);
    } else if (!strcmp(argv[1],"allocation_failure")) {
        host_fail_kzalloc=true;
        assert(acquire(&f,&r)==-ENOMEM);
        assert(!atomic_load(&host_mappings) && !dev->ringbuf->read_pos);
        host_fail_kzalloc=false;
        assert(!acquire(&f,&r));
    } else if (!strcmp(argv[1],"per_fd")) {
        struct fixture second={0};
        struct xtos_bci_dev *other=setup(&second,true);
        assert(other!=dev && second.file.private_data!=f.file.private_data);
        assert(!acquire(&f,&r));
        struct vm_area_struct v=mapping(r.mapping_bytes);
        assert(xtos_bci_mmap(&second.file,&v)==-EOPNOTSUPP);
        assert(release_snapshot(&second,r.token)==-ESTALE);
        assert(!xtos_bci_release(&second.inode,&second.file));
        host_fail_kzalloc=true;
        assert(xtos_bci_open(&second.inode,&second.file)==-ENOMEM);
        assert(!second.file.private_data);
        host_fail_kzalloc=false;
        teardown(&second);
    } else if (!strcmp(argv[1],"read_after_op")) {
        open_lock=&dev->op_lock;
        struct read_job job={.file=&f.file};
        pthread_t t;
        assert(!pthread_create(&t,NULL,read_before_op,&job));
        flag_wait(&paused);
        assert(!acquire(&f,&r));
        flag_set(&resume_thread);
        assert(!pthread_join(t,NULL) && job.result==-EBUSY);
        assert(!dev->ringbuf->read_pos);
    } else if (!strcmp(argv[1],"copy_failure")) {
        fail_copy=true;
        assert(acquire(&f,&r)==-EFAULT);
        assert(!atomic_load(&host_mappings) && !dev->ringbuf->read_pos);
        struct vm_area_struct v=mapping(4096);
        assert(xtos_bci_mmap(&f.file,&v)==-EOPNOTSUPP);
        assert(release_snapshot(&f,1)==-ESTALE);
        fail_copy=false;
        assert(!acquire(&f,&r));
        assert(!release_snapshot(&f,r.token));
        assert(!atomic_load(&host_mappings));
    } else if (!strcmp(argv[1],"mapping_args")) {
        assert(!acquire(&f,&r));
        for (int i=0;i<6;i++) {
            struct vm_area_struct v=mapping(r.mapping_bytes);
            switch(i) {
            case 0: v.vm_pgoff=1; break;
            case 1: v.vm_end+=4096; break;
            case 2: v.vm_end=v.vm_start; break;
            case 3: v.vm_flags|=VM_WRITE; break;
            case 4: v.vm_flags&=~VM_SHARED; break;
            case 5: v.vm_end=v.vm_start-1; break;
            }
            assert(xtos_bci_mmap(&f.file,&v)==-EINVAL && !v.vm_private_data);
            assert(atomic_load(&host_mappings)==1);
        }
        host_remap_error=-EAGAIN;
        struct vm_area_struct v=mapping(r.mapping_bytes);
        assert(xtos_bci_mmap(&f.file,&v)==-EAGAIN && !v.vm_private_data);
        host_remap_error=0;
        v=mapping(r.mapping_bytes);
        assert(!xtos_bci_mmap(&f.file,&v));
        assert(!(v.vm_flags&VM_MAYWRITE));
        assert_mapping(&v);
        v.vm_ops->close(&v);
    } else if (!strcmp(argv[1],"consume") || !strcmp(argv[1],"overwrite")) {
        assert(!acquire(&f,&r));
        struct xtos_bci_snapshot_consume c={.version=1,.token=r.token,
                                            .cursor=r.cursor,.bytes=r.payload_bytes};
        for (int i=0;i<7;i++) {
            struct xtos_bci_snapshot_consume bad=c;
            switch(i) {
            case 0: bad.version=2; break;
            case 1: bad.flags=1; break;
            case 2: bad.token++; break;
            case 3: bad.cursor++; break;
            case 4: bad.bytes-=4; break;
            case 5: bad.reserved[0]=1; break;
            case 6: bad.reserved[1]=1; break;
            }
            assert(xtos_bci_ioctl(&f.file,XTOS_BCI_SNAPSHOT_CONSUME,(unsigned long)&bad)==-EINVAL);
            assert(dev->ringbuf->read_pos==0);
        }
        if (!strcmp(argv[1],"overwrite")) {
            void *newer=calloc(1,dev->ringbuf->size);
            assert(xtos_bci_ringbuf_write(dev->ringbuf,newer,dev->ringbuf->size)==(int)dev->ringbuf->size);
            free(newer);
            unsigned long cursor=dev->ringbuf->read_pos;
            assert(cursor==64);
            assert(xtos_bci_ioctl(&f.file,XTOS_BCI_SNAPSHOT_CONSUME,(unsigned long)&c)==-EOVERFLOW);
            assert(dev->ringbuf->read_pos==cursor);
        } else {
            assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_SNAPSHOT_CONSUME,(unsigned long)&c));
            assert(dev->ringbuf->read_pos==64);
            assert(xtos_bci_ioctl(&f.file,XTOS_BCI_SNAPSHOT_CONSUME,(unsigned long)&c)==-ESTALE);
        }
    } else if (!strcmp(argv[1],"release_replay")) {
        assert(!acquire(&f,&r));
        struct xtos_bci_snapshot_release rel={.version=1,.token=r.token};
        for (int i=0;i<5;i++) {
            struct xtos_bci_snapshot_release bad=rel;
            switch(i) {
            case 0: bad.version=2; break;
            case 1: bad.flags=1; break;
            case 2: bad.reserved=1; break;
            case 3: bad.token=0; break;
            case 4: bad.token++; break;
            }
            assert(xtos_bci_ioctl(&f.file,XTOS_BCI_SNAPSHOT_RELEASE,(unsigned long)&bad)==-EINVAL);
            assert(atomic_load(&host_mappings)==1);
        }
        assert(!release_snapshot(&f,r.token));
        assert(release_snapshot(&f,r.token)==-ESTALE);
        u64 old=r.token;
        r=request(); assert(!acquire(&f,&r) && r.token>old);
        assert(release_snapshot(&f,old)==-EINVAL);
    } else if (!strcmp(argv[1],"read_busy") || !strcmp(argv[1],"blocked_acquire")) {
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_START_STREAM,0));
        struct read_job job={.file=&f.file};
        if (!strcmp(argv[1],"blocked_acquire")) {
            dev->ringbuf->read_pos=dev->ringbuf->write_pos;
            pthread_t t; assert(!pthread_create(&t,NULL,reading,&job));
            flag_wait(&reader_waiting);
            /* Inject data under ring lock without waking the reader. */
            unsigned long flags;
            spin_lock_irqsave(&dev->ringbuf->lock,flags);
            memcpy(dev->ringbuf->buffer+64,bytes,64);
            dev->ringbuf->write_pos+=64;
            spin_unlock_irqrestore(&dev->ringbuf->lock,flags);
            assert(!acquire(&f,&r));
            assert(!pthread_join(t,NULL) && job.result==-EBUSY);
        } else {
            assert(!acquire(&f,&r));
            reading(&job); assert(job.result==-EBUSY);
            f.file.f_flags=O_NONBLOCK;
            reading(&job); assert(job.result==-EBUSY);
            assert(xtos_bci_read(&f.file,(char *)bytes,0,NULL)==-EBUSY);
        }
    } else if (!strcmp(argv[1],"vma_close_unbind") || !strcmp(argv[1],"vma_release_clone")) {
        assert(!acquire(&f,&r));
        struct vm_area_struct v=mapping(r.mapping_bytes);
        assert(!xtos_bci_mmap(&f.file,&v));
        struct vm_area_struct clone=v;
        clone.vm_ops->open(&clone);
        assert_mapping(&v);
        if (!strcmp(argv[1],"vma_release_clone")) {
            assert(!release_snapshot(&f,r.token));
            assert(!xtos_bci_release(&f.inode,&f.file));
            openbci_remove(&f.serial);
        } else {
            openbci_remove(&f.serial);
            unsigned int commands[]={XTOS_BCI_GET_INFO,XTOS_BCI_START_STREAM,
                XTOS_BCI_STOP_STREAM,XTOS_BCI_SNAPSHOT_ACQUIRE,
                XTOS_BCI_SNAPSHOT_CONSUME,XTOS_BCI_SNAPSHOT_RELEASE,0xffffffff};
            for (size_t i=0;i<sizeof(commands)/sizeof(*commands);i++)
                assert(xtos_bci_ioctl(&f.file,commands[i],0)==-ENODEV);
            struct vm_area_struct bad=mapping(r.mapping_bytes);
            assert(xtos_bci_mmap(&f.file,&bad)==-ENODEV);
            assert(!xtos_bci_release(&f.inode,&f.file));
        }
        assert(dma_allocs==dma_frees && !atomic_load(&driver_module.refs));
        assert_mapping(&clone);
        v.vm_ops->close(&v);
        assert(atomic_load(&host_mappings)==1);
        assert_mapping(&clone);
        clone.vm_ops->close(&clone);
    } else { assert(!"unknown case"); }
    teardown(&f);
    assert(atomic_load(&host_mappings)==0);
    assert(atomic_load(&driver_module.refs)==0);
    assert(dma_allocs==dma_frees);
    xtos_bci_exit();
    puts(argv[1]);
    return 0;
}
