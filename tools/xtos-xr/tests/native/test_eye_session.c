#include "xtos_xr/runtime_adapter.h"
#include "xtos_xr/eye_session.h"
#include <assert.h>
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
int main(int argc,char **argv) {
 assert(argc==3);
 void *lib=dlopen(argv[2],RTLD_NOW|RTLD_LOCAL);assert(lib);
 void (*reset)(int)=(void(*)(int))dlsym(lib,"eye_reset");assert(reset);
 int (*created)(void)=(int(*)(void))dlsym(lib,"eye_instances");assert(created);
 xtos_xr_adapter a={0};
 if(!strcmp(argv[1],"extensions")) {
  for(int mode=1;mode<=1;mode++) {
   reset(mode);assert(xtos_xr_open_features(&a,argv[2],XTOS_XR_FEATURE_HEADLESS_EYE)==XTOS_XR_UNAVAILABLE);
   assert(created()==0 && !a.library && !a.instance);
  }
  reset(5);assert(xtos_xr_open_features(&a,argv[2],XTOS_XR_FEATURE_HEADLESS_EYE)==XTOS_XR_UNAVAILABLE);
  assert(created()==0 && !a.library && !a.instance);
 } else if(!strcmp(argv[1],"create")) {
  reset(0);
  assert(xtos_xr_open_features(&a,argv[2],XTOS_XR_FEATURE_HEADLESS_EYE)==XTOS_XR_OK);
  xtos_xr_eye_session eye={0};
  assert(xtos_xr_eye_open(&eye,&a)==XTOS_XR_OK);
  assert(eye.session && eye.eye_space && eye.reference_space && eye.action && eye.action_set);
  assert(a.active_children==1);
  xtos_xr_close(&a);assert(a.instance && a.library && a.cleanup_failed);
  xtos_xr_eye_close(&eye);assert(!eye.session && a.active_children==0);
  xtos_xr_close(&a);assert(!a.library && !a.instance);
  int (*gone)(void)=(int(*)(void))dlsym(lib,"eye_instance_destroys");assert(gone && gone()==1);
 } else if(!strcmp(argv[1],"missing_hmd")) {
  reset(11);assert(xtos_xr_open_features(&a,argv[2],XTOS_XR_FEATURE_HEADLESS_EYE)==0);
  xtos_xr_eye_session eye={0};assert(xtos_xr_eye_open(&eye,&a)==XTOS_XR_UNAVAILABLE);
  assert(!eye.session && !a.active_children);
  int (*made)(void)=(int(*)(void))dlsym(lib,"eye_sessions");assert(made && made()==0);
  xtos_xr_close(&a);
 } else if(!strcmp(argv[1],"close_retry")) {
  reset(8);assert(xtos_xr_open_features(&a,argv[2],XTOS_XR_FEATURE_HEADLESS_EYE)==0);
  xtos_xr_eye_session eye={0};assert(xtos_xr_eye_open(&eye,&a)==0);
  assert(xtos_xr_eye_close(&eye)==XTOS_XR_RUNTIME);
  assert(eye.session && a.active_children && a.cleanup_failed);
  int (*gone)(void)=(int(*)(void))dlsym(lib,"eye_instance_destroys");assert(gone && !gone());
  xtos_xr_close(&a);assert(a.instance && !gone());
  void (*allow)(void)=(void(*)(void))dlsym(lib,"eye_allow_cleanup");assert(allow);allow();
  assert(xtos_xr_eye_close(&eye)==0 && !a.active_children && !a.cleanup_failed);
  xtos_xr_close(&a);assert(gone()==1);
 } else if(!strcmp(argv[1],"open_retry")) {
  reset(7);assert(xtos_xr_open_features(&a,argv[2],XTOS_XR_FEATURE_HEADLESS_EYE)==0);
  xtos_xr_eye_session eye={0};
  assert(xtos_xr_eye_open(&eye,&a)==XTOS_XR_RUNTIME);
  assert(!eye.adapter && !eye.session && !a.active_children);
  void (*allow)(void)=(void(*)(void))dlsym(lib,"eye_allow_cleanup");assert(allow);allow();
  assert(xtos_xr_eye_open(&eye,&a)==XTOS_XR_OK);
  assert(xtos_xr_eye_close(&eye)==0);xtos_xr_close(&a);
 } else if(!strcmp(argv[1],"events")) {
  reset(0);assert(xtos_xr_open_features(&a,argv[2],XTOS_XR_FEATURE_HEADLESS_EYE)==XTOS_XR_OK);
  xtos_xr_eye_session eye={0};assert(xtos_xr_eye_open(&eye,&a)==0);
  assert(xtos_xr_eye_step(&eye)==0 && eye.state==XR_SESSION_STATE_READY && eye.running);
  int (*begun)(void)=(int(*)(void))dlsym(lib,"eye_begin_calls");assert(begun && begun()==1);
  assert(xtos_xr_eye_step(&eye)==0 && eye.state==XR_SESSION_STATE_FOCUSED);
  assert(xtos_xr_eye_step(&eye)==0 && eye.state==XR_SESSION_STATE_STOPPING && !eye.running);
  int (*ended)(void)=(int(*)(void))dlsym(lib,"eye_end_calls");assert(ended && ended()==1);
  assert(xtos_xr_eye_step(&eye)==XTOS_XR_EMPTY);
  assert(xtos_xr_eye_close(&eye)==0);xtos_xr_close(&a);
 } else if(!strcmp(argv[1],"sample")) {
  reset(0);assert(xtos_xr_open_features(&a,argv[2],XTOS_XR_FEATURE_HEADLESS_EYE)==0);
  xtos_xr_eye_session eye={0};assert(xtos_xr_eye_open(&eye,&a)==0);
  assert(xtos_xr_eye_step(&eye)==0 && eye.running);
  assert(xtos_xr_eye_step(&eye)==0 && eye.state==XR_SESSION_STATE_FOCUSED);
  xtos_xr_eye_sample s={0};
  assert(xtos_xr_eye_query(&eye,12,&s)==XTOS_XR_OK);
  assert(s.query_host_ns==12 && s.query_xr_time==12);
  assert(s.sample_xr_time==123456 && s.sample_host_ns==124456);
  assert(s.pose.orientation.w==1.f && s.pose.position.z==-1.f);
  int (*conversions)(void)=(int(*)(void))dlsym(lib,"eye_conversion_calls");assert(conversions && conversions()==1);
  assert(xtos_xr_eye_step(&eye)==0 && eye.state==XR_SESSION_STATE_STOPPING);
  assert(xtos_xr_eye_query(&eye,12,&s)==XTOS_XR_UNAVAILABLE);
  assert(xtos_xr_eye_close(&eye)==0);xtos_xr_close(&a);
 } else if(!strcmp(argv[1],"invalid_samples")) {
  for(int mode=12;mode<=16;mode++) {
   reset(mode);assert(xtos_xr_open_features(&a,argv[2],XTOS_XR_FEATURE_HEADLESS_EYE)==0);
   xtos_xr_eye_session eye={0};assert(xtos_xr_eye_open(&eye,&a)==0);
   assert(xtos_xr_eye_step(&eye)==0 && xtos_xr_eye_step(&eye)==0);
   xtos_xr_eye_sample s={0};assert(xtos_xr_eye_query(&eye,12,&s)!=XTOS_XR_OK);
   assert(!s.sample_xr_time && !s.sample_host_ns && !strcmp(s.safe_action,"no_action") && !s.physical_acceptance);
   assert(xtos_xr_eye_step(&eye)==0 && xtos_xr_eye_close(&eye)==0);xtos_xr_close(&a);
  }
 } else if(!strcmp(argv[1],"unsupported")) {
  for(int mode=2;mode<=6;mode+=4) {
   reset(mode);assert(xtos_xr_open_features(&a,argv[2],XTOS_XR_FEATURE_HEADLESS_EYE)==0);
   xtos_xr_eye_session eye={0};assert(xtos_xr_eye_open(&eye,&a)==XTOS_XR_UNAVAILABLE);
   assert(!a.active_children);xtos_xr_close(&a);
  }
 } else if(!strcmp(argv[1],"request_exit")) {
  reset(0);assert(xtos_xr_open_features(&a,argv[2],XTOS_XR_FEATURE_HEADLESS_EYE)==0);
  xtos_xr_eye_session eye={0};assert(xtos_xr_eye_open(&eye,&a)==0);
  assert(xtos_xr_eye_step(&eye)==0 && eye.running);
  assert(xtos_xr_eye_request_exit(&eye)==XTOS_XR_OK);
  int (*requests)(void)=(int(*)(void))dlsym(lib,"eye_exit_requests");assert(requests && requests()==1);
  assert(xtos_xr_eye_step(&eye)==0);
  assert(xtos_xr_eye_step(&eye)==0 && !eye.running);
  assert(xtos_xr_eye_close(&eye)==0);xtos_xr_close(&a);
 } else if(!strcmp(argv[1],"loss")) {
  reset(10);assert(xtos_xr_open_features(&a,argv[2],XTOS_XR_FEATURE_HEADLESS_EYE)==0);
  xtos_xr_eye_session eye={0};assert(xtos_xr_eye_open(&eye,&a)==0);
  assert(xtos_xr_eye_step(&eye)==0 && eye.running);
  assert(xtos_xr_eye_step(&eye)==XTOS_XR_UNAVAILABLE && eye.lost);
  xtos_xr_eye_sample s;assert(xtos_xr_eye_query(&eye,12,&s)==XTOS_XR_UNAVAILABLE);
  assert(!strcmp(s.safe_action,"no_action") && !s.physical_acceptance);
  assert(xtos_xr_eye_close(&eye)==XTOS_XR_OK);
  int (*ended)(void)=(int(*)(void))dlsym(lib,"eye_end_calls");assert(ended && ended()==0);
  xtos_xr_close(&a);assert(!a.instance);
 } else if(!strcmp(argv[1],"wrong_session")) {
  reset(9);assert(xtos_xr_open_features(&a,argv[2],XTOS_XR_FEATURE_HEADLESS_EYE)==0);
  xtos_xr_eye_session eye={0};assert(xtos_xr_eye_open(&eye,&a)==0);
  assert(xtos_xr_eye_step(&eye)==0 && eye.running);
  assert(xtos_xr_eye_step(&eye)==XTOS_XR_INVALID && eye.lost);
  /* Contract tightening: terminal loss cannot poll or apply queued STOPPING. */
  assert(xtos_xr_eye_step(&eye)==XTOS_XR_UNAVAILABLE && eye.lost && eye.state==XR_SESSION_STATE_READY);
  assert(xtos_xr_eye_close(&eye)==0);xtos_xr_close(&a);
 } else assert(!"unknown case");
 dlclose(lib);
 puts("simulated ABI eye test PASS; safe_action=no_action; physical_acceptance=false");
 return 0;
}
