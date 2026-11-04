"""Sole live PySide6 window for ABMSynth.

The window inherits the BASS/MIDI controller in `live_controller.py`
and owns the running application's widgets.
"""

from __future__ import annotations

import math
import os
import signal
import sys
import threading
import time
import weakref
from dataclasses import asdict
from html import escape
from pathlib import Path
import numpy as np
import shiboken6

# Direct execution should be as quiet as app.py's child-process launch.
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.window=false")
# This must be set before QApplication exists. Qt 6.8 defaults to DirectWrite
# on Windows, but GDI is the verified legible renderer for ABMSynth's
# compact Korean Widgets UI on the target display.
os.environ.setdefault("QT_QPA_PLATFORM", "windows:fontengine=gdi")

from PySide6.QtCore import QEvent, QEasingCurve, QObject, QPropertyAnimation, QPointF, QRect, QRectF, QSize, QTimer, Qt, Signal, Slot, QStandardPaths
from PySide6.QtGui import QAction, QColor, QFont, QFontDatabase, QFontMetrics, QFontMetricsF, QIcon, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QAbstractSpinBox, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFrame, QGraphicsOpacityEffect, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLayout, QLineEdit, QListView, QMainWindow, QMessageBox, QPushButton, QRadioButton,
    QPlainTextEdit, QScrollArea, QScrollBar, QSizePolicy, QSlider, QSpinBox, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QTabWidget, QTableWidget, QTableWidgetItem, QToolTip, QSystemTrayIcon, QMenu,
    QStyleOptionButton, QStyleOptionComboBox, QStyleOptionTabWidgetFrame, QToolButton, QVBoxLayout, QWidget,
)
import audio_engine as ae
from bass_bindings import (
    MIDI_EVENT_CHORUS_MACRO, MIDI_EVENT_REVERB_MACRO, REVERB_PRESETS,
    bass, bassmidi,
)
from soundfont_files import (
    PresetOverride, SoundFontEntry, SoundFontGroup, SoundFontLibrary,
    load_sfg, save_sfg, soundfont_path_key, validate_group,
    SUPPORTED_SOUNDFONT_LOAD_SUFFIXES, soundfont_load_filter,
    BUNDLED_SOUNDFONT, bundled_soundfont_path, resolve_soundfont_selection_path,
    stored_soundfont_selection_path, soundfont_selection_exists, startup_soundfont_candidates,
)
import audio_output
from windows_integration import (
    open_sound_playback_devices,
    spawn_restart_after_exit,
    set_enabled as set_windows_startup_enabled,
)
from audio_output import AudioSession, AudioDeviceCatalog, AudioTelemetry, OutputDevice
from effect_control import (EffectEvents, EffectRowSpec, EFFECT_ROWS, EFFECT_ROW_BY_CONTROL,
    EFFECT_CAPTURE_CONTROLS, delay_state_from_controls, extended_delay_state_from_controls)
from native_layout import (
    CARD_ROW_GAPS, FORM_ROW_HEIGHT,
    ElasticLabel,
    ElasticPage,
    ElasticPanel,
    StackItem,
    TopClippedCanvas,
    install_three_column_form_row,
    make_horizontally_elastic,
    place_top_anchored_stack,
    set_layout, bordered_page_layout, automatic_window_fit_allowed,
)
from effect_presentation import (
    BMSYNTH_CHORUS_PRESETS, EQ_CENTER_INPUT_STEPS, delay_type_labels, effect_tooltip,
    format_effect_value,
)
from soundfont_runtime import SoundFontLoadCancelled
from midi_input_runtime import prepare_live_native_session
from delay_runtime import BMSYNTH_DELAY_PRESETS
from settings import (
    bmsynth_global_position_to_midi_send, bmsynth_stereo_position_to_multiplier,
    read_bmsynth_ini,
)
from settings import (
    EnginePreferences, SAMPLE_RATES, DEFAULT_MASTER_VOLUME, engine_ini_values,
    import_bmsynth_engine_values, load_soundfont_library, load_ui_values,
    load_midi_cc_values, save_midi_cc_values,
    load_channel_policy_values, save_channel_policy_values,
    save_engine_preferences, save_soundfont_library, save_ui_values, settings_path, update_sections, IniWriteCoordinator,
)
from effect_control import (
    CHORUS_EVENT_BY_CONTROL, REVERB_EVENT_BY_CONTROL,
    BMSYNTH_REVERB_DEFAULT, BMSYNTH_CHORUS_DEFAULT,
    ChorusState, CompressorState, DelayState, ExtendedDelayState, EffectState, LimiterState, ReverbState, SpatialParameters,
    EQBand, EQ_DEFAULT_BANDS, EQ_FILTER_KINDS, EQ_MAX_BANDS, EQ_PRESETS, EqualizerState,
    audio_fx_ini,SYSTEM_RESET_RECEIVERS,EFFECT_FACTORY_VALUES,
)
from event_log import log_text
from i18n import tr, active_locale
from product import ENV_UI_ONLY, MIDI_PORT_NAME, PRODUCT_NAME, PRODUCT_VERSION
from soundfont_files import DEFAULT_SCAN_BUDGET, ScanResult, scan_soundfonts


from native_widgets import (
    request_visual_frame, remove_visual_frame, compositor_visual_clock,
    DARK, LIGHT, DEFAULT_LAYOUT_DENSITY, EFFECT_TAB_FONT_PX, INSTRUMENT_TABLE, LAYOUT_DENSITIES,
    METRICS, MIN_UI_FONT_PX, NOTO_VARIABLE_FONT_PATH, PROJECT_NOTO_STATIC_FONTS,
    SOUNDFONT_DISCOVERY_LIMIT, SOUNDFONT_PICKER_SENTINEL, STATUS_LOG_FONT_PX,
    TOOLTIP_WRAP_WIDTH_PX, UI_BOLD_WEIGHT, UI_REGULAR_WEIGHT,
    AnimatedComboBox, DeviceComboBox, ElasticLabel, ElasticPanel,
    InstrumentTableSpec, NativeActionButton, NativeSettingsButton, NativeAudioVisualizer, NativeMessageBox, NativeMenu,
    NativeCard, NativeChannelStrip, NativeChannelCommandPresentation, NativeSnapshotCommandPresentation, NativeControlRow, NativeDisclosure, NativeEQResponse, NativeHoverReconciler, NativeKnob, NativeKnobField, NativeLoadBar, NativeMeter, NativeMidiActivityBar, NativeMonitorTransitionFrame, NativeNumberBox, NativePointerFocusDismissal, NativeRotatedLabel, NativeWindowSizeMotion,
    NativeSegmentedChoice, NativeSlider, NativeToggleSwitch, NativeWindowInteractionFilter, EditableNumberLabel, OutlineCheckBox, SoundFontComboBox,
    SoundFontDiscoverySignals,
    SOUNDFONT_ROW_CURRENT, SOUNDFONT_ROW_FILE,
    SOUNDFONT_ROW_KIND_ROLE, SOUNDFONT_ROW_PICKER, SOUNDFONT_ROW_UNSAVED,
    apply_native_text_metrics, apply_native_application_palette, format_value,
    install_effect_tooltip, install_native_translations, make_action_button, make_tuning_audition, make_effect_audition, make_parameter_tabs, native_text_font, NativeEffectTabBar,
    COLOR_THEMES, color_palette, widget_palette, disabled_control_color,
    project_noto_family, tooltip_display_text, set_button_primary,
    native_stylesheet, native_tooltip_stylesheet, apply_native_tooltip_theme,
)
from midi_input_runtime import (tuning_hz, tuning_cents, EXPRESSION_EVENTS, CHANNEL_PARAMETERS,
    CHANNEL_FUNCTION_TARGETS, CHANNEL_MIX_RANGES, CHANNEL_MODE_SWITCHES, AUXILIARY_CC)
from native_dialogs import (
    NativeAudioFXPanel,
    NativeActivityCellDelegate, NativeEnginePreferencesDialog, NativeRecordingSaveWorker, NativeInstrumentHeader, NativeMidiPlayerDialog, NativeRecordingDialog, NativeLicensesDialog,
    NativeInstrumentRowDelegate, NativeInstrumentTable, NativeLiveSoundFontGroupDialog,
    NativeSoundFontLoadDialog, NativeSoundFontLoadWorker, NativePerformanceDialog, choose_group_save_path,
)


# The Native window composes its widgets here and inherits only active
# controller and SoundFont-dialog actions from the separate live module.
import live_controller


APP_ICON_PATH = Path(__file__).resolve().parent / "assets" / "abmsynth.ico"


SPATIAL_DETAIL_NAMES = ("spatial_input_gain", "spatial_time", "spatial_hf_ratio")

# BMSynth cbChorusChange reads the macro back in this exact displayed order.
# The same row specs own the UI event mapping and the preset readback order.
REVERB_PRESET_EVENTS = tuple((row.attr, row.event) for row in EFFECT_ROWS["reverb"])
CHORUS_PRESET_EVENTS = tuple((row.attr, row.event) for row in EFFECT_ROWS["chorus"])


class NativeSoundFontFileDropFilter(QObject):
    """Main-window file MIME only; never duplicates the visual frame filter."""

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.DragEnter,QEvent.DragMove,QEvent.DragLeave,QEvent.Drop):
            return self.parent()._filter_soundfont_file_drop(watched,event)
        return False


class NativeLiveWindow(QMainWindow, live_controller.LiveController):
    midi_cc_changed = Signal()
    system_reset_receive_changed = Signal(str,bool)
    _PERSISTED_SLIDERS = tuple(
        row.attr for group, rows in EFFECT_ROWS.items() if group not in {"equalizer", "performance"} for row in rows
    )
    # Boolean presentation may be positive while the established domain/INI
    # identity is negative. One boundary declaration owns that polarity.
    _PERSISTED_FLAGS = tuple((name, name, False) for name in (
        "overnote", "delay_sysex_enabled", "compressor_enabled", "limiter_enabled",
        "extra_delay_details_expanded", "extra_delay_ping_pong", "spatial_details_expanded",
        "soundfont_favorites_only",
        "midi_monitor_enabled", "humanize_preserve_simultaneity",
    ))
    _PERSISTED_COMBOS = ("reverb_preset", "chorus_preset", "delay_preset", "eq_preset")
    _CUSTOM_PRESET_GROUPS = {
        "reverb_preset": "reverb", "chorus_preset": "chorus",
        "delay_preset": "delay", "eq_preset": "equalizer",
    }

    def show(self):
        from native_widgets import invoke_native_visibility
        invoke_native_visibility(self,'show')

    def showNormal(self):
        from native_widgets import invoke_native_visibility
        invoke_native_visibility(self,'showNormal')

    def showMinimized(self):
        from native_widgets import invoke_native_visibility
        invoke_native_visibility(self,'showMinimized')

    def hide(self):
        from native_widgets import invoke_native_visibility
        invoke_native_visibility(self,'hide')

    def __init__(self, *, audio_session=None, effect_events=None, device_catalog=None, audio_telemetry=None,native_session_builder=None):
        self._qt_initialized=False
        self._owns_midi_notifications=False
        try:
            self._initialize_window(audio_session=audio_session,effect_events=effect_events,
                                    device_catalog=device_catalog,audio_telemetry=audio_telemetry,
                                    native_session_builder=native_session_builder)
        except BaseException as error:
            try: self._abort_initialization()
            except Exception as cleanup_error:
                error.add_note(tr("runtime.startup_cleanup_failed",error=cleanup_error))
            raise

    def _abort_initialization(self):
        """Partial construction owns workers, but must not save a partial form."""
        self._shutdown_started=True
        if self._qt_initialized:
            for timer in self.findChildren(QTimer): timer.stop()
            application=QApplication.instance()
            for name in ("_interaction_filter",):
                event_filter=getattr(self,name,None)
                if event_filter is not None: application.removeEventFilter(event_filter)
            visualizer=getattr(self,"audio_visualizer",None)
            if visualizer is not None: visualizer.close_analysis()
            tray=getattr(self,"_tray_icon",None)
            if tray is not None: tray.hide()
        for name in ("_font_load_worker","_recording_save_worker"):
            worker=getattr(self,name,None)
            if worker is not None and worker.isRunning():
                cancel=getattr(worker,"cancel_requested",None)
                if cancel is not None: cancel.set()
                worker.wait()
        midi=getattr(self,"midi_input_runtime",None)
        if midi is not None: midi.stop()
        if self._owns_midi_notifications:
            ae.runtime.midi_learn.on_change=None
            ae.runtime.channel_policy.on_change=None
            self._owns_midi_notifications=False
        telemetry=getattr(self,"audio_telemetry",None)
        if telemetry is not None: telemetry.pause_observation()
        writer=getattr(self,"_settings_writer",None)
        if writer is not None: writer.close(discard_failed=True)

    def _initialize_window(self, *, audio_session=None, effect_events=None, device_catalog=None, audio_telemetry=None,native_session_builder=None):
        from settings import SettingsReadReport
        self._settings_read_report = SettingsReadReport()
        self._font_preload_lock = threading.RLock()
        self._font_load_active = False
        self._font_load_worker = None
        self._font_preloaded_used: set[tuple[int, int, int]] = set()
        self._ui_settings_loading = True
        self._system_reset_receivers={field:True for field,*_ in SYSTEM_RESET_RECEIVERS}
        self._system_reset_persistence_baseline=None
        try:
            self._stored_ui_values = load_ui_values()
        except ValueError:
            self._stored_ui_values = {}
        # The visible selector can follow a live, unsaved group while this
        # path remains the one that the next launch should restore.
        self._persisted_soundfont_path = self._stored_ui_values.get("soundfont", "")
        self._startup_soundfont_requested = self._persisted_soundfont_path
        self._startup_soundfont_pending = False
        self._initial_devices_waiting_for_soundfonts = False
        self._unsaved_group_active = False
        try:
            self._soundfont_library = load_soundfont_library(report=self._settings_read_report)
        except ValueError:
            self._soundfont_library = SoundFontLibrary()
        self._preferred_output_device = self._stored_ui_values.get("output_device")
        self._preferred_output_endpoint_id = self._stored_ui_values.get("output_endpoint_id")
        self._skip_wasapi_warning = self._stored_ui_values.get("skip_wasapi_warning") == "1"
        self._skip_driver_setup_notice = self._stored_ui_values.get('skip_driver_setup_notice')=='1'
        self._driver_setup_notice=None
        self._last_confirmed_output_index = -1
        self._layout_density = DEFAULT_LAYOUT_DENSITY
        self._compact_mode = self._layout_density == "compact"
        self._applied_theme_key = None
        self._visual_window_handle = None
        self._visual_screen = None
        self._visual_refresh_hz = 60.0  # fallback until a QScreen is attached
        self._visual_attach_queued = False
        self._first_show_complete = False
        self._window_placement_ready = False
        self._channel_controls_dirty = False
        self._performance_dialog = None
        self._unopened_channel_sections = NativePerformanceDialog.section_ui_defaults()
        self._preset_programmatic = False
        self._custom_presets = {}
        self._custom_restoring = False
        self._soundfont_discovered_paths: tuple[Path, ...] = ()
        self._soundfont_scan_started = False
        self._soundfont_scan_result: ScanResult | None = None
        # Shutdown has to stop display-paced repaint requests before Windows
        # destroys the native window. Otherwise a queued UpdateRequest can
        # attempt to flush a QBackingStore whose HWND is already gone.
        self._shutdown_started = False
        self._settings_writer = IniWriteCoordinator(asynchronous=not bool(os.environ.get(ENV_UI_ONLY)),
            on_error=lambda error: ae.append_log(log_text("runtime.settings_save_failed", error=error)))
        # The first SoundFont load can open a modal progress dialog. Defer
        # device initialization until this parent window has actually painted.
        self._startup_devices_pending = not bool(os.environ.get(ENV_UI_ONLY))
        self._restart_requested = False
        self._last_trace_effect_state = None
        self._last_channel_reset_revision = ae.runtime.channel_reset_revision
        # The inherited controller invokes the overridden build_ui() before
        # it establishes its own theme field.
        self._dark = True
        self._color_theme = "blue"
        QMainWindow.__init__(self)
        from native_layout import NativeWindowPlacement
        self._window_placement = NativeWindowPlacement(self._stored_ui_values.get(NativeWindowPlacement.KEY, ''))
        self._qt_initialized=True
        install_native_translations(QApplication.instance())
        self._monitor_window_motion = NativeWindowSizeMotion(self)
        self._monitor_window_motion.finished.connect(self._finish_monitor_transition)
        self._effect_window_motion = NativeWindowSizeMotion(self)
        self._effect_fit_pending = False
        self._monitor_transitioning = False
        self.setWindowIcon(QIcon(str(APP_ICON_PATH)))
        self.audio_telemetry = audio_telemetry or AudioTelemetry(bass, ae)
        live_controller.LiveController.initialize_controller(
            self,
            audio_session=audio_session,
            effect_events=effect_events,
            device_catalog=device_catalog,
            native_session_builder=native_session_builder,
        )
        self._soundfont_discovery_signals = SoundFontDiscoverySignals(self)
        self._soundfont_discovery_signals.completed.connect(
            self._accept_soundfont_discovery, Qt.QueuedConnection,
        )
        self.setWindowTitle(tr("main.window_title_live"))
        self._restore_ui_values(self._stored_ui_values, startup=True)
        self._applied_equalizer_state = self._read_equalizer_state()
        self._applied_equalizer_preset = self.eq_preset.currentData()
        self._applied_extended_delay_state = self._read_extended_delay_state()
        self._load_custom_presets(self._stored_ui_values)
        invalid=self._settings_read_report.invalid_fields()
        if invalid: self.append_log(log_text('runtime.settings_recovered_fields',fields=', '.join(invalid)))
        self._connect_ui_persistence()
        self._effect_snapshots=NativeSnapshotCommandPresentation(self,
            lambda:(ae.runtime.identity,ae.runtime.stream_handle,ae.runtime.stream_generation),
            self._install_effect_snapshot,self._queue_ui_save)
        # INI restoration ran before the toggle signal was connected. Restore
        # the selected page as well as the check state, without saving again.
        if self.midi_monitor_enabled.isChecked():
            self.tabs.setCurrentWidget(self.channel_monitor)
        self._ui_settings_loading = False
        self.setFont(native_text_font(METRICS["body_font_px"]))
        apply_native_text_metrics(self)
        self.resize(METRICS["window_width"], METRICS["window_height"])
        # The inherited controller establishes a fixed window minimum. The
        # native surface deliberately clears it: at a small size, Qt clips
        # the grid naturally rather than creating a scrolling control pane.
        self.setMinimumSize(0, 0)
        self._window_placement.apply(self)  # Explicit origin before the HWND's first show.
        # INI theme restoration may have run before the final window font and
        # all layout sizes were established. Commit one post-build style pass.
        self._applied_theme_key = None
        self._apply_responsive_density()
        self._pointer_focus_dismissal = NativePointerFocusDismissal(self)
        self._hover_reconciler = NativeHoverReconciler(self)
        self._font_drop_pending=False
        self.setAcceptDrops(True)
        self._font_drop_filter=NativeSoundFontFileDropFilter(self)
        self._interaction_filter=NativeWindowInteractionFilter(self,self._pointer_focus_dismissal,
            self._hover_reconciler,self._font_drop_filter)
        QApplication.instance().installEventFilter(self._interaction_filter)
        try:
            ae.runtime.midi_learn.restore_ini(load_midi_cc_values(),report=self._settings_read_report)
            ae.runtime.channel_policy.restore_ini(load_channel_policy_values(),report=self._settings_read_report)
        except ValueError:
            ae.runtime.midi_learn.restore_ini({})
        # AutoConnection saves synchronously for GUI assignment/name edits,
        # but queues a MIDI-thread Learn notification onto this QObject.
        self.midi_cc_changed.connect(self._save_midi_cc_configuration)
        ae.runtime.midi_learn.on_change = self.midi_cc_changed.emit
        ae.runtime.channel_policy.on_change = self.midi_cc_changed.emit
        self._saved_midi_audio_key=(ae.runtime.midi_learn.execution_snapshot(),ae.runtime.channel_policy.revision)
        self._owns_midi_notifications=True
        self._tray_icon = None
        if self._engine_preferences.minimize_to_tray or self._engine_preferences.close_to_tray:
            self._setup_tray_icon()
        # CPU/polyphony retain the inherited 100ms cadence. Meter plus the
        # paired time/frequency visualizer use the host monitor's update
        # requests instead of an unrelated repeating GUI timer.

    @Slot()
    def _save_midi_cc_configuration(self):
        if self._shutdown_started:
            return
        try:
            if not hasattr(self,"_cc_owned_keys"):
                targets = (*CHANNEL_MIX_RANGES, *EXPRESSION_EVENTS, *CHANNEL_MODE_SWITCHES,
                           *(f"aux_{cc}" for cc in AUXILIARY_CC))
                self._cc_owned_keys = frozenset(f"ch{channel + 1:02d}.{target}.{suffix}"
                    for channel in range(128) for target in targets for suffix in ("cc","target","name","all"))
                self._policy_owned_keys = frozenset(f"{kind}_{channel}_{field}"
                    for channel in range(128) for kind in ("function","reset")
                    for field in (*CHANNEL_FUNCTION_TARGETS,"mode","bank","bank_lsb","program")) | frozenset(
                        f"pressure_key_{channel}" for channel in range(128))
            cc_keys, policy_keys = self._cc_owned_keys, self._policy_owned_keys
            self._settings_writer.submit({"MIDI CC": ae.runtime.midi_learn.ini_values(),
                "Channel Policy": ae.runtime.channel_policy.ini_values()},
                owned_keys={"MIDI CC": cc_keys, "Channel Policy": policy_keys})
            audio_key=(ae.runtime.midi_learn.execution_snapshot(),ae.runtime.channel_policy.revision)
            if audio_key!=getattr(self,'_saved_midi_audio_key',None):
                ae.ensure_channel_event_filter()
                self._saved_midi_audio_key=audio_key
        except (OSError, ValueError) as exc:
            self.append_log(log_text("channel_editor.cc_save_failed", error=str(exc)))
        if self._performance_dialog is not None:
            self._performance_dialog._refresh_midi_learn_indicators()

    def _restore_ui_values(self, values: dict[str, str], *, startup=False) -> None:
        """Restore the native interface without treating it as user input."""
        self._preset_programmatic = True
        self._unopened_channel_sections.update({key:values[key] for key in self._unopened_channel_sections
                                               if values.get(key) in ('0','1')})
        try:
            if "color_theme" in values:
                self.set_color_theme(values["color_theme"])
            if values.get("dark") in ("0", "1"):
                self.set_dark(values["dark"] == "1")
            self.audio_fx_panel.load_ini(values.get("audio_fx_chain", "[]"), values.get("audio_fx_enabled", "1") != "0",report=self._settings_read_report)
            if startup or values.get("soundfont"):
                requested = resolve_soundfont_selection_path(values.get("soundfont") or BUNDLED_SOUNDFONT)
                available = soundfont_selection_exists(requested)
                if startup:
                    self._startup_soundfont_pending = not available or Path(requested).suffix.casefold() == '.sfg'
                self._soundfont_committed_path = requested if available or values.get("soundfont") else ""
                self.soundfont_path.setText(self._soundfont_committed_path)
                self._populate_soundfont_selector(self._soundfont_committed_path)
            if "volume" in values:
                try:
                    requested=int(values["volume"])
                    self.volume.setValue(max(self.volume.minimum(),min(self.volume.maximum(),requested)))
                    self._settings_read_report.record('UI','volume','accepted' if self.volume.value()==requested else 'clamped')
                except ValueError:
                    self._settings_read_report.record('UI','volume','invalid')
                    pass
            for name in self._PERSISTED_COMBOS:
                if name in values:
                    combo = getattr(self, name)
                    index = combo.findText(values[name])
                    if index >= 0:
                        combo.setCurrentIndex(index)
                    self._settings_read_report.record('UI',name,'accepted' if index>=0 else 'invalid')
            if "delay_type" in values:
                try:
                    index = int(values["delay_type"])
                    if 0 <= index < self.delay_type.count():
                        self.delay_type.setCurrentIndex(index)
                    self._settings_read_report.record('UI','delay_type','accepted' if 0<=index<self.delay_type.count() else 'invalid')
                except ValueError:
                    self._settings_read_report.record('UI','delay_type','invalid')
                    pass
            if "spatial_sample_rate" in values:
                try:
                    rate=int(values["spatial_sample_rate"])
                    accepted=self._set_spatial_sample_rate(rate)
                    self._settings_read_report.record('UI','spatial_sample_rate','accepted' if accepted else 'normalized')
                except (ValueError,TypeError,OverflowError):
                    self._settings_read_report.record('UI','spatial_sample_rate','invalid')
            for name in self._PERSISTED_SLIDERS:
                if name in values:
                    try:
                        number,disposition = EFFECT_ROW_BY_CONTROL[name].restore_value(values[name])
                        getattr(self, name).set(number, False)
                        self._settings_read_report.record('UI',name,disposition)
                    except (ValueError, TypeError, OverflowError):
                        self._settings_read_report.record('UI',name,'invalid')
                        continue
            for name, key, inverted in self._PERSISTED_FLAGS:
                if values.get(key) in ("0", "1"):
                    getattr(self, name).setChecked((values[key] == "1") != inverted)
                    self._settings_read_report.record('UI',key,'accepted')
                elif key in values: self._settings_read_report.record('UI',key,'invalid')
            # Factory is a complete sound,not an overriding instruction for
            # saved scalars. Default and Custom both skip startup macros,so
            # correcting this identity preserves the legacy sound exactly.
            for attr,group in self._CUSTOM_PRESET_GROUPS.items():
                if group not in EFFECT_FACTORY_VALUES:continue
                combo=getattr(self,attr)
                if combo.currentText()!='Default':continue
                expected=EFFECT_FACTORY_VALUES[group]
                if any((self.delay_type.currentIndex() if key=='delay_type' else getattr(self,key).get())!=value for key,value in expected.items()):
                    combo.setCurrentText('Custom')
            for field,key,inverted,_mode in SYSTEM_RESET_RECEIVERS:
                if values.get(key) in ('0','1'):
                    self.set_system_reset_receive(field,(values[key]=='1')!=inverted)
                    self._settings_read_report.record('UI',key,'accepted')
                elif key in values:self._settings_read_report.record('UI',key,'invalid')
            try:
                restored_eq = EqualizerState.from_ini(values, self._engine_preferences.sample_rate,report=self._settings_read_report)
                self._eq_bands = list(restored_eq.bands)
                self.eq_output_trim.set(restored_eq.output_trim_db, False)
            except (ValueError, TypeError, KeyError):
                self._settings_read_report.record('UI','equalizer','invalid')
                self._eq_bands = list(EQ_DEFAULT_BANDS)
            self.on_equalizer_band(0 if self._eq_bands else -1)
            ae.runtime.performance.restore_ini(values,report=self._settings_read_report)
            ae.runtime.stream_effects.channel_fx.restore_ini(values.get('channel_audio_fx','{}'),report=self._settings_read_report)
            self._sync_master_tuning()
            self.tuning_coarse.set(ae.runtime.performance.master_semitones, False)
            self.set_tuning_method(values.get("tuning_method", "cents"))
            if self._performance_dialog is not None:self._performance_dialog.refresh_controls()
            self.performance_reset.setEnabled(ae.runtime.performance.has_master_overrides())
            self._eq_graph_timer.start(0)
        except (ValueError, TypeError):
            # A malformed single scalar should not prevent application startup.
            pass
        finally:
            self._preset_programmatic = False

    def _capture_ui_values(self) -> dict[str, str]:
        bridge=getattr(self,'_effect_snapshots',None)
        if bridge is not None:bridge.flush()
        confirmed_output = self._last_confirmed_output_index
        if not 0 <= confirmed_output < self.device_combo.count():
            confirmed_output = 0
        values = {
            self._window_placement.KEY: self._window_placement.serialized(self),
            "dark": "1" if self._dark else "0",
            "color_theme": self._color_theme,
            "soundfont": stored_soundfont_selection_path(self._persisted_soundfont_path),
            "output_device": self.device_combo.itemText(confirmed_output),
            "skip_wasapi_warning": "1" if self._skip_wasapi_warning else "0",
            "skip_driver_setup_notice":str(int(self._skip_driver_setup_notice)),
            "volume": str(self.volume.value()),
            "delay_type": str(self.delay_type.currentIndex()),
        }
        # The pre-show placeholder is not an applied output. First-paint
        # autosave must not replace a stored identity before device startup.
        if self._last_confirmed_output_index < 0:
            values['output_device']=self._preferred_output_device or ''
            if self._preferred_output_endpoint_id:
                values['output_endpoint_id']=self._preferred_output_endpoint_id
        selected_output = self.device_combo.itemData(confirmed_output)
        if isinstance(selected_output, tuple) and selected_output[:1] == ("wasapi",) and len(selected_output) > 2:
            values["output_endpoint_id"] = selected_output[2]
        values.update({name: str(getattr(self, name).get()) for name in self._PERSISTED_SLIDERS})
        values["spatial_hf_ratio"] = str(round(self.spatial_hf_ratio.get(), 3))
        values["spatial_sample_rate"] = str(self.spatial_sample_rate.currentData())
        values.update(self._read_equalizer_state().ini_values())
        values.update({key: "1" if getattr(self, name).isChecked() != inverted else "0"
                       for name, key, inverted in self._PERSISTED_FLAGS})
        receivers=self._system_reset_persistence_baseline or self._system_reset_receivers
        values.update({key:str(int(receivers[field]!=inverted)) for field,key,inverted,_mode in SYSTEM_RESET_RECEIVERS})
        values.update({name: getattr(self, name).currentText() for name in self._PERSISTED_COMBOS})
        for snapshot in self._custom_presets.values():
            values.update({f"custom_{key}": value for key, value in snapshot.items()})
        channel_editor = getattr(self, "_performance_dialog", None)
        if channel_editor is not None:
            values.update(channel_editor.section_ui_values())
        else:
            values.update(self._unopened_channel_sections)
        # Channel expression/master tuning belong to INI;instrument scale and
        # drum-key profiles are owned and saved by the SFG model only.
        values.update(ae.runtime.performance.ini_values())
        values["tuning_method"] = self._tuning_method
        values["audio_fx_chain"] = self.audio_fx_panel.ini_value()
        values["audio_fx_enabled"] = str(int(self.audio_fx_panel.applied_enabled()))
        values['channel_audio_fx']=ae.runtime.stream_effects.channel_fx.ini_value()
        return values

    def _connect_ui_persistence(self) -> None:
        self._ui_save_timer = QTimer(self)
        self._ui_save_timer.setSingleShot(True)
        self._ui_save_timer.timeout.connect(self._save_ui_values)
        for name in self._PERSISTED_SLIDERS:
            getattr(self, name).slider.valueChanged.connect(self._queue_ui_save)
        for name, _key, _inverted in self._PERSISTED_FLAGS:
            getattr(self, name).toggled.connect(self._queue_ui_save)
        for name in (*self._PERSISTED_COMBOS, "delay_type", "spatial_sample_rate"):
            getattr(self, name).currentIndexChanged.connect(self._queue_ui_save)
        self.volume.valueChanged.connect(self._queue_ui_save)

    def _queue_ui_save(self, *_args) -> None:
        if hasattr(self, "performance_reset"):
            self.performance_reset.setEnabled(ae.runtime.performance.has_master_overrides())
        if not self._ui_settings_loading and not self._preset_programmatic:
            self._ui_save_timer.start(180)

    def _save_ui_values(self) -> None:
        if self._ui_settings_loading:
            return
        try:
            # Scalars merge across versions; sparse known records are a snapshot,
            # so removing a band or resetting an override must remove its key.
            if not hasattr(self,"_ui_owned_keys"):
                self._ui_owned_keys = self._ui_snapshot_owned_keys()
            owned = self._ui_owned_keys
            values = self._capture_ui_values()
            self._settings_writer.submit({"UI": values}, owned_keys={"UI": owned | values.keys()})
        except (OSError, ValueError) as exc:
            self.append_log(log_text("runtime.settings_save_failed", error=exc))

    @staticmethod
    def _ui_snapshot_owned_keys():
            owned = {f"{prefix}eq_band_{index}" for prefix in ("", "custom_")
                     for index in range(EQ_MAX_BANDS)}
            owned.update(f"scale_tune_{index}" for index in range(12))
            owned.update(f"drum_mix_{key}" for key in range(128))
            owned.update(f"expression_{channel}_{field}" for channel in range(128)
                         for field in EXPRESSION_EVENTS)
            owned.update(("eq_enabled", "eq_bypassed", "eq_mid_gain", "eq_mid_center",
                          "eq_mid_width", "velocity_enabled", "tuning_hz", "extra_delay_enabled",
                          "output_endpoint_id"))
            return frozenset(owned)

    def update_status(self):
        """Refresh controller metrics without consuming the meter peak window."""
        recording_signature = tuple((kind, recorder.recording_state,
            self._recording_save_worker is not None, bool(recorder.encoder), bool(recorder.stream))
            for kind in ('midi', 'wave') for recorder in (self._recording_owner(kind),))
        if self._recording_ui_state != recording_signature:
            self._refresh_recording_controls()
        presenting = self.isVisible() and not self.isMinimized()
        self.audio_session.maintain_output_observation()
        asio_state=self.audio_session.maintain_asio_output()
        if asio_state=="recovered":self.append_log(log_text("runtime.asio_recovered"))
        elif asio_state=="failed":self.set_runtime_status(log_text("runtime.asio_recovery_failed"))
        wasapi_state = self.audio_session.maintain_wasapi_output()
        if wasapi_state == "recovered":
            self.append_log(log_text("runtime.wasapi_recovered"))
        elif wasapi_state == "failed":
            self.set_runtime_status(log_text("runtime.wasapi_disconnected"))
        if ae.poll_auto_reset_idle():
            self.append_log(log_text("runtime.auto_reset_idle"))
        if ae.runtime.channel_reset_revision != self._last_channel_reset_revision:
            self._last_channel_reset_revision = ae.runtime.channel_reset_revision
            self._channel_controls_dirty = True
            self._queue_ui_save()
        if presenting and self._channel_controls_dirty:
            if self._performance_dialog is not None:self._performance_dialog.refresh_controls()
            self._channel_controls_dirty = False
        # Engine effects were applied on receipt. Keep their latest UI values
        # queued until there is a visible surface to update.
        delay_updates = ae.pop_delay_sysex_ui_updates() if presenting else None
        if delay_updates and hasattr(self, "delay_preset"):
            # SysEx arrives on the MIDI input thread; update widgets only from
            # this GUI-thread timer while the engine has already applied the
            # corresponding DSP values immediately.
            for field, value in delay_updates.items():
                if field == "preset":
                    blocked = self.delay_preset.blockSignals(True)
                    self.delay_preset.setCurrentText(value)
                    self.delay_preset.blockSignals(blocked)
                elif field == "delay_type":
                    blocked = self.delay_type.blockSignals(True)
                    self.delay_type.setCurrentIndex(value)
                    self.delay_type.blockSignals(blocked)
                else:
                    getattr(self, field).set(value, False)
        trace = self.midi_input_runtime.trace
        if not presenting and trace is None:
            # Keep the existing background observer requested for chord/log
            # collection without painting or opening hidden monitor widgets.
            self.audio_telemetry.request_observation()
            return
        observation_error = self.audio_telemetry.take_observation_error()
        if observation_error:
            self.append_log(log_text("runtime.observation_failed", error=observation_error))
        status = self.audio_telemetry.engine_status()
        if presenting:
            self.polyphony.setText("---" if status.active_voices is None else str(status.active_voices))
            self.load.set_cpu_percent(status.load_percent if status.load_percent is not None else 0)
            self.load_text.setText(f"{status.load_percent:.1f} %" if status.load_percent is not None else "---")
        if presenting:
            self._refresh_performance_info(status)
        detail = getattr(self, "_performance_dialog", None)
        detail_monitoring = (presenting and detail is not None and detail.isVisible()
                             and detail.content_stack.currentWidget() is detail.expression_page)
        if presenting and ((hasattr(self, "midi_monitor_enabled")
                            and self.midi_monitor_enabled.isChecked()) or detail_monitoring):
            frame = self.audio_telemetry.display_observation()
            if not self.audio_telemetry.observation_enabled or frame is not None:
                with ae.present_observation(frame):
                    self._refresh_channel_monitor_names()
        if trace is not None:
            effect_state = asdict(self.capture_effect_state())
            if effect_state != self._last_trace_effect_state:
                trace.record("effect_state", values=effect_state)
                self._last_trace_effect_state = effect_state
            trace.record("engine_status", stream=int(ae.runtime.stream_handle),
                         active_voices=status.active_voices, load_percent=status.load_percent)
            trace.record("tempo_observation", **self.audio_telemetry.tempo_diagnostic())
            levels = ae.runtime.output_meter.drain_diagnostic_levels()
            if levels is not None:
                trace.record("audio_window", **levels)

    def _refresh_performance_info(self, status=None):
        values = self.audio_telemetry.performance_observation()
        self._report_playback_snapshot=dict(values)
        latency_value = values.get("output_latency_ms")
        latency_key = "output_queue_ms" if values.get("output_latency_kind") == "queue" else "latency_ms"
        if latency_value is None:
            if values.get("playback_buffer_ms") is not None:
                latency_key, latency_value = "buffer_target_ms", values["playback_buffer_ms"]
            elif values.get("output_buffer_ms") is not None:
                latency_key, latency_value = "buffer_capacity_ms", values["output_buffer_ms"]
        self.performance_info_captions["latency_ms"].setText(tr(f"playback_info.{latency_key}"))
        self.install_state_tooltip(tr(f"playback_info.{latency_key}_tooltip"),
            self.performance_info_captions["latency_ms"], self.performance_info_values["latency_ms"])
        display = {"source_rate": values.get("source_rate"),
                   "output_rate": values.get("output_rate"),
                   "backend": values.get("backend"),
                   "block_ms": values.get("block_ms"),
                   "latency_ms": latency_value,
                   "note_jitter_mean_ms":values.get("note_jitter_mean_ms"),
                   "note_jitter_max_ms":values.get("note_jitter_max_ms"),
                   "format": ((values["source_bits"], values["source_bits"], values["source_encoding"])
                              if values.get("source_bits") and values.get("source_encoding") else None),
                   "output_format": ((values["output_bits"], values["output_valid_bits"], values["output_encoding"])
                                     if values.get("output_bits") and values.get("output_encoding") else None),
                   "channels": ((values["source_channels"], values.get("output_channels"))
                                if values.get("source_channels") else None)}
        for key, label in self.performance_info_values.items():
            number = display[key]
            if number is None:
                text = tr("playback_info.unavailable")
            elif key.endswith("rate"):
                text = tr("playback_info.hz", value=f"{int(number):,}")
            elif key.endswith("ms"):
                text = tr("playback_info.ms", value=f"{number:.2f}")
            elif key in ("format", "output_format"):
                bits, valid, encoding = number
                text = tr("playback_info.bits_format", bits=bits, encoding=encoding) if bits == valid else tr(
                    "playback_info.aligned_bits_format", bits=valid, container=bits, encoding=encoding)
            elif key == "channels":
                source, output = number
                text = tr("playback_info.channels_pair", source=source, output=output) if output else tr(
                    "playback_info.source_channels_only", source=source)
            elif key == "backend":
                text = tr(f"playback_info.backend_{number}")
            else:
                text = str(number)
            if label.text() != text:
                label.setText(text)
        self._refresh_playback_alerts(values, status)

    def _refresh_playback_alerts(self, values, status=None):
        self.playback_alert.mode_indicator.set_mode(values.get('midi_system_mode'))
        self.playback_alert.set_chord(self.audio_telemetry.chord_observation())
        self.playback_alert.set_tempo(self.audio_telemetry.tempo_observation())
        snapshots=getattr(self,'_effect_snapshots',None)
        self.playback_alert.mode_indicator.setEnabled(bool(ae.runtime.stream_handle) and
            ae.runtime.channel_command_executor is not None and
            not (snapshots and snapshots.pending('midi_mode')))
        identity = (ae.runtime.stream_handle, ae.runtime.stream_generation)
        if getattr(self, "_playback_alert_identity", None) != identity:
            self._playback_alert_identity = identity
            self._playback_fault_counts = {}
            self._playback_warning_evidence=audio_output.PlaybackWarningEvidence()
            self.playback_alert.reset_severity()
        now = time.monotonic()
        events,states,_details,reset=self._playback_warning_evidence.observe(values,
            status.load_percent if status is not None else None,clip=getattr(self,'_clip_active',False))
        if reset:
            self.playback_alert.reset_severity()
        self._playback_fault_counts=self._playback_warning_evidence.counters
        previous=getattr(self,'_report_health_states',{})
        if events or states!=previous:
            from error_reporting import record
            record('observed_playback_health',events=events,states=states,
                engine_load=status.load_percent if status is not None else None)
            self._report_health_states=dict(states)
        self.playback_alert.present(events=events,states=states,now=now)
        warnings=tuple(key for key,lamp in self.playback_alert.indicators.items() if lamp.active)
        messages=[tr(f"playback_info.alert_{key}") for key in warnings]
        caption = tr("playback_info.alert_summary",primary=messages[0],count=len(messages)-1) if len(messages)>1 else messages[0] if messages else ""
        if self.playback_alert.text() != caption:
            self.playback_alert.setText(caption)
        # Each lamp owns meaningful cause help. The card/blank gutters do not
        # need a second generic tooltip repeating this same visible summary.
        if self.playback_alert.toolTip():self.playback_alert.setToolTip('')
        # Paint lamp status locally. Never colour the panel with inherited QSS:
        # Qt tooltips must retain the common theme text colour in every state.

    def update_meter(self):
        left, right = self.audio_telemetry.peak_levels()
        self.meter.set_levels(left, right)
        # OutputMeter publishes linear float amplitude: unity == 0 dBFS.
        self._clip_active=any(math.isfinite(level) and level > 1 for level in (left,right))
        if hasattr(self,'playback_alert'):
            self.playback_alert.set_condition('clip',int(self._clip_active))

    def update_audio_visualizer(self):
        if hasattr(self, "audio_visualizer"):
            identity=(ae.runtime.stream_handle,ae.runtime.stream_generation)
            if getattr(self,'_visualizer_source_identity',None)!=identity:
                self._visualizer_source_identity=identity
                self.audio_visualizer.reset_waveform_history()
            self.audio_visualizer.set_audio_snapshot(
                *self.audio_telemetry.output_visual_samples(), finite_samples=True)

    def _update_display_rate_visuals(self):
        if (self._monitor_transitioning and hasattr(self, "_monitor_snapshot_frame")
                and self._monitor_snapshot_frame.isVisible()):
            return  # The transition displays one frozen frame for <250ms.
        self.update_meter()
        self.update_audio_visualizer()
        self.playback_alert.advance_frame()
        detail = getattr(self, "_performance_dialog", None)
        detail_monitoring = (detail is not None and detail.isVisible()
                             and detail.content_stack.currentWidget() is detail.expression_page)
        monitor_enabled = hasattr(self, "midi_monitor_enabled") and self.midi_monitor_enabled.isChecked()
        channels = set()
        if monitor_enabled:
            self._sync_monitor_ports()
            channels.update(range(self._monitor_port * 16,
                                  min((self._monitor_port + 1) * 16, ae.runtime.addresses.count)))
        if detail_monitoring:
            detail._sync_channel_addresses()
            channels.update(detail.selected_channels())
        fields = (*ae.CHANNEL_PARAMETERS,'active_voices') if detail_monitoring else ()
        field_channels = detail.selected_channels() if detail_monitoring else ()
        self.audio_telemetry.request_observation(channels, fields, field_channels=field_channels)
        frame = self.audio_telemetry.display_observation(channels, fields, field_channels=field_channels)
        if not self.audio_telemetry.observation_enabled or frame is not None:
            with ae.present_observation(frame):
                self._present_channel_visuals(monitor_enabled, detail_monitoring, detail)
        if hasattr(self, "eq_graph") and self.eq_graph.isVisible():
            self._poll_equalizer_graph()

    def _present_channel_visuals(self, monitor_enabled, detail_monitoring, detail):
        if monitor_enabled:
            values = self._refresh_channel_monitor_values()
            activity = self._refresh_channel_monitor_activity()
            if detail_monitoring:
                channel = detail.selected_channel()
                if not detail._global_scope() and self._monitor_port * 16 <= channel < (self._monitor_port + 1) * 16:
                    detail.refresh_live_monitor(values[channel % 16], activity[channel])
                else:
                    detail.refresh_live_monitor()
        elif detail_monitoring:
            detail.refresh_live_monitor()

    def _set_visual_screen(self, screen):
        if screen is self._visual_screen:
            return
        if self._visual_screen is not None:
            try:
                self._visual_screen.refreshRateChanged.disconnect(self._on_visual_refresh_rate_changed)
            except (RuntimeError, TypeError):
                pass
        self._visual_screen = screen
        if screen is not None:
            screen.refreshRateChanged.connect(self._on_visual_refresh_rate_changed)
        self._on_visual_refresh_rate_changed()

    def _on_visual_refresh_rate_changed(self, *_args):
        rate = float(self._visual_screen.refreshRate()) if self._visual_screen is not None else 0.0
        # QScreen documents this as approximate. It is retained for runtime
        # diagnostics; QWindow.requestUpdate performs the actual frame pacing.
        self._visual_refresh_hz = rate if rate > 1.0 else 60.0

    def _attach_display_rate_visuals(self):
        if self._shutdown_started or not self.isVisible() or self.isMinimized():
            return
        handle = self.windowHandle()
        if handle is None:
            return
        ae.runtime.output_meter.set_presentation_enabled(True)
        if handle is not self._visual_window_handle:
            # Hiding may recreate the native handle on some platforms. Remove
            # both old subscriptions before binding the new window lifetime.
            self._detach_display_rate_visuals()
            self._visual_window_handle = handle
            handle.installEventFilter(self)
            handle.screenChanged.connect(self._set_visual_screen)
            self._set_visual_screen(handle.screen())
        # Let the first native UpdateRequest perform presentation work after
        # showEvent returns. Offscreen tests have no platform-driven requests.
        if QApplication.platformName() == "offscreen":
            self._update_display_rate_visuals()
        else:
            request_visual_frame(handle,self,self._present_visual_frame)

    def _present_visual_frame(self):
        if self._shutdown_started or not self.isVisible() or self.isMinimized():return
        self._update_display_rate_visuals()
        if self._visual_window_handle is not None:
            request_visual_frame(self._visual_window_handle,self,self._present_visual_frame)

    def _schedule_display_rate_visuals(self):
        if self._shutdown_started or self._visual_attach_queued:
            return
        self._visual_attach_queued = True
        QTimer.singleShot(0, self._run_scheduled_visual_attach)

    def _run_scheduled_visual_attach(self):
        self._visual_attach_queued = False
        self._attach_display_rate_visuals()

    def _filter_soundfont_file_drop(self, watched, event):
        if (event.type() in (QEvent.DragEnter,QEvent.DragMove,QEvent.DragLeave,QEvent.Drop)
                and isinstance(watched,QWidget) and watched.window() is self):
            if event.type()==QEvent.DragLeave:
                self._set_soundfont_drop_hover(False)
                return False
            if event.mimeData().hasUrls():
                path=self._soundfont_drop_path(event.mimeData())
                allowed=bool(path and self._soundfont_drop_available() and event.possibleActions() & Qt.CopyAction)
                self._set_soundfont_drop_hover(allowed and event.type()!=QEvent.Drop)
                if allowed:
                    event.setDropAction(Qt.CopyAction); event.accept()
                    if event.type()==QEvent.Drop:
                        self._font_drop_pending=True
                        QTimer.singleShot(0,lambda path=path:self._activate_dropped_soundfont(path))
                else:
                    event.ignore()
                return True  # Do not let editable children insert a URL as text.
        return False

    def eventFilter(self, watched, event):
        if (
            not self._shutdown_started
            and watched is self._visual_window_handle
            and event.type() == QEvent.UpdateRequest
            and not compositor_visual_clock(watched,self)
        ):
            if self.isVisible() and not self.isMinimized():
                if QApplication.platformName()=="offscreen":self._update_display_rate_visuals()
                else:self._present_visual_frame()
        return QMainWindow.eventFilter(self, watched, event)

    @staticmethod
    def _soundfont_drop_path(mime):
        urls=mime.urls()
        if len(urls)!=1 or not urls[0].isLocalFile():
            return None
        path=urls[0].toLocalFile()
        return path if Path(path).suffix.casefold() in SUPPORTED_SOUNDFONT_LOAD_SUFFIXES else None

    def _soundfont_drop_available(self):
        return not (self._shutdown_started or self._font_load_active or self._font_drop_pending
                    or QApplication.activeModalWidget() is not None)

    def _set_soundfont_drop_hover(self,active):
        if bool(self.soundfont_card.property('fontDropTarget'))!=bool(active):
            self.soundfont_card.setProperty('fontDropTarget',bool(active))
            self.soundfont_card.style().unpolish(self.soundfont_card)
            self.soundfont_card.style().polish(self.soundfont_card)
            self.soundfont_card.update()

    def _activate_dropped_soundfont(self,path):
        self._font_drop_pending=False
        if self._soundfont_drop_available():
            popup=QApplication.activePopupWidget()
            if popup is not None:
                popup.close()
            self.activate_soundfont_path(path)

    def _detach_display_rate_visuals(self):
        """Stop the self-scheduling visual repaint loop before window teardown."""
        handle = self._visual_window_handle
        if handle is not None:
            remove_visual_frame(handle,self)
            try:
                handle.removeEventFilter(self)
            except RuntimeError:
                pass
            try:
                handle.screenChanged.disconnect(self._set_visual_screen)
            except (RuntimeError, TypeError):
                pass
        self._visual_window_handle = None
        self._set_visual_screen(None)

    def _pause_presentation(self):
        """Stop visual clocks on hide/minimize without touching audio state."""
        self._detach_display_rate_visuals()
        self._effect_window_motion.settle()
        ae.runtime.output_meter.set_presentation_enabled(False)
        if self._monitor_transitioning:
            self._monitor_window_motion.settle()
            self._finish_monitor_transition()

    def begin_shutdown(self):
        """Close is retryable; presentation-stopped is not resource-closed."""
        if getattr(self,"_shutdown_finished",False) or getattr(self,"_shutdown_in_progress",False):
            return
        self._shutdown_in_progress=True
        try:
            if self._shutdown_started:
                self._settings_writer.close()
                super().cleanup_resources()  # Resume a failed resource barrier only.
            else:
                self._perform_shutdown()
            self._shutdown_finished=True
        finally:
            # Failed completion/save barriers leave a usable, retryable UI.
            if not self._shutdown_started:
                with ae.runtime.channel_command_condition:
                    ae.runtime.channel_command_admission_open=True
            self._shutdown_in_progress=False

    def _perform_shutdown(self):
        """Perform the one normal, ordered shutdown path for close and Ctrl+C.

        Timers and the requestUpdate loop are presentation work. They must
        end before the BASS/MIDI resources and native QWidget handle are
        released, rather than being left for QApplication destruction.
        """
        notice=getattr(self,'_driver_setup_notice',None)
        if notice is not None:notice.close()
        preferences=getattr(self,'_preferences_dialog',None)
        report=getattr(preferences,'error_report',None)
        if report is not None and report.busy:raise RuntimeError(tr('error_report.busy_close'))
        # Flush a possibly queued Learn result before detaching its notifier.
        if hasattr(self, "_recording_timer"):
            self._recording_timer.stop()
        worker = getattr(self, "_recording_save_worker", None)
        if worker is not None and worker.isRunning():
            worker.wait()  # forced teardown must not destroy a live QThread
        if self._performance_dialog is not None:
            for _widget, field, _caption in self._performance_dialog._cc_fields.values():
                field.title._finish_edit(True)
        if not self.soundfont_runtime.finish_runtime_controls():
            raise RuntimeError(tr('runtime.preset_controls_pending'))
        # Stop producers before waiting for an empty command set. Continuous
        # external MIDI could otherwise refill _raw_frames during every Close
        # attempt. The native owner still drains/cancels admitted work and
        # retains its leases until the ordinary resource teardown below.
        player = getattr(self, "_midi_player", None)
        if player is not None and player.transport.stop_and_wait() is False:
            raise RuntimeError(tr('runtime.channel_executor_retained'))
        if self.midi_input_runtime.stop() is False:
            raise RuntimeError(tr('midi_pipe.native_shutdown_failed'))
        # Accepted edits must reach saved metadata before the final INI
        # snapshot. A timeout preserves the native owner for Close retry.
        if not ae.runtime.settle_channel_commands(stop_admission=True):
            raise RuntimeError(tr('runtime.channel_commands_settle_failed'))
        if hasattr(self, "_ui_save_timer") and self._ui_save_timer.isActive():
            self._ui_save_timer.stop()
        # The metadata pump may have finished while its Qt presentation signal
        # is still queued. Snapshot committed engine state, not timer activity;
        # do not processEvents or admit reentrant edits during shutdown.
        self._save_ui_values()
        self._save_midi_cc_configuration()
        # This is a durable ownership barrier BEFORE UI/MIDI/audio teardown.
        # Failed writes retain their overlay/worker and reach Close's warning;
        # retry must not enter the resource-only resume branch prematurely.
        self._settings_writer.flush(retry_failed=True)
        player = getattr(self, "_midi_player", None)
        if player is not None:
            player.shutdown()
            player.hide()
            self._midi_player = None
        recording = getattr(self, '_recording_dialog', None)
        if recording is not None:
            recording.hide()
        licenses = getattr(self, '_licenses_dialog', None)
        if licenses is not None:
            licenses.hide()
        group_dialog = getattr(self, '_group_dialog', None)
        creator = getattr(group_dialog, 'instrument_creator', None)
        if creator is not None:
            creator.shutdown()
        ae.runtime.midi_learn.on_change = None
        ae.runtime.channel_policy.on_change = None
        self._shutdown_started = True
        presentation=getattr(self,'_channel_command_presentation',None)
        if presentation is not None:presentation.close_presentation()
        snapshots=getattr(self,'_effect_snapshots',None)
        if snapshots is not None:snapshots.close_presentation()
        QApplication.instance().removeEventFilter(self._interaction_filter)
        self._set_soundfont_drop_hover(False)
        ae.runtime.output_meter.set_presentation_enabled(False)
        if self._performance_dialog is not None:
            self._performance_dialog.hide()
        preferences = getattr(self, "_preferences_dialog", None)
        if preferences is not None:
            preferences.reject()
        worker = self._font_load_worker
        if worker is not None and worker.isRunning():
            worker.wait()  # Never release BASS stream/font handles mid-load.
        ae.set_font_preload_callback(None)
        ae.runtime.output_meter.set_diagnostic_enabled(False)
        for timer_name in ("status_timer", "log_timer", "_eq_graph_timer"):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()
        for disclosure_name in ("extended_delay_details", "spatial_details"):
            disclosure = getattr(self, disclosure_name, None)
            if disclosure is not None:
                disclosure.settle()
        rack_panel = getattr(self, "audio_fx_panel", None)
        if rack_panel is not None:
            for _toggle, disclosure in rack_panel._advanced.values():
                disclosure.settle()
        self._monitor_window_motion.settle()
        self._effect_window_motion.settle()
        if hasattr(self, "_monitor_snapshot_frame"):
            self._monitor_snapshot_frame.hide()
        self._detach_display_rate_visuals()
        self.audio_visualizer.close_analysis()
        self.setUpdatesEnabled(False)
        self._settings_writer.close()
        super().cleanup_resources()

    def restart(self):
        """Request a real close; relaunch only after the Qt loop has ended."""
        # Presentation has stopped before a resource barrier can fail. That
        # intermediate state is retryable, not a completed/reentrant shutdown.
        if getattr(self, "_shutdown_in_progress", False) or getattr(self, "_shutdown_finished", False):
            return
        self._restart_requested = True
        # A restart must not be converted into a hide-to-tray action.
        self._force_close = True
        if not self.close():
            self._restart_requested = False
            self._force_close = False
            return
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def minimumSizeHint(self):
        # QMainWindow otherwise promotes the central layout's content hint
        # into a top-level minimum during show(). The user owns the window
        # size; the visible grid may be clipped but it must not force resize.
        return QSize(0, 0)

    def set_dark(self, dark):
        self._dark = bool(dark)
        choice=getattr(self,'appearance_choice',None)
        if choice is not None:choice.set_selected(int(self._dark))
        theme_key = self._dark, self._compact_mode, self._color_theme
        if self._applied_theme_key == theme_key:
            return  # Re-show and unchanged settings never repolish the tree.
        from native_widgets import native_theme_batch
        with native_theme_batch(self):
            self._apply_theme(theme_key)

    def _apply_theme(self,theme_key,*,building=False):
        from native_widgets import apply_native_stylesheet
        palette = color_palette(self._dark, self._color_theme)
        apply_native_application_palette(palette)
        # A QToolTip is an independent top-level popup, not a child of this
        # window.  Widget-local QSS therefore cannot reliably own its box
        # model after Qt reuses it for a different hovered control.  Keep the
        # complete tooltip rule at QApplication scope; window-local QSS still
        # owns the rest of the native surface.
        apply_native_tooltip_theme(palette)
        apply_native_stylesheet(self,native_stylesheet(palette, self._compact_mode))
        if building:
            return  # Child-specific refresh waits for the completed widget tree.
        player = getattr(self, "_midi_player", None)
        if player is not None:
            player.set_theme()
        apply_native_text_metrics(self)
        for checkbox in self.findChildren(OutlineCheckBox):
            checkbox.set_theme(palette)
        if hasattr(self, "meter"): self.meter.set_theme(self._dark)
        if hasattr(self, "playback_alert"): self.playback_alert.set_theme(self._dark)
        if hasattr(self, "audio_visualizer"): self.audio_visualizer.set_theme(self._dark)
        if hasattr(self, "eq_graph"): self.eq_graph.set_theme(self._dark)
        if hasattr(self, "load"): self.load.set_theme(self._dark)
        if self._performance_dialog is not None:
            self._apply_channel_editor_theme(self._performance_dialog)
        for strip in getattr(self, "channel_strips", ()):
            for knob in strip["knobs"].values():
                knob.set_theme(self._dark)
            strip["activity"].set_theme(self._dark)
            strip["instrument"].set_theme(self._dark)
            program_font = native_text_font(12)
            program_font.setLetterSpacing(QFont.AbsoluteSpacing, -0.6)
            strip["program"].setFont(program_font)
        for label in getattr(self, "channel_monitor_axis_labels", ()):
            label.set_theme(self._dark)
        from native_widgets import flush_native_theme_spacing
        flush_native_theme_spacing()
        self._fit_channel_monitor_content()
        # Theme polish can change measured control/viewport extents. Publish
        # the new page height as well as the strip height before laying out
        # the main cards; retaining the previous card height clips the footer.
        self._sync_effect_card_height()
        if hasattr(self, "log"):
            # QTextDocument selection can bypass stylesheet colour rules on
            # Windows while a drag is active. Set its actual palette too.
            log_palette = self.log.palette()
            log_palette.setColor(QPalette.Base, QColor(palette.input))
            log_palette.setColor(QPalette.Text, QColor(palette.text))
            log_palette.setColor(QPalette.Highlight, QColor(palette.selection))
            log_palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
            self.log.setPalette(log_palette)
        self._applied_theme_key = theme_key
        self._sync_effect_label_widths()

    apply_theme = set_dark

    def set_color_theme(self, name):
        self._color_theme = name if name in COLOR_THEMES else "blue"
        self.set_dark(self._dark)

    def resizeEvent(self, event):
        QMainWindow.resizeEvent(self, event)
        if hasattr(self, "main_viewport"):
            self.main_viewport.sync_content_geometry()
            self.side_rail.sync_content_geometry()

    def _apply_responsive_density(self):
        """Apply the single shared compact density before any clipping."""
        density = LAYOUT_DENSITIES[self._layout_density]
        card_padding = density["card_padding"]
        self.soundfont_card.layout().setContentsMargins(*(card_padding,) * 4)
        self.state_card.layout().setContentsMargins(*density["state_card_margin"])
        self.performance_info_card.layout().setContentsMargins(*density["state_card_margin"])
        self.playback_warning_card.layout().setContentsMargins(*density["state_card_margin"])
        self.effects_card.layout().setContentsMargins(*(card_padding,) * 4)
        for page_layout in getattr(self, "_tab_page_layouts", ()): 
            bordered_page_layout(page_layout,CARD_ROW_GAPS["effects"],compact=True)
        self.log_card.setMinimumHeight(density["log_min_height"])
        self.log_card.setMaximumHeight(16777215)
        self.log.setMinimumHeight(0)
        self.log.setMaximumHeight(16777215)
        # The three device rows are one grid component.  In particular the
        # MIDI-ready pill is not allowed to collapse before the neighbouring
        # combo, line edit, or button when the parent card is clipped.
        for cell in getattr(self, "_top_row_cells", ()):
            cell.setFixedHeight(FORM_ROW_HEIGHT)
        for row in self.findChildren(NativeControlRow):
            row.slider.setFixedHeight(FORM_ROW_HEIGHT)
        self.set_dark(self._dark)
        self._lock_main_vertical_priority()
        self._lock_side_top_rows()

    def _read_equalizer_state(self):
        return EqualizerState(tuple(self._eq_bands), self.eq_preset.currentData() == "off",
                              self.eq_output_trim.get())

    def on_equalizer(self, *_args):
        if self._preset_programmatic:
            return
        if 0 <= self._eq_selected < len(self._eq_bands):
            prior = self._eq_bands[self._eq_selected]
            self._eq_bands[self._eq_selected] = EQBand(
                self.eq_center.get(), self.eq_width.get(),
                prior.gain_db if prior.kind in ("high_pass", "low_pass") else self.eq_gain.get(),
                prior.kind)
        self._apply_equalizer()

    def on_equalizer_filter(self, *_args):
        if self._preset_programmatic or not 0 <= self._eq_selected < len(self._eq_bands):
            return
        kind = self.eq_filter.currentData()
        if kind not in EQ_FILTER_KINDS:
            return
        band = self._eq_bands[self._eq_selected]
        self._eq_bands[self._eq_selected] = EQBand(
            band.center_hz, band.bandwidth_semitones, band.gain_db, kind)
        self.on_equalizer_band(self._eq_selected)
        self._apply_equalizer()
        # Filter-family changes are infrequent. Refresh this one curve
        # synchronously so a stale bell response never accompanies a new
        # high/low-pass label while the coalesced worker catches up.
        self._refresh_equalizer_graph()

    def _apply_equalizer(self):
        self._sync_equalizer_preset()
        requested = self._read_equalizer_state()
        if self.effect_runtime.set_equalizer(requested) is False:
            previous = getattr(self, "_applied_equalizer_state", EqualizerState())
            self._eq_bands = list(previous.bands)
            self.eq_output_trim.set(previous.output_trim_db, False)
            identity = getattr(self, "_applied_equalizer_preset", "off")
            blocked = self.eq_preset.blockSignals(True)
            self.eq_preset.setCurrentIndex(self.eq_preset.findData(identity))
            self.eq_preset.blockSignals(blocked)
            self.on_equalizer_band(min(self._eq_selected, len(self._eq_bands) - 1))
            self.append_log(log_text("runtime.eq_apply_failed"))
            return
        self._applied_equalizer_state = requested
        self._applied_equalizer_preset = self.eq_preset.currentData()
        if self.eq_preset.currentData() == "custom":
            self._remember_custom_preset("equalizer")
        # Keep point feedback immediate; the independently measured curve is
        # coalesced, never allowed to delay audio or drag-position feedback.
        self.eq_graph.set_response(self.eq_graph._response, tuple(self._eq_bands),
                                   self._eq_selected, self._read_equalizer_state().bypassed)
        self._poll_equalizer_graph()
        if hasattr(self, "_ui_save_timer"):
            self._queue_ui_save()

    def on_equalizer_band(self, index):
        self._eq_selected = index if 0 <= index < len(self._eq_bands) else -1
        valid = self._eq_selected >= 0
        band = self._eq_bands[self._eq_selected] if valid else EQ_DEFAULT_BANDS[0]
        blocked = self.eq_filter.blockSignals(True)
        self.eq_filter.setCurrentIndex(self.eq_filter.findData(band.kind))
        self.eq_filter.blockSignals(blocked)
        self.eq_filter.setEnabled(valid)
        for control, value in ((self.eq_center, band.center_hz), (self.eq_width, band.bandwidth_semitones),
                               (self.eq_gain, 0 if band.kind in ("high_pass", "low_pass") else band.gain_db)):
            control.set(value, False)
            control.setEnabled(valid and (control is self.eq_center or
                                         (control is self.eq_width and band.kind == "bell") or
                                         (control is self.eq_gain and band.kind not in ("high_pass", "low_pass"))))
        self.eq_graph.set_response(self.eq_graph._response, tuple(self._eq_bands),
                                   self._eq_selected, self._read_equalizer_state().bypassed)
        self._sync_equalizer_preset()

    def _sync_equalizer_preset(self):
        if self._preset_programmatic or not hasattr(self, "eq_preset"):
            return
        preset = EQ_PRESETS.get(self.eq_preset.currentData())
        if preset is not None and (tuple(self._eq_bands) != EqualizerState(
                preset).validated(self._engine_preferences.sample_rate).bands
                                   or abs(self.eq_output_trim.get()) > 1e-9):
            # Like other tabs, editing a preset becomes Custom. Do not rename
            # it by guessing a different preset from zero gains or selection.
            blocked = self.eq_preset.blockSignals(True)
            self.eq_preset.setCurrentIndex(self.eq_preset.findData("custom"))
            self.eq_preset.blockSignals(blocked)

    def apply_equalizer_preset(self, _name):
        if self._preset_programmatic:
            return
        preset = EQ_PRESETS.get(self.eq_preset.currentData())
        if preset is None:
            self._recall_custom_preset("equalizer")
            return
        self._eq_bands = list(EqualizerState(preset).validated(
            self._engine_preferences.sample_rate).bands)
        self.eq_output_trim.set(0, False)
        self.on_equalizer_band(0)
        self._apply_equalizer()

    def add_equalizer_band(self, hz, db):
        if len(self._eq_bands) >= EQ_MAX_BANDS:
            return  # Tooltip records the real-time resource limit.
        index = max(0, len(self._eq_bands) - 1)
        self._eq_bands.insert(index, EQBand(round(hz), 12, round(db, 1)))
        self._eq_bands = list(self._read_equalizer_state().validated(self._engine_preferences.sample_rate).bands)
        self.on_equalizer_band(index)
        self._apply_equalizer()

    def remove_equalizer_band(self, index):
        if 0 <= index < len(self._eq_bands):
            self._eq_bands.pop(index)
            self.on_equalizer_band(min(index, len(self._eq_bands) - 1))
            self._apply_equalizer()

    def edit_equalizer_band(self, index, hz, db):
        if 0 <= index < len(self._eq_bands):
            band = self._eq_bands[index]
            state = EqualizerState((EQBand(hz, band.bandwidth_semitones,
                                           band.gain_db if band.kind in ("high_pass", "low_pass") else db,
                                           band.kind),)).validated(
                self._engine_preferences.sample_rate)
            self._eq_bands[index] = state.bands[0]
            # The graph is a continuous input, not a slider-round-trip. Only
            # format the inspector; never read its rounded values back here.
            if self._eq_selected != index:
                self.on_equalizer_band(index)
            else:
                self.eq_center.set(state.bands[0].center_hz, False)
                self.eq_gain.set(state.bands[0].gain_db, False)
            self._apply_equalizer()

    def _poll_equalizer_graph(self):
        if getattr(self, "_shutdown_started", False):
            return
        state = self._read_equalizer_state()
        completed, pending = self.effect_runtime.poll_equalizer_response(state)
        if completed is not None:
            measured_state, response = completed
            dragging = self.eq_graph.dragged_band()
            progressive = (
                response is not None
                and 0 <= dragging < len(state.bands) == len(measured_state.bands)
                and measured_state.bypassed == state.bypassed
                and measured_state.output_trim_db == state.output_trim_db
                and all(
                    old == new if index != dragging else
                    (old.kind, old.bandwidth_semitones) == (new.kind, new.bandwidth_semitones)
                    for index, (old, new) in enumerate(zip(measured_state.bands, state.bands))
                )
            )
            if measured_state == state or progressive:
                # During an active drag this is the newest *measured* BASS
                # response, not an invented curve. It may trail the point by
                # one worker job; after release only exact states are shown.
                self.eq_graph.set_response(response, state.bands, self._eq_selected, state.bypassed)
        # Native windows poll on display UpdateRequest. Hidden/offscreen
        # surfaces need a timer fallback; no 60ms curve-rate cap remains.
        if pending and (self._visual_window_handle is None or QApplication.platformName() == "offscreen"
                        or not self.isVisible()):
            self._eq_graph_timer.start(max(1, math.ceil(1000 / self._visual_refresh_hz)))

    def _refresh_equalizer_graph(self):
        """Synchronous refresh for diagnostics and infrequent filter-family changes."""
        state = self._read_equalizer_state()
        response = self.effect_runtime.equalizer_response(state)
        self.eq_graph.set_response(response, state.bands,
                                   self._eq_selected, state.bypassed)

    def on_compressor(self):
        for row in EFFECT_ROWS["dynamics"][:5]:
            getattr(self, row.attr).setEnabled(self.compressor_enabled.isChecked())
        self.effect_runtime.set_compressor(CompressorState(
            self.compressor_enabled.isChecked(), int(round(self.comp_threshold.get())),
            float(self.comp_ratio.get()), int(round(self.comp_attack.get())),
            int(round(self.comp_release.get())), int(round(self.comp_makeup.get())),
        ))

    def on_limiter(self):
        for row in EFFECT_ROWS["dynamics"][5:]:
            getattr(self, row.attr).setEnabled(self.limiter_enabled.isChecked())
        self.effect_runtime.set_limiter(LimiterState(
            self.limiter_enabled.isChecked(), float(self.limiter_ceiling.get()),
            int(round(self.limiter_release.get())),
        ))

    def _edit_master_output(self,value):
        bridge=getattr(self,'_effect_snapshots',None)
        if bridge is None or ae.runtime.channel_command_executor is None:
            return self.effect_runtime.set_master_volume(value)
        bridge.touch('volume')
        def submit(completed):
            def done(receipt):
                completed(receipt,{'volume':ae.runtime.stream_effects.master_volume.value})
            return self.effect_runtime.set_master_volume(value,on_complete=done)
        receipt=bridge.submit('master_volume',('volume',),submit)
        if receipt is not None and not receipt.accepted:
            self._install_effect_snapshot('master_volume',receipt,{'volume':ae.runtime.stream_effects.master_volume.value})
            self._engine_effect_completed(receipt)
        return receipt

    def _sync_master_tuning(self):
        cents = ae.runtime.performance.master_cents
        self.tuning_master.set(cents, False)
        self.tuning_hz.set(tuning_hz(cents), False)

    def on_master_tuning(self, value, *, hz=False):
        if getattr(self, "_ui_settings_loading", False):
            return
        from midi_input_runtime import MASTER_FINE_PARAMETER
        cents=MASTER_FINE_PARAMETER.clamp(tuning_cents(value) if hz else value)
        self.tuning_master.set(cents,False);self.tuning_hz.set(tuning_hz(cents),False)
        self._submit_master_edit('master_fine',(('master_fine',cents),),
                                 {'tuning_master':cents,'tuning_hz':tuning_hz(cents)})

    def on_master_coarse(self, value):
        if getattr(self, "_ui_settings_loading", False):
            return
        from midi_input_runtime import MASTER_COARSE_PARAMETER
        value=MASTER_COARSE_PARAMETER.clamp(value)
        self._submit_master_edit('master_coarse',(('master_coarse',value),),{'tuning_coarse':value})

    def _submit_master_edit(self,group,values,snapshot):
        """Same receipt/presentation owner as effect and channel controls."""
        bridge=self._effect_snapshots
        for field in snapshot:bridge.touch(field)
        def submit(completed):
            def finish(report):completed(report,snapshot if report.state_changed else None)
            receipt=ae.runtime.submit_master_controls(values,on_complete=finish)
            if receipt.completed:finish(receipt)
            return receipt
        receipt=bridge.submit(group,tuple(snapshot),submit)
        if receipt is None or not receipt.accepted:
            self._sync_master_tuning()
            self.tuning_coarse.set(ae.runtime.performance.master_semitones,False)
            if receipt is not None:self._engine_effect_completed(receipt)
        self._queue_ui_save()

    def set_tuning_method(self, method):
        # Never read the rounded label/handle back into the runtime here.
        # MIDI/INI are owned by the unrounded canonical cent value.
        self._tuning_method = "hz" if method == "hz" else "cents"
        index = 1 if self._tuning_method == "hz" else 0
        self.tuning_methods.set_selected(index)
        self.tuning_stack.setCurrentIndex(index)
        if hasattr(self, "_ui_save_timer"):
            self._queue_ui_save()

    def open_performance_details(self):
        self.open_group(performance=True)

    def _apply_channel_editor_theme(self,dialog):
        dialog.channel_activity.set_theme(self._dark)
        for control in (*dialog.channel_mixer_controls.values(),*dialog.expression_controls.values(),
                        *dialog.auxiliary_cc_controls.values(),dialog.channel_pressure_control,dialog.poly_pressure_control):
            if isinstance(control,NativeKnobField):control.set_theme(self._dark)
        for _widget,field,_caption in dialog._cc_fields.values():
            field.cc_badge.set_theme(self._dark);field.cc_status.set_theme(self._dark)

    def _channel_command_context(self,channel,field):
        return (ae.runtime.stream_generation,ae.runtime.channel_policy.revision,
                ae.runtime.channel_policy.pressure_key(channel) if field=='key_pressure' else None)

    def _refresh_channel_command_presentation(self):
        if self._shutdown_started:return
        if hasattr(self,'channel_strips'):
            for strip in self.channel_strips:strip.pop('_monitor_visual_key',None)
            self._refresh_channel_monitor_values()
        dialog=getattr(self,'_performance_dialog',None)
        if dialog is not None and shiboken6.isValid(dialog):
            dialog.refresh_live_monitor();dialog._update_reset_buttons()

    @property
    def channel_command_presentation(self):
        if not hasattr(self,'_channel_command_presentation'):
            self._channel_command_presentation=NativeChannelCommandPresentation(self,
                lambda:getattr(ae.runtime,'submit_channel_controls',None),ae.set_channel_control_value,
                self._channel_command_context,self._refresh_channel_command_presentation,self._queue_ui_save,
                mode_submitter=lambda:getattr(ae.runtime,'submit_channel_modes',None),legacy_mode_setter=ae.set_channel_mode_switch,
                auxiliary_submitter=lambda:getattr(ae.runtime,'submit_auxiliary_controls',None),
                legacy_auxiliary_setter=ae.set_channel_auxiliary_cc,
                mode_intent=lambda ch,target,enabled,pending:ae.channel_mode_switch_selection(
                    ae.runtime.performance.observed_expression_value(ch,'mode') if pending is None else pending,target,enabled))
        return self._channel_command_presentation

    def submit_channel_controls(self,channels,field,amount,*,persist=False):
        return self.channel_command_presentation.submit(channels,field,amount,persist=persist)

    def submit_channel_modes(self,channels,target,enabled):
        return self.channel_command_presentation.submit(channels,target,enabled,persist=True)

    def submit_auxiliary_controls(self,channels,number,amount):
        return self.channel_command_presentation.submit(channels,f'aux_{number}',amount)

    def submit_channel_instruments(self,requests):
        def submit(completed):
            receipt=ae.runtime.submit_channel_instruments(requests,on_complete=completed)
            if receipt.completed:completed(receipt)
            return receipt
        receipt=self._effect_snapshots.submit_receipt('instrument',submit)
        if receipt is None or not receipt.accepted:
            if receipt is not None:self._engine_effect_completed(receipt)
            self._refresh_channel_command_presentation()
        return receipt

    @property
    def performance_dialog(self):
        """Explicit preparation/use creates widgets; observation/save/theme do not."""
        if self._performance_dialog is None:
            dialog=NativePerformanceDialog(self,ae.runtime.performance,self._save_ui_values)
            self._performance_dialog=dialog
            dialog.destroyed.connect(lambda _object=None,owned=dialog:
                self._forget_performance_dialog(owned))
            self._apply_channel_editor_theme(dialog)
        return self._performance_dialog

    def _forget_performance_dialog(self,dialog):
        if self._performance_dialog is dialog:self._performance_dialog=None

    def _show_performance_dialog(self):
        self.performance_dialog.show()
        self.performance_dialog.raise_()
        self.performance_dialog.activateWindow()

    def open_monitor_channel_editor(self, channel):
        if channel is not None and ae.runtime.stream_handle:
            ae.ensure_midi_port(channel // 16)
        self.performance_dialog.show_channel(channel)
        self._show_performance_dialog()

    def apply_reverb_preset(self, name, *, update_controls=True):
        """Mirror BMSynth's event-30 handler and read back all five values."""
        if getattr(self, "_preset_programmatic", False):
            return
        bridge=getattr(self,'_effect_snapshots',None)
        if bridge is not None and update_controls:bridge.invalidate('reverb')
        if name == "Custom":
            if update_controls:
                self._recall_custom_preset("reverb")
            return
        names = ["Default", "Off"] + [item[0] for item in REVERB_PRESETS[1:]]
        try:
            macro_index = names.index(name)
        except ValueError:
            return
        events = REVERB_PRESET_EVENTS
        stream = ae.runtime.stream_handle
        values = None
        if not update_controls:
            # Stream recreation/INI import: original startup sends the macro
            # first, then the saved scalar values. Do not replace visible INI
            # values with macro readback on this path.
            if stream and macro_index > 0 and not self.effect_events.send(
                MIDI_EVENT_REVERB_MACRO, ae.BMSYNTH_REVERB_MACROS[macro_index],
            ):
                self.append_log(log_text("runtime.reverb_preset_apply_failed"))
            return
        if macro_index == 0:
            values = BMSYNTH_REVERB_DEFAULT
        elif stream:
            if bridge is not None and getattr(self.effect_events,'macro_submitter',None) is not None:
                receipt=bridge.submit('reverb',tuple(attr for attr,_event in events),
                    lambda completed:self.effect_events.submit_macro(MIDI_EVENT_REVERB_MACRO,
                        ae.BMSYNTH_REVERB_MACROS[macro_index],events,on_complete=completed))
                if receipt is None or not receipt.accepted:self.append_log(log_text('runtime.reverb_preset_apply_failed'))
                return
            values = self.effect_events.apply_macro_and_read(
                MIDI_EVENT_REVERB_MACRO,
                ae.BMSYNTH_REVERB_MACROS[macro_index], events,
            )
            if values is None:
                self.append_log(log_text("runtime.reverb_preset_apply_failed"))
                return
        if values is None:
            # UI-only/no-stream fallback uses the verified source table. The
            # live stream path above remains authoritative for macro output.
            if name == "Off":
                values = {attr: 0 for attr, _event in events}
            else:
                source_index = next((i for i, item in enumerate(REVERB_PRESETS) if item[0] == name), 0)
                _, time_ms, delay, high, low = REVERB_PRESETS[source_index]
                values = {"reverb_level": self.reverb_level.get(), "reverb_time": time_ms,
                          "reverb_delay": delay, "reverb_hi": high, "reverb_lo": low}
        for attr, event in events:
            value = values[attr]
            getattr(self, attr).set(value, False)
            if stream and macro_index == 0:
                self.bass_event(event, value)

    def _read_reverb_preset_values(self):
        return {
            attr: getattr(self, attr).get()
            for attr in ("reverb_level", "reverb_time", "reverb_delay", "reverb_hi", "reverb_lo")
        }

    def _read_delay_state(self):
        delay_sysex = getattr(self, "delay_sysex_enabled", None)
        return delay_state_from_controls({key:getattr(self,key).get() for key in
            ('delay_level','delay_time','delay_feedback')},self.delay_type.currentIndex(),
            delay_sysex.isChecked() if delay_sysex is not None else False)

    def _read_extended_delay_state(self):
        return extended_delay_state_from_controls({key:getattr(self,key).get() for key in
            ('extra_delay_time','extra_delay_wet','extra_delay_feedback')},
            self.extra_delay_ping_pong.isChecked())

    def on_extended_delay(self, *_args):
        requested = self._read_extended_delay_state()
        result = self.effect_runtime.set_extended_delay(requested)
        if result is False or result == "disabled":
            restored = (ExtendedDelayState() if result == "disabled" else
                        getattr(self, "_applied_extended_delay_state", ExtendedDelayState()))
            blocked = self.extra_delay_ping_pong.blockSignals(True)
            self.extra_delay_ping_pong.setChecked(restored.ping_pong)
            self.extra_delay_ping_pong.blockSignals(blocked)
            for control, value in ((self.extra_delay_time, restored.time_ms),
                                   (self.extra_delay_wet, restored.wet_percent),
                                   (self.extra_delay_feedback, restored.feedback_percent)):
                control.set(value, False)
            self._sync_extended_delay_controls()
            self._applied_extended_delay_state = restored
            self.append_log(log_text("runtime.extended_delay_apply_failed"))
            return
        self._applied_extended_delay_state = requested
        self._sync_extended_delay_controls()
        if hasattr(self, "_ui_save_timer"):
            self._queue_ui_save()

    def _sync_extended_delay_controls(self):
        self.extended_delay_details.set_expanded(self.extra_delay_details_expanded.isChecked())

    def _sync_spatial_details(self, *_args):
        expanded = self.spatial_details_expanded.isChecked()
        self.spatial_details.set_expanded(expanded)

    def _set_spatial_sample_rate(self, rate):
        combo=self.spatial_sample_rate
        index=combo.findData(rate)
        blocked=combo.blockSignals(True)
        try:combo.setCurrentIndex(index if index>=0 else 0)
        finally:combo.blockSignals(blocked)
        return index>=0

    def system_reset_receivers(self):
        """One non-widget state snapshot,including before preferences exist."""
        return dict(self._system_reset_receivers)

    def set_system_reset_receive(self,field,receive):
        if field not in self._system_reset_receivers or type(receive) is not bool:
            raise ValueError('system reset receive policy')
        if self._system_reset_receivers[field]==receive:return
        if field=='gs_reset':receipt=self.effect_runtime.set_gs_reset(receive)
        else:receipt=self.effect_runtime.set_system_reset_receive(field,receive)
        if getattr(receipt,'accepted',True) is False:
            self.system_reset_receive_changed.emit(field,self._system_reset_receivers[field])
            return False
        self._system_reset_receivers[field]=receive
        self.system_reset_receive_changed.emit(field,receive)
        if self._system_reset_persistence_baseline is None:self._queue_ui_save()
        return True

    def capture_effect_state(self):
        """Capture controls and shared receiver state;EffectRuntime applies it."""
        return EffectState.from_controls(
            {name:getattr(self,name).get() for name in EFFECT_CAPTURE_CONTROLS},
            master_volume=self.volume.value(),
            reverb_preset=self.reverb_preset.currentText(),chorus_preset=self.chorus_preset.currentText(),
            delay_type=self.delay_type.currentIndex(),delay_sysex=self.delay_sysex_enabled.isChecked(),
            extra_delay_ping_pong=self.extra_delay_ping_pong.isChecked(),
            audio_fx=self.audio_fx_panel.applied_slots(),
            audio_fx_enabled=self.audio_fx_panel.applied_enabled(),
            channel_audio_fx=ae.runtime.stream_effects.channel_fx.ini_value(),
            humanize_preserve_simultaneity=self.humanize_preserve_simultaneity.isChecked(),
            equalizer=self._read_equalizer_state(),
            compressor_enabled=self.compressor_enabled.isChecked(),
            limiter_enabled=self.limiter_enabled.isChecked(),
            overnote=self.overnote.isChecked(),
            **self.system_reset_receivers(),
            spatial_sample_rate=self.spatial_sample_rate.currentData(),
        )

    def on_dx(self):
        # Level zero is the explicit bypass state for spatial ambience.
        self.effect_runtime.set_spatial(self.dx_level.get(), SpatialParameters(
            self.spatial_input_gain.get(), self.spatial_time.get(), self.spatial_hf_ratio.get(),
            self.spatial_sample_rate.currentData()))

    def on_global_reverb(self, value):
        self.effect_runtime.set_reverb_send_floor(value)

    def on_global_chorus(self, value):
        self.effect_runtime.set_chorus_send_floor(value)

    def on_stereo(self, multiplier):
        self.effect_runtime.set_stereo_multiplier(multiplier)

    def set_runtime_status(self, text):
        """Failures stay visible in the log without occupying the waveform."""
        self.append_log(text)

    def create_virtual_port(self):
        """Start the local listener without presenting it as DAW readiness."""
        if self.midi_input_runtime.start():
            self.midi_device_combo.setItemText(0, MIDI_PORT_NAME)
        else:
            self.midi_device_combo.setItemText(0, tr("runtime.port_create_failed"))
            self.set_runtime_status(log_text("runtime.port_create_failed_log"))

    def on_chorus_preset(self, name, *, update_controls=True):
        if getattr(self, "_preset_programmatic", False):
            return
        bridge=getattr(self,'_effect_snapshots',None)
        if bridge is not None and update_controls:bridge.invalidate('chorus')
        if name == "Custom":
            if update_controls:
                self._recall_custom_preset("chorus")
            return
        if name == "Default":
            values = BMSYNTH_CHORUS_DEFAULT
            self._set_chorus_preset_controls(values)
            self._send_chorus_parameter_values(values)
            return

        try:
            preset_index = BMSYNTH_CHORUS_PRESETS.index(name)
        except ValueError:
            return
        if preset_index == 0 or preset_index >= len(ae.BMSYNTH_CHORUS_MACROS):
            return

        stream = ae.runtime.stream_handle
        if not stream:
            return
        if not update_controls:
            if not self.effect_events.send(
                MIDI_EVENT_CHORUS_MACRO, ae.BMSYNTH_CHORUS_MACROS[preset_index],
            ):
                self.append_log(log_text("runtime.chorus_preset_apply_failed"))
            return
        if bridge is not None and getattr(self.effect_events,'macro_submitter',None) is not None:
            receipt=bridge.submit('chorus',tuple(attr for attr,_event in CHORUS_PRESET_EVENTS),
                lambda completed:self.effect_events.submit_macro(MIDI_EVENT_CHORUS_MACRO,
                    ae.BMSYNTH_CHORUS_MACROS[preset_index],CHORUS_PRESET_EVENTS,on_complete=completed))
            if receipt is None or not receipt.accepted:self.append_log(log_text('runtime.chorus_preset_apply_failed'))
            return
        values = self.effect_events.apply_macro_and_read(
            MIDI_EVENT_CHORUS_MACRO,
            ae.BMSYNTH_CHORUS_MACROS[preset_index],
            CHORUS_PRESET_EVENTS,
        )
        if values is None:
            self.append_log(log_text("runtime.chorus_preset_apply_failed"))
            return

        self._set_chorus_preset_controls(values)

    def _install_effect_snapshot(self,group,receipt,values):
        if group=='master_volume':
            if values is not None and 'volume' in values:
                blocked=self.volume.blockSignals(True)
                try:
                    self.volume.setValue(values['volume'])
                    self.volume_text.setText(str(self.volume.value()))
                finally:self.volume.blockSignals(blocked)
            return # The shared stream-effect observer reports failures once.
        if group=='midi_mode':
            self.playback_alert.mode_indicator.setEnabled(True)
            if receipt.successful:
                self.append_log(log_text('midi_mode.switched',mode=tr('midi_mode.name_'+
                    self.playback_alert.mode_indicator.MODE_KEYS[receipt.results[0][2]])))
                self._refresh_channel_command_presentation()
            else:self._engine_effect_completed(receipt)
            return
        if group=='instrument':
            if not receipt.successful:self._engine_effect_completed(receipt)
            self._refresh_channel_command_presentation()
            return
        if group in ('master_fine','master_coarse','master_reset'):
            if values is not None:
                for field,value in values.items():getattr(self,field).set(value,False)
            if not receipt.successful:self._engine_effect_completed(receipt)
            return
        if group=='panic':
            if receipt.successful:self.append_log(log_text('runtime.midi_panic'))
            else:self._engine_effect_completed(receipt)
            return
        if group.startswith('mixer'):
            if not receipt.successful:self._engine_effect_completed(receipt)
            self._sync_monitor_mixer_buttons()
            return
        if group=='reset':
            if receipt.successful:
                self.append_log(log_text('runtime.midi_reset'))
                if self._performance_dialog is not None:self._performance_dialog.refresh_controls()
            else:self._engine_effect_completed(receipt)
            return
        if values is None:
            self.append_log(log_text('runtime.reverb_preset_apply_failed' if group=='reverb' else 'runtime.chorus_preset_apply_failed'))
            return
        previous=self._preset_programmatic;self._preset_programmatic=True
        try:
            for attr,value in values.items():getattr(self,attr).set(value,False)
        finally:self._preset_programmatic=previous
        combo=self.reverb_preset if group=='reverb' else self.chorus_preset
        if combo.currentText()=='Custom':self._remember_custom_preset(group)

    def _read_chorus_preset_values(self):
        return {
            attr: int(round(getattr(self, attr).get()))
            for attr, _event in CHORUS_PRESET_EVENTS
        }

    def _set_chorus_preset_controls(self, values):
        for attr, _event in CHORUS_PRESET_EVENTS:
            getattr(self, attr).set(values[attr], False)

    def _send_chorus_parameter_values(self, values):
        if not ae.runtime.stream_handle:
            return
        if not self.effect_events.send_batch(
            tuple((event, values[attr]) for attr, event in CHORUS_PRESET_EVENTS),
        ):
            self.append_log(log_text("runtime.chorus_preset_apply_failed"))

    def _on_chorus_parameter_changed(self, attr, value):
        if getattr(self, "_preset_programmatic", False):
            return
        bridge=getattr(self,'_effect_snapshots',None)
        if bridge is not None:bridge.touch(attr)
        self.effect_runtime.set_chorus_control(attr, int(round(value)))
        self.mark_custom(self.chorus_preset)

    def _on_delay_type_changed(self):
        if getattr(self, "_preset_programmatic", False):
            return
        self.on_delay()
        self.mark_custom(self.delay_preset)

    def mark_custom(self, combo):
        if getattr(self, "_preset_programmatic", False):
            return
        for attr, group in self._CUSTOM_PRESET_GROUPS.items():
            if getattr(self, attr, None) is combo:
                self._remember_custom_preset(group)
                break
        if combo.currentText() != "Custom":
            combo.blockSignals(True); combo.setCurrentText("Custom"); combo.blockSignals(False)

    def _custom_snapshot(self, group):
        if group == "equalizer":
            return self._read_equalizer_state().ini_values()
        snapshot = {row.attr: str(getattr(self, row.attr).get()) for row in EFFECT_ROWS[group]}
        if group == "delay":
            snapshot["delay_type"] = str(self.delay_type.currentIndex())
        return snapshot

    def _remember_custom_preset(self, group):
        if (not self._ui_settings_loading and not self._custom_restoring
                and not self._preset_programmatic):
            self._custom_presets[group] = self._custom_snapshot(group)

    def _load_custom_presets(self, values):
        stored = {key.removeprefix("custom_"): value for key, value in values.items()
                  if key.startswith("custom_")}
        for attr, group in self._CUSTOM_PRESET_GROUPS.items():
            current = self._custom_snapshot(group)
            snapshot = dict(current)
            try:
                if group == "equalizer" and "eq_band_count" in stored:
                    snapshot = EqualizerState.from_ini(stored, self._engine_preferences.sample_rate,
                        report=self._settings_read_report,prefix='custom_').ini_values()
                elif group != "equalizer" and any(key in stored for key in current):
                    for row in EFFECT_ROWS[group]:
                        if row.attr not in stored: continue
                        try:
                            number = row.validate(stored[row.attr])
                            snapshot[row.attr]=stored[row.attr]
                        except (TypeError,ValueError):
                            self._settings_read_report.record('UI','custom_'+row.attr,'invalid')
                    if group == "delay" and 'delay_type' in stored:
                        try:
                            if not 0<=int(stored['delay_type'])<self.delay_type.count(): raise ValueError()
                            snapshot['delay_type']=stored['delay_type']
                        except (TypeError,ValueError): self._settings_read_report.record('UI','custom_delay_type','invalid')
            except (KeyError, TypeError, ValueError):
                self._settings_read_report.record('UI','custom_eq_band_count' if group=='equalizer' else 'custom_'+group,'invalid')
                snapshot = dict(current)
            # Active values and the last manual snapshot are separate: an
            # external MIDI/SysEx update must not silently replace the latter.
            self._custom_presets[group] = snapshot

    def _recall_custom_preset(self, group):
        snapshot = self._custom_presets.get(group)
        if snapshot is None:
            return  # Controls are still being constructed; seeded after startup.
        previous = self._preset_programmatic
        self._preset_programmatic = True
        self._custom_restoring = True
        try:
            if group == "equalizer":
                restored = EqualizerState.from_ini(snapshot, self._engine_preferences.sample_rate)
                self._eq_bands = list(restored.bands)
                self.eq_output_trim.set(restored.output_trim_db, False)
                self.on_equalizer_band(0 if self._eq_bands else -1)
            else:
                for row in EFFECT_ROWS[group]:
                    getattr(self, row.attr).set(float(snapshot[row.attr]), False)
                if group == "delay":
                    blocked = self.delay_type.blockSignals(True)
                    self.delay_type.setCurrentIndex(int(snapshot["delay_type"]))
                    self.delay_type.blockSignals(blocked)
            if group == "equalizer":
                self._apply_equalizer()
            elif group == "delay":
                self.on_delay()
            elif group == "reverb":
                for row in EFFECT_ROWS[group]:
                    self.effect_runtime.set_reverb_control(row.attr, getattr(self, row.attr).get())
            else:
                for row in EFFECT_ROWS[group]:
                    self.effect_runtime.set_chorus_control(row.attr, int(round(getattr(self, row.attr).get())))
        finally:
            self._preset_programmatic = previous
            self._custom_restoring = False
        self._queue_ui_save()

    def reset_performance(self):
        self._effect_snapshots.invalidate('master_fine');self._effect_snapshots.invalidate('master_coarse')
        self.tuning_master.set(0,False);self.tuning_hz.set(tuning_hz(0),False);self.tuning_coarse.set(0,False)
        self._submit_master_edit('master_reset',(('master_fine',0),('master_coarse',0)),
            {'tuning_master':0,'tuning_hz':tuning_hz(0),'tuning_coarse':0})

    def request_midi_mode(self,mode):
        indicator=self.playback_alert.mode_indicator
        if ae.runtime.channel_command_executor is None:return
        if self._effect_snapshots.pending('midi_mode'):return
        indicator.setEnabled(False)
        try:
            receipt=self._effect_snapshots.submit_receipt('midi_mode',lambda completed:
                ae.runtime.submit_system_mode(mode,resetter=self.midi_input_runtime.reset,
                    restore_events=self.effect_runtime.mode_reset_events(self.capture_effect_state()),on_complete=completed))
            if receipt is None:indicator.setEnabled(True)
            elif receipt.completed:self._install_effect_snapshot('midi_mode',receipt,{})
            return receipt
        except BaseException:
            indicator.setEnabled(True)
            raise

    def panic(self):
        self.audio_session.stop_tuning_audition()
        player=getattr(self,'_midi_player',None)
        if player is not None and player.transport.stop_and_wait() is False:
            self.append_log(log_text('player.stop_failed',error=player.transport.error or tr('runtime.channel_executor_retained')))
            return
        return self._effect_snapshots.submit_receipt('panic',lambda completed:
            ae.runtime.submit_panic(on_complete=completed))

    def reset(self):
        self.audio_session.stop_tuning_audition()
        player = getattr(self, "_midi_player", None)
        if player is not None:
            if player.transport.stop_and_wait() is False:
                self.append_log(log_text('player.stop_failed',error=player.transport.error or tr('runtime.channel_executor_retained')))
                return
        if ae.runtime.channel_command_executor is not None:
            return self._effect_snapshots.submit_receipt('reset',lambda completed:
                ae.runtime.submit_manual_reset(self.midi_input_runtime.reset,on_complete=completed))
        live_controller.LiveController.reset(self)
        if self._performance_dialog is not None:self._performance_dialog.refresh_controls()
        self._queue_ui_save()

    def reset_channel(self, channel):
        """Clear one channel's MIDI state, mappings, notes and saved overlays."""
        if not 0 <= channel < ae.runtime.addresses.count:
            return
        ae.reset_channel_state(channel)
        _token,accepted=ae.runtime.stream_effects.channel_fx.reset_values(channel)
        if accepted is False:
            self.append_log(log_text('audio_fx.invalid'))
        if self._performance_dialog is not None:
            self._performance_dialog.refresh_controls()
            self._performance_dialog.refresh_live_monitor()
        self._queue_ui_save()

    def apply_delay_preset(self, name):
        if name == "Custom" and not self._preset_programmatic:
            self._recall_custom_preset("delay")
        else:
            super().apply_delay_preset(name)

    def _add_combo_row(self, layout, caption, attr, values, callback, *, include_custom=False, tooltip_key=None, trailing=None, initial=None):
        """Create one shared form-grid combo row; presets optionally add Custom."""
        row = QWidget()
        combo = AnimatedComboBox()
        options = (("Custom",) + tuple(values)) if include_custom else tuple(values)
        combo.addItems(options)
        if initial is not None:
            index=combo.findText(initial)
            if index<0:raise ValueError('preset initial identity')
            combo.setCurrentIndex(index)
        combo.currentTextChanged.connect(callback)
        trailing = trailing if trailing is not None else ElasticPanel(preferred_width=METRICS["form_value_width"])
        _form, title = install_three_column_form_row(
            row, caption, combo, trailing,
            label_width=METRICS["form_label_width"], gap=METRICS["form_column_gap"],
        )
        combo.setAccessibleName(caption)
        install_effect_tooltip(effect_tooltip(tooltip_key) if tooltip_key else None, row, title, combo, trailing)
        setattr(self, attr, combo)
        layout.addWidget(row)
        return combo

    def _add_preset_row(self, layout, caption, attr, values, callback, *, tooltip_key=None, trailing=None):
        return self._add_combo_row(layout, caption, attr, values, callback, include_custom=True,
                                   tooltip_key=tooltip_key, trailing=trailing,
                                   initial='Default' if self._CUSTOM_PRESET_GROUPS.get(attr) in EFFECT_FACTORY_VALUES else None)

    def _add_labeled_combo(self, layout, caption, attr, values, callback, *, tooltip_key=None):
        return self._add_combo_row(layout, caption, attr, values, callback, tooltip_key=tooltip_key)

    def paintEvent(self, event):
        QMainWindow.paintEvent(self, event)
        if (self._startup_devices_pending and not self._shutdown_started
                and not self.isMinimized()):
            self._startup_devices_pending = False
            QTimer.singleShot(0, self._start_initial_devices)

    def _start_initial_devices(self):
        if not self._shutdown_started:
            if self._startup_soundfont_pending:
                self._initial_devices_waiting_for_soundfonts = True
                if not self._soundfont_scan_started:
                    self._soundfont_scan_started = True
                    self._start_soundfont_discovery()
                return
            self.populate_devices()

    def showEvent(self, event):
        # native_stylesheet() owns palette propagation for this window.
        QMainWindow.showEvent(self, event)
        if self._shutdown_started:
            return
        first_show = not self._first_show_complete
        self._first_show_complete = True
        if not self._soundfont_scan_started:
            self._soundfont_scan_started = True
            QTimer.singleShot(0, self._start_soundfont_discovery)
        # At this point Qt has assigned the actual Windows screen/DPI.  The
        # polyphony grid cell must use the rendered caption's width in this
        # same coordinate system, not a pre-show DPI estimate.
        QTimer.singleShot(0, self._sync_polyphony_caption_width)
        if first_show:
            if not os.environ.get(ENV_UI_ONLY):
                from native_dialogs import NativeDriverSetupNotice
                self._driver_setup_notice=NativeDriverSetupNotice(self)
                QTimer.singleShot(0,self._driver_setup_notice.start)
            self._lock_main_vertical_priority()
            self._lock_side_top_rows()
            if self.midi_monitor_enabled.isChecked():
                self._monitor_transitioning = True
                self.channel_monitor_vertical_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
                self.channel_monitor_channel_scrollbar.hide()
                QTimer.singleShot(0, self._fit_monitor_window_for_activation)
            else:
                self._fit_effect_window(opening=True)
            self._window_placement.apply(self)  # Real frame margins/content fit, before first paint.
            self._window_placement.record(self)
            self._window_placement_ready = True
        self._schedule_display_rate_visuals()

    def moveEvent(self, event):
        QMainWindow.moveEvent(self, event)
        placement = getattr(self, '_window_placement', None)
        if placement is not None and getattr(self, '_window_placement_ready', False):
            placement.record(self)

    def hideEvent(self, event):
        # Close-to-tray and minimize-to-tray are presentation pauses, not
        # audio-session shutdowns. Rebind and seed a frame on the next show.
        QMainWindow.hideEvent(self, event)
        self._pause_presentation()

    def _sync_polyphony_caption_width(self):
        """Set a normal caption width without creating a compression floor."""
        if not hasattr(self, "polyphony_caption"):
            return
        metrics = QFontMetricsF(self.polyphony_caption.font())
        # horizontalAdvance is the real layout advance (not a guessed display
        # scale nor a tight glyph rectangle that could clip antialiasing).
        width = max(1, math.ceil(metrics.horizontalAdvance(self.polyphony_caption.text())))
        if width == getattr(self, "_state_poly_width", None):
            return
        self._state_poly_width = width
        self.polyphony_caption.set_preferred_width(width)
        self.polyphony.set_preferred_width(width)
        self._layout_main_sections()

    def build_ui(self):
        # The first addTab measures and shows its page synchronously. Install
        # the shared subcontrol geometry before that initial layout. Qt 6.11.2
        # Windows11's unstyled combo metric misreads pixel fonts as -1 points.
        self._apply_theme((self._dark,self._compact_mode,self._color_theme),building=True)
        root = QWidget(); self.setCentralWidget(root)
        root.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        layout = set_layout(QGridLayout(root), METRICS["outer"], METRICS["section_gap"])
        # A default Qt layout propagates its content minimum back to the
        # top-level window on show. The live surface must instead clip at a
        # user-chosen size, without adding a scroll pane or minimum floor.
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        # The right rail keeps its normal preferred width.  It has no minimum
        # width, so it yields only after the flexible main rail has done so.
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 0)
        layout.addWidget(self._build_live_main(), 0, 0)
        layout.addWidget(self._build_live_side(), 0, 1)

    @staticmethod
    def section_title(text):
        label = QLabel(text)
        label.setObjectName("CardTitle")
        return label

    @staticmethod
    def divider():
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setObjectName("Divider")
        return line

    @staticmethod
    def state_divider():
        """Status-card separators match the card outline in colour and weight."""
        line = NativeLiveWindow.divider()
        line.setObjectName("StateDivider")
        # QGrid otherwise gives the visual stroke a 3px layout cell.  The
        # card outline itself is a 1px stroke, so keep the separator at the
        # same actual weight rather than only reusing its colour token.
        line.setFrameShape(QFrame.NoFrame)
        line.setFixedWidth(1)
        return line

    @staticmethod
    def install_state_tooltip(text, *widgets):
        """Attach one concise measurement explanation to a state-cell group."""
        displayed_text = tooltip_display_text(text)
        for widget in widgets:
            widget.setToolTip(displayed_text)
            widget.setAccessibleDescription(text)

    def _add_top_device_field(self, grid, row, label_id, field, trailing=None):
        """Keep the three top rows on the same yielding form grid."""
        label = ElasticLabel(tr(label_id), preferred_width=METRICS["form_label_width"])
        label.setObjectName("FieldLabel")
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        trailing = trailing or ElasticPanel(preferred_width=METRICS["form_value_width"])
        make_horizontally_elastic(field)
        grid.addWidget(label, row, 0)
        grid.addWidget(field, row, 1)
        grid.addWidget(trailing, row, 2)
        self._top_row_cells.extend((label, field, trailing))
        return label, trailing

    def _build_live_main(self):
        # The live settings pane must always fit the minimum main-window size;
        # scrolling the primary control surface would hide active controls.
        viewport = TopClippedCanvas(); viewport.setObjectName("NativeMainViewport"); self.main_viewport = viewport
        body = QWidget(); body.setObjectName("NativeMainContent"); layout = set_layout(QGridLayout(body), 0, METRICS["section_gap"]); self.main_grid = layout
        layout.setColumnStretch(0, 1); layout.setRowStretch(3, 1); layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.soundfont_card = NativeCard(inset_role='compact-form'); grid = set_layout(QGridLayout(self.soundfont_card), METRICS["card_padding"], METRICS["form_column_gap"])
        grid.setVerticalSpacing(CARD_ROW_GAPS["device"])
        grid.setColumnStretch(1, 1)
        self._top_row_cells = []
        self.midi_device_combo = DeviceComboBox(self)
        self.midi_device_combo.addItem(MIDI_PORT_NAME)
        self.midi_device_combo.setAccessibleName(tr("main.midi_input"))
        # Current selected-tab state;not an old-format migration or port selector.
        self.midi_monitor_enabled = QAction(self)
        self.midi_monitor_enabled.setCheckable(True)
        # Runtime status mirror only. The visible port inventory belongs
        # to preferences; this never offered hardware/input selection.
        self.midi_device_combo.hide()
        self.midi_caption=QLabel(tr('main.midi_input'),self);self.midi_caption.hide()
        self.device_combo = DeviceComboBox()
        self.device_combo.set_category_provider(self._audio_device_category)
        self.device_combo.setAccessibleName(tr("main.audio_device"))
        self.output_settings_button = make_action_button(
            tr("main.audio_settings"), self.open_output_settings, object_name="CompactAction",
        )
        self.output_settings_button.setEnabled(False)
        self.device_combo.currentIndexChanged.connect(self._on_output_device_changed)
        self.audio_caption, self._audio_trailing = self._add_top_device_field(
            grid, 0, "main.audio_device", self.device_combo,
            self.output_settings_button,
        )
        self.soundfont_path = SoundFontComboBox()
        self.soundfont_selector = self.soundfont_path
        self.soundfont_path.setObjectName("SoundFontSelector")
        self.soundfont_path.setAccessibleName(tr("main.soundfont"))
        self.soundfont_path.activated.connect(self._on_soundfont_selector_activated)
        self.soundfont_path.favoriteToggled.connect(self._toggle_soundfont_favorite)
        self.soundfont_path.hideRequested.connect(self._hide_soundfont_path)
        favorites_cell = ElasticPanel(preferred_width=METRICS["form_value_width"])
        favorites_layout = set_layout(QHBoxLayout(favorites_cell), 0, 0)
        self.soundfont_favorites_only = NativeToggleSwitch(tr("soundfont_picker.favorites_only"))
        self.soundfont_favorites_only.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.soundfont_favorites_only.setToolTip(tr("soundfont_picker.favorites_only_tooltip"))
        self.soundfont_favorites_only.toggled.connect(self._on_soundfont_favorites_filter_changed)
        favorites_layout.addWidget(self.soundfont_favorites_only)
        self.soundfont_caption, _ = self._add_top_device_field(
            grid, 1, "main.soundfont", self.soundfont_path, favorites_cell,
        )
        drop_tip=tr('soundfont_picker.drop_tip')
        self.soundfont_card.setToolTip(tooltip_display_text(drop_tip))
        self.soundfont_caption.setToolTip(tooltip_display_text(drop_tip))
        self._soundfont_committed_path = ""
        self._populate_soundfont_selector(self._soundfont_committed_path)
        layout.addWidget(self.soundfont_card, 0, 0)
        self.state_card = NativeCard(compact=True); state_grid = set_layout(QGridLayout(self.state_card), METRICS["state_card_margin"], METRICS["state_column_gap"]); state_grid.setVerticalSpacing(METRICS["state_row_gap"])
        # This is only a pre-show provisional value.  showEvent replaces it
        # with QFontMetricsF from the actual target screen/DPI.
        self._state_poly_width = QFontMetricsF(native_text_font(METRICS["state_caption_font_px"])).horizontalAdvance(tr("main.polyphony"))
        self._state_poly_width = max(1, math.ceil(self._state_poly_width))
        self.state_grid = state_grid
        # POLYPHONY is the sole fixed-width state column.  Engine load and
        # status are proportional fields with no horizontal minimum.
        # The engine strip is intentionally more present than before.  Of the
        # flexible remainder it receives 3/8 (about 1.5x its former 1/4),
        # while status text receives the remaining 5/8.
        state_grid.setColumnStretch(2, 3); state_grid.setColumnStretch(4, 5)
        state_grid.setRowMinimumHeight(0, METRICS["state_caption_height"])
        state_grid.setRowMinimumHeight(1, METRICS["state_value_height"])
        # Its normal width is the actual caption advance, but unlike the old
        # fixed cell it has a 0px minimum and yields with every other field.
        self.polyphony = ElasticLabel("0", preferred_width=self._state_poly_width); self.polyphony.setObjectName("StatValue"); self.polyphony.setAlignment(Qt.AlignCenter)
        self.polyphony_caption = ElasticLabel(tr("main.polyphony"), preferred_width=self._state_poly_width); self.polyphony_caption.setObjectName("StatCaption"); self.polyphony_caption.setFixedHeight(METRICS["state_caption_height"]); self.polyphony_caption.setAlignment(Qt.AlignCenter)
        self.polyphony.setFixedHeight(METRICS["state_value_height"])
        self.install_state_tooltip(tr("state.polyphony.tooltip"), self.polyphony_caption, self.polyphony)
        state_grid.addWidget(self.polyphony_caption, 0, 0); state_grid.addWidget(self.polyphony, 1, 0)
        left_divider = self.state_divider(); left_divider.setFixedHeight(METRICS["state_content_height"]); state_grid.addWidget(left_divider, 0, 1, 2, 1)
        # ENGINE LOAD shares Polyphony's two-row grid exactly: caption/value
        # header first, then the bar centered in the value row.
        engine_header = ElasticPanel(); engine_header.setObjectName("EngineLoadHeader"); engine_header.setFixedHeight(METRICS["state_caption_height"]); engine_header.setMaximumWidth(METRICS["engine_load_max_width"]); engine_header.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed); header_layout = set_layout(QGridLayout(engine_header), 0, 0); header_layout.setSizeConstraint(QLayout.SetNoConstraint); header_layout.setColumnStretch(0, 1)
        self.engine_caption = ElasticLabel(tr("main.engine_load")); self.engine_caption.setObjectName("StatCaption"); self.engine_caption.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.load = NativeLoadBar()
        self.load_text = ElasticLabel("0.0 %"); self.load_text.setObjectName("EngineLoadValue"); self.load_text.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header_layout.addWidget(self.engine_caption, 0, 0); header_layout.addWidget(self.load_text, 0, 1)
        self.engine_load_row = ElasticPanel(); self.engine_load_row.setObjectName("EngineLoadValueRow"); self.engine_load_row.setFixedHeight(METRICS["state_value_height"]); self.engine_load_row.setMaximumWidth(METRICS["engine_load_max_width"]); self.engine_load_row.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed); load_row_layout = set_layout(QGridLayout(self.engine_load_row), 0, 0); load_row_layout.setSizeConstraint(QLayout.SetNoConstraint); load_row_layout.addWidget(self.load, 0, 0, Qt.AlignVCenter)
        self.install_state_tooltip(tr("state.engine_load.tooltip"), engine_header, self.engine_caption, self.load_text, self.engine_load_row, self.load)
        state_grid.addWidget(engine_header, 0, 2); state_grid.addWidget(self.engine_load_row, 1, 2)
        right_divider = self.state_divider(); right_divider.setFixedHeight(METRICS["state_content_height"]); state_grid.addWidget(right_divider, 0, 3, 2, 1)
        # The visualizer uses two signal-monitor regions:
        # left time-domain waveform, internal vertical divider, right
        # frequency-band response. It uses both yielding former-status cells.
        self.audio_visualizer = NativeAudioVisualizer(tr("state.waveform.tooltip"), tr("state.spectrum.tooltip")); self.audio_visualizer.set_theme(self._dark)
        state_grid.addWidget(self.audio_visualizer, 0, 4, 2, 2)
        layout.addWidget(self.state_card, 1, 0)
        self.performance_info_card = NativeCard(compact=True)
        info_grid = set_layout(QGridLayout(self.performance_info_card), METRICS["state_card_margin"], METRICS["state_column_gap"])
        info_grid.setVerticalSpacing(METRICS["state_row_gap"])
        self.performance_info_captions = {}
        self.performance_info_dividers = []
        self.performance_info_values = {}
        for position, key in enumerate(("source_rate", "output_rate", "backend", "channels", "note_jitter_mean_ms",
                                       "format", "output_format", "block_ms", "latency_ms", "note_jitter_max_ms")):
            row, column = (position // 5) * 2, (position % 5) * 2
            caption = ElasticLabel(tr(f"playback_info.{key}"))
            caption.setObjectName("StatCaption")
            caption.setFixedHeight(METRICS["state_caption_height"])
            caption.setAlignment(Qt.AlignCenter)
            value = ElasticLabel(tr("playback_info.unavailable"))
            value.setObjectName("StatValue")
            value.setFixedHeight(METRICS["state_value_height"])
            value.setAlignment(Qt.AlignCenter)
            self.install_state_tooltip(tr(f"playback_info.{key}_tooltip"), caption, value)
            info_grid.addWidget(caption, row, column)
            info_grid.addWidget(value, row + 1, column)
            info_grid.setColumnStretch(column, 1)
            self.performance_info_captions[key] = caption
            self.performance_info_values[key] = value
        for column in (1, 3, 5, 7):
            divider = self.state_divider()
            info_grid.addWidget(divider, 0, column, 4, 1)
            self.performance_info_dividers.append(divider)
        from native_widgets import NativeWarningLights
        self.playback_alert = NativeWarningLights()
        self.playback_alert.mode_indicator.modeRequested.connect(self.request_midi_mode)
        self.playback_alert.setObjectName("MutedLabel")
        layout.addWidget(self.performance_info_card, 2, 0)
        self.playback_warning_card=NativeCard(compact=True)
        warning_layout=set_layout(QVBoxLayout(self.playback_warning_card),METRICS['state_card_margin'],0)
        warning_layout.addWidget(self.playback_alert)
        layout.addWidget(self.playback_warning_card,3,0)
        self.effects_card = NativeCard(inset_role='compact-form'); effects_layout = set_layout(QVBoxLayout(self.effects_card), METRICS["card_padding"], 0)
        self.effect_stack = QStackedWidget()
        self.effect_stack.setMinimumSize(0, 0)
        self.tabs = make_parameter_tabs(self); self.tabs.setObjectName("EffectTabs")
        self._monitor_return_tab = 0
        self._build_live_tabs()
        self.effect_stack.addWidget(self.tabs)
        self.channel_monitor = self._build_channel_monitor()
        self.tabs.addTab(self.channel_monitor, tr("monitor.toggle"))
        self.tabs.currentChanged.connect(self._on_effect_tab_changed)
        effects_layout.addWidget(self.effect_stack)
        layout.addWidget(self.effects_card, 4, 0)
        self.midi_monitor_enabled.toggled.connect(self._switch_channel_monitor)
        QTimer.singleShot(0, self._lock_main_vertical_priority)
        # Restore the normal card title rhythm. The card itself?봭ot the title
        # area?봧s fixed to its content height; spare window height belongs to
        # the outer layout stretch below it.
        self.log_card = NativeCard(compact=True,inset_role='log'); self.log_card.setMinimumHeight(LAYOUT_DENSITIES[DEFAULT_LAYOUT_DENSITY]["log_min_height"])
        log_layout = set_layout(QVBoxLayout(self.log_card), METRICS["log_card_margin"], CARD_ROW_GAPS["log"]); self.log_title = self.section_title(tr("main.status_log")); self.log_title.setObjectName("StatusLogTitle"); log_layout.addWidget(self.log_title)
        self.log = QPlainTextEdit(); self.log.setObjectName("StatusLog"); self.log.setReadOnly(True); self.log.setMaximumBlockCount(1000); self.log.setMinimumHeight(LAYOUT_DENSITIES[DEFAULT_LAYOUT_DENSITY]["log_body_min_height"]); log_layout.addWidget(self.log, 1); layout.addWidget(self.log_card, 5, 0)
        self.log.setAccessibleName(tr('main.status_log'))
        self._sync_main_log_display()
        viewport.set_content(body)
        return viewport

    def _audio_device_category(self, row):
        identity = self.device_combo.itemData(row)
        if identity == -1:
            return ()
        backend = identity[0] if isinstance(identity, tuple) else "shared"
        return (tr(f"main.audio_category_{backend}"),)

    def add_output_device_row(self, device: OutputDevice) -> None:
        """Keep the BASS playback choice independent of Windows controls."""
        name = device.name or tr("runtime.default_device")
        label = tr("runtime.asio_device", name=name) if device.asio else (
            tr("runtime.wasapi_device", name=name) if device.wasapi else name
        )
        index = self.device_combo.count()
        self.device_combo.addItem(label, device.backend_id)

    def _sync_output_settings_button(self):
        selected = self.device_combo.currentData()
        button = self.output_settings_button
        button.setEnabled(selected is not None)
        if selected is None:
            return
        asio = isinstance(selected, tuple) and selected[:1] == ("asio",)
        label = tr("main.asio_settings" if asio else "main.audio_settings")
        tooltip = "main.asio_settings_tooltip" if asio else "main.audio_settings_tooltip"
        button.setText(label)
        button.setAccessibleName(label)
        button.setToolTip(tr(tooltip))

    def open_output_settings(self):
        selected = self.device_combo.currentData()
        if selected is None:
            return
        if isinstance(selected, tuple) and selected[:1] == ("asio",):
            self.open_asio_settings()
            return
        # WASAPI's exclusive permission and device format live in Windows
        # Playback > Properties > Advanced, not in a vendor control panel.
        try:
            open_sound_playback_devices()
        except (OSError, NotImplementedError) as exc:
            NativeMessageBox.warning(
                self, tr("main.audio_settings"),
                tr("error.audio_settings_open_failed", error=exc),
            )

    def _confirm_wasapi_selection(self) -> bool:
        if self._skip_wasapi_warning:
            return True
        dialog = NativeMessageBox(self)
        dialog.setIcon(QMessageBox.Warning)
        dialog.setWindowTitle(tr("main.wasapi_warning_title"))
        dialog.setText(tr("main.wasapi_warning"))
        dialog.setInformativeText(tr("main.wasapi_warning_detail"))
        proceed = dialog.addButton(tr("main.wasapi_use"), QMessageBox.AcceptRole)
        cancel = dialog.addButton(tr("common.cancel"), QMessageBox.RejectRole)
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        remember = OutlineCheckBox(tr("main.wasapi_dont_show_again"))
        remember.set_theme(widget_palette(self))
        dialog.setCheckBox(remember)
        dialog.exec()
        if dialog.clickedButton() is not proceed:
            return False
        if remember.isChecked():
            self._skip_wasapi_warning = True
        return True

    def _on_output_device_changed(self, index: int) -> None:
        if index < 0:
            return
        selected = self.device_combo.itemData(index)
        prior = self._last_confirmed_output_index
        prior_warning = self._skip_wasapi_warning
        if isinstance(selected, tuple) and selected[:1] == ("wasapi",):
            if not self._confirm_wasapi_selection():
                self.device_combo.blockSignals(True)
                self.device_combo.setCurrentIndex(prior if 0 <= prior < self.device_combo.count() else 0)
                self.device_combo.blockSignals(False)
                self._sync_output_settings_button()
                if prior < 0:
                    # Canceling the exclusive warning is not confirmation of
                    # the fallback. Startup can fail here as in a direct switch.
                    if self.init_bass() is True:
                        self._last_confirmed_output_index = self.device_combo.currentIndex()
                return
        self._sync_output_settings_button()
        try:
            retained=self.audio_session.switch_decode_output(selected,self._asio_dll,self._wasapi_dll,self._engine_preferences)
            activated=True if retained is True else self.init_bass(preserve_soundfonts=True)
        except (OSError,RuntimeError,audio_output.AudioStartError) as error:
            self.set_runtime_status(log_text("runtime.output_switch_failed",error=error))
            self.device_combo.blockSignals(True)
            self.device_combo.setCurrentIndex(prior if 0<=prior<self.device_combo.count() else 0)
            self.device_combo.blockSignals(False)
            self._sync_output_settings_button()
            return
        if activated is False:
            self._skip_wasapi_warning = prior_warning
            fallback = prior if 0 <= prior < self.device_combo.count() else 0
            self.device_combo.blockSignals(True)
            self.device_combo.setCurrentIndex(fallback)
            self.device_combo.blockSignals(False)
            self._sync_output_settings_button()
            self._last_confirmed_output_index = prior
            recovered=self.init_bass(preserve_soundfonts=True)
            if recovered is True:
                self._last_confirmed_output_index = fallback
                if isinstance(selected,tuple) and selected[:1]==('wasapi',):
                    self.append_log(log_text("runtime.wasapi_fallback"))
            return
        self._last_confirmed_output_index = index
        self._queue_ui_save()

    def open_asio_settings(self):
        selected = self.device_combo.currentData()
        if not (isinstance(selected, tuple) and selected[:1] == ("asio",) and self._asio_dll is not None):
            return False
        if not self.audio_session.asio_started:
            if self.init_bass() is False or not self.audio_session.asio_started:
                NativeMessageBox.warning(
                    self, tr("main.asio_settings"), tr("error.asio_settings_not_ready"),
                )
                return False
        dll = self._asio_dll
        if dll is None:
            return False
        # bassasio.h BOOL; the vendor panel is invoked by the GUI-thread action.
        # BASS_ASIO_THREAD already supplies the initialized driver's own HWND.
        try:
            if dll.BASS_ASIO_ControlPanel():
                return True
            error = dll.BASS_ASIO_ErrorGetCode()  # capture before another ASIO call
            if error == 14:  # bassasio.h BASS_ERROR_ALREADY: panel is already open
                return True
        except (OSError, RuntimeError) as exc:
            error = exc
        NativeMessageBox.warning(
            self, tr("main.asio_settings"), tr("error.asio_settings_open_failed", error=error),
        )
        return False

    def _lock_main_vertical_priority(self):
        """Capture preferred heights; the top-anchored stack owns placement."""
        self._sync_effect_card_height()
        self._main_preferred_heights = {
            "soundfont": self.soundfont_card.sizeHint().height(),
            "state": self.state_card.sizeHint().height(),
            "performance_info": self.performance_info_card.sizeHint().height(),
            "warnings": self.playback_warning_card.sizeHint().height(),
            "effects": self._effect_preferred_height,
        }
        for card in (self.soundfont_card, self.state_card, self.performance_info_card, self.playback_warning_card, self.effects_card, self.log_card):
            card.setMinimumHeight(0)
            card.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.main_grid.setEnabled(False)
        self.main_viewport.resize_callback = self._layout_main_sections
        self.main_viewport.lock_natural_width()
        self._layout_main_sections()

    def _layout_main_sections(self):
        """Lay out the declared main-card stack using the shared tail rule."""
        if not hasattr(self, "_main_preferred_heights"):
            return
        place_top_anchored_stack(
            self.main_viewport.content.width(),
            self.main_viewport.height(),
            METRICS["section_gap"],
            (
                StackItem(self.soundfont_card, self._main_preferred_heights["soundfont"]),
                StackItem(self.state_card, self._main_preferred_heights["state"]),
                StackItem(self.performance_info_card, self._main_preferred_heights["performance_info"]),
                StackItem(self.playback_warning_card,self._main_preferred_heights['warnings']),
                StackItem(self.effects_card, self._main_preferred_heights["effects"]),
                StackItem(self.log_card, 0, fill_tail=True),
            ),
        )

    def _log_required_height(self):
        # A preferred opening height,not a permanent floor on the tail card.
        # Title + gap + text body + inner margins + frame must all fit.
        layout=self.log_card.layout();margins=layout.contentsMargins()
        return max(LAYOUT_DENSITIES[self._layout_density]['log_min_height'],
            self.log_title.sizeHint().height()+layout.spacing()
            +LAYOUT_DENSITIES[self._layout_density]['log_body_min_height']
            +margins.top()+margins.bottom()+2*self.log_card.frameWidth())

    def _add_live_control(self, layout, attr, label, minimum, maximum, initial, callback, kind="value", step=1, *, tooltip_key=None, input_steps, fit_value_spacing=False, snap_navigation=False, preserve_value_precision=False):
        control = NativeControlRow(
            label, minimum, maximum, initial, callback,
            lambda value: format_value(int(round(value)) if step == 1 else value, kind), step,
            tooltip=effect_tooltip(tooltip_key or attr), input_steps=input_steps,
            fit_value_spacing=fit_value_spacing,
            snap_navigation=snap_navigation,
            preserve_value_precision=preserve_value_precision,
            entry_kind=kind,
        )
        setattr(self, attr, control); layout.addWidget(control)

    def _build_channel_monitor(self):
        control_rows = tuple((field,CHANNEL_PARAMETERS[field].reset_value(
            default_reverb=ae.runtime.channel_sends.default_reverb),CHANNEL_PARAMETERS[field].maximum)
            for field in ("reverb","chorus","pan","modulation","pitch"))
        knob_row_height = NativeKnobField.DIAL_READOUT_HEIGHT
        self._monitor_port = 0
        page = ElasticPage()
        page.setObjectName("ChannelMonitor")
        self._monitor_snapshot_frame = NativeMonitorTransitionFrame(page)
        outer = bordered_page_layout(QVBoxLayout(page),CARD_ROW_GAPS["effects"],compact=True)
        outer.setSizeConstraint(QLayout.SetNoConstraint)
        self.channel_monitor_port = AnimatedComboBox(page)
        self.channel_monitor_port.setAccessibleName(tr("monitor.port"))
        self.channel_monitor_port.addItem(tr("monitor.port_item", port=0), 0)
        self.channel_monitor_port.setMinimumWidth(0)
        self.channel_monitor_port.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.channel_monitor_port.hide()
        self.channel_monitor_port.currentIndexChanged.connect(self._select_monitor_port)
        outer.addWidget(self.channel_monitor_port)
        scroll = QScrollArea()
        scroll.setObjectName("ChannelMonitorScroll")
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidgetResizable(False)
        scroll.setMinimumSize(0, 0)
        scroll.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        body_widget = QWidget()
        body_widget.setObjectName("ChannelMonitorBody")
        self.channel_monitor_body = body_widget
        body = set_layout(QHBoxLayout(body_widget), 0, 4)
        axis = QWidget()
        axis.setObjectName("ChannelMonitorAxis")
        self.channel_monitor_axis = axis
        axis.setMaximumWidth(26)
        axis.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        axis_column = set_layout(QVBoxLayout(axis), (0, 6, 0, 0), 2)
        for _ in range(2):
            spacer = QWidget()
            spacer.setFixedHeight(18)
            axis_column.addWidget(spacer)
        self.channel_monitor_axis_labels = []
        for kind, _initial, _maximum in control_rows:
            label = NativeRotatedLabel(tr(f"monitor.{kind}_axis"),
                                       preferred_width=26, wrap=kind == "modulation",
                                       text_px=11, leading_edge=True)
            label.setFixedHeight(knob_row_height)
            label.set_theme(self._dark)
            axis_column.addWidget(label)
            self.channel_monitor_axis_labels.append(label)
        axis_column.addStretch(1)
        self.channel_monitor_global_edit = make_action_button(
            tr("monitor.global_edit"), lambda: self.open_monitor_channel_editor(None),
            object_name="ChannelMonitorGlobalEdit")
        self.channel_monitor_global_edit.setMinimumSize(0, 0)
        self.channel_monitor_global_edit.set_caption_rotation(-90)
        self.channel_monitor_global_edit.setAccessibleName(tr("monitor.global_edit"))
        self.channel_monitor_global_edit.setToolTip(tr("monitor.global_edit_tooltip"))
        axis_column.addWidget(self.channel_monitor_global_edit)
        body.addWidget(axis, 0, Qt.AlignTop)
        contents = QWidget()
        contents.setObjectName("ChannelMonitorContents")
        row = set_layout(QHBoxLayout(contents), 0, 3)
        self.channel_strips = []
        for channel in range(16):
            strip = NativeChannelStrip()
            strip.setObjectName("ChannelStrip")
            strip.setMinimumSize(0, 0)
            strip.setMaximumWidth(48)
            column = set_layout(QVBoxLayout(strip), (2, 6, 2, 6), 2)
            number = QLabel(tr("monitor.channel", number=channel + 1))
            number.setObjectName("ChannelStripTitle")
            number.setAlignment(Qt.AlignCenter)
            number.setFixedHeight(18)
            column.addWidget(number)
            program = QLabel(f"{128 if channel == 9 else 0:03d}:000")
            program.setObjectName("ChannelStripProgram")
            program.setAlignment(Qt.AlignCenter)
            program.setFixedHeight(18)
            program.setToolTip(tr("monitor.program_tooltip"))
            column.addWidget(program)
            knobs = {}
            values = {}
            for kind, initial, maximum in control_rows:
                row_widget = QWidget()
                row_widget.setObjectName("ChannelKnobRow")
                row_widget.setFixedHeight(knob_row_height)
                knob_row = set_layout(QGridLayout(row_widget), 0, 0)
                knob = NativeKnob(0, maximum, initial, compact=True)
                knob.set_accessible_domain(ae.CHANNEL_PARAMETERS[kind].entry_domain,
                    lambda value, kind=kind: self._format_monitor_value(kind,value))
                knob.setMaximumWidth(40)
                knob.setAccessibleName(tr("monitor.channel", number=channel + 1)
                                       + " " + tr(f"monitor.{kind}"))
                knob.setToolTip(tooltip_display_text(tr(f"monitor.{kind}_tooltip")))
                knob.valueChanged.connect(
                    lambda value, index=channel, control=kind:
                    self._set_monitor_channel_value(index, control, value))
                knob_row.addWidget(knob, 0, 0, Qt.AlignHCenter | Qt.AlignTop)
                value_label = EditableNumberLabel(self._format_monitor_value(kind, initial))
                domain = CHANNEL_PARAMETERS[kind].entry_domain
                value_label.configure(
                    minimum=domain.minimum, maximum=domain.maximum,
                    decimals=domain.decimals, format_input=domain.entry_text,
                    read=lambda knob=knob, domain=domain: domain.to_entry(knob.value()),
                    write=lambda number, index=channel, control=kind, domain=domain:
                    self._set_monitor_channel_value(index, control, domain.to_raw(number)),
                    label=knob.accessibleName(),
                )
                value_label.setObjectName("ChannelKnobValue")
                value_label.bind_control(knob)
                value_label.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
                value_label.setFixedHeight(QFontMetrics(native_text_font(METRICS['small_font_px'])).height())
                value_label.setMinimumWidth(0)
                value_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
                knob_row.addWidget(value_label, 0, 0, Qt.AlignBottom)
                column.addWidget(row_widget)
                knobs[kind] = knob
                values[kind] = value_label
            meter_row = set_layout(QHBoxLayout(), (2, 0, 2, 0), 1)
            fader = NativeSlider(Qt.Vertical)
            fader.setObjectName("ChannelMonitorFader")
            fader.setRange(0, 127)
            fader.setValue(100)
            fader.set_neutral_tick(100)
            fader.setFixedHeight(124)
            fader.setMinimumWidth(0)
            fader.setMaximumWidth(14)
            fader.setAccessibleName(tr("monitor.channel", number=channel + 1)
                                    + " " + tr("monitor.volume"))
            fader.setToolTip(tooltip_display_text(tr("monitor.volume_tooltip")))
            fader.valueChanged.connect(
                lambda value, index=channel:
                self._set_monitor_channel_value(index, "volume", value))
            meter_row.addWidget(fader, 0, Qt.AlignTop)
            volume_value = EditableNumberLabel("100")
            volume_domain = CHANNEL_PARAMETERS['volume'].entry_domain
            volume_value.configure(
                minimum=volume_domain.minimum, maximum=volume_domain.maximum,
                decimals=volume_domain.decimals, format_input=volume_domain.entry_text,
                read=lambda fader=fader, domain=volume_domain: domain.to_entry(fader.value()),
                write=lambda number, index=channel, domain=volume_domain:
                self._set_monitor_channel_value(index, "volume", domain.to_raw(number)),
                label=fader.accessibleName(),
            )
            volume_value.setObjectName("ChannelKnobValue")
            volume_value.bind_control(fader)
            volume_value.setAlignment(Qt.AlignCenter)
            volume_value.setFixedHeight(14)
            activity = NativeMidiActivityBar()
            activity.setMinimumWidth(0)
            activity.setMaximumWidth(6)
            activity.setFixedHeight(fader.height())
            activity.setToolTip(tooltip_display_text(tr("monitor.activity_tooltip")))
            activity.setAccessibleName(tr("monitor.channel", number=channel + 1)
                                       + " " + tr("monitor.activity_accessible"))
            meter_row.addWidget(activity, 0, Qt.AlignTop)
            instrument = NativeRotatedLabel(tr("monitor.no_instrument"),
                                            top_aligned=True, preferred_width=18,
                                            text_px=12, emphasis=True)
            instrument.setMaximumWidth(18)
            instrument.set_theme(self._dark)
            meter_row.addWidget(instrument)
            column.addLayout(meter_row, 1)
            column.addWidget(volume_value)
            switches=QWidget();switches.setMinimumWidth(0)
            switch_row=set_layout(QHBoxLayout(switches),0,2)
            mixer_buttons={}
            for kind in ('mute','solo'):
                button=make_action_button(tr('monitor.'+kind+'_button'),
                    lambda checked,index=channel,kind=kind:self._toggle_monitor_mixer(index,kind,checked),
                    checkable=True,object_name='ChannelMonitorAction',compact=True)
                button.setMinimumWidth(0)
                button.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
                button.setToolTip(tooltip_display_text(tr('monitor.'+kind+'_tip')))
                button.setAccessibleName(tr('monitor.'+kind+'_accessible',number=channel+1))
                switch_row.addWidget(button,1);mixer_buttons[kind]=button
            column.addWidget(switches)
            panic=make_action_button(tr('monitor.panic_button'),
                lambda _checked=False,index=channel:self._panic_monitor_channel(index),object_name='ChannelMonitorAction',compact=True)
            panic.setMinimumWidth(0)
            panic.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
            panic.setToolTip(tooltip_display_text(tr('monitor.panic_tip')))
            panic.setAccessibleName(tr('monitor.panic_accessible',number=channel+1))
            column.addWidget(panic)
            edit = make_action_button(tr("monitor.edit"),
                                      lambda _checked=False, index=channel:
                                      self.open_monitor_channel_editor(self._monitor_port * 16 + index),
                                      object_name="ChannelMonitorEdit",compact=True)
            edit.setToolTip(tooltip_display_text(tr("monitor.edit_tooltip", number=channel + 1)))
            edit.setMinimumWidth(0)
            column.addWidget(edit)
            row.addWidget(strip)
            self.channel_strips.append({"program": program, "knobs": knobs,
                                        "values": values, "volume": fader,
                                        "volume_value": volume_value,
                                        "activity": activity, "instrument": instrument,
                                        "edit": edit,"mute":mixer_buttons['mute'],"solo":mixer_buttons['solo'],"panic":panic})
        contents.adjustSize()
        self.channel_monitor_contents = contents
        self.channel_monitor_scroll = scroll
        scroll.setWidget(contents)
        scroll.setFixedHeight(contents.sizeHint().height())
        axis.setFixedHeight(scroll.height())
        body.addWidget(scroll, 1, Qt.AlignTop)
        vertical_scroll = QScrollArea()
        vertical_scroll.setObjectName("ChannelMonitorVerticalScroll")
        self.channel_monitor_vertical_scroll = vertical_scroll
        vertical_scroll.setFrameShape(QFrame.NoFrame)
        vertical_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        vertical_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        vertical_scroll.setWidgetResizable(True)
        vertical_scroll.setWidget(body_widget)
        vertical_scroll.setMinimumSize(0, 0)
        vertical_scroll.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        outer.addWidget(vertical_scroll, 1)
        # Own the scrollbar before changing visibility. A parentless QWidget
        # is a top-level window, so showing it here used to flash a tiny
        # "python" window before the main window was constructed.
        channel_scrollbar = QScrollBar(Qt.Horizontal, page)
        channel_scrollbar.setObjectName("ChannelMonitorChannelScroll")
        self.channel_monitor_channel_scrollbar = channel_scrollbar
        channel_scrollbar.setMinimumWidth(0)
        inner_scrollbar = scroll.horizontalScrollBar()
        inner_scrollbar.rangeChanged.connect(channel_scrollbar.setRange)
        inner_scrollbar.rangeChanged.connect(
            lambda _minimum, _maximum: channel_scrollbar.setPageStep(inner_scrollbar.pageStep()))
        inner_scrollbar.valueChanged.connect(channel_scrollbar.setValue)
        channel_scrollbar.valueChanged.connect(inner_scrollbar.setValue)
        channel_scrollbar.setRange(inner_scrollbar.minimum(), inner_scrollbar.maximum())
        channel_scrollbar.setPageStep(inner_scrollbar.pageStep())
        channel_scrollbar.setVisible(channel_scrollbar.maximum() > 0)
        inner_scrollbar.rangeChanged.connect(
            lambda _minimum, _maximum: self._sync_monitor_channel_scrollbar())
        outer.addWidget(channel_scrollbar)
        page.setMinimumSize(0, 0)
        return page

    def _switch_channel_monitor(self, enabled):
        # Settle pending growth before another tab acts on current geometry.
        self._monitor_window_motion.settle()
        self._sync_monitor_ports()
        self._effect_window_motion.settle()
        if not enabled:
            self._monitor_snapshot_frame.hide()
            self.channel_monitor_body.setUpdatesEnabled(True)
        if enabled and self.isVisible():
            self._monitor_transitioning = True
            self.channel_monitor_vertical_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.channel_monitor_channel_scrollbar.hide()
        blocked = self.tabs.blockSignals(True)
        if enabled:
            self.tabs.setCurrentWidget(self.channel_monitor)
        elif self.tabs.currentWidget() is self.channel_monitor:
            self.tabs.setCurrentIndex(self._monitor_return_tab)
        self.tabs.blockSignals(blocked)
        if enabled:
            self._fit_channel_monitor_content()
        self._sync_effect_card_height()
        if enabled:
            self._refresh_channel_monitor_values()
            self._refresh_channel_monitor_names()
            self._refresh_channel_monitor_activity()
            if self.isVisible():
                QTimer.singleShot(0, self._fit_monitor_window_for_activation)
        else:
            self._monitor_transitioning=False
            self._finish_monitor_transition()
            self._queue_effect_window_fit()

    def _sync_monitor_channel_scrollbar(self):
        if not self._monitor_transitioning:
            bar=self.channel_monitor_channel_scrollbar
            needed=self.channel_monitor_scroll.horizontalScrollBar().maximum()>0
            if needed != (not bar.isHidden()):
                bar.setVisible(needed)
                self._sync_effect_card_height()

    def _finish_monitor_transition(self):
        self._monitor_snapshot_frame.hide()
        self.channel_monitor_body.setUpdatesEnabled(True)
        self._monitor_transitioning = False
        self.channel_monitor_vertical_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._sync_monitor_channel_scrollbar()
        if self.isVisible() and not self.isMinimized() and not self._shutdown_started:
            self._update_display_rate_visuals()
        if (self.isVisible() and not self.isMinimized() and automatic_window_fit_allowed(self)
                and self.midi_monitor_enabled.isChecked()
                and QApplication.platformName() != "offscreen"):
            screen = self.screen()
            if screen is not None:
                available = screen.availableGeometry()
                frame = self.frameGeometry()
                shift_x = max(0, frame.right() - available.right())
                shift_y = max(0, frame.bottom() - available.bottom())
                if shift_x or shift_y:
                    self.move(max(available.left(), self.x() - shift_x),
                              max(available.top(), self.y() - shift_y))

    def _fit_channel_monitor_content(self):
        if (not self._shutdown_started and hasattr(self, "midi_monitor_enabled")
                and self.midi_monitor_enabled.isChecked()):
            content = self.channel_monitor_contents
            scroll = self.channel_monitor_scroll
            content.resize(content.sizeHint())
            # A QScrollArea viewport may be smaller than its own widget even
            # with NoFrame. Measure that styled chrome instead of hiding the
            # last pixels of the Edit row behind a guessed height allowance.
            chrome = max(0, scroll.height() - scroll.viewport().height())
            scroll.setFixedHeight(content.sizeHint().height() + chrome)
            # Match the card's exterior, not the individual Edit's6px inset
            # or the scroll area's potentially1px styled viewport chrome.
            self.channel_monitor_axis.setFixedHeight(
                content.mapTo(scroll, QPointF(0, content.height()).toPoint()).y())

    def _monitor_required_card_height(self):
        page_margins = self.channel_monitor.layout().contentsMargins()
        card_margins = self.effects_card.layout().contentsMargins()
        body_height = max(self.channel_monitor_scroll.height(),
                          self.channel_monitor_axis.sizeHint().height())
        port_height = (self.channel_monitor_port.sizeHint().height() + self.channel_monitor.layout().spacing()
                       if not self.channel_monitor_port.isHidden() else 0)
        bar=self.channel_monitor_channel_scrollbar
        channel_scroll_height=(bar.sizeHint().height()+self.channel_monitor.layout().spacing()
                               if not bar.isHidden() else 0)
        return (body_height + port_height + channel_scroll_height + page_margins.top() + page_margins.bottom()
                + card_margins.top() + card_margins.bottom()
                + 2 * self.effects_card.frameWidth()
                + self.tabs.tabBar().sizeHint().height() + 4)

    def _monitor_required_window_size(self):
        """Natural size for all 16 strips, full height and the status log.

        Every term is a measured layout/content extent. The returned size is
        a one-time opening target, not a top-level minimum or resize floor.
        """
        page_margins = self.channel_monitor.layout().contentsMargins()
        card_margins = self.effects_card.layout().contentsMargins()
        body_layout = self.channel_monitor_body.layout()
        body_margins = body_layout.contentsMargins()
        scroll = self.channel_monitor_scroll
        scroll_chrome = max(0, scroll.width() - scroll.viewport().width())
        needed_main_width = (
            self.channel_monitor_contents.sizeHint().width()
            + self.channel_monitor_axis.sizeHint().width() + body_layout.spacing()
            + body_margins.left() + body_margins.right() + scroll_chrome
            + page_margins.left() + page_margins.right()
            + card_margins.left() + card_margins.right()
            + 2 * self.effects_card.frameWidth()
            + max(0,self.tabs.width()-self.channel_monitor.width())
        )
        stack = getattr(self, "_main_preferred_heights", {})
        needed_main_height = (
            stack.get("soundfont", self.soundfont_card.sizeHint().height())
            + stack.get("state", self.state_card.sizeHint().height())
            + stack.get("performance_info", self.performance_info_card.sizeHint().height())
            + stack.get('warnings',self.playback_warning_card.sizeHint().height())
            + self._monitor_required_card_height()
            + self._log_required_height()
            + 5 * METRICS["section_gap"]
        )
        root_layout = self.centralWidget().layout()
        root_margins = root_layout.contentsMargins()
        window_width = (needed_main_width + self.side_rail.sizeHint().width()
                        + root_layout.horizontalSpacing()
                        + root_margins.left() + root_margins.right())
        window_height = (max(needed_main_height, self._side_required_height())
                         + root_margins.top() + root_margins.bottom())
        return QSize(window_width, window_height)

    def _fit_monitor_window_for_activation(self):
        if (self._shutdown_started or not self.isVisible()
                or not self.midi_monitor_enabled.isChecked()):
            return
        self._fit_channel_monitor_content()
        self._sync_effect_card_height()
        if not automatic_window_fit_allowed(self):
            # A refused geometry fit still completes page activation. No
            # animation will run to release snapshot/repaint/scroll/name gates.
            self._finish_monitor_transition()
            return
        required = self._monitor_required_window_size()
        target_width = max(self.width(), required.width())
        target_height = max(self.height(), required.height())
        screen = self.screen()
        if screen is not None and QApplication.platformName() != "offscreen":
            available = screen.availableGeometry()
            frame_width = max(0, self.frameGeometry().width() - self.width())
            frame_height = max(0, self.frameGeometry().height() - self.height())
            target_width = max(self.width(), min(target_width, available.width() - frame_width))
            target_height = max(self.height(), min(target_height, available.height() - frame_height))
        # Two auto bars can keep each other visible at the exact fit: the
        # vertical bar steals the horizontal 10px, and the channel bar steals
        # the vertical 14px. Resolve the no-bar geometry together, then put
        # the automatic vertical policy back for later user resizing.
        self._monitor_transitioning = True
        self.channel_monitor_vertical_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.channel_monitor_channel_scrollbar.hide()
        if (QApplication.platformName() != "offscreen"
                and QSize(target_width, target_height) != self.size()):
            self._monitor_snapshot_frame.capture(
                self.channel_monitor_axis, self.channel_monitor_contents,
                self.channel_monitor_body.layout().spacing(),
                self.channel_monitor.layout().contentsMargins().top(), self._dark)
            self.channel_monitor_body.setUpdatesEnabled(False)
        self._monitor_window_motion.start(QSize(target_width, target_height), expanding=True)

    def _set_monitor_channel_value(self, channel, kind, value):
        receipt=self.submit_channel_controls((self._monitor_port * 16 + channel,),kind,value)
        if not NativeChannelCommandPresentation.accepted(receipt):
            if NativeChannelCommandPresentation._get(receipt,'mode')=='legacy':self._refresh_channel_monitor_values()
            return
        strip = self.channel_strips[channel]
        strip.pop('_monitor_visual_key',None)
        if kind in strip["values"]:
            control = strip["knobs"][kind]
            if control.value() != value:
                blocked = control.blockSignals(True)
                control.setValue(value)
                control.blockSignals(blocked)
            strip["values"][kind].setText(self._format_monitor_value(kind, value))
        else:
            control = strip["volume"]
            if control.value() != value:
                blocked = control.blockSignals(True)
                control.setValue(value)
                control.blockSignals(blocked)
            strip["volume_value"].setText(str(value))

    def _toggle_monitor_mixer(self,channel,kind,checked):
        # Read visible intent on this port plus retained intent on other ports.
        options=ae.runtime.midi_options
        mutes=set(getattr(self,'_mixer_mutes',options.mixer_mutes))
        solos=set(getattr(self,'_mixer_solos',options.mixer_solos))
        target=mutes if kind=='mute' else solos;address=self._monitor_port*16+channel
        target.add(address) if checked else target.discard(address)
        receipt=self._effect_snapshots.submit_receipt('mixer',lambda done:
            ae.runtime.submit_mixer(mutes,solos,on_complete=done))
        if receipt is not None and receipt.accepted:
            self._mixer_mutes=frozenset(mutes);self._mixer_solos=frozenset(solos)
        self._sync_monitor_mixer_buttons()

    def _panic_monitor_channel(self,channel):
        address=self._monitor_port*16+channel
        return self._effect_snapshots.submit_receipt('mixer_panic',lambda done:
            ae.runtime.submit_mixer(panic_channel=address,on_complete=done))

    def _sync_monitor_mixer_buttons(self):
        options=ae.runtime.midi_options
        pending=hasattr(self,'_effect_snapshots') and self._effect_snapshots.pending('mixer')
        if not pending:
            self._mixer_mutes=options.mixer_mutes;self._mixer_solos=options.mixer_solos
        for index,strip in enumerate(self.channel_strips):
            address=self._monitor_port*16+index
            for kind,selected in (('mute',self._mixer_mutes),('solo',self._mixer_solos)):
                button=strip[kind];checked=address in selected
                if button.isChecked()!=checked:
                    old=button.blockSignals(True)
                    try:button.setChecked(checked)
                    finally:button.blockSignals(old)
            # These action names depend on address/locale,not audio frames.
            # Keep pending M/S state above live even when the names are stable.
            context=address,active_locale()
            if strip.get('_mixer_accessible_context')!=context:
                for kind in ('mute','solo','panic'):
                    strip[kind].setAccessibleName(tr('monitor.'+kind+'_accessible',number=address+1))
                strip['_mixer_accessible_context']=context

    @staticmethod
    def _format_monitor_value(kind, value):
        if kind=='pan' and value==128:return tr('channel_editor.random_pan_value')
        if kind == "pitch":
            offset = int(value) - 8192
            return f"+{offset}" if offset > 0 else f"−{-offset}" if offset < 0 else "0"
        return str(value)

    def _sync_monitor_ports(self):
        count = ae.runtime.addresses.count // 16
        names={row['slot']:row['name'] for row in self.midi_input_runtime.port_settings.ports}
        document=getattr(getattr(self,'_midi_player',None),'document',None)
        available=sorted(set(names)|ae.runtime.addresses.used_ports|set(document.ports if document else ()))
        available=[port for port in available if port<count]
        if not available:available=[0]
        signature=(count,tuple(names.items()),tuple(available))
        if getattr(self,'_monitor_port_signature',None)==signature:
            return
        self._monitor_port_signature=signature
        blocked = self.channel_monitor_port.blockSignals(True)
        self.channel_monitor_port.clear()
        for port in available:
            self.channel_monitor_port.addItem(names.get(port,tr("monitor.port_item", port=port)),port)
        if self._monitor_port not in available:self._monitor_port=available[0]
        self.channel_monitor_port.setCurrentIndex(self.channel_monitor_port.findData(self._monitor_port))
        self.channel_monitor_port.blockSignals(blocked)
        self.channel_monitor_port.setVisible(len(available)>1)

    def _select_monitor_port(self, _index):
        self._monitor_port = self.channel_monitor_port.currentData() or 0
        for strip in self.channel_strips:
            for label in (*strip["values"].values(), strip["volume_value"]):
                label.cancel_editor()
        self._refresh_channel_monitor_values()
        self._refresh_channel_monitor_names()
        self._refresh_channel_monitor_activity()

    def _refresh_channel_monitor_values(self):
        self._sync_monitor_ports()
        self._sync_monitor_mixer_buttons()
        snapshot = ae.channel_monitor_snapshot(self._monitor_port,with_readbacks=True)
        default_reverb=ae.runtime.channel_sends.default_reverb
        if getattr(self,'_monitor_neutral_default',None)!=default_reverb:
            self._monitor_neutral_default=default_reverb
            self._monitor_neutral_values={kind:CHANNEL_PARAMETERS[kind].reset_value(default_reverb=default_reverb)
                for kind in self.channel_strips[0]['knobs']} if self.channel_strips else {}
        neutral=self._monitor_neutral_values
        bridge=self.channel_command_presentation
        for index,(strip, values) in enumerate(zip(self.channel_strips, snapshot)):
            values=dict(values)
            readbacks=dict(values.get('_readbacks',{}))
            for field in (*strip['knobs'],'volume'):
                pending=bridge.pending_value(self._monitor_port*16+index,field)
                if pending is not None:
                    values[field]=pending;readbacks[field]=ae.ChannelReadback(pending,True,'requested')
            values['_readbacks']=readbacks
            key=(self._monitor_port,ae.runtime.stream_generation,ae.runtime.channel_policy.revision,default_reverb,
                 values['bank'],values['program'],tuple((field,values[field],
                     readbacks[field].known if field in readbacks else True) for field in (*strip['knobs'],'volume')))
            if strip.get('_monitor_visual_key')==key:continue
            strip['_monitor_visual_key']=key
            for kind, knob in strip["knobs"].items():
                if knob._neutral!=neutral[kind]:knob.set_neutral(neutral[kind])
                value = values[kind]
                readback=values.get('_readbacks',{}).get(kind)
                unknown=readback is not None and not readback.known
                label = strip["values"][kind]
                if bool(knob.property('unknownValue'))!=unknown:
                    knob.setProperty('unknownValue',unknown);knob.update();knob._notify_accessible_value()
                if bool(label.property('unknownValue'))!=unknown:label.setProperty('unknownValue',unknown)
                description=tr('channel_editor.unknown_hint') if unknown else ''
                if label.accessibleDescription()!=description:label.setAccessibleDescription(description)
                if unknown:
                    if label.text()!=tr('channel_editor.unknown_compact'):label.setText(tr('channel_editor.unknown_compact'))
                    continue
                if value > knob.maximum():
                    continue
                if knob.value() != value:
                    blocked = knob.blockSignals(True)
                    knob.setValue(value)
                    knob.blockSignals(blocked)
                label = strip["values"][kind]
                displayed = self._format_monitor_value(kind, value)
                if label.text() != displayed:
                    label.setText(displayed)
            volume = values["volume"]
            fader = strip["volume"]
            readback=values.get('_readbacks',{}).get('volume')
            unknown=readback is not None and not readback.known
            label=strip['volume_value']
            fader.set_unknown(unknown)
            if bool(label.property('unknownValue'))!=unknown:label.setProperty('unknownValue',unknown)
            description=tr('channel_editor.unknown_hint') if unknown else ''
            if label.accessibleDescription()!=description:label.setAccessibleDescription(description)
            if unknown:
                if label.text()!=tr('channel_editor.unknown_compact'):label.setText(tr('channel_editor.unknown_compact'))
            elif volume <= 127:
                if fader.value() != volume:
                    blocked = fader.blockSignals(True)
                    fader.setValue(volume)
                    fader.blockSignals(blocked)
                if strip["volume_value"].text() != str(volume):
                    strip["volume_value"].setText(str(volume))
            program = f'{values["bank"]:03d}:{values["program"]:03d}'
            if strip["program"].text() != program:
                strip["program"].setText(program)
        return snapshot

    def _refresh_channel_monitor_names(self):
        if not self._font_load_active and not self._monitor_transitioning:
            names = self.soundfont_runtime.active_channel_names()
            for index,strip in enumerate(self.channel_strips):
                channel=self._monitor_port*16+index
                name=names[channel] if channel<len(names) else None
                strip["instrument"].set_text(name or tr("monitor.no_instrument"))
            detail = getattr(self, "_performance_dialog", None)
            if detail is not None:
                detail.set_channel_names(names)

    @ae.activity_observation
    def _refresh_channel_monitor_activity(self):
        snapshot = ae.get_channel_midi_activity()
        visual=ae.get_channel_note_visual_state()
        for row,(strip,(held,_velocity,_onset)) in enumerate(zip(self.channel_strips,snapshot[self._monitor_port*16:])):
            strip["activity"].set_activity(visual[self._monitor_port*16+row]*127,held)
        return snapshot

    def _sync_effect_card_height(self, *_args):
        """The effect surface follows the selected control count, not a filler stretch."""
        if not hasattr(self, "effects_card") or not self.tabs.currentWidget(): return
        if (hasattr(self, "midi_monitor_enabled") and self.midi_monitor_enabled.isChecked()
                and hasattr(self, "channel_monitor")):
            self._effect_preferred_height = self._monitor_required_card_height()
            if hasattr(self, "_main_preferred_heights"):
                self._main_preferred_heights["effects"] = self._effect_preferred_height
                self._layout_main_sections()
            return
        page_height = self._effect_page_height(self.tabs.currentWidget().width())
        tabs_height = self.tabs.tabBar().sizeHint().height() + page_height + 4
        margins = self.effects_card.layout().contentsMargins()
        self._effect_preferred_height = margins.top() + margins.bottom() + tabs_height
        if hasattr(self, "_main_preferred_heights"):
            self._main_preferred_heights["effects"] = self._effect_preferred_height
            self._layout_main_sections()

    def _effect_page_height(self, width, *, full_disclosures=False, page=None):
        page = self.tabs.currentWidget() if page is None else page
        page_layout = page.layout()
        page_layout.invalidate()
        # Wrapped description/editor height depends on the current page's real
        # width, not QLabel's zero compression minimum or cached other pages.
        width_aware = page_layout.hasHeightForWidth()
        page_height = (page_layout.totalHeightForWidth(width)
                       if width_aware else page_layout.sizeHint().height())
        if full_disclosures:
            for disclosure in page.findChildren(NativeDisclosure):
                if disclosure._expanded and disclosure.isVisibleTo(page):
                    page_height += max(0, disclosure._natural_height - disclosure.height())
        return page_height

    def _on_effect_tab_changed(self, *_args):
        monitoring = self.tabs.currentWidget() is getattr(self, "channel_monitor", None)
        if not monitoring:
            self._monitor_return_tab = self.tabs.currentIndex()
        if self.midi_monitor_enabled.isChecked() != monitoring:
            self.midi_monitor_enabled.setChecked(monitoring)
            return  # One motion owner per monitor navigation, not two.
        self.midi_monitor_enabled.setChecked(monitoring)
        self._sync_effect_card_height()
        self._queue_effect_window_fit()

    def _queue_effect_window_fit(self, expanded=True):
        """Only explicit presentation actions grow a window, never resize events."""
        if (not expanded or self._effect_fit_pending or not self._first_show_complete
                or self._shutdown_started or not self.isVisible()
                or self.midi_monitor_enabled.isChecked()):
            return
        self._effect_fit_pending = True
        self._effect_fit_origin = self.size()
        QTimer.singleShot(0, self._flush_effect_window_fit)

    def _flush_effect_window_fit(self):
        self._effect_fit_pending = False
        if (self.size() != self._effect_fit_origin
                and self._effect_window_motion._started_at is None):
            return  # A later user resize takes precedence over a queued fit.
        self._fit_effect_window()

    def _effect_required_window_size(self, *, opening=False, window_width=None):
        """Measure a fit target, not a persistent minimum or resize constraint."""
        page = self.tabs.currentWidget()
        if page is self.channel_monitor:
            page = self.tabs.widget(self._monitor_return_tab)
        card_margins = self.effects_card.layout().contentsMargins()
        root = self.centralWidget().layout()
        margins = root.contentsMargins()
        window_chrome = (self.side_rail.sizeHint().width() + root.horizontalSpacing()
                         + margins.left() + margins.right())
        card_chrome = (card_margins.left() + card_margins.right()
                       + 2 * self.effects_card.frameWidth())
        # A hidden page can retain Qt's100px bootstrap geometry. Measure the
        # styled pane, never subtract a hidden child's unrelated width.
        option = QStyleOptionTabWidgetFrame()
        self.tabs.initStyleOption(option)
        pane = self.tabs.style().subElementRect(QStyle.SE_TabWidgetTabContents, option, self.tabs)
        pane_chrome = max(0, self.tabs.width() - pane.width())
        tab_width = self.tabs.tabBar().sizeHint().width()
        main_width = max(tab_width + card_chrome,
                         max(self.tabs.widget(index).layout().sizeHint().width()
                             for index in range(self.tabs.count())
                             if self.tabs.widget(index) is not self.channel_monitor) + pane_chrome + card_chrome,
                         self.soundfont_card.sizeHint().width(), self.state_card.sizeHint().width())
        width = main_width + window_chrome
        if not opening:
            width = max(width, self.width())
        if window_width is not None:
            width = window_width
        page_width = max(0, width - window_chrome - card_chrome - pane_chrome)
        # Reserve the full end extent once, before an explicit disclosure's
        # animation starts. Repeated intermediate heightChanged frames only
        # lay out cards; they must not restart a window animation every frame.
        page_height = self._effect_page_height(page_width, full_disclosures=True, page=page)
        effect_height = (card_margins.top() + card_margins.bottom()
                         + self.tabs.tabBar().sizeHint().height() + page_height + 4)
        stack = self._main_preferred_heights
        main_height = (stack["soundfont"] + stack["state"] + stack["performance_info"] + stack['warnings'] + effect_height
                       + self._log_required_height()
                       + 5 * METRICS["section_gap"])
        # The meter is flexible, but its master-fader title, track and value
        # still need the actual preferred vertical extent to remain usable.
        side_height = self._side_required_height()
        return QSize(width, max(main_height, side_height) + margins.top() + margins.bottom())

    def _side_required_height(self):
        margins = self.monitor_card.layout().contentsMargins()
        monitor_height = max(self.meter.sizeHint().height(), self.master_fader.layout().sizeHint().height())
        return (sum(self._side_preferred_heights.values()) + monitor_height
                + margins.top() + margins.bottom() + 2 * self.monitor_card.frameWidth()
                + 2 * METRICS["section_gap"])

    def _effect_window_fit_target(self, *, opening=False):
        required = self._effect_required_window_size(opening=opening)
        target = required if opening else self.size().expandedTo(required)
        if self.screen() is not None and QApplication.platformName() != "offscreen":
            available = self.screen().availableGeometry().size()
            frame = self.frameGeometry().size() - self.size()
            target = target.boundedTo(available - frame)
            if not opening:
                target = target.expandedTo(self.size())
            # Wrapped descriptions must be measured at the screen-limited
            # width, not at an unattainable natural width.
            measured = self._effect_required_window_size(opening=opening, window_width=target.width())
            target.setHeight(min(measured.height(), available.height() - frame.height())
                             if opening else max(self.height(), min(measured.height(),
                                 available.height() - frame.height())))
        return target

    def _fit_effect_window(self, *, opening=False):
        if (self._shutdown_started or not self.isVisible()
                or self.midi_monitor_enabled.isChecked()
                or not automatic_window_fit_allowed(self)):
            return
        target = self._effect_window_fit_target(opening=opening)
        if opening:
            self.resize(target)
        elif target != self.size():
            self._effect_window_motion.start(target, expanding=True)
        self.centralWidget().layout().activate()
        self._sync_effect_card_height()

    def _new_effect_page(self):
        page = ElasticPage()
        page.setMinimumSize(0, 0)
        page.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        layout = bordered_page_layout(QVBoxLayout(page),CARD_ROW_GAPS["effects"],compact=True)
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        self._tab_page_layouts.append(layout)
        return page, layout

    def _finish_effect_page(self, page, layout, tab_id):
        page.setProperty("effectTabId", tab_id)
        layout.addStretch(1)
        self.tabs.addTab(page, tr(tab_id))

    def _add_effect_rows(self, layout, rows, callback_for):
        for row in rows:
            self._add_live_control(
                layout, row.attr, tr(row.label_id), row.minimum, row.maximum,
                row.initial, callback_for(row), row.kind, row.step,
                input_steps=row.input_steps,
                fit_value_spacing=row.fit_value_spacing,
                snap_navigation=row.snap_navigation,
                preserve_value_precision=row.preserve_value_precision,
            )

    def _make_effect_toggle(self, attr, label_id, callback):
        toggle = NativeToggleSwitch(tr(label_id))
        toggle.toggled.connect(callback)
        install_effect_tooltip(effect_tooltip(attr), toggle)
        setattr(self, attr, toggle)
        return toggle

    def _add_effect_toggle_row(self, layout, *leading, expander=None):
        row = make_horizontally_elastic(QWidget())
        grid = set_layout(QGridLayout(row), 0, METRICS["form_column_gap"])
        for column, toggle in enumerate(leading):
            grid.addWidget(toggle, 0, column)
        grid.setColumnStretch(len(leading), 1)
        if expander is not None:
            grid.addWidget(expander, 0, len(leading) + 1, Qt.AlignRight)
        row.setFixedHeight(FORM_ROW_HEIGHT)
        layout.addWidget(row)

    def _on_reverb_slider_change(self, control_name, value):
        bridge=getattr(self,'_effect_snapshots',None)
        if bridge is not None:bridge.touch(control_name)
        self.effect_runtime.set_reverb_control(control_name, value)
        self.mark_custom(self.reverb_preset)

    def _on_delay_slider_change(self, _value):
        self.on_delay()
        self.mark_custom(self.delay_preset)

    def _build_live_tabs(self):
        self._tab_page_layouts = []
        self.effect_preview_buttons={}
        page, layout = self._new_effect_page()
        self._add_preset_row(
            layout, tr("common.preset"), "reverb_preset",
            ["Default", "Off"] + [preset[0] for preset in REVERB_PRESETS[1:]],
            self.apply_reverb_preset, tooltip_key="reverb_preset",trailing=self._effect_preview_cell('reverb'),
        )
        self._add_effect_rows(
            layout, EFFECT_ROWS["reverb"],
            lambda row: lambda value, control_name=row.attr: self._on_reverb_slider_change(control_name, value),
        )
        self._add_effect_rows(layout, (row for row in EFFECT_ROWS["other"] if row.attr == "global_reverb"),
                              lambda _row: self.on_global_reverb)
        self._finish_effect_page(page, layout, "tab.reverb")

        page, layout = self._new_effect_page()
        self._add_preset_row(
            layout, tr("common.preset"), "chorus_preset",
            BMSYNTH_CHORUS_PRESETS, self.on_chorus_preset,
            tooltip_key="chorus_preset",trailing=self._effect_preview_cell('chorus'),
        )
        self._add_effect_rows(
            layout, EFFECT_ROWS["chorus"],
            lambda row: lambda value, attr=row.attr: self._on_chorus_parameter_changed(attr, value),
        )
        self._add_effect_rows(layout, (row for row in EFFECT_ROWS["other"] if row.attr == "global_chorus"),
                              lambda _row: self.on_global_chorus)
        self._finish_effect_page(page, layout, "tab.chorus")

        page, layout = self._new_effect_page()
        self._add_preset_row(
            layout, tr("common.preset"), "delay_preset",
            tuple(BMSYNTH_DELAY_PRESETS), self.apply_delay_preset,
            tooltip_key="delay_preset",trailing=self._effect_preview_cell('delay'),
        )
        self._add_labeled_combo(
            layout, tr("common.type"), "delay_type", delay_type_labels(),
            lambda _name: self._on_delay_type_changed(), tooltip_key="delay_type",
        )
        self.delay_type.blockSignals(True)
        self.delay_type.setCurrentIndex(EFFECT_FACTORY_VALUES['delay']['delay_type'])
        self.delay_type.blockSignals(False)
        self._add_effect_rows(layout, EFFECT_ROWS["delay"], lambda _row: self._on_delay_slider_change)
        self._add_effect_toggle_row(
            layout,
            self._make_effect_toggle(
                "delay_sysex_enabled", "effect.delay_sysex_enabled", self.effect_runtime.set_delay_sysex,
            ),
        )
        from native_widgets import make_control_section
        self.extended_delay_section,_grid=make_control_section(tr('effect.extra_delay_details_expanded'),effect_tooltip('extra_delay_details_expanded'),collapsible=True)
        self.extra_delay_details_expanded=self.extended_delay_section.disclosure_button
        self.extra_delay_details_expanded.setChecked(False)
        self.extended_delay_details=self.extended_delay_section.disclosure
        self.extended_delay_details.heightChanged.connect(self._sync_effect_card_height)
        self.extended_delay_details.expansionChanged.connect(self._queue_effect_window_fit)
        self._add_effect_rows(self.extended_delay_details.content_layout, EFFECT_ROWS["extended_delay"],
                              lambda _row: self.on_extended_delay)
        self.extended_delay_details.content_layout.addWidget(self._make_effect_toggle(
            "extra_delay_ping_pong", "effect.extra_delay_ping_pong", self.on_extended_delay,
        ))
        layout.addWidget(self.extended_delay_section)
        self._sync_extended_delay_controls()
        self._finish_effect_page(page, layout, "tab.delay")

        page, layout = self._new_effect_page()
        self._eq_bands = list(EqualizerState().validated(self._engine_preferences.sample_rate).bands)
        self._eq_selected = 0
        self.eq_graph = NativeEQResponse()
        layout.addWidget(self.eq_graph)
        self._eq_graph_timer = QTimer(self)
        self._eq_graph_timer.setSingleShot(True)
        self._eq_graph_timer.timeout.connect(self._poll_equalizer_graph)
        center_limit = min(16000, self._engine_preferences.sample_rate // 3)
        self.eq_graph.set_center_range(80, center_limit)
        self._add_preset_row(
            layout, tr("common.preset"), "eq_preset",
            tuple(tr(f"eq.preset_{identity}") for identity in EQ_PRESETS),
            self.apply_equalizer_preset,trailing=self._effect_preview_cell('equalizer'),
        )
        for index, identity in enumerate(("custom", *EQ_PRESETS)):
            self.eq_preset.setItemData(index, identity)
        blocked = self.eq_preset.blockSignals(True)
        self.eq_preset.setCurrentIndex(self.eq_preset.findData("off"))
        self.eq_preset.blockSignals(blocked)
        install_effect_tooltip(effect_tooltip("eq_preset"), self.eq_preset,
                               self.eq_preset.parentWidget().layout().itemAtPosition(0, 0).widget())
        self._add_labeled_combo(
            layout, tr("eq.filter_kind"), "eq_filter",
            tuple(tr(f"eq.filter_{kind}") for kind in EQ_FILTER_KINDS),
            self.on_equalizer_filter, tooltip_key="eq_filter",
        )
        for index, kind in enumerate(EQ_FILTER_KINDS):
            self.eq_filter.setItemData(index, kind)
        for row in EFFECT_ROWS["equalizer"]:
            control = NativeControlRow(
                tr(row.label_id), row.minimum, center_limit if row.attr == "eq_center" else row.maximum,
                row.initial, self.on_equalizer, lambda value, kind=row.kind: format_value(value, kind),
                step=row.step, input_steps=row.input_steps,
                logarithmic=row.attr == "eq_center", preserve_value_precision=True,
                tooltip=effect_tooltip(row.attr), entry_kind=row.kind,
                entry_context=lambda attr=row.attr: self._eq_selected if attr != "eq_output_trim" else None)
            setattr(self, row.attr, control)
            layout.addWidget(control)
        self.eq_graph.bandSelected.connect(self.on_equalizer_band)
        self.eq_graph.bandAdded.connect(self.add_equalizer_band)
        self.eq_graph.bandRemoved.connect(self.remove_equalizer_band)
        self.eq_graph.bandEdited.connect(self.edit_equalizer_band)
        self.on_equalizer_band(0)
        self._finish_effect_page(page, layout, "tab.equalizer")

        page, layout = self._new_effect_page()
        other_callbacks = {
            "stereo": self.on_stereo,
            "dx_level": lambda _value: self.on_dx(),
            "spatial_input_gain": lambda _value: self.on_dx(),
            "spatial_time": lambda _value: self.on_dx(),
            "spatial_hf_ratio": lambda _value: self.on_dx(),
        }
        self._add_effect_rows(
            layout, (row for row in EFFECT_ROWS["other"] if row.attr in ("stereo", "dx_level")),
            lambda row: other_callbacks[row.attr],
        )
        spatial_preview=make_effect_audition(self,'other',self.stereo)
        spatial_preview.setToolTip(tr('instrument_audition.effect_help'))
        self.effect_preview_buttons['other']=spatial_preview
        self.stereo.add_value_action(spatial_preview)
        from native_widgets import make_control_section
        self.spatial_section,_grid=make_control_section(tr('effect.spatial_details_expanded'),effect_tooltip('spatial_details_expanded'),collapsible=True)
        self.spatial_details_expanded=self.spatial_section.disclosure_button
        self.spatial_details_expanded.setChecked(False)
        self.spatial_details=self.spatial_section.disclosure
        self.spatial_details.heightChanged.connect(self._sync_effect_card_height)
        self.spatial_details.expansionChanged.connect(self._queue_effect_window_fit)
        from native_dialogs import sample_rate_caption
        rate=self._engine_preferences.sample_rate
        combo=self._add_combo_row(self.spatial_details.content_layout,
            tr('effect.spatial_sample_rate'),'spatial_sample_rate',(),lambda _text:None,
            tooltip_key='spatial_sample_rate')
        combo.addItem(tr('effect.spatial_sample_rate_default',rate=sample_rate_caption(rate)),0)
        for value in SAMPLE_RATES:
            if value<=rate:combo.addItem(sample_rate_caption(value),value)
        combo.currentIndexChanged.connect(lambda _index:self.on_dx())
        self._add_effect_rows(
            self.spatial_details.content_layout,
            (row for row in EFFECT_ROWS["other"] if row.attr in SPATIAL_DETAIL_NAMES),
            lambda row: other_callbacks[row.attr],
        )
        layout.addWidget(self.spatial_section)
        self._sync_spatial_details()
        self._finish_effect_page(page, layout, "tab.other")

        page, layout = self._new_effect_page()
        dynamics_preview=make_effect_audition(self,'dynamics',page)
        dynamics_preview.setToolTip(tr('instrument_audition.effect_help'))
        self.effect_preview_buttons['dynamics']=dynamics_preview
        self._add_effect_toggle_row(layout,self._make_effect_toggle(
            "compressor_enabled", "effect.compressor_enabled",
            lambda _checked: self.on_compressor(),
        ),expander=dynamics_preview)
        self._add_effect_rows(
            layout, EFFECT_ROWS["dynamics"][:5],
            lambda _row: lambda _value: self.on_compressor(),
        )
        layout.addWidget(self._make_effect_toggle(
            "limiter_enabled", "effect.limiter_enabled",
            lambda _checked: self.on_limiter(),
        ))
        self._add_effect_rows(
            layout, EFFECT_ROWS["dynamics"][5:],
            lambda _row: lambda _value: self.on_limiter(),
        )
        for row in EFFECT_ROWS["dynamics"]:
            getattr(self, row.attr).setEnabled(False)
        self._finish_effect_page(page, layout, "tab.dynamics")

        page, layout = self._new_effect_page()
        method_row = QWidget()
        reset_cell = ElasticPanel(preferred_width=METRICS["form_label_width"])
        reset_layout = set_layout(QHBoxLayout(reset_cell), 0, 0)
        self.performance_reset = make_action_button(tr("performance.reset"), self.reset_performance)
        self.performance_reset.setEnabled(ae.runtime.performance.has_master_overrides())
        reset_layout.addStretch(1)
        reset_layout.addWidget(self.performance_reset)
        method_cell = ElasticPanel(preferred_width=METRICS["form_value_width"])
        method_layout = set_layout(QHBoxLayout(method_cell), 0, 0)
        self.tuning_methods = NativeSegmentedChoice(
            (tr("performance.cent_method"), tr("performance.hz_method")))
        self.tuning_methods.setAccessibleName(tr("performance.method"))
        method_layout.addWidget(self.tuning_methods)
        method_layout.addStretch(1)
        method_grid, unused_title = install_three_column_form_row(
            method_row, "", ElasticPanel(), method_cell,
            label_width=METRICS["form_label_width"], gap=METRICS["form_column_gap"],
        )
        method_grid.removeWidget(unused_title)
        unused_title.deleteLater()
        method_grid.addWidget(reset_cell, 0, 0)
        install_effect_tooltip(tr("performance.reset_tooltip"), self.performance_reset)
        install_effect_tooltip(tr("tooltip.performance.method"),
                               method_row, self.tuning_methods,
                               *self.tuning_methods.buttons)
        audition_cell=QWidget();audition_layout=set_layout(QHBoxLayout(audition_cell),0,4)
        self.master_tuning_preview=make_tuning_audition(self,self.reference_tuning_request,audition_cell,note='A4')
        self.master_tuning_preview.setToolTip(tr('tuning_preview.master_help'))
        audition_layout.addStretch(1);audition_layout.addWidget(self.master_tuning_preview)
        unused=method_grid.itemAtPosition(0,1).widget();unused.hide()
        method_grid.removeWidget(method_cell);method_grid.removeWidget(unused);unused.deleteLater()
        method_grid.addWidget(method_cell,0,1)
        method_grid.addWidget(audition_cell,0,2)
        layout.addWidget(method_row)
        self.tuning_stack = QStackedWidget()
        self.tuning_stack.setMinimumWidth(0)
        self.tuning_stack.setFixedHeight(FORM_ROW_HEIGHT)
        self.tuning_stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._add_effect_rows(self.tuning_stack, EFFECT_ROWS["performance"][:2],
            lambda row: lambda value: self.on_master_tuning(value, hz=row.attr == "tuning_hz"))
        self.tuning_methods.selected.connect(
            lambda index: self.set_tuning_method("hz" if index else "cents"))
        self.set_tuning_method("cents")
        layout.addWidget(self.tuning_stack)
        self._add_effect_rows(layout, EFFECT_ROWS["performance"][2:],
            lambda _row: self.on_master_coarse)
        self._add_effect_rows(layout, (row for row in EFFECT_ROWS["other"] if row.attr == "velocity"),
                              lambda _row: lambda _value: self.on_velocity())
        self._add_effect_rows(layout,EFFECT_ROWS['humanization'][:1],lambda _row:lambda _value:self._on_humanization())
        toggle=self._make_effect_toggle('humanize_preserve_simultaneity','performance.humanize_preserve_simultaneity',lambda _value:self._on_humanization())
        toggle.setChecked(True)
        self._add_effect_rows(layout,EFFECT_ROWS['humanization'][1:],lambda _row:lambda _value:self._on_humanization())
        self._add_effect_toggle_row(layout,
            self._make_effect_toggle("overnote", "effect.overnote", self.effect_runtime.set_overnote),
            toggle)
        self._performance_settings = None
        self._finish_effect_page(page, layout, "tab.performance")
        page, layout = self._new_effect_page()
        self.audio_fx_panel = NativeAudioFXPanel(self)
        layout.addWidget(self.audio_fx_panel)
        self._finish_effect_page(page, layout, "audio_fx.title")
        self.tabs.tabBar().moveTab(5, 7)
        self.tabs.setTabToolTip(4, tr("tab.other_help"))
        self.tabs.setTabToolTip(5, tr("tab.performance_help"))
        self.tabs.setTabToolTip(6, tr("tab.output_help"))
        self.tabs.setTabToolTip(7, tr("tab.dynamics_help"))
        self._sync_effect_label_widths()

    def _effect_preview_cell(self,effect):
        cell=ElasticPanel(preferred_width=METRICS['form_value_width'])
        row=set_layout(QHBoxLayout(cell),0,0);row.addStretch(1)
        button=make_effect_audition(self,effect,cell);row.addWidget(button)
        button.setToolTip(tr('instrument_audition.effect_help'))
        self.effect_preview_buttons[effect]=button
        return cell

    def _on_humanization(self):
        if not hasattr(self,'humanize_velocity'):return
        self.effect_runtime.set_humanization(self.humanize_timing.get(),self.humanize_velocity.get(),self.humanize_preserve_simultaneity.isChecked())

    def _sync_effect_label_widths(self):
        if not hasattr(self, "performance_reset"):
            return
        # English parameter captions can be wider than the former Korean copy.
        # Measure ONE preferred caption column for all8 pages; it is not a
        # minimum, and sliders still yield to0 under user compression.
        labels = [label for index in range(self.tabs.count())
                  for label in self.tabs.widget(index).findChildren(ElasticLabel)
                  if label.objectName() == "FieldLabel"]
        for label in labels:
            label.ensurePolished()
        caption_width = max([METRICS["form_label_width"],
                             *(QFontMetrics(label.font()).horizontalAdvance(label.text()) for label in labels)])
        for label in labels:
            label.set_preferred_width(caption_width)
        self._effect_caption_width = caption_width
        self.performance_reset.parentWidget().set_preferred_width(caption_width)

    def _build_live_side(self):
        viewport=TopClippedCanvas(); viewport.setObjectName("NativeSideRail"); self.side_rail = viewport; viewport.setMinimumWidth(0); viewport.set_preferred_width(METRICS["right_column"]); viewport.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Ignored)
        side=QWidget(); self.side_content = side; layout=set_layout(QGridLayout(side),0,METRICS["section_gap"]); self.side_grid = layout; layout.setColumnStretch(0, 1); layout.setRowStretch(2, 1); layout.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.appearance_card=NativeCard(inset_role='compact-form')
        appearance=set_layout(QHBoxLayout(self.appearance_card),METRICS['card_padding'],METRICS['side_action_gap'])
        self.appearance_choice=NativeSegmentedChoice((tr('appearance.light'),tr('appearance.dark')),symbols=('light','dark'))
        self.appearance_choice.set_selected(int(self._dark))
        self.appearance_choice.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
        self.appearance_choice.selected.connect(self._select_appearance)
        self.preferences_button=NativeSettingsButton(tr('action.preferences'),self.open_preferences,icon_only=True)
        self.preferences_button.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
        appearance.addWidget(self.appearance_choice,2);appearance.addWidget(self.preferences_button,1)
        layout.addWidget(self.appearance_card,0,0)
        self.actions_card = NativeCard(inset_role='compact-form')
        actions_layout = set_layout(
            QGridLayout(self.actions_card), METRICS["card_padding"], METRICS["side_action_gap"],
        )
        actions_layout.setVerticalSpacing(CARD_ROW_GAPS["actions"])
        actions_layout.setColumnStretch(0,1);actions_layout.setColumnStretch(1,1)
        for row, (action_id, caption, callback) in enumerate((
            ("soundfont_group", tr("action.soundfont_group"), self.open_group),
            ("midi_player", tr("player.title"), self.open_midi_player),
            ("recording", tr("recording_workspace.title"), self.open_recording),
            ("panic", tr("action.panic"), self.panic),
            ("reset", tr("action.reset"), self.reset),
            ("licenses", tr("licenses.action"), self.open_licenses),
            ("restart", tr("action.restart"), self.restart),
            ("exit", tr("action.exit"), self._request_exit),
        )):
            button = make_action_button(caption,callback)
            button.setMinimumWidth(0);button.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
            grid_row=row-(1 if row>3 else 0)-(1 if action_id=='exit' else 0)
            if action_id in ('exit','reset'):actions_layout.addWidget(button,grid_row,1)
            elif action_id in ('restart','panic'):actions_layout.addWidget(button,grid_row,0)
            else:actions_layout.addWidget(button,grid_row,0,1,2)
            if action_id=='restart':self.restart_button=button
            elif action_id=='exit':self.exit_button=button
            elif action_id=='panic':
                self.panic_button=button;button.setToolTip(tr('action.panic_tip'))
            elif action_id=='reset':
                self.reset_button=button;button.setToolTip(tr('action.reset_tip'))
            if action_id == "soundfont_group":
                self.group_button = button
            elif action_id == 'recording':
                self.recording_button = button
            elif action_id == 'licenses':
                self.licenses_button = button
        layout.addWidget(self.actions_card, 1, 0)
        self._recording_dialog = None
        self._recording_rows = {}
        self._recording_save_worker = None
        self._recording_save_kind = None
        self._recording_after_save = None
        self._recording_ui_state = None
        self._recording_timer = QTimer(self)
        self._recording_timer.setInterval(500)
        self._recording_timer.timeout.connect(self._refresh_recording_controls)

        self.monitor_card = NativeCard(inset_role='visualizer')
        self.monitor_card.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        monitor_layout = set_layout(QGridLayout(self.monitor_card), 0, 0)
        monitor_layout.setColumnStretch(0, 2)
        monitor_layout.setColumnStretch(1, 1)
        self.meter = NativeMeter()
        monitor_layout.addWidget(self.meter, 0, 0)
        self.install_state_tooltip(tr("state.meter.tooltip"), self.meter)

        self.master_fader = QWidget()
        self.master_fader.setObjectName("MasterFader")
        fader_layout = set_layout(
            QVBoxLayout(self.master_fader),
            METRICS["master_fader_margin"], METRICS["side_action_gap"],
        )
        self.master_fader_title = ElasticLabel(
            tr("main.master_volume"),
            preferred_width=METRICS["master_caption_preferred_width"],
        )
        self.master_fader_title.setObjectName("MasterFaderTitle")
        self.master_fader_title.setAlignment(Qt.AlignCenter)
        fader_layout.addWidget(self.master_fader_title)
        self.volume = NativeSlider(Qt.Vertical)
        self.volume.setAccessibleName(tr('main.master_volume').replace('\n',' '))
        self.volume.setRange(0, 256)
        self.volume.set_input_ticks(1, 4, 16)
        self.volume.setValue(DEFAULT_MASTER_VOLUME)
        self.volume.setInvertedAppearance(False)
        self.volume.valueChanged.connect(self._edit_master_output)
        fader_layout.addWidget(self.volume, 1, Qt.AlignHCenter)
        self.volume_text = QLabel(str(self.volume.value()))
        self.volume_text.setObjectName("MasterFaderValue")
        self.volume_text.setAlignment(Qt.AlignCenter)
        self.volume.valueChanged.connect(lambda _value: self.volume_text.setText(str(self.volume.value())))
        fader_layout.addWidget(self.volume_text)
        self.install_state_tooltip(
            tr("state.master_volume.tooltip"), self.master_fader_title,
            self.volume, self.volume_text,
        )
        self.master_fader.setMinimumWidth(0)
        self.master_fader.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        monitor_layout.addWidget(self.master_fader, 0, 1)
        layout.addWidget(self.monitor_card, 2, 0)
        QTimer.singleShot(0, self._lock_side_top_rows)
        viewport.set_content(side)
        return viewport

    @staticmethod
    def _soundfont_application_root() -> Path:
        """Return the packaged executable directory, or the source app dir."""
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent

    def _start_soundfont_discovery(self) -> None:
        """Run one bounded catalogue scan after the first interactive frame."""
        if self._shutdown_started:
            return
        root = self._soundfont_application_root()
        signals = self._soundfont_discovery_signals
        requested = self._startup_soundfont_requested
        library = self._soundfont_library
        select_startup = self._startup_soundfont_pending

        def work() -> None:
            result = scan_soundfonts(root)
            if select_startup:
                from dataclasses import replace
                result = replace(result, startup_candidates=startup_soundfont_candidates(requested, library, result.paths))
            try:
                signals.completed.emit(result)
            except RuntimeError:
                # The window may have closed while the daemon worker was in
                # an OS filesystem call. Never wait for it during shutdown.
                pass

        threading.Thread(target=work, name="ABMSynth-SoundFont-scan", daemon=True).start()

    @Slot(object)
    def _accept_soundfont_discovery(self, result: ScanResult) -> None:
        if self._shutdown_started:
            return
        self._soundfont_scan_result = result
        self._soundfont_discovered_paths = result.paths
        if self._startup_soundfont_pending:
            self._startup_soundfont_pending = False
            candidates = result.startup_candidates or ()
            selected = (candidates[0] if candidates else
                        resolve_soundfont_selection_path(self._startup_soundfont_requested))
            if not self._unsaved_group_active:
                self._soundfont_committed_path = selected
                self.soundfont_path.setText(selected)
                if (selected and self._startup_soundfont_requested
                        and soundfont_path_key(selected) != soundfont_path_key(self._startup_soundfont_requested)):
                    self.append_log(log_text('runtime.soundfont_fallback',name=Path(selected).name))
        self._populate_soundfont_selector(self._soundfont_committed_path)
        if result.stop_reason != "complete":
            self.append_log(log_text("runtime.soundfont_scan_partial"))
        if self._initial_devices_waiting_for_soundfonts:
            self._initial_devices_waiting_for_soundfonts = False
            self._start_initial_devices()

    @classmethod
    def _soundfont_display_name(cls, path: Path) -> str:
        if soundfont_path_key(path) == soundfont_path_key(bundled_soundfont_path()):
            return path.name
        root = cls._soundfont_application_root()
        try:
            return str(path.relative_to(root))
        except ValueError:
            return path.name

    @staticmethod
    def _soundfont_path_key(path: str | Path) -> str:
        return soundfont_path_key(path)

    def _set_soundfont_library(self, library: SoundFontLibrary) -> bool:
        library = library.normalized()
        if library == self._soundfont_library:
            return True
        try:
            from settings import soundfont_library_ini,read_ini
            values,owned=soundfont_library_ini(library,read_ini())
            self._settings_writer.submit({'SoundFontLibrary':values},owned_keys={'SoundFontLibrary':owned})
        except (OSError, ValueError) as exc:
            self.append_log(log_text("runtime.settings_save_failed", error=exc))
            return False
        self._soundfont_library = library
        self._populate_soundfont_selector(self._soundfont_committed_path)
        return True

    def _remember_soundfont_selection(self, path: str, *, manually_added: bool = False) -> None:
        library = (self._soundfont_library.manually_add(path) if manually_added else
                   self._soundfont_library.mark_used(path))
        self._set_soundfont_library(library)

    def _toggle_soundfont_favorite(self, path: str) -> None:
        self._set_soundfont_library(self._soundfont_library.toggle_favorite(path))

    def _hide_soundfont_path(self, path: str) -> None:
        # Hiding is a catalogue action, not an audio stop or stream change.
        self._set_soundfont_library(self._soundfont_library.hide(path))

    def _on_soundfont_favorites_filter_changed(self, _checked: bool) -> None:
        self._populate_soundfont_selector(self._soundfont_committed_path)

    def _populate_soundfont_selector(self, selected_path: str | None = None) -> None:
        """Build one ordered file list without rescanning or loading."""
        combo = getattr(self, "soundfont_selector", None)
        if combo is None:
            return
        selected = str(self.soundfont_path.text() if selected_path is None else selected_path).strip()
        selected_key = self._soundfont_path_key(selected) if selected else None
        library = self._soundfont_library
        only_favorites = self.soundfont_favorites_only.isChecked()
        visible_keys: set[str] = set()

        def visible(paths) -> list[str]:
            result = []
            for path in paths:
                value = str(path)
                key = self._soundfont_path_key(value)
                if key not in visible_keys and not library.is_hidden(value):
                    result.append(value)
                    visible_keys.add(key)
            return result

        if self._unsaved_group_active:
            # A live edit is a separate state from its disk origin, even when
            # both currently resolve to the same path. Pin the disk origin as
            # a rollback choice before any ordinary favorite/history rows.
            previous = self._persisted_soundfont_path or selected
            # A new/empty edited document has no disk origin. Its unsaved
            # identity must still be visible after the no-font first launch.
            paths = [(selected, SOUNDFONT_ROW_UNSAVED)]
            if previous:
                paths.append((previous, SOUNDFONT_ROW_CURRENT))
            visible_keys.update(self._soundfont_path_key(path) for path, _ in paths)
            paths.extend((path, SOUNDFONT_ROW_FILE) for path in visible(library.favorites))
            if not only_favorites:
                paths.extend((path, SOUNDFONT_ROW_FILE) for path in visible((
                    *library.recent,
                    *sorted(self._soundfont_discovered_paths, key=lambda item: str(item).casefold()),
                )))
        elif only_favorites:
            # The current file is useful even with zero favorites. A distinct
            # row kind keeps it visible without offering an ineffective ×.
            paths = [(selected, SOUNDFONT_ROW_CURRENT)] if selected else []
            if selected:
                visible_keys.add(selected_key)
            paths.extend((path, SOUNDFONT_ROW_FILE) for path in visible(library.favorites))
        else:
            # The active visible font precedes even favorites; the shared
            # path-key set keeps it single when also favorite/recent/scanned.
            # A hidden active font remains a closed-selection-only stub.
            ordered = (
                *((selected,) if selected and not library.is_hidden(selected) else ()),
                *library.favorites, *library.recent,
                *sorted(self._soundfont_discovered_paths, key=lambda item: str(item).casefold()),
            )
            paths = [(path, SOUNDFONT_ROW_FILE) for path in visible(ordered)]

        blocked = combo.blockSignals(True)
        try:
            combo.clear()
            selected_index = -1
            hidden_current_index = -1
            if selected and selected_key not in visible_keys and not only_favorites:
                # The closed combo must keep showing the loaded font, even
                # when its ordinary popup row was explicitly hidden.
                hidden_current_index = combo.count()
                combo.addItem(self._soundfont_display_name(Path(selected)), selected)
                combo.setItemData(hidden_current_index, SOUNDFONT_ROW_CURRENT, SOUNDFONT_ROW_KIND_ROLE)
                combo.setItemData(hidden_current_index, tr(
                    "soundfont_picker.hidden_current_tooltip", path=selected,
                ) if library.is_hidden(selected) else selected, Qt.ToolTipRole)
                selected_index = hidden_current_index
            for path, kind in paths:
                index = combo.count()
                name = (tr("group.unsaved_display") if kind == SOUNDFONT_ROW_UNSAVED
                        else self._soundfont_display_name(Path(path)))
                combo.addItem(name, path)
                combo.setItemData(index, kind, SOUNDFONT_ROW_KIND_ROLE)
                combo.setItemData(index, tr(
                    "group.unsaved_tooltip" if kind == SOUNDFONT_ROW_UNSAVED
                    else "soundfont_picker.hidden_current_tooltip" if kind == SOUNDFONT_ROW_CURRENT and library.is_hidden(path)
                    else "soundfont_picker.current_row_tooltip" if kind == SOUNDFONT_ROW_CURRENT
                    else "soundfont_picker.row_tooltip", path=path,
                ), Qt.ToolTipRole)
                favorite = kind != SOUNDFONT_ROW_UNSAVED and library.is_favorite(path)
                combo.set_favorite_icon(index, favorite)
                if favorite:
                    combo.setItemData(index, tr("soundfont_picker.favorite_accessible", name=name), Qt.AccessibleTextRole)
                if kind == SOUNDFONT_ROW_UNSAVED or (
                    not self._unsaved_group_active and selected_key and
                    self._soundfont_path_key(path) == selected_key
                ):
                    selected_index = index
            # A path in history/INI is not proof of an available file. Preserve
            # identity for repair;custom unsaved groups have no disk file yet.
            for index in range(combo.count()):
                path=combo.itemData(index,Qt.UserRole)
                if (path and combo.itemData(index,SOUNDFONT_ROW_KIND_ROLE)!=SOUNDFONT_ROW_UNSAVED
                        and not Path(path).is_file()):
                    combo.setItemText(index,tr('soundfont_picker.missing_name',name=combo.itemText(index)))
                    combo.setItemData(index,tr('soundfont_picker.missing_tooltip',path=path),Qt.ToolTipRole)
            command_index = -1
            if not only_favorites:
                command_index = combo.count()
                combo.addItem(tr("action.change_soundfont"), SOUNDFONT_PICKER_SENTINEL)
                combo.setItemData(command_index, SOUNDFONT_ROW_PICKER, SOUNDFONT_ROW_KIND_ROLE)
                combo.setItemData(command_index, tr("action.change_soundfont"), Qt.ToolTipRole)
            combo.setCurrentIndex(selected_index if selected_index >= 0 else command_index)
            for index in range(combo.count()):
                combo.view().setRowHidden(index, index == hidden_current_index)
            combo.setToolTip(
                tr("group.unsaved_tooltip") if self._unsaved_group_active else
                tr("soundfont_picker.hidden_current_tooltip", path=selected)
                if selected and library.is_hidden(selected) else
                tr('soundfont_picker.missing_tooltip',path=selected) if selected and not Path(selected).is_file() else
                selected if selected else tr("main.soundfont")
            )
        finally:
            combo.blockSignals(blocked)

    def _on_soundfont_selector_activated(self, index: int) -> None:
        combo = self.soundfont_selector
        if combo.itemData(index, SOUNDFONT_ROW_KIND_ROLE) == SOUNDFONT_ROW_UNSAVED:
            self._populate_soundfont_selector(self._soundfont_committed_path)
            return
        value = combo.itemData(index, Qt.UserRole)
        if value == SOUNDFONT_PICKER_SENTINEL:
            # QComboBox has already made the command row current by the time
            # activated fires. text() therefore cannot recover the previous
            # path. Restore the last committed file both before and after the
            # dialog; a successful load updates that path in load_group().
            self._populate_soundfont_selector(self._soundfont_committed_path)
            try:
                picked = self.browse_font()
                if picked:
                    self._remember_soundfont_selection(picked, manually_added=True)
            finally:
                self._populate_soundfont_selector(self._soundfont_committed_path)
            return
        if value:
            if (not self._unsaved_group_active and self.soundfont_runtime.group.fonts and
                    Path(str(value)).is_file() and
                    self._soundfont_path_key(value) == self._soundfont_path_key(self._soundfont_committed_path)):
                self._populate_soundfont_selector(self._soundfont_committed_path)
                return
            self.activate_soundfont_path(str(value))
            self._populate_soundfont_selector(self._soundfont_committed_path)

    def _lock_side_top_rows(self):
        density = LAYOUT_DENSITIES[self._layout_density]
        padding = density["card_padding"]
        self.actions_card.layout().setContentsMargins(*(padding,) * 4)
        self._side_preferred_heights = {
            "appearance": self.appearance_card.sizeHint().height(),
            "actions": self.actions_card.sizeHint().height(),
        }
        for card in (self.appearance_card,self.actions_card, self.monitor_card):
            card.setMinimumHeight(0)
            card.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.side_grid.setEnabled(False)
        self.side_rail.resize_callback = self._layout_side_sections
        self.side_rail.lock_natural_width()
        self._layout_side_sections()

    def _layout_side_sections(self):
        """The shared stack rule leaves every spare rail pixel to its meter."""
        if not hasattr(self, "_side_preferred_heights"):
            return
        place_top_anchored_stack(
            self.side_rail.content.width(),
            self.side_rail.height(),
            METRICS["section_gap"],
            (
                StackItem(self.appearance_card,self._side_preferred_heights['appearance']),
                StackItem(self.actions_card, self._side_preferred_heights["actions"]),
                StackItem(self.monitor_card, 0, fill_tail=True),
            ),
        )

    def _select_appearance(self,index):
        self.set_dark(index==1)
        self._queue_ui_save()

    def _neutralize_bmsynth_extensions(self):
        """Clear processing absent from the traced original import surface."""
        # Functional wiring and reset seeds can change even an original CC.
        # The explicitly selected source-compatible import clears them too.
        ae.runtime.channel_policy.restore_ini({})
        self.audio_fx_panel.load_ini("[]")
        ae.ensure_channel_event_filter()
        self._save_midi_cc_configuration()
        for control in (self.compressor_enabled, self.limiter_enabled):
            blocked = control.blockSignals(True)
            control.setChecked(False)
            control.blockSignals(blocked)
        self.extra_delay_wet.set(0, False)
        self._sync_extended_delay_controls()
        for row in EFFECT_ROWS["dynamics"]:
            getattr(self, row.attr).setEnabled(False)
        self.spatial_input_gain.set(SpatialParameters().input_gain_db, False)
        self.spatial_time.set(SpatialParameters().time_ms, False)
        self.spatial_hf_ratio.set(SpatialParameters().high_frequency_ratio, False)
        self._set_spatial_sample_rate(0)
        self.dx_level.set(0, False)
        self.velocity.set(100, False)
        self.humanize_timing.set(0, False)
        self.humanize_velocity.set(0, False)
        self.humanize_preserve_simultaneity.setChecked(True)
        self._eq_bands = list(EQ_DEFAULT_BANDS)
        self.eq_output_trim.set(0, False)
        self._custom_presets["equalizer"] = self._custom_snapshot("equalizer")
        blocked = self.eq_preset.blockSignals(True)
        self.eq_preset.setCurrentIndex(self.eq_preset.findData("off"))
        self.eq_preset.blockSignals(blocked)
        self.on_equalizer_band(0)
        performance = ae.runtime.performance
        performance.reset_master()
        performance.reset_scale()
        performance.reset_drums()
        performance.reset_expression()
        # Original BMSynth leaves CC65 off when the DAW has not set it.
        # ABMSynth's startup default is an extension, not source parity.
        for channel in ae.runtime.addresses.channels():
            performance.set_expression(channel, "portamento", 0)
        self._sync_master_tuning()
        self.tuning_coarse.set(0, False)
        self.performance_reset.setEnabled(False)

    def apply_bmsynth_values(self, values: dict, *, neutralize_extensions=False) -> int:
        """Apply validated source fields, optionally clearing extra processing."""
        bms = values.get("BMS", {})
        applied = 0
        self._preset_programmatic = True
        try:
            if neutralize_extensions:
                self._neutralize_bmsynth_extensions()
            for key, setter in (
                ("Volume", lambda v: self.volume.setValue(v * 8)),
                ("StereoEh", lambda v: self.stereo.set(bmsynth_stereo_position_to_multiplier(v), False)),
                ("GlobalReverb", lambda v: self.global_reverb.set(bmsynth_global_position_to_midi_send(v), False)),
                ("GlobalChorus", lambda v: self.global_chorus.set(bmsynth_global_position_to_midi_send(v), False)),
                ("OverNote", lambda v: self.overnote.setChecked(bool(v))),
                ("UseGSReset", lambda v: self.set_system_reset_receive('gs_reset',bool(v))),
            ):
                if key in bms:
                    setter(bms[key]); applied += 1
            reverb = values.get("Reverb", {}); names = ["Custom", "Default", "Off"] + [item[0] for item in REVERB_PRESETS[1:]]
            if "Preset" in reverb:
                preset_index = max(0, min(int(reverb["Preset"]) + 1, len(names) - 1))
                self.reverb_preset.setCurrentText(names[preset_index]); applied += 1
            for key, control in (("Level",self.reverb_level),("Time",self.reverb_time),("Delay",self.reverb_delay),("HighCutoff",self.reverb_hi),("LowCutoff",self.reverb_lo)):
                if key in reverb: control.set(reverb[key], False); applied += 1
            chorus = values.get("Chorus", {})
            if "Preset" in chorus:
                self.chorus_preset.setCurrentIndex(min(chorus["Preset"] + 1, self.chorus_preset.count() - 1)); applied += 1
            for key,control in (("Level",self.ch_level),("Delay",self.ch_delay),("Depth",self.ch_depth),("Rate",self.ch_rate),("FeedBack",self.ch_feedback),("ToReverb",self.ch_to_reverb)):
                if key in chorus: control.set(chorus[key], False); applied += 1
            delay = values.get("Delay", {})
            if "Preset" in delay:
                self.delay_preset.setCurrentIndex(min(delay["Preset"] + 1, self.delay_preset.count() - 1)); applied += 1
            if "DelayType" in delay:
                self.delay_type.setCurrentIndex(min(delay["DelayType"], self.delay_type.count() - 1)); applied += 1
            for key,control in (("Level",self.delay_level),("Time",self.delay_time),("FeedBack",self.delay_feedback)):
                if key in delay: control.set(delay[key], False); applied += 1
            if "UseDelaySysEx" in delay:
                self.delay_sysex_enabled.setChecked(bool(delay["UseDelaySysEx"])); applied += 1
            boost = values.get("Boost", {})
            if boost:
                # Source Boost has only Bass/Treble. Neutralize every
                # ABMSynth-only filter and post-EQ trim before storing the
                # imported Custom snapshot; otherwise the visible source
                # gains would not produce the source Boost output.
                # Retain imported gains while bypassed for later recall.
                enabled = bool(boost.get("Use", self.eq_preset.currentData() != "off"))
                if "Use" in boost:
                    applied += 1
                previous = self._eq_bands
                bass_gain = previous[0].gain_db if previous else 0
                treble_gain = previous[-1].gain_db if len(previous) >= 2 else 0
                self._eq_bands = [EQBand(100, 36, boost.get("Bass", bass_gain)),
                                  *(EQBand(b.center_hz, b.bandwidth_semitones, 0) for b in previous[1:-1]),
                                  EQBand(15000, 36, boost.get("Treble", treble_gain))]
                self.eq_output_trim.set(0, False)
                self._eq_bands = list(self._read_equalizer_state().validated(self._engine_preferences.sample_rate).bands)
                self._custom_presets["equalizer"] = self._custom_snapshot("equalizer")
                blocked = self.eq_preset.blockSignals(True)
                self.eq_preset.setCurrentIndex(self.eq_preset.findData("custom" if enabled else "off"))
                self.eq_preset.blockSignals(blocked)
                if not enabled:
                    self._eq_bands = list(EQ_DEFAULT_BANDS)
                applied += sum(key in boost for key in ("Bass", "Treble"))
                self.on_equalizer_band(0)
            dx = values.get("DXReverb", {})
            if dx:
                # Original 00466292..004662A7 installs four DX8 fields;
                # only the wet-mix Level is serialized in BMSynth's INI.
                # Re-importing it must not retain ABMSynth-only room values.
                original_room = SpatialParameters()
                self.spatial_input_gain.set(original_room.input_gain_db, False)
                self.spatial_time.set(original_room.time_ms, False)
                self.spatial_hf_ratio.set(original_room.high_frequency_ratio, False)
                self._set_spatial_sample_rate(0)
            if "Use" in dx and not dx["Use"]:
                self.dx_level.set(0, False); applied += 1
            elif "Level" in dx:
                if dx.get("Use") == 1 and dx["Level"] == 0:
                    self.append_log(log_text("runtime.ini_warning", message=tr("error.ini_dx_zero_unrepresentable")))
                else:
                    self.dx_level.set(dx["Level"], False); applied += 1
            velocity = values.get("VelocityRate", {})
            if velocity.get("Use") == 0:
                # Original BMSynth bypass maps to the single native neutral value.
                self.velocity.set(100, False); applied += 1
            elif "Level" in velocity:
                self.velocity.set(velocity["Level"], False); applied += 1
        finally:
            self._preset_programmatic = False
        # Preset callbacks deliberately ignore programmatic combo changes.
        # Apply only after this guard is released, or INI import silently
        # skips both BASSMIDI macro events despite showing preset captions.
        self.apply_effect_state()
        self._eq_graph_timer.start(0)
        return applied

    def open_group(self, *, performance=False):
        # The committed runtime model, not the last filename on disk, owns
        # edits made through Apply Now and remains the dialog-open baseline.
        dialog = getattr(self, "_group_dialog", None)
        if dialog is None:
            dialog = NativeLiveSoundFontGroupDialog(self, self.soundfont_runtime.group)
            self._group_dialog = dialog
            dialog.setModal(True)
            dialog.finished.connect(lambda _result: self._release_group(dialog))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        if performance:
            QTimer.singleShot(0, lambda: (dialog.group_tabs.setCurrentIndex(1),
                dialog._select_preset_page(2), dialog._select_all_instruments()))

    def _release_group(self, dialog):
        dialog.poll.stop()
        creator = getattr(dialog, 'instrument_creator', None)
        if creator is not None:
            creator.shutdown()
        if getattr(self, "_group_dialog", None) is dialog:
            self._group_dialog = None
        dialog.deleteLater()

    def mark_unsaved_group(self):
        self._startup_soundfont_pending = False
        self._unsaved_group_active = True
        self._populate_soundfont_selector(self._soundfont_committed_path)

    def _save_unsaved_group(self):
        group = self.soundfont_runtime.group
        path = choose_group_save_path(self, group)
        if not path:
            return False
        try:
            saved = save_sfg(group, path)
        except (OSError, UnicodeError, ValueError) as exc:
            NativeMessageBox.critical(self, tr("group.save_failed_title"), str(exc))
            return False
        if self.load_group(saved, fast_remap=self.soundfont_runtime.can_remap_existing(saved)):
            return True
        NativeMessageBox.warning(self, tr("group.title"), tr("group.saved_not_applied", filename=Path(path).name))
        return False

    def _confirm_unsaved_group_loss(self, intent):
        if not self._unsaved_group_active:
            return True
        prompt_ids = {
            "exit": ("group.unsaved_shutdown", "group.save_and_exit", "group.discard_and_exit"),
            "restart": ("group.unsaved_restart", "group.save_and_restart", "group.discard_and_restart"),
            "switch": ("group.unsaved_switch", "group.save_and_switch", "group.discard_and_switch"),
        }
        message_id, save_id, discard_id = prompt_ids[intent]
        prompt = NativeMessageBox(self)
        prompt.setWindowTitle(tr("group.title"))
        prompt.setText(tr(message_id))
        prompt.setIcon(QMessageBox.Question)
        save = prompt.addButton(tr(save_id), QMessageBox.AcceptRole)
        discard = prompt.addButton(tr(discard_id), QMessageBox.DestructiveRole)
        stay = prompt.addButton(tr("common.cancel"), QMessageBox.RejectRole)
        set_button_primary(save,True)
        prompt.setDefaultButton(save)
        prompt.setEscapeButton(stay)
        prompt.exec()
        if prompt.clickedButton() is save:
            return self._save_unsaved_group()
        return prompt.clickedButton() is discard

    def show_soundfont_load_error(self, error):
        NativeMessageBox.warning(self, tr('main.soundfont'), str(error))

    def browse_font(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("group.select_soundfont_title"), "", soundfont_load_filter(),
        )
        if not path:
            return None
        if self.activate_soundfont_path(path):
            return path
        return None

    def activate_soundfont_path(self, path):
        if self._shutdown_started or self._font_load_active:
            return False
        if not self._confirm_unsaved_group_loss("switch"):
            return False
        previous = self._soundfont_committed_path
        self.soundfont_path.setText(resolve_soundfont_selection_path(path))
        try:
            if self.load_current_soundfont():
                self._startup_soundfont_pending = False
                return True
        except (OSError,ValueError) as exc:
            self.soundfont_path.setText(previous)
            self._populate_soundfont_selector(previous)
            self._report_soundfont_load_error(exc)
            return False
        self.soundfont_path.setText(previous)
        self._populate_soundfont_selector(previous)
        return False

    def apply_group_runtime(self, group, change, *, persist=True):
        if not self.soundfont_runtime.finish_runtime_controls():
            self.append_log(log_text('runtime.preset_controls_pending'));return False
        if change == "font_layout" and self.soundfont_runtime.can_remap_existing(group):
            return self.load_group(group, persist=persist, fast_remap=True)
        return super().apply_group_runtime(group, change, persist=persist)

    def load_group(self, group, *, persist=True, fast_remap=False):
        """Keep the relocated action's stable caption after a runtime load.

        The inherited controller formerly used the SoundFont-card button as a
        loaded-group display.  In the native layout that button is a fixed
        action in the right rail, so the filename continues to live in the
        SoundFont path field and the action remains discoverable.
        """
        if self._shutdown_started or self._font_load_active:
            return False
        if not self.soundfont_runtime.finish_runtime_controls():
            self.append_log(log_text('runtime.preset_controls_pending'));return False
        self._font_load_active = True
        ae.set_font_preload_callback(None)
        try:
            if fast_remap and self.soundfont_runtime.stream_handle():
                with self._font_preload_lock:
                    try:
                        result = self.soundfont_runtime.remap_existing(group)
                    except Exception as exc:
                        loaded = self._report_soundfont_load_error(exc)
                    else:
                        loaded = self._present_loaded_soundfont_group(group, result)
            elif os.environ.get(ENV_UI_ONLY) or not self.soundfont_runtime.stream_handle():
                with self._font_preload_lock:
                    loaded = super().load_group(group, persist=persist)
            else:
                loaded = self._load_group_with_progress(group)
        finally:
            if self._soundfont_handles and self._engine_preferences.loading_mode == 1:
                ae.set_font_preload_callback(self._preload_used_preset)
            self._font_load_active = False
        if hasattr(self, "group_button"):
            self.group_button.setText(tr("action.soundfont_group"))
        if loaded and hasattr(self, "soundfont_selector"):
            if persist:
                self._unsaved_group_active = False
            self._soundfont_committed_path = self.soundfont_path.text()
            if persist:
                self._persisted_soundfont_path = self._soundfont_committed_path
                library_path = (
                    group.source_path or
                    (group.fonts[0].filename if len(group.fonts) == 1 and not group.presets else "")
                )
                if library_path:
                    self._remember_soundfont_selection(library_path)
            self._populate_soundfont_selector(self.soundfont_path.text())
            if persist:
                self._queue_ui_save()
        return loaded

    def _load_group_with_progress(self, group):
        """Join the BASS worker before any window/device shutdown can proceed."""
        parent = QApplication.activeModalWidget() or self
        progress = NativeSoundFontLoadDialog(parent)
        worker = NativeSoundFontLoadWorker(
            self.soundfont_runtime, group, self._font_preload_lock, self,
            source_recovery=self._recover_soundfont_source_owner,
        )
        progress.worker = worker
        worker.stage_changed.connect(progress.set_stage)
        worker.retryNeeded.connect(progress.set_recovery_pending)
        worker.recoveryStarted.connect(progress.set_recovery_running)
        progress.retryRequested.connect(worker.retry_requested.set)
        progress.cancelRequested.connect(worker.cancel_requested.set)
        worker.finished.connect(progress.finish)
        self._font_load_worker = worker
        worker.start()
        try:
            progress.exec()
        finally:
            worker.wait()
            self._font_load_worker = None
            progress.finish()
            progress.deleteLater()
        error, result = worker.error, worker.result
        worker.deleteLater()
        if isinstance(error, SoundFontLoadCancelled):
            return False
        if error is not None:
            return self._report_soundfont_load_error(error)
        return self._present_loaded_soundfont_group(group, result)

    def _preload_used_preset(self, bank: int, preset: int) -> None:
        """Load the first mapped font for a newly encountered MIDI program."""
        with self._font_preload_lock:
            if self._shutdown_started:
                return
            self.soundfont_runtime.preload_used_preset(bank, preset)

    def debug(self):
        active = self.midi_input_runtime.trace
        if active is not None:
            try:
                summary = self.midi_input_runtime.stop_trace()
            except (OSError,ValueError,RuntimeError) as error:
                self._refresh_debug_controls()
                self.append_log(log_text("runtime.debug_stop_failed",error=error))
                return
            ae.runtime.output_meter.set_diagnostic_enabled(False)
            self.append_log(log_text("runtime.debug_stopped", path=active.path))
            self._refresh_debug_controls()
            self._last_trace_effect_state = None
            if summary and (summary["trace_queue_dropped"] or summary["driver_ring_overwritten"] or summary["driver_in_flight"]):
                self.append_log(log_text("runtime.debug_incomplete",
                                   queue=summary["trace_queue_dropped"],
                                   driver=summary["driver_ring_overwritten"],
                                   pending=summary["driver_in_flight"]))
            if summary and summary["write_error"]:
                self.append_log(log_text("runtime.debug_write_failed", error=summary["write_error"]))
            if summary and not summary["writer_finished"]:
                self.append_log(log_text("runtime.debug_flush_pending"))
            if summary and summary["counts"].get("pipe_midi", 0) and not summary["counts"].get("driver_midi", 0):
                self.append_log(log_text("runtime.debug_driver_unavailable"))
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = settings_path().parent / "logs" / f"ABMSynth-debug-{stamp}-{os.getpid()}-{time.time_ns() % 1_000_000:06d}.log"
        try:
            trace = self.midi_input_runtime.start_trace(path)
        except (OSError, ValueError, RuntimeError) as exc:
            self._refresh_debug_controls()
            self.append_log(log_text("runtime.debug_start_failed", error=exc))
            return
        ae.runtime.output_meter.set_diagnostic_enabled(True)
        effect_state = asdict(self.capture_effect_state())
        self._last_trace_effect_state = effect_state
        trace.record("session_config", stream=int(ae.runtime.stream_handle),
                     soundfont=self.soundfont_path.text(),
                     overlap_correction=ae.runtime.midi_options.overnote_enabled,
                     output_device=self.device_combo.currentText(),
                     sample_rate=self._engine_preferences.sample_rate,
                     sample_format="float32",
                     voice_limit=self._engine_preferences.voices,
                     program_by_channel=list(ae.runtime.soundfont_presets.program),
                     bank_by_channel=[ae.runtime.soundfont_presets.bank(index) for index in ae.runtime.addresses.channels()],
                     bank_lsb_by_channel=ae.runtime.soundfont_presets.bank_lsb[:ae.runtime.addresses.count],
                     midi_ports=list(ae.runtime.addresses.ports()),
                     effect_state=effect_state)
        self._refresh_debug_controls()
        self.append_log(log_text("runtime.debug_started", path=path))

    def _refresh_debug_controls(self):
        dialog = getattr(self, '_preferences_dialog', None)
        if dialog is not None:
            dialog.error_report.refresh_capture()

    def open_licenses(self):
        dialog=getattr(self,'_licenses_dialog',None)
        if dialog is None:
            dialog=NativeLicensesDialog(self);self._licenses_dialog=dialog
        dialog.show();dialog.raise_();dialog.activateWindow()

    def set_driver_notice_suppressed(self,suppressed):
        try:
            self._settings_writer.submit({'UI':{'skip_driver_setup_notice':str(int(suppressed))}},
                owned_keys={'UI':{'skip_driver_setup_notice'}})
        except (OSError,ValueError) as error:
            self.append_log(log_text('runtime.settings_save_failed',error=error))
            return False
        self._skip_driver_setup_notice=bool(suppressed)
        return True

    def open_preferences(self, *, performance=False):
        if performance:
            self.open_performance_details()
            return
        dialog = getattr(self, "_preferences_dialog", None)
        if dialog is None:
            dialog = NativeEnginePreferencesDialog(self)
            self._preferences_dialog = dialog
            dialog.setModal(True)
            dialog.finished.connect(lambda _result: self._release_preferences(dialog))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _release_preferences(self, dialog):
        if getattr(self, "_preferences_dialog", None) is dialog:
            self._preferences_dialog = None
        dialog.deleteLater()

    def open_midi_player(self):
        if getattr(self, "_midi_player", None) is None:
            self._midi_player = NativeMidiPlayerDialog(self)
        self._midi_player.set_theme()
        self._midi_player.show()
        self._midi_player.raise_()
        self._midi_player.activateWindow()

    def _recording_owner(self, kind):
        return self.midi_input_runtime.midi_recorder if kind == 'midi' else ae.runtime.output_analysis

    def open_recording(self):
        if self._recording_dialog is None:
            self._recording_dialog = NativeRecordingDialog(self)
            self._recording_rows = self._recording_dialog.rows
            self.midi_record_button, self.midi_record_save_button, _ = self._recording_rows['midi']
            self.record_button, self.record_save_button, _ = self._recording_rows['wave']
            self._recording_ui_state = None
        self._refresh_recording_controls()
        self._recording_dialog.show()
        self._recording_dialog.raise_()
        self._recording_dialog.activateWindow()

    def toggle_recording(self, kind='wave'):
        recorder = self._recording_owner(kind)
        try:
            if recorder.recording_state == "recording":
                recorder.pause_recording()
            elif recorder.has_draft:
                recorder.resume_recording()
            else:
                preferences = self._engine_preferences
                from settings import recording_capture_options
                recorder.start_recording(**({} if kind == 'midi' else recording_capture_options(preferences)))
            self._recording_timer.start()
        except (OSError, RuntimeError, ValueError) as exc:
            NativeMessageBox.warning(self, tr('midi_recording.title' if kind == 'midi' else "recording.title"), str(exc))
        self._refresh_recording_controls()

    def _refresh_recording_controls(self):
        busy = self._recording_save_worker is not None
        signatures = []
        running = False
        active = False
        for kind in ('midi', 'wave'):
            recorder = self._recording_owner(kind)
            state = recorder.recording_state
            own_busy = busy and self._recording_save_kind == kind
            old = getattr(self, '_recording_ui_state', None) or ()
            status = recorder.recording_status() if not own_busy else None
            if status:
                if status['failed'] and state == 'recording':
                    try:recorder.pause_recording()
                    except RuntimeError:recorder.stop_recording()
                    self.append_log(log_text('midi_recording.capture_failed', count=status['lost_bytes'], error=status.get('error', 0))
                                    if kind == 'midi' else log_text('output_tools.record_loss', count=status['lost_bytes']))
            state = recorder.recording_state
            phase = 'saving' if own_busy else 'stop' if state == 'recording' else 'resume' if state == 'paused' else 'start'
            signature = (kind, state, busy, bool(recorder.encoder), bool(recorder.stream))
            signatures.append(signature)
            if kind in self._recording_rows:
                button, save, discard = self._recording_rows[kind]
                button.setText(tr('midi_recording.'+phase if kind == 'midi' else 'recording.wave_'+phase))
                save.setVisible(state == 'paused' or own_busy)
                discard.setVisible(state == 'paused' and not own_busy)
                button.setEnabled(not busy and (bool(recorder.encoder) if state == 'paused' else bool(recorder.stream)))
                save.setEnabled(not busy)
                discard.setEnabled(not busy)
                if signature not in old:
                    set_button_primary(button, state == 'recording')
                self._recording_dialog.present(kind, state, own_busy, status)
                if state == 'idle' and not recorder.stream:
                    self._recording_dialog.status_labels[kind].setText(tr('recording_workspace.state_unavailable'))
            active = active or recorder.recording_state == 'recording'
            running = running or state != 'idle'
        if tuple(signatures) != self._recording_ui_state:
            self._recording_ui_state = tuple(signatures)
        draft = any(self._recording_owner(kind).has_draft for kind in ('midi', 'wave'))
        self.recording_button.setText(tr('recording_workspace.active' if active else 'recording.saving' if busy else 'recording_workspace.pending' if draft else 'recording_workspace.title'))
        self.recording_button.setToolTip(tr('recording_workspace.open_tip'))
        if not running and not busy:
            self._recording_timer.stop()

    def discard_recording(self, kind):
        if self._recording_save_worker is not None:
            return
        recorder = self._recording_owner(kind)
        if not recorder.has_draft:
            return
        try:
            recorder.discard_recording()
        except (OSError, RuntimeError, ValueError) as exc:
            NativeMessageBox.warning(self._recording_dialog or self, tr('recording_workspace.title'), str(exc))
        self._refresh_recording_controls()

    def _choose_recording_path(self, kind='wave'):
        from settings import recording_filename, RECORDING_EXTENSIONS
        recorder = self._recording_owner(kind)
        preferences = self._engine_preferences
        extension = 'mid' if kind == 'midi' else RECORDING_EXTENSIONS[recorder.recording_container]
        folder = preferences.recording_directory or QStandardPaths.writableLocation(QStandardPaths.MusicLocation)
        if not Path(folder).is_dir():
            folder = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
        filename = recording_filename(preferences, container='wav' if kind == 'midi' else recorder.recording_container, started=recorder.recording_date)
        if kind == 'midi':filename = str(Path(filename).with_suffix('.mid'))
        suggestion = str(Path(folder) / filename)
        chooser = QFileDialog(self._recording_dialog if self._recording_dialog and self._recording_dialog.isVisible() else self, tr('midi_recording.save_title' if kind == 'midi' else "recording.save_title"), suggestion,
                             tr('midi_recording.filter' if kind == 'midi' else "recording.filter_" + recorder.recording_container))
        chooser.setAcceptMode(QFileDialog.AcceptSave)
        chooser.setDefaultSuffix(extension)
        if not chooser.exec():
            return None
        path = chooser.selectedFiles()[0]
        valid = (".aif", ".aiff") if extension == "aif" else ('.'+extension,)
        if Path(path).suffix.lower() not in valid:
            NativeMessageBox.warning(self, tr('midi_recording.title' if kind == 'midi' else "recording.title"), tr("recording.extension_invalid", extension=extension))
            return None
        return path

    def save_recording(self, kind='wave'):
        if self._recording_save_worker is not None:
            return
        recorder = self._recording_owner(kind)
        if not recorder.has_draft:
            return
        path = self._choose_recording_path(kind)
        if not path:
            self._recording_after_save = None
            return
        worker = NativeRecordingSaveWorker(recorder, path, self)
        self._recording_save_worker = worker
        self._recording_save_kind = kind
        worker.finished.connect(self._recording_saved)
        worker.start()
        self._refresh_recording_controls()

    @Slot()
    def _recording_saved(self):
        worker = self._recording_save_worker
        kind = self._recording_save_kind
        self._recording_save_worker = None
        self._recording_save_kind = None
        if worker.error:
            self._recording_after_save = None
            NativeMessageBox.warning(self, tr('midi_recording.title' if kind == 'midi' else "recording.title"), tr("recording.save_failed", error=worker.error))
        else:
            self.append_log(log_text("output_tools.record_saved", path=worker.path))
            recorder = self._recording_owner(kind)
            if recorder.last_recording_loss or recorder.last_recording_error:
                NativeMessageBox.warning(self, tr("recording.title"), tr("output_tools.record_loss", count=recorder.last_recording_loss))
        worker.deleteLater()
        self._refresh_recording_controls()
        after, self._recording_after_save = self._recording_after_save, None
        if after is not None:
            QTimer.singleShot(0, self.restart if after == "restart" else self._request_exit)

    def _confirm_unsaved_recording(self, intent):
        if self._recording_save_worker is not None:
            NativeMessageBox.information(self, tr("recording.title"), tr("recording.save_busy"))
            return False
        kind = next((key for key in ('midi', 'wave') if self._recording_owner(key).has_draft), None)
        if kind is None:
            return True
        recorder = self._recording_owner(kind)
        prompt = NativeMessageBox(self)
        prompt.setWindowTitle(tr('midi_recording.title' if kind == 'midi' else "recording.title"))
        prompt.setText(tr("recording.unsaved_restart" if intent == "restart" else "recording.unsaved"))
        prompt.setIcon(QMessageBox.Question)
        save = prompt.addButton(tr("recording.save_restart" if intent == "restart" else "recording.save_exit"), QMessageBox.AcceptRole)
        discard = prompt.addButton(tr("recording.discard_restart" if intent == "restart" else "recording.discard_exit"), QMessageBox.DestructiveRole)
        cancel = prompt.addButton(tr("common.cancel"), QMessageBox.RejectRole)
        set_button_primary(save,True)
        prompt.setDefaultButton(save)
        prompt.setEscapeButton(cancel)
        prompt.exec()
        if prompt.clickedButton() is save:
            try:
                recorder.pause_recording()
            except RuntimeError as exc:
                NativeMessageBox.warning(self, tr("recording.title"), str(exc))
                return False
            self._recording_after_save = intent
            self.save_recording(kind)
            self._refresh_recording_controls()
            return False
        if prompt.clickedButton() is discard:
            try:
                recorder.discard_recording()
                return self._confirm_unsaved_recording(intent)
            except (OSError, RuntimeError, ValueError) as exc:
                NativeMessageBox.warning(self, tr("recording.title"), tr("recording.discard_failed", error=exc))
        return False

    def _setup_tray_icon(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray_icon = QSystemTrayIcon(self)
        self._tray_icon.setIcon(self.windowIcon())
        self._tray_icon.setToolTip(tr("main.window_title"))
        menu = NativeMenu(self)
        show_action = QAction(tr("main.show_window"), self)
        show_action.triggered.connect(self.restore_main_window)
        exit_action = QAction(tr("action.exit"), self)
        exit_action.triggered.connect(self._request_exit)
        menu.addAction(show_action); menu.addSeparator(); menu.addAction(exit_action)
        self._tray_icon.setContextMenu(menu)
        self._tray_icon.activated.connect(lambda reason: self.restore_main_window() if reason == QSystemTrayIcon.DoubleClick else None)
        self._tray_icon.show()

    def restore_main_window(self):
        """Shared tray/duplicate-launch activation, preserving window state."""
        if self._shutdown_started:return
        if self.isMinimized():self.showNormal()
        else:self.show()
        hidden=getattr(self,'_tray_hidden_windows',())
        self._tray_hidden_windows=()
        for reference in reversed(hidden):
            window=reference()
            if (window is not None and shiboken6.isValid(window)
                    and window.property('nativeTrayRestorePending')):
                window.setProperty('nativeTrayRestorePending',False)
                window.show()
        self.raise_();self.activateWindow()
        modal=QApplication.activeModalWidget()
        if modal is not None:modal.raise_();modal.activateWindow()

    def _hide_to_tray(self):
        """Hide the owned window group without closing dialog transactions."""
        if getattr(self,'_shutdown_started',False):return
        modal=QApplication.activeModalWidget()
        managed=(getattr(self,'_group_dialog',None),getattr(self,'_preferences_dialog',None))
        if modal is not None and modal not in managed:
            # Hiding an exec/native picker can answer its nested loop. Leave
            # that decision reachable rather than fabricating cancellation.
            self.restore_main_window();return
        visible=[]
        for window in QApplication.topLevelWidgets():
            if window is self or not window.isVisible() or window.windowType() not in (Qt.Window,Qt.Dialog):continue
            ancestor=window.parentWidget();depth=0
            while ancestor is not None and ancestor is not self:
                ancestor=ancestor.parentWidget();depth+=1
            if ancestor is self:visible.append((depth,window))
        pending=list(getattr(self,'_tray_hidden_windows',()))
        for _depth,window in sorted(visible,key=lambda item:item[0],reverse=True):
            reference=weakref.ref(window)
            if not window.property('nativeTrayRestorePending'):
                pending.append(reference)
            window.setProperty('nativeTrayRestorePending',True)
            if isinstance(window,QDialog) and not window.property('nativeTrayFinishedHook'):
                def finished(_result,reference=reference):
                    item=reference()
                    if item is not None and shiboken6.isValid(item):item.setProperty('nativeTrayRestorePending',False)
                window.finished.connect(finished)
                window.setProperty('nativeTrayFinishedHook',True)
            window.hide()
        self._tray_hidden_windows=tuple(pending)
        self.hide()

    def _sync_tray_preferences(self):
        """Tray visibility follows the live policy without rebuilding the UI."""
        enabled = self._engine_preferences.minimize_to_tray or self._engine_preferences.close_to_tray
        if enabled:
            if self._tray_icon is None:
                self._setup_tray_icon()
            elif not self._tray_icon.isVisible():
                self._tray_icon.show()
        elif self._tray_icon is not None:
            self._tray_icon.hide()
            if not self.isVisible():
                self.showNormal()

    def _request_exit(self):
        """Exit explicitly; only the window X may honor close-to-tray."""
        self._force_close = True
        if not self.close():
            self._force_close = False

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            if self.isMinimized():
                self._pause_presentation()
                if self._engine_preferences.minimize_to_tray and self._tray_icon:
                    QTimer.singleShot(0, self._hide_to_tray)
            elif not self._shutdown_started:
                # Normal minimize/restore may retain both widget visibility
                # and its QWindow; showEvent alone is not guaranteed to fire.
                self._schedule_display_rate_visuals()

    def closeEvent(self, event):
        modal=QApplication.activeModalWidget()
        managed=(getattr(self,'_group_dialog',None),getattr(self,'_preferences_dialog',None))
        if modal is not None and modal is not self and modal not in managed:
            # This guard also precedes retrying a partial resource shutdown.
            # An existing question/file picker/worker always owns its result.
            if modal.isMinimized():modal.showNormal()
            modal.raise_();modal.activateWindow()
            event.ignore();return
        if self._shutdown_started:
            try:
                self.begin_shutdown()
            except (OSError,RuntimeError,ValueError) as error:
                NativeMessageBox.warning(self,tr("main.window_title"),tr("runtime.shutdown_failed",error=error))
                event.ignore(); return
            if self._tray_icon: self._tray_icon.hide()
            event.accept(); return
        if getattr(self, "_engine_preferences", None) and self._engine_preferences.close_to_tray and not getattr(self, "_force_close", False):
            self._hide_to_tray()
            event.ignore()
            return
        creator = getattr(getattr(self, '_group_dialog', None), 'instrument_creator', None)
        if creator is not None and not creator.can_close(self.restart if self._restart_requested else self._request_exit):
            event.ignore()
            return
        if modal is not None and modal is not self:
            # Same path for X,taskbar Close and explicit Exit. Finish the
            # ordinary owned transaction before final save/resource barriers.
            if not modal.close():
                event.ignore();return
        if not self._confirm_unsaved_group_loss("restart" if self._restart_requested else "exit"):
            event.ignore()
            return
        if not self._confirm_unsaved_recording("restart" if self._restart_requested else "exit"):
            event.ignore()
            return
        try:
            self._save_midi_cc_configuration()
            if self._ui_save_timer.isActive():
                self._ui_save_timer.stop()
                self._save_ui_values()
            self._settings_writer.flush(retry_failed=True)
        except (OSError, ValueError) as exc:
            NativeMessageBox.warning(self, tr("preferences.title"), tr("runtime.settings_save_failed", error=exc))
            event.ignore()
            return
        try:
            self.begin_shutdown()
        except (OSError,RuntimeError,ValueError) as error:
            NativeMessageBox.warning(self,tr("main.window_title"),tr("runtime.shutdown_failed",error=error))
            event.ignore(); return
        if self._tray_icon:
            self._tray_icon.hide()
        event.accept()




def run_live(*, instance=None) -> int:
    """Run the Native UI with the existing, proven BASS/MIDI controller."""
    from windows_integration import configure_taskbar_identity
    configure_taskbar_identity()
    from error_reporting import current_reporter
    reporter=current_reporter()
    if reporter is not None:reporter.install_qt_hook()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(PRODUCT_NAME)
    app.setApplicationVersion(PRODUCT_VERSION)
    app.setFont(native_text_font(METRICS["body_font_px"]))
    app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    # Composition root: the view receives its audio/session adapters. The
    # adapters have no Qt widget dependency and can be exercised separately.
    session = AudioSession(
        bass, bassmidi, ae, audio_output,
    )
    events = EffectEvents(
        bassmidi, lambda: ae.runtime.stream_handle,
        lambda operation: ae.append_log(log_text(
            "runtime.effect_event_failed", operation=operation,
            error=bass.BASS_ErrorGetCode(),
        )),
    )
    devices = AudioDeviceCatalog(bass, audio_output)
    telemetry = AudioTelemetry(bass, ae)
    try:
        window = NativeLiveWindow(
            audio_session=session, effect_events=events,
            device_catalog=devices, audio_telemetry=telemetry,
            native_session_builder=prepare_live_native_session,
        )
    except BaseException as error:
        # Constructor failure precedes the normal aboutToQuit lifetime owner.
        # Stop/join readers before releasing the session, then propagate the
        # actual error to app.main; no partial window masquerades as running.
        for cleanup in (telemetry.pause_observation,session.close):
            try: cleanup()
            except Exception as cleanup_error:
                error.add_note(tr("runtime.startup_cleanup_failed",error=cleanup_error))
        raise
    shutdown_queued = False

    def shutdown_and_quit():
        """Leave the Qt event loop through the same path as the Close button."""
        nonlocal shutdown_queued
        shutdown_queued = True
        window._force_close = True
        if window.close():
            app.quit()
        else:
            window._force_close = False
            shutdown_queued = False

    def request_quit(_signum, _frame):
        # Do not call QApplication.quit() from the signal handler while a
        # paint/update callback may still be on the stack. Queue the normal
        # QWidget close sequence at the next safe Qt event-loop boundary.
        nonlocal shutdown_queued
        if not shutdown_queued:
            shutdown_queued = True
            QTimer.singleShot(0, shutdown_and_quit)

    previous_sigint = signal.signal(signal.SIGINT, request_quit)
    activation_timer=QTimer(window)
    if instance is not None:
        def activate_existing():
            if shutdown_queued or window._shutdown_started:return
            if instance.consume_activation():
                window.restore_main_window()
        activation_timer.timeout.connect(activate_existing)
        activation_timer.start(250)
    app.aboutToQuit.connect(window.begin_shutdown)
    if window._engine_preferences.start_minimized:
        window.showMinimized()
        # A minimized window may never receive a paint event. It still needs
        # audio/MIDI initialization, but has no visible parent to precede.
        if window._startup_devices_pending:
            window._startup_devices_pending = False
            QTimer.singleShot(0, window._start_initial_devices)
    else:
        window.show()
    try:
        result = app.exec()
    finally:
        activation_timer.stop()
        window.begin_shutdown()
        signal.signal(signal.SIGINT, previous_sigint)
    if window._restart_requested:
        try:
            # The replacement waits for this process to finish normally, so
            # QApplication/Qt thread storage are not torn down inside a slot.
            spawn_restart_after_exit()
        except OSError as exc:
            print(tr("error.restart_failed", error=exc), file=sys.stderr)
            return 1
    return result


if __name__ == "__main__":
    from windows_integration import run_single_application
    raise SystemExit(run_single_application(lambda instance:run_live(instance=instance)))

