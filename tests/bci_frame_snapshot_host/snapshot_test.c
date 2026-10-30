/* SPDX-License-Identifier: GPL-2.0 */
#define XTOS_BCI_FRAME_HOST_TEST
#define XTOS_BCI_FRAME_SNAPSHOT_HOST_TEST
#include "snapshot_shim.h"
#include <sys/mman.h>
#include <unistd.h>
#include <fcntl.h>
#include "../../kernel/linux/drivers/xtos/bci/xtos_bci_frame.c"
#include "../../kernel/linux/drivers/xtos/bci/xtos_bci_frame_snapshot.c"
static void test_create(void)
{
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(3);
    struct xtos_bci_frame_snapshot *s;
    int calls=host_alloc_calls;
    assert(PTR_ERR(xtos_bci_frame_snapshot_create(r,0,1))==-EINVAL);
    assert(PTR_ERR(xtos_bci_frame_snapshot_create(r,293,1))==-EINVAL);
    assert(PTR_ERR(xtos_bci_frame_snapshot_create(r,1,0))==-EINVAL);
    assert(PTR_ERR(xtos_bci_frame_snapshot_create(NULL,1,1))==-EINVAL);
    assert(host_alloc_calls==calls);
    s=xtos_bci_frame_snapshot_create(r,1,1);
    assert(IS_ERR(s) && PTR_ERR(s)==-EAGAIN);
    assert(vm_live==0 && host_alloc_live==2);
    host_alloc_fail=host_alloc_calls+1;
    assert(PTR_ERR(xtos_bci_frame_snapshot_create(r,1,1))==-ENOMEM);
    host_alloc_fail=0; vm_fail=1;
    assert(PTR_ERR(xtos_bci_frame_snapshot_create(r,1,1))==-ENOMEM);
    vm_fail=0;
    assert(vm_live==0 && host_alloc_live==2);
    xtos_bci_frame_ring_free(r);
}
static struct xtos_bci_config_v1 fixture_config(void)
{
    struct xtos_bci_config_v1 c={.version=1,.channels=8,.sample_rate_hz=250,
        .enabled_mask=255,.capabilities=3,.generation=7};
    c.registers[1]=0x96;
    for(u32 i=0;i<8;i++) { c.gain[i]=24; c.registers[5+i]=0x60; }
    strcpy(c.firmware,"SYNTHETIC snapshot fixture");
    return c;
}
static struct xtos_bci_frame_v1 fixture_record(void)
{
    struct xtos_bci_frame_v1 f={.version=1,.record_bytes=224,.channels=8,
        .sample_rate_hz=250,.unit=1,.host_clock=1,.quality_flags=1,.source_flags=2,
        .host_rx_ns=123,.stream_generation=4,.config_generation=7,
        .counter_bits=8,.enabled_mask=255};
    for(u32 i=0;i<8;i++) f.samples_nv[i]=(int)i-4;
    return f;
}
static void publish(struct xtos_bci_frame_ring *r, u32 n)
{
    const struct xtos_bci_config_v1 c=fixture_config();
    const struct xtos_bci_frame_v1 f=fixture_record();
    for(u32 i=0;i<n;i++) assert(!xtos_bci_frame_ring_publish(r,&f,&c));
}
static void raw_drop(struct xtos_bci_frame_snapshot *s) { xtos_bci_frame_snapshot_put(s); }
static void test_publication(void)
{
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(3);
    struct xtos_bci_frame_snapshot *s;
    const struct xtos_bci_config_v1 c=fixture_config();
    struct xtos_bci_frame_request expected={.version=1,.capacity_frames=292,
        .frame_count=3,.cursor=1,.next_cursor=4,.overwritten_frames=1,.token=99,
        .stream_generation=4,.record_bytes=224,.header_bytes=128,
        .mapping_bytes=4096,.payload_bytes=672,.config=c};
    struct xtos_bci_frame_snapshot_header h={.magic=0x58424631,.version=1,
        .header_bytes=128,.record_bytes=224,.frame_count=3,.unit=1,.host_clock=1,
        .payload_bytes=672,.cursor=1,.next_cursor=4,.token=99,.stream_generation=4,
        .config_generation=7,.overwritten_frames=1,.mapping_bytes=4096,
        .config_bytes=384,.records_offset=512};
    publish(r,4); s=xtos_bci_frame_snapshot_create(r,292,99);
    assert(!IS_ERR(s));
    assert(s->mapping_bytes==4096 && vm_allocated==PAGE_ALIGN(512+292*224));
    assert(!memcmp(&s->descriptor,&expected,sizeof(expected)));
    assert(!memcmp(s->mapping,&h,sizeof(h)));
    assert(!memcmp((char *)s->mapping+128,&c,384));
    for(u32 i=0;i<3;i++) {
        struct xtos_bci_frame_v1 f=fixture_record(); f.sequence=i+2;
        assert(!memcmp((char *)s->mapping+512+i*224,&f,224));
    }
    for(size_t i=512+672;i<s->mapping_bytes;i++) assert(!((unsigned char *)s->mapping)[i]);
    assert(r->read_cursor==1 && !s->consumed);
    unsigned char saved[4096]; memcpy(saved,s->mapping,sizeof(saved));
    publish(r,5); xtos_bci_frame_ring_free(r);
    assert(!memcmp(saved,s->mapping,sizeof(saved)));
    raw_drop(s);
}
static struct xtos_bci_frame_ring *hook_ring;
static int hook_action;
static void allocation_interleave(void)
{
    if(hook_action==1) publish(hook_ring,4);
    if(hook_action==2) xtos_bci_frame_ring_discard_unread(hook_ring);
    if(hook_action==3) hook_ring->read_cursor=hook_ring->write_cursor+1;
}
static void test_allocation_interleave(void)
{
    struct xtos_bci_frame_snapshot *s;
    hook_ring=xtos_bci_frame_ring_alloc(3); allocation_hook=allocation_interleave;
    hook_action=1; s=xtos_bci_frame_snapshot_create(hook_ring,3,2);
    assert(!IS_ERR(s) && s->descriptor.cursor==1 && s->descriptor.frame_count==3);
    assert(s->descriptor.overwritten_frames==1 && hook_ring->read_cursor==1);
    raw_drop(s);
    hook_action=2; s=xtos_bci_frame_snapshot_create(hook_ring,3,3);
    assert(IS_ERR(s) && PTR_ERR(s)==-EAGAIN && host_alloc_live==2 && !vm_live);
    hook_action=3; s=xtos_bci_frame_snapshot_create(hook_ring,3,4);
    assert(IS_ERR(s) && PTR_ERR(s)==-EOVERFLOW && host_alloc_live==2 && !vm_live);
    allocation_hook=NULL; xtos_bci_frame_ring_free(hook_ring);
}
static struct xtos_bci_frame_consume consume_request(struct xtos_bci_frame_snapshot *s)
{
    return (struct xtos_bci_frame_consume){.version=1,.token=s->descriptor.token,
        .cursor=s->descriptor.cursor,.frame_count=s->descriptor.frame_count,
        .stream_generation=s->descriptor.stream_generation};
}
static void test_consume(void)
{
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(3);
    struct xtos_bci_frame_snapshot *s;
    publish(r,2); s=xtos_bci_frame_snapshot_create(r,3,123);
    assert(!IS_ERR(s));
    const struct xtos_bci_frame_consume good=consume_request(s);
    unsigned char saved[4096]; memcpy(saved,s->mapping,sizeof(saved));
    for(u32 i=0;i<10;i++) {
        struct xtos_bci_frame_consume q=good;
        int error=-EINVAL;
        switch(i) {
        case 0: q.version=2; break;
        case 1: q.flags=1; break;
        case 2: q.reserved=1; break;
        case 3: q.token=0; break;
        case 4: q.frame_count=0; break;
        case 5: q.frame_count=293; break;
        case 6: q.token++; error=-ESTALE; break;
        case 7: q.cursor++; break;
        case 8: q.frame_count--; break;
        case 9: q.stream_generation++; break;
        }
        assert(xtos_bci_frame_snapshot_consume(s,r,&q)==error);
        assert(!s->consumed && r->read_cursor==0 && !memcmp(saved,s->mapping,4096));
    }
    assert(xtos_bci_frame_snapshot_consume(NULL,r,&good)==-EINVAL);
    assert(xtos_bci_frame_snapshot_consume(s,NULL,&good)==-EINVAL);
    assert(xtos_bci_frame_snapshot_consume(s,r,NULL)==-EINVAL);
    assert(!xtos_bci_frame_snapshot_consume(s,r,&good));
    assert(s->consumed && r->read_cursor==2 && !memcmp(saved,s->mapping,4096));
    assert(xtos_bci_frame_snapshot_consume(s,r,&good)==-ESTALE);
    struct xtos_bci_frame_consume malformed=good; malformed.flags=1;
    assert(xtos_bci_frame_snapshot_consume(s,r,&malformed)==-EINVAL);
    raw_drop(s); publish(r,2); s=xtos_bci_frame_snapshot_create(r,3,124);
    const struct xtos_bci_frame_consume displaced=consume_request(s);
    publish(r,3); u64 cursor=r->read_cursor;
    assert(xtos_bci_frame_snapshot_consume(s,r,&displaced)==-EOVERFLOW);
    assert(!s->consumed && r->read_cursor==cursor);
    raw_drop(s); xtos_bci_frame_ring_free(r);
}
static void test_references(void)
{
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(1);
    publish(r,1);
    struct xtos_bci_frame_snapshot *s=xtos_bci_frame_snapshot_create(r,1,1);
    assert(!IS_ERR(s) && atomic_load(&s->refs.value)==1);
    xtos_bci_frame_snapshot_get(s);
    assert(atomic_load(&s->refs.value)==2);
    xtos_bci_frame_snapshot_put(s);
    assert(atomic_load(&s->refs.value)==1 && vm_live==1);
    xtos_bci_frame_ring_free(r);
    int frees=vm_frees;
    xtos_bci_frame_snapshot_put(s);
    assert(!vm_live && !host_alloc_live && vm_frees==frees+1);
    xtos_bci_frame_snapshot_put(NULL);
}
static pthread_mutex_t clone_lock=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t clone_cond=PTHREAD_COND_INITIALIZER;
static unsigned int clones_ready;
static bool clones_release;
static void *clone_close(void *arg)
{
    struct vm_area_struct *v=arg;
    assert(!pthread_mutex_lock(&clone_lock));
    clones_ready++;
    assert(!pthread_cond_broadcast(&clone_cond));
    while(!clones_release) assert(!pthread_cond_wait(&clone_cond,&clone_lock));
    assert(!pthread_mutex_unlock(&clone_lock));
    v->vm_ops->close(v);
    return NULL;
}
static void test_mmap(void)
{
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(3);
    publish(r,3);
    struct xtos_bci_frame_snapshot *s=xtos_bci_frame_snapshot_create(r,3,4);
    const struct vm_area_struct good={.vm_start=4096,.vm_end=8192,.vm_flags=VM_SHARED|VM_MAYWRITE};
    for(u32 i=0;i<5;i++) {
        struct vm_area_struct v=good;
        switch(i) { case 0: v.vm_pgoff=1; break; case 1: v.vm_end++; break;
        case 2: v.vm_end=v.vm_start; break; case 3: v.vm_flags|=VM_WRITE; break;
        case 4: v.vm_flags&=~VM_SHARED; break; }
        int calls=remap_calls;
        assert(xtos_bci_frame_snapshot_mmap(s,&v)==-EINVAL);
        assert(remap_calls==calls && atomic_load(&s->refs.value)==1 && !v.vm_ops);
    }
    assert(xtos_bci_frame_snapshot_mmap(NULL,(struct vm_area_struct *)&good)==-EINVAL);
    assert(xtos_bci_frame_snapshot_mmap(s,NULL)==-EINVAL);
    struct vm_area_struct v=good; remap_error=-ENOMEM;
    assert(xtos_bci_frame_snapshot_mmap(s,&v)==-ENOMEM);
    assert(!(v.vm_flags & VM_MAYWRITE) && !v.vm_ops && !v.vm_private_data);
    assert(atomic_load(&s->refs.value)==1);
    remap_error=0; v=good;
    assert(!xtos_bci_frame_snapshot_mmap(s,&v));
    assert(atomic_load(&s->refs.value)==2 && v.vm_private_data==s && v.vm_ops);
    assert(!(v.vm_flags & VM_MAYWRITE));
    struct vm_area_struct clones[8]; pthread_t threads[8];
    for(u32 i=0;i<8;i++) { clones[i]=v; clones[i].vm_ops->open(&clones[i]); }
    assert(atomic_load(&s->refs.value)==10);
    unsigned char saved[4096]; memcpy(saved,s->mapping,sizeof(saved));
    const struct xtos_bci_frame_consume q=consume_request(s);
    assert(!xtos_bci_frame_snapshot_consume(s,r,&q));
    xtos_bci_frame_snapshot_put(s); xtos_bci_frame_ring_free(r);
    assert(!memcmp(saved,s->mapping,sizeof(saved)));
    v.vm_ops->close(&v); assert(atomic_load(&s->refs.value)==8);
    int frees=vm_frees;
    for(u32 i=0;i<8;i++) assert(!pthread_create(&threads[i],NULL,clone_close,&clones[i]));
    assert(!pthread_mutex_lock(&clone_lock));
    while(clones_ready!=8) assert(!pthread_cond_wait(&clone_cond,&clone_lock));
    assert(atomic_load(&s->refs.value)==8 && vm_frees==frees);
    clones_release=true;
    assert(!pthread_cond_broadcast(&clone_cond));
    assert(!pthread_mutex_unlock(&clone_lock));
    for(u32 i=0;i<8;i++) assert(!pthread_join(threads[i],NULL));
    assert(!vm_live && !host_alloc_live && vm_frees==frees+1);
}
static void test_boundaries(void)
{
    for(u32 capacity=1;capacity<=292;capacity+=291) {
        struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(293);
        publish(r,293);
        struct xtos_bci_frame_snapshot *s=xtos_bci_frame_snapshot_create(r,capacity,UINT64_MAX);
        assert(!IS_ERR(s) && s->descriptor.frame_count==capacity && s->descriptor.cursor==0);
        assert(s->descriptor.next_cursor==capacity && s->descriptor.payload_bytes==(u64)capacity*224);
        assert(s->mapping_bytes==PAGE_ALIGN(512+(size_t)capacity*224));
        assert(s->mapping_bytes==vm_allocated);
        for(u32 i=0;i<capacity;i++) {
            const struct xtos_bci_frame_v1 *f=(const void *)((char *)s->mapping+512+i*224);
            assert(f->sequence==i+1 && f->config_generation==s->descriptor.config.generation);
        }
        for(size_t i=512+capacity*224;i<s->mapping_bytes;i++) assert(!((unsigned char *)s->mapping)[i]);
        raw_drop(s); xtos_bci_frame_ring_free(r);
    }
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(3);
    publish(r,1);
    struct xtos_bci_config_v1 c=fixture_config(); c.generation++;
    struct xtos_bci_frame_v1 f=fixture_record(); f.config_generation++; f.stream_generation++;
    assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    struct xtos_bci_frame_snapshot *s=xtos_bci_frame_snapshot_create(r,3,4);
    assert(!IS_ERR(s) && s->descriptor.frame_count==1 && s->descriptor.stream_generation==4);
    struct xtos_bci_frame_consume q=consume_request(s);
    assert(!xtos_bci_frame_snapshot_consume(s,r,&q)); raw_drop(s);
    s=xtos_bci_frame_snapshot_create(r,3,5);
    assert(!IS_ERR(s) && s->descriptor.frame_count==1 && s->descriptor.stream_generation==5);
    assert(!memcmp(&s->descriptor.config,&c,384)); raw_drop(s); xtos_bci_frame_ring_free(r);
}
static void test_readonly_inputs(void)
{
    int fd=open("/dev/zero",O_RDWR); assert(fd>=0);
    void *memory=mmap(NULL,PAGE_SIZE,PROT_READ|PROT_WRITE,MAP_PRIVATE,fd,0);
    assert(memory!=MAP_FAILED); assert(!close(fd));
    struct xtos_bci_frame_v1 f=fixture_record();
    struct xtos_bci_config_v1 c=fixture_config();
    memcpy(memory,&f,sizeof(f)); memcpy((char *)memory+224,&c,384);
    assert(!mprotect(memory,PAGE_SIZE,PROT_READ));
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(3);
    assert(!xtos_bci_frame_ring_publish(r,memory,(const void *)((char *)memory+224)));
    struct xtos_bci_frame_snapshot *s=xtos_bci_frame_snapshot_create(r,3,1);
    assert(!IS_ERR(s));
    assert(!mprotect(memory,PAGE_SIZE,PROT_READ|PROT_WRITE));
    struct xtos_bci_frame_consume q=consume_request(s); memcpy(memory,&q,sizeof(q));
    assert(!mprotect(memory,PAGE_SIZE,PROT_READ));
    assert(!xtos_bci_frame_snapshot_consume(s,r,memory));
    assert(!memcmp(memory,&q,sizeof(q)));
    assert(!munmap(memory,PAGE_SIZE)); raw_drop(s); xtos_bci_frame_ring_free(r);
}
int main(void)
{
    void (*tests[])(void)={test_create,test_publication,test_allocation_interleave,
        test_consume,test_references,test_mmap,test_boundaries,test_readonly_inputs};
    size_t count=sizeof(tests)/sizeof(tests[0]);
    for(size_t i=0;i<count;i++) { tests[i](); assert(!host_alloc_live && !vm_live); }
    printf("SNAPSHOT_CASES=%zu\n",count);
    return 0;
}
