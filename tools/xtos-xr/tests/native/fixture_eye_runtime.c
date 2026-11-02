#include <time.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>
/* Compiled Khronos ABI fixture. Never a physical eye tracker. */
static int mode,instances,instance_destroys,sessions,session_destroys,actions,action_destroys,sets,set_destroys,spaces,space_destroys,conversion_calls,events,begin_calls,end_calls,locate_calls;
static XrSession current_session;
static int exit_requests;
#define H(type,n) ((type)(uintptr_t)(n))
#define FAIL_IF(x) do {if(x) return XR_ERROR_VALIDATION_FAILURE;}while(0)
void eye_reset(int m) {mode=m;instances=instance_destroys=sessions=session_destroys=actions=action_destroys=sets=set_destroys=spaces=space_destroys=conversion_calls=events=begin_calls=end_calls=locate_calls=0;current_session=XR_NULL_HANDLE;exit_requests=0;}
void eye_allow_cleanup(void) {mode=0;}
int eye_instances(void) {return instances;}
int eye_instance_destroys(void) {return instance_destroys;}
int eye_sessions(void) {return sessions;}
int eye_session_destroys(void) {return session_destroys;}
int eye_spaces(void) {return spaces;}
int eye_space_destroys(void) {return space_destroys;}
int eye_actions(void) {return actions;}
int eye_action_destroys(void) {return action_destroys;}
int eye_sets(void) {return sets;}
int eye_set_destroys(void) {return set_destroys;}
int eye_conversion_calls(void) {return conversion_calls;}
int eye_begin_calls(void) {return begin_calls;}
int eye_end_calls(void) {return end_calls;}
int eye_exit_requests(void) {return exit_requests;}
int eye_locate_calls(void) {return locate_calls;}
static XrResult XRAPI_CALL extensions(const char *layer,uint32_t cap,uint32_t *count,XrExtensionProperties *out) {
 FAIL_IF(layer || !count);*count=mode==1 || mode==5?2:3;
 if(!cap) return XR_SUCCESS;
 FAIL_IF(cap<*count || !out);
 const char *names[]={XR_KHR_CONVERT_TIMESPEC_TIME_EXTENSION_NAME,XR_MND_HEADLESS_EXTENSION_NAME,XR_EXT_EYE_GAZE_INTERACTION_EXTENSION_NAME};
 if(mode==5) names[1]=XR_EXT_EYE_GAZE_INTERACTION_EXTENSION_NAME;
 for(uint32_t j=0;j<*count;j++) {FAIL_IF(out[j].type!=XR_TYPE_EXTENSION_PROPERTIES || out[j].next);strcpy(out[j].extensionName,names[j]);out[j].extensionVersion=1;}
 return XR_SUCCESS;
}
static XrResult XRAPI_CALL create_instance(const XrInstanceCreateInfo *ci,XrInstance *out) {
 FAIL_IF(ci->type!=XR_TYPE_INSTANCE_CREATE_INFO || ci->next || ci->enabledExtensionCount!=3 || ci->enabledApiLayerCount || !out);
 const char *names[]={XR_KHR_CONVERT_TIMESPEC_TIME_EXTENSION_NAME,XR_MND_HEADLESS_EXTENSION_NAME,XR_EXT_EYE_GAZE_INTERACTION_EXTENSION_NAME};
 for(int i=0;i<3;i++) FAIL_IF(strcmp(ci->enabledExtensionNames[i],names[i]));
 *out=H(XrInstance,1);instances++;return XR_SUCCESS;
}
static XrResult XRAPI_CALL destroy_instance(XrInstance i) {
 FAIL_IF(i!=H(XrInstance,1) || sessions!=session_destroys || spaces!=space_destroys || actions!=action_destroys || sets!=set_destroys);
 instance_destroys++;return XR_SUCCESS;
}
XRAPI_ATTR XrResult XRAPI_CALL xrDestroyInstance(XrInstance i) {return destroy_instance(i);}
static XrResult XRAPI_CALL poll_event(XrInstance i,XrEventDataBuffer *out) {
 FAIL_IF(i!=H(XrInstance,1) || out->type!=XR_TYPE_EVENT_DATA_BUFFER || out->next);
 if(!current_session || events>2) return XR_EVENT_UNAVAILABLE;
 XrEventDataSessionStateChanged e={XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED,0,current_session,events==0?XR_SESSION_STATE_READY:(events==1?XR_SESSION_STATE_FOCUSED:XR_SESSION_STATE_STOPPING),100};
 if(mode==9 && events==1)e.session=H(XrSession,99);
 if(mode==10 && events==1)e.state=XR_SESSION_STATE_LOSS_PENDING;
 events++;memcpy(out,&e,sizeof(e));return XR_SUCCESS;
}
static XrResult XRAPI_CALL to_host(XrInstance i,XrTime t,struct timespec *out) {
 FAIL_IF(i!=H(XrInstance,1) || t<=0 || !out);conversion_calls++;out->tv_sec=0;out->tv_nsec=(long)(t+conversion_calls*1000);return XR_SUCCESS;
}
static XrResult XRAPI_CALL to_xr(XrInstance i,const struct timespec *in,XrTime *out) {
 FAIL_IF(i!=H(XrInstance,1) || !in || !out);*out=(XrTime)in->tv_sec*1000000000+in->tv_nsec;return XR_SUCCESS;
}
static XrResult XRAPI_CALL get_system(XrInstance i,const XrSystemGetInfo *in,XrSystemId *out) {
 FAIL_IF(i!=H(XrInstance,1) || !in || in->type!=XR_TYPE_SYSTEM_GET_INFO || in->next || in->formFactor!=XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY || !out);
 if(mode==11)return XR_ERROR_FORM_FACTOR_UNAVAILABLE;
 *out=42;return XR_SUCCESS;
}
static XrResult XRAPI_CALL get_properties(XrInstance i,XrSystemId sid,XrSystemProperties *out) {
 FAIL_IF(i!=H(XrInstance,1) || sid!=42 || !out || out->type!=XR_TYPE_SYSTEM_PROPERTIES || !out->next);
 XrSystemEyeGazeInteractionPropertiesEXT *eye=out->next;
 FAIL_IF(eye->type!=XR_TYPE_SYSTEM_EYE_GAZE_INTERACTION_PROPERTIES_EXT || eye->next);
 eye->supportsEyeGazeInteraction=(mode==6)?XR_FALSE:XR_TRUE;out->systemId=sid;return XR_SUCCESS;
}
static XrResult XRAPI_CALL create_session(XrInstance i,const XrSessionCreateInfo *ci,XrSession *out) {
 FAIL_IF(i!=H(XrInstance,1) || !ci || ci->type!=XR_TYPE_SESSION_CREATE_INFO || ci->next || ci->createFlags || ci->systemId!=42 || !out);
 if(mode==7)return XR_ERROR_RUNTIME_FAILURE;
 *out=H(XrSession,2);current_session=*out;sessions++;return XR_SUCCESS;
}
static XrResult XRAPI_CALL destroy_session(XrSession s) {
 FAIL_IF(s!=current_session || spaces!=space_destroys);
 if(mode==8)return XR_ERROR_RUNTIME_FAILURE;
 session_destroys++;current_session=XR_NULL_HANDLE;return XR_SUCCESS;
}
static XrResult XRAPI_CALL string_to_path(XrInstance i,const char *str,XrPath *out) {
 FAIL_IF(i!=H(XrInstance,1) || !str || !out);
 if(!strcmp(str,"/interaction_profiles/ext/eye_gaze_interaction"))*out=11;
 else if(!strcmp(str,"/user/eyes_ext/input/gaze_ext/pose"))*out=12;
 else return XR_ERROR_PATH_UNSUPPORTED;
 return XR_SUCCESS;
}
static XrResult XRAPI_CALL create_set(XrInstance i,const XrActionSetCreateInfo *ci,XrActionSet *out) {
 FAIL_IF(i!=H(XrInstance,1) || ci->type!=XR_TYPE_ACTION_SET_CREATE_INFO || ci->next || !ci->actionSetName[0] || !ci->localizedActionSetName[0] || !out);
 *out=H(XrActionSet,3);sets++;return XR_SUCCESS;
}
static XrResult XRAPI_CALL destroy_set(XrActionSet s) {FAIL_IF(s!=H(XrActionSet,3) || actions!=action_destroys);set_destroys++;return XR_SUCCESS;}
static XrResult XRAPI_CALL create_action(XrActionSet set,const XrActionCreateInfo *ci,XrAction *out) {
 FAIL_IF(set!=H(XrActionSet,3) || ci->type!=XR_TYPE_ACTION_CREATE_INFO || ci->next || ci->actionType!=XR_ACTION_TYPE_POSE_INPUT || ci->countSubactionPaths || !ci->actionName[0] || !ci->localizedActionName[0] || !out);
 *out=H(XrAction,4);actions++;return XR_SUCCESS;
}
static XrResult XRAPI_CALL destroy_action(XrAction a) {FAIL_IF(a!=H(XrAction,4));action_destroys++;return XR_SUCCESS;}
static XrResult XRAPI_CALL suggest(XrInstance i,const XrInteractionProfileSuggestedBinding *b) {
 FAIL_IF(i!=H(XrInstance,1) || b->type!=XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING || b->next || b->interactionProfile!=11 || b->countSuggestedBindings!=1 || !b->suggestedBindings || b->suggestedBindings[0].action!=H(XrAction,4) || b->suggestedBindings[0].binding!=12);
 return XR_SUCCESS;
}
static XrResult XRAPI_CALL reference_space(XrSession s,const XrReferenceSpaceCreateInfo *ci,XrSpace *out) {
 FAIL_IF(s!=current_session || ci->type!=XR_TYPE_REFERENCE_SPACE_CREATE_INFO || ci->next || ci->referenceSpaceType!=XR_REFERENCE_SPACE_TYPE_LOCAL || ci->poseInReferenceSpace.orientation.w!=1.f || !out);
 *out=H(XrSpace,5);spaces++;return XR_SUCCESS;
}
static XrResult XRAPI_CALL action_space(XrSession s,const XrActionSpaceCreateInfo *ci,XrSpace *out) {
 FAIL_IF(s!=current_session || ci->type!=XR_TYPE_ACTION_SPACE_CREATE_INFO || ci->next || ci->action!=H(XrAction,4) || ci->subactionPath!=XR_NULL_PATH || ci->poseInActionSpace.orientation.w!=1.f || !out);
 *out=H(XrSpace,6);spaces++;return XR_SUCCESS;
}
static XrResult XRAPI_CALL destroy_space(XrSpace space) {FAIL_IF(space!=H(XrSpace,5) && space!=H(XrSpace,6));space_destroys++;return XR_SUCCESS;}
static XrResult XRAPI_CALL attach(XrSession s,const XrSessionActionSetsAttachInfo *ci) {
 FAIL_IF(s!=current_session || ci->type!=XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO || ci->next || ci->countActionSets!=1 || !ci->actionSets || ci->actionSets[0]!=H(XrActionSet,3));return XR_SUCCESS;
}
static XrResult XRAPI_CALL begin(XrSession s,const XrSessionBeginInfo *ci) {
 FAIL_IF(s!=current_session || ci->type!=XR_TYPE_SESSION_BEGIN_INFO || ci->next || ci->primaryViewConfigurationType!=XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO);begin_calls++;return XR_SUCCESS;
}
static XrResult XRAPI_CALL end(XrSession s) {FAIL_IF(s!=current_session);end_calls++;return XR_SUCCESS;}
static XrResult XRAPI_CALL request_exit(XrSession s) {FAIL_IF(s!=current_session);exit_requests++;return XR_SUCCESS;}
static XrResult XRAPI_CALL sync_actions(XrSession s,const XrActionsSyncInfo *ci) {
 FAIL_IF(s!=current_session || ci->type!=XR_TYPE_ACTIONS_SYNC_INFO || ci->next || ci->countActiveActionSets!=1 || ci->activeActionSets[0].actionSet!=H(XrActionSet,3) || ci->activeActionSets[0].subactionPath!=XR_NULL_PATH);return XR_SUCCESS;
}
static XrResult XRAPI_CALL state_pose(XrSession s,const XrActionStateGetInfo *ci,XrActionStatePose *out) {
 FAIL_IF(s!=current_session || ci->type!=XR_TYPE_ACTION_STATE_GET_INFO || ci->next || ci->action!=H(XrAction,4) || ci->subactionPath!=XR_NULL_PATH || out->type!=XR_TYPE_ACTION_STATE_POSE);
 out->isActive=mode==12?XR_FALSE:XR_TRUE;return XR_SUCCESS;
}
static XrResult XRAPI_CALL locate(XrSpace eye,XrSpace base,XrTime time,XrSpaceLocation *out) {
 FAIL_IF(eye!=H(XrSpace,6) || base!=H(XrSpace,5) || time<=0 || out->type!=XR_TYPE_SPACE_LOCATION || !out->next);
 XrEyeGazeSampleTimeEXT *sample=out->next;FAIL_IF(sample->type!=XR_TYPE_EYE_GAZE_SAMPLE_TIME_EXT || sample->next);
 locate_calls++;sample->time=mode==15?0:123456;out->locationFlags=XR_SPACE_LOCATION_POSITION_VALID_BIT|XR_SPACE_LOCATION_ORIENTATION_VALID_BIT|XR_SPACE_LOCATION_POSITION_TRACKED_BIT|XR_SPACE_LOCATION_ORIENTATION_TRACKED_BIT;
 if(mode==13)out->locationFlags &= ~XR_SPACE_LOCATION_ORIENTATION_TRACKED_BIT;
 out->pose.orientation.w=mode==16?2.f:1.f;
 out->pose.position.z=mode==14?NAN:-1.f;
 return XR_SUCCESS;
}
XRAPI_ATTR XrResult XRAPI_CALL xrGetInstanceProcAddr(XrInstance i,const char *name,PFN_xrVoidFunction *out) {
 if(!out)return XR_ERROR_VALIDATION_FAILURE;
 *out=0;
 #define MAP(n,f) if(!strcmp(name,"xr" #n)) *out=(PFN_xrVoidFunction)f
 if(!i) {MAP(EnumerateInstanceExtensionProperties,extensions);MAP(CreateInstance,create_instance);return *out?XR_SUCCESS:XR_ERROR_FUNCTION_UNSUPPORTED;}
 if(i!=H(XrInstance,1))return XR_ERROR_HANDLE_INVALID;
 MAP(DestroyInstance,destroy_instance);MAP(PollEvent,poll_event);MAP(ConvertTimeToTimespecTimeKHR,to_host);MAP(ConvertTimespecTimeToTimeKHR,to_xr);
 MAP(GetSystem,get_system);MAP(GetSystemProperties,get_properties);MAP(CreateSession,create_session);MAP(DestroySession,destroy_session);MAP(StringToPath,string_to_path);
 MAP(CreateActionSet,create_set);MAP(DestroyActionSet,destroy_set);MAP(CreateAction,create_action);MAP(DestroyAction,destroy_action);MAP(SuggestInteractionProfileBindings,suggest);
 MAP(CreateReferenceSpace,reference_space);MAP(CreateActionSpace,action_space);MAP(DestroySpace,destroy_space);MAP(AttachSessionActionSets,attach);
 MAP(BeginSession,begin);MAP(EndSession,end);MAP(RequestExitSession,request_exit);MAP(SyncActions,sync_actions);MAP(GetActionStatePose,state_pose);MAP(LocateSpace,locate);
 #undef MAP
 if(mode==2 && !strcmp(name,"xrCreateSession"))*out=0;
 return *out?XR_SUCCESS:XR_ERROR_FUNCTION_UNSUPPORTED;
}
