"""Windows process integration: DPI, priority, and user login startup.

These small platform operations share a Windows-only dependency boundary.
Importing this module has no effects; callers choose when to apply each one.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path


PRIORITY_CLASSES = {
    "idle": 0x00000040,
    "below_normal": 0x00004000,
    "normal": 0x00000020,
    "above_normal": 0x00008000,
    "high": 0x00000080,
    "realtime": 0x00000100,
}

RESTART_PARENT_PID_ENV = "ABMSYNTH_RESTART_PARENT_PID"

_taskbar_identity_set = False


class _ResizePaintAPI:
    """WinUser/WinGDI ABI;stock DC_BRUSH is borrowed,never deleted."""
    def __init__(self):
        from ctypes import wintypes
        self.rect_type=wintypes.RECT;self.point_type=wintypes.POINT
        user=ctypes.WinDLL('user32',use_last_error=True)
        gdi=ctypes.WinDLL('gdi32',use_last_error=True)
        def bind(library,name,result,*args):
            function=getattr(library,name);function.restype=result;function.argtypes=list(args)
            return function
        handle=ctypes.c_void_p;integer=ctypes.c_int;colour=ctypes.c_uint32
        self.client_rect=bind(user,'GetClientRect',integer,handle,ctypes.POINTER(wintypes.RECT))
        self.fill_rect=bind(user,'FillRect',integer,handle,ctypes.POINTER(wintypes.RECT),handle)
        self.save_dc=bind(gdi,'SaveDC',integer,handle)
        self.restore_dc=bind(gdi,'RestoreDC',integer,handle,integer)
        self.flush=bind(gdi,'GdiFlush',integer)
        self.brush_colour=bind(gdi,'SetDCBrushColor',colour,handle,colour)
        self.map_mode=bind(gdi,'GetMapMode',integer,handle)
        self.graphics_mode=bind(gdi,'GetGraphicsMode',integer,handle)
        self.layout=bind(gdi,'GetLayout',colour,handle)
        self.window_origin=bind(gdi,'GetWindowOrgEx',integer,handle,ctypes.POINTER(wintypes.POINT))
        self.viewport_origin=bind(gdi,'GetViewportOrgEx',integer,handle,ctypes.POINTER(wintypes.POINT))
        stock=bind(gdi,'GetStockObject',handle,integer)
        self.brush=stock(18)  # WinGDI.h DC_BRUSH;system-owned for process lifetime.

    def client_size(self,hwnd):
        rect=self.rect_type()
        if not self.client_rect(hwnd,ctypes.byref(rect)):return None
        size=rect.right-rect.left,rect.bottom-rect.top
        return size if min(size)>0 else None

    def fill(self,hdc,rectangles,colour):
        if not hdc or not rectangles or not self.brush:return 0
        # Native client coordinates are pixels. Decline an unusual DC rather
        # than paint a translated/scaled strip over existing window content.
        if self.map_mode(hdc)!=1 or self.graphics_mode(hdc)!=1 or self.layout(hdc)!=0:return 0  # MM_TEXT/GM_COMPATIBLE/LTR.
        window=self.point_type();viewport=self.point_type()
        if not self.window_origin(hdc,ctypes.byref(window)) or not self.viewport_origin(hdc,ctypes.byref(viewport)):return 0
        if window.x or window.y or viewport.x or viewport.y:return 0
        saved=self.save_dc(hdc)
        if not saved:return 0
        try:
            if self.brush_colour(hdc,colour)==0xffffffff:return 0  # CLR_INVALID.
            count=0
            for bounds in rectangles:
                rect=self.rect_type(*bounds)
                count+=bool(self.fill_rect(hdc,ctypes.byref(rect),self.brush))
            if count:self.flush()  # Submit this thread's GDI batch before Qt's CPU paint.
            return count
        finally:self.restore_dc(hdc,saved)


_resize_paint_api=None


class NativeResizeBackdrop:
    """Only colour unpainted growth strips during an owned modal resize.

    Qt still receives every native message and owns the final paint. No DC,
    brush,HWND subclass,timer or audio resource belongs to this state owner.
    The caller supplies current opaque-window eligibility/theme colour.
    """
    MESSAGES=frozenset((0x0231,0x0005,0x0014,0x0232,
        0x001f,0x0082,0x02e0))

    def __init__(self,api=None):
        global _resize_paint_api
        if api is None and os.name=='nt':
            if _resize_paint_api is None:_resize_paint_api=_ResizePaintAPI()
            api=_resize_paint_api
        self._api=api;self._colour=None;self.cancel()

    def set_background(self,colour):
        self._colour=int(colour) if colour is not None else None
        if self._colour is None:self.cancel()

    def cancel(self):
        self._modal=False;self._resizing=False;self._valid=None

    @staticmethod
    def growth_rects(valid,size):
        """Half-open physical rectangles;never include previously valid pixels."""
        old_width,old_height=valid;width,height=size;result=[]
        if width>old_width:result.append((old_width,0,width,height))
        if height>old_height:result.append((0,old_height,min(old_width,width),height))
        return tuple(rect for rect in result if rect[0]<rect[2] and rect[1]<rect[3])

    def message(self,hwnd,message,wparam=0):
        # WinUser.h ENTER/EXIT/CANCEL/N C DESTROY/DPICHANGED lifetime boundaries.
        if message in (0x0232,0x001f,0x0082,0x02e0):self.cancel();return 0
        if self._api is None or self._colour is None:return 0
        if message==0x0231:
            self.cancel();self._valid=self._api.client_size(hwnd)
            self._modal=self._valid is not None
            return 0
        if not self._modal:return 0
        if message==0x0005 and wparam!=0:self.cancel();return 0  # SIZE_MINIMIZED/MAXIMIZED.
        if message not in (0x0005,0x000f,0x0014):return 0
        size=self._api.client_size(hwnd)
        if size is None:self.cancel();return 0
        if message==0x0005:
            # QPA excludes sent WM_SIZING from application native filters.
            # ENTER plus an actual SIZE change identifies resizing reliably;
            # a titlebar move leaves client size unchanged and never fills.
            if size!=self._valid:self._resizing=True
            # Shrink can discard old window pixels even before another paint.
            self._valid=tuple(min(a,b) for a,b in zip(self._valid,size))
        elif message==0x000f and self._resizing:
            # Qt's native WM_PAINT synchronously exposes/flushes before return.
            # This is a conservative next-message extent,not a scanout ACK.
            self._valid=size
        elif message==0x0014 and self._resizing and wparam:
            return self._api.fill(wparam,self.growth_rects(self._valid,size),self._colour)
        return 0


def configure_taskbar_identity():
    """Shellapi.h:one process AppUserModelID groups all ABMSynth windows."""
    global _taskbar_identity_set
    if os.name != 'nt' or _taskbar_identity_set:
        return
    from product import APP_USER_MODEL_ID
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    setter = shell.SetCurrentProcessExplicitAppUserModelID
    setter.argtypes = [ctypes.c_wchar_p]
    setter.restype = ctypes.c_long
    result = setter(APP_USER_MODEL_ID)
    if result < 0:
        from i18n import tr
        raise OSError(result, tr('runtime.taskbar_group_failed'))
    _taskbar_identity_set = True


def expose_taskbar_window(hwnd):
    """WinUser.h:expose this process's owned HWND without changing GW_OWNER.

    WS_EX_APPWINDOW affects Shell discovery,not Qt modality or ownership.
    Call before the native ShowWindow boundary;never hide/show to force it.
    """
    if os.name != 'nt':
        return
    configure_taskbar_identity()
    user = ctypes.WinDLL('user32', use_last_error=True)
    pid = ctypes.c_ulong()
    query = user.GetWindowThreadProcessId
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    query.restype = ctypes.c_ulong
    if not query(hwnd, ctypes.byref(pid)) or pid.value != os.getpid():
        from i18n import tr
        raise OSError(tr('runtime.taskbar_window_invalid'))
    suffix = 'LongPtrW' if ctypes.sizeof(ctypes.c_void_p) == 8 else 'LongW'
    getter = getattr(user, 'GetWindow'+suffix)
    setter = getattr(user, 'SetWindow'+suffix)
    getter.argtypes = [ctypes.c_void_p, ctypes.c_int]
    getter.restype = ctypes.c_ssize_t
    setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    setter.restype = ctypes.c_ssize_t
    ctypes.set_last_error(0)
    previous = getter(hwnd, -20)  # GWL_EXSTYLE.
    if not previous and ctypes.get_last_error():
        raise ctypes.WinError(ctypes.get_last_error())
    wanted = (previous | 0x00040000) & ~0x00000080  # APPWINDOW,not TOOLWINDOW.
    if wanted != previous:
        ctypes.set_last_error(0)
        if not setter(hwnd, -20, wanted) and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())

# Read-only Core Audio metadata. ABI: Windows SDK mmdeviceapi.h,
# propsys.h/propidl.h and mmreg.h. No audio client initialization or writes.
def parse_endpoint_wave_format(data):
    import struct
    import uuid
    if len(data) < 18:
        return {}
    tag, channels, rate, _average, alignment, bits = struct.unpack_from("<HHIIHH", data)
    valid, encoding = bits, {1: "PCM", 3: "Float"}.get(tag)
    if tag == 0xfffe:
        extension_size = struct.unpack_from("<H", data, 16)[0]
        if len(data) < 40 or extension_size < 22 or len(data) < 18 + extension_size:
            return {}
        valid = struct.unpack_from("<H", data, 18)[0] or bits
        subtype = uuid.UUID(bytes_le=bytes(data[24:40]))
        encoding = {uuid.UUID("00000001-0000-0010-8000-00aa00389b71"): "PCM",
                    uuid.UUID("00000003-0000-0010-8000-00aa00389b71"): "Float"}.get(subtype)
    if not encoding or not channels or not rate or not 0 < valid <= bits or alignment < channels * ((bits+7)//8):
        return {}
    return dict(output_bits=bits, output_valid_bits=valid, output_encoding=encoding,
                configured_rate=rate, configured_channels=channels)


def read_endpoint_device_format(endpoint_id=None):
    """STGM_READ DeviceFormat: Windows shared-mode configured endpoint format."""
    if os.name != "nt":
        return {}
    import uuid
    class Guid(ctypes.Structure):
        _fields_ = [("first", ctypes.c_uint32), ("second", ctypes.c_uint16),
                    ("third", ctypes.c_uint16), ("last", ctypes.c_ubyte * 8)]
    class Key(ctypes.Structure):
        _fields_ = [("guid", Guid), ("pid", ctypes.c_uint32)]
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.c_void_p)]
    class Value(ctypes.Union):
        _fields_ = [("blob", Blob), ("storage", ctypes.c_void_p * 2)]
    class Variant(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_uint16), ("reserved", ctypes.c_uint16 * 3), ("value", Value)]
    def guid(value):
        return Guid.from_buffer_copy(uuid.UUID(value).bytes_le)
    def invoke(pointer, slot, result, arguments, *values):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *arguments)(table[slot])(pointer, *values)
    ole = ctypes.WinDLL("ole32")
    ole.CoInitializeEx.argtypes, ole.CoInitializeEx.restype = [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_long
    ole.CoUninitialize.argtypes, ole.CoUninitialize.restype = [], None
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(Guid), ctypes.c_void_p, ctypes.c_uint32,
                                     ctypes.POINTER(Guid), ctypes.POINTER(ctypes.c_void_p)]
    ole.CoCreateInstance.restype = ctypes.c_long
    ole.PropVariantClear.argtypes, ole.PropVariantClear.restype = [ctypes.POINTER(Variant)], ctypes.c_long
    initialized = ole.CoInitializeEx(None, 0)  # MTA on the metadata worker.
    if initialized < 0 and (initialized & 0xffffffff) != 0x80010106:
        return {}
    enumerator, device, properties = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    value = Variant()
    try:
        cls = guid("bcde0395-e52f-467c-8e3d-c4579291692e")
        iid = guid("a95664d2-9614-4f35-a746-de8db63617e6")
        if ole.CoCreateInstance(ctypes.byref(cls), None, 1, ctypes.byref(iid), ctypes.byref(enumerator)) < 0:
            return {}
        if endpoint_id:
            result = invoke(enumerator, 5, ctypes.c_long, [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)],
                            endpoint_id, ctypes.byref(device))
        else:
            result = invoke(enumerator, 4, ctypes.c_long, [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
                            0, 1, ctypes.byref(device))  # eRender/eMultimedia default.
        if result < 0:
            return {}
        if invoke(device, 4, ctypes.c_long, [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
                  0, ctypes.byref(properties)) < 0:
            return {}
        key = Key(guid("f19f064d-082c-4e27-bc73-6882a1bb8e4c"), 0)
        if invoke(properties, 5, ctypes.c_long, [ctypes.POINTER(Key), ctypes.POINTER(Variant)],
                  ctypes.byref(key), ctypes.byref(value)) < 0:
            return {}
        if value.vt != 65 or not value.value.blob.data or not 16 <= value.value.blob.size <= 4096:
            return {}
        return parse_endpoint_wave_format(ctypes.string_at(value.value.blob.data, value.value.blob.size))
    finally:
        ole.PropVariantClear(ctypes.byref(value))
        for pointer in (properties, device, enumerator):
            if pointer:
                invoke(pointer, 2, ctypes.c_uint32, [])
        if initialized >= 0:
            ole.CoUninitialize()



class SingleApplicationInstance:
    """Launch-owned Win32 mutex/event barrier, before Qt/BASS/settings imports.

    Use mutex ownership, not ERROR_ALREADY_EXISTS, to recover an abandoned
    process even while another launcher holds a handle. Names match the fixed
    machine-wide MIDI endpoint. Default ACL is not name-squatting resistance.
    """

    def __init__(self, name=r'Global\ABMSynth.SingleApplication.v1'):
        self.name=name
        self.handle=self.event=None
        self.owned=False
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        for symbol,args,result in (
            ('CreateMutexW',[ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p],ctypes.c_void_p),
            ('CreateEventW',[ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_wchar_p],ctypes.c_void_p),
            ('WaitForSingleObject',[ctypes.c_void_p,ctypes.c_uint32],ctypes.c_uint32),
            ('SetEvent',[ctypes.c_void_p],ctypes.c_int),
            ('ReleaseMutex',[ctypes.c_void_p],ctypes.c_int),
            ('CloseHandle',[ctypes.c_void_p],ctypes.c_int)):
            function=getattr(self.kernel,symbol);function.argtypes=args;function.restype=result

    @staticmethod
    def _error(operation):
        from i18n import tr
        return OSError(ctypes.get_last_error(),tr('instance.operation_failed',operation=operation))

    def acquire(self):
        if self.handle is not None:raise RuntimeError('single instance already acquired')
        self.handle=self.kernel.CreateMutexW(None,False,self.name)
        if not self.handle:raise self._error('CreateMutexW')
        try:
            result=self.kernel.WaitForSingleObject(self.handle,0)
            if result not in (0,0x80,0x102):raise self._error('WaitForSingleObject')
            self.owned=result in (0,0x80) # WAIT_OBJECT_0 / WAIT_ABANDONED
            self.event=self.kernel.CreateEventW(None,False,False,self.name+'.Activate')
            if not self.event:raise self._error('CreateEventW')
            return self.owned
        except BaseException:
            self.close()
            raise

    def request_activation(self):
        if self.event and not self.kernel.SetEvent(self.event):raise self._error('SetEvent')

    def consume_activation(self):
        if not self.owned or not self.event:return False
        result=self.kernel.WaitForSingleObject(self.event,0)
        if result==0x102:return False
        if result!=0:raise self._error('WaitForSingleObject')
        return True

    def close(self):
        if self.event:
            self.kernel.CloseHandle(self.event);self.event=None
        if self.handle:
            if self.owned:self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle);self.handle=None
        self.owned=False


def run_single_application(start):
    """One startup/restart ownership path shared by both executable entries."""
    await_previous_instance_exit()
    instance=SingleApplicationInstance()
    try:
        if not instance.acquire():
            instance.request_activation()
            return 0
        return start(instance)
    finally:
        instance.close()


def ui_animations_enabled() -> bool:
    """Read, never modify, Windows' client-area motion accessibility setting."""
    if os.name != "nt":
        return True
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    query = user32.SystemParametersInfoW
    query.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    query.restype = ctypes.c_int
    enabled = ctypes.c_int(1)
    # winuser.h SPI_GETCLIENTAREAANIMATION: pvParam points to a 32-bit BOOL.
    if query(0x1042, 0, ctypes.byref(enabled), 0):
        return bool(enabled.value)
    return True


def spawn_restart_after_exit() -> None:
    """Launch a replacement that waits until this process has fully exited."""
    environment = os.environ.copy()
    environment[RESTART_PARENT_PID_ENV] = str(os.getpid())
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.append(str(Path(__file__).with_name("app.py").resolve()))
    command.extend(sys.argv[1:])
    subprocess.Popen(command, env=environment, close_fds=True)


def await_previous_instance_exit() -> None:
    """On Windows, hold startup until the previous Qt process has gone."""
    parent_pid = os.environ.pop(RESTART_PARENT_PID_ENV, None)
    if not parent_pid or os.name != "nt":
        return
    try:
        process_id = int(parent_pid)
    except ValueError:
        return
    if process_id <= 0 or process_id == os.getpid():
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x00100000, 0, process_id)  # SYNCHRONIZE
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: the parent already exited.
            return
        raise OSError(error, "Cannot wait for previous ABMSynth process")
    try:
        result = kernel32.WaitForSingleObject(handle, 30000)
        if result == 0x00000102:  # WAIT_TIMEOUT
            raise TimeoutError("Previous ABMSynth process did not exit within 30 seconds")
        if result != 0:  # WAIT_OBJECT_0
            raise OSError(ctypes.get_last_error(), "Waiting for previous ABMSynth process failed")
    finally:
        kernel32.CloseHandle(handle)


def set_current_process_priority(level: str) -> None:
    """Apply an explicit Win32 class; verify realtime privilege fallback.

    processthreadsapi.h:SetPriorityClass may succeed but grant only HIGH when
    the caller lacks SeIncreaseBasePriorityPrivilege. Never report that as RT.
    """
    flag = PRIORITY_CLASSES[level]
    if os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.SetPriorityClass.restype = ctypes.c_int
    process=kernel32.GetCurrentProcess()
    previous=None
    if level=='realtime':
        kernel32.GetPriorityClass.argtypes=[ctypes.c_void_p]
        kernel32.GetPriorityClass.restype=ctypes.c_uint32
        previous=kernel32.GetPriorityClass(process)
        if not previous:raise OSError(ctypes.get_last_error(), 'GetPriorityClass failed')
    if not kernel32.SetPriorityClass(process, flag):
        raise OSError(ctypes.get_last_error(), "SetPriorityClass failed")
    if level=='realtime' and kernel32.GetPriorityClass(process)!=flag:
        from i18n import tr
        if not kernel32.SetPriorityClass(process,previous):
            raise OSError(ctypes.get_last_error(),tr('runtime.priority_restore_failed'))
        raise OSError(1314,tr('runtime.priority_realtime_denied'))


def startup_entry_path() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "Microsoft/Windows/Start Menu/Programs/Startup/ABMSynth.cmd"


def command_line() -> str:
    app = Path(__file__).with_name("app.py").resolve()
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    return f'"{Path(sys.executable).resolve()}" "{app}"'


def set_enabled(enabled: bool) -> None:
    """Maintain only ABMSynth's user-scope Startup-folder command."""
    target = startup_entry_path()
    if enabled:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("@echo off\nstart \"\" " + command_line() + "\n", encoding="utf-8")
    elif target.exists():
        target.unlink()


def is_enabled() -> bool:
    return startup_entry_path().is_file()


def open_sound_playback_devices() -> None:
    """Open the classic Sound control panel on its Playback tab.

    Microsoft documents ``control mmsys.cpl,,0`` for rendering endpoints.
    It opens the list, not a selected BASS device's properties dialog.
    """
    if os.name != "nt":
        raise OSError("Windows Sound control panel is available only on Windows")
    subprocess.Popen(["control.exe", "mmsys.cpl,,0"], close_fds=True)


def set_midi_mapper_enabled(enabled: bool) -> None:
    """Own HKCU preference only; does not install/register any system driver."""
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,r"Software\ABMSynth\MIDIMapper",0,
                           winreg.KEY_SET_VALUE|winreg.KEY_WOW64_64KEY) as key:
        winreg.SetValueEx(key,"Enabled",0,winreg.REG_DWORD,int(bool(enabled)))


def midi_mapper_api_mode(transfer_complete, use_legacy):
    """Registry request,not a runtime endpoint or service-availability probe.

    Microsoft MIDI required-registry-entries:UseLegacyMidi0=service,1=legacy,
    2=hybrid. Accept the documented string and deployed DWORD forms. Unknown
    modern values stay unqualified instead of silently authorizing installation.
    """
    if transfer_complete != 1 or str(use_legacy)=='1':return 'legacy'
    return 'hybrid' if str(use_legacy)=='2' else 'services'


def midi_driver_routing_contract(root=None):
    """Only admit the reviewed ordinary-port policy with both exact binaries."""
    import json,hashlib
    root=(Path(root) if root is not None else
          (Path(sys.executable).resolve().parent if getattr(sys,'frozen',False)
           else Path(__file__).resolve().parents[1])/'driver/winmm')
    try:
        contract=json.loads((root/'routing_contract.json').read_text(encoding='utf-8-sig'))
        if not isinstance(contract,dict) or contract.get('schema')!=1 or contract.get('policy') not in ('ordinary-ports-in-services-v1','winmm-default-with-sdk-caveat-v2'):return None
        for arch in ('x64','x86'):
            if hashlib.sha256((root/f'out/{arch}/Mapper/ABMSynthMapper.drv').read_bytes()).hexdigest()!=contract['sha256'][arch]:return None
        return contract
    except (OSError,ValueError,KeyError,TypeError):return None


def midi_mapper_status():
    """Read registration/owned file state only;never open a MIDI endpoint.

    A registered driver is not proof of an already running sender's route.
    Stable state/action identities are data;presentation belongs to catalogs.
    """
    import winreg
    import hashlib
    import json
    import re
    def values(hive,path,names,view):
        try:
            with winreg.OpenKey(hive,path,0,winreg.KEY_READ|view) as key:
                result={}
                for name in names:
                    try:result[name]=winreg.QueryValueEx(key,name)[0]
                    except FileNotFoundError:pass
                return result
        except FileNotFoundError:return {}
    rows=[];api_mode='unknown';enabled=None
    try:
        enabled=bool(values(winreg.HKEY_CURRENT_USER,r'Software\ABMSynth\MIDIMapper',('Enabled',),winreg.KEY_WOW64_64KEY).get('Enabled',1))
        for arch,view,directory in (('x64',winreg.KEY_WOW64_64KEY,'System32'),('x86',winreg.KEY_WOW64_32KEY,'SysWOW64')):
            registrations=values(winreg.HKEY_LOCAL_MACHINE,r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Drivers32',
                tuple(f'midi{i}' for i in range(1,10))+('MidisrvTransferComplete','UseLegacyMidi'),view)
            if arch=='x64':api_mode=midi_mapper_api_mode(registrations.get('MidisrvTransferComplete',0),registrations.get('UseLegacyMidi','0'))
            owner=values(winreg.HKEY_LOCAL_MACHINE,rf'SOFTWARE\ABMSynth\MIDIMapper\{arch}',('Backup','SHA256'),winreg.KEY_WOW64_64KEY)
            file=Path(os.environ.get('WINDIR',r'C:\Windows'))/directory/'ABMSynthMapper.drv'
            registered=any(str(v).casefold()=='abmsynthmapper.drv' for k,v in registrations.items() if k.startswith('midi'))
            exists=file.is_file();state='absent';owned=False;slot=None
            if owner:
                try:
                    backup=json.loads(owner.get('Backup',''))
                    slot=backup.get('slot');digest=owner.get('SHA256','')
                    owned=bool(isinstance(slot,str) and re.fullmatch(r'midi[1-9]',slot)
                        and isinstance(digest,str) and re.fullmatch(r'[0-9a-fA-F]{64}',digest)
                        and isinstance(backup.get('exists'),bool)
                        and (not backup['exists'] or (backup.get('kind')==1
                            and str(backup.get('value','')).casefold() in ('abmsynthmidi.drv','abmsynthmapper.drv'))))
                except (ValueError,TypeError,AttributeError):pass
            if owned:
                current=registrations.get(slot)
                if current is not None and str(current).casefold()!='abmsynthmapper.drv':state='conflict'
                elif registered and current is None:state='conflict'
                elif not exists:state='missing_file'
                elif hashlib.sha256(file.read_bytes()).hexdigest().casefold()!=digest.casefold():state='changed_file'
                else:state='ready' if registered else 'missing_registration'
            elif exists or registered or owner:state='unverified'
            rows.append(dict(architecture=arch,state=state,sha256=owner['SHA256'].casefold() if owned else None))
    except OSError:
        return dict(architectures=tuple(rows),state='unreadable',action=None,requested_action=None,
                    api_mode=api_mode,routing_restriction=None,enabled=enabled)
    states={row['state'] for row in rows}
    state=next((s for s in ('conflict','changed_file','unverified','missing_file','missing_registration','absent') if s in states),'ready')
    action=('Uninstall' if state=='ready' else 'Repair' if states<={'ready','missing_file','missing_registration'}
            else 'Install' if states<={'ready','absent'} else None)
    requested_action=action
    contract=midi_driver_routing_contract()
    modern=api_mode in ('services','hybrid')
    restriction=(('sdk_port_numbers' if contract['policy']=='winmm-default-with-sdk-caveat-v2' else 'default_requires_legacy')
                 if contract else 'unqualified') if modern else None
    # Microsoft issue1062 describes SDK correlation,not failed WinMM default
    # delivery. The v2 package honors the requested route with that caveat.
    if restriction=='unqualified' and action!='Uninstall':action=None
    if modern and contract and state in ('ready','missing_file','missing_registration') and any(row['sha256']!=contract['sha256'][row['architecture']] for row in rows):
        action=requested_action='Update'
    return dict(architectures=tuple(rows),state=state,action=action,
                requested_action=requested_action,api_mode=api_mode,
                routing_restriction=restriction,enabled=enabled)


def launch_midi_mapper_install(action="Install"):
    """Explicit button action -> Windows UAC. No automatic system mutation."""
    # A one-file bundle's extraction directory is temporary. Install assets
    # remain beside the released executable; source launch uses repository root.
    package_root=(Path(sys.executable).resolve().parent if getattr(sys,'frozen',False)
                  else Path(__file__).resolve().parents[1])
    script=package_root/"driver/winmm/mapper_install.ps1"
    if action not in ('Install','Uninstall','Repair','Update'):
        raise ValueError('Mapper action')
    if not script.is_file():
        raise FileNotFoundError(str(script))
    arguments=f'-NoProfile -ExecutionPolicy Bypass -File "{script}" -Action {action}'
    literal=arguments.replace("'","''")
    command=("try {$p=Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -Wait -PassThru -ErrorAction Stop "
             f"-ArgumentList '{literal}'; exit $p.ExitCode"+
             "} catch {$e=$_.Exception;while ($null -ne $e) {"
             "if (($e -is [System.ComponentModel.Win32Exception] -and $e.NativeErrorCode -eq 1223) "
             "-or (($e.HResult -band 65535) -eq 1223)) {exit 1223};$e=$e.InnerException};exit 1}")
    return subprocess.Popen(["powershell.exe","-NoProfile","-Command",command],
                            creationflags=subprocess.CREATE_NO_WINDOW)

