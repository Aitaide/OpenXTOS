#include "xtos_xr/runtime_adapter.h"
#include <assert.h>
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
 assert(argc == 3);
 if (!strcmp(argv[1], "missing_loader")) {
  xtos_xr_adapter a = {0};
  assert(xtos_xr_open(&a,"/no/xtos/loader.so") == XTOS_XR_UNAVAILABLE);
  assert(!a.library && !a.instance);
  xtos_xr_close(&a); xtos_xr_close(&a);
 } else if (!strcmp(argv[1], "loader")) {
  void *lib=dlopen(argv[2],RTLD_NOW|RTLD_LOCAL); assert(lib);
  void (*reset)(int)=(void(*)(int))dlsym(lib,"fixture_reset");
  int (*made)(void)=(int(*)(void))dlsym(lib,"fixture_created");
  int (*gone)(void)=(int(*)(void))dlsym(lib,"fixture_destroyed");
  int (*queries)(void)=(int(*)(void))dlsym(lib,"fixture_ext_queries");
  for(int mode=0;mode<=5;mode++) {
   reset(mode); xtos_xr_adapter a={0};
   int rc=xtos_xr_open(&a,argv[2]);
   if(mode<3) { assert(rc==0); assert(a.instance); assert(a.timespec_enabled==(mode!=2)); assert(queries()==(mode==2?0:2)); }
   else { assert(rc<0); assert(!a.instance && !a.library); }
   xtos_xr_close(&a);xtos_xr_close(&a);assert(made()==gone());
  }
  xtos_xr_adapter a={0}; assert(xtos_xr_open(&a,"relative.so")==XTOS_XR_INVALID);
  dlclose(lib);
 } else if (!strcmp(argv[1],"events")) {
  xtos_xr_adapter a={0}; assert(xtos_xr_open(&a,argv[2])==0);
  xtos_xr_event e; memset(&e,0x7f,sizeof e);
  assert(xtos_xr_poll(&a,&e)==XTOS_XR_EMPTY);assert(e.sequence==0);assert(a.sequence==0);
  xtos_xr_close(&a);
  void *lib=dlopen(argv[2],RTLD_NOW|RTLD_LOCAL);assert(lib);
  void (*reset)(int)=(void(*)(int))dlsym(lib,"fixture_reset");reset(6);
  assert(xtos_xr_open(&a,argv[2])==0);
  for(int n=1;n<=3;n++) { assert(xtos_xr_poll(&a,&e)==0);assert(e.sequence==(uint64_t)n);assert(e.barrier==(uint64_t)n);assert(e.generation==a.generation);
   if(n==1) assert(e.has_lost_count && e.lost_count==7);
   if(n==2) assert(e.has_session_state && e.session_state==XR_SESSION_STATE_STOPPING);
   if(n==3) assert(e.type==1234567 && !e.has_session_state && !e.has_lost_count);
  }
  assert(xtos_xr_poll(&a,&e)==XTOS_XR_RUNTIME);assert(a.barrier==4);assert(!a.instance && !a.library);
  xtos_xr_close(&a);dlclose(lib);
 } else if (!strcmp(argv[1],"clock")) {
  xtos_xr_adapter a={0};xtos_xr_conversion c;
  assert(xtos_xr_open(&a,argv[2])==0);
  assert(xtos_xr_time_to_host(&a,12,&c)==0);assert(c.raw_xr_time==12 && c.host_ns==1012);
  assert(xtos_xr_time_to_host(&a,13,&c)==0);assert(c.host_ns==2013);
  assert(xtos_xr_host_to_time(&a,12,&c)==0);assert(c.raw_host_ns==12 && c.xr_time==3012);
  assert(xtos_xr_time_to_host(&a,0,&c)==XTOS_XR_INVALID && c.raw_xr_time==0);
  assert(xtos_xr_host_to_time(&a,-1,&c)==XTOS_XR_INVALID);
  assert(xtos_xr_time_to_host(&a,20,&c)==XTOS_XR_INVALID && !c.host_ns);
  assert(!a.instance);assert(xtos_xr_open(&a,argv[2])==0);
  assert(xtos_xr_time_to_host(&a,21,&c)==XTOS_XR_INVALID);
  assert(!a.instance);assert(xtos_xr_open(&a,argv[2])==0);
  assert(xtos_xr_time_to_host(&a,23,&c)==XTOS_XR_INVALID);
  assert(!a.instance);assert(xtos_xr_open(&a,argv[2])==0);
  assert(xtos_xr_time_to_host(&a,22,&c)==XTOS_XR_RUNTIME && c.runtime_result==XR_ERROR_RUNTIME_FAILURE);
  assert(!a.instance && !a.library);
  void *lib=dlopen(argv[2],RTLD_NOW|RTLD_LOCAL);assert(lib);
  void (*reset)(int)=(void(*)(int))dlsym(lib,"fixture_reset");reset(2);
  assert(xtos_xr_open(&a,argv[2])==0);assert(xtos_xr_time_to_host(&a,12,&c)==XTOS_XR_NO_CONVERSION);
  assert(xtos_xr_host_to_time(&a,12,&c)==XTOS_XR_NO_CONVERSION);
  xtos_xr_close(&a);dlclose(lib);
  xtos_xr_close(&a);
 } else if (!strcmp(argv[1],"destroy_failure") || !strcmp(argv[1],"missing_destroy_export")) {
  void *lib=dlopen(argv[2],RTLD_NOW|RTLD_LOCAL);assert(lib);
  void (*reset)(int)=(void(*)(int))dlsym(lib,"fixture_reset");
  int (*made)(void)=(int(*)(void))dlsym(lib,"fixture_created");
  int (*gone)(void)=(int(*)(void))dlsym(lib,"fixture_destroyed");
  reset(7);xtos_xr_adapter a={0};
  assert(xtos_xr_open(&a,argv[2])==XTOS_XR_UNAVAILABLE);
  if(!strcmp(argv[1],"missing_destroy_export")) assert(made()==0);
  else {assert(made()==1);assert(gone()==1);}
  assert(!a.instance && !a.library);xtos_xr_close(&a);assert(made()==gone());dlclose(lib);
 } else if (!strcmp(argv[1],"boundaries")) {
  void *lib=dlopen(argv[2],RTLD_NOW|RTLD_LOCAL);assert(lib);
  void (*reset)(int)=(void(*)(int))dlsym(lib,"fixture_reset");
  int (*made)(void)=(int(*)(void))dlsym(lib,"fixture_created");
  int (*gone)(void)=(int(*)(void))dlsym(lib,"fixture_destroyed");
  int modes[]={8,3,9,10};
  for(unsigned n=0;n<sizeof modes/sizeof *modes;n++) {
   reset(modes[n]);xtos_xr_adapter a={0};int rc=xtos_xr_open(&a,argv[2]);
   if(n==0) {assert(rc==0);assert(made()==1);} else {assert(rc==XTOS_XR_UNAVAILABLE);assert(made()==0);}
   xtos_xr_close(&a);assert(made()==gone());
  }
  reset(11);xtos_xr_adapter a={0};assert(xtos_xr_open(&a,argv[2])==0);
  const int32_t types[]={XR_TYPE_EVENT_DATA_EVENTS_LOST,XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED,XR_TYPE_EVENT_DATA_INSTANCE_LOSS_PENDING,XR_TYPE_EVENT_DATA_REFERENCE_SPACE_CHANGE_PENDING,XR_TYPE_EVENT_DATA_INTERACTION_PROFILE_CHANGED};
  for(unsigned n=0;n<5;n++) {xtos_xr_event e;assert(xtos_xr_poll(&a,&e)==0);assert(e.type==types[n]);assert(e.sequence==n+1 && e.barrier==n+1);}
  xtos_xr_close(&a);
  reset(12);assert(xtos_xr_open(&a,argv[2])==0);xtos_xr_conversion c;
  int64_t edges[]={1,INT64_MAX-1,INT64_MAX};
  for(unsigned n=0;n<3;n++) {
   assert(xtos_xr_time_to_host(&a,edges[n],&c)==0 && c.host_ns==edges[n] && c.raw_xr_time==edges[n]);
   assert(xtos_xr_host_to_time(&a,edges[n],&c)==0 && c.xr_time==edges[n] && c.raw_host_ns==edges[n]);
  }
  xtos_xr_close(&a);reset(13);assert(xtos_xr_open(&a,argv[2])==0);
  assert(xtos_xr_time_to_host(&a,INT64_MAX,&c)==XTOS_XR_INVALID && !c.host_ns && !a.instance);
  for(int mode=14;mode<=16;mode++) {
   reset(mode);assert(xtos_xr_open(&a,argv[2])==0);
   assert(xtos_xr_host_to_time(&a,1,&c)==(mode==16?XTOS_XR_RUNTIME:XTOS_XR_INVALID));
   assert(c.raw_host_ns==1 && !c.xr_time && !a.instance && !a.library);
  }
  dlclose(lib);
 } else if (!strcmp(argv[1],"counter_sequence") || !strcmp(argv[1],"counter_barrier") || !strcmp(argv[1],"counter_conversion")) {
  void *lib=dlopen(argv[2],RTLD_NOW|RTLD_LOCAL);assert(lib);
  void (*reset)(int)=(void(*)(int))dlsym(lib,"fixture_reset");reset(11);
  xtos_xr_adapter a={0};assert(xtos_xr_open(&a,argv[2])==0);xtos_xr_event e;
  if(!strcmp(argv[1],"counter_sequence")) {
   a.sequence=UINT64_MAX-1;assert(xtos_xr_poll(&a,&e)==0);assert(e.sequence==UINT64_MAX);
  } else {
   a.barrier=UINT64_MAX-1;assert(xtos_xr_poll(&a,&e)==0);assert(e.barrier==UINT64_MAX);
  }
  if(!strcmp(argv[1],"counter_conversion")) {xtos_xr_conversion c;assert(xtos_xr_time_to_host(&a,20,&c)==XTOS_XR_LIMIT);assert(c.status==XTOS_XR_LIMIT);}
  else assert(xtos_xr_poll(&a,&e)==XTOS_XR_LIMIT);
  assert(!a.instance && !a.library);
  assert(a.sequence==UINT64_MAX || a.barrier==UINT64_MAX);xtos_xr_close(&a);dlclose(lib);
 } else if (!strcmp(argv[1],"counter_generation")) {
  xtos_xr_adapter a={0};assert(xtos_xr_open(&a,argv[2])==0);assert(a.generation==UINT64_MAX);xtos_xr_close(&a);
  assert(xtos_xr_open(&a,argv[2])==XTOS_XR_LIMIT);assert(!a.instance && !a.library);assert(a.generation==UINT64_MAX);
 } else if (!strcmp(argv[1],"destroy_error_retry")) {
  void *lib=dlopen(argv[2],RTLD_NOW|RTLD_LOCAL);assert(lib);
  void (*reset)(int)=(void(*)(int))dlsym(lib,"fixture_reset");
  void (*allow)(void)=(void(*)(void))dlsym(lib,"fixture_allow_destroy");
  int (*made)(void)=(int(*)(void))dlsym(lib,"fixture_created");
  int (*gone)(void)=(int(*)(void))dlsym(lib,"fixture_destroyed");
  for(int mode=17;mode<=18;mode++) {
   reset(mode);xtos_xr_adapter a={0};assert(xtos_xr_open(&a,argv[2])==0);
   xtos_xr_close(&a);
   assert(a.last_result==XR_ERROR_RUNTIME_FAILURE);
   assert(a.instance && a.library && a.destroy && made()==1 && gone()==0);
   assert(xtos_xr_open(&a,argv[2])==XTOS_XR_INVALID);
   if(mode==18) {xtos_xr_close(&a);assert(a.instance && gone()==0);allow();}
   xtos_xr_close(&a);assert(!a.instance && !a.library && made()==gone());
   xtos_xr_close(&a);assert(gone()==1);
  }
  dlclose(lib);
 } else { assert(!"unknown case"); }
 puts("simulated ABI test PASS; physical_acceptance=false; safe_action=no_action");
 return 0;
}
