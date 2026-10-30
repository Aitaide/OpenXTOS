#include <xtos/eeg_window.h>
#include <errno.h>
#include <math.h>
#include <string.h>

static int valid_options(const struct xtos_eeg_window_options *o)
{
    return o && o->version==XTOS_EEG_WINDOW_VERSION && o->channels>=1 &&
        o->channels<=XTOS_EEG_MAX_CHANNELS && o->sample_rate_hz>=1 &&
        o->sample_rate_hz<=65535 && o->window_frames>=2 &&
        o->window_frames<=XTOS_EEG_MAX_WINDOW_FRAMES && o->hop_frames>=1 &&
        o->hop_frames<=o->window_frames && o->max_receive_age_ns>0;
}
static int valid_sample(const struct xtos_eeg_window *w, const struct xtos_eeg_sample *s)
{
    if (!s || s->version!=XTOS_EEG_WINDOW_VERSION || s->channels!=w->options.channels ||
        s->sample_rate_hz!=w->options.sample_rate_hz || s->unit!=XTOS_EEG_UNIT_NANOVOLT ||
        s->host_clock!=XTOS_EEG_CLOCK_MONOTONIC || !s->enabled_mask ||
        s->enabled_mask>=(1u<<s->channels) || !s->session_id || !s->stream_generation ||
        !s->config_generation || !s->sequence || !s->host_rx_ns ||
        (s->source_flags!=XTOS_EEG_SOURCE_SERDEV && s->source_flags!=XTOS_EEG_SOURCE_SYNTHETIC) ||
        (s->quality_flags & ~XTOS_EEG_QUALITY_KNOWN)) return 0;
    for (uint32_t ch=s->channels;ch<XTOS_EEG_MAX_CHANNELS;ch++)
        if (s->samples_nv[ch]) return 0;
    return 1;
}
int xtos_eeg_window_init(struct xtos_eeg_window *w, const struct xtos_eeg_window_options *o)
{
    struct xtos_eeg_window_options copy;
    if (!w || !valid_options(o)) return -EINVAL;
    copy=*o;
    memset(w,0,sizeof(*w));w->options=copy;w->initialized=1;
    return 0;
}
void xtos_eeg_window_reset(struct xtos_eeg_window *w)
{
    if (!w || w->initialized!=1) return;
    w->count=0;w->next=0;w->since_emit=0;w->have_identity=0;
}
int xtos_eeg_window_push(struct xtos_eeg_window *w, const struct xtos_eeg_sample *s)
{
    int reset=0;
    if (!w || w->initialized!=1 || !valid_sample(w,s)) return -EINVAL;
    if (s->quality_flags!=XTOS_EEG_CONFIG_VERIFIED || s->missing_before) {
        xtos_eeg_window_reset(w);
        return XTOS_EEG_PUSH_DROPPED;
    }
    if (w->have_identity && (w->session_id!=s->session_id ||
        w->stream_generation!=s->stream_generation || w->config_generation!=s->config_generation ||
        w->source_flags!=s->source_flags || w->enabled_mask!=s->enabled_mask)) {
        xtos_eeg_window_reset(w);reset=1;
    }
    if (w->have_identity && (s->sequence<=w->last_sequence || s->host_rx_ns<w->last_host_rx_ns)) {
        /* Retain the fence so replay cannot restart an already observed stream. */
        w->count=0;w->next=0;w->since_emit=0;
        return XTOS_EEG_PUSH_DROPPED;
    }
    if (w->have_identity && s->sequence-w->last_sequence!=1) {
        xtos_eeg_window_reset(w);reset=1;
    }
    if (!w->have_identity) {
        w->session_id=s->session_id;w->stream_generation=s->stream_generation;
        w->config_generation=s->config_generation;w->source_flags=s->source_flags;
        w->enabled_mask=s->enabled_mask;w->have_identity=1;
    }
    memcpy(w->samples_nv[w->next],s->samples_nv,sizeof(s->samples_nv));
    w->receive_ns[w->next]=s->host_rx_ns;
    w->next=(w->next+1)%w->options.window_frames;
    if (w->count<w->options.window_frames) w->count++;
    if (w->since_emit<w->options.window_frames) w->since_emit++;
    w->last_sequence=s->sequence;w->last_host_rx_ns=s->host_rx_ns;
    return reset ? XTOS_EEG_PUSH_RESET_ACCEPTED : XTOS_EEG_PUSH_ACCEPTED;
}
int xtos_eeg_window_features(struct xtos_eeg_window *w, uint64_t now_ns, struct xtos_eeg_features *out)
{
    struct xtos_eeg_features value;
    uint32_t first;
    if (!w || w->initialized!=1 || !out) return -EINVAL;
    if (w->count<w->options.window_frames || w->since_emit<w->options.hop_frames) return -EAGAIN;
    if (now_ns<w->last_host_rx_ns || now_ns-w->receive_ns[w->next]>w->options.max_receive_age_ns) {
        xtos_eeg_window_reset(w);
        return -ESTALE;
    }
    if (w->window_id==UINT64_MAX) return -EOVERFLOW;
    memset(&value,0,sizeof(value));first=w->next;
    value.version=XTOS_EEG_WINDOW_VERSION;value.feature_kind=XTOS_EEG_FEATURE_MEAN_RMS_V1;
    value.channels=w->options.channels;value.sample_rate_hz=w->options.sample_rate_hz;
    value.feature_count=2*value.channels;value.source_flags=w->source_flags;
    value.enabled_mask=w->enabled_mask;value.frame_count=w->count;
    value.window_id=w->window_id+1;value.session_id=w->session_id;
    value.stream_generation=w->stream_generation;value.config_generation=w->config_generation;
    value.first_sequence=w->last_sequence-w->count+1;value.last_sequence=w->last_sequence;
    value.first_host_rx_ns=w->receive_ns[first];value.last_host_rx_ns=w->last_host_rx_ns;
    for (uint32_t ch=0;ch<value.channels;ch++) {
        int64_t sum=0;long double square_sum=0;
        if (!(w->enabled_mask&(1u<<ch))) continue;
        for (uint32_t i=0;i<w->count;i++) {
            int32_t sample=w->samples_nv[i][ch];sum+=(int64_t)sample;
            square_sum+=(long double)sample*(long double)sample;
        }
        value.values_uv[ch]=(double)((long double)sum/w->count/1000.0L);
        value.values_uv[value.channels+ch]=(double)(sqrtl(square_sum/w->count)/1000.0L);
    }
    *out=value;w->window_id=value.window_id;w->since_emit=0;
    return 0;
}
