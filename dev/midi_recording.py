"""Raw-input MIDI recording and atomic SMF save; no Qt/audio callback owner.

Native spool capture runs before transforms on the existing MIDI execution
thread. It retains full packets (including cross-packet SysEx) and QPC times.
Python conversion is save-time work, never the live event delivery path.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import struct
import tempfile
import time
import threading
from pathlib import Path

from i18n import tr

_HEADER = struct.Struct('<QQ')
_RECORD = struct.Struct('<QQQ')
_MAGIC = 0x3143455244494D41
_TICKS_PER_BEAT = 10000  # 120 BPM -> 50us ticks, a file policy, not input accuracy


def _vlq(value):
    if not 0 <= value <= 0x0fffffff:
        raise ValueError(tr('midi_recording.invalid_data'))
    result = [value & 127]
    while value >> 7:
        value >>= 7
        result.insert(0, (value & 127) | 128)
    return bytes(result)


class _WireDecoder:
    """MIDI1 byte-stream state, including running status and realtime in SysEx."""
    def __init__(self):
        self.running = 0
        self.message = bytearray()
        self.sysex = bytearray()

    def feed(self, data):
        for byte in data:
            if byte >= 0xf8:
                yield bytes([byte])
                continue
            if self.sysex:
                if byte == 0xf7:
                    self.sysex.append(byte)
                    yield bytes(self.sysex)
                    self.sysex.clear()
                    continue
                if byte < 0x80:
                    self.sysex.append(byte)
                    continue
                raise ValueError(tr('midi_recording.incomplete_message'))
            if byte & 128:
                if self.message:
                    raise ValueError(tr('midi_recording.incomplete_message'))
                if byte == 0xf0:
                    self.running = 0
                    self.sysex.append(byte)
                    continue
                if byte in (0xf4, 0xf5, 0xf7):
                    self.running = 0
                    yield bytes([byte])
                    continue
                self.running = byte if byte < 0xf0 else 0
                self.message.append(byte)
            else:
                if not self.message:
                    if not self.running:
                        raise ValueError(tr('midi_recording.incomplete_message'))
                    self.message.append(self.running)
                self.message.append(byte)
            status = self.message[0]
            size = (2 if status & 0xf0 in (0xc0, 0xd0) else 3) if status < 0xf0 else {0xf1: 2, 0xf2: 3, 0xf3: 2, 0xf6: 1}.get(status, 1)
            if len(self.message) == size:
                yield bytes(self.message)
                self.message.clear()


class _Track:
    def __init__(self, path, port):
        self.file = Path(path).open('w+b')
        self.tick = 0
        self.notes = {}
        self.channels = set()
        self.event(0, b'\xff\x21\x01' + bytes([port]))
        name = tr('midi_recording.track_name', port=port + 1).encode('utf-8')
        self.event(0, b'\xff\x03' + _vlq(len(name)) + name)

    def event(self, tick, data):
        tick = max(self.tick, tick)
        delta = tick - self.tick
        while delta > 0x0fffffff:
            self.file.write(_vlq(0x0fffffff) + b'\xff\x7f\x00')
            delta -= 0x0fffffff
        self.file.write(_vlq(delta) + data)
        self.tick = tick

    def message(self, tick, data):
        if data[0] < 0xf0:
            channel, command = data[0] & 15, data[0] & 0xf0
            self.channels.add(channel)
            if command in (0x80, 0x90):
                key = channel, data[1]
                count = self.notes.get(key, 0)
                count += 1 if command == 0x90 and data[2] else -1
                if count > 0:
                    self.notes[key] = count
                else:
                    self.notes.pop(key, None)
            if command == 0xb0 and data[1] in (120, 123):
                self.notes = {key: n for key, n in self.notes.items() if key[0] != channel}
            self.event(tick, data)
        elif data[0] == 0xf0:
            self.event(tick, b'\xf0' + _vlq(len(data) - 1) + data[1:])
        else:
            # SMF escape retains System Common/Realtime, including FF Reset.
            self.event(tick, b'\xf7' + _vlq(len(data)) + data)

    def release_notes(self, tick):
        for (channel, key), count in tuple(self.notes.items()):
            for _ in range(count):
                self.message(tick, bytes([0x80 | channel, key, 0]))
        for channel in sorted(self.channels):
            self.message(tick, bytes([0xb0 | channel, 120, 0]))


def write_smf(spool, destination):
    """Stream a validated recording into SMF1 with one ordered track per port.

    Fixed 120BPM is a time coordinate, not guessed score tempo. Gaps while
    paused are removed. Paused controller changes resume at the next segment;
    paused notes are excluded. File-only note cleanup never reaches playback.
    """
    destination = Path(destination)
    if destination.suffix.lower() != '.mid':
        raise ValueError(tr('midi_recording.extension_invalid'))
    tracks = {}
    decoders = {}
    pending = []
    directory = tempfile.TemporaryDirectory(prefix='ABMSynth-midi-')
    try:
        with Path(spool).open('rb') as source:
            header = source.read(_HEADER.size)
            if len(header) != _HEADER.size:
                raise ValueError(tr('midi_recording.invalid_data'))
            magic, frequency = _HEADER.unpack(header)
            if magic != _MAGIC or not frequency:
                raise ValueError(tr('midi_recording.invalid_data'))
            active = False
            start = elapsed = last = 0
            def ticks(stamp):
                return round((elapsed + max(0, stamp-start) if active else elapsed) * 20000 / frequency)
            def track(port):
                if port not in tracks:
                    tracks[port] = _Track(Path(directory.name) / f'{port}.track', port)
                return tracks[port]
            ended = False
            while raw := source.read(_RECORD.size):
                if len(raw) != _RECORD.size:
                    raise ValueError(tr('midi_recording.invalid_data'))
                stamp, descriptor, size = _RECORD.unpack(raw)
                kind, port = descriptor >> 32, descriptor & 0xffffffff
                if port > 7 or size > 1024*1024 or (kind and size) or kind > 3:
                    raise ValueError(tr('midi_recording.invalid_data'))
                payload = source.read(size)
                if len(payload) != size:
                    raise ValueError(tr('midi_recording.invalid_data'))
                if kind == 1:
                    if active:
                        raise ValueError(tr('midi_recording.invalid_data'))
                    start, active = stamp, True
                    for saved_port, data in pending:
                        track(saved_port).message(ticks(stamp), data)
                    pending.clear()
                elif kind in (2, 3):
                    boundary = ticks(stamp)
                    for current in tracks.values():
                        current.release_notes(boundary)
                    if active:
                        elapsed += max(0, stamp-start)
                    active = False
                    if kind == 3:
                        ended = True
                        if source.read(1):
                            raise ValueError(tr('midi_recording.invalid_data'))
                        break
                else:
                    decoder = decoders.setdefault(port, _WireDecoder())
                    for data in decoder.feed(payload):
                        if active:
                            track(port).message(ticks(stamp), data)
                        elif data[0] >= 0xa0 and data[0] < 0xf0 or data[0] == 0xf0:
                            pending.append((port, data))
                last = stamp
            if not ended or any(d.message or d.sysex for d in decoders.values()):
                raise ValueError(tr('midi_recording.incomplete_message'))
            final_tick = ticks(last)
            conductor = bytearray(b'\x00\xff\x51\x03\x07\xa1\x20')
            remaining = final_tick
            while remaining > 0x0fffffff:
                conductor.extend(_vlq(0x0fffffff) + b'\xff\x7f\x00')
                remaining -= 0x0fffffff
            conductor.extend(_vlq(remaining) + b'\xff\x2f\x00')
            for current in tracks.values():
                current.event(final_tick, b'\xff\x2f\x00')
                current.file.flush()
                if current.file.tell() > 0xffffffff:
                    raise ValueError(tr('midi_recording.file_too_large'))
            fd, temporary = tempfile.mkstemp(prefix='.'+destination.name+'.', suffix='.tmp', dir=destination.parent)
            try:
                with os.fdopen(fd, 'wb') as target:
                    target.write(b'MThd' + struct.pack('>IHHH', 6, 1, 1+len(tracks), _TICKS_PER_BEAT))
                    target.write(b'MTrk' + struct.pack('>I', len(conductor)) + conductor)
                    for port in sorted(tracks):
                        stream = tracks[port].file
                        target.write(b'MTrk' + struct.pack('>I', stream.seek(0, 2)))
                        stream.seek(0)
                        shutil.copyfileobj(stream, target)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    finally:
        for current in tracks.values():
            current.file.close()
        directory.cleanup()


class MidiPerformanceRecorder:
    """Draft ownership across stream/device changes, independent of audio capture."""
    recording_container = 'mid'
    last_recording_loss = 0
    last_recording_error = None

    def __init__(self, input_runtime):
        self.input_runtime = input_runtime
        self.library = None
        self.pointer = None
        self.path = None
        self.recording_state = 'idle'
        self.recording_date = None
        self._bound = None
        self._finished = False
        self._saving = False
        self._binding_lock = threading.RLock()

    @property
    def stream(self):
        return bool(self.input_runtime.native_factory)

    @property
    def encoder(self):
        return self.pointer is not None and self.stream and not self._finished

    @property
    def has_draft(self):
        return self.pointer is not None

    def _bind(self, execution):
        with self._binding_lock:
            self._bind_locked(execution)

    def _bind_locked(self, execution):
        if self._bound is execution:
            return
        if self._bound is not None:
            with self._bound.borrow() as pointer:
                if pointer:
                    self.library.midi_execution_recording_bind(pointer, None)
            self._bound = None
        if execution is not None:
            with execution.borrow() as pointer:
                if not pointer or not self.library.midi_execution_recording_bind(pointer, self.pointer):
                    raise RuntimeError(tr('midi_recording.no_input'))
            self._bound = execution

    def start_recording(self):
        if self.has_draft:
            raise RuntimeError(tr('midi_recording.draft_exists'))
        factory = self.input_runtime.native_factory
        if factory is None:
            raise RuntimeError(tr('midi_recording.no_input'))
        library = ctypes.CDLL(factory.execution.library._name)
        signatures = {
            'midi_recording_version': ([], ctypes.c_uint),
            'midi_recording_create': ([ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint)], ctypes.c_void_p),
            'midi_recording_release': ([ctypes.c_void_p], None),
            'midi_recording_segment': ([ctypes.c_void_p, ctypes.c_uint], ctypes.c_int),
            'midi_recording_finish': ([ctypes.c_void_p], ctypes.c_int),
            'midi_recording_status': ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64)], ctypes.c_int),
            'midi_execution_recording_bind': ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
        }
        try:
            for name, (args, result) in signatures.items():
                fn = getattr(library, name)
                fn.argtypes, fn.restype = args, result
            if library.midi_recording_version() != 1:
                raise AttributeError('ABI')
        except AttributeError as error:
            raise RuntimeError(tr('midi_recording.native_unavailable')) from error
        directory = Path(tempfile.mkdtemp(prefix='ABMSynth-midi-recording-'))
        self.path = directory / 'capture.raw'
        error = ctypes.c_uint()
        self.library = library
        self.pointer = library.midi_recording_create(str(self.path), ctypes.byref(error))
        if not self.pointer:
            self._delete_spool()
            raise OSError(tr('midi_recording.native_error', error=error.value))
        try:
            self._bind(factory.execution)
        except BaseException:
            self.discard_recording()
            raise
        self._finished = False
        self.recording_date = time.localtime()
        self.recording_state = 'recording'

    def pause_recording(self):
        if self.pointer and not self._finished and not self.library.midi_recording_segment(self.pointer, 2):
            status = self.recording_status()
            if not status['failed']:
                raise RuntimeError(tr('midi_recording.native_error', error=status['error']))
        if self.has_draft:
            self.recording_state = 'paused'

    def resume_recording(self):
        if not self.has_draft or self._finished or not self.stream:
            raise RuntimeError(tr('midi_recording.no_input'))
        self._bind(self.input_runtime.native_factory.execution)
        if not self.library.midi_recording_segment(self.pointer, 1):
            raise RuntimeError(tr('midi_recording.native_error', error=self.recording_status()['error']))
        self.recording_state = 'recording'

    def recording_status(self):
        if not self.pointer:
            return None
        values = (ctypes.c_uint64 * 8)()
        if not self.library.midi_recording_status(self.pointer, values):
            raise RuntimeError(tr('midi_recording.native_unavailable'))
        return dict(seconds=values[3]/values[4], failed=bool(values[1] or values[2]),
                    lost_bytes=int(values[1]), error=int(values[2]), frames=int(values[0]))

    def source_closed(self):
        if self.has_draft and not self._saving:
            try:self.pause_recording()
            finally:self._bind(None)

    def save_recording(self, path):
        if not self.has_draft:
            return
        self._saving = True
        try:
            self._save_recording(path)
        finally:
            self._saving = False

    def _save_recording(self, path):
        self.pause_recording()
        self._bind(None)
        if not self.library.midi_recording_finish(self.pointer):
            raise RuntimeError(tr('midi_recording.flush_pending'))
        self._finished = True
        status = self.recording_status()
        if status['failed']:
            raise RuntimeError(tr('midi_recording.capture_failed', count=status['lost_bytes'], error=status['error']))
        write_smf(self.path, path)
        self.discard_recording()

    def _delete_spool(self):
        if self.path is not None:
            self.path.unlink(missing_ok=True)
            self.path.parent.rmdir()
            self.path = None

    def discard_recording(self):
        self._bind(None)
        if self.pointer:
            if not self.library.midi_recording_finish(self.pointer):
                raise RuntimeError(tr('midi_recording.flush_pending'))
            self.library.midi_recording_release(self.pointer)
            self.pointer = None
        self._delete_spool()
        self.recording_state = 'idle'
        self._finished = False

    def stop_recording(self):
        # Error handling pauses the draft; never discard the user's recording.
        self.recording_state = 'paused' if self.has_draft else 'idle'
