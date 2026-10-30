#include "eeg_export.h"
#include <errno.h>
#include <inttypes.h>
#include <stdarg.h>
#include <string.h>
#include <math.h>

static int options_valid(const struct xtos_eeg_export_options *o)
{
    return o && o->device && o->device[0]=='/' && o->session_id &&
        o->window_frames>=2 && o->window_frames<=4096 && o->hop_frames &&
        o->hop_frames<=o->window_frames && o->max_receive_age_ns &&
        o->duration_ms && o->duration_ms<=600000 && o->max_windows && o->max_windows<=100000;
}
static int decimal(const char *p,uint64_t *out)
{
    uint64_t n=0;
    if(!p || !*p)return -EINVAL;
    for(;*p;p++) {
        if(*p<'0' || *p>'9' || n>(UINT64_MAX-(unsigned)(*p-'0'))/10)return -EINVAL;
        n=n*10+(unsigned)(*p-'0');
    }
    *out=n;return 0;
}
int xtos_eeg_export_parse(int argc,char *const *argv,struct xtos_eeg_export_options *out)
{
    static const char *const names[]={"--device","--session-id","--window","--hop","--max-age-ns","--duration-ms","--max-windows"};
    uint64_t values[7]={0};unsigned seen=0;
    struct xtos_eeg_export_options o={0};
    if(!out || !argv || argc!=15)return -EINVAL;
    for(int i=1;i<argc;i+=2) {
        unsigned k;
        for(k=0;k<7;k++)if(!strcmp(argv[i],names[k]))break;
        if(k==7 || (seen&(1u<<k)))return -EINVAL;
        seen|=1u<<k;
        if(!k)o.device=argv[i+1];
        else if(decimal(argv[i+1],&values[k]))return -EINVAL;
    }
    if(values[2]>UINT32_MAX || values[3]>UINT32_MAX || values[5]>UINT32_MAX || values[6]>UINT32_MAX)return -EINVAL;
    o.session_id=values[1];o.window_frames=(uint32_t)values[2];o.hop_frames=(uint32_t)values[3];
    o.max_receive_age_ns=values[4];o.duration_ms=(uint32_t)values[5];o.max_windows=(uint32_t)values[6];
    if(seen!=127 || !options_valid(&o))return -EINVAL;
    *out=o;return 0;
}
static int feature_valid(const struct xtos_eeg_export_options *o,
    const struct xtos_eeg_stream_features *v,uint64_t now)
{
    if(!options_valid(o) || !v)return 0;
    const struct xtos_eeg_features *f=&v->features;
    if(!v->barrier_id || f->version!=1 || f->feature_kind!=1 || f->feature_count!=16 ||
       f->channels!=8 || f->sample_rate_hz!=250 || (f->source_flags!=1 && f->source_flags!=2) ||
       !f->enabled_mask || f->enabled_mask>255 || f->frame_count!=o->window_frames ||
       !f->window_id || f->session_id!=o->session_id || !f->stream_generation || !f->config_generation ||
       !f->first_sequence || f->last_sequence<f->first_sequence ||
       f->last_sequence-f->first_sequence!=(uint64_t)f->frame_count-1 ||
       !f->first_host_rx_ns || f->last_host_rx_ns<f->first_host_rx_ns ||
       now<f->last_host_rx_ns || now-f->first_host_rx_ns>o->max_receive_age_ns)return 0;
    for(unsigned i=0;i<16;i++) {
        if(!isfinite(f->values_uv[i]) || (i>=8 && f->values_uv[i]<0) ||
           (!(f->enabled_mask&(1u<<(i%8))) && f->values_uv[i]!=0))return 0;
    }
    return 1;
}

static int append(char *buffer,size_t capacity,size_t *used,const char *format,...)
{
    va_list ap;
    va_start(ap,format);
    int n=vsnprintf(buffer+*used,capacity-*used,format,ap);
    va_end(ap);
    if(n<0 || (size_t)n>=capacity-*used)return -EOVERFLOW;
    *used+=(size_t)n;
    return 0;
}
int xtos_eeg_export_format(char *buffer,size_t capacity,const struct xtos_eeg_export_options *o,
    const struct xtos_eeg_stream_features *value,uint64_t now)
{
    size_t used=0;
    if(!buffer || !capacity || !feature_valid(o,value,now))return -EINVAL;
    const struct xtos_eeg_features *f=&value->features;
#define ADD(...) do {int r=append(buffer,capacity,&used,__VA_ARGS__);if(r)return r;}while(0)
    ADD("{\"schema\":\"xtos.eeg.window.v1\",\"feature_mapping\":\"xtos.eeg.mean-rms.v1\",\"unit\":\"microvolt\",\"order\":\"mean_then_rms\",\"channels\":8,\"sample_rate_hz\":250,\"enabled_mask\":%u,\"frame_count\":%u,\"window_frames\":%u,\"hop_frames\":%u,\"max_receive_age_ns\":%" PRIu64 ",\"emitted_at_ns\":%" PRIu64 ",\"source_flags\":%u,\"source_kind\":\"%s\",\"barrier_id\":%" PRIu64 ",\"session_id\":%" PRIu64 ",\"stream_generation\":%" PRIu64 ",\"config_generation\":%" PRIu64 ",\"window_id\":%" PRIu64 ",\"first_sequence\":%" PRIu64 ",\"last_sequence\":%" PRIu64 ",\"first_host_rx_ns\":%" PRIu64 ",\"last_host_rx_ns\":%" PRIu64 ",\"values\":[",
        f->enabled_mask,f->frame_count,o->window_frames,o->hop_frames,o->max_receive_age_ns,now,
        f->source_flags,f->source_flags==1?"serdev":"synthetic",value->barrier_id,
        f->session_id,f->stream_generation,f->config_generation,f->window_id,
        f->first_sequence,f->last_sequence,f->first_host_rx_ns,f->last_host_rx_ns);
    for(unsigned i=0;i<16;i++)ADD("%s%.17g",i?",":"",f->values_uv[i]);
    ADD("],\"safe_action\":\"no_action\",\"physical_acceptance\":false}\n");
#undef ADD
    return (int)used;
}
int xtos_eeg_export_window(FILE *out,const struct xtos_eeg_export_options *o,
    const struct xtos_eeg_stream_features *value,uint64_t now)
{
    char buffer[4096];
    if(!out)return -EINVAL;
    int used=xtos_eeg_export_format(buffer,sizeof(buffer),o,value,now);
    if(used<0)return used;
    return fwrite(buffer,1,(size_t)used,out)==(size_t)used?0:-EIO;
}
