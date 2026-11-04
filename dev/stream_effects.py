"""Small BASS effect objects bound to one MIDI-stream generation.

The controller owns the BASS device. Each effect owns only its own handles
and sound-setting rule. `StreamEffects` groups their shared attach/detach
lifecycle; BASS_Free releases the underlying resources after detach.
"""

from __future__ import annotations

import ctypes
import os
import math
import threading
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from i18n import tr

from bass_bindings import BASS_MIDI_CHAN_USERFX, BASS_SAMPLE_FLOAT, BASS_STREAM_DECODE, BASS_SYNC_FREE, DSPPROC, SYNCPROC, NativeChannelTransaction
from effect_control import EQ_DEFAULT_BANDS, EqualizerState, ExtendedDelayState, SpatialParameters


def _remove_fx_checked(bass, check, stream, handle):
    """Control-side ownership barrier; a FALSE removal is not detachment."""
    if handle and not check(bass.BASS_ChannelRemoveFX(stream,handle),tr("audio_fx.operation.remove")):
        raise RuntimeError(tr("runtime.effect_teardown_failed"))


# bass.h BASS_FX_DX8_PARAMEQ / BASS_FX_DX8_REVERB and their exact C layouts.
BASS_FX_DX8_PARAMEQ = 7
BASS_FX_DX8_COMPRESSOR = 1
BASS_FX_DX8_REVERB = 8
BASS_FX_VOLUME = 9
# bass_fx.h: the BQF add-on type is 0x10000 + enum slot 19.
BASS_FX_BFX_BQF = 0x10013
BASS_FX_BFX_ECHO4 = 0x10014
BQF_KIND = {"high_pass": 1, "low_pass": 0, "low_shelf": 7, "high_shelf": 8}


class BASS_DX8_PARAMEQ(ctypes.Structure):
    _fields_ = [("fCenter", ctypes.c_float), ("fBandwidth", ctypes.c_float), ("fGain", ctypes.c_float)]


# bass.h BASS_FX_VOLUME_PARAM; 0 dB is a detached, bit-identical path.
class BASS_FX_VOLUME_PARAM(ctypes.Structure):
    _fields_ = [("fTarget", ctypes.c_float), ("fCurrent", ctypes.c_float),
                ("fTime", ctypes.c_float), ("lCurve", ctypes.c_uint32)]


def master_output_value(value):
    """Host fader policy:257 positions,attenuation only;not MIDI master data."""
    return max(0,min(256,int(round(float(value)))))


def master_output_gain(value):
    # Continuous square-law preserves the old dry MIDI fader's approximate
    # loudness taper without its33-position quantization. Unity is256.
    return (master_output_value(value)/256.0)**2


class MasterOutputVolume:
    """Final float-PCM fader,after protection,before meters/recording.

    bass.h BASS_FX_VOLUME_PARAM:linear amplitude,current=-1 retains the current
    ramp level,time is seconds. No Python render callback or device attenuation.
    The handle lives for one stream generation;live edits use the native FIFO.
    """
    PRIORITY=-9000
    TRANSITION_SECONDS=.010

    def __init__(self,bass,check):
        self.bass,self.check=bass,check;self.stream=self.handle=0;self.value=256

    @staticmethod
    def parameters(value,initial=False):
        gain=master_output_gain(value)
        return BASS_FX_VOLUME_PARAM(gain,gain if initial else -1,0 if initial else MasterOutputVolume.TRANSITION_SECONDS,0)

    def attach(self,stream):
        self.detach();self.stream=int(stream)
        self.handle=self.check(self.bass.BASS_ChannelSetFX(self.stream,BASS_FX_VOLUME,self.PRIORITY),tr('runtime.master_output_attach'))
        if not self.handle:raise RuntimeError(tr('runtime.master_output_failed'))
        if not self.check(self.bass.BASS_FXSetParameters(self.handle,ctypes.byref(self.parameters(self.value,True))),tr('runtime.master_output_apply')):
            raise RuntimeError(tr('runtime.master_output_failed'))

    def set(self,value):
        value=master_output_value(value)
        if not self.stream:self.value=value;return True
        result=self.check(self.bass.BASS_FXSetParameters(self.handle,ctypes.byref(self.parameters(value,self.initial_position()))),tr('runtime.master_output_apply'))
        if result:self.value=value
        return bool(result)

    def initial_position(self):
        # A saved zero must be silent from the very first rendered sample.
        # Before decoding starts there is no prior audible level to crossfade.
        read=getattr(self.bass,'BASS_ChannelGetPosition',None)
        return bool(self.stream and callable(read) and read(self.stream,0)==0)

    def detach(self):
        if self.stream and self.handle:_remove_fx_checked(self.bass,self.check,self.stream,self.handle)
        self.stream=self.handle=0;return True


# bass_fx.h BASS_BFX_BQF. Pass filters use Butterworth Q; shelf slope 1
# remains monotonic. These deliberately fixed values avoid a second width
# control with unlike units beside the legacy semitone bell-band width.
class BASS_BFX_BQF(ctypes.Structure):
    _fields_ = [("lFilter", ctypes.c_int), ("fCenter", ctypes.c_float),
                ("fGain", ctypes.c_float), ("fBandwidth", ctypes.c_float),
                ("fQ", ctypes.c_float), ("fS", ctypes.c_float),
                ("lChannel", ctypes.c_int)]


# bass_fx.h BASS_BFX_ECHO4: final-stream optional stereo echo, deliberately
# separate from the BMSynth-compatible per-channel CC94 delay DSP.
class BASS_BFX_ECHO4(ctypes.Structure):
    _fields_ = [("fDryMix", ctypes.c_float), ("fWetMix", ctypes.c_float),
                ("fFeedback", ctypes.c_float), ("fDelay", ctypes.c_float),
                ("bStereo", ctypes.c_int), ("lChannel", ctypes.c_int)]


def eq_band_active(band):
    return band.kind in ("high_pass", "low_pass") or abs(band.gain_db) >= 1e-7


def eq_band_parameters(band):
    if band.kind == "bell":
        return BASS_DX8_PARAMEQ(band.center_hz, band.bandwidth_semitones, band.gain_db)
    return BASS_BFX_BQF(BQF_KIND[band.kind], band.center_hz,
                        band.gain_db if band.kind.endswith("shelf") else 0,
                        0, 0.70710678 if band.kind.endswith("pass") else 0,
                        1 if band.kind.endswith("shelf") else 0, -1)


class BASS_DX8_REVERB(ctypes.Structure):
    _fields_ = [
        ("fInGain", ctypes.c_float), ("fReverbMix", ctypes.c_float),
        ("fReverbTime", ctypes.c_float), ("fHighFreqRTRatio", ctypes.c_float),
    ]


# bass.h/md BASS_DX8_COMPRESSOR: gain/attack/release/
# threshold/ratio/predelay float order, with Windows DX8 stream lifetime.
class BASS_DX8_COMPRESSOR(ctypes.Structure):
    _fields_ = [
        ("fGain", ctypes.c_float), ("fAttack", ctypes.c_float),
        ("fRelease", ctypes.c_float), ("fThreshold", ctypes.c_float),
        ("fRatio", ctypes.c_float), ("fPredelay", ctypes.c_float),
    ]


class BassEqualizer:
    """Variable DX8 ParamEQ owner; zero gain/bypass detach real effects."""

    @staticmethod
    def priority(index, count):
        return 15 if index == 0 else 10 if index == count - 1 else 12

    def __init__(self, bass, check):
        self.bass, self.check = bass, check
        self.stream = 0
        self.sample_rate = 96000
        self.handles = [0] * len(EQ_DEFAULT_BANDS)
        self.handle_kinds = [None] * len(EQ_DEFAULT_BANDS)
        self.trim_handle = 0
        self.state = EqualizerState()
        self._parameter_transaction=NativeChannelTransaction(bass)
        self._parameter_pending=None
        self._response_cache = None
        self._response_lock = threading.RLock()
        self._response_executor = None
        self._response_future = None
        self._response_requested = None

    def attach(self, stream, sample_rate=96000):
        self.detach()
        self.stream = stream
        self.sample_rate = max(11025, int(sample_rate))
        self.state = EqualizerState().validated(self.sample_rate)

    def detach(self):
        # A response probe borrows this generation's device, not the MIDI
        # stream's PCM. Join before either stream or BASS_Free can invalidate it.
        if self._parameter_transaction.pending is not None:
            if not self._parameter_transaction.run(self.stream,()).success:
                raise RuntimeError(tr('runtime.effect_teardown_failed'))
            self._parameter_pending=None
        if self._response_executor is not None:
            self._response_executor.shutdown(wait=True, cancel_futures=True)
        self._response_executor = self._response_future = self._response_requested = None
        for index in range(len(self.handles)):
            self._remove(index)
        self._remove_trim()
        self.stream = 0
        self._response_cache = None

    def _remove(self, index):
        handle = self.handles[index]
        if self.stream and handle:
            _remove_fx_checked(self.bass,self.check,self.stream,handle)
        self.handles[index] = 0
        self.handle_kinds[index] = None

    def _remove_trim(self):
        if self.stream and self.trim_handle:
            _remove_fx_checked(self.bass,self.check,self.stream,self.trim_handle)
        self.trim_handle = 0

    def apply(self, state):
        state = state.validated(self.sample_rate)
        if not self.stream:
            self.state = state
            return True
        previous = self.state
        if self._parameter_pending is not None:
            pending=self._parameter_pending
            if not self._parameter_transaction.run(self.stream,()).success:return False
            self._parameter_pending=None
            if pending[2][0]==pending[3]:self.state=pending[0];previous=self.state
            elif pending[2][4]<pending[2][0]:
                try:self._apply_fx(pending[1])
                except RuntimeError:return False
        same=(not state.bypassed and not previous.bypassed and len(state.bands)==len(previous.bands)==len(self.handles)
            and all(handle and before.kind==after.kind and eq_band_active(before) and eq_band_active(after)
                for handle,before,after in zip(self.handles,previous.bands,state.bands))
            and bool(self.trim_handle)==(abs(state.output_trim_db)>=1e-7)==(abs(previous.output_trim_db)>=1e-7))
        if same:
            steps=[(handle,eq_band_parameters(after),eq_band_parameters(before)) for handle,before,after in zip(self.handles,previous.bands,state.bands) if before!=after]
            if self.trim_handle and previous.output_trim_db!=state.output_trim_db:
                steps.append((self.trim_handle,BASS_FX_VOLUME_PARAM(10**(state.output_trim_db/20),-1,0,0),BASS_FX_VOLUME_PARAM(10**(previous.output_trim_db/20),-1,0,0)))
            if not steps:self.state=state;return True
            if len(steps)<=32:
                result=self._parameter_transaction.parameters(self.stream,steps)
                if result is not None:
                    if result[3]==3:self._parameter_pending=(state,previous,result,len(steps))
                    elif result[3]==0:self.state=state
                    elif result[4]<result[0]:
                        try:self._apply_fx(previous)
                        except RuntimeError:return False
                    if result[3]:self.check(0,'Equalizer parameters')
                    return result[3]==0
        try:
            self._apply_fx(state)
        except RuntimeError:
            # Do not publish an uninstalled curve. Restore the audible
            # snapshot if a newly requested BQF/volume FX is unavailable.
            try:
                self._apply_fx(previous)
            except RuntimeError:
                for index in range(len(self.handles)):
                    self._remove(index)
                self._remove_trim()
                self.state = EqualizerState().validated(self.sample_rate)
            return False
        self.state = state
        return True

    def _require(self, result, operation):
        checked = self.check(result, operation)
        if not checked:
            raise RuntimeError(operation)
        return checked

    def _apply_fx(self, state):
        if len(self.handles) != len(state.bands):
            for index in range(len(self.handles)):
                self._remove(index)
            self.handles = [0] * len(state.bands)
            self.handle_kinds = [None] * len(state.bands)
        for index, band in enumerate(state.bands):
            priority = self.priority(index, len(state.bands))
            if state.bypassed or not eq_band_active(band):
                self._remove(index)
                continue
            if self.handle_kinds[index] not in (None, band.kind):
                self._remove(index)
            if not self.handles[index]:
                self.handles[index] = self._require(
                    self.bass.BASS_ChannelSetFX(self.stream,
                                                BASS_FX_DX8_PARAMEQ if band.kind == "bell" else BASS_FX_BFX_BQF,
                                                priority),
                    "Equalizer attach",
                )
                self.handle_kinds[index] = band.kind if self.handles[index] else None
            if self.handles[index]:
                self._require(self.bass.BASS_FXSetParameters(
                    self.handles[index], ctypes.byref(eq_band_parameters(band)),
                ), "Equalizer parameters")
        if state.bypassed or abs(state.output_trim_db) < 1e-7:
            self._remove_trim()
        else:
            if not self.trim_handle:
                self.trim_handle = self._require(
                    self.bass.BASS_ChannelSetFX(self.stream, BASS_FX_VOLUME, 7),
                    "Equalizer output trim attach",
                )
            if self.trim_handle:
                level = 10 ** (state.output_trim_db / 20)
                self._require(self.bass.BASS_FXSetParameters(
                    self.trim_handle, ctypes.byref(BASS_FX_VOLUME_PARAM(level, -1, 0, 0)),
                ), "Equalizer output trim parameters")

    def response(self, state=None):
        """Synchronous diagnostic entry; production UI uses poll_response."""
        with self._response_lock:
            return self._measure_response(state)

    def poll_response(self, state):
        """Nonblocking latest-only response pump; caller owns UI scheduling.

        At most one measurement exists. While it runs, intermediate drag
        states are not queued; the next poll submits the newest state. A
        completed curve may trail the immediate point/audio by one job.
        """
        state = state.validated(self.sample_rate)
        completed = None
        if self._response_future is not None and self._response_future.done():
            completed = self._response_requested, self._response_future.result()
            self._response_future = None
        if self._response_future is None and state != self._response_requested:
            if self._response_executor is None:
                self._response_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="EQ-response")
            self._response_requested = state
            self._response_future = self._response_executor.submit(self.response, state)
        return completed, self._response_future is not None

    def _measure_response(self, state=None):
        """Measure the saved EQ, without playback or touching the MIDI stream.

        bass.h STREAMPROC_DUMMY=0; bass.md StreamCreate/GetData permit feeding
        an impulse through a separate FLOAT|DECODE stream. Its FFT is the
        actual BASS filter response, not an assumed biquad approximation.
        Production calls run on the response worker, never on the GUI/audio
        callback. bass.md BASS_SetDevice selects a thread-local device, so
        probing on that worker does not change the playback thread's device.
        Temporary FX/stream are freed before returning; bypass only changes
        presentation, so the saved curve stays available for comparison.
        """
        state = (state or self.state).validated(self.sample_rate)
        key = self.sample_rate, state.bands, state.output_trim_db
        if self._response_cache is not None and self._response_cache[0] == key:
            return self._response_cache[1]
        frequencies = np.geomspace(20, min(20000, self.sample_rate * .49), 384)
        if not any(eq_band_active(band) for band in state.bands):
            response = frequencies, np.full_like(frequencies, state.output_trim_db)
        elif not self.stream:
            return None
        else:
            previous_device = self.bass.BASS_GetDevice()
            device = self.bass.BASS_ChannelGetDevice(self.stream)
            if device == 0xFFFFFFFF or not self.bass.BASS_SetDevice(device):
                return None
            probe = self.bass.BASS_StreamCreate(
                self.sample_rate, 1, BASS_SAMPLE_FLOAT | BASS_STREAM_DECODE,
                None, None,
            )
            if not probe:
                self.bass.BASS_SetDevice(previous_device)
                return None
            try:
                for index, band in enumerate(state.bands):
                    priority = self.priority(index, len(state.bands))
                    if not eq_band_active(band):
                        continue
                    handle = self.bass.BASS_ChannelSetFX(
                        probe, BASS_FX_DX8_PARAMEQ if band.kind == "bell" else BASS_FX_BFX_BQF, priority)
                    if not handle or not self.bass.BASS_FXSetParameters(
                        handle, ctypes.byref(eq_band_parameters(band)),
                    ):
                        return None
                if abs(state.output_trim_db) > 1e-7:
                    handle = self.bass.BASS_ChannelSetFX(probe, BASS_FX_VOLUME, 7)
                    if not handle or not self.bass.BASS_FXSetParameters(
                        handle, ctypes.byref(BASS_FX_VOLUME_PARAM(
                            10 ** (state.output_trim_db / 20), -1, 0, 0)),
                    ):
                        return None
                impulse = np.zeros(131072, dtype=np.float32)
                amplitude = 10 ** (-(20 + sum(max(0, band.gain_db) for band in state.bands)) / 20)
                impulse[0] = amplitude  # bounded headroom for overlapping bands
                got = self.bass.BASS_ChannelGetData(
                    probe, impulse.ctypes.data_as(ctypes.c_void_p), impulse.nbytes,
                )
                if got != impulse.nbytes:
                    return None
                spectrum = np.abs(np.fft.rfft(impulse)) / amplitude
                fft_hz = np.fft.rfftfreq(len(impulse), 1 / self.sample_rate)
                gain = 20 * np.log10(np.maximum(spectrum, 1e-12))
                response = frequencies, np.interp(frequencies, fft_hz, gain)
            finally:
                self.bass.BASS_StreamFree(probe)
                self.bass.BASS_SetDevice(previous_device)
        for array in response:
            array.setflags(write=False)
        self._response_cache = key, response
        return response

class ExtendedDelay:
    """Optional post-mix echo; no handle and no PCM work at neutral default."""

    def __init__(self, bass, check):
        self.bass, self.check = bass, check
        self.stream = self.handle = 0
        self.state = ExtendedDelayState()

    def attach(self, stream):
        self.detach()
        self.stream = stream
        self.state = ExtendedDelayState()

    def detach(self):
        if self.stream and self.handle:
            _remove_fx_checked(self.bass,self.check,self.stream,self.handle)
        self.stream = self.handle = 0

    def set(self, state: ExtendedDelayState):
        state = state.validated()
        if not self.stream:
            self.state = state
            return True
        if not state.enabled:
            if self.handle:
                _remove_fx_checked(self.bass,self.check,self.stream,self.handle)
            self.handle = 0
            self.state = state
            return True
        created = False
        if not self.handle:
            self.handle = self.check(
                self.bass.BASS_ChannelSetFX(self.stream, BASS_FX_BFX_ECHO4, 6),
                "Extended delay attach",
            )
            created = bool(self.handle)
            if not created:
                return False
        if self.handle:
            parameters = BASS_BFX_ECHO4(
                1, state.wet_percent / 100, state.feedback_percent / 100,
                state.time_ms / 1000, int(state.ping_pong), -1,
            )
            if not self.check(self.bass.BASS_FXSetParameters(
                self.handle, ctypes.byref(parameters),
            ), "Extended delay parameters"):
                if created:
                    _remove_fx_checked(self.bass,self.check,self.stream,self.handle)
                    self.handle = 0
                else:
                    previous = self.state
                    restored = self.bass.BASS_FXSetParameters(self.handle, ctypes.byref(BASS_BFX_ECHO4(
                        1, previous.wet_percent / 100, previous.feedback_percent / 100,
                        previous.time_ms / 1000, int(previous.ping_pong), -1,
                    )))
                    if not restored:
                        _remove_fx_checked(self.bass,self.check,self.stream,self.handle)
                        self.handle = 0
                        self.state = ExtendedDelayState()
                        return "disabled"
                return False
        self.state = state
        return True


class Compressor:
    """Optional DX8 dynamics stage; FX priority is after EQ, before DSP."""

    def __init__(self, bass, check):
        self.bass, self.check = bass, check
        self.detach()

    def attach(self, stream):
        self.detach()
        self.stream = stream

    def detach(self):
        if getattr(self, "stream", 0) and getattr(self, "handle", 0):
            _remove_fx_checked(self.bass,self.check,self.stream,self.handle)
        self.stream = 0
        self.handle = 0

    def set(self, enabled, threshold_db, ratio, attack_ms, release_ms, makeup_db):
        if not self.stream:
            return
        if not enabled:
            if self.handle:
                _remove_fx_checked(self.bass,self.check,self.stream,self.handle)
            self.handle = 0
            return
        if not self.handle:
            self.handle = self.check(
                self.bass.BASS_ChannelSetFX(self.stream, BASS_FX_DX8_COMPRESSOR, 5),
                "Compressor attach",
            )
        if self.handle:
            params = BASS_DX8_COMPRESSOR(
                max(-60.0, min(60.0, float(makeup_db))),
                max(0.01, min(500.0, float(attack_ms))),
                max(50.0, min(3000.0, float(release_ms))),
                max(-60.0, min(0.0, float(threshold_db))),
                max(1.0, min(100.0, float(ratio))),
                0.0,
            )
            self.check(self.bass.BASS_FXSetParameters(self.handle, ctypes.byref(params)),
                       "Compressor parameters")


class TruePeakLimiter:
    """Optional stereo-linked 4x FIR-estimated intersample peak limiter.

    Fixed 5ms lookahead delays only the enabled output. A 17-tap windowed-sinc
    interpolator estimates four phases/sample; a forward risk envelope starts
    attenuation before the approaching peak, with a release-only smoothing
    path. This is an approximate 4x dBTP ceiling, not a certified meter.
    Production uses the native preallocated kernel; process_block remains the
    Python arithmetic oracle. DSP/FREE sync resources belong to this owner.
    No Python, Qt, allocation, mutex or I/O runs on the normal audio callback.
    """

    FIR_RADIUS = 8
    OVERSAMPLE = 4
    LOOKAHEAD_MS = 5

    def __init__(self, bass, check, failure_sink=None, *, native_dll=None):
        self.bass, self.check, self.failure_sink = bass, check, failure_sink
        # Preserved Python arithmetic oracle, not the active BASS DSP.
        self.callback = DSPPROC(self._process_dsp)
        from pathlib import Path
        self.native_path = Path(native_dll or os.environ.get('ABMSYNTH_OBSERVATION_DLL',Path(__file__).with_name("abmsynth_dsp.dll")))
        self._transaction=NativeChannelTransaction(bass,self.native_path)
        self.native_library = self.native_callback = self.pointer = None
        self._control = threading.RLock()
        self.free_callback = SYNCPROC(self._on_free)
        self.free_sync = 0
        taps = np.arange(-self.FIR_RADIUS, self.FIR_RADIUS + 1)
        window = np.kaiser(taps.size, 8.6)
        self.kernels = tuple(
            (lambda kernel: kernel / kernel.sum())(np.sinc(taps - phase / self.OVERSAMPLE) * window)
            for phase in range(self.OVERSAMPLE)
        )
        self.stream = self.handle = 0
        self.sample_rate = 96000
        self.ceiling_db = -1.0
        self.release_ms = 100
        self.failed = False
        self._reset_processing()

    def _reset_processing(self):
        self.lookahead = max(1, round(self.sample_rate * self.LOOKAHEAD_MS / 1000))
        self.grain = max(8, round(self.sample_rate / 3000))
        self.delay = self.lookahead + self.FIR_RADIUS
        self.pending = np.zeros((self.delay, 2), dtype=np.float32)
        self.history = np.zeros((self.FIR_RADIUS, 2), dtype=np.float32)
        self.gain = 1.0
        self.failed = False

    def attach(self, stream, sample_rate=96000):
        self.detach()
        self.stream, self.handle = int(stream), 0
        self.sample_rate = max(11025, int(sample_rate))
        self._reset_processing()

    def detach(self):
        with self._control:
            self._remove_native()
            if self.pointer:
                self.native_library.limiter_destroy(self.pointer)
                self.pointer = None
            self.stream = self.handle = 0
        # Removal synchronizes the native DSP lifetime; nevertheless keep
        # its pending/history arrays intact until the next attach(), after
        # the old stream is fully freed. Never mutate them in this phase.

    def _on_free(self, sync, channel, _data, _user):
        # bass.h/md terminal FREE: no BASS calls. Keep native state owned until
        # control-side detach. Some BASS StreamFree paths do not deliver NULL.
        if sync==self.free_sync and channel==self.stream:
            self.stream = self.handle = self.free_sync = 0

    def _remove_native(self):
        actions=[]
        if self.stream and self.handle:actions.append(('handle',self.bass.BASS_ChannelRemoveDSP,self.handle))
        if self.stream and self.free_sync:actions.append(('free_sync',self.bass.BASS_ChannelRemoveSync,self.free_sync))
        result=self._transaction.run(self.stream,((1,function,self.stream,handle) for _,function,handle in actions))
        for name,_,_ in actions[:result.completed]:setattr(self,name,0)
        if not result.success:
            raise RuntimeError(tr('native_limiter.remove_failed' if result.phase==2 else 'native_limiter.lock_failed'))

    def _prepare_native(self):
        if self.native_library is None:
            try:
                library = ctypes.CDLL(str(self.native_path.resolve()))
                library.limiter_version.argtypes,library.limiter_version.restype = [],ctypes.c_uint
                if library.limiter_version()!=1: raise ValueError("limiter ABI1")
                for name, arguments, result in (
                    ("limiter_create",[ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(ctypes.c_double)],ctypes.c_void_p),
                    ("limiter_destroy",[ctypes.c_void_p],None),
                    ("limiter_parameters",[ctypes.c_void_p,ctypes.c_double,ctypes.c_double],ctypes.c_int),
                ):
                    function = getattr(library,name); function.argtypes,function.restype = arguments,result
                # Installed bass.h DSPPROC export. State is destroyed only after
                # DSP removal synchronizes processing; no Python PCM thunk.
                self.native_callback = DSPPROC(("limiter_bass_dsp",library))
            except (OSError,AttributeError,ValueError) as error:
                raise RuntimeError(tr("native_limiter.prepare_failed",error=error)) from error
            self.native_library = library
        if self.pointer:
            self.native_library.limiter_destroy(self.pointer)
            self.pointer = None
        coefficients = (ctypes.c_double*68)(*np.concatenate(self.kernels))
        self.pointer = self.native_library.limiter_create(self.lookahead,self.grain,coefficients)
        if not self.pointer: raise MemoryError(tr("native_limiter.allocation_failed"))

    def _publish_native(self):
        # Preserve Python double pow/exp and native float32 gain/output stages.
        ceiling = 10.**(self.ceiling_db/20.)*.96
        release = 1.-math.exp(-self.grain/(self.sample_rate*self.release_ms/1000.))
        if not self.native_library.limiter_parameters(self.pointer,ceiling,release):
            raise RuntimeError(tr("native_limiter.parameter_failed"))

    def set(self, enabled, ceiling_db=-1.0, release_ms=100):
        with self._control:
            self.ceiling_db = max(-12.0, min(0.0, float(ceiling_db)))
            self.release_ms = max(20, min(500, int(round(float(release_ms)))))
            if not self.stream: return
            if not enabled:
                self._remove_native()
                self._reset_processing()
                return
            if not self.handle:
                # Retry a previously failed sync removal before state replacement.
                self._remove_native()
                self._reset_processing()
                self._prepare_native()
                self._publish_native()
                # bass.md priority: EQ/comp/rack/stereo before, meter after.
                self.handle = self.bass.BASS_ChannelSetDSP(self.stream,self.native_callback,self.pointer,-1000)
                if not self.handle: raise RuntimeError(tr("native_limiter.attach_failed"))
                self.free_sync = self.bass.BASS_ChannelSetSync(self.stream,BASS_SYNC_FREE,0,self.free_callback,None)
                if not self.free_sync:
                    self._remove_native()
                    raise RuntimeError(tr("native_limiter.sync_failed"))
            else: self._publish_native()

    def process_block(self, frames: np.ndarray) -> np.ndarray:
        """Preserved Python reference; production registers native_callback."""
        source = np.asarray(frames, dtype=np.float32)
        if source.ndim != 2 or source.shape[1] != 2:
            raise ValueError("stereo frames required")
        count = source.shape[0]
        if not count:
            return source.copy()
        source = np.nan_to_num(source, nan=0.0, posinf=0.0, neginf=0.0)
        combined = np.concatenate((self.pending, source), axis=0)
        padded = np.concatenate((self.history, combined), axis=0)
        peaks = np.zeros(count + self.lookahead, dtype=np.float64)
        for channel in range(2):
            for kernel in self.kernels:
                interpolated = np.correlate(padded[:, channel], kernel, mode="valid")
                np.maximum(peaks, np.abs(interpolated), out=peaks)
        # Group the peak envelope into ~0.33ms slices. This bounds the audio
        # callback's lookahead work instead of allocating N x 5ms windows at
        # every block, while each slice still uses its worst 4x FIR peak.
        # A modest FIR-reconstruction margin also covers gain steps between
        # adjacent ~0.33ms envelope slices. The user ceiling is still the
        # exposed target, not a claim of laboratory-certified dBTP accuracy.
        ceiling = 10.0 ** (self.ceiling_db / 20.0) * 0.96
        grain = self.grain
        block_count = (count + grain - 1) // grain
        future_blocks = (self.lookahead + grain - 1) // grain
        padded_peaks = np.pad(peaks, (0, -(len(peaks) % grain) % grain))
        block_peaks = padded_peaks.reshape((-1, grain)).max(axis=1)
        needed_blocks = block_count + future_blocks
        if block_peaks.size < needed_blocks:
            block_peaks = np.pad(block_peaks, (0, needed_blocks - block_peaks.size))
        windows = np.lib.stride_tricks.sliding_window_view(block_peaks, future_blocks + 1)[:block_count]
        offset_weight = np.maximum(0.0, 1.0 - np.arange(future_blocks + 1) * grain / self.lookahead)
        risk = np.maximum(0.0, 1.0 - ceiling / np.maximum(windows, 1e-12)) * offset_weight
        needed = 1.0 - risk.max(axis=1)
        release = 1.0 - math.exp(-grain / (self.sample_rate * self.release_ms / 1000.0))
        block_gains = np.empty(block_count, dtype=np.float32)
        gain = self.gain
        for index, required in enumerate(needed):
            gain = min(float(required), gain + release * (1.0 - gain))
            block_gains[index] = gain
        self.gain = gain
        gains = np.repeat(block_gains, grain)[:count]
        output = combined[:count] * gains[:, None]
        self.pending = combined[count:].copy()
        self.history = np.concatenate((self.history, combined[:count]))[-self.FIR_RADIUS:]
        return output

    def _process_dsp(self, _hdsp, _channel, buffer, length, _user):
        if not buffer or length < 8:
            return
        count = length // (2 * ctypes.sizeof(ctypes.c_float))
        samples = np.ctypeslib.as_array(
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_float)), shape=(count * 2,),
        ).reshape((count, 2))
        try:
            samples[:] = self.process_block(samples)
        except Exception as exc:
            samples.fill(0.0)
            if not self.failed and self.failure_sink is not None:
                self.failed = True
                self.failure_sink("True-peak limiter DSP", str(exc))


class _FixedSpatialParameters(ctypes.Structure):
    _fields_=[('fx',BASS_DX8_REVERB),('dry',ctypes.c_float)]


class SpatialReverb:
    """Selectable-rate DX8, original-rate dry and fixed6ms wet alignment.

    A mocked BASS keeps the old direct API oracle. A real backend requires
    the native ABI; missing/broken native processing is never silently used.
    """

    def __init__(self, bass, check):
        self.bass, self.check = bass, check
        self.dll=self.pointer=self.native_callback=None
        self.fx_handle=0;self._dry_cache={}
        self._fixed=isinstance(getattr(bass,'BASS_StreamCreate',None),ctypes._CFuncPtr)
        self._transaction=NativeChannelTransaction(bass)
        self.detach()

    def _required(self,value):
        if not value:raise RuntimeError(tr('spatial_runtime.failure',code=int(self.bass.BASS_ErrorGetCode())))
        return value

    def _prepare(self, requested_rate=0, *, candidate=False):
        from pathlib import Path
        from bass_bindings import BASS_CHANNELINFO
        dll=ctypes.CDLL(str(os.environ.get('ABMSYNTH_OBSERVATION_DLL',Path(__file__).with_name('abmsynth_dsp.dll'))))
        dll.spatial_rate_version.argtypes=[];dll.spatial_rate_version.restype=ctypes.c_uint
        if dll.spatial_rate_version()!=1:raise RuntimeError(tr('spatial_runtime.abi'))
        for name,args,result in (
            ('create',[ctypes.c_uint]*3+[ctypes.c_void_p]*3,ctypes.c_void_p),
            ('create_ex',[ctypes.c_uint]*4+[ctypes.c_void_p]*3,ctypes.c_void_p),
            ('processing_rate',[ctypes.c_void_p],ctypes.c_uint),('effect',[ctypes.c_void_p],ctypes.c_uint),
            ('destroy',[ctypes.c_void_p],None),('parameters',[ctypes.c_void_p,ctypes.POINTER(_FixedSpatialParameters)],ctypes.c_int),
            ('failures',[ctypes.c_void_p],ctypes.c_uint),('reset_function',[ctypes.c_void_p]*2,None),
            ('configure',[ctypes.c_uint,ctypes.c_void_p,ctypes.POINTER(_FixedSpatialParameters),ctypes.c_uint,
                          ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_int)],None)):
            function=getattr(dll,'spatial_rate_'+name);function.argtypes=args;function.restype=result
        callback=DSPPROC(('spatial_rate_bass_dsp',dll))
        info=BASS_CHANNELINFO();self._required(self.bass.BASS_ChannelGetInfo(self.stream,ctypes.byref(info)))
        if info.chans!=2 or not info.flags&BASS_SAMPLE_FLOAT:raise ValueError(tr('spatial_runtime.format'))
        rate=min(int(requested_rate) or info.freq,info.freq)
        if self.pointer and (not candidate or dll.spatial_rate_processing_rate(self.pointer)==rate):
            return self.pointer
        work=self._required(self.bass.BASS_StreamCreate(rate,2,BASS_SAMPLE_FLOAT|BASS_STREAM_DECODE,None,None))
        pointer=None
        try:
            fx=self._required(self.bass.BASS_ChannelSetFX(work,BASS_FX_DX8_REVERB,30))
            # Warm the Windows effect outside DSPPROC. Parameters and histories
            # are reset under the parent-stream lock before the first attach.
            block=np.zeros((256,2),np.float32)
            self._required(self.bass.BASS_ChannelGetData(work,block.ctypes.data,block.nbytes))
            pointer=dll.spatial_rate_create_ex(info.freq,rate,work,fx,*(ctypes.cast(fn,ctypes.c_void_p)
                for fn in (self.bass.BASS_ChannelGetData,self.bass.BASS_FXSetParameters,self.bass.BASS_StreamFree)))
            self._required(pointer)
            dll.spatial_rate_reset_function(pointer,ctypes.cast(self.bass.BASS_FXReset,ctypes.c_void_p))
        except BaseException:
            if pointer:dll.spatial_rate_destroy(pointer)
            else:self.bass.BASS_StreamFree(work)
            raise
        self.dll,self.native_callback=dll,callback
        if not candidate:self.pointer,self.fx_handle=pointer,fx
        return pointer

    def descriptor(self,enabled,level,parameters=None):
        level=max(0,min(21,int(round(float(level)))))
        advanced=(parameters or SpatialParameters()).validated()
        params=BASS_DX8_REVERB(advanced.input_gain_db,-((21-level)**2)/20.,advanced.time_ms,advanced.high_frequency_ratio)
        if not self._fixed:return bool(enabled),params
        self._prepare()
        # Calibrate the DMO's direct coefficient rather than invent its private
        # equal-power formula. Calibration uses the selected effect rate too.
        from bass_bindings import BASS_CHANNELINFO
        info=BASS_CHANNELINFO();self._required(self.bass.BASS_ChannelGetInfo(self.stream,ctypes.byref(info)))
        rate=min(advanced.sample_rate or info.freq,info.freq)
        key=(rate,bytes(params))
        if enabled and key not in self._dry_cache:
            work=self._required(self.bass.BASS_StreamCreate(rate,2,BASS_SAMPLE_FLOAT|BASS_STREAM_DECODE,None,None))
            try:
                fx=self._required(self.bass.BASS_ChannelSetFX(work,BASS_FX_DX8_REVERB,30))
                self._required(self.bass.BASS_FXSetParameters(fx,ctypes.byref(params)))
                block=np.zeros((1,2),np.float32);block[0,:]=1
                self._required(self.bass.BASS_ChannelGetData(work,block.ctypes.data,block.nbytes))
                if len(self._dry_cache)>=128:self._dry_cache.clear()
                self._dry_cache[key]=float(block[0,0])
            finally:self._required(self.bass.BASS_StreamFree(work))
        return bool(enabled),_FixedSpatialParameters(params,self._dry_cache[key] if enabled else 1)

    def _remove(self):
        steps=((1,self.bass.BASS_ChannelRemoveDSP,self.stream,self.handle),) if self.handle else ()
        result=self._transaction.run(self.stream,steps)
        if result.completed:self.handle=0
        if not result.success:raise RuntimeError(tr('runtime.effect_teardown_failed'))

    def attach(self, stream):
        self.detach()
        self.stream = stream

    def detach(self):
        if self._fixed:
            if getattr(self,'handle',0) or self._transaction.pending is not None:self._remove()
            if self.pointer:self.dll.spatial_rate_destroy(self.pointer);self.pointer=None
            self.fx_handle=0
        elif getattr(self, "stream", 0) and getattr(self, "handle", 0):
            _remove_fx_checked(self.bass,self.check,self.stream,self.handle)
        self.stream = 0
        self.handle = 0

    def set(self, enabled, level, parameters=None):
        if not self.stream:
            return
        if self._fixed:
            if self._transaction.pending is not None:self._remove()
            if not enabled:
                if self.handle:self._remove()
                return
            active,params=self.descriptor(enabled,level,parameters)
            pointer=self._prepare((parameters or SpatialParameters()).validated().sample_rate,candidate=True)
            if pointer!=self.pointer:
                try:
                    if self.handle:self._remove()
                except BaseException:
                    self.dll.spatial_rate_destroy(pointer)
                    raise
                previous=self.pointer;self.pointer=pointer
                self.fx_handle=self.dll.spatial_rate_effect(pointer)
                if previous:self.dll.spatial_rate_destroy(previous)
            result=(ctypes.c_int*4)()
            self.dll.spatial_rate_configure(self.stream,self.pointer,ctypes.byref(params),int(not self.handle),
                ctypes.cast(self.bass.BASS_ChannelLock,ctypes.c_void_p),ctypes.cast(self.bass.BASS_ErrorGetCode,ctypes.c_void_p),result)
            if result[3]==3:self._transaction.pending=(self.stream,threading.get_ident(),False)
            if result[3]:raise RuntimeError(tr('spatial_runtime.failure',code=result[1] or result[2]))
            if not self.handle:self.handle=self._required(self.bass.BASS_ChannelSetDSP(self.stream,self.native_callback,self.pointer,30))
            return
        if not enabled:
            if self.handle:
                _remove_fx_checked(self.bass,self.check,self.stream,self.handle)
            self.handle = 0
            return
        if not self.handle:
            self.handle = self.check(
                self.bass.BASS_ChannelSetFX(self.stream, BASS_FX_DX8_REVERB, 30),
                "Spatial reverb attach",
            )
        if self.handle:
            level = max(0, min(21, int(round(float(level)))))
            advanced = (parameters or SpatialParameters()).validated()
            params = BASS_DX8_REVERB(
                advanced.input_gain_db, -((21 - level) ** 2) / 20.0,
                advanced.time_ms, advanced.high_frequency_ratio)
            self.check(
                self.bass.BASS_FXSetParameters(self.handle, ctypes.byref(params)),
                "Spatial reverb parameters",
            )


class StereoWidth:
    """Own native Mid/Side state and DSP; Python callback is an audit oracle only."""

    def __init__(self, bass, callback, coefficient, check):
        self.bass, self.callback, self.coefficient, self.check = bass, callback, coefficient, check
        self.dll, self.pointer, self.native_callback = None, None, None
        self._transaction=NativeChannelTransaction(bass)
        self.detach()

    def _prepare(self):
        if self.pointer:
            return
        from pathlib import Path
        # audio_fx.cpp additive stereo ABI1; cdecl management exports, bass.h
        # DSPPROC CALLBACK for processing. Preparation never occurs in DSPPROC.
        dll = ctypes.CDLL(str(os.environ.get('ABMSYNTH_OBSERVATION_DLL',Path(__file__).with_name("abmsynth_dsp.dll"))))
        dll.stereo_version.argtypes, dll.stereo_version.restype = [], ctypes.c_uint
        if dll.stereo_version() != 1:
            raise RuntimeError(tr("stereo.native_abi"))
        dll.stereo_create.argtypes, dll.stereo_create.restype = [], ctypes.c_void_p
        dll.stereo_coefficient.argtypes = [ctypes.c_void_p, ctypes.c_float]
        dll.stereo_coefficient.restype = ctypes.c_int
        dll.stereo_destroy.argtypes, dll.stereo_destroy.restype = [ctypes.c_void_p], None
        # Resolve the processing export before allocating owner memory.
        native_callback = DSPPROC(("stereo_bass_dsp", dll))
        pointer = dll.stereo_create()
        if not pointer:
            raise MemoryError(tr("stereo.native_allocation"))
        self.dll, self.pointer = dll, pointer
        # Export address, not a ctypes Python thunk: no GIL in audio processing.
        self.native_callback = native_callback

    def _remove(self):
        steps=((1,self.bass.BASS_ChannelRemoveDSP,self.stream,self.handle),) if self.handle else ()
        result=self._transaction.run(self.stream,steps)
        if result.completed:self.handle=0
        if not result.success:
            raise RuntimeError(tr('stereo.native_removal' if result.phase==2 else 'stereo.native_lock'))

    def attach(self, stream):
        self.detach()
        self.stream = stream

    def detach(self):
        if getattr(self, "handle", 0) or self._transaction.pending is not None:
            self._remove()
        if self.pointer:
            self.dll.stereo_destroy(self.pointer)
            self.pointer = None
        self.stream = 0
        self.handle = 0

    def set(self, multiplier):
        if self._transaction.pending is not None:self._remove()
        multiplier = max(0.0, min(3.0, float(multiplier)))
        # Existing DSP: S' = S*(1+k), so k = displayed multiplier - 1.
        self.coefficient.value = multiplier - 1.0
        if not self.stream:
            return
        if abs(multiplier - 1.0) >= 1e-7 and not self.handle:
            self._prepare()
            if not self.dll.stereo_coefficient(self.pointer, self.coefficient.value):
                raise RuntimeError(tr("stereo.native_coefficient"))
            self.handle = self.check(
                self.bass.BASS_ChannelSetDSP(self.stream, self.native_callback, self.pointer, 2),
                tr("stereo.native_attach"),
            )
        elif abs(multiplier - 1.0) < 1e-7 and self.handle:
            self._remove()
        elif self.pointer:
            if not self.dll.stereo_coefficient(self.pointer, self.coefficient.value):
                raise RuntimeError(tr("stereo.native_coefficient"))


class ChannelDelayEffects:
    """Own allocated channel taps and one UserFX bus, not delay sample state."""

    def __init__(self, bass, bassmidi, callback, check, *, native_runtime=None):
        self.bass, self.bassmidi, self.callback, self.check = bass, bassmidi, callback, check
        self.native_runtime = native_runtime
        self._transaction=NativeChannelTransaction(bass,getattr(native_runtime,'_native_path',None))
        self._native_keys = set()
        self._selection_borrowers=set()
        self.channel_fx_library=None
        self.detach()

    def attach(self, stream):
        self.detach()
        self.stream = stream

    def detach(self):
        if getattr(self,'_selection_borrowers',None):raise RuntimeError('native selection graph borrowers must retire before delay detach')
        locked = self.native_runtime is not None and bool(getattr(self, "stream", 0)) and bool(self._native_keys)
        if locked or self._transaction.pending is not None:
            actions=[]
            for channel,(mix,handle) in enumerate(zip(getattr(self,'mixes',()),getattr(self,'handles',()))):
                if mix:
                    if handle:actions.append(('handles',channel,1,self.bass.BASS_ChannelRemoveDSP,mix,handle))
                    actions.append(('mixes',channel,4,self.bass.BASS_StreamFree,mix,0))
            if getattr(self,'userfx_handle',0):
                actions.append(('userfx_handle',None,1,self.bass.BASS_ChannelRemoveDSP,self.userfx_mix,self.userfx_handle))
            if getattr(self,'userfx_mix',0):
                actions.append(('userfx_mix',None,4,self.bass.BASS_StreamFree,self.userfx_mix,0))
            result=self._transaction.run(self.stream,((kind,function,first,second) for _,_,kind,function,first,second in actions))
            for name,index,*_ in actions[:result.completed]:
                if index is None:setattr(self,name,0)
                else:getattr(self,name)[index]=0
            if not result.success:
                raise RuntimeError(tr('native_delay.remove_failed' if result.phase==2 else 'native_delay.lock_failed'))
        else:
            # Explicit reference/mock path without native tap ownership.
            for channel, (mix, handle) in enumerate(zip(getattr(self, "mixes", ()), getattr(self, "handles", ()))):
                if mix:
                    if handle:
                        result = self.bass.BASS_ChannelRemoveDSP(mix, handle)
                        if locked and not result:
                            raise RuntimeError(tr("native_delay.remove_failed"))
                        self.handles[channel] = 0
                    if not self.check(self.bass.BASS_StreamFree(mix),tr("audio_fx.operation.remove")):
                        raise RuntimeError(tr("runtime.effect_teardown_failed"))
                    self.mixes[channel] = 0
            if getattr(self, "userfx_handle", 0):
                result = self.bass.BASS_ChannelRemoveDSP(self.userfx_mix, self.userfx_handle)
                if locked and not result:
                    raise RuntimeError(tr("native_delay.remove_failed"))
                self.userfx_handle = 0
            if getattr(self, "userfx_mix", 0):
                if not self.check(self.bass.BASS_StreamFree(self.userfx_mix),tr("audio_fx.operation.remove")):
                    raise RuntimeError(tr("runtime.effect_teardown_failed"))
                self.userfx_mix = 0
        if self.native_runtime is not None:
            self.native_runtime.release_native_taps(self._native_keys)
        self._native_keys.clear()
        self.stream = 0
        self.handles = [0] * 128
        self.mixes = [0] * 128
        self.userfx_mix = self.userfx_handle = 0
        self.active_channels = set()

    def _tap(self, channel, wet_only=False):
        if self.native_runtime is not None:
            result = self.native_runtime.prepare_native_tap(channel, wet_only)
            self._native_keys.add((channel, bool(wet_only)))
            return result
        # Explicit Python-reference/fake audit path, never the active app.
        return self.callback, ctypes.c_void_p(channel + (128 if wet_only else 0))

    def install(self, channels=range(16), drums=None):
        if not self.stream:
            return
        channels = tuple(channels)
        self.active_channels.update(channels)
        if self.native_runtime is not None:
            # Include currently-percussion dry routes so a later mode switch
            # reuses prepared memory instead of allocating in a MIDI filter.
            for channel in channels:
                self._tap(channel)
        if not self.userfx_mix:
            self.userfx_mix = self.bassmidi.BASS_MIDI_StreamGetChannel(self.stream, BASS_MIDI_CHAN_USERFX)
            if self.userfx_mix:
                #128 marks a wet-only bus;9 retains the original BMSynth
                # drum delay state without stealing melodic channel10's tap.
                callback, pointer = self._tap(9, True)
                self.userfx_handle = self.check(self.bass.BASS_ChannelSetDSP(
                    self.userfx_mix, callback, pointer, 20), tr("native_delay.userfx_attach"))
        for channel in channels:
            percussion = channel % 16 == 9 if drums is None else drums[channel]
            if percussion:
                continue
            if self.handles[channel]:
                continue
            # The drum channel's dry stream collapses key-specific reverb and
            # chorus sends (bassmidi.md StreamGetChannel). Its UserFX bus tap
            # preserves those sends and returns only the new echo, not dry.
            mix = self.bassmidi.BASS_MIDI_StreamGetChannel(
                self.stream, channel)
            if mix:
                if self.channel_fx_library is not None:
                    self.channel_fx_library.channel_fx_delay_acquire(self.stream,channel,mix)
                self.mixes[channel] = mix
                callback, pointer = self._tap(channel)
                self.handles[channel] = self.check(
                    self.bass.BASS_ChannelSetDSP(mix, callback, pointer, 20),
                    tr("native_delay.channel_attach", channel=channel+1),
                )

    def sync_drums(self, drums, count):
        if not self.stream:
            return
        for channel in range(count):
            if drums[channel] and self.mixes[channel]:
                # A dry channel tap disables per-key sends. Release that tap
                # when a native SysEx changes the channel to percussion.
                # BASSMIDI 2.4.16 retires the route at the next render boundary;
                # same-block notes can retain collapsed sends. The audit records
                # this native limitation; consuming live PCM is not a safe fix.
                self.bass.BASS_ChannelRemoveDSP(self.mixes[channel], self.handles[channel])
                retained=(self.channel_fx_library is not None and
                    self.channel_fx_library.channel_fx_delay_release(self.stream,channel,self.mixes[channel]))
                if not retained:self.bass.BASS_StreamFree(self.mixes[channel])
                self.mixes[channel] = self.handles[channel] = 0
        self.install(sorted(self.active_channels), drums)

    def selection_snapshot(self,library):
        """Control-side graph identity; no MIDI/DSP callback enters Python.

        Existing install prewarms dry taps even for percussion channels. The
        candidate borrows those exact pointers and returns graph changes only
        as metadata. All borrowers must retire before this graph detaches.
        """
        enabled=bool(self.stream)
        if enabled and self.native_runtime is None:raise ValueError('native selection requires native delay graph')
        active=(ctypes.c_uint*128)(*(int(ch in self.active_channels) for ch in range(128)))
        mixes=(ctypes.c_uint*128)(*self.mixes);handles=(ctypes.c_uint*128)(*self.handles);taps=(ctypes.c_void_p*128)()
        wet=None;callback=None
        if enabled:
            self.native_runtime._load_native()
            if self.native_runtime._native_library._handle!=library._handle:raise ValueError('native selection graph DLL identity')
            for ch in sorted(self.active_channels):callback,taps[ch]=self._tap(ch)
            callback,wet=self._tap(9,True)
        return enabled,active,mixes,handles,taps,wet,callback

    def replay_selection_graph(self,kind,channel,mix,handle):
        """Accepted ownership mirror only; never remove/attach audio twice."""
        if kind==14:self.userfx_mix,self.userfx_handle=mix,handle
        else:self.mixes[channel],self.handles[channel]=mix,handle


class StreamEffects:
    """Aggregate independent stream effects under one BASS lifetime."""

    def __init__(self, bass, bassmidi, stereo_callback, stereo_coefficient, delay_callback, failure_sink=None, *, delay_runtime=None):
        self.bass = bass
        self.failure_sink = failure_sink
        self.equalizer = BassEqualizer(bass, self._check)
        self.extended_delay = ExtendedDelay(bass, self._check)
        self.compressor = Compressor(bass, self._check)
        self.master_volume = MasterOutputVolume(bass,self._check)
        self.limiter = TruePeakLimiter(bass, self._check, failure_sink)
        self.spatial = SpatialReverb(bass, self._check)
        self.stereo = StereoWidth(bass, stereo_callback, stereo_coefficient, self._check)
        self.delay = ChannelDelayEffects(bass, bassmidi, delay_callback, self._check, native_runtime=delay_runtime)
        self.rack = AudioFXRack(bass,self._check,
            domain_failure_sink=lambda message:self.failure_sink(message,None) if self.failure_sink is not None else None)
        from channel_audio_effects import ChannelAudioEffects
        self.channel_fx=ChannelAudioEffects(self,bassmidi,rack_factory=AudioFXRack,
            graph_factory=NativeFXTopologyGraph,snapshot_type=_FXTopologySnapshot2)
        self.stream = 0
        self.spatial_enabled = True
        self._ordered_graphs={};self._ordered_submitter=None;self._ordered_observer=None
        self._ordered_lock=threading.RLock()

    def bind_ordered_commands(self,owner,library=None,submitter=None,completion_observer=None):
        """One seven-owner port;startup/export uses the same direct owners."""
        with self._ordered_lock:
            # Do not partially retire the first five owners when the sixth
            # still holds an accepted OR externally prepared native Plan.
            # FIFO admission shares this lock; metadata may only release leases.
            if any(getattr(graph,'_preparing',0) or
                   any(plan.pointer or plan.inflight for plan in graph.plans)
                   or not getattr(graph,'ready_for_swap',lambda:True)()
                   for graph in self._ordered_graphs.values()):return False
            if submitter is not None and (not callable(submitter) or
                    (completion_observer is not None and not callable(completion_observer))):
                raise ValueError('stream effect command publisher')
            graphs={}
            try:
                if submitter is not None:
                    for kind in ('extended_delay','compressor','spatial','stereo','limiter','master_volume'):
                        graphs[kind]=NativeStreamEffectGraph(self,kind,owner,library)
                    graphs['equalizer']=NativeEqualizerGraph(self,owner,library)
            except BaseException:
                # Construction/ABI failure retains the original complete port.
                for graph in graphs.values():assert graph.close()
                raise
            for graph in self._ordered_graphs.values():assert graph.close()
            self._ordered_graphs=graphs;self._ordered_submitter=submitter
            self._ordered_observer=completion_observer if submitter is not None else None
            return True

    def _ordered(self,kind,*values,on_complete=None):
        with self._ordered_lock:
            graph=self._ordered_graphs[kind];plan=graph.prepare(*values)
            def done(receipt):
                if self._ordered_observer is not None:self._ordered_observer(receipt)
                if on_complete is not None:on_complete(receipt)
            try:receipt=self._ordered_submitter(plan,on_complete=done)
            except BaseException:assert plan.close();raise
            if not receipt.accepted or receipt.completed:assert plan.close()
            return receipt

    def _apply_effect(self,kind,*values):
        """Choose original direct or native admission under the swap boundary."""
        with self._ordered_lock:
            if self._ordered_submitter is not None:return self._ordered(kind,*values)
            if kind=='spatial':self.spatial_enabled=bool(values[0])
            target=getattr(self,kind)
            return (target.apply if kind=='equalizer' else target.set)(*values)

    def _check(self, result, operation):
        if not result and self.failure_sink is not None:
            self.failure_sink(operation, int(self.bass.BASS_ErrorGetCode()))
        return result

    def attach(self, stream, sample_rate=96000):
        if not stream:
            raise ValueError("A live MIDI stream is required")
        # BASS may recycle a numeric handle; lifetime is defined by attach.
        self.detach()
        self.stream = int(stream)
        self.equalizer.attach(self.stream, sample_rate)
        for effect in (self.spatial, self.stereo, self.delay, self.extended_delay, self.compressor):
            effect.attach(self.stream)
        self.limiter.attach(self.stream, sample_rate)
        self.rack.attach(self.stream, sample_rate)
        self.channel_fx.attach(self.stream,sample_rate)
        self.master_volume.attach(self.stream)

    def detach(self):
        if self.bind_ordered_commands(None) is False:raise RuntimeError(tr('runtime.effect_teardown_failed'))
        if self.channel_fx.detach() is False:raise RuntimeError(tr('runtime.effect_teardown_failed'))
        for effect in (self.equalizer, self.spatial, self.stereo, self.delay,
                       self.extended_delay, self.compressor, self.limiter, self.rack,self.master_volume):
            if effect.detach() is False:
                raise RuntimeError(tr("runtime.effect_teardown_failed"))
        self.stream = 0

    def set_equalizer(self, state):
        return self._apply_effect('equalizer',state)

    def set_extended_delay(self, state):
        return self._apply_effect('extended_delay',state)

    def set_audio_fx(self, slots):
        return self.rack.set(slots)

    def set_compressor(self, enabled, threshold_db, ratio, attack_ms, release_ms, makeup_db):
        return self._apply_effect('compressor',enabled,threshold_db,ratio,attack_ms,release_ms,makeup_db)

    def set_master_volume(self,value,*,on_complete=None):
        with self._ordered_lock:
            if self._ordered_submitter is not None:
                return self._ordered('master_volume',master_output_value(value),on_complete=on_complete)
            return self.master_volume.set(value)

    def set_limiter(self, enabled, ceiling_db, release_ms):
        return self._apply_effect('limiter',enabled,ceiling_db,release_ms)

    def set_spatial_reverb(self, enabled, level, parameters=None):
        return self._apply_effect('spatial',bool(enabled),level,parameters)

    def set_stereo_multiplier(self, multiplier):
        return self._apply_effect('stereo',multiplier)

    def install_delay_dsps(self):
        self.delay.install()

    @property
    def spatial_reverb(self):
        return self.spatial.fx_handle if self.spatial._fixed and self.spatial.handle else self.spatial.handle

    @property
    def stereo_dsp(self):
        return self.stereo.handle

    @property
    def delay_dsps(self):
        return self.delay.handles


# ABI from current bass.h/bass_fx.h: every scalar below occupies four bytes.
# Native BFX flags always address the two audio channels, not MIDI channels.
_F, _I, _U = ctypes.c_float, ctypes.c_int32, ctypes.c_uint32
_AUDIO_FX_ABI = {
    "flanger": (4, (_F, _F, _F, _F, _U, _F, _U)),
    "phaser": (0x1000b, (_F,) * 6 + (_I,)),
    "auto_wah": (0x10009, (_F,) * 6 + (_I,)),
    "chorus_fx": (0x1000d, (_F,) * 6 + (_I,)),
    "freeverb": (0x10016, (_F,) * 5 + (_U, _I)),
    "distortion": (0x10010, (_F,) * 5 + (_I,)),
    "echo": (0x10014, (_F,) * 4 + (_I, _I)),
    "gargle": (5, (_U, _U)),
    "compressor_fx": (0x10011, (_F,) * 5 + (_I,)),
    "auto_gain": (0x10008, (_F,) * 5 + (_I,)),
    "rotate": (0x10000, (_F, _I)),
    "band_pass": (0x10013, (_I,) + (_F,) * 5 + (_I,)),
    "notch": (0x10013, (_I,) + (_F,) * 5 + (_I,)),
    "all_pass": (0x10013, (_I,) + (_F,) * 5 + (_I,)),
    "pitch_shift": (0x10015, (_F, _F, _I, _I, _I)),
    "chorus_dx8": (0, (_F, _F, _F, _F, _U, _F, _U)),
    "distortion_dx8": (2, (_F,) * 5),
    "echo_dx8": (3, (_F,) * 4 + (_I,)),
    "reverb_dx8": (8, (_F,) * 4),
}
_AUDIO_FX_STRUCTS = {key: type("AudioFX_" + key, (ctypes.Structure,), {
    "_fields_": [("field_" + str(index), scalar) for index, scalar in enumerate(layout)]})
    for key, (_number, layout) in _AUDIO_FX_ABI.items()}
# bass_fx.h: ENV_NODE is explicitly packed to 4; the enclosing pointer-bearing
# VOLUME_ENV has normal x64 alignment. Nodes stay alive until FX removal.
class AudioFXEnvelopeNode(ctypes.Structure):
    _pack_ = 4
    _fields_ = [("pos", ctypes.c_double), ("val", ctypes.c_float)]


class AudioFXEnvelope(ctypes.Structure):
    _fields_ = [("channel", ctypes.c_int), ("count", ctypes.c_int),
               ("nodes", ctypes.POINTER(AudioFXEnvelopeNode)), ("follow", ctypes.c_int)]


_AUDIO_FX_ABI["volume_envelope"] = (0x10012, ())
for _filter_kind in ("parametric_eq", "low_pass", "high_pass", "low_shelf", "high_shelf"):
    _AUDIO_FX_ABI[_filter_kind] = _AUDIO_FX_ABI["band_pass"]
    _AUDIO_FX_STRUCTS[_filter_kind] = _AUDIO_FX_STRUCTS["band_pass"]
_CUSTOM_FX = {kind: index for index, kind in enumerate((
    "tremolo", "auto_pan", "ring_mod", "bitcrusher", "saturator", "gate", "dc_block", "stereo_tools", "headphone_stage", "convolution", "gain", "channel_matrix"))}


def audio_fx_native_parameters(slot, sample_rate):
    v, kind = slot.values, slot.kind
    if kind in ("flanger", "chorus_dx8"):
        data = (*v[:4], int(v[4]), v[5], int(v[6] / 90 + 2))
    elif kind in ("phaser", "auto_wah", "chorus_fx"):
        data = (v[0] / 100, v[1] / 100, v[2] / 100, *v[3:], -1)
    elif kind == "freeverb":
        data = (v[0] / 100, v[1] / 100, *v[2:5], int(v[5]), -1)
    elif kind == "distortion":
        data = (v[0], v[1] / 100, v[2] / 100, v[3] / 100, 10 ** (v[4] / 20), -1)
    elif kind == "echo":
        data = (v[0] / 100, v[1] / 100, v[2] / 100, v[3] / 1000, int(v[4]), -1)
    elif kind == "gargle":
        data = tuple(int(value) for value in v)
    elif kind == "echo_dx8":
        data = (*v[:4], int(v[4]))
    elif kind in ("distortion_dx8", "reverb_dx8"):
        data = v
    elif kind in ("compressor_fx", "auto_gain"):
        data = (*v, -1)
    elif kind == "rotate":
        data = (v[0], -1)
    elif kind in ("band_pass", "notch", "all_pass"):
        # Same80..16k normal range as existing EQ. BQF's documented ceiling
        # is strictly below Nyquist, not DX8 ParamEQ's sample_rate/3 ceiling.
        data = ({"band_pass": 2, "notch": 4, "all_pass": 5}[kind],
                min(v[0], sample_rate * .49), 0, v[1], 0, 0, -1)
    elif kind == "parametric_eq":
        data = (6, min(v[0], sample_rate * .49), v[2], v[1], 0, 0, -1)
    elif kind in ("low_pass", "high_pass"):
        data = (0 if kind == "low_pass" else 1, min(v[0], sample_rate * .49), 0, 0, v[1], 0, -1)
    elif kind in ("low_shelf", "high_shelf"):
        data = (7 if kind == "low_shelf" else 8, min(v[0], sample_rate * .49), v[1], 0, 0, v[2], -1)
    elif kind == "pitch_shift":
        data = (2 ** (v[0] / 12), 0, 1024 << int(v[1]), int(v[2]), -1)
    else:
        raise ValueError("Native audio FX kind")
    structure = _AUDIO_FX_STRUCTS[kind]
    assert ctypes.sizeof(structure) == 4 * len(data)
    return structure(*data)


def audio_fx_neutral(slot):
    v = slot.values
    if slot.enabled and dict(slot.extras).get('midi_cc'):return False
    return (not slot.enabled or
        slot.kind == "parametric_eq" and v[2] == 0 or
        slot.kind in ("low_shelf", "high_shelf") and v[1] == 0 or
        slot.kind == "pitch_shift" and v[0] == 0 or
        slot.kind in ("tremolo", "auto_pan") and v[1] == 0 or
        slot.kind in ("flanger", "ring_mod") and v[0 if slot.kind == "flanger" else 1] == 0 or
        slot.kind in ("chorus_dx8", "echo_dx8") and v[0] == 0 or
        slot.kind == "bitcrusher" and v[2] == 0 or
        slot.kind == "saturator" and v[1] == 0 and v[2] == 0 or
        slot.kind == "stereo_tools" and not any(v) or
        slot.kind == "headphone_stage" and v[4] == 0 and v[5] == 0 or
        slot.kind == "convolution" and v[0] == 0 and v[1] == 0 or
        slot.kind == "gain" and v[0] == 0 and v[1] == 0 or
        slot.kind == "channel_matrix" and v == (100, 0, 0, 100) or
        slot.kind == "volume_envelope" and v[:3] == (100, 100, 100) or
        slot.kind in ("echo", "freeverb", "phaser", "auto_wah", "chorus_fx")
        and v[0] == 100 and v[1] == 0)


def processing_timing(state, sample_rate):
    """Fixed FIFO delay, NOT musical delay/IR predelay or physical latency.

    Sources: native_dsp/audio_fx.cpp Convolver/SaturationQuality/LimiterState;
    unknown foreign buffering is never admitted as zero for export alignment.
    Only source-defined FIFO is compensable. Foreign effects documenting
    musical time/phase/window parameters but no fixed-delay contract are an
    explicit unsupported alignment policy, not an assumed zero-frame delay.
    """
    rate = int(sample_rate)
    nodes = []
    def add(kind, frames, evidence, time_domain="sample_transform"):
        nodes.append(dict(kind=kind, fixed_frames=frames, evidence=evidence,
                          time_domain=time_domain,
                          alignment_disposition="source_fixed_fifo" if frames is not None
                          else "excluded_no_universal_fifo_contract"))
    if state.spatial_level > 0:
        add("spatial", None, "selectable-rate DX8: dry FIFO0, wet alignment6ms plus musical reflections; no whole-path shift", "reverberation")
    if state.compressor.enabled:
        add("compressor", None, "DX8 envelope/predelay does not specify whole-path FIFO", "envelope")
    if not state.equalizer.bypassed and (any(eq_band_active(b) for b in state.equalizer.bands)
                                        or state.equalizer.output_trim_db):
        add("equalizer", None, "DX8/BQF phase response is not a universal sample shift", "frequency_dependent_phase")
    if state.extended_delay.enabled:
        add("extended_delay", None, "BFX dry/wet echo time retained; no whole-path FIFO contract", "musical_delay")
    if state.audio_fx_enabled:
        for slot in state.audio_fx:
            slot = slot.validated()
            if audio_fx_neutral(slot): continue
            if slot.kind in ("headphone_stage", "convolution"):
                add(slot.kind, 256, "native 256-frame block; IR onset retained", "convolution")
            elif slot.kind == "saturator":
                add(slot.kind, 16 if slot.values[3] > 0 else 0, "native quality FIR and dry-path alignment")
            elif slot.kind in _CUSTOM_FX:
                add(slot.kind, 0, "native per-sample path; no FIFO")
            else:
                domain = ("stft_window" if slot.kind == "pitch_shift" else
                          "musical_delay" if slot.kind in ("echo", "echo_dx8", "chorus_fx", "chorus_dx8", "flanger") else
                          "reverberation" if slot.kind in ("freeverb", "reverb_dx8") else
                          "frequency_dependent_phase" if slot.kind in ("band_pass", "notch", "all_pass", "parametric_eq", "low_pass", "high_pass", "low_shelf", "high_shelf", "phaser", "auto_wah", "distortion_dx8") else
                          "envelope" if slot.kind in ("compressor_fx", "auto_gain", "volume_envelope") else
                          "sample_transform")
                add(slot.kind, None, "vendor domain has no universal fixed FIFO contract; creative timing/phase retained", domain)
    if state.limiter.enabled:
        add("limiter", max(1, round(max(11025, rate)*.005))+8, "native lookahead + FIR_RADIUS; impulse verified")
    if getattr(state,'channel_audio_fx','{}')!='{}':
        import json
        from effect_control import audio_fx_from_ini
        for channel,row in json.loads(state.channel_audio_fx).items():
            if row.get('enabled',True) and any(not audio_fx_neutral(slot) for slot in audio_fx_from_ini(json.dumps(row.get('effects',[])))):
                add('channel_audio_fx_'+channel,None,'independent channel effect timing; no common whole-mix shift','channel_dependent')
    unknown = [node['kind'] for node in nodes if node['fixed_frames'] is None]
    return dict(schema=1, sample_rate=rate, nodes=nodes,
                known_fixed_frames=sum(node['fixed_frames'] or 0 for node in nodes),
                total_fixed_frames=None if unknown else sum(node['fixed_frames'] for node in nodes),
                unqualified=unknown, alignment_available=not unknown,
                alignment_contract="source-defined-fixed-FIFO-only",
                excluded_policy="no onset/FFT/decay inferred sample shift; preserve native musical time",
                tail_policy="user-selected duration; musical/reverb/IR decay is not inferred",
                physical_latency_included=False)


class AudioFXStage:
    """One native BASS FX or direct-C DSP callback; GUI owns no handles."""

    def __init__(self, owner, slot, priority, prepared_pointer=None):
        self.owner, self.slot, self.priority = owner, slot, priority
        self.handle, self.pointer, self.callback = 0, None, None
        self.asset_identity = None
        self.previous_asset_identity = None
        self.envelope_nodes = None
        try:
            if slot.kind in _CUSTOM_FX:
                self.pointer = prepared_pointer
                with owner._asset_lock:
                    self.asset_identity = owner._prepared_assets.get(self.pointer)
                if not self.pointer:
                    raise RuntimeError("Audio FX allocation")
                if slot.kind not in ("headphone_stage", "convolution") and not self.update(slot):
                    raise RuntimeError("Audio FX parameters")
                # Native DLL export address, NOT a Python function trampoline.
                self.callback = DSPPROC(("fx_bass_dsp", owner.dll))
                self.handle = owner.bass.BASS_ChannelSetDSP(owner.stream, self.callback, self.pointer, priority)
            else:
                self.handle = owner.bass.BASS_ChannelSetFX(owner.stream, _AUDIO_FX_ABI[slot.kind][0], priority)
                if not self.handle or not self.update(slot):
                    raise RuntimeError("Audio FX parameters")
            if not self.handle:
                raise RuntimeError("Audio FX attach")
        except BaseException:
            owner.check(0, tr("audio_fx.operation.attach"))  # Report BASS error before removal overwrites it.
            # The caller retains prepared-pointer ownership on failed attach;
            # do not destroy a large FIR while its stream lock is held.
            if not self.detach(dispose=False):
                owner._retired_stages.append(self)  # Callback still owns this memory.
            raise

    def update(self, slot):
        if slot.kind in _CUSTOM_FX:
            self.owner.require_quality(slot)
            parameters = slot.values[4:] if slot.kind == "headphone_stage" else slot.values
            values = (ctypes.c_double * len(parameters))(*parameters)
            success = self.owner.dll.fx_parameters(self.pointer, values, len(parameters))
        elif slot.kind == "volume_envelope":
            v = slot.values
            nodes = (AudioFXEnvelopeNode * 4)(
                AudioFXEnvelopeNode(0, v[0] / 100), AudioFXEnvelopeNode(v[3] / 1000, v[1] / 100),
                AudioFXEnvelopeNode((v[3] + v[4]) / 1000, v[1] / 100),
                AudioFXEnvelopeNode((v[3] + v[4] + v[5]) / 1000, v[2] / 100))
            params = AudioFXEnvelope(-1, 4, nodes, False)
            success = self.owner.bass.BASS_FXSetParameters(self.handle, ctypes.byref(params))
            if success: self.envelope_nodes = nodes
        else:
            params = audio_fx_native_parameters(slot, self.owner.sample_rate)
            success = self.owner.bass.BASS_FXSetParameters(self.handle, ctypes.byref(params))
        if success:
            self.slot = slot
        return bool(success)

    def detach(self, dispose=True):
        if self.handle:
            operation = (self.owner.bass.BASS_ChannelRemoveDSP if self.pointer
                         else self.owner.bass.BASS_ChannelRemoveFX)
            if not operation(self.owner.processing_stream, self.handle):
                self.owner.check(0, tr("audio_fx.operation.remove"))
                return False
        self.handle = 0
        self.callback = None
        self.envelope_nodes = None
        if dispose: self.dispose()
        return True

    def dispose(self):
        """Destroy prepared FIR/memory only after the render lock is released."""
        if self.handle:
            return False  # Remove failure is retryable; never free callback data.
        if self.pointer:
            self.owner._destroy_prepared(self.pointer)
            self.pointer = None
        return True


@dataclass(frozen=True)
class AudioFXPreparationFailure:
    message: str


class _FXTopologyDescriptor(ctypes.Structure):
    _fields_=[('exact',ctypes.c_uint64),('reuse',ctypes.c_uint64),('kind',ctypes.c_uint),('bytes',ctypes.c_uint),
              ('handle',ctypes.c_uint),('priority',ctypes.c_int),('parameters',ctypes.c_void_p)]


class _StreamEQDescriptor(ctypes.Structure):
    _fields_=[('exact',ctypes.c_uint64),('kind',ctypes.c_uint),('fx_kind',ctypes.c_uint),('bytes',ctypes.c_uint),
        ('active',ctypes.c_uint),('priority',ctypes.c_int),('handle',ctypes.c_uint),('parameters',ctypes.c_void_p)]


class NativeEqualizerPlan:
    def __init__(self,graph,state):
        self.graph=graph;self.library=graph.library;self._lifetime=threading.RLock();self.inflight=False;self.pointer=None
        self.values=(state.validated(graph.target.sample_rate),)
        descriptors,roots=graph.describe(self.values[0]);self._roots=roots
        self.pointer=self.library.stream_equalizer_plan_create(graph.pointer,graph.identity(self.values[0]),len(self.values[0].bands),int(self.values[0].bypassed),descriptors)
        if not self.pointer:raise MemoryError('EQ33 prepared plan')
        graph.plans.add(self)

    def close(self):
        with self.graph._lifetime,self._lifetime:
            if self.inflight:return False
            if self.pointer:self.library.stream_equalizer_plan_release(self.pointer);self.pointer=None
            self.graph.plans.discard(self);self._roots=();self.graph.collect_catalog();return True


class NativeEqualizerGraph:
    """Original32 indexed EQ bands plus separate trim; no rack substitution."""
    kind='equalizer'
    def __init__(self,effects,owner,library):
        self.effects=effects;self.target=effects.equalizer;self.owner=owner;self.library=library
        self.stream=effects.stream;self.stream_generation=owner.stream_generation;self.pointer=None;self.plans=set()
        self._lifetime=threading.RLock();self.states={};self.rows={};self._state_lookup={};self._row_lookup={}
        self._state_serial=self._row_serial=self.catalog_reclaims=0
        self._catalog_snapshot=getattr(library,'stream_equalizer_catalog_snapshot',None)
        if self._catalog_snapshot is not None:
            version=library.stream_equalizer_catalog_version;version.argtypes=[];version.restype=ctypes.c_uint
            if version()!=1:raise ValueError('EQ33 catalog ABI')
            self._catalog_snapshot.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint]
            self._catalog_snapshot.restype=ctypes.c_uint
        self._catalog_live=getattr(library,'stream_equalizer_catalog_live_snapshot',None)
        if self._catalog_live is not None:
            version=library.stream_equalizer_catalog_live_version;version.argtypes=[];version.restype=ctypes.c_uint
            if version()!=1:raise ValueError('EQ33 live catalog ABI')
            self._catalog_live.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint]
            self._catalog_live.restype=ctypes.c_uint
        if self.stream!=owner.stream_handle or self.target._parameter_pending is not None:raise ValueError('EQ33 generation/pending')
        descriptor=ctypes.POINTER(_StreamEQDescriptor)
        for name,args,result in (
            ('version',[],ctypes.c_uint),('graph_create',[ctypes.c_uint,ctypes.c_uint64,ctypes.c_uint,ctypes.c_uint,descriptor,ctypes.c_uint64,ctypes.c_uint,descriptor]+[ctypes.c_void_p]*5,ctypes.c_void_p),
            ('graph_release',[ctypes.c_void_p],None),('plan_create',[ctypes.c_void_p,ctypes.c_uint64,ctypes.c_uint,ctypes.c_uint,descriptor],ctypes.c_void_p),('plan_release',[ctypes.c_void_p],None)):
            function=getattr(library,'stream_equalizer_'+name);function.argtypes=args;function.restype=result
        if library.stream_equalizer_version()!=1:raise ValueError('EQ33 ABI')
        state=self.target.state.validated(self.target.sample_rate);neutral=EqualizerState().validated(self.target.sample_rate)
        current,roots=self.describe(state,seed=True);defaults,default_roots=self.describe(neutral)
        functions=(effects.bass.BASS_ChannelLock,effects.bass.BASS_ErrorGetCode,effects.bass.BASS_ChannelSetFX,effects.bass.BASS_FXSetParameters,effects.bass.BASS_ChannelRemoveFX)
        if not all(isinstance(function,ctypes._CFuncPtr) for function in functions):raise ValueError('EQ33 native functions')
        self._roots=(*roots,*default_roots,*functions)
        self.pointer=library.stream_equalizer_graph_create(self.stream,self.identity(state),len(state.bands),int(state.bypassed),current,
            self.identity(neutral),len(neutral.bands),defaults,*(ctypes.cast(function,ctypes.c_void_p) for function in functions))
        if not self.pointer:raise MemoryError('EQ33 graph')
        retain=getattr(owner.channel_command_executor,'retain_effect_graph',None)
        if not callable(retain) or not retain(self):self.close();raise ValueError('EQ33 preparation owner')

    def identity(self,state):
        if state not in self._state_lookup:
            self._state_serial+=1
            if self._state_serial>0xffffffffffffffff:raise OverflowError('EQ33 state identity exhausted')
            self.states[self._state_serial]=state;self._state_lookup[state]=self._state_serial
        return self._state_lookup[state]

    def row_identity(self,value):
        if value not in self._row_lookup:
            self._row_serial+=1
            if self._row_serial>0xffffffffffffffff:raise OverflowError('EQ33 row identity exhausted')
            self.rows[self._row_serial]=value;self._row_lookup[value]=self._row_serial
        return self._row_lookup[value]

    def collect_catalog(self):
        """Reclaim only with complete known leases; never recycle IDs."""
        with self._lifetime:
            if not self.pointer or self._catalog_snapshot is None:return False
            snapshot=(ctypes.c_uint64*4)()
            if self._catalog_live is not None:
                live=tuple(plan for plan in self.plans if plan.pointer)
                if self._catalog_live(self.pointer,len(live)+1,snapshot,4)!=4:return False
            else:
                if self.plans or self._catalog_snapshot(self.pointer,snapshot,4)!=4:return False
                live=()
            kept={int(identity) for identity in snapshot[1:] if identity}
            kept.update(self._state_lookup[plan.values[0]] for plan in live)
            kept.add(self._state_lookup[self.target.state])
            if not kept<=self.states.keys():raise ValueError('EQ33 native catalog identity')
            states={identity:self.states[identity] for identity in kept}
            rows={band for state in states.values() for band in state.bands}
            rows.update(('trim',state.output_trim_db) for state in states.values())
            before=len(self.states)+len(self.rows)
            self.states=states;self._state_lookup={value:identity for identity,value in states.items()}
            self.rows={identity:value for identity,value in self.rows.items() if value in rows}
            self._row_lookup={value:identity for identity,value in self.rows.items()}
            self.catalog_reclaims+=before-len(self.states)-len(self.rows)
            return True

    def ready_for_swap(self):
        with self._lifetime:
            if self.plans:return False
            if not self.pointer or self._catalog_snapshot is None:return True
            return self._catalog_snapshot(self.pointer,(ctypes.c_uint64*4)(),4)==4

    def describe(self,state,seed=False):
        roots=[];rows=[];kinds=('bell','high_pass','low_pass','low_shelf','high_shelf')
        for index,band in enumerate(state.bands):
            params=eq_band_parameters(band);roots.append(params)
            rows.append(_StreamEQDescriptor(self.row_identity(band),kinds.index(band.kind)+1,
                BASS_FX_DX8_PARAMEQ if band.kind=='bell' else BASS_FX_BFX_BQF,ctypes.sizeof(params),int(eq_band_active(band)),
                BassEqualizer.priority(index,len(state.bands)),self.target.handles[index] if seed else 0,ctypes.addressof(params)))
        trim=('trim',state.output_trim_db)
        params=BASS_FX_VOLUME_PARAM(10**(state.output_trim_db/20),-1,0,0);roots.append(params)
        rows.append(_StreamEQDescriptor(self.row_identity(trim),0,BASS_FX_VOLUME,ctypes.sizeof(params),int(abs(state.output_trim_db)>=1e-7),7,
            self.target.trim_handle if seed else 0,ctypes.addressof(params)))
        return (_StreamEQDescriptor*len(rows))(*rows),tuple(roots)

    def prepare(self,state):
        with self._lifetime:
            if not self.pointer or self.stream!=self.owner.stream_handle or self.stream_generation!=self.owner.stream_generation:raise ValueError('EQ33 generation')
            self.collect_catalog()
            try:return NativeEqualizerPlan(self,state)
            except BaseException:self.collect_catalog();raise

    def apply_legacy(self,plan):
        result=self.target.apply(plan.values[0]);return ((0,1,0,bool(result),0),)

    def replay(self,plan,report):
        with self._lifetime:return self._replay(plan,report)

    def _replay(self,plan,report):
        if plan.graph is not self or not report.get('equalizer_command'):raise ValueError('EQ33 completion identity')
        if report['phase'] in (5,6):return True
        identity,count,trim,rows=report['equalizer_snapshot']
        if identity not in self.states or not 0<=count<=32:raise ValueError('EQ33 snapshot bounds')
        self.target.state=self.states[identity];self.target.handles=[row[0] for row in rows[:count]]
        kinds=(None,'bell','high_pass','low_pass','low_shelf','high_shelf')
        self.target.handle_kinds=[kinds[row[1]] for row in rows[:count]];self.target.trim_handle=trim
        return True

    def close(self):
        with self._lifetime:
            if any(plan.inflight for plan in self.plans):return False
            for plan in tuple(self.plans):assert plan.close()
            if not self.ready_for_swap():return False
            if self.pointer:self.library.stream_equalizer_graph_release(self.pointer);self.pointer=None
            self._roots=();self.states.clear();self.rows.clear();self._state_lookup.clear();self._row_lookup.clear()
            release=getattr(getattr(self,'_execution_root',None),'release_effect_graph',None)
            if release is not None:assert release(self)
            return True


class _FXTopologyDescriptor2(ctypes.Structure):
    _fields_=_FXTopologyDescriptor._fields_+[('pointer',ctypes.c_void_p),('kernel',ctypes.c_uint64),('transition',ctypes.c_double)]


class _FXTopologySnapshot(ctypes.Structure):
    _fields_=[('exact',ctypes.c_uint64),('handle',ctypes.c_uint),('kind',ctypes.c_uint),('priority',ctypes.c_int),('reserved',ctypes.c_uint)]


class _FXTopologySnapshot2(ctypes.Structure):
    _fields_=[('base',_FXTopologySnapshot),('pointer',ctypes.c_void_p),('kernel',ctypes.c_uint64)]


class NativeStreamEffectPlan:
    """Immutable original global setter snapshot retained until FIFO ACK."""
    def __init__(self,graph,values):
        self.graph=graph;self.library=graph.library;self.values=values;self.pointer=None
        self._lifetime=threading.RLock();self.inflight=False
        graph._preparing+=1
        try:
            active,parameters=graph.describe(values);self._roots=(parameters,)
            self.prepared_pointer=None
            if graph._dsp:
                self.prepared_pointer=graph.prepare_memory(values) if active else None
                self.pointer=self.library.stream_vendor_dsp_plan_create(graph.pointer,int(active),ctypes.byref(parameters),self.prepared_pointer,
                    ctypes.cast(graph.target.free_callback,ctypes.c_void_p) if graph.kind=='limiter' else None)
            else:self.pointer=self.library.stream_vendor_plan_create(graph.pointer,int(active),ctypes.byref(parameters))
            if not self.pointer:raise MemoryError('stream effect plan')
            graph.plans.add(self)
        finally:
            graph._preparing-=1
            if not self.pointer:graph.collect_prepared()

    def close(self):
        with self.graph._lifetime,self._lifetime:
            if self.inflight:return False
            if self.pointer:self.library.stream_vendor_plan_release(self.pointer);self.pointer=None
            self.graph.plans.discard(self);self._roots=();self.graph.collect_prepared();return True


class NativeStreamEffectGraph:
    """Original ECHO4/DX8/stereo/limiter owners; not rack-slot substitutes.

    EQ uses its independent33-handle typed graph. No unsupported setter
    silently bypasses the native FIFO through this candidate interface.
    """
    kinds={'extended_delay':2,'compressor':3,'spatial':4,'stereo':5,'limiter':6,'master_volume':7}
    def __init__(self,effects,kind,owner,library):
        if kind not in self.kinds:raise ValueError('stream effect kind')
        self.effects=effects;self.kind=kind;self.owner=owner;self.library=library
        self.stream=effects.stream;self.stream_generation=owner.stream_generation
        self.pointer=None;self.plans=set();self.prepared=set();self._preparing=0;self.target=getattr(effects,kind)
        self._dsp=kind in ('stereo','limiter') or kind=='spatial' and self.target._fixed
        self._lifetime=threading.RLock()
        self._references=getattr(library,'stream_vendor_graph_references',None)
        if self._references is not None:
            version=library.stream_vendor_borrow_version;version.argtypes=[];version.restype=ctypes.c_uint
            if version()!=1:raise ValueError('stream effect borrow ABI')
            self._references.argtypes=[ctypes.c_void_p];self._references.restype=ctypes.c_uint
        if self.stream!=owner.stream_handle:raise ValueError('stream effect stream')
        for name,args,result in (
            ('version',[],ctypes.c_uint),
            ('graph_create',[ctypes.c_uint]*4+[ctypes.c_void_p]*5,ctypes.c_void_p),
            ('graph_release',[ctypes.c_void_p],None),('plan_create',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p],ctypes.c_void_p),
            ('plan_release',[ctypes.c_void_p],None)):
            function=getattr(library,'stream_vendor_'+name);function.argtypes=args;function.restype=result
        if library.stream_vendor_version()!=1:raise ValueError('stream effect ABI')
        if kind=='master_volume':
            version=getattr(library,'stream_vendor_master_version',None)
            if version is None:raise RuntimeError(tr('runtime.master_output_update_required'))
            version.argtypes=[];version.restype=ctypes.c_uint
            if version()!=1:raise ValueError('final master volume ABI')
        if self._dsp:
            for name,args in (
                ('dsp_graph_create',[ctypes.c_uint]*4+[ctypes.c_void_p]*8),
                ('dsp_plan_create',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p])):
                function=getattr(library,'stream_vendor_'+name);function.argtypes=args;function.restype=ctypes.c_void_p
            library.stream_vendor_dsp_version.argtypes=[];library.stream_vendor_dsp_version.restype=ctypes.c_uint
            if library.stream_vendor_dsp_version()!=1:raise ValueError('stream effect DSP ABI')
            for name,args,result in (
                ('stereo_create',[],ctypes.c_void_p),('stereo_destroy',[ctypes.c_void_p],None),
                ('limiter_create',[ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(ctypes.c_double)],ctypes.c_void_p),
                ('limiter_destroy',[ctypes.c_void_p],None)):
                function=getattr(library,name);function.argtypes=args;function.restype=result
            if kind=='spatial':
                self.target._prepare();seed=self.describe((False,0,SpatialParameters()))[1];native=self.target.dll
                # CDLL wrappers share an image, not their ctypes prototypes.
                library.spatial_rate_destroy.argtypes=[ctypes.c_void_p]
                library.spatial_rate_destroy.restype=None
            elif kind=='stereo':
                self.target._prepare();seed=ctypes.c_float(self.target.coefficient.value);native=self.target.dll
            else:
                if self.target.native_library is None:self.target._prepare_native()
                native=self.target.native_library
                seed=self.describe((bool(self.target.handle),self.target.ceiling_db,self.target.release_ms))[1]
            if native._handle!=library._handle:raise ValueError('stream effect DSP DLL image')
            functions=(effects.bass.BASS_ChannelLock,effects.bass.BASS_ErrorGetCode,effects.bass.BASS_ChannelSetDSP,effects.bass.BASS_ChannelRemoveDSP,
                effects.bass.BASS_ChannelSetSync,effects.bass.BASS_ChannelRemoveSync)
            if not all(isinstance(function,ctypes._CFuncPtr) for function in functions):raise ValueError('stream effect DSP native functions')
            self._roots=(seed,*functions,self.target.native_callback)
            self.pointer=library.stream_vendor_dsp_graph_create(self.stream,self.kinds[kind],self.target.handle,
                self.target.free_sync if kind=='limiter' else 0,self.target.pointer,ctypes.byref(seed),
                *(ctypes.cast(function,ctypes.c_void_p) for function in functions))
            if not self.pointer:raise MemoryError('stream effect DSP graph')
            retain=getattr(owner.channel_command_executor,'retain_effect_graph',None)
            if not callable(retain) or not retain(self):self.close();raise ValueError('stream effect preparation owner')
            return
        if kind=='extended_delay':seed=self.describe((self.target.state,))[1]
        elif kind=='compressor':seed=BASS_DX8_COMPRESSOR(0,10,200,-18,3,0)
        elif kind=='master_volume':seed=self.target.parameters(self.target.value,True)
        else:seed=BASS_DX8_REVERB(0,-22.05,500,.01)
        functions=(effects.bass.BASS_ChannelSetFX,effects.bass.BASS_FXSetParameters,
            effects.bass.BASS_ChannelRemoveFX,effects.bass.BASS_ErrorGetCode)
        if not all(isinstance(function,ctypes._CFuncPtr) for function in functions):raise ValueError('stream effect native functions')
        self._roots=(seed,*functions)
        self.pointer=library.stream_vendor_graph_create(self.stream,self.kinds[kind],self.target.handle,ctypes.sizeof(seed),
            ctypes.byref(seed),*(ctypes.cast(function,ctypes.c_void_p) for function in functions))
        if not self.pointer:raise MemoryError('stream effect graph')
        retain=getattr(owner.channel_command_executor,'retain_effect_graph',None)
        if not callable(retain) or not retain(self):
            self.close();raise ValueError('stream effect preparation owner')

    def describe(self,values):
        if self.kind=='master_volume':return True,self.target.parameters(values[0],self.target.initial_position())
        if self.kind=='spatial':return self.target.descriptor(*values)
        if self.kind=='stereo':
            multiplier=max(0.,min(3.,float(values[0])))
            return abs(multiplier-1)>=1e-7,ctypes.c_float(multiplier-1)
        if self.kind=='limiter':
            enabled,ceiling,release=values;ceiling=max(-12.,min(0.,float(ceiling)));release=max(20,min(500,int(round(float(release)))))
            return bool(enabled),(ctypes.c_double*2)(10**(ceiling/20)*.96,1-math.exp(-self.target.grain/(self.target.sample_rate*release/1000)))
        if self.kind=='extended_delay':
            state=values[0].validated()
            return state.enabled,BASS_BFX_ECHO4(1,state.wet_percent/100,state.feedback_percent/100,state.time_ms/1000,int(state.ping_pong),-1)
        if self.kind=='compressor':
            enabled,threshold,ratio,attack,release,makeup=values
            return bool(enabled),BASS_DX8_COMPRESSOR(max(-60.,min(60.,float(makeup))),max(.01,min(500.,float(attack))),
                max(50.,min(3000.,float(release))),max(-60.,min(0.,float(threshold))),max(1.,min(100.,float(ratio))),0)
        raise ValueError('stream effect kind')

    def prepare(self,*values):
        with self._lifetime:
            if (not self.pointer or self.stream!=self.owner.stream_handle or self.stream_generation!=self.owner.stream_generation):
                raise ValueError('stream effect generation')
            return NativeStreamEffectPlan(self,values)

    def prepare_memory(self,values=None):
        if self.kind=='spatial':
            pointer=self.target._prepare((values[2] or SpatialParameters()).validated().sample_rate,candidate=True)
        elif self.kind=='stereo':pointer=self.library.stereo_create()
        else:
            coefficients=(ctypes.c_double*68)(*np.concatenate(self.target.kernels))
            pointer=self.library.limiter_create(self.target.lookahead,self.target.grain,coefficients)
        if not pointer:raise MemoryError('stream effect prepared memory')
        self.prepared.add(pointer);return pointer

    def collect_prepared(self):
        if self._preparing:return
        # A native Control may retain a Plan after its Python pointer closed.
        # Preserve all prepared buffers until every borrower is known again.
        if self.pointer and self._references is not None and self._references(self.pointer)!=1+sum(bool(plan.pointer) for plan in self.plans):return
        retained={getattr(self.target,'pointer',None)}
        retained.update(plan.prepared_pointer for plan in self.plans if plan.pointer)
        for pointer in tuple(self.prepared):
            if pointer not in retained:
                (self.library.spatial_rate_destroy if self.kind=='spatial' else self.library.stereo_destroy if self.kind=='stereo' else self.library.limiter_destroy)(pointer)
                self.prepared.remove(pointer)

    def apply_legacy(self,plan):
        result=self.target.set(*plan.values)
        return ((0,self.kinds[self.kind],0,result is not False and result!='disabled',0),)

    def replay(self,plan,report):
        with self._lifetime:return self._replay(plan,report)

    def _replay(self,plan,report):
        if plan.graph is not self or not report.get('stream_effect_command'):raise ValueError('stream effect completion identity')
        if report['phase'] in (5,6):return True
        kind,handle,committed,_operations=report['stream_effect_snapshot']
        if kind!=self.kinds[self.kind]:raise ValueError('stream effect snapshot kind')
        self.target.handle=handle
        if self.kind=='master_volume' and committed==1:self.target.value=master_output_value(plan.values[0])
        if self._dsp:
            pointer=report['stream_effect_pointer'] or None;previous=self.target.pointer
            self.target.pointer=pointer
            if self.kind=='spatial':self.target.fx_handle=self.target.dll.spatial_rate_effect(pointer)
            elif self.kind=='stereo':self.target.coefficient.value=max(0.,min(3.,float(plan.values[0])))-1
            elif self.kind=='limiter':
                self.target.free_sync=report['stream_effect_sync']
                self.target.ceiling_db=max(-12.,min(0.,float(plan.values[1])))
                self.target.release_ms=max(20,min(500,int(round(float(plan.values[2])))))
                if committed==1 and (not handle or previous!=pointer):self.target._reset_processing()
            if previous and previous!=pointer:
                if self.kind=='spatial':self.prepared.add(previous)
                else:
                    (self.library.stereo_destroy if self.kind=='stereo' else self.library.limiter_destroy)(previous)
                    self.prepared.discard(previous)
        if self.kind=='extended_delay':
            if committed==1:self.target.state=plan.values[0].validated()
            elif committed==2:self.target.state=ExtendedDelayState()
        elif self.kind=='spatial':self.effects.spatial_enabled=bool(plan.values[0])
        return True

    def close(self):
        with self._lifetime:
            if self._preparing:return False
            if any(plan.inflight for plan in self.plans):return False
            for plan in tuple(self.plans):assert plan.close()
            if not self.ready_for_swap():return False
            self.collect_prepared();self.prepared.clear()
            if self.pointer:self.library.stream_vendor_graph_release(self.pointer);self.pointer=None
            self._roots=()
            release=getattr(getattr(self,'_execution_root',None),'release_effect_graph',None)
            if release is not None:assert release(self)
            return True

    def ready_for_swap(self):
        with self._lifetime:
            return not self._preparing and not self.plans and (not self.pointer or self._references is None or self._references(self.pointer)==1)


class NativeFXTopologyPlan:
    """Immutable copied vendor request; graph/slot roots survive native ACK."""
    def __init__(self,graph,slots):
        self.graph=graph;self.rack=graph.rack;self.library=graph.library;self.slots=slots;self.pointer=None
        self._lifetime=threading.RLock();self.inflight=False
        graph._preparing+=1
        try:
            descriptors,roots=graph.describe(slots)
            self.prepared_pointers=tuple(row.pointer for row in descriptors if graph.version==2 and row.pointer)
            create=getattr(self.library,'fx_topology_plan_create2' if graph.version==2 else 'fx_topology_plan_create')
            self.pointer=create(graph.pointer,descriptors,len(descriptors))
            if not self.pointer:raise ValueError('FX topology preparation')
            if graph.rack.channel_index is not None:
                from channel_audio_effects import prepare_channel_midi
                prepare_channel_midi(graph,self.pointer,slots,audio_fx_native_parameters,_CUSTOM_FX)
            self._roots=roots;graph.plans.add(self)
        except BaseException:
            if self.pointer:self.library.fx_topology_plan_release(self.pointer);self.pointer=None
            raise
        finally:
            graph._preparing-=1
            if not self.pointer:graph.collect_prepared()

    def close(self):
        with self._lifetime:
            if self.inflight:return False
            if self.pointer:self.library.fx_topology_plan_release(self.pointer);self.pointer=None
            self.graph.plans.discard(self);self._roots=();self.graph.collect_prepared()
            return True


class NativeFXTopologyGraph:
    """Explicit candidate vendor graph. No worker owns Qt or INI values.

    Every accepted target is executed in FIFO; symbolic matching resolves handles
    at execution time, so later Add/Reorder doesn't copy stale GUI handles.
    Custom DSP/assets and envelope-node pointer lifetime are separate contracts.
    """
    def __init__(self,rack,library,*,owner=None):
        self.rack=rack;self.library=library;self.pointer=None;self.plans=set();self.identities=[];self.reuse_keys=[]
        self.kernel_keys=[];self.prepared={};self.slot_roots={};self._preparing=0;self.version=2 if hasattr(library,'fx_topology_version2') else 1
        self.stream=rack.stream;self.owner=owner;self.stream_generation=owner.stream_generation if owner is not None else None
        if owner is not None and owner.stream_handle!=self.stream:raise ValueError('FX topology owner stream')
        for name,args,result in (('version',[],ctypes.c_uint),
            ('graph_create',[ctypes.c_uint,ctypes.POINTER(_FXTopologyDescriptor),ctypes.c_uint]+[ctypes.c_void_p]*6,ctypes.c_void_p),
            ('graph_release',[ctypes.c_void_p],None),('plan_create',[ctypes.c_void_p,ctypes.POINTER(_FXTopologyDescriptor),ctypes.c_uint],ctypes.c_void_p),
            ('plan_release',[ctypes.c_void_p],None)):
            fn=getattr(library,'fx_topology_'+name);fn.argtypes=args;fn.restype=result
        if library.fx_topology_version()!=1:raise ValueError('FX topology ABI')
        if self.version==2:
            for name,args in (('graph_create2',[ctypes.c_uint,ctypes.POINTER(_FXTopologyDescriptor2),ctypes.c_uint]+[ctypes.c_void_p]*8),
                ('plan_create2',[ctypes.c_void_p,ctypes.POINTER(_FXTopologyDescriptor2),ctypes.c_uint])):
                fn=getattr(library,'fx_topology_'+name);fn.argtypes=args;fn.restype=ctypes.c_void_p
            library.fx_topology_version2.argtypes=[];library.fx_topology_version2.restype=ctypes.c_uint
            if library.fx_topology_version2()!=2:raise ValueError('FX topology ABI2')
        if rack._retired_stages or rack._parameter_pending or rack._faulted:raise ValueError('FX topology unresolved owner')
        descriptors,roots=self.describe(tuple(stage.slot for stage in rack.stages),seed=rack.stages)
        backend=rack.bass;functions=(backend.BASS_ChannelLock,backend.BASS_ErrorGetCode,backend.BASS_ChannelSetFX,
            backend.BASS_FXSetParameters,backend.BASS_FXSetPriority,backend.BASS_ChannelRemoveFX)
        if self.version==2:functions+=(backend.BASS_ChannelSetDSP,backend.BASS_ChannelRemoveDSP)
        if not all(isinstance(fn,ctypes._CFuncPtr) for fn in functions):raise ValueError('FX topology native functions')
        create=getattr(library,'fx_topology_graph_create2' if self.version==2 else 'fx_topology_graph_create')
        self.pointer=create(self.stream,descriptors,len(descriptors),*(ctypes.cast(fn,ctypes.c_void_p) for fn in functions))
        if not self.pointer:raise ValueError('FX topology graph')
        if rack.channel_index is not None:
            version=library.fx_topology_channel_version;version.argtypes=[];version.restype=ctypes.c_uint
            if version()!=1:raise ValueError('channel audio FX ABI')
            configure=library.fx_topology_channel_configure
            configure.argtypes=[ctypes.c_void_p,ctypes.c_uint]+[ctypes.c_void_p]*3;configure.restype=ctypes.c_int
            callbacks=rack.channel_callbacks
            if not configure(self.pointer,rack.channel_index,*(ctypes.cast(fn,ctypes.c_void_p) for fn in callbacks)):
                library.fx_topology_graph_release(self.pointer);self.pointer=None
                raise ValueError('channel audio FX configuration')
            library.fx_topology_channel_target.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint)]
            library.fx_topology_channel_target.restype=None
            roots=(*roots,*callbacks)
        self._roots=roots

    def exact_id(self,slot):
        for index,known in enumerate(self.identities):
            if known==slot and known.extras==slot.extras:return index+1
        self.identities.append(slot);return len(self.identities)

    def reuse_id(self,slot):
        key=(slot.kind,slot.identity)
        if key not in self.reuse_keys:self.reuse_keys.append(key)
        return self.reuse_keys.index(key)+1

    def describe(self,slots,seed=None):
        from effect_control import validate_audio_fx
        slots=validate_audio_fx(slots);active=tuple(slot for slot in slots if not audio_fx_neutral(slot));roots=[];rows=[]
        if self.version==1 and any(slot.kind in _CUSTOM_FX or slot.kind=='volume_envelope' for slot in active):raise ValueError('FX topology vendor preparation scope')
        for index,slot in enumerate(active):
            pointer=None;kernel=0;transition=0;exact=self.exact_id(slot)
            if slot.kind in _CUSTOM_FX:
                self.rack._load_native()
                if self.rack.dll._handle!=self.library._handle:raise ValueError('FX topology custom DLL image')
                values=slot.values[4:] if slot.kind=='headphone_stage' else slot.values
                parameters=(ctypes.c_double*len(values))(*values)
                pointer=seed[index].pointer if seed is not None else self.rack.prepare_custom(slot)
                if not pointer:raise ValueError('FX topology custom preparation')
                if seed is None:self.prepared[pointer]=slot
                key=(slot.kind,slot.values[:4] if slot.kind=='headphone_stage' else slot.asset if slot.kind=='convolution' else None)
                if key not in self.kernel_keys:self.kernel_keys.append(key)
                kernel=self.kernel_keys.index(key)+1
                transition=slot.values[-1] if slot.kind in ('headphone_stage','convolution') else 0
                kind=0x80000000|_CUSTOM_FX[slot.kind]
            elif slot.kind=='volume_envelope':
                v=slot.values
                nodes=self.slot_roots.get(exact)
                if nodes is None:
                    nodes=seed[index].envelope_nodes if seed is not None else None
                if nodes is None:
                    nodes=(AudioFXEnvelopeNode*4)(AudioFXEnvelopeNode(0,v[0]/100),AudioFXEnvelopeNode(v[3]/1000,v[1]/100),
                        AudioFXEnvelopeNode((v[3]+v[4])/1000,v[1]/100),AudioFXEnvelopeNode((v[3]+v[4]+v[5])/1000,v[2]/100))
                parameters=AudioFXEnvelope(-1,4,nodes,False);roots.append(nodes);self.slot_roots[exact]=nodes;kind=_AUDIO_FX_ABI[slot.kind][0]
            else:parameters=audio_fx_native_parameters(slot,self.rack.sample_rate);kind=_AUDIO_FX_ABI[slot.kind][0]
            roots.append(parameters)
            values=(exact,self.reuse_id(slot),kind,ctypes.sizeof(parameters),seed[index].handle if seed is not None else 0,
                seed[index].priority if seed is not None else -1-index,ctypes.addressof(parameters))
            rows.append((_FXTopologyDescriptor2 if self.version==2 else _FXTopologyDescriptor)(*values,*((pointer,kernel,transition) if self.version==2 else ())))
        descriptor=_FXTopologyDescriptor2 if self.version==2 else _FXTopologyDescriptor
        return (descriptor*len(rows))(*rows),tuple(roots)

    def collect_prepared(self):
        if self._preparing:return
        retained={stage.pointer for stage in (*self.rack.stages,*self.rack._retired_stages) if stage.pointer and stage.handle}
        for plan in self.plans:
            if plan.pointer:retained.update(plan.prepared_pointers)
        for pointer in tuple(self.prepared):
            if pointer not in retained:self.rack._destroy_prepared(pointer);self.prepared.pop(pointer)

    def prepare(self,slots):
        if not self.pointer or self.rack.stream!=self.stream or (self.owner is not None and
            (self.owner.stream_handle!=self.stream or self.owner.stream_generation!=self.stream_generation)):raise ValueError('FX topology stream generation')
        return NativeFXTopologyPlan(self,tuple(slots))

    def replay(self,plan,report):
        if plan.graph is not self or not report.get('topology_command'):raise ValueError('FX topology completion identity')
        if report['phase'] in (1,5,6):return True
        old={stage.handle:stage for stage in (*self.rack.stages,*self.rack._retired_stages) if stage.handle}
        def stage(row):
            exact,handle,kind,priority,_reserved=row[:5]
            if not 0<exact<=len(self.identities):raise ValueError('FX topology slot snapshot')
            slot=self.identities[exact-1]
            expected=(0x80000000|_CUSTOM_FX[slot.kind]) if slot.kind in _CUSTOM_FX else _AUDIO_FX_ABI[slot.kind][0]
            if expected!=kind:raise ValueError('FX topology native kind snapshot')
            value=old.get(handle)
            if value is None:
                value=AudioFXStage.__new__(AudioFXStage);value.owner=self.rack;value.pointer=None;value.callback=None
                value.asset_identity=value.previous_asset_identity=value.envelope_nodes=None
                value.slot=slot
            if len(row)>5:
                pointer,kernel=row[5:]
                if pointer:
                    if pointer not in self.prepared and value.pointer!=pointer:raise ValueError('FX topology prepared pointer snapshot')
                    value.pointer=pointer;value.callback=DSPPROC(('fx_bass_dsp',self.rack.dll))
                    identity=value.asset_identity
                    if identity is None:
                        with self.rack._asset_lock:identity=self.rack._prepared_assets.get(pointer)
                    if self.rack._kernel_change(slot,value):
                        value.previous_asset_identity=value.asset_identity
                        # Kernel replacement keeps the active Effect pointer;
                        # its prepared source carries the newly decoded identity.
                        source=next((p for p,s in self.prepared.items() if s==slot),None)
                        with self.rack._asset_lock:identity=self.rack._prepared_assets.get(source,identity)
                        with self.rack._asset_lock:
                            if identity is None:self.rack._prepared_assets.pop(pointer,None)
                            else:self.rack._prepared_assets[pointer]=identity
                    value.asset_identity=identity
            if slot.kind=='volume_envelope':value.envelope_nodes=self.slot_roots[exact]
            value.slot=slot;value.handle=handle;value.priority=priority;return value
        self.rack.stages=[stage(row) for row in report['topology_current']]
        self.rack._retired_stages=[stage(row) for row in report['topology_retired']]
        self.rack._faulted=report['topology_faulted']
        if report['phase']==0:self.rack.state=plan.slots
        live={stage.handle for stage in (*self.rack.stages,*self.rack._retired_stages)}
        for handle,value in old.items():
            if handle not in live:
                value.handle=0
                if value.pointer:self.prepared.pop(value.pointer,None)
                value.dispose()
        return True

    def close(self):
        self.rack.cancel_topology_preparation()
        if self._preparing:return False
        if any(getattr(plan,'inflight',False) for plan in self.plans):return False
        for plan in tuple(self.plans):assert plan.close()
        self.collect_prepared()
        if self.pointer:
            if self.rack.channel_index is not None:
                target=(ctypes.c_uint*2)();self.library.fx_topology_channel_target(self.pointer,target)
                self.rack._channel_target,self.rack._owns_channel_target=int(target[0]),bool(target[1])
            self.library.fx_topology_graph_release(self.pointer);self.pointer=None
        # Active callbacks now belong solely to retained AudioFXStage objects;
        # detached preparation roots have already been retired above.
        self.prepared.clear();self.slot_roots.clear();self._roots=()
        release=getattr(getattr(self,'_execution_root',None),'release_effect_graph',None)
        if release is not None:assert release(self)
        if getattr(self.rack,'_topology_graph',None) is self:
            self.rack._topology_graph=None;self.rack._topology_submitter=None
        return True


class AudioFXRack:
    """Ordered post-width/pre-limiter slots, empty/bypassed path bit-neutral."""

    def __init__(self, bass, check, *, native_dll=None, domain_failure_sink=None):
        self.native_path = native_dll
        self.bass, self.check = bass, check
        self.domain_failure_sink=domain_failure_sink
        self.stream, self.sample_rate, self.dll = 0, 96000, None
        self.channel_index=None;self.channel_callbacks=()
        self._channel_target=0;self._owns_channel_target=False
        self.on_topology_result=None
        self.state, self.stages = (), []
        self._retired_stages = []
        self._faulted = False
        self._parameter_transaction=NativeChannelTransaction(bass,native_dll)
        self._parameter_pending=None
        self._dll_lock = threading.Lock()
        self._asset_lock = threading.Lock()
        self._prepared_assets = {}
        self._prepare_executor = self._prepare_future = None
        self._prepare_job = self._prepare_latest = None
        self._prepare_ready = None
        self._prepare_cancel = None
        self._prepare_serial = 0
        self._topology_graph=None;self._topology_submitter=None;self._topology_lock=threading.RLock();self._topology_results={}
        self._topology_prepare_executor=self._topology_prepare_future=None
        self._topology_prepare_job=self._topology_prepare_latest=self._topology_prepare_cancel=None

    def bind_topology_commands(self,owner,library,submitter=None):
        """Explicit candidate UI publisher; existing product path stays direct.

        Vendor scope is checked before publication. Unsupported custom pointer
        plans are refused, not silently mixed with a synchronous ordering bypass.
        """
        if self._topology_graph is not None:
            if not self._topology_graph.close():return False
        if submitter is None:
            # Explicit unbind must clear an old executor callable even after
            # its graph has already retired.
            self._topology_graph=None;self._topology_submitter=None
            return True
        if not callable(submitter):raise ValueError('FX topology command publisher')
        self.cancel_preparation()
        graph=NativeFXTopologyGraph(self,library,owner=owner)
        executor=getattr(owner,'channel_command_executor',None)
        retain=getattr(executor,'retain_effect_graph',None)
        try:
            if not callable(retain) or not retain(graph):raise ValueError('FX topology preparation owner')
        except BaseException:
            assert graph.close()
            raise
        self._topology_graph=graph;self._topology_submitter=submitter
        return True

    def _request_topology(self,slots):
        token=self._prepare_serial+1;self._prepare_serial=token
        with self._topology_lock:self._topology_results[token]=None
        heavy=any(slot.kind in ('headphone_stage','convolution') and not audio_fx_neutral(slot) for slot in slots)
        if heavy or self._topology_prepare_future is not None:
            if self._topology_prepare_cancel is not None:self._topology_prepare_cancel.set()
            if self._topology_prepare_latest is not None:
                with self._topology_lock:self._topology_results[self._topology_prepare_latest[0]]=False
            self._topology_prepare_latest=(token,slots,int(self.bass.BASS_GetDevice()))
            self._start_topology_preparation()
            return token,None
        return self._publish_topology_plan(self._topology_graph.prepare(slots),token)

    def _publish_topology_plan(self,plan,token):
        def completed(receipt):
            success=receipt.get('successful',False) if isinstance(receipt,dict) else receipt.successful
            with self._topology_lock:
                self._topology_results[token]=bool(success)
            if self.on_topology_result is not None:self.on_topology_result(token,bool(success))
            # NativeExec mirrors graph once before this callback. This is only
            # prepared root retirement; no widgets, saving or BASS operations.
        try:receipt=self._topology_submitter(plan,on_complete=completed)
        except BaseException:
            plan.close()
            with self._topology_lock:self._topology_results.pop(token,None)
            raise
        get=lambda key,default=None:receipt.get(key,default) if isinstance(receipt,dict) else getattr(receipt,key,default)
        if not get('accepted',False):
            plan.close()
            with self._topology_lock:self._topology_results.pop(token,None)
            return token,False
        self._prepare_serial=token
        with self._topology_lock:
            for old in tuple(self._topology_results):
                if old!=token and self._topology_results[old] is not None:self._topology_results.pop(old)
        if get('completed',False):completed(receipt);plan.close();return token,get('successful',False)
        return token,None

    def _start_topology_preparation(self):
        if self._topology_prepare_future is not None or self._topology_prepare_latest is None:return
        job,self._topology_prepare_latest=self._topology_prepare_latest,None
        if self._topology_prepare_executor is None:
            self._topology_prepare_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='ABMSynth-FX-topology-prepare')
        self._topology_prepare_job=job;self._topology_prepare_cancel=threading.Event()
        self._topology_prepare_future=self._topology_prepare_executor.submit(self._prepare_topology,job,self._topology_prepare_cancel)

    def _prepare_topology(self,job,cancel):
        """Detached IR/HRTF work; publication remains the ordered native actor."""
        try:
            if cancel.is_set():return None
            if not self.bass.BASS_SetDevice(job[2]):raise RuntimeError('FX topology preparation device')
            plan=self._topology_graph.prepare(job[1])
            if cancel.is_set():assert plan.close();return None
            return plan
        except (OSError,RuntimeError,AttributeError,ValueError,MemoryError,ctypes.ArgumentError) as error:
            return AudioFXPreparationFailure(str(error))

    def _poll_topology_preparation(self):
        future=self._topology_prepare_future
        if future is not None and future.done():
            token=self._topology_prepare_job[0]
            # A kernel transition already renders asynchronously. Keep the new
            # detached request outside the MIDI actor until it can be published;
            # never hold ChannelLock or stall later MIDI for a fade to finish.
            if token==self._prepare_serial:
                if any(plan.inflight for plan in self._topology_graph.plans):return
                if self._transition_busy(self._topology_prepare_job[1]):return
            self._topology_prepare_future=self._topology_prepare_job=self._topology_prepare_cancel=None
            plan=future.result()
            if token!=self._prepare_serial:
                if isinstance(plan,NativeFXTopologyPlan):assert plan.close()
                with self._topology_lock:self._topology_results[token]=False
            elif isinstance(plan,NativeFXTopologyPlan):self._publish_topology_plan(plan,token)
            else:
                with self._topology_lock:self._topology_results[token]=False
                if isinstance(plan,AudioFXPreparationFailure):self._preparation_failed(plan)
        self._start_topology_preparation()

    def cancel_topology_preparation(self):
        """Join detached allocations before graph/DLL/device retirement only."""
        if self._topology_prepare_cancel is not None:self._topology_prepare_cancel.set()
        jobs=(self._topology_prepare_job,self._topology_prepare_latest)
        self._topology_prepare_latest=None
        if self._topology_prepare_executor is not None:
            self._topology_prepare_executor.shutdown(wait=True,cancel_futures=False)
            if self._topology_prepare_future is not None:
                plan=self._topology_prepare_future.result()
                if isinstance(plan,NativeFXTopologyPlan):assert plan.close()
        with self._topology_lock:
            for job in jobs:
                if job is not None:self._topology_results[job[0]]=False
        self._topology_prepare_executor=self._topology_prepare_future=None
        self._topology_prepare_job=self._topology_prepare_cancel=None

    def completed_topology_request(self,token):
        """Pure saved-snapshot query; never prepare, poll or execute BASS."""
        with self._topology_lock:return self._topology_results.get(token)

    def attach(self, stream, sample_rate):
        if self.detach() is False:
            raise RuntimeError(tr("runtime.effect_teardown_failed"))
        self.stream, self.sample_rate = int(stream), sample_rate

    @property
    def processing_stream(self):
        if self.channel_index is None:return self.stream
        graph=self._topology_graph
        if graph is not None and graph.pointer:
            target=(ctypes.c_uint*2)();graph.library.fx_topology_channel_target(graph.pointer,target)
            return int(target[0])
        return self._channel_target

    def detach(self):
        # Decoders and detached FIR pointers still belong to this BASS device.
        # Join before the owner can call StreamFree/BASS_Free or change rate.
        if self._topology_graph is not None and not self._topology_graph.close():return False
        self.cancel_preparation()
        # A failed native unlock belongs to the original calling thread. Do
        # not recursively take a new lock or free any handles before retry.
        if self._parameter_transaction.pending is not None:
            if not self._parameter_transaction.run(self.stream,()).success:return False
            self._parameter_pending=None
        retired = list(dict.fromkeys((*self.stages,*self._retired_stages)))
        removed = []
        if self.stream and retired:
            if not self.check(self.bass.BASS_ChannelLock(self.stream, True), tr("audio_fx.operation.lock")):
                return False
            try:
                for stage in retired:
                    if stage.detach(dispose=False): removed.append(stage)
            finally:
                self.bass.BASS_ChannelLock(self.stream, False)
        else:
            removed=retired
        for stage in removed: stage.dispose()
        if len(removed)!=len(retired):
            self._faulted=True
            self._retired_stages=[stage for stage in retired if stage not in removed]
            self.stages=[stage for stage in self.stages if stage not in removed]
            return False
        if self._owns_channel_target and self._channel_target:
            if not self.check(self.channel_callbacks[1](self._channel_target),tr('audio_fx.operation.remove')):return False
        self._channel_target=0;self._owns_channel_target=False
        self._retired_stages=[]; self._faulted=False
        self.stream, self.state, self.stages = 0, (), []
        return True

    def _load_native(self):
        with self._dll_lock:
            self._load_native_locked()

    def _load_native_locked(self):
        from pathlib import Path
        if self.dll is None:
            dll = ctypes.CDLL(str(self.native_path or os.environ.get('ABMSYNTH_OBSERVATION_DLL',Path(__file__).with_name("abmsynth_dsp.dll"))))
            dll.fx_version.argtypes, dll.fx_version.restype = [], ctypes.c_uint
            if dll.fx_version() != 1: raise RuntimeError("Audio FX ABI version")
            dll.fx_create.argtypes, dll.fx_create.restype = [ctypes.c_int, ctypes.c_double], ctypes.c_void_p
            dll.fx_parameters.argtypes, dll.fx_parameters.restype = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_double), ctypes.c_int], ctypes.c_int
            dll.fx_destroy.argtypes, dll.fx_destroy.restype = [ctypes.c_void_p], None
            dll.fx_create_hrtf.argtypes, dll.fx_create_hrtf.restype = [ctypes.c_double, ctypes.POINTER(ctypes.c_double)], ctypes.c_void_p
            dll.fx_create_convolution.argtypes = [ctypes.c_double, ctypes.POINTER(ctypes.c_float), ctypes.c_uint,
                ctypes.c_uint, ctypes.c_double, ctypes.c_double, ctypes.c_double]
            dll.fx_create_convolution.restype = ctypes.c_void_p
            self.dll = dll

    def prepare_custom(self, slot, sample_rate=None):
        self._load_native()
        sample_rate = self.sample_rate if sample_rate is None else sample_rate
        self.require_quality(slot)
        if slot.kind == "headphone_stage":
            values = (ctypes.c_double * len(slot.values))(*slot.values)
            return self.dll.fx_create_hrtf(sample_rate, values)
        if slot.kind == "convolution":
            from soundfont_files import asset_file_identity
            from bass_bindings import BASS_CHANNELINFO, BASS_SAMPLE_FLOAT, BASS_STREAM_DECODE, BASS_UNICODE
            identity = asset_file_identity(slot.asset)
            decode = self.bass.BASS_StreamCreateFile(0, slot.asset, 0, 0,
                                                   BASS_SAMPLE_FLOAT | BASS_STREAM_DECODE | BASS_UNICODE)
            if not decode: raise RuntimeError("Convolution IR open")
            try:
                info = BASS_CHANNELINFO()
                if not self.bass.BASS_ChannelGetInfo(decode, ctypes.byref(info)): raise RuntimeError("Convolution IR format")
                length = self.bass.BASS_ChannelGetLength(decode, 0)
                # Additive native ABI reports OUR preparation bound, not BASS's
                # supported rate. Older DLLs retain their192k IR-source contract.
                maximum_rate = 192000
                if hasattr(self.dll,'fx_max_rate'):
                    self.dll.fx_max_rate.argtypes,self.dll.fx_max_rate.restype=[],ctypes.c_uint
                    maximum_rate = self.dll.fx_max_rate()
                if info.chans not in (1, 2, 4) or not 8000 <= info.freq <= maximum_rate or not 0 < length <= info.freq * info.chans * 4 * 2:
                    raise RuntimeError("Convolution IR domain")
                data = (ctypes.c_float * (length // 4))()
                received = self.bass.BASS_ChannelGetData(decode, data, length)
                if received != length: raise RuntimeError("Convolution IR read")
                if asset_file_identity(slot.asset) != identity:
                    raise RuntimeError("Convolution IR changed during preparation")
                pointer = self.dll.fx_create_convolution(sample_rate, data, length // (info.chans * 4),
                    info.chans, info.freq, slot.values[0], slot.values[1])
                if pointer:
                    with self._asset_lock: self._prepared_assets[pointer] = identity
                return pointer
            finally:
                self.bass.BASS_StreamFree(decode)
        return self.dll.fx_create(_CUSTOM_FX[slot.kind], sample_rate)

    def require_quality(self,slot):
        if ((slot.kind=="saturator" and slot.values[3]>0) or (slot.kind=="gain" and slot.values[1]>0)):
            try:
                version=self.dll.fx_quality_version
                version.argtypes,version.restype=[],ctypes.c_uint
                if version()!=1: raise AttributeError("quality ABI")
            except AttributeError as exc:
                raise RuntimeError(tr("audio_fx.quality_unavailable")) from exc
        transition = (slot.values[4] if slot.kind=='saturator' else
                      slot.values[-1] if slot.kind in ('headphone_stage','convolution') else 0)
        if transition>0:
            try:
                if slot.kind=='saturator':
                    version=self.dll.fx_quality_transition_version
                else:
                    version=self.dll.fx_kernel_transition_version
                    for name in ('fx_kernel_transition','fx_kernel_transition_undo','fx_kernel_transition_commit'):
                        function=getattr(self.dll,name)
                        function.argtypes=[ctypes.c_void_p,ctypes.c_void_p]+([ctypes.c_double] if name=='fx_kernel_transition' else [])
                        function.restype=ctypes.c_int
                    remaining=self.dll.fx_kernel_transition_remaining
                    remaining.argtypes=[ctypes.c_void_p]; remaining.restype=ctypes.c_uint
                version.argtypes=[]; version.restype=ctypes.c_uint
                if version()!=1: raise AttributeError('transition ABI')
            except AttributeError as exc:
                raise RuntimeError(tr('audio_fx.quality_unavailable')) from exc

    @staticmethod
    def _kernel_change(slot,stage,*,reload_assets=False):
        return bool(stage is not None and (slot.kind=='headphone_stage' and
            stage.slot.values[:4]!=slot.values[:4] or slot.kind=='convolution' and
            (stage.slot.asset!=slot.asset or reload_assets)))

    def _missing_kernels(self,active,plan,*,reload_assets=False):
        return tuple(stage is None or self._kernel_change(slot,stage,reload_assets=reload_assets)
                     for slot,stage in zip(active,plan))

    def _transition_busy(self,slots):
        active,plan,_=self._plan(slots)
        return any(self._kernel_change(slot,stage) and
            self.dll.fx_kernel_transition_remaining(stage.pointer)>0
            for slot,stage in zip(active,plan))

    def asset_snapshot(self):
        """Committed decoded IR identity, not the current file at the same path.

        Copies only small metadata. No file/BASS read or hashing in observation.
        """
        identities=[]
        for stage in self.stages:
            if stage.asset_identity is not None: identities.append(dict(stage.asset_identity))
            if stage.previous_asset_identity is not None:
                if self.dll.fx_kernel_transition_remaining(stage.pointer):
                    identities.append(dict(stage.previous_asset_identity))
                else: stage.previous_asset_identity=None
        return tuple(identities)

    def _plan(self, slots, reload_assets=False):
        active = tuple(slot for slot in slots if not audio_fx_neutral(slot))
        # Match exact slots first (including duplicates), then compatible
        # parameter edits. Order/add/remove must not reset unrelated tails,
        # oscillator phase or an already running volume envelope.
        available = [stage for stage in self.stages if stage.handle]
        detached = [stage for stage in self.stages if not stage.handle]
        reusable = list(available)
        plan = [None] * len(active)
        for index, slot in enumerate(active):
            stage = next((stage for stage in available if stage.slot == slot and
                          not (reload_assets and slot.kind=='convolution')), None)
            if stage is not None:
                plan[index] = stage; available.remove(stage)
        for index, slot in enumerate(active):
            if plan[index] is not None: continue
            stage = next((stage for stage in available if stage.slot.kind == slot.kind and
                stage.slot.identity == slot.identity and
                (slot.kind != "headphone_stage" or slot.values[-1]>0 or stage.slot.values[:4] == slot.values[:4]) and
                (slot.kind != "convolution" or slot.values[-1]>0 or
                 stage.slot.asset == slot.asset and not reload_assets)), None)
            if stage is not None:
                plan[index] = stage; available.remove(stage)
        if reload_assets:
            available += [stage for stage in self.stages if stage.handle and stage not in reusable]
        return active, plan, available+detached

    def _dispose_prepared(self, prepared):
        if isinstance(prepared,AudioFXPreparationFailure): return
        for pointer in prepared or ():
            if pointer: self._destroy_prepared(pointer)

    def _destroy_prepared(self, pointer):
        if any(stage.pointer==pointer and stage.handle for stage in (*self.stages,*self._retired_stages)):
            return False  # A failed removal still has a native borrower.
        with self._asset_lock: self._prepared_assets.pop(pointer, None)
        self.dll.fx_destroy(pointer)

    def _prepare(self, active, missing, sample_rate, device=None, cancel=None):
        """Worker owns detached allocations only; never logs or calls widgets."""
        prepared = [None] * len(active)
        try:
            # bass.h/current bass.md: StreamCreateFile uses thread-local device.
            if device is not None and not self.bass.BASS_SetDevice(device):
                raise RuntimeError("Audio FX preparation device")
            for index, slot in enumerate(active):
                if cancel is not None and cancel.is_set():
                    self._dispose_prepared(prepared)
                    return None
                if missing[index] and slot.kind in _CUSTOM_FX:
                    prepared[index] = self.prepare_custom(slot, sample_rate)
                    if not prepared[index]: raise RuntimeError("Audio FX preparation")
            if cancel is not None and cancel.is_set():
                self._dispose_prepared(prepared)
                return None
        except (OSError, RuntimeError, AttributeError, ValueError, MemoryError, ctypes.ArgumentError) as error:
            self._dispose_prepared(prepared)
            return AudioFXPreparationFailure(str(error))
        return prepared

    def needs_preparation(self, slots):
        """Live admission only. Offline/startup callers still use set()."""
        if self._topology_submitter is not None:return True
        if self._prepare_future is not None or self._prepare_ready is not None: return True
        if not self.stream: return False
        active, plan, _ = self._plan(slots)
        return any(missing and slot.kind in ("headphone_stage", "convolution")
                   for slot, missing in zip(active,self._missing_kernels(active,plan)))

    def request(self, slots):
        """Control-thread API: (token, applied bool or None while preparing).

        At most one running job and one latest description. No Future backlog,
        no widget callbacks. A finished stale job is disposed, never attached.
        """
        from effect_control import validate_audio_fx
        slots = validate_audio_fx(slots)
        if self._topology_submitter is not None:return self._request_topology(slots)
        self._prepare_serial += 1
        if self._prepare_cancel is not None: self._prepare_cancel.set()
        token = self._prepare_serial
        self._prepare_latest = None
        if self._prepare_ready is not None:
            self._dispose_prepared(self._prepare_ready[1]); self._prepare_ready=None
        active, plan, _ = self._plan(slots)
        heavy = self.stream and any(missing and slot.kind in
                    ("headphone_stage", "convolution") for slot, missing in zip(active,self._missing_kernels(active,plan)))
        if not heavy:
            return token, self._set(slots)
        self._prepare_latest = (token, slots, self.sample_rate, int(self.bass.BASS_GetDevice()))
        self._start_latest()
        return token, None

    def _start_latest(self):
        if self._prepare_future is not None or self._prepare_latest is None: return
        job, self._prepare_latest = self._prepare_latest, None
        token, slots, rate, device = job
        active, plan, _ = self._plan(slots)
        if self._prepare_executor is None:
            self._prepare_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ABMSynth-FX-prepare")
        self._prepare_job = job
        self._prepare_cancel = threading.Event()
        self._prepare_future = self._prepare_executor.submit(
            self._prepare, active, self._missing_kernels(active,plan), rate, device, self._prepare_cancel)

    def poll(self, token):
        """Commit a ready latest result on the control thread, without waiting."""
        if self._topology_submitter is not None:
            if token!=self._prepare_serial:return False
            self._poll_topology_preparation()
            with self._topology_lock:
                if token!=self._prepare_serial:return False
                return self._topology_results.get(token,False)
        # An obsolete caller must not commit/acknowledge another caller's job.
        if token != self._prepare_serial: return False
        if self._prepare_future is not None and self._prepare_future.done():
            job, future = self._prepare_job, self._prepare_future
            self._prepare_job = self._prepare_future = None
            self._prepare_cancel = None
            prepared = future.result()
            if job[0] != self._prepare_serial:
                self._dispose_prepared(prepared)
            elif prepared is None or isinstance(prepared,AudioFXPreparationFailure):
                if isinstance(prepared,AudioFXPreparationFailure): self._preparation_failed(prepared)
                return False
            else:
                self._prepare_ready=(job,prepared)
        if self._prepare_ready is not None:
            job,prepared=self._prepare_ready
            if self._transition_busy(job[1]): return None
            self._prepare_ready=None
            return self._set(job[1],prepared)
        self._start_latest()
        if token != self._prepare_serial: return False
        return None if self._prepare_future is not None else True

    def cancel_preparation(self):
        self._prepare_serial += 1
        if self._prepare_cancel is not None: self._prepare_cancel.set()
        self._prepare_latest = None
        if self._prepare_ready is not None:
            self._dispose_prepared(self._prepare_ready[1]); self._prepare_ready=None
        if self._prepare_executor is not None:
            self._prepare_executor.shutdown(wait=True, cancel_futures=False)
            if self._prepare_future is not None:
                self._dispose_prepared(self._prepare_future.result())
        self._prepare_executor = self._prepare_future = None
        self._prepare_job = None
        self._prepare_cancel = None

    def set(self, slots, *, reload_assets=False):
        # Synchronous startup/export/restore invalidates and drains live work.
        self.cancel_preparation()
        return self._set(slots, reload_assets=reload_assets)

    def _set(self, slots, prepared=None, *, reload_assets=False):
        from effect_control import validate_audio_fx
        slots = validate_audio_fx(slots)
        if self._topology_graph is not None and any(plan.inflight for plan in self._topology_graph.plans):return False
        if self._parameter_pending is not None:
            pending=self._parameter_pending
            released=self._parameter_transaction.run(self.stream,())
            if not released.success:return False
            self._parameter_pending=None
            # The native batch contains changed stages, not every active stage.
            # BOOL success is completed count, not a possibly-zero error code.
            # Unlock retry replays metadata only; never resubmit accepted FX.
            if pending[2][0]==len(pending[1]):
                for stage,slot in pending[1]:stage.slot=slot
                self.state=pending[0];self._faulted=False
            else:
                for index,(stage,slot) in enumerate(pending[1]):
                    rolled=(bool(pending[2][6]&(1<<index)) if len(pending[2])>6 else index<pending[2][4])
                    if index<pending[2][0] and not rolled:stage.slot=slot
                self._faulted=pending[2][4]<pending[2][0]
        if self._retired_stages:
            # Failed newly-created attachments are owned until removal succeeds.
            if not self.bass.BASS_ChannelLock(self.stream, True): return False
            removed=[]
            try:
                remaining=[]
                for stage in self._retired_stages:
                    if stage.detach(dispose=False): removed.append(stage)
                    else: remaining.append(stage)
                self._retired_stages=remaining
            finally: self.bass.BASS_ChannelLock(self.stream, False)
            for stage in removed: stage.dispose()
            if remaining: return False
        if not self.stream or (slots == self.state and not reload_assets and not self._faulted):
            self._dispose_prepared(prepared)
            self.state = slots
            return True
        active, plan, available = self._plan(slots, reload_assets)
        # The common native barrier removes lock-held Python/GIL returns for
        # existing vendor parameter edits only. Topology/FIR remains separate.
        if not reload_assets and not self._faulted and not available and len(active)==len(self.stages) and active and \
                all(stage is current and slot.kind not in _CUSTOM_FX and slot.kind!='volume_envelope' and stage.priority==-1-index
                    for index,(slot,stage,current) in enumerate(zip(active,plan,self.stages))):
            updates=[(stage,slot) for stage,slot in zip(self.stages,active) if stage.slot!=slot]
            if updates:
                values=tuple((stage.handle,audio_fx_native_parameters(slot,self.sample_rate),audio_fx_native_parameters(stage.slot,self.sample_rate))
                    for stage,slot in updates)
                result=self._parameter_transaction.parameters(self.stream,values)
                if result is not None:
                    if result[3]==3:self._parameter_pending=(slots,tuple(updates),result)
                    elif result[3]==0:
                        for stage,slot in updates:stage.slot=slot
                        self.state=slots
                    else:
                        for index,(stage,slot) in enumerate(updates):
                            rolled=(bool(result[6]&(1<<index)) if len(result)>6 else index<result[4])
                            if index<result[0] and not rolled:stage.slot=slot
                        self._faulted=result[4]<result[0]
                    if result[3]!=0:self.check(0,tr('audio_fx.operation.parameters'))
                    self._dispose_prepared(prepared)
                    return result[3]==0
        if prepared is None:
            prepared = self._prepare(active,self._missing_kernels(active,plan,reload_assets=reload_assets),self.sample_rate)
        if isinstance(prepared,AudioFXPreparationFailure):
            self._preparation_failed(prepared); return False
        if prepared is None:
            self.check(0, tr("audio_fx.operation.prepare"))
            return False
        if not self.check(self.bass.BASS_ChannelLock(self.stream, True), tr("audio_fx.operation.lock")):
            for pointer in prepared:
                if pointer: self._destroy_prepared(pointer)
            return False
        created, retired, replaced = [], [], []
        previous = [(stage, stage.slot, stage.priority) for stage in self.stages]
        try:
            for index, slot in enumerate(active):
                stage = plan[index]
                priority = -1 - index
                if stage is None:
                    pointer, prepared[index] = prepared[index], None
                    try:
                        stage = AudioFXStage(self, slot, priority, pointer)
                    except (OSError, RuntimeError, ValueError):
                        prepared[index] = pointer
                        raise
                    created.append(stage); plan[index] = stage
                else:
                    if self._kernel_change(slot,stage,reload_assets=reload_assets):
                        pointer=prepared[index]
                        if not pointer or not self.dll.fx_kernel_transition(stage.pointer,pointer,slot.values[-1]):
                            self._preparation_failed(AudioFXPreparationFailure(tr('audio_fx.transition_failed')))
                            raise RuntimeError('Audio FX kernel transition')
                        replaced.append((stage,pointer,stage.asset_identity,stage.previous_asset_identity))
                        stage.previous_asset_identity=stage.asset_identity
                        with self._asset_lock: stage.asset_identity=self._prepared_assets.get(pointer)
                        with self._asset_lock:
                            if stage.asset_identity is None: self._prepared_assets.pop(stage.pointer,None)
                            else: self._prepared_assets[stage.pointer]=stage.asset_identity
                        stage.slot=slot
                    elif slot != stage.slot and not stage.update(slot):
                        self.check(0, tr("audio_fx.operation.parameters"))
                        raise RuntimeError("Audio FX parameters")
                    if priority != stage.priority:
                        if not self.check(self.bass.BASS_FXSetPriority(stage.handle, priority), tr("audio_fx.operation.order")):
                            raise RuntimeError("Audio FX order")
                        stage.priority = priority
            retired = available
            for stage in retired:
                if not stage.detach(dispose=False): raise RuntimeError("Audio FX removal")
            for stage,pointer,_identity,_previous_identity in replaced:
                if not self.dll.fx_kernel_transition_commit(stage.pointer,pointer):
                    raise RuntimeError('Audio FX kernel commit')
            self.stages = plan
            self.state = slots
            self._faulted = False
            return True
        except (OSError, RuntimeError, ValueError):
            self._faulted = True
            for stage,pointer,identity,previous_identity in reversed(replaced):
                self.dll.fx_kernel_transition_undo(stage.pointer,pointer)
                stage.asset_identity=identity
                stage.previous_asset_identity=previous_identity
                with self._asset_lock:
                    if identity is None: self._prepared_assets.pop(stage.pointer,None)
                    else: self._prepared_assets[stage.pointer]=identity
            for stage, saved, priority in previous:
                if stage.slot != saved: stage.update(saved)
                if stage.priority != priority:
                    self.bass.BASS_FXSetPriority(stage.handle, priority); stage.priority = priority
            for stage in created:
                if not stage.detach(dispose=False): self._retired_stages.append(stage)
            retired = created
            return False
        finally:
            self.bass.BASS_ChannelLock(self.stream, False)
            for stage in retired: stage.dispose()
            for pointer in prepared:
                if pointer: self._destroy_prepared(pointer)

    def _preparation_failed(self,failure):
        message=tr('audio_fx.preparation_failed',error=failure.message)
        if self.domain_failure_sink is not None: self.domain_failure_sink(message)
        else: self.check(0,message)

