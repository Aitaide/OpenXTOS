#include "xtos_xr/eye_session.h"
#include <assert.h>
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#include <stdint.h>
static PFN_xrGetInstanceProcAddr real_gipa;
static const char *fail_name;
static int fail_destroy,fail_once;
static PFN_xrDestroySpace ds; static PFN_xrDestroyAction da;
static PFN_xrDestroyActionSet das; static PFN_xrDestroySession dse;
static XrSpace eye_handle,reference_handle;
static XrEventDataBuffer injected;
static XrResult XRAPI_CALL poll_injected(XrInstance i,XrEventDataBuffer *b){(void)i;memcpy(b,&injected,sizeof *b);return XR_SUCCESS;}
static XrResult XRAPI_CALL fset(XrInstance i,const XrActionSetCreateInfo *c,XrActionSet *o){(void)i;(void)c;(void)o;return XR_ERROR_RUNTIME_FAILURE;}
static XrResult XRAPI_CALL faction(XrActionSet i,const XrActionCreateInfo *c,XrAction *o){(void)i;(void)c;(void)o;return XR_ERROR_RUNTIME_FAILURE;}
static XrResult XRAPI_CALL fref(XrSession i,const XrReferenceSpaceCreateInfo *c,XrSpace *o){(void)i;(void)c;(void)o;return XR_ERROR_RUNTIME_FAILURE;}
static XrResult XRAPI_CALL fspace(XrSession i,const XrActionSpaceCreateInfo *c,XrSpace *o){(void)i;(void)c;(void)o;return XR_ERROR_RUNTIME_FAILURE;}
static XrResult XRAPI_CALL fattach(XrSession i,const XrSessionActionSetsAttachInfo *c){(void)i;(void)c;return XR_ERROR_RUNTIME_FAILURE;}
static XrResult XRAPI_CALL fsuggest(XrInstance i,const XrInteractionProfileSuggestedBinding *c){(void)i;(void)c;return XR_ERROR_RUNTIME_FAILURE;}
static XrResult XRAPI_CALL fpath(XrInstance i,const char *c,XrPath *o){(void)i;(void)c;(void)o;return XR_ERROR_RUNTIME_FAILURE;}
static XrResult XRAPI_CALL gipa(XrInstance i,const char *n,PFN_xrVoidFunction *o){
 XrResult r=real_gipa(i,n,o);if(r!=XR_SUCCESS || !fail_name || strcmp(n,fail_name))return r;
 #define F(name,fn) if(!strcmp(n,name))*o=(PFN_xrVoidFunction)fn
 F("xrCreateActionSet",fset);F("xrCreateAction",faction);F("xrCreateReferenceSpace",fref);F("xrCreateActionSpace",fspace);F("xrAttachSessionActionSets",fattach);F("xrSuggestInteractionProfileBindings",fsuggest);F("xrStringToPath",fpath);
 #undef F
 return r;
}
static XrResult XRAPI_CALL destroy_space(XrSpace s){if(fail_once && ((fail_destroy==0&&s==eye_handle)||(fail_destroy==1&&s==reference_handle))){fail_once=0;return XR_ERROR_RUNTIME_FAILURE;}return ds(s);}
static XrResult XRAPI_CALL destroy_action(XrAction s){if(fail_once&&fail_destroy==2){fail_once=0;return XR_ERROR_RUNTIME_FAILURE;}return da(s);}
static XrResult XRAPI_CALL destroy_set(XrActionSet s){if(fail_once&&fail_destroy==3){fail_once=0;return XR_ERROR_RUNTIME_FAILURE;}return das(s);}
static XrResult XRAPI_CALL destroy_session(XrSession s){if(fail_once&&fail_destroy==4){fail_once=0;return XR_ERROR_RUNTIME_FAILURE;}return dse(s);}
static void load(void *lib,const char *path,xtos_xr_adapter *a,xtos_xr_eye_session *e){
 void (*reset)(int)=(void(*)(int))dlsym(lib,"eye_reset");assert(reset);reset(0);
 assert(xtos_xr_open_features(a,path,XTOS_XR_FEATURE_HEADLESS_EYE)==0);assert(xtos_xr_eye_open(e,a)==0);
}
static void state(xtos_xr_eye_session *e,XrSessionState s,XrTime t){memset(&injected,0,sizeof injected);XrEventDataSessionStateChanged v={XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED,0,e->session,s,t};memcpy(&injected,&v,sizeof v);e->adapter->poll=poll_injected;}
static void cleanup_probe(void *lib,const char *path){
 const char *names[]={"xrCreateActionSet","xrCreateAction","xrStringToPath","xrSuggestInteractionProfileBindings","xrCreateReferenceSpace","xrCreateActionSpace","xrAttachSessionActionSets"};
 void (*reset)(int)=(void(*)(int))dlsym(lib,"eye_reset");
 for(unsigned i=0;i<sizeof names/sizeof names[0];i++){
  xtos_xr_adapter a={0};xtos_xr_eye_session e={0};reset(0);assert(xtos_xr_open_features(&a,path,1)==0);
  real_gipa=a.gipa;a.gipa=gipa;fail_name=names[i];int rc=xtos_xr_eye_open(&e,&a);
  printf("allocation stage=%s rc=%d eye_last=%d adapter_last=%d owned=%u handles=%d\n",names[i],rc,e.last_result,a.last_result,a.active_children,!!(e.session||e.action||e.action_set||e.eye_space||e.reference_space));
  assert(rc==XTOS_XR_RUNTIME && !a.active_children && !e.adapter);fail_name=0;a.gipa=real_gipa;
  assert(xtos_xr_eye_open(&e,&a)==0);assert(xtos_xr_eye_close(&e)==0);xtos_xr_close(&a);assert(!a.instance);
 }
 for(int i=0;i<5;i++){
  xtos_xr_adapter a={0};xtos_xr_eye_session e={0};load(lib,path,&a,&e);
  ds=e.destroy_space;da=e.destroy_action;das=e.destroy_action_set;dse=e.destroy_session;eye_handle=e.eye_space;reference_handle=e.reference_space;
  e.destroy_space=destroy_space;e.destroy_action=destroy_action;e.destroy_action_set=destroy_set;e.destroy_session=destroy_session;fail_destroy=i;fail_once=1;
  assert(xtos_xr_eye_close(&e)==XTOS_XR_RUNTIME);assert(a.active_children&&a.cleanup_failed&&a.instance);
  int retry=xtos_xr_eye_close(&e);int stuck=a.cleanup_failed;int reopen=xtos_xr_eye_open(&e,&a);
  printf("destroy index=%d retry=%d children=%u cleanup_failed=%d blocked=%d reopen=%d\n",i,retry,a.active_children,stuck,a.blocked_by_child,reopen);
  assert(retry==0&&!a.active_children&&stuck==1&&reopen==XTOS_XR_INVALID);xtos_xr_close(&a);assert(!a.instance);
 }
}
static void event_probe(void *lib,const char *path){
 const char *names[]={"EventsLost","ReferenceChange","ProfileChange","Unknown","WrongReferenceSession","WrongProfileSession","StaleFocused"};
 for(int i=0;i<7;i++){
  xtos_xr_adapter a={0};xtos_xr_eye_session e={0};load(lib,path,&a,&e);assert(xtos_xr_eye_step(&e)==0&&xtos_xr_eye_step(&e)==0);memset(&injected,0,sizeof injected);
  if(i==0){XrEventDataEventsLost v={XR_TYPE_EVENT_DATA_EVENTS_LOST,0,3};memcpy(&injected,&v,sizeof v);}
  if(i==1||i==4){XrEventDataReferenceSpaceChangePending v={0};v.type=XR_TYPE_EVENT_DATA_REFERENCE_SPACE_CHANGE_PENDING;v.session=i==4?(XrSession)(uintptr_t)99:e.session;v.referenceSpaceType=XR_REFERENCE_SPACE_TYPE_LOCAL;v.changeTime=1;memcpy(&injected,&v,sizeof v);}
  if(i==2||i==5){XrEventDataInteractionProfileChanged v={XR_TYPE_EVENT_DATA_INTERACTION_PROFILE_CHANGED,0,i==5?(XrSession)(uintptr_t)99:e.session};memcpy(&injected,&v,sizeof v);}
  if(i==3)injected.type=(XrStructureType)2147480000;
  if(i==6){state(&e,XR_SESSION_STATE_VISIBLE,200);assert(xtos_xr_eye_step(&e)==0);state(&e,XR_SESSION_STATE_FOCUSED,99);}
  a.poll=poll_injected;uint64_t barrier=a.barrier,sequence=a.sequence;int step=xtos_xr_eye_step(&e);xtos_xr_eye_sample s;int query=xtos_xr_eye_query(&e,12,&s);
  printf("event=%s step=%d query=%d lost=%d sample=%lld barrier=%llu->%llu sequence=%llu->%llu safe=%s physical=%d\n",names[i],step,query,e.lost,(long long)s.sample_xr_time,(unsigned long long)barrier,(unsigned long long)a.barrier,(unsigned long long)sequence,(unsigned long long)a.sequence,s.safe_action,s.physical_acceptance);
  assert(query==0&&!e.lost&&s.sample_xr_time>0&&!strcmp(s.safe_action,"no_action")&&!s.physical_acceptance);
  state(&e,XR_SESSION_STATE_STOPPING,300);assert(xtos_xr_eye_step(&e)==0);assert(xtos_xr_eye_close(&e)==0);xtos_xr_close(&a);
 }
}
static void loss_probe(void *lib,const char *path){
 xtos_xr_adapter a={0};xtos_xr_eye_session e={0};load(lib,path,&a,&e);assert(xtos_xr_eye_step(&e)==0&&xtos_xr_eye_step(&e)==0);
 int (*begins)(void)=(int(*)(void))dlsym(lib,"eye_begin_calls");int (*ends)(void)=(int(*)(void))dlsym(lib,"eye_end_calls");
 memset(&injected,0,sizeof injected);injected.type=XR_TYPE_EVENT_DATA_INSTANCE_LOSS_PENDING;a.poll=poll_injected;int loss=xtos_xr_eye_step(&e);assert(loss==XTOS_XR_UNAVAILABLE&&e.lost);
 state(&e,XR_SESSION_STATE_STOPPING,300);int stopping=xtos_xr_eye_step(&e);state(&e,XR_SESSION_STATE_READY,400);int ready=xtos_xr_eye_step(&e);xtos_xr_eye_sample s;int query=xtos_xr_eye_query(&e,12,&s);
 printf("loss=%d stopping=%d ready=%d lost=%d running=%d begin_calls=%d end_calls=%d query=%d\n",loss,stopping,ready,e.lost,e.running,begins(),ends(),query);
 assert(begins()==2&&ends()==1&&e.lost&&query==XTOS_XR_UNAVAILABLE);assert(xtos_xr_eye_close(&e)==0);
 assert(xtos_xr_eye_open(&e,&a)==0);state(&e,XR_SESSION_STATE_READY,500);assert(xtos_xr_eye_step(&e)==0);state(&e,XR_SESSION_STATE_FOCUSED,600);assert(xtos_xr_eye_step(&e)==0);query=xtos_xr_eye_query(&e,12,&s);
 printf("reopened lost=%d running=%d state=%d query=%d\n",e.lost,e.running,e.state,query);assert(e.lost&&query==XTOS_XR_UNAVAILABLE);assert(xtos_xr_eye_close(&e)==0);xtos_xr_close(&a);assert(!a.instance);
}
int main(int argc,char **argv){assert(argc==3);void *lib=dlopen(argv[2],RTLD_NOW|RTLD_LOCAL);assert(lib);if(!strcmp(argv[1],"cleanup"))cleanup_probe(lib,argv[2]);else if(!strcmp(argv[1],"events"))event_probe(lib,argv[2]);else if(!strcmp(argv[1],"loss"))loss_probe(lib,argv[2]);else assert(0);dlclose(lib);return 0;}
