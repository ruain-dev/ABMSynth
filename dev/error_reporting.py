"""Local support evidence: bounded background logs and user-reviewed archives.

No Qt/BASS calls, network client, MIDI payload or automatic user-file harvest.
The app launch owns hooks/files. GUI callers supply immutable cached snapshots;
report collection/archive run off-thread and never lock the live engine.
"""
from __future__ import annotations

import faulthandler
import hashlib
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import queue
import re
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime, UTC


_reporter = None
_startup_error = ''
_OWNED_SESSION = re.compile(r'session-\d{8}-\d{6}-[0-9a-f]{8}\.jsonl\Z')
_PATH = re.compile(r'(?i)(?:[a-z]:[\\/]|\\\\)[^\r\n\t"<>|]*')


def redact_text(value):
    """Remove absolute paths, including foreign users/UNC, before sharing."""
    text = str(value)
    for root in (str(Path.home()), os.environ.get('USERPROFILE','')):
        if root:
            text = re.sub(re.escape(root), '<USER>', text, flags=re.I)
            text = re.sub(re.escape(root.replace('\\','/')), '<USER>', text, flags=re.I)
    return _PATH.sub('<PATH>', text)


def sanitized(value):
    if isinstance(value, dict):
        return {str(key): sanitized(item) for key,item in value.items()}
    if isinstance(value,(tuple,list)):
        return [sanitized(item) for item in value]
    if isinstance(value,str): return redact_text(value)
    if type(value) is float and not math.isfinite(value):return None
    if value is None or type(value) in (int,float,bool): return value
    return redact_text(value)


class SessionReporter:
    """Producers only enqueue; one worker owns rotating writes and flushing."""
    def __init__(self, folder):
        self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=True)
        self.session=datetime.now(UTC).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
        self.path=self.folder/f'session-{self.session}.jsonl'
        self.fault_path=self.folder/f'fault-{self.session}.log'
        self.queue=queue.Queue(maxsize=512);self.dropped=0;self.write_error=''
        self.closing=False;self.previous_hooks=None;self.qt_previous=None;self.qt_installed=False
        self.history=[];self.lock=threading.Lock()
        self.worker=threading.Thread(target=self._run,name='ABMSynth-support-log',daemon=True)
        self.worker.start()
        from product import PRODUCT_NAME, PRODUCT_VERSION
        self.record('session_start',product=PRODUCT_NAME,version=PRODUCT_VERSION,python=sys.version,platform=platform.platform())

    def record(self, kind, **fields):
        if self.closing:return
        row=dict(time=datetime.now(UTC).isoformat(),monotonic=time.monotonic(),kind=kind,
                 **{key:str(value)[:16000] for key,value in fields.items()})
        # Local logs are redacted too. This producer never opens a file.
        row=sanitized(row)
        try:self.queue.put_nowait(row)
        except queue.Full:self.dropped+=1

    def _run(self):
        handler=None
        try:
            handler=RotatingFileHandler(self.path,maxBytes=1024*1024,backupCount=2,encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(message)s'))
            def write_failed(_record):
                self.write_error=redact_text(sys.exception())
            handler.handleError=write_failed
            # Keep only the latest six owned sessions; never scan user's logs.
            sessions=sorted((p for p in self.folder.glob('session-*.jsonl') if _OWNED_SESSION.fullmatch(p.name)),
                            key=lambda p:p.stat().st_mtime,reverse=True)
            for old in sessions[6:]:
                for candidate in (old,Path(str(old)+'.1'),Path(str(old)+'.2'),
                                  self.folder/old.name.replace('session-','fault-').replace('.jsonl','.log')):
                    if candidate.is_file() and not candidate.is_symlink():candidate.unlink()
            while not self.closing or not self.queue.empty():
                try:row=self.queue.get(timeout=.1)
                except queue.Empty:continue
                try:
                    line=json.dumps(row,ensure_ascii=False,allow_nan=False)
                    handler.emit(logging.LogRecord('support',logging.INFO,'',0,line,(),None))
                    with self.lock:
                        self.history.append(row)
                        if len(self.history)>512:del self.history[:-512]
                finally:self.queue.task_done()
        except Exception as error:self.write_error=redact_text(error)
        finally:
            if handler is not None:handler.close()

    def install_hooks(self):
        self.previous_hooks=(sys.excepthook,threading.excepthook)
        previous,thread_previous=self.previous_hooks
        def exception(kind,error,tb):
            self.record('unhandled_exception',traceback=''.join(traceback.format_exception(kind,error,tb)))
            previous(kind,error,tb)
        def thread_exception(args):
            self.record('thread_exception',thread=args.thread.name if args.thread else '',
                traceback=''.join(traceback.format_exception(args.exc_type,args.exc_value,args.exc_traceback)))
            thread_previous(args)
        self._exception_hook=exception;self._thread_hook=thread_exception
        sys.excepthook=exception;threading.excepthook=thread_exception
        self._fault_file=self.fault_path.open('a',encoding='utf-8')
        self._owns_fault=not faulthandler.is_enabled()
        if self._owns_fault:faulthandler.enable(self._fault_file,all_threads=True,c_stack=False)

    def install_qt_hook(self):
        if self.qt_installed:return
        from PySide6.QtCore import qInstallMessageHandler, QtMsgType
        def message(kind,context,text):
            if kind!=QtMsgType.QtDebugMsg:self.record('qt_message',level=str(kind),message=text,
                category=context.category,file=context.file,line=context.line,function=context.function)
            if self.qt_previous is not None:self.qt_previous(kind,context,text)
            elif kind!=QtMsgType.QtDebugMsg:print(text,file=sys.stderr)
        self._qt_hook=message;self.qt_previous=qInstallMessageHandler(message);self.qt_installed=True

    def snapshot(self):
        with self.lock:rows=tuple(self.history)
        return dict(session=self.session,recent=rows,queued=self.queue.qsize(),dropped=self.dropped,
                    write_error=self.write_error,fault_capture_owned=getattr(self,'_owns_fault',False))

    def close(self):
        if self.closing:return
        self.record('session_end');self.closing=True;self.worker.join(3)
        if self.previous_hooks:
            if sys.excepthook is self._exception_hook:sys.excepthook=self.previous_hooks[0]
            if threading.excepthook is self._thread_hook:threading.excepthook=self.previous_hooks[1]
        if self.qt_installed:
            from PySide6.QtCore import qInstallMessageHandler
            qInstallMessageHandler(self.qt_previous)
        if getattr(self,'_owns_fault',False):faulthandler.disable()
        if hasattr(self,'_fault_file'):self._fault_file.close()


def current_reporter(): return _reporter


def record(kind, **fields):
    if _reporter is not None:_reporter.record(kind,**fields)


def start_reporting():
    global _reporter, _startup_error
    if _reporter is not None:return _reporter
    override=os.environ.get('ABMSYNTH_TEST_INI_PATH')
    folder=(Path(override).parent/'support-logs' if override else
            Path(os.environ.get('LOCALAPPDATA',tempfile.gettempdir()))/'ABMSynth'/'Reports')
    folders=(folder,) if override else (folder,Path(tempfile.gettempdir())/'ABMSynth'/'Reports')
    for destination in folders:
        reporter=None
        try:
            reporter=SessionReporter(destination);reporter.install_hooks()
            _reporter=reporter;_startup_error='';return reporter
        except (OSError,RuntimeError) as error:
            if reporter is not None:reporter.close()
            _startup_error=redact_text(error)
    # Logging is ancillary. A read-only profile must not break audio startup.
    return None


def stop_reporting():
    global _reporter
    if _reporter is not None:_reporter.close();_reporter=None


def collect_report(snapshot, description, *, logs=True, system=True, settings=True, reporter=None):
    """Worker-only, whitelist-owned files; never includes an INI or MIDI file."""
    result=dict(schema=1,created_utc=datetime.now(UTC).isoformat(),description=redact_text(description[:12000]))
    if settings:result['settings']=sanitized(snapshot.get('settings',{}))
    if system:
        import importlib.metadata
        from product import PRODUCT_NAME, PRODUCT_VERSION
        versions={}
        for name in ('PySide6','numpy'):
            try:versions[name]=importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:versions[name]=None
        root=Path(__file__).parent
        hashes={name:hashlib.sha256((root/name).read_bytes()).hexdigest()
                for name in ('abmsynth_dsp.dll','bass.dll','bassmidi.dll','bassasio.dll','basswasapi.dll') if (root/name).is_file()}
        result['system']=dict(product=PRODUCT_NAME,version=PRODUCT_VERSION,os=platform.platform(),python=sys.version,architecture=platform.machine(),
            logical_cpus=os.cpu_count(),packages=versions,binary_sha256=hashes)
        result['playback']=sanitized(snapshot.get('playback',{}))
    if logs:
        reporter=reporter or current_reporter()
        result['logs']=sanitized(reporter.snapshot() if reporter else dict(recent=(),logging_active=False,write_error=_startup_error))
        # Include bounded tails of owned prior sessions/faults, so a restart
        # does not hide a failure. No trace/audio/source files are harvested.
        previous=[]
        if reporter:
            owned=tuple(p for p in reporter.folder.glob('session-*.jsonl') if _OWNED_SESSION.fullmatch(p.name))
            faults=tuple(p for p in reporter.folder.glob('fault-*.log') if
                         _OWNED_SESSION.fullmatch(p.name.replace('fault-','session-',1).replace('.log','.jsonl')))
            candidates=sorted((*owned,*faults),
                              key=lambda p:p.stat().st_mtime,reverse=True)[:8]
            for path in candidates:
                if path.is_symlink():continue
                with path.open('rb') as stream:
                    stream.seek(0,2);stream.seek(max(0,stream.tell()-65536))
                    text=redact_text(stream.read(65536).decode('utf-8',errors='replace'))
                if text:previous.append(dict(name=path.name,text=text))
        result['logs']['previous_sessions']=previous
    return json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)


def save_report(path, preview):
    """Atomic ZIP, exact reviewed contents, no user files or hidden attachments."""
    json.loads(preview)  # Reject truncated/invalid report before destination use.
    target=Path(path);temporary=None
    try:
        descriptor,temporary=tempfile.mkstemp(prefix='.abmsynth-report-',suffix='.zip',dir=target.parent)
        os.close(descriptor)
        with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('report.json',preview.encode('utf-8'))
        with open(temporary,'r+b') as stream:os.fsync(stream.fileno())
        os.replace(temporary,target);temporary=None
    finally:
        if temporary is not None:Path(temporary).unlink(missing_ok=True)
