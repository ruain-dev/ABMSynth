"""Custom-instrument authoring/file boundary; no Qt or live engine mutation.

SF2 2.01 RIFF/hydra follows the SoundFont2.04 specification; local BASS ABI
and manuals govern decoding/private MIDI rendering. All I/O is worker-side.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import struct
import sys
import tempfile
from dataclasses import dataclass, field, replace, fields
from pathlib import Path

import numpy as np
from i18n import tr

# Authoring safety policies, not BASS/SF2 capability limits.
MAX_SAMPLE_BYTES = 64 * 1024 * 1024
MAX_ZONES = 32
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_PREVIEW_BYTES = MAX_FILE_BYTES  # Private visualization payload policy,not a BASS duration limit.
WAVE_POINTS = 256


@dataclass(frozen=True)
class NativeSoundParameter:
    """SF2 generator contract, shared with authoring UI/serialization/audits.

    E-mu2.04 sections8.1.3/9.3; BASSMIDI FontInit supports SF2 generators.
    Product domains are intentionally narrower than native raw domains.
    """
    minimum: float
    maximum: float
    default: float
    step: float
    unit: str
    generator: int
    native_minimum: int
    native_maximum: int
    conversion: str

    def validate(self,value):
        if (type(value) not in (int,float) or not math.isfinite(value)
                or not self.minimum<=value<=self.maximum):
            raise ValueError(tr('creator.invalid_sound'))
        return value

    def raw(self,value):
        value=self.validate(value)
        if self.conversion=='time':return min(self.native_maximum,_timecents(value))
        raw=round(1200*math.log2(value/8.176)) if self.conversion=='frequency' else round(value*10) if self.conversion=='centibels' else round(value)
        return max(self.native_minimum,min(self.native_maximum,raw))


# Safe authoring policies: Q/depth deliberately modest; no generator is
# emitted at default so old source-zone payloads/sound remain unchanged.
NATIVE_SOUND_PARAMETERS={
    'envelope_delay':NativeSoundParameter(0,5,2**-10,.001,'seconds',33,-12000,5000,'time'),
    'envelope_hold':NativeSoundParameter(0,5,2**-10,.001,'seconds',35,-12000,5000,'time'),
    'filter_cutoff':NativeSoundParameter(20,20000,20000,1,'hz',8,1500,13500,'frequency'),
    'filter_resonance':NativeSoundParameter(0,12,0,.1,'db',9,0,960,'centibels'),
    'vibrato_rate':NativeSoundParameter(.1,20,8.176,.01,'hz',24,-16000,4500,'frequency'),
    'vibrato_depth':NativeSoundParameter(0,100,0,1,'cents',6,-12000,12000,'identity'),
    'vibrato_delay':NativeSoundParameter(0,5,2**-10,.001,'seconds',23,-12000,5000,'time'),
    'tremolo_rate':NativeSoundParameter(.1,20,8.176,.01,'hz',22,-16000,4500,'frequency'),
    'tremolo_depth':NativeSoundParameter(0,12,0,.1,'db',13,-960,960,'centibels'),
    'tremolo_delay':NativeSoundParameter(0,5,2**-10,.001,'seconds',21,-12000,5000,'time'),
}


def _native_sound_values(record):
    return {key:spec.validate(record[key]) for key,spec in NATIVE_SOUND_PARAMETERS.items()}


def _stored_wave_settings(record):
    value=record['wave_settings']
    if type(value) is not dict or set(value)!={f.name for f in fields(WaveSettings)}:
        raise ValueError(tr('creator.invalid_project'))
    return WaveSettings(**value)


def wave_preset(kind):
    phase = np.arange(WAVE_POINTS) / WAVE_POINTS
    return dict(sine=lambda: np.sin(2*np.pi*phase),
                triangle=lambda: 1-4*np.abs(phase-.5),
                saw=lambda: 2*phase-1,
                square=lambda: np.where(phase < .5, 1., -1.),
                silence=lambda: np.zeros(WAVE_POINTS))[kind]().astype(np.float32)


@dataclass
class SampleZone:
    name: str
    pcm: np.ndarray
    rate: int
    root: int = 60
    low: int = 0
    high: int = 127
    velocity_low: int = 1
    velocity_high: int = 127
    start: int = 0
    end: int = 0
    loop_start: int = 0
    loop_end: int = 0
    loop: int = 0
    tune: int = 0
    gain: float = 0.
    source_path: str = ''
    speed: float = 1.
    pitch: float = 0.
    reverse: bool = False
    key_tracking: bool = True

    def validate(self):
        pcm = np.asarray(self.pcm)
        if (pcm.ndim != 2 or pcm.shape[1] not in (1, 2) or len(pcm) < 4
                or pcm.nbytes > MAX_SAMPLE_BYTES or not np.all(np.isfinite(pcm))):
            raise ValueError(tr('creator.invalid_sample'))
        if not 400 <= self.rate <= 768000:
            raise ValueError(tr('creator.invalid_rate'))
        end = self.end or len(pcm)
        if not 0 <= self.start < end <= len(pcm) or end-self.start < 4:
            raise ValueError(tr('creator.invalid_trim'))
        if not 0 <= self.low <= self.high <= 127 or not 0 <= self.root <= 127:
            raise ValueError(tr('creator.invalid_keys'))
        if not 1 <= self.velocity_low <= self.velocity_high <= 127:
            raise ValueError(tr('creator.invalid_velocity'))
        if self.loop not in (0, 1, 3) or (self.loop and not self.start <= self.loop_start < self.loop_end <= end):
            raise ValueError(tr('creator.invalid_loop'))
        if not -99 <= self.tune <= 99 or not math.isfinite(self.gain) or not -48 <= self.gain <= 12:
            raise ValueError(tr('creator.invalid_gain'))
        if (not math.isfinite(self.speed) or not .25 <= self.speed <= 4
                or not math.isfinite(self.pitch) or not -24 <= self.pitch <= 24
                or type(self.reverse) is not bool or type(self.key_tracking) is not bool):
            raise ValueError(tr('creator.invalid_transform'))
        return end


@dataclass(frozen=True)
class WaveSettings:
    """Generated-cycle mapping/transform;neutral retains the original adapter."""
    root: int = 45  # The generated source is110Hz,A2;unchanged default tuning.
    low: int = 0
    high: int = 127
    velocity_low: int = 1
    velocity_high: int = 127
    tune: int = 0
    gain: float = 0.
    pitch: float = 0.
    reverse: bool = False
    key_tracking: bool = True

    def zone(self,wave):
        return SampleZone('Wave',np.asarray(wave,np.float32)[:,None],WAVE_POINTS*110,
            **vars(self),end=WAVE_POINTS,loop_end=WAVE_POINTS,loop=1)

    def validate(self):
        if any(type(getattr(self,key)) is not int for key in ('root','low','high','velocity_low','velocity_high','tune')):
            raise ValueError(tr('creator.invalid_keys'))
        self.zone(np.zeros(WAVE_POINTS,np.float32)).validate()


@dataclass
class InstrumentDraft:
    name: str = ''
    bank: int = 0
    program: int = 0
    mode: str = 'wave'
    wave: np.ndarray = field(default_factory=lambda: wave_preset('sine'))
    zones: list[SampleZone] = field(default_factory=list)
    attack: float = .01
    decay: float = .2
    sustain: float = 100.
    release: float = .1
    gain: float = 0.
    wave_peak_fit: bool = True
    wave_settings: WaveSettings = field(default_factory=WaveSettings)
    envelope_delay: float = 2**-10
    envelope_hold: float = 2**-10
    filter_cutoff: float = 20000.
    filter_resonance: float = 0.
    vibrato_rate: float = 8.176
    vibrato_depth: float = 0.
    vibrato_delay: float = 2**-10
    tremolo_rate: float = 8.176
    tremolo_depth: float = 0.
    tremolo_delay: float = 2**-10

    def validate(self):
        if type(self.wave_peak_fit) is not bool:raise ValueError(tr('creator.invalid_project'))
        if not isinstance(self.wave_settings,WaveSettings):raise ValueError(tr('creator.invalid_project'))
        self.wave_settings.validate()
        for key,spec in NATIVE_SOUND_PARAMETERS.items():spec.validate(getattr(self,key))
        if not isinstance(self.name,str) or not self.name.strip() or len(self.name) > 80 or any(c in self.name for c in '\r\n\x00'):
            raise ValueError(tr('creator.invalid_name'))
        if type(self.bank) is not int or type(self.program) is not int or not 0 <= self.bank <= 128 or not 0 <= self.program <= 127:
            raise ValueError(tr('creator.invalid_address'))
        for value in (self.attack, self.decay, self.release):
            if not math.isfinite(value) or not 0 <= value <= 30:
                raise ValueError(tr('creator.invalid_envelope'))
        if not math.isfinite(self.sustain) or not 0 <= self.sustain <= 100 or not math.isfinite(self.gain) or not -48 <= self.gain <= 0:
            raise ValueError(tr('creator.invalid_envelope'))
        if self.mode == 'wave':
            if np.shape(self.wave) != (WAVE_POINTS,) or not np.all(np.isfinite(self.wave)) or np.max(np.abs(self.wave)) > 1:
                raise ValueError(tr('creator.invalid_wave'))
        elif self.mode == 'sample':
            if not 1 <= len(self.zones) <= MAX_ZONES:
                raise ValueError(tr('creator.zone_limit', maximum=MAX_ZONES))
            if sum(z.pcm.nbytes for z in self.zones) > MAX_FILE_BYTES:
                raise ValueError(tr('creator.too_large'))
            for zone in self.zones: zone.validate()
        else:
            raise ValueError(tr('creator.invalid_project'))

    def snapshot(self):
        # Edits replace immutable PCM references; background tasks never mutate them.
        return replace(self, wave=self.wave.copy(), zones=[replace(z) for z in self.zones])


def decode_sample(path, cancelled=lambda: False):
    from bass_bindings import bass, BASS_CHANNELINFO, BASS_SAMPLE_FLOAT, BASS_STREAM_DECODE, BASS_UNICODE
    stream = bass.BASS_StreamCreateFile(False, str(path), 0, 0, BASS_UNICODE | BASS_SAMPLE_FLOAT | BASS_STREAM_DECODE)
    if not stream: raise RuntimeError(tr('creator.decode_failed', error=bass.BASS_ErrorGetCode()))
    try:
        info = BASS_CHANNELINFO()
        if not bass.BASS_ChannelGetInfo(stream, ctypes.byref(info)) or info.chans not in (1, 2):
            raise ValueError(tr('creator.mono_stereo'))
        blocks, total = [], 0
        block = np.empty((16384, info.chans), np.float32)
        while True:
            if cancelled(): raise RuntimeError(tr('creator.cancelled'))
            size = int(bass.BASS_ChannelGetData(stream, block.ctypes.data, block.nbytes))
            if size == 0xffffffff:
                error = bass.BASS_ErrorGetCode()
                if error == 45: break  # bass.h BASS_ERROR_ENDED
                raise RuntimeError(tr('creator.decode_failed', error=error))
            if not size: break
            if size % (info.chans*4): raise ValueError(tr('creator.invalid_sample'))
            total += size
            if total > MAX_SAMPLE_BYTES: raise ValueError(tr('creator.sample_limit', mb=MAX_SAMPLE_BYTES//1024//1024))
            blocks.append(block[:size//(info.chans*4)].copy())
        pcm = np.concatenate(blocks) if blocks else np.empty((0, info.chans), np.float32)
        zone = SampleZone(Path(path).name, pcm, int(info.freq), end=len(pcm), loop_end=len(pcm),source_path=str(Path(path).resolve()))
        zone.validate()
        return zone
    finally: bass.BASS_StreamFree(stream)


def draft_record(draft):
    """Editable SFG data. No PCM or generated adapter paths are persisted."""
    draft.validate()
    result={key:getattr(draft,key) for key in ('name','bank','program','mode','attack','decay','sustain','release','gain')}
    result.update(schema=1,wave=draft.wave.tolist(),zones=[])
    result['wave_peak_fit']=draft.wave_peak_fit
    result['wave_settings']=vars(draft.wave_settings).copy()
    result.update({key:getattr(draft,key) for key in NATIVE_SOUND_PARAMETERS})
    for zone in (draft.zones if draft.mode=='sample' else ()):
        if not zone.source_path:raise ValueError(tr('creator.sample_reference_missing',name=zone.name))
        result['zones'].append({key:getattr(zone,key) for key in
            ('name','source_path','root','low','high','velocity_low','velocity_high','start','end','loop_start','loop_end','loop','tune','gain',
             'speed','pitch','reverse','key_tracking')})
    # Validate plain schema independently of decoded audio before saving.
    validate_record(result)
    return result


def validate_record(record):
    keys={'schema','name','bank','program','mode','attack','decay','sustain','release','gain',
          'wave','zones','wave_peak_fit','wave_settings',*NATIVE_SOUND_PARAMETERS}
    if (type(record) is not dict or set(record)!=keys or type(record.get('schema')) is not int
            or record['schema']!=1):raise ValueError(tr('creator.invalid_project'))
    try:
        if len(json.dumps(record,ensure_ascii=True,separators=(',',':')))>512*1024:raise ValueError()
        if any(type(record[key]) not in (int,float) for key in ('attack','decay','sustain','release','gain')):raise ValueError()
        draft=InstrumentDraft(**{key:record[key] for key in
            ('name','bank','program','mode','attack','decay','sustain','release','gain')},wave=np.asarray(record['wave'],np.float32),
            wave_peak_fit=record['wave_peak_fit'],wave_settings=_stored_wave_settings(record),**_native_sound_values(record))
        if draft.mode not in ('wave','sample'):raise ValueError()
        # Common/wave validation does not require loading sample files.
        mode=draft.mode;draft.mode='wave';draft.validate();draft.mode=mode
        zones=record['zones']
        if not isinstance(zones,list) or len(zones)>MAX_ZONES or mode=='sample' and not zones:raise ValueError()
        for zone in zones:
            if type(zone) is not dict or set(zone)!={'name','source_path','root','low','high','velocity_low','velocity_high',
                    'start','end','loop_start','loop_end','loop','tune','gain','speed','pitch','reverse','key_tracking'}:raise ValueError()
            if not isinstance(zone,dict) or not isinstance(zone['source_path'],str) or not zone['source_path']:raise ValueError()
            if not isinstance(zone['name'],str) or not zone['name'] or '\x00' in zone['source_path']:raise ValueError()
            if any(type(zone[k]) is not int for k in ('root','low','high','velocity_low','velocity_high','start','end','loop_start','loop_end','loop','tune')):raise ValueError()
            if not 0<=zone['low']<=zone['high']<=127 or not 0<=zone['root']<=127:raise ValueError()
            if not 1<=zone['velocity_low']<=zone['velocity_high']<=127:raise ValueError()
            if not 0<=zone['start']<zone['end'] or zone['end']-zone['start']<4:raise ValueError()
            if zone['loop'] not in (0,1,3) or zone['loop'] and not zone['start']<=zone['loop_start']<zone['loop_end']<=zone['end']:raise ValueError()
            if not -99<=zone['tune']<=99 or not math.isfinite(zone['gain']) or not -48<=zone['gain']<=12:raise ValueError()
            if (not math.isfinite(zone['speed']) or not .25<=zone['speed']<=4
                    or not math.isfinite(zone['pitch']) or not -24<=zone['pitch']<=24
                    or type(zone['reverse']) is not bool or type(zone['key_tracking']) is not bool):raise ValueError()
        return draft
    except (KeyError,TypeError,OverflowError,ValueError) as exc:raise ValueError(tr('creator.invalid_project')) from exc


def resolve_sample_path(specified,group_path='',execution_dir=None):
    """First existing file, exact user order, direct directories only."""
    specified=Path(specified)
    base=Path(group_path).resolve().parent if group_path else Path.cwd()
    execution=(Path(execution_dir) if execution_dir is not None else
               Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent)
    candidates=[specified if specified.is_absolute() else base/specified]
    if group_path:candidates.append(base/specified.name)
    candidates.append(execution/specified.name)
    for candidate in candidates:
        if candidate.is_file():return candidate.resolve()
    raise FileNotFoundError(tr('creator.sample_missing',path=specified))


def record_draft(record,group_path='',execution_dir=None,cancelled=lambda:False):
    draft=validate_record(record);total=0
    for item in record['zones']:
        zone=decode_sample(resolve_sample_path(item['source_path'],group_path,execution_dir),cancelled)
        total+=zone.pcm.nbytes
        if total>MAX_FILE_BYTES:raise ValueError(tr('creator.too_large'))
        for key in ('name','root','low','high','velocity_low','velocity_high','start','end','loop_start','loop_end','loop','tune','gain'):
            setattr(zone,key,item[key])
        for key in ('speed','pitch','reverse','key_tracking'):
            setattr(zone,key,item[key])
        zone.validate();draft.zones.append(zone)
    draft.validate()
    return draft


# Process-private generated fonts survive editor closure, just like runtime
# BASS handles; no generated paths are saved in SFG. BASS is freed at shutdown
# before Python's TemporaryDirectory cleanup. Content keys prevent stale reuse.
_adapter_directory=None


def compile_record(record,group_path='',cancelled=lambda:False):
    global _adapter_directory
    draft=record_draft(record,group_path,cancelled=cancelled)
    if cancelled():raise RuntimeError(tr('creator.cancelled'))
    data=encode_sf2(draft,cancelled)
    if cancelled():raise RuntimeError(tr('creator.cancelled'))
    digest=hashlib.sha256(data).hexdigest()
    if _adapter_directory is None:_adapter_directory=tempfile.TemporaryDirectory(prefix='abmsynth-instruments-')
    path=Path(_adapter_directory.name)/(digest+'.sf2')
    if not path.exists():
        with tempfile.NamedTemporaryFile(dir=path.parent,delete=False,suffix='.tmp') as file:
            temporary=Path(file.name);file.write(data)
        try:os.replace(temporary,path)
        finally:temporary.unlink(missing_ok=True)
    return str(path)


def _wave_zones(wave,settings=None):
    # One-second periodic A2: exactly110 cycles at44100Hz. Per-octave harmonic
    # pruning uses the highest key in each range to keep source harmonics below
    # destination Nyquist after pitch transposition. No oscillator callback.
    spectrum = np.fft.rfft(np.asarray(wave, dtype=np.float64)) / WAVE_POINTS
    spectrum[0] = 0  # remove drawn DC, not a live output filter
    zones=[];settings=settings or WaveSettings()
    for low in range(0, 128, 12):
        high=min(127, low+11)
        if high<settings.low or low>settings.high:continue
        highest=high if settings.key_tracking else settings.root
        frequency=440*2**((highest-69+(45-settings.root)+settings.pitch+settings.tune/100)/12)
        harmonics=min(len(spectrum)-1, int(22049/frequency))
        bins=np.zeros(22051, np.complex128)
        for h in range(1, harmonics+1): bins[110*h]=spectrum[h]*44100
        pcm=np.fft.irfft(bins,n=44100).astype(np.float32)
        if settings.reverse:pcm=pcm[::-1].copy()
        zones.append(SampleZone('Wave',pcm[:,None],44100,root=settings.root,low=max(low,settings.low),high=min(high,settings.high),
            velocity_low=settings.velocity_low,velocity_high=settings.velocity_high,tune=settings.tune,
            gain=settings.gain,key_tracking=settings.key_tracking,end=len(pcm),loop=1,loop_end=len(pcm)))
    return zones


def transform_sample(zone, cancelled=lambda:False):
    """Worker-only private BASS_FX tempo/pitch stream; source PCM stays immutable.

    bass_fx.h: HSTREAM TempoCreate(DWORD,DWORD), FREESOURCE0x10000.
    TEMPO0x10000 is percent change; TEMPO_PITCH0x10001 is semitones.
    The memory WAV buffer must outlive both decode and tempo borrowers.
    Native domain tempo>-95%,pitch[-60,60]; .25..4/-24..24 are authoring
    policies to bound memory/cost, not native capability limits.
    """
    zone.validate()
    source=zone.pcm[::-1].copy() if zone.reverse else zone.pcm
    if zone.speed==1 and zone.pitch==0:return source
    from bass_bindings import bass,bassfx,BASS_SAMPLE_FLOAT,BASS_STREAM_DECODE
    channels=source.shape[1]
    # Flush SoundTouch's sequence/seek window explicitly. At high rates its
    # EOF drain can otherwise lose the last analysis window. Silence is only
    # a private processor tail; return exactly the authored duration below.
    target_frames=max(4,round(len(source)/zone.speed))
    if target_frames*channels*4>MAX_SAMPLE_BYTES:
        raise ValueError(tr('creator.sample_limit',mb=MAX_SAMPLE_BYTES//1024//1024))
    raw=np.asarray(source,dtype='<f4').tobytes()+bytes(round(zone.rate*.3)*channels*4)
    fmt=struct.pack('<HHIIHH',3,channels,zone.rate,zone.rate*channels*4,channels*4,32)
    container=ctypes.create_string_buffer(_chunk(b'RIFF',b'WAVE'+_chunk(b'fmt ',fmt)+_chunk(b'data',raw)))
    stream=bass.BASS_StreamCreateFile(True,ctypes.cast(container,ctypes.c_wchar_p),0,len(container)-1,BASS_SAMPLE_FLOAT|BASS_STREAM_DECODE)
    if not stream:raise RuntimeError(tr('creator.native_failed',error=bass.BASS_ErrorGetCode()))
    tempo=0
    try:
        tempo=bassfx.BASS_FX_TempoCreate(stream,BASS_STREAM_DECODE|0x10000)
        if not tempo:raise RuntimeError(tr('creator.native_failed',error=bass.BASS_ErrorGetCode()))
        for attribute,value in ((0x10000,(zone.speed-1)*100),(0x10001,zone.pitch)):
            if not bass.BASS_ChannelSetAttribute(tempo,attribute,value):
                raise RuntimeError(tr('creator.native_failed',error=bass.BASS_ErrorGetCode()))
        blocks=[];size=0;block=np.empty((16384,channels),np.float32)
        while True:
            if cancelled():raise RuntimeError(tr('creator.cancelled'))
            got=int(bass.BASS_ChannelGetData(tempo,block.ctypes.data,block.nbytes))
            if got==0xffffffff:
                if bass.BASS_ErrorGetCode()==45:break
                raise RuntimeError(tr('creator.native_failed',error=bass.BASS_ErrorGetCode()))
            if not got:break
            if got%(4*channels):raise ValueError(tr('creator.invalid_sample'))
            size+=got
            blocks.append(block[:got//(4*channels)].copy())
            if size>=target_frames*channels*4:break
        result=np.concatenate(blocks) if blocks else np.empty((0,channels),np.float32)
        if len(result)<target_frames:raise ValueError(tr('creator.incomplete_transform'))
        return result[:target_frames]
    finally:
        bass.BASS_StreamFree(tempo or stream)  # tempo owns source only after successful creation.


def prepared_zone(zone, cancelled=lambda:False):
    """Map original source boundaries into the transformed sample exactly once."""
    pcm=transform_sample(zone,cancelled);n=len(zone.pcm);scale=len(pcm)/n
    start,end=zone.start,zone.end or n;loop_start,loop_end=zone.loop_start,zone.loop_end
    if zone.reverse:
        start,end=n-end,n-start;loop_start,loop_end=n-loop_end,n-loop_start
    first=min(len(pcm)-4,max(0,round(start*scale)));last=min(len(pcm),max(first+4,round(end*scale)))
    ls=max(first,min(last-1,round(loop_start*scale)));le=max(ls+1,min(last,round(loop_end*scale)))
    return replace(zone,pcm=pcm,start=first,end=last,loop_start=ls,loop_end=le,speed=1.,pitch=0.,reverse=False)


def analyze_sample(zone, cancelled=lambda:False):
    """Worker-only librosa-derived analysis, with independent acceptance gates.

    The vendored ISC primitives do not load the full librosa/SciPy/numba
    stack. Mono/F0/constant-tempo acceptance below is ABMSynth policy, not a
    library guarantee. Source PCM, live audio and SFG state remain untouched.
    """
    from vendor.librosa_analysis import yin_frames,mel_basis,onset_strength,autocorrelate
    if cancelled():raise RuntimeError(tr('creator.cancelled'))
    end=zone.validate()
    # Pick the stronger channel; anti-phase stereo must not cancel into silence.
    section=zone.pcm[zone.start:min(end,zone.start+round(zone.rate*60))]
    power=np.zeros(section.shape[1])
    for first in range(0,len(section),65536):
        if cancelled():raise RuntimeError(tr('creator.cancelled'))
        block=section[first:first+65536].astype(np.float64)
        power+=np.sum(block*block,axis=0)
    channel=int(np.argmax(power))
    # Analysis-only admission policy: about22k/s,not a source/audio rate cap.
    stride=max(1,zone.rate//22050);rate=zone.rate/stride
    signal=np.empty((len(section)+stride-1)//stride,np.float64)
    if stride>1:
        # Anti-alias before the analysis-only decimation. Product audio and
        # immutable source samples are untouched. FFT lowpass avoids aliasing
        # ultrasonic content into a false fundamental at high source rates.
        margin=max(256,32*stride)
        for first in range(0,len(section),65536):
            if cancelled():raise RuntimeError(tr('creator.cancelled'))
            last=min(len(section),first+65536);begin=max(0,first-margin);end=min(len(section),last+margin)
            segment=section[begin:end,channel].astype(np.float64);spectrum=np.fft.rfft(segment)
            spectrum[np.fft.rfftfreq(len(segment),1/zone.rate)>rate*.45]=0
            filtered=np.fft.irfft(spectrum,len(segment))
            indices=np.arange((first+stride-1)//stride,(last+stride-1)//stride)
            signal[indices]=filtered[indices*stride-begin]
    else:signal[:]=section[:,channel]
    result=dict(pitch=None,bpm=None,pitch_reason='unstable_pitch',bpm_reason='unstable_tempo')
    if len(signal)/rate<.08:
        result.update(pitch_reason='pitch_too_short',bpm_reason='too_short');return result
    if len(signal)<32 or np.sqrt(np.mean(signal**2))<1e-5:
        result.update(pitch_reason='silence',bpm_reason='silence');return result
    rms=float(np.sqrt(np.mean(signal**2)))
    window=min(8192,len(signal));notes=[];active=0
    # Up to96 evenly distributed witnesses,batched16 to bound FFT workspace.
    starts=np.linspace(0,max(0,len(signal)-window),min(96,max(1,len(signal)//(window//2))),dtype=int)
    for first in range(0,len(starts),16):
        if cancelled():raise RuntimeError(tr('creator.cancelled'))
        frames=np.stack([signal[start:start+window] for start in starts[first:first+16]])
        frames-=frames.mean(axis=1,keepdims=True)
        frames=frames[np.sqrt(np.mean(frames**2,axis=1))>=max(1e-5,rms*.12)]
        active+=len(frames)
        if not len(frames):continue
        hz,depth=yin_frames(frames,rate)
        valid=(depth<.1)&np.isfinite(hz)&(hz>=20)&(hz<=min(5000,rate/2))
        notes.extend((69+12*np.log2(hz[valid]/440)).tolist())
    if notes and len(notes)>=max(1,active*.8):
        median=float(np.median(notes));spread=float(np.percentile(np.abs(np.array(notes)-median),90))*100
        if spread<30:
            result['pitch']=dict(note=round(median),cents=(median-round(median))*100,hz=440*2**((median-69)/12),spread_cents=spread)
            result['pitch_reason']=None
    if len(signal)/rate<4:
        result['bpm_reason']='too_short';return result
    # Log-mel spectral flux also observes constant-loudness note changes.
    # NumPy einsum(optimize=False) avoids spawning a BLAS thread pool beside
    # the realtime engine. At most64 STFT frames are materialized per chunk.
    hop=max(1,round(rate*.01));n_fft=1024
    padded=np.pad(signal,(n_fft//2,n_fft//2))
    frames=np.lib.stride_tricks.sliding_window_view(padded,n_fft)[::hop]
    basis=mel_basis(rate,n_fft);mel=np.empty((len(basis),len(frames)));energy=np.empty(len(frames))
    taper=np.hanning(n_fft+1)[:-1]
    for first in range(0,len(frames),64):
        if cancelled():raise RuntimeError(tr('creator.cancelled'))
        spectrum=abs(np.fft.rfft(frames[first:first+64]*taper,axis=1))**2
        mel[:,first:first+len(spectrum)]=np.einsum('mf,tf->mt',basis,spectrum,optimize=False)
        energy[first:first+len(spectrum)]=np.sqrt(np.mean(frames[first:first+64]**2,axis=1))
    log_mel=10*np.log10(np.maximum(1e-10,mel))
    log_mel=np.maximum(log_mel,float(np.max(log_mel))-80)
    onset=onset_strength(log_mel)
    # Abrupt note releases spread spectral energy too. Reject falling-energy
    # edges,while retaining equal-loudness changes between melodic notes.
    before=energy[np.maximum(0,np.arange(len(energy))-3)]
    after=energy[np.minimum(len(energy)-1,np.arange(len(energy))+3)]
    onset[after<before*.8]=0
    # Ignore padded edges and sub-salient fluctuations in steady tones/noise.
    edge=max(1,round(n_fft/(2*hop)));onset[:edge]=0;onset[-edge:]=0
    threshold=max(.5,float(np.max(onset))*.12,float(np.median(onset)+4*np.median(abs(onset-np.median(onset)))))
    peaks=[]
    for index in range(1,len(onset)-1):
        if onset[index]>threshold and onset[index]>=onset[index-1] and onset[index]>=onset[index+1]:
            if not peaks or index-peaks[-1]>.18*rate/hop:peaks.append(index)
            elif onset[index]>onset[peaks[-1]]:peaks[-1]=index
    if len(peaks)>=6:
        intervals=np.diff(peaks)*hop/rate;period=float(np.median(intervals));bpm=60/period
        # Independent onset-autocorrelation witness; no120BPM preference that
        # forces half/double tempo. Stable intervals and recurrence must agree.
        centered=onset-np.mean(onset)
        correlation=autocorrelate(centered)
        lag=round(period*rate/hop)
        strength=float(np.max(correlation[max(1,lag-1):lag+2]))/max(float(correlation[0]),1e-20)
        if 30<=bpm<=300 and strength>=.35 and np.percentile(abs(intervals-period),90)<max(.025,period*.08):
            result['bpm']=dict(value=bpm,beats=len(peaks),spread_ms=float(np.std(intervals))*1000)
        else:
            # Multiple instruments can add syncopated onsets between beats.
            # Use the recurring spectral pattern rather than requiring every
            # adjacent onset to be one quarter note. Earliest peak within85%
            # of the strongest recurrence avoids its slower integer multiples.
            low=max(2,math.ceil(60*rate/(300*hop)));high=min(len(onset)//3,math.floor(60*rate/(30*hop)))
            candidates=[i for i in range(low,high+1) if correlation[i]>correlation[i-1] and correlation[i]>=correlation[i+1]]
            if candidates:
                best=max(correlation[i] for i in candidates)
                candidates=[i for i in candidates if correlation[i]>=best*.85]
                lag=min(candidates);strength=correlation[lag]/max(correlation[0],1e-20)
                # Both chronological halves must independently show the same
                # recurrence; a global average must not conceal tempo changes.
                halves=np.array_split(centered,2)
                support=[];consistent=True
                for half in halves:
                    ac=autocorrelate(half)
                    support.append(float(max(ac[max(1,lag-1):lag+2]))/max(float(ac[0]),1e-20))
                    local=[i for i in range(low,min(high,len(half)//3)+1) if ac[i]>ac[i-1] and ac[i]>=ac[i+1]]
                    if not local:consistent=False;continue
                    maximum=max(ac[i] for i in local)
                    first=min(i for i in local if ac[i]>=maximum*.85)
                    consistent=consistent and abs(first-lag)<=max(2,lag*.08)
                if strength>=.45 and min(support)>=.3 and consistent:
                    a,b,c=correlation[lag-1:lag+2]
                    shift=.5*(a-c)/(a-2*b+c) if abs(a-2*b+c)>1e-12 else 0.
                    period=(lag+max(-.5,min(.5,shift)))*hop/rate
                    # Spread describes onset-grid residuals,not confidence or
                    # a guarantee that this is the score's metrical level.
                    residual=intervals-np.maximum(1,np.rint(intervals/period))*period
                    result['bpm']=dict(value=float(60/period),beats=len(peaks),spread_ms=float(np.std(residual))*1000)
    if result['bpm'] is not None:result['bpm_reason']=None
    return result


def _chunk(name, data):
    return name+struct.pack('<I',len(data))+data+(b'\0' if len(data)&1 else b'')


def _timecents(seconds):
    return -32768 if seconds == 0 else max(-12000,min(8000,round(1200*math.log2(seconds))))


def _text_chunk(name, data):
    # SoundFont INFO ZSTR lengths include their terminator and must be even;
    # generic RIFF's outside padding alone is not accepted by BASSMIDI here.
    data=data+b'\0'
    if len(data)&1:data+=b'\0'
    return _chunk(name,data)


def encode_sf2(draft,cancelled=lambda:False):
    draft.validate()
    zones = (_wave_zones(draft.wave,draft.wave_settings) if draft.mode == 'wave' else
             [prepared_zone(zone,cancelled) if zone.speed!=1 or zone.pitch!=0 or zone.reverse else zone for zone in draft.zones])
    # Harmonic pruning/interpolation of a drawn cycle can overshoot its input
    # points. Fit only an overflowing generated-wave adapter to signed16bit,
    # with one common scalar across all octave zones BEFORE authored gain;
    # gain therefore has no silent plateau at the top. The authored flag owns
    # this policy explicitly in every current saved recipe.
    wave_headroom=1.
    if draft.mode=='wave' and draft.wave_peak_fit:
        peak=max(float(np.max(np.abs(zone.pcm))) for zone in zones)
        if peak>1:wave_headroom=1/peak
    samples, headers, bags, generators, descriptors = [], [], [], [], []
    offset=0
    name=draft.name.encode('utf-8')[:19].decode('utf-8','ignore').encode('utf-8')
    for zone in zones:
        if cancelled():raise RuntimeError(tr('creator.cancelled'))
        end=zone.validate()
        # Crop for synthesis only; retain full PCM plus offsets for editable
        # source in the standard smpl pool. Generator offsets set trim bounds.
        indices=[]
        for channel in range(zone.pcm.shape[1]):
            indices.append(len(headers)); start=offset
            scaled=np.asarray(zone.pcm[:,channel],np.float64)*10**((draft.gain+zone.gain)/20)*wave_headroom
            if np.max(np.abs(scaled)) > 1+1e-12:
                raise ValueError(tr('creator.sample_clips'))
            pcm=np.rint(np.clip(scaled,-1,1)*32767).astype('<i2')
            samples.append(pcm.tobytes()+bytes(46*2))
            offset+=len(pcm)+46
            # sample header loop addresses are absolute and end-exclusive.
            loop_start=zone.loop_start if zone.loop else zone.start
            loop_end=zone.loop_end if zone.loop else end
            headers.append([name,start,start+len(pcm),start+loop_start,start+loop_end,zone.rate,zone.root,0,0,1])
        for channel,index in enumerate(indices):
            if len(indices)==2: headers[index][-2:]=[indices[1-channel],4 if channel==0 else 2]
            bags.append((len(generators),0))
            coarse,fine=(divmod(round(draft.wave_settings.pitch*100)+zone.tune,100) if draft.mode=='wave' and draft.wave_settings.pitch else (0,zone.tune))
            generators.extend([(43,zone.low | zone.high<<8),(44,zone.velocity_low | zone.velocity_high<<8),
                (34,_timecents(draft.attack)),(36,_timecents(draft.decay)),
                (37,1440 if draft.sustain==0 else round(-200*math.log10(draft.sustain/100))),
                (38,_timecents(draft.release)),(52,fine)])
            if coarse:generators.append((51,coarse))  # SF2 coarseTune,semitones;no SoundTouch cycle regeneration.
            generators.extend((spec.generator,spec.raw(getattr(draft,key))) for key,spec in NATIVE_SOUND_PARAMETERS.items()
                              if getattr(draft,key)!=spec.default)
            if zone.start:
                generators.extend([(4,zone.start//32768),(0,zone.start%32768)])
            trim=end-len(zone.pcm)
            if trim:
                coarse, fine=divmod(trim,32768);generators.extend([(12,coarse),(1,fine)])
            if len(indices)==2: generators.append((17,-500 if channel==0 else 500))
            if not zone.key_tracking:generators.append((56,0))  # SF2 scaleTuning:0 keeps one pitch over the key range.
            generators.extend([(54,zone.loop),(53,index)])  # sampleID is last
        if draft.mode == 'sample':
            descriptors.append(dict(name=zone.name,rate=zone.rate,root=zone.root,low=zone.low,high=zone.high,
                velocity_low=zone.velocity_low,velocity_high=zone.velocity_high,start=zone.start,end=zone.end,
                loop_start=zone.loop_start,loop_end=zone.loop_end,loop=zone.loop,tune=zone.tune,
                gain=0.,key_tracking=zone.key_tracking,sample_indices=indices))
    bags.append((len(generators),0)); generators.append((0,0))
    headers.append([b'EOS',offset,offset,offset,offset,44100,0,0,0,1])
    metadata=dict(schema=1,creator='ABMSynth',name=draft.name,bank=draft.bank,program=draft.program,mode=draft.mode,
        wave=draft.wave.tolist(),attack=draft.attack,decay=draft.decay,sustain=draft.sustain,release=draft.release,
        gain=0. if draft.mode=='sample' else draft.gain,zones=descriptors)
    metadata.update({key:getattr(draft,key) for key in NATIVE_SOUND_PARAMETERS})
    metadata['wave_peak_fit']=draft.wave_peak_fit
    metadata['wave_settings']=vars(draft.wave_settings).copy()
    # PCM contains the authored gain, so reopen with neutral gain to avoid
    # applying it a second time. Wave mode regenerates from the original points.
    info=b'INFO'+_chunk(b'ifil',struct.pack('<HH',2,1))+_chunk(b'isng',b'EMU8000\0')
    info+=_text_chunk(b'INAM',name)+_text_chunk(b'ISFT',b'ABMSynth')
    info+=_text_chunk(b'ICMT',json.dumps(metadata,ensure_ascii=True,separators=(',',':')).encode('ascii'))
    pdta=b'pdta'+b''.join(_chunk(key,value) for key,value in (
        (b'phdr',struct.pack('<20sHHHIII',name,draft.program,draft.bank,0,0,0,0)+struct.pack('<20sHHHIII',b'EOP',0,0,1,0,0,0)),
        (b'pbag',struct.pack('<HHHH',0,0,1,0)),(b'pmod',bytes(10)),(b'pgen',struct.pack('<HHHH',41,0,0,0)),
        (b'inst',struct.pack('<20sH20sH',name,0,b'EOI',len(bags)-1)),
        (b'ibag',b''.join(struct.pack('<HH',*bag) for bag in bags)),(b'imod',bytes(10)),
        (b'igen',b''.join(struct.pack('<HH',op,value & 0xffff) for op,value in generators)),
        (b'shdr',b''.join(struct.pack('<20sIIIIIBbHH',*header) for header in headers))))
    result=_chunk(b'RIFF',b'sfbk'+_chunk(b'LIST',info)+_chunk(b'LIST',b'sdta'+_chunk(b'smpl',b''.join(samples)))+_chunk(b'LIST',pdta))
    if len(result)>MAX_FILE_BYTES: raise ValueError(tr('creator.too_large'))
    return result


def save_sf2(draft, path, cancelled=lambda: False):
    data=encode_sf2(draft,cancelled)
    if cancelled(): raise RuntimeError(tr('creator.cancelled'))
    target=Path(path)
    fd,temporary=tempfile.mkstemp(prefix='.abmsynth-instrument-',suffix='.tmp',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as file:
            file.write(data);file.flush();os.fsync(file.fileno())
        if cancelled(): raise RuntimeError(tr('creator.cancelled'))
        os.replace(temporary,target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return str(target)


def load_sf2(path):
    if Path(path).stat().st_size>MAX_FILE_BYTES: raise ValueError(tr('creator.too_large'))
    data=Path(path).read_bytes()
    def chunks(begin,end):
        at=begin
        while at<end:
            if at+8>end: raise ValueError(tr('creator.invalid_project'))
            key,size=struct.unpack_from('<4sI',data,at);start=at+8;stop=start+size
            if stop>end: raise ValueError(tr('creator.invalid_project'))
            yield key,start,stop
            at=stop+(size&1)
    if data[:4]!=b'RIFF' or data[8:12]!=b'sfbk' or len(data)<12 or struct.unpack_from('<I',data,4)[0]+8!=len(data):
        raise ValueError(tr('creator.invalid_project'))
    entries={}
    for key,start,end in chunks(12,len(data)):
        if key==b'LIST':
            for child,a,b in chunks(start+4,end): entries[(data[start:start+4],child)]=data[a:b]
    try:
        comment=entries[b'INFO',b'ICMT']
        if len(comment)>256*1024:raise ValueError()
        from configuration_data import read_settings_json
        meta=read_settings_json(comment.rstrip(b'\0'))
        if meta['schema']!=1 or meta['creator']!='ABMSynth': raise ValueError()
        if not isinstance(meta['wave'],list) or len(meta['wave'])!=WAVE_POINTS:raise ValueError()
        draft=InstrumentDraft(**{key:meta[key] for key in ('name','bank','program','mode','attack','decay','sustain','release','gain')},
            wave=np.asarray(meta['wave'],np.float32),wave_peak_fit=meta['wave_peak_fit'],
            wave_settings=_stored_wave_settings(meta),**_native_sound_values(meta))
        pool=np.frombuffer(entries[b'sdta',b'smpl'],dtype='<i2')
        headers=list(struct.iter_unpack('<20sIIIIIBbHH',entries[b'pdta',b'shdr']))
        if len(meta['zones']) > MAX_ZONES:raise ValueError()
        total_bytes=0
        for item in meta['zones']:
            indices=item['sample_indices'];channels=[]
            if not 1<=len(indices)<=2: raise ValueError()
            zone_bytes=0
            for index in indices:
                if not isinstance(index,int) or not 0 <= index < len(headers)-1:raise ValueError()
                header=headers[index];start,end=header[1:3]
                if not 0<=start<end<=len(pool): raise ValueError()
                zone_bytes+=(end-start)*4
                if zone_bytes>MAX_SAMPLE_BYTES or total_bytes+zone_bytes>MAX_FILE_BYTES:raise ValueError()
                channels.append(pool[start:end].astype(np.float32)/32767)
            total_bytes+=zone_bytes
            zone=SampleZone(**{key:value for key,value in item.items() if key!='sample_indices'},pcm=np.column_stack(channels))
            draft.zones.append(zone)
        draft.validate()
        return draft
    except (KeyError,IndexError,TypeError,ValueError,struct.error) as exc:
        raise ValueError(tr('creator.invalid_project')) from exc


def render_preview(draft,path,note=69,velocity=127):
    """Private decoding stream; never steal a live channel/device or change INI."""
    save_sf2(draft,path)
    return render_compiled_preview(path,draft.bank,draft.program,note,velocity,draft.release)


def preview_note_duration(draft,note=69,velocity=127,override=0):
    """Visualization note length,not a change to the instrument's envelope.

    Finite source travel includes trim,tempo,key tracking and fine tuning.
    Cyclic sources need an intentional reference interval:at least1s,all
    envelope stages up to sustain,and one enabled LFO cycle after its delay.
    User override replaces only the held interval;full release still follows.
    """
    if type(override) not in (int,float) or not math.isfinite(override) or override<0:
        raise ValueError(tr('creator.invalid_preview_duration'))
    if override:return float(override)
    shape=draft.envelope_delay+draft.attack+draft.envelope_hold+draft.decay
    for kind in ('vibrato','tremolo'):
        if getattr(draft,kind+'_depth')>0:
            shape=max(shape,getattr(draft,kind+'_delay')+1/getattr(draft,kind+'_rate'))
    if draft.mode=='wave':return max(1.,shape)
    selected=[zone for zone in draft.zones if zone.low<=note<=zone.high and zone.velocity_low<=velocity<=zone.velocity_high]
    lengths=[((zone.end or len(zone.pcm))-zone.start)/zone.rate/zone.speed/
             2**(((note-zone.root) if zone.key_tracking else 0)/12+zone.tune/1200) for zone in selected]
    return max([1.,shape,*lengths]) if any(zone.loop for zone in selected) else max(lengths,default=1.)


def render_compiled_preview(path,bank,program,note=69,velocity=127,release=.1,cancelled=None,*,rate=44100,hold_seconds=1.):
    """Instrument-only signed float PCM from an already compiled private SF2.

    bass.h ChannelGetData decodes this stream only; never consume the live
    audition decoder. render_preview retains its44100Hz default; the UI uses
    its prepared audition's actual rate. The caller owns the reference held
    interval;the full authored release follows without a hidden150ms/3s clamp.
    A bounded private PCM payload prevents unchecked allocations. Blocks bound
    cancellation latency. BASSMIDI note1/drums6/bank10/program2 from bassmidi.h.
    """
    if (type(hold_seconds) not in (int,float) or not math.isfinite(hold_seconds) or hold_seconds<=0
            or type(release) not in (int,float) or not math.isfinite(release) or release<0):
        raise ValueError(tr('creator.invalid_preview_duration'))
    held=max(1,round(hold_seconds*rate));tail=max(0,round(release*rate))
    frames=held+tail
    if frames*2*np.dtype(np.float32).itemsize>MAX_PREVIEW_BYTES:
        raise ValueError(tr('creator.preview_duration_budget',seconds=max(0,MAX_PREVIEW_BYTES/(8*rate)-release)))
    from bass_bindings import bass,bassmidi,BASS_MIDI_FONT,BASS_UNICODE,BASS_SAMPLE_FLOAT,BASS_STREAM_DECODE,BASS_MIDI_NOFX
    font=bassmidi.BASS_MIDI_FontInit(ctypes.cast(ctypes.c_wchar_p(str(path)),ctypes.c_void_p),BASS_UNICODE)
    if not font: raise RuntimeError(tr('creator.native_failed',error=bass.BASS_ErrorGetCode()))
    stream=0
    try:
        if not bassmidi.BASS_MIDI_FontLoad(font,program,bank):
            raise RuntimeError(tr('creator.native_failed',error=bass.BASS_ErrorGetCode()))
        stream=bassmidi.BASS_MIDI_StreamCreate(16,BASS_SAMPLE_FLOAT|BASS_STREAM_DECODE|BASS_MIDI_NOFX,rate)
        if not stream or not bassmidi.BASS_MIDI_StreamSetFonts(stream,(BASS_MIDI_FONT*1)(BASS_MIDI_FONT(font,-1,0)),1):
            raise RuntimeError(tr('creator.native_failed',error=bass.BASS_ErrorGetCode()))
        # bassmidi.h: DRUMS6,BANK10,PROGRAM2,NOTE1; SF2 bank128 is drums.
        for event,value in ((6,int(bank==128)),(10,0 if bank==128 else bank),(2,program),(12,127),(14,127),(1,note|(velocity<<8))):
            if not bassmidi.BASS_MIDI_StreamEvent(stream,0,event,value):
                raise RuntimeError(tr('creator.native_failed',error=bass.BASS_ErrorGetCode()))
        data=np.zeros((frames,2),np.float32);offset=0
        for length,on in ((held,True),(tail,False)):
            if not on and not bassmidi.BASS_MIDI_StreamEvent(stream,0,1,note):
                raise RuntimeError(tr('creator.native_failed',error=bass.BASS_ErrorGetCode()))
            for first in range(0,length,4096):
                if cancelled and cancelled():raise RuntimeError(tr('creator.cancelled'))
                block=data[offset+first:offset+min(length,first+4096)]
                if bass.BASS_ChannelGetData(stream,block.ctypes.data,block.nbytes)!=block.nbytes:
                    raise RuntimeError(tr('creator.native_failed',error=bass.BASS_ErrorGetCode()))
            offset+=length
        return data
    finally:
        if stream:bass.BASS_StreamFree(stream)
        bassmidi.BASS_MIDI_FontFree(font)
