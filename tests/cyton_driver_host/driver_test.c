/* SPDX-License-Identifier: GPL-2.0 */
/* Compile the actual driver; only kernel services are replaced. */
#define XTOS_CYTON_HOST_TEST 1
#include "../../kernel/linux/drivers/xtos/bci/devices/openbci_cyton.c"


static unsigned char board_regs[24] = {0x3e,0x96,0xc0,0xec,2,0x68,0x68,0x68,0x68,0x68,0x68,0x68,0x68,255,255};
static bool missing_reply, missing_row, wrong_ack, no_effect;
static unsigned char banner[300];
static size_t banner_length, reply_fragment=7;
static void board_reply(struct serdev_device *s,const void *data,size_t len)
{
    const char *p=data;
    char text[4096]; size_t n=0;
    static const char *names[24]={"ADS_ID","CONFIG1","CONFIG2","CONFIG3","LOFF","CH1SET","CH2SET","CH3SET","CH4SET","CH5SET","CH6SET","CH7SET","CH8SET","BIAS_SENSP","BIAS_SENSN","LOFF_SENSP","LOFF_SENSN","LOFF_FLIP","LOFF_STATP","LOFF_STATN","GPIO","MISC1","MISC2","CONFIG4"};
    if (missing_reply) return;
    if (len==1 && p[0]=='v') {
        for (int i=5;i<13;i++) board_regs[i]=0x68;
        n=snprintf(text,sizeof(text),"OpenBCI V3 8-16 channel\r\nOn Board ADS1299 Device ID: 0x3E\r\nLIS3DH Device ID: 0x33\r\nFirmware: v3.1.2\r\n$$$");
        if (banner_length) { memcpy(text,banner,banner_length); memcpy(text+banner_length,"$$$",3); n=banner_length+3; }
    } else if (len==2 && !memcmp(p,"~6",2)) {
        /* Official setSampleRate calls initialize_ads: all writable state resets. */
        static const unsigned char defaults[24]={0x3e,0x96,0xc0,0xec,2,0x68,0x68,0x68,0x68,0x68,0x68,0x68,0x68,255,255};
        memcpy(board_regs,defaults,sizeof(board_regs));
        n=snprintf(text,sizeof(text),"Success: Sample rate is 250Hz$$$");
    } else if (len==1 && p[0]=='?') {
        n=snprintf(text,sizeof(text),"Board ADS Registers\r\n");
        for (int i=0;i<24;i++) {
            if (missing_row && i==23) continue;
            unsigned v=board_regs[i];
            n+=snprintf(text+n,sizeof(text)-n,"%s, %02X, %02X",names[i],i,v);
            for (int b=7;b>=0;b--) n+=snprintf(text+n,sizeof(text)-n,", %u",(v>>b)&1);
            n+=snprintf(text+n,sizeof(text)-n,"\r\n");
        }
        n+=snprintf(text+n,sizeof(text)-n,"LIS3DH Registers\r\n$$$");
    } else if (len==9 && p[0]=='x') {
        unsigned ch=p[1]-'1'; assert(ch<8);
        if (!no_effect) {
            board_regs[5+ch]=((p[2]-'0')<<7)|((p[3]-'0')<<4)|((p[6]-'0')<<3)|(p[4]-'0');
            for (int i=13;i<=14;i++) board_regs[i]=(board_regs[i]&~(1u<<ch))|((p[5]-'0')<<ch);
            board_regs[21]=(p[7]-'0')<<5;
        }
        n=snprintf(text,sizeof(text),"Success: Channel set for %c$$$",wrong_ack ? '9':p[1]);
    } else if (len==1) {
        const char *q=strchr("!@#$%^&*",p[0]);
        if (q && !no_effect) board_regs[5+(q-"!@#$%^&*")] &= 0x7f;
        q=strchr("12345678",p[0]);
        if (q && !no_effect) board_regs[5+(q-"12345678")] |= 0x80;
    }
    for (size_t i=0;i<n;i+=reply_fragment) { size_t part=n-i<reply_fragment?n-i:reply_fragment; assert(s->ops->receive_buf(s,(unsigned char *)text+i,part)==(int)part); }
}

static struct serdev_device serial;
#define openbci_cyton_dev (*(struct xtos_bci_dev *)serial.data)
static bool inject_open;
static unsigned char frame[33];
static unsigned int commands;
static char command_bytes[256];
static int write_result = -999;
static bool fail_only_start;
static bool fail_only_stop;
static unsigned int allocations, frees;
static bool inject_tx, inject_cancel_tx, cancel_before_arm;
static int ring_result = 32;
static pthread_mutex_t gate = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static bool block_tx, tx_entered, release_tx;
static bool block_ring, ring_entered, release_ring;
static _Thread_local bool is_cleanup, is_reset;
static bool cleanup_control_waiting, cleanup_state_waiting, cleanup_done;
static bool fault_observe, fault_attempted, reset_requested, reset_ready;
static bool observe_wait, wait_entered, cancel_wait;
static bool admission_observe, admission_fault, fault_paused, control_state_attempted;
static _Thread_local bool is_admission, admission_control_held, admission_state_checked;
void host_wait_enter(void)
{
    if (!observe_wait) return;
    assert(!pthread_mutex_lock(&gate)); wait_entered=true;
    assert(!pthread_cond_broadcast(&changed)); assert(!pthread_mutex_unlock(&gate));
}
void host_before_unlock(struct mutex *m)
{
    if (admission_observe && is_admission) {
        struct openbci_priv *priv = openbci_cyton_dev.hw_private;
        if (m == &priv->state_lock && !admission_state_checked) {
            assert(admission_control_held);
            assert(!priv->reply_pending && "admission must check state before arming TX");
            admission_state_checked = true;
        }
        if (m == &priv->control_lock) {
            assert(admission_state_checked && "receiving admission requires state_lock");
            admission_control_held = false;
        }
    }
    if (fault_observe && m==&((struct openbci_priv *)openbci_cyton_dev.hw_private)->state_lock &&
        READ_ONCE(openbci_cyton_dev.acquisition_error)==-ERANGE) {
        assert(!pthread_mutex_lock(&gate));
        assert(fault_attempted && "overflow statistics require dev->lock");
        assert(!pthread_mutex_unlock(&gate));
    }
}
void host_after_write(const void *address)
{
    if (admission_fault && address == &openbci_cyton_dev.acquisition_error &&
        READ_ONCE(openbci_cyton_dev.acquisition_error) == -ERANGE) {
        assert(!pthread_mutex_lock(&gate));
        fault_paused = true;
        assert(!pthread_cond_broadcast(&changed));
        while (!control_state_attempted) assert(!pthread_cond_wait(&changed, &gate));
        assert(!pthread_mutex_unlock(&gate));
    }
    if (fault_observe && address==&openbci_cyton_dev.acquisition_error &&
        READ_ONCE(openbci_cyton_dev.acquisition_error)==-ERANGE) {
        assert(!pthread_mutex_lock(&gate)); reset_requested=true;
        assert(!pthread_cond_broadcast(&changed));
        while (!reset_ready) assert(!pthread_cond_wait(&changed,&gate));
        assert(!pthread_mutex_unlock(&gate));
    }
}
void host_before_lock(struct mutex *m)
{
    if (cancel_before_arm) {
        struct openbci_priv *priv=openbci_cyton_dev.hw_private;
        if (m==&priv->state_lock && priv->generation==2) {
            cancel_before_arm=false;
            openbci_cancel(&openbci_cyton_dev);
        }
    }
    if (admission_observe && is_admission) {
        struct openbci_priv *priv = openbci_cyton_dev.hw_private;
        if (m == &priv->control_lock) {
            assert(!admission_control_held);
            admission_control_held = true;
        }
        if (m == &priv->state_lock) {
            assert(admission_control_held && "control_lock must precede state_lock");
            assert(!pthread_mutex_lock(&gate));
            control_state_attempted = true;
            assert(!pthread_cond_broadcast(&changed));
            assert(!pthread_mutex_unlock(&gate));
        }
    }
    if (fault_observe && !is_reset && m==&openbci_cyton_dev.lock && READ_ONCE(openbci_cyton_dev.acquisition_error)==-ERANGE) {
        assert(!pthread_mutex_lock(&gate)); fault_attempted=true;
        assert(!pthread_cond_broadcast(&changed)); assert(!pthread_mutex_unlock(&gate));
    }
    if (!is_cleanup)
        return;
    struct openbci_priv *priv = openbci_cyton_dev.hw_private;
    assert(!pthread_mutex_lock(&gate));
    if (m == &priv->control_lock)
        cleanup_control_waiting = true;
    if (m == &priv->state_lock)
        cleanup_state_waiting = true;
    assert(!pthread_cond_broadcast(&changed));
    assert(!pthread_mutex_unlock(&gate));
}
static void wait_flag(bool *flag)
{
    assert(!pthread_mutex_lock(&gate));
    while (!*flag)
        assert(!pthread_cond_wait(&changed, &gate));
    assert(!pthread_mutex_unlock(&gate));
}
static void release_flag(bool *flag)
{
    assert(!pthread_mutex_lock(&gate));
    *flag = true;
    assert(!pthread_cond_broadcast(&changed));
    assert(!pthread_mutex_unlock(&gate));
}
static void *control_thread(void *unused)
{
    (void)unused;
    assert(openbci_set_channel_enable(&openbci_cyton_dev, 7, true) == 0);
    return NULL;
}
static void *reset_fault_thread(void *unused)
{
    (void)unused;
    wait_flag(&reset_requested);
    is_reset=true;
    unsigned long flags;
    spin_lock_irqsave(&openbci_cyton_dev.lock,flags);
    openbci_cyton_dev.errors=0;
    release_flag(&reset_ready);
    /* Same lock as core RESET/GET_STATS, without using the gate to order
     * these accesses after RX. TSan catches an unlocked fault increment. */
    for (unsigned int i=0;i<10000;i++) {
        volatile unsigned long observed=openbci_cyton_dev.errors;
        assert(observed==0);
    }
    wait_flag(&fault_attempted);
    assert(openbci_cyton_dev.errors==0);
    spin_unlock_irqrestore(&openbci_cyton_dev.lock,flags);
    return NULL;
}
static void *reply_thread(void *unused)
{
    (void)unused; wait_flag(&wait_entered);
    if (cancel_wait) openbci_cancel(&openbci_cyton_dev);
    else { missing_reply=false; board_reply(&serial,"?",1); }
    return NULL;
}
static void *receive_thread(void *unused)
{
    (void)unused;
    assert(serial.ops->receive_buf(&serial, frame, 33) == 33);
    return NULL;
}
static void *cleanup_thread(void *unused)
{
    (void)unused;
    is_cleanup = true;
    openbci_cleanup(&openbci_cyton_dev);
    release_flag(&cleanup_done);
    return NULL;
}

static void packet(unsigned int counter)
{
    memset(frame, 0, sizeof(frame));
    frame[0] = 0xa0;
    frame[1] = counter;
    frame[2] = 0x7f; frame[3] = 0xff; frame[4] = 0xff;
    frame[5] = 0x80;
    frame[32] = 0xc0;
}
int serdev_device_open(struct serdev_device *s)
{
    if (inject_open)
        assert(s->ops->receive_buf(s, frame, sizeof(frame)) == 33);
    return 0;
}
void serdev_device_close(struct serdev_device *s) { (void)s; }
int serdev_device_write(struct serdev_device *s, const void *data, size_t len, unsigned long timeout)
{
    (void)timeout;
    struct xtos_bci_dev *dev = serdev_device_get_drvdata(s);
    struct openbci_priv *priv = dev->hw_private;
    /* Synchronous TX can depend on receive progress. Never own state here. */
    assert(!pthread_mutex_trylock(&priv->state_lock.native));
    assert(!pthread_mutex_unlock(&priv->state_lock.native));
    assert(commands + len <= sizeof(command_bytes));
    memcpy(command_bytes + commands, data, len);
    commands += len;
    bool failing = write_result != -999 && (!fail_only_start || (len==1 && *(const char *)data=='b')) && (!fail_only_stop || (len==1 && *(const char *)data=='s'));
    if (!failing) board_reply(s,data,len);
    if (inject_cancel_tx && len==1 && *(const char *)data=='b')
        openbci_cancel(&openbci_cyton_dev);
    if (inject_tx && len==1 && *(const char *)data=='b')
        assert(s->ops->receive_buf(s, frame, sizeof(frame)) == 33);
    assert(!pthread_mutex_lock(&gate));
    if (block_tx) {
        tx_entered = true;
        assert(!pthread_cond_broadcast(&changed));
        while (!release_tx)
            assert(!pthread_cond_wait(&changed, &gate));
    }
    assert(!pthread_mutex_unlock(&gate));
    if (host_fixed_clock && len==1 && *(const char *)data=='?') { host_clock+=host_tx_ticks; host_clock_after_tx=true; }
    return failing ? write_result : (int)len;
}
int xtos_bci_register_device(struct xtos_bci_dev *dev)
{
    assert(dev->lock.initialized);
    return 0;
}
void xtos_bci_unregister_device(struct xtos_bci_dev *dev) { (void)dev; }
struct xtos_bci_ringbuf *xtos_bci_ringbuf_alloc(struct device *dev, size_t size)
{
    struct xtos_bci_ringbuf *rb = calloc(1, sizeof(*rb));
    (void)dev;
    assert(rb); init_waitqueue_head(&rb->wait); rb->size = size; allocations++;
    return rb;
}
void xtos_bci_ringbuf_free(struct device *dev, struct xtos_bci_ringbuf *rb)
{ (void)dev; assert(rb); frees++; free(rb); }
int xtos_bci_ringbuf_write(struct xtos_bci_ringbuf *rb, const void *data, size_t len)
{
    assert(rb && len == 32);
    assert(!pthread_mutex_lock(&gate));
    if (block_ring) {
        ring_entered = true;
        assert(!pthread_cond_broadcast(&changed));
        while (!release_ring)
            assert(!pthread_cond_wait(&changed, &gate));
    }
    assert(!pthread_mutex_unlock(&gate));
    memcpy(rb->last, data, len); rb->writes++;
    return ring_result;
}
/* Capture bridge arguments verbatim. This is not the production frame ring. */
static struct xtos_bci_frame_v1 captured[4096];
static struct xtos_bci_config_v1 captured_config[4096];
static unsigned int frame_count, frame_enables, clock_calls;
static _Atomic unsigned int frame_wakes;
static int enable_result, publish_result;
static u64 rx_clock = 1000000;
u64 ktime_get_ns(void)
{
    struct openbci_priv *priv = openbci_cyton_dev.hw_private;
    assert(pthread_mutex_trylock(&priv->state_lock.native) == EBUSY);
    clock_calls++;
    return rx_clock;
}
int xtos_bci_device_frames_enable(struct xtos_bci_dev *dev, unsigned int capacity_frames)
{
    struct openbci_priv *priv = dev->hw_private;
    assert(!priv->receiving && capacity_frames == 500);
    frame_enables++;
    return enable_result;
}
int xtos_bci_device_frame_publish(struct xtos_bci_dev *dev,
    const struct xtos_bci_frame_v1 *f, const struct xtos_bci_config_v1 *c)
{
    (void)dev;
    if (publish_result) return publish_result;
    assert(frame_count < 4096);
    captured[frame_count] = *f;
    captured_config[frame_count++] = *c;
    return 0;
}
void xtos_bci_device_frames_wake(struct xtos_bci_dev *dev)
{ (void)dev; frame_wakes++; }
static void setup(void)
{
    packet(1);
    assert(openbci_probe(&serial) == 0);
    assert(serial.ops->write_wakeup);
    assert(openbci_cyton_dev.resolution_bits == 24);
    assert(openbci_cyton_dev.input_range_mv == 4500);
}
int main(int argc, char **argv)
{
    assert(argc == 2);
    if (!strcmp(argv[1], "frame_counter_boundaries")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        assert(!openbci_start_stream(&openbci_cyton_dev));
        const unsigned int values[]={0,127,255,126,128};
        for (unsigned int i=0;i<5;i++) { packet(values[i]); serial.ops->receive_buf(&serial,frame,33); }
        assert(frame_count==3 && captured[1].missing_before==126);
        assert(captured[2].device_counter==128 && !captured[2].missing_before);
        assert((captured[2].quality_flags&(4|16))==(4|16));
    } else if (!strcmp(argv[1], "frame_raw_rails")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        assert(!openbci_start_stream(&openbci_cyton_dev));
        packet(1); memset(frame+2,0,24); frame[2]=0x80;
        serial.ops->receive_buf(&serial,frame,33);
        assert(captured[0].quality_flags==9 && captured[0].samples_nv[0]==-187500022);
        packet(2); memset(frame+2,0,24); frame[2]=0x7f; frame[3]=frame[4]=0xff;
        serial.ops->receive_buf(&serial,frame,33);
        assert(captured[1].quality_flags==9 && captured[1].samples_nv[0]==187500000);
        packet(3); memset(frame+2,0,24); frame[2]=0x7f; frame[3]=0xff; frame[4]=0xfe;
        serial.ops->receive_buf(&serial,frame,33);
        assert(captured[2].quality_flags==1);
    } else if (!strcmp(argv[1], "frame_cancel_start") || !strcmp(argv[1], "frame_cancel_arm")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        cancel_before_arm=!strcmp(argv[1],"frame_cancel_arm");
        inject_cancel_tx=!cancel_before_arm;
        assert(openbci_start_stream(&openbci_cyton_dev)==-ENODEV);
        assert(!openbci_cyton_dev.streaming);
        assert(!((struct openbci_priv *)openbci_cyton_dev.hw_private)->receiving);
        assert(!frame_count);
        if (!strcmp(argv[1],"frame_cancel_arm"))
            assert(((struct openbci_priv *)openbci_cyton_dev.hw_private)->stream_generation==0);
    } else if (!strcmp(argv[1], "frame_time_config_lifetime")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        assert(!openbci_start_stream(&openbci_cyton_dev));
        unsigned char bytes[66]; packet(1); memcpy(bytes,frame,33);
        packet(2); memcpy(bytes+33,frame,33);
        unsigned int calls=clock_calls;
        serial.ops->receive_buf(&serial,bytes,66);
        assert(clock_calls==calls+1 && frame_count==2);
        assert(captured[0].host_rx_ns==1000000 && captured[1].host_rx_ns==1000000);
        packet(3); serial.ops->receive_buf(&serial,frame,9);
        rx_clock=2000000; serial.ops->receive_buf(&serial,frame+9,24);
        assert(captured[2].host_rx_ns==2000000);
        struct xtos_bci_config_v1 old=captured_config[0];
        unsigned int before=commands;
        assert(!openbci_start_stream(&openbci_cyton_dev) && commands==before);
        assert(!openbci_stop_stream(&openbci_cyton_dev));
        assert(!openbci_set_channel_config(&openbci_cyton_dev,0,false,12));
        assert(!openbci_start_stream(&openbci_cyton_dev));
        packet(1); serial.ops->receive_buf(&serial,frame,33);
        assert(captured[3].stream_generation==2 && captured[3].config_generation>old.generation);
        assert(captured[3].enabled_mask==254 && captured[3].samples_nv[0]==375000000);
        assert(!memcmp(&old,&captured_config[0],sizeof(old)));
        assert(!openbci_stop_stream(&openbci_cyton_dev));
        write_result=0; fail_only_start=true;
        assert(openbci_start_stream(&openbci_cyton_dev)==-EIO);
        write_result=-999; fail_only_start=false;
        openbci_cleanup(&openbci_cyton_dev);
        assert(!openbci_init(&openbci_cyton_dev));
        assert(!openbci_start_stream(&openbci_cyton_dev));
        packet(1); serial.ops->receive_buf(&serial,frame,33);
        assert(captured[4].stream_generation==4 && frame_count==5);
        assert(frame_enables==2 && allocations==1);
    } else if (!strcmp(argv[1], "frame_enable_failure")) {
        setup(); enable_result=-ENOMEM;
        assert(openbci_init(&openbci_cyton_dev)==-ENOMEM);
        assert(openbci_cyton_dev.acquisition_error==-ENOMEM && !openbci_cyton_dev.streaming);
        struct xtos_bci_config_v1 cfg={0};
        assert(openbci_get_config(&openbci_cyton_dev,&cfg)==-ENODATA);
        assert(!openbci_cyton_dev.ringbuf);
        enable_result=0; assert(!openbci_init(&openbci_cyton_dev));
    } else if (!strcmp(argv[1], "frame_legacy_independent")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        assert(!openbci_start_stream(&openbci_cyton_dev));
        ring_result=-EIO; serial.ops->receive_buf(&serial,frame,33);
        assert(frame_count==1 && !openbci_cyton_dev.samples_read);
        struct xtos_bci_frame_v1 saved=captured[0];
        ring_result=32; packet(2); serial.ops->receive_buf(&serial,frame,33);
        assert(frame_count==2 && !memcmp(&saved,&captured[0],sizeof(saved)));
        assert(openbci_cyton_dev.samples_read==8 && openbci_cyton_dev.samples_dropped==8);
    } else if (!strcmp(argv[1], "frame_cancel_idle")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        assert(!openbci_start_stream(&openbci_cyton_dev));
        openbci_cancel(&openbci_cyton_dev);
        assert(!openbci_cyton_dev.streaming);
        assert(!((struct openbci_priv *)openbci_cyton_dev.hw_private)->receiving);
    } else if (!strcmp(argv[1], "frame_cancel_rx")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        assert(!openbci_start_stream(&openbci_cyton_dev));
        openbci_cancel(&openbci_cyton_dev);
        serial.ops->receive_buf(&serial,frame,33);
        assert(!frame_count && !openbci_cyton_dev.ringbuf->writes);
        assert(!openbci_cyton_dev.streaming);
    } else if (!strcmp(argv[1], "frame_terminal_wake")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        assert(!openbci_start_stream(&openbci_cyton_dev));
        unsigned int before=frame_wakes;
        assert(!openbci_stop_stream(&openbci_cyton_dev));
        assert(frame_wakes>before);
        before=frame_wakes; openbci_cancel(&openbci_cyton_dev);
        assert(frame_wakes>before);
    } else if (!strcmp(argv[1], "frame_sync_fault")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        inject_tx=true; publish_result=-EINVAL;
        assert(openbci_start_stream(&openbci_cyton_dev)==-EINVAL);
        assert(openbci_cyton_dev.acquisition_error==-EINVAL && !openbci_cyton_dev.streaming);
        assert(((struct openbci_priv *)openbci_cyton_dev.hw_private)->stream_generation==1);
        assert(!frame_count && !openbci_cyton_dev.ringbuf->writes);
    } else if (!strcmp(argv[1], "frame_publish_fault")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        assert(!openbci_start_stream(&openbci_cyton_dev));
        unsigned char noise=0x55; serial.ops->receive_buf(&serial,&noise,1);
        publish_result=-EOVERFLOW;
        unsigned int wakes=frame_wakes;
        serial.ops->receive_buf(&serial,frame,33);
        struct openbci_priv *priv=openbci_cyton_dev.hw_private;
        assert(openbci_cyton_dev.acquisition_error==-EOVERFLOW);
        assert(!priv->receiving && !priv->config_valid && !openbci_cyton_dev.streaming);
        assert(!frame_count && !openbci_cyton_dev.ringbuf->writes);
        assert(openbci_cyton_dev.errors==1 && frame_wakes>wakes);
        assert(priv->pending_quality&4);
        publish_result=0; packet(2); serial.ops->receive_buf(&serial,frame,33);
        assert(!frame_count);
    } else if (!strcmp(argv[1], "frame_stream_exhaustion")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        struct openbci_priv *priv=openbci_cyton_dev.hw_private;
        priv->stream_generation=~(u64)0;
        unsigned int before=commands;
        assert(openbci_start_stream(&openbci_cyton_dev)==-EOVERFLOW);
        assert(commands==before && !priv->receiving && !openbci_cyton_dev.streaming);
        assert(openbci_cyton_dev.acquisition_error==-EOVERFLOW && !priv->config_valid);
        assert(priv->stream_generation==~(u64)0);
    } else if (!strcmp(argv[1], "frame_resync")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        assert(!openbci_start_stream(&openbci_cyton_dev));
        unsigned char noise=0x55;
        serial.ops->receive_buf(&serial,&noise,1);
        packet(1); serial.ops->receive_buf(&serial,frame,33);
        assert((captured[0].quality_flags & (4|16))==4);
        serial.ops->receive_buf(&serial,frame,33); /* duplicate */
        packet(2); serial.ops->receive_buf(&serial,frame,33);
        assert(frame_count==2 && (captured[1].quality_flags&(4|16))==(4|16));
        packet(3); serial.ops->receive_buf(&serial,frame,33);
        assert(!(captured[2].quality_flags&(4|16)));
        packet(4); frame[32]=0; serial.ops->receive_buf(&serial,frame,33);
        packet(4); serial.ops->receive_buf(&serial,frame,33);
        assert((captured[3].quality_flags&(4|16))==(4|16));
    } else if (!strcmp(argv[1], "frame_gap_saturation")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        assert(!openbci_start_stream(&openbci_cyton_dev));
        packet(254); serial.ops->receive_buf(&serial,frame,33);
        packet(255); serial.ops->receive_buf(&serial,frame,33);
        packet(0); serial.ops->receive_buf(&serial,frame,33);
        assert(frame_count==3 && !captured[2].missing_before && !(captured[2].quality_flags&2));
        struct openbci_priv *priv = openbci_cyton_dev.hw_private;
        priv->parser.missing_samples=~0UL;
        packet(3); serial.ops->receive_buf(&serial,frame,33);
        assert(frame_count==4 && captured[3].missing_before==2);
        assert((captured[3].quality_flags&2) && priv->parser.missing_samples==~0UL);
    } else if (!strcmp(argv[1], "frame_provenance")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        inject_tx = true;
        assert(!openbci_start_stream(&openbci_cyton_dev));
        assert(frame_count == 1 && "RX must publish a complete frame");
        struct xtos_bci_config_v1 cfg = {0};
        assert(!openbci_get_config(&openbci_cyton_dev, &cfg));
        struct xtos_bci_frame_v1 expected = {
            .version=1, .record_bytes=224, .channels=8, .sample_rate_hz=250,
            .unit=1, .host_clock=1, .quality_flags=1|8, .source_flags=1,
            .host_rx_ns=1000000, .stream_generation=1,
            .config_generation=cfg.generation, .device_counter=1,
            .counter_bits=8, .enabled_mask=255,
            .samples_nv={187500000, -187500022},
        };
        assert(!memcmp(&expected, &captured[0], sizeof(expected)));
        assert(!memcmp(&cfg, &captured_config[0], sizeof(cfg)));
        assert(frame_enables == 1 && clock_calls);
    } else if (!strncmp(argv[1], "control_admission_", 18) || !strncmp(argv[1], "control_fault_", 14)) {
        bool fault = !strncmp(argv[1], "control_fault_", 14);
        const char *kind = argv[1] + (fault ? 14 : 18);
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        if (fault) board_regs[5] = 0x08;
        assert(!openbci_start_stream(&openbci_cyton_dev));
        commands = 0;
        admission_observe = true;
        pthread_t rx;
        if (fault) {
            admission_fault = true;
            assert(!pthread_create(&rx, NULL, receive_thread, NULL));
            wait_flag(&fault_paused);
        }
        is_admission = true;
        int ret = !strcmp(kind, "gain") ? openbci_set_channel_config(&openbci_cyton_dev, 0, true, 12) :
                  !strcmp(kind, "rate") ? openbci_set_sampling_rate(&openbci_cyton_dev, 250) :
                  openbci_set_channel_enable(&openbci_cyton_dev, 0, false);
        assert(ret == (fault ? 0 : -EBUSY));
        assert(admission_state_checked && control_state_attempted);
        is_admission = false;
        if (fault) {
            assert(!pthread_join(rx, NULL));
            assert(!openbci_cyton_dev.streaming && openbci_cyton_dev.acquisition_error == -ERANGE);
            assert(!openbci_cyton_dev.ringbuf->writes);
            assert(commands == (!strcmp(kind, "gain") ? 11u : !strcmp(kind, "rate") ? 1u : 3u));
            struct xtos_bci_config_v1 cfg = {0};
            assert(!openbci_get_config(&openbci_cyton_dev, &cfg));
            serial.ops->receive_buf(&serial, frame, sizeof(frame));
            assert(!openbci_cyton_dev.ringbuf->writes);
        } else assert(!commands && openbci_cyton_dev.streaming);
        admission_observe = admission_fault = false;
    } else if (!strncmp(argv[1], "banner_",7)) {
        const char *kind=argv[1]+7;
        banner_length=snprintf((char *)banner,sizeof(banner),"OpenBCI V3 8-16 channel\r\nOn Board ADS1299 Device ID: 0x3E\r\n");
        bool good=!strcmp(kind,"min") || !strcmp(kind,"max") || !strcmp(kind,"sync") || !strcmp(kind,"version");
        if (!strcmp(kind,"id_suffix")) { memmove(banner+banner_length-1,banner+banner_length-2,2); banner[banner_length-2]='0'; banner_length++; }
        if (!strcmp(kind,"prefix")) { memmove(banner+5,banner,banner_length); memcpy(banner,"fake ",5); banner_length+=5; }
        if (!strcmp(kind,"4ch")) banner[11]='4';
        if (!strcmp(kind,"empty")) { banner[0]='\n'; banner_length=1; }
        if (!strcmp(kind,"max") || !strcmp(kind,"long")) { size_t target=!strcmp(kind,"max")?255:256; memset(banner+banner_length,'V',target-banner_length); banner_length=target; }
        if (!strcmp(kind,"daisy")) { memcpy(banner+banner_length,"Daisy\r\n",7); banner_length+=7; }
        if (!strcmp(kind,"nul") || !strcmp(kind,"control") || !strcmp(kind,"high") || !strcmp(kind,"tab")) {
            banner[banner_length++]=!strcmp(kind,"nul")?0:!strcmp(kind,"control")?1:!strcmp(kind,"high")?128:'\t';
        }
        if (!strcmp(kind,"version")) banner_length+=snprintf((char *)banner+banner_length,sizeof(banner)-banner_length,"Firmware: arbitrary future version!?\r\n");
        if (!strcmp(kind,"sync")) reply_fragment=4096;
        setup(); int ret=openbci_init(&openbci_cyton_dev);
        assert(good ? ret==0 : ret<0);
        if (!good) { struct xtos_bci_config_v1 cfg={0}; assert(openbci_get_config(&openbci_cyton_dev,&cfg)==-ENODATA); }
    } else if (!strcmp(argv[1],"wait_delayed") || !strcmp(argv[1],"wait_cancel")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev)); commands=0;
        missing_reply=true; observe_wait=true; cancel_wait=!strcmp(argv[1],"wait_cancel");
        pthread_t reply; assert(!pthread_create(&reply,NULL,reply_thread,NULL));
        assert(openbci_start_stream(&openbci_cyton_dev)==(cancel_wait?-ENODEV:0));
        assert(!pthread_join(reply,NULL));
        assert(wait_entered && commands==(cancel_wait?1u:2u));
        if (!cancel_wait) assert(!memcmp(command_bytes,"?b",2));
        observe_wait=false;
    } else if (!strncmp(argv[1],"wait_",5)) {
        setup(); assert(!openbci_init(&openbci_cyton_dev)); commands=0; host_wait_calls=0;
        int expected=0;
        if (!strcmp(argv[1],"wait_signal") || !strcmp(argv[1],"wait_error")) {
            missing_reply=true; host_wait_result=!strcmp(argv[1],"wait_signal")?-ERESTARTSYS:-EIO; expected=(int)host_wait_result;
            write_result=-ETIMEDOUT; fail_only_stop=true;
        } else {
            host_fixed_clock=true; host_clock=!strcmp(argv[1],"wait_wrap")?~0UL-100:1000;
            host_tx_ticks=!strcmp(argv[1],"wait_expired")?300:299;
            if (host_tx_ticks==300) expected=-ETIMEDOUT;
        }
        int ret=openbci_start_stream(&openbci_cyton_dev); assert(ret==expected);
        if (expected) {
            struct xtos_bci_config_v1 cfg; memset(&cfg,0x5a,sizeof(cfg));
            assert(openbci_get_config(&openbci_cyton_dev,&cfg)==-ENODATA);
            assert(cfg.version==0x5a5a5a5a && commands==2 && !memcmp(command_bytes,"?s",2));
            assert(!openbci_cyton_dev.streaming);
        } else assert(host_wait_calls==1 && host_wait_budget==1);
        if (host_tx_ticks==300) assert(!host_wait_calls);
        host_fixed_clock=false; host_wait_result=1; write_result=-999; fail_only_stop=false;
    } else if (!strcmp(argv[1], "config_transaction")) {
        setup();
        assert(openbci_init(&openbci_cyton_dev) == 0);
        assert(commands == 3 && !memcmp(command_bytes, "sv?", 3));
    } else if (!strcmp(argv[1], "gain_refresh")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        board_regs[5]=0x58;
        commands=0;
        inject_tx=true;
        assert(!openbci_start_stream(&openbci_cyton_dev));
        assert(commands==2 && !memcmp(command_bytes,"?b",2));
        assert(openbci_cyton_dev.ringbuf->last[0]==375000000);
    } else if (!strcmp(argv[1], "fault_stats_lock")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        board_regs[5]=0x08;
        assert(!openbci_start_stream(&openbci_cyton_dev));
        fault_observe=true;
        pthread_t reset; assert(!pthread_create(&reset,NULL,reset_fault_thread,NULL));
        serial.ops->receive_buf(&serial,frame,33);
        assert(!pthread_join(reset,NULL));
        assert(openbci_cyton_dev.errors==1 && fault_attempted);
        fault_observe=false;
    } else if (!strcmp(argv[1], "conversion_fault")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        board_regs[5]=0x08;
        inject_tx=true;
        assert(openbci_start_stream(&openbci_cyton_dev)==-ERANGE);
        assert(openbci_cyton_dev.acquisition_error==-ERANGE);
        assert(!openbci_cyton_dev.streaming);
        assert(!openbci_cyton_dev.ringbuf->writes);
    } else if (!strcmp(argv[1], "atomic_gain")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        assert(openbci_ops.set_channel_config);
        assert(!openbci_ops.set_channel_config(&openbci_cyton_dev,0,false,12));
        struct xtos_bci_config_v1 cfg={0};
        assert(openbci_ops.get_config);
        assert(!openbci_ops.get_config(&openbci_cyton_dev,&cfg));
        assert(cfg.gain[0]==12 && cfg.enabled_mask==254 && cfg.generation>1);
        assert(cfg.capabilities==7 && cfg.registers[5]==0xd8);
    } else if (!strcmp(argv[1], "enable_mismatch")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        no_effect=true;
        assert(openbci_set_channel_enable(&openbci_cyton_dev,0,false)==-EPROTO);
        struct xtos_bci_config_v1 cfg={0};
        assert(openbci_ops.get_config(&openbci_cyton_dev,&cfg)==-ENODATA);
    } else if (!strcmp(argv[1], "cancel_transaction")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        assert(openbci_ops.cancel);
        openbci_ops.cancel(&openbci_cyton_dev);
        unsigned int before=commands;
        assert(openbci_start_stream(&openbci_cyton_dev)==-ENODEV);
        assert(commands==before);
    } else if (!strcmp(argv[1], "rate_preserves")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        board_regs[5]=0x58; board_regs[13]=board_regs[14]=0x55; board_regs[21]=0x20; board_regs[15]=3;
        unsigned char before[24]; memcpy(before,board_regs,24); commands=0;
        assert(!openbci_set_sampling_rate(&openbci_cyton_dev,250));
        assert(!memcmp(before,board_regs,24));
        assert(commands==1 && command_bytes[0]=='?');
        struct xtos_bci_config_v1 cfg={0};
        assert(!openbci_get_config(&openbci_cyton_dev,&cfg));
        assert(!memcmp(cfg.registers,before,24) && cfg.gain[0]==12);
    } else if (!strcmp(argv[1], "rate_mismatch")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        board_regs[1]=0x95;
        commands=0;
        assert(openbci_set_sampling_rate(&openbci_cyton_dev,250)==-EPROTO);
        assert(board_regs[1]==0x95 && commands==1 && command_bytes[0]=='?');
        struct xtos_bci_config_v1 cfg={0};
        assert(openbci_ops.get_config(&openbci_cyton_dev,&cfg)==-ENODATA);
    } else if (!strcmp(argv[1], "failed_command_invalidates")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        write_result=0; fail_only_start=true;
        assert(openbci_start_stream(&openbci_cyton_dev)==-EIO);
        struct xtos_bci_config_v1 cfg={0};
        assert(openbci_ops.get_config(&openbci_cyton_dev,&cfg)==-ENODATA);
        write_result=-999;
    } else if (!strcmp(argv[1], "missing_rows") || !strcmp(argv[1], "reserved_gain") ||
               !strcmp(argv[1], "wrong_ack") || !strcmp(argv[1], "bias_asymmetry") ||
               !strcmp(argv[1], "generation_overflow") || !strcmp(argv[1], "reply_timeout")) {
        setup(); assert(!openbci_init(&openbci_cyton_dev));
        if (!strcmp(argv[1], "missing_rows")) missing_row=true;
        if (!strcmp(argv[1], "reserved_gain")) board_regs[5]=0x78;
        if (!strcmp(argv[1], "wrong_ack")) {
            wrong_ack=true;
            assert(openbci_ops.set_channel_config(&openbci_cyton_dev,0,true,12)==-EPROTO);
        } else if (!strcmp(argv[1], "bias_asymmetry")) {
            board_regs[14]=254;
            unsigned int before=commands;
            assert(openbci_ops.set_channel_config(&openbci_cyton_dev,0,true,12)==-EOPNOTSUPP);
            assert(commands==before+1 && command_bytes[before]=='?');
        } else {
            if (!strcmp(argv[1], "generation_overflow")) ((struct openbci_priv *)openbci_cyton_dev.hw_private)->generation=~(u64)0;
            if (!strcmp(argv[1], "reply_timeout")) missing_reply=true;
            unsigned int before=commands;
            int expected=!strcmp(argv[1], "generation_overflow") ? -EOVERFLOW : missing_reply ? -ETIMEDOUT : -EPROTO;
            assert(openbci_start_stream(&openbci_cyton_dev)==expected);
            assert(commands==before+2 && !memcmp(command_bytes+before,"?s",2));
        }
    } else if (!strcmp(argv[1], "probe")) {
        inject_open = true;
        setup();
        assert(!openbci_cyton_dev.ringbuf);
        assert(openbci_init(&openbci_cyton_dev) == 0);
    } else if (!strcmp(argv[1], "start")) {
        setup();
        assert(openbci_start_stream(&openbci_cyton_dev) == -ENODEV);
        assert(commands == 0);
        assert(openbci_init(&openbci_cyton_dev) == 0);
        inject_tx = true;
        assert(openbci_start_stream(&openbci_cyton_dev) == 0);
        assert(openbci_cyton_dev.samples_read == 8);
        assert(openbci_cyton_dev.ringbuf->last[0] == 187500000);
        assert(openbci_cyton_dev.ringbuf->last[1] == -187500022);
        unsigned int before = commands;
        assert(openbci_start_stream(&openbci_cyton_dev) == 0);
        assert(commands == before);
        assert(openbci_stop_stream(&openbci_cyton_dev) == 0);
        before = commands;
        assert(openbci_stop_stream(&openbci_cyton_dev) == 0);
        assert(commands == before);
    } else if (!strcmp(argv[1], "controls")) {
        setup();
        assert(openbci_init(&openbci_cyton_dev) == 0);
        commands = 0;
        for (int ch = 0; ch < 8; ch++) {
            assert(openbci_set_channel_enable(&openbci_cyton_dev, ch, true) == 0);
            assert(openbci_set_channel_enable(&openbci_cyton_dev, ch, false) == 0);
        }
        for (int i=0;i<16;i++) { assert(command_bytes[i*3]=='?'); assert(command_bytes[i*3+1]=="!1@2#3$4%5^6&7*8"[i]); assert(command_bytes[i*3+2]=='?'); }
        unsigned int before = commands;
        assert(openbci_set_channel_enable(&openbci_cyton_dev, -1, true) == -EINVAL);
        assert(openbci_set_channel_enable(&openbci_cyton_dev, 8, true) == -EINVAL);
        assert(openbci_calibrate(&openbci_cyton_dev) == -EOPNOTSUPP);
        assert(openbci_impedance_check(&openbci_cyton_dev, NULL) == -EOPNOTSUPP);
        for (int rate = 500; rate <= 2000; rate *= 2)
            assert(openbci_set_sampling_rate(&openbci_cyton_dev, rate) == -EOPNOTSUPP);
        assert(openbci_set_sampling_rate(&openbci_cyton_dev, 123) == -EINVAL);
        assert(commands == before);
        write_result = 0;
        assert(openbci_set_sampling_rate(&openbci_cyton_dev, 250) == -EIO);
        write_result = -ETIMEDOUT;
        assert(openbci_set_sampling_rate(&openbci_cyton_dev, 250) == -ETIMEDOUT);
        assert(openbci_set_channel_enable(&openbci_cyton_dev, 0, true) == -ETIMEDOUT);
        write_result = 0;
        assert(openbci_set_channel_enable(&openbci_cyton_dev, 0, true) == -EIO);
        write_result = -999;
        assert(openbci_set_sampling_rate(&openbci_cyton_dev, 250) == 0);
        assert(command_bytes[commands-1]=='?');
        assert(openbci_start_stream(&openbci_cyton_dev) == 0);
        before = commands;
        assert(openbci_set_channel_enable(&openbci_cyton_dev, 0, true) == -EBUSY);
        assert(openbci_set_sampling_rate(&openbci_cyton_dev, 250) == -EBUSY);
        assert(commands == before);
    } else if (!strcmp(argv[1], "stats")) {
        setup();
        assert(openbci_init(&openbci_cyton_dev) == 0);
        assert(openbci_start_stream(&openbci_cyton_dev) == 0);
        const unsigned int counters[] = {254, 255, 1, 4, 4, 3, 5};
        for (unsigned int i = 0; i < sizeof(counters)/sizeof(counters[0]); i++) {
            packet(counters[i]);
            assert(serial.ops->receive_buf(&serial, frame, 10) == 10);
            assert(serial.ops->receive_buf(&serial, frame + 10, 23) == 23);
        }
        assert(openbci_cyton_dev.samples_read == 40);
        assert(openbci_cyton_dev.samples_dropped == 24);
        assert(openbci_cyton_dev.errors == 2);
        packet(6); frame[32] = 0;
        serial.ops->receive_buf(&serial, frame, 33);
        assert(openbci_cyton_dev.errors == 3);
        assert(openbci_cyton_dev.samples_dropped == 24);
        ring_result = -EIO; packet(6);
        serial.ops->receive_buf(&serial, frame, 33);
        assert(openbci_cyton_dev.errors == 4);
        assert(openbci_cyton_dev.samples_dropped == 32);
        ring_result = 16; packet(7);
        serial.ops->receive_buf(&serial, frame, 33);
        assert(openbci_cyton_dev.samples_dropped == 40);
        assert(openbci_cyton_dev.samples_read == 40);
    } else if (!strcmp(argv[1], "failed_start")) {
        setup();
        assert(openbci_init(&openbci_cyton_dev) == 0);
        commands = 0;
        inject_tx = true;
        write_result = 0;
        fail_only_start = true;
        assert(openbci_start_stream(&openbci_cyton_dev) == -EIO);
        assert(commands == 3 && !memcmp(command_bytes, "?bs", 3));
        fail_only_start = false;
        assert(!openbci_cyton_dev.streaming);
        unsigned long before = openbci_cyton_dev.samples_read;
        packet(2);
        serial.ops->receive_buf(&serial, frame, 33);
        assert(openbci_cyton_dev.samples_read == before);
        write_result = -999;
        assert(openbci_start_stream(&openbci_cyton_dev) == 0);
        write_result = -ETIMEDOUT;
        assert(openbci_stop_stream(&openbci_cyton_dev) == -ETIMEDOUT);
        assert(!openbci_cyton_dev.streaming);
        write_result = -999;
    } else if (!strcmp(argv[1], "reopen")) {
        setup();
        assert(openbci_init(&openbci_cyton_dev) == 0);
        struct xtos_bci_ringbuf *rb = openbci_cyton_dev.ringbuf;
        assert(openbci_init(&openbci_cyton_dev) == 0);
        assert(openbci_cyton_dev.ringbuf == rb && allocations == 1);
        assert(openbci_start_stream(&openbci_cyton_dev) == 0);
        inject_tx = true;
        unsigned long before = openbci_cyton_dev.samples_read;
        openbci_cleanup(&openbci_cyton_dev);
        assert(openbci_cyton_dev.ringbuf);
        serial.ops->receive_buf(&serial, frame, 33);
        assert(openbci_cyton_dev.samples_read == before);
        inject_tx = false;
        assert(openbci_init(&openbci_cyton_dev) == 0);
        assert(openbci_start_stream(&openbci_cyton_dev) == 0);
    } else if (!strcmp(argv[1], "concurrent_control")) {
        pthread_t ctl, cleanup;
        setup();
        assert(openbci_init(&openbci_cyton_dev) == 0);
        block_tx = true;
        assert(!pthread_create(&ctl, NULL, control_thread, NULL));
        wait_flag(&tx_entered);
        /* Complete receive synchronously while control is stuck in TX. */
        serial.ops->receive_buf(&serial, frame, 33);
        assert(!pthread_create(&cleanup, NULL, cleanup_thread, NULL));
        assert(!pthread_mutex_lock(&gate));
        while (!cleanup_control_waiting && !cleanup_done)
            assert(!pthread_cond_wait(&changed, &gate));
        assert(!cleanup_done && frees == 0);
        assert(!pthread_mutex_unlock(&gate));
        release_flag(&release_tx);
        assert(!pthread_join(ctl, NULL));
        assert(!pthread_join(cleanup, NULL));
        assert(openbci_cyton_dev.ringbuf && frees == 0);
    } else if (!strcmp(argv[1], "cleanup_receive")) {
        pthread_t rx, cleanup;
        setup();
        assert(openbci_init(&openbci_cyton_dev) == 0);
        assert(openbci_start_stream(&openbci_cyton_dev) == 0);
        block_ring = true;
        assert(!pthread_create(&rx, NULL, receive_thread, NULL));
        wait_flag(&ring_entered);
        assert(!pthread_create(&cleanup, NULL, cleanup_thread, NULL));
        wait_flag(&cleanup_state_waiting);
        assert(!pthread_mutex_lock(&gate));
        assert(!cleanup_done && frees == 0);
        assert(!pthread_mutex_unlock(&gate));
        release_flag(&release_ring);
        assert(!pthread_join(rx, NULL));
        assert(!pthread_join(cleanup, NULL));
        unsigned long before = openbci_cyton_dev.samples_read;
        assert(before == 8 && !openbci_cyton_dev.streaming);
        serial.ops->receive_buf(&serial, frame, 33);
        assert(openbci_cyton_dev.samples_read == before);
        assert(openbci_cyton_dev.ringbuf && frees == 0);
    } else {
        assert(!"unknown test");
    }
    inject_tx = false;
    openbci_cleanup(&openbci_cyton_dev);
    if (openbci_cyton_dev.ringbuf) xtos_bci_ringbuf_free(&serial.dev, openbci_cyton_dev.ringbuf);
    openbci_cyton_dev.ringbuf = NULL;
    openbci_remove(&serial);
    assert(allocations == frees);
    printf("PASS %s\n", argv[1]);
    return 0;
}
