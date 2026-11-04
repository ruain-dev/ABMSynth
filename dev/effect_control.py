"""BASSMIDI effect-event port independent of Qt controls and preset labels."""

from __future__ import annotations

import ctypes
import math
import threading

from bass_bindings import BASS_MIDI_EVENT, BASS_MIDI_EVENTS_FLUSH
from midi_input_runtime import MASTER_FINE_PARAMETER,MASTER_COARSE_PARAMETER,A4_REFERENCE_HZ
from delay_runtime import BMSYNTH_DELAY_PRESETS

# Positive runtime reception,stable persisted identity,and its opt-out encoding.
# Native modes and processing remain owned by the MIDI runtime,not this UI map.
SYSTEM_RESET_RECEIVERS=(('gm1_reset','ignore_gm1_reset',True,'GM1'),
    ('gm2_reset','ignore_gm2_reset',True,'GM2'),('gs_reset','gs_reset',False,'GS'),
    ('xg_reset','ignore_xg_reset',True,'XG'))


class EffectEvents:
    """One responsibility: send/read effect events on the current MIDI stream."""

    def __init__(self, bassmidi, stream_getter, failure_sink=None):
        self.bassmidi = bassmidi
        self.stream_getter = stream_getter
        self.failure_sink = failure_sink
        self.command_submitter=None;self.macro_submitter=None;self.batch_submitter=None;self.completion_observer=None

    def bind_ordered_commands(self,submitter=None,completion_observer=None,*,macro_submitter=None,batch_submitter=None):
        """Explicit candidate port; no GUI ownership or automatic admission.

        The engine owns retained generation/FIFO/metadata completion. Single
        StreamEvent calls qualify here; FLUSH macro/read transactions do not.
        """
        if submitter is not None and not callable(submitter):raise ValueError('effect command submitter')
        if completion_observer is not None and not callable(completion_observer):raise ValueError('effect completion observer')
        if macro_submitter is not None and not callable(macro_submitter):raise ValueError('effect macro submitter')
        if batch_submitter is not None and not callable(batch_submitter):raise ValueError('effect batch submitter')
        self.command_submitter=submitter;self.completion_observer=completion_observer;self.macro_submitter=macro_submitter;self.batch_submitter=batch_submitter

    def submit(self,event,value,channel=0,*,on_complete=None):
        """Return the engine's typed receipt unchanged; never sync-fallback.

        The observer runs at engine completion, not on a Qt thread. Native
        success/failure and saved UI completion are separate responsibilities.
        """
        if self.command_submitter is None:raise ValueError('effect ordered port not bound')
        complete=self._completion_callback(on_complete)
        receipt=self.command_submitter(((int(channel),int(event),int(round(value))),),on_complete=complete)
        if self._receipt_value(receipt,'completed',False):complete(receipt)
        return receipt

    @staticmethod
    def _receipt_value(receipt,key,default=None):
        return receipt.get(key,default) if isinstance(receipt,dict) else getattr(receipt,key,default)

    def _completion_callback(self,on_complete):
        delivery=threading.RLock();observer=self.completion_observer;observer_done=False;callback_done=False
        def complete(receipt):
            nonlocal observer_done,callback_done
            with delivery:
                if not observer_done:
                    if observer is not None:observer(receipt)
                    observer_done=True
                if not callback_done:
                    if on_complete is not None:on_complete(receipt)
                    callback_done=True
        return complete

    def submit_macro(self,event,value,parameters,channel=0,*,on_complete=None):
        """Explicit queued FLUSH/read; completion(receipt, snapshots-or-None).

        Existing apply_macro_and_read stays synchronous/dict-return. A Qt root
        may consume the copied snapshot on its owner thread, never GetEvent
        again after a later FIFO writer has already changed native values.
        """
        if self.macro_submitter is None:raise ValueError('effect macro port not bound')
        parameters=tuple(tuple(row) for row in parameters)
        if not parameters or any(len(row)!=2 or not isinstance(row[0],str) or not row[0] or type(row[1]) is not int for row in parameters):raise ValueError('effect macro read parameters')
        def completed(receipt):
            values=None
            if self._receipt_value(receipt,'successful',False):
                rows=self._receipt_value(receipt,'results',())
                if len(rows)!=len(parameters)+1 or rows[0][0:2]!=(channel,event) or rows[0][2:4]!=(1,True):raise ValueError('effect macro snapshot contract')
                values={}
                for (name,read),row in zip(parameters,rows[1:]):
                    if row[0:2]!=(channel,read) or not row[3] or row[2]==0xffffffff:raise ValueError('effect macro snapshot identity')
                    values[name]=row[2]
            if on_complete is not None:on_complete(receipt,values)
        complete=self._completion_callback(completed)
        receipt=self.macro_submitter(int(channel),int(event),int(round(value)),tuple(row[1] for row in parameters),on_complete=complete)
        if self._receipt_value(receipt,'completed',False):complete(receipt)
        return receipt

    def _failed(self, operation: str) -> None:
        if self.failure_sink is not None:
            self.failure_sink(operation)

    def send(self, event: int, value: int, channel: int = 0) -> bool:
        if self.command_submitter is not None:
            receipt=self.submit(event,value,channel)
            get=lambda key,default=None:receipt.get(key,default) if isinstance(receipt,dict) else getattr(receipt,key,default)
            return bool(get('accepted',False) and (not get('completed',False) or get('successful',False)))
        stream = int(self.stream_getter())
        if not stream:
            return False
        # bassmidi.h BASS_MIDI_StreamEvent; bassmidi.md: channel-local except
        # global events, TRUE on success. The active BMSynth-like MIDI stream
        # is synchronous, so the event is applied by this call.
        success = bool(self.bassmidi.BASS_MIDI_StreamEvent(stream, channel, event, int(round(value))))
        if not success:
            self._failed("BASS_MIDI_StreamEvent")
        return success

    def send_batch(self, values: tuple[tuple[int, int], ...], channel: int = 0) -> bool:
        if self.batch_submitter is not None:
            if not int(self.stream_getter()):return False
            complete=self._completion_callback(None)
            receipt=self.batch_submitter(int(channel),tuple((int(event),int(round(value))) for event,value in values),on_complete=complete)
            if self._receipt_value(receipt,'completed',False):complete(receipt)
            return bool(self._receipt_value(receipt,'accepted',False) and
                (not self._receipt_value(receipt,'completed',False) or self._receipt_value(receipt,'successful',False)))
        return self._send_batch_immediate(values,channel)

    def _send_batch_immediate(self,values,channel):
        stream = int(self.stream_getter())
        if not stream:
            return False
        events = (BASS_MIDI_EVENT * len(values))()
        for index, (event, value) in enumerate(values):
            events[index].event = int(event)
            events[index].param = int(round(value))
            events[index].chan = channel
        processed = self.bassmidi.BASS_MIDI_StreamEvents(
            stream, BASS_MIDI_EVENTS_FLUSH, ctypes.cast(events, ctypes.c_void_p), len(events),
        )
        success = processed != 0xFFFFFFFF and processed == len(events)
        if not success:
            self._failed("BASS_MIDI_StreamEvents")
        return success

    def apply_macro_and_read(
        self, macro_event: int, macro_value: int, parameters: tuple[tuple[str, int], ...], channel: int = 0,
    ) -> dict[str, int] | None:
        stream = int(self.stream_getter())
        if not stream:
            return None
        # The explicit synchronous dict-return API must remain synchronous
        # even when the UI separately uses the queued snapshot API.
        if not self._send_batch_immediate(((macro_event, macro_value),), channel):
            return None
        # FLUSH above processes pending async input and this macro before
        # reading parameter state (bassmidi.md BASS_MIDI_StreamEvent/GetEvent).
        values = {
            name: int(self.bassmidi.BASS_MIDI_StreamGetEvent(stream, channel, event))
            for name, event in parameters
        }
        if any(value == 0xFFFFFFFF for value in values.values()):
            self._failed("BASS_MIDI_StreamGetEvent")
            return None
        return values



"""Unified BASSMIDI event port and effect-tab application boundary.

The UI captures native BASS/BASSMIDI values in :class:`EffectState` and this
port applies them in the established startup order.  Individual slider and
checkbox callbacks use the same methods, so rebuilding a MIDI stream cannot
silently take a second, differently mapped effect path.
"""

from dataclasses import dataclass, field
from typing import Callable

from bass_bindings import (
    MIDI_EVENT_CHORUS_DELAY,
    MIDI_EVENT_CHORUS_DEPTH,
    MIDI_EVENT_CHORUS_FEEDBACK,
    MIDI_EVENT_CHORUS_LEVEL,
    MIDI_EVENT_CHORUS_RATE,
    MIDI_EVENT_CHORUS_REVERB,
    MIDI_EVENT_REVERB_DELAY,
    MIDI_EVENT_REVERB_HICUTOFF,
    MIDI_EVENT_REVERB_LEVEL,
    MIDI_EVENT_REVERB_LOCUTOFF,
    MIDI_EVENT_REVERB_TIME,
)


REVERB_EVENTS = (
    ("level", MIDI_EVENT_REVERB_LEVEL),
    ("time_ms", MIDI_EVENT_REVERB_TIME),
    ("delay_tenths_ms", MIDI_EVENT_REVERB_DELAY),
    ("high_cutoff_hz", MIDI_EVENT_REVERB_HICUTOFF),
    ("low_cutoff_hz", MIDI_EVENT_REVERB_LOCUTOFF),
)
REVERB_CONTROL_FIELDS = {
    "reverb_level": "level",
    "reverb_time": "time_ms",
    "reverb_delay": "delay_tenths_ms",
    "reverb_hi": "high_cutoff_hz",
    "reverb_lo": "low_cutoff_hz",
}
REVERB_EVENT_BY_FIELD = dict(REVERB_EVENTS)
REVERB_EVENT_BY_CONTROL = {
    control: REVERB_EVENT_BY_FIELD[field]
    for control, field in REVERB_CONTROL_FIELDS.items()
}
CHORUS_EVENTS = (
    ("level", MIDI_EVENT_CHORUS_LEVEL),
    ("delay_tenths_ms", MIDI_EVENT_CHORUS_DELAY),
    ("depth_tenths_ms", MIDI_EVENT_CHORUS_DEPTH),
    ("rate_centihz", MIDI_EVENT_CHORUS_RATE),
    ("feedback", MIDI_EVENT_CHORUS_FEEDBACK),
    ("to_reverb", MIDI_EVENT_CHORUS_REVERB),
)
CHORUS_CONTROL_FIELDS = {
    "ch_level": "level",
    "ch_delay": "delay_tenths_ms",
    "ch_depth": "depth_tenths_ms",
    "ch_rate": "rate_centihz",
    "ch_feedback": "feedback",
    "ch_to_reverb": "to_reverb",
}
CHORUS_EVENT_BY_FIELD = dict(CHORUS_EVENTS)
CHORUS_CONTROL_EVENTS = tuple(
    (control, CHORUS_EVENT_BY_FIELD[field])
    for control, field in CHORUS_CONTROL_FIELDS.items()
)
CHORUS_EVENT_BY_CONTROL = dict(CHORUS_CONTROL_EVENTS)

from effect_presentation import EQ_CENTER_INPUT_STEPS, numeric_entry_domain

@dataclass(frozen=True)
class EffectRowSpec:
    """One visible slider's stable label, domain, default and value unit."""

    attr: str
    label_id: str
    minimum: float
    maximum: float
    initial: float
    kind: str
    input_steps: tuple[float, float, float]
    step: float = 1
    event: int | None = None
    fit_value_spacing: bool = False
    snap_navigation: bool = False
    preserve_value_precision: bool = False

    @property
    def entry_domain(self):
        return numeric_entry_domain(self.kind,self.minimum,self.maximum)

    def validate(self, value):
        return self.entry_domain.validate_raw(value)

    def restore_value(self, value):
        """Legacy product restore, not a clamp on external/native MIDI.

        Keep the existing Qt bounded result and widget grid quantization, but
        report a finite out-of-range input as clamped rather than unchanged.
        Missing/invalid sibling fields and high-precision tuning stay separate.
        """
        number = float(value)
        if not math.isfinite(number): raise ValueError('effect restore domain')
        bounded = max(self.minimum, min(self.maximum, number))
        return bounded, 'accepted' if bounded == number else 'clamped'


# One factory definition feeds interactive Default and missing-INI startup.
# The pinned source's immutable table is documented in the bounded behavior
# map:reverb_preset_application/chorus_preset_application. Delay owns its table.
BMSYNTH_REVERB_DEFAULT = {
    "reverb_level": 100, "reverb_time": 2100, "reverb_delay": 171,
    "reverb_hi": 90, "reverb_lo": 2000,
}
BMSYNTH_CHORUS_DEFAULT = {
    "ch_level": 100, "ch_delay": 171, "ch_depth": 49,
    "ch_rate": 25, "ch_feedback": 100, "ch_to_reverb": 0,
}
EFFECT_FACTORY_VALUES = {
    'reverb':BMSYNTH_REVERB_DEFAULT,
    'chorus':BMSYNTH_CHORUS_DEFAULT,
    'delay':dict(zip(('delay_type','delay_level','delay_time','delay_feedback'),BMSYNTH_DELAY_PRESETS['Default'])),
}

EFFECT_ROWS = {
    "reverb": (
        EffectRowSpec("reverb_level", "effect.level", 0, 200, BMSYNTH_REVERB_DEFAULT['reverb_level'], "reverb_level", event=REVERB_EVENT_BY_CONTROL["reverb_level"], input_steps=(1, 5, 10)),
        EffectRowSpec("reverb_time", "effect.time", 0, 6000, BMSYNTH_REVERB_DEFAULT['reverb_time'], "ms", event=REVERB_EVENT_BY_CONTROL["reverb_time"], input_steps=(10, 50, 100)),
        EffectRowSpec("reverb_delay", "effect.delay", 0, 1000, BMSYNTH_REVERB_DEFAULT['reverb_delay'], "tenths_ms", event=REVERB_EVENT_BY_CONTROL["reverb_delay"], input_steps=(10, 50, 100)),
        EffectRowSpec("reverb_hi", "effect.high_cutoff", 0, 11200, BMSYNTH_REVERB_DEFAULT['reverb_hi'], "hz", event=REVERB_EVENT_BY_CONTROL["reverb_hi"], input_steps=(100, 500, 1000)),
        EffectRowSpec("reverb_lo", "effect.low_cutoff", 0, 11200, BMSYNTH_REVERB_DEFAULT['reverb_lo'], "hz", event=REVERB_EVENT_BY_CONTROL["reverb_lo"], input_steps=(100, 500, 1000)),
    ),
    "chorus": (
        EffectRowSpec("ch_level", "effect.level", 0, 200, BMSYNTH_CHORUS_DEFAULT['ch_level'], "gain_db", event=CHORUS_EVENT_BY_CONTROL["ch_level"], input_steps=(1, 5, 10)),
        EffectRowSpec("ch_delay", "effect.delay", 0, 1000, BMSYNTH_CHORUS_DEFAULT['ch_delay'], "tenths_ms", event=CHORUS_EVENT_BY_CONTROL["ch_delay"], input_steps=(10, 50, 100)),
        EffectRowSpec("ch_depth", "effect.depth", 0, 1000, BMSYNTH_CHORUS_DEFAULT['ch_depth'], "tenths_ms", event=CHORUS_EVENT_BY_CONTROL["ch_depth"], input_steps=(10, 50, 100)),
        EffectRowSpec("ch_rate", "effect.rate", 0, 1000, BMSYNTH_CHORUS_DEFAULT['ch_rate'], "centihz", event=CHORUS_EVENT_BY_CONTROL["ch_rate"], input_steps=(10, 20, 50)),
        EffectRowSpec("ch_feedback", "effect.feedback", 0, 200, BMSYNTH_CHORUS_DEFAULT['ch_feedback'], "bipolar_percent", event=CHORUS_EVENT_BY_CONTROL["ch_feedback"], input_steps=(1, 5, 10)),
        EffectRowSpec("ch_to_reverb", "effect.to_reverb", 0, 200, BMSYNTH_CHORUS_DEFAULT['ch_to_reverb'], "gain_db", event=CHORUS_EVENT_BY_CONTROL["ch_to_reverb"], input_steps=(1, 5, 10)),
    ),
    "delay": (
        EffectRowSpec("delay_level", "effect.level", 0, 200, EFFECT_FACTORY_VALUES['delay']['delay_level'], "delay_level", input_steps=(1, 5, 10)),
        EffectRowSpec("delay_time", "effect.time", 1, 2000, EFFECT_FACTORY_VALUES['delay']['delay_time'], "ms", input_steps=(10, 50, 100)),
        EffectRowSpec("delay_feedback", "effect.feedback", 0, 100, EFFECT_FACTORY_VALUES['delay']['delay_feedback'], "percent", input_steps=(1, 5, 10)),
    ),
    "extended_delay": (
        EffectRowSpec("extra_delay_time", "effect.extra_delay_time", 1, 2000, 250, "ms", input_steps=(10, 50, 100)),
        EffectRowSpec("extra_delay_wet", "effect.extra_delay_wet", 0, 100, 0, "percent", input_steps=(1, 5, 10)),
        EffectRowSpec("extra_delay_feedback", "effect.extra_delay_feedback", 0, 90, 35, "percent", input_steps=(1, 5, 10)),
    ),
    "equalizer": (
        EffectRowSpec("eq_center", "eq.center", 80, 16000, 100, "hz", input_steps=EQ_CENTER_INPUT_STEPS),
        EffectRowSpec("eq_width", "eq.width", 1, 36, 36, "semitones_decimal", step=.1, input_steps=(.1, 1, 5)),
        EffectRowSpec("eq_gain", "eq.gain", -15, 15, 0, "db_decimal", step=.1, input_steps=(.1, 1, 5)),
        EffectRowSpec("eq_output_trim", "eq.output_trim", -24, 0, 0, "db_decimal", step=.1, input_steps=(.1, .5, 1)),
    ),
    "other": (
        EffectRowSpec("global_reverb", "effect.global_reverb", 0, 127, 0, "midi_send", input_steps=(1, 5, 10)),
        EffectRowSpec("global_chorus", "effect.global_chorus", 0, 127, 0, "midi_send", input_steps=(1, 5, 10)),
        EffectRowSpec("stereo", "effect.stereo", 0, 3, 1, "stereo_multiplier", step=.01, input_steps=(.1, .2, .5)),
        EffectRowSpec("velocity", "effect.velocity", 0, 200, 100, "velocity_gap_native", input_steps=(1, 5, 10)),
        EffectRowSpec("dx_level", "effect.spatial", 0, 21, 0, "spatial_mix", input_steps=(1, 2, 5)),
        EffectRowSpec("spatial_input_gain", "effect.spatial_input_gain", -96, 0, 0, "db", input_steps=(1, 3, 6)),
        EffectRowSpec("spatial_time", "effect.spatial_time", 1, 3000, 500, "ms", input_steps=(10, 50, 100)),
        EffectRowSpec("spatial_hf_ratio", "effect.spatial_hf_ratio", .001, .999, .01, "ratio_decimal", step=.001, input_steps=(.01, .05, .1)),
    ),
    "dynamics": (
        EffectRowSpec("comp_threshold", "effect.comp_threshold", -60, 0, -18, "db", input_steps=(1, 3, 6)),
        EffectRowSpec("comp_ratio", "effect.comp_ratio", 1, 10, 3, "ratio", step=.1, input_steps=(.1, .5, 1)),
        EffectRowSpec("comp_attack", "effect.comp_attack", 1, 100, 10, "ms", input_steps=(1, 5, 10)),
        EffectRowSpec("comp_release", "effect.comp_release", 50, 1000, 200, "ms", input_steps=(1, 10, 50)),
        EffectRowSpec("comp_makeup", "effect.comp_makeup", 0, 12, 0, "db", input_steps=(1, 2, 4)),
        EffectRowSpec("limiter_ceiling", "effect.limiter_ceiling", -12, 0, -1, "db_tenth", step=.1, input_steps=(.1, .5, 1)),
        EffectRowSpec("limiter_release", "effect.limiter_release", 20, 500, 100, "ms", input_steps=(1, 10, 50)),
    ),
    "performance": (
        EffectRowSpec("tuning_master", "performance.tuning_value", MASTER_FINE_PARAMETER.minimum, MASTER_FINE_PARAMETER.maximum,
            MASTER_FINE_PARAMETER.default, MASTER_FINE_PARAMETER.entry_kind, step=.01,
            input_steps=MASTER_FINE_PARAMETER.input_steps, fit_value_spacing=True, snap_navigation=True, preserve_value_precision=True),
        EffectRowSpec("tuning_hz", "performance.tuning_value",
            round(A4_REFERENCE_HZ*2**(MASTER_FINE_PARAMETER.minimum/1200),2),
            round(A4_REFERENCE_HZ*2**(MASTER_FINE_PARAMETER.maximum/1200),2),A4_REFERENCE_HZ,
            "hz_decimal",step=.01,input_steps=(.1,.5,1),snap_navigation=True,preserve_value_precision=True),
        EffectRowSpec("tuning_coarse", "performance.tuning_coarse", MASTER_COARSE_PARAMETER.minimum, MASTER_COARSE_PARAMETER.maximum,
            MASTER_COARSE_PARAMETER.default, MASTER_COARSE_PARAMETER.entry_kind, input_steps=MASTER_COARSE_PARAMETER.input_steps),
    ),
    "humanization": (
        EffectRowSpec("humanize_timing", "performance.humanize_timing", 0, 300, 0, "ms", input_steps=(1, 5, 10)),
        EffectRowSpec("humanize_velocity", "performance.humanize_velocity", 0, 64, 0, "midi_send", input_steps=(1, 4, 8)),
    ),
}

EFFECT_ROW_BY_CONTROL = {row.attr: row for rows in EFFECT_ROWS.values() for row in rows}
# Pure product value -> state bindings. There is no Qt/widget ownership here.
# Reverb/chorus reuse their event bindings; other models share the same named
# controls as UI construction/INI/direct-entry metadata. First-run preset rows
# use the same factory values as interactive Default;INI scalars still win.
EFFECT_STATE_CONTROL_FIELDS = {
    'root': {'stereo':'stereo_multiplier','global_reverb':'reverb_send_floor',
        'global_chorus':'chorus_send_floor','dx_level':'spatial_level','velocity':'velocity_gap',
        'humanize_timing':'humanize_timing_ms','humanize_velocity':'humanize_velocity'},
    'reverb': REVERB_CONTROL_FIELDS,
    'chorus': CHORUS_CONTROL_FIELDS,
    'delay': {'delay_level':'level','delay_time':'time_ms','delay_feedback':'feedback'},
    'extended_delay': {'extra_delay_time':'time_ms','extra_delay_wet':'wet_percent',
        'extra_delay_feedback':'feedback_percent'},
    'compressor': {'comp_threshold':'threshold_db','comp_ratio':'ratio',
        'comp_attack':'attack_ms','comp_release':'release_ms','comp_makeup':'makeup_db'},
    'limiter': {'limiter_ceiling':'ceiling_db','limiter_release':'release_ms'},
    'spatial_parameters': {'spatial_input_gain':'input_gain_db','spatial_time':'time_ms',
        'spatial_hf_ratio':'high_frequency_ratio'},
}
EFFECT_CAPTURE_CONTROLS = tuple(key for fields in EFFECT_STATE_CONTROL_FIELDS.values() for key in fields)
_EFFECT_FLOAT_CONTROLS = frozenset(('stereo','comp_ratio','limiter_ceiling',
    'spatial_input_gain','spatial_time','spatial_hf_ratio'))


def effect_model_values(group, values):
    """One named, bounded product conversion for every model capture path."""
    result={}
    for control,field in EFFECT_STATE_CONTROL_FIELDS[group].items():
        source=values.get(control,0) if control in ('humanize_timing','humanize_velocity') else values[control]
        value=EFFECT_ROW_BY_CONTROL[control].validate(source)
        result[field]=float(value) if control in _EFFECT_FLOAT_CONTROLS else int(round(value))
    return result


def delay_state_from_controls(values, type_index, sysex_enabled):
    return DelayState(type_index=type_index,sysex_enabled=bool(sysex_enabled),
                      **effect_model_values('delay',values))


def extended_delay_state_from_controls(values, ping_pong):
    numbers=effect_model_values('extended_delay',values)
    return ExtendedDelayState(enabled=numbers['wet_percent']>0,ping_pong=bool(ping_pong),**numbers)


@dataclass(frozen=True)
class ReverbState:
    level: int
    time_ms: int
    delay_tenths_ms: int
    high_cutoff_hz: int
    low_cutoff_hz: int
    preset: str = "Custom"


@dataclass(frozen=True)
class ChorusState:
    level: int
    delay_tenths_ms: int
    depth_tenths_ms: int
    rate_centihz: int
    feedback: int
    to_reverb: int
    preset: str


@dataclass(frozen=True)
class DelayState:
    type_index: int
    level: int
    time_ms: int
    feedback: int
    sysex_enabled: bool


@dataclass(frozen=True)
class ExtendedDelayState:
    enabled: bool = False
    time_ms: int = 250
    wet_percent: int = 0
    feedback_percent: int = 35
    ping_pong: bool = False

    def validated(self) -> "ExtendedDelayState":
        wet = max(0, min(100, int(self.wet_percent)))
        return ExtendedDelayState(bool(self.enabled) and wet > 0,
                                  max(1, min(2000, int(self.time_ms))),
                                  wet,
                                  max(0, min(90, int(self.feedback_percent))),
                                  bool(self.ping_pong))


@dataclass(frozen=True)
class EQBand:
    center_hz: float
    bandwidth_semitones: float = 12
    gain_db: float = 0
    kind: str = "bell"


EQ_FILTER_KINDS = ("bell", "high_pass", "low_pass", "low_shelf", "high_shelf")


EQ_DEFAULT_BANDS = (
    EQBand(100, 36), EQBand(15000, 36),
)
# ABMSynth starting curves, not BASS factory presets. Local bass.md BASS_DX8_PARAMEQ
# defines these as bell filters. Tonal regions follow iZotope's EQ frequency
# guide; exact centers, bandwidths and conservative gains are project choices.
EQ_PRESETS = {
    "off": EQ_DEFAULT_BANDS,
    "warm": (EQBand(180, 18, 2.5), EQBand(3500, 24, -1.5)),
    "bright": (EQBand(300, 18, -1.5), EQBand(8000, 24, 2)),
    "presence": (EQBand(350, 12, -2), EQBand(3000, 18, 2.5)),
    "low_end_control": (EQBand(100, 18, -3), EQBand(350, 12, -1.5)),
}
EQ_MAX_BANDS = 32

@dataclass(frozen=True)
class EqualizerState:
    bands: tuple[EQBand, ...] = EQ_DEFAULT_BANDS
    bypassed: bool = False
    output_trim_db: float = 0.0

    def validated(self, sample_rate: int):
        if len(self.bands) > EQ_MAX_BANDS:
            raise ValueError("Equalizer band limit exceeded")
        if not math.isfinite(self.output_trim_db):
            raise ValueError("Equalizer output trim must be finite")
        if any(not math.isfinite(value) for b in self.bands
               for value in (b.center_hz, b.bandwidth_semitones, b.gain_db)):
            raise ValueError("Equalizer values must be finite")
        if any(b.kind not in EQ_FILTER_KINDS for b in self.bands):
            raise ValueError("Unknown equalizer filter kind")
        maximum = min(16000, sample_rate / 3)
        return EqualizerState(tuple(EQBand(
            max(80, min(maximum, band.center_hz)),
            max(1, min(36, band.bandwidth_semitones)),
            max(-15, min(15, band.gain_db)),
            band.kind,
        ) for band in self.bands), self.bypassed,
                              max(-24.0, min(0.0, self.output_trim_db)))

    def ini_values(self):
        return {"eq_band_count": str(len(self.bands)),
                "eq_output_trim": str(self.output_trim_db), **{
            f"eq_band_{index}": ",".join((str(float(band.center_hz)),
                                           str(float(band.bandwidth_semitones)),
                                           str(float(band.gain_db)), band.kind))
            for index, band in enumerate(self.bands)
        }}

    @classmethod
    def from_ini(cls, values, sample_rate, *, report=None, prefix=''):
        if "eq_band_count" in values:
            count = int(values["eq_band_count"])
            if not 0 <= count <= EQ_MAX_BANDS:
                raise ValueError("Equalizer band limit exceeded")
            bands = []
            for index in range(count):
                raw = values.get(f"eq_band_{index}")
                if raw is None:
                    continue
                parts = raw.split(",")
                if len(parts) != 4:
                    if report is not None: report.record('UI',prefix+f'eq_band_{index}','invalid')
                    continue
                try:
                    band = EQBand(*(float(value) for value in parts[:3]), parts[3])
                    bands.extend(cls((band,)).validated(sample_rate).bands)
                    if report is not None: report.record('UI',prefix+f'eq_band_{index}','accepted')
                except (ValueError, TypeError):
                    if report is not None: report.record('UI',prefix+f'eq_band_{index}','invalid')
                    continue
        else:
            bands = EQ_DEFAULT_BANDS
        try:
            trim = float(values.get("eq_output_trim", "0"))
            if not math.isfinite(trim):
                if report is not None: report.record('UI',prefix+'eq_output_trim','invalid')
                trim = 0.0
        except (ValueError, TypeError):
            if report is not None: report.record('UI',prefix+'eq_output_trim','invalid')
            trim = 0.0
        return cls(tuple(bands), output_trim_db=trim).validated(sample_rate)


@dataclass(frozen=True)
class CompressorState:
    enabled: bool = False
    threshold_db: int = -18
    ratio: float = 3.0
    attack_ms: int = 10
    release_ms: int = 200
    makeup_db: int = 0


@dataclass(frozen=True)
class LimiterState:
    enabled: bool = False
    ceiling_db: float = -1.0
    release_ms: int = 100


@dataclass(frozen=True)
class SpatialParameters:
    """DX8 reverb parameters not covered by BMSynth's 0..21 mix position."""

    input_gain_db: float = 0.0
    time_ms: float = 500.0
    high_frequency_ratio: float = 0.01
    sample_rate: int = 0

    def validated(self) -> "SpatialParameters":
        values = (self.input_gain_db, self.time_ms, self.high_frequency_ratio)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("Spatial reverb parameters must be finite")
        from settings import SAMPLE_RATES
        rate = int(self.sample_rate)
        if isinstance(self.sample_rate, bool) or rate != self.sample_rate or rate not in (0, *SAMPLE_RATES):
            raise ValueError("Invalid spatial processing sample rate")
        return SpatialParameters(
            max(-96.0, min(0.0, float(self.input_gain_db))),
            max(0.001, min(3000.0, float(self.time_ms))),
            round(max(0.001, min(0.999, float(self.high_frequency_ratio))), 3),
            rate,
        )


@dataclass(frozen=True)
class AudioFXParameter:
    key: str
    minimum: float
    maximum: float
    initial: float
    step: float = .1
    unit: str = ""
    choices: tuple = ()
    input_steps: tuple = ()

    @property
    def entry_kind(self):
        decimals=max(0,-round(math.log10(self.step)))
        return {0:'number_integer',1:'number_tenth',2:'number_hundredth',3:'number_thousandth'}[decimals]

    @property
    def entry_domain(self):
        return numeric_entry_domain(self.entry_kind,self.minimum,self.maximum)

    def validate(self, value):
        value=self.entry_domain.validate_raw(value)
        if (self.choices or self.step>=1) and not math.isclose(value/self.step,round(value/self.step),abs_tol=1e-8):
            raise ValueError('Audio FX discrete parameter')
        return value


def _fxp(key, low, high, initial, step=.1, unit="", choices=()):
    navigation = (step, step * 5, step * 10)
    if unit == "Hz" and key == "rate" and step <= .1: navigation = (.1, .2, .5)
    elif unit == "ms" and "delay" in key: navigation = (1, 5, 10)
    elif key == "reverb_time": navigation = (10, 50, 100)
    elif unit == "Hz" and key in ("frequency", "pre_cutoff", "post_center", "post_bandwidth") and high >= 8000:
        navigation = (100, 500, 1000)
    return AudioFXParameter(key, low, high, initial, step, unit, choices, navigation)


# Current bass.h/bass_fx.h/CHM domains. Chosen starting sounds are not claims
# of universal factory presets. Native flags/masks remain the runtime's ABI.
_FX_MIX = (_fxp("dry", 0, 100, 85, .1, "%"), _fxp("wet", 0, 100, 15, .1, "%"))
_FX_SWEEP = (*_FX_MIX, _fxp("feedback", -99, 99, 20, .1, "%"),
    _fxp("rate", .01, 9.99, .5, .01, "Hz"), _fxp("range", .01, 9.99, 2, .01, "octaves"),
    # Header/manual notation at1000 is ambiguous; installed FX2.4.12.6
    # rejects exactly1000 with BASS_ERROR_ILLPARAM. Integer UI ends at999.
    _fxp("frequency", 1, 999, 300, 1, "Hz"))
_FX_FILTER = (_fxp("frequency", 80, 16000, 1000, 1, "Hz"),
              _fxp("bandwidth", .1, 9.9, 1, .1, "octaves"))
RACK_BQF_KINDS = ("band_pass", "notch", "all_pass", "parametric_eq", "low_pass",
                  "high_pass", "low_shelf", "high_shelf")
AUDIO_FX_PARAMETERS = {
    "flanger": (_fxp("wet", 0, 100, 35, .1, "%"), _fxp("depth", 0, 100, 60, .1, "%"),
        _fxp("feedback", -99, 99, -35, .1, "%"), _fxp("rate", 0, 10, .25, .01, "Hz"),
        _fxp("waveform", 0, 1, 1, 1, choices=("triangle", "sine")),
        _fxp("delay", 0, 4, 2, .01, "ms"), _fxp("phase", -180, 180, 0, 90, "°", ("-180", "-90", "0", "+90", "+180"))),
    "phaser": _FX_SWEEP,
    "auto_wah": _FX_SWEEP,
    "chorus_fx": (*_FX_MIX, _fxp("feedback", -99, 99, 15, .1, "%"),
        _fxp("min_delay", .1, 100, 15, .1, "ms"), _fxp("max_delay", .1, 100, 25, .1, "ms"),
        _fxp("sweep_rate", .1, 1000, 5, .1, "ms/s")),
    "freeverb": (*_FX_MIX, _fxp("room_size", 0, 1, .5, .01),
        _fxp("damping", 0, 1, .5, .01), _fxp("width", 0, 1, 1, .01),
        _fxp("freeze", 0, 1, 0, 1, choices=("off", "on"))),
    # Application safe-start preset, NOT BASS_FX's factory preset. Native
    # curve heavily amplifies tiny input even at low Drive; wet must be small.
    "distortion": (_fxp("drive", 0, 5, .02, .01), _fxp("dry", 0, 100, 95, .1, "%"), _fxp("wet", 0, 100, .1, .1, "%"),
        _fxp("feedback", -99, 99, 0, .1, "%"), _fxp("output", -24, 6, -3, .1, "dB")),
    "echo": (*_FX_MIX, _fxp("feedback", -90, 90, 20, .1, "%"),
        _fxp("delay", 1, 2000, 250, 1, "ms"), _fxp("ping_pong", 0, 1, 0, 1, choices=("off", "on"))),
    "gargle": (_fxp("rate", 1, 1000, 20, 1, "Hz"),
        _fxp("waveform", 0, 1, 0, 1, choices=("triangle", "square"))),
    "compressor_fx": (_fxp("output", -24, 24, 0, .1, "dB"),
        _fxp("threshold", -60, 0, -18, .1, "dB"), _fxp("ratio", 1, 20, 3, .1),
        _fxp("attack", .1, 1000, 10, .1, "ms"), _fxp("release", 1, 5000, 200, 1, "ms")),
    "auto_gain": (_fxp("target", .01, 1, .1, .01), _fxp("quiet", 0, 1, .02, .01),
        _fxp("rate", 0, 1, .03, .01), _fxp("gain", 1, 8, 1, .01), _fxp("wait", 0, 10, 2, .01, "s")),
    "rotate": (_fxp("rate", -10, 10, .25, .01, "Hz"),),
    "band_pass": _FX_FILTER,
    "notch": _FX_FILTER,
    "all_pass": _FX_FILTER,
    "pitch_shift": (_fxp("semitones", -12, 12, 0, .01, "semitones"),
        _fxp("fft_size", 0, 3, 1, 1, choices=("1024", "2048", "4096", "8192")),
        _fxp("oversampling", 4, 32, 8, 1)),
    "tremolo": (_fxp("rate", .05, 20, 4, .01, "Hz"), _fxp("depth", 0, 100, 50, .1, "%"),
        _fxp("waveform", 0, 2, 0, 1, choices=("sine", "triangle", "square"))),
    "auto_pan": (_fxp("rate", .05, 20, .5, .01, "Hz"), _fxp("depth", 0, 100, 70, .1, "%")),
    "ring_mod": (_fxp("frequency", 1, 5000, 100, 1, "Hz"), _fxp("wet", 0, 100, 50, .1, "%")),
    "bitcrusher": (_fxp("bits", 2, 24, 12, 1), _fxp("hold", 1, 64, 1, 1, "samples"),
        _fxp("wet", 0, 100, 50, .1, "%")),
    "saturator": (_fxp("drive", 0, 36, 6, .1, "dB"), _fxp("wet", 0, 100, 50, .1, "%"),
        _fxp("output", -24, 12, -3, .1, "dB"),
        _fxp("saturation_quality",0,2,0,1,choices=("1x","2x","4x")),
        _fxp("quality_transition_ms",0,500,0,1,"ms")),
    "gate": (_fxp("threshold", -90, 0, -60, .1, "dB"), _fxp("attack", .1, 100, 2, .1, "ms"),
        _fxp("hold", 0, 500, 50, 1, "ms"), _fxp("release", 1, 1000, 100, 1, "ms"),
        _fxp("hysteresis", 0, 12, 3, .1, "dB")),
    "dc_block": (_fxp("frequency", 2, 30, 10, .1, "Hz"),),
    "stereo_tools": (_fxp("swap", 0, 1, 0, 1, choices=("off", "on")),
        _fxp("invert_left", 0, 1, 0, 1, choices=("off", "on")),
        _fxp("invert_right", 0, 1, 0, 1, choices=("off", "on"))),
    "headphone_stage": (_fxp("azimuth", -180, 180, 0, 1, "°"),
        _fxp("elevation", -90, 90, 0, 1, "°"), _fxp("speaker_angle", 0, 180, 60, 1, "°"),
        _fxp("distance", .1, 10, 1, .1, "m"), _fxp("wet", 0, 100, 100, .1, "%"),
        _fxp("output", -24, 12, -3, .1, "dB"), _fxp("kernel_transition_ms",0,500,0,1,"ms")),
    "convolution": (_fxp("wet", 0, 100, 25, .1, "%"), _fxp("output", -48, 12, -6, .1, "dB"),
        _fxp("kernel_transition_ms",0,500,0,1,"ms")),
    "gain": (_fxp("output", -48, 24, 0, .1, "dB"), _fxp("transition_ms",0,500,0,1,"ms")),
    "channel_matrix": (_fxp("ll", -200, 200, 100, .1, "%"), _fxp("lr", -200, 200, 0, .1, "%"),
        _fxp("rl", -200, 200, 0, .1, "%"), _fxp("rr", -200, 200, 100, .1, "%")),
    "volume_envelope": (_fxp("start_level", 0, 200, 0, .1, "%"), _fxp("peak_level", 0, 200, 100, .1, "%"),
        _fxp("end_level", 0, 200, 100, .1, "%"), _fxp("attack", 1, 10000, 100, 1, "ms"),
        _fxp("hold", 1, 10000, 100, 1, "ms"), _fxp("release", 1, 10000, 100, 1, "ms")),
    "chorus_dx8": (_fxp("wet", 0, 100, 35, .1, "%"), _fxp("depth", 0, 100, 60, .1, "%"),
        _fxp("feedback", -99, 99, 20, .1, "%"), _fxp("rate", 0, 10, .8, .01, "Hz"),
        _fxp("waveform", 0, 1, 1, 1, choices=("triangle", "sine")),
        _fxp("delay", 0, 20, 10, .01, "ms"), _fxp("phase", -180, 180, 90, 90, "°", ("-180", "-90", "0", "+90", "+180"))),
    "distortion_dx8": (_fxp("output", -60, 0, -36, .1, "dB"), _fxp("edge", 0, 100, 5, .1, "%"),
        _fxp("post_center", 100, 8000, 2400, 1, "Hz"), _fxp("post_bandwidth", 100, 8000, 2400, 1, "Hz"),
        _fxp("pre_cutoff", 100, 8000, 8000, 1, "Hz")),
    "echo_dx8": (_fxp("wet", 0, 100, 35, .1, "%"), _fxp("feedback", 0, 99, 35, .1, "%"),
        _fxp("left_delay", 1, 2000, 250, 1, "ms"), _fxp("right_delay", 1, 2000, 375, 1, "ms"),
        _fxp("ping_pong", 0, 1, 0, 1, choices=("off", "on"))),
    "reverb_dx8": (_fxp("input_gain", -96, 0, 0, .1, "dB"), _fxp("reverb_mix", -96, 0, -12, .1, "dB"),
        _fxp("reverb_time", .001, 3000, 1500, .001, "ms"), _fxp("high_ratio", .001, .999, .5, .001)),
    "parametric_eq": (*_FX_FILTER, _fxp("gain", -18, 18, 0, .1, "dB")),
    "low_pass": (_fxp("frequency", 80, 16000, 16000, 1, "Hz"), _fxp("q", .1, 1, .707, .001)),
    "high_pass": (_fxp("frequency", 80, 16000, 80, 1, "Hz"), _fxp("q", .1, 1, .707, .001)),
    "low_shelf": (_fxp("frequency", 80, 16000, 200, 1, "Hz"),
        _fxp("gain", -18, 18, 0, .1, "dB"), _fxp("slope", .1, 1, 1, .01)),
    "high_shelf": (_fxp("frequency", 80, 16000, 5000, 1, "Hz"),
        _fxp("gain", -18, 18, 0, .1, "dB"), _fxp("slope", .1, 1, 1, .01)),
}
AUDIO_FX_GROUPS = {
    "modulation": ("flanger", "phaser", "auto_wah", "chorus_fx", "chorus_dx8", "tremolo", "gargle", "ring_mod"),
    "space": ("freeverb", "reverb_dx8", "convolution", "headphone_stage", "echo", "echo_dx8"),
    "dynamics": ("compressor_fx", "auto_gain", "gate", "volume_envelope"),
    "tone": ("parametric_eq", "low_pass", "high_pass", "low_shelf", "high_shelf",
             "distortion", "distortion_dx8", "saturator", "bitcrusher", "band_pass", "notch", "all_pass", "pitch_shift", "dc_block"),
    "stereo": ("auto_pan", "rotate", "stereo_tools", "channel_matrix", "gain"),
}
# Primary musical controls stay visible; algorithm/engineering choices disclose.
AUDIO_FX_ADVANCED = {"pitch_shift": (1, 2), "chorus_dx8": (4, 6), "flanger": (4, 6),
                     "distortion_dx8": (2, 3, 4), "distortion": (3,),
                     "saturator":(3,4),"gain":(1,),"headphone_stage":(6,),"convolution":(2,)}
AUDIO_FX_MAX_SLOTS = 32


@dataclass(frozen=True)
class AudioFXSlot:
    kind: str
    enabled: bool = True
    values: tuple = ()
    asset: str = ""
    extras: tuple = field(default=(), compare=False, repr=False)
    identity: str = ""

    def validated(self):
        specs = AUDIO_FX_PARAMETERS.get(self.kind)
        if specs is None:
            raise ValueError("Unknown audio FX")
        values = self.values or tuple(p.initial for p in specs)
        if len(values) != len(specs) or type(self.enabled) is not bool or not isinstance(self.asset, str) or not isinstance(self.identity, str):
            raise ValueError("Audio FX parameter count/state")
        numbers = []
        for value, spec in zip(values, specs):
            numbers.append(spec.validate(value))
        if self.kind == "chorus_fx" and numbers[3] > numbers[4]:
            raise ValueError("Audio FX sweep order")
        return AudioFXSlot(self.kind, self.enabled, tuple(numbers), self.asset, self.extras, self.identity)


def validate_audio_fx(slots):
    if len(slots) > AUDIO_FX_MAX_SLOTS:
        raise ValueError("Audio FX slot limit")
    return tuple(slot.validated() for slot in slots)


def audio_fx_cc_bindings(slot):
    """Optional CC assignments use stable current parameter names."""
    bindings=dict(slot.extras).get('midi_cc',{})
    if not isinstance(bindings,dict):raise ValueError('audio effect CC mapping')
    keys={parameter.key for parameter in AUDIO_FX_PARAMETERS[slot.kind]}
    if any(key not in keys or (type(value) is not int or not 0<=value<120) and value!='learn'
           for key,value in bindings.items()):raise ValueError('audio effect CC source')
    return dict(bindings)


def audio_fx_cc_value(parameter,cc):
    """MIDI7-bit source,existing parameter domain and precision,nearest grid."""
    value=parameter.minimum+(parameter.maximum-parameter.minimum)*cc/127
    ticks=math.floor((value-parameter.minimum)/parameter.step+.5)
    return parameter.validate(max(parameter.minimum,min(parameter.maximum,
        round(parameter.minimum+ticks*parameter.step,parameter.entry_domain.decimals))))


def audio_fx_from_ini(raw, *, report=None):
    from configuration_data import read_settings_json
    try:
        records = read_settings_json(raw)
        if not isinstance(records, list):
            if report is not None: report.record('UI','audio_fx_chain','invalid')
            return ()
    except (ValueError, TypeError):
        if report is not None: report.record('UI','audio_fx_chain','invalid')
        return ()
    slots = []
    for index,record in enumerate(records):
        if len(slots) >= AUDIO_FX_MAX_SLOTS: break
        try:
            extras = tuple((key, value) for key, value in record.items() if key not in ("kind", "enabled", "values", "asset", "id", "parameters", "parameter_schema"))
            named = record['parameters']
            if type(record.get('parameter_schema')) is not int or record['parameter_schema']!=1 or not isinstance(named,dict):
                raise ValueError('audio FX schema')
            values = ()
            if isinstance(named,dict) and record["kind"] in AUDIO_FX_PARAMETERS:
                specs = AUDIO_FX_PARAMETERS[record["kind"]]
                values = []
                for spec in specs:
                    value = named.get(spec.key,spec.initial)
                    try:
                        checked = spec.validate(value)
                    except (ValueError,TypeError):
                        value = spec.initial
                        if report is not None: report.record('UI',f'audio_fx_chain.{index}.{spec.key}','invalid')
                    values.append(value)
                unknown = {key:value for key,value in named.items() if key not in {spec.key for spec in specs}}
                if unknown: extras += (("parameters",unknown),)
            slots.append(AudioFXSlot(record["kind"], record.get("enabled", True),
                                     tuple(values), record.get("asset", ""), extras, record.get("id", "")).validated())
            if report is not None: report.record('UI',f'audio_fx_chain.{index}','accepted')
        except (ValueError, TypeError, KeyError, AttributeError):
            if report is not None: report.record('UI',f'audio_fx_chain.{index}','invalid_or_unsupported')
            continue
    return tuple(slots)


def audio_fx_ini(slots):
    import json
    return json.dumps([{**dict(s.extras), "kind": s.kind, "enabled": s.enabled,
                       "parameter_schema":1,
                       "parameters":{**dict(s.extras).get("parameters",{}),
                                     **{spec.key:value for spec,value in zip(AUDIO_FX_PARAMETERS[s.kind],s.values)}},
                       **({"asset": s.asset} if s.asset else {}), **({"id": s.identity} if s.identity else {})}
                       for s in validate_audio_fx(slots)], separators=(",", ":"))


@dataclass(frozen=True)
class EffectState:
    master_volume: int
    stereo_multiplier: float
    reverb_send_floor: int
    chorus_send_floor: int
    reverb: ReverbState
    chorus: ChorusState
    delay: DelayState
    spatial_level: int
    velocity_gap: int
    overnote: bool
    gs_reset: bool
    equalizer: EqualizerState = field(default_factory=EqualizerState)
    compressor: CompressorState = field(default_factory=CompressorState)
    limiter: LimiterState = field(default_factory=LimiterState)
    spatial_parameters: SpatialParameters = field(default_factory=SpatialParameters)
    extended_delay: ExtendedDelayState = field(default_factory=ExtendedDelayState)
    audio_fx: tuple = ()
    audio_fx_enabled: bool = True
    gm1_reset: bool = True
    gm2_reset: bool = True
    xg_reset: bool = True
    channel_audio_fx: str = '{}'
    humanize_timing_ms: int = 0
    humanize_velocity: int = 0
    humanize_preserve_simultaneity: bool = False

    @classmethod
    def from_controls(cls, values, *, master_volume, reverb_preset, chorus_preset,
                      delay_type, delay_sysex, extra_delay_ping_pong, compressor_enabled,
                      limiter_enabled, overnote, gs_reset, equalizer, audio_fx,
                      audio_fx_enabled, spatial_sample_rate=0,gm1_reset=True,gm2_reset=True,xg_reset=True,channel_audio_fx='{}',humanize_preserve_simultaneity=False):
        """Capture already-present product controls without a second UI mapping.

        Composite EQ/flags/presets keep their established owners and semantics;
        numeric fields use the same row metadata as construction/restore/entry.
        This must not normalize native DAW readback or manufacture mixed zero.
        """
        return cls(master_volume=master_volume,**effect_model_values('root',values),
            reverb=ReverbState(preset=reverb_preset,**effect_model_values('reverb',values)),
            chorus=ChorusState(preset=chorus_preset,**effect_model_values('chorus',values)),
            delay=delay_state_from_controls(values,delay_type,delay_sysex),
            extended_delay=extended_delay_state_from_controls(values,extra_delay_ping_pong),
            compressor=CompressorState(enabled=bool(compressor_enabled),**effect_model_values('compressor',values)),
            limiter=LimiterState(enabled=bool(limiter_enabled),**effect_model_values('limiter',values)),
            spatial_parameters=SpatialParameters(**effect_model_values('spatial_parameters',values),sample_rate=spatial_sample_rate).validated(),
            overnote=bool(overnote),gs_reset=bool(gs_reset),equalizer=equalizer,
            audio_fx=audio_fx,audio_fx_enabled=audio_fx_enabled,
            gm1_reset=bool(gm1_reset),gm2_reset=bool(gm2_reset),xg_reset=bool(xg_reset),channel_audio_fx=channel_audio_fx,humanize_preserve_simultaneity=bool(humanize_preserve_simultaneity))


class EffectRuntime:
    """Translate native effect state into the existing engine/event calls."""

    def __init__(self, engine, effect_events):
        self.engine = engine
        self.effect_events = effect_events
        self._reverb_by_field = REVERB_EVENT_BY_FIELD
        self._chorus_by_field = CHORUS_EVENT_BY_FIELD

    def bind_ordered_commands(self,submitter=None,completion_observer=None,*,macro_submitter=None,batch_submitter=None):
        return self.effect_events.bind_ordered_commands(submitter,completion_observer,macro_submitter=macro_submitter,batch_submitter=batch_submitter)

    def send_event(self, event: int, value: int | float) -> bool:
        return self.effect_events.send(event, value)

    def set_reverb_parameter(self, field: str, value: int | float) -> bool:
        return self.send_event(self._reverb_by_field[field], value)

    def set_reverb_control(self, control: str, value: int | float) -> bool:
        return self.set_reverb_parameter(REVERB_CONTROL_FIELDS[control], value)

    def set_chorus_parameter(self, field: str, value: int | float) -> bool:
        return self.send_event(self._chorus_by_field[field], value)

    def set_chorus_control(self, control: str, value: int | float) -> bool:
        return self.set_chorus_parameter(CHORUS_CONTROL_FIELDS[control], value)

    def mode_reset_events(self,state: EffectState):
        """Global MIDI settings lost by SYSTEMEX; no DSP/native work here.

        Reuse the same field/event/unit mappings as apply_state. External
        DSPs,SoundFonts,send policy and channel Reset keep their own owners.
        """
        from bass_bindings import MIDI_EVENT_MASTERVOL
        return ((0,MIDI_EVENT_MASTERVOL,16383),)+tuple(
            (0,event,int(round(getattr(part,field))))
            for part,fields in ((state.reverb,REVERB_EVENTS),(state.chorus,CHORUS_EVENTS)) for field,event in fields)

    def set_master_volume(self, value: int | float, *, on_complete=None):
        if on_complete is None:return self.engine.set_master_volume(value)
        return self.engine.set_master_volume(value,on_complete=on_complete)

    def set_stereo_multiplier(self, value: float) -> None:
        return self.engine.set_stereo_multiplier(value)

    def set_reverb_send_floor(self, value: int | float) -> None:
        if getattr(self.effect_events,'command_submitter',None) is not None:
            from bass_bindings import MIDI_EVENT_REVERB
            return self.engine.submit_global_send_floor(MIDI_EVENT_REVERB,value)
        self.engine.set_global_reverb_send(value)

    def set_chorus_send_floor(self, value: int | float) -> None:
        if getattr(self.effect_events,'command_submitter',None) is not None:
            from bass_bindings import MIDI_EVENT_CHORUS
            return self.engine.submit_global_send_floor(MIDI_EVENT_CHORUS,value)
        self.engine.set_global_chorus_send(value)

    def set_delay(self, state: DelayState) -> None:
        self.engine.set_delay(state.type_index, state.level, state.time_ms, state.feedback)

    def set_extended_delay(self, state: ExtendedDelayState) -> None:
        return self.engine.set_extended_delay(state.validated())

    def set_audio_fx(self, slots, enabled=True):
        return self.engine.set_audio_fx(slots if enabled else ())

    def request_audio_fx(self, slots, enabled=True):
        effective = slots if enabled else ()
        if self.engine.audio_fx_needs_preparation(effective):
            return self.engine.request_audio_fx(effective)
        return None, self.set_audio_fx(slots, enabled)

    def poll_audio_fx(self, token):
        return self.engine.poll_audio_fx(token)

    def completed_audio_fx_request(self,token):
        read=getattr(self.engine,'audio_fx_request_completed',None)
        return read(token) if read is not None else None

    def set_equalizer(self, state: EqualizerState) -> None:
        return self.engine.set_equalizer(state)

    def equalizer_response(self, state):
        return self.engine.equalizer_response(state)

    def poll_equalizer_response(self, state):
        return self.engine.poll_equalizer_response(state)

    def set_compressor(self, state: CompressorState) -> None:
        return self.engine.set_compressor(
            state.enabled, state.threshold_db, state.ratio,
            state.attack_ms, state.release_ms, state.makeup_db,
        )

    def set_limiter(self, state: LimiterState) -> None:
        return self.engine.set_true_peak_limiter(state.enabled, state.ceiling_db, state.release_ms)

    def set_spatial(self, level: int | float, parameters: SpatialParameters | None = None) -> None:
        return self.engine.set_dx_reverb(level > 0, level, (parameters or SpatialParameters()).validated())

    def set_velocity(self, gap: int | float) -> None:
        self.engine.set_velocity_rate(gap)

    def set_humanization(self,timing_ms,velocity,preserve_simultaneity=False):
        return self.engine.set_humanization(int(timing_ms),int(velocity),bool(preserve_simultaneity))

    def set_overnote(self, enabled: bool) -> None:
        self.engine.set_overnote(enabled)

    def set_gs_reset(self, enabled: bool) -> None:
        if getattr(self.effect_events,'command_submitter',None) is not None:
            return self.engine.submit_reset_policy({'gs_reset':bool(enabled)},
                on_complete=self.effect_events._completion_callback(None))
        self.engine.set_gs_reset(enabled)

    def set_system_reset_receive(self,field,enabled):
        if field not in ('gm1_reset','gm2_reset','xg_reset'):raise ValueError('system reset field')
        if getattr(self.effect_events,'command_submitter',None) is not None:
            return self.engine.submit_reset_policy({field:bool(enabled)},
                on_complete=self.effect_events._completion_callback(None))
        self.engine.set_system_reset_receive(field,enabled)

    def set_delay_sysex(self, enabled: bool) -> None:
        if getattr(self.effect_events,'command_submitter',None) is not None:
            return self.engine.submit_reset_policy({'delay_sysex':bool(enabled)},
                on_complete=self.effect_events._completion_callback(None))
        self.engine.set_delay_sysex(enabled)

    def apply_state(
        self, state: EffectState, apply_reverb_preset: Callable[[str], None],
        apply_chorus_preset: Callable[[str], None],
    ) -> bool:
        """Apply one captured state in the existing stream-recreation order.

        Source startup 0046B37F/0046B396 selects non-Default macros first;
        the first 0046C93C refresh sends the saved per-parameter values after
        those macros. The incoming state is captured before any callback, so
        a macro cannot overwrite user/INI parameters or their visible rows.
        """
        self.set_master_volume(state.master_volume)
        self.set_stereo_multiplier(state.stereo_multiplier)
        self.set_reverb_send_floor(state.reverb_send_floor)
        self.set_chorus_send_floor(state.chorus_send_floor)
        if state.reverb.preset not in ("Default", "Custom"):
            apply_reverb_preset(state.reverb.preset)
        if state.chorus.preset not in ("Default", "Custom"):
            apply_chorus_preset(state.chorus.preset)
        for field, _event in REVERB_EVENTS:
            self.set_reverb_parameter(field, getattr(state.reverb, field))
        for field, _event in CHORUS_EVENTS:
            self.set_chorus_parameter(field, getattr(state.chorus, field))
        self.set_delay(state.delay)
        self.set_extended_delay(state.extended_delay)
        audio_fx_applied = self.set_audio_fx(state.audio_fx, state.audio_fx_enabled)
        self.set_equalizer(state.equalizer)
        self.set_compressor(state.compressor)
        self.set_limiter(state.limiter)
        self.set_spatial(state.spatial_level, state.spatial_parameters)
        self.set_velocity(state.velocity_gap)
        self.set_humanization(state.humanize_timing_ms,state.humanize_velocity,state.humanize_preserve_simultaneity)
        self.set_overnote(state.overnote)
        self.set_gs_reset(state.gs_reset)
        for field in ('gm1_reset','gm2_reset','xg_reset'):
            self.set_system_reset_receive(field,getattr(state,field))
        self.set_delay_sysex(state.delay.sysex_enabled)
        return audio_fx_applied is not False


