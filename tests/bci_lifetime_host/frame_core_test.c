/* SPDX-License-Identifier: GPL-2.0 */
#define main legacy_lifetime_main
#include "lifetime_test.c"
#undef main

static void emit(struct fixture *f, unsigned int counter)
{
    unsigned char bytes[33] = {0};
    bytes[0] = 0xa0; bytes[1] = (unsigned char)counter; bytes[32] = 0xc0;
    bytes[4] = 24;
    assert(f->serial.ops->receive_buf(&f->serial, bytes, sizeof(bytes)) == 33);
}

struct frame_read_job {
    struct file *file;
    struct xtos_bci_frame_request request;
    struct xtos_bci_frame_v1 record;
    long result;
};

static void *frame_reading(void *opaque)
{
    struct frame_read_job *job = opaque;
    job->request.version = 1; job->request.capacity_frames = 1;
    job->request.frames_ptr = (uintptr_t)&job->record;
    job->result = xtos_bci_ioctl(job->file, XTOS_BCI_FRAME_READ,
                              (unsigned long)&job->request);
    return NULL;
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    assert(!xtos_bci_init());
    struct fixture f = {0};
    struct xtos_bci_dev *dev = setup(&f, true);
    if (!strcmp(argv[1], "read_frames")) {
        assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_START_STREAM, 0));
        emit(&f, 1); emit(&f, 3);
        struct xtos_bci_frame_v1 frames[2];
        struct xtos_bci_frame_request q = {0};
        q.version = 1; q.capacity_frames = 2; q.frames_ptr = (uintptr_t)frames;
        assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_FRAME_READ, (unsigned long)&q));
        assert(q.frame_count == 2 && q.cursor == 0 && q.next_cursor == 2);
        assert(q.payload_bytes == sizeof(frames) && q.record_bytes == 224);
        assert(q.header_bytes == 128 && !q.token && !q.mapping_bytes);
        assert(frames[0].sequence == 1 && frames[1].sequence == 2);
        assert(frames[0].samples_nv[0] == 536 && frames[1].missing_before == 1);
        assert(frames[1].quality_flags & XTOS_BCI_FRAME_QUALITY_COUNTER_GAP);
        assert(frames[0].config_generation == q.config.generation);
        assert(!xtos_bci_frame_ring_available(dev->frames));
        /* Independent legacy consumer still has both complete eight-channel frames. */
        int32_t samples[16];
        assert(xtos_bci_read(&f.file, (char *)samples, sizeof(samples), NULL) == sizeof(samples));
        assert(samples[0] == 536 && samples[8] == 536);
    } else if (!strcmp(argv[1], "frame_acquire_mmap")) {
        assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_START_STREAM, 0));
        emit(&f, 1); emit(&f, 2);
        struct xtos_bci_frame_request q = {.version=1, .capacity_frames=2};
        assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE, (unsigned long)&q));
        assert(q.frame_count==2 && q.token==1 && q.cursor==0 && q.next_cursor==2);
        assert(q.capacity_frames==2 && q.mapping_bytes==4096 && !q.frames_ptr);
        assert(q.payload_bytes==448 && q.record_bytes==224 && q.header_bytes==128);
        assert(dev->frames->read_cursor==0);
        struct vm_area_struct vma={.vm_end=4096,.vm_flags=VM_SHARED|VM_MAYWRITE};
        assert(!xtos_bci_mmap(&f.file,&vma));
        assert(!(vma.vm_flags&VM_MAYWRITE));
        struct xtos_bci_frame_snapshot *s=vma.vm_private_data;
        assert(atomic_load(&s->refs.refs)==2);
        assert(!memcmp(&q,&s->descriptor,sizeof(q)));
        const struct xtos_bci_frame_snapshot_header *h=s->mapping;
        assert(h->token==q.token && h->frame_count==2 && h->magic==XTOS_BCI_FRAME_SNAPSHOT_MAGIC);
        assert(!memcmp((char *)s->mapping+128,&q.config,sizeof(q.config)));
        const struct xtos_bci_frame_v1 *records=(void *)((char *)s->mapping+512);
        assert(records[0].sequence==1 && records[1].sequence==2);
        /* Direct host release is not a Linux VFS file-reference test. */
        teardown(&f);
        assert(atomic_load(&s->refs.refs)==1 && atomic_load(&host_mappings)==1);
        assert(records[0].sequence==1 && records[1].sequence==2);
        vma.vm_ops->close(&vma);
        assert(!atomic_load(&host_mappings));
    } else if (!strcmp(argv[1], "frame_consume")) {
        assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_START_STREAM, 0));
        emit(&f,1); emit(&f,2);
        struct xtos_bci_frame_request q={.version=1,.capacity_frames=1};
        assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q));
        struct xtos_bci_frame_consume consume={.version=1,.token=q.token,
            .cursor=q.cursor,.frame_count=q.frame_count,.stream_generation=q.stream_generation};
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_CONSUME,(unsigned long)&consume));
        assert(dev->frames->read_cursor==1 && xtos_bci_frame_ring_available(dev->frames)==1);
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_CONSUME,(unsigned long)&consume)==-ESTALE);
        consume.flags=1;
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_CONSUME,(unsigned long)&consume)==-EINVAL);
        consume.flags=0;
        openbci_remove(&f.serial);
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_CONSUME,(unsigned long)&consume)==-ENODEV);
    } else if (!strcmp(argv[1], "frame_release_detached")) {
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_START_STREAM,0));
        emit(&f,1);
        struct xtos_bci_frame_request q={.version=1,.capacity_frames=1};
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q));
        struct vm_area_struct vma={.vm_end=q.mapping_bytes,.vm_flags=VM_SHARED};
        assert(!xtos_bci_mmap(&f.file,&vma));
        struct xtos_bci_frame_snapshot *s=vma.vm_private_data;
        unsigned char saved[4096]; memcpy(saved,s->mapping,sizeof(saved));
        openbci_remove(&f.serial);
        struct xtos_bci_frame_release release={.version=1,.token=q.token+1};
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_RELEASE,(unsigned long)&release)==-ESTALE);
        release.token=q.token;
        release.flags=1;
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_RELEASE,(unsigned long)&release)==-EINVAL);
        release.flags=0;
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_RELEASE,(unsigned long)&release));
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_RELEASE,(unsigned long)&release)==-ESTALE);
        assert(!memcmp(saved,s->mapping,sizeof(saved)) && atomic_load(&s->refs.refs)==1);
        teardown(&f);
        assert(!memcmp(saved,s->mapping,sizeof(saved)));
        vma.vm_ops->close(&vma);
        assert(!atomic_load(&host_mappings));
    } else if (!strcmp(argv[1], "frame_family_tokens")) {
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_START_STREAM,0)); emit(&f,1);
        struct xtos_bci_frame_request q={.version=1,.capacity_frames=1}, request=q;
        struct xtos_bci_snapshot_request old={.version=1,.requested_bytes=32};
        struct xtos_bci_frame_release release={.version=1};
        int32_t data[8];
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q));
        assert(q.token==1);
        assert(xtos_bci_read(&f.file,(char *)data,sizeof(data),NULL)==-EBUSY);
        struct xtos_bci_frame_request read={.version=1,.capacity_frames=1,.frames_ptr=(uintptr_t)data};
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_READ,(unsigned long)&read)==-EBUSY);
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_SNAPSHOT_ACQUIRE,(unsigned long)&old)==-EBUSY);
        struct xtos_bci_snapshot_release old_release={.version=1,.token=1};
        struct xtos_bci_snapshot_consume old_consume={.version=1,.token=1,.bytes=32};
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_SNAPSHOT_CONSUME,(unsigned long)&old_consume)==-ESTALE);
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_SNAPSHOT_RELEASE,(unsigned long)&old_release)==-ESTALE);
        release.token=q.token;
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_RELEASE,(unsigned long)&release));
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_SNAPSHOT_ACQUIRE,(unsigned long)&old));
        assert(old.token==2);
        q=request;
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q)==-EBUSY);
        struct xtos_bci_frame_consume consume={.version=1,.token=2,.frame_count=1,.stream_generation=1};
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_CONSUME,(unsigned long)&consume)==-ESTALE);
        release.token=old.token;
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_RELEASE,(unsigned long)&release)==-ESTALE);
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_READ,(unsigned long)&read)==-EBUSY);
        old_release.token=old.token;
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_SNAPSHOT_RELEASE,(unsigned long)&old_release));
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q));
        assert(q.token==3);
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_RELEASE,(unsigned long)&release)==-ESTALE);
        release.token=q.token;
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_RELEASE,(unsigned long)&release));
    } else if (!strcmp(argv[1], "snapshot_wakes_legacy") || !strcmp(argv[1], "snapshot_wakes_frames")) {
        bool legacy_waiter=!strcmp(argv[1],"snapshot_wakes_legacy");
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_START_STREAM,0)); emit(&f,1);
        int32_t data[8];
        struct xtos_bci_frame_v1 record;
        if (legacy_waiter) {
            assert(xtos_bci_read(&f.file,(char *)data,sizeof(data),NULL)==sizeof(data));
        } else {
            struct xtos_bci_frame_request read={.version=1,.capacity_frames=1,.frames_ptr=(uintptr_t)&record};
            assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_READ,(unsigned long)&read));
        }
        struct read_job old_job={.file=&f.file};
        struct frame_read_job new_job={.file=&f.file};
        pthread_t thread;
        assert(!pthread_create(&thread,NULL,legacy_waiter?reading:frame_reading,legacy_waiter?(void *)&old_job:(void *)&new_job));
        flag_wait(&reader_waiting);
        wait_queue_head_t *wait=legacy_waiter?&dev->ringbuf->wait:&dev->frames->wait;
        pthread_mutex_lock(&wait->lock); unsigned int generation=wait->generation; pthread_mutex_unlock(&wait->lock);
        if (legacy_waiter) {
            struct xtos_bci_frame_request q={.version=1,.capacity_frames=1};
            assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q));
        } else {
            struct xtos_bci_snapshot_request q={.version=1,.requested_bytes=32};
            assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_SNAPSHOT_ACQUIRE,(unsigned long)&q));
        }
        pthread_mutex_lock(&wait->lock); bool woke=wait->generation>generation; pthread_mutex_unlock(&wait->lock);
        assert(woke);
        assert(!pthread_join(thread,NULL));
        assert((legacy_waiter?old_job.result:new_job.result)==-EBUSY);
    } else if (!strcmp(argv[1], "snapshot_release_wakes")) {
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_START_STREAM,0)); emit(&f,1);
        struct xtos_bci_frame_request q={.version=1,.capacity_frames=1};
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q));
        unsigned int a=dev->ringbuf->wait.generation,b=dev->frames->wait.generation;
        struct xtos_bci_frame_release release={.version=1,.token=q.token};
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_RELEASE,(unsigned long)&release));
        assert(dev->ringbuf->wait.generation>a && dev->frames->wait.generation>b);
        struct xtos_bci_snapshot_request old={.version=1,.requested_bytes=32};
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_SNAPSHOT_ACQUIRE,(unsigned long)&old));
        a=dev->ringbuf->wait.generation; b=dev->frames->wait.generation;
        struct xtos_bci_snapshot_release old_release={.version=1,.token=old.token};
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_SNAPSHOT_RELEASE,(unsigned long)&old_release));
        assert(dev->ringbuf->wait.generation>a && dev->frames->wait.generation>b);
    } else if (!strcmp(argv[1], "generic_control_wakes_frames")) {
        const struct xtos_bci_device_ops *original=dev->ops;
        struct xtos_bci_device_ops generic=*original;
        generic.stop_stream=NULL; generic.cancel=NULL; generic.cleanup=NULL;
        dev->ops=&generic;
        unsigned int a=dev->ringbuf->wait.generation,b=dev->frames->wait.generation;
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_STOP_STREAM,0)==-EOPNOTSUPP);
        assert(dev->ringbuf->wait.generation>a && dev->frames->wait.generation>b);
        a=dev->ringbuf->wait.generation; b=dev->frames->wait.generation;
        xtos_bci_unregister_device(dev);
        assert(dev->ringbuf->wait.generation>a && dev->frames->wait.generation>b);
        dev->ops=original;
    } else if (!strcmp(argv[1], "frame_only_poll")) {
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_START_STREAM,0)); emit(&f,1);
        struct xtos_bci_ringbuf *legacy=dev->ringbuf;
        dev->ringbuf=NULL;
        assert(xtos_bci_poll(&f.file,NULL)==(EPOLLIN|EPOLLRDNORM));
        WRITE_ONCE(dev->streaming,false);
        assert(xtos_bci_poll(&f.file,NULL)==(EPOLLIN|EPOLLRDNORM|EPOLLHUP));
        dev->ringbuf=legacy;
    } else if (!strcmp(argv[1], "frame_acquire_shape_empty")) {
        struct xtos_bci_frame_request q={.version=1,.capacity_frames=1}, clean=q;
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q)==-EAGAIN);
        assert(!memcmp(&q,&clean,sizeof(q)));
        for (size_t at=0;at<sizeof(q);at++) {
            if (at<4 || (at>=8 && at<12)) continue;
            ((unsigned char *)&q)[at]=1;
            struct xtos_bci_frame_request changed=q;
            assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q)==-EINVAL);
            assert(!memcmp(&q,&changed,sizeof(q)));
            q=clean;
        }
        q.version=2;
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q)==-EINVAL);
        q=clean; q.capacity_frames=0;
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q)==-EINVAL);
        q.capacity_frames=293;
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q)==-EINVAL);
        q=clean; WRITE_ONCE(dev->acquisition_error,-EIO);
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q)==-EIO);
        assert(!memcmp(&q,&clean,sizeof(q)));
    } else if (!strcmp(argv[1], "frame_acquire_copy_allocation")) {
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_START_STREAM,0)); emit(&f,1);
        struct xtos_bci_frame_request q={.version=1,.capacity_frames=1}, saved=q;
        struct xtos_bci_file *ctx=f.file.private_data;
        host_fail_from=true;
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q)==-EFAULT);
        host_fail_from=false; host_fail_kzalloc=true;
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q)==-ENOMEM);
        host_fail_kzalloc=false; fail_copy=true;
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q)==-EFAULT);
        fail_copy=false;
        assert(!ctx->frame_snapshot && ctx->next_token==1 && !dev->frames->read_cursor);
        assert(!atomic_load(&host_mappings) && !memcmp(&q,&saved,sizeof(q)));
        WRITE_ONCE(dev->streaming,false); WRITE_ONCE(dev->acquisition_error,-EIO);
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q));
        assert(q.token==1 && q.frame_count==1);
    } else if (!strcmp(argv[1], "frame_token_exhaustion")) {
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_START_STREAM,0)); emit(&f,1);
        struct xtos_bci_file *ctx=f.file.private_data; ctx->next_token=UINT64_MAX;
        struct xtos_bci_frame_request q={.version=1,.capacity_frames=1}, clean=q;
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q));
        assert(q.token==UINT64_MAX && !ctx->next_token);
        struct xtos_bci_frame_release release={.version=1,.token=q.token};
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_RELEASE,(unsigned long)&release));
        q=clean;
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q)==-EOVERFLOW);
        struct xtos_bci_snapshot_request old={.version=1,.requested_bytes=32};
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_SNAPSHOT_ACQUIRE,(unsigned long)&old)==-EOVERFLOW);
        assert(!ctx->frame_snapshot && !ctx->snapshot && !dev->frames->read_cursor);
    } else if (!strcmp(argv[1], "frame_consume_overwrite")) {
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_START_STREAM,0)); emit(&f,0);
        struct xtos_bci_frame_request q={.version=1,.capacity_frames=1};
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q));
        struct vm_area_struct vma={.vm_end=q.mapping_bytes,.vm_flags=VM_SHARED};
        assert(!xtos_bci_mmap(&f.file,&vma));
        struct xtos_bci_frame_snapshot *s=vma.vm_private_data;
        unsigned char saved[4096]; memcpy(saved,s->mapping,sizeof(saved));
        for (unsigned int i=1;i<=501;i++) emit(&f,i);
        assert(dev->frames->read_cursor==2 && dev->frames->overwritten_frames==2);
        struct xtos_bci_frame_consume consume={.version=1,.token=q.token,.cursor=q.cursor,
            .frame_count=q.frame_count,.stream_generation=q.stream_generation};
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_CONSUME,(unsigned long)&consume)==-EOVERFLOW);
        assert(!s->consumed && dev->frames->read_cursor==2 && !memcmp(saved,s->mapping,sizeof(saved)));
        struct xtos_bci_frame_release release={.version=1,.token=q.token};
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_RELEASE,(unsigned long)&release));
        vma.vm_ops->close(&vma);
        assert(!atomic_load(&host_mappings));
    } else if (!strcmp(argv[1], "frame_mmap_reopen")) {
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_START_STREAM,0)); emit(&f,1);
        struct xtos_bci_frame_request q={.version=1,.capacity_frames=1}, clean=q;
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q));
        struct vm_area_struct vma={.vm_end=q.mapping_bytes,.vm_flags=VM_SHARED|VM_WRITE};
        assert(xtos_bci_mmap(&f.file,&vma)==-EINVAL);
        vma.vm_flags=VM_READ;
        assert(xtos_bci_mmap(&f.file,&vma)==-EINVAL);
        vma.vm_flags=VM_SHARED; vma.vm_pgoff=1;
        assert(xtos_bci_mmap(&f.file,&vma)==-EINVAL);
        vma.vm_pgoff=0; vma.vm_end=8192;
        assert(xtos_bci_mmap(&f.file,&vma)==-EINVAL);
        vma.vm_end=q.mapping_bytes; host_remap_error=-ENOMEM;
        assert(xtos_bci_mmap(&f.file,&vma)==-ENOMEM);
        host_remap_error=0;
        assert(!vma.vm_private_data && !vma.vm_ops);
        assert(!xtos_bci_mmap(&f.file,&vma));
        struct xtos_bci_frame_snapshot *s=vma.vm_private_data;
        assert(!xtos_bci_release(&f.inode,&f.file));
        assert(!xtos_bci_open(&f.inode,&f.file));
        assert(!xtos_bci_frame_ring_available(dev->frames) && dev->frames->write_cursor==1);
        assert(((struct xtos_bci_frame_snapshot_header *)s->mapping)->token==q.token);
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&clean)==-EAGAIN);
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_START_STREAM,0)); emit(&f,2);
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&clean));
        assert(clean.token==1 && clean.cursor==1 && clean.stream_generation>q.stream_generation);
        vma.vm_ops->close(&vma);
    } else if (!strcmp(argv[1], "frame_errno_no_owner")) {
        struct xtos_bci_frame_release release={.version=1,.token=1};
        struct xtos_bci_frame_consume consume={.version=1,.token=1,.frame_count=1,.stream_generation=1};
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_RELEASE,(unsigned long)&release)==-ESTALE);
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_CONSUME,(unsigned long)&consume)==-ESTALE);
        release.reserved=1; consume.reserved=1;
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_RELEASE,(unsigned long)&release)==-EINVAL);
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_CONSUME,(unsigned long)&consume)==-EINVAL);
        host_fail_from=true;
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_RELEASE,(unsigned long)&release)==-EFAULT);
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_CONSUME,(unsigned long)&consume)==-EFAULT);
        host_fail_from=false;
        struct xtos_bci_frame_ring *ring=dev->frames; dev->frames=NULL;
        struct xtos_bci_frame_v1 record;
        struct xtos_bci_frame_request q={.version=1,.capacity_frames=1};
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q)==-EOPNOTSUPP);
        q.frames_ptr=(uintptr_t)&record;
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_READ,(unsigned long)&q)==-EOPNOTSUPP);
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_RELEASE,(unsigned long)&release)==-EOPNOTSUPP);
        assert(xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_CONSUME,(unsigned long)&consume)==-EOPNOTSUPP);
        dev->frames=ring;
    } else if (!strcmp(argv[1], "frame_homogeneous_config")) {
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_START_STREAM,0)); emit(&f,1);
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_STOP_STREAM,0));
        struct xtos_bci_channel_config channel={.channel=0,.enabled=1,.gain=12};
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_SET_CHANNEL,(unsigned long)&channel));
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_START_STREAM,0)); emit(&f,2);
        struct xtos_bci_frame_v1 records[2];
        struct xtos_bci_frame_request q={.version=1,.capacity_frames=2,.frames_ptr=(uintptr_t)records};
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_READ,(unsigned long)&q));
        assert(q.frame_count==1 && q.config.gain[0]==24 && records[0].sequence==1);
        u64 generation=q.config.generation,stream=q.stream_generation;
        q=(struct xtos_bci_frame_request){.version=1,.capacity_frames=2};
        assert(!xtos_bci_ioctl(&f.file,XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE,(unsigned long)&q));
        assert(q.frame_count==1 && q.cursor==1 && q.config.gain[0]==12);
        assert(q.config.generation>generation && q.stream_generation>stream);
        struct vm_area_struct vma={.vm_end=q.mapping_bytes,.vm_flags=VM_SHARED};
        assert(!xtos_bci_mmap(&f.file,&vma));
        struct xtos_bci_frame_snapshot *s=vma.vm_private_data;
        const struct xtos_bci_frame_v1 *record=(void *)((char *)s->mapping+512);
        assert(record->samples_nv[0]==1072 && record->config_generation==q.config.generation);
        vma.vm_ops->close(&vma);
    } else if (!strcmp(argv[1], "frame_pointer_boundaries")) {
        struct xtos_bci_frame_request q={.version=1,.capacity_frames=1,.frames_ptr=0xffffffffULL-224};
        host_compat_syscall=true;
        assert(!xtos_bci_frame_read_input(&q));
        q.frames_ptr++;
        assert(xtos_bci_frame_read_input(&q)==-EOVERFLOW);
        q.frames_ptr=1ULL<<32;
        assert(xtos_bci_frame_read_input(&q)==-EOVERFLOW);
        host_compat_syscall=false; q.frames_ptr=(u64)ULONG_MAX-224;
        assert(!xtos_bci_frame_read_input(&q));
        q.frames_ptr++;
        assert(xtos_bci_frame_read_input(&q)==-EOVERFLOW);
    } else if (!strcmp(argv[1], "frame_poll_independent")) {
        assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_START_STREAM, 0));
        emit(&f, 1);
        int32_t samples[8];
        assert(xtos_bci_read(&f.file, (char *)samples, sizeof(samples), NULL) == sizeof(samples));
        assert(!xtos_bci_ringbuf_available(dev->ringbuf));
        assert(xtos_bci_frame_ring_available(dev->frames));
        assert(xtos_bci_poll(&f.file, NULL) & EPOLLIN);
    } else if (!strcmp(argv[1], "compat_pointer_end")) {
        struct xtos_bci_frame_request q = {0};
        q.version = 1; q.capacity_frames = 1; q.frames_ptr = 0xfffffff0ULL;
        host_compat_syscall = true;
        assert(xtos_bci_ioctl(&f.file, XTOS_BCI_FRAME_READ, (unsigned long)&q) == -EOVERFLOW);
        host_compat_syscall = false;
    } else if (!strcmp(argv[1], "frame_empty_and_fault")) {
        struct xtos_bci_frame_v1 data;
        struct xtos_bci_frame_request q = {0}, input;
        q.version = 1; q.capacity_frames = 1; q.frames_ptr = (uintptr_t)&data;
        input = q;
        assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_FRAME_READ, (unsigned long)&q));
        assert(!memcmp(&q, &input, sizeof(q)));
        assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_START_STREAM, 0));
        q.flags = XTOS_BCI_FRAME_NONBLOCK;
        assert(xtos_bci_ioctl(&f.file, XTOS_BCI_FRAME_READ, (unsigned long)&q) == -EAGAIN);
        emit(&f, 1);
        WRITE_ONCE(dev->acquisition_error, -EIO);
        WRITE_ONCE(dev->streaming, false);
        assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_FRAME_READ, (unsigned long)&q));
        assert(q.frame_count == 1 && data.sequence == 1);
        q = input;
        assert(xtos_bci_ioctl(&f.file, XTOS_BCI_FRAME_READ, (unsigned long)&q) == -EIO);
        assert(!memcmp(&q, &input, sizeof(q)));
    } else if (!strcmp(argv[1], "frame_shape_rejection")) {
        struct xtos_bci_frame_v1 data;
        struct xtos_bci_frame_request q = {0}, saved;
        q.version = 1; q.capacity_frames = 1; q.frames_ptr = (uintptr_t)&data;
        for (size_t at = 0; at < sizeof(q); at++) {
            if (at < 12 || (at >= 16 && at < 24)) continue;
            saved = q; ((unsigned char *)&q)[at] = 1;
            struct xtos_bci_frame_request changed = q;
            assert(xtos_bci_ioctl(&f.file, XTOS_BCI_FRAME_READ, (unsigned long)&q) == -EINVAL);
            assert(!memcmp(&q, &changed, sizeof(q)));
            q = saved;
        }
        q.frames_ptr = UINT64_MAX;
        assert(xtos_bci_ioctl(&f.file, XTOS_BCI_FRAME_READ, (unsigned long)&q) == -EOVERFLOW);
        q.frames_ptr = (uintptr_t)&data; q.capacity_frames = 293;
        assert(xtos_bci_ioctl(&f.file, XTOS_BCI_FRAME_READ, (unsigned long)&q) == -EINVAL);
        q.capacity_frames = 1; q.flags = 2;
        assert(xtos_bci_ioctl(&f.file, XTOS_BCI_FRAME_READ, (unsigned long)&q) == -EINVAL);
        host_fail_from = true;
        assert(xtos_bci_ioctl(&f.file, XTOS_BCI_FRAME_READ, (unsigned long)&q) == -EFAULT);
        host_fail_from = false;
    } else if (!strcmp(argv[1], "frame_copy_failure")) {
        assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_START_STREAM, 0));
        emit(&f, 1);
        struct xtos_bci_frame_v1 data;
        struct xtos_bci_frame_request q = {0}, input;
        q.version = 1; q.capacity_frames = 1; q.frames_ptr = (uintptr_t)&data;
        input = q;
        for (unsigned int at = 1; at <= 2; at++) {
            copy_calls = 0; copy_fail_at = at;
            assert(xtos_bci_ioctl(&f.file, XTOS_BCI_FRAME_READ, (unsigned long)&q) == -EFAULT);
            assert(!dev->frames->read_cursor && !memcmp(&q, &input, sizeof(q)));
        }
        copy_fail_at = 0;
        assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_FRAME_READ, (unsigned long)&q));
        assert(q.frame_count == 1 && dev->frames->read_cursor == 1);
    } else if (!strcmp(argv[1], "frame_wait_stop") || !strcmp(argv[1], "frame_wait_unbind")) {
        assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_START_STREAM, 0));
        struct frame_read_job job = {.file = &f.file};
        pthread_t thread;
        assert(!pthread_create(&thread, NULL, frame_reading, &job));
        flag_wait(&reader_waiting);
        bool unbind = !strcmp(argv[1], "frame_wait_unbind");
        if (unbind) openbci_remove(&f.serial);
        else assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_STOP_STREAM, 0));
        assert(!pthread_join(thread, NULL));
        assert(job.result == (unbind ? -ENODEV : 0));
        if (!unbind) assert(!job.request.frame_count);
    } else if (!strcmp(argv[1], "frame_overwrite_during_copy")) {
        assert(!xtos_bci_ioctl(&f.file, XTOS_BCI_START_STREAM, 0));
        emit(&f, 0);
        struct frame_read_job job = {.file = &f.file};
        copy_pause = true;
        pthread_t thread;
        assert(!pthread_create(&thread, NULL, frame_reading, &job));
        flag_wait(&paused);
        for (unsigned int i = 1; i <= 501; i++) emit(&f, i);
        assert(dev->frames->read_cursor == 2 && dev->frames->overwritten_frames == 2);
        flag_set(&resume_thread);
        assert(!pthread_join(thread, NULL));
        assert(job.result == -EOVERFLOW);
        assert(dev->frames->read_cursor == 2 && job.record.sequence == 1);
    } else {
        assert(!"unknown frame core case");
    }
    teardown(&f);
    xtos_bci_exit();
    puts(argv[1]);
    return 0;
}
