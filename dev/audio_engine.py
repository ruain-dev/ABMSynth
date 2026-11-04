# audio_engine.py
import ctypes
import queue
import time
import math
import threading
from itertools import count
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
from dataclasses import dataclass
from collections.abc import Mapping
import numpy as np
from event_log import log_text
from i18n import tr
from settings import bmsynth_global_position_to_midi_send
from bass_bindings import (
    bass, bassmidi,
    DSPPROC, MIDIFILTERPROC, SYNCPROC, BASS_MIDI_EVENTS_FILTER, BASS_MIDI_EVENTS_SYNC,
    BASS_ATTRIB_MIDI_VOICES_ACTIVE, BASS_ATTRIB_TAIL, BASS_FX_DX8_ECHO, BASS_FX_DX8_FLANGER,
    MIDI_EVENT_PROGRAM, MIDI_EVENT_VOLUME, MIDI_EVENT_PAN, MIDI_EVENT_PITCH,
    MIDI_EVENT_MODULATION, MIDI_EVENT_BANK, MIDI_EVENT_MASTERVOL,
    MIDI_EVENT_BANK_LSB, MIDI_EVENT_DRUMS, MIDI_EVENT_DEFDRUMS, BASS_ATTRIB_MIDI_CHANS,
    MIDI_EVENT_SUSTAIN, MIDI_EVENT_SOSTENUTO, MIDI_EVENT_SOFT, MIDI_EVENT_CHANPRES, MIDI_EVENT_KEYPRES,
    MIDI_EVENT_ATTACK, MIDI_EVENT_MIXLEVEL, MIDI_EVENT_CUTOFF, MIDI_EVENT_RESONANCE,
    MIDI_EVENT_RELEASE, MIDI_EVENT_DECAY, MIDI_EVENT_MODE,
    MIDI_EVENT_REVERB, MIDI_EVENT_CHORUS, MIDI_EVENT_USERFX, MIDI_EVENT_CONTROL, MIDI_EVENT_REVERB_MACRO,
    MIDI_EVENT_CHORUS_MACRO, MIDI_EVENT_CHORUS_DELAY, MIDI_EVENT_CHORUS_DEPTH,
    MIDI_EVENT_CHORUS_RATE, MIDI_EVENT_CHORUS_FEEDBACK, MIDI_EVENT_CHORUS_REVERB,
    MIDI_EVENT_REVERB_LEVEL, MIDI_EVENT_REVERB_TIME, MIDI_EVENT_REVERB_DELAY,
    MIDI_EVENT_REVERB_LOCUTOFF, MIDI_EVENT_REVERB_HICUTOFF, MIDI_EVENT_CHORUS_LEVEL,
    MIDI_EVENT_SOUNDOFF, MIDI_EVENT_RESET, MIDI_EVENT_NOTESOFF, MIDI_EVENT_SYSTEM, MIDI_EVENT_SYSTEMEX,
    MIDI_SYSTEM_GM1, MIDI_SYSTEM_GM2, MIDI_SYSTEM_GS,
    BASS_CONFIG_UPDATEPERIOD, BASS_CONFIG_MIDI_VOICES, BASS_CONFIG_UPDATETHREADS,
    BASS_DEVICE_DEFAULT, BASS_3DVECTOR, BASS_MIDI_FONT, GUID
)
from stream_effects import StreamEffects
from midi_input_runtime import (ControlOrigin, ChannelReadback, MidiAddress, MidiAddressSpace, MIDI_CHANNEL_LIMIT, AutoResetClock, AUXILIARY_CC, AuxiliaryControllers, CHANNEL_MIX_RANGES,
                                ChannelMidiActivity, MidiChordEventSource, ChannelMidiLearn, ChannelSends, CHANNEL_MODE_SWITCHES, NativeNoteFilter,
                                EXPRESSION_EVENTS, EXPRESSION_DEFAULTS, MASTER_FINE_PARAMETER, MASTER_COARSE_PARAMETER, MidiPerformanceControls, DeferredPerformanceFailureLog, CHANNEL_PARAMETERS,
                                ChannelControlPolicy, CHANNEL_FUNCTION_TARGETS, CHANNEL_TIMBRE_FIELDS,
                                MidiTransformOptions, channel_cc_target_value, trace_midi_payload, detach_midi_sync_handles, NativeMidiState, NativeMidiFilterGate, NativeMidiClock,midi_chord_source_is_effect)
from output_meter import OutputMeter, OutputAnalysis, PlaybackTiming
from soundfont_runtime import SoundFontActivity, SoundFontPresetRouting
from soundfont_files import PRESET_EXPRESSION_RANGES, effective_preset_expression
from delay_runtime import BMSYNTH_DELAY_PRESETS, BMSynthDelayState, DelayRuntime

_presented_observation = ContextVar("abmsynth_presented_observation", default=None)
_runtime_owner = ContextVar("abmsynth_runtime_owner", default=None)
_native_callback = ContextVar("abmsynth_native_callback", default=False)
_control_source = ContextVar("abmsynth_control_source", default=None)
_raw_actor_context = ContextVar("abmsynth_raw_actor_context", default=None)


@contextmanager
def control_source(source):
    """A writer's semantic origin follows nested calls, never another thread."""
    token = _control_source.set(source)
    try:
        yield
    finally:
        _control_source.reset(token)


def _active_runtime():
    """Compatibility facade resolves the caller/callback owner, never another stream."""
    owner = _runtime_owner.get()
    return runtime if owner is None else owner


def observation_display_stamp():
    """Manual/lifecycle identity; live MIDI progress must not veto painting."""
    owner = _active_runtime()
    return (owner.stream_handle, owner.stream_generation, owner.observation_token,
            owner.channel_reset_revision, owner.channel_policy.revision, owner.addresses.count,
            owner.identity)


def observation_display_epoch():
    """Fence manual transactions without waiting for native MIDI silence."""
    owner = _active_runtime()
    return owner.display_state_token, bool(owner.display_state_writers)


def observation_stamp():
    """Identity includes generation: a recycled numeric BASS handle is not new data."""
    owner = _active_runtime()
    return observation_display_stamp() + (owner.native_control_revision()[0],)


def observation_read_epoch():
    """Optimistic consistency fence; an observer never locks the audio writer."""
    owner = _active_runtime()
    # A transient snapshot/replay failure must not permanently veto the UI:
    # retry on the control reader, never on a BASS callback/audio thread.
    observers=owner.native_control_observers()
    if any(observer is not None and observer.busy for observer in observers):
        owner.drain_native_controls()
    revision,busy=owner.native_control_revision()
    return (owner.state_token,revision), bool(owner.state_writers) or busy


@contextmanager
def present_observation(frame):
    """Presentation-only scope; never changes MIDI dispatch or native precision."""
    token = _presented_observation.set(frame)
    try:
        with activity_observation_scope():yield
    finally:
        _presented_observation.reset(token)


def activity_observation_scope():
    """One immutable activity generation per GUI refresh, no engine locks."""
    presentation=getattr(_active_runtime(),'activity_presentation',None)
    return presentation.read_scope() if presentation is not None else nullcontext()


def activity_observation(function):
    @wraps(function)
    def read(*args,**kwargs):
        with activity_observation_scope():return function(*args,**kwargs)
    return read


def get_channel_midi_activity():
    owner=_active_runtime();presentation=getattr(owner,'activity_presentation',None)
    return (presentation if presentation is not None else owner.midi_activity).snapshot()


def get_midi_system_mode():
    """Processed native mode; no file-name inference or requested-state cache.

    bassmidi.h MIDI_EVENT_SYSTEM61,StreamGetEvent DWORD;UINT_MAX is failure.
    Called by the observation worker,never a GUI paint or audio callback.
    """
    stream=_active_runtime().stream_handle
    if not stream:return None
    value=bassmidi.BASS_MIDI_StreamGetEvent(stream,0,MIDI_EVENT_SYSTEM)
    return value if type(value) is int and 0<=value<=5 else None


def configure_chord_sources(sources):
    """Prepared immutable annotations;atomic replacement,never file I/O here."""
    _active_runtime().chord_source_metadata={int(handle):(bool(standard),frozenset(unpitched))
        for handle,(standard,unpitched) in sources.items()}


def _chord_excluded_channels(owner,channels,streams,stream):
    excluded=set(owner.midi_options.mixer_muted)
    for channel in channels-excluded:
        source=streams[channel%16] if streams else stream
        drums=bassmidi.BASS_MIDI_StreamGetEvent(source,channel,MIDI_EVENT_DRUMS)
        if drums not in (0,1):return None
        if drums:excluded.add(channel);continue
        # bassmidi.h BASS_MIDI_FONT {font,int preset,int bank}. Current preset
        # resolves actual source mappings/LSB/fallback,not requested Program.
        # Missing-before-first-note metadata is unknown,not proof of noise.
        active=BASS_MIDI_FONT()
        if bassmidi.BASS_MIDI_StreamGetPreset(source,channel,ctypes.byref(active)) and active.font:
            standard,unpitched=owner.chord_source_metadata.get(active.font,(False,frozenset()))
            if midi_chord_source_is_effect(active.bank,active.preset,standard=standard,unpitched=unpitched):
                excluded.add(channel)
    return excluded


def get_midi_chord_notes(*,synthesis_streams=()):
    """Processed MIDI held keys,not acoustic pitches/release voices.

    Reuse canonical activity counts. Direct BASS NOTE|(key<<16) collides
    with TRANSPOSE/VOICES for keys1/2 in the installed library. Activity
    covers all128keys and direct/native/file input with existing clear paths.
    Exclude drums,mutes and classified unpitched/effect sources;use the actual
    mapped preset,never requested Program names or acoustic guesses.
    """
    owner=_active_runtime();stream=owner.stream_handle
    if not stream:return None
    streams=tuple(synthesis_streams)
    if streams and (len(streams)!=16 or not all(streams)):return None
    channels=tuple(owner.addresses.channels())
    native=owner.native_activity
    if native is not None:
        notes=native.pressed_notes(channels)
        if notes is None:return None
    else:
        with owner.midi_activity.lock:
            notes=tuple((channel,key) for channel in channels
                for key,count in owner.midi_activity.notes[channel].items() if count and 0<=key<128)
    channels={channel for channel,_key in notes}
    excluded=_chord_excluded_channels(owner,channels,streams,stream)
    if excluded is None:return None
    melodic=channels-excluded
    return tuple(sorted({key for channel,key in notes if channel in melodic}))


def get_midi_chord_events(cursor,*,synthesis_streams=()):
    """Accepted note metadata,not a GUI-time reconstruction of short notes."""
    owner=_active_runtime();stream=owner.stream_handle
    if not stream:return None,()
    streams=tuple(synthesis_streams)
    if streams and (len(streams)!=16 or not all(streams)):return None,()
    native=owner.native_activity
    batch=native.chord_events(cursor) if native is not None else owner.chord_events.snapshot(cursor)
    if batch is None:return None,()
    channels={address[0] for address,_count in batch.notes}
    channels.update(ch for _serial,_packed,ch,_qpc in batch.rows if 0<=ch<128)
    excluded=_chord_excluded_channels(owner,channels,streams,stream)
    if excluded is None:return None,()
    return batch,tuple(sorted(excluded))


def get_channel_note_visual_state(now=None):
    """Canonical onset/hold/release envelope;GUI readers never infer events."""
    owner=_active_runtime();presentation=getattr(owner,'activity_presentation',None)
    return (presentation.channel_visual_state(now) if presentation is not None
            else owner.midi_activity.visual_state(now))


def _current_observation():
    frame = _presented_observation.get()
    if frame is not None and getattr(frame, "presentation_only", False):
        # A completed display sweep is not an atomic control transaction. Keep
        # manual/lifetime protection, without querying native journals on paint.
        return frame if (frame.stamp[:7] == observation_display_stamp()
                         and frame.display_epoch == observation_display_epoch()) else None
    return frame if frame is not None and frame.stamp == observation_stamp() else None


def _channel_command(function):
    @wraps(function)
    def apply(*args, **kwargs):
        # Immediate commands still call BASS on their original caller. An old
        # readback must not repaint over a just-committed manual value.
        owner = _active_runtime()
        manual = (threading.get_ident() == owner.control_thread_id and
                  not _native_callback.get() and not kwargs.get("native_input", False))
        if manual:
            owner.observation_token = object()
            _presented_observation.set(None)
        # BASS filters may be entered on its update thread while another caller
        # waits inside BASS. They must not wait on our MIDI/control mutex.
        # Ordinary producers share one ordered, reentrant dispatch boundary.
        native_token = _native_callback.set(True) if kwargs.get("native_input", False) else None
        try:
            if _native_callback.get() or kwargs.get("native_input", False):
                with owner.state_write():
                    return function(*args, **kwargs)
            with owner.midi_lock, owner.state_write():
                owner.channel_sends.flush_pending()
                result=function(*args, **kwargs)
                if owner.stream_handle and owner.channel_native_filter is not None:
                    installed=getattr(owner.channel_native_filter,'installed_stream',0)
                    if installed and installed!=owner.stream_handle:
                        _ensure_channel_event_filter(owner) # Same lifetime barrier as raw dispatch.
                    else:
                        refresh=getattr(owner.channel_native_filter,'refresh',None)
                        if refresh is not None:refresh(bass,owner.stream_handle,owner.addresses,owner.channel_policy)
                return result
        finally:
            if native_token is not None:
                _native_callback.reset(native_token)
            if manual:
                owner.observation_token = object()
    return apply

# --- 理쒖쥌 ?뺤젙 ?ㅼ젙 ---

# --- ?꾩뿭 蹂??諛???---


def get_soundfont_group_playing_state():
    owner=_active_runtime();presentation=getattr(owner,'activity_presentation',None)
    return (presentation if presentation is not None else owner.soundfont_activity).playing_state()


def get_soundfont_group_played_state():
    owner=_active_runtime();presentation=getattr(owner,'activity_presentation',None)
    return (presentation if presentation is not None else owner.soundfont_activity).played_state()


def clear_soundfont_group_played_history():
    owner=_active_runtime()
    if getattr(owner,'activity_presentation',None) is None:
        owner.soundfont_activity.clear_played_history();return
    return owner._submit_channel_command(lambda:(),lambda executor,done:executor.queue_activity_control('history',on_complete=done),
                                        None,operation='native_activity_history')


def configure_soundfont_group_available_presets(keys):
    """Dynamic writer: preserve bound legacy owners across native admission."""
    owner=_active_runtime();presentation=getattr(owner,'activity_presentation',None)
    if presentation is None:owner.soundfont_activity.set_available_presets(keys);return
    executor=owner.channel_command_executor;factory=getattr(executor,'metadata_factory',None)
    report=executor.pending
    if report is not None and report.get('disposition')==11 and factory is not None:
        with factory._changed:request=factory._source_edit_requests.get(report['control_ticket'])
        if request is not None and request.get('in_scope') and request.get('owner')==threading.get_ident():
            if not executor.publish_activity_available(report,keys):raise RuntimeError(tr('runtime.midi_generation_publish_failed'))
            return
    return owner._submit_channel_command(lambda:(),lambda execution,done:execution.queue_activity_control('available',keys,on_complete=done),
                                        None,operation='native_activity_available')


def get_soundfont_group_note_visual_state(now=None):
    owner=_active_runtime();presentation=getattr(owner,'activity_presentation',None)
    return (presentation if presentation is not None else owner.soundfont_activity).visual_state(now)


def _soundfont_group_note_on(channel, note, velocity, *, now=None):
    key = _active_runtime().soundfont_presets.preset_key(channel)
    if key is None:
        return
    _active_runtime().soundfont_activity.note_on(
        channel, note, key[0], key[1], velocity,now=now,
    )


def _soundfont_group_note_off(channel, note, *, now=None):
    _active_runtime().soundfont_activity.note_off(
        channel, note, _active_runtime().soundfont_presets.bank(channel),
        _active_runtime().soundfont_presets.program[channel],now=now,
    )


def _clear_soundfont_group_channel_notes(channel):
    _active_runtime().soundfont_activity.clear_channel(channel)


def _clear_soundfont_group_playing_state():
    _active_runtime().soundfont_activity.clear_active()

class _MIDIOUTCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", ctypes.c_ushort),
        ("wPid", ctypes.c_ushort),
        ("vDriverVersion", ctypes.c_uint),
        ("szPname", ctypes.c_wchar * 32),
        ("wTechnology", ctypes.c_ushort),
        ("wVoices", ctypes.c_ushort),
        ("wNotes", ctypes.c_ushort),
        ("wChannelMask", ctypes.c_ushort),
        ("dwSupport", ctypes.c_uint),
    ]


_winmm = ctypes.WinDLL("winmm")
_winmm.midiOutGetNumDevs.restype = ctypes.c_uint
_winmm.midiOutGetDevCapsW.argtypes = [ctypes.c_uint, ctypes.POINTER(_MIDIOUTCAPSW), ctypes.c_uint]
_winmm.midiOutGetDevCapsW.restype = ctypes.c_uint


def get_winmm_output_device_names():
    """Return output device names seen by this (normally x64) process."""
    names = []
    for device_id in range(_winmm.midiOutGetNumDevs()):
        caps = _MIDIOUTCAPSW()
        if _winmm.midiOutGetDevCapsW(device_id, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            names.append(caps.szPname)
    return names


def install_output_meter():
    return _active_runtime().output_meter.install()


def reset_output_meter(remove_dsp=False):
    if remove_dsp:
        _active_runtime().performance.detach_native_observers()
        try:
            _active_runtime().output_analysis.close()
        except RuntimeError as exc:
            append_log(str(exc))
    _active_runtime().output_meter.reset(remove_dsp=remove_dsp)


def refresh_output_meter():
    return _active_runtime().output_meter.refresh_if_needed()


def set_output_meter_decode_source(stream_handle: int) -> None:
    _active_runtime().output_meter.set_decode_source(stream_handle)


def get_stream_peak_levels():
    return _active_runtime().output_meter.consume_peak_levels()


def get_output_visual_samples():
    return _active_runtime().output_meter.visual_snapshot()

# BMSynth CC#91/93 channel-send state is owned by a separate engine component.
# The UI's native floor domain is 0..127; BMSynth Position conversion remains
# at the explicit importer/compatibility boundary below.


def configure_auto_reset(delay_seconds: int) -> None:
    """Arm one reset per MIDI-input idle episode; startup silence is ignored."""
    _active_runtime().auto_reset_clock.configure(delay_seconds)


def _record_auto_reset_input(now: float | None = None) -> None:
    owner=_active_runtime();actor=_raw_actor_context.get()
    if (actor is not None and actor[0] is owner and actor[1] is owner.channel_command_executor and
            getattr(actor[1],'auto_reset_ready',False)):
        actor[1].record_auto_reset_input() # Same original frame token, never late suffix/replay QPC.
    else:owner.auto_reset_clock.record_input(now)


def set_font_preload_callback(callback) -> None:
    """Owned by the active UI; first-use preload is optional and best-effort."""

    _active_runtime().font_preload_callback = callback


def poll_auto_reset_idle(now: float | None = None) -> bool:
    """GUI observes native completion; old ABI retains its original timer."""
    owner=_active_runtime()
    if owner.auto_reset_clock.native_execution is not None:return owner.auto_reset_clock.consume_native_completion()
    if not owner.stream_handle or not owner.auto_reset_clock.consume_idle_reset(now):
        return False
    _bmsynth_reset_from_sysex()
    return True


def _reset_contract_command(function):
    @wraps(function)
    def apply(*args,**kwargs):
        with _active_runtime().reset_contract_scope():return function(*args,**kwargs)
    apply._requires_reset_contract=True
    return apply


# BMSynth v1.6.9's preset index tables. Each entry is passed directly to
# BASSMIDI event 30/31; it is not an ABMSynth-local preset identifier.
BMSYNTH_REVERB_MACROS = (
    0xFFFFFFFF, 0x00000000, 0x00008000, 0x00008001, 0x00008002, 0x00008003,
    0x00008004, 0x00008005, 0x00000100, 0x00000101, 0x00000200, 0x00000201,
    0x00000202, 0x00000300, 0x00000301, 0x00000400, 0x00001000, 0x00001100,
    0x00001300,
)
BMSYNTH_CHORUS_MACROS = (
    0xFFFFFFFF, 0x00000000, 0x00008000, 0x00008001, 0x00008002, 0x00008003,
    0x00008004, 0x00008005, 0x00008006, 0x00008007, 0x00004100, 0x00004101,
    0x00004102, 0x00004108, 0x00004200, 0x00004201, 0x00004202, 0x00004208,
    0x00004300, 0x00004301, 0x00004308,
)
@_channel_command
def apply_channel_effect_sends():
    """Emit BMSynth event 23/24 values for all 16 MIDI channels."""
    _active_runtime().channel_sends.apply_all()


@_channel_command
def set_global_reverb_send(send_value):
    # bassmidi.h MIDI_EVENT_REVERB=23; bassmidi.md: channel send 0..127.
    _active_runtime().channel_sends.set_global_reverb(send_value)


@_channel_command
def set_global_chorus_send(send_value):
    # bassmidi.h MIDI_EVENT_CHORUS=24; bassmidi.md: channel send 0..127.
    _active_runtime().channel_sends.set_global_chorus(send_value)


def submit_global_send_floor(event,amount,*,on_complete=None):
    """Qt-free public facade to the active owner's typed floor port."""
    return _active_runtime().submit_global_send_floor(event,amount,on_complete=on_complete)


@_channel_command
def set_global_reverb(ui_value):
    """Convert a BMSynth-position reverb value to the MIDI send domain."""
    set_global_reverb_send(bmsynth_global_position_to_midi_send(ui_value))


@_channel_command
def set_global_chorus(ui_value):
    """Convert a BMSynth-position chorus value to the MIDI send domain."""
    set_global_chorus_send(bmsynth_global_position_to_midi_send(ui_value))


@_channel_command
def reset_channel_effect_sends():
    _active_runtime().channel_sends.reset_channels()


@_channel_command
def reset_channel_monitor_mix():
    """App Reset clears session-only mixer, pedal and pressure values."""
    if _active_runtime().stream_handle:
        for channel in _active_runtime().addresses.channels():
            for event, value in ((MIDI_EVENT_VOLUME, 100), (MIDI_EVENT_PAN, 64),
                                 (MIDI_EVENT_MODULATION, 0), (MIDI_EVENT_PITCH, 8192),
                                 (MIDI_EVENT_SUSTAIN, 0), (MIDI_EVENT_SOSTENUTO, 0),
                                 (MIDI_EVENT_SOFT, 0), (MIDI_EVENT_CHANPRES, 0)):
                bassmidi.BASS_MIDI_StreamEvent(_active_runtime().stream_handle, channel, event, value)
    _active_runtime().auxiliary_cc.reset()
    _active_runtime().delay_runtime.clear_sends()


def channel_auxiliary_cc_snapshot(channel: int) -> dict[int, int]:
    return _active_runtime().auxiliary_cc.snapshot(channel)


@_channel_command
def set_channel_auxiliary_cc(channel: int, number: int, value: int) -> bool:
    """Send one CC; explicit channel routing alone adds an audible target."""
    if not 0 <= channel < _active_runtime().addresses.count or not 0 <= number < 120 or not 0 <= value <= 127:
        raise ValueError("auxiliary MIDI CC")
    if not _active_runtime().stream_handle:
        return False
    # Use the same raw CC router as hardware input: native CC semantics and
    # custom source consumption must not differ for manual knob changes.
    receipt=forward_midi_bytes(bytes((0xB0 | (channel % 16), number, value)), port=channel // 16)
    if hasattr(receipt,'resume'):_active_runtime().retain_owned_midi(receipt)
    return True


def submit_auxiliary_controls(channels,number,amount,*,on_complete=None):
    return _active_runtime().submit_auxiliary_controls(channels,number,amount,on_complete=on_complete)


def channel_delay_send(channel: int) -> int:
    if not 0 <= channel < _active_runtime().addresses.count:
        raise ValueError("channel")
    return _active_runtime().delay_runtime.states[channel].send


@_channel_command
def set_channel_delay_send(channel: int, value: int) -> bool:
    if not 0 <= channel < _active_runtime().addresses.count or not 0 <= value <= 127:
        raise ValueError("delay send")
    if not _active_runtime().stream_handle:
        return False
    _active_runtime().delay_runtime.set_channel_send(channel, value)
    _active_runtime().record_origin(channel, MIDI_EVENT_USERFX, value, field="delay_send")
    if value and _active_runtime().addresses.drums[channel] and channel != 9 and channel not in _active_runtime().drum_delay_warnings:
        _active_runtime().drum_delay_warnings.add(channel)
        append_log(log_text("runtime.drum_delay_port_limited", port=channel // 16, channel=channel % 16 + 1))
    return True


@_channel_command
def set_channel_monitor_value(channel: int, kind: str, value: int) -> bool:
    """Set one live MIDI channel control; no INI or lasting override."""
    if not 0 <= channel < _active_runtime().addresses.count or kind not in CHANNEL_MIX_RANGES:
        raise ValueError("channel monitor control")
    low, high = CHANNEL_MIX_RANGES[kind]
    amount = max(low, min(high, int(value)))
    if kind in ("sustain", "sostenuto", "soft"):
        amount = 127 if amount >= 64 else 0
    if not _active_runtime().stream_handle:
        return False
    if kind == "reverb":
        return _active_runtime().channel_sends.receive_cc91(channel, amount)
    elif kind == "chorus":
        return _active_runtime().channel_sends.receive_cc93(channel, amount)
    elif kind == "delay_send":
        set_channel_delay_send(channel, amount)
    elif kind == "key_pressure":
        return set_channel_key_pressure(channel, _active_runtime().channel_policy.pressure_key(channel), amount)
    else:
        event = {"volume": MIDI_EVENT_VOLUME, "pan": MIDI_EVENT_PAN,
                 "modulation": MIDI_EVENT_MODULATION, "pitch": MIDI_EVENT_PITCH,
                 "sustain": MIDI_EVENT_SUSTAIN, "sostenuto": MIDI_EVENT_SOSTENUTO,
                 "soft": MIDI_EVENT_SOFT, "channel_pressure": MIDI_EVENT_CHANPRES}[kind]
        if not bassmidi.BASS_MIDI_StreamEvent(_active_runtime().stream_handle, channel, event, amount):
            return False
        _active_runtime().record_origin(channel, event, amount)
    return True


CHANNEL_NATIVE_EVENTS = {
    field: parameter.event for field, parameter in CHANNEL_PARAMETERS.items()
    if parameter.event is not None and field != "mode"
}
CHANNEL_NATIVE_FIELDS = {event: field for field, event in CHANNEL_NATIVE_EVENTS.items()}


def channel_key_pressure(channel, key):
    """Read per-key pressure from BASS, not a last-message/channel-wide cache.

    bassmidi.h event71; StreamGetEvent selects key in HIWORD, returns
    LOBYTE=key/HIBYTE=pressure. BASS owns reset/stream lifetime.
    """
    if not 0 <= channel < _active_runtime().addresses.count or not 0 <= key < 128:
        raise ValueError("poly pressure channel/key")
    raw = (bassmidi.BASS_MIDI_StreamGetEvent(_active_runtime().stream_handle, channel,
                                          MIDI_EVENT_KEYPRES | (key << 16))
           if _active_runtime().stream_handle else 0)
    return 0 if raw == 0xFFFFFFFF else (raw >> 8) & 127


@_channel_command
def set_channel_key_pressure(channel, key, value):
    """Send one native poly-pressure event; no CC conversion or persistence."""
    if not 0 <= channel < _active_runtime().addresses.count or not 0 <= key < 128:
        raise ValueError("poly pressure channel/key")
    pressure = max(0, min(127, int(round(value))))
    raw = key | (pressure << 8)
    accepted = bool(_active_runtime().stream_handle and bassmidi.BASS_MIDI_StreamEvent(
        _active_runtime().stream_handle, channel, MIDI_EVENT_KEYPRES, raw))
    if accepted:
        _active_runtime().record_origin(channel, MIDI_EVENT_KEYPRES, raw)
    return accepted


@_reset_contract_command
def reset_channel_assignments(channel=None):
    """One durable configuration edit for CC, sound destinations and names."""
    owner = _active_runtime()
    functions_changed = owner.channel_policy.reset_functions(channel, notify=False)
    cc_changed = owner.midi_learn.reset(channel, notify=False)
    effect_changes=owner.stream_effects.channel_fx.clear_assignments(channel)
    if any(result is False for _ch,_token,result in effect_changes):append_log(log_text('audio_fx.invalid'))
    if functions_changed or cc_changed or effect_changes:
        # Both registries use the same product persistence/native compilation
        # callback. Publish only after both successor states are ready.
        owner.midi_learn._notify()
    return bool(functions_changed or cc_changed or effect_changes)


def channel_control_value(channel, field):
    """Shared editor/monitor readback, including an explicitly rerouted input."""
    return channel_control_readback(channel,field).value


def channel_control_display_readback(channel, field):
    """Sequential display sample, independent of metadata replay readiness."""
    return channel_control_readback(channel, field, presentation=True)


def channel_control_readback(channel, field, *, presentation=False):
    """One native query, qualified without changing compatibility setter state."""
    frame = _current_observation()
    if frame is not None and (channel, field) in frame.values:
        readbacks=getattr(frame,'readbacks',None)
        return readbacks[channel,field] if readbacks is not None else ChannelReadback(frame.values[channel,field],True,'snapshot')
    if field=='active_voices':return ChannelReadback(0,False,'unavailable')
    policy = _active_runtime().channel_policy
    with policy.lock:
        if (channel, field) in policy.inputs and policy.mapped(channel, field):
            return ChannelReadback(policy.inputs[channel,field],True,'requested')
    if field in EXPRESSION_EVENTS:
        return _active_runtime().performance.expression_readback(channel, field, presentation=presentation)
    if field == "delay_send":
        return ChannelReadback(channel_delay_send(channel),True,'owner')
    if field == "key_pressure":
        stream=_active_runtime().stream_handle
        raw=bassmidi.BASS_MIDI_StreamGetEvent(stream,channel,MIDI_EVENT_KEYPRES|(policy.pressure_key(channel)<<16)) if stream else 0
        return ChannelReadback(0 if raw==0xffffffff else (raw>>8)&127,raw!=0xffffffff,'native' if stream else 'factory')
    if field == "reverb":
        return ChannelReadback(_active_runtime().channel_sends.channel_reverb[channel],True,'owner')
    if field == "chorus":
        return ChannelReadback(_active_runtime().channel_sends.channel_chorus[channel],True,'owner')
    event = CHANNEL_NATIVE_EVENTS[field]
    default = {"volume": 100, "pan": 64, "pitch": 8192}.get(field, 0)
    raw = bassmidi.BASS_MIDI_StreamGetEvent(_active_runtime().stream_handle, channel, event) if _active_runtime().stream_handle else default
    return ChannelReadback(default if raw==0xffffffff else raw,raw!=0xffffffff,
                           'native' if _active_runtime().stream_handle and raw!=0xffffffff else 'unavailable' if raw==0xffffffff else 'factory')


def channel_control_origin(channel, field, *, key=None):
    """Read-only provenance port; no widget, persistence or MIDI side effect."""
    return _active_runtime().control_origin(channel, field, key=key)


def _set_direct_channel_control(channel, field, value, *, native_input=False):
    """Destination setter; never recursively reinterpret the target's wiring."""
    if field in CHANNEL_TIMBRE_FIELDS:
        with _active_runtime().channel_policy.lock:
            _active_runtime().channel_policy.timbre[channel, field] = _active_runtime().performance.expression_parameter(field, int(value))
        if field in PRESET_EXPRESSION_RANGES:
            _active_runtime().performance.observe_native_stream(_active_runtime().stream_handle)
            _active_runtime().performance.preset_seeds.bind(bool(_active_runtime().stream_handle))
        if native_input and field in PRESET_EXPRESSION_RANGES:
            _active_runtime().performance.preset_seeds.native[channel, field] = _active_runtime().performance.expression_parameter(field, int(value))
    if field in CHANNEL_MIX_RANGES:
        return set_channel_monitor_value(channel, field, value)
    if native_input:
        # A native event filter runs inside BASS. Never acquire the performance
        # owner's lock here: UI Apply may hold it while entering BASS, creating
        # a lock-order inversion. Native input is read back from the stream,
        # not serialized as a manual INI edit. StreamEvent does not re-filter.
        _active_runtime().performance._emit(channel, EXPRESSION_EVENTS[field],
                                 _active_runtime().performance.expression_parameter(field, value))
        return bool(_active_runtime().stream_handle)
    _active_runtime().performance.set_expression(channel, field, value)
    return bool(_active_runtime().stream_handle)


@_channel_command
def set_channel_control_value(channel, field, value, *, native_input=False):
    if not 0 <= channel < _active_runtime().addresses.count or field not in CHANNEL_FUNCTION_TARGETS:
        raise ValueError("channel control")
    low, high = ChannelControlPolicy.bounds(field)
    amount = max(low, min(high, int(round(value))))
    if field in ("portamento", "sustain", "sostenuto", "soft"):
        amount = 127 if amount >= 64 else 0
    policy = _active_runtime().channel_policy
    destination = policy.destination(channel, field)
    if policy.mapped(channel, field):
        with policy.lock:
            policy.inputs[channel, field] = amount
    if destination is None:
        return True
    return _set_direct_channel_control(channel, destination,
                                      policy.convert(field, destination, amount), native_input=native_input)


def reapply_channel_timbre(channel):
    _active_runtime().drain_native_controls()
    changed, profile, force = _active_runtime().soundfont_presets.performance_seed(channel)
    if changed:
        _active_runtime().performance.apply_soundfont_profile(channel, profile, force=force)
    # The group is the baseline. A touched per-channel value replaces the
    # SAME native event at the end, not another DSP or a multiplication.
    seeds = _active_runtime().performance.preset_seeds
    if _active_runtime().soundfont_presets.extension_count or seeds.applied:
        _active_runtime().performance.observe_native_stream(_active_runtime().stream_handle)
    with _active_runtime().channel_policy.lock:
        watch_manual = any(field in PRESET_EXPRESSION_RANGES for (_ch, field) in _active_runtime().channel_policy.timbre)
    seeds.bind(bool(_active_runtime().soundfont_presets.extension_count or seeds.applied or watch_manual))
    override = _active_runtime().soundfont_presets.override(channel)
    with control_source("soundfont_group"):
        seeds.apply(channel, effective_preset_expression(override))
    with _active_runtime().channel_policy.lock:
        controls = [(field, value) for (ch, field), value in _active_runtime().channel_policy.timbre.items()
                    if ch == channel]
    for field, value in controls:
        with control_source("channel_override"):
            _active_runtime().performance._emit(channel, EXPRESSION_EVENTS[field], value)


def _observe_preset_native(channel, field, raw):
    # Last native writer updates an already-touched channel override, retaining
    # exact14-bit tuning rather than round-tripping through integer UI cents.
    with _active_runtime().channel_policy.lock:
        if (channel, field) in _active_runtime().channel_policy.timbre:
            _active_runtime().channel_policy.timbre[channel, field] = raw


def ensure_channel_event_filter():
    owner=_active_runtime()
    with owner.midi_lock:
        return _ensure_channel_event_filter(owner)


def _ensure_channel_event_filter(owner):
    if getattr(owner,'_source_filter_publication_thread',None)==threading.get_ident():
        # A delivered source-only FIFO fence retains MIDI ownership. Publish
        # its final map once on scope exit, not after every intermediate
        # performance/preset setter. Direct BASS events still run normally.
        return
    previous=getattr(owner.channel_native_filter,'installed_stream',0)
    if previous and previous!=owner.stream_handle:
        owner.detach_channel_filter() # Compatibility callers cannot borrow a prior stream's journal.
    stream = _active_runtime().stream_handle
    if not stream:
        return
    _active_runtime().performance.observe_native_stream(stream)
    _active_runtime().observe_origins(stream)
    # Native DRUMS/Bank-LSB changes must be observed at their event position,
    # even without a custom CC/function or an extended group preset.
    enabled = True
    if _active_runtime().channel_native_filter is None:
        _active_runtime().channel_native_filter = NativeMidiFilterGate(
            _active_runtime().channel_filter_callback,CHANNEL_NATIVE_FIELDS,owner=_active_runtime())
    native = _active_runtime().channel_native_filter
    native.refresh(bass,stream,_active_runtime().addresses,_active_runtime().channel_policy)
    if _active_runtime().channel_filter_state != (stream, enabled):
        if bassmidi.BASS_MIDI_StreamSetFilter(stream, False,
                native.callback if enabled else None, native.user if enabled else None):
            _active_runtime().channel_filter_state = stream, enabled
            native.installed_stream=stream if enabled else 0


def _channel_event_filter(handle, track, pointer, seeking, user):
    _active_runtime().drain_native_controls()
    # MIDIFILTERPROC permits additional StreamEvent calls at this exact event
    # position. Raw CC, RPN and SysEx are decoded by BASSMIDI, not guessed here.
    event = pointer.contents
    if event.chan < _active_runtime().addresses.count and event.event in (
            MIDI_EVENT_PROGRAM, MIDI_EVENT_BANK, MIDI_EVENT_BANK_LSB, MIDI_EVENT_DRUMS):
        # Process Program/Bank before the following Note even in one packet.
        # StreamEvent does not recursively filter or emit native syncs.
        bassmidi.BASS_MIDI_StreamEvent(handle, event.chan, event.event, event.param)
        if event.event == MIDI_EVENT_PROGRAM:
            _active_runtime().soundfont_presets.set_program(event.chan, event.param)
        elif event.event == MIDI_EVENT_BANK:
            _active_runtime().soundfont_presets.set_bank_msb(event.chan, event.param)
        elif event.event == MIDI_EVENT_BANK_LSB:
            _active_runtime().soundfont_presets.set_bank_lsb(event.chan, event.param)
        else:
            _active_runtime().addresses.drums[event.chan] = bool(event.param)
            if event.chan == 9:
                bassmidi.BASS_MIDI_StreamEvent(handle, 9, MIDI_EVENT_USERFX, 127 if event.param else 0)
            _active_runtime().soundfont_presets.reset_channel(event.chan)
            _active_runtime().stream_effects.delay.sync_drums(_active_runtime().addresses.drums, _active_runtime().addresses.count)
            _active_runtime().soundfont_presets.apply_channel(event.chan)
        override = _active_runtime().soundfont_presets.override(event.chan)
        _active_runtime().performance.preset_seeds.apply(event.chan,
            effective_preset_expression(override), force=True)
        reapply_channel_timbre(event.chan)
        return False
    field = CHANNEL_NATIVE_FIELDS.get(event.event)
    channel = event.chan
    if field is None or channel >= _active_runtime().addresses.count or not _active_runtime().channel_policy.mapped(channel, field):
        return True
    if field == "key_pressure" and (event.param & 127) != _active_runtime().channel_policy.pressure_key(channel):
        return True  # Other keys retain their native independent pressure.
    try:
        value = (_active_runtime().performance.expression_from_parameter(field, event.param)
                 if field in EXPRESSION_EVENTS else (event.param >> 8) & 127
                 if field == "key_pressure" else event.param)
        set_channel_control_value(channel, field, value, native_input=True)
        return False
    except Exception as exc:
        append_log(log_text("channel_editor.function_failed", error=str(exc)))
        return False


def channel_factory_reset_values(channel):
    values = {field:parameter.reset_value(default_reverb=_active_runtime().channel_sends.default_reverb)
              for field,parameter in CHANNEL_PARAMETERS.items()}
    values.update(bank=128 if channel % 16 == 9 else 0,bank_lsb=0,program=0)
    values["channel_pressure_vibrato"] = _active_runtime().performance.expression_default("channel_pressure_vibrato")
    override = _active_runtime().soundfont_presets.presets.get((values["bank"], 0), {})
    if not override.get("disabled"):
        for field in CHANNEL_TIMBRE_FIELDS:
            key = "volume" if field == "mix_level" else field
            if override.get(key) is not None:
                values[field] = int(override[key])
        values.update(effective_preset_expression(override))
    return values


@_channel_command
def apply_channel_reset_defaults(channel=None):
    with control_source("reset_seed"):
        return _apply_channel_reset_defaults(channel)


def _apply_channel_reset_defaults(channel=None):
    channels = _active_runtime().addresses.channels() if channel is None else (channel,)
    with _active_runtime().channel_policy.lock:
        defaults = dict(_active_runtime().channel_policy.reset_defaults)
    for ch in channels:
        if _active_runtime().soundfont_presets.extension_count:
            reapply_channel_timbre(ch)
            override = _active_runtime().soundfont_presets.override(ch)
            _active_runtime().performance.preset_seeds.apply(ch,
                effective_preset_expression(override), force=True)
            reapply_channel_timbre(ch)
        if any((ch, field) in defaults for field in ("bank", "program", "bank_lsb")):
            set_channel_instrument(ch, defaults.get((ch, "bank"), _active_runtime().soundfont_presets.bank(ch)),
                                   defaults.get((ch, "program"), _active_runtime().soundfont_presets.program[ch]),
                                   bank_lsb=defaults.get((ch, "bank_lsb"), _active_runtime().soundfont_presets.bank_lsb[ch]))
        for (selected, field), value in defaults.items():
            if selected != ch or field in ("bank", "program", "bank_lsb"):
                continue
            if field == "mode":
                _active_runtime().performance.set_expression(ch, "mode", value)
            else:
                set_channel_control_value(ch, field, value)


@_channel_command
def set_channel_mode_switch(channel: int, target: str, enabled: bool) -> bool:
    """One native mode state; no contradictory Mono/Poly/Legato booleans.

    Raw CC126/127 commands retain BASSMIDI channel-mode semantics. Legato is
    an explicit engine-only variant of Mono, never a fabricated standard CC.
    """
    if not 0 <= channel < _active_runtime().addresses.count or target not in CHANNEL_MODE_SWITCHES:
        raise ValueError("channel mode switch")
    current = _active_runtime().performance.observed_expression_value(channel, "mode")
    selected=channel_mode_switch_selection(current,target,enabled)
    if not _active_runtime().stream_handle:
        _active_runtime().performance.set_expression(channel, "mode", selected)
        return False
    if selected != current:
        if selected == 2:
            _active_runtime().performance.set_expression(channel, "mode", selected)
        else:
            receipt=forward_midi_bytes(bytes((0xB0 | (channel % 16), 126 if selected == 1 else 127, 0)), port=channel // 16)
            # Remember the manual/custom choice without emitting a second
            # MODE event after the native channel-mode command already ran.
            owner=_active_runtime()
            def save_mode():
                with owner.performance.lock:
                    if selected == EXPRESSION_DEFAULTS["mode"]:
                        owner.performance.expression.pop((channel, "mode"), None)
                    else:
                        owner.performance.expression[channel, "mode"] = selected
            if hasattr(receipt,'resume'):owner.retain_owned_midi(receipt,on_complete=save_mode)
            else:save_mode()
    return True


def channel_mode_switch_selection(current,target,enabled):
    """Existing switch intent over one mode enum; no MIDI/state side effect."""
    if target == "mode_legato":
        selected = 2 if enabled else 1 if current == 2 else current
    elif target == "mode_mono":
        selected = (current if current in (1, 2) else 1) if enabled else 0
    else:
        selected = 0 if enabled else (current if current in (1, 2) else 1)
    return selected


@_channel_command
def set_channel_instrument(channel: int, bank: int, program: int, *, bank_lsb=None) -> bool:
    """Choose the next sounding preset through BASSMIDI and routing state.

    bassmidi.h/StreamEvent: MIDI_EVENT_BANK is the 0..127 melodic bank MSB;
    MIDI_EVENT_PROGRAM is 0..127. MIDI channel 10 retains its percussion
    bank 128 routing, so only its program event is sent here. Existing voices
    keep their original preset; the new selection affects subsequent notes.
    """
    if not 0 <= channel < _active_runtime().addresses.count or not 0 <= bank <= 128 or not 0 <= program < 128:
        raise ValueError("channel instrument")
    bank_lsb = _active_runtime().soundfont_presets.bank_lsb[channel] if bank_lsb is None else bank_lsb
    if not 0 <= bank_lsb < 128:
        raise ValueError("bank LSB")
    stream = _active_runtime().stream_handle
    if not stream:
        return False
    requested_drums = bank == 128
    if requested_drums != _active_runtime().addresses.drums[channel]:
        if not bassmidi.BASS_MIDI_StreamEvent(stream, channel, MIDI_EVENT_DRUMS, int(requested_drums)):
            return False
        _active_runtime().addresses.drums[channel] = requested_drums
        if channel == 9:
            bassmidi.BASS_MIDI_StreamEvent(stream, 9, MIDI_EVENT_USERFX, 127 if requested_drums else 0)
        _active_runtime().stream_effects.delay.sync_drums(_active_runtime().addresses.drums, _active_runtime().addresses.count)
    if not requested_drums:
        if not bassmidi.BASS_MIDI_StreamEvent(stream, channel, MIDI_EVENT_BANK, bank):
            return False
        _active_runtime().soundfont_presets.observe_bank_msb(channel, bank)
    if not bassmidi.BASS_MIDI_StreamEvent(stream, channel, MIDI_EVENT_BANK_LSB, bank_lsb):
        return False
    _active_runtime().soundfont_presets.observe_bank_lsb(channel, bank_lsb)
    if not bassmidi.BASS_MIDI_StreamEvent(stream, channel, MIDI_EVENT_PROGRAM, program):
        return False
    _active_runtime().soundfont_presets.set_program(channel, program)
    return True


@_reset_contract_command
@_channel_command
def silence_channel_notes(channels=None, *, release=False):
    """Transport-only panic/release bypasses user CC remapping.

    bassmidi.h: SUSTAIN=15/SOSTENUTO=76, SOUNDOFF=16/NOTESOFF=18.
    Direct calls are not raw-input/filter requests: a mapped CC64/120 cannot
    prevent Pause/Stop or redirect an end-of-file release to another effect.
    Controllers/programs/FX remain otherwise intact; MIDI note observers clear.
    """
    stream = _active_runtime().stream_handle
    channels = _active_runtime().addresses.channels() if channels is None else channels
    for channel in channels:
        if stream:
            for event in (MIDI_EVENT_SUSTAIN, MIDI_EVENT_SOSTENUTO,
                          MIDI_EVENT_NOTESOFF if release else MIDI_EVENT_SOUNDOFF):
                _emit_midi_event(stream, channel, event, 0)
        _active_runtime().midi_options.reset_notes(channel)
        _active_runtime().midi_activity.clear(channel)
        _active_runtime().soundfont_activity.clear_channel(channel)


def reset_file_transport_state():
    """Shared deterministic file-start/seek baseline for live and export.

    Channel state follows the existing explicit reset policy. Global tuning,
    scale/drum overrides survive, and checked reset seeds are last writers.
    No effect-tab preset or device handle is reset by this operation.
    """
    owner=_active_runtime();executor=owner.channel_command_executor
    actor=_raw_actor_context.get();factory=getattr(executor,'metadata_factory',None)
    in_actor=(actor is not None and actor[0] is owner) or (factory is not None and threading.current_thread() is factory._thread)
    if executor is not None and not in_actor and getattr(executor.kernel,'_file_reset_requested',False):
        # Original preparation must finish before chase/start establishes its
        # next anchor. Wait on the shared completion owner; never steal ACK or
        # apply a Python audio fallback after an accepted native prefix.
        if getattr(owner.performance,'_native_failure_compatibility',False):
            # Arbitrary error callbacks own Python state at the original error
            # position. Keep the original chain at an exact source-edit fence.
            with owner.reset_contract_scope():return _reset_file_transport_state()
        completed=[]
        receipt=owner.submit_file_reset(_reset_file_transport_state,on_complete=completed.append)
        if not receipt.accepted or not owner.settle_channel_commands(timeout=5):raise RuntimeError(tr('runtime.channel_executor_retained'))
        if not completed or not completed[0].successful:raise RuntimeError(tr('runtime.channel_executor_retained'))
        return
    return _reset_file_transport_state()


@_reset_contract_command
@_channel_command
def _reset_file_transport_state():
    _active_runtime().playback_timing.reset_notes()
    for channel in _active_runtime().addresses.channels():
        reset_channel_state(channel)
    _active_runtime().performance.apply_all()
    apply_channel_reset_defaults()


@_reset_contract_command
@_channel_command
def reset_channel_state(channel: int) -> None:
    with control_source("channel_reset"):
        return _reset_channel_state(channel)


def _reset_channel_state(channel: int) -> None:
    """Reset exactly one MIDI channel, including session-only learn/mix state."""
    if not 0 <= channel < _active_runtime().addresses.count:
        raise ValueError("channel")
    _active_runtime().clear_origins(channel)
    stream = _active_runtime().stream_handle
    if stream:
        for event in (MIDI_EVENT_SOUNDOFF, MIDI_EVENT_RESET, MIDI_EVENT_NOTESOFF):
            _emit_midi_event(stream, channel, event, 0)
    _active_runtime().midi_activity.clear(channel)
    _active_runtime().midi_options.reset_notes(channel)
    _active_runtime().soundfont_activity.clear_channel(channel)
    _active_runtime().soundfont_presets.reset_channel(channel)
    _active_runtime().midi_learn.cancel(channel)
    _active_runtime().auxiliary_cc.reset(channel)
    _active_runtime().performance.reset_expression(channel, apply=False)
    _active_runtime().performance.preset_seeds.clear(channel)
    _active_runtime().channel_policy.clear_session(channel)
    if stream:
        _active_runtime().performance.apply_expression_defaults(channel)
    _active_runtime().channel_sends.receive_cc91(channel, _active_runtime().channel_sends.default_reverb)
    _active_runtime().channel_sends.receive_cc93(channel, 0)
    _active_runtime().delay_runtime.set_channel_send(channel, 0)
    if stream:
        set_channel_instrument(channel, 128 if channel % 16 == 9 else 0, 0, bank_lsb=0)
    for kind, value in (("volume", 100), ("pan", 64),
                        ("modulation", 0), ("pitch", 8192),
                        ("sustain", 0), ("sostenuto", 0),
                        ("soft", 0), ("channel_pressure", 0)):
        if stream:
            set_channel_monitor_value(channel, kind, value)
    apply_channel_reset_defaults(channel)


def _apply_learned_cc(channel: int, target: str, controller_value: int) -> None:
    amount = channel_cc_target_value(target, controller_value)
    if target in CHANNEL_MODE_SWITCHES:
        set_channel_mode_switch(channel, target, amount >= 64)
    elif target in CHANNEL_FUNCTION_TARGETS:
        set_channel_control_value(channel, target, amount)


def channel_monitor_snapshot(port=None, *, with_readbacks=False, channels=None):
    """Read live CC state without creating BASS channel streams/audio taps."""
    owner = _active_runtime()
    scoped = channels is not None
    if scoped:
        channels = tuple(sorted(set(channels)))
        if port is not None or any(type(channel) is not int or not 0 <= channel < owner.addresses.count for channel in channels):
            raise ValueError("channel monitor scope")
    else:
        channels = tuple(owner.addresses.channels() if port is None else range(port * 16, min((port + 1) * 16, owner.addresses.count)))
    frame = _current_observation()
    cached = {}
    if frame is not None and frame.rows:
        if isinstance(frame.rows, Mapping):
            cached = frame.rows
        elif not scoped:
            return frame.rows if port is None else frame.rows[port * 16:(port + 1) * 16]
        else:
            cached = {channel: frame.rows[channel] for channel in channels if channel < len(frame.rows)}
    stream = owner.stream_handle
    rows = {}
    for channel in channels:
        if channel in cached:
            rows[channel] = cached[channel]
            continue
        pan = bassmidi.BASS_MIDI_StreamGetEvent(stream, channel, MIDI_EVENT_PAN) if stream else 64
        volume = bassmidi.BASS_MIDI_StreamGetEvent(stream, channel, MIDI_EVENT_VOLUME) if stream else 100
        modulation = bassmidi.BASS_MIDI_StreamGetEvent(stream, channel, MIDI_EVENT_MODULATION) if stream else 0
        pitch = bassmidi.BASS_MIDI_StreamGetEvent(stream, channel, MIDI_EVENT_PITCH) if stream else 8192
        rows[channel] = {
            "reverb": _active_runtime().channel_sends.channel_reverb[channel],
            "chorus": _active_runtime().channel_sends.channel_chorus[channel],
            "delay_send": _active_runtime().delay_runtime.states[channel].send,
            "pan": pan if pan <= 128 else 64,
            "volume": volume if volume <= 127 else 100,
            "modulation": modulation if modulation <= 127 else 0,
            "pitch": pitch if pitch <= 16383 else 8192,
            "bank": _active_runtime().soundfont_presets.bank(channel),
            "bank_msb": _active_runtime().soundfont_presets.bank_msb[channel],
            "bank_lsb": _active_runtime().soundfont_presets.bank_lsb[channel],
            "drums": _active_runtime().addresses.drums[channel],
            "port": channel // 16, "channel": channel % 16,
            "program": _active_runtime().soundfont_presets.program[channel],
        }
        with _active_runtime().channel_policy.lock:
            # Read only this row's fields, rather than scanning every channel's
            # custom input for every visible channel. Keep the same lock and
            # original per-field mapping test; no MIDI input is coalesced.
            policy = owner.channel_policy
            rows[channel].update({field: policy.inputs[channel, field] for field in tuple(rows[channel])
                                  if (channel, field) in policy.inputs and policy.mapped(channel, field)})
            if with_readbacks:
                from types import MappingProxyType
                native={'pan':pan,'volume':volume,'modulation':modulation,'pitch':pitch}
                rows[channel]['_readbacks']=MappingProxyType({field:ChannelReadback(rows[channel][field],
                    raw!=0xffffffff or (channel,field) in _active_runtime().channel_policy.inputs and _active_runtime().channel_policy.mapped(channel,field),
                    'requested' if (channel,field) in _active_runtime().channel_policy.inputs and _active_runtime().channel_policy.mapped(channel,field)
                    else 'native' if stream and raw!=0xffffffff else 'factory' if not stream else 'unavailable') for field,raw in native.items()})
    return rows if scoped else tuple(rows[channel] for channel in channels)


def channel_monitor_snapshot_scoped(channels, *, with_readbacks=False):
    """Explicit optional port for sparse immutable observation consumers."""
    return channel_monitor_snapshot(channels=channels, with_readbacks=with_readbacks)


def get_channel_voice_readbacks(channels,*,synthesis_streams=()):
    """Observation worker reads existing synthesis owners,no child streams.

    bassmidi.h MIDI_EVENT_VOICES0x20001 counts active sample voices,including
    releases/layers. The high-performance controller itself has no voices.
    Caller joins the observer before closing these borrowed stream handles.
    """
    owner=_active_runtime();channels=tuple(channels);streams=tuple(synthesis_streams)
    if any(type(ch) is not int or not 0<=ch<owner.addresses.count for ch in channels):raise ValueError('voice observation scope')
    if streams and (len(streams)!=16 or not all(streams)):return {ch:ChannelReadback(0,False,'unavailable') for ch in channels}
    rows={}
    for channel in channels:
        stream=streams[channel%16] if streams else owner.stream_handle
        raw=int(bassmidi.BASS_MIDI_StreamGetEvent(stream,channel,0x20001)) if stream else 0xffffffff
        rows[channel]=ChannelReadback(0 if raw==0xffffffff else raw,raw!=0xffffffff,'synthesis' if raw!=0xffffffff else 'unavailable')
    return rows


def current_channel_voice_observation(channels):
    """Frame-only display port;absence never triggers GUI native polling."""
    frame=_current_observation();readbacks=getattr(frame,'readbacks',None)
    channels=tuple(channels)
    if readbacks is None or any((ch,'active_voices') not in readbacks for ch in channels):return None
    return tuple(readbacks[ch,'active_voices'] for ch in channels)


def channel_live_pedals(channel: int) -> dict[str, int]:
    """Read only the selected editor channel, never all 16 monitor strips."""
    if not 0 <= channel < _active_runtime().addresses.count:
        raise ValueError("channel")
    stream = _active_runtime().stream_handle
    events = {"sustain": MIDI_EVENT_SUSTAIN,
              "sostenuto": MIDI_EVENT_SOSTENUTO,
              "soft": MIDI_EVENT_SOFT,
              "channel_pressure": MIDI_EVENT_CHANPRES}
    return {field: (raw if 0 <= raw <= 127 else 0)
            for field, event in events.items()
            for raw in (bassmidi.BASS_MIDI_StreamGetEvent(stream, channel, event) if stream else 0,)}

def init_custom_fx(stream_handle, sample_rate):
    """Bind one effect owner to this stream generation before installing FX."""
    # Shared Windows output may choose the endpoint's rate when MIDI creation
    # uses freq=0. Time/Hz-based DSP must use the actual source format, not the
    # requested preference; decode/ASIO/exclusive streams remain explicit.
    from bass_bindings import BASS_CHANNELINFO
    source_info = BASS_CHANNELINFO()
    if bass.BASS_ChannelGetInfo(stream_handle, ctypes.byref(source_info)) and source_info.freq:
        sample_rate = source_info.freq
    _active_runtime().stream_effects.detach()
    _active_runtime().delay_runtime.set_sample_rate(sample_rate)
    _active_runtime().stream_effects.attach(stream_handle, sample_rate)
    _active_runtime().output_analysis.bind_stream(stream_handle)
    _active_runtime().channel_filter_state = None
    ensure_channel_event_filter()
    install_output_meter()
    _active_runtime().playback_timing.install(stream_handle,_active_runtime().channel_native_filter)
    set_dx_reverb(_active_runtime().stream_effects.spatial_enabled, 0)
    install_bmsynth_delay_dsp()
    # Only channel 10 feeds the native UserFX bus; the DSP there returns wet
    # echo only. App CC94 remains in DelayRuntime for the original gain curve.
    bassmidi.BASS_MIDI_StreamEvent(stream_handle, 9, MIDI_EVENT_USERFX, 127)
    # 由щ쾭釉??붿쭊?쇰줈 ?ㅼ떆 遺李?
    return


# Stereo Enhancer (Mid/Side). Each EngineRuntime binds its own DSP callback.
def _apply_stereo_width(buffer, length, k):
    if not buffer or length <= 0:
        return
    fptr = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_float))
    total_floats = length // ctypes.sizeof(ctypes.c_float)
    frames = total_floats // 2
    if abs(k) < 1e-7:
        return
    scale = 1.0 + k
    for i in range(frames):
        li = 2*i
        L = fptr[li]
        R = fptr[li+1]
        M = 0.5 * (L + R)
        S = 0.5 * (L - R) * scale
        fptr[li]   = M + S
        fptr[li+1] = M - S

# --- MIDI 肄쒕갚 ---
def set_equalizer(state):
    """Apply the complete EQ snapshot through its single stream-bound owner."""
    return _active_runtime().stream_effects.set_equalizer(state)


def equalizer_response(state):
    return _active_runtime().stream_effects.equalizer.response(state)


def poll_equalizer_response(state):
    return _active_runtime().stream_effects.equalizer.poll_response(state)


def set_compressor(enabled, threshold_db, ratio, attack_ms, release_ms, makeup_db):
    """Optional DX8 compressor; deliberately distinct from peak limiting."""
    return _active_runtime().stream_effects.set_compressor(
        enabled, threshold_db, ratio, attack_ms, release_ms, makeup_db,
    )


def set_true_peak_limiter(enabled, ceiling_db, release_ms):
    """Optional final source-stream output protection before meter observation."""
    return _active_runtime().stream_effects.set_limiter(enabled, ceiling_db, release_ms)


def set_dx_reverb(enabled, level, parameters=None):
    """BMSynth DXReverb: optional DX8 reverb, priority 30, original mix formula."""
    return _active_runtime().stream_effects.set_spatial_reverb(enabled, level, parameters)


def set_stereo_multiplier(multiplier):
    """Set the visible Side multiplier without exposing a DSP handle to Qt."""
    return _active_runtime().stream_effects.set_stereo_multiplier(multiplier)


def detach_stream_effects():
    """Invalidate stream-bound handles before the controller calls BASS_Free."""
    _active_runtime().detach_channel_filter()
    _active_runtime().detach_origin_observers()
    _active_runtime().performance.detach_native_observers()
    _active_runtime().stream_effects.detach()
    _active_runtime().channel_filter_state = None
    _active_runtime().channel_policy_stream = None


def set_spatial_reverb(val):
    set_dx_reverb(_active_runtime().stream_effects.spatial_enabled, val)


@_reset_contract_command
@_channel_command
def set_default_reverb_send(send_value):
    """Set the native 0..127 MIDI_EVENT_REVERB channel-reset seed directly."""
    _active_runtime().channel_sends.set_default_reverb(send_value)


def bmsynth_master_volume_parameter(position):
    """Pure original Position conversion; channel/global consumers stay apart."""
    position=max(0,min(32,int(round(float(position)))))
    internal_value=position<<2
    return 0 if internal_value==0 else min(16383,internal_value*128-1)


def master_volume_parameter(ui_value):
    raw=max(0,min(256,int(round(float(ui_value)))))
    return bmsynth_master_volume_parameter(int(round(raw*32.0/256.0)))


@_channel_command
def set_bmsynth_master_volume(position):
    """BMSynth handler: Position*4, then (127+1)*value-1 -> event 9."""
    # The original 004769F0 path uses (127 + 1) * [current value] - 1.
    # Position 32 therefore reaches the documented 14-bit full scale 16383.
    parameter = bmsynth_master_volume_parameter(position)
    if _active_runtime().stream_handle:
        bassmidi.BASS_MIDI_StreamEvent(_active_runtime().stream_handle, 0, MIDI_EVENT_MASTERVOL, parameter)


@_channel_command
def set_master_volume(ui_value,*,on_complete=None):
    """Host output fader;MIDI_EVENT_MASTERVOL remains independent MIDI data."""
    return _active_runtime().stream_effects.set_master_volume(ui_value,on_complete=on_complete)




def set_delay_output_sample_rate(sample_rate):
    _active_runtime().delay_runtime.set_sample_rate(sample_rate)


def set_master_tuning(cents):
    _active_runtime().performance.set_master(cents)


def set_master_coarse_tuning(semitones):
    _active_runtime().performance.set_master_coarse(semitones)


def submit_master_controls(values,*,on_complete=None):
    return _active_runtime().submit_master_controls(values,on_complete=on_complete)


def submit_reset_policy(values,*,on_complete=None):
    return _active_runtime().submit_reset_policy(values,on_complete=on_complete)


def install_bmsynth_delay_dsp():
    _active_runtime().stream_effects.delay.install(_active_runtime().addresses.channels(), _active_runtime().addresses.drums)


def set_delay(delay_type, level, time_ms, feedback):
    _active_runtime().drain_native_controls()
    _active_runtime().delay_runtime.set_delay(delay_type, level, time_ms, feedback)


def set_extended_delay(state):
    return _active_runtime().stream_effects.set_extended_delay(state)


def set_audio_fx(slots):
    return _active_runtime().stream_effects.set_audio_fx(slots)


def audio_fx_needs_preparation(slots):
    return _active_runtime().stream_effects.rack.needs_preparation(slots)


def request_audio_fx(slots):
    return _active_runtime().stream_effects.rack.request(slots)


def audio_fx_request_completed(token):
    """Read copied topology ACK only; never poll preparation or execute BASS."""
    return _active_runtime().stream_effects.rack.completed_topology_request(token)


def poll_audio_fx(token):
    return _active_runtime().stream_effects.rack.poll(token)


@_reset_contract_command
@_channel_command
def set_delay_sysex(enabled):
    _active_runtime().delay_runtime.sysex_enabled = bool(enabled)


def pop_delay_sysex_ui_updates():
    return _active_runtime().delay_runtime.pop_ui_updates()


def reset_delay_preset_for_midi_reset():
    _active_runtime().delay_runtime.reset_preset_for_midi_reset()


@_channel_command
def set_velocity_rate(value):
    _active_runtime().midi_options.set_velocity(value)


def set_humanization(timing_ms,velocity,preserve_simultaneity=False):
    from midi_humanization import HumanizationSettings
    values=HumanizationSettings(timing_ms,velocity,preserve_simultaneity).validated()
    owner=_active_runtime()
    # Match native source-republication's option lock. Publish the Python pair
    # before releasing the GIL for the atomic native call;an older compile must
    # not overwrite the new profile. The audio/input actor takes no such lock.
    with owner.midi_lock:
        options=owner.midi_options;previous=(options.humanize_timing_ms,options.humanize_velocity,options.humanize_preserve_simultaneity)
        options.humanize_timing_ms=values.timing_ms;options.humanize_velocity=values.velocity;options.humanize_preserve_simultaneity=values.preserve_simultaneity
        notes=getattr(getattr(getattr(owner,'channel_command_executor',None),'kernel',None),'notes',None)
        if notes is not None and not notes.set_humanization(values.timing_ms,values.velocity,values.preserve_simultaneity):
            options.humanize_timing_ms,options.humanize_velocity,options.humanize_preserve_simultaneity=previous
            raise RuntimeError(tr('humanization.native_unavailable'))


@_channel_command
def set_overnote(enabled):
    _active_runtime().midi_options.overnote_enabled = bool(enabled)


@_reset_contract_command
@_channel_command
def set_gs_reset(enabled):
    _active_runtime().midi_options.gs_reset_enabled = bool(enabled)


@_reset_contract_command
@_channel_command
def set_system_reset_receive(field,enabled):
    """Future input policy only; explicit SYSTEMEX/UI Reset stays independent."""
    if field not in ('gm1_reset','gm2_reset','xg_reset'):raise ValueError('system reset policy field')
    setattr(_active_runtime().midi_options,field+'_enabled',bool(enabled))


@_channel_command
def set_cc_interpretation(*, cc6_direct_pitch_range: bool,
                          cc94_custom_delay_send: bool) -> None:
    """Switch only future incoming CC routing; never reset sounding state."""
    _active_runtime().midi_options.cc6_direct_pitch_range = bool(cc6_direct_pitch_range)
    _active_runtime().midi_options.cc94_custom_delay_send = bool(cc94_custom_delay_send)


def _bmsynth_velocity_rate(value):
    return _active_runtime().midi_options.velocity(value)


@_channel_command
def configure_soundfont_group_presets(overrides):
    _active_runtime().soundfont_presets.configure(overrides)
    ensure_channel_event_filter()


@_channel_command
def configure_soundfont_group_performance(profile):
    _active_runtime().soundfont_presets.performance = None if profile is None else dict(profile)


@_channel_command
def update_soundfont_group_preset(bank, preset, override):
    _active_runtime().soundfont_presets.update_one(bank, preset, override)
    ensure_channel_event_filter()


@_channel_command
def clear_soundfont_group_presets():
    _active_runtime().soundfont_presets.configure({})
    ensure_channel_event_filter()


def _soundfont_group_bank(channel):
    return _active_runtime().soundfont_presets.bank(channel)


def _apply_soundfont_group_preset(channel):
    _active_runtime().soundfont_presets.apply_channel(channel)


@_channel_command
def set_soundfont_group_channel_program(channel, program):
    _active_runtime().soundfont_presets.set_program(channel, program)


@_channel_command
def set_soundfont_group_channel_bank_msb(channel, bank):
    _active_runtime().soundfont_presets.set_bank_msb(channel, bank)


@_channel_command
def set_soundfont_group_channel_bank_lsb(channel, bank):
    _active_runtime().soundfont_presets.set_bank_lsb(channel, bank)


@_channel_command
def reapply_soundfont_group_preset_key(bank, preset):
    """Reapply matching channels after the original TFRSFG source-row edit."""
    _active_runtime().soundfont_presets.reapply_key(bank, preset)


@_reset_contract_command
@_channel_command
def reset_soundfont_group_channel_state():
    _active_runtime().soundfont_presets.reset_channels()
    _active_runtime().soundfont_activity.clear_active()

def _bmsynth_reset_from_sysex(system_mode=None):
    _active_runtime().playback_timing.reset_notes()
    with control_source("midi_reset"):
        if system_mode is not None:
            # bassmidi.h SYSTEMEX resets all allocated ports, not only CH1-16.
            # Apply the actual wire-selected mode once, before restoring the
            # existing ABMSynth/SFG user defaults. Never infer it from a font.
            if not bassmidi.BASS_MIDI_StreamEvent(_active_runtime().stream_handle, 0, MIDI_EVENT_SYSTEMEX, system_mode):
                raise RuntimeError(tr('runtime.performance_event_failed', event=MIDI_EVENT_SYSTEMEX, error=bass.BASS_ErrorGetCode()))
        return _apply_bmsynth_reset_from_sysex()


def _apply_bmsynth_reset_from_sysex():
    _active_runtime().clear_origins()
    _active_runtime().midi_options.reset_notes()
    _active_runtime().midi_activity.clear()
    _active_runtime().midi_learn.cancel()
    _active_runtime().auxiliary_cc.reset()
    _active_runtime().performance.reset_expression(apply=False)
    _active_runtime().performance.preset_seeds.clear()
    _active_runtime().channel_policy.clear_session()
    _active_runtime().channel_reset_revision += 1
    _active_runtime().channel_sends.reset_channels(apply=False)
    _active_runtime().delay_runtime.clear_sends()
    for channel in _active_runtime().addresses.channels():
        bassmidi.BASS_MIDI_StreamEvent(_active_runtime().stream_handle, channel, MIDI_EVENT_SOUNDOFF, 0)
        _emit_midi_event(_active_runtime().stream_handle, channel, MIDI_EVENT_RESET, 0)
        bassmidi.BASS_MIDI_StreamEvent(_active_runtime().stream_handle, channel, MIDI_EVENT_NOTESOFF, 0)
        _active_runtime().performance.apply_expression_defaults(channel)
        _active_runtime().delay_runtime.set_channel_send(channel, 0)
    reset_delay_preset_for_midi_reset()
    reset_soundfont_group_channel_state()
    # This compatibility reset is not a raw SYSTEM event. RESET does not
    # necessarily clear the native Bank/Program. Never publish invented zero
    # addresses when the native stream retained its instrument selection.
    _refresh_native_addresses()
    for channel in _active_runtime().addresses.channels():
        _apply_soundfont_group_preset(channel)
    apply_channel_effect_sends()
    reset_channel_monitor_mix()
    bassmidi.BASS_MIDI_StreamEvent(_active_runtime().stream_handle, 9, MIDI_EVENT_USERFX, 127 if _active_runtime().addresses.drums[9] else 0)
    _active_runtime().performance.apply_all()
    apply_channel_reset_defaults()


def ensure_midi_port(port):
    """Allocate a complete port before dispatch, never during PCM rendering."""
    address = MidiAddress(port, 0)
    target = address.native + 16
    with _active_runtime().midi_lock:
        _active_runtime().addresses.used_ports.add(port)
        if target <= _active_runtime().addresses.count:
            if _active_runtime().stream_effects.delay.stream:
                _active_runtime().stream_effects.delay.install(range(address.native, target), _active_runtime().addresses.drums)
            return
        if not _active_runtime().stream_handle:
            raise RuntimeError(tr("player.engine_not_ready"))
        if not bass.BASS_ChannelSetAttribute(_active_runtime().stream_handle, BASS_ATTRIB_MIDI_CHANS, target):
            raise RuntimeError(tr("runtime.midi_port_allocate_failed", port=port, error=bass.BASS_ErrorGetCode()))
        previous = _active_runtime().addresses.count
        _active_runtime().addresses.count = target
        for channel in range(previous, target):
            bassmidi.BASS_MIDI_StreamEvent(_active_runtime().stream_handle, channel, MIDI_EVENT_DEFDRUMS, int(channel % 16 == 9))
            _active_runtime().soundfont_presets.reset_channel(channel)
            _active_runtime().channel_sends.receive_cc91(channel, _active_runtime().channel_sends.default_reverb)
            _active_runtime().channel_sends.receive_cc93(channel, 0)
            _active_runtime().performance.apply_expression_defaults(channel)
            _active_runtime().performance.apply_channel_settings(channel)
            _active_runtime().soundfont_presets.apply_channel(channel)
        _active_runtime().stream_effects.delay.install(channels=range(address.native, target), drums=_active_runtime().addresses.drums)


def _refresh_native_addresses():
    for channel in _active_runtime().addresses.channels():
        values = [bassmidi.BASS_MIDI_StreamGetEvent(_active_runtime().stream_handle, channel, event)
                  for event in (MIDI_EVENT_BANK, MIDI_EVENT_BANK_LSB, MIDI_EVENT_PROGRAM, MIDI_EVENT_DRUMS)]
        if all(isinstance(value, int) and 0 <= value < 128 for value in values):
            bank, lsb, program, drums = values
            changed = (bank, lsb, program, bool(drums)) != (
                _active_runtime().soundfont_presets.bank_msb[channel], _active_runtime().soundfont_presets.bank_lsb[channel],
                _active_runtime().soundfont_presets.program[channel], _active_runtime().addresses.drums[channel])
            _active_runtime().soundfont_presets.observe_bank_msb(channel, bank)
            _active_runtime().soundfont_presets.observe_bank_lsb(channel, lsb)
            _active_runtime().soundfont_presets.observe_program(channel, program)
            _active_runtime().addresses.drums[channel] = bool(drums)
            if changed:
                _active_runtime().soundfont_presets.apply_channel(channel)
    _active_runtime().stream_effects.delay.sync_drums(_active_runtime().addresses.drums, _active_runtime().addresses.count)


def capture_midi_received_time():
    return _active_runtime().playback_timing.timestamp()


def _emit_midi_event(stream,channel,event,parameter):
    """Original single-event result with shared decoder lifetime observation.

    MIDI_EVENT_RESET17 also clears BASS's RAW RPN selector for native0..15.
    Other events retain the original direct call without new BASS error reads.
    """
    accepted=bassmidi.BASS_MIDI_StreamEvent(stream,channel,event,parameter)
    owner=_active_runtime()
    if event==MIDI_EVENT_RESET and owner.raw_decode_context is not None:
        owner.observe_midi_event(stream,channel,event,parameter,bool(accepted),0 if accepted else int(bass.BASS_ErrorGetCode()))
    return accepted


def forward_midi_bytes(midi_data: bytes, *, port=0, trace_packet_id: int | None = None, received_stamp=None,received_origin=None):
    owner=_active_runtime()
    actor=_raw_actor_context.get()
    in_actor=actor is not None and actor[0] is owner and actor[1] is owner.channel_command_executor
    if midi_data and owner.stream_handle and owner.channel_command_executor is not None and not in_actor:
        executor=owner.channel_command_executor
        if hasattr(executor,'queue_raw') and getattr(executor,'multi',False):
            MidiAddress(port,0)
            if received_stamp is None:received_stamp=capture_midi_received_time()
            if received_stamp is None:
                # Timing display/tap admission must not decide whether MIDI
                # can play. The same native QPC exists without that DSP tap.
                now=executor.library.playback_timing_now
                now.argtypes=[];now.restype=ctypes.c_uint64;received_stamp=int(now())
            # Dynamic port allocation is a separate prepared-generation
            # publication, not a side effect racing the active input actor.
            if (port+1)*16>owner.addresses.count:raise RuntimeError(tr('runtime.channel_executor_retained'))
            return QueuedRawMidiSubmission(owner,executor,midi_data,port,trace_packet_id,received_stamp,received_origin).resume()
    if owner.raw_decode_context is not None and owner.channel_command_executor is not None:
        if not in_actor:
            # Explicit context candidates require the same native ingress
            # actor. A concurrent direct producer is not silently qualified.
            raise RuntimeError(tr('runtime.channel_executor_retained'))
    if received_stamp is None: received_stamp=capture_midi_received_time()
    MidiAddress(port, 0)
    source = _control_source.get() or ("manual" if threading.get_ident() == _active_runtime().control_thread_id
                                      and not _native_callback.get() else "native_midi")
    with _active_runtime().midi_lock, _active_runtime().state_write(), control_source(source):
        if midi_data and _active_runtime().stream_handle:
            ensure_midi_port(port)
        _active_runtime().playback_timing.observe_input(received_origin)
        return _forward_midi_bytes(midi_data, port=port, trace_packet_id=trace_packet_id,received_stamp=received_stamp,received_origin=received_origin)


class QueuedRawMidiSubmission:
    """Direct producers share the native ingress actor and completion owner.

    A refused admission retains original bytes/QPC for retry. Once accepted,
    resume only waits for copied metadata; it never submits the audio again.
    No GUI MIDI lock is held while waiting for that metadata consumer.
    """
    def __init__(self,owner,executor,raw,port,trace,stamp,origin):
        self.owner=owner;self.executor=executor;self.raw=bytes(raw);self.port=port;self.trace=trace
        self.stamp=stamp;self.origin=dict(origin) if origin is not None else None
        self.stream=owner.stream_handle;self.ticket=0;self.completed=False;self.accepted=False
        self.side_effects=False;self.error=None;self.source=_control_source.get();self.thread_id=threading.get_ident()
        self._done=threading.Event()
    def __bool__(self):return self.completed and self.accepted
    def _completed(self,report):
        self.error=int(report.get('error',0));self.accepted=not report.get('legacy_cancelled') and report.get('status',1)==1
        if report.get('legacy_audio_completed'):
            submission=report.get('legacy_submission')
            # Consumed SysEx/reset source handlers return None on success.
            # A real BOOL/owned failure remains false; None is not a failure
            # invented by the new receipt around the existing source contract.
            self.accepted=submission is None or bool(submission)
        self.completed=True;self._done.set()
        with self.owner.channel_command_condition:self.owner.channel_command_condition.notify_all()
    def resume(self):
        if self.completed:return self
        if self.owner.stream_handle!=self.stream or self.owner.channel_command_executor is not self.executor:
            raise RuntimeError(tr('runtime.channel_executor_retained'))
        factory=getattr(self.executor,'metadata_factory',None)
        if factory is not None and factory.error is not None:
            # A copied/accepted frame remains owned for metadata retry. Do not
            # spin forever in complete_submission or submit its RAW again when
            # the one consumer has stopped at a retained handoff failure.
            raise RuntimeError(tr('runtime.midi_filter_config_failed',error=factory.error)) from factory.error
        if not self.ticket:
            origin=None
            if self.origin is not None and self.origin.get('quality')=='driver':
                origin=tuple(self.origin.get(key,0) for key in ('ingress_qpc','frequency','epoch','sequence','trace','transport_loss_total','pid','flags'))
            read_stamp=self.origin.get('read_qpc',self.stamp) if origin is not None else self.stamp
            self.ticket=self.executor.queue_raw(self.raw,port=self.port,received_stamp=read_stamp,
                trace=self.trace or 0,origin=origin,on_complete=self._completed)
            self.side_effects=bool(self.ticket)
        if self.ticket:self._done.wait(.001)
        return self


class OwnedRawMidiSubmission:
    """Retained RAW prefix/suffix; original context survives backpressure.

    completed means the original submission boundary reached its terminal
    result. accepted is false on a native failure; side_effects prevents a
    transport from replaying a possibly played prefix. ASYNC is never split.
    """
    def __init__(self,owner,adapter,out,port,trace_packet_id,stamp,origin,mode):
        self.owner=owner;self.adapter=adapter;self.raw=bytes(out);self.port=port
        self.trace_packet_id=trace_packet_id;self.received_stamp=stamp;self.received_origin=dict(origin) if origin is not None else None
        self.mode=mode;self.stream=owner.stream_handle;self.channel_count=owner.addresses.count;self.source=_control_source.get();self.thread_id=threading.get_ident()
        self.cursor=0;self.completed=False;self.accepted=False;self.side_effects=False
        self.error=None;self.pending=None;self.attempts=[];self.qualification='native_counted';self._timing=False
    def __bool__(self):return self.completed and self.accepted
    def _wire(self,raw):
        return bytes((255,33,1,self.port))+raw if self.channel_count>16 else raw
    def _prefix(self,capacity):
        remaining=self.raw[self.cursor:];wire=self._wire(remaining)
        count=self.adapter.count_submission(bassmidi,wire,self.mode)
        if count is None:self.qualification='legacy_unqualified';return len(remaining),None
        if count<=capacity:return len(remaining),count
        # Only explicit channel-event framing is independently divisible.
        # Running status, SysEx/meta and timed/ASYNC boundaries stay intact.
        flags=int(bass.BASS_ChannelFlags(self.owner.stream_handle,0,0))
        if self.mode&0x38000000 or flags==0xffffffff or flags&0x400000:
            prepare=getattr(self.adapter,'prepare_submission_capacity',None)
            try:prepared=flags!=0xffffffff and prepare is not None and prepare(count)
            except RuntimeError as error:
                # Preparation may own an unresolved native unlock. Return the
                # original receipt, not an exception which loses its bytes.
                # The next resume retries on this same original owner thread.
                self._capacity_pending=True;self.preparation_error=str(error);return 0,count
            if prepared:
                available=self.adapter.admission_capacity()
                if available is not None and count<=available:return len(remaining),count
            return 0,count # Keep the original whole boundary on failure.
        ends=[];index=0
        while index<len(remaining):
            status=remaining[index]
            if not 0x80<=status<0xf0:return 0,count
            width=2 if status&0xf0 in (0xc0,0xd0) else 3
            if index+width>len(remaining) or any(value>=128 for value in remaining[index+1:index+width]):return 0,count
            index+=width;ends.append(index)
        low=0;high=len(ends);chosen=0;selected=0
        while low<high:
            middle=(low+high)//2;end=ends[middle]
            amount=self.adapter.count_submission(bassmidi,self._wire(remaining[:end]),self.mode)
            if amount is not None and amount<=capacity:chosen=end;selected=amount;low=middle+1
            else:high=middle
        return chosen,selected
    def resume(self):
        if self.completed:return self
        with self.owner.activate(),self.owner.midi_lock,self.owner.state_write(),control_source(self.source):
            if self.owner.stream_handle!=self.stream:raise RuntimeError(tr('player.engine_not_ready'))
            if getattr(self,'_capacity_pending',False):
                if not self.adapter.retry_owned_unlock():return self
                self._capacity_pending=False
                self.preparation_error=None
            if self.pending is not None:
                if not self.adapter.retry_owned_unlock():return self
                report,length=self.pending;self.pending=None
                if not self._finish(report,length):return self
            if not self.adapter.settle_failed_tickets():return self
            if not self.owner.drain_native_controls():return self
            capacity=self.adapter.admission_capacity()
            if capacity is None:self.qualification='legacy_unqualified';length=len(self.raw)-self.cursor;count=None
            else:length,count=self._prefix(capacity)
            if not length:return self
            raw=self.raw[self.cursor:self.cursor+length];wire=self._wire(raw)
            if not self._timing:
                self.owner.playback_timing.register_raw(self.raw,self.port,self.received_stamp,self.received_origin);self._timing=True
            report=self.adapter.submit_owned(wire,self.mode,count)
            if report is None:
                data=(ctypes.c_ubyte*len(wire)).from_buffer_copy(wire)
                accepted=int(bassmidi.BASS_MIDI_StreamEvents(self.owner.stream_handle,self.mode,data,len(wire)))
                code=int(bass.BASS_ErrorGetCode()) if accepted==0xffffffff else 0
                self.owner.observe_raw_submission(wire,self.mode,accepted!=0xffffffff,code)
                report=dict(status=2 if accepted==0xffffffff else 1,submitted=True,accepted_events=accepted,error=code,ticket=0)
            elif report['submitted']:
                if report.get('raw_decode_owned'):
                    self.owner.raw_submission_seen=True # Adapter committed the same shared context once.
                else:
                    # An adapter published before the explicit decoder root
                    # still owns its original submission, but not that root's
                    # state. Observe the accepted RAW boundary exactly once.
                    self.owner.observe_raw_submission(wire,self.mode,
                        report['accepted_events']!=0xffffffff,report['error'])
            self.attempts.append(dict(report,source_cursor=self.cursor,source_length=length,mode=self.mode,port=self.port))
            trace=self.owner.diagnostic_trace
            if trace is not None and report['submitted']:
                trace.record('bass_stream_events',packet_id=self.trace_packet_id,port=self.port,stream=int(self.stream),
                    accepted_events=report['accepted_events'],bass_error=report['error'] if report['accepted_events']==0xffffffff else None,
                    source_cursor=self.cursor,source_length=length,qualification=self.qualification)
            self.side_effects|=report['submitted']
            if report['status']==-2:self.pending=(report,length);return self
            self._finish(report,length)
            return self
    def _finish(self,report,length):
        if not report['submitted']:return False
        if report['accepted_events']==0xffffffff:
            self.error=report['error'];self.completed=True;self.accepted=False
            self.owner.playback_timing.cancel(self.received_stamp)
            append_log(log_text('runtime.midi_stream_events_failed',error=self.error));return False
        self.cursor+=length
        self.completed=self.cursor==len(self.raw);self.accepted=self.completed
        self.owner.drain_native_controls();return True


def _dispatch_midi_block(out, port, trace_packet_id, received_stamp=None,received_origin=None):
    owner = _active_runtime()
    trace = owner.diagnostic_trace
    if trace is not None:
        trace.record("engine_output", packet_id=trace_packet_id, port=port,
                     handled_without_raw=not bool(out), **trace_midi_payload(out))
    if not out:
        return True
    ensure_channel_event_filter()
    mapper=getattr(owner.channel_native_filter,'function_map',None)
    adapter=getattr(mapper,'selection_adapters',{}).get(getattr(owner.channel_native_filter,'published_map',None))
    if adapter is not None:
        mode=0x10000|BASS_MIDI_EVENTS_SYNC|(BASS_MIDI_EVENTS_FILTER if owner.channel_filter_state==(owner.stream_handle,True) else 0)
        receipt=OwnedRawMidiSubmission(owner,adapter,out,port,trace_packet_id,received_stamp,received_origin,mode)
        receipt.resume()
        return bool(receipt) if receipt.completed else receipt
    # Context is in the same call: no file/live input can inherit another
    # producer's active port. First-port wire data stays unchanged at startup.
    wire = bytearray((0xFF, 0x21, 1, port)) + out if owner.addresses.count > 16 else out
    # Export the existing mutable wire buffer, not one Python argument per
    # byte followed by another copy. Lifetime spans StreamEvents and its
    # synchronous filter callbacks; flush resizes only AFTER this returns.
    c_bytes = (ctypes.c_ubyte * len(wire)).from_buffer(wire)
    owner.playback_timing.register_raw(out,port,received_stamp,received_origin)
    accepted = int(bassmidi.BASS_MIDI_StreamEvents(owner.stream_handle,
        0x10000 | BASS_MIDI_EVENTS_SYNC | (BASS_MIDI_EVENTS_FILTER
            if owner.channel_filter_state == (owner.stream_handle, True) else 0),
        # An address-only argument does not retain the bytearray export in
        # tracing/mock call histories. The local c_bytes owns it for the call.
        ctypes.c_void_p(ctypes.addressof(c_bytes)), len(wire)))
    error = int(bass.BASS_ErrorGetCode()) if accepted == 0xFFFFFFFF else None
    owner.observe_raw_submission(wire,0x10000|BASS_MIDI_EVENTS_SYNC|(BASS_MIDI_EVENTS_FILTER
        if owner.channel_filter_state==(owner.stream_handle,True) else 0),accepted!=0xffffffff,error or 0)
    if trace is not None:
        trace.record("bass_stream_events", packet_id=trace_packet_id, port=port,
                     stream=int(owner.stream_handle), accepted_events=accepted, bass_error=error)
    if error is not None:
        owner.playback_timing.cancel(received_stamp)
        append_log(log_text("runtime.midi_stream_events_failed", error=error))
        return False
    owner.drain_native_controls()
    return True


def _forward_midi_bytes(midi_data: bytes, *, port=0, trace_packet_id=None,received_stamp=None,received_origin=None):
    """BMSynth-compatible raw MIDI interception for CC91/93/94 and options."""
    owner = _active_runtime()
    trace = owner.diagnostic_trace
    if trace is not None:
        trace.record("engine_input", packet_id=trace_packet_id,
                     stream=int(owner.stream_handle), overlap_correction=owner.midi_options.overnote_enabled,
                     **trace_midi_payload(midi_data))
    if not midi_data or not owner.stream_handle:
        if trace is not None:
            trace.record("engine_drop", packet_id=trace_packet_id,
                         reason="empty_packet" if not midi_data else "no_stream")
        return
    _record_auto_reset_input()
    original = bytearray(midi_data)
    if owner.midi_options.passive_packet(original):
        # All qualified bytes previously passed unchanged through the loop.
        # Keep the same native filter/sync, port, timing, trace and drain path.
        return _dispatch_midi_block(original,port,trace_packet_id,received_stamp,received_origin)
    if original.startswith(b'\xF0') and original.endswith(b'\xF7'):
        owner.delay_runtime.apply_sysex(original)
        gm_reset = original in (b'\xF0\x7E\x7F\x09\x01\xF7', b'\xF0\x7E\x7F\x09\x03\xF7')
        # Block only the selected incoming initialization,not later CC/notes
        # or explicit UI mode commands. XG's allowed path remains native RAW.
        receive_field=('gm2_reset_enabled' if original[4]==3 else 'gm1_reset_enabled') if gm_reset else (
            'xg_reset_enabled' if original==b'\xf0\x43\x10\x4c\x00\x00\x7e\x00\xf7' else None)
        if receive_field and not getattr(owner.midi_options,receive_field):
            if trace is not None:trace.record('engine_drop',packet_id=trace_packet_id,reason=receive_field+'_disabled')
            return
        # Source 0045969C/0045995B matches the exact Roland 10 42 12
        # header and reset address; 00459AF0 also requires the full frame
        # length, including the literal checksum 41 at 0045991B..00459936.
        gs_reset = original == b'\xF0\x41\x10\x42\x12\x40\x00\x7F\x00\x41\xF7'
        if gm_reset or (gs_reset and owner.midi_options.gs_reset_enabled):
            _bmsynth_reset_from_sysex(MIDI_SYSTEM_GS if gs_reset else
                                      MIDI_SYSTEM_GM2 if original[4] == 3 else MIDI_SYSTEM_GM1)
            # The whole frame is consumed after its one SYSTEMEX transition
            # and the product reset policy. Do not submit the raw reset again.
            if trace is not None:
                trace.record("engine_reset", packet_id=trace_packet_id, kind="gm" if gm_reset else "gs")
            return
        if gs_reset and not owner.midi_options.gs_reset_enabled:
            if trace is not None:
                trace.record("engine_drop", packet_id=trace_packet_id, reason="gs_reset_disabled")
            return
    if owner.midi_options.raw_planner is None:
        return _apply_midi_payload(owner,original,port,trace_packet_id,received_stamp,received_origin,None)
    def planned_steps():
        # The native plan borrower remains alive across retained dispatches.
        with owner.midi_options.raw_planner.packet(original,owner.midi_learn,owner.midi_options,port) as plan:
            return (yield from _midi_payload_steps(owner,original,port,trace_packet_id,received_stamp,received_origin,plan))
    return OwnedMidiContinuation(owner,planned_steps()).resume()


class OwnedMidiContinuation:
    """Exact semantic suspension; never re-enter an already executed prefix."""
    def __init__(self,owner,steps):
        self.owner=owner;self.steps=steps;self.pending=None;self.completed=False;self.accepted=False
        self.side_effects=True;self.source=_control_source.get();self.thread_id=threading.get_ident();self._started=False
    def __bool__(self):return self.completed and self.accepted
    def resume(self):
        if self.completed:return self
        with self.owner.activate(),self.owner.midi_lock,self.owner.state_write(),control_source(self.source):
            value=None
            if self.pending is not None:
                self.pending.resume()
                if not self.pending.completed:return self
                value=self.pending.accepted;self.pending=None
            try:
                if not self._started:self._started=True;self.pending=next(self.steps)
                else:self.pending=self.steps.send(value)
            except StopIteration as result:
                self.completed=True;self.accepted=result.value is not False
            return self


def _apply_midi_payload(owner,original,port,trace_packet_id,received_stamp,received_origin,plan):
    receipt=OwnedMidiContinuation(owner,_midi_payload_steps(owner,original,port,trace_packet_id,received_stamp,received_origin,plan))
    receipt.resume()
    return None if receipt.completed and receipt.accepted else receipt


def _midi_payload_steps(owner,original,port,trace_packet_id,received_stamp,received_origin,plan):
    """Execute original semantic actions in a native-planned or legacy walk."""
    trace=owner.diagnostic_trace
    out = bytearray()
    selection_pending = False
    def flush():
        if not out:
            return True
        result = _dispatch_midi_block(out, port, trace_packet_id,received_stamp,received_origin)
        if hasattr(result,'resume'):
            result=(yield result)
        out.clear()
        return result
    post_program_changes = []
    reset_channels = set()
    restore_drum_userfx = original.startswith(b'\xF0')
    i = 0
    while i < len(original):
        if plan is not None:
            length,packed=plan.next(i)
            if not packed:
                out.extend(original[i:i+length]);i+=length;continue
        # Program Change is a two-byte message, so it must be handled before
        # the generic three-byte MIDI-event path below.
        if i + 1 < len(original) and (original[i] & 0xF0) == 0xC0:
            channel, program = port * 16 + (original[i] & 0x0F), original[i + 1]
            out.extend(original[i:i + 2])
            # Keep the indicator's instrument identity in source order.  The
            # actual BASS preset event is still reapplied after StreamEvents,
            # but a Program Change followed by Note On in one packet must use
            # the new program for the row indicator.
            owner.soundfont_presets.observe_program(channel, program)
            post_program_changes.append(("program", channel, program))
            selection_pending = True
            i += 2
            continue
        # Channel pressure is the other two-byte channel message. A packed
        # packet may place CC/note events immediately after it; consuming a
        # phantom third byte would corrupt those following events.
        if i + 1 < len(original) and (original[i] & 0xF0) == 0xD0:
            out.extend(original[i:i + 2])
            i += 2
            continue
        if i + 2 >= len(original):
            out.extend(original[i:])
            break
        status, data1, data2 = original[i], original[i + 1], original[i + 2]
        command, channel = status & 0xF0, port * 16 + (status & 0x0F)
        if command == 0xB0:
            # The legacy/offline interpreter has no realtime MIDI actor.
            # Use the same prepared native processor,never a Qt callback.
            if getattr(owner.stream_effects.channel_fx,'_offline',False) and data1<120:
                if not (yield from flush()):return False
                owner.stream_effects.channel_fx.apply_offline_cc(channel,data1,data2)
            if data1 < 120:
                owner.auxiliary_cc.receive(channel, data1, data2)
            learned = owner.midi_learn.observe_cc(channel, data1)
            if learned:
                if not (yield from flush()):
                    return False
                for target in learned:
                    _apply_learned_cc(channel, target, data2)
                i += 3
                continue
            send_field = {91: "reverb", 93: "chorus", 94: "delay_send"}.get(data1)
            if (send_field and (data1 != 94 or owner.midi_options.cc94_custom_delay_send)
                    and owner.channel_policy.mapped(channel, send_field)):
                if not (yield from flush()):
                    return False
                set_channel_control_value(channel, send_field, data2)
                i += 3
                continue
            if data1 == 121:
                reset_channels.add(channel)
                owner.auxiliary_cc.reset(channel)
                owner.midi_learn.cancel(channel)
                owner.delay_runtime.set_channel_send(channel, 0)
                owner.channel_reset_revision += 1
            native_timbre = {71: "resonance", 72: "release", 73: "attack", 74: "cutoff", 75: "decay"}.get(data1)
            if native_timbre and not owner.channel_policy.mapped(channel, native_timbre):
                with owner.channel_policy.lock:
                    # Do not silently activate an override from ordinary DAW
                    # input on the unmodified compatibility path. Once the
                    # user has created one, later native CC is the last writer.
                    if (channel, native_timbre) in owner.channel_policy.timbre:
                        owner.channel_policy.timbre[channel, native_timbre] = data2
        if command == 0x90 and data2:
            # Resolve native source/destination before activity/preload. A
            # later selection or intercepted send must not overtake old notes.
            if selection_pending:
                if not (yield from flush()):
                    return False
                selection_pending = False
            if owner.font_preload_callback is not None:
                try:
                    owner.font_preload_callback(owner.soundfont_presets.bank(channel), owner.soundfont_presets.program[channel])
                except Exception as exc:
                    append_log(log_text("runtime.font_preload_failed", error=exc))
            muted=channel in owner.midi_options.mixer_muted
            if owner.midi_options.note_on(channel, data1) and not muted:
                out.extend((0x80 | (channel % 16), data1, 0))
            observed_at=time.monotonic()
            _soundfont_group_note_on(channel, data1, data2,now=observed_at)
            owner.midi_activity.note_on(channel, data1, data2,now=observed_at)
            if not muted:out.extend((status, data1, _bmsynth_velocity_rate(data2)))
            i += 3
        elif command == 0x80 or (command == 0x90 and data2 == 0):
            suppress_note_off = owner.midi_options.note_off(channel, data1)
            observed_at=time.monotonic()
            _soundfont_group_note_off(channel, data1,now=observed_at)
            owner.midi_activity.note_off(channel, data1,now=observed_at)
            if not suppress_note_off:
                out.extend(original[i:i + 3])
            i += 3
        elif command == 0xB0 and data1 == 91:
            if not (yield from flush()):
                return False
            owner.channel_sends.receive_cc91(channel, data2)
            i += 3
        elif command == 0xB0 and data1 == 93:
            if not (yield from flush()):
                return False
            owner.channel_sends.receive_cc93(channel, data2)
            i += 3
        elif command == 0xB0 and data1 == 94 and owner.midi_options.cc94_custom_delay_send:
            if not (yield from flush()):
                return False
            set_channel_delay_send(channel, data2)
            i += 3
        elif command == 0xB0 and data1 in (120, 121, 123, 124, 125, 126, 127):
            # StreamEvent: CC120 stops sounds; CC123/omni/mode release keys.
            # Clear overlap counts with those note-ending messages, not CC121:
            # reset-controllers does not release physically held keys.
            if data1 in (120, 123, 124, 125, 126, 127):
                owner.midi_options.reset_notes(channel)
            _clear_soundfont_group_channel_notes(channel)
            owner.midi_activity.clear(channel)
            if owner.addresses.drums[channel] and data1 == 121:
                restore_drum_userfx = True
            out.extend(original[i:i + 3])
            i += 3
        elif command == 0xB0 and data1 == 0:
            out.extend(original[i:i + 3])
            owner.soundfont_presets.observe_bank_msb(channel, data2)
            post_program_changes.append(("bank_msb", channel, data2))
            selection_pending = True
            i += 3
        elif command == 0xB0 and data1 == 32:
            out.extend(original[i:i + 3])
            owner.soundfont_presets.observe_bank_lsb(channel, data2)
            post_program_changes.append(("bank_lsb", channel, data2))
            selection_pending = True
            i += 3
        elif command == 0xB0 and data1 == 6 and owner.midi_options.cc6_direct_pitch_range:
            out.extend((0xB0 | (channel % 16), 101, 0, 0xB0 | (channel % 16), 100, 0,
                        0xB0 | (channel % 16), 6, data2, 0xB0 | (channel % 16), 101, 127,
                        0xB0 | (channel % 16), 100, 127))
            i += 3
        else:
            out.append(original[i])
            i += 1
    if not out and trace is not None:
        trace.record("engine_output", packet_id=trace_packet_id, port=port,
                     handled_without_raw=True, **trace_midi_payload(out))
    if out:
        if not (yield from flush()):
            return False
        if restore_drum_userfx:
            # CC121/system messages can reset the native UserFX send. The
            # app's CC94 value is held in DelayRuntime, not BASSMIDI.
            bassmidi.BASS_MIDI_StreamEvent(owner.stream_handle, 9, MIDI_EVENT_USERFX, 127 if owner.addresses.drums[9] else 0)
    if original.startswith(b'\xF0'):
        _refresh_native_addresses()
    # BMSynth applies per-preset settings after BASSMIDI has accepted the
    # bank/program event, preserving the source event order.
    for kind, channel, value in post_program_changes:
        if owner.channel_filter_state == (owner.stream_handle, True):
            continue  # Applied by the extension-enabled native event filter.
        if kind == "program":
            set_soundfont_group_channel_program(channel, value)
        elif kind == "bank_msb":
            set_soundfont_group_channel_bank_msb(channel, value)
        else:
            set_soundfont_group_channel_bank_lsb(channel, value)
    # CC121 is a limited native controller reset; only explicitly selected
    # application defaults override its standard result.
    for channel in reset_channels:
        apply_channel_reset_defaults(channel)
    return True


def append_log(message: str):
    _active_runtime().append_log(message)


@dataclass(frozen=True)
class ChannelCommandReceipt:
    """Control admission/completion evidence, never a saved setting."""
    request_id: int
    mode: str
    accepted: bool
    generation: int
    policy_revision: int
    results: tuple = ()
    phase: int = 0
    state_changed: bool = False
    completed: bool = False
    successful: bool = False
    error: int = 0
    unlock_error: int = 0
    topology_current: tuple = ()
    topology_retired: tuple = ()
    topology_masks: tuple = ()
    topology_faulted: bool = False
    operation: str = 'BASS_MIDI_StreamEvent'


class EngineRuntime:
    """One explicit owner for the current stream generation and MIDI state.

    AudioSession alone writes ``stream_handle``. The BASS callback objects,
    channel state, preload hook, and log queue live for this runtime's life.
    """

    def __init__(self):
        self.addresses = MidiAddressSpace()
        self.midi_lock = threading.RLock()
        self.stream_handle = 0
        self.stream_generation = 0
        self.raw_decode_context=None  # Explicit candidate, one actual RAW actor.
        self.raw_submission_seen=False
        self.identity = object()
        self.state_token = object()
        self.state_writers = {}
        self.display_state_token = object()
        self.display_state_writers = {}
        self.control_origins = {}
        self.origin_sequence = count(1)
        self.channel_command_executor=None  # Explicit product admission only.
        self.channel_command_sequence=count(1)
        self.channel_command_pending={}
        self.channel_command_condition=threading.Condition()
        self.channel_command_admission_open=True
        self.origin_stream = 0
        self.origin_sync_handles = []
        self.origin_sync_failed_stream = 0
        self.origin_sync_ready = False
        self.origin_replay = self._bind_callback(self._observe_origin, native=True, writes=True)
        self.origin_callback = SYNCPROC(self.origin_replay)
        self.origin_native_observer = None
        self._draining_native_controls = False
        self.midi_clock=NativeMidiClock()
        self.control_thread_id = threading.get_ident()
        self.observation_token = object()
        self.log_queue = queue.Queue(maxsize=2048)
        self.diagnostic_trace = None
        self.font_preload_callback = None
        self.output_meter = OutputMeter(bass, self.append_log)
        self.playback_timing = PlaybackTiming(bass, self.append_log)
        self.output_analysis = OutputAnalysis(bass)
        self.soundfont_presets = SoundFontPresetRouting(bassmidi, lambda: self.stream_handle)
        self.soundfont_activity = SoundFontActivity()
        self.chord_events = MidiChordEventSource()
        self.chord_source_metadata={}
        self.midi_activity = ChannelMidiActivity(self.chord_events)
        self.native_activity = None
        self.activity_presentation = None
        self.midi_learn = ChannelMidiLearn()
        self.midi_learn.reset_commit_scope=self.reset_contract_scope
        self.channel_policy = ChannelControlPolicy()
        self.channel_policy.reset_commit_scope=self.reset_contract_scope
        self.channel_filter_state = None
        self.channel_policy_stream = None
        self.channel_filter_callback = MIDIFILTERPROC(self._bind_callback(_channel_event_filter, native=True))
        self.channel_native_filter = None
        self.soundfont_presets.after_apply = self._bind_callback(reapply_channel_timbre)
        self.soundfont_presets.on_emit = self.record_origin
        self.auxiliary_cc = AuxiliaryControllers()
        self.channel_reset_revision = 0
        self.channel_sends = ChannelSends(bassmidi, lambda: self.stream_handle, bass_backend=bass)
        self.channel_sends.on_emit = self.record_origin
        self.channel_sends.channels = self.addresses.channels
        self.soundfont_presets.channels = self.addresses.channels
        self.soundfont_presets.is_drum = lambda channel: self.addresses.drums[channel]
        self.auto_reset_clock = AutoResetClock()
        self.midi_options = MidiTransformOptions()
        self.performance = MidiPerformanceControls(
            bassmidi, lambda: self.stream_handle,
            DeferredPerformanceFailureLog(self), bass,
            observer_failure_sink=lambda error: self.append_log(log_text(
                "capability.modulation_observer_failed", error=error)),
        )
        self.performance.channels = self.addresses.channels
        self.performance.reset_commit_scope=self.reset_contract_scope
        self.performance.failure_sink_commit_scope=self.failure_sink_publication_scope
        self.performance.soundfont_profile = self.soundfont_presets.performance_for
        self.performance.on_emit = self.record_origin
        self.performance.mod_replay_callback = self._bind_callback(
            self.performance._observe_mod_sync, native=True, writes=True)
        self.performance.mod_sync_callback = SYNCPROC(self.performance.mod_replay_callback)
        self.performance.preset_seeds.replay_callback = self._bind_callback(
            self.performance.preset_seeds._observe, native=True, writes=True)
        self.performance.preset_seeds.callback = SYNCPROC(self.performance.preset_seeds.replay_callback)
        self.performance.drain_native = self.drain_native_controls
        self.performance.native_clock = self.midi_clock
        self.performance.is_drum = lambda channel: self.addresses.drums[channel]
        self.midi_learn.channels = self.addresses.channels
        self.delay_runtime = DelayRuntime()
        self.delay_runtime.reset_commit_scope=self.reset_contract_scope
        self.drum_delay_warnings = set()
        self.performance.preset_seeds.on_native = self._bind_callback(_observe_preset_native, native=True)
        self.stereo_callback = DSPPROC(self._stereo_proc)
        self.stream_effects = StreamEffects(
            bass, bassmidi, self.stereo_callback,
            ctypes.c_float(0.0), self.delay_runtime.callback,
            lambda operation, error: self.append_log(log_text(
                "runtime.effect_backend_failed", operation=operation, error=error,
            ) if error is not None else str(operation)),
            delay_runtime=self.delay_runtime,
        )

    @contextmanager
    def activate(self):
        """Context-local owner binding; nested calls restore their previous owner."""
        token = _runtime_owner.set(self)
        try:
            yield self
        finally:
            _runtime_owner.reset(token)

    def command(self, function, *args, **kwargs):
        """Immediate ordered control command; no GUI queue or note timestamp change."""
        scope=self.reset_contract_scope() if getattr(function,'_requires_reset_contract',False) else nullcontext()
        # A reset fence must precede this writer, not wait for its own writer
        # inside the decorated function. Nested same-thread fences are reused.
        with self.activate(), scope, self.midi_lock, self.state_write():
            self.channel_sends.flush_pending()
            if not self.drain_owned_midi():
                raise RuntimeError(tr('runtime.channel_executor_retained'))
            self.drain_native_controls()
            return function(*args, **kwargs)

    def ensure_event_filter(self):
        """Composition-owned filter publication; MIDI adapters never import UI/engine."""
        with self.activate(),self.midi_lock:
            return _ensure_channel_event_filter(self)

    def retain_owned_midi(self,receipt,*,on_complete=None):
        """Retain a BOOL setter's actual submission and its post-RAW writer.

        A native failure still performs the original setter's saved writer.
        Completion callbacks ACK once; failures preserve the checkpoint for
        retry and never repeat the already submitted MIDI/audio prefix.
        """
        if not hasattr(receipt,'resume') or (on_complete is not None and not callable(on_complete)):
            raise ValueError('owned MIDI receipt')
        with self.midi_lock:
            pending=getattr(self,'owned_midi_pending',None)
            if pending is None:self.owned_midi_pending=pending=[]
            pending.append(dict(receipt=receipt,thread_id=receipt.thread_id,source=receipt.source,
                on_complete=on_complete,writer_applied=False))
            self.drain_owned_midi()
        return receipt

    def drain_owned_midi(self):
        """Owner-thread progress only; foreign pumps retain every receipt."""
        # BASS may invoke this owner on its update thread while the original
        # producer holds midi_lock and waits inside BASS. That callback must
        # not resume a producer-owned RAW receipt or wait for its mutex. The
        # ordinary external control/transport barrier still drains it below.
        if _native_callback.get():return True
        if getattr(self,'_draining_owned_midi',False):return True
        with self.midi_lock:
            self._draining_owned_midi=True
            try:
                pending=getattr(self,'owned_midi_pending',())
                while pending:
                    registration=pending[0]
                    if registration['thread_id']!=threading.get_ident():return False
                    receipt=registration['receipt']
                    with self.activate(),self.state_write(),control_source(registration['source']):
                        if not receipt.completed:receipt.resume()
                        if not receipt.completed:return False
                        if not registration['writer_applied']:
                            callback=registration['on_complete']
                            if callback is not None:callback()
                            registration['writer_applied']=True
                    pending.pop(0)
                return True
            finally:self._draining_owned_midi=False

    def submit_channel_instruments(self,requests,*,on_complete=None):
        """Typed address batch; absent fields are execution-time state.

        (channel,mask,bank,LSB,Program) preserves the original DRUMS/UserFX,
        address BOOL prefix and SoundFont tone/profile reapply. Mask1/2/4
        selects bank/LSB/program; use7 for an explicit complete selection.
        """
        requests=tuple(tuple(row) for row in requests)
        if (not 1<=len(requests)<=self.addresses.count or
                (on_complete is not None and not callable(on_complete)) or
                any(len(row)!=5 or any(type(value) is not int for value in row) or
                    not 0<=row[0]<self.addresses.count or not 1<=row[1]<=7 or
                    not 0<=row[2]<=128 or not 0<=row[3]<128 or not 0<=row[4]<128 for row in requests) or
                len({row[0] for row in requests})!=len(requests)):
            raise ValueError('channel instrument command batch')
        def legacy():
            results=[]
            for channel,mask,bank,lsb,program in requests:
                chosen_bank=bank if mask&1 else self.soundfont_presets.bank(channel)
                chosen_lsb=lsb if mask&2 else self.soundfont_presets.bank_lsb[channel]
                chosen_program=program if mask&4 else self.soundfont_presets.program[channel]
                accepted=bool(self.command(set_channel_instrument,channel,chosen_bank,chosen_program,bank_lsb=chosen_lsb))
                results.append((channel,MIDI_EVENT_PROGRAM,self.soundfont_presets.program[channel],accepted,0))
            return tuple(results)
        if self.channel_command_executor is not None and not hasattr(self.channel_command_executor.library,'midi_execution_instruments'):
            rows=legacy();successful=all(row[3] for row in rows)
            receipt=ChannelCommandReceipt(next(self.channel_command_sequence),'legacy',True,self.stream_generation,
                self.channel_policy.revision,rows,0 if successful else 2,True,True,successful,
                operation=tr('runtime.instrument_operation'))
            if on_complete is not None:on_complete(receipt)
            return receipt
        return self._submit_channel_command(legacy,
            lambda executor,done:executor.queue_instruments(requests,on_complete=done),on_complete,
            operation=tr('runtime.instrument_operation'))

    def submit_preset_tone(self,overrides,*,on_complete=None):
        """Preset defaults share channel controls' nonblocking FIFO ownership."""
        if self.channel_command_executor is None:return None
        keys=('volume','attack','decay','release','cutoff','resonance')
        copied={tuple(key):None if item is None else {name:item.get(name) for name in keys} for key,item in overrides.items()}
        rows=[]
        for (bank,program),item in copied.items():
            values=[None if item is None else item.get(name) for name in keys]
            flags=sum(4<<i for i,value in enumerate(values) if value is not None)
            rows.append((bank,program,flags,*((100 if i==0 else 64) if value is None else int(value) for i,value in enumerate(values))))
        adopted=[False]
        def complete(receipt):
            if receipt.state_changed and not adopted[0]:
                routing=self.soundfont_presets
                for key,item in copied.items():
                    current=dict(routing.presets.get(key,{}))
                    current.update({name:None if item is None else item.get(name) for name in keys})
                    routing.presets[key]=current
                    routing._performance_key_revisions[key]=routing._performance_key_revisions.get(key,0)+1
                adopted[0]=True
            if on_complete is not None:on_complete(receipt)
        return self._submit_channel_command(lambda:(),
            lambda execution,done:execution.queue_preset_tone(rows,on_complete=done),complete,
            operation=tr('runtime.preset_tone_operation'))

    def submit_channel_controls(self,channels,field,amount,*,on_complete=None):
        """Common UI batch receipt; musical/native setters remain immediate.

        A queue rejection never falls back to a synchronous ordering bypass.
        The product keeps the original path until a session is explicitly
        attached. Queued completion runs after saved metadata replay, preserving
        saved-before-audio-failure, and owns its callback through final ACK.
        No Qt dependency and no INI write belong to this engine boundary.
        """
        channels=tuple(channels)
        if (not channels or field not in CHANNEL_FUNCTION_TARGETS or
                any(type(channel) is not int or not 0<=channel<self.addresses.count for channel in channels) or
                len(set(channels))!=len(channels) or (on_complete is not None and not callable(on_complete))):
            raise ValueError('channel command batch')
        low,high=ChannelControlPolicy.bounds(field);value=max(low,min(high,int(round(amount))))
        if field in ('portamento','sustain','sostenuto','soft'):value=127 if value>=64 else 0
        def legacy():
            rows=[]
            for channel in channels:
                accepted=bool(self.command(set_channel_control_value,channel,field,value))
                target=self.channel_policy.destination(channel,field)
                raw=self.channel_policy.convert(field,target,value)
                event=0 if target is None else MIDI_EVENT_USERFX if target=='delay_send' else CHANNEL_PARAMETERS[target].event
                if target=='key_pressure':raw=(raw<<8)|self.channel_policy.pressure_key(channel)
                elif target in EXPRESSION_EVENTS:raw=self.performance.expression_parameter(target,raw)
                rows.append((channel,event,raw or 0,accepted,0))
            return tuple(rows)
        return self._submit_channel_command(legacy,
            lambda executor,done:executor.queue_manual_controls(tuple((channel,field,value) for channel in channels),on_complete=done),on_complete)

    def submit_auxiliary_controls(self,channels,number,amount,*,on_complete=None):
        """Original per-channel CC packets through the retained ingress FIFO.

        Preserve packet bytes, ports, order, custom routing and current active
        RAW provenance. A multi-channel edit is not a new atomic MIDI batch.
        Partial refusal retains accepted frames until their final completion;
        it never executes a synchronous fallback or fabricates a BASS event.
        """
        channels=tuple(channels)
        if (not channels or len(set(channels))!=len(channels) or
                any(type(channel) is not int or not 0<=channel<self.addresses.count for channel in channels) or
                type(number) is not int or not 0<=number<120 or
                (on_complete is not None and not callable(on_complete))):raise ValueError('auxiliary CC command batch')
        numeric=float(amount)
        if not math.isfinite(numeric):raise ValueError('auxiliary CC value')
        value=max(0,min(127,int(round(numeric))))
        def legacy():
            rows=[]
            for channel in channels:
                accepted=bool(self.command(set_channel_auxiliary_cc,channel,number,value))
                rows.append((channel,number,value,accepted,0))
            return tuple(rows)
        def enqueue(executor,done):
            if not getattr(executor,'multi',False) or not hasattr(executor,'queue_raw'):return 0
            # Callback may execute as soon as CDLL releases GIL. All rows and
            # the shared pending owner exist before any native admission.
            guard=threading.RLock();rows=[None]*len(channels);remaining=[len(channels)]
            admission=[True];first=[0];delivered=[False]
            def complete_if_ready():
                if admission[0] or remaining[0] or delivered[0] or not first[0]:return
                report=dict(phase=0 if all(row[3] for row in rows) else 2,results=tuple(rows),
                    error=next((row[4] for row in rows if not row[3]),0),unlock_error=0)
                # Publish only after done succeeds; metadata retry reuses this
                # exact final report and never resubmits an accepted CC packet.
                done(report);delivered[0]=True
            def completed(index,report):
                with guard:
                    if rows[index] is None:
                        accepted=(report.get('status',1)==1 and not report.get('legacy_cancelled') and
                                  not report.get('explicit_cancelled') and report.get('disposition')!=9)
                        if report.get('legacy_audio_completed'):
                            submission=report.get('legacy_submission');accepted=accepted and (submission is None or bool(submission))
                        rows[index]=(channels[index],number,value,accepted,int(report.get('error',0)))
                        remaining[0]-=1
                    complete_if_ready()
            now=executor.library.playback_timing_now;now.argtypes=[];now.restype=ctypes.c_uint64
            with guard:
                for index,channel in enumerate(channels):
                    try:
                        ticket=executor.queue_raw(bytes((0xb0|(channel%16),number,value)),port=channel//16,
                            received_stamp=int(now()),on_complete=lambda report,index=index:completed(index,report))
                    except (ValueError,RuntimeError):
                        # An earlier accepted frame still owns its audio/ACK.
                        # Return a retained partial failure instead of letting
                        # outer admission cleanup orphan that prefix's receipt.
                        ticket=0
                    if ticket:
                        if not first[0]:first[0]=ticket
                    else:rows[index]=(channel,number,value,False,0);remaining[0]-=1
                admission[0]=False;complete_if_ready()
            return first[0]
        return self._submit_channel_command(legacy,enqueue,on_complete,operation='MIDI CC')

    def submit_channel_modes(self,channels,target,enabled,*,on_complete=None):
        """Original mode-switch receipt; never reinterpret as scalar/CC input."""
        channels=tuple(channels)
        if (not channels or target not in CHANNEL_MODE_SWITCHES or type(enabled) is not bool or
                any(type(channel) is not int or not 0<=channel<self.addresses.count for channel in channels) or
                len(set(channels))!=len(channels) or (on_complete is not None and not callable(on_complete))):
            raise ValueError('channel mode command batch')
        def legacy():
            rows=[]
            for channel in channels:
                accepted=bool(self.command(set_channel_mode_switch,channel,target,enabled))
                rows.append((channel,MIDI_EVENT_MODE,self.performance.observed_expression_value(channel,'mode'),accepted,0))
            return tuple(rows)
        return self._submit_channel_command(legacy,
            lambda executor,done:executor.queue_manual_modes(tuple((channel,target,enabled) for channel in channels),on_complete=done),on_complete)

    def submit_engine_events(self,steps,*,on_complete=None,continue_on_error=False):
        """Typed StreamEvent FIFO port shared by effects and channel controls.

        This is not a FLUSH macro/read transaction. Capture BASS errors on
        the emitting thread: BASS_ErrorGetCode is thread-local (bass.md).
        Rejection never executes a synchronous fallback beside the worker.
        """
        steps=tuple(tuple(step) for step in steps)
        if (not 1<=len(steps)<=1024 or type(continue_on_error) is not bool or
                (on_complete is not None and not callable(on_complete)) or
                any(len(step)!=3 or any(type(value) is not int for value in step) or
                    not 0<=step[0]<self.addresses.count or
                    any(not 0<=value<=0xffffffff for value in step[1:]) for step in steps)):
            raise ValueError('engine event command batch')
        def legacy():
            rows=[]
            def apply():
                for channel,event,parameter in steps:
                    accepted=bool(_emit_midi_event(self.stream_handle,channel,event,parameter))
                    error=0 if accepted else int(bass.BASS_ErrorGetCode())
                    rows.append((channel,event,parameter,accepted,error))
                    if not accepted and not continue_on_error:break
            self.command(apply)
            return tuple(rows)
        return self._submit_channel_command(legacy,
            lambda executor,done:executor.queue_control(steps,continue_on_error=continue_on_error,on_complete=done),on_complete)

    def submit_engine_batch(self,channel,values,*,on_complete=None):
        """Original FLUSH count/BOOL/error, one FIFO transaction."""
        from bass_bindings import BASS_MIDI_EVENT,BASS_MIDI_EVENTS_FLUSH
        values=tuple(tuple(row) for row in values)
        if (type(channel) is not int or not 0<=channel<self.addresses.count or len(values)>1024 or
                any(len(row)!=2 or any(type(value) is not int or not 0<=value<=0xffffffff for value in row) for row in values) or
                (on_complete is not None and not callable(on_complete))):raise ValueError('engine FLUSH batch')
        steps=tuple((channel,event,parameter) for event,parameter in values)
        def legacy():
            with self.activate(),self.midi_lock:
                events=(BASS_MIDI_EVENT*len(steps))()
                for entry,(ch,event,parameter) in zip(events,steps):entry.chan=ch;entry.event=event;entry.param=parameter
                count=int(bassmidi.BASS_MIDI_StreamEvents(self.stream_handle,BASS_MIDI_EVENTS_FLUSH,
                    ctypes.cast(events,ctypes.c_void_p),len(steps)))
                accepted=count!=0xffffffff and count==len(steps)
                error=0 if accepted else int(bass.BASS_ErrorGetCode())
                self.observe_midi_batch(self.stream_handle,steps,accepted,error)
                return ((channel,0,count,accepted,error),)
        return self._submit_channel_command(legacy,
            lambda executor,done:executor.queue_effect_batch(channel,steps,on_complete=done),on_complete,
            operation='BASS_MIDI_StreamEvents')

    def submit_global_send_floor(self,event,amount,*,on_complete=None):
        """Original floor setter, both sends, execution-time requested shadow."""
        if event not in (MIDI_EVENT_REVERB,MIDI_EVENT_CHORUS) or (on_complete is not None and not callable(on_complete)):
            raise ValueError('global send floor')
        value=max(0,min(127,int(round(float(amount)))))
        def legacy():
            self.command(set_global_reverb_send if event==MIDI_EVENT_REVERB else set_global_chorus_send,value)
            return () # Original setter returns no per-event BOOL report.
        return self._submit_channel_command(legacy,
            lambda executor,done:executor.queue_global_send_floor(event,value,on_complete=done),on_complete)

    def submit_master_controls(self,values,*,on_complete=None):
        """Ordered global tuning, including the next MIDI/manual/file Reset.

        Native actor updates the baseline before each event83/84, exactly as
        the original setters do on BOOL failure. Completion is metadata only.
        The source-generation lease remains the explicit old-ABI fallback.
        """
        parameters={'master_fine':MASTER_FINE_PARAMETER,'master_coarse':MASTER_COARSE_PARAMETER}
        values=tuple(values)
        if (not 1<=len(values)<=2 or (on_complete is not None and not callable(on_complete)) or
                any(len(row)!=2 or row[0] not in parameters for row in values) or
                len({row[0] for row in values})!=len(values)):
            raise ValueError('master control fields')
        checked=tuple((field,parameters[field].clamp(value)) for field,value in values)
        with self.channel_command_condition,self.performance.lock:
            pending=any(record is None or record['receipt'].operation==tr('runtime.master_tuning_operation')
                        for record in self.channel_command_pending.values())
            if not pending:
                checked=tuple((field,value) for field,value in checked
                    if value!=getattr(self.performance,'master_cents' if field=='master_fine' else 'master_semitones'))
        if not checked:
            receipt=ChannelCommandReceipt(next(self.channel_command_sequence),'legacy',True,
                self.stream_generation,self.channel_policy.revision,completed=True,successful=True,operation=tr('runtime.master_tuning_operation'))
            if on_complete is not None:on_complete(receipt)
            return receipt
        steps=tuple((0,parameters[field].event,parameters[field].parameter(0,value)) for field,value in checked)
        def legacy():
            with self.activate():
                for field,value in checked:
                    (self.performance.set_master if field=='master_fine' else self.performance.set_master_coarse)(value)
            return tuple((*row,True,0) for row in steps)
        executor=self.channel_command_executor
        if executor is not None and (not hasattr(executor.library,'midi_execution_master') or
                getattr(self.performance,'_native_failure_compatibility',False)):
            # A queued plain StreamEvent would leave the Reset baseline stale.
            # Keep the original synchronous path until the qualified ABI exists.
            rows=legacy()
            receipt=ChannelCommandReceipt(next(self.channel_command_sequence),'legacy',True,
                self.stream_generation,self.channel_policy.revision,rows,0,True,True,True,operation=tr('runtime.master_tuning_operation'))
            if on_complete is not None:on_complete(receipt)
            return receipt
        def enqueue(executor,done):
            def completed(report):
                if report['phase'] in (0,2) and not report.get('master_metadata_replayed'):
                    # Registration holds exact cents, including values smaller
                    # than one14-bit step. Never decode rounded event parameters.
                    with self.midi_lock,self.state_write(),self.performance.lock:
                        if tuple(row[:3] for row in report['results'])!=steps:
                            raise ValueError('master receipt identity')
                        for field,value in checked:
                            setattr(self.performance,'master_cents' if field=='master_fine' else 'master_semitones',value)
                        contract=getattr(executor.kernel,'full_reset_contract',None)
                        if contract is not None:
                            contract['masters']=tuple((parameter.event,parameter.parameter(0,value))
                                for parameter,value in ((MASTER_FINE_PARAMETER,self.performance.master_cents),
                                                        (MASTER_COARSE_PARAMETER,self.performance.master_semitones)) if value)
                    report['master_metadata_replayed']=True
                done(report)
            return executor.queue_master(steps,tuple(bool(value) for _field,value in checked),on_complete=completed)
        return self._submit_channel_command(legacy,enqueue,on_complete,operation=tr('runtime.master_tuning_operation'))

    def submit_reset_policy(self,values,*,on_complete=None):
        """Same FIFO for pure reset seeds/flags; never prepare source assets."""
        values=dict(values);bits={'default_reverb':1,'gs_reset':2,'delay_sysex':4,'gm1_reset':8,'gm2_reset':16,'xg_reset':32}
        if not values or values.keys()-bits.keys() or (on_complete is not None and not callable(on_complete)):
            raise ValueError('reset policy fields')
        checked={field:max(0,min(127,int(round(value)))) if field=='default_reverb' else bool(value)
                 for field,value in values.items()}
        mask=sum(bits[field] for field in checked)
        def legacy():
            with self.activate():
                for field,value in checked.items():
                    if field in ('gm1_reset','gm2_reset','xg_reset'):set_system_reset_receive(field,value)
                    else:{'default_reverb':set_default_reverb_send,'gs_reset':set_gs_reset,'delay_sysex':set_delay_sysex}[field](value)
            return tuple((0xffffffff,bits[field] if field!='delay_sysex' else 3,int(value),True,0) for field,value in checked.items())
        executor=self.channel_command_executor
        if executor is not None and not hasattr(executor.library,'midi_execution_reset_policy'):
            rows=legacy();receipt=ChannelCommandReceipt(next(self.channel_command_sequence),'legacy',True,
                self.stream_generation,self.channel_policy.revision,rows,0,True,True,True,operation=tr('runtime.reset_policy_operation'))
            if on_complete is not None:on_complete(receipt)
            return receipt
        def enqueue(executor,done):
            def completed(report):
                if report['phase']==0 and not report.get('reset_policy_metadata_replayed'):
                    with self.midi_lock,self.state_write():
                        for field,value in checked.items():
                            target,attribute={'default_reverb':(self.channel_sends,'default_reverb'),
                                'gs_reset':(self.midi_options,'gs_reset_enabled'),
                                'delay_sysex':(self.delay_runtime,'sysex_enabled'),
                                **{key:(self.midi_options,key+'_enabled') for key in ('gm1_reset','gm2_reset','xg_reset')}}[field]
                            setattr(target,attribute,value)
                        contract=getattr(executor.kernel,'full_reset_contract',None)
                        if contract is not None:
                            if 'default_reverb' in checked:contract['default_reverb']=checked['default_reverb']
                            for field,bit in (('gs_reset',1),('delay_sysex',2)):
                                if field in checked:contract['flags']=(contract['flags']&~bit)|(bit if checked[field] else 0)
                            for field,bit in (('gm1_reset',8),('gm2_reset',16),('xg_reset',32)):
                                if field in checked:contract['flags']=(contract['flags']&~bit)|(0 if checked[field] else bit)
                    report['reset_policy_metadata_replayed']=True
                done(report)
            return executor.queue_reset_policy(mask,checked.get('default_reverb',self.channel_sends.default_reverb),
                checked.get('gs_reset',self.midi_options.gs_reset_enabled),
                checked.get('delay_sysex',self.delay_runtime.sysex_enabled),
                **({'system_receivers':tuple(checked.get(field,getattr(self.midi_options,field+'_enabled')) for field in ('gm1_reset','gm2_reset','xg_reset'))} if mask&~7 else {}),
                on_complete=completed)
        return self._submit_channel_command(legacy,enqueue,on_complete,operation=tr('runtime.reset_policy_operation'))

    def submit_engine_macro(self,channel,event,parameter,reads,*,on_complete=None):
        """Ordered FLUSH then complete GetEvent snapshot, not scalar routing.

        FLUSH processes prior async events before the macro/read operation;
        retain every read result/error on this thread, including failures.
        No external BASS_ChannelLock is added to the original macro path.
        """
        from bass_bindings import BASS_MIDI_EVENT,BASS_MIDI_EVENTS_FLUSH
        reads=tuple(reads)
        if (any(type(value) is not int for value in (channel,event,parameter)) or
                not 0<=channel<self.addresses.count or event not in (MIDI_EVENT_REVERB_MACRO,MIDI_EVENT_CHORUS_MACRO) or
                not 0<=parameter<=0xffffffff or not 1<=len(reads)<=32 or
                any(type(value) is not int or not 0<=value<=0xffffffff for value in reads) or
                (on_complete is not None and not callable(on_complete))):
            raise ValueError('engine macro command')
        def legacy():
            rows=[]
            with self.activate(),self.midi_lock:
                events=(BASS_MIDI_EVENT*1)();events[0].chan=channel;events[0].event=event;events[0].param=parameter
                count=int(bassmidi.BASS_MIDI_StreamEvents(self.stream_handle,BASS_MIDI_EVENTS_FLUSH,
                    ctypes.cast(events,ctypes.c_void_p),1))
                accepted=count==1;error=0 if accepted else int(bass.BASS_ErrorGetCode())
                rows.append((channel,event,count,accepted,error))
                if accepted:
                    for read in reads:
                        raw=int(bassmidi.BASS_MIDI_StreamGetEvent(self.stream_handle,channel,read))
                        rows.append((channel,read,raw,raw!=0xffffffff,
                                     int(bass.BASS_ErrorGetCode()) if raw==0xffffffff else 0))
            return tuple(rows)
        return self._submit_channel_command(legacy,
            lambda executor,done:executor.queue_effect_macro(channel,event,parameter,reads,on_complete=done),on_complete,
            operation='BASS_MIDI_StreamEvents')

    def submit_stream_effect(self,plan,*,on_complete=None):
        """Stream-owner prepared effect snapshot, not a rack-slot conversion."""
        graph=plan.graph
        if (getattr(graph,'owner',None) is not self or graph.stream!=self.stream_handle or
                graph.stream_generation!=self.stream_generation or not graph.pointer or not plan.pointer or
                (on_complete is not None and not callable(on_complete))):
            raise ValueError('stream effect generation')
        return self._submit_channel_command(lambda:graph.apply_legacy(plan),
            lambda executor,done:executor.queue_stream_effect(plan,on_complete=done),on_complete,
            operation='StreamEffects.'+str(graph.kind))

    def submit_panic(self,*,on_complete=None):
        """Pedal release + immediate voice stop, without controller/program reset.

        bassmidi.h events15/76/16. Use the same typed FIFO as other controls,
        bypassing user CC maps without calling BASS from the Qt thread.
        """
        def legacy():
            rows=[]
            def apply():
                for channel in self.addresses.channels():
                    for event in (MIDI_EVENT_SUSTAIN,MIDI_EVENT_SOSTENUTO,MIDI_EVENT_SOUNDOFF):
                        accepted=bool(_emit_midi_event(self.stream_handle,channel,event,0))
                        rows.append((channel,event,0,accepted,0 if accepted else int(bass.BASS_ErrorGetCode())))
                    self.midi_options.reset_notes(channel)
                    self.midi_activity.clear(channel)
                    self.soundfont_activity.clear_channel(channel)
            self.command(apply)
            return tuple(rows)
        return self._submit_channel_command(legacy,
            lambda executor,done:executor.queue_panic(on_complete=done),on_complete)

    def submit_mixer(self,mutes=None,solos=None,*,panic_channel=None,on_complete=None):
        """Live all-source isolation; never rewrite channel gains or file time."""
        panic=panic_channel is not None
        if panic:
            if type(panic_channel) is not int or not 0<=panic_channel<self.addresses.count:raise ValueError('mixer channel')
            muted=None
        else:
            mutes,solos=frozenset(mutes),frozenset(solos)
            if any(type(ch) is not int or not 0<=ch<128 for ch in mutes|solos):raise ValueError('mixer mask')
            muted=mutes|(frozenset(range(128))-solos if solos else frozenset())
        def completed(receipt):
            if not panic and receipt.accepted and receipt.phase not in (1,5,6):
                self.midi_options.mixer_mutes=mutes;self.midi_options.mixer_solos=solos;self.midi_options.mixer_muted=muted
            if on_complete is not None:on_complete(receipt)
        def legacy():
            channels=(panic_channel,) if panic else tuple(sorted(muted-self.midi_options.mixer_muted))
            rows=[]
            for channel in channels:
                if channel>=self.addresses.count:continue
                for event in (MIDI_EVENT_SUSTAIN,MIDI_EVENT_SOSTENUTO,MIDI_EVENT_SOUNDOFF):
                    ok=bool(_emit_midi_event(self.stream_handle,channel,event,0)) if self.stream_handle else True
                    rows.append((channel,event,0,ok,0 if ok else int(bass.BASS_ErrorGetCode())))
                self.midi_options.reset_notes(channel);self.midi_activity.clear(channel);self.soundfont_activity.clear_channel(channel)
            return tuple(rows)
        receipt=self._submit_channel_command(legacy,
            lambda executor,done:executor.queue_mixer(muted,panic_channel,on_complete=done),completed,operation='MIDI mixer')
        if receipt.completed:completed(receipt)
        return receipt

    def submit_system_mode(self,mode,*,resetter,restore_events=(),on_complete=None):
        """One-shot SYSTEMEX reset, never a persistent mode or input filter."""
        if type(mode) is not int or mode not in (0,1,2,3,4) or not callable(resetter) or (on_complete is not None and not callable(on_complete)):raise ValueError('system mode')
        restore_events=tuple(tuple(row) for row in restore_events)
        allowed={9,*range(32,43)} # bassmidi.h MASTERVOL + REVERB_TIME..CHORUS_REVERB
        if len(restore_events)>32 or any(len(row)!=3 or any(type(v) is not int for v in row) or
                row[0]!=0 or row[1] not in allowed or not 0<=row[2]<=0xffffffff for row in restore_events):
            raise ValueError('system mode restore events')
        def legacy():
            with self.activate():
                _bmsynth_reset_from_sysex(mode)
                for channel,event,value in restore_events:
                    if not bassmidi.BASS_MIDI_StreamEvent(self.stream_handle,channel,event,value):
                        raise RuntimeError(tr('runtime.performance_event_failed',event=event,error=bass.BASS_ErrorGetCode()))
                resetter()
            return ((0,MIDI_EVENT_SYSTEMEX,mode,True,0),)
        return self._submit_channel_command(legacy,
            lambda executor,done:executor.queue_system_mode(mode,restore_events=restore_events,on_complete=done),on_complete,operation='MIDI_EVENT_SYSTEMEX')

    def submit_manual_reset(self,resetter,*,on_complete=None):
        if not callable(resetter) or (on_complete is not None and not callable(on_complete)):raise ValueError('manual reset port')
        # CC/function/Learn edits can change the Reset journal's revision.
        # Refresh its existing native contract at the ordered source fence,
        # before admitting FX plans or Reset. No stream/assets are reloaded.
        with self.reset_contract_scope():pass
        effects=self.stream_effects.channel_fx
        effect_requests=effects.reset_all_values()
        def effect_results():
            return tuple((channel,0,0,result is not False and (token is None or effects.completed(token) is True),0)
                for channel,token,result in effect_requests)
        def legacy():resetter();return ((0,0,0,True,0),*effect_results())
        def enqueue(executor,done):
            def completed(report):
                # Earlier FX plans share this FIFO. Their copied completion
                # precedes this Reset ACK;never report a refused FX prefix as OK.
                results=tuple(report['results'])+effect_results()
                phase=report['phase']
                if phase==0 and any(not row[3] for row in results):phase=2
                done({**report,'results':results,'phase':phase})
            return executor.queue_manual_reset(on_complete=completed)
        return self._submit_channel_command(legacy,
            enqueue,on_complete,operation='MIDI Reset')

    def submit_file_reset(self,resetter,*,on_complete=None):
        """Original file Reset on the existing command FIFO, not UI/GM Reset.

        This explicit port retains the caller's outer origin. Each channel
        reset and reset-seed writer retains its separately prepared identity.
        No whole-file Reset wrapper selects this candidate implicitly yet.
        """
        if not callable(resetter) or (on_complete is not None and not callable(on_complete)):raise ValueError('file Reset port')
        source=_control_source.get() or ('native_midi' if _native_callback.get() else 'manual')
        if source not in ('manual','native_midi'):raise ValueError('file Reset source admission')
        def legacy():resetter();return ((0,0,0,True,0),)
        return self._submit_channel_command(legacy,
            lambda executor,done:executor.queue_file_reset(source=source,on_complete=done),on_complete,operation='MIDI file Reset')

    def submit_effect_topology(self,plan,*,on_complete=None):
        """Copied target graph FIFO; handles resolve at execution, not Qt time."""
        graph=plan.graph
        if (getattr(graph,'owner',None) is not self or graph.stream!=self.stream_handle or
                graph.stream_generation!=self.stream_generation or not graph.pointer or not plan.pointer or
                (on_complete is not None and not callable(on_complete))):
            raise ValueError('effect topology generation')
        def legacy():
            accepted=bool(self.command(graph.rack.set,plan.slots))
            return ((0,0,len(plan.slots),accepted,0),)
        return self._submit_channel_command(legacy,
            lambda executor,done:executor.queue_effect_topology(plan,on_complete=done),on_complete)

    def _submit_channel_command(self,legacy,enqueue,on_complete,*,operation='BASS_MIDI_StreamEvent'):
        """Shared admission, retention, completion and Close/notify ownership."""
        request_id=next(self.channel_command_sequence)
        generation,revision=self.stream_generation,self.channel_policy.revision
        # Admission and retention are one transaction. Closing cannot observe
        # an empty queue between accepting a command and retaining its owner.
        with self.channel_command_condition:
            if not self.channel_command_admission_open or getattr(self,'owned_midi_pending',()):
                return ChannelCommandReceipt(request_id,'refused',False,generation,revision,phase=5,completed=True,operation=operation)
            executor=self.channel_command_executor
            self.channel_command_pending[request_id]=None
        if executor is None:
            # Keep original exception/BOOL contracts and selected-channel order.
            try:
                rows=legacy()
                successful=all(row[3] for row in rows)
                return ChannelCommandReceipt(request_id,'legacy',True,generation,revision,tuple(rows),
                                             0 if successful else 2,True,True,successful,
                                             next((row[4] for row in rows if not row[3]),0),operation=operation)
            finally:
                with self.channel_command_condition:
                    self.channel_command_pending.pop(request_id,None);self.channel_command_condition.notify_all()
        receipt=ChannelCommandReceipt(request_id,'queued',True,generation,revision,operation=operation)
        registration=dict(receipt=receipt,callback=on_complete,completion=None,delivered=False)
        def completed(report):
            # NativeMidiExecution retries metadata/callback only, never audio.
            if registration['completion'] is None:
                phase=int(report['phase']);rows=tuple(report['results'])
                registration['completion']=ChannelCommandReceipt(request_id,'queued',True,generation,revision,rows,phase,
                    phase not in (1,5,6) and bool(report.get('topology_changed',bool(rows))),True,phase==0 and all(row[3] for row in rows),
                    int(report.get('error',0)),int(report.get('unlock_error',0)),
                    tuple(report.get('topology_current',())),tuple(report.get('topology_retired',())),
                    tuple(report.get('topology_masks',())),bool(report.get('topology_faulted',False)),operation=report.get('operation') or operation)
            if not registration['delivered']:
                if on_complete is not None:on_complete(registration['completion'])
                registration['delivered']=True
            with self.channel_command_condition:
                self.channel_command_pending.pop(request_id,None);self.channel_command_condition.notify_all()
        # Register before releasing GIL in enqueue: a metadata consumer may
        # otherwise finish the accepted command before the root retains it.
        with self.channel_command_condition:self.channel_command_pending[request_id]=registration
        try:ticket=enqueue(executor,completed)
        except BaseException:
            with self.channel_command_condition:
                self.channel_command_pending.pop(request_id,None);self.channel_command_condition.notify_all()
            raise
        if not ticket:
            with self.channel_command_condition:
                self.channel_command_pending.pop(request_id,None);self.channel_command_condition.notify_all()
            return ChannelCommandReceipt(request_id,'refused',False,generation,revision,phase=6,completed=True,operation=operation)
        return receipt

    def settle_channel_commands(self,*,timeout=5.0,stop_admission=False):
        """Wait for accepted receipt metadata, never steal the session consumer.

        The metadata pump still owns native read/replay/ACK. Condition wait
        releases GIL; timeout retains commands/resources for explicit retry.
        Final settings save may follow only a successful completion barrier.
        """
        if not 0<=timeout<=60:raise ValueError('channel completion timeout')
        deadline=time.monotonic()+timeout
        with self.channel_command_condition:
            if stop_admission:self.channel_command_admission_open=False
        while True:
            # Resume on the original owner thread before waiting. A foreign
            # closer cannot unlock or replay that thread's retained RAW lease.
            owned_complete=self.drain_owned_midi()
            with self.channel_command_condition:
                raw_pending=bool(getattr(self.channel_command_executor,'_raw_frames',{}))
                if owned_complete and not self.channel_command_pending and not raw_pending:return True
                remaining=deadline-time.monotonic()
                if remaining<=0:return False
                self.channel_command_condition.wait(min(remaining,.01))

    def attach_channel_executor(self,executor):
        """Explicit session admission; construction does not enable the product."""
        with self.channel_command_condition:
            if (self.channel_command_executor is not None or self.channel_command_pending or
                    not self.channel_command_admission_open or getattr(executor,'owner',None) is not self or
                    not getattr(executor,'pointer',None)):
                raise ValueError('channel executor admission')
            self.auto_reset_clock.bind_native(executor)
            self.channel_command_executor=executor

    def retire_channel_executor(self,executor):
        """Unpublish only after the metadata owner confirmed native join/close."""
        with self.channel_command_condition:
            if (self.channel_command_executor is not executor or self.channel_command_pending or
                    getattr(executor,'pointer',None)):
                return False
            self.channel_command_executor=None
            self.auto_reset_clock.unbind_native(executor)
            return True

    @contextmanager
    def reset_contract_scope(self):
        """Pure reset-setting publication before any caller's MIDI mutex.

        Existing reset/delegate/source actors already own their exact FIFO
        generation. Never recursively wait for their own metadata consumer.
        """
        executor=self.channel_command_executor
        factory=getattr(executor,'metadata_factory',None)
        actor=_raw_actor_context.get()
        if factory is None or threading.current_thread() is factory._thread or (actor is not None and actor[0] is self):
            yield;return
        for request in tuple(factory._source_edit_requests.values()):
            if request['owner_thread'] is threading.current_thread() and request.get('in_scope',False):
                yield;return
        with factory.reset_contract_scope():yield

    @contextmanager
    def failure_sink_publication_scope(self,old,new):
        executor=self.channel_command_executor
        factory=getattr(executor,'metadata_factory',None)
        if factory is None or not getattr(executor,'failure_notification_admitted',False):
            yield;return
        # Existing source-edit fence drains every earlier immutable error to
        # its original sink before a mutable public sink can replace it.
        with self.reset_contract_scope():
            # Admit the native recipient policy before publishing the public
            # callable. If admission fails, the old callable and native policy
            # remain paired; source-edit cleanup retains its ordinary retry.
            executor.publish_failure_notification_sink(new)
            yield

    def _require_retired_channel_executor(self):
        # Neither a successful enqueue nor an empty Python completion map
        # establishes that the native worker released its stream/filter leases.
        with self.channel_command_condition:
            kernel=getattr(self,'cc_native_kernel',None)
            if (self.channel_command_executor is not None or self.channel_command_pending or
                    getattr(kernel,'pointer',None) or getattr(self,'owned_midi_pending',())):
                raise RuntimeError(tr('runtime.channel_executor_retained'))

    def native_control_observers(self):
        # Only the registered native gate owns mapping journals. A legacy
        # callback/test double's arbitrary attributes are not native leases.
        mapper=getattr(self.channel_native_filter,'function_map',None) if isinstance(self.channel_native_filter,NativeMidiFilterGate) else None
        observers=(self.origin_native_observer,self.performance.mod_native_observer,
                   self.performance.preset_seeds.native_observer,getattr(mapper,'journal',None))
        observers+=tuple(getattr(mapper,'selection_adapters',{}).values())
        cc=getattr(getattr(self,'cc_native_kernel',None),'journal',None)
        semantic=getattr(getattr(self,'cc_native_kernel',None),'semantic_journal',None)
        actions=getattr(getattr(self,'cc_native_kernel',None),'semantic_actions',None)
        manual=getattr(getattr(self,'cc_native_kernel',None),'manual_journal',None)
        reset=getattr(getattr(self,'cc_native_kernel',None),'reset_journal',None)
        return observers+(cc,semantic,actions,manual,reset) if any(observer is not None for observer in (cc,semantic,actions,manual,reset)) else observers

    def native_control_revision(self):
        # Source publication may retire these observers on the loader thread.
        # A GUI poll must neither dereference a retired pointer nor wait for
        # the whole source transaction. Reuse its last coherent stamp as busy;
        # no missing events are acknowledged or replaced by an empty image.
        if not self.midi_lock.acquire(blocking=False):
            return getattr(self,'_native_control_revision_cache',()),True
        try:
            observers=self.native_control_observers()
            states=tuple(observer.revision() if observer is not None else (0,False) for observer in observers)
            revision=tuple(state[0] for state in states)
            self._native_control_revision_cache=revision
            return revision,any(state[1] for state in states) or self.midi_clock.revision()[1]
        finally:self.midi_lock.release()

    def drain_native_controls(self):
        if self._draining_native_controls:return True
        try:owned_ready=self.drain_owned_midi()
        except BaseException:
            self._native_control_quiet_stamp=None
            raise
        if not owned_ready:
            self._native_control_quiet_stamp=None
            return False
        observers=self.native_control_observers()
        # Repeated metadata records often contain no new control capture. The
        # common native clock already covers every admitted observer writer.
        # Cache only a successfully drained, writer-free EXACT domain/cursor;
        # independent/old ABI clocks and busy observers always use full replay.
        clock=self.midi_clock
        shared=bool(clock.pointer and getattr(clock,'quiet_ready',False) and all(observer is None or
            (not observer.pointer or getattr(observer,'clock',None) is clock) for observer in observers))
        before=clock.revision() if shared else None
        identity=tuple((observer,observer.pointer,observer.serial,observer.busy)
            for observer in observers if observer is not None) if shared else ()
        stamp=(clock.pointer,before[0],identity) if before is not None and not before[1] and not any(row[3] for row in identity) else None
        if stamp is not None and getattr(self,'_native_control_quiet_stamp',None)==stamp:return True
        self._native_control_quiet_stamp=None
        # Read-only calls with no pending capture must NOT create a writer:
        # origin/value queries also run inside immutable-frame collection.
        if not any(observer is not None and (observer.busy or
                observer.revision()[0]!=observer.serial) for observer in observers):
            after=self.midi_clock.revision();ready=not after[1]
            if ready and stamp is not None and after==before:self._native_control_quiet_stamp=stamp
            return ready
        self._draining_native_controls=True
        try:
            # Hold one optimistic writer marker across the ENTIRE aggregate,
            # including gaps between metadata delegates which can release GIL.
            # Readers reject half-replayed frames without locking audio.
            with self.state_write():
                ready=self.midi_clock.drain(observers)
            # Capture arriving DURING replay must not be hidden by caching its
            # newer clock with the older Python cursors. Retain no cache then.
            if ready and before is not None and not before[1] and clock.revision()==before:
                identity=tuple((observer,observer.pointer,observer.serial,observer.busy)
                    for observer in observers if observer is not None)
                if not any(row[3] for row in identity):self._native_control_quiet_stamp=(clock.pointer,before[0],identity)
            return ready
        finally:self._draining_native_controls=False

    @contextmanager
    def state_write(self):
        # Only control batches/selective parameter callbacks use this fence.
        # No lock or waiting on a native callback; no marker on every Note.
        marker = object()
        self.state_writers[marker] = True
        self.state_token = object()
        # Strict state_token also covers native metadata replay. Its progress
        # cannot invalidate every completed live display sweep. Track only UI
        # manual transactions here; reset/policy/lifetime have their own stamp.
        display_write = (threading.get_ident() == self.control_thread_id
                         and not _native_callback.get()
                         and not self._draining_native_controls)
        if display_write:
            self.display_state_writers[marker] = True
            self.display_state_token = object()
        try:
            yield
        finally:
            self.state_writers.pop(marker, None)
            self.state_token = object()
            if display_write:
                self.display_state_writers.pop(marker, None)
                self.display_state_token = object()

    def _bind_callback(self, function, *, native=False, writes=False):
        def callback(*args, **kwargs):
            with self.activate():
                token = _native_callback.set(native or _native_callback.get())
                try:
                    if writes:
                        with self.state_write():
                            return function(*args, **kwargs)
                    return function(*args, **kwargs)
                finally:
                    _native_callback.reset(token)
        return callback

    def append_log(self, message: str) -> None:
        from event_log import log_entry
        message=log_entry(message)
        from error_reporting import record
        record('app_log',message=str(message),level=message.level,message_id=message.message_id,
            details=message.detail,repeated=message.repeat,indicator=message.indicator)
        trace = self.diagnostic_trace
        if trace is not None:
            trace.record("app_log", message=str(message),level=message.level,message_id=message.message_id,
                details=message.detail,repeated=message.repeat,indicator=message.indicator)
        try:
            self.log_queue.put_nowait(message)
        except queue.Full:
            pass

    def record_origin(self, channel, event, raw, source=None, *, field=None, phase="accepted"):
        self.drain_native_controls()
        if not self.stream_handle or not 0 <= channel < self.addresses.count:
            return
        field = field or CHANNEL_NATIVE_FIELDS.get(event) or ("mode" if event == EXPRESSION_EVENTS["mode"] else None)
        if field is None:
            return
        key = (channel, field, raw & 127) if field == "key_pressure" else (channel, field)
        self.control_origins[key] = ControlOrigin(self.stream_generation, int(event), int(raw),
            source or _control_source.get() or ("native_midi" if _native_callback.get() else "manual"),
            next(self.origin_sequence), phase)

    def clear_origins(self, channel=None):
        self.drain_native_controls()
        if channel is None:
            self.control_origins.clear()
        else:
            for key in tuple(self.control_origins):
                if key[0] == channel:
                    self.control_origins.pop(key, None)

    def _observe_origin(self, _sync, stream, data, event):
        if stream != self.origin_stream:
            return
        channel, raw = (data >> 16) & 0xffff, data & 0xffff
        if event == MIDI_EVENT_SYSTEM:
            self.clear_origins()
        elif event == MIDI_EVENT_RESET:
            # CC121 resets only the documented controller subset, not routes/tuning.
            for field, value in (("modulation", 0), ("expression_level", 127), ("sustain", 0),
                                 ("portamento", 0), ("release", 64), ("attack", 64),
                                 ("pitch", 8192), ("channel_pressure", 0)):
                self.record_origin(channel, CHANNEL_NATIVE_EVENTS[field], value, "midi_reset", phase="processed")
        else:
            # A sync proves processed raw data; it is not a GUI/request snapshot.
            self.record_origin(channel, event, raw, "native_midi", phase="processed")

    def observe_origins(self, stream):
        if stream == self.origin_stream and self.origin_sync_ready:
            return True
        if stream == self.origin_sync_failed_stream and stream:
            return False
        self.detach_origin_observers()
        self.origin_stream = stream
        # Selective channel controls only: never a Python callback on every Note.
        events = tuple(dict.fromkeys((*CHANNEL_NATIVE_FIELDS, EXPRESSION_EVENTS["mode"],
                                     MIDI_EVENT_SYSTEM, MIDI_EVENT_RESET)))
        self.origin_native_observer=NativeMidiState(stream,events,self.origin_replay,clock=self.midi_clock)
        for event in events:
            # bass.h MIXTIME / bassmidi.h BASS_SYNC_MIDI_EVENT; raw in LOWORD,
            # channel in HIWORD. StreamEvent itself does NOT trigger these syncs.
            callback,context=self.origin_native_observer.registration(event,self.origin_callback)
            handle = bass.BASS_ChannelSetSync(stream, 0x40010004, event, callback, context)
            if not handle:
                error = int(bass.BASS_ErrorGetCode())
                self.origin_sync_failed_stream = stream
                failure = RuntimeError(tr("runtime.origin_observer_failed", error=error))
                try:
                    self.detach_origin_observers()
                except Exception as cleanup:
                    failure.add_note(str(cleanup))
                    raise failure from cleanup
                self.origin_sync_failed_stream = stream
                self.append_log(str(failure))
                return False
            self.origin_sync_handles.append(handle)
        self.origin_sync_ready = True
        return True

    def detach_origin_observers(self):
        detach_midi_sync_handles(bass,self.origin_stream,self.origin_sync_handles)
        if self.origin_native_observer is not None:
            self.origin_native_observer.close();self.origin_native_observer=None
        self.origin_stream = 0
        self.origin_sync_ready = False
        self.origin_sync_failed_stream = 0

    def control_origin(self, channel, field, *, key=None):
        """Return only a matching live raw value; unknown stays None, not zero.

        Admission is deliberately distinguished from a native processing sync.
        External StreamEvent callers have no provenance until observed. Matching
        a number alone cannot manufacture its origin after an untracked writer.
        """
        if not self.drain_native_controls():return None
        origin = self.control_origins.get((channel, field, key) if field == "key_pressure" else (channel, field))
        if origin is None or origin.generation != self.stream_generation or not self.stream_handle:
            return None
        if field == "delay_send":
            raw = self.delay_runtime.states[channel].send
        elif field.startswith("modulation_"):
            raw = self.performance.mod_observed.get((channel, field))
        else:
            event = origin.event | ((key << 16) if field == "key_pressure" and key is not None else 0)
            raw = bassmidi.BASS_MIDI_StreamGetEvent(self.stream_handle, channel, event)
        return origin if raw == origin.raw else None

    def detach_channel_filter(self):
        """Retain all mapper/journal borrowers until the filter barrier succeeds."""
        self._require_retired_channel_executor()
        self.channel_sends.flush_pending()
        gate=self.channel_native_filter
        retry=getattr(gate,'retry_publication',None)
        if retry is not None:retry()
        installed=getattr(gate,'installed_stream',0)
        stream=installed or self.stream_handle
        if stream and (installed or self.channel_filter_state and self.channel_filter_state[1]):
            if not bassmidi.BASS_MIDI_StreamSetFilter(stream,False,None,None):
                error=int(bass.BASS_ErrorGetCode())
                if error!=5:
                    failure=RuntimeError(tr('runtime.effect_teardown_failed'))
                    failure.add_note(f'BASS_MIDI_StreamSetFilter: {error}')
                    raise failure
            # Removal succeeded (or HANDLE confirms the old stream is gone).
            # Retirement below can still fail: never cache the removed filter
            # as installed and let a later ensure/Close skip its real barrier.
            self.channel_filter_state=None
            if gate is not None:gate.installed_stream=0
        if gate is not None:
            # Capture during the detach call belongs to the old generation.
            self.drain_native_controls()
            close=getattr(gate,'close',None)
            closed=close(cancel_pending=True) if isinstance(gate,NativeMidiFilterGate) else close() if close is not None else None
            if closed is False:
                raise RuntimeError(tr('runtime.effect_teardown_failed'))
            self.channel_native_filter=None
        self.channel_filter_state=None

    def prepare_raw_decode_context(self,library,*,fresh=False):
        """Explicit stream decoder root; warm state is never guessed fresh."""
        from midi_input_runtime import NativeRawDecodeContext
        with self.midi_lock:
            if self.raw_decode_context is not None:return self.raw_decode_context
            if self.channel_command_executor is not None or not self.stream_handle or (fresh and self.raw_submission_seen):raise ValueError('RAW decoder fresh stream admission')
            if not hasattr(library,'raw_decode_context_version'):return None
            self.raw_decode_context=NativeRawDecodeContext(self.stream_handle,bassmidi,bass,library,fresh=fresh)
            return self.raw_decode_context

    @contextmanager
    def raw_handoff_scope(self,execution,report):
        if execution.owner is not self or report is not execution.pending or report.get('disposition')!=2:
            raise ValueError('RAW decoder handoff actor')
        token=_raw_actor_context.set((self,execution,report))
        try:yield
        finally:_raw_actor_context.reset(token)

    def forward_raw_handoff(self,report):
        """Composition-owned legacy delegate, no reverse engine-domain import."""
        return forward_midi_bytes(report['remaining_payload'],port=report['direct_port'],
            received_stamp=report['read_qpc'],received_origin=report['origin'])

    def observe_raw_submission(self,wire,mode,accepted,error=0):
        """After actual RAW only, never a consumed compatibility frame."""
        self.raw_submission_seen=True
        context=self.raw_decode_context
        if context is not None and not context.observe(wire,mode,successful=accepted,error=error):
            raise RuntimeError(tr('runtime.midi_filter_config_failed',error='RAW decoder submission owner'))

    def observe_midi_event(self,stream,channel,event,parameter,accepted,error=0):
        context=self.raw_decode_context
        if context is not None and event==MIDI_EVENT_RESET and accepted and not _native_callback.get():
            flags=int(bass.BASS_ChannelFlags(stream,0,0))
            if flags==0xffffffff or flags&0x400000:
                # No original single-event sync callback exists. Queued
                # acceptance must not prematurely claim a processed Reset.
                if context.stream!=stream or not context.pending_reset(channel,successful=True,error=error):
                    raise RuntimeError(tr('runtime.midi_filter_config_failed',error='RAW decoder queued Reset uncertainty'))
                return
        if context is not None and (context.stream!=stream or not context.observe_event(event,channel,parameter,successful=accepted,error=error)):
            raise RuntimeError(tr('runtime.midi_filter_config_failed',error='RAW decoder event owner'))

    def observe_midi_batch(self,stream,steps,accepted,error=0):
        context=self.raw_decode_context
        if context is None or not any(event==MIDI_EVENT_RESET for _,event,_ in steps):return
        if not accepted:
            if context.stream!=stream or not context.invalidate(error=error):
                raise RuntimeError(tr('runtime.midi_filter_config_failed',error='RAW decoder partial structured submission'))
            return
        for channel,event,parameter in steps:
            # This helper owns the original FLUSH batch: FLUSH overrides the
            # stream's ASYNC flag, so these are actual processed events.
            if event==MIDI_EVENT_RESET and (context.stream!=stream or not context.observe_event(event,channel,parameter,successful=True,error=0)):
                raise RuntimeError(tr('runtime.midi_filter_config_failed',error='RAW decoder FLUSH event owner'))

    def retire_raw_decode_context(self):
        with self.midi_lock:
            context=self.raw_decode_context
            mapper=getattr(self.channel_native_filter,'function_map',None)
            if context is not None and any(getattr(adapter,'raw_context',None) is context
                    for adapter in getattr(mapper,'selection_adapters',{}).values()):
                # Python adapters use the context on their next RAW call even
                # without an outstanding native Plan. Filter detachment must
                # retire those adapters before this stream root can close.
                return False
            if context is not None and not context.close():return False
            self.raw_decode_context=None;return True

    def bind_stream(self, handle: int) -> None:
        """Publish the newly created stream only after BASSMIDI created it."""
        self._require_retired_channel_executor()
        self.detach_channel_filter()
        if not self.retire_raw_decode_context():raise RuntimeError(tr('runtime.channel_executor_retained'))
        self.playback_timing.close()
        self.detach_origin_observers()
        self.performance.detach_native_observers()
        self.clear_origins()
        self.origin_sync_failed_stream = 0
        self.stream_generation += 1
        self.observation_token = object()
        self.addresses.reset()
        self.drum_delay_warnings.clear()
        self.stream_handle = int(handle)
        self.raw_submission_seen=False
        with self.channel_command_condition:self.channel_command_admission_open=True
        self.soundfont_presets.reset_channels()
        self.channel_sends.reset_channels(apply=False)
        self.midi_options.reset_notes()
        self.soundfont_activity.clear_active()
        self.auxiliary_cc.reset()
        self.performance.mod_observed.clear()
        self.chord_events.begin_generation()
        self.midi_activity.clear()
        if self.diagnostic_trace is not None:
            self.diagnostic_trace.record("stream_bound", stream=self.stream_handle)

    def clear_stream(self) -> None:
        """Invalidate the public handle after stream/device teardown."""
        self._require_retired_channel_executor()
        self.detach_channel_filter()
        if not self.retire_raw_decode_context():raise RuntimeError(tr('runtime.channel_executor_retained'))
        self.playback_timing.close()
        self.detach_origin_observers()
        self.performance.detach_native_observers()
        self.clear_origins()
        self.origin_sync_failed_stream = 0
        self.stream_generation += 1
        self.observation_token = object()
        old_stream = self.stream_handle
        self.stream_handle = 0
        self.raw_submission_seen=False
        self.addresses.reset()
        self.chord_events.begin_generation()
        if self.diagnostic_trace is not None:
            self.diagnostic_trace.record("stream_cleared", stream=old_stream)

    def _stereo_proc(self, _hdsp, _channel, buffer, length, _user):
        _apply_stereo_width(buffer, length, self.stream_effects.stereo.coefficient.value)


runtime = EngineRuntime()

