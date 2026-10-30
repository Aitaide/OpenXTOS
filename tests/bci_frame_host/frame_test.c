/* SPDX-License-Identifier: GPL-2.0 */
#include "frame_shim.h"
#include <sys/mman.h>
#include <unistd.h>
#include <fcntl.h>
#include "../../kernel/linux/drivers/xtos/bci/xtos_bci_frame.c"
static void alloc_case(void)
{
    struct xtos_bci_frame_ring *r;
    assert(PTR_ERR(xtos_bci_frame_ring_alloc(0)) == -EINVAL);
    assert(PTR_ERR(xtos_bci_frame_ring_alloc(4097)) == -EINVAL);
    for (u32 c=1;c<=4096;c=c==1?3:4096) {
        r=xtos_bci_frame_ring_alloc(c); assert(!IS_ERR(r));
        assert(r->capacity==c && !r->read_cursor && !r->write_cursor && !r->overwritten_frames);
        xtos_bci_frame_ring_free(r); assert(!host_alloc_live);
        if(c==4096) break;
    }
    for(int i=1;i<=2;i++) {
        host_alloc_calls=0; host_alloc_fail=i;
        assert(PTR_ERR(xtos_bci_frame_ring_alloc(3))==-ENOMEM);
        assert(!host_alloc_live);
    }
    host_alloc_fail=0;
}
static struct xtos_bci_config_v1 config_fixture(void)
{
    struct xtos_bci_config_v1 c={.version=1,.channels=8,.sample_rate_hz=250,
        .enabled_mask=255,.capabilities=3,.generation=1};
    c.registers[1]=0x96;
    for(u32 i=0;i<8;i++) { c.gain[i]=24; c.registers[5+i]=0x60; }
    strcpy(c.firmware,"OpenBCI Cyton fixture"); return c;
}
static struct xtos_bci_frame_v1 record_fixture(void)
{
    struct xtos_bci_frame_v1 f={.version=1,.record_bytes=224,.channels=8,
        .sample_rate_hz=250,.unit=1,.host_clock=1,.quality_flags=1,
        .source_flags=2,.host_rx_ns=100,.stream_generation=1,.config_generation=1,
        .counter_bits=8,.enabled_mask=255};
    for(u32 i=0;i<8;i++) f.samples_nv[i]=(int)i;
    return f;
}
static void publish_case(void)
{
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(3);
    struct xtos_bci_frame_v1 f=record_fixture();
    struct xtos_bci_config_v1 c=config_fixture();
    int calls=host_alloc_calls;
    for(u32 i=0;i<8;i++) {
        f.device_counter=i; f.samples_nv[0]=(int)i;
        assert(!xtos_bci_frame_ring_publish(r,&f,&c));
        assert(r->write_cursor==i+1);
        assert(r->slots[i%3].record.sequence==i+1);
        assert(r->slots[i%3].record.samples_nv[0]==(int)i);
        assert(!memcmp(&r->slots[i%3].config,&c,sizeof(c)));
    }
    assert(r->read_cursor==5 && r->overwritten_frames==5);
    c.gain[0]=1; f.samples_nv[0]=-1;
    assert(r->slots[1].config.gain[0]==24 && r->slots[1].record.samples_nv[0]==7);
    assert(host_alloc_calls==calls);
    xtos_bci_frame_ring_free(r);
}
static void rejected(struct xtos_bci_frame_ring *r, struct xtos_bci_frame_v1 *f,
    struct xtos_bci_config_v1 *c, int error)
{
    struct xtos_bci_frame_slot slots[3];
    u64 rd=r->read_cursor,wr=r->write_cursor,ov=r->overwritten_frames;
    u64 stream=r->last_stream_generation,stamp=r->last_host_rx_ns;
    unsigned int wakes=r->wait.wakes;
    struct xtos_bci_config_v1 saved=r->last_config;
    memcpy(slots,r->slots,sizeof(slots));
    assert(xtos_bci_frame_ring_publish(r,f,c)==error);
    assert(rd==r->read_cursor && wr==r->write_cursor && ov==r->overwritten_frames);
    assert(stream==r->last_stream_generation && stamp==r->last_host_rx_ns && wakes==r->wait.wakes);
    assert(!memcmp(slots,r->slots,sizeof(slots)) && !memcmp(&saved,&r->last_config,sizeof(saved)));
}
static void validation_case(void)
{
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(3);
    struct xtos_bci_frame_v1 f=record_fixture(),bad;
    struct xtos_bci_config_v1 c=config_fixture(),bc;
    for(u32 i=0;i<22;i++) {
        bad=f; bc=c;
        switch(i) {
        case 0: bad.version=2; break; case 1: bad.record_bytes=0; break;
        case 2: bad.channels=9; break; case 3: bad.sample_rate_hz=500; break;
        case 4: bad.unit=0; break; case 5: bad.host_clock=2; break;
        case 6: bad.quality_flags=32; break; case 7: bad.quality_flags=0; break;
        case 8: bad.source_flags=3; break; case 9: bad.host_rx_ns=0; break;
        case 10: bad.sequence=1; break; case 11: bad.stream_generation=0; break;
        case 12: bad.config_generation=0; break; case 13: bad.counter_bits=7; break;
        case 14: bad.device_counter=256; break; case 15: bad.missing_before=1; break;
        case 16: bad.missing_before=127; bad.quality_flags|=2; break;
        case 17: bad.quality_flags|=2; break; case 18: bad.enabled_mask=0; break;
        case 19: bad.reserved[1]=1; break; case 20: bad.samples_nv[31]=1; break;
        case 21: bad.source_flags=0; break;
        }
        rejected(r,&bad,&bc,-EINVAL);
    }
    for(u32 i=0;i<17;i++) {
        bc=c;
        switch(i) {
        case 0: bc.version=0; break; case 1: bc.flags=1; break;
        case 2: bc.channels=7; break; case 3: bc.sample_rate_hz=500; break;
        case 4: bc.enabled_mask=256; break; case 5: bc.generation=2; break;
        case 6: bc.capabilities=0; break; case 7: bc.capabilities=32; break;
        case 8: bc.reserved0[1]=1; break; case 9: bc.reserved[7]=1; break;
        case 10: bc.registers[1]=5; break; case 11: bc.gain[7]=3; break;
        case 12: bc.registers[5]=0x70; break; case 13: bc.registers[5]|=0x80; break;
        case 14: memset(bc.firmware,'a',sizeof(bc.firmware)); break;
        case 15: bc.firmware[0]=0; break; case 16: bc.firmware[0]=1; break;
        }
        rejected(r,&f,&bc,-EINVAL);
    }
    f.quality_flags|=2; f.missing_before=126;
    assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    xtos_bci_frame_ring_free(r);
}
static void identity_case(void)
{
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(3);
    struct xtos_bci_frame_v1 f=record_fixture();
    struct xtos_bci_config_v1 c=config_fixture(),bc;
    assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    assert(!xtos_bci_frame_ring_publish(r,&f,&c)); /* equal callback time */
    bc=c; bc.firmware[0]='X'; rejected(r,&f,&bc,-EINVAL);
    f.host_rx_ns--; rejected(r,&f,&c,-EINVAL); f.host_rx_ns++;
    c.generation=2; f.config_generation=2;
    assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    c.generation=1; f.config_generation=1; rejected(r,&f,&c,-EINVAL);
    c.generation=2; f.config_generation=2; f.stream_generation=2; f.host_rx_ns=1;
    assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    f.stream_generation=1; rejected(r,&f,&c,-EINVAL);
    xtos_bci_frame_ring_free(r);
}
static void exhaustion_case(void)
{
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(3);
    struct xtos_bci_frame_v1 f=record_fixture(); struct xtos_bci_config_v1 c=config_fixture();
    r->read_cursor=U64_MAX-1; r->write_cursor=U64_MAX-1;
    assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    assert(r->slots[(U64_MAX-1)%3].record.sequence==U64_MAX);
    rejected(r,&f,&c,-EOVERFLOW);
    r->read_cursor=0; r->write_cursor=3; r->overwritten_frames=U64_MAX;
    rejected(r,&f,&c,-EOVERFLOW);
    r->read_cursor=4; r->write_cursor=3; rejected(r,&f,&c,-EOVERFLOW);
    xtos_bci_frame_ring_free(r);
}
static void peek_case(void)
{
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(3);
    struct xtos_bci_frame_v1 f=record_fixture(),out[4],saved[4];
    struct xtos_bci_frame_request q,old;
    struct xtos_bci_config_v1 c=config_fixture();
    memset(&q,0xa5,sizeof(q)); old=q;
    memset(out,0xa5,sizeof(out)); memcpy(saved,out,sizeof(out));
    assert(xtos_bci_frame_ring_peek(r,3,out,&q)==-EAGAIN);
    assert(!memcmp(&q,&old,sizeof(q)) && !memcmp(out,saved,sizeof(out)));
    assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    c.generation=2; f.config_generation=2;
    assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    assert(!xtos_bci_frame_ring_peek(r,3,out,&q));
    assert(q.cursor==0 && q.next_cursor==2 && q.frame_count==2 && q.capacity_frames==3);
    assert(q.version==1 && q.record_bytes==224 && q.header_bytes==128 && q.payload_bytes==448);
    assert(!q.flags && !q.frames_ptr && !q.token && !q.mapping_bytes && !q.overwritten_frames);
    assert(q.stream_generation==1 && q.config.generation==1);
    for(u32 i=0;i<10;i++) assert(!q.reserved[i]);
    assert(out[0].sequence==1 && out[1].sequence==2 && !memcmp(&out[2],&saved[2],sizeof(out[2])));
    for(u32 i=0;i<3;i++) {
        old=q; memcpy(saved,out,sizeof(out));
        assert(xtos_bci_frame_ring_peek(r,i==0?0:i==1?293:UINT32_MAX,out,&q)==-EINVAL);
        assert(!memcmp(&q,&old,sizeof(q)) && !memcmp(out,saved,sizeof(out)));
    }
    assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    assert(!xtos_bci_frame_ring_peek(r,3,out,&q));
    assert(q.cursor==1 && q.frame_count==1 && q.overwritten_frames==1 && out[0].sequence==2);
    assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    assert(!xtos_bci_frame_ring_peek(r,3,out,&q));
    assert(q.cursor==2 && q.frame_count==3 && q.overwritten_frames==2 && q.config.generation==2);
    xtos_bci_frame_ring_free(r);
}
static void commit_case(void)
{
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(3);
    struct xtos_bci_frame_v1 f=record_fixture(),out[3];
    struct xtos_bci_config_v1 c=config_fixture(); struct xtos_bci_frame_request q;
    assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    assert(!xtos_bci_frame_ring_peek(r,3,out,&q));
    c.generation++; f.config_generation++;
    assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    assert(xtos_bci_frame_ring_commit(r,0,2)==-EINVAL && !r->read_cursor);
    assert(xtos_bci_frame_ring_commit(r,1,1)==-EOVERFLOW && !r->read_cursor);
    assert(xtos_bci_frame_ring_commit(r,0,0)==-EINVAL && !r->read_cursor);
    assert(!xtos_bci_frame_ring_commit(r,q.cursor,q.frame_count));
    assert(r->read_cursor==1 && !r->overwritten_frames);
    assert(xtos_bci_frame_ring_commit(r,0,1)==-EOVERFLOW);
    assert(!xtos_bci_frame_ring_peek(r,3,out,&q));
    for(u32 i=0;i<3;i++) assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    assert(xtos_bci_frame_ring_commit(r,q.cursor,q.frame_count)==-EOVERFLOW);
    assert(r->read_cursor==2 && r->overwritten_frames==1);
    assert(xtos_bci_frame_ring_commit(r,2,4)==-EINVAL);
    assert(!xtos_bci_frame_ring_commit(r,2,3));
    assert(r->read_cursor==r->write_cursor && r->overwritten_frames==1);
    r->read_cursor=U64_MAX-1; r->write_cursor=U64_MAX;
    assert(xtos_bci_frame_ring_commit(r,U64_MAX-1,2)==-EOVERFLOW);
    xtos_bci_frame_ring_free(r);
}
struct wake_args { struct xtos_bci_frame_ring *ring; atomic_bool ready; };
static void *waiter(void *arg)
{
    struct wake_args *a=arg; struct timespec deadline;
    assert(!clock_gettime(CLOCK_REALTIME,&deadline)); deadline.tv_sec+=2;
    assert(!pthread_mutex_lock(&a->ring->wait.lock));
    unsigned int before=a->ring->wait.wakes;
    atomic_store(&a->ready,true);
    while(a->ring->wait.wakes==before)
        assert(!pthread_cond_timedwait(&a->ring->wait.cond,&a->ring->wait.lock,&deadline));
    assert(!pthread_mutex_unlock(&a->ring->wait.lock)); return NULL;
}
static void discard_wake_case(void)
{
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(3);
    struct xtos_bci_frame_v1 f=record_fixture(); struct xtos_bci_config_v1 c=config_fixture();
    assert(!xtos_bci_frame_ring_available(r));
    for(u32 i=0;i<4;i++) assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    assert(xtos_bci_frame_ring_available(r));
    xtos_bci_frame_ring_discard_unread(r);
    assert(!xtos_bci_frame_ring_available(r) && r->read_cursor==4 && r->overwritten_frames==1);
    c.firmware[0]='X'; rejected(r,&f,&c,-EINVAL); c.firmware[0]='O';
    f.host_rx_ns--; rejected(r,&f,&c,-EINVAL); f.host_rx_ns++;
    struct wake_args a={.ring=r}; atomic_init(&a.ready,false);
    pthread_t t; assert(!pthread_create(&t,NULL,waiter,&a));
    struct timespec deadline,now; assert(!clock_gettime(CLOCK_MONOTONIC,&deadline)); deadline.tv_sec+=1;
    while(!atomic_load(&a.ready)) { assert(!clock_gettime(CLOCK_MONOTONIC,&now)); assert(now.tv_sec<=deadline.tv_sec); }
    xtos_bci_frame_ring_wake(r); assert(!pthread_join(t,NULL));
    assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    assert(r->write_cursor==5 && r->slots[4%3].record.sequence==5);
    xtos_bci_frame_ring_free(r);
}
struct concurrent_args { struct xtos_bci_frame_ring *ring; atomic_bool done; atomic_uint captures; };
static void *producer(void *arg)
{
    struct concurrent_args *a=arg; struct xtos_bci_frame_v1 f=record_fixture();
    struct xtos_bci_config_v1 c=config_fixture();
    for(u32 i=0;i<20000;i++) {
        c.generation=1+i/7; f.config_generation=c.generation;
        f.stream_generation=1+i/101; f.host_rx_ns=100+i;
        f.device_counter=i%256; c.firmware[0]=(char)('A'+i/7%26);
        for(u32 j=0;j<8;j++) f.samples_nv[j]=(int)c.generation;
        assert(!xtos_bci_frame_ring_publish(a->ring,&f,&c));
    }
    atomic_store(&a->done,true); return NULL;
}
static void concurrent_case(void)
{
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(3);
    struct concurrent_args a={.ring=r}; atomic_init(&a.done,false); atomic_init(&a.captures,0);
    pthread_t t; assert(!pthread_create(&t,NULL,producer,&a));
    struct xtos_bci_frame_v1 out[3]; struct xtos_bci_frame_request q;
    u64 consumed=0,last=0; unsigned int attempts=0;
    while(!atomic_load(&a.done) || xtos_bci_frame_ring_available(r)) {
        assert(++attempts<10000000);
        int rc=xtos_bci_frame_ring_peek(r,3,out,&q);
        if(rc==-EAGAIN) continue;
        assert(!rc && q.frame_count && q.frame_count<=3);
        assert(q.next_cursor==q.cursor+q.frame_count);
        for(u32 i=0;i<q.frame_count;i++) {
            assert(out[i].sequence==q.cursor+i+1 && out[i].sequence>last);
            assert(out[i].stream_generation==q.stream_generation);
            assert(out[i].config_generation==q.config.generation);
            assert(out[i].host_rx_ns==99+out[i].sequence);
            assert(q.config.firmware[0]==(char)('A'+(q.config.generation-1)%26));
            for(u32 j=0;j<8;j++) assert(out[i].samples_nv[j]==(int)q.config.generation);
        }
        atomic_fetch_add(&a.captures,1);
        rc=xtos_bci_frame_ring_commit(r,q.cursor,q.frame_count);
        assert(!rc || rc==-EOVERFLOW);
        if(!rc) { consumed+=q.frame_count; last=q.next_cursor; }
    }
    assert(!pthread_join(t,NULL));
    assert(atomic_load(&a.captures) && r->write_cursor==20000);
    assert(r->read_cursor==20000 && r->overwritten_frames+consumed==20000);
    xtos_bci_frame_ring_free(r);
}
static void boundaries_case(void)
{
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(4096);
    struct xtos_bci_frame_v1 f=record_fixture(),out[292];
    struct xtos_bci_config_v1 c=config_fixture(); struct xtos_bci_frame_request q;
    for(u32 i=0;i<293;i++) assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    assert(!xtos_bci_frame_ring_peek(r,292,out,&q) && q.frame_count==292 && q.payload_bytes==65408);
    assert(!xtos_bci_frame_ring_commit(r,q.cursor,q.frame_count));
    f.source_flags=1; assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    f.stream_generation=2; assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    assert(!xtos_bci_frame_ring_peek(r,292,out,&q) && q.frame_count==1 && out[0].source_flags==2);
    assert(!xtos_bci_frame_ring_commit(r,q.cursor,q.frame_count));
    assert(!xtos_bci_frame_ring_peek(r,292,out,&q) && q.frame_count==1 && out[0].stream_generation==1);
    assert(!xtos_bci_frame_ring_commit(r,q.cursor,q.frame_count));
    assert(!xtos_bci_frame_ring_peek(r,292,out,&q) && q.frame_count==1 && out[0].stream_generation==2);
    assert(xtos_bci_frame_ring_publish(NULL,&f,&c)==-EINVAL);
    assert(xtos_bci_frame_ring_publish(r,NULL,&c)==-EINVAL);
    assert(xtos_bci_frame_ring_publish(r,&f,NULL)==-EINVAL);
    assert(xtos_bci_frame_ring_peek(r,1,NULL,&q)==-EINVAL);
    assert(xtos_bci_frame_ring_peek(r,1,out,NULL)==-EINVAL);
    xtos_bci_frame_ring_free(r);
}
static void readonly_case(void)
{
    long pagesize=sysconf(_SC_PAGESIZE); assert(pagesize>0);
    int fd=open("/dev/zero",O_RDWR); assert(fd>=0);
    void *memory=mmap(NULL,(size_t)pagesize,PROT_READ|PROT_WRITE,MAP_PRIVATE,fd,0);
    assert(memory!=MAP_FAILED); assert(!close(fd));
    struct xtos_bci_frame_v1 *f=memory;
    struct xtos_bci_config_v1 *c=(void *)((char *)memory+224);
    *f=record_fixture(); *c=config_fixture();
    assert(!mprotect(memory,(size_t)pagesize,PROT_READ));
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(3);
    assert(!xtos_bci_frame_ring_publish(r,f,c));
    assert(!munmap(memory,(size_t)pagesize));
    struct xtos_bci_frame_v1 out[3]; struct xtos_bci_frame_request q,saved;
    assert(!xtos_bci_frame_ring_peek(r,3,out,&q));
    assert(!strcmp(q.config.firmware,"OpenBCI Cyton fixture") && out[0].sequence==1);
    saved=q; r->read_cursor=2;
    assert(xtos_bci_frame_ring_peek(r,3,out,&q)==-EOVERFLOW && !memcmp(&q,&saved,sizeof(q)));
    xtos_bci_frame_ring_free(r);
}
static void profiles_case(void)
{
    static const u32 gains[]={1,2,4,6,8,12,24};
    struct xtos_bci_frame_ring *r=xtos_bci_frame_ring_alloc(3);
    struct xtos_bci_frame_v1 f=record_fixture(),out[3];
    struct xtos_bci_config_v1 c=config_fixture(); struct xtos_bci_frame_request q;
    for(u32 i=0;i<7;i++) {
        c.generation=i+1; f.config_generation=i+1;
        for(u32 j=0;j<8;j++) { c.gain[j]=gains[i]; c.registers[5+j]=(unsigned char)(i<<4); }
        f.host_rx_ns=100+i;
        assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    }
    c.generation++; f.config_generation++; f.host_rx_ns--;
    rejected(r,&f,&c,-EINVAL); f.host_rx_ns++;
    c.enabled_mask=0; f.enabled_mask=0;
    for(u32 j=0;j<8;j++) c.registers[5+j]|=0x80;
    assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    xtos_bci_frame_ring_discard_unread(r);
    c.generation=U64_MAX; f.config_generation=U64_MAX; f.stream_generation=U64_MAX;
    assert(!xtos_bci_frame_ring_publish(r,&f,&c));
    assert(!xtos_bci_frame_ring_peek(r,3,out,&q) && q.config.generation==U64_MAX);
    assert(!xtos_bci_frame_ring_commit(r,q.cursor,q.frame_count));
    c.generation=1; f.config_generation=1; rejected(r,&f,&c,-EINVAL);
    xtos_bci_frame_ring_free(r);
}
int main(int argc,char **argv)
{
    assert(argc==2);
    if(!strcmp(argv[1],"alloc")) alloc_case();
    else if(!strcmp(argv[1],"publish")) publish_case();
    else if(!strcmp(argv[1],"validation")) validation_case();
    else if(!strcmp(argv[1],"identity")) identity_case();
    else if(!strcmp(argv[1],"exhaustion")) exhaustion_case();
    else if(!strcmp(argv[1],"peek")) peek_case();
    else if(!strcmp(argv[1],"commit")) commit_case();
    else if(!strcmp(argv[1],"discard_wake")) discard_wake_case();
    else if(!strcmp(argv[1],"concurrent")) concurrent_case();
    else if(!strcmp(argv[1],"boundaries")) boundaries_case();
    else if(!strcmp(argv[1],"readonly")) readonly_case();
    else if(!strcmp(argv[1],"profiles")) profiles_case();
    else abort();
    puts(argv[1]); return 0;
}
