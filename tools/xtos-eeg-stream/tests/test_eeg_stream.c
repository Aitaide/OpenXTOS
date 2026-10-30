#include <xtos/bci.h>
#include "eeg_stream.h"
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <stdarg.h>
#include <fcntl.h>
#include <unistd.h>
int __real_open(const char *,int,...);
int __real_close(int);
static int fixture_fd=-1,read_error,read_calls,close_calls;
static struct xtos_bci_frame_batch transport;
int __wrap_open(const char *path,int flags,...)
{
    if(!strcmp(path,"/dev/xtos-eeg-fixture")) {
        fixture_fd=__real_open("/dev/null",O_RDWR);return fixture_fd;
    }
    return __real_open(path,flags);
}
int __wrap_close(int fd) {if(fd==fixture_fd)close_calls++;return __real_close(fd);}
int __wrap_ioctl(int fd,unsigned long op,...)
{
    va_list ap;void *pointer;
    va_start(ap,op);pointer=va_arg(ap,void *);va_end(ap);
    if(fd!=fixture_fd || op!=XTOS_BCI_FRAME_READ){errno=ENOTTY;return -1;}
    read_calls++;
    if(read_error){errno=read_error;return -1;}
    struct xtos_bci_frame_request *r=pointer;
    if(transport.frame_count>r->capacity_frames){errno=ENOBUFS;return -1;}
    if(transport.frame_count) {
        r->frame_count=transport.frame_count;r->cursor=transport.cursor;r->next_cursor=transport.next_cursor;
        r->stream_generation=transport.stream_generation;r->config=transport.config;
        r->record_bytes=sizeof(struct xtos_bci_frame_v1);
        r->header_bytes=sizeof(struct xtos_bci_frame_snapshot_header);
        r->payload_bytes=(uint64_t)r->frame_count*sizeof(struct xtos_bci_frame_v1);
        memcpy((void *)(uintptr_t)r->frames_ptr,transport.records,(size_t)r->payload_bytes);
    }
    return 0;
}
#define CHECK(x) do { if(!(x)){fprintf(stderr,"check failed line=%d: %s\n",__LINE__,#x);return 1;} }while(0)
static struct xtos_eeg_stream stream;
static struct xtos_bci_frame_v1 records[XTOS_BCI_FRAME_MAX_BATCH];
static struct xtos_eeg_window_options options(void)
{
    return (struct xtos_eeg_window_options){.version=1,.channels=8,.sample_rate_hz=250,
        .window_frames=4,.hop_frames=2,.max_receive_age_ns=1000};
}
static struct xtos_bci_frame_batch batch(unsigned int count,unsigned int cursor)
{
    struct xtos_bci_frame_batch b={0};
    memset(records,0,sizeof(records));b.records=records;b.frame_count=count;
    b.cursor=cursor;b.next_cursor=cursor+count;b.stream_generation=2;
    b.config.version=1;b.config.channels=8;b.config.sample_rate_hz=250;
    b.config.enabled_mask=255;b.config.capabilities=3;b.config.generation=3;
    b.config.registers[1]=6;strcpy(b.config.firmware,"deterministic transport fixture");
    for (unsigned int ch=0;ch<8;ch++) {b.config.gain[ch]=24;b.config.registers[5+ch]=0x60;}
    for (unsigned int i=0;i<count;i++) {
        records[i]=(struct xtos_bci_frame_v1){.version=1,.record_bytes=224,.channels=8,
            .sample_rate_hz=250,.unit=1,.host_clock=1,.quality_flags=1,.source_flags=2,
            .host_rx_ns=100,.sequence=cursor+i+1,.stream_generation=2,.config_generation=3,
            .device_counter=(cursor+i)&255,.counter_bits=8,.enabled_mask=255};
        for (unsigned int ch=0;ch<8;ch++) records[i].samples_nv[ch]=(int32_t)(ch+1)*1000;
    }
    return b;
}
static int golden(void)
{
    struct xtos_eeg_window_options o=options();struct xtos_eeg_stream_features f,old;
    CHECK(xtos_eeg_stream_init(&stream,&o,9)==0);
    struct xtos_bci_frame_batch b=batch(4,0);
    CHECK(xtos_eeg_stream_accept_batch(&stream,&b)==0);
    memset(&f,0x5a,sizeof(f));old=f;
    CHECK(xtos_eeg_stream_features(&stream,1100,&f)==0);
    CHECK(f.barrier_id==1 && f.features.window_id==1 && f.features.session_id==9);
    CHECK(f.features.source_flags==2 && f.features.stream_generation==2 && f.features.config_generation==3);
    CHECK(f.features.first_sequence==1 && f.features.last_sequence==4 && f.features.feature_count==16);
    for(unsigned int ch=0;ch<8;ch++)CHECK(f.features.values_uv[ch]==ch+1 && f.features.values_uv[ch+8]==ch+1);
    old=f;CHECK(xtos_eeg_stream_features(&stream,100,&f)==-EAGAIN);
    CHECK(!memcmp(&f,&old,sizeof(f)));
    return 0;
}
static int malformed(void)
{
    for(unsigned int kind=0;kind<26;kind++) {
        struct xtos_eeg_window_options o=options();struct xtos_eeg_stream_features f,old;
        CHECK(xtos_eeg_stream_init(&stream,&o,9)==0);
        struct xtos_bci_frame_batch first=batch(3,0);CHECK(xtos_eeg_stream_accept_batch(&stream,&first)==0);
        struct xtos_bci_frame_batch bad=batch(1,3);
        if(kind==0)bad.next_cursor++;
        if(kind==1)bad.frame_count=293;
        if(kind==2)bad.config.version=2;
        if(kind==3)bad.config.flags=1;
        if(kind==4)bad.config.channels=7;
        if(kind==5)bad.config.sample_rate_hz=249;
        if(kind==6)bad.config.capabilities=0;
        if(kind==7)bad.config.generation=0;
        if(kind==8)bad.config.reserved[7]=1;
        if(kind==9)bad.config.registers[1]=0;
        if(kind==10)bad.config.gain[0]=12;
        if(kind==11)memset(bad.config.firmware,'x',sizeof(bad.config.firmware));
        if(kind==12)records[0].record_bytes=223;
        if(kind==13)records[0].channels=7;
        if(kind==14)records[0].unit=0;
        if(kind==15)records[0].host_clock=0;
        if(kind==16)records[0].quality_flags=32;
        if(kind==17)records[0].source_flags=3;
        if(kind==18)records[0].sequence++;
        if(kind==19)records[0].stream_generation++;
        if(kind==20)records[0].config_generation++;
        if(kind==21)records[0].counter_bits=7;
        if(kind==22)records[0].device_counter=256;
        if(kind==23)records[0].missing_before=1;
        if(kind==24)records[0].reserved[0]=1;
        if(kind==25)records[0].samples_nv[31]=1;
        CHECK(xtos_eeg_stream_accept_batch(&stream,&bad)==-EPROTO);
        CHECK(stream.barrier_id==2);
        memset(&f,0x7a,sizeof(f));old=f;
        CHECK(xtos_eeg_stream_features(&stream,100,&f)==-EAGAIN);
        CHECK(!memcmp(&f,&old,sizeof(f)));
        struct xtos_bci_frame_batch clean=batch(4,4);CHECK(xtos_eeg_stream_accept_batch(&stream,&clean)==0);
        CHECK(xtos_eeg_stream_features(&stream,100,&f)==0 && f.features.first_sequence==5);
    }
    /* A good prefix of a malformed batch must never become publishable. */
    struct xtos_eeg_window_options o=options();struct xtos_eeg_stream_features f;
    CHECK(xtos_eeg_stream_init(&stream,&o,9)==0);
    struct xtos_bci_frame_batch first=batch(3,0);CHECK(xtos_eeg_stream_accept_batch(&stream,&first)==0);
    struct xtos_bci_frame_batch tail=batch(2,3);records[1].version=2;
    CHECK(xtos_eeg_stream_accept_batch(&stream,&tail)==-EPROTO);
    CHECK(xtos_eeg_stream_features(&stream,100,&f)==-EAGAIN);
    return 0;
}
static int continuity(void)
{
    for(unsigned int kind=0;kind<8;kind++) {
        struct xtos_eeg_window_options o=options();struct xtos_eeg_stream_features f;
        CHECK(xtos_eeg_stream_init(&stream,&o,9)==0);
        struct xtos_bci_frame_batch first=batch(3,0);CHECK(xtos_eeg_stream_accept_batch(&stream,&first)==0);
        struct xtos_bci_frame_batch b=batch(1,3);
        if(kind==0) {b.stream_generation++;records[0].stream_generation++;}
        if(kind==1) {b.config.generation++;records[0].config_generation++;}
        if(kind==2) records[0].source_flags=1;
        if(kind==3) {b.config.enabled_mask=127;b.config.registers[12]|=128;records[0].enabled_mask=127;}
        if(kind==4) {b.cursor+=5;b.next_cursor+=5;records[0].sequence+=5;}
        if(kind==5) records[0].quality_flags|=XTOS_BCI_FRAME_QUALITY_RESYNC;
        if(kind==6) {b.cursor=2;b.next_cursor=3;records[0].sequence=3;}
        if(kind==7) records[0].host_rx_ns=99;
        CHECK(xtos_eeg_stream_accept_batch(&stream,&b)==0);
        CHECK(stream.barrier_id==2);
        CHECK(xtos_eeg_stream_features(&stream,100,&f)==-EAGAIN);
    }
    return 0;
}
static int freshness(void)
{
    struct xtos_eeg_window_options o=options();struct xtos_eeg_stream_features f,old;
    CHECK(xtos_eeg_stream_init(&stream,&o,9)==0);
    struct xtos_bci_frame_batch b=batch(4,0);CHECK(xtos_eeg_stream_accept_batch(&stream,&b)==0);
    memset(&f,0x4c,sizeof(f));old=f;
    CHECK(xtos_eeg_stream_features(&stream,1101,&f)==-ESTALE);
    CHECK(stream.barrier_id==2 && !memcmp(&f,&old,sizeof(f)));
    CHECK(xtos_eeg_stream_features(&stream,100,&f)==-EAGAIN);
    b=batch(4,4);CHECK(xtos_eeg_stream_accept_batch(&stream,&b)==0);
    CHECK(xtos_eeg_stream_features(&stream,100,&f)==0 && f.barrier_id==2);
    old=f;CHECK(xtos_eeg_stream_barrier(&stream)==0 && stream.barrier_id==3);
    CHECK(xtos_eeg_stream_features(&stream,100,&f)==-EAGAIN && !memcmp(&f,&old,sizeof(f)));
    b=batch(4,8);CHECK(xtos_eeg_stream_accept_batch(&stream,&b)==0);
    stream.barrier_id=UINT64_MAX;
    CHECK(xtos_eeg_stream_barrier(&stream)==-EOVERFLOW && stream.exhausted);
    CHECK(xtos_eeg_stream_accept_batch(&stream,&b)==-EOVERFLOW);
    CHECK(xtos_eeg_stream_features(&stream,100,&f)==-EOVERFLOW && !memcmp(&f,&old,sizeof(f)));
    return 0;
}
static int empty_disabled(void)
{
    struct xtos_eeg_window_options o=options();struct xtos_eeg_stream_features f;
    CHECK(xtos_eeg_stream_init(&stream,&o,9)==0);
    struct xtos_bci_frame_batch b=batch(3,0);CHECK(xtos_eeg_stream_accept_batch(&stream,&b)==0);
    struct xtos_bci_frame_batch empty={0};empty.records=records;
    CHECK(xtos_eeg_stream_accept_batch(&stream,&empty)==-ENODATA && stream.barrier_id==2);
    CHECK(xtos_eeg_stream_features(&stream,100,&f)==-EAGAIN);
    empty.stream_generation=1;
    CHECK(xtos_eeg_stream_accept_batch(&stream,&empty)==-EPROTO && stream.barrier_id==3);
    b=batch(4,4);b.config.enabled_mask=0;
    for(unsigned int ch=0;ch<8;ch++)b.config.registers[5+ch]|=128;
    for(unsigned int i=0;i<4;i++)records[i].enabled_mask=0;
    CHECK(xtos_eeg_stream_accept_batch(&stream,&b)==1 && stream.barrier_id==4);
    CHECK(xtos_eeg_stream_features(&stream,100,&f)==-EAGAIN);
    return 0;
}
static int sdk_read(void)
{
    static struct xtos_eeg_stream before;
    struct xtos_eeg_window_options o=options();struct xtos_eeg_stream_features f;
    CHECK(xtos_eeg_stream_init(&stream,&o,9)==0);
    xtos_bci_session_t *session=xtos_bci_session_create("/dev/xtos-eeg-fixture");
    CHECK(session && fixture_fd>=0 && fcntl(fixture_fd,F_GETFD)>=0);
    transport=batch(3,0);
    CHECK(xtos_eeg_stream_read(&stream,session,4)==0);
    before=stream;read_error=EAGAIN;
    CHECK(xtos_eeg_stream_read(&stream,session,4)==-EAGAIN);
    CHECK(!memcmp(&stream,&before,sizeof(stream)));
    int calls=read_calls;
    CHECK(xtos_eeg_stream_read(&stream,session,0)==-EINVAL && read_calls==calls);
    CHECK(!memcmp(&stream,&before,sizeof(stream)));
    read_error=0;transport=batch(1,3);
    CHECK(xtos_eeg_stream_read(&stream,session,4)==0);
    CHECK(xtos_eeg_stream_features(&stream,100,&f)==0 && f.features.first_sequence==1);
    int failures[]={EIO,ENODEV,EPROTO,EINTR};
    for(unsigned int i=0;i<sizeof(failures)/sizeof(failures[0]);i++) {
        uint64_t prior=stream.barrier_id;read_error=failures[i];
        CHECK(xtos_eeg_stream_read(&stream,session,4)==-failures[i]);
        CHECK(stream.barrier_id==prior+1);
        CHECK(xtos_eeg_stream_features(&stream,100,&f)==-EAGAIN);
    }
    read_error=0;memset(&transport,0,sizeof(transport));
    CHECK(xtos_eeg_stream_read(&stream,session,4)==-ENODATA);
    int fd=fixture_fd;xtos_bci_session_destroy(session);
    CHECK(close_calls==1 && fcntl(fd,F_GETFD)==-1 && errno==EBADF);
    return 0;
}
static int rebind(void)
{
    static struct xtos_eeg_stream before;
    struct xtos_eeg_window_options o=options();struct xtos_eeg_stream_features f;
    CHECK(xtos_eeg_stream_init(&stream,&o,9)==0);
    struct xtos_bci_frame_batch b=batch(4,0);CHECK(xtos_eeg_stream_accept_batch(&stream,&b)==0);
    CHECK(xtos_eeg_stream_features(&stream,100,&f)==0 && f.features.window_id==1);
    before=stream;
    CHECK(xtos_eeg_stream_rebind(&stream,0)==-EINVAL && !memcmp(&stream,&before,sizeof(stream)));
    CHECK(xtos_eeg_stream_rebind(&stream,9)==-EINVAL && !memcmp(&stream,&before,sizeof(stream)));
    CHECK(xtos_eeg_stream_rebind(&stream,8)==-EINVAL && !memcmp(&stream,&before,sizeof(stream)));
    CHECK(xtos_eeg_stream_rebind(&stream,10)==0);
    CHECK(stream.barrier_id==2 && stream.session_id==10);
    CHECK(xtos_eeg_stream_features(&stream,100,&f)==-EAGAIN);
    b=batch(4,0);CHECK(xtos_eeg_stream_accept_batch(&stream,&b)==0);
    CHECK(xtos_eeg_stream_features(&stream,100,&f)==0 && f.features.window_id==2 && f.features.session_id==10);
    return 0;
}
static int source_mask_capacity(void)
{
    struct xtos_eeg_window_options o=options();struct xtos_eeg_stream_features f;
    CHECK(xtos_eeg_stream_init(&stream,&o,9)==0);
    struct xtos_bci_frame_batch b=batch(XTOS_BCI_FRAME_MAX_BATCH,0);
    for(unsigned int i=0;i<b.frame_count;i++)records[i].source_flags=1;
    CHECK(xtos_eeg_stream_accept_batch(&stream,&b)==0);
    CHECK(xtos_eeg_stream_features(&stream,100,&f)==0);
    CHECK(f.features.first_sequence==289 && f.features.last_sequence==292 && f.features.source_flags==1);
    b=batch(4,292);b.config.enabled_mask=128;
    for(unsigned int ch=0;ch<7;ch++)b.config.registers[5+ch]|=128;
    for(unsigned int i=0;i<4;i++)records[i].enabled_mask=128;
    CHECK(xtos_eeg_stream_accept_batch(&stream,&b)==0);
    CHECK(xtos_eeg_stream_features(&stream,100,&f)==0 && f.barrier_id==2);
    CHECK(f.features.source_flags==2 && f.features.enabled_mask==128);
    for(unsigned int ch=0;ch<7;ch++)CHECK(f.features.values_uv[ch]==0 && f.features.values_uv[ch+8]==0);
    CHECK(f.features.values_uv[7]==8 && f.features.values_uv[15]==8);
    return 0;
}
static int invalid_atomic(void)
{
    static struct xtos_eeg_stream before;
    struct xtos_eeg_window_options o=options();struct xtos_eeg_stream_features f,old;
    CHECK(xtos_eeg_stream_init(&stream,&o,9)==0);before=stream;
    CHECK(xtos_eeg_stream_init(&stream,&o,0)==-EINVAL && !memcmp(&stream,&before,sizeof(stream)));
    o.channels=2;CHECK(xtos_eeg_stream_init(&stream,&o,9)==-EINVAL && !memcmp(&stream,&before,sizeof(stream)));
    CHECK(xtos_eeg_stream_accept_batch(&stream,NULL)==-EINVAL && !memcmp(&stream,&before,sizeof(stream)));
    CHECK(xtos_eeg_stream_read(&stream,NULL,4)==-EINVAL && !memcmp(&stream,&before,sizeof(stream)));
    CHECK(xtos_eeg_stream_features(&stream,100,NULL)==-EINVAL && !memcmp(&stream,&before,sizeof(stream)));
    memset(&f,0x45,sizeof(f));old=f;
    CHECK(xtos_eeg_stream_features(NULL,100,&f)==-EINVAL && !memcmp(&f,&old,sizeof(f)));
    CHECK(xtos_eeg_stream_barrier(NULL)==-EINVAL);
    CHECK(xtos_eeg_stream_rebind(NULL,10)==-EINVAL);
    CHECK(xtos_eeg_stream_accept_batch(NULL,NULL)==-EINVAL);
    CHECK(xtos_eeg_stream_init(NULL,NULL,0)==-EINVAL);
    return 0;
}
static int bad_quality(void)
{
    const uint32_t faults[]={XTOS_BCI_FRAME_QUALITY_COUNTER_GAP,XTOS_BCI_FRAME_QUALITY_RESYNC,
        XTOS_BCI_FRAME_QUALITY_ADC_RAIL,XTOS_BCI_FRAME_QUALITY_CONTINUITY_UNKNOWN};
    for(unsigned int k=0;k<sizeof(faults)/sizeof(faults[0]);k++) {
        struct xtos_eeg_window_options o=options();struct xtos_eeg_stream_features f;
        CHECK(xtos_eeg_stream_init(&stream,&o,9)==0);
        struct xtos_bci_frame_batch b=batch(3,0);CHECK(xtos_eeg_stream_accept_batch(&stream,&b)==0);
        b=batch(1,3);records[0].quality_flags|=faults[k];
        if(faults[k]==XTOS_BCI_FRAME_QUALITY_COUNTER_GAP)records[0].missing_before=1;
        CHECK(xtos_eeg_stream_accept_batch(&stream,&b)==0 && stream.barrier_id==2);
        CHECK(xtos_eeg_stream_features(&stream,100,&f)==-EAGAIN);
        b=batch(4,4);CHECK(xtos_eeg_stream_accept_batch(&stream,&b)==0);
        CHECK(xtos_eeg_stream_features(&stream,100,&f)==0 && f.features.first_sequence==5);
    }
    return 0;
}
int main(int argc,char **argv)
{
    if(argc!=2)return 2;
    if(!strcmp(argv[1],"golden"))return golden();
    if(!strcmp(argv[1],"malformed"))return malformed();
    if(!strcmp(argv[1],"continuity"))return continuity();
    if(!strcmp(argv[1],"freshness"))return freshness();
    if(!strcmp(argv[1],"empty_disabled"))return empty_disabled();
    if(!strcmp(argv[1],"sdk_read"))return sdk_read();
    if(!strcmp(argv[1],"rebind"))return rebind();
    if(!strcmp(argv[1],"source_mask_capacity"))return source_mask_capacity();
    if(!strcmp(argv[1],"invalid_atomic"))return invalid_atomic();
    if(!strcmp(argv[1],"bad_quality"))return bad_quality();
    return 2;
}
