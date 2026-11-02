#include "xtos_xr/eye_session.h"
#include <string.h>
#include <math.h>
#include <limits.h>

int xtos_xr_eye_request_exit(xtos_xr_eye_session *e) {
 if(!e || !e->adapter || !e->session || !e->running || e->lost || !e->request_exit)return XTOS_XR_INVALID;
 e->last_result=e->request_exit(e->session);e->adapter->last_result=e->last_result;
 if(e->last_result!=XR_SUCCESS){e->lost=1;return XTOS_XR_RUNTIME;}
 return XTOS_XR_OK;
}
int xtos_xr_eye_step(xtos_xr_eye_session *e) {
 if(!e || !e->adapter || !e->session || !e->adapter->instance || !e->adapter->poll) return XTOS_XR_INVALID;
 xtos_xr_adapter *a=e->adapter;
 if(e->lost || a->terminal_lost)return XTOS_XR_UNAVAILABLE;
 if(a->cleanup_failed)return XTOS_XR_INVALID;
 if(a->sequence==UINT64_MAX || a->barrier==UINT64_MAX){e->lost=1;return XTOS_XR_LIMIT;}
 XrEventDataBuffer event={0};event.type=XR_TYPE_EVENT_DATA_BUFFER;
 e->last_result=a->poll(a->instance,&event);a->last_result=e->last_result;
 if(e->last_result==XR_EVENT_UNAVAILABLE)return XTOS_XR_EMPTY;
 if(e->last_result!=XR_SUCCESS){a->barrier++;e->lost=1;return XTOS_XR_RUNTIME;}
 a->sequence++;a->barrier++;
 if(event.type==XR_TYPE_EVENT_DATA_INSTANCE_LOSS_PENDING)a->terminal_lost=1;
 /* Unreconciled loss, reference/profile changes and unknown events fence gaze. */
 if(event.type!=XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED){e->lost=1;return XTOS_XR_UNAVAILABLE;}
 XrEventDataSessionStateChanged change;memcpy(&change,&event,sizeof change);
 if(change.session!=e->session){e->lost=1;return XTOS_XR_INVALID;}
 if(change.time<e->last_event_time){e->lost=1;return XTOS_XR_UNAVAILABLE;}
 e->last_event_time=change.time;
 e->state=change.state;
 if(change.state==XR_SESSION_STATE_LOSS_PENDING || change.state==XR_SESSION_STATE_EXITING){e->lost=1;return XTOS_XR_UNAVAILABLE;}
 if(change.state==XR_SESSION_STATE_READY && !e->running) {
  XrSessionBeginInfo begin={0};begin.type=XR_TYPE_SESSION_BEGIN_INFO;begin.primaryViewConfigurationType=XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO;
  e->last_result=e->begin_session(e->session,&begin);a->last_result=e->last_result;
  if(e->last_result!=XR_SUCCESS){e->lost=1;return XTOS_XR_RUNTIME;}
  e->running=1;
 } else if(change.state==XR_SESSION_STATE_STOPPING && e->running) {
  e->last_result=e->end_session(e->session);a->last_result=e->last_result;
  if(e->last_result!=XR_SUCCESS){e->lost=1;return XTOS_XR_RUNTIME;}
  e->running=0;
 }
 return XTOS_XR_OK;
}

int xtos_xr_eye_query(xtos_xr_eye_session *e,int64_t query_ns,xtos_xr_eye_sample *s) {
 if(!s)return XTOS_XR_INVALID;
 memset(s,0,sizeof *s);s->status=XTOS_XR_INVALID;s->safe_action="no_action";
 if(!e || !e->adapter || !e->session || !e->adapter->instance || e->adapter->cleanup_failed || e->lost || !e->running || e->state!=XR_SESSION_STATE_FOCUSED || !e->sync_actions || !e->get_pose || !e->locate_space || !e->adapter->to_xr || !e->adapter->to_host || query_ns<=0) return s->status=XTOS_XR_UNAVAILABLE;
 xtos_xr_adapter *a=e->adapter;
 s->query_host_ns=query_ns;
 struct timespec query={(time_t)(query_ns/1000000000),(long)(query_ns%1000000000)};
 if((int64_t)query.tv_sec!=query_ns/1000000000)return s->status;
 XrTime time=0;
 s->runtime_result=a->to_xr(a->instance,&query,&time);a->last_result=s->runtime_result;
 if(s->runtime_result!=XR_SUCCESS){e->lost=1;return s->status=XTOS_XR_RUNTIME;}
 if(time<=0)return s->status;
 s->query_xr_time=time;
 XrActiveActionSet active={e->action_set,XR_NULL_PATH};
 XrActionsSyncInfo sync={0};sync.type=XR_TYPE_ACTIONS_SYNC_INFO;sync.countActiveActionSets=1;sync.activeActionSets=&active;
 s->runtime_result=e->sync_actions(e->session,&sync);a->last_result=s->runtime_result;
 if(s->runtime_result!=XR_SUCCESS){e->lost=1;return s->status=XTOS_XR_RUNTIME;}
 XrActionStateGetInfo get={0};get.type=XR_TYPE_ACTION_STATE_GET_INFO;get.action=e->action;
 XrActionStatePose pose={0};pose.type=XR_TYPE_ACTION_STATE_POSE;
 s->runtime_result=e->get_pose(e->session,&get,&pose);a->last_result=s->runtime_result;
 if(s->runtime_result!=XR_SUCCESS){e->lost=1;return s->status=XTOS_XR_RUNTIME;}
 if(pose.isActive!=XR_TRUE)return s->status=XTOS_XR_UNAVAILABLE;
 XrEyeGazeSampleTimeEXT sample={0};sample.type=XR_TYPE_EYE_GAZE_SAMPLE_TIME_EXT;
 XrSpaceLocation location={0};location.type=XR_TYPE_SPACE_LOCATION;location.next=&sample;
 s->runtime_result=e->locate_space(e->eye_space,e->reference_space,time,&location);a->last_result=s->runtime_result;
 if(s->runtime_result!=XR_SUCCESS){e->lost=1;return s->status=XTOS_XR_RUNTIME;}
 const XrSpaceLocationFlags tracked=XR_SPACE_LOCATION_ORIENTATION_VALID_BIT|XR_SPACE_LOCATION_POSITION_VALID_BIT|XR_SPACE_LOCATION_ORIENTATION_TRACKED_BIT|XR_SPACE_LOCATION_POSITION_TRACKED_BIT;
 if((location.locationFlags&tracked)!=tracked)return s->status=XTOS_XR_UNAVAILABLE;
 XrPosef p=location.pose;float norm=p.orientation.x*p.orientation.x+p.orientation.y*p.orientation.y+p.orientation.z*p.orientation.z+p.orientation.w*p.orientation.w;
 if(!isfinite(p.position.x)||!isfinite(p.position.y)||!isfinite(p.position.z)||!isfinite(p.orientation.x)||!isfinite(p.orientation.y)||!isfinite(p.orientation.z)||!isfinite(p.orientation.w)||!isfinite(norm)||fabsf(norm-1.f)>0.01f||sample.time<=0)return s->status;
 struct timespec host={0};
 s->runtime_result=a->to_host(a->instance,sample.time,&host);a->last_result=s->runtime_result;
 if(s->runtime_result!=XR_SUCCESS){e->lost=1;return s->status=XTOS_XR_RUNTIME;}
 if(host.tv_sec<0 || host.tv_nsec<0 || host.tv_nsec>=1000000000 || (uint64_t)host.tv_sec>(uint64_t)INT64_MAX/1000000000 || ((uint64_t)host.tv_sec==(uint64_t)INT64_MAX/1000000000 && host.tv_nsec>INT64_MAX%1000000000))return s->status;
 int64_t host_ns=(int64_t)host.tv_sec*1000000000+host.tv_nsec;
 if(host_ns<=0)return s->status;
 s->sample_xr_time=sample.time;s->sample_host_ns=host_ns;s->pose=p;
 return s->status=XTOS_XR_OK;
}
/* No global XR dispatch: every function is resolved against this instance. */
#define RESOLVE(field,name) do {PFN_xrVoidFunction fn=0; XrResult r=a->gipa(a->instance,"xr" #name,&fn); if(r!=XR_SUCCESS || !fn) goto unavailable; e->field=(PFN_xr##name)fn;}while(0)
#define LOCAL(name) do {PFN_xrVoidFunction fn=0; XrResult r=a->gipa(a->instance,"xr" #name,&fn); if(r!=XR_SUCCESS || !fn) goto unavailable; name=(PFN_xr##name)fn;}while(0)
#define CALL(expr) do {e->last_result=(expr);a->last_result=e->last_result;if(e->last_result!=XR_SUCCESS) goto runtime_fail;}while(0)
static int owned(const xtos_xr_eye_session *e) {return e->session || e->action_set || e->action || e->reference_space || e->eye_space;}
static void update_owner(xtos_xr_eye_session *e) {
 if(e->adapter) e->adapter->active_children=owned(e)?1u:0u;
}
int xtos_xr_eye_close(xtos_xr_eye_session *e) {
 if(!e)return XTOS_XR_INVALID;
 if(!e->adapter)return owned(e)?XTOS_XR_INVALID:XTOS_XR_OK;
 xtos_xr_adapter *a=e->adapter;
 /* Do not destroy a live action while its session is running; caller must stop first. */
 if(e->running && e->state!=XR_SESSION_STATE_STOPPING && !e->lost) return XTOS_XR_INVALID;
 if(e->running && !e->lost) {
  if(!e->end_session)return XTOS_XR_UNAVAILABLE;
  e->last_result=e->end_session(e->session);a->last_result=e->last_result;
  if(e->last_result!=XR_SUCCESS)return XTOS_XR_RUNTIME;
  e->running=0;
 }
 if(e->lost)e->running=0;
 #define DESTROY(member,fn) do {if(e->member) {e->last_result=e->fn?e->fn(e->member):XR_ERROR_FUNCTION_UNSUPPORTED;a->last_result=e->last_result;if(e->last_result!=XR_SUCCESS) {if(a->cleanup_failed && !a->child_cleanup_failed && !a->blocked_by_child)a->parent_cleanup_failed=1;a->child_cleanup_failed=1;a->cleanup_failed=1;return XTOS_XR_RUNTIME;}e->member=XR_NULL_HANDLE;}}while(0)
 DESTROY(eye_space,destroy_space);DESTROY(reference_space,destroy_space);
 DESTROY(action,destroy_action);DESTROY(action_set,destroy_action_set);
 DESTROY(session,destroy_session);
 #undef DESTROY
 update_owner(e);
 if(a->cleanup_failed && !a->child_cleanup_failed && !a->blocked_by_child)a->parent_cleanup_failed=1;
 a->child_cleanup_failed=0;a->blocked_by_child=0;a->cleanup_failed=a->parent_cleanup_failed;
 e->adapter=0;return XTOS_XR_OK;
}
int xtos_xr_eye_open(xtos_xr_eye_session *e,xtos_xr_adapter *a) {
 if(!e || !a || e->adapter || owned(e) || !a->instance || a->terminal_lost || a->cleanup_failed || a->active_children || a->features!=XTOS_XR_FEATURE_HEADLESS_EYE || !a->timespec_enabled || !a->to_host || a->sequence==UINT64_MAX || a->barrier==UINT64_MAX) return XTOS_XR_INVALID;
 e->lost=0;e->running=0;e->state=XR_SESSION_STATE_UNKNOWN;e->last_event_time=0;
 PFN_xrGetSystem GetSystem=0;PFN_xrGetSystemProperties GetSystemProperties=0;
 PFN_xrCreateSession CreateSession=0;PFN_xrStringToPath StringToPath=0;
 PFN_xrCreateActionSet CreateActionSet=0;PFN_xrCreateAction CreateAction=0;
 PFN_xrSuggestInteractionProfileBindings SuggestInteractionProfileBindings=0;
 PFN_xrCreateReferenceSpace CreateReferenceSpace=0;PFN_xrCreateActionSpace CreateActionSpace=0;
 PFN_xrAttachSessionActionSets AttachSessionActionSets=0;
 /* Resolve every required function before allocating a child. */
 LOCAL(GetSystem);LOCAL(GetSystemProperties);LOCAL(CreateSession);LOCAL(StringToPath);
 LOCAL(CreateActionSet);LOCAL(CreateAction);LOCAL(SuggestInteractionProfileBindings);
 LOCAL(CreateReferenceSpace);LOCAL(CreateActionSpace);LOCAL(AttachSessionActionSets);
 RESOLVE(destroy_session,DestroySession);RESOLVE(destroy_action_set,DestroyActionSet);
 RESOLVE(destroy_action,DestroyAction);RESOLVE(destroy_space,DestroySpace);
 RESOLVE(begin_session,BeginSession);RESOLVE(end_session,EndSession);RESOLVE(request_exit,RequestExitSession);
 RESOLVE(sync_actions,SyncActions);RESOLVE(get_pose,GetActionStatePose);RESOLVE(locate_space,LocateSpace);
 XrSystemGetInfo get={0};get.type=XR_TYPE_SYSTEM_GET_INFO;get.formFactor=XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY;
 XrSystemId system=XR_NULL_SYSTEM_ID;
 e->last_result=GetSystem(a->instance,&get,&system);a->last_result=e->last_result;
 if(e->last_result==XR_ERROR_FORM_FACTOR_UNAVAILABLE || e->last_result==XR_ERROR_FORM_FACTOR_UNSUPPORTED) goto unavailable;
 if(e->last_result!=XR_SUCCESS) goto runtime_fail;
 if(system==XR_NULL_SYSTEM_ID) goto unavailable;
 XrSystemEyeGazeInteractionPropertiesEXT eyeprops={0};eyeprops.type=XR_TYPE_SYSTEM_EYE_GAZE_INTERACTION_PROPERTIES_EXT;
 XrSystemProperties props={0};props.type=XR_TYPE_SYSTEM_PROPERTIES;props.next=&eyeprops;
 CALL(GetSystemProperties(a->instance,system,&props));
 if(props.systemId!=system || eyeprops.supportsEyeGazeInteraction!=XR_TRUE) goto unavailable;
 e->adapter=a;
 XrSessionCreateInfo sci={0};sci.type=XR_TYPE_SESSION_CREATE_INFO;sci.systemId=system;
 e->last_result=CreateSession(a->instance,&sci,&e->session);a->last_result=e->last_result;update_owner(e);
 if(e->last_result!=XR_SUCCESS)goto runtime_fail;
 if(!e->session)goto unavailable;
 XrActionSetCreateInfo set={0};set.type=XR_TYPE_ACTION_SET_CREATE_INFO;
 strcpy(set.actionSetName,"xtos_eyes");strcpy(set.localizedActionSetName,"XTOS Eyes");
 e->last_result=CreateActionSet(a->instance,&set,&e->action_set);a->last_result=e->last_result;update_owner(e);
 if(e->last_result!=XR_SUCCESS)goto runtime_fail;
 if(!e->action_set)goto unavailable;
 XrActionCreateInfo action={0};action.type=XR_TYPE_ACTION_CREATE_INFO;action.actionType=XR_ACTION_TYPE_POSE_INPUT;
 strcpy(action.actionName,"gaze_pose");strcpy(action.localizedActionName,"Gaze Pose");
 e->last_result=CreateAction(e->action_set,&action,&e->action);a->last_result=e->last_result;update_owner(e);
 if(e->last_result!=XR_SUCCESS)goto runtime_fail;
 if(!e->action)goto unavailable;
 XrPath profile=XR_NULL_PATH,binding=XR_NULL_PATH;
 CALL(StringToPath(a->instance,"/interaction_profiles/ext/eye_gaze_interaction",&profile));
 CALL(StringToPath(a->instance,"/user/eyes_ext/input/gaze_ext/pose",&binding));
 if(!profile || !binding)goto unavailable;
 XrActionSuggestedBinding suggested={e->action,binding};
 XrInteractionProfileSuggestedBinding sb={0};sb.type=XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING;
 sb.interactionProfile=profile;sb.countSuggestedBindings=1;sb.suggestedBindings=&suggested;
 CALL(SuggestInteractionProfileBindings(a->instance,&sb));
 XrReferenceSpaceCreateInfo reference={0};reference.type=XR_TYPE_REFERENCE_SPACE_CREATE_INFO;
 reference.referenceSpaceType=XR_REFERENCE_SPACE_TYPE_LOCAL;reference.poseInReferenceSpace.orientation.w=1.f;
 e->last_result=CreateReferenceSpace(e->session,&reference,&e->reference_space);a->last_result=e->last_result;update_owner(e);
 if(e->last_result!=XR_SUCCESS)goto runtime_fail;
 if(!e->reference_space)goto unavailable;
 XrActionSpaceCreateInfo space={0};space.type=XR_TYPE_ACTION_SPACE_CREATE_INFO;
 space.action=e->action;space.poseInActionSpace.orientation.w=1.f;
 e->last_result=CreateActionSpace(e->session,&space,&e->eye_space);a->last_result=e->last_result;update_owner(e);
 if(e->last_result!=XR_SUCCESS)goto runtime_fail;
 if(!e->eye_space)goto unavailable;
 XrSessionActionSetsAttachInfo attach={0};attach.type=XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO;
 attach.countActionSets=1;attach.actionSets=&e->action_set;
 CALL(AttachSessionActionSets(e->session,&attach));
 return XTOS_XR_OK;
unavailable:
 if(e->adapter) {if(xtos_xr_eye_close(e)!=XTOS_XR_OK)return XTOS_XR_RUNTIME;}
 return XTOS_XR_UNAVAILABLE;
runtime_fail:
 if(e->adapter) {if(xtos_xr_eye_close(e)!=XTOS_XR_OK)return XTOS_XR_RUNTIME;}
 return XTOS_XR_RUNTIME;
}
