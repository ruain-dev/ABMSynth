"""Audio output discovery, ASIO/WASAPI bridges, session lifetime, telemetry.

These objects share one output-device lifecycle and are intentionally grouped;
stream-bound DSP and MIDI effect state remain in their own modules.
"""

from __future__ import annotations


# ---- asio_output.py ----
import ctypes
import queue
import os
import time
import threading
import math
import struct
from types import MappingProxyType
from contextvars import Context
from contextlib import contextmanager
from pathlib import Path
from event_log import log_text, log_entry
from i18n import tr

# Complete presentation domain for the output-buffer card. These are product
# applicability rules,not device limits. Local bass.md/BASS_CONFIG_BUFFER and
# BASS_ASIO_Start document separate ordinary-playback and decode/pull ownership.
OUTPUT_TIMING_SETTINGS = MappingProxyType({
    'buffer_ms': frozenset(('shared',)),
    'shared_update_ms': frozenset(('shared',)),
    'exclusive_block_ms': frozenset(('wasapi',)),
})


def output_timing_availability(selected):
    """Field admission for the selected output;never change stored values."""
    backend=selected[0] if isinstance(selected,tuple) else 'shared'
    return {field:backend in backends for field,backends in OUTPUT_TIMING_SETTINGS.items()}


def native_output_format(code, backend):
    """Local bassasio.h/basswasapi.h native formats, including valid bits."""
    formats = ({0: (32, 32, "Float"), 1: (8, 8, "PCM"), 2: (16, 16, "PCM"),
                3: (24, 24, "PCM"), 4: (32, 32, "PCM")} if backend == "wasapi" else
               {16: (16, 16, "PCM"), 17: (24, 24, "PCM"), 18: (32, 32, "PCM"),
                19: (32, 32, "Float"), 24: (32, 16, "PCM"), 25: (32, 18, "PCM"),
                26: (32, 20, "PCM"), 27: (32, 24, "PCM")})
    value = formats.get(code)
    return dict(output_bits=value[0], output_valid_bits=value[1], output_encoding=value[2]) if value else {}


# bassasio.h: BASS_ASIO_DEVICEINFO { const char *name; const char *driver; }.
class ASIODeviceInfo(ctypes.Structure):
    _fields_ = [("name", ctypes.c_void_p), ("driver", ctypes.c_void_p)]


def load_bassasio():
    dll = ctypes.WinDLL(str(Path(__file__).with_name("bassasio.dll")))
    for name, arguments, result in (
        ("BASS_ASIO_GetVersion", [], ctypes.c_uint32),
        ("BASS_ASIO_SetUnicode", [ctypes.c_int], ctypes.c_int),
        ("BASS_ASIO_GetDeviceInfo", [ctypes.c_uint32, ctypes.POINTER(ASIODeviceInfo)], ctypes.c_int),
        ("BASS_ASIO_ErrorGetCode", [], ctypes.c_uint32),
        ("BASS_ASIO_Init", [ctypes.c_int, ctypes.c_uint32], ctypes.c_int),
        ("BASS_ASIO_SetDevice", [ctypes.c_uint32], ctypes.c_int),
        ("BASS_ASIO_GetCPU", [], ctypes.c_float),
        ("BASS_ASIO_Free", [], ctypes.c_int),
        ("BASS_ASIO_ChannelEnableBASS", [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int], ctypes.c_int),
        ("BASS_ASIO_ChannelEnable", [ctypes.c_int,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_void_p],ctypes.c_int),
        ("BASS_ASIO_ChannelJoin", [ctypes.c_int,ctypes.c_uint32,ctypes.c_int],ctypes.c_int),
        ("BASS_ASIO_ChannelSetFormat", [ctypes.c_int,ctypes.c_uint32,ctypes.c_uint32],ctypes.c_int),
        ("BASS_ASIO_ChannelSetRate", [ctypes.c_int,ctypes.c_uint32,ctypes.c_double],ctypes.c_int),
        ("BASS_ASIO_Start", [ctypes.c_uint32, ctypes.c_uint32], ctypes.c_int),
        ("BASS_ASIO_Stop", [], ctypes.c_int),
        ("BASS_ASIO_ControlPanel", [], ctypes.c_int),
        # bassasio.h ASIONOTIFYPROC is native, DWORD notice + void* user.
        ("BASS_ASIO_SetNotify", [ctypes.c_void_p,ctypes.c_void_p], ctypes.c_int),
        ("BASS_ASIO_IsStarted", [], ctypes.c_int),
    ):
        func = getattr(dll, name)
        func.argtypes = arguments
        func.restype = result
    # bassasio.h BASSASIOVERSION=0x104. Reject incompatible major/minor API.
    if dll.BASS_ASIO_GetVersion() >> 16 != 0x104:
        raise RuntimeError("Unsupported BASSASIO API version")
    return dll


def devices(dll) -> list[tuple[str, int]]:
    dll.BASS_ASIO_SetUnicode(True)
    result = []
    index = 0
    info = ASIODeviceInfo()
    while dll.BASS_ASIO_GetDeviceInfo(index, ctypes.byref(info)):
        # BASS_ASIO_SetUnicode makes the returned name UTF-16 on Windows.
        name = ctypes.wstring_at(info.name) if info.name else f"ASIO {index}"
        if not name.startswith('ABMSynth observer: '):
            result.append((name, index))
        index += 1
    return result


# bassasio.h BASS_ASIO_THREAD=1: dedicated STA driver host, not DSP parallelism.
BASS_ASIO_THREAD = 1

_ASIO_OBSERVER_LOCK=threading.RLock()
_ASIO_OBSERVER_IDS={}

class AsioDriverObservation:
    """Unregistered COM adapter; BASSASIO retains all PCM/SRC work.

    Native time-info/completion observation and final-byte buffer isolation.
    No registry writes, sample algorithm changes or vendor/SDK redistribution.
    """
    def __init__(self,dll,index):
        import uuid
        import winreg
        dll.BASS_ASIO_SetUnicode(True)
        info=ASIODeviceInfo()
        if not dll.BASS_ASIO_GetDeviceInfo(index,ctypes.byref(info)):
            raise RuntimeError(tr('runtime.asio_observer_identity_failed'))
        name=ctypes.wstring_at(info.name);path=ctypes.wstring_at(info.driver)
        target=None;candidates=[]
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SOFTWARE\ASIO') as root:
            for n in range(winreg.QueryInfoKey(root)[0]):
                entry=winreg.EnumKey(root,n)
                try:
                    with winreg.OpenKey(root,entry) as item:
                        clsid=winreg.QueryValueEx(item,'CLSID')[0]
                        try:description=winreg.QueryValueEx(item,'Description')[0]
                        except OSError:description=entry
                        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,rf'CLSID\{clsid}\InprocServer32') as server:
                            registered=winreg.QueryValueEx(server,None)[0]
                        if os.path.normcase(os.path.abspath(registered))==os.path.normcase(os.path.abspath(path)):
                            candidates.append(clsid)
                            if name in (description,entry):target=clsid;break
                except OSError:continue
        if target is None and len(candidates)==1:target=candidates[0]
        if target is None:raise RuntimeError(tr('runtime.asio_observer_identity_failed'))
        library=Path(os.environ.get('ABMSYNTH_ASIO_OBSERVER_DLL',Path(__file__).with_name('abmsynth_asio_observer.dll')))
        self.native=ctypes.CDLL(str(library))
        self.native.asio_observer_configure.argtypes=[ctypes.c_void_p]
        self.native.asio_observer_configure.restype=ctypes.c_int
        self.native.asio_observer_snapshot.argtypes=[ctypes.POINTER(ctypes.c_uint64)]
        self.native.asio_observer_snapshot.restype=None
        target_guid=ctypes.create_string_buffer(uuid.UUID(target).bytes_le)
        proxy_guid=ctypes.create_string_buffer(uuid.UUID('b4708bc6-a02d-471c-9137-e03d75f19ba8').bytes_le)
        with _ASIO_OBSERVER_LOCK:
            if not self.native.asio_observer_configure(target_guid):
                raise RuntimeError(tr('runtime.asio_observer_busy'))
            key=(str(library.resolve()),target)
            if key not in _ASIO_OBSERVER_IDS:
                dll.BASS_ASIO_AddDevice.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p]
                dll.BASS_ASIO_AddDevice.restype=ctypes.c_uint
                proxy_index=int(dll.BASS_ASIO_AddDevice(proxy_guid,ctypes.c_wchar_p(str(library.resolve())),
                    ctypes.c_wchar_p('ABMSynth observer: '+name)))
                if proxy_index==0xffffffff:raise RuntimeError(tr('runtime.asio_observer_registration_failed'))
                _ASIO_OBSERVER_IDS[key]=proxy_index
            self.index=_ASIO_OBSERVER_IDS[key]
        self.original_index=int(index)

    def health(self):
        values=(ctypes.c_uint64*8)();self.native.asio_observer_snapshot(values)
        result=dict(output_device_missing_buffers=int(values[1]),
            output_device_dropout_incidents=int(values[2]),
            output_device_position_invalid=int(values[3]),
            output_device_overload_incidents=int(values[4]),
            output_device_position_valid=bool(values[5]),output_device_position_epoch=int(values[6]),
            output_device_buffer_samples=int(values[7]))
        if hasattr(self.native,'asio_observer_completion'):
            self.native.asio_observer_completion.argtypes=[ctypes.POINTER(ctypes.c_uint64)]
            self.native.asio_observer_completion.restype=None
            observed=(ctypes.c_uint64*4)();self.native.asio_observer_completion(observed)
            result.update(output_device_completion_late_blocks=int(observed[1]),
                output_device_completion_peak_ms=observed[3]/1000)
            if hasattr(self.native,'asio_observer_completion_budget'):
                self.native.asio_observer_completion_budget.argtypes=[]
                self.native.asio_observer_completion_budget.restype=ctypes.c_double
                result['output_device_completion_budget_ms']=self.native.asio_observer_completion_budget()
        if hasattr(self.native,'asio_observer_delivery'):
            self.native.asio_observer_delivery.argtypes=[ctypes.POINTER(ctypes.c_uint64),ctypes.POINTER(ctypes.c_double),ctypes.POINTER(ctypes.c_double)]
            self.native.asio_observer_delivery.restype=None
            count=ctypes.c_uint64();excess=ctypes.c_double();peak=ctypes.c_double()
            self.native.asio_observer_delivery(ctypes.byref(count),ctypes.byref(excess),ctypes.byref(peak))
            result.update(output_device_delivery_late_blocks=count.value,
                output_device_delivery_excess_ms=excess.value,output_device_delivery_peak_ms=peak.value)
        return result

def _prepare_asio_observer(dll,index):
    """Real DLL only; failures are visible startup failures, never healthy zeros."""
    if not isinstance(dll,ctypes.CDLL):return index
    observer=AsioDriverObservation(dll,index)
    dll._abmsynth_driver_observer=observer
    if not hasattr(dll,'_abmsynth_original_set_device'):
        dll._abmsynth_original_set_device=dll.BASS_ASIO_SetDevice
        def select(requested):
            active=getattr(dll,'_abmsynth_driver_observer',None)
            physical=active.index if active and int(requested)==active.original_index else requested
            return dll._abmsynth_original_set_device(physical)
        dll.BASS_ASIO_SetDevice=select
    return observer.index


def start(dll, device_index: int, decode_stream: int, *, output_probe=None) -> None:
    """bassasio.h: BASS decode stream -> joined stereo ASIO output 0/1."""
    # ASIO_Init's COM driver requests use its initialization thread. Keep
    # that host independent of the GUI message queue; callback, buffer and
    # sample policy are unchanged. Official BASS_ASIO_Init/Free lifetime.
    physical_index=_prepare_asio_observer(dll,device_index)
    if not dll.BASS_ASIO_Init(physical_index, BASS_ASIO_THREAD):
        raise RuntimeError(f"BASS_ASIO_Init: {dll.BASS_ASIO_ErrorGetCode()}")
    try:
        if output_probe is None:
            if not dll.BASS_ASIO_ChannelEnableBASS(False, 0, decode_stream, True):
                raise RuntimeError(f"BASS_ASIO_ChannelEnableBASS: {dll.BASS_ASIO_ErrorGetCode()}")
        else:
            output_probe.acquire("asio")
            # Additive own-native observation of accumulated supply lateness;
            # configure before ASIO callback admission, never a Python callback.
            if callable(getattr(output_probe,"enable_progress_observation",None)):
                output_probe.enable_progress_observation()
            if callable(getattr(output_probe,"observe_asio_processing",None)):
                output_probe.observe_asio_processing(dll)
            if callable(getattr(output_probe,"asio_notification_callback",None)):
                notice=output_probe.asio_notification_callback()
                if notice is not None and not dll.BASS_ASIO_SetNotify(notice,output_probe.pointer):
                    raise RuntimeError(f"BASS_ASIO_SetNotify: {dll.BASS_ASIO_ErrorGetCode()}")
            info=BASS_CHANNELINFO()
            from bass_bindings import BASS_SAMPLE_FLOAT
            if not output_probe.bass.BASS_ChannelGetInfo(decode_stream,ctypes.byref(info)) or info.chans != 2 or not info.flags&BASS_SAMPLE_FLOAT:
                raise RuntimeError(tr("playback_info.source_format_failed"))
            for operation,args in ((dll.BASS_ASIO_ChannelEnable,(False,0,output_probe.callback("asio"),output_probe.pointer)),
                (dll.BASS_ASIO_ChannelJoin,(False,1,0)),(dll.BASS_ASIO_ChannelSetFormat,(False,0,19)),
                (dll.BASS_ASIO_ChannelSetRate,(False,0,float(info.freq)))):
                if not operation(*args):raise RuntimeError(tr("playback_info.output_callback_failed",error=int(dll.BASS_ASIO_ErrorGetCode())))
        if not dll.BASS_ASIO_Start(0, 0):
            raise RuntimeError(f"BASS_ASIO_Start: {dll.BASS_ASIO_ErrorGetCode()}")
    except Exception as error:
        if not dll.BASS_ASIO_Free():
            error.backend_cleanup_pending='asio'
            error.add_note(tr('runtime.startup_cleanup_failed',error=f'BASS_ASIO_Free: {dll.BASS_ASIO_ErrorGetCode()}'))
        elif output_probe is not None:output_probe.release("asio")
        raise


def stop(dll) -> None:
    if callable(getattr(dll,"BASS_ASIO_SetNotify",None)) and not dll.BASS_ASIO_SetNotify(None,None):
        raise RuntimeError(f'BASS_ASIO_SetNotify: {dll.BASS_ASIO_ErrorGetCode()}')
    if not dll.BASS_ASIO_Stop():
        code=int(dll.BASS_ASIO_ErrorGetCode())
        # Installed bass.h BASS_ERROR_START=9: a prior successful Stop can
        # precede a failed Free. Retry must still release that stopped owner.
        if code!=9:raise RuntimeError(f'BASS_ASIO_Stop: {code}')
    if not dll.BASS_ASIO_Free():
        raise RuntimeError(f'BASS_ASIO_Free: {dll.BASS_ASIO_ErrorGetCode()}')


# basswasapi24/c/basswasapi.h: this optional backend uses the same BASSMIDI
# decoding-stream lifetime as ASIO, but owns a separate exclusive endpoint.
BASS_WASAPI_EXCLUSIVE = 1
BASS_WASAPI_EVENT = 16
WASAPI_NOTIFY_FAIL = 0x100
WASAPI_NOTIFY_ENABLED = 0
WASAPI_NOTIFY_DISABLED = 1
WASAPI_NOTIFY_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)


class WASAPIDeviceInfo(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p), ("id", ctypes.c_char_p),
        ("type", ctypes.c_uint32), ("flags", ctypes.c_uint32),
        ("minperiod", ctypes.c_float), ("defperiod", ctypes.c_float),
        ("mixfreq", ctypes.c_uint32), ("mixchans", ctypes.c_uint32),
    ]


class WASAPIInfo(ctypes.Structure):
    # Installed basswasapi24/c/basswasapi.h: volumes are FLOAT, even though
    # the online structure listing currently says DWORD. Local ABI is primary.
    _fields_ = [(name, ctypes.c_uint32) for name in
                ("initflags", "freq", "chans", "format", "buflen")] + [
                (name, ctypes.c_float) for name in ("volmax", "volmin", "volstep")]


def load_basswasapi():
    dll = ctypes.WinDLL(str(Path(__file__).with_name("basswasapi.dll")))
    for name, arguments, result in (
        ("BASS_WASAPI_GetVersion", [], ctypes.c_uint32),
        ("BASS_WASAPI_GetDeviceInfo", [ctypes.c_uint32, ctypes.POINTER(WASAPIDeviceInfo)], ctypes.c_int),
        ("BASS_WASAPI_CheckFormat", [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32], ctypes.c_uint32),
        ("BASS_WASAPI_Init", [ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                              ctypes.c_float, ctypes.c_float, ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
        ("BASS_WASAPI_Start", [], ctypes.c_int),
        ("BASS_WASAPI_SetDevice", [ctypes.c_uint32], ctypes.c_int),
        ("BASS_WASAPI_GetCPU", [], ctypes.c_float),
        # basswasapi.h DWORD GetData(void*,DWORD). AVAILABLE=0 is a
        # read-only queued-byte query; no retained buffer/callback lifetime.
        ("BASS_WASAPI_GetData", [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_uint32),
        ("BASS_WASAPI_IsStarted", [], ctypes.c_int),
        ("BASS_WASAPI_Stop", [ctypes.c_int], ctypes.c_int),
        ("BASS_WASAPI_Free", [], ctypes.c_int),
        ("BASS_WASAPI_SetNotify", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
    ):
        function = getattr(dll, name)
        function.argtypes = arguments
        function.restype = result
    # basswasapi.h documents the ABI; the high word is BASSWASAPI 2.4.
    if dll.BASS_WASAPI_GetVersion() >> 16 != 0x204:
        raise RuntimeError("Unsupported BASSWASAPI API version")
    return dll


def wasapi_devices(dll) -> list[tuple[str, int, str]]:
    """List enabled physical output endpoints, excluding input/loopback."""
    result = []
    info = WASAPIDeviceInfo()
    index = 0
    while dll.BASS_WASAPI_GetDeviceInfo(index, ctypes.byref(info)):
        if info.flags & BASS_DEVICE_ENABLED and not info.flags & (0x10 | 0x8):
            if info.name:
                try:
                    name = info.name.decode("utf-8")
                except UnicodeDecodeError:
                    # A test or another BASS owner may have enumerated before
                    # BASS_CONFIG_UNICODE was enabled; never drop the endpoint.
                    name = info.name.decode("mbcs", errors="replace")
            else:
                name = f"WASAPI {index}"
            endpoint_id = info.id.decode("ascii", errors="replace") if info.id else ""
            result.append((name, index, endpoint_id))
        index += 1
    return result


def start_wasapi(dll, device_index: int, decode_stream: int, sample_rate: int, *, block_ms=5, output_probe=None) -> None:
    """basswasapi.h: pull a float BASS decoding channel into exclusive output."""
    supported = dll.BASS_WASAPI_CheckFormat(device_index, sample_rate, 2, BASS_WASAPI_EXCLUSIVE)
    if supported == 0xFFFFFFFF:
        # Official CheckFormat distinguishes NOTAVAIL37 (exclusive mode),
        # FORMAT6 (rate/format) and DRIVER3. Never relabel every rejection as
        # unsupported sample rate; leave the exact native failure available.
        raise RuntimeError(tr("runtime.output_format_check_failed",rate=sample_rate,error=int(bass.BASS_ErrorGetCode())))
    # basswasapi.h: event-driven exclusive mode uses two buffers of this
    # length. The ordinary BASS_CONFIG_BUFFER preference is not a WASAPI
    # event period; the saved request is a minimum, not the actual block.
    # Honor endpoints that report a longer minimum update period.
    if type(block_ms) is not int or not 1 <= block_ms <= 100:
        raise ValueError("exclusive_block_ms")
    info = WASAPIDeviceInfo()
    if not dll.BASS_WASAPI_GetDeviceInfo(device_index, ctypes.byref(info)):
        raise RuntimeError(f"BASS_WASAPI_GetDeviceInfo: {bass.BASS_ErrorGetCode()}")
    buffer_seconds = max(block_ms / 1000.0, float(info.minperiod))
    if output_probe is not None:output_probe.acquire("wasapi")
    if not dll.BASS_WASAPI_Init(
        device_index, sample_rate, 2, BASS_WASAPI_EXCLUSIVE | BASS_WASAPI_EVENT,
        buffer_seconds, 0.0, output_probe.callback("wasapi") if output_probe is not None else ctypes.c_void_p(-1),
        ctypes.c_void_p(output_probe.pointer if output_probe is not None else decode_stream),
    ):
        if output_probe is not None:output_probe.release("wasapi")
        raise RuntimeError(f"BASS_WASAPI_Init: {bass.BASS_ErrorGetCode()}")
    try:
        if not dll.BASS_WASAPI_Start():
            raise RuntimeError(f"BASS_WASAPI_Start: {bass.BASS_ErrorGetCode()}")
    except Exception as error:
        if not dll.BASS_WASAPI_Free():
            error.backend_cleanup_pending='wasapi'
            error.add_note(tr('runtime.startup_cleanup_failed',error=f'BASS_WASAPI_Free: {bass.BASS_ErrorGetCode()}'))
        elif output_probe is not None:output_probe.release("wasapi")
        raise


def stop_wasapi(dll) -> None:
    if not dll.BASS_WASAPI_Stop(True):
        raise RuntimeError(f'BASS_WASAPI_Stop: {bass.BASS_ErrorGetCode()}')
    if not dll.BASS_WASAPI_Free():
        raise RuntimeError(f'BASS_WASAPI_Free: {bass.BASS_ErrorGetCode()}')

# ---- audio_devices.py ----
import ctypes
from dataclasses import dataclass

from bass_bindings import (
    BASS_CONFIG_UNICODE, BASS_DEVICE_DEFAULT, BASS_DEVICE_ENABLED, BASS_DEVICEINFO, BASS_CHANNELINFO, bass,
)


@dataclass(frozen=True)
class OutputDevice:
    name: str | None
    backend_id: int | tuple[str, int] | tuple[str, int, str]
    asio: bool = False
    wasapi: bool = False


@dataclass(frozen=True)
class DeviceCatalog:
    devices: tuple[OutputDevice, ...]
    asio_dll: object | None
    asio_error: OSError | RuntimeError | None
    wasapi_dll: object | None = None
    wasapi_error: OSError | RuntimeError | None = None


class AudioDeviceCatalog:
    """One responsibility: discover output endpoints and their backend IDs."""

    def __init__(self, bass, asio_output):
        self.bass = bass
        self.asio_output = asio_output

    def enumerate(self) -> DeviceCatalog:
        # bass.md BASS_CONFIG_UNICODE: Windows device names are ANSI by
        # default; set UTF-8 before the first enumeration or BASS_Init.
        self.bass.BASS_SetConfig(BASS_CONFIG_UNICODE, 1)
        encoding = "utf-8" if self.bass.BASS_GetConfig(BASS_CONFIG_UNICODE) else "mbcs"
        devices = []
        index = 1
        info = BASS_DEVICEINFO()
        while self.bass.BASS_GetDeviceInfo(index, ctypes.byref(info)):
            if int(info.flags) & BASS_DEVICE_ENABLED:
                name = info.name.decode(encoding) if info.name else None
                # bass.md: both the virtual Default endpoint and the physical
                # endpoint can carry DEFAULT; only the first maps to -1.
                backend = -1 if index == 1 and int(info.flags) & BASS_DEVICE_DEFAULT else index
                devices.append(OutputDevice(name, backend))
            index += 1
        try:
            dll = self.asio_output.load_bassasio()
            devices.extend(OutputDevice(name, ("asio", device_id), True)
                           for name, device_id in self.asio_output.devices(dll))
            asio_error = None
        except (OSError, RuntimeError) as exc:
            dll, asio_error = None, exc
        try:
            wasapi_dll = self.asio_output.load_basswasapi()
            devices.extend(OutputDevice(name, ("wasapi", device_id, endpoint_id), wasapi=True)
                           for name, device_id, endpoint_id in self.asio_output.wasapi_devices(wasapi_dll))
            wasapi_error = None
        except (AttributeError, OSError, RuntimeError) as exc:
            wasapi_dll, wasapi_error = None, exc
        return DeviceCatalog(tuple(devices), dll, asio_error, wasapi_dll, wasapi_error)

# ---- audio_session.py ----
from dataclasses import dataclass

from bass_bindings import (
    BASS_ATTRIB_MIDI_CPU, BASS_ATTRIB_MIDI_SRC, BASS_ATTRIB_MIDI_VOICES, BASS_CONFIG_BUFFER, BASS_CONFIG_FLOATDSP,
    BASS_CONFIG_MIDI_COMPACT, BASS_CONFIG_MIDI_SAMPLEREAD,
    BASS_CONFIG_MIDI_VOICES, BASS_CONFIG_UPDATEPERIOD, BASS_CONFIG_UPDATETHREADS, BASS_DEVICEINFO,
)


UPDATE_THREAD_COUNT = 16


@dataclass
class AudioStartError(Exception):
    # Exceptions need writable traceback/cause/notes (Python BaseException),
    # unlike immutable device/observation data transferred between owners.
    stage: str
    code: int | str


def _backend_processing_load(bass, session=None, asio_dll=None):
    """One native utilization reader for visible and diagnostic consumers."""
    cpu = None
    if session is not None and session.asio_device is not None:
        dll = asio_dll if asio_dll is not None else session._asio_dll
        if session.asio_started and dll is not None and dll.BASS_ASIO_SetDevice(session.asio_device):
            cpu = float(dll.BASS_ASIO_GetCPU())
    elif session is not None and session.wasapi_device is not None:
        dll = session._wasapi_dll
        if session.wasapi_started and dll is not None and dll.BASS_WASAPI_SetDevice(session.wasapi_device):
            cpu = float(dll.BASS_WASAPI_GetCPU())
    else:
        cpu = float(bass.BASS_GetCPU())
    return cpu if cpu is not None and math.isfinite(cpu) and cpu >= 0 else None


class AudioCloseError(RuntimeError):
    def __init__(self, code):
        self.code=int(code)
        super().__init__(tr("runtime.output_close_failed",error=self.code))


class BASSUpdateRetirement:
    """Own the last-device updater barrier through Free and config restoration.

    bass.h: DEVICE_INIT=4, ERROR_DEVICE=23, BOOL SetDevice/SetConfig/Free;
    bass.md: device selection is thread-local, UPDATETHREADS is global and
    zero disables automatic updates. GetDeviceInfo enumeration includes0.
    The installed Windows updater-join behavior is separately qualified by
    actual shutdown-cycle probes; docs alone do not prove an internal join.
    This is a teardown barrier, never a playback buffer/update policy change.
    """
    _guard=threading.RLock()
    _owners={}

    def __init__(self,bass):
        self.bass=bass
        # Multiple ctypes wrappers of one loaded BASS share the same config.
        # Test adapters still must supply the complete device/config ABI.
        self.domain=bass._handle if isinstance(bass,ctypes.CDLL) else bass
        self.device=None
        self.saved_updates=None
        self.suspended=False
        self.free_confirmed=False

    @property
    def pending(self):return self.saved_updates is not None

    def _failure(self,stage,code=None):
        error=AudioCloseError(self.bass.BASS_ErrorGetCode() if code is None else code)
        error.add_note(stage)
        raise error

    @contextmanager
    def initialization(self):
        with self._guard:
            if self._owners.get(self.domain) is not None or self.device is not None or self.pending:
                self._failure('BASS_CONFIG_UPDATETHREADS / BASS_Init',46)
            yield

    def adopt_initialized(self):
        # BASS_Init selected the actual device, including the Default proxy.
        # Store it before stream creation can fail; never retain requested-1.
        device=self.bass.BASS_GetDevice()
        if type(device) is not int or device==0xffffffff:
            self._failure('BASS_GetDevice / BASS_Init')
        self.device=device;self.free_confirmed=False

    def select_owned(self):
        if self.device is None:self._failure('BASS_SetDevice / BASS_DEVICE_INIT',8)
        if not self.bass.BASS_SetDevice(self.device):self._failure('BASS_SetDevice')

    def stop(self):
        self.select_owned()
        if not self.bass.BASS_Stop():self._failure('BASS_Stop')

    def suspend(self):
        with self._guard:
            incumbent=self._owners.get(self.domain)
            if incumbent is not None and incumbent is not self:self._failure('BASS_CONFIG_UPDATETHREADS',46)
            if self.suspended:return
            initialized=[];index=0
            while True:
                info=BASS_DEVICEINFO()
                if not self.bass.BASS_GetDeviceInfo(index,ctypes.byref(info)):
                    if self.bass.BASS_ErrorGetCode()!=23:self._failure('BASS_GetDeviceInfo')
                    break
                if info.flags&4:initialized.append(index)
                index+=1
            # Include disabled initialized entries and no-sound0. No fallback
            # guesses that a missing/fake enumeration means exclusive ownership.
            if initialized!=[self.device]:self._failure(f'BASS_DEVICE_INIT: {initialized} / {self.device}',46)
            if self.saved_updates is None:
                value=self.bass.BASS_GetConfig(BASS_CONFIG_UPDATETHREADS)
                if type(value) is not int or value==0xffffffff:self._failure('BASS_GetConfig(BASS_CONFIG_UPDATETHREADS)')
                self.saved_updates=value
            self._owners[self.domain]=self
            if not self.bass.BASS_SetConfig(BASS_CONFIG_UPDATETHREADS,0):self._failure('BASS_SetConfig(BASS_CONFIG_UPDATETHREADS, 0)')
            actual=self.bass.BASS_GetConfig(BASS_CONFIG_UPDATETHREADS)
            if type(actual) is not int or actual!=0:self._failure('BASS_GetConfig(BASS_CONFIG_UPDATETHREADS) / 0',20)
            self.suspended=True

    def free(self):
        with self._guard:
            if self.free_confirmed:return
            if not self.suspended:self._failure('BASS_CONFIG_UPDATETHREADS / BASS_Free',46)
            self.select_owned()
            if not self.bass.BASS_Free():self._failure('BASS_Free')
            self.free_confirmed=True;self.device=None

    def restore(self):
        with self._guard:
            if not self.pending:return
            if not self.free_confirmed:self._failure('BASS_Free / BASS_CONFIG_UPDATETHREADS',46)
            value=self.saved_updates
            if not self.bass.BASS_SetConfig(BASS_CONFIG_UPDATETHREADS,value):self._failure('BASS_SetConfig(BASS_CONFIG_UPDATETHREADS)')
            actual=self.bass.BASS_GetConfig(BASS_CONFIG_UPDATETHREADS)
            if type(actual) is not int or actual!=value:self._failure('BASS_GetConfig(BASS_CONFIG_UPDATETHREADS)',20)
            self.saved_updates=None;self.suspended=False;self.free_confirmed=False
            if self._owners.get(self.domain) is self:self._owners.pop(self.domain)


class NativeMulticoreSynthesis:
    """Own channel-partitioned MIDI decoders before the original output DSP.

    The main HMIDI retains RAW parsing, controller policy and the post-mix FX
    chain. Native WINAPI facades fan effective events/fonts/attributes out;
    a native early DSP renders the independent owners without Python callbacks.
    The context borrows BASS handles until workers and deferred syncs retire.
    """
    # Production uses one decoder per base channel and16 render threads.
    # Other topologies require a separately built analysis DLL; the active
    # adapter rejects a DLL whose published layout is not exactly16/16.
    _source_owner_count=16
    _facade_guard=threading.RLock()
    _facades={}
    _functions=(
        ('event','midi','BASS_MIDI_StreamEvent'),
        ('events','midi','BASS_MIDI_StreamEvents'),
        ('filter','midi','BASS_MIDI_StreamSetFilter'),
        ('fonts','midi','BASS_MIDI_StreamSetFonts'),
        ('channel','midi','BASS_MIDI_StreamGetChannel'),
        ('set_attribute','bass','BASS_ChannelSetAttribute'),
        ('get_attribute','bass','BASS_ChannelGetAttribute'),
        ('set_sync','bass','BASS_ChannelSetSync'),
        ('remove_sync','bass','BASS_ChannelRemoveSync'),
        ('get_data','bass','BASS_ChannelGetData'),
        ('set_dsp','bass','BASS_ChannelSetDSP'),
        ('remove_dsp','bass','BASS_ChannelRemoveDSP'),
        ('error','bass','BASS_ErrorGetCode'),
        ('set_device','bass','BASS_SetDevice'),
        ('get_device','bass','BASS_GetDevice'),
        ('lock','bass','BASS_ChannelLock'),
        ('get_position','bass','BASS_ChannelGetPosition'),
        ('stream_create','bass','BASS_StreamCreate'),
        ('stream_free','bass','BASS_StreamFree'),
        ('channel_flags','bass','BASS_ChannelFlags'),
    )
    class Functions(ctypes.Structure):
        _fields_=[(name,ctypes.c_void_p) for name in (
            'event','events','filter','fonts','channel','set_attribute','get_attribute',
            'set_sync','remove_sync','get_data','set_dsp','remove_dsp','error',
            'set_device','get_device','lock','get_position','stream_create','stream_free','channel_flags')]

    def __init__(self,bass,bassmidi,main,preferences):
        self.bass,self.bassmidi,self.main=bass,bassmidi,int(main)
        self.pointer=None;self.streams=[];self.library=None
        self.mode=preferences.synthesis_mode
        self.carrier_stream=0;self.carrier_font_pointer=None;self.carrier_font=0
        # Scalar receipts use the current DLL's verified unknown-event filter
        # dispatch. This is not a promise for an unreviewed future BASSMIDI.
        version=int(bassmidi.BASS_MIDI_GetVersion())
        if version!=0x02041000:
            raise RuntimeError(tr('runtime.multicore_unverified_library',version=f'0x{version:08x}'))
        path=Path(os.environ.get('ABMSYNTH_OBSERVATION_DLL',Path(__file__).with_name('abmsynth_dsp.dll')))
        library=ctypes.CDLL(str(path))
        try:
            library.multicore_synth_version.argtypes=[]
            library.multicore_synth_version.restype=ctypes.c_uint
            if library.multicore_synth_version()!=2:raise ValueError('multicore ABI')
            library.multicore_synth_create.argtypes=[ctypes.c_uint,ctypes.POINTER(ctypes.c_uint),ctypes.c_uint,
                ctypes.c_uint,ctypes.c_uint,ctypes.POINTER(self.Functions)]
            library.multicore_synth_create.restype=ctypes.c_void_p
            library.multicore_synth_destroy.argtypes=[ctypes.c_void_p]
            library.multicore_synth_destroy.restype=ctypes.c_int
            library.multicore_synth_health.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint)]
            library.multicore_synth_health.restype=ctypes.c_int
            library.multicore_synth_layout.argtypes=[]
            library.multicore_synth_layout.restype=ctypes.c_uint
            library.multicore_synth_policy.argtypes=[]
            library.multicore_synth_policy.restype=ctypes.c_uint
            layout=int(library.multicore_synth_layout())
            if (layout&0xffff)!=16 or (layout>>16)!=16 or library.multicore_synth_policy()!=1:
                raise ValueError('multicore channel layout/policy')
        except (AttributeError,ValueError) as error:
            raise RuntimeError(tr('runtime.multicore_unavailable')) from error
        self.library=library
        # Additive diagnostic ABI. Older images remain valid for the explicit
        # compatible path; the candidate exposes atomic dispatch counters.
        self._dispatch_statistics=getattr(library,'multicore_synth_dispatch_statistics',None)
        if self._dispatch_statistics is not None:
            self._dispatch_statistics.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64)]
            self._dispatch_statistics.restype=ctypes.c_int
        # A process keeps the genuine function addresses even after facade
        # installation. Native actors subsequently borrow real C function
        # pointers, never Python callbacks or closures.
        domain=(bass._handle,bassmidi._handle)
        with self._facade_guard:
            previous=self._facades.get(domain)
            if previous is None:
                originals={name:getattr(bassmidi if surface=='midi' else bass,symbol)
                           for name,surface,symbol in self._functions}
                self._facades[domain]=(library,originals,{})
            else:
                installed,originals,_=previous
                if installed._handle!=library._handle:raise RuntimeError(tr('runtime.multicore_retained'))
            self.originals=originals
        from bass_bindings import BASS_CHANNELINFO,BASS_SAMPLE_FLOAT,BASS_STREAM_DECODE,BASS_MIDI_ASYNC
        information=BASS_CHANNELINFO()
        if not bass.BASS_ChannelGetInfo(self.main,ctypes.byref(information)):
            raise RuntimeError(tr('runtime.multicore_prepare_failed',error=originals['error']()))
        channels=ctypes.c_float()
        from bass_bindings import BASS_ATTRIB_MIDI_CHANS,BASS_ATTRIB_MIDI_SRC,BASS_ATTRIB_MIDI_CPU,BASS_ATTRIB_MIDI_VOICES
        if not originals['get_attribute'](self.main,BASS_ATTRIB_MIDI_CHANS,ctypes.byref(channels)):
            raise RuntimeError(tr('runtime.multicore_prepare_failed',error=originals['error']()))
        try:
            for _ in range(self._source_owner_count):
                stream=int(bassmidi.BASS_MIDI_StreamCreate(int(channels.value),
                    (information.flags & ~BASS_MIDI_ASYNC)|BASS_SAMPLE_FLOAT|BASS_STREAM_DECODE,information.freq))
                if not stream:raise RuntimeError(tr('runtime.multicore_prepare_failed',error=originals['error']()))
                self.streams.append(stream)
                for attr,value in ((BASS_ATTRIB_MIDI_SRC,preferences.interpolation),
                    (BASS_ATTRIB_MIDI_CPU,preferences.midi_cpu_limit),(BASS_ATTRIB_MIDI_VOICES,preferences.voices)):
                    if not originals['set_attribute'](stream,attr,value):
                        raise RuntimeError(tr('runtime.multicore_prepare_failed',error=originals['error']()))
            functions=self.Functions(*(ctypes.cast(originals[name],ctypes.c_void_p).value
                                       for name,_,_ in self._functions))
            handles=(ctypes.c_uint*len(self.streams))(*self.streams)
            # Preferred bounded allocation, not a sample-rate/decoder limit.
            self.pointer=library.multicore_synth_create(self.main,handles,len(handles),65536,information.freq,ctypes.byref(functions))
            if not self.pointer:raise RuntimeError(tr('runtime.multicore_prepare_failed',error=originals['error']()))
            self._prepare_common_effects(information.freq,information.flags,preferences)
            with self._facade_guard:
                _installed,_originals,wrappers=self._facades[domain]
                for name,surface,symbol in self._functions:
                    getter=getattr(library,'multicore_synth_'+name+'_proc',None)
                    if getter is None:continue
                    getter.argtypes=[];getter.restype=ctypes.c_void_p
                    address=getter()
                    if not address:raise RuntimeError(tr('runtime.multicore_unavailable'))
                    original=originals[name]
                    wrapper=ctypes.WINFUNCTYPE(original.restype,*(original.argtypes or []))(address)
                    wrappers[name]=wrapper
                    setattr(bassmidi if surface=='midi' else bass,symbol,wrapper)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _carrier_font_data():
        """Minimal SF2 2.1, one silent loop. No external asset dependency.

        RIFF/phdr/pbag/pgen/inst/ibag/igen/shdr use the same verified SF2 layout
        as the existing capability audit. It never becomes a user preset.
        """
        def chunk(name,data):return name+struct.pack('<I',len(data))+data+(b'\0' if len(data)%2 else b'')
        phdr=struct.pack('<20sHHHIII20sHHHIII',b'Carrier',0,0,0,0,0,0,b'EOP',0,0,1,0,0,0)
        pbag=struct.pack('<HHHH',0,0,1,0);pgen=struct.pack('<HHHH',41,0,0,0)
        inst=struct.pack('<20sH20sH',b'Carrier',0,b'EOI',1)
        ibag=struct.pack('<HHHH',0,0,2,0);igen=struct.pack('<HHHHHH',54,1,53,0,0,0)
        shdr=struct.pack('<20sIIIIIBbHH20sIIIIIBbHH',b'Zero',0,64,0,64,48000,60,0,0,1,
                         b'EOS',64,64,64,64,48000,0,0,0,1)
        info=b'INFO'+chunk(b'ifil',struct.pack('<HH',2,1))+chunk(b'isng',b'EMU8000\0')+chunk(b'INAM',b'ABMSynth private FX carrier\0')
        pdta=b'pdta'+b''.join(chunk(key,value) for key,value in
            ((b'phdr',phdr),(b'pbag',pbag),(b'pmod',bytes(10)),(b'pgen',pgen),
             (b'inst',inst),(b'ibag',ibag),(b'imod',bytes(10)),(b'igen',igen),(b'shdr',shdr)))
        return chunk(b'RIFF',b'sfbk'+chunk(b'LIST',info)+chunk(b'LIST',b'sdta'+chunk(b'smpl',bytes((64+46)*2)))+chunk(b'LIST',pdta))

    def _prepare_common_effects(self,rate,source_flags,preferences):
        from bass_bindings import BASS_MIDI_FONT,BASS_SAMPLE_FLOAT,BASS_STREAM_DECODE,BASS_ATTRIB_MIDI_CPU,BASS_ATTRIB_MIDI_SRC
        library=self.library
        try:
            library.multicore_synth_common_version.argtypes=[];library.multicore_synth_common_version.restype=ctypes.c_uint
            if library.multicore_synth_common_version()!=2:raise ValueError('common FX ABI')
            library.multicore_carrier_font_create.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint),ctypes.POINTER(ctypes.c_uint)]
            library.multicore_carrier_font_create.restype=ctypes.c_void_p
            library.multicore_carrier_font_destroy.argtypes=[ctypes.c_void_p];library.multicore_carrier_font_destroy.restype=ctypes.c_int
            library.multicore_carrier_font_reads.argtypes=[ctypes.c_void_p];library.multicore_carrier_font_reads.restype=ctypes.c_uint64
            library.multicore_synth_common_effects.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_uint];library.multicore_synth_common_effects.restype=ctypes.c_int
        except (AttributeError,ValueError) as error:raise RuntimeError(tr('runtime.multicore_unavailable')) from error
        # bassmidi.h: HSOUNDFONT WINAPI(const BASS_FILEPROCS*,void*,DWORD).
        # Native callback roots own bytes until successful FontFree; preload
        # is control-side and no Python callback/file path enters audio.
        initialize=self.bassmidi.BASS_MIDI_FontInitUser
        initialize.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint];initialize.restype=ctypes.c_uint
        payload=ctypes.create_string_buffer(self._carrier_font_data());handle=ctypes.c_uint();loaded=ctypes.c_uint()
        self.carrier_font_pointer=library.multicore_carrier_font_create(payload,len(payload)-1,
            ctypes.cast(initialize,ctypes.c_void_p),ctypes.cast(self.bassmidi.BASS_MIDI_FontLoad,ctypes.c_void_p),
            ctypes.cast(self.bassmidi.BASS_MIDI_FontFree,ctypes.c_void_p),ctypes.byref(handle),ctypes.byref(loaded))
        self.carrier_font=int(handle.value)
        if not self.carrier_font_pointer or not loaded.value:raise RuntimeError(tr('runtime.multicore_prepare_failed',error=self.originals['error']()))
        # bassmidi.h: NOSYSRESET=0x800 and NOFX=0x2000 must preserve the
        # source's native reset/FX policy. ASYNC belongs only to the controller.
        carrier_flags=BASS_SAMPLE_FLOAT|BASS_STREAM_DECODE|(source_flags&(0x800|0x2000))
        self.carrier_stream=int(self.bassmidi.BASS_MIDI_StreamCreate(16,carrier_flags,rate))
        if not self.carrier_stream:raise RuntimeError(tr('runtime.multicore_prepare_failed',error=self.originals['error']()))
        mapping=BASS_MIDI_FONT(self.carrier_font,-1,0)
        if not self.originals['fonts'](self.carrier_stream,ctypes.byref(mapping),1):raise RuntimeError(tr('runtime.multicore_prepare_failed',error=self.originals['error']()))
        for attr,value in ((BASS_ATTRIB_MIDI_CPU,101),(BASS_ATTRIB_MIDI_SRC,preferences.interpolation)):
            if not self.originals['set_attribute'](self.carrier_stream,attr,value):raise RuntimeError(tr('runtime.multicore_prepare_failed',error=self.originals['error']()))
        # High-performance synthesis shares one native carrier for the three
        # effect-send buses. Mask7 enables all three common buses.
        if not library.multicore_synth_common_effects(self.pointer,self.carrier_stream,7):raise RuntimeError(tr('runtime.multicore_prepare_failed',error=self.originals['error']()))

    def health(self):
        if not self.pointer:return {}
        values=(ctypes.c_uint*3)()
        if not self.library.multicore_synth_health(self.pointer,values):
            raise RuntimeError(tr('runtime.multicore_unavailable'))
        if values[0]:
            raise RuntimeError(tr('runtime.multicore_prepare_failed',error=values[1]))
        result=dict(synthesis_mode=self.mode,synthesis_workers=int(values[2]))
        if self._dispatch_statistics is not None:
            dispatch=(ctypes.c_uint64*5)()
            if not self._dispatch_statistics(self.pointer,dispatch):
                raise RuntimeError(tr('runtime.multicore_unavailable'))
            result.update(synthesis_serial_spans=int(dispatch[0]),synthesis_parallel_spans=int(dispatch[1]),
                          synthesis_serial_frames=int(dispatch[2]),synthesis_parallel_frames=int(dispatch[3]),
                          synthesis_parallel_minimum_frames=int(dispatch[4]))
        return result

    def close(self):
        if self.pointer:
            if not self.library.multicore_synth_destroy(self.pointer):
                raise RuntimeError(tr('runtime.multicore_retained'))
            self.pointer=None
        if self.carrier_stream:
            if not self.bass.BASS_StreamFree(self.carrier_stream):raise RuntimeError(tr('runtime.multicore_retained'))
            self.carrier_stream=0
        if self.carrier_font_pointer:
            if not self.library.multicore_carrier_font_destroy(self.carrier_font_pointer):raise RuntimeError(tr('runtime.multicore_retained'))
            self.carrier_font_pointer=None;self.carrier_font=0
        while self.streams:
            if not self.bass.BASS_StreamFree(self.streams[-1]):
                raise RuntimeError(tr('runtime.multicore_prepare_failed',error=self.originals['error']()))
            self.streams.pop()


class AudioSession:
    """Own physical/ASIO/WASAPI output and the BASS stream lifetime."""

    def __init__(self, bass, bassmidi, engine, asio_output):
        self.bass = bass
        self.bassmidi = bassmidi
        self.engine = engine
        self.asio_output = asio_output
        self.stream = 0
        self.telemetry = None
        self._asio_dll = None
        self._asio_cleanup_pending = False
        self._asio_notice_seen=(0,0)
        self._asio_recovery_needed=False
        self._asio_retry_after=0.
        self._asio_failure_reported=False
        self.asio_device = None
        self.asio_started = False
        self.wasapi_device = None
        self.wasapi_endpoint_id = None
        self.wasapi_started = False
        self._wasapi_dll = None
        self._wasapi_cleanup_pending = False
        self._wasapi_preferences = None
        self._wasapi_notify = None
        self._wasapi_events = queue.SimpleQueue()
        self._wasapi_retry_after = 0.0
        self._wasapi_recovery_needed = False
        self._wasapi_failure_reported = False
        self.initialized = False
        self.update_retirement=BASSUpdateRetirement(bass)
        self._quiesced = True
        self._shared_start_pending = False
        self._device_format_key = None
        self._device_format_cache = {}
        self._device_format_due = 0.
        self.output_probe = None
        self.multicore_synthesis = None
        self._output_health_epoch = 0

    def _prepare_output_probe(self):
        if self.output_probe is not None:return
        # Fake adapters and pull-only export do not own a physical callback.
        if self.asio_device is None and self.wasapi_device is None:return
        sensor=getattr(getattr(self.engine,"runtime",None),"playback_timing",None)
        if sensor is None:return
        probe=NativeOutputProbe(self.bass,self.stream,native_dll=os.environ.get("ABMSYNTH_OBSERVATION_DLL"))
        try:
            source=BASS_CHANNELINFO()
            if not self.bass.BASS_ChannelGetInfo(self.stream,ctypes.byref(source)) or source.chans != 2:
                raise AudioStartError("output_format",int(self.bass.BASS_ErrorGetCode()))
            probe.enable_observation(int(source.freq))
        except Exception:
            probe.close();raise
        self.output_probe=probe
        self._output_health_epoch+=1

    def output_health(self):
        status=dict(self.output_probe.health(),output_health_epoch=self._output_health_epoch) if self.output_probe is not None else {}
        observer=getattr(self._asio_dll,'_abmsynth_driver_observer',None) if self.asio_started else None
        if observer is not None:status.update(observer.health())
        if self.multicore_synthesis is not None:
            status.update(self.multicore_synthesis.health())
        return status

    def open(self, selected, preferences, window_handle: int, stream_flags: int, *, start_shared=True):
        """Create the source stream and its stream-scoped DSP/FX once."""
        if self.initialized or self.update_retirement.pending:
            raise AudioStartError('bass_init',46)
        self._device_format_key = None
        self.asio_device = selected[1] if isinstance(selected, tuple) and selected[0] == "asio" else None
        self.wasapi_device = selected[1] if isinstance(selected, tuple) and selected[0] == "wasapi" else None
        self.wasapi_endpoint_id = selected[2] if self.wasapi_device is not None and len(selected) > 2 else None
        self._wasapi_events = queue.SimpleQueue()
        self._wasapi_retry_after = 0.0
        self._wasapi_recovery_needed = False
        self._wasapi_failure_reported = False
        # Serialize initialization against an owned global teardown barrier.
        # The active product's export initializes in a spawned process; an
        # unrelated same-process initialized output is explicitly refused at
        # retirement instead of having its updater policy changed.
        with self.update_retirement.initialization():
            self._initialize_bass(selected,preferences,window_handle,stream_flags)
        self._quiesced = False
        self.engine.set_delay_output_sample_rate(preferences.sample_rate)
        from bass_bindings import BASS_STREAM_DECODE
        decode_output = self.asio_device is not None or self.wasapi_device is not None or bool(stream_flags & BASS_STREAM_DECODE)
        stream = self.bassmidi.BASS_MIDI_StreamCreate(
            16, stream_flags, preferences.sample_rate if decode_output else 0,
        )
        if not stream:
            code = int(self.bass.BASS_ErrorGetCode())
            self._abort_start("midi_stream", code)
        self.stream = int(stream)
        if getattr(preferences,'synthesis_mode','compatible')=='high_performance' and isinstance(self.bass,ctypes.CDLL) and isinstance(self.bassmidi,ctypes.CDLL):
            # Adopt before initialization: a refused native teardown must not
            # lose the Python owner of borrowed decoder/worker resources.
            candidate=NativeMulticoreSynthesis.__new__(NativeMulticoreSynthesis)
            self.multicore_synthesis=candidate
            try:
                candidate.__init__(self.bass,self.bassmidi,self.stream,preferences)
            except BaseException as failure:
                try:self.close()
                except (OSError,RuntimeError) as cleanup:
                    failure.add_note(tr('runtime.startup_cleanup_failed',error=str(cleanup)))
                    raise failure from cleanup
                raise
        if not self.bass.BASS_ChannelSetAttribute(self.stream, BASS_ATTRIB_MIDI_CPU, preferences.midi_cpu_limit):
            error = int(self.bass.BASS_ErrorGetCode())
            self._abort_start("midi_cpu_limit", error)
        # AudioSession is the sole normal writer of the explicit engine context.
        self.engine.runtime.bind_stream(self.stream)
        self.engine.set_output_meter_decode_source(self.stream if decode_output else 0)
        interpolation_error = None
        if not self.bass.BASS_ChannelSetAttribute(self.stream, BASS_ATTRIB_MIDI_SRC, float(preferences.interpolation)):
            interpolation_error = int(self.bass.BASS_ErrorGetCode())
        self.engine.init_custom_fx(self.stream, preferences.sample_rate)
        self.engine.reset_channel_effect_sends()
        self._shared_start_pending = not decode_output and not start_shared
        if not decode_output and start_shared and not self.bass.BASS_ChannelPlay(self.stream, False):
            code = int(self.bass.BASS_ErrorGetCode())
            self._abort_start("channel_play", code)
        self.engine.configure_auto_reset(preferences.auto_reset_delay_s)
        return interpolation_error

    def _initialize_bass(self,selected,preferences,window_handle,stream_flags):
        from bass_bindings import BASS_STREAM_DECODE
        decode_output = self.asio_device is not None or self.wasapi_device is not None or bool(stream_flags & BASS_STREAM_DECODE)
        for option, value in (
            (BASS_CONFIG_UPDATETHREADS, UPDATE_THREAD_COUNT),
            (BASS_CONFIG_BUFFER, preferences.buffer_ms),
            # bass.md: shared playback updates5..100ms, NOT device period.
            # Decoders/ASIO retain prior configuration; they pull their own data.
            (BASS_CONFIG_UPDATEPERIOD, 5 if decode_output else preferences.shared_update_ms),
            (BASS_CONFIG_FLOATDSP, True),
            (BASS_CONFIG_MIDI_VOICES, preferences.voices),
            # bassmidi.md BASS_CONFIG_MIDI_COMPACT/SAMPLEREAD: fixed defaults.
            (BASS_CONFIG_MIDI_COMPACT, True),
            (BASS_CONFIG_MIDI_SAMPLEREAD, 32768),
        ):
            self.bass.BASS_SetConfig(option, value)
        # Explicit pull-only export is a no-device session too. Its requested
        # sample rate must not accidentally become the default output rate.
        device = 0 if decode_output else selected
        if not self.bass.BASS_Init(device, preferences.sample_rate, 0, window_handle, None):
            raise AudioStartError("bass_init", int(self.bass.BASS_ErrorGetCode()))
        self.initialized = True
        self._quiesced = False
        self.update_retirement.adopt_initialized()

    def _abort_start(self, stage, code):
        """Keep the startup failure primary even if native cleanup also fails.

        BASS_Free may return BUSY. close retains resource ownership in that
        case, so the controller can retry rather than publish a false CLOSED.
        """
        failure=AudioStartError(stage,code)
        try:
            self.close()
        except (OSError,RuntimeError) as cleanup:
            failure.add_note(tr('runtime.startup_cleanup_failed',error=str(cleanup)))
            raise failure from cleanup
        raise failure

    def apply_live_preferences(self, preferences, previous):
        """Apply only documented live BASS controls; undo partial API failure.

        Do not recreate the stream, alter its MIDI timing flags, or reset CCs.
        bassmidi.md BASS_ATTRIB_MIDI_VOICES/SRC: stream attributes.
        Topics8/19 compact/read chunk configuration is fixed at startup.
        """
        if not self.initialized or not self.stream:
            return
        operations = (
            ("interpolation", self.bass.BASS_ChannelSetAttribute, (self.stream, BASS_ATTRIB_MIDI_SRC)),
            ("voices", self.bass.BASS_ChannelSetAttribute, (self.stream, BASS_ATTRIB_MIDI_VOICES)),
            ("midi_cpu_limit", self.bass.BASS_ChannelSetAttribute, (self.stream, BASS_ATTRIB_MIDI_CPU)),
        )
        applied = []
        for name, setter, args in operations:
            value, old = getattr(preferences, name), getattr(previous, name)
            if value == old:
                continue
            if not setter(*args, value):
                error = int(self.bass.BASS_ErrorGetCode())
                for undo_name, undo_setter, undo_args in reversed(applied):
                    undo_setter(*undo_args, getattr(previous, undo_name))
                raise AudioStartError(name, error)
            applied.append((name, setter, args))

    def diagnostic_status(self, asio_dll=None):
        """Read processing utilization in the correct backend, not Qt widgets.

        bass.h BASS_ATTRIB_CPU/BASS_ATTRIB_MIDI_VOICES_ACTIVE; bassasio.h and
        basswasapi.h GetCPU return float. No graph/resource mutation.
        """
        from bass_bindings import BASS_ATTRIB_CPU, BASS_ATTRIB_MIDI_VOICES_ACTIVE
        result = {}
        if self.multicore_synthesis is not None:
            result.update(self.multicore_synthesis.health())
        for name, attr in (("source_cpu", BASS_ATTRIB_CPU), ("voices", BASS_ATTRIB_MIDI_VOICES_ACTIVE)):
            value = ctypes.c_float()
            if self.stream and self.bass.BASS_ChannelGetAttribute(self.stream, attr, ctypes.byref(value)):
                result[name] = float(value.value)
        if self.initialized:
            cpu = _backend_processing_load(self.bass, self, asio_dll)
            if cpu is not None:
                result["output_cpu"] = cpu
        return result

    def output_timing(self, asio_dll=None):
        """Read native source/device domains; unknown is None, never an invented zero.

        Control-side only. Reported startup/driver latency is not a render
        deadline or a measured hardware loopback. No sample-rate policy change.
        """
        from bass_bindings import BASS_CHANNELINFO, BASS_INFO, BASS_SAMPLE_FLOAT
        result = dict(backend=("asio" if self.asio_started else
                      "wasapi_exclusive" if self.wasapi_started else "shared") if self.initialized else None,
                      source_rate=None, output_rate=None, source_channels=None,
                      output_channels=None, output_latency_ms=None,
                      minimum_buffer_ms=None, buffer_bytes=None, underruns=None)
        source = BASS_CHANNELINFO()
        if self.stream and self.bass.BASS_ChannelGetInfo(self.stream, ctypes.byref(source)):
            result.update(source_rate=int(source.freq), source_channels=int(source.chans),
                          source_bits=32 if source.flags & BASS_SAMPLE_FLOAT else 8 if source.flags & 1 else 16,
                          source_encoding="Float" if source.flags & BASS_SAMPLE_FLOAT else "PCM")
        if not self.initialized:
            return result
        if self.asio_started and asio_dll is not None:
            # bassasio.h: double GetRate(void), DWORD GetLatency(BOOL input).
            asio_dll.BASS_ASIO_GetRate.argtypes, asio_dll.BASS_ASIO_GetRate.restype = [], ctypes.c_double
            asio_dll.BASS_ASIO_GetLatency.argtypes = [ctypes.c_int]
            asio_dll.BASS_ASIO_GetLatency.restype = ctypes.c_uint32
            rate = float(asio_dll.BASS_ASIO_GetRate())
            frames = int(asio_dll.BASS_ASIO_GetLatency(False))
            if rate > 0:
                result["output_rate"] = rate
                if frames != 0xFFFFFFFF:
                    result["output_latency_ms"] = frames / rate * 1000
            if callable(getattr(asio_dll, "BASS_ASIO_ChannelGetInfo", None)):
                class ChannelInfo(ctypes.Structure):
                    _fields_ = [("group", ctypes.c_uint32), ("format", ctypes.c_uint32), ("name", ctypes.c_char*32)]
                # bassasio.h native channel format, not ChannelGetFormat's
                # user-configurable callback conversion format.
                asio_dll.BASS_ASIO_ChannelGetInfo.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.POINTER(ChannelInfo)]
                asio_dll.BASS_ASIO_ChannelGetInfo.restype = ctypes.c_int
                formats = []
                for channel in range(int(source.chans)):
                    native = ChannelInfo()
                    if not asio_dll.BASS_ASIO_ChannelGetInfo(False, channel, ctypes.byref(native)):
                        formats = []; break
                    formats.append(native_output_format(int(native.format), "asio"))
                if formats and all(value == formats[0] and value for value in formats):
                    result.update(formats[0], output_channels=int(source.chans))
        elif self.wasapi_started and self._wasapi_dll is not None:
            dll = self._wasapi_dll
            dll.BASS_WASAPI_GetInfo.argtypes = [ctypes.POINTER(WASAPIInfo)]
            dll.BASS_WASAPI_GetInfo.restype = ctypes.c_int
            info = WASAPIInfo()
            if dll.BASS_WASAPI_GetInfo(ctypes.byref(info)):
                result.update(output_rate=int(info.freq), output_channels=int(info.chans),
                              buffer_bytes=int(info.buflen))
                result.update(native_output_format(int(info.format), "wasapi"))
                if info.freq > 0 and info.chans > 0:
                    # Installed CHM/GetData and official c/synth/synth.c:
                    # queued/allocated bytes use callback Float, irrespective
                    # of the device's PCM resolution. AVAILABLE consumes no PCM.
                    bytes_per_second = int(info.freq) * int(info.chans) * ctypes.sizeof(ctypes.c_float)
                    result["output_buffer_ms"] = int(info.buflen) / bytes_per_second * 1000
                    if callable(getattr(dll, "BASS_WASAPI_GetData", None)):
                        waiting = dll.BASS_WASAPI_GetData(None, 0)
                        if type(waiting) is int and 0 <= waiting < 0xFFFFFFFF:
                            result["output_latency_ms"] = waiting / bytes_per_second * 1000
                            result["output_latency_kind"] = "queue"
        else:
            info = BASS_INFO()
            if self.bass.BASS_GetInfo(ctypes.byref(info)):
                result.update(output_rate=int(info.freq), output_channels=int(info.speakers),
                              minimum_buffer_ms=int(info.minbuf))
                # bass.h BASS_DEVICE_LATENCY=0x100: otherwise zero means the
                # latency measurement was not requested, not zero latency.
                if info.initflags & 0x100:
                    result["output_latency_ms"] = int(info.latency)
                result.update(self._shared_device_format(int(info.freq)))
            # bass.h attribute13 / bass.md: seconds of target playback buffer.
            # This shared-stream target is not the global maximum capacity or
            # reported device latency; decoding ASIO/WASAPI branches skip it.
            from bass_bindings import BASS_ATTRIB_BUFFER, BASS_ACTIVE_STALLED
            target = ctypes.c_float()
            if self.stream and self.bass.BASS_ChannelGetAttribute(self.stream, BASS_ATTRIB_BUFFER, ctypes.byref(target)):
                if math.isfinite(target.value) and target.value >= 0:
                    result["playback_buffer_ms"] = target.value * 1000
            if self.stream:
                result["output_stalled"] = self.bass.BASS_ChannelIsActive(self.stream) == BASS_ACTIVE_STALLED
        return result

    def _shared_device_format(self, rate):
        from bass_bindings import BASS_DEVICEINFO
        from windows_integration import read_endpoint_device_format
        index = self.bass.BASS_GetDevice()
        if not isinstance(index, int) or index <= 0: return {}
        info = BASS_DEVICEINFO()
        if not self.bass.BASS_GetDeviceInfo(index, ctypes.byref(info)) or not info.name:
            return {}
        endpoint = info.driver.decode("utf-8") if info.driver else None
        if not endpoint:
            # The virtual Default entry and its concrete BASS target both
            # carry BASS_DEVICE_DEFAULT. Resolve that target rather than
            # assume a Windows console/multimedia role matches BASS.
            candidate = BASS_DEVICEINFO()
            target = 2
            while self.bass.BASS_GetDeviceInfo(target, ctypes.byref(candidate)):
                if candidate.flags & 2 and candidate.driver:
                    endpoint = candidate.driver.decode("utf-8")
                    break
                target += 1
            if not endpoint: return {}
        if endpoint and not endpoint.startswith("{"): return {}
        key = (index, endpoint, rate)
        if key != self._device_format_key or time.monotonic() >= self._device_format_due:
            self._device_format_key = key
            self._device_format_due = time.monotonic() + 2
            self._device_format_cache = read_endpoint_device_format(endpoint)
        return {key: value for key, value in self._device_format_cache.items() if key.startswith("output_")}

    def check_output_rate(self, sample_rate):
        """Readonly exclusive-format preflight, not a startup guarantee.

        Installed basswasapi.h/official CheckFormat: stereo, EXCLUSIVE,
        resolution enum or DWORD -1; BASS error37 is unsupported FORMAT.
        Shared/ASIO can resample and must not be falsely rejected by this
        exclusive-only query. No open/stop/rebind/INI or hardware-rate change.
        """
        if self.wasapi_device is None or self._wasapi_dll is None:
            return None
        resolution=int(self._wasapi_dll.BASS_WASAPI_CheckFormat(
            self.wasapi_device,int(sample_rate),2,BASS_WASAPI_EXCLUSIVE))
        if resolution != 0xffffffff:
            return True
        raise AudioStartError('output_format',int(self.bass.BASS_ErrorGetCode()))

    def start_output(self, asio_dll, wasapi_dll=None, preferences=None) -> None:
        self._prepare_output_probe()
        if self._shared_start_pending:
            if not self.stream or not self.bass.BASS_ChannelPlay(self.stream, False):
                raise AudioStartError("channel_play", int(self.bass.BASS_ErrorGetCode()))
            self._shared_start_pending = False
        if self.asio_device is not None and not self.asio_started:
            if asio_dll is None:
                raise AudioStartError("asio_start", 0)
            try:
                self.asio_output.start(asio_dll, self.asio_device, self.stream,
                    **({"output_probe":self.output_probe} if self.output_probe is not None else {}))
            except (OSError, RuntimeError) as exc:
                if getattr(exc,'backend_cleanup_pending',None)=='asio':
                    self._asio_cleanup_pending=True;self._asio_dll=asio_dll
                raise AudioStartError("asio_start", str(exc)) from exc
            self.asio_started = True
            self._asio_dll = asio_dll
            self._asio_notice_seen=(0,0)
            self._asio_recovery_needed=False
            self._asio_retry_after=0.
            self._asio_failure_reported=False
        if self.wasapi_device is not None and not self.wasapi_started:
            if wasapi_dll is None or preferences is None:
                raise AudioStartError("wasapi_start", 0)
            try:
                self.wasapi_device = self._resolve_wasapi_device(wasapi_dll)
                self.asio_output.start_wasapi(
                    wasapi_dll, self.wasapi_device, self.stream,
                    preferences.sample_rate,
                    block_ms=preferences.exclusive_block_ms,
                    **({"output_probe":self.output_probe} if self.output_probe is not None else {}),
                )
            except (OSError, RuntimeError) as exc:
                if getattr(exc,'backend_cleanup_pending',None)=='wasapi':
                    self._wasapi_cleanup_pending=True;self._wasapi_dll=wasapi_dll
                raise AudioStartError("wasapi_start", str(exc)) from exc
            self._wasapi_dll = wasapi_dll
            self._wasapi_preferences = preferences
            self.wasapi_started = True
            if hasattr(wasapi_dll, "BASS_WASAPI_SetNotify"):
                # basswasapi.h WASAPINOTIFYPROC: callbacks may arrive on an
                # audio/system thread. Only enqueue; Qt/control work is later.
                self._wasapi_notify = WASAPI_NOTIFY_CALLBACK(
                    lambda notice, device, _user: self._wasapi_events.put((notice, device))
                )
                if not wasapi_dll.BASS_WASAPI_SetNotify(
                    ctypes.cast(self._wasapi_notify, ctypes.c_void_p), None,
                ):
                    failure=AudioStartError("wasapi_start",int(self.bass.BASS_ErrorGetCode()))
                    try:
                        self.asio_output.stop_wasapi(wasapi_dll)
                        if self.output_probe is not None:self.output_probe.release("wasapi")
                    except (OSError,RuntimeError) as cleanup:
                        failure.add_note(tr('runtime.startup_cleanup_failed',error=str(cleanup)))
                        raise failure from cleanup
                    self._wasapi_notify = None
                    self._wasapi_dll = None
                    self.wasapi_started = False
                    raise failure

    def _resolve_wasapi_device(self, dll) -> int:
        """Rebind by persistent Windows endpoint ID after device-list changes."""
        enumerate_devices = getattr(self.asio_output, "wasapi_devices", None)
        if self.wasapi_endpoint_id and enumerate_devices is not None:
            for _name, index, endpoint_id in enumerate_devices(dll):
                if endpoint_id == self.wasapi_endpoint_id:
                    return index
            raise RuntimeError("Selected WASAPI endpoint is no longer available")
        return self.wasapi_device

    def maintain_output_observation(self) -> None:
        """Repair only the final-mix meter after a Default-device switch."""
        if self.initialized and not self._quiesced and self.asio_device is None and self.wasapi_device is None and self.stream:
            self.engine.refresh_output_meter()

    def maintain_asio_output(self) -> str | None:
        """Handle driver Reset/rate/stop outside its native notification callback.

        Preserve MIDI stream, voices, fonts, effects and ingress workers. A
        failed Free retains its probe borrower; retries never force-free it.
        """
        if self._quiesced or self.asio_device is None or self._asio_dll is None:
            return None
        dll=self._asio_dll
        if not dll.BASS_ASIO_SetDevice(self.asio_device):
            self._asio_recovery_needed=True
        notices=self.output_probe.asio_notifications() if self.output_probe is not None else (0,0)
        if notices!=self._asio_notice_seen:
            self._asio_recovery_needed=True
        started=getattr(dll,"BASS_ASIO_IsStarted",None)
        if self.asio_started and callable(started) and not started():
            self._asio_recovery_needed=True
        if not self._asio_recovery_needed or time.monotonic()<self._asio_retry_after:
            return None
        self._asio_retry_after=time.monotonic()+2.
        observe=self.telemetry is not None and self.telemetry.observation_enabled
        if observe:self.telemetry.pause_observation()
        try:
            if self.asio_started or self._asio_cleanup_pending:
                self.asio_output.stop(dll)
                if self.output_probe is not None:self.output_probe.release("asio")
            self.asio_started=False;self._asio_cleanup_pending=False
            if self.output_probe is not None:self.output_probe.close();self.output_probe=None
            self._prepare_output_probe()
            self._asio_notice_seen=(0,0)
            self.asio_output.start(dll,self.asio_device,self.stream,
                **({"output_probe":self.output_probe} if self.output_probe is not None else {}))
        except (OSError,RuntimeError) as error:
            if getattr(error,'backend_cleanup_pending',None)=='asio':self._asio_cleanup_pending=True
            if not self._asio_failure_reported:
                self._asio_failure_reported=True
                return "failed"
            return None
        finally:
            if observe:self.telemetry.start_observation()
        self.asio_started=True
        self._asio_recovery_needed=False;self._asio_failure_reported=False
        return "recovered"

    def switch_decode_output(self, selected, asio_dll, wasapi_dll, preferences) -> bool:
        """Rebind ASIO/exclusive output only; unchanged source and loaded fonts.

        Playback/decode stream flags cannot be changed by ChannelFlags. Those
        transitions return False for the retained-font stream-rebuild path.
        """
        if not self.initialized or self._quiesced or not self.stream:
            return False
        if not isinstance(selected,tuple) or selected[0] not in ("asio","wasapi"):
            return False
        if self.asio_device is None and self.wasapi_device is None:return False
        prior=("asio",self.asio_device) if self.asio_device is not None else (
            "wasapi",self.wasapi_device,self.wasapi_endpoint_id)
        if tuple(selected)==prior:return True
        observe=self.telemetry is not None and self.telemetry.observation_enabled
        if observe:self.telemetry.pause_observation()
        def detach():
            if self.asio_device is not None:
                if self.asio_started or self._asio_cleanup_pending:
                    if not self._asio_dll.BASS_ASIO_SetDevice(self.asio_device):
                        raise RuntimeError(f'BASS_ASIO_SetDevice: {self._asio_dll.BASS_ASIO_ErrorGetCode()}')
                    self.asio_output.stop(self._asio_dll)
                    if self.output_probe is not None:self.output_probe.release("asio")
                self.asio_started=False;self._asio_cleanup_pending=False
            if self.wasapi_device is not None:
                if self._wasapi_notify is not None:
                    if not self._wasapi_dll.BASS_WASAPI_SetNotify(None,None):
                        raise RuntimeError('BASS_WASAPI_SetNotify')
                    self._wasapi_notify=None
                if self.wasapi_started or self._wasapi_cleanup_pending:
                    self.asio_output.stop_wasapi(self._wasapi_dll)
                    if self.output_probe is not None:self.output_probe.release("wasapi")
                self.wasapi_started=False;self._wasapi_cleanup_pending=False
            if self.output_probe is not None:self.output_probe.close();self.output_probe=None
        def attach(target):
            self.asio_device=target[1] if target[0]=="asio" else None
            self.wasapi_device=target[1] if target[0]=="wasapi" else None
            self.wasapi_endpoint_id=target[2] if target[0]=="wasapi" and len(target)>2 else None
            self._asio_notice_seen=(0,0);self._asio_recovery_needed=False
            self._wasapi_events=queue.SimpleQueue();self._wasapi_recovery_needed=False
            self.start_output(asio_dll,wasapi_dll,preferences)
        try:
            detach()
            try:attach(selected)
            except (OSError,RuntimeError,AudioStartError) as failure:
                # start_output retains any unconfirmed backend cleanup owner.
                # Roll back output without touching source/FontInit/CC reset.
                detach()
                attach(prior)
                raise failure
        finally:
            if observe:self.telemetry.start_observation()
        return True

    def maintain_wasapi_output(self) -> str | None:
        """Recover a failed endpoint without rebuilding MIDI/SoundFont state."""
        if self._quiesced or self.wasapi_device is None or self._wasapi_dll is None:
            return None
        while not self._wasapi_events.empty():
            notice, device = self._wasapi_events.get_nowait()
            if device == self.wasapi_device and notice in (WASAPI_NOTIFY_FAIL, WASAPI_NOTIFY_DISABLED):
                self._wasapi_recovery_needed = True
            elif device == self.wasapi_device and notice == WASAPI_NOTIFY_ENABLED and not self.wasapi_started:
                self._wasapi_recovery_needed = True
        is_started = getattr(self._wasapi_dll, "BASS_WASAPI_IsStarted", None)
        if self.wasapi_started and is_started is not None and not is_started():
            self._wasapi_recovery_needed = True
        if not self._wasapi_recovery_needed or time.monotonic() < self._wasapi_retry_after:
            return None
        self._wasapi_retry_after = time.monotonic() + 2.0
        observe = self.telemetry is not None and self.telemetry.observation_enabled
        if observe:
            self.telemetry.pause_observation()
        try:
            if self.wasapi_started or self._wasapi_cleanup_pending:
                self.asio_output.stop_wasapi(self._wasapi_dll)
                if self.output_probe is not None:self.output_probe.release("wasapi")
            self.wasapi_started = False
            self._wasapi_cleanup_pending = False
            if self.output_probe is not None:
                self.output_probe.close();self.output_probe=None
            self._prepare_output_probe()
            self.wasapi_device = self._resolve_wasapi_device(self._wasapi_dll)
            self.asio_output.start_wasapi(
                self._wasapi_dll, self.wasapi_device, self.stream,
                self._wasapi_preferences.sample_rate,
                block_ms=self._wasapi_preferences.exclusive_block_ms,
                **({"output_probe":self.output_probe} if self.output_probe is not None else {}),
            )
        except (OSError, RuntimeError) as error:
            if getattr(error,'backend_cleanup_pending',None)=='wasapi':
                self._wasapi_cleanup_pending=True
            if observe:
                self.telemetry.start_observation()
            if self._wasapi_failure_reported:
                return None
            self._wasapi_failure_reported = True
            return "failed"
        self.wasapi_started = True
        self._wasapi_recovery_needed = False
        self._wasapi_failure_reported = False
        if observe:
            self.telemetry.start_observation()
        return "recovered"

    def quiesce(self, asio_dll=None) -> None:
        """Stop output and forget effect handles before SoundFont teardown."""
        if self.telemetry is not None:
            self.telemetry.pause_observation()
        if self._quiesced:
            return
        asio_dll = asio_dll if asio_dll is not None else self._asio_dll
        if self.asio_started or self._asio_cleanup_pending:
            if asio_dll is None:raise RuntimeError('owned ASIO backend unavailable for teardown')
            self.asio_output.stop(asio_dll)
            if self.output_probe is not None:self.output_probe.release("asio")
        self.asio_started = False
        self._asio_cleanup_pending = False
        self._asio_recovery_needed=False
        self._asio_notice_seen=(0,0)
        self._asio_dll = None
        if self._wasapi_dll is not None:
            if self._wasapi_notify is not None:
                if not self._wasapi_dll.BASS_WASAPI_SetNotify(None, None):
                    raise RuntimeError(f'BASS_WASAPI_SetNotify teardown: {self.bass.BASS_ErrorGetCode()}')
                self._wasapi_notify = None
            if self.wasapi_started or self._wasapi_cleanup_pending:
                self.asio_output.stop_wasapi(self._wasapi_dll)
                if self.output_probe is not None:self.output_probe.release("wasapi")
        self.wasapi_started = False
        self._wasapi_cleanup_pending = False
        self._wasapi_dll = None
        self._wasapi_preferences = None
        self._wasapi_recovery_needed = False
        self._wasapi_failure_reported = False
        # BASS_Init starts its device output. Shared output has no ASIO/WASAPI
        # stop above: removing final-mix DSP/FREE sync while it still processes
        # and immediately freeing BASS races deferred native cleanup. Stop the
        # current owned output first; refusal retains all meter/effect owners.
        if self.initialized:
            self.update_retirement.stop()
            self.update_retirement.suspend()
        self.stop_tuning_audition()
        audition=getattr(self,'instrument_audition',None)
        if audition:audition.close();self.instrument_audition=None
        self.engine.reset_output_meter(remove_dsp=True)
        sensor=getattr(getattr(self.engine,"runtime",None),"playback_timing",None)
        if sensor is not None: sensor.close()
        self.engine.set_output_meter_decode_source(0)
        self.engine.detach_stream_effects()
        if self.multicore_synthesis is not None:
            self.multicore_synthesis.close();self.multicore_synthesis=None
        if self.output_probe is not None:
            self.output_probe.close();self.output_probe=None
        self._quiesced = True

    def stop_tuning_audition(self):
        source=getattr(self,'tuning_audition',None)
        if source:source.close();self.tuning_audition=None
        prepared=getattr(self,'instrument_audition',None)
        if prepared and not prepared.closed and getattr(prepared,'_phrase_active',False):prepared.silence()

    def install_instrument_audition(self, source, *, tuning=False):
        if not self.stream or not self.initialized:raise RuntimeError(tr('creator.engine_required'))
        from bass_bindings import BASS_CHANNELINFO
        parent_info=BASS_CHANNELINFO();source_info=BASS_CHANNELINFO()
        if (not self.bass.BASS_ChannelGetInfo(self.stream,ctypes.byref(parent_info))
                or not self.bass.BASS_ChannelGetInfo(source.stream,ctypes.byref(source_info))
                or parent_info.freq!=source_info.freq or parent_info.chans!=2 or source_info.chans!=2):
            source.close();raise RuntimeError(tr('creator.audition_unavailable'))
        slot='tuning_audition' if tuning else 'instrument_audition'
        previous=getattr(self,slot,None)
        if previous:previous.close()
        setattr(self,slot,source)
        source.attach(self.stream)
        for event in range(32,43):
            value=self.bassmidi.BASS_MIDI_StreamGetEvent(self.stream,0,event)
            if value!=0xffffffff and not self.bassmidi.BASS_MIDI_StreamEvent(source.stream,0,event,value):source._fail()

    def close(self) -> None:
        """Free BASS after the caller has released its SoundFont handles."""
        self.quiesce()
        if self.initialized:
            self.update_retirement.free()
        self.initialized = False
        self.stream = 0
        self._shared_start_pending = False
        self.asio_device = None
        self.wasapi_device = None
        self.wasapi_endpoint_id = None
        self.engine.runtime.clear_stream()
        # Free is confirmed even when restoration fails. Subsequent close
        # retries only the retained original config and never calls Free again.
        self.update_retirement.restore()


# Fixed diagnostic attacks use the MIDI maximum. Musical phrase and pointer
# velocities remain authored/user input;this is not an output makeup gain.
AUDITION_NOTE_VELOCITY=127
AUDITION_CHANNEL_VOLUME=127


class InstrumentAuditionSource:
    """Session-owned private MIDI decoder, mixed into the existing output.

    Preparation may run on a file worker; attach/events/close are control-side.
    No live MIDI channel or group/font map is changed. The parent DSP supplies
    shared/ASIO/WASAPI equally and the existing final effects/meter see its PCM.
    """
    def _init_backend(self):
        from bass_bindings import bass,bassmidi
        self._control_lock=threading.RLock()
        self.bass=bass;self.midi=bassmidi;self.stream=0;self.font=0;self.parent=0;self.dsp=0;self.pointer=None;self.closed=False
        self.dll=ctypes.CDLL(str(os.environ.get('ABMSYNTH_OBSERVATION_DLL',Path(__file__).with_name('abmsynth_dsp.dll'))))
        for name,args,result in (('version',[],ctypes.c_uint),('create',[ctypes.c_uint,ctypes.c_void_p],ctypes.c_void_p),
                ('proc',[],ctypes.c_void_p),('destroy',[ctypes.c_void_p],None),('failures',[ctypes.c_void_p],ctypes.c_uint)):
            function=getattr(self.dll,'instrument_audition_'+name);function.argtypes=args;function.restype=result
        if self.dll.instrument_audition_version()!=1:raise RuntimeError(tr('creator.audition_version'))

    def __init__(self,path,bank,program,rate,preferences=None):
        from bass_bindings import BASS_UNICODE,BASS_MIDI_FONT,BASS_SAMPLE_FLOAT,BASS_STREAM_DECODE
        self._init_backend();bass,bassmidi=self.bass,self.midi
        try:
            self.font=bassmidi.BASS_MIDI_FontInit(ctypes.cast(ctypes.c_wchar_p(str(path)),ctypes.c_void_p),BASS_UNICODE)
            if not self.font or not bassmidi.BASS_MIDI_FontLoad(self.font,program,bank):self._fail()
            self.stream=bassmidi.BASS_MIDI_StreamCreate(1,BASS_SAMPLE_FLOAT|BASS_STREAM_DECODE,rate)
            if not self.stream or not bassmidi.BASS_MIDI_StreamSetFonts(self.stream,(BASS_MIDI_FONT*1)(BASS_MIDI_FONT(self.font,-1,0)),1):self._fail()
            for event,value in ((6,int(bank==128)),(10,0 if bank==128 else bank),(2,program),
                                (12,AUDITION_CHANNEL_VOLUME),(14,127),(23,getattr(preferences,'default_reverb_send',40))):
                if not bassmidi.BASS_MIDI_StreamEvent(self.stream,0,event,value):self._fail()
            self.pointer=self.dll.instrument_audition_create(self.stream,ctypes.cast(bass.BASS_ChannelGetData,ctypes.c_void_p))
            if not self.pointer:self._fail()
        except BaseException:
            self.close();raise

    @classmethod
    def for_tuning(cls,parent,request):
        """Borrow the current mapping,never select or silence a live channel.

        SoundFontRuntime closes this reader before remap/free under its source
        lease. AudioSession closes it before stream/device teardown. No FontInit
        or file compilation occurs on the GUI press path.
        """
        from bass_bindings import BASS_CHANNELINFO,BASS_MIDI_FONT,BASS_MIDI_FONTEX,BASS_MIDI_FONT_EX,BASS_SAMPLE_FLOAT,BASS_STREAM_DECODE
        self=cls.__new__(cls);self._init_backend()
        try:
            info=BASS_CHANNELINFO()
            if not self.bass.BASS_ChannelGetInfo(parent,ctypes.byref(info)):self._fail()
            device=self.bass.BASS_ChannelGetDevice(parent)
            previous=self.bass.BASS_GetDevice()
            if not self.bass.BASS_SetDevice(device):self._fail()
            try:self.stream=self.midi.BASS_MIDI_StreamCreate(16 if request.get('tracks') else 1,BASS_SAMPLE_FLOAT|BASS_STREAM_DECODE|0x400,info.freq)
            finally:self.bass.BASS_SetDevice(previous)
            if not self.stream:self._fail()
            system=self.midi.BASS_MIDI_StreamGetEvent(parent,0,61)
            if system!=0xffffffff and not self.midi.BASS_MIDI_StreamEvent(self.stream,0,61,system):self._fail()
            from bass_bindings import BASS_ATTRIB_MIDI_SRC
            interpolation=ctypes.c_float()
            if self.bass.BASS_ChannelGetAttribute(parent,BASS_ATTRIB_MIDI_SRC,ctypes.byref(interpolation)):
                self.bass.BASS_ChannelSetAttribute(self.stream,BASS_ATTRIB_MIDI_SRC,interpolation)
            if request.get('source'):
                font,bank,program=request['source']
                # Pin the already resolved source to a private address. This
                # also handles single-preset fallback and XG bank127 drums.
                maps=(BASS_MIDI_FONTEX*1)(BASS_MIDI_FONTEX(font,program,bank,0,128 if request.get('drum') else 0,0))
                pointer=ctypes.cast(maps,ctypes.POINTER(BASS_MIDI_FONT));count=1|BASS_MIDI_FONT_EX
                bank=program=0
            else:
                count=self.midi.BASS_MIDI_StreamGetFonts(parent,None,0)
                if not count or count==0xffffffff:self._fail()
                maps=(BASS_MIDI_FONTEX*count)()
                if self.midi.BASS_MIDI_StreamGetFonts(parent,maps,count|BASS_MIDI_FONT_EX)!=count:self._fail()
                pointer=ctypes.cast(maps,ctypes.POINTER(BASS_MIDI_FONT));count|=BASS_MIDI_FONT_EX
                channel=request.get('channel',0)
                bank=self.midi.BASS_MIDI_StreamGetEvent(parent,channel,10)
                program=self.midi.BASS_MIDI_StreamGetEvent(parent,channel,2)
                if 'program' in request:bank,program=request.get('bank',0),request['program']
            if not self.midi.BASS_MIDI_StreamSetFonts(self.stream,pointer,count):self._fail()
            channel=request.get('channel',0)
            drums=int(request.get('drum',self.midi.BASS_MIDI_StreamGetEvent(parent,channel,6)))
            lsb=0 if request.get('source') else request.get('bank_lsb',self.midi.BASS_MIDI_StreamGetEvent(parent,channel,70))
            for event,value in ((6,drums),(10,bank if bank!=128 else 0),(70,lsb),(2,program)):
                if not self.midi.BASS_MIDI_StreamEvent(self.stream,0,event,value):self._fail()
            if 'program' in request:
                resolved=BASS_MIDI_FONT()
                if not self.midi.BASS_MIDI_StreamGetPreset(self.stream,0,ctypes.byref(resolved)) or not resolved.font:
                    # A single-instrument font need not contain the curated
                    # GM reference. Fall back to the actual channel selection,
                    # not to a guessed source bank or silent fixed Program.
                    drums,bank,lsb,program=(self.midi.BASS_MIDI_StreamGetEvent(parent,channel,event)
                        for event in (6,10,70,2))
                    for event,value in zip((6,10,70,2),(drums,bank,lsb,program)):
                        self._require(self.midi.BASS_MIDI_StreamEvent(self.stream,0,event,value))
            self._tuning_selection=(drums,bank,lsb,program)
            self._audition_channels=(0,)
            if request.get('tracks'):
                self._audition_channels=tuple(track[0] for track in request['tracks'])
                self._track_selections={}
                for target,prog,pan,volume,is_drum in request['tracks']:
                    for event,value in ((6,int(is_drum)),(10,0),(70,0),(2,prog),(13,pan),(12,volume),(14,127)):
                        self._require(self.midi.BASS_MIDI_StreamEvent(self.stream,target,event,value))
                    resolved=BASS_MIDI_FONT()
                    if not is_drum and (not self.midi.BASS_MIDI_StreamGetPreset(self.stream,target,ctypes.byref(resolved)) or not resolved.font):
                        # Sparse fonts cannot supply a GM ensemble. Preserve
                        # rhythm/parts and use the current actual preset.
                        for event,value in zip((6,10,70,2),self._tuning_selection):
                            self._require(self.midi.BASS_MIDI_StreamEvent(self.stream,target,event,value))
                    self._track_selections[target]=tuple(self.midi.BASS_MIDI_StreamGetEvent(self.stream,target,event) for event in (6,10,70,2))
            self.pointer=self.dll.instrument_audition_create(self.stream,ctypes.cast(self.bass.BASS_ChannelGetData,ctypes.c_void_p))
            if not self.pointer:self._fail()
            self._tuning_parent=parent;self._tuning_state={};self._tuning_defaults={}
            self.update_tuning(request)
            if request.get('tracks'):
                for target,_prog,_pan,volume,_drum in request['tracks']:
                    self._require(self.midi.BASS_MIDI_StreamEvent(self.stream,target,12,volume))
            if request.get('phrase'):self._prepare_phrase_loop(request,info.freq)
            elif request.get('one_shot'):
                self.play_phrase(((0,.10,request['note'],AUDITION_NOTE_VELOCITY),))
                self._one_shot_rate=info.freq
            return self
        except BaseException:
            self.close();raise

    def update_tuning(self,request):
        """Reuse the actual scale/drum/expression encoders;update held voices."""
        with self._control_lock:self._update_tuning(request)

    def _update_tuning(self,request):
        if self.closed:return
        from midi_input_runtime import SCALE_PARAMETER,DRUM_PARAMETERS,EXPRESSION_EVENTS,MidiPerformanceControls
        from soundfont_files import effective_preset_expression,performance_profile_key
        parent=self._tuning_parent;channel=request.get('channel',0);values={}
        selection_changed=False
        if 'profile' not in request and 'program' not in request:
            selection=tuple(self.midi.BASS_MIDI_StreamGetEvent(parent,channel,event) for event in (6,10,70,2))
            if 0xffffffff in selection:self._fail()
            if selection!=self._tuning_selection:
                for event,value in zip((6,10,70,2),selection):
                    if not self.midi.BASS_MIDI_StreamEvent(self.stream,0,event,value):self._fail()
                self._tuning_selection=selection;selection_changed=True
        def unpack(event,key,raw):
            # StreamGetEvent returns the full StreamEvent parameter,including
            # pitch/key bits. Keep the value separate before encoding it again.
            if raw==0xffffffff or key is None:return raw
            return raw&65535 if event==63 else (raw>>8)&255
        def read(event,key=None,global_event=False):
            raw=self.midi.BASS_MIDI_StreamGetEvent(parent,0 if global_event else channel,event|((key or 0)<<16))
            if raw!=0xffffffff:values[event,key]=unpack(event,key,raw)
        # Instrument/reference audition has maximum ordinary input CC7/11.
        # MIXLEVEL and authored SF2 gain remain audible edited parameters.
        # A full-channel preview instead tests its actual fade/mute state.
        values.update({(9,None):16383,(12,None):AUDITION_CHANNEL_VOLUME,(14,None):127})
        if request.get('full_channel'):
            for event in (9,12,14):read(event,global_event=event==9)
            if request.get('muted'):values[12,None]=0
        for event in (83,84):read(event,global_event=True)
        for event in range(32,43):read(event,global_event=True)
        if 'profile' not in request and 'program' not in request:
            for event in set((3,4,5,7,8,11,13,15,21,23,24,25,26,27,28,29,43,60,76,80,81,82,85,0x10000,0x10001,*EXPRESSION_EVENTS.values())):
                if event not in (9,12,14):read(event)
            for key in range(12):read(63,key)
            if request.get('drum') or self.midi.BASS_MIDI_StreamGetEvent(parent,channel,6):
                keys=sorted({key for _at,_len,key,_vel in request['phrase']}) if request.get('phrase') else (request['note'],)
                for key in keys:
                    for parameter in DRUM_PARAMETERS.values():read(parameter.event,key)
        elif 'profile' in request:
            preset=request.get('preset') or {}
            if preset.get('disabled'):preset={}
            values[0x10000,None]=preset.get('volume') if preset.get('volume') is not None else 100
            for name,event in (('attack',28),('decay',29),('release',27),('cutoff',25),('resonance',26)):
                values[event,None]=preset.get(name) if preset.get(name) is not None else 64
            for name,value in effective_preset_expression(preset).items():
                values[EXPRESSION_EVENTS[name],None]=MidiPerformanceControls.expression_parameter(name,value)
            for name,value in request['profile'].items():
                kind,key=performance_profile_key(name)
                parameter=SCALE_PARAMETER if kind=='scale' else DRUM_PARAMETERS[kind]
                if kind=='scale' or request.get('drum'):
                    raw=parameter.parameter(key,value)
                    values[parameter.event,key]=(raw&65535 if kind=='scale' else raw>>8)
        for event,value in request.get('raw_events',()):values[event,None]=value
        changed=set()
        for key in self._tuning_state.keys()|values.keys():
            event,index=key
            if key not in self._tuning_defaults:
                self._tuning_defaults[key]=unpack(event,index,self.midi.BASS_MIDI_StreamGetEvent(self.stream,0,event|((index or 0)<<16)))
            value=values.get(key,self._tuning_defaults[key])
            if value==0xffffffff:self._fail()
            if value!=self._tuning_state.get(key):
                changed.add(event)
                raw=((index<<16)|value if event==63 else (value<<8)|index) if index is not None else value
                if not self.midi.BASS_MIDI_StreamEvent(self.stream,0,event,raw):self._fail()
                if request.get('tracks') and event in (23,24,43):
                    for target in self._audition_channels:
                        if target:self._require(self.midi.BASS_MIDI_StreamEvent(self.stream,target,event,raw))
        self._tuning_state=values
        for cc,value in request.get('controllers',()):
            if getattr(self,'_preview_cc',{}).get(cc)!=value:
                self._require(self.midi.BASS_MIDI_StreamEvent(self.stream,0,64,cc|(value<<8)))
                if not hasattr(self,'_preview_cc'):self._preview_cc={}
                self._preview_cc[cc]=value
        self._update_channel_effects(request)
        held=getattr(self,'_tuning_note',None)
        if held and (selection_changed or changed.intersection((8,84,63,50,51,0x10001))):
            # Coarse/scale/per-key pitch can be latched at Note On. Restart
            # this private voice only;never release or replay a real channel.
            if not self.midi.BASS_MIDI_StreamEvent(self.stream,0,16,0):self._fail()
            if not self.midi.BASS_MIDI_StreamEvent(self.stream,0,1,held[0]|(held[1]<<8)):self._fail()

    def _update_channel_effects(self,request):
        """Use existing native delay and rack owners,never a Python audio DSP."""
        if 'channel_fx' in request:
            if not hasattr(self,'_preview_fx'):
                from types import SimpleNamespace
                from channel_audio_effects import ChannelAudioEffects
                from stream_effects import AudioFXRack,NativeFXTopologyGraph,_FXTopologySnapshot2
                shell=SimpleNamespace(bass=self.bass,_check=lambda result,_op:self._require(result),
                    delay=SimpleNamespace(channel_fx_library=None))
                self._preview_fx=ChannelAudioEffects(shell,self.midi,rack_factory=AudioFXRack,
                    graph_factory=NativeFXTopologyGraph,snapshot_type=_FXTopologySnapshot2)
                rate=ctypes.c_float();self.bass.BASS_ChannelGetAttribute(self.stream,1,ctypes.byref(rate))
                self._preview_fx.attach(self.stream,round(rate.value))
                self._preview_fx.bind_offline(SimpleNamespace(addresses=SimpleNamespace(count=1),stream_generation=0))
                self._preview_fx_state=None
            if request['channel_fx']!=self._preview_fx_state:
                slots,enabled=request['channel_fx']
                _token,success=self._preview_fx.request(0,slots,enabled)
                self._require(success);self._preview_fx_state=request['channel_fx']
        if 'delay' in request:
            if not hasattr(self,'_preview_delay'):
                from delay_runtime import DelayRuntime
                from stream_effects import ChannelDelayEffects
                self._preview_delay_state=DelayRuntime()
                rate=ctypes.c_float();self.bass.BASS_ChannelGetAttribute(self.stream,1,ctypes.byref(rate))
                self._preview_delay_state.set_sample_rate(round(rate.value))
                self._preview_delay=ChannelDelayEffects(self.bass,self.midi,None,
                    lambda result,_op:self._require(result),native_runtime=self._preview_delay_state)
                self._preview_delay.attach(self.stream)
                if hasattr(self,'_preview_fx'):self._preview_delay.channel_fx_library=self._preview_fx.library
                channels=self._audition_channels
                drums=[False]*16 if request.get('tracks') else [bool(self._tuning_selection[0])]
                for track in request.get('tracks',()):drums[track[0]]=track[4]
                self._preview_delay.install(channels,drums)
                self._preview_delay_values=None
            if request['delay']!=self._preview_delay_values:
                kind,level,time_ms,feedback,send=request['delay']
                self._preview_delay_state.set_delay(kind,level,time_ms,feedback)
                for channel in set(self._audition_channels)|(set((9,)) if not request.get('tracks') else set()):
                    self._preview_delay_state.set_channel_send(channel,send)
                self._preview_delay_values=request['delay']

    def _require(self,result):
        if not result:self._fail()
        return result

    def _prepare_phrase_loop(self,request,rate):
        from bass_bindings import BASS_MIDI_EVENT
        events=[]
        for row in request['phrase']:
            at,length,key,velocity=row[:4];channel=row[4] if len(row)>4 else 0
            for seconds,value in ((at,key|(velocity<<8)),(at+length,key)):
                frames=round(seconds*rate)
                if frames*8>0xffffffff:raise ValueError('audition phrase position')
                events.append(BASS_MIDI_EVENT(1,value,channel,0,frames*8))
        events.sort(key=lambda event:(event.pos,bool(event.param>>8)))
        period=max(round(request.get('period',max(row[0]+row[1] for row in request['phrase'])+.5)*rate),
                   events[-1].pos//8+1)
        fn=self.dll.instrument_audition_sequence
        fn.argtypes=[ctypes.c_void_p,ctypes.POINTER(BASS_MIDI_EVENT),ctypes.c_uint,ctypes.c_uint64,ctypes.c_void_p];fn.restype=ctypes.c_int
        # bassmidi.h BASS_ATTRIB_MIDI_QUEUE_BYTE:preallocate native event queue
        # before rendering;sequence deadlines are not driven by a GUI timer.
        self._require(self.bass.BASS_ChannelSetAttribute(self.stream,0x1200c,len(events)+16))
        array=(BASS_MIDI_EVENT*len(events))(*events)
        self._require(fn(self.pointer,array,len(events),period,ctypes.cast(self.midi.BASS_MIDI_StreamEvents,ctypes.c_void_p)))
        self._loop_phrase=True

    def one_shot_finished(self):
        """Observe actual decoded tail without reading/advancing the source.

        -120dBFS for200ms is an audition retirement policy,not a BASS limit.
        GUI latency can delay cleanup but cannot delay Note On/Off.
        """
        if self.closed:return True
        fn=self.dll.instrument_audition_progress
        fn.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.POINTER(ctypes.c_uint64)]
        fn.restype=ctypes.c_int
        frames=ctypes.c_uint64();quiet=ctypes.c_uint64()
        self._require(fn(self.pointer,ctypes.byref(frames),ctypes.byref(quiet)))
        rate=self._one_shot_rate
        return frames.value>=round(.3*rate) and quiet.value>=round(.2*rate)
    def _fail(self):raise RuntimeError(tr('creator.native_failed',error=self.bass.BASS_ErrorGetCode()))

    def play_phrase(self,phrase):
        """All deadlines go to BASS,not a Python/Qt playback timer.

        bassmidi.h BASS_MIDI_EVENT and StreamEvents TIME use relative byte
        positions. The array is copied by BASS during the call.
        """
        from bass_bindings import BASS_CHANNELINFO,BASS_MIDI_EVENT,BASS_MIDI_EVENTS_TIME,BASS_MIDI_EVENTS_SYNC
        with self._control_lock:
            if self.closed:return
            self.silence();events=[]
            info=BASS_CHANNELINFO();self._require(self.bass.BASS_ChannelGetInfo(self.stream,ctypes.byref(info)))
            for row in phrase:
                at,length,key,velocity=row[:4];channel=row[4] if len(row)>4 else 0
                for stamp,value in ((at,key|(velocity<<8)),(at+length,key)):
                    # Match the repeating scheduler's nearest-frame rule.
                    # Seconds2Bytes truncates fractional frames instead.
                    position=round(stamp*info.freq)*8
                    if position==0xffffffffffffffff or position>0xffffffff:self._fail()
                    events.append(BASS_MIDI_EVENT(1,value,channel,0,position))
            events.sort(key=lambda event:(event.pos,bool(event.param>>8)))
            array=(BASS_MIDI_EVENT*len(events))(*events)
            if self.midi.BASS_MIDI_StreamEvents(self.stream,BASS_MIDI_EVENTS_TIME|BASS_MIDI_EVENTS_SYNC,array,len(events))!=len(events):self._fail()
            self._phrase_active=True
    def attach(self,parent):
        from bass_bindings import DSPPROC
        if self.closed or self.parent or parent==self.stream:raise RuntimeError(tr('creator.audition_unavailable'))
        self.callback=DSPPROC(self.dll.instrument_audition_proc());self.parent=parent
        # After multicore's replacement (MAX-1),before ordinary output DSP.
        self.dsp=self.bass.BASS_ChannelSetDSP(parent,self.callback,self.pointer,2147483645)
        if not self.dsp:self._fail()
    def note_on(self,note,velocity):
        with self._control_lock:
            if not self.closed and not self.midi.BASS_MIDI_StreamEvent(self.stream,0,1,int(note)|(int(velocity)<<8)):self._fail()
            self._tuning_note=(int(note),int(velocity))
    def note_off(self,note):
        with self._control_lock:
            if not self.closed and not self.midi.BASS_MIDI_StreamEvent(self.stream,0,1,int(note)):self._fail()
            if getattr(self,'_tuning_note',None) and self._tuning_note[0]==note:self._tuning_note=None
    def silence(self):
        with self._control_lock:
            if not self.closed and getattr(self,'_phrase_active',False):
                from bass_bindings import BASS_MIDI_EVENTS_CANCEL
                if self.midi.BASS_MIDI_StreamEvents(self.stream,BASS_MIDI_EVENTS_CANCEL,None,0)==0xffffffff:self._fail()
                self._phrase_active=False
            if not self.closed:
                for target in getattr(self,'_audition_channels',(0,)):
                    self._require(self.midi.BASS_MIDI_StreamEvent(self.stream,target,16,0))
            self._tuning_note=None
    def close(self):
        # Source commits can already own the parent lock. Acquire it before
        # the private control lock everywhere;otherwise simultaneous GUI release
        # and worker remap can invert those locks.
        parent=self.parent if self.dsp else 0
        if parent and not self.bass.BASS_ChannelLock(parent,True):self._fail()
        try:
            with self._control_lock:self._close()
        finally:
            if parent and not self.bass.BASS_ChannelLock(parent,False):self._fail()

    def _close(self):
        if self.closed:return
        if self.dsp:
            if not self.bass.BASS_ChannelRemoveDSP(self.parent,self.dsp):self._fail()
            self.dsp=0
        if hasattr(self,'_preview_fx'):self._require(self._preview_fx.detach())
        # Channel rack and delay can share a child stream. Retire rack FX
        # handles before delay releases/frees its final child lease.
        if hasattr(self,'_preview_delay'):self._preview_delay.detach()
        if self.stream:
            if not self.bass.BASS_StreamFree(self.stream):self._fail()
            self.stream=0
        if self.font:
            if not self.midi.BASS_MIDI_FontFree(self.font):self._fail()
            self.font=0
        if self.pointer:self.dll.instrument_audition_destroy(self.pointer);self.pointer=None
        self.closed=True

# ---- audio_telemetry.py ----
import ctypes
from dataclasses import dataclass

from bass_bindings import BASS_ATTRIB_MIDI_VOICES_ACTIVE


@dataclass(frozen=True)
class EngineStatus:
    active_voices: int | None
    load_percent: float | None


def processing_load_warning(load_percent):
    """Processing headroom advisory, never proof of an audible dropout.

    BASS[_ASIO/_WASAPI]_GetCPU measures processing time / audio duration.
    One overload indicator starts at70% (product headroom policy).
    Sustained new evidence controls colour; this does not redefine native CPU.
    """
    if load_percent is None or not math.isfinite(load_percent) or load_percent < 0:
        return None
    return "overload" if load_percent >= 70 else None


@dataclass
class PlaybackLampPulse:
    """Qt-free time/severity envelope for warning presentation.

    No Qt or I/O. Consumers own independent instances on their existing
    observation clocks;identical evidence/time produces identical severity.
    """
    severity: int = 0
    condition: int = 0
    hold_until: float = 0.0
    fade_seconds: float = 1.0
    HOLD_SECONDS = 1.0
    FADE_SECONDS = 1.0

    def strength(self, now):
        if not self.severity:return 0.0
        if self.condition or now <= self.hold_until:return 1.0
        fraction = max(0.0, min(1.0, (now-self.hold_until)/self.fade_seconds))
        return 1-fraction*fraction*(3-2*fraction)

    def trigger(self, now, severity=1, *, repeat=True):
        self.severity = max(severity, 2 if repeat and self.strength(now)>0 else 1)
        self.fade_seconds = self.FADE_SECONDS*self.severity
        self.hold_until = now+self.HOLD_SECONDS*self.severity

    def set_condition(self, level, now, *, magnitude=False):
        level = int(level)
        if level and not self.condition:
            self.trigger(now, level, repeat=not magnitude)
        elif level and magnitude:
            self.severity=level;self.fade_seconds=self.FADE_SECONDS*level
        elif level:
            self.severity=max(self.severity,level)
        elif self.condition:
            self.hold_until=now+self.HOLD_SECONDS*self.severity
        self.condition=level


class PlaybackWarningEvidence:
    """One detection policy for lamps/logs; no Qt, callbacks or file I/O."""
    COUNTERS=(("source_stalls","stalled"),("output_short_bytes","supply"),
        ("output_errors","supply"),("output_late_blocks","deadline"),
        ("source_late_fx_blocks","deadline"),("output_callback_gaps","dispatch"),
        ("output_progress_late","dispatch"),("output_asio_overload","deadline"),
        ("output_device_dropout","supply"),("output_device_overload","supply"),
        ("output_device_completion_late_blocks","deadline"),
        ("output_device_delivery_late_blocks","dispatch"),("note_transport_loss","transport"))
    DEVICE_FIELDS=tuple(field for field,_key in COUNTERS if field.startswith('output_'))

    def __init__(self):
        self.counters={};self.epoch=None

    def observe(self,values,load=None,*,clip=False):
        epoch=values.get('output_health_epoch');reset=epoch is not None and epoch!=self.epoch
        if reset:
            self.epoch=epoch
            for field in self.DEVICE_FIELDS:self.counters.pop(field,None)
        events={};details={}
        states={'stalled':int(bool(values.get('output_stalled'))),'clip':int(clip),
            'dispatch':int(values.get('output_progress_excess_ms',0)>0),
            'timing':int(values.get('note_timing_excess_ms',0)>0)}
        for field,key in self.COUNTERS:
            count=values.get(field+'_incidents',values.get(field))
            if count is None:continue
            previous=self.counters.get(field,0)
            if count>previous:
                delta=int(count-previous)
                events[key]=max(events.get(key,0),1 if field=='output_short_bytes' else min(2,delta))
                details[field]=delta
            self.counters[field]=count
        if processing_load_warning(load):states['overload']=2 if load>=80 else 1
        return events,states,details,reset


class PlaybackWarningLog:
    """Edge/interval logs from existing observations, never new audio reads.

    Native cumulative fields are distinct evidence, not additive independent
    dropouts. Repeated counters/entries coalesce for10s; conditions log on
    entry/escalation, not every repaint. Transport owns its detailed reporter.
    """
    METRICS={
        'dispatch':('output_progress_excess_ms','output_device_delivery_excess_ms','output_device_delivery_peak_ms'),
        'supply':('output_device_missing_buffers',),
        'deadline':('output_render_ms','output_device_completion_peak_ms','output_device_completion_budget_ms','block_ms'),
        'overload':('engine_load',), 'stalled':()}
    REPEAT_SECONDS=10.
    SUMMARY_METRICS={
        'dispatch':('output_progress_excess_ms','output_device_delivery_excess_ms'),
        'supply':('output_device_missing_buffers',),
        'deadline':('output_render_ms','output_device_completion_peak_ms','output_device_completion_budget_ms'),
        'overload':('engine_load',), 'stalled':()}

    def __init__(self,log):
        self.log=log;self.lock=threading.Lock();self.identity=None
        self.evidence=PlaybackWarningEvidence();self.states={};self.pending={};self.last={};self.last_severity={}
        self.clip=False;self.clip_identity=None
        self.pulses={key:PlaybackLampPulse() for key in (*self.METRICS,'clip')}

    @staticmethod
    def _field(key,value):
        unit=' ms' if key.endswith('_ms') else '%' if key=='engine_load' else ''
        number=f'{value:.2f}' if unit or isinstance(value,float) else str(value)
        return tr('warning_log.field',name=tr('warning_log.field_'+key),value=number,unit=unit)

    def observe(self,values,load=None,*,identity,now=None):
        now=time.monotonic() if now is None else now
        with self.lock:
            if identity!=self.identity:
                self._flush(now,force=True)
                self.identity=identity;self.evidence=PlaybackWarningEvidence();self.states={};self.last={};self.last_severity={}
                self.pulses={key:PlaybackLampPulse() for key in (*self.METRICS,'clip')}
            events,states,details,reset=self.evidence.observe(values,load)
            if reset:
                self._flush(now,force=True)
                # A device reset starts a new supply/timing observation epoch.
                self.states={};self.last={};self.last_severity={}
                self.pulses={key:PlaybackLampPulse() for key in (*self.METRICS,'clip')}
            measurements=dict(values,engine_load=load)
            evidence={}
            for field,delta in details.items():
                key=dict(PlaybackWarningEvidence.COUNTERS)[field]
                if key!='transport':evidence.setdefault(key,{})[field]=delta
            # Note timing remains measured/displayed,but never enters logs.
            for key in ('dispatch','supply','deadline','overload','stalled'):
                level=states.get(key,0);prior=self.states.get(key,0)
                pulse=self.pulses[key]
                pulse.set_condition(level,now,magnitude=key=='overload')
                if not level:
                    for _ in range(min(2,events.get(key,0))):pulse.trigger(now)
                if key in evidence or level>prior:
                    if key not in self.pending and now-self.last.get(key,now)>=self.REPEAT_SECONDS:
                        self.last.pop(key,None) # new isolated episode after quiet
                    item=self.pending.setdefault(key,dict(fields={},metrics={},backend=values.get('backend')))
                    item['severity']=max(item.get('severity',0),pulse.severity)
                    for field,delta in evidence.get(key,{}).items():item['fields'][field]=item['fields'].get(field,0)+delta
                    for field in self.METRICS[key]:
                        value=measurements.get(field)
                        if isinstance(value,(int,float)) and math.isfinite(value):
                            item['metrics'][field]=max(item['metrics'].get(field,value),value)
                    if level>prior:
                        item['level']=max(item.get('level',0),level)
                        item['entries']=item.get('entries',0)+1
                        if key=='overload' and level==2 and prior==1:item['urgent']=True
                elif level and key in self.pending:
                    for field in self.METRICS[key]:
                        value=measurements.get(field)
                        if isinstance(value,(int,float)) and math.isfinite(value):
                            item=self.pending[key]
                            item['metrics'][field]=max(item['metrics'].get(field,value),value)
                known={'stalled':'output_stalled','dispatch':'output_progress_excess_ms'}.get(key)
                if known and known in values:self.states[key]=level
                elif key=='overload' and isinstance(load,(int,float)) and math.isfinite(load) and load>=0:self.states[key]=level
            self._flush(now)

    def _flush(self,now,*,force=False):
        for key,item in tuple(self.pending.items()):
            if key=='timing':
                # Never emit a retained/legacy timing summary on shutdown,
                # identity change,device epoch or an unrelated clip flush.
                del self.pending[key];self.last.pop(key,None);self.states.pop(key,None);continue
            elapsed=now-self.last.get(key,now)
            repeat=key in self.last
            severity=item.get('severity',1)
            repeated=repeat and severity<=self.last_severity.get(key,0)
            if not force and repeat and elapsed<self.REPEAT_SECONDS and not item.get('urgent'):continue
            if key=='clip':
                self.log(log_entry(log_text('warning_log.clip_summary' if repeated else 'warning_log.clip',
                    peak=f'{item["peak"]:+.2f}',count=item['count'],seconds=elapsed),
                    'error' if severity>=2 else 'warning',repeat=repeated,indicator=key))
                self.last_severity[key]=severity
                self.last[key]=now;del self.pending[key];continue
            reason=[tr('warning_log.reason_'+field,count=count) for field,count in item['fields'].items()]
            if repeat and item.get('entries'):reason.append(tr('warning_log.entries',count=item['entries']))
            elif item.get('level'):reason.append(tr('warning_log.state_'+key))
            metrics=[self._field(field,value) for field,value in item['metrics'].items()
                if not repeat or field in self.SUMMARY_METRICS[key]]
            values=dict(name=tr('warning_lamps.'+key),seconds=elapsed,
                backend=tr('playback_info.backend_'+item['backend']) if item['backend'] in ('asio','wasapi_exclusive','shared') else tr('playback_info.unavailable'),reason='; '.join(reason))
            message_id='warning_log.summary' if repeated else 'warning_log.entry'
            all_metrics=[self._field(field,value) for field,value in item['metrics'].items()]
            message=log_text(message_id,**values,details='; '.join(metrics) or tr('warning_log.no_measurement'))
            expanded=tr(message_id,**values,details='; '.join(all_metrics) or tr('warning_log.no_measurement'))
            self.log(log_entry(message,'error' if severity>=2 else 'warning',repeat=repeated,detail=expanded,indicator=key))
            self.last_severity[key]=severity
            self.last[key]=now;del self.pending[key]

    def flush(self):
        with self.lock:self._flush(time.monotonic(),force=True)

    def observe_clip(self,levels,*,identity,now=None):
        now=time.monotonic() if now is None else now
        active=any(isinstance(value,(int,float)) and math.isfinite(value) and value>1 for value in levels)
        with self.lock:
            if identity!=self.clip_identity:
                if 'clip' in self.pending:self._flush(now,force=True)
                self.last.pop('clip',None);self.last_severity.pop('clip',None);self.clip_identity=identity;self.clip=False
                self.pulses['clip']=PlaybackLampPulse()
            self.pulses['clip'].set_condition(int(active),now)
            if active and not self.clip:
                if 'clip' not in self.pending and now-self.last.get('clip',now)>=self.REPEAT_SECONDS:self.last.pop('clip',None)
                peak=max(value for value in levels if math.isfinite(value))
                item=self.pending.setdefault('clip',dict(peak=20*math.log10(peak),count=0))
                item['severity']=max(item.get('severity',0),self.pulses['clip'].severity)
                item['peak']=max(item['peak'],20*math.log10(peak));item['count']+=1
            elif active and 'clip' in self.pending:
                self.pending['clip']['peak']=max(self.pending['clip']['peak'],20*math.log10(max(levels)))
            self.clip=active
            self._flush(now)


@dataclass(frozen=True)
class EngineObservation:
    stamp: tuple
    channels: tuple
    fields: tuple
    rows: object
    values: object
    status: EngineStatus
    captured_at: float
    query_seconds: float
    readbacks: object = None
    performance: object = None
    field_channels: tuple | None = None
    presentation_only: bool = False
    display_epoch: object = None


class AudioTelemetry:
    """Query BASS/BASSMIDI status and the engine's output snapshots."""

    # Only metadata validated for this live output can survive a delayed poll.
    # Everything else (note statistics, health samples, live queue) expires.
    _performance_metadata = frozenset((
        "backend", "source_rate", "output_rate", "source_channels", "output_channels",
        "source_bits", "source_encoding", "output_bits", "output_valid_bits", "output_encoding",
        "minimum_buffer_ms", "buffer_bytes", "playback_buffer_ms", "output_buffer_ms",
        "output_latency_ms", "output_latency_kind",
    ))

    @staticmethod
    def _performance_identity(stamp):
        # observation_stamp[7] is a MIDI-control revision, NOT output identity.
        # Keep handle/generation and runtime owner, including legacy test stamps.
        return stamp[:2] + stamp[6:7]

    def __init__(self, bass, engine):
        self.bass = bass
        self.engine = engine
        active_owner = getattr(engine, "_active_runtime", None)
        self.owner = active_owner() if callable(active_owner) else getattr(engine, "runtime", None)
        self.session = None
        self._condition = threading.Condition()
        self._lifecycle = threading.RLock()
        self._worker = None
        self._enabled = False
        self._stopping = False
        self._pending = None
        self._wanted = ((), ())
        self._wanted_field_channels = None
        self._pending_field_channels = None
        self._latest = None
        self._latest_display = None
        self._error = None
        self._last_failure = None
        self._performance_cache = None
        self._performance_stamp = None
        self._performance_due = 0.
        self._latest_performance = None
        self.warning_log=PlaybackWarningLog(self._append_warning)
        from midi_input_runtime import MidiHarmonyTracker
        self.chord_tracker=MidiHarmonyTracker()
        self._latest_chord=None
        from tempo_analysis import TempoAnalysisService
        self.tempo_analysis=TempoAnalysisService()
        self._tempo_error=None;self._tempo_failure_until=0.

    def _append_warning(self,message):
        append=getattr(self.owner,'append_log',None)
        if callable(append):append(message)

    def _owner_scope(self):
        from contextlib import nullcontext
        activate = getattr(self.owner, "activate", None)
        return activate() if callable(activate) else nullcontext()

    def _stamp(self):
        with self._owner_scope():
            return self.engine.observation_stamp()

    def _read_epoch(self):
        read = getattr(self.engine, "observation_read_epoch", None)
        with self._owner_scope():
            return read() if callable(read) else (None, False)

    def _display_stamp(self):
        read = getattr(self.engine, "observation_display_stamp", None)
        with self._owner_scope():
            return read() if callable(read) else self._stamp()[:7]

    def _display_epoch(self):
        read = getattr(self.engine, "observation_display_epoch", None)
        with self._owner_scope():
            return read() if callable(read) else self._read_epoch()

    @property
    def observation_enabled(self):
        return self._enabled

    def start_observation(self):
        """Enable latest-only reading after successful stream/output startup."""
        with self._lifecycle:
            with self._condition:
                self._enabled = True
                self._stopping = False
                self._last_failure = None
            self.request_observation()

    def request_observation(self, channels=(), fields=(), *, field_channels=None):
        if not self._enabled:
            return
        channels, fields = tuple(sorted(set(channels))), tuple(sorted(set(fields)))
        field_channels = None if field_channels is None else tuple(sorted(set(field_channels).intersection(channels)))
        stamp = self._display_stamp()
        if not stamp[0]:
            return
        with self._condition:
            if self._stopping or not self._enabled:
                return
            self._wanted = channels, fields
            self._wanted_field_channels = field_channels
            self._pending_field_channels = field_channels
            self._pending = stamp, channels, fields  # Replace; never enqueue frames.
            if self._worker is None:
                self._worker = threading.Thread(target=self._observe, name="ABMSynth-observation", context=Context())
                self._worker.start()
            self._condition.notify()

    def observation(self, channels=(), fields=(), *, field_channels=None):
        stamp = self._stamp()
        with self._condition:
            frame = self._latest
            wanted_field_channels = self._wanted_field_channels
        if (frame is None or frame.stamp != stamp or not set(channels).issubset(frame.channels)
                or not set(fields).issubset(frame.fields) or frame.field_channels != wanted_field_channels
                or fields and frame.field_channels is not None and not set(
                    channels if field_channels is None else field_channels).issubset(frame.field_channels)):
            return None
        return frame

    def display_observation(self, channels=(), fields=(), *, field_channels=None):
        """Latest completed live sweep; never a coherent-state/persistence API."""
        stamp = self._display_stamp()
        with self._condition:
            frame = self._latest_display
            wanted_field_channels = self._wanted_field_channels
        if (frame is None or frame.stamp[:7] != stamp or self._display_epoch() != frame.display_epoch
                or time.monotonic() - frame.captured_at > 1
                or not set(channels).issubset(frame.channels)
                or not set(fields).issubset(frame.fields)
                or frame.field_channels != wanted_field_channels
                or fields and frame.field_channels is not None and not set(
                    channels if field_channels is None else field_channels).issubset(frame.field_channels)):
            return None
        return frame

    def performance_observation(self):
        """Format/cadence survive control edits, but never another stream owner."""
        stamp = self._display_stamp()
        with self._condition:
            snapshot = self._latest_performance
        identity = self._performance_identity(stamp)
        if snapshot is None or not stamp[0] or snapshot[0] != identity:
            return {}
        values = dict(snapshot[2])
        if time.monotonic() - snapshot[1] > 1:
            # A slow observer must not erase unchanged source/device metadata,
            # but old samples must never masquerade as live jitter or health.
            values = {key: value for key, value in values.items() if key in self._performance_metadata}
            if values.get("output_latency_kind") == "queue":
                values.pop("output_latency_ms", None)
                values.pop("output_latency_kind", None)
        observed = values.get("observed_at")
        if observed is not None and time.perf_counter() - observed > 1:
            for key in ("block_ms", "jitter_mean_ms", "jitter_max_ms"):
                values.pop(key, None)
        return values

    def _collect_performance(self, stamp):
        identity = self._performance_identity(stamp)
        if identity != self._performance_stamp or time.monotonic() >= self._performance_due:
            try:
                with self._owner_scope():
                    timing = self.session.output_timing(getattr(self.session,"_asio_dll",None)) if self.session is not None and callable(getattr(self.session,"output_timing",None)) else {}
                    meter = getattr(self.owner,"output_meter",None)
                    cadence = meter.timing_statistics(include_deviation=False) if callable(getattr(meter,"timing_statistics",None)) else {}
                    sensor=getattr(self.owner,"playback_timing",None)
                    notes=sensor.snapshot() if sensor is not None else {}
                    health=self.session.output_health() if self.session is not None and callable(getattr(self.session,"output_health",None)) else {}
                    read_mode=getattr(self.engine,'get_midi_system_mode',None)
                    mode=read_mode() if callable(read_mode) else None
                    mode=mode if type(mode) is int and 0<=mode<=5 else None
                    values = MappingProxyType({**timing,**cadence,**notes,**health,'midi_system_mode':mode})
            except Exception:
                self._performance_stamp = None
                with self._condition:
                    self._latest_performance = None
                raise
            current = self._stamp()
            if self._performance_identity(current) != identity:
                return
            self._performance_stamp = identity
            self._performance_due = time.monotonic() + .25
            self._performance_cache = values
            with self._condition:
                self._latest_performance = identity, time.monotonic(), values

    def take_observation_error(self):
        with self._condition:
            error, self._error = self._error, None
        return error

    def _collect_chord(self,stamp,*,available=True):
        """Existing observation worker only;no new callback/thread/PCM read."""
        identity=self._performance_identity(stamp);now=time.monotonic()
        if not available:
            self._publish_chord(None,identity,now,invalidated=True);return
        read=getattr(self.engine,'get_midi_chord_events',None)
        if not callable(read):return
        epoch=self._display_epoch()
        if epoch[1]:
            self._publish_chord(None,identity,now,invalidated=True);return
        adapter=getattr(self.session,'multicore_synthesis',None)
        streams=tuple(adapter.streams) if isinstance(adapter,NativeMulticoreSynthesis) else ()
        cursor=self.chord_tracker.cursor if self.chord_tracker.identity==identity else 0
        with self._owner_scope():batch,excluded=read(cursor,synthesis_streams=streams)
        invalidated=self._performance_identity(self._stamp())!=identity or self._display_epoch()!=epoch
        if invalidated:batch=None
        self._publish_chord(batch,identity,time.monotonic(),excluded=excluded,invalidated=invalidated)

    def _publish_chord(self,batch,identity,now,*,excluded=(),invalidated=False):
        reading=self.chord_tracker.observe_events(batch,identity=identity,excluded=excluded,now_ns=time.perf_counter_ns(),invalidated=invalidated)
        with self._condition:self._latest_chord=(identity,now,reading)
        self.tempo_analysis.observe_notes(None if invalidated else batch,time.perf_counter())

    def _collect_tempo(self,stamp):
        if time.monotonic()<self._tempo_failure_until:return
        meter=getattr(self.owner,'output_meter',None)
        snapshot=getattr(meter,'tempo_snapshot',None)
        if not callable(snapshot):return
        try:
            self.tempo_analysis.submit(snapshot(),self._performance_identity(stamp))
            self._tempo_error=None
        except Exception as error:
            self.tempo_analysis.observe_notes(None,time.perf_counter())
            self._tempo_failure_until=time.monotonic()+5
            if str(error)!=self._tempo_error:
                self._append_warning(log_text('tempo_display.unavailable',error=error));self._tempo_error=str(error)

    def tempo_observation(self):
        stamp=self._display_stamp()
        if not stamp[0] or self._display_epoch()[1]:return None
        return self.tempo_analysis.reading(self._performance_identity(stamp))

    def tempo_diagnostic(self):
        return self.tempo_analysis.diagnostic(self._performance_identity(self._display_stamp()))

    def chord_observation(self):
        """GUI reads immutable symbols only;never performs native note reads."""
        with self._condition:snapshot=self._latest_chord
        stamp=self._display_stamp()
        if (snapshot is None or not stamp[0] or snapshot[0]!=self._performance_identity(stamp)
                or (time.monotonic()-snapshot[1])*1e9>self.chord_tracker.OBSERVATION_GRACE_NS or self._display_epoch()[1]):return None
        return snapshot[2]

    def _observe(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopping or self._pending is not None)
                if self._stopping:
                    return
                request, self._pending = self._pending, None
                field_channels = self._pending_field_channels
            stamp, channels, fields = request
            current = self._stamp()
            if self._performance_identity(stamp) != self._performance_identity(current):
                continue
            stamp = current
            try:
                started = time.monotonic()
                display_epoch = self._display_epoch()
                display_stamp = self._display_stamp()
                epoch = self._read_epoch()
                # bass.h DWORD/BOOL ABI; bass.md BASS_SetDevice is thread-local.
                # The control owner joins this worker BEFORE quiesce/Free.
                device = self.bass.BASS_ChannelGetDevice(stamp[0])
                if device == 0xFFFFFFFF or not self.bass.BASS_SetDevice(device):
                    raise RuntimeError(tr("runtime.observation_device_unavailable"))
                with self._owner_scope():
                    status = self._read_engine_status()
                self._collect_performance(stamp)
                if self._performance_cache is not None:
                    self.warning_log.observe(self._performance_cache,status.load_percent,
                        identity=self._performance_identity(stamp))
                if display_epoch[1]:
                    self._collect_chord(stamp,available=False)
                    continue  # Never present a partial manual transaction.
                self._collect_chord(stamp)
                self._collect_tempo(stamp)
                with self._owner_scope():
                    qualified=getattr(self.engine,'channel_control_display_readback',None) or getattr(self.engine,'channel_control_readback',None)
                    scoped = getattr(self.engine, 'channel_monitor_snapshot_scoped', None)
                    if channels and callable(scoped):
                        rows = MappingProxyType({channel: MappingProxyType(dict(row)) for channel, row in
                            scoped(channels, **({'with_readbacks':True} if qualified is not None else {})).items()})
                    else:
                        # Exact legacy/test adapter: its original tuple remains
                        # globally indexed. Active runtime uses explicit sparse
                        # channel identities (e.g. port7 channel127), not offsets.
                        rows = tuple(MappingProxyType(dict(row)) for row in
                            self.engine.channel_monitor_snapshot(**({'with_readbacks':True} if qualified is not None else {}))) if channels else ()
                    # Monitor rows cover visible channels; the inspector's
                    # additional fields belong only to its selected channels.
                    detail_channels = channels if field_channels is None else field_channels
                    voice_reader=getattr(self.engine,'get_channel_voice_readbacks',None)
                    voice_rows={}
                    if 'active_voices' in fields and callable(voice_reader):
                        adapter=getattr(self.session,'multicore_synthesis',None)
                        streams=tuple(adapter.streams) if isinstance(adapter,NativeMulticoreSynthesis) else ()
                        voice_rows=voice_reader(detail_channels,synthesis_streams=streams)
                    readbacks=MappingProxyType({(channel,field):voice_rows[channel] if field=='active_voices' and channel in voice_rows else rows[channel].get('_readbacks',{}).get(field) or qualified(channel,field)
                        for channel in detail_channels for field in fields}) if qualified is not None else None
                    values = MappingProxyType({key:result.value for key,result in readbacks.items()}) if readbacks is not None else MappingProxyType({(channel, field): rows[channel][field]
                        if field in rows[channel] else self.engine.channel_control_value(channel, field)
                        for channel in detail_channels for field in fields})
                frame = EngineObservation(stamp, channels, fields, rows, values, status,
                                          time.monotonic(), time.monotonic() - started,readbacks,self._performance_cache,field_channels,True,display_epoch)
                strict_epoch = self._read_epoch()
            except Exception as exc:
                self.chord_tracker.reset()
                self.tempo_analysis.observe_notes(None,time.perf_counter())
                with self._condition:
                    self._latest_chord=None
                    self._latest = None
                    self._latest_display = None
                    error = str(exc)
                    if error != self._last_failure:
                        self._error = error
                        self._last_failure = error
                continue
            with self._condition:
                if (not self._stopping and display_stamp == self._display_stamp()
                        and self._display_epoch() == display_epoch
                        and self._wanted == (channels, fields) and self._wanted_field_channels == field_channels):
                    self._latest_display = frame
                    if (not epoch[1] and strict_epoch == epoch and stamp == self._stamp() and self._read_epoch() == epoch
                            and not any(result.source in ('pending', 'native_pending_metadata') for result in (readbacks or {}).values())):
                        from dataclasses import replace
                        self._latest = replace(frame, presentation_only=False)
                    self._last_failure = None

    def pause_observation(self):
        """Control-side barrier, not an audio callback or paint-time join."""
        with self._lifecycle:
            with self._condition:
                self._enabled = False
                self._stopping = True
                self._pending = None
                worker = self._worker
                self._condition.notify_all()
            if worker is not None:
                worker.join()
            self.warning_log.flush()
            with self._condition:
                self._worker = None
                self._latest = None
                self._latest_display = None
                self._wanted = ((), ())
                self._wanted_field_channels = self._pending_field_channels = None
                self._error = None
                self._last_failure = None
                self._performance_stamp = None
                self._performance_cache = None
                self._latest_performance = None
                self.chord_tracker.reset();self._latest_chord=None
            self.tempo_analysis.close()

    def engine_status(self) -> EngineStatus:
        if self._enabled:
            with self._condition:
                channels, fields = self._wanted
                field_channels = self._wanted_field_channels
                frame = self._latest_display
            self.request_observation(channels, fields, field_channels=field_channels)
            if frame is not None and frame.stamp[:2] != self._display_stamp()[:2]:
                frame = None
            return frame.status if frame is not None else EngineStatus(None, None)
        return self._read_engine_status()

    def _read_engine_status(self) -> EngineStatus:
        stream = self.owner.stream_handle
        if not stream:
            return EngineStatus(None, 0.0)
        active = ctypes.c_float()
        # bass.h BASS_ChannelGetAttribute(float*), bassmidi.h
        # BASS_ATTRIB_MIDI_VOICES_ACTIVE=0x12004. Failure leaves the displayed
        # voice count unknown; it must not be converted to a false zero.
        voices = int(active.value) if self.bass.BASS_ChannelGetAttribute(
            stream, BASS_ATTRIB_MIDI_VOICES_ACTIVE, ctypes.byref(active),
        ) else None
        # Native backend processing utilization, NOT process CPU or a
        # per-block deadline measurement. Device contexts are thread-local.
        return EngineStatus(voices, _backend_processing_load(self.bass, self.session))

    def peak_levels(self):
        levels=self.engine.get_stream_peak_levels()
        # Exactly the meter's consume-on-read sample. Never consume peaks a
        # second time to log and accidentally hide clipping from the display.
        identity=(getattr(self.owner,'stream_handle',0),getattr(self.owner,'stream_generation',0))
        self.warning_log.observe_clip(levels,identity=identity)
        return levels

    def output_visual_samples(self):
        return self.engine.get_output_visual_samples()


class NativeRenderProbe:
    """Opt-in native decode timing; no QWidget/backend handle or physical claim.

    Serialized caller owns GetData and the snapshot. Its data pointer is borrowed
    only for that call. BASS's DLL/function remains retained until probe close.
    DSP-chain plus synthesis time is measured inside C, not Python return jitter.
    """
    def __init__(self,bass,native_dll=None):
        import threading
        self.bass=bass
        self.lock=threading.RLock()
        self.dll=ctypes.CDLL(str(Path(native_dll) if native_dll else Path(__file__).with_name("abmsynth_dsp.dll")))
        self.dll.render_probe_version.argtypes = []
        self.dll.render_probe_version.restype = ctypes.c_uint
        if self.dll.render_probe_version() != 1:
            raise RuntimeError("unsupported native render probe ABI")
        for name,arguments,result in (
            ("render_probe_create",[ctypes.c_void_p],ctypes.c_void_p),
            ("render_probe_destroy",[ctypes.c_void_p],None),
            ("render_probe_get_data",[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint],ctypes.c_uint),
            ("render_probe_snapshot",[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.POINTER(ctypes.c_uint64)],ctypes.c_uint),
        ):
            function=getattr(self.dll,name);function.argtypes,function.restype=arguments,result
        # Installed bass.h DWORD BASSDEF(BASS_ChannelGetData)(DWORD,void*,DWORD).
        self.pointer=self.dll.render_probe_create(ctypes.cast(bass.BASS_ChannelGetData,ctypes.c_void_p))
        if not self.pointer:raise MemoryError("native render probe")

    def get_data(self,stream,buffer,length):
        with self.lock:
            if not self.pointer:raise RuntimeError("render probe closed")
            return self.dll.render_probe_get_data(self.pointer,stream,buffer,length)

    def snapshot(self):
        with self.lock:
            values=(ctypes.c_uint64*4096)();frequency=ctypes.c_uint64()
            if not self.pointer:return ()
            count=self.dll.render_probe_snapshot(self.pointer,values,ctypes.byref(frequency))
            return tuple(value/frequency.value for value in values[:count])

    def close(self):
        with self.lock:
            if self.pointer:self.dll.render_probe_destroy(self.pointer);self.pointer=None


class NativeMidiProcessedProbe:
    """Opt-in native processed-event QPC, distinct from producer/GUI timestamps.

    bass.h SYNCPROC and bassmidi.h BASS_SYNC_MIDI_EVENT|MIXTIME supply raw
    DWORD data (channel in HIWORD). No Python callback is installed. The
    caller must close this borrower BEFORE freeing its stream; failed removal
    retains the native data/DLL/sync for retry. It never changes MIDI/audio.
    """
    def __init__(self,bass,stream,event,native_dll=None):
        from bass_bindings import SYNCPROC,NativeChannelTransaction
        self.bass,self.stream,self.event=bass,int(stream),int(event)
        self.lock=threading.RLock();self.sync=0;self._unlock_pending=False;self._lock_thread=None
        self.dll=ctypes.CDLL(str(Path(native_dll) if native_dll else Path(__file__).with_name('abmsynth_dsp.dll')))
        self._transaction=NativeChannelTransaction(bass,native_dll)
        for name,args,result in (
            ('midi_processed_probe_version',[],ctypes.c_uint),
            ('midi_processed_probe_create',[],ctypes.c_void_p),
            ('midi_processed_probe_destroy',[ctypes.c_void_p],None),
            ('midi_processed_probe_proc',[],ctypes.c_void_p),
            ('midi_processed_probe_snapshot',[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint,
                ctypes.POINTER(ctypes.c_uint64),ctypes.POINTER(ctypes.c_uint64)],ctypes.c_uint)):
            function=getattr(self.dll,name);function.argtypes,function.restype=args,result
        if self.dll.midi_processed_probe_version()!=1:raise RuntimeError('processed MIDI probe ABI')
        self.pointer=self.dll.midi_processed_probe_create()
        if not self.pointer:raise MemoryError('processed MIDI probe')
        self.callback=ctypes.cast(ctypes.c_void_p(self.dll.midi_processed_probe_proc()),SYNCPROC)
        # This C function pointer is not a Python callable/wrapper callback.
        self.sync=bass.BASS_ChannelSetSync(self.stream,0x40010004,self.event,self.callback,self.pointer)
        if not self.sync:
            error=int(bass.BASS_ErrorGetCode())
            self.dll.midi_processed_probe_destroy(self.pointer);self.pointer=None
            raise RuntimeError(f'processed MIDI sync: {error}')

    def snapshot(self):
        with self.lock:
            if not self.pointer:return dict(total=0,records=())
            buffer=(ctypes.c_uint64*(4096*5))();frequency=ctypes.c_uint64();total=ctypes.c_uint64()
            count=self.dll.midi_processed_probe_snapshot(self.pointer,buffer,4096,
                ctypes.byref(frequency),ctypes.byref(total))
            rows=tuple(dict(sequence=buffer[5*i],processed=buffer[5*i+1]/frequency.value,
                stream=buffer[5*i+2],raw_data=buffer[5*i+3],thread=buffer[5*i+4],event=self.event)
                for i in range(count))
            return dict(total=total.value,records=rows,capacity=4096,
                overwritten=max(0,total.value-4096))

    def close(self):
        with self.lock:
            steps=((1,self.bass.BASS_ChannelRemoveSync,self.stream,self.sync),) if self.sync else ()
            result=self._transaction.run(self.stream,steps)
            if result.completed:self.sync=0
            self._unlock_pending=self._transaction.pending is not None
            self._lock_thread=self._transaction.pending[1] if self._unlock_pending else None
            if not result.success:raise RuntimeError('processed MIDI probe transaction: '+str(result))
            if self.pointer:
                self.dll.midi_processed_probe_destroy(self.pointer);self.pointer=None


class NativeOutputProbe:
    """Production/diagnostic C callbacks, no Python/QWidget work on render.

    Source must be FLOAT|DECODE. Installed ASIO/Wasapi/STREAMPROC signatures
    feed GetData exactly as documented. Stop/free borrowers before destruction.
    Callback jitter/short supply are observable; physical DAC underruns are not.
    """
    def __init__(self,bass,source,native_dll=None,*,gain=1.):
        self.bass,self.source=bass,int(source)
        self.dll=ctypes.CDLL(str(Path(native_dll or os.environ.get("ABMSYNTH_OBSERVATION_DLL",Path(__file__).with_name('abmsynth_dsp.dll')))))
        self.dll.output_probe_version.restype=ctypes.c_uint
        if self.dll.output_probe_version()!=1:raise RuntimeError('output probe ABI')
        self.dll.output_probe_create.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint,ctypes.c_double]
        self.dll.output_probe_create.restype=ctypes.c_void_p
        self.dll.output_probe_destroy.argtypes=[ctypes.c_void_p];self.dll.output_probe_destroy.restype=None
        self.dll.output_probe_snapshot.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint64),ctypes.POINTER(ctypes.c_uint64)]
        self.dll.output_probe_snapshot.restype=ctypes.c_uint
        self.pointer=self.dll.output_probe_create(ctypes.cast(bass.BASS_ChannelGetData,ctypes.c_void_p),
            ctypes.cast(bass.BASS_ErrorGetCode,ctypes.c_void_p),self.source,gain)
        if not self.pointer:raise MemoryError('output probe')
        self.borrowers=set()
        # Only control/readback threads take this lock. Native audio callbacks
        # use the preallocated stamped ring and never enter Python or wait here.
        self.lock=threading.RLock()

    def callback(self,backend):
        with self.lock:
            if not self.pointer:raise RuntimeError('output probe closed')
            return ctypes.cast(getattr(self.dll,'output_probe_'+backend),ctypes.c_void_p)

    def enable_observation(self, sample_rate):
        # Additive own-DLL ABI; source rate matches the callback's FLOAT stereo
        # channel, including the source rate before BASSASIO resampling.
        self.dll.output_probe_observe.argtypes=[ctypes.c_void_p,ctypes.c_double]
        self.dll.output_probe_observe.restype=None
        self.dll.output_probe_health.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64)]
        self.dll.output_probe_health.restype=None
        self.dll.output_probe_observe(self.pointer,sample_rate)

    def health(self):
        with self.lock:
            if not self.pointer:return {}
            values=(ctypes.c_uint64*9)()
            self.dll.output_probe_health(self.pointer,values)
            result=dict(output_blocks=int(values[0]),output_short_bytes=int(values[1]),output_errors=int(values[2]),
                output_late_blocks=int(values[3]),output_callback_gaps=int(values[4]),
                output_render_ms=(values[6]-values[5])/values[8]*1000 if values[8] else None)
            if getattr(self,"_progress_observed",False):
                count=ctypes.c_uint64();current=ctypes.c_double();peak=ctypes.c_double()
                self.dll.output_probe_progress_health(self.pointer,ctypes.byref(count),ctypes.byref(current),ctypes.byref(peak))
                result.update(output_progress_late_incidents=count.value,
                    output_progress_excess_ms=current.value,output_progress_peak_ms=peak.value)
            if getattr(self,"_asio_processing_observed",False):
                count=ctypes.c_uint64();peak=ctypes.c_double()
                self.dll.output_probe_asio_metrics(self.pointer,ctypes.byref(count),ctypes.byref(peak))
                result.update(output_asio_overload_incidents=count.value,output_asio_processing_peak=peak.value)
            return result

    def enable_progress_observation(self):
        """Before callback admission only: ASIO source-progress risk, not DAC loss."""
        if not all(hasattr(self.dll,name) for name in (
                "output_probe_progress_enable","output_probe_progress_health")):
            return False  # Old image never invents a healthy zero.
        self.dll.output_probe_progress_enable.argtypes=[ctypes.c_void_p,ctypes.c_int]
        self.dll.output_probe_progress_enable.restype=None
        self.dll.output_probe_progress_health.argtypes=[ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint64),ctypes.POINTER(ctypes.c_double),ctypes.POINTER(ctypes.c_double)]
        self.dll.output_probe_progress_health.restype=None
        self.dll.output_probe_progress_enable(self.pointer,1)
        self._progress_observed=True
        return True

    def asio_notification_callback(self):
        if not all(hasattr(self.dll,name) for name in (
                "output_probe_asio_notify","output_probe_asio_notices")):
            return None
        self.dll.output_probe_asio_notices.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64)]
        self.dll.output_probe_asio_notices.restype=None
        return ctypes.cast(self.dll.output_probe_asio_notify,ctypes.c_void_p)

    def observe_asio_processing(self,dll):
        if not all(hasattr(self.dll,name) for name in ("output_probe_asio_cpu","output_probe_asio_metrics")):
            return False
        self.dll.output_probe_asio_cpu.argtypes=[ctypes.c_void_p,ctypes.c_void_p]
        self.dll.output_probe_asio_cpu.restype=None
        self.dll.output_probe_asio_metrics.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint64),ctypes.POINTER(ctypes.c_double)]
        self.dll.output_probe_asio_metrics.restype=None
        self.dll.output_probe_asio_cpu(self.pointer,ctypes.cast(dll.BASS_ASIO_GetCPU,ctypes.c_void_p))
        self._asio_processing_observed=True
        return True

    def asio_notifications(self):
        with self.lock:
            if not self.pointer or not hasattr(self.dll,"output_probe_asio_notices"):
                return (0,0)
            values=(ctypes.c_uint64*2)()
            self.dll.output_probe_asio_notices(self.pointer,values)
            return tuple(map(int,values))

    def acquire(self,owner):
        with self.lock:
            if not self.pointer:raise RuntimeError('output probe closed')
            self.borrowers.add(owner)

    def release(self,owner):
        with self.lock:
            self.borrowers.discard(owner)

    def snapshot(self):
        with self.lock:
            if not self.pointer:return dict(total=0,records=())
            buffer=(ctypes.c_uint64*(4096*6))();frequency=ctypes.c_uint64();total=ctypes.c_uint64()
            count=self.dll.output_probe_snapshot(self.pointer,buffer,4096,ctypes.byref(frequency),ctypes.byref(total))
            rows=tuple(dict(start=buffer[6*i]/frequency.value,seconds=(buffer[6*i+1]-buffer[6*i])/frequency.value,
                requested=buffer[6*i+2],returned=buffer[6*i+3],error=buffer[6*i+4],thread=buffer[6*i+5]) for i in range(count))
            return dict(total=total.value,records=rows)

    def close(self):
        with self.lock:
            if self.borrowers:raise RuntimeError('output probe still borrowed')
            if self.pointer:self.dll.output_probe_destroy(self.pointer);self.pointer=None
