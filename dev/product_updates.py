"""Verified update staging. The external helper owns only post-exit deployment."""

from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
import ctypes

from i18n import tr
from product import PRODUCT_NAME, PRODUCT_VERSION
from product_support import atomic_write, version_key


MAX_PACKAGE_BYTES = 2 * 1024**3


def verify_executable(path, version):
    """Read the downloaded PE's actual product name and numeric version."""
    from ctypes import wintypes
    import struct
    api = ctypes.WinDLL('version', use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    api.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    api.GetFileVersionInfoW.restype = wintypes.BOOL
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    api.VerQueryValueW.restype = wintypes.BOOL
    size = api.GetFileVersionInfoSizeW(str(path), None)
    if not 0 < size < 1024**2:raise ValueError(tr('support.update_unverified'))
    buffer = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(str(path), 0, size, buffer):raise ctypes.WinError(ctypes.get_last_error())
    def query(name):
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if not api.VerQueryValueW(buffer, name, ctypes.byref(pointer), ctypes.byref(length)):
            raise ValueError(tr('support.update_unverified'))
        return pointer.value, length.value
    pointer, length = query('\\')
    if length < 52:raise ValueError(tr('support.update_unverified'))
    fixed = struct.unpack('<13I', ctypes.string_at(pointer,52))
    observed = (fixed[4] >> 16, fixed[4] & 65535, fixed[5] >> 16, fixed[5] & 65535)
    if fixed[0] != 0xfeef04bd or observed != version_key(version) + (0,):
        raise ValueError(tr('support.update_unverified'))
    pointer, length = query('\\VarFileInfo\\Translation')
    if length < 4:raise ValueError(tr('support.update_unverified'))
    language, codepage = struct.unpack('<HH', ctypes.string_at(pointer,4))
    pointer, length = query(f'\\StringFileInfo\\{language:04X}{codepage:04X}\\ProductName')
    if ctypes.wstring_at(pointer,length-1) != PRODUCT_NAME:raise ValueError(tr('support.update_unverified'))


def install_context():
    if not getattr(sys, 'frozen', False) or os.environ.get('ABMSYNTH_RECOVERY_PROFILE'):
        return None
    executable = Path(sys.executable).resolve()
    return dict(root=str(executable.parent), executable=str(executable),
                mode='installed' if (executable.parent / 'installed.mode').is_file() else 'portable')


def ignored_version():
    from settings import load_ui_values
    try:
        value = load_ui_values().get('ignored_update_version', '')
        version_key(value)
    except (OSError, ValueError, TypeError):
        return ''
    return value


def discover_release():
    from product_support import check_release
    result = check_release()
    result['ignored_version'] = ignored_version()
    return result


def ignore_version(version, writer=None):
    from settings import save_ui_values
    version_key(version)
    if writer is None:save_ui_values({'ignored_update_version': version})
    else:writer.submit({'UI':{'ignored_update_version':version}}, owned_keys={'UI':{'ignored_update_version'}})


def download_host(url):
    parsed = urlparse(url)
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password
            and parsed.port in (None, 443)
            and parsed.hostname in ('github.com', 'objects.githubusercontent.com',
                                    'release-assets.githubusercontent.com'))


def _response(url):
    if not download_host(url):
        raise ValueError(tr('support.update_unverified'))
    request = Request(url, headers={'User-Agent': f'{PRODUCT_NAME}/{PRODUCT_VERSION}'})
    response = urlopen(request, timeout=10)
    if not download_host(response.geturl()):
        response.close()
        raise ValueError(tr('support.update_unverified'))
    return response


def artifact_digest(release, artifact):
    digest = artifact.get('sha256', '')
    if re.fullmatch('[a-f0-9]{64}', digest):
        return digest
    checksum = release.get('checksums')
    if not checksum:
        raise ValueError(tr('support.update_unverified'))
    with _response(checksum) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError(tr('support.update_unverified'))
    matches = []
    for line in raw.decode('utf-8-sig').splitlines():
        match = re.fullmatch(r'([a-fA-F0-9]{64})\s+\*?(.+)', line)
        if match and match[2] == artifact['name']:
            matches.append(match[1].lower())
    if len(matches) != 1:
        raise ValueError(tr('support.update_unverified'))
    return matches[0]


def _portable_stage(package, folder, cancelled=lambda: False):
    """Admit plain relative files. Never replace profile data or linked paths."""
    rows = []
    seen = set()
    total = 0
    with zipfile.ZipFile(package) as archive:
        if len(archive.infolist()) > 10000:
            raise ValueError(tr('support.update_unverified'))
        for item in archive.infolist():
            if cancelled():raise InterruptedError(tr('support.cancelled'))
            path = PurePosixPath(item.filename)
            if ('\\' in item.filename or not path.parts or path.parts[0] != 'ABMSynth'
                    or path.is_absolute() or '..' in path.parts
                    or any(':' in part or part.endswith((' ', '.')) for part in path.parts)
                    or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError(tr('support.update_unverified'))
            if item.is_dir():
                continue
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            key = relative.casefold()
            if (not relative or key in seen or key in ('abmsynth.ini', 'installed.mode')
                    or path.parts[1].casefold() in ('logs', 'recovery')
                    or any(re.fullmatch(r'(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?',
                                        part, re.I) for part in path.parts)):
                raise ValueError(tr('support.update_unverified'))
            seen.add(key)
            total += item.file_size
            if total > MAX_PACKAGE_BYTES:
                raise ValueError(tr('support.update_unverified'))
            target = folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open('xb') as output:
                while chunk := source.read(256 * 1024):
                    if cancelled():raise InterruptedError(tr('support.cancelled'))
                    output.write(chunk)
            with target.open('rb') as source:
                rows.append(dict(path=relative, sha256=hashlib.file_digest(source,'sha256').hexdigest()))
    if 'abmsynth.exe' not in seen:
        raise ValueError(tr('support.update_unverified'))
    if not any(key.startswith('_internal/') for key in seen):
        raise ValueError(tr('support.update_unverified'))
    return rows


def prepare_update(release, context, *, cancelled=lambda: False, progress=lambda done, total: None):
    if version_key(release['version']) <= version_key(PRODUCT_VERSION):
        raise ValueError(tr('support.update_unverified'))
    artifact = release.get('artifacts', {}).get(context['mode'])
    if not artifact:
        raise ValueError(tr('support.update_unavailable'))
    suffix = 'Setup-x64.exe' if context['mode'] == 'installed' else 'Portable-x64.zip'
    expected = f'ABMSynth-{release["version"]}-{suffix}'
    from urllib.parse import quote
    base = 'https://github.com/ruain-dev/ABMSynth/releases/download/'
    tag = release['url'].rsplit('/',1)[-1]
    if artifact['name'] != expected or artifact['url'] != base + tag + '/' + quote(expected, safe=''):
        raise ValueError(tr('support.update_unverified'))
    size = artifact['size']
    if type(size) is not int or not 0 < size <= MAX_PACKAGE_BYTES:
        raise ValueError(tr('support.update_unverified'))
    digest = artifact_digest(release, artifact)
    directory = Path(tempfile.mkdtemp(prefix='ABMSynth-update-'))
    try:
        package = directory / artifact['name']
        observed = hashlib.sha256()
        done = 0
        with _response(artifact['url']) as response, package.open('xb') as output:
            while True:
                if cancelled():
                    raise InterruptedError(tr('support.cancelled'))
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > size:
                    raise ValueError(tr('support.update_unverified'))
                output.write(chunk); observed.update(chunk)
                progress(done, size)
        if done != size or observed.hexdigest() != digest:
            raise ValueError(tr('support.update_unverified'))
        plan = dict(schema=1, mode=context['mode'], version=release['version'], parent=os.getpid(),
                    parentExecutable=str(Path(sys.executable).resolve()),
                    root=context['root'], executable=context['executable'], package=str(package), sha256=digest)
        from settings import settings_path
        plan['logDirectory'] = str(settings_path().parent / 'logs' / 'updates')
        if context['mode'] == 'portable':
            plan['files'] = _portable_stage(package, directory / 'payload', cancelled)
        verify_executable(directory / 'payload/ABMSynth.exe' if context['mode']=='portable' else package,
                          release['version'])
        helper = Path(__file__).with_name('assets') / 'apply-update.ps1'
        shutil.copyfile(helper, directory / 'apply-update.ps1')
        atomic_write(directory / 'plan.json', json.dumps(plan, ensure_ascii=False).encode('utf-8'))
        return str(directory)
    except BaseException:
        shutil.rmtree(directory)
        raise


def start_handoff(directory):
    directory = Path(directory).resolve(strict=True)
    helper = directory / 'apply-update.ps1'
    powershell = Path(os.environ['WINDIR']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    environment = os.environ.copy()
    # PyInstaller documents a fresh bootloader environment for restarted apps.
    # The external helper and replacement must not reuse the retiring runtime.
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    return subprocess.Popen([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
                             'Bypass', '-File', str(helper), '-PlanPath', str(directory / 'plan.json')],
                            creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True, env=environment)
