/* Opt-in, headless runtime sampling; no pose is authorized to trigger an action.
 * Runtime-reported sample time is a pose-expression time, not verified acquisition.
 * Duration bounds cooperative polling, not arbitrary third-party runtime calls. */
#include "xtos_xr/eye_session.h"
#include <inttypes.h>
#include <stdarg.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <unistd.h>

typedef struct {const char *loader;int consent,simulated,max_samples,duration_ms;} options;
static volatile sig_atomic_t stopping;
static void stop_signal(int signal_number) {(void)signal_number;stopping=1;}
static int decimal(const char *s,int max,int *out) {
 if(!s || !*s)return -1;
 unsigned v=0;
 for(;*s;s++){if(*s<'0'||*s>'9')return -1;v=v*10u+(unsigned)(*s-'0');if(v>(unsigned)max)return -1;}
 if(!v)return -1;
 *out=(int)v;return 0;
}
static int parse(int argc,char **argv,options *o) {
 unsigned seen=0;*o=(options){.max_samples=1,.duration_ms=1000};
 for(int i=1;i<argc;i++){
  unsigned bit=0;int param=0;
  if(!strcmp(argv[i],"--loader")){bit=1;param=1;}
  else if(!strcmp(argv[i],"--accept-eye-data"))bit=2;
  else if(!strcmp(argv[i],"--simulated"))bit=4;
  else if(!strcmp(argv[i],"--max-samples")){bit=8;param=1;}
  else if(!strcmp(argv[i],"--duration-ms")){bit=16;param=1;}
  else return -1;
  if((seen&bit)||(param&&++i>=argc))return -1;
  seen|=bit;
  if(bit==1)o->loader=argv[i];
  if(bit==2)o->consent=1;
  if(bit==4)o->simulated=1;
  if(bit==8 && decimal(argv[i],64,&o->max_samples))return -1;
  if(bit==16 && decimal(argv[i],5000,&o->duration_ms))return -1;
 }
 return !o->loader||o->loader[0]!='/'?-1:0;
}
static int64_t now_ns(void) {
 struct timespec ts;
 if(clock_gettime(CLOCK_MONOTONIC,&ts))return -1;
 return (int64_t)ts.tv_sec*INT64_C(1000000000)+ts.tv_nsec;
}
static void pace(void) {struct timespec slice={.tv_sec=0,.tv_nsec=5000000};(void)nanosleep(&slice,0);}
static int add(char *out,size_t capacity,size_t *used,const char *fmt,...) {
 va_list ap;va_start(ap,fmt);
 int n=vsnprintf(out+*used,capacity-*used,fmt,ap);va_end(ap);
 if(n<0 || (size_t)n>=capacity-*used)return -1;
 *used+=(size_t)n;return 0;
}
static int write_summary(const char *output,size_t length,int64_t deadline) {
 int flags=fcntl(STDOUT_FILENO,F_GETFL);
 if(flags<0 || fcntl(STDOUT_FILENO,F_SETFL,flags|O_NONBLOCK)<0)return -1;
 size_t sent=0;int result=0;
 while(sent<length){
  ssize_t n=write(STDOUT_FILENO,output+sent,length-sent);
  if(n>0){sent+=(size_t)n;continue;}
  if(n<0 && errno==EINTR){if(stopping){result=-1;break;}continue;}
  if(n<0 && (errno==EAGAIN || errno==EWOULDBLOCK)){
   int64_t current=now_ns();
   if(stopping || current<=0 || current>=deadline){result=-1;break;}
   int64_t ms=(deadline-current+999999)/1000000;
   struct pollfd fd={.fd=STDOUT_FILENO,.events=POLLOUT};
   int rc=poll(&fd,1,ms>5?5:(int)ms);
   if(rc<0 && errno!=EINTR){result=-1;break;}
   if(rc>0 && (fd.revents&(POLLERR|POLLHUP|POLLNVAL))){result=-1;break;}
   continue;
  }
  result=-1;break;
 }
 if(fcntl(STDOUT_FILENO,F_SETFL,flags)<0)result=-1;
 return result;
}
static void report_output_error(void) {
 /* Diagnostics must not reopen an unbounded wait after runtime cleanup.
  * One nonblocking attempt is best-effort, including EINTR and EAGAIN. */
 static const char message[]="stdout write or flush failed\n";
 int flags=fcntl(STDERR_FILENO,F_GETFL);
 if(flags<0 || fcntl(STDERR_FILENO,F_SETFL,flags|O_NONBLOCK)<0)return;
 (void)write(STDERR_FILENO,message,sizeof message-1);
 (void)fcntl(STDERR_FILENO,F_SETFL,flags);
}
static int emit(const options *o,const char *status,const xtos_xr_eye_sample *samples,int count,int64_t deadline) {
 char output[32768];size_t used=0;
 if(add(output,sizeof output,&used,"{\"status\":\"%s\",\"source_kind\":\"%s\",\"safe_action\":\"no_action\",\"physical_acceptance\":false,\"pose_time_provenance\":\"runtime_reported_unknown_prediction_or_interpolation\",\"sample_count\":%d,\"samples\":[",status,o->simulated?"simulated":"runtime_unverified",count))return -1;
 for(int i=0;i<count;i++){
  const xtos_xr_eye_sample *s=&samples[i];const XrPosef *p=&s->pose;
  if(add(output,sizeof output,&used,"%s{\"query_host_ns\":%" PRId64 ",\"query_xr_time\":%" PRId64 ",\"sample_xr_time\":%" PRId64 ",\"converted_host_ns\":%" PRId64 ",\"pose\":{\"position\":[%.9g,%.9g,%.9g],\"orientation\":[%.9g,%.9g,%.9g,%.9g]}}",i?",":"",s->query_host_ns,s->query_xr_time,s->sample_xr_time,s->sample_host_ns,(double)p->position.x,(double)p->position.y,(double)p->position.z,(double)p->orientation.x,(double)p->orientation.y,(double)p->orientation.z,(double)p->orientation.w))return -1;
 }
 if(add(output,sizeof output,&used,"]}\n"))return -1;
 return write_summary(output,used,deadline);
}
int main(int argc,char **argv) {
 options o;
 if(parse(argc,argv,&o)){fputs("invalid arguments: --loader /absolute/path --accept-eye-data [--simulated] [--max-samples 1..64] [--duration-ms 1..5000]\n",stderr);return 2;}
 if(!o.consent){fputs("--accept-eye-data is required for private eye data\n",stderr);return 2;}
 struct sigaction action={0};action.sa_handler=stop_signal;sigemptyset(&action.sa_mask);
 if(sigaction(SIGINT,&action,0) || sigaction(SIGTERM,&action,0)){fputs("signal setup failed\n",stderr);return 1;}
 struct sigaction pipe_action={0};pipe_action.sa_handler=SIG_IGN;sigemptyset(&pipe_action.sa_mask);
 if(sigaction(SIGPIPE,&pipe_action,0)){fputs("signal setup failed\n",stderr);return 1;}
 xtos_xr_adapter adapter={0};xtos_xr_eye_session eye={0};xtos_xr_eye_sample samples[64];int count=0;
 const char *status="no_sample";int failed=0;
 int64_t start=now_ns(),deadline=start+(int64_t)o.duration_ms*INT64_C(1000000);
 if(start<=0){status="clock_error";failed=1;goto output;}
 if(xtos_xr_open_features(&adapter,o.loader,XTOS_XR_FEATURE_HEADLESS_EYE)!=XTOS_XR_OK){status="runtime_unavailable";failed=1;goto cleanup;}
 if(xtos_xr_eye_open(&eye,&adapter)!=XTOS_XR_OK){status="eye_unavailable";failed=1;goto cleanup;}
 while(!stopping && count<o.max_samples){
  int64_t current=now_ns();
  if(current<0){status="clock_error";failed=1;break;}
  if(current>=deadline)break;
  /* At most four events per iteration, with a 5ms cooperative pacing slice. */
  for(int events=0;events<4;events++){
   int step=xtos_xr_eye_step(&eye);
   if(step==XTOS_XR_EMPTY)break;
   if(step!=XTOS_XR_OK){status="runtime_error";failed=1;break;}
  }
  if(failed)break;
  if(eye.running && eye.state==XR_SESSION_STATE_FOCUSED){
   current=now_ns();
   if(current<0){status="clock_error";failed=1;break;}
   if(current>=deadline)break;
   xtos_xr_eye_sample sample;
   int result=xtos_xr_eye_query(&eye,current,&sample);
   if(result==XTOS_XR_OK)samples[count++]=sample;
   else if(result!=XTOS_XR_UNAVAILABLE && result!=XTOS_XR_INVALID){status="runtime_error";failed=1;break;}
  }
  pace();
 }
 if(stopping){status="interrupted";failed=1;}
 else if(!failed && count==o.max_samples)status="complete";
 else if(!failed && count){status="partial";failed=1;}
 else if(!failed){status="no_sample";failed=1;}
cleanup:
 if(eye.adapter && eye.running && !eye.lost){
  if(xtos_xr_eye_request_exit(&eye)!=XTOS_XR_OK){status="runtime_error";failed=1;}
  else {
   int64_t end=now_ns()+INT64_C(100000000);
   while(eye.running && now_ns()<end){
    for(int events=0;events<4;events++){int step=xtos_xr_eye_step(&eye);if(step==XTOS_XR_EMPTY || step!=XTOS_XR_OK)break;}
    if(eye.running)pace();
   }
  }
 }
 if(eye.adapter){
  if(xtos_xr_eye_close(&eye)!=XTOS_XR_OK){status="cleanup_failed";failed=1;}
 }
 for(int attempt=0;attempt<2 && (adapter.instance || adapter.library);attempt++)xtos_xr_close(&adapter);
 if(adapter.instance || adapter.library || adapter.active_children || adapter.cleanup_failed){status="cleanup_failed";failed=1;}
output:
 if(emit(&o,status,samples,count,deadline)){report_output_error();return 1;}
 return failed?1:0;
}
