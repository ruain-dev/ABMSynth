"""Receive MIDI 1.0 byte frames from the ABMSynth WinMM MIDI driver."""

from __future__ import annotations

import ctypes
import struct
import threading
import time
import os
from contextlib import contextmanager
from typing import Callable
from pathlib import Path
from product import MIDI_PORT_NAME, PRODUCT_NAME, WINMM_PIPE_NAME
from event_log import log_text
from i18n import tr


PIPE_NAME = WINMM_PIPE_NAME
_FRAME_MAGIC = 0x3144494D  # b"MID1" interpreted as a little-endian DWORD.
_TRACE_FRAME_MAGIC = 0x3244494D  # b"MID2" adds one signed 64-bit trace sequence.
_TIMED_FRAME_MAGIC = 0x3344494D  # MID3: driver-entry QPC and ordered producer identity.
_ORIGIN = struct.Struct("<QQQQqQII")
_HEADER = struct.Struct("<II")
_TRACE_SEQUENCE = struct.Struct("<q")
_MAX_FRAME_BYTES = 1024 * 1024
_MAX_CLIENTS = 8

_read_clock = threading.local()
_timed_library = None
_timed_read = None
_timed_frame = None
_timed_origin = None
_reader_checked = False
_reader_lock = threading.Lock()
_scheduling_backend = None
_scheduling_checked = False
_scheduling_lock = threading.Lock()


def _midi_scheduling_backend():
    global _scheduling_backend, _scheduling_checked
    with _scheduling_lock:
        if not _scheduling_checked:
            try:
                backend=ctypes.WinDLL('avrt',use_last_error=True)
                backend.AvSetMmThreadCharacteristicsW.argtypes=[ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_uint)]
                backend.AvSetMmThreadCharacteristicsW.restype=ctypes.c_void_p
                backend.AvRevertMmThreadCharacteristics.argtypes=[ctypes.c_void_p]
                backend.AvRevertMmThreadCharacteristics.restype=ctypes.c_int
                _scheduling_backend=backend
            except (OSError,AttributeError):pass # Original normal-priority fallback.
            _scheduling_checked=True
    return _scheduling_backend


class MidiThreadScheduling:
    """One MIDI application worker's MMCSS registration; never process policy.

    avrt.h W API: task index starts0, returned HANDLE is an AVRT task handle,
    not a kernel handle to CloseHandle. Revert must run on the registering
    thread. No affinity, realtime process priority, registry or timer change.
    Failure leaves the original scheduling, with developer statistics only.
    """
    def __init__(self,backend):
        self.backend=backend;self.handle=None;self.owner=None
        self.registered=False;self.error=0;self.revert_error=0;self.reverted=False
        self.owner_mismatch=False

    def start(self):
        if self.owner is not None:
            return self.owner==threading.get_native_id() and bool(self.handle)
        self.owner=threading.get_native_id()
        if self.backend is None:return False
        index=ctypes.c_uint(0)
        self.handle=self.backend.AvSetMmThreadCharacteristicsW('Pro Audio',ctypes.byref(index))
        self.registered=bool(self.handle)
        if not self.handle:self.error=ctypes.get_last_error()
        return self.registered

    def close(self):
        if not self.handle:return True
        if self.owner!=threading.get_native_id():
            self.owner_mismatch=True;return False # Retain ownership, no foreign-thread Revert.
        if not self.backend.AvRevertMmThreadCharacteristics(self.handle):
            self.revert_error=ctypes.get_last_error();return False
        self.handle=None;self.reverted=True;self.owner_mismatch=False;return True

    def statistics(self):
        return dict(consumer_mmcss_available=self.backend is not None,
            consumer_mmcss_registered=self.registered,consumer_mmcss_error=self.error,
            consumer_mmcss_reverted=self.reverted,consumer_mmcss_revert_error=self.revert_error,
            consumer_mmcss_owner_thread=self.owner,consumer_mmcss_owner_mismatch=self.owner_mismatch)


def _timed_reader():
    """Owned native ReadFile wrapper; timestamp precedes GIL reacquisition."""
    global _timed_library,_timed_read,_timed_frame,_timed_origin,_reader_checked
    with _reader_lock:
        if not _reader_checked:
            try:
                path=os.environ.get("ABMSYNTH_OBSERVATION_DLL",str(Path(__file__).with_name("abmsynth_dsp.dll")))
                library=ctypes.CDLL(path,use_last_error=True)
                function=library.playback_pipe_read
                function.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.POINTER(ctypes.c_uint64)]
                function.restype=ctypes.c_int
                frame=library.playback_pipe_frame
                frame.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.POINTER(ctypes.c_int64),ctypes.POINTER(ctypes.c_uint64)]
                frame.restype=ctypes.c_int
                _timed_library,_timed_read,_timed_frame=library,function,frame
                origin=getattr(library,"playback_pipe_frame_origin",None)
                if origin is not None:
                    origin.argtypes=frame.argtypes+[ctypes.POINTER(ctypes.c_uint64)]
                    origin.restype=ctypes.c_int
                    _timed_origin=origin
            except (OSError,AttributeError):pass
            _reader_checked=True
    return _timed_read

class NativeMidiQueue:
    """Borrowed pipe + native reader; bounded storage, exact ordered frames.

    Additive own-DLL ABI. No native-to-Python reader callback. Stop cancels
    its native I/O; release joins before pipe close. Capacities are product
    resource policies, not MIDI/library maxima. Missing ABI retains fallback.
    """
    fields=('read_frames','dequeued_frames','payload_bytes','queued_frames',
            'retained_bytes','peak_frames','peak_bytes','backpressure_waits',
            'max_queue_age_qpc','batches','mmcss_registered','mmcss_error','ended','stopping')

    def __init__(self, library, handle, *, listener_session=None):
        self.library=library
        self.pointer=None
        self._lifetime=threading.Lock();self._calls=0
        self.scheduling=None
        self.buffer=(ctypes.c_ubyte*_MAX_FRAME_BYTES)()
        self.descriptors=(ctypes.c_uint64*(13*64))()
        create=library.midi_pipe_queue_create if listener_session is None else library.midi_pipe_queue_create_listener
        create.argtypes=[ctypes.c_void_p]+([] if listener_session is None else [ctypes.c_uint]);create.restype=ctypes.c_void_p
        self.pop=library.midi_pipe_queue_pop
        self.pop.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint,
                          ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint]
        self.pop.restype=ctypes.c_int
        self.stop=library.midi_pipe_queue_stop
        self.stop.argtypes=[ctypes.c_void_p];self.stop.restype=None
        self.release=library.midi_pipe_queue_release
        self.release.argtypes=[ctypes.c_void_p];self.release.restype=ctypes.c_int
        self.snapshot=library.midi_pipe_queue_snapshot
        self.snapshot.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint]
        self.snapshot.restype=ctypes.c_uint
        self.listener_snapshot=getattr(library,'midi_pipe_queue_listener_snapshot',None)
        if self.listener_snapshot is not None:
            self.listener_snapshot.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint]
            self.listener_snapshot.restype=ctypes.c_uint
        self.pointer=create(handle) if listener_session is None else create(handle,listener_session)

    def read(self):
        with self.borrow() as pointer:
            return self.pop(pointer,self.buffer,_MAX_FRAME_BYTES,self.descriptors,64) if pointer else -2

    @contextmanager
    def borrow(self):
        """Root-call lease across CDLL's GIL release, not a wait/consumer lock.

        A blocked pop must still permit request_stop. close refuses active
        calls instead of freeing the root between pointer evaluation and native
        entry. Native worker's own consumer lease begins before this one ends.
        """
        with self._lifetime:
            pointer=self.pointer
            if pointer:self._calls+=1
        try:yield pointer
        finally:
            if pointer:
                with self._lifetime:self._calls-=1

    def payload(self, offset, size):
        return ctypes.string_at(ctypes.addressof(self.buffer)+offset,size)

    def statistics(self):
        values=(ctypes.c_uint64*14)()
        with self.borrow() as pointer:
            if not pointer or self.snapshot(pointer,values,14)!=14:return {}
        listener={}
        if self.listener_snapshot is not None:
            extra=(ctypes.c_uint64*7)()
            with self.borrow() as pointer:
                if pointer and self.listener_snapshot(pointer,extra,7)==7:
                    listener=dict(zip(('prepared_listener','client_connected','session_error','reader_thread','listener_ready',
                                       'reader_mmcss_reverted','reader_mmcss_revert_error'),extra))
        return {**dict(zip(self.fields,values)),**listener,**(self.scheduling.statistics() if self.scheduling is not None else {})}

    def request_stop(self):
        with self.borrow() as pointer:
            if pointer:self.stop(pointer)

    def close(self):
        with self._lifetime:
            if not self.pointer:return True
            if self._calls or not self.release(self.pointer):return False
            self.pointer=None
            return True


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateNamedPipeW.argtypes = [
    ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
]
_kernel32.CreateNamedPipeW.restype = ctypes.c_void_p
_kernel32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_kernel32.ConnectNamedPipe.restype = ctypes.c_int
_kernel32.DisconnectNamedPipe.argtypes = [ctypes.c_void_p]
_kernel32.DisconnectNamedPipe.restype = ctypes.c_int
_kernel32.ReadFile.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
]
_kernel32.ReadFile.restype = ctypes.c_int
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel32.CloseHandle.restype = ctypes.c_int
_kernel32.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
_kernel32.OpenThread.restype = ctypes.c_void_p
_kernel32.CancelSynchronousIo.argtypes = [ctypes.c_void_p]
_kernel32.CancelSynchronousIo.restype = ctypes.c_int
_kernel32.CreateFileW.argtypes = [
    ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
]
_kernel32.CreateFileW.restype = ctypes.c_void_p
_kernel32.GetNamedPipeClientSessionId.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32)]
_kernel32.GetNamedPipeClientSessionId.restype=ctypes.c_int
_kernel32.ProcessIdToSessionId.argtypes=[ctypes.c_uint32,ctypes.POINTER(ctypes.c_uint32)]
_kernel32.ProcessIdToSessionId.restype=ctypes.c_int

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_PIPE_ACCESS_INBOUND = 0x00000001
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_ERROR_PIPE_CONNECTED = 535
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_THREAD_TERMINATE = 0x0001


class WinMMMidiPipeServer:
    """Multi-client inbound pipe endpoint owned by the ABMSynth UI process."""

    def __init__(self, on_midi: Callable[[bytes, int | None], None], on_log: Callable[[str], None] | None = None,*,native_client_factory=None):
        self._on_midi = on_midi
        self._on_log = on_log
        self._stopping = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._listener_handle: int | None = None
        self._workers: dict[threading.Thread, int] = {}
        self._native_readers={}
        self._native_client_factory=native_client_factory
        self._native_consumers={}
        self._pending_callbacks={}
        self._completed_transport=[]
        self._connection_slots = threading.BoundedSemaphore(_MAX_CLIENTS)
        # The WinMM driver exposes zero output devices until this listener
        # actually exists.  The UI must not claim that the port is ready while
        # the accept thread is merely scheduled but has not created its pipe.
        self._ready = threading.Event()
        self._startup_error: str | None = None
        self._creation_error=0
        self._origin_sequences={}
        self._origin_lock=threading.Lock()
        frequency=ctypes.c_int64()
        _kernel32.QueryPerformanceFrequency(ctypes.byref(frequency))
        self._frequency=frequency.value
        session=ctypes.c_uint32()
        if not _kernel32.ProcessIdToSessionId(os.getpid(),ctypes.byref(session)):
            raise OSError(ctypes.get_last_error(),tr('midi_pipe.session_query_failed'))
        self._session_id=session.value

    @property
    def running(self) -> bool:
        return self._accept_thread is not None and self._accept_thread.is_alive()

    def start(self, timeout: float = 2.0) -> bool:
        if self._stopping.is_set():
            with self._state_lock:
                if self._workers or self._native_readers:return False
        if self.running:
            return self._ready.is_set() and self._startup_error is None
        _timed_reader() # before publishing a ready pipe; not on first Note On
        _midi_scheduling_backend() # Resolve API before the first input worker.
        self._stopping.clear()
        self._ready.clear()
        self._startup_error = None
        prepared=(self._native_client_factory is not None and _timed_library is not None and
                  hasattr(_timed_library,'midi_pipe_queue_create_listener'))
        self._accept_thread = threading.Thread(target=self._prepared_accept_loop if prepared else self._accept_loop,
                                              name="ABMSynthMidiAccept", daemon=True)
        self._accept_thread.start()
        if not self._ready.wait(timeout):
            self._startup_error = tr('midi_pipe.startup_timeout')
            self._log(log_text('midi_pipe.startup_failed',error=self._startup_error))
            self.stop()
            return False
        return self._startup_error is None

    def stop(self) -> bool:
        with self._state_lock:
            self._stopping.set()
            threads = [self._accept_thread, *self._workers.keys()]
            # Cancel the actual native ReadFile owner, also waking native pop.
            for reader in self._native_readers.values():reader.request_stop()
            for consumer in self._native_consumers.values():consumer.request_stop()
        for thread in threads:
            self._cancel_thread_io(thread)
        # Wake a blocking ConnectNamedPipe call without terminating the thread.
        wake_handle = _kernel32.CreateFileW(
            PIPE_NAME, _GENERIC_WRITE, 0, None, _OPEN_EXISTING, 0, None
        )
        if wake_handle and wake_handle != _INVALID_HANDLE_VALUE:
            _kernel32.CloseHandle(wake_handle)
        for thread in threads[1:]:
            if thread and thread.ident is not None:
                thread.join(timeout=1.0)
        # A semaphore waiter may have had no pending I/O at the first cancel.
        # CancelSynchronousIo does not cancel future ConnectNamedPipe calls.
        # Recheck cancellation until this accept owner actually exits, bounded
        # by the same one-second join budget; timeout still retains ownership.
        accept=threads[0]
        if accept:
            deadline=time.monotonic()+1.0
            while accept.is_alive():
                self._cancel_thread_io(accept)
                remaining=deadline-time.monotonic()
                if remaining<=0:break
                accept.join(timeout=min(.01,remaining))
        # A prior native join timeout retains ownership. Retry only after the
        # Python borrower has exited; never free beneath an active callback.
        with self._state_lock:
            retired=[(thread,handle,self._native_readers[thread]) for thread,handle in self._workers.items()
                     if not thread.is_alive() and thread in self._native_readers]
        for _thread,handle,reader in retired:self._finish_worker(handle,reader,_thread)
        with self._state_lock:
            if self._accept_thread and not self._accept_thread.is_alive():
                self._accept_thread = None
            return (not self._workers and not self._native_readers and not self._native_consumers and
                    not self._pending_callbacks and self._accept_thread is None)

    @staticmethod
    def _cancel_thread_io(thread: threading.Thread | None) -> None:
        # ReadFile and ConnectNamedPipe are synchronous in the worker. Closing
        # their pipe handle from another thread does not reliably unblock I/O.
        if not thread or not thread.native_id:
            return
        native_thread = _kernel32.OpenThread(_THREAD_TERMINATE, False, thread.native_id)
        if native_thread and native_thread != _INVALID_HANDLE_VALUE:
            _kernel32.CancelSynchronousIo(native_thread)
            _kernel32.CloseHandle(native_thread)

    def _log(self, text: str) -> None:
        if self._on_log:
            self._on_log(text)

    def _new_pipe(self, *, first=False) -> int | None:
        handle = _kernel32.CreateNamedPipeW(
            PIPE_NAME,
            _PIPE_ACCESS_INBOUND | (_FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0),
            _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
            _MAX_CLIENTS,
            0,
            65536,
            0,
            None,
        )
        if not handle or handle == _INVALID_HANDLE_VALUE:
            self._creation_error=ctypes.get_last_error()
            self._log(log_text('midi_pipe.creation_failed',error=self._creation_error))
            return None
        return handle

    def transport_statistics(self):
        """Read-only diagnostics; no MIDI, PCM or settings mutation."""
        with self._state_lock:
            return tuple(dict(row) for row in self._completed_transport)+tuple(
                reader.statistics() for reader in self._native_readers.values())

    def _finish_worker(self, handle, queued, thread):
        # A retained MIDI boundary is not an acknowledged/discardable frame.
        # A failed continuation keeps its stream/pipe/plan borrowers alive.
        with self._state_lock:
            pending=self._pending_callbacks.get(thread)
            if pending is not None and not pending.completed:return False
        with self._state_lock:consumer=self._native_consumers.get(thread)
        if consumer is not None:
            if not consumer.close():
                self._log(log_text('midi_pipe.native_shutdown_failed'));return False
            with self._state_lock:self._native_consumers.pop(thread,None)
        if queued is not None:
            with self._state_lock:
                statistics=queued.statistics()
                if not queued.close():
                    self._log(log_text('midi_pipe.native_shutdown_failed'))
                    return False
                self._native_readers.pop(thread,None)
                self._completed_transport.append(statistics)
                del self._completed_transport[:-_MAX_CLIENTS]
        _kernel32.DisconnectNamedPipe(handle)
        _kernel32.CloseHandle(handle)
        with self._state_lock:self._workers.pop(thread,None)
        self._connection_slots.release()
        return True

    def _client_allowed(self, handle):
        """Fail closed before reading a frame; no callback impersonation."""
        session=ctypes.c_uint32()
        allowed=bool(_kernel32.GetNamedPipeClientSessionId(handle,ctypes.byref(session))) and session.value==self._session_id
        if not allowed:self._log(log_text('midi_pipe.session_rejected'))
        return allowed

    @staticmethod
    def _read_exact(handle: int, size: int) -> bytes | None:
        _read_clock.completed=None
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            buffer = (ctypes.c_ubyte * remaining)()
            received = ctypes.c_uint32()
            reader=_timed_reader()
            stamp=ctypes.c_uint64()
            success=reader(handle,buffer,remaining,ctypes.byref(received),ctypes.byref(stamp)) if reader is not None else _kernel32.ReadFile(handle, buffer, remaining, ctypes.byref(received), None)
            if not success:
                return None
            if not received.value:
                return None
            if received.value>remaining:
                return None # A malformed reader must not read beyond its buffer.
            # Keep an owned binary copy, including embedded NULs, without a
            # ctypes slice's GIL-held Python integer-list allocation.
            chunks.append(ctypes.string_at(ctypes.addressof(buffer),received.value))
            remaining -= received.value
            if reader is not None:_read_clock.completed=int(stamp.value)
        return b"".join(chunks)

    @staticmethod
    def received_timestamp():
        return getattr(_read_clock,"completed",None)

    @staticmethod
    def received_origin():
        """Per-frame immutable metadata; never reused for a following frame."""
        return getattr(_read_clock,"origin",None)

    @staticmethod
    def received_port():
        """Port owned by this prepared input frame; ordinary pipe input is0."""
        return getattr(_read_clock,"port",None)

    @staticmethod
    def deliver_prepared(callback,report):
        """Legacy suffix with its already-validated original frame metadata."""
        completed=getattr(_read_clock,'completed',None);origin=getattr(_read_clock,'origin',None);port=getattr(_read_clock,'port',None)
        input_port=report.get('input_port',report.get('direct_port',0))
        if type(input_port) is not int or not 0<=input_port<8:raise ValueError('prepared MIDI input port')
        try:
            _read_clock.completed=report['read_qpc'];_read_clock.origin=dict(report['origin']);_read_clock.port=input_port
            return callback(report['remaining_payload'],report['trace'] or None)
        finally:_read_clock.completed=completed;_read_clock.origin=origin;_read_clock.port=port

    def _deliver_midi(self,payload,sequence):
        """One callback invocation, then resume its exact retained suffix.

        The caller may not read a later frame while this boundary is pending.
        Stop timeout retains ownership; it does not silently abandon bytes.
        """
        result=self._on_midi(payload,sequence)
        if not callable(getattr(result,'resume',None)):return result
        thread=threading.current_thread()
        with self._state_lock:self._pending_callbacks[thread]=result
        self.complete_submission(result)
        with self._state_lock:self._pending_callbacks.pop(thread,None)
        return result

    @staticmethod
    def complete_submission(result):
        """Shared exact-suffix drain; caller retains ownership on exception."""
        if not callable(getattr(result,'resume',None)):return result
        while not result.completed:
            result.resume()
            if not result.completed:time.sleep(.001)
        return result

    def _set_origin(self, values, completed):
        if values is None:
            _read_clock.origin={"quality":"legacy","read_qpc":completed}
            return
        ingress,frequency,epoch,sequence,trace,losses,pid,flags=values
        valid=(frequency==self._frequency and ingress>0 and epoch>0 and pid>0 and sequence>0
               and (flags==0 or flags&~7==0x80000000) and type(completed) is int and ingress<=completed+1)
        faults=0;sequence_faults=0;loss_delta=0
        if valid:
            with self._origin_lock:
                previous=self._origin_sequences.get((pid,epoch))
                if previous is not None and sequence!=previous[0]+1:sequence_faults=1
                loss_delta=max(0,int(losses)-(previous[1] if previous is not None else 0))
                if previous is not None and losses<previous[1]:valid=False
                self._origin_sequences[pid,epoch]=(sequence,int(losses))
                faults=loss_delta+sequence_faults
        _read_clock.origin=dict(quality="driver" if valid else "invalid",ingress_qpc=min(ingress,completed) if valid else None,
            read_qpc=completed,frequency=frequency,pid=pid,epoch=epoch,sequence=sequence,transport_faults=faults,
            transport_loss_total=int(losses),sequence_faults=sequence_faults,
            native_read=_timed_origin is not None)
        _read_clock.port=flags&7 if flags&~7==0x80000000 else 0

    def _serve_client(self, handle: int) -> None:
        _timed_reader()
        native_frame=_timed_frame
        native_origin=_timed_origin
        if native_origin is None:native_frame=None # older DLL: Python MID3 decoder preserves MIDI
        buffer=(ctypes.c_ubyte*_MAX_FRAME_BYTES)() if native_frame is not None else None
        size=ctypes.c_uint();sequence=ctypes.c_int64();stamp=ctypes.c_uint64()
        origin=(ctypes.c_uint64*8)()
        queued=None
        scheduling=MidiThreadScheduling(_midi_scheduling_backend())
        try:
            scheduling.start()
            if native_origin is not None:
                try:
                    queued=NativeMidiQueue(_timed_library,handle)
                    if not queued.pointer:queued=None
                    else:queued.scheduling=scheduling
                except AttributeError:queued=None # Older DLL, exact-frame fallback.
            if queued is not None:
                with self._state_lock:
                    self._native_readers[threading.current_thread()]=queued
                    if self._stopping.is_set():queued.request_stop()
                if self._native_client_factory is not None:
                    # Explicit candidate opt-in. A failed factory must leave
                    # the queue unaccepted or retain its own retry owner; do
                    # not silently run legacy audio beside a common kernel.
                    try:
                        consumer=self._native_client_factory(queued)
                        if consumer is None:raise RuntimeError(tr('runtime.midi_executor_start_failed'))
                        with self._state_lock:
                            self._native_consumers[threading.current_thread()]=consumer
                            if self._stopping.is_set():consumer.request_stop()
                        consumer.serve()
                    except Exception as exc:self._log(log_text('midi_pipe.callback_failed',error=exc))
                    return
                while not self._stopping.is_set():
                    count=queued.read()
                    if count<=0:
                        if count<0:self._log(log_text('midi_pipe.invalid_frame'))
                        return
                    for index in range(count):
                        if self._stopping.is_set():return
                        start=index*13
                        kind,length,offset,trace,completed=queued.descriptors[start:start+5]
                        _read_clock.completed=int(completed)
                        self._set_origin(tuple(queued.descriptors[start+5:start+13]) if kind==3 else None,int(completed))
                        payload=queued.payload(offset,length)
                        signed_trace=ctypes.c_int64(trace).value
                        try:self._deliver_midi(payload,signed_trace if kind>=2 and signed_trace else None)
                        except Exception as exc:
                            self._log(log_text('midi_pipe.callback_failed',error=exc))
                            if threading.current_thread() in self._pending_callbacks:return
                return
            while not self._stopping.is_set():
                _read_clock.origin=None
                if native_frame is not None:
                    reader=native_origin or native_frame
                    args=(handle,buffer,_MAX_FRAME_BYTES,ctypes.byref(size),ctypes.byref(sequence),ctypes.byref(stamp))
                    result=reader(*args,origin) if native_origin else reader(*args)
                    if result<=0:
                        if result<0:self._log(log_text('midi_pipe.invalid_frame'))
                        return
                    _read_clock.completed=int(stamp.value)
                    self._set_origin(tuple(origin) if result==3 else None,int(stamp.value))
                    payload=ctypes.string_at(buffer,size.value)
                    try:self._deliver_midi(payload,int(sequence.value) if result>=2 and sequence.value else None)
                    except Exception as exc:
                        self._log(log_text('midi_pipe.callback_failed',error=exc))
                        if threading.current_thread() in self._pending_callbacks:return
                    continue
                header = self._read_exact(handle, _HEADER.size)
                if header is None:
                    return
                magic, length = _HEADER.unpack(header)
                if magic not in (_FRAME_MAGIC, _TRACE_FRAME_MAGIC, _TIMED_FRAME_MAGIC) or not 0 < length <= _MAX_FRAME_BYTES:
                    self._log(log_text('midi_pipe.invalid_frame'))
                    return
                trace_sequence = None
                origin_values=None
                if magic==_TIMED_FRAME_MAGIC:
                    origin_bytes=self._read_exact(handle,_ORIGIN.size)
                    if origin_bytes is None:return
                    origin_values=_ORIGIN.unpack(origin_bytes)
                    trace_sequence=origin_values[4] or None
                if magic == _TRACE_FRAME_MAGIC:
                    sequence_bytes = self._read_exact(handle, _TRACE_SEQUENCE.size)
                    if sequence_bytes is None:
                        return
                    trace_sequence = _TRACE_SEQUENCE.unpack(sequence_bytes)[0]
                payload = self._read_exact(handle, length)
                if payload is None:
                    return
                completed=self.received_timestamp()
                if completed is None:
                    tick=ctypes.c_int64();_kernel32.QueryPerformanceCounter(ctypes.byref(tick));completed=tick.value
                    _read_clock.completed=completed
                self._set_origin(origin_values,completed)
                try:
                    self._deliver_midi(payload, trace_sequence)
                except Exception as exc:
                    self._log(log_text('midi_pipe.callback_failed',error=exc))
                    if threading.current_thread() in self._pending_callbacks:return
        finally:
            scheduling.close() # This worker owns registration AND termination.
            # Failed native join retains pipe + native owner for stop retry.
            self._finish_worker(handle,queued,threading.current_thread())

    def _serve_prepared(self,handle,queued,consumer):
        # The native Connect/Read/MMCSS owner and execution attachment already
        # exist. This Python worker only owns metadata EOF/detach/retirement.
        try:consumer.serve()
        except Exception as error:self._log(log_text('midi_pipe.callback_failed',error=error))
        finally:self._finish_worker(handle,queued,threading.current_thread())

    def _prepare_listener(self,*,first):
        handle=self._new_pipe(first=first)
        if handle is None:
            self._connection_slots.release()
            raise OSError(self._creation_error,tr('midi_pipe.creation_failed',error=self._creation_error))
        queued=consumer=worker=None
        published=False
        try:
            queued=NativeMidiQueue(_timed_library,handle,listener_session=self._session_id)
            if not queued.pointer:raise RuntimeError(tr('runtime.midi_executor_start_failed'))
            if not queued.statistics().get('listener_ready'):raise RuntimeError(tr('runtime.midi_executor_start_failed'))
            consumer=self._native_client_factory(queued)
            if consumer is None:raise RuntimeError(tr('runtime.midi_executor_start_failed'))
            worker=threading.Thread(target=self._serve_prepared,args=(handle,queued,consumer),
                                    name='ABMSynthMidiClient',daemon=True)
            with self._state_lock:
                if self._stopping.is_set():raise RuntimeError(tr('midi_pipe.startup_timeout'))
                self._workers[worker]=handle;self._native_readers[worker]=queued;self._native_consumers[worker]=consumer
                published=True
                worker.start()
        except BaseException:
            if queued is not None:queued.request_stop()
            # A start wrapper may raise AFTER actual launch. Its finally owns
            # teardown, so never detach/free beneath that borrower.
            if published:
                if worker.ident is None:self._finish_worker(handle,queued,worker)
            else:
                # Register all partial roots before cleanup. A failed detach or
                # native join remains retriable by the original server.stop.
                worker=threading.Thread(name='ABMSynthMidiClientRetained')
                with self._state_lock:
                    self._workers[worker]=handle
                    if queued is not None:self._native_readers[worker]=queued
                    if consumer is not None:self._native_consumers[worker]=consumer
                self._finish_worker(handle,queued,worker)
            raise

    def _prepared_accept_loop(self):
        # Eight is the EXISTING concurrent connection resource policy, not a
        # lifetime connection limit. Replenish after exact EOF/metadata ACK only.
        # All eight readers and Factory clients are prepared before _ready.
        first=True;prepared=0
        try:
            while not self._stopping.is_set():
                if not self._connection_slots.acquire(timeout=.1):continue
                if self._stopping.is_set():
                    self._connection_slots.release();return
                self._prepare_listener(first=first);first=False;prepared+=1
                if prepared==_MAX_CLIENTS:
                    self._ready.set();self._log(log_text('midi_pipe.ready',clients=_MAX_CLIENTS))
        except BaseException as error:
            self._startup_error=str(error)
            self._stopping.set()
            with self._state_lock:
                for queued in self._native_readers.values():queued.request_stop()
                for consumer in self._native_consumers.values():consumer.request_stop()
            self._ready.set()
            self._log(log_text('midi_pipe.callback_failed',error=error))

    def _accept_loop(self) -> None:
        first_listener = True
        while not self._stopping.is_set():
            if not self._connection_slots.acquire(timeout=0.1):
                continue
            # Stop can occur while all eight slots are occupied. Acquiring a
            # slot after they retire is not permission to create a new listener.
            if self._stopping.is_set():
                self._connection_slots.release()
                return
            handle = self._new_pipe(first=first_listener)
            if handle is None:
                self._connection_slots.release()
                if first_listener:
                    self._startup_error = tr('midi_pipe.creation_failed',error=self._creation_error)
                    self._ready.set()
                return
            with self._state_lock:
                closing=self._stopping.is_set()
                if not closing:self._listener_handle = handle
            if closing:
                _kernel32.CloseHandle(handle)
                self._connection_slots.release()
                return
            if first_listener:
                first_listener = False
                self._ready.set()
                self._log(log_text('midi_pipe.ready',clients=_MAX_CLIENTS))
            connected = _kernel32.ConnectNamedPipe(handle, None)
            if not connected and ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
                _kernel32.CloseHandle(handle)
                with self._state_lock:
                    self._listener_handle = None
                self._connection_slots.release()
                continue
            with self._state_lock:
                self._listener_handle = None
            if self._stopping.is_set():
                _kernel32.DisconnectNamedPipe(handle)
                _kernel32.CloseHandle(handle)
                self._connection_slots.release()
                return
            if not self._client_allowed(handle):
                _kernel32.DisconnectNamedPipe(handle)
                _kernel32.CloseHandle(handle)
                self._connection_slots.release()
                continue
            worker = None
            launch_error = None
            retained = False
            with self._state_lock:
                closing=self._stopping.is_set()
                if not closing:
                    # Publish AND start under stop's ownership lock. Otherwise
                    # stop could miss a just-accepted, not-yet-started borrower.
                    try:
                        worker = threading.Thread(target=self._serve_client, args=(handle,), name="ABMSynthMidiClient", daemon=True)
                        self._workers[worker] = handle
                        worker.start()
                    except Exception as error:
                        launch_error = error
                        # A wrapper may raise AFTER starting the real borrower.
                        # An assigned native id proves it ran even if it has
                        # already exited; its finally/stop path owns this pipe.
                        retained = worker is not None and (worker.ident is not None or worker.is_alive())
                        if retained:
                            self._stopping.set()
                        elif worker is not None:
                            self._workers.pop(worker, None)
            if closing or (launch_error is not None and not retained):
                _kernel32.DisconnectNamedPipe(handle)
                _kernel32.CloseHandle(handle)
                self._connection_slots.release()
            if launch_error is not None:
                # Logging is outside the ownership lock and after cleanup.
                # Partial starts retain their resources for a normal stop retry.
                self._log(log_text('midi_pipe.callback_failed',error=launch_error))
                return
            if closing:
                return

