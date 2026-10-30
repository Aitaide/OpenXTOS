/* Portable golden-vector test; no device or inference is involved. */
#include <xtos/eeg_window.h>
#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHECK(value) do { if (!(value)) { fprintf(stderr,"check failed line=%d: %s\n",__LINE__,#value); return 1; } } while (0)

static struct xtos_eeg_window window;
static struct xtos_eeg_window_options options(void)
{
    return (struct xtos_eeg_window_options){.version=1,.channels=2,.sample_rate_hz=250,
        .window_frames=4,.hop_frames=2,.max_receive_age_ns=1000};
}
static struct xtos_eeg_sample sample(unsigned int i)
{
    return (struct xtos_eeg_sample){.version=1,.channels=2,.sample_rate_hz=250,
        .unit=XTOS_EEG_UNIT_NANOVOLT,.host_clock=XTOS_EEG_CLOCK_MONOTONIC,
        .quality_flags=XTOS_EEG_CONFIG_VERIFIED,.source_flags=XTOS_EEG_SOURCE_SYNTHETIC,
        .enabled_mask=3,.session_id=1,.stream_generation=1,.config_generation=1,
        .sequence=i+1,.host_rx_ns=100};
}
static int golden(void)
{
    struct xtos_eeg_window_options opts=options();
    struct xtos_eeg_features out,old;
    CHECK(xtos_eeg_window_init(&window,&opts)==0);
    memset(&out,0x5a,sizeof(out));old=out;
    for (unsigned int i=0;i<4;i++) {
        struct xtos_eeg_sample frame=sample(i);
        frame.samples_nv[0]=(i&1)?-3000:3000;
        frame.samples_nv[1]=(i&1)?-4000:4000;
        CHECK(xtos_eeg_window_push(&window,&frame)==XTOS_EEG_PUSH_ACCEPTED);
        if (i<3) { CHECK(xtos_eeg_window_features(&window,100,&out)==-EAGAIN); CHECK(!memcmp(&out,&old,sizeof(out))); }
    }
    CHECK(xtos_eeg_window_features(&window,1100,&out)==0);
    CHECK(out.version==1 && out.feature_count==4 && out.channels==2 && out.enabled_mask==3);
    CHECK(out.window_id==1 && out.session_id==1 && out.stream_generation==1 && out.config_generation==1);
    CHECK(out.first_sequence==1 && out.last_sequence==4 && out.frame_count==4);
    CHECK(out.first_host_rx_ns==100 && out.last_host_rx_ns==100);
    CHECK(out.source_flags==XTOS_EEG_SOURCE_SYNTHETIC);
    CHECK(out.values_uv[0]==0 && out.values_uv[1]==0 && out.values_uv[2]==3 && out.values_uv[3]==4);
    for (unsigned int i=4;i<16;i++) CHECK(out.values_uv[i]==0);
    old=out;
    CHECK(xtos_eeg_window_features(&window,100,&out)==-EAGAIN);
    CHECK(!memcmp(&out,&old,sizeof(out)));
    return 0;
}
static int boundaries(void)
{
    /* One independent identity/discontinuity mutation per fresh window. */
    for (unsigned int kind=0;kind<7;kind++) {
        struct xtos_eeg_window_options opts=options();
        struct xtos_eeg_features out,old;
        CHECK(xtos_eeg_window_init(&window,&opts)==0);
        for (unsigned int i=0;i<3;i++) { struct xtos_eeg_sample s=sample(i); CHECK(xtos_eeg_window_push(&window,&s)==0); }
        struct xtos_eeg_sample changed=sample(3);
        if (kind==0) changed.session_id++;
        if (kind==1) changed.stream_generation++;
        if (kind==2) changed.config_generation++;
        if (kind==3) changed.source_flags=XTOS_EEG_SOURCE_SERDEV;
        if (kind==4) changed.enabled_mask=1;
        if (kind==5) changed.sequence=10;
        if (kind==6) changed.host_rx_ns=101;
        if (kind==6) { /* Equal receive time is allowed; decreasing is not. */
            changed.host_rx_ns=99;
            CHECK(xtos_eeg_window_push(&window,&changed)==XTOS_EEG_PUSH_DROPPED);
        } else CHECK(xtos_eeg_window_push(&window,&changed)==XTOS_EEG_PUSH_RESET_ACCEPTED);
        memset(&out,0x6c,sizeof(out));old=out;
        CHECK(xtos_eeg_window_features(&window,100,&out)==-EAGAIN);
        CHECK(!memcmp(&out,&old,sizeof(out)));
    }
    return 0;
}
static int quality(void)
{
    uint32_t flags[]={0,XTOS_EEG_CONFIG_VERIFIED|XTOS_EEG_COUNTER_GAP,
        XTOS_EEG_CONFIG_VERIFIED|XTOS_EEG_RESYNC,XTOS_EEG_CONFIG_VERIFIED|XTOS_EEG_ADC_RAIL,
        XTOS_EEG_CONFIG_VERIFIED|XTOS_EEG_CONTINUITY_UNKNOWN,XTOS_EEG_CONFIG_VERIFIED};
    for (unsigned int kind=0;kind<sizeof(flags)/sizeof(flags[0]);kind++) {
        struct xtos_eeg_window_options opts=options();struct xtos_eeg_features out;
        CHECK(xtos_eeg_window_init(&window,&opts)==0);
        for (unsigned int i=0;i<3;i++) { struct xtos_eeg_sample s=sample(i); CHECK(xtos_eeg_window_push(&window,&s)==0); }
        struct xtos_eeg_sample bad=sample(3);bad.quality_flags=flags[kind];
        if (kind==5) bad.missing_before=1;
        CHECK(xtos_eeg_window_push(&window,&bad)==XTOS_EEG_PUSH_DROPPED);
        CHECK(xtos_eeg_window_features(&window,100,&out)==-EAGAIN);
        for (unsigned int i=4;i<8;i++) { struct xtos_eeg_sample s=sample(i); CHECK(xtos_eeg_window_push(&window,&s)>=0); }
        CHECK(xtos_eeg_window_features(&window,100,&out)==0);
        CHECK(out.first_sequence==5 && out.last_sequence==8);
    }
    return 0;
}
static int freshness(void)
{
    for (unsigned int kind=0;kind<3;kind++) {
        struct xtos_eeg_window_options opts=options();struct xtos_eeg_features out,old;
        CHECK(xtos_eeg_window_init(&window,&opts)==0);
        for (unsigned int i=0;i<4;i++) { struct xtos_eeg_sample s=sample(i); CHECK(xtos_eeg_window_push(&window,&s)==0); }
        memset(&out,0x8a,sizeof(out));old=out;
        if (kind==2) window.window_id=UINT64_MAX;
        int expected=kind==2?-EOVERFLOW:-ESTALE;
        uint64_t now=kind==0?1101:kind==1?99:100;
        CHECK(xtos_eeg_window_features(&window,now,&out)==expected);
        CHECK(!memcmp(&out,&old,sizeof(out)));
        if (kind<2) CHECK(xtos_eeg_window_features(&window,100,&out)==-EAGAIN);
    }
    return 0;
}
static int rolling(void)
{
    struct xtos_eeg_window_options opts=options();struct xtos_eeg_features out,old;
    CHECK(xtos_eeg_window_init(&window,&opts)==0);
    for (unsigned int i=0;i<10;i++) {
        struct xtos_eeg_sample s=sample(i);s.samples_nv[0]=(int32_t)(i+1)*1000;
        s.samples_nv[1]=-s.samples_nv[0];CHECK(xtos_eeg_window_push(&window,&s)==0);
        if (i==3 || i==5 || i==9) {
            CHECK(xtos_eeg_window_features(&window,100,&out)==0);
            CHECK(out.first_sequence==i-2 && out.last_sequence==i+1);
            CHECK(fabs(out.values_uv[0]-((double)i-0.5))<1e-12);
            CHECK(out.values_uv[1]==-out.values_uv[0]);
            old=out;
        } else if (i==4) {
            CHECK(xtos_eeg_window_features(&window,100,&out)==-EAGAIN);
            CHECK(!memcmp(&out,&old,sizeof(out)));
        }
    }
    CHECK(out.window_id==3);
    xtos_eeg_window_reset(&window);
    for (unsigned int i=0;i<4;i++) { struct xtos_eeg_sample s=sample(i);CHECK(xtos_eeg_window_push(&window,&s)==0); }
    CHECK(xtos_eeg_window_features(&window,100,&out)==0 && out.window_id==4);
    return 0;
}
static int bounds(void)
{
    struct xtos_eeg_window_options opts=options();struct xtos_eeg_features out;
    opts.channels=8;opts.window_frames=4096;opts.hop_frames=4096;
    CHECK(xtos_eeg_window_init(&window,&opts)==0);
    for (unsigned int i=0;i<4096;i++) {
        struct xtos_eeg_sample s=sample(i);s.channels=8;s.enabled_mask=255;
        for (unsigned int ch=0;ch<8;ch++) s.samples_nv[ch]=ch&1?INT32_MIN:INT32_MAX;
        CHECK(xtos_eeg_window_push(&window,&s)==0);
    }
    CHECK(xtos_eeg_window_features(&window,100,&out)==0 && out.feature_count==16);
    for (unsigned int ch=0;ch<8;ch++) {
        double expected=(double)(ch&1?INT32_MIN:INT32_MAX)/1000.0;
        CHECK(fabs(out.values_uv[ch]-expected)<1e-9);
        CHECK(fabs(out.values_uv[8+ch]-fabs(expected))<1e-9);
    }
    opts=options();opts.channels=1;opts.window_frames=2;opts.hop_frames=1;
    CHECK(xtos_eeg_window_init(&window,&opts)==0);
    for (unsigned int i=0;i<2;i++) { struct xtos_eeg_sample s=sample(i);s.channels=1;s.enabled_mask=1;s.sequence=UINT64_MAX-1+i;s.samples_nv[0]=1000;CHECK(xtos_eeg_window_push(&window,&s)==0); }
    CHECK(xtos_eeg_window_features(&window,100,&out)==0);
    CHECK(out.first_sequence==UINT64_MAX-1 && out.last_sequence==UINT64_MAX);
    struct xtos_eeg_sample wrap=sample(0);wrap.channels=1;wrap.enabled_mask=1;
    CHECK(xtos_eeg_window_push(&window,&wrap)==XTOS_EEG_PUSH_DROPPED);
    CHECK(xtos_eeg_window_features(&window,100,&out)==-EAGAIN);
    return 0;
}
static int invalid(void)
{
    static struct xtos_eeg_window before;
    struct xtos_eeg_window_options opts=options();struct xtos_eeg_features out,old;
    CHECK(xtos_eeg_window_init(&window,&opts)==0);before=window;
    for (unsigned int kind=0;kind<9;kind++) {
        struct xtos_eeg_window_options bad=opts;
        if (kind==0) bad.version=2;
        if (kind==1) bad.channels=0;
        if (kind==2) bad.channels=9;
        if (kind==3) bad.window_frames=1;
        if (kind==4) bad.window_frames=4097;
        if (kind==5) bad.hop_frames=0;
        if (kind==6) bad.hop_frames=5;
        if (kind==7) bad.max_receive_age_ns=0;
        if (kind==8) bad.sample_rate_hz=65536;
        CHECK(xtos_eeg_window_init(&window,&bad)==-EINVAL && !memcmp(&window,&before,sizeof(window)));
    }
    for (unsigned int kind=0;kind<12;kind++) {
        struct xtos_eeg_sample bad=sample(0);
        if (kind==0) bad.version=2;
        if (kind==1) bad.channels=0;
        if (kind==2) bad.sample_rate_hz=249;
        if (kind==3) bad.unit=0;
        if (kind==4) bad.host_clock=0;
        if (kind==5) bad.enabled_mask=4;
        if (kind==6) bad.session_id=0;
        if (kind==7) bad.stream_generation=0;
        if (kind==8) bad.config_generation=0;
        if (kind==9) bad.sequence=0;
        if (kind==10) bad.quality_flags=32;
        if (kind==11) bad.samples_nv[2]=1;
        CHECK(xtos_eeg_window_push(&window,&bad)==-EINVAL && !memcmp(&window,&before,sizeof(window)));
    }
    memset(&out,0x99,sizeof(out));old=out;
    CHECK(xtos_eeg_window_init(NULL,&opts)==-EINVAL);
    CHECK(xtos_eeg_window_push(&window,NULL)==-EINVAL);
    CHECK(xtos_eeg_window_features(NULL,0,&out)==-EINVAL && !memcmp(&out,&old,sizeof(out)));
    CHECK(xtos_eeg_window_features(&window,0,NULL)==-EINVAL);
    return 0;
}
static int duplicate(void)
{
    struct xtos_eeg_window_options opts=options();struct xtos_eeg_features out;
    CHECK(xtos_eeg_window_init(&window,&opts)==0);
    for (unsigned int i=0;i<3;i++) { struct xtos_eeg_sample s=sample(i);CHECK(xtos_eeg_window_push(&window,&s)==0); }
    struct xtos_eeg_sample duplicate=sample(2);
    CHECK(xtos_eeg_window_push(&window,&duplicate)==XTOS_EEG_PUSH_DROPPED);
    struct xtos_eeg_sample backward=sample(0);
    CHECK(xtos_eeg_window_push(&window,&backward)==XTOS_EEG_PUSH_DROPPED);
    for (unsigned int i=3;i<7;i++) { struct xtos_eeg_sample s=sample(i);CHECK(xtos_eeg_window_push(&window,&s)==0); }
    CHECK(xtos_eeg_window_features(&window,100,&out)==0);
    CHECK(out.first_sequence==4 && out.last_sequence==7);
    return 0;
}
static int oldest_stale(void)
{
    struct xtos_eeg_window_options opts=options();struct xtos_eeg_features out,old;
    CHECK(xtos_eeg_window_init(&window,&opts)==0);
    for (unsigned int i=0;i<4;i++) {
        struct xtos_eeg_sample s=sample(i);if (i) s.host_rx_ns=1100;
        CHECK(xtos_eeg_window_push(&window,&s)==0);
    }
    memset(&out,0xb9,sizeof(out));old=out;
    CHECK(xtos_eeg_window_features(&window,1101,&out)==-ESTALE);
    CHECK(!memcmp(&out,&old,sizeof(out)));
    CHECK(xtos_eeg_window_features(&window,1100,&out)==-EAGAIN);
    return 0;
}
int main(int argc,char **argv)
{
    if (argc!=2) return 2;
    if (!strcmp(argv[1],"golden")) return golden();
    if (!strcmp(argv[1],"boundaries")) return boundaries();
    if (!strcmp(argv[1],"quality")) return quality();
    if (!strcmp(argv[1],"freshness")) return freshness();
    if (!strcmp(argv[1],"rolling")) return rolling();
    if (!strcmp(argv[1],"bounds")) return bounds();
    if (!strcmp(argv[1],"invalid")) return invalid();
    if (!strcmp(argv[1],"duplicate")) return duplicate();
    if (!strcmp(argv[1],"oldest_stale")) return oldest_stale();
    return 2;
}
