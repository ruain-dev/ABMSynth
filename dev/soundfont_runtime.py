"""SoundFont lifetime and mapping service for the active BASSMIDI stream.

This module deliberately has no Qt dependency.  It owns the handles belonging
to the current stream generation, reuses compatible handles when a group is
edited, and applies the same BASSMIDI font-loading policy for every caller
(startup, the group dialog, and the native MIDI callback).
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
import unicodedata
from collections import deque
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from bass_bindings import (
    BASS_MIDI_FONT, BASS_MIDI_FONTEX, BASS_MIDI_FONT_EX,
    BASS_MIDI_FONT_MINFX, BASS_MIDI_FONT_NOFX, BASS_MIDI_FONT_STEREO,
    BASS_MIDI_FONTINFO,
    BASS_MIDI_FONTLOAD_NOWAIT,
    BASS_CONFIG_MIDI_SAMPLELOADING,
    BASS_UNICODE,
    BASS_ATTRIB_MIDI_CHANS, MIDI_EVENT_ATTACK, MIDI_EVENT_CUTOFF, MIDI_EVENT_DECAY,
    MIDI_EVENT_MIXLEVEL, MIDI_EVENT_RELEASE, MIDI_EVENT_RESONANCE,
)
from i18n import tr
from midi_input_runtime import MIDI_CHANNEL_LIMIT, midi_activity_strength_at
from soundfont_files import INSTRUMENT_VOLUME_MAX, effective_preset_expression, PresetOverride, SoundFontEntry, SoundFontGroup, validate_group
from soundfont_files import asset_file_identity, soundfont_asset_identity, stage_sfz_asset,inspect_soundfont_pitch


class SoundFontRuntimeError(RuntimeError):
    """A user-actionable failure while changing the active font group."""


SOUNDFONT_FX_FLAGS = {"sum": 0, "minimum": BASS_MIDI_FONT_MINFX,
                      "midi_only": BASS_MIDI_FONT_NOFX}
SOUNDFONT_FX_MASK = BASS_MIDI_FONT_MINFX | BASS_MIDI_FONT_NOFX


class SoundFontLoadCancelled(SoundFontRuntimeError):
    """A staged load stopped before its new mapping was committed."""


def _decodes_cleanly(value, encoding):
    try:
        value.decode(encoding)
        return True
    except UnicodeDecodeError:
        return False


@dataclass(frozen=True)
class SoundFontLoadResult:
    font_count: int
    explicit_mapping_count: int


@dataclass(frozen=True)
class SoundFontPreloadRequest:
    bank: int
    preset: int
    bank_lsb: int = 0


@dataclass(frozen=True)
class SoundFontPreloadTarget:
    font: int
    bank: int
    preset: int


@dataclass(frozen=True)
class SoundFontPreloadRoute:
    request: SoundFontPreloadRequest
    candidates: tuple[SoundFontPreloadTarget, ...]
    disabled: bool


@dataclass(frozen=True)
class SoundFontPreloadManifest:
    """Immutable candidate evidence, not a native ownership transfer.

    mapping is the exact ordered BASS_MIDI_FONT array. Routes cover possible
    bank/explicit-first-preset fallbacks; BASS still selects the actual source.
    The manifest expires when its owning preparation job is retired.
    """
    generation: int
    source_generation: int
    stream: int
    mapping: tuple[tuple[int, int, int], ...]
    routes: tuple[SoundFontPreloadRoute, ...]
    length: int
    units: str
    prepared: tuple[SoundFontPreloadTarget, ...]
    failures: tuple[tuple[SoundFontPreloadTarget, int], ...]

    @property
    def ready(self):
        """All planned candidates prepared; not a universal native resolver."""
        return not self.failures and all(route.candidates for route in self.routes)


class SoundFontPreparationJob:
    """Explicit opt-in preparation on an owned, cancellable I/O worker.

    NOWAIT still parses SFZ on its caller, which is this worker. An indivisible
    native call cannot be interrupted: retirement joins it before FontFree.
    This Python candidate is not GUI/GIL-independent MIDI execution.
    """
    def __init__(self, runtime, requests, length, units):
        self._runtime = runtime
        self.requests, self.length, self.units = requests, length, units
        self.generation = runtime._preparation_generation
        self.source_generation = runtime.source_generation
        self.stream = int(runtime.stream_handle())
        self.handles = tuple(runtime.handles)
        self._extended_mapping=tuple(runtime.group.mappings)
        effective = runtime.effective_presets(runtime.group)
        self._mapping = tuple((self.handles[item.index], item.preset, item.bank) for item in effective
                              if item.index is not None and not item.disabled)
        self._mapping += tuple((handle, -1, 0) for handle in self.handles)
        self._disabled = frozenset((item.bank, item.preset) for item in effective if item.disabled)
        self.cancel_requested = threading.Event()
        self.done = threading.Event()
        self._result = self._error = None
        self._thread = threading.Thread(target=self._run, name="ABMSynth-font-prepare", daemon=False)

    @property
    def current(self):
        return (not self.cancel_requested.is_set() and
                self.generation == self._runtime._preparation_generation)

    def cancel(self):
        self.cancel_requested.set()

    def join(self, timeout=None):
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def result(self, timeout=None):
        if not self.done.wait(timeout):
            raise TimeoutError("SoundFont preparation")
        if not self.current:
            raise SoundFontLoadCancelled()
        if self._error is not None:
            raise self._error
        return self._result

    def _check_cancel(self):
        if self.cancel_requested.is_set():
            raise SoundFontLoadCancelled()

    def _run(self):
        try:
            self._result = self._runtime._prepare_first_notes(self)
        except BaseException as error:
            self._error = error
        finally:
            self.done.set()


class _NativePreloadTarget(ctypes.Structure):
    _fields_ = [('font',ctypes.c_uint),('preset',ctypes.c_int),('bank',ctypes.c_int),
                ('length',ctypes.c_uint),('flags',ctypes.c_uint),('method',ctypes.c_uint)]


class _NativePreloadResult(ctypes.Structure):
    _fields_ = [('state',ctypes.c_uint),('error',ctypes.c_uint),('serial',ctypes.c_uint64),
                ('begin_qpc',ctypes.c_uint64),('end_qpc',ctypes.c_uint64)]


class NativeSoundFontPreparationQueue:
    """Opt-in owner of native I/O queue and its borrowed handle/DLL lifetimes.

    Native results distinguish unknown/queued/running/full-ready/partial-ready/
    failed/cancelled. No readiness here authorizes removing a legacy Note hook.
    Control barriers must detach additional native borrowers before close.
    """
    def __init__(self, runtime, job, library, *, length, units, capacity, paused):
        manifest = job.result()
        if job._runtime is not runtime or not job.current:
            raise ValueError('SoundFont preparation generation')
        if isinstance(length,bool) or not isinstance(length,int) or not 0<=length<=0xffffffff:
            raise ValueError('SoundFont preparation length')
        if units not in ('frames','milliseconds'):
            raise ValueError('SoundFont preparation units')
        if isinstance(capacity,bool) or not isinstance(capacity,int) or not 1<=capacity<=65536:
            raise ValueError('SoundFont preparation capacity')
        self.library=library
        # Explicit keepalive for native BASS/BASSMIDI code and source owner.
        self.bass,self.bassmidi,self._runtime,self.job=runtime.bass,runtime.bassmidi,runtime,job
        self.generation=manifest.generation
        self.targets=tuple(dict.fromkeys(item for route in manifest.routes for item in route.candidates))
        self.handles=tuple(dict.fromkeys(item.font for item in self.targets))
        if not self.targets:
            raise ValueError('SoundFont preparation has no source targets')
        signatures={
            'version':([],ctypes.c_uint),
            'create':([ctypes.POINTER(_NativePreloadTarget),ctypes.c_uint,ctypes.c_uint64,ctypes.c_uint,
                       ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_int],ctypes.c_void_p),
            'submit':([ctypes.c_void_p,ctypes.c_uint64,ctypes.c_uint,ctypes.c_uint],ctypes.c_int),
            'resume':([ctypes.c_void_p],ctypes.c_int),
            'wait_idle':([ctypes.c_void_p,ctypes.c_uint,ctypes.c_int,ctypes.POINTER(ctypes.c_uint64)],ctypes.c_int),
            'result':([ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(_NativePreloadResult)],ctypes.c_int),
            'status':([ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64)],ctypes.c_int),
            'cancel':([ctypes.c_void_p],ctypes.c_int),'join':([ctypes.c_void_p,ctypes.c_uint],ctypes.c_int),
            'retain':([ctypes.c_void_p],ctypes.c_uint),'release':([ctypes.c_void_p],ctypes.c_uint),
            'destroy':([ctypes.c_void_p],ctypes.c_int),'live_count':([],ctypes.c_uint)}
        for name,(args,result) in signatures.items():
            function=getattr(library,'font_preload_queue_'+name)
            function.argtypes=args;function.restype=result
        if library.font_preload_queue_version()!=1:
            raise RuntimeError('SoundFont preparation queue ABI')
        rows=(_NativePreloadTarget*len(self.targets))(*(
            _NativePreloadTarget(item.font,item.preset,item.bank,length,
                8 if length and units=='milliseconds' else 0,1 if length else 0)
            for item in self.targets))
        addresses=[ctypes.cast(function,ctypes.c_void_p) for function in (
            self.bassmidi.BASS_MIDI_FontLoad,self.bassmidi.BASS_MIDI_FontLoadEx,
            self.bass.BASS_ErrorGetCode,self.bass.BASS_GetConfig)]
        self.pointer=library.font_preload_queue_create(rows,len(rows),self.generation,capacity,*addresses,int(paused))
        if not self.pointer:
            raise RuntimeError('SoundFont preparation queue creation')

    @property
    def current(self):
        return bool(self.pointer and self.job.current)

    def submit(self, slot, *, timeout_ms=0):
        if not self.current:
            return -1
        return self.library.font_preload_queue_submit(self.pointer,self.generation,slot,timeout_ms)

    def status(self):
        values=(ctypes.c_uint64*12)()
        if not self.pointer or not self.library.font_preload_queue_status(self.pointer,values):
            raise RuntimeError('SoundFont preparation queue status')
        return tuple(values)

    def result(self, slot):
        result=_NativePreloadResult()
        if not self.pointer or not self.library.font_preload_queue_result(self.pointer,slot,ctypes.byref(result)):
            raise ValueError('SoundFont preparation queue target')
        return result

    def cancel(self):
        return not self.pointer or bool(self.library.font_preload_queue_cancel(self.pointer))

    def close(self, *, timeout_ms=5000):
        if not self.pointer:
            return True
        self.cancel()
        if not self.library.font_preload_queue_join(self.pointer,timeout_ms):
            return False
        if not self.library.font_preload_queue_destroy(self.pointer):
            return False
        self.pointer=None
        return True


class _NativePreloadAttempt(ctypes.Structure):
    _fields_=[('slot',ctypes.c_uint),('alias_slot',ctypes.c_uint)]


class _NativePreloadRoute(ctypes.Structure):
    _fields_=[('bank',ctypes.c_uint),('preset',ctypes.c_uint),('first',ctypes.c_uint),('count',ctypes.c_uint)]


class NativeSoundFontFirstUsePlan:
    """Native full first-use consumer; immutable legacy attempts, not resolver.

    No new Note path admission: applied-selection/system/LSB qualification and
    exact FIFO suspend/restart belong to the native execution integration.
    Source planning occurs here on a control thread, never before_note.
    """
    def __init__(self,runtime,requests,library,stream_generation,capacity):
        self._lifetime=threading.RLock();self._calls=0
        self.cancel_reason=None
        self._runtime,self.library,self.bass,self.bassmidi=runtime,library,runtime.bass,runtime.bassmidi
        self.epoch,self.stream_generation,self.stream=runtime._preparation_generation,stream_generation,int(runtime.stream_handle())
        self.mode=int(runtime.engine_preferences().loading_mode)
        self.handles=tuple(runtime.handles)
        self.source_generation=runtime.source_generation
        self.targets=[];routes=[];attempts=[];aliases=[];chains={};singleton_sources={}
        mappings=runtime.group.preset_map()
        full=set(runtime.preloaded_used or ())
        indices={}
        def target(handle,bank,preset):
            key=(handle,bank,preset)
            if key not in indices:indices[key]=len(self.targets);self.targets.append(SoundFontPreloadTarget(*key))
            return indices[key]
        for request in dict.fromkeys(requests):
            if not isinstance(request,SoundFontPreloadRequest):request=SoundFontPreloadRequest(*request)
            if not 0<=request.bank<=128 or not 0<=request.preset<128 or request.bank_lsb:
                raise ValueError('Native first-use request requires qualified legacy bank/Program/LSB0')
            if self.mode!=1:continue
            mapping=mappings.get((request.bank,request.preset))
            explicit=mapping is not None and mapping.index is not None and not mapping.disabled
            candidates=[mapping.index] if explicit else range(len(self.handles))
            first=len(attempts)
            for index in candidates:
                if index is None or not 0<=index<len(self.handles):continue
                handle=self.handles[index]
                primary=target(handle,request.bank,request.preset);alias_slot=0xffffffff
                if explicit:
                    if handle not in singleton_sources:
                        actual=None;info=BASS_MIDI_FONTINFO()
                        if self.bassmidi.BASS_MIDI_FontGetInfo(handle,ctypes.byref(info)) and info.presets==1:
                            packed=(ctypes.c_uint*1)()
                            if self.bassmidi.BASS_MIDI_FontGetPresets(handle,packed):actual=(handle,(packed[0]>>16)&0xffff,packed[0]&0xffff)
                        singleton_sources[handle]=actual
                    actual=singleton_sources[handle]
                    if actual is not None and actual!=(handle,request.bank,request.preset):alias_slot=target(*actual)
                attempts.append(_NativePreloadAttempt(primary,alias_slot))
                known=runtime._preload_single_source_aliases.get((handle,request.bank,request.preset))
                aliases.append(int(alias_slot!=0xffffffff and known==tuple(vars(self.targets[alias_slot]).values())))
            if len(attempts)>first:
                count=len(attempts)-first
                # Only byte-identical ordered attempts AND seeded alias state
                # may share storage. Different failed request keys never merge;
                # failures still retry on the next Note in native full().
                chain=tuple((item.slot,item.alias_slot,aliases[first+offset]) for offset,item in enumerate(attempts[first:]))
                previous=chains.get(chain)
                if previous is None:chains[chain]=first
                else:del attempts[first:];del aliases[first:];first=previous
                routes.append(_NativePreloadRoute(request.bank,request.preset,first,count))
        if any(len(values)>0xffffffff for values in (self.targets,routes,attempts)):
            raise ValueError('Native first-use DWORD count overflow')
        self.targets=tuple(self.targets)
        self._cache_alias_keys=tuple((self.targets[item.slot],
            None if item.alias_slot==0xffffffff else self.targets[item.alias_slot]) for item in attempts)
        signatures={
            'version':([],ctypes.c_uint),'live_count':([],ctypes.c_uint),
            'create':([ctypes.POINTER(_NativePreloadTarget),ctypes.c_uint,ctypes.POINTER(ctypes.c_ubyte),
                ctypes.POINTER(_NativePreloadRoute),ctypes.c_uint,ctypes.POINTER(_NativePreloadAttempt),ctypes.c_uint,
                ctypes.POINTER(ctypes.c_ubyte),ctypes.c_uint64,ctypes.c_uint64,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint,
                ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p],ctypes.c_void_p),
            'before_note':([ctypes.c_void_p,ctypes.c_uint64,ctypes.c_uint64]+[ctypes.c_uint]*6+[ctypes.POINTER(ctypes.c_uint64)],ctypes.c_int),
            'status':([ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64)],ctypes.c_int),
            'io_status':([ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64)],ctypes.c_int),
            'io_result':([ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(_NativePreloadResult)],ctypes.c_int),
            'retain':([ctypes.c_void_p],ctypes.c_uint),'release':([ctypes.c_void_p],ctypes.c_uint),
            'cancel':([ctypes.c_void_p],ctypes.c_int),'join':([ctypes.c_void_p,ctypes.c_uint],ctypes.c_int),
            'destroy':([ctypes.c_void_p],ctypes.c_int)}
        for name,(args,result) in signatures.items():
            function=getattr(library,'font_preload_plan_'+name);function.argtypes=args;function.restype=result
        if library.font_preload_plan_version()!=1:raise RuntimeError('Native first-use plan ABI')
        if self.mode==1 and not self.targets:
            version=getattr(library,'font_preload_plan_empty_version',None)
            if version is None:raise RuntimeError('Native empty first-use plan ABI unavailable')
            version.argtypes=[];version.restype=ctypes.c_uint
            if version()!=1:raise RuntimeError('Native empty first-use plan ABI')
        self.cache_available=hasattr(library,'font_preload_plan_cache_version')
        if self.cache_available:
            library.font_preload_plan_cache_version.argtypes=[];library.font_preload_plan_cache_version.restype=ctypes.c_uint
            if library.font_preload_plan_cache_version()!=1:raise RuntimeError('Native first-use cache ABI')
            library.font_preload_plan_cache.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ubyte),ctypes.c_uint,
                ctypes.POINTER(ctypes.c_ubyte),ctypes.c_uint]
            library.font_preload_plan_cache.restype=ctypes.c_int
        rows=(_NativePreloadTarget*len(self.targets))(*(_NativePreloadTarget(item.font,item.preset,item.bank,0,0,0) for item in self.targets))
        seeds=(ctypes.c_ubyte*len(rows))(*(int((item.font,item.bank,item.preset) in full) for item in self.targets))
        route_rows=(_NativePreloadRoute*len(routes))(*routes);attempt_rows=(_NativePreloadAttempt*len(attempts))(*attempts)
        alias_rows=(ctypes.c_ubyte*len(aliases))(*aliases)
        addresses=[ctypes.cast(function,ctypes.c_void_p) for function in (
            self.bassmidi.BASS_MIDI_FontLoad,self.bassmidi.BASS_MIDI_FontLoadEx,self.bass.BASS_ErrorGetCode,self.bass.BASS_GetConfig)]
        self.pointer=library.font_preload_plan_create(rows,len(rows),seeds,route_rows,len(routes),attempt_rows,len(attempts),
            alias_rows,self.epoch,self.stream_generation,self.stream,self.mode,capacity,*addresses)
        if not self.pointer:raise RuntimeError('Native first-use plan creation')

    @property
    def current(self):
        with self._lifetime:
            return bool(self.pointer and self.epoch==self._runtime._preparation_generation and
                        self.stream==self._runtime.stream_handle())

    @contextmanager
    def borrow(self):
        # Admission occurs before CDLL releases GIL/native active increments.
        # Cancellation may wake this call; destruction must wait for its return.
        with self._lifetime:
            pointer=self.pointer
            if pointer:self._calls+=1
        try:yield pointer
        finally:
            if pointer:
                with self._lifetime:self._calls-=1

    def before_note(self,bank,preset,*,channel=0,lsb=0,qualification=0):
        output=(ctypes.c_uint64*8)()
        with self.borrow() as pointer:
            if not pointer:return -3,tuple(output)
            if not self.current:return -2,tuple(output)
            status=self.library.font_preload_plan_before_note(pointer,self.epoch,self.stream_generation,self.stream,
                                                            channel,bank,preset,lsb,qualification,output)
        return status,tuple(output)

    def cancel(self,*,reason='explicit'):
        if reason not in ('explicit','source_refresh','retirement'):raise ValueError('Native first-use cancel reason')
        if self.cancel_reason is None:self.cancel_reason=reason
        with self.borrow() as pointer:return not pointer or bool(self.library.font_preload_plan_cancel(pointer))

    def replay_cache(self):
        """Metadata-only successful-source cache replay, with no I/O wait.

        A native worker borrower must first preserve its original frame/Note
        prefix. This does not replay or submit any MIDI or FontLoad call.
        """
        if not self.cache_available:return False
        runtime=self._runtime
        if not runtime._preparation_lock.acquire(blocking=False):return False
        try:
            with self.borrow() as pointer:
                if not pointer or not self.current:return False
                full=(ctypes.c_ubyte*len(self.targets))()
                aliases=(ctypes.c_ubyte*len(self._cache_alias_keys))()
                status=self.library.font_preload_plan_cache(pointer,full,len(full),aliases,len(aliases))
                if status==0:return False
                if status!=1:raise RuntimeError('Native first-use cache snapshot')
            if runtime.preloaded_used is not None:
                runtime.preloaded_used.update((item.font,item.bank,item.preset) for item,ready in zip(self.targets,full) if ready)
            for (source,alias),used in zip(self._cache_alias_keys,aliases):
                if used and alias is not None:
                    runtime._preload_single_source_aliases[source.font,source.bank,source.preset]=(alias.font,alias.bank,alias.preset)
            return True
        finally:runtime._preparation_lock.release()

    def close(self,*,timeout_ms=5000):
        with self._lifetime:
            if not self.pointer:return True
            if self.cancel_reason is None:self.cancel_reason='retirement'
            self.library.font_preload_plan_cancel(self.pointer)
            if self._calls:return False
            if not self.library.font_preload_plan_join(self.pointer,timeout_ms):return False
            if not self.library.font_preload_plan_destroy(self.pointer):return False
            self.pointer=None;return True

    def join_cancelled(self,*,timeout_ms=5000):
        """Quiesce native I/O before reading final cache, retaining metadata.

        Cancellation wakes the queue worker. Its last mutex write must finish
        before the nonblocking cache snapshot; destruction remains separate.
        """
        with self._lifetime:
            if not self.pointer:return True
            if self.cancel_reason is None:self.cancel_reason='retirement'
            self.library.font_preload_plan_cancel(self.pointer)
            if self._calls:return False
            return bool(self.library.font_preload_plan_join(self.pointer,timeout_ms))


class SoundFontRuntime:
    """Own font assets/group state across output-driven stream recreation.

    Explicit source replacement/close still retires all borrowers and handles;
    output switching may retain assets and rebind them to a new MIDI stream.
    """

    def __init__(self, *, bass, bassmidi, stream_handle, engine_preferences,
                 configure_preset_controls, preloaded_used=None,
                 configure_available_presets=None, update_preset_control=None,
                 configure_performance=None, commit_scope=None, scalar_scope=None,configure_chord_sources=None):
        self.bass = bass
        self.bassmidi = bassmidi
        self.stream_handle = stream_handle
        self.engine_preferences = engine_preferences
        self._configure_preset_controls = configure_preset_controls
        self._configure_available_presets = configure_available_presets
        self._update_preset_control = update_preset_control
        self._configure_performance = configure_performance
        self._configure_chord_sources=configure_chord_sources
        self._chord_pitch_metadata={}
        if commit_scope is not None and not callable(commit_scope):raise ValueError('SoundFont commit scope')
        self.commit_scope=commit_scope or nullcontext
        if scalar_scope is not None and not callable(scalar_scope):raise ValueError('SoundFont scalar scope')
        # Optional composition-owned, NONBLOCKING font-lifetime lease. It
        # yields True only while source loading/free is excluded. Without the
        # port, preserve the original source publication fence.
        self.scalar_scope=scalar_scope
        self.source_commit_uncertain=False
        self._source_refresh_context=ContextVar('soundfont_source_refresh_group',default=None)
        self._source_refresh_controls=None
        self.preloaded_used = preloaded_used
        self.handles: list[int] = []
        self._reference_font=0
        self.group = SoundFontGroup()
        self._preset_positions = {}
        self._active_name_cache = {}
        self._font_encoding_cache = {}
        self.source_generation=0
        self.asset_identities=()
        self._asset_staging = {}
        self._retired_handles = set()
        self._resolved_asset_sources = {}
        self._preparation_generation = 0
        self._preload_single_source_aliases = {}
        self._preparation_jobs = []
        self._native_preparation_queues = []
        self._native_first_use_plans = []
        self._preparation_lock = threading.RLock()
        self.preset_tone_submitter=None;self.control_lock=None;self.control_recovery=None;self.control_commit_scope=None
        self._control_condition=threading.Condition();self._control_pending=None
        self._control_busy=False;self._control_stopping=False;self._control_thread=None;self._control_serial=0

    def prepare_reference_tone(self):
        """Startup/control-side only;never load a font from a GUI press.

        bassmidi.h FontInit/FontLoad own/preload a device-independent font.
        It is not part of the user's group or SFG and has no live mapping.
        """
        if self._reference_font:return self._reference_font
        path=Path(__file__).with_name('assets')/'tuning-reference.sf2'
        handle=self.bassmidi.BASS_MIDI_FontInit(str(path),BASS_UNICODE)
        if not handle:raise SoundFontRuntimeError(tr('tuning_preview.reference_unavailable',error=self.bass.BASS_ErrorGetCode()))
        if not self.bassmidi.BASS_MIDI_FontLoad(handle,0,0):
            error=self.bass.BASS_ErrorGetCode();self.free_handles((handle,))
            raise SoundFontRuntimeError(tr('tuning_preview.reference_unavailable',error=error))
        self._reference_font=handle
        return handle

    def reference_tone_source(self):
        if not self._reference_font:raise SoundFontRuntimeError(tr('tuning_preview.reference_not_ready'))
        return self._reference_font,0,0

    def queue_runtime_controls(self,group,change,on_complete):
        """One bounded source-control owner for every preset/profile editor.

        Unstarted snapshots may be superseded;each accepted request receives
        its own completion. A running transaction is never cancelled/replayed.
        Rendering and QWidget work are not performed by this worker.
        """
        supported=change=='performance' or isinstance(change,tuple) and change[:1] in (('preset_controls',),('preset_batch',))
        if not supported or self.control_lock is None or not self.stream_handle() or not self.same_layout(group):return None
        snapshot=self.copy_group(group)
        if isinstance(change,tuple) and change[:1]==('preset_controls',) and len(change)>3:
            # The explicit single-preset API can carry a newer value than the
            # document. Preserve that established contract before coalescing.
            key=change[1:3];item=change[3]
            position=next((i for i,entry in enumerate(snapshot.presets) if (entry.bank,entry.preset)==key),None)
            if position is None:
                if item is not None:snapshot.presets.append(PresetOverride(**vars(item)))
            elif item is None:snapshot.presets.pop(position)
            else:snapshot.presets[position]=PresetOverride(**vars(item))
        with self._control_condition:
            if self._control_stopping:return dict(accepted=False,completed=True,successful=False)
            self._control_serial+=1;serial=self._control_serial;previous=self._control_pending
            self._control_pending=(serial,snapshot,on_complete)
            if self._control_thread is None:
                self._control_thread=threading.Thread(target=self._run_runtime_controls,name='ABMSynth preset controls',daemon=True)
                self._control_thread.start()
            self._control_condition.notify_all()
        if previous is not None:
            old_serial,old_group,old_done=previous
            old_done(dict(accepted=True,completed=True,successful=False,state_changed=False,superseded=True,serial=old_serial),None)
        return dict(accepted=True,mode='queued',completed=False,serial=serial)

    def finish_runtime_controls(self,timeout=5.):
        deadline=time.monotonic()+timeout
        with self._control_condition:
            while self._control_busy or self._control_pending is not None:
                remaining=deadline-time.monotonic()
                if remaining<=0:return False
                self._control_condition.wait(min(.01,remaining))
        return True

    def _run_runtime_controls(self):
        while True:
            with self._control_condition:
                while self._control_pending is None and not self._control_stopping:self._control_condition.wait()
                if self._control_stopping:return
                serial,group,done=self._control_pending;self._control_pending=None;self._control_busy=True
            error=None;success=False
            try:
                with self.control_lock:
                    device=self.bass.BASS_ChannelGetDevice(self.stream_handle())
                    if device==0xffffffff or not self.bass.BASS_SetDevice(device):
                        raise SoundFontRuntimeError(tr('runtime.soundfont_device_failed'))
                    previous=self.group.preset_map();current=group.preset_map()
                    keys=tuple(key for key in previous.keys()|current.keys() if previous.get(key)!=current.get(key))
                    change='performance' if group.performance!=self.group.performance else ('preset_batch',keys)
                    success=self.apply_runtime(group,change) is True
            except Exception as exc:
                error=exc
                if self.control_recovery is not None:
                    # Any native unlock/publication remains on its original
                    # worker until the existing recovery owner certifies exit.
                    while True:
                        try:safe,_owners=self.control_recovery()
                        except Exception as recovery_error:
                            error=recovery_error;safe=False
                        if safe:break
                        with self._control_condition:self._control_condition.wait(.05)
            try:
                done(dict(accepted=True,completed=True,successful=success,state_changed=success,serial=serial,
                          error=None if error is None else str(error)),self.copy_group(self.group))
            finally:
                with self._control_condition:self._control_busy=False;self._control_condition.notify_all()

    def _apply_runtime_tone(self,group):
        """Use the existing MIDI actor for unchanged mapping/extended policy."""
        if self.preset_tone_submitter is None or threading.current_thread() is not self._control_thread:return None
        if group.performance!=self.group.performance or group.mappings!=self.group.mappings:return None
        old=self.group.preset_map();new=group.preset_map();keys=old.keys()|new.keys()
        tone=('volume','attack','decay','release','cutoff','resonance')
        changed={}
        for key in keys:
            before=old.get(key) or PresetOverride(*key);after=new.get(key) or PresetOverride(*key)
            if any(getattr(before,name)!=getattr(after,name) for name in vars(before) if name not in (*tone,'memo')):return None
            if any(getattr(before,name)!=getattr(after,name) for name in tone):
                if before.disabled or after.disabled:return None
                changed[key]={name:getattr(after,name) for name in tone}
        if not changed:return None
        complete=threading.Event();result=[]
        def done(receipt):result.append(receipt);complete.set()
        receipt=self.preset_tone_submitter(changed,on_complete=done)
        if receipt is None:return None
        if not receipt.accepted:return False
        # Wait on the control worker only. GUI and native input/output continue.
        # Never abandon an accepted receipt and then repeat its audio.
        complete.wait();receipt=result[0]
        if receipt.state_changed:
            self.group=self.copy_group(group)
            self._preset_positions={(item.bank,item.preset):index for index,item in enumerate(self.group.presets)}
        return bool(receipt.successful)

    def start_first_note_preparation(self, requests, *, length, units):
        """Prepare explicit selections; production loading remains unchanged.

        length=0 means full preload; otherwise units is frames or milliseconds.
        No default head length or startup-wide admission is hidden here. Call
        at a preparation/control phase, not a Note/CC callback. Existing
        controller barriers serialize group/mapping control mutations.
        """
        if isinstance(length, bool) or not isinstance(length, int) or not 0 <= length <= 0xffffffff:
            raise ValueError("SoundFont preparation length")
        if units not in ("frames", "milliseconds"):
            raise ValueError("SoundFont preparation units")
        normalized = tuple(dict.fromkeys(
            item if isinstance(item, SoundFontPreloadRequest) else SoundFontPreloadRequest(*item)
            for item in requests))
        for item in normalized:
            if (not all(isinstance(value, int) and not isinstance(value, bool)
                        for value in (item.bank, item.preset, item.bank_lsb)) or
                    not 0 <= item.bank <= 65535 or not 0 <= item.preset <= 65535 or
                    not 0 <= item.bank_lsb < 128):
                raise ValueError("SoundFont preparation request")
        with self._preparation_lock:
            if not self.stream_handle() or not self.handles:
                raise RuntimeError("SoundFont preparation has no committed stream/fonts")
            job = SoundFontPreparationJob(self, normalized, length, units)
            self._preparation_jobs.append(job)
            try:
                job._thread.start()
            except BaseException:
                self._preparation_jobs.remove(job)
                raise
        return job

    def retire_first_note_preparations(self, handles=None):
        """Cancel queued work and join active calls before remap/free.

        Completed manifests are invalidated too. Native consumers copying rows
        must retire their borrower before this control operation. This does
        not claim to cancel BASS-owned asynchronous sample readers.
        """
        with self._preparation_lock:
            targets = None if handles is None else set(handles)
            jobs = [job for job in self._preparation_jobs
                    if targets is None or targets.intersection(job.handles)]
            queues = [queue for queue in self._native_preparation_queues
                      if targets is None or targets.intersection(queue.handles)]
            plans = [plan for plan in self._native_first_use_plans
                     if targets is None or targets.intersection(plan.handles)]
            for job in jobs:
                job.cancel()
            for queue in queues:
                queue.cancel()
            for plan in plans:
                plan.cancel(reason='retirement')
            for job in jobs:
                job.join()
                self._preparation_jobs.remove(job)
            for queue in queues:
                if not queue.close():
                    # Keep owner/DLLs/handles alive; do not advance into remap
                    # or FontFree when an external native borrower is present.
                    raise SoundFontRuntimeError(tr('runtime.font_preload_failed',error='native queue retirement'))
                self._native_preparation_queues.remove(queue)
            for plan in plans:
                # A stale stream/epoch must never receive old cache metadata.
                # It still owns native I/O/handles and must be cancellable and
                # destructible; invalidation is not a permanent teardown trap.
                if not plan.join_cancelled():
                    raise SoundFontRuntimeError(tr('runtime.font_preload_failed',error='native first-use join retirement'))
                if plan.cache_available and plan.current and not plan.replay_cache():
                    raise SoundFontRuntimeError(tr('runtime.font_preload_failed',error='native first-use cache retirement'))
                if not plan.close():
                    raise SoundFontRuntimeError(tr('runtime.font_preload_failed',error='native first-use retirement'))
                self._native_first_use_plans.remove(plan)

    def start_native_first_note_preparation(self, job, library, *, length, units, capacity, paused=False):
        """Candidate native source I/O; no production loading-mode admission."""
        with self._preparation_lock:
            queue=NativeSoundFontPreparationQueue(self,job,library,length=length,units=units,
                                                 capacity=capacity,paused=paused)
            self._native_preparation_queues.append(queue)
            return queue

    def prepare_native_first_use_plan(self,requests,library,*,stream_generation,capacity,all_addresses=False):
        """Candidate immutable legacy full plan; no app/INI admission."""
        if type(all_addresses) is not bool:raise ValueError('Native first-use address coverage')
        requests=tuple(requests)
        if all_addresses and int(self.engine_preferences().loading_mode)==1:
            version=getattr(library,'font_preload_plan_address_domain_version',None)
            if version is None:raise ValueError('Native first-use full address ABI unavailable')
            version.argtypes=[];version.restype=ctypes.c_uint
            if version()!=1:raise ValueError('Native first-use full address ABI')
            # Route preparation only: never FontLoad(-1,-1) or any sample I/O.
            # MIDI CC0/Program's complete product admission, plus SF2 drum128.
            requests=tuple(dict.fromkeys((*requests,*((bank,preset) for bank in range(129) for preset in range(128)))))
        with self._preparation_lock:
            plan=NativeSoundFontFirstUsePlan(self,requests,library,stream_generation,capacity)
            self._native_first_use_plans.append(plan)
            return plan

    def _prepare_first_notes(self, job):
        catalog = {}
        for handle in dict.fromkeys(job.handles):
            job._check_cancel()
            info = BASS_MIDI_FONTINFO()
            if not self.bassmidi.BASS_MIDI_FontGetInfo(handle, ctypes.byref(info)):
                raise SoundFontRuntimeError(tr("runtime.font_preload_failed", error=self.bass.BASS_ErrorGetCode()))
            packed = (ctypes.c_uint * info.presets)()
            if info.presets and not self.bassmidi.BASS_MIDI_FontGetPresets(handle, packed):
                raise SoundFontRuntimeError(tr("runtime.font_preload_failed", error=self.bass.BASS_ErrorGetCode()))
            catalog[handle] = tuple(SoundFontPreloadTarget(handle, (value >> 16) & 0xffff, value & 0xffff)
                                    for value in packed)
        mapping = job._mapping
        routes = []
        for request in job.requests:
            job._check_cancel()
            disabled = (request.bank, request.preset) in job._disabled
            candidates = []
            for item in job._extended_mapping:
                if item.address==(request.bank,request.bank_lsb,request.preset):
                    candidates.extend(target for target in catalog[job.handles[item.source_index]]
                        if (target.bank,target.preset)==(item.source_bank,item.source_program))
            # Disabled SFG instrument settings suppress its explicit mapping
            # and overrides, not the Note. Wildcard sources still sound.
            for handle, preset, bank in mapping:
                if preset != -1:
                    if (bank, preset) != (request.bank, request.preset):
                        continue
                    exact = [item for item in catalog[handle] if (item.bank, item.preset) == (bank, preset)]
                    # FONT individual mapping falls back to the font's
                    # first preset when exact is absent. GetPresets is
                    # numeric order, not documented file order: prepare
                    # all in that font instead of guessing "first".
                    candidates.extend(exact or catalog[handle])
            # Exact matches across all stacked fonts precede fallback banks.
            for handle in job.handles:
                candidates.extend(item for item in catalog[handle]
                                  if (item.bank, item.preset) == (request.bank, request.preset))
            # BASS falls back LSB then MSB. Nonzero LSB covers all same-program
            # banks conservatively; this is coverage, not a native resolver.
            for handle in job.handles:
                candidates.extend(item for item in catalog[handle] if item.preset == request.preset
                                  and (request.bank_lsb or item.bank == 0))
            routes.append(SoundFontPreloadRoute(request, tuple(dict.fromkeys(candidates)), disabled))
        targets = tuple(dict.fromkeys(item for route in routes for item in route.candidates))
        prepared, failures = [], []
        # bassmidi.h: TIME=8, length DWORD; no COMPACT/NOLOAD/KEEPDEC change.
        flags = BASS_MIDI_FONTLOAD_NOWAIT | (8 if job.units == "milliseconds" else 0)
        for target in targets:
            job._check_cancel()
            if self.bassmidi.BASS_MIDI_FontLoadEx(target.font, target.preset, target.bank, job.length, flags):
                prepared.append(target)
            else:
                failures.append((target, int(self.bass.BASS_ErrorGetCode())))
        # SAMPLELOADING counts readers globally, not per-job/preset. Zero is
        # conservative completion; bytes==samsize is wrong for partial/SFZ.
        while self.bass.BASS_GetConfig(BASS_CONFIG_MIDI_SAMPLELOADING):
            job._check_cancel()
            job.cancel_requested.wait(0.01)
        job._check_cancel()
        return SoundFontPreloadManifest(job.generation, job.source_generation, job.stream,
                                       mapping, tuple(routes), job.length, job.units,
                                       tuple(prepared), tuple(failures))

    def _remember_group(self, group):
        self.group = self.copy_group(group)
        if self._configure_chord_sources is not None:
            self._configure_chord_sources({handle:(entry.custom is None and
                Path(entry.filename).suffix.lower() in ('.sf2','.sf3'),self._chord_pitch_metadata.get(handle,frozenset()))
                for handle,entry in zip(self.handles,self.group.fonts)})
        if self._configure_performance is not None:
            self._configure_performance(self.group.performance)
        resolved={str(group.font_path(entry).resolve()) for entry in group.fonts}
        self.asset_identities=tuple(identity for identity in self.asset_identities if identity['path'] in resolved)
        self._active_name_cache.clear()
        self._font_encoding_cache.clear()
        self._preload_single_source_aliases.clear()
        self._preset_positions = {
            (item.bank, item.preset): index for index, item in enumerate(self.group.presets)
        }

    @staticmethod
    def copy_group(group):
        return SoundFontGroup(
            [SoundFontEntry(entry.filename, int(entry.volume),entry.custom,entry.runtime_path) for entry in group.fonts],
            [PresetOverride(**vars(entry)) for entry in group.presets],
            group.source_path,
            group.performance,
            group.mappings,
        )

    @staticmethod
    def group_key(group, entry):
        key=os.path.normcase(os.path.abspath(str(group.font_path(entry))))
        if entry.custom is not None:
            import json
            key+='|'+json.dumps(entry.custom,sort_keys=True,ensure_ascii=True)
        return key

    def same_layout(self, group):
        return len(group.fonts) == len(self.group.fonts) and all(
            self.group_key(group, current) == self.group_key(self.group, active)
            for current, active in zip(group.fonts, self.group.fonts)
        )

    def free_handles(self, handles):
        before=getattr(self,'before_mapping_change',None)
        if before:before()
        handles = tuple(handles)
        self.retire_first_note_preparations(handles)
        for handle in set(handles):
            self._retired_handles.add(handle)
            try:
                freed = self.bassmidi.BASS_MIDI_FontFree(handle)
                if freed and handle in self._asset_staging:
                    self._asset_staging[handle].cleanup()
                    self._asset_staging.pop(handle)
                if freed: self._retired_handles.discard(handle)
                if freed: self._resolved_asset_sources.pop(handle,None)
                if freed:self._chord_pitch_metadata.pop(handle,None)
            except Exception:
                pass  # Retain ownership for retry; never remove live sample files.
        return not self._retired_handles

    def asset_snapshot(self):
        """Committed source and actual native path provenance, with no file I/O."""
        return tuple(dict(identity, resolved_sources=[dict(source) for handle,entry in zip(self.handles,self.group.fonts)
            if self._resolved_asset_sources.get(handle) and self._resolved_asset_sources[handle][0]['source']==identity['path']
            for source in self._resolved_asset_sources.get(handle,())]) for identity in self.asset_identities)

    def apply_fx_policy(self, policy):
        """Atomically update every owned font without reloading samples.

        bassmidi.h BASS_MIDI_FontFlags changes MINFX/NOFX on live handles;
        preserve unrelated flags and roll back all earlier handles on error.
        Newly initialized fonts receive the selected flags at FontInit.
        """
        requested = SOUNDFONT_FX_FLAGS[policy]
        changed = []
        for handle in dict.fromkeys(self.handles):
            previous = self.bassmidi.BASS_MIDI_FontFlags(handle, 0, 0)
            if previous == 0xffffffff:
                error = self.bass.BASS_ErrorGetCode()
                for old_handle, old_flags in reversed(changed):
                    self.bassmidi.BASS_MIDI_FontFlags(old_handle, old_flags, SOUNDFONT_FX_MASK)
                raise SoundFontRuntimeError(tr("runtime.soundfont_fx_policy_failed",
                                               error=error))
            if previous & SOUNDFONT_FX_MASK == requested:
                continue
            applied = self.bassmidi.BASS_MIDI_FontFlags(handle, requested, SOUNDFONT_FX_MASK)
            if applied == 0xffffffff:
                error = self.bass.BASS_ErrorGetCode()
                for old_handle, old_flags in reversed(changed):
                    self.bassmidi.BASS_MIDI_FontFlags(old_handle, old_flags, SOUNDFONT_FX_MASK)
                raise SoundFontRuntimeError(tr("runtime.soundfont_fx_policy_failed",
                                               error=error))
            changed.append((handle, previous & SOUNDFONT_FX_MASK))

    @staticmethod
    def effective_presets(group):
        return list(group.preset_map().values())

    def set_mapping(self, group, handles):
        before=getattr(self,'before_mapping_change',None)
        if before:before()
        routes=self._mapping_sources(group,handles)
        self.retire_first_note_preparations()
        effective = self.effective_presets(group)
        stream = self.stream_handle()
        if not handles:
            if not self.bassmidi.BASS_MIDI_StreamSetFonts(stream, None, 0):
                raise SoundFontRuntimeError(tr("runtime.soundfont_unmap_failed"))
            self._preparation_generation += 1
            self._preload_single_source_aliases.clear()
            return effective, 0
        explicit = [
            BASS_MIDI_FONT(handles[item.index], item.preset, item.bank)
            for item in effective
            if item.index is not None and not item.disabled
        ]
        settings = explicit + [BASS_MIDI_FONT(handle, -1, 0) for handle in handles]
        if group.mappings:
            # Local bassmidi.h FONTEX: independent source/destination and
            # LSB; no source-bank-to-LSB inference. Preserve legacy LSB0 and
            # wildcard order exactly behind the explicit nonzero destinations.
            routes.extend(BASS_MIDI_FONTEX(row.font,row.preset,
                -1 if row.preset==-1 else row.bank,row.preset,row.bank,0) for row in settings)
            array=(BASS_MIDI_FONTEX*len(routes))(*routes)
            pointer=ctypes.cast(array,ctypes.POINTER(BASS_MIDI_FONT))
            count=len(routes)|BASS_MIDI_FONT_EX
        else:
            array=(BASS_MIDI_FONT*len(settings))(*settings)
            pointer,count=array,len(settings)
        if not self.bassmidi.BASS_MIDI_StreamSetFonts(stream, pointer, count):
            raise SoundFontRuntimeError(tr("runtime.soundfont_map_failed"))
        self._preparation_generation += 1
        self._preload_single_source_aliases.clear()
        return effective, len(explicit)+len(group.mappings)

    def _mapping_sources(self,group,handles):
        from soundfont_files import SoundFontMapping
        if (any(not isinstance(item,SoundFontMapping) or not item.valid(len(handles)) for item in group.mappings)
                or len(group.mapping_map())!=len(group.mappings)):
            raise SoundFontRuntimeError(tr('error.invalid_soundfont_mapping'))
        rows=[]
        for item in group.mappings:
            handle=handles[item.source_index]
            if not self.bassmidi.BASS_MIDI_FontGetPreset(handle,item.source_program,item.source_bank):
                raise SoundFontRuntimeError(tr('error.mapping_source_missing',bank=item.source_bank,program=item.source_program))
            rows.append(BASS_MIDI_FONTEX(handle,item.source_program,item.source_bank,item.program,item.bank,item.bank_lsb))
        return rows

    def configure_preset_controls(self, effective):
        overrides={
            (item.bank, item.preset): {
                "volume": item.volume,
                "attack": item.attack,
                "decay": item.decay,
                "release": item.release,
                "cutoff": item.cutoff,
                "resonance": item.resonance,
                "disabled": item.disabled,
                "extended": dict(item.extended),
                "extended_disabled": set(item.extended_disabled),
                "performance": dict(item.performance),
            }
            for item in effective
        }
        if self._source_refresh_controls is not None:
            self._source_refresh_controls(overrides)
        else:self._configure_preset_controls(overrides)

    @property
    def _source_refresh_group(self):return self._source_refresh_context.get()

    @contextmanager
    def source_commit_scope(self,group):
        """Expose the intended source policy only for this owned publication.

        Detached preparation happens before the existing exact MIDI fence.
        Unsupported policies retain that fence's complete legacy transfer.
        """
        token=self._source_refresh_context.set(group)
        try:
            with self.commit_scope():yield
        finally:self._source_refresh_context.reset(token)

    def available_preset_keys(self, handles):
        """Resolve actual loaded-font preset keys, as the original list writer does."""
        keys = set()
        for handle in handles:
            info = BASS_MIDI_FONTINFO()
            if not self.bassmidi.BASS_MIDI_FontGetInfo(handle, ctypes.byref(info)) or not info.presets:
                continue
            raw = (ctypes.c_uint * info.presets)()
            if self.bassmidi.BASS_MIDI_FontGetPresets(handle, raw):
                keys.update(((packed >> 16) & 0xffff, packed & 0xffff) for packed in raw)
        return keys

    def can_remap_existing(self, group):
        available = {}
        for entry, handle in zip(self.group.fonts, self.handles):
            available.setdefault(self.group_key(self.group, entry), []).append(handle)
        for entry in group.fonts:
            candidates = available.get(self.group_key(group, entry))
            if not candidates or not candidates.pop(0):
                return False
        return True

    def remap_existing(self, group):
        """Reorder/remove already owned fonts without reinitializing samples."""
        with self.scalar_scope() if self.scalar_scope is not None else nullcontext(True) as admitted:
            if (admitted and not self.source_commit_uncertain and self.same_layout(group) and
                    self._runtime_audio_signature(group)==self._runtime_audio_signature(self.group)):
                # Save/Save As changes the document path and Memo, not sound.
                # No ingress pause,128-channel hydration or held-Note reapply.
                self.group=self.copy_group(group)
                self._preset_positions={(item.bank,item.preset):index for index,item in enumerate(self.group.presets)}
                return SoundFontLoadResult(len(self.handles),sum(item.index is not None and not item.disabled
                    for item in self.effective_presets(group))+len(group.mappings))
        available = {}
        for entry, handle in zip(self.group.fonts, self.handles):
            available.setdefault(self.group_key(self.group, entry), []).append(handle)
        handles = []
        for entry in group.fonts:
            candidates = available.get(self.group_key(group, entry), [])
            if not candidates:
                raise SoundFontRuntimeError(tr("runtime.soundfont_map_failed"))
            handles.append(candidates.pop(0))
        with self.source_commit_scope(group):
            self.retire_first_note_preparations()
            effective, explicit_count = self.set_mapping(group, handles)
            self.source_commit_uncertain=True
            retained = set(handles)
            obsolete = {handle for values in available.values() for handle in values
                        if handle not in retained}
            self.handles = handles
            self._remember_group(group)
            self.configure_preset_controls(effective)
            if self._configure_available_presets is not None:
                self._configure_available_presets(self.available_preset_keys(handles))
            if self.preloaded_used is not None and obsolete:
                self.preloaded_used.difference_update(
                    {entry for entry in self.preloaded_used if entry[0] in obsolete}
                )
            self.free_handles(obsolete)
            self.source_commit_uncertain=False
        return SoundFontLoadResult(len(handles), explicit_count)

    def rebind_loaded_stream(self):
        """Map retained assets after output-driven stream recreation.

        BASSMIDI FontInit/StreamSetFonts document independently owned fonts
        shared by streams. Do not hash/reopen/reload unchanged assets here.
        Explicit reload/group editing still uses load_group's identity checks.
        """
        if not self.stream_handle():return None
        effective,count=self.set_mapping(self.group,self.handles)
        self.configure_preset_controls(effective)
        self._remember_group(self.group)
        if self._configure_available_presets is not None:
            self._configure_available_presets(self.available_preset_keys(self.handles))
        return SoundFontLoadResult(len(self.handles),count)

    def load_group(self, group, *, progress=None, cancel=None,retain_custom_sources=False):
        """Load a group, committing owned handles only after mapping succeeds."""
        if any(entry.custom is not None for entry in group.fonts):
            from instrument_creator import compile_record
            for entry in group.fonts:
                if entry.custom is not None:
                    if retain_custom_sources:
                        if not entry.runtime_path or not Path(entry.runtime_path).is_file():
                            raise SoundFontRuntimeError(tr('player.asset_changed',path=entry.label))
                    else:
                        entry.runtime_path=compile_record(entry.custom,group.source_path,
                            lambda:cancel is not None and cancel.is_set())
        errors = validate_group(group,resolve_custom_samples=not retain_custom_sources)
        if errors and group.fonts:
            raise SoundFontRuntimeError("\n".join(errors))
        if not self.stream_handle():
            return None

        # BASSMIDI FontInit shares data by path and last-modified time. Never
        # claim content refresh when bytes changed with the timestamp held.
        # Hash once in the already-staged load worker, not periodic readback.
        identities=[]
        old_assets={identity['path']:identity for identity in self.asset_identities}
        # GetConfigPtr borrows BASS-owned header bytes only for this control
        # operation. No audio callback/hash path owns that pointer.
        from bass_bindings import BASS_CONFIG_MIDI_SFZHEAD
        header_pointer = self.bass.BASS_GetConfigPtr(BASS_CONFIG_MIDI_SFZHEAD)
        sfz_header = ctypes.string_at(header_pointer) if isinstance(header_pointer, int) and header_pointer else b''
        for entry in group.fonts:
            reader = soundfont_asset_identity if entry.filename.lower().endswith('.sfz') else asset_file_identity
            try: identity=reader(group.font_path(entry),lambda:cancel is not None and cancel.is_set(),
                                 **({'sfz_header':sfz_header} if reader is soundfont_asset_identity else {}))
            except RuntimeError:
                if cancel is not None and cancel.is_set(): raise SoundFontLoadCancelled()
                raise
            previous=old_assets.get(identity['path'])
            if previous and previous['sha256']!=identity['sha256'] and previous['mtime_ns']==identity['mtime_ns']:
                raise SoundFontRuntimeError(tr('runtime.soundfont_same_timestamp',filename=entry.filename))
            identities.append(identity)

        previous_preloaded = set(self.preloaded_used) if self.preloaded_used is not None else None
        staged_preloaded=set()

        reusable = {}
        for entry, handle in zip(self.group.fonts, self.handles):
            key=self.group_key(self.group,entry)
            previous=old_assets.get(str(self.group.font_path(entry).resolve()))
            current=next((item for item in identities if item['path']==str(self.group.font_path(entry).resolve())),None)
            if previous is not None and previous==current and not entry.filename.lower().endswith('.sfz'):
                reusable.setdefault(key, []).append(handle)
        handles, created = [], []
        original_volumes = {
            handle: float(entry.volume) / 100
            for entry, handle in zip(self.group.fonts, self.handles)
        }
        changed_reused = set()
        def check_cancel():
            if cancel is not None and cancel.is_set():
                raise SoundFontLoadCancelled()

        def wait_for_sample_reads():
            # bassmidi.h: BASS_CONFIG_MIDI_SAMPLELOADING is the number of
            # samples currently being read; there is no per-preset completion
            # callback. Used-preset/SFZ paths wait here without blocking Cancel.
            while self.bass.BASS_GetConfig(BASS_CONFIG_MIDI_SAMPLELOADING):
                check_cancel()
                if cancel is None:
                    time.sleep(0.05)
                else:
                    cancel.wait(0.05)
            check_cancel()

        committed=False;mapping_published=False
        try:
            for number, entry in enumerate(group.fonts, 1):
                check_cancel()
                if progress is not None:
                    progress("font", number, len(group.fonts), entry.filename)
                available = reusable.get(self.group_key(group, entry), [])
                if available:
                    handle = available.pop(0)
                else:
                    load_path = group.font_path(entry)
                    resolved = str(load_path.resolve())
                    previous = old_assets.get(resolved)
                    current = identities[number - 1]
                    staged = None
                    used_stage = any(self.group_key(self.group,old_entry)==os.path.normcase(resolved) and old_handle in self._asset_staging
                                     for old_entry,old_handle in zip(self.group.fonts,self.handles))
                    if (previous is not None and ('dependencies' in current) and
                            (previous.get('dependencies')!=current['dependencies'] or
                             previous.get('resolution')!=current.get('resolution') or used_stage)):
                        staged = stage_sfz_asset(current, lambda:cancel is not None and cancel.is_set())
                        if staged is None:
                            raise SoundFontRuntimeError(tr('runtime.sfz_reload_unqualified',filename=entry.filename))
                        load_path = staged[1]
                    try:
                        handle = self.bassmidi.BASS_MIDI_FontInit(
                            ctypes.c_wchar_p(str(load_path)),
                            BASS_UNICODE | SOUNDFONT_FX_FLAGS[self.engine_preferences().soundfont_fx_policy]
                            | (BASS_MIDI_FONT_STEREO if getattr(self.engine_preferences(), "soundfont_stereo", False) else 0),
                        )
                    except BaseException:
                        if staged is not None: staged[0].cleanup()
                        raise
                    if staged is not None:
                        if handle: self._asset_staging[handle] = staged[0]
                        else: staged[0].cleanup()
                    if not handle:
                        raise SoundFontRuntimeError(tr(
                            "runtime.soundfont_init_failed",
                            filename=entry.filename,
                            error=self.bass.BASS_ErrorGetCode(),
                        ))
                    created.append(handle)
                    consumed = (current,*current.get('dependencies',()))
                    self._resolved_asset_sources[handle] = tuple(dict(
                        source=item['path'], native_path=str((load_path.parent/Path(item['path']).relative_to(group.font_path(entry).resolve().parent))
                            if staged is not None else item['path']), sha256=item['sha256']) for item in consumed)
                check_cancel()
                if not self.bassmidi.BASS_MIDI_FontSetVolume(handle, float(entry.volume) / 100):
                    raise SoundFontRuntimeError(tr(
                        "runtime.font_volume_failed",
                        filename=entry.filename,
                        error=self.bass.BASS_ErrorGetCode(),
                    ))
                handles.append(handle)
                if self._configure_chord_sources is not None and handle not in self._chord_pitch_metadata:
                    # Bounded file metadata only,inside existing source prepare;
                    # no observation/callback/GUI repaint performs file parsing.
                    report=inspect_soundfont_pitch(group.font_path(entry),lambda:cancel is not None and cancel.is_set())
                    check_cancel()
                    self._chord_pitch_metadata[handle]=report.get('unpitched',frozenset()) if report['state']=='ready' else frozenset()
                if handle in original_volumes:
                    changed_reused.add(handle)
                if progress is not None:
                    progress("font_done", number, len(group.fonts), entry.filename)

            loading_mode = self.engine_preferences().loading_mode
            if loading_mode == 2:
                unique_handles = list(dict.fromkeys(handles))
                sizes = []
                for handle in unique_handles:
                    info = BASS_MIDI_FONTINFO()
                    known = bool(self.bassmidi.BASS_MIDI_FontGetInfo(handle, ctypes.byref(info)))
                    sizes.append(int(info.samsize) if known and info.samsize > 0 and info.samload != 0xffffffff else 1)
                total_units = sum(sizes) or 1
                completed_units = 0
                for number, handle in enumerate(unique_handles, 1):
                    check_cancel()
                    filename = group.fonts[handles.index(handle)].filename
                    if progress is not None:
                        progress("samples", number, len(unique_handles), filename)
                    # bassmidi.h/.md: NOWAIT returns after metadata parsing,
                    # then reads sample data in BASSMIDI's worker. Polling the
                    # font's loaded bytes gives Cancel a 50 ms observation
                    # boundary rather than a whole-font blocking boundary.
                    loaded = self.bassmidi.BASS_MIDI_FontLoadEx(
                        handle, -1, -1, 0, BASS_MIDI_FONTLOAD_NOWAIT,
                    )
                    if not loaded:
                        raise SoundFontRuntimeError(tr(
                            "runtime.font_preload_failed",
                            error=self.bass.BASS_ErrorGetCode(),
                        ))
                    while True:
                        check_cancel()
                        info = BASS_MIDI_FONTINFO()
                        if not self.bassmidi.BASS_MIDI_FontGetInfo(handle, ctypes.byref(info)):
                            raise SoundFontRuntimeError(tr(
                                "runtime.font_preload_failed", error=self.bass.BASS_ErrorGetCode(),
                            ))
                        read = int(info.samload)
                        size = int(info.samsize)
                        if progress is not None and size > 0 and read != 0xffffffff:
                            progress("sample_bytes", completed_units + min(read, sizes[number - 1]), total_units, filename)
                        # samsize can include data that full preload does not
                        # retain (verified on soundfont.sf2: 170893014 loaded
                        # of 171006536 bytes). The active-reader count, not
                        # equality with samsize, is the completion signal.
                        if (read == 0xffffffff or read >= size > 0 or
                                not self.bass.BASS_GetConfig(BASS_CONFIG_MIDI_SAMPLELOADING)):
                            break
                        if size == 0:
                            wait_for_sample_reads()
                            break
                        if cancel is None:
                            time.sleep(0.05)
                        else:
                            cancel.wait(0.05)
                    check_cancel()
                    completed_units += sizes[number - 1]
                    if progress is not None:
                        progress("sample_bytes", completed_units, total_units, filename)
            elif loading_mode == 1:
                if progress is not None:
                    progress("used_samples", 0, 0, "")
                required = [item for item in self.effective_presets(group)
                            if item.index is not None and not item.disabled]
                # Explicit LSB routes refer to different source addresses.
                # Prepare those sources on this existing load worker,not a
                # MIDI callback or guessed legacy destination preload key.
                for route in group.mappings:
                    check_cancel();handle=handles[route.source_index]
                    if self.bassmidi.BASS_MIDI_FontLoadEx(handle,route.source_program,
                            route.source_bank,0,BASS_MIDI_FONTLOAD_NOWAIT):
                        staged_preloaded.add((handle,route.source_bank,route.source_program))
                for number, item in enumerate(required, 1):
                    check_cancel()
                    handle = handles[item.index]
                    if self.bassmidi.BASS_MIDI_FontLoadEx(
                            handle, item.preset, item.bank, 0, BASS_MIDI_FONTLOAD_NOWAIT):
                        if self.preloaded_used is not None:
                            staged_preloaded.add((handle, item.bank, item.preset))
                    if progress is not None:
                        progress("used_samples_done", number, len(required), "")
                    check_cancel()
                wait_for_sample_reads()
            check_cancel()
            # Reject observed replacement during prepare, before remapping/free.
            if any(identity['path'].lower().endswith('.sfz') for identity in identities):
                header_pointer = self.bass.BASS_GetConfigPtr(BASS_CONFIG_MIDI_SFZHEAD)
                current_header = ctypes.string_at(header_pointer) if isinstance(header_pointer,int) and header_pointer else b''
                if current_header != sfz_header:
                    raise SoundFontRuntimeError(tr('player.asset_changed',path=identities[0]['path']))
            for identity in identities:
                reader = soundfont_asset_identity if identity['path'].lower().endswith('.sfz') else asset_file_identity
                if reader(identity['path'],lambda:cancel is not None and cancel.is_set(),
                          **({'sfz_header':sfz_header} if reader is soundfont_asset_identity else {}))!=identity:
                    raise SoundFontRuntimeError(tr('player.asset_changed',path=identity['path']))
            if progress is not None:
                progress("mapping", 0, 0, "")
            check_cancel()
            # All slow identity/font/sample preparation above is outside the
            # FIFO pause. The lease spans only mapping and ownership commit.
            with self.source_commit_scope(group):
                self.retire_first_note_preparations()
                effective, explicit_count = self.set_mapping(group, handles)
                mapping_published=True
                self.source_commit_uncertain=True
                if self.preloaded_used is not None:
                    self.preloaded_used.clear();self.preloaded_used.update(staged_preloaded)
                retained = set(handles)
                self.free_handles([handle for handle in self.handles if handle not in retained])
                self.handles = list(handles)
                committed=True
                self.asset_identities=tuple(dict(item) for item in identities)
                self.source_generation+=1
                self._remember_group(group)
                self.configure_preset_controls(effective)
                if self._configure_available_presets is not None:
                    self._configure_available_presets(self.available_preset_keys(handles))
                self.source_commit_uncertain=False
        except Exception as exc:
            if not mapping_published and self.preloaded_used is not None:
                self.preloaded_used.update(previous_preloaded)
            if not mapping_published:
                for handle in changed_reused:
                    self.bassmidi.BASS_MIDI_FontSetVolume(handle, original_volumes[handle])
            # A scope-exit republish failure after a successful commit retains
            # the new mapping/fonts. Never free its current source handles.
            if not mapping_published:
                self.free_handles([handle for handle in created if handle not in self.handles])
            elif not committed:
                # Mapping already references these sources even if retiring an
                # obsolete handle failed. Retain ownership for recovery rather
                # than freeing active mapped fonts in the precommit cleanup.
                self._retired_handles.update(created)
            if cancel is not None and cancel.is_set() and not isinstance(exc,SoundFontLoadCancelled):
                raise SoundFontLoadCancelled() from exc
            raise

        return SoundFontLoadResult(len(handles), explicit_count)

    def apply_runtime(self, group, change):
        if not self.stream_handle():
            return False
        if (change=='font_mapping' and group.mappings!=self.group.mappings
                and self.engine_preferences().loading_mode==1):
            # The current native request ABI keys first-use by MSB/Program,
            # not LSB. New sources therefore prepare on the existing font
            # load worker before publication,never a GUI/MIDI callback.
            return None
        supported=(change in ('performance','font_mapping') if isinstance(change,str) else
                   isinstance(change,tuple) and change[:1] in (('preset_batch',),('font_volume',),('preset_controls',)))
        if not supported or not self.same_layout(group):return None
        if change=='font_mapping':self._mapping_sources(group,self.handles)
        tone_result=self._apply_runtime_tone(group)
        if tone_result is not None:return tone_result
        if self.scalar_scope is not None:
            with self.scalar_scope() as admitted:
                if not admitted or self.source_commit_uncertain:return False
                if not self.same_layout(group):return None
                scalar=self._apply_runtime_scalar(group,change)
                if scalar is not None:return scalar
        controls_only=change!='font_mapping' and self.control_commit_scope is not None
        with (self.control_commit_scope() if controls_only else self.commit_scope()):
            if not controls_only:self.retire_first_note_preparations()
            if not controls_only:self.source_commit_uncertain=True
            result=self._apply_runtime_committed(group,change)
            if not controls_only:self.source_commit_uncertain=False
            return result

    @staticmethod
    def _runtime_audio_signature(group, *, omit_font_volume=None):
        """Exact document audio fields; Memo/source spelling are presentation.

        Keep every current PresetOverride field except Memo, including routing,
        disabled generators and performance seeds. Future fields are included
        automatically rather than accidentally classified as metadata-only.
        same_layout separately verifies resolved source identity/order.
        """
        return (tuple(None if index==omit_font_volume else entry.volume
                      for index,entry in enumerate(group.fonts)),group.performance,
                tuple({name:value for name,value in vars(item).items() if name!='memo'}
                      for item in group.presets),tuple(group.mappings))

    def _apply_runtime_scalar(self, group, change):
        """Qualified cheap edits; None retains the full semantic commit path."""
        font_volume=isinstance(change,tuple) and change[:1]==('font_volume',)
        if font_volume:
            if len(change)!=2 or type(change[1]) is not int:return False
            index=change[1]
            if not 0<=index<len(group.fonts) or index>=len(self.handles):return False
        # An explicit preset_controls payload can differ from the document.
        # Do not elide that original writer merely because the draft is equal.
        explicit_matches=(not (isinstance(change,tuple) and change[:1]==('preset_controls',)
                              and len(change)>3) or
                          change[3]==group.preset_map().get((change[1],change[2])))
        if explicit_matches and self._runtime_audio_signature(group)==self._runtime_audio_signature(self.group):
            self.group=self.copy_group(group)
            self._preset_positions={(item.bank,item.preset):index
                                    for index,item in enumerate(self.group.presets)}
            return True
        if font_volume and (self._runtime_audio_signature(group,omit_font_volume=index)==
                            self._runtime_audio_signature(self.group,omit_font_volume=index)):
            # bassmidi.h BOOL FontSetVolume(HSOUNDFONT,float); current manual:
            # gain takes immediate effect in all using streams. No font/map,
            # sample ownership or first-use preparation plan changes here.
            if not self.bassmidi.BASS_MIDI_FontSetVolume(self.handles[index],float(group.fonts[index].volume)/100):
                return False
            self.group.fonts[index].volume=int(group.fonts[index].volume)
            return True
        return None

    def _apply_runtime_committed(self, group, change):
        """Original same-layout writers, inside the shared source FIFO lease."""
        if change == "performance" and self.same_layout(group):
            self._remember_group(group)
            self.configure_preset_controls(self.effective_presets(group))
            return True
        if isinstance(change, tuple) and change[:1] == ("preset_batch",) and self.same_layout(group):
            # One model commit, no per-row catalog enumeration/global reseed.
            keys = tuple(change[1])
            self.group = self.copy_group(group)
            self._preset_positions = {(item.bank, item.preset): index for index, item in enumerate(self.group.presets)}
            items = group.preset_map()
            if self._update_preset_control is not None:
                for key in keys:
                    item = items.get(key)
                    self._update_preset_control(*key, None if item is None else {
                        field: getattr(item, field) for field in SoundFontPresetRouting.OVERRIDE_KEYS})
            else:
                self.configure_preset_controls(self.effective_presets(group))
            return True
        if isinstance(change, tuple) and change[:1] == ("font_volume",) and self.same_layout(group):
            index = change[1]
            if 0 <= index < len(group.fonts) and self.bassmidi.BASS_MIDI_FontSetVolume(
                    self.handles[index], float(group.fonts[index].volume) / 100):
                self.group.fonts[index].volume = int(group.fonts[index].volume)
                return True
            return False
        if isinstance(change, tuple) and change[:1] == ("preset_controls",) and self.same_layout(group):
            key = change[1], change[2]
            item = change[3] if len(change) > 3 else group.preset_map().get(key)
            if self._update_preset_control is not None:
                self._update_preset_control(key[0], key[1], None if item is None else {
                    field: getattr(item, field) for field in SoundFontPresetRouting.OVERRIDE_KEYS
                })
            else:
                self.configure_preset_controls(self.effective_presets(group))
            position = self._preset_positions.get(key)
            if position is None:
                if item is not None:
                    self._preset_positions[key] = len(self.group.presets)
                    self.group.presets.append(PresetOverride(**vars(item)))
            elif item is None:
                self.group.presets.pop(position)
                self._preset_positions = {
                    (entry.bank, entry.preset): index
                    for index, entry in enumerate(self.group.presets)
                }
            else:
                self.group.presets[position] = PresetOverride(**vars(item))
            return True
        if change == "font_mapping" and self.same_layout(group):
            effective, _count = self.set_mapping(group, self.handles)
            self._remember_group(group)
            self.configure_preset_controls(effective)
            return True
        return None  # Caller owns the full-load presentation path.

    @staticmethod
    def _preset_name_encoding(names):
        """Choose one legacy encoding per font, not per preset name.

        SF2 preset names are byte fields with no charset flag. BASSMIDI returns
        those bytes unchanged; a locally authored Thai SF2 uses CP874, while
        many Korean files use CP949. Mixed-script/invalid decodes rank below
        a consistent printable script. UTF-8 wins when genuinely valid.
        """
        non_ascii = [name for name in names if name and any(b >= 128 for b in name)]
        if not non_ascii:
            return "utf-8"
        if all(_decodes_cleanly(name, "utf-8") for name in non_ascii):
            return "utf-8"
        if all(_decodes_cleanly(name, "cp949") for name in non_ascii):
            korean = [name.decode("cp949") for name in non_ascii]
            if all(all(ord(char) < 128 or "\uac00" <= char <= "\ud7a3" for char in name)
                   for name in korean):
                return "cp949"
        western = [name.decode("cp1252", "replace") for name in non_ascii]
        if all(all(ord(char) < 128 or (unicodedata.category(char).startswith("L")
                    and "LATIN" in unicodedata.name(char, "")) for char in name)
               for name in western):
            return "cp1252"
        candidates = ("cp949", "cp1252", "cp1251", "cp874", "cp932", "gb18030")

        def score(encoding):
            result = 0.0
            scripts = set()
            for raw in non_ascii:
                decoded = raw.decode(encoding, "replace")
                for char in decoded:
                    if char == "\ufffd" or unicodedata.category(char).startswith("C"):
                        result -= 15
                    elif "\u0e00" <= char <= "\u0e7f":
                        scripts.add("thai"); result += 2
                    elif "\uac00" <= char <= "\ud7a3":
                        scripts.add("hangul"); result += 2
                    elif "\u3040" <= char <= "\u30ff":
                        scripts.add("japanese"); result += 2
                    elif "\u4e00" <= char <= "\u9fff":
                        scripts.add("han"); result += 1.5
                    elif "\u0400" <= char <= "\u052f":
                        scripts.add("cyrillic"); result += 2
                    elif ord(char) >= 128 and unicodedata.category(char).startswith("L"):
                        scripts.add("latin"); result += 1.5
            return result - 8 * max(0, len(scripts) - 1)

        return max(candidates, key=score)

    @staticmethod
    def decode_preset_name(value, encoding="utf-8"):
        if not value:
            return tr("group.empty_name")
        return value.decode(encoding, errors="replace")

    def active_channel_names(self, port=None):
        """Return BASSMIDI's actually mapped presets, not guessed GM names.

        bassmidi.h/md: StreamGetPreset may be unavailable until the channel
        plays a note. Its FONT triple may differ from incoming Bank/Program.
        Cache decoded labels per stream font generation; clear on group swap.
        """
        stream = self.stream_handle()
        if not stream:
            return (None,) * 16
        count = ctypes.c_float(16)
        self.bass.BASS_ChannelGetAttribute(stream, BASS_ATTRIB_MIDI_CHANS, ctypes.byref(count))
        channels = range(max(16, min(128, int(count.value)))) if port is None else range(port * 16, (port + 1) * 16)
        names = []
        for channel in channels:
            active = BASS_MIDI_FONT()
            if not self.bassmidi.BASS_MIDI_StreamGetPreset(
                    stream, channel, ctypes.byref(active)) or not active.font:
                names.append(None)
                continue
            key = (active.font, active.preset, active.bank)
            if key not in self._active_name_cache:
                encoding = self._font_encoding_cache.get(active.font)
                if encoding is None:
                    info = BASS_MIDI_FONTINFO()
                    raw_names = None
                    if self.bassmidi.BASS_MIDI_FontGetInfo(active.font, ctypes.byref(info)) and info.presets:
                        packed = (ctypes.c_uint * info.presets)()
                        if self.bassmidi.BASS_MIDI_FontGetPresets(active.font, packed):
                            raw_names = [self.bassmidi.BASS_MIDI_FontGetPreset(
                                active.font, item & 0xffff, (item >> 16) & 0xffff)
                                for item in packed]
                    # A failed/partial metadata read must not become a
                    # permanent UTF-8 decision for a legacy-encoded font.
                    if not raw_names or any(name is None for name in raw_names):
                        names.append(None)
                        continue
                    encoding = self._preset_name_encoding(raw_names)
                    self._font_encoding_cache[active.font] = encoding
                raw_name = self.bassmidi.BASS_MIDI_FontGetPreset(
                    active.font, active.preset, active.bank)
                if raw_name is not None:
                    self._active_name_cache[key] = self.decode_preset_name(raw_name, encoding)
            names.append(self._active_name_cache.get(key))
        return tuple(names)

    def enumerate_instruments(self, group=None):
        group = group or self.group
        temporary = []
        if self.same_layout(group):
            handles = list(self.handles)
        else:
            handles = []
            for entry in group.fonts:
                handle = self.bassmidi.BASS_MIDI_FontInit(
                    ctypes.c_wchar_p(str(group.font_path(entry))), BASS_UNICODE,
                )
                handles.append(handle)
                if handle:
                    temporary.append(handle)
        result = []
        try:
            for index, handle in enumerate(handles):
                if not handle:
                    continue
                info = BASS_MIDI_FONTINFO()
                if not self.bassmidi.BASS_MIDI_FontGetInfo(handle, ctypes.byref(info)) or not info.presets:
                    continue
                raw = (ctypes.c_uint * info.presets)()
                if not self.bassmidi.BASS_MIDI_FontGetPresets(handle, raw):
                    continue
                presets = [(packed & 0xffff, (packed >> 16) & 0xffff) for packed in raw]
                names = [self.bassmidi.BASS_MIDI_FontGetPreset(handle, preset, bank)
                         for preset, bank in presets]
                encoding = self._preset_name_encoding(names)
                for (preset, bank), raw_name in zip(presets, names):
                    name = (group.fonts[index].custom['name'] if index<len(group.fonts) and group.fonts[index].custom is not None
                            else self.decode_preset_name(raw_name, encoding))
                    result.append({
                        "bank": bank,
                        "preset": preset,
                        "source_index": index,
                        "name": name,
                        "filename": group.fonts[index].label
                        if index < len(group.fonts) else f"SoundFont {index + 1}",
                    })
        finally:
            self.free_handles(temporary)
        return sorted(result, key=lambda item: (item["bank"], item["preset"], item["source_index"]))

    def preload_used_preset(self, bank: int, preset: int) -> None:
        """First-use loading policy for the current channel program."""
        if self.engine_preferences().loading_mode != 1:
            return
        for route in self.group.mappings:
            if (route.bank,route.program)!=(bank,preset):continue
            handle=self.handles[route.source_index];key=(handle,route.source_bank,route.source_program)
            if self.preloaded_used is not None and key in self.preloaded_used:continue
            if self.bassmidi.BASS_MIDI_FontLoad(handle,route.source_program,route.source_bank):
                if self.preloaded_used is not None:self.preloaded_used.add(key)
        mapping = self.group.preset_map().get((bank, preset))
        candidates = (
            [mapping.index] if mapping is not None and mapping.index is not None and not mapping.disabled
            else range(len(self.handles))
        )
        for index in candidates:
            if index is None or not 0 <= index < len(self.handles):
                continue
            handle = self.handles[index]
            key = (handle, bank, preset)
            alias = self._preload_single_source_aliases.get(key)
            source_key = alias or key
            if self.preloaded_used is not None and source_key in self.preloaded_used:
                return
            if alias is not None:
                if self.bassmidi.BASS_MIDI_FontLoad(handle, alias[2], alias[1]):
                    if self.preloaded_used is not None:
                        self.preloaded_used.add(alias)
                return
            if self.bassmidi.BASS_MIDI_FontLoad(handle, preset, bank):
                if self.preloaded_used is not None:
                    self.preloaded_used.add(key)
                return
            # bass.h NOTAVAIL=37 and bassmidi.md FONT: an individual
            # destination absent from its font uses that font's first preset.
            # Only a singleton native catalogue proves that source without
            # assuming file order or using a prior Note's StreamGetPreset.
            # Keep multi-preset/bank-LSB/general fallback paths unchanged.
            explicit = mapping is not None and mapping.index is not None and not mapping.disabled
            if explicit and int(self.bass.BASS_ErrorGetCode()) == 37:
                info = BASS_MIDI_FONTINFO()
                if (self.bassmidi.BASS_MIDI_FontGetInfo(handle, ctypes.byref(info)) and
                        info.presets == 1):
                    packed = (ctypes.c_uint * 1)()
                    if self.bassmidi.BASS_MIDI_FontGetPresets(handle, packed):
                        actual = (handle, (packed[0] >> 16) & 0xffff, packed[0] & 0xffff)
                        if actual != key:
                            self._preload_single_source_aliases[key] = actual
                            if self.preloaded_used is not None and actual in self.preloaded_used:
                                return
                            if self.bassmidi.BASS_MIDI_FontLoad(handle, actual[2], actual[1]):
                                if self.preloaded_used is not None:
                                    self.preloaded_used.add(actual)
                            return

    def close(self):
        if not self.finish_runtime_controls():raise SoundFontRuntimeError(tr('runtime.preset_controls_pending'))
        with self._control_condition:
            self._control_stopping=True;self._control_condition.notify_all()
        if self._control_thread is not None:self._control_thread.join()
        self._control_thread=None;self._control_stopping=False
        self.retire_first_note_preparations()
        self._preparation_generation += 1
        self.free_handles((*self.handles,*self._retired_handles,*((self._reference_font,) if self._reference_font else ())))
        self._reference_font=0
        self.handles = []
        self.asset_identities=()
        self.source_generation+=1
        self._active_name_cache.clear()
        self._font_encoding_cache.clear()
        self._preload_single_source_aliases.clear()
        self.group = SoundFontGroup()
        self._preset_positions = {}
        if self._configure_chord_sources is not None:self._configure_chord_sources({})
        if self._configure_available_presets is not None:
            self._configure_available_presets(set())
        if self.preloaded_used is not None:
            self.preloaded_used.clear()
        return not self._retired_handles


# MIDI preset routing and instrument-row activity share this runtime boundary.

class SoundFontActivity:
    # Source rates are per 20ms tick; ABMSynth interpolates them continuously.
    TICK_SECONDS = 0.020
    ONSET_STRENGTH = 100
    HELD_STRENGTH = 58
    HELD_DROP_PER_TICK = 3
    RELEASE_DROP_PER_TICK = 10

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.active_notes = {}
        # One MIDI key may have multiple outstanding Note Ons. Keep every
        # onset's preset identity, including across a later Program Change.
        self.note_rows = {}
        self.played_rows = set()
        self.visual_rows = {}
        self.release_channels = {}
        self.available_presets = None

    def set_available_presets(self, keys):
        with self.lock:
            self.available_presets = set(keys)

    def _played_preset(self, bank, program):
        """BMSynth 0046FF2C: resolve absent variations before recording."""
        if self.available_presets is None:
            return bank, program
        if bank >= 128:
            return (128, program) if (128, program) in self.available_presets else (
                (128, 0) if (128, 0) in self.available_presets else None
            )
        if (bank, program) in self.available_presets:
            return bank, program
        return (0, program) if (0, program) in self.available_presets else None

    def playing_state(self):
        with self.lock:
            return {row: count for row, count in self.active_notes.items() if count > 0}

    def played_state(self):
        with self.lock:
            return set(self.played_rows)

    def clear_played_history(self):
        """Forget history without stopping notes; the next Note On may re-add it.

        Deliberate ABMSynth UX: unlike BMSynth's channel dirty flag, clearing
        the list needs no Program Change before the same preset is recorded.
        """
        with self.lock:
            self.played_rows.clear()

    def visual_state(self, now=None):
        current = self.clock() if now is None else float(now)
        with self.lock:
            result, expired = {}, []
            for row, state in self.visual_rows.items():
                strength = self._strength_at(state, current)
                if state[0] == 0 and strength == 0:
                    expired.append(row)
                elif strength > 0:
                    result[row] = strength / self.ONSET_STRENGTH
            for row in expired:
                self.visual_rows.pop(row, None)
                self.release_channels.pop(row, None)
            return result

    @classmethod
    def _strength_at(cls, state, now):
        return midi_activity_strength_at(state,now,held_strength=cls.HELD_STRENGTH,
            held_rate=cls.HELD_DROP_PER_TICK/cls.TICK_SECONDS,
            release_rate=cls.RELEASE_DROP_PER_TICK/cls.TICK_SECONDS)

    def note_on(self, channel, note, bank, program, velocity=127, *, now=None):
        row = (bank, program)
        with self.lock:
            self.active_notes[row] = self.active_notes.get(row, 0) + 1
            self.note_rows.setdefault((channel, note), deque()).append(row)
            # BMSynth 00476F28/00477C28 records only a velocity >= 5 Note On
            # whose current bank/program resolves to a loaded font preset.
            if velocity >= 5:
                played = self._played_preset(bank, program)
                if played is not None:
                    self.played_rows.add(played)
            now = self.clock() if now is None else float(now)
            state = self.visual_rows.get(row, (0, now, 0))
            strength = self.ONSET_STRENGTH if velocity >= 5 else self._strength_at(state, now)
            # Source velocity <5 contributes to held activity but does not flash.
            self.visual_rows[row] = (state[0] + 1, now, strength)
            self.release_channels.pop(row, None)

    def note_off(self, channel, note, bank, program, *, now=None):
        with self.lock:
            pending = self.note_rows.get((channel, note))
            if not pending:
                # An unmatched Note Off cannot end a voice in another row.
                return
            row = pending.popleft()
            if not pending:
                self.note_rows.pop((channel, note), None)
            count = self.active_notes.get(row, 0) - 1
            if count > 0:
                self.active_notes[row] = count
            else:
                self.active_notes.pop(row, None)
            now = self.clock() if now is None else float(now)
            state = self.visual_rows.get(row, (0, now, 0))
            remaining = max(0, state[0] - 1)
            self.visual_rows[row] = (remaining, now, self._strength_at(state, now))
            if not remaining:
                self.release_channels[row] = channel

    def clear_channel(self, channel):
        """All Notes/Sound Off is immediate, unlike a musical Note Off."""
        with self.lock:
            note_keys = [key for key in self.note_rows if key[0] == channel]
            for note_key in note_keys:
                for row in self.note_rows.pop(note_key):
                    count = self.active_notes.get(row, 0) - 1
                    if count > 0:
                        self.active_notes[row] = count
                    else:
                        self.active_notes.pop(row, None)
                    now = self.clock()
                    state = self.visual_rows.get(row, (0, now, 0))
                    remaining = max(0, state[0] - 1)
                    if remaining:
                        self.visual_rows[row] = (remaining, now, self._strength_at(state, now))
                    else:
                        self.visual_rows.pop(row, None)
                        self.release_channels.pop(row, None)
            for row, owner in tuple(self.release_channels.items()):
                if owner == channel:
                    self.visual_rows.pop(row, None)
                    self.release_channels.pop(row, None)

    def clear_all(self):
        with self.lock:
            self.active_notes.clear()
            self.note_rows.clear()
            self.played_rows.clear()
            self.visual_rows.clear()
            self.release_channels.clear()

    def clear_active(self):
        """A MIDI reset stops voices but does not erase the played-history list."""
        with self.lock:
            self.active_notes.clear()
            self.note_rows.clear()
            self.visual_rows.clear()
            self.release_channels.clear()


class SoundFontPresetRouting:
    """Own ordered group overrides and native-address preset/controller state."""

    CONTROL_EVENTS = (
        ("attack", MIDI_EVENT_ATTACK),
        ("decay", MIDI_EVENT_DECAY),
        ("release", MIDI_EVENT_RELEASE),
        ("cutoff", MIDI_EVENT_CUTOFF),
        ("resonance", MIDI_EVENT_RESONANCE),
    )
    OVERRIDE_KEYS = (
        "volume", "attack", "decay", "release", "cutoff", "resonance", "disabled", "extended", "extended_disabled", "performance",
    )

    def __init__(self, bassmidi, stream_getter):
        self.bassmidi = bassmidi
        self.stream_getter = stream_getter
        self.presets = {}
        self.channels = lambda: range(16)
        self.is_drum = lambda channel: channel % 16 == 9
        self.bank_msb = [0] * MIDI_CHANNEL_LIMIT
        self.bank_lsb = [0] * MIDI_CHANNEL_LIMIT
        self.program = [0] * MIDI_CHANNEL_LIMIT
        self.controllers = {key: [64] * MIDI_CHANNEL_LIMIT for key, _event in self.CONTROL_EVENTS}
        self.after_apply = None
        self.on_emit = None
        self.extension_count = 0
        self.performance = None
        self._performance_revision = 0
        self._performance_tokens = {}
        self._performance_key_revisions = {}
        self.native_selection_revision = 0

    def performance_seed(self, channel):
        key = self.preset_key(channel)
        token = (self._performance_revision, key, self._performance_key_revisions.get(key, 0),
                 self.bank_lsb[channel], self.is_drum(channel))
        previous = self._performance_tokens.get(channel)
        changed = previous != token
        force = previous is None or (previous[1], previous[3], previous[4]) != (key, self.bank_lsb[channel], self.is_drum(channel))
        self._performance_tokens[channel] = token
        return changed, self.performance_for(channel), force

    def performance_for(self, channel):
        override = self.override(channel)
        exception = {} if override.get("disabled") else override.get("performance") or {}
        if self.performance is None and not exception:
            return None
        return {**(self.performance or {}), **exception}

    def configure(self, overrides):
        self._performance_revision += 1
        self.native_selection_revision += 1
        self.presets = {
            (int(bank), int(preset)): {
                key: dict(item.get(key) or {}) if key in ("extended", "performance") else set(item.get(key) or ())
                    if key == "extended_disabled" else item.get(key) for key in self.OVERRIDE_KEYS
            }
            for (bank, preset), item in overrides.items()
        }
        self.extension_count = sum(bool(effective_preset_expression(item))
                                   for item in self.presets.values())
        for channel in self.channels():
            self.apply_channel(channel)

    def update_one(self, bank, preset, override):
        self.native_selection_revision += 1
        key = int(bank), int(preset)
        previous_extended = bool(effective_preset_expression(self.presets.get(key, {})))
        self._performance_key_revisions[key] = self._performance_key_revisions.get(key, 0) + 1
        if override is None:
            self.presets.pop(key, None)
        else:
            self.presets[key] = {field: dict(override.get(field) or {}) if field in ("extended", "performance") else
                                 set(override.get(field) or ()) if field == "extended_disabled" else override.get(field)
                                 for field in self.OVERRIDE_KEYS}
        self.extension_count += int(bool(effective_preset_expression(self.presets.get(key, {})))) - int(previous_extended)
        self.reapply_key(*key)

    def bank(self, channel):
        # BMSynth uses SF2 drum bank 128 for channel 10 independently of CC0.
        return 128 if self.is_drum(channel) else self.bank_msb[channel]

    def source(self, channel):
        """Resolved SoundFont identity is not the incoming MIDI bank address.

        StreamGetPreset resolves bank-LSB/fallback/source mapping without
        guessing that every destination is SF2 bankMSB/program. It may fail
        with no mapped font; in that case expose no fabricated source.
        """
        stream = self.stream_getter()
        active = BASS_MIDI_FONT()
        if stream and self.bassmidi.BASS_MIDI_StreamGetPreset(stream, channel, ctypes.byref(active)) and active.font:
            return int(active.font), int(active.bank), int(active.preset)
        return None

    def override(self, channel):
        # Legacy SFG keys address bankLSB0. Preserve its exact first-port
        # request-key policy; nonzero LSB uses the actually resolved source.
        key = self.preset_key(channel)
        return self.presets.get(key, {})

    def preset_key(self, channel):
        key = self.bank(channel), self.program[channel]
        if self.bank_lsb[channel]:
            source = self.source(channel)
            if source is None:
                return None
            key = source[1:]
        return key

    @staticmethod
    def blend(base, controller):
        """BMSynth 004767E0: centered-64 controller interpolation."""
        base = max(0, min(127, int(base)))
        controller = max(0, min(127, int(controller)))
        if controller == 64:
            return base
        if controller < 64:
            return (controller * base) // 64
        return base + (((controller - 63) * (127 - base)) // 64)

    def apply_channel(self, channel):
        stream = self.stream_getter()
        if not stream or not 0 <= channel < MIDI_CHANNEL_LIMIT:
            return
        override = self.override(channel)
        # Disabled instrument settings retain their saved values for later
        # re-enabling, but neither source selection nor per-preset controls
        # affect the sounding channel while the switch is checked.
        if override.get("disabled"):
            override = {}
        volume = override.get("volume")
        mixlevel = 100 if volume is None else max(0, min(INSTRUMENT_VOLUME_MAX, int(volume)))
        # 004762A0 -> 00477D2C uses MIXLEVEL, not channel CC#7 volume.
        if self.bassmidi.BASS_MIDI_StreamEvent(stream, channel, MIDI_EVENT_MIXLEVEL, mixlevel):
            if self.on_emit is not None:
                self.on_emit(channel, MIDI_EVENT_MIXLEVEL, mixlevel,
                             "soundfont_group" if volume is not None else "preset_default")
        for field, event in self.CONTROL_EVENTS:
            configured = override.get(field)
            base = 64 if configured is None else max(0, min(127, int(configured)))
            value = self.blend(base, self.controllers[field][channel])
            if self.bassmidi.BASS_MIDI_StreamEvent(stream, channel, event, value):
                if self.on_emit is not None:
                    self.on_emit(channel, event, value,
                                 "soundfont_group" if configured is not None else
                                 "native_midi" if self.controllers[field][channel] != 64 else "preset_default")

        if self.after_apply is not None:
            self.after_apply(channel)

    def observe_program(self, channel, program):
        """Record parser order before a combined Program+Note packet is sent."""
        self.program[channel] = program
        self._performance_tokens.pop(channel, None)

    def observe_bank_msb(self, channel, bank):
        self.bank_msb[channel] = bank

    def observe_bank_lsb(self, channel, bank):
        self.bank_lsb[channel] = bank

    def set_program(self, channel, program):
        if 0 <= channel < MIDI_CHANNEL_LIMIT:
            self._performance_tokens.pop(channel, None)
            self.program[channel] = max(0, min(127, int(program)))
            self.apply_channel(channel)

    def set_bank_msb(self, channel, bank):
        if 0 <= channel < MIDI_CHANNEL_LIMIT:
            self.bank_msb[channel] = max(0, min(127, int(bank)))
            self.apply_channel(channel)

    def set_bank_lsb(self, channel, bank):
        if 0 <= channel < MIDI_CHANNEL_LIMIT:
            self.bank_lsb[channel] = max(0, min(127, int(bank)))
            self.apply_channel(channel)

    def reapply_key(self, bank, preset):
        if not self.stream_getter():
            return
        bank = max(0, min(128, int(bank)))
        preset = max(0, min(127, int(preset)))
        for channel in self.channels():
            if self.preset_key(channel) == (bank, preset):
                self.apply_channel(channel)

    def reset_channels(self):
        for channel in self.channels():
            self.reset_channel(channel)

    def reset_channel(self, channel):
        if not 0 <= channel < MIDI_CHANNEL_LIMIT:
            raise ValueError("channel")
        self.bank_msb[channel] = 0
        self.bank_lsb[channel] = 0
        self.program[channel] = 0
        self._performance_tokens.pop(channel, None)
        for values in self.controllers.values():
            values[channel] = 64

