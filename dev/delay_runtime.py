"""BMSynth-style per-channel delay DSP and GS preset notification owner."""

from __future__ import annotations

import ctypes
import os
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path

import numpy as np

from bass_bindings import DSPPROC
from i18n import tr


# Source runtime order: Default, Off, four Delay, four Pan Delay rows.
BMSYNTH_DELAY_PRESETS = {
    "Default": (3, 100, 340, 35), "Off": (0, 0, 10, 0),
    "Delay 1": (3, 100, 340, 30), "Delay 2": (3, 100, 600, 50),
    "Delay 3": (3, 100, 1000, 50), "Delay 4": (3, 100, 150, 30),
    "Pan Delay 1": (2, 100, 300, 50), "Pan Delay 2": (2, 100, 400, 50),
    "Pan Delay 3": (2, 100, 550, 50), "Pan Delay 4": (2, 100, 140, 30),
}


def bmsynth_delay_curve(percent, value):
    """Original 00465F88 two-pass response shared by Delay and VelocityRate.

    The old port stopped after the first quadratic. The second rounded pass
    at 00466023..0046608F changes both CC#94 gain and the Note-On table.
    x87 FISTP at 004029A4 uses round-to-nearest-even in its normal mode.
    """
    midpoint = (max(0, min(200, int(percent))) * 64) // 100

    def one_pass(amount):
        x = max(0, min(127, int(amount))) / 127.0
        return int(round(2.0 * midpoint * (1.0 - x) * x + 127.0 * x * x))

    return one_pass(one_pass(value))


class BMSynthDelayState:
    """One original-style user-FX delay buffer for one MIDI channel."""

    def __init__(self, sample_rate=96000, delay_type=0, level=0, time_ms=10, feedback=0, send=0):
        self.sample_rate = int(sample_rate)
        self.delay_type = int(delay_type)
        self.level = int(level)
        self.time_ms = int(time_ms)
        self.feedback = float(feedback) / 100.0
        self._parameters = (self.delay_type, self.level, self.time_ms, self.feedback)
        self.send = int(send)
        self._configure_buffer()

    def _configure_buffer(self, frames=None):
        if frames is None:
            frames = max(1, (self.sample_rate * max(1, self.time_ms)) // 1000)
        self.buffer = np.zeros((frames, 2), dtype=np.float32)
        self.write_index = 0
        self.filled = 0

    def set_parameters(self, delay_type, level, time_ms, feedback):
        self.delay_type = delay_type
        self.level = level
        self.time_ms = time_ms
        self.feedback = feedback / 100.0
        # The DSP callback reads one immutable snapshot. GUI edits never
        # partially change a block or replace a live callback's ring.
        self._parameters = (delay_type, level, time_ms, self.feedback)

    @property
    def gain(self):
        first = bmsynth_delay_curve(self._parameters[1], self.send)
        second = bmsynth_delay_curve(80, first)
        return min(second / 128.0, 0.95)

    def process(self, samples, *, wet_only=False):
        delay_type, level, time_ms, feedback = self._parameters
        send = self.send
        # BMSynth's UserFX callback clears ring cursors on a zero send. A
        # parameter edit does not erase pending echoes unless the requested
        # delay length actually changes (00465A98..00465AD5).
        if send <= 0:
            self.write_index = 0
            self.filled = 0
            if wet_only:
                samples.fill(0)
            return
        requested_frames = max(1, (self.sample_rate * max(1, time_ms)) // 1000)
        if requested_frames != len(self.buffer):
            self._configure_buffer(requested_frames)
        if delay_type not in (1, 2, 3):
            if wet_only:
                samples.fill(0)
            return
        offset = 0
        gain = min(bmsynth_delay_curve(80, bmsynth_delay_curve(level, send)) / 128.0, 0.95)
        while offset < len(samples):
            count = min(len(self.buffer) - self.write_index, len(samples) - offset)
            source = samples[offset:offset + count].copy()
            delayed = self.buffer[self.write_index:self.write_index + count].copy()
            if self.filled >= len(self.buffer):
                if delay_type == 1:
                    wet = delayed
                elif delay_type == 2:
                    wet = delayed[:, ::-1]
                else:
                    mono = 0.5 * (delayed[:, 0] + delayed[:, 1])
                    wet = np.column_stack((mono, mono))
                samples[offset:offset + count] = wet if wet_only else source + wet
                self.buffer[self.write_index:self.write_index + count] = (source + wet * feedback) * gain
            else:
                if wet_only:
                    samples[offset:offset + count] = 0
                self.buffer[self.write_index:self.write_index + count] = source * gain
                self.filled = min(len(self.buffer), self.filled + count)
            self.write_index = (self.write_index + count) % len(self.buffer)
            offset += count


class DelayRuntime:
    """Own native-address DSP buffers, CC#94 values and UI preset mailbox."""

    def __init__(self, *, native_dll=None):
        self.sample_rate = 96000
        self.reset_commit_scope = nullcontext
        self.states = [BMSynthDelayState() for _ in range(128)]
        self.melodic_nine = BMSynthDelayState()
        self.sysex_enabled = False
        self._ui_lock = threading.Lock()
        self._ui_updates = {}
        self.callback = DSPPROC(self.process_dsp)
        # Reference callback above is never registered by the active runtime.
        # The control lock protects publication vs disposal, not the audio DSP.
        self._native_lock = threading.RLock()
        self._native_taps = {}
        self._native_library = None
        self._native_path = Path(native_dll or os.environ.get('ABMSYNTH_OBSERVATION_DLL',Path(__file__).with_name('abmsynth_dsp.dll')))
        self._midi_controls=None;self._midi_send=None;self._midi_gain=None
        self.native_revision=0

    def _load_native(self):
        if self._native_library is not None:
            return
        try:
            library = ctypes.CDLL(str(self._native_path.resolve()))
            library.delay_version.argtypes, library.delay_version.restype = [], ctypes.c_uint
            if library.delay_version() != 1:
                raise ValueError("delay ABI1")
            library.delay_create.argtypes, library.delay_create.restype = [ctypes.c_uint, ctypes.c_int], ctypes.c_void_p
            library.delay_destroy.argtypes, library.delay_destroy.restype = [ctypes.c_void_p], None
            library.delay_parameters.argtypes = [ctypes.c_void_p]+[ctypes.c_uint]*5
            library.delay_parameters.restype = ctypes.c_int
            # audio_fx.cpp delay ABI1; bass.h DSPPROC CALLBACK signature,
            # interleaved stereo float, bytes length and null removal notice.
            self.native_callback = DSPPROC(("delay_bass_dsp", library))
        except (OSError, AttributeError, ValueError) as error:
            raise RuntimeError(tr("native_delay.prepare_failed", error=error)) from error
        self._native_library = library
        if getattr(library,'delay_midi_version',None) is not None:
            library.delay_midi_version.argtypes=[];library.delay_midi_version.restype=ctypes.c_uint
            if library.delay_midi_version()==1:
                for name,args,result in (('delay_midi_controls',[ctypes.c_void_p]+[ctypes.c_uint]*4,ctypes.c_int),
                    ('delay_midi_send',[ctypes.c_void_p,ctypes.c_uint],ctypes.c_int),
                    ('delay_midi_table',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ubyte),ctypes.c_uint],ctypes.c_int),
                    ('delay_midi_retain',[ctypes.c_void_p],None),('delay_midi_release',[ctypes.c_void_p],None)):
                    function=getattr(library,name);function.argtypes=args;function.restype=result
                self._midi_controls=library.delay_midi_controls;self._midi_send=library.delay_midi_send

    @contextmanager
    def midi_links(self,library):
        """Control-side leases; a map never borrows pointers across DLL images."""
        leased=[]
        with self._native_lock:
            self._load_native()
            if self._midi_controls is None or library._handle!=self._native_library._handle:
                links=None
            else:
                links=dict(self._native_taps)
                for pointer in links.values():
                    self._native_library.delay_midi_retain(pointer);leased.append(pointer)
        try:yield links
        finally:
            for pointer in leased:self._native_library.delay_midi_release(pointer)

    def _publish_native(self, key, pointer, *, send=False):
        channel, wet_only = key
        state = self.melodic_nine if channel == 9 and not wet_only else self.states[channel]
        kind, level, time_ms, feedback = state._parameters
        frames = max(1, state.sample_rate*max(1, time_ms)//1000)
        if self._midi_controls is not None:
            accepted=self._midi_controls(pointer,frames,kind,round(feedback*100),level)
            if accepted and send:accepted=self._midi_send(pointer,state.send)
        else:
            gain = bmsynth_delay_curve(80, bmsynth_delay_curve(level, state.send))
            accepted=self._native_library.delay_parameters(pointer,frames,kind,round(feedback*100),gain,state.send>0)
        if not accepted:
            raise RuntimeError(tr("native_delay.parameters_failed"))

    def _publish_native_taps(self, channel=None, *, send=False):
        with self._native_lock:
            for key, pointer in self._native_taps.items():
                if channel is None or key[0] == channel:
                    self._publish_native(key, pointer,send=send)

    def prepare_native_tap(self, channel, wet_only=False):
        """Control path only; prewarm potential dry routes before playback."""
        key = (channel, bool(wet_only))
        with self._native_lock:
            self._load_native()
            if key not in self._native_taps:
                capacity = self.sample_rate*2  # Current Time ceiling2000ms.
                if not 1 <= capacity <= 0xffffffff:
                    raise ValueError(tr("native_delay.capacity_failed"))
                pointer = self._native_library.delay_create(capacity, key[1])
                if not pointer:
                    raise MemoryError(tr("native_delay.capacity_failed"))
                try:
                    if self._midi_controls is not None:
                        if self._midi_gain is None:
                            values=[bmsynth_delay_curve(80,bmsynth_delay_curve(level,value)) for level in range(201) for value in range(128)]
                            self._midi_gain=(ctypes.c_ubyte*len(values))(*values)
                        if not self._native_library.delay_midi_table(pointer,self._midi_gain,len(self._midi_gain)):
                            raise RuntimeError(tr('native_delay.parameters_failed'))
                    self._publish_native(key, pointer,send=True)
                except Exception:
                    self._native_library.delay_destroy(pointer)
                    raise
                self._native_taps[key] = pointer
                self.native_revision+=1
            return self.native_callback, self._native_taps[key]

    def release_native_taps(self, keys):
        """Only after ChannelDelayEffects has removed all corresponding DSPs."""
        with self._native_lock:
            for key in keys:
                pointer = self._native_taps.pop(key, None)
                if pointer:
                    self._native_library.delay_destroy(pointer)
                    self.native_revision+=1

    def set_sample_rate(self, sample_rate):
        with self.reset_commit_scope():return self._set_sample_rate(sample_rate)

    def _set_sample_rate(self, sample_rate):
        if self._native_taps:
            raise RuntimeError(tr("native_delay.rate_while_active"))
        # AudioSession calls this once per new BASS stream generation. Do not
        # carry an old stream's pending echoes into the replacement stream.
        self.sample_rate = int(sample_rate)
        self.states = [BMSynthDelayState(
            self.sample_rate, state.delay_type, state.level, state.time_ms,
            round(state.feedback * 100), 0,
        ) for state in self.states]
        state = self.states[9]
        self.melodic_nine = BMSynthDelayState(self.sample_rate, state.delay_type, state.level,
                                             state.time_ms, round(state.feedback * 100), 0)

    def process_dsp(self, _hdsp, _channel, buffer, length, user):
        if not buffer or length < 8:
            return
        index = ctypes.cast(user, ctypes.c_void_p).value or 0
        wet_only = index >= 128
        if wet_only:
            index -= 128
        if not 0 <= index < len(self.states):
            return
        floats = length // ctypes.sizeof(ctypes.c_float)
        floats -= floats % 2
        if floats:
            samples = np.ctypeslib.as_array(
                ctypes.cast(buffer, ctypes.POINTER(ctypes.c_float)), shape=(floats,),
            )
            # The drum UserFX bus is additive to the normal dry path. Output
            # only the echo there; melodic channel DSPs retain dry+wet.
            state = self.melodic_nine if index == 9 and not wet_only else self.states[index]
            state.process(samples.reshape((-1, 2)), wet_only=wet_only)

    def set_delay(self, delay_type, level, time_ms, feedback):
        delay_type = max(0, min(3, int(round(float(delay_type)))))
        level = max(0, min(200, int(round(float(level)))))
        time_ms = max(1, min(2000, int(round(float(time_ms)))))
        feedback = max(0, min(100, int(round(float(feedback)))))
        for state in (*self.states, self.melodic_nine):
            # Publish parameter changes without replacing the DSP-owned ring.
            # Its next callback resizes only when the requested time differs.
            state.set_parameters(delay_type, level, time_ms, feedback)
        self._publish_native_taps()

    def set_channel_send(self, channel, value):
        self.states[channel].send = value
        if channel == 9:
            self.melodic_nine.send = value
        self._publish_native_taps(channel,send=True)

    def clear_sends(self):
        for state in (*self.states, self.melodic_nine):
            state.send = 0
        self._publish_native_taps(send=True)

    def apply_preset_index(self, index):
        index = int(index)
        names = tuple(BMSYNTH_DELAY_PRESETS)
        if not 0 <= index < len(names):
            return False
        name = names[index]
        self.set_delay(*BMSYNTH_DELAY_PRESETS[name])
        with self._ui_lock:
            self._ui_updates.update(zip(
                ("delay_type", "delay_level", "delay_time", "delay_feedback"),
                BMSYNTH_DELAY_PRESETS[name],
            ))
            self._ui_updates["preset"] = name
        return True

    def pop_ui_updates(self):
        with self._ui_lock:
            updates, self._ui_updates = self._ui_updates, {}
        return updates

    def reset_preset_for_midi_reset(self):
        if self.sysex_enabled:
            self.apply_preset_index(0)

    @staticmethod
    def sysex_time_ms(value):
        """Source 00459574 piecewise GS Time conversion (before UI clamp)."""
        for low, high, slope, offset in (
            (1, 44, 0, 9), (45, 54, 1, -35), (55, 69, 2, -90),
            (70, 73, 5, -300), (80, 89, 10, -700),
            (90, 104, 20, -1600), (105, 115, 50, -4750),
        ):
            if low <= value <= high:
                return slope * value + offset
        return 300

    @staticmethod
    def decode_sysex(message):
        """Source GS DT1 preset/Time/Feedback parser, before control clamps."""
        if len(message) != 11 or message[0] != 0xF0 or message[-1] != 0xF7:
            return None
        if message[1:5] != b"\x41\x10\x42\x12" or message[5:7] != b"\x40\x01":
            return None
        value, checksum = message[8], message[9]
        # Source 00459538 uses 128-(sum & 127), not a final &127.
        # Keeping its unusual zero-sum boundary avoids inventing acceptance.
        if checksum != 128 - (sum(message[5:9]) & 0x7F):
            return None
        address = message[7]
        if address == 0x50:
            return "preset", value if value <= 7 else 0
        if address == 0x52:
            return "delay_time", DelayRuntime.sysex_time_ms(value if value <= 115 else 97)
        if address == 0x58:
            value = value if value <= 127 else 64
            return "delay_feedback", 100 + int((value - 64) * 100 / 64)
        return None

    def apply_sysex(self, message):
        if not self.sysex_enabled:
            return False
        decoded = self.decode_sysex(message)
        if decoded is None:
            return False
        field, value = decoded
        if field == "preset":
            return self.apply_preset_index(value + 2)
        value = max(10, min(1000, value)) if field == "delay_time" else max(0, min(100, value))
        for state in self.states:
            delay_type, level, time_ms, feedback = state._parameters
            state.set_parameters(delay_type, level, value if field == "delay_time" else time_ms,
                                 value if field == "delay_feedback" else round(feedback * 100))
        self._publish_native_taps()
        with self._ui_lock:
            self._ui_updates[field] = value
        return True

