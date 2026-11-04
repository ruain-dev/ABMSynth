"""Passive tempo inference. Native PCM copy -> bounded IPC -> separate CPU owner.

No BASS/Qt imports in the worker. Never modifies audio or incoming MIDI.
Library-derived operations live in vendor.librosa_tempo;policy lives here.
"""
from __future__ import annotations
import ctypes
import math
import multiprocessing
import queue
import threading
import time
from pathlib import Path
from collections import deque
import numpy as np
from i18n import tr

class NativeTempoCapture:
    """bass.h DSPPROC,owned until RemoveDSP/terminal FREE completes.

    Read/close serialized off audio. Registration owns a separate native ref.
    Final mix float format is supplied by OutputMeter's existing validated tap.
    """
    def __init__(self,bass):
        self.bass=bass;self.stream=0;self.dsp=0;self.pointer=None;self.library=None
        self.lock=threading.RLock();self.epoch=0;self.rate=0;self.buffer=None
    def install(self,stream,rate,channels):
        from bass_bindings import DSPPROC
        self.close()
        import os
        dll=ctypes.CDLL(os.environ.get('ABMSYNTH_TEMPO_CAPTURE_DLL',str(Path(__file__).with_name('abmsynth_tempo_capture.dll'))))
        U=ctypes.c_uint;P=ctypes.c_void_p
        for name,args,result in (
            ('version',[],U),('create',[U,U],P),('arm',[P],ctypes.c_int),
            ('removed',[P],None),('release',[P],None),
            ('read',[P,P,U,P],U)):
            fn=getattr(dll,'tempo_capture_'+name);fn.argtypes=args;fn.restype=result
        if dll.tempo_capture_version()!=1:raise RuntimeError(tr('tempo_display.capture_version'))
        pointer=dll.tempo_capture_create(channels,rate)
        if not pointer:raise MemoryError(tr('tempo_display.capture_allocation'))
        self.library=dll;self.callback=DSPPROC(('tempo_capture_dsp',dll))
        if not dll.tempo_capture_arm(pointer):
            dll.tempo_capture_release(pointer)
            raise RuntimeError(tr('tempo_display.capture_version'))
        dsp=self.bass.BASS_ChannelSetDSP(stream,self.callback,pointer,-12000)
        if not dsp:
            dll.tempo_capture_removed(pointer);dll.tempo_capture_release(pointer)
            raise RuntimeError(tr('tempo_display.capture_failed',error=self.bass.BASS_ErrorGetCode()))
        with self.lock:
            self.stream=stream;self.dsp=dsp;self.pointer=pointer;self.rate=rate
            self.buffer=np.empty(rate*2,dtype=np.float32);self.epoch+=1
    def source_freed(self):
        with self.lock:
            if self.pointer:self.library.tempo_capture_removed(self.pointer)
            self.dsp=0;self.stream=0;self.epoch+=1
    def read(self):
        with self.lock:
            if not self.pointer or not self.stream:return None
            metadata=(ctypes.c_uint64*2)()
            n=self.library.tempo_capture_read(self.pointer,self.buffer.ctypes.data,len(self.buffer),metadata)
            return (self.epoch,self.rate,int(metadata[0]),int(metadata[1]),self.buffer[:n].copy())
    def close(self):
        with self.lock:
            if not self.pointer:return
            if self.dsp and not self.bass.BASS_ChannelRemoveDSP(self.stream,self.dsp):
                raise RuntimeError(tr('tempo_display.capture_remove_failed',error=self.bass.BASS_ErrorGetCode()))
            self.library.tempo_capture_removed(self.pointer)
            self.library.tempo_capture_release(self.pointer)
            self.pointer=None;self.stream=self.dsp=0;self.buffer=None;self.epoch+=1

class TempoResampler:
    """Causal windowed-sinc rate conversion,observation PCM only.

    Twelve input/output-normalized zero crossings per side,512 fractional
    phases. Buffer carries past PCM;available lookahead is at most12/22050s.
    Explicit observation policy,not an audio sample-rate capability limit.
    """
    def __init__(self,rate):
        from vendor.librosa_tempo import SR
        self.ratio=rate/SR;self.direct=rate==SR;self.next=0;self.base=0
        self.half=math.ceil(12*max(1.,self.ratio));self.offsets=np.arange(-self.half,self.half+1)
        self.buffer=np.zeros(self.half,np.float32);self.base=-self.half
        cutoff=.45/max(1.,self.ratio)
        delta=self.offsets[None,:]-np.arange(512)[:,None]/512
        coeff=2*cutoff*np.sinc(2*cutoff*delta)*np.hanning(len(self.offsets))[None,:]
        self.coeff=(coeff/coeff.sum(axis=1,keepdims=True)).astype(np.float32)
    def feed(self,pcm):
        if self.direct:return pcm
        self.buffer=np.r_[self.buffer,pcm];right=self.base+len(self.buffer)-1-self.half
        count=max(0,int(math.floor(right/self.ratio))-self.next+1)
        if not count:return np.array([],np.float32)
        outputs=[]
        for left in range(0,count,2048):
            positions=(self.next+np.arange(left,min(count,left+2048)))*self.ratio
            centers=np.floor(positions).astype(np.int64)
            phase=np.minimum(511,np.rint((positions-centers)*512).astype(np.int64))
            samples=self.buffer[centers[:,None]+self.offsets[None,:]-self.base]
            outputs.append(np.einsum('ij,ij->i',samples,self.coeff[phase],optimize=False))
        self.next+=count;discard=max(0,int(math.floor(self.next*self.ratio))-self.half-self.base)
        self.buffer=self.buffer[discard:].copy();self.base+=discard
        return np.concatenate(outputs)

class TempoEstimator:
    """Worker-only state. No estimate without rhythm and current audio evidence."""
    def __init__(self,rate):
        from vendor.librosa_tempo import StreamingFeatures,SR
        mel=np.load(Path(__file__).parent/'vendor/librosa-analysis/tempo-mel.npy')
        self.features=StreamingFeatures(mel);self.resampler=TempoResampler(rate)
        self.seconds=0.;self.due=0.;self.energy=deque(maxlen=20);self.rate=rate;self.frames=0
    def feed(self,pcm,attacks):
        self.frames+=len(pcm);self.seconds=self.frames/self.rate
        self.energy.append(float(np.mean(pcm.astype(np.float64)**2)) if len(pcm) else 0.)
        self.features.feed(self.resampler.feed(pcm))
        if self.seconds<self.due:return False,None
        self.due=self.seconds+.5
        rms=math.sqrt(sum(self.energy)/len(self.energy)) if self.energy else 0.
        value=self.features.bpm() if attacks>=4 and self.seconds>=1 and rms>1e-6 else None
        return True,float(value) if value is not None and math.isfinite(value) and value>0 else None

def _tempo_worker(inbox,outbox):
    estimator=None;identity=None;expected=0
    while True:
        message=inbox.get()
        if message is None:return
        token,sequence,rate,pcm,attacks=message
        if token!=identity or sequence!=expected:
            estimator=TempoEstimator(rate);identity=token
        expected=sequence+1
        due,value=estimator.feed(pcm,attacks)
        if due:
            result=(token,time.monotonic(),value,sequence)
            try:outbox.put_nowait(result)
            except queue.Full:pass # GUI will expire old results;never block capture.

class TempoAnalysisService:
    """Observation-worker IPC owner;GUI reads an immutable cached number only."""
    def __init__(self):
        self.process=None;self.inbox=self.outbox=None;self.identity=None
        self.sequence=0;self.expected_frame=0;self.gaps=0;self.result=None;self.attacks=deque()
        self.note_cursor=0;self.note_source=None;self.note_valid=False;self.lock=threading.RLock()
        self.failure_until=0.
        self.minimum_result_sequence=0
        # Runtime identities contain object() sentinels. Keep them in this
        # process;pickle cannot preserve their identity across IPC messages.
        self.generation=0
        self.submitted_packets=self.dropped_packets=self.received_results=0
        self.last_result_at=None;self.worker_pid=None
        self._published=None
    def observe_notes(self,batch,now):
        with self.lock:self._observe_notes(batch,now)
    def _observe_notes(self,batch,now):
        if batch is None or not batch.known:
            self.note_valid=False;self.attacks.clear();self._published=None;return
        if batch.source!=self.note_source or not batch.history_complete:
            self.attacks.clear();self.note_cursor=0;self.note_source=batch.source
        for serial,packed,ch,stamp in batch.rows:
            if serial<=self.note_cursor:continue
            if ch==-1:self.attacks.clear();self.result=None;self._published=None;self.identity=None
            elif packed&240==144 and (packed>>16)&127:
                t=stamp/1e9
                if not self.attacks or t-self.attacks[-1]>.001:self.attacks.append(t)
            self.note_cursor=max(self.note_cursor,serial)
        while self.attacks and self.attacks[0]<now-8:self.attacks.popleft()
        self.note_valid=True
    def submit(self,packet,identity):
        if packet is None:return
        epoch,rate,first,gaps,pcm=packet
        if not len(pcm):return
        with self.lock:
            token=(identity,epoch,rate,gaps)
            if token!=self.identity or first!=self.expected_frame:
                self.identity=token;self.result=None;self._published=None;self.sequence+=1
                self.generation+=1
                self.minimum_result_sequence=self.sequence
            self.expected_frame=first+len(pcm)
            if self.process is None or not self.process.is_alive():
                if time.monotonic()<self.failure_until:return
                self.close(reset_notes=False);ctx=multiprocessing.get_context('spawn')
                self.inbox=ctx.Queue(maxsize=2);self.outbox=ctx.Queue(maxsize=2)
                self.process=ctx.Process(target=_tempo_worker,args=(self.inbox,self.outbox),name='ABMSynth-tempo',daemon=True)
                self.process.start();self.failure_until=time.monotonic()+5;self.identity=token
                self.worker_pid=self.process.pid
                self.generation+=1
                self.expected_frame=first+len(pcm)
            sequence=self.sequence;self.sequence+=1
            try:
                self.inbox.put_nowait((self.generation,sequence,rate,pcm,len(self.attacks) if self.note_valid else 0))
                self.submitted_packets+=1
            except queue.Full:
                self.dropped_packets+=1
                self.result=None;self._published=None;self.minimum_result_sequence=self.sequence
            while True:
                try:result=self.outbox.get_nowait()
                except queue.Empty:break
                if result[0]==self.generation and result[3]>=self.minimum_result_sequence:
                    self.received_results+=1;self.last_result_at=result[1]
                    self.result=(self.identity,*result[1:])
                    if self.note_valid:self._published=self.result
    def reading(self,identity):
        # One immutable publication. GUI never waits for process start/join/IPC.
        snapshot=self._published
        if snapshot is None or snapshot[0][0]!=identity or time.monotonic()-snapshot[1]>2:return None
        return snapshot[2]
    def diagnostic(self,identity):
        """Debug capture only. No IPC/native calls or lifecycle locks on GUI."""
        return dict(worker_pid=self.worker_pid,generation=self.generation,
            submitted_packets=self.submitted_packets,dropped_packets=self.dropped_packets,
            received_results=self.received_results,notes_known=self.note_valid,
            recent_attacks=len(self.attacks),bpm=self.reading(identity),
            last_result_age_seconds=None if self.last_result_at is None else time.monotonic()-self.last_result_at)
    def close(self,*,reset_notes=True):
        with self.lock:
            process=self.process
            if process is not None and process.pid is not None:
                try:self.inbox.put_nowait(None)
                except queue.Full:pass
                process.join(.25)
                if process.is_alive():process.terminate();process.join(.25)
                process.close()
            elif process is not None:process.close()
            for q in (self.inbox,self.outbox):
                if q is not None:q.cancel_join_thread();q.close()
            self.process=None;self.inbox=self.outbox=None;self.identity=None;self.result=None;self._published=None
            self.worker_pid=None;self.last_result_at=None
            self.expected_frame=0
            if reset_notes:
                self.note_valid=False;self.attacks.clear();self.note_cursor=0;self.note_source=None
