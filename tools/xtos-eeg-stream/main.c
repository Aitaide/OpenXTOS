#define _POSIX_C_SOURCE 200809L
#include "eeg_export.h"
#include <errno.h>
#include <inttypes.h>
#include <time.h>
#include <signal.h>
#include <fcntl.h>
#include <poll.h>
#include <unistd.h>
static volatile sig_atomic_t cancelled;
static void cancel_handler(int signal_number) { cancelled=signal_number; }
static struct xtos_eeg_stream stream;
static uint64_t deadline;
static uint64_t clock_ns(void)
{
    struct timespec ts;
    if(clock_gettime(CLOCK_MONOTONIC,&ts))return 0;
    return (uint64_t)ts.tv_sec*1000000000u+(uint64_t)ts.tv_nsec;
}
/* Never buffer stdout in stdio: a deferred flush could block cleanup or exit.
 * All backpressure waits share the acquisition deadline. Terminal records get
 * an immediate best-effort write after timeout/cancel, never a fresh wait. */
static int output(const char *buffer,size_t length,int terminal)
{
    while(length) {
        uint64_t now=clock_ns();
        if(!terminal && (cancelled || !now || now>=deadline))return -EIO;
        ssize_t n=write(STDOUT_FILENO,buffer,length);
        if(n>0){buffer+=n;length-=(size_t)n;continue;}
        if(n<0 && errno==EINTR)continue;
        if(n>=0 || (errno!=EAGAIN && errno!=EWOULDBLOCK))return -EIO;
        now=clock_ns();
        if(cancelled || !now || now>=deadline)return -EIO;
        uint64_t remaining=(deadline-now+999999u)/1000000u;
        int wait_ms=remaining>10?10:(int)remaining;
        struct pollfd fd={.fd=STDOUT_FILENO,.events=POLLOUT};
        if(poll(&fd,1,wait_ms)<0 && errno!=EINTR)return -EIO;
    }
    return 0;
}
static int barrier(uint64_t *published,const char *reason,int terminal)
{
    while(*published<stream.barrier_id) {
        (*published)++;
        char buffer[512];
        int n=snprintf(buffer,sizeof(buffer),"{\"schema\":\"xtos.eeg.barrier.v1\",\"session_id\":%" PRIu64 ",\"barrier_id\":%" PRIu64 ",\"reason\":\"%s\",\"safe_action\":\"no_action\",\"physical_acceptance\":false}\n",stream.session_id,*published,reason);
        if(n<0 || (size_t)n>=sizeof(buffer) || output(buffer,(size_t)n,terminal))return -EIO;
    }
    return 0;
}
int main(int argc,char **argv)
{
    struct xtos_eeg_export_options o;
    if(xtos_eeg_export_parse(argc,argv,&o)) {fputs("invalid options\n",stderr);return 2;}
    struct xtos_eeg_window_options w={.version=1,.channels=8,.sample_rate_hz=250,
        .window_frames=o.window_frames,.hop_frames=o.hop_frames,.max_receive_age_ns=o.max_receive_age_ns};
    if(xtos_eeg_stream_init(&stream,&w,o.session_id))return 3;
    struct sigaction action={0};
    action.sa_handler=cancel_handler;sigemptyset(&action.sa_mask);
    if(sigaction(SIGINT,&action,NULL) || sigaction(SIGTERM,&action,NULL)) {
        fputs("signal setup failed\n",stderr);return 3;
    }
    /* Let stdout EPIPE reach the output-error path and owned cleanup. */
    action.sa_handler=SIG_IGN;
    if(sigaction(SIGPIPE,&action,NULL)) {
        fputs("signal setup failed\n",stderr);return 3;
    }
    xtos_bci_session_t *session=xtos_bci_session_create(o.device);
    if(!session){fputs("device setup failed\n",stderr);return 3;}
    uint32_t windows=0;uint64_t published=1,start=clock_ns();
    deadline=start+(uint64_t)o.duration_ms*1000000u;
    int stdout_flags=fcntl(STDOUT_FILENO,F_GETFL);
    if(stdout_flags<0 || fcntl(STDOUT_FILENO,F_SETFL,stdout_flags|O_NONBLOCK)<0) {
        xtos_bci_session_destroy(session);
        fputs("eeg-stream: output_failed\n",stderr);return 5;
    }
    int code=0;const char *reason="requested_windows";
    if(xtos_bci_start(session)) {code=3;reason="start_failed";}
    while(!code && windows<o.max_windows) {
        if(cancelled){code=4;reason="cancelled";break;}
        uint64_t now=clock_ns();
        if(!now || now<start || now-start>=(uint64_t)o.duration_ms*1000000u) {code=4;reason="duration_exhausted";break;}
        int r=xtos_eeg_stream_read(&stream,session,1);
        if(cancelled){code=4;reason="cancelled";break;}
        if(barrier(&published,"continuity",0)){code=5;reason="output_failed";break;}
        if(r==-EAGAIN || r==-EWOULDBLOCK) {
            struct timespec pause={.tv_nsec=1000000};nanosleep(&pause,NULL);continue;
        }
        if(r) {code=4;reason=r==1?"disabled_configuration":"read_failed";break;}
        struct xtos_eeg_stream_features f;
        now=clock_ns();
        if(!now || now<start || now-start>=(uint64_t)o.duration_ms*1000000u) {
            code=4;reason="duration_exhausted";break;
        }
        r=xtos_eeg_stream_features(&stream,now,&f);
        if(cancelled){code=4;reason="cancelled";break;}
        if(barrier(&published,"freshness",0)){code=5;reason="output_failed";break;}
        if(r==-EAGAIN)continue;
        if(r) {code=4;reason="stale_or_invalid_window";break;}
        now=clock_ns();
        if(!now || now<start || now-start>=(uint64_t)o.duration_ms*1000000u) {
            code=4;reason="duration_exhausted";break;
        }
        char buffer[4096];
        int length=xtos_eeg_export_format(buffer,sizeof(buffer),&o,&f,now);
        if(length<0 || output(buffer,(size_t)length,0)){code=5;reason="output_failed";break;}
        windows++;
    }
    if(cancelled && !code){code=4;reason="cancelled";}
    xtos_eeg_stream_barrier(&stream);
    if(xtos_bci_stop(session)) {
        fputs("eeg-stream: STOP failed\n",stderr);
        if(!code){code=4;reason="stop_failed";}
    }
    xtos_bci_session_destroy(session);
    if(code!=5 && barrier(&published,cancelled?"cancelled":"stop",1)){code=5;reason="output_failed";}
    if(cancelled && code!=5){code=4;reason="cancelled";}
    if(code!=5) {
        const char *status=cancelled?"interrupted":code==3?"error":code?"degraded":"complete";
        char buffer[512];
        int n=snprintf(buffer,sizeof(buffer),"{\"schema\":\"xtos.eeg.stream-summary.v1\",\"status\":\"%s\",\"reason\":\"%s\",\"windows_emitted\":%u,\"session_id\":%" PRIu64 ",\"barrier_id\":%" PRIu64 ",\"safe_action\":\"no_action\",\"physical_acceptance\":false}\n",status,reason,windows,o.session_id,stream.barrier_id);
        if(n<0 || (size_t)n>=sizeof(buffer) || output(buffer,(size_t)n,1)) {
            code=5;reason="output_failed";
        }
    }
    if(fcntl(STDOUT_FILENO,F_SETFL,stdout_flags)<0){code=5;reason="output_failed";}
    if(code)fprintf(stderr,"eeg-stream: %s\n",reason);
    return code;
}
