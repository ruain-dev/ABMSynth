"""Per-MIDI-channel descriptions and leases for the shared audio-FX processors.

No QWidget, MIDI filter, Python audio callback or synthesis worker lives here.
The existing native FIFO executes root-locked, channel-targeted topology plans.
"""
from __future__ import annotations

import ctypes
import json
import threading
from dataclasses import replace

from effect_control import (AUDIO_FX_GROUPS,AUDIO_FX_PARAMETERS,audio_fx_from_ini,audio_fx_ini,
    validate_audio_fx,audio_fx_cc_bindings,audio_fx_cc_value)


CHANNEL_FX_CC_MANUAL={'saturation_quality','quality_transition_ms','transition_ms'}


def channel_fx_cc_allowed(slot,parameter):
    return AUDIO_FX_PARAMETERS[slot.kind][parameter].key not in CHANNEL_FX_CC_MANUAL


def _cancel_learn(slot):
    bindings=audio_fx_cc_bindings(slot);extras=dict(slot.extras)
    previous=extras.pop('midi_cc_previous',{})
    for key,value in tuple(bindings.items()):
        if value=='learn':
            if type(previous.get(key)) is int:bindings[key]=previous[key]
            else:bindings.pop(key)
    if bindings:extras['midi_cc']=bindings
    else:extras.pop('midi_cc',None)
    return replace(slot,extras=tuple(extras.items()))


def channel_fx_reset_defaults(slot):
    """Optional per-identity Reset seeds; no new parameter domain or precision."""
    values=dict(slot.extras).get('reset_defaults',{})
    if not isinstance(values,dict):raise ValueError('channel effect Reset defaults')
    specs={parameter.key:parameter for parameter in AUDIO_FX_PARAMETERS[slot.kind]}
    if any(key not in specs for key in values):raise ValueError('channel effect Reset parameter')
    return {key:specs[key].validate(value) for key,value in values.items()}


class _MidiDescriptor(ctypes.Structure):
    _fields_=[('reuse',ctypes.c_uint64),('parameter',ctypes.c_uint),('cc',ctypes.c_uint),
        ('bytes',ctypes.c_uint),('kind',ctypes.c_uint),('mask',ctypes.c_void_p),
        ('payload',ctypes.c_void_p),('values',ctypes.c_void_p)]


class _MidiSnapshot(ctypes.Structure):
    _fields_=[('reuse',ctypes.c_uint64),('serial',ctypes.c_uint64),('parameter',ctypes.c_uint),
        ('cc',ctypes.c_uint),('value',ctypes.c_double)]


def prepare_channel_midi(graph,plan,slots,encode,custom_kinds):
    """Copy all conversions before FIFO admission. No realtime Python work."""
    rows=[];roots=[]
    for slot in slots:
        for key,source in audio_fx_cc_bindings(slot).items():
            index=next(i for i,p in enumerate(AUDIO_FX_PARAMETERS[slot.kind]) if p.key==key)
            if not channel_fx_cc_allowed(slot,index):raise ValueError('manual kernel control')
            parameter=AUDIO_FX_PARAMETERS[slot.kind][index]
            decoded=[audio_fx_cc_value(parameter,cc) for cc in range(128)]
            payload=[]
            for value in decoded:
                values=list(slot.values);values[index]=value
                changed=replace(slot,values=tuple(values))
                data=((ctypes.c_double*len(values))(*values) if slot.kind in custom_kinds
                      else encode(changed,graph.rack.sample_rate))
                payload.append(bytes(data))
            size=len(payload[0]);base=payload[0]
            unit=8 if slot.kind in custom_kinds else 4
            mask=(ctypes.c_ubyte*size)(*(int(any(data[j]!=base[j] for data in payload
                for j in range(i//unit*unit,min(size,(i//unit+1)*unit)))) for i in range(size)))
            data=ctypes.create_string_buffer(b''.join(payload));table=(ctypes.c_double*128)(*decoded)
            rows.append(_MidiDescriptor(graph.reuse_id(slot),index,0xffffffff if source=='learn' else source,
                size,int(slot.kind=='chorus_fx'),ctypes.addressof(mask),ctypes.addressof(data),ctypes.addressof(table)))
            roots.extend((mask,data,table))
    descriptors=(_MidiDescriptor*len(rows))(*rows)
    function=graph.library.channel_fx_midi_prepare
    function.argtypes=[ctypes.c_void_p,ctypes.POINTER(_MidiDescriptor),ctypes.c_uint];function.restype=ctypes.c_int
    if not function(plan,descriptors,len(rows)):raise ValueError('channel FX MIDI preparation')


# Channel effects expose independent processors. Source envelopes and L/R
# utilities remain output-rack controls. Asset/FFT effects need separate
# channel-latency handling before they can be added here.
CHANNEL_FX_DEFERRED = {
    'volume_envelope':'source envelope editing is available in the output rack',
    'headphone_stage':'asset kernel preparation and channel latency need independent live qualification',
    'convolution':'asset kernel preparation and channel latency need independent live qualification',
    'pitch_shift':'FFT latency and independent channel compensation need live qualification',
    'stereo_tools':'polarity and channel swap are output-rack utilities',
    'channel_matrix':'L/R matrix routing is available in the output rack',
    'all_pass':'phase-only utility deferred; not a clear general channel tone control',
    'dc_block':'DC utility deferred; not a new general instrument effect',
    'rotate':'rotation candidate overlaps auto pan; not a rotary-speaker model',
}
CHANNEL_FX_GROUPS = {group:tuple(kind for kind in kinds if kind not in CHANNEL_FX_DEFERRED)
                     for group,kinds in AUDIO_FX_GROUPS.items()}
CHANNEL_FX_KINDS = frozenset(kind for kinds in CHANNEL_FX_GROUPS.values() for kind in kinds)


class ChannelAudioEffects:
    def __init__(self,effects,bassmidi,*,rack_factory,graph_factory,snapshot_type):
        self.effects,self.bassmidi=effects,bassmidi
        self.rack_factory,self.graph_factory,self.snapshot_type=rack_factory,graph_factory,snapshot_type
        self.stream=0;self.sample_rate=96000;self.owner=self.library=None
        self._lock=threading.RLock();self._states={};self._racks={};self._requests={};self.revision=0
        self._midi_seen={};self._midi_failures={};self.midi_errors=0

    def attach(self,stream,rate):
        if not self.detach():raise RuntimeError('channel audio FX retirement')
        self.stream,self.sample_rate=int(stream),int(rate)

    def snapshot(self,channel):
        self.sync_midi()
        with self._lock:return self._states.get(channel,((),True))

    def sync_midi(self):
        """Read native copied results; no audio API or GUI acknowledgement."""
        if self.library is None:return
        function=self.library.channel_fx_midi_snapshot
        function.argtypes=[ctypes.c_void_p,ctypes.POINTER(_MidiSnapshot),ctypes.c_uint,ctypes.POINTER(ctypes.c_uint64)]
        function.restype=ctypes.c_uint
        with self._lock:
            for channel,rack in tuple(self._racks.items()):
                if any(ch==channel for ch,_token in self._requests):continue
                graph=rack._topology_graph
                if graph is None or not graph.pointer:continue
                rows=(_MidiSnapshot*256)();failures=ctypes.c_uint64()
                count=function(graph.pointer,rows,256,ctypes.byref(failures))
                if count==0xffffffff:raise ValueError('channel MIDI FX snapshot')
                prior=self._midi_failures.get(channel,0)
                self.midi_errors+=max(0,failures.value-prior);self._midi_failures[channel]=failures.value
                slots,enabled=self._states.get(channel,((),True));slots=list(slots);changed=False
                for row in sorted(rows[:count],key=lambda value:value.serial):
                    key=(channel,int(graph.pointer),row.reuse,row.parameter)
                    if not row.serial or row.serial<=self._midi_seen.get(key,0):continue
                    if not 0<row.reuse<=len(graph.reuse_keys):raise ValueError('channel MIDI slot identity')
                    kind,identity=graph.reuse_keys[row.reuse-1]
                    index=next((i for i,slot in enumerate(slots) if (slot.kind,slot.identity)==(kind,identity)),None)
                    if index is None:continue
                    slot=slots[index];bindings=audio_fx_cc_bindings(slot)
                    parameter=AUDIO_FX_PARAMETERS[kind][row.parameter]
                    if parameter.key not in bindings:continue
                    self._midi_seen[key]=row.serial
                    values=list(slot.values);values[row.parameter]=parameter.validate(row.value)
                    if kind=='chorus_fx':
                        if row.parameter==3:values[4]=max(values[4],values[3])
                        elif row.parameter==4:values[3]=min(values[3],values[4])
                    bindings[parameter.key]=int(row.cc)
                    extras=dict(slot.extras);extras['midi_cc']=bindings
                    previous=dict(extras.get('midi_cc_previous',{}));previous.pop(parameter.key,None)
                    if previous:extras['midi_cc_previous']=previous
                    else:extras.pop('midi_cc_previous',None)
                    slots[index]=replace(slot,values=tuple(values),extras=tuple(extras.items())).validated();changed=True
                if changed:self._states[channel]=(tuple(slots),enabled);self.revision+=1

    def assign_cc(self,channel,identity,parameter,source):
        self.sync_midi()
        with self._lock:
            slots,enabled=next((request for (ch,_token),request in reversed(tuple(self._requests.items()))
                if ch==channel),self._states.get(channel,((),True)))
        # One armed target,like the existing channel Learn. Cancelling restores
        # its former source; a partial Learn is never a persisted preference.
        if source=='learn':
            for other,(records,active) in tuple(self._states.items()):
                if other!=channel and any('learn' in audio_fx_cc_bindings(slot).values() for slot in records):
                    self.request(other,tuple(_cancel_learn(slot) for slot in records),active)
            slots=tuple(_cancel_learn(slot) for slot in slots)
        updated=[];found=False
        for slot in slots:
            if slot.identity==identity:
                found=True
                if not channel_fx_cc_allowed(slot,parameter):raise ValueError('manual kernel control')
                bindings=audio_fx_cc_bindings(slot);key=AUDIO_FX_PARAMETERS[slot.kind][parameter].key
                extras=dict(slot.extras);previous=dict(extras.get('midi_cc_previous',{}))
                if source is None and bindings.get(key)=='learn':
                    old=previous.pop(key,None)
                    if type(old) is int:bindings[key]=old
                    else:bindings.pop(key,None)
                elif source in (None,'clear'):bindings.pop(key,None);previous.pop(key,None)
                elif source=='learn':
                    previous[key]=bindings.get(key);bindings[key]='learn'
                elif type(source) is int and 0<=source<120:bindings[key]=source;previous.pop(key,None)
                else:raise ValueError('channel effect CC source')
                if previous:extras['midi_cc_previous']=previous
                else:extras.pop('midi_cc_previous',None)
                if bindings:extras['midi_cc']=bindings
                else:extras.pop('midi_cc',None)
                slot=replace(slot,extras=tuple(extras.items()))
            updated.append(slot)
        if not found:raise ValueError('channel effect identity')
        return self.request(channel,tuple(updated),enabled)

    def apply_offline_cc(self,channel,cc,value):
        if self.library is None or not 0<=cc<120:return
        function=self.library.channel_fx_midi_event
        function.argtypes=[ctypes.c_uint]*4;function.restype=ctypes.c_int
        result=function(self.stream,channel,cc,value)
        if result==-2:
            # Same offline producer owns the failed unlock. Retry its barrier
            # before exposing failure;never move this cleanup to another thread.
            if not self.effects.bass.BASS_ChannelLock(self.stream,False):raise RuntimeError('channel FX retained unlock')
        if result!=1:raise RuntimeError('channel FX MIDI application')

    def reset_values(self,channel):
        """Reset values only; retain processor IDs/order, bypass and metadata."""
        return next((row[1:] for row in self._edit_configuration(channel,self._reset_slot)),(None,True))

    def request_controls(self,channel,slots,enabled,previous,force_parameters):
        """UI edits own their changed values;configuration metadata has one owner.

        A late editor snapshot must not restore removed CC routes or erase new
        Reset seeds. It must not copy other stale values over an earlier Reset
        or newer MIDI CC. Stable identities make reorder independent of this.
        """
        self.sync_midi()
        with self._lock:
            current=next((state for (ch,_token),state in reversed(tuple(self._requests.items()))
                          if ch==channel),self._states.get(channel,((),True)))[0]
            latest={slot.identity:slot for slot in current}
            baseline={slot.identity:slot for slot in previous}
            updated=[]
            for slot in slots:
                actual,old=latest.get(slot.identity),baseline.get(slot.identity)
                if actual is not None and old is not None and actual.kind==slot.kind==old.kind:
                    forced=force_parameters.get(slot.identity,())
                    values=tuple(value if i in forced or value!=old.values[i] else actual.values[i]
                        for i,value in enumerate(slot.values))
                    extras=dict(slot.extras);owned=dict(actual.extras)
                    for key in ('midi_cc','midi_cc_previous','reset_defaults'):
                        extras.pop(key,None)
                        if key in owned:extras[key]=owned[key]
                    slot=replace(slot,values=values,extras=tuple(extras.items()),
                        enabled=slot.enabled if slot.enabled!=old.enabled else actual.enabled)
                updated.append(slot)
            updated=validate_audio_fx(updated)
            token,result=self.request(channel,updated,enabled)
            return token,result,updated

    @staticmethod
    def _reset_slot(slot):
        defaults=channel_fx_reset_defaults(slot)
        return replace(_cancel_learn(slot),values=tuple(defaults.get(parameter.key,parameter.initial)
            for parameter in AUDIO_FX_PARAMETERS[slot.kind])).validated()

    def reset_all_values(self):return self._edit_configuration(None,self._reset_slot)

    def clear_assignments(self,channel=None):
        def cleared(slot):
            extras={key:value for key,value in slot.extras if key not in ('midi_cc','midi_cc_previous')}
            return replace(slot,extras=tuple(extras.items()))
        return self._edit_configuration(channel,cleared)

    def replace_reset_defaults(self,channels,values):
        channels=frozenset(channels)
        def selected(channel,slot):
            if channel not in channels:return slot
            defaults={parameter.key:values[channel,slot.identity,parameter.key]
                for parameter in AUDIO_FX_PARAMETERS[slot.kind] if (channel,slot.identity,parameter.key) in values}
            return self.with_reset_defaults(slot,defaults)
        return self._edit_configuration(None,None,selected)

    @classmethod
    def with_reset_defaults(cls,slot,defaults):
        extras=dict(slot.extras)
        if defaults:extras['reset_defaults']=defaults
        else:extras.pop('reset_defaults',None)
        updated=replace(slot,extras=tuple(extras.items()))
        cls._reset_slot(updated) # Validate coupled parameters before any write.
        return updated

    def _edit_configuration(self,channel,transform,scoped_transform=None):
        """One scope/latest-request owner for values,Learn and Reset seed edits."""
        if channel is not None and (type(channel) is not int or not 0<=channel<128):raise ValueError('channel effects scope')
        self.sync_midi()
        with self._lock:
            latest=dict(self._states)
            for (ch,_token),state in self._requests.items():latest[ch]=state
        prepared=[]
        for ch,(slots,enabled) in sorted(latest.items()):
            if channel is not None and ch!=channel:continue
            updated=tuple(scoped_transform(ch,slot) if scoped_transform else transform(slot) for slot in slots)
            if audio_fx_ini(updated)==audio_fx_ini(slots):continue
            prepared.append((ch,updated,enabled))
        results=[]
        for ch,updated,enabled in prepared:
            if self.stream and self.owner is not None and ch>=self.owner.addresses.count:
                # Stored inactive ports have no live child; never create one.
                with self._lock:self._states[ch]=(updated,enabled);self.revision+=1
                result=(None,True)
            else:result=self.request(ch,updated,enabled)
            results.append((ch,*result))
        return tuple(results)

    def ini_value(self):
        self.sync_midi()
        with self._lock:
            rows={str(ch):{'enabled':enabled,'effects':json.loads(audio_fx_ini(tuple(_cancel_learn(slot) for slot in slots)))}
                  for ch,(slots,enabled) in sorted(self._states.items()) if slots or not enabled}
        return json.dumps(rows,ensure_ascii=False,separators=(',',':'))

    def restore_ini(self,raw,*,report=None):
        states={}
        try:
            from configuration_data import read_settings_json
            rows=read_settings_json(raw)
            if not isinstance(rows,dict):raise ValueError('channel effects document')
            for key,row in rows.items():
                channel=int(key)
                if str(channel)!=key or not 0<=channel<128 or not isinstance(row,dict) or type(row.get('enabled',True)) is not bool:
                    raise ValueError('channel effects address')
                records=row.get('effects',[])
                if not isinstance(records,list):raise ValueError('channel effects list')
                slots=audio_fx_from_ini(json.dumps(records,ensure_ascii=False))
                if len(slots)!=len(records) or any(slot.kind not in CHANNEL_FX_KINDS for slot in slots):
                    raise ValueError('channel effects processor')
                from uuid import uuid4
                slots=tuple(_cancel_learn(replace(slot,identity=slot.identity or uuid4().hex)) for slot in slots)
                for slot in slots:
                    bindings=audio_fx_cc_bindings(slot)
                    self._reset_slot(slot)
                    if any(not channel_fx_cc_allowed(slot,i) for i,p in enumerate(AUDIO_FX_PARAMETERS[slot.kind]) if p.key in bindings):
                        raise ValueError('manual kernel MIDI mapping')
                if len({slot.identity for slot in slots})!=len(slots):raise ValueError('channel effects identity')
                states[channel]=(slots,row.get('enabled',True))
        except (ValueError,TypeError,KeyError,OverflowError):
            if report is not None:report.record('UI','channel_audio_fx','invalid')
            return False
        with self._lock:
            if self._racks:raise ValueError('channel FX restore requires retired generation')
            self._states=states;self.revision+=1
        if report is not None:report.record('UI','channel_audio_fx','accepted')
        return True

    def bind(self,owner,library):
        midi_version=library.channel_fx_midi_version;midi_version.argtypes=[];midi_version.restype=ctypes.c_uint
        if midi_version()!=1:raise ValueError('channel MIDI FX ABI')
        version=library.fx_topology_channel_version;version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:raise ValueError('channel audio FX ABI')
        for name,result in (('channel_fx_delay_release',ctypes.c_int),('channel_fx_delay_acquire',None)):
            fn=getattr(library,name);fn.argtypes=[ctypes.c_uint]*3;fn.restype=result
        self.owner,self.library=owner,library
        self.effects.delay.channel_fx_library=library
        # Restore only channels already present. Dormant port descriptions are
        # retained without expanding the MIDI address space or opening devices.
        for channel in tuple(self._states):
            if channel<owner.addresses.count:
                slots,enabled=self.snapshot(channel)
                if slots:self.request(channel,slots,enabled)

    def bind_offline(self,owner):
        """Single-producer export, same native plan and existing processors."""
        import os
        from pathlib import Path
        library=ctypes.CDLL(str(os.environ.get('ABMSYNTH_OBSERVATION_DLL',Path(__file__).with_name('abmsynth_dsp.dll'))))
        self._offline=True
        self.bind(owner,library)

    def _rack(self,channel):
        if channel in self._racks:return self._racks[channel]
        rack=self.rack_factory(self.effects.bass,self.effects._check)
        rack.channel_index=channel
        # bassmidi.h StreamGetChannel: borrowed float child, root generation
        # owns the stream. Native selection and this rack share its lease.
        rack.channel_callbacks=(self.bassmidi.BASS_MIDI_StreamGetChannel,
            self.effects.bass.BASS_StreamFree,self.bassmidi.BASS_MIDI_StreamGetEvent)
        rack.attach(self.stream,self.sample_rate)
        rack.on_topology_result=lambda token,success:self._completed(channel,token,success)
        if getattr(self,'_offline',False):
            rack._topology_graph=self.graph_factory(rack,self.library)
        else:rack.bind_topology_commands(self.owner,self.library,self.owner.submit_effect_topology)
        self._racks[channel]=rack
        return rack

    def _completed(self,channel,token,success):
        # Native metadata consumer: update descriptions only. No Qt/BASS/INI.
        with self._lock:
            request=self._requests.pop((channel,token),None)
            if success and request is not None:
                self._states[channel]=request;self.revision+=1

    def request(self,channel,slots,enabled=True):
        self.sync_midi()
        slots=validate_audio_fx(slots)
        for slot in slots:
            audio_fx_cc_bindings(slot);channel_fx_reset_defaults(slot)
        if type(channel) is not int or not 0<=channel<128 or any(slot.kind not in CHANNEL_FX_KINDS for slot in slots):
            raise ValueError('channel audio FX scope')
        with self._lock:
            if not self.stream:
                self._states[channel]=(slots,bool(enabled));self.revision+=1
                return None,True
            if self.owner is None or channel>=self.owner.addresses.count:return None,False
            rack=self._rack(channel);expected=rack._prepare_serial+1
            self._requests[channel,expected]=(slots,bool(enabled))
            try:
                if getattr(self,'_offline',False):
                    plan=rack._topology_graph.prepare(slots if enabled else tuple(replace(slot,enabled=False) for slot in slots))
                    status=(ctypes.c_uint*6)();rows=(self.snapshot_type*64)()
                    run=self.library.fx_topology_channel_run
                    run.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.POINTER(self.snapshot_type)];run.restype=ctypes.c_int
                    outcome=run(plan.pointer,status,rows)
                    if outcome!=1:raise RuntimeError('offline channel FX execution/unlock')
                    def row(value):return (value.base.exact,value.base.handle,value.base.kind,value.base.priority,value.base.reserved,value.pointer,value.kernel)
                    report=dict(topology_command=True,phase=int(status[0]),topology_faulted=bool(status[5]),
                        topology_current=tuple(row(rows[i]) for i in range(status[3])),
                        topology_retired=tuple(row(rows[i]) for i in range(status[3],status[3]+status[4])))
                    rack._topology_graph.replay(plan,report);plan.close()
                    rack._prepare_serial=expected;token,result=expected,status[0]==0
                else:token,result=rack.request(slots if enabled else tuple(replace(slot,enabled=False) for slot in slots))
            except BaseException:
                self._requests.pop((channel,expected),None);raise
            if token!=expected:raise ValueError('channel FX request identity')
            if result is not None:self._completed(channel,token,bool(result))
            return (channel,self.owner.stream_generation,token),result

    def completed(self,token):
        channel,generation,serial=token
        if self.owner is None or generation!=self.owner.stream_generation:return False
        rack=self._racks.get(channel)
        return rack.completed_topology_request(serial) if rack is not None else False

    def poll(self,token):
        # Channel candidates exclude detached asset preparation, so this is
        # an immutable ACK read. MIDI and rendering never wait for a UI timer.
        return self.completed(token)

    def detach(self):
        # Native session retirement has already drained accepted plans and
        # closed graph leases. AudioSession stops output before this removal.
        for channel,rack in tuple(self._racks.items()):
            if rack.detach() is False:return False
            self._racks.pop(channel)
        self.effects.delay.channel_fx_library=None
        self.stream=0;self.owner=self.library=None;self._offline=False;self._requests.clear()
        self._midi_seen.clear();self._midi_failures.clear()
        return True

    def binding(self,channel):
        return ChannelAudioFXBinding(self,channel)


class ChannelAudioFXBinding:
    """Explicit channel argument adapter for the existing shared FX editor."""
    def __init__(self,manager,channel):self.manager,self.channel=manager,channel
    def request_audio_fx(self,slots,enabled=True):return self.manager.request(self.channel,slots,enabled)
    def request_audio_fx_edit(self,slots,enabled,previous,force_parameters):
        return self.manager.request_controls(self.channel,slots,enabled,previous,force_parameters)
    def poll_audio_fx(self,token):return self.manager.poll(token)
    def completed_audio_fx_request(self,token):return self.manager.completed(token)
