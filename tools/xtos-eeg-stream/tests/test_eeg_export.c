#define _POSIX_C_SOURCE 200809L
#include "eeg_export.h"
#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef EXPORT_SERIALIZER_TEST
static struct xtos_eeg_stream_features golden(void)
{
    struct xtos_eeg_stream_features f={.barrier_id=1};
    f.features=(struct xtos_eeg_features){.version=1,.feature_kind=1,.feature_count=16,
        .channels=8,.sample_rate_hz=250,.source_flags=2,.enabled_mask=255,.frame_count=4,
        .window_id=1,.session_id=UINT64_MAX,.stream_generation=2,.config_generation=3,
        .first_sequence=1,.last_sequence=4,.first_host_rx_ns=100,.last_host_rx_ns=100};
    for(unsigned i=0;i<16;i++)f.features.values_uv[i]=i%8+1;
    return f;
}
int main(int argc,char **argv)
{
    if(argc!=2)return 2;
    struct xtos_eeg_stream_features f=golden();
    struct xtos_eeg_export_options o={.device="/dev/fixture",.session_id=UINT64_MAX,
        .window_frames=4,.hop_frames=2,.max_receive_age_ns=1000,.duration_ms=30,.max_windows=2};
    if(!strcmp(argv[1],"invalid")) {
        for(unsigned k=0;k<32;k++) {
            struct xtos_eeg_stream_features bad=f;
            struct xtos_eeg_export_options opt=o;
            if(k==0)bad.features.version++;
            if(k==1)bad.features.feature_kind++;
            if(k==2)bad.features.feature_count--;
            if(k==3)bad.features.channels--;
            if(k==4)bad.features.sample_rate_hz--;
            if(k==5)bad.features.source_flags=3;
            if(k==6)bad.features.enabled_mask=0;
            if(k==7)bad.features.enabled_mask=256;
            if(k==8)bad.features.frame_count--;
            if(k==9)bad.barrier_id=0;
            if(k==10)bad.features.window_id=0;
            if(k==11)bad.features.session_id=0;
            if(k==12)bad.features.stream_generation=0;
            if(k==13)bad.features.config_generation=0;
            if(k==14)bad.features.first_sequence=0;
            if(k==15)bad.features.last_sequence=3;
            if(k==16)bad.features.first_host_rx_ns=0;
            if(k==17)bad.features.last_host_rx_ns=101;
            if(k==18)bad.features.first_host_rx_ns=101;
            if(k==19)bad.features.values_uv[0]=NAN;
            if(k==20)bad.features.values_uv[15]=INFINITY;
            if(k==21)bad.features.values_uv[8]=-1;
            if(k==22){bad.features.enabled_mask=254;bad.features.values_uv[0]=1;}
            if(k==23)opt.window_frames=1;
            if(k==24)opt.window_frames=4097;
            if(k==25)opt.hop_frames=0;
            if(k==26)opt.hop_frames=5;
            if(k==27)opt.max_receive_age_ns=0;
            if(k==28)opt.session_id=1;
            if(k==29)opt.duration_ms=0;
            if(k==30)opt.max_windows=0;
            if(k==31)opt.device="relative";
            FILE *tmp=tmpfile();
            if(!tmp)return 10;
            int r=xtos_eeg_export_window(tmp,&opt,&bad,100);
            long bytes=ftell(tmp);fclose(tmp);
            if(r>=0 || bytes){fprintf(stderr,"invalid metadata k=%u returned=%d bytes=%ld\n",k,r,bytes);return 1;}
        }
        FILE *tmp=tmpfile();if(!tmp)return 10;
        int r=xtos_eeg_export_window(tmp,&o,&f,1101);
        long bytes=ftell(tmp);fclose(tmp);
        return r>=0 || bytes;
    }
    if(!strcmp(argv[1],"golden"))return xtos_eeg_export_window(stdout,&o,&f,100)!=0;
    return 2;
}
#endif
#ifndef EXPORT_SERIALIZER_TEST
#include <xtos/bci_uapi.h>
#include <fcntl.h>
#include <signal.h>
#include <stdarg.h>
#include <time.h>
#include <unistd.h>
int __real_open(const char *,int,...);
int __real_close(int);
static void trace(const char *event);
#ifdef EXPORT_DELAY_FEATURES
int __real_xtos_eeg_window_features(struct xtos_eeg_window *,uint64_t,struct xtos_eeg_features *);
#endif
static int fd=-1;
static unsigned calls,frames;
static const char *scenario(void) {const char *s=getenv("EXPORT_SCENARIO");return s?s:"complete";}
static int is(const char *s) {return !strcmp(scenario(),s);}
#ifdef EXPORT_DELAY_FEATURES
int __wrap_xtos_eeg_window_features(struct xtos_eeg_window *window,uint64_t now,struct xtos_eeg_features *out)
{
    int result=__real_xtos_eeg_window_features(window,now,out);
    if(!result && ((frames==4 && (is("feature_sigint") || is("feature_sigterm"))) ||
                   (frames==6 && (is("feature_sigint_preserve") || is("feature_sigterm_preserve"))))) {
        trace("cancel_before_publish");
        raise((is("feature_sigint") || is("feature_sigint_preserve"))?SIGINT:SIGTERM);
    }
    if(!result && is("late_features")) {
        struct timespec delay={.tv_nsec=60000000};
        while(nanosleep(&delay,&delay) && errno==EINTR) {}
    }
    return result;
}
#endif
static void trace(const char *event)
{
    const char *path=getenv("EXPORT_TRACE");
    if(!path)return;
    FILE *f=fopen(path,"a");if(f){fprintf(f,"%s\n",event);fclose(f);}
}
int __wrap_open(const char *path,int flags,...)
{
    if(!strcmp(path,"/dev/xtos-export-fixture")) {
        trace("open");fd=__real_open("/dev/null",O_RDWR);return fd;
    }
    return __real_open(path,flags);
}
int __wrap_close(int n) {if(n==fd)trace("close");return __real_close(n);}
int __wrap_ioctl(int n,unsigned long op,...)
{
    if(n!=fd){errno=ENOTTY;return -1;}
    if(op==XTOS_BCI_START_STREAM) {trace("start");if(is("start_fail")){errno=EIO;return -1;}return 0;}
    if(op==XTOS_BCI_STOP_STREAM) {trace("stop");if(is("stop_fail") || is("disconnect_stop")){errno=EIO;return -1;}return 0;}
    if(op!=XTOS_BCI_FRAME_READ){errno=ENOTTY;return -1;}
    va_list ap;va_start(ap,op);struct xtos_bci_frame_request *r=va_arg(ap,void *);va_end(ap);
    if(r->capacity_frames!=1 || r->flags!=XTOS_BCI_FRAME_NONBLOCK){errno=EINVAL;return -1;}
    calls++;
    if(is("late_read") && frames==3) {
        struct timespec delay={.tv_nsec=60000000};
        while(nanosleep(&delay,&delay) && errno==EINTR) {}
    }
    if((is("eagain") && calls==1) || is("timeout")){errno=EAGAIN;return -1;}
    if(((frames==2 && (is("sigint") || is("sigterm")))) || (frames==4 && (is("sigint_window") || is("sigterm_window")))) {
        raise((is("sigint") || is("sigint_window"))?SIGINT:SIGTERM);errno=EINTR;return -1;
    }
    if(frames==2 && (is("disconnect") || is("disconnect_stop"))){errno=ENODEV;return -1;}
    if(frames==2 && is("insufficient")){errno=EAGAIN;return -1;}
    struct timespec ts;clock_gettime(CLOCK_MONOTONIC,&ts);
    uint64_t now=(uint64_t)ts.tv_sec*1000000000u+(uint64_t)ts.tv_nsec;
    if(is("stale"))now-=2000000000u;
    unsigned seq=frames+1;if(is("gap") && frames>=2)seq+=3;
    r->frame_count=1;r->cursor=seq-1;r->next_cursor=seq;r->stream_generation=2;
    r->record_bytes=sizeof(struct xtos_bci_frame_v1);r->header_bytes=sizeof(struct xtos_bci_frame_snapshot_header);
    r->payload_bytes=sizeof(struct xtos_bci_frame_v1);
    r->config=(struct xtos_bci_config_v1){.version=1,.channels=8,.sample_rate_hz=250,.enabled_mask=255,.capabilities=3,.generation=3};
    r->config.registers[1]=6;strcpy(r->config.firmware,"export syscall fixture");
    for(unsigned ch=0;ch<8;ch++){r->config.gain[ch]=24;r->config.registers[5+ch]=0x60;}
    struct xtos_bci_frame_v1 value={.version=1,.record_bytes=sizeof(value),.channels=8,.sample_rate_hz=250,
        .unit=1,.host_clock=1,.quality_flags=1,.source_flags=is("serdev")?1:2,
        .host_rx_ns=now,.sequence=seq,.stream_generation=2,.config_generation=3,
        .device_counter=(seq-1)&255,.counter_bits=8,.enabled_mask=255};
    for(unsigned ch=0;ch<8;ch++)value.samples_nv[ch]=(int32_t)(ch+1)*1000;
    if(is("bad_tail") && frames==3)value.version=2;
    if(is("disabled")) {
        value.enabled_mask=0;r->config.enabled_mask=0;
        for(unsigned ch=0;ch<8;ch++)r->config.registers[5+ch]|=128;
    }
    if(is("mask")) {
        value.enabled_mask=128;r->config.enabled_mask=128;
        for(unsigned ch=0;ch<7;ch++)r->config.registers[5+ch]|=128;
    }
    memcpy((void *)(uintptr_t)r->frames_ptr,&value,sizeof(value));frames++;trace("frame");return 0;
}
#endif
