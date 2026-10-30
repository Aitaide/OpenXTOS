#include "eeg_stream.h"
#include <errno.h>
#include <string.h>

int xtos_eeg_stream_init(struct xtos_eeg_stream *s,
    const struct xtos_eeg_window_options *o,uint64_t session_id)
{
    int result;
    if(!s || !o || !session_id || o->channels!=8 || o->sample_rate_hz!=250)return -EINVAL;
    result=xtos_eeg_window_init(&s->window,o);
    if(result)return result;
    s->session_id=session_id;s->barrier_id=1;s->initialized=1;s->exhausted=0;
    return 0;
}
static int zero_bytes(const void *data,size_t size)
{
    const unsigned char *p=data;
    for(size_t i=0;i<size;i++)if(p[i])return 0;
    return 1;
}
static int config_valid(const struct xtos_bci_config_v1 *c)
{
    static const uint32_t gains[]={1,2,4,6,8,12,24};
    if(c->version!=1 || c->flags || c->channels!=8 || c->sample_rate_hz!=250 ||
       c->enabled_mask>255 || !c->generation || (c->capabilities&~31u) ||
       (c->capabilities&3u)!=3u || !zero_bytes(c->reserved0,sizeof(c->reserved0)) ||
       !zero_bytes(c->reserved,sizeof(c->reserved)) || (c->registers[1]&7u)!=6u)return 0;
    for(unsigned int ch=0;ch<8;ch++) {
        unsigned int code=(c->registers[5+ch]>>4)&7u;
        if(code>=7 || c->gain[ch]!=gains[code] ||
           !!(c->registers[5+ch]&0x80u)==!!(c->enabled_mask&(1u<<ch)))return 0;
    }
    const char *end=memchr(c->firmware,0,sizeof(c->firmware));
    if(!end || end==c->firmware)return 0;
    for(const unsigned char *p=(const unsigned char *)c->firmware;
        p<(const unsigned char *)end;p++)
        if(*p!='\r' && *p!='\n' && (*p<0x20 || *p>0x7e))return 0;
    return 1;
}
static int batch_valid(const struct xtos_bci_frame_batch *b)
{
    if(!b->frame_count || b->frame_count>XTOS_BCI_FRAME_MAX_BATCH || !b->records ||
       !b->stream_generation || b->cursor>UINT64_MAX-b->frame_count ||
       b->next_cursor!=b->cursor+b->frame_count || !config_valid(&b->config))return 0;
    for(uint32_t i=0;i<b->frame_count;i++) {
        const struct xtos_bci_frame_v1 *r=&b->records[i];
        if(r->version!=1 || r->record_bytes!=sizeof(*r) || r->channels!=8 ||
           r->sample_rate_hz!=250 || r->unit!=XTOS_BCI_UNIT_NANOVOLT ||
           r->host_clock!=XTOS_BCI_FRAME_CLOCK_MONOTONIC ||
           (r->quality_flags&~XTOS_BCI_FRAME_QUALITY_KNOWN) ||
           !(r->quality_flags&XTOS_BCI_FRAME_QUALITY_CONFIG_VERIFIED) ||
           (r->source_flags!=XTOS_BCI_FRAME_SOURCE_SERDEV &&
            r->source_flags!=XTOS_BCI_FRAME_SOURCE_SYNTHETIC) || !r->host_rx_ns ||
           r->sequence!=b->cursor+i+1 || r->stream_generation!=b->stream_generation ||
           r->config_generation!=b->config.generation || r->enabled_mask!=b->config.enabled_mask ||
           r->counter_bits!=8 || r->device_counter>255 || r->missing_before>126 ||
           !!(r->quality_flags&XTOS_BCI_FRAME_QUALITY_COUNTER_GAP)!=!!r->missing_before ||
           !zero_bytes(r->reserved,sizeof(r->reserved)) ||
           !zero_bytes(r->samples_nv+8,24*sizeof(r->samples_nv[0])) ||
           (i && (r->host_rx_ns<b->records[i-1].host_rx_ns ||
                  r->source_flags!=b->records[0].source_flags)))return 0;
    }
    return 1;
}
int xtos_eeg_stream_barrier(struct xtos_eeg_stream *s)
{
    if(!s || s->initialized!=1)return -EINVAL;
    xtos_eeg_window_reset(&s->window);
    if(s->exhausted || s->barrier_id==UINT64_MAX) {s->exhausted=1;return -EOVERFLOW;}
    s->barrier_id++;
    return 0;
}
int xtos_eeg_stream_accept_batch(struct xtos_eeg_stream *s,const struct xtos_bci_frame_batch *b)
{
    if(!s || s->initialized!=1 || !b)return -EINVAL;
    if(s->exhausted)return -EOVERFLOW;
    if(!b->frame_count && !b->cursor && !b->next_cursor && !b->overwritten_frames &&
       !b->stream_generation && zero_bytes(&b->config,sizeof(b->config))) {
        int result=xtos_eeg_stream_barrier(s);
        return result ? result : -ENODATA;
    }
    if(!batch_valid(b)) {
        int result=xtos_eeg_stream_barrier(s);
        return result ? result : -EPROTO;
    }
    if(!b->config.enabled_mask) {
        int result=xtos_eeg_stream_barrier(s);
        return result ? result : 1;
    }
    for(uint32_t i=0;i<b->frame_count;i++) {
        const struct xtos_bci_frame_v1 *r=&b->records[i];
        struct xtos_eeg_sample sample={.version=1,.channels=r->channels,
            .sample_rate_hz=r->sample_rate_hz,.unit=r->unit,.host_clock=r->host_clock,
            .quality_flags=r->quality_flags,.source_flags=r->source_flags,
            .enabled_mask=r->enabled_mask,.missing_before=r->missing_before,
            .session_id=s->session_id,.stream_generation=r->stream_generation,
            .config_generation=r->config_generation,.sequence=r->sequence,.host_rx_ns=r->host_rx_ns};
        memcpy(sample.samples_nv,r->samples_nv,sizeof(sample.samples_nv));
        int result=xtos_eeg_window_push(&s->window,&sample);
        if(result<0) {
            int barrier=xtos_eeg_stream_barrier(s);
            return barrier ? barrier : -EPROTO;
        }
        if(result!=XTOS_EEG_PUSH_ACCEPTED) {
            if(s->barrier_id==UINT64_MAX) {
                xtos_eeg_window_reset(&s->window);s->exhausted=1;
                return -EOVERFLOW;
            }
            s->barrier_id++;
        }
    }
    return 0;
}
int xtos_eeg_stream_rebind(struct xtos_eeg_stream *s,uint64_t session_id)
{
    if(!s || s->initialized!=1 || !session_id || session_id<=s->session_id)return -EINVAL;
    int result=xtos_eeg_stream_barrier(s);
    if(result)return result;
    s->session_id=session_id;
    return 0;
}
int xtos_eeg_stream_read(struct xtos_eeg_stream *s,xtos_bci_session_t *session,uint32_t capacity)
{
    struct xtos_bci_frame_batch batch;
    if(!s || s->initialized!=1 || !session || !capacity || capacity>XTOS_BCI_FRAME_MAX_BATCH)return -EINVAL;
    if(s->exhausted)return -EOVERFLOW;
    if(xtos_bci_frame_read(session,capacity,XTOS_BCI_FRAME_NONBLOCK,&batch)<0) {
        int saved=errno;
        if(saved==EAGAIN || saved==EWOULDBLOCK)return -saved;
        /* Preserve the primary transport failure even if invalidation exhausts. */
        xtos_eeg_stream_barrier(s);
        return saved>0 ? -saved : -EIO;
    }
    return xtos_eeg_stream_accept_batch(s,&batch);
}
int xtos_eeg_stream_features(struct xtos_eeg_stream *s,uint64_t now_ns,struct xtos_eeg_stream_features *out)
{
    struct xtos_eeg_stream_features value={0};
    if(!s || s->initialized!=1 || !out)return -EINVAL;
    if(s->exhausted)return -EOVERFLOW;
    int result=xtos_eeg_window_features(&s->window,now_ns,&value.features);
    if(result==-ESTALE) {
        int barrier=xtos_eeg_stream_barrier(s);
        return barrier ? barrier : result;
    }
    if(result)return result;
    value.barrier_id=s->barrier_id;*out=value;
    return 0;
}
