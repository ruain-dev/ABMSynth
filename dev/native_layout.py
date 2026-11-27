"""Shared, non-visual layout primitives for the Modern Native UI.

This module owns geometry rules only.  Colours, fonts, audio state, and widget
semantics stay with ``pyside_native_ui.py`` so a layout refactor cannot alter
the synth path.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, QTimer
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QAbstractScrollArea, QScrollArea, QGridLayout, QHBoxLayout, QVBoxLayout, QLabel, QLayout, QSizePolicy, QStyle, QWidget


class NativeWindowPlacement:
    """Main utility's normal frame origin; never an audio or size policy."""
    KEY = 'main_window_placement'

    def __init__(self, serialized=''):
        self.value = None
        self.restoring = False
        try:
            from configuration_data import read_settings_json
            value = read_settings_json(serialized)
            if (isinstance(value, dict) and value.get('version') == 1
                    and all(type(value.get(key)) is int and abs(value[key]) <= 10000000 for key in ('x', 'y'))
                    and all(isinstance(value.get(key), str) and len(value[key]) <= 512
                            for key in ('name', 'serial'))):
                self.value = {key: value[key] for key in ('version', 'x', 'y', 'name', 'serial')}
        except (TypeError, ValueError):
            pass  # Missing/invalid placement never prevents startup.

    @staticmethod
    def origin(available, frame_size, offset=None):
        dx = max(0, available.width() - frame_size.width())
        dy = max(0, available.height() - frame_size.height())
        x, y = offset if offset is not None else (dx // 2, round(dy * .45))
        return QPoint(available.left() + max(0, min(dx, x)),
                      available.top() + max(0, min(dy, y)))

    def target_screen(self, screens, primary):
        if self.value is not None:
            serial = self.value['serial']
            matching = [screen for screen in screens if serial and screen.serialNumber() == serial]
            if len(matching) != 1:
                matching = [screen for screen in screens if screen.name() == self.value['name']]
            if len(matching) == 1:
                return matching[0], (self.value['x'], self.value['y'])
        return primary, None

    def apply(self, window):
        screen, offset = self.target_screen(QApplication.screens(), QApplication.primaryScreen())
        if screen is None:
            return
        self.restoring = True
        try:
            window.setScreen(screen)
            window.move(self.origin(screen.availableGeometry(), window.frameGeometry().size(), offset))
        finally:
            self.restoring = False

    def record(self, window):
        if (self.restoring or not window.isVisible() or window.isMinimized()
                or window.isMaximized() or window.isFullScreen()):
            return
        screen = window.screen()
        if screen is not None:
            frame, area = window.frameGeometry(), screen.availableGeometry()
            self.value = dict(version=1, x=frame.x()-area.x(), y=frame.y()-area.y(),
                              name=screen.name(), serial=screen.serialNumber())

    def serialized(self, window):
        self.record(window)
        return json.dumps(self.value, separators=(',', ':'), ensure_ascii=False) if self.value is not None else ''


# A form control occupies one row slot regardless of its visual shape.  Card
# spacing is independent: changing a gap must never resize the controls.
FORM_ROW_HEIGHT = 28
FORM_COLUMN_GAP = 10
COMPACT_CONTENT_GAP = 6
CARD_GAP = 14
INNER_CARD_GAP = 8
TAB_PAGE_INSET = 12
COMPACT_TAB_PAGE_INSET = 8
CARD_ROW_GAPS = {
    "device": 10,
    "effects": 4,
    "preferences": 10,
    "actions": 8,
    "appearance": 6,
    "log": 8,
    "group": 10,
}


def set_layout(layout, margins=0, spacing=0):
    """Apply the project's one margins/spacing convention to any Qt layout."""
    if isinstance(margins, int):
        margins = (margins,) * 4
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    host=layout.parentWidget()
    configure=getattr(host,'configure_content_layout',None)
    if configure is not None:configure(layout)
    return layout


def control_row_layout(parent=None):
    """Related controls have explicit spacing, independent of their host."""
    row=set_layout(QHBoxLayout(parent),0,FORM_COLUMN_GAP)
    row.setProperty('nativeLayoutRole','control-row')
    return row


class WrappingControlLayout(QLayout):
    """Natural related controls,or opt-in equal cells with bounded spacing.

    Geometry only: widgets keep their own input, focus and accessible state.
    Preferred widths determine wrapping, never a host's horizontal minimum.
    Equal cells center their group;excess width outside maximum_gap stays out.
    """

    def __init__(self, parent=None, *, equal_cells=False, maximum_gap=48, start_aligned=False):
        super().__init__(parent)
        self._items = []
        self.equal_cells=bool(equal_cells)
        self.maximum_gap=None if maximum_gap is None else max(0,int(maximum_gap))
        self.start_aligned=start_aligned
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(FORM_COLUMN_GAP)
        self.setProperty('nativeLayoutRole', 'wrapping-control-row')
        self.setSizeConstraint(QLayout.SetNoConstraint)

    def addItem(self, item):
        self._items.append(item)
        self.invalidate()

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        if not 0 <= index < len(self._items):
            return None
        item = self._items.pop(index)
        self.invalidate()
        return item

    def expandingDirections(self):
        return Qt.Orientations()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, max(0, width), 0), measure=True)

    def minimumSize(self):
        return QSize(0, 0)

    def sizeHint(self):
        hints = [self._hint(item) for item in self._visible_items()]
        margins = self.contentsMargins()
        width = (max((h.width() for h in hints), default=0)*len(hints) if self.equal_cells
                 else sum(h.width() for h in hints))
        width += max(0,len(hints)-1)*self.spacing()
        return QSize(width+margins.left()+margins.right(),
                     max((h.height() for h in hints), default=0)+margins.top()+margins.bottom())

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, measure=False)

    def _visible_items(self):
        return [item for item in self._items
                if item.widget() is None or not item.widget().isHidden()]

    @staticmethod
    def _hint(item):
        # Ignored horizontal policy must not erase a control's normal hint.
        return item.widget().sizeHint() if item.widget() is not None else item.sizeHint()

    def _arrange(self, rect, *, measure):
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        available = max(0, area.width())
        if self.equal_cells:
            return self._arrange_equal(area,measure=measure)
        x, y, line_height = 0, 0, 0
        direction = self.parentWidget().layoutDirection() if self.parentWidget() else Qt.LeftToRight
        for item in self._visible_items():
            hint = self._hint(item)
            width = min(available, max(0, hint.width()))
            if x and x+width > available:
                x, y, line_height = 0, y+line_height+self.spacing(), 0
            height = max(0, item.heightForWidth(width) if item.hasHeightForWidth() else hint.height())
            if not measure:
                logical = QRect(area.x()+x, area.y()+y, width, height)
                item.setGeometry(QStyle.visualRect(direction, area, logical))
            x += width+self.spacing()
            line_height = max(line_height, height)
        return margins.top()+y+line_height+margins.bottom()

    def _arrange_equal(self,area,*,measure):
        items=self._visible_items();margins=self.contentsMargins()
        if not items:return margins.top()+margins.bottom()
        available=max(0,area.width());hints=[self._hint(item) for item in items]
        cell=min(available,max(h.width() for h in hints))
        count=max(1,min(len(items),(available+self.spacing())//max(1,cell+self.spacing())))
        if count>2 and len(items)%count==1:
            count-=1
        y=0;direction=self.parentWidget().layoutDirection() if self.parentWidget() else Qt.LeftToRight
        for start in range(0,len(items),count):
            line=items[start:start+count];n=len(line)
            if self.maximum_gap is None:
                gap = self.spacing()
                pitch, remainder = divmod(available + gap, count)
                widths = [pitch - gap + (index < remainder) for index in range(n)]
                x = 0
            else:
                gap = min(self.maximum_gap, max(0, (available - n * cell) // (n - 1))) if n > 1 else 0
                used = n * cell + max(0, n - 1) * gap
                x = 0 if self.start_aligned else max(0, (available - used) // 2)
                widths = [cell] * n
            height = max(max(0, item.heightForWidth(width) if item.hasHeightForWidth() else self._hint(item).height())
                         for item, width in zip(line, widths))
            if not measure:
                for item, width in zip(line, widths):
                    geometry = QRect(area.x() + x, area.y() + y, width, height)
                    item.setGeometry(QStyle.visualRect(direction, area, geometry))
                    x += width + gap
            y+=height+self.spacing()
        return margins.top()+y-self.spacing()+margins.bottom()


class FormRowLayout(WrappingControlLayout):
    """Aligned caption and wrapping field, stacked when their hints do not fit."""

    def sizeHint(self):
        hints = [self._hint(item) for item in self._items]
        return QSize(sum(h.width() for h in hints) + self.spacing(),
                     max((h.height() for h in hints), default=0))

    def _arrange(self, rect, *, measure):
        if self.count() != 2:
            return super()._arrange(rect, measure=measure)
        caption, field = self._items
        caption_hint, field_hint = self._hint(caption), self._hint(field)
        width = max(0, rect.width())
        stacked = width < caption_hint.width() + self.spacing() + field_hint.width()
        caption_width = min(width, caption_hint.width())
        field_width = width if stacked else max(0, width-caption_width-self.spacing())
        field_height = max(0, field.heightForWidth(field_width)
                           if field.hasHeightForWidth() else field_hint.height())
        caption_height = max(FORM_ROW_HEIGHT, caption.heightForWidth(caption_width)
                             if caption.hasHeightForWidth() else caption_hint.height())
        height = (caption_height + COMPACT_CONTENT_GAP + field_height if stacked
                  else max(caption_height, field_height))
        if not measure:
            direction = self.parentWidget().layoutDirection()
            caption.setGeometry(QStyle.visualRect(direction, rect,
                QRect(rect.x(), rect.y(), caption_width, caption_height)))
            field.setGeometry(QStyle.visualRect(direction, rect,
                QRect(rect.x() if stacked else rect.x()+caption_width+self.spacing(),
                      rect.y()+caption_height+COMPACT_CONTENT_GAP if stacked else rect.y(),
                      field_width, field_height)))
        return height


class ContextActionLayout(WrappingControlLayout):
    """Flexible context + trailing commands;stack only under width pressure.

    Preferred context width is a reflow threshold,never a minimum. This layout
    owns geometry only;the widgets keep their state,signals and tab chain.
    """
    def __init__(self,parent=None,*,preferred_context_width=360):
        super().__init__(parent)
        self.preferred_context_width=preferred_context_width
        self.setProperty('nativeLayoutRole','context-action-row')

    def sizeHint(self):
        if self.count()!=2:return super().sizeHint()
        context,actions=(self._hint(item) for item in self._items)
        return QSize(self.preferred_context_width+actions.width()+self.spacing(),max(context.height(),actions.height()))

    def _arrange(self,rect,*,measure):
        if self.count()!=2:return super()._arrange(rect,measure=measure)
        context,actions=self._items
        width=max(0,rect.width())
        action_layout=actions.widget().layout() if actions.widget() is not None else None
        if isinstance(action_layout,QHBoxLayout):
            # Measure current visible commands without invalidating layouts
            # from a height query. The host hint can retain a stale hidden gap.
            visible=[action_layout.itemAt(i) for i in range(action_layout.count())
                     if not action_layout.itemAt(i).isEmpty()]
            hints=[self._hint(item) for item in visible];m=action_layout.contentsMargins()
            action_hint=QSize(sum(max(0,h.width()) for h in hints)+max(0,len(hints)-1)*action_layout.spacing()+m.left()+m.right(),
                max((max(h.height(),item.widget().minimumHeight() if item.widget() else 0) for item,h in zip(visible,hints)),default=0)+m.top()+m.bottom())
        else:action_hint=self._hint(actions)
        action_width=min(width,max(0,action_hint.width()))
        stacked=width<self.preferred_context_width+action_width+self.spacing()
        context_width=width if stacked else max(0,width-action_width-self.spacing())
        context_height=max(0,context.heightForWidth(context_width) if context.hasHeightForWidth() else self._hint(context).height())
        action_height=max(0,actions.heightForWidth(action_width) if actions.hasHeightForWidth() else action_hint.height())
        height=context_height+COMPACT_CONTENT_GAP+action_height if stacked else max(context_height,action_height)
        if not measure:
            direction=self.parentWidget().layoutDirection() if self.parentWidget() else Qt.LeftToRight
            context.setGeometry(QStyle.visualRect(direction,rect,QRect(rect.x(),rect.y(),context_width,context_height)))
            actions.setGeometry(QStyle.visualRect(direction,rect,QRect(rect.right()-action_width+1,
                rect.y()+context_height+COMPACT_CONTENT_GAP if stacked else rect.y(),action_width,action_height)))
        return height


def bordered_page_layout(layout, spacing=0, *, compact=False):
    """Inset the contents of a visible tab pane, independently of child cards.

    This is spacing, never a minimum width. Borderless embedded tabs keep
    their ordinary zero-margin row/form layouts instead.
    """
    inset=COMPACT_TAB_PAGE_INSET if compact else TAB_PAGE_INSET
    set_layout(layout,inset,spacing)
    layout.setProperty('nativeLayoutRole','bordered-page-compact' if compact else 'bordered-page')
    return layout


def invalidate_layout_chain(widget):
    """Publish a changed content extent before synchronous parent measurement.

    updateGeometry schedules layout work; callers that immediately measure a
    card/window must also discard ancestor height-for-width caches now.
    This changes hints, never user geometry or horizontal minimums.
    """
    while widget is not None:
        layout=widget.layout()
        if layout is not None:layout.invalidate()
        widget.updateGeometry()
        if widget.isWindow():break
        widget=widget.parentWidget()


def control_grid_layout(parent=None):
    """Compact form rows keep distinct horizontal and vertical rhythm."""
    grid=set_layout(QGridLayout(parent),0,COMPACT_CONTENT_GAP)
    grid.setHorizontalSpacing(FORM_COLUMN_GAP)
    grid.setProperty('nativeLayoutRole','control-grid')
    return grid


class PeerCardGridLayout(QGridLayout):
    """Peer shells fill each row. Content layouts own their top alignment."""

    def addWidget(self, widget, row, column, *placement):
        placement = list(placement)
        if placement and isinstance(placement[-1], Qt.AlignmentFlag):
            placement[-1] &= ~Qt.AlignVertical_Mask
        policy = widget.sizePolicy()
        if policy.verticalPolicy() in (QSizePolicy.Maximum, QSizePolicy.Preferred):
            policy.setVerticalPolicy(QSizePolicy.Minimum)
            widget.setSizePolicy(policy)
        super().addWidget(widget, row, column, *placement)

    def setGeometry(self, rect):
        headers = {}
        for index in range(self.count()):
            item = self.itemAt(index)
            widget = item.widget()
            row, _column, row_span, _column_span = self.getItemPosition(index)
            header = getattr(widget, 'header', None)
            if row_span == 1 and header is not None and header.layout() is not None:
                headers.setdefault(row, []).append(header)
        for row_headers in headers.values():
            height = max(header.layout().totalSizeHint().height() for header in row_headers)
            for header in row_headers:
                if header.minimumHeight() != height or header.maximumHeight() != height:
                    header.setFixedHeight(height)
        super().setGeometry(rect)


def card_grid_layout(parent=None):
    """Peer cards inside a bordered group share one compact gap on both axes."""
    grid=set_layout(PeerCardGridLayout(parent),0,INNER_CARD_GAP)
    grid.setProperty('nativeLayoutRole','card-grid')
    return grid


class PeerCardStackLayout(QVBoxLayout):
    """Keep the painted peer gap exact when Qt distributes residual height."""

    def setGeometry(self, rect):
        super().setGeometry(rect)
        top = self.contentsRect().top()
        for index in range(self.count()):
            item = self.itemAt(index)
            if item.isEmpty():
                continue
            geometry = item.geometry()
            if geometry.top() != top:
                geometry.moveTop(top)
                item.setGeometry(geometry)
            top = item.geometry().bottom() + 1 + self.spacing()


def card_stack_layout(parent=None, *, margins=0):
    """Stack peer cards or card grids with the same gap used inside a grid."""
    stack = set_layout(PeerCardStackLayout(parent), margins, INNER_CARD_GAP)
    stack.setAlignment(Qt.AlignTop)
    stack.setProperty('nativeLayoutRole', 'card-stack')
    return stack


def make_horizontally_elastic(widget: QWidget, vertical=QSizePolicy.Fixed) -> QWidget:
    """Keep a normal preferred size while allowing width to compress to 0px."""
    widget.setMinimumWidth(0)
    policy = widget.sizePolicy()
    policy.setHorizontalPolicy(QSizePolicy.Ignored)
    policy.setVerticalPolicy(vertical)
    widget.setSizePolicy(policy)
    return widget


def install_three_column_form_row(
    host: QWidget,
    caption: str,
    field: QWidget,
    trailing: QWidget,
    *,
    label_width: int,
    gap: int,
    stacked: bool = False,
):
    """Install the shared ``label | flexible field | trailing value`` grid.

    Native effect and instrument-inspector rows share this alignment,
    compression and height contract. Only the centre field stretches; all
    three cells can yield width. The caller owns field semantics and the
    trailing widget's preferred width.
    """
    layout = set_layout(QGridLayout(host), (0, 0, 0, 0), gap)
    layout.setColumnStretch(1, 1)
    label = FormCaptionLabel(caption, field, preferred_width=label_width)
    label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    make_horizontally_elastic(field)
    if stacked:
        label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(label,0,0,1,2);layout.addWidget(trailing,0,2)
        layout.addWidget(field,1,0,1,3)
        host.setFixedHeight(FORM_ROW_HEIGHT+max(label.sizeHint().height(),trailing.sizeHint().height())+gap)
    else:
        layout.addWidget(label, 0, 0)
        layout.addWidget(field, 0, 1)
        layout.addWidget(trailing, 0, 2)
        host.setFixedHeight(FORM_ROW_HEIGHT)
    return layout, label


def reflow_card_columns(card,columns,preferred_widths,*,force_stack=False,start_row=1):
    """Balance columns while their contents fit, then stack without a width floor."""
    layout=card.layout();margins=layout.contentsMargins()
    gaps=max(0,len(columns)-1)*layout.horizontalSpacing()
    available=max(0,card.width()-margins.left()-margins.right()-gaps)
    stacked=force_stack or available<sum(preferred_widths)
    if card.property('settingsStacked')!=stacked:
        card.setProperty('settingsStacked',stacked)
        for i,column in enumerate(columns):
            layout.removeWidget(column);layout.addWidget(column,i+start_row if stacked else start_row,0 if stacked else i,Qt.AlignTop)
    if stacked:
        stretches=[1]+[0]*(len(columns)-1)
    else:
        stretches=list(preferred_widths)
        # Reserve space for the longest columns first. Divide the rest evenly.
        widest_first=sorted(range(len(columns)),key=preferred_widths.__getitem__,reverse=True)
        for remaining,index in zip(range(len(columns),0,-1),widest_first):
            stretches[index]=max(preferred_widths[index],available//remaining)
            available-=stretches[index]
    for i,stretch in enumerate(stretches):
        layout.setColumnStretch(i,stretch)
    return stacked


class ElasticLabel(QLabel):
    """Text with a preferred width but no horizontal minimum-size floor."""

    def __init__(self, text="", parent=None, preferred_width=0):
        super().__init__(text, parent)
        self._preferred_width = max(0, int(preferred_width))

    def set_preferred_width(self, width: int):
        width = max(0, int(width))
        if self._preferred_width == width:
            return
        self._preferred_width = width
        self.updateGeometry()

    def sizeHint(self):
        hint = super().sizeHint()
        return QSize(self._preferred_width or hint.width(), hint.height())

    def minimumSizeHint(self):
        return QSize(0, 0)


class FormCaptionLabel(ElasticLabel):
    """Form captions fit their current text before the flexible field grows.

    The common column hint remains the normal baseline. Longer captions use
    their actual styled font's ink width, without creating a minimum width.
    QLabel invalidates its layout hint on font/text/style changes.
    """

    def __init__(self, text, field, *, preferred_width=0):
        super().__init__(text, preferred_width=preferred_width)
        self.setObjectName('FieldLabel')
        bind_field_enabled(field, self)

    def sizeHint(self):
        hint = super().sizeHint()
        metrics = self.fontMetrics()
        natural = max((max(metrics.horizontalAdvance(line), metrics.boundingRect(line).width())
                       for line in self.text().splitlines()), default=0)
        return QSize(max(hint.width(), natural + 2), hint.height())


class _FieldEnabledBinding(QObject):
    def __init__(self, field, widget):
        super().__init__(widget)
        self._widget = widget
        field.installEventFilter(self)
        widget.setEnabled(field.isEnabled())

    def eventFilter(self, watched, event):
        if event.type() == QEvent.EnabledChange:
            self._widget.setEnabled(watched.isEnabled())
        return super().eventFilter(watched, event)


def bind_field_enabled(field, *widgets):
    """Propagate the effective field state to its sibling caption or readout."""
    for widget in widgets:
        _FieldEnabledBinding(field, widget)


def align_form_labels(labels, preferred_width, gap=0):
    """One measured preferred caption column, without a minimum-width floor.

    Call after assigning the owning form's font. Labels remain compressible;
    this only prevents the normal form from reserving less than its text ink.
    """
    labels = tuple(label for label in labels if isinstance(label, ElasticLabel))
    width = max(int(preferred_width), max((
        label.fontMetrics().horizontalAdvance(label.text()) + int(gap)
        for label in labels), default=0))
    for label in labels:
        label.set_preferred_width(width)
    return width


class ElasticPage(QWidget):
    """Keep layout preferred dimensions without per-page compression floors."""

    def minimumSizeHint(self):
        return QSize(0, 0)


class ElasticPanel(QWidget):
    """A container that can advertise a normal width without reserving it."""

    def __init__(self, parent=None, preferred_width=0):
        super().__init__(parent)
        self._preferred_width = max(0, int(preferred_width))

    def set_preferred_width(self, width: int):
        width = max(0, int(width))
        if self._preferred_width == width:
            return
        self._preferred_width = width
        self.updateGeometry()

    def sizeHint(self):
        return QSize(self._preferred_width, self.height())

    def minimumSizeHint(self):
        return QSize(0, 0)


class WidthMeasuredPanel(QWidget):
    """Scroll content whose vertical hints use its real, elastic width.

    QWidget's preferred-width wrapping can inflate the minimum height too,
    so correcting only a window's fit equation still leaves phantom scroll.
    """

    def set_height_override(self, height):
        """Animation-only measured baseline; None restores width-aware hints."""
        if getattr(self, "_height_override", None) == height:
            return
        self._height_override = height
        self.updateGeometry()

    def set_height_source(self, measure):
        """Aggregate mounted child panes at their actual widths, not cached hints."""
        self._height_source=measure
        self.updateGeometry()

    def _measured_height(self):
        override = getattr(self, "_height_override", None)
        if override is not None:
            return override
        source=getattr(self,'_height_source',None)
        if source is not None:
            return max(0,int(source()))
        layout = self.layout()
        if layout is None:
            return 0
        return max(0, layout.totalHeightForWidth(self.width()) if layout.hasHeightForWidth()
                   else layout.totalSizeHint().height())

    def sizeHint(self):
        return QSize(super().sizeHint().width(), self._measured_height())

    def minimumSizeHint(self):
        return QSize(0, self._measured_height())

    def resizeEvent(self,event):
        super().resizeEvent(event)
        layout=self.layout()
        # Our vertical hint depends on the ACTUAL width. Qt does not infer
        # that dependency from a Python sizeHint override. Invalidate it when
        # wrapping width changes,including inside a subsection's grid.
        if event.size().width()!=event.oldSize().width() and layout is not None and layout.hasHeightForWidth():
            self.updateGeometry()


def reveal_expanded_region(widget, preview_height=56):
    """Expose the start of an explicit expansion,not an entire tall form.

    Window size/focus and horizontal scroll remain owned by the user. Qt
    ensureVisible applies the minimum vertical delta for this bounded point.
    Already visible content and collapse never move the viewport.
    """
    if not widget.isVisible() or widget.height()<=0:return
    area=widget.parentWidget()
    while area is not None and not isinstance(area,QScrollArea):area=area.parentWidget()
    if area is None or area.widget() is None:return
    point=widget.mapTo(area.viewport(),QPoint(0,0))
    visible=min(widget.height(),preview_height)
    if point.y()+visible<=area.viewport().height():return
    content_point=widget.mapTo(area.widget(),QPoint(0,visible))
    horizontal=area.horizontalScrollBar().value()
    area.ensureVisible(horizontal,content_point.y(),0,0)
    area.horizontalScrollBar().setValue(horizontal)


def automatic_window_fit_allowed(window, *, opening=False):
    """Opening convenience yields to chosen geometry,window state and scroll.

    The native WM_SIZING/SC_SIZE boundary records a real user gesture. A Qt
    Resize alone is not evidence: layout,DPI and our own fits also emit it.
    This is window-lifetime intent,not persisted geometry or a width floor.
    A known pending DPI correction also owns its transient client geometry.
    """
    if window.property('nativeUserGeometryChosen') or window.windowState():return False
    dpi_guard=getattr(window,'_native_dpi_size_guard',None)
    if dpi_guard is not None and dpi_guard._target is not None:return False
    # The provisional layout can set a scrollbar before a dialog opens.
    # Only keep a scroll position from an already presented window.
    return opening or not any(area.window() is window and area.isVisible() and
                   (area.verticalScrollBar().value()>0 or area.horizontalScrollBar().value()>0)
                   for area in window.findChildren(QAbstractScrollArea))


def fit_plain_dialog_contents(dialog, preferred_width=None, *, opening=False, grow_only=False):
    """Measured visible layout for non-scroll task/mapping dialogs.

    Explicit content/opening calls only, never a per-frame resize loop. HFW
    includes wrapped task text; preferred width remains a hint, not a floor.
    """
    if not automatic_window_fit_allowed(dialog, opening=opening):return dialog.size()
    dialog.ensurePolished()
    root=dialog.layout()
    if root is None: return dialog.size()
    root.invalidate(); root.activate()
    width=int(preferred_width) if opening and preferred_width is not None else dialog.width()
    frame=dialog.frameGeometry().size()-dialog.size()
    available=dialog.screen().availableGeometry()
    if QApplication.platformName()!='offscreen':
        width=min(width,available.width()-max(0,frame.width()))
    height=root.totalHeightForWidth(max(1,width)) if root.hasHeightForWidth() else root.totalSizeHint().height()
    if grow_only: height=max(height,dialog.height())
    if QApplication.platformName()!='offscreen': height=min(height,available.height()-max(0,frame.height()))
    target=QSize(max(1,width),max(1,height))
    if target!=dialog.size(): dialog.resize(target)
    root.activate()
    dialog._measured_content_height=height
    return target


def fit_dialog_contents(dialog, scroll, preferred_width, *, opening=False, shift_to_fit=False,grow_only=False):
    """Fit actual visible content; preferred widths never become minimums.

    QScrollArea's viewport hint is not the content height. Measure its widget
    at the proposed width, then add only real root siblings/footer/margins.
    A constrained work area or later user resize may legitimately scroll.
    """
    if not automatic_window_fit_allowed(dialog):return dialog.size()
    dialog.ensurePolished()
    root = dialog.layout()
    root.activate()
    margins = root.contentsMargins()
    virtual = QApplication.platformName() == 'offscreen'
    available = dialog.screen().availableGeometry()
    frame = dialog.frameGeometry().size()-dialog.size()
    width = int(preferred_width) if opening else dialog.width()
    if not virtual:
        width = min(width, available.width()-max(0,frame.width()))
    content = scroll.widget()
    viewport_margins=scroll.viewportMargins()
    content_width = max(0,width-margins.left()-margins.right()-2*scroll.frameWidth()
                        -viewport_margins.left()-viewport_margins.right())
    content.resize(content_width,content.height())
    content.ensurePolished()
    content.layout().activate()
    layout = content.layout()
    override=getattr(content,'_height_override',None)
    height = (content.minimumSizeHint().height() if isinstance(content,WidthMeasuredPanel) else
              override if override is not None else
              layout.totalHeightForWidth(content_width) if layout.hasHeightForWidth()
              else layout.totalSizeHint().height())
    height = (max(0,height)+2*scroll.frameWidth()+margins.top()+margins.bottom()
              +viewport_margins.top()+viewport_margins.bottom())
    visible = 0
    for index in range(root.count()):
        item = root.itemAt(index)
        widget = item.widget()
        if widget is scroll:
            visible += 1
        elif widget is not None and not widget.isHidden():
            visible += 1
            hint = widget.heightForWidth(content_width) if widget.hasHeightForWidth() else widget.sizeHint().height()
            height += max(0,hint)
        elif item.layout() is not None:
            visible += 1
            height += item.layout().totalSizeHint().height()
    height += max(0,visible-1)*root.spacing()
    if grow_only and not opening:height=max(height,dialog.height())
    fitted_top = None
    if not virtual:
        top=available.top() if opening else max(available.top(),dialog.frameGeometry().top())
        if shift_to_fit:
            # Preserve the top when possible; use unused space above only if
            # otherwise fitting content would be artificially forced to scroll.
            full_capacity=max(1,available.height()-max(0,frame.height()))
            height=min(height,full_capacity)
            top=max(available.top(),min(top,available.bottom()-height-max(0,frame.height())+1))
            fitted_top=top
        height = min(height,available.bottom()-top+1-max(0,frame.height()))
    # Required geometry is visible to audits; no unexplained safety padding.
    dialog._measured_content_height = height
    dialog.resize(max(1,width),max(1,height))
    root.activate()
    # Final layout can change width (for example a disappearing scrollbar)
    # and invalidate wrapped content hints. Resolve against the REAL viewport,
    # then remove any avoidable scroll using the actual deficit. No guessed
    # safety padding and no event pumping/reentrant callbacks are required.
    if isinstance(content,WidthMeasuredPanel):
        content.layout().invalidate()
        natural=content._measured_height()
        capacity=scroll.viewport().height()
        if natural>capacity:
            deficit=natural-capacity
            limit=(height+deficit if virtual else
                   max(1,available.bottom()-(fitted_top if fitted_top is not None else
                          available.top() if opening else max(available.top(),dialog.frameGeometry().top()))
                       +1-max(0,frame.height())))
            corrected=min(height+deficit,limit)
            if corrected>height:
                height=corrected
                dialog.resize(max(1,width),max(1,height));root.activate()
                dialog._measured_content_height=height
    if fitted_top is not None and dialog.frameGeometry().top()!=fitted_top:
        dialog.move(dialog.x(),fitted_top)
    if opening and not virtual and dialog.frameGeometry().bottom()>available.bottom():
        dialog.move(dialog.x(),max(available.top(),available.bottom()-dialog.frameGeometry().height()+1))
    return QSize(width,height)


class TopClippedCanvas(QWidget):
    """Viewport for a manually top-anchored stack with no width floor."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.content = None
        self._content_floor_height = 0
        self._preferred_width = 0
        self.resize_callback = None
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

    def set_content(self, content):
        self.content = content
        content.setParent(self)
        QTimer.singleShot(0, self.sync_content_geometry)

    def sync_content_geometry(self):
        if self.content is not None:
            self.content.setGeometry(0, 0, max(0, self.width()), max(self.height(), self._content_floor_height))

    def lock_natural_width(self):
        """Compatibility hook; width floors are intentionally unsupported."""
        self.sync_content_geometry()

    def set_content_floor_width(self, _width: int):
        """Compatibility hook; content width must always yield to its viewport."""
        self.sync_content_geometry()

    def set_preferred_width(self, width: int):
        width = max(0, int(width))
        if self._preferred_width == width:
            return
        self._preferred_width = width
        self.updateGeometry()

    def sizeHint(self):
        return QSize(self._preferred_width, 0)

    def minimumSizeHint(self):
        return QSize(0, 0)

    def resizeEvent(self, event):
        self.sync_content_geometry()
        if self.resize_callback is not None:
            self.resize_callback()
        return super().resizeEvent(event)


@dataclass(frozen=True)
class StackItem:
    """A card in a top-anchored stack.

    ``fill_tail`` designates the one bottom item that receives all remaining
    height.  Other cards preserve their preferred height until the available
    tail is exhausted.
    """

    widget: QWidget
    preferred_height: int | Callable[[], int]
    fill_tail: bool = False

    def preferred(self) -> int:
        value = self.preferred_height() if callable(self.preferred_height) else self.preferred_height
        return max(0, int(value))


def place_top_anchored_stack(width: int, height: int, gap: int, items: tuple[StackItem, ...]):
    """Place cards from the top; only the tail loses height under pressure."""
    y, remaining = 0, max(0, int(height))
    width = max(0, int(width))
    for index, item in enumerate(items):
        item_height = remaining if item.fill_tail else min(remaining, item.preferred())
        item.widget.setGeometry(0, y, width, item_height)
        y += item_height
        remaining -= item_height
        if remaining > 0 and index < len(items) - 1:
            step = min(max(0, int(gap)), remaining)
            y += step
            remaining -= step

