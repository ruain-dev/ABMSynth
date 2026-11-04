"""Incoming MIDI listener, transforms, channel sends and reset state.

Keep related MIDI-input behavior in one module while preserving separate
classes for independent runtime ownership and testability.
"""

from __future__ import annotations

import ctypes
import bisect
import json
import mmap
import math
import os
import queue
import threading
import time
from collections import Counter, deque
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
from effect_presentation import numeric_entry_domain

from bass_bindings import (
    SYNCPROC,
    BASS_MIDI_EVENTS_RAW, BASS_MIDI_EVENTS_FILTER, BASS_MIDI_EVENTS_SYNC,
    MIDI_EVENT_MOD_VIBRATO, MIDI_EVENT_MOD_PITCH, MIDI_EVENT_MOD_FILTER, MIDI_EVENT_MOD_VOLUME,
    MIDI_EVENT_CHORUS, MIDI_EVENT_NOTESOFF, MIDI_EVENT_RESET, MIDI_EVENT_USERFX,
    MIDI_EVENT_REVERB, MIDI_EVENT_SOUNDOFF, MIDI_EVENT_REVERB_MACRO, MIDI_EVENT_CHORUS_MACRO,
    MIDI_EVENT_DRUM_PAN, MIDI_EVENT_DRUM_REVERB, MIDI_EVENT_DRUM_CHORUS, MIDI_EVENT_DRUM_USERFX,
    MIDI_EVENT_DRUM_LEVEL, MIDI_EVENT_SCALETUNING, MIDI_EVENT_MASTER_FINETUNE,
    MIDI_EVENT_MASTER_COARSETUNE,
    MIDI_EVENT_DRUM_FINETUNE, MIDI_EVENT_DRUM_COARSETUNE,
    MIDI_EVENT_DRUM_CUTOFF, MIDI_EVENT_DRUM_RESONANCE,
    MIDI_EVENT_PITCHRANGE, MIDI_EVENT_VIBRATO_RATE,
    MIDI_EVENT_VIBRATO_DEPTH, MIDI_EVENT_VIBRATO_DELAY,
    MIDI_EVENT_PORTAMENTO, MIDI_EVENT_PORTATIME, MIDI_EVENT_MODE,
    MIDI_EVENT_FINETUNE, MIDI_EVENT_COARSETUNE, MIDI_EVENT_EXPRESSION,
    MIDI_EVENT_CUTOFF, MIDI_EVENT_RESONANCE, MIDI_EVENT_ATTACK,
    MIDI_EVENT_DECAY, MIDI_EVENT_RELEASE,
    MIDI_EVENT_SYSTEM,
    MIDI_EVENT_MIXLEVEL, MIDI_EVENT_TRANSPOSE,
    MIDI_EVENT_PROGRAM, MIDI_EVENT_BANK, MIDI_EVENT_BANK_LSB, MIDI_EVENT_DRUMS,
    MIDI_EVENT_CHANPRES_VIBRATO, MIDI_EVENT_CHANPRES_PITCH,
    MIDI_EVENT_CHANPRES_FILTER, MIDI_EVENT_CHANPRES_VOLUME,
    MIDI_EVENT_KEYPRES_VIBRATO, MIDI_EVENT_KEYPRES_PITCH,
    MIDI_EVENT_KEYPRES_FILTER, MIDI_EVENT_KEYPRES_VOLUME,
    MIDI_EVENT_VOLUME, MIDI_EVENT_PAN, MIDI_EVENT_PAN_LSB, MIDI_EVENT_PITCH, MIDI_EVENT_MODULATION,
    MIDI_EVENT_SUSTAIN, MIDI_EVENT_SOSTENUTO, MIDI_EVENT_SOFT, MIDI_EVENT_CHANPRES, MIDI_EVENT_KEYPRES,
)
from delay_runtime import bmsynth_delay_curve
from soundfont_files import (INSTRUMENT_VOLUME_MAX, PRESET_EXPRESSION_RANGES,
                            DrumKeyMix, DRUM_NEUTRAL, DRUM_KEY_DOMAINS,
                            SCALE_TUNING_DOMAIN, performance_profile_key)
from winmm_midi_pipe import WinMMMidiPipeServer
from event_log import log_text
from i18n import tr


def _snapshot_word_rows(value,count,width,maximum=0xffffffffffffffff):
    """Own and validate a rectangular unsigned image before any state write.

    The same exact-int/range checks apply to every cell, including empty
    channels. Batch the numeric scan rather than allocating a validator call,
    type set and min/max scan for each tiny watcher/accepted-event row.
    This changes no admission, epoch check or handoff publication boundary.
    """
    from itertools import chain
    if not isinstance(value,(tuple,list)) or len(value)!=count:
        raise ValueError('snapshot row dimensions')
    rows=[]
    for row in value:
        if not isinstance(row,(tuple,list)) or len(row)!=width:
            raise ValueError('snapshot row dimensions')
        rows.append(tuple(row))
    flat=tuple(chain.from_iterable(rows))
    if set(map(type,flat))-{int} or min(flat,default=0)<0 or max(flat,default=0)>maximum:
        raise ValueError('snapshot unsigned integer')
    return tuple(rows)


def midi_endpoint_catalog():
    """WinMM enumeration only. No Python callback participates in playback.

    mmeapi.h MIDIINCAPSW/MIDIOUTCAPSW use MAXPNAMELEN32 WCHAR. Retain name
    occurrence for identical names; volatile WinMM indices are session-only.
    """
    class InCaps(ctypes.Structure):
        _fields_=[('mid',ctypes.c_ushort),('pid',ctypes.c_ushort),('version',ctypes.c_uint32),
                  ('name',ctypes.c_wchar*32),('support',ctypes.c_uint32)]
    class OutCaps(ctypes.Structure):
        _fields_=[('mid',ctypes.c_ushort),('pid',ctypes.c_ushort),('version',ctypes.c_uint32),
                  ('name',ctypes.c_wchar*32),('technology',ctypes.c_ushort),('voices',ctypes.c_ushort),
                  ('notes',ctypes.c_ushort),('channels',ctypes.c_ushort),('support',ctypes.c_uint32)]
    winmm=ctypes.WinDLL('winmm');result={}
    for direction,prefix,caps_type in (('inputs','midiIn',InCaps),('outputs','midiOut',OutCaps)):
        count=getattr(winmm,prefix+'GetNumDevs');count.argtypes=[];count.restype=ctypes.c_uint
        get=getattr(winmm,prefix+'GetDevCapsW');get.argtypes=[ctypes.c_size_t,ctypes.c_void_p,ctypes.c_uint];get.restype=ctypes.c_uint
        rows=[];seen={}
        for index in range(count()):
            caps=caps_type()
            if get(index,ctypes.byref(caps),ctypes.sizeof(caps)):continue
            ordinal=seen.get(caps.name,0);seen[caps.name]=ordinal+1
            rows.append(dict(id=index,name=caps.name,key=json.dumps([caps.name,ordinal],ensure_ascii=False,separators=(',',':')),
                             own=caps.mid==1 and caps.pid==0x4142))
        result[direction]=rows
    return result


def publish_midi_port_names(settings):
    """Publish only ABMSynth's HKCU endpoint metadata, never other drivers."""
    import winreg
    rows=settings.normalized().ports
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,r'Software\ABMSynth\MIDIPorts',0,winreg.KEY_SET_VALUE|winreg.KEY_WOW64_64KEY) as key:
        for row in rows:winreg.SetValueEx(key,'Name'+str(row['slot']),0,winreg.REG_SZ,row['name'])
        winreg.SetValueEx(key,'InMask',0,winreg.REG_DWORD,sum(1<<r['slot'] for r in rows if r['virtual_in']))
        winreg.SetValueEx(key,'OutMask',0,winreg.REG_DWORD,sum(1<<r['slot'] for r in rows if r['virtual_out']))
        winreg.SetValueEx(key,'Count',0,winreg.REG_DWORD,max(r['slot'] for r in rows)+1)


class NativeMidiPorts:
    """Endpoint handles and callback buffers retire before the MIDI actor."""
    def __init__(self,execution,settings,log):
        self.library=execution.library;self.pointer=None;self.log=log;self.last=(0,0);self.logged_at=0.
        lib=self.library
        for name,args,restype in (
            ('create',[ctypes.c_void_p],ctypes.c_void_p),('input',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint],ctypes.c_int),
            ('output',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint],ctypes.c_int),
            ('virtual',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint],ctypes.c_int),
            ('start',[ctypes.c_void_p],ctypes.c_int),('release',[ctypes.c_void_p],ctypes.c_int),
            ('stats',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64)],ctypes.c_uint)):
            function=getattr(lib,'midi_ports_'+name);function.argtypes=args;function.restype=restype
        self.pointer=lib.midi_ports_create(execution.pointer)
        if not self.pointer:raise RuntimeError(tr('midi_ports.start_failed'))
        self.settings=settings

    def start(self):
        settings=self.settings;lib=self.library;log=self.log
        catalog=midi_endpoint_catalog()
        for row in settings.ports:
            port=row['slot']
            lib.midi_ports_virtual(self.pointer,port,int(row['virtual_out']))
            for direction,function in (('inputs',lib.midi_ports_input),('outputs',lib.midi_ports_output)):
                available={item['key']:item for item in catalog[direction]}
                for key in row[direction]:
                    device=available.get(key)
                    if device is None:
                        try:label=json.loads(key)[0]
                        except (ValueError,TypeError,IndexError):label=key
                        log(log_text('midi_ports.missing',device=label,port=row['name']));continue
                    if device['own']:log(log_text('midi_ports.loop_blocked',device=device['name']));continue
                    if not function(self.pointer,device['id'],port):log(log_text('midi_ports.open_failed',device=device['name'],port=row['name']))
        if not lib.midi_ports_start(self.pointer):raise RuntimeError(tr('midi_ports.start_failed'))

    def poll(self):
        if not self.pointer:return
        values=(ctypes.c_uint64*4)()
        if self.library.midi_ports_stats(self.pointer,values)==4:
            now=tuple(values[2:])
            if now!=self.last and time.monotonic()-self.logged_at>=5:
                self.log(log_text('midi_ports.io_warning',lost=now[0]-self.last[0],errors=now[1]-self.last[1]));self.last=now;self.logged_at=time.monotonic()

    def close(self):
        if self.pointer and not self.library.midi_ports_release(self.pointer):return False
        self.pointer=None;return True


def _native_owned_bytes(buffer,size):
    """Exact owned copy; never expose reused native storage to delayed replay."""
    if type(size) is not int or not 0<=size<=ctypes.sizeof(buffer):
        raise ValueError('native byte-copy bounds')
    return ctypes.string_at(ctypes.addressof(buffer),size)


def _native_items(buffer,count):
    """Iterate bounded ctypes records without allocating an intermediate list."""
    if type(count) is not int or not 0<=count<=len(buffer):
        raise ValueError('native record-copy bounds')
    return (buffer[index] for index in range(count))


def detach_midi_sync_handles(bass,stream,handles):
    """Shared control-side barrier for the three MIDI observation owners.

    bass.h BASS_ChannelRemoveSync: TRUE removes; documented HANDLE(5) means
    this stream/sync pair is already invalid. Every other failure retains the
    handle/callback owner and is retryable. Never clear ownership first.
    No new channel lock is introduced inside MIDIFILTER/SYNCPROC callbacks.
    """
    if not handles:return
    if not stream or bass is None:
        raise RuntimeError(tr('runtime.midi_observer_remove_failed',error='missing stream/backend'))
    failures=[];cause=None
    for handle in tuple(handles):
        try:
            if bass.BASS_ChannelRemoveSync(stream,handle):
                handles.remove(handle)
            else:
                error=int(bass.BASS_ErrorGetCode())
                if error==5:handles.remove(handle)
                else:failures.append((int(handle),error))
        except Exception as error:
            cause=error;failures.append((int(handle),str(error)))
    if failures:
        raise RuntimeError(tr('runtime.midi_observer_remove_failed',error=failures)) from cause


class NativeMidiClock:
    """One runtime's ordered native control capture domain, not audio timing.

    Reference-counted in C++; every state borrower retains the clock. Atomic
    serial/writer snapshots never lock/wait on audio or enter Python callbacks.
    All replay cursors commit only after a coherent aggregate succeeds.
    """
    def __init__(self,native_path=None):
        self.library=None;self.pointer=None;self._draining=False;self.quiet_ready=False
        try:
            path=native_path or os.environ.get('ABMSYNTH_OBSERVATION_DLL',str(Path(__file__).with_name('abmsynth_dsp.dll')))
            library=ctypes.PyDLL(str(path))
            library.midi_clock_version.argtypes=[];library.midi_clock_version.restype=ctypes.c_uint
            if library.midi_clock_version()!=1:return
            create=library.midi_clock_create;create.argtypes=[];create.restype=ctypes.c_void_p
            self.status=library.midi_clock_status
            quiet=getattr(library,'midi_clock_quiet_version',None)
            if quiet is not None:
                quiet.argtypes=[];quiet.restype=ctypes.c_uint
                if quiet()==1:self.status=library.midi_clock_quiet_status;self.quiet_ready=True
            self.status.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64)];self.status.restype=None
            self.release=library.midi_clock_release;self.release.argtypes=[ctypes.c_void_p];self.release.restype=None
            self.pointer=create()
            if self.pointer:self.library=library
        except (OSError,AttributeError,MemoryError):self.close()

    def revision(self):
        if not self.pointer:return (0,False)
        values=(ctypes.c_uint64*2)();self.status(self.pointer,values)
        return int(values[0]),bool(values[1] or values[0]>=(1<<48))

    def drain(self,observers):
        observers=tuple(observer for observer in observers if observer is not None and observer.pointer)
        # A prepared failure ledger is one observer's ordered sidecar, not an
        # independent replay domain. All normal owner aggregates include it.
        failures=tuple(observer.failures for observer in observers if isinstance(getattr(observer,'failures',None),NativeOrderedFailureQueue))
        observers+=tuple(queue for queue in failures if queue not in observers)
        if self._draining:return True
        if not self.pointer or any(observer.clock is not self for observer in observers):
            ready=True
            for observer in observers:ready=observer.drain() and ready
            return ready # Exact old ABI/domain fallback, never sort independent serials.
        self._draining=True
        try:
            for _ in range(32):
                before,busy=self.revision()
                if busy:continue
                pending=[observer.pending_rows() for observer in observers]
                after,changed=self.revision()
                if any(rows is None for rows in pending) or changed or after!=before:continue
                ordered=sorted((version,index,event,data) for index,rows in enumerate(pending)
                    for version,event,data in rows[0])
                try:
                    for _version,index,event,data in ordered:
                        observer=observers[index];observer.delegate(0,observer.stream,data,event)
                except BaseException:
                    for observer in observers:observer.busy=True
                    raise # Entire aggregate retains cursors, including earlier replayed rows.
                for observer,(_rows,cursor) in zip(observers,pending):
                    observer.serial=cursor;observer.busy=False
                return True
            for observer in observers:observer.busy=True
            return False
        finally:self._draining=False

    def close(self):
        if self.pointer:self.release(self.pointer);self.pointer=None
        self.library=None

    def __del__(self):
        try:self.close() # Root reference only; native borrowers keep their own references.
        except Exception:pass


class _NativeSendResult(ctypes.Structure):
    _fields_=[('channel',ctypes.c_uint),('event',ctypes.c_uint),('request',ctypes.c_uint),
        ('raw',ctypes.c_uint),('accepted',ctypes.c_uint),('error',ctypes.c_int)]


class NativeMidiSendDomain:
    """One runtime's native reverb/chorus-floor publication and lifetime."""
    def __init__(self,native_path=None):
        self.library=None;self.pointer=None;self.requested=None;self.batch=None;self.single=None;self.transaction=None;self.batch_library=None
        try:
            library=ctypes.PyDLL(str(native_path or os.environ.get('ABMSYNTH_OBSERVATION_DLL',Path(__file__).with_name('abmsynth_dsp.dll'))))
            library.midi_send_domain_version.argtypes=[];library.midi_send_domain_version.restype=ctypes.c_uint
            if library.midi_send_domain_version()!=1:return
            create=library.midi_send_domain_create;create.argtypes=[];create.restype=ctypes.c_void_p
            self.publish=library.midi_send_domain_floor;self.publish.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint];self.publish.restype=ctypes.c_int
            self.release=library.midi_send_domain_release;self.release.argtypes=[ctypes.c_void_p];self.release.restype=None
            self.pointer=create()
            if self.pointer:
                self.library=library
                requested=getattr(library,'midi_send_domain_request',None)
                if requested is not None:
                    requested.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint];requested.restype=ctypes.c_int
                    self.requested=requested
                # Blocking batch MUST release GIL; atomic publications retain
                # the existing PyDLL ABI. Both objects own the same DLL image.
                batch_library=ctypes.CDLL(library._name)
                batch=getattr(batch_library,'midi_send_domain_apply',None)
                if batch is not None and requested is not None:
                    batch.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,
                        ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(_NativeSendResult),ctypes.c_uint,ctypes.POINTER(ctypes.c_int)]
                    batch.restype=ctypes.c_int;self.batch=batch;self.batch_library=batch_library
                    single=getattr(batch_library,'midi_send_domain_emit',None)
                    if single is not None:
                        single.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,
                            ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(_NativeSendResult),ctypes.POINTER(ctypes.c_int)]
                        single.restype=ctypes.c_int;self.single=single
        except (OSError,AttributeError,MemoryError):self.close()

    def set_floor(self,event,value):
        if self.pointer and not self.publish(self.pointer,event,value):
            raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_send_domain_floor'))

    def set_request(self,channel,event,value):
        if self.pointer and self.requested is not None and not self.requested(self.pointer,channel,event,value):
            raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_send_domain_request'))

    def retry_batch(self):
        if self.transaction is not None and self.transaction.pending is not None:
            result=self.transaction.run(0,())
            if result.phase:raise RuntimeError(tr('runtime.midi_filter_config_failed',error=result.unlock_error))

    def apply_native(self,bassmidi,bass,stream,channels,*,direct=None):
        if not self.pointer or self.batch is None or bass is None or (direct is not None and self.single is None):return None
        functions=(bassmidi.BASS_MIDI_StreamEvent,bass.BASS_ChannelLock,bass.BASS_ErrorGetCode)
        if not all(isinstance(function,ctypes._CFuncPtr) for function in functions):return None
        self.retry_batch();channels=tuple(channels)
        if not channels:return (1,(),(0,0,0))
        selected=(ctypes.c_uint*len(channels))(*channels);rows=(_NativeSendResult*(2*len(channels)))();status=(ctypes.c_int*3)()
        addresses=tuple(ctypes.cast(fn,ctypes.c_void_p) for fn in functions)
        if direct is None:
            result=self.batch(self.pointer,stream,selected,len(selected),*addresses,rows,len(rows),status)
        else:
            rows=(_NativeSendResult*1)()
            result=self.single(self.pointer,stream,*direct,*addresses,rows,status)
        # No metadata callback while an unlock is pending. Keep the owner and
        # replay rows until the SAME thread completes the barrier.
        if result==-2:
            from bass_bindings import NativeChannelTransaction
            if self.transaction is None:self.transaction=NativeChannelTransaction(bass,self.library._name)
            self.transaction.backend=bass;self.transaction.pending=(stream,threading.get_ident(),True)
        copied=tuple((row.channel,row.event,row.request,row.raw,row.accepted,row.error) for row in rows) if result in (1,-2) else ()
        return result,copied,tuple(status)

    def close(self):
        self.retry_batch()
        if self.pointer:self.release(self.pointer);self.pointer=None
        self.library=None

    def __del__(self):
        try:self.close() # Native map generations retain their own references.
        except Exception:pass


class NativeOrderedFailureQueue:
    """Prepared immutable error composites in the existing capture domain.

    Additive storage/replay component, not automatic producer qualification.
    Audio admission must reserve sufficient storage BEFORE emission; refusal
    is never permission to discard an error or repeat already-applied audio.
    This preserves ordered error values, not arbitrary global prefix state.
    """
    WIDTH=10
    STATUS=('produced','consumed','refused','capacity','pending','prepared','capture','writers')
    LEASE_STATUS=('reserved','active_leases','lease_capacity','free_records','references','readers')

    def __init__(self,state,capacity):
        if type(capacity) is not int or not 1<=capacity<=65536:
            raise ValueError(tr('runtime.midi_state_snapshot_failed',error='midi_state_failure_prepare/capacity'))
        self.state=state;self.clock=state.clock;self.stream=state.stream;self.busy=False;self._serial=0
        if self.clock is None or not self.clock.pointer:
            raise ValueError(tr('runtime.midi_state_snapshot_failed',error='midi_state_failure_prepare/clock'))
        self._pending={};self._stages={};self._next_ordinal=0;library=state.library
        version=getattr(library,'midi_state_failure_version',None)
        if version is None:raise ValueError(tr('runtime.midi_state_snapshot_failed',error='midi_state_failure_version'))
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:raise ValueError(tr('runtime.midi_state_snapshot_failed',error='midi_state_failure_version'))
        prepare=library.midi_state_failure_prepare;prepare.argtypes=[ctypes.c_void_p,ctypes.c_uint];prepare.restype=ctypes.c_int
        self.snapshot=library.midi_state_failure_snapshot
        self.snapshot.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint];self.snapshot.restype=ctypes.c_int
        self.ack=library.midi_state_failure_ack;self.ack.argtypes=[ctypes.c_void_p,ctypes.c_uint64];self.ack.restype=ctypes.c_int
        self.push=library.midi_state_failure_push;self.push.argtypes=[ctypes.c_void_p]+[ctypes.c_uint]*6;self.push.restype=ctypes.c_int
        self._status=library.midi_state_failure_status
        self._status.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint];self._status.restype=ctypes.c_int
        self.lease_ready=False;self._reserve=self._release=self._push_reserved=self._lease_status=None
        lease_version=getattr(library,'midi_state_failure_lease_version',None)
        if lease_version is not None:
            lease_version.argtypes=[];lease_version.restype=ctypes.c_uint
            if lease_version()==1:
                self._reserve=library.midi_state_failure_reserve
                self._reserve.argtypes=[ctypes.c_void_p,ctypes.c_uint];self._reserve.restype=ctypes.c_uint64
                self._release=library.midi_state_failure_release
                self._release.argtypes=[ctypes.c_void_p,ctypes.c_uint64];self._release.restype=ctypes.c_int
                self._push_reserved=library.midi_state_failure_push_reserved
                self._push_reserved.argtypes=[ctypes.c_void_p,ctypes.c_uint64]+[ctypes.c_uint]*6;self._push_reserved.restype=ctypes.c_int
                self._lease_status=library.midi_state_failure_lease_status
                self._lease_status.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint];self._lease_status.restype=ctypes.c_int
                self.lease_ready=True
        self.capacity=capacity;self.rows=(ctypes.c_uint64*(capacity*self.WIDTH))()
        if not prepare(state.pointer,capacity):raise ValueError(tr('runtime.midi_state_snapshot_failed',error='midi_state_failure_prepare'))
        self.growth_ready=False;self._growth_prepare=None
        growth_version=getattr(library,'midi_state_failure_growth_version',None)
        if growth_version is not None:
            growth_version.argtypes=[];growth_version.restype=ctypes.c_uint
            if growth_version()==1:
                growth_prepare=library.midi_state_failure_growth_prepare
                growth_prepare.argtypes=[ctypes.c_void_p];growth_prepare.restype=ctypes.c_int
                self._growth_prepare=growth_prepare

    def prepare_growth(self):
        # Explicit normal-product notification admission. Fixed small-budget
        # component probes retain their original pressure/refusal contract.
        if self._growth_prepare is None:return True
        if self.growth_ready:return True
        self.growth_ready=bool(self._growth_prepare(self.pointer))
        return self.growth_ready

    @property
    def pointer(self):return self.state.pointer

    @property
    def serial(self):return self._serial

    @serial.setter
    def serial(self,value):
        for first in sorted(tuple(self._pending)):
            if first>value:continue
            row=self._pending[first];parts=3 if row[6] else 2
            if self._stages.get(first,0)!=parts or not self.ack(self.pointer,first):
                raise RuntimeError(tr('runtime.midi_state_snapshot_failed',error='midi_state_failure_ack'))
            self._pending.pop(first);self._stages.pop(first,None)
            self._next_ordinal+=1
        self._serial=value

    def statistics(self):
        values=(ctypes.c_uint64*8)()
        if not self.pointer or self._status(self.pointer,values,8)!=8:
            raise ValueError(tr('runtime.midi_state_snapshot_failed',error='midi_state_failure_status'))
        return dict(zip(self.STATUS,map(int,values)))

    def lease_statistics(self):
        if not self.pointer or not self.lease_ready:
            raise ValueError(tr('runtime.midi_state_snapshot_failed',error='midi_state_failure_lease_version'))
        values=(ctypes.c_uint64*len(self.LEASE_STATUS))()
        if self._lease_status(self.pointer,values,len(values))!=len(values):
            raise ValueError(tr('runtime.midi_state_snapshot_failed',error='midi_state_failure_lease_status'))
        return dict(zip(self.LEASE_STATUS,map(int,values)))

    def reserve(self,count):
        if not self.pointer or not self.lease_ready or type(count) is not int or not 1<=count<=self.capacity:
            raise ValueError(tr('runtime.midi_state_snapshot_failed',error='midi_state_failure_reserve'))
        return int(self._reserve(self.pointer,count)) # 0 = admission pressure; no audio was attempted.

    def release_reservation(self,token):
        if not self.pointer or not self.lease_ready or type(token) is not int or not 1<=token<=0xffffffffffffffff:
            raise ValueError(tr('runtime.midi_state_snapshot_failed',error='midi_state_failure_release'))
        return bool(self._release(self.pointer,token)) # Explicit release, including exhausted leases.

    def push_reserved(self,token,channel,low_event,high_event,type_event,error,type_value):
        values=(channel,low_event,high_event,type_event,error,type_value)
        if (not self.pointer or not self.lease_ready or type(token) is not int or not 1<=token<=0xffffffffffffffff or
                any(type(value) is not int or not 0<=value<=0xffffffff for value in values)):
            raise ValueError(tr('runtime.midi_state_snapshot_failed',error='midi_state_failure_push_reserved'))
        return int(self._push_reserved(self.pointer,token,*values))

    def pending_rows(self):
        while True:
            capacity=self.statistics()['capacity'] if self.growth_ready else self.capacity
            if capacity>self.capacity:
                self.rows=(ctypes.c_uint64*(capacity*self.WIDTH))();self.capacity=capacity
            count=int(self.snapshot(self.pointer,self.rows,self.capacity))
            if count!=-2 or not self.growth_ready:break
        if count<0:raise RuntimeError(tr('runtime.midi_state_snapshot_failed',error=('midi_state_failure_snapshot',count)))
        if count>self.capacity:raise ValueError(tr('runtime.midi_state_snapshot_failed',error='midi_state_failure_snapshot/count'))
        records=tuple(tuple(map(int,self.rows[index*self.WIDTH:(index+1)*self.WIDTH])) for index in range(count))
        previous=0
        for index,row in enumerate(records):
            low,high,kind,ch,low_event,high_event,type_event,error,type_value,ordinal=row
            if (not low or low<=previous or low<=self._serial or high!=low+1 or kind!=(high+1 if type_event else 0) or
                    ordinal!=self._next_ordinal+index or ch>=128 or error>0xffffffff or
                    type_value>65535 or low_event not in self.state._event_types or high_event not in self.state._event_types or
                    low_event==high_event or (type_event and (type_event not in self.state._event_types or type_event in (low_event,high_event))) or
                    (not type_event and type_value) or (low in self._pending and self._pending[low]!=row)):
                raise ValueError(tr('runtime.midi_state_snapshot_failed',error='midi_state_failure_snapshot/schema'))
            previous=kind or high
        # Validate the complete immutable image before admitting any row. A
        # malformed suffix must not leave an apparently valid replay prefix.
        for row in records:
            low=row[0]
            self._pending[low]=row
        return tuple((row[0],0,row[0]) for row in records),max((row[0] for row in records),default=self._serial)

    def delegate(self,_sync,stream,first,_event):
        if stream!=self.stream or first not in self._pending:
            raise ValueError(tr('runtime.midi_state_snapshot_failed',error='midi_state_failure_snapshot/identity'))
        row=self._pending[first];ch,low_event,high_event,type_event,error,type_value=row[3:9]
        parts=((low_event,error&65535),(high_event,error>>16))+(((type_event,type_value),) if type_event else ())
        for stage in range(self._stages.get(first,0),len(parts)):
            event,raw=parts[stage]
            self.state.delegate(0,stream,(ch<<16)|raw,event)
            self._stages[first]=stage+1 # A throwing sink retains this exact part.


class NativeMidiState:
    """Native MIXTIME state capture, replayed at ordered control barriers.

    Only registered control events use raw16/channel16 (bassmidi.h sync ABI).
    Native cells cover all128 channels and128 individual pressure keys; there
    is no lossy history ring. Repeated writes coalesce OBSERVATION state only,
    never MIDI audio events. Reset and latest field writes replay in version
    order. Python never executes from the registered native SYNCPROC.
    """
    def __init__(self, stream, events, delegate, native_path=None, *, clock=None, keyed_events=(), failure_capacity=None):
        self.stream=stream;self.delegate=delegate
        self.library=None;self.pointer=None;self.serial=0;self.busy=False
        self._draining=False
        self.clock=None;self._keyed_events=frozenset((MIDI_EVENT_KEYPRES,));self.failures=None
        try:
            path=native_path or os.environ.get('ABMSYNTH_OBSERVATION_DLL',str(Path(__file__).with_name('abmsynth_dsp.dll')))
            # Snapshot/status are bounded atomic copies, not blocking I/O or
            # BASS calls. Hold GIL during copy to protect this owner's buffer.
            library=ctypes.PyDLL(str(path))
            create=library.midi_state_create
            create.argtypes=[ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint];create.restype=ctypes.c_void_p
            procedure=library.midi_state_proc;procedure.argtypes=[];procedure.restype=ctypes.c_void_p
            self.context=library.midi_state_context
            self.context.argtypes=[ctypes.c_void_p,ctypes.c_uint];self.context.restype=ctypes.c_void_p
            capacity=library.midi_state_capacity;capacity.argtypes=[ctypes.c_void_p];capacity.restype=ctypes.c_uint
            self.snapshot=library.midi_state_snapshot
            self.snapshot.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint,
                                   ctypes.c_uint64,ctypes.POINTER(ctypes.c_uint64)]
            self.snapshot.restype=ctypes.c_int
            self.status=library.midi_state_status
            self.status.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64)];self.status.restype=None
            self.destroy=library.midi_state_destroy;self.destroy.argtypes=[ctypes.c_void_p];self.destroy.restype=None
            events=tuple(dict.fromkeys(events));self._event_types=events;values=(ctypes.c_uint*len(events))(*events)
            shared=getattr(library,'midi_state_create_clock',None)
            keyed_version=getattr(library,'midi_state_keyed_version',None)
            if keyed_events and keyed_version is not None:
                keyed_version.argtypes=[];keyed_version.restype=ctypes.c_uint
                if keyed_version()!=1:raise ValueError(tr('runtime.midi_filter_config_failed',error='midi_state_keyed_version'))
                if len(set(keyed_events))!=len(keyed_events) or any(event not in events for event in keyed_events):
                    raise ValueError(tr('runtime.midi_filter_config_failed',error='midi_state_create_clock_keyed/events'))
                shared=library.midi_state_create_clock_keyed
                shared.argtypes=[ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint]
                shared.restype=ctypes.c_void_p
                if clock is None or not clock.pointer or clock.library._handle!=library._handle:raise ValueError(tr('runtime.midi_filter_config_failed',error='midi_state_create_clock_keyed/clock'))
                keyed=(ctypes.c_uint*len(keyed_events))(*keyed_events)
                self.pointer=shared(stream,values,len(events),clock.pointer,keyed,len(keyed_events))
                if not self.pointer:raise ValueError(tr('runtime.midi_filter_config_failed',error='midi_state_create_clock_keyed'))
                self.clock=clock;self._keyed_events=frozenset((MIDI_EVENT_KEYPRES,*keyed_events))
                shared=None
            if shared is not None and clock is not None and clock.pointer and clock.library._handle==library._handle:
                shared.argtypes=[ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,ctypes.c_void_p];shared.restype=ctypes.c_void_p
                self.pointer=shared(stream,values,len(events),clock.pointer)
                if self.pointer:self.clock=clock
            elif not self.pointer:self.pointer=create(stream,values,len(events))
            if not self.pointer:return
            self.capacity=int(capacity(self.pointer))
            self.rows=(ctypes.c_uint64*(3*self.capacity))()
            self.meta=(ctypes.c_uint64*2)()
            self.callback=SYNCPROC(procedure())
            self.library=library
            if failure_capacity is not None:
                try:self.failures=NativeOrderedFailureQueue(self,failure_capacity)
                except BaseException:self.close();raise
        except (OSError,AttributeError,MemoryError):
            # Older additive ABI: exact existing callback, never no observation.
            self.close()

    def registration(self, event, fallback):
        if not self.pointer:return fallback,event
        context=self.context(self.pointer,event)
        if not context:raise RuntimeError(tr('runtime.midi_state_unwatched',event=event))
        return self.callback,context

    def revision(self):
        if not self.pointer:return (0,False)
        meta=(ctypes.c_uint64*2)();self.status(self.pointer,meta)
        return int(meta[0]),bool(self.busy or meta[1] or meta[0]>=(1<<48))

    def pending_rows(self):
        """Prepare without replay/cursor mutation, for one ordered aggregate."""
        count=self.snapshot(self.pointer,self.rows,self.capacity,self.serial,self.meta)
        if count==-2:raise RuntimeError(tr('runtime.midi_state_snapshot_failed',error=count))
        if count<0:return None
        return (tuple((int(self.rows[3*i+2]),int(self.rows[3*i]),int(self.rows[3*i+1])) for i in range(count)),int(self.meta[0]))

    def drain(self):
        if not self.pointer or self._draining:return True
        if self.failures is not None:
            if not self.clock.pointer:raise RuntimeError(tr('runtime.midi_state_snapshot_failed',error='midi_state_failure_prepare/clock'))
            return self.clock.drain((self,))
        self._draining=True
        try:
            # No native/audio wait. A control retry sees a coherent snapshot;
            # an unstable UI observation is explicitly unavailable, not cursor loss.
            for _ in range(32):
                prepared=self.pending_rows()
                if prepared is not None:break
            else:
                self.busy=True;return False
            self.busy=False
            rows,cursor=prepared;ordered=sorted(rows)
            try:
                for _version,event,data in ordered:self.delegate(0,self.stream,data,event)
            except BaseException:
                self.busy=True;raise # Retain cursor for retry; no lost unexecuted state.
            self.serial=cursor
            return True
        finally:self._draining=False

    def close(self):
        # Caller MUST first remove all BASS sync borrowers successfully.
        if self.pointer and self.failures is not None and self.failures.statistics()['pending']:
            raise RuntimeError(tr('runtime.midi_observer_remove_failed',error='midi_state_failure_ack/pending'))
        if self.pointer and self.failures is not None and self.failures.lease_ready and self.failures.lease_statistics()['active_leases']:
            raise RuntimeError(tr('runtime.midi_observer_remove_failed',error='midi_state_failure_release/pending'))
        if self.pointer:
            self.destroy(self.pointer);self.pointer=None
        self.library=None;self.serial=0;self.busy=False
        self.clock=None;self.failures=None


class NativeNoteFilter:
    """Stateless native passthrough with the existing Python filter as delegate.

    bassmidi.h MIDIFILTERPROC/BASS_MIDI_EVENT; StreamSetFilter retains neither
    callback nor userdata. The EngineRuntime retains this DLL and its delegate
    for the complete stream lifetime. Missing additive ABI uses the original
    filter, never changes event semantics or silently disables mapping.
    """

    def __init__(self, delegate, native_path=None):
        self.delegate = delegate
        self.library = None
        self.callback = ctypes.cast(delegate, ctypes.c_void_p)
        self.user = None
        try:
            library = ctypes.CDLL(str(native_path or Path(__file__).with_name('abmsynth_dsp.dll')))
            function = library.midi_note_filter_proc
            function.argtypes = []
            function.restype = ctypes.c_void_p
            pointer = function()
            if pointer:
                self.library = library
                self.callback = ctypes.c_void_p(pointer)
                self.user = ctypes.cast(delegate, ctypes.c_void_p)
        except (OSError, AttributeError):
            pass  # Older own-DSP ABI: exact original Python implementation.


class _NativeMidiMapDescriptor(ctypes.Structure):
    _fields_=[('slot',ctypes.c_uint),('channel',ctypes.c_uint),('target_slot',ctypes.c_uint),
              ('target_event',ctypes.c_uint),('table',ctypes.POINTER(ctypes.c_uint))]


class NativeRawDecodeContext:
    """Stream-owned input decoder state; conversion never submits audio.

    Create fresh only before this stream's first actual RAW submission. A
    warm stream has no documented decoder-state query and stays unqualified.
    Every producer must use one stream actor; prepare/validate/actual RAW/
    commit must not be interleaved with another producer's submission.
    """
    def __init__(self,stream,bassmidi,bass,library,*,fresh=False):
        self.library,self.bassmidi,self.bass=library,bassmidi,bass
        self.stream=int(stream);self.lock=threading.RLock();self.plans=set();self.pointer=None
        version=library.raw_decode_context_version;version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:raise RuntimeError('native RAW decode context ABI')
        additive=getattr(library,'raw_decode_context_selection_version',None)
        if additive is None:raise RuntimeError('native RAW decode selection ABI unavailable')
        additive.argtypes=[];additive.restype=ctypes.c_uint
        if additive()!=2:raise RuntimeError('native RAW decode selection ABI')
        signatures=(('context_create',[ctypes.c_uint,ctypes.c_uint],ctypes.c_void_p),
            ('context_close',[ctypes.c_void_p],ctypes.c_int),
            ('context_read',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint],ctypes.c_int),
            ('context_read2',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint],ctypes.c_int),
            ('context_flags_source',[ctypes.c_void_p,ctypes.c_void_p],ctypes.c_int),
            ('context_observe',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_int],ctypes.c_int),
            ('context_event',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.c_int],ctypes.c_int),
            ('context_pending_reset',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.c_int],ctypes.c_int),
            ('context_invalidate',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_int],ctypes.c_int),
            ('plan_prepare',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_int)],ctypes.c_void_p),
            ('plan_read',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint],ctypes.c_int),
            ('plan_read2',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint],ctypes.c_int),
            ('plan_events',[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint],ctypes.c_int),
            ('plan_selection_events',[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint],ctypes.c_int),
            ('plan_validate',[ctypes.c_void_p],ctypes.c_int),
            ('plan_commit',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_int],ctypes.c_int),
            ('plan_release',[ctypes.c_void_p],None))
        for name,args,result in signatures:
            function=getattr(library,'raw_decode_'+name);function.argtypes=args;function.restype=result
        self.pointer=library.raw_decode_context_create(self.stream,int(fresh))
        if not self.pointer:raise RuntimeError('native RAW decode context create')
        if not library.raw_decode_context_flags_source(self.pointer,ctypes.cast(bass.BASS_ChannelFlags,ctypes.c_void_p)):
            library.raw_decode_context_close(self.pointer);self.pointer=None
            raise RuntimeError('native RAW decode stream-policy source')
    def statistics(self):
        with self.lock:
            values=(ctypes.c_uint64*23)()
            if not self.pointer or self.library.raw_decode_context_read2(self.pointer,values,len(values))!=len(values):return {}
            return dict(sequence=int(values[0]),qualified=bool(values[1]),running_status=int(values[2]),port=int(values[3]),
                references=int(values[4]),RPN_selectors=tuple(values[5:21]),selector_uncertainty_mask=int(values[21]),
                full_qualified=bool(values[1]) and not values[21],stream_policy_bound=bool(values[22]))
    def prepare(self,data,mode):
        """Original immutable bytes; None/refusal is never a zero-event claim."""
        raw=bytes(data)
        if len(raw)>0xffffffff:raise ValueError('native RAW length representation')
        with self.lock:
            if not self.pointer:raise RuntimeError('retired native RAW decode context')
            source=(ctypes.c_ubyte*len(raw)).from_buffer_copy(raw);status=(ctypes.c_int*3)()
            pointer=self.library.raw_decode_plan_prepare(self.pointer,int(mode),source,len(raw),
                ctypes.cast(self.bassmidi.BASS_MIDI_ConvertEvents,ctypes.c_void_p),ctypes.cast(self.bass.BASS_ErrorGetCode,ctypes.c_void_p),status)
            if not pointer:raise RuntimeError('native RAW decode preparation: '+str(tuple(status)))
            plan=NativeRawDecodePlan(self,pointer,raw,int(mode));self.plans.add(plan);return plan
    def observe(self,data,mode,*,successful,error=0):
        """After one actual RAW, once: do not also commit its prepared plan."""
        raw=bytes(data)
        with self.lock:
            if not self.pointer:return False
            source=(ctypes.c_ubyte*len(raw)).from_buffer_copy(raw)
            return bool(self.library.raw_decode_context_observe(self.pointer,self.stream,int(mode),source,len(raw),int(bool(successful)),int(error)))
    def close(self):
        with self.lock:
            if not self.pointer:return True
            if self.plans or not self.library.raw_decode_context_close(self.pointer):return False
            self.pointer=None;return True

    def observe_event(self,event,channel,param,*,successful,error=0):
        """Actual non-RAW completion; never reset status/port wholesale."""
        with self.lock:
            return bool(self.pointer and self.library.raw_decode_context_event(self.pointer,self.stream,
                int(event),int(channel),int(param),int(bool(successful)),int(error)))

    def invalidate(self,*,error=0):
        """Uncertain accepted bulk prefix; no guessed count or fake RAW."""
        with self.lock:
            return bool(self.pointer and self.library.raw_decode_context_invalidate(self.pointer,self.stream,int(error)))

    def pending_reset(self,channel,*,successful,error=0):
        """Queued acceptance without an actual processing observer."""
        with self.lock:
            return bool(self.pointer and self.library.raw_decode_context_pending_reset(self.pointer,self.stream,
                int(channel),int(bool(successful)),int(error)))


class NativeRawDecodePlan:
    """Prepared occurrences plus retained per-call native completion facts."""
    def __init__(self,owner,pointer,original,mode):
        self.owner,self.pointer,self.original,self.mode=owner,pointer,original,mode
    def snapshot(self):
        with self.owner.lock:
            values=(ctypes.c_uint64*13)()
            if not self.pointer or self.owner.library.raw_decode_plan_read2(self.pointer,values,len(values))!=len(values):return {}
            keys=('sequence','qualified','event_count','selection_count','next_status','next_port','error','finished','submitted','successful','mode',
                'selection_qualified','selector_uncertainty_mask')
            result=dict(zip(keys,map(int,values)));result['error']=ctypes.c_int(result['error']).value
            for key in ('qualified','finished','submitted','successful','selection_qualified'):result[key]=bool(result[key])
            if not result['qualified']:result['event_count']=None
            if not result['selection_qualified']:result['selection_count']=None
            return result
    def events(self):
        from bass_bindings import BASS_MIDI_EVENT
        with self.owner.lock:
            report=self.snapshot()
            if not report or not report['qualified']:return None
            rows=(BASS_MIDI_EVENT*report['event_count'])()
            if self.owner.library.raw_decode_plan_events(self.pointer,rows,len(rows))!=len(rows):raise RuntimeError('native RAW occurrence read')
            return rows
    def selection_events(self):
        """Exact selection-only identities, never presented as full events."""
        from bass_bindings import BASS_MIDI_EVENT
        with self.owner.lock:
            report=self.snapshot()
            if not report or not report['selection_qualified']:return None
            rows=(BASS_MIDI_EVENT*report['selection_count'])()
            if self.owner.library.raw_decode_plan_selection_events(self.pointer,rows,len(rows))!=len(rows):raise RuntimeError('native RAW selection occurrence read')
            return rows
    def validate(self):
        with self.owner.lock:return bool(self.pointer and self.owner.library.raw_decode_plan_validate(self.pointer))
    def commit(self,*,submitted,successful,error=0):
        with self.owner.lock:
            if not self.pointer:return False
            result=self.owner.library.raw_decode_plan_commit(self.pointer,int(bool(submitted)),int(bool(successful)),int(error))
            if result<0:raise ValueError('native RAW submission completion')
            return result==1
    def close(self):
        with self.owner.lock:
            if self.pointer:self.owner.library.raw_decode_plan_release(self.pointer);self.pointer=None
            self.owner.plans.discard(self);return True


@dataclass(frozen=True)
class _LegacySelectionStage:
    """Fully validated control-side image; no native cursors or audio ownership."""
    adapter: object
    owner: object
    epoch: tuple
    destinations: tuple
    values: tuple


@dataclass(frozen=True)
class _LegacyActivityStage:
    provider: object
    owner: object
    epoch: tuple
    values: tuple


@dataclass(frozen=True)
class _LegacyProvenanceStage:
    adapter: object
    owner: object
    epoch: tuple
    targets: tuple
    origins: tuple
    inputs: tuple
    capture: int
    selection: object


class NativeSelectionAdapter:
    """Prepared selection plan; immutable preparation, metadata-only replay.

    The admitted normal native session owns this plan. Full DWORD
    profile records and repeated accepted origins share the runtime clock.
    Native graph/source/writer borrowers must retire before stream/FontFree.
    """
    class Field(ctypes.Structure):
        _fields_=[('event',ctypes.c_uint),('key',ctypes.c_uint),('neutral',ctypes.c_uint),('flags',ctypes.c_uint),('neutral_amount',ctypes.c_int)]
    class Preset(ctypes.Structure):
        _fields_=[('bank',ctypes.c_uint),('program',ctypes.c_uint),('flags',ctypes.c_uint),('profile_first',ctypes.c_uint),('profile_count',ctypes.c_uint),
            ('extended_first',ctypes.c_uint),('extended_count',ctypes.c_uint),('tone',ctypes.c_uint*6)]
    class Value(ctypes.Structure):
        _fields_=[('field',ctypes.c_uint),('raw',ctypes.c_uint),('amount',ctypes.c_int)]
    class Record(ctypes.Structure):
        _fields_=[('version',ctypes.c_uint64),('channel',ctypes.c_uint),('event',ctypes.c_uint),('raw',ctypes.c_uint),('kind',ctypes.c_uint),('key',ctypes.c_uint),('error',ctypes.c_int)]
    class Restore(ctypes.Structure):
        _fields_=[('old',ctypes.c_uint),('new',ctypes.c_uint),('first',ctypes.c_uint),('count',ctypes.c_uint)]
    class SnapshotRequest(ctypes.Structure):
        _fields_=[('execution',ctypes.c_void_p),('ticket',ctypes.c_uint64),('selection',ctypes.c_void_p),
            ('stream',ctypes.c_uint),('words',ctypes.POINTER(ctypes.c_uint64)),('capacity',ctypes.c_uint),('status',ctypes.c_int)]
    class KeyRevision(ctypes.Structure):
        _fields_=[('bank',ctypes.c_uint),('program',ctypes.c_uint),('revision',ctypes.c_uint64)]

    def __init__(self,owner,library):
        from soundfont_files import effective_preset_expression
        self.owner=owner;self.library=library;self.pointer=None;self.clock=owner.midi_clock;self.stream=owner.stream_handle
        self.raw_context=getattr(owner,'raw_decode_context',None)
        self.raw_failure_plans=[]
        if self.raw_context is not None and self.raw_context.stream==self.stream:
            self.count_submission=self.count_contextual_submission
        self._serial=0;self._rows={};self._pending=();self._checkpoints={};self.delegate=self._replay;self.busy=False
        self.borrowers=[];self.function_owners=[];self.note_sources=[];self.note_cursor=0;self.note_base=0
        self._selection_watch_layout=[]
        for name,args,result in (
            ('create',[ctypes.c_void_p,ctypes.c_uint]+[ctypes.c_void_p]*4+[ctypes.POINTER(self.Field),ctypes.c_uint,ctypes.POINTER(self.Preset),ctypes.c_uint,ctypes.POINTER(self.Value),ctypes.c_uint],ctypes.c_void_p),
            ('seed',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,ctypes.POINTER(ctypes.c_int),ctypes.c_uint,ctypes.POINTER(self.Preset)],ctypes.c_int),
            ('touch_order',[ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint],ctypes.c_int),
            ('restore_order',[ctypes.c_void_p,ctypes.POINTER(self.Restore),ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint],ctypes.c_int),
            ('watch',[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint],ctypes.c_int),
            ('mod_seed',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint],ctypes.c_int),
            ('read',[ctypes.c_void_p,ctypes.POINTER(self.Record),ctypes.c_uint],ctypes.c_int),
            ('ack',[ctypes.c_void_p,ctypes.c_uint64],ctypes.c_int),('release',[ctypes.c_void_p],ctypes.c_int),('references',[ctypes.c_void_p],ctypes.c_uint)):
            function=getattr(library,'midi_selection_state_'+name);function.argtypes=args;function.restype=result
        for name,args,result in (('reserve',[ctypes.c_void_p,ctypes.c_uint],ctypes.c_int),('cancel_reservation',[ctypes.c_void_p,ctypes.c_uint],None),
            ('status',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint],ctypes.c_uint),
            ('ticket',[ctypes.c_void_p,ctypes.c_uint],ctypes.c_uint64),('ticket_settle',[ctypes.c_void_p,ctypes.c_uint64,ctypes.c_uint,ctypes.c_uint],ctypes.c_int),
            ('ticket_status',[ctypes.c_void_p,ctypes.c_uint64,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint],ctypes.c_int),
            ('failed_tickets',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint],ctypes.c_int),
            ('settle_failed',[ctypes.c_void_p,ctypes.c_uint64]+[ctypes.c_void_p]*3+[ctypes.POINTER(ctypes.c_int)],ctypes.c_int)):
            function=getattr(library,'midi_selection_state_'+name);function.argtypes=args;function.restype=result
        routing=owner.soundfont_presets;performance=owner.performance;policy=owner.channel_policy
        group=routing.performance
        base_profile=dict(group or {})
        profiles=[base_profile]
        for item in routing.presets.values():
            exception={} if item.get('disabled') else item.get('performance') or {}
            profiles.append({**base_profile,**exception})
        # Preparation must cover the old applied image as well as the new
        # source profiles. apply_soundfont_profile restores sorted(previous
        # union active) to parameter.default; omitting a removed key here
        # would lose its seed and leave the previous scale/drum value audible.
        # This allocation is control-side only, before native publication.
        profile_names=sorted({key for profile in (*profiles,*performance._profile_values.values()) for key in profile})
        self.names=tuple((*profile_names,*PRESET_EXPRESSION_RANGES,*('mix_level','attack','decay','release','cutoff','resonance')))
        self.indices={field:index for index,field in enumerate(self.names)};fields=[]
        for index,name in enumerate(self.names):
            if name in profile_names:
                kind,key=performance_profile_key(name);parameter=SCALE_PARAMETER if kind=='scale' else DRUM_PARAMETERS[kind]
                fields.append(self.Field(parameter.event,key,parameter.parameter(key,parameter.default),1|(8 if kind!='scale' else 0),parameter.default))
            else:
                raw=performance.expression_parameter(name,performance.expression_default(name));flags=2 if name in PRESET_EXPRESSION_RANGES else 0
                if name.startswith('modulation_'):flags|=4
                fields.append(self.Field(EXPRESSION_EVENTS[name],index,raw,flags,performance.expression_default(name)))
        values=[];presets=[];self.extended_sets=[]
        def prepared(bank,program,item,profile):
            first=len(values)
            for name in sorted(profile):
                kind,key=performance_profile_key(name);parameter=SCALE_PARAMETER if kind=='scale' else DRUM_PARAMETERS[kind]
                values.append(self.Value(self.indices[name],parameter.parameter(key,profile[name]),int(profile[name])))
            count=len(values)-first;extended=effective_preset_expression(item);extended_first=len(values)
            for name,amount in extended.items():
                low,high,_=PRESET_EXPRESSION_RANGES[name];amount=max(low,min(high,int(amount)))
                values.append(self.Value(self.indices[name],performance.expression_parameter(name,amount),amount))
            self.extended_sets.append(tuple(extended))
            disabled=bool(item.get('disabled'));flags=int(disabled)
            tone=[]
            for index,name in enumerate(('volume','attack','decay','release','cutoff','resonance')):
                supplied=None if disabled else item.get(name)
                if supplied is not None:flags|=4<<index
                tone.append((100 if index==0 else 64) if supplied is None else max(0,min(400 if index==0 else 127,int(supplied))))
            return self.Preset(bank,program,flags,first,count,extended_first,len(extended),(ctypes.c_uint*6)(*tone))
        fallback=prepared(0,0,{},base_profile)
        for ((bank,program),item),profile in zip(routing.presets.items(),profiles[1:]):presets.append(prepared(bank,program,item,profile))
        callbacks=(performance.bassmidi.BASS_MIDI_StreamEvent,performance.bassmidi.BASS_MIDI_StreamGetEvent,
            performance.bassmidi.BASS_MIDI_StreamGetPreset,performance.bass.BASS_ErrorGetCode)
        if not all(isinstance(function,ctypes._CFuncPtr) for function in callbacks):raise ValueError('native selection callback ABI')
        self.function_owners.extend(callbacks)
        field_rows=(self.Field*len(fields))(*fields);preset_rows=(self.Preset*len(presets))(*presets);value_rows=(self.Value*len(values))(*values)
        self.pointer=library.midi_selection_state_create(self.clock.pointer,self.stream,*(ctypes.cast(fn,ctypes.c_void_p) for fn in callbacks),
            field_rows,len(fields),preset_rows,len(presets),value_rows,len(values))
        if not self.pointer:raise ValueError('native selection preparation domain')
        self.capacity_worker=False
        try:
            version=getattr(library,'midi_selection_state_capacity_worker_version',None)
            prepare=getattr(library,'midi_selection_state_capacity_worker_prepare',None)
            if version is not None and prepare is not None:
                version.argtypes=[];version.restype=ctypes.c_uint
                prepare.argtypes=[ctypes.c_void_p];prepare.restype=ctypes.c_int
                if version()!=1 or prepare(self.pointer)!=1:raise ValueError('native selection capacity staging admission')
                self.capacity_worker=True
            import numpy as np
            addresses=[]
            # Dense ABI image, sparse Python owners: initialise the neutral
            # 128-channel image once in native array operations, then fill
            # only existing dictionary entries. Same signed32 cells/order.
            cells=np.zeros((128,len(self.names),7),dtype=np.int32)
            cells[:,:,6]=-2147483648
            for values,present_slot,value_slot in ((performance.preset_seeds.native,0,1),
                    (performance.preset_seeds.applied,2,3),(policy.timbre,4,5)):
                for (ch,name),value in values.items():
                    index=self.indices.get(name)
                    if 0<=ch<128 and index is not None:
                        cells[ch,index,present_slot]=1;cells[ch,index,value_slot]=ctypes.c_int(value).value
            for ch,profile in performance._profile_values.items():
                if not 0<=ch<128:continue
                for name,value in profile.items():
                    index=self.indices.get(name)
                    if index is not None:cells[ch,index,6]=ctypes.c_int(value).value
            for ch in range(128):
                token=routing._performance_tokens.get(ch);key=routing.preset_key(ch)
                valid=bool(token and token[0]==routing._performance_revision and token[2]==routing._performance_key_revisions.get(key,0))
                addresses.extend((routing.bank_msb[ch],routing.bank_lsb[ch],routing.program[ch],int(owner.addresses.drums[ch]),int(valid)|(int(token is not None)<<1),
                    (token[1][0] if token and token[1] is not None else -1)&0xffffffff,(token[1][1] if token and token[1] is not None else -1)&0xffffffff,
                    token[3] if token else 0,int(token[4]) if token else int(owner.addresses.drums[ch])))
            assert library.midi_selection_state_seed(self.pointer,(ctypes.c_uint*len(addresses))(*addresses),len(addresses),
                cells.ctypes.data_as(ctypes.POINTER(ctypes.c_int)),cells.size,ctypes.byref(fallback))
            self._seed_metadata(routing,performance)
            for ch in range(128):
                order=[self.indices[field] for (channel,field) in policy.timbre if channel==ch and field in self.indices]
                assert library.midi_selection_state_touch_order(self.pointer,ch,(ctypes.c_uint*len(order))(*order),len(order))
            extended_names=tuple(PRESET_EXPRESSION_RANGES);masks={0:()}
            for keys in (*self.extended_sets,*(tuple(field for (ch,field) in performance.preset_seeds.applied if ch==channel) for channel in range(128))):
                masks[sum(1<<extended_names.index(key) for key in keys)]=keys
            restore=[];order=[]
            for old,old_keys in masks.items():
                for new,new_keys in masks.items():
                    first=len(order);order.extend(self.indices[key] for key in dict.fromkeys(old_keys).keys()-dict.fromkeys(new_keys).keys())
                    restore.append(self.Restore(old,new,first,len(order)-first))
            assert library.midi_selection_state_restore_order(self.pointer,(self.Restore*len(restore))(*restore),len(restore),(ctypes.c_uint*len(order))(*order),len(order))
            graph=owner.stream_effects.delay;enabled,active,mixes,handles,taps,wet,callback=graph.selection_snapshot(library)
            attach=library.midi_selection_state_graph
            attach.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint]+[ctypes.c_void_p]*5+[ctypes.c_uint]*2+[ctypes.c_void_p]*5;attach.restype=ctypes.c_int
            callbacks=(graph.bassmidi.BASS_MIDI_StreamGetChannel,graph.bass.BASS_ChannelSetDSP,graph.bass.BASS_ChannelRemoveDSP,graph.bass.BASS_StreamFree)
            self.function_owners.extend(callbacks)
            assert attach(self.pointer,int(enabled),owner.addresses.count,active,mixes,handles,taps,wet,graph.userfx_mix,graph.userfx_handle,
                *(ctypes.cast(fn,ctypes.c_void_p) for fn in callbacks),ctypes.cast(callback,ctypes.c_void_p) if callback is not None else None)
            graph._selection_borrowers.add(self);self.borrowers.append(graph)
            mapper=getattr(owner.channel_native_filter,'function_map',None);kernel=getattr(owner,'cc_native_kernel',None)
            def watch(observer,name,event,kind):
                if observer is None or not observer.pointer:return
                assert library.midi_selection_state_watch(self.pointer,observer.pointer,self.indices[name],event,kind)
                self._selection_watch_layout.append((self.indices[name],observer._event_types.index(event),kind))
                self.borrowers.append(observer)
            for name in PRESET_EXPRESSION_RANGES:
                event=EXPRESSION_EVENTS[name]
                watch(performance.preset_seeds.native_observer,name,event,0)
                if name.startswith('modulation_'):
                    for ch in range(128):
                        raw=performance.mod_observed.get((ch,name));assert library.midi_selection_state_mod_seed(self.pointer,ch,self.indices[name],int(raw is not None),raw or 0)
                    watch(performance.mod_native_observer,name,event,2)
                    if mapper is not None:watch(mapper.journal,name,event,2)
                    if kernel is not None:watch(kernel.manual_journal,name,event,2)
            for name in CHANNEL_TIMBRE_FIELDS:
                if name not in self.indices:continue
                if mapper is not None and name in mapper.slots:
                    watch(mapper.journal,name,0x30000+mapper.slots[name],1)
                    if name in PRESET_EXPRESSION_RANGES:watch(mapper.journal,name,0x30000+mapper.slots[name],0)
                if kernel is not None and name in kernel.semantic_fields:watch(kernel.semantic_journal,name,0xe0000+kernel.semantic_fields.index(name),1)
            self._selection_watch_layout=tuple(self._selection_watch_layout)
            self._prepare_provenance()
        except BaseException:
            self.close();raise

    @staticmethod
    def provenance_event_fields():
        """Accepted event identities, including the custom delay's journal."""
        events={parameter.event:name for name,parameter in CHANNEL_PARAMETERS.items() if parameter.event is not None}
        # bassmidi.h / BASS_MIDI_StreamEvent: USERFX43 is the provenance
        # identity of delay_send even when retained custom taps, rather than
        # a native UserFX emission, supply its audio. Routing is unchanged.
        events[MIDI_EVENT_USERFX]='delay_send'
        return events

    @staticmethod
    def provenance_donor_contract(owner,kernel=None):
        """One control-side symbolic contract for preparation/publication.

        Rows are pointer,event,role,field,source,phase. Baseline/MOD roots are
        retained selection watchers even when they have no provenance row.
        No observer is drained, audio sent or current seed state modified.
        """
        mapper=getattr(owner.channel_native_filter,'function_map',None)
        if kernel is None:kernel=getattr(owner,'cc_native_kernel',None)
        event_fields=NativeSelectionAdapter.provenance_event_fields()
        rows=[];seen=set();roots=[]
        def bind(observer,event,role,name,source,phase):
            if observer is None or not observer.pointer or event not in observer._event_types:return
            identity=(observer.pointer,event,role,name)
            if identity in seen:return
            seen.add(identity);rows.append((observer.pointer,event,role,name,source,phase))
            if observer not in roots:roots.append(observer)
        observer=owner.origin_native_observer
        if observer is not None:
            for event in observer._event_types:
                if event==MIDI_EVENT_SYSTEM:bind(observer,event,3,'volume','native_midi','processed')
                elif event==MIDI_EVENT_RESET:bind(observer,event,4,'volume','midi_reset','processed')
                elif event in event_fields:bind(observer,event,1,event_fields[event],'native_midi','processed')
        if mapper is not None:
            for name,slot in mapper.slots.items():bind(mapper.journal,0x20000+slot,2,name,'native_midi','accepted')
            for event,name in event_fields.items():
                bind(mapper.journal,event,1,name,'native_midi','accepted')
                bind(mapper.journal,0x80000+event,1,name,'preset_default','accepted')
        if kernel is not None:
            bind(kernel.journal,0x1d0000,5,'volume','channel_reset','processed')
            source=getattr(kernel,'semantic_source','native_midi')
            for slot,name in enumerate(kernel.semantic_fields):
                bind(kernel.semantic_journal,0xc0000+slot,2,name,source,'accepted')
                bind(kernel.semantic_journal,0xf0000+slot,1,name,source,'accepted')
                bind(getattr(kernel,'reset_journal',None),0x1b0000+slot,1,name,'reset_seed','accepted')
            for event,name in event_fields.items():
                bind(kernel.manual_journal,event,1,name,'manual','accepted')
                bind(kernel.journal,event,1,name,'native_midi','accepted')
            for event,source_name in ((0x150000,source),(0x190000,'reset_seed')):
                bind(kernel.semantic_journal,event,1,'mode',source_name,'accepted')
        for observer in (owner.performance.preset_seeds.native_observer,owner.performance.mod_native_observer):
            if observer is not None and observer.pointer and observer not in roots:roots.append(observer)
        return tuple(rows),tuple(roots)

    def _prepare_provenance(self):
        """Control-side ABI1 seeds/descriptors; existing journals remain owned.

        Original provenance sequence is opaque seed data. New capture ordinals
        are a separate domain until an explicitly qualified ownership cutover.
        Signed Python raw is encoded as int64 bits, never silently DWORD-wrapped.
        """
        function=getattr(self.library,'midi_selection_state_provenance_seed',None)
        if function is None:return
        owner=self.owner
        fields=list(CHANNEL_PARAMETERS)
        sources=['manual','native_midi','preset_default','soundfont_group','channel_override','preset_restore','midi_reset','reset_seed','channel_reset']
        phases=['accepted','processed'];origins=[];inputs=[]
        def intern(values,name):
            if type(name) is not str or not name:raise ValueError('provenance string descriptor')
            if name not in values:values.append(name)
            return values.index(name)
        def integer(value,low=0,high=0xffffffffffffffff):
            if type(value) is not int or not low<=value<=high:raise ValueError('provenance seed integer')
            return value
        activation,busy=self.clock.revision()
        if busy:raise ValueError('provenance preparation clock busy')
        for key,value in owner.control_origins.items():
            if not isinstance(key,tuple) or len(key) not in (2,3):raise ValueError('provenance origin key')
            ch,name=key[:2];integer(ch,0,127);field=intern(fields,name)
            pressure=name=='key_pressure'
            if (pressure and len(key)!=3) or (not pressure and len(key)!=2):raise ValueError('provenance origin keyed domain')
            note=integer(key[2],0,127) if pressure else 0xffffffff
            if value.generation!=owner.stream_generation:raise ValueError('provenance stale seed generation')
            origins.append((1,ch,field,note,integer(value.event,0,0xffffffff),integer(value.raw,-2**63,2**63-1)&0xffffffffffffffff,
                intern(sources,value.source),intern(phases,value.phase),1,integer(value.sequence),activation))
        for (ch,name),value in owner.channel_policy.inputs.items():
            if name not in CHANNEL_PARAMETERS:raise ValueError('provenance input field')
            inputs.append((2,integer(ch,0,127),fields.index(name),0xffffffff,0,
                integer(value,-2**31,2**31-1)&0xffffffffffffffff,0,0,1,0,activation))
        event_fields=self.provenance_event_fields()
        descriptors,roots=self.provenance_donor_contract(owner)
        bindings=[(pointer,event,role,intern(fields,name),intern(sources,source),intern(phases,phase),activation)
                  for pointer,event,role,name,source,phase in descriptors]
        accepted=tuple(sorted((event,fields.index(name)) for event,name in event_fields.items()))
        seeds=tuple((*origins,*inputs))
        prepared=(ctypes.c_uint64*(7*len(bindings)))(*(word for row in bindings for word in row))
        seed_words=(ctypes.c_uint64*(11*len(seeds)))(*(word for row in seeds for word in row))
        events=(ctypes.c_uint*(2*len(accepted)))(*(word for row in accepted for word in row))
        function.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint64];function.restype=ctypes.c_int
        if not function(self.pointer,prepared,len(bindings),seed_words,len(seeds),events,len(accepted),len(fields),len(sources),len(phases),activation):
            raise ValueError('provenance preparation refused')
        self.provenance_fields=tuple(fields);self.provenance_sources=tuple(sources);self.provenance_phases=tuple(phases)
        self.provenance_bindings=tuple(bindings);self.provenance_seeds=seeds;self.provenance_events=accepted;self.provenance_activation=activation
        self.borrowers.extend(root for root in roots if root not in self.borrowers)

    def _seed_metadata(self,routing,performance):
        """Own ABI1 immutable generation/previous-token/controller preparation."""
        function=getattr(self.library,'midi_selection_state_metadata_seed',None)
        controls=tuple(name for name,_ in routing.CONTROL_EVENTS)
        if function is None:
            if any(value!=64 for name in controls for value in routing.controllers[name]):
                raise ValueError('old selection image cannot preserve nonneutral routing controllers')
            return
        def revision(value):
            if type(value) is not int or not 0<=value<=0xffffffffffffffff:raise ValueError('selection revision representation')
            return value
        def word(value):
            if type(value) is not int or not -0x80000000<=value<=0x7fffffff:raise ValueError('selection signed controller representation')
            return value&0xffffffff
        rows=[]
        for channel in range(128):
            token=routing._performance_tokens.get(channel)
            rows.extend((int(channel in performance._profile_values),revision(token[0]) if token is not None else 0,
                revision(token[2]) if token is not None else 0,*(word(routing.controllers[name][channel]) for name in controls)))
        keys=[]
        for key,value in routing._performance_key_revisions.items():
            if (not isinstance(key,tuple) or len(key)!=2 or any(type(item) is not int or not -0x80000000<=item<=0xffffffff for item in key)):
                raise ValueError('selection revision key representation')
            keys.append((key[0]&0xffffffff,key[1]&0xffffffff,revision(value)))
        keys.sort();prepared=(self.KeyRevision*len(keys))(*(self.KeyRevision(*row) for row in keys))
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint64,ctypes.c_uint64,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint,
            ctypes.POINTER(self.KeyRevision),ctypes.c_uint];function.restype=ctypes.c_int
        if not function(self.pointer,revision(self.owner.stream_generation),revision(routing._performance_revision),
                (ctypes.c_uint64*len(rows))(*rows),len(rows),prepared,len(prepared)):
            raise ValueError('selection generation metadata seed refused')

    @property
    def serial(self):return self._serial
    @serial.setter
    def serial(self,value):
        for row in self._pending:
            if row[0]<=value:assert self.library.midi_selection_state_ack(self.pointer,row[0]);self._rows.pop(row[0],None);self._checkpoints.pop(row[0],None)
        self._pending=tuple(row for row in self._pending if row[0]>value);self._serial=value
    def pending_rows(self):
        read_count=getattr(self.library,'midi_selection_state_read_count',None)
        capacity=65536
        if read_count is not None:
            read_count.argtypes=[ctypes.c_void_p];read_count.restype=ctypes.c_uint
            # The additive count API reports pending records, not the ring's
            # admission limit. Empty revision polls need one valid output cell,
            # not a newly zeroed 65,536-record array on every MIDI frame.
            # read() still checks the coherent count under recordsLock: a
            # growing producer returns -2 and is retried below, never truncated.
            capacity=max(1,int(read_count(self.pointer)))
        rows=(self.Record*capacity)();count=self.library.midi_selection_state_read(self.pointer,rows,len(rows))
        if count==-2 and read_count is not None:
            rows=(self.Record*max(capacity,int(read_count(self.pointer))))()
            count=self.library.midi_selection_state_read(self.pointer,rows,len(rows))
        if count==-2 and read_count is not None:
            self.busy=True;return None # Producer progressed; retry the coherent aggregate, never ACK a truncated prefix.
        if count<0:raise RuntimeError('native selection metadata snapshot')
        result=[]
        for record in rows[:count]:
            self._rows[record.version]=(record.channel,record.event,record.raw,record.kind,record.key,record.error)
            result.append((record.version,0,record.version))
        self._pending=tuple(result);self.busy=False;return self._pending,(result[-1][0] if result else self._serial)
    def version(self):
        pending=self.pending_rows();return self._serial if pending is None else pending[1]
    def reserve_events(self,count):
        if not isinstance(count,int) or count<0:raise ValueError('native selection event reservation')
        return bool(self.pointer and self.library.midi_selection_state_reserve(self.pointer,count))
    def cancel_reservation(self,count):
        if self.pointer:self.library.midi_selection_state_cancel_reservation(self.pointer,count)
    def reserve_ticket(self,count):
        if not isinstance(count,int) or count<0:raise ValueError('native selection ticket count')
        return int(self.library.midi_selection_state_ticket(self.pointer,count)) if self.pointer and count else 0
    def identify_ticket(self,ticket,rows,position=0):
        """Original decoded identities, prepared before StreamEvents submission.

        position 0 is data-only, 1 absolute byte, 2 absolute tick, 3 per-event. Relative
        timing is not relabelled as absolute. MIDIFILTERPROC positions stay
        unchanged; indistinguishable tickets settle only at an empty queue.
        """
        from bass_bindings import BASS_MIDI_EVENT
        function=self.library.midi_selection_state_ticket_events
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint64,ctypes.POINTER(BASS_MIDI_EVENT),ctypes.c_uint,ctypes.c_uint];function.restype=ctypes.c_int
        return int(function(self.pointer,ticket,rows,len(rows),position))==1
    def ticket_phase(self,ticket,phase):
        result=int(self.library.midi_selection_state_ticket_settle(self.pointer,ticket,phase,0))
        if result<0:raise RuntimeError('native selection ticket phase or unsafe rollback: '+str(result))
        return result
    def ticket_status(self,ticket):
        values=(ctypes.c_uint*4)()
        if not self.pointer or not self.library.midi_selection_state_ticket_status(self.pointer,ticket,values,len(values)):return None
        return dict(zip(('requested','processed','remaining','phase'),values))
    def settle_failed_tickets(self):
        """Control barrier only; no pending async event may lose its budget.

        Native lock/query/unlock stays on one thread. Unlock refusal keeps the
        selection owner alive; retry never resends original audio.
        """
        kernel=getattr(self,'native_settlement_kernel',None)
        if kernel is not None and kernel.pointer:
            phase=kernel.library.midi_cc_kernel_selection_settlement_phase
            phase.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint];phase.restype=ctypes.c_uint
            values=(ctypes.c_uint*5)()
            if phase(kernel.pointer,values,len(values))!=5:raise RuntimeError('native selection settlement owner snapshot')
            if values[0] and not values[1]:
                # The active actor, not this Python metadata consumer, owns
                # queue-drain settlement. Never steal its retained unlock.
                tickets=(ctypes.c_uint64*4096)();count=self.library.midi_selection_state_failed_tickets(self.pointer,tickets,len(tickets))
                if count<0:raise RuntimeError('native selection failed ticket snapshot')
                return count==0 and not values[3]
            if values[3] and values[4]!=int(_clock.GetCurrentThreadId()):return False
        # Ticket bookkeeping can finish before the unlock fails. Even an
        # empty failed-ticket list is not proof that the native owner released
        # its channel lock; retry that original thread-owned unlock first.
        if not self.retry_owned_unlock():return False
        tickets=(ctypes.c_uint64*4096)();count=self.library.midi_selection_state_failed_tickets(self.pointer,tickets,len(tickets))
        if count<0:raise RuntimeError('native selection failed ticket snapshot')
        backend=self.owner.performance.bass
        for ticket in tickets[:count]:
            status=(ctypes.c_int*3)();result=self.library.midi_selection_state_settle_failed(self.pointer,ticket,
                ctypes.cast(backend.BASS_ChannelLock,ctypes.c_void_p),ctypes.cast(backend.BASS_ChannelGetAttribute,ctypes.c_void_p),ctypes.cast(backend.BASS_ErrorGetCode,ctypes.c_void_p),status)
            if result in (-2,-1):raise RuntimeError('native selection settle owner/unlock: '+str(tuple(status)))
            if result!=1:return False
        return True
    def statistics(self):
        values=(ctypes.c_uint64*6)()
        if not self.pointer or self.library.midi_selection_state_status(self.pointer,values,len(values))!=len(values):return {}
        return dict(zip(('produced','consumed','reserved','queued','unreserved_refused','failures'),values))
    def admission_capacity(self):
        function=getattr(self.library,'midi_selection_state_ticket_capacity',None)
        if function is None:return None
        function.argtypes=[ctypes.c_void_p];function.restype=ctypes.c_uint
        return int(function(self.pointer)) if self.pointer else 0
    def prepare_submission_capacity(self,count):
        """Input-side paged storage for ONE original whole ASYNC boundary.

        Native decoder callbacks use only prepared pages; allocation failure
        retains original bytes rather than splitting them or forcing SYNC.
        Missing additive ABI remains the original bounded candidate path.
        """
        if type(count) is not int or not 0<=count<=0xffffffff:raise ValueError('native selection prepared capacity')
        function=getattr(self.library,'midi_selection_state_prepare_capacity',None)
        if function is None:return False
        function.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(ctypes.c_int)];function.restype=ctypes.c_int
        status=(ctypes.c_int*2)()
        result=int(function(self.pointer,ctypes.cast(self.owner.performance.bass.BASS_ChannelLock,ctypes.c_void_p),count,status))
        if result<0:raise RuntimeError(tr('runtime.midi_filter_config_failed',error=('storage owner/unlock',result,tuple(status))))
        return result==1
    def preparation_pending(self):
        """Displaced buffers survive failed native unlock on their owner."""
        function=getattr(self.library,'midi_selection_state_preparation_pending',None)
        if function is None:return None
        function.argtypes=[ctypes.c_void_p];function.restype=ctypes.c_int
        return bool(function(self.pointer)) if self.pointer else False
    def metadata_record_bound(self):
        function=getattr(self.library,'midi_selection_state_record_bound',None)
        if function is None:return None
        function.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint];function.restype=ctypes.c_int
        values=(ctypes.c_uint*6)()
        if not function(self.pointer,values,len(values)):return None
        return dict(zip(('fields','profile','extended','touched_bound','graph_bound','record_bound'),map(int,values)))
    def reachable_record_bound(self):
        """Immutable source/seed/watch reachability, not current-preset count."""
        function=getattr(self.library,'midi_selection_state_reachable_bound',None)
        if function is None:return None
        function.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint];function.restype=ctypes.c_int
        values=(ctypes.c_uint*5)()
        if not function(self.pointer,values,len(values)):return None
        return dict(zip(('reachable_profile','reachable_extended','reachable_touched','graph_bound','record_bound'),map(int,values)))
    def submit_owned(self,data,mode,count):
        """One unchanged native submission; refusal has submitted=False.

        The caller retains bytes and context until completion. Errors after
        submitted=True may own an audio prefix and must never resend it.
        bass.h ChannelLock and bassmidi.h StreamEvents remain one native call.
        """
        context=self.raw_context
        if context is not None and mode&BASS_MIDI_EVENTS_RAW:
            with context.lock:
                return self._submit_owned_contextual(data,mode,count,context)
        return self._submit_owned_direct(data,mode,count)
    def count_contextual_submission(self,bassmidi,data,mode=0x10000):
        if not mode&BASS_MIDI_EVENTS_RAW:return type(self).count_submission(bassmidi,data,mode)
        context=self.raw_context
        if context is None:return type(self).count_submission(bassmidi,data,mode)
        with context.lock:
            plan=context.prepare(data,mode)
            try:return plan.snapshot()['selection_count']
            finally:plan.close()
    def _submit_owned_contextual(self,data,mode,count,context):
        from bass_bindings import BASS_MIDI_EVENT
        plan=context.prepare(data,mode)
        try:
            report=plan.snapshot();decoded=plan.selection_events()
            actual=report['selection_count']
            if actual is not None and count is not None and count!=actual:
                return dict(status=-6,ticket=0,accepted_events=0,error=0,submitted=False,lock_error=0,unlock_error=0,context_stale_count=True)
            identities=None if decoded is None else decoded
            if not plan.validate():return dict(status=-6,ticket=0,accepted_events=0,error=0,submitted=False,lock_error=0,unlock_error=0,context_stale_plan=True)
            result=self._submit_owned_direct(data,mode,actual,prepared_identities=identities)
            if result is not None:result['raw_decode_owned']=True
            if result is not None and result['submitted']:
                successful=result['accepted_events']!=0xffffffff
                if not plan.commit(submitted=True,successful=successful,error=result['error']):raise RuntimeError('native RAW context submission order changed')
                if not successful:self.raw_failure_plans.append(plan);plan=None
            return result
        finally:
            if plan is not None:plan.close()
    def _submit_owned_direct(self,data,mode,count,prepared_identities=None):
        function=getattr(self.library,'midi_selection_state_submit_owned',None)
        if function is None:return None
        # UINT_MAX is the additive ABI's explicit unknown-framing sentinel.
        # Immediate decoded callbacks reserve themselves; no count is guessed.
        if count is None:
            version=getattr(self.library,'midi_selection_state_owned_version',None)
            if version is None:return None
            version.argtypes=[];version.restype=ctypes.c_uint
            if version()!=2:return None
        count=0xffffffff if count is None else count
        function.argtypes=[ctypes.c_void_p]*3+[ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint64)]
        function.restype=ctypes.c_int
        from bass_bindings import BASS_MIDI_EVENT
        if mode&BASS_MIDI_EVENTS_RAW:
            raw=bytearray(data);source=(ctypes.c_ubyte*len(raw)).from_buffer(raw);length=len(raw)
        else:
            if not isinstance(data,ctypes.Array) or getattr(data,'_type_',None) is not BASS_MIDI_EVENT:raise ValueError('native STRUCT ownership ABI')
            source=data;length=len(data)
        values=(ctypes.c_uint64*6)()
        backend=self.owner.performance.bass;midi=self.owner.performance.bassmidi
        identities=prepared_identities;position=0
        identified=getattr(self.library,'midi_selection_state_submit_identified',None)
        if identities is None and identified is not None and count!=0xffffffff:
            if mode&BASS_MIDI_EVENTS_RAW:
                converter=midi.BASS_MIDI_ConvertEvents
                converter.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(BASS_MIDI_EVENT),ctypes.c_uint,ctypes.c_uint];converter.restype=ctypes.c_uint
                decode_flags=(mode&0x2000000)|(0x8000000 if mode&0x18000000 else 0)
                size=int(converter(source,length,None,0,decode_flags))
                if size!=0xffffffff and size<=1048576:
                    decoded=(BASS_MIDI_EVENT*size)()
                    if converter(source,length,decoded,size,decode_flags)==size:
                        selected=[row for row in decoded if row.event in (MIDI_EVENT_PROGRAM,MIDI_EVENT_BANK,MIDI_EVENT_BANK_LSB,MIDI_EVENT_DRUMS)]
                        if mode&0xffff:
                            for row in selected:row.chan=(mode&0xffff)-1
                        identities=(BASS_MIDI_EVENT*len(selected))(*selected)
            else:
                selected=[row for row in source if row.event in (MIDI_EVENT_PROGRAM,MIDI_EVENT_BANK,MIDI_EVENT_BANK_LSB,MIDI_EVENT_DRUMS)]
                identities=(BASS_MIDI_EVENT*len(selected))(*selected)
                if mode&0x10000000:position=3
                # Relative tick/pos depend on the stream's current position.
                # Data-only original identity can qualify unique tickets;
                # duplicate tickets still retain ambiguous pooled ownership.
            if identities is not None and len(identities)!=count:raise ValueError('native selected identity/count mismatch')
        arguments=(self.pointer,ctypes.cast(backend.BASS_ChannelLock,ctypes.c_void_p),
            ctypes.cast(midi.BASS_MIDI_StreamEvents,ctypes.c_void_p),mode,source,length,count,values)
        if identities is not None:
            identified.argtypes=function.argtypes+[ctypes.POINTER(BASS_MIDI_EVENT),ctypes.c_uint,ctypes.c_uint];identified.restype=ctypes.c_int
            status=int(identified(*arguments,identities,len(identities),position))
        else:status=int(function(*arguments))
        return dict(status=status,ticket=int(values[0]),accepted_events=int(values[1]),error=ctypes.c_int(values[2]).value,
            submitted=bool(values[3]),lock_error=ctypes.c_int(values[4]).value,unlock_error=ctypes.c_int(values[5]).value)
    def retry_owned_unlock(self):
        function=self.library.midi_selection_state_retry_owned_unlock
        function.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_int)];function.restype=ctypes.c_int
        error=ctypes.c_int();status=int(function(self.pointer,ctypes.cast(self.owner.performance.bass.BASS_ChannelLock,ctypes.c_void_p),ctypes.byref(error)))
        if status<0:raise RuntimeError(tr('runtime.midi_filter_config_failed',error=(status,error.value)))
        return status==1
    def cancel_pending(self):
        """Explicit stream teardown, never ordinary generation publication.

        Producers/kernel must already be retired and StreamSetFilter removal
        confirmed. Native CANCEL/query/unlock is one same-thread operation;
        refusal retains this owner, metadata and every unconfirmed budget.
        """
        cancel=getattr(self.library,'midi_selection_state_cancel_pending',None)
        if cancel is None:
            # Old candidates cannot prove cancellation of future tickets.
            return self.statistics().get('reserved',0)==0
        cancel.argtypes=[ctypes.c_void_p]*5+[ctypes.POINTER(ctypes.c_int)];cancel.restype=ctypes.c_int
        backend=self.owner.performance.bass;midi=self.owner.performance.bassmidi
        status=(ctypes.c_int*3)()
        result=cancel(self.pointer,ctypes.cast(backend.BASS_ChannelLock,ctypes.c_void_p),
            ctypes.cast(backend.BASS_ChannelGetAttribute,ctypes.c_void_p),
            ctypes.cast(midi.BASS_MIDI_StreamEvents,ctypes.c_void_p),ctypes.cast(backend.BASS_ErrorGetCode,ctypes.c_void_p),status)
        if result in (-2,-1):raise RuntimeError('native selection cancel owner/unlock: '+str(tuple(status)))
        return result==1
    @staticmethod
    def count_submission(bassmidi,data,mode=0x10000):
        """Native RAW/STRUCT decode count; None means unqualified, never zero.

        bassmidi.h ConvertEvents WINAPI, official doc: NULL events yields its
        event count; only TIME/NORSTATUS affect conversion. RAW is submitted
        unchanged after this read-only check. Cross-packet running status and
        unfinished SysEx are not reconstructed from a product guess.
        """
        from bass_bindings import BASS_MIDI_EVENT
        if not mode&BASS_MIDI_EVENTS_RAW:
            if not isinstance(data,ctypes.Array) or getattr(data,'_type_',None) is not BASS_MIDI_EVENT:return None
            return sum(row.event in (MIDI_EVENT_PROGRAM,MIDI_EVENT_BANK,MIDI_EVENT_BANK_LSB,MIDI_EVENT_DRUMS) for row in data)
        raw=bytes(data)
        timed=bool(mode&0x18000000)
        status=None;index=0
        while index<len(raw):
            if timed:
                for _ in range(4):
                    if index>=len(raw):return None
                    delta=raw[index];index+=1
                    if delta<128:break
                else:return None
                if index>=len(raw):return None
            byte=raw[index]
            if byte==0xf0:
                end=raw.find(b'\xf7',index+1)
                if end<0 or any(value>=128 for value in raw[index+1:end]):return None
                index=end+1;status=None;continue
            if byte==0xff:
                if index+2>=len(raw):return None
                at=index+2;size=0
                for _ in range(4):
                    if at>=len(raw):return None
                    value=raw[at];at+=1;size=(size<<7)|(value&127)
                    if value<128:break
                else:return None
                if at+size>len(raw):return None
                index=at+size;status=None;continue
            if 0x80<=byte<0xf0:status=byte;index+=1
            elif byte>=0xf0:return None
            elif status is None or mode&0x2000000:return None
            width=1 if status&0xf0 in (0xc0,0xd0) else 2
            if index+width>len(raw) or any(value>=128 for value in raw[index:index+width]):return None
            index+=width
        converter=bassmidi.BASS_MIDI_ConvertEvents
        converter.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(BASS_MIDI_EVENT),ctypes.c_uint,ctypes.c_uint];converter.restype=ctypes.c_uint
        source=(ctypes.c_ubyte*len(raw)).from_buffer_copy(raw);flags=(mode&0x2000000)|(0x8000000 if timed else 0)
        count=int(converter(source,len(raw),None,0,flags))
        if count==0xffffffff or count>1048576:return None # Same bounded preparation policy, not native max.
        rows=(BASS_MIDI_EVENT*count)()
        if converter(source,len(raw),rows,count,flags)!=count:return None
        return sum(row.event in (MIDI_EVENT_PROGRAM,MIDI_EVENT_BANK,MIDI_EVENT_BANK_LSB,MIDI_EVENT_DRUMS) for row in rows)
    def revision(self):return self.version(),self.busy or self.clock.revision()[1]
    def drain(self):return self.clock.drain((self,))
    def _replay(self,_sync,stream,data,_event):
        if stream!=self.stream:return
        if data in self._checkpoints:return
        ch,event,raw,kind,key,error=self._rows[data];owner=self.owner;field=self.names[key] if key<len(self.names) else None
        if kind in (1,2,3,4,5):
            sources={1:'preset_default',2:'soundfont_group',3:'native_midi',4:'channel_override',5:'preset_restore'}
            profile=field is not None and (field.startswith('scale_') or field.startswith('drum_'))
            owner.record_origin(ch,event,raw,sources[kind],field=None if profile else field)
            if event in MODULATION_NATIVE_FIELDS:owner.performance.mod_observed[ch,MODULATION_NATIVE_FIELDS[event]]=raw
        elif kind==6:
            if event==MIDI_EVENT_DRUMS:
                owner.addresses.drums[ch]=bool(raw);owner.soundfont_presets.reset_channel(ch)
                self._checkpoints[data]=True;return
            name={MIDI_EVENT_PROGRAM:'program',MIDI_EVENT_BANK:'bank_msb',MIDI_EVENT_BANK_LSB:'bank_lsb'}[event]
            getattr(owner.soundfont_presets,name)[ch]=raw
            if event==MIDI_EVENT_PROGRAM:owner.soundfont_presets._performance_tokens.pop(ch,None)
        elif kind==7:
            values=owner.performance._profile_values.setdefault(ch,{})
            if error:values[field]=ctypes.c_int(raw).value
            else:values.pop(field,None)
        elif kind in (8,10):
            values=owner.performance.preset_seeds.applied if kind==8 else owner.performance.preset_seeds.native
            if error:values[ch,field]=ctypes.c_int(raw).value if kind==8 else raw
            else:values.pop((ch,field),None)
        elif kind==9:
            if owner.performance.failure_sink:owner.performance.failure_sink(event,error)
        elif kind==11:raise RuntimeError('native selection baseline or prepared-state qualification: '+str((ch,event,field,error)))
        elif kind==12:
            routing=owner.soundfont_presets;resolved=None if event==0xffffffff and raw==0xffffffff else (ctypes.c_int(event).value,ctypes.c_int(raw).value)
            routing._performance_tokens[ch]=(routing._performance_revision,resolved,routing._performance_key_revisions.get(resolved,0),key,bool(error))
        elif kind==18:
            # The phase receipt follows native memory clearing. This is not a
            # SYSTEM event and does not reset routing/profile tokens early.
            with owner.channel_policy.lock:owner.channel_policy.clear_session(ch)
            for values in (owner.performance.preset_seeds.native,owner.performance.preset_seeds.applied,owner.performance.mod_observed):
                for pair in tuple(values):
                    if pair[0]==ch:values.pop(pair,None)
        elif kind==20:owner.soundfont_presets.reset_channel(ch)
        elif kind==19:
            routing=owner.soundfont_presets;routing.observe_bank_msb(ch,event);routing.observe_bank_lsb(ch,raw)
            routing.observe_program(ch,key);owner.addresses.drums[ch]=bool(error)
        elif kind==27:owner.addresses.drums[ch]=bool(raw)
        elif kind==28:owner.performance._profile_values.setdefault(ch,{})
        elif kind in (29,30,31):
            if key:
                owner.record_origin(ch,event,raw,{29:'manual',30:'channel_reset',31:'native_midi'}[kind])
                if event in MODULATION_NATIVE_FIELDS:owner.performance.mod_observed[ch,MODULATION_NATIVE_FIELDS[event]]=raw
            elif owner.performance.failure_sink:owner.performance.failure_sink(event,error)
        elif kind==22:
            # Prepared full reset uses the source emitter's captured BOOL and
            # thread-local error, not ErrorGetCode on this metadata thread.
            if key:
                owner.record_origin(ch,event,raw,'midi_reset')
                if event in MODULATION_NATIVE_FIELDS:owner.performance.mod_observed[ch,MODULATION_NATIVE_FIELDS[event]]=raw
            elif owner.performance.failure_sink:owner.performance.failure_sink(event,error)
        elif kind in (13,14):owner.stream_effects.delay.replay_selection_graph(kind,ch,raw,event)
        elif kind==15:
            # Original StreamEffects._check logs a captured failed attachment
            # and keeps the later channels/state writers running. A consumer
            # exception retains this checkpoint for metadata-only retry.
            operation=tr('native_delay.channel_attach',channel=ch+1) if key else tr('native_delay.userfx_attach')
            sink=owner.stream_effects.failure_sink
            if sink is not None:sink(operation,error)
        elif kind in (16,17):
            identity=None if kind==17 else (error&65535,(error>>16)&65535)
            self.note_sources.append((data,ch,key,(raw<<32)|event,identity))
        self._checkpoints[data]=True
    def close(self):
        if self.pointer:
            transaction=getattr(self,'snapshot_transaction',None)
            if transaction is not None and transaction.pending is not None:
                if transaction.pending[1]!=threading.get_ident():return False
                if not transaction.run(self.stream,()).success:return False
            if getattr(self,'checkpoint_pointer',None):
                if self.library.midi_selection_state_references(self.pointer)!=2 or not self.retire_checkpoints():return False
            if self.library.midi_selection_state_references(self.pointer)!=1:return False
            if not self.library.midi_selection_state_release(self.pointer):return False
            self.pointer=None
        for plan in self.raw_failure_plans:plan.close()
        self.raw_failure_plans.clear()
        for borrower in self.borrowers:
            if hasattr(borrower,'_selection_borrowers'):borrower._selection_borrowers.discard(self)
        self.borrowers.clear();self.function_owners.clear();return True

    def core_snapshot_at(self,execution,ticket,*,metadata=False,provenance=False):
        """Read all128 selection cores at a delivered type2/11 ingress fence.

        Own cdecl ABI1, via the existing bass.h ChannelLock transaction. No
        ACK, watcher consumption, owner hydration, cancellation or audio emit.
        Missing ABI returns None; unsafe/insufficient copies fail explicitly.
        Optional metadata adds token revision/profile/controller/clear state.
        Accepted origins and Reset/Learn ownership still require their journals:
        this is NOT a complete legacy transfer.
        """
        from bass_bindings import NativeChannelTransaction
        if type(metadata) is not bool:raise ValueError('selection snapshot metadata flag')
        if type(provenance) is not bool:raise ValueError('selection snapshot provenance flag')
        metadata=metadata or provenance;level=3 if provenance else 2 if metadata else 1
        if type(ticket) is not int or not 0<ticket<=0xffffffffffffffff:raise ValueError('selection snapshot ticket')
        if execution.owner is not self.owner or execution.library._handle!=self.library._handle:
            raise ValueError('selection snapshot execution/image owner')
        version=getattr(self.library,'midi_execution_selection_snapshot3_version' if provenance else 'midi_execution_selection_snapshot2_version' if metadata else 'midi_execution_selection_snapshot_version',None)
        if version is None:return None
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:return None
        size=getattr(self.library,'midi_execution_selection_snapshot_words_at3' if provenance else 'midi_execution_selection_snapshot_words_at2' if metadata else 'midi_execution_selection_snapshot_words_at')
        size.argtypes=[ctypes.c_void_p,ctypes.c_uint64,ctypes.c_void_p];size.restype=ctypes.c_uint64
        copy=getattr(self.library,'midi_execution_selection_snapshot_copy3' if provenance else 'midi_execution_selection_snapshot_copy2' if metadata else 'midi_execution_selection_snapshot_copy')
        copy.argtypes=[ctypes.c_void_p];copy.restype=None
        if not self.pointer:raise ValueError('selection snapshot retired owner')
        transaction=getattr(self,'snapshot_transaction',None)
        if transaction is None:
            transaction=self.snapshot_transaction=NativeChannelTransaction(self.owner.performance.bass,native_path=self.library._name)
        with execution.borrow() as pointer:
            if not pointer:raise ValueError('selection snapshot retired execution')
            capacity=int(size(pointer,ticket,self.pointer))
            if not 16<=capacity<=0xffffffff:raise ValueError('selection snapshot capacity or fence')
            words=(ctypes.c_uint64*capacity)()
            request=self.SnapshotRequest(pointer,ticket,self.pointer,self.stream,words,capacity,0)
            result=transaction.run(self.stream,((2,copy,0,ctypes.addressof(request)),))
        if not result.success or request.status!=1:
            raise RuntimeError('selection snapshot refused: '+str((request.status,result.error,result.unlock_error,result.phase,self.statistics())))
        header=tuple(words[:16]);version,channels,fields,watches=header[:4]
        core_words=16+3*watches+128*(14+7*fields+2*watches)+514
        if (version!=level or channels!=128 or fields!=len(self.names) or header[15]!=capacity or
                (capacity<core_words+5+128*10 if metadata else capacity!=core_words)):
            raise RuntimeError('selection snapshot image schema')
        cursor=16;watch_layout=tuple(tuple(words[index:index+3]) for index in range(cursor,cursor+watches*3,3));cursor+=watches*3
        if any(field>=fields or slot>=256 or kind>2 for field,slot,kind in watch_layout):
            raise RuntimeError('selection snapshot watch layout')
        rows=[]
        for _ in range(channels):
            address=tuple(words[cursor:cursor+14]);cursor+=14
            if address[13]>fields:raise RuntimeError('selection snapshot touched order')
            cells=tuple(tuple(words[index:index+6]) for index in range(cursor,cursor+fields*6,6));cursor+=fields*6
            order=tuple(words[cursor:cursor+address[13]]);cursor+=fields
            watched=tuple(tuple(words[index:index+2]) for index in range(cursor,cursor+watches*2,2));cursor+=watches*2
            rows.append(dict(address=address,cells=cells,touched_order=order,watches=watched))
        graph=tuple(words[cursor:cursor+514]);cursor+=514
        extra={}
        if metadata:
            metadata_header=tuple(words[cursor:cursor+5]);cursor+=5
            end_metadata=core_words+5+128*10+metadata_header[4]*3
            if metadata_header[:2]!=(1,1) or (capacity<end_metadata+14 if provenance else capacity!=end_metadata):
                raise RuntimeError('selection metadata snapshot schema')
            channel_metadata=tuple(tuple(words[index:index+10]) for index in range(cursor,cursor+128*10,10));cursor+=128*10
            revisions=tuple(tuple(words[index:index+3]) for index in range(cursor,end_metadata,3));cursor=end_metadata
            extra=dict(metadata_header=metadata_header,channel_metadata=channel_metadata,key_revisions=revisions)
        if provenance:
            footer=tuple(words[cursor:cursor+14]);cursor+=14
            if footer[0]!=1 or footer[11]!=capacity or footer[12]!=end_metadata or footer[3]!=header[6]:raise RuntimeError('provenance snapshot footer')
            donated=[]
            for _ in range(footer[6]):
                descriptor=tuple(words[cursor:cursor+8]);cursor+=8;length=descriptor[7]
                if length not in (1,128,16384) or cursor+length>capacity:raise RuntimeError('provenance donor dimensions')
                donated.append((descriptor,tuple(words[cursor:cursor+length])));cursor+=length
            if cursor+footer[7]*11+footer[8]*386!=capacity:raise RuntimeError('provenance snapshot row counts')
            seed_rows=tuple(tuple(words[index:index+11]) for index in range(cursor,cursor+footer[7]*11,11));cursor+=footer[7]*11
            accepted=[]
            for _ in range(footer[8]):
                identity=tuple(words[cursor:cursor+2]);cursor+=2
                cells=tuple(tuple(words[index:index+3]) for index in range(cursor,cursor+384,3));cursor+=384;accepted.append((identity,cells))
            extra.update(provenance_header=footer,provenance_bindings=tuple(donated),provenance_seeds=seed_rows,provenance_accepted=tuple(accepted))
        if cursor!=capacity:raise RuntimeError('selection snapshot incomplete image')
        return dict(header=header,fields=self.names,watch_layout=watch_layout,channels=tuple(rows),graph=graph,**extra)

    def stage_provenance_state(self,owner,image):
        """Merge a fully validated raw donor image, without owner/audio writes.

        Additive component only. Seed sequence numbers remain opaque legacy
        data; new rows identify common capture order (serial*16+reset rank).
        Product cutover must separately qualify all clears/failure prefixes.
        """
        if owner is not self.owner or not getattr(self,'provenance_fields',None):raise ValueError('provenance stage owner')
        keys={'header','fields','watch_layout','channels','graph','metadata_header','channel_metadata','key_revisions',
              'provenance_header','provenance_bindings','provenance_seeds','provenance_accepted'}
        if type(image) is not dict or set(image)!=keys:raise ValueError('provenance image schema')
        def sequence(row,length):
            if not isinstance(row,(tuple,list)) or len(row)!=length:raise ValueError('provenance dimensions')
            return tuple(row)
        def word(value,maximum=0xffffffffffffffff):
            if type(value) is not int or not 0<=value<=maximum:raise ValueError('provenance word')
            return value
        def words(row,length,maximum=0xffffffffffffffff):
            values=sequence(row,length)
            # Native snapshots contain large homogeneous integer images. Keep
            # exact-type and range rejection, but validate in C-backed bulk
            # operations instead of one Python call/generator per word while
            # the MIDI actor is fenced. bool and int subclasses still fail.
            if (set(map(type,values))-{int} or min(values,default=0)<0 or
                    max(values,default=0)>maximum):raise ValueError('provenance word')
            return values
        footer=words(image['provenance_header'],14);head=words(image['header'],16)
        if (footer[:3]!=(1,owner.stream_generation,self.provenance_activation) or footer[3]!=head[6] or head[0]!=3 or
                footer[4:6]!=(len(self.provenance_sources),len(self.provenance_phases)) or footer[13]!=len(self.provenance_fields) or
                footer[6:9]!=(len(self.provenance_bindings),len(self.provenance_seeds),len(self.provenance_events)) or
                footer[2]>footer[3] or footer[9]>footer[3] or footer[11]!=head[15]):raise ValueError('provenance footer identity')
        # Reuse full selection schema validation without changing its owner.
        core={key:value for key,value in image.items() if not key.startswith('provenance_')}
        core_head=list(head);core_head[0]=2;core_head[15]=footer[12];core['header']=tuple(core_head)
        selection_stage=self.stage_legacy_state(owner,core)
        seed_rows=_snapshot_word_rows(image['provenance_seeds'],len(self.provenance_seeds),11)
        if seed_rows!=self.provenance_seeds:raise ValueError('provenance seed identity')
        epoch=self._legacy_epoch(owner);origins={};inputs={};origin_order={};input_order={};candidates=[];total_cells=0
        for domain,ch,field,key,event,raw,source,phase,present,ordinal,activation in seed_rows:
            name=self.provenance_fields[field];identity=(ch,name,key) if name=='key_pressure' and domain==1 else (ch,name)
            if not present:continue
            if domain==1:
                origins[identity]=ControlOrigin(owner.stream_generation,event,ctypes.c_int64(raw).value,
                    self.provenance_sources[source],ordinal,self.provenance_phases[phase]);origin_order[identity]=(activation,0)
            else:inputs[identity]=ctypes.c_int64(raw).value;input_order[identity]=(activation,0)
        origin_clear=footer[9];channel_origin_clear={}
        reset=(('modulation',0),('expression_level',127),('sustain',0),('portamento',0),('release',64),('attack',64),('pitch',8192),('channel_pressure',0))
        for index,entry in enumerate(sequence(image['provenance_bindings'],len(self.provenance_bindings))):
            descriptor,packed=sequence(entry,2);descriptor=words(descriptor,8)
            if descriptor[:7]!=self.provenance_bindings[index]:raise ValueError('provenance donor identity')
            event,role,field,source,phase,activation,length=descriptor[1:]
            expected_length=1 if role==3 else 16384 if role==1 and self.provenance_fields[field]=='key_pressure' else 128
            if length!=expected_length:raise ValueError('provenance donor length')
            packed=words(packed,length);total_cells+=length
            if not any(packed):continue # Validated empty donor; no capture ordinal can contribute.
            for at,cell in enumerate(packed):
                serial=cell>>16;raw=cell&65535
                if serial>footer[3]:raise ValueError('provenance donor future capture')
                if serial<=activation:continue
                ch=at//128 if length==16384 else at
                if role==3:origin_clear=max(origin_clear,serial);continue
                if ch>=owner.addresses.count:continue
                if role==5:channel_origin_clear[ch]=max(channel_origin_clear.get(ch,0),serial);continue
                if role==4:
                    for rank,(name,value) in enumerate(reset,1):
                        candidates.append((serial,rank,index,1,ch,name,CHANNEL_PARAMETERS[name].event,value,'midi_reset','processed'))
                elif role==2:
                    name=self.provenance_fields[field];low,high=ChannelControlPolicy.bounds(name);value=raw+low
                    if value>high:raise ValueError('provenance input offset domain')
                    candidates.append((serial,0,index,2,ch,name,0,value,'',''))
                else:
                    name=self.provenance_fields[field]
                    if length==16384 and raw&127!=at%128:raise ValueError('provenance keyed cell identity')
                    native_event=(MIDI_EVENT_USERFX if name=='delay_send' else
                                  CHANNEL_PARAMETERS[name].event if name in CHANNEL_PARAMETERS else event)
                    candidates.append((serial,0,index,1,ch,name,native_event,raw,self.provenance_sources[source],self.provenance_phases[phase]))
        if total_cells!=footer[10]:raise ValueError('provenance total donor cells')
        accepted=sequence(image['provenance_accepted'],len(self.provenance_events))
        sources={1:'preset_default',2:'soundfont_group',3:'native_midi',4:'channel_override',5:'preset_restore',22:'midi_reset',29:'manual',30:'channel_reset',31:'native_midi'}
        for index,entry in enumerate(accepted):
            identity,rows=sequence(entry,2);identity=words(identity,2,0xffffffff)
            if identity!=self.provenance_events[index]:raise ValueError('provenance accepted event identity')
            event,field=identity;name=self.provenance_fields[field]
            for ch,(serial,raw,kind) in enumerate(_snapshot_word_rows(rows,128,3)):
                if raw>0xffffffff or serial>footer[3] or kind not in (0,*sources) or (bool(serial)!=bool(kind)):
                    raise ValueError('provenance accepted value')
                if serial:
                    if serial<=footer[2]:raise ValueError('provenance accepted before activation')
                    if ch<owner.addresses.count:candidates.append((serial,0,len(self.provenance_bindings)+index,1,ch,name,event,raw,sources[kind],'accepted'))
        for serial,rank,_index,domain,ch,name,event,raw,source,phase in sorted(candidates):
            if domain==1:
                identity=(ch,name,raw&127) if name=='key_pressure' else (ch,name)
                if (serial,rank)>origin_order.get(identity,(-1,-1)):
                    origins[identity]=ControlOrigin(owner.stream_generation,event,raw,source,serial*16+rank,phase)
                    origin_order[identity]=(serial,rank)
            else:
                identity=ch,name
                if (serial,rank)>input_order.get(identity,(-1,-1)):inputs[identity]=raw;input_order[identity]=(serial,rank)
        for identity in tuple(origins):
            clear=max(origin_clear,channel_origin_clear.get(identity[0],0))
            if clear and origin_order[identity][0]<=clear:del origins[identity]
        for identity in tuple(inputs):
            clear=image['channel_metadata'][identity[0]][3]
            if clear and input_order[identity][0]<=clear:del inputs[identity]
        # Exact rows retain seed insertion order. New keys follow capture order;
        # reconstructing unobserved historical first-insertion order is not claimed.
        return _LegacyProvenanceStage(self,owner,epoch,(owner.control_origins,owner.channel_policy.inputs),
            tuple(origins.items()),tuple(inputs.items()),footer[3],selection_stage)

    def validate_provenance_stage(self,owner,staged):
        if (type(staged) is not _LegacyProvenanceStage or staged.adapter is not self or staged.owner is not owner or
                staged.epoch!=self._legacy_epoch(owner) or staged.targets[0] is not owner.control_origins or
                staged.targets[1] is not owner.channel_policy.inputs):raise ValueError('provenance stale stage')

    @staticmethod
    def _commit_provenance_state_unlocked(owner,staged):
        # Caller validates EVERY compound stage with all owner locks held.
        # No BASS calls, capture mutation, journal ACK or audio replay here.
        owner.control_origins.clear();owner.control_origins.update(staged.origins)
        owner.channel_policy.inputs.clear();owner.channel_policy.inputs.update(staged.inputs)

    def commit_provenance_state(self,owner,staged):
        """Explicit owner commit; product handoff requires its separate ABI."""
        with owner.midi_lock,owner.channel_policy.lock:
            self.validate_provenance_stage(owner,staged)
            self._commit_provenance_state_unlocked(owner,staged)

    def _legacy_destinations(self,owner):
        routing=owner.soundfont_presets;performance=owner.performance
        graph=owner.stream_effects.delay
        return (routing.bank_msb,routing.bank_lsb,routing.program,owner.addresses.drums,
            routing._performance_tokens,performance._profile_values,performance.preset_seeds.native,
            performance.preset_seeds.applied,owner.channel_policy.timbre,performance.mod_observed,
            *(routing.controllers[name] for name,_ in routing.CONTROL_EVENTS),graph.mixes,graph.handles)

    def _legacy_epoch(self,owner):
        routing=owner.soundfont_presets
        clock=getattr(owner,'midi_clock',None);frontier=clock.revision() if clock is not None else None
        graph=owner.stream_effects.delay
        return (owner.stream_handle,owner.stream_generation,self.pointer,self.names,routing._performance_revision,
                tuple(sorted(routing._performance_key_revisions.items())),getattr(owner,'state_token',None),
                owner.channel_policy.revision,frontier,getattr(owner,'observation_token',None),owner.addresses.count,
                graph,getattr(graph,'stream',None),tuple(sorted(graph.active_channels)),graph.userfx_mix,graph.userfx_handle)

    def stage_legacy_state(self,owner,image):
        """Validate/allocate selection state without changing any legacy owner.

        Caller retains an exact type2/11 native fence and MIDI ownership, and
        drains existing journals first. This additive codec does not transfer
        origin/input/Learn ownership or authorize suppressing those journals.
        Snapshot has completed ChannelLock/unlock before Python locks are taken.
        """
        if owner is not self.owner or owner.stream_handle!=self.stream or not self.pointer:
            raise ValueError('selection legacy image owner')
        def integer(value,maximum=0xffffffffffffffff):
            if type(value) is not int or not 0<=value<=maximum:raise ValueError('selection legacy integer')
            return value
        def sequence(value,count):
            if not isinstance(value,(tuple,list)) or len(value)!=count:raise ValueError('selection legacy dimensions')
            return tuple(value)
        def words(value,count,maximum=0xffffffffffffffff):
            values=sequence(value,count)
            if (set(map(type,values))-{int} or min(values,default=0)<0 or
                    max(values,default=0)>maximum):raise ValueError('selection legacy integer')
            return values
        signed=lambda value:ctypes.c_int(value).value
        expected={'header','fields','watch_layout','channels','graph','metadata_header','channel_metadata','key_revisions'}
        if type(image) is not dict or set(image)!=expected:raise ValueError('selection legacy schema')
        fields=sequence(image['fields'],len(self.names))
        if fields!=self.names or len(set(fields))!=len(fields):raise ValueError('selection legacy fields')
        field_set=set(fields);timbre_fields=set(CHANNEL_TIMBRE_FIELDS)
        profile_fields=set();expression_fields=set(PRESET_EXPRESSION_RANGES);mod_fields={name for name in fields if name.startswith('modulation_')}
        for name in fields:
            if name not in expression_fields and name not in ('mix_level','attack','decay','release','cutoff','resonance'):
                performance_profile_key(name);profile_fields.add(name)
        header=words(image['header'],16);version,channels,count,watch_count=header[:4]
        meta=words(image['metadata_header'],5)
        required=16+3*watch_count+128*(14+7*count+2*watch_count)+514+5+128*10+3*meta[4]
        if (version!=2 or channels!=128 or count!=len(fields) or header[5]>header[4] or
                header[9]>1 or header[10]>1 or header[11]>128 or header[15]!=required or meta[:2]!=(1,1)):
            raise ValueError('selection legacy header')
        epoch=self._legacy_epoch(owner);routing=owner.soundfont_presets;performance=owner.performance
        if meta[2:4]!=(epoch[1],epoch[4]):raise ValueError('selection legacy generation')
        if epoch[8] is not None and (epoch[8][1] or epoch[8][0]!=header[6]):raise ValueError('selection legacy changed capture frontier')
        revisions=_snapshot_word_rows(image['key_revisions'],meta[4],3)
        if any(bank>0xffffffff or program>0xffffffff for bank,program,_ in revisions):raise ValueError('selection legacy revision key')
        expected_revisions=tuple(sorted((bank&0xffffffff,program&0xffffffff,value)
            for (bank,program),value in routing._performance_key_revisions.items()))
        if (revisions!=expected_revisions or len({row[:2] for row in revisions})!=len(revisions)):
            raise ValueError('selection legacy revisions')
        layout=_snapshot_word_rows(image['watch_layout'],watch_count,3)
        if layout!=self._selection_watch_layout:raise ValueError('selection legacy prepared watch identity')
        for field,slot,kind in layout:
            if field>=count or slot>=256 or kind>2 or (kind==0 and fields[field] not in expression_fields) or (kind==1 and fields[field] not in timbre_fields) or (kind==2 and fields[field] not in mod_fields):
                raise ValueError('selection legacy watch layout')
        graph=words(image['graph'],514,0xffffffff)
        if graph[512:514]!=header[12:14] or any(value>1 for value in graph[256:512]):raise ValueError('selection legacy graph')
        if graph[256:384]!=tuple(int(ch in epoch[13]) for ch in range(128)):raise ValueError('selection legacy immutable graph active set')
        rows=sequence(image['channels'],128);metadata_rows=_snapshot_word_rows(image['channel_metadata'],128,10)
        destinations=self._legacy_destinations(owner)
        if any(type(target) is not list or len(target)!=128 for target in (*destinations[:4],*destinations[10:])):
            raise ValueError('selection legacy destination arrays')
        if any(type(target) is not dict for target in destinations[4:10]):raise ValueError('selection legacy destination dictionaries')
        if any(type(value) is not dict for value in destinations[5].values()):raise ValueError('selection legacy nested profile dictionary')
        addresses=[[],[],[],[]];tokens=dict(destinations[4]);profiles={ch:dict(value) for ch,value in destinations[5].items()}
        native=dict(destinations[6]);applied=dict(destinations[7]);timbre=dict(destinations[8]);mod=dict(destinations[9])
        existing_fields={}
        for values in (native,applied,timbre,mod):
            for channel,name in values:existing_fields.setdefault(channel,set()).add(name)
        controls=[[] for _ in routing.CONTROL_EVENTS]
        for ch,(row,extra) in enumerate(zip(rows,metadata_rows)):
            if type(row) is not dict or set(row)!={'address','cells','touched_order','watches'}:raise ValueError('selection legacy channel schema')
            address=words(row['address'],14)
            if (address[0]&~0x017f7f7f or any(address[index]>127 for index in (3,4,5,11)) or
                    any(address[index]>1 for index in (6,7,8,12)) or address[7]>address[8] or
                    any(address[index]>0xffffffff for index in (9,10)) or address[13]>count or extra[0]>1 or
                    any(value>header[6] for value in extra[3:5]) or any(value>0xffffffff for value in extra[5:])):
                raise ValueError('selection legacy channel address')
            if graph[384+ch]!=address[6]:raise ValueError('selection legacy drum graph')
            cells=list(_snapshot_word_rows(row['cells'],count,6,0xffffffff))
            order=list(words(row['touched_order'],address[13]))
            if len(set(order))!=len(order) or any(index>=count for index in order):raise ValueError('selection legacy touched order')
            present={index for index,cell in enumerate(cells) if cell[5]&4}
            if set(order)!=present:raise ValueError('selection legacy touched presence')
            for index,cell in enumerate(cells):
                name=fields[index];mask=cell[5]
                if (mask>31 or (mask&1 and (name not in profile_fields or not extra[0])) or
                        (mask&2 and name not in expression_fields) or (mask&4 and name not in timbre_fields) or (mask&8 and name not in mod_fields) or
                        (mask&16 and name not in expression_fields)):
                    raise ValueError('selection legacy cell category')
            pending=[]
            for ordinal,(frontier,packed) in enumerate(_snapshot_word_rows(row['watches'],watch_count,2)):
                captured=packed>>16
                if frontier>header[6] or captured>header[6]:raise ValueError('selection legacy watch clock')
                if captured>frontier:pending.append((captured,ordinal,packed&65535))
            for _,ordinal,value in sorted(pending):
                field,_,kind=layout[ordinal];cell=list(cells[field]);cells[field]=cell
                if kind==0:
                    cell[1]=value;cell[5]|=2
                    if cell[5]&4:cell[2]=value
                elif kind==1:
                    if not cell[5]&4:order.append(field)
                    cell[2]=value;cell[5]|=4
                else:cell[3]=value;cell[5]|=8
            # A per-channel image cannot invent cross-channel insertion order.
            # Retained journals have already made these keys authoritative.
            old_order=[field for channel,field in timbre if channel==ch and field in fields]
            if old_order!=[fields[index] for index in order]:raise ValueError('selection legacy global touched order')
            previous=profiles.get(ch,{})
            outside={name:value for name,value in previous.items() if name not in profile_fields}
            if outside and not extra[0]:raise ValueError('selection legacy absent profile outside domain')
            profile=dict(previous)
            for index,cell in enumerate(cells):
                name=fields[index];mask=cell[5];pair=(ch,name)
                if not mask and name not in previous and name not in existing_fields.get(ch,()):continue
                if name in profile_fields:
                    if mask&1:profile[name]=signed(cell[0])
                    else:profile.pop(name,None)
                for target,bit,position,domain,convert in ((native,2,1,expression_fields,int),(timbre,4,2,field_set,int),
                        (mod,8,3,mod_fields,int),(applied,16,4,expression_fields,signed)):
                    if name in domain:
                        if mask&bit:target[pair]=convert(cell[position])
                        else:target.pop(pair,None)
            if extra[0]:profiles[ch]=profile
            else:profiles.pop(ch,None)
            requested=address[0]
            for target,value in zip(addresses,(requested&127,(requested>>8)&127,(requested>>16)&127,bool(requested&0x1000000))):target.append(value)
            if not address[8] or address[1]!=address[2]:tokens.pop(ch,None)
            else:
                key=None if address[9:11]==(0xffffffff,0xffffffff) else (signed(address[9]),signed(address[10]))
                tokens[ch]=(extra[1],key,extra[2],address[11],bool(address[12]))
            for target,value in zip(controls,extra[5:]):target.append(signed(value))
        values=tuple(tuple(value) for value in addresses)+(tuple(tokens.items()),
            tuple((ch,tuple(profile.items())) for ch,profile in profiles.items()),tuple(native.items()),tuple(applied.items()),
            tuple(timbre.items()),tuple(mod.items()),*(tuple(value) for value in controls),graph[:128],graph[128:256],graph[512:514])
        nested=tuple((ch,value) for ch,value in destinations[5].items())
        return _LegacySelectionStage(self,owner,(epoch,nested),destinations,values)

    def commit_legacy_state(self,owner,staged):
        """Pure Python commit under caller's retained fence/MIDI ownership.

        Schema/stale refusals precede mutation. Preserve mutable
        container identities; never emit MIDI, mutate a journal or attach FX.
        Built-in updates can still allocate; MemoryError rollback is not claimed.
        No origin/input/Learn ownership is acquired by this limited image.
        """
        self.validate_legacy_stage(owner,staged)
        performance=owner.performance;policy=owner.channel_policy
        with owner.midi_lock,performance.lock,policy.lock:
            self.validate_legacy_stage(owner,staged)
            self._commit_legacy_state_unlocked(staged)

    def validate_legacy_stage(self,owner,staged):
        """No writes; call again after acquiring all compound destination locks."""
        if (type(staged) is not _LegacySelectionStage or staged.adapter is not self or staged.owner is not owner or
                staged.epoch[0]!=self._legacy_epoch(owner) or
                len(staged.destinations)!=len(self._legacy_destinations(owner)) or
                any(left is not right for left,right in zip(staged.destinations,self._legacy_destinations(owner)))):
            raise ValueError('selection legacy stale stage')
        if (set(owner.performance._profile_values)!={ch for ch,_ in staged.epoch[1]} or
                any(owner.performance._profile_values.get(ch) is not value or type(value) is not dict for ch,value in staged.epoch[1])):
            raise ValueError('selection legacy changed stage')

    def _commit_legacy_state_unlocked(self,staged):
        """Internal compound commit: caller has validated both owners under locks."""
        for index,(target,value) in enumerate(zip(staged.destinations,staged.values)):
            if type(target) is list:target[:]=value
            elif index==5:
                keep={ch for ch,_ in value}
                for ch in tuple(target):
                    if ch not in keep:del target[ch]
                for ch,items in value:
                    if ch in target:target[ch].clear();target[ch].update(items)
                    else:target[ch]=dict(items)
            else:target.clear();target.update(value)
        graph=staged.owner.stream_effects.delay;graph.userfx_mix,graph.userfx_handle=staged.values[-1]

    def prepare_checkpoints(self,capacity=64):
        """Optional lossless stage arena, distinct from actual BASS readback.

        Allocation is control-side. Published capture copies only; note/audio
        events and the original journal remain unchanged. Consumers must ACK
        every checkpoint before detaching/releasing this extra native borrower.
        """
        if getattr(self,'checkpoint_pointer',None):raise ValueError('selection checkpoints already prepared')
        if type(capacity) is not int or not 1<=capacity<=1024:raise ValueError('selection checkpoint capacity')
        function=self.library.midi_selection_checkpoint_create;function.argtypes=[ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_void_p
        pointer=function(self.pointer,capacity)
        if not pointer:raise RuntimeError(tr('runtime.midi_filter_config_failed',error='checkpoint preparation'))
        self.checkpoint_pointer=pointer;self.checkpoint_transaction=None
        self._attach_checkpoints(pointer)
        return self
    def _attach_checkpoints(self,pointer):
        from bass_bindings import NativeChannelTransaction
        class Attach(ctypes.Structure):
            _fields_=[('state',ctypes.c_void_p),('arena',ctypes.c_void_p),('accepted',ctypes.c_int)]
        function=self.library.midi_selection_checkpoint_attach;function.argtypes=[ctypes.c_void_p];function.restype=None
        request=Attach(self.pointer,pointer,0)
        transaction=getattr(self,'checkpoint_transaction',None)
        if transaction is None:
            transaction=self.checkpoint_transaction=NativeChannelTransaction(self.owner.performance.bass)
        result=transaction.run(self.stream,((2,function,0,ctypes.addressof(request)),))
        # Publication may already have happened when unlock fails; retain the
        # arena/transaction and retry unlock, never repeat original audio.
        self.checkpoint_attach_accepted=bool(request.accepted)
        if not result.success or not request.accepted:raise RuntimeError(tr('runtime.midi_filter_config_failed',error=(result.error,result.unlock_error,result.phase)))
    def checkpoint_statistics(self):
        function=self.library.midi_selection_checkpoint_status;function.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint];function.restype=ctypes.c_int
        values=(ctypes.c_uint64*6)()
        if not getattr(self,'checkpoint_pointer',None) or not function(self.checkpoint_pointer,values,len(values)):return {}
        return dict(zip(('produced','consumed','refused','capacity','fields','watches'),map(int,values)))
    def checkpoint_rows(self,*,metadata=False):
        if type(metadata) is not bool:raise ValueError('selection checkpoint metadata flag')
        stats=self.checkpoint_statistics();result=[]
        function=self.library.midi_selection_checkpoint_read
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint64,ctypes.POINTER(ctypes.c_uint64)]+[ctypes.POINTER(ctypes.c_uint)]*3+[ctypes.POINTER(ctypes.c_uint64)];function.restype=ctypes.c_int
        metadata_read=getattr(self.library,'midi_selection_checkpoint_metadata_read',None) if metadata else None
        if metadata and metadata_read is None:raise RuntimeError('selection checkpoint metadata ABI unavailable')
        if metadata_read is not None:
            metadata_read.argtypes=[ctypes.c_void_p,ctypes.c_uint64,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint];metadata_read.restype=ctypes.c_int
        names=('serial','cursor','channel','event','raw','kind','key','error','requested','bank','lsb','program','drums','token_valid','token_known',
            'token_bank','token_program','token_lsb','token_drums','touch_count','graph_enabled','graph_ready','graph_channels','user_mix')
        for ordinal in range(stats.get('consumed',0),stats.get('produced',0)):
            header=(ctypes.c_uint64*24)();cells=(ctypes.c_uint*(stats['fields']*6))();order=(ctypes.c_uint*stats['fields'])();graph=(ctypes.c_uint*514)();watch=(ctypes.c_uint64*max(1,stats['watches']*2))()
            if not function(self.checkpoint_pointer,ordinal,header,cells,order,graph,watch):raise RuntimeError(tr('runtime.midi_filter_config_failed',error='checkpoint read'))
            row=dict(zip(names,map(int,header)));row['error']=ctypes.c_int(row['error']).value
            row.update(ordinal=ordinal,cells=tuple(tuple(cells[index:index+6]) for index in range(0,len(cells),6)),
                touched_order=tuple(order[:row['touch_count']]),graph=tuple(graph),watches=tuple(watch[:stats['watches']*2]))
            if metadata_read is not None:
                extra=(ctypes.c_uint64*11)()
                if not metadata_read(self.checkpoint_pointer,ordinal,extra,len(extra)) or extra[0]!=1:
                    raise RuntimeError('selection checkpoint metadata unavailable')
                row['metadata']=tuple(extra[1:])
            result.append(row)
        return tuple(result)
    def ack_checkpoint(self,ordinal):
        function=self.library.midi_selection_checkpoint_ack;function.argtypes=[ctypes.c_void_p,ctypes.c_uint64];function.restype=ctypes.c_int
        return bool(function(self.checkpoint_pointer,ordinal))
    def retire_checkpoints(self):
        pointer=getattr(self,'checkpoint_pointer',None)
        if not pointer:return True
        stats=self.checkpoint_statistics()
        if stats['produced']!=stats['consumed']:return False
        transaction=getattr(self,'checkpoint_transaction',None)
        if transaction is not None and transaction.pending is not None:
            if not transaction.run(self.stream,()).success:return False
        self._attach_checkpoints(None)
        function=self.library.midi_selection_checkpoint_release;function.argtypes=[ctypes.c_void_p];function.restype=ctypes.c_int
        if not function(pointer):return False
        self.checkpoint_pointer=None;self.checkpoint_transaction=None;return True

    def action_source(self,result,index,channel,packed,qpc,fallback):
        cache=result.setdefault('selection_source_cache',{})
        if index in cache:return cache[index][0]
        used=result.setdefault('selection_source_ordinals',{})
        start=max(0,used.get(self,self.note_cursor)-self.note_base)
        for offset in range(start,len(self.note_sources)):
            serial,ch,source_packed,tick,identity=self.note_sources[offset]
            if (ch,source_packed,tick)==(channel,packed,qpc):
                ordinal=self.note_base+offset;used[self]=ordinal+1;cache[index]=(identity,self,ordinal)
                return identity
        return fallback

    def commit_note_sources(self,cursor):
        self.note_cursor=max(self.note_cursor,cursor);count=min(len(self.note_sources),max(0,self.note_cursor-self.note_base))
        if count:del self.note_sources[:count];self.note_base+=count

class NativeMidiFunctionMap:
    """Prepared native scalar mappings + metadata-only ordered journal.

    Existing ChannelControlPolicy/Python conversion remains the table oracle.
    MIDI audio never enters Python for a prepared route. Replay updates inputs,
    timbre/baselines/MOD/provenance, NOT BASS or widgets/INI. Reverb/chorus
    requests and floored native values remain distinct; delay borrows existing
    native taps. A conservative default-only selection gate preserves the
    old seven-event order; non-neutral profile/timbre and DRUMS delegate.
    """
    def __init__(self,gate,owner):
        self.gate=gate;self.owner=owner;self.library=None;self.journal=None
        self.generations=[];self.function_owners={};self.decoders={};self.error_low={};self.retired_failures=0
        self.names=tuple(gate.fields[event] for event in gate.events)
        self.slots={field:index for index,field in enumerate(self.names)}
        self.attach_sends=None
        self.attach_delay=None
        self.attach_selection=None;self.selection_cache=None;self.selection_function_cache=None
        self.full_selection_candidate=False;self.selection_adapters={}
        self.selection_fields={0x80000|event:field for event,field in (
            (MIDI_EVENT_PROGRAM,'program'),(MIDI_EVENT_BANK,'bank_msb'),(MIDI_EVENT_BANK_LSB,'bank_lsb'),
            (MIDI_EVENT_MIXLEVEL,'mix_level'),(MIDI_EVENT_ATTACK,'attack'),(MIDI_EVENT_DECAY,'decay'),
            (MIDI_EVENT_RELEASE,'release'),(MIDI_EVENT_CUTOFF,'cutoff'),(MIDI_EVENT_RESONANCE,'resonance'))}
        try:
            library=gate.library
            library.midi_function_map_version.argtypes=[];library.midi_function_map_version.restype=ctypes.c_uint
            if not owner.midi_clock.pointer or owner.midi_clock.library._handle!=library._handle or library.midi_function_map_version()!=1:return
            self.create=library.midi_function_map_create
            self.create.argtypes=[ctypes.c_uint,ctypes.c_void_p,ctypes.POINTER(_NativeMidiMapDescriptor),ctypes.c_uint,ctypes.c_void_p,ctypes.c_void_p]
            self.create.restype=ctypes.c_void_p
            self.destroy=library.midi_function_map_destroy;self.destroy.argtypes=[ctypes.c_void_p];self.destroy.restype=None
            self.failures=library.midi_function_map_failures;self.failures.argtypes=[ctypes.c_void_p];self.failures.restype=ctypes.c_uint64
            self.publish=library.midi_filter_gate_publish_map
            self.publish.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,
                ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint)]
            self.publish.restype=ctypes.c_int
            assert ctypes.sizeof(_NativeMidiMapDescriptor)==24
            self.library=library
            domain=owner.channel_sends.native_domain
            attach=getattr(library,'midi_function_map_sends',None)
            if attach is not None and domain.pointer and domain.library._handle==library._handle:
                attach.argtypes=[ctypes.c_void_p,ctypes.c_void_p];attach.restype=ctypes.c_int
                self.attach_sends=attach
            attach_delay=getattr(library,'midi_function_map_delay',None)
            if attach_delay is not None:
                attach_delay.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_void_p];attach_delay.restype=ctypes.c_int
                self.attach_delay=attach_delay
                self.slots['delay_send']=len(self.names);self.names+=('delay_send',)
            attach_selection=getattr(library,'midi_function_map_selection',None)
            if attach_selection is not None:
                attach_selection.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint]
                attach_selection.restype=ctypes.c_int;self.attach_selection=attach_selection
        except (AttributeError,OSError):pass # Old own-DLL exact existing routing.

    def selection_channels(self):
        """Conservative default-only proof, not a guessed profile substitute.

        Cache the potentially large preset inventory by its owning revision.
        Cheap channel guards also exclude *pending* mapped timbre destinations,
        before their native journal has replayed into the Python override.
        """
        if self.attach_selection is None:return ()
        owner=self.owner;routing=owner.soundfont_presets;performance=owner.performance
        key=(routing.native_selection_revision,id(routing.presets))
        if self.selection_cache is None or self.selection_cache[0]!=key:
            safe=all(item.get('disabled') or (not any(item.get(field) is not None for field in
                ('volume','attack','decay','release','cutoff','resonance')) and
                not item.get('extended') and not item.get('performance')) for item in routing.presets.values())
            self.selection_cache=key,safe
        neutral=(self.selection_cache[1] and not routing.performance and not any(performance.scale_cents)
            and not performance.drum_mixes and not any(performance._profile_values.values()))
        function_key=(owner.channel_policy,owner.channel_policy.revision)
        if self.selection_function_cache is None or self.selection_function_cache[0]!=function_key:
            self.selection_function_cache=function_key,{channel for (channel,_source),target in
                owner.channel_policy.functions.items() if target in CHANNEL_TIMBRE_FIELDS}
        blocked={channel for channel,_field in owner.channel_policy.timbre}
        blocked.update(self.selection_function_cache[1])
        blocked.update(channel for channel,_field in performance.preset_seeds.applied)
        blocked.update(channel for values in routing.controllers.values() for channel,value in enumerate(values) if value!=64)
        return tuple(int(neutral and channel<owner.addresses.count and channel not in blocked and
            routing.bank_lsb[channel]==0) for channel in range(128))

    def _decoder(self,field):
        if field not in self.decoders:
            low,high=ChannelControlPolicy.bounds(field)
            rows=(ctypes.c_ushort*65536)()
            for raw in range(65536):
                value=(MidiPerformanceControls.expression_from_parameter(field,raw) if field in EXPRESSION_EVENTS
                    else (raw>>8)&127 if field=='key_pressure' else raw)
                amount=max(low,min(high,int(round(value))))
                if field in ('portamento','sustain','sostenuto','soft'):amount=127 if amount>=64 else 0
                rows[raw]=amount-low
            self.decoders[field]=rows
        return self.decoders[field]

    def prepare(self,stream,functions,keys,selection=()):
        if self.library is None:return None
        if not all(isinstance(function,ctypes._CFuncPtr) for function in
                (self.owner.performance.bassmidi.BASS_MIDI_StreamEvent,self.owner.performance.bass.BASS_ErrorGetCode)):
            return None # Explicit fake backend: never store a Python callable in an audio ABI.
        eligible=[((channel,source),target) for (channel,source),target in sorted(functions.items())
            if source in self.slots and channel<self.owner.addresses.count and
            (target!='delay_send' or self.attach_delay is not None) and (target not in ('reverb','chorus') or self.attach_sends is not None)
            and (target is None or target in self.slots)]
        if not eligible and not any(selection) and not self.full_selection_candidate:return None
        if self.journal is None:
            events=(*self.gate.events,MIDI_EVENT_USERFX,*(base+index for base in (0x20000,0x30000,0x40000,0x50000) for index in range(len(self.names))))
            if self.attach_sends is not None:events+=tuple(0x60000+self.slots[field] for field in ('reverb','chorus'))
            if self.attach_selection is not None:events+=tuple(self.selection_fields)
            self.journal=NativeMidiState(stream,events,self.owner._bind_callback(self._replay,native=True,writes=True),
                self.gate.native_path,clock=self.owner.midi_clock)
            if not self.journal.pointer or self.journal.clock is not self.owner.midi_clock:
                self.journal.close();self.journal=None;return None
        if self.journal.stream!=stream:raise RuntimeError(tr('runtime.midi_filter_config_failed',error='stream lifetime'))
        compiled=[];tables={}
        for (channel,source),target in eligible:
            source_slot=self.slots[source];target_slot=self.slots[target] if target is not None else source_slot
            decoder=self._decoder(source);low,high=ChannelControlPolicy.bounds(source)
            key=keys.get(channel,60);table_key=(source,target,key if target=='key_pressure' else None)
            if table_key not in tables:
                target_values=[]
                for offset in range(high-low+1):
                    amount=ChannelControlPolicy.convert(source,target,offset+low)
                    raw=(0 if target is None else (amount<<8)|key if target=='key_pressure' else
                        MidiPerformanceControls.expression_parameter(target,amount) if target in EXPRESSION_EVENTS else amount)
                    assert 0<=raw<=65535
                    target_values.append(raw<<16)
                tables[table_key]=(ctypes.c_uint*65536)(*(int(offset)|target_values[offset] for offset in decoder))
            compiled.append(_NativeMidiMapDescriptor(source_slot,channel,target_slot,
                MIDI_EVENT_USERFX if target=='delay_send' else CHANNEL_PARAMETERS[target].event if target is not None else 0,tables[table_key]))
        # Extended targets' native/manual baseline observers must be installed
        # BEFORE rendering/filter entry, not by a Python callback during audio.
        if any(target in PRESET_EXPRESSION_RANGES for target in functions.values()):
            self.owner.performance.preset_seeds.bind(True)
        rows=(_NativeMidiMapDescriptor*len(compiled))(*compiled)
        emit=self.owner.performance.bassmidi.BASS_MIDI_StreamEvent;error=self.owner.performance.bass.BASS_ErrorGetCode
        pointer=self.create(len(self.names),self.journal.pointer,rows,len(rows),
            ctypes.cast(emit,ctypes.c_void_p),ctypes.cast(error,ctypes.c_void_p))
        if not pointer:raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_function_map_create'))
        if self.attach_sends is not None and not self.attach_sends(pointer,self.owner.channel_sends.native_domain.pointer):
            self.destroy(pointer)
            raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_function_map_sends'))
        if any(selection) and not self.attach_selection(pointer,(ctypes.c_uint*128)(*selection),128):
            self.destroy(pointer)
            raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_function_map_selection'))
        if self.full_selection_candidate:
            adapter=None
            try:
                adapter=NativeSelectionAdapter(self.owner,self.library)
                attach=self.library.midi_function_map_selection_state;attach.argtypes=[ctypes.c_void_p,ctypes.c_void_p];attach.restype=ctypes.c_int
                if not attach(pointer,adapter.pointer):raise RuntimeError('native selection state publication')
                self.selection_adapters[pointer]=adapter
            except BaseException:
                self.destroy(pointer)
                if adapter is not None:adapter.close()
                raise
        delay_channels={channel for ((channel,_source),target) in eligible if target=='delay_send'}
        if delay_channels:
            try:
                with self.owner.delay_runtime.midi_links(self.library) as links:
                    if links is None:raise RuntimeError(tr('native_delay.prepare_failed',error='DLL image/ABI'))
                    for channel in delay_channels:
                        if not self.attach_delay(pointer,channel,links.get((channel,False)),links.get((channel,True))):
                            raise RuntimeError(tr('native_delay.prepare_failed',error='midi_function_map_delay'))
            except BaseException:
                self.destroy(pointer);raise
        self.generations.append(pointer)
        self.function_owners[pointer]=(emit,error) # BASS/probe DLL function borrowers survive the generation.
        return pointer # Native copies own all table data; no Python borrow remains.

    def _replay(self,_sync,stream,data,event):
        if not self.journal or stream!=self.journal.stream:return
        channel,raw=data>>16,data&65535
        if channel>=self.owner.addresses.count:return
        if event in self.selection_fields:
            field=self.selection_fields[event]
            if field in ('program','bank_msb','bank_lsb'):
                routing=self.owner.soundfont_presets
                getattr(routing,field)[channel]=raw
                # Native qualification proves LSB0: this token uses the exact
                # requested bank/program, never a future Note's resolved source.
                # Match the old token without applying a profile/audio again.
                if field=='program':routing._performance_tokens.pop(channel,None)
                routing.performance_seed(channel)
            else:self.owner.record_origin(channel,event&~0x80000,raw,'preset_default',phase='accepted',field=field)
            return
        kind=event&0xf0000
        if kind in (0x20000,0x30000,0x40000,0x50000,0x60000):
            field=self.names[event&65535]
            if kind==0x20000:
                with self.owner.channel_policy.lock:
                    self.owner.channel_policy.inputs[channel,field]=raw+ChannelControlPolicy.bounds(field)[0]
            elif kind==0x30000:
                if field in CHANNEL_TIMBRE_FIELDS:
                    with self.owner.channel_policy.lock:self.owner.channel_policy.timbre[channel,field]=raw
                    if field in PRESET_EXPRESSION_RANGES:self.owner.performance.preset_seeds.native[channel,field]=raw
            elif kind==0x40000:self.error_low[channel,field]=raw
            elif kind==0x50000:
                error=ctypes.c_int((raw<<16)|self.error_low.get((channel,field),0)).value
                if field in EXPRESSION_EVENTS and self.owner.performance.failure_sink:
                    self.owner.performance.failure_sink(CHANNEL_PARAMETERS[field].event,error)
            elif kind==0x60000:
                values=self.owner.channel_sends.channel_reverb if field=='reverb' else self.owner.channel_sends.channel_chorus
                values[channel]=raw # Successful requested send; no audio resubmission.
            return
        field='delay_send' if event==MIDI_EVENT_USERFX else self.gate.fields[event]
        if field=='delay_send':
            self.owner.delay_runtime.states[channel].send=raw
            if channel==9:self.owner.delay_runtime.melodic_nine.send=raw
            if raw and self.owner.addresses.drums[channel] and channel!=9 and channel not in self.owner.drum_delay_warnings:
                self.owner.drum_delay_warnings.add(channel)
                self.owner.append_log(log_text('runtime.drum_delay_port_limited',port=channel//16,channel=channel%16+1))
        if event in MODULATION_NATIVE_FIELDS:self.owner.performance.mod_observed[channel,field]=raw
        self.owner.record_origin(channel,event,raw,'native_midi',phase='accepted',field=field)

    def retire(self,current):
        for pointer in tuple(self.generations):
            if pointer!=current:
                self.retired_failures+=int(self.failures(pointer));self.destroy(pointer);self.generations.remove(pointer)
                self.function_owners.pop(pointer,None)
                # A freed C map address may be reused by the next generation.
                # Kernel-held selection roots outlive that address: retain
                # them under a unique retirement identity, never the C map key.
                adapter=self.selection_adapters.pop(pointer,None)
                if adapter is not None:self.selection_adapters[('retired',object())]=adapter
        for pointer,adapter in tuple(self.selection_adapters.items()):
            if pointer!=current and pointer not in self.generations:
                if adapter.library.midi_selection_state_references(adapter.pointer)>1:continue
                if not adapter.close():raise RuntimeError('native selection retirement metadata pending')
                self.selection_adapters.pop(pointer)

    def failure_count(self):
        return self.retired_failures+sum(int(self.failures(pointer)) for pointer in self.generations)

    def close(self):
        self.retire(None)
        if self.selection_adapters:raise RuntimeError('native selection kernel/graph borrower retirement pending')
        if self.journal:self.journal.close();self.journal=None
        self.decoders.clear();self.error_low.clear()


class NativeMidiFilterGate(NativeNoteFilter):
    """Native mappings/default selections; non-neutral selections delegate.

    Immutable field IDs + bounded route copy under BASS_ChannelLock. Prepare
    outside that lock; one C++ call locks/copies/unlocks, with no GIL reentry
    while locked. Revision cache avoids unchanged-route scans. Existing
    note-filter ABI remains independently available for older DLLs/tests.
    No callback wait/lock/Python for passthrough.
    """
    def __init__(self, delegate, fields, native_path=None, *, owner=None):
        native_path=native_path or os.environ.get('ABMSYNTH_OBSERVATION_DLL',str(Path(__file__).with_name('abmsynth_dsp.dll')))
        super().__init__(delegate,native_path)
        self.native_path=native_path;self.owner=owner;self.function_map=None;self.transaction=None
        self.installed_stream=0
        self.playback_timing=None
        self.pointer=None;self.signature=None;self.checked_key=None;self.fields=dict(fields)
        self.events=tuple(fields)
        try:
            library=ctypes.CDLL(str(native_path))
            create=library.midi_filter_gate_create
            create.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint];create.restype=ctypes.c_void_p
            procedure=library.midi_filter_gate_proc;procedure.argtypes=[];procedure.restype=ctypes.c_void_p
            self.configure=library.midi_filter_gate_configure
            self.configure.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint]
            self.configure.restype=ctypes.c_int
            # Require the complete publication ABI before creating an owner.
            # An older DLL uses the independent exact legacy note/delegate ABI.
            self.publish=library.midi_filter_gate_publish
            self.publish.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,
                ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,ctypes.c_void_p,ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint)]
            self.publish.restype=ctypes.c_int
            self.destroy=library.midi_filter_gate_destroy;self.destroy.argtypes=[ctypes.c_void_p];self.destroy.restype=None
            types=(ctypes.c_uint*len(self.events))(*self.events)
            self.pointer=create(ctypes.cast(delegate,ctypes.c_void_p),types,len(self.events))
            if self.pointer:
                self.library=library;self.callback=ctypes.c_void_p(procedure());self.user=ctypes.c_void_p(self.pointer)
        except (OSError,AttributeError):pass # Existing exact delegate/passthrough fallback.
        if self.pointer and owner is not None:self.function_map=NativeMidiFunctionMap(self,owner)

    def bind_playback_timing(self, timing, *, freed=False):
        """Same-image accepted filter position; caller owns source barrier.

        No second filter, audio submission, or Python render callback is added.
        Older native images preserve their existing v1 observer, unqualified
        for accepted event-position correlation.
        """
        if timing is self.playback_timing:return bool(timing)
        if not self.pointer:return False
        if not hasattr(self.library,'midi_filter_gate_timing'):return False
        if timing is not None:
            if not timing.pointer or timing.stream!=self.installed_stream:return False
            if self.library._handle!=timing.dll._handle:return False
        function=self.library.midi_filter_gate_timing
        function.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        if freed:
            result=function(self.pointer,timing.pointer if timing else None,self.installed_stream)
        else:
            backend=timing.bass if timing is not None else self.playback_timing.bass
            publish=self.library.midi_filter_gate_timing_publish
            publish.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_int)]
            publish.restype=ctypes.c_int;error=(ctypes.c_int*2)()
            result=publish(self.pointer,timing.pointer if timing else None,self.installed_stream,
                ctypes.cast(backend.BASS_ChannelLock,ctypes.c_void_p),ctypes.cast(backend.BASS_ErrorGetCode,ctypes.c_void_p),error)
            if result==-2:
                from bass_bindings import NativeChannelTransaction
                if self.transaction is None:self.transaction=NativeChannelTransaction(backend,self.native_path)
                self.transaction.pending=(self.installed_stream,threading.get_ident(),True)
        if result==-2:
            if self.playback_timing is not None:self.playback_timing._filter_barrier_gate=self
            if timing is not None:timing._filter_barrier_gate=self
        applied=result==1 if freed else bool(error[1])
        if not applied:raise RuntimeError(tr('runtime.midi_filter_config_failed',error=error[0] if not freed else 'ABI'))
        previous=self.playback_timing;self.playback_timing=timing
        if previous is not None:previous._filter_gate=None;previous.position_qualified=False
        if timing is not None:timing._filter_gate=self;timing.position_qualified=True
        if result!=1:raise RuntimeError(tr('runtime.midi_filter_config_failed',error=error[0]))
        return timing is not None

    def retry_publication(self):
        if self.transaction is not None and self.transaction.pending is not None:
            result=self.transaction.run(0,())
            if result.phase:raise RuntimeError(tr('runtime.midi_filter_config_failed',error=result.unlock_error))

    def refresh(self, bass, stream, addresses, policy):
        if not self.pointer:return
        self.retry_publication()
        mapper=self.function_map
        native_mapping=mapper is not None and mapper.library is not None and not getattr(self.owner.performance,'_native_failure_compatibility',False)
        delay_revision=self.owner.delay_runtime.native_revision if native_mapping and mapper.attach_delay is not None else None
        if native_mapping:self.owner.drain_native_controls()
        with policy.lock:
            selection=mapper.selection_channels() if native_mapping else ()
            checked_key=(stream,addresses.count,policy,policy.revision,native_mapping)
            if native_mapping and mapper.full_selection_candidate:
                routing=self.owner.soundfont_presets
                checked_key+=(routing.native_selection_revision,routing._performance_revision,tuple(policy.timbre.items()),True)
            if selection:checked_key+=(selection,)
            if delay_revision is not None:checked_key+=(delay_revision,)
            if checked_key==self.checked_key:return
            signature=(stream,addresses.count,native_mapping,tuple(sorted(policy.functions.items())) if native_mapping
                else tuple(sorted(policy.functions)),tuple(sorted(policy.pressure_keys.items())))
            if native_mapping and mapper.full_selection_candidate:signature+=checked_key[4:]
            if selection:signature+=(selection,)
            if delay_revision is not None:signature+=(delay_revision,)
            if signature==self.signature:
                self.checked_key=checked_key;return
            functions=dict(policy.functions);mapped=set(functions);keys=dict(policy.pressure_keys)
        routes=(ctypes.c_uint*(len(self.events)*128))()
        for slot,event in enumerate(self.events):
            field=self.fields[event]
            for channel in range(addresses.count):
                if (channel,field) in mapped:
                    routes[slot*128+channel]=1|(keys.get(channel,60)<<8) if field=='key_pressure' else 1
        error=ctypes.c_uint()
        prepared=mapper.prepare(stream,functions,keys,selection) if native_mapping else None
        arguments=(self.pointer,stream,addresses.count,routes,len(routes))
        if native_mapping:arguments+= (prepared,)
        result=(mapper.publish if native_mapping else self.publish)(*arguments,
            ctypes.cast(bass.BASS_ChannelLock,ctypes.c_void_p),
            ctypes.cast(bass.BASS_ErrorGetCode,ctypes.c_void_p),ctypes.byref(error))
        if result!=1:
            if result==-2:
                from bass_bindings import NativeChannelTransaction
                if self.transaction is None:self.transaction=NativeChannelTransaction(bass,self.native_path)
                self.transaction.backend=bass
                self.transaction.pending=(stream,threading.get_ident(),True)
            # Transport retains32 raw bits; bass.h ErrorGetCode is signed int,
            # including BASS_ERROR_UNKNOWN=-1. Do not display it as4294967295.
            code=ctypes.c_int(error.value).value if result in (0,-2) else 'ABI'
            raise RuntimeError(tr('runtime.midi_filter_config_failed',error=code))
        if native_mapping:
            self.published_map=prepared
            # Kernel may still borrow an old selection generation until its
            # paused configure switches that pointer; preserve retired roots.
            mapper.retire(prepared)
        # A failure never certifies publication. Retry even if policy is unchanged.
        self.signature=signature;self.checked_key=checked_key

    def close(self,*,cancel_pending=False):
        # Caller holds successful StreamSetFilter removal/replacement barrier.
        self.retry_publication()
        self.bind_playback_timing(None,freed=True)
        self.signature=self.checked_key=None;self.published_map=None;self.installed_stream=0
        if self.owner is not None:self.owner.channel_filter_state=None
        if self.pointer and hasattr(self.library,'midi_filter_gate_release_map'):
            release=self.library.midi_filter_gate_release_map;release.argtypes=[ctypes.c_void_p];release.restype=None;release(self.pointer)
        if cancel_pending and self.function_map:
            # Only EngineRuntime stream teardown selects this branch. Ordinary
            # retire(prepared) leaves future events and their leases intact.
            for adapter in tuple(self.function_map.selection_adapters.values()):
                if not adapter.cancel_pending():raise RuntimeError('native selection pending-event cancellation unconfirmed')
        if self.function_map:self.function_map.close()
        if self.pointer:self.destroy(self.pointer);self.pointer=None
        if self.function_map:self.function_map=None
        self.signature=None;self.checked_key=None


MIDI_CHANNELS_PER_PORT = 16
MIDI_PORT_LIMIT = 8
MIDI_CHANNEL_LIMIT = MIDI_CHANNELS_PER_PORT * MIDI_PORT_LIMIT


@dataclass(frozen=True)
class ChannelReadback:
    """Numeric compatibility value plus truthful current-state qualification."""
    value: int
    known: bool
    source: str


@dataclass(frozen=True)
class ControlOrigin:
    """Native admission/processing evidence, not a saved setting.

    None means unobserved/unassigned; an explicit raw0 remains an observation.
    Native units are retained, including14-bit tuning and keyed pressure.
    """
    generation: int
    event: int
    raw: int
    source: str
    sequence: int
    phase: str = "accepted"


@dataclass(frozen=True)
class MidiAddress:
    """Wire channel is local to a port; native channel is never a status byte.

    BASSMIDI supports 8 blocks of 16 channels. Out-of-range ports are rejected
    rather than silently aliasing independent parts onto port zero.
    """
    port: int = 0
    channel: int = 0

    def __post_init__(self):
        if (type(self.port) is not int or type(self.channel) is not int or
                not 0 <= self.port < MIDI_PORT_LIMIT or not 0 <= self.channel < MIDI_CHANNELS_PER_PORT):
            raise ValueError("MIDI address")

    @property
    def native(self):
        return self.port * MIDI_CHANNELS_PER_PORT + self.channel

    @classmethod
    def from_native(cls, channel):
        if type(channel) is not int:
            raise ValueError("MIDI channel")
        return cls(*divmod(channel, MIDI_CHANNELS_PER_PORT))


class MidiAddressSpace:
    """Allocated native channels and actual percussion state for one engine.

    Grow only at a port boundary, outside rendering. The ordinary one-port
    startup remains 16 channels. Every port's channel 10 defaults to drums;
    native DRUMS/SysEx can subsequently change any channel independently.
    """
    def __init__(self):
        self.count = MIDI_CHANNELS_PER_PORT
        self.used_ports=set()
        self.drums = [index % MIDI_CHANNELS_PER_PORT == 9 for index in range(MIDI_CHANNEL_LIMIT)]

    def channels(self):
        return range(self.count)

    def ports(self):
        return range(self.count // MIDI_CHANNELS_PER_PORT)

    def reset(self, count=MIDI_CHANNELS_PER_PORT):
        if not MIDI_CHANNELS_PER_PORT <= count <= MIDI_CHANNEL_LIMIT or count % MIDI_CHANNELS_PER_PORT:
            raise ValueError("MIDI channel count")
        self.count = int(count)
        self.used_ports.clear()
        self.drums[:] = [index % MIDI_CHANNELS_PER_PORT == 9 for index in range(MIDI_CHANNEL_LIMIT)]


# ABI shared with driver/winmm/ABMSynthMidi.cpp. Keep the fixed-size ring in
# pagefile-backed shared memory; a DAW MIDI callback never writes a log file.
_TRACE_MAPPING_NAME = "Local\\ABMSynthMidiTraceV1"
_TRACE_MAGIC = 0x31524441
_TRACE_CAPACITY = 4096
_TRACE_OUTCOMES = {1: "delivered", 2: "no_pipe", 3: "header_failed", 4: "payload_failed"}


def trace_midi_payload(data: bytes | bytearray) -> dict:
    """Keep every event observable without hex-expanding a megabyte SysEx in a callback."""
    return {"data_hex": data[:256].hex(" "), "length": len(data),
            "data_truncated": len(data) > 256}


class _DriverTraceEntry(ctypes.Structure):
    _pack_ = 8
    _fields_ = [
        ("committed_sequence", ctypes.c_int64), ("counter", ctypes.c_int64),
        ("process_id", ctypes.c_uint32), ("thread_id", ctypes.c_uint32),
        ("length", ctypes.c_uint32), ("outcome", ctypes.c_uint32),
        ("error", ctypes.c_uint32), ("header_bytes", ctypes.c_uint32),
        ("payload_bytes", ctypes.c_uint32), ("prefix", ctypes.c_ubyte * 8),
    ]


class _DriverTraceMapping(ctypes.Structure):
    _pack_ = 8
    _fields_ = [
        ("magic", ctypes.c_uint32), ("version", ctypes.c_uint32),
        ("capacity", ctypes.c_uint32), ("enabled", ctypes.c_int32),
        ("next_sequence", ctypes.c_int64),
        ("entries", _DriverTraceEntry * _TRACE_CAPACITY),
    ]


assert ctypes.sizeof(_DriverTraceEntry) == 56
assert ctypes.sizeof(_DriverTraceMapping) == 24 + _TRACE_CAPACITY * 56
assert _DriverTraceEntry.prefix.offset == 44
assert _DriverTraceMapping.entries.offset == 24

_clock = ctypes.WinDLL("kernel32", use_last_error=True)
_clock.QueryPerformanceCounter.argtypes = [ctypes.POINTER(ctypes.c_int64)]
_clock.QueryPerformanceCounter.restype = ctypes.c_int
_clock.QueryPerformanceFrequency.argtypes = [ctypes.POINTER(ctypes.c_int64)]
_clock.QueryPerformanceFrequency.restype = ctypes.c_int
_clock.GetCurrentThreadId.argtypes=[]
_clock.GetCurrentThreadId.restype=ctypes.c_uint


class MidiLossJournal:
    """Read-only failure evidence; invoked by telemetry, never MIDI/rendering.

    ABMSynthMidi.cpp extends OriginHealth after its unchanged40-byte prefix.
    OpenFileMapping(FILE_MAP_READ) must not create a missing producer object.
    MapViewOfFile bytes must fit its declared size (Win32 memoryapi.h).
    """
    capacity=128
    size=48+capacity*64

    @staticmethod
    def read(source,previous,total):
        import struct
        pid,epoch=source
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.OpenFileMappingW.argtypes=[ctypes.c_uint,ctypes.c_int,ctypes.c_wchar_p]
        api.OpenFileMappingW.restype=ctypes.c_void_p
        api.MapViewOfFile.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.c_size_t]
        api.MapViewOfFile.restype=ctypes.c_void_p
        api.UnmapViewOfFile.argtypes=[ctypes.c_void_p];api.UnmapViewOfFile.restype=ctypes.c_int
        api.CloseHandle.argtypes=[ctypes.c_void_p];api.CloseHandle.restype=ctypes.c_int
        handle=api.OpenFileMappingW(4,False,f'Local\\ABMSynthMidiOriginV1-{pid}-{epoch}')
        if not handle:return ()
        view=None
        try:
            view=api.MapViewOfFile(handle,4,0,0,40)
            if not view:return ()
            magic,version,owner,size,generation,frequency,losses=struct.unpack('<IIIIQQQ',ctypes.string_at(view,40))
            if (magic,version,owner,generation,size)!=(0x314f4d41,1,pid,epoch,MidiLossJournal.size):return ()
            api.UnmapViewOfFile(view);view=None
            view=api.MapViewOfFile(handle,4,0,0,size)
            if not view or struct.unpack('<II',ctypes.string_at(view+40,8))!=(1,MidiLossJournal.capacity):return ()
            result=[]
            for slot in range(MidiLossJournal.capacity):
                address=view+48+slot*64
                raw=ctypes.string_at(address,64)
                sequence,ingress,length,reason,error,reserved,prefix=struct.unpack('<qqIIII32s',raw)
                # Negative=in flight. Never decode a torn/overwritten record.
                if (previous<sequence<=total and sequence==ctypes.c_int64.from_address(address).value
                        and not reserved and 0<length<=1024*1024 and 2<=reason<=7):
                    result.append(dict(sequence=sequence,ingress=ingress,length=length,reason=reason,
                        error=error,prefix=prefix[:min(length,32)]))
            return tuple(sorted(result,key=lambda row:row['sequence']))
        finally:
            if view:api.UnmapViewOfFile(view)
            api.CloseHandle(handle)


def describe_lost_midi(prefix,length):
    """Describe confirmed failed bytes, not the packet following a gap.

    Long frames retain a prefix, not reconstructed SysEx or guessed suffixes.
    Malformed/partial frames still retain bounded hexadecimal evidence.
    """
    data=bytes(prefix[:32]);messages=[];index=0;running=None
    while index<len(data):
        status=data[index]
        if status>=0xf8:
            messages.append({0xf8:'Timing Clock',0xfa:'Start',0xfb:'Continue',0xfc:'Stop',
                0xfe:'Active Sensing',0xff:'System Reset'}.get(status,f'0x{status:02X}'));index+=1;continue
        if status==0xf0:messages.append('SysEx');break
        if status>=0x80:
            index+=1;running=status if status<0xf0 else None
        elif running is not None:status=running
        else:break
        command=status&0xf0
        size=1 if command in (0xc0,0xd0) or status in (0xf1,0xf3) else 2 if status==0xf2 or 0x80<=status<0xc0 or command==0xe0 else 0
        values=[]
        while len(values)<size and index<len(data):
            value=data[index]
            if value>=0xf8:
                messages.append({0xf8:'Timing Clock',0xfa:'Start',0xfb:'Continue',0xfc:'Stop',
                    0xfe:'Active Sensing',0xff:'System Reset'}.get(value,f'0x{value:02X}'));index+=1;continue
            if value>=0x80:break
            values.append(value);index+=1
        if len(values)!=size:break
        channel=(status&15)+1
        if command in (0x80,0x90):
            messages.append(tr('midi_loss.note',channel=channel,kind='Note Off' if command==0x80 or values[1]==0 else 'Note On',note=values[0],velocity=values[1]))
        elif command==0xb0:messages.append(tr('midi_loss.cc',channel=channel,controller=values[0],value=values[1]))
        elif command==0xa0:messages.append(tr('midi_loss.poly',channel=channel,note=values[0],value=values[1]))
        elif command==0xc0:messages.append(tr('midi_loss.program',channel=channel,value=values[0]))
        elif command==0xd0:messages.append(tr('midi_loss.pressure',channel=channel,value=values[0]))
        elif command==0xe0:messages.append(tr('midi_loss.pitch',channel=channel,value=(values[0]|values[1]<<7)-8192))
        else:messages.append(f'0x{status:02X}')
    return tr('midi_loss.payload',messages='; '.join(messages) or tr('midi_loss.unparsed'),
        raw=data.hex(' ').upper(),length=length,suffix=tr('midi_loss.prefix_only') if length>len(data) else '')


class MidiLossLog:
    """Coalesce counters outside real-time dispatch; preserve old-driver truth."""
    def __init__(self,log,reader=None):
        self.log=log;self.reader=reader or MidiLossJournal.read;self.totals={};self.next_poll=0.;self.notice_overflow=0

    def poll(self,coverage,*,now=None):
        now=time.monotonic() if now is None else now
        if now<self.next_poll:return
        self.next_poll=now+.25
        for source,total in tuple(coverage.losses.items()):
            previous=self.totals.get(source,0)
            if total<=previous:continue
            rows=self.reader(source,previous,total) if source is not None else ()
            pid,epoch=source or (0,0)
            # At most128 retained records/source; aggregate identical failures.
            groups=Counter((row['reason'],row['error'],row['length'],row['prefix']) for row in rows)
            for (reason,error,length,prefix),count in groups.items():
                self.log(log_text('midi_loss.detail',pid=pid,epoch=epoch,count=count,
                    reason=tr({2:'midi_loss.no_pipe',3:'midi_loss.header_failed',4:'midi_loss.payload_failed',
                        5:'midi_loss.no_memory',6:'midi_loss.admission_failed',7:'midi_loss.cancelled_queue'}[reason]),
                    error=error,message=describe_lost_midi(prefix,length)))
            missing=total-previous-len(rows)
            if missing>0:self.log(log_text('midi_loss.unavailable',pid=pid,epoch=epoch,count=missing))
            self.totals[source]=total
        notices={}
        while coverage.loss_notices:
            source,sequence=coverage.loss_notices.popleft();notices[source]=sequence
        for source,sequence in notices.items():
            pid,epoch=source or (0,0)
            self.log(log_text('midi_loss.sequence',pid=pid,epoch=epoch,sequence=sequence))
        if coverage.loss_notice_overflow>self.notice_overflow:
            self.log(log_text('midi_loss.notice_overflow',count=coverage.loss_notice_overflow-self.notice_overflow))
            self.notice_overflow=coverage.loss_notice_overflow


def _counter_value() -> int:
    value = ctypes.c_int64()
    _clock.QueryPerformanceCounter(ctypes.byref(value))
    return value.value


class MidiTraceRecorder:
    """Bounded, asynchronous MIDI trace owned by one user capture session."""

    def __init__(self, path: Path, *, mapping_name: str = _TRACE_MAPPING_NAME):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("x", encoding="utf-8", buffering=1024 * 1024)
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=16384)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._packet_sequence = 0
        self._dropped = 0
        self._driver_overwritten = 0
        self._counts = Counter()
        self._write_error = None
        self._accepting = True
        self._mapping = None
        self._shared = None
        try:
            self._mapping = mmap.mmap(
                -1, ctypes.sizeof(_DriverTraceMapping), tagname=mapping_name,
            )
            self._shared = _DriverTraceMapping.from_buffer(self._mapping)
            if self._shared.magic == 0:
                self._shared.magic = _TRACE_MAGIC
                self._shared.version = 1
                self._shared.capacity = _TRACE_CAPACITY
            if (self._shared.magic, self._shared.version, self._shared.capacity) != (
                _TRACE_MAGIC, 1, _TRACE_CAPACITY,
            ):
                raise OSError("The installed MIDI trace mapping has an incompatible version")
            self._shared.enabled = 0
            self._next_driver_sequence = int(self._shared.next_sequence) + 1
        except Exception:
            self._shared = None
            if self._mapping is not None:
                self._mapping.close()
                self._mapping = None
            self._stream.close()
            raise
        self._worker = threading.Thread(target=self._write_loop, name="ABMSynthMidiTrace", daemon=True)
        self._worker.start()
        frequency = ctypes.c_int64()
        _clock.QueryPerformanceFrequency(ctypes.byref(frequency))
        self.record("capture_start", schema_version=1, pid=os.getpid(),
                    driver_sequence_before=self._next_driver_sequence - 1,
                    qpc_frequency=frequency.value)
        self._shared.enabled = 1

    def _write_loop(self):
        dirty=False;flushed=time.monotonic()
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    event = self._queue.get(timeout=0.05)
                except queue.Empty:
                    if dirty and time.monotonic()-flushed>=.25:
                        self._stream.flush();dirty=False;flushed=time.monotonic()
                    continue
                self._stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                dirty=True
                if time.monotonic()-flushed>=.25:
                    self._stream.flush();dirty=False;flushed=time.monotonic()
        except (OSError, TypeError, ValueError) as exc:
            self._write_error = str(exc)
            self._accepting = False
        finally:
            try:
                self._stream.close()
            except OSError as exc:
                self._write_error = str(exc)

    @property
    def write_error(self) -> str | None:
        return self._write_error

    def record(self, stage: str, **fields) -> None:
        if not self._accepting:
            return
        event = {"wall_ns": time.time_ns(), "qpc_ticks": _counter_value(),
                 "stage": stage, **fields}
        with self._lock:
            self._counts[stage] += 1
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._lock:
                self._dropped += 1

    def next_packet_id(self) -> int:
        with self._lock:
            self._packet_sequence += 1
            return self._packet_sequence

    def poll_driver(self, limit: int = 512) -> None:
        shared = self._shared
        if shared is None:
            return
        last = int(shared.next_sequence)
        if last - self._next_driver_sequence + 1 > _TRACE_CAPACITY:
            lost = last - self._next_driver_sequence + 1 - _TRACE_CAPACITY
            self._driver_overwritten += lost
            self._next_driver_sequence += lost
            self.record("driver_ring_overflow", lost=lost)
        for _ in range(min(limit, max(0, last - self._next_driver_sequence + 1))):
            sequence = self._next_driver_sequence
            entry = shared.entries[(sequence - 1) % _TRACE_CAPACITY]
            committed = int(entry.committed_sequence)
            if committed != sequence:
                if committed > sequence:
                    self._driver_overwritten += 1
                    self._next_driver_sequence += 1
                    self.record("driver_ring_overflow", lost=1)
                    continue
                break  # The producing callback has reserved, but not published, this slot.
            snapshot = {
                "driver_sequence": sequence, "driver_qpc": int(entry.counter),
                "process_id": int(entry.process_id), "thread_id": int(entry.thread_id),
                "length": int(entry.length), "outcome": _TRACE_OUTCOMES.get(int(entry.outcome), "unknown"),
                "win32_error": int(entry.error), "header_bytes": int(entry.header_bytes),
                "payload_bytes": int(entry.payload_bytes), "prefix_hex": bytes(entry.prefix).hex(" "),
            }
            if int(entry.committed_sequence) != sequence:
                continue
            self.record("driver_midi", **snapshot)
            self._next_driver_sequence += 1

    def stop(self) -> dict:
        pending_driver = 0
        if self._shared is not None:
            self._shared.enabled = 0
            # A producer may already have reserved a sequence when the user
            # clicks Stop. Let those in-flight callbacks publish before the
            # shared view is unmapped, but never wait indefinitely for a DAW.
            deadline = time.monotonic() + 0.2
            while True:
                self.poll_driver(limit=_TRACE_CAPACITY)
                pending_driver = max(0, int(self._shared.next_sequence) - self._next_driver_sequence + 1)
                if not pending_driver or time.monotonic() >= deadline:
                    break
                time.sleep(0.001)
        with self._lock:
            summary = dict(self._counts)
            dropped = self._dropped
        self.record("capture_stop", counts=summary, trace_queue_dropped=dropped,
                    driver_ring_overwritten=self._driver_overwritten,
                    driver_in_flight=pending_driver)
        self._accepting = False
        self._stop.set()
        self._worker.join(timeout=5)
        if self._shared is not None:
            self._shared = None
        if self._mapping is not None:
            self._mapping.close()
            self._mapping = None
        return {"counts": summary, "trace_queue_dropped": dropped,
                "driver_ring_overwritten": self._driver_overwritten,
                "driver_in_flight": pending_driver,
                "write_error": self._write_error,
                "writer_finished": not self._worker.is_alive()}


class AutoResetClock:
    """Arm one reset per MIDI-input idle episode."""

    def __init__(self):
        self._lock = threading.Lock()
        self.delay_seconds = 0
        self.last_input = None
        self.fired = False
        self.native_execution = None
        self._native_completions = 0
        self._native_observed = 0

    def configure(self, delay_seconds: int) -> None:
        delay_seconds = max(0, min(600, int(delay_seconds)))
        native = self.native_execution
        if native is not None and not native.configure_auto_reset(delay_seconds):
            raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_execution_idle_configure'))
        with self._lock:
            self.delay_seconds = delay_seconds
            self.last_input = None
            self.fired = False

    def record_input(self, now: float | None = None) -> None:
        with self._lock:
            self.last_input = time.monotonic() if now is None else now
            self.fired = False

    def consume_idle_reset(self, now: float | None = None) -> bool:
        instant = time.monotonic() if now is None else now
        with self._lock:
            if (self.native_execution is not None or self.delay_seconds == 0 or self.last_input is None or self.fired
                    or instant - self.last_input < self.delay_seconds):
                return False
            self.fired = True
            return True

    def bind_native(self, execution):
        if not getattr(execution,'auto_reset_ready',False):return False
        if self.native_execution is not None:raise ValueError('idle execution already bound')
        if not execution.configure_auto_reset(self.delay_seconds):raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_execution_idle_configure admission'))
        with self._lock:self.native_execution=execution
        return True

    def unbind_native(self, execution):
        with self._lock:
            if self.native_execution is execution:
                self.native_execution=None;self.last_input=None;self.fired=False

    def native_completed(self):
        with self._lock:self._native_completions+=1

    def consume_native_completion(self):
        with self._lock:
            changed=self._native_completions!=self._native_observed
            self._native_observed=self._native_completions
            return changed


class ChannelSends:
    """Own CC#91/93 state independently of engine effect Level events.

    The global floor is a native 0..127 channel-send input. The two-stage
    integer formula is retained as recovered from BMSynth; allocated-channel reset
    seeds and per-channel CC updates share the same emitter.
    """

    def __init__(self, bassmidi, stream_getter, *, bass_backend=None, native_apply=False):
        self.bassmidi = bassmidi
        self.stream_getter = stream_getter
        self.global_reverb = 0
        self.global_chorus = 0
        self.native_domain=NativeMidiSendDomain()
        self.bass_backend=bass_backend;self.pending_native_batch=None;self.last_native_batch=None
        # Candidate opt-in only. DLL presence is NOT admission: individual
        # Dense CC traffic needs native execution without per-event Python barriers.
        self.native_apply=bool(native_apply)
        self.default_reverb = 40
        self.channels = lambda: range(MIDI_CHANNELS_PER_PORT)
        self.channel_reverb = [self.default_reverb] * MIDI_CHANNEL_LIMIT
        self.channel_chorus = [0] * MIDI_CHANNEL_LIMIT
        self.on_emit = None

    @staticmethod
    def calculate(global_scale, channel_amount, global_base=0):
        """BMSynth's two-stage 127-domain integer send calculation."""
        global_scale = max(0, min(127, int(global_scale)))
        channel_amount = max(0, min(127, int(channel_amount)))
        global_base = max(0, min(127, int(global_base)))
        base = global_base + ((127 - global_base) * global_scale) // 127
        base = max(0, min(127, base))
        return min(127, base + ((127 - base) * channel_amount) // 127)

    def _emit(self, channel, event, scale, amount):
        if self.pending_native_batch is not None:self.flush_pending()
        stream = self.stream_getter()
        if stream:
            if self.native_apply and 0<=channel<MIDI_CHANNEL_LIMIT:
                floor=max(0,min(127,int(scale)));request=max(0,min(127,int(amount)))
                batch=self.native_domain.apply_native(self.bassmidi,self.bass_backend,stream,(channel,),direct=(channel,event,floor,request))
                if batch is not None:
                    self._consume_native_batch(batch)
                    return batch[0]==1 and bool(batch[1][0][4])
            raw = self.calculate(scale, amount)
            accepted = bool(self.bassmidi.BASS_MIDI_StreamEvent(stream, channel, event, raw))
            if accepted and self.native_domain.requested is not None:
                self.native_domain.set_request(channel,event,max(0,min(127,int(amount))))
            if accepted and self.on_emit is not None:
                self.on_emit(channel, event, raw)
            return accepted
        return False

    def _replay_native_batch(self,rows):
        for channel,event,request,raw,accepted,_error in rows:
            values=self.channel_reverb if event==MIDI_EVENT_REVERB else self.channel_chorus
            values[channel]=request # Accepted native request may be ahead of the journal.
            if accepted and self.on_emit is not None:self.on_emit(channel,event,raw)

    def _consume_native_batch(self,batch):
        self.last_native_batch=batch
        result,rows,status=batch
        if result==-2:
            self.pending_native_batch=rows
            raise RuntimeError(tr('runtime.midi_filter_config_failed',error=status[1]))
        if result==-1:raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_send_domain_apply ABI'))
        if result==1:
            self.pending_native_batch=rows
            self.flush_pending() # Retain metadata on replay failure; never resend audio.

    def flush_pending(self):
        self.native_domain.retry_batch()
        if self.pending_native_batch is not None:
            self._replay_native_batch(self.pending_native_batch)
            self.pending_native_batch=None

    def apply_all(self):
        self.flush_pending()
        if not self.stream_getter():
            return
        batch=self.native_domain.apply_native(self.bassmidi,self.bass_backend,self.stream_getter(),self.channels()) if self.native_apply else None
        if batch is not None:
            self._consume_native_batch(batch)
            return # Lock failure records no attempts; never certifies emission.
        for channel in self.channels():
            self._emit(channel, MIDI_EVENT_REVERB, self.global_reverb, self.channel_reverb[channel])
            self._emit(channel, MIDI_EVENT_CHORUS, self.global_chorus, self.channel_chorus[channel])

    def set_global_reverb(self, value):
        self.global_reverb = max(0, min(127, int(round(float(value)))))
        self.native_domain.set_floor(MIDI_EVENT_REVERB,self.global_reverb)
        self.apply_all()

    def set_global_chorus(self, value):
        self.global_chorus = max(0, min(127, int(round(float(value)))))
        self.native_domain.set_floor(MIDI_EVENT_CHORUS,self.global_chorus)
        self.apply_all()

    def set_default_reverb(self, value):
        self.default_reverb = max(0, min(127, int(round(float(value)))))

    def reset_channels(self, *, apply=True):
        self.flush_pending()
        self.channel_reverb = [self.default_reverb] * MIDI_CHANNEL_LIMIT
        self.channel_chorus = [0] * MIDI_CHANNEL_LIMIT
        for channel in range(MIDI_CHANNEL_LIMIT):
            self.native_domain.set_request(channel,MIDI_EVENT_REVERB,self.default_reverb)
            self.native_domain.set_request(channel,MIDI_EVENT_CHORUS,0)
        if apply:
            self.apply_all()

    def receive_cc91(self, channel, value):
        if self.pending_native_batch is not None:self.flush_pending()
        previous = self.channel_reverb[channel]
        self.channel_reverb[channel] = value
        if not self.stream_getter():self.native_domain.set_request(channel,MIDI_EVENT_REVERB,max(0,min(127,int(value))))
        accepted = self._emit(channel, MIDI_EVENT_REVERB, self.global_reverb, value)
        if not accepted and self.stream_getter():
            self.channel_reverb[channel] = previous
        return accepted

    def receive_cc93(self, channel, value):
        if self.pending_native_batch is not None:self.flush_pending()
        previous = self.channel_chorus[channel]
        self.channel_chorus[channel] = value
        if not self.stream_getter():self.native_domain.set_request(channel,MIDI_EVENT_CHORUS,max(0,min(127,int(value))))
        accepted = self._emit(channel, MIDI_EVENT_CHORUS, self.global_chorus, value)
        if not accepted and self.stream_getter():
            self.channel_chorus[channel] = previous
        return accepted


def midi_activity_strength_at(state,now,*,held_strength=58,held_rate=150.0,release_rate=500.0):
    """Shared MIDI-activity display decay; never changes audio sample values."""
    active,phase_at,initial=state
    elapsed=max(0.0,now-phase_at)
    if active:
        if initial<=60:return initial
        return max(held_strength,initial-held_rate*elapsed)
    return max(0.0,initial-release_rate*elapsed)


_CHORD_PITCH_NAMES=('C','C#','D','Eb','E','F','F#','G','Ab','A','Bb','B')


def _pychord_vocabulary():
    """Compile the installed PyChord1.4.0 public QualityManager,not a local dictionary.

    Fold compound intervals into pitch classes:9ths/11ths/13ths may be voiced
    anywhere. Alias choice affects spelling only,not intervals or recognition.
    Construction happens at import/startup,never in an audio callback.
    """
    from pychord import QualityManager
    preferred={'maj7','maj9','maj13','m7b5','7b5','7#5','7b9','7#9',
               '9b5','9#5','13b9','13#9','13#11'}
    groups={}
    for name,quality in QualityManager().get_qualities().items():
        intervals=tuple(sorted({value%12 for value in quality.components}))
        groups.setdefault(intervals,[]).append(name)
    return tuple((next((name for name in names if name in preferred),names[0]),intervals)
                 for intervals,names in groups.items())


MIDI_CHORD_INTERVALS=_pychord_vocabulary()
_MIDI_CHORD_LOOKUP={}
for _root in range(12):
    for _index,(_suffix,_intervals) in enumerate(MIDI_CHORD_INTERVALS):
        _mask=sum(1<<((_root+interval)%12) for interval in _intervals)
        _MIDI_CHORD_LOOKUP.setdefault(_mask,[]).append((_index,_root,_suffix))


def midi_chord_candidates(keys):
    """PyChord-defined voicings with observed bass;independent of wall time."""
    keys=tuple(keys)
    if not keys or any(type(key) is not int or not 0<=key<128 for key in keys):return ()
    pitches=frozenset(key%12 for key in keys)
    if len(pitches)<2:return ()
    bass=min(keys)%12;matches=[];mask=sum(1<<pitch for pitch in pitches)
    for index,root,suffix in _MIDI_CHORD_LOOKUP.get(mask,()):
        name=_CHORD_PITCH_NAMES[root]+suffix
        if root!=bass:name+='/'+_CHORD_PITCH_NAMES[bass]
        matches.append((root!=bass,index,root,name))
    return tuple(dict.fromkeys(row[-1] for row in sorted(matches)))


@dataclass(frozen=True)
class MidiChordReading:
    symbols: tuple=()
    approximate: bool=False

    @property
    def text(self):return self.symbols[0] if self.symbols else ''


@dataclass(frozen=True)
class MidiChordEventBatch:
    serial: int
    rows: tuple
    notes: tuple
    known: bool
    history_complete: bool
    source: int=0


class MidiChordEventSource:
    """Read-only derived event journal on metadata workers,never the audio worker.

    8192 rows bounds observation storage,not MIDI/audio admission. Oversized
    metadata batches invalidate only this observer;playback is never retried.
    A known all-channel clear re-establishes a reliable empty reference.
    """
    capacity=8192

    def __init__(self):
        self.lock=threading.Lock();self.notes={};self.rows=deque(maxlen=self.capacity)
        self.serial=0;self.known=True

    def begin_generation(self):
        with self.lock:
            self.notes.clear();self.rows.clear();self.serial=0;self.known=True

    def submit(self,actions):
        if not actions:return
        with self.lock:
            if len(actions)>self.capacity:
                self.known=False;self.notes.clear();self.rows.clear();self.serial+=1;return
            for packed,channel,_overlap,qpc in actions:
                status,key,velocity=packed&240,(packed>>8)&255,(packed>>16)&255
                if channel==-1:
                    self.notes.clear();self.known=True
                    self.rows.clear()
                elif not 0<=channel<128 or status in (128,144) and key>=128:
                    self.known=False
                elif status==176:
                    for address in tuple(self.notes):
                        if address[0]==channel:self.notes.pop(address)
                elif status in (128,144):
                    address=(channel,key);count=self.notes.get(address,0)
                    if status==144 and velocity:self.notes[address]=count+1
                    elif count>1:self.notes[address]=count-1
                    else:self.notes.pop(address,None)
                else:continue
                self.serial+=1;self.rows.append((self.serial,packed,channel,int(qpc)))
    
    def snapshot(self,cursor):
        with self.lock:
            complete=not self.rows or cursor>=self.rows[0][0]-1
            return MidiChordEventBatch(self.serial,
                tuple(row for row in self.rows if row[0]>cursor),tuple(self.notes.items()),
                self.known,complete,id(self))


class MidiChordTracker:
    """Event-driven PyChord recognition. No ms window,tempo guess or debounce.

    GUI refresh may skip intermediate faces,but every admitted journal row is
    replayed in order. Uniformly scaling event times cannot change names.
    Current voicing identification does not infer a song's intended harmony.
    """
    def __init__(self):self.reset()

    def reset(self):
        self.identity=None;self.cursor=0;self.notes={};self.reading=MidiChordReading()
        self.last_transitions=();self.source=None;self.needs_resync=False

    def observe_events(self,batch,*,identity,excluded=()):
        if self.identity!=identity:self.reset();self.identity=identity
        self.last_transitions=()
        if batch is None or not batch.known:
            self.notes={};self.reading=MidiChordReading();self.needs_resync=True
            if batch is not None:self.cursor=batch.serial
            return self.reading
        excluded=frozenset(excluded);transitions=[]
        if self.needs_resync or not batch.history_complete or self.source is not None and self.source!=batch.source:
            # Missing history cannot be reconstructed. Final counts are atomic.
            self.notes=dict(batch.notes)
        else:
            # Aggregate once per batch,not all channel/key pairs for every row.
            # Same-key overlaps change counts,not the chord pitch membership.
            counts=[0]*128
            for (ch,key),count in self.notes.items():
                if ch not in excluded:counts[key]+=count
            for serial,packed,channel,qpc in batch.rows:
                status,key,velocity=packed&240,(packed>>8)&255,(packed>>16)&255
                changed=False
                if channel==-1:
                    changed=any(counts);counts=[0]*128;self.notes.clear()
                elif status==176:
                    for address in tuple(self.notes):
                        if address[0]!=channel:continue
                        count=self.notes.pop(address);pitch=address[1]
                        if channel not in excluded:
                            counts[pitch]-=count
                            changed|=not counts[pitch]
                elif 0<=key<128:
                    address=(channel,key);count=self.notes.get(address,0)
                    if status==144 and velocity:
                        self.notes[address]=count+1
                        if channel not in excluded:
                            changed=counts[key]==0;counts[key]+=1
                    elif count:
                        if count>1:self.notes[address]=count-1
                        else:self.notes.pop(address)
                        if channel not in excluded:
                            counts[key]-=1;changed=counts[key]==0
                if changed:
                    reading=MidiChordReading(midi_chord_candidates(tuple(
                        key for key,count in enumerate(counts) if count)))
                    if reading!=self.reading:transitions.append((serial,qpc,reading))
                    self.reading=reading
        self.cursor=batch.serial;self.source=batch.source;self.needs_resync=False
        self.reading=MidiChordReading(midi_chord_candidates(tuple(sorted({
            key for (ch,key),count in self.notes.items() if count and ch not in excluded}))))
        self.last_transitions=tuple(transitions)
        return self.reading


def midi_chord_source_is_effect(bank,program,*,standard=True,unpitched=()):
    """Source identity,never incoming address/name substring/acoustic guess.

    GM bank0:Reverse Cymbal119 and Sound Effects120..127 (zero-based).
    Synth Effects96..103 and pitched percussion remain harmonic candidates.
    Custom/SFZ sources opt out of GM address semantics. Explicit SF2/SF3
    all-zone unpitched declarations apply independently of bank/program.
    """
    return (bank,program) in unpitched or bool(standard and bank==0 and 119<=program<=127)


def _harmony_templates():
    """PyChord vocabulary plus explicit core/colour separation,not new names."""
    rows=[]
    cores=((0,4,7),(0,3,7),(0,3,6),(0,4,8),(0,2,7),(0,5,7),(0,4,6),(0,3,8))
    for root in range(12):
        for suffix,intervals in MIDI_CHORD_INTERVALS:
            if len(intervals)<3:continue # Exact dyads use _dyad_reading after richer context,not temporal triad scoring.
            core=next((core for core in cores if set(core)<=set(intervals)),intervals)
            pitches=tuple((root+i)%12 for i in intervals)
            core=tuple((root+i)%12 for i in core)
            rows.append((_CHORD_PITCH_NAMES[root]+suffix,root,pitches,core,tuple(p for p in pitches if p not in core)))
    return tuple(rows)


MIDI_HARMONY_TEMPLATES=_harmony_templates()
MIDI_HARMONY_EQUIVALENTS={}
for _i,_row in enumerate(MIDI_HARMONY_TEMPLATES):
    MIDI_HARMONY_EQUIVALENTS.setdefault(frozenset(_row[2]),[]).append(_i)


class MidiHarmonyTracker:
    """Causal duration/context decoder,independent of raw voicing matching.

    All temporal domains are ratios of observed onset spacing/note duration,
    not fixed ms or an assumed BPM. Counts/history are observation resources.
    Scores are product heuristics,not calibrated musical probabilities.
    """
    HISTORY_CAPACITY=512
    ONSET_CAPACITY=64
    HISTORY_UNITS=4.
    TRANSITION_MARGIN=.12
    CHECKPOINT_BUDGET=32
    # Observation freshness,not a musical grouping interval. Matches the
    # telemetry's existing one-second stale-observation fence.
    OBSERVATION_GRACE_NS=1_000_000_000

    def __init__(self):
        self.raw=MidiChordTracker();self._clear_context()
        import numpy as np
        self._np=np
        self._masks=np.array([[p in row[2] for p in range(12)] for row in MIDI_HARMONY_TEMPLATES],dtype=float)
        self._sizes=self._masks.sum(axis=1)
        self._cores=np.array([[p in row[3] for p in range(12)] for row in MIDI_HARMONY_TEMPLATES],dtype=bool)
        self._medians=((self._cores.sum(axis=1)-1)//2).astype(int)
        # Gather only actual core pitches,not twelve padded pitch classes.
        # Padding preserves the lower median for any future variable-size core.
        core_width=max(len(row[3]) for row in MIDI_HARMONY_TEMPLATES)
        self._core_indices=np.array([(*row[3],*(12 for _ in range(core_width-len(row[3]))))
            for row in MIDI_HARMONY_TEMPLATES],dtype=int)
        self._triads=tuple({frozenset(row[3]) for row in MIDI_HARMONY_TEMPLATES if len(row[2])==3})

    @property
    def identity(self):return self.raw.identity

    @property
    def cursor(self):return self.raw.cursor

    def _clear_context(self):
        self.live={};self.history=deque(maxlen=self.HISTORY_CAPACITY)
        # Pattern evidence has a per-part horizon. Share ended-note tuples,
        # not the much shorter global harmonic window;still bounded512 rows.
        self.role_history=deque(maxlen=self.HISTORY_CAPACITY)
        self.onsets=deque(maxlen=self.ONSET_CAPACITY);self.durations=deque(maxlen=self.ONSET_CAPACITY)
        self.reading=MidiChordReading();self.accepted=None;self.last_time=0;self.last_release=None
        self.event_reading=MidiChordReading()
        self.pending_at=None
        self.age_deadlines=()
        self.excluded=frozenset();self.source=None
        self.repeat_support={}
        self.attack_serial=0;self.analysed_attack=0;self.last_attack=0;self.last_transitions=()
        self.supported_at=None;self.observed_at=None
        self.role_weights={}
        self._role_plan=None
        self._unit_plan=None
        self.dyad=None
        self.bass_pitch=None;self.bass_root=None;self.bass_proposal=None

    def reset(self):self.raw.reset();self._clear_context()

    def _unit(self):
        signature=(tuple(self.onsets),tuple(self.durations))
        if self._unit_plan is not None and self._unit_plan[0]==signature:return self._unit_plan[1]
        gaps=[b-a for a,b in zip(self.onsets,tuple(self.onsets)[1:]) if b>a]
        durations=sorted(value for value in self.durations if value>0)
        reference=durations[len(durations)//4] if durations else max(gaps,default=0)
        # Context follows typical attack spacing,not the shortest common
        # integer subdivision. A sparse 1/8 fill must not divide every past
        # interval and history lifetime by eight. Gates reject co-onset jitter.
        useful=sorted(gap for gap in gaps if gap>=reference/16)
        if useful:reference=useful[(len(useful)-1)//2]
        result=float(reference) or None
        self._unit_plan=(signature,result)
        return result

    def _event(self,packed,ch,stamp):
        status,key,velocity=packed&240,(packed>>8)&255,(packed>>16)&255
        self.last_time=max(self.last_time,stamp)
        if ch==-1:
            excluded=self.excluded;self._clear_context();self.excluded=excluded;self.last_time=stamp;return
        if status==176:
            affected=any(c==ch for c,_key in self.live) or any(row[0]==ch for row in self.history)
            self.live={a:v for a,v in self.live.items() if a[0]!=ch}
            self.history=deque((row for row in self.history if row[0]!=ch),maxlen=self.HISTORY_CAPACITY)
            self.role_history=deque((row for row in self.role_history if row[0]!=ch),maxlen=self.HISTORY_CAPACITY)
            if affected:
                self.accepted=None;self.reading=MidiChordReading();self.supported_at=None;self.repeat_support.clear()
                self.dyad=None
                self.bass_pitch=None;self.bass_root=None
            if not self.live and not self.history:
                self.accepted=None;self.reading=MidiChordReading();self.onsets.clear();self.durations.clear()
            self._track_bass(stamp);return
        if ch in self.excluded or not 0<=key<128:return
        address=(ch,key);old=self.live.get(address)
        if status==144 and velocity:
            if old:self.live[address]=(old[0]+1,old[1])
            else:
                self.live[address]=(1,stamp);self.attack_serial+=1;self.last_attack=stamp
            if not self.onsets or stamp>self.onsets[-1]:self.onsets.append(stamp)
            self.last_release=None
        elif old:
            if old[0]>1:self.live[address]=(old[0]-1,old[1])
            else:
                self.live.pop(address);ended=(ch,key,old[1],stamp)
                self.history.append(ended);self.role_history.append(ended)
                if stamp>old[1]:self.durations.append(stamp-old[1])
                if not self.live:self.last_release=stamp
        self._track_bass(stamp)

    def _track_bass(self,stamp):
        """Lowest-note frontier changes at event time,not at reader time."""
        key=min((key for _ch,key in self.live),default=None)
        pitch=None if key is None else key%12
        if pitch is None:self.bass_proposal=None
        elif self.bass_proposal is None or self.bass_proposal[0]!=pitch:
            self.bass_proposal=(pitch,stamp)

    def _with_bass(self,reading,now):
        """Preserve standard slash notation separately from harmonic scoring.

        Initial/new-root exact voicings publish their current bass. Later
        lowest-note changes need one observed context unit and polyphonic
        evidence;brief walking tones/single-note arpeggios do not chase text.
        Uses the existing event deadlines. No fixed-ms/UI last-text timer.
        """
        if not reading.symbols:
            self.bass_pitch=None;self.bass_root=None
            return reading
        names=tuple(symbol.split('/')[0] for symbol in reading.symbols)
        def root(symbol):return symbol[:2] if symbol[1:2] in ('#','b') else symbol[:1]
        main_root=root(names[0]);keys=tuple(key for _ch,key in self.live)
        if len({key%12 for key in keys})>=2 and self.bass_proposal is not None:
            pitch,since=self.bass_proposal;unit=self._unit()
            if self.bass_pitch is None or main_root!=self.bass_root or unit is None or now-since>=unit:
                self.bass_pitch=pitch
        self.bass_root=main_root
        if self.bass_pitch is None:return MidiChordReading(names,reading.approximate)
        bass=_CHORD_PITCH_NAMES[self.bass_pitch]
        return MidiChordReading(tuple(name if root(name)==bass else name+'/'+bass for name in names),reading.approximate)

    def _extra_supported(self,pitch,core,records,unit,now,context=None):
        # One inference owns this optional index/cache. No result survives
        # an event,age deadline or isolated presentation query.
        query=(pitch,frozenset(core))
        if context is not None and query in context[1]:return context[1][query]
        entries=(context[0].get(pitch,()) if context is not None else
            [row for row in records if row[1]%12==pitch and self.role_weights.get(row[:3],1.)>=.95])
        if not entries:
            if context is not None:context[1][query]=False
            return False
        if len(entries)>=2 and max(r[2] for r in entries)-min(r[2] for r in entries)>=unit*2:
            self.repeat_support[pitch]=max(r[2] for r in entries)
        supported=(pitch in self.repeat_support and now-self.repeat_support[pitch]<=unit*8 or
            any(end-start>=unit*1.5 for _ch,_key,start,end in entries))
        if not supported:
            for ch,_key,start,_end in entries:
                peers={key%12 for c,key,attack,_e in records if c==ch and key%12 in core and abs(attack-start)<=unit/8
                    and self.role_weights.get((c,key,attack),1.)>=.95}
                if len(peers)>=2:supported=True;break
        if context is not None:context[1][query]=supported
        return supported

    def _role_weights(self,records,unit):
        """Soft causal upper-line prior,not voice labels or fixed pitch zones.

        Duration/step-motion ideas:Chorder,Hu/Arthur2021 and Temperley/Sleator.
        Independent implementation/product ratios;no upstream model or code.
        Require a separate complete accompaniment before discounting anything.
        """
        # Structure is constant between attacks/releases and horizon crossings.
        # Live-note duration still changes on every query. Cache an immutable
        # plan,not final weights;presentation views cannot mutate its owner.
        now=max((row[3] for row in records),default=0)
        # Caller appends live rows after immutable ended rows. Reuse those
        # tuples directly;do not rebuild512 normalized tuples per deadline.
        held_count=len(self.live);past=len(records)-held_count
        ended=records[:past];held=records[past:]
        live=frozenset(row[:3] for row in held)
        signature=(unit,ended,tuple(row[:3] for row in held))
        cached=self._role_plan
        if cached is not None and cached[0]==signature and cached[1]<=now<cached[2]:
            return {address:1.-.75*strength*max(0.,min(1.,(part_unit*1.5-max(0,(now if held else end)-address[2]))/(part_unit*.5)))
                for address,end,held,part_unit,strength in cached[3]}
        groups={}
        for row in records:groups.setdefault(row[0],[]).append(row)
        lanes=[]
        for rows in groups.values():
            pitches=sorted({row[1] for row in rows})
            gaps=[(b-a,b) for a,b in zip(pitches,pitches[1:]) if b-a>=12]
            split=max(gaps,default=(0,None))[1]
            lower=[row for row in rows if split is not None and row[1]<split]
            if len({row[1]%12 for row in lower})>=3:
                lanes.extend((lower,[row for row in rows if row[1]>=split]))
            else:lanes.append(rows)
        result={};plan=[];expires=math.inf
        ended_times=sorted(row[3] for row in ended)
        for lane in lanes:
            # Interleaving parts can halve global onset spacing without
            # speeding up either melody. Derive this lane's own pattern span.
            starts=sorted({row[2] for row in lane})
            gaps=sorted(b-a for a,b in zip(starts,starts[1:]) if b-a>=unit/16)
            if not gaps:continue
            part_unit=max(unit,gaps[(len(gaps)-1)//2])
            # Expire after inclusive horizon/strict overlap comparisons cross.
            # Other lanes also contribute accompaniment rows at this horizon.
            position=bisect.bisect_left(ended_times,now-part_unit*self.HISTORY_UNITS)
            if position<len(ended_times):
                expires=min(expires,math.floor(ended_times[position]+part_unit*self.HISTORY_UNITS)+1)
            for at in starts:
                crossing=math.floor(at+part_unit/4)+1
                if crossing>now:expires=min(expires,crossing)
            cutoff=now-part_unit*self.HISTORY_UNITS
            lane=[row for row in lane if row[3]>=cutoff]
            attacks={}
            for row in lane:
                attacks.setdefault(row[2],set()).add(row[1])
            if len(attacks)<4 or len({row[1] for row in lane})<2:continue
            ordered=sorted(attacks.items())
            # Multiple distinct simultaneous pitch classes are harmonic,
            # even when the entire accompaniment is in a high register.
            if sum(len({key%12 for key in keys})==1 for _at,keys in ordered)/len(ordered)<.8:continue
            # Most accompaniment lanes exit above. Only possible single-line
            # melodies need the more expensive overlap/end-time index.
            ends={}
            for _ch,key,at,end in lane:
                address=(at,key%12);ends[address]=max(ends.get(address,end),end)
            active={};polyphonic=0
            for at,keys in ordered:
                active={pitch:end for pitch,end in active.items() if end>at+part_unit/4}
                pitches={key%12 for key in keys}
                polyphonic+=bool(set(active)-pitches)
                for pitch in pitches:active[pitch]=max(active.get(pitch,at),ends[(at,pitch)])
            if polyphonic/len(ordered)>.2:continue
            melody=[sorted(keys)[len(keys)//2] for _at,keys in ordered]
            steps=sum(0<abs(b-a)<=2 for a,b in zip(melody,melody[1:]))/(len(melody)-1)
            if steps<.6:continue
            addresses={row[:3] for row in lane}
            median=sorted(melody)[len(melody)//2]
            # Find a complete lower accompaniment,not a median dominated by
            # other upper melodies. Register alone never establishes a role.
            others=[row for row in records if row[:3] not in addresses and row[3]>=cutoff and row[1]<=median-7]
            if not others:continue
            support={row[1]%12 for row in others}
            if not any(core<=support for core in self._triads):continue
            other_keys=sorted(row[1] for row in others)
            # Register is relative to this performance,never C4/C5 or GM Inst.
            separation=median-other_keys[len(other_keys)//2]
            if separation<7:continue
            strength=min(1.,steps/.8)*min(1.,separation/12)*min(1.,(len(ordered)-1)/5)
            for row in lane:
                # A long high tone remains legitimate extension evidence.
                shortness=max(0.,min(1.,(part_unit*1.5-max(0,row[3]-row[2]))/(part_unit*.5)))
                result[row[:3]]=1.-.75*strength*shortness
                plan.append((row[:3],row[3],row[:3] in live and row[3]==now,part_unit,strength))
        self._role_plan=(signature,now,expires,tuple(plan))
        return result

    def _bass_support(self,records,unit,now,start):
        """Relative low-register duration prior;not bass=root enforcement."""
        if not records:return (0.,)*12
        keys=sorted({row[1] for row in records})
        ceiling=min(keys[0]+7,keys[(len(keys)-1)//4])
        weights=[0.]*12;tau=unit*2;parts={}
        for ch,key,left,right in records:
            if key>ceiling:continue
            left=max(left,start);right=min(right,now)
            if right<left:continue
            weight=tau*(math.exp((right-now)/tau)-math.exp((left-now)/tau))/unit
            if right==now and (ch,key) in self.live:weight=max(weight,1/8)
            address=(ch,key%12);parts[address]=parts.get(address,0.)+weight
        for (_ch,pitch),weight in parts.items():weights[pitch]=max(weights[pitch],weight)
        total=sum(weights)
        return tuple(w/total for w in weights) if total else (0.,)*12

    def _evidence(self,records,unit,now,start):
        evidence=self._np.zeros((128,12),float);tau=unit*2
        for ch,key,left,right in records:
            row_on=left
            left=max(left,start);right=min(right,now)
            if right<left:continue
            weight=tau*(math.exp((right-now)/tau)-math.exp((left-now)/tau))
            if right==now and (ch,key) in self.live:weight=max(weight,unit/8)
            evidence[ch,key%12]+=weight/unit*self.role_weights.get((ch,key,row_on),1.)
        return evidence.max(axis=0)

    def _dyad_reading(self,keys,now,unit):
        """Exact library dyads only,after richer harmony/context has priority.

        Do not aggregate two sequential single notes into a new dyad. A
        previously observed dyad may bridge a short missing-voice interval,
        but that retention cannot renew its own evidence lifetime.
        """
        pitches=frozenset(key%12 for key in keys)
        if len(pitches)==2:
            symbols=tuple(dict.fromkeys(symbol.split('/')[0] for symbol in midi_chord_candidates(keys)))
            if symbols:
                reading=MidiChordReading(symbols)
                self.dyad=(pitches,reading,now)
                return reading
        if self.dyad is not None and unit:
            previous,reading,supported=self.dyad
            if pitches and (pitches<=previous or previous<=pitches) and 0<=now-supported<=unit*self.HISTORY_UNITS:
                return MidiChordReading(reading.symbols,approximate=True)
        self.dyad=None
        return MidiChordReading()

    def _dyad_boundary_ready(self,keys,now,unit,previous):
        current={key%12 for key in keys}
        attacks=tuple(on for _count,on in self.live.values())
        if max(attacks)-min(attacks)>unit/8:return False
        # Two genuinely new pitches can announce a new chord immediately.
        # A shared-tone pair can be just a handoff/short upper-line fragment.
        # Require support before discarding the established richer context.
        return len(current-previous)==2 or now-max(attacks)>=unit

    def _infer(self,now):
        self.reading=self._with_bass(self._infer_harmony(now),now)
        return self.reading

    def _infer_harmony(self,now):
        unit=self._unit()
        keys=tuple(key for (_ch,key) in self.live)
        current=set(key%12 for key in keys)
        if not unit:
            candidates=tuple(symbol.split('/')[0] for symbol in midi_chord_candidates(keys))
            full=next((i for symbol in candidates for i,row in enumerate(MIDI_HARMONY_TEMPLATES) if row[0]==symbol),None)
            if full is not None:
                self.accepted=full;self.reading=MidiChordReading(tuple(dict.fromkeys(candidates)))
                self.supported_at=now;self.dyad=None
            elif self.accepted is None:self.reading=self._dyad_reading(keys,now,unit)
            elif not keys:self.reading=MidiChordReading();self.accepted=None;self.dyad=None
            return self.reading
        previous=(frozenset(MIDI_HARMONY_TEMPLATES[self.accepted][2]) if self.accepted is not None
            else self.dyad[0] if self.dyad is not None else None)
        if self.accepted is None and self.dyad is not None and len(current)==3:
            novel=current-previous
            attacks=tuple(on for _count,on in self.live.values())
            coherent=max(attacks)-min(attacks)<=unit/8
            if coherent or previous<current and all(any(key%12==p and now-on>=unit/8 for (_ch,key),(_count,on) in self.live.items()) for p in novel):
                full=next((i for i,row in enumerate(MIDI_HARMONY_TEMPLATES) if len(row[2])==3 and set(row[2])==current),None)
                if full is not None:
                    if not previous<=current:self.history.clear();self.repeat_support.clear()
                    self.accepted=full;self.supported_at=now;self.dyad=None
                    self.reading=MidiChordReading((MIDI_HARMONY_TEMPLATES[full][0],));return self.reading
        if previous is not None and len(current)==2 and not current<=previous:
            ready=self._dyad_boundary_ready(keys,now,unit,previous)
            supported=self.supported_at if self.accepted is not None else self.dyad[2] if self.dyad is not None else None
            if (not ready and current.intersection(previous) and supported is not None
                    and 0<=now-supported<=unit*self.HISTORY_UNITS):
                self.reading=MidiChordReading(self.reading.symbols,approximate=True);return self.reading
            if ready and midi_chord_candidates(keys):
                # A qualified new simultaneous dyad is a real segment change.
                # Old released tones must not reappear as an invented sixth/
                # ninth chord later. Subsets of an old triad take no fast path.
                self.reading=self._dyad_reading(keys,now,unit);self.accepted=None;self.supported_at=None
                self.history.clear();self.repeat_support.clear();return self.reading
        boundaries=[]
        if self.accepted is not None and len(current)>=3:
            previous=MIDI_HARMONY_TEMPLATES[self.accepted];old_core=set(previous[3]);bass=min(keys)%12
            boundaries=[i for i,row in enumerate(MIDI_HARMONY_TEMPLATES)
                if len(row[2])==3 and not row[4] and set(row[3])<=current
                and len(current-set(row[3]))<=1 and bass in row[3]
                and (len(set(row[3])-old_core)>=2 or row[1]!=previous[1] and row[1]==bass and not old_core<=current or
                     row[1]==previous[1] and bool(set(row[3])-old_core) and not old_core<=current and all(
                         any(key%12==p and now-on>=unit for (_ch,key),(_count,on) in self.live.items())
                         for p in set(row[3])-old_core))]
            # A boundary is a proposal,not a committed state change. Evaluate
            # it against intact evidence before retiring the old segment.
        if not self.live and self.last_release is not None:
            rhythmic=bool(self.durations and self.onsets and any(
                b-a>=min(self.durations)/4 for a,b in zip(self.onsets,tuple(self.onsets)[1:])))
            if not rhythmic or now-self.last_release>=unit:
                self.history.clear();self.role_history.clear();self.repeat_support.clear();self.reading=MidiChordReading();self.accepted=None;self.dyad=None;return self.reading
            return self.reading # A release tail must not invent a new harmony.
        start=now-unit*self.HISTORY_UNITS
        while self.history and self.history[0][3]<start:self.history.popleft()
        records=tuple(self.history)+tuple((ch,key,on,now) for (ch,key),(_count,on) in self.live.items())
        role_records=tuple(self.role_history)+tuple((ch,key,on,now) for (ch,key),(_count,on) in self.live.items())
        self.role_weights=self._role_weights(role_records,unit)
        support_entries={}
        for row in records:
            if self.role_weights.get(row[:3],1.)>=.95:support_entries.setdefault(row[1]%12,[]).append(row)
        support_context=(support_entries,{})
        if self.accepted is not None:
            previous=MIDI_HARMONY_TEMPLATES[self.accepted]
            for pitch in previous[4]:self._extra_supported(pitch,previous[3],records,unit,now,support_context)
        weights=self._evidence(records,unit,now,start);peak=float(weights.max());total=float(weights.sum())
        if not total:self.reading=MidiChordReading();self.accepted=None;return self.reading
        covered=self._masks@weights
        scores=(1.7*covered-.7*total)/total-.025*(self._sizes-3)
        bass=min(keys)%12 if keys else None
        bass_support=self._bass_support(records,unit,now,start)
        anchor=min((row[1] for row in records if row[3]>=start),default=None)
        anchor=None if anchor is None else anchor%12
        eligible=[]
        ordered=self._np.sort(self._np.append(weights,self._np.inf)[self._core_indices],axis=1)
        references=ordered[self._np.arange(len(ordered)),self._medians]
        potential=self._np.flatnonzero((ordered[:,0]>=self._np.maximum(peak*.04,references*.15))&(covered/total>=.6))
        strict=set(map(int,potential))
        old=self.accepted
        # A complete new triad over its own current bass qualifies from live
        # evidence. Old-segment duration must not veto a genuine chord change.
        qualified={i for i in boundaries if MIDI_HARMONY_TEMPLATES[i][1]==bass}
        segment_start=None
        if (old is not None and bass!=MIDI_HARMONY_TEMPLATES[old][1]
                and not set(MIDI_HARMONY_TEMPLATES[old][3])<=current
                and min(keys)<=min(row[1] for row in records)):
            # A new bass can introduce a distributed triad too. Evaluate its
            # segment locally,without destroying the still-established one.
            bass_key=min(keys)
            segment_start=min((row[2] for row in records if row[1]==bass_key),default=now)
            segment=self._evidence(records,unit,now,max(start,segment_start))
            segment_total=float(segment.sum());segment_peak=float(segment.max())
            if segment_total:
                for i,row in enumerate(MIDI_HARMONY_TEMPLATES):
                    if row[1]!=bass or len(row[2])!=3 or row[4]:continue
                    core_values=sorted(segment[p] for p in row[3]);reference=core_values[(len(core_values)-1)//2]
                    if core_values[0]>=max(segment_peak*.04,reference*.15) and sum(segment[p] for p in row[2])/segment_total>=.6:
                        qualified.add(i)
        strict.update(qualified)
        potential=self._np.array(sorted(strict),dtype=int)
        recent=old is not None and self.supported_at is not None and now-self.supported_at<=unit*self.HISTORY_UNITS
        # Existing harmony may survive a weak/absent third during part handoff.
        # This cannot admit a new chord or renew its own evidence lifetime.
        if recent and old not in strict and covered[old]/total>=.5:
            potential=self._np.append(potential,old)
        boundary_cores={MIDI_HARMONY_TEMPLATES[i][3] for i in qualified}
        harmonic_pitches={row[1]%12 for row in records if self.role_weights.get(row[:3],1.)>=.95}
        for i in potential:
            _name,root,pitches,core,extras=MIDI_HARMONY_TEMPLATES[i];reference=references[i]
            if i!=old and not set(core)<=harmonic_pitches:continue
            if self.accepted is not None:
                previous=MIDI_HARMONY_TEMPLATES[self.accepted]
                if core not in boundary_cores and root!=previous[1] and (set(previous[3])<=current or len(current)<3 and anchor==previous[1]):continue
            if i!=old and i not in qualified and root!=anchor and not self._extra_supported(root,tuple(p for p in core if p!=root),records,unit,now,support_context):continue
            retaining=i==self.accepted
            if core in boundary_cores and extras and any(p not in current for p in extras):continue
            if extras and any(not any(key%12==p for (_ch,key) in self.live) and
                sum(1 for row in records if row[1]%12==p)<2 and not (
                    p in self.repeat_support and now-self.repeat_support[p]<=unit*8) for p in extras):continue
            if extras and (any(weights[p]<reference*(.25 if retaining else .65) for p in extras) or not retaining and any(
                    not self._extra_supported(p,core,records,unit,now,support_context) for p in extras)):continue
            if not extras and len(pitches)>3 and any(
                    not self._extra_supported(p,tuple(q for q in pitches if q!=p),records,unit,now,support_context) for p in pitches):continue
            # Keep this a weak prior:inversions,pedal points and a passing
            # walking-bass note cannot rewrite the chord's actual pitch set.
            scores[i]+=.04*bass_support[root]
            eligible.append(i)
        if not eligible:
            if recent and covered[old]/total>=.5 and current.intersection(MIDI_HARMONY_TEMPLATES[old][3]):
                self.reading=MidiChordReading(self.reading.symbols,approximate=True);return self.reading
            self.reading=self._dyad_reading(keys,now,unit);self.accepted=None;return self.reading
        qualified.intersection_update(eligible)
        best=max(eligible,key=lambda i:(scores[i],-i))
        old=self.accepted
        # A coherent new triad/root can cross the boundary immediately. A
        # single passing/colour note cannot use this fast path.
        if old is not None:
            old_core=set(MIDI_HARMONY_TEMPLATES[old][3]);old_root=MIDI_HARMONY_TEMPLATES[old][1]
            structural=[i for i in eligible if not MIDI_HARMONY_TEMPLATES[i][4]
                and set(MIDI_HARMONY_TEMPLATES[i][3])<=current
                and len(current-set(MIDI_HARMONY_TEMPLATES[i][3]))<=1
                and (len(set(MIDI_HARMONY_TEMPLATES[i][3])-old_core)>=2 or
                     (MIDI_HARMONY_TEMPLATES[i][1]!=old_root and bass==MIDI_HARMONY_TEMPLATES[i][1]
                      and not old_core<=current))]
            if qualified:
                target=max(qualified,key=lambda i:scores[i]);family=MIDI_HARMONY_TEMPLATES[target][3]
                best=max((i for i in eligible if MIDI_HARMONY_TEMPLATES[i][3]==family),key=lambda i:scores[i])
            elif structural:
                target=max(structural,key=lambda i:scores[i]);family=MIDI_HARMONY_TEMPLATES[target][3]
                best=max((i for i in eligible if MIDI_HARMONY_TEMPLATES[i][3]==family),key=lambda i:scores[i])
            elif old in eligible and (old in strict or best not in strict) and scores[best]<scores[old]+self.TRANSITION_MARGIN:best=old
            if best!=old and MIDI_HARMONY_TEMPLATES[best][1]==old_root:
                novel=set(MIDI_HARMONY_TEMPLATES[best][3])-old_core
                if novel and not all(any(key%12==p and now-on>=unit for (_ch,key),(_count,on) in self.live.items()) or
                        self._extra_supported(p,tuple(old_core.intersection(MIDI_HARMONY_TEMPLATES[best][3])),records,unit,now,support_context) for p in novel):
                    if old in eligible:best=old
        if best!=old and MIDI_HARMONY_TEMPLATES[best][3] in boundary_cores:
            # Commit only a qualified winner. Rejected proposals cannot erase
            # history,repeat support or the previously established harmony.
            self.history=deque((row for row in self.history if segment_start is not None and row[3]>=segment_start),maxlen=self.HISTORY_CAPACITY)
            self.repeat_support.clear()
        self.accepted=best;name=MIDI_HARMONY_TEMPLATES[best][0];self.dyad=None
        if best in strict:self.supported_at=now
        exact=any(symbol.split('/')[0]==name for symbol in midi_chord_candidates(keys))
        equivalents=MIDI_HARMONY_EQUIVALENTS[frozenset(MIDI_HARMONY_TEMPLATES[best][2])]
        alternatives=tuple(MIDI_HARMONY_TEMPLATES[i][0] for i in equivalents if i!=best and scores[i]>=scores[best]-.06)
        self.reading=MidiChordReading((name,*alternatives),approximate=not exact)
        return self.reading

    def _evaluate(self,now):
        self.reading=self.event_reading
        before=self.reading;reading=self._infer(now)
        if not self.durations and self.accepted is None and self.dyad is not None and not before.symbols:
            # Before any completed gate,co-onset wire jitter is not a usable
            # timing reference. The pure bootstrap view may show an exact pair,
            # but do not commit it as a boundary before a third can arrive.
            reading=before;self.reading=reading
        self.event_reading=reading
        self.analysed_attack=self.attack_serial
        if reading!=before:self.last_transitions=(*self.last_transitions[-511:],(now,reading))
        return reading

    def _presentation(self,now):
        # Query on an isolated inference view. GUI/worker poll timing must
        # never prune canonical history,renew support or choose later roots.
        # Numpy masks and immutable readings are shared;mutable context is not.
        current={key%12 for _ch,key in self.live}
        previous=(set(MIDI_HARMONY_TEMPLATES[self.accepted][2]) if self.accepted is not None
            else set(self.dyad[0]) if self.dyad is not None else set())
        new_dyad=len(current)==2 and len(current-previous)==2
        new_core=len(current-previous)>=2 and any(core<=current for core in self._triads)
        if self.event_reading.symbols and not(self.pending_at is not None and (new_dyad or new_core)):
            self.reading=self.event_reading;return self.reading
        view=object.__new__(type(self));view.__dict__=self.__dict__.copy()
        for name in ('live','repeat_support','role_weights'):
            setattr(view,name,getattr(self,name).copy())
        for name in ('history','role_history','onsets','durations'):
            value=getattr(self,name);setattr(view,name,deque(value,maxlen=value.maxlen))
        view.reading=self.event_reading
        self.reading=view._infer(now)
        return self.reading

    def observe_events(self,batch,*,identity,excluded=(),now_ns=None,invalidated=False):
        old_identity=self.identity;resync=self.raw.needs_resync
        self.last_transitions=()
        now=int(now_ns) if now_ns is not None else self.last_time
        if batch is None and now_ns is not None and not invalidated and old_identity==identity and self.observed_at is not None and 0<=now-self.observed_at<self.OBSERVATION_GRACE_NS:
            # A busy observer has consumed no journal rows. Preserve its cursor
            # and context; the next atomic read checks continuity/known/source.
            self.reading=MidiChordReading(self.reading.symbols,approximate=True)
            return self.reading
        self.raw.observe_events(batch,identity=identity,excluded=excluded)
        if invalidated or batch is None or not batch.known:
            self._clear_context();return self.reading
        excluded=frozenset(excluded)
        stamp=max((row[3] for row in batch.rows),default=self.last_time)
        now=max(stamp,int(now_ns) if now_ns is not None else stamp)
        relevant={ch for ch,_key in self.live}|{row[0] for row in self.history}
        exclusion_changed=bool((self.excluded^excluded)&relevant)
        if old_identity!=identity or self.source not in (None,batch.source) or exclusion_changed:
            resync|=old_identity is not None
            self._clear_context()
        elif self.excluded!=excluded:
            # A role-only past part can outlive harmonic history. Mute/drum
            # changes must retire its pattern even without a held note.
            changed=self.excluded^excluded
            self.role_history=deque((row for row in self.role_history if row[0] not in changed),maxlen=self.HISTORY_CAPACITY)
        self.excluded=excluded
        if resync or not batch.history_complete:
            self._clear_context();self.excluded=excluded
            self.live={address:(count,now) for address,count in batch.notes if address[0] not in excluded}
            self._track_bass(now)
            self.last_time=now
        else:
            checkpoints=0;limited=False
            def advance(until,*,inclusive=False):
                nonlocal checkpoints,limited
                while True:
                    cohort=self.pending_at is not None and (not self.age_deadlines or self.pending_at<=self.age_deadlines[0])
                    due=self.pending_at if cohort else self.age_deadlines[0] if self.age_deadlines else None
                    if due is None or not(due<until or inclusive and due==until):break
                    if cohort:
                        self.pending_at=None
                        unit=self._unit()
                        self.age_deadlines=tuple(sorted({due+max(1,round(unit*ratio)) for ratio in (.125,.5,1,1.5,2,4,8)})) if unit else ()
                    else:self.age_deadlines=self.age_deadlines[1:]
                    if checkpoints<self.CHECKPOINT_BUDGET:self._evaluate(due);checkpoints+=1
                    else:limited=True
            for _serial,packed,ch,when in batch.rows:
                advance(when)
                self._event(packed,ch,when)
                if ch==-1 or packed&240==176:self.event_reading=self.reading
                if packed&240 in (128,144,176) and ch not in excluded and self.pending_at is None:
                    unit=self._unit()
                    self.pending_at=when+round(unit/16) if unit else when
                    self.age_deadlines=()
            advance(now,inclusive=True)
            if limited:
                # A display CPU budget is not MIDI loss or audio backpressure.
                # Discard incomplete harmonic context and rebuild current state.
                self.history.clear();self.role_history.clear();self.repeat_support.clear();self.accepted=None;self.reading=MidiChordReading();self.dyad=None
                self.bass_pitch=None;self.bass_root=None
                self.event_reading=self.reading
                self.pending_at=None
                self.age_deadlines=()
        self.source=batch.source;self.last_time=max(self.last_time,now);self.observed_at=now
        return self._presentation(now)


class ChannelMidiActivity:
    """Note/velocity observations only; never opens a BASS channel stream.

    This shows MIDI activity, not a measured audio level, preserving the
    existing per-drum-key effect sends on the synthesis path.
    """

    def __init__(self,event_source=None):
        self.event_source=event_source
        self.lock = threading.Lock()
        self.notes = [{} for _ in range(MIDI_CHANNEL_LIMIT)]
        self.velocity = [0] * MIDI_CHANNEL_LIMIT
        self.last_on = [0.0] * MIDI_CHANNEL_LIMIT
        self.visual_phase = [0.0] * MIDI_CHANNEL_LIMIT
        self.visual_initial = [0.0] * MIDI_CHANNEL_LIMIT

    def note_on(self, channel: int, key: int, velocity: int, *, now=None):
        with self.lock:
            notes = self.notes[channel]
            current=time.monotonic() if now is None else float(now)
            self.visual_initial[channel]=(100. if velocity>=5 else midi_activity_strength_at(
                (sum(notes.values()),self.visual_phase[channel],self.visual_initial[channel]),current))
            self.visual_phase[channel]=current
            notes[key] = notes.get(key, 0) + 1
            self.velocity[channel] = velocity
            self.last_on[channel] = current
            if self.event_source is not None:
                self.event_source.submit(((0x90|(key<<8)|(velocity<<16),channel,0,int(current*1_000_000_000)),))

    def note_off(self, channel: int, key: int, *, now=None):
        with self.lock:
            notes = self.notes[channel]
            count = notes.get(key, 0)
            if count:
                current=time.monotonic() if now is None else float(now)
                self.visual_initial[channel]=midi_activity_strength_at(
                    (sum(notes.values()),self.visual_phase[channel],self.visual_initial[channel]),current)
                self.visual_phase[channel]=current
            if count <= 1:
                notes.pop(key, None)
            else:
                notes[key] = count - 1
            if self.event_source is not None:
                current=time.monotonic() if now is None else float(now)
                self.event_source.submit(((0x80|(key<<8),channel,0,int(current*1_000_000_000)),))

    def clear(self, channel: int | None = None):
        with self.lock:
            channels = range(MIDI_CHANNEL_LIMIT) if channel is None else (channel,)
            for index in channels:
                self.notes[index].clear()
                self.velocity[index] = 0
                self.last_on[index] = 0.0
                self.visual_phase[index]=self.visual_initial[index]=0.
            if self.event_source is not None:
                self.event_source.submit(((0xb0|(120<<8),-1 if channel is None else channel,0,time.perf_counter_ns()),))

    def snapshot(self):
        with self.lock:
            return tuple((sum(notes.values()), self.velocity[index], self.last_on[index])
                         for index, notes in enumerate(self.notes))

    def visual_state(self,now=None):
        current=time.monotonic() if now is None else float(now)
        with self.lock:
            return tuple(midi_activity_strength_at((sum(notes.values()),self.visual_phase[ch],
                         self.visual_initial[ch]),current)/100 for ch,notes in enumerate(self.notes))


DRUM_MIX_EVENTS = {
    "level": MIDI_EVENT_DRUM_LEVEL,
    "pan": MIDI_EVENT_DRUM_PAN,
    "reverb": MIDI_EVENT_DRUM_REVERB,
    "chorus": MIDI_EVENT_DRUM_CHORUS,
    "fine_tuning": MIDI_EVENT_DRUM_FINETUNE,
    "coarse_tuning": MIDI_EVENT_DRUM_COARSETUNE,
    "cutoff": MIDI_EVENT_DRUM_CUTOFF,
    "resonance": MIDI_EVENT_DRUM_RESONANCE,
    "userfx": MIDI_EVENT_DRUM_USERFX,
}
DRUM_PARAMETER_ORDER = (
    "coarse_tuning", "fine_tuning", "level", "pan",
    "reverb", "chorus", "userfx", "cutoff", "resonance",
)


@dataclass(frozen=True)
class PerformanceParameter:
    """Performance domain shared by UI, runtime, native event and INI paths."""
    key: str
    minimum: int
    maximum: int
    default: int
    entry_kind: str
    event: int
    scope: str
    input_steps: tuple[float,float,float]
    nrpn_msb: int | None = None
    integer: bool = True

    @property
    def entry_domain(self):
        return numeric_entry_domain(self.entry_kind,self.minimum,self.maximum)

    def clamp(self,value):
        number=float(value)
        if not math.isfinite(number):raise ValueError('performance parameter: '+self.key)
        amount=max(self.minimum,min(self.maximum,int(round(number)) if self.integer else number))
        return int(amount) if self.integer else float(amount)

    def validate(self,value):
        number=self.entry_domain.validate_raw(value)
        if self.integer and number!=int(number):raise ValueError('performance parameter: '+self.key)
        return int(number) if self.integer else number

    def parameter(self,key,value):
        amount=self.validate(value)
        if self.scope=='master_fine_channel0':
            return round((amount+100)*16383/200)
        if self.scope=='master_coarse_channel0':
            return 64+amount
        if self.scope=='drum_key':
            if not 0<=key<128:raise ValueError('drum key')
            return (amount<<8)|key
        if not 0<=key<12:raise ValueError('pitch class')
        # Same fine14 formula as the existing scale path, including ties-to-even.
        fine=round((amount+100)*16383/200)
        return (key<<16)|fine


# bassmidi.h / StreamEvent NRPN14/15/18/19/1A/1C/1D/1Eknh.
# key is separate from raw7bit controller value; PAN128 means SoundFont pan.
DRUM_PARAMETERS={field:PerformanceParameter(field,*DRUM_KEY_DOMAINS[field],
    {'coarse_tuning':'drum_coarse','fine_tuning':'drum_fine',
    'pan':'drum_pan','cutoff':'midi_offset','resonance':'midi_offset'}.get(field,'drum_amount'),
    event,'drum_key',(1,4,8) if field=='pan' else (1,5,10),
    {'cutoff':0x14,'resonance':0x15,'coarse_tuning':0x18,'fine_tuning':0x19,
     'level':0x1A,'pan':0x1C,'reverb':0x1D,'chorus':0x1E,'userfx':0x1F}[field])
    for field,event in DRUM_MIX_EVENTS.items()}
SCALE_PARAMETER=PerformanceParameter('scale',*SCALE_TUNING_DOMAIN,'cents',MIDI_EVENT_SCALETUNING,
    'pitch_class_all_channels',(1,5,10))
MASTER_FINE_PARAMETER=PerformanceParameter('master_fine',-100,100,0,'master_cents',
    MIDI_EVENT_MASTER_FINETUNE,'master_fine_channel0',(1,5,10),integer=False)
MASTER_COARSE_PARAMETER=PerformanceParameter('master_coarse',-24,24,0,'signed_semitones',
    MIDI_EVENT_MASTER_COARSETUNE,'master_coarse_channel0',(1,2,4))
A4_REFERENCE_HZ = 440.0


def tuning_hz(cents: float) -> float:
    return A4_REFERENCE_HZ * 2.0 ** (float(cents) / 1200.0)


def tuning_cents(hz: float) -> float:
    if not math.isfinite(hz) or hz <= 0:
        raise ValueError("Invalid A4 reference frequency")
    return max(-100.0, min(100.0, 1200.0 * math.log2(hz / A4_REFERENCE_HZ)))


EXPRESSION_EVENTS = {
    "modulation_vibrato": MIDI_EVENT_MOD_VIBRATO,
    "modulation_pitch": MIDI_EVENT_MOD_PITCH,
    "modulation_filter": MIDI_EVENT_MOD_FILTER,
    "modulation_volume": MIDI_EVENT_MOD_VOLUME,
    "pitch_range": MIDI_EVENT_PITCHRANGE,
    "fine_tuning": MIDI_EVENT_FINETUNE,
    "coarse_tuning": MIDI_EVENT_COARSETUNE,
    "expression_level": MIDI_EVENT_EXPRESSION,
    "vibrato_rate": MIDI_EVENT_VIBRATO_RATE,
    "vibrato_depth": MIDI_EVENT_VIBRATO_DEPTH,
    "vibrato_delay": MIDI_EVENT_VIBRATO_DELAY,
    "portamento": MIDI_EVENT_PORTAMENTO,
    "portamento_time": MIDI_EVENT_PORTATIME,
    "mode": MIDI_EVENT_MODE,
    "cutoff": MIDI_EVENT_CUTOFF,
    "resonance": MIDI_EVENT_RESONANCE,
    "attack": MIDI_EVENT_ATTACK,
    "decay": MIDI_EVENT_DECAY,
    "release": MIDI_EVENT_RELEASE,
    "mix_level": MIDI_EVENT_MIXLEVEL,
    "transpose": MIDI_EVENT_TRANSPOSE,
    "channel_pressure_vibrato": MIDI_EVENT_CHANPRES_VIBRATO,
    "channel_pressure_pitch": MIDI_EVENT_CHANPRES_PITCH,
    "channel_pressure_filter": MIDI_EVENT_CHANPRES_FILTER,
    "channel_pressure_volume": MIDI_EVENT_CHANPRES_VOLUME,
    "key_pressure_vibrato": MIDI_EVENT_KEYPRES_VIBRATO,
    "key_pressure_pitch": MIDI_EVENT_KEYPRES_PITCH,
    "key_pressure_filter": MIDI_EVENT_KEYPRES_FILTER,
    "key_pressure_volume": MIDI_EVENT_KEYPRES_VOLUME,
}
EXPRESSION_DEFAULTS = {"pitch_range": 2, "fine_tuning": 0,
                       "coarse_tuning": 0, "expression_level": 127,
                       "vibrato_rate": 64,
                       "vibrato_depth": 64, "vibrato_delay": 64,
                       "portamento": 127, "portamento_time": 0,
                       "mode": 0, "cutoff": 64, "resonance": 64,
                       "attack": 64, "decay": 64, "release": 64,
                       "mix_level": 100, "transpose": 0,
                       "channel_pressure_vibrato": 50,
                       "channel_pressure_pitch": 0,
                       "channel_pressure_filter": 0,
                       "channel_pressure_volume": 0,
                       "key_pressure_vibrato": 0,
                       "key_pressure_pitch": 0,
                       "key_pressure_filter": 0,
                       "key_pressure_volume": 0,
                       "modulation_vibrato": 50, "modulation_pitch": 0,
                       "modulation_filter": 0, "modulation_volume": 0}
EXPRESSION_MINIMUMS = {field: (-9600 if field.endswith("_filter") else
                               -100 if field in ("fine_tuning", "channel_pressure_volume",
                                                 "key_pressure_volume", "modulation_volume") else
                               -24 if field in ("coarse_tuning", "transpose",
                                                "channel_pressure_pitch", "key_pressure_pitch", "modulation_pitch") else 0)
                       for field in EXPRESSION_EVENTS}
EXPRESSION_MAXIMUMS = {field: (9600 if field.endswith("_filter") else
                               INSTRUMENT_VOLUME_MAX if field == "mix_level" else
                               200 if field in ("channel_pressure_vibrato",
                                                "key_pressure_vibrato", "modulation_vibrato") else
                               100 if field in ("channel_pressure_volume", "key_pressure_volume", "modulation_volume") else
                               24 if field in ("pitch_range", "transpose",
                                               "channel_pressure_pitch", "key_pressure_pitch", "modulation_pitch") else
                               100 if field == "fine_tuning" else
                               24 if field == "coarse_tuning" else
                               2 if field == "mode" else 127)
                       for field in EXPRESSION_EVENTS}

CHANNEL_MIX_RANGES = {
    "volume": (0, 127), "pan": (0, 127), "chorus": (0, 127),
    "reverb": (0, 127), "modulation": (0, 127), "pitch": (0, 16383),
    "delay_send": (0, 127),
    "sustain": (0, 127), "sostenuto": (0, 127), "soft": (0, 127),
    "channel_pressure": (0, 127), "key_pressure": (0, 127),
}
CHANNEL_FACTORY_DEFAULTS = {**EXPRESSION_DEFAULTS,"volume":100,"pan":64,"pitch":8192,
    "reverb":40,"chorus":0,"modulation":0,"delay_send":0,"sustain":0,"sostenuto":0,
    "soft":0,"channel_pressure":0,"key_pressure":0}
MODULATION_NATIVE_FIELDS = {EXPRESSION_EVENTS[field]: field for field in EXPRESSION_EVENTS
                            if field.startswith("modulation_")}

CHANNEL_MODE_SWITCHES = ("mode_mono", "mode_poly", "mode_legato")

# MIDI 1.0 default controller identities. UI and native routing share this map.
# CC21..24 drive SF2 modulators only when defined by the selected SoundFont.
SOUNDFONT_MODULATOR_CC = (21, 22, 23, 24)
AUXILIARY_CC = (2, 4, 8, 12, 13, 92, 95, *SOUNDFONT_MODULATOR_CC, 42)
NATIVE_ADDITIONAL_CC = (*SOUNDFONT_MODULATOR_CC,42)
CHANNEL_CC_DEFAULTS = {
    "volume": 7, "pan": 10, "modulation": 1, "reverb": 91, "chorus": 93,
    "delay_send": 94, "expression_level": 11, "portamento_time": 5,
    "portamento": 65, "sustain": 64, "sostenuto": 66, "soft": 67,
    "resonance": 71, "release": 72, "attack": 73, "cutoff": 74,
    "decay": 75, "vibrato_rate": 76, "vibrato_depth": 77, "vibrato_delay": 78,
    "mode_mono": 126, "mode_poly": 127,
    **{f"aux_{number}": number for number in AUXILIARY_CC},
}
CHANNEL_FUNCTION_TARGETS = tuple(CHANNEL_MIX_RANGES) + tuple(
    key for key in EXPRESSION_EVENTS if key != "mode")
CHANNEL_RPN_DEFAULTS = {"pitch_range": 0, "fine_tuning": 1,
                        "coarse_tuning": 2, "modulation_vibrato": 5}
# Universal Real-Time Controller Destination: subtype, source CC if any, PP.
# Yamaha PSR-S910 Data List pp75-76; BASSMIDI supports modulation CC1 only.
CHANNEL_SYSEX_ROUTES = {
    f"{source}_{destination}": (subtype, 1 if source == "modulation" else None, parameter)
    for source, subtype in (("channel_pressure", 1), ("key_pressure", 2), ("modulation", 3))
    for destination, parameter in (("pitch", 0), ("filter", 1), ("volume", 2), ("vibrato", 3))
}
CHANNEL_TIMBRE_FIELDS = ("mix_level", "attack", "decay", "release", "cutoff", "resonance", *PRESET_EXPRESSION_RANGES)


@dataclass(frozen=True)
class ChannelParameter:
    """Canonical channel bounds/default/unit/protocol; not stored live values.

    Display precision is not wire precision. Native 14-bit pitch/RPN remains
    native; an explicitly assigned CC deliberately has only128 input steps.
    """
    key: str
    minimum: int
    maximum: int
    default: int | None
    entry_kind: str
    event: int | None
    cc: int | None
    rpn: int | None
    wire_bits: int | None

    @property
    def entry_domain(self):
        return numeric_entry_domain(self.entry_kind,self.minimum,self.maximum)

    def validate(self, value):
        number = self.entry_domain.validate_raw(value)
        if number != int(number):
            raise ValueError("channel parameter domain: " + self.key)
        return int(number)

    def reset_value(self, *, default_reverb=None):
        """Native neutral/factory seed; group/policy overlays remain later writers."""
        return self.validate(default_reverb if self.key=="reverb" and default_reverb is not None else self.default)

    def cc_value(self, controller_value):
        """Explicit128-step automation, distinct from native wire precision.

        Keep the established switch threshold and signed CC64 neutral. This
        same descriptor now owns bounds, editing, reset and custom conversion.
        """
        if not 0<=controller_value<=127 or int(controller_value)!=controller_value:
            raise ValueError('MIDI CC value')
        if self.key=='mode':raise ValueError('MIDI CC target')
        if self.key in ('sustain','sostenuto','soft','portamento'):
            return 127 if controller_value>=64 else 0
        if self.key=='pitch':return 16383 if controller_value==127 else controller_value*128
        if self.key in ('fine_tuning','coarse_tuning','transpose') or self.key.endswith(('_pitch','_filter','_volume')):
            return (round((controller_value-64)*-self.minimum/64) if controller_value<=64 else
                    round((controller_value-64)*self.maximum/63))
        return self.minimum+round(controller_value*(self.maximum-self.minimum)/127)

    @property
    def signal(self):
        return (f"CC#{self.cc:03d}" if self.cc is not None else "RPN" if self.rpn is not None
                else "SysEx" if self.key in CHANNEL_SYSEX_ROUTES else "")

    def wire_contract(self):
        """Protocol data, not translated UI copy or an invented CC source.

        Product entry bounds don't clamp unrelated native/DAW wire values.
        An explicit custom CC is separately and deliberately seven-bit.
        """
        source=('pitch_wheel' if self.key=='pitch' else
                'channel_pressure' if self.key=='channel_pressure' else
                'key_pressure' if self.key=='key_pressure' else
                'RPN' if self.rpn is not None else 'CC' if self.cc is not None else
                'SysEx' if self.key in CHANNEL_SYSEX_ROUTES else 'internal')
        return dict(source=source,event=self.event,cc=self.cc,rpn=self.rpn,
            route=CHANNEL_SYSEX_ROUTES.get(self.key),wire_bits=self.wire_bits,
            key_addressed=self.key=='key_pressure',
            readback='native sync/filter observation; GetEvent69 unavailable in active DLL' if self.key.startswith('modulation_') else 'native event/readback',
            native_resolution='RPN0 cents LSB not represented by integer PITCHRANGE' if self.rpn==0 else
                              'RPN2 uses Data Entry MSB only' if self.rpn==2 else 'preserved by native dispatcher',
            entry_domain=(self.minimum,self.maximum),custom_cc_steps=128,
            custom_cc_policy='explicit whole-entry-domain mapping; does not reduce native wire precision')


def _channel_entry_kind(field):
    return ("pitch_wheel" if field == "pitch" else "cents" if field == "fine_tuning" else "signed_semitones"
            if field in ("coarse_tuning", "transpose") or field.endswith("_pitch")
            else "cents" if field.endswith(("_filter", "_vibrato"))
            else "signed_percent" if field.endswith("_volume")
            else "percent" if field == "mix_level" else "semitones"
            if field == "pitch_range" else "midi_offset"
            if field in ("vibrato_rate", "vibrato_depth", "vibrato_delay", "cutoff", "resonance", "attack", "decay", "release")
            else "midi_send")


CHANNEL_PARAMETERS = {
    field: ChannelParameter(field, *(CHANNEL_MIX_RANGES[field] if field in CHANNEL_MIX_RANGES
                                    else (EXPRESSION_MINIMUMS[field], EXPRESSION_MAXIMUMS[field])),
                            CHANNEL_FACTORY_DEFAULTS.get(field), _channel_entry_kind(field),
                            EXPRESSION_EVENTS.get(field, {"volume":MIDI_EVENT_VOLUME,"pan":MIDI_EVENT_PAN,
                                "pitch":MIDI_EVENT_PITCH,"modulation":MIDI_EVENT_MODULATION,
                                "reverb":MIDI_EVENT_REVERB,"chorus":MIDI_EVENT_CHORUS,
                                "sustain":MIDI_EVENT_SUSTAIN,"sostenuto":MIDI_EVENT_SOSTENUTO,
                                "soft":MIDI_EVENT_SOFT,"channel_pressure":MIDI_EVENT_CHANPRES,
                                "key_pressure":MIDI_EVENT_KEYPRES}.get(field)), CHANNEL_CC_DEFAULTS.get(field),
                            CHANNEL_RPN_DEFAULTS.get(field),
                            14 if field in ("pitch", "fine_tuning", "modulation_vibrato") else
                            7 if field in CHANNEL_CC_DEFAULTS or field in CHANNEL_RPN_DEFAULTS or
                                 field in CHANNEL_SYSEX_ROUTES or field in ("channel_pressure","key_pressure") else None)
    for field in (*CHANNEL_MIX_RANGES, *EXPRESSION_EVENTS)
}


# Address selectors are reset seeds, not new CC/function targets. Bank128 is
# the existing UI drum-bank identity, never a fabricated raw CC value128.
CHANNEL_RESET_ADDRESS_PARAMETERS={
    key:ChannelParameter(key,0,maximum,0,'number_integer',None,None,None,None)
    for key,maximum in (('bank',128),('bank_lsb',127),('program',127))
}


def channel_reset_parameter(field):
    return CHANNEL_RESET_ADDRESS_PARAMETERS[field] if field in CHANNEL_RESET_ADDRESS_PARAMETERS else CHANNEL_PARAMETERS[field]


def _publish_channel_configuration(method=None, *, unchanged=None):
    """Edit routing only inside the existing ordered publication fence.

    Native capture/replay uses its private locked writers, not this control
    entry. Enter before taking model locks so metadata can drain old routes.
    Standalone/startup models have no live executor and use nullcontext.
    """
    if method is None:
        return lambda target: _publish_channel_configuration(target, unchanged=unchanged)
    @wraps(method)
    def edit(self, *args, **kwargs):
        # Presentation calls (for example hiding a Learn-capable window)
        # must not pause the MIDI actor when there is no configuration edit.
        # The guarded read is a valid no-op linearization point; real edits
        # still recheck their state inside the ordered scope/model lock.
        if unchanged is not None:
            with self.lock:
                if unchanged(self, *args, **kwargs):
                    return
        with self.reset_commit_scope():
            return method(self, *args, **kwargs)
    edit._requires_reset_contract = True
    return edit


class ChannelControlPolicy:
    """Durable wiring/reset seeds, separate from temporary sounding values.

    Owned with MIDI state, not widgets. Native filter callbacks only perform
    lookups under this short lock; notification/disk writes happen on edits.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.functions = {}
        self.reset_defaults = {}
        self.inputs = {}
        self.timbre = {}
        self.pressure_keys = {}
        self.revision = 0
        self.on_change = None
        self.reset_commit_scope = nullcontext

    def destination(self, channel, source):
        with self.lock:
            return self.functions.get((channel, source), source)

    def pressure_key(self, channel):
        with self.lock:
            return self.pressure_keys.get(channel, 60)

    @_publish_channel_configuration
    def select_pressure_key(self, channels, key):
        channels = tuple(channels)
        if not isinstance(key,int) or not 0 <= key < 128 or any(not isinstance(ch,int) or not 0 <= ch < MIDI_CHANNEL_LIMIT for ch in channels):
            raise ValueError("poly pressure key")
        with self.lock:
            if all(self.pressure_key(ch) == key for ch in channels):
                return
            for ch in channels:
                if key == 60:
                    self.pressure_keys.pop(ch, None)
                else:
                    self.pressure_keys[ch] = key
                self.inputs.pop((ch, "key_pressure"), None)
            self.revision += 1
        if self.on_change:
            self.on_change()

    def mapped(self, channel, source):
        with self.lock:
            return (channel, source) in self.functions

    def has_functions(self):
        with self.lock:
            return bool(self.functions)

    @_publish_channel_configuration
    def reset_functions(self, channel=None, *, notify=True):
        """Restore wiring only; preserve reset seeds and sounding parameters."""
        if channel is not None and (type(channel) is not int or not 0 <= channel < MIDI_CHANNEL_LIMIT):
            raise ValueError("channel")
        with self.lock:
            keys = tuple(key for key in self.functions if channel is None or key[0] == channel)
            for key in keys:
                self.functions.pop(key)
                self.inputs.pop(key, None)
            if keys:
                self.revision += 1
        if keys and notify and self.on_change:
            self.on_change()
        return bool(keys)

    @_publish_channel_configuration
    def assign(self, channels, source, destination):
        if source not in CHANNEL_FUNCTION_TARGETS or destination not in (*CHANNEL_FUNCTION_TARGETS, None):
            raise ValueError("channel functional destination")
        channels = tuple(channels)
        if any(not isinstance(channel,int) or not 0 <= channel < MIDI_CHANNEL_LIMIT for channel in channels):
            raise ValueError("channel")
        with self.lock:
            if all((self.functions.get((channel,source),source)==destination and
                    (channel,source) not in self.inputs) for channel in channels):return
            for channel in channels:
                if destination == source:
                    self.functions.pop((channel, source), None)
                else:
                    self.functions[channel, source] = destination
                self.inputs.pop((channel, source), None)
            self.revision += 1
        if self.on_change:
            self.on_change()

    @staticmethod
    def bounds(field):
        parameter = CHANNEL_PARAMETERS[field]
        return parameter.minimum, parameter.maximum

    @classmethod
    def convert(cls, source, destination, value):
        if destination is None:
            return None
        if source == destination:
            return int(value)
        low, high = cls.bounds(source)
        amount = max(low, min(high, int(value)))
        return channel_cc_target_value(destination, round((amount - low) * 127 / max(1, high - low)))

    def clear_session(self, channel=None):
        with self.lock:
            for state in (self.inputs, self.timbre):
                for key in tuple(state):
                    if channel is None or key[0] == channel:
                        state.pop(key)

    def replace_defaults(self, channels, values):
        channels = tuple(channels)
        if any(not isinstance(channel,int) or not 0 <= channel < MIDI_CHANNEL_LIMIT for channel in channels):
            raise ValueError("channel")
        allowed = (*CHANNEL_FUNCTION_TARGETS, "mode", "bank", "bank_lsb", "program")
        checked = {}
        for (channel, field), value in values.items():
            if channel not in channels or field not in allowed:
                raise ValueError("channel reset default")
            checked[channel,field]=channel_reset_parameter(field).validate(value)
        with self.lock:
            if {key:value for key,value in self.reset_defaults.items() if key[0] in channels}==checked:return
        with self.reset_commit_scope(), self.lock:
            if {key:value for key,value in self.reset_defaults.items() if key[0] in channels}==checked:return
            self.reset_defaults = {key: value for key, value in self.reset_defaults.items()
                                   if key[0] not in channels}
            self.reset_defaults.update(checked)
            self.revision += 1
        if self.on_change:
            self.on_change()

    def ini_values(self):
        with self.lock:
            return {**{f"pressure_key_{channel}": str(key) for channel, key in self.pressure_keys.items()},
                    **{f"function_{channel}_{field}": destination or "none"
                       for (channel, field), destination in self.functions.items()},
                    **{f"reset_{channel}_{field}": str(value)
                       for (channel, field), value in self.reset_defaults.items()}}

    @_publish_channel_configuration
    def restore_ini(self, values, *, report=None):
        with self.lock:
            self.functions.clear()
            self.reset_defaults.clear()
            self.pressure_keys.clear()
            for key, value in values.items():
                if key.startswith("pressure_key_"):
                    try:
                        ch, note = int(key.removeprefix("pressure_key_")), int(value)
                        if not 0 <= ch < MIDI_CHANNEL_LIMIT or not 0 <= note < 128: raise ValueError('pressure key')
                        if 0 <= ch < MIDI_CHANNEL_LIMIT and 0 <= note < 128 and note != 60:
                            self.pressure_keys[ch] = note
                        if report is not None: report.record('Channel Policy',key,'accepted')
                    except (ValueError, TypeError):
                        if report is not None: report.record('Channel Policy',key,'invalid')
                    continue
                kind, separator, rest = key.partition("_")
                channel_text, separator2, field = rest.partition("_")
                try:
                    channel = int(channel_text)
                    if not separator or not separator2 or not 0 <= channel < MIDI_CHANNEL_LIMIT:
                        raise ValueError('channel address')
                    if kind == "function" and field in CHANNEL_FUNCTION_TARGETS:
                        destination = None if value == "none" else value
                        if destination not in (*CHANNEL_FUNCTION_TARGETS,None): raise ValueError('function destination')
                        if destination in (*CHANNEL_FUNCTION_TARGETS, None) and destination != field:
                            self.functions[channel, field] = destination
                    elif kind == "reset":
                        allowed = (*CHANNEL_FUNCTION_TARGETS, "mode", "bank", "bank_lsb", "program")
                        if field not in allowed:
                            if report is not None: report.record('Channel Policy',key,'unknown_preserved')
                            continue
                        self.reset_defaults[channel,field]=channel_reset_parameter(field).validate(int(value))
                    else:
                        if report is not None: report.record('Channel Policy',key,'unknown_preserved')
                        continue
                    if report is not None: report.record('Channel Policy',key,'accepted')
                except (ValueError, TypeError):
                    if report is not None: report.record('Channel Policy',key,'invalid' if kind in ('reset','function') else 'unknown_preserved')
                    continue
            self.clear_session()
            self.revision += 1


def channel_cc_target_value(target: str, controller_value: int) -> int:
    """One 7-bit automation conversion shared by dispatch and presentation.

    CC64 is neutral for signed domains. Voice mode has no standard single-CC
    enum mapping and is deliberately excluded; CC126/127 use the native router.
    """
    if not 0 <= controller_value <= 127:
        raise ValueError("MIDI CC value")
    if target in CHANNEL_MODE_SWITCHES:
        return 127 if controller_value >= 64 else 0
    if target not in CHANNEL_PARAMETERS or target == "mode":
        raise ValueError("MIDI CC target")
    return CHANNEL_PARAMETERS[target].cc_value(controller_value)

# Values remain session-only; explicit routing is separate. CC21..24 also
# retain their original RAW path into SoundFont-defined native modulators.
AUXILIARY_CC_DESTINATIONS = CHANNEL_FUNCTION_TARGETS


class AuxiliaryControllers:
    """Observe unhandled CC values independently of the BASS sound path."""

    def __init__(self):
        self.lock = threading.RLock()
        self.values = [{number: 0 for number in range(120)} for _ in range(MIDI_CHANNEL_LIMIT)]

    def receive(self, channel: int, number: int, value: int) -> None:
        if not 0 <= channel < MIDI_CHANNEL_LIMIT or not 0 <= number < 120 or not 0 <= value <= 127:
            raise ValueError("auxiliary MIDI CC")
        with self.lock:
            self.values[channel][number] = value

    def snapshot(self, channel: int) -> dict[int, int]:
        if not 0 <= channel < MIDI_CHANNEL_LIMIT:
            raise ValueError("channel")
        with self.lock:
            return {number: self.values[channel][number] for number in AUXILIARY_CC}

    def source_value(self, channel: int, number: int) -> int:
        if not 0 <= channel < MIDI_CHANNEL_LIMIT or not 0 <= number < 120:
            raise ValueError("MIDI CC source")
        with self.lock:
            return self.values[channel][number]

    def reset(self, channel: int | None = None) -> None:
        channels = range(MIDI_CHANNEL_LIMIT) if channel is None else (channel,)
        with self.lock:
            for index in channels:
                if not 0 <= index < MIDI_CHANNEL_LIMIT:
                    raise ValueError("channel")
                self.values[index].update({number: 0 for number in range(120)})


class ChannelMidiLearn:
    """Durable, thread-safe CC configuration, separate from musical state.

    The MIDI input thread may acquire a source CC while Qt reads its snapshot;
    no callback from this class touches a widget. CC120..127 remain transport
    commands and can never be captured or consumed as automation sources.
    """

    def __init__(self):
        self.channels = lambda: range(MIDI_CHANNELS_PER_PORT)
        self.lock = threading.RLock()
        self._pending: tuple[int, str] | None = None
        self._assignments: dict[tuple[int, str], set[int]] = {}
        self._destinations: dict[tuple[int, str], str] = {}
        self._names: dict[tuple[int, str], str] = {}
        self._all_channels: set[tuple[int, str]] = set()
        self._dispatch: dict[tuple[int, int], tuple[str, ...]] = {}
        self._pending_all = False
        self._pending_destination = None
        # This notification may run on the MIDI thread. The Qt owner must
        # queue it; neither disk access nor widget work belongs in this class.
        self.on_change = None
        self.revision = 0
        self.caption_revision = 0  # Aliases do not change native routing identity.
        self.reset_commit_scope = nullcontext

    @staticmethod
    def _validate(channel: int, target: str) -> None:
        if not isinstance(channel,int) or not 0 <= channel < MIDI_CHANNEL_LIMIT or (target not in CHANNEL_MIX_RANGES
                                     and target not in EXPRESSION_EVENTS
                                     and target not in CHANNEL_MODE_SWITCHES
                                     and target not in {f"aux_{cc}" for cc in AUXILIARY_CC}):
            raise ValueError("channel MIDI Learn target")

    def _notify(self):
        if self.on_change is not None:
            self.on_change()

    def control_clone(self, *, channels=None):
        """Private control-side successor oracle; never notify or mutate self.

        Copy assignment containers in insertion order, including unassigned
        auxiliary names and pending/global intent. The channel scope is frozen
        only for this preparation; native admission must compare its epoch to
        the live address generation before consuming a capture.
        """
        with self.lock:
            scope=tuple(self.channels()) if channels is None else tuple(channels)
            if any(type(channel) is not int or not 0<=channel<MIDI_CHANNEL_LIMIT for channel in scope):
                raise ValueError('MIDI Learn clone channel scope')
            clone=ChannelMidiLearn();clone.channels=lambda:scope
            clone._pending=self._pending;clone._pending_all=self._pending_all
            clone._pending_destination=self._pending_destination;clone.revision=self.revision
            clone.caption_revision=self.caption_revision
            clone._assignments={key:set(sources) for key,sources in self._assignments.items()}
            clone._destinations=dict(self._destinations);clone._names=dict(self._names)
            clone._all_channels=set(self._all_channels);clone._dispatch=dict(self._dispatch)
            # on_change is deliberately None; compilation owns no UI/disk hook.
            return clone

    @_publish_channel_configuration
    def arm(self, channel: int, target: str, *, all_channels=False, destination=None) -> None:
        self._validate(channel, target)
        if target == "mode":
            raise ValueError("Voice mode uses standard channel-mode messages")
        if target.startswith("aux_") and destination not in AUXILIARY_CC_DESTINATIONS:
            raise ValueError("auxiliary MIDI destination")
        if not target.startswith("aux_") and destination not in (None, target):
            raise ValueError("MIDI control destination")
        with self.lock:
            if (self._pending==(channel,target) and self._pending_all==bool(all_channels) and
                    self._pending_destination==destination):return
            self._pending = channel, target
            self._pending_all = bool(all_channels)
            self._pending_destination = destination
            self.revision += 1

    @_publish_channel_configuration
    def assign(self, channel: int, target: str, controller: int, *, all_channels=False,
               destination=None) -> None:
        """Replace one control's source, without changing its musical value."""
        self._validate(channel, target)
        if target == "mode":
            raise ValueError("Voice mode uses standard channel-mode messages")
        if not isinstance(controller, int) or not 0 <= controller < 120:
            raise ValueError("channel MIDI Learn source CC")
        if target.startswith("aux_") and destination not in AUXILIARY_CC_DESTINATIONS:
            raise ValueError("auxiliary MIDI destination")
        if not target.startswith("aux_") and destination not in (None, target):
            raise ValueError("MIDI control destination")
        with self.lock:
            channels=tuple(self.channels()) if all_channels else (channel,)
            unchanged=self._pending is None and all(
                self._assignments.get((ch,target))=={controller} and
                self._destinations.get((ch,target))==(destination or target) and
                ((ch,target) in self._all_channels)==bool(all_channels) for ch in channels)
            conflicts=target in CHANNEL_MODE_SWITCHES and any(
                other!=target and self._assignments.get((ch,other))=={controller}
                for ch in channels for other in CHANNEL_MODE_SWITCHES)
            if unchanged and not conflicts:return
            self._assign_locked(channel, target, controller, all_channels, destination)
        self._notify()

    def _assign_locked(self, channel, target, controller, all_channels, destination):
        # A source CC is not a sound-function name. Aliases are sparse,
        # explicit edits and remain independent on every channel, including
        # when the same source is assigned globally or captured by Learn.
        for ch in self.channels() if all_channels else (channel,):
            if target in CHANNEL_MODE_SWITCHES:
                # One CC may fan out to ordinary sound controls, but giving
                # it opposite mode selectors is order-dependent and useless.
                for other in CHANNEL_MODE_SWITCHES:
                    conflict = ch, other
                    if other != target and self._assignments.get(conflict) == {controller}:
                        self._assignments.pop(conflict, None)
                        self._destinations.pop(conflict, None)
                        self._names.pop(conflict, None)
                        self._all_channels.discard(conflict)
            key = ch, target
            self._assignments[key] = {controller}
            self._destinations[key] = destination or target
            if all_channels:
                self._all_channels.add(key)
            else:
                self._all_channels.discard(key)
        self._pending = None
        self.revision += 1
        self._reindex_locked()

    def _reindex_locked(self):
        # Configuration changes rebuild the derived index. Frequent incoming
        # CC events never scan every control on sixteen channels.
        dispatch = {}
        for (channel, target), sources in self._assignments.items():
            destination = self._destinations.get((channel, target), target)
            for controller in sources:
                values = dispatch.setdefault((channel, controller), [])
                if destination not in values:
                    values.append(destination)
        self._dispatch = {key: tuple(values) for key, values in dispatch.items()}

    def binding(self, channel, target):
        with self.lock:
            key = channel, target
            sources = self._assignments.get(key)
            if not sources:
                return None
            return (next(iter(sources)), self._destinations.get(key, target),
                    self._names.get(key, ""), key in self._all_channels)

    def control_name(self, channel, target, default=""):
        self._validate(channel, target)
        with self.lock:
            return self._names.get((channel, target), default)

    def rename(self, channel, target, name):
        self._validate(channel, target)
        name = str(name).strip()
        if not name or len(name) > 48 or any(ord(char) < 32 for char in name):
            raise ValueError("CC control name")
        with self.lock:
            key = channel, target
            if key not in self._assignments and not target.startswith("aux_"):
                return
            channels = self.channels() if key in self._all_channels else (channel,)
            changed=False
            for ch in channels:
                candidate = ch, target
                if candidate == key or (candidate in self._all_channels
                                        and self._assignments.get(candidate) == self._assignments[key]
                                        and self._destinations.get(candidate) == self._destinations.get(key)):
                    if self._names.get(candidate)!=name:
                        self._names[candidate] = name;changed=True
            if changed:self.caption_revision += 1
        if changed:self._notify()

    def targets_for_source(self, channel: int, controller: int) -> tuple[str, ...]:
        if not 0 <= channel < MIDI_CHANNEL_LIMIT or not 0 <= controller < 120:
            raise ValueError("channel MIDI source")
        with self.lock:
            return self._dispatch.get((channel, controller), ())

    def assign_source(self, channel: int, controller: int, target: str | None) -> None:
        """The channel editor chooses one audible destination for this CC."""
        if controller not in AUXILIARY_CC or not 0 <= channel < MIDI_CHANNEL_LIMIT:
            raise ValueError("auxiliary MIDI source")
        if target is not None and target not in AUXILIARY_CC_DESTINATIONS:
            raise ValueError("auxiliary MIDI destination")
        key = f"aux_{controller}"
        if target is None:
            self.remove(channel, key)
        else:
            self.assign(channel, key, controller, destination=target)

    @_publish_channel_configuration(unchanged=lambda self, channel=None:
        self._pending is None or channel is not None and self._pending[0] != channel)
    def cancel(self, channel=None) -> None:
        with self.lock:
            if self._pending is not None and (channel is None or self._pending[0] == channel):
                self._pending = None
                self.revision += 1

    def remove(self, channel: int, target: str) -> None:
        self.remove_control((channel,), target)

    @_publish_channel_configuration
    def remove_control(self, channels, target: str) -> None:
        """One per-control edit/notification; never inherit a prior global scope."""
        channels = tuple(channels)
        for channel in channels:
            self._validate(channel, target)
        with self.lock:
            changed = False
            for channel in channels:
                key = channel, target
                changed |= key in self._assignments or key in self._names
                self._assignments.pop(key, None)
                self._destinations.pop(key, None)
                self._names.pop(key, None)
                self._all_channels.discard(key)
                if self._pending == key:
                    self._pending = None
                    changed = True
            if changed:
                self._reindex_locked()
                self.revision += 1
        if changed:
            self._notify()

    @_publish_channel_configuration
    def reset(self, channel: int | None = None, *, notify=True) -> bool:
        with self.lock:
            if channel is None:
                changed = bool(self._assignments or self._names or self._pending)
                self._assignments.clear()
                self._destinations.clear()
                self._names.clear()
                self._all_channels.clear()
                self._pending = None
            else:
                if not 0 <= channel < MIDI_CHANNEL_LIMIT:
                    raise ValueError("channel")
                keys = [key for key in self._assignments.keys() | self._names.keys() if key[0] == channel]
                changed = bool(keys or self._pending and self._pending[0] == channel)
                for key in keys:
                    self._assignments.pop(key, None)
                    self._destinations.pop(key, None)
                    self._names.pop(key, None)
                    self._all_channels.discard(key)
                if self._pending is not None and self._pending[0] == channel:
                    self._pending = None
            if changed:
                self.revision += 1
                self._reindex_locked()
        if changed and notify:
            self._notify()
        return changed

    def observe_cc(self, channel: int, controller: int) -> tuple[str, ...]:
        """Return targets that consume this incoming CC; learning acts at once."""
        if not 0 <= channel < MIDI_CHANNEL_LIMIT or not 0 <= controller < 120:
            return ()
        with self.lock:
            capture = self._pending if self._pending is not None and (
                self._pending[0] == channel or self._pending_all) else None
            if capture:
                self._assign_locked(*capture, controller, self._pending_all, self._pending_destination)
            targets = self.targets_for_source(channel, controller)
        if capture:
            self._notify()
        return targets

    def ini_values(self):
        with self.lock:
            values = {}
            for (channel, target), sources in self._assignments.items():
                prefix = f"ch{channel + 1:02d}.{target}."
                values.update({prefix + "cc": str(next(iter(sources))),
                               prefix + "target": self._destinations.get((channel, target), target),
                               prefix + "all": str(int((channel, target) in self._all_channels))})
            for (channel, target), name in self._names.items():
                values[f"ch{channel + 1:02d}.{target}.name"] = name
            return values

    @_publish_channel_configuration
    def restore_ini(self, values, *, report=None):
        """Discard only malformed bindings; unrelated valid records survive."""
        callback, self.on_change = self.on_change, None
        try:
            self.reset()
            for key, value in values.items():
                if not key.endswith(".cc"):
                    continue
                channel, target = -1, ""
                try:
                    channel_key, target, _suffix = key.split(".")
                    if not channel_key.startswith("ch"):
                        continue
                    channel = int(channel_key[2:]) - 1
                    prefix = key[:-2]
                    self.assign(channel, target, int(value), destination=values.get(prefix + "target"))
                    if report is not None: report.record('MIDI CC',key,'accepted')
                    # Missing names are valid. Legacy saved names (including
                    # CustomN) have no origin metadata, so preserve rather
                    # than infer that they were automatically generated.
                    if prefix + "name" in values:
                        self.rename(channel, target, values[prefix + "name"])
                    if values.get(prefix + "all") == "1":
                        self._all_channels.add((channel, target))
                except (ValueError, KeyError, TypeError):
                    if report is not None: report.record('MIDI CC',key,'invalid')
                    if (channel, target) in self._assignments:
                        self.remove(channel, target)
            for key, name in values.items():
                if not key.endswith(".name") or key[:-4] + "cc" in values:
                    continue
                try:
                    channel_key, target, _suffix = key.split(".")
                    if channel_key.startswith("ch") and target.startswith("aux_"):
                        self.rename(int(channel_key[2:]) - 1, target, name)
                        if report is not None: report.record('MIDI CC',key,'accepted')
                except (ValueError, TypeError):
                    if report is not None: report.record('MIDI CC',key,'invalid')
                    continue
            if report is not None:
                for key in values:
                    if not any(entry[:2]==('MIDI CC',key) for entry in report.entries):
                        report.record('MIDI CC',key,'companion_or_unknown_preserved')
        finally:
            self.on_change = callback

    def snapshot(self):
        with self.lock:
            return (self.revision, self._pending,
                    {key: tuple(sorted(controllers))
                     for key, controllers in self._assignments.items()})

    def execution_snapshot(self):
        """Immutable control-side input for native semantic preparation.

        Destination order is the existing insertion order, not a sorted set:
        one CC can fan out to multiple controls and the last writer matters.
        A pending Learn is separate from established routes. Capturing its
        first eligible CC changes routing before that SAME CC is dispatched;
        an executor must not mistake this snapshot for a fixed capture plan.
        This method neither captures a CC nor notifies/saves configuration.
        """
        with self.lock:
            pending=(None if self._pending is None else
                (*self._pending,self._pending_all,self._pending_destination))
            return self.revision,pending,tuple(self.channels()),tuple(
                (channel,controller,targets) for (channel,controller),targets in self._dispatch.items())


@dataclass(frozen=True)
class PerformanceDetailSnapshot:
    scale_cents: tuple[int, ...]
    drum_mixes: tuple[tuple[int, DrumKeyMix], ...]
    expression: tuple[tuple[tuple[int, str], int], ...]


class PresetExpressionSeeds:
    """Optional preset defaults with native baseline restoration.

    Parameter-only MIXTIME syncs observe native DAW writes, not Note events.
    BASS_MIDI_StreamEvent does not trigger syncs, so our seed writes cannot
    overwrite the baseline. No widgets, file I/O or performance lock in callbacks.
    """
    def __init__(self, performance):
        self.performance = performance
        self.stream = 0
        self.handles = []
        self.native = {}
        self.applied = {}
        self.ready = False
        self.events = {EXPRESSION_EVENTS[field]: field for field in PRESET_EXPRESSION_RANGES}
        self.callback = SYNCPROC(self._observe)
        self.replay_callback = self._observe
        self.native_observer = None
        self.on_native = None

    def detach(self):
        detach_midi_sync_handles(self.performance.bass,self.stream,self.handles)
        if self.native_observer is not None:
            self.native_observer.close();self.native_observer=None
        self.stream = 0
        self.ready = False
        self.native.clear(); self.applied.clear()

    def bind(self, enabled):
        stream = self.performance.stream_getter()
        if not enabled or not stream:
            self.detach(); return
        if stream == self.stream and self.ready:
            return
        self.detach()
        if self.performance.bass is None:
            return  # Mock backend; native runtime always supplies BASS.
        self.stream = stream
        events=(*self.events,MIDI_EVENT_SYSTEM,MIDI_EVENT_RESET)
        self.native_observer=NativeMidiState(stream,events,self.replay_callback,clock=self.performance.native_clock)
        for event in events:
            callback,context=self.native_observer.registration(event,self.callback)
            handle = self.performance.bass.BASS_ChannelSetSync(stream, 0x40010004, event,
                callback, context)
            if not handle:
                error = self.performance.bass.BASS_ErrorGetCode()
                failure = RuntimeError(tr("group.extension_observer_failed", error=error))
                try:
                    self.detach()
                except Exception as cleanup:
                    failure.add_note(str(cleanup))
                    raise failure from cleanup
                raise failure
            self.handles.append(handle)
        self.ready = True

    def clear(self, channel=None):
        self.performance.drain_native()
        for state in (self.native, self.applied):
            for key in tuple(state):
                if channel is None or key[0] == channel:
                    state.pop(key, None)

    def _observe(self, _sync, stream, data, event):
        if stream != self.stream:
            return
        channel, raw = (data >> 16) & 0xffff, data & 0xffff
        if event == MIDI_EVENT_SYSTEM:
            self.clear(); return
        if event == MIDI_EVENT_RESET:
            # CC121 is limited: it does not reset fine tune/vibrato/routes.
            return
        field = self.events.get(event)
        if channel < MIDI_CHANNEL_LIMIT and field is not None:
            self.native[channel, field] = raw
            if self.on_native is not None:
                self.on_native(channel, field, raw)

    def apply(self, channel, values, *, force=False):
        self.performance.drain_native()
        if not self.performance.stream_getter():
            return
        old = {field: value for (ch, field), value in tuple(self.applied.items()) if ch == channel}
        for field in old.keys() - values.keys():
            raw = self.native.pop((channel, field))
            self.performance._emit(channel, EXPRESSION_EVENTS[field], raw, source="preset_restore")
            self.applied.pop((channel, field), None)
        for field, value in values.items():
            if field not in PRESET_EXPRESSION_RANGES:
                continue
            low, high, _default = PRESET_EXPRESSION_RANGES[field]
            amount = max(low, min(high, int(value)))
            if (channel, field) not in self.native:
                raw = (self.performance.mod_observed.get((channel, field), 0xffffffff)
                       if field.startswith("modulation_") else
                       self.performance.bassmidi.BASS_MIDI_StreamGetEvent(
                           self.performance.stream_getter(), channel, EXPRESSION_EVENTS[field]))
                if raw == 0xffffffff:
                    if not field.startswith('modulation_'):
                        # Supported GetEvent failed: no guessed native baseline.
                        raise RuntimeError(tr('group.extension_baseline_unavailable',field=field,
                            channel=channel%MIDI_CHANNELS_PER_PORT+1,port=channel//MIDI_CHANNELS_PER_PORT))
                    # MOD_* GetEvent unsupported in installed2.4.16: retain
                    # the documented seed policy, not a claimed native read.
                    raw=self.performance.expression_parameter(field,self.performance.expression_default(field))
                self.native[channel, field] = raw
            if force or old.get(field) != amount:
                self.performance._emit(channel, EXPRESSION_EVENTS[field],
                                       self.performance.expression_parameter(field, amount), source="soundfont_group")
            self.applied[channel, field] = amount


@dataclass(frozen=True,slots=True)
class DeferredPerformanceFailureLog:
    """Explicit product notification, never an arbitrary stateful callback.

    Its only consumer is the existing log/trace publication. Error records
    retain their event/DWORD and generation; audio never calls this Python.
    """
    owner: object

    def __call__(self,event,error):
        message=tr('runtime.performance_event_failed',event=event,error=error)
        trace=self.owner.diagnostic_trace
        if trace is not None:trace.record('app_log',message=str(message))
        try:self.owner.log_queue.put_nowait(str(message))
        except queue.Full:pass # Preserve existing append_log's bounded UI policy.


class MidiPerformanceControls:
    """ABMSynth tuning and per-key drum overlays for the live MIDI stream.

    bassmidi.md BASS_MIDI_StreamEvent: global master tuning uses channel 0;
    scale tuning is per channel, and drum-key events target MIDI channel 10.
    Later DAW events for the same parameter can supersede these settings.
    """

    def __init__(self, bassmidi, stream_getter, failure_sink=None, bass=None, observer_failure_sink=None):
        self.failure_sink_commit_scope=lambda _old,_new:nullcontext()
        self.channels = lambda: range(MIDI_CHANNELS_PER_PORT)
        self.is_drum = lambda channel: channel % MIDI_CHANNELS_PER_PORT == 9
        self.bassmidi, self.stream_getter = bassmidi, stream_getter
        self.failure_sink, self.bass = failure_sink, bass
        self.observer_failure_sink = observer_failure_sink
        self.master_cents = 0
        self.master_semitones = 0
        self.reset_commit_scope = nullcontext
        self.scale_cents = [0] * 12
        self.drum_mixes: dict[int, DrumKeyMix] = {}
        self.soundfont_profile = lambda channel: None
        self._profile_values = {}
        self.expression: dict[tuple[int, str], int] = {}
        # MOD_* GetEvent unsupported in2.4.16: selective syncs / _emit track
        # exact raw state without acquiring this lock inside a BASS callback.
        self.mod_observed = {}
        self.mod_sync_stream = 0
        self.mod_sync_ready = False
        self.mod_sync_failed_stream = 0
        self.mod_sync_handles = []
        self.mod_sync_callback = SYNCPROC(self._observe_mod_sync)
        self.mod_replay_callback = self._observe_mod_sync
        self.mod_native_observer = None
        self.native_clock = None
        self.lock = threading.RLock()
        self.preset_seeds = PresetExpressionSeeds(self)
        self.on_emit = None
        self.drain_native = self.drain_native_observers

    @property
    def failure_sink(self):return getattr(self,'_failure_sink',None)

    @failure_sink.setter
    def failure_sink(self,value):
        # Generic callable support is unchanged. A native product admission
        # installs an ordered publication hook instead of inferring purity.
        with self.failure_sink_commit_scope(getattr(self,'_failure_sink',None),value):
            self._failure_sink=value

    def drain_native_observers(self):
        if self.native_clock is not None:
            return self.native_clock.drain((self.mod_native_observer,self.preset_seeds.native_observer))
        ready=True
        for observer in (self.mod_native_observer,self.preset_seeds.native_observer):
            if observer is not None:ready=observer.drain() and ready
        return ready

    @staticmethod
    def fine14(cents: int | float) -> int:
        """Map -100..+100 cents to the 14-bit neutral 8192 domain."""
        if not math.isfinite(float(cents)):
            raise ValueError("Invalid master tuning")
        value = max(-100.0, min(100.0, float(cents)))
        return round((value + 100) * 16383 / 200)

    def _observe_mod_sync(self, _sync, stream, data, event):
        # bassmidi.md MIDI syncs: LOWORD=data parameter, HIWORD=MIDI channel.
        # Only five subscribed event types enter Python, not every Note/CC.
        if stream != self.mod_sync_stream:
            return
        if event == MIDI_EVENT_SYSTEM:
            self.mod_observed.clear()
        elif event in MODULATION_NATIVE_FIELDS and data >> 16 < MIDI_CHANNEL_LIMIT:
            self.mod_observed[data >> 16, MODULATION_NATIVE_FIELDS[event]] = data & 0xffff

    def observe_native_stream(self, stream):
        if self.bass is None:
            return
        if self.mod_sync_stream == stream and self.mod_sync_ready:
            return True
        if self.mod_sync_failed_stream == stream and stream:
            return False
        self.detach_native_observers(preset=False)
        self.mod_observed.clear()
        self.mod_sync_stream = stream
        events=(*MODULATION_NATIVE_FIELDS,MIDI_EVENT_SYSTEM)
        self.mod_native_observer=NativeMidiState(stream,events,self.mod_replay_callback,clock=self.native_clock)
        for event in events:
            # bass.h MIXTIME0x40000000, bassmidi.h MIDI_EVENT sync0x10004.
            callback,context=self.mod_native_observer.registration(event,self.mod_sync_callback)
            handle = self.bass.BASS_ChannelSetSync(stream, 0x40010004, event,
                                                 callback, context)
            if not handle:
                error = self.bass.BASS_ErrorGetCode()
                self.mod_sync_failed_stream = stream
                try:
                    self.detach_native_observers(preset=False)
                except Exception as cleanup:
                    failure = RuntimeError(tr("capability.modulation_observer_failed", error=error))
                    failure.add_note(str(cleanup))
                    raise failure from cleanup
                # Observation failure must never interrupt MIDI forwarding.
                # Report once per source generation; keep manual edits usable.
                self.mod_sync_failed_stream = stream
                if self.observer_failure_sink is not None:
                    self.observer_failure_sink(error)
                return False
            self.mod_sync_handles.append(handle)
        self.mod_sync_ready = True
        return True

    def detach_native_observers(self, *, preset=True):
        if preset:
            self.preset_seeds.detach()
        detach_midi_sync_handles(self.bass,self.mod_sync_stream,self.mod_sync_handles)
        if self.mod_native_observer is not None:
            self.mod_native_observer.close();self.mod_native_observer=None
        self.mod_sync_stream = 0
        self.mod_sync_ready = False
        self.mod_sync_failed_stream = 0

    def _emit(self, channel: int, event: int, param: int, *, source=None) -> bool:
        self.drain_native()
        stream = self.stream_getter()
        if stream and channel in self.channels():
            if not self.bassmidi.BASS_MIDI_StreamEvent(stream, channel, event, param):
                if self.failure_sink is not None:
                    error = int(self.bass.BASS_ErrorGetCode()) if self.bass is not None else 0
                    self.failure_sink(event, error)
                return False
            # Syncs preceding this manual StreamEvent must replay BEFORE its
            # new manual baseline/source, not later repaint over the edit.
            self.drain_native()
            if event in MODULATION_NATIVE_FIELDS:
                self.mod_observed[channel, MODULATION_NATIVE_FIELDS[event]] = param
            if self.on_emit is not None:
                self.on_emit(channel, event, param, source)
            return True
        return False

    def set_master(self, cents: int | float) -> None:
        value = MASTER_FINE_PARAMETER.clamp(cents)
        # No-op inspection is not a source publication. Recheck after taking
        # ownership, since a real setter still owns the original reset fence.
        with self.lock:
            if value == self.master_cents:
                return
        with self.reset_commit_scope(), self.lock:
            if value == self.master_cents:
                return
            self.master_cents = value
            self._emit(0, MASTER_FINE_PARAMETER.event, MASTER_FINE_PARAMETER.parameter(0,value))

    def set_master_coarse(self, semitones: int | float) -> None:
        """Apply a global ±24-semitone transposition, independently of A4 fine tune."""
        value = MASTER_COARSE_PARAMETER.clamp(semitones)
        with self.lock:
            if value == self.master_semitones:
                return
        with self.reset_commit_scope(), self.lock:
            if value == self.master_semitones:
                return
            self.master_semitones = value
            self._emit(0, MASTER_COARSE_PARAMETER.event, MASTER_COARSE_PARAMETER.parameter(0,value))

    def set_scale(self, pitch_class: int, cents: int | float) -> None:
        if not 0 <= pitch_class < 12:
            raise ValueError("pitch_class")
        with self.lock:
            value = SCALE_PARAMETER.clamp(cents)
            if value == self.scale_cents[pitch_class]:
                return
            self.scale_cents[pitch_class] = value
            param = SCALE_PARAMETER.parameter(pitch_class,value)
            for channel in self.channels():
                self._emit(channel, MIDI_EVENT_SCALETUNING, param)

    def drum_mix(self, key: int) -> DrumKeyMix:
        if not 0 <= key <= 127:
            raise ValueError("drum_key")
        with self.lock:
            return self.drum_mixes.get(key, DRUM_NEUTRAL)

    def expression_value(self, channel: int, field: str) -> int:
        if not 0 <= channel < MIDI_CHANNEL_LIMIT or field not in EXPRESSION_EVENTS:
            raise ValueError("expression")
        with self.lock:
            if (channel, field) in self.expression:
                return self.expression[channel, field]
            return self.expression_default(field)

    def expression_default(self, field: str) -> int:
        """Return the active system's neutral pressure-vibrato route.

        BASSMIDI documents DEFAULT/GM at raw 64 but GS/XG at raw 0.
        This event is not covered by CC121; resetting it to a hard-coded
        64 after a GS SysEx would audibly change the new system's sound.
        """
        if field == "channel_pressure_vibrato":
            stream = self.stream_getter()
            if stream and hasattr(self.bassmidi, "BASS_MIDI_StreamGetEvent") and self.bassmidi.BASS_MIDI_StreamGetEvent(stream, 0, MIDI_EVENT_SYSTEM) in (3, 4, 5):
                return 0
        return EXPRESSION_DEFAULTS[field]

    @classmethod
    def expression_parameter(cls, field: str, value: int) -> int:
        """Encode display units only at the BASSMIDI event boundary.

        bassmidi.h/StreamEvent: RPN1 uses 0..16383 (8192 neutral), RPN2
        uses 0..127 (64 neutral); all other expression values are native.
        """
        if field == "fine_tuning":
            return cls.fine14(value)
        if field == "coarse_tuning":
            return 64 + value
        if field == "transpose":
            return 100 + value
        if field.endswith("_pitch"):
            return 24 + value
        if field.endswith("_filter"):
            return 9600 + value
        if field.endswith("_volume"):
            return 100 + value
        if field.endswith("_vibrato"):
            return round(value * 128 / 100)
        return value

    @classmethod
    def expression_from_parameter(cls, field: str, value: int) -> int:
        """Decode native channel readback for the live editor, not INI state."""
        if field == "fine_tuning":
            return round(value * 200 / 16383 - 100)
        if field == "coarse_tuning":
            return value - 64
        if field == "transpose":
            return value - 100
        if field.endswith("_pitch"):
            return value - 24
        if field.endswith("_filter"):
            return value - 9600
        if field.endswith("_volume"):
            return value - 100
        if field.endswith("_vibrato"):
            return round(value * 100 / 128)
        return value

    def expression_readback(self, channel: int, field: str, *, presentation=False) -> ChannelReadback:
        """Show the DAW's actual latest event while retaining saved edits."""
        if not 0 <= channel < MIDI_CHANNEL_LIMIT or field not in EXPRESSION_EVENTS:
            raise ValueError("expression")
        ready = self.drain_native()
        if not ready and not presentation:
            return ChannelReadback(self.expression_default(field),False,'unavailable')
        stream = self.stream_getter()
        if field.startswith("modulation_"):
            if stream and not ready:
                return ChannelReadback(self.expression_default(field), False, 'pending')
            raw = self.mod_observed.get((channel, field))
            return ChannelReadback(self.expression_from_parameter(field,raw) if raw is not None else self.expression_default(field),
                                   raw is not None or not stream,'processed' if raw is not None else 'factory' if not stream else 'unavailable')
        if stream and hasattr(self.bassmidi, "BASS_MIDI_StreamGetEvent"):
            # bassmidi.h DWORD GetEvent: -1 means failure. Metadata replay
            # retry does not invalidate this native current-value read. Only
            # display reads bypass the aggregate gate; setter/state reads do not.
            raw = self.bassmidi.BASS_MIDI_StreamGetEvent(stream, channel,
                                                         EXPRESSION_EVENTS[field])
            if raw != 0xFFFFFFFF:
                # Entry limits constrain manual/explicit CC edits,not valid
                # incoming native state. Observation must not falsify RPN48
                # as24 or silently alter the sounding value on refresh.
                return ChannelReadback(self.expression_from_parameter(field, raw),True,
                    'native' if ready else 'native_pending_metadata')
        return ChannelReadback(self.expression_value(channel,field),not stream,'factory' if not stream else 'unavailable')

    def observed_expression_value(self, channel: int, field: str) -> int:
        # Legacy setters/comparisons retain their numeric compatibility value;
        # presentation uses the separately qualified readback, never a fake0.
        return self.expression_readback(channel,field).value

    def set_expression(self, channel: int, field: str, value: int | float) -> None:
        if not 0 <= channel < MIDI_CHANNEL_LIMIT or field not in EXPRESSION_EVENTS:
            raise ValueError("expression")
        amount = max(EXPRESSION_MINIMUMS[field],
                     min(EXPRESSION_MAXIMUMS[field], int(round(float(value)))))
        if field == "portamento":
            amount = 127 if amount >= 64 else 0
        with self.lock:
            if amount == self.expression_value(channel, field) and amount == self.observed_expression_value(channel, field):
                return
            if amount == self.expression_default(field):
                self.expression.pop((channel, field), None)
            else:
                self.expression[channel, field] = amount
            self._emit(channel, EXPRESSION_EVENTS[field],
                       self.expression_parameter(field, amount))

    def set_drum(self, key: int, field: str, value: int | float) -> None:
        if not 0 <= key <= 127 or field not in DRUM_MIX_EVENTS:
            raise ValueError("drum_mix")
        parameter=DRUM_PARAMETERS[field]
        number = parameter.clamp(value)
        with self.lock:
            previous = self.drum_mixes.get(key, DRUM_NEUTRAL)
            if getattr(previous, field) == number:
                return
            updated = replace(previous, **{field: number})
            if updated == DRUM_NEUTRAL:
                self.drum_mixes.pop(key, None)
            else:
                self.drum_mixes[key] = updated
            for channel in self.channels():
                if self.is_drum(channel):
                    self._emit(channel, parameter.event, parameter.parameter(key,number))

    def ini_values(self, detail_snapshot: PerformanceDetailSnapshot | None = None) -> dict[str, str]:
        """Current INI owns master tuning and channel expression only."""
        with self.lock:
            expression = self.expression.items()
            return {
                "tuning_master": str(self.master_cents),
                "tuning_coarse": str(self.master_semitones),
                **{f"expression_{channel}_{field}": str(value)
                   for (channel, field), value in expression},
            }

    def has_overrides(self) -> bool:
        with self.lock:
            return bool(self.master_cents or self.master_semitones
                        or any(self.scale_cents) or self.drum_mixes or self.expression)

    def has_master_overrides(self) -> bool:
        with self.lock:
            return bool(self.master_cents or self.master_semitones)

    def has_scale_overrides(self) -> bool:
        with self.lock:
            return any(self.scale_cents)

    def has_drum_overrides(self) -> bool:
        with self.lock:
            return bool(self.drum_mixes)

    def has_expression_overrides(self, channel: int | None = None) -> bool:
        with self.lock:
            return (bool(self.expression) if channel is None else
                    any(mapped_channel == channel for mapped_channel, _field in self.expression))

    def detail_snapshot(self) -> PerformanceDetailSnapshot:
        with self.lock:
            return PerformanceDetailSnapshot(
                tuple(self.scale_cents), tuple(self.drum_mixes.items()),
                tuple(self.expression.items()),
            )

    def restore_detail(self, snapshot: PerformanceDetailSnapshot) -> None:
        """Undo only detail edits; master tuning belongs to the main tab."""
        if not isinstance(snapshot, PerformanceDetailSnapshot):
            raise TypeError("performance detail snapshot")
        with self.lock:
            for pitch, cents in enumerate(snapshot.scale_cents):
                self.set_scale(pitch, cents)
            saved_drums = dict(snapshot.drum_mixes)
            for key in sorted(self.drum_mixes.keys() | saved_drums.keys()):
                desired = saved_drums.get(key, DRUM_NEUTRAL)
                for field in DRUM_MIX_EVENTS:
                    self.set_drum(key, field, getattr(desired, field))
            saved_expression = dict(snapshot.expression)
            for channel, field in sorted(self.expression.keys() | saved_expression.keys()):
                self.set_expression(channel, field, saved_expression.get(
                    (channel, field), EXPRESSION_DEFAULTS[field]))

    def restore_scale_drum(self, snapshot: PerformanceDetailSnapshot) -> None:
        """Cancel only the transactional detail pages, not live channel edits."""
        if not isinstance(snapshot, PerformanceDetailSnapshot):
            raise TypeError("performance detail snapshot")
        with self.lock:
            for pitch, cents in enumerate(snapshot.scale_cents):
                self.set_scale(pitch, cents)
            saved_drums = dict(snapshot.drum_mixes)
            for key in sorted(self.drum_mixes.keys() | saved_drums.keys()):
                desired = saved_drums.get(key, DRUM_NEUTRAL)
                for field in DRUM_MIX_EVENTS:
                    self.set_drum(key, field, getattr(desired, field))

    def reset_master(self) -> None:
        with self.lock:
            self.set_master(0)
            self.set_master_coarse(0)

    def reset_scale(self) -> None:
        with self.lock:
            for pitch in range(12):
                self.set_scale(pitch, 0)

    def reset_drums(self) -> None:
        with self.lock:
            for key, mix in tuple(self.drum_mixes.items()):
                for field in DRUM_MIX_EVENTS:
                    if getattr(mix, field) != getattr(DRUM_NEUTRAL, field):
                        self.set_drum(key, field, getattr(DRUM_NEUTRAL, field))

    def reset_expression(self, channel: int | None = None, *, apply=True) -> None:
        self.drain_native()
        with self.lock:
            if not apply:
                self.mod_observed = {key: value for key, value in self.mod_observed.items()
                                     if channel is not None and key[0] != channel}
            for (mapped_channel, field), value in tuple(self.expression.items()):
                if channel is not None and mapped_channel != channel:
                    continue
                if apply and value != self.expression_default(field):
                    self.set_expression(mapped_channel, field, self.expression_default(field))
                else:
                    self.expression.pop((mapped_channel, field), None)

    def apply_expression_defaults(self, channel: int) -> None:
        """Clear every channel shaping event, not only CC121's subset.

        BASSMIDI StreamEvent documents CC121's limited reset list; RPN
        tuning, bend range, decay and vibrato need explicit defaults. The
        preset-specific SoundFont controls are reapplied afterward.
        """
        if not 0 <= channel < MIDI_CHANNEL_LIMIT:
            raise ValueError("channel")
        with self.lock:
            for field, event in EXPRESSION_EVENTS.items():
                self._emit(channel, event, self.expression_parameter(
                    field, self.expression_default(field)))

    def restore_ini(self, values: dict[str, str], *, report=None) -> None:
        """Restore state without sending events before a stream exists."""
        if report is not None:
            # Validate the same wire/save domains without exposing saved values.
            # Scale/drum instrument profiles belong to SFG,not this INI.
            for name, raw in values.items():
                state = 'accepted'
                try:
                    if name == 'tuning_master':
                        MASTER_FINE_PARAMETER.validate(float(raw))
                    elif name == 'tuning_coarse':
                        MASTER_COARSE_PARAMETER.validate(int(raw))
                    elif name.startswith('expression_'):
                        channel,field=name[11:].split('_',1)
                        if field not in EXPRESSION_EVENTS:
                            state='unknown_preserved'
                        elif (not 0<=int(channel)<MIDI_CHANNEL_LIMIT or
                              not EXPRESSION_MINIMUMS[field]<=int(raw)<=EXPRESSION_MAXIMUMS[field] or
                              (field=='portamento' and int(raw) not in (0,127))): raise ValueError()
                    else: continue
                except (ValueError,TypeError,OverflowError): state='invalid'
                report.record('UI',name,state)
        with self.lock:
            self.master_cents = 0
            self.master_semitones = 0
            self.scale_cents = [0] * 12
            self.drum_mixes.clear()
            self.expression.clear()
            try:
                self.master_cents = MASTER_FINE_PARAMETER.validate(float(values.get("tuning_master", "0")))
            except ValueError:
                pass
            try:
                self.master_semitones = MASTER_COARSE_PARAMETER.validate(int(values.get("tuning_coarse", "0")))
            except ValueError:
                pass
            for channel in range(MIDI_CHANNEL_LIMIT):
                for field in EXPRESSION_EVENTS:
                    raw = values.get(f"expression_{channel}_{field}")
                    if raw is None:
                        continue
                    try:
                        amount = int(raw)
                    except ValueError:
                        continue
                    if (EXPRESSION_MINIMUMS[field] <= amount <= EXPRESSION_MAXIMUMS[field]
                            and (field != "portamento" or amount in (0, 127))
                            and amount != EXPRESSION_DEFAULTS[field]):
                        self.expression[channel, field] = amount

    def apply_soundfont_profile(self, channel, profile, *, force=True):
        """Group -> instrument replacement, never an additive DSP layer.

        BASSMIDI scale is per channel; drum NRPN includes key in the low byte.
        Restore removed profile keys too, so switching instruments/groups does
        not leak the previous instrument's exceptions. Future MIDI wins until
        an explicit document apply/program/reset reapplies these seeds.
        """
        if profile is None:
            profile = {}
        active = {key: value for key, value in profile.items()
                  if not key.startswith("drum_") or self.is_drum(channel)}
        previous = self._profile_values.get(channel, {})
        for key in sorted(previous.keys() | active.keys()):
            kind, index = performance_profile_key(key)
            parameter = SCALE_PARAMETER if kind == "scale" else DRUM_PARAMETERS[kind]
            value = active.get(key, parameter.default)
            if force or value != previous.get(key, parameter.default):
                self._emit(channel, parameter.event, parameter.parameter(index, value))
        self._profile_values[channel] = dict(active)

    def apply_all(self) -> None:
        """Restore user overrides after a reset or a new stream generation."""
        with self.lock:
            if self.master_cents:
                self._emit(0, MASTER_FINE_PARAMETER.event, MASTER_FINE_PARAMETER.parameter(0,self.master_cents))
            if self.master_semitones:
                self._emit(0, MASTER_COARSE_PARAMETER.event, MASTER_COARSE_PARAMETER.parameter(0,self.master_semitones))
            for channel in self.channels():
                self.apply_soundfont_profile(channel, self.soundfont_profile(channel))
            # The app's only non-neutral factory controller default is CC65=on.
            # CC5 stays untouched; a later DAW CC65 (including off) wins.
            for channel in self.channels():
                self._emit(channel, MIDI_EVENT_PORTAMENTO,
                           self.expression.get((channel, "portamento"), 127))
            for (channel, field), amount in self.expression.items():
                if field == "portamento":
                    continue
                self._emit(channel, EXPRESSION_EVENTS[field],
                           self.expression_parameter(field, amount))

    def apply_channel_settings(self, channel):
        """Seed newly allocated channels without rewriting existing DAW state."""
        with self.lock:
            self.apply_soundfont_profile(channel, self.soundfont_profile(channel))
            self._emit(channel, MIDI_EVENT_PORTAMENTO, self.expression.get((channel, "portamento"), 127))
            for (selected, field), amount in self.expression.items():
                if selected == channel and field != "portamento":
                    self._emit(channel, EXPRESSION_EVENTS[field], self.expression_parameter(field, amount))


class _NativeCCAction(ctypes.Structure):
    _fields_=[('packed',ctypes.c_uint),('channel',ctypes.c_uint),('event',ctypes.c_uint),('raw',ctypes.c_uint),
        ('accepted',ctypes.c_uint),('error',ctypes.c_int),('qpc',ctypes.c_uint64)]


class NativeCCCaptureQueue:
    """Non-coalesced config receipts in the common capture-clock aggregate.

    Native peek never consumes configuration; all delegates must succeed before
    the aggregate commits cursors/strict FIFO ACK. Mutation and notification
    checkpoints prevent a later state replay error from repeating assignment.
    This is control-side metadata, never a MIDI/FontLoad/audio callback.
    """
    def __init__(self,kernel):
        self.kernel=kernel;self.clock=kernel.owner.midi_clock;self.stream=kernel.owner.stream_handle
        self.busy=False;self._serial=0;self._pending={};self._stages={};self.rows=(ctypes.c_uint64*(256*12))()
        library=kernel.library
        self.snapshot=library.midi_cc_kernel_capture_snapshot
        self.snapshot.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint];self.snapshot.restype=ctypes.c_int
        self.ack=library.midi_cc_kernel_capture_ack;self.ack.argtypes=[ctypes.c_void_p,ctypes.c_uint64];self.ack.restype=ctypes.c_int
        self.retained=False
        version=getattr(library,'midi_cc_kernel_capture_retained_version',None)
        prepare=getattr(library,'midi_cc_kernel_capture_retained_prepare',None)
        if version is not None and prepare is not None:
            version.argtypes=[];version.restype=ctypes.c_uint
            prepare.argtypes=[ctypes.c_void_p];prepare.restype=ctypes.c_int
            if version()==1:
                with kernel.lock:
                    self.retained=bool(kernel.pointer and prepare(kernel.pointer))
                if self.retained:self.rows=(ctypes.c_uint64*(4096*12))()

    @property
    def pointer(self):return self.kernel.pointer

    @property
    def serial(self):return self._serial

    @serial.setter
    def serial(self,value):
        with self.kernel.lock:
            for sequence in sorted(tuple(self._pending)):
                if sequence>value:continue
                if not self.ack(self.pointer,sequence):raise RuntimeError('native capture FIFO ACK')
                self._pending.pop(sequence,None);self._stages.pop(sequence,None)
            self._serial=value

    def revision(self):
        prepared=self.pending_rows()
        if prepared is None:return self._serial,True
        return prepared[1],self.busy

    def pending_rows(self):
        with self.kernel.lock:
            if not self.pointer:return (),self._serial
            if self.retained:
                pending=self.kernel.library.midi_cc_kernel_capture_count
                pending.argtypes=[ctypes.c_void_p];pending.restype=ctypes.c_uint
                capacity=max(1,int(pending(self.pointer)))
                if capacity>len(self.rows)//12:self.rows=(ctypes.c_uint64*(capacity*12))()
                count=int(self.snapshot(self.pointer,self.rows,len(self.rows)//12))
            else:count=int(self.snapshot(self.pointer,self.rows,256))
            if count<0:self.busy=True;return None
            # The ctypes buffer and pending map share the ACK lock. Copying
            # outside it can resurrect an already ACKed row from a GUI peek.
            values=tuple(tuple(int(value) for value in self.rows[index*12:index*12+12]) for index in range(count))
            values=tuple(row for row in values if row[0]>self._serial)
            for row in values:self._pending[row[0]]=row
            return tuple((row[0],0,row[0]) for row in values),max((row[0] for row in values),default=self._serial)

    def delegate(self,_sync,stream,sequence,_event):
        if stream!=self.stream:raise ValueError('native capture stream')
        row=self._pending[sequence];stage=self._stages.get(sequence,0)
        learn=self.kernel.owner.midi_learn;keys=self.kernel.capture_keys
        if row[6] in (128,130):
            owner=self.kernel.owner
            if stage==0:
                with learn.lock:
                    if learn.revision!=row[1]:raise ValueError('native GM reset Learn revision')
                    learn.cancel()
                    if learn.revision!=row[2]:raise ValueError('native GM reset Learn successor')
                self._stages[sequence]=1;stage=1
            if stage==1:
                # Every effect below is metadata only. Native timing/audio and
                # tap sends already completed at their exact source phases.
                if row[6]==128:owner.control_origins.clear()
                owner.midi_options.reset_notes();owner.midi_activity.clear()
                with owner.performance.lock:owner.performance.expression.clear();owner.performance.mod_observed.clear()
                owner.channel_policy.clear_session()
                if row[6]==128:
                    owner.channel_reset_revision+=1
                    owner.channel_sends.channel_reverb=[owner.channel_sends.default_reverb]*MIDI_CHANNEL_LIMIT
                    owner.channel_sends.channel_chorus=[0]*MIDI_CHANNEL_LIMIT
                    for item in (*owner.delay_runtime.states,owner.delay_runtime.melodic_nine):item.send=0
                owner.soundfont_activity.clear_active()
                self._stages[sequence]=2
            return
        if row[6]==131:
            owner=self.kernel.owner
            owner.channel_sends.channel_reverb=[owner.channel_sends.default_reverb]*MIDI_CHANNEL_LIMIT
            owner.channel_sends.channel_chorus=[0]*MIDI_CHANNEL_LIMIT
            return
        if row[6]==132:
            owner=self.kernel.owner
            for item in (*owner.delay_runtime.states,owner.delay_runtime.melodic_nine):item.send=0
            return
        if row[6]==129:
            owner=self.kernel.owner;delay=owner.delay_runtime
            for item in (*delay.states,delay.melodic_nine):item.set_parameters(3,100,340,35)
            with delay._ui_lock:delay._ui_updates.update(delay_type=3,delay_level=100,delay_time=340,delay_feedback=35,preset='Default')
            return
        if row[6]==133:
            # Original SysEx audio/tap publication already completed natively.
            # Only ordered Python/UI shadow changes here; no audio resubmission.
            from delay_runtime import BMSYNTH_DELAY_PRESETS
            owner=self.kernel.owner;delay=owner.delay_runtime;kind,value=row[3:5]
            if kind==1:
                name=tuple(BMSYNTH_DELAY_PRESETS)[value];values=BMSYNTH_DELAY_PRESETS[name]
                for item in (*delay.states,delay.melodic_nine):item.set_parameters(*values)
                with delay._ui_lock:
                    delay._ui_updates.update(zip(('delay_type','delay_level','delay_time','delay_feedback'),values))
                    delay._ui_updates['preset']=name
            elif kind in (2,3):
                for item in delay.states:
                    delay_type,level,time_ms,feedback=item._parameters
                    item.set_parameters(delay_type,level,value if kind==2 else time_ms,value if kind==3 else round(feedback*100))
                with delay._ui_lock:delay._ui_updates['delay_time' if kind==2 else 'delay_feedback']=value
            else:raise ValueError('native SysEx delay capture kind')
            return
        if row[6]==134:
            # File Reset clears exactly this active channel at its original
            # source phase. Audio, native activity and provenance are finished.
            owner=self.kernel.owner;channel=row[3]
            if stage==0:
                with learn.lock:
                    if learn.revision!=row[1]:raise ValueError('file reset Learn revision')
                    if learn._pending is not None and learn._pending[0]==channel:
                        learn._pending=None;learn.revision+=1
                    if learn.revision!=row[2]:raise ValueError('file reset Learn successor')
                self._stages[sequence]=1
            owner.midi_options.reset_notes(channel);owner.midi_activity.clear(channel)
            owner.soundfont_activity.clear_channel(channel)
            with owner.performance.lock:
                for values in (owner.performance.expression,owner.performance.mod_observed):
                    for key in tuple(values):
                        if key[0]==channel:values.pop(key,None)
            return
        if row[6]==121:
            # A non-coalesced pre-RAW receipt: auxiliary zero cells precede
            # this record, and later same-frame CC cells remain last writers.
            if stage==0:
                with learn.lock:
                    if learn.revision!=row[1]:raise ValueError('native reset Learn revision')
                    if learn._pending is not None and learn._pending[0]==row[3]:
                        learn._pending=None;learn.revision+=1
                    if learn.revision!=row[2]:raise ValueError('native reset Learn successor')
                self._stages[sequence]=1;stage=1
            if stage==1:
                self.kernel.owner.channel_reset_revision+=1
                self._stages[sequence]=2
            return
        if stage==0:
            # A latest auxiliary cell may already contain a later same-frame
            # value. Capture notification needs THIS first eligible CC value.
            self.kernel.owner.auxiliary_cc.receive(row[10],row[6],row[11])
            with learn.lock:
                if learn.revision!=row[1] or row[4]>=len(keys):raise ValueError('native capture configuration revision')
                target=keys[row[4]];destination=None if row[5]==0xffffffff else keys[row[5]]
                if learn._pending!=(row[3],target) or learn._pending_all!=bool(row[7]) or learn._pending_destination!=destination:
                    raise ValueError('native capture pending identity')
                if tuple(learn.channels())!=tuple(range(row[8])):raise ValueError('native capture address scope changed')
                learn._assign_locked(row[3],target,row[6],bool(row[7]),destination)
                if learn.revision!=row[2]:raise ValueError('native capture successor revision')
            self._stages[sequence]=1;stage=1
        if stage==1:
            learn._notify();self._stages[sequence]=2

    def drain(self):return self.clock.drain((self,))


class NativeCCKernel:
    """Prepared CC execution for a native MIDI worker.

    One call executes RAW boundaries/CC6/send/delay without per-CC Python.
    Scalar Learn/special-send functions use the optional semantic ABI below.
    Armed capture/mode/selection/reset paths remain qualification gaps,
    not a reduced completion criterion. Product does not instantiate this.
    Engine serialization and future external native stop/join barriers must
    retain all functions, input/output rows and native borrowers until done.
    """
    def __init__(self,bassmidi,bass,send_domain,channels=128,path=None,*,semantic_failure_capacity=None):
        if semantic_failure_capacity is not None and (type(semantic_failure_capacity) is not int or not 1<=semantic_failure_capacity<=65536):
            raise ValueError(tr('runtime.midi_state_snapshot_failed',error='semantic failure capacity'))
        self.semantic_failure_capacity=semantic_failure_capacity
        self.pointer=None;self.library=None;self.lock=threading.RLock();self.pending=None;self.journal=None;self.owner=None;self.notes=None
        self.semantic_journal=None;self.semantic_decoders={};self.semantic_create=None;self.semantic_error_low={};self.semantic_gets=[]
        self.semantic_fields=tuple(CHANNEL_FUNCTION_TARGETS);self.semantic_source='native_midi'
        self.manual_journal=None;self.manual_ready=False;self._manual_requested=False;self.manual_signature=None
        self.semantic_mode_seed=None;self.semantic_mode_error_low={};self.semantic_mode_error={}
        self.semantic_failure_sink=None
        self.failure_notification_admitted=False
        self.first_use_plan=None
        self._frame_owner=None
        self.semantic_arm_prepare=None;self.semantic_actions=None;self.semantic_timbre_watch=None;self.passive_raw_ready=False
        self.reset_prepare=None;self.reset_scalar_prepare=None;self.reset_journal=None;self.reset_contract=None;self._reset_requested=False;self._compound_reset_requested=False
        self.capture_keys=tuple(dict.fromkeys((*CHANNEL_FUNCTION_TARGETS,*CHANNEL_MODE_SWITCHES,*(f'aux_{cc}' for cc in AUXILIARY_CC))))
        self.send_domain=send_domain;self.channels=channels
        self.channel_flags_function=None
        self.functions=(bassmidi.BASS_MIDI_StreamEvents,bassmidi.BASS_MIDI_StreamEvent,bass.BASS_ChannelLock,bass.BASS_ErrorGetCode)
        try:
            library=ctypes.CDLL(str(path or os.environ.get('ABMSYNTH_OBSERVATION_DLL',Path(__file__).with_name('abmsynth_dsp.dll'))))
            library.midi_cc_kernel_version.argtypes=[];library.midi_cc_kernel_version.restype=ctypes.c_uint
            if library.midi_cc_kernel_version()!=1:return
            if semantic_failure_capacity is not None:
                version=getattr(library,'midi_cc_kernel_semantic_failure_version',None)
                if version is None:raise ValueError(tr('runtime.midi_state_snapshot_failed',error='semantic failure producer ABI'))
                version.argtypes=[];version.restype=ctypes.c_uint
                if version()!=1:raise ValueError(tr('runtime.midi_state_snapshot_failed',error='semantic failure producer ABI'))
            if not send_domain.pointer or send_domain.library._handle!=library._handle:
                raise ValueError('midi_cc_kernel send domain DLL image')
            for name,args,result in (
                ('create',[ctypes.c_uint]+[ctypes.c_void_p]*5,ctypes.c_void_p),
                ('destroy',[ctypes.c_void_p],ctypes.c_int),
                ('journal',[ctypes.c_void_p,ctypes.c_void_p],ctypes.c_int),
                ('configure',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint],ctypes.c_int),
                ('delay',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_void_p],ctypes.c_int),
                ('retry',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_int)],ctypes.c_int),
                ('run',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint,
                    ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(_NativeCCAction),ctypes.c_uint,ctypes.POINTER(ctypes.c_uint64)],ctypes.c_int)):
                function=getattr(library,'midi_cc_kernel_'+name);function.argtypes=args;function.restype=result
            assert ctypes.sizeof(_NativeCCAction)==32
            if not all(isinstance(function,ctypes._CFuncPtr) for function in self.functions):return
            self.pointer=library.midi_cc_kernel_create(channels,send_domain.pointer,*(ctypes.cast(fn,ctypes.c_void_p) for fn in self.functions))
            if self.pointer:
                self.library=library
                version=getattr(library,'midi_cc_kernel_passive_raw_version',None)
                if version is not None:
                    version.argtypes=[];version.restype=ctypes.c_uint
                    self.passive_raw_ready=version()==1
                flags_attach=getattr(library,'midi_cc_kernel_channel_flags',None)
                flags_function=getattr(bass,'BASS_ChannelFlags',None)
                if flags_attach is not None and isinstance(flags_function,ctypes._CFuncPtr):
                    flags_attach.argtypes=[ctypes.c_void_p,ctypes.c_void_p];flags_attach.restype=ctypes.c_int
                    if not flags_attach(self.pointer,ctypes.cast(flags_function,ctypes.c_void_p)):raise ValueError('midi_cc_kernel ChannelFlags attachment')
                    self.channel_flags_function=flags_function
                queue_attach=getattr(library,'midi_cc_kernel_selection_queue_reader',None)
                queue_reader=getattr(bass,'BASS_ChannelGetAttribute',None)
                if queue_attach is not None and isinstance(queue_reader,ctypes._CFuncPtr):
                    queue_attach.argtypes=[ctypes.c_void_p,ctypes.c_void_p];queue_attach.restype=ctypes.c_int
                    if not queue_attach(self.pointer,ctypes.cast(queue_reader,ctypes.c_void_p)):raise ValueError('native selection queue reader admission')
                    self.selection_queue_reader=queue_reader
                attach=getattr(library,'midi_cc_kernel_notes',None);mixed=getattr(library,'midi_mixed_kernel_run',None)
                if attach is not None and mixed is not None:
                    attach.argtypes=[ctypes.c_void_p,ctypes.c_void_p];attach.restype=ctypes.c_int
                    mixed.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint,
                        ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(_NativeCCAction),ctypes.c_uint,
                        ctypes.POINTER(_NativeNoteAction),ctypes.c_uint,ctypes.POINTER(ctypes.c_uint64),ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_uint64),ctypes.POINTER(ctypes.c_uint64)];mixed.restype=ctypes.c_int
        except (AttributeError,OSError):pass # Missing ABI leaves the active engine untouched.
        if self.pointer:
            try:
                library.midi_cc_semantic_version.argtypes=[];library.midi_cc_semantic_version.restype=ctypes.c_uint
                if library.midi_cc_semantic_version()!=1:return
                create=library.midi_cc_semantic_create
                create.argtypes=[ctypes.c_void_p]*4+[ctypes.POINTER(self._SemanticField),ctypes.c_uint,
                    ctypes.POINTER(self._SemanticRoute),ctypes.c_uint,ctypes.POINTER(ctypes.c_int),ctypes.c_uint]
                create.restype=ctypes.c_void_p
                library.midi_cc_semantic_release.argtypes=[ctypes.c_void_p];library.midi_cc_semantic_release.restype=None
                library.midi_cc_kernel_semantic_configure.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint]
                library.midi_cc_kernel_semantic_configure.restype=ctypes.c_int
                library.midi_cc_kernel_semantic_failures.argtypes=[ctypes.c_void_p];library.midi_cc_kernel_semantic_failures.restype=ctypes.c_uint64
                assert ctypes.sizeof(self._SemanticField)==32 and ctypes.sizeof(self._SemanticRoute)==32
                self.semantic_create=create
                if hasattr(library,'midi_cc_semantic_timbre_watch_version'):
                    version=library.midi_cc_semantic_timbre_watch_version;version.argtypes=[];version.restype=ctypes.c_uint
                    if version()==1:
                        watch=library.midi_cc_semantic_timbre_watch
                        watch.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint];watch.restype=ctypes.c_int
                        self.semantic_timbre_watch=watch
                if hasattr(library,'midi_cc_semantic_mode_version'):
                    library.midi_cc_semantic_mode_version.argtypes=[];library.midi_cc_semantic_mode_version.restype=ctypes.c_uint
                    if library.midi_cc_semantic_mode_version()==1:
                        seed=library.midi_cc_semantic_mode_seed
                        seed.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_int),ctypes.c_uint];seed.restype=ctypes.c_int
                        self.semantic_mode_seed=seed
                if hasattr(library,'midi_cc_semantic_arm_version'):
                    version=library.midi_cc_semantic_arm_version;version.argtypes=[];version.restype=ctypes.c_uint
                    if version()==1:
                        prepare=library.midi_cc_semantic_arm_prepare
                        prepare.argtypes=[ctypes.c_void_p]+[ctypes.c_uint]*4+[ctypes.c_uint64,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,
                            ctypes.POINTER(self._ArmPatch),ctypes.c_uint];prepare.restype=ctypes.c_int
                        self.semantic_arm_prepare=prepare
                if hasattr(library,'midi_cc_kernel_reset_version'):
                    version=library.midi_cc_kernel_reset_version;version.argtypes=[];version.restype=ctypes.c_uint
                    if version()==1:
                        prepare=library.midi_cc_kernel_reset_configure
                        prepare.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,ctypes.c_uint64]
                        prepare.restype=ctypes.c_int;self.reset_prepare=prepare
                if hasattr(library,'midi_cc_semantic_reset_version'):
                    version=library.midi_cc_semantic_reset_version;version.argtypes=[];version.restype=ctypes.c_uint
                    if version()==1:
                        prepare=library.midi_cc_semantic_reset_prepare
                        prepare.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint];prepare.restype=ctypes.c_int
                        self.reset_scalar_prepare=prepare
            except (AttributeError,OSError):pass

    class _SemanticValue(ctypes.Structure):
        _fields_=[('input',ctypes.c_int),('amount',ctypes.c_int),('raw',ctypes.c_uint)]

    class _SemanticField(ctypes.Structure):
        _fields_=[('event',ctypes.c_uint),('low',ctypes.c_int),('high',ctypes.c_int),('neutral',ctypes.c_int),
            ('flags',ctypes.c_uint),('decoder',ctypes.POINTER(ctypes.c_int))]

    class _SemanticRoute(ctypes.Structure):
        _fields_=[('channel',ctypes.c_uint),('cc',ctypes.c_uint),('source',ctypes.c_uint),('destination',ctypes.c_uint),
            ('event',ctypes.c_uint),('flags',ctypes.c_uint),('table',ctypes.c_void_p)]

    class _ArmPatch(ctypes.Structure):
        _fields_=[(name,ctypes.c_uint) for name in ('captured','channel','cc','first','count','blocked')]

    @staticmethod
    def prepare_interceptions(owner,*,include_modes=False,learn=None,only_sources=None):
        """Compile old pre-RAW CC semantics without admitting a native route.

        Rows are (channel,CC,kind,ordered routes); each route contains source,
        direct destination/event, mapped-input, saved-expression, attempted
        timbre and preset-observer-required flags, and 128 exact (input amount,
        destination amount,BASS parameter) tuples.
        Learn takes precedence over special 91/93/94 functions, consumes the
        source RAW CC and dispatches each distinct target in insertion order.
        Both paths call the old native_input=False setter: expression storage
        is a writer BEFORE emission, even when BASS subsequently rejects it.
        Ordinary native-filter functions MUST NOT use this writer contract.
        The observer flag installs the existing preset observer; it does NOT
        authorize writing preset_seeds.native (native_input=False never does).

        Mode targets and armed Learn are explicit unresolved execution paths.
        set_expression's saved+observed equality guard, current-system neutral,
        attempted timbre, send success shadows and native observers still need
        executor state/journals; this table alone is not semantic admission.
        Values are prepared on the control side using the existing oracle;
        no MIDI event, BASS call, callback, INI or session writer runs here.
        """
        learn=owner.midi_learn if learn is None else learn;policy=owner.channel_policy
        with learn.lock,policy.lock:
            revision,pending,channels,dispatch=learn.execution_snapshot()
            functions=dict(policy.functions);keys=dict(policy.pressure_keys)
            policy_revision=policy.revision
        rows=[];unsupported=[];tables={}
        active=owner.addresses.count
        custom={(channel,cc):targets for channel,cc,targets in dispatch if channel<active}
        candidates=[(channel,cc,'learn',targets) for (channel,cc),targets in custom.items()]
        for (channel,source),destination in functions.items():
            cc={'reverb':91,'chorus':93,'delay_send':94}.get(source)
            if (channel>=active or cc is None or (channel,cc) in custom or
                    (cc==94 and not owner.midi_options.cc94_custom_delay_send)):
                continue
            candidates.append((channel,cc,'special_function',(source,)))
        for channel,cc,kind,sources in candidates:
            if only_sources is not None and (channel,cc) not in only_sources:continue
            routes=[]
            for source in sources:
                if source in CHANNEL_MODE_SWITCHES:
                    if not include_modes:
                        unsupported.append((channel,cc,source,'channel_mode_state'));continue
                    values=tuple((int(value>=64),int(value>=64),int(value>=64)) for value in range(128))
                    routes.append((source,None,MIDI_EVENT_MODE,False,False,False,False,values));continue
                destination=functions.get((channel,source),source)
                mapped=(channel,source) in functions
                key=keys.get(channel,60)
                cache_key=(source,destination,key if destination=='key_pressure' else None,kind)
                if cache_key not in tables:
                    values=[]
                    for value in range(128):
                        amount=channel_cc_target_value(source,value) if kind=='learn' else value
                        target=ChannelControlPolicy.convert(source,destination,amount)
                        raw=(0 if destination is None else (target<<8)|key if destination=='key_pressure' else
                            MidiPerformanceControls.expression_parameter(destination,target)
                            if destination in EXPRESSION_EVENTS else target)
                        values.append((amount,target,raw))
                    tables[cache_key]=tuple(values)
                event=0 if destination is None else MIDI_EVENT_USERFX if destination=='delay_send' else CHANNEL_PARAMETERS[destination].event
                routes.append((source,destination,event,mapped,destination in EXPRESSION_EVENTS,
                    destination in CHANNEL_TIMBRE_FIELDS,destination in PRESET_EXPRESSION_RANGES,tables[cache_key]))
            rows.append((channel,cc,kind,tuple(routes)))
        return dict(revision=(revision,policy_revision,owner.midi_options.cc94_custom_delay_send,active),
            pending=pending,capture_channels=channels,rows=tuple(rows),unsupported=tuple(unsupported))

    def _prepare_arm_successors(self,owner,base_plan):
        """Prepare120 affected-only capture deltas with the original assigner.

        Clone/reindex runs on the paused control side. No complete120-way
        native config copies are retained: only changed dispatch rows and
        binding metadata survive. Conversion tuples are interned across every
        successor, while untouched base rows stay in the existing generation.
        This method neither publishes ABI tables nor captures/acknowledges CC.
        """
        learn=owner.midi_learn;policy=owner.channel_policy
        with learn.lock,policy.lock:
            snapshot=learn.control_clone()
            revision,pending,channels,_dispatch=snapshot.execution_snapshot()
            policy_revision=policy.revision
        expected=(revision,policy_revision,owner.midi_options.cc94_custom_delay_send,owner.addresses.count)
        if base_plan['revision']!=expected or base_plan['pending']!=pending:
            raise ValueError('armed Learn preparation revision changed')
        if pending is None:return None
        channel,target,all_channels,destination=pending
        scope=channels if all_channels else (channel,)
        if any(selected>=owner.addresses.count for selected in scope):
            raise ValueError('armed Learn scope not allocated')
        base_dispatch=dict(snapshot._dispatch);intern={}
        for _ch,_cc,_kind,routes in base_plan['rows']:
            for route in routes:intern.setdefault(route[-1],route[-1])
        successors={}
        for captured_cc in range(120):
            successor=snapshot.control_clone(channels=channels)
            with successor.lock:successor._assign_locked(channel,target,captured_cc,all_channels,destination)
            affected=tuple(key for key in dict.fromkeys((*base_dispatch,*successor._dispatch))
                if base_dispatch.get(key)!=successor._dispatch.get(key))
            affected_set=set(affected)
            # The original compiler sees only changed rows and their relevant
            # function policies; removed learned91/93/94 rows still expose
            # their original special-function fallback when one exists.
            plan=self.prepare_interceptions(owner,include_modes=True,learn=successor,only_sources=affected_set)
            rows=[]
            for ch,cc,kind,routes in plan['rows']:
                if (ch,cc) not in affected_set:continue
                reused=tuple((*route[:-1],intern.setdefault(route[-1],route[-1])) for route in routes)
                rows.append((ch,cc,kind,reused))
            row_map={(ch,cc):routes for ch,cc,_kind,routes in rows}
            blocked={(ch,cc) for ch,cc,_source,_reason in plan['unsupported']}
            ordinal={key:index for index,key in enumerate(successor._assignments)}
            removed=tuple(key for key in snapshot._assignments if key not in successor._assignments)
            updates=[]
            for key,sources in successor._assignments.items():
                old=(snapshot._assignments.get(key),snapshot._destinations.get(key),snapshot._names.get(key),key in snapshot._all_channels)
                new=(sources,successor._destinations.get(key),successor._names.get(key),key in successor._all_channels)
                if old!=new:
                    updates.append((*key,tuple(sources),new[1],new[2],new[3],ordinal[key]))
            successors[captured_cc]=dict(revision=successor.revision,pending=None,rows=tuple(rows),
                affected=tuple((key,row_map.get(key)) for key in affected),
                blocked=tuple(key for key in affected if key in blocked),
                metadata=dict(removed=removed,updates=tuple(updates),
                    removed_names=tuple(key for key in snapshot._names if key not in successor._names)))
        if learn.execution_snapshot()[:3]!=(revision,pending,channels) or policy.revision!=policy_revision:
            raise ValueError('armed Learn preparation changed during compile')
        return dict(revision=expected,pending=pending,scope=scope,successors=successors,
            reused_conversion_tables=len(intern),control_only=True)

    def _reset_mode_supported(self):
        version=getattr(self.library,'midi_cc_semantic_reset_mode_version',None)
        if version is None:return False
        version.argtypes=[];version.restype=ctypes.c_uint
        return version()==1

    @staticmethod
    def prepare_reset_defaults(owner,*,include_mode=False,include_address=False):
        """Compile the original ordered scalar default pass without emitting.

        Address/Mode defaults and extension force-reapply are separate compound
        operators. Reject those channels wholesale until they are prepared.
        """
        with owner.channel_policy.lock:
            defaults=dict(owner.channel_policy.reset_defaults)
            functions=dict(owner.channel_policy.functions);keys=dict(owner.channel_policy.pressure_keys)
        rows=[];incomplete=set()
        for (channel,source),value in defaults.items():
            if source in ('bank','bank_lsb','program') and include_address:continue
            if source=='mode' and include_mode:
                amount=max(0,min(2,int(round(value))))
                rows.append((channel,('mode','mode',MIDI_EVENT_MODE,False,True,False,False,((amount,amount,amount),)*128)))
                continue
            if source not in CHANNEL_FUNCTION_TARGETS:
                incomplete.add(channel);continue
            low,high=ChannelControlPolicy.bounds(source);amount=max(low,min(high,int(round(value))))
            if source in ('portamento','sustain','sostenuto','soft'):amount=127 if amount>=64 else 0
            destination=functions.get((channel,source),source);mapped=(channel,source) in functions
            converted=ChannelControlPolicy.convert(source,destination,amount)
            raw=0 if destination is None else (converted<<8)|keys.get(channel,60) if destination=='key_pressure' else \
                MidiPerformanceControls.expression_parameter(destination,converted) if destination in EXPRESSION_EVENTS else converted
            event=0 if destination is None else MIDI_EVENT_USERFX if destination=='delay_send' else CHANNEL_PARAMETERS[destination].event
            rows.append((channel,(source,destination,event,mapped,destination in EXPRESSION_EVENTS,
                destination in CHANNEL_TIMBRE_FIELDS,destination in PRESET_EXPRESSION_RANGES,((amount,converted,raw),)*128)))
        return dict(rows=tuple(rows),incomplete=frozenset(incomplete),defaults=defaults)

    def _prepare_semantics(self,owner,plan,blocked):
        """Control-barrier preparation; native copies own tables and observer refs.

        Worker/manual command publication must pause/join before this call.
        GetEvent is a DLL pointer, never a Python callable on the MIDI worker.
        Armed Learn and any mode fanout block the whole source CC.
        """
        get=owner.performance.bassmidi.BASS_MIDI_StreamGetEvent
        # bassmidi.h: DWORD WINAPI StreamGetEvent(HSTREAM,DWORD,DWORD).
        # bassmidi.md: 0xffffffff is unavailable; MOD readback uses native observers.
        if not isinstance(get,ctypes._CFuncPtr):return None
        excluded={(ch,cc) for ch,cc,_source,_reason in plan['unsupported']}
        pending=plan['pending']
        arm=self._prepare_arm_successors(owner,plan) if pending is not None and self.semantic_arm_prepare is not None else None
        if pending is not None and arm is None:
            excluded.update((ch,cc) for ch in (range(128) if pending[2] else (pending[0],)) for cc in range(120))
        rows=[row for row in plan['rows'] if (row[0],row[1]) not in excluded and row[3]]
        reset=self.prepare_reset_defaults(owner,include_mode=self._reset_mode_supported(),include_address=self._reset_address_supported()) if self._reset_requested and self.reset_scalar_prepare is not None else dict(rows=())
        reset_rows=tuple((channel,route) for channel,route in reset['rows'] if channel not in reset.get('incomplete',()))
        if not rows and not self._manual_requested and not self._compound_reset_requested and arm is None and not reset_rows:return None
        destinations={route[1] for _ch,_cc,_kind,routes in rows for route in routes}
        destinations.update(route[1] for _channel,route in reset_rows)
        if arm is not None:
            destinations.update(route[1] for successor in arm['successors'].values() for _ch,_cc,_kind,routes in successor['rows'] for route in routes)
        if self._manual_requested:destinations.update(self.semantic_fields)
        if any(field in PRESET_EXPRESSION_RANGES for field in destinations):
            owner.performance.observe_native_stream(owner.stream_handle)
            owner.performance.preset_seeds.bind(True)
        if any(field is not None and field.startswith('modulation_') for field in destinations):
            if not owner.performance.observe_native_stream(owner.stream_handle):return None
        if not owner.drain_native_controls():return None
        names=self.semantic_fields;slots={field:index for index,field in enumerate(names)}
        if self.semantic_journal is None:
            events=tuple(base+index for base in (0xc0000,0xd0000,0xe0000,0xf0000,0x100000,0x110000) for index in range(len(names)))
            events+=tuple(0x120000+slots[field] for field in ('reverb','chorus'))
            if hasattr(self.library,'midi_execution_manual_send_version'):events+=(0x130000+slots['delay_send'],)
            if self.semantic_mode_seed is not None:events+=tuple(range(0x140000,0x190000,0x10000))
            if self._reset_mode_supported():events+=(0x190000,)
            keyed=(0xf0000+slots['key_pressure'],) if 'key_pressure' in slots else ()
            journal=NativeMidiState(owner.stream_handle,events,self._observe_semantics,self.library._name,clock=owner.midi_clock,
                keyed_events=keyed,failure_capacity=self.semantic_failure_capacity)
            if self.semantic_failure_capacity is not None and (journal.failures is None or not journal.failures.lease_ready):
                journal.close();raise ValueError(tr('runtime.midi_state_snapshot_failed',error='semantic failure lease ABI'))
            if not journal.pointer or journal.clock is not owner.midi_clock:
                journal.close();return None
            self.semantic_journal=journal
        if self.reset_scalar_prepare is not None and self.reset_journal is None:
            keyed=(0x1b0000+slots['key_pressure'],) if 'key_pressure' in slots else ()
            journal=NativeMidiState(owner.stream_handle,tuple(0x1b0000+index for index in range(len(names))),
                self._observe_semantics,self.library._name,clock=owner.midi_clock,keyed_events=keyed)
            if not journal.pointer or journal.clock is not owner.midi_clock:journal.close();return None
            self.reset_journal=journal
        timbre_publication=self._manual_requested and self.semantic_timbre_watch is not None
        provenance_publication=hasattr(self.library,'midi_selection_state_provenance_seed')
        if timbre_publication or provenance_publication:
            # Initial selection may predate this journal (including when Learn
            # created it before manual admission). Reuse existing watcher
            # construction/publication; a failed attempt remains retryable and
            # never clears its original lease/borrower or resends audio.
            gate=owner.channel_native_filter;mapper=getattr(gate,'function_map',None)
            # A compatibility callback leaves Python the selection writer.
            # Its dormant native map has no newly published watcher; native
            # return still requires the full check at its exact actor fence.
            if mapper is not None and mapper.full_selection_candidate and not getattr(owner.performance,'_native_failure_compatibility',False):
                def publication_ready(adapter):
                    if adapter is None:return False
                    if timbre_publication and self.semantic_journal not in adapter.borrowers:return False
                    if not provenance_publication:return True # Preserve older-image timbre contract.
                    if not hasattr(adapter,'provenance_bindings'):return False
                    descriptors,required_roots=NativeSelectionAdapter.provenance_donor_contract(owner,self)
                    try:
                        actual={(pointer,event,role,adapter.provenance_fields[field],adapter.provenance_sources[source],adapter.provenance_phases[phase])
                            for pointer,event,role,field,source,phase,_activation in adapter.provenance_bindings}
                    except (IndexError,TypeError,ValueError):return False
                    return actual==set(descriptors) and set(required_roots).issubset(adapter.borrowers)
                adapter=mapper.selection_adapters.get(gate.published_map)
                if not publication_ready(adapter):
                    with owner.state_write():
                        gate.signature=gate.checked_key=None;owner.ensure_event_filter()
                    mapper=getattr(gate,'function_map',None)
                    adapter=mapper.selection_adapters.get(gate.published_map) if mapper is not None else None
                    if not publication_ready(adapter):
                        raise RuntimeError(tr('runtime.midi_filter_config_failed',error='native timbre selection watch publication'))
        mod=owner.performance.mod_native_observer
        mapper=getattr(owner.channel_native_filter,'function_map',None);mapped=getattr(mapper,'journal',None)
        fields=[];seeds=[]
        with owner.performance.lock,owner.channel_policy.lock:
            for field in names:
                low,high=ChannelControlPolicy.bounds(field);expression=field in EXPRESSION_EVENTS
                flags=int(expression)|(int(field.startswith('modulation_'))<<1)|(int(field=='channel_pressure_vibrato')<<2)
                if hasattr(self.library,'midi_execution_manual_version'):flags|=int(field in CHANNEL_TIMBRE_FIELDS)<<3
                decoder=None
                if expression:
                    if field not in self.semantic_decoders:
                        values=[MidiPerformanceControls.expression_from_parameter(field,raw) for raw in range(65536)]
                        if not field.startswith('modulation_'):values=[max(low,min(high,value)) for value in values]
                        self.semantic_decoders[field]=(ctypes.c_int*65536)(*values)
                    decoder=self.semantic_decoders[field]
                event=MIDI_EVENT_USERFX if field=='delay_send' else CHANNEL_PARAMETERS[field].event
                fields.append(self._SemanticField(event,low,high,CHANNEL_FACTORY_DEFAULTS[field],flags,decoder))
                for channel in range(128):
                    key=(channel,field);value=owner.performance.expression.get(key,CHANNEL_FACTORY_DEFAULTS[field])
                    raw=owner.performance.mod_observed.get(key)
                    observed=int(raw is not None)
                    if flags&8:observed|=(int(key in owner.channel_policy.timbre)<<1)|(int(owner.channel_policy.mapped(channel,field))<<2)
                    seeds.extend((int(key in owner.performance.expression),value,observed,raw or 0))
        compiled=[];tables={};pooled={};order=[];patches=[]
        def descriptor(channel,cc,route):
            source,destination,event,mapped_input,saved,timbre,_observer,values=route
            if values not in tables:
                tables[values]=(self._SemanticValue*128)(*(self._SemanticValue(amount,target or 0,raw) for amount,target,raw in values))
            mode_kind=CHANNEL_MODE_SWITCHES.index(source)+1 if source in CHANNEL_MODE_SWITCHES else 0
            if source=='mode':
                return self._SemanticRoute(channel,cc,0,0xffffffff,MIDI_EVENT_MODE,32,ctypes.cast(tables[values],ctypes.c_void_p))
            return self._SemanticRoute(channel,cc,0 if mode_kind else slots[source],slots[destination] if destination is not None else 0xffffffff,
                event,(mode_kind<<3) if mode_kind else int(mapped_input)|(int(saved)<<1)|(int(timbre)<<2),ctypes.cast(tables[values],ctypes.c_void_p))
        for channel,cc,_kind,routes in rows:
            for route in routes:compiled.append(descriptor(channel,cc,route))
        reset_ranges=[]
        for channel in range(128):
            first=len(compiled)
            for selected,route in reset_rows:
                if selected==channel:compiled.append(descriptor(128,128,route))
            reset_ranges.extend((first,len(compiled)-first))
        if arm is not None:
            for captured,successor in arm['successors'].items():
                unsupported=set(successor['blocked'])
                for (channel,cc),routes in successor['affected']:
                    first=len(order)
                    for route in routes or ():
                        if route not in pooled:
                            pooled[route]=len(compiled);compiled.append(descriptor(128,128,route))
                        order.append(pooled[route])
                    # For a removed binding, restore the current underlying
                    # function/filter qualification, not an arbitrary RAW pass.
                    blocked_flag=int((channel,cc) in unsupported or (not routes and bool(self._raw_blocked_mask[channel*128+cc])))
                    patches.append(self._ArmPatch(captured,channel,cc,first,len(order)-first,blocked_flag))
        field_rows=(self._SemanticField*len(fields))(*fields);route_rows=(self._SemanticRoute*len(compiled))(*compiled)
        saved_rows=(ctypes.c_int*len(seeds))(*seeds)
        generation=self.semantic_create(self.semantic_journal.pointer,ctypes.cast(get,ctypes.c_void_p),
            mod.pointer if mod is not None else None,mapped.pointer if mapped is not None else None,
            field_rows,len(fields),route_rows,len(compiled),saved_rows,len(seeds))
        if generation:
            if self._manual_requested and mapped is not None and self.semantic_timbre_watch is not None:
                # Map journal 0x30000 records attempted timbre BEFORE the target
                # API (also on failure). Create already retains this same-clock
                # journal. No extra audio, value replay, or saved-manual writer.
                for field in CHANNEL_TIMBRE_FIELDS:
                    if not self.semantic_timbre_watch(generation,slots[field],0x30000+mapper.slots[field]):
                        self.library.midi_cc_semantic_release(generation)
                        raise RuntimeError(tr('runtime.midi_filter_config_failed',error='native timbre watch preparation'))
            if self.reset_scalar_prepare is not None:
                native_ranges=(ctypes.c_uint*256)(*reset_ranges)
                if not self.reset_scalar_prepare(generation,self.reset_journal.pointer,native_ranges,len(native_ranges)):
                    self.library.midi_cc_semantic_release(generation);return None
            if self.semantic_mode_seed is not None:
                with owner.performance.lock:
                    mode_values=tuple(value for channel in range(128) for value in
                        (int((channel,'mode') in owner.performance.expression),owner.performance.expression.get((channel,'mode'),0)))
                if not self.semantic_mode_seed(generation,(ctypes.c_int*256)(*mode_values),256):
                    self.library.midi_cc_semantic_release(generation);return None
            if arm is not None:
                channel,target,all_channels,destination=arm['pending'];keys=self.capture_keys
                native_order=(ctypes.c_uint*max(1,len(order)))(*order)
                native_patches=(self._ArmPatch*max(1,len(patches)))(*patches)
                if not self.semantic_arm_prepare(generation,channel,keys.index(target),0xffffffff if destination is None else keys.index(destination),
                        int(all_channels),arm['revision'][0],owner.addresses.count,native_order,len(order),native_patches,len(patches)):
                    self.library.midi_cc_semantic_release(generation);return None
                if self.semantic_actions is None:self.semantic_actions=NativeCCCaptureQueue(self)
            self.semantic_gets.append(get) # Retain every borrowed image until kernel destruction, including failed publication.
            for channel,cc,_kind,_routes in rows:blocked[channel*128+cc]=0
        return generation

    def _observe_semantics(self,_sync,stream,data,event):
        """Metadata-only replay, including failed-attempt saved values."""
        owner=self.owner
        if owner is None or stream!=owner.stream_handle:return
        failure_sink=self.semantic_failure_sink if self.semantic_failure_sink is not None else owner.performance.failure_sink
        channel,raw=data>>16,data&65535;kind=event&0xff0000
        if kind==0x1b0000:
            field=self.semantic_fields[event&65535]
            if field=='delay_send':
                owner.delay_runtime.states[channel].send=raw
                if channel==9:owner.delay_runtime.melodic_nine.send=raw
                if raw and owner.addresses.drums[channel] and channel!=9 and channel not in owner.drum_delay_warnings:
                    owner.drum_delay_warnings.add(channel)
                    owner.append_log(log_text('runtime.drum_delay_port_limited',port=channel//16,channel=channel%16+1))
            if field.startswith('modulation_'):owner.performance.mod_observed[channel,field]=raw
            native=MIDI_EVENT_USERFX if field=='delay_send' else CHANNEL_PARAMETERS[field].event
            owner.record_origin(channel,native,raw,'reset_seed',field=field)
            return
        if kind>=0x140000:
            if kind==0x140000:
                with owner.performance.lock:
                    if raw&0x8000:owner.performance.expression[channel,'mode']=raw&0x7fff
                    else:owner.performance.expression.pop((channel,'mode'),None)
            elif kind==0x150000:owner.record_origin(channel,MIDI_EVENT_MODE,raw,self.semantic_source,field='mode')
            elif kind==0x190000:owner.record_origin(channel,MIDI_EVENT_MODE,raw,'reset_seed',field='mode')
            elif kind==0x160000:self.semantic_mode_error_low[channel]=raw
            elif kind==0x170000:self.semantic_mode_error[channel]=ctypes.c_int((raw<<16)|self.semantic_mode_error_low.get(channel,0)).value
            elif kind==0x180000:
                code=self.semantic_mode_error.get(channel,0)
                if raw==2 and failure_sink:failure_sink(MIDI_EVENT_MODE,code)
                elif raw==1:owner.append_log(log_text('runtime.midi_stream_events_failed',error=code))
            return
        field=self.semantic_fields[event&65535]
        if kind==0xc0000:
            with owner.channel_policy.lock:owner.channel_policy.inputs[channel,field]=raw+ChannelControlPolicy.bounds(field)[0]
        elif kind==0xd0000:
            with owner.performance.lock:
                if raw&0x8000:owner.performance.expression[channel,field]=(raw&0x7fff)+ChannelControlPolicy.bounds(field)[0]
                else:owner.performance.expression.pop((channel,field),None)
        elif kind==0xe0000:
            with owner.channel_policy.lock:owner.channel_policy.timbre[channel,field]=raw
        elif kind==0xf0000:
            if field=='delay_send':
                owner.delay_runtime.states[channel].send=raw
                if channel==9:owner.delay_runtime.melodic_nine.send=raw
                if raw and owner.addresses.drums[channel] and channel!=9 and channel not in owner.drum_delay_warnings:
                    owner.drum_delay_warnings.add(channel)
                    owner.append_log(log_text('runtime.drum_delay_port_limited',port=channel//16,channel=channel%16+1))
            if field.startswith('modulation_'):owner.performance.mod_observed[channel,field]=raw
            event=MIDI_EVENT_USERFX if field=='delay_send' else CHANNEL_PARAMETERS[field].event
            owner.record_origin(channel,event,raw,self.semantic_source,field=field)
        elif kind==0x100000:self.semantic_error_low[channel,field]=raw
        elif kind==0x110000:
            error=ctypes.c_int((raw<<16)|self.semantic_error_low.get((channel,field),0)).value
            if failure_sink:failure_sink(CHANNEL_PARAMETERS[field].event,error)
        elif kind==0x120000:
            values=owner.channel_sends.channel_reverb if field=='reverb' else owner.channel_sends.channel_chorus
            values[channel]=raw
        elif kind==0x130000:
            owner.delay_runtime.states[channel].send=raw
            if channel==9:owner.delay_runtime.melodic_nine.send=raw

    def _observe_manual(self,_sync,stream,data,event):
        if self.owner is not None and stream==self.owner.stream_handle:
            if event in (MIDI_EVENT_MOD_VIBRATO,MIDI_EVENT_MOD_PITCH,MIDI_EVENT_MOD_FILTER,MIDI_EVENT_MOD_VOLUME):
                field=next(field for field,parameter in CHANNEL_PARAMETERS.items() if parameter.event==event)
                self.owner.performance.mod_observed[data>>16,field]=data&65535
            if event==MIDI_EVENT_USERFX:
                channel,raw=data>>16,data&65535
                if raw and self.owner.addresses.drums[channel] and channel!=9 and channel not in self.owner.drum_delay_warnings:
                    self.owner.drum_delay_warnings.add(channel)
                    self.owner.append_log(log_text('runtime.drum_delay_port_limited',port=channel//16,channel=channel%16+1))
            self.owner.record_origin(data>>16,event,data&65535,'manual',field='delay_send' if event==MIDI_EVENT_USERFX else None)

    def prepare_manual_controls(self,owner):
        """Candidate explicit preparation, before worker admission; no UI wiring.

        Typed sends need their additive ABI; mode/generation publication remain gates.
        A raw StreamEvent command is not an alternative to these saved writers.
        """
        if not hasattr(self.library,'midi_execution_manual_version'):return False
        self.library.midi_execution_manual_version.argtypes=[];self.library.midi_execution_manual_version.restype=ctypes.c_uint
        if self.library.midi_execution_manual_version()!=1:return False
        with owner.midi_lock,self.lock:
            if self.journal is None and not self.configure(owner):return False
            self._manual_requested=True
            if self.manual_journal is None:
                events=tuple(dict.fromkeys(CHANNEL_PARAMETERS[field].event for field in self.semantic_fields
                    if field not in ('reverb','chorus','delay_send')))
                if hasattr(self.library,'midi_execution_manual_send_version'):events+=(MIDI_EVENT_REVERB,MIDI_EVENT_CHORUS,MIDI_EVENT_USERFX)
                if hasattr(self.library,'midi_execution_manual_mode_version'):events+=(MIDI_EVENT_MODE,)
                journal=NativeMidiState(owner.stream_handle,events,self._observe_manual,self.library._name,clock=owner.midi_clock)
                attach=self.library.midi_cc_kernel_manual_journal;attach.argtypes=[ctypes.c_void_p,ctypes.c_void_p];attach.restype=ctypes.c_int
                if not journal.pointer or not attach(self.pointer,journal.pointer):journal.close();return False
                self.manual_journal=journal
            return self.configure(owner)

    def apply_selection_reset_stage(self,channel,stage):
        """Candidate primitive for the exact full reset plan, no MIDI replay.

        Stages0..4 are session clear, routing clear, readback/change apply,
        all-channel apply and graph sync. The caller advances only once an
        executed stage completes; metadata failures retry metadata only.
        This API alone does not admit GM/GS or alter the product reset path.
        """
        function=getattr(self.library,'midi_cc_kernel_selection_reset',None)
        if function is None:return None
        if type(channel) is not int or not 0<=channel<128 or type(stage) is not int or not 0<=stage<=4:raise ValueError('selection reset phase')
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint64)];function.restype=ctypes.c_int
        result=(ctypes.c_uint64*4)()
        with self.lock:status=int(function(self.pointer,channel,stage,result))
        return dict(status=status,executed=bool(result[0]),completed=bool(result[1]),first=int(result[2]),last=int(result[3]))

    def _reset_address_supported(self):
        version=getattr(self.library,'midi_cc_kernel_reset_address_version',None)
        if version is None:return False
        version.argtypes=[];version.restype=ctypes.c_uint
        return version()==1

    def _prepare_reset_addresses(self,owner,defaults):
        if not self._reset_address_supported():return not any(field in ('bank','bank_lsb','program') for _channel,field in defaults)
        rows=[]
        for channel in range(128):
            mask=sum(1<<index for index,field in enumerate(('bank','bank_lsb','program')) if (channel,field) in defaults)
            rows.extend((mask,defaults.get((channel,'bank'),0),defaults.get((channel,'bank_lsb'),0),defaults.get((channel,'program'),0)))
        prepare=self.library.midi_cc_kernel_reset_addresses
        prepare.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint];prepare.restype=ctypes.c_int
        return bool(prepare(self.pointer,(ctypes.c_uint*512)(*rows),512))

    def prepare_full_reset(self,owner):
        """Prepare the original GM chain without changing audio or user state.

        Address/Mode saved defaults still require compound preparation. They
        reject admission before any prefix; no existing default is omitted.
        This operator does not by itself enable RAW SysEx worker admission.
        """
        function=getattr(self.library,'midi_cc_kernel_gm_prepare',None)
        if function is None or self.owner is not owner or self.selection_adapter is None:return False
        version=self.library.midi_cc_kernel_gm_version;version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=2:return False
        self._full_reset_requested=True
        reset=self.prepare_reset_defaults(owner,include_mode=self._reset_mode_supported(),include_address=self._reset_address_supported())
        if reset['incomplete']:return False
        if not self._prepare_reset_addresses(owner,reset['defaults']):return False
        channels=tuple(owner.addresses.channels())
        # Store the mode-independent source seed. Native Reset resolves the
        # GS/XG zero branch at execution time,not the mode at preparation.
        defaults=tuple((event,owner.performance.expression_parameter(field,EXPRESSION_DEFAULTS[field] if field=='channel_pressure_vibrato' else owner.performance.expression_default(field)),
                        int(field=='channel_pressure_vibrato')) for field,event in EXPRESSION_EVENTS.items())
        masters=[]
        if owner.performance.master_cents:masters.append((MASTER_FINE_PARAMETER.event,MASTER_FINE_PARAMETER.parameter(0,owner.performance.master_cents)))
        if owner.performance.master_semitones:masters.append((MASTER_COARSE_PARAMETER.event,MASTER_COARSE_PARAMETER.parameter(0,owner.performance.master_semitones)))
        flags=(int(owner.midi_options.gs_reset_enabled)|(int(owner.delay_runtime.sysex_enabled)<<1)|
            (int(bool(owner.soundfont_presets.extension_count))<<2)|
            (int(not owner.midi_options.gm1_reset_enabled)<<3)|
            (int(not owner.midi_options.gm2_reset_enabled)<<4)|(int(not owner.midi_options.xg_reset_enabled)<<5))
        frames=max(1,int(owner.delay_runtime.states[0].sample_rate)*340//1000)
        channel_rows=(ctypes.c_uint*len(channels))(*channels)
        default_rows=(ctypes.c_uint*(len(defaults)*3))(*(value for row in defaults for value in row))
        master_rows=(ctypes.c_uint*max(1,len(masters)*2))(*(value for row in masters for value in row))
        send_channels=tuple(owner.channel_sends.channels());send_rows=(ctypes.c_uint*len(send_channels))(*send_channels)
        function.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,
                           ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint];function.restype=ctypes.c_int
        with self.lock:prepared=bool(function(self.pointer,channel_rows,len(channels),default_rows,len(defaults),master_rows,len(masters),owner.channel_sends.default_reverb,flags,frames,send_rows,len(send_channels)))
        if prepared:self.full_reset_contract=dict(channels=channels,send_channels=send_channels,defaults=defaults,masters=tuple(masters),flags=flags,
            default_reverb=owner.channel_sends.default_reverb,delay_frames=frames,reset_defaults=tuple(sorted(reset['defaults'].items())),
            generation=owner.stream_generation)
        # Full/manual Reset is also a public standalone operator with no RAW
        # decoder. Generic SysEx admission requires its separately attached
        # context; absence must not invalidate an otherwise prepared Reset.
        context=getattr(self,'raw_context',None)
        if prepared and context is not None and context.pointer and getattr(self.library,'midi_cc_kernel_generic_raw_prepare',None) is not None:
            from bass_bindings import bassmidi as decode_library
            from delay_runtime import BMSYNTH_DELAY_PRESETS,DelayRuntime
            presets=tuple(value for row in BMSYNTH_DELAY_PRESETS.values() for value in row)
            times=tuple(max(10,min(1000,DelayRuntime.sysex_time_ms(value if value<=115 else 97))) for value in range(128))
            feedback=tuple(max(0,min(100,100+int((value-64)*100/64))) for value in range(128))
            prepare=self.library.midi_cc_kernel_generic_raw_prepare
            prepare.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,
                ctypes.POINTER(ctypes.c_uint),ctypes.POINTER(ctypes.c_uint),ctypes.c_uint];prepare.restype=ctypes.c_int
            with self.lock:prepared=bool(prepare(self.pointer,ctypes.cast(decode_library.BASS_MIDI_ConvertEvents,ctypes.c_void_p),
                int(owner.delay_runtime.states[0].sample_rate),(ctypes.c_uint*40)(*presets),40,
                (ctypes.c_uint*128)(*times),(ctypes.c_uint*128)(*feedback),128))
        return prepared

    def run_full_reset(self,owner):
        """Run/resume the prepared source-phase cursor; metadata ACK is separate."""
        function=getattr(self.library,'midi_cc_kernel_gm_run',None)
        if function is None or self.owner is not owner:return None
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p];function.restype=ctypes.c_int
        timing=getattr(owner.playback_timing,'pointer',None)
        with self.lock:return int(function(self.pointer,owner.stream_handle,timing))

    def system_reset_kind(self,stream,payload):
        function=getattr(self.library,'midi_cc_kernel_system_reset_kind',None)
        if function is None:return 0
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        data=(ctypes.c_ubyte*len(payload)).from_buffer_copy(payload)
        with self.lock:return int(function(self.pointer,stream,data,len(payload)))

    def system_reset_pending(self,stream):
        function=getattr(self.library,'midi_cc_kernel_system_reset_pending',None)
        if function is None:return False
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        with self.lock:status=int(function(self.pointer,stream))
        if status<0:raise ValueError('system reset stream ownership')
        return status==1

    def retire_system_reset(self,owner):
        function=getattr(self.library,'midi_cc_kernel_system_reset_abort',None)
        if function is None or self.owner is not owner:return False
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        with self.lock:return bool(function(self.pointer,owner.stream_handle))

    def prepare_manual_reset(self,owner):
        version=getattr(self.library,'midi_cc_kernel_manual_reset_version',None)
        if version is None or self.owner is not owner:return False
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:return False
        self._manual_reset_requested=True
        if not self.prepare_full_reset(owner):return False
        prepare=self.library.midi_cc_kernel_manual_reset_prepare
        prepare.argtypes=[ctypes.c_void_p];prepare.restype=ctypes.c_int
        with self.lock:return bool(prepare(self.pointer))

    def prepare_file_reset(self,owner):
        """Prepare original file-reset expression order/encoding, no audio.

        Candidate only: worker/file transport admission remains separate.
        Tables use the existing expression encoder, not guessed formulas.
        """
        function=getattr(self.library,'midi_cc_kernel_file_reset_prepare',None)
        if function is None or self.owner is not owner:return False
        if not self.prepare_full_reset(owner):return False
        channels=tuple(owner.addresses.channels());names=self.semantic_fields
        # apply_all retains out-of-scope entries, but _emit rejects a channel
        # outside channels(). Keep those values; do not send their audio.
        order=[]
        for channel in channels:
            for (selected,field),_value in owner.channel_policy.reset_defaults.items():
                destination=owner.channel_policy.functions.get((channel,field),field)
                if selected==channel and destination in EXPRESSION_EVENTS and (channel,destination) not in order:order.append((channel,destination))
        if any(field not in (*names,'mode') for _channel,field in order):return False
        encoder=owner.performance.expression_parameter
        domains=tuple((field,*ChannelControlPolicy.bounds(field)) for field in names if field in EXPRESSION_EVENTS)
        # These are pure unit-conversion tables, independent of live Notes,
        # reset values, source handles and per-preset state. Rebuild if the
        # encoder/type/bounds change; still republish dynamic reset order and
        # full reset contracts below on EVERY generation.
        encoding_key=(names,domains,encoder,getattr(type(owner.performance),'fine14',None))
        cached=getattr(self,'_file_reset_encoding',None)
        if cached is None or cached[0]!=encoding_key:
            offsets=[];encoded=[]
            for field in names:
                begin=len(encoded)
                if field in EXPRESSION_EVENTS:
                    low,high=ChannelControlPolicy.bounds(field)
                    encoded.extend(encoder(field,value) for value in range(low,high+1))
                offsets.extend((begin,len(encoded)-begin))
            cached=(encoding_key,(ctypes.c_uint*len(offsets))(*offsets),
                    (ctypes.c_uint*len(encoded))(*encoded))
            self._file_reset_encoding=cached
        offsets,encoded=cached[1:]
        rows=tuple(value for channel,field in order for value in (channel,len(names) if field=='mode' else names.index(field)))
        function.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint]
        function.restype=ctypes.c_int
        with self.lock:prepared=bool(function(self.pointer,(ctypes.c_uint*len(rows))(*rows),len(order),
            offsets,len(names),encoded,len(encoded)))
        if prepared:self._file_reset_requested=True
        return prepared

    def run_file_reset(self,owner,*,source='native_midi'):
        if source not in ('native_midi','manual'):raise ValueError('unprepared file Reset source')
        function=self.library.midi_cc_kernel_file_reset_run
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        with self.lock:return int(function(self.pointer,owner.stream_handle,getattr(owner.playback_timing,'pointer',None),31 if source=='native_midi' else 29))

    def file_reset_pending(self,stream):
        function=getattr(self.library,'midi_cc_kernel_file_reset_pending',None)
        if function is None:return False
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        with self.lock:status=int(function(self.pointer,stream))
        if status<0:raise ValueError('file Reset stream ownership')
        return status==1

    def retire_file_reset(self,owner):
        function=getattr(self.library,'midi_cc_kernel_file_reset_abort',None)
        if function is None or self.owner is not owner:return False
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        with self.lock:return bool(function(self.pointer,owner.stream_handle))

    def prepare_published_reset_contracts(self,owner):
        """Refresh only previously admitted reset operators at a paused fence.

        Semantic/source publication does not refresh the GM global/default
        contract. Manual Reset includes that contract, so prepare its original
        chain once when requested. Missing old ABI must not newly enable Reset.
        Failure retains the paused generation; these preparations send no audio.
        """
        if getattr(self,'_file_reset_requested',False):return self.prepare_file_reset(owner)
        if getattr(self,'_manual_reset_requested',False):return self.prepare_manual_reset(owner)
        if getattr(self,'_full_reset_requested',False):return self.prepare_full_reset(owner)
        return True

    def run_manual_reset(self,owner):
        function=getattr(self.library,'midi_cc_kernel_manual_reset_run',None)
        if function is None or self.owner is not owner:return None
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p];function.restype=ctypes.c_int
        with self.lock:return int(function(self.pointer,owner.stream_handle,getattr(owner.playback_timing,'pointer',None)))

    def manual_reset_pending(self,stream):
        function=getattr(self.library,'midi_cc_kernel_manual_reset_pending',None)
        if function is None:return False
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        with self.lock:status=int(function(self.pointer,stream))
        if status<0:raise ValueError('manual reset stream ownership')
        return status==1

    def retire_manual_reset(self,owner):
        function=getattr(self.library,'midi_cc_kernel_manual_reset_abort',None)
        if function is None or self.owner is not owner:return False
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        with self.lock:return bool(function(self.pointer,owner.stream_handle))

    def full_reset_status(self):
        """Captured phase/fatal status; never query another thread's BASS error."""
        function=getattr(self.library,'midi_cc_kernel_gm_status',None)
        if function is None:return None
        function.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_int),ctypes.c_uint];function.restype=ctypes.c_int
        values=(ctypes.c_int*5)()
        with self.lock:
            if not function(self.pointer,values,5):return None
        return dict(active=bool(values[0]),phase=values[1],channel_cursor=values[2],fatal=values[3],error=values[4])

    def prepare_compound_reset_controls(self,owner):
        """Explicit candidate full CC121 tail; no partial/implicit admission."""
        version=getattr(self.library,'midi_cc_kernel_reset_tail_version',None)
        if version is None or not self._reset_address_supported() or not self._reset_mode_supported():return False
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1 or self.notes is None:return False
        self._reset_requested=True;self._compound_reset_requested=True
        return self.configure(owner)

    def semantic_pending(self,stream):
        function=getattr(self.library,'midi_cc_kernel_semantic_pending',None)
        if function is None:return False
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        with self.lock:result=int(function(self.pointer,stream))
        if result<0:raise RuntimeError(tr('runtime.midi_filter_config_failed',error='semantic stream ownership'))
        return result>0

    def resume_semantics(self,owner,report):
        """Resume only this kernel's unexecuted routes, retain its original receipt."""
        function=getattr(self.library,'midi_cc_kernel_semantic_resume',None)
        if function is None or self.owner is not owner:return None
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        with self.lock:
            if self.pending is not None and self.pending is not report:
                raise RuntimeError(tr('runtime.midi_filter_config_failed',error='semantic receipt ownership'))
            status=int(function(self.pointer,owner.stream_handle))
            take=self.library.midi_cc_kernel_take_actions
            take.argtypes=[ctypes.c_void_p,ctypes.POINTER(_NativeNoteAction),ctypes.c_uint];take.restype=ctypes.c_int
            rows=(_NativeNoteAction*4096)();count=int(take(self.pointer,rows,len(rows)))
            if count<0:raise RuntimeError(tr('runtime.midi_filter_config_failed',error='semantic action drain '+str(count)))
            if count:
                report['note_actions']=report.get('note_actions',())+tuple((row.packed,row.channel,row.count,row.qpc) for row in rows[:count])
                if 'note_replay' in report:report['note_replay']['actions']=report['note_actions']
            report['semantic_pending']=self.semantic_pending(owner.stream_handle)
            report['semantic_resume_status']=status;report['unlocked']=status!=-2
            if status==-2:self.pending=report
            return status

    def retire_semantics(self,owner):
        """Explicit post-stop cancellation after prefix ACK; never a successful frame."""
        generic=getattr(self.library,'midi_cc_kernel_generic_raw_abort',None)
        if generic is not None:
            generic.argtypes=[ctypes.c_void_p,ctypes.c_uint];generic.restype=ctypes.c_int
            with self.lock:
                if not generic(self.pointer,owner.stream_handle):return False
        function=getattr(self.library,'midi_cc_kernel_semantic_abort',None)
        if function is None:return True
        if self.owner is not owner:return False
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        with self.lock:return int(function(self.pointer,owner.stream_handle))==1

    def retire_cancelled(self,owner):
        """Post join, original prefix ACK, no unexecuted audio/RAW suffix."""
        function=getattr(self.library,'midi_cc_kernel_cancel_owned',None)
        if function is None:return True
        if self.owner is not owner:return False
        receipt=getattr(self.library,'midi_cc_kernel_cancel_receipt',None)
        if receipt is None:return False
        receipt.argtypes=[ctypes.c_void_p,ctypes.c_uint];receipt.restype=ctypes.c_uint64
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint64];function.restype=ctypes.c_int
        with self.lock:return int(function(self.pointer,owner.stream_handle,receipt(self.pointer,owner.stream_handle)))==1

    def reset_tail_pending(self,stream):
        function=getattr(self.library,'midi_cc_kernel_reset_tail_pending',None)
        if function is None:return False
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        with self.lock:result=int(function(self.pointer,stream))
        if result<0:raise ValueError('reset tail stream ownership')
        return result==1

    def resume_reset_tail(self,owner):
        function=getattr(self.library,'midi_cc_kernel_reset_tail',None)
        if function is None or self.owner is not owner:return None
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        with self.lock:return int(function(self.pointer,owner.stream_handle))

    def retire_reset_tail(self,owner):
        """Post-stop fatal retirement after original metadata ACK, no audio."""
        function=getattr(self.library,'midi_cc_kernel_reset_tail_abort',None)
        if function is None or self.owner is not owner:return False
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        with self.lock:return bool(function(self.pointer,owner.stream_handle))

    def prepare_reset_controls(self,owner):
        """Explicit native transport/limited-CC121 admission, before worker.

        Extensions/saved defaults remain a compound-plan requirement, never
        an inferred empty pass. The product bootstrap chooses this candidate.
        """
        if self.reset_prepare is None:return False
        import sys
        if self.reset_scalar_prepare is not None and sys.version_info[:3]!=(3,14,7):
            raise ValueError('reset channel-set order requires the qualified CPython3.14.7')
        self._reset_requested=True
        return self.configure(owner)

    def configure(self,owner):
        """Control-side stable preparation; never invoke from an audio callback."""
        # Metadata handoff also calls this without the controller's command
        # scope. Match command -> capture snapshot lock order: runtime first,
        # kernel second. Holding kernel while draining runtime receipts creates
        # a cycle with a concurrent command reading the kernel capture queue.
        with owner.midi_lock,self.lock:
            return self._configure_owned(owner)

    def _configure_owned(self,owner):
        """Both reentrant owner locks are held in runtime -> kernel order."""
        self._refresh_frame_owner()
        if self._frame_owner is not None:return False
        if self.failure_notification_admitted:
            sink=owner.performance.failure_sink
            self.semantic_failure_sink=sink if type(sink) is DeferredPerformanceFailureLog and sink.owner is owner else None
        blocked=bytearray(128*128)
        with owner.channel_policy.lock:
            mapper=getattr(owner.channel_native_filter,'function_map',None)
            prepared=(mapper is not None and mapper.library is not None and mapper.library._handle==self.library._handle and bool(mapper.generations))
            for (channel,field),destination in owner.channel_policy.functions.items():
                eligible=(prepared and field in mapper.slots and (destination is None or destination in mapper.slots)
                    and (destination!='delay_send' or mapper.attach_delay is not None)
                    and (destination not in ('reverb','chorus') or mapper.attach_sends is not None))
                cc=CHANNEL_CC_DEFAULTS.get(field)
                # Ordinary CC goes through the SAME native BASS filter as old
                # RAW dispatch. Special send interception uses native_input=False
                # and saved-expression semantics; do not substitute that filter.
                special=field in ('reverb','chorus') or (field=='delay_send' and owner.midi_options.cc94_custom_delay_send)
                if cc is not None and cc<120:
                    if special or not eligible:blocked[channel*128+cc]=1
                elif not eligible:
                    pressure_slot={'key_pressure':120,'channel_pressure':121,'pitch':122}.get(field)
                    if pressure_slot is not None:blocked[channel*128+pressure_slot]=1
                    # RPN/SysEx sources can be reached through data entry: the
                    # full stateful parser must qualify them, not guess RPN state.
                    blocked[channel*128:channel*128+120]=b'\1'*120
            tone_cc={'resonance':71,'release':72,'attack':73,'cutoff':74,'decay':75}
            for (channel,field) in owner.channel_policy.timbre:
                if field in tone_cc and not owner.channel_policy.mapped(channel,field) and not self._manual_requested:blocked[channel*128+tone_cc[field]]=1
            if self._manual_requested:
                # Function-map attempted-timbre state is a distinct journal.
                # Older images still delegate: RAW may be native only when the
                # immutable semantic generation can watch every timbre attempt.
                mapped=getattr(mapper,'journal',None)
                touch_ready=(self.semantic_timbre_watch is not None and mapped is not None and bool(mapped.pointer)
                    and mapped.clock is owner.midi_clock)
                for (channel,field),destination in owner.channel_policy.functions.items():
                    if destination not in CHANNEL_TIMBRE_FIELDS or touch_ready:continue
                    cc=CHANNEL_CC_DEFAULTS.get(field)
                    if cc is not None and cc<120:blocked[channel*128+cc]=1
                    else:
                        slot={'key_pressure':120,'channel_pressure':121,'pitch':122}.get(field)
                        if slot is not None:blocked[channel*128+slot]=1
        self._raw_blocked_mask=bytes(blocked)
        with owner.midi_learn.lock:
            for (channel,cc) in owner.midi_learn._dispatch:blocked[channel*128+cc]=1
            pending=owner.midi_learn._pending
            if pending is not None and self.semantic_arm_prepare is None:
                channels=range(128) if owner.midi_learn._pending_all else (pending[0],)
                for channel in channels:blocked[channel*128:channel*128+120]=b'\1'*120
        flags=int(owner.midi_options.cc6_direct_pitch_range)|(int(owner.midi_options.cc94_custom_delay_send)<<1)
        with self.lock:
            if self.pending is not None:raise RuntimeError(tr('runtime.midi_filter_config_failed',error='CC unlock pending'))
            if self.semantic_actions is not None and not owner.drain_native_controls():return False
            if getattr(owner,'cc_native_kernel',None) not in (None,self):raise ValueError('midi_cc_kernel owner already bound')
            if self.journal is None:
                events=tuple(0xa0000+cc for cc in range(120))+(0xb0000+MIDI_EVENT_REVERB,0xb0000+MIDI_EVENT_CHORUS,0xb0000+MIDI_EVENT_USERFX,
                    MIDI_EVENT_REVERB,MIDI_EVENT_CHORUS,MIDI_EVENT_USERFX)
                if hasattr(self.library,'midi_cc_kernel_file_reset_version'):events+=(0x1d0000,)
                journal=NativeMidiState(owner.stream_handle,events,self._observe,self.library._name,clock=owner.midi_clock)
                if not journal.pointer or not self.library.midi_cc_kernel_journal(self.pointer,journal.pointer):
                    journal.close();raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_cc_kernel_journal'))
                self.journal=journal;self.owner=owner;owner.cc_native_kernel=self
            elif self.owner is not owner or not self.journal.pointer or self.journal.stream!=owner.stream_handle:
                raise ValueError('midi_cc_kernel stream generation')
            if self.reset_prepare is not None and self._reset_requested:
                # Extensions and saved defaults need a compound prepared
                # post-RAW plan. Admission must never omit existing writers.
                with owner.channel_policy.lock,owner.midi_learn.lock:
                    defaults=dict(owner.channel_policy.reset_defaults)
                    scalar=self.prepare_reset_defaults(owner,include_mode=self._reset_mode_supported(),include_address=self._compound_reset_requested and self._reset_address_supported())
                    admitted=tuple(int((self._compound_reset_requested or not owner.soundfont_presets.extension_count) and
                        channel not in scalar['incomplete'] and (self.reset_scalar_prepare is not None or
                        not any(ch==channel for ch,_field in defaults))) for channel in range(128))
                    revision=owner.midi_learn.revision
                    pending_channel=owner.midi_learn._pending[0] if owner.midi_learn._pending is not None else None
                values=(ctypes.c_uint*384)(*(value for channel in range(128) for value in
                    (admitted[channel],int(owner.addresses.drums[channel]),int(channel==pending_channel))))
                if not self.reset_prepare(self.pointer,values,len(values),revision):return False
                self.reset_contract=dict(admitted=admitted,learn_revision=revision,
                    stream_generation=owner.stream_generation,post_raw_defaults='scalar candidate' if self.reset_scalar_prepare is not None else 'empty only')
                if self.semantic_actions is None:self.semantic_actions=NativeCCCaptureQueue(self)
            plan=self.prepare_interceptions(owner,include_modes=self.semantic_mode_seed is not None) if self.semantic_create is not None else None
            if self._reset_requested or flags&2 or (self._manual_requested and hasattr(self.library,'midi_execution_manual_send_version')) or (plan and any(route[1]=='delay_send' for _ch,_cc,_kind,routes in plan['rows'] for route in routes)):
                with owner.delay_runtime.midi_links(self.library) as links:
                    if links is None:raise ValueError('midi_cc_kernel delay DLL image')
                    for channel in range(self.channels):
                        if not self.library.midi_cc_kernel_delay(self.pointer,channel,links.get((channel,False)),links.get((channel,True))):
                            raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_cc_kernel_delay'))
            generation=self._prepare_semantics(owner,plan,blocked) if plan is not None else None
            if self._reset_requested and self.reset_scalar_prepare is not None and generation is None and scalar['rows']:
                return False
            if generation is None and plan is not None and plan['pending'] is not None:
                pending=plan['pending']
                for channel in (range(128) if pending[2] else (pending[0],)):
                    blocked[channel*128:channel*128+120]=b'\1'*120
            if hasattr(self.library,'midi_cc_kernel_selection'):
                attach=self.library.midi_cc_kernel_selection;attach.argtypes=[ctypes.c_void_p,ctypes.c_void_p];attach.restype=ctypes.c_int
                gate=owner.channel_native_filter;mapper=getattr(gate,'function_map',None)
                adapter=getattr(mapper,'selection_adapters',{}).get(getattr(gate,'published_map',None))
                pointer=adapter.pointer if adapter is not None else None
                if adapter is not None and (mapper.library._handle!=self.library._handle or adapter.stream!=owner.stream_handle):raise ValueError('native selection kernel DLL/stream generation')
                if not attach(self.pointer,pointer):raise RuntimeError('native selection kernel publication refused')
                self.selection_adapter=adapter
                if adapter is not None and getattr(self,'selection_queue_reader',None) is not None:adapter.native_settlement_kernel=self
                if mapper is not None:mapper.retire(getattr(gate,'published_map',None))
            data=(ctypes.c_ubyte*len(blocked)).from_buffer(blocked)
            if self.semantic_create is not None:
                try:
                    accepted=bool(self.library.midi_cc_kernel_semantic_configure(self.pointer,generation,flags,data,len(data)))
                    if accepted and self._compound_reset_requested:
                        accepted=bool(self.selection_adapter) and self._prepare_reset_addresses(owner,scalar['defaults'])
                        prepare=getattr(self.library,'midi_cc_kernel_reset_tail_prepare',None)
                        if accepted and prepare is not None:
                            prepare.argtypes=[ctypes.c_void_p,ctypes.c_uint];prepare.restype=ctypes.c_int
                            accepted=bool(prepare(self.pointer,1|(int(bool(owner.soundfont_presets.extension_count))<<1)))
                        else:accepted=False
                    if self._manual_requested:
                        self.manual_ready=accepted and bool(generation) and self.manual_journal is not None
                        if self.manual_ready:self.manual_signature=(owner.stream_handle,owner.stream_generation,owner.channel_policy.revision,owner.addresses.count)
                    return accepted
                finally:
                    if generation:self.library.midi_cc_semantic_release(generation)
            return bool(self.library.midi_cc_kernel_configure(self.pointer,flags,data,len(data)))

    def prepare_failure_notification(self,owner):
        sink=owner.performance.failure_sink
        if type(sink) is not DeferredPerformanceFailureLog or sink.owner is not owner:return False
        function=getattr(self.library,'midi_cc_kernel_failure_notification_prepare',None)
        if function is None or self.semantic_journal is None or self.semantic_journal.failures is None:return False
        if not self.semantic_journal.failures.prepare_growth():return False
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        if not function(self.pointer,1):return False
        self.semantic_failure_sink=sink;self.failure_notification_admitted=True;return True

    def attach_raw_context(self,context):
        """Explicit actor-qualified decoder root, retained by the native kernel."""
        if context is not None and (not context.pointer or context.library._handle!=self.library._handle or
                self.owner is None or context.stream!=self.owner.stream_handle):raise ValueError('RAW kernel decoder generation')
        attach=getattr(self.library,'midi_cc_kernel_raw_context',None)
        if attach is None:return context is None
        attach.argtypes=[ctypes.c_void_p,ctypes.c_void_p];attach.restype=ctypes.c_int
        with self.lock:
            if not attach(self.pointer,context.pointer if context is not None else None):return False
            self.raw_context=context;return True

    def attach_notes(self,owner,notes):
        """Candidate control preparation; preload/selection remain real gaps."""
        if owner.font_preload_callback is not None and self.first_use_plan is None:raise ValueError('midi_mixed_kernel unresolved preload path')
        if not notes.pointer or notes.library._handle!=self.library._handle:raise ValueError('midi_mixed_kernel note DLL image')
        if notes.channels!=self.channels or not notes.configure(owner.midi_options) or not notes.seed(owner.midi_options.active_notes):return False
        with self.lock,notes.lock:
            if self.notes is not None or self.pending is not None:return False
            if not self.library.midi_cc_kernel_notes(self.pointer,notes.pointer):return False
            self.notes=notes;return True

    def attach_first_use_plan(self,plan,requests):
        """Explicit immutable legacy-attempt attachment; never infer sources.

        Each channel row is(bank,Program,LSB,qualification bits). Missing source
        proof is0 and causes native ordered handoff, not guessed preparation.
        The plan owns source epochs/fonts and releases only after kernel detach.
        """
        owner=self.owner
        if owner is None or not hasattr(self.library,'midi_cc_kernel_firstuse_version'):return False
        version=self.library.midi_cc_kernel_firstuse_version;version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:return False
        if (plan.library._handle!=self.library._handle or plan.stream!=owner.stream_handle or
                plan.stream_generation!=owner.stream_generation or not plan.current):raise ValueError('first-use plan identity')
        rows=tuple(tuple(row) for row in requests)
        if len(rows)!=128 or any(len(row)!=4 for row in rows):raise ValueError('first-use channel rows')
        for bank,program,lsb,qualification in rows:
            if any(type(value) is not int for value in (bank,program,lsb,qualification)) or not(
                    0<=bank<=128 and 0<=program<128 and 0<=lsb<128 and 0<=qualification<=7):raise ValueError('first-use source row')
        attach=self.library.midi_cc_kernel_firstuse
        attach.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint64,ctypes.c_uint64,ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint];attach.restype=ctypes.c_int
        values=(ctypes.c_uint*512)(*(value for row in rows for value in row))
        with self.lock,plan.borrow() as pointer:
            if not self.pointer or not pointer or self.first_use_plan is not None:return False
            if not attach(self.pointer,pointer,plan.epoch,plan.stream_generation,plan.stream,plan.mode,values,len(values)):return False
            self.first_use_plan=plan;return True

    def detach_first_use_plan(self):
        """Detach only at the native owner's acknowledged pause boundary.

        This does not cancel/join/destroy the former plan. Factory/runtime
        retain it until source I/O and all native borrowers retire safely.
        """
        with self.lock:
            if not self.pointer:return False
            detach=self.library.midi_cc_kernel_firstuse
            detach.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint64,ctypes.c_uint64,ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint];detach.restype=ctypes.c_int
            if not detach(self.pointer,None,0,0,0,0,None,0):return False
            self.first_use_plan=None
            return True

    def submit_mixed(self,stream,data,port=0,mode=BASS_MIDI_EVENTS_RAW|BASS_MIDI_EVENTS_FILTER|BASS_MIDI_EVENTS_SYNC,*,timing=None,received=None,origin=None):
        if self.notes is None:raise ValueError('midi_mixed_kernel notes not attached')
        if timing is not None and timing.pointer and timing.dll._handle!=self.library._handle:raise ValueError('midi_mixed_kernel timing DLL image')
        raw=bytearray(data);source=(ctypes.c_ubyte*len(raw)).from_buffer(raw);output=(ctypes.c_ubyte*(len(raw)*5+4))()
        count=max(1,len(raw)//3);actions=(_NativeCCAction*count)()
        note_capacity=count*(4 if self.semantic_mode_seed is not None else 1)
        notes=(_NativeNoteAction*note_capacity)();note_count=ctypes.c_uint64();result=(ctypes.c_uint64*8)()
        fields=(ctypes.c_uint64*7)(received or 0,(origin or {}).get('read_qpc') or received or 0,
            (origin or {}).get('pid',0),(origin or {}).get('epoch',0),(origin or {}).get('sequence',0),
            int((origin or {}).get('quality')=='driver'),int(origin is not None))
        with self.lock:
            self._refresh_frame_owner()
            if self._frame_owner is not None:raise ValueError('original frame receipt pending')
            status=self.library.midi_mixed_kernel_run(self.pointer,stream,mode,port,source,len(raw),output,len(output),actions,count,
                notes,note_capacity,ctypes.byref(note_count),timing.pointer if timing is not None else None,fields,result)
            report=dict(status=status,wire=_native_owned_bytes(output,result[0]),actions=tuple((row.packed,row.channel,row.event,row.raw,row.accepted,row.error,row.qpc)
                for row in _native_items(actions,result[1])),note_actions=tuple((row.packed,row.channel,row.count,row.qpc) for row in _native_items(notes,note_count.value)),
                raw_calls=int(result[2]),admitted=int(result[3]),error=ctypes.c_int(result[4]).value,consumed=int(result[5]),
                send_failures=int(result[6]),send_calls=int(result[7]),unlocked=status!=-2)
            if status==-2:self.pending=report
            report['semantic_pending']=self.semantic_pending(stream)
            self._remember_frame(report,stream,mode,port,bytes(raw),timing,tuple(fields))
            return report

    def replay_mixed(self,owner,report,sources):
        with owner.state_write():
            if not self.replay(owner,report):return False
            # Original action records are never coalesced: suppressed Off still
            # releases the activity FIFO. Processing time is not ingress timing.
            note_report=report.setdefault('note_replay',dict(actions=report['note_actions'],
                activity_already_applied=report.get('activity_already_applied',False)))
            self.notes.replay(owner,note_report,sources)
            adapter=getattr(self,'selection_adapter',None)
            if adapter is not None and not adapter.settle_failed_tickets():return False
            return True

    def submit(self,stream,data,port=0,mode=BASS_MIDI_EVENTS_RAW|BASS_MIDI_EVENTS_FILTER|BASS_MIDI_EVENTS_SYNC):
        raw=bytearray(data);source=(ctypes.c_ubyte*len(raw)).from_buffer(raw)
        output=(ctypes.c_ubyte*(len(raw)*5+4))();rows=(_NativeCCAction*max(1,len(raw)//3))();result=(ctypes.c_uint64*8)()
        with self.lock:
            self._refresh_frame_owner()
            if self._frame_owner is not None:raise ValueError('original frame receipt pending')
            status=self.library.midi_cc_kernel_run(self.pointer,stream,mode,port,source,len(raw),output,len(output),rows,len(rows),result)
            report=dict(status=status,wire=_native_owned_bytes(output,result[0]),actions=tuple((row.packed,row.channel,row.event,row.raw,row.accepted,row.error,row.qpc)
                for row in _native_items(rows,result[1])),raw_calls=int(result[2]),admitted=int(result[3]),error=ctypes.c_int(result[4]).value,
                consumed=int(result[5]),send_failures=int(result[6]),send_calls=int(result[7]),unlocked=status!=-2)
            if status==-2:self.pending=report
            report['semantic_pending']=self.semantic_pending(stream)
            self._remember_frame(report,stream,mode,port,bytes(raw),None,None)
            return report

    def _frame_status(self):
        version=getattr(self.library,'midi_cc_kernel_frame_version',None)
        if version is None:return None  # Old image ABI keeps its old behavior.
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:return None
        function=self.library.midi_cc_kernel_frame_status
        function.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint];function.restype=ctypes.c_int
        row=(ctypes.c_uint64*8)()
        if function(self.pointer,row,len(row))!=len(row):raise ValueError('native frame status ABI')
        return tuple(row)

    def _refresh_frame_owner(self):
        if self._frame_owner is not None:
            row=self._frame_status()
            if row is not None and not row[0]:self._frame_owner=None

    def _remember_frame(self,report,stream,mode,port,original,timing,origin):
        row=self._frame_status()
        if row is None:return
        report['frame_receipt']=row[0];report['frame_consumed']=row[5] if row[0] else report['consumed']
        if row[0]:self._frame_owner=(report,row[0],stream,mode,port,original,timing,origin)

    def resume_frame(self,report):
        """Execute only a receipt's original remaining bytes; return new metadata.

        Replay prior reports before resuming. Replay remains metadata-only;
        this API never retries a RAW failure or resends already executed bytes.
        """
        with self.lock:
            owner=self._frame_owner
            if owner is None or owner[0] is not report:raise ValueError('original frame report ownership')
            if self.pending is not None:raise ValueError('frame BASS unlock pending')
            _report,receipt,stream,mode,port,raw,timing,origin=owner
            row=self._frame_status()
            if row is None or row[0]!=receipt:raise ValueError('stale native frame receipt')
            source=(ctypes.c_ubyte*len(raw)).from_buffer_copy(raw);output=(ctypes.c_ubyte*(len(raw)*5+4))()
            capacity=max(1,len(raw)//3);actions=(_NativeCCAction*capacity)()
            note_capacity=capacity*4;notes=(_NativeNoteAction*note_capacity)();note_count=ctypes.c_uint64();result=(ctypes.c_uint64*8)()
            fields=(ctypes.c_uint64*7)(*origin) if origin is not None else None
            function=self.library.midi_cc_kernel_frame_resume
            function.argtypes=[ctypes.c_void_p,ctypes.c_uint64,ctypes.c_uint]+[ctypes.c_uint]*3+[ctypes.c_void_p,ctypes.c_uint,
                ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(_NativeCCAction),ctypes.c_uint,ctypes.POINTER(_NativeNoteAction),
                ctypes.c_uint,ctypes.POINTER(ctypes.c_uint64),ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.POINTER(ctypes.c_uint64)]
            function.restype=ctypes.c_int
            status=int(function(self.pointer,receipt,row[5],stream,mode,port,source,len(raw),output,len(output),actions,capacity,
                notes if origin is not None else None,note_capacity if origin is not None else 0,
                ctypes.byref(note_count) if origin is not None else None,timing.pointer if timing is not None else None,fields,result))
            fragment=dict(status=status,wire=_native_owned_bytes(output,result[0]),
                actions=tuple((r.packed,r.channel,r.event,r.raw,r.accepted,r.error,r.qpc) for r in _native_items(actions,result[1])),
                raw_calls=int(result[2]),admitted=int(result[3]),error=ctypes.c_int(result[4]).value,consumed=int(result[5]),
                send_failures=int(result[6]),send_calls=int(result[7]),unlocked=status!=-2)
            if origin is not None:fragment['note_actions']=tuple((r.packed,r.channel,r.count,r.qpc) for r in _native_items(notes,note_count.value))
            fragment['semantic_pending']=self.semantic_pending(stream)
            current=self._frame_status();fragment['frame_receipt']=current[0]
            fragment['frame_consumed']=current[5] if current[0] else row[5]+fragment['consumed']
            report['frame_consumed']=fragment['frame_consumed']
            if not current[0]:self._frame_owner=None
            if status==-2:self.pending=fragment
            return fragment

    def retry(self):
        with self.lock:
            error=ctypes.c_int();accepted=self.library.midi_cc_kernel_retry(self.pointer,ctypes.byref(error))
            if accepted!=1:raise RuntimeError(tr('runtime.midi_filter_config_failed',error=error.value))
            report=self.pending
            if report is not None:
                if hasattr(self.library,'midi_cc_kernel_take_actions'):
                    take=self.library.midi_cc_kernel_take_actions
                    take.argtypes=[ctypes.c_void_p,ctypes.POINTER(_NativeNoteAction),ctypes.c_uint];take.restype=ctypes.c_int
                    rows=(_NativeNoteAction*4096)();count=int(take(self.pointer,rows,len(rows)))
                    if count<0:raise RuntimeError(tr('runtime.midi_filter_config_failed',error='compound action drain '+str(count)))
                    if count:report['note_actions']=report.get('note_actions',())+tuple((row.packed,row.channel,row.count,row.qpc) for row in rows[:count])
                report['unlocked']=True;self.pending=None
                report['semantic_pending']=self.semantic_pending(self.owner.stream_handle) if self.owner is not None else False
            return report

    def replay(self,owner,report,*,source=None):
        """Metadata only. No BASS or delay setter/audio resubmission."""
        if not report['unlocked']:raise RuntimeError(tr('runtime.midi_filter_config_failed',error='CC unlock pending'))
        if self.first_use_plan is not None and self.first_use_plan.current and not self.first_use_plan.replay_cache():return False
        if self.journal is not None:return owner.drain_native_controls()
        for packed,channel,event,raw,accepted,_error,_qpc in report['actions']:
            cc=(packed>>8)&255;value=(packed>>16)&255
            owner.auxiliary_cc.receive(channel,cc,value)
            if not event or not accepted:continue
            if event==MIDI_EVENT_USERFX:
                owner.delay_runtime.states[channel].send=value
                if channel==9:owner.delay_runtime.melodic_nine.send=value
                if value and owner.addresses.drums[channel] and channel!=9 and channel not in owner.drum_delay_warnings:
                    owner.drum_delay_warnings.add(channel)
                    owner.append_log(log_text('runtime.drum_delay_port_limited',port=channel//16,channel=channel%16+1))
                owner.record_origin(channel,event,raw,source,field='delay_send')
            else:
                values=owner.channel_sends.channel_reverb if event==MIDI_EVENT_REVERB else owner.channel_sends.channel_chorus
                values[channel]=value
                owner.record_origin(channel,event,raw,source)

    def _observe(self,_sync,stream,data,event):
        owner=self.owner
        if owner is None or stream!=owner.stream_handle:return
        channel=(data>>16)&65535;raw=data&65535
        if event==0x1d0000:
            for key in tuple(owner.control_origins):
                if key[0]==channel:owner.control_origins.pop(key,None)
            return
        if event&0xf0000==0xa0000:
            owner.auxiliary_cc.receive(channel,event&65535,raw);return
        if event&0xf0000==0xb0000:
            if event&65535==MIDI_EVENT_USERFX:
                owner.delay_runtime.states[channel].send=raw
                if channel==9:owner.delay_runtime.melodic_nine.send=raw
                return  # set_channel_send's pre-RAW writer creates no origin.
            values=owner.channel_sends.channel_reverb if event&65535==MIDI_EVENT_REVERB else owner.channel_sends.channel_chorus
            values[channel]=raw;return
        if event==MIDI_EVENT_USERFX:
            owner.delay_runtime.states[channel].send=raw
            if channel==9:owner.delay_runtime.melodic_nine.send=raw
            if raw and owner.addresses.drums[channel] and channel!=9 and channel not in owner.drum_delay_warnings:
                owner.drum_delay_warnings.add(channel)
                owner.append_log(log_text('runtime.drum_delay_port_limited',port=channel//16,channel=channel%16+1))
            owner.record_origin(channel,event,raw,'native_midi',field='delay_send')
        else:owner.record_origin(channel,event,raw,'native_midi')

    def close(self):
        with self.lock:
            if self.pointer:
                if not self.library.midi_cc_kernel_destroy(self.pointer):
                    raise RuntimeError(tr('runtime.midi_filter_config_failed',error='CC unlock pending'))
                self.pointer=None
            self._frame_owner=None
            if self.journal is not None:self.journal.close();self.journal=None
            if self.semantic_journal is not None:self.semantic_journal.close();self.semantic_journal=None
            if self.manual_journal is not None:self.manual_journal.close();self.manual_journal=None
            if self.reset_journal is not None:self.reset_journal.close();self.reset_journal=None
            self.manual_ready=False
            self.semantic_decoders.clear();self.semantic_error_low.clear()
            self.semantic_gets.clear()
            if self.owner is not None and getattr(self.owner,'cc_native_kernel',None) is self:self.owner.cc_native_kernel=None
            self.owner=None
            self.notes=None # Native borrower released above; Python root may remain.
            self.raw_context=None
            self.first_use_plan=None
            self.semantic_actions=None


class _NativeExecutionControlStep(ctypes.Structure):
    _fields_=[('channel',ctypes.c_uint),('event',ctypes.c_uint),('parameter',ctypes.c_uint)]


class _NativeExecutionInstrumentStep(ctypes.Structure):
    # Own additive ABI1: five UINT32 fields, no platform-sized pointers.
    _fields_=[('channel',ctypes.c_uint),('mask',ctypes.c_uint),('bank',ctypes.c_uint),
              ('lsb',ctypes.c_uint),('program',ctypes.c_uint)]


class _NativeExecutionControlResult(ctypes.Structure):
    _fields_=[('channel',ctypes.c_uint),('event',ctypes.c_uint),('parameter',ctypes.c_uint),('accepted',ctypes.c_uint),('error',ctypes.c_int)]

class _NativeFXTopologySnapshot(ctypes.Structure):
    _fields_=[('exact',ctypes.c_uint64),('handle',ctypes.c_uint),('kind',ctypes.c_uint),('priority',ctypes.c_int),('reserved',ctypes.c_uint)]


class _NativeFXTopologySnapshot2(ctypes.Structure):
    _fields_=_NativeFXTopologySnapshot._fields_+[('pointer',ctypes.c_void_p),('kernel',ctypes.c_uint64)]


class _NativeExecutionManualStep(ctypes.Structure):
    _fields_=[('channel',ctypes.c_uint),('source',ctypes.c_uint),('destination',ctypes.c_uint),('flags',ctypes.c_uint),
              ('input',ctypes.c_int),('amount',ctypes.c_int),('event',ctypes.c_uint),('parameter',ctypes.c_uint)]


class NativeMidiOriginState:
    """Shared native MID3 validity/sequence state; input-side, candidate only."""
    def __init__(self,path=None):
        self.lock=threading.RLock()
        self.pointer=None;self.library=ctypes.PyDLL(str(path or os.environ.get('ABMSYNTH_OBSERVATION_DLL',Path(__file__).with_name('abmsynth_dsp.dll'))))
        self.library.midi_execution_origin_create.argtypes=[];self.library.midi_execution_origin_create.restype=ctypes.c_void_p
        self.library.midi_execution_origin_release.argtypes=[ctypes.c_void_p];self.library.midi_execution_origin_release.restype=None
        self.pointer=self.library.midi_execution_origin_create()
        if not self.pointer:raise MemoryError('midi_execution_origin_create')

    def close(self):
        with self.lock:
            if self.pointer:self.library.midi_execution_origin_release(self.pointer);self.pointer=None


class NativeMidiExecution:
    """Candidate queue consumer with native execution and bounded Note FIFO.

    No product auto-admission. One queue and prepared kernel are exclusively
    leased until native join + all metadata/handoff delivery. Python read/replay
    may lag without delaying qualified audio until finite backpressure. Unknown
    grammar is a FIFO handoff, not dropped/coalesced/normalized MIDI. A frozen
    generation and caller-owned stream/timing/functions live until close succeeds.
    Ordered general GUI commands/trace/preload/multi-client session admission
    remain required before this consumer can replace the complete engine.
    """
    fields=('frames_dequeued','frames_native','raw_failures','frames_delegated','note_records','records_read',
        'mmcss_registered','mmcss_error','thread_id','mmcss_reverted','mmcss_revert_error','ended','stop_requested',
        'handoff_kind','metadata_backpressure','peak_records','peak_metadata_bytes','payload_bytes','send_failures',
        'invalid_origins','transport_fault_intervals','terminal_bits','queued_records','queued_metadata_bytes','handoff','handoff_ticket')
    def __init__(self,queue,kernel,*,origin_state=None,timing=None,port=0,mode=BASS_MIDI_EVENTS_RAW|BASS_MIDI_EVENTS_FILTER|BASS_MIDI_EVENTS_SYNC,multi=False):
        self.pointer=None;self.queue=queue;self.kernel=kernel;self.owner=kernel.owner;self.timing=timing;self.pending=None
        self._lifetime=threading.RLock();self._calls=0;self._read_lock=threading.Lock();self._controls={};self._raw_frames={}
        self.multi=multi;self._clients={};self._client_ports={};self.prepared_roots=[];self.effect_graph_roots=[]
        if type(multi) is not bool or (multi and queue is not None) or (not multi and queue is None):raise ValueError('midi_execution queue/session admission')
        self.library=kernel.library;self.sources={channel:self.owner.soundfont_presets.preset_key(channel) for channel in range(kernel.channels)}
        idle_version=getattr(self.library,'midi_execution_idle_version',None)
        self.auto_reset_ready=False
        if idle_version is not None:
            idle_version.argtypes=[];idle_version.restype=ctypes.c_uint
            self.auto_reset_ready=idle_version()==1
        self.own_origins=origin_state is None;self.origins=origin_state
        if (queue is not None and queue.library._handle!=self.library._handle) or (origin_state is not None and origin_state.library._handle!=self.library._handle):
            raise ValueError('midi_execution queue/origin DLL image')
        if timing is not None and timing.pointer and timing.dll._handle!=self.library._handle:raise ValueError('midi_execution timing DLL image')
        if self.owner.diagnostic_trace is not None:raise ValueError('midi_execution diagnostic trace admission pending')
        self.rows=(_NativeNoteAction*(1024*1024//3+1))();self.payload=(ctypes.c_ubyte*(1024*1024))();self.descriptor=(ctypes.c_uint64*(34 if multi else 32))()
        for name,args,result in (
            ('version',[],ctypes.c_uint),('create',[ctypes.c_void_p]*4+[ctypes.c_uint]*3,ctypes.c_void_p),
            ('start',[ctypes.c_void_p],ctypes.c_int),('stop',[ctypes.c_void_p],None),('release',[ctypes.c_void_p],ctypes.c_int),
            ('read',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint,ctypes.POINTER(_NativeNoteAction),ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint],ctypes.c_int),
            ('resume',[ctypes.c_void_p,ctypes.c_uint64,ctypes.c_uint],ctypes.c_int),('snapshot',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint],ctypes.c_uint)):
            function=getattr(self.library,'midi_execution_'+name);function.argtypes=args;function.restype=result
        if self.library.midi_execution_version()!=1:raise ValueError('midi_execution ABI')
        self.activity_receipt_ready=False
        activity_version=getattr(self.library,'midi_execution_activity_receipt_version',None)
        if activity_version is not None:
            activity_version.argtypes=[];activity_version.restype=ctypes.c_uint
            if activity_version()!=1:raise ValueError('native execution activity receipt ABI')
            receipt=self.library.midi_execution_activity_receipt
            receipt.argtypes=[ctypes.c_void_p,ctypes.c_uint64];receipt.restype=ctypes.c_int
            self.activity_receipt_ready=True
        self.activity_receipt_enabled=(self.activity_receipt_ready and getattr(kernel.notes,'activity',None) is not None)
        # Additive ABI: old images retain the original single-record decoder.
        # CDLL releases GIL while waiting; storage is owned here until one ACK
        # after every exact record has replayed. No MIDI/audio call is batched.
        self.metadata_batch_ready=False;self.metadata_batch_enabled=False
        self._batch_count=0;self._batch_next=0;self._batch_completed=0
        batch_version=getattr(self.library,'midi_execution_read_batch_version',None)
        if batch_version is not None:
            batch_version.argtypes=[];batch_version.restype=ctypes.c_uint
            if batch_version()==1:
                for name,args,result in (
                    ('read_notes_batch',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(_NativeNoteAction),ctypes.c_uint],ctypes.c_int),
                    ('metadata_batch_ack',[ctypes.c_void_p,ctypes.c_uint64,ctypes.c_uint],ctypes.c_int),
                    ('metadata_batch_snapshot',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint],ctypes.c_uint)):
                    function=getattr(self.library,'midi_execution_'+name);function.argtypes=args;function.restype=result
                self._batch_descriptors=(ctypes.c_uint64*(64*35))()
                self._batch_notes=(_NativeNoteAction*4096)()
                # Explicit audit opt-in until same-image throughput qualifies.
                # F863 comparison regressed despite fewer FFI reads; availability
                # alone is not evidence for changing the default product path.
                self.metadata_batch_ready=True
        if multi:
            for name,args,result in (
                ('multi_version',[],ctypes.c_uint),('multi_create',[ctypes.c_void_p]*3+[ctypes.c_uint]*2,ctypes.c_void_p),
                ('attach',[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint],ctypes.c_uint64),
                ('detach',[ctypes.c_void_p,ctypes.c_uint64],ctypes.c_int),
                ('client_snapshot',[ctypes.c_void_p,ctypes.c_uint64,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint],ctypes.c_int)):
                function=getattr(self.library,'midi_execution_'+name);function.argtypes=args;function.restype=result
            if self.library.midi_execution_multi_version()!=1:raise ValueError('midi_execution multi ABI')
        self.submit_control=getattr(self.library,'midi_execution_control',None)
        # Additive Panic ABI: owned immutable command, same lifetime as controls.
        self.submit_panic=getattr(self.library,'midi_execution_panic',None)
        if self.submit_panic is not None:
            self.submit_panic.argtypes=[ctypes.c_void_p];self.submit_panic.restype=ctypes.c_uint64
        self.submit_manual=getattr(self.library,'midi_execution_manual',None)
        if self.submit_manual is not None:
            self.submit_manual.argtypes=[ctypes.c_void_p,ctypes.POINTER(_NativeExecutionManualStep),ctypes.c_uint,ctypes.c_uint]
            self.submit_manual.restype=ctypes.c_uint64
        if self.submit_control is not None:
            self.library.midi_execution_control_version.argtypes=[];self.library.midi_execution_control_version.restype=ctypes.c_uint
            if self.library.midi_execution_control_version()!=1:raise ValueError('midi_execution_control ABI')
            self.submit_control.argtypes=[ctypes.c_void_p,ctypes.POINTER(_NativeExecutionControlStep),ctypes.c_uint,ctypes.c_uint]
            self.submit_control.restype=ctypes.c_uint64
        if self.origins is None:self.origins=NativeMidiOriginState(self.library._name)
        with queue.borrow() if queue is not None else nullcontext(None) as queue_pointer,kernel.lock,self.origins.lock:
            with timing.lock if timing is not None else nullcontext():
                if timing is not None and not timing.pointer:
                    if self.own_origins:self.origins.close()
                    raise ValueError('midi_execution timing owner not installed')
                if multi:self.pointer=self.library.midi_execution_multi_create(kernel.pointer,self.origins.pointer,timing.pointer if timing is not None else None,self.owner.stream_handle,mode)
                else:self.pointer=self.library.midi_execution_create(queue_pointer,kernel.pointer,self.origins.pointer,timing.pointer if timing is not None else None,self.owner.stream_handle,mode,port)
        self.port=port;self.mode=mode
        if not self.pointer:
            if self.own_origins:self.origins.close()
            raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_execution_create'))

    @contextmanager
    def borrow(self):
        with self._lifetime:
            pointer=self.pointer
            if pointer:self._calls+=1
        try:yield pointer
        finally:
            if pointer:
                with self._lifetime:self._calls-=1

    def start(self):
        with self.borrow() as pointer:return bool(pointer and self.library.midi_execution_start(pointer))

    def configure_auto_reset(self, seconds):
        if not self.auto_reset_ready:return False
        function=self.library.midi_execution_idle_configure
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        with self.borrow() as pointer:return bool(pointer and function(pointer,seconds))

    def record_auto_reset_input(self):
        """Original delegate's entry; same frame token prevents suffix re-arm."""
        function=self.library.midi_execution_idle_enter
        function.argtypes=[ctypes.c_void_p];function.restype=ctypes.c_int
        with self.borrow() as pointer:
            if not pointer or function(pointer) not in (1,2):raise ValueError('idle parser frame ownership')

    def auto_reset_snapshot(self):
        if not self.auto_reset_ready:return {}
        function=self.library.midi_execution_idle_snapshot
        function.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint];function.restype=ctypes.c_uint
        values=(ctypes.c_uint64*10)()
        with self.borrow() as pointer:
            if not pointer or function(pointer,values,len(values))!=len(values):return {}
        return dict(zip(('frequency','configuration_epoch','delay_seconds','last_parser_entry','fired','claims','completed','claimed_epoch','frame_active','frame_entered'),values))

    def queue_raw(self,payload,*,port=0,received_stamp,trace=0,origin=None,on_complete=None):
        """Whole direct frame joins the original ingress publication order.

        Copied before return, no decoder pre-commit or Python audio call.
        Refusal is pre-audio and callers retain their same bytes for retry.
        Uses this execution's original RAW/FILTER/SYNC/ASYNC policy, not a
        per-producer alternate decoder or a competing BASS callback actor.
        """
        if (not self.multi or type(port) is not int or not 0<=port<8 or
                type(received_stamp) is not int or not 0<received_stamp<2**64 or
                type(trace) is not int or not -(2**63)<=trace<2**63 or
                (on_complete is not None and not callable(on_complete))):raise ValueError('RAW actor admission')
        wire=bytes(payload)
        if not 0<len(wire)<=1024*1024:raise ValueError('RAW actor frame bounds')
        if origin is not None:
            origin=tuple(origin)
            if len(origin)!=8 or any(type(v) is not int or not 0<=v<2**64 for v in origin):raise ValueError('RAW actor origin')
        submit=getattr(self.library,'midi_execution_raw',None)
        if submit is None:return 0
        submit.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint64,
            ctypes.c_int64,ctypes.POINTER(ctypes.c_uint64)];submit.restype=ctypes.c_uint64
        native=(ctypes.c_ubyte*len(wire)).from_buffer_copy(wire)
        stamp=(ctypes.c_uint64*8)(*origin) if origin is not None else None
        # Registration covers CDLL's GIL release: metadata cannot observe an
        # accepted publication before its Python callback owner is retained.
        with self._lifetime:
            if not self.pointer:return 0
            ticket=int(submit(self.pointer,native,len(wire),port,received_stamp,trace,stamp))
            if ticket:self._raw_frames[ticket]=dict(wire=wire,port=port,stamp=received_stamp,
                trace=trace,origin=origin,on_complete=on_complete)
            return ticket

    def set_trace_enabled(self,enabled):
        if type(enabled) is not bool:raise ValueError('native trace policy')
        function=getattr(self.library,'midi_execution_trace_enable',None)
        version=getattr(self.library,'midi_execution_trace_version',None)
        if function is None or version is None:raise RuntimeError('native trace ABI unavailable')
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:raise RuntimeError('native trace ABI')
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_int
        with self.borrow() as pointer:
            if not pointer or not function(pointer,int(enabled)):raise RuntimeError('native trace admission')

    def trace_observation_statistics(self):
        """Optional diagnostic loss, never MIDI event/completion loss."""
        version=getattr(self.library,'midi_execution_trace_observation_version',None)
        function=getattr(self.library,'midi_execution_trace_observation_snapshot',None)
        if version is None or function is None:return None
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_execution_trace_observation_version'))
        function.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint];function.restype=ctypes.c_uint
        values=(ctypes.c_uint64*4)()
        with self.borrow() as pointer:
            if not pointer or function(pointer,values,len(values))!=4:return None
        return dict(zip(('captured_frames','dropped_frames','read_rows','queued_pairs'),values))

    def prepare_retained_records(self):
        """Native producer-space transfer; original Python semantic ACK stays."""
        version=getattr(self.library,'midi_execution_retained_records_version',None)
        function=getattr(self.library,'midi_execution_retained_records_prepare',None)
        if version is None or function is None:return False
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_execution_retained_records_version'))
        function.argtypes=[ctypes.c_void_p];function.restype=ctypes.c_int
        with self.borrow() as pointer:
            if not pointer or function(pointer)!=1:raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_execution_retained_records_prepare'))
        self.metadata_batch_enabled=False
        return True

    def retained_record_statistics(self):
        function=getattr(self.library,'midi_execution_retained_records_snapshot',None)
        if function is None:return None
        function.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint];function.restype=ctypes.c_uint
        values=(ctypes.c_uint64*6)()
        with self.borrow() as pointer:
            if not pointer or function(pointer,values,len(values))!=6:return None
        return dict(zip(('queued_records','queued_bytes','transferred','allocation_failures','enabled','ended'),values))

    @property
    def failure_notification_admitted(self):return self.kernel.failure_notification_admitted

    def publish_failure_notification_sink(self,sink):
        report=self.pending
        if report is None or report['disposition'] not in (2,11):raise RuntimeError(tr('runtime.failure_sink_order_required'))
        if not self.owner.drain_native_controls():raise RuntimeError(tr('runtime.failure_sink_pending_records'))
        log_only=type(sink) is DeferredPerformanceFailureLog and sink.owner is self.owner
        function=self.library.midi_execution_failure_notification_at
        function.argtypes=[ctypes.c_void_p,ctypes.c_uint64,ctypes.c_uint];function.restype=ctypes.c_int
        if not function(self.pointer,report['ticket'],int(log_only)):raise RuntimeError(tr('runtime.failure_sink_publication_refused'))
        self.kernel.semantic_failure_sink=sink if log_only else None
        self.owner.performance._native_failure_compatibility=not log_only
        gate=self.owner.channel_native_filter;gate.signature=gate.checked_key=None

    def complete_raw(self,report):
        """Only final copied metadata/handoff completion ACKs direct frames."""
        if report.get('client')!=2**64-1 or report['disposition'] in (3,6,7,8,10):return
        publication=report['publication']
        with self._lifetime:
            registration=self._raw_frames.get(publication)
            if registration is None:return # Already ACKed after metadata-only retry.
            report['raw_terminal_ack']=True;self.pending=report
            callback=registration['on_complete']
            if callback is not None:callback(report)
            if registration.get('native_file'):
                values=(ctypes.c_uint64*4)()
                if self.library.midi_execution_file_receipt(self.pointer,publication,values,None,0,1)!=1:
                    raise ValueError('native file completion owner')
            self._raw_frames.pop(publication)
            self.pending=None

    def attach_queue(self,queue,*,port=0):
        """Candidate8-client attachment; one common kernel, no MIDI resubmit."""
        if not self.multi:raise ValueError('midi_execution not a multi session')
        if type(port) is not int or not 0<=port<8 or queue.library._handle!=self.library._handle:raise ValueError('midi_execution client identity')
        with self._lifetime,queue.borrow() as source:
            if not self.pointer or not source:return 0
            client=int(self.library.midi_execution_attach(self.pointer,source,port))
            if client:self._clients[client]=queue;self._client_ports[client]=port
            return client

    def client_statistics(self,client):
        values=(ctypes.c_uint64*8)()
        with self.borrow() as pointer:
            if not self.multi or not pointer or not self.library.midi_execution_client_snapshot(pointer,client,values,len(values)):return {}
        return dict(zip(('client','queued_frames','reader_ended','reader_stopping','pending_metadata','inflight','read_frames','terminal_bits'),values))

    def detach_queue(self,client):
        with self._lifetime:
            if not self.multi or client not in self._clients:return False
            if self.pending is not None and self.pending.get('client')==client:return False
            if not self.pointer or not self.library.midi_execution_detach(self.pointer,client):return False
            self._clients.pop(client);self._client_ports.pop(client);return True

    def hydrate_legacy_handoff(self,report,*,preserve_activity=False):
        """Stage selection and activity together before an owned legacy writer.

        Type2/11 exact delivered native ticket is the authority, not a GUI
        sample. Retained journals still supply origins/inputs/Learn and global
        timbre order. No raw resend, journal suppression or audio work here.
        Caller holds MIDI ownership continuously through the legacy delegate.
        """
        if report is not self.pending or report.get('disposition') not in (2,11):
            raise ValueError('legacy hydration handoff identity')
        if report.get('legacy_state_hydrated'):return
        owner=self.owner
        if not owner.drain_native_controls():raise RuntimeError('legacy hydration metadata not drained')
        adapter=getattr(self.kernel,'selection_adapter',None)
        selection_stage=provenance_stage=None
        handoff=getattr(self.library,'midi_execution_selection_handoff_version',None)
        selection_ready=False
        if handoff is not None:
            handoff.argtypes=[];handoff.restype=ctypes.c_uint;selection_ready=handoff()==1
        # The ordered compatibility-publication fence already hydrated the
        # native selection before Python became its writer. While every RAW
        # frame is delegated, that native selection root is dormant, not an
        # authority over subsequent Python timbre/route edits. Activity still
        # has its independent exact-ticket transfer below. Returning to native
        # log-only policy republishes a full fresh selection generation.
        if adapter is not None and selection_ready and not getattr(owner.performance,'_native_failure_compatibility',False):
            provenance=getattr(self.library,'midi_execution_provenance_handoff_version',None)
            provenance_ready=False
            if provenance is not None:
                provenance.argtypes=[];provenance.restype=ctypes.c_uint;provenance_ready=provenance()==1
            image=adapter.core_snapshot_at(self,report['ticket'],metadata=True,provenance=provenance_ready)
            # Old additive images retain journal-owned exact legacy state.
            if provenance_ready:
                if image is None:raise ValueError('provenance handoff snapshot unavailable')
                provenance_stage=adapter.stage_provenance_state(owner,image);selection_stage=provenance_stage.selection
            elif image is not None:selection_stage=adapter.stage_legacy_state(owner,image)
        activity=getattr(self.kernel.notes,'activity',None)
        # Font-only transactions change sources/profiles/Available, not Note
        # ownership. Keep that canonical image native. Generic legacy writers
        # and reset-policy fences retain the complete hydration path.
        keep_activity=bool(preserve_activity and report.get('disposition')==11 and
            report.get('source_edit_command') and activity is not None and
            getattr(self.kernel.notes,'activity_authoritative',False))
        if keep_activity:activity=None
        activity_stage=activity.stage_legacy_state(owner) if activity is not None else None
        if report is not self.pending:raise ValueError('legacy hydration displaced handoff')
        # Both schemas/stages and their epochs must pass AFTER all destination
        # locks are held, BEFORE either commit. No BASS/CDLL waits are allowed
        # in the pure Python commit region. The delivered worker ticket and
        # continuous MIDI ownership exclude original/product state writers.
        with owner.midi_lock,owner.performance.lock,owner.channel_policy.lock:
            activity_locks=(owner.soundfont_activity.lock,owner.midi_activity.lock) if activity is not None else (nullcontext(),nullcontext())
            with activity_locks[0],activity_locks[1]:
                if report is not self.pending:raise ValueError('legacy hydration displaced commit')
                if selection_stage is not None:adapter.validate_legacy_stage(owner,selection_stage)
                if activity is not None:activity.validate_legacy_stage(owner,activity_stage)
                if provenance_stage is not None:adapter.validate_provenance_stage(owner,provenance_stage)
                if activity is not None:activity._commit_legacy_state_unlocked(activity_stage)
                if selection_stage is not None:adapter._commit_legacy_state_unlocked(selection_stage)
                if provenance_stage is not None:adapter._commit_provenance_state_unlocked(owner,provenance_stage)
        report['legacy_state_hydrated']=True
        report['activity_hydrated']=activity is not None
        report['source_activity_preserved']=keep_activity
        report['selection_hydrated']=selection_stage is not None
        report['provenance_hydrated']=provenance_stage is not None

    def source_refresh_eligible(self,source):
        """Only source/mapping changes with identical audible control policy.

        Profile/extended changes and arbitrary Python failure callbacks keep
        the full compound transfer. Never use a stale GUI channel snapshot.
        """
        group=getattr(source,'_source_refresh_group',None)
        if (group is None or group.mappings or source.group.mappings or group.performance is not None or source.group.performance is not None or
                self.owner.soundfont_presets.performance is not None or
                getattr(self.owner.performance,'_native_failure_compatibility',False)):
            return False
        version=getattr(self.library,'midi_execution_source_refresh_version',None)
        if version is None:return False
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:return False
        def policy(overrides):
            result={}
            for key,item in overrides.items():
                if item.get('extended') or item.get('extended_disabled') or item.get('performance'):return None
                values=() if item.get('disabled') else tuple(item.get(name) for name in ('volume','attack','decay','release','cutoff','resonance'))
                if values and any(value is not None for value in values):result[key]=values
            return result
        target={(item.bank,item.preset):vars(item) for item in source.effective_presets(group)}
        previous=policy(self.owner.soundfont_presets.presets);current=policy(target)
        return previous is not None and current==previous

    def source_refresh_at(self,report,*,operation=0):
        """Own additive ABI1;512 DWORD address rows,no complete Python hydration.

        bass.h ChannelLock/StreamEvent are confined to the native transaction.
        Exact delivered type11 ticket and current selection root are mandatory.
        The native policy gates out profiles/extensions before any state write.
        """
        from bass_bindings import NativeChannelTransaction
        class Request(ctypes.Structure):
            _fields_=[('execution',ctypes.c_void_p),('ticket',ctypes.c_uint64),('selection',ctypes.c_void_p),
                ('stream',ctypes.c_uint),('channels',ctypes.c_uint),('operation',ctypes.c_uint),
                ('addresses',ctypes.POINTER(ctypes.c_uint)),('status',ctypes.c_int)]
        if report is not self.pending or report.get('disposition')!=11:raise ValueError('source refresh fence')
        if report.get('source_refresh_failed'):raise RuntimeError(tr('runtime.source_refresh_partial'))
        adapter=self.kernel.selection_adapter
        if adapter is None:return None
        transaction=getattr(adapter,'snapshot_transaction',None)
        if transaction is None:
            transaction=adapter.snapshot_transaction=NativeChannelTransaction(self.owner.performance.bass,native_path=self.library._name)
        rows=(ctypes.c_uint*512)()
        function=self.library.midi_execution_source_refresh_at;function.argtypes=[ctypes.c_void_p];function.restype=None
        with self.borrow() as pointer:
            request=Request(pointer,report['ticket'],adapter.pointer,self.owner.stream_handle,self.owner.addresses.count,operation,rows,0)
            result=transaction.run(self.owner.stream_handle,((2,function,0,ctypes.addressof(request)),))
        # A successful native body must never run twice on an unlock retry.
        if operation==1 and request.status==1:report['source_refresh_applied']=True
        if request.status==-8:report['source_refresh_failed']=True
        if not result.success:raise RuntimeError(tr('runtime.source_refresh_transaction'))
        if request.status==-8:raise RuntimeError(tr('runtime.source_refresh_partial'))
        return tuple(tuple(rows[index:index+4]) for index in range(0,512,4)) if request.status==1 else None

    def republish_generation(self,*,source_runtime=None,full_selection=False,reset_contracts_only=False):
        """Reprepare the shared generation at an exact legacy handoff barrier.

        No audio replay, fabricated source or flag change. Unknown source/LSB/
        ASYNC stays unqualified until its documented native path is admitted.
        """
        owner=self.owner;kernel=self.kernel;report=self.pending
        status=self.statistics()
        if report is None or report['disposition'] not in (2,11) or not status.get('handoff') or status.get('stop_requested'):return False
        if report['disposition']==11 and getattr(source_runtime,'source_commit_uncertain',False):return False
        if owner.stream_handle!=kernel.journal.stream or not self.pointer:return False
        checkpoint=report.setdefault('generation_republication',dict(identity=(owner.stream_handle,owner.stream_generation),done=set()))
        if checkpoint['identity']!=(owner.stream_handle,owner.stream_generation):return False
        done=checkpoint['done']
        fast=report.get('source_policy_preserved',False)
        if fast:
            if report.get('source_refresh_failed'):return False
            if not report.get('source_refresh_applied'):
                # A source writer can fail before configure_preset_controls
                # (for example SetFonts refusal). Reapply the retained policy
                # once,then republish the still-owned source generation.
                if self.source_refresh_at(report,operation=1) is None:return False
            # Sources changed,but functions/CC policy/Notes/reset contracts did
            # not. Keep their exact native generations and current clocks.
            done.update(('filter','kernel','notes_config','reset_contracts'))
        activity=getattr(kernel.notes,'activity',None)
        def publish_activity():
            if activity is None or 'activity_restore' in done:return True
            # Prefix metadata and the completed legacy delegate have already
            # populated the old mutable owners. Copy them as ONE complete
            # fenced image, never seed just physical counts into an attached
            # activity owner. A failure retains the paused checkpoint/roots.
            report['republish_stage']='activity_restore'
            if report.get('source_activity_preserved'):
                if fast:
                    rows=self.source_refresh_at(report,operation=2)
                    if rows is None:return False
                    sources={ch:None if row[3]==0xffffffff else (row[3]>>16,row[3]&65535) for ch,row in enumerate(rows)}
                else:sources={channel:owner.soundfont_presets.preset_key(channel) for channel in range(kernel.channels)}
                if self.restore_activity_state(report,None,sources,preserve_current=True)!=1:return False
                done.add('activity_restore');done.add('notes_seed');return True
            reference=checkpoint.setdefault('activity_reference',activity.export_state())
            image=activity.capture_legacy_state(owner,reference=reference)
            sources={channel:owner.soundfont_presets.preset_key(channel) for channel in range(kernel.channels)}
            if self.restore_activity_state(report,image,sources,expected_revision=reference['header'][0])!=1:return False
            done.add('activity_restore');done.add('notes_seed');return True
        with owner.activate(),owner.midi_lock:
            if 'metadata' not in done:
                report['republish_stage']='metadata'
                if not owner.drain_native_controls():return False
                done.add('metadata')
            # The composition root publishes BASS filters before CC semantic
            # tables borrow their observer/source generations.
            with owner.state_write():
                if 'filter' not in done:
                    report['republish_stage']='filter';gate=owner.channel_native_filter;mapper=getattr(gate,'function_map',None)
                    if full_selection and mapper is not None:mapper.full_selection_candidate=True;gate.signature=None;gate.checked_key=None
                    owner.ensure_event_filter();done.add('filter')
                stages=[('kernel',lambda:kernel.configure(owner)),('notes_config',lambda:kernel.notes.configure(owner.midi_options))]
                if activity is None:stages.append(('notes_seed',lambda:kernel.notes.seed_original_shadow(owner.midi_options.active_notes)))
                stages.append(('reset_contracts',lambda:kernel.prepare_published_reset_contracts(owner)))
                for stage,apply in stages:
                    if stage not in done:
                        report['republish_stage']=stage
                        if not apply():return False
                        done.add(stage)
                if reset_contracts_only:return publish_activity() # No assets changed or first-use plan retirement.
                if 'source_prepare' not in done:
                    old=kernel.first_use_plan;plan=old;report['republish_stage']='source_identity'
                    if old is not None:
                        if old.cancel_reason in ('explicit','retirement'):return False
                        if source_runtime is None:source_runtime=old._runtime
                        if source_runtime is not old._runtime or old.source_generation!=source_runtime.source_generation:return False
                    if source_runtime is not None:
                        report['republish_stage']='source_prepare'
                        requests=_native_first_use_requests(owner)
                        plan=_prepare_native_first_use_plan(source_runtime,requests,kernel.library,owner.stream_generation)
                        self.prepared_roots.append(plan)
                    checkpoint.update(old=old,plan=plan);done.add('source_prepare')
                old=checkpoint['old'];plan=checkpoint['plan']
                if plan is not None and 'source_attach' not in done:
                    report['republish_stage']='source_attach'
                    attach=kernel.library.midi_cc_kernel_firstuse
                    if fast:
                        current=self.source_refresh_at(report,operation=2)
                        if current is None:return False
                        rows=tuple((*row[:3],7 if ch<owner.addresses.count and plan.current else 0) for ch,row in enumerate(current))
                    else:rows=_native_first_use_rows(owner,plan,kernel.library,asynchronous=bool(self.mode&0x20000000))
                    values=(ctypes.c_uint*512)(*(value for row in rows for value in row))
                    with kernel.lock,plan.borrow() as pointer:
                        if not pointer or not attach(kernel.pointer,pointer,plan.epoch,plan.stream_generation,plan.stream,plan.mode,values,len(values)):
                            return False
                        kernel.first_use_plan=plan
                    done.add('source_attach')
                if old is not None and old is not plan and 'old_source_retire' not in done:
                    report['republish_stage']='old_source_retire'
                    if 'old_cache' not in done:
                        if old.current and old.cache_available and not old.replay_cache():return False
                        done.add('old_cache')
                    old.cancel(reason='source_refresh')
                    if not old.close():return False
                    with old._runtime._preparation_lock:
                        if old in old._runtime._native_first_use_plans:old._runtime._native_first_use_plans.remove(old)
                    if old in self.prepared_roots:self.prepared_roots.remove(old)
                    done.add('old_source_retire')
                if not publish_activity():return False
                if activity is None:self.sources={channel:owner.soundfont_presets.preset_key(channel) for channel in range(kernel.channels)}
                report['republish_stage']='complete'
                return True

    def queue_panic(self,*,on_complete=None):
        if on_complete is not None and not callable(on_complete):raise ValueError('Panic completion')
        steps=tuple((ch,event,0) for ch in range(self.kernel.channels)
                    for event in (MIDI_EVENT_SUSTAIN,MIDI_EVENT_SOSTENUTO,MIDI_EVENT_SOUNDOFF))
        with self._lifetime:
            if not self.pointer or self.submit_panic is None:return 0
            ticket=int(self.submit_panic(self.pointer))
            if ticket:self._controls[ticket]=(steps,on_complete)
            return ticket

    def queue_mixer(self,muted=None,channel=None,*,on_complete=None):
        """Local ABI1: channel SoundOff15/76/16 + NoteOn mask at FIFO fence."""
        marker=getattr(self.library,'midi_execution_mixer_version',None)
        if marker is None:return 0
        marker.argtypes=[];marker.restype=ctypes.c_uint
        if marker()!=1:return 0
        function=self.library.midi_execution_mixer
        function.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint];function.restype=ctypes.c_uint64
        mask=(ctypes.c_ubyte*128)(*(int(ch in muted) for ch in range(128))) if muted is not None else None
        with self._lifetime:
            if not self.pointer:return 0
            ticket=int(function(self.pointer,mask,0 if channel is None else channel))
            if ticket:self._controls[ticket]=((),on_complete)
            return ticket

    def queue_control(self,steps,*,continue_on_error=False,on_complete=None):
        """Candidate raw StreamEvent FIFO; no existing UI setter admission.

        UINT32 parameters are native values, not UI/source/floor conversions.
        Selection/reset/note identity edits, prepared-table publications and
        non-scalar DSP/rack controls remain gates. Completion callbacks must
        acknowledge metadata only, never resend audio or replace state writers.
       0 means unaccepted/full/closed: caller retains the original request.
        """
        steps=tuple(tuple(step) for step in steps)
        if not steps or len(steps)>1024 or any(len(step)!=3 or any(type(part) is not int or not 0<=part<=0xffffffff for part in step) for step in steps):
            raise ValueError('midi_execution_control scalar UINT32 steps')
        if type(continue_on_error) is not bool or (on_complete is not None and not callable(on_complete)):raise ValueError('midi_execution_control policy')
        values=(_NativeExecutionControlStep*len(steps))(*(_NativeExecutionControlStep(*step) for step in steps))
        with self._lifetime:
            if not self.pointer or self.submit_control is None:return 0
            ticket=int(self.submit_control(self.pointer,values,len(values),int(continue_on_error)))
            if ticket:self._controls[ticket]=(steps,on_complete)
            return ticket

    def queue_master(self,steps,present,*,on_complete=None):
        """Live event83/84 and Reset baseline share one native FIFO fence.

        Presence is the original nonzero UI value, not the rounded14-bit
        parameter. No source generation/asset/note ownership is replaced.
        An old library is explicitly unadmitted, never a plain-event fallback.
        """
        steps=tuple(tuple(row) for row in steps);present=tuple(present)
        if (not 1<=len(steps)<=2 or len(present)!=len(steps) or
                any(type(value) is not bool for value in present) or
                any(len(row)!=3 or any(type(value) is not int for value in row) or
                    row[0]!=0 or row[1] not in (83,84) or
                    not (0<=row[2]<=16383 if row[1]==83 else 40<=row[2]<=88) for row in steps) or
                len({row[1] for row in steps})!=len(steps) or
                (on_complete is not None and not callable(on_complete))):
            raise ValueError('master command bounds')
        version=getattr(self.library,'midi_execution_master_version',None)
        submit=getattr(self.library,'midi_execution_master',None)
        if version is None or submit is None:return 0
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:raise ValueError('master command ABI')
        submit.argtypes=[ctypes.c_void_p,ctypes.POINTER(_NativeExecutionControlStep),
                         ctypes.POINTER(ctypes.c_uint),ctypes.c_uint]
        submit.restype=ctypes.c_uint64
        values=(_NativeExecutionControlStep*len(steps))(*(_NativeExecutionControlStep(*row) for row in steps))
        presence=(ctypes.c_uint*len(present))(*(int(value) for value in present))
        with self._lifetime:
            if not self.pointer:return 0
            ticket=int(submit(self.pointer,values,presence,len(values)))
            if ticket:self._controls[ticket]=(steps,on_complete)
            return ticket

    def queue_instruments(self,requests,*,on_complete=None):
        """Original address selection in the same ingress FIFO as live notes.

        Each (channel, mask, bank, LSB, Program) uses mask bits 1/2/4. Missing
        components resolve from native execution state, not a GUI snapshot.
        No RAW selection, MIDI Learn rewrite or implicit synchronous fallback.
        """
        requests=tuple(tuple(row) for row in requests)
        if (not 1<=len(requests)<=128 or
                (on_complete is not None and not callable(on_complete)) or
                any(len(row)!=5 or any(type(value) is not int for value in row) or
                    not 0<=row[0]<self.owner.addresses.count or not 1<=row[1]<=7 or
                    not 0<=row[2]<=128 or not 0<=row[3]<128 or not 0<=row[4]<128 for row in requests) or
                len({row[0] for row in requests})!=len(requests)):
            raise ValueError('instrument command requests')
        version=getattr(self.library,'midi_execution_instrument_version',None)
        submit=getattr(self.library,'midi_execution_instruments',None)
        if version is None or submit is None:return 0
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:raise ValueError('instrument command ABI')
        submit.argtypes=[ctypes.c_void_p,ctypes.POINTER(_NativeExecutionInstrumentStep),ctypes.c_uint]
        submit.restype=ctypes.c_uint64
        values=(_NativeExecutionInstrumentStep*len(requests))(*(_NativeExecutionInstrumentStep(*row) for row in requests))
        with self._lifetime:
            if not self.pointer:return 0
            ticket=int(submit(self.pointer,values,len(values)))
            if ticket:self._controls[ticket]=(requests,on_complete)
            return ticket

    def queue_preset_tone(self,rows,*,on_complete=None):
        """Immutable six-tone policy edits on the existing native control FIFO.

        bassmidi.h MIXLEVEL and tone events retain their existing domains.
        This changes no font,selection generation,profile/extension layout or
        timed-event deadline. Completion contains metadata only.
        """
        class Row(ctypes.Structure):
            _fields_=[('bank',ctypes.c_uint),('program',ctypes.c_uint),('flags',ctypes.c_uint),('tone',ctypes.c_uint*6)]
        function=getattr(self.library,'midi_execution_preset_tone',None)
        version=getattr(self.library,'midi_execution_preset_tone_version',None)
        if function is None or version is None:return 0
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:raise ValueError('preset tone command ABI')
        rows=tuple(rows)
        if not rows or len(rows)>65536 or any(len(row)!=9 for row in rows):raise ValueError('preset tone rows')
        native=(Row*len(rows))(*(Row(*row[:3],(ctypes.c_uint*6)(*row[3:])) for row in rows))
        function.argtypes=[ctypes.c_void_p,ctypes.POINTER(Row),ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p];function.restype=ctypes.c_uint64
        with self._lifetime:
            if not self.pointer:return 0
            ticket=int(function(self.pointer,native,len(rows),self.owner.addresses.count,
                ctypes.cast(self.owner.performance.bass.BASS_ChannelLock,ctypes.c_void_p)))
            if ticket:self._controls[ticket]=(rows,on_complete)
            return ticket

    def queue_reset_policy(self,mask,default_reverb,gs_reset,delay_sysex,*,system_receivers=(True,True,True),on_complete=None):
        """Pure policy FIFO, without MIDI emission or paused publication."""
        if (type(mask) is not int or not 1<=mask<=63 or type(default_reverb) is not int or
                not 0<=default_reverb<=127 or type(gs_reset) is not bool or type(delay_sysex) is not bool or
                len(system_receivers)!=3 or any(type(v) is not bool for v in system_receivers) or
                (on_complete is not None and not callable(on_complete))):raise ValueError('reset policy bounds')
        extended=bool(mask&~7)
        version=getattr(self.library,'midi_execution_reset_policy2_version' if extended else 'midi_execution_reset_policy_version',None)
        submit=getattr(self.library,'midi_execution_reset_policy2' if extended else 'midi_execution_reset_policy',None)
        if version is None or submit is None:return 0
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:raise ValueError('reset policy ABI')
        submit.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint]
        if extended:submit.argtypes.append(ctypes.c_uint)
        submit.restype=ctypes.c_uint64
        with self._lifetime:
            if not self.pointer:return 0
            args=(self.pointer,mask,default_reverb,int(gs_reset),int(delay_sysex))
            if extended:args+=(sum(int(v)<<i for i,v in enumerate(system_receivers)),)
            ticket=int(submit(*args))
            if ticket:self._controls[ticket]=((mask,default_reverb,gs_reset,delay_sysex),on_complete)
            return ticket

    def queue_effect_macro(self,channel,event,parameter,reads,*,on_complete=None):
        """Prepared original FLUSH macro/read compound, no Qt or getter replay.

        StreamEvents receives one BASS_MIDI_EVENT with the original FLUSH flag.
        Native GetEvent snapshots are captured before this FIFO command ACK.
        The current DLL/getter roots remain retained by registration and worker.
        """
        reads=tuple(reads)
        if (type(channel) is not int or not 0<=channel<self.kernel.channels or event not in (MIDI_EVENT_REVERB_MACRO,MIDI_EVENT_CHORUS_MACRO)
            or type(parameter) is not int or not 0<=parameter<=0xffffffff or not 0<len(reads)<=32
            or any(type(value) is not int or not 0<=value<=0xffffffff for value in reads)
            or (on_complete is not None and not callable(on_complete))):raise ValueError('effect macro command')
        submit=getattr(self.library,'midi_execution_macro',None)
        if submit is None:return 0
        getter=self.owner.performance.bassmidi.BASS_MIDI_StreamGetEvent
        if not isinstance(getter,ctypes._CFuncPtr):raise ValueError('effect macro native getter')
        submit.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,ctypes.c_void_p]
        submit.restype=ctypes.c_uint64
        values=(ctypes.c_uint*len(reads))(*reads)
        with self._lifetime:
            if not self.pointer:return 0
            ticket=int(submit(self.pointer,channel,event,parameter,values,len(values),ctypes.cast(getter,ctypes.c_void_p)))
            if ticket:self._controls[ticket]=((channel,event,parameter,reads,getter),on_complete)
            return ticket

    def queue_effect_batch(self,channel,steps,*,on_complete=None):
        """One exact FLUSH transaction, not independent scalar submissions."""
        steps=tuple(tuple(step) for step in steps)
        if (type(channel) is not int or not 0<=channel<self.owner.addresses.count or len(steps)>1024 or
                any(len(step)!=3 or any(type(value) is not int for value in step) or
                    not 0<=step[0]<self.owner.addresses.count or
                    any(not 0<=value<=0xffffffff for value in step[1:]) for step in steps) or
                (on_complete is not None and not callable(on_complete))):raise ValueError('effect batch command')
        submit=getattr(self.library,'midi_execution_batch',None)
        if submit is None:return 0
        version=self.library.midi_execution_batch_version;version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:raise ValueError('effect batch ABI')
        submit.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(_NativeExecutionControlStep),ctypes.c_uint]
        submit.restype=ctypes.c_uint64
        values=(_NativeExecutionControlStep*len(steps))(*(_NativeExecutionControlStep(*step) for step in steps))
        with self._lifetime:
            if not self.pointer:return 0
            ticket=int(submit(self.pointer,channel,values,len(steps)))
            if ticket:self._controls[ticket]=(steps,on_complete)
            return ticket

    def queue_global_send_floor(self,event,value,*,on_complete=None):
        """Native floor writer and both-send apply_all at one ingress fence.

        bassmidi.h send23/24 are channel parameters, not engine Level36/41.
        Kernel retains this exact send-domain image through worker retirement.
        Rejection never publishes a floor or runs a synchronous fallback.
        """
        if (event not in (MIDI_EVENT_REVERB,MIDI_EVENT_CHORUS) or type(value) is not int or not 0<=value<=127 or
                (on_complete is not None and not callable(on_complete))):raise ValueError('global send floor command')
        submit=getattr(self.library,'midi_execution_send_floor',None)
        if submit is None:return 0
        version=self.library.midi_execution_send_floor_version;version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:raise ValueError('global send floor ABI')
        domain=self.owner.channel_sends.native_domain
        if not domain.pointer or domain.library._handle!=self.library._handle:return 0
        channels=tuple(self.owner.channel_sends.channels())
        if not channels or len(channels)>128 or len(set(channels))!=len(channels) or any(type(ch) is not int or not 0<=ch<self.kernel.channels for ch in channels):
            raise ValueError('global send floor channels')
        submit.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint]
        submit.restype=ctypes.c_uint64;values=(ctypes.c_uint*len(channels))(*channels)
        with self._lifetime:
            if not self.pointer:return 0
            ticket=int(submit(self.pointer,domain.pointer,event,value,values,len(values)))
            if ticket:self._controls[ticket]=((domain,event,value,channels),on_complete)
            return ticket

    def queue_system_mode(self,mode,*,restore_events=(),on_complete=None):
        """SYSTEMEX (including Default0) -> globals -> existing UI Reset, ABI3.

        Explicit one-shot command. Does not alter incoming SysEx policies.
        Native stream/metadata lifetime remains retained until completion.
        """
        if type(mode) is not int or mode not in (0,1,2,3,4) or (on_complete is not None and not callable(on_complete)):
            raise ValueError('system mode command')
        rows=tuple(tuple(row) for row in restore_events)
        if len(rows)>32 or any(len(row)!=3 or any(type(v) is not int for v in row) or row[0]!=0 or
                row[1] not in {9,*range(32,43)} or not 0<=row[2]<=0xffffffff for row in rows):raise ValueError('system mode restore events')
        values=(_NativeExecutionControlStep*len(rows))(*(_NativeExecutionControlStep(*row) for row in rows))
        submit=getattr(self.library,'midi_execution_system_mode',None)
        if submit is None:return 0
        version=self.library.midi_execution_system_mode_version;version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=3:raise ValueError('system mode execution ABI')
        submit.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(_NativeExecutionControlStep),ctypes.c_uint];submit.restype=ctypes.c_uint64
        with self._lifetime:
            if not self.pointer:return 0
            ticket=int(submit(self.pointer,mode,values,len(rows)))
            if ticket:self._controls[ticket]=((),on_complete)
            return ticket

    def queue_manual_reset(self,*,on_complete=None):
        """Distinct UI Reset plan; incoming GM and startup remain separate."""
        if on_complete is not None and not callable(on_complete):raise ValueError('manual reset completion')
        submit=getattr(self.library,'midi_execution_reset',None)
        if submit is None:return 0
        version=self.library.midi_execution_reset_version;version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:raise ValueError('manual reset execution ABI')
        submit.argtypes=[ctypes.c_void_p];submit.restype=ctypes.c_uint64
        with self._lifetime:
            if not self.pointer:return 0
            ticket=int(submit(self.pointer))
            if ticket:self._controls[ticket]=((),on_complete)
            return ticket

    def queue_file_reset(self,*,source='native_midi',on_complete=None):
        """Same command FIFO/fence; distinct original file Reset operator."""
        if source not in ('manual','native_midi') or (on_complete is not None and not callable(on_complete)):
            raise ValueError('file Reset command')
        submit=getattr(self.library,'midi_execution_file_reset',None)
        if submit is None:return 0
        version=self.library.midi_execution_file_reset_version;version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:raise ValueError('file Reset execution ABI')
        submit.argtypes=[ctypes.c_void_p,ctypes.c_uint];submit.restype=ctypes.c_uint64
        with self._lifetime:
            if not self.pointer:return 0
            ticket=int(submit(self.pointer,29 if source=='manual' else 31))
            if ticket:self._controls[ticket]=((),on_complete)
            return ticket

    def queue_stream_effect(self,plan,*,on_complete=None):
        """Original global vendor owner publication at the MIDI FIFO fence."""
        if on_complete is not None and not callable(on_complete):raise ValueError('stream effect completion')
        equalizer=getattr(plan.graph,'kind',None)=='equalizer'
        prefix='midi_execution_equalizer' if equalizer else 'midi_execution_stream_effect'
        submit=getattr(self.library,prefix,None)
        if submit is None:return 0
        version=getattr(self.library,prefix+'_version');version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:raise ValueError('stream effect execution ABI')
        if (not plan.pointer or plan.library._handle!=self.library._handle or plan.graph.owner is not self.owner or
            plan.graph.stream_generation!=self.owner.stream_generation or plan.graph.stream!=self.owner.stream_handle or plan.inflight):
            raise ValueError('stream effect prepared generation')
        submit.argtypes=[ctypes.c_void_p,ctypes.c_void_p];submit.restype=ctypes.c_uint64
        with self._lifetime,plan._lifetime:
            if not self.pointer or not plan.pointer:return 0
            plan.inflight=True
            try:ticket=int(submit(self.pointer,plan.pointer))
            except BaseException:plan.inflight=False;raise
            if ticket:self._controls[ticket]=(plan,on_complete)
            else:plan.inflight=False
            return ticket

    def retain_effect_graph(self,graph):
        """Retain preparation-only graphs before their first accepted command.

        Detached IR work still borrows the stream/device and native image.
        Factory retirement must join that work even if no plan reaches FIFO.
        """
        if (graph.library._handle!=self.library._handle or graph.owner is not self.owner or
                graph.stream_generation!=self.owner.stream_generation or graph.stream!=self.owner.stream_handle):
            raise ValueError('FX topology graph generation')
        with self._lifetime:
            if not self.pointer:return False
            previous=getattr(graph,'_execution_root',None)
            if previous is not None and previous is not self:raise ValueError('FX graph execution owner')
            if graph not in self.effect_graph_roots:self.effect_graph_roots.append(graph)
            graph._execution_root=self
            return True

    def release_effect_graph(self,graph):
        """Drop only fully closed roots; borrower retirement remains first."""
        with self._lifetime:
            if graph.pointer or any(plan.pointer or plan.inflight for plan in graph.plans):return False
            if getattr(graph,'_execution_root',None) not in (None,self):raise ValueError('FX graph release owner')
            if graph in self.effect_graph_roots:self.effect_graph_roots.remove(graph)
            graph._execution_root=None
            return True

    def queue_effect_topology(self,plan,*,on_complete=None):
        """Prepared vendor graph publication, original MIDI fence/FIFO lease.

        Snapshot replay mirrors copied native handles only; no BASS topology
        operation occurs on the metadata consumer or Qt callback thread.
        """
        if on_complete is not None and not callable(on_complete):raise ValueError('FX topology completion')
        submit=getattr(self.library,'midi_execution_fx_topology',None)
        if submit is None:return 0
        if (not plan.pointer or plan.library._handle!=self.library._handle or plan.graph.owner is not self.owner or
            plan.graph.stream_generation!=self.owner.stream_generation or plan.graph.stream!=self.owner.stream_handle or getattr(plan,'inflight',False)):
            raise ValueError('FX topology prepared generation')
        submit.argtypes=[ctypes.c_void_p,ctypes.c_void_p];submit.restype=ctypes.c_uint64
        with self._lifetime,plan._lifetime:
            if not self.pointer or not plan.pointer:return 0
            plan.inflight=True
            try:ticket=int(submit(self.pointer,plan.pointer))
            except BaseException:plan.inflight=False;raise
            if ticket:
                self._controls[ticket]=(plan,on_complete)
                if plan.graph not in self.effect_graph_roots:self.effect_graph_roots.append(plan.graph)
                plan.graph._execution_root=self
            else:plan.inflight=False
            return ticket

    def read(self):
        if not self._read_lock.acquire(blocking=False):raise ValueError('midi_execution single metadata consumer')
        try:
            with self.borrow() as pointer:
                report=self._read(pointer) if pointer else None
                if report is not None:self._capture_activity_receipt(report)
                return report
        finally:self._read_lock.release()

    @staticmethod
    def _activity_available_rows(keys):
        values=() if keys is None else tuple(tuple(row) for row in keys)
        if any(len(row)!=2 or any(type(value) is not int or not 0<=value<=65535 for value in row) for row in values):raise ValueError('native activity available identity')
        return values,(ctypes.c_uint*max(1,len(values)))(*(bank|(program<<16) for bank,program in values))

    def publish_activity_available(self,report,keys):
        """Source-edit publication at its delivered type11 owner, not a queue.

        Caller must already own the composition's source-edit scope. Native
        checks the actual paused ticket/disposition/attachment; an arbitrary
        pause or copied/stale report cannot change canonical availability.
        """
        if report is not self.pending or report.get('disposition')!=11 or not report.get('source_edit_command'):raise ValueError('native activity source-edit owner')
        version=getattr(self.library,'midi_execution_activity_source_version',None)
        publish=getattr(self.library,'midi_execution_activity_available_at',None)
        if version is None or publish is None:return False
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:return False
        publish.argtypes=[ctypes.c_void_p,ctypes.c_uint64,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,ctypes.c_uint];publish.restype=ctypes.c_int
        values,rows=self._activity_available_rows(keys)
        with self.borrow() as pointer:
            committed=bool(pointer and publish(pointer,report['ticket'],rows,len(values),int(keys is not None)))
        if committed:self._replay_activity_control('available',None if keys is None else values)
        return committed

    def _replay_activity_control(self,kind,keys):
        """Ordered old-shadow writer until native authority/GUI cutover.

        Updating only native history/Available would let a later legacy-state
        reconciliation resurrect obsolete values. Never copy an entire newer
        canonical snapshot over earlier FIFO Note metadata here.
        """
        sf=self.owner.soundfont_activity
        if kind=='history':sf.clear_played_history()
        elif keys is None:
            with sf.lock:sf.available_presets=None
        else:sf.set_available_presets(keys)

    def restore_activity_state(self,report,state,sources,*,expected_revision=None,preserve_current=False):
        """Full state reconciliation at an owned type2/type11 ingress fence.

        Never use bool(result): 1=committed,0=refused,-1=stale target. Native
        validates every state row before atomically replacing activity,
        original physical overlap counters and fallback source identities.
        No RAW, Note action or timing origin is fabricated. This is not yet
        the product's legacy codec or automatic authority transfer.
        """
        if report is not self.pending or report.get('disposition') not in (2,11):raise ValueError('native activity restore fence owner')
        activity=getattr(self.kernel.notes,'activity',None)
        if activity is None:return 0
        version=getattr(self.library,'midi_execution_activity_restore_version',None)
        restore=getattr(self.library,'midi_execution_activity_restore_at',None)
        if version is None or restore is None:return 0
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1 or not activity._transfer_api():return 0
        uint,wide,ptr=ctypes.c_uint,ctypes.c_uint64,ctypes.c_void_p
        pwide,puint=ctypes.POINTER(wide),ctypes.POINTER(uint)
        restore.argtypes=[ptr,wide,wide,pwide,uint,pwide,uint,pwide,uint,pwide,uint,pwide,uint,puint,uint,puint,uint]
        restore.restype=ctypes.c_int
        if preserve_current:
            if (state is not None or report.get('disposition')!=11 or not report.get('source_activity_preserved')):
                raise ValueError('native activity preservation owner')
            header,ch_rows,preset_rows,fifo_rows,count_rows,available_rows=activity._export_transfer_buffers()
            expected_revision=header[0]
        else:header,ch_rows,preset_rows,fifo_rows,count_rows,available_rows=activity._pack_transfer_state(state)
        source_rows=self.kernel.notes._activity_source_rows(sources)
        if expected_revision is not None and (type(expected_revision) is not int or not 0<=expected_revision<2**64):raise ValueError('native activity transfer target revision')
        with self.borrow() as pointer,activity.borrow() as activity_pointer:
            if not pointer or not activity_pointer:return 0
            if expected_revision is None:
                current=(wide*9)()
                if activity.library.native_activity_transfer_header(activity_pointer,current,9)!=9:raise RuntimeError('native activity transfer target header')
                expected_revision=int(current[0])
            result=int(restore(pointer,report['ticket'],expected_revision,(wide*9)(*header),9,ch_rows,activity.channels,
                preset_rows,header[2],fifo_rows,header[3],count_rows,activity.channels*256,available_rows,header[6],source_rows,self.kernel.notes.channels))
        if result==1:
            self.sources={channel:(int(source_rows[channel*3+1]),int(source_rows[channel*3+2])) if source_rows[channel*3] else None
                          for channel in range(self.kernel.notes.channels)}
        return result

    def queue_activity_control(self,kind,keys=None,*,on_complete=None):
        """Ordered native Played/Available writer; explicit completion owner.

        Source-edit code already paused at its own fence must NOT enqueue and
        wait on this worker. That path needs the distinct fenced publication.
        This method is an opt-in actor building block, not product admission.
        """
        if kind not in ('history','available') or (kind=='history' and keys is not None):raise ValueError('native activity control kind')
        if on_complete is not None and not callable(on_complete):raise ValueError('native activity completion callback')
        if getattr(self.kernel.notes,'activity',None) is None:return 0
        version=getattr(self.library,'midi_execution_activity_control_version',None)
        submit=getattr(self.library,'midi_execution_activity_control',None)
        if version is None or submit is None:return 0
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:return 0
        submit.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,ctypes.c_uint]
        submit.restype=ctypes.c_uint64
        values,rows=self._activity_available_rows(keys)
        with self._lifetime:
            if not self.pointer:return 0
            ticket=int(submit(self.pointer,1 if kind=='history' else 2,rows,len(values),int(keys is not None)))
            if ticket:self._controls[ticket]=((kind,values),on_complete)
            return ticket

    def queue_manual_controls(self,requests,*,continue_on_error=True,on_complete=None):
        """Explicit channel-control facade candidate, not auto-applied UI work.

        Native saved equality/pop, attempted timbre and accepted manual origin
        execute at the same ingress fence.0 means unaccepted/closed/full.
        Candidate typed sends use the send/tap contract; mode is still separate.
        """
        if type(continue_on_error) is not bool or (on_complete is not None and not callable(on_complete)):raise ValueError('manual command policy')
        owner=self.owner;kernel=self.kernel
        if not kernel.manual_ready or self.submit_manual is None:return 0
        signature=(owner.stream_handle,owner.stream_generation,owner.channel_policy.revision,owner.addresses.count)
        if signature!=kernel.manual_signature:return 0
        requests=tuple(requests)
        if not 0<len(requests)<=1024:raise ValueError('manual command resource bound')
        steps=[];indices={field:index for index,field in enumerate(kernel.semantic_fields)}
        with owner.channel_policy.lock:
            for channel,field,value in requests:
                if type(channel) is not int or not 0<=channel<owner.addresses.count or field not in indices:raise ValueError('manual command channel/field')
                low,high=ChannelControlPolicy.bounds(field);amount=max(low,min(high,int(round(value))))
                if field in ('portamento','sustain','sostenuto','soft'):amount=127 if amount>=64 else 0
                target=owner.channel_policy.destination(channel,field);mapped=owner.channel_policy.mapped(channel,field)
                if target=='mode' or (target in ('reverb','chorus','delay_send') and not hasattr(kernel.library,'midi_execution_manual_send_version')):raise ValueError('manual send/delay/mode command admission pending')
                converted=owner.channel_policy.convert(field,target,amount)
                if target is None:event,raw=0,0
                else:
                    event=MIDI_EVENT_USERFX if target=='delay_send' else CHANNEL_PARAMETERS[target].event
                    if target=='key_pressure':raw=(converted<<8)|owner.channel_policy.pressure_key(channel)
                    elif target in EXPRESSION_EVENTS:raw=MidiPerformanceControls.expression_parameter(target,converted)
                    else:raw=converted
                flags=int(mapped)|(int(target in EXPRESSION_EVENTS)<<1)|(int(target in CHANNEL_TIMBRE_FIELDS)<<2)
                steps.append((channel,indices[field],0xffffffff if target is None else indices[target],flags,amount,converted or 0,event,raw))
        values=(_NativeExecutionManualStep*len(steps))(*(_NativeExecutionManualStep(*step) for step in steps))
        with self._lifetime:
            if not self.pointer:return 0
            ticket=int(self.submit_manual(self.pointer,values,len(values),int(continue_on_error)))
            if ticket:self._controls[ticket]=(tuple(steps),on_complete)
            return ticket

    def queue_manual_modes(self,requests,*,continue_on_error=True,on_complete=None):
        """Candidate original Mono/Poly/Legato toggle command, not CC mapping.

        Execution-time current/saved state, raw126/127 clearing and mode2's
        saved-before-BOOL failure remain native compound contracts. The same
        publication fence, retained generation and completion owner are used.
        """
        if type(continue_on_error) is not bool or (on_complete is not None and not callable(on_complete)):
            raise ValueError('manual mode command policy')
        owner=self.owner;kernel=self.kernel
        if not kernel.manual_ready or self.submit_manual is None or not hasattr(kernel.library,'midi_execution_manual_mode_version'):return 0
        if (owner.stream_handle,owner.stream_generation,owner.channel_policy.revision,owner.addresses.count)!=kernel.manual_signature:return 0
        requests=tuple(requests)
        if not 0<len(requests)<=1024:raise ValueError('manual mode command resource bound')
        steps=[]
        for channel,target,enabled in requests:
            if type(channel) is not int or not 0<=channel<owner.addresses.count or target not in CHANNEL_MODE_SWITCHES or type(enabled) is not bool:
                raise ValueError('manual mode command request')
            value=int(enabled);kind=CHANNEL_MODE_SWITCHES.index(target)+1
            steps.append((channel,0,0xffffffff,kind<<3,value,value,MIDI_EVENT_MODE,value))
        values=(_NativeExecutionManualStep*len(steps))(*(_NativeExecutionManualStep(*step) for step in steps))
        with self._lifetime:
            if not self.pointer:return 0
            ticket=int(self.submit_manual(self.pointer,values,len(values),int(continue_on_error)))
            if ticket:self._controls[ticket]=(tuple(steps),on_complete)
            return ticket

    def _read(self,pointer):
        if self.pending is not None:raise RuntimeError(tr('runtime.midi_filter_config_failed',error='MIDI metadata replay pending'))
        if self._batch_count or self.metadata_batch_enabled:
            if not self._batch_count:
                count=self.library.midi_execution_read_notes_batch(pointer,self._batch_descriptors,64,35,self._batch_notes,len(self._batch_notes))
                if count==0:return None
                if count>0:
                    # Retain the copied native owner BEFORE Python conversion.
                    # A decoder exception leaves the same ordinal retryable.
                    self._batch_count=count;self._batch_next=0;self._batch_completed=0
                elif count!=-2:raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_execution_read_notes_batch '+str(count)))
            if self._batch_count:
                ordinal=self._batch_next
                if not 0<=ordinal<self._batch_count:raise ValueError('metadata batch replay/ACK pending')
                row=tuple(self._batch_descriptors[ordinal*35:(ordinal+1)*35])
                offset,actions=row[34],row[24]
                # row4 is original wire_size, not copied payload storage size.
                if row[1]!=1 or offset+actions>len(self._batch_notes):raise ValueError('metadata batch bounds')
                notes=tuple((entry.packed,entry.channel,entry.count,entry.qpc)
                    for entry in (self._batch_notes[index] for index in range(offset,offset+actions)))
                report=self._decode_record(row,note_actions=notes)
                report['_metadata_batch_ordinal']=ordinal
                self._batch_next+=1
                return report
        accepted=self.library.midi_execution_read(pointer,self.descriptor,len(self.descriptor),self.rows,len(self.rows),self.payload,len(self.payload))
        if accepted==0:return None
        if accepted!=1:raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_execution_read '+str(accepted)))
        row=tuple(self.descriptor)
        return self._decode_record(row)

    def _decode_record(self,row,*,note_actions=None):
        """One decoder for old single records and owned batch completions."""
        if row[4]>len(self.payload) or row[24]>len(self.rows):
            raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_execution descriptor bounds'))
        if row[1] in (12,13):
            independent=hasattr(self.library,'midi_execution_trace_observation_version')
            preview=int(row[24]) if independent else int(row[4])
            if preview>row[4] or preview>len(self.payload) or (independent and preview>256):
                raise ValueError('native trace preview bounds')
            report=dict(ticket=row[0],disposition=row[1],native_trace=True,port=row[2],input_port=row[2],frame_kind=row[3],
                payload=_native_owned_bytes(self.payload,preview),wire_size=row[4],read_qpc=row[5],source_origin=row[6:14],
                received_qpc=row[14],driver_pid=row[16],driver_epoch=row[17],driver_sequence=row[18],
                origin_valid=bool(row[19]),completed_qpc=row[31],consumed=row[27],error=ctypes.c_int(row[26]).value)
            if self.multi:report.update(client=row[32],publication=row[33])
            self.pending=report;return report
        if row[1]==14 and row[13]==2048:
            if row[4] or row[24]:raise ValueError('native trace fence descriptor')
            report=dict(ticket=row[0],disposition=14,native_trace_fence=True,control_ticket=row[6],phase=row[10],
                captured_frames=row[9],dropped_frames=row[11])
            self.pending=report;return report
        if row[13]==4096 and row[1] in (5,10,15):
            if row[4] or row[24]:raise ValueError('idle reset receipt bounds')
            report=dict(ticket=row[0],disposition=row[1],auto_reset=True,successful=bool(row[9]),
                phase=row[10],error=ctypes.c_int(row[11]).value,unlock_error=ctypes.c_int(row[12]).value,completed_qpc=row[31])
            return self._retain_activity_report(report)
        # In input completions row13 is the original MID3 reserved/flags
        # word. Unknown flags make timing quality invalid, not a control.
        # Only actual control dispositions own this internal flag namespace.
        if row[1] in (4,11) and row[13]&1024:
            if row[4] or row[24]:raise ValueError('source-edit barrier descriptor')
            report=dict(ticket=row[0],disposition=row[1],control_ticket=row[6],fence=row[7],
                submitted_qpc=row[8],phase=row[10],source_edit_command=True)
            self.pending=report;return report
        # ctypes.string_at(pointer, exact size) returns an owned bytes copy.
        # A ctypes array slice first allocates a Python integer list (~8MiB
        # for a1MiB frame) while holding GIL; do not build that intermediate.
        # Never publish a borrowed view: the next native read reuses storage.
        if row[1] in (4,5) or (row[1]==10 and row[13] in (512,65536,131072,262144,2097152,4194304)):
            raw=_native_owned_bytes(self.payload,row[4])
            topology=bool(row[13]&4);stream_effect=bool(row[13]&64)
            if stream_effect:
                if len(raw)!=(292 if row[13]&256 else 52 if row[13]&128 else 36):raise ValueError('stream effect snapshot bounds')
                entries=(_NativeExecutionControlResult*1).from_buffer_copy(raw[:20])
                if row[13]&256:
                    identity=int(ctypes.c_uint64.from_buffer_copy(raw[20:28]).value)
                    count,trim=tuple((ctypes.c_uint*2).from_buffer_copy(raw[28:36]))
                    bands=tuple(tuple(values) for values in ((ctypes.c_uint*2)*32).from_buffer_copy(raw[36:]))
                    equalizer_snapshot=(identity,count,trim,bands)
                else:stream_effect_snapshot=tuple((ctypes.c_uint*4).from_buffer_copy(raw[20:36]))
                if row[13]&128:
                    stream_effect_pointer=int(ctypes.c_uint64.from_buffer_copy(raw[36:44]).value)
                    stream_effect_sync=int(ctypes.c_uint.from_buffer_copy(raw[44:48]).value)
            elif topology:
                total=row[14]+row[15]
                snapshot_type=_NativeFXTopologySnapshot2 if row[13]&8 else _NativeFXTopologySnapshot
                if total>64 or len(raw)!=20+ctypes.sizeof(snapshot_type)*total:raise ValueError('FX topology snapshot bounds')
                entries=(_NativeExecutionControlResult*1).from_buffer_copy(raw[:20])
                snapshots=(snapshot_type*total).from_buffer_copy(raw[20:])
            elif row[13]&32:
                if len(raw)%ctypes.sizeof(_NativeSendResult):raise ValueError('global send floor result bounds')
                entries=(_NativeSendResult*(len(raw)//ctypes.sizeof(_NativeSendResult))).from_buffer_copy(raw)
            else:
                assert len(raw)%ctypes.sizeof(_NativeExecutionControlResult)==0
                entries=(_NativeExecutionControlResult*(len(raw)//20)).from_buffer_copy(raw)
            report=dict(ticket=row[0],disposition=row[1],control_ticket=row[6],fence=row[7],submitted_qpc=row[8],
                successful=row[9],phase=row[10],error=ctypes.c_int(row[11]).value,unlock_error=ctypes.c_int(row[12]).value,
                manual_command=bool(row[13]&(1|512|65536|131072|262144|524288|1048576|2097152|4194304|8388608)),system_mode_reset=bool(row[13]&4194304),instrument_command=bool(row[13]&2097152),manual_reset=bool(row[13]&512),file_reset=bool(row[13]&(65536|131072|262144)),macro_command=bool(row[13]&2),skipped_audio=tuple(entry.accepted==2 for entry in entries),
                note_actions=tuple((entry.packed,entry.channel,entry.count,entry.qpc) for entry in self.rows[:row[24]]),
                results=tuple((entry.channel,entry.event,entry.raw if row[13]&32 else entry.parameter,bool(entry.accepted),entry.error) for entry in entries),
                completed_qpc=row[31])
            if row[13]&8388608:report['topology_changed']=bool(row[23])
            if row[13]&8192:
                if len(entries)!=1 or entries[0].channel!=0xffffffff or entries[0].event not in (1,2):raise ValueError('native activity control descriptor')
                report['activity_control']='history' if entries[0].event==1 else 'available'
            if row[13] in (32768,65536):report['file_loop_boundary']='panic' if row[13]==32768 else 'reset'
            if row[13]==262144:report['file_seek_boundary']='reset'
            if row[13]&32:
                report.update(send_floor_command=True,floor_event=row[14],floor_value=row[15],floor_published=bool(row[16]),
                    send_rows=tuple((entry.channel,entry.event,entry.request,entry.raw,bool(entry.accepted),entry.error) for entry in entries))
            if stream_effect:
                report.update(stream_effect_command=True)
                operations={1:'BASS_ChannelLock',2:'BASS_ChannelSetFX',3:'BASS_FXSetParameters',4:'BASS_ChannelRemoveFX',
                    5:'BASS_ChannelSetDSP',6:'BASS_ChannelRemoveDSP',7:'BASS_ChannelSetSync',8:'BASS_ChannelRemoveSync',
                    9:'stereo_coefficient',10:'limiter_parameters',11:'spatial_rate_parameters'}
                if row[23]:
                    if row[23] not in operations:raise ValueError('stream effect operation snapshot')
                    report['operation']=operations[row[23]]
                elif report['unlock_error']:report['operation']='BASS_ChannelLock'
                if row[13]&256:report.update(equalizer_command=True,equalizer_snapshot=equalizer_snapshot)
                else:report['stream_effect_snapshot']=stream_effect_snapshot
            if stream_effect and row[13]&128:report.update(stream_effect_pointer=stream_effect_pointer,stream_effect_sync=stream_effect_sync)
            if topology:
                values=tuple((entry.exact,entry.handle,entry.kind,entry.priority,entry.reserved)+((entry.pointer,entry.kernel) if row[13]&8 else ()) for entry in snapshots)
                report.update(topology_command=True,topology_current=values[:row[14]],topology_retired=values[row[14]:],
                    topology_faulted=bool(row[16]),topology_changed=bool(row[17]),topology_masks=row[18:23])
            with self._lifetime:
                if row[6] not in self._controls and not (report.get('file_loop_boundary') or report.get('file_seek_boundary')):raise ValueError('midi_execution control completion owner')
                self.pending=report
            return self._retain_activity_report(report)
        source=row[6:14];legacy=row[3]!=3
        origin=dict(quality='legacy' if legacy else 'driver' if row[19] else 'invalid',ingress_qpc=row[14] if row[19] else None,
            read_qpc=row[5],frequency=source[1],pid=source[6],epoch=source[2],sequence=source[3],
            transport_faults=int(row[21])+int(row[22]),transport_loss_total=source[5],sequence_faults=row[22],native_read=True)
        report=dict(ticket=row[0],disposition=row[1],status=ctypes.c_int64(row[2]).value,
            payload=_native_owned_bytes(self.payload,row[4]) if row[1] in (2,3) else None,wire_size=row[4],frame_kind=row[3],
            trace=ctypes.c_int64(source[4]).value,read_qpc=row[5],origin=origin,received=row[14],
            origin_health=bool(row[23]),actions=(),note_actions=note_actions if note_actions is not None else tuple((entry.packed,entry.channel,entry.count,entry.qpc)
                for entry in _native_items(self.rows,row[24])),
            raw_calls=row[25],error=ctypes.c_int(row[26]).value,consumed=row[27],admitted=row[28],send_failures=row[29],send_calls=row[30],
            unlocked=row[1]!=3,completed_qpc=row[31],input_port=self.port)
        if row[1]==9 and report['status']==-9:report['explicit_cancelled']=True
        if self.multi:report.update(client=row[32],publication=row[33])
        if self.multi and row[3]==3 and source[7]&~7==0x100:
            # Native WinMM callback owns the admitted copy,not a Python RAW
            # submission callback or the file transport. Port survives delayed
            # metadata and legacy handoff without a mutable callback TLS.
            port=source[7]&7
            report.update(external_input=True,input_port=port,direct_port=port,direct_origin=source)
            report['origin']['quality']='callback'
        elif self.multi and row[32]==2**64-1:
            with self._lifetime:
                registration=self._raw_frames.get(row[33])
                if registration is None:
                    lookup=getattr(self.library,'midi_execution_file_receipt',None)
                    if lookup is not None:
                        lookup.argtypes=[ctypes.c_void_p,ctypes.c_uint64,ctypes.POINTER(ctypes.c_uint64),ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint]
                        lookup.restype=ctypes.c_int;fields=(ctypes.c_uint64*4)();wire=self.payload
                        if lookup(self.pointer,row[33],fields,wire,len(wire),0)==1:
                            registration=dict(port=int(fields[0]),wire=bytes(wire[:fields[1]]),stamp=int(fields[2]),
                                trace=0,origin=None,on_complete=None,native_file=True)
                            self._raw_frames[row[33]]=registration
                    if registration is None:raise ValueError('RAW actor publication owner')
                report['direct_port']=registration['port'];report['direct_origin']=registration['origin'];report['input_port']=registration['port']
        elif self.multi:
            with self._lifetime:
                if row[32] not in self._client_ports:raise ValueError('midi_execution input client owner')
                report['input_port']=(source[7]&7) if row[3]==3 and source[7]&~7==0x80000000 else self._client_ports[row[32]]
        if report['disposition']==2:
            if not 0<=report['consumed']<=len(report['payload']):raise ValueError('midi_execution invalid handoff suffix')
            report['remaining_payload']=report['payload'][report['consumed']:]
        return self._retain_activity_report(report)

    def activity_receipt(self,ticket):
        """Side metadata, never flags in MID3 origin or an old descriptor field."""
        if not getattr(self,'activity_receipt_ready',False):return None
        if type(ticket) is not int or not 0<ticket<2**64:raise ValueError('native activity receipt ticket')
        with self.borrow() as pointer:
            if not pointer:raise ValueError('native activity receipt owner closed')
            result=int(self.library.midi_execution_activity_receipt(pointer,ticket))
        if result not in (0,1):raise RuntimeError('native activity receipt ticket unavailable')
        return bool(result)

    def _capture_activity_receipt(self,report):
        if report.get('_activity_receipt_pending'):
            report['activity_already_applied']=self.activity_receipt(report['ticket'])
            report['_activity_receipt_pending']=False

    def _retain_activity_report(self,report):
        # Retain the copied report BEFORE an optional query can fail. A retry
        # resolves this same ticket, not a second native read/audio submission.
        self.pending=report
        if getattr(self,'activity_receipt_enabled',False) and report['disposition'] in (1,4,6,7,8,9,15):
            report['_activity_receipt_pending']=True
            # read() queries only after _read has installed batch ordinal and
            # next-index ownership. A query fault must not replay this ordinal
            # twice or strand the native batch's final ACK.
        return report

    def _complete_metadata_batch_record(self,report):
        ordinal=report.get('_metadata_batch_ordinal')
        if ordinal is None:return True
        if not report.get('_metadata_batch_counted'):
            if ordinal!=self._batch_completed:raise ValueError('metadata batch completion order')
            self._batch_completed+=1;report['_metadata_batch_counted']=True
        # A direct-RAW completion callback can retry after successful batch ACK.
        if not self._batch_count:return True
        if self._batch_completed==self._batch_count:
            last=self._batch_descriptors[(self._batch_count-1)*35]
            with self.borrow() as pointer:
                if not pointer or not self.library.midi_execution_metadata_batch_ack(pointer,last,self._batch_count):return False
            self._batch_count=0;self._batch_next=0;self._batch_completed=0
        return True

    def metadata_batch_statistics(self):
        if not self.metadata_batch_ready:return {}
        values=(ctypes.c_uint64*4)()
        with self.borrow() as pointer:
            if not pointer or self.library.midi_execution_metadata_batch_snapshot(pointer,values,len(values))!=4:return {}
        return dict(zip(('reads','records','acks','unacked_records'),values))

    def replay(self,report):
        if report is not self.pending:raise ValueError('midi_execution replay owner/order')
        self._capture_activity_receipt(report)  # Shadow still replays the old state for differential proof.
        if report.get('auto_reset') and report['disposition']==15:
            if not self.owner.drain_native_controls():return False
            if report['successful'] and not report.get('metadata_replayed'):
                self.owner.auto_reset_clock.native_completed()
            report['metadata_replayed']=True;self.pending=None
            if not report['successful']:self.terminal_fault=dict(report)
            return True
        if report['disposition']==10:
            if not self.owner.drain_native_controls():return False
            report['metadata_replayed']=True;return True
        if report['disposition']==9:
            # Preserve the completed prefix and captured terminal evidence.
            # No legacy handoff and no audio retry may follow this frame.
            if report.get('note_actions'):
                copied=report.setdefault('terminal_note_replay',dict(actions=report['note_actions'],
                    activity_already_applied=report.get('activity_already_applied',False)))
                self.kernel.notes.replay(self.owner,copied,self.sources)
            if not self.owner.drain_native_controls():return False
            self.terminal_fault=dict(report);self.pending=None;return True
        if report['disposition']==4:
            if report.get('note_actions'):
                cleared=report.setdefault('control_actions_replay',dict(actions=report['note_actions'],
                    activity_already_applied=report.get('activity_already_applied',False)))
                self.kernel.notes.replay(self.owner,cleared,self.sources)
            if report.get('manual_command') and not self.owner.drain_native_controls():return False
            if report.get('file_loop_boundary') or report.get('file_seek_boundary'):
                if not self.owner.drain_native_controls():return False
                if report['phase'] in (1,5,6) or ((report.get('file_loop_boundary')=='reset' or report.get('file_seek_boundary')=='reset') and report['phase']!=0):
                    self.terminal_fault=dict(report)
                report['metadata_replayed']=True;self.pending=None;return True
            if report.get('send_floor_command'):
                if not report.get('send_floor_drained'):
                    if not self.owner.drain_native_controls():return False
                    report['send_floor_drained']=True
                sends=self.owner.channel_sends
                if report['floor_published']:
                    if report['floor_event']==MIDI_EVENT_REVERB:sends.global_reverb=report['floor_value']
                    else:sends.global_chorus=report['floor_value']
                # Requests are not new input writers. Accepted provenance is
                # captured by the native manual journal at the real event
                # position and drained above in common-clock order. Do not
                # replay an old request or invoke on_emit after a newer CC.
            with self._lifetime:registration=self._controls[report['control_ticket']]
            if report.get('activity_control') and not report.get('activity_control_replayed'):
                if report['successful']:
                    kind,keys=registration[0]
                    known=bool(report['results'][0][2])
                    self._replay_activity_control(kind,keys if known else None)
                report['activity_control_replayed']=True
            if report.get('topology_command') and not report.get('topology_replayed'):
                registration[0].graph.replay(registration[0],report);report['topology_replayed']=True
            if report.get('stream_effect_command') and not report.get('stream_effect_replayed'):
                registration[0].graph.replay(registration[0],report);report['stream_effect_replayed']=True
            if registration[1] is not None and not report.get('control_replayed'):
                registration[1](report);report['control_replayed']=True
            if report.get('topology_command') or report.get('stream_effect_command'):
                registration[0].inflight=False
                assert registration[0].close() # copied/native command root survives until its C++ completion destructor
            with self._lifetime:self._controls.pop(report['control_ticket']);self.pending=None
            return True
        if report['disposition'] not in (1,6,7,8):return False
        if not report.get('metadata_replayed'):
            if not self.kernel.replay_mixed(self.owner,report,self.sources):return False
        # Coverage is observational; the native worker already applied the
        # timing registrations/health watch at ingress, not this late replay.
        if not report.get('metadata_replayed') and self.timing is not None and report['disposition']==1:
            self.timing.coverage.observe(report['origin'],report['completed_qpc'],report['origin_health'])
        report['metadata_replayed']=True
        if report['disposition'] in (6,8):return True # Explicit ACK before native/legacy suffix.
        if not self._complete_metadata_batch_record(report):return False
        self.pending=None;return True

    def resume(self,report,*,legacy_completed=False,retry_unlock=False,take_handoff=False):
        if report is not self.pending:raise ValueError('midi_execution resume owner/order')
        if report['disposition']==11:
            if retry_unlock or legacy_completed or take_handoff:raise ValueError('source-edit barrier resume policy')
            action=2
        elif report['disposition']==10:
            if retry_unlock or legacy_completed or take_handoff or not report.get('metadata_replayed'):
                raise ValueError('manual reset pressure requires metadata ACK')
            action=2
        elif report['disposition'] in (3,5):
            if not retry_unlock or legacy_completed or take_handoff:raise ValueError('midi_execution pending unlock requires native owner retry')
            if self.statistics().get('handoff_ticket')!=report['ticket']:
                self.pending=None;return True # Earlier notification already serviced out-of-band; no audio retry.
            action=2
        elif report['disposition'] in (6,8):
            if retry_unlock or legacy_completed or not report.get('metadata_replayed'):
                raise ValueError('midi_execution semantic pressure requires prefix metadata ACK')
            action=3 if take_handoff else 2
        elif report['disposition']==2:
            if retry_unlock or legacy_completed==take_handoff:raise ValueError('midi_execution handoff disposition')
            action=3 if take_handoff else 1
            if legacy_completed:
                # All preceding Note records have replayed before this FIFO
                # handoff. Reuse the staged generation checkpoint: a second
                # configure must not replace semantic/reset state after the
                # factory already prepared the current full GM contract.
                if not self.republish_generation():
                    raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_execution handoff prepare'))
        else:raise ValueError('midi_execution no handoff')
        with self.borrow() as pointer:
            if not pointer or not self.library.midi_execution_resume(pointer,report['ticket'],action):raise RuntimeError(tr('runtime.midi_filter_config_failed',error='midi_execution_resume'))
        self.pending=None;return True

    def retry_pending_unlock(self):
        """Control-only notification; original Note FIFO order is unchanged."""
        status=self.statistics()
        if status.get('handoff_kind') not in (3,5) or not status.get('handoff_ticket'):return False
        with self.borrow() as pointer:return bool(pointer and self.library.midi_execution_resume(pointer,status['handoff_ticket'],2))

    def statistics(self):
        values=(ctypes.c_uint64*26)()
        with self.borrow() as pointer:
            if not pointer or self.library.midi_execution_snapshot(pointer,values,26)!=26:return {}
        result=dict(zip(self.fields,values));result['terminal']=ctypes.c_int64(result['terminal_bits']).value;return result

    def request_stop(self):
        with self.borrow() as pointer:
            if pointer:self.library.midi_execution_stop(pointer)

    def close(self):
        with self._lifetime:
            schedule=getattr(self,'_file_schedule',None)
            if schedule is not None:
                _position,_index,state,pending=schedule._status()
                if state==1 or pending:return False
                schedule.close()
            if self.pending is not None or self._calls or self._batch_count:return False
            if self.pointer:
                # release requests stop so pending controls receive explicit
                # cancellation records. Unread completions keep native owners.
                self.library.midi_execution_stop(self.pointer)
                state=(ctypes.c_uint64*26)()
                if self._controls or getattr(self,'_raw_frames',{}) or (self.library.midi_execution_snapshot(self.pointer,state,26)==26 and (state[22] or state[24])):
                    return False # Do not spend the2s join timeout on a known required FIFO drain.
                if not self.library.midi_execution_release(self.pointer):return False
                if self._controls:raise ValueError('midi_execution unread control owner')
                self.pointer=None
                self._clients.clear()
                self._client_ports.clear()
        if self.own_origins:self.origins.close()
        return True


def native_source_selection_adapters(owner,*,kernel=None):
    """Current, retired and kernel-borrowed source publication roots."""
    if kernel is None:kernel=getattr(owner,'cc_native_kernel',None)
    mapper=getattr(getattr(owner,'channel_native_filter',None),'function_map',None)
    adapters=set(getattr(mapper,'selection_adapters',{}).values())
    adapter=getattr(kernel,'selection_adapter',None)
    if adapter is not None:adapters.add(adapter)
    return adapters


def native_source_transactions(owner,*,kernel=None):
    """Retained thread-affine locks, including the original no-worker path."""
    roots=(getattr(owner,'channel_native_filter',None),getattr(getattr(owner,'channel_sends',None),'native_domain',None))
    transactions={getattr(root,'transaction',None) for root in roots}
    transactions.update(getattr(adapter,name,None) for adapter in native_source_selection_adapters(owner,kernel=kernel)
        for name in ('checkpoint_transaction','snapshot_transaction'))
    transactions.discard(None)
    return transactions


def native_source_unlock_owners(owner,*,kernel=None):
    """Unknown native ABI never certifies that a source worker may exit."""
    if kernel is None:kernel=getattr(owner,'cc_native_kernel',None)
    # BASS_ChannelLock (bass.h/bass.md) retains the original calling thread.
    result=[int(transaction.pending[1]) for transaction in native_source_transactions(owner,kernel=kernel) if transaction.pending is not None]
    for adapter in native_source_selection_adapters(owner,kernel=kernel):
        query=getattr(adapter.library,'midi_selection_state_unlock_owner',None)
        if query is None:raise RuntimeError('source-edit unlock inspection ABI unavailable')
        query.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint];query.restype=ctypes.c_int
        values=(ctypes.c_uint*2)()
        if not adapter.pointer or query(adapter.pointer,values,2)!=1:raise RuntimeError('source-edit unlock inspection')
        if values[0]:result.append(int(values[1]))
    if getattr(kernel,'pending',None) is not None:result.append(-1)
    return tuple(result)


class NativeMidiClientFactory:
    """Explicit common metadata owner; original client pipes remain separate.

    Product admission is caller-owned. Unknown suffix audio runs once while
    the native common kernel is paused; failed replay/retirement keeps every
    borrowed pipe/kernel/stream owner alive for inspection and retry.
    """
    def __init__(self,execution,on_midi,republish=None,*,on_error=None,source_runtime=None,full_selection=False):
        if not execution.multi or not callable(on_midi) or (republish is not None and not callable(republish)):raise ValueError('native MIDI client factory admission')
        self.execution=execution;self.on_midi=on_midi
        self.republish=republish or (lambda *_:execution.republish_generation(source_runtime=source_runtime,full_selection=full_selection))
        self.on_error=on_error
        self._lock=threading.RLock();self._changed=threading.Condition(self._lock);self._thread=None
        self.error=None;self._done=threading.Event()
        self._retire_roots=None
        self._retirement_lock=threading.Lock()
        self._source_edit_lock=threading.Lock()
        self._source_edit_requests={}
        self._source_edit_retained=None
        self._trace_owner=None;self._trace_tail=None
        if getattr(execution,'metadata_factory',None) is not None:raise ValueError('MIDI metadata factory already owned')
        execution.metadata_factory=self

    def start_native_trace(self,trace):
        with self._changed:
            if self._trace_owner is not None:raise RuntimeError('native trace owner retained')
            self._trace_owner=trace
            try:self.execution.set_trace_enabled(True)
            except BaseException:self._trace_owner=None;raise

    def stop_native_trace(self,*,timeout=5.0):
        if not 0<=timeout<=60:raise ValueError('native trace tail timeout')
        if threading.current_thread() is self._thread:raise ValueError('native trace metadata consumer owner')
        with self._changed:
            if self._trace_owner is None:return True
            if self._trace_tail is None:
                function=getattr(self.execution.library,'midi_execution_trace_finish',None)
                if function is None:raise RuntimeError('native trace tail ABI unavailable')
                function.argtypes=[ctypes.c_void_p];function.restype=ctypes.c_uint64
                with self.execution.borrow() as pointer:ticket=int(function(pointer)) if pointer else 0
                if not ticket:raise RuntimeError('native trace tail FIFO refused')
                self._trace_tail=dict(ticket=ticket,done=False)
            deadline=time.monotonic()+timeout
            while not self._trace_tail['done']:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise RuntimeError('native trace tail retained')
                self._changed.wait(min(.01,remaining))
            self._trace_tail=None;self._trace_owner=None;return True

    def _source_edit_quiet(self,*,reset_contracts_only=False):
        """Drain real timed callbacks while output continues; never FLUSH."""
        owner=self.execution.owner
        if owner._draining_native_controls or owner.state_writers:return False
        if not owner.drain_native_controls():return False
        # Even a settings-only fence hydrates the selection image. Its native
        # snapshot rejects outstanding selection reservations. The previous
        # shortcut skipped this prerequisite and stranded the paused actor.
        for adapter in self._source_edit_selection_adapters():
            if not adapter.settle_failed_tickets():return False
            state=adapter.statistics()
            if not state or state['reserved'] or state['queued'] or state['produced']!=state['consumed']:return False
        if reset_contracts_only:
            return not(owner._draining_native_controls or owner.state_writers or owner.midi_clock.revision()[1])
        # bassmidi.h: actual pending event counts, not QUEUE capacity. A
        # selection-free async/timed note still borrows its old source mapping.
        backend=owner.performance.bass
        for attribute in (0x12010,0x1200f,0x1200e):
            value=ctypes.c_float()
            if not backend.BASS_ChannelGetAttribute(owner.stream_handle,attribute,ctypes.byref(value)):
                raise RuntimeError('source-edit queue query failed: '+str(backend.BASS_ErrorGetCode()))
            if not math.isfinite(value.value) or value.value<0:raise RuntimeError('source-edit invalid queue count')
            if value.value:return False
        return not(owner._draining_native_controls or owner.state_writers or owner.midi_clock.revision()[1])

    @contextmanager
    def source_edit_scope(self,source_runtime,*,timeout=5.0,reset_contracts_only=False,preserve_activity=False):
        """Borrow a paused FIFO generation for the short source commit only.

        Detached asset preparation belongs outside this scope. Timeout before
        mutation releases the admitted fence later without source changes;
        publication failure retains the pause and every native root for retry.
        """
        if not 0<timeout<=60:raise ValueError('source-edit timeout')
        if threading.current_thread() is self._thread:raise ValueError('source-edit metadata consumer owner')
        if not self._source_edit_lock.acquire(blocking=False):raise RuntimeError('source-edit lease already owned')
        request=None
        try:
            with self._retirement_lock:
                if self._source_edit_retained is not None:raise RuntimeError('source-edit retained publication requires retry')
                execution=self.execution;library=execution.library
                source_stream=execution.owner.stream_handle if reset_contracts_only else source_runtime.stream_handle()
                if (self.error is not None or self._done.is_set() or source_stream!=execution.owner.stream_handle):
                    raise RuntimeError('source-edit generation admission')
                submit=getattr(library,'midi_execution_source_edit',None)
                version=getattr(library,'midi_execution_source_edit_version',None)
                if submit is None or version is None:raise RuntimeError('source-edit ABI unavailable')
                version.argtypes=[];version.restype=ctypes.c_uint
                if version()!=1:raise RuntimeError('source-edit ABI')
                submit.argtypes=[ctypes.c_void_p];submit.restype=ctypes.c_uint64
                request=dict(owner=threading.get_ident(),owner_thread=threading.current_thread(),ready=False,release=False,done=False,report=None,
                    failed=None,abandoned=False,source=source_runtime,mutated=False,
                    reset_contracts_only=reset_contracts_only,
                    generation=(execution.owner.stream_handle,execution.owner.stream_generation))
                self.start()
                with self._changed,execution.borrow() as pointer:
                    ticket=int(submit(pointer)) if pointer else 0
                    if not ticket:raise RuntimeError('source-edit FIFO admission refused')
                    self._source_edit_requests[ticket]=request
                    deadline=time.monotonic()+timeout
                    while not request['ready'] and not request['done']:
                        remaining=deadline-time.monotonic()
                        if remaining<=0:
                            request.update(abandoned=True,release=True);self._changed.notify_all()
                            raise TimeoutError('source-edit FIFO fence timeout')
                        self._changed.wait(min(.01,remaining))
                    if request['done']:raise RuntimeError('source-edit fence cancelled')
                try:
                    deadline=time.monotonic()+timeout
                    while not self._source_edit_quiet(reset_contracts_only=reset_contracts_only):
                        if time.monotonic()>=deadline:raise TimeoutError('source-edit timed callback drain timeout')
                        with self._changed:self._changed.wait(.001)
                except TimeoutError:
                    with self._changed:request.update(abandoned=True,release=True);self._changed.notify_all()
                    raise
                except BaseException as error:
                    request['failed']=error;self._source_edit_retained=request
                    raise
                # The only detached consumer is owned by this source I/O thread.
                # NativeExecution retains closed-plan roots until republish.
                try:
                    with execution.owner.midi_lock,execution.owner.state_write():
                        try:
                            fast=(preserve_activity and not reset_contracts_only and
                                getattr(execution.kernel.notes,'activity_authoritative',False) and
                                execution.source_refresh_eligible(source_runtime) and
                                execution.source_refresh_at(request['report']) is not None)
                            if fast:
                                request['report'].update(source_policy_preserved=True,source_activity_preserved=True)
                            else:execution.hydrate_legacy_handoff(request['report'],**({'preserve_activity':True}
                                if preserve_activity else {}))
                            request['activity_hydrated']=request['report'].get('activity_hydrated',False)
                            request['in_scope']=True
                            request['mutated']=True
                            if not reset_contracts_only:
                                if not execution.kernel.detach_first_use_plan():raise RuntimeError('source-edit first-use detach refused')
                                source_runtime.retire_first_note_preparations()
                                execution.prepared_roots[:]=[root for root in execution.prepared_roots if root.pointer]
                            previous_filter_deferral=getattr(execution.owner,'_source_filter_publication_thread',None)
                            previous_controls=getattr(source_runtime,'_source_refresh_controls',None) if source_runtime is not None else None
                            def refresh_controls(overrides):
                                if not request['report'].get('source_refresh_applied'):
                                    if execution.source_refresh_at(request['report'],operation=1) is None:
                                        raise RuntimeError(tr('runtime.midi_generation_publish_failed'))
                                # Same effective policy:keep its revision and
                                # native table. Preserve the document entries
                                # for later explicit per-preset edits.
                                routing=execution.owner.soundfont_presets
                                routing.presets={key:{name:dict(item.get(name) or {}) if name in ('extended','performance') else
                                    set(item.get(name) or ()) if name=='extended_disabled' else item.get(name)
                                    for name in routing.OVERRIDE_KEYS} for key,item in overrides.items()}
                            try:
                                if request['report'].get('source_activity_preserved'):
                                    execution.owner._source_filter_publication_thread=threading.get_ident()
                                if fast:source_runtime._source_refresh_controls=refresh_controls
                                yield
                            finally:
                                execution.owner._source_filter_publication_thread=previous_filter_deferral
                                if source_runtime is not None:source_runtime._source_refresh_controls=previous_controls
                        except BaseException as error:
                            if not request['mutated']:
                                # Failed hydration has not granted legacy state
                                # mutation ownership. Never import its stale
                                # image over canonical Notes in the finally.
                                request['failed']=error
                                # A refused read has not changed source/config
                                # or native execution state. Release the exact
                                # old generation when no thread owns a failed
                                # native unlock. Do not leave future Note Off,
                                # trace Stop and Close behind a paused reader.
                                try:unlock_owners=self.source_edit_unlock_owners()
                                except BaseException:
                                    self._source_edit_retained=request
                                    raise
                                if not unlock_owners:
                                    request['publication_ready']=True
                                    request['aborted_before_mutation']=True
                                else:self._source_edit_retained=request
                            raise
                        finally:
                            request['in_scope']=False
                            if request['mutated']:
                                try:
                                    if not execution.republish_generation(source_runtime=source_runtime,full_selection=True,reset_contracts_only=reset_contracts_only):
                                        raise RuntimeError('source-edit generation publication pending')
                                except BaseException as error:
                                    request['failed']=error;self._source_edit_retained=request
                                    raise
                                request['publication_ready']=True
                finally:
                    # Do not retain MIDI mutex/writer markers while waiting for
                    # the metadata owner to acknowledge native resume.
                    if request.get('publication_ready'):
                        with self._changed:
                            request['release']=True;self._changed.notify_all()
                            deadline=time.monotonic()+timeout
                            while not request['done']:
                                remaining=deadline-time.monotonic()
                                if remaining<=0:raise TimeoutError('source-edit resume acknowledgement timeout')
                                self._changed.wait(min(.01,remaining))
        finally:self._source_edit_lock.release()

    def reset_contract_scope(self):
        return self.source_edit_scope(None,reset_contracts_only=True)

    def _source_edit_selection_adapters(self):
        return native_source_selection_adapters(self.execution.owner,kernel=self.execution.kernel)

    def _source_edit_transactions(self):
        return native_source_transactions(self.execution.owner,kernel=self.execution.kernel)

    def source_edit_unlock_owners(self):
        """Exact native thread leases; unknown ABI is not an empty-lock proof."""
        return native_source_unlock_owners(self.execution.owner,kernel=self.execution.kernel)

    def recover_source_edit_owner(self,*,timeout=5.0):
        """Loader-thread hook before exit; no resource free on failure."""
        if not 0<=timeout<=60:raise ValueError('source-edit recovery timeout')
        deadline=time.monotonic()+timeout
        while self._source_edit_retained is not None:
            try:
                if self.retry_source_edit():return True
            except (RuntimeError,ValueError):
                pass
            if time.monotonic()>=deadline:return False
            with self._changed:self._changed.wait(.01)
        return True

    def release_source_edit_owner(self):
        """Original loader's final hook: explicit QThread termination marker.

        Python DummyThread.is_alive stays true after a Qt thread exits. This
        marker avoids using recycled thread IDs as ownership evidence.
        """
        with self._source_edit_lock:
            request=self._source_edit_retained
            if request is None:return True
            if request['owner_thread'] is not threading.current_thread():raise ValueError('source-edit release owner')
            if self.source_edit_unlock_owners():return False
            request['owner_finished']=True;return True

    def retry_source_edit(self,*,allow_handover=False):
        """Only the original source owner may finish retained publication."""
        with self._source_edit_lock:
            request=self._source_edit_retained
            if request is None:return True
            if request['owner_thread'] is not threading.current_thread():
                if (not allow_handover or (request['owner_thread'].is_alive() and not request.get('owner_finished')) or self.source_edit_unlock_owners()):
                    raise ValueError('source-edit foreign retry owner')
                source_stream=self.execution.owner.stream_handle if request.get('reset_contracts_only') else request['source'].stream_handle()
                if (source_stream!=self.execution.owner.stream_handle or
                        request['generation']!=(self.execution.owner.stream_handle,self.execution.owner.stream_generation) or
                        self.execution.pending is not request['report'] or self.execution.pending['disposition']!=11):
                    raise ValueError('source-edit handover generation')
                request.update(owner=threading.get_ident(),owner_thread=threading.current_thread())
            for transaction in self._source_edit_transactions():
                if transaction.pending is not None:
                    # run() checks the saved thread before entering native code;
                    # retry the unlock only, never the already-published audio.
                    if not transaction.run(0,()).success:return False
            reset_only=request.get('reset_contracts_only',False)
            if not self._source_edit_quiet(reset_contracts_only=reset_only):return False
            if request['mutated']:
                with self.execution.owner.midi_lock,self.execution.owner.state_write():
                    if not self.execution.republish_generation(source_runtime=request['source'],full_selection=True,reset_contracts_only=reset_only):return False
            with self._changed:
                self._source_edit_retained=None;request['failed']=None;request['release']=True;self._changed.notify_all()
            return True

    def __call__(self,queue):
        with self._lock:
            if self.error is not None or self._done.is_set():return None
            client=self.execution.attach_queue(queue)
            if not client:return None
            lease=_NativeMidiClientLease(self,client)
            self.start()
            return lease

    def start(self):
        """Start before input/UI admission, including a zero-client session."""
        with self._lock:
            if self.error is not None or self._done.is_set():return False
            if self._thread is None:
                if not self.execution.start():raise RuntimeError(tr('runtime.midi_executor_start_failed'))
                self._thread=threading.Thread(target=self._consume,name='ABMSynthMidiMetadata',daemon=False);self._thread.start()
            return True

    def _consume(self):
        try:
            with self.execution.owner.activate():
                while self.execution.pointer:
                    report=self.execution.pending or self.execution.read()
                    if report is None:break
                    self.execution._capture_activity_receipt(report)
                    if report.get('raw_terminal_ack'):
                        self.execution.complete_raw(report)
                        with self._changed:self._changed.notify_all()
                        continue
                    if report.get('native_trace'):
                        with self._changed:trace=self._trace_owner
                        if trace is not None:
                            payload=trace_midi_payload(report['payload'])
                            payload.update(length=report['wire_size'],data_truncated=report['wire_size']>len(report['payload']))
                            trace.record('native_dispatch_begin' if report['disposition']==12 else 'native_dispatch_complete',
                                native_qpc_ticks=report['completed_qpc'],source_read_qpc=report['read_qpc'],
                                driver_ingress_qpc=report['received_qpc'] if report['origin_valid'] else None,
                                client=report.get('client'),publication=report.get('publication'),port=report['port'],
                                driver_pid=report['driver_pid'],driver_epoch=report['driver_epoch'],driver_sequence=report['driver_sequence'],
                                packet_id='native:{}:{}:{}'.format(report.get('client',0),report.get('publication',0),report['read_qpc']),
                                consumed=report['consumed'],native_error=report['error'],
                                **payload)
                        self.execution.pending=None;continue
                    if report.get('native_trace_fence'):
                        with self._changed:
                            if self._trace_tail is None or self._trace_tail['ticket']!=report['control_ticket']:raise ValueError('native trace tail owner')
                            if self._trace_owner is not None:
                                self._trace_owner.record('native_trace_tail',captured_frames=report['captured_frames'],
                                    dropped_frames=report['dropped_frames'],phase=report['phase'])
                            self._trace_tail['done']=True;self.execution.pending=None;self._changed.notify_all()
                        continue
                    if report.get('source_edit_command'):
                        with self._changed:
                            request=self._source_edit_requests.get(report['control_ticket'])
                            if request is None:raise ValueError('source-edit completion owner')
                            request['report']=report
                            if report['disposition']==11:
                                request['ready']=True;self._changed.notify_all()
                                while not request['release']:self._changed.wait()
                                self.execution.resume(report)
                            else:self.execution.pending=None
                            request['done']=True;self._source_edit_requests.pop(report['control_ticket']);self._changed.notify_all()
                        continue
                    if report.get('auto_reset') and report['disposition']==15:
                        while not self.execution.replay(report):time.sleep(.001)
                        if not report['successful'] and report.get('phase')!=5:
                            self.error=RuntimeError(tr('runtime.midi_filter_config_failed',error=('idle reset terminal',report['error'])))
                            if self.on_error is not None:self.on_error(self.error)
                    elif report['disposition'] in (1,4,6,7,8,10):
                        while not self.execution.replay(report):time.sleep(.001)
                        if report['disposition'] in (6,8,10):self.execution.resume(report)
                    elif report['disposition'] in (3,5):self.execution.resume(report,retry_unlock=True)
                    elif report['disposition']==2:
                        old=self.execution.kernel.first_use_plan
                        if self.execution.statistics().get('stop_requested') or (old is not None and old.cancel_reason in ('explicit','retirement')):
                            report['legacy_cancelled']=True
                            self.execution.resume(report,take_handoff=True)
                            self.execution.complete_raw(report)
                            continue
                        # Never re-run audio after a delegate exception: a
                        # partial legacy prefix may already have taken effect.
                        if not report.get('legacy_audio_completed'):
                            with self.execution.owner.midi_lock,self.execution.owner.state_write(),self.execution.owner.raw_handoff_scope(self.execution,report):
                                self.execution.hydrate_legacy_handoff(report)
                                if not report.get('legacy_audio_entered'):
                                    report['legacy_audio_entered']=True
                                    if 'direct_port' in report:
                                        # Original input origin/port belongs to
                                        # this direct producer, not a pipe TLS
                                        # or another client's active port.
                                        report['legacy_submission']=self.execution.owner.forward_raw_handoff(report)
                                    else:report['legacy_submission']=WinMMMidiPipeServer.deliver_prepared(self.on_midi,report)
                                submission=report.get('legacy_submission')
                                WinMMMidiPipeServer.complete_submission(submission)
                                report['legacy_audio_completed']=True
                        if not self.republish(self.execution.owner,report):raise RuntimeError(tr('runtime.midi_generation_publish_failed'))
                        self.execution.resume(report,legacy_completed=True)
                    elif report['disposition']==9:
                        while not self.execution.replay(report):time.sleep(.001)
                        if not report.get('explicit_cancelled'):
                            self.error=RuntimeError(tr('runtime.midi_filter_config_failed',error=('reset tail terminal',report['status'],report['error'])))
                            if self.on_error is not None:self.on_error(self.error)
                        # Native stop publishes accepted command cancellations
                        # before EOF. Drain them; retirement alone may abort
                        # the confirmed unexecuted tail after worker detach.
                    else:raise ValueError('native MIDI metadata disposition')
                    self.execution.complete_raw(report)
                    with self._changed:self._changed.notify_all()
                # EOF completes this single metadata consumer, not the server's
                # client leases. A concurrent snapshot borrower can also make
                # close temporarily refuse. Keep the owner for server detach
                # and explicit Factory.close/retire_owner; refusal is not a
                # terminal playback error which would prevent lease retirement.
        except Exception as error:
            self.error=error
            from error_reporting import record
            import traceback
            record('midi_metadata_failure',traceback=''.join(traceback.format_exception(error)),
                   disposition=self.execution.pending.get('disposition') if self.execution.pending else None)
            if self.on_error is not None:self.on_error(error)
        finally:
            self._done.set()
            with self._changed:self._changed.notify_all()

    def request_stop(self):self.execution.request_stop()

    def retry_metadata(self):
        """Retry only confirmed metadata/publication, never partial audio."""
        with self._lock:
            report=self.execution.pending;thread=self._thread
            if self.error is None or report is None or (thread is not None and thread.is_alive()):return False
            if (report['disposition']==2 and report.get('legacy_audio_entered') and
                    not report.get('legacy_audio_completed') and 'legacy_submission' not in report):return False
            if report['disposition'] not in (1,4,6,7,8,10,2):return False
            self.error=None;self._done.clear()
            self._thread=threading.Thread(target=self._consume,name='ABMSynthMidiMetadata',daemon=False);self._thread.start();return True

    def close(self):
        self.request_stop();thread=self._thread
        if thread is not None and thread is not threading.current_thread():thread.join(2)
        if thread is not None and thread.is_alive():return False
        return self.execution.close()

    def retire_owner(self):
        """Retire execution/kernel/borrowed plans, never SoundFont/audio assets.

        Caller still owns server input admission and BASS/SoundFont lifetimes.
        Any false result retains the roots needed to retry this exact order.
        """
        with self._retirement_lock:
            if self._retire_roots is None:
                kernel=self.execution.kernel
                plans=list(self.execution.prepared_roots)
                if kernel.first_use_plan is not None:plans.append(kernel.first_use_plan)
                checkpoint=(self.execution.pending or {}).get('generation_republication',{})
                plans.extend(plan for plan in (checkpoint.get('old'),checkpoint.get('plan')) if plan is not None)
                self._retire_roots=(kernel,kernel.notes,tuple(dict.fromkeys(plans)))
            kernel,notes,plans=self._retire_roots
            if not self.close():return False
            owner=self.execution.owner
            # Accepted FX graph roots outlive execution and receipt replay.
            # Retain rack/HFX/stream resources on any retryable graph close.
            for graph in tuple(self.execution.effect_graph_roots):
                if not graph.close():return False
            self.execution.effect_graph_roots.clear()
            bound=owner.channel_command_executor
            if bound is not None and (bound is not self.execution or not owner.retire_channel_executor(self.execution)):return False
            if kernel.pointer:
                if not owner.drain_native_controls() or not kernel.retire_cancelled(owner) or not kernel.retire_semantics(owner):return False
            if kernel.pointer and kernel.reset_tail_pending(owner.stream_handle):
                if not owner.drain_native_controls() or not kernel.retire_reset_tail(owner):return False
            if kernel.pointer and kernel.system_reset_pending(owner.stream_handle):
                if not owner.drain_native_controls() or not kernel.retire_system_reset(owner):return False
            if kernel.pointer and kernel.manual_reset_pending(owner.stream_handle):
                if not owner.drain_native_controls() or not kernel.retire_manual_reset(owner):return False
            if kernel.pointer and kernel.file_reset_pending(owner.stream_handle):
                if not owner.drain_native_controls() or not kernel.retire_file_reset(owner):return False
            kernel.close()
            if notes is not None:notes.close()
            for plan in plans:
                if not plan.pointer:continue
                if plan.current and plan.cache_available and not plan.replay_cache():return False
                plan.cancel(reason='retirement')
                if not plan.close():return False
                runtime=plan._runtime
                with runtime._preparation_lock:
                    if plan in runtime._native_first_use_plans:runtime._native_first_use_plans.remove(plan)
            self.execution.prepared_roots.clear()
            self._retire_roots=(kernel,None,())
            return True


class _NativeMidiClientLease:
    def __init__(self,factory,client):self.factory=factory;self.client=client
    def serve(self):
        factory=self.factory
        while factory.execution.pointer:
            # Keep the client/root alive while metadata can be explicitly
            # retried. A failed pump must not silently close its pipe.
            if factory.error is not None:
                with factory._changed:factory._changed.wait(.05)
                continue
            state=factory.execution.client_statistics(self.client)
            if state and state['reader_ended'] and not(state['queued_frames'] or state['pending_metadata'] or state['inflight']):return
            with factory._changed:factory._changed.wait(.05)
    def request_stop(self):self.factory.request_stop()
    def close(self):
        execution=self.factory.execution
        if not execution.pointer:return True
        if self.factory.error is not None:return False
        return execution.detach_queue(self.client)


class _NativeNoteAction(ctypes.Structure):
    _fields_=[('packed',ctypes.c_uint),('channel',ctypes.c_uint),('count',ctypes.c_uint64),('qpc',ctypes.c_uint64)]


class NativeMidiActivity:
    """Opt-in canonical activity component; not yet a product execution owner.

    Same original Note identity FIFO as the Python activity oracle. ABI1 uses
    caller-owned revision-qualified copies, cdecl/CDLL and explicit root leases.
    No BASS calls, RAW decoding, or discarded completion records here. Attaching
    it requires event-position identity, pre-audio resource admission and ordered
    reset/history commands; merely constructing it does not remove ring pressure.
    """
    def __init__(self,path,*,channels=128):
        if type(channels) is not int or not 1<=channels<=128:raise ValueError('native activity channel domain')
        self.library=ctypes.CDLL(str(path));self.pointer=None;self.channels=channels
        self._lifetime=threading.RLock();self._calls=0
        uint,wide,ptr=ctypes.c_uint,ctypes.c_uint64,ctypes.c_void_p
        for name,args,result in (
            ('version',[],uint),('create',[uint],ptr),('retain',[ptr],None),('release',[ptr],None),
            ('action',[ptr,uint,uint,wide,wide,uint,uint,uint],ctypes.c_int),
            ('clear',[ptr,uint,uint,wide],ctypes.c_int),('available',[ptr,ctypes.POINTER(uint),uint,uint],ctypes.c_int),
            ('prune',[ptr,wide],ctypes.c_int),('snapshot_sizes',[ptr,ctypes.POINTER(wide),uint],uint),
            ('snapshot_copy',[ptr,wide,ctypes.POINTER(wide),uint,ctypes.POINTER(wide),uint,ctypes.POINTER(wide),uint],ctypes.c_int),
            ('counts_copy',[ptr,wide,ctypes.POINTER(wide),uint],ctypes.c_int)):
            function=getattr(self.library,'native_activity_'+name);function.argtypes=args;function.restype=result
        if self.library.native_activity_version()!=1:raise ValueError('native activity ABI')
        self.presentation_available=False
        self.chord_available=False
        if hasattr(self.library,'native_activity_chord_version'):
            self.library.native_activity_chord_version.argtypes=[]
            self.library.native_activity_chord_version.restype=uint
            function=self.library.native_activity_chord_copy
            function.argtypes=[ptr,wide,ctypes.POINTER(wide),uint,ctypes.POINTER(wide),uint,ctypes.POINTER(wide),uint]
            function.restype=ctypes.c_int
            self.chord_available=self.library.native_activity_chord_version()==1
        try:
            for name,args,result in (
                ('version',[],uint),('lease_size',[],uint),
                ('publish',[ptr,wide],ctypes.c_int),
                ('acquire',[ptr,ptr,uint,ctypes.POINTER(wide),uint],ctypes.c_int),
                ('copy',[ptr,uint,ctypes.POINTER(wide),uint,ctypes.POINTER(wide),uint,ctypes.POINTER(wide),uint],ctypes.c_int),
                ('release',[ptr,uint],ctypes.c_int),('stats',[ptr,ctypes.POINTER(wide),uint],uint)):
                function=getattr(self.library,'native_activity_view_'+name);function.argtypes=args;function.restype=result
            self.presentation_available=(self.library.native_activity_view_version()==1 and
                self.library.native_activity_view_lease_size()==24)
        except AttributeError:pass  # Older component: debug copy remains available, no GUI view.
        self.pointer=self.library.native_activity_create(channels)
        if not self.pointer:raise MemoryError('native_activity_create')

    @contextmanager
    def borrow(self):
        with self._lifetime:
            pointer=self.pointer
            if pointer:self._calls+=1
        try:yield pointer
        finally:
            if pointer:
                with self._lifetime:self._calls-=1

    def chord_events(self,cursor):
        """Atomic native journal+final counts. Only the observation worker reads.

        Copy ABI1 uses uint64 serial/packed/channel/QPC rows;UINT_MAX clears
        all channels. Convert QPC to ns on this reader,not the MIDI producer.
        """
        if not self.chord_available:return None
        wide=ctypes.c_uint64;header=(wide*6)();rows=(wide*(8192*4))();counts=(wide*(self.channels*128))()
        with self.borrow() as pointer:
            if not pointer:return None
            result=self.library.native_activity_chord_copy(pointer,cursor,header,len(header),rows,8192,counts,len(counts))
        if result!=1:return None
        serial,complete,known,frequency,total,channels=map(int,header)
        if not frequency or total>8192 or channels!=self.channels:raise ValueError('native chord journal bounds')
        events=tuple((int(rows[i*4]),int(rows[i*4+1]),-1 if rows[i*4+2]==0xffffffff else int(rows[i*4+2]),
            int(rows[i*4+3])*1_000_000_000//frequency) for i in range(total))
        # This is a private completed copy,not borrowed native memory. Scan
        # zeros in NumPy instead of holding Python's GIL over128x128 cells.
        # Keep flattened address order and exact uint64 multiplicities.
        import numpy as np
        image=np.ctypeslib.as_array(counts)
        notes=tuple(((i//128,i%128),int(counts[i])) for i in np.flatnonzero(image).tolist())
        return MidiChordEventBatch(serial,events,notes,bool(known),bool(complete),id(self))

    def action(self,packed,channel,count,qpc,identity):
        if (type(packed) is not int or not 0<=packed<2**24 or type(channel) is not int or
                not 0<=channel<self.channels or type(count) is not int or not 0<=count<2**64 or
                type(qpc) is not int or not 0<qpc<2**64):raise ValueError('native activity action bounds')
        bank,program=(0,0) if identity is None else tuple(identity)
        if any(type(value) is not int or not 0<=value<=65535 for value in (bank,program)):raise ValueError('native activity identity')
        with self.borrow() as pointer:
            if not pointer:raise ValueError('native activity closed')
            return bool(self.library.native_activity_action(pointer,packed,channel,count,qpc,int(identity is not None),bank,program))

    def clear(self,channel=None,*,kind=1,qpc):
        if (channel is not None and (type(channel) is not int or not 0<=channel<self.channels)) or type(kind) is not int or kind not in (1,2,3) or type(qpc) is not int or not 0<qpc<2**64:
            raise ValueError('native activity clear bounds')
        if kind!=1 and channel is not None:raise ValueError('native activity history/all clear is global')
        with self.borrow() as pointer:
            if not pointer:raise ValueError('native activity closed')
            return bool(self.library.native_activity_clear(pointer,0xffffffff if channel is None else channel,kind,qpc))

    def set_available_presets(self,keys):
        values=() if keys is None else tuple(keys)
        for bank,program in values:
            if any(type(value) is not int or not 0<=value<=65535 for value in (bank,program)):raise ValueError('native activity available identity')
        rows=(ctypes.c_uint*max(1,len(values)))(*(bank|(program<<16) for bank,program in values))
        with self.borrow() as pointer:
            if not pointer:raise ValueError('native activity closed')
            return bool(self.library.native_activity_available(pointer,rows,len(values),int(keys is not None)))

    def prune(self,qpc):
        if type(qpc) is not int or not 0<qpc<2**64:raise ValueError('native activity prune time')
        with self.borrow() as pointer:
            if not pointer:raise ValueError('native activity closed')
            return bool(self.library.native_activity_prune(pointer,qpc))

    def snapshot(self,*,attempts=8):
        """A mismatch discards the WHOLE copy, never mixes separate revisions."""
        for _ in range(attempts):
            with self.borrow() as pointer:
                if not pointer:raise ValueError('native activity closed')
                header=(ctypes.c_uint64*6)()
                if self.library.native_activity_snapshot_sizes(pointer,header,6)!=6:raise ValueError('native activity snapshot header')
                revision,channels,presets,fifo,known,frequency=tuple(header)
                if channels!=self.channels or not frequency:raise ValueError('native activity snapshot domain')
                channel_rows=(ctypes.c_uint64*(channels*4))()
                preset_rows=(ctypes.c_uint64*max(1,presets*7))()
                fifo_rows=(ctypes.c_uint64*max(1,fifo*5))()
                count_rows=(ctypes.c_uint64*(channels*256*4))()
                copied=self.library.native_activity_snapshot_copy(pointer,revision,channel_rows,channels,preset_rows,presets,fifo_rows,fifo)
                if copied==-1:continue
                if copied!=1:raise ValueError('native activity snapshot copy')
                copied=self.library.native_activity_counts_copy(pointer,revision,count_rows,channels*256)
                if copied==-1:continue
                if copied!=1:raise ValueError('native activity counts copy')
            decoded=[]
            for index in range(presets):
                row=tuple(preset_rows[index*7:(index+1)*7])
                strength=ctypes.c_double.from_buffer_copy(ctypes.c_uint64(row[5])).value
                decoded.append(row[:5]+(strength,row[6]))
            return dict(revision=revision,frequency=frequency,available_known=bool(known),
                channels=tuple(tuple(channel_rows[i*4:(i+1)*4]) for i in range(channels)),
                presets=tuple(decoded),fifo=tuple(tuple(fifo_rows[i*5:(i+1)*5]) for i in range(fifo)),
                counts=tuple(tuple(count_rows[i*4:(i+1)*4]) for i in range(channels*256)))
        raise RuntimeError('native activity snapshot changed during every copy')

    def pressed_notes(self,channels,*,attempts=2):
        """Observer-worker-only fixed count copy,not a transfer/FIFO snapshot.

        Existing counts_copy ABI owns one short shared activity-state lock,
        never a BASS/render/execution lock. No Python row expansion for32768
        records. Return None on revision churn rather than old/mixed notes.
        The audio owner retires observers before releasing this activity root.
        """
        channels=tuple(channels)
        if any(type(ch) is not int or not 0<=ch<self.channels for ch in channels):return None
        rows=(ctypes.c_uint64*(self.channels*256*4))()
        for _ in range(attempts):
            view=self.acquire_presentation()
            if view is None:return None
            try:
                # Existing pinned-view quality ABI supplies canonical validity
                # and revision without expanding preset/FIFO presentation rows.
                if view.quality is None:return None
                quality=(ctypes.c_uint64*4)()
                if view.quality(view._lease,24,quality,4)!=4 or not quality[1]&1:return None
                revision=int(quality[3])
                with self.borrow() as pointer:
                    if not pointer:return None
                    copied=self.library.native_activity_counts_copy(pointer,revision,rows,self.channels*256)
                if view.quality(view._lease,24,quality,4)!=4 or not quality[1]&1 or quality[3]!=revision:
                    copied=-1
            finally:
                if not view.close():raise RuntimeError('native chord observation lease release pending')
            if copied==-1:continue
            if copied!=1:return None
            import numpy as np
            counts=np.ctypeslib.as_array(rows).reshape(self.channels,256,4)[:, :128, 2]
            active_ch,active_key=np.nonzero(counts)
            selected=set(channels)
            return tuple((int(ch),int(key)) for ch,key in zip(active_ch,active_key) if ch in selected)
        return None

    def _transfer_api(self):
        """Control-only ABI; capacities are rows, not bytes or scalar cells."""
        uint,wide,ptr=ctypes.c_uint,ctypes.c_uint64,ctypes.c_void_p
        pwide,puint=ctypes.POINTER(wide),ctypes.POINTER(uint)
        try:
            version=getattr(self.library,'native_activity_channel_visual_version',None)
            if version is not None:version.argtypes=[];version.restype=uint
            self._transfer_extended=version is not None and version()==1
            for name,args,result in (
                ('version',[],uint),('header',[ptr,pwide,uint],uint),
                ('copy',[ptr,wide,pwide,uint,pwide,uint,pwide,uint,pwide,uint,puint,uint],ctypes.c_int),
                ('import',[ptr,wide,pwide,uint,pwide,uint,pwide,uint,pwide,uint,pwide,uint,puint,uint],ctypes.c_int)):
                function=getattr(self.library,'native_activity_transfer_'+name);function.argtypes=args;function.restype=result
            for name in ('header','copy'):
                function=getattr(self.library,'native_activity_transfer_'+name+('_v2' if self._transfer_extended else ''))
                base=getattr(self.library,'native_activity_transfer_'+name)
                function.argtypes=base.argtypes;function.restype=base.restype
                setattr(self,'_transfer_'+name,function)
            return self.library.native_activity_transfer_version()==1
        except AttributeError:return False

    def export_state(self,*,attempts=8):
        """Complete ownership transfer, NOT a GUI snapshot or audio read.

        Includes Available, global ordinal, every raw overlap/activity count,
        identity FIFO, Played and exact visual-state double bits. A revision
        mismatch discards the whole copy. Native actor/typed fence integration
        remains separate; public import refuses an attached activity owner.
        """
        header,ch_rows,preset_rows,fifo_rows,count_rows,available_rows=self._export_transfer_buffers(attempts=attempts)
        _revision,channels,presets,fifo,_known,_frequency,available,_ordinal,_version=header
        rows=lambda values,width,count:tuple(tuple(values[index*width:(index+1)*width]) for index in range(count))
        return dict(header=header,channels=rows(ch_rows,6 if _version==2 else 4,channels),presets=rows(preset_rows,7,presets),
            fifo=rows(fifo_rows,5,fifo),counts=rows(count_rows,4,channels*256),available=tuple(available_rows[:available]))

    def _export_transfer_buffers(self,*,attempts=8):
        """Owned coherent UINT buffers, never borrowed native storage.

        Source-only republishing need not decode/re-encode32768 count rows
        into Python objects. Native transfer/restore still validates every
        dimension, revision, sum and FIFO identity at its delivered fence.
        """
        if not self._transfer_api():raise ValueError('native activity transfer ABI unavailable')
        for _ in range(attempts):
            with self.borrow() as pointer:
                if not pointer:raise ValueError('native activity closed')
                header=(ctypes.c_uint64*9)()
                if self._transfer_header(pointer,header,9)!=9:raise RuntimeError('native activity transfer header')
                revision,channels,presets,fifo,known,frequency,available,ordinal,version=map(int,header)
                if channels!=self.channels or not frequency or version not in (1,2):raise ValueError('native activity transfer domain')
                counts=channels*256
                ch_rows=(ctypes.c_uint64*(channels*(6 if version==2 else 4)))();preset_rows=(ctypes.c_uint64*max(1,presets*7))()
                fifo_rows=(ctypes.c_uint64*max(1,fifo*5))();count_rows=(ctypes.c_uint64*(counts*4))()
                available_rows=(ctypes.c_uint*max(1,available))()
                status=self._transfer_copy(pointer,revision,ch_rows,channels,preset_rows,presets,
                    fifo_rows,fifo,count_rows,counts,available_rows,available)
                if status==-1:continue
                if status!=1:raise RuntimeError('native activity transfer copy')
            return tuple(header),ch_rows,preset_rows,fifo_rows,count_rows,available_rows
        raise RuntimeError('native activity transfer changed during every copy')

    def _pack_transfer_state(self,state,*,validate_only=False):
        """Shared strict row/UINT validation for initial and fenced transfers."""
        if set(state)!=set(('header','channels','presets','fifo','counts','available')):raise ValueError('native activity transfer fields')
        header=tuple(state['header'])
        if len(header)!=9 or any(type(value) is not int or not 0<=value<2**64 for value in header):raise ValueError('native activity transfer header values')
        if header[1]!=self.channels or header[8] not in (1,2) or header[4]>1:raise ValueError('native activity transfer header domain')
        def pack(name,width,count):
            from itertools import chain
            import numpy as np
            values=tuple(tuple(row) for row in state[name])
            if len(values)!=count or any(len(row)!=width for row in values):
                raise ValueError('native activity transfer '+name+' dimensions/values')
            flat=tuple(chain.from_iterable(values))
            if set(map(type,flat))-{int} or min(flat,default=0)<0 or max(flat,default=0)>=2**64:
                raise ValueError('native activity transfer '+name+' dimensions/values')
            if validate_only:return None
            # Same UINT64 ABI, copied once by NumPy's native iterator rather
            # than expanding128k Python positional arguments into ctypes.
            packed=np.fromiter(flat,dtype=np.uint64,count=len(flat)) if flat else np.zeros(1,dtype=np.uint64)
            return (ctypes.c_uint64*max(1,count*width)).from_buffer(packed)
        ch_rows=pack('channels',6 if header[8]==2 else 4,self.channels);preset_rows=pack('presets',7,header[2])
        fifo_rows=pack('fifo',5,header[3]);count_rows=pack('counts',4,self.channels*256)
        available=tuple(state['available'])
        if len(available)!=header[6] or any(type(value) is not int or not 0<=value<2**32 for value in available):raise ValueError('native activity transfer available values')
        available_rows=None if validate_only else (ctypes.c_uint*max(1,len(available)))(*available)
        return header,ch_rows,preset_rows,fifo_rows,count_rows,available_rows

    def capture_legacy_state(self,owner,*,reference=None):
        """Control/handoff-only codec; caller must own the ingress fence.

        Capture the three legacy owners together. QPC/float seconds must
        round-trip exactly: silently rounding an arbitrary clock would change
        the visual envelope. On Windows monotonic uses QPC/QPF, documented at
        https://docs.python.org/3.14/library/time.html#time.monotonic
        Existing FIFO ordinals survive an unchanged logical FIFO. Otherwise
        allocate fresh internal migration IDs preserving each key's order;
        these IDs never claim historical driver order or become MIDI origins.
        """
        reference=self.export_state() if reference is None else reference
        self._pack_transfer_state(reference,validate_only=True)
        frequency=reference['header'][5];absent=2**64-1
        if not frequency:raise ValueError('native activity legacy clock domain')
        def phase(value,previous=None):
            if type(value) not in (int,float) or not math.isfinite(value) or value<0:raise ValueError('native activity legacy phase')
            if previous is not None and previous/frequency==value:return previous
            ticks=round(value*frequency)
            if not 0<=ticks<2**64 or ticks/frequency!=value:raise ValueError('native activity legacy phase not QPC-exact')
            return ticks
        def identity(value):
            values=tuple(value)
            if len(values)!=2 or any(type(v) is not int or not 0<=v<=65535 for v in values):raise ValueError('native activity legacy preset identity')
            return values[0]|(values[1]<<16)
        def count(value):
            if type(value) is not int or not 0<=value<2**64:raise ValueError('native activity legacy count')
            return value
        sf=owner.soundfont_activity;channel=owner.midi_activity
        with owner.midi_lock,sf.lock,channel.lock:
            overlap=dict(owner.midi_options.active_notes)
            note_counts=tuple(dict(rows) for rows in channel.notes)
            velocities=tuple(channel.velocity);times=tuple(channel.last_on)
            visual_phases=tuple(channel.visual_phase);visual_initials=tuple(channel.visual_initial)
            active={identity(key):count(value) for key,value in sf.active_notes.items()}
            held={tuple(key):tuple(identity(row) for row in rows) for key,rows in sf.note_rows.items()}
            played={identity(row) for row in sf.played_rows}
            visuals={identity(row):tuple(value) for row,value in sf.visual_rows.items()}
            released={identity(row):value for row,value in sf.release_channels.items()}
            available=None if sf.available_presets is None else tuple(sorted(identity(row) for row in sf.available_presets))
        if len(note_counts)!=128 or len(velocities)!=128 or len(times)!=128:raise ValueError('native activity legacy channel storage')
        def key_cell(cell):
            if len(cell)!=2 or any(type(v) is not int for v in cell) or not 0<=cell[0]<self.channels or not 0<=cell[1]<256:
                raise ValueError('native activity legacy key domain')
        for cell,value in overlap.items():key_cell(cell);count(value)
        for cell in held:key_cell(cell)
        for ch,rows in enumerate(note_counts):
            for key,value in rows.items():key_cell((ch,key));count(value)
            if ch>=self.channels and (velocities[ch] or times[ch]):raise ValueError('native activity legacy inactive channel')
        channels=[]
        if not hasattr(self,'_empty_transfer_counts'):
            self._empty_transfer_counts=tuple((ch,key,0,0) for ch in range(self.channels) for key in range(256))
        counts=list(self._empty_transfer_counts)
        physical_by_channel=[{} for _ in range(self.channels)]
        for (ch,key),value in overlap.items():physical_by_channel[ch][key]=value
        for ch in range(self.channels):
            if type(velocities[ch]) is not int or not 0<=velocities[ch]<=255:raise ValueError('native activity legacy velocity')
            physical=physical_by_channel[ch];activity=note_counts[ch]
            activity_total=sum(activity.values());overlap_total=sum(physical.values())
            for key in activity.keys()|physical.keys():
                counts[ch*256+key]=(ch,key,activity.get(key,0),physical.get(key,0))
            channels.append((activity_total,velocities[ch],phase(times[ch],reference['channels'][ch][2]),overlap_total))
            if reference['header'][8]==2:
                ticks=phase(visual_phases[ch],reference['channels'][ch][4])
                initial=visual_initials[ch]
                if not math.isfinite(initial) or not 0<=initial<=100:raise ValueError('native channel visual initial')
                bits=ctypes.c_uint64.from_buffer_copy(ctypes.c_double(initial)).value
                channels[-1]+=ticks,bits
        prior={}
        for ch,key,address,size,ordinal in reference['fifo']:prior.setdefault((ch,key),[]).append((address,size,ordinal))
        logical={cell:tuple(rows) for cell,rows in held.items() if rows}
        def same_fifo():
            if logical.keys()!=prior.keys():return False
            for cell,runs in prior.items():
                rows=logical[cell]
                if sum(size for _address,size,_ordinal in runs)!=len(rows):return False
                offset=0
                for address,size,_ordinal in runs:
                    if any(row!=address for row in rows[offset:offset+size]):return False
                    offset+=size
            return True
        if same_fifo():
            fifo=tuple(reference['fifo']);ordinal=reference['header'][7]
        else:
            fifo=[];ordinal=reference['header'][7]
            for (ch,key),rows in sorted(logical.items()):
                for address in rows:
                    if ordinal==absent:raise ValueError('native activity legacy ordinal exhausted')
                    ordinal+=1
                    if fifo and fifo[-1][:3]==(ch,key,address) and fifo[-1][4]+fifo[-1][3]==ordinal:
                        previous=fifo[-1];fifo[-1]=previous[:3]+(previous[3]+1,previous[4])
                    else:fifo.append((ch,key,address,1,ordinal))
            fifo=tuple(fifo)
        presets=[]
        addresses=set(active)|played|set(visuals)|set(released)|{row[0] for row in reference['presets']}
        prior_presets={row[0]:row for row in reference['presets']}
        for address in sorted(addresses):
            visual=visuals.get(address)
            if visual is None:visual_count=absent;ticks=bits=0
            else:
                if len(visual)!=3:raise ValueError('native activity legacy visual row')
                visual_count=count(visual[0]);old=prior_presets.get(address)
                ticks=phase(visual[1],old[4] if old is not None and old[3]!=absent else None);initial=visual[2]
                if type(initial) not in (int,float) or not math.isfinite(initial) or not 0<=initial<=100:raise ValueError('native activity legacy strength')
                bits=ctypes.c_uint64.from_buffer_copy(ctypes.c_double(initial)).value
            release=released.get(address,absent)
            if type(release) is not int or (release!=absent and not 0<=release<self.channels):raise ValueError('native activity legacy release channel')
            presets.append((address,active.get(address,0),int(address in played),visual_count,ticks,bits,release))
        result=dict(header=(reference['header'][0],self.channels,len(presets),len(fifo),int(available is not None),frequency,
                           len(available or ()),ordinal,reference['header'][8]),channels=tuple(channels),presets=tuple(presets),fifo=fifo,
                    counts=tuple(counts),available=available or ())
        self._pack_transfer_state(result,validate_only=True)
        return result

    def restore_legacy_state(self,owner,state=None,*,staged=None):
        """Hydrate existing mutable owners before an owned legacy delegation.

        Caller owns the ingress fence. Prevalidate native cross-row invariants
        and allocate all dictionaries/deques before any legacy state changes.
        Preserve the owner objects, locks, clocks and previously bound setters.
        No BASS, audio event, phase update or visual pruning is performed.
        """
        if staged is not None and state is not None:raise ValueError('native activity ambiguous legacy stage')
        staged=self.stage_legacy_state(owner,state) if staged is None else staged
        self.commit_legacy_state(owner,staged)

    def stage_legacy_state(self,owner,state=None):
        """Validate and allocate, without committing any legacy activity owner.

        A compound selection/activity handoff can stage both images before
        either commit. This does not advance native state, emit or ACK MIDI.
        """
        state=self.export_state() if state is None else state
        validator=NativeMidiActivity(self.library._name,channels=self.channels)
        try:
            if validator.import_state(state)!=1:raise ValueError('native activity legacy restore image')
        finally:
            if not validator.close():raise RuntimeError('native activity legacy validation owner')
        frequency=state['header'][5];absent=2**64-1
        keys=lambda address:(address&65535,address>>16)
        overlap={};notes=[{} for _ in range(128)];velocity=[0]*128;last_on=[0.0]*128
        for ch,key,activity,physical in state['counts']:
            if activity:notes[ch][key]=activity
            if physical:overlap[ch,key]=physical
        visual_phases=[0.]*128;visual_initials=[0.]*128
        for ch,row in enumerate(state['channels']):
            velocity[ch]=row[1];last_on[ch]=row[2]/frequency
            if state['header'][8]==2:
                visual_phases[ch]=row[4]/frequency
                visual_initials[ch]=ctypes.c_double.from_buffer_copy(ctypes.c_uint64(row[5])).value
        active={};played=set();visuals={};released={};held={}
        for address,total,seen,visual,qpc,bits,release in state['presets']:
            key=keys(address)
            if total:active[key]=total
            if seen:played.add(key)
            if visual!=absent:visuals[key]=(visual,qpc/frequency,ctypes.c_double.from_buffer_copy(ctypes.c_uint64(bits)).value)
            if release!=absent:released[key]=release
        for ch,key,address,size,_ordinal in state['fifo']:
            held.setdefault((ch,key),deque()).extend(keys(address) for _ in range(size))
        available={keys(address) for address in state['available']} if state['header'][4] else None
        return _LegacyActivityStage(self,owner,(owner.stream_handle,owner.stream_generation,self.pointer),
                (overlap,notes,velocity,last_on,active,held,played,visuals,released,available,visual_phases,visual_initials))

    def commit_legacy_state(self,owner,staged):
        """Commit only a fully staged activity image; no BASS/audio/native writes."""
        self.validate_legacy_stage(owner,staged)
        sf=owner.soundfont_activity;channel=owner.midi_activity
        with owner.midi_lock,sf.lock,channel.lock:
            self.validate_legacy_stage(owner,staged)
            self._commit_legacy_state_unlocked(staged)

    def validate_legacy_stage(self,owner,staged):
        if (type(staged) is not _LegacyActivityStage or staged.provider is not self or staged.owner is not owner or
                staged.epoch!=(owner.stream_handle,owner.stream_generation,self.pointer)):
            raise ValueError('native activity stale legacy stage')

    def _commit_legacy_state_unlocked(self,staged):
        owner=staged.owner
        overlap,notes,velocity,last_on,active,held,played,visuals,released,available,visual_phases,visual_initials=staged.values
        sf=owner.soundfont_activity;channel=owner.midi_activity
        owner.midi_options.active_notes=overlap
        channel.notes=notes;channel.velocity=velocity;channel.last_on=last_on
        channel.visual_phase=visual_phases;channel.visual_initial=visual_initials
        sf.active_notes=active;sf.note_rows=held;sf.played_rows=played;sf.visual_rows=visuals
        sf.release_channels=released;sf.available_presets=available

    def import_state(self,state,*,expected_revision=None):
        """1=committed,0=refused,-1=target changed; no partial state/audio.

        Validate caller-owned ctypes dimensions before crossing the ABI. Native
        staging additionally checks cross-row sums, FIFO ordinal ownership and
        source identities, then atomically swaps only an unattached clean owner.
        Never use bool(status): stale=-1 must NOT count as successful transfer.
        """
        if not self._transfer_api():raise ValueError('native activity transfer ABI unavailable')
        header,ch_rows,preset_rows,fifo_rows,count_rows,available_rows=self._pack_transfer_state(state)
        header_rows=(ctypes.c_uint64*9)(*header)
        if expected_revision is not None and (type(expected_revision) is not int or not 0<=expected_revision<2**64):raise ValueError('native activity transfer target revision')
        with self.borrow() as pointer:
            if not pointer:raise ValueError('native activity closed')
            if expected_revision is None:
                target=(ctypes.c_uint64*9)()
                if self.library.native_activity_transfer_header(pointer,target,9)!=9:raise RuntimeError('native activity transfer target header')
                expected_revision=int(target[0])
            return int(self.library.native_activity_transfer_import(pointer,expected_revision,header_rows,9,ch_rows,self.channels,
                preset_rows,header[2],fifo_rows,header[3],count_rows,self.channels*256,available_rows,header[6]))

    def publish_presentation(self,qpc):
        """Input/control actor only: BUSY skips a GUI copy, never a MIDI event."""
        if type(qpc) is not int or not 0<qpc<2**64:raise ValueError('native activity publication time')
        if not self.presentation_available:raise ValueError('native activity presentation ABI unavailable')
        with self.borrow() as pointer:
            if not pointer:raise ValueError('native activity closed')
            status=self.library.native_activity_view_publish(pointer,qpc)
            if status not in (0,1,2):raise RuntimeError('native activity presentation publication ABI')
            return status

    def acquire_presentation(self):
        """Pin one immutable GUI copy; native retains the root before borrow ends."""
        if not self.presentation_available:raise ValueError('native activity presentation ABI unavailable')
        view=NativeActivityView(self.library)
        with self.borrow() as pointer:
            if not pointer:raise ValueError('native activity closed')
            status=self.library.native_activity_view_acquire(pointer,view._lease,24,view._header,7)
            if status==1:view._held=True
            elif status not in (0,2):raise RuntimeError('native activity presentation acquire ABI')
        if status==1:return view
        if status==0:raise ValueError('native activity presentation acquire refused')
        return None

    def presentation_statistics(self):
        if not self.presentation_available:raise ValueError('native activity presentation ABI unavailable')
        rows=(ctypes.c_uint64*4)()
        with self.borrow() as pointer:
            if not pointer:raise ValueError('native activity closed')
            if self.library.native_activity_view_stats(pointer,rows,4)!=4:raise RuntimeError('native activity presentation stats ABI')
        return dict(zip(('published','busy','allocation_failures','generation_exhausted'),map(int,rows)))

    def close(self):
        with self._lifetime:
            if self._calls:return False
            if self.pointer:self.library.native_activity_release(self.pointer);self.pointer=None
        return True


class NativeActivityView:
    """Unique caller-owned lease; copy/release never take the canonical-state lock.

    audio_fx.cpp native_activity_view ABI1: aligned uint64[3], header uint64[7].
    Additive view_copy_v2 retains the same lease/header and row capacities,
    but copies6 UINT64/channel:4 original fields plus phase QPC/double bits.
    The v1 copy export still writes exactly4 fields. No live canonical lock
    is added by either copy. Transfer header/copy v2 uses the same6-field row
    for ownership handoff;the legacy codec preserves its phase/strength too.
    CDLL releases the GIL (Python ctypes documentation); native lease state uses
    InterlockedCompareExchange64 and retains its owner independent of root.close.
    Keep the DLL and lease buffer alive until confirmed release. A view is NOT
    cloneable: duplicating its bytes would duplicate a single release identity.
    Use a context manager or explicit close; stale rows remain coherent and are
    reported separately from the current canonical revision.
    """
    def __init__(self,library):
        self.library=library;self._lease=(ctypes.c_uint64*3)();self._header=(ctypes.c_uint64*7)()
        self._lock=threading.RLock();self._held=False
        self._channel_visual_copy=getattr(library,'native_activity_view_copy_v2',None)
        if self._channel_visual_copy is not None:
            self._channel_visual_copy.argtypes=library.native_activity_view_copy.argtypes
            self._channel_visual_copy.restype=ctypes.c_int
        self.quality=None
        version=getattr(library,'native_activity_view_quality_version',None)
        quality=getattr(library,'native_activity_view_quality',None)
        if version is not None and quality is not None:
            version.argtypes=[];version.restype=ctypes.c_uint
            if version()==1:
                quality.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint]
                quality.restype=ctypes.c_uint;self.quality=quality
        if ctypes.sizeof(self._lease)!=24 or ctypes.addressof(self._lease)%8:
            raise ValueError('native activity presentation lease alignment')

    def __copy__(self):raise TypeError('native activity presentation lease cannot be copied')
    def __deepcopy__(self,memo):raise TypeError('native activity presentation lease cannot be copied')

    def snapshot(self):
        with self._lock:
            if not self._held:raise ValueError('native activity presentation closed')
            channels,presets=int(self._header[3]),int(self._header[4])
            if not 1<=channels<=128 or not self._header[5]:raise ValueError('native activity presentation domain')
            stride=6 if self._channel_visual_copy is not None else 4
            channel_rows=(ctypes.c_uint64*(channels*stride))();preset_rows=(ctypes.c_uint64*max(1,presets*7))()
            copy=self._channel_visual_copy or self.library.native_activity_view_copy
            status=copy(self._lease,24,self._header,7,channel_rows,channels,preset_rows,presets)
            if status!=1:raise RuntimeError('native activity presentation copy busy' if status==2 else 'native activity presentation copy refused')
            generation,revision,current_revision,channel_count,preset_count,frequency,published_qpc=tuple(self._header)
            if (channel_count,preset_count)!=(channels,presets):raise RuntimeError('native activity presentation changed while pinned')
            source_validity=validity=validity_known=None;stale=revision!=current_revision
            if self.quality is not None:
                values=(ctypes.c_uint64*4)();quality_status=self.quality(self._lease,24,values,4)
                if quality_status==4:
                    source_validity,validity,source_revision,current_revision=map(int,values)
                    if source_revision!=revision:raise RuntimeError('native activity presentation quality owner')
                    validity_known=bool(validity&1)
                    stale=revision!=current_revision or source_validity!=validity or not validity_known or not source_validity&1
                elif quality_status==2:stale=True;current_revision=None
                else:raise RuntimeError('native activity presentation quality refused')
            decoded=[]
            for index in range(presets):
                row=tuple(preset_rows[index*7:(index+1)*7])
                strength=ctypes.c_double.from_buffer_copy(ctypes.c_uint64(row[5])).value
                decoded.append(row[:5]+(strength,row[6]))
            return dict(generation=generation,revision=revision,current_revision=current_revision,
                stale=stale,source_validity=source_validity,validity=validity,validity_known=validity_known,
                frequency=frequency,published_qpc=published_qpc,
                channels=tuple(tuple(channel_rows[i*stride:i*stride+4]) for i in range(channels)),presets=tuple(decoded),
                channel_visuals=(tuple((channel_rows[i*stride],channel_rows[i*stride+4]/frequency,
                    ctypes.c_double.from_buffer_copy(ctypes.c_uint64(channel_rows[i*stride+5])).value)
                    for i in range(channels)) if stride==6 else None))

    def close(self):
        with self._lock:
            if not self._held:return True
            status=self.library.native_activity_view_release(self._lease,24)
            if status==2:return False  # Retain DLL/buffer/owner for explicit retry.
            if status!=1:raise RuntimeError('native activity presentation release refused')
            self._held=False;return True

    def __enter__(self):
        if not self._held:raise ValueError('native activity presentation closed')
        return self

    def __exit__(self,*exception):
        if not self.close():raise RuntimeError('native activity presentation release pending')


class NativeActivityPresentation:
    """Read-only adapter for the existing SoundFont/channel activity APIs.

    One read_scope pins a coherent decoded snapshot for all consumers in a GUI
    refresh. No publication/prune/clear/BASS/kernel lock is entered here.
    Ordered writers and canonical authority are owned by execution admission,
    not by a GUI reader or by the mere presence of this adapter.
    BUSY preserves the last coherent presentation; no sample means explicit
    unavailability, never fabricated zero activity. Only Python readers share
    this short adapter lock; MIDI/native/audio actors never borrow it.
    """
    def __init__(self,activity,*,clock=time.monotonic):
        if not activity.presentation_available:raise ValueError('native activity presentation ABI unavailable')
        self.activity=activity;self.clock=clock;self._latest=None
        self._lock=threading.RLock();self._scope=threading.local();self._retained_views=[];self._closed=False;self._active_scopes=0

    def _read(self):
        if self._closed:raise ValueError('native activity presentation adapter closed')
        scoped=getattr(self._scope,'state',None)
        if scoped is not None:return scoped
        with self._lock:
            if self._closed:raise ValueError('native activity presentation adapter closed')
            retained=[]
            for view in self._retained_views:
                if not view.close():retained.append(view)
            self._retained_views=retained
            view=self.activity.acquire_presentation()
            if view is not None:
                try:self._latest=view.snapshot()
                finally:
                    try:closed=view.close()
                    except BaseException:
                        self._retained_views.append(view)
                        raise
                    if not closed:self._retained_views.append(view)
            if self._latest is None:raise RuntimeError('native activity presentation not yet published')
            if view is None:
                # A missed acquisition is not evidence that the cached source
                # is current. Keep its rows but qualify freshness as unknown.
                self._latest={**self._latest,'current_revision':None,'stale':True,'validity':None,'validity_known':None}
            return self._latest

    @contextmanager
    def read_scope(self):
        previous=getattr(self._scope,'state',None)
        if previous is not None:
            yield self
            return
        with self._lock:
            state=self._read();when=float(self.clock())
            self._scope.state=state;self._scope.when=when;self._active_scopes+=1
        try:yield self
        finally:
            with self._lock:
                self._scope.state=None;self._scope.when=None;self._active_scopes-=1

    def revision(self):
        state=self._read()
        return state['revision'],state['current_revision'],state['stale']

    def playing_state(self):
        return {(row[0]&65535,row[0]>>16):row[1] for row in self._read()['presets'] if row[1]}

    def played_state(self):
        return {(row[0]&65535,row[0]>>16) for row in self._read()['presets'] if row[2]}

    def visual_state(self,now=None):
        state=self._read();frequency=state['frequency']
        current=(getattr(self._scope,'when',None) if now is None else float(now))
        if current is None:current=float(self.clock())
        visual={}
        for row in state['presets']:
            if row[3]==0xffffffffffffffff:continue
            strength=midi_activity_strength_at((row[3],row[4]/frequency,row[5]),current)
            if strength>0:visual[row[0]&65535,row[0]>>16]=strength/100
        return visual

    def snapshot(self):
        """ChannelMidiActivity shape: held count, velocity, last-On seconds."""
        state=self._read();frequency=state['frequency']
        return tuple((row[0],row[1],row[2]/frequency) for row in state['channels'])

    def channel_visual_state(self,now=None):
        state=self._read()
        if state.get('channel_visuals') is None:raise RuntimeError('native channel activity envelope ABI unavailable')
        current=getattr(self._scope,'when',None) if now is None else float(now)
        if current is None:current=float(self.clock())
        return tuple(midi_activity_strength_at(row,current)/100 for row in state['channel_visuals'])

    def close(self):
        with self._lock:
            if self._active_scopes:return False
            self._retained_views=[view for view in self._retained_views if not view.close()]
            if self._retained_views:return False
            self._closed=True;self._latest=None
            return True


class NativeNoteKernel:
    """Native note semantics + one native raw submission, candidate only.

    The current product still uses the original interpreter. This building
    block is not an input worker: CC/selection/preload, command ordering and
    bounded asynchronous metadata delivery must join it before activation.
    Pointer/function borrowers live through completion; close takes the same
    control/input lock as calls. Future external C++ borrowers require their
    own explicit stop/join barrier before this root can be destroyed.
    """
    def _activity_api(self):
        if self.library is None:return False
        uint,ptr,wide=ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint64
        try:
            for name,args,result in (
                    ('version',[],uint),('attach',[ptr,ptr],ctypes.c_int),
                    ('sources',[ptr,ctypes.POINTER(uint),uint],ctypes.c_int),
                    ('snapshot',[ptr,ctypes.POINTER(wide),uint],uint)):
                function=getattr(self.library,'midi_note_kernel_activity_'+name)
                function.argtypes=args;function.restype=result
            return self.library.midi_note_kernel_activity_version()==1
        except AttributeError:return False

    def _activity_source_rows(self,sources):
        if isinstance(sources,dict):
            if any(type(channel) is not int or not 0<=channel<self.channels for channel in sources):
                raise ValueError('native activity source channel')
            identities=tuple(sources.get(channel) for channel in range(self.channels))
        else:
            identities=tuple(sources)
            if len(identities)!=self.channels:raise ValueError('native activity source rows')
        rows=[]
        for identity in identities:
            if identity is None:rows.extend((0,0,0));continue
            values=tuple(identity)
            if len(values)!=2 or any(type(value) is not int or not 0<=value<=65535 for value in values):
                raise ValueError('native activity source identity')
            rows.extend((1,*values))
        return (ctypes.c_uint*len(rows))(*rows)

    def attach_activity(self,activity,sources=None,*,seeded=False):
        """Opt-in SHADOW only. No replay skipping or product authority transfer.

        Native attach retains an empty compatible owner before an execution
        borrower exists (or after retirement); a paused borrower still refuses.
        A source publication failure leaves the attached root retained for an
        explicit retry/detach, never silently retires it. No implicit history
        seed is invented: product binding requires a separate complete transfer.
        """
        if type(seeded) is not bool or (seeded and activity is None):raise ValueError('native activity seeded attachment policy')
        if activity is None and sources is not None:raise ValueError('native activity detach cannot publish sources')
        rows=self._activity_source_rows(sources) if sources is not None else None
        if activity is not None and (not isinstance(activity,NativeMidiActivity) or activity.channels!=self.channels or
                self.library is None or activity.library._handle!=self.library._handle):
            raise ValueError('native activity note owner image/domain')
        with self.lock:
            if not self.pointer or not self._activity_api():return False
            attach=self.library.midi_note_kernel_activity_attach
            if seeded:
                version=getattr(self.library,'midi_note_kernel_activity_seed_version',None)
                attach=getattr(self.library,'midi_note_kernel_activity_seed_attach',None)
                if version is None or attach is None:return False
                version.argtypes=[];version.restype=ctypes.c_uint
                if version()!=1:return False
                attach.argtypes=[ctypes.c_void_p,ctypes.c_void_p];attach.restype=ctypes.c_int
            with activity.borrow() if activity is not None else nullcontext(None) as pointer:
                if activity is not None and not pointer:return False
                if not attach(self.pointer,pointer):return False
                if self.activity is not activity:self.activity_authoritative=False
                self.activity=activity
                if rows is not None and not self.library.midi_note_kernel_activity_sources(self.pointer,rows,self.channels):return False
            return True

    def set_activity_sources(self,sources):
        rows=self._activity_source_rows(sources)
        with self.lock:
            if not self.pointer or getattr(self,'activity',None) is None or not self._activity_api():return False
            return bool(self.library.midi_note_kernel_activity_sources(self.pointer,rows,self.channels))

    def set_activity_authority(self,enabled):
        """Admit only before executor creation/after joined retirement.

        An attached observer alone is not authority. ctypes CDLL uses the
        exact cdecl/pointer ABI; live mutation cannot borrow a prefix pause.
        https://docs.python.org/3.14/library/ctypes.html
        """
        if type(enabled) is not bool:raise ValueError('native activity authority flag')
        with self.lock:
            if not self.pointer or self.library is None:return False
            version=getattr(self.library,'midi_note_kernel_activity_authority_version',None)
            apply=getattr(self.library,'midi_note_kernel_activity_authority',None)
            if version is None or apply is None:return False
            version.argtypes=[];version.restype=ctypes.c_uint
            if version()!=1:return False
            apply.argtypes=[ctypes.c_void_p,ctypes.c_uint];apply.restype=ctypes.c_int
            if apply(self.pointer,int(enabled))!=1:return False
            self.activity_authoritative=enabled;return True

    def activity_statistics(self):
        with self.lock:
            if not self.pointer or not self._activity_api():return {}
            rows=(ctypes.c_uint64*8)()
            if self.library.midi_note_kernel_activity_snapshot(self.pointer,rows,8)!=8:
                raise RuntimeError('native activity note snapshot ABI')
        state=dict(zip(('attached','revision','applied_notes','applied_clears','refusals','last_actions','sources_ready','dirty'),map(int,rows)))
        for field in ('attached','sources_ready','dirty'):state[field]=bool(state[field])
        return state

    def attach_raw_context(self,context):
        if context is not None and (not context.pointer or context.library._handle!=self.library._handle):raise ValueError('RAW note decoder image')
        attach=getattr(self.library,'midi_note_kernel_raw_context',None)
        if attach is None:return context is None
        attach.argtypes=[ctypes.c_void_p,ctypes.c_void_p];attach.restype=ctypes.c_int
        with self.lock:
            if not attach(self.pointer,context.pointer if context is not None else None):return False
            self.raw_context=context;return True

    def __init__(self,bassmidi,bass,channels=128,path=None):
        self.pointer=None;self.library=None;self.lock=threading.RLock();self.channels=channels;self.activity=None;self.activity_authoritative=False
        self.functions=(bassmidi.BASS_MIDI_StreamEvents,bass.BASS_ErrorGetCode)
        self._human_functions=tuple(getattr(bass,name,None) for name in ('BASS_ChannelGetPosition','BASS_ChannelSeconds2Bytes','BASS_ChannelLock'))
        self._human_bound=False
        try:
            library=ctypes.CDLL(str(path or os.environ.get('ABMSYNTH_OBSERVATION_DLL',Path(__file__).with_name('abmsynth_dsp.dll'))))
            library.midi_note_kernel_version.argtypes=[];library.midi_note_kernel_version.restype=ctypes.c_uint
            if library.midi_note_kernel_version()!=1:return
            for name,args,result in (
                ('create',[ctypes.c_uint,ctypes.c_void_p,ctypes.c_void_p],ctypes.c_void_p),
                ('destroy',[ctypes.c_void_p],None),
                ('configure',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,ctypes.c_uint],ctypes.c_int),
                ('seed',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint],ctypes.c_int),
                ('snapshot',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint],ctypes.c_int),
                ('frequency',[],ctypes.c_uint64),
                ('run',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint,
                    ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(_NativeNoteAction),ctypes.c_uint,ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_uint64),ctypes.POINTER(ctypes.c_uint64)],ctypes.c_int)):
                function=getattr(library,'midi_note_kernel_'+name);function.argtypes=args;function.restype=result
            assert ctypes.sizeof(_NativeNoteAction)==24
            if not all(isinstance(function,ctypes._CFuncPtr) for function in self.functions):return
            pointer=library.midi_note_kernel_create(channels,*(ctypes.cast(fn,ctypes.c_void_p) for fn in self.functions))
            if not pointer:return
            self.pointer=pointer;self.library=library;self.frequency=int(library.midi_note_kernel_frequency())
            marker=getattr(library,'midi_note_kernel_humanization_version',None)
            if marker is not None and all(isinstance(fn,ctypes._CFuncPtr) for fn in self._human_functions):
                marker.argtypes=[];marker.restype=ctypes.c_uint
                if marker()==1:
                    bind=library.midi_note_kernel_humanization_bind
                    bind.argtypes=[ctypes.c_void_p]*4;bind.restype=ctypes.c_int
                    self._human_bound=bool(bind(pointer,*(ctypes.cast(fn,ctypes.c_void_p) for fn in self._human_functions)))
        except (AttributeError,OSError):pass # Older ABI leaves the original engine untouched.

    def configure(self,options):
        with self.lock:
            if not self.set_humanization(getattr(options,'humanize_timing_ms',0),getattr(options,'humanize_velocity',0),getattr(options,'humanize_preserve_simultaneity',False)):return False
            values=(ctypes.c_uint*128)(*options.velocity_table)
            if not self.library.midi_note_kernel_configure(self.pointer,values,128,int(options.overnote_enabled)):return False
            seed=getattr(self.library,'midi_note_kernel_mixer_seed',None)
            if seed is None:return not getattr(options,'mixer_muted',())
            seed.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint];seed.restype=ctypes.c_int
            mask=(ctypes.c_ubyte*128)(*(int(ch in getattr(options,'mixer_muted',())) for ch in range(128)))
            return bool(seed(self.pointer,mask,len(mask)))

    def set_humanization(self,timing,velocity,preserve_simultaneity=False):
        with self.lock:
            if not self.pointer:return False
            if not self._human_bound:return not timing and not velocity
            function=getattr(self.library,'midi_note_kernel_humanization_grouped',None)
            if function is not None:
                function.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint];function.restype=ctypes.c_int
                return bool(function(self.pointer,timing,velocity,int(preserve_simultaneity)))
            if preserve_simultaneity and timing:return False
            function=self.library.midi_note_kernel_humanization
            function.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint];function.restype=ctypes.c_int
            return bool(function(self.pointer,timing,velocity))

    def seed(self,values):
        cells=(ctypes.c_uint64*(128*128))()
        for (channel,key),count in values.items():
            if not 0<=channel<128 or not 0<=key<128 or not 0<=count<=0xffffffffffffffff:return False
            cells[channel*128+key]=count
        with self.lock:return bool(self.library.midi_note_kernel_seed(self.pointer,cells,len(cells)))

    def seed_original_shadow(self,values):
        """Handoff only: preserve bytewalker shadow, seed supported counters.

        bassmidi.h MIDI_EVENT_NOTE / bassmidi.md StreamEvent document keys
        0..127. The original app bytewalker also retains malformed byte keys
        128..255 in its Python overlap shadow. Those entries/counts are NOT
        MIDI aliases and must stay unchanged until original Off/Clear writers.
        midiCCRun qualifies the whole input and rejects high-bit payload before
        effects; future such bytes still hand off to that same original walker.

        Caller owns the EngineRuntime control/handoff barrier. seed() copies
        the projection into native-owned uint64 cells under this kernel lock;
        its BOOL refuses unsupported counts/owners without changing the source
        mapping. No RAW/audio submission and no borrowed Python storage result.
        Initial attach and public strict seed admission remain separate.
        """
        projected={}
        for (channel,key),count in values.items():
            if not 0<=channel<128 or not 0<=key<256:return False
            if key<128:projected[channel,key]=count
            elif not isinstance(count,int) or count<0:return False
        return self.seed(projected)

    def snapshot(self):
        cells=(ctypes.c_uint64*(128*128))()
        with self.lock:
            if not self.library.midi_note_kernel_snapshot(self.pointer,cells,len(cells)):raise RuntimeError('midi_note_kernel_snapshot ABI')
        return {(index//128,index%128):int(value) for index,value in enumerate(cells) if value}

    def submit(self,stream,data,port=0,mode=BASS_MIDI_EVENTS_RAW|BASS_MIDI_EVENTS_FILTER|BASS_MIDI_EVENTS_SYNC,*,timing=None,received=None,origin=None):
        # Diagnostics may use explicit mode/functions; production does not call
        # this candidate. Same-DLL timing lease + EngineRuntime serialization
        # must be held by caller through completion, never cross-image pointers.
        if timing is not None and timing.pointer and timing.dll._handle!=self.library._handle:
            raise ValueError('midi_note_kernel timing DLL image')
        raw=bytearray(data);source=(ctypes.c_ubyte*len(raw)).from_buffer(raw)
        output=(ctypes.c_ubyte*(len(raw)*2+4))();actions=(_NativeNoteAction*max(1,len(raw)//3))();result=(ctypes.c_uint64*5)()
        fields=(ctypes.c_uint64*7)(received or 0,(origin or {}).get('read_qpc') or received or 0,
            (origin or {}).get('pid',0),(origin or {}).get('epoch',0),(origin or {}).get('sequence',0),
            int((origin or {}).get('quality')=='driver'),int(origin is not None))
        with self.lock:
            status=self.library.midi_note_kernel_run(self.pointer,stream,mode,port,ctypes.addressof(source),len(raw),
                output,len(output),actions,len(actions),timing.pointer if timing is not None else None,fields,result)
            metadata=tuple((int(row.packed),int(row.channel),int(row.count),int(row.qpc)) for row in _native_items(actions,result[1]))
        return dict(status=status,wire=_native_owned_bytes(output,result[0]),actions=metadata,admitted=int(result[2]),
            error=ctypes.c_int(result[3]).value if status==2 else None,submissions=int(result[4]))

    def replay(self,owner,result,sources):
        """Metadata ONLY, using event-time QPC, not delayed GUI replay time."""
        if getattr(self,'activity_authoritative',False) and result.get('activity_already_applied'):
            # The original actions already belong to canonical native state.
            # Complete metadata receipts, not a second legacy state mutation.
            # Source/generic/retirement fences hydrate complete legacy owners.
            result['replay_index']=len(result['actions']);result['replay_stage']=0
            return
        index=result.get('replay_index',0)
        while index<len(result['actions']):
            packed,channel,count,qpc=result['actions'][index]
            status,key,velocity=packed&255,(packed>>8)&255,(packed>>16)&255
            when=qpc/self.frequency
            stage=result.get('replay_stage',0);on=status&240==144 and velocity
            if status&240==176:
                # Ordered activity-control marker, not an original Note Off.
                # Native mode handling has already cleared its counters/audio;
                # replay updates presentation only and preserves Played history.
                if key not in (120,121,123,124,125,126,127) or velocity or count:
                    raise ValueError('midi_note_kernel invalid activity-clear action')
                if stage<1:
                    if key!=121:owner.midi_options.reset_notes(channel)
                    result['replay_stage']=1
                if stage<2:
                    owner.soundfont_activity.clear_channel(channel);result['replay_stage']=2
                if stage<3:
                    owner.midi_activity.clear(channel);result['replay_stage']=3
                index+=1;result['replay_index']=index;result['replay_stage']=0
                continue
            if stage<1:
                if count:owner.midi_options.active_notes[channel,key]=count
                else:owner.midi_options.active_notes.pop((channel,key),None)
                result['replay_stage']=1
            if stage<2:
                row=sources.get(channel)
                mapper=getattr(getattr(owner,'channel_native_filter',None),'function_map',None)
                adapters=tuple(getattr(mapper,'selection_adapters',{}).values())
                cache=result.setdefault('selection_source_cache',{})
                if index in cache:row=cache[index][0]
                else:
                    candidates=[];used=result.setdefault('selection_source_ordinals',{})
                    for adapter in adapters:
                        start=max(0,used.get(adapter,adapter.note_cursor)-adapter.note_base)
                        for offset in range(start,len(adapter.note_sources)):
                            serial,ch,source_packed,tick,identity=adapter.note_sources[offset]
                            if (ch,source_packed,tick)==(channel,packed,qpc):candidates.append((serial,adapter));break
                    if candidates:row=min(candidates,key=lambda value:value[0])[1].action_source(result,index,channel,packed,qpc,row)
                if on:
                    if row is not None:owner.soundfont_activity.note_on(channel,key,*row,velocity,now=when)
                else:
                    identity=row or (owner.soundfont_presets.bank(channel),owner.soundfont_presets.program[channel])
                    owner.soundfont_activity.note_off(channel,key,*identity,now=when)
                result['replay_stage']=2
            if stage<3:
                if on:owner.midi_activity.note_on(channel,key,velocity,now=when)
                else:owner.midi_activity.note_off(channel,key,now=when)
                result['replay_stage']=3
            index+=1;result['replay_index']=index;result['replay_stage']=0
        for adapter,cursor in result.get('selection_source_ordinals',{}).items():adapter.commit_note_sources(cursor)

    def close(self):
        with self.lock:
            if self.pointer:self.library.midi_note_kernel_destroy(self.pointer);self.pointer=None
            self.raw_context=None;self.activity=None;self.activity_authoritative=False


class _MidiRawSpan(ctypes.Structure):
    _fields_=[('offset',ctypes.c_uint),('length',ctypes.c_uint),('packed',ctypes.c_uint)]


class NativeRawPlan:
    """Whole interception plan; unchanged Python semantic actions stay ordered.

    This is NOT native submission or GIL independence. Pools belong to the
    engine's serialized parser, with separate leases on reentrant input. Every
    mask change/MIDI Learn capture and unexpected consumption regenerates from
    the current byte, never replays already executed state/audio side effects.
    """
    def __init__(self,library=None):
        self.library=None;self.scanner=None;self.pool=[];self.mask_key=None;self.masks=None
        try:
            if library is None:library=ctypes.CDLL(os.environ.get('ABMSYNTH_OBSERVATION_DLL',str(Path(__file__).with_name('abmsynth_dsp.dll'))))
            library.midi_raw_plan_version.argtypes=[];library.midi_raw_plan_version.restype=ctypes.c_uint
            if library.midi_raw_plan_version()!=1:return
            scanner=library.midi_raw_plan
            scanner.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint,
                ctypes.POINTER(_MidiRawSpan),ctypes.c_uint];scanner.restype=ctypes.c_int
            assert ctypes.sizeof(_MidiRawSpan)==12
            self.library=library;self.scanner=scanner
        except (OSError,AttributeError):pass # Exact complete old parser, no admission rejection.

    def _masks(self,learn,options,port):
        with learn.lock:
            key=(learn,learn.revision,options.cc6_direct_pitch_range,options.cc94_custom_delay_send,port)
            if key!=self.mask_key:
                masks=(ctypes.c_ubyte*(16*128))()
                fixed=(0,32,91,93,120,121,123,124,125,126,127)
                for channel in range(16):
                    for cc in fixed:masks[channel*128+cc]=1
                    if options.cc6_direct_pitch_range:masks[channel*128+6]=1
                    if options.cc94_custom_delay_send:masks[channel*128+94]=1
                for (channel,cc),targets in learn._dispatch.items():
                    if channel//16==port and targets:masks[(channel%16)*128+cc]=1
                self.mask_key=key;self.masks=masks
            return key,self.masks

    @contextmanager
    def packet(self,data,learn,options,port):
        # A small input still executes the original interpreter; crossover is
        # measured separately. Never reject oversized/incomplete/running status.
        if self.scanner is None or not 24<=len(data)<=1024*1024:
            yield None;return
        capacity=1<<(len(data)-1).bit_length()
        rows=next((item for item in self.pool if len(item)>=capacity),None)
        if rows is not None:self.pool.remove(rows)
        else:rows=(_MidiRawSpan*capacity)()
        cursor=_NativeRawCursor(self,data,rows,learn,options,port)
        try:yield cursor
        finally:self.pool.append(rows)


class _NativeRawCursor:
    def __init__(self,planner,data,rows,learn,options,port):
        self.planner,self.data,self.rows=planner,data,rows
        self.learn,self.options,self.port=learn,options,port
        self.buffer=(ctypes.c_ubyte*len(data)).from_buffer(data)
        self.key=None;self.count=0;self.index=0
        self.start=self.end=self.packed=0

    def _row(self):
        row=self.rows[self.index]
        self.start,self.end,self.packed=row.offset,row.offset+row.length,row.packed

    def next(self,offset):
        key=(self.learn,self.learn.revision,self.options.cc6_direct_pitch_range,self.options.cc94_custom_delay_send,self.port)
        # Capture changes dispatch within the same packet. Checking revision at
        # each semantic boundary keeps it, and explicit consumption divergence,
        # exact without asking a native speculative plan to mutate learn state.
        if key==self.key:
            while self.index<self.count and self.end<=offset:
                self.index+=1
                if self.index<self.count:self._row()
            if self.index<self.count:
                if self.start==offset or not self.packed and self.start<offset:
                    return self.end-offset,self.packed
        key,masks=self.planner._masks(self.learn,self.options,self.port)
        self.count=self.planner.scanner(ctypes.addressof(self.buffer),len(self.data),offset,masks,len(masks),self.rows,len(self.rows))
        if self.count<=0:raise RuntimeError(tr('runtime.raw_plan_failed',error='midi_raw_plan ABI/buffer'))
        self.key=key;self.index=0
        self._row();return self.end-offset,self.packed


class MidiTransformOptions:
    """Own velocity, overlap and GS-reset parser choices."""

    def __init__(self):
        self.velocity_gap = 100
        self.humanize_timing_ms=0
        self.humanize_velocity=0
        self.humanize_preserve_simultaneity=False
        self.velocity_table = tuple(range(128))
        self.overnote_enabled = False
        self.gs_reset_enabled = True
        self.gm1_reset_enabled = True
        self.gm2_reset_enabled = True
        self.xg_reset_enabled = True
        self.cc6_direct_pitch_range = True
        self.cc94_custom_delay_send = True
        self.active_notes: dict[tuple[int, int], int] = {}
        self.mixer_mutes=frozenset();self.mixer_solos=frozenset();self.mixer_muted=frozenset()
        self.passive_scanner=None;self._scanner_library=None
        # Candidate grammar is exact, but a Python per-span bridge regresses
        # dense Note/CC workloads. Never auto-enable it merely because a DLL
        # exports the ABI. The native submission worker must consume the plan
        # without this bridge before it becomes a production path.
        self.raw_planner=None
        try:
            library=ctypes.CDLL(os.environ.get('ABMSYNTH_OBSERVATION_DLL',str(Path(__file__).with_name('abmsynth_dsp.dll'))))
            scanner=library.midi_passive_packet
            scanner.argtypes=[ctypes.c_void_p,ctypes.c_uint];scanner.restype=ctypes.c_int
            self._scanner_library=library;self.passive_scanner=scanner
        except (OSError,AttributeError):pass # Original complete parser, no lost events.

    def passive_packet(self,data):
        #24byte is an acceleration crossover, NOT an admission/range limit.
        # Native qualification and BASS borrow this existing mutable buffer only
        # during each call. No frame merge, delay, MIDI state or data mutation.
        if self.passive_scanner is None or not 24<=len(data)<=0xffffffff or data[0]&0xf0 not in (0xd0,0xe0):return False
        buffer=(ctypes.c_ubyte*len(data)).from_buffer(data)
        return bool(self.passive_scanner(ctypes.c_void_p(ctypes.addressof(buffer)),len(data)))

    def set_velocity(self, value):
        self.velocity_gap = max(0, min(200, int(round(float(value)))))
        # Source 0046D424 builds a 128-value table when the control changes.
        # Publish one immutable replacement for the MIDI callback to index.
        self.velocity_table = (
            tuple(range(128)) if self.velocity_gap == 100 else
            tuple(bmsynth_delay_curve(self.velocity_gap, velocity) for velocity in range(128))
        )

    def velocity(self, value):
        return self.velocity_table[value]

    def note_on(self, channel, note):
        key = (channel, note)
        previous = self.active_notes.get(key, 0)
        self.active_notes[key] = previous + 1
        return self.overnote_enabled and previous > 0

    def note_off(self, channel, note):
        key = (channel, note)
        remaining = max(0, self.active_notes.get(key, 0) - 1)
        if remaining:
            self.active_notes[key] = remaining
        else:
            self.active_notes.pop(key, None)
        # BMSynth suppresses the early Note Off only while another matching
        # Note On is still outstanding (00476F4F..00476FAA).
        return self.overnote_enabled and remaining > 0

    def reset_notes(self, channel: int | None = None):
        if channel is None:
            self.active_notes.clear()
        else:
            for key in tuple(self.active_notes):
                if key[0] == channel:
                    self.active_notes.pop(key, None)


def _native_first_use_requests(owner):
    """Prepared legacy address coverage, including Reset's failure prefixes.

    This prepares routes without selecting presets or loading samples. The
    legacy loading hook consumes Bank MSB/drum-bank and Program, not LSB. The
    separate tone/source resolver retains its original LSB-aware behavior.
    Future address coverage is prepared by the domain ABI, not this inventory.
    """
    with owner.channel_policy.lock:defaults=dict(owner.channel_policy.reset_defaults)
    requests={}
    for channel in owner.addresses.channels():
        presets=owner.soundfont_presets
        current_bank=presets.bank(channel);current_program=presets.program[channel]
        factory_bank=128 if channel%16==9 else 0
        # set_channel_instrument may stop after DRUMS/BANK/LSB/Program. Include
        # the raw melodic bank as well as the effective drum-bank identity.
        banks=dict.fromkeys((current_bank,presets.bank_msb[channel],factory_bank,
                             defaults.get((channel,'bank'),current_bank),
                             defaults.get((channel,'bank'),factory_bank)))
        programs=dict.fromkeys((current_program,0,defaults.get((channel,'program'),current_program),
                                defaults.get((channel,'program'),0)))
        for bank in banks:
            for program in programs:requests[bank,program]=None
    return tuple(requests)


def _prepare_native_first_use_plan(source_runtime,requests,library,stream_generation):
    """Initial and successor generations share full-domain ABI admission.

    New address-domain ABI prepares every allowed Bank/Program route without
    loading samples. Older images retain their explicit limited inventory;
    DLL presence alone still does not activate this candidate in the product.
    """
    version=getattr(library,'font_preload_plan_address_domain_version',None)
    kwargs=dict(stream_generation=stream_generation,capacity=1)
    if version is not None:
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:raise ValueError('Native first-use address domain ABI')
        kwargs['all_addresses']=True
    return source_runtime.prepare_native_first_use_plan(requests,library,**kwargs)


def _native_first_use_rows(owner,plan,library,*,asynchronous):
    """One initial/successor request policy, separate from resolved tone state.

    Request ABI2 loads the same legacy MSB/Program key for every LSB and
    preserves deferred RAW timing. Older explicit candidates retain their
    original LSB0/synchronous admission; no DLL-presence assumption.
    """
    version=getattr(library,'font_preload_plan_request_version',None)
    expanded=False
    if version is not None:
        version.argtypes=[];version.restype=ctypes.c_uint
        value=version()
        if value not in (1,2):raise ValueError('Native first-use request ABI')
        expanded=value==2
    presets=owner.soundfont_presets
    return tuple((presets.bank(channel),presets.program[channel],presets.bank_lsb[channel],
        7 if channel<owner.addresses.count and plan.current and
        (expanded or (not presets.bank_lsb[channel] and not asynchronous)) else 0)
        for channel in range(128))


def prepare_live_native_session(runtime,source_runtime):
    """Normal-launch composition, with an explicit complete-image ABI gate.

    Older installed images keep the original engine and explain that choice.
    Once the matching image is installed, preparation failures are real
    startup errors, not a silent switch back to GUI-bound MIDI processing.
    """
    owner=runtime.engine.runtime
    library=owner.channel_sends.native_domain.library
    required=(('raw_decode_context_selection_version',2),
        ('font_preload_plan_request_version',2),('font_preload_plan_address_domain_version',1),
        ('midi_execution_multi_version',1),('midi_execution_raw_version',1),
        ('midi_execution_stream_effect_version',1),('midi_execution_reset_version',1),('midi_execution_system_mode_version',3),
        ('midi_execution_reset_policy2_version',1),
        ('midi_cc_kernel_manual_reset_version',1),('midi_execution_source_edit_version',1),
        ('midi_execution_idle_version',1),
        ('native_activity_version',1),('native_activity_transfer_version',1),('native_activity_view_version',1),
        ('native_activity_view_quality_version',1),('native_activity_view_prepare_version',1),
        ('native_activity_channel_visual_version',1),
        ('midi_note_kernel_activity_seed_version',1),('midi_execution_activity_control_version',1),
        ('midi_note_kernel_activity_authority_version',1),
        ('midi_execution_activity_restore_version',1),('midi_execution_activity_presentation_version',1),
        ('midi_execution_trace_version',1))
    if library is None:
        owner.append_log(log_text('runtime.native_session_legacy',reason='native DLL'))
        return None
    for name,expected in required:
        version=getattr(library,name,None)
        if version is None:
            owner.append_log(log_text('runtime.native_session_legacy',reason=name))
            return None
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=expected:
            owner.append_log(log_text('runtime.native_session_legacy',reason=name))
            return None
    # Recovery must inspect the native lease, not infer an empty lock set from
    # Python bookkeeping. A partially upgraded image is not product admission.
    for name in ('midi_selection_state_unlock_owner','midi_execution_trace_enable',
            'midi_execution_trace_finish','midi_execution_idle_configure','midi_execution_idle_enter',
            'midi_execution_idle_snapshot'):
        if getattr(library,name,None) is None:
            owner.append_log(log_text('runtime.native_session_legacy',reason=name))
            return None
    return runtime.prepare_native_session(source_runtime,full_selection=True,decode_context=True,
        activity_observation=True,activity_authority=True)


class MidiInputRuntime:
    """Own input-listener lifetime and the application's manual reset path."""

    def __init__(self, engine, bassmidi, stream_getter, server_factory=WinMMMidiPipeServer):
        self.engine = engine
        self.bassmidi = bassmidi
        self.stream_getter = stream_getter
        self.server_factory = server_factory
        self.server = None
        self.shutdown_pending=False
        self.trace: MidiTraceRecorder | None = None
        self.native_factory=None
        from settings import load_midi_ports
        self.port_settings=load_midi_ports();self.port_router=None
        self._native_preparation=None
        from midi_recording import MidiPerformanceRecorder
        self.midi_recorder = MidiPerformanceRecorder(self)

    def prepare_native_session(self,source_runtime,*,full_selection=True,decode_context=False,activity_observation=False,activity_authority=False):
        """Prepare the real controller generation for explicit candidate use.

        This does not change product defaults or infer eligibility from DLL
        presence. Every native borrower uses the runtime's existing image;
        partial construction remains owned until an explicit retirement.
        """
        if (type(full_selection) is not bool or type(decode_context) is not bool or type(activity_observation) is not bool or
                type(activity_authority) is not bool or (activity_authority and not activity_observation) or self.server is not None or self.native_factory is not None or
                self._native_preparation is not None or self.shutdown_pending):raise ValueError('native preparation admission')
        owner=self.engine.runtime
        if not owner.stream_handle or owner.diagnostic_trace is not None:raise ValueError('native preparation generation')
        roots=self._native_preparation=dict(kernel=None,notes=None,plan=None,execution=None,activity=None,presentation=None,activity_seeded=False,
            raw_planner=owner.midi_options.raw_planner,mapper=None,full_selection=None)
        with owner.activate(),owner.midi_lock:
            owner.ensure_event_filter();gate=owner.channel_native_filter;mapper=getattr(gate,'function_map',None)
            if mapper is None or mapper.library is None:raise ValueError('native preparation filter ABI')
            roots.update(mapper=mapper,full_selection=mapper.full_selection_candidate)
            if decode_context:
                if owner.raw_decode_context is None and owner.raw_submission_seen:raise ValueError('native preparation requires fresh RAW stream')
                context=owner.prepare_raw_decode_context(mapper.library,fresh=True)
                if context is None:raise ValueError('native preparation RAW decoder ABI')
                if not context.statistics().get('qualified'):raise ValueError('native preparation unknown RAW decoder state')
                gate.signature=None;gate.checked_key=None
            if full_selection:
                mapper.full_selection_candidate=True;gate.signature=None;gate.checked_key=None
            if full_selection or decode_context:owner.ensure_event_filter()
            notification=getattr(mapper.library,'midi_execution_failure_notification_version',None)
            if notification is not None:notification.argtypes=[];notification.restype=ctypes.c_uint
            native_log=(notification is not None and notification()==1 and
                type(owner.performance.failure_sink) is DeferredPerformanceFailureLog and owner.performance.failure_sink.owner is owner)
            kernel=roots['kernel']=NativeCCKernel(self.bassmidi,source_runtime.bass,owner.channel_sends.native_domain,path=mapper.library._name,
                semantic_failure_capacity=65536 if native_log else None)
            notes=roots['notes']=NativeNoteKernel(self.bassmidi,source_runtime.bass,128,mapper.library._name)
            if not kernel.pointer or not notes.pointer or not kernel.configure(owner):raise ValueError('native preparation kernel ABI')
            requests=_native_first_use_requests(owner)
            plan=roots['plan']=_prepare_native_first_use_plan(source_runtime,requests,kernel.library,owner.stream_generation)
            flags=int(source_runtime.bass.BASS_ChannelFlags(owner.stream_handle,0,0))
            if flags==0xffffffff:raise ValueError('native preparation stream flags')
            rows=_native_first_use_rows(owner,plan,kernel.library,asynchronous=bool(flags&0x400000))
            if not kernel.attach_first_use_plan(plan,rows) or not kernel.attach_notes(owner,notes):raise ValueError('native preparation source identity')
            if decode_context and (not kernel.attach_raw_context(owner.raw_decode_context) or
                    not notes.attach_raw_context(owner.raw_decode_context)):raise ValueError('native preparation RAW decoder owner')
            if hasattr(kernel.library,'midi_cc_kernel_reset_tail_version'):
                reset_ready=kernel.prepare_compound_reset_controls(owner)
            else:reset_ready=kernel.prepare_reset_controls(owner)
            if not reset_ready or not kernel.prepare_manual_controls(owner):raise ValueError('native preparation controls ABI')
            if hasattr(kernel.library,'midi_cc_kernel_system_reset_version') and not kernel.prepare_full_reset(owner):
                raise ValueError('native preparation full reset ABI')
            mode_reset=getattr(kernel.library,'midi_cc_kernel_system_mode_reset_version',None)
            if mode_reset is None:raise ValueError('native preparation system mode reset ABI')
            mode_reset.argtypes=[];mode_reset.restype=ctypes.c_uint
            if mode_reset()!=1:raise ValueError('native preparation system mode reset ABI')
            if hasattr(kernel.library,'midi_cc_kernel_manual_reset_version') and not kernel.prepare_manual_reset(owner):
                raise ValueError('native preparation manual reset ABI')
            if hasattr(kernel.library,'midi_cc_kernel_file_reset_version') and not kernel.prepare_file_reset(owner):
                raise ValueError('native preparation file Reset ABI')
            if native_log and not kernel.prepare_failure_notification(owner):raise ValueError(tr('runtime.failure_notification_prepare_failed'))
            if activity_observation:
                if owner.native_activity is not None or owner.activity_presentation is not None:raise ValueError('native activity generation already bound')
                activity=roots['activity']=NativeMidiActivity(kernel.library._name,channels=notes.channels)
                image=activity.capture_legacy_state(owner)
                if activity.import_state(image)!=1:raise ValueError('native activity initial image')
                roots['activity_seeded']=True
                prepare=activity.library.native_activity_view_prepare
                prepare.argtypes=[ctypes.c_void_p,ctypes.c_uint];prepare.restype=ctypes.c_int
                if not prepare(activity.pointer,max(256,len(image['presets']))):raise ValueError('native activity presentation preparation')
                sources={ch:owner.soundfont_presets.preset_key(ch) for ch in range(notes.channels)}
                if not notes.attach_activity(activity,sources,seeded=True):raise ValueError('native activity initial attachment')
                now=kernel.library.playback_timing_now;now.argtypes=[];now.restype=ctypes.c_uint64
                if activity.publish_presentation(int(now()))!=1:raise ValueError('native activity initial publication')
                roots['presentation']=NativeActivityPresentation(activity)
                if activity_authority and not notes.set_activity_authority(True):raise ValueError('native activity authority admission')
            planner=NativeRawPlan(kernel.library)
            if planner.scanner is None:raise ValueError('native preparation raw ABI')
            owner.midi_options.raw_planner=planner
            timing=owner.playback_timing if owner.playback_timing.pointer else None
            execution=roots['execution']=NativeMidiExecution(None,kernel,multi=True,timing=timing)
            if hasattr(kernel.library,'midi_execution_retained_records_version'):
                execution.prepare_retained_records()
            execution.prepared_roots.append(plan)
            return NativeMidiClientFactory(execution,self._receive_midi,source_runtime=source_runtime,full_selection=full_selection,
                on_error=lambda error:owner.append_log(log_text('midi_pipe.callback_failed',error=error)))

    def bind_native_factory(self,factory):
        """Explicit composition admission, never DLL-presence autoactivation.

        Caller prepares the entire source/filter/kernel generation first.
        This owner retains it through server stop and resource retirement.
        """
        owner=self.engine.runtime;execution=factory.execution
        if (self.server is not None or self.native_factory is not None or self.shutdown_pending or
                execution.owner is not owner or not execution.pointer or factory.on_midi!=self._receive_midi):
            raise ValueError('native MIDI session admission')
        if owner.channel_command_executor not in (None,execution):raise ValueError('native MIDI command owner')
        self.native_factory=factory
        if owner.channel_command_executor is None:owner.attach_channel_executor(execution)
        roots=self._native_preparation
        if roots is not None and roots.get('activity') is not None:
            if owner.native_activity not in (None,roots['activity']) or owner.activity_presentation not in (None,roots['presentation']):
                raise ValueError('native activity observation generation')
            owner.native_activity=roots['activity'];owner.activity_presentation=roots['presentation']
        if not factory.start():raise RuntimeError(tr('runtime.midi_executor_start_failed'))

    def retire_native_session(self):
        """After input stop, before FontFree/stream/output teardown."""
        factory=self.native_factory
        if factory is None:
            roots=self._native_preparation
            if roots is None:return True
            execution=roots['execution']
            if execution is not None and not execution.close():return False
            if roots['kernel'] is not None:roots['kernel'].close()
            if roots['notes'] is not None:roots['notes'].close()
            plan=roots['plan']
            if plan is not None and plan.pointer:
                plan.cancel(reason='retirement')
                if not plan.close():return False
                with plan._runtime._preparation_lock:
                    if plan in plan._runtime._native_first_use_plans:plan._runtime._native_first_use_plans.remove(plan)
            if not self._restore_native_preparation():return False
            return True
        if self.server is not None:return False
        if not factory.execution.owner.settle_channel_commands(timeout=5,stop_admission=True):return False
        if not factory.retire_owner():self.shutdown_pending=True;return False
        if not self._restore_native_preparation():self.shutdown_pending=True;return False
        self.native_factory=None;self.shutdown_pending=False
        return True

    def _restore_native_preparation(self):
        roots=self._native_preparation
        if roots is None:return True
        owner=self.engine.runtime
        presentation=roots.get('presentation');activity=roots.get('activity')
        if presentation is not None:
            if not presentation.close():return False
            if owner.activity_presentation is presentation:owner.activity_presentation=None
        if activity is not None:
            # Execution has joined and detached first. Preserve final complete
            # state for legacy callbacks/recreation before releasing this root.
            if activity.pointer:
                if roots.get('activity_seeded'):activity.restore_legacy_state(owner)
                if not activity.close():return False
            if owner.native_activity is activity:owner.native_activity=None
        owner.midi_options.raw_planner=roots['raw_planner']
        mapper=roots['mapper']
        if mapper is not None:mapper.full_selection_candidate=roots['full_selection']
        gate=owner.channel_native_filter
        if gate is not None:gate.signature=None;gate.checked_key=None
        self._native_preparation=None
        return True

    def _receive_midi(self, payload: bytes, driver_sequence: int | None = None):
        capture=getattr(self.engine,"capture_midi_received_time",None)
        native_clock=getattr(self.server,"received_timestamp",WinMMMidiPipeServer.received_timestamp)
        received=native_clock() if callable(native_clock) else None
        if type(received) is not int or received<=0:received=None
        origin_reader=getattr(self.server,"received_origin",WinMMMidiPipeServer.received_origin)
        origin=origin_reader() if callable(origin_reader) else None
        if not isinstance(origin,dict):origin={"quality":"legacy","read_qpc":received}
        if origin.get("quality")=="driver":received=origin["ingress_qpc"]
        if received is None:received=capture() if callable(capture) else None
        if type(received) is not int or received<=0:received=None
        trace = self.trace
        packet_id = trace.next_packet_id() if trace is not None else None
        if trace is not None:
            trace.record("pipe_midi", packet_id=packet_id, driver_sequence=driver_sequence,
                         native_frame_completed_qpc=origin.get("read_qpc"), driver_origin=origin,
                         **trace_midi_payload(payload))
        kwargs={"received_stamp":received} if received is not None else {}
        port_reader=getattr(self.server,"received_port",WinMMMidiPipeServer.received_port)
        port=port_reader() if callable(port_reader) else None
        if port is not None:
            if type(port) is not int or not 0<=port<MIDI_PORT_LIMIT:raise ValueError('MIDI input port')
            if port:kwargs['port']=port
        kwargs["received_origin"]=origin
        return self.engine.forward_midi_bytes(payload, trace_packet_id=packet_id,**kwargs)

    def start(self) -> bool:
        if self.shutdown_pending:return False
        if self.server is None:
            options={'native_client_factory':self.native_factory} if self.native_factory is not None else {}
            self.server = self.server_factory(self._receive_midi, self.engine.append_log,**options)
        ready=bool(self.server.start())
        if ready and self.native_factory is not None and self.port_router is None:
            self.port_router=NativeMidiPorts(self.native_factory.execution,self.port_settings,self.engine.append_log)
            self.port_router.start()
        return ready

    def stop(self) -> bool:
        # Trace tail belongs to the still-live metadata consumer. Retiring the
        # server/factory first would strand an admitted observation fence.
        if self.port_router is not None:
            if self.port_router.close() is False:return False
            self.port_router=None
        self.midi_recorder.source_closed()
        self.stop_trace()
        if self.server is not None:
            # ExplicitFalse retains the server/pipe/native retry owner. Old
            # injected server factories returningNone keep their prior API.
            if self.server.stop() is False:
                self.shutdown_pending=True;return False
            self.server = None
        self.shutdown_pending=False
        self.stop_trace()
        return True

    def start_trace(self, path: Path, *, mapping_name: str = _TRACE_MAPPING_NAME) -> MidiTraceRecorder:
        if self.trace is not None:
            return self.trace
        trace = MidiTraceRecorder(path, mapping_name=mapping_name)
        try:
            if self.native_factory is not None:self.native_factory.start_native_trace(trace)
        except BaseException:trace.stop();raise
        self.trace = trace
        if hasattr(self.engine, "runtime"):
            self.engine.runtime.diagnostic_trace = trace
        return trace

    def poll_trace(self) -> None:
        if self.trace is not None:
            self.trace.poll_driver()

    def stop_trace(self) -> dict | None:
        trace = self.trace
        if trace is None:
            return None
        if self.native_factory is not None:self.native_factory.stop_native_trace()
        self.trace = None
        if hasattr(self.engine, "runtime"):
            self.engine.runtime.diagnostic_trace = None
        return trace.stop()

    def reset(self) -> None:
        # Enter the native FIFO fence BEFORE any legacy MIDI mutex/state
        # writer. Generic/source actors already own a scope and do not wait
        # recursively. Reconciliation also restores native overlap counters.
        owner=getattr(self.engine,'runtime',None)
        with owner.reset_contract_scope() if owner is not None else nullcontext():
            self._reset_fenced()

    def _reset_fenced(self) -> None:
        stream = self.stream_getter()
        sensor=getattr(getattr(self.engine,"runtime",None),"playback_timing",None)
        if sensor is not None:sensor.reset_notes()
        if hasattr(self.engine, "runtime"):
            self.engine.runtime.midi_options.reset_notes()
            self.engine.runtime.midi_activity.clear()
            self.engine.runtime.midi_learn.cancel()
            self.engine.runtime.auxiliary_cc.reset()
            # Channel expression is a live MIDI state. Do not reapply a saved
            # override after the Reset event has just cleared the channel.
            self.engine.runtime.performance.reset_expression(apply=False)
            self.engine.runtime.performance.preset_seeds.clear()
            self.engine.runtime.channel_policy.clear_session()
            self.engine.reset_soundfont_group_channel_state()
        # bassmidi.h MIDI_EVENT_SOUNDOFF/RESET/NOTESOFF (16/17/18).
        # Keep the original per-channel event order; unlike an effect preset,
        # these are MIDI transport commands and are not folded into a macro.
        for channel in self.engine.runtime.addresses.channels():
            for event in (MIDI_EVENT_SOUNDOFF, MIDI_EVENT_RESET, MIDI_EVENT_NOTESOFF):
                accepted=self.bassmidi.BASS_MIDI_StreamEvent(stream, channel, event, 0)
                owner=self.engine.runtime
                if event==MIDI_EVENT_RESET and getattr(owner,'raw_decode_context',None) is not None:
                    code=0 if accepted else int(owner.performance.bass.BASS_ErrorGetCode())
                    owner.observe_midi_event(stream,channel,event,0,bool(accepted),code)
            if hasattr(self.engine, "runtime"):
                self.engine.runtime.performance.apply_expression_defaults(channel)
            self.engine.set_channel_instrument(channel, 128 if channel % 16 == 9 else 0, 0, bank_lsb=0)
        self.engine.reset_channel_effect_sends()
        self.engine.reset_channel_monitor_mix()
        self.engine.reset_delay_preset_for_midi_reset()
        if hasattr(self.engine, "runtime"):
            # Master/scale/drum adjustments belong to separate settings
            # surfaces. Only the channel editor's values are cleared here.
            self.engine.runtime.performance.apply_all()
            self.engine.apply_channel_reset_defaults()

