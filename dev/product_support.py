"""Product help, release discovery and user-directed configuration transport.

Disk and network operations in this module are called by the support worker.
They do not query BASS, open MIDI endpoints or own a running audio session.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
from configparser import ConfigParser, Error as ConfigError
from datetime import datetime, UTC
from pathlib import Path
from urllib.parse import urlencode, urlparse, quote
from urllib.request import Request, urlopen

from i18n import tr
from product import PRODUCT_NAME, PRODUCT_VERSION, RELEASE_API_URL, ISSUES_URL


def atomic_write(destination, data):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.abmsynth-', dir=destination.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)


def version_key(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value)
    if match is None:
        raise ValueError(tr('support.invalid_version'))
    return tuple(int(part) for part in match.groups())


def project_link(value):
    parsed = urlparse(value)
    return (parsed.scheme == 'https' and parsed.netloc == 'github.com'
            and parsed.path.startswith('/ruain-dev/ABMSynth/')
            and not parsed.username and not parsed.password)


def check_release():
    request = Request(RELEASE_API_URL, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': f'{PRODUCT_NAME}/{PRODUCT_VERSION}',
    })
    with urlopen(request, timeout=6) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError(tr('support.release_invalid'))
    record = json.loads(raw)
    tag = record['tag_name']
    version_key(tag)
    base = 'https://github.com/ruain-dev/ABMSynth/releases/'
    if (record.get('draft') or record.get('prerelease')
            or record['html_url'] != base + 'tag/' + quote(tag, safe='')):
        raise ValueError(tr('support.release_invalid'))
    assets = {}
    artifacts = {}
    checksums = None
    for item in record.get('assets', []):
        name = item['name']
        url = item['browser_download_url']
        if url != base + 'download/' + quote(tag, safe='') + '/' + quote(name, safe=''):
            continue
        if name == 'SHA256SUMS.txt':
            checksums = url
        kind = None
        if name == f'ABMSynth-{tag.removeprefix("v")}-Setup-x64.exe':
            kind = 'installed'
        elif name == f'ABMSynth-{tag.removeprefix("v")}-Portable-x64.zip':
            kind = 'portable'
        if kind:
            if kind in assets:raise ValueError(tr('support.release_invalid'))
            assets[kind] = url
            digest = item.get('digest') or ''
            artifacts[kind] = dict(name=name, url=url, size=item.get('size'),
                sha256=digest[7:] if re.fullmatch(r'sha256:[a-f0-9]{64}', digest) else '')
    return dict(version=tag.removeprefix('v'), newer=version_key(tag) > version_key(PRODUCT_VERSION),
                url=record['html_url'], notes=str(record.get('body') or '')[:100000],
                assets=assets, artifacts=artifacts, checksums=checksums)


def version_information():
    path = Path(__file__).with_name('assets') / 'build-info.json'
    if path.is_file():
        build = json.loads(path.read_text(encoding='utf-8'))['build']
    else:
        digest = hashlib.sha256()
        for source in sorted(Path(__file__).parent.glob('*.py')):
            digest.update(source.name.encode('utf-8'))
            digest.update(source.read_bytes())
        build = digest.hexdigest()[:12]
    return '\n'.join((f'{PRODUCT_NAME} {PRODUCT_VERSION}', f'{tr("support.build")} {build}',
                      platform.platform(), f'{platform.machine()} · {tr("support.recovery_session")}'
                      if os.environ.get('ABMSYNTH_RECOVERY_PROFILE') else platform.machine()))


def issue_link():
    return ISSUES_URL + '?' + urlencode({'body': tr('support.issue_body', version=PRODUCT_VERSION)})


def validate_backup(raw):
    from configuration_data import INI_MAX_BYTES, INI_MAX_LINE
    if len(raw) > INI_MAX_BYTES * 2:
        raise ValueError(tr('support.backup_invalid'))
    record = json.loads(raw)
    if (not isinstance(record, dict) or type(record.get('schema')) is not int
            or record.get('schema') != 1 or record.get('product') != PRODUCT_NAME
            or not isinstance(record.get('ini'), str)):
        raise ValueError(tr('support.backup_invalid'))
    text = record['ini']
    if (len(text.encode('utf-8')) > INI_MAX_BYTES
            or any(len(line) > INI_MAX_LINE for line in text.splitlines())
            or not isinstance(record.get('version'), str)
            or re.fullmatch(r'\d+\.\d+\.\d+', record['version']) is None):
        raise ValueError(tr('support.backup_invalid'))
    parser = ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(text)
    except ConfigError:
        raise ValueError(tr('support.backup_invalid')) from None
    if not parser.sections():
        raise ValueError(tr('support.backup_invalid'))
    return record


def save_settings_backup(destination, writer):
    from settings import EnginePreferences, engine_ini_values, settings_path
    writer.flush(retry_failed=True)
    try:
        text = settings_path().read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        text = '[Engine]\n' + ''.join(f'{key}={value}\n' for key, value in engine_ini_values(EnginePreferences()).items())
    record = dict(schema=1, product=PRODUCT_NAME, version=PRODUCT_VERSION,
                  created=datetime.now(UTC).isoformat(), ini=text)
    data = json.dumps(record, ensure_ascii=False, indent=2).encode('utf-8')
    validate_backup(data)
    atomic_write(destination, data)
    return str(destination)


def read_backup(path):
    from configuration_data import INI_MAX_BYTES
    with Path(path).open('rb') as source:
        return validate_backup(source.read(INI_MAX_BYTES * 2 + 1))


def stage_settings_restore(record):
    data = json.dumps(record, ensure_ascii=False).encode('utf-8')
    validate_backup(data)
    target = restore_request_path()
    atomic_write(target, data)
    return str(target)


def restore_request_path():
    from settings import settings_path
    target = Path(os.environ.get('ABMSYNTH_RECOVERY_ORIGINAL_INI') or settings_path())
    return target.with_suffix('.restore.json')


def cancel_settings_restore():
    restore_request_path().unlink(missing_ok=True)


def backup_summary(record):
    parser = ConfigParser(interpolation=None)
    parser.read_string(record['ini'])
    if not parser.has_section('Engine'):
        return ''
    fields = (('sample_rate', 'preferences.sample_rate'), ('buffer_ms', 'preferences.buffer'),
              ('synthesis_mode', 'preferences.synthesis_mode'), ('priority', 'preferences.priority'))
    values = []
    for key, caption in fields:
        if key not in parser['Engine']:
            continue
        value = parser['Engine'][key][:80]
        if key == 'sample_rate':
            value += ' Hz'
        elif key == 'buffer_ms':
            value += ' ms'
        elif key == 'synthesis_mode':
            value = tr('support.on' if value == 'high_performance' else 'support.off')
        values.append(f'{tr(caption)}  {value}')
    return '\n'.join(values)


def apply_pending_restore():
    from settings import settings_path
    if os.environ.get('ABMSYNTH_RECOVERY_PROFILE'):
        return
    target = settings_path()
    pending = target.with_suffix('.restore.json')
    if not pending.exists():
        return
    try:
        record = read_backup(pending)
    except (ValueError, TypeError, KeyError):
        stamp = datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f')
        pending.rename(pending.with_name(pending.name + f'.rejected-{stamp}'))
        raise ValueError(tr('support.backup_invalid')) from None
    if target.exists():
        stamp = datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f')
        atomic_write(target.with_name(target.name + f'.before-restore-{stamp}.bak'), target.read_bytes())
    atomic_write(target, record['ini'].encode('utf-8'))
    pending.unlink()


def group_file_inventory(group, *, cancelled=lambda: False):
    from instrument_creator import resolve_sample_path
    from soundfont_files import asset_file_identity, soundfont_asset_identity
    entries = []
    for index, font in enumerate(group.fonts):
        if cancelled():
            raise RuntimeError(tr('support.cancelled'))
        if font.custom is not None:
            if not font.custom['zones']:
                entries.append(dict(index=index, kind='generated', path=font.filename, error=''))
            for number, zone in enumerate(font.custom['zones']):
                try:
                    path = resolve_sample_path(zone['source_path'], group.source_path)
                    entries.append(dict(index=index, kind='sample', path=str(path), zone=number,
                                        identity=asset_file_identity(path, cancelled), error=''))
                except OSError as error:
                    entries.append(dict(index=index, kind='sample', path=zone['source_path'], zone=zone['name'], error=str(error)))
        else:
            path = group.font_path(font)
            try:
                identity = soundfont_asset_identity(path, cancelled)
                error = ''
                if path.suffix.lower() == '.sfz':
                    if (not identity['resolution']['qualified']
                            or any(Path(edge['literal'].replace('\\', '/')).is_absolute()
                                   for edge in identity['resolution']['edges'])
                            or any(not Path(item['path']).is_relative_to(path.resolve().parent)
                                   for item in identity['dependencies'])):
                        error = tr('support.sfz_external')
                entries.append(dict(index=index, kind='font', path=str(path), identity=identity, error=error))
                entries.extend(dict(index=index, kind='dependency', path=item['path'], error='')
                               for item in identity.get('dependencies', []))
            except (OSError, RuntimeError, ValueError) as error:
                entries.append(dict(index=index, kind='font', path=str(path), error=str(error)))
    if cancelled():
        raise RuntimeError(tr('support.cancelled'))
    return entries


def collect_group_files(group, destination, inventory, *, cancelled=lambda: False):
    """Publish a new folder atomically. The source group and its files stay owned by the caller."""
    from soundfont_files import save_sfg, stage_sfz_asset
    if not inventory or any(item['error'] for item in inventory):
        raise ValueError(tr('support.assets_unresolved'))
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(tr('support.folder_exists'))
    group = copy.deepcopy(group)
    with tempfile.TemporaryDirectory(prefix='.abmsynth-collect-', dir=destination.parent) as folder:
        stage = Path(folder) / 'files'
        stage.mkdir()
        for index, font in enumerate(group.fonts):
            if cancelled():
                raise RuntimeError(tr('support.cancelled'))
            asset_dir = stage / f'{index + 1:03d}'
            asset_dir.mkdir()
            if font.custom is not None:
                for number, zone in enumerate(font.custom['zones']):
                    identity = next(row['identity'] for row in inventory
                                    if row['index'] == index and row['kind'] == 'sample' and row['zone'] == number)
                    source = Path(identity['path'])
                    target = asset_dir / f'{number + 1:03d}-{source.name}'
                    copy_verified_asset(source, target, identity, cancelled)
                    zone['source_path'] = str(target)
            else:
                source = group.font_path(font)
                identity = next(item['identity'] for item in inventory
                                if item['index'] == index and item['kind'] == 'font')
                if source.suffix.lower() == '.sfz':
                    copied = stage_sfz_asset(identity, cancelled)
                    if copied is None:
                        raise ValueError(tr('support.sfz_external'))
                    temporary, root = copied
                    try:
                        for item in (identity, *identity.get('dependencies', ())):
                            relative = Path(item['path']).relative_to(source.resolve().parent)
                            target = asset_dir / relative
                            target.parent.mkdir(parents=True, exist_ok=True)
                            copy_verified_asset(root.parent / relative, target, item, cancelled)
                    finally:
                        temporary.cleanup()
                else:
                    copy_verified_asset(source, asset_dir / source.name, identity, cancelled)
                font.filename = str(asset_dir / source.name)
        group.source_path = str(stage / 'ABMSynth.sfg')
        saved = save_sfg(group, group.source_path)
        # save_sfg preserves resolved sources. Rebase only the newly written copy.
        file = stage / 'ABMSynth.sfg'
        text = file.read_text(encoding='cp949')
        for font in saved.fonts:
            if font.custom is None:
                text = text.replace('filename=' + font.filename,
                                    'filename=' + Path(font.filename).relative_to(stage).as_posix())
            else:
                for zone in font.custom['zones']:
                    absolute = zone['source_path']
                    relative = Path(absolute).relative_to(stage).as_posix()
                    text = text.replace(json.dumps(absolute, ensure_ascii=True), json.dumps(relative, ensure_ascii=True))
        file.write_text(text, encoding='cp949')
        if cancelled():
            raise RuntimeError(tr('support.cancelled'))
        os.rename(stage, destination)
    return str(destination)


def copy_verified_asset(source, target, identity, cancelled=lambda: False):
    digest = hashlib.sha256()
    with source.open('rb') as original, target.open('wb') as output:
        while block := original.read(1024 * 1024):
            if cancelled():
                raise RuntimeError(tr('support.cancelled'))
            digest.update(block)
            output.write(block)
    if digest.hexdigest() != identity['sha256']:
        raise ValueError(tr('support.assets_changed'))
    shutil.copystat(source, target)
