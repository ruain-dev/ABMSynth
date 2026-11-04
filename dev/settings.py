"""ABMSynth INI storage, validated preferences, and read-only BMSynth import.

The own-file write functions and foreign-file read functions remain explicit;
co-location does not grant the importer permission to modify BMSynth files.
"""

from __future__ import annotations

import os
import hashlib
import json
import re
import shutil
import sys
import tempfile
import time
from configparser import ConfigParser, Error as ConfigError
from dataclasses import dataclass, fields, replace
from pathlib import Path
from threading import RLock
import threading
from contextlib import contextmanager
from weakref import WeakSet
from uuid import uuid4

from configuration_data import INI_MAX_BYTES, INI_MAX_LINE, read_settings_json
from i18n import tr
from soundfont_files import SoundFontLibrary, resolve_soundfont_selection_path, stored_soundfont_selection_path
from windows_integration import PRIORITY_CLASSES


# ABMSynth-owned atomic INI storage.
_INI_LOCK = RLock()
_INI_COORDINATORS_LOCK = RLock()
_INI_COORDINATORS = WeakSet()
STORAGE_SCHEMA_VERSION = 1
# First-run host fader policy,not MIDI master volume. The final square-law
# fader maps128/256 to0.25 amplitude(-12.04dB). Saved UI.volume wins unchanged.
DEFAULT_MASTER_VOLUME = 128
_STORAGE_OWNER = f'{os.getpid()}:{uuid4().hex}'  # Process instance, not a PID-reuse ownership test.
SETTINGS_DOMAINS = {name: 1 for name in ('UI','Engine','MIDI CC','Channel Policy','SoundFontLibrary','MIDI Export','MIDI Ports')}
_SECTION_NAMES = {name.casefold(): name for name in
                  ("UI", "Engine", "MIDI CC", "Channel Policy", "SoundFontLibrary", "MIDI Export", "MIDI Ports", "ABMSynth Storage")}
_INI_READER_API = None


@dataclass(frozen=True)
class MidiPortSettings:
    """Eight independent16-channel addresses; the default is one port.

    WinMM names include a directional suffix within MAXPNAMELEN32 WCHAR.
    Endpoint identities use name plus occurrence, not volatile device numbers.
    """
    ports: tuple = ()

    def normalized(self):
        source=self.ports or ({'name':'ABMSynth MIDI','virtual_in':True,'virtual_out':False,'inputs':[],'outputs':[]},)
        if not 1<=len(source)<=8:raise ValueError(tr('midi_ports.invalid_count'))
        result=[];names=set();slots=set()
        for index,row in enumerate(source):
            if not isinstance(row,dict):raise ValueError(tr('midi_ports.invalid_device'))
            if not isinstance(row.get('name'),str):raise ValueError(tr('midi_ports.invalid_name'))
            name=row['name'].strip()
            if not name or len(name.encode('utf-16-le'))>54 or any(ord(c)<32 for c in name) or name.casefold() in names:
                raise ValueError(tr('midi_ports.invalid_name'))
            names.add(name.casefold())
            slot=row.get('slot',index)
            if type(slot) is not int or not 0<=slot<8 or slot in slots:raise ValueError(tr('midi_ports.invalid_count'))
            slots.add(slot)
            item=dict(row,name=name,slot=slot,virtual_in=bool(row.get('virtual_in',True)),virtual_out=bool(row.get('virtual_out',False)))
            if type(row.get('virtual_in',True)) is not bool or type(row.get('virtual_out',False)) is not bool:raise ValueError(tr('midi_ports.invalid_device'))
            for direction in ('inputs','outputs'):
                values=row.get(direction,[])
                if not isinstance(values,list) or any(not isinstance(v,str) or len(v)>256 for v in values):
                    raise ValueError(tr('midi_ports.invalid_device'))
                item[direction]=list(dict.fromkeys(values))
            result.append(item)
        return MidiPortSettings(tuple(sorted(result,key=lambda row:row['slot'])))

    def serialized(self):return json.dumps(self.normalized().ports,ensure_ascii=False,separators=(',',':'))


def load_midi_ports(path=None):
    parser=read_ini(path)
    try:
        raw=parser.get('MIDI Ports','Configuration',fallback='')
        if not raw:return MidiPortSettings().normalized()
        records=read_settings_json(raw)
        if not isinstance(records,list):raise ValueError('MIDI ports document')
        required={'name','slot','virtual_in','virtual_out','inputs','outputs'}
        if any(type(row) is not dict or not required<=row.keys() for row in records):raise ValueError('MIDI ports fields')
        return MidiPortSettings(tuple(records)).normalized()
    except (ValueError,TypeError,AttributeError):return MidiPortSettings().normalized()


@contextmanager
def _open_ini_reader(target):
    """Short-lived compatible read handle; parsing starts after it closes."""
    if os.name != 'nt':
        with Path(target).open('rb') as stream:yield stream
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes
    global _INI_READER_API
    if _INI_READER_API is None:
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        create=kernel.CreateFileW
        create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,
            ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        create.restype=wintypes.HANDLE
        close=kernel.CloseHandle;close.argtypes=[wintypes.HANDLE];close.restype=wintypes.BOOL
        _INI_READER_API=(create,close)
    create,close=_INI_READER_API
    # WinBase.h GENERIC_READ, FILE_SHARE_READ|WRITE|DELETE, OPEN_EXISTING.
    handle=create(str(Path(target)),0x80000000,7,None,3,0x80,None)
    if handle == ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    try:descriptor=msvcrt.open_osfhandle(int(handle),os.O_RDONLY|os.O_BINARY)
    except BaseException:
        close(handle);raise
    try:stream=os.fdopen(descriptor,'rb')
    except BaseException:
        os.close(descriptor);raise
    with stream:yield stream  # fd owns the native handle after transfer.


def _ini_bytes(target):
    with _open_ini_reader(target) as stream:return stream.read(INI_MAX_BYTES+1)


def _replace_ini(temporary,target,before_hash):
    """Bounded retry only while both publication inputs are still unchanged."""
    for attempt in range(5):
        try:
            os.replace(temporary,target);return
        except OSError as error:
            if os.name!='nt' or getattr(error,'winerror',None) not in (5,32,33) or attempt==4:raise
            if not temporary.exists():raise
            try:current_hash=hashlib.sha256(_ini_bytes(target)).digest()
            except FileNotFoundError:current_hash=None
            if current_hash!=before_hash:raise error  # never retry over an external edit.
            # Existing writer worker, max200ms; no permission/ACL or fallback
            # overwrite changes. Persistent denial remains a failed durable save.
            time.sleep(.05)

def settings_path() -> Path:
    # Audio audits temporarily disable UI_ONLY to exercise real decode streams.
    # Their explicitly selected INI must remain isolated during that phase too.
    isolated = os.environ.get("ABMSYNTH_TEST_INI_PATH")
    if isolated:
        return Path(isolated)
    if not getattr(sys, "frozen", False):
        source = Path(__file__).resolve().parent
        return source.parent / "local" / "profile" / "ABMSynth.ini"
    # Frozen deployment markers are beside the actual launched executable.
    directory = Path(sys.executable).resolve().parent
    if (directory / "installed.mode").is_file():
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return local / "ABMSynth" / "ABMSynth.ini"
    return directory / "ABMSynth.ini"


def _parser() -> ConfigParser:
    # Literal paths/percent signs, explicit section ownership, no interpolation.
    parser = ConfigParser(interpolation=None, strict=True)
    return parser


def parse_settings_text(source, *, source_name='<settings>'):
    """Recover current INI records without guessing a retired representation.

    Python configparser's strict grammar remains the final parser. This scan
    isolates malformed records first; duplicate keys are ambiguous and neither
    value wins. DEFAULT inheritance is forbidden. Unknown explicit keys survive
    as inert data; typed consumers select their current allowlists.
    """
    parser = _parser()
    issues, records, section, key = [], {}, None, None
    if len(source.encode('utf-8')) > INI_MAX_BYTES:
        parser._abmsynth_issues = [('file', 'size', 'invalid')]
        parser._abmsynth_recovered = True
        return parser
    duplicates = set()
    for number, line in enumerate(source.splitlines(), 1):
        text = line.strip()
        if not text or text.startswith(('#', ';')):
            continue
        if len(line) > INI_MAX_LINE or any(ord(c)<32 and c!='\t' for c in line):
            issues.append((section or 'file', str(number), 'invalid'))
            if line.startswith('\t') and section is not None and key is not None:
                records[section].pop(key, None)
                duplicates.add((section, key))
            key = None
            continue
        if text.startswith('['):
            match = re.fullmatch(r'\[([^\]\r\n]+)\]\s*(?:[;#].*)?', text)
            if not match or match[1].strip().casefold() == 'default':
                issues.append(('file', str(number), 'invalid'))
                section, key = None, None
                continue
            name = match[1].strip()
            section = _SECTION_NAMES.get(name.casefold(), name)
            records.setdefault(section, {})
            key = None
            continue
        if section is None:
            issues.append(('file', str(number), 'invalid'))
            continue
        # ConfigParser.write indents continuation lines with a tab. Retain that
        # current representation only after an unambiguous admitted key.
        if line.startswith('\t') and key is not None:
            records[section][key] += '\n' + line
            continue
        match = re.fullmatch(r'\s*([^=:\s][^=:]*?)\s*[=:]\s*(.*)', line)
        if not match:
            issues.append((section, str(number), 'invalid'))
            key = None
            continue
        key = match[1].strip().lower()
        if key in records[section] or (section, key) in duplicates:
            records[section].pop(key, None)
            duplicates.add((section, key))
            issues.append((section, key, 'invalid'))
            key = None
            continue
        records[section][key] = match[2]
    for section, values in records.items():
        block = '[' + section + ']\n' + ''.join(key + '=' + value + '\n' for key, value in values.items())
        checked = _parser()
        try:
            checked.read_string(block, source=source_name)
        except ConfigError:
            issues.append((section, 'syntax', 'invalid'))
            continue
        parser.read_dict({section: dict(checked.items(section))})
    parser._abmsynth_issues = issues
    parser._abmsynth_recovered = bool(issues)
    return parser


def _read_ini_disk(path: Path | None = None) -> ConfigParser:
    parser = _parser()
    target = path or settings_path()
    try:
        raw = _ini_bytes(target)
        source_hash=hashlib.sha256(raw).digest()
        try:
            if len(raw) > INI_MAX_BYTES: raise UnicodeError('settings size')
            source = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            # Never guess another encoding or silently corrupt a file path.
            parser._abmsynth_issues=[('file','encoding','invalid')]
            parser._abmsynth_recovered=True
        except UnicodeError:
            parser._abmsynth_issues=[('file','size','invalid')]
            parser._abmsynth_recovered=True
            parser._abmsynth_oversize=True
        else:
            parser=parse_settings_text(source,source_name=str(target))
        parser._abmsynth_source_hash=source_hash
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, ConfigError) as exc:
        raise ValueError(tr('storage.read_failed',path=target)) from exc
    return parser


@dataclass
class SettingsReadReport:
    """Read-only field recovery evidence; values are never included in logs.

    Absent fields alone are not corruption. Unknown schemas/fields survive
    writes; reports describe current consumption, not forced file migration.
    """
    entries: list = None

    def __post_init__(self):
        self.entries=[] if self.entries is None else list(self.entries)

    def record(self,section,key,disposition):
        entry=(section,key,disposition)
        if entry not in self.entries: self.entries.append(entry)

    def invalid_fields(self):
        return tuple(f'{section}.{key}' for section,key,state in self.entries if state=='invalid')


def read_ini(path: Path | None = None, *, report=None) -> ConfigParser:
    """Read committed bytes plus immutable own-process queued changes.

    Read-your-writes does not wait for disk on the GUI thread. Other processes
    see only atomic committed files; this is not a claim of durable completion.
    """
    target = Path(path or settings_path()).resolve()
    with _INI_COORDINATORS_LOCK:
        coordinators = tuple(_INI_COORDINATORS)
    # Capture BEFORE reading: a worker may commit and clear `active` after the
    # file read. Capturing afterward can otherwise return stale pre-commit bytes
    # with no queued overlay. This snapshot linearizes our own writes at entry;
    # submissions made during the read may appear on the next read.
    batches = tuple(batch for coordinator in coordinators if coordinator.path == target
                    for batch in coordinator.observations())
    parser = _read_ini_disk(target)
    for batch in batches:
        _merge_sections(parser, batch.sections, batch.owned_keys)
    if report is not None:
        metadata=parser['ABMSynth Storage'] if parser.has_section('ABMSynth Storage') else {}
        for section in SETTINGS_DOMAINS:
            schema=metadata.get('schema_'+section.lower().replace(' ','_'))
            report.record(section,'schema','unspecified' if schema is None else
                          'current' if schema=='1' else 'unrecognized_metadata')
        for section,key,state in getattr(parser,'_abmsynth_issues',()): report.record(section,key,state)
        if getattr(parser,'_abmsynth_recovered',False): report.record('file','syntax','recovered')
    return parser


@contextmanager
def _ini_transaction_lock(target):
    """Windows cross-process read/merge/replace lock; never called by audio.

    msvcrt.locking starts at the current file offset and may lock beyond EOF.
    A persistent sidecar is intentional: deleting it creates two lock domains.
    """
    import msvcrt
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.with_name(target.name + ".lock").open("a+b") as lock:
        deadline = time.monotonic() + 5
        while True:
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(tr("storage.busy"))
                time.sleep(.01)
        try: yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _merge_sections(parser, sections, owned_keys):
    for section, values in sections.items():
        previous = dict(parser.items(section)) if parser.has_section(section) else {}
        if owned_keys is not None and section in owned_keys:
            owned = owned_keys[section]
            previous = {key: value for key, value in previous.items()
                        if not (owned(key) if callable(owned) else key in owned)}
        values = {**previous, **values}
        if parser.has_section(section): parser.remove_section(section)
        parser.add_section(section)
        for key, value in values.items(): parser.set(section, key, str(value))


@dataclass(frozen=True)
class IniWriteBatch:
    """Owned values copied before publication; widgets never enter the worker."""
    records: tuple
    ownership: tuple

    @property
    def sections(self): return {section: dict(values) for section, values in self.records}
    @property
    def owned_keys(self): return dict(self.ownership)

    @classmethod
    def capture(cls, sections, owned_keys):
        return cls(tuple((section, tuple((str(key), str(value)) for key, value in values.items()))
                         for section, values in sections.items()),
                   tuple((section, frozenset(keys)) for section, keys in owned_keys.items()))


class IniWriteCoordinator:
    """One coalescing storage worker per window; explicit durable flush on close.

    Unrelated sections merge under the process-wide and cross-process locks.
    Concurrent writes to the same owned field retain last-transaction semantics.
    app.main separately enforces one running application instance; these
    storage locks still protect external tools/export recovery. Failures retry.
    """
    def __init__(self, path=None, *, asynchronous=True, on_error=None):
        self.path = Path(path or settings_path()).resolve()
        self.asynchronous, self.on_error = asynchronous, on_error
        self.condition = threading.Condition()
        self.pending = self.active = self.failed = None
        self.error = None
        self.closing = False
        self.thread = None
        with _INI_COORDINATORS_LOCK: _INI_COORDINATORS.add(self)

    def observations(self):
        with self.condition:
            return tuple(batch for batch in (self.failed, self.active, self.pending) if batch)

    @staticmethod
    def combine(previous, latest):
        if previous is None: return latest
        if latest is None: return previous
        sections, owned = previous.sections, previous.owned_keys
        for section, values in latest.sections.items():
            keys = latest.owned_keys.get(section, frozenset(values))
            merged = {key: value for key, value in sections.get(section, {}).items() if key not in keys}
            sections[section] = {**merged, **values}
            owned[section] = owned.get(section, frozenset()) | keys
        return IniWriteBatch.capture(sections, owned)

    def submit(self, sections, *, owned_keys):
        batch = IniWriteBatch.capture(sections, owned_keys)
        with self.condition:
            if self.closing: raise RuntimeError("INI coordinator closed")
            if not self.asynchronous:
                update_sections(batch.sections, self.path, owned_keys=batch.owned_keys)
                return
            self.pending = self.combine(self.combine(self.failed, self.pending), batch)
            self.failed = None; self.error = None
            # Lazy start: an aborted window constructor or read-only window
            # does not leave a storage thread alive with no work to perform.
            self._start_worker_locked()
            self.condition.notify_all()

    def _start_worker_locked(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="ABMSynth-INI", daemon=True)
            self.thread.start()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending is not None or self.closing)
                if self.pending is None: return
                self.active, self.pending = self.combine(self.failed, self.pending), None
                self.failed = None; self.error = None
                batch = self.active
            try:
                update_sections(batch.sections, self.path, owned_keys=batch.owned_keys)
            except Exception as error:
                with self.condition:
                    self.failed = self.combine(self.failed, batch)
                    self.error = error
                if self.on_error:
                    try: self.on_error(error)
                    except Exception: pass  # logger failure must not kill storage
            finally:
                with self.condition:
                    self.active = None
                    self.condition.notify_all()

    def flush(self, *, retry_failed=False):
        with self.condition:
            if retry_failed and self.failed is not None and not self.closing:
                self.pending = self.combine(self.failed, self.pending)
                self.failed = None; self.error = None
                self._start_worker_locked()
                self.condition.notify_all()
            self.condition.wait_for(lambda: self.active is None and self.pending is None)
            if self.error is not None: raise self.error

    def close(self, *, discard_failed=False):
        """Failed durable close remains a live, retryable storage owner.

        Only an aborted constructor may explicitly abandon its failed draft;
        normal Close/Restart must not hide it by removing the read overlay.
        """
        failure = None
        try: self.flush()
        except Exception as error:
            if not discard_failed: raise
            failure = error
        with self.condition:
            self.closing = True; self.condition.notify_all()
        if self.thread is not None: self.thread.join()
        with self.condition:
            # A concurrent submit immediately after flush may have queued one
            # final transaction. Its error is not a successful durable close.
            if self.error is not None and not discard_failed:
                self.closing = False; self.thread = None
                raise self.error
        with _INI_COORDINATORS_LOCK: _INI_COORDINATORS.discard(self)
        if failure is not None: raise failure


def update_section(section: str, values: dict[str, str], path: Path | None = None) -> None:
    """Replace one section without discarding the other settings domain."""
    update_sections({section: values}, path)


def update_sections(sections: dict[str, dict[str, str]], path: Path | None = None,
                    *, owned_keys=None) -> None:
    """Commit one or more complete sections in one atomic file replacement."""
    with _INI_LOCK:
        target = Path(path or settings_path()).resolve()
        with _ini_transaction_lock(target):
            _update_sections_locked(sections, target, owned_keys=owned_keys)


def _update_sections_locked(sections, path, *, owned_keys):
    target = path or settings_path()
    parser = _read_ini_disk(target)
    if getattr(parser,'_abmsynth_oversize',False):
        # Reading can recover defaults;writing must not replace an unchecked
        # oversized input based on a prefix hash.
        raise ValueError(tr('storage.read_failed',path=target))
    if getattr(parser, "_abmsynth_recovered", False):
        # Retain the exact pre-repair bytes before rewriting recovered records.
        backup = target.with_name(target.name + ".recovery.bak")
        if backup.exists() and hashlib.sha256(_ini_bytes(backup)).digest()!=parser._abmsynth_source_hash:
            backup=target.with_name(target.name+'.recovery.'+parser._abmsynth_source_hash.hex()[:12]+'.bak')
        if not backup.exists():
            shutil.copy2(target, backup)
    _merge_sections(parser, sections, owned_keys)
    # Metadata is descriptive,never an instruction to migrate values. Unknown
    # fields stay inert; a write updates only this transaction's owned keys.
    if not parser.has_section("ABMSynth Storage"): parser.add_section("ABMSynth Storage")
    metadata = parser["ABMSynth Storage"]
    if "schema_version" not in metadata: metadata["schema_version"] = str(STORAGE_SCHEMA_VERSION)
    for section,version in SETTINGS_DOMAINS.items():
        key='schema_'+section.lower().replace(' ','_')
        if parser.has_section(section) and key not in metadata: metadata[key]=str(version)
    try: revision = max(0, int(metadata.get("revision", "0")))
    except ValueError: revision = 0
    metadata["revision"] = str(revision + 1)
    metadata['last_writer'] = _STORAGE_OWNER
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent,
            prefix=".abmsynth-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            parser.write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_ini(temporary, target, getattr(parser,'_abmsynth_source_hash',None))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def load_ui_values(path: Path | None = None) -> dict[str, str]:
    parser = read_ini(path)
    return dict(parser.items("UI")) if parser.has_section("UI") else {}


def save_ui_values(values: dict[str, str], path: Path | None = None, *, owned_keys=()) -> None:
    # Absent scalar/unknown future keys survive partial updates. The window
    # supplies its schema for deliberately removed bands/neutral overrides.
    update_sections({"UI": values}, path, owned_keys={"UI": set(owned_keys) | set(values)})


def load_midi_cc_values(path: Path | None = None) -> dict[str, str]:
    parser = read_ini(path)
    return dict(parser.items("MIDI CC")) if parser.has_section("MIDI CC") else {}


def save_midi_cc_values(values: dict[str, str], path: Path | None = None, *, owned_keys=None) -> None:
    update_sections({"MIDI CC": values}, path,
                    owned_keys={"MIDI CC": owned_keys} if owned_keys is not None else None)


def load_channel_policy_values(path: Path | None = None) -> dict[str, str]:
    parser = read_ini(path)
    return dict(parser.items("Channel Policy")) if parser.has_section("Channel Policy") else {}


def save_channel_policy_values(values: dict[str, str], path: Path | None = None, *, owned_keys=None) -> None:
    update_sections({"Channel Policy": values}, path,
                    owned_keys={"Channel Policy": owned_keys} if owned_keys is not None else None)


def load_soundfont_library(path: Path | None = None, *, report=None) -> SoundFontLibrary:
    """Read ordered selector paths without changing the active SoundFont."""
    parser = read_ini(path,report=report)
    if not parser.has_section("SoundFontLibrary"):
        return SoundFontLibrary()
    groups: dict[str, list[tuple[int, str]]] = {
        "recent": [], "favorites": [], "hidden": [],
    }
    for key, value in parser.items("SoundFontLibrary"):
        name, separator, position = key.rpartition("_")
        if separator and name in groups and position.isdecimal():
            groups[name].append((int(position), value))
            if report is not None: report.record('SoundFontLibrary',key,'accepted')
        elif report is not None: report.record('SoundFontLibrary',key,'unknown_preserved')
    return SoundFontLibrary(*(
        tuple(resolve_soundfont_selection_path(value) for _index, value in sorted(groups[name]))
        for name in ("recent", "favorites", "hidden")
    )).normalized()


def soundfont_library_ini(library, parser=None):
    """Immutable write payload with finite owned keys; no widget/file I/O."""
    state=library.normalized()
    values={f'{name}_{index}':stored_soundfont_selection_path(item) for name in ('recent','favorites','hidden')
            for index,item in enumerate(getattr(state,name))}
    keys=set(values)
    if parser is not None and parser.has_section('SoundFontLibrary'):
        keys.update(key for key in parser['SoundFontLibrary'] if re.fullmatch(r'(?:recent|favorites|hidden)_\d+',key))
    return values,keys


def save_soundfont_library(library: SoundFontLibrary, path: Path | None = None) -> None:
    """Atomically replace only the library section of ABMSynth.ini."""
    values, _ = soundfont_library_ini(library)
    update_sections({"SoundFontLibrary": values}, path, owned_keys={
        "SoundFontLibrary": lambda key: bool(re.fullmatch(r"(?:recent|favorites|hidden)_\d+", key))})


# Validated engine and program preferences.

# Product admission choices, NOT a BASS/BASSMIDI maximum. Higher native decode
# rates are independently covered by capability_review_gate. Selected whole
# effect/encoder recipes and three-backend176.4/192k negotiation are qualified;
# arbitrary hardware/parameter combinations and actual deadlines are not.
# Supported device and codec combinations still require separate checks.
SAMPLE_RATES = (11025, 22050, 44100, 48000, 88200, 96000, 176400, 192000,
                352800, 384000, 705600, 768000)
RECORDING_EXTENSIONS = {"wav": "wav", "aiff": "aif", "flac": "flac", "ogg_flac": "oga", "vorbis": "ogg", "opus": "opus", "mp3": "mp3"}
RECORDING_BITRATES = (64, 96, 128, 160, 192, 256, 320)
# Pinned official encoders' measured rejections, NOT inferred maximum rates.
# Independent native tests cover each pair before admission; codec upgrades
# require renewal. Source synthesis remains available with another container.
RECORDING_RATE_REJECTIONS = {
    'vorbis': (352800,384000,705600,768000),
    'flac': (705600,768000), 'ogg_flac': (705600,768000),
}
# One apply policy shared by the dialog and runtime snapshot. Login startup
# registration itself crosses the Save boundary, not the live preview boundary.
LIVE_PREFERENCE_FIELDS = (
    "main_log_level", "main_log_detail",
    "voices", "interpolation", "midi_cpu_limit",
    "default_reverb_send", "soundfont_fx_policy", "auto_reset_delay_s", "priority",
    "cc6_direct_pitch_range", "cc94_custom_delay_send",
    "startup_with_windows", "minimize_to_tray", "close_to_tray",
    "windows_midi_default",
    "recording_format", "recording_dither", "recording_rf64", "recording_container",
    "recording_directory", "recording_basename", "recording_timestamp",
    "recording_compression", "recording_vorbis_quality", "recording_mp3_quality",
    "recording_opus_complexity", "recording_bitrate", "recording_rate_mode",
)
RESTART_PREFERENCE_FIELDS = (
    "sample_rate", "buffer_ms", "shared_update_ms", "exclusive_block_ms", "midi_async", "loading_mode", "start_minimized", "soundfont_stereo", "synthesis_mode",
)
# Creation-time audio changes need a running engine restart. Start-minimized
# only changes a future launch's presentation and should never demand one.
RESTART_REQUIRED_FIELDS = tuple(name for name in RESTART_PREFERENCE_FIELDS if name != "start_minimized")


@dataclass(frozen=True)
class EnginePreferences:
    sample_rate: int = 96000
    buffer_ms: int = 100
    voices: int = 100000
    interpolation: int = 2
    # Direct BASSMIDI channel-reset seed, with no separate source mode.
    default_reverb_send: int = 40
    priority: str = "normal"
    # Zero disables idle reset; a positive value is its complete setting.
    auto_reset_delay_s: int = 0
    loading_mode: int = 2
    # Program-surface preferences live in the same portable INI so the
    # settings dialog has one atomic save boundary.
    startup_with_windows: bool = False
    start_minimized: bool = False
    minimize_to_tray: bool = False
    close_to_tray: bool = False
    # Requested default-on intent. Modern Windows cannot apply it without a
    # Owned WinMM MOD_MAPPER driver; installation is explicit/UAC, preference
    # alone never claims a registered system route. Fresh MIDI hosts see changes.
    windows_midi_default: bool = True
    # BASS_MIDI_ASYNC is a stream-creation choice; absent INI keys retain the
    # BMSynth-compatible synchronous behavior that aligns overlapping notes.
    midi_async: bool = False
    # Incoming MIDI parser choices, not stream-creation flags. Keep the
    # historical ABMSynth interpretation unless the user turns it off.
    cc6_direct_pitch_range: bool = True
    cc94_custom_delay_send: bool = True
    # bassmidi.h BASS_MIDI_FONT_MINFX/NOFX; default preserves prior summation.
    soundfont_fx_policy: str = "sum"
    # bassmidi.h MIDI_CPU: automatic0; >100 prevents CPU-driven voice killing.
    midi_cpu_limit: int = 0
    # FONT_STEREO changes CC10 semantics for detected stereo SF2 pairs.
    soundfont_stereo: bool = False
    # Next-take encoder policy; never reconfigure an unfinished recording.
    recording_format: str = "float32"
    recording_dither: bool = True
    recording_rf64: bool = True
    recording_container: str = "wav"
    recording_directory: str = ""
    recording_basename: str = "ABMSynth"
    recording_timestamp: bool = True
    recording_compression: int = 5
    recording_vorbis_quality: int = 6
    recording_mp3_quality: int = 2
    recording_opus_complexity: int = 10
    recording_bitrate: int = 192
    recording_rate_mode: str = "vbr"
    # Synchronous synthesis is the default for predictable event timing.
    # Explicit parallel modes stay selectable; absent/invalid keys are safe.
    # Stream graph changes require restart; quality/voices remain independent.
    synthesis_mode: str = "compatible"
    # Current recording and output policies.
    shared_update_ms: int = 5
    exclusive_block_ms: int = 5
    main_log_level: str = "info"
    main_log_detail: str = "summary"

    def validate(self) -> "EnginePreferences":
        if self.main_log_level not in ('info','warning','error'):raise ValueError('main_log_level')
        if self.main_log_detail not in ('changes','summary','detailed'):raise ValueError('main_log_detail')
        if self.synthesis_mode not in ("compatible", "high_performance"):
            raise ValueError("synthesis_mode")
        if self.sample_rate not in SAMPLE_RATES:
            raise ValueError("sample_rate")
        # bass.md BASS_CONFIG_BUFFER permits 10..5000 ms.
        if not 10 <= self.buffer_ms <= 5000:
            raise ValueError("buffer_ms")
        # BASS_CONFIG_UPDATEPERIOD: complete automatic-update domain in ms.
        # BASS_WASAPI_Init buffer: product request range, not a native maximum.
        if type(self.shared_update_ms) is not int or not 5 <= self.shared_update_ms <= 100:
            raise ValueError("shared_update_ms")
        if type(self.exclusive_block_ms) is not int or not 1 <= self.exclusive_block_ms <= 100:
            raise ValueError("exclusive_block_ms")
        # 32 is the minimum accepted by BMSynth's original edPoly handler;
        # Product voice ceiling; this counts samples, not MIDI notes.
        if not 32 <= self.voices <= 100000:
            raise ValueError("voices")
        if self.midi_cpu_limit not in (0, 101):
            raise ValueError("midi_cpu_limit")
        if type(self.soundfont_stereo) is not bool:
            raise ValueError("soundfont_stereo")
        if self.recording_format not in ("float32", "pcm32", "pcm24", "pcm16"):
            raise ValueError("recording_format")
        if type(self.recording_dither) is not bool or type(self.recording_rf64) is not bool:
            raise ValueError("recording")
        if self.recording_container not in RECORDING_EXTENSIONS:
            raise ValueError("recording_container")
        if type(self.recording_timestamp) is not bool:
            raise ValueError("recording_timestamp")
        if type(self.recording_directory) is not str or any(ord(c) < 32 for c in self.recording_directory):
            raise ValueError("recording_directory")
        if not valid_recording_basename(self.recording_basename):
            raise ValueError("recording_basename")
        for key, maximum, minimum in (("recording_compression", 8, 0), ("recording_vorbis_quality", 10, -1),
                                      ("recording_mp3_quality", 9, 0), ("recording_opus_complexity", 10, 0)):
            if type(getattr(self, key)) is not int or not minimum <= getattr(self, key) <= maximum:
                raise ValueError(key)
        if self.recording_bitrate not in RECORDING_BITRATES or self.recording_rate_mode not in ("vbr", "cbr"):
            raise ValueError("recording_rate")
        # bassmidi.md BASS_ATTRIB_MIDI_SRC: 0=linear, 1=8-point sinc,
        # 2=16-point sinc. Leave -1/no interpolation out of this UI.
        if self.interpolation not in (0, 1, 2):
            raise ValueError("interpolation")
        if type(self.default_reverb_send) is not int or not 0 <= self.default_reverb_send <= 127:
            raise ValueError("default_reverb_send")
        if self.soundfont_fx_policy not in ("sum", "minimum", "midi_only"):
            raise ValueError("soundfont_fx_policy")
        if type(self.priority) is not str or self.priority not in PRIORITY_CLASSES:
            raise ValueError("priority")
        if type(self.midi_async) is not bool:
            raise ValueError("midi_async")
        if type(self.auto_reset_delay_s) is not int or not 0 <= self.auto_reset_delay_s <= 600:
            raise ValueError("auto_reset_delay_s")
        if type(self.loading_mode) is not int or self.loading_mode not in (0, 1, 2):
            raise ValueError("loading_mode")
        for key in ("startup_with_windows", "start_minimized", "minimize_to_tray", "close_to_tray",
                    "windows_midi_default", "cc6_direct_pitch_range", "cc94_custom_delay_send"):
            if type(getattr(self, key)) is not bool:
                raise ValueError(key)
        return self


def preferences_path() -> Path:
    return settings_path()


def restore_preference_fields(base, values, names, *, section, report=None):
    """One typed admission boundary for independent current preferences.

    Missing/invalid fields retain the supplied factory value. No aliases,
    old-version transforms or unknown attributes reach the dataclass.
    """
    accepted = {}
    for name in names:
        raw = values.get(name)
        disposition = 'missing_default'
        if raw is not None:
            try:
                if not isinstance(raw,str): raise TypeError('settings text')
                original = getattr(base,name)
                if type(original) is bool:
                    value = ConfigParser.BOOLEAN_STATES[raw.casefold()]
                elif type(original) is int:
                    value = int(raw,10)
                else:
                    value = raw
                replace(base,**{name:value}).validate()
                accepted[name] = value
                disposition = 'accepted'
            except (ValueError,TypeError,KeyError,OverflowError):
                disposition = 'invalid'
        if report is not None: report.record(section,name,disposition)
    if report is not None:
        for name in values.keys()-set(names): report.record(section,name,'unknown_preserved')
    return replace(base,**accepted)


def load_engine_preferences(path: Path | None = None, *, report=None) -> EnginePreferences:
    try:
        parser = read_ini(path,report=report)
    except ValueError:
        # A syntactically unreadable file has no trustworthy field boundaries.
        if report is not None: report.record('file','syntax','invalid')
        return EnginePreferences()
    defaults = EnginePreferences()
    if not parser.has_section("Engine"):
        return defaults
    values = dict(parser.items("Engine"))
    return restore_preference_fields(defaults, values,
        tuple(field.name for field in fields(EnginePreferences)),section='Engine',report=report)


def save_engine_preferences(settings: EnginePreferences, path: Path | None = None) -> None:
    values = engine_ini_values(settings)
    update_sections({"Engine": values}, path, owned_keys={"Engine": set(values)})


def engine_ini_values(settings: EnginePreferences) -> dict[str, str]:
    settings.validate()
    return {
        "main_log_level": settings.main_log_level,
        "main_log_detail": settings.main_log_detail,
        "sample_rate": str(settings.sample_rate),
        "synthesis_mode": settings.synthesis_mode,
        "buffer_ms": str(settings.buffer_ms),
        "shared_update_ms": str(settings.shared_update_ms),
        "exclusive_block_ms": str(settings.exclusive_block_ms),
        "voices": str(settings.voices),
        "midi_cpu_limit": str(settings.midi_cpu_limit),
        "soundfont_stereo": "1" if settings.soundfont_stereo else "0",
        "recording_format": settings.recording_format,
        "recording_dither": "1" if settings.recording_dither else "0",
        "recording_rf64": "1" if settings.recording_rf64 else "0",
        "recording_container": settings.recording_container,
        "recording_directory": settings.recording_directory,
        "recording_basename": settings.recording_basename,
        "recording_timestamp": "1" if settings.recording_timestamp else "0",
        "recording_compression": str(settings.recording_compression),
        "recording_vorbis_quality": str(settings.recording_vorbis_quality),
        "recording_mp3_quality": str(settings.recording_mp3_quality),
        "recording_opus_complexity": str(settings.recording_opus_complexity),
        "recording_bitrate": str(settings.recording_bitrate),
        "recording_rate_mode": settings.recording_rate_mode,
        "interpolation": str(settings.interpolation),
        "default_reverb_send": str(settings.default_reverb_send),
        "soundfont_fx_policy": settings.soundfont_fx_policy,
        "priority": settings.priority,
        "midi_async": "1" if settings.midi_async else "0",
        "cc6_direct_pitch_range": "1" if settings.cc6_direct_pitch_range else "0",
        "cc94_custom_delay_send": "1" if settings.cc94_custom_delay_send else "0",
        "auto_reset_delay_s": str(settings.auto_reset_delay_s),
        "loading_mode": str(settings.loading_mode),
        "startup_with_windows": "1" if settings.startup_with_windows else "0",
        "start_minimized": "1" if settings.start_minimized else "0",
        "minimize_to_tray": "1" if settings.minimize_to_tray else "0",
        "close_to_tray": "1" if settings.close_to_tray else "0",
        "windows_midi_default": "1" if settings.windows_midi_default else "0",
    }


def recording_filename(preferences, *, container=None, started=None):
    """One name rule shared by preferences preview and the real save dialog."""
    container = container or preferences.recording_container
    suffix = "_" + time.strftime("%Y-%m-%d_%H-%M-%S", started or time.localtime()) if preferences.recording_timestamp else ""
    return f"{preferences.recording_basename}{suffix}.{RECORDING_EXTENSIONS[container]}"


def recording_capture_options(preferences):
    """One settings-to-native capture mapping for UI startup and audits."""
    return {"format": preferences.recording_format, "container": preferences.recording_container,
            "dither": preferences.recording_dither, "rf64": preferences.recording_rf64,
            "compression": preferences.recording_compression, "vorbis_quality": preferences.recording_vorbis_quality,
            "mp3_quality": preferences.recording_mp3_quality, "opus_complexity": preferences.recording_opus_complexity,
            "bitrate": preferences.recording_bitrate, "rate_mode": preferences.recording_rate_mode}


EXPORT_QUALITY_FIELDS = ("sample_rate", "recording_container", "recording_format",
    "recording_dither", "recording_rf64", "recording_compression",
    "recording_vorbis_quality", "recording_mp3_quality", "recording_opus_complexity",
    "recording_bitrate", "recording_rate_mode")


def load_export_preferences(base, path=None, *, report=None):
    """Independent export defaults, with the same validated codec domains."""
    parser = read_ini(path,report=report)
    values = dict(parser.items("MIDI Export")) if parser.has_section("MIDI Export") else {}
    return restore_preference_fields(base,values,EXPORT_QUALITY_FIELDS,
                                     section='MIDI Export',report=report)


def save_export_preferences(preferences, path=None):
    values = engine_ini_values(preferences)
    update_sections({"MIDI Export": {name: values[name] for name in EXPORT_QUALITY_FIELDS}},
                    path, owned_keys={"MIDI Export": set(EXPORT_QUALITY_FIELDS)})


def recording_output_rate(container, source_rate):
    if container == "opus":
        return 48000
    if container == "mp3":
        # MPEG1 permits every exposed64..320kb/s setting. Lower-rate MPEG2/2.5
        # silently clamps LAME bitrates (eg11025Hz/192kb/s becomes64kb/s).
        return 44100 if source_rate < 32000 else min(source_rate, 48000)
    return source_rate


def recording_rate_supported(container, source_rate):
    """Shared task/runtime eligibility for qualified product domains.

    None means unqualified, not failed/zero. This is not a codec-wide upper
    bound. Unknown callers still receive the native encoder's own result.
    """
    if container not in RECORDING_EXTENSIONS or source_rate not in SAMPLE_RATES:return None
    return source_rate not in RECORDING_RATE_REJECTIONS.get(container,())


def valid_recording_basename(value):
    """Windows-safe filename stem, independent of presentation validation."""
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        return False
    if value.endswith((" ", ".")) or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in value):
        return False
    stem = value.split(".", 1)[0].upper()
    # Microsoft file namespace also reserves superscript 1/2/3 as digits.
    device_digits = (*range(1, 10), "\u00b9", "\u00b2", "\u00b3")
    return stem not in ("CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in device_digits), *(f"LPT{i}" for i in device_digits))


def import_bmsynth_engine_values(
    current: EnginePreferences, bms: dict[str, int | str], *, neutralize_extensions=False,
) -> tuple[EnginePreferences, int]:
    """Map verified source fields and its implicit channel-reverb seed."""
    changes: dict[str, int] = {}
    # Source 0047A038/00476A75 seeds channel CC91 with 40 regardless of
    # the stored DefReverb field. This is an implicit source value, not a
    # conversion from DefReverb=20; preserve existing CC until channel reset.
    if current.default_reverb_send != 40:
        changes["default_reverb_send"] = 40
    if neutralize_extensions:
        # Original 0046B0DA/0046AF4A never sets BASS_MIDI_ASYNC, and the
        # four traced FontInit paths pass flags=0 (SoundFont+MIDI sends sum).
        if current.midi_async:
            changes["midi_async"] = False
        if current.soundfont_fx_policy != "sum":
            changes["soundfont_fx_policy"] = "sum"
        if current.midi_cpu_limit:
            changes["midi_cpu_limit"] = 0
        if current.soundfont_stereo:
            changes["soundfont_stereo"] = False
    if bms.get("SampleRate") in SAMPLE_RATES:
        changes["sample_rate"] = int(bms["SampleRate"])
    # BMSynth's 1000 is its legacy sentinel/default, not a user override for
    # ABMSynth.  Only an explicitly smaller value is imported; the native
    # default remains100000 and values >=1000 are intentionally ignored.
    if "Voices" in bms and 0 < int(bms["Voices"]) < 1000:
        changes["voices"] = int(bms["Voices"])
    if "Latency" in bms:
        changes["buffer_ms"] = int(bms["Latency"])
    if "UseSinc" in bms:
        # BMSynth's BASS_MIDI_SINCINTER creation flag reads back as
        # BASS_ATTRIB_MIDI_SRC=1 (8-point) on the active BASSMIDI DLL.
        # Its absence means no interpolation change, not forced linear.
        changes["interpolation"] = 1 if bms["UseSinc"] else 0
    # Deliberately ignore DefReverb. Original 1.6.9 probes read its INI
    # value but still initialized channel CC91 to 40 in every tested case.
    if "AutoReset" in bms:
        changes["auto_reset_delay_s"] = (
            int(bms.get("AutoResetDelay", current.auto_reset_delay_s or 10))
            if bms["AutoReset"] else 0
        )
    elif "AutoResetDelay" in bms and current.auto_reset_delay_s:
        changes["auto_reset_delay_s"] = int(bms["AutoResetDelay"])
    if "LoadingType" in bms:
        changes["loading_mode"] = int(bms["LoadingType"])
    return replace(current, **changes).validate(), len(changes)


# Read-only BMSynth compatibility import.

_FIELDS = {
    "BMS": {
        "SampleRate": (11025, 96000), "Voices": (32, 1000),
        "Latency": (50, 2000), "UseSinc": (0, 1),
        "AutoReset": (0, 1), "AutoResetDelay": (5, 600),
        "LoadingType": (0, 2),
        "Volume": (0, 32), "GlobalReverb": (0, 32), "GlobalChorus": (0, 32),
        "StereoEh": (0, 32),
        "OverNote": (0, 1), "UseGSReset": (0, 1),
    },
    "Reverb": {
        "Preset": (0, 18), "Level": (0, 200), "Time": (0, 6000),
        "Delay": (0, 1000), "HighCutoff": (0, 11200), "LowCutoff": (0, 11200),
    },
    "Chorus": {
        "Preset": (0, 20), "Level": (0, 200), "Delay": (0, 1000),
        "Depth": (0, 1000), "Rate": (0, 1000), "FeedBack": (0, 200),
        "ToReverb": (0, 200),
    },
    "Delay": {
        "Preset": (0, 5), "DelayType": (0, 3), "Level": (0, 200),
        "Time": (1, 2000), "FeedBack": (0, 100), "UseDelaySysEx": (0, 1),
    },
    "Boost": {"Use": (0, 1), "Bass": (-15, 15), "Treble": (-15, 15)},
    "DXReverb": {"Use": (0, 1), "Level": (0, 21)},
    "VelocityRate": {"Use": (0, 1), "Level": (0, 200)},
}


def read_bmsynth_ini(path):
    """Return validated values and localized warnings; never write *path*."""
    parser = ConfigParser(interpolation=None)
    parser.optionxform = str
    last_error = None
    for encoding in ("utf-8-sig", "cp949"):
        parser.clear()
        try:
            with open(path, "r", encoding=encoding, errors="strict") as source:
                parser.read_file(source)
            break
        except (OSError, UnicodeError, ConfigError) as exc:
            last_error = exc
    else:
        raise ValueError(tr("error.ini_read", error=last_error)) from last_error

    values, warnings = {}, []
    for section, fields in _FIELDS.items():
        if not parser.has_section(section):
            warnings.append(tr("error.ini_section_missing", section=section))
            continue
        section_values = {}
        for name, (minimum, maximum) in fields.items():
            if not parser.has_option(section, name):
                continue
            raw = parser.get(section, name)
            try:
                value = int(raw.strip(), 10)
            except ValueError:
                warnings.append(tr("error.ini_not_integer", section=section, name=name, value=raw))
                continue
            if not minimum <= value <= maximum:
                warnings.append(tr(
                    "error.ini_out_of_range", section=section, name=name, value=value,
                    minimum=minimum, maximum=maximum,
                ))
                continue
            if section == "BMS" and name == "SampleRate" and value not in (11025, 22050, 44100, 48000, 96000):
                warnings.append(tr("error.ini_unsupported_rate", value=value))
                continue
            section_values[name] = value
        if section_values:
            values[section] = section_values
    if parser.has_section("BMS") and parser.has_option("BMS", "SoundFont"):
        soundfont = parser.get("BMS", "SoundFont").strip()
        if soundfont and soundfont.lower().endswith((".sf2", ".sf3", ".sfz", ".sfg")):
            values.setdefault("BMS", {})["SoundFont"] = soundfont
        elif soundfont:
            warnings.append(tr("error.ini_unsupported_soundfont", value=soundfont))
    return values, warnings


def bmsynth_global_position_to_midi_send(position: int | float) -> int:
    """Original 0..32 GlobalReverb/Chorus position -> MIDI 0..127 send.

    BMSynth 0046BBEC/0046BC40: ``min(127, Position << 2)``.
    """
    position = max(0, min(32, int(round(float(position)))))
    return min(127, position * 4)


def bmsynth_stereo_position_to_multiplier(position: int | float) -> float:
    """Original StereoEh 0..32 -> actual Mid/Side width multiplier."""
    position = max(0, min(32, int(round(float(position)))))
    return 1.0 + position / 16.0

