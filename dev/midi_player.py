"""SMF timelines, non-Qt transport and isolated offline export.

This is a timing/process boundary, not a second audio engine. Both live file
events and offline events use audio_engine.forward_midi_bytes (CC/Learn/SysEx
transforms, drum sends and group overrides included). Widgets own no handles.
"""
from __future__ import annotations

import bisect
import ctypes
import hashlib
import json
import multiprocessing
import os
import random
import struct
import tempfile
import threading
from winmm_midi_pipe import WinMMMidiPipeServer
from contextlib import contextmanager
import time
from functools import lru_cache
from collections import deque
from dataclasses import dataclass, replace, asdict, fields, is_dataclass
from pathlib import Path

from i18n import tr
from midi_input_runtime import MidiAddress, MIDI_PORT_LIMIT


class MidiPlaylistOrder:
    """UI-session navigation; random bag without repeats and reversible history."""
    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.shuffle = False
        self.reset((), None)

    def reset(self, paths, current):
        self.paths = tuple(paths)
        self.history = [current] if current in self.paths else []
        self.cursor = len(self.history)-1
        self.remaining = [path for path in self.paths if path != current]
        self.rng.shuffle(self.remaining)

    def available(self, step):
        if not self.history:return False
        if self.shuffle:
            return self.cursor > 0 if step < 0 else self.cursor+1 < len(self.history) or bool(self.remaining)
        index = self.paths.index(self.history[self.cursor])+step
        return 0 <= index < len(self.paths)

    def move(self, step):
        if not self.available(step):return None
        if not self.shuffle:
            path = self.paths[self.paths.index(self.history[self.cursor])+step]
            self.reset(self.paths, path)
            return path
        if step < 0:self.cursor -= 1
        elif self.cursor+1 < len(self.history):self.cursor += 1
        else:
            self.history.append(self.remaining.pop())
            self.cursor += 1
        return self.history[self.cursor]


@dataclass(frozen=True)
class MidiFileEvent:
    seconds: float
    data: bytes = b""
    text: str = ""
    kind: int = 0
    port: int = 0

    @property
    def channel(self):
        return MidiAddress(self.port, self.data[0] & 15).native if self.data and self.data[0] < 0xf0 else None


@dataclass(frozen=True)
class MidiDocument:
    path: str
    events: tuple[MidiFileEvent, ...]
    duration: float
    tracks: int
    channels: tuple[tuple[int, int], ...]
    source_sha256: str = ""
    source_bytes: int = 0
    # Presentation metadata is separate from transport RAW events. Retaining
    # it must not change native schedule bytes,order or chase semantics.
    score: tuple = ()
    ticks_per_beat: int = 0
    file_format: int = 1

    def musical_position(self,seconds):
        if not self.score:return None
        row=self.score[max(0,bisect.bisect_right(self.score,(max(0,seconds),float('inf')))-1)]
        stamp,tick,tempo,numerator,denominator,key,bar,bar_tick=row
        if self.ticks_per_beat:
            now=tick+max(0,seconds-stamp)*1_000_000/tempo*self.ticks_per_beat
            beat_ticks=self.ticks_per_beat*4/denominator
            beat=max(0,(now-bar_tick)/beat_ticks)
            location=(bar+int(beat//numerator),1+int(beat%numerator))
        else:location=None
        return dict(bpm=60_000_000/tempo,meter=(numerator,denominator),key=key,location=location)

    @property
    def ports(self):
        return tuple(sorted({event.port for event in self.events if event.data} or {0}))

    @classmethod
    def load(cls, path):
        source = Path(path)
        if source.stat().st_size > 64 * 1024 * 1024:
            raise ValueError(tr("player.file_too_large"))
        raw = source.read_bytes()
        identity, source_bytes = hashlib.sha256(raw).hexdigest(), len(raw)
        if raw[:4] == b"RIFF" and raw[8:12] == b"RMID":
            end = min(len(raw), 8 + int.from_bytes(raw[4:8], "little"))
            pos, payload = 12, None
            while pos + 8 <= end:
                size = int.from_bytes(raw[pos+4:pos+8], "little")
                if pos + 8 + size > end:
                    raise ValueError(tr("player.invalid_file"))
                if raw[pos:pos+4] == b"data":
                    payload = raw[pos+8:pos+8+size]
                    break
                pos += 8 + size + size % 2
            raw = payload or b""
        if len(raw) < 14 or raw[:4] != b"MThd":
            raise ValueError(tr("player.invalid_file"))
        header = int.from_bytes(raw[4:8], "big")
        fmt, tracks, division = struct.unpack_from(">HHH", raw, 8)
        if fmt == 2:
            raise ValueError(tr("player.format_two"))
        if fmt not in (0, 1) or not tracks or (fmt == 0 and tracks != 1) or header < 6 or not division:
            raise ValueError(tr("player.invalid_file"))
        pos, collected, last_tick, order = 8 + header, [], 0, 0
        try:
            for _track in range(tracks):
                if raw[pos:pos+4] != b"MTrk":
                    raise ValueError
                size = int.from_bytes(raw[pos+4:pos+8], "big")
                data = raw[pos+8:pos+8+size]
                if len(data) != size:
                    raise ValueError
                pos += 8 + size
                cursor, tick, running, sysex, port, sysex_port = 0, 0, 0, b"", 0, 0

                def vlq():
                    nonlocal cursor
                    value = 0
                    for _ in range(4):
                        byte = data[cursor]; cursor += 1
                        value = (value << 7) | (byte & 127)
                        if not byte & 128:
                            return value
                    raise ValueError

                while cursor < len(data):
                    tick += vlq()
                    status = data[cursor]
                    if status & 128:
                        cursor += 1
                        if status < 0xf0:
                            running = status
                        else:
                            running = 0
                    elif running:
                        status = running
                    else:
                        raise ValueError
                    kind, payload, payload_port = -1, b"", port
                    if status == 0xff:
                        kind = data[cursor]; cursor += 1
                        length = vlq()
                        payload = data[cursor:cursor+length]; cursor += length
                        if len(payload) != length:
                            raise ValueError
                        if kind == 0x21:
                            if length != 1:
                                raise ValueError
                            if payload[0] >= MIDI_PORT_LIMIT:
                                raise UnsupportedMidiPort(payload[0])
                            port = payload[0]
                        if kind == 0x2f:
                            last_tick = max(last_tick, tick)
                            break
                    elif status in (0xf0, 0xf7):
                        length = vlq()
                        part = data[cursor:cursor+length]; cursor += length
                        if len(part) != length:
                            raise ValueError
                        if status == 0xf0:
                            if sysex:
                                raise ValueError
                            sysex = b"\xf0" + part
                            sysex_port = port
                        elif sysex:
                            sysex += part
                        else:
                            payload = part  # SMF escape: complete raw messages.
                        if sysex.endswith(b"\xf7"):
                            payload, sysex = sysex, b""
                            payload_port = sysex_port
                    elif status < 0xf0:
                        length = 1 if status & 0xf0 in (0xc0, 0xd0) else 2
                        part = data[cursor:cursor+length]; cursor += length
                        if len(part) != length or any(byte & 128 for byte in part):
                            raise ValueError
                        payload = bytes([status]) + part
                    else:
                        raise ValueError
                    collected.append((tick, order, kind, payload, payload_port)); order += 1
                    if order > 2_000_000:
                        raise ValueError
                if sysex:
                    raise ValueError
                last_tick = max(last_tick, tick)
        except UnsupportedMidiPort as exc:
            raise ValueError(tr("player.unsupported_port", port=exc.port, limit=MIDI_PORT_LIMIT)) from exc
        except (IndexError, ValueError, struct.error) as exc:
            raise ValueError(tr("player.invalid_file")) from exc
        collected.sort(key=lambda item: (item[0], item[1]))
        tempo, previous_tick, seconds, events, channels = 500000, 0, 0.0, [], {}
        numerator,denominator,key,bar,bar_tick=4,4,None,1,0
        score=[(0.,0,tempo,numerator,denominator,key,bar,bar_tick)]
        system_port = None
        frame_rate = None
        if division & 0x8000:
            frames = 256 - (division >> 8)
            if frames not in (24, 25, 29, 30) or not division & 255:
                raise ValueError(tr("player.invalid_file"))
            frame_rate = (30000 / 1001 if frames == 29 else frames) * (division & 255)
        for tick, _order, kind, payload, port in collected:
            seconds += (tick-previous_tick) / frame_rate if frame_rate else (tick-previous_tick)*tempo/division/1_000_000
            previous_tick = tick
            if kind == 0x51:
                if len(payload) != 3 or not int.from_bytes(payload, "big"):
                    raise ValueError(tr("player.invalid_file"))
                tempo = int.from_bytes(payload, "big")
                score.append((seconds,tick,tempo,numerator,denominator,key,bar,bar_tick))
            elif kind==0x58:
                if len(payload)!=4 or payload[0]==0 or payload[1]>7:raise ValueError(tr('player.invalid_file'))
                if not frame_rate:
                    bar+=int(__import__('math').ceil(max(0,tick-bar_tick)/(division*4/denominator*numerator)))
                bar_tick=tick;numerator,denominator=payload[0],2**payload[1]
                score.append((seconds,tick,tempo,numerator,denominator,key,bar,bar_tick))
            elif kind==0x59:
                if len(payload)!=2 or payload[1]>1:raise ValueError(tr('player.invalid_file'))
                sf=int.from_bytes(payload[:1],'big',signed=True)
                if not -7<=sf<=7:raise ValueError(tr('player.invalid_file'))
                key=(sf,bool(payload[1]));score.append((seconds,tick,tempo,numerator,denominator,key,bar,bar_tick))
            elif kind in (1, 3, 5, 6, 7):
                try:
                    text = payload.decode("utf-8")
                except UnicodeError:
                    text = payload.decode("cp949", errors="replace")
                events.append(MidiFileEvent(seconds, text=text, kind=kind))
            elif kind == -1 and payload:
                if is_midi_system_reset(payload):
                    if system_port is None:
                        system_port = port
                    elif port != system_port:
                        continue  # Native SMF semantics: first system port wins.
                events.append(MidiFileEvent(seconds, payload, port=port))
                if payload[0] < 0xf0:
                    ch = MidiAddress(port, payload[0] & 15).native
                    channels[ch] = channels.get(ch, 0) + int(payload[0] & 0xf0 == 0x90 and payload[-1] > 0)
        seconds += (last_tick-previous_tick) / frame_rate if frame_rate else (last_tick-previous_tick)*tempo/division/1_000_000
        return cls(str(source.resolve()), tuple(events), seconds, tracks, tuple(sorted(channels.items())),
                   identity, source_bytes,tuple(score),0 if frame_rate else division,fmt)


class UnsupportedMidiPort(ValueError):
    def __init__(self, port):
        self.port = port


class _NativeFileRow(ctypes.Structure):
    _fields_=[('seconds',ctypes.c_double),('offset',ctypes.c_uint),('size',ctypes.c_uint),
              ('port',ctypes.c_uint),('reserved',ctypes.c_uint)]


class NativeFileSchedule:
    """Prepared steady file scheduling on the original native ingress worker.

    Reset/chase/EOF/panic stay explicit Python checkpoints. This is not a full
    native file transport claim. Missing ABI/unsupported pre-audio plan retains
    the old whole transport; failures after admission never fall back/resend.
    Native audio completion does not wait for a Python ACK per note. Original
    terminal receipts enter the existing 4096-record/16MiB ring unless the
    native file-observer ABI proves a successful canonical-only completion.
    Mandatory receipts still eventually backpressure delayed Python replay.
    Independence is qualified by event class/count/duration, not unbounded.
    Mandatory error/trace/handoff records are retained unchanged.
    """
    def __init__(self,execution,supports_transpose=False):
        self.execution=execution;self.closed=False;self.supports_transpose=supports_transpose
        self.release_ticket=0;self.native_released=False
        self.release_failure=None
        self.supports_loop=False;self.looping=False
        self.supports_chase=False
        self.supports_live_mutes=False;self.mute_failure=None

    @classmethod
    def prepare(cls,execution,document,*,transpose=0):
        library=execution.library;version=getattr(library,'midi_execution_file_schedule_version',None)
        if version is None or not execution.multi:return None
        version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=1:return None
        shift_marker=getattr(library,'midi_execution_file_transpose_version',None)
        if shift_marker is not None:shift_marker.argtypes=[];shift_marker.restype=ctypes.c_uint
        supports_transpose=shift_marker is not None and shift_marker()==1
        if transpose and not supports_transpose:return None
        if not document.events or len(document.events)>2000000 or any(len(event.data)>1024*1024 for event in document.events):return None
        # Original event rows (including text-only positions), ordering and RAW
        # frame boundaries survive; no STRUCT/tick/time conversion or batching.
        payload=b''.join(event.data for event in document.events)
        if len(payload)>64*1024*1024:return None
        rows=(_NativeFileRow*len(document.events))();offset=0
        for row,event in zip(rows,document.events):
            row.seconds=event.seconds;row.offset=offset;row.size=len(event.data);row.port=event.port
            offset+=len(event.data)
        for name,args,result in (
            ('prepare',[ctypes.c_void_p,ctypes.POINTER(_NativeFileRow),ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint,ctypes.c_double],ctypes.c_int),
            ('command',[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_double,ctypes.c_double,ctypes.c_int,ctypes.c_void_p],ctypes.c_int),
            ('snapshot',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_double),ctypes.POINTER(ctypes.c_uint64)],ctypes.c_uint)):
            function=getattr(library,'midi_execution_file_'+name);function.argtypes=args;function.restype=result
        wire=(ctypes.c_ubyte*len(payload)).from_buffer_copy(payload)
        with execution._lifetime,execution.borrow() as pointer:
            if getattr(execution,'_file_schedule',None) is not None:raise RuntimeError(tr('player.native_schedule_leased'))
            if not pointer or not library.midi_execution_file_prepare(pointer,rows,len(rows),wire,len(wire),document.duration):
                raise RuntimeError(tr('player.native_prepare_refused'))
            result=cls(execution,supports_transpose)
            chase_marker=getattr(library,'midi_execution_file_chase_version',None)
            if chase_marker is not None:
                chase_marker.argtypes=[];chase_marker.restype=ctypes.c_uint
                result.supports_chase=(chase_marker()==1 and getattr(execution.kernel,'_file_reset_requested',False)
                    and not getattr(execution.owner.performance,'_native_failure_compatibility',False))
            loop_marker=getattr(library,'midi_execution_file_loop_version',None)
            if loop_marker is not None:
                loop_marker.argtypes=[];loop_marker.restype=ctypes.c_uint
                if (loop_marker()==1 and getattr(execution.kernel,'_file_reset_requested',False)
                        and not getattr(execution.owner.performance,'_native_failure_compatibility',False)):
                    library.midi_execution_file_loop_configure.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint]
                    library.midi_execution_file_loop_configure.restype=ctypes.c_int
                    library.midi_execution_file_loop_snapshot.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint]
                    library.midi_execution_file_loop_snapshot.restype=ctypes.c_uint
                    result.supports_loop=True
            mute_marker=getattr(library,'midi_execution_file_mutes_version',None)
            if mute_marker is not None:
                mute_marker.argtypes=[];mute_marker.restype=ctypes.c_uint
                result.supports_live_mutes=mute_marker()==1
            execution._file_schedule=result;return result

    def set_mutes(self,muted):
        """File mask + targeted voice clear, preserving the native song clock.

        The additive local ABI shares the execution FIFO/receipt owner. It
        never pauses, resets, chases notes or changes external input filters.
        """
        if self.closed or not self.supports_live_mutes:raise RuntimeError(tr('player.native_command_refused'))
        values=(ctypes.c_ubyte*128)(*(int(ch in muted) for ch in range(128)))
        function=self.execution.library.midi_execution_file_mutes
        function.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint64)]
        function.restype=ctypes.c_int;ticket=ctypes.c_uint64()
        def completed(report):
            if report['phase']:self.mute_failure=(report['phase'],report['error'])
        with self.execution._lifetime,self.execution.borrow() as pointer:
            if not pointer or function(pointer,values,len(values),ctypes.byref(ticket))!=1:
                raise RuntimeError(tr('player.native_command_refused'))
            if ticket.value:self.execution._controls[ticket.value]=((),completed)

    def _command(self,command,index=0,position=0.,speed=1.,transpose=0,muted=None):
        if self.closed:raise RuntimeError(tr('player.native_owner_retired'))
        values=(ctypes.c_ubyte*128)(*(int(ch in (muted or ())) for ch in range(128)))
        with self.execution.borrow() as pointer:
            if not pointer or self.execution.library.midi_execution_file_command(pointer,command,index,position,speed,transpose,values)!=1:
                raise RuntimeError(tr('player.native_command_refused'))

    def _retire_prepared_release(self):
        if not self.release_ticket:return
        execution=self.execution;function=execution.library.midi_execution_file_release_retire
        with execution._lifetime,execution.borrow() as pointer:
            if function(pointer,self.release_ticket,0)!=1:raise RuntimeError(tr('player.native_receipts_pending'))
            execution._controls.pop(self.release_ticket);self.release_ticket=0

    def prepare_chase(self,packets,index,position,transpose,muted):
        """Copy a pure original chase plan before native Reset/RAW admission.

        No old NoteOn history is submitted. Every packet is an original
        non-note prefix message or a currently held/pedal-protected voice.
        The existing actor applies drum-aware transpose at execution time.
        """
        if not self.supports_chase or getattr(self.execution.owner.performance,'_native_failure_compatibility',False):return False
        self._retire_prepared_release()
        payload=b''.join(data for data,_port in packets)
        rows=(_NativeFileRow*len(packets))();offset=0
        for row,(data,port) in zip(rows,packets):
            row.seconds=0;row.offset=offset;row.size=len(data);row.port=port;offset+=len(data)
        wire=(ctypes.c_ubyte*len(payload)).from_buffer_copy(payload)
        values=(ctypes.c_ubyte*128)(*(int(ch in muted) for ch in range(128)))
        function=self.execution.library.midi_execution_file_chase_prepare
        function.argtypes=[ctypes.c_void_p,ctypes.POINTER(_NativeFileRow),ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint,
            ctypes.c_uint,ctypes.c_double,ctypes.c_int,ctypes.c_void_p]
        function.restype=ctypes.c_int
        with self.execution.borrow() as pointer:
            if not pointer or function(pointer,rows,len(rows),wire,len(wire),index,position,transpose,values)!=1:
                raise RuntimeError(tr('player.native_command_refused'))
        return True

    def _arm_release(self):
        execution=self.execution;library=execution.library
        marker=getattr(library,'midi_execution_file_release_version',None)
        if marker is None:return False
        marker.argtypes=[];marker.restype=ctypes.c_uint
        release_version=marker()
        if release_version not in (1,2):return False
        if release_version==2:
            failure_marker=getattr(library,'midi_execution_file_release_failure_version',None)
            if failure_marker is None:return False
            failure_marker.argtypes=[];failure_marker.restype=ctypes.c_uint
            if failure_marker()!=1:return False
        prepare=library.midi_execution_file_release_prepare;prepare.argtypes=[ctypes.c_void_p,ctypes.c_uint];prepare.restype=ctypes.c_uint64
        retire=library.midi_execution_file_release_retire;retire.argtypes=[ctypes.c_void_p,ctypes.c_uint64,ctypes.c_uint];retire.restype=ctypes.c_int
        with execution._lifetime,execution.borrow() as pointer:
            channels=execution.owner.addresses.count;ticket=int(prepare(pointer,channels))
            if not ticket:raise RuntimeError(tr('player.native_command_refused'))
            self.release_ticket=ticket
            def completed(report):
                # Native audio is finished. This callback owns metadata only;
                # never call the user's release function or resend its events.
                with execution.borrow() as owned:
                    if retire(owned,ticket,1)!=1:raise RuntimeError(tr('player.native_receipts_pending'))
                # ACK the exact completed control even if it was refused or
                # cancelled before execution. Do not retry audio or leave its
                # metadata callback stuck on this already-finished receipt.
                self.release_failure=(report['phase'],report['error']) if report['phase'] in (1,5,6) else None
                self.native_released=self.release_failure is None;self.release_ticket=0
            steps=tuple((channel,event,0) for channel in range(channels) for event in (15,76,18))
            execution._controls[ticket]=(steps,completed)
            return True

    def start(self,index,position,speed,transpose,muted,*,release=False,loop=False):
        self._retire_prepared_release();self.native_released=False;self.release_failure=None
        self.looping=bool(loop and self.supports_loop and not getattr(self.execution.owner.performance,'_native_failure_compatibility',False))
        if self.supports_loop:
            with self.execution.borrow() as pointer:
                if not pointer or self.execution.library.midi_execution_file_loop_configure(pointer,int(self.looping),29)!=1:
                    raise RuntimeError(tr('player.native_command_refused'))
        if release:self._arm_release()
        self._command(1,index,position,speed,transpose,muted)

    def loop_snapshot(self):
        if not self.supports_loop:return None
        values=(ctypes.c_uint64*6)()
        with self.execution.borrow() as pointer:
            if not pointer or self.execution.library.midi_execution_file_loop_snapshot(pointer,values,len(values))!=6:
                raise RuntimeError(tr('player.native_snapshot_unavailable'))
        return dict(enabled=bool(values[0]),iterations=int(values[1]),queued=int(values[2]),active=int(values[3]),phase=int(values[4]),error=ctypes.c_int64(values[5]).value)

    def _status(self):
        if self.closed:raise RuntimeError(tr('player.native_owner_retired'))
        position=ctypes.c_double();values=(ctypes.c_uint64*3)()
        with self.execution.borrow() as pointer:
            if not pointer or self.execution.library.midi_execution_file_snapshot(pointer,ctypes.byref(position),values)!=3:
                raise RuntimeError(tr('player.native_snapshot_unavailable'))
        state=int(values[1])
        if state==2 and self.release_failure is not None:state=3 # New facade on an older release image.
        return position.value,int(values[0]),state,int(values[2])

    def snapshot(self):
        if self.mute_failure is not None:raise RuntimeError(tr('player.native_command_refused'))
        result=self._status()
        if result[2]==3:
            # Status3 is additive and only produced by the qualified release
            # failure ABI. Internal status/pause/close must remain usable so
            # the exact receipt is ACKed and prepared resources can retire.
            self.pause_and_drain()
            function=getattr(self.execution.library,'midi_execution_file_release_failure',None)
            if function is not None:
                function.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint)];function.restype=ctypes.c_uint
                values=(ctypes.c_uint*2)()
                with self.execution.borrow() as pointer:
                    if pointer and function(pointer,values)==2:self.release_failure=(int(values[0]),ctypes.c_int(values[1]).value)
            raise RuntimeError(tr('player.native_command_refused'))
        return result[:3]

    def humanized_history(self):
        """Snapshot only after pause/drain;indices are original immutable rows."""
        library=self.execution.library
        fn=getattr(library,'midi_execution_file_history',None)
        if fn is None:return None
        fn.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,ctypes.POINTER(ctypes.c_uint)]
        fn.restype=ctypes.c_uint
        rows=(ctypes.c_uint*2048)();meta=(ctypes.c_uint*3)();result=[]
        with self.execution.borrow() as pointer:
            if not pointer:return None
            while True:
                count=fn(pointer,len(result),rows,len(rows),meta)
                if not meta[0]:return None
                result.extend(int(rows[i]) for i in range(count))
                if len(result)>=meta[2]:return int(meta[1]),tuple(result)
                if not count:raise RuntimeError(tr('player.native_command_refused'))

    def cancel_paused_attacks(self):
        fn=getattr(self.execution.library,'midi_execution_file_human_pause',None)
        if fn is None:return
        fn.argtypes=[ctypes.c_void_p];fn.restype=ctypes.c_int
        with self.execution.borrow() as pointer:
            if not pointer or not fn(pointer):raise RuntimeError(tr('player.native_command_refused'))
        return self.snapshot()[:2]

    def pause_and_drain(self,timeout=5.):
        self._command(0);deadline=time.monotonic()+timeout
        while True:
            position,index,_state,pending=self._status()
            if not pending:return position,index
            if time.monotonic()>=deadline:raise RuntimeError(tr('player.native_receipts_pending'))
            # Releases GIL. The existing NativeMidiClientFactory remains the ONLY
            # metadata consumer; this facade cannot steal read/replay/ACK.
            time.sleep(.001)

    def close(self):
        with self.execution._lifetime:
            if not self.closed:
                self._retire_prepared_release()
                self._command(2);self.closed=True
                if getattr(self.execution,'_file_schedule',None) is self:self.execution._file_schedule=None


def is_midi_system_reset(data):
    return ((len(data) == 6 and data[:1] == b"\xf0" and data[1] in (0x7e, 0x7f) and data[3] == 9 and data[4] in (1, 2, 3))
            or (len(data) >= 10 and data[:2] == b"\xf0\x41" and data[5:8] == b"\x40\x00\x7f")
            or data == b"\xf0\x43\x10\x4c\x00\x00\x7e\x00\xf7")


def silence(sender, channels=range(16)):
    # Pedals off and all-sound-off, not an undocumented voice-count hack.
    for ch in channels:
        address = MidiAddress.from_native(ch)
        for controller in (64, 66, 120):
            packet = bytes([0xb0 | address.channel, controller, 0])
            receipt=sender(packet, port=address.port) if address.port else sender(packet)
            WinMMMidiPipeServer.complete_submission(receipt)


class MidiTransport:
    # NativeFileSchedule is deliberately not used by offline
    # export. It preserves this class's exact preparation and receipt boundary.
    """Monotonic-clock scheduler. Qt refresh latency never schedules notes."""
    def __init__(self, sender, reset, panic=None, release=None, *, start_worker=True, prepare=None, is_drum=None, native_execution=None, native_eof_release=False):
        self.sender, self.reset = sender, reset
        self.prepare = prepare
        self.is_drum = is_drum or (lambda channel: channel % 16 == 9)
        self.panic = panic or (lambda channels=None: silence(self._submit, channels if channels is not None else
            range((max(self.document.ports) + 1) * 16) if self.document else range(16)))
        self.release = release or self.panic
        self.condition = threading.Condition()
        self.commands = deque()
        self.document = None
        self.event_times = ()
        self.state, self.position, self.speed, self.error = "stopped", 0.0, 1.0, ""
        self.loop, self.muted = False, frozenset()
        # Immutable publication: UI consumes EOF once, never infers EOF from
        # Stop/paused position or replays historical events to change order.
        self.completion = (0, None)
        self.chase_notes, self.transpose = False, 0
        self._paused_seek=False
        self.index, self.anchor = 0, time.perf_counter()
        self.closing = False
        self.pending_submission=None
        self._native_execution=native_execution;self._native_schedule=None
        self.native_eof_release=bool(native_eof_release)
        self.thread = threading.Thread(target=self._run, name="ABMSynth-MIDI-player", daemon=True)
        if start_worker:
            self.thread.start()

    def command(self, name, value=None):
        with self.condition:
            if name in ("seek", "speed", "transpose", "mute"):
                self.commands = deque((pending, item) for pending, item in self.commands if pending != name)
            self.commands.append((name, value)); self.condition.notify()

    def send(self, data, port=0):
        drums = self.is_drum(MidiAddress(port, data[0] & 15).native) if data and data[0] < 0xf0 else False
        data = transpose_midi(data, self.transpose, drums=drums)
        if data:
            return self._submit(data,port)

    def _submit(self,data,port=0):
        # Live playback, seek/panic and offline rendering share the same
        # retained suffix contract. Never submit a later event past a fault.
        if self.pending_submission is not None and not self.pending_submission.completed:
            raise RuntimeError(tr('runtime.channel_executor_retained'))
        receipt=self.sender(data,port=port) if port else self.sender(data)
        if callable(getattr(receipt,'resume',None)):self.pending_submission=receipt
        result=WinMMMidiPipeServer.complete_submission(receipt)
        self.pending_submission=None
        return result

    def snapshot(self):
        with self.condition:
            if self._native_schedule is not None and self.state=='playing':
                return self.state,self._native_schedule.snapshot()[0],self.speed,self.error
            position = self.position + (time.perf_counter()-self.anchor)*self.speed if self.state == "playing" else self.position
            if self.document:
                position = min(position, self.document.duration)
            return self.state, position, self.speed, self.error

    def close(self,timeout=5.0):
        self.command("close")
        if self.thread.ident is not None:self.thread.join(timeout)
        return (not self.thread.is_alive() and self.pending_submission is None and
            (self._native_schedule is None or self._native_schedule.closed))

    def stop_and_wait(self,timeout=5.0):
        done = threading.Event()
        self.command("barrier_stop", done)
        deadline=time.monotonic()+timeout
        while self.thread.is_alive() and not done.wait(min(.05,max(0,deadline-time.monotonic()))):
            if time.monotonic()>=deadline:return False
        if self.pending_submission is not None:return False
        if self._native_schedule is not None and not self._native_schedule.closed:
            try:_position,_index,state,pending=self._native_schedule._status()
            except RuntimeError:return False
            if state==1 or pending:return False
        return True

    def _prepare(self, position, sounding):
        doc = self.document
        if self.prepare:
            self.prepare(doc)
        index,packets=self._chase_plan(position,sounding)
        if self._native_schedule is not None and self._native_schedule.prepare_chase(packets,index,position,self.transpose,self.muted):
            self.index=index;self.position=position;self.anchor=time.perf_counter();return
        self.reset()
        for data,port in packets:self.send(data,port)
        self.index=index;self.position=position;self.anchor=time.perf_counter()

    def _chase_plan(self,position,sounding,*,cursor=None,resume=False):
        # One pure compiler serves the source fallback and the native actor.
        # Dict insertion order, overlap FIFO, sustain/sostenuto and system
        # reset clearing are the original _prepare algorithm, not a new chase.
        doc=self.document;packets=[];restoring=False
        def append(data,port):
            if not resume or restoring:packets.append((data,port))
        index = bisect.bisect_left(self.event_times, position) if cursor is None else cursor
        held, sustained, pedals, sostenuto = {}, {}, {}, {}
        captured = set()
        def protected(key):
            return pedals.get(key[0], False) or (sostenuto.get(key[0], False) and key in captured)
        def release_held(key):
            voices = held.pop(key, [])
            if voices and protected(key):
                sustained[key] = voices[-1]
        history=self._native_schedule.humanized_history() if resume and self._native_schedule is not None else None
        events=doc.events[:index] if history is None else (*doc.events[:history[0]],*(doc.events[i] for i in history[1]))
        for event in events:
            data = event.data
            if not data:
                continue
            status, ch = data[0] & 0xf0, event.channel
            if data[0] >= 0xf0:
                append(data, event.port)
                # Only system-reset messages invalidate prior note chase.
                if ((len(data) == 6 and data[1] in (0x7e, 0x7f) and data[3] == 9)
                    or (len(data) >= 10 and data[1] == 0x41 and data[5:8] == b"\x40\x00\x7f")
                    or data == b"\xf0\x43\x10\x4c\x00\x00\x7e\x00\xf7"):
                    held.clear(); sustained.clear(); pedals.clear(); sostenuto.clear(); captured.clear()
            elif status in (0x80, 0x90):
                key = (ch, data[1])
                if status == 0x90 and data[2]:
                    held.setdefault(key, []).append(data[2])
                else:
                    voices = held.get(key, [])
                    if voices:
                        velocity = voices.pop(0)
                        if not voices:
                            held.pop(key)
                            if protected(key):
                                sustained[key] = velocity
            else:
                append(data, event.port)
                if status == 0xb0:
                    if data[1] == 64:
                        pedals[ch] = data[2] >= 64
                        if data[2] < 64:
                            sustained = {key: value for key, value in sustained.items() if key[0] != ch or protected(key)}
                    elif data[1] == 66:
                        enabled = data[2] >= 64
                        if enabled and not sostenuto.get(ch, False):
                            captured.update(key for key in held if key[0] == ch)
                        sostenuto[ch] = enabled
                        if not enabled:
                            captured.difference_update(tuple(key for key in captured if key[0] == ch))
                            sustained = {key: value for key, value in sustained.items() if key[0] != ch or protected(key)}
                    elif data[1] == 121:
                        pedals[ch] = sostenuto[ch] = False
                        captured.difference_update(tuple(key for key in captured if key[0] == ch))
                        sustained = {key: value for key, value in sustained.items() if key[0] != ch}
                    elif data[1] in (123, 124, 125, 126, 127):
                        for key in tuple(held):
                            if key[0] == ch:
                                release_held(key)
                    elif data[1] == 120:
                        held = {key: value for key, value in held.items() if key[0] != ch}
                        sustained = {key: value for key, value in sustained.items() if key[0] != ch}
        restoring=True
        if resume:
            # Pause clears pedals as well as voices. Restore only their final
            # state for future notes;this never requires past Note Ons.
            for ch,enabled in pedals.items():
                append(bytes([0xb0 | (ch % 16),64,127 if enabled else 0]),ch//16)
            if not sounding:
                for ch,enabled in sostenuto.items():
                    append(bytes([0xb0 | (ch % 16),66,127 if enabled else 0]),ch//16)
        if sounding:
            # Restore captured notes BEFORE the sostenuto edge; replaying them
            # afterward would not capture them and they would drop immediately.
            for ch in sostenuto:
                if sostenuto.get(ch):
                    append(bytes([0xb0 | (ch % 16), 66, 0]), ch // 16)
            chased = {**{key: [value] for key, value in sustained.items()}, **held}
            def start_note(key):
                ch, note = key
                if ch not in self.muted:
                    for velocity in chased[key]:
                        append(bytes([0x90 | (ch % 16), note, velocity]), ch // 16)
            for key in chased:
                if key in captured:
                    start_note(key)
            for ch in sostenuto:
                if sostenuto.get(ch):
                    append(bytes([0xb0 | (ch % 16), 66, 127]), ch // 16)
            for key in chased:
                if key not in captured:
                    start_note(key)
            for ch, note in sustained:
                if (ch, note) not in held and ch not in self.muted:
                    append(bytes([0x80 | (ch % 16), note, 0]), ch // 16)
        return index,tuple(packets)

    def _resume(self, *, sounding=False):
        # Pause silences voices/pedals but preserves all other engine state.
        # Keep the exact cursor (including same-time rows). Do not Reset or
        # replay the full historical CC/SysEx automation prefix on resume.
        # Plain resume is continuation,not a locate/chase command. Only the
        # caller's explicit paused seek may request sounding=True.
        _index,packets=self._chase_plan(self.position,sounding,cursor=self.index,resume=True)
        for data,port in packets:self.send(data,port)
        self.anchor=time.perf_counter()

    def _change_transpose(self, value):
        """Change file pitch without Reset, historical replay or time rebisect.

        The caller has drained an active native schedule at its exact cursor.
        Reuse the existing chase-note preference for current melodic voices;
        leave drum voices and non-pedal controller/program state alone.
        """
        if self.document is None or self.state!='playing':
            self.transpose=value
            return
        if self._native_schedule is None:self.position=self.snapshot()[1]
        channels=frozenset(channel for channel,_count in self.document.channels if not self.is_drum(channel))
        # Compile before clearing voices; only the processed prefix belongs
        # here, even when several source rows share the same timestamp.
        _index,packets=self._chase_plan(self.position,True,cursor=self.index,resume=True)
        if channels:self.panic(channels)
        self.transpose=value
        for data,port in packets:
            channel=MidiAddress(port,data[0]&15).native
            if channel in channels and (self.chase_notes or data[0]&0xf0==0xb0):
                self.send(data,port)
        self.anchor=time.perf_counter()

    def _refresh_native_schedule(self):
        # Engine restart retires the schedule, not the loaded document.
        # Reprepare AFTER original Reset/chase on the next play checkpoint.
        executor=self._native_execution() if callable(self._native_execution) else None
        schedule=self._native_schedule
        if schedule is not None and schedule.closed:self._native_schedule=schedule=None
        if schedule is not None and (schedule.execution is not executor or self.transpose and not schedule.supports_transpose):
            schedule.pause_and_drain();schedule.close();self._native_schedule=schedule=None
        if self.document is not None and executor is not None and schedule is None:
            self._native_schedule=NativeFileSchedule.prepare(executor,self.document,transpose=self.transpose)

    def _run(self):
        try:
            while not self.closing:
                with self.condition:
                    command = self.commands.popleft() if self.commands else None
                if command:
                    name, value = command
                    # Decide the toggle on the serialized transport, not on a
                    # potentially stale GUI snapshot. Repeated explicit Play
                    # is not a request to restart/pre-roll a playing file.
                    if name == 'toggle':
                        name = 'pause' if self.state == 'playing' else 'play'
                    if name == 'play' and self.state == 'playing':
                        continue
                    if name=='transpose':
                        value=max(-24,min(24,int(value)))
                        if value==self.transpose:continue
                    # Serialize controls after already admitted original RAW.
                    # Python preparation remains an explicit Reset/chase checkpoint.
                    if self._native_schedule is not None and self._native_schedule.closed:self._native_schedule=None
                    if name=='mute' and self._native_schedule is not None and self._native_schedule.supports_live_mutes:
                        muted=frozenset(value)
                        if muted!=self.muted:self._native_schedule.set_mutes(muted)
                        self.muted=muted
                        continue
                    if self._native_schedule is not None and name!='chase_notes' and not (name=='transpose' and self.state!='playing'):
                        self.position,self.index=self._native_schedule.pause_and_drain()
                        self.anchor=time.perf_counter()
                        if name=='transpose' and int(value)!=0 and not self._native_schedule.supports_transpose:
                            self._native_schedule.close();self._native_schedule=None
                    if name == "close":
                        self.closing = True; break
                    if name == "barrier_stop":
                        self.state, self.position, self.index = "stopped", 0, 0
                        self._paused_seek=False
                        self.panic()
                        value.set()
                        continue
                    if name == "load":
                        if self.document is not None and self.state in ("playing", "paused"):
                            self.panic()
                        self.document, self.state, self.position, self.index, self.error = value, "stopped", 0, 0, ""
                        self._paused_seek=False
                        self.event_times = tuple(event.seconds for event in value.events) if value else ()
                        if self._native_schedule is not None:self._native_schedule.close();self._native_schedule=None
                        executor=self._native_execution() if callable(self._native_execution) else None
                        if value is not None and executor is not None:
                            self._native_schedule=NativeFileSchedule.prepare(executor,value,transpose=self.transpose)
                    elif name == "mute":
                        self.panic(value - self.muted)
                        self.muted = frozenset(value)
                    elif name == "loop":
                        self.loop = bool(value)
                    elif name == "chase_notes":
                        self.chase_notes = bool(value)
                    elif name == "transpose":
                        self._change_transpose(value)
                    elif name == "speed":
                        self.position = self.snapshot()[1]
                        self.anchor = time.perf_counter()
                        self.speed = max(.25, min(4.0, float(value)))
                    elif self.document:
                        if name == "pause":
                            self.position = self.snapshot()[1]
                            self._paused_seek=False
                            if self._native_schedule is not None:
                                stopped=self._native_schedule.cancel_paused_attacks()
                                if stopped is not None:self.position,self.index=stopped
                            self.state = "paused"; self.panic()
                        elif name == "stop":
                            self._paused_seek=False
                            self.state = "stopped"; self._prepare(0, False)
                        elif name == "seek":
                            self._prepare(max(0.0, min(self.document.duration, float(value))), self.state == "playing" and self.chase_notes)
                            self._paused_seek=self.state=='paused'
                        elif name == "play":
                            if self.state == 'paused' and self.position < self.document.duration:
                                # A locate while paused is an explicit seek.
                                # Only that action may restore historical notes.
                                self._resume(sounding=self._paused_seek and self.chase_notes)
                            else:
                                if self.position >= self.document.duration:self.position = 0
                                self._prepare(self.position,self.chase_notes)
                            self._paused_seek=False
                            self.state = "playing"
                            self._refresh_native_schedule()
                    if self._native_schedule is not None and self.state=='playing' and name!='chase_notes':
                        self._native_schedule.start(self.index,self.position,self.speed,self.transpose,self.muted,
                            release=self.native_eof_release and not self.loop,loop=self.loop)
                    continue
                if self.state != "playing" or not self.document:
                    with self.condition:
                        if not self.commands:
                            self.condition.wait()
                    continue
                if self._native_schedule is not None:
                    now,index,status=self._native_schedule.snapshot();self.index=index
                    if status!=2:
                        with self.condition:
                            if not self.commands:self.condition.wait(.02)
                        continue
                    # EOF release/loop preparation is intentionally a remaining
                    # checkpoint, not misreported as fully native file transport.
                    self._native_schedule.pause_and_drain()
                    if self.loop and self.document.duration>0:
                        self.panic();self._prepare(0,self.chase_notes)
                        self._native_schedule.start(self.index,self.position,self.speed,self.transpose,self.muted)
                    else:
                        if not self._native_schedule.native_released:self.release()
                        self.position,self.state=self.document.duration,'stopped'
                        self.completion=(self.completion[0]+1,self.document)
                    continue
                now = self.snapshot()[1]
                while self.index < len(self.document.events) and self.document.events[self.index].seconds <= now:
                    with self.condition:
                        if self.commands:
                            break  # A pending seek/stop precedes overdue notes.
                    event = self.document.events[self.index]
                    data = event.data
                    if data and not (data[0] & 0xf0 == 0x90 and event.channel in self.muted and data[-1]):
                        self.send(data, event.port)
                    self.index += 1
                if now >= self.document.duration:
                    if self.loop and self.document.duration > 0:
                        self.panic()
                        self._prepare(0, self.chase_notes)
                    else:
                        self.release()
                        self.position, self.state = self.document.duration, "stopped"
                        self.completion=(self.completion[0]+1,self.document)
                    continue
                next_time = self.document.events[self.index].seconds if self.index < len(self.document.events) else self.document.duration
                with self.condition:
                    if not self.commands:
                        self.condition.wait(max(.0005, min(.02, (next_time-now)/self.speed)))
        except Exception as exc:
            from error_reporting import record
            import traceback
            self.failure_traceback=''.join(traceback.format_exception(exc))
            record('midi_player_failure',traceback=self.failure_traceback)
            self.error, self.state = str(exc), "stopped"
        finally:
            try:
                if self._native_schedule is not None:
                    self._native_schedule.pause_and_drain();self._native_schedule.close();self._native_schedule=None
                if self.document is not None:
                    self.panic()
            except Exception as exc:
                self.error = str(exc)
            # A failed native call must not leave a GUI teardown barrier stuck.
            with self.condition:
                for name, value in self.commands:
                    if name == "barrier_stop":
                        value.set()


# Original audition phrases,not excerpts from a third-party composition.
# Each family has a deliberate register/articulation in every bank.
@lru_cache(maxsize=257)
def instrument_audition_phrase(bank,program,*,drum=False):
    """Original eight-bar studies,selected by Program even in variation banks.

    Seconds are source data,not GUI scheduling. No Program/CC messages are
    embedded,so the selected instrument's settings survive native repetition.
    """
    family=16 if drum else max(0,min(127,int(program)))//8
    beat=60/116;bar=beat*4;rows=[]
    if drum:
        for measure in range(8):
            start=measure*bar
            for eighth in range(8):
                rows.append((start+eighth*beat/2,.09,46 if eighth==7 and measure%2 else 42,
                             88 if eighth%2==0 else 57+measure%3*5))
            for pulse in (0,1.5,2,3.5) if measure%2 else (0,2,2.75):
                rows.append((start+pulse*beat,.12,36,104 if pulse in (0,2) else 83))
            for pulse in (1,3):rows.append((start+pulse*beat,.13,38,108 if pulse==3 else 95))
            if measure in (0,4):rows.append((start,.5,49,104))
            if measure in (3,7):
                for j,key in enumerate((38,48,45,41)):
                    rows.append((start+(3+j*.25)*beat,.12,key,80+j*9))
        return tuple(sorted(rows)),16
    roots=(0,5,9,7,0,5,7,0)
    melody=((0,4,7,12,11,7,4,2),(4,7,9,12,9,7,5,4),
            (0,7,12,14,12,7,4,2),(7,11,14,12,11,7,5,2))
    for measure,shift in enumerate(roots):
        start=measure*bar
        accent=5 if measure in (0,4,7) else -3
        def add(at,length,key,velocity):
            if shift==9 and (key-shift)%12 in (4,11):key-=1
            rows.append((start+at*beat,min(length,3.98-at)*beat,max(0,min(127,key)),max(1,min(127,velocity+accent))))
        if family in (2,6,11):
            keys=(48+shift,60+shift,64+shift,67+shift)
            for j,key in enumerate(keys):add(j*.018,3.65 if family!=11 else 3.9,key,78+j*3)
            if family!=11:
                for pulse,note in ((2.5,72),(3.25,71)):add(pulse,.65,note+shift,93)
        elif family==3:
            for pulse in (0,1.5,2.5):
                for j,key in enumerate((48,55,60,64,67)):
                    add(pulse+j*.045,.95 if pulse!=2.5 else 1.35,key+shift,76+j*4)
            add(3.5,.3,72+shift,94)
        elif family==4:
            for at,length,interval,velocity in ((0,.65,0,109),(.75,.2,0,76),(1,.45,7,96),
                    (1.75,.2,12,88),(2,.65,0,106),(2.75,.2,10,83),(3,.35,7,96),(3.5,.35,2,89)):
                add(at,length,36+shift+interval,velocity)
        elif family in (5,8,9):
            base=72 if family==9 else 60
            notes=melody[measure%4]
            for j,(at,length) in enumerate(((0,1.35),(1.5,.45),(2,.85),(3,.45),(3.5,.38))):
                add(at,length,base+shift+notes[(j+measure)%8],80+j*4)
        elif family==7:
            for at,length,index,velocity in ((0,.5,0,109),(.75,.22,0,87),(1,.3,2,105),
                    (1.5,.25,1,99),(2,1.1,3,116),(3.5,.35,2,95)):
                add(at,length,60+shift+(0,4,7,12)[index],velocity)
        elif family in (12,15):
            for at,length,key,velocity in ((0,1.35,48,65),(1.75,.65,67,99),(3, .8,72,112)):
                add(at,length,key+shift,velocity)
        elif family==10:
            for at,length,interval,velocity in ((0,.65,0,105),(.75,.2,7,88),(1,.2,12,112),
                    (1.5,.35,10,99),(2,.2,7,107),(2.25,.2,3,86),(2.75,.2,0,103),
                    (3,.2,-2,91),(3.5,.35,0,112)):
                add(at,length,60+shift+interval,velocity)
        elif family==13:
            for at,length,interval,velocity in ((0,.4,0,98),(.5,.2,7,82),(.75,.15,12,108),
                    (1,.15,14,91),(1.25,.15,12,103),(1.5,.35,7,92),(2,.7,5,105),
                    (3,.2,4,88),(3.25,.2,2,97),(3.5,.4,0,109)):
                add(at,length,60+shift+interval,velocity)
        elif family==14:
            for at,interval,velocity in ((0,0,110),(.5,0,75),(.75,7,89),(1.5,0,106),
                    (2,12,115),(2.5,7,86),(3,4,103),(3.5,0,80),(3.75,7,94)):
                add(at,.16,60+shift+interval,velocity)
        else:
            base=72 if family==1 else 60
            for j,interval in enumerate(melody[measure%4]):
                add(j*.5,.18 if family in (1,14) else .38,base+shift+interval,
                    (101 if j%2==0 else 76)+j%3*3)
            if family==0:
                add(0,1.65,48+shift,91)
                for key in (60,64,67):add(2,1.5,key+shift,83)
    return tuple(sorted(rows)),family


# Each admitted output effect has an explicit listening task. Sharing a task
# is deliberate; changing only Program while reusing a generic phrase is not.
EFFECT_AUDITION_SCENARIOS={
    None:'ensemble','output':'ensemble','reverb':'space','chorus':'motion','delay':'echo',
    'equalizer':'tone','other':'stereo','dynamics':'dynamics',
    'flanger':'motion','phaser':'motion','auto_wah':'tone','chorus_fx':'motion',
    'freeverb':'space','distortion':'drive','echo':'echo','gargle':'motion',
    'compressor_fx':'dynamics','auto_gain':'dynamics','rotate':'stereo',
    'band_pass':'tone','notch':'tone','all_pass':'tone','pitch_shift':'pitch',
    'tremolo':'motion','auto_pan':'stereo','ring_mod':'pitch',
    'bitcrusher':'fidelity','saturator':'drive','gate':'dynamics',
    'dc_block':'ensemble','stereo_tools':'stereo','headphone_stage':'stereo',
    'convolution':'space','gain':'ensemble','channel_matrix':'stereo',
    'volume_envelope':'envelope','chorus_dx8':'motion','distortion_dx8':'drive',
    'echo_dx8':'echo','reverb_dx8':'space','parametric_eq':'tone',
    'low_pass':'tone','high_pass':'tone','low_shelf':'tone','high_shelf':'tone',
}


@lru_cache(maxsize=12)
def effect_audition_arrangement(scenario):
    """Original16-bar music/diagnostic studies at116BPM,not a known song.

    Tracks:(channel,GM Program,pan,volume,drum). Notes:(seconds,duration,key,
    velocity,channel). No audio effect settings or live-channel writes.
    Space/echo include empty bars for tails;other studies use sustained,
    dynamic,wide-register or spatially separated material as appropriate.
    """
    programs={
        'ensemble':(0,24,33,48,73), 'space':(0,10,32,48,12),
        'echo':(12,28,32,48,10), 'motion':(16,48,33,24,73),
        'tone':(16,61,33,0,73), 'drive':(29,30,34,16,80),
        'dynamics':(0,24,33,48,12), 'stereo':(24,73,33,48,10),
        'pitch':(71,16,32,48,73), 'envelope':(16,10,32,48,73),
        'fidelity':(80,10,33,0,12),
    }
    selected=programs[scenario]
    pans=(20,108,64,82,44) if scenario=='stereo' else (48,82,64,72,56)
    # Reference music should use ordinary performance levels. Earlier custom
    # CC7 values95/80/92/55/78,plus drums65,made the ensemble about6dB quieter
    # in the qualified font. Preserve the score's intentional velocity dynamics
    # and its FX gain response;do not add a normalizer or output makeup gain.
    volumes=(78,70,84,55,78) if scenario=='drive' else (127,110,120,95,110)
    tracks=tuple((ch,prog,pans[ch],volumes[ch],False) for ch,prog in enumerate(selected))
    if scenario in ('ensemble','tone','drive','dynamics','stereo','fidelity'):
        tracks+=((9,0,64,65 if scenario=='drive' else 110,True),)
    notes=[];beat=60/116
    roots=(48,53,57,55,48,53,50,55,48,52,53,55,57,53,55,48)
    for bar,root in enumerate(roots):
        minor=bar in (2,6,12)
        chord=(0,3 if minor else 4,7)
        def add(ch,at,length,key,velocity):
            length=min(length,4-at-.015)
            if length>0:notes.append(((bar*4+at)*beat,length*beat,key,max(1,min(127,round(velocity))),ch))
        if scenario in ('space','echo'):
            # A complete silent second bar exposes reflections/repeats.
            if bar%2:continue
            if scenario=='space':
                for interval in chord:add(0,0,.75,root+12+interval,88)
                add(1,1.5,.15,root+24,92);add(4,2.25,.12,root+19,74)
            else:
                for at,iv in ((0,12),(1.5,19),(2.75,24)):add(0,at,.12,root+iv,98)
                add(1,.75,.10,root+7,82)
            add(2,0,.45,root-12,79)
            continue
        if scenario=='motion':
            for interval in chord:add(0,0,3.9,root+12+interval,79)
            add(1,0,3.9,root+24,72);add(2,0,3.8,root-12,83)
            for at,iv in ((0,12),(2,19)):add(3,at,1.8,root+iv,86)
            continue
        if scenario=='pitch':
            for j,iv in enumerate((12,14,16 if not minor else 15,19,24,19)):
                add(0,j*.625,.55,root+iv,92)
            add(1,0,3.8,root-12,64)
            continue
        if scenario=='envelope':
            for interval in chord:add(0,0,3.8,root+12+interval,90)
            add(1,0,.18,root+24,85)
            continue
        if scenario=='drive':
            for at,length in ((0,.65),(.75,.2),(1.5,.3),(2,1.2),(3.5,.35)):
                for iv in (0,7,12):add(0,at,length,root+iv,112 if at in (0,2) else 70)
            for at in (1,3):add(1,at,.35,root+19,95)
        elif scenario=='tone':
            for j,key in enumerate((root-12,root,root+12,root+24)):
                add(0,j*.75,1.5,key,94)
            for j,iv in enumerate((12,16,19,24)):add(1,j,.75,root+iv,88)
        elif scenario=='fidelity':
            add(0,0,3.8,root+12,85)
            for j in range(8):add(1,j*.5,.12,root+24+(0,7,4,12)[j%4],98 if j%2==0 else 35)
            for j in range(4):add(3,j,.3,root+chord[j%3],42)
        else:
            scale=(.28,.50,.78,1.0)[bar%4] if scenario=='dynamics' else 1
            for j,iv in enumerate((12,16 if not minor else 15,19,24,23,19,16 if not minor else 15,14)):
                add(0,j*.5,.36,root+iv,(96 if j%2==0 else 70)*scale)
            for at in (0,1.5,3):
                for iv in chord:add(1,at,.65,root+12+iv,74*scale)
            if scenario=='ensemble':
                for iv in chord:add(3,0,3.8,root+12+iv,58)
                for at,iv in ((0,24),(1.5,23),(2.5,19),(3.5,16 if not minor else 15)):
                    add(4,at,.4,root+iv,85)
        scale=(.28,.5,.78,1.)[bar%4] if scenario=='dynamics' else 1
        for at,iv in ((0,-12),(1.5,-5),(2,-12),(3,-8)):
            add(2,at,.65,root+iv,94*scale)
        if any(track[0]==9 for track in tracks):
            for j in range(8):add(9,j*.5,.08,42,((78 if j%2==0 else 48)*scale))
            for at in (0,2):add(9,at,.12,36,100*scale)
            for at in (1,3):add(9,at,.12,38,98*scale)
    return tracks,tuple(sorted(notes)),64*beat



@dataclass(frozen=True)
class ExportSnapshot:
    preferences: object
    group: object
    effects: object
    performance: dict
    midi_cc: dict
    channel_policy: dict
    source_assets: tuple = ()

    def __post_init__(self):
        # Caller dictionaries are live state. Capture before publication to the
        # spawned process so a later UI edit cannot mutate this export request.
        for name in ("performance", "midi_cc", "channel_policy"):
            object.__setattr__(self, name, dict(getattr(self, name)))
        object.__setattr__(self, 'source_assets', tuple(json.loads(json.dumps(item)) for item in self.source_assets))


def file_identity(path, cancelled=None):
    """Worker-only streaming identity; no SoundFont/IR write or GUI hashing.

    A hash identifies these observed bytes, not an unverified library maximum
    or a security lock on a file that another process might replace later.
    """
    from soundfont_files import asset_file_identity
    return asset_file_identity(path,cancelled)


def export_manifest(document, snapshot, container, speed, muted, tail, options, cancelled=None):
    """Concrete render inputs, not display labels or mutable widget handles."""
    root = Path(__file__).resolve().parent
    assets = {snapshot.group.font_path(entry).resolve() for entry in snapshot.group.fonts}
    from stream_effects import audio_fx_neutral
    # A saved but bypassed/mathematically-neutral node does not consume its IR.
    # Missing inactive assets must not turn a valid export into a failure.
    if snapshot.effects.audio_fx_enabled:
        assets.update(Path(slot.asset).resolve() for slot in snapshot.effects.audio_fx
                      if slot.enabled and slot.asset and not audio_fx_neutral(slot))
    from soundfont_files import soundfont_asset_identity
    from bass_bindings import bass, BASS_CONFIG_MIDI_SFZHEAD
    pointer = bass.BASS_GetConfigPtr(BASS_CONFIG_MIDI_SFZHEAD)
    sfz_header = ctypes.string_at(pointer) if pointer else b''
    asset_identities, graphs, unqualified = {}, {}, []
    for path in sorted(assets):
        identity = soundfont_asset_identity(path, cancelled, sfz_header=sfz_header)
        if 'resolution' in identity:
            graphs[str(path)] = identity['resolution']
            if not identity['resolution']['qualified']: unqualified.append(str(path))
            for dependency in identity['dependencies']:
                asset_identities[dependency['path']] = dependency
        asset_identities[str(path)] = {key:identity[key] for key in ('path','bytes','mtime_ns','sha256')}
    # A live-loaded font/IR is a specific generation, not whatever a pathname
    # contains later. Compare the captured identities in the worker; no GUI
    # hashing, no silent switch to newly edited samples on export.
    for source in snapshot.source_assets:
        if 'resolution' in source and graphs.get(source['path']) != source['resolution']:
            raise RuntimeError(tr('player.asset_changed',path=source['path']))
        for captured in (source,*source.get('dependencies',())):
            if captured['path'] not in asset_identities: continue
            if asset_identities[captured['path']]['sha256'] != captured['sha256']:
                raise RuntimeError(tr('player.asset_changed',path=captured['path']))
    config = dict(preferences=asdict(snapshot.preferences), group=asdict(snapshot.group),
                  effects=asdict(snapshot.effects), performance=snapshot.performance,
                  midi_cc=snapshot.midi_cc, channel_policy=snapshot.channel_policy,
                  source_assets=snapshot.source_assets,
                  container=container, speed=speed, muted=sorted(muted), tail=tail,
                  render_options=asdict(options))
    encoded = json.dumps(config, sort_keys=True, separators=(',', ':'),
                         default=lambda item: sorted(item) if isinstance(item, (set, frozenset)) else str(item))
    timeline = json.dumps(dict(duration=document.duration, tracks=document.tracks,
        channels=document.channels, events=[(event.seconds,event.data.hex(),event.text,event.kind,event.port)
                                            for event in document.events]),
        sort_keys=True,separators=(',',':')).encode('utf-8')
    return dict(schema=1, scope='captured inputs and digital export QC; not physical sound certification',
                reproduction=dict(qualified=not unqualified, unqualified_assets=unqualified,
                    basis='contained SF2/IR bytes and qualified filesystem SFZ dependency graphs'),
                asset_graphs=graphs,
                midi=dict(path=document.path, sha256=document.source_sha256, bytes=document.source_bytes,
                          timeline_sha256=hashlib.sha256(timeline).hexdigest()),
                configuration=json.loads(encoded), configuration_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                assets=[asset_identities[path] for path in sorted(asset_identities)],
                runtime=[file_identity(path,cancelled) for path in sorted((*root.glob('*.py'), *root.glob('*.dll')))])


def inspect_encoded_output(bass, path, container, expected_frames, channels, rate, cancel):
    """Full native decode where a decoder exists; explicit codec qualification.

    Local bass.h GetData/CHANNELINFO ABI. Header length alone cannot establish
    the decoded length (notably MP3); consume PCM to the documented ENDED code.
    No new decoder/FFmpeg runtime dependency and no peak normalization here.
    """
    import numpy as np
    from bass_bindings import BASS_CHANNELINFO, BASS_SAMPLE_FLOAT, BASS_STREAM_DECODE, BASS_UNICODE
    identity = file_identity(path)
    if not identity['bytes']:
        raise RuntimeError(tr('player.export_qc_failed'))
    with Path(path).open('rb') as source:
        magic = source.read(4)
    required_magic = {'wav':(b'RIFF',b'RF64'), 'aiff':(b'FORM',),
                      'flac':(b'fLaC',), 'ogg_flac':(b'OggS',),
                      'vorbis':(b'OggS',), 'opus':(b'OggS',)}
    if container in required_magic and magic not in required_magic[container]:
        raise RuntimeError(tr('player.export_qc_failed'))
    stream = bass.BASS_StreamCreateFile(0, str(path), 0, 0,
                                       BASS_SAMPLE_FLOAT | BASS_STREAM_DECODE | BASS_UNICODE)
    if not stream:
        code = bass.BASS_ErrorGetCode()
        if container in ('flac', 'ogg_flac', 'opus') and code in (41, 44):
            # Encoder support is NOT decoder support. Do not call this a full
            # codec decode test when no corresponding decoder is installed.
            return dict(identity=identity, decoder='unavailable', code=code)
        raise RuntimeError(tr('player.decode_failed', error=code))
    try:
        info = BASS_CHANNELINFO()
        if not bass.BASS_ChannelGetInfo(stream, ctypes.byref(info)) or info.chans != channels:
            raise RuntimeError(tr('player.export_qc_failed'))
        data = np.empty((4096, info.chans), np.float32)
        frames, peak = 0, 0.0
        while not cancel.is_set():
            count = bass.BASS_ChannelGetData(stream, data.ctypes.data, data.nbytes)
            if count == 0xffffffff:
                if bass.BASS_ErrorGetCode() != 45:  # bass.h BASS_ERROR_ENDED
                    raise RuntimeError(tr('player.decode_failed', error=bass.BASS_ErrorGetCode()))
                break
            if not count: break
            if count % (4 * info.chans):
                raise RuntimeError(tr('player.export_qc_failed'))
            block = data.reshape(-1)[:count//4]
            if not np.isfinite(block).all():
                raise RuntimeError(tr('player.export_qc_failed'))
            frames += count // (4 * info.chans)
            peak = max(peak, float(np.max(np.abs(block))))
        if cancel.is_set(): return None
        if (not frames or info.chans != channels or
            container in ('wav', 'aiff', 'vorbis') and (info.freq != rate or frames != expected_frames)):
            raise RuntimeError(tr('player.export_qc_failed'))
        return dict(identity=identity, decoder='complete', decoded_frames=frames,
                    decoded_rate=info.freq, channels=info.chans, decoded_peak=peak,
                    expected_source_frames=expected_frames, source_rate=rate,
                    duration_error_s=frames/info.freq-expected_frames/rate)
    finally:
        bass.BASS_StreamFree(stream)


def transpose_midi(data, semitones, *, drums=None):
    """File-only melodic key shift; Note Off/poly pressure use the same key.

    Standard percussion channel10 is preserved. Out-of-range notes are dropped,
    never clamped into a shared key that could leave another note stuck.
    """
    if drums is None:
        drums = bool(data and data[0] & 15 == 9)
    if semitones and len(data) == 3 and data[0] & 0xf0 in (0x80, 0x90, 0xa0) and not drums:
        key = data[1] + int(semitones)
        return bytes((data[0], key, data[2])) if 0 <= key <= 127 else b""
    return data


@dataclass(frozen=True)
class MidiRenderOptions:
    start: float = 0.0
    end: float | None = None
    transpose: int = 0
    mono: bool = False
    normalize: bool = False
    peak_db: float = -1.0
    align_processing: bool = False

    def validate(self, document):
        end = document.duration if self.end is None else self.end
        if (not 0 <= self.start <= end <= document.duration or
                not -24 <= self.transpose <= 24 or not -12 <= self.peak_db <= 0 or
                not isinstance(self.align_processing,bool)):
            raise ValueError(tr("player.export_options_invalid"))
        return replace(self, end=end)


class ExportEndpointEvidence:
    """Bounded offline encoder-input observation, never an inferred FX tail.

    The selected duration remains authoritative. Silence in this finite window
    cannot prove that a later echo or an oscillator has finished. This helper
    is used after mono conversion/normalization, so evidence describes exactly
    the PCM fed to the encoder rather than a different pre-output signal.
    """

    def __init__(self, sample_rate, channels, body_frames):
        import numpy as np
        self.rate, self.channels = int(sample_rate), int(channels)
        self.body_frames = max(0, int(body_frames))
        self.frames = self.tail_frames = 0
        self.tail_peak = self.tail_sum_squares = 0.0
        # Measurement policy, not a DSP/library limit or audibility criterion.
        self.window_frames = max(1, round(self.rate * .1))
        self.endpoint = np.empty((self.window_frames, self.channels), np.float32)
        self.used = 0

    def feed(self, signal):
        import numpy as np
        block = np.asarray(signal).reshape(-1, self.channels)
        if not len(block):
            return
        tail = block[max(0, min(len(block), self.body_frames - self.frames)):]
        if len(tail):
            self.tail_frames += len(tail)
            self.tail_peak = max(self.tail_peak, float(np.max(np.abs(tail))))
            self.tail_sum_squares += float(np.sum(np.square(tail, dtype=np.float64)))
        self.frames += len(block)
        if len(block) >= self.window_frames:
            self.endpoint[:] = block[-self.window_frames:]
            self.used = self.window_frames
        else:
            keep = min(self.used, self.window_frames - len(block))
            if keep:
                self.endpoint[:keep] = self.endpoint[self.used-keep:self.used].copy()
            self.endpoint[keep:keep+len(block)] = block
            self.used = keep + len(block)

    def snapshot(self):
        import numpy as np
        end = self.endpoint[:self.used]
        return dict(schema=1, frame_domain='post-alignment encoder input',
            encoder_frames=self.frames, window_policy_seconds=.1, endpoint_frames=self.used,
            endpoint_observation='no_samples' if not self.used else
                'nonzero_samples_observed' if np.any(end != 0) else 'exact_silence_in_window',
            endpoint_peak=float(np.max(np.abs(end))) if self.used else None,
            endpoint_rms=float(np.sqrt(np.mean(np.square(end, dtype=np.float64)))) if self.used else None,
            final_sample=end[-1].tolist() if self.used else None,
            tail_frames=self.tail_frames, tail_peak=self.tail_peak if self.tail_frames else None,
            tail_rms=(self.tail_sum_squares/(self.tail_frames*self.channels))**.5 if self.tail_frames else None,
            complete_tail_established=False, duration_changed=False,
            scope='finite observation only; silence does not rule out later delayed energy')


def render_midi(document, snapshot, destination, container, speed, muted, tail, cancel, progress, temporary_path=None, render_options=None, report=None, evidence=None, prepared=None):
    """Child-process only. Decode float PCM at event sample boundaries.

    Local bass.h GetData and bassmidi.h StreamCreate/StreamEvents ABI:
    decode streams have no device clock; each GetData advances their samples.
    AudioSession still owns creation/teardown, OutputAnalysis owns encoders.
    """
    import audio_engine as ae
    import audio_output
    from audio_output import AudioSession
    from bass_bindings import bass, bassmidi, BASS_SAMPLE_FLOAT, BASS_STREAM_DECODE
    from effect_control import EffectRuntime, EffectEvents
    from settings import recording_capture_options
    from soundfont_runtime import SoundFontRuntime
    import numpy as np
    render_options = (render_options or MidiRenderOptions()).validate(document)
    def phase(stage, done=0, total=0, percent=None):
        if report is not None:
            report(dict(stage=stage,done=done,total=total,percent=percent,at=time.monotonic(),
                        final_pass=stage=='encode' or stage=='render' and not render_options.normalize))
    phase('prepare')

    preferences = replace(snapshot.preferences, midi_async=False, auto_reset_delay_s=0)
    from windows_integration import set_current_process_priority
    set_current_process_priority("below_normal")
    manifest = export_manifest(document, snapshot, container, speed, muted, tail, render_options)
    manifest['effective_preferences'] = asdict(preferences)
    rate = preferences.sample_rate
    from stream_effects import processing_timing
    timing = processing_timing(snapshot.effects, rate)
    if render_options.align_processing and not timing['alignment_available']:
        raise ValueError(tr('player.alignment_unavailable'))
    shift = timing['total_fixed_frames'] if render_options.align_processing else 0
    manifest['processing_timing'] = timing
    manifest['processing_timing']['applied_alignment_frames'] = shift
    if prepared is not None: prepared(manifest)
    session = AudioSession(bass, bassmidi, ae, audio_output)
    fonts = SoundFontRuntime(bass=bass, bassmidi=bassmidi,
        stream_handle=lambda: ae.runtime.stream_handle, engine_preferences=lambda: preferences,
        configure_preset_controls=ae.configure_soundfont_group_presets,
        configure_performance=ae.configure_soundfont_group_performance,
        configure_available_presets=ae.runtime.soundfont_activity.set_available_presets)
    target = Path(destination)
    temporary = Path(temporary_path) if temporary_path else None
    spool = None
    spool_file = None
    try:
        session.open(0, preferences, 0, BASS_SAMPLE_FLOAT | BASS_STREAM_DECODE)
        ae.runtime.output_meter.set_presentation_enabled(False)
        ae.set_default_reverb_send(preferences.default_reverb_send)
        ae.set_cc_interpretation(cc6_direct_pitch_range=preferences.cc6_direct_pitch_range,
                                cc94_custom_delay_send=preferences.cc94_custom_delay_send)
        load_percent = 0
        phase('fonts')
        def font_progress(stage, done, total, _filename):
            nonlocal load_percent
            if stage == "font_done" and total:
                percent = round(3*done/total)
            elif stage in ("sample_bytes", "used_samples_done") and total:
                percent = 3+round(12*done/total)
            else:
                return
            if percent > load_percent:
                load_percent = percent
                progress(percent)
                phase('fonts',done,total,percent)
        # A custom instrument is a captured compiled source generation. Do not
        # silently regenerate it from a subsequently changed/missing sample.
        fonts.load_group(snapshot.group, progress=font_progress, cancel=cancel,retain_custom_sources=True)
        if cancel.is_set():
            return False
        progress(15)
        ae.runtime.midi_learn.restore_ini(snapshot.midi_cc)
        ae.runtime.channel_policy.restore_ini(snapshot.channel_policy)
        ae.runtime.performance.restore_ini(snapshot.performance)
        fx = EffectRuntime(ae, EffectEvents(bassmidi, lambda: ae.runtime.stream_handle))
        # Captured concrete parameters follow macros; macro callbacks need not
        # touch Qt or recalculate a preset from a display label.
        if not fx.apply_state(snapshot.effects, lambda _name: None, lambda _name: None):
            raise RuntimeError(tr("audio_fx.export_failed"))
        ae.set_humanization(0,0)
        if snapshot.effects.humanize_timing_ms or snapshot.effects.humanize_velocity:
            from midi_humanization import HumanizationSettings,humanize_document
            original_duration=document.duration
            document=humanize_document(document,HumanizationSettings(snapshot.effects.humanize_timing_ms,snapshot.effects.humanize_velocity,snapshot.effects.humanize_preserve_simultaneity),reset_test=is_midi_system_reset,speed=speed)
            if render_options.end>=original_duration:render_options=replace(render_options,end=max(render_options.end,document.duration))
        ae.ensure_midi_port(max(document.ports))
        ae.reset_file_transport_state()
        channel_fx=ae.runtime.stream_effects.channel_fx
        if not channel_fx.restore_ini(snapshot.effects.channel_audio_fx):
            raise RuntimeError(tr('audio_fx.export_failed'))
        channel_fx.bind_offline(ae.runtime)
        if temporary is None:
            fd, name = tempfile.mkstemp(prefix=".abmsynth-export-", suffix=".tmp", dir=target.parent)
            os.close(fd); temporary = Path(name)
        options = recording_capture_options(replace(preferences, recording_container=container))
        analysis = ae.runtime.output_analysis
        if render_options.normalize:
            spool = temporary.with_suffix(".f32")
            spool_file = spool.open("wb")
        else:
            analysis.start_recording(str(temporary), queued=False, manual=True,
                                     mono=render_options.mono, **options)
        buffer = (ctypes.c_float * (4096 * 2))()
        view = np.ctypeslib.as_array(buffer)
        current = 0
        first_frame = round(render_options.start/speed*rate) + shift
        end_frame = round(render_options.end/speed*rate)
        render_total = end_frame+round(tail*rate)+shift
        total = render_total-first_frame
        manifest['frame_range'] = dict(source_start=round(render_options.start/speed*rate),
            source_end=end_frame, tail_frames=round(tail*rate),
            discarded_before=first_frame, decoded_total=render_total, encoded_total=total)
        phase('render',0,render_total,15)
        last_progress = -1
        peak = 0.0
        encoded_frames, encoded_peak, encoded_sum_squares = 0, 0.0, 0.0
        endpoint = ExportEndpointEvidence(rate, 1 if render_options.mono else 2,
                                         end_frame + shift - first_frame)

        def encode(signal):
            nonlocal encoded_frames, encoded_peak, encoded_sum_squares
            if not np.isfinite(signal).all():
                raise RuntimeError(tr('player.export_qc_failed'))
            encoded_frames += signal.size // (1 if render_options.mono else 2)
            encoded_peak = max(encoded_peak, float(np.max(np.abs(signal))))
            encoded_sum_squares += float(np.sum(np.square(signal, dtype=np.float64)))
            analysis.feed_recording(ctypes.c_void_p(signal.ctypes.data),signal.nbytes)
            endpoint.feed(signal)

        def consume(frames):
            nonlocal peak
            samples = view[:frames*2]
            # Render continuously from file start. Pre-range notes/controllers
            # and FX tails must evolve naturally; only their PCM is discarded.
            skip = max(0,min(frames,first_frame-current))
            samples = samples[skip*2:]
            if not samples.size:
                return
            if spool_file is not None:
                signal = samples.reshape(-1,2).mean(axis=1) if render_options.mono else samples
                peak = max(peak, float(np.max(np.abs(signal))))
                spool_file.write(samples.tobytes())
            else:
                if render_options.mono:
                    mono = samples.reshape(-1,2).mean(axis=1)
                    encode(mono)
                else:
                    encode(samples)

        def drain(until):
            nonlocal current, last_progress
            while current < until:
                if cancel.is_set():
                    return False
                frames = min(4096, until-current)
                result = bass.BASS_ChannelGetData(ae.runtime.stream_handle, buffer, frames*8)
                if result != frames*8:
                    raise RuntimeError(tr("player.decode_failed", error=bass.BASS_ErrorGetCode()))
                consume(frames)
                current += frames
                percent = min(99, 15+current*(60 if render_options.normalize else 84)//max(1, render_total))
                if percent != last_progress:
                    progress(percent); last_progress = percent
                    phase('render',current,render_total,percent)
            return True

        cursor = MidiTransport(ae.forward_midi_bytes, ae.reset_file_transport_state,
                               ae.silence_channel_notes, start_worker=False,
                               is_drum=lambda ch: ae.runtime.addresses.drums[ch])
        cursor.document = document
        cursor.event_times = tuple(event.seconds for event in document.events)
        cursor.muted, cursor.transpose = frozenset(muted), render_options.transpose
        cursor.index = 0
        for event in document.events[cursor.index:]:
            if event.seconds >= render_options.end:
                break
            if not drain(round(event.seconds / speed * rate)):
                return False
            data = event.data
            if data and not (data[0] & 0xf0 == 0x90 and event.channel in muted and data[-1]):
                cursor.send(data, event.port)
        if not drain(end_frame):
            return False
        # End-of-file release (not sound-off) retains the chosen reverb tail.
        ae.silence_channel_notes(release=True)
        if not drain(render_total):
            return False
        if spool_file is not None:
            spool_file.close(); spool_file = None
            gain = 10**(render_options.peak_db/20)/peak if peak > 0 else 1.0
            analysis.start_recording(str(temporary), queued=False, manual=True,
                                     mono=render_options.mono, **options)
            encoded = 0
            phase('encode',0,total,75)
            with spool.open("rb") as source:
                while data := source.read(4096*8):
                    if cancel.is_set():
                        return False
                    block = np.frombuffer(data,dtype="<f4").copy()
                    block *= gain
                    encoded_block = block.reshape(-1,2).mean(axis=1) if render_options.mono else block
                    encode(encoded_block)
                    encoded += block.size//2
                    percent = min(99,75+encoded*24//max(1,total))
                    if percent != last_progress:
                        progress(percent); last_progress = percent
                        phase('encode',encoded,total,percent)
        phase('finalize',percent=99)
        ae.runtime.output_analysis.stop_recording()
        if ae.runtime.output_analysis.last_recording_error or ae.runtime.output_analysis.last_recording_loss:
            raise RuntimeError(tr("player.encoder_failed"))
        if cancel.is_set():
            return False
        if encoded_frames != total:
            raise RuntimeError(tr('player.export_qc_failed'))
        for asset in manifest['assets']:
            stat = Path(asset['path']).stat()
            if (stat.st_size,stat.st_mtime_ns) != (asset['bytes'],asset['mtime_ns']):
                raise RuntimeError(tr('player.asset_changed',path=asset['path']))
        checked = inspect_encoded_output(bass, temporary, container, total,
                                        1 if render_options.mono else 2, rate, cancel)
        if checked is None: return False
        manifest['output'] = checked
        manifest['encoded_pcm'] = dict(frames=encoded_frames, peak=encoded_peak,
            rms=(encoded_sum_squares/max(1,encoded_frames*(1 if render_options.mono else 2)))**.5,
            finite=True, rate=rate, channels=1 if render_options.mono else 2)
        manifest['encoded_pcm']['endpoint'] = endpoint.snapshot()
        if evidence is not None: evidence(manifest)
        if temporary_path is None:
            os.replace(temporary, target)
        # In the UI job only the parent commits, after it receives success.
        # Cancel before that cannot replace an existing destination.
        temporary = None
        progress(100)
        return True
    finally:
        if spool_file is not None:
            spool_file.close()
        if spool is not None:
            spool.unlink(missing_ok=True)
        try:
            ae.runtime.output_analysis.stop_recording()
        finally:
            try:
                fonts.close()
            finally:
                try:
                    session.close()
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)


EXPORT_CHECKPOINT_BYTES = 16*1024*1024  # Recovery parser policy, not encoder/BASS.
EXPORT_REPLAY_EVENTS = 50000  # Bound tagged request allocation, not SMF playback.


def _checkpoint_root():
    from settings import settings_path
    return settings_path().parent / '.abmsynth-export-jobs'


@lru_cache(maxsize=1)
def _request_models():
    # Explicit trusted constructors only. Journal text never chooses imports,
    # executable callables or pickle reducers. Exact fields refuse future data.
    from settings import EnginePreferences
    from soundfont_files import SoundFontGroup, SoundFontEntry, PresetOverride, SoundFontMapping
    from effect_control import (EffectState, ReverbState, ChorusState, DelayState,
        ExtendedDelayState, EQBand, EqualizerState, CompressorState, LimiterState,
        SpatialParameters, AudioFXSlot)
    return {cls.__name__:cls for cls in (EnginePreferences, SoundFontGroup,
        SoundFontEntry, PresetOverride, SoundFontMapping, EffectState, ReverbState, ChorusState,
        DelayState, ExtendedDelayState, EQBand, EqualizerState, CompressorState,
        LimiterState, SpatialParameters, AudioFXSlot, ExportSnapshot,
        MidiDocument, MidiFileEvent, MidiRenderOptions)}


def _request_value(value, *, decode=False, depth=0):
    if depth > 32: raise ValueError(tr('player.recovery_invalid'))
    def child(item): return _request_value(item,decode=decode,depth=depth+1)
    if value is None or type(value) in (str,int,bool): return value
    if type(value) is float:
        import math
        if not math.isfinite(value): raise ValueError(tr('player.recovery_invalid'))
        return value
    if decode:
        if type(value) is not dict or set(value)!= {'type','value'}:
            raise ValueError(tr('player.recovery_invalid'))
        kind, data = value['type'],value['value']
        if kind=='bytes' and isinstance(data,str): return bytes.fromhex(data)
        if kind in ('tuple','list','set','dict') and isinstance(data,list):
            if kind=='dict':
                if any(type(pair) is not list or len(pair)!=2 for pair in data):
                    raise ValueError(tr('player.recovery_invalid'))
                pairs=[(child(key),child(item)) for key,item in data]
                result=dict(pairs)
                if len(result)!=len(pairs): raise ValueError(tr('player.recovery_invalid'))
                return result
            return {'tuple':tuple,'list':list,'set':set}[kind](child(item) for item in data)
        cls=_request_models().get(kind)
        if cls is None or type(data) is not dict or set(data)!={field.name for field in fields(cls)}:
            raise ValueError(tr('player.recovery_invalid'))
        return cls(**{key:child(item) for key,item in data.items()})
    if is_dataclass(value) and type(value).__name__ in _request_models():
        return dict(type=type(value).__name__,value={f.name:child(getattr(value,f.name)) for f in fields(value)})
    if type(value) is bytes: return dict(type='bytes',value=value.hex())
    if type(value) in (tuple,list,set):
        return dict(type=type(value).__name__,value=[child(item) for item in value])
    if type(value) is dict:
        return dict(type='dict',value=[[child(key),child(item)] for key,item in value.items()])
    raise ValueError(tr('player.recovery_invalid'))


def _export_request(document,snapshot,container,speed,muted,tail,options):
    return dict(schema=1,payload=_request_value((document,snapshot,container,speed,tuple(muted),tail,options)))


def _restore_export_request(request, *, qualified_sfz=False):
    if type(request) is not dict or set(request)!= {'schema','payload'} or type(request['schema']) is not int or request['schema']!=1:
        raise ValueError(tr('player.recovery_invalid'))
    value=_request_value(request['payload'],decode=True)
    if type(value) is not tuple or len(value)!=7: raise ValueError(tr('player.recovery_invalid'))
    document,snapshot,container,speed,muted,tail,options=value
    from settings import RECORDING_EXTENSIONS,EnginePreferences
    from soundfont_files import SoundFontGroup
    from effect_control import EffectState
    if (type(document) is not MidiDocument or type(snapshot) is not ExportSnapshot
        or type(snapshot.preferences) is not EnginePreferences or type(snapshot.group) is not SoundFontGroup
        or type(snapshot.effects) is not EffectState or type(options) is not MidiRenderOptions
        or container not in RECORDING_EXTENSIONS or not .25<=speed<=4 or not 0<=tail<=30
        or type(document.events) is not tuple or len(document.events)>EXPORT_REPLAY_EVENTS
        or not 0<=document.duration
        or any(type(e) is not MidiFileEvent or type(e.data) is not bytes or
               not 0<=e.seconds<=document.duration or not 0<=e.port<MIDI_PORT_LIMIT for e in document.events)
        or any(a.seconds>b.seconds for a,b in zip(document.events,document.events[1:]))
        or any(type(channel) is not int or not 0<=channel<16*MIDI_PORT_LIMIT for channel in muted)):
        raise ValueError(tr('player.recovery_invalid'))
    snapshot.preferences.validate(); options.validate(document)
    if not qualified_sfz and any(Path(entry.filename).suffix.casefold()=='.sfz' for entry in snapshot.group.fonts):
        raise ValueError(tr('player.recovery_asset_unqualified'))
    return value


@contextmanager
def _checkpoint_directory_lock(root):
    """Serialize lease-domain creation/retirement, not a render/audio lock."""
    import msvcrt
    root.mkdir(parents=True,exist_ok=True)
    with (root/'.directory.lock').open('a+b') as lock:
        deadline=time.monotonic()+2
        while True:
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1); break
            except OSError:
                if time.monotonic()>=deadline: raise
                time.sleep(.005)
        try: yield
        finally:
            lock.seek(0); msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)


def _checkpoint_lease(path, offset=0, length=2):
    """msvcrt: adjacent parent/child regions; recovery acquires both nonblocking.

    Process death releases the OS lock. Persistent per-UUID sidecars avoid
    deleting/recreating a live lock domain; no PID-reuse guess or os.kill.
    """
    import msvcrt
    path=_checkpoint_path(path)
    with _checkpoint_directory_lock(path.parent):
        lock = path.with_suffix('.lock').open('a+b')
        try:
            lock.seek(offset); msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, length)
        except BaseException:
            lock.close(); raise
    return lock


def cleanup_export_metadata(*, older_than=86400, now=None):
    """Remove only expired orphan UUID lease files/writing stages.

    Never deletes journals, unknown names, fresh stages or audio drafts. The
    persistent directory coordination file prevents two recreated lock domains.
    Called by recovery discovery on its I/O worker, not by PCM or UI painting.
    """
    import msvcrt
    root=_checkpoint_root()
    if not root.exists(): return ()
    now=time.time() if now is None else now
    removed=[]
    with _checkpoint_directory_lock(root):
        for path in tuple(root.iterdir()):
            orphan_lock=path.suffix=='.lock' and len(path.stem)==32
            stage=path.suffix=='.writing' and len(path.name.split('-',1)[0])==32
            if not (orphan_lock or stage): continue
            identifier=path.stem if orphan_lock else path.name.split('-',1)[0]
            if any(c not in '0123456789abcdef' for c in identifier): continue
            journal=root/(identifier+'.json'); lease=root/(identifier+'.lock')
            try:
                if path.is_symlink() or now-path.stat().st_mtime<older_than: continue
                if lease.is_symlink(): continue
                if orphan_lock and journal.exists(): continue
                if stage and journal.exists():
                    try: _read_checkpoint(journal)
                    except (ValueError,KeyError,TypeError,OSError): continue
                with lease.open('a+b') as lock:
                    lock.seek(0)
                    try: msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,2)
                    except OSError: continue
                    if stage:
                        path.unlink(); removed.append(path.name)
                    # close releases both regions before unlink, while the
                    # directory mutex prevents any intervening new acquisition.
                if orphan_lock:
                    path.unlink(); removed.append(path.name)
            except OSError: continue  # Retry later; never label cleanup success.
    return tuple(removed)


def _write_checkpoint(path, record):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=path.stem+'-', suffix='.writing', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(record, output, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            output.flush(); os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _checkpoint_path(path):
    path = Path(path).resolve()
    if (path.parent != _checkpoint_root().resolve() or len(path.stem) != 32 or
            any(c not in '0123456789abcdef' for c in path.stem) or
            path.suffix != '.json'):
        raise ValueError(tr('player.recovery_invalid'))
    return path


def _read_checkpoint(path):
    from settings import RECORDING_EXTENSIONS
    path = _checkpoint_path(path)
    if path.stat().st_size > EXPORT_CHECKPOINT_BYTES: raise ValueError(tr('player.recovery_invalid'))
    record = json.loads(path.read_text(encoding='utf-8'))
    if type(record['schema']) is not int or record['schema'] != 1 or record['id'] != path.stem:
        raise ValueError(tr('player.recovery_invalid'))
    temporary, destination = Path(record['temporary']).resolve(), Path(record['destination']).resolve()
    if (temporary.parent != destination.parent or not temporary.name.startswith('.abmsynth-export-')
            or temporary.suffix != '.tmp' or record['container'] not in RECORDING_EXTENSIONS):
        raise ValueError(tr('player.recovery_invalid'))
    return record


def discover_export_checkpoints(cancelled=lambda: False):
    """Preserve journals/audio; retire only expired orphan internal metadata."""
    root = _checkpoint_root()
    if not root.is_dir(): return []
    cleanup_export_metadata()
    records = []
    for path in root.glob('*.json'):
        if cancelled(): break
        lock = None
        try:
            lock = _checkpoint_lease(path)
            record = _read_checkpoint(path)
            if record['state'] == 'verified' and Path(record['temporary']).is_file():
                records.append(dict(record, journal=str(path)))
            elif record['state']=='prepared' and 'request' in record and 'prepared' in record:
                _restore_export_request(record['request'], qualified_sfz=record['prepared'].get('reproduction',{}).get('qualified',False))
                records.append(dict(record,journal=str(path)))
        except (OSError, ValueError, KeyError, TypeError, RecursionError, MemoryError):
            continue  # Preserve unknown/partial records; never delete on read.
        finally:
            if lock is not None: lock.close()
    return records


def restart_export_checkpoint(path,destination,cancelled=lambda: False):
    """Worker-only explicit restart. Original lease survives until job.close.

    Replays the saved event timeline; never changes live widgets/INI/handles.
    Asset and runtime proof is conservative: any changed file refuses replay.
    """
    lock=_checkpoint_lease(path)
    try:
        record=_read_checkpoint(path)
        if record['state']!='prepared': raise ValueError(tr('player.recovery_invalid'))
        request=_restore_export_request(record['request'], qualified_sfz=record.get('prepared',{}).get('reproduction',{}).get('qualified',False))
        document,snapshot,container,speed,muted,tail,options=request
        if Path(destination).resolve()==Path(document.path).resolve():
            raise ValueError(tr('player.source_protected'))
        # Recompute configuration/timeline AND asset/runtime lists, not merely
        # accept a journal's identity list that omits a consumed resource.
        current=export_manifest(document,snapshot,container,speed,muted,tail,options,cancelled)
        previous=record['prepared']
        if record['source']!=str(Path(document.path).resolve()) or record['container']!=container:
            raise ValueError(tr('player.recovery_invalid'))
        _match_export_inputs(current,previous)
        if cancelled(): raise RuntimeError(tr('player.export_cancelled'))
        job=MidiExportJob(document,snapshot,destination,container,speed,muted,tail,options,expected_inputs=previous)
        job._replay_lock=lock; job._replay_checkpoint=Path(path)
        job._replay_temporary=Path(record['temporary'])
        lock=None
        if cancelled():
            job.close(); raise RuntimeError(tr('player.export_cancelled'))
        return job
    finally:
        if lock is not None: lock.close()


def _match_export_inputs(current,previous):
    """Same proof at worker acceptance AND child preparation, before BASS load."""
    if not current['reproduction']['qualified'] or not previous.get('reproduction',{}).get('qualified',False):
        raise ValueError(tr('player.recovery_asset_unqualified'))
    if current.get('asset_graphs',{}) != previous.get('asset_graphs',{}):
        raise ValueError(tr('player.recovery_inputs_changed',path='SFZ dependency graph'))
    if current['midi']!=previous['midi'] or current['configuration_sha256']!=previous['configuration_sha256']:
        raise ValueError(tr('player.recovery_invalid'))
    for key in ('assets','runtime'):
        expected={item['path']:item['sha256'] for item in previous[key]}
        actual={item['path']:item['sha256'] for item in current[key]}
        if set(expected)!=set(actual): raise ValueError(tr('player.recovery_invalid'))
        for path,digest in actual.items():
            if expected[path]!=digest:
                raise RuntimeError(tr('player.recovery_inputs_changed',path=path))


def recover_export_checkpoint(path, destination, cancelled=lambda: False):
    """Worker-only verified save; caller must authorize any replacement."""
    lock = _checkpoint_lease(path)
    try:
        record = _read_checkpoint(path)
        if record['state'] != 'verified': raise ValueError(tr('player.recovery_invalid'))
        destination = Path(destination).resolve()
        if destination == Path(record['source']).resolve(): raise ValueError(tr('player.source_protected'))
        temporary = Path(record['temporary'])
        actual = file_identity(temporary, cancelled)
        expected = record['evidence']['output']['identity']
        if any(actual[key] != expected[key] for key in ('bytes','mtime_ns','sha256')):
            raise RuntimeError(tr('player.export_qc_failed'))
        if destination.parent != temporary.resolve().parent:
            # os.replace may cross volumes. Stage and fsync on target volume,
            # keeping the checked original until that final replacement succeeds.
            fd, name = tempfile.mkstemp(prefix='.abmsynth-recovery-', suffix='.tmp', dir=destination.parent)
            staged = Path(name)
            try:
                with temporary.open('rb') as source, os.fdopen(fd,'wb') as output:
                    digest = hashlib.sha256()
                    while block := source.read(1024*1024):
                        if cancelled(): raise RuntimeError(tr('player.export_cancelled'))
                        output.write(block); digest.update(block)
                    output.flush(); os.fsync(output.fileno())
                if digest.hexdigest() != expected['sha256']: raise RuntimeError(tr('player.export_qc_failed'))
                if cancelled(): raise RuntimeError(tr('player.export_cancelled'))
                os.replace(staged, destination)
            finally: staged.unlink(missing_ok=True)
            try: temporary.unlink(missing_ok=True)
            except OSError: pass  # Save already succeeded; cleanup cannot undo it.
        else:
            if cancelled(): raise RuntimeError(tr('player.export_cancelled'))
            os.replace(temporary,destination)
        # Do not offer a retained journal as unsaved after a successful Save As.
        record.update(state='committed',recovered_destination=str(destination))
        try: _write_checkpoint(path,record)
        except OSError: pass
        try: Path(path).unlink(missing_ok=True)
        except OSError: pass  # The moved draft no longer qualifies for discovery.
        return str(destination)
    finally: lock.close()


def _export_child(document, snapshot, destination, container, speed, muted, tail, cancel, pipe, temporary, render_options, checkpoint, expected_inputs=None):
    lock = None
    def publish(message):
        try: pipe.send(message)
        except (BrokenPipeError, EOFError, OSError): pass  # Finish/checkpoint if parent died.
    def verified(value):
        with Path(temporary).open('r+b') as output: os.fsync(output.fileno())
        record = _read_checkpoint(checkpoint)
        record.update(state='verified', evidence=value)
        _write_checkpoint(checkpoint,record)  # Durable verification BEFORE pipe notification.
        publish(('evidence',value))
    def prepared(value):
        if expected_inputs is not None: _match_export_inputs(value,expected_inputs)
        record=_read_checkpoint(checkpoint)
        if not value['reproduction']['qualified']:
            # Normal SFZ export and checked-audio Save As are still supported.
            # Do not advertise captured replay from a root-only fingerprint.
            record['replay_qualification']=value['reproduction']
            _write_checkpoint(checkpoint,record)
            publish(('phase',dict(stage='prepare',percent=None))); return
        # Product recovery admission, NOT MIDI/BASS/export event limit. Bound
        # expanded tagged timeline allocation as well as journal read bytes.
        if len(document.events)>EXPORT_REPLAY_EVENTS:
            publish(('phase',dict(stage='prepare',percent=None))); return
        request=_export_request(document,snapshot,container,speed,muted,tail,render_options)
        candidate=dict(record,state='prepared',request=request,prepared=value)
        # Recovery qualification is optional. Do not reject an otherwise valid
        # large export because its expanded timeline exceeds this product cap.
        size=0
        for chunk in json.JSONEncoder(ensure_ascii=False,allow_nan=False,separators=(',',':')).iterencode(candidate):
            size+=len(chunk.encode('utf-8'))
            if size>EXPORT_CHECKPOINT_BYTES: break
        if size<=EXPORT_CHECKPOINT_BYTES:
            _write_checkpoint(checkpoint,candidate)
        publish(('phase',dict(stage='prepare',percent=None)))
    try:
        lock = _checkpoint_lease(checkpoint,1,1)
        done = render_midi(document, snapshot, destination, container, speed, muted, tail,
                           cancel, lambda value: publish(("progress", value)), temporary, render_options,
                           lambda value:publish(('phase',value)), verified, prepared)
        publish(("done", done))
    except Exception as exc:
        publish(("error", str(exc)))
    finally:
        if lock is not None: lock.close()
        pipe.close()


class MidiExportJob:
    """Spawned process with a one-way pipe; no inherited live DLL handles."""
    def __init__(self, document, snapshot, destination, container, speed=1., muted=(), tail=3., render_options=None, *, expected_inputs=None):
        from settings import RECORDING_EXTENSIONS
        if container not in RECORDING_EXTENSIONS or not .25 <= speed <= 4 or not 0 <= tail <= 30:
            raise ValueError("MIDI export options")
        snapshot.preferences.validate()
        render_options = (render_options or MidiRenderOptions()).validate(document)
        context = multiprocessing.get_context("spawn")
        self.cancel = context.Event()
        self.destination = Path(destination)
        self.evidence = None
        self.completed = False
        self.reader, writer = context.Pipe(duplex=False)
        self.temporary = None
        self._preserve_verified = False
        self._checkpoint_lock = None
        self._replay_lock = None
        self._replay_checkpoint = None
        self._replay_temporary = None
        self._committed = False
        self.checkpoint = None
        from uuid import uuid4
        try:
            fd, temporary = tempfile.mkstemp(prefix=".abmsynth-export-", suffix=".tmp", dir=Path(destination).parent)
            os.close(fd)
            self.temporary = Path(temporary)
            root = _checkpoint_root(); root.mkdir(parents=True,exist_ok=True)
            self.checkpoint = root / (uuid4().hex+'.json')
            self._checkpoint_lock = _checkpoint_lease(self.checkpoint,0,1)
            _write_checkpoint(self.checkpoint,dict(schema=1,id=self.checkpoint.stem,state='preparing',
                temporary=str(self.temporary.resolve()),destination=str(self.destination.resolve()),
                source=str(Path(document.path).resolve()),container=container))
            self.process = context.Process(target=_export_child,
                args=(document, snapshot, str(destination), container, speed, tuple(muted), tail, self.cancel, writer, temporary, render_options,str(self.checkpoint),expected_inputs),
                name="ABMSynth-MIDI-export")
            self.process.start()
        except Exception:
            try:
                for path in (self.temporary,self.checkpoint):
                    if path is not None:
                        try: path.unlink(missing_ok=True)
                        except OSError: pass
            finally:
                if self._checkpoint_lock is not None: self._checkpoint_lock.close()
                self.reader.close(); writer.close()
            raise
        writer.close()

    def poll(self):
        messages = []
        try:
            while self.reader.poll():
                message = self.reader.recv()
                if message[0] == 'evidence': self.evidence = message[1]
                else:
                    messages.append(message)
                    if message == ('done', True): self.completed = True
        except (EOFError, BrokenPipeError):
            pass
        return messages

    def close(self):
        self.cancel.set()
        # A slow native FontInit/encoder call is confined to this process.
        # It can be terminated without corrupting the live engine or target.
        self.process.join(.2)
        if self.process.is_alive():
            self.process.terminate(); self.process.join()
        self.cleanup_error = ''
        cleanup = [self.temporary.with_suffix('.f32')]
        if not self._preserve_verified: cleanup += [self.checkpoint,self.temporary]
        if self._committed and self._replay_checkpoint is not None:
            cleanup += [self._replay_checkpoint,self._replay_temporary,self._replay_temporary.with_suffix('.f32')]
        try:
            for path in cleanup:
                try: path.unlink(missing_ok=True)
                except OSError as exc: self.cleanup_error = str(exc)
        finally:
            self.reader.close()
            if self._checkpoint_lock is not None:
                self._checkpoint_lock.close(); self._checkpoint_lock = None
            if self._replay_lock is not None:
                self._replay_lock.close(); self._replay_lock=None

    def commit(self):
        if self.cancel.is_set():
            raise RuntimeError(tr("player.export_cancelled"))
        if not self.completed or self.evidence is None:
            raise RuntimeError(tr('player.export_qc_failed'))
        identity = self.evidence['output']['identity']
        stat = self.temporary.stat()
        if (stat.st_size, stat.st_mtime_ns) != (identity['bytes'], identity['mtime_ns']):
            raise RuntimeError(tr('player.export_qc_failed'))
        try:
            os.replace(self.temporary, self.destination)
            self._committed=True
            if self._replay_checkpoint is not None:
                # Saving already succeeded. Cleanup errors must not report it
                # as failure or offer the old request as still unfinished.
                try:
                    old=_read_checkpoint(self._replay_checkpoint)
                    old.update(state='committed',recovered_destination=str(self.destination.resolve()))
                    _write_checkpoint(self._replay_checkpoint,old)
                except (OSError,ValueError,KeyError): pass
        except OSError:
            self._preserve_verified = True
            raise
