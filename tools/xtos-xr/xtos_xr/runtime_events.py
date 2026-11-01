"""Validate bounded native probe records and revoke multimodal work on events.

This joins control-plane event handling only. It does not locate an eye space,
certify a runtime or use probe output as a sample clock-conversion authority.
"""
import threading
from . import MultimodalSession, XRError


class RuntimeFence:
    def __init__(self, session):
        if type(session) is not MultimodalSession:
            raise XRError('exact multimodal session required')
        self._session=session
        self._generation=0
        self._sequence=0
        self._lock=threading.Lock()

    def accept(self, record):
        with self._lock:
            try:
                if type(record) is not dict:raise XRError('native object required')
                d=record.copy()
                required={'source_kind','safe_action','physical_acceptance','generation',
                    'events','event_limit_reached','barrier','status'}
                if set(d) not in (required,required|{'conversion'}):
                    raise XRError('unexpected native record fields')
                if (d['safe_action']!='no_action' or d['physical_acceptance'] is not False or
                    d['source_kind'] not in ('simulated_runtime','runtime_unverified') or d['status']!='ok'):
                    raise XRError('native fault/refusal')
                for name in ('generation','barrier'):
                    if type(d[name]) is not int or not 0<=d[name]<1<<64:raise XRError('invalid native identity')
                if d['generation']==0 or d['generation']<self._generation:
                    raise XRError('native generation rollback')
                if type(d['event_limit_reached']) is not bool:raise XRError('invalid event limit flag')
                if type(d['events']) is not list:raise XRError('events array required')
                events=tuple(d['events'])
                if len(events)>256:raise XRError('event count exceeds bound')
                sequence=self._sequence if d['generation']==self._generation else 0
                for source in events:
                    if type(source) is not dict:raise XRError('event object required')
                    e=source.copy()
                    if set(e)-{'type','sequence','barrier','session_state','lost_count'} or not set(e)>={'type','sequence','barrier'}:
                        raise XRError('unknown event fields')
                    for name in ('type','sequence','barrier'):
                        if type(e[name]) is not int:raise XRError('invalid event integer')
                    if not -(1<<31)<=e['type']<1<<31 or e['sequence']!=sequence+1 or e['barrier']!=e['sequence']:
                        raise XRError('event ordering')
                    for name in ('session_state','lost_count'):
                        if name in e and (type(e[name]) is not int or not 0<=e[name]<1<<32):
                            raise XRError('invalid decoded event')
                    sequence=e['sequence']
                if d['barrier']!=sequence:raise XRError('native barrier differs')
                if 'conversion' in d:
                    conversion=d['conversion']
                    if type(conversion) is not dict:raise XRError('invalid conversion record')
                    c=conversion.copy()
                    if set(c)!={'raw_xr_time','host_ns','runtime_result','status'} or any(type(v) is not int for v in c.values()):
                        raise XRError('invalid conversion fields')
                    if c['status']!=0 or c['runtime_result']!=0 or not 0<c['raw_xr_time']<1<<63 or not 0<c['host_ns']<1<<63:
                        raise XRError('conversion failed')
                changed=(self._generation!=0 and d['generation']!=self._generation) or bool(events) or d['event_limit_reached']
                if changed:self._session.barrier('native runtime event/generation/limit')
                self._generation,self._sequence=d['generation'],sequence
                return 'barrier' if changed else 'current'
            except Exception:
                self._session.barrier('native runtime record rejected')
                return 'rejected'
