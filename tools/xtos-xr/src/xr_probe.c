#include "xtos_xr/runtime_adapter.h"
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
static int finish(int code) { if(fflush(stdout) || ferror(stdout)) {fputs("xr-probe output failure\n",stderr);return 4;}return code; }
static int close_bounded(xtos_xr_adapter *a) {
 xtos_xr_close(a);
 if(a->cleanup_failed) xtos_xr_close(a);
 return a->cleanup_failed;
}
int main(int argc,char **argv) {
 const char *path=0;int simulated=0;int64_t time=0;
 for(int i=1;i<argc;i++) {
  if(!strcmp(argv[i],"--loader") && i+1<argc) path=argv[++i];
  else if(!strcmp(argv[i],"--simulated")) simulated=1;
  else if(!strcmp(argv[i],"--xr-time") && i+1<argc) {
   const char *text=argv[++i];char *end;
   if(text[0]<'1' || text[0]>'9') goto usage;
   for(const char *p=text;*p;p++) if(*p<'0' || *p>'9') goto usage;
   errno=0;long long parsed=strtoll(text,&end,10);
   if(errno==ERANGE || end==text || *end || parsed<=0 || parsed>INT64_MAX) goto usage;
   time=(int64_t)parsed;
  }
  else goto usage;
 }
 if(!path || path[0]!='/') goto usage;
 xtos_xr_adapter a={0};int rc=xtos_xr_open(&a,path);
 if(rc) {int failed=close_bounded(&a);printf("{\"status\":\"%s\",\"safe_action\":\"no_action\",\"physical_acceptance\":false}\n",failed?"cleanup_failed":"unavailable");return finish(3);}
 printf("{\"source_kind\":\"%s\",\"safe_action\":\"no_action\",\"physical_acceptance\":false,\"generation\":%"PRIu64",\"events\":[",simulated?"simulated_runtime":"runtime_unverified",a.generation);
 unsigned count=0;xtos_xr_event e;
 for(;count<XTOS_XR_MAX_EVENTS;count++) {
  rc=xtos_xr_poll(&a,&e);if(rc) break;
  printf("%s{\"type\":%d,\"sequence\":%"PRIu64",\"barrier\":%"PRIu64,count?",":"",e.type,e.sequence,e.barrier);
  if(e.has_session_state) printf(",\"session_state\":%d",e.session_state);
  if(e.has_lost_count) printf(",\"lost_count\":%u",e.lost_count);
  putchar('}');
 }
 printf("],\"event_limit_reached\":%s,\"barrier\":%"PRIu64,count==XTOS_XR_MAX_EVENTS?"true":"false",a.barrier);
 if(time) {xtos_xr_conversion c;int status=xtos_xr_time_to_host(&a,time,&c);printf(",\"conversion\":{\"raw_xr_time\":%"PRId64",\"host_ns\":%"PRId64",\"runtime_result\":%d,\"status\":%d}",c.raw_xr_time,c.host_ns,c.runtime_result,status);if(status) rc=status;}
 int failed=close_bounded(&a);
 printf(",\"status\":\"%s\"}\n",failed?"cleanup_failed":(rc<0?"runtime_fault":"ok"));return finish(failed || rc<0?3:0);
usage:
 puts("{\"status\":\"usage_error\",\"safe_action\":\"no_action\",\"physical_acceptance\":false}");return finish(2);
}
