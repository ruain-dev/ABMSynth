"""Native settings and SoundFont dialogs plus instrument table delegates."""

from __future__ import annotations

import os
import math
import threading
import time
import shutil
import tempfile
import queue
from pathlib import Path
from dataclasses import replace

import audio_engine as ae
from PySide6.QtCore import QEvent, QRect, QRectF, QSize, Qt, QThread, QTimer, Signal, QRegularExpression, QStandardPaths, QUrl, QObject, QItemSelectionModel, QItemSelection
from PySide6.QtGui import QColor, QCursor, QFontMetrics, QPainter, QPainterPath, QRegion, QPen, QRegularExpressionValidator, QIcon, QPixmap, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QAbstractSpinBox, QButtonGroup, QCheckBox, QDialog, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLayout, QLineEdit, QMainWindow,
    QMessageBox, QMenu, QProgressBar, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QListWidget, QListWidgetItem, QSlider,
    QSplitter, QStackedWidget, QStyle, QStyleOptionComboBox, QStyledItemDelegate, QTabWidget, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget, QTextBrowser, QPlainTextEdit,
 )
from event_log import log_text
from i18n import tr
from native_layout import (
    reflow_card_columns,
    CARD_ROW_GAPS, FORM_ROW_HEIGHT, ElasticLabel, ElasticPanel, WidthMeasuredPanel, WrappingControlLayout,
    install_three_column_form_row, make_horizontally_elastic, set_layout,
    fit_dialog_contents, fit_plain_dialog_contents, align_form_labels,
    control_row_layout, invalidate_layout_chain, bordered_page_layout, automatic_window_fit_allowed,
)
from native_widgets import (
    DARK, LIGHT, INSTRUMENT_TABLE, INSTRUMENT_EXTRA_FIELDS, METRICS, AnimatedComboBox, InstrumentTableSpec, NativeCard, NativeDialog,
    COLOR_THEMES, color_palette, widget_palette, disabled_control_color, refresh_native_pointer_feedback, set_native_cursor,
    connect_native_pointer_model, refresh_changed_pointer_feedback,
    EditableNumberLabel, NativeRotatedLabel, NativeSegmentedChoice, NativeSlider, NativeDecimalBox,
    NativeControlRow, NativeDisclosure, NativeInlineNumberEdit, NativeKnobField, NativeLayoutSnapshot, NativeMidiActivityBar, NativeNumberBox, NativeToggleSwitch, NativeWindowSizeMotion, MIDI_ACTIVITY_INTERVAL_MS,
    NativeTransportButton, NativeAuditionButton, NativeHoldButton, make_tuning_audition, make_instrument_audition, NativeMessageBox, NativeMenu, NativeRackButton, NativeRackList, NativeRowList, NativeFilterButton, NativeFXVisual, NativeOptionalLabel,
    OutlineCheckBox, apply_native_text_metrics, present_numeric_selection, set_button_primary,
    install_effect_tooltip, make_action_button, make_control_section, add_section_action, make_parameter_tabs, make_navigation_tabs, tooltip_display_text,
    set_channel_protocol_caption, paint_outline_check_indicator,
    transparent_scroll_area_stylesheet,
    set_transparent_scroll_content,
 )
from settings import EnginePreferences, LIVE_PREFERENCE_FIELDS, RESTART_PREFERENCE_FIELDS, RESTART_REQUIRED_FIELDS, SAMPLE_RATES, RECORDING_EXTENSIONS, RECORDING_BITRATES, engine_ini_values, import_bmsynth_engine_values, read_bmsynth_ini, save_engine_preferences, update_sections, valid_recording_basename, recording_filename
from audio_output import AudioStartError, OUTPUT_TIMING_SETTINGS, output_timing_availability
from soundfont_files import INSTRUMENT_VOLUME_MAX, PRESET_EXPRESSION_GROUPS, PRESET_EXPRESSION_RANGES, PresetOverride, SoundFontEntry, SoundFontGroup, load_sfg, save_sfg, save_instrument_export, resolve_effective_instruments, validate_group
from soundfont_files import inspect_soundfont_controllers,SF2_CC_GENERATOR_NAMES, SoundFontMapping, resolve_mapped_instruments
from soundfont_runtime import SoundFontActivity, SoundFontRuntimeError
from windows_integration import is_enabled as is_windows_startup_enabled, set_enabled as set_windows_startup_enabled
from effect_presentation import effect_tooltip, format_effect_value, audio_fx_parameter_tooltip, audio_fx_parameter_caption, audio_fx_parameter_value, FX_VISUAL_FAMILIES, drum_key_caption
from effect_control import RACK_BQF_KINDS,SYSTEM_RESET_RECEIVERS
from midi_input_runtime import (AUXILIARY_CC, AUXILIARY_CC_DESTINATIONS, SOUNDFONT_MODULATOR_CC, NATIVE_ADDITIONAL_CC,
                                DRUM_NEUTRAL, DRUM_PARAMETER_ORDER, DRUM_PARAMETERS, SCALE_PARAMETER, EXPRESSION_DEFAULTS,
                                EXPRESSION_EVENTS, EXPRESSION_MINIMUMS, EXPRESSION_MAXIMUMS, CHANNEL_PARAMETERS,
                                CHANNEL_MODE_SWITCHES, CHANNEL_CC_DEFAULTS,
                                CHANNEL_FUNCTION_TARGETS, CHANNEL_RPN_DEFAULTS,
                                CHANNEL_SYSEX_ROUTES, channel_cc_target_value)


def sample_rate_caption(rate: int) -> str:
    """Present exact integer-Hz data as kHz without float rounding."""
    whole, fraction = divmod(int(rate), 1000)
    number = str(whole)
    if fraction:
        number += "." + f"{fraction:03d}".rstrip("0")
    return tr("preferences.sample_rate_value", rate=number)


class NativeAudioFXStack(QWidget):
    """Mount one cached form; QStackedLayout measures hidden forms too.

    Only layout membership changes. Widgets/controls are never recreated and
    have no audio handles. This avoids both clipped wrapping and phantom space.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._current, self._pages = None, []
        policy = QSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        # The mounted page's layout determines height-for-width. Forcing it
        # on an ordinary non-HFW page makes Qt's internal layout return0.
        self.setSizePolicy(policy)
        self.setMinimumSize(0, 0)
        set_layout(QVBoxLayout(self), 0, 0)

    def addWidget(self, widget):
        widget.setParent(self); widget.hide(); self._pages.append(widget)
        if self._current is None: self.setCurrentWidget(widget)

    def currentWidget(self):
        return self._current

    def setCurrentIndex(self, index):
        if 0 <= index < len(self._pages):
            self.setCurrentWidget(self._pages[index])

    def setCurrentWidget(self, widget):
        if widget is self._current: return
        if self._current is not None:
            self.layout().removeWidget(self._current); self._current.hide()
        self._current = widget
        self.layout().addWidget(widget); widget.show()
        invalidate_layout_chain(self)

    def sizeHint(self):
        return self.currentWidget().sizeHint() if self.currentWidget() else QSize(0, 0)

    def minimumSizeHint(self):
        current = self.currentWidget()
        return QSize(0, current.minimumSizeHint().height()) if current else QSize(0, 0)

    def hasHeightForWidth(self):
        current = self.currentWidget()
        if current is None: return False
        layout = current.layout()
        return layout.hasHeightForWidth() if layout is not None else current.hasHeightForWidth()

    def heightForWidth(self, width):
        current = self.currentWidget()
        if current is None: return 0
        layout = current.layout()
        if layout is None: return current.heightForWidth(width) if current.hasHeightForWidth() else current.sizeHint().height()
        return layout.totalHeightForWidth(width) if layout.hasHeightForWidth() else layout.totalSizeHint().height()


class NativeAudioFXBody(QWidget):
    """Measured list+editor, reflowing below without horizontal size floors."""
    widthChanged = Signal()
    def __init__(self, navigation, editor, parent=None):
        super().__init__(parent)
        self.navigation, self.editor = navigation, editor
        navigation.setParent(self)
        # Only the output inspector needs a local vertical viewport: the main
        # top-anchored canvas deliberately clips tail cards under pressure.
        # Channel composition already lives in its dialog's outer scroll area.
        self.editor_scroll=QScrollArea(self)
        self.editor_scroll.setFrameShape(QFrame.NoFrame)
        self.editor_scroll.setMinimumSize(0,0)
        self.editor_scroll.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Ignored)
        self.editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.editor_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.editor_scroll.setWidgetResizable(False)
        self.editor_scroll.setAccessibleName(tr('audio_fx.visual.scroll_accessible'))
        self.editor_scroll.setStyleSheet(transparent_scroll_area_stylesheet())
        # QScrollArea.setWidget turns content auto-fill on. Use the common
        # mount so description/graph/controls inherit the enclosing card too.
        set_transparent_scroll_content(self.editor_scroll,editor)
        self.editor_scroll.viewport().installEventFilter(self)
        self._sizing_editor=False
        self.setMinimumSize(0, 0)
        policy = QSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        policy.setHeightForWidth(True); self.setSizePolicy(policy)

    def wide(self, width):
        editor_preference = METRICS["form_label_width"] + METRICS["form_value_width"] + FORM_ROW_HEIGHT * 5
        return not self.editor.isHidden() and width >= self.navigation.sizeHint().width() + editor_preference + 8

    def measures(self, width):
        if self.editor.isHidden():return width,0,self.navigation.layout().totalSizeHint().height(),0
        left = min(220, max(0, width // 3)) if self.wide(width) else width
        right = max(0, width-left-8) if self.wide(width) else width
        nav_height = self.navigation.layout().totalSizeHint().height()
        layout = self.editor.layout()
        editor_height = layout.totalHeightForWidth(right) if layout.hasHeightForWidth() else layout.totalSizeHint().height()
        return left, right, nav_height, max(0, editor_height)

    def hasHeightForWidth(self): return True
    def heightForWidth(self, width):
        _, _, left, right = self.measures(width)
        if self.editor.isHidden():return left
        return max(left, right) if self.wide(width) else left + right + 8
    def sizeHint(self): return QSize(720, self.heightForWidth(self.width() or 720))
    def minimumSizeHint(self): return QSize(0, 0)

    def content_changed(self):
        self.editor.layout().invalidate()
        self.updateGeometry()
        self.resizeEvent(None)

    def _sync_editor_extent(self):
        if self._sizing_editor:return
        self._sizing_editor=True
        try:
            viewport=self.editor_scroll.viewport()
            width=max(0,viewport.width());layout=self.editor.layout()
            natural=layout.totalHeightForWidth(width) if layout.hasHeightForWidth() else layout.totalSizeHint().height()
            self.editor.resize(width,max(viewport.height(),natural))
        finally:self._sizing_editor=False

    def eventFilter(self,watched,event):
        if watched is self.editor_scroll.viewport() and event.type()==QEvent.Resize:self._sync_editor_extent()
        return super().eventFilter(watched,event)

    def resizeEvent(self, event):
        if event is not None: super().resizeEvent(event)
        left, right, lh, rh = self.measures(self.width())
        self.editor_scroll.setVisible(not self.editor.isHidden())
        if self.editor.isHidden():
            self.navigation.setGeometry(0,0,self.width(),self.height())
        elif self.wide(self.width()):
            self.navigation.setGeometry(0, 0, left, self.height())
            self.editor_scroll.setGeometry(left+8, 0, right, self.height())
        else:
            # Under vertical pressure keep an inspector viewport reachable.
            # The catalog already scrolls; it must not consume the entire body.
            nav_height=min(lh,max(0,self.height()//3)) if self.height()<lh+rh+8 else lh
            self.navigation.setGeometry(0, 0, left, nav_height)
            self.editor_scroll.setGeometry(0, nav_height+8, right, max(0, self.height()-nav_height-8))
        self._sync_editor_extent()
        if event is not None and event.size().width() != event.oldSize().width():
            self.widthChanged.emit()


class NativeAudioFXChannelBody(WidthMeasuredPanel):
    """Channel composition, sharing controls and state with the output editor."""
    widthChanged=Signal()

    def __init__(self,navigation,editor,parent=None):
        super().__init__(parent)
        self.navigation,self.editor=navigation,editor
        body=set_layout(QVBoxLayout(self),0,8)
        body.setAlignment(Qt.AlignTop)
        make_horizontally_elastic(navigation,QSizePolicy.Fixed)
        make_horizontally_elastic(editor,QSizePolicy.Fixed)
        body.addWidget(navigation);body.addWidget(editor)

    def content_changed(self):
        invalidate_layout_chain(self)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if event.size().width()!=event.oldSize().width():self.widthChanged.emit()


class NativeAudioFXPanel(QWidget):
    """Compact rack navigation + cached parameter forms; no audio ownership."""
    changed = Signal(object)

    def __init__(self, owner, *, parent=None, runtime=None, groups=None,
                 save=None, geometry_changed=None, window_fit=None, sample_rate=None, channel_presentation=False, parameter_context=None):
        super().__init__(parent or owner)
        self.owner, self.slots, self._foreign = owner, (), []
        self.effect_runtime=runtime or owner.effect_runtime
        self.effect_groups=groups
        self.channel_presentation=channel_presentation
        self.parameter_context=parameter_context
        self._save=save or owner._queue_ui_save
        self._geometry_changed=geometry_changed or owner._sync_effect_card_height
        self._window_fit=window_fit or owner._queue_effect_window_fit
        self._sample_rate=sample_rate
        self._forms, self._blocked = {}, False
        self._visuals = {}
        self._advanced = {}
        self._editor_resize_timer = QTimer(self)
        self._editor_resize_timer.setSingleShot(True)
        self._editor_resize_timer.timeout.connect(self._flush_editor_resize)
        self._committed_slots, self._committed_enabled = (), True
        self._pending_token = None
        self._cleanup_token = None
        self._preparing_labels = {}
        self._prepare_timer = QTimer(self)
        self._prepare_timer.setInterval(16)
        self._prepare_timer.timeout.connect(self._poll_preparation)
        self.setMinimumWidth(0)
        layout = set_layout(QVBoxLayout(self), 0, CARD_ROW_GAPS["effects"])
        self.enabled_switch = NativeToggleSwitch("")
        self.enabled_switch.setChecked(True)
        self.enabled_switch.setAccessibleName(tr('audio_fx.enabled_accessible'))
        install_effect_tooltip(tr("audio_fx.enabled_help"), self.enabled_switch)
        self.enabled_switch.toggled.connect(self.toggle_enabled)
        self.selection = NativeRackList()
        self.selection.currentRowChanged.connect(self.select_effect)
        self.selection.toggleRequested.connect(self.toggle_row)
        self.selection.editorRequested.connect(self.focus_editor)
        self.selection.moveRequested.connect(self.drop_effect)
        self.selection.removeRequested.connect(self.remove_effect)
        self.selection.stepRequested.connect(self.move_effect)
        self.selection.contextRequested.connect(self.show_context_menu)
        navigation = WidthMeasuredPanel() if channel_presentation else ElasticPanel(preferred_width=220)
        navigation.setMinimumSize(0, 0)
        nav_layout = set_layout(QVBoxLayout(navigation), 0, 4)
        if channel_presentation:nav_layout.setAlignment(Qt.AlignTop)
        nav_layout.setSizeConstraint(QLayout.SetNoConstraint)
        self.command_bar = QWidget()
        self.command_bar.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        toolbar = set_layout(QHBoxLayout(self.command_bar), 0, 0)
        toolbar.setSizeConstraint(QLayout.SetNoConstraint)
        self.add_button = NativeRackButton("add", tr("audio_fx.add"), lambda: self.show_add_menu())
        self.remove_button = NativeRackButton("remove", tr("audio_fx.remove"), self.remove_effect)
        self.up_button = NativeRackButton('left' if channel_presentation else 'up', tr("audio_fx.up"), lambda: self.move_effect(-1))
        self.down_button = NativeRackButton('right' if channel_presentation else 'down', tr("audio_fx.down"), lambda: self.move_effect(1))
        for button in (self.add_button, self.remove_button, self.up_button, self.down_button): toolbar.addWidget(button)
        toolbar.addStretch(1)
        self.preparing_hint = None
        toolbar.addWidget(self.enabled_switch)
        nav_layout.addWidget(self.command_bar)
        nav_layout.addWidget(self.selection, 1)
        editor = QWidget(); editor.setMinimumSize(0, 0)
        editor_layout = set_layout(QVBoxLayout(editor), 0, 4)
        editor_layout.setSizeConstraint(QLayout.SetNoConstraint)
        self.stack = NativeAudioFXStack()
        policy = self.stack.sizePolicy(); policy.setHeightForWidth(True); self.stack.setSizePolicy(policy)
        self.stack.layout().setSizeConstraint(QLayout.SetNoConstraint)
        self.empty = QWidget()  # The rack already paints its empty state.
        self.stack.addWidget(self.empty); editor_layout.addWidget(self.stack)
        editor_layout.addStretch(1)
        if channel_presentation:
            # Channel composition has a compact chain above its controls.
            # Commands, IDs, async receipts and individual input widgets stay
            # shared; the output inspector's split pane is not embedded here.
            self.selection.set_compact_horizontal()
            self.body=NativeAudioFXChannelBody(navigation,editor,self)
            self.body.widthChanged.connect(self._queue_editor_resize)
        else:
            self.body = NativeAudioFXBody(navigation, editor, self)
            self.body.widthChanged.connect(self._queue_editor_resize)
        layout.addWidget(self.body)
        self.foreign_hint = ElasticLabel(tr("audio_fx.foreign_hint")); self.foreign_hint.setWordWrap(True)
        layout.addWidget(self.foreign_hint); self.foreign_hint.hide()
        self.refresh(-1)

    def create_add_menu(self):
        from effect_control import AUDIO_FX_GROUPS
        from effect_control import AUDIO_FX_MAX_SLOTS
        menu = NativeMenu(self)
        menu.setToolTipsVisible(True)
        for group, kinds in (self.effect_groups or AUDIO_FX_GROUPS).items():
            if not kinds:continue
            submenu = menu.addMenu(tr(f"audio_fx.group.{group}"))
            submenu.setToolTipsVisible(True)
            for kind in kinds:
                action = submenu.addAction(tr(f"audio_fx.kind.{kind}"))
                action.setToolTip(self._effect_help(kind))
                action.setData(kind); action.setEnabled(len(self.slots) < AUDIO_FX_MAX_SLOTS)
                action.triggered.connect(lambda checked=False, kind=kind: self.add_effect(kind))
        return menu


    def show_add_menu(self, position=None):
        menu = self.create_add_menu()
        try: menu.exec(position or self.add_button.mapToGlobal(self.add_button.rect().bottomLeft()))
        finally: menu.deleteLater()

    def show_context_menu(self, position):
        local = self.selection.viewport().mapFromGlobal(position)
        item = self.selection.itemAt(local)
        if item is None:
            self.show_add_menu(position); return
        self.selection.setCurrentItem(item)
        menu = NativeMenu(self); add_menu = self.create_add_menu()
        # Ownership only. QWidget.setParent(parent) resets Qt.Popup to Widget,
        # clipping this160px category menu inside its132px parent menu.
        add_menu.setParent(menu, add_menu.windowFlags())
        menu.addMenu(add_menu).setText(tr("audio_fx.add"))
        action = menu.addAction(tr("audio_fx.enabled")); action.setCheckable(True)
        action.setChecked(self.slots[self.selection.currentRow()].enabled)
        action.triggered.connect(lambda *_: self.toggle_row(self.selection.currentRow()))
        menu.addSeparator(); menu.addAction(tr("audio_fx.remove"), self.remove_effect)
        try: menu.exec(position)
        finally: menu.deleteLater()

    def focus_editor(self, index):
        self.selection.setCurrentRow(index)
        if 0 <= index < len(self.slots):
            page = self.stack.currentWidget()
            controls = self._forms[self.slots[index].kind][1]
            for control in controls:
                if control.isVisibleTo(page) and control.isEnabled():
                    (control.slider if isinstance(control, NativeControlRow) else
                     control.knob if isinstance(control,NativeKnobField) else control).setFocus(Qt.TabFocusReason)
                    break

    def toggle_row(self, index):
        if 0 <= index < len(self.slots):
            self.selection.setCurrentRow(index)
            self.toggle_effect(not self.slots[index].enabled)

    def drop_effect(self, identity, insertion):
        source = next((i for i, slot in enumerate(self.slots) if slot.identity == identity), -1)
        if source < 0: return False
        target = max(0, min(len(self.slots), insertion)) - (1 if insertion > source else 0)
        if source == target: return True
        selected_id = self.slots[self.selection.currentRow()].identity if self.selection.currentRow() >= 0 else identity
        slots = list(self.slots); slots.insert(target, slots.pop(source))
        selected = next(i for i, slot in enumerate(slots) if slot.identity == selected_id)
        return self._publish(slots, selected)


    def processing_enabled(self):
        """One positive enabled state in the UI, INI and native consumer."""
        return self.enabled_switch.isChecked()

    def restore_defaults(self):
        index = self.selection.currentRow()
        if index >= 0:
            slots = list(self.slots); slots[index] = replace(slots[index],
                enabled=slots[index].enabled if self.channel_presentation else False, values=())
            self._publish(slots,index,force_parameters={slots[index].identity:tuple(range(len(self.slots[index].values)))})

    def load_ini(self, raw, enabled=True, *, report=None):
        import json
        from effect_control import AUDIO_FX_MAX_SLOTS, audio_fx_from_ini, AudioFXSlot
        from uuid import uuid4
        identities = set(); slots = []
        for slot in audio_fx_from_ini(raw,report=report):
            identity = slot.identity
            if not identity or identity in identities: identity = uuid4().hex
            identities.add(identity); slots.append(replace(slot, identity=identity))
        self.slots = tuple(slots)
        self._committed_slots, self._committed_enabled = self.slots, bool(enabled)
        self._pending_token = None
        self._cleanup_token = None
        self._prepare_timer.stop(); self._show_preparing(False)
        self._blocked = True; self.enabled_switch.setChecked(enabled); self._blocked = False
        self._foreign = []
        try:
            from configuration_data import read_settings_json
            records = read_settings_json(raw)
            if isinstance(records, list):
                accepted = 0
                for index, record in enumerate(records):
                    try:
                        if not audio_fx_from_ini(json.dumps([record])):raise ValueError('unsupported audio FX record')
                        if accepted >= AUDIO_FX_MAX_SLOTS:
                            self._foreign.append((index, record))
                        accepted += 1
                    except (ValueError, TypeError, KeyError, AttributeError):
                        self._foreign.append((index, record))
        except (ValueError, TypeError):
            pass
        self.foreign_hint.setVisible(bool(self._foreign))
        self.refresh(0 if self.slots else -1)

    def ini_value(self):
        import json
        from effect_control import audio_fx_ini
        records = json.loads(audio_fx_ini(self.applied_slots()))
        for index, record in self._foreign:
            records.insert(min(index, len(records)), record)
        return json.dumps(records, separators=(",", ":"), ensure_ascii=False)

    def _publish(self, slots, selected, *, force_parameters=None):
        from effect_control import validate_audio_fx
        try:
            slots = validate_audio_fx(slots)
            edit=getattr(self.effect_runtime,'request_audio_fx_edit',None)
            if edit is not None:
                token,result,slots=edit(slots,self.processing_enabled(),self.slots,force_parameters or {})
            else:token,result=self.effect_runtime.request_audio_fx(slots,self.processing_enabled())
            if result is False:
                raise ValueError("Audio FX backend rejected configuration")
        except (ValueError, OSError):
            self.owner.append_log(log_text("audio_fx.invalid"))
            self.refresh(self.selection.currentRow())
            return False
        self.slots = slots
        # Rows describe the requested chain, not the still-audible old chain.
        # Establish its preparation phase before refreshing semantic status.
        self._pending_token = token if result is None else None
        self.refresh(selected)
        self._cleanup_token = token if result is not None else None
        self._show_preparing(result is None)
        if result is None:
            self._prepare_timer.start()
        else:
            self._commit_presentation()
            if token is not None: self._prepare_timer.start()
        return True

    def applied_slots(self):
        pending=self._pending_token
        if pending is not None and self.effect_runtime.completed_audio_fx_request(pending) is not True:return self._committed_slots
        return self.slots

    def applied_enabled(self):
        pending=self._pending_token
        if pending is not None and self.effect_runtime.completed_audio_fx_request(pending) is not True:return self._committed_enabled
        return self.processing_enabled()

    def _commit_presentation(self):
        self._committed_slots, self._committed_enabled = self.slots, self.processing_enabled()
        self._prepare_timer.stop(); self._show_preparing(False)
        self.changed.emit(self.slots)
        self._save()

    def _poll_preparation(self):
        if self._pending_token is None:
            if self._cleanup_token is not None:
                result = self.effect_runtime.poll_audio_fx(self._cleanup_token)
                if result is not None:
                    self._cleanup_token = None; self._prepare_timer.stop()
            return
        result = self.effect_runtime.poll_audio_fx(self._pending_token)
        if result is None: return
        self._pending_token = None
        if result:
            self._commit_presentation()
        else:
            selected_id = (self.slots[self.selection.currentRow()].identity
                           if 0 <= self.selection.currentRow() < len(self.slots) else None)
            self.slots = self._committed_slots
            self._blocked = True; self.enabled_switch.setChecked(self._committed_enabled); self._blocked = False
            self.refresh(next((i for i, slot in enumerate(self.slots) if slot.identity == selected_id), -1))
            self._prepare_timer.stop(); self._show_preparing(False)
            self.owner.append_log(log_text("audio_fx.invalid"))

    def _show_preparing(self, preparing):
        for label in self._preparing_labels.values(): label.setVisible(preparing)
        self._refresh_slot_information()

    def toggle_enabled(self, checked):
        if self._blocked: return
        if not self._publish(self.slots, self.selection.currentRow()):
            self._blocked = True; self.enabled_switch.setChecked(not checked); self._blocked = False
            self.refresh(self.selection.currentRow())

    def add_effect(self, kind):
        from effect_control import AudioFXSlot
        from uuid import uuid4
        asset = ""
        if kind == "convolution":
            asset, _filter = QFileDialog.getOpenFileName(self, tr("audio_fx.ir_choose"), "", tr("audio_fx.ir_filter"))
            if not asset: return
        self._publish((*self.slots, AudioFXSlot(kind, enabled=False, asset=asset, identity=uuid4().hex)), len(self.slots))

    def choose_impulse(self):
        index = self.selection.currentRow()
        if index < 0: return
        asset, _filter = QFileDialog.getOpenFileName(self, tr("audio_fx.ir_choose"), self.slots[index].asset,
                                                   tr("audio_fx.ir_filter"))
        if asset:
            slots = list(self.slots); slots[index] = replace(slots[index], asset=asset)
            self._publish(slots, index)

    def remove_effect(self):
        index = self.selection.currentRow()
        if index >= 0: self._publish(self.slots[:index] + self.slots[index + 1:], min(index, len(self.slots) - 2))

    def move_effect(self, direction):
        index = self.selection.currentRow(); target = index + direction
        if 0 <= target < len(self.slots):
            slots = list(self.slots); slots[index], slots[target] = slots[target], slots[index]
            self._publish(slots, target)

    def toggle_effect(self, checked):
        if self._blocked: return
        index = self.selection.currentRow()
        if index >= 0:
            slots = list(self.slots); slots[index] = replace(slots[index], enabled=checked)
            self._publish(slots, index)

    def set_parameter(self, parameter, value):
        self.set_parameters({parameter:value})

    def set_parameters(self, changes):
        """One publication for a two-axis gesture, through the existing owner."""
        if self._blocked: return
        index = self.selection.currentRow()
        if index >= 0:
            slot = self.slots[index]; values = list(slot.values)
            for parameter,value in changes.items():values[parameter]=value
            if slot.kind in RACK_BQF_KINDS and 0 in changes:
                values[0] = min(values[0], self._filter_ceiling())
            if slot.kind == "chorus_fx":
                if 3 in changes: values[4] = max(values[4], values[3])
                elif 4 in changes: values[3] = min(values[3], values[4])
            if tuple(values)==slot.values:return
            slots = list(self.slots); slots[index] = replace(slot, values=tuple(values))
            forced=(3,4) if slot.kind=='chorus_fx' and any(p in changes for p in (3,4)) else tuple(changes)
            self._publish(slots,index,force_parameters={slot.identity:forced})

    def _visual_edit(self,visual,changes):
        """Read only the current identity; never apply an old drag to a new slot."""
        from effect_control import AUDIO_FX_PARAMETERS
        index=self.selection.currentRow()
        if self._blocked or not 0<=index<len(self.slots):return
        slot=self.slots[index]
        if visual.identity!=(id(self.effect_runtime),slot.identity) or visual.kind!=slot.kind:return
        specs=AUDIO_FX_PARAMETERS[slot.kind];patch={}
        for number,spec in enumerate(specs):
            if spec.key not in changes:continue
            value=max(spec.minimum,min(spec.maximum,changes[spec.key]))
            # Graph editing snaps to the same admitted precision as the fields.
            steps=math.floor((value-spec.minimum)/spec.step+.5)
            patch[number]=spec.validate(max(spec.minimum,min(spec.maximum,spec.minimum+steps*spec.step)))
        self.set_parameters(patch)

    def _visual_cc_menu(self,visual,key,point):
        if self.parameter_context is None:return
        from effect_control import AUDIO_FX_PARAMETERS
        index=self.selection.currentRow()
        if not 0<=index<len(self.slots):return
        slot=self.slots[index]
        if visual.identity!=(id(self.effect_runtime),slot.identity):return
        number=next(i for i,p in enumerate(AUDIO_FX_PARAMETERS[slot.kind]) if p.key==key)
        control=self._forms[slot.kind][1][number]
        self._show_parameter_cc(number,control,visual,point)

    def parameter_cc_menu(self,parameter,control):
        from effect_control import AUDIO_FX_PARAMETERS,audio_fx_cc_bindings
        from channel_audio_effects import channel_fx_cc_allowed
        row=self.selection.currentRow()
        if not 0<=row<len(self.slots):return None
        slot=self.slots[row];spec=AUDIO_FX_PARAMETERS[slot.kind][parameter]
        origin_binding=self.effect_runtime
        source=audio_fx_cc_bindings(slot).get(spec.key)
        menu=NativeMenu(self)
        assign=menu.addAction(tr('channel_editor.assign_cc'))
        learn=menu.addAction(tr('channel_editor.cancel_learn') if source=='learn' else tr('channel_editor.learn_cc'))
        remove=menu.addAction(tr('channel_editor.audio_fx_cc_remove'))
        allowed=channel_fx_cc_allowed(slot,parameter)
        for action in (assign,learn):
            action.setEnabled(allowed)
            if not allowed:action.setToolTip(tr('channel_editor.audio_fx_cc_manual'))
        remove.setEnabled(source is not None)
        def publish(value):
            if self.effect_runtime is not origin_binding:return
            context=self.parameter_context
            binding=origin_binding
            token,accepted=binding.manager.assign_cc(binding.channel,slot.identity,parameter,value)
            if accepted is False:self.owner.append_log(log_text('channel_editor.cc_save_failed'))
            self._channel_cc_pending=token
            self._channel_cc_timer.start()
            context._refresh_channel_audio_effects()
        def choose():
            context=self.parameter_context
            binding=(source,'audio_fx','',False) if type(source) is int else None
            dialog=NativeMidiCcAssignmentDialog(context,'audio_fx',audio_fx_parameter_caption(slot.kind,spec.key),binding,audio_parameter=spec)
            try:
                if dialog.exec()==QDialog.Accepted:publish(dialog.source.currentData())
            finally:dialog.deleteLater()
        assign.triggered.connect(choose)
        learn.triggered.connect(lambda:publish(None if source=='learn' else 'learn'))
        remove.triggered.connect(lambda:publish('clear'))
        return menu

    def _show_parameter_cc(self,parameter,control,source,point):
        menu=self.parameter_cc_menu(parameter,control)
        if menu is None:return
        try:menu.exec(source.mapToGlobal(point))
        finally:menu.deleteLater()

    def _poll_channel_cc(self):
        token=getattr(self,'_channel_cc_pending',None)
        result=self.effect_runtime.poll_audio_fx(token) if token is not None else True
        if result is None:return
        if result is False:self.owner.append_log(log_text('channel_editor.cc_save_failed'))
        self._channel_cc_timer.stop();self.parameter_context._refresh_channel_audio_effects()

    def _install_parameter_cc(self,parameter,control):
        if self.parameter_context is None:return
        if not hasattr(self,'_channel_cc_timer'):
            self._channel_cc_timer=QTimer(self);self._channel_cc_timer.setInterval(16)
            self._channel_cc_timer.timeout.connect(self._poll_channel_cc)
        if isinstance(control,NativeKnobField):
            control.add_cc_identity()
            surfaces=(control.knob,control.title,control.value,control.cc_badge,control.cc_status)
        else:surfaces=(control,*((control._channel_cc_caption,control._channel_cc_status) if hasattr(control,'_channel_cc_caption') else ()))
        for source in surfaces:
            source.setContextMenuPolicy(Qt.CustomContextMenu)
            source.customContextMenuRequested.connect(lambda point,parameter=parameter,control=control,source=source:
                self._show_parameter_cc(parameter,control,source,point))

    def _parameter_edit_context(self):
        index=self.selection.currentRow()
        if not self.channel_presentation:return index
        return (self.effect_runtime.channel,ae.runtime.stream_generation,
            self.slots[index].identity if 0<=index<len(self.slots) else None)

    def refresh(self, selected):
        from effect_control import AUDIO_FX_MAX_SLOTS
        self._blocked = True
        blocked = self.selection.blockSignals(True)
        structural = (self.selection.count() != len(self.slots) or
                      any(self.selection.item(index).data(Qt.UserRole) != slot.identity
                          for index, slot in enumerate(self.slots)))
        if structural: self.selection.clear()
        for index, slot in enumerate(self.slots):
            item = QListWidgetItem() if structural else self.selection.item(index)
            caption = tr("audio_fx.row_name", index=index+1, name=tr(f"audio_fx.kind.{slot.kind}"))
            if item.text() != caption: item.setText(caption)
            if item.data(Qt.UserRole) != slot.identity: item.setData(Qt.UserRole, slot.identity)
            if item.data(Qt.UserRole+1) != slot.enabled: item.setData(Qt.UserRole+1, slot.enabled)
            if structural: self.selection.addItem(item)
        self._refresh_slot_information()
        self.selection.setCurrentRow(selected)
        self.selection.blockSignals(blocked)
        self.add_button.setEnabled(len(self.slots) < AUDIO_FX_MAX_SLOTS)
        valid = 0 <= selected < len(self.slots)
        self.remove_button.setEnabled(valid)
        self.up_button.setEnabled(valid and selected > 0)
        self.down_button.setEnabled(valid and selected < len(self.slots) - 1)
        if self.selection.property("rackBypassed") != (not self.enabled_switch.isChecked()):
            self.selection.setProperty("rackBypassed", not self.enabled_switch.isChecked())
        if structural:
            self.selection.updateGeometry(); self.body.updateGeometry()
        self._blocked = False
        self.select_effect(selected)

    def _refresh_slot_information(self):
        """One model-role writer for draft/commit/rollback and bypass status."""
        for index, slot in enumerate(self.slots):
            item = self.selection.item(index)
            # load_ini may stop an old preparation before rebuilding rows.
            if item is None or item.data(Qt.UserRole) != slot.identity: continue
            state = (tr("audio_fx.preparing") if self._pending_token is not None else
                     tr("audio_fx.inactive") if slot.enabled and not self.enabled_switch.isChecked() else
                     tr("audio_fx.active") if slot.enabled else tr("common.off"))
            accessible = tr("audio_fx.slot", index=index+1,
                            name=tr(f"audio_fx.kind.{slot.kind}"), state=state)
            if item.data(Qt.AccessibleTextRole) != accessible:
                item.setData(Qt.AccessibleTextRole, accessible)
            meaning = self._effect_help(slot.kind)
            if item.data(Qt.AccessibleDescriptionRole) != meaning:
                item.setData(Qt.AccessibleDescriptionRole, meaning)
            if item.toolTip() != meaning: item.setToolTip(meaning)
        self._refresh_selection_information()

    def _refresh_selection_information(self):
        item = self.selection.currentItem()
        description = (tr("audio_fx.selected_description",
            status=item.data(Qt.AccessibleTextRole), meaning=item.data(Qt.AccessibleDescriptionRole))
            if item is not None else tr("audio_fx.no_effects"))
        description = description.replace("\n", " ")
        if self.selection.accessibleDescription() != description:
            install_effect_tooltip(description, self.selection)
            # Standard QAccessibleWidget maps Help to QWidget.whatsThis,
            # unlike our numeric adapter. Keep the standard list/table bridge.
            self.selection.setWhatsThis(description)

    def _processing_sample_rate(self):
        if self._sample_rate is not None:return self._sample_rate()
        rack = ae.runtime.stream_effects.rack
        return rack.sample_rate if rack.stream else self.owner._engine_preferences.sample_rate

    def _filter_ceiling(self):
        return min(16000,int(self._processing_sample_rate()*.49))

    def _editor_height_changed(self, *_):
        self.body.content_changed()
        invalidate_layout_chain(self)
        self._geometry_changed()

    def _effect_help(self,kind):
        return tr(f'audio_fx.help.{kind}')

    def _queue_editor_resize(self):
        if not self._editor_resize_timer.isActive():
            self._editor_resize_timer.start(0)

    def _flush_editor_resize(self):
        if not getattr(self.owner, "_shutdown_started", False): self._editor_height_changed()

    def select_effect(self, index):
        from effect_control import AUDIO_FX_PARAMETERS, AUDIO_FX_ADVANCED
        self._refresh_selection_information()
        if not 0 <= index < len(self.slots):
            self.body.editor.hide()
            self._editor_geometry_key = None
            self.stack.setCurrentWidget(self.empty)
            self._editor_height_changed()
            return
        self.body.editor.show()
        slot = self.slots[index]
        geometry_key = (slot.kind, slot.asset if slot.kind == 'convolution' else '')
        geometry_changed = geometry_key != getattr(self, '_editor_geometry_key', None)
        if slot.kind not in self._forms:
            page = QWidget(); layout = set_layout(QVBoxLayout(page), 0, CARD_ROW_GAPS["effects"])
            description = ElasticLabel(tr(f"audio_fx.help.{slot.kind}")); description.setWordWrap(True)
            description.setObjectName("AudioFXDescription")
            description.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            layout.addWidget(description)
            if slot.kind in ("distortion", "distortion_dx8", "auto_gain", "convolution", "pitch_shift", "headphone_stage", "saturator"):
                warning = ElasticLabel(tr(f"audio_fx.notice.{slot.kind}")); warning.setWordWrap(True)
                warning.setObjectName("AudioFXNotice"); layout.addWidget(warning)
            if slot.kind == "convolution":
                self.ir_button = make_action_button(tr("audio_fx.ir_choose"), self.choose_impulse)
                layout.addWidget(self.ir_button)
            if slot.kind in FX_VISUAL_FAMILIES:
                visual=NativeFXVisual(page)
                note=ElasticLabel('');note.setWordWrap(True);note.setObjectName('MutedLabel')
                visual.parametersEdited.connect(lambda changes,visual=visual:self._visual_edit(visual,changes))
                visual.parameterContextRequested.connect(lambda key,point,visual=visual:self._visual_cc_menu(visual,key,point))
                layout.addWidget(visual);layout.addWidget(note)
                self._visuals[slot.kind]=(visual,note)
            controls = []
            advanced = AUDIO_FX_ADVANCED.get(slot.kind, ())
            advanced_section=None
            if advanced:
                advanced_section,_grid=make_control_section(tr('audio_fx.advanced'),collapsible=True)
            disclosure=advanced_section.disclosure if advanced_section is not None else None
            actions = QWidget(); action_layout = set_layout(QHBoxLayout(actions), 0, 6)
            reset = make_action_button(tr("common.restore_defaults"), self.restore_defaults)
            install_effect_tooltip(tr('channel_editor.audio_fx_values_reset' if self.channel_presentation else "audio_fx.reset_tip"), reset)
            action_layout.addWidget(reset); action_layout.addStretch(1)
            from native_widgets import make_effect_audition
            if self.channel_presentation:
                preview=make_instrument_audition(self.owner,lambda:None if self.parameter_context._global_scope()
                    else self.owner.audition_request(channel=self.parameter_context.selected_channel()),actions)
            else:preview=make_effect_audition(self.owner,lambda:self.slots[self.selection.currentRow()].kind
                if 0<=self.selection.currentRow()<len(self.slots) else 'output',actions)
            action_layout.insertWidget(1,preview)
            preparing = ElasticLabel(tr("audio_fx.preparing"))
            preparing.setMinimumWidth(0); preparing.setVisible(self._pending_token is not None)
            action_layout.addWidget(preparing)
            self._preparing_labels[slot.kind] = preparing
            if disclosure is not None:
                toggle = advanced_section.disclosure_button
                toggle.setChecked(False)
                disclosure.heightChanged.connect(self._editor_height_changed)
                disclosure.expansionChanged.connect(self._window_fit)
                disclosure.set_expanded(False)
                self._advanced[slot.kind] = toggle, disclosure
            if self.channel_presentation:
                main_fields=WidthMeasuredPanel();layout.addWidget(main_fields)
                parameter_layout=WrappingControlLayout(main_fields)
                advanced_layout=None
                if disclosure is not None:
                    advanced_fields=WidthMeasuredPanel();disclosure.content_layout.addWidget(advanced_fields)
                    advanced_layout=WrappingControlLayout(advanced_fields)
            for number, parameter in enumerate(AUDIO_FX_PARAMETERS[slot.kind]):
                caption = audio_fx_parameter_caption(slot.kind, parameter.key)
                tooltip = audio_fx_parameter_tooltip(slot.kind, parameter.key)
                row_layout = ((advanced_layout if number in advanced else parameter_layout)
                              if self.channel_presentation else disclosure.content_layout if number in advanced else layout)
                if parameter.choices:
                    combo = AnimatedComboBox()
                    combo.setAccessibleName(caption)
                    for choice in parameter.choices:
                        combo.addItem(tr("audio_fx.value", value=choice.replace("-", "\u2212"), unit=parameter.unit)
                            if parameter.key == "phase" else choice if choice.isdecimal() else tr(f"audio_fx.choice.{choice}"))
                    combo.currentIndexChanged.connect(lambda value, number=number, low=parameter.minimum, step=parameter.step:
                        self.set_parameter(number, low + value * step))
                    if self.channel_presentation:
                        row=ElasticPanel(preferred_width=140)
                        choice_layout=set_layout(QVBoxLayout(row),0,4)
                        label=ElasticLabel(caption);label.setAlignment(Qt.AlignCenter)
                        label.setObjectName('FieldLabel')
                        status=ElasticLabel('');status.setObjectName('MutedLabel');status.setAlignment(Qt.AlignCenter)
                        combo._channel_cc_caption=label;combo._channel_cc_status=status
                        choice_layout.addWidget(label);choice_layout.addWidget(combo);choice_layout.addWidget(status);choice_layout.addStretch(1)
                    else:
                        row = QWidget(); install_three_column_form_row(row, caption, combo, ElasticPanel(),
                            label_width=METRICS["form_label_width"], gap=METRICS["form_column_gap"])
                    install_effect_tooltip(tooltip, row, combo)
                    row_layout.addWidget(row); controls.append(combo)
                    self._install_parameter_cc(number,combo)
                else:
                    decimals = max(0, -round(math.log10(parameter.step)))
                    def formatter(value, parameter=parameter):return audio_fx_parameter_value(parameter,value)
                    if self.channel_presentation:
                        control=NativeKnobField(caption,parameter.minimum,parameter.maximum,parameter.initial,
                            lambda value,number=number:self.set_parameter(number,value),formatter,
                            tooltip=tooltip,preferred_width=140,editable=True,entry_domain=parameter.entry_domain,
                            value_scale=10**decimals,value_step=parameter.step,
                            edit_context=self._parameter_edit_context)
                        control.set_theme(self.owner._dark)
                    else:
                        control = NativeControlRow(caption, parameter.minimum, parameter.maximum, parameter.initial,
                            lambda value, number=number: self.set_parameter(number, value), formatter,
                            step=parameter.step, input_steps=parameter.input_steps, snap_navigation=True,
                            logarithmic=slot.kind in RACK_BQF_KINDS and parameter.key == "frequency",
                            neutral_value=parameter.initial,
                            entry_kind=parameter.entry_kind,
                            entry_context=self._parameter_edit_context, tooltip=tooltip)
                    row_layout.addWidget(control); controls.append(control)
                    self._install_parameter_cc(number,control)
            layout.addWidget(actions)
            if disclosure is not None: layout.addWidget(advanced_section)
            self._forms[slot.kind] = page, controls
            self.stack.addWidget(page)
            apply_native_text_metrics(page)
            # Caption widths are preferred metrics, never horizontal floors.
            # Use the widest reviewed caption across the shared inspector so
            # selecting another effect does not shift its slider grid. The
            # actual project font (after native metrics) owns this measurement.
            titles=[] if self.channel_presentation else [control.title if isinstance(control,NativeControlRow) else
                next(label for label in control.parentWidget().findChildren(QLabel)
                     if label.objectName()=='FieldLabel') for control in controls]
            for title in titles:
                metrics=QFontMetrics(title.font())
                preferred=max(METRICS['form_label_width'],4+max(
                    metrics.horizontalAdvance(audio_fx_parameter_caption(kind,p.key))
                    for kind,parameters in AUDIO_FX_PARAMETERS.items() for p in parameters))
                title.set_preferred_width(preferred)
        page, controls = self._forms[slot.kind]
        if slot.kind in self._visuals:
            visual,note=self._visuals[slot.kind]
            rate=self._processing_sample_rate()
            visual.set_configuration(slot.kind,dict(zip((p.key for p in AUDIO_FX_PARAMETERS[slot.kind]),slot.values)),
                rate,(id(self.effect_runtime),slot.identity),self.isEnabled())
            note.setText(visual.plot.note)
        from effect_control import audio_fx_cc_bindings
        bindings=audio_fx_cc_bindings(slot)
        self.preparing_hint = self._preparing_labels[slot.kind]
        self._blocked = True
        try:
            for parameter, control, value in zip(AUDIO_FX_PARAMETERS[slot.kind], controls, slot.values):
                source=bindings.get(parameter.key)
                if self.channel_presentation and isinstance(control,NativeKnobField):
                    set_channel_protocol_caption(control,f'CC#{source:03d}' if type(source) is int else '')
                    control.cc_status.set_text(tr('channel_editor.audio_fx_cc_learning_badge') if source=='learn' else '')
                    control.set_theme(self.owner._dark)
                    tooltip=audio_fx_parameter_tooltip(slot.kind,parameter.key)
                    if source is not None:tooltip=tr('channel_editor.audio_fx_cc_status',description=tooltip,
                        source=tr('channel_editor.learning_status') if source=='learn' else manual_cc_display(source))
                    install_effect_tooltip(tooltip,control,control.title,control.knob,control.value,control.cc_badge)
                elif self.channel_presentation:
                    control._channel_cc_status.setVisible(source is not None)
                    control._channel_cc_status.setText(tr('channel_editor.audio_fx_cc_learning_badge') if source=='learn'
                        else f'CC#{source:03d}' if type(source) is int else '')
                    tooltip=audio_fx_parameter_tooltip(slot.kind,parameter.key)
                    if source is not None:tooltip=tr('channel_editor.audio_fx_cc_status',description=tooltip,
                        source=tr('channel_editor.learning_status') if source=='learn' else manual_cc_display(source))
                    install_effect_tooltip(tooltip,control,control._channel_cc_caption,control._channel_cc_status)
                if isinstance(control, (NativeControlRow,NativeKnobField)):
                    if isinstance(control,NativeControlRow) and slot.kind in RACK_BQF_KINDS and control is controls[0]:
                        control.set_maximum(self._filter_ceiling())
                    control.set(value, False)
                else:
                    blocked = control.blockSignals(True)
                    control.setCurrentIndex(round((value - parameter.minimum) / parameter.step))
                    control.blockSignals(blocked)
        finally:
            self._blocked = False
        self.stack.setCurrentWidget(page)
        if slot.kind == "convolution":
            self.ir_button.setText(Path(slot.asset).name if slot.asset else tr("audio_fx.ir_choose"))
            self.ir_button.setToolTip(slot.asset)
        self._editor_geometry_key = geometry_key
        if geometry_changed:
            self._editor_height_changed()
            # The just-mounted page's wrapped labels become measurable after
            # Qt assigns their actual width. Retire old-page HFW caches once.
            self._queue_editor_resize()
            if self.isVisible():self._window_fit()


# MIDI Association MIDI 1.0 Control Change table. The key is a stable locale
# identity, not a claim that BASSMIDI applies the named controller by default.
MIDI_CC_NAME_KEYS = {
    0: "bank_select", 1: "modulation", 2: "breath", 4: "foot",
    5: "portamento_time", 6: "data_entry_msb", 7: "channel_volume",
    8: "balance", 10: "pan", 11: "expression", 12: "effect_control_1",
    13: "effect_control_2", 16: "general_purpose_1",
    17: "general_purpose_2", 18: "general_purpose_3",
    19: "general_purpose_4", 64: "sustain", 65: "portamento_switch",
    66: "sostenuto", 67: "soft_pedal", 68: "legato_footswitch",
    69: "hold_2", 70: "sound_variation", 71: "timbre",
    72: "release_time", 73: "attack_time", 74: "brightness",
    75: "decay_time", 76: "vibrato_rate", 77: "vibrato_depth",
    78: "vibrato_delay", 79: "sound_controller_10",
    80: "general_purpose_5", 81: "general_purpose_6",
    82: "general_purpose_7", 83: "general_purpose_8",
    84: "portamento_control", 88: "velocity_prefix",
    91: "reverb_send", 92: "tremolo_depth", 93: "chorus_send",
    94: "celeste_depth", 95: "phaser_depth",
    96: "data_increment", 97: "data_decrement", 98: "nrpn_lsb",
    99: "nrpn_msb", 100: "rpn_lsb", 101: "rpn_msb",
}


def manual_cc_display(number):
    """One named, numeric-order choice for each assignable CC 0..119."""
    if not 0 <= number <= 119:
        raise ValueError(number)
    if 32 <= number <= 63:
        base = number - 32
        base_key = "data_entry" if base == 6 else MIDI_CC_NAME_KEYS.get(base)
        name = tr(f"midi_cc.{base_key}") if base_key else tr("midi_cc.undefined")
        name = tr("midi_cc.lsb_for", cc=base, name=name)
    else:
        key = MIDI_CC_NAME_KEYS.get(number)
        name = tr(f"midi_cc.{key}") if key else tr("midi_cc.undefined")
    return tr("midi_cc.choice", number=number, name=name)


def channel_native_input_description(target, channel):
    """Protocol metadata is shared with badges; explain native input, not FX names."""
    native_channel = channel
    channel %= 16
    status = f"{0xB0 | channel:02X}"
    if target == "pitch":
        return tr("channel_editor.native_pitch", status=f"{0xE0 | channel:02X}")
    if target == "channel_pressure":
        return tr("channel_editor.native_channel_pressure", status=f"{0xD0 | channel:02X}")
    if target == "key_pressure":
        return tr("channel_editor.native_key_pressure", status=f"{0xA0 | channel:02X}",
                  key=ae.runtime.channel_policy.pressure_key(native_channel))
    if target in CHANNEL_RPN_DEFAULTS:
        rpn = CHANNEL_RPN_DEFAULTS[target]
        return tr("channel_editor.native_rpn", number=rpn, status=status, selector=f"{rpn:02X}",
                  data=tr("channel_editor.native_rpn_fine_data" if rpn == 1 else
                          "channel_editor.native_rpn_mod_data" if rpn == 5 else
                          "channel_editor.native_rpn_coarse_data" if rpn == 2 else
                          "channel_editor.native_rpn_range_data", status=status))
    if target in CHANNEL_SYSEX_ROUTES:
        subtype, source, parameter = CHANNEL_SYSEX_ROUTES[target]
        packet = f"F0 7F 7F 09 {subtype:02X} {channel:02X} "
        if source is not None:
            packet += f"{source:02X} "
        packet += f"{parameter:02X} RR F7"
        performance = (f"{0xD0 | channel:02X} pp" if subtype == 1 else
                       f"{0xA0 | channel:02X} kk pp" if subtype == 2 else f"{status} 01 vv")
        return tr("channel_editor.native_sysex", packet=packet, performance=performance)
    if target == "delay_send" and not ae.runtime.midi_options.cc94_custom_delay_send:
        return tr("channel_editor.native_delay_disabled")
    number = CHANNEL_CC_DEFAULTS.get(target)
    if number is not None:
        return tr("channel_editor.native_cc", source=manual_cc_display(number) if number < 120 else f"CC#{number:03d}",
                  packet=f"{status} {number:02X} vv",
                  meaning=tr("channel_editor.native_mode_command" if number >= 120 else
                             "channel_editor.native_cc_data"))
    return tr("channel_editor.native_no_midi")


class NativeChannelMappingDialog(NativeDialog):
    """Shared assignment-form sizing; no layout-derived horizontal floor."""
    def minimumSizeHint(self):
        # Windows honors QDialog's layout-derived WM_GETMINMAXINFO hint even
        # with minimumSize=0/SetNoConstraint. Preferred width is not a floor.
        return QSize(0, 0)


class NativeChannelScopeChoice(NativeSegmentedChoice):
    """Global-first scope with semantic accessors independent of row order."""

    def __init__(self, parent=None, *, all_channels=False):
        super().__init__((tr('channel_editor.cc_all_channels'),
                          tr('channel_editor.cc_this_channel')), parent)
        self.setAccessibleName(tr('channel_editor.cc_scope'))
        self.set_all_channels(all_channels)

    def set_all_channels(self, enabled):
        self.set_selected(0 if enabled else 1)

    def all_channels(self):
        return self.selected_index() == 0


class NativeMidiCcAssignmentDialog(NativeChannelMappingDialog):
    """One CC/scope chooser, with an audible destination for auxiliary slots."""

    def __init__(self, parent, target, label, binding, *, learning=False, audio_parameter=None):
        if target == "mode":
            raise ValueError("Voice mode uses native channel-mode messages")
        super().__init__(parent)
        self._learning = learning
        self._auxiliary = target.startswith("aux_")
        self.setWindowTitle(tr("channel_editor.assign_cc_title"))
        self.setWindowIcon(parent.windowIcon())
        self.setMinimumSize(0, 0)
        root = set_layout(QVBoxLayout(self), METRICS["outer"], METRICS["section_gap"])
        root.setSizeConstraint(QLayout.SetNoConstraint)
        heading = QLabel(label)
        heading.setWordWrap(True)
        root.addWidget(heading)
        channel = parent.selected_channels()[0]
        root.addWidget(QLabel(tr("channel_editor.native_input_all_heading") if parent._global_scope() else
                             tr("channel_editor.native_input_heading", channel=channel + 1)))
        self.native_input = QLabel(tr('channel_editor.audio_fx_cc_native') if audio_parameter is not None else channel_native_input_description(target, channel))
        if target == "key_pressure" and parent._global_scope():
            keys = ", ".join(f"P{ch // 16}/CH{ch % 16 + 1:02d}:{ae.runtime.channel_policy.pressure_key(ch):03d}" for ch in parent.selected_channels())
            self.native_input.setText(tr("channel_editor.native_key_all_details", description=self.native_input.text(), keys=keys))
        self.native_input.setWordWrap(True)
        self.native_input.setMinimumWidth(0)
        self.native_input.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.native_input.setObjectName("MutedLabel")
        root.addWidget(self.native_input)
        self.source = AnimatedComboBox(self)
        for number in range(120):
            self.source.addItem(manual_cc_display(number), number)
        native_source = CHANNEL_CC_DEFAULTS.get(target)
        if target == "delay_send" and not ae.runtime.midi_options.cc94_custom_delay_send:
            native_source = None
        initial = binding[0] if binding else native_source
        self.source.setPlaceholderText(tr("channel_editor.cc_unassigned_placeholder"))
        self.source.setCurrentIndex(self.source.findData(initial))
        self.source.setAccessibleName(tr("channel_editor.cc_source"))
        bindings = [binding] if audio_parameter is not None else [ae.runtime.midi_learn.binding(ch, target) for ch in parent.selected_channels()]
        mixed_binding = len(set(bindings)) > 1
        if mixed_binding:
            self.source.setCurrentIndex(-1)
        if not learning:
            root.addWidget(QLabel(tr("channel_editor.cc_source")))
            root.addWidget(self.source)
            self.binding_status = NativeOptionalLabel()
            self.binding_status.setWordWrap(True)
            if mixed_binding:
                self.binding_status.setText(tr("channel_editor.cc_mixed_binding"))
            self.binding_status.setObjectName("MutedLabel")
            root.addWidget(self.binding_status)
        else:
            self.source.hide()
        self.destination = AnimatedComboBox(self)
        self.destination.setAccessibleName(tr("channel_editor.cc_destination"))
        if target.startswith("aux_"):
            root.addWidget(QLabel(tr("channel_editor.cc_destination")))
            for destination in AUXILIARY_CC_DESTINATIONS:
                self.destination.addItem(parent._channel_parameter_label(destination), destination)
            if binding:
                self.destination.setCurrentIndex(self.destination.findData(binding[1]))
            else:
                self.destination.setPlaceholderText(tr("channel_editor.cc_destination_unassigned"))
                self.destination.setCurrentIndex(-1)
            root.addWidget(self.destination)
            if mixed_binding:
                self.destination.setCurrentIndex(-1)
        else:
            self.destination.hide()
        self.precision_hint = QLabel()
        self.precision_hint.setWordWrap(True)
        self.precision_hint.setMinimumWidth(0)
        self.precision_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.precision_hint.setObjectName("MutedLabel")
        root.addWidget(self.precision_hint)
        def update_precision(_index=None):
            selected = self.destination.currentData() if target.startswith("aux_") else target
            self.precision_hint.setVisible(selected is not None)
            if selected is None:
                self.precision_hint.clear()
            else:
                if audio_parameter is not None:
                    from effect_control import audio_fx_cc_value
                    positions=len({audio_fx_cc_value(audio_parameter,value) for value in range(128)})
                else:positions = len({channel_cc_target_value(selected, value) for value in range(128)})
                self.precision_hint.setText(tr('channel_editor.audio_fx_cc_precision' if audio_parameter is not None
                    else "channel_editor.cc_precision_hint", positions=positions))
            if self.isVisible():
                invalidate_layout_chain(self.precision_hint)
                fit_plain_dialog_contents(self,grow_only=True)
        self.destination.currentIndexChanged.connect(update_precision)
        update_precision()
        self.scope = NativeChannelScopeChoice(all_channels=bool(binding and binding[3]))
        if parent._global_scope():
            self.scope.set_all_channels(True)
            self.scope.setEnabled(False)
        if audio_parameter is None:
            root.addWidget(QLabel(tr("channel_editor.cc_scope")))
            root.addWidget(self.scope)
        else:self.scope.hide()
        footer = set_layout(QHBoxLayout(), 0, METRICS["form_column_gap"])
        footer.addStretch(1)
        self.confirm = make_action_button(tr("channel_editor.learn_cc") if learning else
                                         tr("channel_editor.assign_cc_button"), self.accept, primary=True)
        self.confirm.setDefault(True)
        self.source.currentIndexChanged.connect(self._update_confirmation)
        self.destination.currentIndexChanged.connect(self._update_confirmation)
        self._update_confirmation()
        footer.addWidget(self.confirm)
        footer.addWidget(make_action_button(tr("common.cancel"), self.reject))
        root.addLayout(footer)
        apply_native_text_metrics(self)
        self.ensurePolished()
        self.resize(440, root.totalHeightForWidth(440))

    def _update_confirmation(self, _index=None):
        self.confirm.setEnabled(self._valid_selection())

    def _valid_selection(self):
        return ((self._learning or self.source.currentData() is not None) and
                (not self._auxiliary or self.destination.currentData() is not None))

    def accept(self):
        # Enter and programmatic acceptance must obey the same validation as
        # the button; an empty selection is not CC119 or CC0.
        if self._valid_selection():
            super().accept()


class NativeChannelResetDefaultsDialog(NativeDialog):
    """A non-mutating, per-parameter diff; Save is the only policy write."""

    def __init__(self, parent):
        super().__init__(parent)
        self.channels = parent.selected_channels()
        self.setWindowTitle(tr("channel_editor.reset_defaults"))
        self.setWindowIcon(parent.windowIcon())
        self.setMinimumSize(0, 0)
        root = set_layout(QVBoxLayout(self), METRICS["outer"], METRICS["section_gap"])
        root.setSizeConstraint(QLayout.SetNoConstraint)
        description = QLabel(tr("channel_editor.reset_defaults_description"))
        description.setWordWrap(True)
        root.addWidget(description)
        self.table = NativeInstrumentTable(0, 6)
        self.table.setAccessibleName(tr("channel_editor.reset_defaults"))
        self.table.setMinimumSize(0, 0)
        self.table.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.table.setHorizontalHeaderLabels([tr(f"channel_editor.reset_column_{key}")
            for key in ("use", "channel", "parameter", "factory", "current", "saved")])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setTabKeyNavigation(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setFont(INSTRUMENT_TABLE.font())
        self.table.setHorizontalHeader(NativeInstrumentHeader(lambda: parent.owner._dark, self.table))
        self.table.setItemDelegate(NativeSettingsRowDelegate(lambda: parent.owner._dark, self.table, self.table))
        self.table.verticalHeader().setDefaultSectionSize(INSTRUMENT_TABLE.row_height)
        self.table.verticalHeader().setMinimumSectionSize(INSTRUMENT_TABLE.row_height)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setMinimumSectionSize(0)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        for column, width in enumerate((44, 54, 180, 110, 110, 170)):
            self.table.setColumnWidth(column, width)
        self.records = []
        with ae.runtime.channel_policy.lock:
            saved = dict(ae.runtime.channel_policy.reset_defaults)
        monitor = ae.channel_monitor_snapshot()
        for channel in self.channels:
            factory = ae.channel_factory_reset_values(channel)
            for field, baseline in factory.items():
                readback = None if field in ("bank", "bank_lsb", "program") else ae.channel_control_readback(channel, field)
                current = (monitor[channel][field] if readback is None else
                           readback.value if readback.known else None)
                key = channel, field
                row = self.table.rowCount()
                self.table.insertRow(row)
                checked = QTableWidgetItem()
                checked.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                if current is None and key not in saved:
                    checked.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    checked.setToolTip(tr("channel_editor.unknown_hint"))
                checked.setCheckState(Qt.Checked if key in saved else Qt.Unchecked)
                self.table.setItem(row, 0, checked)
                label = parent._channel_parameter_label(field, channel)
                for column, text in ((1, f"{channel + 1:02d}"), (2, label),
                    (3, parent._format_channel_parameter(field, baseline)),
                    (4, parent._format_channel_parameter(field, current))):
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                    item.setToolTip(text)
                    if current is not None and current != baseline:
                        item.setForeground(QColor(widget_palette(parent.owner).accent))
                    self.table.setItem(row, column, item)
                choice = AnimatedComboBox(self.table)
                choice.setAccessibleName(label)
                choice.setObjectName('NativeTableChoice')
                choice.setFixedHeight(INSTRUMENT_TABLE.row_height)
                if current is not None:
                    choice.addItem(tr("channel_editor.reset_use_current",
                        value=parent._format_channel_parameter(field, current)), current)
                if key in saved:
                    choice.addItem(tr("channel_editor.reset_keep_saved",
                        value=parent._format_channel_parameter(field, saved[key])), saved[key])
                    choice.setCurrentIndex(choice.count()-1)
                choice.setEnabled(key in saved)
                choice.setMinimumWidth(0)
                self.table.setCellWidget(row, 5, choice)
                self.records.append((key, choice))
        from channel_audio_effects import channel_fx_reset_defaults
        from effect_control import AUDIO_FX_PARAMETERS
        for channel in self.channels:
            slots,_enabled=ae.runtime.stream_effects.channel_fx.snapshot(channel)
            for position,slot in enumerate(slots):
                defaults=channel_fx_reset_defaults(slot)
                for parameter,value in zip(AUDIO_FX_PARAMETERS[slot.kind],slot.values):
                    key=(channel,slot.identity,parameter.key);row=self.table.rowCount();self.table.insertRow(row)
                    checked=QTableWidgetItem();checked.setFlags(Qt.ItemIsEnabled|Qt.ItemIsSelectable|Qt.ItemIsUserCheckable)
                    checked.setCheckState(Qt.Checked if parameter.key in defaults else Qt.Unchecked)
                    self.table.setItem(row,0,checked)
                    label=tr('channel_editor.reset_effect_parameter',position=position+1,
                        effect=tr('audio_fx.kind.'+slot.kind),parameter=audio_fx_parameter_caption(slot.kind,parameter.key))
                    for column,text in ((1,f'{channel+1:02d}'),(2,label),
                            (3,audio_fx_parameter_value(parameter,parameter.initial)),(4,audio_fx_parameter_value(parameter,value))):
                        item=QTableWidgetItem(text);item.setTextAlignment(Qt.AlignLeft|Qt.AlignVCenter)
                        if value!=parameter.initial:item.setForeground(QColor(widget_palette(parent.owner).accent))
                        self.table.setItem(row,column,item)
                    choice=AnimatedComboBox(self.table);choice.setAccessibleName(label)
                    choice.setObjectName('NativeTableChoice');choice.setFixedHeight(INSTRUMENT_TABLE.row_height)
                    choice.setMinimumWidth(0)
                    choice.addItem(tr('channel_editor.reset_use_current',value=audio_fx_parameter_value(parameter,value)),value)
                    if parameter.key in defaults:
                        choice.addItem(tr('channel_editor.reset_keep_saved',value=audio_fx_parameter_value(parameter,defaults[parameter.key])),defaults[parameter.key])
                        choice.setCurrentIndex(choice.count()-1)
                    choice.setEnabled(parameter.key in defaults)
                    self.table.setCellWidget(row,5,choice);self.records.append((key,choice))
        self.table.itemChanged.connect(lambda item: self.records[item.row()][1].setEnabled(
            item.checkState() == Qt.Checked) if item.column() == 0 else None)
        root.addWidget(self.table, 1)
        footer = set_layout(QHBoxLayout(), 0, METRICS["form_column_gap"])
        footer.addStretch(1)
        self.save_button = make_action_button(tr("common.save"), self.accept, primary=True)
        self.save_button.setDefault(True)
        footer.addWidget(self.save_button)
        footer.addWidget(make_action_button(tr("common.cancel"), self.reject))
        root.addLayout(footer)
        apply_native_text_metrics(self)
        # Table options must fit the actual translated current/saved values
        # at rest; the Interactive header still lets users reduce this width.
        saved_width=max((choice.sizeHint().width() for _key,choice in self.records),default=0)
        self.table.setColumnWidth(5,max(self.table.columnWidth(5),saved_width))
        self.resize(840, 560)

    def selected_values(self):
        return {key: choice.currentData() for row, (key, choice) in enumerate(self.records)
                if len(key)==2 and self.table.item(row, 0).checkState() == Qt.Checked and choice.currentData() is not None}

    def selected_effect_values(self):
        return {key:choice.currentData() for row,(key,choice) in enumerate(self.records)
            if len(key)==3 and self.table.item(row,0).checkState()==Qt.Checked and choice.currentData() is not None}

    def accept(self):
        from channel_audio_effects import ChannelAudioEffects
        values=self.selected_effect_values()
        try:
            for channel in self.channels:
                for slot in ae.runtime.stream_effects.channel_fx.snapshot(channel)[0]:
                    ChannelAudioEffects.with_reset_defaults(slot,{key:value for (ch,identity,key),value in values.items()
                        if ch==channel and identity==slot.identity})
        except ValueError:
            NativeMessageBox.warning(self,self.windowTitle(),tr('channel_editor.reset_effect_invalid'));return
        super().accept()




class GroupPerformanceEditor:
    """Document draft adapter: missing instrument keys inherit the group.

    No global INI setters, snapshot transactions, or audio handles in widgets.
    The group dialog already owns apply/save/discard and its rollback model.
    """
    def __init__(self, dialog):
        self.dialog, self.key = dialog, None

    def base(self):
        profile = self.dialog.group.performance
        return dict(profile or {})

    def values(self):
        override = self.dialog.group.preset_map().get(self.key)
        return {**self.base(), **(override.performance if override and not override.disabled else {})}

    @property
    def scale_cents(self):
        values = self.values()
        return [values.get(f"scale_{index}", 0) for index in range(12)]

    def drum_mix(self, key):
        from midi_input_runtime import DrumKeyMix, DRUM_NEUTRAL
        values = self.values()
        return DrumKeyMix(**{field: values.get(f"drum_{key}_{field}", getattr(DRUM_NEUTRAL, field))
                             for field in DRUM_PARAMETER_ORDER})

    def _target(self):
        group = self.dialog.group
        if group.performance is None:
            group.performance = self.base()
        if self.key is None:
            return group.performance
        override = group.preset_map().get(self.key)
        if override is None:
            override = PresetOverride(*self.key)
            group.presets.append(override)
        return override.performance

    def _set(self, key, amount, default):
        target = self._target()
        inherited = self.base().get(key, default) if self.key is not None else default
        if amount == inherited:
            target.pop(key, None)
        else:
            target[key] = int(amount)
        self.dialog._apply_live("performance")

    def set_scale(self, pitch, value):
        self._set(f"scale_{pitch}", SCALE_PARAMETER.clamp(value), SCALE_PARAMETER.default)

    def set_drum(self, key, field, value):
        parameter = DRUM_PARAMETERS[field]
        self._set(f"drum_{key}_{field}", parameter.clamp(value), parameter.default)

    def _reset(self, prefix):
        target = self._target()
        for key in tuple(target):
            if key.startswith(prefix):
                target.pop(key)
        self.dialog._apply_live("performance")

    def reset_scale(self): self._reset("scale_")
    def reset_drums(self): self._reset("drum_")
    def has_scale_overrides(self): return any(key.startswith("scale_") for key in self._saved())
    def has_drum_overrides(self): return any(key.startswith("drum_") for key in self._saved())
    def _saved(self):
        if self.key is None:
            return self.base()
        override = self.dialog.group.preset_map().get(self.key)
        return override.performance if override else {}


class NativePerformanceDialog(NativeDialog):
    """Immediate channel editor; scale/drum profiles belong to the SFG editor."""

    NORMAL_CHANNEL_WIDTH = 900  # Smallest width retaining the normal3-column form, never a floor.
    SECTION_UI_KEYS = {
        "channel_pressure_routes": "channel_editor_channel_pressure_expanded",
        "key_pressure_routes": "channel_editor_poly_pressure_expanded",
        # Retain the old combined group's saved visibility for its secondary
        # SoundFont-defined controls; conventional Additional CC stays visible.
        "soundfont_cc": "channel_editor_additional_cc_expanded",
        "modulation_routes": "channel_editor_modulation_expanded",
        "audio_effects": "channel_editor_audio_effects_expanded",
    }

    @classmethod
    def section_ui_defaults(cls):
        return {stored:'0' if key=='audio_effects' else '1' for key,stored in cls.SECTION_UI_KEYS.items()}

    def __init__(self, owner, performance, commit):
        super().__init__(owner)
        self.owner, self.performance, self.commit = owner, performance, commit
        self.setModal(False)
        self.setWindowTitle(tr("channel_editor.title", number=1))
        self.setWindowIcon(owner.windowIcon())
        self.setFont(owner.font())
        self.resize(640, 610)
        self._channel_window_motion = NativeWindowSizeMotion(self)
        self._channel_window_motion.finished.connect(self._finish_section_transition)
        self._section_transitioning = False
        self._section_transition_setup = False
        self._fit_channel_on_show = False
        self.setMinimumSize(0, 0)
        root = set_layout(QVBoxLayout(self), METRICS["outer"], METRICS["section_gap"])
        root.setSizeConstraint(QLayout.SetNoConstraint)
        self.content_stack = QStackedWidget()
        self.content_stack.setMinimumSize(0, 0)
        self.content_stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        root.addWidget(self.content_stack, 1)
        expression_page, expression_layout = self._page(
            "performance.expression_tab", scrollable=True, in_tabs=False,
            plain=True,
        )
        self.expression_page = expression_page
        self.content_stack.addWidget(expression_page)
        self._build_expression_editor(expression_layout)
        self._build_channel_audio_effects()
        self.expression_channel.currentIndexChanged.connect(self._refresh_expression)
        self.expression_channel.currentIndexChanged.connect(
            lambda _index: self._refresh_selected_channel())
        self.channel_footer = QWidget()
        channel_buttons = set_layout(QHBoxLayout(self.channel_footer), 0,
                                     METRICS["form_column_gap"])
        channel_buttons.addStretch(1)
        self.cc_reset_channel = make_action_button(
            tr("channel_editor.cc_reset_channel"), lambda: self._reset_cc(False))
        self.cc_reset_all = make_action_button(
            tr("channel_editor.cc_reset_all"), lambda: self._reset_cc(True))
        self.reset_defaults_button = make_action_button(
            tr("channel_editor.reset_defaults"), self._edit_reset_defaults)
        self.reset_defaults_button.setToolTip(tr("channel_editor.reset_defaults_hint"))
        channel_buttons.addWidget(self.reset_defaults_button)
        for button in (self.cc_reset_channel, self.cc_reset_all):
            button.setToolTip(tr("channel_editor.cc_reset_hint"))
            channel_buttons.addWidget(button)
        self.channel_close_button = make_action_button(
            tr("channel_editor.close"), self.close, primary=True)
        channel_buttons.addWidget(self.channel_close_button)
        root.addWidget(self.channel_footer)
        self.channel_footer.hide()
        self.refresh_controls()
        apply_native_text_metrics(self)

    def _build_expression_editor(self, expression_layout):
        self._cc_fields = {}
        expression_layout.setContentsMargins(8, 8, 8, 0)
        page_layout = self.expression_page.layout()
        page_margins = page_layout.contentsMargins()
        page_layout.setContentsMargins(page_margins.left(), page_margins.top(), page_margins.right(), 0)
        # The root's section_gap separates this content from its footer.
        # Generic tab-page bottom padding must not compound that same gap.
        expression_layout.setSpacing(6)
        self._instrument_catalog_key = None
        self._instrument_choices = {}
        header = NativeCard(compact=True,inset_role='compact-form')
        self.channel_header = header
        header_layout = set_layout(QGridLayout(header), 6, 4)
        self.expression_channel = AnimatedComboBox()
        self.expression_channel.set_category_provider(self._channel_port_category)
        self._sync_channel_addresses()
        self.instrument_bank = AnimatedComboBox()
        self.instrument_bank_lsb = AnimatedComboBox()
        self.instrument_bank_lsb.addItems(tuple(f"{number:03d}" for number in range(128)))
        self.instrument_bank_lsb.currentIndexChanged.connect(self._on_bank_lsb_selected)
        self.instrument_program = AnimatedComboBox()
        self.instrument_program.set_category_provider(self._instrument_category)
        self._channel_header_fields = []
        for column, (caption, control) in enumerate((
                ("channel", self.expression_channel),
                ("bank", self.instrument_bank),
                ("bank_lsb", self.instrument_bank_lsb),
                ("instrument", self.instrument_program))):
            panel=ElasticPanel(preferred_width=80)
            panel.setMinimumWidth(0);panel.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Fixed)
            column_layout=set_layout(QVBoxLayout(panel),0,4)
            label = ElasticLabel(tr(f"channel_editor.{caption}"))
            label.setObjectName("MutedLabel")
            column_layout.addWidget(label)
            control.setMinimumWidth(0)
            control.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            control.setAccessibleName(label.text())
            if caption in ('bank','bank_lsb','instrument'):
                help_key='program' if caption=='instrument' else caption
                install_effect_tooltip(tr(f'channel_editor.{help_key}_tooltip'),label,control)
            column_layout.addWidget(control)
            panel.setFixedHeight(label.fontMetrics().height()+4+control.height())
            header_layout.addWidget(panel, 0, column,2,1)
            self._channel_header_fields.append((panel,label,control))
        install_effect_tooltip(effect_tooltip("expression_channel"),
                               self.expression_channel)
        header_layout.setColumnStretch(3, 1)
        self.expression_reset = make_action_button(
            tr("channel_editor.reset"), lambda: self._reset_scope("expression"))
        install_effect_tooltip(tr("channel_editor.reset_tooltip"), self.expression_reset)
        commands=QWidget();commands_layout=set_layout(QHBoxLayout(commands),0,4)
        self.channel_preview=make_instrument_audition(self.owner,
            lambda:None if self._global_scope() else self.owner.audition_request(channel=self.selected_channel()),commands)
        self.channel_preview.setToolTip(tr('instrument_audition.channel_help'))
        commands_layout.addWidget(self.channel_preview);commands_layout.addWidget(self.expression_reset)
        header_layout.addWidget(commands, 1, 4, Qt.AlignRight)
        self._displayed_program = None
        self.channel_activity_text = ElasticLabel(tr("channel_editor.active_counts", notes=0, voices=tr('channel_editor.unknown_value')))
        self.channel_activity_text.setObjectName("MutedLabel")
        self.channel_activity_text.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        install_effect_tooltip(tr('channel_editor.active_counts_tooltip'),self.channel_activity_text)
        counts=ElasticPanel()
        self.channel_counts=counts
        counts.setMinimumWidth(0);counts.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Fixed)
        count_layout=set_layout(QHBoxLayout(counts),0,METRICS['form_column_gap'])
        self.channel_activity_text.setMinimumWidth(0)
        self.channel_activity_text.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        count_layout.addWidget(self.channel_activity_text,1)
        header_layout.addWidget(counts, 0, 4)
        self.channel_activity = NativeMidiActivityBar()
        self.channel_activity.set_theme(self.owner._dark)
        self.channel_activity.setFixedHeight(36)
        self.channel_activity.setMinimumWidth(0)
        self.channel_activity.setMaximumWidth(8)
        self.channel_activity.setToolTip(tr("monitor.activity_tooltip"))
        self.channel_activity.setAccessibleName(tr("monitor.activity_accessible"))
        header_layout.addWidget(self.channel_activity, 0, 5, 2, 1)
        header_host=QWidget()
        make_horizontally_elastic(header_host,QSizePolicy.Fixed)
        header_host_layout=set_layout(QVBoxLayout(header_host),(8,8,8,0),0)
        header_host_layout.addWidget(header)
        page_layout.insertWidget(0,header_host)
        self.instrument_bank.activated.connect(self._on_bank_selected)
        self.instrument_program.activated.connect(self._on_program_selected)

        editor_card = QFrame()
        editor_card.setObjectName('ChannelEditorFrame')
        self.channel_editor_card = editor_card
        editor_card.setMinimumWidth(0)
        editor_card.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        editor_layout = set_layout(QVBoxLayout(editor_card),
            header.content_insets(), 8)
        editor_layout.setAlignment(Qt.AlignTop)
        live_panel = QWidget()
        live_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        live_layout = set_layout(QGridLayout(live_panel), 0, 8)
        self.channel_mixer_panel = live_panel
        self._mixer_grid = live_layout
        self._mixer_sections = {}
        self._mixer_section_controls = {}
        for key in ("mix", "sends", "gestures"):
            section, grid = self._channel_section(tr(f"channel_editor.group_{key}"),
                                                 tr(f"channel_editor.section_{key}_tooltip"))
            self._mixer_sections[key] = (section, grid)
            self._mixer_section_controls[key] = []
        self.channel_mixer_controls = {}
        self._channel_names = None
        mixer_defaults = {field:CHANNEL_PARAMETERS[field].reset_value(default_reverb=ae.runtime.channel_sends.default_reverb)
                          for field in ("volume","pan","chorus","reverb","delay_send","modulation","pitch")}
        for kind in ("volume", "pitch", "pan", "reverb", "chorus", "delay_send", "modulation"):
            control = NativeKnobField(
                tr(f"channel_editor.{kind}"), CHANNEL_PARAMETERS[kind].minimum,
                CHANNEL_PARAMETERS[kind].maximum, mixer_defaults[kind],
                lambda value, kind=kind: self._set_mixer_value(kind, value),
                lambda value, kind=kind: (str(value) if kind == "delay_send" else
                                          self.owner._format_monitor_value(kind, value)),
                tooltip=(tr("channel_editor.pitch_knob_tooltip",
                            description=tr("monitor.pitch_tooltip"), value="0")
                         if kind == "pitch" else tr("channel_editor.delay_send_tooltip")
                         if kind == "delay_send" else self._with_neutral_hint(
                             tr(f"monitor.{kind}_tooltip"),
                             self.owner._format_monitor_value(kind, mixer_defaults[kind]))),
                editable=True, entry_domain=CHANNEL_PARAMETERS[kind].entry_domain,
                edit_context=self.expression_channel.currentIndex,
            )
            control.set_theme(self.owner._dark)
            self._install_midi_learn(control.knob, kind)
            self.channel_mixer_controls[kind] = control
            group = ("mix" if kind in ("volume", "pan") else
                     "sends" if kind in ("reverb", "chorus", "delay_send") else "gestures")
            self._mixer_section_controls[group].append(control)
        editor_layout.addWidget(live_panel)
        expression_layout.addWidget(editor_card)

        self.expression_group_grid = set_layout(QGridLayout(), 0, 8)
        self.expression_group_grid.setAlignment(Qt.AlignTop)
        self.expression_groups = []
        self._group_controls = {}
        self._group_grids = {}
        self.section_disclosures = {}
        for key in ("filter", "envelope", "vibrato", "tuning", "performance",
                    "pedals", "auxiliary_cc", "soundfont_cc", "modulation_routes",
                    "channel_pressure_routes", "key_pressure_routes"):
            collapsible = key in self.SECTION_UI_KEYS
            group, group_grid = self._channel_section(tr(f"channel_editor.group_{key}"),
                tr(f"channel_editor.section_{key}_tooltip"), collapsible=collapsible)
            if collapsible:
                self.section_disclosures[key] = group
                group.disclosure.bind_control(group.disclosure_button,
                    lambda expanded, key=key: self._set_section_expanded(key, expanded))
                group.disclosure.heightChanged.connect(
                    lambda key=key: self._section_height_changed(key))
            self._group_grids[key] = group_grid
            self._group_controls[key] = []
            self.expression_groups.append(group)
        self.performance_rows = self._group_controls["performance"]
        editor_layout.addLayout(self.expression_group_grid)
        # A resizable scroll content widget otherwise distributes unused
        # viewport height into the cards, making sparse groups look inflated.
        expression_layout.addStretch(1)

        self.portamento_switch = NativeToggleSwitch("")
        self.portamento_switch.setAccessibleName(tr("channel_editor.portamento"))
        self.portamento_switch.setChecked(True)
        portamento_row = self._compact_channel_field(
            tr("channel_editor.portamento"), self.portamento_switch,
            effect_tooltip("portamento"))
        self.portamento_switch.toggled.connect(
            lambda checked: self._set_expression("portamento", 127 if checked else 0))
        self.mode_switches = {}
        for target in CHANNEL_MODE_SWITCHES:
            switch = NativeToggleSwitch("")
            switch.setAccessibleName(tr(f"channel_editor.{target}"))
            row = self._compact_channel_field(tr(f"channel_editor.{target}"), switch,
                                             tr(f"channel_editor.{target}_tooltip"))
            self.performance_rows.append(row)
            self.mode_switches[target] = switch
            switch.toggled.connect(lambda checked, target=target: self._set_mode_switch(target, checked))
            self._install_midi_learn(switch, target)
        self.performance_rows.append(portamento_row)
        self.expression_controls = {}
        self._last_expression_observation = 0.0
        field_groups = {
            "cutoff": "filter", "resonance": "filter",
            "attack": "envelope", "decay": "envelope", "release": "envelope",
            "vibrato_rate": "vibrato", "vibrato_depth": "vibrato",
            "vibrato_delay": "vibrato", "fine_tuning": "tuning",
            "coarse_tuning": "tuning", "pitch_range": "tuning",
            "transpose": "tuning", "mix_level": "mix",
            "expression_level": "performance", "portamento_time": "performance",
            **{f"channel_pressure_{destination}": "channel_pressure_routes"
               for destination in ("vibrato", "pitch", "filter", "volume")},
            **{f"key_pressure_{destination}": "key_pressure_routes"
               for destination in ("vibrato", "pitch", "filter", "volume")},
            **{f"modulation_{destination}": "modulation_routes"
               for destination in ("vibrato", "pitch", "filter", "volume")},
        }
        for field in EXPRESSION_EVENTS:
            if field in ("portamento", "mode"):
                continue
            parameter = CHANNEL_PARAMETERS[field]
            minimum, maximum, unit = parameter.minimum, parameter.maximum, parameter.entry_kind
            control = NativeKnobField(
                tr(f"channel_editor.{field}"), minimum, maximum,
                parameter.default,
                lambda value, field=field: self._set_expression(field, value),
                lambda value, unit=unit: format_effect_value(value, unit),
                tooltip=self._with_neutral_hint(
                    effect_tooltip(field),
                    format_effect_value(EXPRESSION_DEFAULTS[field], unit)),
                fit_value_spacing=True,
                editable=True,
                entry_domain=parameter.entry_domain,
                edit_context=self.expression_channel.currentIndex,
            )
            control.set_theme(self.owner._dark)
            group = field_groups[field]
            (self._mixer_section_controls if group in self._mixer_section_controls else
             self._group_controls)[group].append(control)
            self._install_midi_learn(control.knob, field)
            self.expression_controls[field] = control
        self.performance_rows.remove(self.expression_controls["expression_level"])
        self.performance_rows.append(self.expression_controls["expression_level"])
        self.pedal_switches = {}
        for field in ("sustain", "sostenuto", "soft"):
            switch = NativeToggleSwitch("")
            switch.setAccessibleName(tr(f"channel_editor.{field}"))
            row = self._compact_channel_field(
                tr(f"channel_editor.{field}"), switch, effect_tooltip(field))
            switch.toggled.connect(
                lambda checked, field=field: self._set_mixer_value(field, 127 if checked else 0))
            self._group_controls["pedals"].append(row)
            self.pedal_switches[field] = switch
            self._install_midi_learn(switch, field)
        pressure = NativeKnobField(
            tr("channel_editor.channel_pressure"), 0, 127, 0,
            lambda value: self._set_mixer_value("channel_pressure", value),
            lambda value: format_effect_value(value, "midi_send"),
            tooltip=self._with_neutral_hint(effect_tooltip("channel_pressure"),
                                            format_effect_value(0, "midi_send")),
            editable=True, edit_context=self.expression_channel.currentIndex,
        )
        pressure.set_theme(self.owner._dark)
        self._group_controls["channel_pressure_routes"].insert(0, pressure)
        self.channel_pressure_control = pressure
        pressure.title.set_multiline_gap(2)
        pressure.title.set_preferred_tracking(.5)
        for control in self._group_controls["channel_pressure_routes"]:
            control.title.setFixedHeight(28)
        for controls in self._mixer_section_controls.values():
            for control in controls:
                control.title.setFixedHeight(28)
        self._install_midi_learn(pressure.knob, "channel_pressure")
        self.poly_pressure_note = AnimatedComboBox(self)
        self.poly_pressure_note.setProperty("compactChannelField", True)
        self.poly_pressure_note.setAccessibleName(tr("channel_editor.poly_pressure_note"))
        self.poly_pressure_note.setToolTip(tr("channel_editor.poly_pressure_note_tooltip"))
        for key in range(128):
            # MIDI key numbers avoid the inconsistent C3/C4 octave conventions
            # used by hosts. This is a key selector, not a CC assignment.
            self.poly_pressure_note.addItem(tr("channel_editor.poly_pressure_key", key=key), key)
        self.poly_pressure_note.setCurrentIndex(60)
        self.poly_pressure_control = NativeKnobField(
            tr("channel_editor.poly_pressure"), 0, 127, 0,
            self._set_poly_pressure, lambda value: format_effect_value(value, "midi_send"),
            tooltip=tr("channel_editor.poly_pressure_tooltip"), editable=True,
            edit_context=lambda: (self.expression_channel.currentIndex(),
                                  self.poly_pressure_note.currentData()),
        )
        self.poly_pressure_control.set_theme(self.owner._dark)
        self.poly_pressure_control.title.set_multiline_gap(2)
        self.poly_pressure_control.title.set_preferred_tracking(.5)
        self._install_midi_learn(self.poly_pressure_control.knob, "key_pressure")
        note_slot = ElasticPanel(preferred_width=self.poly_pressure_note.sizeHint().width())
        note_slot.setMinimumWidth(0)
        note_slot.setFixedHeight(self.poly_pressure_note.height())
        note_slot.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        note_layout = set_layout(QHBoxLayout(note_slot), 0, 0)
        make_horizontally_elastic(self.poly_pressure_note)
        note_layout.addWidget(self.poly_pressure_note)
        poly_header = self.section_disclosures["key_pressure_routes"].header_layout
        poly_header.insertWidget(poly_header.count() - 1, note_slot)
        self._group_controls["key_pressure_routes"].insert(0, self.poly_pressure_control)
        for control in self._group_controls["key_pressure_routes"]:
            control.title.setFixedHeight(28)
        self.poly_pressure_note.currentIndexChanged.connect(self._poly_pressure_key_changed)
        self.auxiliary_cc_controls = {}
        for number in AUXILIARY_CC:
            control = NativeKnobField(
                tr(f"channel_editor.cc_{number}"), 0, 127, 0,
                lambda value, number=number: self._set_auxiliary_cc(number, value),
                str,
                tooltip=tr("channel_editor.pan_fine_tooltip" if number==42 else "channel_editor.font_modulator_cc_tooltip" if number in SOUNDFONT_MODULATOR_CC else "channel_editor.auxiliary_cc_tooltip",
                           number=number, name=tr(f"channel_editor.cc_{number}")),
                editable=True, edit_context=self.expression_channel.currentIndex,
            )
            control.set_theme(self.owner._dark)
            self._install_midi_learn(control.knob, f"aux_{number}")
            if number==42:
                control.title.setFixedHeight(28)
                mix=self._mixer_section_controls["mix"]
                mix[:]=[mix[0],mix[2],mix[1],control]
            else:
                self._group_controls["soundfont_cc" if number in SOUNDFONT_MODULATOR_CC else "auxiliary_cc"].append(control)
            if number in SOUNDFONT_MODULATOR_CC:control.set_detail_caption('')
            self.auxiliary_cc_controls[number] = control
        font_controller_index(self.owner).changed.connect(self._refresh_midi_learn_indicators)
        for key, section in self.section_disclosures.items():
            for control in self._group_controls[key]:
                control.title.setFixedHeight(28)
            if key in ("channel_pressure_routes", "key_pressure_routes"):
                separator = QFrame()
                separator.setObjectName("ChannelEditorInputSeparator")
                separator.setFrameShape(QFrame.VLine)
                separator.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
                section.input_separator = separator
        self._layout_expression_groups()
        stored = getattr(self.owner, "_stored_ui_values", {})
        for key, section in self.section_disclosures.items():
            expanded = stored.get(self.SECTION_UI_KEYS[key], "1") != "0"
            blocked = section.disclosure_button.blockSignals(True)
            section.disclosure_button.setChecked(expanded)
            section.disclosure_button.blockSignals(blocked)
            section.disclosure.set_expanded(expanded)
        self._section_static_snapshots = [NativeLayoutSnapshot(target) for target in
            (self.channel_header, self.channel_mixer_panel,
             *(group for group in self.expression_groups if not hasattr(group,'disclosure')))]
        self._install_midi_learn(self.portamento_switch, "portamento")

    @staticmethod
    def _with_neutral_hint(tooltip, value):
        return tr("channel_editor.tooltip_with_reset_hint",
                  description=tooltip, value=value)

    def _build_channel_audio_effects(self):
        from channel_audio_effects import CHANNEL_FX_GROUPS
        layout=self.channel_editor_card.layout()
        section,grid=self._channel_section(tr('channel_editor.audio_effects'),collapsible=True)
        self.channel_audio_section=section
        self.section_disclosures['audio_effects']=section
        section.disclosure.bind_control(section.disclosure_button,
            lambda expanded:self._set_section_expanded('audio_effects',expanded))
        section.disclosure.heightChanged.connect(lambda:self._section_height_changed('audio_effects'))
        audio_layout=section.disclosure.content_layout
        self.channel_audio_notice=ElasticLabel(tr('channel_editor.audio_fx_drum_notice'))
        self.channel_audio_notice.setWordWrap(True)
        self.channel_audio_notice.setObjectName('AudioFXNotice')
        audio_layout.addWidget(self.channel_audio_notice)
        condition=tr('tooltip.performance.drum_send_audio_effect_condition',description='').strip()
        self.channel_audio_panel=NativeAudioFXPanel(self.owner,parent=section,
            runtime=ae.runtime.stream_effects.channel_fx.binding(self.selected_channel()),
            groups=CHANNEL_FX_GROUPS,save=self.commit,
            geometry_changed=self._channel_audio_geometry_changed,
            window_fit=self._channel_audio_geometry_changed,
            sample_rate=lambda:ae.runtime.stream_effects.channel_fx.sample_rate,
            channel_presentation=True,parameter_context=self)
        audio_layout.addWidget(self.channel_audio_panel)
        section.header.set_activation_control(self.channel_audio_panel.enabled_switch,enabled_by=self.channel_audio_panel)
        install_effect_tooltip(tr('channel_editor.audio_fx_scope_help',condition=condition),
            section.heading,section.disclosure_button)
        layout.addWidget(section)
        expanded=getattr(self.owner,'_stored_ui_values',{}).get(self.SECTION_UI_KEYS['audio_effects'],'0')!='0'
        section.disclosure_button.setChecked(expanded)
        section.disclosure.set_expanded(expanded)
        self._channel_audio_context=None
        self._refresh_channel_audio_effects()

    def _channel_audio_geometry_changed(self,*_):
        if not hasattr(self,'channel_audio_panel'):return
        invalidate_layout_chain(self.channel_editor_card)
        section=self.channel_audio_section
        section.disclosure.set_expanded(section.disclosure_button.isChecked(),refresh_extent=True)
        self.channel_editor_card.updateGeometry()

    def _refresh_channel_audio_effects(self):
        if not hasattr(self,'channel_audio_panel'):return
        manager=ae.runtime.stream_effects.channel_fx
        manager.sync_midi()
        channel=self.selected_channel();global_scope=self._global_scope()
        self.channel_audio_section.setToolTip(tr('channel_editor.audio_fx_select_channel') if global_scope else '')
        context=(channel,ae.runtime.stream_generation,manager.revision)
        if self._channel_audio_context!=context:
            slots,enabled=manager.snapshot(channel)
            from effect_control import audio_fx_ini
            old=self._channel_audio_context
            if old is None or old[:2]!=context[:2]:
                for _page,controls in self.channel_audio_panel._forms.values():
                    for control in controls:
                        if isinstance(control,(NativeKnobField,NativeControlRow)):control.value.cancel_editor()
                self.channel_audio_panel.effect_runtime=manager.binding(channel)
                self.channel_audio_panel.load_ini(audio_fx_ini(slots),enabled)
                self._channel_audio_context=context
            elif self.channel_audio_panel._pending_token is None:
                panel=self.channel_audio_panel
                if (audio_fx_ini(panel._committed_slots),panel._committed_enabled)!=(audio_fx_ini(slots),enabled):
                    selected=panel.selection.currentRow()
                    panel.slots=panel._committed_slots=slots;panel._committed_enabled=enabled
                    panel._blocked=True
                    panel.enabled_switch.setChecked(enabled);panel._blocked=False
                    panel.refresh(min(selected,len(slots)-1))
                self._channel_audio_context=context
        self.channel_audio_panel.setEnabled(not global_scope)
        self.channel_audio_notice.setVisible(not global_scope and bool(ae.runtime.addresses.drums[channel]))

    @staticmethod
    def _channel_section(title_text, tooltip=None, *, collapsible=False):
        return make_control_section(title_text, tooltip, collapsible=collapsible,
                                    background='card')

    def _set_section_expanded(self, key, expanded):
        section = self.section_disclosures[key]
        # Shared UI state, not per-channel sound state; commit immediately.
        self.commit()
        if self.isVisible() and self.content_stack.currentWidget() is self.expression_page and automatic_window_fit_allowed(self):
            scroll = self.expression_page.findChild(QScrollArea)
            content = scroll.widget()
            self._section_height_baseline = (content.width(),
                content.layout().totalHeightForWidth(content.width()),
                tuple(0 if item.disclosure.isHidden() else item.disclosure.height()
                      for item in self.section_disclosures.values()))
            content.set_height_override(self._section_height_baseline[1])
            self._section_transition_setup = True
            self._section_transitioning = True
            if any(snapshot._pixels is None for snapshot in self._section_static_snapshots):
                pixels = self.grab()
                for snapshot in self._section_static_snapshots:
                    snapshot.capture(pixels)
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        section.disclosure.set_expanded(expanded)
        if expanded and self._section_transitioning:
            self._fit_channel_window(animate=True)
        self._section_transition_setup = False
        self._finish_section_transition()

    def section_ui_values(self):
        return {self.SECTION_UI_KEYS[key]: "1" if section.disclosure_button.isChecked() else "0"
                for key, section in self.section_disclosures.items()}

    def _section_height_changed(self, key):
        section = self.section_disclosures[key]
        section.layout().invalidate()
        section.layout().activate()
        self.channel_editor_card.layout().activate()
        scroll = self.expression_page.findChild(QScrollArea)
        content = scroll.widget()
        baseline = getattr(self, "_section_height_baseline", None)
        if self._section_transitioning and baseline is not None and baseline[0] == content.width():
            current = tuple(0 if item.disclosure.isHidden() else item.disclosure.height()
                            for item in self.section_disclosures.values())
            content.set_height_override(baseline[1] + sum(current) - sum(baseline[2]))
        else:
            content.set_height_override(None)
        # Disclosure/window clocks can be a frame apart. Preserve the body's
        # measured minimum rather than squeeze upstream rows by1px while the
        # window's bottom is catching up.
        content.resize(content.width(), max(scroll.viewport().height(), content.minimumSizeHint().height()))
        content.layout().activate()
        self._finish_section_transition()

    def _finish_section_transition(self):
        if not getattr(self, "_section_transitioning", False):
            return
        if self._section_transition_setup:
            return
        if self._channel_window_motion._started_at is not None or any(
                section.disclosure._motion_started_at is not None for section in self.section_disclosures.values()):
            return
        self._section_transitioning = False
        self.expression_page.findChild(QScrollArea).widget().set_height_override(None)
        self._section_height_baseline = None
        for snapshot in self._section_static_snapshots:
            snapshot.release()
        self.expression_page.findChild(QScrollArea).setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    def _channel_required_height(self):
        """Measure current row hints plus the final saved disclosure heights."""
        scroll = self.expression_page.findChild(QScrollArea)
        content = scroll.widget()
        content.layout().activate()
        self.channel_editor_card.layout().activate()
        layout = content.layout()
        # Elastic fields intentionally have tiny preferred widths. sizeHint
        # can wrap their labels at that width and reserve phantom rows even
        # though the real viewport is wide. Measure the actual rendered width.
        height = (layout.totalHeightForWidth(content.width()) if layout.hasHeightForWidth()
                  else layout.sizeHint().height())
        for section in self.section_disclosures.values():
            disclosure = section.disclosure
            target = disclosure.content_layout.sizeHint().height() if section.disclosure_button.isChecked() else 0
            current = 0 if disclosure.isHidden() else disclosure.height()
            height += target - current
        root = self.layout()
        margins = root.contentsMargins()
        page_layout = self.expression_page.layout()
        page_margins = page_layout.contentsMargins()
        # The pinned header is a sibling of the scroll area,not part of its
        # content. Measure all visible siblings rather than hard-code a header
        # height; wrapped captions and future siblings may change this extent.
        page_items = [page_layout.itemAt(index) for index in range(page_layout.count())
                      if not page_layout.itemAt(index).isEmpty()]
        siblings = sum(max(0, item.heightForWidth(item.geometry().width())
                           if item.hasHeightForWidth() else item.sizeHint().height())
                       for item in page_items if item.widget() is not scroll)
        return (height + margins.top() + margins.bottom() + root.spacing()
                + self.channel_footer.sizeHint().height()
                + page_margins.top() + page_margins.bottom()
                + siblings + max(0,len(page_items)-1) * max(0,page_layout.spacing())
                + 2 * scroll.frameWidth() + 2 * self.content_stack.frameWidth())

    def _channel_height_limit(self, opening=False):
        if QApplication.platformName() == "offscreen":
            return 1000000  # Geometry tests have no physical work area.
        available = self.screen().availableGeometry()
        frame_height = max(0, self.frameGeometry().height() - self.height())
        top = available.top() if opening else max(available.top(), self.frameGeometry().top())
        return max(1, available.bottom() - top + 1 - frame_height)

    def _fit_channel_window(self, *, animate=False, opening=False):
        if not automatic_window_fit_allowed(self):return
        required = min(self._channel_required_height(), self._channel_height_limit(opening))
        height = max(self.height(), required) if animate else required
        target = QSize(self.width(), height)
        if animate:
            self._channel_window_motion.start(target, expanding=True)
        else:
            self._channel_window_motion.settle()
            self.resize(target)

    @staticmethod
    def _compact_channel_field(label, control, tooltip):
        field = QWidget()
        field.setMinimumWidth(0)
        field.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout = set_layout(QVBoxLayout(field), 0, 0)
        title = EditableNumberLabel(label)
        title.setObjectName("ChannelEditorFieldLabel")
        title.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        title.setWordWrap(True)
        layout.addWidget(title)
        control.setMinimumWidth(0)
        if isinstance(control, AnimatedComboBox):
            control.setProperty("compactChannelField", True)
        if isinstance(control, NativeToggleSwitch):
            # The switch paints its 36px track at x=1. Its empty-text size
            # hint is wider than that paint, so cap the *preferred* width to
            # the visible track; it still shrinks with the surrounding grid.
            control.setMaximumWidth(control.TRACK_WIDTH + 2)
        slot = QWidget()
        slot.setFixedHeight(40)
        slot.setMinimumWidth(0)
        slot_layout = set_layout(QHBoxLayout(slot), 0, 0)
        cluster = QWidget()
        cluster.setMinimumWidth(0)
        cluster.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        cluster_layout = set_layout(QHBoxLayout(cluster), 0, METRICS["channel_protocol_gap"])
        cluster_layout.setSizeConstraint(QLayout.SetNoConstraint)
        width = METRICS["channel_protocol_width"]
        field.cc_badge = NativeRotatedLabel("", text_px=METRICS["channel_protocol_font_px"],
                                            preferred_width=width, wrap=True)
        field.cc_badge.setMaximumWidth(width)
        cluster_layout.addWidget(field.cc_badge)
        cluster_layout.addWidget(control, 1, Qt.AlignCenter)
        balance = NativeRotatedLabel("", preferred_width=width,
                                    text_px=METRICS["channel_protocol_font_px"], clockwise=True)
        balance.setMaximumWidth(width)
        cluster_layout.addWidget(balance)
        field.cc_status = balance
        field.cc_control = control
        field.cc_cluster = cluster
        slot_layout.addWidget(cluster, 0, Qt.AlignCenter)
        field.title = title
        # Same53px envelope as NativeKnobField, including its readout area.
        # Empty-value controls reserve the same area, not another QLabel's
        # font-dependent height plus several spacing pixels.
        dial_readout = QWidget()
        dial_readout.setFixedHeight(NativeKnobField.DIAL_READOUT_HEIGHT)
        dial_readout.setMinimumWidth(0)
        dial_layout = set_layout(QVBoxLayout(dial_readout), 0, 0)
        dial_layout.addWidget(slot)
        dial_layout.addStretch(1)
        layout.addWidget(dial_readout)
        # Section height is determined by its densest neighbor. Extra height
        # belongs below the field, never between its title and control.
        layout.addStretch(1)
        install_effect_tooltip(tooltip, field, title, control)
        return field

    def _layout_expression_groups(self):
        self._sync_channel_header_hints()
        if hasattr(self, "poly_pressure_note"):
            combo = self.poly_pressure_note
            measurement_key = combo.font().key(), self.owner._dark, getattr(self.owner, "_compact_mode", False)
            if getattr(self, "_poly_note_measurement", None) != measurement_key:
                self._poly_note_measurement = measurement_key
                option = QStyleOptionComboBox()
                combo.initStyleOption(option)
                width = combo.sizeHint().width()
                option.rect = QRect(0, 0, width, combo.height())
                edit_rect = combo.style().subControlRect(QStyle.CC_ComboBox, option,
                                                         QStyle.SC_ComboBoxEditField, combo)
                text_width = max(QFontMetrics(combo.font()).horizontalAdvance(combo.itemText(index))
                                 for index in range(combo.count()))
                combo.parentWidget().set_preferred_width(text_width + width - edit_rect.width() + 2)
        columns = 3 if self.width() >= self.NORMAL_CHANNEL_WIDTH else 2 if self.width() >= 620 else 1
        mixer_columns = 3 if columns == 3 else 2 if columns == 2 else 1
        mixer_fields = 2 if self.width() >= 330 else 1
        if getattr(self, "_mixer_columns", None) != (mixer_columns, mixer_fields):
            self._mixer_columns = mixer_columns, mixer_fields
            mixer_grid = self._mixer_grid
            while mixer_grid.count():
                mixer_grid.takeAt(0)
            for index, key in enumerate(self._mixer_sections):
                section, group_grid = self._mixer_sections[key]
                while group_grid.count():
                    group_grid.takeAt(0)
                section_fields = min(len(self._mixer_section_controls[key]),
                                     len(self._mixer_section_controls[key]) if columns==3 else
                                     (2 if key=="mix" else 3) if self.width() >= 620 else mixer_fields)
                for field_index, control in enumerate(self._mixer_section_controls[key]):
                    group_grid.addWidget(control, field_index // section_fields,
                                         field_index % section_fields)
                for column in range(4):
                    group_grid.setColumnStretch(column, 1 if column < section_fields else 0)
                if columns==3:
                    start=sum(len(self._mixer_section_controls[previous]) for previous in tuple(self._mixer_sections)[:index])
                    mixer_grid.addWidget(section,0,start,1,section_fields,Qt.AlignTop)
                else:mixer_grid.addWidget(section,index//mixer_columns,index%mixer_columns,Qt.AlignTop)
            for column in range(9):
                mixer_grid.setColumnStretch(column,1 if column < (9 if columns==3 else mixer_columns) else 0)
        if getattr(self, "_expression_columns", None) != columns:
            self._expression_columns = columns
            grid = self.expression_group_grid
            while grid.count():
                grid.takeAt(0)
            unit=4 if columns==3 else 1
            positions=tuple((index//columns,index%columns*unit,unit) if index<6 else
                            ((6+columns-1)//columns+index-6,0,columns*unit)
                            for index in range(len(self.expression_groups)))
            for group, (row, column, span) in zip(self.expression_groups, positions):
                grid.addWidget(group, row, column, 1, span, Qt.AlignTop)
            for column in range(12):
                grid.setColumnStretch(column, 1 if column < (12 if columns == 3 else columns) else 0)
        preferred_fields={'filter':2,'envelope':3,'vibrato':3,'tuning':2,'performance':3,'pedals':2,
                          'auxiliary_cc':7,'soundfont_cc':4,'modulation_routes':4,
                          'channel_pressure_routes':5,'key_pressure_routes':5}
        field_columns=tuple(preferred_fields[key] if columns>1 or key.endswith('_routes') else
                            1 if self.width()<330 else 2 for key in self._group_grids)
        if getattr(self, "_field_columns", None) != field_columns:
            self._field_columns = field_columns
            for key, count in zip(self._group_grids, field_columns):
                group_grid = self._group_grids[key]
                while group_grid.count():
                    group_grid.takeAt(0)
                for index, control in enumerate(self._group_controls[key]):
                    pressure_group = key in ("channel_pressure_routes", "key_pressure_routes")
                    column = (index + (1 if index > 0 else 0)) if pressure_group else index % count
                    group_grid.addWidget(control, index // count, column)
                if pressure_group:
                    group_grid.addWidget(self.section_disclosures[key].input_separator, 0, 1)
                for column in range(8):
                    group_grid.setColumnStretch(column, (0 if column == 1 else 1)
                                                if pressure_group and column <= count else
                                                1 if not pressure_group and column < count else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "expression_group_grid"):
            self._layout_expression_groups()

    def minimumSizeHint(self):
        return QSize(0, 0)

    def _page(self, title, *, scrollable=False, in_tabs=True, plain=False):
        page = QWidget()
        layout = set_layout(QVBoxLayout(page), METRICS["form_page_padding"], CARD_ROW_GAPS["effects"])
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        card = WidthMeasuredPanel() if plain else NativeCard()
        if plain:
            card.setObjectName("ChannelEditorCanvas")
        card.setMinimumSize(0, 0)
        card.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        card_layout = set_layout(QVBoxLayout(card), 0 if plain else METRICS["card_padding"],
                                 CARD_ROW_GAPS["effects"])
        card_layout.setSizeConstraint(QLayout.SetNoConstraint)
        if scrollable:
            scroll = QScrollArea()
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setMinimumSize(0, 0)
            # The viewport otherwise paints the OS default white strips
            # around the themed card in Dark mode.
            scroll.setStyleSheet(transparent_scroll_area_stylesheet())
            scroll.setWidget(card)
            layout.addWidget(scroll)
        else:
            layout.addWidget(card)
        if in_tabs:
            self.tabs.addTab(page, tr(title))
        return page, card_layout

    def show_channel(self, channel):
        """The monitor Edit entry is an immediately applied channel surface."""
        fresh_open = not self.isVisible() or self.content_stack.currentWidget() is not self.expression_page
        self.setModal(False)
        self._sync_channel_addresses()
        self.expression_channel.setCurrentIndex(self.expression_channel.findData(channel))
        self.content_stack.setCurrentWidget(self.expression_page)
        self._update_channel_title()
        self.channel_footer.show()
        if fresh_open and automatic_window_fit_allowed(self):
            available = self.screen().availableGeometry()
            # 900 px fits the normal three-column editor; it is a preferred
            # opening size, not a minimum imposed on user resizing.
            preferred_width = (self.NORMAL_CHANNEL_WIDTH if QApplication.platformName() == "offscreen" else
                               min(self.NORMAL_CHANNEL_WIDTH, max(1, available.width() - 24)))
            self.resize(preferred_width, self.height())
            self._layout_expression_groups()
        if fresh_open:
            if self.isVisible():
                self._fit_channel_window(opening=True)
            else:
                self._fit_channel_on_show = True
        self._populate_instrument_choices()
        self.refresh_live_monitor()

    def _global_scope(self):
        return self.expression_channel.currentIndex() >= 0 and self.expression_channel.currentData() is None

    def selected_channel(self):
        """Native channel identity, independent of the selector's display order."""
        value = self.expression_channel.currentData()
        return int(value) if value is not None else 0

    def _sync_channel_addresses(self):
        count = ae.runtime.addresses.count
        if self.expression_channel.count() == count + 1:
            return
        previous = self.selected_channel()
        was_global = self.expression_channel.count() > 0 and self._global_scope()
        blocked = self.expression_channel.blockSignals(True)
        self.expression_channel.clear()
        self.expression_channel.addItem(tr("channel_editor.all_channels"), None)
        for channel in range(count):
            caption = (tr("channel_editor.channel_item", number=channel + 1) if count == 16 else
                       tr("channel_editor.port_channel", port=channel // 16, number=channel % 16 + 1))
            self.expression_channel.addItem(caption, channel)
        self.expression_channel.setCurrentIndex(self.expression_channel.findData(
            None if was_global else min(previous, count - 1)))
        self.expression_channel.blockSignals(blocked)
        self._sync_channel_header_hints()

    def _channel_port_category(self,row):
        channel=self.expression_channel.itemData(row)
        if channel is None or ae.runtime.addresses.count<=16:return ()
        names={entry['slot']:entry['name'] for entry in self.owner.midi_input_runtime.port_settings.ports}
        port=channel//16
        return (names.get(port,tr('monitor.port_item',port=port)),)

    def _sync_channel_header_hints(self):
        """Measured normal hints, not minimums; long Instrument yields first."""
        fields=getattr(self,'_channel_header_fields',())
        key=(self.owner._dark,getattr(self.owner,'_color_theme',None),self.devicePixelRatioF(),
             tuple((control.count(),control.font().key(),label.font().key(),label.text()) for _,label,control in fields))
        if getattr(self,'_channel_header_hint_key',None)==key:return
        self._channel_header_hint_key=key
        counts=getattr(self,'channel_counts',None)
        if counts is not None:
            # ElasticPanel's height hint follows its current height. Give this
            # single text row its actual font height before the first snapshot.
            label=self.channel_activity_text
            counts.setFixedHeight(label.fontMetrics().height())
            counts.set_preferred_width(max(label.fontMetrics().horizontalAdvance(
                tr('channel_editor.active_counts',notes=100000,voices=value))
                for value in (100000,tr('channel_editor.unknown_value'))))
        for index,(panel,label,control) in enumerate(fields):
            panel.setFixedHeight(label.fontMetrics().height()+4+control.height())
            option=QStyleOptionComboBox();control.initStyleOption(option)
            option.rect=QRect(0,0,200,control.height())
            text_rect=control.style().subControlRect(QStyle.CC_ComboBox,option,QStyle.SC_ComboBoxEditField,control)
            chrome=200-text_rect.width()+4
            caption=max((control.fontMetrics().horizontalAdvance(control.itemText(row))
                for row in range(control.count())),default=control.fontMetrics().horizontalAdvance('000')) if index<3 else 0
            panel.set_preferred_width(max(label.fontMetrics().horizontalAdvance(label.text()),caption+chrome))

    def selected_channels(self):
        return tuple(ae.runtime.addresses.channels()) if self._global_scope() else (self.selected_channel(),)

    def _update_channel_title(self):
        self.setWindowTitle(tr("channel_editor.all_title") if self._global_scope()
                            else tr("channel_editor.title", number=self.selected_channel() % 16 + 1))

    def _common_value(self, field):
        values = [ae.channel_control_value(channel, field) for channel in self.selected_channels()]
        return values[0] if len(set(values)) == 1 else None

    def _control_readbacks(self, field, channels=None):
        result=[]
        for channel in self.selected_channels() if channels is None else channels:
            pending=self.owner.channel_command_presentation.pending_value(channel,field)
            result.append(ae.ChannelReadback(pending,True,'requested') if pending is not None else
                          ae.channel_control_readback(channel,field))
        return result

    def _switch_values(self, field):
        return [value.value >= 64 if value.known else None for value in self._control_readbacks(field)]

    def _present_control(self, control, field, observed=None):
        if control.knob.isSliderDown() or getattr(control.value, "_editing", False):
            return
        channels = self.selected_channels()
        readbacks = self._control_readbacks(field, channels)
        context = (channels, ae.observation_display_stamp())
        now = time.monotonic()
        if any(readback.source == 'pending' for readback in readbacks):
            previous = getattr(control, '_qualified_display_context', None)
            if previous is not None and previous[0] == context and now - previous[1] <= 1:
                return  # Short metadata retry: keep the last qualified display.
        unknown = any(not readback.known for readback in readbacks)
        if not unknown:
            control._qualified_display_context = context, now
        else:
            control._qualified_display_context = None
        values = [readback.value for readback in readbacks]
        pending=any(self.owner.channel_command_presentation.pending_value(ch,field) is not None for ch in channels)
        value = (observed if observed is not None and not unknown and not pending else
                 values[0] if not unknown and len(set(values)) == 1 else None)
        if value is not None and control.get() != value:
            control.set_observed(value)
        control.set_mixed(value is None and not unknown)
        control.set_unknown(unknown)
        if unknown:
            control.value.setAccessibleDescription(tr("channel_editor.unknown_hint"))
        elif value is None:
            control.value.setAccessibleDescription(tr("channel_editor.mixed_hint"))
        else:
            control.value.setAccessibleDescription("")

    @staticmethod
    def _present_switch(switch, values):
        unknown = any(value is None for value in values)
        mixed = not unknown and len(set(values)) > 1
        state = switch.isChecked() if unknown else bool(values[0]) if not mixed else False
        if (switch.isChecked() == state and bool(switch.property("mixedValue")) == mixed
                and bool(switch.property("unknownValue")) == unknown):
            return
        blocked = switch.blockSignals(True)
        switch.setChecked(state)
        switch.blockSignals(blocked)
        switch.setProperty("mixedValue", mixed)
        switch.setProperty("unknownValue", unknown)
        switch.setAccessibleDescription(tr("channel_editor.unknown_hint") if unknown else
                                       tr("channel_editor.mixed_hint") if mixed else "")
        switch.update()

    def _populate_instrument_choices(self):
        group = self.owner.soundfont_runtime.group
        key = (tuple(self.owner.soundfont_runtime.handles),
               tuple(self.owner.soundfont_runtime.group_key(group,entry) for entry in group.fonts),
               tuple((item.bank, item.preset, item.index, item.disabled)
                     for item in group.presets),tuple(group.mappings))
        if key == self._instrument_catalog_key:
            return
        self._instrument_catalog_key = key
        catalog = self.owner.enumerate_instruments(group)
        self._instrument_source_catalog=catalog
        self._select_instrument_catalog(0)
        self._sync_instrument_selectors()

    def _select_instrument_catalog(self,lsb):
        group=self.owner.soundfont_runtime.group
        mode=getattr(self.owner,'_report_playback_snapshot',{}).get('midi_system_mode')
        resolved=resolve_mapped_instruments(group,getattr(self,'_instrument_source_catalog',()),lsb,system_mode=mode)
        self._instrument_source_records={(row['bank'],row['preset']):row for row in resolved}
        self._instrument_choices = {
            (row["bank"], row["preset"]): row["name"]
            for row in resolved
            if 0 <= row["bank"] <= 128 and 0 <= row["preset"] < 128
        }

    def _sync_instrument_selectors(self, channel_values=None):
        channel = self.selected_channel()
        if channel < 0:
            return
        global_scope = self._global_scope()
        snapshot = ae.channel_monitor_snapshot(channels=self.selected_channels())
        values = channel_values or snapshot[0 if global_scope else channel]
        bank, program = values["bank"], values["program"]
        self._select_instrument_catalog(values.get('bank_lsb',0))
        blocked = self.instrument_bank_lsb.blockSignals(True)
        lsb_values = {snapshot[ch]["bank_lsb"] for ch in self.selected_channels()}
        self.instrument_bank_lsb.setCurrentIndex(next(iter(lsb_values)) if len(lsb_values) == 1 else -1)
        self.instrument_bank_lsb.setPlaceholderText(tr("channel_editor.mixed_value"))
        self.instrument_bank_lsb.blockSignals(blocked)
        banks = sorted({key[0] for key in self._instrument_choices
                        if global_scope or ae.runtime.addresses.drums[channel] == (key[0] == 128)} | {bank})
        self.instrument_bank.blockSignals(True)
        self.instrument_bank.clear()
        for choice in banks:
            self.instrument_bank.addItem(f"{choice:03d}", choice)
        self.instrument_bank.setCurrentIndex(self.instrument_bank.findData(bank))
        self.instrument_bank.setEnabled(global_scope or not ae.runtime.addresses.drums[channel])
        if global_scope and len({snapshot[ch]["bank"] for ch in ae.runtime.addresses.channels() if not ae.runtime.addresses.drums[ch]}) > 1:
            self.instrument_bank.setCurrentIndex(-1)
            self.instrument_bank.setPlaceholderText(tr("channel_editor.mixed_value"))
        self.instrument_bank.blockSignals(False)
        self._sync_program_choices(bank, program)
        if global_scope and len({row["program"] for row in snapshot.values()}) > 1:
            self.instrument_program.setCurrentIndex(-1)
            self.instrument_program.setPlaceholderText(tr("channel_editor.mixed_value"))

    def _refresh_selected_channel(self):
        self._global_observe_at = 0
        global_scope = self._global_scope()
        self.cc_reset_channel.setEnabled(not global_scope)
        self.expression_reset.setText(tr("channel_editor.reset_all") if global_scope else tr("channel_editor.reset"))
        ae.runtime.midi_learn.cancel()
        for _widget, field, _caption in self._cc_fields.values():
            field.title.cancel_editor()
        for control in (*self.channel_mixer_controls.values(),
                        *self.expression_controls.values(),
                        *self.auxiliary_cc_controls.values(),
                        self.channel_pressure_control, self.poly_pressure_control):
            control.value.cancel_editor()
        self._displayed_program = None
        self.refresh_live_monitor()
        self._update_channel_title()
        self._refresh_channel_audio_effects()

    def _sync_program_choices(self, bank, selected):
        programs = sorted({key[1] for key in self._instrument_choices if key[0] == bank}
                          | {selected})
        self.instrument_program.blockSignals(True)
        self.instrument_program.clear()
        for program in programs:
            name = self._instrument_choices.get((bank, program))
            if not name and program == selected and self._channel_names is not None:
                channel=self.selected_channels()[0]
                name = self._channel_names[channel] if channel<len(self._channel_names) else None
            self.instrument_program.addItem(
                tr("channel_editor.program_named", number=program, name=name)
                if name else f"{program:03d}", program)
        self.instrument_program.setCurrentIndex(self.instrument_program.findData(selected))
        self.instrument_program.blockSignals(False)

    def _instrument_category(self, row):
        program = self.instrument_program.itemData(row)
        if self.instrument_bank.currentData() == 128:
            return (tr("channel_editor.instrument_category_drums"),)
        return (tr(f"channel_editor.instrument_family_{int(program) // 8}"),) if program is not None else ()

    def _on_bank_selected(self, _index):
        channel = self.selected_channel()
        bank = self.instrument_bank.currentData()
        if bank is None or channel < 0:
            return
        channels=self.selected_channels()
        program = ae.channel_monitor_snapshot(channels=channels)[channels[0]]["program"]
        if (bank, program) not in self._instrument_choices:
            available = sorted(candidate for candidate_bank, candidate in self._instrument_choices
                               if candidate_bank == bank)
            if available:
                program = available[0]
        self._select_instrument(bank, program)

    def _on_bank_lsb_selected(self, index):
        if index < 0:
            return
        self.owner.submit_channel_instruments(tuple((channel,2,0,index,0) for channel in self.selected_channels()))

    def _on_program_selected(self, _index):
        bank, program = self.instrument_bank.currentData(), self.instrument_program.currentData()
        if program is not None:
            self.owner.submit_channel_instruments(tuple((channel,5 if bank is not None else 4,
                128 if ae.runtime.addresses.drums[channel] else bank if bank is not None else 0,0,program)
                for channel in self.selected_channels()))

    def _select_instrument(self, bank, program):
        self.owner.submit_channel_instruments(tuple((channel,5,
            128 if ae.runtime.addresses.drums[channel] else bank,0,program) for channel in self.selected_channels()))

    def _set_mixer_value(self, kind, value):
        receipt=self.owner.submit_channel_controls(self.selected_channels(),kind,value)
        from native_widgets import NativeChannelCommandPresentation
        if not NativeChannelCommandPresentation.accepted(receipt):
            if NativeChannelCommandPresentation._get(receipt,'mode')=='legacy':self.refresh_live_monitor()

    def _set_mode_switch(self, target, checked):
        self.owner.submit_channel_modes(self.selected_channels(),target,checked)
        self._refresh_expression()

    def _set_auxiliary_cc(self, number, value):
        for channel in self.selected_channels():
            binding = ae.runtime.midi_learn.binding(channel, f"aux_{number}")
            self.owner.submit_auxiliary_controls((channel,), binding[0] if binding else number, value)

    def _reset_cc(self, all_channels):
        for _widget, field, _caption in self._cc_fields.values():
            field.title.cancel_editor()
        ae.reset_channel_assignments(None if all_channels or self._global_scope()
                                     else self.selected_channel())
        self._refresh_midi_learn_indicators()
        self.refresh_live_monitor()

    def _install_midi_learn(self, widget, target):
        if target != "mode":
            widget.setContextMenuPolicy(Qt.CustomContextMenu)
            widget.customContextMenuRequested.connect(
                lambda point, source=widget, field=target:
                self._show_midi_learn_menu(source, field, point))
        # Store semantic copy, not the width-wrapped HTML presentation. It is
        # later combined with current routing state and formatted exactly once.
        widget.setProperty("learn_base_tooltip", widget.accessibleDescription())
        field = widget.parentWidget()
        while field is not None and not hasattr(field, "title"):
            field = field.parentWidget()
        if field is None:
            raise RuntimeError("CC control has no presentation field")
        if isinstance(field, NativeKnobField):
            field.add_cc_identity()
        field.setAttribute(Qt.WA_StyledBackground, True)
        field.cc_badge.set_theme(self.owner._dark)
        self._cc_fields[target] = widget, field, field.title.text()
        # Match the existing pressure title's measured ink spacing. Standard
        # one-line captions are unchanged; a derived connection is two lines
        # inside the shared 28px caption slot, not Qt's 34px line boxes.
        field.title.set_multiline_gap(2)
        # Both protocol caption and title offer the same context actions.
        for surface in (field.title, field.cc_badge, *((field.value,) if isinstance(field, NativeKnobField) else ())):
            if target == "mode":
                continue
            surface.setContextMenuPolicy(Qt.CustomContextMenu)
            surface.customContextMenuRequested.connect(
                lambda _point, source=surface, control=widget, key=target:
                self._show_midi_learn_menu(control, key,
                    control.mapFromGlobal(source.mapToGlobal(source.rect().center()))))

    def _show_midi_learn_menu(self, widget, target, point):
        channel = self.selected_channels()[0]
        _revision, pending, assigned = ae.runtime.midi_learn.snapshot()
        menu, actions = self._control_mapping_menu(target)
        assign, learn, function, restore = (actions[key] for key in ("cc", "learn", "function", "restore"))
        chosen = menu.exec(widget.mapToGlobal(point))
        if chosen is assign:
            request = self._request_manual_cc(widget, target)
            if request is not None:
                number, destination, all_channels = request
                ae.runtime.midi_learn.assign(channel, target, number,
                    destination=destination, all_channels=all_channels)
        elif chosen is learn:
            if pending == (channel, target):
                ae.runtime.midi_learn.cancel()
            else:
                request = self._request_manual_cc(widget, target, learning=True)
                if request is not None:
                    _number, destination, all_channels = request
                    ae.runtime.midi_learn.arm(channel, target,
                        destination=destination, all_channels=all_channels)
        elif function is not None and chosen is function:
            self._request_function_mapping(widget, target)
        elif chosen is restore:
            self._restore_control_mapping(target)
        self._refresh_midi_learn_indicators()
        menu.deleteLater()

    def _control_mapping_menu(self, target):
        menu, assign, learn = self._create_midi_learn_menu()
        function = menu.addAction(tr("channel_editor.assign_function")) if (
            target in CHANNEL_FUNCTION_TARGETS or target.startswith("aux_")) else None
        menu.addSeparator()
        restore = menu.addAction(tr("channel_editor.restore_control_mapping"))
        caption = self._cc_fields[target][2]
        restore.setEnabled(any(ae.runtime.midi_learn.binding(ch, target) or
            ae.runtime.midi_learn.control_name(ch, target, caption) != caption or
            ae.runtime.channel_policy.mapped(ch, target) for ch in self.selected_channels()))
        restore.setToolTip(tr("channel_editor.restore_control_mapping_hint",
            scope=tr("channel_editor.cc_all_channels" if self._global_scope() else
                     "channel_editor.cc_this_channel")))
        return menu, dict(cc=assign, learn=learn, function=function, restore=restore)

    def _restore_control_mapping(self, target):
        # Configuration only. No CC121, no numerical setter/reset-default write.
        channels = self.selected_channels()
        ae.runtime.midi_learn.remove_control(channels, target)
        if target in CHANNEL_FUNCTION_TARGETS:
            ae.runtime.channel_policy.assign(channels, target, target)
        self._refresh_midi_learn_indicators()
        self.refresh_live_monitor()

    def _request_function_mapping(self, widget, target):
        if target.startswith("aux_"):
            # Auxiliary CC has no native effect. Its existing binding owns the
            # destination, so use the SAME chooser, not a second policy layer.
            request = self._request_manual_cc(widget, target)
            if request is not None:
                number, destination, all_channels = request
                ae.runtime.midi_learn.assign(self.selected_channels()[0], target, number,
                    destination=destination, all_channels=all_channels)
            return
        dialog = NativeChannelMappingDialog(self)
        dialog.setWindowTitle(tr("channel_editor.assign_function"))
        dialog.setMinimumSize(0, 0)
        root = set_layout(QVBoxLayout(dialog), METRICS["outer"], METRICS["section_gap"])
        root.setSizeConstraint(QLayout.SetNoConstraint)
        root.addWidget(QLabel(widget.accessibleName()))
        hint = QLabel(tr("channel_editor.function_mapping_hint"))
        hint.setWordWrap(True)
        root.addWidget(hint)
        if target == "key_pressure":
            native = QLabel(channel_native_input_description(target, self.selected_channels()[0]))
            native.setWordWrap(True)
            native.setMinimumWidth(0)
            native.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            root.addWidget(native)
        destination = AnimatedComboBox()
        destination.setAccessibleName(tr("channel_editor.cc_destination"))
        destination.addItem(tr("channel_editor.function_none"), None)
        for field in CHANNEL_FUNCTION_TARGETS:
            destination.addItem(self._channel_parameter_label(field), field)
        values = [ae.runtime.channel_policy.destination(ch, target) for ch in self.selected_channels()]
        destination.setCurrentIndex(destination.findData(values[0]) if len(set(values)) == 1 else -1)
        destination.setPlaceholderText(tr("channel_editor.mixed_value"))
        root.addWidget(destination)
        scope = NativeChannelScopeChoice(all_channels=self._global_scope())
        scope.setEnabled(not self._global_scope())
        root.addWidget(scope)
        footer = set_layout(QHBoxLayout(), 0, METRICS["form_column_gap"])
        footer.addStretch(1)
        confirm = make_action_button(tr("common.apply"), dialog.accept, primary=True)
        confirm.setDefault(True)
        confirm.setEnabled(destination.currentIndex() >= 0)
        destination.currentIndexChanged.connect(lambda index: confirm.setEnabled(index >= 0))
        footer.addWidget(confirm)
        footer.addWidget(make_action_button(tr("common.cancel"), dialog.reject))
        root.addLayout(footer)
        apply_native_text_metrics(dialog)
        dialog.ensurePolished()
        dialog.resize(430, root.totalHeightForWidth(430))
        try:
            if dialog.exec() == QDialog.Accepted:
                ae.runtime.channel_policy.assign(ae.runtime.addresses.channels() if scope.all_channels() else self.selected_channels(),
                                                  target, destination.currentData())
                ae.ensure_channel_event_filter()
                self.refresh_live_monitor()
        finally:
            dialog.deleteLater()

    def _edit_reset_defaults(self):
        dialog = NativeChannelResetDefaultsDialog(self)
        try:
            if dialog.exec() == QDialog.Accepted:
                changes=ae.runtime.stream_effects.channel_fx.replace_reset_defaults(self.selected_channels(),dialog.selected_effect_values())
                if any(result is False for _ch,_token,result in changes):
                    self.owner.append_log(log_text('audio_fx.invalid'));return
                ae.runtime.channel_policy.replace_defaults(self.selected_channels(), dialog.selected_values())
                self.commit()
        finally:
            dialog.deleteLater()

    def _channel_parameter_label(self, key, channel=None):
        """Disambiguate identical short knob captions by their owning section."""
        caption = tr("channel_editor.voice_mode") if key == "mode" else tr(f"channel_editor.{key}")
        caption = caption.replace("\n", " ")
        section = None
        entry = self._cc_fields.get(key)
        if entry:
            parent = entry[1].parentWidget()
            while parent is not None:
                if hasattr(parent, "heading"):
                    section = parent.heading.text()
                    break
                parent = parent.parentWidget()
        if key == "mode":
            section = tr("channel_editor.group_performance")
        label = tr("channel_editor.parameter_path", section=section, parameter=caption) if section else caption
        if channel is not None and key in CHANNEL_FUNCTION_TARGETS:
            destination = ae.runtime.channel_policy.destination(channel, key)
            if destination != key:
                label = tr("channel_editor.parameter_mapping", parameter=label,
                           destination=self._channel_parameter_label(destination)
                           if destination is not None else tr("channel_editor.function_none"))
        return label

    def _format_channel_parameter(self, field, value):
        if value is None:
            return tr("channel_editor.unknown_value")
        if field in self.channel_mixer_controls:
            return self.channel_mixer_controls[field].formatter(value)
        if field in self.expression_controls:
            return self.expression_controls[field].formatter(value)
        if field in ("bank", "bank_lsb", "program"):
            return f"{value:03d}"
        if field == "mode":
            return tr(f"channel_editor.{('mode_poly', 'mode_mono', 'mode_legato')[value]}")
        if field in ("sustain", "sostenuto", "soft", "portamento"):
            return tr("common.on" if value >= 64 else "common.off")
        return str(value)

    def _create_midi_learn_menu(self):
        menu = NativeMenu(self)
        assign = menu.addAction(tr("channel_editor.assign_cc"))
        learn = menu.addAction(tr("channel_editor.learn_cc"))
        return menu, assign, learn

    def _request_manual_cc(self, widget, target, assigned=None, *, learning=False):
        if target == "mode":
            return None
        # bassmidi.h: CC 120..127 are channel-mode commands, not assignable
        # automation sources. The MIDI input thread uses this same 0..119 bound.
        binding = ae.runtime.midi_learn.binding(self.selected_channels()[0], target)
        dialog = NativeMidiCcAssignmentDialog(self, target, widget.accessibleName(), binding,
                                               learning=learning)
        if self._global_scope():
            dialog.scope.set_all_channels(True)
            dialog.scope.setEnabled(False)
        try:
            if dialog.exec() != QDialog.Accepted:
                return None
            return (dialog.source.currentData(),
                    dialog.destination.currentData() if target.startswith("aux_") else None,
                    dialog.scope.all_channels())
        finally:
            dialog.deleteLater()

    def _connection_no_effect(self, channel, key):
        """Applicable connection, not whether a current numeric value is zero."""
        def supported(target):
            destination = ae.runtime.channel_policy.destination(channel, target)
            return destination is not None and not (
                ae.runtime.addresses.drums[channel] and (
                    destination == "transpose" or destination == "delay_send" and channel != 9))
        if not supported(key): return True
        if not key.startswith("aux_"): return False
        binding = ae.runtime.midi_learn.binding(channel, key)
        source = binding[0] if binding else CHANNEL_CC_DEFAULTS.get(key)
        routes = ae.runtime.midi_learn.targets_for_source(channel, source)
        # An explicit route consumes the RAW CC. Only the unbound native
        # CC21..24 path is conditionally interpreted by an SF2 modulator.
        return not any(supported(target) for target in routes) if routes else source not in NATIVE_ADDITIONAL_CC

    def _font_defined_cc(self, channel, key):
        if not key.startswith("aux_"): return False
        binding = ae.runtime.midi_learn.binding(channel,key)
        source = binding[0] if binding else CHANNEL_CC_DEFAULTS[key]
        return (source in SOUNDFONT_MODULATOR_CC and not self._connection_no_effect(channel,key)
                and ae.runtime.channel_policy.destination(channel,key)==key
                and not ae.runtime.midi_learn.targets_for_source(channel,source))

    def _inspect_font_cc(self):
        service=font_controller_index(self.owner)
        values=ae.channel_monitor_snapshot(channels=self.selected_channels())
        mode=getattr(self.owner,'_report_playback_snapshot',{}).get('midi_system_mode')
        stamp=(self._instrument_catalog_key,self.owner.soundfont_runtime.source_generation,service.revision,
               mode,tuple((row['bank'],row['program'],row.get('bank_lsb',0)) for row in values.values()))
        if getattr(self,'_font_cc_stamp',None)==stamp:return
        self._font_cc_stamp=stamp;presentations={};group=self.owner.soundfont_runtime.group
        by_lsb={lsb:{(item['bank'],item['preset']):item for item in resolve_mapped_instruments(
            group,getattr(self,'_instrument_source_catalog',()),lsb,system_mode=mode)}
            for lsb in {row.get('bank_lsb',0) for row in values.values()}}
        for cc in (21,22,23,24):
            results=[]
            for row in values.values():
                record=by_lsb[row.get('bank_lsb',0)].get((row['bank'],row['program']))
                if (record is None or row.get('bank_lsb',0) and not record.get('source_mapping_qualified')
                        or not 0<=record['source_index']<len(group.fonts)):
                    results.append((tr('font_cc.unknown'),tr('font_cc.source_unknown')));continue
                entry=group.fonts[record['source_index']]
                report=service.request(group.font_path(entry),(self.owner.soundfont_runtime.source_generation,
                    self.owner.soundfont_runtime.group_key(group,entry)))
                status,detail=font_controller_text(report,record.get('source_bank',record['bank']),
                    record.get('source_preset',record['preset']),cc)
                # The tooltip stays concise. Full zone/curve definitions belong
                # in the inspector's deliberate detail disclosure,not a popup.
                results.append((status,detail))
            results=list(dict.fromkeys(results))
            presentations[cc]=(results[0] if len(results)==1 else
                (tr('font_cc.mixed'),'\n'.join(detail for _,detail in results)))
        self._font_cc_presentations=presentations


    def _common_control_name(self, key, caption):
        names = {ae.runtime.midi_learn.control_name(ch, key, caption) for ch in self.selected_channels()}
        return next(iter(names)).replace("\n", " ") if len(names) == 1 else ""

    def _control_destinations(self, channel, key):
        if not key.startswith("aux_"):
            return (ae.runtime.channel_policy.destination(channel, key),)
        binding = ae.runtime.midi_learn.binding(channel, key)
        source = binding[0] if binding else CHANNEL_CC_DEFAULTS[key]
        routes = ae.runtime.midi_learn.targets_for_source(channel, source)
        # An auxiliary readout observes/sends this CC even when another
        # control owns its route. Apply exactly the same single policy edge
        # as dispatch; do not recursively invent an audio routing chain.
        return tuple(dict.fromkeys(ae.runtime.channel_policy.destination(channel, route)
                                   for route in routes)) if routes else (key,)

    def _control_title(self, key, caption, channels):
        names = {ae.runtime.midi_learn.control_name(ch, key, caption) for ch in channels}
        if len(names) != 1:
            return tr("channel_editor.mixed_value"), True
        name = next(iter(names))
        destinations = {self._control_destinations(ch, key) for ch in channels}
        if destinations == {(key,)}:
            return name, False
        destination = (tr("channel_editor.mixed_value") if len(destinations) != 1 else
                       ", ".join(tr(f"channel_editor.{target}").replace("\n", " ")
                                 if target is not None else tr("channel_editor.cc_no_effect_status")
                                 for target in next(iter(destinations))))
        return tr("channel_editor.mapped_control_name", control=name.replace("\n", " "),
                  destination=destination), False

    def _refresh_midi_learn_indicators(self):
        self._inspect_font_cc()
        revision, pending, assigned = ae.runtime.midi_learn.snapshot()
        signature = (revision, self.expression_channel.currentIndex(),
                     ae.runtime.channel_policy.revision,
                     ae.runtime.midi_learn.caption_revision,
                     ae.runtime.midi_options.cc6_direct_pitch_range,
                     ae.runtime.midi_options.cc94_custom_delay_send,
                     tuple(ae.runtime.addresses.drums[ch] for ch in self.selected_channels()))
        signature=(*signature,self._font_cc_stamp)
        if getattr(self, "_learn_revision", None) == signature:
            return
        channel = self.selected_channels()[0]
        self._learn_revision = signature
        protocols = {**{key: "SysEx" for key in CHANNEL_SYSEX_ROUTES},
                     **{key: "RPN" for key in CHANNEL_RPN_DEFAULTS}}
        for key, (widget, field, caption) in self._cc_fields.items():
            base = widget.property("learn_base_tooltip") or ""
            binding = ae.runtime.midi_learn.binding(channel, key)
            if binding:
                source, destination, name, _all = binding
                badge = f"CC#{source:03d}"
            else:
                source = CHANNEL_CC_DEFAULTS.get(key)
                if key == "delay_send" and not ae.runtime.midi_options.cc94_custom_delay_send:
                    source = None
                badge = f"CC#{source:03d}" if source is not None else protocols.get(key, "")
            title, _name_mixed = self._control_title(key, caption, (channel,))
            field.title.setText(title)
            field.title.setAccessibleName(title)
            if binding or key.startswith("aux_"):
                field.title.configure_text(
                    read=lambda key=key, caption=caption: ae.runtime.midi_learn.control_name(
                        self.selected_channels()[0], key, caption).replace("\n", " "),
                    write=lambda text, key=key: self._rename_cc(key, text),
                    context=self.expression_channel.currentIndex, label=caption)
            else:
                field.title.disable_entry()
            routes = ae.runtime.midi_learn.targets_for_source(channel, source) if source is not None and source < 120 else ()
            routes = tuple(target for target in routes if ae.runtime.channel_policy.destination(channel, target) is not None)
            function_destination = ae.runtime.channel_policy.destination(channel, key)
            no_effect = self._connection_no_effect(channel,key)
            unsupported_drum = ae.runtime.addresses.drums[channel] and (function_destination == "transpose" or (function_destination == "delay_send" and channel != 9))
            no_effect = no_effect or unsupported_drum
            function_mapped = ae.runtime.channel_policy.mapped(channel, key)
            if function_mapped and function_destination is not None:
                hint_target = tr(f"channel_editor.{function_destination}")
            else:
                hint_target = None
            set_channel_protocol_caption(field, badge, status=no_effect)
            field.cc_badge.set_theme(self.owner._dark)
            field.cc_status.set_text(tr("channel_editor.cc_no_effect_status") if no_effect else
                self._font_cc_presentations[source][0] if self._font_defined_cc(channel,key) else "")
            field.cc_status.set_tone("danger" if no_effect else None)
            field.cc_status.set_theme(self.owner._dark)
            if pending == (channel, key):
                suffix = tr("channel_editor.learning_status")
            elif binding:
                suffix = tr("channel_editor.cc_binding_hint", source=manual_cc_display(source),
                            target=tr(f"channel_editor.{destination}"),
                            scope=tr("channel_editor.cc_all_channels" if binding[3] else
                                     "channel_editor.cc_this_channel"))
                conversion = tr("channel_editor.cc_conversion_examples",
                                    low=channel_cc_target_value(destination, 0),
                                    middle=channel_cc_target_value(destination, 64),
                                    high=channel_cc_target_value(destination, 127))
                suffix = tr("channel_editor.cc_binding_details", binding=suffix,
                            conversion=conversion)
            elif no_effect:
                suffix = tr("channel_editor.drum_transpose_no_effect") if unsupported_drum else tr("channel_editor.cc_unmapped_hint")
            elif self._font_defined_cc(channel,key):
                suffix = tr("channel_editor.font_modulator_cc_tooltip",number=source,name=caption)
            elif key.startswith("aux_") and routes:
                suffix = tr("channel_editor.cc_source_routes_hint",
                            targets=", ".join(tr(f"channel_editor.{target}") for target in routes))
            else:
                suffix = tr("channel_editor.learn_hint")
            hint = tr("channel_editor.tooltip_with_cc_status", description=base, status=suffix) if base else suffix
            if self._font_defined_cc(channel,key) and pending != (channel,key):
                hint = self._font_cc_presentations[source][1]
            if function_mapped:
                hint = tr("channel_editor.tooltip_with_cc_status", description=hint,
                          status=tr("channel_editor.function_mapping_status",
                                    destination=hint_target or tr("channel_editor.function_none")))
            partial_effect = False
            mixed_source = False
            editable_name = bool(binding or key.startswith("aux_"))
            if self._global_scope():
                channels = self.selected_channels()
                bindings = [ae.runtime.midi_learn.binding(ch, key) for ch in channels]
                signatures = {(entry[:3] if entry else None,
                               ae.runtime.channel_policy.destination(ch, key),
                               ae.runtime.midi_learn.control_name(ch, key, caption))
                              for ch, entry in zip(channels,bindings)}
                badges = {f"CC#{entry[0]:03d}" if entry else (
                    f"CC#{CHANNEL_CC_DEFAULTS[key]:03d}" if key in CHANNEL_CC_DEFAULTS and
                    not (key == "delay_send" and not ae.runtime.midi_options.cc94_custom_delay_send)
                    else protocols.get(key, "")) for entry in bindings}
                mixed_source = len(badges) > 1
                badge = tr("channel_editor.mixed_value") if mixed_source else next(iter(badges))
                title, name_mixed = self._control_title(key, caption, channels)
                field.title.setText(title)
                field.title.setAccessibleName(title)
                editable_name = any(bindings) or key.startswith("aux_")
                if editable_name:
                    field.title.configure_text(
                        read=lambda key=key, caption=caption: self._common_control_name(key,caption),
                        write=lambda text, key=key: self._rename_cc(key,text),
                        context=self.expression_channel.currentIndex,label=caption)
                else: field.title.disable_entry()
                field.title.setProperty("mixedValue",name_mixed)
                without_effect = sum(self._connection_no_effect(ch,key) for ch in channels)
                no_effect = without_effect == len(channels)
                partial_effect = 0 < without_effect < len(channels)
                function_mapped = any(ae.runtime.channel_policy.mapped(ch,key) for ch in channels)
                binding = bindings[0] if all(entry == bindings[0] for entry in bindings) else None
                set_channel_protocol_caption(field,badge,status=no_effect)
                field.cc_status.set_text(tr("channel_editor.cc_no_effect_status") if no_effect else
                    tr("channel_editor.partial_effect_status") if partial_effect else
                    tr("channel_editor.cc_font_defined") if all(self._font_defined_cc(ch,key) for ch in channels) else "")
                field.cc_status.set_tone("danger" if no_effect or partial_effect else None)
                mixed_wiring = len(signatures) > 1
                if mixed_wiring:
                    hint = tr("channel_editor.tooltip_with_cc_status", description=base,
                              status=tr("channel_editor.mixed_mapping_hint"))
                if partial_effect:
                    hint = tr("channel_editor.tooltip_with_cc_status",description=hint,
                              status=tr("channel_editor.partial_effect_hint",
                                        count=without_effect,total=len(channels)))
            else:
                field.title.setProperty("mixedValue",False)
            widget.setToolTip(hint)
            if key in ("pitch", "channel_pressure") and binding is None:
                hint = tr("channel_editor.tooltip_with_cc_status", description=hint,
                          status=tr("channel_editor.pitch_signal_hint" if key == "pitch" else
                                    "channel_editor.aftertouch_signal_hint"))
            elif badge == "SysEx":
                hint = tr("channel_editor.tooltip_with_cc_status", description=hint,
                          status=tr("channel_editor.sysex_signal_hint"))
            install_effect_tooltip(hint, widget)
            current_name = field.title.text().replace("\n", " ")
            widget.setAccessibleName(current_name)
            if isinstance(field, NativeKnobField):
                field.value.setAccessibleName(tr("number_entry.accessible", control=current_name))
            field.title.setToolTip(tr("channel_editor.cc_name_hint", description=hint)
                                   if editable_name and not self._font_defined_cc(channel,key) else hint)
            field.cc_badge.setToolTip(hint)
            field.cc_status.setToolTip(tr("channel_editor.cc_unmapped_hint") if no_effect else
                                      tr("channel_editor.partial_effect_hint",count=without_effect,total=len(channels))
                                      if partial_effect else hint if self._font_defined_cc(channel,key) else "")
            if hasattr(field,'detail_caption'):
                status=field.cc_status._text
                if not status:
                    status=tr('channel_editor.cc_assigned_status') if binding or function_mapped or routes else tr('font_cc.unknown')
                font_footer=key in tuple(f'aux_{cc}' for cc in SOUNDFONT_MODULATOR_CC)
                if font_footer:
                    status=self._font_cc_presentations[source][0] if self._font_defined_cc(channel,key) else (
                        tr('font_cc.none') if no_effect else tr('font_cc.connected'))
                    set_channel_protocol_caption(field,'',status=False)
                field.set_detail_caption(status if font_footer else tr('channel_editor.cc_detail',source=badge,status=status),
                                         tooltip=field.cc_status.toolTip() or hint)
                field.cc_status.set_text('')
            field.setProperty("ccNoEffect", no_effect)
            widget.setProperty("ccNoEffect", no_effect)
            field.cc_badge.set_tone("danger" if no_effect else "accent" if not mixed_source and (binding or routes or function_mapped) else None)
            widget.update()
            field.setProperty("ccLearning", pending == (channel, key))
            field.style().unpolish(field)
            field.style().polish(field)

    def _rename_cc(self, key, name):
        caption = self._cc_fields[key][2].replace("\n", " ")
        for channel in self.selected_channels():
            # Enter on an unchanged built-in caption is not an alias edit.
            # In particular, layout-only newlines never become saved names.
            if name == caption and not ae.runtime.midi_learn.control_name(channel, key):
                continue
            ae.runtime.midi_learn.rename(channel, key, name)
        self._refresh_midi_learn_indicators()

    @ae.activity_observation
    def refresh_live_monitor(self, channel_values=None, activity=None, names=None):
        """Present existing live MIDI observations; never label them audio dB."""
        if not hasattr(self, "channel_mixer_controls"):
            return
        self._sync_channel_addresses()
        channel = self.selected_channel()
        if channel < 0:
            return
        global_scope = self._global_scope()
        if global_scope:
            now = time.monotonic()
            if now - getattr(self, "_global_observe_at", 0) < .03:
                return
            self._global_observe_at = now
        first = self.selected_channels()[0]
        if channel_values is None:
            channel_values = ae.channel_monitor_snapshot(channels=(first,))[first]
        if global_scope:
            # The caller's snapshot may be for its selected physical channel.
            channel_values = ae.channel_monitor_snapshot(channels=(first,))[first]
        if activity is None:
            activity = ae.get_channel_midi_activity()[first]
        if global_scope:
            activities = ae.get_channel_midi_activity()
            activity = (sum(row[0] for row in activities), max(row[1] for row in activities),
                        max(row[2] for row in activities))
        if names is None:
            names = self._channel_names
            if names is None:
                names = self.owner.soundfont_runtime.active_channel_names()
        self._channel_names = names
        code = (tuple((row["bank"], row["bank_lsb"], row["program"]) for row in ae.channel_monitor_snapshot())
                if global_scope else (channel_values["bank"], channel_values.get("bank_lsb", 0), channel_values["program"]))
        code=(code,getattr(self.owner,'_report_playback_snapshot',{}).get('midi_system_mode'))
        bridge=getattr(self.owner,'_effect_snapshots',None)
        if self._displayed_program != code and not (bridge is not None and bridge.pending('instrument')):
            self._displayed_program = code
            self._sync_instrument_selectors(channel_values)
        for kind, control in self.channel_mixer_controls.items():
            self._present_control(control, kind, channel_values.get(kind) if not global_scope else None)
        for number, control in self.auxiliary_cc_controls.items():
            if control.knob.isSliderDown() or getattr(control.value,'_editing',False):continue
            values = []
            for ch in self.selected_channels():
                binding = ae.runtime.midi_learn.binding(ch, f"aux_{number}")
                source=binding[0] if binding else number
                pending=self.owner.channel_command_presentation.pending_value(ch,f'aux_{source}')
                values.append(pending if pending is not None else ae.runtime.auxiliary_cc.source_value(ch,source))
            if len(set(values)) == 1 and control.get() != values[0]:
                control.set(values[0], False)
            control.set_mixed(len(set(values)) > 1)
        for field, switch in self.pedal_switches.items():
            self._present_switch(switch, self._switch_values(field))
        self._present_control(self.channel_pressure_control, "channel_pressure",
                              channel_values.get("channel_pressure") if not global_scope else None)
        self._refresh_poly_pressure()
        now = time.monotonic()
        if now - self._last_expression_observation >= .03:
            self._last_expression_observation = now
            self._refresh_expression()
        self._refresh_midi_learn_indicators()
        held, _velocity, _onset = activity
        visual=ae.get_channel_note_visual_state()
        strength=max((visual[ch] for ch in self.selected_channels()),default=0.) if global_scope else visual[first]
        self.channel_activity.set_activity(strength*127, held)
        # These values were requested on the existing observation worker.
        # Never replace an absent/stale frame with native reads from the GUI.
        frame=ae.current_channel_voice_observation(self.selected_channels())
        count=(sum(row.value for row in frame) if frame is not None and all(row.known for row in frame)
            else tr('channel_editor.unknown_value'))
        text=tr('channel_editor.active_counts',notes=held,voices=count)
        if self.channel_activity_text.text()!=text:self.channel_activity_text.setText(text)
        self._refresh_channel_audio_effects()

    def _poly_pressure_key_changed(self, _index=None):
        self.poly_pressure_control.value.cancel_editor()
        key = self.poly_pressure_note.currentData()
        if key is not None:
            ae.runtime.channel_policy.select_pressure_key(self.selected_channels(), key)
        self._refresh_poly_pressure()

    def _refresh_poly_pressure(self):
        control = self.poly_pressure_control
        if control.knob.isSliderDown() or control.value._editing:
            return
        keys = [ae.runtime.channel_policy.pressure_key(ch) for ch in self.selected_channels()]
        index = self.poly_pressure_note.findData(keys[0]) if len(set(keys)) == 1 else -1
        blocked = self.poly_pressure_note.blockSignals(True)
        self.poly_pressure_note.setPlaceholderText(tr("channel_editor.mixed_value"))
        self.poly_pressure_note.setCurrentIndex(index)
        self.poly_pressure_note.blockSignals(blocked)
        control.setEnabled(index >= 0)
        readbacks = self._control_readbacks("key_pressure")
        unknown = any(not readback.known for readback in readbacks)
        values = [readback.value for readback in readbacks]
        mixed = not unknown and (index < 0 or len(set(values)) > 1)
        if not mixed and not unknown and control.get() != values[0]:
            control.set(values[0], False)
        control.set_mixed(mixed)
        control.set_unknown(unknown)

    def _set_poly_pressure(self, value):
        if self.poly_pressure_note.currentData() is None:
            return
        self.owner.submit_channel_controls(self.selected_channels(),"key_pressure",value)

    def set_channel_names(self, names):
        self._channel_names = names
        channel = self.selected_channel()
        if 0 <= channel < ae.runtime.addresses.count and not self._global_scope():
            state = ae.channel_monitor_snapshot(channels=(channel,))[channel]
            bank, program = state["bank"], state["program"]
            if (bank, program) not in self._instrument_choices:
                index = self.instrument_program.findData(program)
                if index >= 0:
                    name = names[channel] if channel<len(names) else None
                    label = (tr("channel_editor.program_named", number=program, name=name)
                             if name else f"{program:03d}")
                    if self.instrument_program.itemText(index) != label:
                        self.instrument_program.setItemText(index, label)


    def _update_reset_buttons(self):
        self.expression_reset.setEnabled(True)
        self.channel_preview.setEnabled(not self._global_scope())

    def _reset_scope(self, scope):
        if scope != "expression":
            raise ValueError("channel editor reset scope")
        for channel in self.selected_channels():
            self.owner.reset_channel(channel)
        self.refresh_controls()


    def _set_expression(self, field, value):
        self.owner.submit_channel_controls(self.selected_channels(),field,value,persist=True)
        self._update_reset_buttons()

    def _refresh_expression(self, *_args):
        drums=all(ae.runtime.addresses.drums[ch] for ch in self.selected_channels())
        mode=getattr(self.owner,'_report_playback_snapshot',{}).get('midi_system_mode')
        if getattr(self,'_poly_key_drum_caption',None)!=(drums,mode):
            self._poly_key_drum_caption=drums,mode
            for key in range(128):self.poly_pressure_note.setItemText(key,drum_key_caption(key,mode=mode) if drums else tr('channel_editor.poly_pressure_key',key=key))
        for field, control in self.expression_controls.items():
            self._present_control(control, field)
        self._present_switch(self.portamento_switch,self._switch_values("portamento"))
        modes = [value.value if value.known else None for value in self._control_readbacks("mode")]
        states = {"mode_mono": [mode in (1, 2) if mode is not None else None for mode in modes],
                  "mode_poly": [mode == 0 if mode is not None else None for mode in modes],
                  "mode_legato": [mode == 2 if mode is not None else None for mode in modes]}
        for target, switch in self.mode_switches.items():
            self._present_switch(switch, states[target])


    def refresh_controls(self):
        self._refresh_expression()
        self._update_reset_buttons()
        self._refresh_channel_audio_effects()

    def showEvent(self, event):
        if self.content_stack.currentWidget() is self.expression_page:
            self.refresh_live_monitor()
        super().showEvent(event)
        if self._fit_channel_on_show and self.content_stack.currentWidget() is self.expression_page:
            self._fit_channel_on_show = False
            self._fit_channel_window(opening=True)

    def hideEvent(self, event):
        self._channel_window_motion.settle()
        for section in self.section_disclosures.values():
            section.disclosure.settle()
        self._finish_section_transition()
        ae.runtime.midi_learn.cancel()
        for _widget, field, _caption in self._cc_fields.values():
            field.title._finish_edit(True)
        super().hideEvent(event)


def choose_group_save_path(parent, group):
    """Save edits to an opened SFG; ask for a path only for new groups."""
    if group.source_path and Path(group.source_path).suffix.lower() == ".sfg":
        return group.source_path
    path, _ = QFileDialog.getSaveFileName(
        parent, tr("group.save_title"), group.source_path, "SoundFont Group (*.sfg)",
    )
    if path and not Path(path).suffix:
        path += ".sfg"
    return path


class NativeFontControllerIndex(QObject):
    """One bounded metadata reader; no audio handles or worker Qt calls."""
    changed=Signal()

    def __init__(self,parent):
        super().__init__(parent)
        self.cache={};self.pending={};self.results=queue.Queue();self.worker=None;self.revision=0
        self.timer=QTimer(self);self.timer.setInterval(50);self.timer.timeout.connect(self._collect)

    def request(self,path,revision):
        key=(os.path.normcase(os.path.abspath(str(path))),revision)
        if key in self.cache:return self.cache[key]
        if key not in self.pending:self.pending[key]=None
        self._start();return None

    def _start(self):
        if self.worker is not None or not self.pending:return
        key=next(iter(self.pending));out=self.results
        def read():
            try:result=inspect_soundfont_controllers(key[0])
            except Exception:result=dict(state='error',presets={})
            out.put((key,result))
        self.worker=threading.Thread(target=read,name='SoundFont-controller-inspection',daemon=True)
        self.worker.start();self.timer.start()

    def _collect(self):
        changed=False
        while True:
            try:key,result=self.results.get_nowait()
            except queue.Empty:break
            self.pending.pop(key,None);self.cache[key]=result;changed=True
            # Do not evict a currently displayed file just to meet a font-count
            # guess: all-channel inspection could otherwise re-read forever.
            # Each report has explicit metadata/link bounds; no PCM is cached.
        if self.worker is not None and not self.worker.is_alive():self.worker=None
        if changed:self.revision+=1;self.changed.emit()
        self._start()
        if self.worker is None:self.timer.stop()


def font_controller_index(owner):
    index=getattr(owner,'_font_controller_index',None)
    if index is None:owner._font_controller_index=index=NativeFontControllerIndex(owner)
    return index


def font_controller_has_links(report,bank,preset):
    """None means unverified,not an absent connection."""
    if report is None or report.get('state')!='ready':return None
    links=report.get('presets',{}).get((bank,preset))
    return None if links is None else bool(links)


def font_controller_text(report,bank,preset,cc):
    """One concise file-connection summary for the inspector and tooltip.

    Coalesce duplicate SF2 zones by destination. Never infer BASS's winner
    among incompatible destinations or claim unsupported file targets sound.
    Raw records remain in the metadata index,not the presentation string.
    """
    if report is None:return tr('font_cc.reading'),tr('font_cc.reading_detail')
    if report['state']!='ready':return tr('font_cc.unknown'),tr('font_cc.state_'+report['state'])
    if (bank,preset) not in report['presets']:return tr('font_cc.unknown'),tr('font_cc.preset_missing')
    links=[link for link in report['presets'][(bank,preset)] if link.cc==cc]
    if not links:return tr('font_cc.none'),tr('font_cc.none_detail',cc=cc)
    lines=[tr('font_cc.connection_heading',cc=cc)]
    for destination in dict.fromkeys(link.destination for link in links):
        rows=[link for link in links if link.destination==destination]
        name=(tr('font_cc.target_'+str(destination)) if destination in (5,6,7,8,9,10,11,13,15,16,17,21,22,23,24,25,26,27,28,29,30,33,34,35,36,37,38,48,51,52)
              else SF2_CC_GENERATOR_NAMES.get(destination,tr('font_cc.generator',number=destination)))
        if destination not in (8,48,34,36,38,30,11):
            reaction=tr('font_cc.connection_unsupported')
        elif all(not link.amount for link in rows):reaction=tr('font_cc.connection_zero')
        else:
            signs={1 if link.amount*(-1 if link.source&0x100 else 1)>0 else -1 for link in rows if link.amount}
            change='varies' if len(signs)>1 else 'up' if signs=={1} else 'down'
            if destination==48 and change!='varies':change='down' if change=='up' else 'up'
            reaction=tr('font_cc.connection_'+change)
        lines.append(tr('font_cc.connection_target',target=name,reaction=reaction))
    active=[link for link in links if link.destination in (8,48,34,36,38,30,11) and link.amount]
    if len({link.destination for link in active})>1:lines.append(tr('font_cc.connection_one_target'))
    if len({link.source for link in active})>1:lines.append(tr('font_cc.connection_curve_choice'))
    if any(link.secondary for link in links):lines.append(tr('font_cc.connection_secondary'))
    if any(link.qualification=='curve' for link in links):lines.append(tr('font_cc.connection_curve_unknown'))
    ranges=sorted({link.keys for link in links});merged=[]
    for low,high in ranges:
        if merged and low<=merged[-1][1]+1:merged[-1]=(merged[-1][0],max(merged[-1][1],high))
        else:merged.append((low,high))
    if merged!=[(0,127)]:
        def note_label(note):return f"{('C','C♯','D','D♯','E','F','F♯','G','G♯','A','A♯','B')[note%12]}{note//12-1}"
        # Keep gaps explicit. Many disjoint ranges use a bounded count rather
        # than a false min..max span or another long zone dump.
        if len(merged)<=3:
            labels=[]
            for low,high in merged:
                labels.append(note_label(low) if low==high else tr('font_cc.connection_note_range',low=note_label(low),high=note_label(high)))
            lines.append(tr('font_cc.connection_keys',ranges=', '.join(labels)))
        else:lines.append(tr('font_cc.connection_regions',count=len(merged)))
    if any(link.velocities!=(0,127) for link in links):lines.append(tr('font_cc.connection_velocity'))
    return tr('font_cc.connected'),'\n'.join(lines)

class NativeSoundFontLoadWorker(QThread):
    """Run the existing transactional BASSMIDI load without blocking Qt paint."""

    stage_changed = Signal(str, object, object, str)
    retryNeeded = Signal(str)
    recoveryStarted = Signal()

    def __init__(self, runtime, group, load_lock, parent=None, *, source_recovery=None):
        super().__init__(parent)
        self.runtime = runtime
        self.group = group
        self.load_lock = load_lock
        self.result = None
        self.error = None
        self.cancel_requested = threading.Event()
        self.committing = threading.Event()
        if source_recovery is not None and not callable(source_recovery):raise ValueError('SoundFont source recovery')
        self.source_recovery=source_recovery
        self.retry_requested=threading.Event()
        self.recovery_pending=threading.Event()

    def run(self):
        try:
            with self.load_lock:
                stream = self.runtime.stream_handle()
                device = self.runtime.bass.BASS_ChannelGetDevice(stream)
                # bass.md: current device belongs to the calling thread.
                # A device-less decode stream needs no device selection.
                if device not in (0xFFFFFFFF, 0x20000):
                    if not self.runtime.bass.BASS_SetDevice(device):
                        raise RuntimeError(tr("runtime.soundfont_device_failed"))
                def report_stage(stage, number, total, filename):
                    if stage == "mapping":
                        self.committing.set()
                    self.stage_changed.emit(stage, number, total, filename)

                self.result = self.runtime.load_group(
                    self.group, progress=report_stage, cancel=self.cancel_requested,
                )
        except Exception as exc:
            self.error = exc
            if self.source_recovery is not None:
                # Keep this QThread's native owner alive until a confirmed
                # release. Recovery never calls load_group or submits audio.
                while True:
                    self.recoveryStarted.emit()
                    try:
                        safe_to_exit,owners=self.source_recovery()
                        if type(safe_to_exit) is not bool or not isinstance(owners,tuple):raise ValueError('SoundFont source recovery result')
                    except Exception as recovery_error:
                        # An unknown recovery failure cannot certify that this
                        # owning thread is safe to destroy. Preserve its root.
                        safe_to_exit=False;owners=(threading.get_native_id(),)
                        detail=str(recovery_error)
                    else:detail=str(self.error)
                    if safe_to_exit:
                        self.recovery_pending.clear()
                        break
                    self.committing.set();self.recovery_pending.set()
                    self.retry_requested.clear()
                    self.retryNeeded.emit(detail)
                    self.retry_requested.wait()


class NativeSoundFontLoadDialog(NativeDialog):
    """Determinate stage/file progress, with measured bytes for known samples."""

    cancelRequested = Signal()
    retryRequested = Signal()

    def __init__(self, owner):
        super().__init__(owner)
        self._worker_finished = False
        self._cancelling = False
        self._recovery_pending=False
        self.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
        self.setWindowModality(Qt.ApplicationModal)
        self.setWindowTitle(tr("soundfont_loading.title"))
        self.setFont(owner.font())
        self.resize(420, 148)
        self.setMinimumSize(0, 0)
        layout = set_layout(QVBoxLayout(self), METRICS["outer"], METRICS["section_gap"])
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        title = QLabel(tr("soundfont_loading.title"))
        make_horizontally_elastic(title)
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        self.stage_label = QLabel(tr("soundfont_loading.starting"))
        self.stage_label.setWordWrap(True)
        make_horizontally_elastic(self.stage_label, QSizePolicy.Preferred)
        stage_policy = self.stage_label.sizePolicy()
        stage_policy.setHeightForWidth(True)
        self.stage_label.setSizePolicy(stage_policy)
        stage_row = set_layout(QHBoxLayout(), 0, METRICS["compact_content_gap"])
        stage_row.addWidget(self.stage_label, 1)
        self.progress_percent = ElasticLabel(tr("soundfont_loading.percent", value=0),
            preferred_width=self.fontMetrics().horizontalAdvance(tr('soundfont_loading.percent',value=100)))
        self.progress_percent.setObjectName("MutedLabel")
        self.progress_percent.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        stage_row.addWidget(self.progress_percent)
        layout.addLayout(stage_row)
        self.progress_bar = QProgressBar()
        make_horizontally_elastic(self.progress_bar)
        self.progress_bar.setObjectName("SoundFontLoadProgress")
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)
        commands = set_layout(QHBoxLayout(), 0, 0)
        commands.addStretch(1)
        self.cancel_button = make_action_button(tr("common.cancel"), self.request_cancel)
        commands.addWidget(self.cancel_button)
        layout.addLayout(commands)

    def showEvent(self, event):
        fit_plain_dialog_contents(self,420,opening=True)
        super().showEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Reflow only when the available text width changes. Error details
        # can wrap beyond the prior height after horizontal compression;
        # percentage updates and the resulting height resize do not refit.
        if (self.isVisible() and event.size().width()!=event.oldSize().width()
                and not getattr(self,'_width_refitting',False)):
            self._width_refitting=True
            try:fit_plain_dialog_contents(self)
            finally:self._width_refitting=False

    def _set_task_text(self, text):
        if text==self.stage_label.text(): return
        self.stage_label.setText(text)
        # Only real stage/file text changes can change wrapping. Percentage
        # updates keep the same preferred100% cell and do not trigger layout I/O.
        if self.isVisible(): fit_plain_dialog_contents(self)

    def request_cancel(self):
        if self._worker_finished or not self.cancel_button.isEnabled():
            return
        if self._recovery_pending:
            self.cancel_button.setEnabled(False)
            self._set_task_text(tr('soundfont_loading.recovery_running'))
            self.retryRequested.emit()
            return
        if getattr(self, "worker", None) is not None and self.worker.committing.is_set():
            self.cancel_button.setEnabled(False)
            return
        self.cancel_button.setEnabled(False)
        self._cancelling = True
        self._set_task_text(tr("soundfont_loading.cancelling"))
        self.cancelRequested.emit()

    def set_recovery_pending(self,error):
        self._recovery_pending=True
        self._cancelling=False
        self.cancel_button.setText(tr('soundfont_loading.recovery_retry'))
        self.cancel_button.setEnabled(True)
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus(Qt.OtherFocusReason)
        self.progress_bar.setValue(min(self.progress_bar.value(),950))
        self.progress_percent.setText(tr('soundfont_loading.percent',value=round(self.progress_bar.value()/10)))
        self._set_task_text(tr('soundfont_loading.recovery_pending',error=error))

    def set_recovery_running(self):
        self.cancel_button.setEnabled(False)
        self._set_task_text(tr('soundfont_loading.recovery_running'))

    def set_stage(self, stage, number, total, filename):
        if self._recovery_pending:return
        label = Path(filename).name if filename else ""
        if stage == "font":
            message = tr("soundfont_loading.font", number=number, total=total, filename=label)
            self.progress_bar.setValue(round(150 * (number - 1) / max(1, total)))
        elif stage == "font_done":
            message = tr("soundfont_loading.font", number=number, total=total, filename=label)
            self.progress_bar.setValue(round(150 * number / max(1, total)))
        elif stage == "samples":
            message = tr("soundfont_loading.samples", number=number, total=total, filename=label)
        elif stage == "sample_bytes":
            message = tr("soundfont_loading.samples_progress", filename=label)
            self.progress_bar.setValue(150 + round(800 * number / max(1, total)))
        elif stage == "used_samples":
            message = tr("soundfont_loading.used_samples")
        elif stage == "used_samples_done":
            message = tr("soundfont_loading.used_samples")
            self.progress_bar.setValue(150 + round(800 * number / max(1, total)))
        elif stage == "mapping":
            message = tr("soundfont_loading.mapping")
            self.progress_bar.setValue(950)
            self.cancel_button.setEnabled(False)
        else:
            message = tr("soundfont_loading.starting")
        if not self._cancelling:
            self._set_task_text(message)
        self.progress_percent.setText(tr("soundfont_loading.percent", value=round(self.progress_bar.value() / 10)))

    def finish(self):
        worker=getattr(self,'worker',None)
        if worker is not None and worker.isRunning():return
        self._worker_finished = True
        if getattr(self, "worker", None) is None or self.worker.error is None:
            self.progress_bar.setValue(1000)
            self.progress_percent.setText(tr("soundfont_loading.percent", value=100))
        self.accept()

    def reject(self):
        if self._worker_finished:
            super().reject()

    def closeEvent(self, event):
        if not self._worker_finished:
            event.ignore()
            return
        super().closeEvent(event)


class NativeOutputDiagnosticsPanel(QWidget):
    """Visible-tab-only native diagnostics; recording has a separate owner."""

    def __init__(self, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.analysis = ae.runtime.output_analysis
        self._measurement_stream = 0
        root = set_layout(QVBoxLayout(self), 0, METRICS["section_gap"])
        root.setSizeConstraint(QLayout.SetNoConstraint)
        self.measure_button = make_action_button(tr('output_tools.measure_reset'),self.reset_measurement)
        self.readouts = {}
        self._readout_cards=[]
        for title,names in (('preferences.diagnostic_processing_title',('source_cpu','output_cpu','voices')),
                            ('preferences.diagnostic_loudness_title',('momentary','short_term','integrated','range','peak','true_peak'))):
            card=NativeCard();make_horizontally_elastic(card,QSizePolicy.Fixed)
            content=set_layout(QGridLayout(card),METRICS['card_padding'],METRICS['compact_content_gap'])
            content.setSizeConstraint(QLayout.SetNoConstraint)
            content.setHorizontalSpacing(METRICS['section_gap'])
            header=control_row_layout();label=ElasticLabel(tr(title));label.setObjectName('SectionTitle');header.addWidget(label,1)
            if 'momentary' in names:header.addWidget(self.measure_button)
            content.addLayout(header,0,0,1,2)
            split=(len(names)+1)//2;columns=[];widths=[]
            for index,group in enumerate((names[:split],names[split:])):
                column=QWidget();make_horizontally_elastic(column,QSizePolicy.Preferred)
                grid=set_layout(QGridLayout(column),0,2);grid.setSizeConstraint(QLayout.SetNoConstraint)
                preferred=0
                for row,name in enumerate(group):
                    label=ElasticLabel(tr('output_tools.'+name));value=ElasticLabel('—')
                    label.setFixedHeight(24);value.setFixedHeight(24)
                    label.setMinimumWidth(0);label.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Fixed)
                    make_horizontally_elastic(value)
                    value.setAlignment(Qt.AlignRight|Qt.AlignVCenter);self.readouts[name]=value
                    grid.addWidget(label,row,0);grid.addWidget(value,row,1)
                    preferred=max(preferred,label.fontMetrics().horizontalAdvance(label.text())+value.fontMetrics().horizontalAdvance('−100.0 dBFS')+METRICS['form_column_gap'])
                grid.setColumnStretch(1,1);columns.append(column);widths.append(preferred)
                content.addWidget(column,1,index,Qt.AlignTop)
            self._readout_cards.append((card,columns,widths));card.installEventFilter(self)
            reflow_card_columns(card,columns,widths);root.addWidget(card)
        self.status = ElasticLabel(tr("output_tools.scope"))
        self.status.setWordWrap(True)
        # Measurement scope belongs to its measurement card,not between cards.
        self._readout_cards[-1][0].layout().addWidget(self.status,2,0,1,2)
        root.addStretch(1)
        apply_native_text_metrics(self)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)

    def minimumSizeHint(self):
        return QSize(0, 0)

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Resize:
            for card,columns,widths in self._readout_cards:
                if watched is card:reflow_card_columns(card,columns,widths)
        return super().eventFilter(watched,event)

    def reset_measurement(self):
        self.analysis.stop_measurement()
        self._measurement_stream = self.analysis.stream
        for name in self.analysis.MODES:
            self.readouts[name].setText("—")
        self.status.setText(tr("output_tools.scope"))
        try:
            self.analysis.start_measurement()
        except (OSError, RuntimeError) as exc:
            self.status.setText(str(exc))

    def refresh(self):
        if self.analysis.stream != self._measurement_stream:
            self.reset_measurement()
        state = self.owner.audio_session.diagnostic_status(self.owner._asio_dll)
        for name in ("source_cpu", "voices", "output_cpu"):
            value = state.get(name)
            self.readouts[name].setText((str(int(value)) if name == "voices" else f"{value:.1f} %") if value is not None else "—")
        for name, value in self.analysis.levels().items():
            if name in ("peak", "true_peak"):
                value = 20 * math.log10(value) if value > 0 else -math.inf
            unit = "LU" if name == "range" else "dBFS" if name in ("peak", "true_peak") else "LUFS"
            self.readouts[name].setText(f"{value:.1f} {unit}" if math.isfinite(value) else f"−∞ {unit}")
        self.measure_button.setEnabled(bool(self.analysis.stream))

    def showEvent(self, event):
        super().showEvent(event)
        self.status.setText(tr("output_tools.scope"))
        self.reset_measurement()
        self.refresh()
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        self.analysis.stop_measurement()
        super().hideEvent(event)


class NativeRecordingSaveWorker(QThread):
    """One control-thread job; native audio never waits for file copying."""

    def __init__(self, analysis, path, parent=None):
        super().__init__(parent)
        self.analysis, self.path = analysis, path
        self.error = None

    def run(self):
        try:
            self.analysis.save_recording(self.path)
        except Exception as exc:
            self.error = str(exc)


class NativeMidiFileLoadWorker(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path

    def run(self):
        from midi_player import MidiDocument
        try:
            document = MidiDocument.load(self.path)
            if not self.isInterruptionRequested():
                self.loaded.emit(document)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))


class NativeRecordingQualitySlider(QWidget):
    """Shared integer codec control with the number-box value/signal contract."""
    valueChanged=Signal(int)

    def __init__(self,minimum,maximum,caption,default):
        super().__init__()
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
        body=set_layout(QHBoxLayout(self),0,0)
        self.control=NativeControlRow('',minimum,maximum,default,
            lambda value:self.valueChanged.emit(round(value)),lambda value:str(round(value)),
            input_steps=(1,2,3),label_width=0,value_width=32,
            entry_kind='number_integer',neutral_value=default)
        self.control.title.hide()
        self.control.slider.setAccessibleName(caption)
        self.setAccessibleName(caption)
        body.addWidget(self.control)

    def value(self):return round(self.control.get())

    def setValue(self,value):self.control.set(value)


def sync_recording_formats(field,container):
    """Only offer usable sample formats; no disabled sentinel in the popup."""
    formats=('pcm32','pcm24','pcm16') if container in ('flac','ogg_flac') else ('float32','pcm32','pcm24','pcm16')
    if tuple(field.itemData(index) for index in range(field.count()))==formats:return
    current=field.currentData()
    blocked=field.blockSignals(True)
    try:
        field.clear()
        for value in formats:field.addItem(tr('recording.format_'+value),value)
        field.setCurrentIndex(field.findData(current if current in formats else 'pcm24'))
    finally:field.blockSignals(blocked)


def make_recording_quality_fields():
    """One control/domain factory for recording preferences and MIDI export."""
    fields = {}
    for name, values, namespace in (
        ("recording_format", ("float32", "pcm32", "pcm24", "pcm16"), "recording.format_"),
        ("recording_container", tuple(RECORDING_EXTENSIONS), "recording.container_"),
        ("recording_rate_mode", ("vbr", "cbr"), "recording.rate_"),
    ):
        combo = AnimatedComboBox()
        for value in values:
            combo.addItem(tr(namespace+value),value)
        fields[name] = combo
    fields["recording_bitrate"] = AnimatedComboBox()
    for value in RECORDING_BITRATES:
        fields["recording_bitrate"].addItem(tr("recording.bitrate_value",value=value),value)
    for name in ("recording_dither", "recording_rf64"):
        fields[name] = NativeToggleSwitch("")
    for name, low, high in (("recording_compression",0,8), ("recording_vorbis_quality",-1,10),
                            ("recording_mp3_quality",0,9), ("recording_opus_complexity",0,10)):
        fields[name] = NativeRecordingQualitySlider(low,high,tr("preferences."+name),
            EnginePreferences.__dataclass_fields__[name].default)
    return fields


def set_quality_fields(fields, preferences):
    sync_recording_formats(fields['recording_format'],preferences.recording_container)
    for name, widget in fields.items():
        value = getattr(preferences,name)
        if isinstance(widget, AnimatedComboBox):
            index=widget.findData(value)
            widget.setCurrentIndex(index if index>=0 else widget.findData('pcm24'))
        elif isinstance(widget,QCheckBox):
            widget.setChecked(value)
        else:
            widget.setValue(value)


def quality_field_values(fields):
    return {name: (widget.currentData() if isinstance(widget,AnimatedComboBox)
                   else widget.isChecked() if isinstance(widget,QCheckBox) else widget.value())
            for name,widget in fields.items()}


def midi_form_row(caption, control, tooltip=""):
    row=QWidget()
    row.setMinimumWidth(0)
    grid=set_layout(QGridLayout(row),0,METRICS["form_column_gap"])
    label=ElasticLabel(caption,preferred_width=METRICS["form_label_width"])
    label.setObjectName("FieldLabel")
    label.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
    control.setMinimumWidth(0)
    control.setAccessibleName(caption)
    control.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
    grid.addWidget(label,0,0); grid.addWidget(control,0,1)
    grid.setColumnStretch(1,1)
    row.setFixedHeight(FORM_ROW_HEIGHT)
    if tooltip:
        control.setAccessibleDescription(tooltip)
        row.setToolTip(tooltip); control.setToolTip(tooltip)
    return row


def midi_adjustment_row(kind, initial, callback=None):
    """Reuse the app-wide slider/inline-value interaction and number domains."""
    speed = kind=='speed'
    return NativeControlRow(tr('player.'+kind),25 if speed else -24,
        400 if speed else 24,initial,callback,
        lambda value: format_effect_value(value,'percent' if speed else 'semitones'),
        input_steps=(1,5,10) if speed else (1,2,4),
        entry_kind='percent' if speed else 'signed_semitones',
        neutral_value=100 if speed else 0,tooltip=tr('player.'+kind+'_tip'))


def midi_preferred_width(host, control):
    # One normal form grid: existing preferred columns and a font-measured
    # useful track. It remains a preference, never a horizontal size floor.
    return (2*METRICS['outer']+2*METRICS['card_padding']+control.label_width
            +control.value_width+2*control.column_gap
            +QFontMetrics(host.font()).horizontalAdvance('0'*20))


class ExportRecoveryWorker(QThread):
    resultReady = Signal(str, object, str)

    def __init__(self, parent, operation, journal=None, destination=None):
        super().__init__(parent)
        self.operation, self.journal, self.destination = operation, journal, destination
        self._published_job=None

    def run(self):
        from midi_player import discover_export_checkpoints, recover_export_checkpoint, restart_export_checkpoint
        try:
            value = (discover_export_checkpoints(self.isInterruptionRequested) if self.operation == 'scan'
                     else restart_export_checkpoint(self.journal,self.destination,self.isInterruptionRequested) if self.operation=='restart'
                     else recover_export_checkpoint(self.journal,self.destination,self.isInterruptionRequested))
            if self.operation=='restart' and self.isInterruptionRequested():
                value.close(); raise RuntimeError(tr('player.export_cancelled'))
            if self.operation=='restart': self._published_job=value
            self.resultReady.emit(self.operation,value,'')
        except Exception as exc:
            self.resultReady.emit(self.operation,None,str(exc) or tr('player.recovery_invalid'))


class NativeMidiExportPanel(NativeCard):
    """Persistent inline export task; hides detail without changing its values."""
    renderRequested = Signal(object)
    def __init__(self, player):
        super().__init__(player)
        from settings import load_export_preferences
        self.player=player
        self.document=player.document
        self.preferences=load_export_preferences(player.owner._engine_preferences)
        self.result_options=None
        self._disclosure_baseline=None
        self._pending_extent=False
        self._disclosure_setup=False
        self._recoveries=[]; self._recovery_worker=None; self._recovery_scan_pending=False
        self._recovery_stopping=False
        self.setMinimumSize(0,0)
        root=set_layout(QVBoxLayout(self),METRICS["card_padding"],METRICS["compact_content_gap"])
        header=control_row_layout()
        header.addWidget(ElasticLabel(tr('player.export_heading')),1)
        root.addLayout(header)
        self.detail_section,_grid=make_control_section(tr('player.export_details'),
            tr('player.export_details_tip'),collapsible=True)
        self.details=self.detail_section.disclosure_button
        self.details.setChecked(False)
        self.disclosure=self.detail_section.disclosure
        body=self.disclosure.content_layout
        def card(key):
            panel=QWidget(); panel.setMinimumWidth(0)
            layout=set_layout(QVBoxLayout(panel),0,METRICS["compact_content_gap"])
            line=QFrame(); line.setObjectName('HorizontalDivider')
            line.setFrameShape(QFrame.NoFrame); line.setFixedHeight(1); line.setMinimumWidth(0)
            layout.addWidget(line)
            heading=ElasticLabel(tr(key)); heading.setObjectName("SectionTitle")
            layout.addWidget(heading); body.addWidget(panel)
            return layout
        file_body=root
        self.filename=QLineEdit()
        self.filename.setMinimumWidth(0)
        self.filename.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
        self.filename.setAccessibleName(tr("player.export_destination"))
        filename_row=QWidget(); filename_layout=set_layout(QHBoxLayout(filename_row),0,METRICS["compact_content_gap"])
        filename_layout.addWidget(self.filename,1)
        self.browse_button=make_action_button(tr("recording.browse"),self.choose_destination)
        filename_layout.addWidget(self.browse_button)
        file_body.addWidget(filename_row)
        self.quality=make_recording_quality_fields()
        set_quality_fields(self.quality,self.preferences)
        self.container=self.quality["recording_container"]
        file_body.addWidget(midi_form_row(tr("player.format"),self.container))
        self.filename.setText(str(Path(self.document.path).with_suffix("."+RECORDING_EXTENSIONS[self.container.currentData()])) if self.document else "")
        self.filename.setToolTip(self.filename.text())
        root.addWidget(self.detail_section)
        audio=card("player.export_audio_title")
        self.sample_rate=AnimatedComboBox()
        for rate in SAMPLE_RATES:
            self.sample_rate.addItem(sample_rate_caption(rate),rate)
        self.sample_rate.setCurrentIndex(self.sample_rate.findData(self.preferences.sample_rate))
        audio.addWidget(midi_form_row(tr("preferences.sample_rate"),self.sample_rate))
        self.audio_channels=AnimatedComboBox()
        self.audio_channels.addItem(tr("player.stereo"),False); self.audio_channels.addItem(tr("player.mono"),True)
        audio.addWidget(midi_form_row(tr("player.export_channels"),self.audio_channels))
        self.quality_rows={}
        for name,field in self.quality.items():
            if name == "recording_container":
                continue
            row=midi_form_row(tr("preferences."+name),field,tr("preferences."+name+"_detail"))
            self.quality_rows[name]=row; audio.addWidget(row)
        rendering=card("player.export_render_title")
        self.range_mode=AnimatedComboBox()
        self.range_mode.addItem(tr("player.export_range_all"),False); self.range_mode.addItem(tr("player.export_range_custom"),True)
        rendering.addWidget(midi_form_row(tr("player.export_range"),self.range_mode))
        duration=self.document.duration if self.document else 0
        self.start=NativeDecimalBox(0,duration,tr("player.export_start"),suffix=" s")
        self.end=NativeDecimalBox(0,duration,tr("player.export_end"),suffix=" s")
        self.end.setValue(duration)
        self.start_row=midi_form_row(tr("player.export_start"),self.start)
        self.end_row=midi_form_row(tr("player.export_end"),self.end)
        rendering.addWidget(self.start_row); rendering.addWidget(self.end_row)
        # Same-window export follows the visible playback adjustments. Do not
        # create a second unexplained copy with divergent speed/pitch values.
        self.speed=player.speed
        self.transpose=player.transpose
        self.tail=NativeControlRow(tr('player.tail'),0,30,3,lambda _value:self.refresh(),
            lambda value:f'{value:.2f} s',step=.01,input_steps=(.01,.1,1),
            entry_kind='seconds_decimal',tooltip=tr('player.tail_tip'))
        self.normalize=NativeToggleSwitch("")
        self.align_processing=NativeToggleSwitch("")
        self.peak=NativeControlRow(tr('player.export_peak'),-12,0,-1,None,
            lambda value:f'{value:.1f} dBFS'.replace('-','\u2212'),step=.1,
            input_steps=(.1,.2,1),entry_kind='db_decimal',tooltip=tr('player.export_normalize_tip'))
        rendering.addWidget(self.tail)
        rendering.addWidget(midi_form_row(tr('player.align_processing'),self.align_processing,
                                         tr('player.align_processing_tip')))
        self.alignment_hint=ElasticLabel(); self.alignment_hint.setWordWrap(True)
        rendering.addWidget(self.alignment_hint)
        for key,field,tip in (("export_normalize",self.normalize,"export_normalize_tip"),):
            rendering.addWidget(midi_form_row(tr("player."+key),field,tr("player."+tip)))
        rendering.addWidget(self.peak)
        self.info=QLabel(); self.info.setWordWrap(True); self.info.setMinimumWidth(0)
        self.info.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        root.addWidget(self.info)
        self.error=QLabel(); self.error.setWordWrap(True); self.error.setMinimumWidth(0)
        root.addWidget(self.error); self.error.hide()
        self.recovery_row=QWidget(); recovery_layout=set_layout(QVBoxLayout(self.recovery_row),0,METRICS['compact_content_gap'])
        self.recovery_row.setMinimumSize(0,0); self.recovery_row.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        recovery_layout.setSizeConstraint(QLayout.SetNoConstraint)
        self.recovery_label=ElasticLabel(); recovery_layout.addWidget(self.recovery_label)
        recovery_actions=control_row_layout()
        recovery_actions.setSizeConstraint(QLayout.SetNoConstraint)
        self.recovery_choice=AnimatedComboBox(); self.recovery_choice.setMinimumWidth(0)
        self.recovery_choice.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
        recovery_actions.addWidget(self.recovery_choice,1)
        self.recovery_button=make_action_button(tr('player.recovery_save'),self.recover_audio)
        self.recovery_choice.currentIndexChanged.connect(self._recovery_selection)
        recovery_actions.addWidget(self.recovery_button); recovery_layout.addLayout(recovery_actions)
        root.addWidget(self.recovery_row); self.recovery_row.hide()
        self.task=QWidget(); self.task.setMinimumWidth(0)
        task_body=set_layout(QVBoxLayout(self.task),0,METRICS['compact_content_gap'])
        self.task_identity=ElasticLabel(); task_body.addWidget(self.task_identity)
        self.task_stage=ElasticLabel(); task_body.addWidget(self.task_stage)
        self.progress=QProgressBar(); self.progress.setObjectName('TaskProgress')
        self.progress.setMinimumWidth(0); self.progress.setTextVisible(False)
        self.progress.setAccessibleName(tr('player.export_progress'))
        self.progress.setToolTip(tr('player.export_progress_tip'))
        task_body.addWidget(self.progress)
        self.task_time=ElasticLabel(); task_body.addWidget(self.task_time)
        self.task.hide(); root.addWidget(self.task)
        footer=control_row_layout()
        self.folder_button=make_action_button(tr('player.export_open_folder'),self.open_export_folder)
        self.folder_button.hide(); footer.addWidget(self.folder_button)
        self.cancel_button=make_action_button(tr('common.cancel'),player.cancel_render)
        self.cancel_button.hide(); footer.addWidget(self.cancel_button)
        footer.addStretch(1)
        self.export_button=make_action_button(tr("player.export_begin"),self.confirm)
        self.export_button.setObjectName("PrimaryButton")
        self.export_button.setAutoDefault(False)
        footer.addWidget(self.export_button)
        root.addLayout(footer)
        self._job_editables=[self.filename,self.browse_button,self.container,self.sample_rate,
                            self.audio_channels,self.range_mode,self.start,self.end,self.tail,self.normalize,self.peak,self.align_processing,
                            *self.quality.values()]
        self._task_state='idle'; self._task_started=None; self._phase_started=None
        self._task_progress=None; self._task_destination=None; self._phase_rate=None
        self._last_rate_sample=None; self._last_time_text=None
        for field in (self.container,self.quality['recording_format'],self.quality['recording_rate_mode'],self.sample_rate,self.audio_channels,self.range_mode):
            field.currentIndexChanged.connect(self.refresh)
        for field in (self.start,self.end):
            field.valueChanged.connect(self.refresh)
        self.speed.slider.valueChanged.connect(self.refresh)
        self.transpose.slider.valueChanged.connect(self.refresh)
        self.normalize.toggled.connect(self.refresh)
        self.disclosure.bind_control(self.details,self._set_expanded)
        self.disclosure.heightChanged.connect(self._disclosure_changed)
        for check in self.findChildren(OutlineCheckBox):
            check.set_theme(widget_palette(player.owner))
        apply_native_text_metrics(self)
        align_form_labels((label for label in self.findChildren(ElasticLabel)
                           if label.objectName() == 'FieldLabel'),
                          METRICS['form_label_width'], METRICS['form_column_gap'])
        self.disclosure.set_expanded(False)
        self.refresh()
        QTimer.singleShot(0,self.scan_recovery)

    def begin_export(self,source,destination):
        self._task_started=time.monotonic(); self._task_destination=Path(destination)
        self._last_time_text=None
        self.task_stage.setProperty('exportResult','running')
        self.task_stage.style().unpolish(self.task_stage); self.task_stage.style().polish(self.task_stage)
        identity=tr('player.export_job_identity',source=Path(source).name,filename=Path(destination).name)
        self.task_identity.setText(identity)
        self.task_identity.setToolTip(tr('player.export_job_paths',source=source,destination=destination))
        self.error.clear(); self.error.hide()
        self.folder_button.hide(); self.cancel_button.show(); self.cancel_button.setEnabled(True)
        self.export_button.setText(tr('player.export_running')); self.export_button.setEnabled(False)
        self.export_button.setProperty('exportBusy',True)
        self.export_button.style().unpolish(self.export_button); self.export_button.style().polish(self.export_button)
        for field in self._job_editables:
            field.setEnabled(False)
        self.task.show(); self.progress.show()
        self.update_export_phase(dict(stage='prepare',percent=None))
        self.update_source_availability()
        self._fit_content()

    def update_source_availability(self):
        available=self.document is not None and self.player.export_job is None and self._task_state in ('idle','done','cancelled','failed')
        for field in self._job_editables:
            field.setEnabled(available)
        for row in (self.start_row,self.end_row):
            row.setEnabled(available and bool(self.range_mode.currentData()))
        self.peak.setEnabled(available and self.normalize.isChecked())
        self.align_processing.setEnabled(available and getattr(self,'_alignment_available',False))
        self.export_button.setEnabled(available and getattr(self,'_rate_supported',True))
        recovering = self._task_state not in ('idle','done','cancelled','failed')
        self.recovery_choice.setEnabled(not recovering)
        self.recovery_button.setEnabled(bool(self._recoveries) and not recovering)

    def scan_recovery(self):
        if self._recovery_stopping: return
        if self._recovery_worker is not None:
            self._recovery_scan_pending=True; return
        self._start_recovery_worker('scan')

    def _start_recovery_worker(self,operation,journal=None,destination=None):
        worker=ExportRecoveryWorker(self,operation,journal,destination)
        self._recovery_worker=worker
        worker.resultReady.connect(self._recovery_result)
        def finished():
            if self._recovery_worker is worker: self._recovery_worker=None
            worker.deleteLater()
            if self._recovery_scan_pending and not self._recovery_stopping:
                self._recovery_scan_pending=False; QTimer.singleShot(0,self.scan_recovery)
        worker.finished.connect(finished); worker.start()

    def _recovery_result(self,operation,value,error):
        if operation=='restart' and self._recovery_worker is not None:
            self._recovery_worker._published_job=None  # Ownership transfers here.
        if self._recovery_stopping:
            if operation=='restart' and value is not None: value.close()
            return
        if operation=='scan':
            if error:
                self.player.owner.append_log(log_text('player.recovery_scan_failed',error=error)); return
            self._recoveries=value; self.recovery_choice.clear()
            for record in value:
                self.recovery_choice.addItem(tr('player.recovery_candidate',filename=Path(record['destination']).name,
                    state=tr('player.recovery_finished' if record['state']=='verified' else 'player.recovery_interrupted')),record)
            self.recovery_label.setText(tr('player.recovery_available',count=len(value)))
            self.recovery_row.setVisible(bool(value)); self.update_source_availability(); self._fit_content()
        elif operation=='restart' and not error:
            self.player.export_job=value
            QTimer.singleShot(0,self._fit_content)
        else:
            self.finish_export('cancelled' if error==tr('player.export_cancelled') else 'failed' if error else 'done',error)
            if not error: self.task_stage.setText(tr('player.recovery_saved',filename=Path(value).name))
            self.scan_recovery()

    def _recovery_selection(self):
        record=self.recovery_choice.currentData()
        self.recovery_button.setText(tr('player.recovery_restart' if record and record['state']=='prepared' else 'player.recovery_save'))

    def recover_audio(self):
        record=self.recovery_choice.currentData()
        if not record or self._recovery_worker is not None: return
        path,_filter=QFileDialog.getSaveFileName(self,tr('player.export_destination'),record['destination'],
            f"{tr('recording.container_'+record['container'])} (*.{RECORDING_EXTENSIONS[record['container']]})",options=QFileDialog.DontConfirmOverwrite)
        if not path: return
        destination=Path(path).with_suffix('.'+RECORDING_EXTENSIONS[record['container']])
        if destination.resolve()==Path(record['source']).resolve():
            self.error.setText(tr('player.source_protected')); self.error.show(); self._fit_content(); return
        if destination.exists() and not self._confirm_overwrite(destination): return
        self.begin_export(record['source'],str(destination))
        restart=record['state']=='prepared'
        self.task_stage.setText(tr('player.recovery_restart_working' if restart else 'player.recovery_working')); self.progress.setRange(0,0)
        self._start_recovery_worker('restart' if restart else 'recover',record['journal'],str(destination))

    def stop_recovery(self):
        self._recovery_stopping=True
        if self._recovery_worker is not None:
            self._recovery_worker.requestInterruption(); self._recovery_worker.wait()
            job=self._recovery_worker._published_job
            if job is not None:
                job.close(); self._recovery_worker._published_job=None

    def update_export_phase(self,value):
        now=time.monotonic(); stage=value['stage']
        if self._task_state!=stage:
            self._phase_started=value.get('at',now); self._phase_rate=None; self._last_rate_sample=None
        self._task_state=stage; self._task_progress=value
        percent=value.get('percent')
        if percent is None:
            self.progress.setRange(0,0)
        else:
            self.progress.setRange(0,100); self.progress.setValue(min(99,int(percent)))
        caption=tr('player.export_phase_'+stage)
        self.task_stage.setText(tr('player.export_phase_percent',phase=caption,percent=int(percent)) if percent is not None else caption)
        sample=(value.get('at',now),int(value.get('done',0)))
        if stage in ('render','encode'):
            previous=self._last_rate_sample
            if previous and sample[0]-previous[0]>=.2 and sample[1]>previous[1]:
                rate=(sample[1]-previous[1])/(sample[0]-previous[0])
                self._phase_rate=rate if self._phase_rate is None else .25*rate+.75*self._phase_rate
                self._last_rate_sample=sample
            elif previous is None:
                self._last_rate_sample=sample
        self.update_export_time()

    def update_export_time(self):
        if self._task_started is None or self._task_progress is None:
            return
        now=time.monotonic(); elapsed=max(0,int(now-self._task_started))
        value=self._task_progress
        if self._task_state not in ('prepare','fonts','render','encode','finalize'):
            return
        prefix=tr('player.export_elapsed',elapsed=self.player.time_text(elapsed))
        total=int(value.get('total',0)); done=int(value.get('done',0))
        if (self._phase_rate and total>done and now-self._phase_started>=.75
                and self._last_rate_sample and now-self._last_rate_sample[0]<2.5):
            # Estimates advance only on measured worker work. A stalled native
            # call does not magically consume work or move the bar forward.
            remaining=max(1,math.ceil((total-done)/self._phase_rate))
            duration=self.player.time_text(remaining)
            if value.get('final_pass'):
                finish=time.strftime('%H:%M:%S',time.localtime(time.time()+remaining))
                text=tr('player.export_eta',elapsed=prefix,remaining=duration,finish=finish)
            else:
                text=tr('player.export_stage_eta',elapsed=prefix,remaining=duration)
        else:
            text=tr('player.export_time_pending',elapsed=prefix)
        if text!=self._last_time_text:
            self._last_time_text=text; self.task_time.setText(text)

    def finish_export(self,state,error=''):
        elapsed=self.player.time_text(max(0,int(time.monotonic()-(self._task_started or time.monotonic()))))
        self._task_state=state; self._task_progress=None
        self.task.show(); self.cancel_button.hide()
        self.export_button.setText(tr('player.export_begin'))
        self.export_button.setProperty('exportBusy',False)
        self.export_button.style().unpolish(self.export_button); self.export_button.style().polish(self.export_button)
        self.update_source_availability()
        self.task_stage.setText(tr('player.export_result_'+state))
        self.task_stage.setProperty('exportResult',state)
        self.task_stage.style().unpolish(self.task_stage); self.task_stage.style().polish(self.task_stage)
        self.task_stage.setToolTip(error)
        self.task_time.setText(error or tr('player.export_elapsed',elapsed=elapsed))
        self.task_time.setToolTip(error)
        self.progress.setRange(0,100)
        if state=='done':
            self.progress.setValue(100); self.progress.show(); self.folder_button.show()
        else:
            self.progress.hide(); self.folder_button.hide()
        self.refresh(); self._fit_content()

    def open_export_folder(self):
        if self._task_destination is not None:
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._task_destination.parent))):
                self.task_time.setText(tr('player.export_folder_failed'))

    def set_document(self,document):
        if self.document is document:
            return
        self.document=document
        if document is None:
            self.filename.clear(); self.start.setValue(0); self.end.setValue(0)
            self.error.clear(); self.error.hide(); self.refresh()
            return
        self.start.setMaximum(document.duration); self.end.setMaximum(document.duration)
        self.start.setValue(0); self.end.setValue(document.duration)
        self.filename.setText(str(Path(document.path).with_suffix('.'+RECORDING_EXTENSIONS[self.container.currentData()])))
        self.error.clear(); self.error.hide()
        self.refresh()

    def _set_expanded(self,expanded, *, refresh_extent=False):
        content=self.player.scroll.widget()
        content.layout().activate()
        height=self.player.left_panel.minimumSizeHint().height()
        self._disclosure_baseline=(height,0 if self.disclosure.isHidden() else self.disclosure.height())
        content.set_height_override(max(height,self.player.channel_content_height()))
        self.player.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._disclosure_setup=True
        try:
            self.disclosure.set_expanded(expanded,refresh_extent=refresh_extent)
        finally:
            self._disclosure_setup=False
        self._disclosure_changed()

    def _disclosure_changed(self):
        self.layout().invalidate(); self.layout().activate()
        content=self.player.scroll.widget()
        if self._disclosure_baseline is not None:
            height,previous=self._disclosure_baseline
            current=0 if self.disclosure.isHidden() else self.disclosure.height()
            content.set_height_override(max(height+current-previous,self.player.channel_content_height()))
        self.player._fit_content()
        if self.disclosure._motion_started_at is None and not self._disclosure_setup:
            content.set_height_override(None)
            self._disclosure_baseline=None
            self.player.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self.player._fit_content()
            if self._pending_extent:
                self._pending_extent=False
                QTimer.singleShot(0,self._fit_content)

    def _fit_content(self):
        if self.isVisible():
            self.layout().activate()
            if self.disclosure._motion_started_at is not None:
                self._pending_extent=True
                return
            if self.details.isChecked():
                self._set_expanded(True,refresh_extent=True)
            self.player._fit_content()

    def refresh(self,*_args):
        from settings import recording_output_rate,recording_rate_supported
        from stream_effects import processing_timing
        from effect_control import AUDIO_FX_PARAMETERS
        timing=processing_timing(self.player.owner.capture_effect_state(),self.sample_rate.currentData())
        self._alignment_available=timing['alignment_available']
        if not self._alignment_available: self.align_processing.setChecked(False)
        self.align_processing.setEnabled(timing['alignment_available'] and self._task_state in ('idle','done','cancelled','failed'))
        names=[tr('audio_fx.kind.'+kind) if kind in AUDIO_FX_PARAMETERS else tr('player.timing_effect.'+kind)
               for kind in timing['unqualified']]
        self.alignment_hint.setText(tr('player.alignment_effects_unqualified',effects=', '.join(names)) if names else '')
        self.alignment_hint.setVisible(not timing['alignment_available'])
        kind=self.container.currentData(); mode=self.quality['recording_rate_mode'].currentData()
        format_field=self.quality['recording_format']
        sync_recording_formats(format_field,kind)
        visible={"recording_format":kind in ('wav','aiff','flac','ogg_flac'),
            "recording_dither":kind in ('wav','aiff','flac','ogg_flac') and self.quality['recording_format'].currentData()!='float32',
            "recording_rf64":kind=='wav',"recording_compression":kind in ('flac','ogg_flac'),
            "recording_vorbis_quality":kind=='vorbis',"recording_mp3_quality":kind=='mp3' and mode=='vbr',
            "recording_opus_complexity":kind=='opus',"recording_bitrate":kind=='opus' or kind=='mp3' and mode=='cbr',
            "recording_rate_mode":kind in ('opus','mp3')}
        for name,row in self.quality_rows.items():
            row.setVisible(visible[name])
        custom=bool(self.range_mode.currentData())
        available=self.document is not None and self.player.export_job is None and self._task_state in ('idle','done','cancelled','failed')
        self.start_row.setEnabled(custom and available);self.end_row.setEnabled(custom and available)
        self.peak.setEnabled(self.normalize.isChecked() and self._task_state in ('idle','done','cancelled','failed'))
        rate=recording_output_rate(kind,self.sample_rate.currentData())
        duration=(self.end.value()-self.start.value() if custom else self.document.duration if self.document else 0)/(self.speed.get()/100)+self.tail.get()
        self.info.setText(tr("player.export_summary",duration=max(0,duration),rate=rate,
                             channels=1 if self.audio_channels.currentData() else 2))
        self._rate_supported=recording_rate_supported(kind,self.sample_rate.currentData()) is not False
        if not self._rate_supported:
            self.info.setText(tr('recording.rate_unsupported',rate=self.sample_rate.currentData(),format=self.container.currentText()))
        self.info.setToolTip(tr("player.export_snapshot_tip"))
        self.info.setVisible(self.document is not None)
        path=Path(self.filename.text())
        if path.suffix.lower().lstrip('.') in RECORDING_EXTENSIONS.values():
            normalized=str(path.with_suffix('.'+RECORDING_EXTENSIONS[kind]))
            if self.filename.text()!=normalized:
                self.filename.setText(normalized)
        self.filename.setToolTip(self.filename.text())
        if self.isVisible():
            QTimer.singleShot(0,self._fit_content)
        self.update_source_availability()

    def choose_destination(self):
        kind=self.container.currentData()
        path,_filter=QFileDialog.getSaveFileName(self,tr("player.export_destination"),self.filename.text(),
            f"{self.container.currentText()} (*.{RECORDING_EXTENSIONS[kind]})",options=QFileDialog.DontConfirmOverwrite)
        if path:
            self.filename.setText(path)

    def _confirm_overwrite(self,target):
        prompt=NativeMessageBox(self)
        prompt.setOption(QMessageBox.Option.DontUseNativeDialog,True)
        prompt.setWindowTitle(tr('player.overwrite_title'))
        prompt.setTextFormat(Qt.PlainText)
        # Match the app's long-identity policy: preserve both filename ends
        # and extension, expose the exact path separately. Qt's stock label
        # can clip an unbroken filename rather than wrap it at its hard limit.
        width=min(self.fontMetrics().horizontalAdvance('0'*48),
                  max(0,self.window().width()-2*METRICS['outer']
                      -self.style().pixelMetric(QStyle.PM_MessageBoxIconSize)
                      -self.fontMetrics().horizontalAdvance(tr('player.overwrite',filename='').split('\n')[0])))
        filename=self.fontMetrics().elidedText(target.name,Qt.ElideMiddle,width)
        prompt.setText(tr('player.overwrite',filename=filename))
        prompt.setToolTip(str(target)); prompt.setAccessibleDescription(str(target))
        prompt.setInformativeText(tr('player.overwrite_detail'))
        prompt.setIcon(QMessageBox.Question)
        replace_button=prompt.addButton(tr('player.overwrite_action'),QMessageBox.AcceptRole)
        cancel=prompt.addButton(tr('common.cancel'),QMessageBox.RejectRole)
        set_button_primary(replace_button,True)
        prompt.setDefaultButton(cancel); prompt.setEscapeButton(cancel)
        try:
            prompt.exec()
            return prompt.clickedButton() is replace_button
        finally:
            prompt.deleteLater()

    def confirm(self):
        from midi_player import MidiRenderOptions
        if self.document is None or self._task_state not in ('idle','done','cancelled','failed'):
            return
        self.refresh()  # Recheck the captured live chain, not stale window-open state.
        kind=self.container.currentData()
        raw=self.filename.text().strip()
        try:
            if not raw:
                raise ValueError(tr("player.export_path_invalid"))
            target=Path(raw).with_suffix('.'+RECORDING_EXTENSIONS[kind])
            if Path(raw).resolve()==Path(self.document.path) or target.resolve()==Path(self.document.path):
                raise ValueError(tr("player.source_protected"))
            if not target.parent.is_dir():
                raise ValueError(tr("player.export_path_invalid"))
            custom=bool(self.range_mode.currentData())
            options=MidiRenderOptions(self.start.value() if custom else 0,
                self.end.value() if custom else self.document.duration,int(self.transpose.get()),
                bool(self.audio_channels.currentData()),self.normalize.isChecked(),self.peak.get(),
                self.align_processing.isChecked()).validate(self.document)
            if custom and options.start >= options.end:
                raise ValueError(tr("player.export_options_invalid"))
            preferences=replace(self.preferences,sample_rate=self.sample_rate.currentData(),**quality_field_values(self.quality)).validate()
        except (ValueError,OSError) as exc:
            self.error.setText(str(exc)); self.error.show(); self._fit_content(); return
        self.error.clear(); self.error.hide(); self._fit_content()
        if target.exists():
            if not self._confirm_overwrite(target):
                return
        self.result_options=(str(target),preferences,options,self.speed.get()/100,self.tail.get())
        self.renderRequested.emit(self.result_options)


class NativeLicensesDialog(NativeDialog):
    """Offline modeless reader; no synth resources or settings writes."""
    def minimumSizeHint(self):return QSize(0,0)

    def __init__(self,owner):
        super().__init__(owner)
        from third_party_notices import load_notice_catalog,notice_presentation
        self.owner=owner;self.setWindowTitle(tr('licenses.title'));self.setMinimumSize(0,0)
        self.setFont(owner.font());self.resize(860,600)
        root=set_layout(QVBoxLayout(self),METRICS['outer'],METRICS['section_gap']);root.setSizeConstraint(QLayout.SetNoConstraint)
        heading=QLabel(tr('licenses.title'));heading.setTextFormat(Qt.PlainText);heading.setObjectName('DialogTitle');root.addWidget(heading)
        self.search=QLineEdit();self.search.setPlaceholderText(tr('licenses.search'));self.search.setAccessibleName(tr('licenses.search'))
        self.search.setMinimumWidth(0);root.addWidget(self.search)
        self.splitter=QSplitter(Qt.Horizontal);self.splitter.setMinimumSize(0,0)
        self.components=QListWidget();self.components.setObjectName('LicenseComponents')
        self.components.setAccessibleName(tr('licenses.components'));self.components.setMinimumSize(0,0)
        self.components.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Ignored)
        self.components.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);self.components.setTextElideMode(Qt.ElideRight)
        self.splitter.addWidget(self.components)
        detail=QWidget();detail.setMinimumSize(0,0);detail.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Ignored)
        body=set_layout(QVBoxLayout(detail),0,METRICS['compact_content_gap']);body.setSizeConstraint(QLayout.SetNoConstraint)
        self.component_title=ElasticLabel('',preferred_width=570);self.component_title.setObjectName('CardTitle');body.addWidget(self.component_title)
        self.description=NativeOptionalLabel();self.description.setWordWrap(True);body.addWidget(self.description)
        self.terms=NativeOptionalLabel();self.terms.setWordWrap(True);body.addWidget(self.terms)
        for label in (self.component_title,self.description,self.terms):label.setTextFormat(Qt.PlainText)
        self.documents=AnimatedComboBox();self.documents.setMinimumWidth(0)
        self.documents.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed);self.documents.setAccessibleName(tr('licenses.document'));body.addWidget(self.documents)
        self.reader=QTextBrowser();self.reader.setObjectName('LicenseDocument');self.reader.setMinimumSize(0,0)
        self.reader.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Ignored);self.reader.setOpenLinks(False);self.reader.setOpenExternalLinks(False)
        self.reader.setAccessibleName(tr('licenses.document'));body.addWidget(self.reader,1)
        self.reader.setContextMenuPolicy(Qt.CustomContextMenu)
        self.reader.customContextMenuRequested.connect(self.document_menu)
        self.splitter.addWidget(detail);self.splitter.setSizes([225,575]);self.splitter.setStretchFactor(0,1);self.splitter.setStretchFactor(1,3)
        root.addWidget(self.splitter,1)
        footer=control_row_layout()
        self.copy_button=make_action_button(tr('licenses.copy'),self.copy_document)
        self.website_button=make_action_button(tr('licenses.website'),self.open_website)
        self.close_button=make_action_button(tr('common.close'),self.close)
        for button in (self.copy_button,self.website_button,self.close_button):
            button.setAutoDefault(False);button.setMinimumWidth(0);button.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Fixed)
        footer.addWidget(self.copy_button);footer.addWidget(self.website_button);footer.addStretch(1);footer.addWidget(self.close_button);root.addLayout(footer)
        self._selected=None;self._catalog=None;self._document_valid=False
        try:
            self._catalog=load_notice_catalog()
            for component in self._catalog['components']:
                visible=notice_presentation(component,tr)
                item=QListWidgetItem(visible['name']);item.setData(Qt.UserRole,component)
                item.setToolTip(tooltip_display_text(tr(component['description_id'])));self.components.addItem(item)
        except (OSError,ValueError,KeyError,TypeError):self.reader.setPlainText(tr('licenses.unavailable'))
        self.search.textChanged.connect(self.filter_components)
        self.components.currentItemChanged.connect(self.select_component);self.documents.currentIndexChanged.connect(self.select_document)
        self.components.setCurrentRow(0)
        self.copy_button.setEnabled(self._document_valid);self.website_button.setEnabled(self._selected is not None)
        apply_native_text_metrics(self)

    def filter_components(self,text):
        from third_party_notices import notice_presentation
        for index in range(self.components.count()):
            item=self.components.item(index);row=item.data(Qt.UserRole)
            visible=notice_presentation(row,tr)
            item.setHidden(text.casefold() not in (visible['name']+' '+visible['terms']+' '+row['name']+' '+row['license']).casefold())
        current=self.components.currentItem()
        if current is None or current.isHidden():
            self.components.setCurrentItem(next((self.components.item(i) for i in range(self.components.count()) if not self.components.item(i).isHidden()),None))

    def select_component(self,item,*_args):
        self._selected=item.data(Qt.UserRole) if item else None
        self.documents.blockSignals(True);self.documents.clear()
        if self._selected:
            from third_party_notices import notice_presentation
            row=self._selected
            visible=notice_presentation(row,tr)
            self.component_title.setText(visible['title'])
            self.description.setText(visible['description'])
            # The selected original documents carry the full legal conditions.
            # Show the license identity without a second prose guide to them.
            self.terms.setText(visible['terms'])
            for document in row['documents']:
                self.documents.addItem(document['path'],document)
        else:self.component_title.setText(tr('licenses.no_results'));self.description.clear();self.terms.clear()
        self.documents.blockSignals(False);self.website_button.setEnabled(self._selected is not None);self.select_document()

    def select_document(self,*_args):
        from third_party_notices import read_notice
        document=self.documents.currentData();self._document_valid=False
        try:
            text=read_notice(document) if document else '';self._document_valid=document is not None
        except (OSError,ValueError,KeyError,TypeError):text=tr('licenses.document_unavailable')
        self.reader.setPlainText(text);self.copy_button.setEnabled(self._document_valid)

    def copy_document(self):
        if self._document_valid:QApplication.clipboard().setText(self.reader.toPlainText())

    def document_menu(self,position):
        menu=NativeMenu(self.reader)
        copy=menu.addAction(tr('licenses.copy_selection'),self.reader.copy)
        copy.setEnabled(self.reader.textCursor().hasSelection())
        menu.addAction(tr('licenses.select_all'),self.reader.selectAll)
        menu.exec(self.reader.viewport().mapToGlobal(position));menu.deleteLater()

    def open_website(self):
        if self._selected:QDesktopServices.openUrl(QUrl(self._selected['homepage']))

    def showEvent(self,event):
        if not getattr(self,'_fitted',False):
            # Text documents intentionally scroll. Keep a usable reading viewport
            # rather than expanding the window to the complete legal document.
            available=self.screen().availableGeometry();frame=self.frameGeometry().height()-self.height()
            self.resize(min(860,available.width()),min(600,available.height()-max(0,frame)))
            self._fitted=True
        super().showEvent(event)


class NativeRecordingDialog(NativeDialog):
    """Retained modeless capture workspace; owner retains drafts and save worker."""
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle(tr('recording_workspace.title'))
        self.setMinimumSize(0, 0)
        root = set_layout(QVBoxLayout(self), METRICS['outer'], METRICS['section_gap'])
        root.setSizeConstraint(QLayout.SetNoConstraint)
        self.rows, self.status_labels, self.elapsed_labels, self.progress = {}, {}, {}, {}
        for kind, title in (('midi', 'midi_recording.title'), ('wave', 'recording.title')):
            card = NativeCard()
            card.setMinimumWidth(0)
            card.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            body = set_layout(QVBoxLayout(card), METRICS['card_padding'], METRICS['compact_content_gap'])
            heading = ElasticLabel(tr(title), preferred_width=380)
            heading.setObjectName('CardTitle')
            body.addWidget(heading)
            description = QLabel(tr('recording_workspace.' + kind + '_source'))
            description.setWordWrap(True)
            description.setMinimumWidth(0)
            description.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            body.addWidget(description)
            feedback = control_row_layout()
            self.status_labels[kind] = ElasticLabel('', preferred_width=180)
            self.elapsed_labels[kind] = ElasticLabel('00:00:00.0', preferred_width=110)
            self.elapsed_labels[kind].setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            feedback.addWidget(self.status_labels[kind], 1)
            feedback.addWidget(self.elapsed_labels[kind])
            body.addLayout(feedback)
            progress = QProgressBar()
            progress.setRange(0, 0)
            progress.setTextVisible(False)
            progress.setAccessibleName(tr('recording.saving'))
            progress.hide()
            self.progress[kind] = progress
            body.addWidget(progress)
            controls = control_row_layout()
            start = make_action_button('', lambda _checked=False, key=kind: owner.toggle_recording(key))
            start.setToolTip(tr('midi_recording.start_tip' if kind == 'midi' else 'recording.start_tip'))
            save = make_action_button(tr('recording.save'), lambda _checked=False, key=kind: owner.save_recording(key))
            discard = make_action_button(tr('recording_workspace.discard'), lambda _checked=False, key=kind: owner.discard_recording(key))
            self.rows[kind] = (start, save, discard)
            for button in self.rows[kind]:
                button.setAutoDefault(False)
                button.setMinimumWidth(0)
                button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
                controls.addWidget(button, 1)
            body.addLayout(controls)
            root.addWidget(card,0,Qt.AlignTop)
        self._recording_active=set()
        self.close_hint = QLabel(tr('recording_workspace.close_hint'))
        self.close_hint.setWordWrap(True)
        self.close_hint.setMinimumWidth(0)
        self.close_hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.close_hint.hide();root.addWidget(self.close_hint)
        root.addStretch(1)
        footer = control_row_layout()
        self.settings_button = make_action_button(tr('recording_workspace.settings'), self.open_settings)
        self.close_button = make_action_button(tr('common.close'), self.close)
        for button in (self.settings_button, self.close_button):
            button.setAutoDefault(False)
            button.setMinimumWidth(0)
        footer.addWidget(self.settings_button)
        footer.addStretch(1)
        footer.addWidget(self.close_button)
        root.addLayout(footer)
        apply_native_text_metrics(self)

    def open_settings(self):
        self.owner.open_preferences()
        preferences = self.owner._preferences_dialog
        preferences.tabs.setCurrentWidget(preferences.recording_scroll)

    def present(self, kind, state, busy, status):
        phase = 'saving' if busy else 'failed' if status and status['failed'] else state
        if phase=='recording':self._recording_active.add(kind)
        else:self._recording_active.discard(kind)
        self.close_hint.setVisible(bool(self._recording_active))
        self.status_labels[kind].setText(tr('recording_workspace.state_' + phase))
        seconds = max(0., float(status['seconds'])) if status else 0.
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        # A save worker owns the recorder while busy; retain the last published time.
        if not busy and (status or state == 'idle'):
            self.elapsed_labels[kind].setText(f'{int(hours):02d}:{int(minutes):02d}:{seconds:04.1f}')
        changed = self.progress[kind].isHidden() == busy
        self.progress[kind].setVisible(busy)
        if changed and self.isVisible():
            QTimer.singleShot(0, self, lambda: fit_plain_dialog_contents(self, grow_only=True) if self.isVisible() else None)

    def showEvent(self, event):
        self.owner._refresh_recording_controls()
        if not getattr(self, '_fitted', False):
            fit_plain_dialog_contents(self, 460, opening=True)
            self._fitted = True
        super().showEvent(event)

    def closeEvent(self, event):
        # Capture is owned by the controller, not this optional view.
        event.accept()


class NativeMidiPlaylist(NativeRowList):
    """Playlist behavior shares the common four-row opening hint."""


class NativeMidiPlayerDialog(NativeDialog):
    """Non-modal file transport; the scheduler and renderer do not own Qt."""
    def __init__(self, owner):
        super().__init__(owner)
        from midi_player import MidiTransport, MidiPlaylistOrder
        self.owner = owner
        self.documents, self.document, self.pending = [], None, []
        self._playlist_order = MidiPlaylistOrder()
        self._navigating_playlist = False
        self._advance_on_end = False
        self._completion_serial = 0
        self.loader = self.export_job = None
        self.export_panel = None
        self.export_button = None
        self.muted, self.solo = set(), set()
        self.setWindowTitle(tr("player.title"))
        self.setMinimumSize(0, 0)
        self.setAcceptDrops(True)
        self.transport = MidiTransport(ae.forward_midi_bytes,
            ae.reset_file_transport_state, ae.silence_channel_notes,
            lambda: ae.silence_channel_notes(release=True),
            prepare=lambda doc: ae.ensure_midi_port(max(doc.ports)),
            is_drum=lambda ch: ae.runtime.addresses.drums[ch],
            native_execution=lambda: ae.runtime.channel_command_executor,native_eof_release=True)
        outer = set_layout(QVBoxLayout(self), METRICS["outer"], METRICS["compact_content_gap"])
        outer.setSizeConstraint(QLayout.SetNoConstraint)
        self.scroll=QScrollArea(); self.scroll.setWidgetResizable(True)
        self.scroll.setObjectName('PreferencesScroll'); self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setMinimumSize(0,0); self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content=WidthMeasuredPanel(); content.setObjectName('PreferencesContent'); content.setMinimumSize(0,0)
        self.scroll.setWidget(content); outer.addWidget(self.scroll,1)
        panes=set_layout(QHBoxLayout(content),0,METRICS['compact_content_gap'])
        panes.setSizeConstraint(QLayout.SetNoConstraint)
        self.left_panel=WidthMeasuredPanel()
        self.left_panel.setMinimumWidth(0)
        self.left_panel.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Expanding)
        panes.addWidget(self.left_panel,3)
        root=set_layout(QVBoxLayout(self.left_panel),0,METRICS['compact_content_gap'])
        root.setSizeConstraint(QLayout.SetNoConstraint)
        self._content_layout=root
        files = control_row_layout()
        self.open_button = make_action_button(tr("player.open"), self.choose_files)
        self.remove_button = make_action_button(tr("player.remove"), self.remove_file)
        for button in (self.open_button, self.remove_button):
            button.setMinimumWidth(0); button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            files.addWidget(button)
        library = NativeCard()
        self.library_card=library
        library_body = set_layout(QVBoxLayout(library), METRICS["card_padding"], METRICS["compact_content_gap"])
        library_body.addLayout(files)
        root.addWidget(library,1)
        library.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Expanding)
        self.playlist = NativeMidiPlaylist()
        self.playlist.setAccessibleName(tr("player.playlist_accessible"))
        self.playlist.setMinimumSize(0, 0)
        self.playlist.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.playlist.setAlternatingRowColors(True)
        self.playlist.setAcceptDrops(False)  # Parent owns local-file URL drops.
        self.playlist.currentRowChanged.connect(self.select_file)
        self.playlist.itemActivated.connect(lambda _item: self.play_pause(force_play=True))
        library_body.addWidget(self.playlist,1)
        playback = NativeCard()
        self.playback_card=playback
        playback.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        playback_body = set_layout(QVBoxLayout(playback), METRICS["card_padding"], METRICS["compact_content_gap"])
        root.insertWidget(0,playback)
        self.summary = ElasticLabel(tr("player.empty"), preferred_width=450)
        self.summary.setToolTip(tr("player.shared_channels"))
        self.summary.setWordWrap(True)
        playback_body.addWidget(self.summary)
        self.file_info=ElasticLabel('',preferred_width=450)
        self.file_info.setWordWrap(True)
        self.file_info.setToolTip(tr('player.file_info_tip'))
        self.file_info.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
        self.file_info.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Preferred)
        self.file_info.set_preferred_width(0)
        seek_row = control_row_layout()
        self.time_label = ElasticLabel("00:00 / 00:00", preferred_width=104)
        self.seek = NativeSlider(Qt.Horizontal)
        self.seek.absolute_position = True
        self.seek.setSingleStep(1000)
        self.seek.setPageStep(10000)
        self.seek.setMinimumWidth(0)
        self.seek.setRange(0, 0)
        self.seek.setAccessibleName(tr("player.seek"))
        self.seek.sliderReleased.connect(lambda: self.transport.command("seek", self.seek.value()/1000))
        self.seek.valueChanged.connect(self._seek_value_changed)
        self._refreshing_seek = False
        seek_row.addWidget(self.time_label); seek_row.addWidget(self.seek, 1)
        playback_body.addLayout(seek_row)
        self.score_info=ElasticLabel(tr('player.score_empty'),preferred_width=450)
        self.score_info.setWordWrap(True)
        self.score_info.setToolTip(tr('player.score_tip'))
        controls = control_row_layout()
        self.previous_button = NativeTransportButton('previous',tr("player.previous"),lambda:self.step_file(-1))
        self.play_button = NativeTransportButton('play',tr("player.play"),self.play_pause,primary=True)
        self.stop_button = NativeTransportButton('stop',tr("player.stop"),self._stop_playback)
        self.next_button = NativeTransportButton('next',tr("player.next"),lambda:self.step_file(1))
        self.repeat = NativeTransportButton('repeat',tr("player.repeat_track"),lambda:None)
        self.repeat.setCheckable(True)
        self.repeat.setToolTip(tr('player.repeat_tip'))
        self.repeat.toggled.connect(lambda value: self.transport.command("loop", value))
        self.shuffle = NativeTransportButton('shuffle',tr('player.shuffle'),lambda:None)
        self.shuffle.setCheckable(True)
        self.shuffle.setToolTip(tr('player.shuffle_tip'))
        self.shuffle.toggled.connect(self._shuffle_changed)
        self.speed = midi_adjustment_row('speed',100,lambda value:self.transport.command('speed',value/100))
        for control in (self.previous_button, self.play_button, self.stop_button, self.next_button):
            controls.addWidget(control)
        controls.addStretch(1)
        controls.addWidget(self.shuffle); controls.addWidget(self.repeat)
        playback_body.addLayout(controls)
        metadata=control_row_layout();metadata.addWidget(self.score_info,1);metadata.addWidget(self.file_info)
        playback_body.addLayout(metadata)
        self.transpose = midi_adjustment_row('transpose',0,lambda value:self.transport.command('transpose',int(value)))
        self.chase = NativeToggleSwitch(tr("player.chase_notes"))
        self.chase.setToolTip(tr("player.chase_notes_tip"))
        self.chase.toggled.connect(lambda value: self.transport.command("chase_notes", value))
        playback_body.addWidget(self.speed); playback_body.addWidget(self.transpose)
        controls.addWidget(self.chase)
        self.cue = ElasticLabel("", preferred_width=450)
        self.cue.setWordWrap(True)
        self.cue.setToolTip(tr("player.cue_tip"))
        playback_body.addWidget(self.cue)
        self.cue.hide()
        headers=('',tr('player.channel'),tr('player.instrument'),tr('player.mute'),tr('player.solo'))
        self._channel_spec=InstrumentTableSpec(headers=headers,trailing_column=2)
        self.channels = NativeInstrumentTable(16, 5)
        self.channels.setTabKeyNavigation(False)
        configure_instrument_table(self.channels,lambda:self.owner._dark,spec=self._channel_spec)
        self.channels.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._channel_column_preferences={0:self._channel_spec.activity_width,
            1:self._channel_spec.fitted_width(tr('player.channel')),
            3:self._channel_spec.fitted_width(tr('player.mute')),
            4:self._channel_spec.fitted_width(tr('player.solo'))}
        self._channel_columns_syncing=False
        self.channels.horizontalHeader().sectionResized.connect(self._channel_column_resized)
        self.channels.setAcceptDrops(False)
        self.channels.cellDoubleClicked.connect(lambda row,column:self.owner.open_monitor_channel_editor(self._player_port * 16 + row) if column in (1,2) and self.document and self._player_port * 16 + row in dict(self.document.channels) else None)
        self.channels.setToolTip(tr("player.channels_tip"))
        self.channels.setAccessibleName(tr("player.channels_accessible"))
        self.channels_card=NativeCard()
        self.channels_card.setMinimumWidth(0)
        self.channels_card.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Expanding)
        channel_layout=set_layout(QVBoxLayout(self.channels_card),METRICS['card_padding'],0)
        self._player_port = 0
        self.port_selection = AnimatedComboBox()
        self.port_selection.setMinimumWidth(0)
        self.port_selection.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.port_selection.setAccessibleName(tr('monitor.port'))
        self.port_selection.addItem(tr('monitor.port_item', port=0), 0)
        self.port_selection.hide()
        self.port_selection.currentIndexChanged.connect(self._select_player_port)
        channel_layout.addWidget(self.port_selection)
        channel_layout.addWidget(self.channels)
        panes.addWidget(self.channels_card,2)
        self._channel_checks=[]
        for ch in range(16):
            for column,text in ((0,''),(1,f'{ch+1:02d}'),(2,'')):
                self.channels.setItem(ch,column,QTableWidgetItem(text))
            self.channels.item(ch,1).setData(NativeInstrumentRowDelegate.ROLE_SETTINGS_DISABLED,True)
            checks=[]
            for column,kind in ((3,'mute'),(4,'solo')):
                cell=QWidget(); body=set_layout(QHBoxLayout(cell),0,0)
                check=OutlineCheckBox('',fit_cell=True)
                check.setAccessibleName(tr('player.channel_toggle',channel=ch+1,action=tr('player.'+kind)))
                check.setEnabled(False)
                check.toggled.connect(lambda value,ch=ch,kind=kind:self.channel_toggle(ch,kind,value))
                # Fill the row's height for a stable table-cell hit target;
                # the shared checkbox painter centers its16px indicator.
                body.addWidget(check,alignment=Qt.AlignHCenter)
                self.channels.setCellWidget(ch,column,cell); checks.append(check)
            self._channel_checks.append(checks)
        # The channel card is a live view of the MIDI file.  Do not reserve
        # sixteen disabled rows when the file has fewer (or no) used
        # channels: that makes an empty player look populated and creates a
        # misleading scroll/height requirement.  The card itself remains
        # expandable so the two panes keep their aligned outer height.
        self._set_visible_channel_rows(set())
        self.channels.viewport().installEventFilter(self)
        self.export_panel=NativeMidiExportPanel(self)
        self.export_panel.renderRequested.connect(self.start_export)
        self.export_button=self.export_panel.export_button
        self.export_panel.update_source_availability()
        self.export_panel.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        root.addWidget(self.export_panel)
        self.progress=self.export_panel.progress
        self.cancel_export=self.export_panel.cancel_button
        self.status = ElasticLabel("", preferred_width=450)
        root.addWidget(self.status)
        self.status.hide()
        self.left_panel.set_height_source(self._preferred_left_height)
        content.set_height_source(lambda:max(self._preferred_left_height(),
                                            self.channel_content_height()))
        self.footer=QWidget(); self.footer.setMinimumWidth(0)
        footer=set_layout(QHBoxLayout(self.footer),0,METRICS['side_action_gap'])
        footer.addStretch(1)
        self.close_button=make_action_button(tr('common.close'),self.close,primary=True)
        self.close_button.setAutoDefault(False); self.close_button.setDefault(False)
        self.close_button.setToolTip(tr('player.close_tip'))
        footer.addWidget(self.close_button)
        outer.addWidget(self.footer)
        self.timer = QTimer(self)
        self.timer.setInterval(50); self.timer.timeout.connect(self.refresh); self.timer.start()
        self.activity_timer=QTimer(self)
        self.activity_timer.setInterval(MIDI_ACTIVITY_INTERVAL_MS)
        self.activity_timer.timeout.connect(self.refresh_activity)
        self._reported_error = ""
        self._last_names_refresh = 0
        self.set_theme()
        self.refresh()

    def showEvent(self,event):
        super().showEvent(event)
        self.timer.start()
        self.activity_timer.start()
        self.refresh()
        self._prepare_content_geometry()
        if not getattr(self,'_opening_fitted',False):
            fit_dialog_contents(self,self.scroll,self.preferred_width(),opening=True)
            self._opening_fitted=True
            self._opening_refit_pending=True
            QTimer.singleShot(0,self._fit_content)

    def _fit_content(self):
        if self.isVisible():
            self._prepare_content_geometry()
            opening_refit=getattr(self,'_opening_refit_pending',False)
            self._opening_refit_pending=False
            fit_dialog_contents(self,self.scroll,self.width(),shift_to_fit=True,grow_only=not opening_refit)

    def _preferred_left_height(self):
        layout=self._content_layout
        # Forms retain natural heights;only the library receives surplus.
        # Four opening rows are a preference. The list remains scrollable.
        height=layout.totalHeightForWidth(self.left_panel.width()) if layout.hasHeightForWidth() else layout.totalSizeHint().height()
        return max(0,height)

    def _prepare_content_geometry(self):
        self.time_label.set_preferred_width(self.time_label.fontMetrics().horizontalAdvance(self.time_label.text())+8)
        # Child list height changes before Qt posts its LayoutRequest. Resolve
        # the nested cache now, before first-show/content fitting reads it.
        self.library_card.layout().invalidate(); self.library_card.layout().activate()
        self._content_layout.invalidate(); self._content_layout.activate()
        self.left_panel.updateGeometry()

    def channel_content_height(self):
        # totalMinimumSize includes the styled card's own contents margins.
        # Adding only the explicit16px layout margins omitted its two border
        # pixels and forced a permanent2px scrollbar at the measured opening.
        self.channels_card.layout().activate()
        return self.channels_card.layout().totalMinimumSize().height()

    def set_theme(self):
        for toggle in self.findChildren(QCheckBox):
            if hasattr(toggle, "set_theme"):
                toggle.set_theme(widget_palette(self.owner))
        apply_native_text_metrics(self)
        self.channels.setFont(self._channel_spec.font())
        self.channels.horizontalHeader().setFont(self._channel_spec.font())
        self.channels.ensurePolished()
        self._set_visible_channel_rows(self._used_channels_for_port())
        self._resize_channel_columns()

    def _used_channels_for_port(self):
        used = dict(self.document.channels) if self.document else {}
        return {self._player_port * 16 + row for row in range(16)
                if self._player_port * 16 + row in used}

    def _set_visible_channel_rows(self, used):
        """Show only channels present in the selected MIDI port.

        The header remains visible as the table's column key.  Unused channel
        rows are hidden instead of rendered as disabled empty controls; this
        follows the same progressive-disclosure rule used by the rest of the
        player and avoids a false impression that sixteen channels are active.
        """
        used = set(used)
        visible = 0
        for row in range(16):
            show = self._player_port * 16 + row in used
            self.channels.setRowHidden(row, not show)
            visible += int(show)
        header = self.channels.horizontalHeader().sizeHint().height()
        rows = max(0, visible) * self._channel_spec.row_height
        self.channels.setMinimumHeight(header + rows + 2 * self.channels.frameWidth())
        self.channels.updateGeometry()
        self.channels_card.layout().invalidate()
        self.channels_card.layout().activate()

    def preferred_width(self):
        spec=self._channel_spec
        monitor=(spec.activity_width+spec.fitted_width(tr('player.channel'))
                 +spec.fitted_width('Acoustic Grand Piano')
                 +sum(spec.fitted_width(tr('player.'+key)) for key in ('mute','solo'))
                 +2*METRICS['card_padding'])
        return midi_preferred_width(self,self.speed)+METRICS['compact_content_gap']+monitor

    def _resize_channel_columns(self):
        available=max(0,self.channels.viewport().width())
        preferred=self._channel_column_preferences
        scale=min(1,available/max(1,sum(preferred.values())))
        self._channel_columns_syncing=True
        try:
            for column,width in preferred.items():
                self.channels.setColumnWidth(column,int(width*scale))
        finally:self._channel_columns_syncing=False

    def _channel_column_resized(self,column,_old,width):
        if not self._channel_columns_syncing and column in (1,3,4):
            self._channel_column_preferences[column]=max(0,width)

    def eventFilter(self,watched,event):
        if watched is self.channels.viewport() and event.type()==QEvent.Resize:
            self._resize_channel_columns()
        return super().eventFilter(watched,event)

    def choose_files(self):
        paths, _filter = QFileDialog.getOpenFileNames(self, tr("player.open"), "", tr("player.file_filter"))
        self.add_files(paths)

    def add_files(self, paths):
        for path in paths:
            normalized = str(Path(path).resolve())
            if normalized not in [doc.path for doc in self.documents] and normalized not in self.pending:
                self.pending.append(normalized)
        self._load_next()

    def _load_next(self):
        if self.loader is not None or not self.pending:
            return
        self.status.setText(tr("player.loading"))
        self.loader = NativeMidiFileLoadWorker(self.pending.pop(0), self)
        self.loader.loaded.connect(self._loaded)
        self.loader.failed.connect(self._load_failed)
        self.loader.finished.connect(self._loader_finished)
        self.loader.start()

    def _loaded(self, document):
        self.documents.append(document)
        self.playlist.addItem(Path(document.path).name)
        self.playlist.item(self.playlist.count()-1).setToolTip(document.path)
        if self.playlist.currentRow() < 0:
            self.playlist.setCurrentRow(0)
        else:
            self._reset_playlist_order()
            self.refresh()
        self.status.setText("")

    def _load_failed(self, error):
        self.status.setText(error)
        self.status.setToolTip('')

    def _loader_finished(self):
        worker, self.loader = self.loader, None
        worker.deleteLater()
        self._load_next()

    def select_file(self, row):
        self.document = self.documents[row] if 0 <= row < len(self.documents) else None
        if not self._navigating_playlist:
            self._reset_playlist_order()
            self._advance_on_end = False
        self._completion_serial = self.transport.completion[0]
        self._last_names_refresh = 0
        self._cues = tuple(event for event in self.document.events if event.text and event.kind in (5, 6, 7)) if self.document else ()
        self._cue_times = tuple(event.seconds for event in self._cues)
        self.transport.command("load", self.document)
        self.cue.clear(); self.cue.hide()
        self.muted.clear(); self.solo.clear()
        self._player_port = self.document.ports[0] if self.document else 0
        blocked = self.port_selection.blockSignals(True)
        self.port_selection.clear()
        for port in self.document.ports if self.document else (0,):
            names={row['slot']:row['name'] for row in self.owner.midi_input_runtime.port_settings.ports}
            self.port_selection.addItem(names.get(port,tr('monitor.port_item',port=port)),port)
        self.port_selection.setCurrentIndex(0)
        self.port_selection.setVisible(bool(self.document and self.document.ports != (0,)))
        self.port_selection.blockSignals(blocked)
        self.transport.command("mute", set())
        doc = self.document
        self.export_panel.set_document(doc)
        self.export_panel.update_source_availability()
        self.seek.setRange(0, round(doc.duration*1000) if doc else 0)
        self.summary.setText(Path(doc.path).name if doc else tr("player.empty"))
        self.summary.setToolTip(doc.path + "\n" + tr("player.shared_channels") if doc else tr("player.shared_channels"))
        used=dict(doc.channels) if doc else {}
        for ch in range(16):
            self.channels.item(ch,2).setText('')
            self.channels.item(ch,2).setToolTip('')
            self.channels.item(ch,0).setData(NativeActivityCellDelegate.ROLE_INTENSITY,0.)
            self.channels.item(ch,1).setData(NativeInstrumentRowDelegate.ROLE_SETTINGS_DISABLED,self._player_port * 16 + ch not in used)
            for check in self._channel_checks[ch]:
                blocked=check.blockSignals(True)
                check.setChecked(False); check.setEnabled(self._player_port * 16 + ch in used)
                check.blockSignals(blocked)
        self._set_visible_channel_rows({self._player_port * 16 + ch for ch in range(16)
                                        if self._player_port * 16 + ch in used})
        self.refresh()
        QTimer.singleShot(0,self._fit_content)

    def _select_player_port(self, _index):
        self._player_port = self.port_selection.currentData() or 0
        used = dict(self.document.channels) if self.document else {}
        for row in range(16):
            channel = self._player_port * 16 + row
            self.channels.item(row, 2).setText("")
            self.channels.item(row, 0).setData(NativeActivityCellDelegate.ROLE_INTENSITY, 0.)
            self.channels.item(row, 1).setData(NativeInstrumentRowDelegate.ROLE_SETTINGS_DISABLED, channel not in used)
            for check, values in zip(self._channel_checks[row], (self.muted, self.solo)):
                blocked = check.blockSignals(True)
                check.setChecked(channel in values)
                check.setEnabled(channel in used)
                check.blockSignals(blocked)
        self._set_visible_channel_rows(self._used_channels_for_port())
        self._last_names_refresh = 0
        self.refresh()
        QTimer.singleShot(0, self._fit_content)

    def channel_toggle(self, ch, kind, checked):
        ch += self._player_port * 16
        target = self.muted if kind == "mute" else self.solo
        target.add(ch) if checked else target.discard(ch)
        self.transport.command("mute", self.effective_mutes())

    def effective_mutes(self):
        return self.muted | (set(range(max(ae.runtime.addresses.count, (max(self.document.ports) + 1) * 16 if self.document else 16)))-self.solo if self.solo else set())

    def remove_file(self):
        row = self.playlist.currentRow()
        if row >= 0:
            self.documents.pop(row)
            self.playlist.blockSignals(True)
            self.playlist.takeItem(row)
            self.playlist.blockSignals(False)
            self.select_file(self.playlist.currentRow())

    def step_file(self, step):
        path = self._playlist_order.move(step)
        if path is not None:
            playing = self.transport.snapshot()[0] == "playing"
            self._navigating_playlist = True
            try:self.playlist.setCurrentRow(next(i for i,doc in enumerate(self.documents) if doc.path==path))
            finally:self._navigating_playlist = False
            if playing:
                self.transport.command("play")

    def _reset_playlist_order(self):
        self._playlist_order.reset([doc.path for doc in self.documents], self.document.path if self.document else None)

    def _shuffle_changed(self, checked):
        self._playlist_order.shuffle = checked
        self._reset_playlist_order()
        self.refresh()

    def _stop_playback(self):
        self._advance_on_end = False
        self.transport.command('stop')

    def play_pause(self, force_play=False):
        if self.document and ae.runtime.stream_handle:
            self._advance_on_end = force_play or self.transport.snapshot()[0] != 'playing'
            self.transport.command("play" if force_play else "toggle")
        elif self.document:
            self.status.setText(tr("player.engine_not_ready"))

    def _seek_value_changed(self, value):
        if not self._refreshing_seek and not self.seek.isSliderDown():
            self.transport.command("seek", value/1000)

    @staticmethod
    def time_text(seconds):
        value = max(0, int(seconds))
        return f"{value//60:02d}:{value%60:02d}"

    def refresh(self):
        serial, completed = self.transport.completion
        if serial != self._completion_serial:
            self._completion_serial = serial
            if completed is self.document and self._advance_on_end:
                if self._playlist_order.available(1):
                    self.step_file(1)
                    self.transport.command('play')
                else:self._advance_on_end = False
        state, position, _speed, error = self.transport.snapshot()
        doc = self.document
        command='pause' if state=='playing' else 'play'
        if self.play_button.command!=command:
            self.play_button.set_command(command,tr('player.'+command))
        self.play_button.setEnabled(doc is not None)
        self.stop_button.setEnabled(doc is not None)
        self.previous_button.setEnabled(self._playlist_order.available(-1))
        self.next_button.setEnabled(self._playlist_order.available(1))
        self.repeat.setEnabled(doc is not None)
        self.shuffle.setEnabled(len(self.documents)>1)
        self.remove_button.setEnabled(doc is not None)
        if self.export_button is not None:
            self.export_panel.update_source_availability()
        self.time_label.setText(f"{self.time_text(position)} / {self.time_text(doc.duration if doc else 0)}")
        musical=doc.musical_position(position) if doc else None
        if musical:
            bpm=musical['bpm'];meter=musical['meter'];location=musical['location'];key=musical['key']
            major=('C♭','G♭','D♭','A♭','E♭','B♭','F','C','G','D','A','E','B','F♯','C♯')
            minor=('A♭','E♭','B♭','F','C','G','D','A','E','B','F♯','C♯','G♯','D♯','A♯')
            key_text=tr('player.key_value',note=(minor if key[1] else major)[key[0]+7],mode=tr('player.minor' if key[1] else 'player.major')) if key else tr('player.key_unknown')
            self.score_info.setText(tr('player.score_value',bpm=bpm,effective=bpm*_speed,numerator=meter[0],denominator=meter[1],
                location=tr('player.bar_beat',bar=location[0],beat=location[1]) if location else tr('player.timecode'),key=key_text))
            self.score_info.setToolTip(tr('player.score_details',bpm=bpm,effective=bpm*_speed,numerator=meter[0],denominator=meter[1],
                location=tr('player.bar_beat_long',bar=location[0],beat=location[1]) if location else tr('player.timecode'),key=key_text))
        else:self.score_info.setText(tr('player.score_empty'))
        self.file_info.setText(tr('player.file_info_short',tracks=doc.tracks,channels=len(doc.channels)) if doc else tr('player.file_info_empty'))
        self.file_info.setToolTip(tr('player.file_info',format=doc.file_format,tracks=doc.tracks,channels=len(doc.channels),ports=len(doc.ports),
            notes=sum(count for _,count in doc.channels),resolution=tr('player.ppq',value=doc.ticks_per_beat) if doc.ticks_per_beat else tr('player.timecode')) if doc else tr('player.file_info_tip'))
        if not self.seek.isSliderDown():
            self._refreshing_seek = True
            try:
                self.seek.setValue(round(position*1000))
            finally:
                self._refreshing_seek = False
        if doc:
            import bisect
            cue_index = bisect.bisect_right(self._cue_times, position) - 1
            cue = tr("player.cue_value", text=self._cues[cue_index].text) if cue_index >= 0 else ""
            if self.cue.text() != cue:
                self.cue.setText(cue); self.cue.setToolTip(tr('player.cue_tip')); self.cue.setVisible(bool(cue))
                QTimer.singleShot(0,self._fit_content)
            used=dict(doc.channels)
            if time.monotonic()-self._last_names_refresh >= .2:
                self._last_names_refresh = time.monotonic()
                names = self.owner.soundfont_runtime.active_channel_names()
                for ch in range(16):
                    native_channel = self._player_port * 16 + ch
                    name=(names[native_channel] or '') if native_channel in used and native_channel < len(names) else ''
                    item = self.channels.item(ch, 2)
                    if item and item.text() != name:
                        item.setText(name); item.setToolTip('')
        self.refresh_activity()
        if error and error != self._reported_error:
            self._advance_on_end = False
            self._reported_error = error; self.status.setText(error)
        if self.export_job:
            final = None
            for kind, value in self.export_job.poll():
                if kind == "progress":
                    self.progress.setValue(min(99,value))
                elif kind=='phase':
                    self.export_panel.update_export_phase(value)
                else:
                    final = (kind, value)
            if final is None and not self.export_job.process.is_alive():
                final = ("error", tr("player.export_worker_failed"))
            if final:
                if final == ("done", True):
                    try:
                        self.export_job.commit()
                    except (OSError, RuntimeError) as exc:
                        final = ("error", str(exc))
                self.last_export_evidence = self.export_job.evidence
                self.export_job.close()
                cleanup_error=getattr(self.export_job,'cleanup_error','')
                if isinstance(cleanup_error,str) and cleanup_error:
                    self.owner.append_log(log_text('player.export_cleanup_failed',error=cleanup_error))
                self.export_job = None
                self.export_panel.scan_recovery()
                self.export_panel.finish_export('done' if final==('done',True) else 'failed' if final[0]=='error' else 'cancelled',str(final[1]) if final[0]=='error' else '')
            else:
                self.export_panel.update_export_time()
        if self.status.isHidden()==bool(self.status.text()):
            self.status.setVisible(bool(self.status.text()))
            QTimer.singleShot(0,self._fit_content)
        if not self.isVisible() and self.export_job is None and not self._advance_on_end:
            self.timer.stop()

    def export_audio(self):
        if self.export_job or self.document is None:
            return
        self.export_panel.confirm()

    def start_export(self,result_options):
        if self.export_job or self.document is None:
            return
        from midi_player import MidiExportJob, ExportSnapshot
        from settings import EXPORT_QUALITY_FIELDS
        path, preferences, options, speed, tail = result_options
        kind = preferences.recording_container
        self.export_panel.begin_export(self.document.path,path)
        try:
            snapshot = ExportSnapshot(preferences,
                self.owner.soundfont_runtime.copy_group(self.owner._soundfont_group),
                self.owner.capture_effect_state(), ae.runtime.performance.ini_values(),
                ae.runtime.midi_learn.ini_values(), ae.runtime.channel_policy.ini_values(),
                source_assets=(*self.owner.soundfont_runtime.asset_snapshot(),
                               *ae.runtime.stream_effects.rack.asset_snapshot()))
            if not snapshot.group.fonts:
                self.export_panel.finish_export('failed',tr('player.no_soundfont')); return
            quality=engine_ini_values(preferences)
            self.owner._settings_writer.submit({'MIDI Export':{key:quality[key] for key in EXPORT_QUALITY_FIELDS}},
                owned_keys={'MIDI Export':set(EXPORT_QUALITY_FIELDS)})
            self.export_job = MidiExportJob(self.document, snapshot, path, kind,
                speed, self.effective_mutes(), tail, render_options=options)
        except Exception as exc:
            self.export_panel.finish_export('failed',str(exc)); return
        self.export_button.setEnabled(False)
        self.timer.start()
        QTimer.singleShot(0,self._fit_content)

    def cancel_render(self):
        worker=self.export_panel._recovery_worker if self.export_panel is not None else None
        if worker is not None and worker.operation in ('recover','restart'):
            worker.requestInterruption(); self.cancel_export.setEnabled(False)
        if self.export_job:
            self.cancel_export.setEnabled(False)
            self.export_job.close()
            cleanup_error=getattr(self.export_job,'cleanup_error','')
            if isinstance(cleanup_error,str) and cleanup_error:
                self.owner.append_log(log_text('player.export_cleanup_failed',error=cleanup_error))
            self.export_job = None
            self.export_panel.finish_export('cancelled')
            self.refresh()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in (".mid", ".midi", ".rmi") for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.add_files([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in (".mid", ".midi", ".rmi")])
        event.acceptProposedAction()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space and not isinstance(QApplication.focusWidget(), (QLineEdit, QAbstractSpinBox)):
            self.play_pause(); event.accept()
        elif event.key() == Qt.Key_Delete and QApplication.focusWidget() is self.playlist:
            self.remove_file(); event.accept()
        else:
            super().keyPressEvent(event)

    @ae.activity_observation
    def refresh_activity(self):
        if not self.isVisible():return
        visual=ae.get_channel_note_visual_state()
        used=dict(self.document.channels) if self.document else {}
        for row in range(16):
            channel=self._player_port*16+row
            value=visual[channel] if channel in used else 0.
            item=self.channels.item(row,0)
            if item.data(NativeActivityCellDelegate.ROLE_INTENSITY)!=value:
                item.setData(NativeActivityCellDelegate.ROLE_INTENSITY,value)

    def shutdown(self):
        self.timer.stop()
        self.activity_timer.stop()
        if self.export_panel is not None:
            self.export_panel.stop_recovery()
            self.export_panel.disclosure.settle()
        self.pending.clear()
        if self.loader:
            self.loader.requestInterruption(); self.loader.wait()
        self.cancel_render()
        if self.transport.close() is False:
            raise RuntimeError(tr('runtime.channel_executor_retained'))

    def closeEvent(self, event):
        # Closing is not a stealth stop; reopening keeps the transport session.
        event.accept()

    def hideEvent(self,event):
        self.activity_timer.stop()
        if self.export_job is None and not self._advance_on_end:
            self.timer.stop()
        super().hideEvent(event)


class NativeErrorReportWorker(QThread):
    def __init__(self,operation,arguments,parent):
        super().__init__(parent);self.operation=operation;self.arguments=arguments
        self.result=None;self.error=''

    def run(self):
        from error_reporting import collect_report, save_report
        try:
            if self.operation=='collect':self.result=collect_report(**self.arguments)
            else:save_report(**self.arguments);self.result=self.arguments['path']
        except Exception as error:self.error=str(error)


class NativeErrorReportPanel(QWidget):
    """Explicit, preview-first local support workflow in preferences."""
    captureChanged=Signal()
    def __init__(self,owner,parent=None):
        super().__init__(parent);self.owner=owner;self.worker=None;self.reviewed=''
        root=set_layout(QVBoxLayout(self),0,METRICS['section_gap'])
        root.setSizeConstraint(QLayout.SetNoConstraint)
        self.capture_card=NativeCard();make_horizontally_elastic(self.capture_card,QSizePolicy.Fixed)
        capture_body=set_layout(QVBoxLayout(self.capture_card),METRICS['card_padding'],METRICS['compact_content_gap'])
        heading=ElasticLabel(tr('error_report.capture_title'));heading.setObjectName('SectionTitle');capture_body.addWidget(heading)
        root.addWidget(self.capture_card)
        intro=QLabel(tr('error_report.intro'));intro.setWordWrap(True);intro.setMinimumWidth(0)
        intro.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred);capture_body.addWidget(intro)
        capture=control_row_layout()
        self.capture_button=make_action_button(tr('action.debug'),self._toggle_capture,
                                               checkable=True,object_name='DebugCapture')
        self.capture_button.setMinimumWidth(0)
        self.capture_button.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Fixed)
        self.capture_button.setToolTip(tr('error_report.capture_tip'))
        self.capture_status=NativeOptionalLabel('',preferred_width=300)
        self.capture_status.setWordWrap(True)
        capture.addWidget(self.capture_button);capture.addWidget(self.capture_status,1);capture.addStretch(1)
        capture_body.addLayout(capture)
        self.refresh_capture()
        self.description=QPlainTextEdit();self.description.setAccessibleName(tr('error_report.description'))
        self.description.setObjectName('SupportText')
        self.description.setTabChangesFocus(True)
        self.description.setPlaceholderText(tr('error_report.description_hint'))
        self.description.setMinimumSize(0,72);self.description.setMaximumHeight(112)
        capture_body.addWidget(self.description)
        self.options={}
        options=set_layout(QHBoxLayout(),0,METRICS['form_column_gap'])
        for key in ('logs','system','settings'):
            check=OutlineCheckBox(tr('error_report.include_'+key));check.setChecked(True)
            check.setMinimumWidth(0);check.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
            self.options[key]=check;options.addWidget(check,1);check.toggled.connect(self.invalidate)
        capture_body.addLayout(options)
        self.preview_card=NativeCard();make_horizontally_elastic(self.preview_card,QSizePolicy.Expanding)
        preview_body=set_layout(QVBoxLayout(self.preview_card),METRICS['card_padding'],METRICS['compact_content_gap'])
        heading=ElasticLabel(tr('error_report.review_title'));heading.setObjectName('SectionTitle');preview_body.addWidget(heading)
        root.addWidget(self.preview_card,1)
        self.preview=QPlainTextEdit();self.preview.setReadOnly(True)
        self.preview.setObjectName('SupportText')
        self.preview.setTabChangesFocus(True)
        self.preview.setAccessibleName(tr('error_report.preview'));self.preview.setMinimumSize(0,110)
        self.preview.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        preview_body.addWidget(self.preview,1)
        self.status=NativeOptionalLabel();self.status.setWordWrap(True);preview_body.addWidget(self.status)
        actions=set_layout(QHBoxLayout(),0,METRICS['side_action_gap'])
        self.collect_button=make_action_button(tr('error_report.collect'),self.collect)
        self.save_button=make_action_button(tr('error_report.save'),self.save,primary=True);self.save_button.setEnabled(False)
        for button in (self.collect_button,self.save_button):
            button.setMinimumWidth(0);button.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed);actions.addWidget(button,1)
        preview_body.addLayout(actions);self.description.textChanged.connect(self.invalidate)

    def _set_status(self,text):
        self.status.setText(text)

    @property
    def busy(self):return self.worker is not None

    def _toggle_capture(self):
        self.owner.debug()
        self.refresh_capture()

    def refresh_capture(self):
        trace=self.owner.midi_input_runtime.trace
        self.capture_button.setChecked(trace is not None)
        self.capture_button.setText(tr('action.debug_stop' if trace is not None else 'action.debug'))
        self.capture_status.setText(tr('error_report.capture_active',path=trace.path) if trace is not None else '')
        self.captureChanged.emit()

    def invalidate(self,*_args):
        self.reviewed='';self.save_button.setEnabled(False)
        if not self.busy:self._set_status('');self.preview.clear()

    def collect(self):
        if self.busy:return
        from dataclasses import asdict
        from error_reporting import record
        record('support_preview_requested')
        # These are GUI-owned/cached immutable values: no BASS/registry/engine
        # query, no source assets, no raw INI, no soundfont/MIDI file parsing.
        snapshot=dict(settings=dict(engine=asdict(self.owner._engine_preferences),
                pending_engine=asdict(self.owner._pending_engine_preferences),
                effects=asdict(self.owner.capture_effect_state())),
            playback=dict(getattr(self.owner,'_report_playback_snapshot',{}),
                output=self.owner.device_combo.currentText(),midi_input=self.owner.midi_device_combo.itemText(0)))
        self._start('collect',dict(snapshot=snapshot,description=self.description.toPlainText(),
            **{key:field.isChecked() for key,field in self.options.items()}))

    def save(self):
        if self.busy or not self.reviewed:return
        dialog=QFileDialog(self,tr('error_report.save'))
        dialog.setAcceptMode(QFileDialog.AcceptSave);dialog.setFileMode(QFileDialog.AnyFile)
        dialog.setNameFilter(tr('error_report.filter'));dialog.setDefaultSuffix('zip')
        dialog.selectFile('ABMSynth-report.zip')
        try:
            if dialog.exec()==QDialog.Accepted and dialog.selectedFiles():
                self._start('save',dict(path=dialog.selectedFiles()[0],preview=self.reviewed))
        finally:dialog.deleteLater()

    def _start(self,operation,arguments):
        if operation=='collect':self.reviewed='';self.preview.clear()
        self.worker=NativeErrorReportWorker(operation,arguments,self)
        self._set_status(tr('error_report.collecting' if operation=='collect' else 'error_report.saving'))
        self.collect_button.setEnabled(False);self.save_button.setEnabled(False)
        self.description.setEnabled(False)
        for field in self.options.values():field.setEnabled(False)
        self.worker.finished.connect(self._finished);self.worker.start()

    def _finished(self):
        worker=self.worker;self.worker=None
        self.collect_button.setEnabled(True);self.description.setEnabled(True)
        for field in self.options.values():field.setEnabled(True)
        if worker.error:self._set_status(tr('error_report.failed',error=worker.error))
        elif worker.operation=='collect':
            self.reviewed=worker.result;self.preview.setPlainText(self.reviewed)
            self._set_status('')
        else:self._set_status(tr('error_report.saved',path=worker.result))
        self.save_button.setEnabled(bool(self.reviewed));worker.deleteLater()


class NativeMidiPortSettingsPanel(WidthMeasuredPanel):
    changed=Signal()
    def __init__(self,parent=None):
        super().__init__(parent)
        from settings import load_midi_ports
        self.rows=[dict(row,inputs=list(row['inputs']),outputs=list(row['outputs'])) for row in load_midi_ports().ports]
        self.index=0;self.loading=False;self.catalog={'inputs':[],'outputs':[]}
        root=set_layout(QVBoxLayout(self),0,METRICS['compact_content_gap'])
        header=control_row_layout();self.selector=AnimatedComboBox();header.addWidget(self.selector,1)
        self.selector.setAccessibleName(tr('midi_ports.title'))
        self.add=NativeRackButton('add',tr('midi_ports.add'),lambda:self._add())
        self.remove=NativeRackButton('remove',tr('midi_ports.remove'),lambda:self._remove())
        self.add.setToolTip(tr('midi_ports.add_help'));self.remove.setToolTip(tr('midi_ports.remove_help'))
        header.addWidget(self.add);header.addWidget(self.remove);root.addLayout(header)
        name_row=control_row_layout();name_row.addWidget(ElasticLabel(tr('midi_ports.name')))
        self.name=QLineEdit();self.name.setMaxLength(27);self.name.setToolTip(tr('midi_ports.name_help'));name_row.addWidget(self.name,1);root.addLayout(name_row)
        self.name.setAccessibleName(tr('midi_ports.name'))
        switches=control_row_layout()
        self.virtual_in=OutlineCheckBox(tr('midi_ports.virtual_in'));self.virtual_out=OutlineCheckBox(tr('midi_ports.virtual_out'))
        for control in (self.virtual_in,self.virtual_out):control.setToolTip(tr('midi_ports.virtual_requirement'))
        switches.addWidget(self.virtual_in,1);switches.addWidget(self.virtual_out,1);root.addLayout(switches)
        hints=ElasticLabel(tr('midi_ports.direction_help'));hints.setWordWrap(True);root.addWidget(hints)
        devices=QGridLayout();set_layout(devices,0,METRICS['form_column_gap']);self.devices={}
        for column,direction in enumerate(('inputs','outputs')):
            devices.addWidget(ElasticLabel(tr('midi_ports.'+direction)),0,column)
            listing=NativeRowList();listing.setMinimumSize(0,0);listing.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
            listing.setAccessibleName(tr('midi_ports.'+direction));listing.setSelectionMode(QAbstractItemView.NoSelection)
            listing.itemChanged.connect(self._edit);devices.addWidget(listing,1,column);devices.setColumnStretch(column,1);self.devices[direction]=listing
        root.addLayout(devices,1)
        self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Expanding)
        for listing in self.devices.values():
            listing.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Expanding)
            listing.setMinimumHeight(listing.fontMetrics().lineSpacing()*2+10)
        self.refresh_button=make_action_button(tr('midi_ports.refresh'),self.refresh_devices);root.addWidget(self.refresh_button,alignment=Qt.AlignRight)
        self.selector.currentIndexChanged.connect(self._select)
        # Include assistive ValuePattern edits;_show_row owns the loading guard.
        self.name.textChanged.connect(self._edit)
        self.virtual_in.toggled.connect(self._edit);self.virtual_out.toggled.connect(self._edit)
        self._show_row();self.refresh_devices()

    def _commit(self):
        row=self.rows[self.index];row.update(name=self.name.text(),virtual_in=self.virtual_in.isChecked(),virtual_out=self.virtual_out.isChecked())
        for direction,listing in self.devices.items():
            row[direction]=[listing.item(i).data(Qt.UserRole) for i in range(listing.count()) if listing.item(i).checkState()==Qt.Checked]

    def _edit(self,*_):
        if self.loading:return
        self._commit();self.selector.setItemText(self.index,self.name.text());self.changed.emit()

    def _show_row(self):
        self.loading=True
        try:
            self.selector.clear()
            for row in self.rows:self.selector.addItem(row['name'])
            self.selector.setCurrentIndex(self.index)
            row=self.rows[self.index];self.name.setText(row['name']);self.virtual_in.setChecked(row['virtual_in']);self.virtual_out.setChecked(row['virtual_out'])
            for direction,listing in self.devices.items():
                listing.clear();known=set()
                for device in self.catalog[direction]:
                    if device['own']:continue
                    item=QListWidgetItem(device['name']);item.setData(Qt.UserRole,device['key']);item.setFlags(item.flags()|Qt.ItemIsUserCheckable)
                    item.setCheckState(Qt.Checked if device['key'] in row[direction] else Qt.Unchecked);listing.addItem(item);known.add(device['key'])
                for key in row[direction]:
                    if key in known:continue
                    try:label=__import__('json').loads(key)[0]
                    except (ValueError,TypeError,IndexError):label=key
                    item=QListWidgetItem(tr('midi_ports.disconnected',name=label));item.setData(Qt.UserRole,key);item.setFlags(item.flags()|Qt.ItemIsUserCheckable);item.setCheckState(Qt.Checked);listing.addItem(item)
                if not listing.count():
                    item=QListWidgetItem(tr('midi_ports.empty_devices'));item.setFlags(Qt.NoItemFlags);listing.addItem(item)
                # Both directions share one grid row. Device counts affect
                # scroll range,never the relative list heights.
            self.add.setEnabled(len(self.rows)<8);self.remove.setEnabled(len(self.rows)>1)
        finally:self.loading=False

    def _select(self,index):
        if self.loading or index<0:return
        self._commit();self.index=index;self._show_row()

    def _add(self):
        self._commit()
        if len(self.rows)>=8:return
        number=2
        while 'ABMSynth MIDI '+str(number) in {row['name'] for row in self.rows}:number+=1
        slot=next(i for i in range(8) if i not in {row['slot'] for row in self.rows})
        self.rows.append(dict(name='ABMSynth MIDI '+str(number),slot=slot,virtual_in=True,virtual_out=True,inputs=[],outputs=[]))
        self.rows.sort(key=lambda row:row['slot']);self.index=next(i for i,r in enumerate(self.rows) if r['slot']==slot)
        self._show_row();self.changed.emit()

    def _remove(self):
        if len(self.rows)<=1:return
        self.rows.pop(self.index);self.index=min(self.index,len(self.rows)-1);self._show_row();self.changed.emit()

    def refresh_devices(self):
        self._commit()
        from midi_input_runtime import midi_endpoint_catalog
        self.catalog=midi_endpoint_catalog();self._show_row()

    def settings(self):
        self._commit()
        from settings import MidiPortSettings
        return MidiPortSettings(tuple(self.rows)).normalized()


class NativeDriverSetupNotice(QObject):
    """One optional read-only startup check;no installer or audio ownership."""
    KEY='skip_driver_setup_notice'

    def __init__(self,owner):
        super().__init__(owner);self.owner=owner;self.started=False;self.closed=False
        self.dialog=None;self.pending=None;self.results=queue.SimpleQueue()
        self.timer=QTimer(self);self.timer.setInterval(200);self.timer.timeout.connect(self._poll)

    def requested(self):
        ports=self.owner.midi_input_runtime.port_settings.ports
        return self.owner._engine_preferences.windows_midi_default or any(p['virtual_in'] or p['virtual_out'] for p in ports)

    def start(self):
        if self.started or self.closed:return
        self.started=True
        if self.owner._skip_driver_setup_notice or not self.requested():return
        results=self.results
        def work():
            from windows_integration import midi_mapper_status
            try:result=midi_mapper_status()
            except Exception:result=None
            results.put(result)
        threading.Thread(target=work,name='ABMSynth-driver-status',daemon=True).start()
        self.timer.start()

    def _poll(self):
        if self.closed or self.owner._shutdown_started:self.close();return
        if self.pending is None:
            try:report=self.results.get_nowait()
            except queue.Empty:return
            if not isinstance(report,dict) or report.get('state')!='absent' or report.get('action')!='Install':
                self.timer.stop();return
            self.pending=report
        if self.owner._skip_driver_setup_notice or not self.requested():self.timer.stop();return
        if not self.owner.isVisible() or self.owner.isMinimized() or QApplication.activeModalWidget() is not None:return
        self.timer.stop();self._offer()

    def _offer(self):
        if self.closed or self.dialog is not None:return
        box=NativeMessageBox(self.owner);self.dialog=box
        box.setWindowTitle(tr('driver_notice.title'));box.setIcon(QMessageBox.Information)
        box.setText(tr('driver_notice.message'));box.setInformativeText(tr('driver_notice.detail'))
        setup=box.addButton(tr('driver_notice.open_settings'),QMessageBox.AcceptRole)
        dismiss=box.addButton(tr('common.close'),QMessageBox.RejectRole)
        box.setDefaultButton(dismiss);box.setEscapeButton(dismiss)
        checkbox=OutlineCheckBox(tr('driver_notice.do_not_show'));checkbox.set_theme(widget_palette(self.owner));box.setCheckBox(checkbox)
        box.setModal(False);box.setWindowModality(Qt.NonModal)
        def finished(_result):
            if not self.closed:
                if checkbox.isChecked():self.owner.set_driver_notice_suppressed(True)
                if box.clickedButton() is setup:
                    self.owner.open_preferences()
                    preferences=self.owner._preferences_dialog
                    preferences.tabs.setCurrentWidget(preferences.program_scroll)
                    preferences.program_scroll.ensureWidgetVisible(preferences.mapper_card)
            self.dialog=None;box.deleteLater()
        box.finished.connect(finished);box.show()

    def close(self):
        self.closed=True;self.timer.stop()
        if self.dialog is not None:
            box=self.dialog;self.dialog=None;box.close()


class NativeEnginePreferencesDialog(NativeDialog):
    """Live preview with cancel rollback and staged restart preferences."""

    def minimumSizeHint(self):
        return QSize(0, 0)

    def install_mapper(self):
        from windows_integration import launch_midi_mapper_install
        if self.mapper_install_process is not None:return
        self._refresh_mapper_status()
        action=self._mapper_status_snapshot['action']
        if action is None:return
        confirm={'Install':'player.mapper_install_confirm','Repair':'player.mapper_repair_confirm','Uninstall':'player.mapper_uninstall_confirm','Update':'player.mapper_update_confirm'}
        caption={'Install':'player.mapper_install_action','Repair':'player.mapper_repair_action','Uninstall':'player.mapper_uninstall_action','Update':'player.mapper_update_action'}
        prompt = NativeMessageBox(self)
        prompt.setWindowTitle(tr('player.mapper_title'))
        prompt.setText(tr(confirm[action]))
        prompt.setIcon(QMessageBox.Question)
        proceed = prompt.addButton(tr(caption[action]), QMessageBox.AcceptRole)
        cancel = prompt.addButton(tr('common.cancel'), QMessageBox.RejectRole)
        prompt.setDefaultButton(cancel); prompt.setEscapeButton(cancel)
        try:
            prompt.exec()
            accepted = prompt.clickedButton() is proceed
        finally:
            prompt.deleteLater()
        if not accepted:
            return
        try:
            self.mapper_install_process=launch_midi_mapper_install(action)
            self.mapper_install_action=action
        except OSError:
            self._set_mapper_operation_status(tr('player.mapper_operation_failed'));return
        self.mapper_install_button.setEnabled(False)
        self.mapper_check_button.setEnabled(False)
        self._set_mapper_operation_status(tr('player.mapper_operation_running'))
        self._mapper_poll_timer.start()
        self._poll_mapper_install()

    def _poll_mapper_install(self):
        process=self.mapper_install_process
        if process is None or process.poll() is None:return
        self._mapper_poll_timer.stop()
        code=process.returncode
        self.mapper_install_process=None
        self._refresh_mapper_status()
        errors={10:'player.mapper_error_host',11:'player.mapper_error_admin',12:'player.mapper_error_busy',
            13:'player.mapper_error_mode',14:'player.mapper_error_preparation',15:'player.mapper_error_transaction',
            16:'player.mapper_error_file',17:'player.mapper_error_slot',18:'player.mapper_error_owner',
            19:'player.mapper_error_package',20:'player.mapper_error_abandoned',21:'player.mapper_error_in_use',
            22:'player.mapper_error_cleanup',1223:'player.mapper_operation_cancelled'}
        if code not in (0,3010):self._set_mapper_operation_status(tr(errors.get(code,'player.mapper_operation_failed')))
        else:
            expected='absent' if self.mapper_install_action=='Uninstall' else 'ready'
            message=('player.mapper_uninstall_pending' if code==3010 else 'player.mapper_uninstall_done') if self.mapper_install_action=='Uninstall' else 'player.mapper_operation_done'
            self._set_mapper_operation_status(tr(message if self._mapper_status_snapshot['state']==expected else 'player.mapper_operation_verify_failed'))

    def _set_mapper_operation_status(self,text):
        self.mapper_operation_status.setText(text)
        invalidate_layout_chain(self.mapper_operation_status)
        self._queue_content_fit()

    def _refresh_mapper_status(self, *_args):
        if self.mapper_install_process is not None:return
        from windows_integration import midi_mapper_status
        report=midi_mapper_status();self._mapper_status_snapshot=report
        captions={'ready':'player.mapper_state_ready','absent':'player.mapper_state_absent',
            'missing_registration':'player.mapper_state_registration','missing_file':'player.mapper_state_file',
            'changed_file':'player.mapper_state_changed','conflict':'player.mapper_state_conflict',
            'unverified':'player.mapper_state_unverified'}
        rows={row['architecture']:row['state'] for row in report['architectures']}
        for arch,label in self.mapper_arch_status.items():label.setText(tr(captions.get(rows.get(arch),'player.mapper_state_unknown')))
        hints={'absent':'player.mapper_hint_install',
            'missing_registration':'player.mapper_hint_registration','missing_file':'player.mapper_hint_file',
            'changed_file':'player.mapper_hint_changed','conflict':'player.mapper_hint_conflict',
            'unverified':'player.mapper_hint_unverified','unsupported':'player.mapper_hint_unsupported','unreadable':'player.mapper_hint_unreadable'}
        status='' if report['state']=='ready' else tr(hints[report['state']])
        if report.get('routing_restriction')=='unqualified':
            routing=tr('player.mapper_hint_unqualified',mode='Hybrid' if report['api_mode']=='hybrid' else 'Windows MIDI Services')
            status=(tr('player.mapper_status_with_routing',status=status,routing=routing)
                    if report['state'] not in ('ready','absent') else routing)
        elif report.get('routing_restriction')=='default_requires_legacy':
            routing=tr('player.mapper_hint_ports_only')
            status=(tr('player.mapper_status_with_routing',status=status,routing=routing)
                    if report['state'] not in ('ready','absent') else routing)
        elif report.get('routing_restriction')=='sdk_port_numbers':
            routing=tr('player.mapper_sdk_caveat')
            status=(tr('player.mapper_status_with_routing',status=status,routing=routing)
                    if report['state'] not in ('ready','absent') else routing)
        limited=report.get('routing_restriction') in ('unqualified','default_requires_legacy')
        self.windows_midi_default.setEnabled(not limited)
        tooltip=('player.mapper_default_legacy' if limited else
                 'player.mapper_sdk_caveat_detail' if report.get('routing_restriction')=='sdk_port_numbers' else 'player.mapper_detail')
        self.windows_midi_default.setToolTip(tr(tooltip))
        self.mapper_status.setText(status)
        actions={'Install':'player.mapper_install','Repair':'player.mapper_repair','Uninstall':'player.mapper_uninstall','Update':'player.mapper_update'}
        intended=report.get('requested_action',report['action'])
        self.mapper_install_button.setText(tr(actions.get(intended,'player.mapper_repair')))
        self.mapper_install_button.setEnabled(report['action'] is not None)
        self.mapper_check_button.setEnabled(True)
        if hasattr(self,'mapper_card'):
            invalidate_layout_chain(self.mapper_status)
            self._queue_content_fit()

    def _mapper_tab_changed(self,*_args):
        if self.isVisible() and self.tabs.currentWidget() is self.program_scroll:self._refresh_mapper_status()

    def __init__(self, owner: QMainWindow):
        super().__init__(owner)
        self._loading = True
        self.owner = owner
        self._original_live_preferences = owner._engine_preferences
        self._original_color_theme = owner._color_theme
        self._original_system_reset_receivers=owner.system_reset_receivers()
        self.setWindowTitle(tr("preferences.title"))
        self.resize(760, 620)
        self.setMinimumSize(0, 0)
        self.setFont(owner.font())
        root = set_layout(QVBoxLayout(self), METRICS["outer"], METRICS["section_gap"])
        root.setSizeConstraint(QLayout.SetNoConstraint)
        heading = QLabel(tr("preferences.title"))
        heading.setObjectName("DialogTitle")
        root.addWidget(heading)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("PreferencesScroll")
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = WidthMeasuredPanel()
        content.setObjectName("PreferencesContent")
        content.setMinimumWidth(0)
        body = bordered_page_layout(QVBoxLayout(content),METRICS['section_gap'])
        body.setSizeConstraint(QLayout.SetNoConstraint)
        self.sample_rate = AnimatedComboBox()
        self.sample_rate.setAccessibleName(tr("preferences.sample_rate"))
        for rate in SAMPLE_RATES:
            self.sample_rate.addItem(sample_rate_caption(rate), rate)
        self.buffer_ms = NativeNumberBox(10, 5000, tr("preferences.buffer"), suffix=tr("preferences.buffer_suffix"))
        self.shared_update_ms = NativeNumberBox(5, 100, tr("preferences.shared_update_ms"), suffix=tr("preferences.buffer_suffix"))
        self.exclusive_block_ms = NativeNumberBox(1, 100, tr("preferences.exclusive_block_ms"), suffix=tr("preferences.buffer_suffix"))
        self.voices = NativeNumberBox(32, 100000, tr("preferences.voices"))
        self.synthesis_mode = OutlineCheckBox("")
        self.midi_cpu_limit = AnimatedComboBox()
        for value, key in ((0, "preferences.cpu_auto"), (101, "preferences.cpu_no_kill")):
            self.midi_cpu_limit.addItem(tr(key), value)
        self.soundfont_stereo = AnimatedComboBox()
        for enabled, key in ((False, "preferences.pan_existing"), (True, "preferences.pan_balance")):
            self.soundfont_stereo.addItem(tr(key), enabled)
        for name, field in make_recording_quality_fields().items():
            setattr(self,name,field)
        self.recording_timestamp = NativeToggleSwitch("")
        self.recording_basename = QLineEdit()
        self.recording_basename.setMaxLength(64)
        self.recording_basename.setValidator(QRegularExpressionValidator(QRegularExpression(r'[^<>:"/\\|?*\x00-\x1f]*'), self))
        self.recording_basename.editingFinished.connect(self._finish_recording_basename)
        self.recording_directory = QWidget()
        folder_row = set_layout(QHBoxLayout(self.recording_directory), 0, METRICS["form_column_gap"])
        self.recording_directory_edit = QLineEdit()
        self.recording_directory_edit.setAccessibleName(tr("preferences.recording_directory"))
        self.recording_directory_edit.setReadOnly(True)
        self.recording_directory_edit.setPlaceholderText(tr("recording.folder_default"))
        make_horizontally_elastic(self.recording_directory_edit)
        folder_row.addWidget(self.recording_directory_edit, 1)
        self.recording_folder_button = make_action_button(tr("recording.browse"), self._choose_recording_directory)
        folder_row.addWidget(self.recording_folder_button)
        self.recording_folder_reset = make_action_button(tr("recording.folder_reset"), lambda: self.recording_directory_edit.clear())
        folder_row.addWidget(self.recording_folder_reset)
        self.interpolation = AnimatedComboBox()
        self.interpolation.setAccessibleName(tr("preferences.interpolation"))
        for quality, key in enumerate((
            "preferences.interpolation_linear", "preferences.interpolation_sinc8",
            "preferences.interpolation_sinc16",
        )):
            self.interpolation.addItem(tr(key), quality)
        self.midi_timing = AnimatedComboBox()
        self.midi_timing.setAccessibleName(tr("preferences.midi_timing"))
        for enabled, key, detail_key in (
            (False, "preferences.midi_timing_sync", "preferences.midi_timing_sync_detail"),
            (True, "preferences.midi_timing_async", "preferences.midi_timing_async_detail"),
        ):
            self.midi_timing.addItem(tr(key), enabled)
            self.midi_timing.setItemData(
                self.midi_timing.count() - 1,
                tooltip_display_text(tr(detail_key)), Qt.ToolTipRole,
            )
        self.loading_mode = AnimatedComboBox()
        for mode, key in enumerate(("preferences.loading_realtime", "preferences.loading_used", "preferences.loading_all")):
            self.loading_mode.addItem(tr(key), mode)
        self.default_reverb_send = NativeNumberBox(0, 127, tr("preferences.default_reverb_send"))
        self.soundfont_fx_policy = AnimatedComboBox()
        for value, key in (("sum", "preferences.fx_sum"),
                           ("minimum", "preferences.fx_minimum"),
                           ("midi_only", "preferences.fx_midi_only")):
            self.soundfont_fx_policy.addItem(tr(key), value)
        self.priority = AnimatedComboBox()
        self.priority.setAccessibleName(tr("preferences.priority"))
        for level, key in (
            ("realtime", "preferences.priority_realtime"),
            ("high", "preferences.priority_high"),
            ("above_normal", "preferences.priority_above"),
            ("normal", "preferences.priority_normal"),
            ("below_normal", "preferences.priority_below"),
            ("idle", "preferences.priority_idle"),
        ):
            self.priority.addItem(tr(key), level)
        self.auto_reset_delay_s = NativeNumberBox(0, 600, tr("preferences.auto_reset_delay_s"), suffix=tr("preferences.seconds_suffix"))
        self.cc6_direct_pitch_range = NativeToggleSwitch("")
        self.cc94_custom_delay_send = NativeToggleSwitch("")
        self.startup_with_windows = OutlineCheckBox("")
        self.start_minimized = OutlineCheckBox("")
        self.minimize_to_tray = OutlineCheckBox("")
        self.close_to_tray = OutlineCheckBox("")
        self.windows_midi_default = NativeToggleSwitch("")
        self.main_log_level=AnimatedComboBox()
        for level in ('info','warning','error'):
            self.main_log_level.addItem(tr('preferences.main_log_level_'+level),level)
        self.main_log_detail=AnimatedComboBox()
        for detail in ('changes','summary','detailed'):
            self.main_log_detail.addItem(tr('preferences.main_log_detail_'+detail),detail)
        self._controls = {
            'main_log_level':(self.main_log_level,'main_log_level','main_log_level_detail'),
            'main_log_detail':(self.main_log_detail,'main_log_detail','main_log_detail_help'),
            "windows_midi_default": (self.windows_midi_default, "windows_midi_default", "windows_midi_default_detail"),
            "sample_rate": (self.sample_rate, "sample_rate", "sample_rate_detail"),
            "synthesis_mode": (self.synthesis_mode, "synthesis_mode", "synthesis_high_performance_detail"),
            "buffer_ms": (self.buffer_ms, "buffer", "buffer_detail"),
            "shared_update_ms": (self.shared_update_ms, "shared_update_ms", "shared_update_ms_detail"),
            "exclusive_block_ms": (self.exclusive_block_ms, "exclusive_block_ms", "exclusive_block_ms_detail"),
            "voices": (self.voices, "voices", "voices_detail"),
            "midi_cpu_limit": (self.midi_cpu_limit, "midi_cpu_limit", "midi_cpu_limit_detail"),
            "soundfont_stereo": (self.soundfont_stereo, "soundfont_stereo", "soundfont_stereo_detail"),
            "recording_format": (self.recording_format, "recording_format", "recording_format_detail"),
            "recording_dither": (self.recording_dither, "recording_dither", "recording_dither_detail"),
            "recording_rf64": (self.recording_rf64, "recording_rf64", "recording_rf64_detail"),
            "recording_container": (self.recording_container, "recording_container", "recording_container_detail"),
            "recording_timestamp": (self.recording_timestamp, "recording_timestamp", "recording_timestamp_detail"),
            "recording_basename": (self.recording_basename, "recording_basename", "recording_basename_detail"),
            "recording_directory": (self.recording_directory, "recording_directory", "recording_directory_detail"),
            **{name: (getattr(self, name), name, name + "_detail") for name in (
                "recording_compression", "recording_vorbis_quality", "recording_mp3_quality", "recording_opus_complexity", "recording_bitrate", "recording_rate_mode")},
            "interpolation": (self.interpolation, "interpolation", "interpolation_detail"),
            "midi_async": (self.midi_timing, "midi_timing", "midi_timing_detail"),
            "loading_mode": (self.loading_mode, "loading_mode", "loading_mode_detail"),
            "default_reverb_send": (self.default_reverb_send, "default_reverb_send", "default_reverb_detail"),
            "soundfont_fx_policy": (self.soundfont_fx_policy, "soundfont_fx_policy", "soundfont_fx_policy_detail"),
            "priority": (self.priority, "priority", "priority_detail"),
            "auto_reset_delay_s": (self.auto_reset_delay_s, "auto_reset_delay_s", "auto_reset_delay_detail"),
            "cc6_direct_pitch_range": (self.cc6_direct_pitch_range, "cc6_direct_pitch_range", "cc6_direct_pitch_range_detail"),
            "cc94_custom_delay_send": (self.cc94_custom_delay_send, "cc94_custom_delay_send", "cc94_custom_delay_send_detail"),
            "startup_with_windows": (self.startup_with_windows, "startup_with_windows", "startup_with_windows_detail"),
            "start_minimized": (self.start_minimized, "start_minimized", "start_minimized_detail"),
            "minimize_to_tray": (self.minimize_to_tray, "minimize_to_tray", "minimize_to_tray_detail"),
            "close_to_tray": (self.close_to_tray, "close_to_tray", "close_to_tray_detail"),
        }
        self._form_columns = []
        self._settings_cards = []
        # Present task groups,not the runtime LIVE/RESTART storage partition.
        # Single-column sequences remain ordered;only paired window actions
        # below use explicit semantic columns.
        self.audio_quality_card = self._settings_card('preferences.quality_title',
            ('sample_rate','interpolation','soundfont_stereo','soundfont_fx_policy','default_reverb_send'),
            columns=(('sample_rate','interpolation','soundfont_stereo'),('soundfont_fx_policy','default_reverb_send')))
        self.audio_performance_card = self._settings_card('preferences.processing_title',
            ('synthesis_mode','voices','midi_cpu_limit','loading_mode','priority'),
            columns=(('synthesis_mode','voices','midi_cpu_limit'),('loading_mode','priority')))
        self.audio_buffer_card = self._settings_card('preferences.output_buffer_title',
            tuple(OUTPUT_TIMING_SETTINGS),
            columns=(('buffer_ms','shared_update_ms'),('exclusive_block_ms',)))
        self._sync_output_cadence_controls()
        owner.device_combo.currentIndexChanged.connect(self._sync_output_cadence_controls)
        for card in (self.audio_quality_card,self.audio_performance_card):body.addWidget(card)
        body.addStretch(1)
        self.scroll.setWidget(content)
        self.tabs = make_navigation_tabs()
        self.tabs.setObjectName("PreferencesTabs")
        self.tabs.setMinimumSize(0, 0)
        self.tabs.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.tabs.addTab(self.scroll, tr("preferences.tab_audio"))
        self.midi_input_card=NativeCard()
        input_body=set_layout(QVBoxLayout(self.midi_input_card),METRICS['card_padding'],METRICS['compact_content_gap'])
        title=ElasticLabel(tr('preferences.restart_caption',label=tr('preferences.port_connections_title')));title.setObjectName('SectionTitle');input_body.addWidget(title)
        self.midi_ports=NativeMidiPortSettingsPanel();input_body.addWidget(self.midi_ports,1)
        self.midi_ports.changed.connect(self._mark_dirty)
        self.midi_ports.changed.connect(self._refresh_restart_action)
        make_horizontally_elastic(self.midi_input_card,QSizePolicy.Expanding)
        self.program_scroll, program_body = self._tab_page("preferences.tab_program")
        self.color_theme = AnimatedComboBox()
        for name in COLOR_THEMES:
            self.color_theme.addItem(tr("appearance.palette_" + name), name)
        self.color_theme.setCurrentIndex(self.color_theme.findData(owner._color_theme))
        self.color_theme.currentIndexChanged.connect(self._preview_color_theme)
        self.program_card = self._settings_card('preferences.general_title',
            ('startup_with_windows','minimize_to_tray','start_minimized','close_to_tray'),
            columns=(('startup_with_windows','minimize_to_tray'),('start_minimized','close_to_tray')))
        program_body.addWidget(self.program_card)
        general_grid=self.program_card.layout()
        self.program_card._form_start_row=2
        general_grid.addWidget(self._row('appearance.color_theme',self.color_theme),1,0,1,2)
        for column,container in enumerate(next(cols for card,cols,_ in self._settings_cards if card is self.program_card)):
            general_grid.addWidget(container,2,column)
        general_order=(self.color_theme,self.startup_with_windows,self.start_minimized,self.minimize_to_tray,self.close_to_tray)
        for left,right in zip(general_order,general_order[1:]):QWidget.setTabOrder(left,right)
        self.main_log_card=self._settings_card('preferences.main_log_title',
            ('main_log_level','main_log_detail'),columns=(('main_log_level',),('main_log_detail',)))
        program_body.addWidget(self.main_log_card)
        QWidget.setTabOrder(self.close_to_tray,self.main_log_level)
        QWidget.setTabOrder(self.main_log_level,self.main_log_detail)
        mapper_card = NativeCard()
        self.mapper_card = mapper_card
        mapper_body = set_layout(QGridLayout(mapper_card), METRICS["card_padding"], METRICS["card_content_gap"])
        mapper_body.setSizeConstraint(QLayout.SetNoConstraint)
        title=ElasticLabel(tr('player.mapper_title'));title.setObjectName('SectionTitle');mapper_body.addWidget(title,0,0,1,2)
        self.mapper_columns=tuple(WidthMeasuredPanel() for _ in range(2))
        connection,driver=(set_layout(QVBoxLayout(column),0,METRICS['compact_content_gap']) for column in self.mapper_columns)
        for index,column in enumerate(self.mapper_columns):
            column.setMinimumSize(0,0);column.layout().setSizeConstraint(QLayout.SetNoConstraint)
            mapper_body.addWidget(column,1,index,Qt.AlignTop);mapper_body.setColumnStretch(index,1)
        connection.addWidget(self._row('player.mapper_request',self.windows_midi_default,'player.mapper_detail'))
        self.mapper_install_process = None
        self._mapper_poll_timer=QTimer(self);self._mapper_poll_timer.setInterval(250)
        self._mapper_poll_timer.timeout.connect(self._poll_mapper_install)
        self.mapper_arch_status={}
        diagnostics=set_layout(QGridLayout(),0,METRICS['side_action_gap'])
        for index,(arch,key) in enumerate((('x86','player.mapper_arch_32'),('x64','player.mapper_arch_64'))):
            caption=ElasticLabel(tr(key));label=ElasticLabel();label.setWordWrap(True)
            diagnostics.addWidget(caption,index,0);diagnostics.addWidget(label,index,1)
            self.mapper_arch_status[arch]=label
        diagnostics.setColumnStretch(1,1);driver.addLayout(diagnostics)
        self.mapper_status=NativeOptionalLabel();self.mapper_status.setWordWrap(True)
        # Multi-line registration plus routing diagnosis must grow vertically;
        # Maximum caps QLabel at its preferred hint and can clip wrapped text.
        make_horizontally_elastic(self.mapper_status,QSizePolicy.Preferred);driver.addWidget(self.mapper_status)
        help_label=ElasticLabel(tr('player.mapper_startup_help'));help_label.setWordWrap(True);connection.addWidget(help_label)
        self.mapper_operation_status=NativeOptionalLabel();self.mapper_operation_status.setWordWrap(True)
        make_horizontally_elastic(self.mapper_operation_status,QSizePolicy.Preferred);driver.addWidget(self.mapper_operation_status)
        actions=set_layout(QHBoxLayout(),0,METRICS['side_action_gap'])
        self.mapper_check_button=make_action_button(tr('player.mapper_check'),self._refresh_mapper_status)
        self.mapper_check_button.setToolTip(tr('player.mapper_check_detail'))
        self.mapper_install_button=make_action_button('',self.install_mapper)
        actions.addWidget(self.mapper_check_button);actions.addWidget(self.mapper_install_button);actions.addStretch(1)
        driver.addLayout(actions)
        self.restore_driver_notice_button=make_action_button(tr('driver_notice.restore'),self._restore_driver_notice)
        self.restore_driver_notice_button.setVisible(owner._skip_driver_setup_notice)
        driver.addWidget(self.restore_driver_notice_button,alignment=Qt.AlignLeft)
        self._refresh_mapper_status()
        make_horizontally_elastic(mapper_card, QSizePolicy.Preferred)
        mapper_card.installEventFilter(self);self._reflow_mapper_card()
        self.recording_scroll, recording_body = self._tab_page("preferences.tab_recording")
        self.recording_card = self._settings_card("preferences.recording_title", ("recording_container", "recording_format", "recording_dither", "recording_rf64"),
            columns=(('recording_container','recording_format'),('recording_dither','recording_rf64')))
        recording_body.addWidget(self.recording_card)
        self.recording_codec_card = self._settings_card(None, (
            "recording_compression", "recording_vorbis_quality", "recording_rate_mode", "recording_bitrate", "recording_mp3_quality", "recording_opus_complexity"),
            columns=(('recording_compression','recording_vorbis_quality','recording_rate_mode'),('recording_bitrate','recording_mp3_quality','recording_opus_complexity')),
            subsection=True)
        self.recording_card._trailing_widgets=(self.recording_codec_card,)
        self.recording_card.layout().addWidget(self.recording_codec_card,2,0,1,2)
        self.recording_files_card = self._settings_card("preferences.recording_files_title", ('recording_basename','recording_timestamp'),
            columns=(('recording_basename',),('recording_timestamp',)))
        self.recording_files_card._form_start_row=2
        self.recording_files_card.layout().addWidget(self._row('preferences.recording_directory',self.recording_directory,
            'preferences.recording_directory_detail'),1,0,1,2)
        for column,container in enumerate(next(cols for card,cols,_ in self._settings_cards if card is self.recording_files_card)):
            self.recording_files_card.layout().addWidget(container,2,column)
        recording_body.addWidget(self.recording_files_card)
        self.recording_summary = NativeOptionalLabel()
        self.recording_summary.setWordWrap(True)
        self.recording_summary.setObjectName("MutedLabel")
        self.recording_space = NativeOptionalLabel()
        self.recording_space.setWordWrap(True)
        self.recording_space.setObjectName("MutedLabel")
        self.recording_filename = NativeOptionalLabel()
        self.recording_filename.setWordWrap(True)
        self.recording_filename.setObjectName("MutedLabel")
        self.recording_files_card.layout().addWidget(self.recording_filename,2,0,1,2)
        self.recording_files_card._trailing_widgets=(self.recording_filename,self.recording_space)
        self.recording_files_card.layout().addWidget(self.recording_space,4,0,1,2)
        self.recording_card._trailing_widgets=(self.recording_codec_card,self.recording_summary)
        self.recording_card.layout().addWidget(self.recording_summary,3,0,1,2)
        recording_body.addStretch(1)
        self.midi_input_scroll,midi_body=self._tab_page('preferences.tab_midi_input')
        program_body.addWidget(mapper_card)
        program_body.addStretch(1)
        self.midi_policy_card=self._settings_card('preferences.midi_policy_title',
            ('midi_async','auto_reset_delay_s','cc6_direct_pitch_range','cc94_custom_delay_send'),
            columns=(('midi_async','auto_reset_delay_s'),('cc6_direct_pitch_range','cc94_custom_delay_send')))
        midi_body.addWidget(self.midi_policy_card)
        self.system_reset_controls={}
        self.system_reset_card,reset_grid=make_control_section(tr('preferences.system_reset_ignore_title'))
        self.system_reset_options=WidthMeasuredPanel()
        make_horizontally_elastic(self.system_reset_options,QSizePolicy.Preferred)
        reset_options=WrappingControlLayout(self.system_reset_options,equal_cells=True,maximum_gap=48)
        for field,key,_inverted,mode in SYSTEM_RESET_RECEIVERS:
            toggle=NativeToggleSwitch(mode)
            toggle.setAccessibleName(tr('effect.ignore_system_reset',mode=mode))
            install_effect_tooltip(effect_tooltip(key),toggle)
            toggle.setChecked(not self._original_system_reset_receivers[field])
            toggle.toggled.connect(lambda ignored,field=field:self._preview_system_reset_receive(field,not ignored))
            reset_options.addWidget(toggle)
            self.system_reset_controls[field]=toggle
        reset_grid.addWidget(self.system_reset_options,0,0)
        self.midi_policy_card.layout().addWidget(self.system_reset_card,2,0,1,2)
        self.midi_policy_card._trailing_widgets=(self.system_reset_card,)
        midi_body.addWidget(self.midi_input_card,1)
        self._system_reset_connection=owner.system_reset_receive_changed.connect(self._refresh_system_reset_receive)
        self.diagnostics_scroll,diagnostics_body=self._tab_page('preferences.tab_diagnostics')
        diagnostics_body.addWidget(self.audio_buffer_card)
        self.diagnostics = NativeOutputDiagnosticsPanel(owner)
        diagnostics_body.addWidget(self.diagnostics)
        self.error_report=NativeErrorReportPanel(owner,self)
        self.error_report_scroll,error_body=self._tab_page('error_report.title')
        error_body.addWidget(self.error_report)
        # Reorder existing pages without recreating any controls or owners.
        pages=(self.program_scroll,self.scroll,self.midi_input_scroll,self.recording_scroll,self.diagnostics_scroll,self.error_report_scroll)
        for target,page in enumerate(pages):
            self.tabs.tabBar().moveTab(self.tabs.indexOf(page),target)
        self.tabs.setCurrentWidget(self.program_scroll)
        root.addWidget(self.tabs, 1)
        self.restart_legend=ElasticLabel(tr('preferences.restart_legend'));self.restart_legend.setObjectName('MutedLabel')
        make_horizontally_elastic(self.restart_legend);root.addWidget(self.restart_legend)
        self.tabs.currentChanged.connect(self._sync_restart_legend)
        self._sync_restart_legend()

        # Reparenting must preserve visual task order in keyboard navigation.
        # MIDI policy appears before the port editor,although the port editor
        # is constructed first. No domain callbacks are involved here.
        midi_order=(self.midi_timing,self.cc6_direct_pitch_range,self.auto_reset_delay_s,
            self.cc94_custom_delay_send,*self.system_reset_controls.values(),
            self.midi_ports.selector,self.midi_ports.add,self.midi_ports.remove,
            self.midi_ports.name,self.midi_ports.virtual_in,self.midi_ports.virtual_out,
            *self.midi_ports.devices.values(),self.midi_ports.refresh_button)
        for left,right in zip(midi_order,midi_order[1:]):QWidget.setTabOrder(left,right)
        QWidget.setTabOrder(self.main_log_detail,self.windows_midi_default)

        footer = set_layout(QHBoxLayout(), 0, METRICS["side_action_gap"])
        self.restore_defaults_button=make_action_button(tr('common.restore_defaults'),self.restore_defaults)
        self.import_bmsynth_button=make_action_button(tr('preferences.import_bmsynth'),self.import_bmsynth)
        footer.addWidget(self.restore_defaults_button);footer.addWidget(self.import_bmsynth_button)
        footer.addStretch(1)
        self.save_restart_button = make_action_button(tr("preferences.save_restart"), self.save_restart)
        self.save_restart_button.hide()
        footer.addWidget(self.save_restart_button)
        self.save_close_button = make_action_button(tr("preferences.save_close"), self.save_close, primary=True)
        self.save_close_button.setDefault(True)
        footer.addWidget(self.save_close_button)
        footer.addWidget(make_action_button(tr("preferences.cancel"), self.reject))
        for index in range(footer.count()):
            button = footer.itemAt(index).widget()
            if button is not None:
                button.setMinimumWidth(0)
                button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        root.addLayout(footer)
        self.set_values(owner._pending_engine_preferences)
        # Live controls preview; no INI or startup-registration write until Save.
        self._loading = False
        for name in RESTART_REQUIRED_FIELDS:
            field = self._controls[name][0]
            signal = (field.toggled if isinstance(field, QCheckBox) else
                      field.currentIndexChanged if isinstance(field, AnimatedComboBox) else field.valueChanged)
            signal.connect(self._refresh_restart_action)
        self._refresh_restart_action()
        self.recording_format.currentIndexChanged.connect(self._sync_recording_controls)
        self.recording_container.currentIndexChanged.connect(self._sync_recording_controls)
        self.recording_rate_mode.currentIndexChanged.connect(self._sync_recording_controls)
        self.tabs.currentChanged.connect(self._sync_recording_controls)
        self._sync_recording_controls()
        for name in LIVE_PREFERENCE_FIELDS:
            if name == "windows_midi_default":
                continue  # System-facing preference crosses Save, not live editing.
            field = self._controls[name][0]
            signal = (field.toggled if isinstance(field, QCheckBox) else
                      field.currentIndexChanged if isinstance(field, AnimatedComboBox) else
                      field.textChanged if isinstance(field, QLineEdit) else
                      self.recording_directory_edit.textChanged if name == "recording_directory" else field.valueChanged)
            signal.connect(self._preview_live)
        apply_native_text_metrics(self)
        for rows in self._form_columns:
            self._fit_label_column(rows)
        for card, _columns, _single in self._settings_cards:
            self._reflow_settings_card(card)
        self._recording_info_timer = QTimer(self)
        self._recording_info_timer.setInterval(2000)
        self._recording_info_timer.timeout.connect(self._update_recording_info)
        self.tabs.currentChanged.connect(self._recording_tab_changed)
        self.tabs.currentChanged.connect(self._mapper_tab_changed)
        for field in (self.recording_container, self.recording_format, self.recording_bitrate, self.recording_rate_mode):
            field.currentIndexChanged.connect(self._update_recording_info)
        self.recording_directory_edit.textChanged.connect(self._update_recording_info)
        self.recording_basename.textChanged.connect(self._update_recording_info)
        self.recording_timestamp.toggled.connect(self._update_recording_info)
        self._update_recording_info()
        # OutlineCheckBox paints from its own palette; inheriting the
        # owner's stylesheet alone does not update that palette.
        palette = widget_palette(owner)
        for checkbox in self.findChildren(OutlineCheckBox):
            checkbox.set_theme(palette)
        self._refresh_theme_icons()
        owner._system_reset_persistence_baseline=dict(self._original_system_reset_receivers)
        self._fit_pending = False
        self._opening_fit = True
        self._window_motion = NativeWindowSizeMotion(self)
        self.tabs.currentChanged.connect(self._queue_content_fit)
        self.error_report.captureChanged.connect(self._queue_content_fit)

    def _restore_driver_notice(self):
        if self.owner.set_driver_notice_suppressed(False):
            self.restore_driver_notice_button.hide()

    def _refresh_midi_input_ports(self,*_args):
        self.midi_ports.refresh_devices()

    def _queue_content_fit(self, *_args):
        if not self.isVisible() or getattr(self, "_fit_pending", False):
            return
        self._fit_pending = True
        QTimer.singleShot(0, self._fit_current_page)

    def _fit_current_page(self):
        self._fit_pending = False
        if not self.isVisible() or not automatic_window_fit_allowed(self):
            return
        page = self.tabs.currentWidget()
        if page is None:
            return
        width = self.width()
        root = self.layout()
        margins = root.contentsMargins()
        body_width = max(0, width-margins.left()-margins.right()-2)
        if isinstance(page, QScrollArea):
            layout = page.widget().layout()
            body_height = layout.totalHeightForWidth(body_width) if layout.hasHeightForWidth() else layout.totalSizeHint().height()
            body_height += 2*page.frameWidth()
        else:
            body_height = page.heightForWidth(body_width) if page.hasHeightForWidth() else page.sizeHint().height()
        height = body_height + self.tabs.tabBar().sizeHint().height()+4 + margins.top()+margins.bottom()
        visible = 0
        for index in range(root.count()):
            item = root.itemAt(index)
            widget = item.widget()
            if widget is self.tabs:
                visible += 1
            elif widget is not None and not widget.isHidden():
                visible += 1
                height += widget.heightForWidth(body_width) if widget.hasHeightForWidth() else widget.sizeHint().height()
            elif item.layout() is not None:
                visible += 1
                height += item.layout().totalSizeHint().height()
        height += max(0, visible-1)*root.spacing()
        if QApplication.platformName() != "offscreen":
            frame = self.frameGeometry().size()-self.size()
            height = min(height, self.screen().availableGeometry().height()-frame.height())
        target = QSize(width, max(1, height) if self._opening_fit else max(self.height(),height))
        if self._opening_fit:
            self.resize(target)
            self._opening_fit = False
        elif target != self.size():
            self._window_motion.start(target, expanding=True)
        root.activate()

    def _settings_card(self, title_key, names, *, single_column=True, columns=None,subsection=False):
        card = QFrame() if subsection else NativeCard()
        card._preference_fields=tuple(names)
        if subsection:card.setObjectName('PreferencesSubsection')
        layout = set_layout(QGridLayout(card), 0 if subsection else METRICS["card_padding"], METRICS["card_content_gap"])
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        card._form_start_row=1 if title_key else 0
        card._group_heading=None
        if title_key:
            title = ElasticLabel(tr(title_key));title.setObjectName('SectionTitle')
            layout.addWidget(title,0,0,1,2);card._group_heading=title
        field_columns=columns if columns is not None else (names,)
        if tuple(name for fields in field_columns for name in fields)!=tuple(names):
            raise ValueError('Preference columns must cover the ordered fields exactly once')
        columns = []
        for column, fields in enumerate(field_columns):
            if not fields:
                continue
            container = QWidget()
            container.setMinimumWidth(0)
            rows = set_layout(QVBoxLayout(container), 0, CARD_ROW_GAPS["preferences"])
            rows.setAlignment(Qt.AlignTop)
            for name in fields:
                field, key, detail = self._controls[name]
                rows.addWidget(self._row("preferences." + key, field, "preferences." + detail,
                    restart_required=name in RESTART_REQUIRED_FIELDS))
            self._form_columns.append(rows)
            columns.append(container)
            layout.addWidget(container, card._form_start_row, column, Qt.AlignTop)
            layout.setColumnStretch(column, 1)
        if len(field_columns)>1:
            reading_order=[fields[row] for row in range(max(map(len,field_columns)))
                           for fields in field_columns if row<len(fields)]
            for left,right in zip(reading_order,reading_order[1:]):
                QWidget.setTabOrder(self._controls[left][0],self._controls[right][0])
        make_horizontally_elastic(card, QSizePolicy.Fixed)
        self._settings_cards.append((card, columns, single_column and len(columns)==1))
        card.installEventFilter(self)
        return card

    def eventFilter(self, watched, event):
        if event.type()==QEvent.Resize and watched is getattr(self,'mapper_card',None):self._reflow_mapper_card()
        if event.type() == QEvent.Resize and any(watched is card for card, *_ in getattr(self, "_settings_cards", ())):
            self._reflow_settings_card(watched)
        return super().eventFilter(watched, event)

    def _reflow_mapper_card(self):
        # About22 text cells per column keeps help readable at normal size.
        # This is a shared reflow breakpoint,not a window/column width floor.
        width=self.fontMetrics().horizontalAdvance('가')*22
        reflow_card_columns(self.mapper_card,self.mapper_columns,(width,width))

    def _reflow_settings_card(self, card):
        """Content-measured hints, not fixed columns or a dialog width floor."""
        _card, columns, single = next(item for item in self._settings_cards if item[0] is card)
        widths = []
        for container in columns:
            preferred = 0
            for i in range(container.layout().count()):
                row = container.layout().itemAt(i).widget()
                if row.isHidden():
                    continue
                label = row.layout().itemAtPosition(0, 0).widget()
                field = row.layout().itemAtPosition(0, 1).widget()
                control_width = (max((field.fontMetrics().horizontalAdvance(field.itemText(j)) for j in range(field.count())), default=0) + 40
                                 if isinstance(field, AnimatedComboBox) else field.sizeHint().width() if isinstance(field,QCheckBox) else 96)
                preferred = max(preferred, label.sizeHint().width() + control_width + METRICS["form_column_gap"])
                badge=row.layout().itemAtPosition(0,2)
                if badge:preferred=max(preferred,label.sizeHint().width()+control_width+badge.widget().sizeHint().width()+2*METRICS['form_column_gap'])
            widths.append(preferred)
        start=getattr(card,'_form_start_row',1)
        stacked=reflow_card_columns(card,columns,widths,force_stack=single,start_row=start)
        for index,widget in enumerate(getattr(card,'_trailing_widgets',())):
            card.layout().removeWidget(widget)
            card.layout().addWidget(widget,start+(len(columns) if stacked else 1)+index,0,1,2)
        # A wrapping sibling (mapper explanation) makes the scroll layout use
        # heightForWidth. Zero-minimum column containers then allowed that
        # calculation to squeeze 28px fixed rows on top of one another. Only
        # the vertical content extent is constrained; horizontal floors remain0.
        for container in columns:
            rows = container.layout()
            visible = [rows.itemAt(i).widget() for i in range(rows.count())
                       if not rows.itemAt(i).widget().isHidden()]
            container.setMinimumHeight(sum(row.height() for row in visible)
                + max(0, len(visible)-1)*rows.spacing())
        card.layout().invalidate()
        # Keep fixed control rows admitted through their column minima.
        # Do not cache the wrapping subsection's CURRENT height as a card
        # minimum:after widening it would retain the previous narrow height.
        card.setMinimumHeight(0)
        card.updateGeometry()

    def _tab_page(self, title):
        scroll = QScrollArea()
        scroll.setObjectName("PreferencesScroll")
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumSize(0, 0)
        content = WidthMeasuredPanel()
        content.setObjectName("PreferencesContent")
        content.setMinimumSize(0, 0)
        body = bordered_page_layout(QVBoxLayout(content),METRICS['section_gap'])
        body.setSizeConstraint(QLayout.SetNoConstraint)
        scroll.setWidget(content)
        self.tabs.addTab(scroll, tr(title))
        return scroll, body

    def _sync_recording_controls(self, *_args):
        busy = ae.runtime.output_analysis.has_draft
        kind = self.recording_container.currentData()
        integer = kind in ("flac", "ogg_flac")
        pcm = kind in ("wav", "aiff")
        sync_recording_formats(self.recording_format,kind)
        self.recording_format.setEnabled(not busy and (pcm or integer))
        self.recording_format.parentWidget().setVisible(pcm or integer)
        self.recording_container.setEnabled(not busy)
        self.recording_rf64.parentWidget().setEnabled(not busy and kind == "wav")
        self.recording_dither.parentWidget().setEnabled(not busy and (pcm or integer) and self.recording_format.currentData() != "float32")
        self.recording_codec_card.setVisible(not pcm)
        mode = self.recording_rate_mode.currentData()
        visible = {"recording_compression": integer, "recording_vorbis_quality": kind == "vorbis",
                   "recording_rate_mode": kind in ("mp3", "opus"), "recording_bitrate": kind == "opus" or kind == "mp3" and mode == "cbr",
                   "recording_mp3_quality": kind == "mp3" and mode == "vbr", "recording_opus_complexity": kind == "opus"}
        for name, show in visible.items():
            field = self._controls[name][0]
            field.parentWidget().setVisible(show)
            field.setEnabled(not busy)
        self._reflow_settings_card(self.recording_codec_card)
        self._reflow_settings_card(self.recording_card)
        self.recording_card.setToolTip(tr("recording.settings_locked") if busy else "")
        # Codec-dependent rows change the current page's height without a tab
        # switch. Reuse the existing grow-only fit rather than retaining the
        # previous codec's too-short viewport.
        self._queue_content_fit()

    def _finish_recording_basename(self):
        if not valid_recording_basename(self.recording_basename.text()):
            self.recording_basename.setText(self.owner._engine_preferences.recording_basename)
            self.recording_basename.setToolTip(tr("recording.basename_invalid"))
        else:
            self.recording_basename.setToolTip(tooltip_display_text(tr("preferences.recording_basename_detail")))

    def _choose_recording_directory(self):
        selected = QFileDialog.getExistingDirectory(self, tr("recording.folder_title"), self.recording_directory_edit.text())
        if selected:
            self.recording_directory_edit.setText(selected)

    def _recording_tab_changed(self, *_args):
        if self.isVisible() and self.tabs.currentWidget() is self.recording_scroll:
            self._update_recording_info()
            self._recording_info_timer.start()
        else:
            self._recording_info_timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        if self._system_reset_connection is None:
            self._original_system_reset_receivers=self.owner.system_reset_receivers()
            for field,receive in self._original_system_reset_receivers.items():self._refresh_system_reset_receive(field,receive)
            self.owner._system_reset_persistence_baseline=dict(self._original_system_reset_receivers)
            self._system_reset_connection=self.owner.system_reset_receive_changed.connect(self._refresh_system_reset_receive)
        self._recording_tab_changed()
        self._mapper_tab_changed()
        self._queue_content_fit()

    def hideEvent(self, event):
        self._recording_info_timer.stop()
        super().hideEvent(event)

    def _update_recording_info(self, *_args):
        # Do not query BASS or a destination disk merely to open Audio settings.
        # Network/removable recording folders may block filesystem calls.
        if not self.isVisible() or self.tabs.currentWidget() is not self.recording_scroll:
            return
        from bass_bindings import BASS_CHANNELINFO, bass
        import ctypes
        recorder = ae.runtime.output_analysis
        info = BASS_CHANNELINFO()
        if recorder.stream and bass.BASS_ChannelGetInfo(recorder.stream, ctypes.byref(info)):
            rate, channels = info.freq, info.chans
            source = tr("recording.source_value", rate=rate, channels=channels)
        else:
            rate, channels = self.owner._engine_preferences.sample_rate, 2
            source = tr("recording.source_pending", rate=rate, channels=channels)
        bits = {"float32": 32, "pcm32": 32, "pcm24": 24, "pcm16": 16}.get(self.recording_format.currentData(), 32)
        mb = rate * channels * (bits // 8) * 60 / 1_000_000
        kind = self.recording_container.currentData()
        if kind in ("opus", "mp3"):
            from settings import recording_output_rate
            encoded_rate = recording_output_rate(kind, rate)
            if encoded_rate != rate:
                source = tr("recording.source_converted", source_rate=rate, rate=encoded_rate, channels=channels)
        if kind in ("mp3", "opus", "vorbis"):
            variable = kind == "vorbis" or self.recording_rate_mode.currentData() == "vbr"
            mb = (self.recording_bitrate.currentData() or 192) * 1000 * 60 / 8 / 1_000_000
            if variable:
                summary = tr("recording.summary_variable", source=source)
            else:
                summary = tr("recording.summary", source=source, mb=mb)
        elif kind in ("flac", "ogg_flac"):
            summary = tr("recording.summary_lossless", source=source, mb=mb)
        else:
            summary = tr("recording.summary", source=source, mb=mb)
        folder = self.recording_directory_edit.text() or QStandardPaths.writableLocation(QStandardPaths.MusicLocation)
        folder, space = self._recording_space_snapshot(folder)
        from settings import recording_rate_supported
        if recording_rate_supported(kind,rate) is False:
            summary=tr('recording.rate_unsupported',rate=rate,format=self.recording_container.currentText())
        self.recording_summary.setText(summary)
        self.recording_summary.setToolTip(tooltip_display_text(tr("recording.source_detail") + "\n" + summary))
        self.recording_space.setText(space)
        self.recording_space.setToolTip(tooltip_display_text(tr('recording.space_locations',
            temporary=tempfile.gettempdir(),destination=folder)))
        extension = recorder.recording_container if recorder.has_draft else self.recording_container.currentData()
        filename = recording_filename(self._values(), container=extension, started=recorder.recording_date if recorder.has_draft else None)
        self.recording_filename.setText(tr("recording.filename_preview", filename=filename))
        self.recording_filename.setToolTip(str(Path(folder) / filename))

    def _recording_space_snapshot(self, requested):
        """Nonblocking presentation of at most one immutable disk query.

        A stuck network disk must not keep a Qt worker/parent alive at exit.
        The daemon captures only strings and its result queue, never a widget.
        The visible-page timer consumes results; hidden pages do no polling.
        """
        request=getattr(self,'_recording_space_request',None)
        if request is not None:
            thread,results=request
            if not thread.is_alive():
                try:self._recording_space_cache=results.get_nowait()
                except queue.Empty:pass
                self._recording_space_request=None
                request=None
        cached=getattr(self,'_recording_space_cache',None)
        last_key,last_at=getattr(self,'_recording_space_last_query',('',-math.inf))
        if request is None and (requested!=last_key or time.monotonic()-last_at>=2):
            self._recording_space_last_query=(requested,time.monotonic())
            temporary=tempfile.gettempdir()
            fallback=QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
            results=queue.SimpleQueue()
            def query(folder=requested):
                try:
                    if not Path(folder).is_dir():folder=fallback
                    temp_free=shutil.disk_usage(temporary).free/1_000_000_000
                    target_free=shutil.disk_usage(folder).free/1_000_000_000
                    result=(requested,folder,temp_free,target_free)
                except (OSError,ValueError):result=(requested,folder,None,None)
                results.put(result)
            thread=threading.Thread(target=query,name='ABMSynth-recording-space',daemon=True)
            self._recording_space_request=(thread,results)
            thread.start()
        if cached is None or cached[0]!=requested:
            return requested,tr('recording.space_unknown')
        _key,folder,temp_free,target_free=cached
        return folder,(tr('recording.space',temp=temp_free,target=target_free)
                       if temp_free is not None and target_free is not None else tr('recording.space_unknown'))

    def _preview_color_theme(self, *_args):
        self.owner.set_color_theme(self.color_theme.currentData())
        for checkbox in self.findChildren(OutlineCheckBox):
            checkbox.set_theme(widget_palette(self.owner))
        self.owner._queue_ui_save()

    def _refresh_theme_icons(self):
        for index, name in enumerate(COLOR_THEMES):
            swatch = QPixmap(14, 14)
            swatch.fill(QColor(color_palette(self.owner._dark, name).accent))
            self.color_theme.setItemIcon(index, QIcon(swatch))

    def _refresh_system_reset_receive(self,field,receive):
        control=self.system_reset_controls[field]
        blocked=control.blockSignals(True)
        try:control.setChecked(not receive)
        finally:control.blockSignals(blocked)

    def _preview_system_reset_receive(self,field,receive):
        try:self.owner.set_system_reset_receive(field,receive)
        except (OSError,ValueError,RuntimeError) as error:
            self._refresh_system_reset_receive(field,self.owner.system_reset_receivers()[field])
            self._show_apply_failure(error)

    @staticmethod
    def _row(key, field, detail_key=None,*,label_text=None,detail_text=None,restart_required=False):
        row = QWidget()
        row.setFixedHeight(FORM_ROW_HEIGHT)
        grid = set_layout(QGridLayout(row), 0, METRICS["form_column_gap"])
        grid.setColumnStretch(1, 1)
        caption=tr(key) if label_text is None else label_text
        label = ElasticLabel(tr('preferences.restart_caption',label=caption) if restart_required else caption, preferred_width=METRICS["form_label_width"])
        label.setObjectName("FieldLabel")
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        field.setAccessibleName(tr(key) if label_text is None else label_text)
        if isinstance(field, AnimatedComboBox):
            field.setProperty("fitPreferenceContents", True)
        make_horizontally_elastic(field)
        grid.addWidget(label, 0, 0)
        grid.addWidget(field, 0, 1)
        if detail_key or detail_text is not None:
            explanation = tr(detail_key) if detail_text is None else detail_text
            rendered = tooltip_display_text(explanation)
            for widget in (row, label, field):
                widget.setToolTip(rendered)
                widget.setAccessibleDescription(explanation)
        if restart_required:
            field.setAccessibleDescription(tr('preferences.restart_accessible',description=field.accessibleDescription()))
        return row

    def _sync_restart_legend(self,*_args):
        self.restart_legend.setVisible(self.tabs.currentWidget() in (self.scroll,self.midi_input_scroll,self.diagnostics_scroll))

    @staticmethod
    def _fit_label_column(rows):
        """Size a settings column from its translated captions, not a guess."""
        labels = []
        for index in range(rows.count()):
            item = rows.itemAt(index)
            row = item.widget() if item else None
            grid = row.layout() if row else None
            caption = grid.itemAtPosition(0, 0) if isinstance(grid, QGridLayout) else None
            label = caption.widget() if caption else None
            if isinstance(label, ElasticLabel):
                labels.append(label)
        if labels:
            align_form_labels(labels, METRICS['form_label_width'], METRICS['form_column_gap'])

    def set_values(self, values: EnginePreferences):
        self.sample_rate.setCurrentIndex(self.sample_rate.findData(values.sample_rate))
        self.synthesis_mode.setChecked(values.synthesis_mode == "high_performance")
        self.buffer_ms.setValue(values.buffer_ms)
        self.shared_update_ms.setValue(values.shared_update_ms)
        self.exclusive_block_ms.setValue(values.exclusive_block_ms)
        self.voices.setValue(values.voices)
        self.midi_cpu_limit.setCurrentIndex(self.midi_cpu_limit.findData(values.midi_cpu_limit))
        self.soundfont_stereo.setCurrentIndex(self.soundfont_stereo.findData(values.soundfont_stereo))
        sync_recording_formats(self.recording_format,values.recording_container)
        index=self.recording_format.findData(values.recording_format)
        self.recording_format.setCurrentIndex(index if index>=0 else self.recording_format.findData('pcm24'))
        self.recording_dither.setChecked(values.recording_dither)
        self.recording_rf64.setChecked(values.recording_rf64)
        self.recording_container.setCurrentIndex(self.recording_container.findData(values.recording_container))
        self.recording_directory_edit.setText(values.recording_directory)
        self.recording_basename.setText(values.recording_basename)
        self.recording_timestamp.setChecked(values.recording_timestamp)
        for name in ("recording_compression", "recording_vorbis_quality", "recording_mp3_quality", "recording_opus_complexity"):
            getattr(self, name).setValue(getattr(values, name))
        for name in ("recording_bitrate", "recording_rate_mode"):
            field = getattr(self, name)
            field.setCurrentIndex(field.findData(getattr(values, name)))
        self.interpolation.setCurrentIndex(self.interpolation.findData(values.interpolation))
        self.midi_timing.setCurrentIndex(self.midi_timing.findData(values.midi_async))
        self.loading_mode.setCurrentIndex(self.loading_mode.findData(values.loading_mode))
        self.default_reverb_send.setValue(values.default_reverb_send)
        self.soundfont_fx_policy.setCurrentIndex(self.soundfont_fx_policy.findData(values.soundfont_fx_policy))
        self.priority.setCurrentIndex(self.priority.findData(values.priority))
        self.auto_reset_delay_s.setValue(values.auto_reset_delay_s)
        self.cc6_direct_pitch_range.setChecked(values.cc6_direct_pitch_range)
        self.cc94_custom_delay_send.setChecked(values.cc94_custom_delay_send)
        self.startup_with_windows.setChecked(values.startup_with_windows)
        self.start_minimized.setChecked(values.start_minimized)
        self.minimize_to_tray.setChecked(values.minimize_to_tray)
        self.close_to_tray.setChecked(values.close_to_tray)
        self.windows_midi_default.setChecked(values.windows_midi_default)
        self.main_log_level.setCurrentIndex(self.main_log_level.findData(values.main_log_level))
        self.main_log_detail.setCurrentIndex(self.main_log_detail.findData(values.main_log_detail))
        self._refresh_restart_action()

    def _sync_output_cadence_controls(self, *_args):
        states=output_timing_availability(self.owner.selected_output_device())
        if set(states)!=set(self.audio_buffer_card._preference_fields):
            raise ValueError('Output timing policy field inventory')
        for name,active in states.items():
            getattr(self,name).parentWidget().setEnabled(active)
        self._reflow_settings_card(self.audio_buffer_card)

    def _restart_changes(self):
        values = self._values()
        changed=tuple(name for name in RESTART_REQUIRED_FIELDS
                     if getattr(values, name) != getattr(self.owner._engine_preferences, name))
        if hasattr(self,'midi_ports'):
            try:
                if self.midi_ports.settings().serialized()!=self.owner.midi_input_runtime.port_settings.serialized():changed+=('midi_ports',)
            except ValueError:changed+=('midi_ports',)
        return changed

    def _refresh_restart_action(self, *_args):
        if not hasattr(self, "save_restart_button"):
            return
        changed = self._restart_changes()
        self.save_restart_button.setVisible(bool(changed))
        set_button_primary(self.save_close_button, not changed, default=not changed)
        set_button_primary(self.save_restart_button, bool(changed), default=bool(changed))
        self.save_restart_button.setToolTip(tr("preferences.restart_required_for",
            settings="\n".join(tr('midi_ports.title') if name=='midi_ports' else tr("preferences." + self._controls[name][1]) for name in changed)) if changed else "")

    def restore_defaults(self):
        defaults = EnginePreferences()
        if ae.runtime.output_analysis.has_draft:
            current = self._values()
            defaults = replace(defaults, **{name: getattr(current, name) for name in LIVE_PREFERENCE_FIELDS
                if name.startswith("recording_") and name not in ("recording_directory", "recording_basename", "recording_timestamp")})
        self._loading = True
        try:
            self.set_values(defaults)
            from settings import MidiPortSettings
            self.midi_ports.rows=[dict(row) for row in MidiPortSettings().normalized().ports]
            self.midi_ports.index=0;self.midi_ports._show_row()
            for field,*_ in SYSTEM_RESET_RECEIVERS:self.owner.set_system_reset_receive(field,True)
        finally:
            self._loading = False
        self._mark_dirty()
        self._refresh_restart_action()
        self._preview_live()
        self.color_theme.setCurrentIndex(self.color_theme.findData("blue"))

    def _values(self) -> EnginePreferences:
        return EnginePreferences(
            main_log_level=str(self.main_log_level.currentData()),
            main_log_detail=str(self.main_log_detail.currentData()),
            sample_rate=int(self.sample_rate.currentData()),
            synthesis_mode="high_performance" if self.synthesis_mode.isChecked() else "compatible",
            buffer_ms=self.buffer_ms.value(),
            shared_update_ms=self.shared_update_ms.value(),
            exclusive_block_ms=self.exclusive_block_ms.value(),
            voices=self.voices.value(),
            midi_cpu_limit=int(self.midi_cpu_limit.currentData()),
            soundfont_stereo=bool(self.soundfont_stereo.currentData()),
            recording_format=str(self.recording_format.currentData()),
            recording_dither=self.recording_dither.isChecked(),
            recording_rf64=self.recording_rf64.isChecked(),
            recording_container=str(self.recording_container.currentData()),
            recording_directory=self.recording_directory_edit.text(),
            recording_basename=self.recording_basename.text() if valid_recording_basename(self.recording_basename.text()) else self.owner._engine_preferences.recording_basename,
            recording_timestamp=self.recording_timestamp.isChecked(),
            recording_compression=self.recording_compression.value(),
            recording_vorbis_quality=self.recording_vorbis_quality.value(),
            recording_mp3_quality=self.recording_mp3_quality.value(),
            recording_opus_complexity=self.recording_opus_complexity.value(),
            recording_bitrate=int(self.recording_bitrate.currentData()),
            recording_rate_mode=str(self.recording_rate_mode.currentData()),
            interpolation=int(self.interpolation.currentData()),
            midi_async=bool(self.midi_timing.currentData()),
            default_reverb_send=self.default_reverb_send.value(),
            soundfont_fx_policy=str(self.soundfont_fx_policy.currentData()),
            priority=str(self.priority.currentData()),
            auto_reset_delay_s=self.auto_reset_delay_s.value(),
            cc6_direct_pitch_range=self.cc6_direct_pitch_range.isChecked(),
            cc94_custom_delay_send=self.cc94_custom_delay_send.isChecked(),
            loading_mode=int(self.loading_mode.currentData()),
            startup_with_windows=self.startup_with_windows.isChecked(),
            start_minimized=self.start_minimized.isChecked(),
            minimize_to_tray=self.minimize_to_tray.isChecked(),
            close_to_tray=self.close_to_tray.isChecked(),
            windows_midi_default=self.windows_midi_default.isChecked(),
        )

    def _preview_live(self, *_args):
        if self._loading:
            return True
        if self.priority.currentData()=='realtime' and self.owner._engine_preferences.priority!='realtime':
            prompt=NativeMessageBox(self)
            prompt.setWindowTitle(tr('preferences.priority'))
            prompt.setIcon(QMessageBox.Warning)
            prompt.setText(tr('preferences.priority_realtime_confirm'))
            proceed=prompt.addButton(tr('common.apply'),QMessageBox.AcceptRole)
            cancel=prompt.addButton(tr('common.cancel'),QMessageBox.RejectRole)
            set_button_primary(proceed,True)
            prompt.setDefaultButton(cancel);prompt.setEscapeButton(cancel)
            try:
                prompt.exec()
                accepted=prompt.clickedButton() is proceed
            finally:prompt.deleteLater()
            if not accepted:
                blocked=self.priority.blockSignals(True)
                self.priority.setCurrentIndex(self.priority.findData(self.owner._engine_preferences.priority))
                self.priority.blockSignals(blocked)
                return False
        try:
            self.owner.apply_live_preferences(self._values())
        except (AudioStartError, SoundFontRuntimeError, OSError, ValueError) as exc:
            # Keep failed controls honest: the form must reflect actual live
            # state, while preserving unrelated staged restart choices.
            current = self._values()
            self._loading = True
            try:
                self.set_values(replace(current, **{
                    name: getattr(self.owner._engine_preferences, name)
                    for name in LIVE_PREFERENCE_FIELDS
                }))
            finally:
                self._loading = False
            self._show_apply_failure(exc)
            return False
        return True

    def done(self, result):
        if self.error_report.busy:
            self.error_report.status.setText(tr('error_report.busy_close'))
            return
        if hasattr(self, "_window_motion"):
            self._window_motion.settle()
        if result == QDialog.Rejected:
            try:
                self.owner.apply_live_preferences(self._original_live_preferences)
                if self._system_reset_connection is not None:
                    for field,receive in self._original_system_reset_receivers.items():
                        if self.owner.set_system_reset_receive(field,receive) is False:
                            raise RuntimeError(tr('runtime.channel_executor_retained'))
            except (AudioStartError, SoundFontRuntimeError, OSError, ValueError,RuntimeError) as exc:
                self._show_apply_failure(exc)
                return
            self.owner._color_theme = self._original_color_theme
            self.owner._queue_ui_save()
        if self._system_reset_connection is not None:
            self.owner._system_reset_persistence_baseline=None
            QObject.disconnect(self._system_reset_connection);self._system_reset_connection=None
        self.owner._queue_ui_save()
        self.diagnostics.timer.stop()
        self._recording_info_timer.stop()
        self.diagnostics.analysis.stop_measurement()
        super().done(result)

    def _show_apply_failure(self, error):
        if isinstance(error, AudioStartError) and error.stage in self._controls:
            caption = self._controls[error.stage][1]
            message = tr("preferences.apply_api_failed", setting=tr("preferences." + caption), code=error.code)
        else:
            message = tr("preferences.apply_failed", error=error)
        NativeMessageBox.warning(self, tr("preferences.title"), message)

    def _mark_dirty(self, *_args):
        if not self._loading:
            self._dirty = True

    def _save(self, restart=False):
        try:port_settings=self.midi_ports.settings()
        except ValueError as error:
            NativeMessageBox.warning(self,tr('preferences.title'),str(error));return
        values = self._values().validate()
        if values.sample_rate != self.owner._engine_preferences.sample_rate:
            try:
                self.owner.audio_session.check_output_rate(values.sample_rate)
            except AudioStartError as error:
                NativeMessageBox.warning(self,tr('preferences.title'),
                    tr('preferences.output_rate_unavailable',rate=values.sample_rate,code=error.code))
                return  # No INI write, preview change, close or restart.
        changes = self._restart_changes()
        restart = restart and bool(changes)
        if changes and not restart:
            decision = self._confirm_unapplied_save(changes)
            if decision is None:
                return
            restart = decision
        if not self._preview_live():
            return
        old_startup = is_windows_startup_enabled()
        try:
            if old_startup != values.startup_with_windows:
                set_windows_startup_enabled(values.startup_with_windows)
            engine_values=engine_ini_values(values)
            self.owner._settings_writer.submit({'Engine':engine_values,'MIDI Ports':{'Configuration':port_settings.serialized()}},
                owned_keys={'Engine':set(engine_values),'MIDI Ports':{'Configuration'}})
            self.owner._settings_writer.flush()  # Explicit Save: durable before close/restart.
            if not os.environ.get("ABMSYNTH_UI_ONLY"):
                from windows_integration import set_midi_mapper_enabled
                set_midi_mapper_enabled(values.windows_midi_default)
        except (OSError, ValueError) as exc:
            if old_startup != values.startup_with_windows:
                try:
                    set_windows_startup_enabled(old_startup)
                except OSError:
                    pass
            NativeMessageBox.warning(self, tr("preferences.title"), tr("preferences.save_failed", error=exc))
            return
        self.owner._pending_engine_preferences = values
        # Live state already reflects the form; only creation/launch values
        # stay pending until the next launch. No stream reset occurs here.
        self.owner._ui_save_timer.stop()
        policy_baseline=self.owner._system_reset_persistence_baseline
        self.owner._system_reset_persistence_baseline=None
        self.owner._save_ui_values()
        try:
            self.owner._settings_writer.flush()
        except (OSError, ValueError) as exc:
            self.owner._system_reset_persistence_baseline=policy_baseline
            NativeMessageBox.warning(self, tr("preferences.title"), tr("preferences.save_failed", error=exc))
            return
        self.accept()
        if restart:
            QTimer.singleShot(0, self.owner.restart)

    def save_close(self):
        self._save(False)

    def save_restart(self):
        self._save(True)

    def _confirm_unapplied_save(self, changes):
        prompt = NativeMessageBox(self)
        prompt.setWindowTitle(tr("preferences.unapplied_title"))
        prompt.setIcon(QMessageBox.Warning)
        prompt.setText(tr("preferences.unapplied_body", settings="\n".join(
            tr('midi_ports.title') if name=='midi_ports' else tr("preferences." + self._controls[name][1]) for name in changes)))
        now = prompt.addButton(tr("preferences.save_restart"), QMessageBox.AcceptRole)
        later = prompt.addButton(tr("preferences.save_next_launch"), QMessageBox.ActionRole)
        cancel = prompt.addButton(tr("common.cancel"), QMessageBox.RejectRole)
        prompt.setDefaultButton(cancel)
        prompt.setEscapeButton(cancel)
        try:
            prompt.exec()
            if prompt.clickedButton() is now:
                return True
            if prompt.clickedButton() is later:
                return False
            return None
        finally:
            prompt.deleteLater()

    def _choose_bmsynth_extensions(self):
        """True clears native extensions, False keeps them, None aborts."""
        choice = NativeMessageBox(self)
        choice.setWindowTitle(tr("preferences.import_extensions_title"))
        choice.setText(tr("preferences.import_extensions_body"))
        choice.setIcon(QMessageBox.Question)
        neutralize = choice.addButton(tr("preferences.import_extensions_neutralize"), QMessageBox.AcceptRole)
        keep = choice.addButton(tr("preferences.import_extensions_keep"), QMessageBox.ActionRole)
        cancel = choice.addButton(tr("common.cancel"), QMessageBox.RejectRole)
        set_button_primary(neutralize,True)
        choice.setDefaultButton(keep)
        choice.setEscapeButton(cancel)
        choice.exec()
        if choice.clickedButton() is neutralize:
            return True
        if choice.clickedButton() is keep:
            return False
        return None

    def import_bmsynth(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, tr("preferences.import_title"), "", "INI (*.ini);;All files (*)",
        )
        if not path:
            return
        try:
            values, warnings = read_bmsynth_ini(path)
            if not values:
                NativeMessageBox.information(self, tr("preferences.title"), tr("preferences.import_none"))
                return
            neutralize_extensions = self._choose_bmsynth_extensions()
            if neutralize_extensions is None:
                return
            next_engine, engine_count = import_bmsynth_engine_values(
                self._values(), values.get("BMS", {}),
                neutralize_extensions=neutralize_extensions,
            )
            applied = self.owner.apply_bmsynth_values(values, neutralize_extensions=neutralize_extensions)
            soundfont = values.get("BMS", {}).get("SoundFont")
            if soundfont:
                font_path = Path(soundfont)
                if not font_path.is_absolute():
                    font_path = Path(path).parent / font_path
                if font_path.is_file():
                    if self.owner.activate_soundfont_path(str(font_path)):
                        applied += 1
                else:
                    warnings.append(tr("error.ini_soundfont_missing", value=soundfont))
            if not (engine_count or applied):
                NativeMessageBox.information(self, tr("preferences.title"), tr("preferences.import_none"))
                return
            self._loading = True
            try:
                self.set_values(next_engine)
            finally:
                self._loading = False
            self._preview_live()
            # Restart fields stay staged; SoundFont/effect import retains its
            # existing explicit runtime-action semantics.
            self.owner.append_log(log_text("preferences.import_success", count=applied + engine_count))
            for warning in warnings:
                self.owner.append_log(log_text("runtime.ini_warning", message=warning))
        except (OSError, ValueError) as exc:
            NativeMessageBox.warning(
                self, tr("preferences.title"), tr("preferences.import_failed", error=exc),
            )


class NativeInstrumentTable(QTableWidget):
    """Row-navigation table with one hover state for the complete row."""

    edit_requested = Signal(int, int)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._hover_row = -1
        self.clickable_column_start = None
        self._lazy_builder = None
        self._materialized_rows = set()
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        connect_native_pointer_model(self)
        self._rounded_clip_ready = True
        self.objectNameChanged.connect(lambda _name: self._sync_table_clip())
        self._watch_table_clip()

    def _watch_table_clip(self):
        for child in (self.viewport(), self.horizontalHeader(), self.verticalHeader()):
            child.installEventFilter(self)
        self._sync_table_clip()

    def setHorizontalHeader(self, header):
        super().setHorizontalHeader(header)
        if getattr(self, '_rounded_clip_ready', False):
            self._watch_table_clip()

    def _sync_table_clip(self):
        if not getattr(self, '_rounded_clip_ready', False):
            return
        children = (self.viewport(), self.horizontalHeader(), self.verticalHeader())
        rounded = self.objectName() == 'NativeInstrumentTable'
        geometry = (rounded, self.size(), tuple(child.geometry() for child in children),
                    tuple(id(child) for child in children))
        if geometry == getattr(self, '_rounded_clip_geometry', None):
            return
        self._rounded_clip_geometry = geometry
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 7, 7)
        interior = QRegion(path.toFillPolygon().toPolygon())
        for child in children:
            if rounded and child.width() and child.height():
                region = interior.translated(-child.x(), -child.y()).intersected(child.rect())
                child.setMask(region)
            else:
                child.clearMask()

    def event(self, event):
        result = super().event(event)
        if event.type() in (QEvent.Show, QEvent.Resize, QEvent.StyleChange, QEvent.Polish, QEvent.DynamicPropertyChange):
            self._sync_table_clip()
        return result

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Resize, QEvent.Move, QEvent.Show, QEvent.Hide):
            self._sync_table_clip()
        return super().eventFilter(watched, event)

    def set_lazy_rows(self, count, builder):
        """Keep the Qt row geometry, instantiate only painted/queried rows."""
        self._lazy_builder = None
        self._materialized_rows.clear()
        self.clearContents()
        self.setRowCount(count)
        self._lazy_builder = builder
        self._materialize_visible()

    def setRowCount(self, count):
        # An explicit external table reset leaves the lazy catalog behind.
        self._lazy_builder = None
        self._materialized_rows.clear()
        super().setRowCount(count)

    def _materialize_row(self, row):
        if self._lazy_builder is None or row in self._materialized_rows or not 0 <= row < self.rowCount():
            return
        self._materialized_rows.add(row)
        self._lazy_builder(row)

    def _materialize_visible(self):
        if self._lazy_builder is None or not self.rowCount():
            return
        first = max(0, self.rowAt(0))
        last = self.rowAt(max(0, self.viewport().height() - 1))
        if last < first:
            last = min(self.rowCount() - 1, first + 24)
        for row in range(first, min(last + 2, self.rowCount())):
            self._materialize_row(row)

    def refresh_row(self, row):
        """Rebuild just one already-painted row after its source changes."""
        if self._lazy_builder is not None and row in self._materialized_rows:
            self._lazy_builder(row)

    def item(self, row, column):
        self._materialize_row(row)
        return super().item(row, column)

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self._materialize_visible()
        refresh_native_pointer_feedback(self)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._materialize_visible()

    @property
    def hover_row(self):
        return self._hover_row

    def refresh_pointer_feedback(self):refresh_native_pointer_feedback(self)

    def pointer_data_changed(self,first,last,roles):refresh_changed_pointer_feedback(self,first,last,roles)

    def _set_hover_row(self, row):
        row = int(row)
        if row == self._hover_row:
            return
        self._hover_row = row
        self.viewport().update()

    def mouseMoveEvent(self, event):
        self.reconcile_hover(event.position().toPoint())
        super().mouseMoveEvent(event)

    def reconcile_hover(self, position):
        index = self.indexAt(position)
        usable=self.isEnabled() and index.isValid() and bool(index.flags() & Qt.ItemIsEnabled)
        self._set_hover_row(index.row() if usable else -1)
        clickable = (
            usable
            and self.clickable_column_start is not None
            and index.column() >= self.clickable_column_start
            and index.data(Qt.UserRole) is not None
        )
        set_native_cursor(self.viewport(),Qt.PointingHandCursor if clickable else Qt.ArrowCursor)

    def leaveEvent(self, event):
        self.reset_stale_hover()
        super().leaveEvent(event)

    def reset_stale_hover(self):
        if self._hover_row >= 0:
            self._set_hover_row(-1)
        set_native_cursor(self.viewport(),None)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F2 and self.currentColumn() >= 5:
            self.edit_requested.emit(self.currentRow(), self.currentColumn())
            event.accept()
            return
        super().keyPressEvent(event)


class NativeFontOrderTable(QTableWidget):
    """Keep drag/drop row-aligned; the group model, not Qt cells, owns order."""

    move_requested = Signal(int, int)
    remove_requested = Signal()

    def __init__(self, rows, columns, parent=None):
        super().__init__(rows, columns, parent)
        self._active_drop_boundary = None
        self._drop_line = QFrame(self.viewport())
        self._drop_line.setObjectName("FontDropLine")
        self._drop_line.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._drop_line.hide()
        self.viewport().installEventFilter(self)
        header = self.horizontalHeader()
        header.setAcceptDrops(True)
        header.viewport().setAcceptDrops(True)
        header.installEventFilter(self)
        header.viewport().installEventFilter(self)

    def _drop_boundary(self, point):
        """Return an insertion boundary, never an item-overwrite target."""
        if not self.viewport().rect().contains(point) or not self.rowCount():
            return None
        row = self.rowAt(point.y())
        if row < 0:
            return 0 if point.y() < self.rowViewportPosition(0) else self.rowCount()
        return row + int(point.y() >= self.rowViewportPosition(row) + self.rowHeight(row) / 2)

    def _show_drop_boundary(self, boundary):
        self._active_drop_boundary = boundary
        y = (self.rowViewportPosition(boundary) if boundary < self.rowCount()
             else self.rowViewportPosition(boundary - 1) + self.rowHeight(boundary - 1))
        y = max(0, min(self.viewport().height() - 2, y - 1))
        self._drop_line.setGeometry(0, y, self.viewport().width(), 2)
        self._drop_line.raise_()
        self._drop_line.show()

    def _hide_drop_boundary(self):
        self._active_drop_boundary = None
        self._drop_line.hide()

    def cancel_pointer_gesture(self):self._hide_drop_boundary()

    def _internal_drag(self, event):
        return event.source() is self and 0 <= self.currentRow() < self.rowCount()

    def eventFilter(self, watched, event):
        header = self.horizontalHeader()
        if watched in (header, header.viewport()):
            if event.type() in (QEvent.DragEnter, QEvent.DragMove):
                if self._internal_drag(event):
                    self._show_drop_boundary(0)
                    event.setDropAction(Qt.MoveAction)
                    event.accept()
                else:
                    event.ignore()
                return True
            if event.type() == QEvent.Drop:
                if self._internal_drag(event):
                    self._complete_drop(event, 0)
                else:
                    event.ignore()
                return True
            if event.type() == QEvent.DragLeave:
                self._hide_drop_boundary()
                return True
        if watched is self.viewport() and event.type() == QEvent.Resize and self._active_drop_boundary is not None:
            self._show_drop_boundary(self._active_drop_boundary)
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event):
        if self._internal_drag(event):
            event.setDropAction(Qt.MoveAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if not self._internal_drag(event):
            self._hide_drop_boundary()
            event.ignore()
            return
        super().dragMoveEvent(event)
        boundary = self._drop_boundary(event.position().toPoint())
        if boundary is None:
            self._hide_drop_boundary()
            event.ignore()
            return
        self._show_drop_boundary(boundary)
        event.setDropAction(Qt.MoveAction)
        event.accept()

    def dragLeaveEvent(self, event):
        self._hide_drop_boundary()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        if not self._internal_drag(event):
            self._hide_drop_boundary()
            event.ignore()
            return
        boundary = self._drop_boundary(event.position().toPoint())
        if boundary is None:
            self._hide_drop_boundary()
            event.ignore()
            return
        self._complete_drop(event, boundary)

    def _complete_drop(self, event, boundary):
        source = self.currentRow()
        target = boundary - (source < boundary)
        self._hide_drop_boundary()
        if source != target:
            self.move_requested.emit(source, target)
        event.setDropAction(Qt.MoveAction)
        event.accept()
        # QTableWidget marks an already accepted self-move as handled. Without
        # this bookkeeping QAbstractItemView::startDrag clears the source cells
        # after our model-driven refresh, creating the apparent merged row.
        super().dropEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.remove_requested.emit()
            event.accept()
            return
        if event.modifiers() & Qt.ControlModifier and event.key() in (Qt.Key_Up, Qt.Key_Down):
            source = self.currentRow()
            if source >= 0:
                self.move_requested.emit(source, source + (-1 if event.key() == Qt.Key_Up else 1))
            event.accept()
            return
        super().keyPressEvent(event)


def _paint_instrument_cell_rules(painter, rect, palette):
    """One geometry for data and transparent activity-cell boundaries."""
    painter.setRenderHint(QPainter.Antialiasing, False)
    painter.setPen(QPen(QColor(palette.border_subtle), 1))
    painter.drawLine(rect.bottomLeft(), rect.bottomRight())
    painter.drawLine(rect.topRight(), rect.bottomRight())


class NativeInstrumentHeader(QHeaderView):
    """Header text painted with the exact same left/right inset as cells.

    QHeaderView's platform painter reserves its own label margins and reacts
    to selected rows.  That made the apparent inset differ from body cells
    and could make every heading look bold after row selection.  This narrow
    header only replaces that presentation layer; native resize mechanics,
    section sizes, and horizontal scrolling remain QHeaderView behavior.
    """

    def __init__(self, dark_source, parent=None, *, spec=INSTRUMENT_TABLE):
        super().__init__(Qt.Horizontal, parent)
        self._dark_source = dark_source
        self._spec = spec
        self._hover_section = -1
        self.setMouseTracking(True)
        self.setHighlightSections(False)
        self.setSectionsClickable(False)
        self.geometriesChanged.connect(self.refresh_pointer_feedback)

    def refresh_pointer_feedback(self):refresh_native_pointer_feedback(self)

    def setSectionsClickable(self,enabled):
        super().setSectionsClickable(enabled)
        if hasattr(self,'_hover_section'):self.refresh_pointer_feedback()

    def setSectionsMovable(self,enabled):
        super().setSectionsMovable(enabled)
        if hasattr(self,'_hover_section'):self.refresh_pointer_feedback()

    def _set_hover_section(self, section):
        section = int(section)
        if section == self._hover_section:
            return
        old = self._hover_section
        self._hover_section = section
        if old >= 0:
            self.updateSection(old)
        if section >= 0:
            self.updateSection(section)

    def mouseMoveEvent(self, event):
        self.reconcile_hover(event.position().toPoint())
        super().mouseMoveEvent(event)

    def reconcile_hover(self, position):
        face_action=self.isEnabled() and (self.sectionsClickable() or self.sectionsMovable())
        self._set_hover_section(self.logicalIndexAt(position) if face_action else -1)

    def leaveEvent(self, event):
        self.reset_stale_hover()
        super().leaveEvent(event)

    def reset_stale_hover(self):
        if self._hover_section >= 0:
            self._set_hover_section(-1)

    def paintSection(self, painter, rect, logical_index):
        if not rect.isValid():
            return
        palette = widget_palette(self, self._dark_source())
        painter.save()
        painter.fillRect(
            rect,
            QColor(palette.interaction_hover if self.isEnabled() and (self.sectionsClickable() or self.sectionsMovable()) and logical_index == self._hover_section else palette.surface_alt),
        )
        painter.setPen(QPen(QColor(palette.border), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.setPen(QPen(QColor(palette.border_subtle), 1))
        painter.drawLine(rect.topRight(), rect.bottomRight())
        text = self.model().headerData(logical_index, Qt.Horizontal, Qt.DisplayRole) or ""
        font = self._spec.font()
        painter.setFont(font)
        painter.setPen(QColor(palette.label))
        # The dedicated body delegate below uses this exact section geometry
        # and inset.  Avoid the platform header/item painters entirely: they
        # introduce independent implicit margins at different DPI settings.
        text_rect = self._spec.text_rect(rect)
        painter.drawText(
            text_rect,
            Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine,
            QFontMetrics(font).elidedText(str(text), Qt.ElideRight, max(0, text_rect.width())),
        )
        painter.restore()


class NativeInstrumentRowDelegate(QStyledItemDelegate):
    """Body painter paired exactly with :class:`NativeInstrumentHeader`."""

    ROLE_EFFECTIVE_SOURCE = Qt.UserRole + 21
    ROLE_SETTINGS_DISABLED = Qt.UserRole + 22
    ROLE_NUMERIC_DOMAIN = Qt.UserRole + 23

    def __init__(self, dark_source, table, parent=None, *, spec=INSTRUMENT_TABLE):
        super().__init__(parent)
        self._dark_source = dark_source
        self._table = table
        self._spec = spec

    def createEditor(self, parent, option, index):
        domain = index.data(self.ROLE_NUMERIC_DOMAIN)
        if domain is not None:
            low, high = domain
            return NativeInlineNumberEdit(low, high, parent)
        return super().createEditor(parent, option, index)

    @staticmethod
    def effective_background(palette):
        soft, accent = QColor(palette.accent_soft), QColor(palette.accent)
        return QColor(*(
            round(getattr(soft, channel)() * .72 + getattr(accent, channel)() * .28)
            for channel in ("red", "green", "blue")
        ))

    def paint(self, painter, option, index):
        palette = widget_palette(self, self._dark_source())
        selected = bool(option.state & QStyle.State_Selected)
        hovered = self._table.isEnabled() and index.row() == self._table.hover_row
        background = self._spec.row_background(palette, index.row(), selected, hovered)
        if index.data(self.ROLE_EFFECTIVE_SOURCE):
            background = self.effective_background(palette)
        painter.save()
        painter.fillRect(option.rect, QColor(background))
        _paint_instrument_cell_rules(painter, option.rect, palette)
        font = self._spec.font()
        painter.setFont(font)
        painter.setPen(QColor(palette.muted if index.data(self.ROLE_SETTINGS_DISABLED) else palette.text))
        text_rect = self._spec.text_rect(option.rect, index.column())
        alignment = index.data(Qt.TextAlignmentRole) or self._spec.alignment(index.column())
        text = QFontMetrics(font).elidedText(
            str(index.data(Qt.DisplayRole) or ""), Qt.ElideRight, max(0, text_rect.width())
        )
        painter.drawText(text_rect, int(alignment) | int(Qt.TextSingleLine), text)
        painter.restore()


class NativeSettingsRowDelegate(QStyledItemDelegate):
    """Instrument-table visual tokens, preserving checkable model semantics.

    A settings table is not a numeric preset editor: no instrument-specific
    createEditor mapping. Checkboxes use the same OutlineCheckBox painter and
    the whole cell is the pointer target; Space remains keyboard accessible.
    """

    def __init__(self, dark_source, table, parent=None):
        super().__init__(parent)
        self._dark_source, self._table = dark_source, table

    def paint(self, painter, option, index):
        p = widget_palette(self, self._dark_source())
        enabled=self._table.isEnabled() and bool(index.flags() & Qt.ItemIsEnabled)
        hovered = self._table.isEnabled() and index.row() == self._table.hover_row
        painter.save()
        painter.fillRect(option.rect, QColor(INSTRUMENT_TABLE.row_background(p, index.row(),
            bool(option.state & QStyle.State_Selected), hovered)))
        _paint_instrument_cell_rules(painter, option.rect, p)
        state = index.data(Qt.CheckStateRole)
        if state is not None:
            size = min(self._table.style().pixelMetric(QStyle.PM_IndicatorWidth),
                       option.rect.width(), option.rect.height())
            rect = QRect(0, 0, size, size); rect.moveCenter(option.rect.center())
            paint_outline_check_indicator(painter, rect, p, checked=state==Qt.Checked.value,
                enabled=enabled, hovered=hovered)
        else:
            font = INSTRUMENT_TABLE.font(); painter.setFont(font)
            brush = index.data(Qt.ForegroundRole)
            painter.setPen(QColor(disabled_control_color(p)) if not enabled else brush.color() if brush is not None else QColor(p.text))
            rect = INSTRUMENT_TABLE.text_rect(option.rect)
            text = QFontMetrics(font).elidedText(str(index.data(Qt.DisplayRole) or ''), Qt.ElideRight, max(0, rect.width()))
            painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine, text)
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if index.data(Qt.CheckStateRole) is None:
            return super().editorEvent(event, model, option, index)
        if not (self._table.isEnabled() and index.flags() & Qt.ItemIsEnabled and index.flags() & Qt.ItemIsUserCheckable):
            return False
        pointer = event.type()==QEvent.MouseButtonRelease and event.button()==Qt.LeftButton and option.rect.contains(event.position().toPoint())
        keyboard = event.type()==QEvent.KeyPress and event.key() in (Qt.Key_Space, Qt.Key_Select)
        if pointer or keyboard:
            state = Qt.Unchecked if index.data(Qt.CheckStateRole)==Qt.Checked.value else Qt.Checked
            # Qt item models store this role as an integer. Passing a Python
            # enum leaves a different QVariant type and breaks later toggles.
            return model.setData(index, state.value, Qt.CheckStateRole)
        return False


class NativeActivityCellDelegate(QStyledItemDelegate):
    """Project Note activity rendered with the live window's table tokens.

    This is intentionally distinct from BMSynth's source-only S/mapping
    marker.  It displays the already-established ABMSynth MIDI activity
    state and is refreshed by the dialog's 10ms display timer.
    """

    ROLE_INTENSITY = Qt.UserRole + 20
    HELD_LEVEL = SoundFontActivity.HELD_STRENGTH / SoundFontActivity.ONSET_STRENGTH

    def __init__(self, dark_source, parent=None):
        super().__init__(parent)
        self._dark_source = dark_source

    def paint(self, painter, option, index):
        dark = self._dark_source()
        palette = widget_palette(self, dark)
        painter.save()
        painter.fillRect(option.rect, QColor(INSTRUMENT_TABLE.row_background(
            palette, index.row(), bool(option.state & QStyle.State_Selected),
            self.parent().isEnabled() and index.row() == self.parent().hover_row,
        )))
        intensity = max(0.0, min(1.0, float(index.data(self.ROLE_INTENSITY) or 0.0)))
        if intensity > 0.0:
            color = QColor(palette.accent_hover if dark else palette.accent)
            cell = QRectF(option.rect)
            emphasis = max(0.0, (intensity - self.HELD_LEVEL) / (1.0 - self.HELD_LEVEL))
            diameter = max(0.0, min(
                INSTRUMENT_TABLE.activity_diameter + 2.0 * emphasis,
                cell.width() - 2, cell.height() - 2,
            ))
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            if emphasis > 0.0:
                halo_diameter = max(0.0, min(diameter + 4.0 * emphasis, cell.width() - 2, cell.height() - 2))
                painter.setOpacity(0.25 * emphasis)
                painter.drawEllipse(QRectF(
                    cell.center().x() - halo_diameter / 2,
                    cell.center().y() - halo_diameter / 2,
                    halo_diameter, halo_diameter,
                ))
            painter.setOpacity(intensity)
            painter.drawEllipse(QRectF(
                cell.center().x() - diameter / 2,
                cell.center().y() - diameter / 2,
                diameter, diameter,
            ))
        painter.setOpacity(1.0)
        _paint_instrument_cell_rules(painter, option.rect, palette)
        painter.restore()


def configure_instrument_table(table,dark_source,*,spec=INSTRUMENT_TABLE,
                               selection=QAbstractItemView.SingleSelection):
    """One presentation/input policy;callers retain task-specific columns/actions."""
    table.setObjectName('NativeInstrumentTable')
    table.setFrameShape(QFrame.NoFrame);table.setMinimumSize(0,0)
    table.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Expanding)
    table.setFont(spec.font())
    table.setHorizontalHeader(NativeInstrumentHeader(dark_source,table,spec=spec))
    table.setHorizontalHeaderLabels(spec.headers)
    table.setItemDelegate(NativeInstrumentRowDelegate(dark_source,table,table,spec=spec))
    if spec.activity_column>=0:
        table.setItemDelegateForColumn(spec.activity_column,NativeActivityCellDelegate(dark_source,table))
    table.verticalHeader().hide();table.verticalHeader().setDefaultSectionSize(spec.row_height)
    table.setSelectionBehavior(QAbstractItemView.SelectRows);table.setSelectionMode(selection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setAlternatingRowColors(True);table.setShowGrid(False);table.setWordWrap(False)
    table.setTextElideMode(Qt.ElideRight);table.setSortingEnabled(False)
    table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
    table.setVerticalScrollMode(QAbstractItemView.ScrollPerItem)
    header=table.horizontalHeader();header.setFont(spec.font())
    header.setMinimumSectionSize(0);header.setDefaultAlignment(Qt.AlignLeft|Qt.AlignVCenter)
    header.setSectionsMovable(False);header.setSectionResizeMode(QHeaderView.Interactive)
    if spec.activity_column>=0:
        header.setSectionResizeMode(spec.activity_column,QHeaderView.Fixed)
        table.setColumnWidth(spec.activity_column,spec.activity_width)


class NativeSoundFontMappingDialog(NativeDialog):
    """An explicit LSB destination,with real source metadata and safe Cancel."""
    def __init__(self,parent,catalog,mapping=None):
        super().__init__(parent)
        self.group=parent.group;self.original=mapping;self.mapping=None
        self.setWindowTitle(tr('mapping.title'));self.setMinimumSize(0,0)
        root=set_layout(QVBoxLayout(self),METRICS['outer'],METRICS['section_gap'])
        root.setSizeConstraint(QLayout.SetNoConstraint)
        card=NativeCard();body=set_layout(QGridLayout(card),0,8);root.addWidget(card,0,Qt.AlignTop)
        self.bank=NativeNumberBox(0,128,tr('creator.bank'))
        self.lsb=NativeNumberBox(1,127,tr('channel_editor.bank_lsb'))
        self.program=NativeNumberBox(0,127,tr('creator.program'))
        self.source=AnimatedComboBox();self.source.setMinimumWidth(0)
        self.source.setAccessibleName(tr('mapping.source'))
        self.catalog=tuple(row for row in catalog if 0<=row['bank']<=128 and 0<=row['preset']<=127)
        initial=(mapping.bank,mapping.bank_lsb,mapping.program) if mapping else next(
            ((0,lsb,program) for lsb in range(1,128) for program in range(128)
             if (0,lsb,program) not in self.group.mapping_map()),(0,1,0))
        for control,value in zip((self.bank,self.lsb,self.program),initial):control.setValue(value)
        for row,(caption,control) in enumerate(((tr('creator.bank'),self.bank),
            (tr('channel_editor.bank_lsb'),self.lsb),(tr('creator.program'),self.program),
            (tr('mapping.source'),self.source))):
            label=ElasticLabel(caption);label.setBuddy(control);body.addWidget(label,row,0)
            control.setMinimumWidth(0);control.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
            body.addWidget(control,row,1)
        body.setColumnStretch(1,1)
        self.status=NativeOptionalLabel();self.status.setWordWrap(True);root.addWidget(self.status)
        root.addStretch(1)
        actions=control_row_layout();actions.addStretch(1)
        self.confirm=make_action_button(tr('common.apply'),self.accept);set_button_primary(self.confirm,True,default=True)
        actions.addWidget(self.confirm);actions.addWidget(make_action_button(tr('common.cancel'),self.reject));root.addLayout(actions)
        self.bank.valueChanged.connect(self._populate_sources)
        self.lsb.valueChanged.connect(self._validate)
        self.program.valueChanged.connect(self._validate)
        self.source.currentIndexChanged.connect(self._validate)
        self._populate_sources()
        if mapping is not None:
            self.source.setCurrentIndex(self._source_index((mapping.source_index,mapping.source_bank,mapping.source_program)))
        self._validate();apply_native_text_metrics(self);self.resize(580,root.totalHeightForWidth(580))

    def _populate_sources(self,*_):
        selected=self.source.currentData();self.source.blockSignals(True);self.source.clear()
        for row in self.catalog:
            if (row['bank']==128)!=(self.bank.value()==128):continue
            self.source.addItem(tr('mapping.source_item',name=row['name'],font=self.group.fonts[row['source_index']].label,
                bank=row['bank'],program=row['preset']),(row['source_index'],row['bank'],row['preset']))
        at=self._source_index(selected);self.source.setCurrentIndex(at if at>=0 else 0)
        self.source.blockSignals(False);self._validate()

    def _source_index(self,identity):
        # Qt's QVariant matcher does not reliably compare a Python tuple
        # stored as user data. Compare the actual immutable identities.
        return next((index for index in range(self.source.count())
            if self.source.itemData(index)==identity),-1)

    def _validate(self,*_):
        data=self.source.currentData();address=(self.bank.value(),self.lsb.value(),self.program.value())
        duplicate=any(item.address==address and item is not self.original for item in self.group.mappings)
        self.confirm.setEnabled(data is not None and not duplicate)
        self.status.setText(tr('mapping.duplicate') if duplicate else tr('mapping.no_source') if data is None else
            tr('mapping.drum_mode_hint') if self.bank.value()==128 else
            tr('mapping.xg_bank_hint') if self.bank.value()==127 else '')

    def accept(self):
        self._validate()
        if not self.confirm.isEnabled():return
        self.mapping=SoundFontMapping(self.bank.value(),self.lsb.value(),self.program.value(),*self.source.currentData())
        super().accept()


class NativeLiveSoundFontGroupDialog(NativeDialog):
    """Live SoundFont-group editor; owns widgets and their actions."""

    FONT_LIST_VISIBLE_ROWS = 4

    def minimumSizeHint(self):
        # Native Windows WM_GETMINMAXINFO otherwise adopts the hidden table
        # pages' aggregate hint as a floor despite minimumWidth=0.
        return QSize(0, 0)

    def current_font_row(self):
        return self.font_table.currentRow()

    def _update_font_list_height(self):
        table = self.font_table
        height = (
            table.horizontalHeader().sizeHint().height()
            + self.FONT_LIST_VISIBLE_ROWS * table.verticalHeader().defaultSectionSize()
            + (table.horizontalScrollBar().sizeHint().height()
               if table.horizontalScrollBar().maximum() > 0 else 0)
            + 2 * table.frameWidth()
        )
        table.setMaximumHeight(height)
        self.font_card.updateGeometry()

    def refresh_fonts(self, *, update_routing=True):
        self._sync_save_actions()
        selected = self.current_font_row()
        self.font_table.blockSignals(True)
        self.font_table.setRowCount(len(self.group.fonts))
        for row, item in enumerate(self.group.fonts):
            volume = QTableWidgetItem(f"{item.volume}%")
            volume.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.font_table.setItem(row, 0, volume)
            path = item.label if item.custom is not None else os.path.normpath(str(self.group.font_path(item)))
            font_cell = QTableWidgetItem(path)
            if item.custom is None and not self.group.font_path(item).is_file():
                font_cell.setText(tr('soundfont_picker.missing_name',name=path))
                font_cell.setToolTip(tr('soundfont_picker.missing_tooltip',path=path))
            else:
                font_cell.setToolTip(path)
            font_cell.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.font_table.setItem(row, 1, font_cell)
        self.font_table.blockSignals(False)
        self._update_font_list_height()
        self.count.setText(tr("group.instrument_count", count=len(self._instrument_rows)))
        if self.group.fonts:
            self.font_table.selectRow(max(0, min(selected, len(self.group.fonts) - 1)))
        self.select_font()
        self.preset_font.blockSignals(True)
        self.preset_font.clear()
        self.preset_font.addItem(tr("common.default"), None)
        for index, item in enumerate(self.group.fonts):
            self.preset_font.addItem(f"{index + 1}. {item.label}", index)
        self.preset_font.blockSignals(False)
        if update_routing and hasattr(self, "routing_table"):
            self._populate_routing_rows()

    def _configure_routing_columns(self):
        signature = tuple(str(self.group.font_path(font)) for font in self.group.fonts)
        if signature == self._routing_font_signature:
            return False
        headers = ("", tr("group.bank_header"), tr("group.program_header")) + tuple(
            font.label for font in self.group.fonts
        )
        spec = InstrumentTableSpec(headers=headers, trailing_column=len(headers) - 1)
        self._routing_table_spec = spec
        table = self.routing_table
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        for column in range(len(headers)):
            table.horizontalHeaderItem(column).setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        header = table.horizontalHeader()
        header._spec = spec
        table.itemDelegate()._spec = spec
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        table.setColumnWidth(0, spec.activity_width)
        for column in spec.named_columns:
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            if column >= 3:
                font = self.group.fonts[column - 3]
                table.horizontalHeaderItem(column).setToolTip(font.source_description if font.custom is not None else os.path.normpath(str(self.group.font_path(font))))
        self._routing_font_signature = signature
        return True

    def _fit_routing_columns(self):
        spec = self._routing_table_spec
        for column in spec.named_columns:
            preferred = self._routing_content_widths[column]
            preferred = max(70, preferred) if column < 3 else max(140, min(280, preferred))
            self.routing_table.setColumnWidth(column, preferred)

    def _populate_routing_rows(self):
        """One destination row with all candidate fonts and one effective cell."""
        schema_changed = self._configure_routing_columns()
        table = self.routing_table
        selected_item = table.item(table.currentRow(), 0) if table.currentRow() >= 0 else None
        selected_record = selected_item.data(Qt.UserRole) if selected_item else None
        selected_key = ((selected_record["bank"], selected_record["preset"])
                        if selected_record else None)
        was_blocked = table.blockSignals(True)
        self._routing_rows = self._instrument_rows
        spec = self._routing_table_spec
        metrics = QFontMetrics(spec.font())
        self._routing_content_widths = [metrics.horizontalAdvance(str(text)) + 2 * spec.horizontal_padding
                                        for text in spec.headers]
        for record in self._routing_rows:
            self._routing_content_widths[1] = max(self._routing_content_widths[1], spec.fitted_width(f"{record['bank']:03d}"))
            self._routing_content_widths[2] = max(self._routing_content_widths[2], spec.fitted_width(f"{record['preset']:03d}"))
            for source in record["source_candidates"]:
                column = int(source["source_index"]) + 3
                if column < len(self._routing_content_widths):
                    self._routing_content_widths[column] = max(
                        self._routing_content_widths[column],
                        metrics.horizontalAdvance(source["name"]) + 2 * spec.horizontal_padding,
                    )
        table.set_lazy_rows(len(self._routing_rows), self._build_routing_row)
        if selected_key is not None:
            selected = next((number for number, record in enumerate(self._routing_rows)
                             if (record["bank"], record["preset"]) == selected_key), None)
            if selected is not None:
                table.selectRow(selected)
        table.blockSignals(was_blocked)
        if schema_changed:
            self._fit_routing_columns()

    def _build_routing_row(self, number):
        table = self.routing_table
        record = self._routing_rows[number]
        was_blocked = table.blockSignals(True)
        try:
            candidates = {int(source["source_index"]): source for source in record["source_candidates"]}
            effective_index = int(record["source_index"])
            for column in range(table.columnCount()):
                if column == 0:
                    value = ""
                elif column == 1:
                    value = f"{record['bank']:03d}"
                elif column == 2:
                    value = f"{record['preset']:03d}"
                else:
                    source_index = column - 3
                    candidate = candidates.get(source_index)
                    fallback = (candidate is None and source_index == effective_index
                                and record.get("source_preset_fallback"))
                    value = (candidate["name"] if candidate is not None else
                             tr("group.routing_fallback_name", name=record["name"]) if fallback else "")
                cell = QTableWidgetItem(value)
                cell.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                if column == 0:
                    cell.setData(Qt.UserRole, record)
                elif column >= 3 and value:
                    cell.setData(Qt.UserRole, column - 3)
                    effective = column - 3 == effective_index
                    cell.setData(NativeInstrumentRowDelegate.ROLE_EFFECTIVE_SOURCE, effective)
                    if fallback:
                        cell.setToolTip(tr("group.routing_fallback_tooltip", name=record["name"]))
                    else:
                        font=self.group.fonts[column-3]
                        path = font.source_description if font.custom is not None else os.path.normpath(str(self.group.font_path(font)))
                        cell.setToolTip(tr(
                            "group.routing_active_tooltip" if effective else "group.routing_candidate_tooltip",
                            name=value, path=path,
                        ))
                table.setItem(number, column, cell)
        finally:
            table.blockSignals(was_blocked)

    def select_routing_source(self, row, column):
        if column < 3 or row < 0 or row >= self.routing_table.rowCount():
            return
        cell = self.routing_table.item(row, column)
        record_item = self.routing_table.item(row, 0)
        source = cell.data(Qt.UserRole) if cell else None
        record = record_item.data(Qt.UserRole) if record_item else None
        if source is None or record is None:
            return  # Empty cells cannot be assigned to a font without a preset.
        key = record["bank"], record["preset"]
        override = self.group.preset_map().get(key)
        if source == record["source_index"] and (override is None or not override.disabled):
            return
        if override is None:
            self.group.presets.append(PresetOverride(*key, index=source))
        else:
            override.index = source
            override.disabled = False
        if self._apply_live("font_mapping"):
            self._refresh_instrument_assignment(key)

    def open_routing_in_settings(self, row, _column):
        record = self.routing_table.item(row, 0).data(Qt.UserRole)
        key = record["bank"], record["preset"]
        self.filter_all.setChecked(True)
        self.group_tabs.setCurrentIndex(1)
        index = self._visible_instrument_rows.get(key)
        if index is not None:
            self.instrument_table.selectRow(index)
            self.instrument_table.scrollToItem(self.instrument_table.item(index, 0))

    def change_font_volume(self, value):
        index = self.current_font_row()
        if 0 <= index < len(self.group.fonts):
            self.group.fonts[index].volume = int(value)
            item = self.font_table.item(index, 0)
            if item:
                item.setText(f"{int(value)}%")
            self._apply_live(("font_volume", index))

    def _apply_live(self, change):
        submit=getattr(self.owner,'queue_group_controls',None)
        if submit is not None:
            bridge=getattr(self,'_group_control_presentation',None)
            if bridge is None:
                from native_widgets import NativeSnapshotCommandPresentation
                bridge=self._group_control_presentation=NativeSnapshotCommandPresentation(self,
                    lambda:(id(self.group),self.owner.audio_session.stream),
                    self._complete_group_controls,self._sync_save_actions)
            receipt=bridge.submit('preset-controls',(),lambda done:submit(self.group,change,done))
            if receipt is not None:
                if receipt.get('accepted'):
                    self._sync_save_actions();return True
                return False
        if not self._settle_group_controls():return False
        if self.owner.apply_group_runtime(self.group, change, persist=False):
            self._sync_save_actions()
            return True
        self.group = self.owner.soundfont_runtime.copy_group(self.owner.soundfont_runtime.group)
        self.refresh_fonts(update_routing=False)
        self.refresh_instruments()
        return False

    def _complete_group_controls(self,_group,receipt,values):
        if receipt.get('superseded'):return
        if not receipt.get('successful'):
            error=receipt.get('error') or tr('runtime.channel_executor_retained')
            self.owner.append_log(log_text('runtime.preset_controls_failed',error=error))
            if values is not None and values.get('group') is not None:
                self.group=values['group'];self.refresh_fonts(update_routing=False);self.refresh_instruments()

    def _settle_group_controls(self):
        """Explicit save/mapping/close barriers retain the last accepted edit."""
        if not self.owner.soundfont_runtime.finish_runtime_controls():
            self.owner.append_log(log_text('runtime.preset_controls_pending'));return False
        bridge=getattr(self,'_group_control_presentation',None)
        if bridge is not None:bridge.flush()
        return True

    def _refresh_lsb_mappings(self):
        if not hasattr(self,'lsb_mapping_table'):return
        table=self.lsb_mapping_table;selected=table.currentRow()
        table.setRowCount(len(self.group.mappings))
        sources={(row['source_index'],row['bank'],row['preset']):row for rows in
            self._instrument_catalog_by_key.values() for row in rows}
        for number,item in enumerate(self.group.mappings):
            source=sources.get((item.source_index,item.source_bank,item.source_program))
            font=self.group.fonts[item.source_index].label if item.source_index<len(self.group.fonts) else ''
            name=tr('mapping.missing_source') if source is None else source['name']
            for column,value in enumerate((f'{item.bank:03d}',f'{item.bank_lsb:03d}',f'{item.program:03d}',name,font)):
                cell=QTableWidgetItem(value);cell.setFlags(Qt.ItemIsEnabled|Qt.ItemIsSelectable)
                table.setItem(number,column,cell)
        if table.rowCount():table.selectRow(min(max(0,selected),table.rowCount()-1))
        self._lsb_mapping_actions()

    def _lsb_mapping_actions(self):
        valid=0<=self.lsb_mapping_table.currentRow()<len(self.group.mappings)
        self.lsb_mapping_edit.setEnabled(valid);self.lsb_mapping_remove.setEnabled(valid)
        self.lsb_mapping_add.setEnabled(bool(self._instrument_catalog_by_key))

    def _edit_lsb_mapping(self,editing=False):
        at=self.lsb_mapping_table.currentRow()
        if editing and not 0<=at<len(self.group.mappings):return
        catalog=[row for rows in self._instrument_catalog_by_key.values() for row in rows]
        dialog=NativeSoundFontMappingDialog(self,catalog,self.group.mappings[at] if editing else None)
        try:
            if dialog.exec()!=QDialog.Accepted:return
            if editing:self.group.mappings[at]=dialog.mapping
            else:self.group.mappings.append(dialog.mapping)
            if self._apply_live('font_mapping'):self.refresh_instruments()
        finally:dialog.deleteLater()

    def _remove_lsb_mapping(self):
        at=self.lsb_mapping_table.currentRow()
        if not 0<=at<len(self.group.mappings):return
        self.group.mappings.pop(at)
        if self._apply_live('font_mapping'):self.refresh_instruments()

    def add_fonts(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr("group.add_soundfonts_title"), "", "SoundFont (*.sf2 *.sf3 *.sfz)")
        if paths:
            self.group.fonts.extend(SoundFontEntry(path) for path in paths)
            self.refresh_fonts(update_routing=False)
            if self._apply_live("font_layout"):
                self.refresh_instruments()

    def remove_font(self):
        index = self.current_font_row()
        if 0 <= index < len(self.group.fonts) and (len(self.group.fonts)>1 or self.group.fonts[index].custom is not None):
            self.group.remap_font_indices({old:old-(old>index) for old in range(len(self.group.fonts)) if old!=index})
            self.group.fonts.pop(index)
            # Preserve the meaning of existing Index mappings after deletion.
            for override in self.group.presets:
                if override.index == index:
                    override.index = None
                elif override.index is not None and override.index > index:
                    override.index -= 1
            self.refresh_fonts(update_routing=False)
            if self._apply_live("font_layout"):
                self.refresh_instruments()

    def relocate_font(self, index, target):
        count = len(self.group.fonts)
        if not (0 <= index < count and 0 <= target < count) or index == target:
            return
        moved = self.group.fonts.pop(index)
        self.group.fonts.insert(target, moved)
        order=list(range(count));order.insert(target,order.pop(index))
        self.group.remap_font_indices({old:new for new,old in enumerate(order)})
        for override in self.group.presets:
            old = override.index
            if old == index:
                override.index = target
            elif old is not None and index < old <= target:
                override.index = old - 1
            elif old is not None and target <= old < index:
                override.index = old + 1
        self.refresh_fonts(update_routing=False)
        self.font_table.selectRow(target)
        if self._apply_live("font_layout"):
            self.refresh_instruments()

    def move_font(self, delta):
        self.relocate_font(self.current_font_row(), self.current_font_row() + delta)

    def new_group(self):
        creator=getattr(self,'instrument_creator',None)
        if creator is not None and not creator.can_close(self.new_group):return
        if not self._confirm_replace_staged_group():
            return
        # A new document begins with the active stack's first-priority font.
        # Its source path is deliberately blank until the user saves an SFG.
        active = self.owner.soundfont_runtime.group
        first = active.fonts[0] if active.fonts else None
        self.group = SoundFontGroup(fonts=[SoundFontEntry(first.filename,first.volume,first.custom,first.runtime_path)],
            source_path=active.source_path) if first else SoundFontGroup()
        if first and first.custom is None:self.group=SoundFontGroup.single_font(str(active.font_path(first)))
        if not self.owner.apply_group_runtime(self.group, "font_layout", persist=False):
            self.group = self.owner.soundfont_runtime.copy_group(active)
            self.refresh_fonts(update_routing=False)
            self.refresh_instruments()
            return
        self._selected_instrument_key = None
        self.refresh_fonts(update_routing=False)
        self.refresh_instruments()

    def load_group(self):
        creator=getattr(self,'instrument_creator',None)
        if creator is not None and not creator.can_close(self.load_group):return
        path, _ = QFileDialog.getOpenFileName(self, tr("group.open_title"), "", "SoundFont Group (*.sfg)")
        if path:
            try:
                loaded = load_sfg(path)
                errors = validate_group(loaded, allow_empty=True)
                if errors:
                    NativeMessageBox.warning(self, tr("group.title"), "\n".join(errors))
                    return
                # Parse/validate first. A missing or malformed target must not
                # discard edits just because the user chose another filename.
                if not self._confirm_replace_staged_group(restore_on_discard=False):
                    return
                if not self.owner.load_group(loaded):
                    return
                self.group = loaded
                self._saved_group = self.owner.soundfont_runtime.copy_group(loaded)
                self._original_group = self.owner.soundfont_runtime.copy_group(loaded)
                self._selected_instrument_key = None
                self.refresh_fonts(update_routing=False)
                self.refresh_instruments()
            except (OSError, UnicodeError, ValueError) as exc:
                NativeMessageBox.critical(self, tr("group.title"), str(exc))

    def save_close(self):
        self._finish_instrument_edit(True)
        creator = getattr(self, 'instrument_creator', None)
        if creator is not None and not creator.can_close(self.save_close):
            return
        self._save_group(close=True)

    def _sync_save_actions(self):
        if not hasattr(self,'save_group_button'):return
        creator=getattr(self,'instrument_creator',None)
        changed=(self.group!=self._saved_group or self.owner._unsaved_group_active
                 or bool(creator and creator.dirty))
        if getattr(self,'_save_actions_state',None)==changed:return
        self._save_actions_state=changed
        self.save_group_button.setVisible(changed)
        set_button_primary(self.save_group_button,changed,default=changed)
        set_button_primary(self.close_group_button,not changed,default=not changed)

    def _confirm_replace_staged_group(self, *, restore_on_discard=True):
        if self.group == self._saved_group and not self.owner._unsaved_group_active:
            return True
        choice = self._ask_replace_unsaved()
        if choice == "save":
            return self._save_group(close=False)
        if choice == "discard":
            if restore_on_discard and self.owner.soundfont_runtime.group != self._original_group:
                if not self.owner.load_group(self._original_group, persist=False):
                    return False
            return True
        return False

    def _ask_replace_unsaved(self):
        prompt = NativeMessageBox(self)
        prompt.setWindowTitle(tr("group.title"))
        prompt.setText(tr("group.replace_unsaved"))
        prompt.setIcon(QMessageBox.Question)
        save = prompt.addButton(tr("common.save"), QMessageBox.AcceptRole)
        discard = prompt.addButton(tr("group.discard_changes"), QMessageBox.ActionRole)
        cancel = prompt.addButton(tr("common.cancel"), QMessageBox.RejectRole)
        set_button_primary(save,True)
        prompt.setDefaultButton(save)
        prompt.setEscapeButton(cancel)
        prompt.exec()
        chosen = prompt.clickedButton()
        return "save" if chosen is save else "discard" if chosen is discard else "cancel"

    def _save_group(self, *, close):
        if not self._settle_group_controls():return False
        candidate = self.owner.soundfont_runtime.copy_group(self.group)
        if candidate.performance is None:
            candidate.performance = self.performance_model.base()
        errors = validate_group(candidate, allow_empty=True)
        if errors:
            NativeMessageBox.warning(self, tr("group.title"), "\n".join(errors))
            return False
        path = choose_group_save_path(self, candidate)
        if not path:
            return False
        try:
            saved = save_sfg(candidate, path)
        except (OSError, UnicodeError, ValueError) as exc:
            NativeMessageBox.critical(self, tr("group.save_failed_title"), str(exc))
            return False
        if self.owner.load_group(
            saved, fast_remap=self.owner.soundfont_runtime.can_remap_existing(saved),
        ):
            self.group = saved
            self._saved_group = self.owner.soundfont_runtime.copy_group(saved)
            self._original_group = self.owner.soundfont_runtime.copy_group(saved)
            if close:
                self.accept()
            return True
        NativeMessageBox.warning(
            self, tr("group.title"), tr("group.saved_not_applied", filename=Path(path).name),
        )
        return False

    def reject(self):
        """Window X and Esc have the same session-preserving meaning as Close."""
        self.close_preserving_runtime()

    def close_preserving_runtime(self):
        """Keep already sounding edits for this run without writing a file."""
        if not self._settle_group_controls():return
        creator = getattr(self, 'instrument_creator', None)
        if creator is not None and not creator.can_close(self.close_preserving_runtime):
            return
        self._finish_instrument_edit(True)
        if self.group != self._saved_group:
            self.owner.mark_unsaved_group()
        QDialog.accept(self)

    def _populate_instrument_rows(self):
        self._instrument_profile_base = (dict(self.group.performance) if self.group.performance is not None
                                         else {})
        self._instrument_summary_cache = {}
        played = ae.get_soundfont_group_played_state()
        overrides = self.group.preset_map()
        rows = [
            item for item in self._instrument_rows
            if (not self.filter_played.isChecked() or (item["bank"], item["preset"]) in played)
            and (not self.filter_mapped.isChecked() or (item["bank"], item["preset"]) in overrides)
        ]
        current = self._selected_instrument_key
        selected = set(getattr(self, "_selected_instrument_keys", ()))
        self.instrument_table.blockSignals(True)
        self._instrument_display_rows = rows
        self._instrument_override_lookup = overrides
        self._visible_instrument_rows = {
            (item["bank"], item["preset"]): row for row, item in enumerate(rows)
        }
        metrics = QFontMetrics(INSTRUMENT_TABLE.font())
        pad = 2 * INSTRUMENT_TABLE.horizontal_padding
        self._instrument_content_widths = [metrics.horizontalAdvance(text) + pad
                                           for text in INSTRUMENT_TABLE.headers]
        for item in rows:
            override = overrides.get((item["bank"], item["preset"]))
            for column, value in enumerate(self._instrument_values(item, override)):
                if column in INSTRUMENT_TABLE.named_columns:
                    self._instrument_content_widths[column] = max(
                        self._instrument_content_widths[column], metrics.horizontalAdvance(value) + pad,
                    )
        self._update_instrument_column_visibility(overrides)
        self.instrument_table.set_lazy_rows(len(rows), self._build_instrument_row)
        selection = QItemSelection()
        model = self.instrument_table.model()
        for key in selected or ({current} if current is not None else set()):
            if key in self._visible_instrument_rows:
                row = self._visible_instrument_rows[key]
                selection.select(model.index(row, 0), model.index(row, model.columnCount() - 1))
        self.instrument_table.selectionModel().select(selection, QItemSelectionModel.ClearAndSelect)
        if current in self._visible_instrument_rows:
            self.instrument_table.selectionModel().setCurrentIndex(
                model.index(self._visible_instrument_rows[current], 0), QItemSelectionModel.NoUpdate)
        self.instrument_table.blockSignals(False)
        self.count.setText(tr("group.instrument_count", count=len(self._instrument_rows)))

    def _build_instrument_row(self, row):
        table = self.instrument_table
        item = self._instrument_display_rows[row]
        override = self._instrument_override_lookup.get((item["bank"], item["preset"]))
        was_blocked = table.blockSignals(True)
        try:
            for column, value in enumerate(self._instrument_values(item, override)):
                cell = QTableWidgetItem(value)
                if column < 5 or column >= 12 + len(PRESET_EXPRESSION_RANGES):
                    cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                if 5 <= column <= 10:
                    cell.setData(NativeInstrumentRowDelegate.ROLE_NUMERIC_DOMAIN,
                                 (0, INSTRUMENT_VOLUME_MAX) if column == 5 else (-64, 63))
                elif 12 <= column < 12 + len(PRESET_EXPRESSION_RANGES):
                    field = INSTRUMENT_EXTRA_FIELDS[column - 12]
                    low, high, _default = PRESET_EXPRESSION_RANGES[field]
                    offset = 64 if field.startswith("vibrato_") else 0
                    cell.setData(NativeInstrumentRowDelegate.ROLE_NUMERIC_DOMAIN, (low - offset, high - offset))
                if column >= 12:
                    field = INSTRUMENT_EXTRA_FIELDS[column - 12]
                    cell.setToolTip(tr("group.changed_column_help", name=tr(f"group.column_help_{field}"), value=value or tr("group.factory_default")))
                cell.setTextAlignment(INSTRUMENT_TABLE.alignment(column))
                cell.setData(NativeInstrumentRowDelegate.ROLE_SETTINGS_DISABLED, bool(override and override.disabled))
                if column == 0:
                    cell.setData(Qt.UserRole, item)
                elif column == 3 and override and override.disabled:
                    cell.setToolTip(tr("group.mapping_disabled_tooltip"))
                self._present_instrument_cell(column, cell, override)
                table.setItem(row, column, cell)
        finally:
            table.blockSignals(was_blocked)

    def _instrument_values(self, item, override):
        def offset(field):
            value = None if override is None else getattr(override, field)
            return 0 if value is None else int(value) - 64

        mapped_name = item["filename"]
        if override is not None and override.index is not None and not override.disabled and 0 <= override.index < len(self.group.fonts):
            mapped_name = self.group.fonts[override.index].filename
        # Keep the state visible even if the name column is narrowed.
        name = item["name"]
        return (
            "", f"{item['bank']:03d}", f"{item['preset']:03d}", name, Path(mapped_name).name,
            str(100 if override is None or override.volume is None else override.volume),
            str(offset("attack")), str(offset("decay")), str(offset("release")),
            str(offset("cutoff")), str(offset("resonance")), "" if override is None or not override.memo else override.memo,
        ) + tuple(self._instrument_extra_value(item, override, field) for field in INSTRUMENT_EXTRA_FIELDS)

    def _present_instrument_cell(self, column, cell, override):
        inactive = bool(override and override.disabled)
        if column >= 12:
            field = INSTRUMENT_EXTRA_FIELDS[column - 12]
            section = next((section for section, members in PRESET_EXPRESSION_GROUPS.items() if field in members), None)
            inactive = inactive or bool(override and section in override.extended_disabled)
            help_text = tr("group.changed_column_help", name=tr(f"group.column_help_{field}"), value=cell.text() or tr("group.factory_default"))
            help_text=self._preset_default_tooltip(help_text)
            cell.setToolTip(tr("group.inactive_column_help", detail=help_text) if inactive else help_text)
        elif 6<=column<=10:
            field=('attack','decay','release','cutoff','resonance')[column-6]
            cell.setToolTip(self._preset_default_tooltip(effect_tooltip(field)))
        cell.setData(NativeInstrumentRowDelegate.ROLE_SETTINGS_DISABLED, inactive)

    def _instrument_extra_value(self, record, override, field):
        if field in PRESET_EXPRESSION_RANGES:
            default = PRESET_EXPRESSION_RANGES[field][2]
            value = override.extended.get(field, default) if override else default
            return str(value - 64 if field.startswith("vibrato_") else value)
        if (record["bank"] == 128) != field.startswith("drum_"):
            return ""
        key = record["bank"], record["preset"]
        if key not in self._instrument_summary_cache:
            summaries = {}
            values = {**self._instrument_profile_base, **(override.performance if override else {})}
            for name, value in values.items():
                if record["bank"] != 128 and name.startswith("scale_"):
                    note = int(name.removeprefix("scale_")); column = "scale"; neutral = 0
                elif record["bank"] == 128 and name.startswith("drum_"):
                    _prefix, note, member = name.split("_", 2); note = int(note)
                    column = f"drum_{member}"; neutral = getattr(DRUM_NEUTRAL, member)
                else: continue
                if value != neutral: summaries.setdefault(column, []).append((note, value))
            self._instrument_summary_cache[key] = {column: ", ".join(f"{note:03d}:{value}" for note, value in sorted(items))
                                                   for column, items in summaries.items()}
        return self._instrument_summary_cache[key].get(field, "")

    def _update_instrument_column_visibility(self, overrides=None):
        # Use the full catalogue: Played/Mapped filtering cannot conceal a
        # changed parameter in another row. No cell/selection-model rebuild.
        overrides = self.group.preset_map() if overrides is None else overrides
        changed = set()
        for key, item in overrides.items():
            row = self._instrument_row_by_key.get(key)
            if row is None: continue
            record = self._instrument_rows[row]
            for column, field in enumerate(("volume", "attack", "decay", "release", "cutoff", "resonance"), 5):
                value = getattr(item, field) if item else None
                if value is not None and value != (100 if field == "volume" else 64): changed.add(column)
            for column, field in enumerate(INSTRUMENT_EXTRA_FIELDS, 12):
                if field in PRESET_EXPRESSION_RANGES:
                    if item and item.extended.get(field, PRESET_EXPRESSION_RANGES[field][2]) != PRESET_EXPRESSION_RANGES[field][2]: changed.add(column)
                elif self._instrument_extra_value(record, item, field): changed.add(column)
        for record in (next((row for row in self._instrument_rows if row["bank"] == 128), None),
                       next((row for row in self._instrument_rows if row["bank"] != 128), None)):
            if record is not None:
                for column, field in enumerate(INSTRUMENT_EXTRA_FIELDS, 12):
                    if field not in PRESET_EXPRESSION_RANGES and self._instrument_extra_value(record, overrides.get((record["bank"], record["preset"])), field): changed.add(column)
        for column in (*range(5, 11), *range(12, len(INSTRUMENT_TABLE.headers))):
            self.instrument_table.setColumnHidden(column, column not in changed)

    def _refresh_selected_instrument_row(self):
        key = self._selected_instrument_key
        row = getattr(self, "_visible_instrument_rows", {}).get(key)
        if row is None:
            return
        item = self.instrument_table.item(row, 0)
        record = item.data(Qt.UserRole) if item else None
        if not record:
            return
        override = self.selected_override(False)
        self._instrument_summary_cache.pop(key, None)
        values = self._instrument_values(record, override)
        disabled = bool(override and override.disabled)
        blocked = self.instrument_table.blockSignals(True)
        try:
            for column, value in enumerate(values):
                cell = self.instrument_table.item(row, column)
                if column == 0:
                    cell.setData(Qt.UserRole, record)
                if cell.text() != value:
                    cell.setText(value)
                    if column in INSTRUMENT_TABLE.named_columns and column not in self._instrument_user_columns:
                        target = INSTRUMENT_TABLE.fitted_width(value)
                        if target > self._instrument_content_widths[column]:
                            self._instrument_content_widths[column] = target
                            self._instrument_auto_fitting = True
                            try:
                                self.instrument_table.setColumnWidth(column, target)
                            finally:
                                self._instrument_auto_fitting = False
                if cell.data(NativeInstrumentRowDelegate.ROLE_SETTINGS_DISABLED) != disabled:
                    cell.setData(NativeInstrumentRowDelegate.ROLE_SETTINGS_DISABLED, disabled)
                self._present_instrument_cell(column, cell, override)
        finally:
            self.instrument_table.blockSignals(blocked)

        self._update_instrument_column_visibility()

    def edit_instrument_cell(self, row, column):
        if column < 5:
            return
        table = self.instrument_table
        item = table.item(row, column)
        if item is None or not item.flags() & Qt.ItemIsEditable:
            return
        if getattr(self, "_instrument_edit_item", None) is item:
            self._instrument_editor.setFocus()
            return
        self._finish_instrument_edit(True)
        table.openPersistentEditor(item)
        editor = table.indexWidget(table.indexFromItem(item))
        if not isinstance(editor, QLineEdit):
            table.closePersistentEditor(item)
            return
        self._instrument_edit_item = item
        self._instrument_edit_initial = item.text()
        self._instrument_editor = editor
        editor.installEventFilter(self)
        editor.setFocus()
        editor.selectAll()

    def _finish_instrument_edit(self, commit):
        item = getattr(self, "_instrument_edit_item", None)
        if item is None:
            return
        editor = self._instrument_editor
        original = self._instrument_edit_initial
        self._instrument_edit_item = None
        self._instrument_editor = None
        editor.removeEventFilter(self)
        valid = (editor.entered_number() is not None
                 if isinstance(editor, NativeInlineNumberEdit)
                 else editor.hasAcceptableInput())
        if not commit or not valid:
            editor.setText(original)
        elif editor.text() != original:
            index = self.instrument_table.indexFromItem(item)
            self.instrument_table.itemDelegateForIndex(index).setModelData(editor, self.instrument_table.model(), index)
        self.instrument_table.closePersistentEditor(item)

    def eventFilter(self, watched, event):
        if watched is getattr(self,'_preset_inspector_scroll',None) and event.type()==QEvent.Resize:
            self._sync_inspector_extent()
        if watched is getattr(self, "_instrument_editor", None):
            if event.type() == QEvent.KeyPress:
                if event.key() == Qt.Key_Escape:
                    self._finish_instrument_edit(False)
                    return True
                if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                    self._finish_instrument_edit(True)
                    return True
            if event.type() == QEvent.FocusOut:
                self._finish_instrument_edit(True)
                return True
        return super().eventFilter(watched, event)

    def commit_instrument_cell(self, cell):
        column = cell.column()
        if column < 5 or column >= 12 + len(PRESET_EXPRESSION_RANGES):
            return
        record_cell = self.instrument_table.item(cell.row(), 0)
        record = record_cell.data(Qt.UserRole) if record_cell else None
        if not record:
            return
        key = record["bank"], record["preset"]
        field = (("volume", "attack", "decay", "release", "cutoff", "resonance", "memo")[column - 5]
                 if column <= 11 else INSTRUMENT_EXTRA_FIELDS[column - 12])
        value = cell.text().strip()
        if column != 11:
            try:
                number = int(value)
            except ValueError:
                number = None
            low, high = cell.data(NativeInstrumentRowDelegate.ROLE_NUMERIC_DOMAIN)
            if number is None or not low <= number <= high:
                previous = self._instrument_override_lookup.get(key)
                correct = self._instrument_values(record, previous)[column]
                blocked = self.instrument_table.blockSignals(True)
                cell.setText(correct)
                self.instrument_table.blockSignals(blocked)
                return
            value = number + (64 if 6 <= column <= 10 or field.startswith("vibrato_") else 0)
        override = self._instrument_override_lookup.get(key)
        if override is None:
            override = PresetOverride(*key, index=record["source_index"])
            self.group.presets.append(override)
            self._instrument_override_lookup[key] = override
        if column >= 12:
            override.extended[field] = value
            section = next(section for section, members in PRESET_EXPRESSION_GROUPS.items() if field in members)
            override.extended_disabled.discard(section)
        else:
            setattr(override, field, value or None if column == 11 else value)
        if self._apply_live(("preset_controls", *key, override)):
            self._selected_instrument_key = key
            self.select_preset()
            self._refresh_selected_instrument_row()

    def _refresh_instrument_assignment(self, key):
        """Resolve one destination; preserve both large table models and scroll."""
        if not self.filter_all.isChecked():
            self.refresh_instruments()
            return
        catalog = self._instrument_catalog_by_key.get(key, ())
        resolved = resolve_effective_instruments(self.group, catalog)
        index = self._instrument_row_by_key.get(key)
        if index is None or len(resolved) != 1:
            self.refresh_instruments()
            return
        self._instrument_rows[index] = resolved[0]
        self._instrument_override_lookup[key] = self.group.preset_map().get(key)
        visible_row = self._visible_instrument_rows.get(key)
        if visible_row is not None:
            self._instrument_display_rows[visible_row] = resolved[0]
            self.instrument_table.refresh_row(visible_row)
        self.routing_table.refresh_row(index)
        if self._selected_instrument_key == key:
            self.select_preset()

    @ae.activity_observation
    def refresh_activity(self):
        if not self.isVisible():
            return
        visual = ae.get_soundfont_group_note_visual_state()
        if self.filter_played.isChecked():
            played = ae.get_soundfont_group_played_state()
            if getattr(self, "_played_snapshot", set()) != played:
                self._played_snapshot = played
                self.populate_instruments()
        table = self.routing_table if self.group_tabs.currentIndex() == 0 else self.instrument_table
        viewport = table.viewport()
        first = max(0, table.rowAt(0))
        last = table.rowAt(max(0, viewport.height() - 1))
        if last < 0:
            last = table.rowCount() - 1
        for row in range(first, min(last + 1, table.rowCount())):
            item = table.item(row, 0)
            record = item.data(Qt.UserRole) if item else None
            if record:
                intensity = visual.get((record["bank"], record["preset"]), 0.0)
                if item.data(NativeActivityCellDelegate.ROLE_INTENSITY) != intensity:
                    item.setData(NativeActivityCellDelegate.ROLE_INTENSITY, intensity)

    def selected_override(self, create=False):
        row = self.instrument_table.currentRow()
        item = self.instrument_table.item(row, 0) if row >= 0 else None
        record = item.data(Qt.UserRole) if item else None
        if not record:
            return None
        key = record["bank"], record["preset"]
        override = getattr(self, "_instrument_override_lookup", {}).get(key)
        if override is not None:
            return override
        if create:
            override = PresetOverride(record["bank"], record["preset"], index=record["source_index"])
            self.group.presets.append(override)
            self._instrument_override_lookup[key] = override
            return override
        return None

    def _clear_preset_inspector(self):
        self._selected_instrument_key = None
        self._selected_instrument_keys = ()
        was_blocked = self.instrument_table.blockSignals(True)
        self.instrument_table.clearSelection()
        self.instrument_table.blockSignals(was_blocked)
        self.preset_title.clear()
        if hasattr(self,'preset_font_cc'):
            self.preset_font_cc.clear();self.preset_font_cc.hide();self.preset_font_cc.setToolTip('')
            self.preset_cc_detail.clear()
            self.preset_cc_toggle.setChecked(False);self.preset_cc_toggle.setEnabled(False)
        for key, control in self.preset_controls.items():
            control.set(100 if key == "volume" else 0, call=False)
        self.preset_memo.blockSignals(True)
        self.preset_memo.clear()
        self.preset_memo.blockSignals(False)
        self.preset_enabled.blockSignals(True)
        self.preset_enabled.setChecked(False)
        self.preset_enabled.blockSignals(False)
        self.preset_enabled.setEnabled(False)
        self.preset_font.setEnabled(False)
        self.preset_memo.setEnabled(False)
        self.preset_enabled.setProperty("mixedValue", False)
        for control in self.preset_controls.values():
            control.setEnabled(False)
        self._refresh_instrument_tuning()
        for section, switch in self.preset_extension_switches.items():
            NativePerformanceDialog._present_switch(switch,[False])
            switch.setEnabled(False)
            for key in PRESET_EXPRESSION_GROUPS[section]:
                field=self.preset_extension_controls[key]
                field.set(PRESET_EXPRESSION_RANGES[key][2],False)
                field.setEnabled(False)

    def select_preset(self):
        records = self._selected_preset_records()
        if not records:
            self._clear_preset_inspector()
            self._selected_instrument_keys = ()
            self._refresh_font_cc_inspector()
            return
        record = records[0]
        overrides = [self.group.preset_map().get((row["bank"], row["preset"])) for row in records]
        override = overrides[0]
        self._selected_instrument_keys = tuple((row["bank"], row["preset"]) for row in records)
        self._selected_instrument_key = (record["bank"], record["preset"])
        self.preset_title.setText(f"{record['bank']:03d}:{record['preset']:03d}  {record['name']}" if len(records) == 1
                                  else tr("group.selected_count", count=len(records)))
        self.preset_font.blockSignals(True)
        self.preset_font.clear()
        self.preset_font.addItem(tr("group.priority_fallback"), None)
        for index, font in enumerate(self.group.fonts):
            self.preset_font.addItem(font.label, index)
            self.preset_font.setItemData(index + 1, font.source_description if font.custom is not None else os.path.normpath(str(self.group.font_path(font))), Qt.ToolTipRole)
        sources = [item.index if item else None for item in overrides]
        common_source = all(value == sources[0] for value in sources)
        self.preset_font.setPlaceholderText(tr("channel_editor.mixed_value"))
        self.preset_font.setCurrentIndex(self.preset_font.findData(sources[0]) if common_source else -1)
        self.preset_font.blockSignals(False)
        available = any(not (item and item.disabled) for item in overrides)
        for key, control in self.preset_controls.items():
            values = []
            for item in overrides:
                raw = getattr(item, key) if item and not item.disabled else None
                raw = (100 if key == "volume" else 64) if raw is None else raw
                values.append(raw if key == "volume" else raw - 64)
            present_numeric_selection(control, values, 100 if key == "volume" else 0)
            control.setEnabled(available)
        for section, switch in self.preset_extension_switches.items():
            enabled = [bool(item and not item.disabled and section not in item.extended_disabled
                            and any(key in item.extended for key in PRESET_EXPRESSION_GROUPS[section])) for item in overrides]
            NativePerformanceDialog._present_switch(switch, enabled)
            switch.setEnabled(available)
            for key in PRESET_EXPRESSION_GROUPS[section]:
                field = self.preset_extension_controls[key]
                default = PRESET_EXPRESSION_RANGES[key][2]
                present_numeric_selection(field, [item.extended.get(key, default) if item and state else default
                                                 for item, state in zip(overrides, enabled)], default)
                field.setEnabled(available and any(enabled))
        self.preset_memo.blockSignals(True)
        memos = [item.memo or "" if item else "" for item in overrides]
        self.preset_memo.setText(memos[0] if len(set(memos)) == 1 else "")
        self.preset_memo.setPlaceholderText(tr("channel_editor.mixed_value") if len(set(memos)) > 1 else "")
        self.preset_memo.setModified(False)
        self.preset_memo.blockSignals(False)
        self.preset_font.setEnabled(True); self.preset_memo.setEnabled(True)
        NativePerformanceDialog._present_switch(self.preset_enabled, [not (item and item.disabled) for item in overrides])
        self.preset_enabled.setEnabled(True)
        self._refresh_instrument_tuning()
        self._refresh_font_cc_inspector()

    def _refresh_font_cc_inspector(self):
        records=self._selected_preset_records()
        if len(records)!=1:
            self.preset_font_cc.clear();self.preset_font_cc.hide();self.preset_cc_detail.clear()
            self.preset_cc_toggle.setChecked(False);self.preset_cc_toggle.setEnabled(False);return
        record=records[0];entry=self.group.fonts[record['source_index']]
        report=font_controller_index(self.owner).request(self.group.font_path(entry),
            (self._active_instrument_catalog_key,self.owner.soundfont_runtime.source_generation))
        values={cc:font_controller_text(report,record.get('source_bank',record['bank']),
            record.get('source_preset',record['preset']),cc) for cc in (21,22,23,24)}
        self.preset_font_cc.setText('\n'.join(tr('font_cc.summary_item',cc=cc,status=value[0]) for cc,value in values.items()))
        self.preset_font_cc.show();self.preset_font_cc.setToolTip('')
        known_empty=font_controller_has_links(report,record.get('source_bank',record['bank']),
            record.get('source_preset',record['preset'])) is False
        self.preset_cc_toggle.setEnabled(not known_empty)
        if known_empty:self.preset_cc_toggle.setChecked(False)
        text='\n\n'.join(value[1] for value in values.values())
        if self.preset_cc_detail.toPlainText()!=text:self.preset_cc_detail.setPlainText(text)
        invalidate_layout_chain(self.preset_font_cc)

    def _selected_preset_records(self):
        return [self._instrument_display_rows[index.row()]
                for index in sorted(self.instrument_table.selectionModel().selectedRows(), key=lambda index: index.row())
                if 0 <= index.row() < len(self._instrument_display_rows)]

    def _selected_preset_keys(self):
        return tuple((row["bank"], row["preset"]) for row in self._selected_preset_records())

    def _select_all_instruments(self):
        self.instrument_table.selectAll()

    def _preset_for_key(self, key):
        item = self._instrument_override_lookup.get(key)
        if item is None:
            item = PresetOverride(*key)
            self.group.presets.append(item)
        self._instrument_override_lookup[key] = item
        return item

    def _edit_selected_presets(self, field, value, *, mapping=False):
        if field not in ('volume','attack','decay','release','cutoff','resonance','index','disabled','memo'):return
        keys = self._selected_preset_keys()
        if not keys or (field == "index" and self.preset_font.currentIndex() < 0):
            return
        for key in keys:
            item = self._preset_for_key(key)
            if item.disabled and not mapping and field != "memo":
                continue
            setattr(item, field, value)
            if field == "index": item.disabled = False
        if self._apply_live("font_mapping" if mapping else self._preset_batch_change(keys)):
            if mapping:
                if self.filter_all.isChecked():
                    for key in keys: self._refresh_instrument_assignment(key)
                else:
                    self.refresh_instruments()
            else:
                self._refresh_visible_preset_rows(keys)
            if mapping:self.select_preset()

    def _refresh_visible_preset_rows(self, keys):
        # Keep materialized cell identities alive during inline entry and
        # navigation. Replacing row items would invalidate a live editor.
        overrides = self.group.preset_map()
        blocked = self.instrument_table.blockSignals(True)
        try:
            for key in keys:
                self._instrument_summary_cache.pop(key, None)
                row = self._visible_instrument_rows.get(key)
                if row is None or row not in self.instrument_table._materialized_rows: continue
                record = self._instrument_display_rows[row]
                item = overrides.get(key)
                for column, value in enumerate(self._instrument_values(record, item)):
                    cell = self.instrument_table.item(row, column)
                    if cell is None: continue
                    cell.setText(value)
                    cell.setData(NativeInstrumentRowDelegate.ROLE_SETTINGS_DISABLED, bool(item and item.disabled))
                    self._present_instrument_cell(column, cell, item)
        finally:
            self.instrument_table.blockSignals(blocked)

        self._update_instrument_column_visibility(overrides)

    def _preset_batch_change(self, keys):
        if len(keys) == 1:
            item = self.group.preset_map().get(keys[0])
            return ("preset_controls", *keys[0], item)
        return ("preset_batch", tuple(keys))

    def _edit_selected_memo(self):
        if self.preset_memo.isModified():
            self._edit_selected_presets("memo", self.preset_memo.text().strip() or None)

    def _show_preset_control_values(self, override):
        disabled = bool(override and override.disabled)
        for key, control in self.preset_controls.items():
            stored = None if override is None else getattr(override, key)
            raw = (100 if key == "volume" else 64) if disabled or stored is None else stored
            control.set(raw if key == "volume" else raw - 64, call=False)

    @staticmethod
    def _preset_default_tooltip(detail):
        return tr('group.preset_default_tooltip',detail=detail)

    def _change_preset_extension(self, section, *, enabled=None, field=None, value=None):
        keys=self._selected_preset_keys()
        if not keys:return
        for key in keys:
            item=self._preset_for_key(key)
            if item.disabled:continue
            if enabled is False:item.extended_disabled.add(section)
            else:
                item.extended_disabled.discard(section)
                if enabled is True:
                    for member in PRESET_EXPRESSION_GROUPS[section]:
                        item.extended.setdefault(member,PRESET_EXPRESSION_RANGES[member][2])
                if field is not None:item.extended[field]=int(round(value))
        if self._apply_live(self._preset_batch_change(keys)):
            self._refresh_visible_preset_rows(keys)
            if enabled is not None:self.select_preset()

    def _preset_extension_section(self, section):
        title_key={'tuning':'tuning','vibrato':'vibrato','modulation':'modulation_routes',
                   'channel_pressure':'channel_pressure_routes','key_pressure':'key_pressure_routes'}[section]
        title=tr('channel_editor.group_'+title_key)
        container,grid=make_control_section(title,tr('group.extension_help'),collapsible=True,checkable=True)
        container.disclosure.heightChanged.connect(self._sync_inspector_extent)
        self._preset_extension_sections[section]=container
        switch=container.toggle_switch
        self.preset_extension_switches[section]=switch
        switch.setAccessibleName(tr('group.default_section_use',section=title))
        switch.toggled.connect(lambda enabled:self._change_preset_extension(section,enabled=enabled))
        for column,key in enumerate(PRESET_EXPRESSION_GROUPS[section]):
            low,high,default=PRESET_EXPRESSION_RANGES[key]
            parameter=CHANNEL_PARAMETERS[key];kind=parameter.entry_kind
            field=NativeKnobField(tr('channel_editor.'+key),low,high,default,
                lambda value,key=key:self._change_preset_extension(section,field=key,value=value),
                lambda value,kind=kind:format_effect_value(value,kind),
                tooltip=self._preset_default_tooltip(effect_tooltip(key)),editable=True,
                entry_domain=parameter.entry_domain,edit_context=self._selected_preset_keys)
            grid.addWidget(field,0,column);grid.setColumnStretch(column,1)
            self.preset_extension_controls[key]=field
        container.disclosure.set_expanded(True,refresh_extent=True)
        return container

    def _build_instrument_tuning(self):
        page=QWidget();layout=set_layout(QVBoxLayout(page),0,6);layout.setAlignment(Qt.AlignTop)
        self.instrument_scale_section,grid=make_control_section(tr('performance.scale_section'))
        self.instrument_scale_reset=make_action_button(tr('common.restore_defaults'),lambda:self._reset_instrument_tuning('scale_'))
        install_effect_tooltip(tr('group.tuning_reset_help'),self.instrument_scale_reset)
        self.instrument_scale_fields={}
        for pitch,note in enumerate(('C','C♯','D','D♯','E','F','F♯','G','G♯','A','A♯','B')):
            field=NativeKnobField(note,SCALE_PARAMETER.minimum,SCALE_PARAMETER.maximum,SCALE_PARAMETER.default,
                lambda value,pitch=pitch:self._edit_instrument_tuning(f'scale_{pitch}',value),
                lambda value:format_effect_value(value,'cents'),
                tooltip=self._preset_default_tooltip(effect_tooltip('scale_tuning')),editable=True,
                entry_domain=SCALE_PARAMETER.entry_domain,edit_context=self._tuning_context)
            grid.addWidget(field,pitch//3,pitch%3);grid.setColumnStretch(pitch%3,1)
            self.instrument_scale_fields[pitch]=field
            preview=make_tuning_audition(self.owner,
                lambda pitch=pitch:self._tuning_preview_request(60+pitch),field)
            field.add_caption_action(preview)
        grid.addWidget(self.instrument_scale_reset,4,0,1,3,Qt.AlignRight)
        layout.addWidget(self.instrument_scale_section)
        self.instrument_drum_section,grid=make_control_section(tr('performance.drum_section'))
        self.instrument_drum_key=AnimatedComboBox()
        for key in range(128):self.instrument_drum_key.addItem(drum_key_caption(key),key)
        self.instrument_drum_key.setCurrentIndex(36)
        self.instrument_drum_key.setAccessibleName(tr('performance.drum_key'))
        self.instrument_drum_key.currentIndexChanged.connect(self._refresh_instrument_tuning)
        install_effect_tooltip(effect_tooltip('drum_key'),self.instrument_drum_key)
        self.instrument_drum_preview=make_tuning_audition(self.owner,
            lambda:self._tuning_preview_request(self.instrument_drum_key.currentIndex()),self.instrument_drum_section,one_shot=True)
        add_section_action(self.instrument_drum_section,self.instrument_drum_preview)
        grid.addWidget(self.instrument_drum_key,0,0,1,3)
        self.instrument_drum_fields={}
        # Visual groups are independent of the MIDI/model parameter order.
        field_order=('coarse_tuning','fine_tuning','level','pan','cutoff','resonance',
                     'reverb','chorus','userfx')
        for index,key in enumerate(field_order):
            parameter=DRUM_PARAMETERS[key]
            field=NativeKnobField(tr('performance.drum_'+key),parameter.minimum,parameter.maximum,parameter.default,
                lambda value,key=key:self._edit_instrument_tuning(f'drum_{self.instrument_drum_key.currentIndex()}_{key}',value),
                lambda value,kind=parameter.entry_kind:format_effect_value(value,kind),
                tooltip=self._preset_default_tooltip(effect_tooltip('drum_'+key)),editable=True,
                entry_domain=parameter.entry_domain,
                edit_context=self._tuning_context)
            if key in ('reverb','chorus','userfx'):
                field.title.setText(field.title.text().replace(' ','\n',1))
            grid.addWidget(field,index//3+1,index%3);grid.setColumnStretch(index%3,1)
            self.instrument_drum_fields[key]=field
        self.instrument_drum_reset=make_action_button(tr('common.restore_defaults'),lambda:self._reset_instrument_tuning('drum_'))
        install_effect_tooltip(tr('group.tuning_reset_help'),self.instrument_drum_reset)
        grid.addWidget(self.instrument_drum_reset,4,0,1,3,Qt.AlignRight)
        layout.addWidget(self.instrument_drum_section)
        return page

    def _tuning_context(self):
        return self._selected_preset_keys(),self.instrument_drum_key.currentIndex()

    def _tuning_preview_request(self,note):
        records=self._selected_preset_records()
        if len(records)!=1:return None
        row=records[0];runtime=self.owner.soundfont_runtime
        if not 0<=row['source_index']<len(runtime.handles):return None
        key=(row['bank'],row['preset']);item=self.group.preset_map().get(key)
        from dataclasses import asdict
        preset=asdict(item) if item is not None and not item.disabled else {}
        profile={**self.performance_model.base(),**preset.get('performance',{})}
        return dict(note=note,identity=key,drum=row['bank']==128,
            source=(runtime.handles[row['source_index']],row.get('source_bank',row['bank']),
                    row.get('source_preset',row['preset'])),profile=profile,preset=preset)

    def _tuning_targets(self,kind):
        return tuple(key for key in self._selected_preset_keys()
                     if (key[0]==128)==(kind=='drum'))

    def _instrument_phrase_request(self):
        request=self._tuning_preview_request(69)
        if request is None:return None
        from midi_player import instrument_audition_phrase
        bank,program=request['identity']
        phrase,family=instrument_audition_phrase(bank,program,drum=request['drum'])
        request['phrase']=phrase
        request['period']=32*60/116
        self.instrument_audition_button.setToolTip(tr('instrument_audition.family_help',
            family=tr(f'instrument_audition.families.{family}')))
        return request

    def _refresh_instrument_tuning(self,*_args):
        if not hasattr(self,'instrument_drum_key'):return
        mode=getattr(self.owner,'_report_playback_snapshot',{}).get('midi_system_mode')
        if getattr(self,'_drum_caption_mode',None)!=mode or not hasattr(self,'_drum_caption_mode'):
            self._drum_caption_mode=mode
            for key in range(128):self.instrument_drum_key.setItemText(key,drum_key_caption(key,mode=mode))
        one=len(self._selected_preset_keys())==1
        self.instrument_drum_preview.setEnabled(one)
        if hasattr(self,'instrument_audition_button'):self.instrument_audition_button.setEnabled(one)
        for field in self.instrument_scale_fields.values():field.side_action.setEnabled(one)
        base=self.performance_model.base();overrides=self.group.preset_map()
        for kind,section,fields in (('scale',self.instrument_scale_section,self.instrument_scale_fields),
                                    ('drum',self.instrument_drum_section,self.instrument_drum_fields)):
            targets=self._tuning_targets(kind)
            section.setVisible(bool(targets))
            available=any(key not in overrides or not overrides[key].disabled for key in targets)
            section.setEnabled(available)
            profiles=[{**base,**(overrides[key].performance if key in overrides and not overrides[key].disabled else {})}
                      for key in targets]
            for key,field in fields.items():
                name=f'scale_{key}' if kind=='scale' else f'drum_{self.instrument_drum_key.currentIndex()}_{key}'
                neutral=0 if kind=='scale' else DRUM_PARAMETERS[key].default
                field.knob.set_neutral(base.get(name,neutral))
                present_numeric_selection(field,[profile.get(name,neutral) for profile in profiles],neutral)
                if kind=='drum' and key=='userfx':
                    field.setEnabled(available)
                    detail=self._preset_default_tooltip(effect_tooltip('drum_userfx'))
                    install_effect_tooltip(detail,field,field.title,field.knob,field.value)
        self._sync_inspector_extent()

    def _edit_instrument_tuning(self,name,value,neutral=None):
        kind='scale' if name.startswith('scale_') else 'drum'
        targets=self._tuning_targets(kind)
        if not targets:return
        parameter=SCALE_PARAMETER if kind=='scale' else DRUM_PARAMETERS[name.split('_',2)[2]]
        amount=parameter.clamp(value)
        inherited=self.performance_model.base().get(name,parameter.default if neutral is None else neutral)
        for key in targets:
            item=self._preset_for_key(key)
            if item.disabled:continue
            if amount==inherited:item.performance.pop(name,None)
            else:item.performance[name]=amount
        if self._apply_live(self._preset_batch_change(targets)):
            self._refresh_visible_preset_rows(targets);self._refresh_instrument_tuning()

    def _reset_instrument_tuning(self,prefix):
        targets=self._tuning_targets('scale' if prefix=='scale_' else 'drum')
        if not targets:return
        for key in targets:
            item=self.group.preset_map().get(key)
            if item is None or item.disabled:continue
            for name in tuple(item.performance):
                if name.startswith(prefix):item.performance.pop(name)
        if self._apply_live(self._preset_batch_change(targets)):
            self._refresh_visible_preset_rows(targets);self._refresh_instrument_tuning()

    def _select_preset_page(self, index):
        self.preset_pages.setCurrentIndex(index)

    def _add_preset_page(self, page, caption):
        # The inspector owns overflow;each tab is content,not another viewport.
        host = WidthMeasuredPanel(); layout = bordered_page_layout(QVBoxLayout(host))
        host.setMinimumSize(0,0);host.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        layout.addWidget(page, 0, Qt.AlignTop)
        self.preset_pages.addTab(host, caption)

    def _sync_inspector_extent(self,*_args):
        """Preserve complete fields inside the outer constrained scroll area."""
        tabs=getattr(self,'preset_pages',None)
        if tabs is None or tabs.currentWidget() is None:return
        tabs._page_extent_changed()

    def change_preset(self, *_):
        # Compatibility entry point; never snapshot unrelated form fields.
        sender = self.sender()
        if sender is self.preset_enabled:
            self._edit_selected_presets("disabled", not sender.isChecked(), mapping=True)
        elif sender is self.preset_font:
            self._edit_selected_presets("index", sender.currentData(), mapping=True)
        elif sender is self.preset_memo:
            self._edit_selected_memo()

    def clear_played_history(self):
        # BMSynth btDelListClick 00472B90 clears the played-list count and
        # redraws its grid; it does not reset MIDI voices or SoundFonts.
        ae.clear_soundfont_group_played_history()
        self._played_snapshot = set()
        self.populate_instruments()

    def export_instruments(self, format_name):
        """Export the full cached instrument catalog, regardless of view filter."""
        if not self._instrument_rows:
            return
        suffix = ".ins" if format_name == "ins" else ".nwcitree"
        default_name = Path(self.group.source_path).stem if self.group.source_path else "ABMSynth"
        filter_name = tr("group.export_ins_filter") if format_name == "ins" else tr("group.export_nwc_filter")
        path, _ = QFileDialog.getSaveFileName(self, tr("group.export_title"), default_name + suffix, filter_name)
        if not path:
            return
        destination = Path(path)
        if not destination.suffix:
            destination = destination.with_suffix(suffix)
        try:
            # Export remains the full source catalog, independent of the
            # one-effective-row navigation view and its active filter.
            save_instrument_export(
                self.group, self._instrument_catalog_cache[self._active_instrument_catalog_key],
                format_name, destination,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            NativeMessageBox.critical(self, tr("group.export_failed_title"), str(exc))

    def __init__(self, owner, group):
        QDialog.__init__(self, owner)
        self.owner, self.group = owner, owner.soundfont_runtime.copy_group(group)
        # _original_group is the committed runtime rollback target. _saved_group
        # is the staged editor's last save/new/load checkpoint for prompts;
        # after New they deliberately differ until the new group is applied.
        self._original_group = owner.soundfont_runtime.copy_group(group)
        self._saved_group = owner.soundfont_runtime.copy_group(group)
        self._instrument_rows, self._selected_instrument_key = [], None
        # This cache is deliberately separate from the visible table.  The
        # All/Played/Mapped filters may redraw their subset freely, but never
        # need to enumerate presets from BASS again for the same font files.
        self._instrument_catalog_cache = {}
        self._active_instrument_catalog_key = None
        self._instrument_user_columns = set()
        self._instrument_auto_fitting = False
        self._routing_font_signature = None
        self.setWindowTitle(tr("group.window_title"))
        self.resize(1080, 720)
        self.setMinimumSize(0, 0)
        self.setSizeGripEnabled(True)
        self.setFont(owner.font())
        apply_native_text_metrics(self)
        root = set_layout(QVBoxLayout(self), METRICS["outer"], METRICS["section_gap"])
        root.setSizeConstraint(QLayout.SetNoConstraint)
        header = control_row_layout()
        title = QLabel(tr("group.title")); title.setObjectName("DialogTitle"); header.addWidget(title)
        self.count = QLabel(); self.count.setObjectName("StatusChip"); header.addWidget(self.count)
        self.export_button = make_action_button(tr("group.export_instruments"), lambda: None)
        self.export_button.setToolTip(tr("group.export_instruments_tooltip"))
        export_menu = NativeMenu(self.export_button)
        for caption, format_name in (
            (tr("group.export_nwc"), "nwc"),
            (tr("group.export_nwc_gm"), "nwc_gm"),
            (tr("group.export_ins"), "ins"),
        ):
            action = export_menu.addAction(caption)
            action.triggered.connect(lambda _checked=False, kind=format_name: self.export_instruments(kind))
        self.export_button.setMenu(export_menu)
        header.addSpacing(METRICS["compact_content_gap"])
        header.addWidget(self.export_button)
        header.addStretch(1)
        header.addWidget(make_action_button(tr("action.new_group"), self.new_group))
        root.addLayout(header)

        self.group_tabs = make_navigation_tabs()
        self.group_tabs.addTab(self._group_page(), tr("tab.soundfont_group"))
        self.group_tabs.addTab(self._settings_page(), tr("tab.instrument_settings"))
        self.instrument_creator = None
        self._creator_page = QWidget()
        self._creator_page.setMinimumSize(0, 0)
        bordered_page_layout(QVBoxLayout(self._creator_page))
        self.group_tabs.addTab(self._creator_page, tr('creator.title'))
        self.group_tabs.currentChanged.connect(self._materialize_active_tab)
        root.addWidget(self.group_tabs, 1)

        footer = control_row_layout()
        footer.addStretch(1)
        for caption, tooltip, callback, primary in (
            ("group.save_sfg_and_close", "group.save_sfg_and_close_tooltip", self.save_close, True),
            ("group.close", "group.close_tooltip", self.close_preserving_runtime, False),
        ):
            button = make_action_button(tr(caption), callback, primary=primary)
            button.setDefault(primary)
            button.setToolTip(tr(tooltip))
            footer.addWidget(button)
            if primary:self.save_group_button=button
            else:self.close_group_button=button
        root.addLayout(footer)
        self._sync_save_actions()

        self.refresh_fonts()
        QTimer.singleShot(0, self.refresh_instruments)
        self.poll = QTimer(self)
        self.poll.timeout.connect(self.refresh_activity)
        self.poll.setInterval(MIDI_ACTIVITY_INTERVAL_MS)
    def _materialize_active_tab(self, index):
        if index == 2:
            if self.instrument_creator is None:
                from native_instrument_creator import NativeInstrumentCreator
                self.instrument_creator = NativeInstrumentCreator(self)
                self._creator_scroll=QScrollArea()
                self._creator_scroll.setObjectName('InstrumentCreatorScroll')
                self._creator_scroll.setFrameShape(QFrame.NoFrame)
                self._creator_scroll.setMinimumSize(0,0)
                self._creator_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
                self._creator_scroll.setWidgetResizable(True)
                self._creator_scroll.setStyleSheet(transparent_scroll_area_stylesheet())
                set_transparent_scroll_content(self._creator_scroll,self.instrument_creator)
                self._creator_page.layout().addWidget(self._creator_scroll)
            return
        if self.instrument_creator is not None:
            self.instrument_creator.stop_preview()
        table = self.routing_table if index == 0 else self.instrument_table
        QTimer.singleShot(0, table._materialize_visible)

    def add_created_instrument(self, record, runtime_path, entry_id=None):
        from uuid import uuid4
        entry=SoundFontEntry(entry_id or 'ABMSynth-'+uuid4().hex,100,record,runtime_path)
        index=next((i for i,font in enumerate(self.group.fonts) if font.filename==entry_id),len(self.group.fonts))
        old=self.owner.soundfont_runtime.copy_group(self.group)
        if index<len(self.group.fonts):
            previous=self.group.fonts[index].custom
            if previous and (previous['bank'],previous['program'])!=(record['bank'],record['program']):
                prior=self.group.preset_map().get((previous['bank'],previous['program']))
                if prior is not None and prior.index==index:prior.index=None
            entry.volume=self.group.fonts[index].volume;self.group.fonts[index]=entry
        else:self.group.fonts.append(entry)
        # Explicit admission makes replacement deterministic, not dependent
        # on the user's existing fallback-font order.
        address=(record['bank'],record['program'])
        override=self.group.preset_map().get(address)
        if override is None:
            override=PresetOverride(*address);self.group.presets.append(override)
        override.index=index;override.disabled=False
        self.refresh_fonts(update_routing=False)
        if self._apply_live('font_layout'):
            self.refresh_instruments(force=True)
            self.owner.mark_unsaved_group()
            return entry.filename
        self.group=old
        return None

    def _group_page(self):
        page = QWidget()
        layout = bordered_page_layout(QVBoxLayout(page), METRICS["section_gap"])
        card = NativeCard()
        self.font_card = card
        card.setMinimumHeight(0)
        card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        card_layout = set_layout(QVBoxLayout(card), METRICS["card_padding"], CARD_ROW_GAPS["group"])
        actions = control_row_layout()
        self.font_actions = {}
        for key, caption, slot in (
            ("add", tr("common.add_font"), self.add_fonts),
            ("up", tr("common.move_up"), lambda: self.move_font(-1)),
            ("down", tr("common.move_down"), lambda: self.move_font(1)),
            ("delete", tr("common.delete"), self.remove_font),
        ):
            button = make_action_button(caption, slot)
            self.font_actions[key] = button
            actions.addWidget(button)
        actions.addStretch(1)
        card_layout.addLayout(actions)

        self.font_table = NativeFontOrderTable(0, 2)
        self.font_table.setHorizontalHeaderLabels([tr("common.volume"), tr("group.font_file")])
        for column in range(self.font_table.columnCount()):
            self.font_table.horizontalHeaderItem(column).setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.font_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.font_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        self.font_table.horizontalHeader().setStretchLastSection(True)
        self.font_table.verticalHeader().setVisible(False)
        self.font_table.verticalHeader().setDefaultSectionSize(INSTRUMENT_TABLE.row_height)
        self.font_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.font_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.font_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.font_table.setDragEnabled(True)
        self.font_table.setAcceptDrops(True)
        self.font_table.viewport().setAcceptDrops(True)
        # A single explicit insertion line covers row bodies, header and
        # bottom whitespace without Qt's misleading OnItem highlight.
        self.font_table.setDropIndicatorShown(False)
        self.font_table.setDragDropMode(QAbstractItemView.InternalMove)
        self.font_table.setDragDropOverwriteMode(False)
        self.font_table.setDefaultDropAction(Qt.MoveAction)
        self.font_table.setToolTip(tr("group.font_order_hint"))
        self.font_table.setHorizontalScrollMode(QTableWidget.ScrollPerPixel)
        self.font_table.setVerticalScrollMode(QTableWidget.ScrollPerItem)
        self.font_table.setMinimumHeight(0)
        self.font_table.horizontalScrollBar().rangeChanged.connect(
            lambda *_: self._update_font_list_height()
        )
        self.font_table.itemSelectionChanged.connect(self.select_font)
        self.font_table.move_requested.connect(self.relocate_font)
        self.font_table.remove_requested.connect(self.remove_font)
        card_layout.addWidget(self.font_table)

        self.font_volume = NativeControlRow(
            tr("group.selected_font_volume"), 0, 400, 100,
            self.change_font_volume, lambda value: f"{int(value)} %",
            input_steps=(1, 10, 50),
        )
        card_layout.addWidget(self.font_volume)
        layout.addWidget(card)

        routing_card = NativeCard()
        routing_layout = set_layout(QVBoxLayout(routing_card), METRICS["card_padding"], CARD_ROW_GAPS["group"])
        routing_heading = QLabel(tr("group.effective_routing"))
        routing_heading.setObjectName("CardTitle")
        routing_heading.setToolTip(tr("group.effective_routing_tooltip"))
        routing_layout.addWidget(routing_heading)
        self._routing_table_spec = InstrumentTableSpec(
            headers=("", tr("group.bank_header"), tr("group.program_header")),
            trailing_column=2,
        )
        self.routing_table = NativeInstrumentTable(0, 3)
        self.routing_table.clickable_column_start = 3
        configure_instrument_table(self.routing_table,lambda:self.owner._dark,spec=self._routing_table_spec)
        routing_header = self.routing_table.horizontalHeader()
        routing_header.setStretchLastSection(True)
        self.routing_table.cellClicked.connect(self.select_routing_source)
        self.routing_table.cellDoubleClicked.connect(self.open_routing_in_settings)
        routing_layout.addWidget(self.routing_table, 1)
        self.lsb_mapping_section,lsb_body=make_control_section(tr('mapping.section'),tr('mapping.section_help'),collapsible=True)
        self.lsb_mapping_section.disclosure_button.setChecked(bool(self.group.mappings))
        actions=control_row_layout()
        self.lsb_mapping_add=make_action_button(tr('mapping.add'),lambda:self._edit_lsb_mapping())
        self.lsb_mapping_edit=make_action_button(tr('mapping.edit'),lambda:self._edit_lsb_mapping(True))
        self.lsb_mapping_remove=make_action_button(tr('common.delete'),self._remove_lsb_mapping)
        for button in (self.lsb_mapping_add,self.lsb_mapping_edit,self.lsb_mapping_remove):actions.addWidget(button)
        actions.addStretch(1);lsb_body.addLayout(actions,0,0)
        self.lsb_mapping_table=NativeInstrumentTable(0,5)
        self._lsb_mapping_spec=InstrumentTableSpec(headers=(tr('group.bank_header'),tr('channel_editor.bank_lsb'),
            tr('group.program_header'),tr('group.preset_header'),tr('group.font_header')),activity_column=-1,trailing_column=4)
        configure_instrument_table(self.lsb_mapping_table,lambda:self.owner._dark,spec=self._lsb_mapping_spec)
        self.lsb_mapping_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.lsb_mapping_table.setSelectionMode(QTableWidget.SingleSelection)
        self.lsb_mapping_table.setMinimumHeight(0)
        self.lsb_mapping_table.setMaximumHeight(self._lsb_mapping_spec.row_height*5+40)
        self.lsb_mapping_table.itemSelectionChanged.connect(self._lsb_mapping_actions)
        self.lsb_mapping_table.cellDoubleClicked.connect(lambda *_:self._edit_lsb_mapping(True))
        self.lsb_mapping_table.horizontalHeader().setStretchLastSection(True)
        lsb_body.addWidget(self.lsb_mapping_table,1,0)
        # Bind after population: the shared arrow starts checked,so setting
        # checked=True alone does not emit toggled or reveal its new body.
        self.lsb_mapping_section.disclosure.set_expanded(bool(self.group.mappings),refresh_extent=True)
        routing_layout.addWidget(self.lsb_mapping_section)
        self.routing_card = routing_card
        layout.addWidget(routing_card, 1)
        return page

    def _settings_page(self):
        page = QWidget()
        layout = bordered_page_layout(QHBoxLayout(page), METRICS["section_gap"])
        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(METRICS["section_gap"])
        layout.addWidget(split)

        left = NativeCard()
        left_layout = set_layout(QVBoxLayout(left), METRICS["card_padding"], CARD_ROW_GAPS["group"])
        list_header = control_row_layout()
        list_title = QLabel(tr("group.instrument_list"))
        list_title.setObjectName("CardTitle")
        list_header.addWidget(list_title)
        list_header.addStretch(1)
        self.instrument_summary = QLabel(tr("group.instrument_loading"))
        self.instrument_summary.setObjectName("MutedLabel")
        list_header.addWidget(self.instrument_summary)
        self.select_all_instruments = make_action_button(tr("group.select_all"), self._select_all_instruments)
        list_header.addWidget(self.select_all_instruments)
        left_layout.addLayout(list_header)

        filter_bar = QFrame()
        filter_bar.setObjectName("NativeFilterBar")
        filters = set_layout(QHBoxLayout(filter_bar), (3, 3, 3, 3), METRICS["compact_content_gap"])
        self.filter_group = QButtonGroup(filter_bar)
        self.filter_group.setExclusive(True)
        self.filter_all = NativeFilterButton(tr("group.filter_all"), filter_bar)
        self.filter_played = NativeFilterButton(tr("group.filter_played"), filter_bar)
        self.filter_mapped = NativeFilterButton(tr("group.filter_mapped"), filter_bar)
        for control in (self.filter_all, self.filter_played, self.filter_mapped):
            self.filter_group.addButton(control)
            filters.addWidget(control)
        self.filter_all.setChecked(True)
        for control in (self.filter_all, self.filter_played, self.filter_mapped):
            control.toggled.connect(self.populate_instruments)
        filters.addStretch(1)
        self.clear_played_button = make_action_button(
            tr("group.clear_played_history"), self.clear_played_history,
        )
        self.clear_played_button.setToolTip(tr("group.clear_played_history_tooltip"))
        filters.addWidget(self.clear_played_button)
        left_layout.addWidget(filter_bar)

        self.instrument_table = NativeInstrumentTable(0, len(INSTRUMENT_TABLE.headers))
        configure_instrument_table(self.instrument_table,lambda:self.owner._dark,
                                   selection=QAbstractItemView.ExtendedSelection)
        header = self.instrument_table.horizontalHeader()
        header.moveSection(header.visualIndex(INSTRUMENT_TABLE.trailing_column), len(INSTRUMENT_TABLE.headers) - 1)
        # The final Memo column owns residual viewport space.  Its natural
        # content fit remains its baseline; users resize it through its left
        # boundary while the right edge stays the table boundary.
        header.setStretchLastSection(True)
        self.instrument_table.setColumnWidth(INSTRUMENT_TABLE.activity_column, INSTRUMENT_TABLE.activity_width)
        header.setSectionResizeMode(INSTRUMENT_TABLE.activity_column, QHeaderView.Fixed)
        # The activity cell is intentionally fixed: it is an untitled visual
        # indicator. Every named data column is user-resizable from its
        # practical initial width rather than being locked as Stretch/contents.
        for column in INSTRUMENT_TABLE.named_columns:
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            self.instrument_table.setColumnWidth(
                column, INSTRUMENT_TABLE.fitted_width(INSTRUMENT_TABLE.headers[column])
            )
        header.sectionResized.connect(self._on_instrument_column_resized)
        self.instrument_table.itemSelectionChanged.connect(self.select_preset)
        self.instrument_table.cellClicked.connect(self.edit_instrument_cell)
        self.instrument_table.edit_requested.connect(self.edit_instrument_cell)
        self.instrument_table.itemChanged.connect(self.commit_instrument_cell)
        left_layout.addWidget(self.instrument_table, 1)
        split.addWidget(left)

        inspector = NativeCard(inset_role='inspector-shell')
        self.preset_inspector = inspector
        self._preset_window_motion = NativeWindowSizeMotion(self)
        inspector.setMinimumWidth(0)
        inspector.setMaximumWidth(METRICS["group_inspector_max_width"])
        inspector.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)
        # Keep rectangular scroll children inside the8px rounded card.
        outer=set_layout(QVBoxLayout(inspector),8,0)
        self._preset_inspector_scroll=QScrollArea()
        self._preset_inspector_scroll.setFrameShape(QFrame.NoFrame)
        self._preset_inspector_scroll.setWidgetResizable(True)
        self._preset_inspector_scroll.setMinimumSize(0,0)
        self._preset_inspector_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._preset_inspector_scroll.setStyleSheet(transparent_scroll_area_stylesheet())
        self._preset_inspector_scroll.installEventFilter(self)
        outer.addWidget(self._preset_inspector_scroll)
        self._preset_inspector_content=WidthMeasuredPanel()
        self._preset_inspector_content.setMinimumSize(0,0)
        il = set_layout(QVBoxLayout(self._preset_inspector_content), max(0,METRICS["card_padding"]-8), CARD_ROW_GAPS["group"])
        il.setSizeConstraint(QLayout.SetNoConstraint)
        il.setAlignment(Qt.AlignTop)
        set_transparent_scroll_content(self._preset_inspector_scroll,self._preset_inspector_content)
        inspector_header = control_row_layout()
        heading = QLabel(tr("group.instrument_defaults")); heading.setObjectName("CardTitle")
        inspector_header.addWidget(heading)
        inspector_header.addStretch(1)
        il.addLayout(inspector_header)
        self.preset_title = QLabel('')
        self.preset_title.setObjectName("PresetIdentity")
        self.preset_title.setWordWrap(True)
        identity_row=QWidget();identity_layout=set_layout(QHBoxLayout(identity_row),0,4)
        identity_layout.addWidget(self.preset_title,1)
        self.instrument_audition_button=make_instrument_audition(self.owner,self._instrument_phrase_request,identity_row)
        self.instrument_audition_button.setEnabled(False);identity_layout.addWidget(self.instrument_audition_button)
        il.addWidget(identity_row)
        self.preset_font = AnimatedComboBox()
        self.preset_font.setToolTip(tr("group.instrument_font_tooltip"))
        self.preset_font.currentIndexChanged.connect(lambda *_: self._edit_selected_presets("index", self.preset_font.currentData(), mapping=True))
        inspector_label_width = METRICS["inspector_label_width"]
        inspector_value_width = METRICS["inspector_value_width"]
        inspector_gap = METRICS["inspector_column_gap"]
        font_row = QWidget()
        install_three_column_form_row(
            font_row, tr("group.instrument_font"), self.preset_font,
            ElasticPanel(preferred_width=inspector_value_width),
            label_width=inspector_label_width, gap=inspector_gap,
        )
        il.addWidget(font_row)
        self.preset_font_cc=QLabel('')
        self.preset_font_cc.setObjectName('MutedLabel');self.preset_font_cc.setWordWrap(True)
        self.preset_font_cc.setMinimumWidth(0);self.preset_font_cc.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        self.preset_font_cc.setAccessibleName(tr('font_cc.title'))
        self.preset_cc_section,_cc_grid=make_control_section(tr('font_cc.details'),collapsible=True)
        self.preset_cc_toggle=self.preset_cc_section.disclosure_button
        self.preset_cc_toggle.setChecked(False);self.preset_cc_toggle.setEnabled(False)
        self.preset_cc_disclosure=self.preset_cc_section.disclosure
        self.preset_cc_detail=QPlainTextEdit();self.preset_cc_detail.setReadOnly(True)
        self.preset_cc_detail.setMinimumWidth(0);self.preset_cc_detail.setAccessibleName(tr('font_cc.title'))
        self.preset_cc_detail.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        self.preset_cc_detail.setFixedHeight(self.preset_cc_detail.fontMetrics().lineSpacing()*8+12)
        self.preset_cc_disclosure.content_layout.addWidget(self.preset_cc_detail)
        self.preset_cc_disclosure.heightChanged.connect(self._sync_inspector_extent)
        font_controller_index(self.owner).changed.connect(self._refresh_font_cc_inspector)
        self.preset_pages = make_parameter_tabs(current_page_height=True)
        self.preset_pages.setObjectName("InstrumentParameterTabs")
        policy=self.preset_pages.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Ignored);policy.setVerticalPolicy(QSizePolicy.Maximum)
        self.preset_pages.setSizePolicy(policy)
        self.preset_page_choice = self.preset_pages.tabBar()
        il.addWidget(self.preset_pages)
        tone = QWidget(); tone_layout = set_layout(QVBoxLayout(tone), 0, 6)
        tone_layout.setAlignment(Qt.AlignTop)
        self.preset_controls = {}
        self.performance_model=GroupPerformanceEditor(self)
        self.preset_extension_controls, self.preset_extension_switches = {}, {}
        self._preset_extension_sections={}
        for section,members in (('tone',('volume','cutoff','resonance')),('envelope',('attack','decay','release'))):
            caption=tr('group.sound_section_tone') if section=='tone' else tr('channel_editor.group_envelope')
            container,grid=make_control_section(caption)
            tone_layout.addWidget(container)
            for column,key in enumerate(members):
                is_volume=key=='volume'
                detail=tr('group.volume_tooltip') if is_volume else self._preset_default_tooltip(tr('group.'+key+'_tooltip'))
                control=NativeKnobField(tr('group.instrument_volume') if is_volume else tr('group.'+key),
                    0 if is_volume else -64,INSTRUMENT_VOLUME_MAX if is_volume else 63,100 if is_volume else 0,
                    lambda value,key=key:self._edit_selected_presets(key,int(round(value))+(0 if key=='volume' else 64)),
                lambda value,is_volume=is_volume:format_effect_value(value if is_volume else value+64,'percent' if is_volume else 'midi_offset'),
                    tooltip=detail,editable=True,edit_context=self._selected_preset_keys)
                grid.addWidget(control,0,column);grid.setColumnStretch(column,1)
                self.preset_controls[key]=control
        for section in ('tuning','vibrato'):
            tone_layout.addWidget(self._preset_extension_section(section))
        self._add_preset_page(tone,tr('group.sound_page'))
        response=QWidget();response_layout=set_layout(QVBoxLayout(response),0,6);response_layout.setAlignment(Qt.AlignTop)
        for section in ('modulation','channel_pressure','key_pressure'):
            response_layout.addWidget(self._preset_extension_section(section))
        self._add_preset_page(response,tr('group.response_page'))
        self._add_preset_page(self._build_instrument_tuning(),tr('group.tuning_page'))
        il.addWidget(self.preset_font_cc);il.addWidget(self.preset_cc_section)

        self.preset_memo = QLineEdit()
        self.preset_memo.setToolTip(tr("group.memo_tooltip"))
        self.preset_memo.editingFinished.connect(self._edit_selected_memo)
        memo_row = QWidget()
        install_three_column_form_row(
            memo_row, tr("common.memo"), self.preset_memo,
            ElasticPanel(preferred_width=inspector_value_width),
            label_width=inspector_label_width, gap=inspector_gap,
        )
        il.insertWidget(il.indexOf(self.preset_pages), memo_row)

        self.preset_enabled = NativeToggleSwitch(tr("group.settings_enabled"))
        self.preset_enabled.setToolTip(tooltip_display_text(tr("group.settings_enabled_tooltip")))
        self.preset_enabled.toggled.connect(lambda checked: self._edit_selected_presets("disabled", not checked, mapping=True))
        il.insertWidget(il.indexOf(self.preset_pages), self.preset_enabled)
        self.preset_pages.currentChanged.connect(self._sync_inspector_extent)
        self._sync_inspector_extent()
        split.addWidget(inspector)
        split.setStretchFactor(0, 7)
        split.setStretchFactor(1, 3)
        split.setSizes((720, 360))
        return page

    def select_font(self):
        """Adapt the inherited font-selection action to a native control row."""
        index = self.current_font_row()
        valid = 0 <= index < len(self.group.fonts)
        value = self.group.fonts[index].volume if valid else 100
        self.font_volume.set(value, call=False)
        self.font_volume.setEnabled(valid)
        self.font_actions["up"].setEnabled(valid and index > 0)
        self.font_actions["down"].setEnabled(valid and index < len(self.group.fonts) - 1)
        self.font_actions["delete"].setEnabled(valid and (len(self.group.fonts)>1 or self.group.fonts[index].custom is not None))

    def _instrument_catalog_key(self):
        """Stable font-source fingerprint for one enumerated instrument set."""
        entries = []
        for entry in self.group.fonts:
            path = os.path.normcase(os.path.abspath(str(self.group.font_path(entry))))
            try:
                stat = os.stat(path)
                revision = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                revision = None
            entries.append((path, revision))
        return tuple(entries)

    def _on_instrument_column_resized(self, column, _old_size, _new_size):
        """Remember an explicit user choice without mistaking auto-fit for one."""
        if (
            not self._instrument_auto_fitting
            and INSTRUMENT_TABLE.is_named(column)
            and column != INSTRUMENT_TABLE.trailing_column
        ):
            self._instrument_user_columns.add(column)

    def _instrument_column_target_width(self, column):
        """Longest displayed header/cell plus shared padding and tiny safety room."""
        return self._instrument_content_widths[column]

    def _fit_instrument_columns(self):
        """Fit unmodified named columns; preserve manual widths across filters."""
        self._instrument_auto_fitting = True
        try:
            for column in INSTRUMENT_TABLE.named_columns:
                if column not in self._instrument_user_columns and not self.instrument_table.isColumnHidden(column):
                    self.instrument_table.setColumnWidth(column, self._instrument_column_target_width(column))
        finally:
            self._instrument_auto_fitting = False

    def populate_instruments(self):
        """Use the inherited state mapping, then apply native table semantics."""
        self._finish_instrument_edit(True)
        self._populate_instrument_rows()
        self.clear_played_button.setVisible(self.filter_played.isChecked())
        self.clear_played_button.setEnabled(bool(ae.get_soundfont_group_played_state()))
        self._fit_instrument_columns()
        self.select_preset()

    def refresh_instruments(self, force=False):
        """Enumerate once per font-source revision; filters use the cache."""
        key = self._instrument_catalog_key()
        if key != self._active_instrument_catalog_key:
            # A different font collection has different longest values; any
            # prior manual widths belong to the former collection.
            self._instrument_user_columns.clear()
        if force or key not in self._instrument_catalog_cache:
            self._instrument_catalog_cache[key] = self.owner.enumerate_instruments(self.group)
        self._active_instrument_catalog_key = key
        catalog = self._instrument_catalog_cache[key]
        self._instrument_catalog_by_key = {}
        for record in catalog:
            self._instrument_catalog_by_key.setdefault((record["bank"], record["preset"]), []).append(record)
        self._instrument_rows = resolve_effective_instruments(self.group, catalog)
        self._instrument_row_by_key = {
            (record["bank"], record["preset"]): index
            for index, record in enumerate(self._instrument_rows)
        }
        self.export_button.setEnabled(bool(self._instrument_rows))
        self.export_button.setToolTip(tr('mapping.export_scope_tooltip',description=tr('group.export_instruments_tooltip'))
            if self.group.mappings else tr('group.export_instruments_tooltip'))
        self.instrument_summary.setText(
            tr("group.instrument_summary", count=len(self._instrument_rows))
            if self._instrument_rows else tr("group.no_instruments")
        )
        self.populate_instruments()
        self._populate_routing_rows()
        self._refresh_lsb_mappings()
        if self.instrument_table.currentRow() >= 0:
            self.select_preset()

    def refresh_theme(self):
        apply_native_text_metrics(self)
        palette = widget_palette(self.owner)
        for checkbox in self.findChildren(OutlineCheckBox):
            checkbox.set_theme(palette)
        # The generic font normalizer intentionally restores 14px body text,
        # but this dense navigation table has its own reviewed 12px contract.
        for table, spec in ((self.instrument_table, INSTRUMENT_TABLE),
                            (self.routing_table, self._routing_table_spec),
                            (self.lsb_mapping_table,self._lsb_mapping_spec)):
            table.setFont(spec.font())
            table.horizontalHeader().setFont(spec.font())

    def showEvent(self, event):
        QDialog.showEvent(self, event)
        self.refresh_theme()
        self._update_font_list_height()
        self.poll.start()
        self.refresh_activity()
        if not getattr(self, "_opening_rows_fitted", False):
            self._opening_rows_fitted = True
            self.group_tabs.setCurrentIndex(0)
            QTimer.singleShot(0, self._fit_opening_routing_rows)

    def _fit_opening_routing_rows(self):
        # Actual viewport geometry includes header, frame and scrollbar chrome.
        # This is a preferred opening height only; manual resizing stays free.
        if not automatic_window_fit_allowed(self):return
        self.layout().activate()
        required = sum(self.routing_table.rowHeight(row) if row < self.routing_table.rowCount()
                       else self._routing_table_spec.row_height for row in range(16))
        target = self.height() + required - self.routing_table.viewport().height()
        if self.screen() is not None and QApplication.platformName() != "offscreen":
            target = min(target, self.screen().availableGeometry().height() - (self.frameGeometry().height() - self.height()))
        self.resize(self.width(), max(1, target))

    def hideEvent(self, event):
        self._preset_window_motion.settle()
        if self.instrument_creator is not None:
            self.instrument_creator.stop_preview()
        self.poll.stop()
        QDialog.hideEvent(self, event)
