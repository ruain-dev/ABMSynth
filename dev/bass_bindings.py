# bass_bindings.py
import ctypes
import os
import threading
from dataclasses import dataclass
from i18n import tr
# Qt's Windows platform owns process DPI at QApplication construction.
# Loading audio libraries must not preempt it with System-DPI awareness.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class _ChannelTransactionStep(ctypes.Structure):
    _fields_=[('kind',ctypes.c_uint),('first',ctypes.c_uint),
             ('second',ctypes.c_uint64),('address',ctypes.c_uint64)]

class _ChannelFXParameterStep(ctypes.Structure):
    _fields_=[('handle',ctypes.c_uint),('reserved',ctypes.c_uint),('values',ctypes.c_uint64),('previous',ctypes.c_uint64)]


@dataclass(frozen=True)
class ChannelTransactionResult:
    completed:int=0
    error:int=0
    unlock_error:int=0
    phase:int=0

    @property
    def success(self):return self.phase==0


class NativeChannelTransaction:
    """Control-side lock/call/unlock without returning to Python while locked.

    bass.h BOOL WINAPI(DWORD,DWORD) RemoveDSP/RemoveSync, BOOL(DWORD) StreamFree, and own-DLL cdecl
    void(pointer) reset / void(pointer,int) meter clear only. Operation buffers/owners live through the call;
    callers apply the successful prefix and retain state on failure. ABI2
    count1024 is a product bound, not a BASS limit. No audio callback use.
    """
    def __init__(self,backend,native_path=None):
        self.backend=backend;self.library=None;self.execute=None;self.pending=None
        try:
            library=ctypes.CDLL(str(native_path or os.environ.get('ABMSYNTH_OBSERVATION_DLL',os.path.join(BASE_DIR,'abmsynth_dsp.dll'))))
            library.channel_transaction_version.argtypes=[];library.channel_transaction_version.restype=ctypes.c_uint
            if library.channel_transaction_version()!=2:return
            execute=library.channel_transaction_run
            execute.argtypes=[ctypes.c_uint,ctypes.POINTER(_ChannelTransactionStep),ctypes.c_uint,
                ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_int)];execute.restype=None
            unlock=library.channel_transaction_unlock
            unlock.argtypes=[ctypes.c_uint,ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_int)];unlock.restype=ctypes.c_int
            self.library=library;self.execute=execute;self.unlock=unlock
        except (OSError,AttributeError):pass # Exact old/mocked backend fallback.

    def parameters(self,stream,steps):
        """Additive same-topology vendor FX batch; None means missing ABI.

        bass.h BOOL WINAPI BASS_FXSetParameters(DWORD,const void*). Prepared
        ctypes structures and DLL functions live through forward/rollback/unlock.
        Envelope pointer-bearing parameters are not admitted by this caller.
        """
        modern=self.library is not None and hasattr(self.library,'channel_fx_parameters_v2_run')
        execute=getattr(self.library,'channel_fx_parameters_v2_run' if modern else 'channel_fx_parameters_run',None) if self.library is not None else None
        if execute is None:return None
        version=getattr(self.library,'channel_fx_parameters_v2_version' if modern else 'channel_fx_parameters_version');version.argtypes=[];version.restype=ctypes.c_uint
        if version()!=(2 if modern else 1):return None
        lock=getattr(self.backend,'BASS_ChannelLock',None);error=getattr(self.backend,'BASS_ErrorGetCode',None);setter=getattr(self.backend,'BASS_FXSetParameters',None)
        if not all(isinstance(fn,ctypes._CFuncPtr) for fn in (lock,error,setter)):return None
        steps=tuple(steps)
        if not 0<len(steps)<=32 or any(type(handle) is not int or not 0<handle<=0xffffffff for handle,_,_ in steps):
            raise ValueError('FX parameter transaction batch')
        if self.pending is not None:
            released=self.run(stream,())
            if not released.success:return (released.completed,released.error,released.unlock_error,released.phase,0,0)
        execute.argtypes=[ctypes.c_uint,ctypes.POINTER(_ChannelFXParameterStep),ctypes.c_uint,
            ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_int)];execute.restype=None
        data=(_ChannelFXParameterStep*len(steps))(*(_ChannelFXParameterStep(handle,0,ctypes.addressof(value),ctypes.addressof(previous))
            for handle,value,previous in steps));result=(ctypes.c_int*(7 if modern else 6))()
        execute(stream,data,len(data),ctypes.cast(lock,ctypes.c_void_p),ctypes.cast(error,ctypes.c_void_p),ctypes.cast(setter,ctypes.c_void_p),result)
        if result[3]==3:self.pending=(stream,threading.get_ident(),True)
        return tuple(result)

    def run(self,stream,steps):
        # Step=(kind,function,first,second); references are retained throughout.
        steps=tuple(steps)
        if len(steps)>1024 or any(kind not in (1,2,3,4) or not callable(function)
            or not 0<=first<=0xffffffff or not 0<=second<=0xffffffffffffffff
            for kind,function,first,second in steps):
            return ChannelTransactionResult(phase=4)
        lock=self.backend.BASS_ChannelLock
        error=getattr(self.backend,'BASS_ErrorGetCode',lambda:-1) # Explicit fake backend only.
        native=self.execute is not None and all(isinstance(fn,ctypes._CFuncPtr)
            for fn in (lock,error,*(step[1] for step in steps)))
        if self.pending is not None:
            previous,owner,previous_native=self.pending
            if owner!=threading.get_ident():
                raise RuntimeError(tr('runtime.native_transaction_thread_mismatch'))
            if previous_native:
                failure=ctypes.c_int()
                released=bool(self.unlock(previous,ctypes.cast(lock,ctypes.c_void_p),ctypes.cast(error,ctypes.c_void_p),ctypes.byref(failure)))
                code=failure.value
            else:
                released=bool(lock(previous,False));code=0 if released else int(error())
            # API success is the BOOL, not whether an injected error is nonzero.
            if not released:return ChannelTransactionResult(unlock_error=code,phase=3)
            self.pending=None
        if not steps:return ChannelTransactionResult()
        if native:
            buffer=(_ChannelTransactionStep*len(steps))(*(_ChannelTransactionStep(kind,first,second,
                ctypes.cast(function,ctypes.c_void_p).value) for kind,function,first,second in steps))
            result=(ctypes.c_int*4)()
            self.execute(stream,buffer,len(buffer),ctypes.cast(lock,ctypes.c_void_p),ctypes.cast(error,ctypes.c_void_p),result)
            record=ChannelTransactionResult(*result)
        else:
            if not lock(stream,True):return ChannelTransactionResult(error=int(error()),phase=1)
            completed=code=phase=0
            try:
                for kind,function,first,second in steps:
                    if kind==1 and not function(first,second):code=int(error());phase=2;break
                    if kind==2:function(second)
                    if kind==3:function(second,ctypes.c_int(first).value)
                    if kind==4 and not function(first):code=int(error());phase=2;break
                    completed+=1
            finally:
                unlocked=bool(lock(stream,False));unlock_error=0 if unlocked else int(error())
                if not unlocked:self.pending=(stream,threading.get_ident(),False)
            record=ChannelTransactionResult(completed,code,unlock_error,phase if unlocked else 3)
        if record.phase==3:self.pending=(stream,threading.get_ident(),native)
        return record


class AudioLibraryLoadError(ImportError):
    """Required native library failure; launch owner decides how to report it."""


def load_output_addons():
    """Lazy optional native analysis/recording DLLs; failure never stops audio.

    ABI: vendor/bassloud/c/bassloud.h and vendor/bassenc/c/bassenc.h.
    HLOUDNESS/HENCODE are DWORD; GetCount is QWORD; filenames use BASS_UNICODE.
    Owners must stop measurement/flush encoding before freeing their source.
    """
    loud = ctypes.WinDLL(os.path.join(BASE_DIR, "bassloud.dll"))
    enc = ctypes.WinDLL(os.path.join(BASE_DIR, "bassenc.dll"))
    for dll, name, args, result in (
        (loud, "BASS_Loudness_GetVersion", [], ctypes.c_uint),
        (enc, "BASS_Encode_GetVersion", [], ctypes.c_uint),
        (loud, "BASS_Loudness_Start", [ctypes.c_uint, ctypes.c_uint, ctypes.c_int], ctypes.c_uint),
        (loud, "BASS_Loudness_Stop", [ctypes.c_uint], ctypes.c_int),
        (loud, "BASS_Loudness_GetLevel", [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_float)], ctypes.c_int),
        (enc, "BASS_Encode_StartPCMFile", [ctypes.c_uint, ctypes.c_uint, ctypes.c_wchar_p], ctypes.c_uint),
        (enc, "BASS_Encode_StopEx", [ctypes.c_uint, ctypes.c_uint], ctypes.c_int),
        (enc, "BASS_Encode_SetPaused", [ctypes.c_uint, ctypes.c_int], ctypes.c_int),
        (enc, "BASS_Encode_GetCount", [ctypes.c_uint, ctypes.c_uint], ctypes.c_uint64),
        (enc, "BASS_Encode_IsActive", [ctypes.c_uint], ctypes.c_uint),
        # bassenc.h Write: manually feed channel-format/float samples even
        # while the encoder's automatic DSP feed is paused. BOOL return.
        (enc, "BASS_Encode_Write", [ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint], ctypes.c_int),
    ):
        function = getattr(dll, name)
        function.argtypes, function.restype = args, result
    if loud.BASS_Loudness_GetVersion() >> 16 != 0x204 or enc.BASS_Encode_GetVersion() >> 16 != 0x204:
        raise OSError(tr("output_tools.addon_version"))
    return loud, enc


def load_recording_codec(kind):
    """Lazy official encoder ABI; common HENCODE lifetime stays in BASSenc.

    vendor/bassenc_{flac,ogg,opus,mp3}/c/*.h: WINAPI StartFile(DWORD,
    options, DWORD flags, filename); BASS_UNICODE makes both strings WCHAR*.
    StartUser flags/async queue apply; StopEx/SetPaused/GetCount belong to
    bassenc.dll, not the codec DLL. Loading failure never stops playback.
    """
    name = {"flac": "flac", "ogg_flac": "flac", "vorbis": "ogg", "opus": "opus", "mp3": "mp3"}[kind]
    dll = ctypes.WinDLL(os.path.join(BASE_DIR, f"bassenc_{name}.dll"))
    version = getattr(dll, f"BASS_Encode_{name.upper()}_GetVersion")
    version.argtypes, version.restype = [], ctypes.c_uint
    value = version()
    if value >> 16 != 0x204 or name == "flac" and value < 0x2040400:
        raise OSError(tr("output_tools.addon_version"))
    start = getattr(dll, f"BASS_Encode_{name.upper()}_StartFile")
    start.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_wchar_p]
    start.restype = ctypes.c_uint
    return dll, start

# --- ?쇱씠釉뚮윭由?濡쒕뱶 ---
try:
    bass = ctypes.cdll.LoadLibrary(os.path.join(BASE_DIR, "bass.dll"))
    bassmidi = ctypes.cdll.LoadLibrary(os.path.join(BASE_DIR, "bassmidi.dll"))
    # bass_fx.h BASS_FX_GetVersion: loading the add-on registers BQF with
    # BASS_ChannelSetFX. It owns no stream handle; BASS_Free remains last.
    bassfx = ctypes.cdll.LoadLibrary(os.path.join(BASE_DIR, "bass_fx.dll"))
    bassfx.BASS_FX_GetVersion.argtypes = []
    bassfx.BASS_FX_GetVersion.restype = ctypes.c_uint
    bassfx.BASS_FX_GetVersion()
    # bass_fx.h: HSTREAM BASS_FX_TempoCreate(DWORD,DWORD). Source must be
    # DECODE; FREESOURCE transfers ownership only on successful creation.
    bassfx.BASS_FX_TempoCreate.argtypes = [ctypes.c_uint, ctypes.c_uint]
    bassfx.BASS_FX_TempoCreate.restype = ctypes.c_uint
except OSError as e:
    raise AudioLibraryLoadError(tr("error.dll_load_body", error=e)) from e

# Active MIDI input belongs to the in-repository WinMM driver/pipe. Archived
# teVirtualMIDI experiments own their loader; do not eagerly load their DLL
# into this product or introduce an undeclared release dependency.

# --- ?곸닔 ?뺤쓽 ---
BASS_DEVICE_DEFAULT = 2
BASS_DEVICE_ENABLED = 1
BASS_DEVICE_DEFAULTCOM = 128
BASS_CONFIG_BUFFER = 0
BASS_CONFIG_UPDATEPERIOD = 1
BASS_CONFIG_MIDI_VOICES = 0x10401
# bassmidi.h/md BASS_CONFIG_MIDI_COMPACT/SAMPLEREAD: global config; chunk >=8192.
BASS_CONFIG_MIDI_COMPACT = 0x10400
BASS_CONFIG_MIDI_SAMPLEREAD = 0x10408
# bassmidi.h/md: process-wide UTF-8 SFZ header, copied by SetConfigPtr and
# prepended on FontInit only. GetConfigPtr returns BASS-owned memory.
BASS_CONFIG_MIDI_SFZHEAD = 0x10409
BASS_CONFIG_UPDATETHREADS = 24
BASS_CONFIG_FLOATDSP = 9
# bass.h: BASS_CONFIG_UNICODE=42. bass.md: request UTF-8 device names
# before the first BASS_GetDeviceInfo/BASS_Init call on Windows.
BASS_CONFIG_UNICODE = 42
BASS_MIDI_EVENTS_RAW = 0x10000
BASS_MIDI_EVENTS_STRUCT = 0
BASS_MIDI_EVENTS_CANCEL = 0x4000000  # bassmidi.h: pending event cancellation, whole stream.
BASS_MIDI_EVENTS_TIME = 0x8000000  # relative BASS_MIDI_EVENT tick/byte positions.
BASS_MIDI_EVENTS_ABSTIME = 0x10000000  # absolute processing positions, not wall clock.
BASS_MIDI_EVENTS_SYNC = 0x1000000  # bassmidi.h: trigger subscribed event syncs.
# bassmidi.md: flush pending asynchronous MIDI events before applying this batch.
BASS_MIDI_EVENTS_FLUSH = 0x80000000
# bassmidi.h: 0x1000 is DECAYEND, not NOFX. NOFX disables the native
# reverb/chorus engines; tests must verify the installed stream's flags.
BASS_MIDI_NOFX = 0x2000
# bassmidi.h / bassmidi.md: BMSynth 00476C58..00476CD8 explicitly seeds
# every drum key's reverb/chorus sends to full. The current BASSMIDI default
# is lower; NODRUMPARAM restores full reverb/chorus/UserFX on stream creation
# and drum program/system resets. UserFX alone remains useful in probes.
BASS_MIDI_NODRUMPARAMUSER = 0x200
BASS_MIDI_NODRUMPARAM = 0x400
BASS_MIDI_FONT_NOFX = 0x80000
BASS_MIDI_FONT_MINFX = 0x1000000
BASS_MIDI_FONT_STEREO = 0x4000000  # upgraded bassmidi.h: detected SF2 pair CC10 balance.
BASS_MIDI_CHAN_USERFX = 0xFFFFFFFD
BASS_MIDI_SINCINTER = 0x800000
BASS_MIDI_ASYNC = 0x400000
BASSMIDI_REVERB_SYS_GS  = 0x80  # HIBYTE = 0x80 ??GS
BASSMIDI_REVERB_SYS_GM2 = 0x81  # HIBYTE = 0x81 ??GM2
BASSMIDI_REVERB_SYS_XG  = 0x00  # (HIBYTE != 0x80/0x81) ??XG (LOBYTE留??ъ슜)
MIDI_EVENT_PROGRAM = 2
MIDI_EVENT_VOLUME = 12
MIDI_EVENT_PAN = 13
# bassmidi.h2.4.16 / StreamEvent: CC42 low pan part, native0..128.
MIDI_EVENT_PAN_LSB = 85
MIDI_EVENT_PITCH = 4       # bassmidi.h: 14-bit wheel, 8192 = center.
MIDI_EVENT_MODULATION = 11  # bassmidi.h: MIDI CC#1, 0..127.
MIDI_EVENT_EXPRESSION = 14  # bassmidi.h: MIDI CC#11, 0..127.
MIDI_EVENT_BANK = 10
# bassmidi.h / StreamEvent: separate 7-bit bank halves; DRUMS changes
# percussion mode and resets bank/program. DEFDRUMS also changes reset seed.
MIDI_EVENT_BANK_LSB = 70
MIDI_EVENT_DRUMS = 6
MIDI_EVENT_DEFDRUMS = 0x10006
BASS_ATTRIB_MIDI_CHANS = 0x12002
MIDI_EVENT_MASTERVOL = 9
MIDI_EVENT_ATTACK = 28
MIDI_EVENT_MIXLEVEL = 0x10000
MIDI_EVENT_CUTOFF = 25
MIDI_EVENT_RESONANCE = 26
MIDI_EVENT_RELEASE = 27
MIDI_EVENT_DECAY = 29
MIDI_EVENT_SYSTEM = 61
MIDI_EVENT_SYSTEMEX = 0x10002  # bassmidi.h: SYSTEM reset including additional allocated channels.
MIDI_EVENT_REVERB = 23       # per-channel reverb send (CC#91 equivalent)
MIDI_EVENT_USERFX = 43       # per-channel user-effect send (CC#94 equivalent)
MIDI_EVENT_CONTROL = 64      # bassmidi.h: unhandled CC; low byte=#, high byte=value
MIDI_EVENT_DRUM_USERFX = 58  # per-key user-effect send (NRPN 1Fknh)
MIDI_EVENT_CHORUS = 24       # per-channel chorus send (CC#93 equivalent)
MIDI_EVENT_REVERB_MACRO = 30
MIDI_EVENT_CHORUS_MACRO = 31
MIDI_EVENT_CHORUS_DELAY = 37
MIDI_EVENT_CHORUS_DEPTH = 38
MIDI_EVENT_CHORUS_RATE = 39
MIDI_EVENT_CHORUS_FEEDBACK = 40
MIDI_EVENT_REVERB_LEVEL = 36
MIDI_EVENT_REVERB_TIME     = 32
MIDI_EVENT_REVERB_DELAY    = 33
MIDI_EVENT_REVERB_LOCUTOFF = 34
MIDI_EVENT_REVERB_HICUTOFF = 35
MIDI_EVENT_CHORUS_LEVEL = 41
MIDI_EVENT_CHORUS_REVERB = 42
MIDI_EVENT_SOUNDOFF = 16
MIDI_EVENT_RESET = 17
MIDI_EVENT_NOTESOFF = 18
# bassmidi.h / BASS_MIDI_StreamEvent: CC65 switch and CC126/127 channel mode.
# MIDI_EVENT_PORTATIME follows the same channel controller when the user edits
# it; incoming CC5 can still take precedence on the live stream.
MIDI_EVENT_PORTAMENTO = 19
MIDI_EVENT_PORTATIME = 20
MIDI_EVENT_NOTE = 1  # bassmidi.h: LOBYTE key, HIBYTE velocity.
MIDI_EVENT_NOTES = 0x20000  # bassmidi.h/StreamGetEvent: distinct pressed keys,not sounding release voices.
MIDI_EVENT_KEYPRES = 71  # bassmidi.h: LOBYTE key, HIBYTE poly pressure.
MIDI_EVENT_CHANPRES = 3
MIDI_EVENT_SUSTAIN = 15
MIDI_EVENT_SOSTENUTO = 76
MIDI_EVENT_SOFT = 60
MIDI_EVENT_CHANPRES_VIBRATO = 65
MIDI_EVENT_CHANPRES_PITCH = 66
MIDI_EVENT_CHANPRES_FILTER = 67
MIDI_EVENT_CHANPRES_VOLUME = 68
MIDI_EVENT_KEYPRES_VIBRATO = 72
MIDI_EVENT_KEYPRES_PITCH = 73
MIDI_EVENT_KEYPRES_FILTER = 74
MIDI_EVENT_KEYPRES_VOLUME = 75
# bassmidi.h: per-channel CC1 destination amounts; vibrato also MIDI RPN5.
MIDI_EVENT_MOD_VIBRATO = 69
MIDI_EVENT_MOD_PITCH = 77
MIDI_EVENT_MOD_FILTER = 78
MIDI_EVENT_MOD_VOLUME = 79
MIDI_EVENT_TRANSPOSE = 0x10001
MIDI_EVENT_MODE = 22
MIDI_EVENT_FINETUNE = 7  # bassmidi.h: channel RPN 1, 8192 = neutral.
MIDI_EVENT_COARSETUNE = 8  # bassmidi.h: channel RPN 2, 64 = neutral.
# bassmidi.h / bassmidi.md BASS_MIDI_StreamEvent: per-drum key controls,
# pitch-class tuning (LOWORD fine14, HIWORD class), global master fine tune.
MIDI_EVENT_DRUM_PAN = 52
MIDI_EVENT_DRUM_FINETUNE = 50  # bassmidi.h: NRPN19, key LOWBYTE/value HIGHBYTE.
MIDI_EVENT_DRUM_COARSETUNE = 51
MIDI_EVENT_DRUM_CUTOFF = 55
MIDI_EVENT_DRUM_RESONANCE = 56
MIDI_EVENT_DRUM_REVERB = 53
MIDI_EVENT_DRUM_CHORUS = 54
MIDI_EVENT_DRUM_LEVEL = 57
MIDI_EVENT_SCALETUNING = 63
MIDI_EVENT_MASTER_FINETUNE = 83
MIDI_EVENT_MASTER_COARSETUNE = 84  # bassmidi.h/md: global, raw 64 = neutral semitone offset.
# bassmidi.h BASS_MIDI_StreamEvent: per-channel expression. 64 is the
# SoundFont-relative neutral value for vibrato rate/depth/delay.
MIDI_EVENT_PITCHRANGE = 5
MIDI_EVENT_VIBRATO_RATE = 80
MIDI_EVENT_VIBRATO_DEPTH = 81
MIDI_EVENT_VIBRATO_DELAY = 82

MIDI_SYSTEM_GM1 = 1
MIDI_SYSTEM_GM2	= 2
MIDI_SYSTEM_XG = 3
MIDI_SYSTEM_GS = 4
TE_VM_FLAGS_PARSE_RX = 1
BASS_ATTRIB_BUFFER = 13
BASS_ATTRIB_CPU = 7
BASS_ATTRIB_MIDI_SRC = 0x12006
BASS_ATTRIB_MIDI_CPU = 0x12001  # bassmidi.h; automatic0, >100 no CPU-driven voice kill.
# bassmidi.h/md BASS_ATTRIB_MIDI_VOICES: stream lifetime, live sample voice limit.
BASS_ATTRIB_MIDI_VOICES = 0x12003
BASS_ATTRIB_MIDI_VOL = 0x1200a
BASS_ATTRIB_MIDI_REVERB = 0x12009
BASS_ATTRIB_MIDI_VOICES_ACTIVE = 0x12004
BASS_FX_DX8_ECHO = 0x00000003
BASS_FX_DX8_FLANGER = 0x00000004

BASS_DX8_PHASE_NEG_180 = 0 
BASS_DX8_PHASE_NEG_90 = 1 
BASS_DX8_PHASE_ZERO = 2 
BASS_DX8_PHASE_90 = 3 
BASS_DX8_PHASE_180 = 4

BASS_3DMODE_NORMAL = 0
BASS_3DMODE_RELATIVE = 1
BASS_3DMODE_OFF = 2

# BASS_ACTIVE ?곹깭
BASS_ACTIVE_STOPPED = 0
BASS_ACTIVE_PLAYING = 1
BASS_ACTIVE_STALLED = 2
BASS_ACTIVE_PAUSED = 3
BASS_ACTIVE_PAUSED_DEVICE = 4

# Stall 媛먯? ?곸닔
BASS_SYNC_STALL = 6

# --- ?ㅽ뀒?덉삤 ?뺤옣 DSP???뚮옒洹????---
BASS_SAMPLE_FLOAT = 0x100
BASS_STREAM_DECODE = 0x200000  # bass.h: pull-only stream for BASSASIO output
BASS_DATA_FLOAT = 0x40000000
# ``STREAMPROC_DEVICE`` in bass.h is the pointer sentinel ``(STREAMPROC*)-2``.
# It creates the current device's final output mix stream, which is always
# floating-point. Keep the sentinel as an integer and marshal it as c_void_p
# at the BASS_StreamCreate call site.
BASS_STREAMPROC_DEVICE = -2
# bass.h BASS_SYNC_FREE=8: STREAMPROC_DEVICE is freed when the output mix
# format changes. Its SYNCPROC must remain alive while the sync is installed.
BASS_SYNC_FREE = 8

# ctypes 肄쒕갚/?쒓렇?덉쿂 ??낅뱾
# bass.h: all BASS handles are DWORD, not pointer-sized or signed int.
# Native callbacks borrow these handles; their unsigned identity must match
# API returns even when bit31 is set. Retain callbacks until removal/free.
DSPPROC = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p)
SYNCPROC = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)
TE_MIDI_RECEIVE_PROC = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p)

# --- 援ъ“泥?---
class BASS_DX8_CHORUS(ctypes.Structure):
    _fields_ = [
        ("fWetDryMix", ctypes.c_float),
        ("fDepth", ctypes.c_float),
        ("fFeedback", ctypes.c_float),
        ("fFrequency", ctypes.c_float),
        ("lWaveform", ctypes.c_uint),
        ("fDelay", ctypes.c_float),
        ("lPhase", ctypes.c_uint)
    ]

class BASS_DX8_ECHO(ctypes.Structure):
    _fields_ = [
        ("fWetDryMix", ctypes.c_float),
        ("fFeedback", ctypes.c_float),
        ("fLeftDelay", ctypes.c_float),
        ("fRightDelay", ctypes.c_float),
        ("lPanDelay", ctypes.c_int)
    ]

class BASS_DX8_FLANGER(ctypes.Structure):
    _fields_ = [
        ("fWetDryMix", ctypes.c_float),
        ("fDepth", ctypes.c_float),
        ("fFeedback", ctypes.c_float),
        ("fFrequency", ctypes.c_float),
        ("lWaveform", ctypes.c_uint),
        ("fDelay", ctypes.c_float),
        ("lPhase", ctypes.c_uint)
    ]

class BASS_3DVECTOR(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float), ("z", ctypes.c_float)]

class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, guid_string=None):
        import uuid
        if guid_string:
            guid = uuid.UUID(guid_string)
            self.Data1 = guid.time_low
            self.Data2 = guid.time_mid
            self.Data3 = guid.time_hi_version
            self.Data4 = (ctypes.c_ubyte * 8).from_buffer_copy(guid.bytes[8:])

class BASS_DEVICEINFO(ctypes.Structure):
    # bass.h: flags is DWORD, including the upper-byte device-type mask.
    _fields_ = [("name", ctypes.c_char_p), ("driver", ctypes.c_char_p), ("flags", ctypes.c_uint)]

class BASS_MIDI_FONT(ctypes.Structure):
    _fields_ = [("font", ctypes.c_uint), ("preset", ctypes.c_int), ("bank", ctypes.c_int)]

class BASS_MIDI_FONTEX(ctypes.Structure):
    # bassmidi.h2.4.16: independent source/destination,24bytes. StreamSetFonts
    # reads this array during the call;caller retains fonts until stream detach.
    _fields_ = [("font",ctypes.c_uint),("spreset",ctypes.c_int),("sbank",ctypes.c_int),
                ("dpreset",ctypes.c_int),("dbank",ctypes.c_int),("dbanklsb",ctypes.c_int)]

BASS_MIDI_FONT_EX = 0x1000000  # StreamSet/GetFonts count flag,not FontInit flags.

class BASS_MIDI_EVENT(ctypes.Structure):
    """BASS_MIDI_EVENT field order from bassmidi.h / bassmidi.md."""
    _fields_ = [
        ("event", ctypes.c_uint),
        ("param", ctypes.c_uint),
        ("chan", ctypes.c_uint),
        ("tick", ctypes.c_uint),
        ("pos", ctypes.c_uint),
    ]

# bassmidi.h MIDIFILTERPROC: BOOL CALLBACK(HSTREAM, int, EVENT*, BOOL, void*).
# BASS_MIDI_StreamSetFilter owns no callback memory; retain it for the stream
# lifetime. Only EVENTS_FILTER requests this callback for manually sent data.
MIDIFILTERPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint, ctypes.c_int,
                                  ctypes.POINTER(BASS_MIDI_EVENT), ctypes.c_int,
                                  ctypes.c_void_p)
BASS_MIDI_EVENTS_FILTER = 0x40000000
bassmidi.BASS_MIDI_StreamSetFilter.argtypes = [ctypes.c_uint, ctypes.c_int,
                                             ctypes.c_void_p, ctypes.c_void_p]
bassmidi.BASS_MIDI_StreamSetFilter.restype = ctypes.c_int

class BASS_MIDI_FONTINFO(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p), ("copyright", ctypes.c_char_p), ("comment", ctypes.c_char_p),
        ("presets", ctypes.c_uint), ("samsize", ctypes.c_uint), ("samload", ctypes.c_uint),
        ("samtype", ctypes.c_uint),
    ]


class BASS_INFO(ctypes.Structure):
    """bass.h BASS_INFO; bass.md latency/minbuf are milliseconds, freq is Hz."""
    _fields_ = [("flags", ctypes.c_uint), ("reserved", ctypes.c_uint * 7),
                *((name, ctypes.c_uint) for name in
                  ("minbuf", "dsver", "latency", "initflags", "speakers", "freq"))]


class BASS_CHANNELINFO(ctypes.Structure):
    """Exact BASS_CHANNELINFO field order from the local BASS 2.4 header."""
    _fields_ = [
        ("freq", ctypes.c_uint),
        ("chans", ctypes.c_uint),
        ("flags", ctypes.c_uint),
        ("ctype", ctypes.c_uint),
        ("origres", ctypes.c_uint),
        ("plugin", ctypes.c_uint),
        ("sample", ctypes.c_uint),
        ("filename", ctypes.c_char_p),
    ]

# --- ?⑥닔 ?쒓렇?덉쿂 諛붿씤??---
bass.BASS_ChannelSetDSP.argtypes = [ctypes.c_uint, DSPPROC, ctypes.c_void_p, ctypes.c_int]
bass.BASS_ChannelSetDSP.restype  = ctypes.c_uint
bass.BASS_ChannelRemoveDSP.argtypes = [ctypes.c_uint, ctypes.c_uint]
bass.BASS_ChannelLock.argtypes = [ctypes.c_uint, ctypes.c_int]
bass.BASS_ChannelLock.restype = ctypes.c_int
# bass.h/md BASS_ChannelLock: guard DSP owner destruction/configuration;
# never acquire from a DSP callback, and unlock before BASS_Free.
bass.BASS_ChannelRemoveDSP.restype  = ctypes.c_int

bass.BASS_ChannelSetFX.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_int]
bass.BASS_ChannelSetFX.restype = ctypes.c_uint
# bass.h/md: accepts either HFX or HDSP; changes order without clearing state.
bass.BASS_FXSetPriority.argtypes = [ctypes.c_uint, ctypes.c_int]
bass.BASS_FXSetPriority.restype = ctypes.c_int
bass.BASS_FXSetParameters.argtypes = [ctypes.c_uint, ctypes.c_void_p]
bass.BASS_FXSetParameters.restype = ctypes.c_int
bass.BASS_FXGetParameters.argtypes = [ctypes.c_uint, ctypes.c_void_p]
bass.BASS_FXGetParameters.restype = ctypes.c_int
bass.BASS_ChannelRemoveFX.argtypes = [ctypes.c_uint, ctypes.c_uint]
bass.BASS_ChannelRemoveFX.restype = ctypes.c_int

bass.BASS_Set3DFactors.argtypes = [ctypes.c_float, ctypes.c_float, ctypes.c_float]
bass.BASS_Set3DFactors.restype = ctypes.c_int
bass.BASS_Get3DFactors.argtypes = [ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float)]
bass.BASS_Get3DFactors.restype = ctypes.c_int
bass.BASS_Set3DPosition.argtypes = [ctypes.POINTER(BASS_3DVECTOR), ctypes.POINTER(BASS_3DVECTOR), ctypes.POINTER(BASS_3DVECTOR), ctypes.POINTER(BASS_3DVECTOR)]
bass.BASS_Set3DPosition.restype = ctypes.c_int
bass.BASS_Get3DPosition.argtypes = [ctypes.POINTER(BASS_3DVECTOR), ctypes.POINTER(BASS_3DVECTOR), ctypes.POINTER(BASS_3DVECTOR), ctypes.POINTER(BASS_3DVECTOR)]
bass.BASS_Get3DPosition.restype = ctypes.c_int
bass.BASS_Apply3D.restype = None

BASS_ATTRIB_TAIL = 16
BASS_UNICODE = 0x80000000

bass.BASS_Init.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
# bass.h BOOL WINAPI BASS_Stop(void); bass.md: stop current-device output
# before final-mix DSP/sync retirement and BASS_Free, never a live UI refresh.
bass.BASS_Stop.argtypes = []
bass.BASS_Stop.restype = ctypes.c_int
bass.BASS_GetConfig.argtypes = [ctypes.c_uint]
bass.BASS_GetConfig.restype = ctypes.c_uint
bass.BASS_GetConfigPtr.argtypes = [ctypes.c_uint]
bass.BASS_GetConfigPtr.restype = ctypes.c_void_p
bass.BASS_GetDeviceInfo.argtypes = [ctypes.c_uint, ctypes.POINTER(BASS_DEVICEINFO)]
bass.BASS_GetDeviceInfo.restype = ctypes.c_int
# bass.h: BASS_ChannelGetDevice(DWORD handle) -> DWORD and
# BASS_SetDevice(DWORD device) -> BOOL. bass.md: device selection is local to
# each thread; the SoundFont-load worker adopts its stream's output device.
bass.BASS_ChannelGetDevice.argtypes = [ctypes.c_uint]
bass.BASS_ChannelGetDevice.restype = ctypes.c_uint
bass.BASS_SetDevice.argtypes = [ctypes.c_uint]
bass.BASS_SetDevice.restype = ctypes.c_int
bass.BASS_GetDevice.argtypes = []
bass.BASS_GetDevice.restype = ctypes.c_uint
bass.BASS_StreamCreate.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
bass.BASS_StreamCreate.restype = ctypes.c_uint
# bass.h: temporary HSTREAM lifetime ends with StreamFree. bass.md: on a
# FLOAT decoding dummy stream GetData transforms caller-owned float samples.
bass.BASS_StreamFree.argtypes = [ctypes.c_uint]
bass.BASS_StreamFree.restype = ctypes.c_int
bass.BASS_ChannelGetData.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
bass.BASS_ChannelGetData.restype = ctypes.c_uint
bassmidi.BASS_MIDI_StreamCreate.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
bassmidi.BASS_MIDI_StreamCreate.restype = ctypes.c_uint
bass.BASS_SetConfig.argtypes = [ctypes.c_uint, ctypes.c_uint]
bassmidi.BASS_MIDI_FontInit.argtypes = [ctypes.c_void_p, ctypes.c_uint]
bassmidi.BASS_MIDI_FontInit.restype = ctypes.c_uint
bassmidi.BASS_MIDI_FontFree.argtypes = [ctypes.c_uint]
bassmidi.BASS_MIDI_FontFree.restype = ctypes.c_int
# bassmidi.h: BOOL BASS_MIDI_FontLoad(HSOUNDFONT,int preset,int bank).
# bassmidi.md: -1/-1 preloads every preset; preloaded samples remain loaded
# until FontCompact/FontUnload or FontFree. Never call StreamLoadSamples on a
# real-time event stream: that API returns BASS_ERROR_NOTAVAIL there.
bassmidi.BASS_MIDI_FontLoad.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int]
bassmidi.BASS_MIDI_FontLoad.restype = ctypes.c_int
# bassmidi.h: FontLoadEx(HSOUNDFONT,int,int,DWORD,DWORD).
# bassmidi.md: NOWAIT (1) queues sample reads; the caller must retain the
# font until completion or free an uncommitted font to cancel those reads.
BASS_MIDI_FONTLOAD_NOWAIT = 1
BASS_CONFIG_MIDI_SAMPLELOADING = 0x1040a
bassmidi.BASS_MIDI_FontLoadEx.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
bassmidi.BASS_MIDI_FontLoadEx.restype = ctypes.c_int
bassmidi.BASS_MIDI_FontSetVolume.argtypes = [ctypes.c_uint, ctypes.c_float]
bassmidi.BASS_MIDI_FontSetVolume.restype = ctypes.c_int
bassmidi.BASS_MIDI_FontGetInfo.argtypes = [ctypes.c_uint, ctypes.POINTER(BASS_MIDI_FONTINFO)]
bassmidi.BASS_MIDI_FontGetInfo.restype = ctypes.c_int
bassmidi.BASS_MIDI_FontGetPresets.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
bassmidi.BASS_MIDI_FontGetPresets.restype = ctypes.c_int
bassmidi.BASS_MIDI_FontGetPreset.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int]
bassmidi.BASS_MIDI_FontGetPreset.restype = ctypes.c_char_p
bassmidi.BASS_MIDI_StreamSetFonts.argtypes = [ctypes.c_uint, ctypes.POINTER(BASS_MIDI_FONT), ctypes.c_uint]
bassmidi.BASS_MIDI_StreamSetFonts.restype = ctypes.c_int
bassmidi.BASS_MIDI_StreamLoadSamples.argtypes = [ctypes.c_uint]
bassmidi.BASS_MIDI_StreamLoadSamples.restype = ctypes.c_int
bassmidi.BASS_MIDI_StreamEvents.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
bassmidi.BASS_MIDI_StreamEvents.restype = ctypes.c_uint
bassmidi.BASS_MIDI_StreamEvent.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
bassmidi.BASS_MIDI_StreamEvent.restype = ctypes.c_int
# bassmidi.h declares a DWORD return; bassmidi.md specifies -1 on error.
bassmidi.BASS_MIDI_StreamGetEvent.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
bassmidi.BASS_MIDI_StreamGetEvent.restype = ctypes.c_uint
# bassmidi.h / bassmidi.md: current resolved source font/preset. It may be
# available before Note On when a mapping already resolves the selection;
# always check BOOL and font. The returned font remains borrowed, not owned.
bassmidi.BASS_MIDI_StreamGetPreset.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(BASS_MIDI_FONT)]
bassmidi.BASS_MIDI_StreamGetPreset.restype = ctypes.c_int
# bassmidi.h / StreamGetFonts: caller owns the descriptor array only. Font
# handles remain borrowed until all mapped streams detach; -1 is failure.
bassmidi.BASS_MIDI_StreamGetFonts.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
bassmidi.BASS_MIDI_StreamGetFonts.restype = ctypes.c_uint
bassmidi.BASS_MIDI_FontFlags.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
bassmidi.BASS_MIDI_FontFlags.restype = ctypes.c_uint
bassmidi.BASS_MIDI_StreamGetChannel.argtypes = [ctypes.c_uint, ctypes.c_uint]
bassmidi.BASS_MIDI_StreamGetChannel.restype = ctypes.c_uint
bass.BASS_ChannelSetAttribute.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_float]
bass.BASS_ChannelGetAttribute.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_float)]
bass.BASS_ChannelGetInfo.argtypes = [ctypes.c_uint, ctypes.POINTER(BASS_CHANNELINFO)]
bass.BASS_ChannelGetInfo.restype = ctypes.c_int
# bass.h: DWORD filetype, const void* file, QWORD offset/length, DWORD flags.
# This binding supports filetype=0 + UNICODE filenames only (all active callers).
# bass.md: FLOAT|DECODE yields owned decoded data; StreamFree ends file lifetime.
bass.BASS_StreamCreateFile.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint]
bass.BASS_StreamCreateFile.restype = ctypes.c_uint
bass.BASS_ChannelGetLength.argtypes = [ctypes.c_uint, ctypes.c_uint]
bass.BASS_ChannelGetLength.restype = ctypes.c_uint64
# bass.h QWORD BASS_ChannelGetPosition(DWORD,DWORD), including -1 sentinel.
# Bind before any native actor borrows its address; never depend on a timing
# probe having primed ctypes or truncate positions at the DWORD rollover.
bass.BASS_ChannelGetPosition.argtypes = [ctypes.c_uint, ctypes.c_uint]
bass.BASS_ChannelGetPosition.restype = ctypes.c_uint64
# bass.h DWORD BASS_ChannelFlags(DWORD,DWORD,DWORD); 0xffffffff on failure.
bass.BASS_ChannelFlags.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
bass.BASS_ChannelFlags.restype = ctypes.c_uint
# bass.h int BASS_ErrorGetCode(void); keep the true empty-argument ABI explicit.
bass.BASS_ErrorGetCode.argtypes = []
bass.BASS_ErrorGetCode.restype = ctypes.c_int
bass.BASS_ChannelGetLevel.argtypes = [ctypes.c_uint]
bass.BASS_ChannelGetLevel.restype = ctypes.c_uint
bass.BASS_ChannelSetSync.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_ulonglong, SYNCPROC, ctypes.c_void_p]
bass.BASS_ChannelSetSync.restype = ctypes.c_uint
bass.BASS_ChannelRemoveSync.argtypes = [ctypes.c_uint, ctypes.c_uint]
bass.BASS_ChannelRemoveSync.restype = ctypes.c_int
bass.BASS_GetCPU.restype = ctypes.c_float
# bass.h BOOL BASS_GetInfo(BASS_INFO*); device selection is thread-local.
bass.BASS_GetInfo.argtypes = [ctypes.POINTER(BASS_INFO)]
bass.BASS_GetInfo.restype = ctypes.c_int
bass.BASS_ChannelIsActive.argtypes = [ctypes.c_uint]
bass.BASS_ChannelIsActive.restype = ctypes.c_int

REVERB_PRESETS = [
    ("Default",      2100, 171, 90,   2000),
    ("GM/GS Room 1",    1350,   1, 32,   5000),
    ("GM/GS Room 2",    1600,   6, 32,   3400),
    ("GM/GS Room 3",    1850,   6, 32,      0),
    ("GM/GS Hall 1",    2100, 174, 32,   3400),
    ("GM/GS Hall 2",    2000, 174, 32,      0),
    ("GM/GS Plate",     1900,  43, 32,      0),
    ("XG Hall 1",    1900, 127, 90,   5600),
    ("XG Hall 2",    2600, 442, 40,   4000),
    ("XG Room 1",     600, 253, 32,   5600),
    ("XG Room 2",    1300,  80, 32,   1600),
    ("XG Room 3",    1000, 741, 36,   1200),
    ("XG Stage 1",   2000, 253, 45,  10000),
    ("XG Stage 2",   1200, 253, 45,   7000),
    ("XG Plate",     2600,  95, 50,   5600),
    ("XG White Room",   1000, 174,  0,   4000),
    ("XG Tunnel",    5300, 300,  0,   3200),
    ("XG Canyon",    5300, 300,  0,   3200),
    ("XG Basement",   400,  48,  0,   1000),
]

# ?대쫫 -> dict 留ㅽ븨 (UI?먯꽌 ?ъ슜)
REVERB_PRESET_TABLE = {
    name: {"time_ms": t, "delay_0p1ms": d, "hi_hz": hi, "lo_hz": lo}
    for (name, t, d, hi, lo) in REVERB_PRESETS
}

