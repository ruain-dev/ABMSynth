"""Shared Native Qt palette, controls, meters, and table specifications.

These widgets own presentation only. Audio/session handles and effect state
remain in runtime modules; the main window composes these primitives.
"""

from __future__ import annotations

import math
import json
import logging
import os
import re
import time
import threading
import weakref
import ctypes
from ctypes import wintypes
import shiboken6
from contextlib import contextmanager
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from dataclasses import dataclass, replace
from functools import lru_cache
from html import escape
from PySide6.QtGui import QTextDocument, QEnterEvent, QHoverEvent, QWheelEvent
from PySide6.QtGui import QSurface
from PySide6.QtGui import QTransform
from pathlib import Path

import numpy as np
from numpy import fft as _visual_fft  # Initialization precedes live audio cadence.
from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QEasingCurve, QItemSelectionModel, QLibraryInfo, QLocale, QMimeData, QObject, QPoint, QPointF, QRect, QRectF, QSize, QPropertyAnimation, QTranslator, QVariantAnimation, Property, Qt, QTimer, Signal, QMetaObject, Q_ARG, QThread
from PySide6.QtGui import QAccessible, QAccessibleEvent, QAccessibleActionInterface, QAccessibleValueInterface, QAccessibleValueChangeEvent, QColor, QContextMenuEvent, QCursor, QDoubleValidator, QDrag, QFont, QFontDatabase, QFontMetrics, QFontMetricsF, QIcon, QIconEngine, QImage, QIntValidator, QPainter, QPainterPath, QPalette, QPen, QPicture, QPixmap, QPolygonF, QStyleHints
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication, QAccessibleWidget, QAbstractItemView, QAbstractSpinBox, QAbstractSlider, QButtonGroup, QCheckBox, QComboBox, QFrame, QHeaderView,
    QDial, QDialog, QDialogButtonBox, QGraphicsOpacityEffect, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu, QPushButton,
    QSizePolicy, QSlider, QSpinBox, QDoubleSpinBox, QVBoxLayout, QLayout, QTabBar, QTabWidget,
    QStyle, QStylePainter, QStyledItemDelegate, QStyleOption, QStyleOptionButton, QStyleOptionComboBox, QStyleOptionSlider, QStyleOptionTab,
    QStyleOptionViewItem, QToolButton, QToolTip, QWidget, QMessageBox,
)
from effect_presentation import (effect_tooltip, format_effect_value, numeric_entry_domain, knob_input_steps,
    audio_fx_visual_plot, audio_fx_visual_edit, audio_fx_parameter_caption, audio_fx_parameter_value)
from event_log import log_text
from i18n import DEFAULT_LOCALE, active_locale, load_catalog, tr
from windows_integration import ui_animations_enabled
from native_layout import CARD_GAP, CARD_ROW_GAPS, FORM_ROW_HEIGHT, FORM_COLUMN_GAP, COMPACT_CONTENT_GAP, TAB_PAGE_INSET, COMPACT_TAB_PAGE_INSET, ElasticLabel, ElasticPanel, install_three_column_form_row, fit_plain_dialog_contents, invalidate_layout_chain, set_layout, automatic_window_fit_allowed
from soundfont_files import DEFAULT_SCAN_BUDGET
from audio_output import PlaybackLampPulse

MIDI_ACTIVITY_INTERVAL_MS = 10


@dataclass(frozen=True)
class NativePalette:
    window: str
    surface: str
    surface_alt: str
    input: str
    border: str
    border_subtle: str
    text: str
    label: str
    muted: str
    accent: str
    accent_hover: str
    accent_soft: str
    interaction_hover: str
    success: str
    warning: str
    danger: str
    meter_lane: str
    meter_grid: str
    load_track: str
    load_safe: str
    load_warning: str
    load_danger: str
    meter_safe: str
    meter_warning: str
    meter_danger: str
    selection: str = "#0f6cbd"
    primary_text: str = "#ffffff"


LIGHT = NativePalette(
    window="#f4f6f8", surface="#ffffff", surface_alt="#edf1f5",
    input="#f8fafc", border="#d7dde5", border_subtle="#e7ebf0",
    text="#242424", label="#424242", muted="#505a66", accent="#086bd8", accent_hover="#0759b3",
    accent_soft="#d7e7f9", interaction_hover="#e4effb", success="#15803d", warning="#b7791f", danger="#c2413b",
    meter_lane="#93abc0", meter_grid="#60768b",
    load_track="#aab9c7", load_safe="#00db66", load_warning="#ff9d20", load_danger="#ff6551",
    meter_safe="#00db66", meter_warning="#ff9d20", meter_danger="#ff6551",
    selection="#086bd8",
)
DARK = NativePalette(
    window="#171a1f", surface="#20242b", surface_alt="#292f38",
    input="#1b2027", border="#39414c", border_subtle="#2c333d",
    text="#f5f5f5", label="#d6d6d6", muted="#b6c0cb", accent="#3297ff", accent_hover="#55a9ff",
    accent_soft="#264b73", interaction_hover="#243e5c", success="#4ade80", warning="#f4c75e", danger="#fb7770",
    meter_lane="#3b5062", meter_grid="#91a5b8",
    load_track="#303c47", load_safe="#00a149", load_warning="#cf7c00", load_danger="#ec3d00",
    meter_safe="#00a149", meter_warning="#cf7c00", meter_danger="#ec3d00",
    selection="#1c558f", primary_text="#171a1f",
)


# Names are locale identities; hue is independent of Light/Dark mode.
COLOR_THEMES = {
    "blue": ("#086bd8", "#3297ff"), "teal": ("#007f87", "#00c9c7"),
    "green": ("#14813c", "#3ddc84"), "purple": ("#7c3bc4", "#b178ff"),
    "rose": ("#c12658", "#ff699e"), "amber": ("#906000", "#ffc12e"),
}
_COLOR_PALETTES = {}


def _blend_colour(first, second, amount):
    a, b = QColor(first), QColor(second)
    return QColor(*(round(x * (1 - amount) + y * amount) for x, y in zip(a.getRgb()[:3], b.getRgb()[:3]))).name()


def disabled_control_color(palette):
    """The same legible, inactive ink for styled and custom-painted controls."""
    return _blend_colour(palette.label,palette.surface,.48)


def command_state_colors(palette, *, role='normal', enabled=True, hovered=False, pressed=False, checked=False):
    """One existing command-state policy for QSS and painted icon commands.

    This does not opt any widget into hover. Geometry, glyphs and exclusive
    segmented navigation remain their own controls' responsibility.
    """
    p=palette
    if not enabled:return p.surface_alt,p.border_subtle,disabled_control_color(p)
    if role=='flat':
        return (p.accent_soft if pressed or checked else p.interaction_hover if hovered else None,
                p.accent,p.accent if checked else p.text)
    if role=='primary':
        if pressed:return p.accent_soft,p.accent,p.text
        return (p.accent_hover if hovered else p.accent,
                p.accent_hover if hovered else p.accent,p.primary_text)
    if checked:
        return p.accent_soft,p.accent_hover if hovered else p.accent,p.accent_hover if QColor(p.window).lightness()>128 else p.accent
    return (p.accent_soft if pressed else p.surface if hovered else p.surface_alt,
            p.accent_hover if hovered and not pressed else p.border,p.text)


def native_command_stylesheet(palette):
    """One state stylesheet; callers select roles rather than copying rules."""
    def rule(selectors,role='normal',**state):
        fill,border,ink=command_state_colors(palette,role=role,**state)
        return ', '.join(selectors)+f' {{ background: {fill}; border-color: {border}; color: {ink}; }}'
    primary=('QPushButton#PrimaryButton',)
    rules=[]
    normal=('QPushButton','QToolButton')
    rules.extend((rule(normal),rule([s+':hover:!pressed:enabled' for s in normal],hovered=True),
        rule([s+':pressed:enabled' for s in normal],pressed=True),
        rule([s+':disabled' for s in normal],enabled=False)))
    latched=('QPushButton#ChannelMonitorAction','QPushButton#DebugCapture','QToolButton#FilterButton')
    rules.extend((rule([s+':checked:enabled' for s in latched],checked=True),
        rule([s+':checked:hover:enabled' for s in latched],checked=True,hovered=True),
        rule([s+':disabled' for s in latched],enabled=False)))
    rules.extend((rule(primary,'primary'),
        rule([s+':hover:!pressed:enabled' for s in primary],'primary',hovered=True),
        rule([s+':pressed:enabled' for s in primary],'primary',pressed=True),
        rule([s+':disabled' for s in primary],'primary',enabled=False)))
    # Custom icon painters own ALL chrome,not a second command stylesheet.
    flat='QPushButton[nativeFlatCommand="true"]'
    states=('',':enabled',':disabled',':hover:enabled',':pressed:enabled',':focus:enabled')
    rules.append(', '.join(flat+state for state in states)+
        ' { background: transparent; border: none; padding: 0px; }')
    return '\n'.join(rules)


def native_item_hover_stylesheet(palette, *selectors):
    """One row-hover precedence: a pointer never erases selected state."""
    return ', '.join(selector+':hover:!selected:enabled' for selector in selectors)+f' {{ background: {palette.interaction_hover}; }}'


def prepare_flat_command(widget, *, quiet=False):
    """Declare custom background ownership before inherited QSS is polished."""
    widget.setProperty('nativeFlatCommand',True)
    widget.setProperty('nativeQuietCommand',bool(quiet))
    widget.setFlat(True)
    widget.setAutoFillBackground(False)
    widget.setAttribute(Qt.WA_Hover,True)
    sync_native_command_cursor(widget)


def paint_flat_command_feedback(painter, widget, palette, *, checked=None):
    """All flat command glyphs share one background geometry and state policy."""
    area=QRectF(widget.rect()).adjusted(1,1,-1,-1)
    radius=METRICS['control_corner_radius']
    quiet=bool(widget.property('nativeQuietCommand'))
    fill,_border,ink=command_state_colors(palette,role='flat',enabled=widget.isEnabled(),
        hovered=not quiet and widget.underMouse(),pressed=not quiet and widget.isDown(),
        checked=widget.isChecked() if checked is None else checked)
    def shape():
        painter.drawRoundedRect(area,radius,radius)
    if not quiet and widget.isEnabled() and fill is not None:
        painter.setPen(Qt.NoPen);painter.setBrush(QColor(fill));shape()
    if widget.isEnabled() and widget.hasFocus() and (not quiet or native_keyboard_focus_visible(widget)):
        painter.setPen(QPen(QColor(palette.accent),1));painter.setBrush(Qt.NoBrush);shape()
    return ink


def update_native_keyboard_focus(widget,event):
    """Opt-in focus-cue input mode for custom chart/point painters.

    A click on an already-focused widget produces no new FocusIn. Observe
    the pointer press too, rather than treating hasFocus as a visual mode.
    Temporary popup/window focus restoration retains the previous mode.
    """
    kind=event.type()
    previous=getattr(widget,'_native_keyboard_focus',False)
    if kind==QEvent.FocusIn:
        reason=event.reason()
        current=(True if reason in (Qt.TabFocusReason,Qt.BacktabFocusReason,Qt.ShortcutFocusReason)
                 else False if reason==Qt.MouseFocusReason else previous)
    elif kind in (QEvent.MouseButtonPress,QEvent.MouseButtonDblClick):current=False
    elif kind==QEvent.KeyPress:current=True
    else:return
    if previous!=current:
        widget._native_keyboard_focus=current;widget.update()


def native_keyboard_focus_visible(widget):
    return widget.isEnabled() and widget.hasFocus() and getattr(widget,'_native_keyboard_focus',False)


def set_native_cursor(widget, shape):
    """One availability/reset rule; region owners choose the semantic shape.

    None retires a transient shape using Qt parent inheritance. Commands use
    explicit Arrow while disabled. Qt shapes preserve the OS cursor scheme.
    """
    if shape is None:
        if widget.testAttribute(Qt.WA_SetCursor):widget.unsetCursor()
        return
    shape=shape if widget.isEnabled() else Qt.ArrowCursor
    if not widget.testAttribute(Qt.WA_SetCursor) or QWidget.cursor(widget).shape()!=shape:
        widget.setCursor(shape)


def sync_native_command_cursor(widget):
    set_native_cursor(widget,Qt.ArrowCursor if widget.property('nativeQuietCommand') else Qt.PointingHandCursor)


class NativeWheelAccumulator(QObject):
    """Per-target angle remainder, retired at interaction boundaries; no timer."""

    def __init__(self, widget):
        super().__init__(widget)
        self.context=None;self.remainder=0
        widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Leave,QEvent.Hide,QEvent.FocusOut,
                QEvent.EnabledChange,QEvent.ParentChange,QEvent.WindowDeactivate):
            self.context=None;self.remainder=0
        return False


def native_wheel_steps(widget,event,*,role='value',axis='vertical-first'):
    """Decode semantic detents once; None means let an ancestor handle input.

    Qt documents 120 angle units per detent. Pixel deltas have no universal
    numeric-step meaning: content panning handles those directly instead.
    Content uses delivered direction; values follow Qt slider's physical
    wheel direction, cancelling natural scrolling exactly once.
    """
    state=getattr(widget,'_native_wheel_input',None)
    if state is None:
        state=widget._native_wheel_input=NativeWheelAccumulator(widget)
    delta=event.angleDelta()
    chosen=('y' if delta.y() else 'x') if axis=='vertical-first' else axis
    amount=delta.x() if chosen=='x' else delta.y()
    if role=='value' and event.inverted():amount=-amount
    if not widget.isEnabled():
        state.context=None;state.remainder=0;event.ignore();return None
    if not amount:
        if not delta.isNull() or event.phase() in (Qt.ScrollBegin,Qt.ScrollEnd):
            state.context=None;state.remainder=0
        event.ignore();return None
    context=(role,chosen,event.modifiers(),event.inverted(),event.device().systemId(),amount>0)
    if context!=state.context or event.phase()==Qt.ScrollBegin:
        state.remainder=0;state.context=context
    state.remainder+=amount
    steps=math.trunc(state.remainder/120)
    state.remainder-=steps*120
    if event.phase()==Qt.ScrollEnd:state.context=None;state.remainder=0
    event.accept()  # A partial target gesture must not also scroll its parent.
    return chosen,steps


def dispatch_native_wheel(widget,event,handler):
    """Share event mechanics while Qt retains stepping and activation semantics."""
    decoded=native_wheel_steps(widget,event,axis='vertical-first' if isinstance(widget,QAbstractSlider) else 'y')
    if decoded is None or not decoded[1]:return
    axis,steps=decoded
    repetitions=min(abs(steps),widget.count()) if isinstance(widget,QComboBox) else 1
    delta=(120 if steps>0 else -120) if isinstance(widget,QComboBox) else steps*120
    angle=QPoint(delta,0) if axis=='x' else QPoint(0,delta)
    normalized=QWheelEvent(event.position(),event.globalPosition(),QPoint(),angle,
        event.buttons(),event.modifiers(),event.phase(),False,
        event.source(),event.pointingDevice())
    normalized.setTimestamp(event.timestamp())
    for _ in range(repetitions):
        handler(normalized)
        if not normalized.isAccepted():break
    event.setAccepted(normalized.isAccepted())


def color_palette(dark, name="blue"):
    name = name if name in COLOR_THEMES else "blue"
    base = DARK if dark else LIGHT
    if name == "blue":
        return base  # Canonical palette for standalone bool-themed controls.
    key = bool(dark), name
    if key not in _COLOR_PALETTES:
        accent = COLOR_THEMES[name][int(bool(dark))]
        surface = _blend_colour("#202124" if dark else "#ffffff", accent, .025 if dark else 0)
        window = _blend_colour("#18191c" if dark else "#f5f6f7", accent, .035)
        _COLOR_PALETTES[key] = replace(base, window=window, surface=surface,
            surface_alt=_blend_colour("#2a2b2e" if dark else "#f0f1f2", accent, .06),
            input=_blend_colour("#1b1c1f" if dark else "#fafbfc", accent, .025),
            border=_blend_colour("#414248" if dark else "#d7d9dd", accent, .12),
            border_subtle=_blend_colour("#303136" if dark else "#e8e9ed", accent, .055),
            accent=accent, accent_hover=_blend_colour(accent, "#ffffff" if dark else "#000000", .17),
            accent_soft=_blend_colour(surface, accent, .34 if dark else .16),
            interaction_hover=_blend_colour(surface, accent, .23 if dark else .11),
            selection=_blend_colour(accent, "#000000", .44 if dark else .04),
            primary_text=window if dark else "#ffffff",
            meter_lane=_blend_colour("#414248" if dark else "#9da3aa", accent, .18),
            meter_grid=_blend_colour("#a1a5ac" if dark else "#687078", accent, .10),
            load_track=_blend_colour("#34363a" if dark else "#acb2b9", accent, .12))
    return _COLOR_PALETTES[key]


def widget_palette(widget, dark=None, *, fallback=None):
    """Resolve root colour identity for every custom painter/subwindow.

    A standalone control still supports its existing bool set_theme API.
    This never owns audio state or changes QApplication's global colour mode.
    """
    current, seen = widget, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        # These identities are Python instance state,not Qt meta-properties.
        # Repeated failed QObject dynamic lookups on every paint dominated
        # continuous resizing. Keep the live walk(no stale ancestor cache).
        state=vars(current)
        if "_color_theme" in state:
            return color_palette(state.get("_dark", True) if dark is None else dark,state["_color_theme"])
        owner=state.get("owner")
        if owner is not None and owner is not current:current=owner
        else:
            parent=getattr(current,"parent",None)
            current=parent() if parent is not None else None
    return fallback if fallback is not None and dark is None else color_palette(True if dark is None else dark)


FORM_EDGE_WIDTH = 112

# SoundFont discovery is intentionally bounded.  The executable/source
# directory is a useful local catalogue, but recursively walking an entire
# user profile or drive would make startup unpredictable.  The cap applies
# to automatically discovered entries; a manually chosen current file is
# allowed as one additional entry so it never disappears from the selector.
SOUNDFONT_DISCOVERY_LIMIT = DEFAULT_SCAN_BUDGET.max_files
SOUNDFONT_PICKER_SENTINEL = "__ABMSYNTH_PICK_SOUNDFONT__"
SOUNDFONT_ROW_KIND_ROLE = Qt.UserRole + 1
SOUNDFONT_ROW_FILE = "file"
SOUNDFONT_ROW_CURRENT = "current"
SOUNDFONT_ROW_UNSAVED = "unsaved"
SOUNDFONT_ROW_PICKER = "picker"


class SoundFontDiscoverySignals(QObject):
    completed = Signal(object)


METRICS = {
    "compact_action_height": 20,
    "compact_action_font_px": 11,
    "channel_protocol_font_px": 10,
    "channel_protocol_width": 15,
    "channel_protocol_gap": 2,
    "control_border_width": 1,
    "control_corner_radius": 7,
    "window_width": 900,
    "window_height": 700,
    "outer": 20,
    "section_gap": CARD_GAP,
    "card_padding": 16,
    "compact_card_padding": 12,
    "card_content_gap": 10,
    "compact_content_gap": COMPACT_CONTENT_GAP,
    "form_column_gap": FORM_COLUMN_GAP,
    "form_label_width": FORM_EDGE_WIDTH,
    "form_value_width": FORM_EDGE_WIDTH,
    "form_page_padding": TAB_PAGE_INSET,
    # The group inspector has short labels/values.  Its dedicated compact
    # form spec returns unused edge space to the central adjustment field.
    "inspector_label_width": 86,
    "inspector_value_width": 46,
    "inspector_column_gap": 6,
    "group_inspector_max_width": 420,
    "side_action_gap": 8,
    "popup_item_height": 30,
    "right_column": 188,
    "meter_width": 100,
    "meter_value_font_px": 14,
    "meter_value_chip_height": 22,
    "meter_value_chip_width": 42,
    "meter_readout_top": 4,
    "meter_readout_to_label_gap": 2,
    "meter_label_height": 16,
    "meter_label_to_track_gap": 4,
    "meter_track_to_footer_gap": 4,
    "meter_footer_height": 18,
    # Meter geometry is one scale gutter, two yielding lanes and one unit
    # footer. These are preferred drawing metrics, never width floors.
    "meter_scale_gutter": 30,
    "meter_right_gutter": 8,
    "meter_lane_gap": 6,
    "meter_lane_gap_divisor": 8,
    "meter_tick_label_width": 26,
    "meter_tick_label_height": 18,
    "meter_signal_inset": 2,
    "meter_readout_side_inset": 1,
    "meter_unit_label_width": 44,
    "popup_fade_ms": 120,
    "title_font_px": 20,
    "body_font_px": 14,
    # Native type contract: no live UI text below 12px. Normal text is the
    # project Variable Noto 400; intentional emphasis is 700.
    "small_font_px": 12,
    "card_title_font_px": 15,
    "dialog_title_font_px": 18,
    "tab_font_px": 15,
    "state_caption_font_px": 12,
    "state_value_font_px": 14,
    "engine_load_value_font_px": 12,
    "master_title_font_px": 12,
    "master_value_font_px": 14,
    "meter_tick_font_px": 12,
    "meter_channel_font_px": 12,
    "state_caption_height": 18,
    "state_value_height": 24,
    "state_row_gap": 2,
    "state_card_margin": (16, 10, 16, 10),
    # State cards use the same compact content gap as the other cards.  A
    # local wider divider gutter made POLYPHONY look detached from the grid.
    "state_column_gap": 6,
    "midi_mode_logo_max_height": 22,
    "state_content_height": 44,
    "engine_load_max_width": 240,
    "log_card_margin": (16, 12, 16, 12),
    # The fader rail remains the same width; this only gives its two-line
    # caption enough inline room at the normal preferred width.
    "master_fader_margin": (2, 14, 2, 14),
    "master_caption_preferred_width": 58,
}

# Every live-main section reads its spacing/size reductions from this one
# density preset. Keeping a single active compact density avoids breakpoint
# jumps while leaving room for an explicit future density choice.
LAYOUT_DENSITIES = {
    "compact": {
        "card_padding": 8,
        # The state card has a 6px visible inset (5px layout + 1px border),
        # exactly matching the divider gutters on both sides of each column.
        "state_card_margin": (5, 6, 5, 6),
        "form_page_padding": COMPACT_TAB_PAGE_INSET,
        "log_min_height": 100,
        "log_body_min_height": 58,
    },
}
DEFAULT_LAYOUT_DENSITY = "compact"

# Stable model identities; Memo remains logical11 for existing inline entry,
# but its visual section is moved last after these optional change columns.
INSTRUMENT_EXTRA_FIELDS = (
    "fine_tuning", "coarse_tuning", "vibrato_rate", "vibrato_depth", "vibrato_delay",
    *(f"{source}_{target}" for source in ("modulation", "channel_pressure", "key_pressure")
      for target in ("vibrato", "pitch", "filter", "volume")),
    "scale", *(f"drum_{key}" for key in ("level", "pan", "reverb", "chorus", "fine_tuning", "coarse_tuning", "cutoff", "resonance", "userfx")),
)


@dataclass(frozen=True)
class InstrumentTableSpec:
    """Single source of truth for the SoundFont instrument grid."""

    headers: tuple[str, ...] = (
        "", tr('group.bank_header'), tr('group.program_header'), tr('group.preset_header'), "SoundFont", "Vol.",
        "Att.", "Dec.", "Rel.", "Cut.", "Res.", "Memo",
    ) + tuple(tr(f"group.column_{key}") for key in INSTRUMENT_EXTRA_FIELDS)
    activity_column: int = 0
    activity_width: int = 20
    activity_diameter: int = 12
    trailing_column: int = 11
    font_px: int = 12
    row_height: int = 26
    horizontal_padding: int = 10
    fit_breathing: int = 4

    @property
    def named_columns(self):
        return tuple(range(self.activity_column + 1, len(self.headers)))

    def is_named(self, column):
        return column in self.named_columns

    def font(self):
        font = native_text_font(self.font_px)
        font.setBold(False)
        return font

    def text_rect(self, rect, column=None):
        """Every named header/body cell shares one left-aligned inset."""
        slack_side = self.horizontal_padding - self.fit_breathing
        return rect.adjusted(self.horizontal_padding, 0, -slack_side, 0)

    def alignment(self, column):
        return int(Qt.AlignLeft | Qt.AlignVCenter)

    @staticmethod
    def row_background(palette, row, selected, hovered):
        if selected:
            return palette.accent_soft
        if hovered:
            return palette.interaction_hover
        return palette.surface_alt if row % 2 else palette.surface

    def fitted_width(self, text):
        return (
            QFontMetrics(self.font()).horizontalAdvance(str(text))
            + self.horizontal_padding * 2
        )


INSTRUMENT_TABLE = InstrumentTableSpec()


def format_value(value: int, kind: str) -> str:
    return format_effect_value(value, kind)


TOOLTIP_WRAP_WIDTH_PX = 420


def tooltip_plain_text(text: str) -> str:
    """Recover semantic copy from our wrapped/composed tooltip presentation."""
    if re.search(r"<(?:table|br|p|html|b|div)\b", str(text), re.I):
        document = QTextDocument()
        document.setHtml(str(text))
        return document.toPlainText().strip()
    return str(text)


def tooltip_repeats_caption(text,caption):
    """Compare presentation markup/accelerators,not locale-specific prose."""
    normalize=lambda value:' '.join(tooltip_plain_text(value).replace('&','').split()).rstrip('…').rstrip('.')
    return bool(normalize(caption)) and normalize(text)==normalize(caption)


def native_tooltip_content(watched, position):
    """Resolve the actual pointer region; empty help never falls into a parent."""
    provider=getattr(watched,'native_tooltip_content',None)
    if provider is not None:return provider(position)
    text,rect=watched.toolTip(),watched.rect()
    caption=None;text_rect=watched.contentsRect()
    if isinstance(watched,QMenu):
        action=watched.actionAt(position)
        if (not watched.toolTipsVisible() or action is None or action.menu() is not None
                or action.isSeparator()):return '',rect
        text,rect=action.toolTip(),watched.actionGeometry(action)
        if tooltip_repeats_caption(text,action.text()):return '',rect
    elif isinstance(watched,QTabBar):
        index=watched.tabAt(position)
        if index<0:return '',rect
        text,rect=watched.tabToolTip(index),watched.tabRect(index)
        caption=watched.tabText(index)
        option=QStyleOptionTab();watched.initStyleOption(option,index)
        text_rect=watched.style().subElementRect(QStyle.SE_TabBarTabText,option,watched)
    else:
        parent=watched.parentWidget()
        header=watched if isinstance(watched,QHeaderView) else parent if isinstance(parent,QHeaderView) and watched is parent.viewport() else None
        if header is not None:
            index=header.logicalIndexAt(position)
            if index<0 or header.model() is None:return '',rect
            rect=(QRect(header.sectionViewportPosition(index),0,header.sectionSize(index),watched.height())
                  if header.orientation()==Qt.Horizontal else
                  QRect(0,header.sectionViewportPosition(index),watched.width(),header.sectionSize(index)))
            text=header.model().headerData(index,header.orientation(),Qt.ToolTipRole) or header.toolTip()
            caption=str(header.model().headerData(index,header.orientation(),Qt.DisplayRole) or '')
            text_rect=rect.adjusted(INSTRUMENT_TABLE.horizontal_padding,0,-INSTRUMENT_TABLE.horizontal_padding,0)
        elif isinstance(parent,QAbstractItemView) and watched is parent.viewport():
            index=parent.indexAt(position)
            if not index.isValid():return '',rect
            text,rect=index.data(Qt.ToolTipRole) or text or parent.toolTip(),parent.visualRect(index)
            visible=str(index.data(Qt.DisplayRole) or '')
            if not getattr(parent,'wordWrap',lambda:False)():
                available=max(0,rect.width()-2*INSTRUMENT_TABLE.horizontal_padding)
                fits=parent.fontMetrics().horizontalAdvance(visible)<=available
                if fits and tooltip_repeats_caption(text,visible):return '',rect
                if not fits and not text:text=visible
            # A valid cell's own help/full text never inherits an unrelated
            # ancestor when it intentionally resolves to no tooltip.
            if not text:return '',rect
    if not text:
        # Preserve Qt's ordinary inherited help for a compound control's
        # passive child. Region-bearing views above deliberately return empty
        # instead, so blank data space cannot inherit an unrelated caption.
        ancestor=watched.parentWidget()
        while ancestor is not None:
            text=ancestor.toolTip()
            if text:break
            if ancestor.isWindow():break
            ancestor=ancestor.parentWidget()
    if caption is None:
        if isinstance(watched,(QLabel,QAbstractButton)):
            caption=watched.text()
            if isinstance(watched,QLabel):text_rect=text_rect.adjusted(watched.margin(),0,-watched.margin(),0)
            elif isinstance(watched,QPushButton):
                option=QStyleOptionButton();watched.initStyleOption(option)
                text_rect=watched.style().subElementRect(QStyle.SE_PushButtonContents,option,watched)
                if not watched.icon().isNull():text_rect.adjust(watched.iconSize().width()+4,0,0,0)
            elif isinstance(watched,QCheckBox):
                if isinstance(watched,NativeToggleSwitch):text_rect.adjust(watched.TRACK_WIDTH+8,0,0,0)
                else:
                    option=QStyleOptionButton();watched.initStyleOption(option)
                    text_rect=watched.style().subElementRect(QStyle.SE_CheckBoxContents,option,watched)
        elif isinstance(watched,QLineEdit):
            caption=watched.text() if watched.echoMode()==QLineEdit.Normal else None
            text_rect=watched.contentsRect().marginsRemoved(watched.textMargins()).adjusted(2,0,-2,0)
        elif isinstance(watched,QComboBox):
            caption=watched.currentText()
            option=QStyleOptionComboBox();watched.initStyleOption(option)
            text_rect=watched.style().subControlRect(QStyle.CC_ComboBox,option,QStyle.SC_ComboBoxEditField,watched)
    if text and caption:
        # This is presentation filtering,not a catalog-language heuristic.
        # Retain full text when actual layout pressure clips the caption.
        if tooltip_repeats_caption(text,caption):
            plain=tooltip_plain_text(caption).replace('&','')
            if '\n' not in plain and watched.fontMetrics().horizontalAdvance(plain)<=text_rect.width():return '',rect
    return text,rect


def handle_native_tooltip(watched,event):
    """Single dispatcher used by the app filter and standalone native controls."""
    # A queued help wake-up can outlive Hide (including a hidden ancestor).
    # Disabled visible controls may still explain why they are unavailable.
    text,rect=native_tooltip_content(watched,event.pos()) if watched.isVisible() else ('',watched.rect())
    if text:show_native_tooltip(event.globalPos(),text,watched,rect)
    else:QToolTip.hideText()
    event.accept()
    return True


class NativeTooltipWrapFilter(QObject):
    """Resolve all help before Qt measures and positions its tooltip window."""

    def eventFilter(self, watched, event):
        if event.type()!=QEvent.ToolTip or not isinstance(watched,QWidget):return False
        return handle_native_tooltip(watched,event)


def install_tooltip_wrap_filter(app):
    if not isinstance(app, QApplication):
        return
    if not hasattr(app, "_native_tooltip_wrap_filter"):
        app._native_tooltip_wrap_filter = NativeTooltipWrapFilter(app)
        app.installEventFilter(app._native_tooltip_wrap_filter)


def tooltip_display_text(text: str) -> str:
    """Wrap long localized help using the active Qt tooltip font."""
    plain = str(text).replace("\r\n", "\n").replace("\r", "\n")
    widest_line = max(
        (QFontMetrics(QToolTip.font()).horizontalAdvance(line) for line in plain.split("\n")),
        default=0,
    )
    if widest_line <= TOOLTIP_WRAP_WIDTH_PX:
        return plain
    escaped = escape(plain).replace("\n", "<br/>")
    return (
        f'<table width="{TOOLTIP_WRAP_WIDTH_PX}" cellspacing="0" cellpadding="0">'
        f'<tr><td width="{TOOLTIP_WRAP_WIDTH_PX}" style="white-space: normal;">{escaped}</td></tr>'
        "</table>"
    )


def show_native_tooltip(position, text, widget, rect=None):
    """One pre-layout help formatting and pointer-anchoring boundary."""
    plain=tooltip_plain_text(text)
    rendered=tooltip_display_text(plain)
    rect=widget.rect() if rect is None else rect
    app=QApplication.instance()
    previous=getattr(app,'_native_tooltip_target',None)
    identity=(weakref.ref(widget),rect.getRect(),plain)
    # Qt keeps unchanged text in place. Only a genuinely different target
    # needs a fresh anchor; repeated help at the same target must not flicker.
    if previous!=identity and QToolTip.isVisible():QToolTip.hideText()
    app._native_tooltip_target=identity
    QToolTip.showText(position,rendered,widget,rect)


def install_effect_tooltip(text: str | None, *widgets: QWidget) -> None:
    """Apply one supplemental help string across a compact control row.

    A row can be entered through its caption, track, numeric readout, or spare
    trailing cell.  Every hover target must therefore offer the same help.
    The accessibility description mirrors the Korean explanation while the
    slider's raw numeric value remains available through Qt's standard role.
    """
    if not text:
        return
    accessible_text = text.replace("\n", " ")
    displayed_text = tooltip_display_text(text)
    for widget in widgets:
        widget.setToolTip(displayed_text)
        widget.setAccessibleDescription(accessible_text)


UI_REGULAR_WEIGHT = QFont.Weight.Normal
UI_BOLD_WEIGHT = QFont.Weight.Bold
MIN_UI_FONT_PX = 12
# Main effect tabs and the status log retain their own font metrics rather
# than using the application-wide font normalizer.
EFFECT_TAB_FONT_PX = METRICS["tab_font_px"]
STATUS_LOG_FONT_PX = 12
NOTO_VARIABLE_FONT_PATH = Path(__file__).with_name("NotoSansKR-VariableFont_wght.ttf")
PROJECT_NOTO_STATIC_FONTS = {
    400: Path(__file__).with_name("fonts") / "NotoSansKR-wght400.ttf",
    500: Path(__file__).with_name("fonts") / "NotoSansKR-wght500.ttf",
    600: Path(__file__).with_name("fonts") / "NotoSansKR-wght600.ttf",
    700: Path(__file__).with_name("fonts") / "NotoSansKR-wght700.ttf",
}
_project_noto_family: str | None = None
_native_font_templates = OrderedDict()


def native_raster_font(font):
    """One raster policy;preserve the caller's typography and tracking."""
    font=QFont(font)
    font.setStyleStrategy(QFont.PreferAntialias)
    font.setHintingPreference(QFont.PreferFullHinting)
    return font


def apply_native_font_raster(widget):
    current=widget.font()
    if (current.hintingPreference()==QFont.PreferFullHinting
            and current.styleStrategy()==QFont.PreferAntialias):return
    widget.setFont(native_raster_font(current))


def native_text_font(pixel_size=None, weight=UI_REGULAR_WEIGHT):
    """Return the project Noto static face for every active UI surface."""
    family=project_noto_family()
    key=(family,max(MIN_UI_FONT_PX,int(pixel_size)),int(weight)) if pixel_size is not None else None
    if key in _native_font_templates:
        _native_font_templates.move_to_end(key)
        return QFont(_native_font_templates[key])
    font = native_raster_font(QFont(family))
    # Qt6.11 GDI database otherwise selects DirectWrite for DefaultHinting
    # when ANY screen raises QApplication DPR, even on a100% text surface.
    # Retain the pre-PerMonitor GDI text raster without undoing DPI ownership.
    font.setWeight(QFont.Weight(int(weight)))
    if pixel_size is not None:
        font.setPixelSize(max(MIN_UI_FONT_PX, int(pixel_size)))
        _native_font_templates[key]=QFont(font)
        while len(_native_font_templates)>64: _native_font_templates.popitem(last=False)
    return font


def project_noto_family() -> str:
    """Register the bundled static Noto Sans KR font faces."""
    global _project_noto_family
    if _project_noto_family is not None:
        return _project_noto_family
    families = []
    for weight, path in PROJECT_NOTO_STATIC_FONTS.items():
        font_id = QFontDatabase.addApplicationFont(str(path)) if path.is_file() else -1
        registered = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        if not registered:
            raise RuntimeError(f"Project Noto {weight} face is missing or unreadable: {path}")
        families.append(registered[0])
    _project_noto_family = families[0]
    registered_styles = set(QFontDatabase.styles(_project_noto_family))
    required_styles = {"Regular", "Medium", "SemiBold", "Bold"}
    if not required_styles.issubset(registered_styles):
        raise RuntimeError(
            f"Project Noto styles missing under {_project_noto_family}: "
            f"expected {sorted(required_styles)}, got {sorted(registered_styles)}"
        )
    return _project_noto_family


class NativeWindowMessageFilter(QAbstractNativeEventFilter):
    """One native boundary;geometry and resize painting have separate owners."""
    def __init__(self):
        super().__init__()
        self.windows = weakref.WeakValueDictionary()
        self.backdrops = weakref.WeakValueDictionary()
        from windows_integration import NativeResizeBackdrop
        self._backdrop_messages=NativeResizeBackdrop.MESSAGES

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) == b'windows_generic_MSG':
            native = wintypes.MSG.from_address(int(message))
            if native.message in self._backdrop_messages:
                surface=self.backdrops.get(int(native.hWnd or 0))
                if surface is not None and shiboken6.isValid(surface):
                    surface.native_message(int(native.hWnd or 0),native.message,int(native.wParam))
            if native.message == 0x0214 or (native.message==0x0112 and int(native.wParam)&0xfff0 in (0xf000,0xf030,0xf120)):
                # WinUser.h WM_SIZING or WM_SYSCOMMAND SC_SIZE/MAXIMIZE/RESTORE.
                guard = self.windows.get(int(native.hWnd or 0))
                if guard is not None and shiboken6.isValid(guard):
                    guard._remember_geometry_choice()
            elif native.message==0x0005 and int(native.wParam)==0: # WM_SIZE/SIZE_RESTORED.
                guard=self.windows.get(int(native.hWnd or 0))
                if guard is not None and shiboken6.isValid(guard):
                    packed=int(native.lParam)
                    guard._native_client_resize(packed&0xffff,(packed>>16)&0xffff)
        return False, 0


def native_window_message_filter():
    app=QApplication.instance()
    if not hasattr(app,'_native_window_message_filter'):
        app._native_window_message_filter=NativeWindowMessageFilter()
        app.installNativeEventFilter(app._native_window_message_filter)
    return app._native_window_message_filter


class NativeResizeBackgroundGuard(QObject):
    """Supply appearance/lifetime to a Win32-only new-strip paint owner."""
    def __init__(self,window):
        super().__init__(window)
        from windows_integration import NativeResizeBackdrop
        self.backdrop=NativeResizeBackdrop();self._hwnd=None
        self.destroyed.connect(self.backdrop.cancel)
        window.installEventFilter(self);self._sync(window)

    def _sync(self,window):
        native=native_window_message_filter()
        hwnd=int(window.effectiveWinId() or 0) or None
        if self._hwnd!=hwnd:
            if self._hwnd is not None and native.backdrops.get(self._hwnd) is self:
                native.backdrops.pop(self._hwnd,None)
            self.backdrop.cancel();self._hwnd=hwnd
        if hwnd:native.backdrops[hwnd]=self
        handle=window.windowHandle()
        eligible=(window.isWindow() and window.windowType() in (Qt.Window,Qt.Dialog)
            and not window.windowFlags()&Qt.FramelessWindowHint
            and not window.testAttribute(Qt.WA_TranslucentBackground)
            and window.windowOpacity()==1.0
            and not window.windowState()&(Qt.WindowMinimized|Qt.WindowMaximized|Qt.WindowFullScreen)
            and (handle is None or handle.surfaceType()==QSurface.RasterSurface))
        colour=QColor(widget_palette(window).window)
        self.backdrop.set_background((colour.red()|(colour.green()<<8)|(colour.blue()<<16)) if eligible else None)

    def native_message(self,hwnd,message,wparam):
        if hwnd!=self._hwnd:return 0
        if message==0x0231 or message==0x0014 and self.backdrop._resizing:
            # Opacity has no QWidget change event. Recheck at entry and just
            # before a fill,without changing native/Qt geometry or painting Qt.
            self._sync(self.parent())
        return self.backdrop.message(hwnd,message,wparam)

    def eventFilter(self,window,event):
        kind=event.type()
        if kind in (QEvent.Show,QEvent.WinIdChange,QEvent.PaletteChange,QEvent.StyleChange,
                QEvent.WindowStateChange,QEvent.DevicePixelRatioChange):
            self.backdrop.cancel();self._sync(window)
        elif kind in (QEvent.Hide,QEvent.Close,QEvent.WindowDeactivate):self.backdrop.cancel()
        elif kind in (QEvent.UpdateRequest,QEvent.Paint) and self._hwnd is not None:
            # Qt can present from UpdateRequest without a native WM_PAINT.
            # Advance the conservative extent so a later erase never colours
            # over content drawn through that independent normal Qt path.
            if self.backdrop._resizing:self.backdrop.message(self._hwnd,0x000f)
        return False


class NativeDpiClientSizeGuard(QObject):
    """Preserve actual client DIPs across one native Windows DPR correction.

    Qt owns awareness and outer-frame placement. This owns no preferred size,
    floor, audio resource or perpetual timer; later resizing always wins.
    """
    def __init__(self, window):
        super().__init__(window)
        self._ratio = window.devicePixelRatioF()
        self._target = None
        self._native_resize_seen = False
        self._revision = 0
        self._hwnd = None
        window.installEventFilter(self)

    def _bind_native_sizing(self, window):
        native = native_window_message_filter()
        # effectiveWinId never creates a hidden/bootstrap native window.
        hwnd = int(window.effectiveWinId() or 0)
        mapping = native.windows
        if self._hwnd is not None and mapping.get(self._hwnd) is self:
            mapping.pop(self._hwnd, None)
        self._hwnd = hwnd or None
        if hwnd: mapping[hwnd] = self

    def _cancel(self):
        self._revision += 1
        self._target = None

    def _remember_geometry_choice(self):
        """Both explicit user sizing and external reductions own this window."""
        self._cancel()
        window=self.parent()
        window.setProperty('nativeUserGeometryChosen',True)
        for motion in window.findChildren(NativeWindowSizeMotion):
            if motion._started_at is not None:motion.cancel_external_resize()

    def _native_client_resize(self,width,height):
        # WinUser.h WM_SIZE delivers client pixels before Qt's queued Resize.
        # An animation can otherwise overwrite an external reduction first.
        # Do not consume the message or classify our own Qt fits/DPI correction.
        window=self.parent()
        if self._target is not None or not window.isVisible() or window.windowState():return
        if max(window.width(),window.height())*window.devicePixelRatioF()>0xffff:return # WM_SIZE packs16-bit dimensions;Qt fallback handles larger windows.
        motions=[motion for motion in getattr(window,'_native_size_motions',()) if motion._started_at is not None]
        if not motions or any(motion._applying_size for motion in motions):return
        ratio=window.devicePixelRatioF()
        if width<round(window.width()*ratio) or height<round(window.height()*ratio):
            self._remember_geometry_choice()

    def eventFilter(self, window, event):
        if not window.isWindow(): return False
        kind = event.type()
        if kind in (QEvent.Show, QEvent.WinIdChange):
            self._bind_native_sizing(window)
        elif kind == QEvent.DevicePixelRatioChange:
            ratio = window.devicePixelRatioF()
            if ratio != self._ratio:
                self._ratio = ratio
                self._cancel()
                if window.isVisible() and not window.windowState() and window.windowType() in (Qt.Window, Qt.Dialog):
                    self._target = QSize(window.size())
                    self._native_resize_seen = False
                    revision = self._revision
                    # Cleanup watchdog only, not the geometry/animation clock.
                    # A missing native correction must never capture a later resize.
                    QTimer.singleShot(1000, self, lambda: self._expire(revision))
        elif kind == QEvent.Resize:
            if self._target is not None:
                if not event.spontaneous() or self._native_resize_seen:
                    self._cancel()
                else:
                    self._native_resize_seen = True
                    # Windows posts the native client Resize after the DPR
                    # notification; a timer queued on DPR can run too early.
                    revision = self._revision
                    QTimer.singleShot(0, self, lambda: self._finish(revision))
            elif (event.spontaneous() and window.isVisible() and not window.windowState()
                  and event.oldSize().isValid()
                  and (event.size().width()<event.oldSize().width()
                       or event.size().height()<event.oldSize().height())):
                # QEvent::spontaneous distinguishes an OS resize from QWidget
                # resize()/our motion. External window management need not send
                # WM_SIZING. Respect its smaller workspace even at scroll0.
                # A known DPR correction above keeps its original precedence.
                self._remember_geometry_choice()
        elif kind in (QEvent.Hide, QEvent.Close, QEvent.WindowStateChange):
            self._cancel()
        return False

    def _expire(self, revision):
        if revision == self._revision: self._cancel()

    def _finish(self, revision):
        if revision != self._revision: return
        window = self.parent()
        target, self._target = self._target, None
        if target is not None and window.isVisible() and not window.windowState() and window.size() != target:
            window.resize(target)


def install_native_dpi_guard(root):
    """Shared late-creation path, independent of whether fonts need a sweep."""
    if (root.isWindow() and root.windowType() in (Qt.Window, Qt.Dialog)
            and QApplication.platformName() == 'windows'
            and not hasattr(root, '_native_dpi_size_guard')):
        root._native_dpi_size_guard = NativeDpiClientSizeGuard(root)


def apply_native_text_metrics(root):
    """Reapply the project Variable Noto 400 after QSS, never below 12px."""
    install_native_dpi_guard(root)
    for widget in [root, *root.findChildren(QWidget)]:
        if isinstance(widget, NativeMeter):
            continue
        # QLabel defaults to AutoText. User data such as file names, preset
        # names and device errors must never be parsed as accidental markup.
        # Rich text is opt-in at the few documented presentation surfaces.
        if isinstance(widget, QLabel) and widget.textFormat() == Qt.AutoText:
            widget.setTextFormat(Qt.PlainText)
        size = widget.font().pixelSize()
        intended = native_text_font(size if size > 0 else METRICS["body_font_px"])
        if widget.font() != intended:
            widget.setFont(intended)


_theme_spacing_pending=ContextVar('abmsynth_theme_spacing_pending',default=None)


def flush_native_theme_spacing():
    """Resolve final stable fonts before measured fit/paint, once per label."""
    pending=_theme_spacing_pending.get()
    if not pending: return
    from shiboken6 import isValid
    labels=tuple(pending.values()); pending.clear()
    token=_theme_spacing_pending.set(None)
    try:
        for label in labels:
            if isValid(label): label._fit_spacing()
    finally: _theme_spacing_pending.reset(token)


@contextmanager
def native_theme_batch(root):
    """GUI-only paint transaction; preserve independently disabled windows.

    QWidget updatesEnabled does not replace layout/style work, and re-enabling
    schedules a repaint. Owned top-levels need their own restoration. Snapshot
    flags BEFORE disabling a parent so inherited false is not mistaken for an
    explicit caller choice. No processEvents/reentrancy inside the transaction.
    """
    windows=[root,*(w for w in root.findChildren(QWidget) if w.isWindow())]
    states=[(w,w.updatesEnabled()) for w in windows]
    previous=_theme_spacing_pending.get()
    token=_theme_spacing_pending.set({} if previous is None else previous)
    try:
        for window,enabled in states:
            if enabled: window.setUpdatesEnabled(False)
        yield
    finally:
        try:
            if previous is None: flush_native_theme_spacing()
        finally:
            _theme_spacing_pending.reset(token)
            for window,enabled in states:
                if enabled: window.setUpdatesEnabled(True)


class NativeFrameworkCopyTranslator(QTranslator):
    """Catalog overlay for Qt source identities absent from shipped qtbase QM.

    Tuple keys are Qt's context/source translation protocol, not application
    lookup IDs. Keep product copy behind semantic catalog IDs. Unhandled
    strings fall through to the official translator installed below this one.
    """
    COPY_IDS = {
        ('QFileDialog', '&Look in:'): 'qt_widgets.file_location',
        ('QFileDialog', 'Look in:'): 'qt_widgets.file_location',
        ('QFileDialog', 'Files of &type:'): 'qt_widgets.file_type',
        ('QFileDialog', 'Files of type:'): 'qt_widgets.file_type',
    }

    def isEmpty(self):
        return False

    def translate(self, context, sourceText, disambiguation=None, n=-1):
        key = self.COPY_IDS.get((context, sourceText))
        return tr(key) if key else None


def install_native_translations(app):
    """Translate Qt-owned menus/fallback pickers in the actual app language.

    Framework copy is supplied by Qt's official qtbase QM, not by replacing
    each edit menu. Retain its lifetime on QApplication. No runtime language
    switch: application widgets would otherwise keep their old catalog text.
    """
    if getattr(app, '_native_qt_translator', None) is not None:
        return app._native_qt_translator
    requested = active_locale()
    language = next(candidate for candidate in
        (requested, requested.split('-', 1)[0], DEFAULT_LOCALE) if load_catalog(candidate))
    translator = QTranslator(app)
    paths = (Path(__file__).with_name('locales'),
             Path(QLibraryInfo.path(QLibraryInfo.TranslationsPath)))
    for directory in paths:
        if translator.load(QLocale(language), 'qtbase', '_', str(directory)):
            app.installTranslator(translator)
            app._native_qt_translator = translator
            app._native_qt_translation_locale = language
            overlay = NativeFrameworkCopyTranslator(app)
            app.installTranslator(overlay)
            app._native_qt_copy_overlay = overlay
            return translator
    # A missing deployment dependency must be visible, not silent English UI.
    raise RuntimeError(tr('error.qt_translation_missing', language=language))


def apply_native_application_palette(p):
    """The shared theme also owns framework-only fallback widgets/icons.

    QSS text alone leaves file trees/sidebar on a bright system Base, making
    Dark text unreadable. Set semantic palette roles, not per-dialog patches.
    Native Windows browsing/control panels remain platform-owned.
    """
    palette = QPalette()
    colors = {
        QPalette.Window: p.window, QPalette.WindowText: p.text,
        QPalette.Base: p.input, QPalette.AlternateBase: p.surface_alt,
        QPalette.Text: p.text, QPalette.Button: p.surface_alt,
        QPalette.ButtonText: p.text, QPalette.ToolTipBase: p.surface,
        QPalette.ToolTipText: p.text, QPalette.Highlight: p.accent_soft,
        QPalette.HighlightedText: p.text, QPalette.Link: p.accent,
        QPalette.LinkVisited: p.accent, QPalette.BrightText: p.primary_text,
        QPalette.Light: p.border, QPalette.Midlight: p.border_subtle,
        QPalette.Mid: p.border, QPalette.Dark: p.border_subtle,
        QPalette.Shadow: p.border,
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText, QPalette.HighlightedText):
        palette.setColor(QPalette.Disabled, role, QColor(disabled_control_color(p)))
    QApplication.instance().setPalette(palette)


def paint_outline_check_indicator(painter, indicator, palette, *, checked, enabled=True, hovered=False):
    """Same indicator pixels for a control and a settings-table cell."""
    p = palette
    painter.setRenderHint(QPainter.Antialiasing)
    hovered=enabled and hovered
    fill = p.surface_alt if not enabled else (p.interaction_hover if hovered else p.input)
    border = p.border if not enabled else (p.accent_hover if hovered else p.accent if checked else p.muted)
    painter.setPen(QPen(QColor(border), 1.5 if checked or hovered else 1))
    painter.setBrush(QColor(fill))
    painter.drawRoundedRect(QRectF(indicator).adjusted(0.75, 0.75, -0.75, -0.75), 4, 4)
    if checked:
        pen = QPen(QColor(disabled_control_color(p) if not enabled else p.accent_hover if hovered else p.accent), 2.2)
        pen.setCapStyle(Qt.RoundCap); pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen); painter.setBrush(Qt.NoBrush)
        mark = QPainterPath()
        mark.moveTo(QPointF(indicator.left() + indicator.width() * .25, indicator.top() + indicator.height() * .52))
        mark.lineTo(QPointF(indicator.left() + indicator.width() * .43, indicator.top() + indicator.height() * .69))
        mark.lineTo(QPointF(indicator.left() + indicator.width() * .76, indicator.top() + indicator.height() * .32))
        painter.drawPath(mark)


class NativeEffectTabBar(QTabBar):
    """Stable preferred caption cells across400/600-weight selection states."""

    def __init__(self,parent=None,*,parameter=True):
        super().__init__(parent)
        self._parameter=parameter
        self.setMouseTracking(True)
        set_native_cursor(self,Qt.ArrowCursor)
        for button in self.findChildren(QAbstractButton):self._prepare_cursor_button(button)
        self.currentChanged.connect(self.refresh_pointer_feedback)
        self.tabMoved.connect(self.refresh_pointer_feedback)

    def _prepare_cursor_button(self,button):
        sync_native_command_cursor(button)
        button.installEventFilter(self)

    def eventFilter(self,watched,event):
        if isinstance(watched,QAbstractButton) and event.type() in (QEvent.EnabledChange,QEvent.Show,QEvent.ParentChange):
            sync_native_command_cursor(watched)
        return super().eventFilter(watched,event)

    def event(self,event):
        result=super().event(event)
        if event.type()==QEvent.ChildPolished and isinstance(event.child(),QAbstractButton):
            self._prepare_cursor_button(event.child())
        elif event.type() in (QEvent.EnabledChange,QEvent.Show,QEvent.ParentChange):
            self.refresh_pointer_feedback()
        return result

    def reconcile_hover(self,position):
        index=self.tabAt(position.toPoint() if isinstance(position,QPointF) else position)
        set_native_cursor(self,Qt.PointingHandCursor if index>=0 and self.isTabEnabled(index) else Qt.ArrowCursor)

    def refresh_pointer_feedback(self,*_args):refresh_native_pointer_feedback(self)
    def reset_stale_hover(self):set_native_cursor(self,Qt.ArrowCursor)

    def setTabEnabled(self,index,enabled):
        super().setTabEnabled(index,enabled);self.refresh_pointer_feedback()

    def setTabVisible(self,index,visible):
        super().setTabVisible(index,visible);self.refresh_pointer_feedback()

    def enterEvent(self,event):
        super().enterEvent(event);self.reconcile_hover(event.position())

    def mouseMoveEvent(self,event):
        super().mouseMoveEvent(event);self.reconcile_hover(event.position())

    def leaveEvent(self,event):
        self.reset_stale_hover();super().leaveEvent(event)

    def resizeEvent(self,event):
        super().resizeEvent(event);self.refresh_pointer_feedback()

    def tabLayoutChange(self):
        super().tabLayoutChange();self.refresh_pointer_feedback()

    def tabInserted(self,index):
        super().tabInserted(index);self.refresh_pointer_feedback()

    def tabRemoved(self,index):
        super().tabRemoved(index);self.refresh_pointer_feedback()

    def tabSizeHint(self, index):
        hint = super().tabSizeHint(index)
        text = self.tabText(index)
        regular = QFontMetrics(native_text_font(EFFECT_TAB_FONT_PX, QFont.Normal)).horizontalAdvance(text)
        selected = QFontMetrics(native_text_font(EFFECT_TAB_FONT_PX,
            QFont.DemiBold if self._parameter else QFont.Bold)).horizontalAdvance(text)
        rendered = selected if index == self.currentIndex() else regular
        hint.setWidth(hint.width() + max(regular, selected) - rendered)
        return hint

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())


class NativeTabs(QTabWidget):
    """Peer tab state changes re-hit the cursor without pointer motion."""
    def setTabEnabled(self,index,enabled):
        super().setTabEnabled(index,enabled)
        refresh_native_pointer_feedback(self.tabBar())

    def setTabVisible(self,index,visible):
        super().setTabVisible(index,visible)
        refresh_native_pointer_feedback(self.tabBar())


def make_parameter_tabs(parent=None,*,current_page_height=False):
    """Actual settings tabs, sharing main/inspector typography and pane style."""
    tabs = NativeContentTabs(parent,preserve_content_height=True) if current_page_height else NativeTabs(parent)
    tabs.setProperty("parameterTabs", True)
    tabs.setTabBar(NativeEffectTabBar(tabs))
    tabs.setMinimumSize(0, 0)
    return tabs


class NativeContentTabs(NativeTabs):
    """Width-aware peer pages,with optional stable maximum-page height."""

    PANE_TOP_GAP = COMPACT_CONTENT_GAP

    def __init__(self,parent=None,*,stable_page_height=False,preserve_content_height=False):
        super().__init__(parent)
        self.stable_page_height=stable_page_height
        self.preserve_content_height=preserve_content_height
        self.currentChanged.connect(self._page_extent_changed)
        policy=self.sizePolicy();policy.setHeightForWidth(True);self.setSizePolicy(policy)

    def _page_extent_changed(self,*_args):
        # A nested tab's new height-for-width must reach the outer scroll
        # content. Updating only this widget can leave ancestor layout caches
        # advertising the old page for a later event-loop turn.
        if self.preserve_content_height:
            # Scroll-owned forms keep fields intact at their real width.
            # The extent follows the current page,never the largest peer.
            self.setMinimumHeight(self.heightForWidth(self.width()))
        invalidate_layout_chain(self)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if self.preserve_content_height and event.size().width()!=event.oldSize().width():
            # Splitters negotiate the real page width after ancestor hints.
            # Re-measure wrapping at that width,without resizing the window.
            self._page_extent_changed()

    def hasHeightForWidth(self):return True

    def heightForWidth(self,width):
        # Explicitly hidden navigation must not reserve a phantom tab row.
        # isHidden also works before the containing window is first shown.
        bar_height=0 if self.tabBar().isHidden() else self.tabBar().sizeHint().height()
        pages=([self.widget(index) for index in range(self.count()) if self.isTabVisible(index)]
               if self.stable_page_height else [self.currentWidget()])
        heights=[0]
        for page in pages:
            if page is None:continue
            layout=page.layout()
            heights.append(layout.totalHeightForWidth(max(0,width)) if layout is not None and layout.hasHeightForWidth()
                else layout.totalSizeHint().height() if layout is not None else page.sizeHint().height())
        return max(heights)+bar_height+self.PANE_TOP_GAP

    def sizeHint(self):
        hint=super().sizeHint()
        return QSize(hint.width(),self.heightForWidth(self.width()))

    def minimumSizeHint(self):return QSize(0,self.heightForWidth(self.width()))


def make_navigation_tabs(parent=None,*,current_page_height=False,stable_page_height=False):
    """Navigation keeps its700-weight style with the same stable geometry."""
    tabs=(NativeContentTabs(parent,stable_page_height=stable_page_height)
          if current_page_height or stable_page_height else NativeTabs(parent))
    tabs.setTabBar(NativeEffectTabBar(tabs,parameter=False))
    tabs.setMinimumSize(0,0)
    return tabs


class OutlineCheckBox(QCheckBox):
    """Shared native checkbox: neutral fill and an explicit accent check.

    Qt owns keyboard activation and accessible state; the widget rect is the
    hit target so every hovered point is also clickable.
    The active UI has no separate focus ornament on peer controls, so this
    checkbox draws no focus-specific decoration either.
    """

    def __init__(self, text: str, parent=None, *, fit_cell=False):
        super().__init__(text, parent)
        sync_native_command_cursor(self)
        self._fit_cell = fit_cell
        self._native_palette = DARK
        self.setAttribute(Qt.WA_Hover, True)
        if fit_cell:
            self.setMaximumHeight(FORM_ROW_HEIGHT)
            self.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Preferred)
        else:
            self.setFixedHeight(FORM_ROW_HEIGHT)

    def minimumSizeHint(self):
        return QSize(0,0 if self._fit_cell else FORM_ROW_HEIGHT)

    def set_theme(self, palette: NativePalette) -> None:
        self._native_palette = widget_palette(self, fallback=palette)
        self.update()

    def _current_palette(self):
        # Tree sweeps cannot see controls created later or cached/reparented
        # pages. The owning window is the authority, including accent previews.
        # Standalone controls retain the explicitly supplied set_theme palette.
        self._native_palette = widget_palette(self, fallback=self._native_palette)
        return self._native_palette

    def event(self, event):
        result = super().event(event)
        if event.type() in (QEvent.EnabledChange,QEvent.ParentChange,QEvent.Show):
            sync_native_command_cursor(self)
        if hasattr(self, "_native_palette") and event.type() in (
                QEvent.ParentChange, QEvent.Show, QEvent.PaletteChange, QEvent.StyleChange):
            self._current_palette()
            self.update()
        return result

    def hitButton(self, position):
        return self.rect().contains(position)

    def paintEvent(self, event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        style = self.style()
        indicator = style.subElementRect(QStyle.SE_CheckBoxIndicator, option, self)
        p = self._current_palette()
        enabled = self.isEnabled()
        hovered = enabled and self.underMouse()
        checked = self.isChecked()

        painter = QPainter(self)
        paint_outline_check_indicator(painter, indicator, p, checked=checked, enabled=enabled, hovered=hovered)

        painter.setRenderHint(QPainter.Antialiasing, False)
        # CE_CheckBoxLabel receives the whole button rect under QSS on some
        # Qt styles, which can place the first glyph beneath the indicator.
        # Use the style-computed content rect while retaining Qt's font and
        # standard mnemonic/alignment semantics.
        contents = style.subElementRect(QStyle.SE_CheckBoxContents, option, self)
        painter.setFont(self.font())
        painter.setPen(QColor(disabled_control_color(p) if not enabled else p.accent_hover if hovered else p.label))
        painter.drawText(contents, Qt.AlignLeft | Qt.AlignVCenter | Qt.TextShowMnemonic, self.text())


@dataclass
class DampedMotion:
    """Elapsed-time, critically damped scalar motion; no storyboard deadline.

    x'' + 2*w*x' + w*w*(x-target) = 0. The exact solution is independent
    of frame rate, and retargeting keeps both position and velocity.
    """

    position: float = 0.0
    velocity: float = 0.0
    target: float = 0.0
    responsiveness: float = 40.0

    @property
    def settled(self):
        return abs(self.position - self.target) < .001 and abs(self.velocity) < .02

    def snap(self, target):
        self.position = self.target = float(target)
        self.velocity = 0.0

    def advance(self, elapsed):
        if elapsed <= 0:
            return
        offset = self.position - self.target
        coefficient = self.velocity + self.responsiveness * offset
        decay = math.exp(-self.responsiveness * elapsed)
        self.position = self.target + (offset + coefficient * elapsed) * decay
        self.velocity = (self.velocity - self.responsiveness * coefficient * elapsed) * decay
        if self.settled:
            self.snap(self.target)


class NativePresentationClock(QObject):
    """One demand-driven screen-paced native wait, bounded GUI delivery.

    Python3.14/Windows sleep uses a high-resolution waitable timer,releasing
    the GIL. DwmFlush is not a per-window refresh clock and can follow another
    composition interval. No QWidget/BASS operations occur on the worker.
    Retain Qt pacing on non-primary screens/platforms and waiter failure.
    This does not certify photon/audio time or bypass GUI-thread layout.
    """
    ready = Signal()

    def __init__(self, application, flush=None):
        super().__init__(application)
        self._condition=threading.Condition()
        self._requests={};self._delivering={};self._pending=False;self._closed=False;self._failed=False
        self._worker=None;self._waits=0;self._deliveries=0;self._frame_interval=1/60;self._paced_waits=0
        self._next_frame=0.
        self._library=None
        self._flush=self._wait_frame if flush is None else flush
        self.ready.connect(self._deliver,Qt.QueuedConnection)
        application.aboutToQuit.connect(self.close)

    def _wait_frame(self):
        with self._condition:interval=self._frame_interval
        now=time.perf_counter();deadline=self._next_frame
        if deadline<=now:deadline=now+interval
        time.sleep(max(0,deadline-now));self._next_frame=deadline+interval
        return 0

    def request(self,window,owner,callback):
        with self._condition:
            if self._closed or self._failed:return False
            screen=window.screen() if callable(getattr(window,'screen',None)) else None
            rate=screen.refreshRate() if screen is not None else 60
            self._frame_interval=1/rate if math.isfinite(rate) and rate>0 else 1/60
            key=(id(window),id(owner))
            self._delivering.pop(key,None)  # A replacement supersedes a captured old frame too.
            self._requests[key]=(weakref.ref(window),weakref.ref(owner),weakref.WeakMethod(callback))
            if self._worker is None:
                self._worker=threading.Thread(target=self._run,name='ABMSynth-visual-pacing',daemon=True)
                self._worker.start()
            self._condition.notify()
        return True

    def remove(self,window,owner):
        with self._condition:
            key=(id(window),id(owner))
            self._requests.pop(key,None);self._delivering.pop(key,None)

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda:self._closed or self._requests and not self._pending)
                if self._closed:return
                self._pending=True
            try:
                began=time.perf_counter();result=self._flush()
                elapsed=time.perf_counter()-began;failed=result!=0
                if not failed and elapsed<.0005:
                    # An immediate successful waiter is not an API failure.
                    # Python3.14/Windows uses a high-resolution waitable timer;
                    # this is the existing worker,not a GUI sleep or busy spin.
                    with self._condition:interval=self._frame_interval
                    time.sleep(max(0,interval-elapsed));self._paced_waits+=1
            except (OSError,RuntimeError):failed=True
            with self._condition:
                if self._closed:return
                self._waits+=1
                if failed:self._failed=True
            try:self.ready.emit()
            except RuntimeError:return
            if failed:return

    def _deliver(self):
        with self._condition:
            requests=tuple(self._requests.items());self._requests.clear()
            self._delivering=dict(requests)
            failed=self._failed
        try:
            for key,request in requests:
                with self._condition:
                    if self._closed or self._delivering.get(key) is not request:continue
                window_ref,owner_ref,callback_ref=request
                window,owner,callback=window_ref(),owner_ref(),callback_ref()
                if window is None or owner is None or callback is None:continue
                if not shiboken6.isValid(window) or not shiboken6.isValid(owner):continue
                if not window.isVisible() or window.windowState() & Qt.WindowMinimized:continue
                if failed:window.requestUpdate()
                else:
                    try:callback()
                    except Exception:
                        # One broken presentation must not consume the other
                        # windows' frame requests. Preserve Qt's visible error
                        # reporting; this is not an audio error or silent PASS.
                        import sys
                        sys.excepthook(*sys.exc_info())
                        window.requestUpdate()
            self._deliveries+=1
        finally:
            with self._condition:
                self._pending=False
                self._delivering.clear()
                self._condition.notify()

    def close(self):
        with self._condition:
            self._closed=True;self._requests.clear();self._delivering.clear();self._condition.notify_all()
        # No GUI join on a compositor/native wait. The daemon retains this
        # owner until it returns, then exits without sending another signal.


def request_visual_frame(window,owner,callback):
    """Shared Windows primary-display candidate; opt-out diagnostic oracle."""
    app=QApplication.instance()
    if (app is None or app.platformName()!='windows' or os.environ.get('ABMSYNTH_QT_VISUAL_CLOCK')=='1'
            or not callable(getattr(window,'screen',None))
            or window.screen() is not app.primaryScreen()):
        window.requestUpdate();return False
    clock=getattr(app,'_abmsynth_presentation_clock',None)
    if clock is None:
        try:clock=NativePresentationClock(app)
        except OSError:window.requestUpdate();return False
        app._abmsynth_presentation_clock=clock
    if not clock.request(window,owner,callback):window.requestUpdate();return False
    return True


def remove_visual_frame(window,owner):
    app=QApplication.instance();clock=getattr(app,'_abmsynth_presentation_clock',None)
    if clock is not None:clock.remove(window,owner)


def compositor_visual_clock(window,owner):
    app=QApplication.instance();clock=getattr(app,'_abmsynth_presentation_clock',None)
    if (clock is None or clock._closed or clock._failed or os.environ.get('ABMSYNTH_QT_VISUAL_CLOCK')=='1'
            or not callable(getattr(window,'screen',None))
            or window.screen() is not app.primaryScreen()):return False
    with clock._condition:
        key=(id(window),id(owner))
        return key in clock._requests or key in clock._delivering


class NativeToggleSwitch(OutlineCheckBox):
    """Compact switch; Qt owns checked/accessibility/keyboard semantics.

    Inherits the shared theme/row contract, not checkbox painting. The whole
    labelled row is clickable. A normal track width is a preferred hint only.
    """

    TRACK_WIDTH = 36
    TRACK_HEIGHT = 20
    THUMB_DIAMETER = 14

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self._thumb_position = 1.0 if self.isChecked() else 0.0
        self._motion = DampedMotion(self._thumb_position, target=self._thumb_position)
        self._motion_time = None
        self._motion_window = None
        self._label_ink_cache = None
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        # Offscreen platform has no presentation clock. Only its tests need
        # a timer; native windows share Qt's coalesced UpdateRequest frames.
        self._motion_timer = QTimer(self)
        self._motion_timer.setTimerType(Qt.PreciseTimer)
        self._motion_timer.setInterval(8)
        self._motion_timer.timeout.connect(self._advance_thumb)
        self.toggled.connect(self._animate_thumb)

    def setChecked(self, checked):
        previous = self.isChecked()
        super().setChecked(checked)
        if self.signalsBlocked() and previous != self.isChecked():
            # Block domain callbacks during model restoration, not visuals.
            self._animate_thumb(self.isChecked())

    def _get_thumb_position(self): return self._thumb_position

    def _set_thumb_position(self, value):
        self._thumb_position = float(value)
        self.update()

    thumbPosition = Property(float, _get_thumb_position, _set_thumb_position)

    def _animate_thumb(self, checked):
        target = 1.0 if checked else 0.0
        if not self.isVisible() or not ui_animations_enabled():
            self._finish_thumb(target)
            return
        # Advance to the click instant under the OLD target first. Reversal
        # changes the force, not the current position or momentum.
        now = time.perf_counter()
        if self._motion_time is not None:
            self._motion.advance(now - self._motion_time)
            self._set_thumb_position(self._motion.position)
        self._motion.target = target
        self._motion_time = now
        handle = self.window().windowHandle()
        if QApplication.platformName() == "offscreen":
            self._motion_timer.start()
        elif handle is not None:
            if handle is not self._motion_window:
                self._detach_motion_window()
                self._motion_window = handle
                handle.installEventFilter(self)
            request_visual_frame(handle,self,self._advance_thumb)
        else:
            self._finish_thumb(target)

    def _detach_motion_window(self):
        if self._motion_window is not None:
            remove_visual_frame(self._motion_window,self)
            try:
                self._motion_window.removeEventFilter(self)
            except RuntimeError:
                pass  # Window may already have been destroyed during shutdown.
            self._motion_window = None

    def _finish_thumb(self, target):
        self._motion.snap(target)
        self._motion_time = None
        self._motion_timer.stop()
        self._detach_motion_window()
        self._set_thumb_position(target)

    def _advance_thumb(self):
        if self._motion_time is None:
            return
        if not self.isVisible() or not ui_animations_enabled():
            self._finish_thumb(float(self.isChecked()))
            return
        now = time.perf_counter()
        self._motion.advance(now - self._motion_time)
        self._motion_time = now
        self._set_thumb_position(self._motion.position)
        if self._motion.settled:
            self._finish_thumb(self._motion.target)
        elif self._motion_window is not None:
            request_visual_frame(self._motion_window,self,self._advance_thumb)

    def eventFilter(self, watched, event):
        if watched is self._motion_window and event.type() == QEvent.UpdateRequest and not compositor_visual_clock(watched,self):
            self._advance_thumb()
        return super().eventFilter(watched, event)

    def hideEvent(self, event):
        self._finish_thumb(float(self.isChecked()))
        super().hideEvent(event)

    def sizeHint(self):
        width = self.TRACK_WIDTH + METRICS["form_column_gap"] + QFontMetrics(self.font()).horizontalAdvance(self.text()) + 4
        return QSize(width, FORM_ROW_HEIGHT)

    def minimumSizeHint(self): return QSize(0, FORM_ROW_HEIGHT)
    def _label_ink_center(self, label_left):
        """Cache native hinted ink position, not the font's outline bounds.

        Measure at the current device scale once, but keep painting ordinary
        native text. Animation frames need no scan or bitmap text.
        """
        ratio = self.devicePixelRatioF()
        palette = self._native_palette
        key = (self.text(), self.font().key(), ratio, palette.surface, palette.label, label_left)
        if self._label_ink_cache is None or self._label_ink_cache[0] != key:
            metrics = QFontMetricsF(self.font(), self)
            margin = math.ceil(metrics.height())
            width = math.ceil(metrics.horizontalAdvance(self.text()) + label_left) + margin
            height = 3 * margin
            pixmap = QPixmap(math.ceil(width * ratio), math.ceil(height * ratio))
            pixmap.setDevicePixelRatio(ratio)
            # Opaque native text may use different hinted edge coverage than
            # transparent text, particularly for longer labels at 150% DPI.
            background, foreground = QColor(palette.surface), QColor(palette.label)
            pixmap.fill(background)
            probe = QPainter(pixmap)
            probe.setFont(self.font())
            probe.setPen(foreground)
            # Match the horizontal subpixel phase too: native glyph hinting
            # at fractional DPR can differ when the probe starts elsewhere.
            probe.drawText(QRectF(label_left, 0, width - label_left, height),
                           Qt.AlignLeft | Qt.AlignTop | Qt.TextShowMnemonic, self.text())
            probe.end()
            pixels = pixmap.toImage()
            # Ignore the very faint antialias fringe, consistently in either
            # theme. Pixel edges, not row indices, define the visible center.
            contrast = sum(abs(a - b) for a, b in zip(
                foreground.getRgb()[:3], background.getRgb()[:3]))
            rows = self._ink_rows(pixels,background,contrast)
            center = ((rows[0] + rows[-1] + 1) / (2 * ratio)
                      if rows else metrics.height() / 2)
            self._label_ink_cache = (key, center)
        return self._label_ink_cache[1]

    @staticmethod
    def _ink_rows(pixels,background,contrast):
        """Same integer RGB threshold as pixelColor, no per-pixel Python ABI.

        QImage RGBA8888 is byte ordered and constBits does not detach. The
        owned converted image outlives the NumPy view; honor row padding.
        Native text/palette/hinting are unchanged, only measurement is batched.
        """
        rgba=pixels.convertToFormat(QImage.Format_RGBA8888)
        view=np.frombuffer(rgba.constBits(),dtype=np.uint8,count=rgba.sizeInBytes())
        rgb=view.reshape(rgba.height(),rgba.bytesPerLine())[:,:rgba.width()*4].reshape(rgba.height(),rgba.width(),4)[:,:,:3]
        distance=np.abs(rgb.astype(np.int16)-np.array(background.getRgb()[:3],dtype=np.int16)).sum(axis=2)
        return np.flatnonzero(np.any(distance>=contrast/8,axis=1)).tolist()

    def paintEvent(self, _event):
        p = self._current_palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        enabled, checked = self.isEnabled(), self.isChecked()
        hovered = enabled and self.underMouse()
        width = min(self.TRACK_WIDTH, max(0, self.width() - 2))
        height = min(self.TRACK_HEIGHT, max(0, self.height() - 2))
        if width <= 0 or height <= 0:
            return
        track = QRectF(1, (self.height() - height) / 2, width, height)
        unknown=bool(self.property('unknownValue'))
        if not enabled or unknown:
            fill = p.surface_alt
        elif checked:
            fill = p.accent_hover if hovered else p.accent
        else:
            fill = p.interaction_hover if hovered else p.input
        border = p.border_subtle if not enabled else p.accent_hover if hovered else p.accent if checked else p.border
        painter.setPen(QPen(QColor(border), METRICS["control_border_width"]))
        painter.setBrush(QColor(fill))
        # Switch identity is a capsule, not a button-shaped rounded rectangle.
        # Share outline colour/stroke with other controls, not their radius.
        radius = min(width, height) / 2
        painter.drawRoundedRect(track, radius, radius)
        diameter = min(self.THUMB_DIAMETER, max(0, width - 6), max(0, height - 6))
        # Retained momentum may briefly continue toward the old endpoint.
        # Keep the thumb visually inside its track, without resetting velocity.
        position = max(0.0, min(1.0, self._thumb_position))
        if self.property("mixedValue"):
            position = .5
            painter.setPen(QPen(QColor(p.muted), 1))
            painter.drawLine(track.center() + QPointF(-4, 0), track.center() + QPointF(4, 0))
        thumb_x = track.left() + 3 + max(0, width - diameter - 6) * position
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(p.muted if not enabled else p.window if checked else p.label if hovered else p.muted))
        if unknown:
            painter.setPen(QColor(p.muted))
            painter.drawText(track,Qt.AlignCenter,'?')
        else:
            painter.drawEllipse(QRectF(thumb_x, track.center().y() - diameter / 2, diameter, diameter))
        label_left = track.right() + METRICS["form_column_gap"]
        contents = QRectF(label_left, 0, max(0, self.width() - label_left), self.height())
        ratio = self.devicePixelRatioF()
        label_y = track.center().y() - self._label_ink_center(label_left)
        # An odd-pixel glyph cannot share an even-pixel pill's exact center.
        # Resolve the half-pixel tie upward, never back to the low-looking side.
        # Remove floating-point noise before resolving the half-pixel tie.
        contents.moveTop(math.ceil(round(label_y * ratio, 9) - .5) / ratio)
        painter.setFont(self.font())
        painter.setPen(QColor(disabled_control_color(p) if not enabled else p.accent_hover if hovered else p.label))
        painter.drawText(contents, Qt.AlignLeft | Qt.AlignTop | Qt.TextShowMnemonic, self.text())


class NativeDisclosure(QWidget):
    """Clip persistent controls while their layout slot grows or shrinks."""

    heightChanged = Signal()
    expansionChanged = Signal(bool)

    def __init__(self, spacing, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._body = QWidget(self)
        self.content_layout = QVBoxLayout(self._body)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(spacing)
        self._natural_height = 0
        self._width_extent_pending = False
        self._expanded = None
        self._reveal_pending=False
        self._reveal_on_finish=False
        self._reveal_revision=0
        self._disclosure_button=None
        self._control_callback=None
        self.motion = QVariantAnimation(self)
        self.motion.valueChanged.connect(self._set_revealed_height)
        self.motion.finished.connect(self._finish_motion)
        self._motion_started_at = None
        self._motion_window = None
        self._motion_timer = QTimer(self)
        self._motion_timer.setTimerType(Qt.PreciseTimer)
        self._motion_timer.setInterval(4)  # Headless fallback; native frames follow the screen.
        self._motion_timer.timeout.connect(self._advance_motion)
        self.setFixedHeight(0)
        self.setHidden(True)

    def _set_revealed_height(self, value):
        height = max(0, int(round(value)))
        if self.height() != height or self.minimumHeight() != height:
            self.setFixedHeight(height)
            invalidate_layout_chain(self)
            self.heightChanged.emit()
        if self._reveal_pending and height>=min(FORM_ROW_HEIGHT,self._natural_height):
            self._reveal_pending=False
            revision=self._reveal_revision
            QTimer.singleShot(0,self,lambda:self._reveal_expansion(revision))

    def _reveal_expansion(self,revision):
        if revision!=self._reveal_revision or not self._expanded:return
        from native_layout import reveal_expanded_region
        reveal_expanded_region(self)

    def _finish_motion(self):
        self._detach_motion_clock()
        self._set_revealed_height(self._natural_height if self._expanded else 0)
        if not self._expanded:
            self.setHidden(True)
        self.heightChanged.emit()
        if self._reveal_on_finish and self._expanded:
            self._reveal_on_finish=False
            revision=self._reveal_revision
            QTimer.singleShot(0,self,lambda:self._reveal_expansion(revision))

    def settle(self):
        """Release the window frame subscription before parent teardown."""
        self.motion.stop()
        self._finish_motion()

    def _detach_motion_clock(self):
        self._motion_started_at = None
        self._motion_timer.stop()
        if self._motion_window is not None:
            remove_visual_frame(self._motion_window,self)
            try:
                self._motion_window.removeEventFilter(self)
            except RuntimeError:
                pass  # The native window may already have been destroyed.
            self._motion_window = None

    def _advance_motion(self):
        started=self._motion_started_at
        if started is None:
            return
        if not self.isVisible() or not ui_animations_enabled():
            self.motion.stop()
            self._finish_motion()
            return
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        duration=self.motion.duration()
        self.motion.setCurrentTime(min(duration, elapsed_ms))
        if self._motion_started_at != started:return  # finished/valueChanged may settle or retarget.
        if elapsed_ms >= duration:
            self.motion.stop()
            self._finish_motion()
            return
        if self._motion_started_at is not None and self._motion_window is not None:
            request_visual_frame(self._motion_window,self,self._advance_motion)

    def bind_control(self,switch,callback=None):
        """One binding owner;task observers may retain layout/persistence policy."""
        if self._disclosure_button is not None:
            self._disclosure_button.toggled.disconnect(self._control_changed)
        self._disclosure_button=switch;self._control_callback=callback
        switch.toggled.connect(self._control_changed)

    def _control_changed(self,expanded):
        if self._control_callback is None:self.set_expanded(expanded)
        else:self._control_callback(expanded)

    def _cancel_content_input(self):
        controls=self._body.findChildren(QWidget)
        header=getattr(self,'input_header',None)
        if header is not None:controls+=header.findChildren(QWidget)
        for control in controls:
            cancel=getattr(control,'cancel_editor',None)
            if callable(cancel):cancel()
            if isinstance(control,QComboBox):control.hidePopup()

    def set_expanded(self, expanded, *, refresh_extent=False):
        expanded = bool(expanded)
        if self._expanded == expanded and not refresh_extent:
            return
        if not expanded:self._cancel_content_input()
        natural_height=(self.content_layout.totalHeightForWidth(self.width()) if self.content_layout.hasHeightForWidth()
                        else self.content_layout.sizeHint().height())
        if (self._expanded == expanded and refresh_extent
                and natural_height==self._natural_height):
            return  # Duplicate queued form refresh must not restart the clock.
        if self._motion_started_at is not None:
            self._advance_motion()
        if expanded!=self._expanded:
            self._reveal_revision+=1
            self._reveal_pending=expanded and self._expanded is False and self.window().isVisible()
            self._reveal_on_finish=self._reveal_pending
        self._natural_height = natural_height
        self._body.setFixedHeight(self._natural_height)
        self._body.resize(self.width(), self._natural_height)
        current = 0 if self.isHidden() else self.height()
        self._detach_motion_clock()
        self.motion.stop()
        self._expanded = expanded
        target = self._natural_height if expanded else 0
        if expanded:
            self.setHidden(False)
        self.expansionChanged.emit(expanded)
        if current == target or not self.isVisible() or not ui_animations_enabled():
            self._set_revealed_height(target)
            if not expanded:
                self.setHidden(True)
                self.heightChanged.emit()
            return
        full_duration = 250 if expanded else 167
        blocked=self.motion.blockSignals(True)
        try:
            # QVariantAnimation retains the previous currentTime. Updating its
            # endpoints at that old time can emit a stale full-height frame
            # before start() rewinds it, visibly jumping up/down on reopening.
            self.motion.setDuration(max(60, round(full_duration * abs(target - current) / max(1, self._natural_height))))
            self.motion.setEasingCurve(QEasingCurve.OutCubic if expanded else QEasingCurve.InCubic)
            self.motion.setStartValue(current)
            self.motion.setEndValue(target)
            self.motion.setCurrentTime(0)
        finally:
            self.motion.blockSignals(blocked)
        self._set_revealed_height(current)
        self.motion.start()
        self.motion.pause()  # requestUpdate, not Qt's generic animation timer, owns the cadence.
        self._motion_started_at = time.perf_counter()
        handle = self.window().windowHandle()
        if QApplication.platformName() == "offscreen" or handle is None:
            self._motion_timer.start()
        else:
            self._motion_window = handle
            handle.installEventFilter(self)
            request_visual_frame(handle,self,self._advance_motion)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._body.resize(self.width(), self._natural_height)
        if (event.size().width()!=event.oldSize().width() and self._expanded
                and not self._width_extent_pending):
            # A hidden disclosure can still have its construction width when
            # expanded. Measure again after mounting/scrollbar negotiation,
            # rather than stretch headings into that stale wrapped-text height.
            self._width_extent_pending=True
            QTimer.singleShot(0,self,self._refresh_width_extent)

    def _refresh_width_extent(self):
        self._width_extent_pending=False
        if self._expanded:
            self.set_expanded(True,refresh_extent=True)

    def hideEvent(self, event):
        if self._motion_started_at is not None:
            self.settle()
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if watched is self._motion_window and event.type() == QEvent.UpdateRequest and not compositor_visual_clock(watched,self):
            self._advance_motion()
        return super().eventFilter(watched, event)


class NativeSectionHeader(QFrame):
    """Header clicks disclose content; embedded activation controls stay independent."""
    def __init__(self,parent=None):
        super().__init__(parent);self._pressed=False;self.disclosure_button=None
        self._activation_control=None;self._activation_source=None
        set_native_cursor(self,Qt.ArrowCursor)

    def bind_disclosure_button(self,switch):
        self.disclosure_button=switch;switch.installEventFilter(self)
        set_native_cursor(self,Qt.PointingHandCursor if switch.isEnabled() else Qt.ArrowCursor)

    def set_activation_control(self,control,*,enabled_by=None):
        """Keep the actual functional control visible, even with the body folded."""
        if self._activation_source is not None:self._activation_source.removeEventFilter(self)
        self._activation_control=control;self._activation_source=enabled_by
        self.layout().insertWidget(1,control)
        if enabled_by is not None:
            control.setEnabled(enabled_by.isEnabled());enabled_by.installEventFilter(self)

    def eventFilter(self,watched,event):
        if watched is self._activation_source and event.type()==QEvent.EnabledChange:
            self._activation_control.setEnabled(watched.isEnabled())
        if watched is self.disclosure_button and event.type()==QEvent.EnabledChange:
            self._pressed=False
            set_native_cursor(self,Qt.PointingHandCursor if watched.isEnabled() else Qt.ArrowCursor)
        return super().eventFilter(watched,event)

    def mousePressEvent(self,event):
        switch=self.disclosure_button
        if event.button()==Qt.LeftButton and switch is not None and switch.isEnabled():
            self._pressed=True;event.accept();return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self,event):
        pressed,self._pressed=self._pressed,False
        switch=self.disclosure_button
        if event.button()==Qt.LeftButton and pressed:
            if self.rect().contains(event.position().toPoint()) and switch is not None and switch.isEnabled():
                switch.setFocus(Qt.MouseFocusReason);switch.click()
            event.accept();return
        super().mouseReleaseEvent(event)

    def cancel_pointer_gesture(self):self._pressed=False

    def event(self,event):
        result=super().event(event)
        if event.type() in (QEvent.Hide,QEvent.EnabledChange,QEvent.WindowDeactivate,QEvent.UngrabMouse):
            self._pressed=False
        if event.type() in (QEvent.EnabledChange,QEvent.ParentChange,QEvent.Show):
            switch=getattr(self,'disclosure_button',None)
            set_native_cursor(self,Qt.PointingHandCursor if switch is not None and switch.isEnabled() else Qt.ArrowCursor)
        return result


def make_control_section(title_text, tooltip=None, *, collapsible=False, checkable=False,
                         background='transparent'):
    """Shared compact subsection; existing channel style hooks stay stable.

    Presentation only. Callers own controls,state and any disclosure policy.
    Optional card-colour fill changes no structure,metrics or input behavior.
    A subsection is not a NativeCard and adds no horizontal minimum.
    Disclosure and functional activation are independent, optional controls.
    """
    if background not in ('transparent','card'):
        raise ValueError('Section background role')
    section = QFrame()
    section.setObjectName("ChannelEditorSection")
    section.setProperty('nativeSectionBackground',background)
    section.setMinimumWidth(0)
    section.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Maximum)
    layout = set_layout(QVBoxLayout(section),6,4)
    layout.setAlignment(Qt.AlignTop)
    heading = QLabel(title_text)
    heading.setObjectName("ChannelEditorSectionTitle")
    heading.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    heading.setMinimumWidth(0)
    section.heading = heading
    if tooltip:install_effect_tooltip(tooltip, heading)
    grid = set_layout(QGridLayout(),0,4)
    layout.setSpacing(0)
    header = NativeSectionHeader()
    header.setObjectName("ChannelEditorSectionHeader")
    header.setMinimumWidth(0)
    header_layout = set_layout(QHBoxLayout(header),0,4)
    heading.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    header_layout.addWidget(heading, 1)
    section.header=header
    if checkable:
        switch=NativeToggleSwitch("");section.toggle_switch=switch
        switch.setChecked(True);switch.setAccessibleName(title_text)
        if tooltip:install_effect_tooltip(tooltip,switch)
        header.set_activation_control(switch)
    layout.addWidget(header)
    section.header_layout = header_layout
    if collapsible:
        button=NativeDisclosureButton(title_text)
        if tooltip:install_effect_tooltip(tooltip,button)
        header_layout.addWidget(button);header.bind_disclosure_button(button)
        heading.setBuddy(button)
        disclosure = NativeDisclosure(0)
        # Keep the4px header/body gap inside the animated disclosure extent.
        disclosure.content_layout.setContentsMargins(0, 4, 0, 0)
        disclosure.content_layout.addLayout(grid);layout.addWidget(disclosure)
        section.disclosure = disclosure
        section.disclosure_button = button
        disclosure.input_header=header
        disclosure.bind_control(button)
    else:
        # A setting's Off state retains its body;callers own value admission.
        layout.setSpacing(4);layout.addLayout(grid)
    return section, grid


def add_section_action(section,button):
    """Put a contextual command in the shared subsection header,not a new row."""
    if not hasattr(section,'header_layout'):
        section.layout().removeWidget(section.heading)
        header=QWidget(section);row=set_layout(QHBoxLayout(header),0,4)
        row.addWidget(section.heading,1);section.layout().insertWidget(0,header)
        section.header_layout=row
    disclosure=getattr(section,'disclosure_button',None)
    index=section.header_layout.indexOf(disclosure) if disclosure is not None else -1
    if index>=0:section.header_layout.insertWidget(index,button)
    else:section.header_layout.addWidget(button)


class NativeLayoutSnapshot(QWidget):
    """A short-lived visual cache for an unchanged part of an animated layout.

    Like the monitor transition cache, this never owns/pauses domain state.
    The real controls still receive input and values; only repeated painting
    is suppressed until the layout motion settles. No refresh-rate throttling.
    """

    def __init__(self, target):
        super().__init__(target.parentWidget())
        self.target = target
        self._pixels = None
        self._was_updates_enabled = True
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.hide()
        target.installEventFilter(self)

    def capture(self, window_pixels=None):
        if self._pixels is not None or not self.target.isVisible():
            return
        window = self.target.window()
        rect = QRect(self.target.mapTo(window, QPoint()), self.target.size())
        # Grabbing a transparent child alone can substitute the OS palette
        # for its actual themed parent backdrop. Cache the composed window
        # pixels instead, sharing one render across the unchanged regions.
        if window_pixels is None:
            self._pixels = window.grab(rect)
        else:
            ratio = window_pixels.devicePixelRatio()
            physical = QRect(round(rect.x() * ratio), round(rect.y() * ratio),
                             round(rect.width() * ratio), round(rect.height() * ratio))
            self._pixels = window_pixels.copy(physical)
            self._pixels.setDevicePixelRatio(ratio)
        self._logical_width = self.target.width()
        self._was_updates_enabled = self.target.updatesEnabled()
        self.setGeometry(self.target.geometry())
        self.show()
        self.raise_()
        self.target.setUpdatesEnabled(False)

    def release(self):
        if self._pixels is None:
            return
        self.hide()
        self._pixels = None
        self.target.setUpdatesEnabled(self._was_updates_enabled)
        self.target.update()

    def eventFilter(self, watched, event):
        if self._pixels is not None:
            if event.type() in (QEvent.Move, QEvent.Resize):
                if self.target.width() != self._logical_width:
                    self.release()  # Width reflow must render real controls.
                else:
                    self.setGeometry(self.target.geometry())
            elif event.type() in (QEvent.Hide, QEvent.StyleChange, QEvent.PaletteChange, QEvent.FontChange,
                                  QEvent.DevicePixelRatioChange):
                self.release()
        return super().eventFilter(watched, event)

    def paintEvent(self, event):
        if self._pixels is not None:
            painter = QPainter(self)
            painter.drawPixmap(0, 0, self._pixels)


class NativeWindowSizeMotion(QObject):
    """Use the disclosure's easing and screen-paced clock for window resizing."""

    finished = Signal()

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.motion = QPropertyAnimation(window, b"size", self)
        self._started_at = None
        self._handle = None
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(4)
        self._timer.timeout.connect(self._advance)
        self._applying_size = False
        if not hasattr(window,'_native_size_motions'):window._native_size_motions=weakref.WeakSet()
        window._native_size_motions.add(self)
        window.installEventFilter(self)

    def start(self, target, *, expanding):
        self.settle()
        if not automatic_window_fit_allowed(self.window):
            self.finished.emit()  # Consumers must release transition resources.
            return
        if (self.window.size() == target or not self.window.isVisible()
                or QApplication.platformName() == "offscreen" or not ui_animations_enabled()):
            self.window.resize(target)
            self.finished.emit()
            return
        self.motion.setDuration(250 if expanding else 167)
        self.motion.setEasingCurve(QEasingCurve.OutCubic if expanding else QEasingCurve.InCubic)
        self.motion.setStartValue(self.window.size())
        self.motion.setEndValue(target)
        self.motion.start()
        self.motion.pause()
        self._started_at = time.perf_counter()
        self._handle = self.window.windowHandle()
        if self._handle is None:
            self._timer.start()
        else:
            self._handle.installEventFilter(self)
            native=request_visual_frame(self._handle,self,self._advance)
            if not native:self._timer.start()  # Existing fallback remains.

    def _advance(self):
        started=self._started_at
        if started is None:
            return
        if not automatic_window_fit_allowed(self.window):
            self.cancel_external_resize()
            return
        elapsed = round((time.perf_counter() - started) * 1000)
        duration=self.motion.duration()
        self._applying_size = True
        try:self.motion.setCurrentTime(min(duration, elapsed))
        finally:self._applying_size = False
        if self._started_at != started:return  # A resize consumer may settle/restart motion.
        if elapsed >= duration:
            self._detach()
            self.motion.stop()
            self.finished.emit()
        elif self._handle is not None:
            request_visual_frame(self._handle,self,self._advance)

    def _detach(self):
        self._started_at = None
        self._timer.stop()
        if self._handle is not None:
            remove_visual_frame(self._handle,self)
            try:
                self._handle.removeEventFilter(self)
            except RuntimeError:
                pass
            self._handle = None

    def settle(self):
        self._detach()
        self.motion.stop()

    def cancel_external_resize(self):
        self.settle()
        self.finished.emit()  # Release any visual snapshot/scroll-policy lease.

    def eventFilter(self, watched, event):
        if (watched is self.window and event.type() == QEvent.Resize
                and not event.spontaneous() and self._started_at is not None
                and not self._applying_size):
            self.cancel_external_resize()
        if watched is self._handle and event.type() == QEvent.UpdateRequest and not compositor_visual_clock(watched,self):
            self._advance()
        return super().eventFilter(watched, event)


class NativeMonitorTransitionFrame(QWidget):
    """Static monitor image during a 250ms window resize, never an audio state."""

    def __init__(self, page):
        super().__init__(page)
        self._axis = None
        self._channels = None
        self._gap = 0
        self._top = 0
        self._dark = True
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        page.installEventFilter(self)
        self.hide()

    def capture(self, axis, channels, gap, top, dark):
        started = time.perf_counter()
        self._axis = axis.grab()
        self._channels = channels.grab()
        self.last_capture_ms = (time.perf_counter() - started) * 1000
        self._gap = int(gap)
        self._top = int(top)
        self._dark = bool(dark)
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()

    def paintEvent(self, event):
        palette = widget_palette(self, self._dark)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(palette.surface))
        if self._axis is not None and self._channels is not None:
            painter.drawPixmap(0, self._top, self._axis)
            painter.drawPixmap(self._axis.width() + self._gap, self._top, self._channels)

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() == QEvent.Resize and self.isVisible():
            self.setGeometry(watched.rect())
        return super().eventFilter(watched, event)


class NativeMenu(QMenu):
    """One font-raster contract for dynamically created menus and submenus."""

    def __init__(self, *args):
        super().__init__(*args)
        self.aboutToShow.connect(self._apply_menu_font)

    def _apply_menu_font(self):
        self.ensurePolished()
        font = native_text_font(self.font().pixelSize() if self.font().pixelSize() > 0 else METRICS["body_font_px"])
        self.setFont(font)
        for action in self.actions():
            action.setFont(font)

    def addMenu(self, menu):
        if isinstance(menu, str):
            child = NativeMenu(menu, self)
            super().addMenu(child)
            return child
        return super().addMenu(menu)

    def event(self, event):
        if event.type()==QEvent.ToolTip:return handle_native_tooltip(self,event)
        return super().event(event)


class AnimatedComboBox(QComboBox):
    """One Native combo visual/interaction contract for every active surface."""

    def wheelEvent(self,event):
        dispatch_native_wheel(self,event,super().wheelEvent)

    COMPACT_TEXT_ROLE = Qt.UserRole + 113

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(FORM_ROW_HEIGHT)
        self._effect = None
        self._animation = None
        self._popup_motion_window = None
        self._popup_motion_surface = None
        self._popup_motion_destroyed = None
        self._popup_motion_started = None
        self._category_provider = None
        self._category_menu = None
        view = QListView(self)
        view.setUniformItemSizes(True)
        view.setFrameShape(QFrame.NoFrame)
        self.setView(view)
        self.setAttribute(Qt.WA_Hover, True)

    def minimumSizeHint(self):
        return QSize(0,FORM_ROW_HEIGHT)

    def paintEvent(self, event):
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        compact=self.currentData(self.COMPACT_TEXT_ROLE)
        if compact is not None:option.currentText=str(compact)
        if not self.isEditable():
            if self.currentIndex()<0 and self.placeholderText():
                option.currentText=self.placeholderText()
                option.palette.setColor(QPalette.ButtonText,
                    option.palette.color(QPalette.PlaceholderText))
            text_rect = self.style().subControlRect(QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxEditField, self)
            # The style subcontrol already excludes arrow and content padding.
            option.currentText = self.fontMetrics().elidedText(option.currentText, Qt.ElideRight, max(0, text_rect.width()))
            styled = QStylePainter(self)
            styled.drawComplexControl(QStyle.CC_ComboBox, option)
            styled.drawControl(QStyle.CE_ComboBoxLabel, option)
            styled.end()
        else:
            super().paintEvent(event)
        arrow = self.style().subControlRect(QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxArrow, self)
        if arrow.isEmpty():
            return
        window = self.window()
        owner = window if hasattr(window, "_dark") else getattr(window, "owner", None)
        if owner is None:
            owner = window.parentWidget()
        palette = widget_palette(owner)
        colour = (disabled_control_color(palette) if not self.isEnabled() else
                  palette.accent_hover if self.underMouse() else palette.label)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(colour), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        x, y = arrow.center().x(), arrow.center().y()
        painter.drawLine(x - 4, y - 2, x, y + 2)
        painter.drawLine(x, y + 2, x + 4, y - 2)

    def _hover_reconciler(self):
        window = self.window()
        while window is not None:
            reconciler = getattr(window, "_hover_reconciler", None)
            if reconciler is not None and hasattr(reconciler, "popup_closed"):
                return reconciler
            window = window.parentWidget()
        return None

    def showPopup(self):
        self._finish_popup_fade()  # Retire the old effect before any reentrant reopen.
        if self._category_provider is not None and self.count():
            self._show_category_popup()
            return
        # This class owns the popup fade. Qt's additional opening scroll can
        # finish after Escape and show a canceled popup again on Windows.
        # Suppress only while the GUI-thread base call chooses its effect;
        # restore the application preference even if construction fails.
        stock_motion=QApplication.isEffectEnabled(Qt.UI_AnimateCombo)
        if stock_motion:QApplication.setEffectEnabled(Qt.UI_AnimateCombo,False)
        try:super().showPopup()
        finally:
            if stock_motion:QApplication.setEffectEnabled(Qt.UI_AnimateCombo,True)
        popup = self.view().window()
        if popup is not None:
            self._fit_popup_width(popup)
        # On Windows the native popup may only become visible at the next
        # event-loop boundary, so registration cannot depend on isVisible().
        reconciler = self._hover_reconciler()
        if self.count() and reconciler is not None:
            reconciler.popup_opened(self)
        if popup is None or not popup.isVisible():
            return
        if not ui_animations_enabled():return
        # Keep the private QWidget wrapper alive while its QWindow is borrowed.
        # On PySide/Windows dropping that wrapper invalidates the saved handle
        # without a usable destroyed notification, stranding a paused fade.
        self._popup_motion_surface=popup
        self._effect = QGraphicsOpacityEffect(popup)
        self._effect.setOpacity(0.0)
        popup.setGraphicsEffect(self._effect)
        self._animation = QPropertyAnimation(self._effect, b"opacity", self)
        self._animation.setDuration(METRICS["popup_fade_ms"])
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        animation=self._animation
        animation.finished.connect(lambda:self._finish_popup_fade(animation))
        animation.start()
        if self._animation is not animation:return
        handle=popup.windowHandle()
        if handle is not None and request_visual_frame(handle,self,self._advance_popup_fade):
            animation.pause()
            self._popup_motion_started=time.perf_counter()
            self._popup_motion_window=handle
            self._popup_motion_destroyed=handle.destroyed.connect(
                lambda:self._popup_frame_destroyed(handle,animation) if shiboken6.isValid(self) else None)
            handle.installEventFilter(self)

    def _popup_frame_destroyed(self, handle, animation):
        if self._popup_motion_window is not handle:return
        remove_visual_frame(handle,self)
        self._popup_motion_window=None;self._popup_motion_started=None
        self._popup_motion_destroyed=None
        # Windows may replace a popup's native handle while its QWidget stays
        # visible. Keep Qt's own animation timer as the lifetime-safe fallback.
        if self._animation is animation and shiboken6.isValid(animation):
            animation.resume()

    def _advance_popup_fade(self):
        animation=self._animation
        if self._popup_motion_started is None or animation is None:return
        if not self.view().window().isVisible():
            self._finish_popup_fade();return
        elapsed=round((time.perf_counter()-self._popup_motion_started)*1000)
        duration=animation.duration()
        # setCurrentTime can synchronously emit finished -> cleanup/reopen.
        # Never reread or retire a different animation after that boundary.
        animation.setCurrentTime(min(duration,elapsed))
        if self._animation is not animation:return
        if elapsed>=duration:self._finish_popup_fade(animation)
        elif self._popup_motion_window is not None:
            request_visual_frame(self._popup_motion_window,self,self._advance_popup_fade)

    def _finish_popup_fade(self, expected=None):
        if expected is not None and self._animation is not expected:return
        handle,self._popup_motion_window=self._popup_motion_window,None
        connection,self._popup_motion_destroyed=self._popup_motion_destroyed,None
        self._popup_motion_started=None
        if handle is not None and shiboken6.isValid(handle):
            if connection is not None:QObject.disconnect(connection)
            remove_visual_frame(handle,self);handle.removeEventFilter(self)
        animation,self._animation=self._animation,None
        if animation is not None and shiboken6.isValid(animation):animation.stop();animation.deleteLater()
        popup=self._popup_motion_surface
        if popup is not None and shiboken6.isValid(popup):popup.setGraphicsEffect(None)
        self._effect=None  # Retire the offscreen effect after opacity reaches1.
        self._popup_motion_surface=None

    def eventFilter(self,watched,event):
        if (watched is self._popup_motion_window and event.type()==QEvent.UpdateRequest
                and not compositor_visual_clock(watched,self)):
            self._advance_popup_fade()
        return super().eventFilter(watched,event)

    def set_category_provider(self, provider):
        """Return a caption path per model row; empty path is a root choice."""
        self._category_provider = provider

    def _show_category_popup(self):
        self.hidePopup()
        menu = NativeMenu(self)
        menu.setToolTipsVisible(True)
        self._category_menu = menu
        parents = {(): menu}
        rows = [(row, tuple(self._category_provider(row) or ())) for row in range(self.count())]
        for row, path in sorted(rows, key=lambda item: bool(item[1])):
            for depth in range(1, len(path) + 1):
                prefix = path[:depth]
                if prefix not in parents:
                    parents[prefix] = parents[prefix[:-1]].addMenu(prefix[-1])
                    parents[prefix].setToolTipsVisible(True)
            action = parents[path].addAction(self.itemText(row))
            detail=self.itemData(row,Qt.ToolTipRole)
            action.setToolTip(str(detail) if detail else '')
            action.setData(row)
            action.setCheckable(True)
            action.setChecked(row == self.currentIndex())
            index = self.model().index(row, self.modelColumn(), self.rootModelIndex())
            action.setEnabled(bool(self.model().flags(index) & Qt.ItemIsEnabled))
            action.triggered.connect(lambda _checked=False, row=row: self._activate_category_row(row))
        reconciler = self._hover_reconciler()
        if reconciler is not None:
            reconciler.popup_opened(self)
        menu.aboutToHide.connect(self.hidePopup)
        menu.popup(self.mapToGlobal(QPoint(0, self.height())))

    def _activate_category_row(self, row):
        # A device selection may synchronously open a modal confirmation.
        # Release the popup/hover capture before invoking that consumer.
        self.hidePopup()
        self.setCurrentIndex(row)
        self.activated.emit(row)

    def _fit_popup_width(self, popup):
        """Popup content preference, never a closed-control/window width floor.

        Native Windows may size this view to the compact field while offscreen
        expands it. Uniform item sizing can also hide a longer final choice.
        Include the delegate's padding/icons and actual scrollbar/frame chrome.
        """
        view = self.view()
        model = view.model()
        root = self.rootModelIndex()
        option = QStyleOptionViewItem()
        view.initViewItemOption(option)
        natural = max((view.itemDelegateForIndex(index).sizeHint(option, index).width()
                       for row in range(model.rowCount(root))
                       for index in (model.index(row, self.modelColumn(), root),)), default=0)
        chrome = max(0, popup.width() - view.viewport().width())
        screen = self.screen().availableGeometry()
        width = min(screen.width(), max(self.width(), natural + chrome))
        geometry = popup.geometry()
        geometry.setWidth(width)
        geometry.moveLeft(max(screen.left(), min(geometry.left(), screen.right() - width + 1)))
        popup.setGeometry(geometry)
        # Windows' private popup layout can retain the pre-resize child width
        # through the show boundary. Widen the real viewport, not just its shell.
        if popup.layout() is not None:
            popup.layout().invalidate()
            popup.layout().activate()
        deficit=natural-view.viewport().width()
        if deficit>0 and popup.width()<screen.width():
            # First activation can change the private popup chrome by a pixel.
            # Correct using the real resulting viewport, not a width floor on
            # the combo/dialog. Keep the shell entirely within the work area.
            geometry=popup.geometry()
            geometry.setWidth(min(screen.width(),popup.width()+deficit))
            geometry.moveLeft(max(screen.left(),min(geometry.left(),screen.right()-geometry.width()+1)))
            popup.setGeometry(geometry)
            if popup.layout() is not None:popup.layout().activate()

    def native_tooltip_content(self,position):
        # Full selected data supplements, never overwrites, semantic help.
        detail=tooltip_plain_text(self.toolTip())
        option=QStyleOptionComboBox();self.initStyleOption(option)
        area=self.style().subControlRect(QStyle.CC_ComboBox,option,QStyle.SC_ComboBoxEditField,self)
        selected=self.currentText()
        clipped=self.fontMetrics().horizontalAdvance(selected)>max(0,area.width())
        if tooltip_repeats_caption(detail,selected):detail=''
        return (selected+("\n" if detail else "") if clipped else '')+detail,self.rect()

    def event(self, event):
        if event.type()==QEvent.ToolTip:return handle_native_tooltip(self,event)
        return super().event(event)

    def hidePopup(self):
        menu, self._category_menu = self._category_menu, None
        if menu is not None:
            menu.hide()
            menu.deleteLater()
        self._finish_popup_fade()
        super().hidePopup()
        popup = self.view().window()
        if popup is not None:
            popup.setGraphicsEffect(None)
        self._effect = self._animation = None
        reconciler = self._hover_reconciler()
        if reconciler is not None:
            reconciler.popup_closed(self)


class SoundFontRowDelegate(QStyledItemDelegate):
    """One popup row painter with a non-selecting hide target."""

    HIDE_WIDTH = 26

    def __init__(self, combo):
        super().__init__(combo)
        self.combo = combo

    def paint(self, painter, option, index):
        palette = self.combo.theme_palette()
        kind = index.data(SOUNDFONT_ROW_KIND_ROLE)
        if kind in (SOUNDFONT_ROW_FILE, SOUNDFONT_ROW_CURRENT, SOUNDFONT_ROW_UNSAVED):
            painter.save()
            enabled=self.combo.isEnabled() and self.combo.view().isEnabled() and bool(index.flags() & Qt.ItemIsEnabled)
            hovered=enabled and bool(option.state & QStyle.State_MouseOver)
            colour = (palette.accent_soft if option.state & QStyle.State_Selected else
                      palette.interaction_hover if hovered else
                      palette.surface)
            painter.fillRect(option.rect, QColor(colour))
            text_option = QStyleOptionViewItem(option)
            if not enabled:text_option.state &= ~(QStyle.State_MouseOver | QStyle.State_Enabled)
            text_option.rect = option.rect.adjusted(
                0, 0, -self.HIDE_WIDTH if kind == SOUNDFONT_ROW_FILE else 0, 0,
            )
            super().paint(painter, text_option, index)
            if kind == SOUNDFONT_ROW_FILE:
                close_rect = self.combo.hide_rect(option.rect)
                painter.setFont(native_text_font(METRICS["body_font_px"]))
                painter.setPen(QColor(disabled_control_color(palette) if not enabled else palette.danger if hovered else palette.muted))
                painter.drawText(close_rect, Qt.AlignCenter, "×")
            painter.restore()
            return
        super().paint(painter, option, index)

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), METRICS["popup_item_height"]))
        return size


class SoundFontComboBox(AnimatedComboBox):
    """Path-aware selector retaining the inherited text/setText contract."""

    favoriteToggled = Signal(str)
    hideRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pending_row_action = None
        self._context_menu = None
        self._favorite_icon = self._make_star_icon()
        self.setIconSize(QSize(16, 16))
        self.setItemDelegate(SoundFontRowDelegate(self))
        self.view().setMouseTracking(True)
        self.view().viewport().setMouseTracking(True)
        self.view().installEventFilter(self)
        self.view().viewport().installEventFilter(self)

    @staticmethod
    def _make_star_icon() -> QIcon:
        image = QPixmap(16, 16)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)
        points = []
        for index in range(10):
            angle = -math.pi / 2 + index * math.pi / 5
            radius = 7.0 if index % 2 == 0 else 3.0
            points.append(QPointF(8 + radius * math.cos(angle), 8 + radius * math.sin(angle)))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#efb326"))
        painter.drawPolygon(QPolygonF(points))
        painter.end()
        return QIcon(image)

    def theme_palette(self) -> NativePalette:
        window = self.window()
        owner = window if hasattr(window, "_dark") else getattr(window, "owner", None)
        return widget_palette(owner)

    def set_favorite_icon(self, index: int, favorite: bool) -> None:
        self.setItemIcon(index, self._favorite_icon if favorite else QIcon())

    @staticmethod
    def hide_rect(row: QRect) -> QRect:
        return QRect(row.right() - SoundFontRowDelegate.HIDE_WIDTH + 1, row.top(),
                     SoundFontRowDelegate.HIDE_WIDTH, row.height())

    def _popup_row(self, position):
        index = self.view().indexAt(position)
        if not index.isValid():
            return None
        kind = index.data(SOUNDFONT_ROW_KIND_ROLE)
        path = index.data(Qt.UserRole)
        if kind not in (SOUNDFONT_ROW_FILE, SOUNDFONT_ROW_CURRENT) or not path:
            return None
        return index, str(path)

    def _emit_row_action(self, action: str, path: str) -> None:
        self.hidePopup()
        (self.favoriteToggled if action == "favorite" else self.hideRequested).emit(path)

    def eventFilter(self, watched, event):
        view = self.view()
        if watched is view and event.type()==QEvent.Hide:
            # Qt can close its native popup container without calling our
            # Python hidePopup override. Never retain that canceled press.
            self._pending_row_action=None
        if (watched in (view,view.viewport()) and event.type()==QEvent.KeyPress
                and event.key()==Qt.Key_Escape):
            self.hidePopup()
            return True
        if watched is view.viewport():
            if event.type() == QEvent.MouseButtonPress:
                row = self._popup_row(event.position().toPoint())
                if row:
                    index, path = row
                    action = ("favorite" if event.button() == Qt.RightButton else
                              "hide" if event.button() == Qt.LeftButton and
                              index.data(SOUNDFONT_ROW_KIND_ROLE) == SOUNDFONT_ROW_FILE and
                              self.hide_rect(view.visualRect(index)).contains(event.position().toPoint()) else None)
                    if action:
                        self._pending_row_action = (action, path, event.button())
                        return True
            elif event.type() == QEvent.MouseButtonRelease and self._pending_row_action:
                action, path, button = self._pending_row_action
                self._pending_row_action = None
                row=self._popup_row(event.position().toPoint())
                if (event.button()!=button or row is None or row[1]!=path
                        or action=='hide' and not self.hide_rect(view.visualRect(row[0])).contains(event.position().toPoint())):
                    return True  # Press-drag-out/release cancels, just like a command button.
                self._emit_row_action(action, path)
                return True
        if watched is view and event.type() == QEvent.KeyPress:
            index = view.currentIndex()
            path = index.data(Qt.UserRole) if index.isValid() else None
            if index.isValid() and index.data(SOUNDFONT_ROW_KIND_ROLE) in (SOUNDFONT_ROW_FILE, SOUNDFONT_ROW_CURRENT) and path:
                if event.key() == Qt.Key_F and event.modifiers() == Qt.NoModifier:
                    self._emit_row_action("favorite", str(path))
                    return True
                if event.key() == Qt.Key_Delete and index.data(SOUNDFONT_ROW_KIND_ROLE) == SOUNDFONT_ROW_FILE:
                    self._emit_row_action("hide", str(path))
                    return True
        if watched in (view, view.viewport()) and event.type() == QEvent.ContextMenu:
            if event.reason()==QContextMenuEvent.Mouse:
                # A mouse release owns Favorite. Windows may also emit a
                # context notification, including after a canceled gesture.
                return True
            position = view.viewport().mapFromGlobal(event.globalPos())
            row = self._popup_row(position) or (
                (view.currentIndex(), str(view.currentIndex().data(Qt.UserRole)))
                if view.currentIndex().isValid() and view.currentIndex().data(SOUNDFONT_ROW_KIND_ROLE) in (SOUNDFONT_ROW_FILE, SOUNDFONT_ROW_CURRENT) else None
            )
            if row:
                _index, path = row
                if self._context_menu is not None:self._context_menu.hide()
                self._context_menu = NativeMenu(self)
                menu=self._context_menu
                def retire():
                    if self._context_menu is menu:self._context_menu=None
                    menu.deleteLater()
                menu.aboutToHide.connect(retire)
                favorite = self.itemIcon(_index.row()).isNull()
                self._context_menu.addAction(
                    tr("soundfont_picker.favorite_add" if favorite else "soundfont_picker.favorite_remove"),
                    lambda: self._emit_row_action("favorite", path),
                )
                if _index.data(SOUNDFONT_ROW_KIND_ROLE) == SOUNDFONT_ROW_FILE:
                    self._context_menu.addAction(
                        tr("soundfont_picker.hide"), lambda: self._emit_row_action("hide", path),
                    )
                self._context_menu.popup(event.globalPos())
                return True
        return super().eventFilter(watched, event)

    def hidePopup(self):
        self._pending_row_action=None
        super().hidePopup()

    def text(self) -> str:
        value = self.currentData(Qt.UserRole)
        if self.currentData(SOUNDFONT_ROW_KIND_ROLE)==SOUNDFONT_ROW_UNSAVED:
            return str(value or '')  # The display caption is never a file path.
        if value and value != SOUNDFONT_PICKER_SENTINEL:
            return str(value)
        return self.currentText() if self.currentText() != tr("action.change_soundfont") else ""

    def setText(self, path: str) -> None:
        if not str(path).strip():
            self.setCurrentIndex(self.findData(SOUNDFONT_PICKER_SENTINEL,Qt.UserRole))
            return
        target = os.path.normcase(os.path.abspath(str(path)))
        for index in range(self.count()):
            value = self.itemData(index, Qt.UserRole)
            if value and value != SOUNDFONT_PICKER_SENTINEL:
                if os.path.normcase(os.path.abspath(str(value))) == target:
                    self.setCurrentIndex(index)
                    return
        # This is only a restoration fallback; normal entries come from the
        # bounded catalogue and are rebuilt by _populate_soundfont_selector.
        command_index = self.findData(SOUNDFONT_PICKER_SENTINEL, Qt.UserRole)
        insert_at = command_index if command_index >= 0 else self.count()
        self.insertItem(insert_at, Path(path).name, str(path))
        self.setCurrentIndex(insert_at)


class DeviceComboBox(AnimatedComboBox):
    """Top-row device identity; popup emphasis follows the committed value."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DeviceCombo")

    def showPopup(self):
        super().showPopup()
        if self._category_provider is not None:
            return
        selected = self.currentIndex()
        if selected < 0:
            return
        index = self.model().index(selected, self.modelColumn(), self.rootModelIndex())
        if index.isValid():
            # The popup has its own selection model. Re-entering it after a
            # modal confirmation must highlight the committed combo item,
            # not an earlier navigation/hover row.
            self.view().selectionModel().setCurrentIndex(
                index, QItemSelectionModel.ClearAndSelect,
            )


class NativeNumberBox(QSpinBox):
    """Shared integer input: validation, geometry, and focus are centralized."""

    def __init__(self, minimum: int, maximum: int, label: str, *, suffix: str = "", parent=None):
        super().__init__(parent)
        self.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.setRange(minimum, maximum)
        self.setKeyboardTracking(False)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setFixedHeight(FORM_ROW_HEIGHT)
        self.setAccessibleName(label)
        if suffix:
            self.setSuffix(suffix)

    def minimumSizeHint(self):return QSize(0,FORM_ROW_HEIGHT)

    def wheelEvent(self,event):
        dispatch_native_wheel(self,event,super().wheelEvent)

    def keyPressEvent(self,event):
        super().keyPressEvent(event)
        # Enter commits this number,not an unrelated dialog-wide command.
        if event.key() in (Qt.Key_Return,Qt.Key_Enter):event.accept()


class NativeDecimalBox(QDoubleSpinBox):
    """Shared bounded decimal form input, matching NativeNumberBox geometry."""
    def __init__(self, minimum, maximum, label, *, decimals=3, suffix="", parent=None):
        super().__init__(parent)
        self.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self.setDecimals(decimals)
        self.setRange(minimum, maximum)
        self.setSingleStep(10**-decimals)
        self.setKeyboardTracking(False)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
        self.setFixedHeight(FORM_ROW_HEIGHT)
        self.setAccessibleName(label)
        self.setSuffix(suffix)

    def minimumSizeHint(self):return QSize(0,FORM_ROW_HEIGHT)

    def wheelEvent(self,event):
        dispatch_native_wheel(self,event,super().wheelEvent)

    def keyPressEvent(self,event):
        super().keyPressEvent(event)
        if event.key() in (Qt.Key_Return,Qt.Key_Enter):event.accept()


class NativeSlider(QSlider):
    """Shared Native track/thumb widget for control rows and the master fader."""

    def __init__(self, orientation: Qt.Orientation, parent=None):
        _install_numeric_accessibility()
        super().__init__(orientation, parent)
        self.setMinimumWidth(0)
        self._wheel_ticks = None
        self.navigation_callback = None
        self._neutral_tick = None
        self._reset_release = False
        self.absolute_position = False
        self._absolute_drag = False
        self.valueChanged.connect(self._notify_accessible_value)

    def set_accessible_domain(self, domain, formatter=None, *, read=None, write=None, step=None):
        _configure_numeric_accessibility(self, domain, formatter, read=read, write=write, step=step)

    def _notify_accessible_value(self, *_args):
        _notify_numeric_accessibility(self)

    def set_unknown(self,unknown):
        # Keep QSlider's paint path entirely native, even for known values.
        # Unknown visuals use the shared QSS property only on state transitions.
        if bool(self.property('unknownValue'))==bool(unknown):return
        self.setProperty('unknownValue',bool(unknown))
        self.style().unpolish(self);self.style().polish(self)
        self.update();self._notify_accessible_value()

    def set_neutral_tick(self, value: int | None) -> None:
        self._neutral_tick = value

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and self._neutral_tick is not None:
            self._reset_release = True
            self.setSliderDown(False)
            self.setRepeatAction(QSlider.SliderNoAction)
            unchanged_mixed = self.value() == self._neutral_tick and bool(self.property("mixedValue"))
            self.setValue(self._neutral_tick)
            if unchanged_mixed: self.valueChanged.emit(self.value())
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def set_input_ticks(self, key: int, wheel: int, track: int) -> None:
        """Declare three distinct discrete gestures; thumb drag keeps 1 tick."""
        if not 1 <= key < wheel < track:
            raise ValueError("Slider input ticks must satisfy 1 <= key < wheel < track")
        self._wheel_ticks = wheel
        state=getattr(self,'_native_wheel_input',None)
        if state is not None:state.context=None;state.remainder=0
        self.setSingleStep(key)
        self.setPageStep(track)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        if self.absolute_position:
            self._absolute_drag = True
            self.setSliderDown(True)
            self._set_absolute_position(event)
            event.accept()
            return
        self._reset_release = False
        point = event.position().toPoint()
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        handle = self.style().subControlRect(
            QStyle.CC_Slider, option, QStyle.SC_SliderHandle, self,
        )
        if handle.contains(point) or not self.rect().contains(point):
            super().mousePressEvent(event)
            return
        # The painted line is 4px high, but the full control area is a track
        # click target. Handle presses still use Qt's pixel-precise dragging.
        if self.orientation() == Qt.Horizontal:
            direction = 1 if point.x() > handle.center().x() else -1
        else:
            direction = 1 if point.y() < handle.center().y() else -1
        if self.invertedAppearance():
            direction = -direction
        if self.navigation_callback:
            self.navigation_callback(direction, "track")
        else:
            self.setValue(self.value() + direction * self.pageStep())
        event.accept()

    def mouseMoveEvent(self, event):
        if self._absolute_drag:
            self._set_absolute_position(event)
            event.accept()
        elif self._reset_release:
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def cancel_pointer_gesture(self):
        self._absolute_drag=False;self._reset_release=False
        blocked=self.blockSignals(True)
        try:
            # QSlider retains a private pressedControl independently of
            # sliderDown. Its base release clears that native hit/grab state;
            # setSliderDown(False) alone lets later idle movement change value.
            # Discard an uncommitted tracking=False position before release.
            self.setSliderPosition(self.value())
            point=QPointF(self.rect().center())
            from PySide6.QtGui import QMouseEvent
            QSlider.mouseReleaseEvent(self,QMouseEvent(QEvent.MouseButtonRelease,
                point,QPointF(self.mapToGlobal(point.toPoint())),Qt.LeftButton,Qt.NoButton,Qt.NoModifier))
            self.setSliderDown(False);self.setRepeatAction(QSlider.SliderNoAction)
        finally:self.blockSignals(blocked)

    def mouseReleaseEvent(self, event):
        if self._absolute_drag and event.button() == Qt.LeftButton:
            self._set_absolute_position(event)
            self._absolute_drag = False
            self.setSliderDown(False)
            event.accept()
        elif self._reset_release and event.button() == Qt.LeftButton:
            self._reset_release = False
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def _set_absolute_position(self, event):
        """Transport timelines seek to the clicked position, not +/-pageStep.

        QStyle supplies handle geometry and upsideDown for orientation/RTL;
        sliders used for audio parameter stepping retain their old policy.
        """
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        handle = self.style().subControlRect(QStyle.CC_Slider, option, QStyle.SC_SliderHandle, self)
        horizontal = self.orientation() == Qt.Horizontal
        extent = handle.width() if horizontal else handle.height()
        span = max(0, (self.width() if horizontal else self.height())-extent)
        coordinate = event.position().x() if horizontal else event.position().y()
        value = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
            max(0, min(span, round(coordinate-extent/2))), span, option.upsideDown)
        self.setValue(value)

    def keyPressEvent(self, event):
        directions = {Qt.Key_Left: (-1, "key"), Qt.Key_Down: (-1, "key"),
                      Qt.Key_PageDown: (-1, "track"), Qt.Key_Right: (1, "key"),
                      Qt.Key_Up: (1, "key"), Qt.Key_PageUp: (1, "track")}
        if self.navigation_callback and event.key() in directions:
            direction, gesture = directions[event.key()]
            self.navigation_callback(direction * (-1 if self.invertedControls() else 1), gesture)
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self, event):
        if self._wheel_ticks is None:
            dispatch_native_wheel(self,event,super().wheelEvent)
            return
        decoded=native_wheel_steps(self,event)
        if decoded is None:return
        axis,detents=decoded
        if axis=='x':detents=-detents  # Match Qt's horizontal-wheel direction.
        if detents:
            direction = -1 if self.invertedControls() else 1
            if self.navigation_callback:
                self.navigation_callback(direction * detents, "wheel")
            else:
                self.setValue(self.value() + direction * detents * self._wheel_ticks)
        event.accept()


_numeric_accessible_adapters = weakref.WeakKeyDictionary()
_numeric_accessible_factory_installed = False


def invoke_native_visibility(widget,operation):
    """Same synchronous GUI-owned QWidget slot, not a worker/queued mutation."""
    if operation not in ('show','showNormal','showMinimized','hide'):
        raise ValueError('unsupported native visibility operation')
    if QThread.currentThread()!=widget.thread():
        raise RuntimeError('native visibility must run on its Qt owner thread')
    if not QMetaObject.invokeMethod(widget,operation,Qt.DirectConnection):
        getattr(QWidget,operation)(widget)  # Never the overriding Python method.


class NativeChannelCommandPresentation(QObject):
    """One GUI-owned receipt bridge; engine commands never own Qt widgets.

    Only queued intent is overlaid on readback. Audio and acknowledgement are
    never coalesced: latest-only applies to presentation of overlapping edits.
    """
    completed = Signal(int, object)

    def __init__(self,parent,submitter,legacy_setter,context_getter,refresh,save,*,mode_submitter=None,legacy_mode_setter=None,mode_intent=None,
                 auxiliary_submitter=None,legacy_auxiliary_setter=None):
        super().__init__(parent)
        self._submitter=submitter;self._legacy=legacy_setter;self._context=context_getter
        self._refresh=refresh;self._save=save;self._serial=0;self._requests={};self._latest={}
        self._mode_submitter=mode_submitter;self._legacy_mode=legacy_mode_setter;self._mode_intent=mode_intent
        self._auxiliary_submitter=auxiliary_submitter;self._legacy_auxiliary=legacy_auxiliary_setter
        self._closed=False
        self.completed.connect(self._complete,Qt.QueuedConnection)

    @staticmethod
    def _get(receipt,key,default=None):
        return receipt.get(key,default) if isinstance(receipt,dict) else getattr(receipt,key,default)

    @classmethod
    def accepted(cls,receipt):
        return bool(cls._get(receipt,'accepted',False) and
                    (cls._get(receipt,'mode')=='queued' or cls._get(receipt,'successful',False)))

    def submit(self,channels,field,amount,*,persist=False):
        if QThread.currentThread()!=self.thread():raise RuntimeError('channel presentation must run on GUI thread')
        if self._closed:return dict(mode='refused',accepted=False,completed=True,successful=False,state_changed=False)
        channels=tuple(channels);self._serial+=1;serial=self._serial
        is_mode=field in ('mode_mono','mode_poly','mode_legato')
        is_auxiliary=field.startswith('aux_')
        auxiliary_number=int(field.removeprefix('aux_')) if is_auxiliary else None
        display_field='mode' if is_mode else field
        keys=tuple((ch,display_field,self._context(ch,display_field)) for ch in channels)
        intents=tuple(self._mode_intent(ch,field,amount,self.pending_value(ch,'mode'))
            if is_mode and self._mode_intent is not None else amount for ch in channels)
        self._requests[serial]=(keys,persist)
        reference=weakref.ref(self)
        def finish(receipt):
            bridge=reference()
            if bridge is not None and shiboken6.isValid(bridge) and not bridge._closed:
                bridge.completed.emit(serial,receipt)
        try:
            submitter=(self._auxiliary_submitter() if is_auxiliary and self._auxiliary_submitter is not None else
                       self._mode_submitter() if is_mode and self._mode_submitter is not None else self._submitter())
            if submitter is None:
                setter=self._legacy_auxiliary if is_auxiliary else self._legacy_mode if is_mode else self._legacy
                results=tuple(setter(ch,auxiliary_number if is_auxiliary else field,amount) for ch in channels)
                receipt=dict(mode='legacy',accepted=True,completed=True,successful=all(results),state_changed=True)
            else:receipt=submitter(channels,auxiliary_number if is_auxiliary else field,amount,on_complete=finish)
        except BaseException:
            self._requests.pop(serial,None);raise
        mode=self._get(receipt,'mode')
        if mode=='queued' and self._get(receipt,'accepted',False):
            for key,intent in zip(keys,intents):self._latest[key]=(serial,intent)
        else:
            self._requests.pop(serial,None)
            if mode=='legacy':
                if persist:self._save()
            else:self._refresh()
        return receipt

    def pending_value(self,channel,field):
        if self._closed or not self._latest:return None
        value=self._latest.get((channel,field,self._context(channel,field)))
        return None if value is None else value[1]

    def _complete(self,serial,receipt):
        request=self._requests.pop(serial,None)
        if request is None or self._closed:return
        keys,persist=request
        for key in keys:
            if self._latest.get(key,(None,None))[0]==serial:self._latest.pop(key,None)
        # Saved-before-audio-failure metadata is still a committed edit.
        if persist and self._get(receipt,'state_changed',False):self._save()
        self._refresh()

    def close_presentation(self):
        self._closed=True;self._requests.clear();self._latest.clear()


class NativeSnapshotCommandPresentation(QObject):
    """Copied worker snapshots with GUI-only installation and final-save flush.

    Audio acknowledgements are not coalesced. Latest-only and per-field edits
    apply only to presentation; no BASS, INI or widget calls on the worker.
    """
    ready=Signal()

    def __init__(self,parent,context,apply,save):
        super().__init__(parent)
        self._context=context;self._apply=apply;self._save=save
        self._lock=threading.RLock();self._serial=0;self._latest={};self._revisions={}
        self._requests={};self._closed=False
        self.ready.connect(self._ready,Qt.QueuedConnection)

    def touch(self,field):
        with self._lock:self._revisions[field]=self._revisions.get(field,0)+1

    def invalidate(self,group):
        with self._lock:
            self._serial+=1;self._latest[group]=self._serial

    def pending(self,group):
        """Presentation guard only; never poll or wait for native work."""
        with self._lock:
            return any(request['group']==group and request['completion'] is None and
                       self._latest.get(group)==serial and request['context']==self._context()
                       for serial,request in self._requests.items())

    def submit(self,group,fields,submitter):
        if QThread.currentThread()!=self.thread():raise RuntimeError('snapshot presentation GUI owner')
        with self._lock:
            if self._closed:return None
            self._serial+=1;serial=self._serial;self._latest[group]=serial
            request=dict(group=group,context=self._context(),revisions={field:self._revisions.get(field,0) for field in fields},
                         completion=None)
            self._requests[serial]=request
        reference=weakref.ref(self)
        def completed(receipt,values):
            bridge=reference()
            if bridge is None:return
            with bridge._lock:
                record=bridge._requests.get(serial)
                if bridge._closed or record is None or record['completion'] is not None:return
                record['completion']=(receipt,None if values is None else dict(values))
            if shiboken6.isValid(bridge):bridge.ready.emit()
        try:receipt=submitter(completed)
        except BaseException:
            with self._lock:self._requests.pop(serial,None)
            raise
        if not NativeChannelCommandPresentation._get(receipt,'accepted',False):
            with self._lock:self._requests.pop(serial,None)
        return receipt

    def submit_receipt(self,group,submitter):
        """One common queued GUI acknowledgement, with no invented snapshot."""
        return self.submit(group,(),lambda completed:submitter(lambda receipt:completed(receipt,{})))

    def flush(self):
        if QThread.currentThread()!=self.thread():raise RuntimeError('snapshot flush GUI owner')
        changed=False
        with self._lock:
            if self._closed:return False
            context=self._context()
            for serial,request in tuple(self._requests.items()):
                completion=request['completion']
                if completion is None:continue
                if request['context']!=context or self._latest.get(request['group'])!=serial:
                    self._requests.pop(serial);continue
                receipt,values=completion
                if values is not None:
                    values={field:value for field,value in values.items()
                            if self._revisions.get(field,0)==request['revisions'].get(field,0)}
                # A failed GUI installation retains the copied checkpoint.
                # Retrying save/presentation never executes the macro again.
                self._apply(request['group'],receipt,values)
                self._requests.pop(serial);changed=True
        return changed

    def _ready(self):
        if self.flush():self._save()

    def close_presentation(self):
        with self._lock:self._closed=True;self._requests.clear();self._latest.clear()


def native_tooltip_stylesheet(p: NativePalette) -> str:
    """Return the QToolTip rule at QApplication scope.

    Qt tooltips are independent top-level popup widgets.  Applying this rule
    to the QApplication gives their box model one stable owner across hover
    handoffs between unrelated controls.
    """
    return f"""
        QToolTip {{
            color: palette(tooltip-text); background: palette(tooltip-base); border: 1px solid palette(mid);
            border-radius: 7px; margin: 0px; padding: 6px 8px;
            font-family: "{project_noto_family()}"; font-size: {METRICS['small_font_px']}px; font-weight: 400;
        }}
    """


def apply_native_tooltip_theme(p: NativePalette) -> None:
    """One invariant popup box; palette changes never repolish the whole app."""
    app = QApplication.instance()
    if app is not None:
        install_tooltip_wrap_filter(app)
        stylesheet = native_tooltip_stylesheet(p)
        if app.styleSheet() != stylesheet:
            apply_native_stylesheet(app,stylesheet)
        colors = QPalette()
        colors.setColor(QPalette.ToolTipText, QColor(p.text))
        colors.setColor(QPalette.ToolTipBase, QColor(p.surface))
        colors.setColor(QPalette.Mid, QColor(p.border))
        if QToolTip.palette() != colors:
            QToolTip.hideText()
            QToolTip.setPalette(colors)
        QToolTip.setFont(native_text_font(METRICS["small_font_px"]))


def native_stylesheet(p: NativePalette, compact: bool = False) -> str:
    """All visual tokens are centralized; no widget owns an ad-hoc colour."""
    # Qt Widgets cannot provide a real Windows backdrop blur without WinUI
    # interop. Use one restrained translucent surface layer instead: Fluent
    # reserves Acrylic for transient surfaces and avoids stacked glass panes.
    light = QColor(p.window).lightness() > 128
    surface = QColor(p.surface)
    glass = f"rgba({surface.red()}, {surface.green()}, {surface.blue()}, 235)"
    selection = p.selection
    selected_button_text = p.accent_hover if light else p.accent
    # One semantic disabled treatment for every control: retain legible text,
    # remove the live accent, and distinguish locked values from editable ones.
    disabled_color = disabled_control_color(p)
    no_effect_color = QColor(p.danger)
    no_effect_fill = f"rgba({no_effect_color.red()}, {no_effect_color.green()}, {no_effect_color.blue()}, 16)"
    no_effect_edge = f"rgba({no_effect_color.red()}, {no_effect_color.green()}, {no_effect_color.blue()}, 110)"
    control_height = FORM_ROW_HEIGHT - 2  # QSS border adds 1px on each side.
    button_height = FORM_ROW_HEIGHT - 2
    popup_row_height = METRICS["popup_item_height"] if compact else 34
    tab_font = METRICS["tab_font_px"]
    tab_padding = "6px 12px 5px" if compact else "10px 16px 9px"
    control_border = METRICS["control_border_width"]
    control_radius = METRICS["control_corner_radius"]
    # QSS padding alone doesn't reserve the native submenu arrow column.
    # Keep the12px text inset and add one font/indicator-sized trailing gutter.
    menu_arrow_gutter = max(METRICS["body_font_px"], QApplication.instance().style().pixelMetric(QStyle.PM_MenuButtonIndicator))
    return f"""
        QMainWindow, QDialog {{ background: {p.window}; color: {p.text}; }}
        QScrollArea#PreferencesScroll, QWidget#PreferencesContent {{ background: transparent; border: none; }}
        QScrollArea#NativeMainScroll, QWidget#NativeScrollViewport, QWidget#NativeScrollContent {{ background: {p.window}; }}
        QScrollArea#ChannelMonitorScroll, QScrollArea#ChannelMonitorVerticalScroll,
        QWidget#ChannelMonitorContents,
        QWidget#qt_scrollarea_viewport {{ background: transparent; border: 0; }}
        QWidget#ChannelMonitorBody, QWidget#ChannelMonitorAxis {{ background: {p.surface}; }}
        /* The project Variable TTF is registered before this stylesheet is
           applied. Every active widget uses that exact returned family. */
        QWidget {{ color: {p.text}; font-family: "{project_noto_family()}"; font-size: {METRICS['body_font_px']}px; font-weight: 400; }}
        {native_tooltip_stylesheet(p)}
        QLabel#AppTitle {{ font-size: {METRICS['title_font_px']}px; }}
        QLabel#AppSubtitle, QLabel#MutedLabel {{ color: {p.muted}; font-size: {METRICS['small_font_px']}px; }}
        QLabel#StatCaption {{ color: {p.accent}; font-size: {METRICS['state_caption_font_px']}px; }}
        QLabel#CardTitle, QLabel#DialogTitle {{ font-size: {METRICS['card_title_font_px']}px; }}
        QLabel#DialogTitle {{ font-size: {METRICS['dialog_title_font_px']}px; }}
        QLabel#StatValue {{ font-size: {METRICS['state_value_font_px']}px; }}
        QLabel#FieldLabel {{ color: {p.label}; }}
        QLabel#FieldLabel:disabled, QLabel#FieldValue:disabled {{ color: {disabled_color}; }}
        QLabel[nativeKnobPart="caption"], QLabel#ChannelEditorFieldLabel {{ color: {p.label}; font-size: {METRICS['small_font_px']}px; }}
        QWidget[ccNoEffect="true"] QLabel#KnobFieldLabel, QWidget[ccNoEffect="true"] QLabel#ChannelEditorFieldLabel {{ color: {p.danger}; }}
        QWidget[ccNoEffect="true"] {{ background: {no_effect_fill}; border: 1px dashed {no_effect_edge}; border-radius: 4px; }}
        QWidget[ccNoEffect="true"] QLabel, QWidget[ccNoEffect="true"] QWidget {{ background: transparent; border: 0; }}
        QWidget[ccLearning="true"] {{ border: 1px solid {p.accent}; border-radius: 4px; }}
        QWidget[ccLearning="true"] QLabel, QWidget[ccLearning="true"] QWidget {{ border: 0; }}
        QLabel[nativeKnobPart="readout"] {{ color: {p.text}; font-size: {METRICS['small_font_px']}px; }}
        QLabel[nativeKnobPart="detail"] {{ color: {p.label}; font-size: {METRICS['small_font_px']}px; }}
        QLabel[nativeEditableField="true"][nativeEntryAvailable="true"]:enabled:hover {{ color: {p.accent}; }}
        QLabel#KnobFieldLabel:disabled, QLabel#KnobFieldValue:disabled, QLabel#KnobFieldDetail:disabled,
        QLabel#ChannelEditorFieldLabel:disabled, QLabel[nativeEditableField="true"][nativeEntryAvailable="false"] {{ color: {disabled_color}; }}
        QLabel#PresetIdentity {{
            color: {p.text}; background: {p.input}; border: 1px solid {p.border};
            border-radius: 7px; padding: 6px 8px;
        }}
        QLabel#ValuePill, QLabel#StatusChip {{
            color: {p.accent}; background: {p.accent_soft}; border-radius: 10px; padding: 4px 8px;
        }}
        QFrame#NativeCard {{
            background: {glass}; border: 1px solid {p.border}; border-radius: 12px;
        }}
        QFrame#NativeCardCompact {{
            background: {glass}; border: 1px solid {p.border}; border-radius: 12px;
        }}
        QFrame#ChannelEditorFrame {{
            background: transparent; border: 1px solid {p.border}; border-radius: 12px;
        }}
        QFrame#ChannelEditorSection {{
            background: transparent; border: 1px solid {p.border_subtle}; border-radius: 6px;
        }}
        QFrame#ChannelEditorSection[nativeSectionBackground="card"] {{ background: {glass}; }}
        QLabel#ChannelEditorSectionTitle {{
            color: {p.muted}; background: {p.surface_alt};
            border-bottom: 1px solid {p.border_subtle};
            font-size: {METRICS['small_font_px']}px; font-weight: 400;
            padding: 2px 4px;
        }}
        QFrame#ChannelEditorSectionHeader {{ background: {p.surface_alt}; border-bottom: 1px solid {p.border_subtle}; }}
        QFrame#ChannelEditorSectionHeader QLabel#ChannelEditorSectionTitle {{ background: transparent; border: 0; }}
        QFrame#ChannelEditorInputSeparator {{ background: {p.border_subtle}; border: 0; max-width: 1px; }}
        QWidget#ChannelEditorCanvas {{ background: {p.window}; }}
        QFrame#ChannelStrip {{ background: {p.surface_alt}; border: 1px solid {p.border_subtle}; border-radius: 8px; }}
        QLabel#ChannelStripProgram {{ color: {p.muted}; font-size: 12px; }}
        QLabel#ChannelStripTitle {{ color: {p.text}; font-size: 12px; font-weight: 700; }}
        QLabel#ChannelKnobValue {{ font-size: {METRICS['small_font_px']}px; }}
        QPushButton#ChannelMonitorGlobalEdit {{ padding: 0 2px; font-size: 12px; }}
        QPushButton[compactAction="true"] {{ padding: 0; font-size: {METRICS['compact_action_font_px']}px; border-radius: 4px; }}
        QPushButton#ChannelMonitorAction:focus {{ border-color: {p.accent}; }}
        QFrame#Divider {{ background: {p.border_subtle}; max-width: 1px; }}
        QFrame#HorizontalDivider {{ background: {p.border_subtle}; border: none; }}
        QFrame#NativeCard[fontDropTarget="true"] {{ border: 1px solid {p.accent}; }}
        QLabel[exportResult="failed"] {{ color: {p.danger}; }}
        QLabel[exportResult="done"] {{ color: {p.success}; }}
        QPushButton#PrimaryButton[exportBusy="true"] {{ background: {p.surface_alt}; color: {p.muted}; border: 1px solid {p.border}; }}
        QFrame#StateDivider {{ background: {p.border}; max-width: 1px; }}
        QLineEdit, QComboBox {{
            min-height: {control_height}px; background: {p.input}; border: {control_border}px solid {p.border};
            border-radius: {control_radius}px; padding: 0 10px;
        }}
        QLineEdit {{ selection-background-color: {selection}; selection-color: #ffffff; }}
        QLineEdit:hover:enabled, QComboBox:hover:enabled, QSpinBox:hover:enabled, QDoubleSpinBox:hover:enabled {{ background: {p.surface}; border-color: {p.accent_hover}; }}
        QLineEdit:focus, QComboBox:focus {{ border: 2px solid {p.accent}; padding: 0 9px; }}
        QLineEdit#InlineNumberEditor {{
            min-height: 0px; padding: 0 2px; border: 1px solid {p.border};
            border-radius: 2px; background: {p.input}; color: {p.text};
        }}
        QLineEdit#InlineNumberEditor:focus {{
            border: 1px solid {p.accent}; padding: 0 2px;
        }}
        QSpinBox:focus, QDoubleSpinBox:focus {{ border: 2px solid {p.accent}; padding: 0 7px; }}
        QPlainTextEdit#SupportText {{
            background: {p.input}; color: {p.text}; border: 1px solid {p.border};
            border-radius: {control_radius}px; padding: 6px;
            selection-background-color: {selection}; selection-color: #ffffff;
        }}
        QPlainTextEdit#SupportText:focus {{ border-color: {p.accent}; }}
        QPlainTextEdit#SupportText QScrollBar:vertical {{ background: {p.input}; }}
        QPlainTextEdit#StatusLog {{
            font-family: "{project_noto_family()}"; font-size: {STATUS_LOG_FONT_PX}px; font-weight: 400;
            background: transparent; color: {p.text}; border: none;
            padding: 2px; selection-background-color: {selection}; selection-color: #ffffff;
        }}
        QLabel#StatusLogTitle {{ color: {p.muted}; font-family: "{project_noto_family()}"; font-size: {METRICS['card_title_font_px']}px; font-weight: 400; }}
        QLabel#EngineLoadValue {{ color: {p.text}; font-size: {METRICS['engine_load_value_font_px']}px; }}
        QComboBox::drop-down {{ border: none; width: 30px; }}
        QComboBox[compactChannelField="true"] {{ padding: 0 5px; }}
        QComboBox[compactChannelField="true"]:focus {{ padding: 0 4px; }}
        QComboBox[compactChannelField="true"]::drop-down {{ width: 22px; }}
        QComboBox::down-arrow {{ width: 0px; height: 0px; }}
        /* Qt fallback uses stock combos, not the custom-painted combo arrow. */
        QFileDialog QComboBox::down-arrow {{ width: 12px; height: 12px; }}
        /* Icon-only file navigation is not a padded caption button. */
        QFileDialog QToolButton {{ padding: 0px; min-width: 0px; min-height: 0px; }}
        QFileDialog QAbstractItemView {{
            background: {p.input}; color: {p.text};
            selection-background-color: {p.accent_soft}; selection-color: {p.text};
            border: 1px solid {p.border}; outline: 0;
        }}
        {native_item_hover_stylesheet(p, 'QFileDialog QAbstractItemView::item')}
        QFileDialog QAbstractItemView::item:selected {{ background: {p.accent_soft}; color: {p.text}; }}
        /* Table controls fit the shared row, not the normal form-height floor. */
        QComboBox#NativeTableChoice {{ min-height: 0px; }}
        QComboBox QAbstractItemView {{
            background: {p.surface}; border: 1px solid {p.border}; selection-background-color: {p.accent_soft};
            selection-color: {p.text}; outline: 0; padding: 4px;
        }}
        QComboBox QAbstractItemView::item {{ min-height: {popup_row_height}px; padding: 0 8px; border-radius: 5px; }}
        {native_item_hover_stylesheet(p, 'QComboBox QAbstractItemView::item')}
        QComboBox QAbstractItemView::item:selected {{ background: {p.accent_soft}; }}
        QMenu {{ background: {p.surface}; color: {p.text}; border: 1px solid {p.border}; border-radius: 7px; padding: 4px; }}
        QMenu::item {{ padding: 5px {12 + menu_arrow_gutter}px 5px 12px; border-radius: 5px; }}
        QMenu::item:selected:enabled {{ background: {p.accent_soft}; color: {p.text}; }}
        QMenu::item:disabled {{ color: {disabled_color}; background: transparent; }}
        QPushButton, QToolButton {{
            color: {p.text}; background: {p.surface_alt}; border: {control_border}px solid {p.border};
            border-radius: {control_radius}px; padding: 0 12px;
        }}
        {native_command_stylesheet(p)}
        QMessageBox QPushButton {{ text-align: center; }}
          QPushButton#EQBandHandle, QPushButton#EQBandHandle:hover,
          QPushButton#EQBandHandle:pressed, QPushButton#EQBandHandle:focus {{
              background: transparent; border: none; padding: 0; min-width: 0; min-height: 0;
          }}
        QFrame#ThemeSwitch {{ background: {p.surface_alt}; border: 1px solid {p.border}; border-radius: 8px; }}
        QFrame#NativeSegmentedChoice {{ background: {p.surface_alt}; border: {control_border}px solid {p.border}; border-radius: {control_radius}px; }}
        QPushButton#SegmentStart, QPushButton#SegmentEnd, QPushButton#SegmentMiddle {{
            min-height: 0px; border: 0; border-radius: 0;
            padding: 0 {NativeSegmentedChoice.INLINE_PADDING}px;
            background: transparent; color: {p.label}; font-weight: 400;
        }}
        QPushButton#SegmentStart {{ border-top-left-radius: 6px; border-bottom-left-radius: 6px; }}
        QPushButton#SegmentEnd {{ border-top-right-radius: 6px; border-bottom-right-radius: 6px; }}
        QPushButton#SegmentStart:hover:!checked:enabled, QPushButton#SegmentEnd:hover:!checked:enabled, QPushButton#SegmentMiddle:hover:!checked:enabled {{ background: {p.interaction_hover}; color: {p.text}; }}
        QPushButton#SegmentStart:checked, QPushButton#SegmentEnd:checked, QPushButton#SegmentMiddle:checked {{ background: {p.accent_soft}; color: {selected_button_text}; font-weight: 700; }}
        QPushButton#SegmentStart:checked:hover:enabled, QPushButton#SegmentEnd:checked:hover:enabled, QPushButton#SegmentMiddle:checked:hover:enabled {{ color: {p.accent_hover}; }}
        QPushButton#SegmentStart:disabled, QPushButton#SegmentEnd:disabled, QPushButton#SegmentMiddle:disabled {{ color: {disabled_color}; }}
        QToolButton#FilterButton {{ min-height: {button_height}px; border-radius: 6px; padding: 0 9px; }}
        QToolButton#FilterButton:checked {{ font-weight: 700; }}
        QFrame#NativeFilterBar {{ background: {p.surface_alt}; border: 1px solid {p.border}; border-radius: 8px; }}
        QFrame#FontDropLine {{ background: {p.accent}; border: 0; }}
        QTabWidget::pane {{ border: 1px solid {p.border_subtle}; border-radius: 8px; top: -1px; }}
        QTabBar::tab {{ background: transparent; color: {p.muted}; font-size: {tab_font}px; padding: {tab_padding}; margin-right: 3px; border-bottom: 2px solid transparent; }}
        QTabBar::tab:selected {{ color: {p.accent}; border-bottom-color: {p.accent}; font-weight: 700; }}
        /* The eight main effect tabs use the shared 15px tab scale. The selected
           600 state is intentional; other selected controls use 700. */
        QTabWidget#EffectTabs QTabBar::tab, QTabWidget[parameterTabs="true"] QTabBar::tab {{ font-family: "{project_noto_family()}"; font-size: {EFFECT_TAB_FONT_PX}px; font-weight: 400; }}
        QTabWidget#EffectTabs QTabBar::tab:selected, QTabWidget[parameterTabs="true"] QTabBar::tab:selected {{ font-weight: 600; }}
        QTabBar::tab:hover:!selected:enabled {{ color: {p.text}; background: {p.surface_alt}; border-radius: 6px; }}
        QTabBar::tab:selected:hover:enabled {{ background: {p.accent_soft}; border-radius: 6px; }}
        QTabBar::tab:disabled, QTabBar::tab:selected:disabled,
        QTabWidget#EffectTabs QTabBar::tab:disabled, QTabWidget#EffectTabs QTabBar::tab:selected:disabled,
        QTabWidget[parameterTabs="true"] QTabBar::tab:disabled, QTabWidget[parameterTabs="true"] QTabBar::tab:selected:disabled {{ color: {disabled_color}; background: transparent; border-bottom-color: transparent; font-weight: 400; }}
        /* These16px Qt navigation buttons are not padded text actions. */
        QTabBar QToolButton {{ min-height: 0px; padding: 0px; }}
        QSlider::groove:horizontal {{ height: 4px; background: {p.border}; border-radius: 2px; }}
        QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 2px; }}
        QSlider::handle:horizontal {{ width: 14px; margin: -5px 0; border-radius: 7px; background: {p.surface}; border: 2px solid {p.accent}; }}
        QSlider::handle:horizontal:hover:enabled {{ background: {p.accent_soft}; }}
        QSlider::groove:horizontal:disabled, QSlider::sub-page:horizontal:disabled {{ background: {p.border_subtle}; }}
        QSlider::handle:horizontal:disabled {{ background: {p.surface_alt}; border-color: {p.border}; }}
        QWidget#MasterFader {{ background: transparent; border-left: 1px solid {p.border_subtle}; }}
        QLabel#MasterFaderTitle {{ color: {p.label}; font-size: {METRICS['master_title_font_px']}px; }}
        QLabel#MasterFaderValue {{ font-size: {METRICS['master_value_font_px']}px; }}
        QSlider::groove:vertical {{ width: 4px; background: {p.border}; border-radius: 2px; }}
        QSlider#ChannelMonitorFader::groove:vertical {{ width: 2px; border-radius: 1px; }}
        /* Qt's vertical sub-page is geometrically above the handle. The
           audible value must fill upward from the minimum at the bottom. */
        QSlider::sub-page:vertical {{ background: {p.border}; border-radius: 2px; }}
        QSlider::add-page:vertical {{ background: {p.accent}; border-radius: 2px; }}
        QSlider::handle:vertical {{ height: 14px; margin: 0 -5px; border-radius: 7px; background: {p.surface}; border: 2px solid {p.accent}; }}
        QSlider#ChannelMonitorFader::handle:vertical {{ height: 11px; margin: 0 -4px; border-radius: 5px; }}
        QSlider::handle:vertical:hover:enabled {{ background: {p.accent_soft}; }}
        QSlider::groove:vertical:disabled, QSlider::add-page:vertical:disabled {{ background: {p.border_subtle}; }}
        QSlider::handle:vertical:disabled {{ background: {p.surface_alt}; border-color: {p.border}; }}
        QSlider#ChannelMonitorFader[unknownValue="true"]::handle:vertical {{ background: transparent; border-color: transparent; }}
        QSlider#ChannelMonitorFader[unknownValue="true"]::add-page:vertical {{ background: {p.border}; }}
        QCheckBox, QRadioButton {{ spacing: 8px; color: {p.text}; }}
        QCheckBox:disabled, QRadioButton:disabled {{ color: {disabled_color}; }}
        QCheckBox:hover:enabled, QRadioButton:hover:enabled {{ color: {p.accent_hover}; }}
        /* OutlineCheckBox paints every state; QSS supplies geometry only. */
        QCheckBox::indicator {{ width: 16px; height: 16px; }}
        QSpinBox, QDoubleSpinBox {{ min-height: {control_height}px; background: {p.input}; border: {control_border}px solid {p.border}; border-radius: {control_radius}px; padding: 0 8px; }}
        QSpinBox:disabled, QDoubleSpinBox:disabled, QLineEdit:disabled, QComboBox:disabled {{ color: {disabled_color}; background: {p.surface_alt}; border-color: {p.border_subtle}; }}
        QRadioButton::indicator {{ width: 15px; height: 15px; border: 1px solid {p.border}; border-radius: 8px; background: {p.input}; }}
        QRadioButton::indicator:checked {{
            border-color: {p.accent};
            background: qradialgradient(cx: 0.5, cy: 0.5, radius: 0.5,
                stop: 0 {p.accent}, stop: 0.45 {p.accent},
                stop: 0.46 {p.input}, stop: 1 {p.input});
        }}
        QRadioButton::indicator:checked:hover:enabled {{ border-color: {p.accent_hover}; }}
        QListWidget {{ background: {p.surface}; alternate-background-color: {p.surface_alt}; color: {p.text}; border: 1px solid {p.border}; border-radius: 7px; }}
        QListWidget::item {{ padding: 4px 6px; border: 1px solid transparent; }}
        {native_item_hover_stylesheet(p, 'QListWidget::item')}
        QListWidget::item:selected {{ background: {selection}; color: #ffffff; }}
        QListWidget[passiveRows="true"]::item:hover:!selected:enabled {{ background: transparent; }}
        QListWidget#LicenseComponents, QTextBrowser#LicenseDocument {{ background: {p.input}; color: {p.text}; border: 0; }}
        QListWidget#LicenseComponents::item {{ padding: 6px 8px; }}
        {native_item_hover_stylesheet(p,'QListWidget#LicenseComponents::item')}
        QListWidget#LicenseComponents::item:selected {{ background: {p.selection}; color: #ffffff; }}
        QTableWidget {{ background: {p.surface}; alternate-background-color: {p.surface_alt}; border: 1px solid {p.border}; gridline-color: {p.border_subtle}; selection-background-color: {p.accent_soft}; selection-color: {p.text}; }}
        QHeaderView::section {{ background: {p.surface_alt}; color: {p.label}; border: 0; border-bottom: 1px solid {p.border}; padding: 7px 8px; }}
        QHeaderView::section:hover:enabled {{ background: {p.interaction_hover}; color: {p.text}; }}
        QTableWidget#NativeInstrumentTable {{
            border-radius: 8px; outline: 0;
            font-size: {INSTRUMENT_TABLE.font_px}px;
        }}
        QProgressBar#SoundFontLoadProgress, QProgressBar#TaskProgress {{
            min-height: 10px; max-height: 10px;
            border: 1px solid {p.border}; border-radius: 5px;
            background: {p.input};
        }}
        QProgressBar#SoundFontLoadProgress::chunk, QProgressBar#TaskProgress::chunk {{
            background: {p.accent}; border-radius: 4px;
        }}
        QSplitter::handle {{ background: transparent; }}
        QSplitter::handle:hover:enabled {{ background: {p.border_subtle}; }}
        QScrollBar:vertical {{ width: 10px; margin: 4px 2px; background: transparent; }}
        QScrollBar:horizontal {{ height: 10px; margin: 2px 4px; background: transparent; }}
        QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
            background: {p.border}; border-radius: 4px; min-height: 28px; min-width: 28px;
        }}
        QScrollBar::handle:vertical:hover:enabled, QScrollBar::handle:horizontal:hover:enabled {{ background: {p.muted}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; background: none; }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    """


class NativeWindowShellGuard(QObject):
    """Taskbar membership follows HWND lifetime,not individual dialog code."""
    def __init__(self, window):
        super().__init__(window)
        window.installEventFilter(self)

    def eventFilter(self, window, event):
        if event.type() in (QEvent.Show,QEvent.WinIdChange):
            hwnd=int(window.effectiveWinId() or 0)
            if hwnd:
                from windows_integration import expose_taskbar_window
                expose_taskbar_window(hwnd)
        return False


def install_native_window_shell(window):
    if (QApplication.platformName()=='windows' and window.isWindow()
            and window.windowType() in (Qt.Window,Qt.Dialog)
            and not hasattr(window,'_native_shell_guard')):
        from windows_integration import configure_taskbar_identity
        configure_taskbar_identity()
        window._native_shell_guard=NativeWindowShellGuard(window)
        window._native_resize_background_guard=NativeResizeBackgroundGuard(window)
        hwnd=int(window.effectiveWinId() or 0)
        if hwnd:
            from windows_integration import expose_taskbar_window
            expose_taskbar_window(hwnd)


class NativeModalCloseRouter(QAbstractNativeEventFilter):
    """Keep a blocked native close bound to its original target window.

    Qt6.11.2 processCloseEvent drops native close before QWidget.closeEvent
    when a modal blocks the target. Ordinary closeEvent owns all transaction
    guards. Never reinterpret an owner's Close as its child's Close.
    """
    def __init__(self):
        super().__init__()
        self._pending = weakref.WeakSet()

    @staticmethod
    def _blocking_dialog(target):
        modal = QApplication.activeModalWidget()
        if modal is None or modal is target or not modal.isVisible():
            return None
        ancestor = modal.parentWidget()
        while ancestor is not None:
            if ancestor is target:
                return modal
            ancestor = ancestor.parentWidget()
        return None

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) != b'windows_generic_MSG':
            return False, 0
        native = wintypes.MSG.from_address(int(message))
        # WinUser.h WM_CLOSE,WM_SYSCOMMAND/SC_CLOSE. The latter is consumed
        # here too,so DefWindowProc cannot turn it into a duplicate WM_CLOSE.
        if not (native.message == 0x0010 or
                native.message == 0x0112 and int(native.wParam) & 0xfff0 == 0xf060):
            return False, 0
        target = QWidget.find(int(native.hWnd or 0))
        if target is None or not target.isWindow():
            return False, 0
        modal = self._blocking_dialog(target)
        if modal is None:
            return False, 0
        if target not in self._pending:
            self._pending.add(target)
            reference = weakref.ref(target)
            blocker = weakref.ref(modal)
            # Leave WndProc before close/reject can open a confirmation loop.
            # Context cancellation and weak capture protect destroyed dialogs.
            QTimer.singleShot(0, target, lambda: self._deliver(reference,blocker))
        return True, 0

    def _deliver(self, reference, blocker):
        dialog = reference()
        modal = blocker()
        try:
            if (dialog is None or not shiboken6.isValid(dialog) or
                    not dialog.isVisible() or modal is None or not shiboken6.isValid(modal) or
                    QApplication.activeModalWidget() is not modal):
                return
            dialog.close()
            if shiboken6.isValid(dialog) and dialog.isVisible():
                # A refused close must keep its reason/action reachable.
                front = QApplication.activeModalWidget() or dialog
                if front.isMinimized():
                    front.showNormal()
                front.raise_()
                front.activateWindow()
        finally:
            if dialog is not None:
                self._pending.discard(dialog)


class NativeDialog(QDialog):
    """Product dialogs share GUI-owned native visibility; modal results stay Qt."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        install_native_window_shell(self)
    def show(self):invoke_native_visibility(self,'show')
    def showNormal(self):invoke_native_visibility(self,'showNormal')
    def showMinimized(self):invoke_native_visibility(self,'showMinimized')
    def hide(self):invoke_native_visibility(self,'hide')


def apply_native_stylesheet(widget, stylesheet):
    """Same synchronous GUI slot, no worker widget/queued half-theme state.

    Qt QMetaObject::invokeMethod DirectConnection executes on the calling
    thread. The explicit ownership guard is required even though invokeMethod
    itself is thread-safe. A native-slot route reduces interpreter contention
    in the qualified scene; it does not make stylesheet polishing cheaper.
    """
    if QThread.currentThread()!=widget.thread():
        raise RuntimeError('native stylesheet must run on its Qt owner thread')
    if widget.styleSheet()==stylesheet:return
    app=QApplication.instance()
    router=getattr(widget,'_interaction_filter',None)
    # Only our owner's global router is unnecessary during synchronous native
    # polish. Active entry filters/popups retain their installed precedence.
    # Never pump events or touch widgets from a worker inside this scope.
    exclude=(router is not None and app.activePopupWidget() is None
             and not any(getattr(item,'_app_filter_installed',False)
                         for item in widget.findChildren(EditableNumberLabel)))
    if exclude:app.removeEventFilter(router)
    try:
        if not QMetaObject.invokeMethod(widget,'setStyleSheet',Qt.DirectConnection,Q_ARG(str,stylesheet)):
            widget.setStyleSheet(stylesheet)
    finally:
        if exclude:
            app.installEventFilter(router)
            router.hover.schedule()
            # A synchronous style callback may create an owned top-level. The
            # normal Show event router resumes for all subsequent windows.
            for child in widget.findChildren(QWidget):
                if child.isWindow() and getattr(child,'_native_dpi_size_guard',None) is None:
                    install_native_dpi_guard(child)
                    guard=getattr(child,'_native_dpi_size_guard',None)
                    if guard is not None:guard._bind_native_sizing(child)


def flat_tab_pane_stylesheet():
    """Shared borderless tab-pane treatment for embedded editor tabs."""
    # Borderless does not mean that the first control touches the active-tab
    # underline. NativeContentTabs includes this gap in height-for-width.
    return ('QTabWidget::pane { border: 0; background: transparent; '
            f'padding: {NativeContentTabs.PANE_TOP_GAP}px 0px 0px 0px; }}')


def transparent_scroll_area_stylesheet():
    """Shared transparent scroll surface for themed embedded pages."""
    return ('QScrollArea { background: transparent; border: 0; }'
            'QWidget#qt_scrollarea_viewport { background: transparent; }')


def set_transparent_scroll_content(scroll, content):
    """Retain the host card's surface after QScrollArea enables auto-fill."""
    scroll.setWidget(content)
    content.setAutoFillBackground(False)


def _install_numeric_accessibility():
    global _numeric_accessible_factory_installed
    if not _numeric_accessible_factory_installed:
        QAccessible.installFactory(_numeric_accessible_factory)
        _numeric_accessible_factory_installed = True


def _configure_numeric_accessibility(widget, domain, formatter, *, read=None, write=None, step=None):
    widget._accessible_entry_domain = domain
    widget._accessible_formatter = formatter
    widget._accessible_read = read
    widget._accessible_write = write
    widget._accessible_step = step


def _notify_numeric_accessibility(widget):
    if not QAccessible.isActive(): return
    if getattr(widget,'_accessible_notification_pending',False):return
    widget._accessible_notification_pending=True
    reference=weakref.ref(widget)
    # A UIA SetValue request is an inbound Windows COM call. Raise the latest
    # custom notification on the next GUI turn, never call back out synchronously.
    QTimer.singleShot(0,widget,lambda:_deliver_numeric_accessibility(reference))


def _deliver_numeric_accessibility(reference):
    widget=reference()
    if widget is None or not shiboken6.isValid(widget):return
    widget._accessible_notification_pending=False
    if not QAccessible.isActive():return
    adapter = QAccessible.queryAccessibleInterface(widget)
    if adapter is not None:
        values = adapter.valueInterface()
        # Qt's Windows bridge forwards string ValueChanged to ValuePattern;
        # DescriptionChanged is not forwarded. Mixed remains text, NOT scalar0.
        QAccessible.updateAccessibility(QAccessibleValueChangeEvent(widget,adapter.text(QAccessible.Value)))
        if values is not None:
            QAccessible.updateAccessibility(QAccessibleValueChangeEvent(widget, values.currentValue()))


class NativeAccessibleValue(QAccessibleValueInterface):
    """Display-domain adapter; raw MIDI/slider precision remains unchanged."""
    def __init__(self, widget):
        super().__init__()
        self._widget = weakref.ref(widget)

    def _control(self):
        widget = self._widget()
        return widget if widget is not None and shiboken6.isValid(widget) else None

    def _domain(self, widget):
        return getattr(widget, '_accessible_entry_domain', None) or numeric_entry_domain(
            'number_integer', widget.minimum(), widget.maximum())

    def currentValue(self):
        widget = self._control()
        if widget is None or widget.property('mixedValue') or widget.property('unknownValue'): return None
        read = getattr(widget, '_accessible_read', None)
        if read is not None: return read()
        return self._domain(widget).to_entry(widget.value())

    def minimumValue(self):
        widget = self._control()
        return self._domain(widget).minimum if widget is not None else None

    def maximumValue(self):
        widget = self._control()
        return self._domain(widget).maximum if widget is not None else None

    def minimumStepSize(self):
        widget = self._control()
        if widget is None: return None
        step = getattr(widget, '_accessible_step', None)
        if step is not None: return step()
        domain = self._domain(widget)
        # Native fine/RPN resolution need not equal rounded readout precision.
        first = widget.minimum()
        fine_step = getattr(widget, '_input_quantum', widget.singleStep())
        return abs(domain.to_entry(min(widget.maximum(), first + fine_step)) - domain.to_entry(first))

    def setCurrentValue(self, value):
        widget = self._control()
        if widget is None or not widget.isEnabled(): return
        try:
            raw = self._domain(widget).to_raw(float(value))
        except (ValueError, TypeError, OverflowError):
            return  # Qt ValueInterface: out-of-domain requests are ignored.
        write = getattr(widget, '_accessible_write', None)
        if write is not None:
            write(float(value))
            return
        if not math.isclose(raw, round(raw), abs_tol=1e-7): return
        amount = int(round(raw))
        mixed = bool(widget.property('mixedValue') or widget.property('unknownValue'))
        previous = widget.value()
        widget.setValue(amount)
        if mixed and previous == widget.value():
            widget.valueChanged.emit(widget.value())


class NativeAccessibleNumeric(QAccessibleWidget):
    def __init__(self, widget):
        # Qt's Windows UIA bridge maps Dial to Custom, whereas Slider supplies
        # the standard bounded numeric control type. Painting remains a dial.
        super().__init__(widget, QAccessible.Slider)
        self._values = NativeAccessibleValue(widget)

    def interface_cast(self, kind):
        if kind == QAccessible.ValueInterface:
            if self.object().property('mixedValue') or self.object().property('unknownValue'):
                return shiboken6.VoidPtr(0)  # Otherwise invalid QVariant becomes0.
            # Qt6.11/PySide void* virtual return MUST be VoidPtr. Returning
            # an int or multiple concrete C++ bases causes an access violation.
            return shiboken6.VoidPtr(shiboken6.getCppPointer(self._values)[0])
        result=super().interface_cast(kind)
        # Foreign UIA clients probe unsupported interfaces too. PySide's
        # void* virtual trampoline dereferences None as a VoidPtr wrapper
        # (Shiboken::VoidPtr::getSize); represent the native null explicitly.
        return result if result is not None else shiboken6.VoidPtr(0)

    def text(self, kind):
        widget = self.object()
        if widget is None: return ''
        mixed = bool(widget.property('mixedValue'))
        if kind == QAccessible.Value:
            if widget.property('unknownValue'):return tr('channel_editor.unknown_value')
            if mixed: return tr('channel_editor.mixed_value')
            formatter = getattr(widget, '_accessible_formatter', None)
            if formatter is not None: return formatter(widget.value())
            domain = self._values._domain(widget)
            return domain.entry_text(domain.to_entry(widget.value()))
        if kind in (QAccessible.Description,QAccessible.Help):
            # Windows HelpText must use semantic copy, not wrapped tooltip HTML.
            base = widget.accessibleDescription()
            if widget.property('unknownValue'):return tr('channel_editor.unknown_hint')
            if mixed:
                return tr('channel_editor.tooltip_with_cc_status',description=base,
                    status=tr('channel_editor.mixed_hint')) if base else tr('channel_editor.mixed_hint')
            return base
        return super().text(kind)

    def actionNames(self):
        widget = self.object()
        if widget is None or not widget.isEnabled(): return []
        actions=[QAccessibleActionInterface.setFocusAction()]
        if widget.value()<widget.maximum() or widget.property('mixedValue'):
            actions.append(QAccessibleActionInterface.increaseAction())
        if widget.value()>widget.minimum() or widget.property('mixedValue'):
            actions.append(QAccessibleActionInterface.decreaseAction())
        return actions

    def doAction(self, action):
        widget = self.object()
        if widget is None or not widget.isEnabled(): return
        navigate = getattr(widget, 'navigation_callback', None)
        if action == QAccessibleActionInterface.increaseAction():
            if navigate is not None: navigate(1, 'key')
            else: widget.triggerAction(QAbstractSlider.SliderSingleStepAdd)
        elif action == QAccessibleActionInterface.decreaseAction():
            if navigate is not None: navigate(-1, 'key')
            else: widget.triggerAction(QAbstractSlider.SliderSingleStepSub)
        else: super().doAction(action)


class NativeAccessibleEQBand(QAccessibleWidget):
    """An immediate chart-point selection, not an animated command button."""
    def __init__(self,widget):super().__init__(widget,QAccessible.Button)

    def actionNames(self):
        return [QAccessibleActionInterface.pressAction(),QAccessibleActionInterface.setFocusAction()] if self.object().isEnabled() else []

    def doAction(self,action):
        widget=self.object()
        if widget is None or not widget.isEnabled():return
        if action==QAccessibleActionInterface.pressAction():widget.click()
        else:super().doAction(action)

    def text(self,kind):
        if kind in (QAccessible.Description,QAccessible.Help):return self.object().accessibleDescription()
        return super().text(kind)


class NativeAccessibleEQChart(QAccessibleWidget):
    """Multi-axis chart status/actions; scalar rows own bounded ValueInterface."""
    def __init__(self, widget):
        super().__init__(widget, QAccessible.Chart)

    def text(self, kind):
        if kind in (QAccessible.Description, QAccessible.Help, QAccessible.Value):
            widget = self.object()
            return widget.accessibleDescription() if widget is not None else ''
        return super().text(kind)

    def actionNames(self):
        widget = self.object()
        if widget is None or not widget.isEnabled(): return []
        actions = [QAccessibleActionInterface.setFocusAction()]
        if widget._bands:
            if widget._selected < len(widget._bands)-1: actions.append(QAccessibleActionInterface.nextPageAction())
            if widget._selected > 0: actions.append(QAccessibleActionInterface.previousPageAction())
            if 0 <= widget._selected < len(widget._bands) and widget._bands[widget._selected].kind not in ('high_pass', 'low_pass'):
                if widget._bands[widget._selected].gain_db < 15: actions.append(QAccessibleActionInterface.increaseAction())
                if widget._bands[widget._selected].gain_db > -15: actions.append(QAccessibleActionInterface.decreaseAction())
        return actions

    def doAction(self, action):
        widget = self.object()
        if widget is None or action not in self.actionNames(): return
        dispatch = {QAccessibleActionInterface.nextPageAction(): Qt.Key_PageDown,
            QAccessibleActionInterface.previousPageAction(): Qt.Key_PageUp,
            QAccessibleActionInterface.increaseAction(): Qt.Key_Up,
            QAccessibleActionInterface.decreaseAction(): Qt.Key_Down}
        if action in dispatch: widget._navigate_band(dispatch[action])
        else: super().doAction(action)

    def keyBindingsForAction(self, action):
        return {QAccessibleActionInterface.nextPageAction(): ['PgDown'],
            QAccessibleActionInterface.previousPageAction(): ['PgUp'],
            QAccessibleActionInterface.increaseAction(): ['Up'],
            QAccessibleActionInterface.decreaseAction(): ['Down']}.get(action, [])


def _numeric_accessible_factory(_name, widget):
    if not isinstance(widget, (NativeKnob, NativeSlider, NativeEQResponse,NativeEQBandHandle)): return None
    # Qt caches by QObject AND construction-stage metaObject. It may request
    # another factory interface for the same QWidget at a later stage. Returning
    # one pointer twice gives two cache IDs ownership of the same C++ object,
    # then Qt deletes it twice. Each factory invocation must create a distinct
    # interface; Qt's own query cache handles normal interface reuse.
    adapter = (NativeAccessibleEQChart(widget) if isinstance(widget,NativeEQResponse) else
               NativeAccessibleEQBand(widget) if isinstance(widget,NativeEQBandHandle) else NativeAccessibleNumeric(widget))
    _numeric_accessible_adapters.setdefault(widget,[]).append(adapter)
    return adapter


class NativePaintReuseMixin:
    """Retain unchanged contents;local geometry changes still repaint fully.

    Qt's backing store owns reuse,exposure and child composition. This does
    not cache a screenshot or suspend value/hover/theme updates. A centred
    control cannot use WA_StaticContents alone:its own resize must invalidate
    all its pixels,not only the newly exposed edge.
    """
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.setAttribute(Qt.WA_StaticContents,True)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self.update()


class NativeKnob(QDial):
    """Theme-painted dial with shared, precision-aware navigation and Qt actions."""

    _paint_cache = OrderedDict()
    _paint_cache_limit = 384
    ARC_START_DEGREES = 225
    ARC_SWEEP_DEGREES = 270

    def __init__(self, minimum=0, maximum=127, neutral=64, parent=None,
                 *, compact=False, input_quantum=1):
        _install_numeric_accessibility()
        super().__init__(parent)
        self._neutral = int(neutral)
        self._dark = True
        self._compact = bool(compact)
        if type(input_quantum) is not int or input_quantum < 1:
            raise ValueError('knob input quantum')
        self._input_quantum = input_quantum
        self.setRange(int(minimum), int(maximum))
        self.setWrapping(False)
        self.setNotchesVisible(False)
        self.rangeChanged.connect(self._refresh_input_steps)
        self._refresh_input_steps()
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setFixedHeight(40 if self._compact else 44)
        self.setValue(self._neutral)
        self._angular_gesture = False
        self._pointer_choice = None
        self._reset_release = False
        self.actionTriggered.connect(self._commit_mixed_action)
        self.valueChanged.connect(self._notify_accessible_value)

    def set_accessible_domain(self, domain, formatter=None, *, read=None, write=None):
        _configure_numeric_accessibility(self, domain, formatter,read=read,write=write)

    def _notify_accessible_value(self, *_args):
        _notify_numeric_accessibility(self)

    def _refresh_input_steps(self, *_args):
        key, self._wheel_ticks, page = knob_input_steps(
            self.maximum() - self.minimum(), self._input_quantum)
        self.setSingleStep(key)
        self.setPageStep(page)
        state = getattr(self, '_native_wheel_input', None)
        if state is not None:
            state.context = None; state.remainder = 0

    def _trigger_step(self, ticks):
        # Use Qt's action path,including mixed-at-bound commits. No direct
        # callback or separate label write may bypass the composed field.
        if not ticks or not self.isEnabled():
            return
        previous = self.singleStep()
        try:
            self.setSingleStep(abs(ticks))
            self.triggerAction(QAbstractSlider.SliderSingleStepAdd if ticks > 0
                               else QAbstractSlider.SliderSingleStepSub)
        finally:
            self.setSingleStep(previous)

    def keyPressEvent(self, event):
        directions = {Qt.Key_Left: -1, Qt.Key_Down: -1,
                      Qt.Key_Right: 1, Qt.Key_Up: 1,
                      Qt.Key_PageDown: -1, Qt.Key_PageUp: 1}
        if event.key() in directions:
            step = (self._input_quantum if event.modifiers() & Qt.ShiftModifier else
                    self.pageStep() if event.key() in (Qt.Key_PageDown, Qt.Key_PageUp)
                    else self.singleStep())
            self._trigger_step(directions[event.key()] * step *
                               (-1 if self.invertedControls() else 1))
            event.accept()
            return
        super().keyPressEvent(event)

    def wheelEvent(self,event):
        decoded = native_wheel_steps(self, event)
        if decoded is None:
            return
        axis, detents = decoded
        if axis == 'x':
            detents = -detents
        step = self._input_quantum if event.modifiers() & Qt.ShiftModifier else self._wheel_ticks
        self._trigger_step(detents * step * (-1 if self.invertedControls() else 1))
        event.accept()

    def _commit_mixed_action(self, action):
        # Home/End or a wheel/key step at a bound is still an explicit edit,
        # even if Qt's hidden dial value already equals that destination.
        if action and (self.property("mixedValue") or self.property('unknownValue')) and self.sliderPosition() == self.value():
            self.valueChanged.emit(self.value())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if not self.isEnabled():
                event.ignore(); return
            self._reset_release = False
            self._angular_gesture = True
            self._pointer_choice = None
            self.setSliderDown(True)
            self.setFocus(Qt.MouseFocusReason)
            self._set_pointer_value(event.position())
            event.accept()
        else:
            super().mousePressEvent(event)

    def _set_pointer_value(self, position):
        # The pointer and painter share the270-degree arc. QDial's private
        #300-degree mapping would visibly disagree at both arc ends.
        dx, dy = position.x() - self.width()/2, self.height()/2 - position.y()
        if math.hypot(dx, dy) <= 2:
            return  # The centre has no angle; do not invent a mixed value.
        angle = math.degrees(math.atan2(dy, dx))
        offset = (self.ARC_START_DEGREES - angle) % 360
        if math.isclose(offset, 360, abs_tol=1e-9):
            offset = 0
        if offset > self.ARC_SWEEP_DEGREES:
            offset = (self.ARC_SWEEP_DEGREES if
                      offset < (360+self.ARC_SWEEP_DEGREES)/2 else 0)
        target = self.minimum() + round((self.maximum()-self.minimum()) * offset/self.ARC_SWEEP_DEGREES)
        unchanged = self.value() == target
        repeated_choice = self._pointer_choice == target
        self._pointer_choice = target
        self.setValue(target)
        if unchanged and not repeated_choice and (self.property('mixedValue') or self.property('unknownValue')):
            self.valueChanged.emit(self.value())

    def mouseMoveEvent(self, event):
        if self._angular_gesture and self.isEnabled() and event.buttons() & Qt.LeftButton:
            self._set_pointer_value(event.position())
            event.accept()
        elif event.buttons() & Qt.LeftButton:
            # A retired gesture must not fall through to QDial's unguarded
            # angular handler after hide/deactivation/disable/reparent.
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def cancel_pointer_gesture(self):
        self._angular_gesture=False;self._pointer_choice=None;self._reset_release=False
        blocked=self.blockSignals(True)
        try:self.setSliderDown(False)
        finally:self.blockSignals(blocked)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._angular_gesture and self.isEnabled() and not self._reset_release:
                self._set_pointer_value(event.position())
            self._angular_gesture = False
            self._pointer_choice = None
            self.setSliderDown(False)
            self._reset_release = False
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def sizeHint(self):
        return QSize(40, 40) if self._compact else QSize(48, 44)

    def minimumSizeHint(self):
        return QSize(0, 0)

    def set_theme(self, dark: bool):
        self._dark = bool(dark)
        self.update()

    def set_neutral(self, value):
        if not self.minimum()<=int(value)<=self.maximum():
            raise ValueError("Knob neutral outside range")
        self._neutral=int(value)  # Reset target only; never emits or moves the dial.

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._reset_release = True
            self._angular_gesture = False
            self._pointer_choice = None
            self.setSliderDown(False)
            unchanged=self.value()==self._neutral
            self.setValue(self._neutral)
            if self.property("mixedValue") or self.property('unknownValue') or (unchanged and self.property('preserveValuePrecision')):
                self.valueChanged.emit(self.value())
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def paintEvent(self, event):
        if self.width() <= 0 or self.height() <= 0:
            return
        ratio = self.devicePixelRatioF()
        palette = widget_palette(self, fallback=color_palette(self._dark))
        key = (self.width(), self.height(), round(ratio, 3), self.value(),
               self.minimum(), self.maximum(), palette, self.isEnabled(), self._compact,
               bool(self.property("ccNoEffect")), bool(self.property("mixedValue")),bool(self.property('unknownValue')))
        pixmap = self._paint_cache.get(key)
        if pixmap is None:
            pixmap = QPixmap(math.ceil(self.width() * ratio), math.ceil(self.height() * ratio))
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            self._paint_dial(painter)
            painter.end()
            self._paint_cache[key] = pixmap
            if len(self._paint_cache) > self._paint_cache_limit:
                self._paint_cache.popitem(last=False)
        else:
            self._paint_cache.move_to_end(key)
        painter = QPainter(self)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()

    def _paint_dial(self, painter):
        palette = widget_palette(self, fallback=color_palette(self._dark))
        painter.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height()) - (2 if self._compact else 10)
        if side <= 0:
            return
        circle = QRectF((self.width() - side) / 2, (self.height() - side) / 2,
                        side, side)
        arc = circle.adjusted(2, 2, -2, -2)
        start = self.ARC_START_DEGREES * 16
        span = -self.ARC_SWEEP_DEGREES * 16
        fraction = ((self.value() - self.minimum()) /
                    max(1, self.maximum() - self.minimum()))
        no_effect = bool(self.property("ccNoEffect"))
        rim = QColor(palette.danger if no_effect else palette.border)
        if no_effect:
            rim.setAlpha(115)
        painter.setPen(QPen(rim, 2.5, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(arc, start, span)
        painter.setPen(QPen(QColor(palette.danger if no_effect else
                                  palette.accent if self.isEnabled() else palette.muted),
                            3, Qt.SolidLine, Qt.RoundCap))
        if not self.property("mixedValue") and not self.property('unknownValue'):
            painter.drawArc(arc, start, round(span * fraction))
        painter.setPen(QPen(QColor(palette.border), 1))
        painter.setBrush(QColor(palette.input))
        face_inset = 5 if self._compact else 6
        painter.drawEllipse(circle.adjusted(face_inset, face_inset,
                                            -face_inset, -face_inset))
        angle = math.radians(self.ARC_START_DEGREES - self.ARC_SWEEP_DEGREES * fraction)
        cx, cy = circle.center().x(), circle.center().y()
        radius = side * 0.23
        painter.setPen(QPen(QColor(palette.danger if no_effect else
                                  palette.text if self.isEnabled() else palette.muted),
                            2, Qt.SolidLine, Qt.RoundCap))
        if not self.property("mixedValue") and not self.property('unknownValue'):
            painter.drawLine(QPointF(cx, cy),
                             QPointF(cx + math.cos(angle) * radius,
                                     cy - math.sin(angle) * radius))
        else:
            painter.drawLine(QPointF(cx - radius * .7, cy),
                             QPointF(cx + radius * .7, cy))


class NativeKnobField(QWidget):
    """A labeled, accessible dial with the same get/set contract as a row."""

    DIAL_READOUT_HEIGHT = 53

    def __init__(self, label, minimum, maximum, initial, callback, formatter,
                 *, tooltip=None, parent=None, preferred_width=0,
                 fit_value_spacing=False, editable=False, edit_offset=0,
                 edit_context=None, entry_domain=None,value_scale=1,value_step=None,preserve_value_precision=False):
        super().__init__(parent)
        # QDial is integer-only. Opt-in fixed-point storage keeps fractional
        # sound controls on the same editor,gesture and accessibility path.
        if type(value_scale) is not int or value_scale<1:raise ValueError('knob value scale')
        self._value_scale=value_scale
        self._preserve_value_precision=preserve_value_precision
        self._reference_value=None;self._exact_neutral=initial
        self._observed_value=None
        self.callback, self.formatter = callback, formatter
        self._preferred_width = max(0, int(preferred_width))
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        policy = self.sizePolicy(); policy.setHeightForWidth(True); self.setSizePolicy(policy)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.title = EditableNumberLabel(label)
        self.title.setObjectName("KnobFieldLabel")
        self.title.setProperty('nativeKnobPart','caption')
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setWordWrap(True)
        self.title.setMinimumWidth(0)
        layout.addWidget(self.title)
        input_quantum = 1
        if value_step is not None:
            if not math.isfinite(value_step) or value_step<=0:raise ValueError('knob value step')
            input_quantum = max(1, round(value_step*value_scale))
        self.knob = NativeKnob(round(minimum*value_scale),round(maximum*value_scale),round(initial*value_scale),self,
                               compact=True,input_quantum=input_quantum)
        self.knob.setProperty('preserveValuePrecision',preserve_value_precision)
        self.knob.setAccessibleName(label)
        self.value = (EditableNumberLabel(preferred_width=0)
                      if editable else AdaptiveValueLabel(preferred_width=0)
                      if fit_value_spacing else QLabel())
        self.value.setObjectName("KnobFieldValue")
        self.value.setProperty('nativeKnobPart','readout')
        self.value.setAlignment(Qt.AlignCenter)
        self.value.setMinimumWidth(0)
        # The 40px dial and full-height readout share a 53px paint slot.
        # Their widget rectangles overlap, but the text ink stays below the
        # painted dial arc; this removes blank pixels without clipping glyphs.
        self.dial_readout = QWidget(self)
        self.dial_readout.setFixedHeight(self.DIAL_READOUT_HEIGHT)
        readout_layout = QGridLayout(self.dial_readout)
        readout_layout.setContentsMargins(0, 0, 0, 0)
        readout_layout.setSpacing(0)
        readout_layout.addWidget(self.knob, 0, 0, Qt.AlignHCenter | Qt.AlignTop)
        readout_layout.addWidget(self.value, 0, 0, Qt.AlignBottom)
        layout.addWidget(self.dial_readout)
        layout.addStretch(1)
        install_effect_tooltip(tooltip, self, self.title, self.knob, self.value)
        if editable:
            domain = entry_domain or replace(numeric_entry_domain('number_integer',minimum,maximum),offset=edit_offset)
            self._entry_domain = domain
            self.value.configure(
                minimum=domain.minimum, maximum=domain.maximum,
                decimals=domain.decimals, format_input=domain.entry_text,
                read=lambda: domain.to_entry(self.get()),
                write=lambda number: self.set(domain.to_raw(number)),
                context=edit_context, label=label,
            )
            self.value.bind_control(self.knob)
        self.knob.valueChanged.connect(self._changed)
        accessible_domain=entry_domain or getattr(self,'_entry_domain',None)
        physical_domain=accessible_domain
        if value_scale!=1:
            accessible_domain=accessible_domain or numeric_entry_domain('number_thousandth',minimum,maximum)
            accessible_domain=replace(accessible_domain,raw_minimum=accessible_domain.raw_minimum*value_scale,
                raw_maximum=accessible_domain.raw_maximum*value_scale,scale=accessible_domain.scale*value_scale,
                offset=accessible_domain.offset*value_scale)
        self.knob.set_accessible_domain(accessible_domain,lambda _raw:formatter(self.get()),
            read=(lambda:physical_domain.to_entry(self.get())) if physical_domain else None,
            write=(lambda number:self.set(physical_domain.to_raw(number))) if physical_domain else None)
        self.set(initial, False)
        self.value.raise_()

    def add_cc_identity(self):
        """An optional protocol legend, beside—not inside—the dial hit area."""
        if hasattr(self, "cc_badge"):
            return self.cc_badge
        grid = self.dial_readout.layout()
        grid.removeWidget(self.knob)
        self.cc_cluster = QWidget(self.dial_readout)
        self.cc_cluster.setFixedHeight(self.knob.sizeHint().height())
        self.cc_cluster.setMinimumWidth(0)
        width, gap = METRICS["channel_protocol_width"], METRICS["channel_protocol_gap"]
        self.cc_cluster.setMaximumWidth(self.knob.sizeHint().width() + 2 * (width + gap))
        self.cc_cluster.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        cluster = QHBoxLayout(self.cc_cluster)
        cluster.setContentsMargins(0, 0, 0, 0)
        cluster.setSpacing(gap)
        self.cc_badge = NativeRotatedLabel("", text_px=METRICS["channel_protocol_font_px"],
                                           preferred_width=width, wrap=True)
        self.cc_badge.setMaximumWidth(width)
        self.cc_badge.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        cluster.addWidget(self.cc_badge)
        cluster.addWidget(self.knob, 1, Qt.AlignHCenter | Qt.AlignTop)
        balance = NativeRotatedLabel("", preferred_width=width,
                                    text_px=METRICS["channel_protocol_font_px"], clockwise=True)
        balance.setMaximumWidth(width)
        balance.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        cluster.addWidget(balance)
        self.cc_status = balance
        self.cc_control = self.knob
        grid.addWidget(self.cc_cluster, 0, 0, Qt.AlignHCenter | Qt.AlignTop)
        # The protocol cluster spans the full readout height for long names;
        # the numeric editor must still own the bottom row's mouse hits.
        self.value.raise_()
        return self.cc_badge

    def set_detail_caption(self,text,*,tooltip=''):
        """Optional centered annotation below the value,with shared sizing."""
        if not hasattr(self,'detail_caption'):
            self.detail_caption=ElasticLabel('',preferred_width=0)
            self.detail_caption.setObjectName('KnobFieldDetail')
            self.detail_caption.setProperty('nativeKnobPart','detail')
            self.detail_caption.setAlignment(Qt.AlignCenter)
            self.detail_caption.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
            self.layout().insertWidget(self.layout().count()-1,self.detail_caption)
        self.detail_caption.setText(text)
        self.detail_caption.setAccessibleName(text)
        install_effect_tooltip(tooltip,self.detail_caption)
        self.updateGeometry()

    def add_caption_action(self,button):
        """Contextual caption command;the dial/readout keep their full width."""
        layout=self.layout();layout.removeWidget(self.title)
        row=QWidget(self);row.setMinimumWidth(0)
        header=set_layout(QHBoxLayout(row),0,0)
        # Equal side cells keep the caption mathematically centered. The
        # action cannot consume its label rectangle or the dial's hit area.
        button.set_compact(True)
        side=METRICS['compact_action_height']+8
        button.setMaximumWidth(side)
        balance=ElasticPanel(preferred_width=side);balance.setMaximumWidth(side)
        action=ElasticPanel(preferred_width=side);action.setMaximumWidth(side)
        # ElasticPanel's preferred height follows its current geometry. These
        # caption gutters must not feed an unlaid-out600px height into HFW.
        balance.setFixedHeight(button.height());action.setFixedHeight(button.height())
        cell=set_layout(QHBoxLayout(action),0,0);cell.addWidget(button)
        header.addWidget(balance);header.addWidget(self.title,1);header.addWidget(action)
        layout.insertWidget(0,row);self.side_action=button;self._caption_row=row
        return button

    def sizeHint(self):
        hint = super().sizeHint()
        width = max(hint.width(), self._preferred_width)
        return QSize(width, self.heightForWidth(self.width() or width))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        title_height = (self.title.heightForWidth(max(1, width)) if self.title.hasHeightForWidth()
                        else QFontMetrics(self.title.font()).height())
        title_height=max(self.title.minimumHeight(),min(self.title.maximumHeight(),title_height))
        if hasattr(self,'_caption_row'):
            title_height=max(title_height,self.side_action.height())
        detail_height=(self.detail_caption.sizeHint().height() if hasattr(self,'detail_caption') else 0)
        return max(0, title_height) + self.DIAL_READOUT_HEIGHT + detail_height

    def minimumSizeHint(self):
        return QSize(0, 0)

    def get(self):
        if self._observed_value is not None:return self._observed_value
        if self._preserve_value_precision and self._reference_value is not None:return self._reference_value
        return self.knob.value()/self._value_scale if self._value_scale!=1 else self.knob.value()

    def set_mixed(self, mixed):
        changed = bool(self.knob.property("mixedValue")) != bool(mixed)
        if changed:
            self.knob.setProperty("mixedValue", bool(mixed))
            self.knob.update()
        if bool(self.value.property("mixedValue")) != bool(mixed):
            self.value.setProperty("mixedValue", bool(mixed))
        if not mixed and self.value.accessibleDescription() == tr("channel_editor.mixed_hint"):
            self.value.setAccessibleDescription("")
        text = tr('channel_editor.unknown_value') if self.knob.property('unknownValue') else tr("channel_editor.mixed_value") if mixed else self.formatter(self.get())
        if self.value.text() != text:
            self.value.setText(text)
        if changed:
            self.knob._notify_accessible_value()

    def set_unknown(self, unknown):
        changed=bool(self.knob.property('unknownValue'))!=bool(unknown)
        if changed:self.knob.setProperty('unknownValue',bool(unknown))
        if bool(self.value.property('unknownValue'))!=bool(unknown):
            self.value.setProperty('unknownValue',bool(unknown))
        if unknown:self.set_mixed(False)
        text=tr('channel_editor.unknown_value') if unknown else tr('channel_editor.mixed_value') if self.knob.property('mixedValue') else self.formatter(self.get())
        if self.value.text()!=text:self.value.setText(text)
        if changed:
            self.knob.update();self.knob._notify_accessible_value()

    def set(self, value, call=True):
        self._observed_value=None
        self.set_unknown(False)
        self._reference_value=(max(self.knob.minimum()/self._value_scale,min(self.knob.maximum()/self._value_scale,float(value)))
                               if self._preserve_value_precision else None)
        blocked = self.knob.blockSignals(True)
        self.knob.setValue(int(round(value*self._value_scale)))
        self.knob.blockSignals(blocked)
        self.set_mixed(False)
        self.value.setText(self.formatter(self.get()))
        if call and self.callback:
            self.callback(self.get())

        self.knob._notify_accessible_value()

    def set_observed(self,value):
        """Present qualified native state independently of entry bounds.

        QDial's position still saturates at its normal endpoint. The composed
        readout owns the true external value;no callback or saved-state write.
        The next explicit edit clears this observation and uses normal bounds.
        """
        if not math.isfinite(value):raise ValueError('nonfinite knob observation')
        self.set(value,False)
        self._observed_value=value
        self.set_mixed(False)
        self.value.setText(self.formatter(self.get()))
        self.knob._notify_accessible_value()

    def set_theme(self, dark):
        self.knob.set_theme(dark)
        if hasattr(self, "cc_badge"):
            self.cc_badge.set_theme(dark)
            self.cc_status.set_theme(dark)

    def _changed(self, value):
        self._observed_value=None
        self._reference_value=(self._exact_neutral if self._preserve_value_precision and self.knob._reset_release else None)
        self.set_unknown(False)
        self.set_mixed(False)
        if self.callback:
            self.callback(self.get())


class NativeMidiActivityBar(NativePaintReuseMixin,QWidget):
    """Incoming MIDI velocity/activity, explicitly not a post-synthesis meter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dark = True
        self._ratio = 0.0
        self._held = 0
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

    def sizeHint(self):
        return QSize(8, 112)

    def set_theme(self, dark):
        self._dark = bool(dark)
        self.update()

    def set_activity(self, velocity: float, held: int):
        ratio = max(0.0, min(1.0, float(velocity) / 127.0))
        held = max(0, int(held))
        count = max(1, self.height() // 5)
        changed = math.ceil(count * ratio) != math.ceil(count * self._ratio)
        self._ratio, self._held = ratio, held
        if changed:
            self.update()

    def paintEvent(self, event):
        palette = widget_palette(self, self._dark)
        painter = QPainter(self)
        # Paint every pixel with the strip's surface, including between cells.
        # Unlit cells remain visible, without one continuous dark lane.
        painter.fillRect(self.rect(), QColor(palette.surface_alt))
        if self.width() < 3 or self.height() < 5:
            return
        inner = self.rect()
        count = max(1, inner.height() // 5)
        lit = min(count, math.ceil(count * self._ratio))
        step = inner.height() / count
        for index in range(count):
            top = inner.bottom() + 1 - round((index + 1) * step)
            bottom = inner.bottom() + 1 - round(index * step) - 1
            cell = QRect(inner.left(), top, inner.width(), max(1, bottom - top))
            painter.fillRect(cell, QColor(palette.accent if index < lit else palette.meter_lane))


class NativeRotatedLabel(QWidget):
    """Counter-clockwise caption with readable tooltip and accessible name."""

    def __init__(self, text="", *, top_aligned=False, preferred_width=17,
                 wrap=False, text_px=12, emphasis=False, leading_edge=False,
                 clockwise=False, line_overlap=5, parent=None):
        super().__init__(parent)
        self._text = text
        self._dark = True
        self._top_aligned = top_aligned
        self._preferred_width = preferred_width
        self._wrap = wrap
        self._text_px = text_px
        self._emphasis = emphasis
        self._leading_edge = leading_edge
        self._tone = None
        self._clockwise = clockwise
        self._line_overlap = line_overlap
        self.setToolTip(text)
        self.setAccessibleName(text)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        font = native_text_font()
        font.setPixelSize(text_px)
        font.setLetterSpacing(QFont.AbsoluteSpacing, -0.5)
        self.setFont(font)

    def set_text(self, text):
        if text != self._text:
            self._text = text
            self.setToolTip(text)
            self.setAccessibleName(text)
            self.update()

    def native_tooltip_content(self,position):
        text=self.toolTip()
        if not tooltip_repeats_caption(text,self._text):return text,self.rect()
        font=native_text_font();font.setPixelSize(self._text_px)
        font.setLetterSpacing(QFont.AbsoluteSpacing,-0.5)
        metrics=QFontMetrics(font);lines=[self._text]
        if self._wrap and metrics.horizontalAdvance(self._text)>self.height()-2:
            words=self._text.split();split=len(self._text)//2
            lines=words if len(words)==2 else [self._text[:split],self._text[split:]]
        fits=all(metrics.horizontalAdvance(line)<=self.height()-2 for line in lines)
        return ('' if fits else text),self.rect()

    def sizeHint(self):
        return QSize(self._preferred_width, 80)

    def minimumSizeHint(self):
        return QSize(0, 0)

    def set_theme(self, dark):
        self._dark = bool(dark)
        self.update()

    def set_tone(self, tone=None):
        if tone != self._tone:
            self._tone = tone
            self.update()

    def paintEvent(self, event):
        palette = widget_palette(self, self._dark)
        painter = QPainter(self)
        painter.translate(self.width(), 0) if self._clockwise else painter.translate(0, self.height())
        painter.rotate(90 if self._clockwise else -90)
        painter.setPen(QColor(getattr(palette, self._tone) if self._tone else
                              palette.text if self._emphasis else palette.label))
        font = native_text_font()
        font.setPixelSize(self._text_px)
        font.setStyleStrategy(QFont.PreferAntialias)
        font.setLetterSpacing(QFont.AbsoluteSpacing, -0.5)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        lines = [self._text]
        if self._wrap and metrics.horizontalAdvance(self._text) > self.height() - 2:
            words = self._text.split()
            if len(words) == 2:
                lines = words
            else:
                split = len(self._text) // 2
                lines = [self._text[:split], self._text[split:]]
        line_height = max(1, metrics.height() - (self._line_overlap if len(lines) > 1 else 0))
        first_y = (0 if self._leading_edge else
                   max(0, (self.width() - len(lines) * line_height) // 2))
        alignment = (Qt.AlignRight if self._top_aligned else Qt.AlignHCenter) | Qt.AlignVCenter
        for index, line in enumerate(lines):
            display = metrics.elidedText(line, Qt.ElideRight, self.height() - 2)
            painter.drawText(QRect(0, first_y + index * line_height,
                                   self.height(), line_height), alignment, display)


def set_channel_protocol_caption(field, text, *, status=False):
    """Only long two-line names need wide legends; CC dials retain their room."""
    badge = field.cc_badge
    font = native_text_font()
    font.setPixelSize(badge._text_px)
    font.setLetterSpacing(QFont.AbsoluteSpacing, -0.5)
    metrics = QFontMetrics(font)
    doubled = metrics.horizontalAdvance(text) > 38
    width = max(METRICS["channel_protocol_width"],
                2 * max(1, metrics.height() - 2) + 1 if doubled else 0)
    if not text and not status:
        width = 0
    badge._line_overlap = 2
    badge.set_text(text)
    badge._preferred_width = width
    field.cc_status._preferred_width = width
    for label in (badge, field.cc_status):
        label.setMaximumWidth(width)
        label.updateGeometry()
    field.cc_cluster.setMaximumWidth(field.cc_control.sizeHint().width()
                                      + 2 * (width + METRICS["channel_protocol_gap"]))


class NativeChannelStrip(NativePaintReuseMixin,QFrame):
    """Preferred mixer width for an explicit horizontal collection, no minimum."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # This container's canvas is the monitor body's solid surface. Paint
        # its rounded corners too, so child repaints need not propagate the
        # parent backgrounds through the whole mixer at every update/move.
        self.setAttribute(Qt.WA_OpaquePaintEvent)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(widget_palette(self).surface))
        option = QStyleOption()
        option.initFrom(self)
        self.style().drawPrimitive(QStyle.PE_Widget, option, painter, self)
        painter.end()
        # Existing QSS owns radius, border and fill; the style primitive must
        # follow our opaque canvas, not QWidget's earlier background pass.

    def event(self, event):
        result = super().event(event)
        if event.type() in (QEvent.Show, QEvent.Polish, QEvent.StyleChange):
            # QSS clears opacity while applying its rounded box. Our painter
            # supplies the missing corners, so restore after that polish.
            self.setAttribute(Qt.WA_OpaquePaintEvent)
        return result

    def sizeHint(self):
        hint = super().sizeHint()
        return QSize(48, hint.height())

    def minimumSizeHint(self):
        return QSize(0, 0)


class NativeCard(QFrame):
    """Shared card shell; content inset is a declared task role,not a caller guess."""
    def __init__(self, parent=None, compact=False, *, inset_role=None):
        super().__init__(parent)
        self.setObjectName("NativeCardCompact" if compact else "NativeCard")
        self.inset_role=inset_role or ('telemetry' if compact else 'form')

    def content_insets(self):
        uniform=lambda value:(value,)*4
        roles={'form':uniform(METRICS['card_padding']),
               'compact-form':uniform(LAYOUT_DENSITIES[DEFAULT_LAYOUT_DENSITY]['card_padding']),
               'telemetry':LAYOUT_DENSITIES[DEFAULT_LAYOUT_DENSITY]['state_card_margin'],'log':METRICS['log_card_margin'],
               'inspector-shell':uniform(METRICS['card_padding']//2),
               'visualizer':uniform(0),'plain':uniform(0)}
        return roles[self.inset_role]

    def configure_content_layout(self,layout):
        if layout is self.layout():
            layout.setProperty('nativeLayoutRole','card-content:'+self.inset_role)
            layout.setContentsMargins(*self.content_insets())


class NativeActionButton(QPushButton):
    """Natural button width at rest, zero horizontal floor under pressure."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        # Only semantic confirmation explicitly opts into Enter. Focus on a
        # mode/browse/reset button must not create a dialog-wide default action.
        self.setAutoDefault(False)
        self.setDefault(False)
        self._caption_rotation = 0
        self._action_height = FORM_ROW_HEIGHT
        self.setFixedHeight(FORM_ROW_HEIGHT)
        sync_native_command_cursor(self)

    def event(self,event):
        if self.property('nativeQuietCommand'):update_native_keyboard_focus(self,event)
        return super().event(event)

    def set_compact(self, compact=True):
        """One density contract for caption, style box and layout height."""
        self._action_height = METRICS['compact_action_height'] if compact else FORM_ROW_HEIGHT
        self.setProperty('compactAction', bool(compact))
        self.style().unpolish(self)
        self.style().polish(self)
        self._sync_caption_extent()
        self.updateGeometry()
        self.update()

    def set_caption_rotation(self, degrees):
        if degrees not in (-90, 0, 90):
            raise ValueError("button caption rotation")
        self._caption_rotation = degrees
        self._sync_caption_extent()
        self.updateGeometry()
        self.update()

    def _sync_caption_extent(self):
        if getattr(self, "_caption_rotation", 0):
            self.setFixedHeight(super().sizeHint().width())
        else:
            self.setFixedHeight(self._action_height)

    def setText(self, text):
        if self.text()==text: return
        super().setText(text)
        if getattr(self, "_caption_rotation", 0):
            self._sync_caption_extent()

    def sizeHint(self):
        hint = super().sizeHint()
        if self.text():
            option=QStyleOptionButton();self.initStyleOption(option)
            option.rect=QRect(0,0,1024,1024)
            content=self.style().subElementRect(QStyle.SE_PushButtonContents,option,self)
            chrome=max(0,option.rect.width()-content.width())
            icon_width=(self.iconSize().width()+4) if not self.icon().isNull() else 0
            hint.setWidth(max(hint.width(),self.fontMetrics().horizontalAdvance(self.text().replace('&',''))+chrome+icon_width))
        return QSize(hint.height(), hint.width()) if getattr(self, "_caption_rotation", 0) else hint

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type()==QEvent.EnabledChange:sync_native_command_cursor(self)
        if getattr(self, "_caption_rotation", 0) and event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._sync_caption_extent()

    def paintEvent(self, event):
        if not self._caption_rotation:
            super().paintEvent(event)
            return
        option = QStyleOptionButton()
        self.initStyleOption(option)
        caption = option.text
        option.text = ""
        painter = QStylePainter(self)
        painter.drawControl(QStyle.CE_PushButton, option)
        painter.setClipRect(self.rect())
        if self._caption_rotation == -90:
            painter.translate(0, self.height())
        else:
            painter.translate(self.width(), 0)
        painter.rotate(self._caption_rotation)
        option.rect = QRect(0, 0, self.height(), self.width())
        option.text = caption
        painter.drawControl(QStyle.CE_PushButtonLabel, option)

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        return QSize(0, hint.height())


class NativeSettingsButton(NativeActionButton):
    """Theme-aware vector gear with the common text/button interaction."""
    def __init__(self,caption,callback,*,icon_only=False):
        super().__init__('' if icon_only else caption)
        self.setAccessibleName(caption);self.setIconSize(QSize(20,20))
        if icon_only:self.setToolTip(caption)
        self._gear_color=None;self._sync_gear();self.clicked.connect(callback)

    def _sync_gear(self):
        color=widget_palette(self).text
        if self._gear_color==color:return
        self._gear_color=color
        image=QPixmap(32,32);image.fill(Qt.transparent)
        painter=QPainter(image);painter.setRenderHint(QPainter.Antialiasing)
        painter.translate(16,16);painter.setPen(QPen(QColor(color),2,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush);points=[]
        for tooth in range(6):
            for part,radius in ((0,10),(1,13),(2,13),(3,10)):
                angle=2*math.pi*(tooth+part/4)/6
                points.append(QPointF(radius*math.cos(angle),radius*math.sin(angle)))
        painter.drawPolygon(QPolygonF(points));painter.drawEllipse(QPointF(0,0),4,4);painter.end()
        self.setIcon(QIcon(image))

    def paintEvent(self,event):
        self._sync_gear();super().paintEvent(event)


class NativeAppearanceButton(NativeActionButton):
    """Vector sun/moon with the same segmented button feedback and hit target."""
    def __init__(self, symbol, caption, parent=None):
        super().__init__('',parent)
        if symbol not in ('light','dark'):raise ValueError('appearance symbol')
        self.symbol=symbol;self.setAccessibleName(caption)
        self.setToolTip(tr('appearance.'+symbol+'_tip'))

    def sizeHint(self):
        return QSize(FORM_ROW_HEIGHT+8,FORM_ROW_HEIGHT)

    def paintEvent(self,event):
        super().paintEvent(event)
        extent=min(18.0,self.height()-8.0,self.width()-8.0)
        if extent<=0:return
        painter=QPainter(self);painter.setRenderHint(QPainter.Antialiasing)
        color=self.palette().color(QPalette.ButtonText)
        painter.setPen(QPen(color,1.5,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush);painter.translate(self.rect().center())
        radius=extent*.45
        if self.symbol=='light':
            painter.drawEllipse(QPointF(0,0),radius*.47,radius*.47)
            for index in range(8):
                angle=index*math.pi/4
                painter.drawLine(QPointF(math.cos(angle)*radius*.76,math.sin(angle)*radius*.76),
                                 QPointF(math.cos(angle)*radius,math.sin(angle)*radius))
        else:
            disc=QPainterPath();disc.addEllipse(QPointF(0,0),radius,radius)
            cut=QPainterPath();cut.addEllipse(QPointF(radius*.55,-radius*.4),radius*.92,radius*.92)
            painter.setPen(Qt.NoPen);painter.setBrush(color);painter.drawPath(disc.subtracted(cut))
        painter.end()


class NativeFilterButton(QToolButton):
    """Shared checkable filter control for compact dataset views."""

    def __init__(self, caption, parent=None):
        super().__init__(parent)
        self.setObjectName("FilterButton")
        self.setText(caption)
        self.setCheckable(True)
        self.setAutoRaise(False)
        sync_native_command_cursor(self)
        self.setMinimumWidth(0)
        self.setFixedHeight(FORM_ROW_HEIGHT)
        # Ignored next to a trailing stretch gives every filter zero width
        # even with ample room. Keep the caption as a preferred hint, while
        # minimumSizeHint below still permits horizontal compression.
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setAccessibleName(caption)

    def minimumSizeHint(self):
        return QSize(0, FORM_ROW_HEIGHT)

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type()==QEvent.EnabledChange:sync_native_command_cursor(self)


class NativeSegmentedChoice(QFrame):
    """A compact, exclusive mode choice with no horizontal size floor."""

    selected = Signal(int)
    INLINE_PADDING = 8

    def __init__(self, captions: tuple[str, ...], parent=None, *, symbols=None):
        super().__init__(parent)
        self.setObjectName("NativeSegmentedChoice")
        self.setFixedHeight(FORM_ROW_HEIGHT)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.group = QButtonGroup(self)
        row = QHBoxLayout(self)
        row.setContentsMargins(1, 1, 1, 1)
        row.setSpacing(0)
        self.buttons = []
        if symbols is not None and len(symbols)!=len(captions):raise ValueError('segmented symbols')
        for index, caption in enumerate(captions):
            button = NativeActionButton(caption, self) if symbols is None else NativeAppearanceButton(symbols[index],caption,self)
            button.setObjectName("SegmentStart" if index == 0 else "SegmentEnd" if index == len(captions)-1 else "SegmentMiddle")
            button.setFixedHeight(FORM_ROW_HEIGHT - 2)
            button.setCheckable(True)
            button.setMinimumWidth(0)
            button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            button.installEventFilter(self)
            self.group.addButton(button, index)
            row.addWidget(button)
            self.buttons.append(button)
        self.group.idClicked.connect(self.selected)
        self.set_selected(0)

    def sizeHint(self):
        selected_font = QFont(self.font())
        selected_font.setWeight(QFont.Bold)
        metrics = QFontMetrics(selected_font)
        # The selected caption becomes bold. Reserve its real advance plus
        # breathing room at normal width; minimumSizeHint still remains 0.
        width = sum((button.sizeHint().width() if isinstance(button,NativeAppearanceButton) else
                    metrics.horizontalAdvance(button.text()) + self.INLINE_PADDING * 2 + 8)
                    for button in self.buttons) + 2
        return QSize(width, FORM_ROW_HEIGHT)

    def minimumSizeHint(self):
        return QSize(0, FORM_ROW_HEIGHT)

    def set_selected(self, index: int):
        self.buttons[index].setChecked(True)

    def selected_index(self):
        return self.group.checkedId()

    def eventFilter(self, watched, event):
        if watched in self.buttons and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Home, Qt.Key_End):
                available=[index for index,button in enumerate(self.buttons)
                           if button.isEnabled() and not button.isHidden()]
                if not available:return True
                origin=self.group.id(watched)
                target = (available[0] if event.key() == Qt.Key_Home else
                          available[-1] if event.key() == Qt.Key_End else
                          next(index for step in range(1,len(self.buttons)+1)
                               if (index:=(origin+(1 if event.key()==Qt.Key_Right else -1)*step)%len(self.buttons)) in available))
                self.buttons[target].setFocus()
                self.buttons[target].click()
                return True
        return super().eventFilter(watched, event)


def make_action_button(caption, callback, *, primary=False, checkable=False, object_name=None, compact=False):
    """One button constructor; semantic roles select their shared style token."""
    button = NativeActionButton(caption)
    button.setCheckable(checkable)
    if object_name or primary:
        button.setObjectName(object_name or "PrimaryButton")
    if compact:
        button.set_compact()
    button.clicked.connect(callback)
    return button


def set_button_primary(button, primary, *, default=False):
    """Change semantic emphasis through the same shared palette stylesheet."""
    name = "PrimaryButton" if primary else ""
    if button.objectName() != name:
        button.setObjectName(name)
        button.style().unpolish(button); button.style().polish(button)
        apply_native_font_raster(button)
        button.update()
    button.setDefault(bool(default))


class NativeTransportButton(NativeActionButton):
    """Theme-aware vector media command, not an emoji or caption substitution."""
    def __init__(self, command, caption, callback, *, primary=False):
        super().__init__('')
        self.command = command
        self.primary = bool(primary)
        self.setObjectName('TransportGraphic')
        prepare_flat_command(self)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Fixed)
        self.set_command(command,caption)
        self.clicked.connect(callback)

    def set_command(self, command, caption):
        if command not in ('previous','play','pause','stop','next','zoom_in','zoom_out','zoom_fit','repeat','loop','shuffle','undo','redo'):
            raise ValueError('media command')
        self.command=command
        self.setAccessibleName(caption)
        self.setToolTip(caption)
        self.update()

    def sizeHint(self):
        return QSize(FORM_ROW_HEIGHT,FORM_ROW_HEIGHT)

    def paintEvent(self,event):
        # Do not paint QPushButton/QSS chrome then overlay a symbol. One
        # painter owns both feedback and glyph, with no idle button box.
        painter=QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        palette=widget_palette(self)
        side=min(self.width(),self.height())
        center=QPointF(self.width()*.5,self.height()*.5)
        color=paint_flat_command_feedback(painter,self,palette)
        if self.isEnabled() and self.primary:color=palette.accent
        painter.setPen(Qt.NoPen); painter.setBrush(QColor(color))
        scale=side*.56
        painter.translate(center); painter.scale(scale,scale)
        if self.command in ('repeat','loop','shuffle','undo','redo'):
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(color),.095,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
            path=QPainterPath()
            if self.command in ('undo','redo'):
                painter.scale(-1 if self.command=='redo' else 1,1)
                path.moveTo(.52,.35);path.cubicTo(.60,-.28,.05,-.45,-.50,-.14)
                painter.drawPath(path)
                painter.drawLine(QPointF(-.50,-.14),QPointF(-.22,-.10))
                painter.drawLine(QPointF(-.50,-.14),QPointF(-.45,-.42))
            elif self.command in ('repeat','loop'):
                path.moveTo(-.44,-.34);path.lineTo(.30,-.34)
                path.quadTo(.58,-.34,.58,0);path.quadTo(.58,.34,.30,.34)
                path.lineTo(-.30,.34);path.quadTo(-.58,.34,-.58,0)
                path.quadTo(-.58,-.34,-.44,-.34)
                painter.drawPath(path)
                for sign in (-1,1):
                    painter.drawLine(QPointF(sign*.30,-sign*.34),QPointF(sign*.12,-sign*.51))
                    painter.drawLine(QPointF(sign*.30,-sign*.34),QPointF(sign*.12,-sign*.17))
                if self.command=='repeat':
                    # Current-track repeat has1; a preview region loop does not.
                    painter.drawLine(QPointF(-.07,-.07),QPointF(.02,-.15))
                    painter.drawLine(QPointF(.02,-.15),QPointF(.02,.15))
            else:
                for sign in (-1,1):
                    path=QPainterPath();path.moveTo(-.58,sign*.34)
                    path.cubicTo(-.12,sign*.34,.12,-sign*.34,.58,-sign*.34)
                    painter.drawPath(path)
                    painter.drawLine(QPointF(.58,sign*.34),QPointF(.39,sign*.34-.17))
                    painter.drawLine(QPointF(.58,sign*.34),QPointF(.39,sign*.34+.17))
            if self.isChecked():
                painter.setPen(Qt.NoPen);painter.setBrush(QColor(color))
                painter.drawEllipse(QPointF(0,.68),.065,.065)
        elif self.command in ('zoom_in','zoom_out','zoom_fit'):
            # Own vectors using the familiar magnifier / horizontal-fit
            # convention. No font glyph,SVG loader or third-party asset copy.
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(color),.095,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
            if self.command=='zoom_fit':
                for sign in (-1,1):
                    painter.drawLine(QPointF(sign*.6,-.5),QPointF(sign*.6,.5))
                    painter.drawLine(QPointF(sign*.5,0),QPointF(sign*.15,0))
                    painter.drawLine(QPointF(sign*.5,0),QPointF(sign*.33,-.18))
                    painter.drawLine(QPointF(sign*.5,0),QPointF(sign*.33,.18))
            else:
                painter.drawEllipse(QPointF(-.12,-.12),.43,.43)
                painter.drawLine(QPointF(.19,.19),QPointF(.61,.61))
                painter.drawLine(QPointF(-.35,-.12),QPointF(.11,-.12))
                if self.command=='zoom_in':painter.drawLine(QPointF(-.12,-.35),QPointF(-.12,.11))
        elif self.command=='stop':
            painter.drawRect(QRectF(-.45,-.45,.9,.9))
        elif self.command=='pause':
            painter.drawRect(QRectF(-.45,-.5,.3,1)); painter.drawRect(QRectF(.15,-.5,.3,1))
        else:
            direction=-1 if self.command=='previous' else 1
            painter.scale(direction,1)
            if self.command=='play':
                # Centre the triangle's filled mass, not its bounding box.
                painter.drawPolygon(QPolygonF([QPointF(-.30,-.55),QPointF(.60,0),QPointF(-.30,.55)]))
            else:
                painter.drawPolygon(QPolygonF([QPointF(-.6,-.55),QPointF(.25,0),QPointF(-.6,.55)]))
                painter.drawRect(QRectF(.43,-.55,.17,1.1))


def paint_rack_glyph(painter, command, rect, color):
    """Shared vector commands for rack toolbar and item delegate."""
    painter.save(); painter.translate(rect.center())
    scale = min(rect.width(), rect.height()) / 20
    painter.scale(scale, scale)
    painter.setPen(QPen(QColor(color), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    if command == "power":
        painter.drawArc(QRectF(-6, -6, 12, 12), 135 * 16, 270 * 16)
        painter.drawLine(QPointF(0, -8), QPointF(0, -1))
    elif command in ("add", "remove"):
        painter.drawLine(-6, 0, 6, 0)
        if command == "add": painter.drawLine(0, -6, 0, 6)
    elif command in ('left','right'):
        sign=1 if command=='right' else -1
        painter.drawLine(QPointF(-2*sign,-5),QPointF(3*sign,0))
        painter.drawLine(QPointF(3*sign,0),QPointF(-2*sign,5))
    elif command in ("up", "down"):
        sign = 1 if command == "down" else -1
        painter.drawLine(QPointF(-5, -2 * sign), QPointF(0, 3 * sign))
        painter.drawLine(QPointF(0, 3 * sign), QPointF(5, -2 * sign))
    painter.restore()


class _AuditionIconEngine(QIconEngine):
    """Original ABMSynth adjusted-A note;see assets/icons/music-note.svg."""
    def __init__(self,color):
        super().__init__();self.color=QColor(color)
        # Winding fill keeps the authored head/stem/flag overlaps solid.
        head=QPainterPath();head.addEllipse(QRectF(-4.2,-2.7,8.4,5.4))
        placement=QTransform();placement.translate(9.5,17.7);placement.rotate(-22)
        path=placement.map(head);path.setFillRule(Qt.WindingFill)
        path.moveTo(11.9,17.7);path.lineTo(11.9,3.2)
        path.lineTo(13.7,3.2);path.lineTo(13.7,17.2);path.closeSubpath()
        path.moveTo(12.8,3.2)
        path.cubicTo(14.1,6.1,19.2,6.2,19.2,10.2)
        path.cubicTo(19.2,12,18.1,13.5,16.7,14.3)
        path.cubicTo(17.5,12.8,17.8,11.3,16.6,10.1)
        path.cubicTo(15.5,9,13.9,8.5,12.8,8.1)
        path.closeSubpath();self.path=path

    def clone(self):return _AuditionIconEngine(self.color)

    def paint(self,painter,rect,mode,state):
        painter.save();painter.setRenderHint(QPainter.Antialiasing)
        rect=QRectF(rect);bounds=self.path.boundingRect()
        scale=min(rect.width()/bounds.width(),rect.height()/bounds.height())
        painter.translate(rect.center());painter.scale(scale,scale)
        painter.translate(-bounds.center())
        painter.setPen(Qt.NoPen);painter.setBrush(self.color)
        painter.drawPath(self.path);painter.restore()

    def pixmap(self,size,mode,state):
        image=QPixmap(size);image.fill(Qt.transparent);painter=QPainter(image)
        self.paint(painter,image.rect(),mode,state);painter.end();return image


class NativeAuditionButton(NativeActionButton):
    """Ordinary command chrome with a vector eighth-note image,not a glyph."""
    def __init__(self,caption,*,note=None):
        super().__init__(note or '')
        self.setAccessibleName(caption);self.setToolTip(caption)
        self.setMinimumWidth(0);self.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Fixed)
        self.setIconSize(QSize(16,16));self._note_ink=None;self._sync_note_icon()

    def _sync_note_icon(self):
        palette=widget_palette(self)
        ink=command_state_colors(palette,enabled=self.isEnabled(),
            checked=self.isChecked() and self.objectName()=='ChannelMonitorAction',
            hovered=self.underMouse(),pressed=self.isDown())[2]
        if self._note_ink!=ink:
            self._note_ink=ink;self._note_engine=_AuditionIconEngine(ink)
            self.setIcon(QIcon(self._note_engine))

    def sizeHint(self):
        if not self.text():return QSize(self._action_height,self._action_height)
        return super().sizeHint()

    def paintEvent(self,event):
        self._sync_note_icon()
        option=QStyleOptionButton();self.initStyleOption(option)
        area=self.style().subElementRect(QStyle.SE_PushButtonContents,option,self)
        option.text='';option.icon=QIcon()
        painter=QStylePainter(self);painter.drawControl(QStyle.CE_PushButton,option)
        # QSS text padding can leave only2px in a compact icon-only command.
        # It is a spacing preference,not a mask that may cut away the image.
        painter.setClipRect(self.rect().adjusted(2,2,-2,-2))
        text_rect,icon_rect=self._caption_geometry(QRectF(area))
        if self.text():
            bounds=QFontMetricsF(self.font()).tightBoundingRect(self.text())
            painter.setPen(QColor(self._note_ink));painter.setFont(self.font())
            painter.drawText(QPointF(text_rect.left()-bounds.left(),text_rect.top()-bounds.top()),self.text())
        self._note_engine.paint(painter,icon_rect,QIcon.Normal,QIcon.Off)

    def _caption_geometry(self,area):
        """Center visible text and path bounds together,independent of padding."""
        bounds=self._note_engine.path.boundingRect()
        height=float(self.iconSize().height());width=height*bounds.width()/bounds.height()
        text=QFontMetricsF(self.font()).tightBoundingRect(self.text())
        gap=4. if self.text() else 0.;total=text.width()+gap+width
        left=area.center().x()-total/2;center=area.center().y()
        return (QRectF(left,center-text.height()/2,text.width(),text.height()),
                QRectF(left+text.width()+gap,center-height/2,width,height))


class NativeHoldButton(NativeAuditionButton):
    """Shared momentary audition;one On/Off pair per real press gesture."""
    heldChanged=Signal(bool)

    def __init__(self,caption,*,note=None):
        self._held=False
        super().__init__(caption,note=note)
        self.setAutoRepeat(False)
        self.pressed.connect(lambda:self._set_held(True))
        self.released.connect(lambda:self._set_held(False))

    def _set_held(self,held):
        if self._held==held:return
        self._held=held;self.heldChanged.emit(held)

    def cancel_pointer_gesture(self):
        self._set_held(False);self.setDown(False)

    def event(self,event):
        if event.type() in (QEvent.Hide,QEvent.WindowDeactivate,QEvent.UngrabMouse,QEvent.FocusOut) or (
                event.type()==QEvent.EnabledChange and not self.isEnabled()):
            self.cancel_pointer_gesture()
        return super().event(event)

    def keyPressEvent(self,event):
        if event.isAutoRepeat():event.accept();return
        super().keyPressEvent(event)

    def mouseReleaseEvent(self,event):
        super().mouseReleaseEvent(event)
        # QAbstractButton does not emit released for every outside release.
        if event.button()==Qt.LeftButton:self.cancel_pointer_gesture()

    def mouseMoveEvent(self,event):
        # The note belongs to the held gesture,not the hover rectangle.
        # Keep it sounding outside the button until release/cancellation.
        if self._held:event.accept();return
        super().mouseMoveEvent(event)

    def keyReleaseEvent(self,event):
        if event.isAutoRepeat():event.accept();return
        super().keyReleaseEvent(event)


class NativePressLatchAuditionButton(NativeAuditionButton):
    """Same checked command,with its transition on press,not release."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.setCheckable(True);self.pressed.connect(self.toggle)

    def nextCheckState(self):
        pass # QAbstractButton release must not toggle a second time.

    def mouseMoveEvent(self,event):
        # QAbstractButton emits pressed again when a held pointer re-enters.
        # A latched audition must change only once per physical press.
        if event.buttons()&Qt.LeftButton:event.accept();return
        super().mouseMoveEvent(event)

    def keyPressEvent(self,event):
        if event.isAutoRepeat():event.accept();return
        super().keyPressEvent(event)

    def keyReleaseEvent(self,event):
        if event.isAutoRepeat():event.accept();return
        super().keyReleaseEvent(event)


class NativeTuningAudition(QObject):
    """One UI lifetime binding;audio handles stay with AudioSession."""
    def __init__(self,owner,request,parent,*,note=None,phrase=False,one_shot=False):
        super().__init__()
        self.owner,self.request,self.source,self.identity=owner,request,None,None
        self.phrase=phrase;self.one_shot=one_shot
        self.button=(NativeHoldButton(tr('instrument_audition.play')) if phrase else
                     NativeAuditionButton(tr('tuning_preview.drum'),note=note) if one_shot else
                     NativePressLatchAuditionButton(tr('tuning_preview.toggle'),note=note))
        self.button.setObjectName('ChannelMonitorAction')
        self.button.setParent(parent)
        self.setParent(self.button)
        self.timer=QTimer(self);self.timer.setInterval(50);self.timer.timeout.connect(self.refresh)
        if phrase:self.button.heldChanged.connect(self.change_hold)
        elif one_shot:self.button.pressed.connect(lambda:self.change_hold(True))
        else:self.button.setCheckable(True);self.button.toggled.connect(self.change_hold)
        self.button.installEventFilter(self)
        self.button.destroyed.connect(self.stop)
        self.button.setToolTip(tr('instrument_audition.help' if phrase else
                                  'tuning_preview.drum_help' if one_shot else 'tuning_preview.help'))

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Hide,QEvent.WindowDeactivate) or (event.type()==QEvent.EnabledChange and not watched.isEnabled()):self.stop()
        return False

    def key(self,request):
        return (self.owner.audio_session.stream,request.get('channel'),request.get('source'),request.get('identity'),request['note'])

    def stop(self,*_args):
        self.timer.stop();source,self.source=self.source,None;self.identity=None
        if shiboken6.isValid(self.button):
            blocked=self.button.blockSignals(True)
            self.button.setChecked(False)
            if self.phrase:self.button.cancel_pointer_gesture()
            self.button.blockSignals(blocked)
        if source and not source.closed:
            try:
                source.close()
            except Exception as exc:self.owner.append_log(log_text('tuning_preview.failed',error=str(exc)))

    def change_hold(self,held):
        if not held:self.stop();return
        try:
            request=self.request()
            if request is None:
                self.stop();return
            if self.one_shot:
                request=dict(request,one_shot=True)
                self.stop()
            self.identity=self.key(request);self.source=self.owner.begin_tuning_preview(request)
            if self.source:self.timer.start()
            else:self.stop()
        except Exception as exc:
            self.stop();self.owner.append_log(log_text('tuning_preview.failed',error=str(exc)))

    def refresh(self):
        try:
            request=self.request()
            if self.source is None or self.source.closed or request is None or self.key(request)!=self.identity:
                self.stop();return
            if self.one_shot and self.source.one_shot_finished():
                self.stop();return
            self.owner.update_tuning_preview(self.source,request)
        except Exception as exc:
            self.stop()
            self.owner.append_log(log_text('tuning_preview.failed',error=str(exc)))


def make_tuning_audition(owner,request,parent,*,note=None,one_shot=False):
    binding=NativeTuningAudition(owner,request,parent,note=note,one_shot=one_shot);button=binding.button
    button._tuning_audition=binding
    if note:
        button.setAccessibleName(tr('tuning_preview.note',note=note))
        button.setToolTip(tr('tuning_preview.note_help',note=note))
    return button


def make_instrument_audition(owner,request,parent):
    binding=NativeTuningAudition(owner,request,parent,phrase=True)
    binding.button._tuning_audition=binding
    return binding.button


def make_effect_audition(owner,effect,parent):
    """Shared held phrase for a global effect,using the ordinary audition UI."""
    button=make_instrument_audition(owner,
        lambda:owner.audition_request(effect=effect() if callable(effect) else effect),parent)
    button.setToolTip(tr('instrument_audition.effect_help'))
    return button


class NativeRackButton(NativeActionButton):
    """Compact preferred-width graphic command with keyboard/accessibility."""
    def __init__(self, command, caption, callback, *, checkable=False):
        super().__init__("")
        self.command = command
        self.setAccessibleName(caption); self.setToolTip(caption)
        self.setCheckable(checkable); self.clicked.connect(callback)
        prepare_flat_command(self); self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def sizeHint(self): return QSize(FORM_ROW_HEIGHT, FORM_ROW_HEIGHT)
    def minimumSizeHint(self): return QSize(0, FORM_ROW_HEIGHT)

    def paintEvent(self, event):
        p = widget_palette(self); painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        area = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        color=paint_flat_command_feedback(painter,self,p)
        glyph = QRectF(area.center().x()-10, area.center().y()-10, 20, 20)
        paint_rack_glyph(painter, self.command, glyph, color)


class NativeDisclosureButton(NativeRackButton):
    """Visibility-only command. Checked means expanded, never effect enabled."""
    def __init__(self,section):
        self.section_title=section
        super().__init__('down','',lambda:None,checkable=True)
        self.toggled.connect(self._present_disclosure)
        self.setChecked(True)
        self.setToolTip(tr('channel_editor.section_disclosure_tooltip'))

    def setChecked(self,expanded):
        super().setChecked(expanded)
        # Silent settings readback must also refresh the accessible action.
        self._present_disclosure(self.isChecked())

    def _present_disclosure(self,expanded):
        key='section_collapse' if expanded else 'section_disclosure'
        self.setAccessibleName(tr('channel_editor.'+key,section=self.section_title))
        self.update()

    def keyPressEvent(self,event):
        key=event.key()
        if key in (Qt.Key_Return,Qt.Key_Enter):
            if not event.isAutoRepeat():self.click()
            event.accept();return
        if key in (Qt.Key_Left,Qt.Key_Right):
            self.setChecked((key==Qt.Key_Right)!=(self.layoutDirection()==Qt.RightToLeft))
            event.accept();return
        super().keyPressEvent(event)

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.Antialiasing)
        ink=paint_flat_command_feedback(painter,self,widget_palette(self),checked=False)
        center=QRectF(self.rect()).center()
        command='down' if self.isChecked() else ('left' if self.layoutDirection()==Qt.RightToLeft else 'right')
        paint_rack_glyph(painter,command,QRectF(center.x()-10,center.y()-10,20,20),ink)


class NativeRackItemDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        view=self.parent()
        width=(view.fontMetrics().horizontalAdvance(str(index.data(Qt.DisplayRole)))+FORM_ROW_HEIGHT+12
               if view.flow()==QListWidget.LeftToRight else 0)
        return QSize(width,FORM_ROW_HEIGHT)

    @staticmethod
    def row_color(palette, active, bypassed, hovered=False):
        effective = active and not bypassed
        dark = QColor(palette.window).lightness() < 128
        base = (palette.accent_soft if dark else palette.surface) if effective else (
            palette.input if dark else palette.surface_alt)
        if hovered:
            return QColor(base).lighter(110) if dark else QColor(base).darker(104)
        return QColor(base)

    def paint(self, painter, option, index):
        view = self.parent(); p = widget_palette(view)
        painter.save(); painter.setRenderHint(QPainter.Antialiasing)
        selected = bool(option.state & QStyle.State_Selected)
        hover = view.isEnabled() and view._hover_row == index.row()
        rect = QRectF(option.rect).adjusted(1, 1, -1, -1)
        active = bool(index.data(Qt.UserRole + 1))
        bypass = bool(view.property("rackBypassed"))
        painter.setPen(QPen(QColor(p.border), 1))
        painter.setBrush(self.row_color(p, active, bypass, hover))
        painter.drawRoundedRect(rect, 5, 5)
        color = p.text if active and not bypass else p.muted
        power = view.power_rect(option.rect)
        effective = active and not bypass
        painter.setPen(QPen(QColor(p.accent if active else p.muted), 1))
        painter.setBrush(QColor(p.accent) if effective else Qt.NoBrush)
        painter.drawEllipse(QRectF(power).adjusted(5, 5, -5, -5))
        paint_rack_glyph(painter, "power", QRectF(power).adjusted(6, 6, -6, -6), p.primary_text if effective else p.muted)
        text_rect = option.rect.adjusted(FORM_ROW_HEIGHT + 2, 0, -5, 0)
        painter.setFont(option.font); painter.setPen(QColor(color))
        painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter,
            option.fontMetrics.elidedText(index.data(Qt.DisplayRole), Qt.ElideRight, max(0, text_rect.width())))
        if selected:
            painter.setPen(Qt.NoPen); painter.setBrush(QColor(p.accent))
            painter.drawRoundedRect(QRectF(rect.left()+2, rect.top()+5, 2, max(0, rect.height()-10)), 1, 1)
        if view.hasFocus() and option.state & QStyle.State_HasFocus:
            painter.setPen(QPen(QColor(p.accent), 1)); painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect, 5, 5)
        painter.restore()


class NativeRowList(QListWidget):
    """A scrollable list with a row-based preferred height,not a size floor."""
    def sizeHint(self):
        row=max(self.fontMetrics().height(),self.sizeHintForRow(0))
        return QSize(0,4*row+2*self.frameWidth())

    def minimumSizeHint(self):return QSize(0,0)


class NativeRackList(QListWidget):
    """Pure presentation: ID-based insertion, no model merge or audio calls."""
    toggleRequested = Signal(int)
    editorRequested = Signal(int)
    moveRequested = Signal(str, int)
    removeRequested = Signal()
    stepRequested = Signal(int)
    contextRequested = Signal(object)
    MIME = "application/x-abmsynth-rack-slot"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(0, 0); self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragEnabled(True); self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setMouseTracking(True); self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setItemDelegate(NativeRackItemDelegate(self))
        self._insertion = None; self._command_press = None; self._hover_row = -1
        self._empty_press = False
        self.setAccessibleName(tr("audio_fx.chain_label"))
        connect_native_pointer_model(self)

    def sizeHint(self): return QSize(220, FORM_ROW_HEIGHT * max(3, min(8, self.count())))
    def minimumSizeHint(self): return QSize(0, 0)

    def set_compact_horizontal(self):
        """Same row commands and drag model, arranged as a compact chain."""
        self.setViewMode(QListWidget.IconMode)
        self.setFlow(QListWidget.LeftToRight);self.setWrapping(False)
        self.setResizeMode(QListWidget.Adjust);self.setMovement(QListWidget.Static)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.model().rowsInserted.connect(self._sync_compact_chain_height)
        self.model().rowsRemoved.connect(self._sync_compact_chain_height)
        self.model().dataChanged.connect(self._sync_compact_chain_height)
        self._compact_chain=True
        self._sync_compact_chain_height()

    def _sync_compact_chain_height(self,*_):
        width=sum(self.fontMetrics().horizontalAdvance(self.item(i).text())+FORM_ROW_HEIGHT+12 for i in range(self.count()))
        height=FORM_ROW_HEIGHT+(self.style().pixelMetric(QStyle.PM_ScrollBarExtent) if width>self.viewport().width() else 0)
        if self.height()!=height:self.setFixedHeight(height)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if getattr(self,'_compact_chain',False):self._sync_compact_chain_height()

    def refresh_pointer_feedback(self):refresh_native_pointer_feedback(self)

    def pointer_data_changed(self,first,last,roles):refresh_changed_pointer_feedback(self,first,last,roles)

    def reset_stale_hover(self):
        if self._hover_row != -1:
            self._hover_row = -1; self.viewport().update()

    def cancel_pointer_gesture(self):
        self._insertion=None;self._command_press=None;self._empty_press=False
        self.viewport().update()

    def viewportEvent(self, event):
        if event.type() == QEvent.Leave: self.reset_stale_hover()
        return super().viewportEvent(event)

    def mouseMoveEvent(self, event):
        self.reconcile_hover(event.position().toPoint())
        super().mouseMoveEvent(event)

    def reconcile_hover(self, position):
        index=self.indexAt(position)
        row=index.row() if self.isEnabled() and index.isValid() and index.flags() & Qt.ItemIsEnabled else -1
        if row!=self._hover_row:
            self._hover_row=row;self.viewport().update()

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx,dy)
        refresh_native_pointer_feedback(self)

    @staticmethod
    def power_rect(rect):
        return QRect(rect.left(), rect.top(), FORM_ROW_HEIGHT, rect.height())

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        self._empty_press = event.button() == Qt.LeftButton and item is None
        if self._empty_press:
            # Qt focuses the view before dispatch; blank viewport space is
            # not an outside click to the common ancestor-aware filter.
            self._command_press = None
            self.clearFocus()
            self.reset_stale_hover()
            self.viewport().update()
            event.accept()
            return
        if event.button() == Qt.LeftButton and item is not None:
            if self.power_rect(self.visualItemRect(item)).contains(event.position().toPoint()):
                self._command_press = item.data(Qt.UserRole)
                self.setCurrentItem(item); event.accept(); return
        self._command_press = None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._empty_press and event.button() == Qt.LeftButton:
            self._empty_press = False
            self.clearFocus()
            event.accept()
            return
        pressed, self._command_press = self._command_press, None
        item = self.itemAt(event.position().toPoint())
        if pressed and event.button() == Qt.LeftButton:
            if item is not None and item.data(Qt.UserRole) == pressed:
                if self.power_rect(self.visualItemRect(item)).contains(event.position().toPoint()):
                    self.toggleRequested.emit(self.row(item))
            event.accept(); return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and self.itemAt(event.position().toPoint()) is None:
            self._empty_press = True
            self._command_press = None
            self.clearFocus()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete and self.currentRow() >= 0:
            self.removeRequested.emit(); event.accept(); return
        order_keys=((Qt.Key_Left,Qt.Key_Right) if self.flow()==QListWidget.LeftToRight else (Qt.Key_Up,Qt.Key_Down))
        if event.modifiers() & Qt.AltModifier and event.key() in order_keys:
            self.stepRequested.emit(-1 if event.key()==order_keys[0] else 1); event.accept(); return
        if event.key() == Qt.Key_Space and self.currentRow() >= 0:
            self.toggleRequested.emit(self.currentRow()); event.accept(); return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and self.currentRow() >= 0:
            self.editorRequested.emit(self.currentRow()); event.accept(); return
        if event.key() == Qt.Key_Menu or (event.key() == Qt.Key_F10 and event.modifiers() & Qt.ShiftModifier):
            self.contextRequested.emit(self.viewport().mapToGlobal(self.visualItemRect(self.currentItem()).center()) if self.currentItem() else self.mapToGlobal(self.rect().center()))
            event.accept(); return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        item = self.itemAt(event.pos())
        if item is not None: self.setCurrentItem(item)
        self.contextRequested.emit(event.globalPos()); event.accept()

    def startDrag(self, actions):
        item = self.currentItem()
        if item is None: return
        mime = QMimeData(); mime.setData(self.MIME, str(item.data(Qt.UserRole)).encode("utf-8"))
        drag = QDrag(self); drag.setMimeData(mime)
        drag.setPixmap(self.viewport().grab(self.visualItemRect(item)))
        try:drag.exec(Qt.MoveAction)
        finally:
            self._insertion = None; self.viewport().update()
            refresh_native_pointer_feedback(self)

    def insertion_at(self, position):
        for row in range(self.count()):
            center=self.visualItemRect(self.item(row)).center()
            if (position.x()<center.x() if self.flow()==QListWidget.LeftToRight else position.y()<center.y()):return row
        return self.count()

    def _accept_drag(self, event):
        if event.source() is not self or not event.mimeData().hasFormat(self.MIME):
            event.ignore(); return False
        event.setDropAction(Qt.MoveAction); event.accept(); return True

    def dragEnterEvent(self, event): self._accept_drag(event)
    def dragMoveEvent(self, event):
        if self._accept_drag(event):
            self._insertion = self.insertion_at(event.position().toPoint())
            edge = FORM_ROW_HEIGHT
            horizontal=self.flow()==QListWidget.LeftToRight
            bar=self.horizontalScrollBar() if horizontal else self.verticalScrollBar()
            position=event.position().x() if horizontal else event.position().y()
            extent=self.viewport().width() if horizontal else self.viewport().height()
            if position<edge:bar.setValue(bar.value()-edge)
            elif position>extent-edge:bar.setValue(bar.value()+edge)
            self.viewport().update()
    def dragLeaveEvent(self, event):
        self._insertion = None; self.viewport().update(); event.accept()
    def dropEvent(self, event):
        if self._accept_drag(event):
            identity = bytes(event.mimeData().data(self.MIME)).decode("utf-8")
            self.moveRequested.emit(identity, self.insertion_at(event.position().toPoint()))
        self._insertion = None; self.viewport().update()
    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.count():
            painter = QPainter(self.viewport()); painter.setPen(QColor(widget_palette(self).muted))
            painter.drawText(self.viewport().rect(), Qt.AlignCenter, tr("audio_fx.no_effects"))
        if self._insertion is not None:
            horizontal=self.flow()==QListWidget.LeftToRight
            rect=(self.visualItemRect(self.item(self._insertion)) if self._insertion<self.count()
                  else self.visualItemRect(self.item(self.count()-1)) if self.count() else QRect(1,1,0,0))
            end=self._insertion>=self.count()
            mark=(rect.right()+1 if end else rect.left()) if horizontal else (rect.bottom()+1 if end else rect.top())
            painter = QPainter(self.viewport()); painter.setPen(QPen(QColor(widget_palette(self).accent), 2))
            if horizontal:painter.drawLine(mark,2,mark,max(2,self.viewport().height()-3))
            else:painter.drawLine(2,mark,max(2,self.viewport().width()-3),mark)


class NativeMessageBox(QMessageBox):
    """One app-owned message contract; Qt still owns modal/keyboard mechanics."""

    def __init__(self, parent=None):
        self._native_shown = False
        super().__init__(parent)
        install_native_window_shell(self)
        install_native_translations(QApplication.instance())
        self.setOption(QMessageBox.Option.DontUseNativeDialog, True)
        self.setTextFormat(Qt.PlainText)
        self.setWindowFlag(Qt.MSWindowsFixedSizeDialogHint, False)
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())
            self.setFont(parent.font())
        else:
            # No ancestor exists:standalone roots use the same theme builder.
            apply_native_stylesheet(self,native_stylesheet(widget_palette(self)))
        # Direct product messages ask for a decision. Acknowledgment helpers
        # and explicit warning/error callers replace this semantic default.
        self.setIcon(QMessageBox.Question)

    def addButton(self, caption, role):
        # A stock QMessageBox button also has a stock minimumSizeHint. Use
        # the same preferred-not-minimum action widget as every app dialog.
        button = NativeActionButton(caption, self)
        self._set_message_content(super().addButton,button,role)
        self._sync_primary_action()
        return button

    def setDefaultButton(self, button):
        super().setDefaultButton(button)
        self._sync_primary_action()

    def removeButton(self, button):
        self._set_message_content(super().removeButton,button)
        set_button_primary(button,False)
        self._sync_primary_action()

    def _set_message_content(self, setter, *values):
        # QMessageBox setters call private updateSize directly,not only via
        # LayoutRequest. Content updates must not replace an existing view.
        geometry=self.geometry() if self._native_shown else None
        result=setter(*values)
        if geometry is not None:
            self.setMinimumSize(0,0);self.setMaximumSize((1<<24)-1,(1<<24)-1)
            self._sync_message_presentation();self.setGeometry(geometry);self.layout().activate()
        return result

    def setText(self, text):self._set_message_content(super().setText,text)
    def setInformativeText(self, text):self._set_message_content(super().setInformativeText,text)
    def setIcon(self, icon):self._set_message_content(super().setIcon,icon)
    def setCheckBox(self, checkbox):self._set_message_content(super().setCheckBox,checkbox)

    def _sync_primary_action(self):
        # Accent completes task intent;Enter/Escape retain explicit safety.
        # A destructive task is still the visible command,not a Cancel action.
        buttons=self.buttons()
        affirmative=[b for b in buttons if self.buttonRole(b) in (QMessageBox.AcceptRole,QMessageBox.YesRole)]
        default=self.defaultButton()
        safe=[b for b in buttons if self.buttonRole(b) in (QMessageBox.RejectRole,QMessageBox.NoRole)]
        primary=next((b for b in affirmative if b is default),None)
        if primary is None:primary=next(iter(affirmative),None)
        for role in (QMessageBox.DestructiveRole,QMessageBox.ActionRole,QMessageBox.ApplyRole,QMessageBox.ResetRole):
            if primary is None:primary=next((b for b in buttons if self.buttonRole(b)==role),None)
        if primary is None:primary=next(iter(safe),None)
        if default is None:
            automatic=next(iter(safe),None) if primary is not None and self.buttonRole(primary)==QMessageBox.DestructiveRole else primary
            if automatic is not None:
                super().setDefaultButton(automatic)
                default=automatic
        for button in buttons:set_button_primary(button,button is primary,default=button is default)

    def _sync_message_presentation(self):
        for child in (self,*self.findChildren(QWidget)):
            apply_native_font_raster(child)
        root=self.layout()
        icon=self.findChild(QLabel,'qt_msgboxex_icon_label')
        has_icon=icon is not None and not icon.isHidden()
        for name in ('qt_msgbox_label','qt_msgbox_informativelabel'):
            label=self.findChild(QLabel,name)
            if label is None:continue
            label.setAlignment(Qt.AlignCenter);label.setIndent(0)
            label.setWordWrap(True);label.setMinimumWidth(0)
            # Changing horizontal compression must not erase QLabel's wrapped
            # height-for-width flag. The layout measures at the final width.
            policy=label.sizePolicy()
            policy.setHorizontalPolicy(QSizePolicy.Ignored)
            policy.setVerticalPolicy(QSizePolicy.Preferred)
            policy.setHeightForWidth(True)
            if label.sizePolicy()!=policy:label.setSizePolicy(policy)
            index=root.indexOf(label)
            if index>=0:
                row,column,rows,columns=root.getItemPosition(index)
                target_column=1 if has_icon else 0
                target_span=max(1,root.columnCount()-target_column)
                if column!=target_column or columns!=target_span:
                    root.removeWidget(label);root.addWidget(label,row,target_column,rows,target_span)
        if has_icon:
            body=self.findChild(QLabel,'qt_msgbox_label')
            index=root.indexOf(body) if body is not None else -1
            if index>=0:
                row,_column,rows,_columns=root.getItemPosition(index)
                icon_index=root.indexOf(icon)
                desired=(row,0,rows,1)
                if icon_index<0 or root.getItemPosition(icon_index)!=desired:
                    root.removeWidget(icon);root.addWidget(icon,*desired,Qt.AlignVCenter)
                elif root.itemAt(icon_index).alignment()!=Qt.AlignVCenter:
                    root.setAlignment(icon,Qt.AlignVCenter)
        for box in self.findChildren(QDialogButtonBox):
            box.setCenterButtons(True)
            if box.layout() is not None:
                box.layout().setContentsMargins(0,0,0,0)
                box.layout().setSpacing(METRICS['form_column_gap'])

    def _message_preferred_width(self):
        root=self.layout();margins=root.contentsMargins()
        icon=self.findChild(QLabel,'qt_msgboxex_icon_label')
        icon_width=icon.sizeHint().width()+root.horizontalSpacing() if icon is not None and not icon.isHidden() else 0
        available=max(1,self.screen().availableGeometry().width()-margins.left()-margins.right()-icon_width-
                      max(0,self.frameGeometry().width()-self.width()))
        body=0
        for name in ('qt_msgbox_label','qt_msgbox_informativelabel'):
            label=self.findChild(QLabel,name)
            if label is None or label.isHidden():continue
            # QSizeHint may already wrap long prose. Include glyph bearings
            # for short lines:horizontalAdvance alone is not an ink bound.
            metrics=QFontMetricsF(label.font())
            lines=label.text().splitlines() or ['']
            ink=max((metrics.boundingRect(line).width() for line in lines),default=0.)
            advance=max((metrics.horizontalAdvance(line) for line in lines),default=0.)
            natural=math.ceil(max(ink,advance))+2
            # A short main question fits its complete line when the work area
            # permits it. Qt's wrapped hint can otherwise unnecessarily break
            # its final glyph onto a new line. Longer detail retains wrapping.
            preferred=natural if name=='qt_msgbox_label' and natural<=available else min(available,label.sizeHint().width()+2)
            body=max(body,preferred)
        body+=icon_width
        commands=max((box.sizeHint().width() for box in self.findChildren(QDialogButtonBox)),default=0)
        return max(body,commands)+margins.left()+margins.right()

    def minimumSizeHint(self):
        return QSize(0, 0)

    def showEvent(self, event):
        # Qt's provisional measurement predates our shared presentation. Only
        # the final styled width may determine the opening content height.
        first=not self._native_shown
        previous_geometry=self.geometry() if not first else None
        super().showEvent(event)
        self._sync_primary_action()
        self._native_shown = True
        root=self.layout()
        root.setContentsMargins(*([METRICS['outer']]*4))
        root.setSpacing(METRICS['form_column_gap'])
        root.setSizeConstraint(QLayout.SetNoConstraint)
        self._sync_message_presentation()
        self.setMinimumSize(0, 0)
        self.setMaximumSize((1 << 24)-1, (1 << 24)-1)  # Qt QWIDGETSIZE_MAX.
        if first:
            # HFW remains declared on every wrapped label. The shared fitter
            # clamps the preferred width first,then queries that layout's HFW.
            # Never reuse Qt's provisional wrapped size hint or mounted width.
            fit_plain_dialog_contents(self,self._message_preferred_width(),opening=True)
        elif previous_geometry is not None:
            self.setGeometry(previous_geometry);root.activate()

    def event(self, event):
        if (event.type()==QEvent.LayoutRequest and getattr(self,'_native_shown',False)
                and self.isVisible()):
            # QMessageBox's private updateSize would reinstall its fixed size
            # on every layout request. This static message already has its
            # preferred initial measurement; only lay out inside the user's size.
            self._sync_message_presentation();self.layout().activate()
            return True
        return super().event(event)

    @classmethod
    def _acknowledge(cls, parent, title, message, icon):
        box = cls(parent)
        box.setWindowTitle(title)
        box.setText(str(message))
        box.setIcon(icon)
        confirm = box.addButton(tr('common.confirm'), QMessageBox.AcceptRole)
        box.setDefaultButton(confirm)
        box.setEscapeButton(confirm)
        try:
            box.exec()
            return QMessageBox.Ok
        finally:
            box.deleteLater()

    @classmethod
    def warning(cls, parent, title, message):
        return cls._acknowledge(parent, title, message, QMessageBox.Warning)

    @classmethod
    def critical(cls, parent, title, message):
        return cls._acknowledge(parent, title, message, QMessageBox.Critical)

    @classmethod
    def information(cls, parent, title, message):
        return cls._acknowledge(parent, title, message, QMessageBox.Information)


class _NativeIndicatorPresentation:
    """Fixed caption plus a theme-aware activity marker; no animated geometry."""
    def __init__(self, caption, help_text, parent=None, *, color_role='warning', state_namespace='indicator_lamps', graphic=None, show_marker=True, graphic_max_height=None):
        super().__init__(parent=parent)
        self.color_role = color_role
        self.state_namespace = state_namespace
        self.active = False
        self.severity = 0
        self.strength = 0.0
        self.caption = caption
        self.graphic = QPainterPath(graphic) if graphic is not None else None
        self.show_marker = bool(show_marker)
        self.graphic_max_height = graphic_max_height
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip(help_text)
        self.setAccessibleDescription(help_text)
        self.set_active(False, force=True)

    def sizeHint(self):
        return QSize(0, max(METRICS["state_caption_height"]+2,
            *(QFontMetrics(self.caption_font(overlay=layer)).height()+2 for layer in (False,True))))

    def caption_font(self, *, overlay=False):
        font = native_text_font(METRICS["small_font_px"], UI_BOLD_WEIGHT if overlay else UI_REGULAR_WEIGHT)
        font.setPointSizeF(METRICS["small_font_px"]*72/self.logicalDpiY()+2)
        return font

    def minimumSizeHint(self):
        return QSize(0, 0)

    def set_active(self, active, *, severity=1, strength=1.0, force=False):
        level=int(severity) if active else 0
        strength=float(strength) if active else 0.0
        if force or bool(active) != self.active or level!=self.severity or abs(strength-self.strength)>.002:
            semantic_change=force or bool(active)!=self.active or level!=self.severity
            self.active = bool(active)
            self.severity=level
            self.strength=strength
            if semantic_change:
                self.setAccessibleName(tr(self.state_namespace+('.active' if active else '.inactive'),name=self.caption))
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self._paint_indicator(painter)

    def _paint_indicator(self,painter):
        p = widget_palette(self)
        background, muted = QColor(p.surface), QColor(p.muted)
        off = QColor(*(round(background.getRgb()[i]*.70+muted.getRgb()[i]*.30) for i in range(3)))
        fonts=(self.caption_font(),self.caption_font(overlay=True))
        metrics=tuple(QFontMetrics(font) for font in fonts)
        marker_width=12 if self.show_marker else 0
        bounds=self.graphic.boundingRect() if self.graphic is not None else QRectF()
        scale=max(0,min((self.height()-2)/bounds.height(),(self.width()-marker_width)/bounds.width())) if not bounds.isEmpty() else 0
        if self.graphic_max_height is not None and not bounds.isEmpty():scale=min(scale,self.graphic_max_height/bounds.height())
        width=bounds.width()*scale if self.graphic is not None else max(metric.horizontalAdvance(self.caption) for metric in metrics)
        left=(self.width()-width-marker_width)/2
        baseline=(self.height()-max(metric.height() for metric in metrics))/2+max(metric.ascent() for metric in metrics)
        # Keep the off layer intact. Neither its weight nor placement depends
        # on activation; the coloured bold layer fades over the same baseline.
        for index,font in enumerate(fonts):
            if index and self.strength<=0:break
            painter.setOpacity(self.strength if index else 1.0)
            active_color=p.accent if self.color_role=='accent' else (p.danger if self.severity>=2 else p.warning)
            painter.setPen(QColor(active_color) if index else off)
            painter.setBrush(painter.pen().color() if index else Qt.NoBrush)
            painter.setFont(font)
            if self.show_marker:painter.drawEllipse(QPointF(left+3,self.height()/2),3,3)
            if self.graphic is not None:
                # The source emblem keeps its proportions and geometry in
                # both layers. Activation changes colour/opacity,not shape.
                painter.save()
                painter.translate(left+marker_width-bounds.left()*scale,(self.height()-bounds.height()*scale)/2-bounds.top()*scale)
                painter.scale(scale,scale)
                painter.fillPath(self.graphic,painter.pen().color())
                painter.restore()
            else:
                advance=metrics[index].horizontalAdvance(self.caption)
                painter.drawText(QPointF(left+marker_width+(width-advance)/2,baseline),self.caption)


class NativeIndicatorLamp(_NativeIndicatorPresentation,QWidget):
    """Passive status uses the shared indicator renderer, not a command."""


class NativeMidiModeButton(_NativeIndicatorPresentation,NativeActionButton):
    """Standard command input plus the unchanged actual-mode lamp layers."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.setObjectName('MidiModeGraphic')
        prepare_flat_command(self,quiet=True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumHeight(0);self.setMaximumHeight(16777215)
        self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)

    def set_active(self,active,**kwargs):
        super().set_active(active,**kwargs)
        self.setAccessibleName(tr('indicator_lamps.active' if active else 'indicator_lamps.inactive',
            name=tr('midi_mode.action',mode=self.caption)))

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.Antialiasing)
        paint_flat_command_feedback(painter,self,widget_palette(self))
        self._paint_indicator(painter)


class NativeWarningLamp(NativeIndicatorLamp):
    """Warning semantics use the shared read-only indicator presentation."""
    def __init__(self,key,parent=None):
        self.key=key
        super().__init__(tr('warning_lamps.'+key),tr('warning_lamps.help_'+key),parent,state_namespace='warning_lamps')


@lru_cache(maxsize=1)
def midi_mode_logo_paths():
    """Original line/Bezier outlines;bounded once-only read,never paint/audio.

    Official source hashes/page/path selection are retained in the data and
    licenses/midi-mode-logos. No pixel tracing,square remapping or curve
    flattening is used. The offline exporter is not a product dependency.
    """
    raw=json.loads((Path(__file__).resolve().parent/'assets/midi-mode-logos.json').read_text(encoding='utf-8'))
    if not isinstance(raw,dict) or raw.get('schema')!=1 or not isinstance(raw.get('logos'),dict):raise ValueError('invalid MIDI vector schema')
    if set(raw['logos'])!={'gm1','gm2','gs','xg'}:raise ValueError('missing MIDI vector')
    result={}
    for key,row in raw['logos'].items():
        if not isinstance(row,dict) or not isinstance(row.get('commands'),list) or not 1<=len(row['commands'])<=2048:raise ValueError('invalid MIDI vector commands')
        path=QPainterPath();path.setFillRule(Qt.WindingFill)
        for index,command in enumerate(row['commands']):
            if not isinstance(command,list) or not command or type(command[0]) is not str:raise ValueError('invalid MIDI vector command')
            kind=command[0]
            if kind not in ('M','L','C') or len(command)!=(7 if kind=='C' else 3) or (index==0 and kind!='M'):raise ValueError('invalid MIDI vector operation')
            if not all(type(v) in (int,float) and math.isfinite(v) and abs(v)<=100000 for v in command[1:]):raise ValueError('invalid MIDI vector coordinate')
            if kind=='M':path.moveTo(*command[1:])
            elif kind=='L':path.lineTo(*command[1:])
            else:path.cubicTo(*command[1:])
        bounds=path.boundingRect();declared=row.get('bounds')
        if not isinstance(declared,list) or len(declared)!=2 or not all(type(v) in (int,float) and math.isfinite(v) and v>0 for v in declared):raise ValueError('invalid MIDI vector bounds')
        if bounds.isEmpty() or max(abs(bounds.width()-declared[0]),abs(bounds.height()-declared[1]))>1e-4:raise ValueError('MIDI vector bounds mismatch')
        result[key]=path
    return result


class NativeMidiModeIndicator(QWidget):
    """Fixed mode lamps; only the actual processed native mode stays lit."""
    MODE_KEYS={0:'default',1:'gm1',2:'gm2',3:'xg',4:'gs',5:'gs'}
    DISPLAY_ORDER=(1,2,4,3)
    modeRequested=Signal(int)

    def __init__(self,parent=None):
        super().__init__(parent)
        self.mode=None
        self.setMinimumSize(0,0)
        self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        self.setFocusPolicy(Qt.NoFocus)
        row=QHBoxLayout(self)
        row.setContentsMargins(0,0,0,0)
        row.setSpacing(METRICS['state_column_gap'])
        self.indicators={}
        try:graphics=midi_mode_logo_paths()
        except (OSError,ValueError):
            # A damaged visual resource must not prevent MIDI/audio startup.
            graphics={}
            logging.getLogger(__name__).warning(tr('midi_mode.artwork_unavailable'))
        for mode in self.DISPLAY_ORDER:
            key=self.MODE_KEYS[mode]
            if self.indicators:row.addStretch(1)
            lamp=NativeMidiModeButton(tr('midi_mode.name_'+key),tr('midi_mode.action_help',mode=tr('midi_mode.name_'+key),description=tr('midi_mode.help_'+key)),self,color_role='accent',graphic=graphics.get(key),show_marker=False,graphic_max_height=METRICS['midi_mode_logo_max_height'])
            lamp.clicked.connect(lambda _checked=False,value=mode:self.request_mode(value))
            bounds=lamp.graphic.boundingRect() if lamp.graphic is not None else QRectF(0,0,1,1)
            if lamp.graphic is not None:lamp.setMaximumWidth(math.ceil(METRICS['midi_mode_logo_max_height']*bounds.width()/bounds.height()))
            row.addWidget(lamp,max(1,round(1000*bounds.width()/bounds.height())))
            self.indicators[mode]=lamp
        self.set_mode(None,force=True)

    def sizeHint(self):
        return QSize(0,max(lamp.sizeHint().height() for lamp in self.indicators.values()))

    def minimumSizeHint(self):return QSize(0,0)

    def request_mode(self,mode):
        """Active observed face -> real Default,never a local lamp toggle."""
        displayed=4 if self.mode==5 else self.mode
        self.modeRequested.emit(0 if mode==displayed else mode)

    def set_mode(self,mode,*,force=False):
        mode=mode if type(mode) is int and mode in self.MODE_KEYS else None
        if not force and self.mode==mode:return
        self.mode=mode
        key=self.MODE_KEYS.get(mode,'unknown')
        self.caption=tr('midi_mode.name_'+key)
        help_text=tr('midi_mode.help_'+key)
        self.setToolTip(help_text)
        self.setAccessibleName(tr('midi_mode.current',mode=self.caption))
        self.setAccessibleDescription(help_text)
        # Presentation groups both GS variants. Never change the actual
        # BASSMIDI system state to achieve this visual grouping.
        displayed_mode=4 if mode==5 else mode
        for value,lamp in self.indicators.items():
            lamp.set_active(value==displayed_mode,force=force)

    def set_theme(self,dark):
        for lamp in self.indicators.values():lamp.update()


class NativeWarningLights(QWidget):
    """Stable diagnostic positions with a semantic summary for accessibility."""
    KEYS = ("dispatch", "supply", "deadline", "timing",
            "overload", "clip", "transport", "stalled")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._summary = ""
        self._pulses={key:PlaybackLampPulse() for key in self.KEYS}
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(METRICS["state_column_gap"])
        grid.setVerticalSpacing(2)
        self.indicators = {}
        for index, key in enumerate(self.KEYS):
            lamp = NativeWarningLamp(key, self)
            grid.addWidget(lamp, index//4, (index%4)*2)
            grid.setColumnStretch((index%4)*2, 1)
            self.indicators[key] = lamp
        self.dividers=[]
        for column in (1,3,5,7):
            divider=QFrame(self);divider.setObjectName('StateDivider')
            divider.setFrameShape(QFrame.NoFrame);divider.setFixedWidth(1)
            grid.addWidget(divider,0,column,2,1);self.dividers.append(divider)
        self.mode_indicator=NativeMidiModeIndicator(self)
        grid.addWidget(self.mode_indicator,0,8)
        self.inferred_row=QWidget(self)
        self.inferred_row.setMinimumWidth(0)
        self.inferred_row.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        row=QHBoxLayout(self.inferred_row)
        row.setContentsMargins(0,0,0,0)
        row.setSpacing(METRICS['state_column_gap'])
        row.setSizeConstraint(QLayout.SetNoConstraint)
        self.chord=AdaptiveValueLabel()
        self.tempo=ElasticLabel()
        for label,name,help_key in ((self.chord,'chord_display.name','chord_display.help'),
                                    (self.tempo,'tempo_display.name','tempo_display.help')):
            label.setMinimumWidth(0)
            label.setTextFormat(Qt.PlainText)
            label.setFont(native_text_font(METRICS['small_font_px']))
            label.setFocusPolicy(Qt.NoFocus)
            label.setAccessibleName(tr(name))
            label.setToolTip(tr(help_key))
            label.setAccessibleDescription(tr(help_key))
        self.chord.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        self.tempo.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Preferred)
        self.chord.setAlignment(Qt.AlignLeft|Qt.AlignVCenter)
        self.tempo.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
        row.addWidget(self.chord,1)
        row.addWidget(self.tempo)
        self.tempo.installEventFilter(self)
        self._chord_text=self._tempo_text=tr('inference_display.unknown')
        grid.addWidget(self.inferred_row,1,8)
        self.set_chord(None)
        self.set_tempo(None)
        grid.setColumnStretch(8,1)

    def set_chord(self,reading):
        self._chord_text=(reading.text if reading is not None else '') or tr('inference_display.unknown')
        self._refresh_inference()

    def set_tempo(self,bpm):
        self._tempo_text=f'{bpm:.0f}' if type(bpm) in (int,float) and math.isfinite(bpm) and bpm>0 else tr('inference_display.unknown')
        self._refresh_inference()

    def _refresh_inference(self):
        if self.chord.text()!=self._chord_text:self.chord.setText(self._chord_text)
        text=tr('tempo_display.value',value=self._tempo_text)
        if self.tempo.text()!=text:self.tempo.setText(text)
        self._sync_tempo_width()

    def _sync_tempo_width(self):
        metrics=QFontMetricsF(self.tempo.font())
        # Measured three-digit preference,not a width floor. Include >999
        # defensively rather than truncate a future estimator output.
        widest=max('0123456789',key=metrics.horizontalAdvance)
        template=tr('tempo_display.value',value=widest*3)
        self.tempo.set_preferred_width(math.ceil(max(metrics.horizontalAdvance(template),
            metrics.horizontalAdvance(self.tempo.text())))+2)

    def eventFilter(self,watched,event):
        if watched is getattr(self,'tempo',None) and event.type() in (QEvent.FontChange,QEvent.StyleChange):
            self._sync_tempo_width()
        return super().eventFilter(watched,event)

    def text(self):
        return self._summary

    def setText(self, text):
        self._summary = str(text)
        self.setAccessibleName(self._summary or tr("warning_lamps.panel"))

    def reset_severity(self):
        self._pulses={key:PlaybackLampPulse() for key in self.KEYS}
        for lamp in self.indicators.values():lamp.set_active(False)

    def present(self, *, events=None, states=None, now=None):
        now=time.monotonic() if now is None else now
        states={} if states is None else states
        for key, pulse in self._pulses.items():
            level=states.get(key,0)
            pulse.set_condition(level,now,magnitude=key=='overload')
            if not level:
                for _ in range(min(2,(events or {}).get(key,0))):pulse.trigger(now)
        self.advance_frame(now)

    def set_condition(self, key, level, now=None):
        now=time.monotonic() if now is None else now
        self._pulses[key].set_condition(level,now,magnitude=key=='overload')
        self.advance_frame(now)

    def advance_frame(self, now=None):
        now=time.monotonic() if now is None else now
        for key, lamp in self.indicators.items():
            pulse=self._pulses[key];strength=pulse.strength(now)
            lamp.set_active(strength>0,severity=pulse.severity,strength=strength)
        return tuple(key for key,lamp in self.indicators.items() if lamp.active)

    def set_theme(self, dark):
        for lamp in self.indicators.values():
            lamp.update()
        self.mode_indicator.set_theme(dark)


class NativeMeter(QWidget):
    """Compact professional stereo meter: dB scale, peak hold and headroom."""

    SCALE_MIN_DB = -60.0
    SCALE_MAX_DB = 3.0
    TICK_MARKS = (3, 0, *range(-6, -61, -6))
    DIGITAL_SILENCE_DBFS = -120.0
    SILENCE_READOUT = "\u2212\u221e"
    FRAME_INTERVAL_MS = 16
    LEVEL_RELEASE_DB_PER_SECOND = 30.0
    PEAK_HOLD_SECONDS = 1.0
    PEAK_RELEASE_DB_PER_SECOND = 24.0
    CLIP_HOLD_SECONDS = 3.0

    def __init__(self, parent=None):
        super().__init__(parent)
        # Readable opening extent is a preference, not a resize floor.
        self.setMinimumWidth(0)
        self.setMinimumHeight(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self._levels = (self.SCALE_MIN_DB, self.SCALE_MIN_DB)  # scale-clamped bars
        self._readouts = (float("-inf"), float("-inf"))  # held, raw dBFS peak values
        self._peaks = [self.SCALE_MIN_DB, self.SCALE_MIN_DB]
        self._peak_hold_until = [0.0, 0.0]
        self._clip_hold_until = [0.0, 0.0]
        self._last_level_update = time.monotonic()
        self._level_appearance = None
        self._dark = False
        self._grid_picture = None
        self._grid_picture_key = None

    @staticmethod
    def tick_spacing():
        return max(METRICS["meter_tick_label_height"],
                   QFontMetrics(native_text_font(METRICS["meter_tick_font_px"])).height()) + CARD_ROW_GAPS["effects"]

    def sizeHint(self):
        track = math.ceil((self.SCALE_MAX_DB - self.SCALE_MIN_DB) / 6 * self.tick_spacing())
        chrome = (METRICS["meter_readout_top"] + METRICS["meter_value_chip_height"]
                  + METRICS["meter_readout_to_label_gap"] + METRICS["meter_label_height"]
                  + METRICS["meter_label_to_track_gap"] + METRICS["meter_track_to_footer_gap"]
                  + METRICS["meter_footer_height"])
        return QSize(0, track + chrome)

    def minimumSizeHint(self):
        return QSize(0, 0)

    def visible_tick_labels(self, track_height):
        """Thin captions, never the scale/grid or measured signal values."""
        pixels_per_db = max(0, track_height) / (self.SCALE_MAX_DB - self.SCALE_MIN_DB)
        gap = self.tick_spacing()
        labels = [0]
        for mark in self.TICK_MARKS:
            if mark < 0 and (labels[-1] - mark) * pixels_per_db >= gap - 1e-6:
                labels.append(mark)
        if labels[-1] != self.SCALE_MIN_DB and -self.SCALE_MIN_DB * pixels_per_db >= gap:
            if len(labels) > 1 and (labels[-1] - self.SCALE_MIN_DB) * pixels_per_db < gap:
                labels.pop()
            labels.append(int(self.SCALE_MIN_DB))
        if self.SCALE_MAX_DB * pixels_per_db >= gap:
            labels.insert(0, int(self.SCALE_MAX_DB))
        return tuple(labels)

    def set_theme(self, dark: bool):
        self._dark = bool(dark)
        self.update()

    set_dark = set_theme

    @classmethod
    def _to_dbfs(cls, value: float) -> float:
        value = float(value)
        return float("-inf") if value <= 0.0 else 20.0 * math.log10(value)

    @classmethod
    def _scale_db(cls, value: float) -> float:
        return max(cls.SCALE_MIN_DB, min(cls.SCALE_MAX_DB, value))

    @classmethod
    def _is_digital_silence(cls, value: float) -> bool:
        """Classify float residue below the documented UI silence floor."""
        return not math.isfinite(value) or value <= cls.DIGITAL_SILENCE_DBFS

    @staticmethod
    def _format_readout(value: float) -> str:
        if not math.isfinite(value):
            return NativeMeter.SILENCE_READOUT
        # Keep the actual measured magnitude. Whole dB formatting below -99
        # keeps the unbounded numeric value readable in its narrow chip.
        return f"{value:.0f}" if value <= -99.95 else f"{value:+.1f}"

    def set_levels(self, left: float, right: float):
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_level_update)
        self._last_level_update = now
        raw_levels = tuple(self._to_dbfs(value) for value in (left, right))
        displayed = []
        held_readouts = list(self._readouts)
        for index, raw in enumerate(raw_levels):
            audible = not self._is_digital_silence(raw)
            target = self._scale_db(raw) if audible else self.SCALE_MIN_DB
            current = self._levels[index]
            # Fast attack preserves transients; release is time based, so a
            # faster UI timer does not change meter ballistics.
            level = target if target >= current else max(target, current - self.LEVEL_RELEASE_DB_PER_SECOND * elapsed)
            displayed.append(level)
            if audible and raw > 0.0:
                self._clip_hold_until[index] = now + self.CLIP_HOLD_SECONDS
            previous_peak = held_readouts[index]
            if audible and (self._is_digital_silence(previous_peak) or raw >= previous_peak):
                peak = raw
                self._peak_hold_until[index] = now + self.PEAK_HOLD_SECONDS
            elif now > self._peak_hold_until[index]:
                falling = previous_peak - self.PEAK_RELEASE_DB_PER_SECOND * elapsed
                peak = max(raw, falling) if audible else falling
                if self._is_digital_silence(peak):
                    peak = float("-inf")
            else:
                peak = previous_peak
            held_readouts[index] = peak
            self._peaks[index] = self._scale_db(peak) if not self._is_digital_silence(peak) else self.SCALE_MIN_DB
        self._readouts = tuple(held_readouts)
        self._levels = tuple(displayed)
        appearance=(self._levels,tuple(self._peaks),self._readouts,
                    tuple(now<until for until in self._clip_hold_until))
        if appearance!=self._level_appearance:
            self._level_appearance=appearance
            self.update()

    def paintEvent(self, _event):
        palette = widget_palette(self, self._dark)
        p = palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # The card is the meter's only canvas. A second full-size background
        # made the monitor look like an unnecessary nested panel.
        # The readout, L/R labels and dBFS caption use one continuously
        # compressed header/footer system. There is no height breakpoint that
        # abruptly changes the meter at a specific pixel count.
        readout_y = METRICS["meter_readout_top"]
        label_y = readout_y + METRICS["meter_value_chip_height"] + METRICS["meter_readout_to_label_gap"]
        track_top = label_y + METRICS["meter_label_height"] + METRICS["meter_label_to_track_gap"]
        footer_height = METRICS["meter_footer_height"]
        track_bottom = max(track_top, self.height() - footer_height - METRICS["meter_track_to_footer_gap"])
        scale_gutter = METRICS["meter_scale_gutter"]
        bounds = QRect(
            scale_gutter, track_top,
            max(0, self.width() - scale_gutter - METRICS["meter_right_gutter"]),
            max(0, track_bottom - track_top),
        )
        lane_gap = min(
            METRICS["meter_lane_gap"],
            max(0, bounds.width() // METRICS["meter_lane_gap_divisor"]),
        )
        lane_width = max(0, (bounds.width() - lane_gap) // 2)
        font = native_text_font(METRICS["meter_tick_font_px"])
        painter.setFont(font)
        painter.setPen(QColor(palette.muted))
        def y_for(db): return bounds.bottom() - int(bounds.height() * (self._scale_db(db) - self.SCALE_MIN_DB) / (self.SCALE_MAX_DB - self.SCALE_MIN_DB))
        # Replay unchanged captions in one native call. QPicture retains the
        # original text/drawing commands, not a rasterized alternative font.
        # Geometry/theme/DPI changes invalidate this single bounded cache.
        grid_key = (bounds.x(), bounds.y(), bounds.width(), bounds.height(),
                    self.devicePixelRatioF(), self.logicalDpiX(), self.logicalDpiY(), p, font.toString())
        # Keep integer grid lines on the original paint device: QPicture
        # serializes them through a different raster path at fractional DPR.
        painter.setPen(QPen(QColor(p.meter_grid), 1))
        for mark in self.TICK_MARKS:
            y = y_for(mark)
            painter.drawLine(bounds.left() - 1, y, bounds.right(), y)
        if grid_key != self._grid_picture_key:
            picture = QPicture()
            grid = QPainter(picture)
            grid.setRenderHint(QPainter.Antialiasing); grid.setFont(font)
            visible_labels = self.visible_tick_labels(bounds.height())
            for mark in self.TICK_MARKS:
                y = y_for(mark)
                tick_height = METRICS["meter_tick_label_height"]
                if mark in visible_labels:
                    grid.setPen(QColor(p.label)); grid.drawText(
                        QRect(0, y - tick_height // 2, METRICS["meter_tick_label_width"], tick_height),
                        Qt.AlignRight | Qt.AlignVCenter, f"+{mark}" if mark > 0 else str(mark))
            grid.end()
            self._grid_picture, self._grid_picture_key = picture, grid_key
        painter.drawPicture(0, 0, self._grid_picture)
        for index, label in enumerate(("L", "R")):
            x = bounds.left() + index * (lane_width + lane_gap)
            lane = QRect(x, bounds.top(), lane_width, bounds.height())
            value = self._levels[index]
            raw_value = self._readouts[index]
            value_text = self._format_readout(raw_value)
            # Readout boxes always fit inside their respective lanes.  They
            # become narrower with the rail rather than overlapping each
            # other at a small width.
            chip_width = min(
                METRICS["meter_value_chip_width"],
                max(0, lane_width - 2 * METRICS["meter_readout_side_inset"]),
            )
            chip = QRect(x + lane_width // 2 - chip_width // 2, readout_y, chip_width, METRICS["meter_value_chip_height"])
            overload = time.monotonic() < self._clip_hold_until[index]
            # Values are readouts, not filled controls.  Their normal state is
            # transparent with a crisp outline; only an over-0 dBFS value
            # gains the solid red warning background.
            if chip_width > 0:
                painter.setPen(QPen(QColor(p.danger if overload else p.border), 1))
                painter.setBrush(QColor(p.danger) if overload else Qt.NoBrush)
                painter.drawRoundedRect(chip, 3, 3)
            readout_font = native_text_font(METRICS["meter_value_font_px"]); painter.setFont(readout_font)
            painter.setPen(QColor("#ffffff") if overload else QColor(p.text))
            if chip_width > 0:
                painter.drawText(chip, Qt.AlignCenter, value_text)
            painter.setPen(Qt.NoPen)
            painter.setRenderHint(QPainter.Antialiasing, False)
            painter.fillRect(lane, QColor(palette.surface))
            for low, high, color in ((-60, -12, p.meter_safe), (-12, -3, p.meter_warning), (-3, 3, p.meter_danger)):
                band_top = max(lane.top(), y_for(high))
                bottom = lane.bottom() + 1 if low == self.SCALE_MIN_DB else y_for(low)
                tint = QColor(color)
                tint.setAlphaF(0.14 if self._dark else 0.10)
                painter.fillRect(QRect(x, band_top, lane_width, max(0, bottom-band_top)), tint)
                level = self._levels[index]
                if level > low:
                    visible_top = min(level, high)
                    top = max(lane.top(), y_for(visible_top))
                    inset = METRICS["meter_signal_inset"]
                    if lane_width > 2 * inset:
                        painter.fillRect(QRect(x + inset, top, lane_width - 2 * inset,
                                               max(0, bottom - top)), QColor(color))
            peak_y = y_for(self._peaks[index])
            painter.setPen(QPen(QColor(p.danger if overload else p.text), 1))
            inset = METRICS["meter_signal_inset"]
            if lane_width > 2 * inset:
                painter.drawLine(x + inset, peak_y, x + lane_width - inset - 1, peak_y)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(QColor(p.danger if overload else palette.muted))
            header_font = native_text_font(METRICS["meter_channel_font_px"], UI_BOLD_WEIGHT); painter.setFont(header_font)
            painter.drawText(QRect(x, label_y, lane_width, METRICS["meter_label_height"]), Qt.AlignCenter, label)
            painter.setFont(font)
        painter.setPen(QColor(palette.muted))
        # The unit belongs to the stereo pair, not to either individual lane
        # or the whole monitor column.  Anchor it at the physical lane gap.
        pair_center = bounds.left() + lane_width + lane_gap // 2
        unit_width = METRICS["meter_unit_label_width"]
        painter.drawText(
            QRect(pair_center - unit_width // 2, self.height() - footer_height, unit_width, footer_height),
            Qt.AlignCenter, "dBFS",
        )


class NativeEQBandHandle(QPushButton):
    """Standard accessible band selector over an already-painted chart point.

    QObject/Qt owns provider lifetime and Invoke; the parent retains ordinary
    mouse hit/drag/reset. No layout hint or scalar RangeValue is introduced.
    """
    def __init__(self,graph,index):
        super().__init__('',graph)
        self._graph=weakref.ref(graph);self.index=index
        self.setObjectName('EQBandHandle')
        self.setMinimumSize(0,0);self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Ignored)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.StrongFocus)
        self.clicked.connect(self.select_band)

    def minimumSizeHint(self):return QSize(0,0)

    def event(self,event):
        update_native_keyboard_focus(self,event)
        return super().event(event)

    def select_band(self):
        graph=self._graph()
        if graph is not None and shiboken6.isValid(graph) and self.isEnabled() and self.index<len(graph._bands):
            # Transparent to mouse input: Invoke/keyboard activation is a
            # non-pointer target and must retain a visible focus indication.
            self._native_keyboard_focus=True;self.update()
            # Invoke selects the keyboard editing target too. Otherwise a
            # pending native window activation can restore the old point's
            # focus and undo the just-invoked selection.
            if not self.hasFocus():self.setFocus(Qt.OtherFocusReason)
            if graph._selected!=self.index:graph.bandSelected.emit(self.index)

    def focusInEvent(self,event):
        super().focusInEvent(event)
        # Focus alone must not activate a Button or restore an old selection.
        # Invoke/Space/shared arrows explicitly select their editing target.
        self.update()

    def focusOutEvent(self,event):
        super().focusOutEvent(event);self.update()

    def keyPressEvent(self,event):
        graph=self._graph()
        if graph is not None and self.isEnabled():
            if event.key() in (Qt.Key_Left,Qt.Key_Right,Qt.Key_Up,Qt.Key_Down,Qt.Key_Delete,
                               Qt.Key_PageUp,Qt.Key_PageDown):
                self.select_band()
                if graph._navigate_band(event.key()):
                    if event.key() in (Qt.Key_PageUp,Qt.Key_PageDown) and 0<=graph._selected<len(graph._band_handles):
                        graph._band_handles[graph._selected].setFocus(Qt.TabFocusReason)
                    event.accept();return
        super().keyPressEvent(event)

    def paintEvent(self,event):
        if native_keyboard_focus_visible(self):
            painter=QPainter(self);painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QPen(QColor(widget_palette(self).accent),1.5));painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QRectF(self.rect()).adjusted(1.5,1.5,-1.5,-1.5))


class NativeEQResponse(QWidget):
    """Measured EQ curve; owns presentation only, never a BASS resource."""

    bandSelected = Signal(int)
    bandAdded = Signal(float, float)
    bandRemoved = Signal(int)
    bandEdited = Signal(int, float, float)
    PREFERRED_HEIGHT = 144

    def __init__(self, parent=None):
        _install_numeric_accessibility()
        super().__init__(parent)
        self._dark = False
        self._response = None
        self._bands = ()
        self._selected = 0
        self._bypassed = False
        self._points = []
        self._band_handles=[]
        self._handle_geometry_key=None
        self._center_range = (80, 16000)
        self._drag_start = None
        self._hovered_band = -1
        self._limit_db = 18
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(tr("eq.graph_name"))
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._base_description = effect_tooltip("eq_graph")
        install_effect_tooltip(self._base_description, self)
        set_native_cursor(self,Qt.ArrowCursor)

    def sizeHint(self): return QSize(400, self.PREFERRED_HEIGHT)
    def minimumSizeHint(self): return QSize(0, 0)

    def event(self,event):
        update_native_keyboard_focus(self,event)
        return super().event(event)

    def set_theme(self, dark):
        self._dark = bool(dark)
        self.update()

    def set_response(self, response, bands, selected, bypassed):
        if len(self._bands)!=len(bands):self.cancel_pointer_gesture()
        changed = (self._bands, self._selected, self._bypassed) != (bands, selected, bypassed)
        self._response, self._bands = response, bands
        self._selected, self._bypassed = selected, bypassed
        while len(self._band_handles)>len(bands):
            retired=self._band_handles.pop();retired.hide();retired.deleteLater()
        while len(self._band_handles)<len(bands):
            self._band_handles.append(NativeEQBandHandle(self,len(self._band_handles)))
        if changed:
            for index,handle in enumerate(self._band_handles):
                handle.setAccessibleName(tr('eq.chart_point_name',index=index+1,filter=tr(f'eq.filter_{bands[index].kind}')))
                description=self._band_status(index)
                handle.setAccessibleDescription(description);handle.setWhatsThis(description)
            if 0 <= selected < len(bands):
                status = self._band_status(selected)
            else:
                status = tr('eq.chart_no_selection', count=len(bands))
            install_effect_tooltip(tr('eq.chart_description', meaning=self._base_description, status=status), self)
            _notify_numeric_accessibility(self)
        self._position_band_handles()
        if changed:
            refresh_native_pointer_feedback(self)
        self.update()

    def _band_status(self,index):
        band=self._bands[index]
        return tr('eq.chart_selected_no_gain' if band.kind in ('high_pass','low_pass') else 'eq.chart_selected',
            index=index+1,count=len(self._bands),filter=tr(f'eq.filter_{band.kind}'),
            frequency=format_effect_value(band.center_hz,'hz_decimal'),gain=format_effect_value(band.gain_db,'db_decimal'),
            state=tr('eq.chart_bypassed' if self._bypassed else 'eq.chart_active'))

    def _position_band_handles(self):
        key=(self.size(),self._bands,self._display_limit())
        if self._handle_geometry_key==key:return
        self._handle_geometry_key=key
        points=dict(self._band_points())
        for index,handle in enumerate(self._band_handles):
            point=points.get(index)
            if point is None:handle.hide();continue
            rect=QRect(round(point.x()-11),round(point.y()-11),22,22)
            if handle.geometry()!=rect:handle.setGeometry(rect)
            handle.show()

    def resizeEvent(self,event):
        super().resizeEvent(event);self._position_band_handles()

    def dragged_band(self):
        return self._drag_start[0] if self._drag_start is not None else -1

    def set_center_range(self, minimum, maximum):
        self._center_range = (minimum, maximum)

    def plot_rect(self):
        return QRectF(self.rect()).adjusted(34, 12, -12, -24)

    def _hit_band(self, position):
        # Hit testing cannot depend on whether Qt has repainted since a state
        # change or resize. The same current geometry drives drawing and input.
        hits = [(math.hypot(position.x() - point.x(), position.y() - point.y()), index)
                for index, point in self._band_points()]
        distance, index = min(hits) if hits else (float("inf"), -1)
        return index if distance <= 12 else -1

    def _display_limit(self):
        if self._drag_start is not None:
            return self._drag_start[-1]
        return (max(18, math.ceil(float(np.max(np.abs(self._response[1]))) / 6) * 6)
                if self._response is not None else 18)

    def _band_points(self):
        plot = self.plot_rect()
        if plot.width() <= 0 or plot.height() <= 0:
            return []
        limit = self._display_limit()
        return [(index, QPointF(
            plot.left() + math.log(max(20, band.center_hz) / 20) / math.log(1000) * plot.width(),
            plot.center().y() - (0 if band.kind in ("high_pass", "low_pass") else band.gain_db) / limit * plot.height() / 2,
        )) for index, band in enumerate(self._bands)]

    def _frequency_at(self, position):
        plot = self.plot_rect()
        fraction = max(0, min(1, (position.x() - plot.left()) / max(1, plot.width())))
        return max(self._center_range[0], min(self._center_range[1], 20 * 1000 ** fraction))

    def _gain_at(self, position):
        plot = self.plot_rect()
        return max(-15, min(15, (plot.center().y() - position.y()) * 2 * self._limit_db / max(1, plot.height())))

    def mousePressEvent(self, event):
        if self.plot_rect().contains(event.position()):
            self.reconcile_hover(event.position())
            index = self._hit_band(event.position())
            if event.button() == Qt.RightButton:
                self._drag_start = None
                self._hovered_band = -1
                self.bandRemoved.emit(index) if index >= 0 else self.bandAdded.emit(
                    self._frequency_at(event.position()), self._gain_at(event.position()))
                event.accept()
                return
            if event.button() == Qt.LeftButton:
                self.setFocus(Qt.MouseFocusReason)
                self.bandSelected.emit(index)
                if index >= 0:
                    band = self._bands[index]
                    self._drag_start = (index, event.position(), band.center_hz, band.gain_db, self._limit_db)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if (event.button() == Qt.LeftButton
                and self.plot_rect().contains(event.position())
                and self._hit_band(event.position()) < 0):
            self._drag_start = None
            self.setFocus(Qt.MouseFocusReason)
            self.bandAdded.emit(self._frequency_at(event.position()),
                                self._gain_at(event.position()))
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_start is not None and event.buttons() & Qt.LeftButton:
            self._edit_drag_position(event.position())
            event.accept()
            return
        self.reconcile_hover(event.position())
        super().mouseMoveEvent(event)

    def reconcile_hover(self, position):
        hovered = self._hit_band(position) if self.isEnabled() else -1
        if hovered != self._hovered_band:
            self._hovered_band = hovered
            self.update()
        set_native_cursor(self,Qt.PointingHandCursor if hovered >= 0 else Qt.ArrowCursor)

    def leaveEvent(self, event):
        self.reset_stale_hover()
        super().leaveEvent(event)

    def reset_stale_hover(self):
        if self._hovered_band >= 0:
            self._hovered_band = -1
            self.update()
        set_native_cursor(self,None)

    def cancel_pointer_gesture(self):
        # Keep already committed edits. Retire only the pointer operation so
        # an old release cannot edit a hidden/replaced band or emit a command.
        self._drag_start=None;self.update()

    def mouseReleaseEvent(self, event):
        if self._drag_start is not None and event.button() == Qt.LeftButton:
            # Qt can coalesce move events. The release position is still the
            # final authoritative pointer input, even without a last move.
            self._edit_drag_position(event.position())
        self._drag_start = None
        self.update()
        super().mouseReleaseEvent(event)

    def _edit_drag_position(self, position):
        index, origin, center, gain, scale = self._drag_start
        plot = self.plot_rect()
        dx, dy = position.x() - origin.x(), position.y() - origin.y()
        hz = center * 1000 ** (dx / max(1, plot.width()))
        db = gain if self._bands[index].kind in ("high_pass", "low_pass") else gain - dy * 2 * scale / max(1, plot.height())
        self.bandEdited.emit(index, max(self._center_range[0], min(self._center_range[1], hz)),
                             max(-15, min(15, db)))

    def _navigate_band(self, key):
        if not self.isEnabled(): return False
        if key in (Qt.Key_PageUp, Qt.Key_PageDown) and self._bands:
            index = min(len(self._bands)-1, max(0, self._selected + (1 if key == Qt.Key_PageDown else -1)))
            self.bandSelected.emit(index)
            return True
        if 0 <= self._selected < len(self._bands):
            if key in (Qt.Key_Delete, Qt.Key_Backspace):
                self.bandRemoved.emit(self._selected)
                return True
            band = self._bands[self._selected]
            steps = {Qt.Key_Left: (-100, 0), Qt.Key_Right: (100, 0),
                     Qt.Key_Down: (0, -1), Qt.Key_Up: (0, 1)}
            if key in steps:
                hz, db = steps[key]
                if db and band.kind in ('high_pass', 'low_pass'): return True
                self.bandEdited.emit(self._selected, max(self._center_range[0], min(self._center_range[1], band.center_hz + hz)),
                    band.gain_db if band.kind in ("high_pass", "low_pass") else max(-15, min(15, band.gain_db + db)))
                return True
        return False

    def keyPressEvent(self, event):
        if self._navigate_band(event.key()):
            event.accept(); return
        super().keyPressEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event); self.update()

    def focusOutEvent(self, event):
        self.cancel_pointer_gesture()
        super().focusOutEvent(event); self.update()

    def paintEvent(self, _event):
        p = widget_palette(self, self._dark)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if native_keyboard_focus_visible(self):
            painter.setPen(QPen(QColor(p.accent), 1, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(2, 2, -2, -2), 4, 4)
        painter.setFont(native_text_font(12))
        painter.setPen(QColor(p.muted))
        plot = self.plot_rect()
        self._points = []
        if plot.width() <= 0 or plot.height() <= 0:
            return
        maximum_hz = 20000
        def x(hz):
            return plot.left() + math.log(max(20, hz) / 20) / math.log(maximum_hz / 20) * plot.width()
        limit_db = self._display_limit()
        self._limit_db = limit_db
        def y(db): return plot.center().y() - db / limit_db * plot.height() / 2
        for db in (-limit_db, 0, limit_db):
            painter.setPen(QPen(QColor(p.border if db == 0 else p.border_subtle), 1))
            painter.drawLine(QPointF(plot.left(), y(db)), QPointF(plot.right(), y(db)))
            painter.setPen(QColor(p.muted))
            painter.drawText(QRectF(0, y(db) - 8, 29, 16), Qt.AlignRight | Qt.AlignVCenter,
                             f"{db:+g}".replace("-", "\u2212") if db else "0")
        for hz, label in ((20, "20"), (100, "100"), (1000, "1k"), (10000, "10k"), (20000, "20k")):
            if hz > maximum_hz:
                continue
            painter.setPen(QPen(QColor(p.border_subtle), 1))
            painter.drawLine(QPointF(x(hz), plot.top()), QPointF(x(hz), plot.bottom()))
            painter.setPen(QColor(p.muted))
            if hz == 20000:
                painter.drawText(QRectF(plot.right() - 40, plot.bottom() + 4, 40, 18), Qt.AlignRight, label)
            elif hz == 20:
                painter.drawText(QRectF(plot.left(), plot.bottom() + 4, 40, 18), Qt.AlignLeft, label)
            elif plot.width() > 240:
                painter.drawText(QRectF(x(hz) - 20, plot.bottom() + 4, 40, 18), Qt.AlignCenter, label)
        if self._response is None:
            painter.drawText(plot, Qt.AlignCenter, tr("eq.response_unavailable"))
        path = QPainterPath()
        for index, (hz, db) in enumerate(zip(*self._response) if self._response is not None else ()):
            point = QPointF(x(hz), y(db))
            path.moveTo(point) if index == 0 else path.lineTo(point)
        painter.save()
        painter.setClipRect(plot.adjusted(-5, -5, 5, 5))
        painter.setPen(QPen(QColor(p.muted if self._bypassed else p.accent), 1.5,
                            Qt.DashLine if self._bypassed else Qt.SolidLine))
        painter.drawPath(path)
        if self._bypassed and self._response is not None:
            painter.setPen(QPen(QColor(p.accent), 1.5))
            painter.drawLine(QPointF(plot.left(), y(0)), QPointF(x(min(maximum_hz, self._response[0][-1])), y(0)))
        self._points = self._band_points()
        for index, point in self._points:
            selected = index == self._selected
            hovered = self.isEnabled() and index == self._hovered_band
            painter.setPen(QPen(QColor(p.accent_hover if hovered else p.accent if selected else p.muted), 1.5))
            painter.setBrush(QColor(p.accent if selected else p.surface))
            painter.drawEllipse(point, 4.5 if selected else 3, 4.5 if selected else 3)
        painter.restore()


def _analyze_visual_spectrum(samples, sample_rate, bin_count, plan, size_limit, floor_db):
    """Worker-only numerical input; no QObject, BASS handle or mutable DSP data."""
    size = min(size_limit, len(samples))
    if size < 256:
        return [0.0] * bin_count, None, threading.get_ident()
    signal = np.asarray(samples[-size:], dtype=np.float32)
    plan_key = (size, bin_count, sample_rate)
    if plan is None or plan[0] != plan_key:
        window = np.hanning(size).astype(np.float32)
        gain = 2.0 / max(float(window.sum()), 1.0)
        frequencies = _visual_fft.rfftfreq(size, d=1.0 / sample_rate)
        maximum_frequency = min(20000.0, sample_rate * 0.5)
        ranges = ()
        if maximum_frequency > 30.0:
            edges = np.geomspace(30.0, maximum_frequency, bin_count + 1)
            first = np.searchsorted(frequencies, edges[:-1], side="left")
            last = np.maximum(first + 1, np.searchsorted(frequencies, edges[1:], side="right"))
            ranges = tuple(zip(map(int, first), map(int, last)))
        plan = plan_key, window, gain, ranges
    _, window, gain, ranges = plan
    magnitudes = np.abs(_visual_fft.rfft(signal * window)) * gain
    targets = []
    for first, last in ranges:
        magnitude = float(np.max(magnitudes[first:last])) if first < len(magnitudes) else 0.0
        db = 20.0 * math.log10(max(magnitude, 1e-8))
        targets.append(max(0.0, min(1.0, (db - floor_db) / -floor_db)))
    return targets or [0.0] * bin_count, plan, threading.get_ident()


class NativeAudioVisualizer(QWidget):
    """Final-mix time waveform and log-band spectrum in one compact view."""

    FFT_SIZE = 4096
    SPECTRUM_MIN_BINS = 8
    SPECTRUM_MAX_BINS = 64
    SPECTRUM_TARGET_BIN_WIDTH = 7
    SPECTRUM_FLOOR_DB = -72.0
    SPECTRUM_RELEASE_PER_SECOND = 0.70
    WAVEFORM_TRAIL_SECONDS = .5
    WAVEFORM_CURRENT_WIDTH = 1.25
    WAVEFORM_TRAIL_WIDTH = 1.0
    WAVEFORM_TRAIL_LAYER_ALPHA = .30
    WAVEFORM_TIME_DENSITY = 4
    DOMAIN_DIVIDER_GAP = METRICS["state_column_gap"]

    def __init__(self, waveform_tooltip="", spectrum_tooltip="", parent=None):
        super().__init__(parent)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        # The widget paints every pixel in its rectangle. This is the Qt
        # recommended path for rapidly updating opaque plot surfaces.
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setMouseTracking(True)
        # A non-empty base tooltip makes Qt dispatch QEvent::ToolTip; event()
        # then replaces it with the region-specific waveform/spectrum copy.
        self.setToolTip(" ")
        self._samples = ()
        self._sample_rate = 48000.0
        self._revision = -1
        self._spectrum = []
        self._last_analysis = time.monotonic()
        self._spectrum_plan = None
        self._analysis_executor = None
        self._analysis_future = None
        self._analysis_pending = None
        self._analysis_closed = False
        self._analysis_thread_id = None
        self._waveform_path_key = None
        self._waveform_path = None
        self._waveform_rasters = None
        self._waveform_raster_key = None
        self._waveform_trail_layer = None
        self._waveform_trails = []
        self._waveform_geometry = None
        self._waveform_path_time = 0.0
        self.destroyed.connect(self.close_analysis)
        self._dark = False
        self._waveform_tooltip = waveform_tooltip
        self._spectrum_tooltip = spectrum_tooltip

    def set_theme(self, dark: bool):
        self._dark = bool(dark)
        self._waveform_trails.clear()
        self._waveform_geometry=None;self._waveform_path_key=None
        self.update()

    def set_audio_snapshot(self, samples, sample_rate: float, revision: int,
                           *, finite_samples=False):
        """Publish immutable samples; poll ready work without waiting on the GUI."""
        self._poll_analysis()
        if revision == self._revision and float(sample_rate) == self._sample_rate:
            if self._waveform_trails:
                now=time.monotonic()
                self._waveform_trails=[(stamp,path) for stamp,path in self._waveform_trails
                    if now-stamp<self.WAVEFORM_TRAIL_SECONDS]
                self.update()  # fade frozen publication too,without FFT work
            return
        if revision < self._revision or sample_rate != self._sample_rate:
            self._waveform_trails.clear();self._waveform_path=None;self._waveform_path_key=None
            self._waveform_raster_key=None
        self._revision = int(revision)
        self._sample_rate = max(1.0, float(sample_rate))
        # OutputMeter already sanitized its published tuple before releasing
        # the audio callback. Other callers retain the defensive conversion.
        self._samples = (samples if finite_samples else
                         tuple(float(value) if math.isfinite(float(value)) else 0.0
                               for value in samples))
        self._update_spectrum()
        self.update()

    def reset_waveform_history(self):
        self._waveform_trails.clear();self._waveform_path=None
        self._waveform_path_key=None;self._waveform_geometry=None
        self._waveform_raster_key=None
        self._revision=-1
        self._waveform_trail_layer=None

    def _spectrum_width(self):
        divider_x = self._domain_divider_x()
        return max(0, self.width() - (divider_x + 1 + self.DOMAIN_DIVIDER_GAP))

    def _domain_divider_x(self):
        return max(1, min(self.width() - 2, round(self.width() * 0.48)))

    def _tooltip_for_x(self, x):
        return self._waveform_tooltip if x < self._domain_divider_x() else self._spectrum_tooltip

    def native_tooltip_content(self,position):
        split=self._domain_divider_x()
        rect=QRect(0,0,split,self.height()) if position.x()<split else QRect(split,0,max(0,self.width()-split),self.height())
        return self._tooltip_for_x(position.x()),rect

    def event(self, event):
        if event.type()==QEvent.ToolTip:return handle_native_tooltip(self,event)
        return QWidget.event(self, event)

    def _bin_count_for_width(self, width):
        return max(
            self.SPECTRUM_MIN_BINS,
            min(self.SPECTRUM_MAX_BINS, round(max(0, width) / self.SPECTRUM_TARGET_BIN_WIDTH)),
        )

    def _update_spectrum(self):
        if self._analysis_closed:
            return
        # One running task plus one overwritten latest request: never enqueue
        # one task per display frame, resize event or audio callback.
        bin_count = self._bin_count_for_width(self._spectrum_width())
        self._analysis_pending = (self._samples, self._sample_rate, bin_count)
        self._poll_analysis()

    def _poll_analysis(self):
        if self._analysis_closed:
            return
        future = self._analysis_future
        if future is not None and future.done():
            self._analysis_future = None
            targets, plan, self._analysis_thread_id = future.result()
            self._spectrum_plan = plan
            if (len(targets) == self._bin_count_for_width(self._spectrum_width())
                    and (plan is None or plan[0][2] == self._sample_rate)):
                self._accept_spectrum(targets)
                self.update()
        if self._analysis_future is None and self._analysis_pending is not None:
            samples, rate, bins = self._analysis_pending
            self._analysis_pending = None
            if self._analysis_executor is None:
                self._analysis_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ABMSynth-visual-FFT")
            self._analysis_future = self._analysis_executor.submit(
                _analyze_visual_spectrum, samples, rate, bins, self._spectrum_plan,
                self.FFT_SIZE, self.SPECTRUM_FLOOR_DB)

    def finish_analysis_for_audit(self):
        """Explicit diagnostic barrier, never called by live frame handlers."""
        while self._analysis_future is not None:
            self._analysis_future.result(timeout=5)
            self._poll_analysis()

    def close_analysis(self, *_args):
        """Join a numerical task before QWidget teardown; no audio lock involved."""
        self._analysis_closed = True
        self._analysis_pending = None
        if self._analysis_executor is not None:
            self._analysis_executor.shutdown(wait=True, cancel_futures=True)
        self._analysis_executor = self._analysis_future = None

    def _accept_spectrum(self, targets):
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_analysis)
        self._last_analysis = now
        # Attack is immediate; release is frame-rate independent so a faster
        # monitor never makes the spectrum decay more quickly.
        release = elapsed * self.SPECTRUM_RELEASE_PER_SECOND
        if len(self._spectrum) != len(targets):
            self._spectrum = [0.0] * len(targets)
        self._spectrum = [target if target >= current else max(target, current - release) for current, target in zip(self._spectrum, targets)]

    @staticmethod
    def _waveform_display_gain(peak: float) -> float:
        """One gain for the whole plot: retain each sample's linear shape.

        The old per-sample dB mapping reshaped the waveform and exaggerated
        low-level detail. This gain only enlarges the entire visible window;
        the stereo meter remains the absolute-level reference. A real 0 dBFS
        peak reaches the edge, while a sub-0 dBFS peak never does.
        """
        peak = abs(float(peak))
        if peak < 10 ** (-60 / 20) or peak >= 1.0:
            return 1.0
        db = 20.0 * math.log10(peak)
        if db <= -12.0:
            target = max(0.0, 0.8 * (db + 60.0) / 48.0)
        else:
            target = 0.8 + 0.2 * (db + 12.0) / 12.0
        return max(1.0, target / peak)

    @staticmethod
    def _waveform_points(samples, width, center_y, half_height, gain):
        """Map real signed samples to a continuous, subpixel-precise trace."""
        span = max(1, len(samples) - 1)
        extent = max(0, width - 1)
        return tuple(
            QPointF(
                index * extent / span,
                center_y - max(-1.0, min(1.0, sample * gain)) * half_height,
            )
            for index, sample in enumerate(samples)
        )

    @classmethod
    def _waveform_age_style(cls, age):
        fraction=max(0.0,min(1.0,age/cls.WAVEFORM_TRAIL_SECONDS))
        return cls.WAVEFORM_TRAIL_WIDTH,(1.0-fraction)**3

    def _waveform_raster_pair(self, polygon, width, colour):
        """Cache current1.25px and historical1px strokes once per trace."""
        ratio=self.devicePixelRatioF();rasters=[]
        for stroke in (self.WAVEFORM_CURRENT_WIDTH,self.WAVEFORM_TRAIL_WIDTH):
            raster=QPixmap(max(1,math.ceil(width*ratio)),max(1,math.ceil(self.height()*ratio)))
            raster.setDevicePixelRatio(ratio);raster.fill(Qt.transparent)
            renderer=QPainter(raster);renderer.setRenderHint(QPainter.Antialiasing)
            renderer.setPen(QPen(colour,stroke,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin))
            renderer.drawPolyline(polygon);renderer.end();rasters.append(raster)
        # Keep the original full rasters/AA phase. Restrict only later blits
        # to ink plus a conservative margin;flat traces need no empty rows.
        ink=polygon.boundingRect().adjusted(-2,-2,2,2).toAlignedRect().intersected(QRect(0,0,width,self.height()))
        return (*rasters,QRectF(ink))

    def _compose_waveform_trails(self, now):
        current=self._waveform_rasters[0]
        layer=self._waveform_trail_layer
        if layer is None or layer.size()!=current.size() or layer.devicePixelRatioF()!=current.devicePixelRatioF():
            layer=QPixmap(current.size());layer.setDevicePixelRatio(current.devicePixelRatioF())
            self._waveform_trail_layer=layer
        layer.fill(Qt.transparent)
        renderer=QPainter(layer)
        for stamp,rasters in self._waveform_trails:
            _width,opacity=self._waveform_age_style(now-stamp)
            if opacity>0:
                renderer.setOpacity(opacity)
                bounds=rasters[2];ratio=rasters[1].devicePixelRatioF()
                renderer.drawPixmap(bounds,rasters[1],QRectF(bounds.x()*ratio,bounds.y()*ratio,
                    bounds.width()*ratio,bounds.height()*ratio))
        renderer.end()
        return layer

    def resizeEvent(self, event):
        QWidget.resizeEvent(self, event)
        self._waveform_trails.clear();self._waveform_geometry=None
        if self._samples:
            self._update_spectrum()

    def paintEvent(self, _event):
        palette = widget_palette(self, self._dark)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(palette.surface))
        if self.width() <= 2 or self.height() <= 2:
            return
        divider_x = self._domain_divider_x()
        waveform_right = max(1, divider_x - self.DOMAIN_DIVIDER_GAP)
        spectrum_left = min(self.width(), divider_x + 1 + self.DOMAIN_DIVIDER_GAP)
        center_y = self.height() // 2
        waveform_half_height = max(1, min(center_y - 2, self.height() - center_y - 3))
        spectrum_baseline = max(1, self.height() - 3)
        spectrum_height = max(1, spectrum_baseline - 2)
        # This is a structural divider between time and frequency domains,
        # so it uses the card-border token rather than a decorative gridline.
        painter.setPen(QPen(QColor(palette.border), 1))
        painter.drawLine(divider_x, 2, divider_x, self.height() - 3)
        painter.setPen(QPen(QColor(palette.border_subtle), 1))
        painter.drawLine(0, center_y, waveform_right, center_y)
        painter.drawLine(spectrum_left, spectrum_baseline, self.width(), spectrum_baseline)

        waveform_width = waveform_right
        if self._samples:
            # Four times the prior time span,using real signed samples. No
            # sample transfer change or waveform fabricated from envelopes.
            key = (id(self._samples), self._revision, waveform_width, center_y, waveform_half_height)
            if key != self._waveform_path_key:
                now=time.monotonic()
                geometry=(waveform_width,center_y,waveform_half_height)
                if geometry!=self._waveform_geometry:self._waveform_trails.clear()
                elif self._waveform_path is not None:
                    self._waveform_trails.append((self._waveform_path_time,self._waveform_rasters))
                self._waveform_geometry=geometry
                self._waveform_trails=[(stamp,path) for stamp,path in self._waveform_trails
                    if now-stamp<self.WAVEFORM_TRAIL_SECONDS]
                visible = tuple(self._samples[-waveform_width*self.WAVEFORM_TIME_DENSITY:])
                raster_key=(visible,geometry,self.height(),palette.accent,self.devicePixelRatioF())
                if raster_key!=self._waveform_raster_key:
                    display_gain = self._waveform_display_gain(max(abs(sample) for sample in visible))
                    points = self._waveform_points(
                        visible, waveform_width, center_y, waveform_half_height, display_gain)
                    path = QPainterPath()
                    # addPolygon is unclosed. Retain the exact signed vertices.
                    polygon=QPolygonF(points)
                    path.addPolygon(polygon)
                    self._waveform_rasters=self._waveform_raster_pair(polygon,waveform_width,QColor(palette.accent))
                    self._waveform_path=path;self._waveform_raster_key=raster_key
                self._waveform_path_key=key
                self._waveform_path_time=now
            now=time.monotonic()
            if self._waveform_trails:
                # Apply opacity once to the ENTIRE historical layer. Each
                # trace may overlap freely without multiplying ghost brightness.
                painter.setOpacity(self.WAVEFORM_TRAIL_LAYER_ALPHA)
                painter.drawPixmap(0,0,self._compose_waveform_trails(now))
            painter.setOpacity(1.0)
            painter.drawPixmap(0,0,self._waveform_rasters[0])

        spectrum_width = max(0, self.width() - spectrum_left)
        if spectrum_width <= 0:
            return
        bin_count = len(self._spectrum)
        if not bin_count:
            return
        gap = 1
        bar_color = QColor(palette.accent)
        for index, response in enumerate(self._spectrum):
            left = spectrum_left + round(index * spectrum_width / bin_count)
            right = spectrum_left + round((index + 1) * spectrum_width / bin_count) - gap
            if left >= self.width() or right <= left:
                break
            width = min(right - left, self.width() - left)
            height = max(1, round(spectrum_height * response)) if response > 0.0 else 0
            if height:
                fill_color = QColor(bar_color); fill_color.setAlpha(205 if self._dark else 180)
                painter.fillRect(QRect(left, spectrum_baseline - height, width, height), fill_color)


class NativeOptionalLabel(ElasticLabel):
    """Supplemental text contributes a layout item only while it has content.

    Opt-in presentation role,not a global rule for labels/value columns. Both
    setText and clear own visibility so callers cannot leave an empty row.
    """
    def __init__(self,text='',parent=None,preferred_width=0):
        super().__init__(text,parent,preferred_width=preferred_width)
        self.setMinimumSize(0,0)
        policy=self.sizePolicy();policy.setHorizontalPolicy(QSizePolicy.Ignored)
        policy.setVerticalPolicy(QSizePolicy.Maximum);policy.setRetainSizeWhenHidden(False)
        self.setSizePolicy(policy);self.setVisible(bool(text))

    def setText(self,text):
        super().setText(text);self.setVisible(bool(text))

    def clear(self):
        self.setText('')


class AdaptiveValueLabel(ElasticLabel):
    """Keep a complete compact readout by tightening only its letter spacing.

    Normal text stays untouched. Under extreme window compression, stop before
    glyphs become illegible and let the existing zero-minimum layout clip.
    """

    def __init__(self, *, preferred_width=0):
        super().__init__(preferred_width=preferred_width)
        self._adapting_font = False
        self._multiline_gap = None
        self._preferred_tracking = 0.0

    def set_preferred_tracking(self, spacing: float):
        """Normal tracking when space allows; no width floor is introduced."""
        self._preferred_tracking = float(spacing)
        self._fit_spacing()
        self.update()

    def set_multiline_gap(self, gap: float | None):
        """Opt-in ink-to-ink leading without changing editor text or geometry."""
        self._multiline_gap = None if gap is None else max(0.0, float(gap))
        self.update()

    def _multiline_ink_layout(self):
        lines = self.text().split("\n")
        if self._multiline_gap is None or len(lines) < 2:
            return ()
        metrics = QFontMetricsF(self.font())
        bounds = [metrics.tightBoundingRect(line) for line in lines]
        height = sum(bound.height() for bound in bounds) + self._multiline_gap * (len(lines) - 1)
        area = self.contentsRect()
        y = area.y() + area.height() / 2 - height / 2
        result = []
        for line, bound in zip(lines, bounds):
            x = area.x() + area.width() / 2 - bound.width() / 2
            result.append((line, QRectF(x, y, bound.width(), bound.height()),
                           QPointF(x - bound.left(), y - bound.top())))
            y += bound.height() + self._multiline_gap
        return tuple(result)

    def paintEvent(self, event):
        lines = self._multiline_ink_layout()
        if not lines:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setFont(self.font())
        group = QPalette.Active if self.isEnabled() else QPalette.Disabled
        painter.setPen(self.palette().color(group, self.foregroundRole()))
        painter.setClipRect(self.contentsRect())
        for text, _ink, baseline in lines:
            painter.drawText(baseline, text)

    def setText(self, text):
        super().setText(text)
        self._fit_spacing()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_spacing()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self._fit_spacing()

    def _fit_spacing(self):
        if getattr(self, "_adapting_font", False):
            return
        pending=_theme_spacing_pending.get()
        if pending is not None:
            pending[id(self)]=self
            return
        font = QFont(self.font())
        preferred = getattr(self, "_preferred_tracking", 0.0)
        font.setLetterSpacing(QFont.AbsoluteSpacing, preferred)
        font.setWordSpacing(0)
        text = self.text()
        width = max(0, self.contentsRect().width() - 2)
        key=(text,width,preferred,QFont(font),self.logicalDpiX(),self.logicalDpiY(),self.devicePixelRatioF())
        cached=getattr(self,'_spacing_cache',None)
        if cached is not None and cached[0]==key:
            spacing,word_spacing=cached[1:]
            self._apply_spacing(font,spacing,word_spacing)
            return
        lines = text.split("\n")
        metrics=QFontMetricsF(font)
        widest = max(lines, key=metrics.horizontalAdvance)
        natural = metrics.horizontalAdvance(widest)
        spacing = preferred
        if width and len(text) > 1 and natural > width:
            # At 14px the normal full-range value needs about -2px/glyph.
            # Preserve the font size and avoid crushing letters indefinitely.
            floor = -max(1.0, font.pixelSize() * 0.18)
            glyph_count = max(1, len(widest) - widest.count(" "))
            spacing = max(floor, preferred + (width - natural) / glyph_count)
            font.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
            font.setWordSpacing(-min(0.0, spacing))
            adjusted=QFontMetricsF(font)
            measured = max(adjusted.horizontalAdvance(line) for line in lines)
            if measured > width:
                spacing = max(floor, spacing + (width - measured) / glyph_count - 0.05)
        word_spacing = -min(0.0, spacing)
        # Store a normalized copy, not the font subsequently fitted below.
        self._spacing_cache=(key,spacing,word_spacing)
        self._apply_spacing(font,spacing,word_spacing)

    def _apply_spacing(self,font,spacing,word_spacing):
        if (abs(self.font().letterSpacing() - spacing) < 0.01
                and abs(self.font().wordSpacing() - word_spacing) < 0.01):
            return
        font=QFont(font)  # The normalized QFont retained in the cache is immutable.
        font.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
        # Keep the actual spaces legible while letter spacing absorbs overflow.
        font.setWordSpacing(word_spacing)
        self._adapting_font = True
        try:
            self.setFont(font)
        finally:
            self._adapting_font = False


class NativeInlineNumberEdit(QLineEdit):
    """One validated in-place integer editor for grid cells and readouts."""

    def __init__(self, minimum, maximum, parent=None, *, decimals=0):
        super().__init__(parent)
        self.setObjectName("InlineNumberEditor")
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.set_numeric_range(minimum, maximum, decimals)

    def set_numeric_range(self, minimum, maximum, decimals=0):
        self._decimals = int(decimals)
        if self._decimals:
            validator = QDoubleValidator(float(minimum), float(maximum), self._decimals, self)
            validator.setNotation(QDoubleValidator.StandardNotation)
        else:
            validator = QIntValidator(int(round(minimum)), int(round(maximum)), self)
        validator.setLocale(QLocale.c())
        self.setValidator(validator)

    def entered_number(self):
        raw = self.text().strip()
        pattern = (rf"[+-]?(?:\d+(?:\.\d{{1,{self._decimals}}})?|\.\d{{1,{self._decimals}}})"
                   if self._decimals else r"[+-]?\d+")
        if not self.hasAcceptableInput() or not re.fullmatch(pattern, raw):
            return None
        number = float(raw) if self._decimals else int(raw)
        validator = self.validator()
        if not validator.bottom() <= number <= validator.top():
            return None
        return number


class NativePointerFocusDismissal(QObject):
    """Clear stale keyboard focus on an unrelated in-app pointer press.

    Qt leaves focus on the last control when a non-focusable card/label is
    clicked. Clear that focus while allowing clicks on another control to
    reach the control's normal handler.
    """

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner

    def eventFilter(self, watched, event):
        if event.type() != QEvent.MouseButtonPress or not isinstance(watched, QWidget):
            return False
        focused = QApplication.focusWidget()
        if focused is None or watched.window() is not focused.window():
            return False
        window = watched.window()
        while window is not None and window is not self.owner:
            window = window.parentWidget()
        if window is None:
            return False
        if watched is focused or focused.isAncestorOf(watched):
            return False
        if watched.isAncestorOf(focused) and watched.focusPolicy() != Qt.NoFocus:
            return False
        focused.clearFocus()
        return False


def clear_native_pointer_feedback(widget, *, cancel_gesture=False):
    """Clear existing transient feedback, never selection, values or gestures."""
    if not shiboken6.isValid(widget):return
    if widget.underMouse():
        if widget.testAttribute(Qt.WA_Hover):
            old=widget.mapFromGlobal(QCursor.pos())
            QApplication.sendEvent(widget,QHoverEvent(QEvent.HoverLeave,QPointF(-1,-1),QPointF(QCursor.pos()),QPointF(old)))
        QApplication.sendEvent(widget,QEvent(QEvent.Leave));widget.update()
    reset=getattr(widget,'reset_stale_hover',None)
    if reset is not None:reset()
    if cancel_gesture:
        state=getattr(widget,'_native_wheel_input',None)
        if state is not None:state.context=None;state.remainder=0
        if isinstance(widget,QAbstractButton):widget.setDown(False)
        cancel=getattr(widget,'cancel_pointer_gesture',None)
        if cancel is not None:cancel()


def refresh_native_pointer_feedback(widget):
    """Re-hit an existing region after a scroll/model/popup boundary."""
    if not shiboken6.isValid(widget):return
    viewport=widget.viewport() if isinstance(widget,QAbstractItemView) else widget
    position=viewport.mapFromGlobal(QCursor.pos())
    if widget.isVisible() and widget.isEnabled() and viewport.underMouse() and viewport.rect().contains(position):
        widget.reconcile_hover(position)
    else:
        reset=getattr(widget,'reset_stale_hover',None)
        if reset is not None:reset()


def connect_native_pointer_model(view):
    """Shared hit invalidation for navigable lists/tables, no polling timer."""
    model=view.model()
    for signal in (model.modelReset,model.rowsRemoved,model.rowsInserted,model.rowsMoved,model.layoutChanged):
        signal.connect(view.refresh_pointer_feedback)
    model.dataChanged.connect(view.pointer_data_changed)


def refresh_changed_pointer_feedback(view,first,last,roles):
    # Empty roles also covers ItemIsEnabled changes. Activity/value updates
    # carry explicit roles and must not re-hit the pointer at render cadence.
    if roles or not shiboken6.isValid(view):return
    row=getattr(view,'_hover_row',-1)
    if first.row()<=row<=last.row():refresh_native_pointer_feedback(view)


class NativeHoverReconciler(QObject):
    """Repair stale Qt hover state once a popup releases the pointer grab."""

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self._scheduled = False
        self._open_combo = None
        self._outside_press = False

    def _belongs_to_owner(self, widget):
        while widget is not None:
            if widget is self.owner:
                return True
            widget = widget.parentWidget()
        return False

    def schedule(self):
        if self._scheduled or getattr(self.owner, "_shutdown_started", False):
            return
        self._scheduled = True
        QTimer.singleShot(0, self.reconcile)

    def popup_opened(self, combo):
        self._open_combo = combo
        self._outside_press = False

    def popup_closed(self, combo):
        outside_press = combo is self._open_combo and self._outside_press
        if combo is self._open_combo:
            self._open_combo = None
            self._outside_press = False
        self.schedule()
        if outside_press:
            # Qt may return focus to the combo as popup teardown completes.
            QTimer.singleShot(0, lambda: self._finish_outside_focus(combo))

    def reconcile(self):
        self._scheduled = False
        if getattr(self.owner, "_shutdown_started", False):
            return
        # widgetAt is intentionally used only at popup/window boundaries;
        # Qt documents it as potentially slow. Ancestors share real hover.
        under_pointer = set()
        widget = QApplication.widgetAt(QCursor.pos())
        while widget is not None:
            under_pointer.add(widget)
            widget = widget.parentWidget()
        for widget in (self.owner, *self.owner.findChildren(QWidget)):
            if not shiboken6.isValid(widget):continue
            if widget in under_pointer and widget.isVisible() and widget.isEnabled():
                entered=not widget.underMouse()
                local=widget.mapFromGlobal(QCursor.pos())
                if entered:
                    QApplication.sendEvent(widget,QEnterEvent(QPointF(local),QPointF(widget.mapTo(widget.window(),local)),QPointF(QCursor.pos())))
                    widget.update()
                if widget.testAttribute(Qt.WA_Hover):
                    QApplication.sendEvent(widget,QHoverEvent(QEvent.HoverEnter if entered else QEvent.HoverMove,
                        QPointF(local),QPointF(QCursor.pos()),QPointF(-1,-1) if entered else QPointF(local)))
                restore=getattr(widget,'reconcile_hover',None)
                if restore is not None:
                    viewport=widget.viewport() if isinstance(widget,(QAbstractItemView,QHeaderView)) else widget
                    restore(viewport.mapFromGlobal(QCursor.pos()))
                continue
            clear_native_pointer_feedback(widget)

    def _finish_outside_focus(self, combo):
        if (not getattr(self.owner, "_shutdown_started", False)
                and shiboken6.isValid(combo) and combo.isVisible() and QApplication.focusWidget() is combo
                and not combo.rect().contains(combo.mapFromGlobal(QCursor.pos()))):
            combo.clearFocus()

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind not in (QEvent.MouseButtonPress,QEvent.Hide,QEvent.WindowDeactivate,QEvent.WindowActivate,QEvent.EnabledChange):
            return False
        if not isinstance(watched, QWidget):
            return False
        if not self._belongs_to_owner(watched):return False
        if kind == QEvent.WindowDeactivate:
            # Qt also sends this change to child widgets. Rewalking every
            # child's subtree makes activation quadratic and stalls the first
            # title-bar drag. Clear each actual recipient once. Hidden widgets
            # already cancel at Hide/disable; separate companion windows are
            # not recipients merely because they share QObject ownership.
            clear_native_pointer_feedback(watched,cancel_gesture=True)
        elif kind in (QEvent.EnabledChange,QEvent.Hide):
            # Boundary-only cleanup; no tree traversal on ordinary mouse moves.
            if not watched.isEnabled() or kind == QEvent.Hide:
                targets=(watched,*watched.findChildren(QWidget))
                for target in targets:
                    clear_native_pointer_feedback(target,cancel_gesture=True)
            elif watched.isVisible():self.schedule()
        if kind == QEvent.MouseButtonPress and self._open_combo is not None:
            combo = self._open_combo
            if not shiboken6.isValid(combo):
                self._open_combo=None;self._outside_press=False
                return False
            point = event.globalPosition().toPoint()
            popup = QApplication.activePopupWidget()
            over_combo = combo.rect().contains(combo.mapFromGlobal(point))
            over_popup = (popup is not None and popup.rect().contains(
                popup.mapFromGlobal(point)))
            if not over_combo and not over_popup:
                self._outside_press = True
        if (kind == QEvent.Hide and watched.isWindow()
                and watched.windowFlags() & Qt.Popup
                and self._belongs_to_owner(watched)):
            self.schedule()
        elif (kind == QEvent.WindowActivate and watched.isWindow()
              and self._belongs_to_owner(watched)):
            self.schedule()
        return False


class NativeWindowInteractionFilter(QObject):
    """One application dispatch, separate stateful pointer/drop handlers.

    Qt documents that global filters run on EVERY delivered event. Unrelated
    style/font/paint events must not cross three Python handlers. Preserve the
    prior installed order: drop, hover, then outside-focus dismissal.
    """
    _HANDLED_EVENTS=frozenset((QEvent.Show,QEvent.Close,QEvent.KeyPress,QEvent.KeyRelease,
        QEvent.DragEnter,QEvent.DragMove,QEvent.DragLeave,QEvent.Drop,
        QEvent.MouseButtonPress,QEvent.Hide,QEvent.WindowDeactivate,
        QEvent.WindowActivate,QEvent.EnabledChange))
    def __init__(self, owner, focus, hover, drop):
        super().__init__(owner)
        self.focus, self.hover, self.drop = focus, hover, drop
        app = QApplication.instance()
        if app.platformName() == 'windows' and not hasattr(app, '_native_modal_close_router'):
            app._native_modal_close_router = NativeModalCloseRouter()
            app.installNativeEventFilter(app._native_modal_close_router)

    def eventFilter(self, watched, event):
        kind=event.type()
        if kind not in self._HANDLED_EVENTS:return False
        if (kind==QEvent.Show and isinstance(watched,QWidget)
                and self.hover._belongs_to_owner(watched)):
            apply_native_font_raster(watched)
        if (kind==QEvent.Close and isinstance(watched,QWidget) and watched.isWindow()
                and watched is not self.parent() and self.hover._belongs_to_owner(watched)):
            modal=NativeModalCloseRouter._blocking_dialog(watched)
            if modal is not None:
                if modal.isMinimized():modal.showNormal()
                modal.raise_();modal.activateWindow()
                event.ignore();return True
        if (kind in (QEvent.KeyPress,QEvent.KeyRelease) and event.key()==Qt.Key_Alt
                and isinstance(watched,QWidget) and self.hover._belongs_to_owner(watched)):
            # The pointer may be over a canvas while keyboard focus is elsewhere.
            # One modifier boundary hit, not a mouse-move or per-frame tree scan.
            target=QApplication.widgetAt(QCursor.pos())
            modifiers=(event.modifiers()|Qt.AltModifier if kind==QEvent.KeyPress else event.modifiers()&~Qt.AltModifier)
            while target is not None and self.hover._belongs_to_owner(target):
                refresh=getattr(target,'pointer_modifiers_changed',None)
                if refresh is not None:refresh(modifiers);break
                target=target.parentWidget()
        if kind==QEvent.Show and isinstance(watched,QWidget) and watched.isWindow():
            ancestor=watched
            while ancestor is not None and ancestor is not self.parent():ancestor=ancestor.parent()
            if ancestor is self.parent():
                install_native_window_shell(watched)
                install_native_dpi_guard(watched)
                guard=getattr(watched,'_native_dpi_size_guard',None)
                if guard is not None:guard._bind_native_sizing(watched)
        if kind in (QEvent.DragEnter,QEvent.DragMove,QEvent.DragLeave,QEvent.Drop):
            handled=self.drop.eventFilter(watched,event)
            if kind in (QEvent.DragLeave,QEvent.Drop) and isinstance(watched,QWidget) and self.hover._belongs_to_owner(watched):
                self.hover.schedule()
            return handled
        if kind in (QEvent.MouseButtonPress,QEvent.Hide,QEvent.WindowDeactivate,QEvent.WindowActivate,QEvent.EnabledChange):
            if self.hover.eventFilter(watched,event): return True
            if kind==QEvent.MouseButtonPress: return self.focus.eventFilter(watched,event)
        return False


class EditableNumberLabel(AdaptiveValueLabel):
    """Compact readout with a shared, bounded integer editor on left click."""

    def __init__(self, text="", *, preferred_width=0, parent=None):
        super().__init__(preferred_width=preferred_width)
        self.setTextFormat(Qt.PlainText)
        if parent is not None:
            self.setParent(parent)
        self._read_number = None
        self._write_number = None
        self._context = None
        self._opened_context = None
        self._editor = None
        self._entry_range = None
        self._entry_decimals = 0
        self._entry_formatter = None
        self._text_entry = False
        self._opened_number = None
        self._editing = False
        self._app_filter_installed = False
        self._entry_control_ref = None
        self._keyboard_entry = False
        self.setText(text)

    def bind_control(self, control):
        """Dense controls keep one Tab stop and share the pointer entry editor."""
        previous=self._entry_control_ref() if self._entry_control_ref else None
        if previous is control:return
        if previous is not None and shiboken6.isValid(previous):previous.removeEventFilter(self)
        self._entry_control_ref=weakref.ref(control)
        control.installEventFilter(self)
        self._sync_entry_cursor()

    def _entry_target_available(self,*,visible=True):
        control=self._entry_control_ref() if self._entry_control_ref else None
        return (self.isEnabled() and (self.isVisible() or not visible) and
                (control is None or (shiboken6.isValid(control) and control.isEnabled() and (control.isVisible() or not visible))))

    def _sync_entry_cursor(self):
        available=self._entry_range is not None and self._entry_target_available(visible=False)
        set_native_cursor(self,Qt.IBeamCursor if available else Qt.ArrowCursor)
        # Feedback follows the input capability,never a dialog's object name.
        editable=self._entry_range is not None
        changed=False
        for key,value in (('nativeEditableField',editable),('nativeEntryAvailable',available)):
            if self.property(key)!=value:self.setProperty(key,value);changed=True
        if changed:
            self.style().unpolish(self);self.style().polish(self);self.update()

    def setText(self, text):
        # Live updates within the same target leave typing intact. A selection
        # change must retire an editor before its label presents another target;
        # outside-click commits still occur before click dispatch as before.
        if (getattr(self, '_editing', False) and self._context is not None
                and self._context() != self._opened_context):
            self.cancel_editor()
        super().setText(text)

    def configure(self, *, minimum, maximum, read, write, context=None, label="",
                  decimals=0, format_input=None):
        self._text_entry = False
        self._read_number, self._write_number = read, write
        self._context = context
        self._entry_range = (minimum, maximum)
        self._entry_decimals = int(decimals)
        self._entry_formatter = format_input
        if self._editor is not None:
            self._editor.set_numeric_range(minimum, maximum, decimals)
        self._sync_entry_cursor()
        self.setAccessibleName(tr("number_entry.accessible", control=label))
        self._clear_redundant_entry_hint()

    def configure_text(self, *, read, write, context=None, label="", max_length=48):
        """Names share numeric readout commit/cancel/focus/geometry semantics."""
        self._text_entry = True
        self._read_number, self._write_number, self._context = read, write, context
        self._entry_range = (0, max_length)
        self._entry_formatter = None
        if self._editor is not None and isinstance(self._editor, NativeInlineNumberEdit):
            self._editor.deleteLater()
            self._editor = None
        self._sync_entry_cursor()
        self.setAccessibleName(tr("channel_editor.cc_name_accessible", control=label))

    def disable_entry(self):
        self.cancel_editor()
        self._entry_range = None
        self._sync_entry_cursor()

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type()==QEvent.EnabledChange:
            if not self.isEnabled():self.cancel_editor()
            self._sync_entry_cursor()

    def _entered_value(self):
        if not self._text_entry:
            return self._editor.entered_number()
        text = self._editor.text().strip()
        return text if text and not any(ord(char) < 32 for char in text) else None

    def _clear_redundant_entry_hint(self):
        # Editing grammar belongs to the shared editor/accessibility contract,
        # not a tutorial appended to every semantic sound-control tooltip.
        hint = tr("number_entry.hint")
        self.setToolTip(self.toolTip().split("\n" + hint)[0])

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled() and self._entry_range is not None:
            self.open_editor()
            event.accept()
            return
        super().mousePressEvent(event)

    def open_editor(self):
        if self._entry_range is None or not self._entry_target_available():
            return
        if self._editing:
            self._editor.setFocus(Qt.MouseFocusReason)
            return
        if self._editor is None:
            if self._text_entry:
                self._editor = QLineEdit(self)
                self._editor.setObjectName("InlineNumberEditor")
                self._editor.setMinimumWidth(0)
                self._editor.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
                self._editor.setMaxLength(self._entry_range[1])
            else:
                self._editor = NativeInlineNumberEdit(
                    *self._entry_range, self, decimals=self._entry_decimals)
            self._editor.setAccessibleName(self.accessibleName())
            self._editor.installEventFilter(self)
        self._editing = True
        self._keyboard_entry = False
        self._opened_context = self._context() if self._context else None
        number = self._read_number()
        shown = (str(number) if self._text_entry else
                 self._entry_formatter(number) if self._entry_formatter else
                 f"{number:.{self._entry_decimals}f}" if self._entry_decimals else
                 str(int(number)))
        self._editor.setText(shown)
        if self.property("mixedValue") or self.property('unknownValue'):
            self._editor.clear()
            self._editor.setPlaceholderText(tr('channel_editor.unknown_value') if self.property('unknownValue') else tr("channel_editor.mixed_value"))
        else:
            self._editor.setPlaceholderText("")
        self._opened_number = self._entered_value()
        font = QFont(self.font())
        if 0 < font.pixelSize() < 12:
            font.setPixelSize(12)
        self._editor.setFont(font)
        if font.pixelSize() > 0:
            # The parent QSS assigns a 14px default to QLineEdit; Qt gives
            # that rule precedence over setFont() unless the editor declares
            # its own font size at the widget level.
            self._editor.setStyleSheet(f"font-size: {font.pixelSize()}px;")
        self._editor.setAlignment(self.alignment())
        self._position_editor()
        self._editor.show()
        self._editor.raise_()
        self._editor.setFocus(Qt.MouseFocusReason)
        self._editor.selectAll()
        QApplication.instance().installEventFilter(self)
        self._app_filter_installed = True

    def cancel_editor(self):
        self._finish_edit(False)

    def _finish_edit(self, commit):
        if self._editor is None or not self._editing:
            return
        self._editing = False
        if self._app_filter_installed:
            QApplication.instance().removeEventFilter(self)
            self._app_filter_installed = False
        available=self._entry_target_available()
        number = self._entered_value() if commit and available else None
        same_context = (self._context is None
                        or self._context() == self._opened_context)
        if self._editor.hasFocus():
            self._editor.clearFocus()
        self._editor.hide()
        # The editor temporarily belongs to a less-clipped ancestor only
        # while open. Restore ownership so deleting the readout also deletes
        # its editor (table rebuilds and dialog lifetime).
        if self._editor.parentWidget() is not self:
            self._editor.setParent(self)
        self._opened_context = None
        self._keyboard_entry = False
        unchanged = number is not None and number == self._opened_number
        self._opened_number = None
        if number is not None and same_context and not unchanged:
            self._write_number(number)

    def eventFilter(self, watched, event):
        control=self._entry_control_ref() if self._entry_control_ref else None
        if watched is control and event.type()==QEvent.EnabledChange:self._sync_entry_cursor()
        if self._editing and isinstance(watched,QWidget):
            kind=event.type()
            if ((watched is self or watched.isAncestorOf(self) or watched is control) and
                (kind in (QEvent.Hide,QEvent.ParentAboutToChange) or
                 kind==QEvent.EnabledChange and not watched.isEnabled())):
                self.cancel_editor()
        if (watched is control and event.type()==QEvent.KeyPress
                and event.key() in (Qt.Key_F2,Qt.Key_Return,Qt.Key_Enter)
                and event.modifiers()==Qt.NoModifier and control.isEnabled()
                and self.isEnabled() and self._entry_range is not None):
            if not event.isAutoRepeat():
                self.open_editor();self._keyboard_entry=True
            event.accept();return True
        if self._editing and watched is self._editor:
            if event.type() == QEvent.KeyPress:
                if event.key() == Qt.Key_Escape:
                    self._finish_keyboard_edit(False)
                    return True
                if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                    self._finish_keyboard_edit(True)
                    return True
            if event.type() == QEvent.FocusOut:
                self._finish_edit(True)
        if self._editing and isinstance(watched, QWidget):
            if (event.type() == QEvent.MouseButtonPress
                    and watched.window() is self.window()
                    and watched is not self._editor
                    and not self._editor.isAncestorOf(watched)):
                # A card background or QLabel may not accept focus. End the
                # edit before dispatching the press, but never eat that press.
                self._finish_edit(True)
            elif event.type() == QEvent.WindowDeactivate and watched is self.window():
                self._finish_edit(True)
        return super().eventFilter(watched, event)

    def _finish_keyboard_edit(self,commit):
        control=self._entry_control_ref() if self._keyboard_entry and self._entry_control_ref else None
        same_context=self._context is None or self._context()==self._opened_context
        self._keyboard_entry=False
        self._finish_edit(commit)
        if (same_context and control is not None and shiboken6.isValid(control)
                and control.isEnabled() and control.isVisible()):
            control.setFocus(Qt.OtherFocusReason)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._editing:
            self._position_editor()

    def moveEvent(self, event):
        super().moveEvent(event)
        if self._editing:
            self._position_editor()

    def _position_editor(self):
        """Fit framed input above a compact readout without clipping it.

        A QLineEdit's one-line size hint is taller than the 14/16 px monitor
        readouts. Children outside a QLabel's rect are clipped, so the editor
        must live in the nearest ancestor with enough room for its full ink.
        Keep its bottom aligned with the readout rather than shifting numbers
        downward into the next monitor row.
        """
        editor = self._editor
        if editor is None:
            return
        height = max(self.height(), editor.sizeHint().height())
        host = self.parentWidget()
        while host is not None and host.parentWidget() is not None:
            bottom = self.mapTo(host, QPoint(0, self.height())).y()
            if bottom >= height and bottom <= host.height():
                break
            host = host.parentWidget()
        if host is None:
            host = self
        if editor.parentWidget() is not host:
            editor.setParent(host)
        bottom_left = self.mapTo(host, QPoint(0, self.height()))
        editor.setGeometry(bottom_left.x(), bottom_left.y() - height,
                           self.width(), height)


class NativeFXVisual(QWidget):
    """Shared configuration plot. No stream, worker, timer or audio samples.

    Only explicit handle gestures emit parameter patches. Accepted readback
    drives every repaint; selection identity changes cancel an old gesture.
    Existing numeric fields remain the complete keyboard/precision interface.
    """
    parametersEdited=Signal(object)
    parameterContextRequested=Signal(str,object)

    def __init__(self,parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
        self.setFixedHeight(194)
        self.setFocusPolicy(Qt.StrongFocus);self.setMouseTracking(True)
        self.kind='';self.values={};self.plot=None;self.identity=None
        self._active=None;self._hover=None;self._selected=0;self._gesture_plot=None
        self._gesture_values=None
        self.setAccessibleName(tr('audio_fx.visual.accessible'))
        install_effect_tooltip(tr('audio_fx.visual.interaction'),self)

    def sizeHint(self):return QSize(420,194)

    def set_configuration(self,kind,values,rate,identity,enabled=True):
        if identity!=self.identity:
            self._cancel_gesture();self._selected=0;self._hover=None
        self.kind,self.values,self.identity=kind,dict(values),identity
        self._rate=rate
        span=self._gesture_plot.xdomain[1] if self._active is not None and self._gesture_plot.family=='envelope' else None
        key=(kind,tuple(self.values.items()),rate,identity,span)
        changed=key!=getattr(self,'_configuration_key',None)
        if changed:
            self.plot=audio_fx_visual_plot(kind,self.values,rate,envelope_span=span)
            self._configuration_key=key
        self.setEnabled(enabled)
        if changed:
            self.setFocusPolicy(Qt.StrongFocus if self.plot.handles else Qt.NoFocus)
            if self.plot.handles:
                install_effect_tooltip(tr('audio_fx.visual.interaction' if len(self.plot.handles)>1 else
                    'audio_fx.visual.interaction_single'),self)
            else:self.setToolTip('')
            self.setAccessibleDescription(self.plot.note);self.update()

    def plot_rect(self):
        return QRectF(48 if self.width()>230 else 8,30,max(0,self.width()-(66 if self.width()>230 else 26)),max(0,self.height()-64))

    def point(self,x,y):
        rect=self.plot_rect();return QPointF(rect.left()+x*rect.width(),rect.bottom()-y*rect.height())

    def handle_at(self,point):
        if not self.plot:return None
        distances=[((self.point(h.x,h.y)-point).manhattanLength(),i) for i,h in enumerate(self.plot.handles)]
        return min(distances)[1] if distances and min(distances)[0]<=18 else None

    def _cancel_gesture(self):
        self._active=None;self._gesture_plot=None;self._gesture_values=None
        if QWidget.mouseGrabber() is self:self.releaseMouse()

    def hideEvent(self,event):self._cancel_gesture();super().hideEvent(event)

    def changeEvent(self,event):
        if event.type()==QEvent.EnabledChange and not self.isEnabled():self._cancel_gesture()
        super().changeEvent(event)

    def paintEvent(self,event):
        if self.plot is None:return
        colors=widget_palette(self);p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        rect=self.plot_rect();plot=self.plot
        ink=QColor(colors.text if self.isEnabled() else colors.muted)
        p.setPen(ink)
        p.drawText(QRectF(0,0,self.width(),24),Qt.AlignVCenter|Qt.AlignLeft,
            self.fontMetrics().elidedText(plot.title,Qt.ElideRight,max(0,self.width()-4)))
        p.setPen(QPen(QColor(colors.border_subtle),1))
        for x,label in plot.xticks:
            a=self.point(x,0);p.drawLine(a,self.point(x,1))
        for y,label in plot.yticks:p.drawLine(self.point(0,y),self.point(1,y))
        p.setPen(QColor(colors.muted))
        for x,label in plot.xticks:
            a=self.point(x,0)
            p.drawText(QRectF(max(0,min(self.width()-48,a.x()-24)),rect.bottom()+5,48,22),Qt.AlignCenter,label)
        if self.width()>230:
            for y,label in plot.yticks:
                p.drawText(QRectF(0,self.point(0,y).y()-10,42,20),Qt.AlignRight|Qt.AlignVCenter,label)
        p.save();p.setClipRect(rect)
        # Reference curves are subdued/dashed. Primary settings use the accent.
        for index,curve in enumerate(plot.curves):
            reference=(plot.family in ('crusher','saturation') and index==(0 if plot.family=='crusher' else 1))
            color=QColor(colors.muted if reference else colors.accent if index==0 or plot.family=='crusher' else colors.success)
            if not self.isEnabled():color.setAlphaF(.35)
            pen=QPen(color,1.15 if reference else 1.8)
            if reference:pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            if plot.bars:
                for x,y in curve:p.drawLine(self.point(x,.5),self.point(x,y))
            elif curve:
                path=QPainterPath();path.moveTo(self.point(*curve[0]))
                for x,y in curve[1:]:path.lineTo(self.point(x,y))
                p.drawPath(path)
        if plot.family=='xy' and plot.handles:
            h=plot.handles[0];c=QColor(colors.accent);c.setAlphaF(.12 if self.isEnabled() else .04)
            p.fillRect(QRectF(self.point(0,h.y),self.point(h.x,0)),c)
            p.setPen(QPen(QColor(colors.muted),1,Qt.DashLine))
            p.drawLine(self.point(h.x,0),self.point(h.x,1));p.drawLine(self.point(0,h.y),self.point(1,h.y))
        p.restore()
        for i,h in enumerate(plot.handles):
            pos=self.point(h.x,h.y)
            if not rect.adjusted(-1,-1,1,1).contains(pos):continue
            active=i==self._hover or i==self._active or (self.hasFocus() and i==self._selected)
            color=QColor((colors.success if self.kind=='echo_dx8' and i==1 else colors.accent) if self.isEnabled() else colors.muted)
            p.setPen(QPen(color,2));p.setBrush(QColor(colors.input))
            p.drawEllipse(pos,6 if active else 4.5,6 if active else 4.5)
            if active:
                keys=tuple(key for key in (h.xkey,h.ykey) if key)
                from effect_control import AUDIO_FX_PARAMETERS
                specs={s.key:s for s in AUDIO_FX_PARAMETERS[self.kind]}
                text=' · '.join(tr('audio_fx.visual.handle_value',name=audio_fx_parameter_caption(self.kind,k),value=audio_fx_parameter_value(specs[k],self.values[k])) for k in keys)
                width=min(self.width()-8,self.fontMetrics().horizontalAdvance(text)+12)
                box=QRectF(max(4,min(self.width()-width-4,pos.x()-width/2)),max(24,pos.y()-31),max(0,width),23)
                p.fillRect(box,QColor(colors.surface));p.setPen(ink)
                p.drawText(box,Qt.AlignCenter,self.fontMetrics().elidedText(text,Qt.ElideRight,max(0,int(width)-8)))
        p.end()

    def mousePressEvent(self,event):
        if event.button()!=Qt.LeftButton or not self.isEnabled():return super().mousePressEvent(event)
        found=self.handle_at(event.position())
        if found is None:return super().mousePressEvent(event)
        self.setFocus(Qt.MouseFocusReason);self._active=self._selected=found
        self._gesture_plot=self.plot;self._gesture_values=dict(self.values)
        self.grabMouse();event.accept();self.update()

    def mouseMoveEvent(self,event):
        if self._active is None:
            self.reconcile_hover(event.position())
            return super().mouseMoveEvent(event)
        rect=self.plot_rect()
        if rect.width()<=0 or rect.height()<=0:return
        plot=self._gesture_plot;handle=plot.handles[self._active]
        patch=audio_fx_visual_edit(self.kind,self._gesture_values,plot,handle,
            (event.position().x()-rect.left())/rect.width(),(rect.bottom()-event.position().y())/rect.height())
        self.parametersEdited.emit(patch);event.accept()

    def reconcile_hover(self,position):
        found=self.handle_at(position) if self.isEnabled() else None
        if found!=self._hover:
            self._hover=found
            handle=self.plot.handles[found] if found is not None else None
            cursor=(Qt.SizeAllCursor if handle and handle.xkey and handle.ykey else
                    Qt.SizeHorCursor if handle and handle.xkey else Qt.SizeVerCursor if handle else Qt.ArrowCursor)
            set_native_cursor(self,cursor);self.update()

    def reset_stale_hover(self):
        self._hover=None
        if self._active is None:set_native_cursor(self,Qt.ArrowCursor)
        self.update()

    def cancel_pointer_gesture(self):self._cancel_gesture();self.reset_stale_hover()

    def enterEvent(self,event):self.reconcile_hover(event.position());super().enterEvent(event)

    def mouseReleaseEvent(self,event):
        if event.button()==Qt.LeftButton and self._active is not None:
            self._cancel_gesture();event.accept()
            self.plot=audio_fx_visual_plot(self.kind,self.values,self._rate);self.update()
        else:super().mouseReleaseEvent(event)

    def leaveEvent(self,event):
        self.reset_stale_hover();super().leaveEvent(event)

    def focusOutEvent(self,event):self._cancel_gesture();self.update();super().focusOutEvent(event)

    def focusInEvent(self,event):self.update();super().focusInEvent(event)

    def contextMenuEvent(self,event):
        found=self.handle_at(event.pos())
        if found is not None:
            handle=self.plot.handles[found];key=handle.xkey or handle.ykey
            if key:self.parameterContextRequested.emit(key,event.pos());event.accept();return
        super().contextMenuEvent(event)

    def keyPressEvent(self,event):
        if not self.plot or not self.plot.handles:return super().keyPressEvent(event)
        if event.key()==Qt.Key_Escape:self._cancel_gesture();event.accept();return
        if event.key() in (Qt.Key_Return,Qt.Key_Enter):
            self._selected=(self._selected+1)%len(self.plot.handles);self.update();event.accept();return
        directions={Qt.Key_Left:-1,Qt.Key_Right:1,Qt.Key_Down:-1,Qt.Key_Up:1}
        if event.key() not in directions:return super().keyPressEvent(event)
        handle=self.plot.handles[self._selected%len(self.plot.handles)]
        key=(handle.xkey if event.key() in (Qt.Key_Left,Qt.Key_Right) else handle.ykey) or handle.xkey or handle.ykey
        from effect_control import AUDIO_FX_PARAMETERS
        spec=next(s for s in AUDIO_FX_PARAMETERS[self.kind] if s.key==key)
        step=spec.step if event.modifiers()&Qt.ShiftModifier else spec.input_steps[0]
        self.parametersEdited.emit({key:self.values[key]+directions[event.key()]*step});event.accept()


class NativeControlRow(QWidget):
    """Native slider with the ``get/set(call=...)`` API used by the live engine."""

    def __init__(
        self, label, minimum, maximum, initial, callback, formatter, step=1, parent=None,
        *, input_steps, label_width=None, value_width=None, column_gap=None,
        tooltip=None, logarithmic=False, fit_value_spacing=False, snap_navigation=False,
        preserve_value_precision=False, neutral_value=None,
        entry_kind=None, entry_context=None, stacked=False,
    ):
        self.minimum, self.maximum, self.step = float(minimum), float(maximum), float(step)
        self.logarithmic = logarithmic
        self.snap_navigation = snap_navigation
        self.preserve_value_precision = preserve_value_precision
        self._reference_value = None
        self._mixed = False
        self._entry_kind = entry_kind
        self._entry_context = entry_context
        self._entry_domain = None
        self.input_steps = dict(zip(("key", "wheel", "track"), input_steps))
        self.callback, self.formatter, self._blocked = callback, formatter, False
        self.label_width = METRICS["form_label_width"] if label_width is None else int(label_width)
        self.value_width = METRICS["form_value_width"] if value_width is None else int(value_width)
        self.column_gap = METRICS["form_column_gap"] if column_gap is None else int(column_gap)
        super().__init__(parent)
        self.slider = NativeSlider(Qt.Horizontal)
        self.slider.setRange(0, self._track_ticks())
        if neutral_value is not None:
            if not self.minimum <= float(neutral_value) <= self.maximum:
                raise ValueError("Slider neutral value outside range")
            self.slider.set_neutral_tick(round((float(neutral_value) - self.minimum) / self.step))
        if len(input_steps) != 3:
            raise ValueError("Slider input_steps must contain key, wheel, track values")
        amounts = tuple(float(amount) for amount in input_steps)
        ticks = tuple(round(amount / self.step) for amount in amounts)
        if any(not math.isclose(tick * self.step, amount, abs_tol=1e-8)
               for amount, tick in zip(amounts, ticks)):
            raise ValueError("Slider input steps must be multiples of the drag tick")
        self.slider.set_input_ticks(*ticks)
        if logarithmic or snap_navigation:
            self.slider.navigation_callback = self._navigate
        self.slider.setFixedHeight(FORM_ROW_HEIGHT)
        value_type = (EditableNumberLabel if entry_kind else
                      AdaptiveValueLabel if fit_value_spacing else ElasticLabel)
        self.value = value_type(preferred_width=self.value_width); self.value.setObjectName("FieldValue"); self.value.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        _layout, self.title = install_three_column_form_row(
            self, label, self.slider, self.value,
            label_width=self.label_width, gap=self.column_gap,
            stacked=stacked,
        )
        self.slider.setAccessibleName(label)
        install_effect_tooltip(tooltip, self, self.title, self.slider, self.value)
        if entry_kind:
            special_hints = {"drum_pan": "drum_pan_hint",
                             "drum_fine": "drum_fine_hint",
                             "drum_amount": "drum_amount_hint",
                             "gain_db": "gain_hint",
                             "reverb_level": "gain_hint",
                             "spatial_mix": "spatial_hint"}
            if entry_kind in special_hints:
                self.value.setToolTip(f"{self.value.toolTip()}\n"
                                      + tr(f"number_entry.{special_hints[entry_kind]}"))
            self._configure_entry()
        self.slider.valueChanged.connect(self._changed)
        self.slider.actionTriggered.connect(self._commit_mixed_action)
        self.slider.valueChanged.disconnect(self.slider._notify_accessible_value)
        self.set(initial, call=False)
        self._configure_accessible_row()

    def add_value_action(self,button):
        """Use spare trailing-cell ink space without changing the form grid."""
        self._value_action=button;button.setParent(self.value)
        self.value.installEventFilter(self)
        self._place_value_action()

    def _place_value_action(self):
        button=self._value_action;hint=button.sizeHint()
        button.setGeometry(max(0,self.value.width()-hint.width()),
                           max(0,(self.value.height()-hint.height())//2),hint.width(),hint.height())
        button.raise_()

    def eventFilter(self,watched,event):
        if watched is self.value and event.type() in (QEvent.Resize,QEvent.Show,QEvent.FontChange,QEvent.StyleChange):
            self._place_value_action()
        return super().eventFilter(watched,event)

    def _configure_accessible_row(self):
        domain = self._entry_domain or numeric_entry_domain('number_integer', self.minimum, self.maximum)
        self.slider.set_accessible_domain(domain, lambda _tick: self.formatter(self.get()),
            read=lambda: domain.to_entry(self.get()),
            write=lambda number: self.set(domain.to_raw(number)),
            step=lambda: abs(domain.to_entry(min(self.maximum, self.minimum+self.step)) - domain.to_entry(self.minimum)))

    def _commit_mixed_action(self, action):
        if action and self._mixed and self.slider.sliderPosition() == self.slider.value():
            self._changed(self.slider.value())

    def _configure_entry(self):
        self._entry_domain = numeric_entry_domain(
            self._entry_kind, self.minimum, self.maximum)
        domain = self._entry_domain
        self.value.configure(
            minimum=domain.minimum, maximum=domain.maximum,
            decimals=domain.decimals, format_input=domain.entry_text,
            read=lambda: self._entry_domain.to_entry(self.get()),
            write=lambda number: self.set(self._entry_domain.to_raw(number)),
            context=self._entry_context, label=self.title.text(),
        )
        self.value.bind_control(self.slider)

    def _navigate(self, direction, gesture):
        """Navigate display-unit steps; optional grid snapping never affects drag."""
        spacing = self.input_steps[gesture]
        value = self.get()
        if self.snap_navigation:
            quotient = value / spacing
            nearest = round(quotient)
            if math.isclose(quotient, nearest, abs_tol=1e-8, rel_tol=0):
                target = (nearest + direction) * spacing
            elif direction > 0:
                target = (math.floor(quotient) + direction) * spacing
            else:
                target = (math.ceil(quotient) + direction) * spacing
        else:
            target = value + direction * spacing
        self.set(target)

    def _track_ticks(self):
        return 1000000 if self.logarithmic else int(round((self.maximum - self.minimum) / self.step))

    def get(self):
        if self.preserve_value_precision and self._reference_value is not None:
            return self._reference_value
        return self._position_value()

    def _position_value(self):
        if not self.logarithmic:
            return self.minimum + self.slider.value() * self.step
        value = self.minimum * (self.maximum / self.minimum) ** (self.slider.value() / 1000000)
        return max(self.minimum, min(self.maximum, round(value / self.step) * self.step))
    def set_maximum(self, value):
        """Update a domain bound without firing an intermediate user edit."""
        previous = self.get()
        self.maximum = max(self.minimum, float(value))
        if self._entry_kind:
            self.value.cancel_editor()
            self._configure_entry()
        self._configure_accessible_row()
        blocked = self.slider.blockSignals(True)
        try:
            self.slider.setMaximum(self._track_ticks())
            self.set(previous, False)
        finally:
            self.slider.blockSignals(blocked)
    def set(self, value, call=True):
        value = max(self.minimum, min(self.maximum, float(value)))
        self.set_mixed(False)
        if self.preserve_value_precision:
            self._reference_value = value
        if self.logarithmic:
            position = (0 if self.maximum <= self.minimum else
                        round(math.log(value / self.minimum) / math.log(self.maximum / self.minimum) * 1000000))
        else:
            position = int(round((value - self.minimum) / self.step))
        self._blocked = True; self.slider.setValue(position); self._blocked = False
        self._sync()
        if call and self.callback: self.callback(self.get())
        self.slider._notify_accessible_value()
    def set_mixed(self, mixed):
        self._mixed = bool(mixed)
        self.slider.setProperty("mixedValue", self._mixed)
        self.slider.set_unknown(self._mixed)
        self.value.setProperty("mixedValue", self._mixed)
        self._sync()

    def _sync(self):
        self.value.setText(tr("channel_editor.mixed_value") if self._mixed else self.formatter(self.get()))
    def _changed(self, _value):
        if not self._blocked:
            self.set_mixed(False)
        if self.preserve_value_precision and not self._blocked:
            self._reference_value = self._position_value()
        self._sync()
        if not self._blocked and self.callback: self.callback(self.get())
        if not self._blocked: self.slider._notify_accessible_value()


def present_numeric_selection(control, values, neutral=0):
    """Shared batch-value contract: no synthetic aggregate is a real value."""
    values = tuple(values)
    common = bool(values) and all(value == values[0] for value in values)
    control.set(values[0] if common else neutral, call=False)
    control.set_mixed(bool(values) and not common)


class NativeLoadBar(QWidget):
    LIGHT_HSL_LIFT = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ratio = 0.; self.dark = False
        self.setFixedHeight(8)
        # This is a pure proportional field.  It has no horizontal floor;
        # when its grid column becomes narrow, its rounded geometry follows.
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    def sizeHint(self): return QSize(0, self.height())
    def minimumSizeHint(self): return QSize(0, self.height())
    def set_theme(self, dark): self.dark = bool(dark); self.update()
    def set_cpu_percent(self, percent):
        ratio = max(0., min(1., float(percent) / 100.))
        if self.ratio != ratio:
            self.ratio = ratio
            self.update()
    @staticmethod
    def ctk_compatible_fill(ratio: float, dark: bool = True) -> QColor:
        """CTk's saturated transfer function, with a Light-only luminance lift.

        The fill follows a saturated green -> yellow -> red path.
        The hue and saturation transfer remains
        identical across themes; Light only raises HSL lightness by 12/255 so
        the filled signal reads correctly against its brighter surface.
        """
        ratio = max(0.0, min(1.0, float(ratio)))
        if ratio <= .5:
            red, green = int(255 * ratio * 2), 255
        else:
            red, green = 255, int(255 * (1 - (ratio - .5) * 2))
        color = QColor(int(red * .9), int(green * .8), 0)
        if dark:
            return color
        hue, saturation, lightness, alpha = color.getHsl()
        return QColor.fromHsl(hue, saturation, min(255, lightness + NativeLoadBar.LIGHT_HSL_LIFT), alpha)
    def paintEvent(self, _event):
        p = widget_palette(self, self.dark); painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(0, 0, -1, -1)
        if rect.width() <= 0 or rect.height() <= 0:
            return
        radius = max(1, min(rect.height() // 2, rect.width() // 2))
        painter.setPen(Qt.NoPen); painter.setBrush(QColor(p.load_track)); painter.drawRoundedRect(rect, radius, radius)
        if self.ratio:
            painter.setBrush(self.ctk_compatible_fill(self.ratio, self.dark)); painter.drawRoundedRect(QRect(rect.left(), rect.top(), max(1, int(rect.width() * self.ratio)), rect.height()), radius, radius)
