#define main shared_main
#include "probe.c"
#undef main
static XrResult XRAPI_CALL dispatch(XrInstance i,const char *n,PFN_xrVoidFunction *o){
 XrResult r=gipa(i,n,o);if(r!=XR_SUCCESS)return r;
 if(!strcmp(n,"xrDestroySpace")){ds=(PFN_xrDestroySpace)*o;*o=(PFN_xrVoidFunction)destroy_space;}
 if(!strcmp(n,"xrDestroyAction")){da=(PFN_xrDestroyAction)*o;*o=(PFN_xrVoidFunction)destroy_action;}
 if(!strcmp(n,"xrDestroyActionSet")){das=(PFN_xrDestroyActionSet)*o;*o=(PFN_xrVoidFunction)destroy_set;}
 if(!strcmp(n,"xrDestroySession")){dse=(PFN_xrDestroySession)*o;*o=(PFN_xrVoidFunction)destroy_session;}
 return r;
}
int main(int argc,char **argv){assert(argc==2);void *lib=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL);assert(lib);void (*reset)(int)=(void(*)(int))dlsym(lib,"eye_reset");
 for(int i=0;i<5;i++){xtos_xr_adapter a={0};xtos_xr_eye_session e={0};reset(0);assert(xtos_xr_open_features(&a,argv[1],1)==0);real_gipa=a.gipa;a.gipa=dispatch;fail_name="xrAttachSessionActionSets";fail_destroy=i;fail_once=1;eye_handle=(XrSpace)(uintptr_t)6;reference_handle=(XrSpace)(uintptr_t)5;
 int rc=xtos_xr_eye_open(&e,&a);assert(rc==XTOS_XR_RUNTIME&&a.active_children&&a.cleanup_failed&&e.adapter==&a);
 printf("partial index=%d rc=%d owned_session=%d action=%d set=%d ref=%d eye=%d cleanup=%d\n",i,rc,!!e.session,!!e.action,!!e.action_set,!!e.reference_space,!!e.eye_space,a.cleanup_failed);
 fail_name=0;a.gipa=real_gipa;assert(xtos_xr_eye_close(&e)==0);printf("partial_retry index=%d children=%u cleanup=%d reopen=%d\n",i,a.active_children,a.cleanup_failed,xtos_xr_eye_open(&e,&a));assert(!a.active_children&&a.cleanup_failed);xtos_xr_close(&a);assert(!a.instance);}
 dlclose(lib);return 0;}
