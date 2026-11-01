#ifndef XTOS_XR_RUNTIME_ADAPTER_H
#define XTOS_XR_RUNTIME_ADAPTER_H
#ifndef XR_NO_PROTOTYPES
#define XR_NO_PROTOTYPES
#endif
#ifndef XR_USE_TIMESPEC
#define XR_USE_TIMESPEC
#endif
#include <stdint.h>
#include <time.h>
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>
#define XTOS_XR_MAX_EXTENSIONS 128u
#define XTOS_XR_MAX_EVENTS 256u
enum { XTOS_XR_OK=0, XTOS_XR_EMPTY=1, XTOS_XR_UNAVAILABLE=-1, XTOS_XR_INVALID=-2, XTOS_XR_RUNTIME=-3, XTOS_XR_LIMIT=-4, XTOS_XR_NO_CONVERSION=-5 };
/* Zero-initialize before first open. Single owner; externally serialize calls. */
typedef struct xtos_xr_adapter {
 void *library;
 XrInstance instance;
 PFN_xrGetInstanceProcAddr gipa;
 PFN_xrDestroyInstance destroy;
 PFN_xrPollEvent poll;
 PFN_xrConvertTimeToTimespecTimeKHR to_host;
 PFN_xrConvertTimespecTimeToTimeKHR to_xr;
 uint64_t generation, sequence, barrier;
 XrResult last_result;
 int timespec_enabled;
 /* Failed destruction retains instance/library; only close retry is allowed. */
 int cleanup_failed;
} xtos_xr_adapter;
int xtos_xr_open(xtos_xr_adapter *a, const char *absolute_loader_path);
void xtos_xr_close(xtos_xr_adapter *a);
typedef struct xtos_xr_event {
 uint64_t generation,sequence,barrier;
 int32_t type,session_state;
 uint32_t lost_count;
 int has_session_state,has_lost_count;
} xtos_xr_event;
int xtos_xr_poll(xtos_xr_adapter *a,xtos_xr_event *event);
typedef struct xtos_xr_conversion {
 int64_t raw_xr_time,raw_host_ns,host_ns,xr_time;
 XrResult runtime_result;
 int status;
} xtos_xr_conversion;
int xtos_xr_time_to_host(xtos_xr_adapter *a,int64_t time,xtos_xr_conversion *out);
int xtos_xr_host_to_time(xtos_xr_adapter *a,int64_t host_ns,xtos_xr_conversion *out);
#endif
