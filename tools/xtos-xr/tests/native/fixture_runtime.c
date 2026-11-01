#include <time.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>
/* This shared object is an injected simulated runtime ABI, not a headset. */
static int mode, created, destroyed, ext_queries;
static int event_index,convert_calls,destroy_attempts;
void fixture_reset(int m) { mode=m; created=destroyed=ext_queries=0; event_index=convert_calls=destroy_attempts=0; }
void fixture_allow_destroy(void) { mode=0; }
int fixture_created(void) { return created; }
int fixture_destroyed(void) { return destroyed; }
int fixture_ext_queries(void) { return ext_queries; }
static XrResult XRAPI_CALL sim_enum(const char *layer,uint32_t cap,uint32_t *count,XrExtensionProperties *p) {
 const char *setting=getenv("XTOS_XR_FIXTURE_MODE");if(setting) mode=atoi(setting);
 if(layer) return XR_ERROR_VALIDATION_FAILURE;
 *count=mode==3?129:(mode==8?128:1);
 if(!cap) return XR_SUCCESS;
 if(mode==9) {*count=cap+1;return XR_SUCCESS;}
 if(cap<*count) return XR_ERROR_SIZE_INSUFFICIENT;
 for(uint32_t n=0;n<*count;n++) {
  if(p[n].type!=XR_TYPE_EXTENSION_PROPERTIES || p[n].next) return XR_ERROR_VALIDATION_FAILURE;
  strcpy(p[n].extensionName,n || mode==2?"XR_fake_unused":XR_KHR_CONVERT_TIMESPEC_TIME_EXTENSION_NAME);
  p[n].extensionVersion=1;
 }
 if(mode==10) memset(p[0].extensionName,'x',sizeof p[0].extensionName);
 return XR_SUCCESS;
}
static XrResult XRAPI_CALL sim_create(const XrInstanceCreateInfo *p,XrInstance *out) {
 if(p->type!=XR_TYPE_INSTANCE_CREATE_INFO || p->next || p->createFlags || p->enabledApiLayerCount || p->applicationInfo.apiVersion!=XR_MAKE_VERSION(1,0,0) || !p->applicationInfo.applicationName[0] || !p->applicationInfo.engineName[0]) return XR_ERROR_VALIDATION_FAILURE;
 if(p->enabledExtensionCount!=(mode==2?0u:1u)) return XR_ERROR_VALIDATION_FAILURE;
 if(p->enabledExtensionCount && strcmp(p->enabledExtensionNames[0],XR_KHR_CONVERT_TIMESPEC_TIME_EXTENSION_NAME)) return XR_ERROR_VALIDATION_FAILURE;
 if(mode==4) return XR_ERROR_RUNTIME_FAILURE;
 *out=(XrInstance)(uintptr_t)1; created++;
 return XR_SUCCESS;
}
static XrResult XRAPI_CALL sim_destroy(XrInstance i) {
 if(!i) abort();
 destroy_attempts++;
 if(mode==18 || (mode==17 && destroy_attempts==1)) return XR_ERROR_RUNTIME_FAILURE;
 destroyed++; return XR_SUCCESS;
}
#ifndef FIXTURE_NO_DESTROY_EXPORT
XRAPI_ATTR XrResult XRAPI_CALL xrDestroyInstance(XrInstance i) { return sim_destroy(i); }
#endif
static XrResult XRAPI_CALL sim_poll(XrInstance i,XrEventDataBuffer *p) {
 (void)i;
 if(mode==11) {
  static const XrStructureType types[]={XR_TYPE_EVENT_DATA_EVENTS_LOST,XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED,XR_TYPE_EVENT_DATA_INSTANCE_LOSS_PENDING,XR_TYPE_EVENT_DATA_REFERENCE_SPACE_CHANGE_PENDING,XR_TYPE_EVENT_DATA_INTERACTION_PROFILE_CHANGED};
  if(p->type!=XR_TYPE_EVENT_DATA_BUFFER || p->next) return XR_ERROR_VALIDATION_FAILURE;
  p->type=types[event_index++%5];p->next=(void*)(uintptr_t)1;return XR_SUCCESS;
 }
 if(mode!=6) return XR_EVENT_UNAVAILABLE;
 if(p->type!=XR_TYPE_EVENT_DATA_BUFFER || p->next) return XR_ERROR_VALIDATION_FAILURE;
 switch(event_index++) {
 case 0: { XrEventDataEventsLost e={XR_TYPE_EVENT_DATA_EVENTS_LOST,(void*)(uintptr_t)1,7};memcpy(p,&e,sizeof e);return XR_SUCCESS; }
 case 1: { XrEventDataSessionStateChanged e={XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED,0,XR_NULL_HANDLE,XR_SESSION_STATE_STOPPING,12};memcpy(p,&e,sizeof e);return XR_SUCCESS; }
 case 2: p->type=(XrStructureType)1234567; p->next=(void*)(uintptr_t)1;return XR_SUCCESS;
 default:return XR_ERROR_RUNTIME_FAILURE;
 }
}
static XrResult XRAPI_CALL sim_to_host(XrInstance i,XrTime t,struct timespec *p) { (void)i;convert_calls++;
 if(mode==12 || mode==13) {p->tv_sec=t/1000000000;p->tv_nsec=t%1000000000+(mode==13);return XR_SUCCESS;}
 p->tv_sec=0;p->tv_nsec=t>INT64_MAX-convert_calls*1000?1000000000:t+convert_calls*1000;
 if(t==20) p->tv_nsec=1000000000;
 if(t==21) {p->tv_sec=9223372037LL;p->tv_nsec=0;}
 if(t==22) return XR_ERROR_RUNTIME_FAILURE;
 if(t==23) p->tv_sec=-1;
 return XR_SUCCESS; }
static XrResult XRAPI_CALL sim_to_xr(XrInstance i,const struct timespec *p,XrTime *t) { (void)i;convert_calls++;
 if(mode==12) {*t=(int64_t)p->tv_sec*1000000000+p->tv_nsec;return XR_SUCCESS;}
 if(mode==14 || mode==15) {*t=mode==14?0:-1;return XR_SUCCESS;}
 if(mode==16) return XR_ERROR_RUNTIME_FAILURE;
 *t=p->tv_nsec+convert_calls*1000;return XR_SUCCESS; }
XRAPI_ATTR XrResult XRAPI_CALL xrGetInstanceProcAddr(XrInstance i,const char *name,PFN_xrVoidFunction *out) {
 (void)i; *out=0;
 if(!strcmp(name,"xrEnumerateInstanceExtensionProperties")) *out=(PFN_xrVoidFunction)sim_enum;
 if(!strcmp(name,"xrCreateInstance")) *out=(PFN_xrVoidFunction)sim_create;
 if(!strcmp(name,"xrDestroyInstance") && i && mode!=7) *out=(PFN_xrVoidFunction)sim_destroy;
 if(!strcmp(name,"xrPollEvent") && mode!=5) *out=(PFN_xrVoidFunction)sim_poll;
 if(!strcmp(name,"xrConvertTimeToTimespecTimeKHR")) {ext_queries++;*out=(PFN_xrVoidFunction)sim_to_host;}
 if(!strcmp(name,"xrConvertTimespecTimeToTimeKHR")) {ext_queries++;*out=(PFN_xrVoidFunction)sim_to_xr;}
 return *out?XR_SUCCESS:XR_ERROR_FUNCTION_UNSUPPORTED;
}
