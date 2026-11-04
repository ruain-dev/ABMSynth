"""Final-output float DSP tap and bounded meter/waveform observations."""

from __future__ import annotations

import ctypes
import math
import threading
import time
import tempfile
import os
import shutil
from pathlib import Path
from collections import deque

import numpy as np

from bass_bindings import BASS_CHANNELINFO, BASS_SAMPLE_FLOAT, BASS_STREAM_DECODE, BASS_STREAMPROC_DEVICE, BASS_SYNC_FREE, DSPPROC, SYNCPROC, load_output_addons, load_recording_codec, NativeChannelTransaction
from settings import RECORDING_EXTENSIONS, RECORDING_BITRATES, recording_output_rate, recording_rate_supported
from event_log import log_text
from i18n import tr


OUTPUT_METER_DSP_PRIORITY = -10000
VISUAL_CAPTURE_RATE = 48000


def render_cadence_statistics(rows, frequency, rate, *, now=None, include_deviation=True):
    """Trailing one-second absolute boundary deviation, centered on the median."""
    if not rows or frequency <= 0 or rate <= 0:
        return {}
    now = time.perf_counter() if now is None else now
    if now - rows[-1][0] / frequency > 1:
        return {}  # Stopped/stale is unavailable, not a zero-jitter claim.
    cutoff = now - 1
    current = [row for row in rows if row[0] / frequency >= cutoff]
    if not current:
        return {}
    result = {"block_ms": float(np.median([row[1] for row in current])) / rate * 1000,
              "blocks": len(current), "observed_at": rows[-1][0]/frequency,
              "window_seconds": min(1., max(0., (current[-1][0] - current[0][0]) / frequency))}
    if not include_deviation:
        return result
    errors = [(stamp - previous[0]) / frequency * 1000 - previous[1] / rate * 1000
              for previous, (stamp, _frames) in zip(rows, rows[1:])
              if stamp > previous[0] and stamp / frequency >= cutoff]
    if len(errors) >= 2:
        errors = np.asarray(errors)
        deviation = np.abs(errors - np.median(errors))
        result.update(jitter_mean_ms=float(np.mean(deviation)),
                      jitter_max_ms=float(np.max(deviation)))
    return result


class OutputAnalysis:
    """Native post-FX MIDI-source loudness and PCM file owner, independent of UI.

    No MIDI channel taps or Python DSP callbacks; no change to drum-key sends.
    Attaches after source FX (priority -11000); shared/device attenuation is
    intentionally outside the measurement. Never uses another final-mix stream.
    """

    MODES = {"momentary": 0, "short_term": 3000 << 16,
             "integrated": 1, "range": 2, "peak": 4, "true_peak": 8}

    def __init__(self, bass):
        self.bass = bass
        self.stream = self.measurement = self.encoder = 0
        self.loud = self.enc = None
        self.recording_path = None
        self.recording_started = 0.0
        self.recording_elapsed = 0.0
        self.recording_paused = False
        self.recording_temp_dir = None
        self.recording_queued = True
        self.last_recording_loss = 0
        self.last_recording_error = False
        self.recording_container = "wav"
        self.recording_format = "float32"
        self.recording_date = None
        self._recording_codecs = {}
        self.manual_encoder_stream = 0

    def bind_stream(self, stream):
        self.close()
        self.stream = int(stream)

    def _load(self):
        if self.loud is None:
            self.loud, self.enc = load_output_addons()

    def start_measurement(self):
        if self.measurement:
            return
        if not self.stream:
            raise RuntimeError(tr("output_tools.no_stream"))
        self._load()
        self.measurement = self.loud.BASS_Loudness_Start(
            self.stream, (3000 << 16) | 1 | 2 | 4 | 8, -11000)
        if not self.measurement:
            raise RuntimeError(tr("output_tools.measure_failed", error=self.bass.BASS_ErrorGetCode()))

    def stop_measurement(self):
        if self.measurement:
            self.loud.BASS_Loudness_Stop(self.measurement)
            self.measurement = 0

    def levels(self):
        if not self.measurement:
            return {}
        result = {}
        for name, mode in self.MODES.items():
            value = ctypes.c_float()
            if self.loud.BASS_Loudness_GetLevel(self.measurement, mode, ctypes.byref(value)):
                result[name] = float(value.value)
        return result

    @property
    def has_draft(self):
        return self.recording_temp_dir is not None

    @property
    def recording_state(self):
        if self.encoder and not self.recording_paused:
            return "recording"
        return "paused" if self.has_draft else "idle"

    def start_recording(self, path=None, *, format="float32", dither=True, rf64=True, container="wav",
                        compression=5, vorbis_quality=6, mp3_quality=2, opus_complexity=10, bitrate=192, rate_mode="vbr", queued=True, manual=False, mono=False):
        if self.has_draft:
            raise RuntimeError(tr("recording.existing_draft"))
        if self.encoder or not self.stream:
            raise RuntimeError(tr("output_tools.no_stream"))
        self._load()
        if format not in ("float32", "pcm32", "pcm24", "pcm16"):
            raise ValueError("recording_format")
        if container not in RECORDING_EXTENSIONS:
            raise ValueError("recording_container")
        source_info = BASS_CHANNELINFO()
        if not self.bass.BASS_ChannelGetInfo(self.stream,ctypes.byref(source_info)):
            raise RuntimeError(tr('output_tools.no_stream'))
        if recording_rate_supported(container,source_info.freq) is False:
            raise RuntimeError(tr('recording.rate_unsupported',rate=source_info.freq,format=tr('recording.container_'+container)))
        if (not 0 <= compression <= 8 or not -1 <= vorbis_quality <= 10 or not 0 <= mp3_quality <= 9
                or not 0 <= opus_complexity <= 10 or bitrate not in RECORDING_BITRATES or rate_mode not in ("vbr", "cbr")):
            raise ValueError("recording codec options")
        pcm = container in ("wav", "aiff")
        if not pcm and container not in self._recording_codecs:
            self._recording_codecs[container] = load_recording_codec(container)
        if container in ("flac", "ogg_flac") and format == "float32":
            format = "pcm24"  # FLAC cannot preserve arbitrary floating samples.
        mp3_resample = ""
        if container == "mp3":
            info = BASS_CHANNELINFO()
            if not self.bass.BASS_ChannelGetInfo(self.stream, ctypes.byref(info)):
                raise RuntimeError(tr("output_tools.no_stream"))
            encoded_rate = recording_output_rate(container, info.freq)
            if encoded_rate != info.freq:
                mp3_resample = f" --resample {encoded_rate / 1000:g}"
        if path is None:
            self.recording_temp_dir = Path(tempfile.mkdtemp(prefix="ABMSynth-recording-"))
            path = self.recording_temp_dir / "recording.wav"
        # bassenc.h: explicit final source FX position. Queue never waits in
        # audio callback. Format is fixed for a take, including resumed segments.
        self.bass.BASS_SetConfig(0x10300, -11000 & 0xffffffff)
        self.last_recording_loss = 0
        self.last_recording_error = False
        # bassenc.h QUEUE is used for real-time recording only. Offline
        # decoding may outrun a finite queue; synchronous encoding in its
        # isolated render process prevents lost samples and never stalls live audio.
        flags = 0x80000000 | (0x200 if queued else 0)
        # bassenc.h PAUSE disables only the DSP feed, not Encode_Write.
        flags |= 0x20 if manual else 0
        source = self.stream
        if manual and mono:
            # bass.h STREAMPROC_DUMMY=nullptr: format-only float/decode stream.
            # Encode_Write bypasses the source DSP (including mono conversion),
            # so its format must genuinely be 1ch; the worker supplies L/R mean.
            info = BASS_CHANNELINFO()
            if not self.bass.BASS_ChannelGetInfo(self.stream, ctypes.byref(info)):
                raise RuntimeError(tr("output_tools.no_stream"))
            self.manual_encoder_stream = self.bass.BASS_StreamCreate(info.freq,1,
                BASS_SAMPLE_FLOAT|BASS_STREAM_DECODE,None,None)
            if not self.manual_encoder_stream:
                raise RuntimeError(tr("output_tools.no_stream"))
            source = self.manual_encoder_stream
        self.recording_queued = bool(queued)
        conversion = {"float32": 0, "pcm32": 8, "pcm24": 6, "pcm16": 4}[format]
        if pcm:
            # bassenc.h AIFF=0x4000; RF64 is WAV-only. AIFF float uses
            # AIFC/fl32, integer AIFF uses big endian, native encoder owns both.
            self.encoder = self.enc.BASS_Encode_StartPCMFile(source,
                flags | (0x4000 if container == "aiff" else 0x80 if rf64 else 0)
                | conversion | (0x8000 if dither and format != "float32" else 0), str(path))
        else:
            options = {
                "flac": f"-{compression}", "ogg_flac": f"--ogg -{compression}",
                "vorbis": f"-q {vorbis_quality}",
                "opus": f"--bitrate {bitrate} --{'vbr' if rate_mode == 'vbr' else 'hard-cbr'} --comp {opus_complexity}",
                "mp3": f"{'-V ' + str(mp3_quality) if rate_mode == 'vbr' else '-b ' + str(bitrate)} -q 2 --noreplaygain{mp3_resample}",
            }[container]
            if container in ("flac", "ogg_flac"):
                flags |= conversion | (0x8000 if dither else 0)
            # Lossy codecs accept float directly; irrelevant PCM flags are not
            # sent. The native encoder owns any required codec-rate resampling.
            self.encoder = self._recording_codecs[container][1](source, options, flags, str(path))
        if not self.encoder:
            error_code = self.bass.BASS_ErrorGetCode()
            if self.manual_encoder_stream:
                self.bass.BASS_StreamFree(self.manual_encoder_stream)
                self.manual_encoder_stream = 0
            if self.recording_temp_dir is not None:
                failed_path = self.recording_temp_dir / "recording.wav"
                if failed_path.exists():
                    failed_path.unlink()
                self.recording_temp_dir.rmdir()
                self.recording_temp_dir = None
            raise RuntimeError(tr("output_tools.record_failed", error=error_code))
        self.recording_path = str(path)
        self.recording_container = container
        self.recording_format = format
        self.recording_date = time.localtime()
        self.recording_started = time.monotonic()
        self.recording_elapsed = 0.0
        self.recording_paused = bool(manual)

    def feed_recording(self, buffer, length):
        """Offline worker only. Explicitly checked native sample delivery."""
        if not self.encoder or not self.enc.BASS_Encode_Write(self.encoder, buffer, length):
            raise RuntimeError(tr("output_tools.record_failed", error=self.bass.BASS_ErrorGetCode()))

    def pause_recording(self):
        if not self.encoder or self.recording_paused:
            return
        if not self.enc.BASS_Encode_SetPaused(self.encoder, True):
            raise RuntimeError(tr("recording.pause_failed", error=self.bass.BASS_ErrorGetCode()))
        self.recording_elapsed += time.monotonic() - self.recording_started
        self.recording_paused = True

    def resume_recording(self):
        if not self.encoder or not self.recording_paused:
            raise RuntimeError(tr("recording.source_changed"))
        if not self.enc.BASS_Encode_SetPaused(self.encoder, False):
            raise RuntimeError(tr("recording.resume_failed", error=self.bass.BASS_ErrorGetCode()))
        self.recording_started = time.monotonic()
        self.recording_paused = False

    def recording_status(self):
        if not self.encoder:
            return None
        # QUEUE_FAIL is unavailable for synchronous encoders (NOTAVAIL),
        # not evidence of lost audio. Do not query a queue that does not exist.
        lost = int(self.enc.BASS_Encode_GetCount(self.encoder, 5)) if self.recording_queued else 0
        if lost == 0xffffffffffffffff:
            lost = 0
            self.last_recording_error = True
        self.last_recording_loss = max(self.last_recording_loss, lost)
        self.last_recording_error |= self.enc.BASS_Encode_IsActive(self.encoder) not in (1, 3)
        # Active wall time excludes paused intervals. Queue counters measure
        # asynchronously delivered data, not an atomic capture-duration clock.
        return {"seconds": self.recording_elapsed + (0 if self.recording_paused else time.monotonic() - self.recording_started),
                "lost_bytes": self.last_recording_loss, "failed": self.last_recording_error}

    def stop_recording(self):
        if not self.encoder:
            return
        self.recording_status()
        if not self.recording_paused:
            self.recording_elapsed += time.monotonic() - self.recording_started
        self.recording_paused = True
        # STOP_WAIT drains queued bytes and finalizes WAV before returning.
        # Called on control/shutdown thread, never from an audio/MIDI callback.
        handle, self.encoder = self.encoder, 0
        try:
            if not self.enc.BASS_Encode_StopEx(handle, 2):
                self.last_recording_error = True
                raise RuntimeError(tr("output_tools.stop_failed", error=self.bass.BASS_ErrorGetCode()))
        finally:
            if self.manual_encoder_stream:
                self.bass.BASS_StreamFree(self.manual_encoder_stream)
                self.manual_encoder_stream = 0

    def _remove_draft(self):
        directory = self.recording_temp_dir
        if directory is None:
            return
        source = Path(self.recording_path)
        if source.parent.resolve() != directory.resolve() or source.name != "recording.wav":
            raise ValueError("recording draft ownership")
        source.unlink(missing_ok=True)
        directory.rmdir()
        self.recording_temp_dir = None

    def save_recording(self, path):
        """Control worker only: flush queue, atomic copy, then release draft.

        Cancellation before calling this leaves the paused encoder intact.
        Any failure retains the source draft for retry/recovery.
        """
        if not self.has_draft or self.recording_state == "recording":
            raise RuntimeError(tr("recording.save_requires_pause"))
        self.stop_recording()
        target = Path(path)
        if target.resolve().is_relative_to(self.recording_temp_dir.resolve()):
            raise ValueError(tr("recording.invalid_destination"))
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".ABMSynth-recording-", suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
            shutil.copyfile(self.recording_path, temporary)
            with temporary.open("r+b") as file:
                os.fsync(file.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._remove_draft()
        self.recording_path = str(target)
        self.recording_paused = False
        return str(target)

    def discard_recording(self):
        self.stop_recording()
        self._remove_draft()
        self.recording_path = None
        self.recording_paused = False

    def close(self):
        try:
            self.stop_recording()
        finally:
            self.stop_measurement()
            self.stream = 0


def note_delivery_statistics(array, frequency, rate, now):
    """Received->sample-referenced PCM changes: mean1s,max5s,producer epochs.

    Integer QPC differences precede float conversion. Each pair's uncertainty
    is the longer actual containing block plus two cross-thread clock ticks.
    Full bounded history supplies one previous reference per producer.
    """
    if not len(array) or frequency<=0 or rate<=0:return {"note_samples":0}
    recent=array[:,6]/frequency>=now-1
    peak_recent=array[:,6]/frequency>=now-5
    result={"note_samples":int(np.count_nonzero(recent))}
    if np.any(array[:,13]==0):return dict(result,note_origin_missing=int(np.count_nonzero(array[:,13]==0)))
    valid=(array[:,1]<=array[:,9])&(array[:,9]<=array[:,2])&(array[:,2]<=array[:,6])
    if not valid.all():return dict(result,note_origin_invalid=int(np.count_nonzero(~valid)))
    delays=(array[:,6]-array[:,1]).astype(np.float64)/frequency*1000
    if recent.any():
        result.update(note_latency_ms=float(np.mean(delays[recent])),
            note_ingress_wait_ms=float(np.mean((array[recent,9]-array[recent,1]).astype(np.float64)/frequency*1000)))
    # Keep true end-to-end latency above;remove only the explicit humanization
    # reservation from fault variation. Older14-column sources mean zero.
    # All rows here have qualified accepted positions in their containing
    # final PCM block. Compare the same sample reference,not only the block
    # timestamp:TIME events can lie at different offsets inside that block.
    # Fixed stereo FLOAT policy is8 bytes/frame. Subtract uint64 byte positions
    # before float conversion so long-running streams retain sample precision.
    sample_offsets=(array[:,3]-array[:,8]).astype(np.float64)/(8*rate)*1000
    fault_delays=delays+sample_offsets
    if array.shape[1]>14:fault_delays-=array[:,14].astype(np.float64)/frequency*1000
    identities=array[:,(10,11,13)]
    variations=[];excesses=[];peaks=[]
    for identity in np.unique(identities[peak_recent],axis=0):
        mask=np.all(identities==identity,axis=1)
        indices=np.flatnonzero(mask&peak_recent)
        older=np.flatnonzero(mask&~peak_recent)
        if older.size:indices=np.concatenate((older[-1:],indices))
        if len(indices)<2:continue
        variation=np.abs(np.diff(fault_delays[indices]))
        peaks.append(variation)
        current=recent[indices[1:]]
        block=array[indices,7].astype(np.float64)/rate*1000
        bound=np.maximum(block[1:],block[:-1])+2/frequency*1000
        if current.any():
            variations.append(variation[current]);excesses.append(np.maximum(0,variation-bound)[current])
    if variations:
        variation=np.concatenate(variations)
        result.update(note_jitter_mean_ms=float(np.mean(variation)),
            note_timing_excess_ms=float(np.max(np.concatenate(excesses))))
    if peaks:result['note_jitter_max_ms']=float(np.max(np.concatenate(peaks)))
    return result


class PlaybackCoverage:
    """Gap boundaries and lifetime history; never a lifetime measurement veto.

    Known input faults cut one producer's segment. Unattributed native loss
    cuts all segments conservatively. Only post-boundary admission timestamps
    are eligible, so delayed pre-gap frames cannot masquerade as new samples.
    """
    fields=("note_omitted","note_origin_missing","note_transport_loss","note_origin_invalid")
    def __init__(self):
        self.boundaries={};self.global_boundary=0;self.losses={}
        self.native_counts={};self.incidents=dict.fromkeys(self.fields,0)
        self.latest=dict.fromkeys(self.fields,0)
        self.bad_tickets={}
        self.loss_notices=deque(maxlen=128);self.loss_notice_sequences={};self.loss_notice_overflow=0

    def fault(self,field,now,source=None,count=1):
        self.incidents[field]+=max(1,int(count));self.latest[field]=int(now)
        boundary=int(now)+1 # cross-thread QPC ordering uncertainty
        if source is None:self.global_boundary=max(self.global_boundary,boundary)
        else:self.boundaries[source]=max(self.boundaries.get(source,0),boundary)

    def transport(self,source,total,now):
        total=int(total);previous=self.losses.get(source,0)
        if total>previous:self.fault("note_transport_loss",now,source,total-previous)
        # Wire headers can lag a newer shared-counter read. Keep a high-water
        # mark; wire-order decreases are validated by the pipe reader itself.
        self.losses[source]=max(previous,total)

    def observe(self,origin,now,health=True):
        source=(origin.get("pid",0),origin.get("epoch",0))
        if not all(source):source=None
        quality=origin.get("quality")
        if quality!="driver":
            self.fault("note_origin_missing" if quality=="legacy" else "note_origin_invalid",now,source)
            return
        if not health:self.fault("note_origin_invalid",now,source)
        if "transport_loss_total" in origin:
            self.transport(source,origin["transport_loss_total"],now)
        if origin.get("sequence_faults",0) or ("transport_loss_total" not in origin and origin.get("transport_faults",0)):
            self.fault("note_transport_loss",now,source)
            sequence=int(origin.get('sequence',0))
            if sequence!=self.loss_notice_sequences.get(source):
                if len(self.loss_notices)==self.loss_notices.maxlen:self.loss_notice_overflow+=1
                self.loss_notices.append((source,sequence));self.loss_notice_sequences[source]=sequence

    def native(self,meta,now):
        for field,index in (("note_omitted",2),("note_origin_missing",6),("note_origin_invalid",8)):
            value=int(meta[index]);previous=self.native_counts.get(field,0)
            if value>previous:self.fault(field,now,count=value-previous)
            self.native_counts[field]=value

    def eligible_mask(self,array):
        keep=array[:,1]>self.global_boundary
        for source,boundary in self.boundaries.items():
            if source is None:continue
            same=(array[:,10]==source[0])&(array[:,11]==source[1])
            keep&=~same|(array[:,1]>boundary)
        return keep

    def select(self,array,now):
        eligible=array[self.eligible_mask(array)]
        valid=(eligible[:,13]>0)&(eligible[:,1]<=eligible[:,9])&(eligible[:,9]<=eligible[:,2])&(eligible[:,2]<=eligible[:,6])
        # An invalid new record establishes a boundary once. It is removed on
        # subsequent snapshots by admission time, not repeatedly rediscovered.
        for row in eligible[~valid]:
            source=(int(row[10]),int(row[11]))
            if int(row[0])>self.bad_tickets.get(source,-1):
                self.fault("note_origin_missing" if row[13]==0 else "note_origin_invalid",now,source)
                self.bad_tickets[source]=int(row[0])
        clean=eligible[valid]
        return clean[self.eligible_mask(clean)]

    def status(self,now,frequency):
        return {key:value for field in self.fields for key,value in (
            (field,int(bool(self.latest[field] and now-self.latest[field]<=frequency))),
            (field+"_incidents",self.incidents[field]))}


class PlaybackTiming:
    """Received Note On -> final source PCM block, never a Python audio callback.

    bass.h BASS_POS_DSP, DSPPROC, SYNCPROC; bassmidi.h MIDI_EVENT_NOTE sync.
    Control submissions are serialized by EngineRuntime; BASS owns render
    serialization. Detach all borrowers before deleting native state.
    """
    def __init__(self, bass, log, native_dll=None):
        from midi_input_runtime import MidiLossLog
        self.bass, self.log = bass, log
        self._loss_log=MidiLossLog(log)
        self.path = Path(native_dll or os.environ.get("ABMSYNTH_OBSERVATION_DLL",Path(__file__).with_name("abmsynth_dsp.dll")))
        self._transaction=NativeChannelTransaction(bass,self.path)
        self.dll = self.pointer = None
        self.stream = self.dsp = 0
        self.begin_dsp = 0
        self.syncs = []
        self.callbacks = []
        self.lock = threading.RLock()
        self.rate = 0
        self._rows=(ctypes.c_uint64*(8192*9))()
        self._meta=(ctypes.c_uint64*6)()
        self._origin_rows=(ctypes.c_uint64*(8192*14))()
        self._origin_meta=(ctypes.c_uint64*9)()
        self._human_rows=(ctypes.c_uint64*(8192*15))()
        self._health_rows=(ctypes.c_uint64*(1024*3))()
        self.coverage=PlaybackCoverage()
        self._filter_gate=None
        self.position_qualified=False
        self.position_version=1
        self._filter_barrier_gate=None

    def _prepare(self):
        if self.dll is not None: return
        dll = ctypes.CDLL(str(self.path.resolve()))
        dll.playback_timing_version.restype = ctypes.c_uint
        if dll.playback_timing_version() != 1: raise RuntimeError("playback timing ABI")
        dll.playback_timing_origin_version.restype=ctypes.c_uint
        if dll.playback_timing_origin_version()!=1:raise RuntimeError("playback origin ABI")
        for name,args,result in (
            ("playback_timing_create",[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint],ctypes.c_void_p),
            ("playback_timing_destroy",[ctypes.c_void_p],None),
            ("playback_timing_rate",[ctypes.c_void_p,ctypes.c_double],None),
            ("playback_timing_reset",[ctypes.c_void_p],None),
            ("playback_timing_now",[],ctypes.c_uint64),
            ("playback_timing_receive",[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint,ctypes.c_uint64],None),
            ("playback_timing_cancel",[ctypes.c_void_p,ctypes.c_uint64],None),
            ("playback_timing_input_fault",[ctypes.c_void_p,ctypes.c_uint],None),
            ("playback_timing_watch_origin",[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint64],ctypes.c_int),
            ("playback_timing_origin_health",[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint],ctypes.c_uint),
            ("playback_timing_receive_origin",[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint]+[ctypes.c_uint64]*5+[ctypes.c_uint],None),
            ("playback_timing_snapshot_origin",[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint,ctypes.POINTER(ctypes.c_uint64)],ctypes.c_uint),
            ("playback_timing_proc",[ctypes.c_uint],ctypes.c_void_p),
            ("playback_timing_freed",[ctypes.c_void_p],ctypes.c_int),
            ("playback_timing_snapshot",[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint,ctypes.POINTER(ctypes.c_uint64)],ctypes.c_uint)):
            func=getattr(dll,name);func.argtypes,func.restype=args,result
        extended=getattr(dll,'playback_timing_snapshot_humanized',None)
        if extended is not None:
            extended.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint,ctypes.POINTER(ctypes.c_uint64)]
            extended.restype=ctypes.c_uint
        self.dll=dll
        version=getattr(dll,'playback_timing_position_version',None)
        if version is not None:
            version.restype=ctypes.c_uint;self.position_version=int(version())

    def install(self, stream, filter_gate=None):
        from midi_input_runtime import MIDI_CHANNEL_LIMIT
        from bass_bindings import BASS_SYNC_STALL
        self.close()
        try:
            self._prepare()
            info=BASS_CHANNELINFO()
            if not self.bass.BASS_ChannelGetInfo(stream,ctypes.byref(info)) or info.chans != 2 or not info.flags&BASS_SAMPLE_FLOAT:
                raise RuntimeError(tr("playback_info.source_format_failed"))
            self.rate=int(info.freq)
            self.pointer=self.dll.playback_timing_create(ctypes.cast(self.bass.BASS_ChannelGetPosition,ctypes.c_void_p),stream,MIDI_CHANNEL_LIMIT)
            if not self.pointer: raise MemoryError("playback timing")
            self.stream=int(stream)
            self.coverage=PlaybackCoverage()
            self.dll.playback_timing_rate(self.pointer,self.rate)
            self.callbacks=[ctypes.cast(self.dll.playback_timing_proc(i),DSPPROC if i in (1,4) else SYNCPROC) for i in range(5)]
            self.begin_dsp=self.bass.BASS_ChannelSetDSP(stream,self.callbacks[4],self.pointer,2147483647)
            if not self.begin_dsp:raise RuntimeError("render observation attach")
            # Lower than every source FX/limiter/encoder/meter: completed PCM.
            self.dsp=self.bass.BASS_ChannelSetDSP(stream,self.callbacks[1],self.pointer,-12000)
            if not self.dsp: raise RuntimeError("PCM observation attach")
            for kind,event,param in ((0,0x40010004,1),(2,BASS_SYNC_STALL|0x40000000,0),(3,BASS_SYNC_FREE,0)):
                sync=self.bass.BASS_ChannelSetSync(stream,event,param,self.callbacks[kind],self.pointer)
                if not sync: raise RuntimeError("note observation attach")
                self.syncs.append(sync)
            if filter_gate is not None and self.position_version==2:
                if not filter_gate.bind_playback_timing(self):raise RuntimeError(tr('runtime.midi_filter_config_failed',error='ABI'))
            return True
        except (OSError,AttributeError,RuntimeError,MemoryError) as exc:
            self.close()
            self.log(log_text("playback_info.sensor_failed",error=str(exc)))
            return False

    def timestamp(self):
        return int(self.dll.playback_timing_now()) if self.pointer else None

    def observe_input(self, origin):
        with self.lock:
            if not self.pointer or not isinstance(origin,dict):return
            health=True
            if origin.get("quality")=="driver" and origin.get("pid") and origin.get("epoch"):
                health=bool(self.dll.playback_timing_watch_origin(self.pointer,origin["pid"],origin["epoch"]))
            self.coverage.observe(origin,self.timestamp(),health)

    def register_raw(self, raw, port, received, origin=None):
        if not self.pointer or received is None: return
        # Common dispatch wire uses full status bytes. BASS retains responsibility
        # for all raw decoding; this parser observes only explicit Note Ons.
        index=0
        while index<len(raw):
            status=raw[index]
            if status==0xF0:
                end=raw.find(b"\xF7",index+1)
                if end<0: break
                index=end+1;continue
            command=status&0xF0
            size=2 if command in (0xC0,0xD0) else 3 if 0x80<=status<0xF0 else 1
            if command==0x90 and index+2<len(raw) and raw[index+2]>0:
                if origin is None:
                    # Explicit in-process feed: native capture before engine lock.
                    # Kept separate from every external producer's driver epoch.
                    self.dll.playback_timing_receive(self.pointer,port*16+(status&15),raw[index+1],received)
                else:
                    self.dll.playback_timing_receive_origin(self.pointer,port*16+(status&15),raw[index+1],received,
                        origin.get("read_qpc") or received,origin.get("pid",0),origin.get("epoch",0),origin.get("sequence",0),
                        1 if origin.get("quality")=="driver" else 0)
            index+=size

    def cancel(self, received):
        if self.pointer and received is not None: self.dll.playback_timing_cancel(self.pointer,received)

    def snapshot(self):
        with self.lock:
            if not self.pointer: return {}
            extended=getattr(self.dll,'playback_timing_snapshot_humanized',None)
            rows,meta=(self._human_rows if extended is not None else self._origin_rows),self._origin_meta
            count=(extended or self.dll.playback_timing_snapshot_origin)(self.pointer,rows,8192,meta)
            health_count=self.dll.playback_timing_origin_health(self.pointer,self._health_rows,1024)
            # Fence AFTER reading counters: a concurrent loss must not be
            # assigned an earlier boundary than its publication.
            now=self.timestamp();self.coverage.native(meta,now)
            for index in range(health_count):
                pid,epoch,loss=(int(self._health_rows[3*index+field]) for field in range(3))
                self.coverage.transport((pid,epoch),loss,now)
            loss_log=getattr(self,'_loss_log',None)
            if loss_log is not None:loss_log.poll(self.coverage)
            result=dict(note_samples=0,source_stalls=int(meta[3]),source_late_fx_blocks=int(meta[4]),source_pcm_blocks=int(meta[5]))
            result.update(note_position_qualified=self.position_qualified,note_position_version=self.position_version,
                note_processing_boundary='accepted_filter' if self.position_qualified else 'legacy_sync_decode')
            observed_at=time.perf_counter()
            cutoff=observed_at-1
            if count==8192 and meta[1]>8192 and meta[0]>0 and rows[6]/meta[0]>=cutoff and rows[1]>self.coverage.global_boundary:
                self.coverage.fault("note_omitted",now) # truncated current1s history: fail closed
            if not count or not meta[0]:return dict(result,**self.coverage.status(now,int(meta[0])))
            array=np.ctypeslib.as_array(rows).reshape(8192,15 if extended is not None else 14)[:count]
            array=self.coverage.select(array,now)
            if self.position_qualified:
                result.update(note_delivery_statistics(array,int(meta[0]),self.rate,observed_at))
                if count==8192 and meta[1]>8192 and meta[0]>0 and rows[6]/meta[0]>=observed_at-5:
                    # The bounded native ring cannot prove an entire5s peak.
                    # Keep valid1s mean,never claim the retained subset is5s max.
                    result.pop('note_jitter_max_ms',None)
                    result['note_peak_history_incomplete']=True
            result.update(self.coverage.status(now,int(meta[0])))
            return result

    def origin_records(self):
        """Full bounded correlation evidence, including producer and read boundary."""
        with self.lock:
            if not self.pointer:return (),0
            rows,meta=self._origin_rows,self._origin_meta
            count=self.dll.playback_timing_snapshot_origin(self.pointer,rows,8192,meta)
            return tuple(tuple(int(rows[14*i+j]) for j in range(14)) for i in range(count)),int(meta[0])

    def records(self):
        """Control-side detailed test evidence; same native immutable snapshot."""
        with self.lock:
            if not self.pointer: return ()
            rows,meta=self._rows,self._meta
            count=self.dll.playback_timing_snapshot(self.pointer,rows,8192,meta)
            return tuple(tuple(int(rows[9*i+j]) for j in range(9)) for i in range(count)),int(meta[0])

    def close(self):
        with self.lock:
            if not self.pointer: return
            if self._filter_barrier_gate is not None:
                self._filter_barrier_gate.retry_publication()
                self._filter_barrier_gate=None
            # The gate retains this pointer. Remove its borrower in the same
            # stream exclusion transaction before destroying sensor resources.
            if self._filter_gate is not None:
                gate=self._filter_gate
                gate.retry_publication()
                gate.bind_playback_timing(None,freed=bool(self.dll.playback_timing_freed(self.pointer)))
            freed=bool(self.dll.playback_timing_freed(self.pointer))
            actions=[]
            if not freed:
                for name in ('begin_dsp','dsp'):
                    if getattr(self,name):actions.append((name,self.bass.BASS_ChannelRemoveDSP,getattr(self,name)))
                actions.extend(('sync',self.bass.BASS_ChannelRemoveSync,handle) for handle in reversed(self.syncs))
            result=self._transaction.run(self.stream,((1,function,self.stream,handle) for _,function,handle in actions))
            for name,_,handle in actions[:result.completed]:
                if name=='sync':self.syncs.remove(handle)
                else:setattr(self,name,0)
            if not result.success:
                raise RuntimeError(tr('native_meter.remove_failed' if result.phase==2 else 'native_meter.lock_failed'))
            self.dll.playback_timing_destroy(self.pointer)
            self.pointer=None;self.stream=self.dsp=self.begin_dsp=0;self.syncs=[];self.callbacks=[]
            self.position_qualified=False;self._filter_gate=None

    def reset_notes(self):
        with self.lock:
            if not self.pointer:return
            result=self._transaction.run(self.stream,((2,self.dll.playback_timing_reset,0,self.pointer),))
            if not result.success:raise RuntimeError(tr('native_meter.lock_failed'))


class OutputMeter:
    """Own one final-mix DSP handle and its consume-on-read peak window.

    ``STREAMPROC_DEVICE`` is the BASS final output mix; ASIO instead supplies
    the decode stream. ``BASS_CONFIG_FLOATDSP`` is enabled by AudioSession
    before installation so the callback reads real float samples, including
    genuine amplitudes above 짹1.0 without changing them.
    """

    def __init__(self, bass, log, *, native_dll=None):
        self.bass = bass
        self.log = log
        self.stream = 0
        self.dsp = 0
        self.free_sync = 0
        self.needs_rebind = False
        self.retry_after = 0.0
        self.decode_source = 0
        self.channels = 2
        self.sample_rate = 48000
        self.peaks = [0.0, 0.0]
        self.waveform = deque(maxlen=4096)
        self.visual_rate = 48000.0
        self.revision = 0
        self.lock = threading.Lock()
        self.presentation_enabled = True
        self.diagnostic_enabled = False
        self._diagnostic_squares = [0.0, 0.0]
        self._diagnostic_peaks = [0.0, 0.0]
        self._diagnostic_frames = 0
        # Keep the native callback alive for exactly as long as the owner.
        self.callback = DSPPROC(self.process_samples)
        self.free_callback = SYNCPROC(self._on_mix_free)
        self._native_path = Path(native_dll or os.environ.get('ABMSYNTH_OBSERVATION_DLL',Path(__file__).with_name("abmsynth_dsp.dll")))
        self._transaction=NativeChannelTransaction(bass,self._native_path)
        self._native_library = None
        self._native_pointer = None
        self._native_callback = None
        self._native_control = threading.RLock()
        self._native_wave = (ctypes.c_float * 4096)()
        self._native_peaks = (ctypes.c_float * 2)()
        self._native_revision = ctypes.c_uint64()
        self._last_native_revision = None
        self._native_timing = (ctypes.c_uint64 * 1024)()
        self._native_timing_frequency = ctypes.c_uint64()
        self._native_timing_revision = ctypes.c_uint64()
        self._timing_rows = deque(maxlen=65536)
        self._timing_identity = None
        self._source_freed = False
        self.diagnostic_dsp = 0
        self.diagnostic_callback = DSPPROC(self._process_diagnostic)
        from tempo_analysis import NativeTempoCapture
        self.tempo_capture=NativeTempoCapture(bass)

    def _prepare_native(self):
        if self._native_library is None:
            try:
                library = ctypes.CDLL(str(self._native_path.resolve()))
                library.meter_version.argtypes, library.meter_version.restype = [], ctypes.c_uint
                if library.meter_version() != 1: raise ValueError("meter ABI1")
                for name, arguments, result in (
                    ("meter_create", [ctypes.c_uint]*3, ctypes.c_void_p),
                    ("meter_release", [ctypes.c_void_p], None),
                    ("meter_arm", [ctypes.c_void_p], ctypes.c_int),
                    ("meter_removed", [ctypes.c_void_p], None),
                    ("meter_is_registered", [ctypes.c_void_p], ctypes.c_int),
                    ("meter_clear", [ctypes.c_void_p, ctypes.c_int], None),
                    ("meter_process", [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint], None),
                    ("meter_peaks", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float)], None),
                    ("meter_snapshot", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_uint,
                                         ctypes.POINTER(ctypes.c_uint64)], ctypes.c_uint),
                ):
                    function = getattr(library, name)
                    function.argtypes, function.restype = arguments, result
                # bass.h DSPPROC; export address, never a Python thunk.
                self._native_callback = DSPPROC(("meter_bass_dsp", library))
                if hasattr(library, "meter_timing_snapshot"):
                    library.meter_timing_snapshot.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint64), ctypes.c_uint,
                        ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_uint64)]
                    library.meter_timing_snapshot.restype = ctypes.c_uint
            except (OSError, AttributeError, ValueError) as error:
                raise RuntimeError(tr("native_meter.prepare_failed", error=error)) from error
            self._native_library = library
        pointer = self._native_library.meter_create(self.channels, self.sample_rate,
                         max(1, round(self.sample_rate / VISUAL_CAPTURE_RATE)))
        if not pointer: raise MemoryError(tr("native_meter.allocation_failed"))
        self._native_pointer = pointer
        self._last_native_revision = None
        self._source_freed = False
        self._native_library.meter_clear(pointer, self.presentation_enabled)

    def _clear_native(self, enabled):
        """Serialize readers; stop producer only during the tiny clear, not paints."""
        with self._native_control:
            if not self._native_pointer: return
            registered = self._native_library.meter_is_registered(self._native_pointer)
            locked = bool(self.stream and registered)
            if locked:
                result=self._transaction.run(self.stream,((3,self._native_library.meter_clear,int(bool(enabled)),self._native_pointer),))
                if not result.success:raise RuntimeError(tr('native_meter.lock_failed'))
            else:
                result=self._transaction.run(self.stream,()) # Retry any retained lock first.
                if not result.success:raise RuntimeError(tr('native_meter.lock_failed'))
                self._native_library.meter_clear(self._native_pointer, enabled)

    def _process_diagnostic(self, _hdsp, _channel, buffer, length, _user):
        """Opt-in debug only; preserves the existing NumPy RMS window."""
        if not self.diagnostic_enabled or not buffer or length < 8: return
        channels = max(1, self.channels)
        frames = length // 4 // channels
        if not frames: return
        values = np.ctypeslib.as_array(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_float)), shape=(frames*channels,))
        left = values[::channels]
        right = values[1::channels] if channels > 1 else left
        squares = float(np.dot(left, left)), float(np.dot(right, right))
        peaks = []
        for channel in (left, right):
            peak = max(abs(float(channel.max())), abs(float(channel.min())))
            peaks.append(peak if math.isfinite(peak) else 0.0)
        with self.lock:
            if self.diagnostic_enabled:
                for index in (0, 1):
                    self._diagnostic_squares[index] += squares[index]
                    self._diagnostic_peaks[index] = max(self._diagnostic_peaks[index], peaks[index])
                self._diagnostic_frames += frames

    def _sync_diagnostic_dsp(self):
        if not self.stream: return
        if self.diagnostic_enabled and not self.diagnostic_dsp:
            self.diagnostic_dsp = self.bass.BASS_ChannelSetDSP(self.stream, self.diagnostic_callback, None, OUTPUT_METER_DSP_PRIORITY-1)
            if not self.diagnostic_dsp:
                raise RuntimeError(tr("native_meter.diagnostic_attach_failed"))
        elif not self.diagnostic_enabled and self.diagnostic_dsp:
            if not self.bass.BASS_ChannelRemoveDSP(self.stream, self.diagnostic_dsp):
                raise RuntimeError(tr("native_meter.remove_failed"))
            self.diagnostic_dsp = 0

    def _on_mix_free(self, sync, channel, _data, _user):
        """Record BASS's device-mix invalidation, without doing GUI work here."""
        if not self.free_sync or sync != self.free_sync or channel != self.stream:
            return
        self.tempo_capture.source_freed()
        # Installed BASS sends NULL for explicit RemoveDSP but not always for
        # StreamFree. BASS_SYNC_FREE is terminal (channel no longer exists).
        # Owner ref survives; release the registration from this terminal path.
        if self._native_pointer:
            self._native_library.meter_removed(self._native_pointer)
        # bass.md BASS_StreamCreate(STREAMPROC_DEVICE): an output format
        # change frees this mix stream, but not the BASSMIDI source stream.
        # Recreate only the observation tap on the application thread.
        self.stream = self.dsp = self.free_sync = self.diagnostic_dsp = 0
        self._source_freed = True
        self.needs_rebind = not bool(self.decode_source)
        # Retain native owner until control-thread refresh/reset. Its registered
        # ref is released by NULL removal or this terminal FREE notice.
        # Never call BASS here.
        self._clear_values()

    def _clear_values(self):
        with self.lock:
            self.peaks[:] = (0.0, 0.0)
            self.waveform.clear()
            self.revision += 1
            self._diagnostic_squares[:] = (0.0, 0.0)
            self._diagnostic_peaks[:] = (0.0, 0.0)
            self._diagnostic_frames = 0

    def process_samples(self, _hdsp, _channel, buffer, length, _user):
        """Reduce signed final-mix float samples; never modify audio data."""
        presenting = self.presentation_enabled
        diagnostic = self.diagnostic_enabled
        if not presenting and not diagnostic:
            return  # Hidden UI must not make the audio callback build plots.
        if not buffer or length < ctypes.sizeof(ctypes.c_float) * 2:
            return
        sample_count = length // ctypes.sizeof(ctypes.c_float)
        channels = max(1, int(self.channels))
        frames = sample_count // channels
        if frames <= 0:
            return
        samples = np.ctypeslib.as_array(
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_float)),
            shape=(frames * channels,),
        )

        def channel_peak(index):
            values = samples[index::channels]
            if values.size == 0:
                return 0.0
            peak = max(abs(float(values.max())), abs(float(values.min())))
            return peak if math.isfinite(peak) else 0.0

        left = channel_peak(0)
        right = channel_peak(1 if channels > 1 else 0)
        if diagnostic:
            left_values = samples[0::channels]
            right_values = samples[1::channels] if channels > 1 else left_values
            diagnostic_squares = (float(np.dot(left_values, left_values)),
                                  float(np.dot(right_values, right_values)))
        else:
            diagnostic_squares = None
        if presenting:
            waveform = (
                (samples[0::channels] + samples[1::channels]) * 0.5
                if channels > 1 else samples[0::channels]
            )
            stride = max(1, int(round(self.sample_rate / VISUAL_CAPTURE_RATE)))
            waveform_points = waveform[::stride]
            # Convert a bounded display copy outside the shared lock. Never
            # change the signed source buffer or a real over-0 dBFS peak.
            if not np.isfinite(waveform_points).all():
                waveform_points = np.nan_to_num(
                    waveform_points, nan=0.0, posinf=0.0, neginf=0.0,
                )
            visual_points = waveform_points.tolist()
        with self.lock:
            if presenting and self.presentation_enabled:
                self.peaks[0] = max(self.peaks[0], left)
                self.peaks[1] = max(self.peaks[1], right)
                self.waveform.extend(visual_points)
                self.visual_rate = self.sample_rate / stride
                self.revision += 1
            if diagnostic_squares is not None and self.diagnostic_enabled:
                self._diagnostic_squares[0] += diagnostic_squares[0]
                self._diagnostic_squares[1] += diagnostic_squares[1]
                self._diagnostic_peaks[0] = max(self._diagnostic_peaks[0], left)
                self._diagnostic_peaks[1] = max(self._diagnostic_peaks[1], right)
                self._diagnostic_frames += frames

    def set_presentation_enabled(self, enabled: bool) -> None:
        """Drop hidden-window observations, never pause the audio stream."""
        enabled = bool(enabled)
        if self.presentation_enabled == enabled:
            return
        self._clear_native(enabled)
        with self.lock:
            self.presentation_enabled = enabled
            self.peaks[:] = (0.0, 0.0)
            self.waveform.clear()
            self.revision += 1

    def set_diagnostic_enabled(self, enabled: bool) -> None:
        previous = self.diagnostic_enabled
        with self.lock:
            self.diagnostic_enabled = bool(enabled)
            self._diagnostic_squares[:] = (0.0, 0.0)
            self._diagnostic_peaks[:] = (0.0, 0.0)
            self._diagnostic_frames = 0
        try:
            self._sync_diagnostic_dsp()
        except RuntimeError:
            self.diagnostic_enabled = previous
            raise

    def drain_diagnostic_levels(self) -> dict | None:
        """Return one final-mix energy window; never touch the DSP callback's file I/O."""
        with self.lock:
            frames = self._diagnostic_frames
            if not self.diagnostic_enabled or not frames:
                return None
            squares = tuple(self._diagnostic_squares)
            peaks = tuple(self._diagnostic_peaks)
            self._diagnostic_squares[:] = (0.0, 0.0)
            self._diagnostic_peaks[:] = (0.0, 0.0)
            self._diagnostic_frames = 0
            return {"frames": frames, "sample_rate": self.sample_rate,
                    "rms_left": math.sqrt(max(0.0, squares[0] / frames)),
                    "rms_right": math.sqrt(max(0.0, squares[1] / frames)),
                    "peak_left": peaks[0], "peak_right": peaks[1]}

    def install(self):
        self.reset(remove_dsp=True)
        stream = self.decode_source or self.bass.BASS_StreamCreate(
            0, 0, 0, ctypes.c_void_p(BASS_STREAMPROC_DEVICE), None,
        )
        if not stream:
            self.log(log_text("runtime.final_meter_create_failed", error=self.bass.BASS_ErrorGetCode()))
            return False
        info = BASS_CHANNELINFO()
        if self.bass.BASS_ChannelGetInfo(stream, ctypes.byref(info)) and info.chans:
            self.channels = int(info.chans)
            self.sample_rate = int(info.freq) or 48000
        else:
            self.channels = 2
            self.sample_rate = 48000
        try:
            self._prepare_native()
        except (OSError, RuntimeError, MemoryError) as error:
            self.log(log_text("native_meter.install_failed", error=error))
            return False
        if not self._native_library.meter_arm(self._native_pointer):
            raise RuntimeError(tr("native_meter.arm_failed"))
        dsp = self.bass.BASS_ChannelSetDSP(
            stream, self._native_callback, self._native_pointer, OUTPUT_METER_DSP_PRIORITY,
        )
        if not dsp:
            self._native_library.meter_removed(self._native_pointer)
            self._native_library.meter_release(self._native_pointer)
            self._native_pointer = None
            self.log(log_text("runtime.final_meter_dsp_failed", error=self.bass.BASS_ErrorGetCode()))
            return False
        self.stream = stream
        self.dsp = dsp
        # Terminal cleanup applies to device-mix AND decode sources. Only a
        # device-mix invalidation requests rebind, not an intentionally freed source.
        self.free_sync = self.bass.BASS_ChannelSetSync(
            stream, BASS_SYNC_FREE, 0, self.free_callback, None,
        )
        if not self.free_sync:
            self.log(log_text("runtime.final_meter_sync_failed", error=self.bass.BASS_ErrorGetCode()))
            self.reset(remove_dsp=True)
            return False
        try:
            self._sync_diagnostic_dsp()
        except RuntimeError as error:
            self.log(log_text("native_meter.install_failed", error=error))
            self.reset(remove_dsp=True)
            return False
        try:
            self.tempo_capture.install(stream,self.sample_rate,self.channels)
        except (OSError,RuntimeError,MemoryError) as error:
            # Optional observer failure cannot remove a working meter/audio path.
            self.log(log_text('tempo_display.unavailable',error=error))
        return True

    def refresh_if_needed(self):
        """Rebind a freed device-mix observation tap, never the MIDI stream."""
        if not self.needs_rebind or self.decode_source or time.monotonic() < self.retry_after:
            return False
        if self.install():
            return True
        self.needs_rebind = True
        self.retry_after = time.monotonic() + 1.0
        return False

    def reset(self, remove_dsp=False):
        """Detach owned callbacks or clear observations; never free the mix."""
        if not remove_dsp and self._native_pointer:
            self._clear_native(self.presentation_enabled)
            self._clear_values()
            return
        self.tempo_capture.close()
        with self._native_control:
            if self._native_pointer:
                # Retain state until NULL removal or terminal FREE notification.
                actions=[]
                if self.stream and self.dsp and self._native_library.meter_is_registered(self._native_pointer):
                    actions.append(('dsp',self.bass.BASS_ChannelRemoveDSP,self.dsp))
                # A failed sync removal must remain retryable after successful
                # DSP removal, rather than silently forgetting its callback.
                if self.stream and self.free_sync:
                    actions.append(('free_sync',self.bass.BASS_ChannelRemoveSync,self.free_sync))
                if self.stream and self.diagnostic_dsp:
                    actions.append(('diagnostic_dsp',self.bass.BASS_ChannelRemoveDSP,self.diagnostic_dsp))
                result=self._transaction.run(self.stream,((1,function,self.stream,handle) for _,function,handle in actions))
                for name,_,_ in actions[:result.completed]:setattr(self,name,0)
                if not result.success:
                    raise RuntimeError(tr('native_meter.remove_failed' if result.phase==2 else 'native_meter.lock_failed'))
                self._native_library.meter_release(self._native_pointer)
                self._native_pointer = None
        if self.diagnostic_dsp and self.stream:
            if not self.bass.BASS_ChannelRemoveDSP(self.stream, self.diagnostic_dsp):
                raise RuntimeError(tr("native_meter.remove_failed"))
            self.diagnostic_dsp = 0
        stream, dsp, free_sync = self.stream, self.dsp, self.free_sync
        self.stream = self.dsp = self.free_sync = 0
        self.needs_rebind = False
        self.retry_after = 0.0
        if remove_dsp and free_sync and stream:
            try:
                self.bass.BASS_ChannelRemoveSync(stream, free_sync)
            except Exception:
                pass
        if remove_dsp and dsp and stream:
            try:
                self.bass.BASS_ChannelRemoveDSP(stream, dsp)
            except Exception:
                pass
        self.channels = 2
        self.sample_rate = 48000
        self._clear_values()

    def set_decode_source(self, stream):
        self.decode_source = int(stream)

    def consume_peak_levels(self):
        with self._native_control:
            if self._native_pointer:
                if self.needs_rebind or self._source_freed: return (0., 0.)
                self._native_library.meter_peaks(self._native_pointer, self._native_peaks)
                return tuple(self._native_peaks)
        with self.lock:
            levels = tuple(self.peaks)
            self.peaks[:] = (0.0, 0.0)
        return levels

    def timing_statistics(self, *, include_deviation=True):
        """Control/observation worker only: recent render-boundary cadence.

        Not DAC clock jitter, MIDI latency or underrun proof. No audio reads,
        DSP/graph changes or GUI-clock samples. Same serialized native reader.
        """
        with self._native_control:
            if self.needs_rebind or self._source_freed or not self._native_pointer or not hasattr(self._native_library, "meter_timing_snapshot"):
                return {}
            count = self._native_library.meter_timing_snapshot(self._native_pointer,self._native_timing,512,
                ctypes.byref(self._native_timing_frequency),ctypes.byref(self._native_timing_revision))
            frequency = self._native_timing_frequency.value
            rate = self.sample_rate
            rows = tuple((self._native_timing[2*i],self._native_timing[2*i+1]) for i in range(count))
            identity = self._native_pointer, frequency, rate
            if identity != self._timing_identity or not rows:
                self._timing_rows.clear(); self._timing_identity = identity
            if rows:
                last = self._timing_rows[-1][0] if self._timing_rows else -1
                # A polling gap longer than the native ring loses boundaries.
                # Restart the window rather than join across missing intervals.
                if self._timing_rows and rows[0][0] > last:
                    self._timing_rows.clear(); last = -1
                self._timing_rows.extend(row for row in rows if row[0] > last)
                cutoff = (time.perf_counter() - 1) * frequency
                while len(self._timing_rows) > 2 and self._timing_rows[1][0] < cutoff:
                    self._timing_rows.popleft()
            history = tuple(self._timing_rows)
        return render_cadence_statistics(history, frequency, rate, include_deviation=include_deviation)

    def visual_snapshot(self):
        with self._native_control:
            if self._native_pointer:
                if self.needs_rebind or self._source_freed: return (), self.sample_rate, self.revision
                count = self._native_library.meter_snapshot(self._native_pointer, self._native_wave, 4096,
                                                          ctypes.byref(self._native_revision))
                if self._last_native_revision != self._native_revision.value:
                    self.revision += 1
                    self._last_native_revision = self._native_revision.value
                return tuple(self._native_wave[:count]), self.sample_rate/max(1, round(self.sample_rate/VISUAL_CAPTURE_RATE)), self.revision
        with self.lock:
            return tuple(self.waveform), self.visual_rate, self.revision

    def tempo_snapshot(self):
        return self.tempo_capture.read()

