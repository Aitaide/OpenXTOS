#include "xtos_xr/runtime_adapter.h"
#include <dlfcn.h>
#include <string.h>
#include <stdatomic.h>
#ifdef XTOS_XR_TEST_GENERATION_START
static _Atomic uint64_t generations=XTOS_XR_TEST_GENERATION_START;
#else
static _Atomic uint64_t generations;
#endif
static int conversion_fault(xtos_xr_adapter *a,xtos_xr_conversion *c,int code) {
 if(a->barrier==UINT64_MAX) code=XTOS_XR_LIMIT;
 else a->barrier++;
 c->status=code;xtos_xr_close(a);return code;
}
int xtos_xr_time_to_host(xtos_xr_adapter *a,int64_t t,xtos_xr_conversion *c) {
 if(!c) return XTOS_XR_INVALID;
 memset(c,0,sizeof *c);c->raw_xr_time=t;c->status=XTOS_XR_INVALID;
 if(!a || !a->instance || a->cleanup_failed || t<=0) return c->status;
 if(!a->timespec_enabled || !a->to_host) return c->status=XTOS_XR_NO_CONVERSION;
 struct timespec ts={0};c->runtime_result=a->to_host(a->instance,t,&ts);a->last_result=c->runtime_result;
 if(c->runtime_result!=XR_SUCCESS) return conversion_fault(a,c,XTOS_XR_RUNTIME);
 if(ts.tv_sec<0 || ts.tv_nsec<0 || ts.tv_nsec>=1000000000 || (uint64_t)ts.tv_sec>(uint64_t)INT64_MAX/1000000000 || ((uint64_t)ts.tv_sec==(uint64_t)INT64_MAX/1000000000 && ts.tv_nsec>INT64_MAX%1000000000)) return conversion_fault(a,c,XTOS_XR_INVALID);
 c->host_ns=(int64_t)ts.tv_sec*1000000000+ts.tv_nsec;
 if(c->host_ns<=0) {c->host_ns=0;return conversion_fault(a,c,XTOS_XR_INVALID);}
 return c->status=XTOS_XR_OK;
}
int xtos_xr_host_to_time(xtos_xr_adapter *a,int64_t t,xtos_xr_conversion *c) {
 if(!c) return XTOS_XR_INVALID;
 memset(c,0,sizeof *c);c->raw_host_ns=t;c->status=XTOS_XR_INVALID;
 if(!a || !a->instance || a->cleanup_failed || t<=0) return c->status;
 if(!a->timespec_enabled || !a->to_xr) return c->status=XTOS_XR_NO_CONVERSION;
 struct timespec ts={(time_t)(t/1000000000),(long)(t%1000000000)};
 if((int64_t)ts.tv_sec!=t/1000000000) return c->status;
 XrTime output=0;c->runtime_result=a->to_xr(a->instance,&ts,&output);a->last_result=c->runtime_result;
 if(c->runtime_result!=XR_SUCCESS) return conversion_fault(a,c,XTOS_XR_RUNTIME);
 if(output<=0) return conversion_fault(a,c,XTOS_XR_INVALID);
 c->xr_time=output;return c->status=XTOS_XR_OK;
}
int xtos_xr_poll(xtos_xr_adapter *a,xtos_xr_event *event) {
 if(!a || !event || !a->instance || a->cleanup_failed) return XTOS_XR_INVALID;
 memset(event,0,sizeof *event);
 if(a->sequence==UINT64_MAX || a->barrier==UINT64_MAX) {xtos_xr_close(a);return XTOS_XR_LIMIT;}
 XrEventDataBuffer data={0};data.type=XR_TYPE_EVENT_DATA_BUFFER;
 a->last_result=a->poll(a->instance,&data);
 if(a->last_result==XR_EVENT_UNAVAILABLE) return XTOS_XR_EMPTY;
 if(a->last_result!=XR_SUCCESS) { a->barrier++;xtos_xr_close(a);return XTOS_XR_RUNTIME; }
 event->type=(int32_t)data.type;
 event->generation=a->generation;event->sequence=++a->sequence;event->barrier=++a->barrier;
 if(data.type==XR_TYPE_EVENT_DATA_EVENTS_LOST) { XrEventDataEventsLost e;memcpy(&e,&data,sizeof e);event->has_lost_count=1;event->lost_count=e.lostEventCount; }
 if(data.type==XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) { XrEventDataSessionStateChanged e;memcpy(&e,&data,sizeof e);event->has_session_state=1;event->session_state=e.state; }
 return XTOS_XR_OK;
}
void xtos_xr_close(xtos_xr_adapter *a) {
 if (!a) return;
 if (a->instance) {
  XrResult result=a->destroy?a->destroy(a->instance):XR_ERROR_FUNCTION_UNSUPPORTED;
  if(result!=XR_SUCCESS) {
   a->last_result=result;a->cleanup_failed=1;return;
  }
  if(a->cleanup_failed) a->last_result=result;
 }
 a->cleanup_failed=0;
 a->instance=XR_NULL_HANDLE;
 if (a->library) dlclose(a->library);
 a->library=0; a->gipa=0; a->destroy=0; a->poll=0;
 a->to_host=0; a->to_xr=0; a->timespec_enabled=0;
}
int xtos_xr_open(xtos_xr_adapter *a, const char *path) {
 if (!a || !path || path[0]!='/') return XTOS_XR_INVALID;
 if (a->library || a->instance) return XTOS_XR_INVALID;
 a->library=dlopen(path,RTLD_NOW|RTLD_LOCAL);
 if (!a->library) return XTOS_XR_UNAVAILABLE;
 a->gipa=(PFN_xrGetInstanceProcAddr)dlsym(a->library,"xrGetInstanceProcAddr");
 /* Core loader export is the cleanup fallback before an instance exists. */
 a->destroy=(PFN_xrDestroyInstance)dlsym(a->library,"xrDestroyInstance");
 PFN_xrEnumerateInstanceExtensionProperties enumerate=0;
 PFN_xrCreateInstance create=0;
 if (!a->gipa || !a->destroy) goto fail;
 if (a->gipa(XR_NULL_HANDLE,"xrEnumerateInstanceExtensionProperties",(PFN_xrVoidFunction*)&enumerate)!=XR_SUCCESS || !enumerate) goto fail;
 if (a->gipa(XR_NULL_HANDLE,"xrCreateInstance",(PFN_xrVoidFunction*)&create)!=XR_SUCCESS || !create) goto fail;
 uint32_t count=0;
 a->last_result=enumerate(0,0,&count,0);
 if (a->last_result!=XR_SUCCESS || count>XTOS_XR_MAX_EXTENSIONS) goto fail;
 XrExtensionProperties properties[XTOS_XR_MAX_EXTENSIONS]; memset(properties,0,sizeof properties);
 for(uint32_t i=0;i<count;i++) properties[i].type=XR_TYPE_EXTENSION_PROPERTIES;
 if(count) {
  uint32_t filled=0;
  a->last_result=enumerate(0,count,&filled,properties);
  if(a->last_result!=XR_SUCCESS || filled>count) goto fail;
  count=filled;
 }
 for(uint32_t i=0;i<count;i++) {
  if(!memchr(properties[i].extensionName,0,XR_MAX_EXTENSION_NAME_SIZE)) goto fail;
  if(!strcmp(properties[i].extensionName,XR_KHR_CONVERT_TIMESPEC_TIME_EXTENSION_NAME)) a->timespec_enabled=1;
 }
 const char *extension=XR_KHR_CONVERT_TIMESPEC_TIME_EXTENSION_NAME;
 XrInstanceCreateInfo ci={0};ci.type=XR_TYPE_INSTANCE_CREATE_INFO;
 strcpy(ci.applicationInfo.applicationName,"xtos-xr-probe");strcpy(ci.applicationInfo.engineName,"xtos-xr");
 ci.applicationInfo.apiVersion=XR_MAKE_VERSION(1,0,0);
 ci.enabledExtensionCount=a->timespec_enabled?1:0;ci.enabledExtensionNames=a->timespec_enabled?&extension:0;
 a->last_result=create(&ci,&a->instance);
 if(a->instance) {
  /* Resolve destruction before every other instance function, even if create failed with an owned handle. */
  PFN_xrDestroyInstance destroy=0;
  if(a->gipa(a->instance,"xrDestroyInstance",(PFN_xrVoidFunction*)&destroy)!=XR_SUCCESS || !destroy) goto fail;
  a->destroy=destroy;
 }
 if(a->last_result!=XR_SUCCESS || !a->instance) goto fail;
 if(a->gipa(a->instance,"xrPollEvent",(PFN_xrVoidFunction*)&a->poll)!=XR_SUCCESS || !a->poll) goto fail;
 if(a->timespec_enabled) {
  if(a->gipa(a->instance,"xrConvertTimeToTimespecTimeKHR",(PFN_xrVoidFunction*)&a->to_host)!=XR_SUCCESS || !a->to_host) goto fail;
  if(a->gipa(a->instance,"xrConvertTimespecTimeToTimeKHR",(PFN_xrVoidFunction*)&a->to_xr)!=XR_SUCCESS || !a->to_xr) goto fail;
 }
 uint64_t previous=atomic_load(&generations);
 do {
  if(previous==UINT64_MAX) {xtos_xr_close(a);return XTOS_XR_LIMIT;}
 } while(!atomic_compare_exchange_weak(&generations,&previous,previous+1));
 a->generation=previous+1;a->sequence=0;a->barrier=0;
 return XTOS_XR_OK;
fail:
 xtos_xr_close(a);return XTOS_XR_UNAVAILABLE;
}
