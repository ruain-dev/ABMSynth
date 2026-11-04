"""Offline notice catalogue shared by presentation and release verification.

No environment/package scan, imports of audio, or network access at runtime.
Original upstream notice text is data; explanatory UI copy uses locale IDs.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

NOTICE_ROOT=Path(__file__).resolve().with_name('licenses')

# Product presentation is separate from licence/provenance metadata. Keep
# original names,versions,source records and legal document bytes available.
# No per-dialog regex replacement or rewriting of upstream legal text.
_PRESENTATION={
    'abmsynth':{'license_id':'licenses.terms_abmsynth'},
    'python':{'license_id':'licenses.terms_python'},
    'qt':{'license_id':'licenses.terms_qt'},
    'ffmpeg':{'license_id':'licenses.terms_ffmpeg'},
    'librosa_analysis':{'name_id':'licenses.name_librosa_analysis','version':''},
    'librosa_tempo':{'name_id':'licenses.name_librosa_tempo'},
    'noto':{'version':'2.004-H2'},
    'bass_fx':{'license_id':'licenses.terms_bass_fx'},
    'midi_mode_logos':{'name_id':'licenses.name_midi_mode_logos','version':'',
                       'license_id':'licenses.terms_midi_mode_logos'},
    'xz':{'name_id':'licenses.name_liblzma','license_id':'licenses.terms_liblzma'},
    'ucrt':{'license_id':'licenses.terms_ucrt'},
    'vc14_runtime':{'version':'14.x','license_id':'licenses.terms_vc_runtime'},
    'mesa_llvmpipe':{'version':''},
    'mesa_llvm':{'name_id':'licenses.name_llvm','version':'','license_id':'licenses.terms_llvm'},
    'pyinstaller':{'name_id':'licenses.name_pyinstaller','license_id':'licenses.terms_pyinstaller'},
    'soundtouch':{'name_id':'licenses.name_soundtouch'},
}
for _component in ('bass','bassmidi','bassasio','basswasapi','bassloud',
                   'bassenc','bassenc_flac','bassenc_ogg','bassenc_opus','bassenc_mp3'):
    _PRESENTATION[_component]={'license_id':'licenses.terms_un4seen'}
del _component


def notice_presentation(component,translate):
    """One product-facing identity for list,search,title and licence summary."""
    presentation=_PRESENTATION.get(component['id'],{})
    name=translate(presentation['name_id']) if 'name_id' in presentation else component['name']
    version=presentation.get('version',component['version'])
    terms=translate(presentation['license_id']) if 'license_id' in presentation else component['license']
    title=translate('licenses.component_title',name=name,version=version) if version else name
    return dict(name=name,title=title,version=version,terms=terms,
                description=translate(component['description_id']))


def notice_path(document,root=NOTICE_ROOT):
    root=Path(root).resolve()
    relative=Path(document['path'])
    path=(root/relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(root) or path==root:
        raise ValueError('notice path outside resource directory')
    return path


def load_notice_catalog(root=NOTICE_ROOT):
    root=Path(root)
    path=root/'catalog.json'
    if path.stat().st_size>1_000_000:raise ValueError('oversized notice catalog')
    raw=path.read_bytes()
    catalog=json.loads(raw)
    if catalog['schema']!=1 or catalog.get('release_cleared') is not False:raise ValueError('unreviewed notice schema')
    rows=catalog['components']
    if not 1<=len(rows)<=128 or len({row['id'] for row in rows})!=len(rows):raise ValueError('invalid notice components')
    for row in rows:
        url=urlparse(row['homepage'])
        if url.scheme!='https' or not url.netloc or url.username or url.password:raise ValueError('unsafe upstream URL')
        if not 1<=len(row['documents'])<=256:raise ValueError('missing or excessive notice documents')
        for document in row['documents']:notice_path(document,root)
    return catalog


def read_notice(document,root=NOTICE_ROOT):
    path=notice_path(document,root)
    if path.stat().st_size>2_000_000:raise ValueError('oversized notice')
    raw=path.read_bytes()
    if len(raw)!=document['bytes'] or hashlib.sha256(raw).hexdigest()!=document['sha256']:
        raise ValueError('notice file differs from reviewed original')
    encoding=document.get('encoding','utf-8-sig')
    if encoding not in ('utf-8-sig','cp1252'):raise ValueError('unsupported notice encoding')
    return raw.decode(encoding)
