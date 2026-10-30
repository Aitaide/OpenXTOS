#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include "xtos/bci.h"
#include "xtos/bci_uapi.h"

static int cases, read_error, acquire_error, consume_error, empty, active, consumed;
static int positive_profile;
static int mutate_kind;
static size_t mutate_offset;
static uint64_t mutate_mask = 1;
static void mutate(void *p) { unsigned char *b=p; for (size_t i=0;i<8;i++) if ((mutate_mask>>(8*i))&255) b[mutate_offset+i]^=(unsigned char)(mutate_mask>>(8*i)); }
static int map_error, unmap_error, release_error, null_mapping, stop_error, legacy_mode;
static int maps, unmaps, releases, reads, closes, allocation_watch, allocations;
static void *live;
static size_t live_size;
static uint32_t count = 2;
static int32_t payload_value = -12345;
static uint64_t cursor = 30, token = 70;
static struct xtos_bci_frame_request reply;
static struct xtos_bci_frame_v1 records[XTOS_BCI_FRAME_MAX_BATCH];
void *__real_mmap(void *, size_t, int, int, int, off_t);
int __real_munmap(void *, size_t);
/* Allocator observers delegate unchanged to libc; only OS calls are mocked. */
void *__real_malloc(size_t);
void *__real_calloc(size_t, size_t);
void *__wrap_malloc(size_t n) { if (allocation_watch) allocations++; return __real_malloc(n); }
void *__wrap_calloc(size_t n, size_t size) { if (allocation_watch) allocations++; return __real_calloc(n,size); }
static void config(struct xtos_bci_config_v1 *c)
{
    memset(c, 0, sizeof(*c)); c->version = 1; c->channels = 8;
    c->sample_rate_hz = 250; c->enabled_mask = 255; c->generation = 5;
    c->capabilities = XTOS_BCI_CONFIG_READBACK | XTOS_BCI_FIRMWARE_READBACK;
    c->registers[1] = 6; strcpy(c->firmware, "Cyton fixture");
    for (int i = 0; i < 8; i++) { c->gain[i] = 24; c->registers[5+i] = 0x60; }
}
static void make_reply(struct xtos_bci_frame_request *r, int snapshot)
{
    r->frame_count = empty ? 0 : count;
    if (empty) return;
    r->cursor = cursor; r->next_cursor = cursor + count; r->stream_generation = 8;
    r->record_bytes = 224; r->header_bytes = 128; r->payload_bytes = count * 224;
    config(&r->config);
    if (snapshot) {
        uint64_t page = (uint64_t)sysconf(_SC_PAGESIZE);
        r->token = ++token; r->mapping_bytes = ((512+r->payload_bytes+page-1)/page)*page;
    }
    memset(records, 0, sizeof(records));
    for (uint32_t i = 0; i < count; i++) {
        struct xtos_bci_frame_v1 *f = records+i;
        f->version=1; f->record_bytes=224; f->channels=8; f->sample_rate_hz=250;
        f->unit=1; f->host_clock=1; f->quality_flags=1; f->source_flags=2;
        f->host_rx_ns=100+i; f->sequence=cursor+i+1; f->stream_generation=8;
        f->config_generation=5; f->device_counter=i & 255; f->counter_bits=8;
        f->enabled_mask=255; f->samples_nv[0]=payload_value;
        if (positive_profile) {
            f->source_flags=1; f->host_rx_ns=100;
            f->quality_flags=XTOS_BCI_FRAME_QUALITY_KNOWN;
            f->missing_before=126; f->device_counter=(255+i)&255;
        }
    }
}
int __wrap_open(const char *p, int flags, ...) { assert(!strcmp(p,"/mock/bci") && flags==O_RDWR); return 42; }
int __wrap_close(int fd) { assert(fd==42 && !live_size); closes++; active=0; return 0; }
int __wrap_ioctl(int fd, unsigned long op, ...)
{
    assert(fd==42);
    if (op==XTOS_BCI_START_STREAM) return 0;
    if (op==XTOS_BCI_STOP_STREAM) { if (stop_error) { errno=stop_error; return -1; } return 0; }
    va_list ap; va_start(ap,op); void *arg=va_arg(ap,void *); va_end(ap);
    if (op==XTOS_BCI_GET_INFO) { memset(arg,0,sizeof(struct xtos_bci_info)); return 0; }
    if (op==XTOS_BCI_SNAPSHOT_ACQUIRE) {
        struct xtos_bci_snapshot_request *r=arg;
        assert(!active); active=1; legacy_mode=1;
        r->mapping_bytes=4096; r->payload_bytes=64; r->cursor=0;
        r->token=++token; r->channels=8; r->sample_rate_hz=250; return 0;
    }
    if (op==XTOS_BCI_SNAPSHOT_RELEASE) {
        assert(active && !live_size);
        if (release_error) { errno=release_error; return -1; }
        active=0; legacy_mode=0; return 0;
    }
    if (op==XTOS_BCI_FRAME_READ || op==XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE) {
        struct xtos_bci_frame_request *r=arg, expected={0};
        int snap=op==XTOS_BCI_FRAME_SNAPSHOT_ACQUIRE;
        expected.version=1; expected.capacity_frames=r->capacity_frames;
        if (!snap) { expected.flags=r->flags; expected.frames_ptr=r->frames_ptr; }
        assert(!memcmp(&expected,r,sizeof(expected)) && r->capacity_frames>=1 && r->capacity_frames<=292);
        assert(snap ? !r->frames_ptr : !!r->frames_ptr); reads++;
        if (snap && acquire_error) { errno=acquire_error; return -1; }
        make_reply(r,snap);
        if (snap) { assert(!active); active=1; consumed=0; }
        if (mutate_kind==2) mutate(records);
        if (!snap) memcpy((void *)(uintptr_t)r->frames_ptr,records,r->payload_bytes);
        if (mutate_kind==1) mutate(r);
        reply=*r;
        if (read_error) { errno=read_error; return -1; }
        return 0;
    }
    if (op==XTOS_BCI_FRAME_SNAPSHOT_CONSUME) {
        struct xtos_bci_frame_consume e={0}; e.version=1; e.token=reply.token;
        e.cursor=reply.cursor; e.frame_count=reply.frame_count; e.stream_generation=reply.stream_generation;
        assert(active && !memcmp(arg,&e,sizeof(e)));
        if (consume_error) { errno=consume_error; return -1; }
        if (consumed) { errno=ESTALE; return -1; } consumed=1; return 0;
    }
    if (op==XTOS_BCI_FRAME_SNAPSHOT_RELEASE) {
        struct xtos_bci_frame_release e={0}; e.version=1; e.token=reply.token;
        assert(active && !live_size && !memcmp(arg,&e,sizeof(e))); releases++;
        if (release_error) { errno=release_error; return -1; } active=0; return 0;
    }
    errno=ENOTTY; return -1;
}
void *__wrap_mmap(void *addr,size_t len,int prot,int flags,int fd,off_t off)
{
    assert(!addr && len==(legacy_mode ? 4096 : reply.mapping_bytes) && prot==PROT_READ && flags==MAP_SHARED && fd==42 && !off);
    maps++; if (map_error) { errno=map_error; return MAP_FAILED; }
    live_size=len; if (null_mapping) return NULL;
    live=__real_mmap(NULL,len,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0); assert(live!=MAP_FAILED);
    struct xtos_bci_frame_snapshot_header *h=live;
    h->magic=XTOS_BCI_FRAME_SNAPSHOT_MAGIC; h->version=1; h->header_bytes=128; h->record_bytes=224;
    h->frame_count=reply.frame_count; h->unit=1; h->host_clock=1; h->payload_bytes=reply.payload_bytes;
    h->cursor=reply.cursor; h->next_cursor=reply.next_cursor; h->token=reply.token;
    h->stream_generation=reply.stream_generation; h->config_generation=reply.config.generation;
    h->overwritten_frames=reply.overwritten_frames; h->mapping_bytes=reply.mapping_bytes;
    h->config_bytes=384; h->records_offset=512;
    memcpy((char *)live+128,&reply.config,384); memcpy((char *)live+512,records,reply.payload_bytes);
    if (mutate_kind==3) mutate(live);
    if (mutate_kind==4) mutate((char *)live+128);
    if (mutate_kind==5) ((unsigned char *)live)[512+reply.payload_bytes]=1;
    if (mutate_kind==6) mutate((char *)live+512);
    assert(!mprotect(live,len,PROT_READ)); return live;
}
int __wrap_munmap(void *p,size_t len)
{
    assert(p==live && len==live_size); unmaps++;
    if (unmap_error) { errno=unmap_error; return -1; }
    if (p) assert(!__real_munmap(p,len));
    live=NULL; live_size=0; return 0;
}
static xtos_bci_session_t *create(void) { xtos_bci_session_t *s=xtos_bci_session_create("/mock/bci"); assert(s); return s; }
static void read_success(void)
{
    xtos_bci_session_t *s=create(); struct xtos_bci_frame_batch b;
    allocation_watch=1; assert(!xtos_bci_frame_read(s,2,XTOS_BCI_FRAME_NONBLOCK,&b)); allocation_watch=0;
    assert(!allocations && b.frame_count==2 && b.cursor==30 && b.next_cursor==32);
    assert(b.config.generation==5 && b.records[1].sequence==32 && b.records[0].samples_nv[0]==-12345);
    xtos_bci_session_destroy(s); cases++;
}
static void read_failure(int kind, size_t offset, uint64_t mask, int error)
{
    xtos_bci_session_t *s=create(); struct xtos_bci_frame_batch b, saved;
    assert(!xtos_bci_frame_read(s,2,0,&b)); saved=b;
    struct xtos_bci_frame_v1 bytes[2]; memcpy(bytes,b.records,sizeof(bytes));
    mutate_kind=kind; mutate_offset=offset; mutate_mask=mask; read_error=error;
    payload_value=98765; allocation_watch=1;
    int ret=xtos_bci_frame_read(s,2,0,&b);
    allocation_watch=0; payload_value=-12345; assert(!allocations);
    if (ret!=-1) fprintf(stderr,"accepted kind=%d offset=%zu mask=%llu\n",kind,offset,(unsigned long long)mask);
    assert(ret==-1);
    assert(errno==(error ? error : EPROTO));
    assert(!memcmp(&b,&saved,sizeof(b)) && !memcmp(bytes,saved.records,sizeof(bytes)));
    mutate_kind=0; read_error=0; xtos_bci_session_destroy(s); cases++;
}
static void snapshot_success(void)
{
    xtos_bci_session_t *s=create(); struct xtos_bci_frame_snapshot v;
    assert(!xtos_bci_frame_snapshot_acquire(s,2,&v));
    assert(v.mapping==live && v.header->frame_count==2 && v.batch.records[0].samples_nv[0]==-12345);
    struct xtos_bci_frame_batch b;
    assert(xtos_bci_frame_read(s,2,0,&b)==-1 && errno==EBUSY);
    float old;
    assert(xtos_bci_read_samples(s,&old,1)==-1 && errno==EBUSY);
    struct xtos_bci_snapshot legacy;
    assert(xtos_bci_snapshot_acquire(s,64,&legacy)==-1 && errno==EBUSY);
    assert(!xtos_bci_frame_snapshot_consume(s));
    assert(xtos_bci_frame_snapshot_consume(s)==-1 && errno==ESTALE);
    assert(v.batch.records[0].sequence==31);
    assert(!xtos_bci_frame_snapshot_release(s));
    assert(!live_size && !active); xtos_bci_session_destroy(s); cases++;
}
static void snapshot_failure(int kind, size_t offset, uint64_t mask, int error)
{
    xtos_bci_session_t *s=create(); struct xtos_bci_frame_snapshot v, saved;
    struct xtos_bci_frame_batch prior, prior_saved;
    struct xtos_bci_frame_v1 bytes[2];
    assert(!xtos_bci_frame_read(s,2,0,&prior)); prior_saved=prior;
    memcpy(bytes,prior.records,sizeof(bytes));
    memset(&v,0xa5,sizeof(v)); saved=v;
    mutate_kind=kind; mutate_offset=offset; mutate_mask=mask;
    int map_before=maps;
    int ret=xtos_bci_frame_snapshot_acquire(s,2,&v);
    if (ret!=-1 || errno!=error) fprintf(stderr,"snapshot kind=%d offset=%zu mask=%llu ret=%d errno=%d expected=%d\n",kind,offset,(unsigned long long)mask,ret,errno,error);
    assert(ret==-1 && errno==error);
    if (kind==1 && offset!=56) assert(maps==map_before);
    assert(!memcmp(&prior,&prior_saved,sizeof(prior)) && !memcmp(bytes,prior.records,sizeof(bytes)));
    assert(!memcmp(&v,&saved,sizeof(v)) && !live_size && !active);
    mutate_kind=0; xtos_bci_session_destroy(s); cases++;
}
static void boundaries(void)
{
    xtos_bci_session_t *s=create(); struct xtos_bci_frame_batch b;
    assert(xtos_bci_frame_read(NULL,2,0,&b)==-1 && errno==EINVAL);
    assert(xtos_bci_frame_read(s,0,0,&b)==-1 && errno==EINVAL);
    assert(xtos_bci_frame_read(s,293,0,&b)==-1 && errno==EINVAL);
    assert(xtos_bci_frame_read(s,2,2,&b)==-1 && errno==EINVAL);
    assert(xtos_bci_frame_read(s,2,0,NULL)==-1 && errno==EINVAL);
    assert(xtos_bci_frame_snapshot_acquire(s,0,NULL)==-1 && errno==EINVAL);
    assert(xtos_bci_frame_snapshot_consume(NULL)==-1 && errno==EINVAL);
    assert(xtos_bci_frame_snapshot_release(NULL)==-1 && errno==EINVAL);
    assert(xtos_bci_frame_snapshot_consume(s)==-1 && errno==ENOENT);
    assert(!xtos_bci_frame_snapshot_release(s));
    count=292; cursor=UINT64_MAX-292;
    assert(!xtos_bci_frame_read(s,292,0,&b));
    assert(b.frame_count==292 && b.records[291].sequence==UINT64_MAX);
    struct xtos_bci_frame_snapshot v;
    assert(!xtos_bci_frame_snapshot_acquire(s,292,&v));
    assert(v.batch.records[291].sequence==UINT64_MAX); assert(!xtos_bci_frame_snapshot_release(s));
    count=2; cursor=30; empty=1;
    assert(!xtos_bci_frame_read(s,2,0,&b) && !b.frame_count && !b.config.generation);
    empty=0; xtos_bci_session_destroy(s); cases++;
}
static void retries(void)
{
    xtos_bci_session_t *s=create(); struct xtos_bci_frame_snapshot v, saved;
    memset(&v,0xa5,sizeof(v)); saved=v;
    null_mapping=1;
    assert(xtos_bci_frame_snapshot_acquire(s,2,&v)==-1 && errno==EPROTO);
    assert(!memcmp(&v,&saved,sizeof(v)) && !live_size && !active); null_mapping=0;
    map_error=ENOMEM; release_error=EINTR;
    assert(xtos_bci_frame_snapshot_acquire(s,2,&v)==-1 && errno==ENOMEM);
    assert(active && !live_size);
    assert(xtos_bci_frame_snapshot_acquire(s,2,&v)==-1 && errno==EBUSY);
    struct xtos_bci_frame_batch b;
    assert(xtos_bci_frame_read(s,2,0,&b)==-1 && errno==EBUSY);
    release_error=0; assert(!xtos_bci_frame_snapshot_release(s)); map_error=0;
    assert(!xtos_bci_frame_snapshot_acquire(s,2,&v));
    unmap_error=EINTR; int prior=releases;
    assert(xtos_bci_frame_snapshot_release(s)==-1 && errno==EINTR);
    assert(releases==prior && live_size && v.batch.records[0].sequence==31);
    unmap_error=0; release_error=ESTALE;
    assert(xtos_bci_frame_snapshot_release(s)==-1 && errno==ESTALE);
    assert(active && !live_size);
    assert(xtos_bci_frame_snapshot_consume(s)==-1 && errno==ENOENT);
    release_error=0; assert(!xtos_bci_frame_snapshot_release(s));
    mutate_kind=3; mutate_offset=0; mutate_mask=1; unmap_error=EIO;
    assert(xtos_bci_frame_snapshot_acquire(s,2,&v)==-1 && errno==EPROTO);
    assert(active && live_size); mutate_kind=0; unmap_error=0;
    assert(!xtos_bci_frame_snapshot_release(s));
    assert(!xtos_bci_start(s)); assert(!xtos_bci_frame_snapshot_acquire(s,2,&v));
    stop_error=EIO; xtos_bci_session_destroy(s); stop_error=0;
    assert(!active && !live_size); cases++;
}
static void old_family(void)
{
    xtos_bci_session_t *s=create(); struct xtos_bci_snapshot old;
    struct xtos_bci_frame_snapshot v; struct xtos_bci_frame_batch b;
    /* Failed legacy mapping retains a token when release temporarily fails. */
    map_error=ENOMEM; release_error=EINTR;
    assert(xtos_bci_snapshot_acquire(s,64,&old)==-1 && errno==ENOMEM);
    assert(xtos_bci_frame_snapshot_acquire(s,2,&v)==-1 && errno==EBUSY);
    assert(xtos_bci_frame_read(s,2,0,&b)==-1 && errno==EBUSY);
    release_error=0; assert(!xtos_bci_snapshot_release(s)); map_error=0;
    assert(!xtos_bci_frame_snapshot_acquire(s,2,&v));
    memset(&old,0xa5,sizeof(old));
    assert(xtos_bci_snapshot_acquire(s,64,&old)==-1 && errno==EBUSY);
    struct xtos_bci_snapshot zero={0}; assert(!memcmp(&old,&zero,sizeof(old)));
    assert(xtos_bci_frame_read(s,2,0,&b)==-1 && errno==EBUSY);
    assert(!xtos_bci_frame_snapshot_release(s)); xtos_bci_session_destroy(s); cases++;
}
static void extra_boundaries(void)
{
    xtos_bci_session_t *s=create(); struct xtos_bci_frame_batch b;
    positive_profile=1;
    assert(!xtos_bci_frame_read(s,2,0,&b) && b.records[1].host_rx_ns==100);
    positive_profile=0;
    struct xtos_bci_frame_snapshot v, saved;
    memset(&v,0xa5,sizeof(v)); saved=v;
    const int errors[]={EAGAIN,ENODEV,EOPNOTSUPP,ENOTTY};
    for (size_t i=0;i<sizeof(errors)/sizeof(*errors);i++) {
        acquire_error=errors[i]; int m=maps, r=releases;
        assert(xtos_bci_frame_snapshot_acquire(s,2,&v)==-1 && errno==errors[i]);
        assert(!memcmp(&v,&saved,sizeof(v)) && m==maps && r==releases);
    }
    acquire_error=0;
    assert(!xtos_bci_frame_snapshot_acquire(s,2,&v));
    consume_error=EOVERFLOW;
    assert(xtos_bci_frame_snapshot_consume(s)==-1 && errno==EOVERFLOW);
    consume_error=ENODEV;
    assert(xtos_bci_frame_snapshot_consume(s)==-1 && errno==ENODEV);
    assert(v.batch.records[0].samples_nv[0]==-12345); consume_error=0;
    assert(!xtos_bci_frame_snapshot_release(s));
    cursor=UINT64_MAX-1;
    struct xtos_bci_frame_batch prior=b;
    struct xtos_bci_frame_v1 bytes[2]; memcpy(bytes,b.records,sizeof(bytes));
    assert(xtos_bci_frame_read(s,2,0,&b)==-1 && errno==EPROTO);
    assert(!memcmp(&b,&prior,sizeof(b)) && !memcmp(bytes,b.records,sizeof(bytes)));
    cursor=30; xtos_bci_session_destroy(s); cases++;
}
int main(void) {
    read_success(); snapshot_success(); boundaries(); retries(); old_family(); extra_boundaries();
    const size_t snapshot_request_fields[]={0,4,8,12,16,24,32,56,64,68,72,80};
    for (size_t i=0;i<sizeof(snapshot_request_fields)/sizeof(*snapshot_request_fields);i++)
        snapshot_failure(1,snapshot_request_fields[i],1,EPROTO);
    for (size_t i=88;i<128;i+=4) snapshot_failure(1,i,1,EPROTO);
    snapshot_failure(1,72,UINT64_MAX, EOVERFLOW);
    const size_t request_fields[]={0,4,8,12,16,24,32,48,56,64,68,72,80};
    for (size_t i=0;i<sizeof(request_fields)/sizeof(*request_fields);i++) read_failure(1,request_fields[i],1,0);
    for (size_t i=88;i<128;i+=4) read_failure(1,i,1,0);
    const size_t config_fields[]={128,132,136,140,144,152,156,160,168,201,480};
    for (size_t i=0;i<sizeof(config_fields)/sizeof(*config_fields);i++) read_failure(1,config_fields[i],1,0);
    const size_t record_fields[]={0,4,8,12,16,20,24,40,48,56,68,72,76,80,88,128,220};
    for (size_t i=0;i<sizeof(record_fields)/sizeof(*record_fields);i++) {
        read_failure(2,record_fields[i],1,0);
        snapshot_failure(6,record_fields[i],1,EPROTO);
    }
    read_failure(2,28,2,0); read_failure(2,28,1,0); read_failure(2,32,100,0);
    read_failure(2,64,256,0); read_failure(2,24,32,0); read_failure(2,72,127,0);
    read_failure(2,224+32,101,0); read_failure(2,224+28,3,0);
    const int errors[]={EAGAIN,EFAULT,EOVERFLOW,ENODEV,ENOTTY,EOPNOTSUPP,EINTR};
    for (size_t i=0;i<sizeof(errors)/sizeof(*errors);i++) read_failure(0,0,1,errors[i]);
    const size_t header_fields[]={0,4,8,12,16,20,24,28,32,40,48,56,64,72,80,88,96,100};
    for (size_t i=0;i<sizeof(header_fields)/sizeof(*header_fields);i++) snapshot_failure(3,header_fields[i],1,EPROTO);
    for (size_t i=104;i<128;i+=4) snapshot_failure(3,i,1,EPROTO);
    for (size_t i=0;i<384;i++) snapshot_failure(4,i,1,EPROTO);
    snapshot_failure(5,0,1,EPROTO);
    printf("frame_sdk_cases=%d pass\n",cases); return 0;
}
