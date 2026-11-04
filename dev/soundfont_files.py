"""SoundFont group data, .sfg persistence, and bounded filesystem discovery.

This module never owns BASS handles. Scanning may run off the GUI thread;
its results and group models are plain Python data.
"""

from __future__ import annotations

import os
import tempfile
import time
import hashlib
import json
import copy
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


# BASSMIDI FontInit's documented SF2 CC21..24 destinations. A record in the
# file is evidence of author intent, not proof that this engine supports it.
SF2_CC_DESTINATIONS = frozenset((8,48,34,36,38,30,11))
SF2_CC_GENERATOR_NAMES={0:'startAddrsOffset',1:'endAddrsOffset',2:'startloopAddrsOffset',3:'endloopAddrsOffset',
    4:'startAddrsCoarseOffset',5:'modLfoToPitch',6:'vibLfoToPitch',7:'modEnvToPitch',8:'initialFilterFc',
    9:'initialFilterQ',10:'modLfoToFilterFc',11:'modEnvToFilterFc',13:'modLfoToVolume',
    12:'endAddrsCoarseOffset',15:'chorusEffectsSend',16:'reverbEffectsSend',17:'pan',
    21:'delayModLFO',22:'freqModLFO',23:'delayVibLFO',24:'freqVibLFO',25:'delayModEnv',
    26:'attackModEnv',27:'holdModEnv',28:'decayModEnv',29:'sustainModEnv',30:'releaseModEnv',
    33:'delayVolEnv',34:'attackVolEnv',35:'holdVolEnv',36:'decayVolEnv',37:'sustainVolEnv',
    31:'keynumToModEnvHold',32:'keynumToModEnvDecay',38:'releaseVolEnv',39:'keynumToVolEnvHold',
    40:'keynumToVolEnvDecay',41:'instrument',43:'keyRange',44:'velRange',45:'startloopAddrsCoarseOffset',
    46:'keynum',47:'velocity',48:'initialAttenuation',50:'endloopAddrsCoarseOffset',
    51:'coarseTune',52:'fineTune',53:'sampleID',54:'sampleModes',56:'scaleTuning',
    57:'exclusiveClass',58:'overridingRootKey'}


@dataclass(frozen=True)
class SoundFontControllerLink:
    source: int
    destination: int
    amount: int
    secondary: int
    transform: int
    level: str
    keys: tuple[int,int]
    velocities: tuple[int,int]

    @property
    def cc(self):return self.source & 127

    @property
    def qualification(self):
        if self.destination not in SF2_CC_DESTINATIONS:return 'unsupported'
        if self.secondary:return 'secondary'
        if self.transform or self.source>>10 not in (0,1,2,3):return 'curve'
        return 'active' if self.amount else 'zero'


def _read_soundfont_hydra(file,formats,cancelled):
    """Bounded shared RIFF metadata reader. Never reads/decodes sample PCM."""
    file.seek(0);header=file.read(12)
    if len(header)!=12 or header[:4]!=b'RIFF' or header[8:]!=b'sfbk':return {},'format'
    limit=struct.unpack_from('<I',header,4)[0]+8
    if limit>os.fstat(file.fileno()).st_size:raise ValueError('RIFF bounds')
    tables={};position=12;metadata_bytes=0
    while position+8<=limit:
        if cancelled():return {},'cancelled'
        file.seek(position);tag,length=struct.unpack('<4sI',file.read(8));end=position+8+length
        if end>limit:raise ValueError('chunk bounds')
        if tag==b'LIST' and length>=4 and file.read(4)==b'pdta':
            at=position+12
            while at+8<=end:
                if cancelled():return {},'cancelled'
                file.seek(at);name,size=struct.unpack('<4sI',file.read(8))
                if at+8+size>end:raise ValueError('pdta bounds')
                if name in formats:
                    metadata_bytes+=size
                    if metadata_bytes>16*1024*1024:return {},'limit'
                    unit=struct.calcsize(formats[name])
                    if size%unit or name in tables:raise ValueError('hydra record size/duplicate')
                    tables[name]=list(struct.iter_unpack(formats[name],file.read(size)))
                at+=8+size+(size&1)
        position=end+(length&1)
    if set(tables)!=set(formats) or any(not value for value in tables.values()):raise ValueError('missing hydra')
    return tables,'ready'


def inspect_soundfont_pitch(path,cancelled=lambda:False):
    """SF2/SF3 author-declared unpitched presets,not an acoustic classifier.

    E-mu SF2 2.04 shdr.byOriginalPitch=255 explicitly denotes unpitched sound.
    Admit a preset only if every reachable key/velocity zone (including stereo
    partners) is so marked. Mixed/unknown/malformed sources remain included.
    Run only during source preparation,never on a note/audio/GUI observation.
    """
    try:
        with Path(path).open('rb') as file:
            before=os.fstat(file.fileno())
            tables,state=_read_soundfont_hydra(file,{b'phdr':'<20sHHHIII',b'pbag':'<HH',b'pgen':'<HH',
                b'inst':'<20sH',b'ibag':'<HH',b'igen':'<HH',b'shdr':'<20sIIIIIBbHH'},cancelled)
            if state!='ready':return dict(state=state,unpitched=frozenset())
            ph,pb,pg,ih,ib,ig,sh=(tables[k] for k in (b'phdr',b'pbag',b'pgen',b'inst',b'ibag',b'igen',b'shdr'))
            def zones(bags,gens,start,stop):
                if not 0<=start<=stop<len(bags):raise ValueError('zone range')
                result=[]
                for index in range(start,stop):
                    a,b=bags[index][0],bags[index+1][0]
                    if not 0<=a<=b<=len(gens):raise ValueError('bag indices')
                    result.append(dict(gens[a:b]))
                return result
            def span(gens,op):
                value=gens.get(op,0x7f00);lo,hi=value&255,value>>8
                if not 0<=lo<=hi<=127:raise ValueError('zone key/velocity')
                return lo,hi
            unpitched=set();count=0
            for index,preset in enumerate(ph[:-1]):
                if cancelled():return dict(state='cancelled',unpitched=frozenset())
                pzones=zones(pb,pg,preset[3],ph[index+1][3]);pglobal={}
                if pzones and 41 not in pzones[0]:pglobal=pzones.pop(0)
                flags=[]
                for pzone in pzones:
                    instrument=pzone.get(41,-1)
                    if not 0<=instrument<len(ih)-1:raise ValueError('instrument index')
                    izones=zones(ib,ig,ih[instrument][1],ih[instrument+1][1]);iglobal={}
                    if izones and 53 not in izones[0]:iglobal=izones.pop(0)
                    for izone in izones:
                        count+=1
                        if count>100000:return dict(state='limit',unpitched=frozenset())
                        if count%128==0 and cancelled():return dict(state='cancelled',unpitched=frozenset())
                        pvalues={**pglobal,**pzone};ivalues={**iglobal,**izone}
                        if any(max(span(pvalues,op)[0],span(ivalues,op)[0])>
                            min(span(pvalues,op)[1],span(ivalues,op)[1]) for op in (43,44)):continue
                        sample=izone.get(53,-1)
                        if not 0<=sample<len(sh)-1:raise ValueError('sample index')
                        row=sh[sample];flag=row[6]==255
                        if (row[9]&15) in (2,4,8):
                            linked=row[8]
                            flag=flag and 0<=linked<len(sh)-1 and sh[linked][6]==255
                        flags.append(flag)
                if flags and all(flags):unpitched.add((preset[2],preset[1]))
            after=os.fstat(file.fileno())
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):return dict(state='changed',unpitched=frozenset())
            return dict(state='ready',unpitched=frozenset(unpitched))
    except (OSError,ValueError,struct.error,IndexError):
        return dict(state='error',unpitched=frozenset())


def inspect_soundfont_controllers(path, cancelled=lambda:False):
    """Read only SF2 hydra metadata, never PCM or native handles.

    E-mu SF2 2.04 sections7.1/9: pmod/imod <HHhHH>, CC source bit7,
    local-over-global identical modulator override, preset/instrument levels,
    key/velocity zone intersection. Do not guess BASS's winner for different
    destinations/types. Limits bound this inspector, not playable SoundFonts.
    Caller runs file parsing off the GUI and real-time threads.
    """
    path=Path(path)
    try:
        with path.open('rb') as file:
            before=os.fstat(file.fileno())
            formats={b'phdr':'<20sHHHIII',b'pbag':'<HH',b'pmod':'<HHhHH',b'pgen':'<HH',
                     b'inst':'<20sH',b'ibag':'<HH',b'imod':'<HHhHH',b'igen':'<HH'}
            tables,state=_read_soundfont_hydra(file,formats,cancelled)
            if state!='ready':return dict(state=state,presets={})
            ph,pb,pm,pg=(tables[k] for k in (b'phdr',b'pbag',b'pmod',b'pgen'))
            ih,ib,im,ig=(tables[k] for k in (b'inst',b'ibag',b'imod',b'igen'))
            def zones(bags,gens,mods,start,stop):
                if not 0<=start<=stop<len(bags):raise ValueError('zone range')
                result=[]
                for index in range(start,stop):
                    g,m=bags[index];gn,mn=bags[index+1]
                    if not (0<=g<=gn<=len(gens) and 0<=m<=mn<=len(mods)):raise ValueError('bag indices')
                    result.append((dict(gens[g:gn]),{(s,d,a,t):amount for s,d,amount,a,t in mods[m:mn]}))
                return result
            def span(gens,op):
                value=gens.get(op,0x7f00);lo,hi=value&255,value>>8
                if not 0<=lo<=hi<=127:raise ValueError('zone key/velocity')
                return lo,hi
            def intersection(*spans):return max(s[0] for s in spans),min(s[1] for s in spans)
            presets={};link_count=0;zone_count=0
            for index,preset in enumerate(ph[:-1]):
                if cancelled():return dict(state='cancelled',presets={})
                preset_zones=zones(pb,pg,pm,preset[3],ph[index+1][3]);global_g,global_m={},{}
                if preset_zones and 41 not in preset_zones[0][0]:global_g,global_m=preset_zones.pop(0)
                links=[]
                for local_g,local_m in preset_zones:
                    if 41 not in local_g:raise ValueError('preset instrument')
                    instrument=local_g[41]
                    if not 0<=instrument<len(ih)-1:raise ValueError('instrument index')
                    instrument_zones=zones(ib,ig,im,ih[instrument][1],ih[instrument+1][1]);iglob_g,iglob_m={},{}
                    if instrument_zones and 53 not in instrument_zones[0][0]:iglob_g,iglob_m=instrument_zones.pop(0)
                    for zone_g,zone_m in instrument_zones:
                        zone_count+=1
                        if zone_count>100000:return dict(state='limit',presets={})
                        if zone_count%128==0 and cancelled():return dict(state='cancelled',presets={})
                        if 53 not in zone_g:raise ValueError('instrument sample')
                        keys=intersection(span({**global_g,**local_g},43),span({**iglob_g,**zone_g},43))
                        velocities=intersection(span({**global_g,**local_g},44),span({**iglob_g,**zone_g},44))
                        if keys[0]>keys[1] or velocities[0]>velocities[1]:continue
                        for level,modulators in (('preset',{**global_m,**local_m}),('instrument',{**iglob_m,**zone_m})):
                            for (source,destination,secondary,transform),amount in modulators.items():
                                if not source&128 or source&127 not in (21,22,23,24):continue
                                links.append(SoundFontControllerLink(source,destination,amount,secondary,transform,level,keys,velocities))
                                link_count+=1
                                if link_count>100000:return dict(state='limit',presets={})
                presets[(preset[2],preset[1])]=tuple(dict.fromkeys(links))
            after=os.fstat(file.fileno())
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):return dict(state='changed',presets={})
            return dict(state='ready',presets=presets,identity=(after.st_size,after.st_mtime_ns))
    except (OSError,ValueError,struct.error,IndexError):
        return dict(state='error',presets={})

from i18n import tr


def asset_file_identity(path,cancelled=None):
    """Worker/control-load only identity, never a frame/callback operation."""
    path=Path(path).resolve()
    with path.open('rb') as source:
        before=os.fstat(source.fileno()); digest=hashlib.sha256()
        while block:=source.read(1024*1024):
            if cancelled is not None and cancelled(): raise RuntimeError(tr('player.export_cancelled'))
            digest.update(block)
        after=os.fstat(source.fileno())
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise RuntimeError(tr('player.asset_changed',path=path))
    return dict(path=str(path),bytes=after.st_size,mtime_ns=after.st_mtime_ns,sha256=digest.hexdigest())


def soundfont_asset_identity(path, cancelled=None, *, sfz_header=b''):
    """Root and SFZ file dependencies at load/export time, never in a callback.

    FontInit's documented sample/default_path/#define/#include file model is
    observed independently of BASS's cache. Unknown syntax and mem: samples
    are explicitly unqualified, not silently called reproducible. Bounds here
    limit this observation, NOT the fonts accepted by the native synthesizer.
    """
    root = Path(path).resolve()
    identity = asset_file_identity(root, cancelled)
    if root.suffix.casefold() != '.sfz':
        return identity
    files, edges, reasons, macros, stack = {str(root): identity}, [], [], {}, []
    if sfz_header:
        reasons.append('process SFZ header not serialized by product')
    text_bytes = 0
    def uncertain(reason):
        if reason not in reasons: reasons.append(reason)
    def expand(value):
        for _ in range(32):
            updated = re.sub(r'\$[A-Za-z_][\w]*', lambda m: macros.get(m[0], m[0]), value)
            if updated == value: break
            value = updated
        if re.search(r'\$[A-Za-z_][\w]*', value): uncertain('unresolved macro')
        return value
    def file_path(base, name):
        # SFZ's paths accept both separators on the active Windows platform.
        candidate = Path(name.replace('\\', '/'))
        return (candidate if candidate.is_absolute() else base / candidate).resolve()
    def record(target, source, kind, literal):
        target = str(target)
        edges.append(dict(source=str(source), target=target, kind=kind, literal=literal))
        if target not in files: files[target] = asset_file_identity(target, cancelled)
    def preprocess(source, supplied=None):
        nonlocal text_bytes
        if cancelled is not None and cancelled(): raise RuntimeError(tr('player.export_cancelled'))
        if len(stack) >= 32 or len(files) >= 4096:
            uncertain('dependency observation bound'); return ''
        if source in stack:
            uncertain('include cycle'); return ''
        data = source.read_bytes() if supplied is None else supplied
        text_bytes += len(data)
        if text_bytes > 32 * 1024 * 1024:
            uncertain('text observation bound'); return ''
        try: text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            uncertain('non UTF-8 SFZ text'); return ''
        stack.append(source)
        try:
            # Remove comments before macro expansion. Strings containing these
            # tokens are uncommon but must not produce false certification.
            if re.search(r'(?:sample|default_path)\s*=.*(?:/\*|\s//)', text):
                uncertain('ambiguous comment in path')
            text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
            output = []
            for line in text.splitlines():
                line = re.sub(r'(^|\s)//.*', r'\1', line).strip()
                if not line: continue
                define = re.fullmatch(r'#define\s+(\$[A-Za-z_][\w]*)\s+(.+)', line)
                include = re.fullmatch(r'#include\s+"([^"]+)"\s*', line)
                if define:
                    if define[1] in macros: uncertain('macro redefinition semantics')
                    value = expand(define[2])
                    if any(character.isspace() for character in value):
                        # Native2.4.16 accepts the directive but only its first
                        # token is effective; do not certify a different path.
                        uncertain('whitespace macro value')
                        value = value.split()[0]
                    macros[define[1]] = value; continue
                if include:
                    name = expand(include[1])
                    if '$' in name: continue
                    # Active BASSMIDI resolves nested includes from the root
                    # SFZ folder (native corpus: parent-relative nested include
                    # fails7000; root-relative nested include succeeds).
                    target = file_path(root.parent, name)
                    record(target, source, 'include', name)
                    output.append(preprocess(target)); continue
                if line.startswith('#'):
                    uncertain('unsupported preprocessor'); continue
                output.append(expand(line))
            return '\n'.join(output)
        finally: stack.pop()
    header = preprocess(root, sfz_header) if sfz_header else ''
    text = header + '\n' + preprocess(root)
    # Values extend to the next opcode/header, so ordinary unquoted filenames
    # with spaces are retained. This is an asset observer, not an SFZ engine.
    tokens = list(re.finditer(r'<[^>]*>|(?<!\S)([A-Za-z_][\w]*)\s*=', text))
    prefix, controls = '', 0
    for index, token in enumerate(tokens):
        if token[1] is None:
            if token[0].casefold() == '<control>':
                controls += 1
                if controls > 1: uncertain('multiple control default_path semantics')
            continue
        name = token[1].casefold()
        if name not in ('sample', 'default_path'): continue
        value = text[token.end():tokens[index+1].start() if index+1<len(tokens) else len(text)].strip()
        if not value or '$' in value:
            uncertain('unresolved asset path'); continue
        if value.startswith('"'):
            # Quoted asset values are not part of the qualified native corpus.
            uncertain('quoted opcode path'); value = value.strip('"')
        if name == 'default_path': prefix = value; continue
        if value.casefold().startswith('mem:') or value.startswith('*'):
            uncertain('non-file sample'); continue
        if '://' in value:
            uncertain('non-filesystem sample'); continue
        target = file_path(root.parent, prefix + value)
        record(target, root, 'sample', prefix + value)
    for target, prior in files.items():
        if asset_file_identity(target, cancelled) != prior:
            raise RuntimeError(tr('player.asset_changed', path=target))
    result = dict(identity)
    result['dependencies'] = [dict(files[target]) for target in sorted(files) if target != str(root)]
    result['resolution'] = dict(schema=1, qualified=not reasons, reasons=reasons,
                                edges=edges, sfz_header_sha256=hashlib.sha256(sfz_header).hexdigest())
    return result


def stage_sfz_asset(identity, cancelled=None):
    """Private file generation defeats shared stale samples without rewriting SFZ.

    Only a fully qualified, root-contained graph can be copied unchanged.
    Absolute/outside paths cannot be safely redirected without rewriting the
    user's synthesis program, so they retain the explicit reload refusal.
    The caller must retain this directory until BASS_MIDI_FontFree succeeds.
    """
    if not identity.get('resolution',{}).get('qualified'):
        return None
    root = Path(identity['path']); base = root.parent
    sources = [identity, *identity.get('dependencies',())]
    relatives = []
    for item in sources:
        try: relatives.append(Path(item['path']).relative_to(base))
        except ValueError: return None
    # An absolute literal continues to point outside the copy even if its file
    # happens to lie under base. Never qualify such a staged reload.
    for edge in identity['resolution']['edges']:
        if Path(edge['literal'].replace('\\','/')).is_absolute(): return None
    stage = tempfile.TemporaryDirectory(prefix='abmsynth-sfz-generation-')
    try:
        for item, relative in zip(sources,relatives):
            target = Path(stage.name)/relative; target.parent.mkdir(parents=True,exist_ok=True)
            digest=hashlib.sha256()
            with Path(item['path']).open('rb') as source, target.open('wb') as output:
                while block:=source.read(1024*1024):
                    if cancelled is not None and cancelled(): raise RuntimeError(tr('player.export_cancelled'))
                    digest.update(block); output.write(block)
            if digest.hexdigest()!=item['sha256'] or target.stat().st_size!=item['bytes']:
                raise RuntimeError(tr('player.asset_changed',path=item['path']))
        return stage, Path(stage.name)/root.name
    except BaseException:
        stage.cleanup(); raise


SFG_VERSION = "1.6.8"
# Shared application range for per-preset/channel MIDI_EVENT_MIXLEVEL.
# 100 is unity, 400 is amplitude x4. This is not MIDI CC#7's 0..127 range.
INSTRUMENT_VOLUME_MAX = 400
# Optional application preset seeds, not edits to SF2 sample generators.
# Values use the same display domains as the channel editor; missing means
# inherit native behavior. A stored zero remains an explicit assignment.
PRESET_EXPRESSION_GROUPS = {
    "tuning": ("fine_tuning", "coarse_tuning"),
    "vibrato": ("vibrato_rate", "vibrato_depth", "vibrato_delay"),
    **{source: tuple(f"{source}_{target}" for target in ("vibrato", "pitch", "filter", "volume"))
       for source in ("modulation", "channel_pressure", "key_pressure")},
}
PRESET_EXPRESSION_RANGES = {
    "fine_tuning": (-100, 100, 0), "coarse_tuning": (-24, 24, 0),
    **{f"vibrato_{target}": (0, 127, 64) for target in ("rate", "depth", "delay")},
    **{f"{source}_{target}": (0, 200, 50 if source != "key_pressure" else 0)
       if target == "vibrato" else (-24, 24, 0) if target == "pitch" else
       (-9600, 9600, 0) if target == "filter" else (-100, 100, 0)
       for source in ("modulation", "channel_pressure", "key_pressure")
       for target in ("vibrato", "pitch", "filter", "volume")},
}


def effective_preset_expression(override):
    """One active seed view for runtime/default comparisons; stored Off data stays."""
    if override.get("disabled"):
        return {}
    inactive = set(override.get("extended_disabled") or ())
    hidden = {name for section in inactive for name in PRESET_EXPRESSION_GROUPS.get(section, ())}
    return {name: value for name, value in (override.get("extended") or {}).items()
            if name in PRESET_EXPRESSION_RANGES and name not in hidden}
SUPPORTED_FONT_SUFFIXES = (".sf2", ".sf3", ".sfz")
SUPPORTED_SOUNDFONT_LOAD_SUFFIXES = (*SUPPORTED_FONT_SUFFIXES, ".sfg")
RECENT_SOUNDFONT_LIMIT = 12


def soundfont_load_filter():
    """One format identity for the native picker and external-file drop."""
    return "SoundFont ("+" ".join('*'+suffix for suffix in SUPPORTED_SOUNDFONT_LOAD_SUFFIXES)+")"


def soundfont_path_key(path: str | Path) -> str:
    """Compare saved paths without touching slow or disconnected volumes."""
    return os.path.normcase(os.path.abspath(resolve_soundfont_selection_path(path)))


BUNDLED_SOUNDFONT = 'assets/soundfonts/FluidR3.sf2'


def bundled_soundfont_path() -> Path:
    """Resolve the application resource in source and frozen deployments."""
    return Path(__file__).resolve().parent / BUNDLED_SOUNDFONT


def resolve_soundfont_selection_path(path: str | Path) -> str:
    value = str(path).strip()
    if value and os.path.normcase(os.path.normpath(value)) == os.path.normcase(os.path.normpath(BUNDLED_SOUNDFONT)):
        return str(bundled_soundfont_path())
    return value


def stored_soundfont_selection_path(path: str | Path) -> str:
    value = resolve_soundfont_selection_path(path)
    if value and os.path.normcase(os.path.abspath(value)) == os.path.normcase(str(bundled_soundfont_path())):
        return BUNDLED_SOUNDFONT
    return value


def soundfont_selection_exists(path: str | Path) -> bool:
    try:
        return bool(str(path).strip()) and Path(resolve_soundfont_selection_path(path)).is_file()
    except (OSError, ValueError):
        return False


def _startup_soundfont_available(path: str | Path) -> bool:
    if not soundfont_selection_exists(path):
        return False
    candidate = Path(resolve_soundfont_selection_path(path))
    if candidate.suffix.casefold() != '.sfg':
        return True
    try:
        group = load_sfg(candidate)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        # An existing invalid document belongs to the normal loader's error
        # path. Missing source files and invalid syntax are distinct failures.
        return True
    return not group.fonts or any(entry.custom is not None or soundfont_selection_exists(group.font_path(entry))
                                  for entry in group.fonts)


def startup_soundfont_candidates(preferred, library, discovered=()) -> tuple[str, ...]:
    """Keep an available selection. Use the bundle last during recovery.

    An absent preference selects the factory font directly. Recovery checks
    the requested basename, recent files, favourites and discovered files.
    The owned A4 reference is an audition input, never a playback fallback.
    This chooses available files. Normal loaders still report invalid data.
    """
    requested = resolve_soundfont_selection_path(preferred or BUNDLED_SOUNDFONT)
    if _startup_soundfont_available(requested):
        return (requested,)
    found = sorted((str(path) for path in discovered), key=str.casefold)
    matching = [path for path in found if Path(path).name.casefold() == Path(requested).name.casefold()]
    bundle = str(bundled_soundfont_path())
    reference = Path(__file__).resolve().parent / 'assets/tuning-reference.sf2'
    excluded = {soundfont_path_key(bundle), soundfont_path_key(reference)}
    result = []
    seen = set()
    for path in (*matching, *library.recent, *library.favorites, *found):
        value = resolve_soundfont_selection_path(path)
        key = soundfont_path_key(value)
        if key not in seen and key not in excluded and _startup_soundfont_available(value):
            result.append(value)
            seen.add(key)
    if soundfont_selection_exists(bundle):
        result.append(bundle)
    return tuple(result)


def _unique_paths(paths) -> tuple[str, ...]:
    unique = []
    seen = set()
    for path in paths:
        value = str(path).strip()
        if not value:
            continue
        key = soundfont_path_key(value)
        if key not in seen:
            unique.append(value)
            seen.add(key)
    return tuple(unique)


@dataclass(frozen=True)
class SoundFontLibrary:
    """Persisted selector history; hiding wins over discovery and history."""

    recent: tuple[str, ...] = ()
    favorites: tuple[str, ...] = ()
    hidden: tuple[str, ...] = ()

    def normalized(self) -> "SoundFontLibrary":
        hidden = _unique_paths(self.hidden)
        hidden_keys = {soundfont_path_key(path) for path in hidden}
        favorites = tuple(path for path in _unique_paths(self.favorites)
                          if soundfont_path_key(path) not in hidden_keys)
        recent = tuple(path for path in _unique_paths(self.recent)
                       if soundfont_path_key(path) not in hidden_keys)[:RECENT_SOUNDFONT_LIMIT]
        return SoundFontLibrary(recent, favorites, hidden)

    def is_hidden(self, path: str | Path) -> bool:
        key = soundfont_path_key(path)
        return any(soundfont_path_key(item) == key for item in self.hidden)

    def is_favorite(self, path: str | Path) -> bool:
        key = soundfont_path_key(path)
        return any(soundfont_path_key(item) == key for item in self.favorites)

    def mark_used(self, path: str | Path) -> "SoundFontLibrary":
        value = str(path).strip()
        if not value or self.is_hidden(value):
            return self
        return SoundFontLibrary(
            _unique_paths((value, *self.recent))[:RECENT_SOUNDFONT_LIMIT],
            self.favorites, self.hidden,
        ).normalized()

    def manually_add(self, path: str | Path) -> "SoundFontLibrary":
        value = str(path).strip()
        if not value:
            return self
        key = soundfont_path_key(value)
        visible = SoundFontLibrary(
            self.recent, self.favorites,
            tuple(item for item in self.hidden if soundfont_path_key(item) != key),
        )
        return visible.mark_used(value)

    def toggle_favorite(self, path: str | Path) -> "SoundFontLibrary":
        value = str(path).strip()
        if not value:
            return self
        if self.is_hidden(value):
            # A deliberate favorite command on the pinned current row is an
            # explicit re-add, unlike discovery or automatic recent tracking.
            return self.manually_add(value).toggle_favorite(value)
        key = soundfont_path_key(value)
        favorites = (
            tuple(item for item in self.favorites if soundfont_path_key(item) != key)
            if self.is_favorite(value) else (value, *self.favorites)
        )
        return SoundFontLibrary(self.recent, _unique_paths(favorites), self.hidden).normalized()

    def hide(self, path: str | Path) -> "SoundFontLibrary":
        value = str(path).strip()
        if not value:
            return self
        key = soundfont_path_key(value)
        return SoundFontLibrary(
            tuple(item for item in self.recent if soundfont_path_key(item) != key),
            tuple(item for item in self.favorites if soundfont_path_key(item) != key),
            _unique_paths((*self.hidden, value)),
        )


@dataclass
class SoundFontEntry:
    filename: str
    volume: int = 100
    custom: dict | None = None
    runtime_path: str = field(default='',compare=False,repr=False)

    def __post_init__(self):
        if self.custom is not None:self.custom=copy.deepcopy(self.custom)

    @property
    def label(self):
        return self.custom['name'] if self.custom is not None else Path(self.filename).name

    @property
    def source_description(self):
        return tr('creator.group_source',name=self.label) if self.custom is not None else self.filename


@dataclass(frozen=True)
class DrumKeyMix:
    """Document/MIDI neutral values; no audio handles in the data model."""
    level: int = 127
    pan: int = 128  # SoundFont pan, not center.
    reverb: int = 127
    chorus: int = 127
    fine_tuning: int = 64
    coarse_tuning: int = 64
    cutoff: int = 64
    resonance: int = 64
    userfx: int = 127  # NRPN1F: normal/full per-key multiplier for existing delay bus.


DRUM_NEUTRAL = DrumKeyMix()
DRUM_KEY_DOMAINS = {key: (0, 128 if key == "pan" else 127, getattr(DRUM_NEUTRAL, key))
                    for key in DrumKeyMix.__dataclass_fields__}
SCALE_TUNING_DOMAIN = (-100, 100, 0)


@dataclass
class PresetOverride:
    bank: int
    preset: int
    index: int | None = None
    volume: int | None = None
    attack: int | None = None
    decay: int | None = None
    release: int | None = None
    cutoff: int | None = None
    resonance: int | None = None
    memo: str | None = None
    disabled: bool = False
    extended: dict[str, int] = field(default_factory=dict)
    extended_disabled: set[str] = field(default_factory=set)
    performance: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        # Dialog draft/original/runtime models must not share mutable seeds.
        self.extended = dict(self.extended)
        self.extended_disabled = set(self.extended_disabled)
        self.performance = dict(self.performance)


@dataclass(frozen=True)
class SoundFontMapping:
    """One nonzero-LSB MIDI destination -> a real font's source preset.

    LSB0 preset controls remain a separate pair-keyed model. This is
    an explicit product mapping,not an inferred GS/XG bank conversion.
    """
    bank: int
    bank_lsb: int
    program: int
    source_index: int
    source_bank: int
    source_program: int

    @property
    def address(self):return self.bank,self.bank_lsb,self.program

    def valid(self,font_count):
        values=tuple(vars(self).values())
        return (all(type(value) is int for value in values) and
            0<=self.bank<=128 and 1<=self.bank_lsb<=127 and 0<=self.program<=127 and
            0<=self.source_index<font_count and 0<=self.source_bank<=128 and
            0<=self.source_program<=127 and (self.bank==128)==(self.source_bank==128))


@dataclass
class SoundFontGroup:
    fonts: list[SoundFontEntry] = field(default_factory=list)
    presets: list[PresetOverride] = field(default_factory=list)
    source_path: str = ""
    # An absent group profile is neutral. Instrument exceptions inherit the
    # group only;an unrelated INI never supplies instrument defaults.
    performance: dict[str, int] | None = None
    mappings: list[SoundFontMapping] = field(default_factory=list)

    def __post_init__(self):
        self.mappings=list(self.mappings)
        if self.performance is not None:
            self.performance = dict(self.performance)

    @classmethod
    def single_font(cls, filename: str) -> "SoundFontGroup":
        return cls(fonts=[SoundFontEntry(filename)])

    def font_path(self, entry: SoundFontEntry) -> Path:
        """Resolve a relative `.sfg` font name against the group-file folder.

        The source text remains unchanged so saving the group does not expand
        a portable relative path into a machine-specific absolute path.
        """
        if entry.custom is not None:
            # Only load workers materialize the native adapter. GUI/save paths
            # can inspect documents without decoding or writing sample files.
            return Path(entry.runtime_path) if entry.runtime_path else Path('__unprepared_custom__')/entry.filename
        candidate = Path(entry.filename)
        if not self.source_path:
            return Path(resolve_soundfont_selection_path(entry.filename))
        if candidate.is_absolute():
            return candidate
        return Path(self.source_path).parent / candidate

    def preset_map(self) -> dict[tuple[int, int], PresetOverride]:
        """BMSynth resolves duplicate sections by the later section value."""
        return {(item.bank, item.preset): item for item in self.presets}

    def mapping_map(self):return {item.address:item for item in self.mappings}

    def remap_font_indices(self,indices):
        """Drop removed owners and preserve routes when the font order moves."""
        self.mappings=[SoundFontMapping(**{**vars(item),'source_index':indices[item.source_index]})
                       for item in self.mappings if item.source_index in indices]


def resolve_mapped_instruments(group,instruments,bank_lsb,*,system_mode=0):
    """LSB choices preserve base browsing;only exact routes qualify sources.

    BASSMIDI owns fallback and system interpretation. A base entry remains
    selectable but does not prove this LSB's actual source/controller links.
    """
    if not bank_lsb:return resolve_effective_instruments(group,instruments)
    instruments=tuple(instruments)
    sources={(row['source_index'],row['bank'],row['preset']):row for row in instruments}
    result={(row['bank'],row['preset']):{**row,'bank_lsb':bank_lsb,'source_mapping_qualified':False}
        for row in resolve_effective_instruments(group,instruments)}
    for item in group.mappings:
        if item.bank_lsb!=bank_lsb:continue
        if system_mode==1 or system_mode==3 and item.bank>=127:continue
        source=sources.get((item.source_index,item.source_bank,item.source_program))
        if source is not None:
            result[item.bank,item.program]={**source,'source_bank':item.source_bank,'source_preset':item.source_program,
                'bank':item.bank,'bank_lsb':item.bank_lsb,'preset':item.program,'font_count':1,
                'source_candidates':(source,),'source_preset_fallback':False,'source_mapping_qualified':system_mode is not None}
    return sorted(result.values(),key=lambda row:(row['bank'],row['preset']))


def resolve_effective_instruments(group: SoundFontGroup, instruments) -> list[dict]:
    """One displayed destination per bank/program, matching font-stack priority.

    BASS_MIDI_StreamSetFonts places enabled explicit mappings before the
    ordered all-preset fallback fonts. A disabled explicit mapping therefore
    uses the first ordinary candidate; it does not mute the instrument.
    `font_count` corresponds to the source grid's Fn. helper (0047031C),
    which counts exact source presets, not simultaneously sounding voices.
    """
    candidates: dict[tuple[int, int], list[dict]] = {}
    first_in_font: dict[int, dict] = {}
    for record in instruments:
        source = int(record["source_index"])
        key = (int(record["bank"]), int(record["preset"]))
        candidates.setdefault(key, []).append(record)
        first_in_font.setdefault(source, record)
    overrides = group.preset_map()
    # BASS_MIDI_FONT can route a destination absent from every source bank:
    # its documented single-preset fallback takes that font's first preset.
    for key, override in overrides.items():
        if key not in candidates and override.index is not None and not override.disabled:
            source = first_in_font.get(override.index)
            if source is not None:
                candidates[key] = []
    resolved = []
    for key, rows in sorted(candidates.items()):
        rows = sorted(rows, key=lambda row: int(row["source_index"]))
        override = overrides.get(key)
        selected = rows[0] if rows else first_in_font[override.index]
        if override is not None and override.index is not None and not override.disabled:
            selected = next(
                (row for row in rows if row["source_index"] == override.index),
                first_in_font.get(override.index, selected),
            )
        resolved.append({
            **selected,
            "source_bank": selected["bank"], "source_preset": selected["preset"],
            "bank": key[0], "preset": key[1],
            "font_count": len({row["source_index"] for row in rows}),
            "source_candidates": tuple(rows),
            "source_preset_fallback": (selected["bank"], selected["preset"]) != key,
        })
    return resolved


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp949", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_int(value: str, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return default


def _parse_preset_header(header: str) -> tuple[int, int] | None:
    # BMSynth writes e.g. [PRESET 000:010].
    prefix = "preset "
    if not header.lower().startswith(prefix):
        return None
    try:
        bank_text, preset_text = header[len(prefix):].split(":", 1)
        bank, preset = int(bank_text.strip()), int(preset_text.strip())
    except ValueError:
        return None
    if not (0 <= bank <= 65535 and 0 <= preset <= 65535):
        return None
    return bank, preset


def performance_profile_key(key):
    if key.startswith("scale_") and key[6:].isdigit() and 0 <= int(key[6:]) < 12:
        return "scale", int(key[6:])
    parts = key.split("_", 2)
    if len(parts) == 3 and parts[0] == "drum" and parts[1].isdigit() and 0 <= int(parts[1]) < 128 and parts[2] in DRUM_KEY_DOMAINS:
        return parts[2], int(parts[1])
    raise ValueError("performance profile key")


def validate_performance_profile_value(key, value):
    kind, _index = performance_profile_key(key)
    low, high, _default = SCALE_TUNING_DOMAIN if kind == "scale" else DRUM_KEY_DOMAINS[kind]
    if type(value) is not int or not low <= value <= high:
        raise ValueError("performance profile value")
    return value


def parse_performance_profile(values):
    result = {}
    for key, raw in values.items():
        try:
            result[key] = validate_performance_profile_value(key, int(raw))
        except (ValueError, TypeError):
            continue
    return result


def load_sfg(path: str | Path) -> SoundFontGroup:
    """Read current SFG sections or the source's earlier volume=filename list."""
    source = Path(path)
    group = SoundFontGroup(source_path=str(source))
    raw_lines = _read_text(source).splitlines()
    first = next((line.strip() for line in raw_lines if line.strip() and not line.lstrip().startswith((";", "#"))), "")
    if first and not first.startswith("["):
        # Source 0046F6FE..0046F80F reads each non-section line's left side
        # as volume (default 100) and right side as the SoundFont filename.
        for raw_line in raw_lines:
            line = raw_line.strip()
            if not line or line.startswith((";", "#")) or "=" not in line:
                continue
            volume_text, filename = line.split("=", 1)
            if filename.strip():
                parsed_volume = _parse_int(volume_text, 100)
                group.fonts.append(SoundFontEntry(
                    filename.strip(), 100 if parsed_volume is None else parsed_volume,
                ))
        if not group.fonts:
            raise ValueError(tr("error.invalid_group_file"))
        return group
    if first.lower() != "[sfg]":
        raise ValueError(tr("error.invalid_group_file"))
    section = ""
    values: dict[str, str] = {}

    def commit() -> None:
        nonlocal values
        if section.lower() == 'abmsynth mapping':
            try:
                if values.get('version')!='1':raise ValueError('mapping version')
                item=SoundFontMapping(**{key:int(values[key]) for key in
                    ('bank','bank_lsb','program','source_index','source_bank','source_program')})
                # Font count is checked after all sections are read.
                if not item.valid(item.source_index+1) or item.address in group.mapping_map():
                    raise ValueError('mapping address')
                group.mappings.append(item)
            except (KeyError,TypeError,ValueError) as exc:
                raise ValueError(tr('error.invalid_soundfont_mapping')) from exc
        elif section.lower() == "abmsynth performance":
            if values.get('version')!='1':raise ValueError(tr('error.invalid_group_file'))
            group.performance = parse_performance_profile(values)
        elif section.lower() == "soundfont":
            filename = values.get("filename", "").strip()
            if filename:
                volume = _parse_int(values.get("volume", "100"), 100)
                custom=None
                if 'abm.instrument' in values:
                    from instrument_creator import validate_record
                    encoded=values['abm.instrument']
                    if len(encoded)>512*1024:raise ValueError(tr('creator.too_large'))
                    from configuration_data import read_settings_json
                    custom=read_settings_json(encoded);validate_record(custom)
                group.fonts.append(SoundFontEntry(filename, 100 if volume is None else volume,custom))
        else:
            parsed = _parse_preset_header(section)
            if parsed:
                bank, preset = parsed
                group.presets.append(PresetOverride(
                    bank=bank,
                    preset=preset,
                    index=_parse_int(values.get("index")),
                    volume=_parse_int(values.get("volume")),
                    attack=_parse_int(values.get("attack")),
                    decay=_parse_int(values.get("decay")),
                    release=_parse_int(values.get("release")),
                    cutoff=_parse_int(values.get("cutoff")),
                    resonance=_parse_int(values.get("resonance")),
                    memo=values.get("memo") or None,
                    disabled=values.get("disabled", "").strip().lower() in ("1", "true", "yes", "on"),
                    extended={name: amount for name, (low, high, _default) in PRESET_EXPRESSION_RANGES.items()
                              if (amount := _parse_int(values.get(f"abm.{name}"))) is not None
                              and low <= amount <= high},
                    extended_disabled={section for section in PRESET_EXPRESSION_GROUPS
                                       if values.get(f"abm.section.{section}") == "0"},
                    performance=parse_performance_profile({key.removeprefix("abm.performance."): value
                        for key, value in values.items() if key.startswith("abm.performance.")}),
                ))
        values = {}

    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            commit()
            section = line[1:-1].strip()
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip().lower()] = value.strip()
    commit()
    if any(not item.valid(len(group.fonts)) for item in group.mappings):
        raise ValueError(tr('error.invalid_soundfont_mapping'))
    return group


def save_sfg(group: SoundFontGroup, path: str | Path) -> SoundFontGroup:
    """Atomically write BMSynth 1.6.8 data and return the saved path model.

    A Save As must not reinterpret relative font names in a new directory.
    Resolve those names against the old group *before* changing source_path.
    """
    destination = Path(path)
    same_source = bool(group.source_path) and os.path.normcase(os.path.abspath(group.source_path)) == os.path.normcase(os.path.abspath(destination))
    saved = SoundFontGroup(
        fonts=[SoundFontEntry(
            entry.filename if entry.custom is not None or same_source or Path(entry.filename).is_absolute()
            else str(group.font_path(entry).resolve()), int(entry.volume),entry.custom,entry.runtime_path,
        ) for entry in group.fonts],
        presets=[PresetOverride(**vars(entry)) for entry in group.presets],
        source_path=str(destination),
        performance=group.performance,
        mappings=group.mappings,
    )
    lines = ["[SFG]", f"version={SFG_VERSION}", ""]
    if any(not item.valid(len(saved.fonts)) for item in saved.mappings) or len(saved.mapping_map())!=len(saved.mappings):
        raise ValueError(tr('error.invalid_soundfont_mapping'))
    for item in sorted(saved.mappings,key=lambda item:item.address):
        lines.extend(('[ABMSynth Mapping]','version=1',
            *(f'{key}={value}' for key,value in vars(item).items()),''))
    if saved.performance is not None:
        lines.extend(("[ABMSynth Performance]", "version=1",
                      *(f"{key}={value}" for key, value in sorted(saved.performance.items())), ""))
    for font in saved.fonts:
        lines.extend(("[SoundFont]", f"filename={font.filename}", f"volume={int(font.volume)}", ""))
        if font.custom is not None:
            from instrument_creator import validate_record,resolve_sample_path
            validate_record(font.custom)
            # Preserve the actual referenced source across Save As. Missing
            # assets remain explicit references, never silently disappear.
            for zone in font.custom['zones']:
                zone['source_path']=str(resolve_sample_path(zone['source_path'],group.source_path))
            lines.insert(len(lines)-1,'ABM.instrument='+json.dumps(font.custom,ensure_ascii=True,separators=(',',':')))
    # Source 00475DEC walks bank 0..128/program 0..127 and emits only
    # non-default fields. Disabled=1 replaces Index, but does not suppress
    # the remaining per-preset controls. Keep the source's field spelling.
    for (bank, program), preset in sorted(saved.preset_map().items()):
        fields = []
        if preset.disabled:
            fields.append("Disabled=1")
        elif preset.index is not None:
            fields.append(f"Index={int(preset.index)}")
        if preset.volume is not None and preset.volume != 100:
            fields.append(f"Volume={int(preset.volume)}")
        for name, value in (
            ("Attack", preset.attack), ("Decay", preset.decay),
            ("Release", preset.release), ("CutOff", preset.cutoff),
            ("Resonance", preset.resonance),
        ):
            if value is not None and value != 64:
                fields.append(f"{name}={int(value)}")
        if preset.memo:
            fields.append(f"Memo={preset.memo}")
        for name, amount in sorted(preset.extended.items()):
            fields.append(f"ABM.{name}={int(amount)}")
        for section in sorted(preset.extended_disabled):
            fields.append(f"ABM.section.{section}=0")
        for key, value in sorted(preset.performance.items()):
            fields.append(f"ABM.performance.{key}={value}")
        if fields:
            lines.extend((f"[PRESET {bank:03d}:{program:03d}]", *fields, ""))
    _write_export_bytes(destination, ("\r\n".join(lines) + "\r\n").encode("cp949"))
    return saved


def _write_export_bytes(destination: Path, encoded: bytes) -> None:
    """Leave an existing file intact if encoding or writing fails."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


INSTRUMENT_TREE_BRANCHES = (
    "Piano", "Chromatic Percussion", "Organ", "Guitar", "Bass", "Strings",
    "Ensemble", "Brass", "Reed", "Pipe", "Synth Lead", "Synth Pad",
    "Synth Effects", "Ethnic", "Percussive", "Sound Effects",
    *(f"Bank #{bank}" for bank in range(120, 128)), "Drum",
)


def _export_rows(group: SoundFontGroup, instruments):
    """Resolve the same bank/program once according to active font priority."""
    candidates = {}
    for record in instruments:
        bank, preset = int(record["bank"]), int(record["preset"])
        if 0 <= bank <= 128 and 0 <= preset < 128:
            candidates.setdefault((bank, preset), []).append(record)
    overrides = group.preset_map()
    for key in sorted(candidates):
        override = overrides.get(key)
        # An instrument definition is a catalog, not the current playback
        # policy. A disabled mapping still names a SoundFont preset and must
        # remain exportable from the user's chosen full-catalog view.
        rows = candidates[key]
        selected = next(
            (row for row in rows if override is not None and override.index is not None
             and row["source_index"] == override.index),
            rows[0],
        )
        yield key[0], key[1], str(selected["name"]).strip()


def _gm_names() -> dict[tuple[int, int], str]:
    names = {}
    source = Path(__file__).with_name("assets") / "gm_instruments.txt"
    for line in source.read_text(encoding="ascii").splitlines():
        if "=" in line and ":" in line:
            address, name = line.split("=", 1)
            bank, preset = address.split(":", 1)
            names[(int(bank), int(preset))] = name
    return names


def _ins_common_definitions() -> list[str]:
    """Generate MIDI metadata supplied by the source's INS template."""
    controllers = {
        0: "Bank Select", 1: "Modulation", 5: "Portamento Time",
        6: "Data Entry MSB", 7: "Master Volume", 10: "Panpot",
        11: "Expression", 38: "Data Entry LSB", 64: "Sustain",
        65: "Portamento", 67: "Soft Pedal", 71: "Resonance",
        72: "Release Time", 73: "Attack Time", 74: "Cutoff",
        84: "Portamento Control", 91: "Reverb", 93: "Chorus",
        94: "Delay", 98: "NRPN LSB", 99: "NRPN MSB",
        100: "RPN LSB", 101: "RPN MSB", 120: "All Sound Off",
        121: "Reset All Controllers", 123: "All Notes Off",
        126: "Mono", 127: "Poly",
    }
    lines = [".Controller Names", "[ABMSynth Controllers]"]
    lines.extend(f"{cc}={cc}-{name}" for cc, name in controllers.items())
    lines.extend(("", ".RPN Names", "[ABMSynth RPN]", "0=Pitch Bend Sensitivity",
                  "1=Fine Tuning", "2=Coarse Tuning", "", ".NRPN Names",
                  "[ABMSynth NRPN]", "160=Cutoff Frequency", "161=Resonance",
                  "227=Attack Time", "230=Release Time"))
    drum_controls = (
        (2560, "Cutoff"), (2688, "Resonance"),
        (3072, "Coarse Tune"), (3200, "Fine Tune"),
        (3328, "Level"), (3584, "Pan"),
        (3712, "Reverb"), (3840, "Chorus"),
    )
    for base, name in drum_controls:
        lines.extend(f"{base + note}=Drum {name} #{note}" for note in range(128))
    lines.append("")
    return lines


_GS_DRUM_KITS = (
    "Roland GS Standard Set", "Roland GS Room Set",
    "Roland GS Power Set", "Roland GS Electronic Set",
    "Roland GS TR-808 Set", "Roland GS Jazz Set",
    "Roland GS Brush Set", "Roland GS Orchestra Set",
    "Roland GS SFX Set",
)


def _drum_key_name(program: int) -> str:
    """BMSynth 00474F0E..00474FEF's drum-program name ranges."""
    if program == 24:
        return _GS_DRUM_KITS[3]
    if 0 <= program < 24:
        return _GS_DRUM_KITS[program // 8]
    if 25 <= program < 32:
        return _GS_DRUM_KITS[4]
    if 32 <= program < 64:
        return _GS_DRUM_KITS[5 + (program - 32) // 8]
    return "General MIDI Drums"


def render_instrument_export(group: SoundFontGroup, instruments, format_name: str) -> str:
    """Export the original command's functional NWC/INS instrument catalog.

    BMSynth 00473D10 groups NWC names into 16 GM families, banks 120..127,
    and drums. 00474BFC emits INS patch-name sections and bank definitions.
    We emit a self-contained definition rather than bundling its proprietary
    BMSynth.ins.template controller/NRPN prose.
    """
    if format_name not in {"nwc", "nwc_gm", "ins"}:
        raise ValueError(format_name)
    rows = list(_export_rows(group, instruments))
    if format_name.startswith("nwc"):
        gm_names = _gm_names() if format_name == "nwc_gm" else {}
        branches = [[] for _ in INSTRUMENT_TREE_BRANCHES]
        for bank, preset, name in rows:
            name = gm_names.get((bank, preset), name)
            if not name:
                continue
            name = name.replace("\r", " ").replace("\n", " ").replace("=", "-").replace("\\", "/")
            if not name[0].isascii() or not name[0].isalnum():
                name = f"{preset}-{name}"
            if bank and not 120 <= bank <= 128:
                name = f"{bank}-{name}"
            branch = 24 if bank == 128 else 16 + bank - 120 if 120 <= bank < 128 else preset // 8
            branches[branch].append(f"{name}={preset},Bank({bank},0)")
        lines = []
        for title, entries in zip(INSTRUMENT_TREE_BRANCHES, branches):
            if entries:
                lines.extend((f"\\{title}", *entries, ""))
        return "\r\n".join(lines) + "\r\n" if lines else ""

    banks = {}
    for bank, preset, name in rows:
        if name:
            banks.setdefault(bank, []).append((preset, name.replace("\r", " ").replace("\n", " ").replace("=", "-")))
    group_name = (
        Path(group.source_path).name if group.source_path else
        group.fonts[0].label if group.fonts else "ABMSynth"
    )
    lines = [f"; {group_name}", "", *_ins_common_definitions(), ".Patch Names"]
    for bank, entries in banks.items():
        section = f"Drums-{group_name}" if bank == 128 else f"{bank:03d}-{group_name}"
        lines.extend((f"[{section}]", *(f"{preset}={name}" for preset, name in entries), ""))
    lines.extend((".Instrument Definitions", f"[{group_name}]", "Control=ABMSynth Controllers",
                  "RPN=ABMSynth RPN", "NRPN=ABMSynth NRPN"))
    for bank in banks:
        if bank == 128:
            continue
        lines.append(f"Patch[{bank * 128}]={bank:03d}-{group_name}")
    if 128 in banks:
        lines.extend(("", f"[{group_name} Drums]", "Control=ABMSynth Controllers",
                      "RPN=ABMSynth RPN", "NRPN=ABMSynth NRPN",
                      f"Patch[0]=Drums-{group_name}"))
        lines.extend(f"Key[0,{program}]={_drum_key_name(program)}" for program, _ in banks[128])
        lines.append("Key[0,*]=General MIDI Drums")
        lines.append("Drum[*,*]=1")
    return "\r\n".join(lines) + "\r\n"


def save_instrument_export(group: SoundFontGroup, instruments, format_name: str, path: str | Path) -> None:
    rendered = render_instrument_export(group, instruments, format_name)
    _write_export_bytes(Path(path), rendered.encode("cp949"))


def validate_group(group: SoundFontGroup, *, allow_empty=False,resolve_custom_samples=True) -> list[str]:
    errors: list[str] = []
    if (any(not isinstance(item,SoundFontMapping) or not item.valid(len(group.fonts)) for item in group.mappings)
            or len(group.mapping_map())!=len(group.mappings)):
        errors.append(tr('error.invalid_soundfont_mapping'))
    profiles = [(None, group.performance)] + [((item.bank, item.preset), item.performance) for item in group.presets]
    for address, profile in profiles:
        if profile is None:
            continue
        for key, value in profile.items():
            try:
                validate_performance_profile_value(key, value)
            except (ValueError, TypeError):
                scope = f"{address[0]:03d}:{address[1]:03d}" if address else tr("group.performance_group_scope")
                errors.append(tr("error.performance_profile_range", scope=scope, field=key))
    if not group.fonts and not allow_empty:
        errors.append(tr("error.no_soundfonts"))
    for number, font in enumerate(group.fonts, 1):
        if font.custom is not None:
            from instrument_creator import validate_record,resolve_sample_path
            try:
                validate_record(font.custom)
                if resolve_custom_samples:
                    for zone in font.custom['zones']:resolve_sample_path(zone['source_path'],group.source_path)
            except (ValueError,OSError) as exc:errors.append(str(exc))
            if not 0<=int(font.volume)<=400:errors.append(tr('error.font_volume_range',number=number))
            continue
        path = group.font_path(font)
        if path.suffix.lower() not in SUPPORTED_FONT_SUFFIXES:
            errors.append(tr("error.unsupported_soundfont", number=number, filename=path.name))
        elif not path.is_file():
            errors.append(tr("error.soundfont_missing", number=number, filename=font.filename))
        if not 0 <= int(font.volume) <= 400:
            errors.append(tr("error.font_volume_range", number=number))
    for override in group.presets:
        if not override.extended_disabled <= PRESET_EXPRESSION_GROUPS.keys():
            errors.append(tr("error.preset_extended_range", field="section",
                             bank=override.bank, preset=override.preset))
        for name, value in override.extended.items():
            bounds = PRESET_EXPRESSION_RANGES.get(name)
            if bounds is None or type(value) is not int or not bounds[0] <= value <= bounds[1]:
                errors.append(tr("error.preset_extended_range", field=name,
                                 bank=override.bank, preset=override.preset))
        if not 0 <= int(override.bank) <= 128 or not 0 <= int(override.preset) <= 127:
            errors.append(tr("error.preset_address_range", bank=override.bank, preset=override.preset))
            continue
        if override.index is not None and not 0 <= override.index < len(group.fonts):
            errors.append(tr("error.preset_index_range", bank=override.bank, preset=override.preset))
        if override.volume is not None and not 0 <= int(override.volume) <= INSTRUMENT_VOLUME_MAX:
            errors.append(tr("error.preset_volume_range", bank=override.bank, preset=override.preset))
        if override.attack is not None and not 0 <= int(override.attack) <= 127:
            errors.append(tr("error.preset_attack_range", bank=override.bank, preset=override.preset))
        for field_name in ("decay", "release", "cutoff", "resonance"):
            value = getattr(override, field_name)
            if value is not None and not 0 <= int(value) <= 127:
                errors.append(
                    tr("error.preset_parameter_range", bank=override.bank, preset=override.preset, field=field_name.title())
                )
    return errors


def display_name(group: SoundFontGroup) -> str:
    if group.source_path:
        return Path(group.source_path).name
    if len(group.fonts) == 1:
        return group.fonts[0].label
    return tr("group.display_name", count=len(group.fonts))


# Bounded automatic SoundFont selection scan.

@dataclass(frozen=True)
class ScanBudget:
    max_files: int = 128
    max_directories: int = 256
    max_entries: int = 10_000
    max_seconds: float = 0.5


DEFAULT_SCAN_BUDGET = ScanBudget()


@dataclass(frozen=True)
class ScanResult:
    paths: tuple[Path, ...]
    directories: int
    entries: int
    stop_reason: str  # complete, files, directories, entries, or time
    startup_candidates: tuple[str, ...] | None = None


def scan_soundfonts(
    root: Path,
    budget: ScanBudget = DEFAULT_SCAN_BUDGET,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> ScanResult:
    """Search below ``root`` until the first work budget is exhausted.

    A time limit is checked between filesystem operations; an individual
    blocked OS call is not interruptible here.  The caller must not wait for
    this function on the GUI thread.
    """
    root = Path(root)
    if not root.is_dir():
        return ScanResult((), 0, 0, "complete")
    deadline = clock() + budget.max_seconds
    stack = [root]
    found: list[Path] = []
    seen: set[str] = set()
    directories = entries = 0
    stop_reason = "complete"

    while stack:
        if clock() >= deadline:
            stop_reason = "time"
            break
        if directories >= budget.max_directories:
            stop_reason = "directories"
            break
        current = stack.pop()
        directories += 1
        children = []
        try:
            with os.scandir(current) as iterator:
                for child in iterator:
                    if clock() >= deadline:
                        stop_reason = "time"
                        break
                    if entries >= budget.max_entries:
                        stop_reason = "entries"
                        break
                    entries += 1
                    children.append(child)
        except OSError:
            # Inaccessible directories do not invalidate already found files.
            continue

        subdirectories = []
        for child in sorted(children, key=lambda item: item.name.casefold()):
            if clock() >= deadline:
                stop_reason = "time"
                break
            try:
                if child.is_dir(follow_symlinks=False):
                    subdirectories.append(Path(child.path))
                elif child.name.casefold().endswith(".sf2") and child.is_file(follow_symlinks=False):
                    path = Path(child.path)
                    key = os.path.normcase(os.path.abspath(path))
                    if key not in seen:
                        found.append(path)
                        seen.add(key)
                        if len(found) >= budget.max_files:
                            stop_reason = "files"
                            break
            except OSError:
                continue
        if stop_reason != "complete":
            break
        stack.extend(reversed(subdirectories))

    return ScanResult(
        tuple(sorted(found, key=lambda path: str(path).casefold())),
        directories,
        entries,
        stop_reason,
    )

