/* Security expectations; independent BUG-detecting originals remain immutable. */
#define main original_probe_main
#include "spec_original/probe.c"
#undef main
static int polls;
static XrResult XRAPI_CALL counted_poll(XrInstance i,XrEventDataBuffer *b){polls++;return poll_injected(i,b);}
static XrResult XRAPI_CALL dispatch_cleanup(XrInstance i,const char *n,PFN_xrVoidFunction *o){
 XrResult r=gipa(i,n,o);if(r!=XR_SUCCESS)return r;
 if(!strcmp(n,"xrDestroySpace")){ds=(PFN_xrDestroySpace)*o;*o=(PFN_xrVoidFunction)destroy_space;}
 if(!strcmp(n,"xrDestroyAction")){da=(PFN_xrDestroyAction)*o;*o=(PFN_xrVoidFunction)destroy_action;}
 if(!strcmp(n,"xrDestroyActionSet")){das=(PFN_xrDestroyActionSet)*o;*o=(PFN_xrVoidFunction)destroy_set;}
 if(!strcmp(n,"xrDestroySession")){dse=(PFN_xrDestroySession)*o;*o=(PFN_xrVoidFunction)destroy_session;}
 return r;
}
static void secure_events(void *lib,const char *path){
 for(int k=0;k<10;k++){
  xtos_xr_adapter a={0};xtos_xr_eye_session e={0};load(lib,path,&a,&e);
  assert(xtos_xr_eye_step(&e)==0 && xtos_xr_eye_step(&e)==0);
  xtos_xr_eye_sample s;assert(xtos_xr_eye_query(&e,12,&s)==0); /* equal time=100 stays valid */
  uint64_t b=a.barrier,q=a.sequence;
  memset(&injected,0,sizeof injected);
  if(k==0){XrEventDataEventsLost v={XR_TYPE_EVENT_DATA_EVENTS_LOST,0,3};memcpy(&injected,&v,sizeof v);}
  if(k==1||k==4){XrEventDataReferenceSpaceChangePending v={0};v.type=XR_TYPE_EVENT_DATA_REFERENCE_SPACE_CHANGE_PENDING;v.session=k==4?(XrSession)(uintptr_t)99:e.session;v.referenceSpaceType=XR_REFERENCE_SPACE_TYPE_LOCAL;v.changeTime=1;memcpy(&injected,&v,sizeof v);}
  if(k==2||k==5){XrEventDataInteractionProfileChanged v={XR_TYPE_EVENT_DATA_INTERACTION_PROFILE_CHANGED,0,k==5?(XrSession)(uintptr_t)99:e.session};memcpy(&injected,&v,sizeof v);}
  if(k==3)injected.type=(XrStructureType)2147480000;
  if(k==6){state(&e,XR_SESSION_STATE_VISIBLE,200);assert(xtos_xr_eye_step(&e)==0);b=a.barrier;q=a.sequence;state(&e,XR_SESSION_STATE_FOCUSED,99);}
  if(k==7)state(&e,XR_SESSION_STATE_FOCUSED,100);
  if(k==8){a.sequence=UINT64_MAX;q=a.sequence;state(&e,XR_SESSION_STATE_FOCUSED,100);}
  if(k==9){a.barrier=UINT64_MAX;b=a.barrier;state(&e,XR_SESSION_STATE_FOCUSED,100);}
  a.poll=poll_injected;int rc=xtos_xr_eye_step(&e);
  if(k==7){assert(rc==0 && a.sequence==q+1 && a.barrier==b+1);assert(xtos_xr_eye_query(&e,12,&s)==0);state(&e,XR_SESSION_STATE_STOPPING,100);assert(xtos_xr_eye_step(&e)==0);}
  else {assert(rc!=0 && e.lost);assert(xtos_xr_eye_query(&e,12,&s)==XTOS_XR_UNAVAILABLE);assert(!s.sample_xr_time && !s.sample_host_ns && !strcmp(s.safe_action,"no_action") && !s.physical_acceptance);if(k<8)assert(a.sequence==q+1 && a.barrier==b+1);else assert(a.sequence==q && a.barrier==b);}
  assert(xtos_xr_eye_close(&e)==0);xtos_xr_close(&a);assert(!a.instance);printf("secure-event %d PASS\n",k);
 }
}
static void secure_cleanup(void *lib,const char *path){
 void (*reset)(int)=(void(*)(int))dlsym(lib,"eye_reset");
 for(int partial=0;partial<2;partial++)for(int k=0;k<5;k++){
  xtos_xr_adapter a={0};xtos_xr_eye_session e={0};
  if(partial){reset(0);assert(xtos_xr_open_features(&a,path,1)==0);real_gipa=a.gipa;a.gipa=dispatch_cleanup;fail_name="xrAttachSessionActionSets";fail_destroy=k;fail_once=1;eye_handle=(XrSpace)(uintptr_t)6;reference_handle=(XrSpace)(uintptr_t)5;assert(xtos_xr_eye_open(&e,&a)==XTOS_XR_RUNTIME);fail_name=0;a.gipa=real_gipa;}
  else {load(lib,path,&a,&e);ds=e.destroy_space;da=e.destroy_action;das=e.destroy_action_set;dse=e.destroy_session;eye_handle=e.eye_space;reference_handle=e.reference_space;e.destroy_space=destroy_space;e.destroy_action=destroy_action;e.destroy_action_set=destroy_set;e.destroy_session=destroy_session;fail_destroy=k;fail_once=1;assert(xtos_xr_eye_close(&e)==XTOS_XR_RUNTIME);}
  assert(a.cleanup_failed && a.active_children && a.instance && e.adapter==&a);
  assert(xtos_xr_eye_close(&e)==0);assert(!a.cleanup_failed && !a.active_children && !e.adapter);
  assert(xtos_xr_eye_open(&e,&a)==0);assert(xtos_xr_eye_close(&e)==0);xtos_xr_close(&a);assert(!a.instance);printf("secure-cleanup partial=%d child=%d PASS\n",partial,k);
 }
 /* A parent's own failure remains independent of child retry bookkeeping. */
 xtos_xr_adapter a={0};xtos_xr_eye_session e={0};load(lib,path,&a,&e);a.cleanup_failed=1;xtos_xr_close(&a);assert(xtos_xr_eye_close(&e)==0);assert(a.cleanup_failed);assert(xtos_xr_eye_open(&e,&a)==XTOS_XR_INVALID);xtos_xr_close(&a);assert(!a.instance);
}
static void secure_loss(void *lib,const char *path){
 for(int kind=0;kind<3;kind++){
  xtos_xr_adapter a={0};xtos_xr_eye_session e={0};load(lib,path,&a,&e);assert(xtos_xr_eye_step(&e)==0 && xtos_xr_eye_step(&e)==0);
  int (*begins)(void)=(int(*)(void))dlsym(lib,"eye_begin_calls");int (*ends)(void)=(int(*)(void))dlsym(lib,"eye_end_calls");
  if(kind==0){memset(&injected,0,sizeof injected);injected.type=XR_TYPE_EVENT_DATA_INSTANCE_LOSS_PENDING;}
  else state(&e,kind==1?XR_SESSION_STATE_LOSS_PENDING:XR_SESSION_STATE_EXITING,200);
  polls=0;a.poll=counted_poll;assert(xtos_xr_eye_step(&e)==XTOS_XR_UNAVAILABLE && e.lost);int n=polls;
  for(int j=0;j<3;j++){state(&e,j==0?XR_SESSION_STATE_STOPPING:XR_SESSION_STATE_READY,300+j);a.poll=counted_poll;assert(xtos_xr_eye_step(&e)==XTOS_XR_UNAVAILABLE);}
  assert(polls==n && begins()==1 && ends()==0);assert(xtos_xr_eye_close(&e)==0);
  if(kind==0){assert(xtos_xr_eye_open(&e,&a)==XTOS_XR_INVALID);xtos_xr_close(&a);assert(!a.instance);assert(xtos_xr_open_features(&a,path,1)==0);}
  assert(xtos_xr_eye_open(&e,&a)==0 && !e.lost && !e.running && e.state==XR_SESSION_STATE_UNKNOWN);
  state(&e,XR_SESSION_STATE_READY,100);assert(xtos_xr_eye_step(&e)==0);state(&e,XR_SESSION_STATE_FOCUSED,100);assert(xtos_xr_eye_step(&e)==0);xtos_xr_eye_sample s;assert(xtos_xr_eye_query(&e,12,&s)==0);
  state(&e,XR_SESSION_STATE_STOPPING,100);assert(xtos_xr_eye_step(&e)==0);assert(xtos_xr_eye_close(&e)==0);xtos_xr_close(&a);printf("secure-loss %d PASS\n",kind);
 }
}
int main(int argc,char **argv){assert(argc==3);void *lib=dlopen(argv[2],RTLD_NOW|RTLD_LOCAL);assert(lib);if(!strcmp(argv[1],"events"))secure_events(lib,argv[2]);else if(!strcmp(argv[1],"cleanup"))secure_cleanup(lib,argv[2]);else if(!strcmp(argv[1],"loss"))secure_loss(lib,argv[2]);else assert(0);dlclose(lib);return 0;}
