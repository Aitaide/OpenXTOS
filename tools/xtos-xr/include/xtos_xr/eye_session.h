#ifndef XTOS_XR_EYE_SESSION_H
#define XTOS_XR_EYE_SESSION_H
#include "xtos_xr/runtime_adapter.h"
/* Zero-initialize. All calls externally serialized; no graphics binding. */
typedef struct xtos_xr_eye_session {
 xtos_xr_adapter *adapter;
 XrSession session;
 XrActionSet action_set;
 XrAction action;
 XrSpace reference_space,eye_space;
 XrResult last_result;
 PFN_xrDestroySession destroy_session;
 PFN_xrDestroyActionSet destroy_action_set;
 PFN_xrDestroyAction destroy_action;
 PFN_xrDestroySpace destroy_space;
 PFN_xrBeginSession begin_session;
 PFN_xrEndSession end_session;
 PFN_xrRequestExitSession request_exit;
 PFN_xrSyncActions sync_actions;
 PFN_xrGetActionStatePose get_pose;
 PFN_xrLocateSpace locate_space;
 XrSessionState state;
 int running,lost;
 XrTime last_event_time;
} xtos_xr_eye_session;
/* Returns unavailable for unsupported extensions/system/functions; runtime for XR call failures. */
int xtos_xr_eye_open(xtos_xr_eye_session *eye,xtos_xr_adapter *adapter);
/* Idempotent; failed destroy retains child and parent, then can be retried. */
int xtos_xr_eye_close(xtos_xr_eye_session *eye);
/* Polls one event, changes state only for the owned session. */
int xtos_xr_eye_step(xtos_xr_eye_session *eye);
/* Request normal shutdown; consume STOPPING with eye_step before close. */
int xtos_xr_eye_request_exit(xtos_xr_eye_session *eye);
/* sampleTimeEXT reports runtime pose time: it may be predicted, interpolated or
 * clamped. It is not raw sensor capture time or proof of physical synchronization.
 * Query and converted host times remain distinct; field names retain ABI compatibility. */
typedef struct xtos_xr_eye_sample {
 int64_t query_host_ns,query_xr_time,sample_xr_time,sample_host_ns;
 XrPosef pose;
 XrResult runtime_result;
 int status;
 const char *safe_action; /* always "no_action" */
 int physical_acceptance; /* always false */
} xtos_xr_eye_sample;
int xtos_xr_eye_query(xtos_xr_eye_session *eye,int64_t query_host_ns,xtos_xr_eye_sample *sample);
#endif
