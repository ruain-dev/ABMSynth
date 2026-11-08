"""Shared product help window and user-directed maintenance actions."""

from pathlib import Path
import copy
import os

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QThread, QUrl, QTimer, Signal, QByteArray
from PySide6.QtGui import QDesktopServices, QTextDocument
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QLabel, QLayout, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QScrollArea, QSizePolicy, QSplitter,
    QTextBrowser, QVBoxLayout, QWidget,
)

from i18n import tr
from native_layout import bordered_page_layout, control_row_layout, set_layout
from native_widgets import (
    METRICS, NativeCard, NativeDialog, NativeMessageBox, NativeOptionalLabel, apply_native_text_metrics,
    make_action_button, make_navigation_tabs,
    OutlineCheckBox,
)
from product import PRODUCT_NAME, PRODUCT_VERSION, PRODUCT_PUBLISHER, PRODUCT_COPYRIGHT, PROJECT_URL, RELEASES_URL
import product_support as support
import product_updates as updates


HELP_TOPICS = ('start', 'midi', 'fonts', 'creator', 'channel', 'effects', 'player', 'recording', 'problems', 'backup')


class SupportMarkdownReader(QTextBrowser):
    def loadResource(self, resource_type, url):
        # Help and release prose never reads arbitrary local or remote images.
        return QByteArray()


class ProductHelpFilter(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner

    def eventFilter(self, watched, event):
        if event.type() != QEvent.KeyPress or event.key() != Qt.Key_F1 or event.modifiers():
            return False
        window = QApplication.activeWindow()
        if isinstance(window, (QMessageBox, QFileDialog)):
            return False
        ancestor = window
        while ancestor is not None and ancestor is not self.owner:
            ancestor = ancestor.parentWidget()
        if ancestor is None:
            return False
        topic = window.property('supportTopic') if window is not None else None
        if window is self.owner:
            topic = self.owner.tabs.currentWidget().property('supportTopic') or 'effects'
        self.owner.open_help(topic or 'start')
        return True


class SupportWorker(QThread):
    progress = Signal(object, object)
    def __init__(self, operation, arguments, parent):
        super().__init__(parent)
        self.operation, self.arguments = operation, arguments
        self.keywords = {}
        self.result = None
        self.error = ''

    def run(self):
        try:
            self.result = self.operation(*self.arguments, **self.keywords)
        except Exception as error:
            self.error = str(error)


def open_support_url(parent, url):
    if not QDesktopServices.openUrl(QUrl(url)):
        NativeMessageBox.warning(parent, tr('support.title'), tr('support.open_failed', path=url))


def reveal_support_path(parent, path):
    path = Path(path)
    target = path if path.is_dir() else path.parent
    if not target.is_dir():
        NativeMessageBox.warning(parent, tr('support.title'), tr('support.folder_missing', path=target))
        return
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
        NativeMessageBox.warning(parent, tr('support.title'), tr('support.open_failed', path=target))


class NativeProductSupportDialog(NativeDialog):
    def minimumSizeHint(self):
        return QSize(0, 0)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.worker = None
        self._release = None
        self.setFont(owner.font())
        self.setWindowTitle(tr('support.title'))
        self.setMinimumSize(0, 0)
        self.resize(820, 610)
        root = set_layout(QVBoxLayout(self), METRICS['outer'], METRICS['section_gap'])
        root.setSizeConstraint(QLayout.SetNoConstraint)
        self.tabs = make_navigation_tabs()
        root.addWidget(self.tabs, 1)
        self.info = self._page('support.info', scroll=True)
        identity, body = self._card(self.info)
        for text, role in ((PRODUCT_NAME, 'DialogTitle'),
                           (tr('support.version', version=PRODUCT_VERSION), 'CardTitle'),
                           (tr('support.description'), ''),
                           (tr('support.publisher', name=PRODUCT_PUBLISHER), ''),
                           (PRODUCT_COPYRIGHT, '')):
            label = QLabel(text)
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            if role:
                label.setObjectName(role)
            body.addWidget(label)
        self.identity_text = QLabel('')
        self.identity_text.setWordWrap(True)
        self.identity_text.setMinimumWidth(0)
        self.identity_text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        body.addWidget(self.identity_text)
        links = control_row_layout()
        self.copy_button = make_action_button(tr('support.copy_version'), self.copy_version)
        self.copy_button.setEnabled(False)
        for caption, callback in ((tr('support.project'), lambda: open_support_url(self, PROJECT_URL)),
                                  (tr('support.report'), self.open_report),
                                  (tr('support.copy_version'), self.copy_version)):
            button = self.copy_button if caption == tr('support.copy_version') else make_action_button(caption, callback)
            links.addWidget(button, 1)
        body.addLayout(links)
        _, credits = self._card(self.info)
        credit_text = QLabel(tr('support.credits'))
        credit_text.setWordWrap(True)
        credit_text.setMinimumWidth(0)
        credit_text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        credits.addWidget(credit_text)
        self.info._body.addStretch(1)

        self.help_page = self._page('support.guide')
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr('support.search'))
        self.search.setAccessibleName(tr('support.search'))
        self.help_page.layout().addWidget(self.search)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setMinimumSize(0, 0)
        self.topics = QListWidget()
        self.topics.setObjectName('LicenseComponents')
        self.topics.setMinimumSize(0, 0)
        self.topics.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.topics.setAccessibleName(tr('support.guide'))
        self.guide = self._reader()
        splitter.addWidget(self.topics)
        splitter.addWidget(self.guide)
        splitter.setSizes([190, 570])
        self.help_page.layout().addWidget(splitter, 1)
        for topic in HELP_TOPICS:
            item = QListWidgetItem(tr('support.topic_' + topic))
            item.setData(Qt.UserRole, topic)
            self.topics.addItem(item)
        self.topics.currentItemChanged.connect(self._show_topic)
        self.search.textChanged.connect(self._filter_topics)
        self.topics.setCurrentRow(0)
        self.interactions = self._page('support.interactions')
        reader = self._reader()
        self._set_markdown(reader, tr('support.interaction_guide'))
        self.interactions.layout().addWidget(reader, 1)
        self.updates = self._page('support.updates')
        self.update_status = QLabel(tr('support.check_prompt', version=PRODUCT_VERSION))
        self.update_status.setWordWrap(True)
        self.update_status.setMinimumWidth(0)
        self.updates.layout().addWidget(self.update_status)
        self.update_notes = self._reader()
        self.updates.layout().addWidget(self.update_notes, 1)
        update_actions = control_row_layout()
        self.check_button = make_action_button(tr('support.check_updates'), self.check_updates)
        update_actions.addWidget(self.check_button)
        self.download_button = make_action_button(tr('support.update_action') if updates.install_context()
                                                  else tr('support.download'), self.download_release)
        self.download_button.setEnabled(False)
        update_actions.addWidget(self.download_button)
        update_actions.addWidget(make_action_button(tr('support.all_releases'),
            lambda: open_support_url(self, RELEASES_URL)))
        self.updates.layout().addLayout(update_actions)
        owner._product_updates.releaseReady.connect(self._release_ready)
        owner._product_updates.checkFailed.connect(self._check_failed)
        owner._product_updates.busyChanged.connect(self._update_busy)
        from native_dialogs import NativeLicensesDialog
        self.licenses = NativeLicensesDialog(owner)
        self.licenses.setParent(self)
        self.licenses.setWindowFlags(Qt.Widget)
        self.licenses.close_button.hide()
        self.licenses.layout().itemAt(0).widget().hide()
        self.licenses._fitted = True
        self.license_host = QWidget()
        self.license_host.setObjectName('EmbeddedLicensePage')
        host_layout = set_layout(QVBoxLayout(self.license_host), 1, 0)
        host_layout.addWidget(self.licenses)
        self.tabs.addTab(self.license_host, tr('licenses.action'))
        self.status = NativeOptionalLabel()
        self.status.setWordWrap(True)
        self.status.setMinimumWidth(0)
        root.addWidget(self.status)
        footer = control_row_layout()
        footer.addStretch(1)
        self.close_button = make_action_button(tr('common.close'), self.close)
        footer.addWidget(self.close_button)
        root.addLayout(footer)
        apply_native_text_metrics(self)
        self._start(support.version_information, (), self._identity_ready)

    def _page(self, key, *, scroll=False):
        page = QWidget()
        page.setMinimumSize(0, 0)
        layout = bordered_page_layout(QVBoxLayout(page), METRICS['section_gap'])
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        page._body = layout
        if scroll:
            viewport = QScrollArea()
            viewport.setWidgetResizable(True)
            viewport.setFrameShape(QScrollArea.NoFrame)
            viewport.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            content = QWidget()
            content.setMinimumWidth(0)
            content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            page._body = set_layout(QVBoxLayout(content), 0, METRICS['section_gap'])
            viewport.setWidget(content)
            layout.addWidget(viewport, 1)
        self.tabs.addTab(page, tr(key))
        return page

    @staticmethod
    def _card(page):
        card = NativeCard()
        card.setMinimumWidth(0)
        card.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        body = set_layout(QVBoxLayout(card), METRICS['card_padding'], METRICS['compact_content_gap'])
        page._body.addWidget(card)
        return card, body

    @staticmethod
    def _reader():
        reader = SupportMarkdownReader()
        reader.setObjectName('SupportDocument')
        reader.setMinimumSize(0, 0)
        reader.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        reader.setOpenLinks(False)
        reader.setOpenExternalLinks(False)
        return reader

    @staticmethod
    def _set_markdown(reader, text):
        reader.document().setDefaultStyleSheet(
            'body { font-size: 13px; line-height: 1.35; } '
            'h1 { font-size: 19px; font-weight: 700; margin-top: 4px; margin-bottom: 10px; } '
            'h2 { font-size: 15px; font-weight: 650; margin-top: 14px; margin-bottom: 6px; } '
            'h3 { font-size: 14px; font-weight: 600; margin-top: 10px; margin-bottom: 4px; } '
            'p { margin-top: 3px; margin-bottom: 7px; } '
            'ul { margin-top: 3px; margin-bottom: 7px; } '
            'li { margin-bottom: 3px; } '
            'code { font-family: monospace; }'
        )
        reader.document().setMarkdown(text, QTextDocument.MarkdownDialectGitHub | QTextDocument.MarkdownNoHTML)

    def _start(self, operation, arguments, completed):
        if self.worker is not None:
            return
        self.check_button.setEnabled(False)
        self.worker = SupportWorker(operation, arguments, self)
        self._completed = completed
        self.worker.finished.connect(self._finished)
        self.worker.start()

    def _finished(self):
        worker = self.worker
        self.worker = None
        self.check_button.setEnabled(True)
        if worker.error:
            self.status.setText(tr('support.operation_failed', error=worker.error))
            if worker.operation is support.check_release:
                self.update_status.setText(tr('support.check_failed'))
        else:
            self.status.clear()
            self._completed(worker.result)
        worker.deleteLater()

    def _identity_ready(self, text):
        self._identity = text
        self.identity_text.setText(text.splitlines()[1])
        self.copy_button.setEnabled(True)

    def copy_version(self):
        QApplication.clipboard().setText(self._identity)

    def show_topic(self, topic=None):
        self.tabs.setCurrentWidget(self.help_page)
        self.search.clear()
        if topic in HELP_TOPICS:
            self.topics.setCurrentRow(HELP_TOPICS.index(topic))

    def show_context(self, topic):
        if topic == 'group':
            editor = getattr(self.owner, '_group_dialog', None)
            if editor is not None:
                topic = ('fonts', 'fonts', 'creator')[editor.group_tabs.currentIndex()]
        elif topic == 'preferences':
            preferences = getattr(self.owner, '_preferences_dialog', None)
            if preferences is not None:
                current = preferences.tabs.currentWidget()
                topic = ('midi' if current is preferences.midi_input_scroll else
                         'recording' if current is preferences.recording_scroll else
                         'problems' if current in (preferences.diagnostics_scroll, preferences.error_report_scroll) else 'backup')
        elif topic == 'effects' and self.owner.tabs.currentWidget() is self.owner.channel_monitor:
            topic = 'channel'
        self.show_topic(topic)

    def _show_topic(self, item, previous=None):
        self._set_markdown(self.guide, tr('support.guide_' + item.data(Qt.UserRole)) if item else '')

    def _filter_topics(self, text):
        for index in range(self.topics.count()):
            item = self.topics.item(index)
            topic = item.data(Qt.UserRole)
            item.setHidden(text.casefold() not in (item.text() + tr('support.guide_' + topic)).casefold())
        current = self.topics.currentItem()
        if current is None or current.isHidden():
            self.topics.setCurrentItem(next((self.topics.item(index) for index in range(self.topics.count())
                                            if not self.topics.item(index).isHidden()), None))

    def open_report(self):
        self.owner.open_preferences()
        preferences = self.owner._preferences_dialog
        preferences.tabs.setCurrentWidget(preferences.error_report_scroll)

    def check_updates(self):
        self.update_status.setText(tr('support.checking'))
        self.download_button.setEnabled(False)
        self.owner._product_updates.check(manual=True)

    def _update_busy(self, busy):
        self.check_button.setEnabled(not busy)
        if busy:self.download_button.setEnabled(False)

    def _check_failed(self, error):
        self.update_status.setText(tr('support.check_failed'))
        self.status.setText(tr('support.operation_failed', error=error))
        self.download_button.setEnabled(False)

    def _release_ready(self, result):
        self._release = result
        key = 'support.new_release' if result['newer'] else 'support.current_release'
        self.update_status.setText(tr(key, current=PRODUCT_VERSION, latest=result['version']))
        self._set_markdown(self.update_notes, result['notes'])
        self.status.clear()
        self.download_button.setEnabled(result['newer'])

    def download_release(self):
        self.owner._product_updates.install(self._release)

    def shutdown(self):
        if self.worker is not None:
            self.worker.wait()
        self.hide()


class ProductUpdateController(QObject):
    """One owner for startup/manual discovery, deferral and verified handoff."""
    releaseReady = Signal(object)
    checkFailed = Signal(str)
    busyChanged = Signal(bool)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.worker = None
        self.prompt = None
        self.pending = None
        self.manual = False
        self.prepared = None
        self.helper = None
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._maybe_prompt)
        self.handoff_timer = QTimer(self)
        self.handoff_timer.setInterval(100)
        self.handoff_timer.timeout.connect(self._handoff_ready)

    def startup(self):
        if (not os.environ.get('ABMSYNTH_UI_ONLY') and not os.environ.get('ABMSYNTH_RECOVERY_PROFILE')
                and not self.owner._shutdown_started and self.owner._engine_preferences.check_updates_on_startup):
            self.check()

    def check(self, *, manual=False):
        if self.owner._shutdown_started:return
        if self.worker is not None and self.worker.operation is not updates.discover_release:return
        self.manual = self.manual or manual
        if self.worker is not None:return
        self.busyChanged.emit(True)
        self.worker = SupportWorker(updates.discover_release, (), self)
        self.worker.finished.connect(self._checked)
        self.worker.start()

    def _checked(self):
        worker = self.worker
        self.worker = None
        manual, self.manual = self.manual, False
        self.busyChanged.emit(False)
        if worker.error:
            if manual:self.checkFailed.emit(worker.error)
        else:
            release = worker.result
            self.releaseReady.emit(release)
            enabled = self.owner._engine_preferences.check_updates_on_startup
            if release['newer'] and (manual or (enabled and release['version'] != release['ignored_version'])):
                self.pending = release
                self._pending_manual = manual
                self.timer.start()
                self._maybe_prompt()
        worker.deleteLater()

    def _maybe_prompt(self):
        if self.pending is None or self.owner._shutdown_started:
            self.timer.stop();return
        if not self._pending_manual and not self.owner._engine_preferences.check_updates_on_startup:
            self.pending = None;self.timer.stop();return
        if self.prompt is not None or QApplication.activeModalWidget() is not None:return
        if not self._pending_manual:
            if not self.owner.isVisible() or not self.owner.isActiveWindow() or self.owner.isMinimized():return
            import audio_engine as ae
            if ae.get_soundfont_group_playing_state():return
            player = getattr(self.owner, '_midi_player', None)
            if player is not None and player.transport.state == 'playing':return
            if any(self.owner._recording_owner(kind).recording_state == 'recording' for kind in ('wave', 'midi')):return
        self.timer.stop()
        release, self.pending = self.pending, None
        parent = self.owner._support_dialog if getattr(self.owner, '_support_dialog', None) is not None and self.owner._support_dialog.isVisible() else self.owner
        prompt = NativeMessageBox(parent)
        prompt.setWindowTitle(tr('support.updates'))
        prompt.setIcon(QMessageBox.Information)
        prompt.setText(tr('support.new_release', current=PRODUCT_VERSION, latest=release['version']))
        prompt.setInformativeText(tr('support.update_install_hint'))
        skip = OutlineCheckBox(tr('support.skip_version'))
        prompt.setCheckBox(skip)
        install = prompt.addButton(tr('support.update_action'), QMessageBox.AcceptRole)
        later = prompt.addButton(tr('support.remind_later'), QMessageBox.RejectRole)
        prompt.setEscapeButton(later)
        skip.toggled.connect(lambda checked:install.setEnabled(not checked))
        reader = NativeProductSupportDialog._reader()
        NativeProductSupportDialog._set_markdown(reader, release['notes'] or tr('support.no_release_notes'))
        prompt.setContentWidget(reader)
        self.prompt = prompt
        def finished(_result):
            self.prompt = None
            clicked = prompt.clickedButton()
            if skip.isChecked():
                try:updates.ignore_version(release['version'], self.owner._settings_writer)
                except (OSError, ValueError) as error:self._error(str(error))
            elif clicked is install:
                QTimer.singleShot(0, lambda:self.install(release))
            prompt.deleteLater()
        prompt.finished.connect(finished)
        prompt.open()

    def _error(self, error):
        NativeMessageBox.warning(self.owner, tr('support.updates'),
                                 tr('support.operation_failed', error=error))

    def install(self, release):
        if self.prepared is not None:
            self.finish_close();return
        if self.worker is not None:return
        context = updates.install_context()
        if context is None:
            open_support_url(self.owner, release['assets'].get('installed', release['url']))
            return
        self.busyChanged.emit(True)
        prompt = NativeMessageBox(self.owner)
        prompt.setWindowTitle(tr('support.updates'))
        prompt.setIcon(QMessageBox.Information)
        prompt.setText(tr('support.update_downloading'))
        cancel = prompt.addButton(tr('common.cancel'), QMessageBox.RejectRole)
        prompt.setEscapeButton(cancel)
        self.prompt = prompt
        self.worker = SupportWorker(updates.prepare_update, (release, context), self)
        self.worker.keywords.update(cancelled=self.worker.isInterruptionRequested, progress=self.worker.progress.emit)
        self.worker.progress.connect(self._progress)
        self.worker.finished.connect(self._downloaded)
        prompt.finished.connect(self._cancel_download)
        prompt.open()
        self.worker.start()

    def _cancel_download(self, _result):
        if self.worker is not None:self.worker.requestInterruption()

    def _progress(self, done, total):
        if self.prompt is not None:
            self.prompt.setInformativeText(tr('support.update_progress', done=f'{done/1048576:.1f}', total=f'{total/1048576:.1f}'))

    def _downloaded(self):
        worker, self.worker = self.worker, None
        cancelled = worker.isInterruptionRequested()
        if self.prompt is not None:
            self.prompt.close();self.prompt.deleteLater();self.prompt = None
        if worker.error or cancelled:
            if worker.result:
                import shutil
                shutil.rmtree(worker.result)
            self.busyChanged.emit(False)
            if worker.error and not cancelled:self._error(worker.error)
        else:
            self.prepared = Path(worker.result)
            try:
                self.helper = updates.start_handoff(self.prepared)
                self._handoff_started = __import__('time').monotonic()
                self.handoff_timer.start()
            except OSError as error:
                self._cancel_handoff()
                self._error(str(error))
        worker.deleteLater()

    def _cancel_handoff(self):
        self.handoff_timer.stop()
        if self.prepared is not None:
            try:(self.prepared / 'cancel').touch()
            except OSError:
                if self.helper is not None and self.helper.poll() is None:self.helper.terminate()
        self.prepared = None
        self.helper = None
        self.busyChanged.emit(False)

    def _handoff_ready(self):
        directory = self.prepared
        if (directory / 'ready').is_file():
            self.handoff_timer.stop()
            self.finish_close()
        elif self.helper.poll() is not None or __import__('time').monotonic() - self._handoff_started > 15:
            self._cancel_handoff()
            self._error(tr('support.update_handoff_failed'))

    def finish_close(self):
        # Unsaved work and all audio barriers use the ordinary Close path.
        # Deferred creator/recording saves return here through _request_exit.
        directory = self.prepared
        if directory is None or not (directory / 'ready').is_file():return
        self.owner._restart_requested = False
        previous = getattr(self.owner, '_force_close', False)
        self.owner._force_close = True
        if self.owner.close():
            try:(directory / 'commit').touch()
            except OSError:
                self._cancel_handoff()
                self.owner._restart_requested = True
                self._error(tr('support.update_handoff_failed'))
            QApplication.instance().quit()
        else:
            self.owner._force_close = previous
            creator = getattr(getattr(self.owner, '_group_dialog', None), 'instrument_creator', None)
            deferred = getattr(self.owner, '_recording_after_save', None) is not None or getattr(creator, '_after_save', None)
            if not deferred:self._cancel_handoff()

    def shutdown(self):
        self.timer.stop()
        if self.worker is not None:
            self.worker.requestInterruption()
            self.worker.wait()


class NativeMaintenancePanel(NativeCard):
    """Saved-settings actions belong to preferences, independently of its unsaved fields."""
    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.worker = None
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        body = set_layout(QVBoxLayout(self), METRICS['card_padding'], METRICS['compact_content_gap'])
        title = QLabel(tr('support.settings_title'))
        title.setObjectName('CardTitle')
        body.addWidget(title)
        from settings import settings_path
        rows = ((tr('support.settings_folder'), lambda: reveal_support_path(self, settings_path().parent)),
                (tr('support.logs_folder'), lambda: reveal_support_path(self, settings_path().parent / 'logs')),
                (tr('support.backup'), self.backup), (tr('support.restore'), self.restore),
                (tr('support.recovery_start'), self.recovery), (tr('support.cancel_restore'), self.cancel_restore))
        self.buttons = []
        for first in range(0, len(rows), 2):
            row = control_row_layout()
            for caption, callback in rows[first:first + 2]:
                button = make_action_button(caption, callback)
                button.setMinimumWidth(0)
                button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
                row.addWidget(button, 1)
                self.buttons.append(button)
            body.addLayout(row)
        self.status = QLabel(tr('support.settings_scope'))
        self.status.setWordWrap(True)
        self.status.setMinimumWidth(0)
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        body.addWidget(self.status)
        self.buttons[-1].setEnabled(support.restore_request_path().is_file())

    def _start(self, operation, arguments, completed):
        if self.worker is not None:
            return
        for button in self.buttons:
            button.setEnabled(False)
        self.status.setText(tr('support.working'))
        self.worker = SupportWorker(operation, arguments, self)
        self._completed = completed
        self.worker.finished.connect(self._finished)
        self.worker.start()

    def _finished(self):
        worker = self.worker
        self.worker = None
        for button in self.buttons:
            button.setEnabled(True)
        self.buttons[-1].setEnabled(support.restore_request_path().is_file())
        if worker.error:
            self.status.setText(tr('support.operation_failed', error=worker.error))
        else:
            self._completed(worker.result)
        worker.deleteLater()

    def backup(self):
        path, _ = QFileDialog.getSaveFileName(self, tr('support.backup'), 'ABMSynth-settings.json', tr('support.backup_filter'))
        if path:
            self._start(support.save_settings_backup, (path, self.owner._settings_writer),
                        lambda target: self.status.setText(tr('support.backup_saved', path=target)))

    def restore(self):
        path, _ = QFileDialog.getOpenFileName(self, tr('support.restore'), '', tr('support.backup_filter'))
        if path:
            self._start(support.read_backup, (path,), self._preview_restore)

    def _preview_restore(self, record):
        from configparser import ConfigParser
        parser = ConfigParser(interpolation=None)
        parser.read_string(record['ini'])
        prompt = NativeMessageBox(self)
        prompt.setWindowTitle(tr('support.restore'))
        prompt.setIcon(QMessageBox.Question)
        prompt.setText(tr('support.restore_prompt', version=record.get('version', '—'),
                          sections=len(parser.sections())))
        prompt.setInformativeText(support.backup_summary(record))
        accept = prompt.addButton(tr('support.restore_schedule'), QMessageBox.AcceptRole)
        prompt.addButton(tr('common.cancel'), QMessageBox.RejectRole)
        prompt.exec()
        if prompt.clickedButton() is accept:
            self._start(support.stage_settings_restore, (record,),
                        lambda _: self.status.setText(tr('support.restore_pending')))
        else:
            self.status.setText(tr('support.settings_scope'))

    def cancel_restore(self):
        try:
            support.cancel_settings_restore()
        except OSError as error:
            self.status.setText(tr('support.operation_failed', error=error))
        else:
            self.buttons[-1].setEnabled(False)
            self.status.setText(tr('support.restore_cancelled'))

    def recovery(self):
        prompt = NativeMessageBox(self)
        prompt.setWindowTitle(tr('support.recovery_start'))
        prompt.setIcon(QMessageBox.Question)
        prompt.setText(tr('support.recovery_prompt'))
        accept = prompt.addButton(tr('support.recovery_start'), QMessageBox.AcceptRole)
        prompt.addButton(tr('common.cancel'), QMessageBox.RejectRole)
        prompt.exec()
        if prompt.clickedButton() is accept:
            self.owner._recovery_restart_requested = True
            if not self.window().close():
                self.owner._recovery_restart_requested = False
                return
            self.owner.restart()
            if not self.owner._restart_requested:
                self.owner._recovery_restart_requested = False

    def shutdown(self):
        if self.worker is not None:
            self.worker.wait()


class NativeGroupFilesDialog(NativeDialog):
    def __init__(self, editor):
        super().__init__(editor)
        self.group = copy.deepcopy(editor.group)
        self.worker = None
        self.inventory = []
        self.setWindowTitle(tr('support.group_files'))
        self.setProperty('supportTopic', 'fonts')
        self.setMinimumSize(0, 0)
        self.resize(760, 480)
        body = set_layout(QVBoxLayout(self), METRICS['outer'], METRICS['section_gap'])
        body.setSizeConstraint(QLayout.SetNoConstraint)
        self.list = QListWidget()
        self.list.setMinimumSize(0, 0)
        self.list.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.list.setAccessibleName(tr('support.group_files'))
        body.addWidget(self.list, 1)
        self.status = QLabel(tr('support.scanning'))
        self.status.setWordWrap(True)
        self.status.setMinimumWidth(0)
        body.addWidget(self.status)
        self.acknowledge = make_action_button(tr('support.collect_files'), self.collect)
        self.acknowledge.setEnabled(False)
        row = control_row_layout()
        row.addWidget(self.acknowledge)
        self.cancel_button = make_action_button(tr('common.cancel'), self.cancel_work)
        self.cancel_button.setEnabled(False)
        row.addWidget(self.cancel_button)
        row.addStretch(1)
        row.addWidget(make_action_button(tr('common.close'), self.close))
        body.addLayout(row)
        apply_native_text_metrics(self)
        self._start(support.group_file_inventory, (self.group,), self._inventory_ready)

    def _start(self, operation, arguments, completed):
        self._completed = completed
        self.worker = SupportWorker(operation, arguments, self)
        self.worker.keywords['cancelled'] = self.worker.isInterruptionRequested
        self.cancel_button.setEnabled(True)
        self.worker.finished.connect(self._finished)
        self.worker.start()

    def _finished(self):
        worker = self.worker
        self.worker = None
        self.cancel_button.setEnabled(False)
        if worker.error:
            self.status.setText(tr('support.operation_failed', error=worker.error))
            self.acknowledge.setEnabled(bool(self.inventory) and not any(row['error'] for row in self.inventory))
        else:
            self._completed(worker.result)
        worker.deleteLater()

    def _inventory_ready(self, inventory):
        self.inventory = inventory
        for row in inventory:
            key = ('support.file_issue' if row['error'] else
                   'support.generated_wave' if row['kind'] == 'generated' else 'support.file_ready')
            self.list.addItem(tr(key, path=row['path'], error=row['error']))
        self.acknowledge.setEnabled(bool(inventory) and not any(row['error'] for row in inventory))
        self.status.setText(tr('support.collect_scope'))

    def cancel_work(self):
        if self.worker is not None:
            self.worker.requestInterruption()
            self.cancel_button.setEnabled(False)

    def collect(self):
        parent = QFileDialog.getExistingDirectory(self, tr('support.collect_destination'))
        if parent:
            from PySide6.QtWidgets import QInputDialog
            name, accepted = QInputDialog.getText(self, tr('support.collect_files'), tr('support.folder_name'), text='ABMSynth-group')
            if not accepted:
                return
            if not name.strip() or name in ('.', '..') or any(c in name for c in '/\\:<>"|?*'):
                self.status.setText(tr('support.folder_invalid'))
                return
            self.acknowledge.setEnabled(False)
            self.status.setText(tr('support.working'))
            self._start(support.collect_group_files, (self.group, str(Path(parent) / name), self.inventory), self._collected)

    def _collected(self, path):
        self.acknowledge.setEnabled(True)
        self.status.setText(tr('support.collected', path=path))
        reveal_support_path(self, path)

    def closeEvent(self, event):
        if self.worker is not None:
            event.ignore()
        else:
            super().closeEvent(event)

    def shutdown(self):
        if self.worker is not None:
            self.worker.requestInterruption()
            self.worker.wait()
        self.hide()

    def reject(self):
        if self.worker is None:
            super().reject()
