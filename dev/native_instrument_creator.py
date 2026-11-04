"""Instrument-authoring view and its bounded file/preview jobs.

Separate from group mapping: this view edits source sample data, owns an
unsaved draft and local preview, but never owns a live synth/channel handle.
"""
from __future__ import annotations

import math
import tempfile
import wave
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QThread, QUrl, Signal, QSize, QTimer, QStandardPaths, QRectF, QEvent, QPoint, QPointF, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPen, QKeySequence, QShortcut, QFontMetrics, QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QTabWidget,
    QLineEdit, QLabel, QListWidget, QFileDialog, QSizePolicy, QLayout, QMessageBox, QApplication, QScrollBar, QStackedWidget)
from native_layout import set_layout, ElasticLabel, WidthMeasuredPanel, FORM_ROW_HEIGHT, ContextActionLayout, control_row_layout, control_grid_layout, card_grid_layout, align_form_labels, bordered_page_layout, automatic_window_fit_allowed
from native_widgets import (NativeCard, NativeControlRow, NativeKnobField, NativeNumberBox, AnimatedComboBox, NativeOptionalLabel,
    NativeDecimalBox, NativeMessageBox, NativeTransportButton, NativeToggleSwitch, METRICS, make_action_button, widget_palette, apply_native_text_metrics,
    request_visual_frame,remove_visual_frame,compositor_visual_clock,NativeMenu,make_control_section,
    flat_tab_pane_stylesheet,make_navigation_tabs,refresh_native_pointer_feedback,native_wheel_steps,set_native_cursor,install_effect_tooltip,
    NativeWindowSizeMotion,make_instrument_audition)
from i18n import tr
from effect_presentation import numeric_entry_domain
from audio_output import AUDITION_NOTE_VELOCITY
from error_reporting import record as record_support_event
from instrument_creator import (InstrumentDraft, WaveSettings, SampleZone, WAVE_POINTS, MAX_ZONES, MAX_FILE_BYTES,
    NATIVE_SOUND_PARAMETERS,MAX_PREVIEW_BYTES,preview_note_duration,
    decode_sample, wave_preset, render_preview, draft_record, record_draft, compile_record,
    prepared_zone, analyze_sample,render_compiled_preview)


def note_caption(note,*,frequency=False):
    pitch=('C','C♯','D','D♯','E','F','F♯','G','G♯','A','A♯','B')[note%12]
    solfege=tr('creator.note_'+str(note%12))
    return tr('creator.note_frequency' if frequency else 'creator.note_caption',
              note=f'{pitch}{note//12-1}',syllable=solfege,**({'hz':440*2**((note-69)/12)} if frequency else {}))


class IdentityChoice(AnimatedComboBox):
    valueChanged=Signal(int)

    def __init__(self,label,parent=None):
        super().__init__(parent);self.setMinimumWidth(0);self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
        self.setProperty('fitPreferenceContents',True)
        self.setAccessibleName(label)
        self.currentIndexChanged.connect(lambda _:self.valueChanged.emit(self.value()))

    def value(self):return self.currentData() if self.currentData() is not None else 0
    def setValue(self,value):self.setCurrentIndex(self.findData(int(value)))


class NoteChoice(IdentityChoice):
    def __init__(self,label,*,frequency=False,parent=None):
        super().__init__(label,parent)
        self._meaning='';self._drum=False;self._drum_mode=None;self._frequency=frequency
        for note in range(128):self.addItem(note_caption(note,frequency=frequency),note)
        self.valueChanged.connect(self._note_tip);self._note_tip(self.value())

    def _note_tip(self,note):
        from effect_presentation import drum_key_caption
        self.setToolTip(self._meaning or tr('creator.note_identity',
            note=drum_key_caption(note,mode=self._drum_mode) if self._drum else note_caption(note),number=note))

    def set_meaning(self,text):self._meaning=text;self._note_tip(self.value())

    def set_drum(self,drum,mode=None):
        if (self._drum,self._drum_mode)==(drum,mode):return
        self._drum,self._drum_mode=drum,mode
        from effect_presentation import drum_key_caption
        for note in range(128):self.setItemText(note,drum_key_caption(note,mode=mode) if drum else note_caption(note,frequency=self._frequency))


class EnvelopeTimeRow(NativeControlRow):
    """Zero-inclusive time travel, precise short attacks, shared value editor."""
    def _track_ticks(self):return 10000

    def _position_value(self):return round(self.maximum*(self.slider.value()/10000)**3,3)

    def set(self,value,call=True):
        value=max(0,min(self.maximum,float(value)))
        self.set_mixed(False);self._reference_value=value
        self._blocked=True
        self.slider.setValue(round((value/self.maximum)**(1/3)*10000) if self.maximum else 0)
        self._blocked=False;self._sync()
        if call and self.callback:self.callback(self.get())
        self.slider._notify_accessible_value()


class InstrumentWaveCanvas(QWidget):
    waveEdited = Signal(object)
    markerEdited = Signal(str, int)
    contentChanged = Signal()

    def __init__(self, parent=None, *, editable=False):
        super().__init__(parent)
        self.editable=editable;self.values=wave_preset('sine')[:,None]
        self.zone=None;self._last=None;self._drag_marker=None;self._before=None
        self.setMinimumSize(0,0)
        self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Expanding)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(tr('creator.wave_canvas' if editable else 'creator.sample_canvas'))
        self.setToolTip(tr('creator.wave_tip' if editable else 'creator.sample_tip'))
        self._path_key=None;self._paths=[]

    def sizeHint(self):return QSize(380,150)
    def minimumSizeHint(self):return QSize(0,80)

    def set_wave(self, values):
        self.values=np.asarray(values,np.float32).reshape(-1,1).copy()
        self.zone=None;self._path_key=None;self.update();self.contentChanged.emit()

    def set_zone(self, zone):
        self.zone=zone
        self.values=zone.pcm if zone else np.zeros((4,1),np.float32)
        self._path_key=None;self.update();self.contentChanged.emit()

    @staticmethod
    def amplitude_y(value,center,half_height):
        """All authoring waveforms use signed full scale,never peak fitting."""
        return center-max(-1.,min(1.,float(value)))*half_height

    @staticmethod
    def wave_lane(rect,channel=0,channels=1):
        slot=max(1.,rect.height()/channels)
        return rect.top()+slot*(channel+.5),slot*.44

    @staticmethod
    def wave_pen(palette):return QPen(QColor(palette.accent),1.)

    @staticmethod
    def overview_path(values,rect,gain=1.):
        path=QPainterPath();count=len(values)
        if not count:return path
        pixels=max(1,int(rect.width()));bins=np.arange(pixels+1)*count//pixels
        for x in range(pixels):
            segment=values[min(count-1,bins[x]):max(bins[x]+1,bins[x+1])]
            peak=min(1.,float(np.max(np.abs(segment)))*gain)*rect.height()*.45
            path.moveTo(x+rect.left(),rect.center().y()-peak);path.lineTo(x+rect.left(),rect.center().y()+peak)
        return path

    @staticmethod
    def paint_overview(painter,palette,rect,path,view):
        # The view locator is a background,not an opaque mask over audio ink.
        a,b=view
        window=QRectF(rect.left()+a*rect.width(),rect.top(),max(2,(b-a)*rect.width()),rect.height())
        painter.save();painter.setClipRect(rect);painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(window,QColor(palette.accent_soft))
        painter.setPen(QPen(QColor(palette.muted),1));painter.drawPath(path)
        painter.setBrush(Qt.NoBrush);painter.setPen(QPen(QColor(palette.accent),1));painter.drawRect(window)
        painter.restore()

    @classmethod
    def wave_path(cls,values,left,width,center,half_height,*,gain=1.,pixel_ratio=1.):
        """Display-only,time-accurate PCM geometry shared by all authoring views.

        Resolved samples use monotone Hermite segments (no overshoot). Dense
        bins retain first/min/max/last in time order at their actual sample x,
        rather than moving both extrema onto one integer pixel column.
        """
        path=QPainterPath();count=len(values)
        if not count:return path
        pixels=max(1,math.ceil(max(1.,width)*pixel_ratio))
        resolved=count<=4*pixels
        if resolved:indices=np.arange(count)
        else:
            edges=np.arange(pixels+1,dtype=np.int64)*count//pixels
            selected=[]
            for start,end in zip(edges[:-1],edges[1:]):
                segment=values[start:end]
                selected.extend((int(start),int(start+np.argmin(segment)),int(start+np.argmax(segment)),int(end-1)))
            indices=np.unique(selected)
        levels=np.clip(np.asarray(values[indices],np.float64)*gain,-1.,1.)
        xs=left+indices/max(1,count-1)*max(0.,width-1.)
        ys=center-levels*half_height
        path.moveTo(float(xs[0]),float(ys[0]))
        if resolved and count>2:
            # Uniform-grid PCHIP tangents. Adjacent extrema have zero slope;
            # step/impulse corners cannot ring beyond their real sample range.
            slopes=np.diff(ys);tangents=np.zeros(count,np.float64)
            same=slopes[:-1]*slopes[1:]>0
            np.divide(2*slopes[:-1]*slopes[1:],slopes[:-1]+slopes[1:],out=tangents[1:-1],where=same)
            for index,near,next_slope in ((0,slopes[0],slopes[1]),(-1,slopes[-1],slopes[-2])):
                value=(3*near-next_slope)/2
                tangents[index]=0. if value*near<=0 else math.copysign(min(abs(value),3*abs(near)),value)
            step=max(0.,width-1.)/max(1,count-1)/3
            for index in range(1,count):
                path.cubicTo(float(xs[index-1]+step),float(ys[index-1]+tangents[index-1]/3),
                             float(xs[index]-step),float(ys[index]-tangents[index]/3),float(xs[index]),float(ys[index]))
        else:
            for x,y in zip(xs[1:],ys[1:]):path.lineTo(float(x),float(y))
        return path

    @classmethod
    def paint_wave_base(cls,painter,palette,canvas,plot,channels=1):
        painter.fillRect(canvas,QColor(palette.input))
        painter.setPen(QPen(QColor(palette.border_subtle),1))
        for channel in range(channels):
            center,_=cls.wave_lane(plot,channel,channels)
            painter.drawLine(QPointF(plot.left(),center),QPointF(plot.right(),center))

    def drawing_rect(self):return self.rect().adjusted(8,6,-8,-6)

    def _point(self, position):
        plot=self.drawing_rect();center,amplitude=self.wave_lane(plot)
        x=max(0,min(len(self.values)-1,round((position.x()-plot.left())/max(1,plot.width()-1)*(len(self.values)-1))))
        y=max(-1,min(1,(center-position.y())/amplitude))
        return x,y

    def _draw_to(self, position):
        x,y=self._point(position)
        if self._last is None:self.values[x,0]=y
        else:
            old_x,old_y=self._last
            first,last=sorted((old_x,x))
            if x==old_x:self.values[x,0]=y
            else:self.values[first:last+1,0]=np.interp(np.arange(first,last+1),[first,last],[old_y,y] if old_x<x else [y,old_y])
        self._last=x,y;self._path_key=None;self.update()

    def mousePressEvent(self,event):
        if event.button()!=Qt.LeftButton:return super().mousePressEvent(event)
        self.setFocus()
        if self.editable:
            self._before=self.values[:,0].copy();self._draw_to(event.position())
        elif self.zone:
            count=len(self.values)
            marks={key:getattr(self.zone,key) for key in ('start','end','loop_start','loop_end')}
            marks['end']=marks['end'] or count
            key=min(marks,key=lambda k:abs(6+marks[k]/count*max(1,self.width()-13)-event.position().x()))
            if abs(6+marks[key]/count*max(1,self.width()-13)-event.position().x())<=12:self._drag_marker=key
        event.accept()

    def mouseMoveEvent(self,event):
        if self.editable and self._before is not None:self._draw_to(event.position())
        elif self._drag_marker:
            value=round(max(0,min(1,(event.position().x()-6)/max(1,self.width()-13)))*len(self.values))
            self.markerEdited.emit(self._drag_marker,value)
        else:return super().mouseMoveEvent(event)
        event.accept()

    def mouseReleaseEvent(self,event):
        if event.button()==Qt.LeftButton:
            if self.editable and self._before is not None:
                self._draw_to(event.position());self.waveEdited.emit(self.values[:,0].copy())
            self._before=None;self._last=None;self._drag_marker=None
            event.accept()
        else:super().mouseReleaseEvent(event)

    def cancel_pointer_gesture(self):
        if self.editable and self._before is not None:
            self.values=self._before.reshape(-1,1).copy();self._path_key=None
        self._before=None;self._last=None;self._drag_marker=None;self.update()

    def paintEvent(self,event):
        p=widget_palette(self);painter=QPainter(self);plot=self.drawing_rect()
        self.paint_wave_base(painter,p,self.rect(),plot,self.values.shape[1])
        key=(self.width(),self.height(),id(self.values),self.devicePixelRatioF())
        if self._path_key!=key:
            self._paths=[]
            for ch in range(self.values.shape[1]):
                center,half_height=self.wave_lane(plot,ch,self.values.shape[1])
                path=self.wave_path(self.values[:,ch],plot.left(),max(1,plot.width()),center,half_height,pixel_ratio=self.devicePixelRatioF())
                self._paths.append(path)
            self._path_key=key
        painter.setRenderHint(QPainter.Antialiasing)
        for ch,path in enumerate(self._paths):
            painter.setPen(self.wave_pen(p));painter.drawPath(path)
        if self.zone:
            for key in ('start','end','loop_start','loop_end'):
                if key.startswith('loop') and not self.zone.loop:continue
                value=getattr(self.zone,key) or (len(self.values) if key=='end' else 0)
                x=6+value/len(self.values)*max(1,self.width()-13)
                painter.setPen(QPen(QColor(p.accent if key.startswith('loop') else p.text),1,
                                     Qt.DashLine if key.startswith('loop') else Qt.SolidLine))
                painter.drawLine(round(x),6,round(x),max(6,self.height()-7))


class ProcessedInstrumentWave(InstrumentWaveCanvas):
    """Read-only output inspector: whole-sound navigator and detailed PCM."""
    viewChanged=Signal()
    def __init__(self,parent=None):
        super().__init__(parent)
        self.values=np.empty((0,2),np.float32);self.message=tr('creator.processed_wave_empty')
        self.rate=44100;self.view=(0.,1.);self.peak=0.;self.pending=False
        self._requested_view=(0.,.020);self._fit_view=False
        self.display_gain=1.;self._amplitude_fitted=False
        self._drag=None;self._overview_path=None;self._detail_paths=[];self._detail_key=None
        self.setAccessibleName(tr('creator.processed_wave'));self.setToolTip(tr('creator.processed_wave_help'))
        self.setFocusPolicy(Qt.StrongFocus);self.setMouseTracking(True)
    def sizeHint(self):return QSize(300,152)
    def minimumSizeHint(self):return QSize(0,152)
    def plot_rect(self):
        return self.rect().adjusted(6,39,-6,-23)
    def overview_rect(self):return QRectF(6,5,max(0,self.width()-12),25)
    def set_pcm(self,pcm,rate=44100,*,peak=None):
        values=np.asarray(pcm,np.float32)
        if values.ndim!=2 or not len(values) or values.shape[1] not in (1,2):
            self.set_message(tr('creator.processed_wave_empty'));return
        self.values=values.copy() if values.flags.writeable else values;self.rate=rate;self.message='';self.pending=False
        self.peak=float(np.max(np.abs(self.values))) if peak is None else peak
        if not self._amplitude_fitted and self.peak>0:self.fit_amplitude()
        self._overview_path=None
        duration=len(self.values)/rate
        first,span=(0.,1.) if self._fit_view else (self._requested_view[0]/duration,self._requested_view[1]/duration)
        self._apply_view(first,span)
    def set_message(self,text,*,reset_view=False):
        self.values=np.empty((0,2),np.float32);self.message=text;self._drag=None;self.pending=False
        if reset_view:
            self._requested_view=(0.,.020);self._fit_view=False;self.reset_amplitude()
        self._overview_path=None;self._detail_key=None;self.view=(0.,1.);self.update();self.viewChanged.emit()
        set_native_cursor(self,None)
    def reset_amplitude(self):
        self.display_gain=1.;self._amplitude_fitted=False
        self._detail_key=None;self._overview_path=None;self.update()
    def fit_amplitude(self):
        if self.peak>0 and math.isfinite(self.peak):
            self.display_gain=.9/self.peak;self._amplitude_fitted=True
        self._detail_key=None;self._overview_path=None;self.update()
    def set_pending(self):
        self.pending=True
        if not self.values.size:self.message=tr('creator.processed_wave_pending')
        self.update();self.viewChanged.emit()
    def set_view(self,first,span):
        if self.values.size:
            duration=len(self.values)/self.rate
            self._requested_view=(max(0.,float(first))*duration,max(16/self.rate,min(1.,float(span))*duration))
            self._fit_view=False
        self._apply_view(first,span)
    def _apply_view(self,first,span):
        span=max(16/max(16,len(self.values)),min(1.,float(span)))
        first=max(0.,min(1.-span,float(first)));self.view=(first,first+span)
        self._detail_key=None;self.update();self.viewChanged.emit()
    def zoom(self,factor,anchor=None,fraction=.5):
        if not self.values.size or not math.isfinite(factor) or factor<=0:return
        a,b=self.view;anchor=(a+b)/2 if anchor is None else anchor
        span=max(16/len(self.values),min(1.,(b-a)/factor))
        self.set_view(anchor-span*fraction,span)
    def fit(self):
        self._fit_view=True;self._apply_view(0.,1.)
    def _fraction(self,x):
        rect=self.plot_rect();return max(0.,min(1.,(x-rect.left())/max(1,rect.width())))
    def wheelEvent(self,event):
        if not self.values.size:event.ignore();return
        decoded=native_wheel_steps(self,event,role='zoom',axis='y')
        if decoded is not None and decoded[1]:
            fraction=self._fraction(event.position().x());a,b=self.view
            self.zoom(2.**max(-32,min(32,decoded[1])),a+(b-a)*fraction,fraction)
    def mousePressEvent(self,event):
        if not self.values.size or event.button() not in (Qt.LeftButton,Qt.MiddleButton):return
        self.setFocus();overview=self.overview_rect().contains(event.position())
        if not overview and not self.plot_rect().contains(event.position().toPoint()):event.ignore();return
        self._drag=('overview' if overview else 'detail',event.position().x(),self.view)
        set_native_cursor(self,Qt.ClosedHandCursor)
        if overview:self._move_overview(event.position().x())
        event.accept()
    def _move_overview(self,x):
        rect=self.overview_rect();a,b=self.view
        self.set_view((x-rect.left())/max(1,rect.width())-(b-a)/2,b-a)
    def mouseMoveEvent(self,event):
        if self._drag:
            kind,x,(a,b)=self._drag
            if kind=='overview':self._move_overview(event.position().x())
            else:self.set_view(a-(event.position().x()-x)/max(1,self.plot_rect().width())*(b-a),b-a)
            event.accept();return
        self.reconcile_hover(event.position())
    def mouseReleaseEvent(self,event):
        self._drag=None;self.reconcile_hover(event.position());event.accept()
    def reconcile_hover(self,position):
        point=position.toPoint() if hasattr(position,'toPoint') else position
        active=self.values.size and (self.overview_rect().contains(QPointF(point)) or self.plot_rect().contains(point))
        set_native_cursor(self,Qt.ClosedHandCursor if self._drag else Qt.OpenHandCursor if active else Qt.ArrowCursor)
    def cancel_pointer_gesture(self):self._drag=None;set_native_cursor(self,None)
    def reset_stale_hover(self):self.cancel_pointer_gesture()
    def leaveEvent(self,event):
        if not self._drag:set_native_cursor(self,None)
        super().leaveEvent(event)
    def hideEvent(self,event):self.cancel_pointer_gesture();super().hideEvent(event)
    def keyPressEvent(self,event):
        if event.key() in (Qt.Key_Plus,Qt.Key_Equal):self.zoom(2)
        elif event.key()==Qt.Key_Minus:self.zoom(.5)
        elif event.key()==Qt.Key_Home:self.fit()
        elif event.key() in (Qt.Key_Left,Qt.Key_Right):
            a,b=self.view;self.set_view(a+(b-a)*(.2 if event.key()==Qt.Key_Right else -.2),b-a)
        else:return super().keyPressEvent(event)
        event.accept()
    def contextMenuEvent(self,event):
        if not self.values.size:return
        menu=NativeMenu(self)
        for key,callback in (('zoom_in',lambda:self.zoom(2)),('zoom_out',lambda:self.zoom(.5)),('zoom_fit',self.fit)):
            menu.addAction(tr('creator.'+key),callback)
        menu.addAction(tr('creator.wave_amplitude_fit'),self.fit_amplitude)
        menu.exec(event.globalPos());menu.deleteLater()
    def time_labels(self):
        rect=self.plot_rect();a,b=self.view;duration=len(self.values)/self.rate
        for count in (3,2,1):
            labels=[]
            for index in range(count):
                fraction=index/(count-1) if count>1 else 0
                text=tr('creator.wave_time_ms',value=(a+(b-a)*fraction)*duration*1000)
                width=self.fontMetrics().horizontalAdvance(text)
                left=max(rect.left(),min(rect.right()-width,rect.left()+rect.width()*fraction-width/2))
                labels.append((QRectF(left,self.height()-21,width,20),text))
            if all(a[0].right()+6<=b[0].left() for a,b in zip(labels,labels[1:])):return labels
        return []
    def channel_geometry(self,ch):
        return self.wave_lane(self.plot_rect(),ch,self.values.shape[1])
    def paintEvent(self,event):
        palette=widget_palette(self);p=QPainter(self)
        p.fillRect(self.rect(),QColor(palette.input))
        if self.values.size:
            rect=self.plot_rect();width=max(1,rect.width());height=max(1,rect.height());channels=self.values.shape[1]
            self.paint_wave_base(p,palette,self.rect(),rect,channels)
            p.setRenderHint(QPainter.Antialiasing)
            a,b=self.view;count=len(self.values);first=int(a*count);last=min(count,max(first+1,math.ceil(b*count)))
            key=(id(self.values),self.width(),self.height(),self.view,self.display_gain,self.devicePixelRatioF())
            if self._detail_key!=key:
                self._detail_paths=[]
                for ch in range(channels):
                    center,amplitude=self.channel_geometry(ch)
                    path=self.wave_path(self.values[first:last,ch],rect.left(),width,center,amplitude,
                                        gain=self.display_gain,pixel_ratio=self.devicePixelRatioF())
                    self._detail_paths.append(path)
                self._detail_key=key
            p.setPen(QPen(QColor(palette.border_subtle),1))
            labels=self.time_labels()
            for index in range(len(labels)):
                x=rect.left()+rect.width()*index/max(1,len(labels)-1)
                p.drawLine(QPointF(x,rect.top()),QPointF(x,rect.bottom()))
            for ch,path in enumerate(self._detail_paths):
                p.setPen(self.wave_pen(palette));p.drawPath(path)
            overview=self.overview_rect();overview_key=(id(self.values),self.width(),self.height(),self.display_gain)
            if not self._overview_path or self._overview_path[0]!=overview_key:
                path=self.overview_path(self.values,overview,self.display_gain)
                self._overview_path=(overview_key,path)
            self.paint_overview(p,palette,overview,self._overview_path[1],self.view)
            p.setPen(QColor(palette.muted))
            for label,text in labels:p.drawText(label,Qt.AlignVCenter|Qt.AlignLeft,text)
            return
        p.setPen(QColor(palette.muted));p.drawText(self.rect().adjusted(6,6,-6,-6),Qt.AlignCenter|Qt.TextWordWrap,self.message)


class InstrumentWaveJob(QThread):
    completed=Signal(object,object,str)
    def __init__(self,parent,key,path,draft,rate):
        super().__init__(parent);self.key=key;self.path=path;self.draft=draft;self.rate=rate
        from bass_bindings import bass
        self.device=bass.BASS_GetDevice()
    def run(self):
        try:
            from bass_bindings import bass
            if not bass.BASS_SetDevice(self.device):raise RuntimeError(tr('creator.engine_required'))
            hold=preview_note_duration(self.draft,self.key[1],self.key[2],self.key[3])
            pcm=render_compiled_preview(self.path,self.draft.bank,self.draft.program,self.key[1],self.key[2],self.draft.release,
                self.isInterruptionRequested,rate=self.rate,hold_seconds=hold)
            if not self.isInterruptionRequested():
                if not np.isfinite(pcm).all():raise RuntimeError(tr('creator.processed_wave_failed'))
                peak=float(np.max(np.abs(pcm)))
                pcm.setflags(write=False)
                self.completed.emit(self.key,(pcm,peak),'')
        except Exception as exc:
            if not self.isInterruptionRequested():self.completed.emit(self.key,None,str(exc))


class InstrumentAuthoringJob(QThread):
    completed=Signal(str,object,str)

    def __init__(self,parent,operation,payload):
        super().__init__(parent)
        from bass_bindings import bass
        self.operation,self.payload=operation,payload
        self.device=bass.BASS_GetDevice()
        self.existing_sample_bytes=sum(zone.pcm.nbytes for zone in parent.draft.zones)
        self.preferences=getattr(parent.group_dialog.owner,'_engine_preferences',None)
        self.sample_rate=getattr(self.preferences,'sample_rate',48000)
        from bass_bindings import BASS_CHANNELINFO
        import ctypes
        session=getattr(parent.group_dialog.owner,'audio_session',None);info=BASS_CHANNELINFO()
        if session and session.stream and bass.BASS_ChannelGetInfo(session.stream,ctypes.byref(info)):self.sample_rate=info.freq

    def run(self):
        try:
            from bass_bindings import bass
            if self.operation in ('import','preview','apply','edit') and not bass.BASS_SetDevice(self.device):
                raise RuntimeError(tr('creator.engine_required'))
            if self.operation=='import':
                value=[];total=self.existing_sample_bytes
                for path in self.payload:
                    zone=decode_sample(path,self.isInterruptionRequested)
                    total+=zone.pcm.nbytes
                    if total>MAX_FILE_BYTES:raise ValueError(tr('creator.too_large'))
                    value.append(zone)
            elif self.operation=='edit':
                record,source_path=self.payload
                value=record_draft(record,source_path,cancelled=self.isInterruptionRequested)
            elif self.operation=='apply':
                draft,source_path=self.payload
                record=draft_record(draft)
                value=(record,compile_record(record,source_path,self.isInterruptionRequested))
            elif self.operation=='preview':
                from instrument_creator import save_sf2
                from audio_output import InstrumentAuditionSource
                draft,path,revision=self.payload
                save_sf2(draft,path)
                if self.isInterruptionRequested():raise RuntimeError(tr('creator.cancelled'))
                source=InstrumentAuditionSource(path,draft.bank,draft.program,self.sample_rate,self.preferences)
                if self.isInterruptionRequested():source.close();raise RuntimeError(tr('creator.cancelled'))
                self.prepared_source=source
                value=(source,revision)
            else:raise ValueError(self.operation)
            self.completed.emit(self.operation,value,'')
        except Exception as exc:self.completed.emit(self.operation,None,str(exc))


class SampleWaveCanvas(InstrumentWaveCanvas):
    """Source-addressed, zoomable sample timeline with distinct edit lanes."""
    seekRequested=Signal(int)
    listenRequested=Signal()
    selectionChanged=Signal(int,int)
    selectionCommitted=Signal(int,int)
    viewChanged=Signal()

    def __init__(self,parent=None):
        super().__init__(parent)
        self.selection=None;self.cursor=0;self.view=(0.,1.);self._anchor=None
        self._processed=None;self._plot_cache=None;self.setMouseTracking(True)
        self._gesture=None;self._gesture_origin=None;self._range_before=None;self._pixels=None;self._pixel_key=None

    def sizeHint(self):return QSize(500,150)
    def minimumSizeHint(self):return QSize(0,120)

    def set_zone(self,zone):
        changed=zone is not self.zone
        visual=(id(zone.pcm),zone.reverse,zone.gain) if zone else None
        visual_changed=visual!=getattr(self,'_source_visual',None)
        if changed:self.selection=None;self.cursor=zone.start if zone else 0;self.view=(0.,1.);self._processed=None
        if changed or visual_changed:
            self._plot_cache=None
            self._display_source=zone.pcm[::-1] if zone and zone.reverse else zone.pcm if zone else self.values
        self._source_visual=visual
        # Publish content only after the complete source/display/view state is
        # ready. Overview listeners must never observe a half-updated sample.
        super().set_zone(zone)
        refresh_native_pointer_feedback(self)

    def set_processed(self,pcm):
        self._processed=pcm;self._plot_cache=None;self.update();self.contentChanged.emit()

    def _display_fraction(self,source):
        value=source/max(1,len(self.values))
        return 1-value if self.zone and self.zone.reverse else value

    def _x(self,source):
        a,b=self.view
        return 8+(self._display_fraction(source)-a)/max(1e-9,b-a)*max(1,self.width()-16)

    def _source(self,x):
        a,b=self.view;value=max(0,min(1,a+(x-8)/max(1,self.width()-16)*(b-a)))
        if self.zone and self.zone.reverse:value=1-value
        return round(value*len(self.values))

    def _marker_handles(self):
        """One source-bound layout for vector heads,hover and drag targeting."""
        if not self.zone:return []
        result=[]
        for lane,keys in ((0,('loop_start','loop_end')),(23,('start','end'))):
            if lane==0 and not self.zone.loop:continue
            heads=[]
            for key in keys:
                value=getattr(self.zone,key) or (len(self.zone.pcm) if key=='end' else 0)
                x=self._x(value)
                if not 7<=x<=self.width()-7:continue
                direction=(1 if key.endswith('start') else -1)*(-1 if self.zone.reverse else 1)
                left=x+2 if direction>0 else x-16
                rect=QRectF(max(1,min(self.width()-15,left)),lane+3,14,16)
                heads.append((key,x,rect,direction))
            if len(heads)==2:
                heads.sort(key=lambda item:(item[1],-item[3]))
                if heads[0][2].right()+2>heads[1][2].left():
                    left=max(1,min(self.width()-31,(heads[0][1]+heads[1][1])/2-15))
                    heads[0][2].moveLeft(left);heads[1][2].moveLeft(left+16)
            result.extend(heads)
        return result

    def _marker_at(self,position):
        candidates=[head for head in self._marker_handles()
            if head[2].adjusted(-2,-2,2,2).contains(position)
            or (head[2].top()-3<=position.y()<=head[2].bottom()+3 and abs(head[1]-position.x())<5)]
        if not candidates:return None
        return min(candidates,key=lambda head:abs(head[2].center().x()-position.x()))[0]

    def _time_labels(self):
        if not self.zone:return []
        a,b=self.view;duration=len(self.zone.pcm)/self.zone.rate/self.zone.speed
        metrics=QFontMetrics(self.font());available=max(0,self.width()-16)
        for count in (5,3,2,1):
            labels=[]
            for index in range(count):
                fraction=index/(count-1) if count>1 else 0
                text=f'{(a+(b-a)*fraction)*duration:.2f}s'
                width=min(available,metrics.horizontalAdvance(text)+2)
                x=8+fraction*available;left=max(8,min(self.width()-8-width,x-width/2))
                labels.append((QRectF(left,self.height()-21,width,20),text))
            if all(left[0].right()+6<=right[0].left() for left,right in zip(labels,labels[1:])):return labels
        return []

    def set_view(self,first,span):
        span=max(16/max(16,len(self.values)),min(1,span))
        first=max(0,min(1-span,first));self.view=(first,first+span)
        self._plot_cache=None;self.update();self.viewChanged.emit();refresh_native_pointer_feedback(self)

    def fit(self):self.set_view(0,1)

    def zoom(self,factor,anchor=None,fraction=.5):
        a,b=self.view;center=(a+b)/2 if anchor is None else anchor
        span=max(16/max(16,len(self.values)),min(1,(b-a)/factor))
        self.set_view(center-max(0,min(1,fraction))*span,span)

    def zoom_selection(self):
        if self.selection:
            a,b=sorted(self._display_fraction(i) for i in self.selection);self.set_view(a,b-a)

    def clear_range(self):
        self.selection=None;self._end_gesture();self.selectionChanged.emit(-1,-1)

    def contextMenuEvent(self,event):
        if getattr(self,'_suppress_context',False):self._suppress_context=False;event.accept();return
        menu=NativeMenu(self);fraction=max(0,min(1,(event.pos().x()-8)/max(1,self.width()-16)))
        a,b=self.view;anchor=a+fraction*(b-a)
        for key,callback in (('zoom_in',lambda:self.zoom(2,anchor,fraction)),('zoom_out',lambda:self.zoom(.5,anchor,fraction)),
                ('zoom_fit',self.fit),('zoom_selection',self.zoom_selection)):
            action=menu.addAction(tr('creator.'+key));action.triggered.connect(callback)
            if key=='zoom_selection':action.setEnabled(bool(self.selection))
        menu.exec(event.globalPos());menu.deleteLater()

    def wheelEvent(self,event):
        a,b=self.view;center=a+(event.position().x()-8)/max(1,self.width()-16)*(b-a)
        fraction=(event.position().x()-8)/max(1,self.width()-16)
        if not self.isEnabled():event.ignore();return
        angle,pixels=event.angleDelta(),event.pixelDelta()
        if event.modifiers()&Qt.ShiftModifier or angle.x() or pixels.x():
            # Content pan has a direct pixel interpretation; zoom does not.
            state=getattr(self,'_native_wheel_input',None)
            if state is not None:state.context=None;state.remainder=0
            movement=((pixels.x() or pixels.y())/max(1,self.width()-16) if not pixels.isNull()
                      else (angle.x() or angle.y())/120*.15)
            if movement:self.set_view(a-movement*(b-a),b-a);event.accept()
            else:event.ignore()
        else:
            decoded=native_wheel_steps(self,event,role='zoom',axis='y')
            if decoded is not None and decoded[1]:self.zoom(1.5**decoded[1],center,fraction)

    def mousePressEvent(self,event):
        if not self.zone:return
        if event.button() in (Qt.MiddleButton,Qt.RightButton):
            self._gesture='pan';self._gesture_origin=event.position().x();self._range_before=self.view
            set_native_cursor(self,Qt.ClosedHandCursor);event.accept();return
        if event.button()!=Qt.LeftButton:return
        self.setFocus();position=event.position();self._drag_marker=None;self._anchor=None
        self._gesture_origin=position.x();self._range_before=self.selection
        if self.selection and position.y()>=45:
            first,last=sorted(self._x(source) for source in self.selection)
            if min(abs(position.x()-first),abs(position.x()-last))<7:
                self._gesture='resize';self._selection_edge=min((0,1),key=lambda edge:abs(self._x(self.selection[edge])-position.x()))
                set_native_cursor(self,Qt.SizeHorCursor)
                event.accept();return
            if first<=position.x()<=last and event.modifiers()&Qt.AltModifier:
                self._gesture='move';self._anchor=self._source(position.x());set_native_cursor(self,Qt.ClosedHandCursor);event.accept();return
        self._drag_marker=self._marker_at(position)
        if self._drag_marker:
            value=getattr(self.zone,self._drag_marker) or (len(self.values) if self._drag_marker=='end' else 0)
            self._marker_offset=self._source(position.x())-value
        if not self._drag_marker:
            self._anchor=self._source(position.x());self.cursor=self._anchor;self.selection=None
            self._gesture='select';self.selectionChanged.emit(-1,-1)
            self.seekRequested.emit(self.cursor)
        else:self._gesture='marker'
        set_native_cursor(self,Qt.SizeHorCursor if self._drag_marker else Qt.CrossCursor)
        self.update();event.accept()

    def mouseMoveEvent(self,event):
        if not self.zone:return
        if self._gesture and not event.buttons()&(Qt.LeftButton|Qt.MiddleButton|Qt.RightButton):self._end_gesture()
        source=self._source(event.position().x())
        if self._gesture=='pan':
            a,b=self._range_before;offset=(self._gesture_origin-event.position().x())/max(1,self.width()-16)*(b-a)
            self.set_view(a+offset,b-a)
        elif self._gesture=='resize':
            first,last=self.selection
            self.selection=(min(source,last-4),last) if self._selection_edge==0 else (first,max(first+4,source))
            self.selectionChanged.emit(*self.selection);self.update()
        elif self._gesture=='move':
            first,last=self._range_before;offset=max(-first,min(len(self.values)-last,source-self._anchor))
            self.selection=(first+offset,last+offset);self.selectionChanged.emit(*self.selection);self.update()
        elif self._drag_marker:self.markerEdited.emit(self._drag_marker,max(0,min(len(self.values),source-self._marker_offset)))
        elif self._anchor is not None and abs(self._x(source)-self._x(self._anchor))>4:
            self.selection=tuple(sorted((source,self._anchor)));self.selectionChanged.emit(*self.selection);self.update()
        else:
            self.reconcile_hover(event.position())

    def mouseReleaseEvent(self,event):
        self._suppress_context=bool(event.button()==Qt.RightButton and self._gesture_origin is not None and abs(event.position().x()-self._gesture_origin)>4)
        committed=self.selection if event.button()==Qt.LeftButton and self._gesture in ('select','resize','move') else None
        self._end_gesture();event.accept()
        if committed and committed[1]-committed[0]>=4:self.selectionCommitted.emit(*committed)

    def _end_gesture(self):
        self._anchor=None;self._drag_marker=None;self._gesture=None
        refresh_native_pointer_feedback(self);self.update()

    def reconcile_hover(self,position,*,modifiers=None):
        if not self.isEnabled() or not self.zone:
            self.reset_stale_hover();return
        if self._gesture is not None:return  # A held gesture keeps its cursor.
        marker=self._marker_at(position)
        self.setToolTip(tr('creator.help_'+marker) if marker else tr('creator.sample_tip'))
        edge=(position.y()>=45 and self.selection and
              min(abs(self._x(i)-position.x()) for i in self.selection)<7)
        moving=(position.y()>=45 and self.selection and (QApplication.keyboardModifiers() if modifiers is None else modifiers)&Qt.AltModifier
                and min(self._x(i) for i in self.selection)<=position.x()<=max(self._x(i) for i in self.selection))
        set_native_cursor(self,Qt.SizeHorCursor if marker or edge else Qt.OpenHandCursor if moving else Qt.CrossCursor)

    def reset_stale_hover(self):self.setToolTip(tr('creator.sample_tip'));set_native_cursor(self,None)

    def cancel_pointer_gesture(self):self._end_gesture()

    def leaveEvent(self,event):
        if self._gesture is None:self.reset_stale_hover()
        super().leaveEvent(event)

    def hideEvent(self,event):
        self._end_gesture();super().hideEvent(event)

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type()==QEvent.EnabledChange:
            if not self.isEnabled():self._end_gesture()
            else:refresh_native_pointer_feedback(self)

    def resizeEvent(self,event):
        super().resizeEvent(event);refresh_native_pointer_feedback(self)

    def focusOutEvent(self,event):self._end_gesture();super().focusOutEvent(event)

    def pointer_modifiers_changed(self,modifiers):
        if self.isVisible() and self.isEnabled() and self.underMouse():
            self.reconcile_hover(self.mapFromGlobal(QCursor.pos()),modifiers=modifiers)

    def keyPressEvent(self,event):
        if event.key()==Qt.Key_Escape:
            self.clear_range();event.accept()
        else:
            if event.key()==Qt.Key_Alt:self.pointer_modifiers_changed(event.modifiers()|Qt.AltModifier)
            super().keyPressEvent(event)

    def keyReleaseEvent(self,event):
        if event.key()==Qt.Key_Alt:self.pointer_modifiers_changed(event.modifiers()&~Qt.AltModifier)
        super().keyReleaseEvent(event)

    def set_cursor(self,source):
        old=round(self._x(self.cursor));self.cursor=source;new=round(self._x(source))
        if old!=new:
            self.update(old-3,45,7,max(0,self.height()-67));self.update(new-3,45,7,max(0,self.height()-67))

    def mouseDoubleClickEvent(self,event):
        if self.zone:
            self.cursor=self._source(event.position().x());self.seekRequested.emit(self.cursor);self.listenRequested.emit()
        event.accept()

    def paintEvent(self,event):
        palette=widget_palette(self);p=QPainter(self);p.fillRect(self.rect(),QColor(palette.input))
        if not self.zone:
            p.setPen(QColor(palette.muted));p.drawText(self.rect().adjusted(12,8,-12,-8),Qt.AlignCenter|Qt.TextWordWrap,tr('creator.no_sample'));return
        zone=self.zone;source=self._processed
        if source is None:source=self._display_source
        a,b=self.view;top=48;bottom=max(top+1,self.height()-23);height=bottom-top
        first=int(a*len(source));last=max(first+1,min(len(source),math.ceil(b*len(source))))
        self.paint_wave_base(p,palette,self.rect(),QRectF(8,top,max(1,self.width()-16),height),source.shape[1])
        # Shared time-accurate paths;cached until audio/zoom/size/DPI changes.
        key=(id(source),self.width(),self.height(),a,b,zone.gain,self.devicePixelRatioF())
        if key!=self._plot_cache:
            self._paths=[];width=max(1,self.width()-16)
            for channel in range(source.shape[1]):
                center,amplitude=self.wave_lane(QRectF(8,top,max(1,self.width()-16),height),channel,source.shape[1])
                path=self.wave_path(source[first:last,channel],8,width,center,amplitude,
                                    gain=10**(zone.gain/20),pixel_ratio=self.devicePixelRatioF())
                self._paths.append(path)
            self._plot_cache=key
        p.save();p.setClipRect(8,top,max(0,self.width()-16),height)
        pixel_key=(self._plot_cache,palette.accent,palette.input,self.devicePixelRatioF())
        if pixel_key!=self._pixel_key:
            ratio=self.devicePixelRatioF();self._pixels=QPixmap(round(self.width()*ratio),round(self.height()*ratio));self._pixels.setDevicePixelRatio(ratio)
            self._pixels.fill(Qt.transparent);cached=QPainter(self._pixels);cached.setRenderHint(QPainter.Antialiasing)
            cached.setPen(self.wave_pen(palette))
            for path in self._paths:cached.drawPath(path)
            cached.end();self._pixel_key=pixel_key
        p.drawPixmap(0,0,self._pixels)
        start,end=sorted((self._x(zone.start),self._x(zone.end or len(zone.pcm))))
        shade=QColor(palette.input);shade.setAlpha(190)
        p.fillRect(QRectF(8,top,max(0,start-8),height),shade)
        p.fillRect(QRectF(end,top,max(0,self.width()-8-end),height),shade)
        if self.selection:
            x1,x2=sorted(self._x(i) for i in self.selection);color=QColor(palette.accent);color.setAlpha(55)
            p.fillRect(QRectF(x1,top,x2-x1,height),color)
            p.setPen(QPen(QColor(palette.accent),2))
            for x in (x1,x2):p.drawLine(QPointF(x,top),QPointF(x,bottom))
        p.restore()
        for key,x,rect,direction in self._marker_handles():
            loop=key.startswith('loop');ink=QColor(palette.accent if loop else palette.text)
            p.setPen(QPen(ink,1,Qt.DashLine if loop else Qt.SolidLine))
            p.drawLine(round(x),round(rect.top()),round(x),bottom)
            # Bracketed inward flags,not clamped prose. Outlined loop heads
            # retain a shape distinction as well as their separate lane/color.
            left,right=rect.left()+1,rect.right()-1;middle=rect.center().y()
            back,tip=(left,right) if direction>0 else (right,left)
            path=QPainterPath();path.moveTo(back,rect.top()+2);path.lineTo(tip,middle)
            path.lineTo(back,rect.bottom()-2);path.closeSubpath()
            p.save();p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(ink,1.25));p.setBrush(QColor(palette.input) if loop else ink)
            p.drawPath(path);p.restore()
        x=self._x(self.cursor)
        if 8<=x<=self.width()-8:p.setPen(QPen(QColor(palette.text),1.5));p.drawLine(round(x),top,round(x),bottom)
        p.setPen(QColor(palette.muted))
        for rect,text in self._time_labels():p.drawText(rect,Qt.AlignCenter,text)


class SourceWaveOverview(QWidget):
    """Whole-source locator;pan/zoom never edits sample selection or markers."""
    def __init__(self,parent=None):
        super().__init__(parent);self.canvas=None;self._path=None;self._drag_offset=None
        self.setMinimumWidth(0);self.setFixedHeight(32)
        self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.StrongFocus);self.setMouseTracking(True)
        self.setAccessibleName(tr('creator.source_overview'))

    def overview_rect(self):return QRectF(8,3,max(0,self.width()-16),26)

    def set_canvas(self,canvas):
        if self.canvas is not None:
            self.canvas.contentChanged.disconnect(self._source_changed)
            if hasattr(self.canvas,'viewChanged'):self.canvas.viewChanged.disconnect(self.sync)
        self.canvas=canvas;self._path=None;self.cancel_pointer_gesture()
        canvas.contentChanged.connect(self._source_changed)
        if hasattr(canvas,'viewChanged'):canvas.viewChanged.connect(self.sync)
        self.sync()

    def _source_changed(self):
        self.cancel_pointer_gesture();self.sync()

    def _data(self):
        canvas=self.canvas
        if isinstance(canvas,SampleWaveCanvas):
            if canvas.zone is None:return None,1.
            values=canvas._processed if canvas._processed is not None else canvas._display_source
            return values,10**(canvas.zone.gain/20)
        return (canvas.values,1.) if canvas is not None else (None,1.)

    def _navigable(self):return self.isEnabled() and isinstance(self.canvas,SampleWaveCanvas) and self.canvas.isEnabled() and self.canvas.zone is not None

    def sync(self):
        values,_gain=self._data()
        present=isinstance(self.canvas,SampleWaveCanvas) and values is not None and bool(values.size)
        self.setVisible(present)
        self.setToolTip(tr('creator.source_overview_help') if present else '')
        self.update()
        if self._drag_offset is None:refresh_native_pointer_feedback(self)

    def paintEvent(self,event):
        palette=widget_palette(self);p=QPainter(self);p.fillRect(self.rect(),QColor(palette.input))
        values,gain=self._data()
        if values is None or not values.size:return
        rect=self.overview_rect();key=(id(values),self.width(),gain,self.devicePixelRatioF())
        if self._path is None or self._path[0]!=key:self._path=(key,InstrumentWaveCanvas.overview_path(values,rect,gain))
        InstrumentWaveCanvas.paint_overview(p,palette,rect,self._path[1],getattr(self.canvas,'view',(0.,1.)))

    def mousePressEvent(self,event):
        if not self._navigable() or event.button()!=Qt.LeftButton:event.ignore();return
        self.setFocus();rect=self.overview_rect();a,b=self.canvas.view
        fraction=(event.position().x()-rect.left())/max(1,rect.width())
        self._drag_offset=fraction-a if a<=fraction<=b else (b-a)/2
        self._move(event.position().x());set_native_cursor(self,Qt.ClosedHandCursor);event.accept()

    def _move(self,x):
        rect=self.overview_rect();a,b=self.canvas.view
        self.canvas.set_view((x-rect.left())/max(1,rect.width())-self._drag_offset,b-a)

    def mouseMoveEvent(self,event):
        if self._drag_offset is not None:
            if not self._navigable() or not event.buttons()&Qt.LeftButton:self.cancel_pointer_gesture()
            else:self._move(event.position().x());event.accept();return
        self.reconcile_hover(event.position())

    def mouseReleaseEvent(self,event):self.cancel_pointer_gesture();self.reconcile_hover(event.position());event.accept()
    def mouseDoubleClickEvent(self,event):
        if self._navigable() and event.button()==Qt.LeftButton:self.canvas.fit();event.accept()
        else:event.ignore()

    def wheelEvent(self,event):
        if not self._navigable():event.ignore();return
        decoded=native_wheel_steps(self,event,role='zoom',axis='y')
        if decoded is not None and decoded[1]:self.canvas.zoom(2.**max(-32,min(32,decoded[1])))

    def keyPressEvent(self,event):
        if self._navigable() and event.key() in (Qt.Key_Left,Qt.Key_Right,Qt.Key_Plus,Qt.Key_Equal,Qt.Key_Minus,Qt.Key_Home):
            # Use existing view setters,never forward selection/drawing keys.
            if event.key()==Qt.Key_Home:self.canvas.fit()
            elif event.key() in (Qt.Key_Plus,Qt.Key_Equal,Qt.Key_Minus):self.canvas.zoom(.5 if event.key()==Qt.Key_Minus else 2)
            else:
                a,b=self.canvas.view;self.canvas.set_view(a+(b-a)*(.2 if event.key()==Qt.Key_Right else -.2),b-a)
            event.accept()
        else:super().keyPressEvent(event)

    def reconcile_hover(self,position):set_native_cursor(self,Qt.ClosedHandCursor if self._drag_offset is not None else Qt.OpenHandCursor if self._navigable() else None)
    def cancel_pointer_gesture(self):self._drag_offset=None;set_native_cursor(self,None)
    def reset_stale_hover(self):self.cancel_pointer_gesture()
    def leaveEvent(self,event):
        if self._drag_offset is None:set_native_cursor(self,None)
        super().leaveEvent(event)
    def hideEvent(self,event):self.cancel_pointer_gesture();super().hideEvent(event)
    def focusOutEvent(self,event):self.cancel_pointer_gesture();super().focusOutEvent(event)
    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type()==QEvent.EnabledChange and not self.isEnabled():self.cancel_pointer_gesture()


class SampleKeyRange(QWidget):
    """Readable octave window with actual piano geometry,not128 equal bars."""
    rangeEdited=Signal(str,int)
    noteRequested=Signal(int)
    noteOn=Signal(int,int)
    noteOff=Signal(int)
    viewChanged=Signal()

    def __init__(self,parent=None,*,audition_only=False):
        super().__init__(parent);self.zone=None;self._drag=None;self.setMinimumWidth(0)
        self.drum=False;self.audition_only=audition_only;self.first=24.;self.span=84;self.active_note=69;self.velocity=AUDITION_NOTE_VELOCITY
        self._held=None;self._computer={}
        self.motion=QVariantAnimation(self);self.motion.setDuration(180);self.motion.setEasingCurve(QEasingCurve.InOutCubic)
        self.motion.valueChanged.connect(self._move_view)
        self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed);self.setFixedHeight(152)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setToolTip(tr('creator.keyboard_tip'));self.setAccessibleName(tr('creator.key_range'))
    def octave(self,direction):
        target=self.motion.endValue() if self.motion.state()==QVariantAnimation.Running else self.first
        self.animate_to(float(target)+12*direction)
    def resizeEvent(self,event):
        # Preserve readable white-key pitch,not a fixed84-note squeeze in a
        # third-width sibling layout. This is density,never a width floor.
        span=max(12,min(84,(self.width()//(7*18))*12))
        if span!=self.span:
            center=self.first+self.span/2;self.motion.stop();self.span=span
            self.first=max(0.,min(float(128-span),center-span/2))
            if not self.first<=self.active_note<self.first+span:
                self.first=max(0.,min(float(128-span),self.active_note-span/2))
            self.viewChanged.emit()
        super().resizeEvent(event)
    def _move_view(self,value):self.first=float(value);self.update();self.viewChanged.emit()
    def animate_to(self,first):
        start=float(self.first)
        target=max(0.,min(float(128-self.span),float(first)))
        self.motion.stop()
        # Qt only interpolates matching QVariant types. Integer clamp bounds
        # previously produced Double->Int intervals and valueChanged(None).
        # Configure atomically at time0: a stopped animation retains its old
        # time, so publishing new keys at that time can also jump the viewport.
        blocked=self.motion.blockSignals(True)
        try:
            self.motion.setKeyValues(((0.,start),(1.,target)))
            self.motion.setCurrentTime(0)
        finally:self.motion.blockSignals(blocked)
        if start!=target:self.motion.start()
    def set_first(self,first):
        self.motion.stop();self._move_view(max(0.,min(float(128-self.span),float(first))))
    def center_on(self,note):
        if not self.first<=note<self.first+self.span:self.animate_to(note-self.span/2)
        else:self.update()
    @staticmethod
    def _white_position(note):
        octave=math.floor(note/12);value=note-octave*12;offset=(0,.5,1,1.5,2,3,3.5,4,4.5,5,5.5,6,7)
        low=math.floor(value);return octave*7+offset[low]+(offset[low+1]-offset[low])*(value-low)
    def _keys(self):
        origin=self._white_position(self.first);width=self.width()/max(1,self._white_position(self.first+self.span)-origin)
        top=4 if self.audition_only else 38;height=self.height()-top-23;rectangles=[]
        for black in (False,True):
            for note in range(128):
                if (note%12 in (1,3,6,8,10))!=black:continue
                at=self._white_position(note)-origin
                rect=QRectF((at+.5-.31 if black else at)*width,top,width*(.62 if black else 1),height*(.60 if black else 1))
                if rect.right()>=0 and rect.left()<=self.width():rectangles.append((note,rect,black))
        return rectangles
    def note(self,x,y=30):
        keys=self._keys()
        for note,rect,black in reversed(keys):
            if rect.contains(QPointF(x,y)):return note
        return min(keys,key=lambda item:abs(item[1].center().x()-x))[0]

    def mousePressEvent(self,event):
        if event.button()!=Qt.LeftButton:return
        note=self.note(event.position().x(),event.position().y());self.setFocus()
        if not self.audition_only and self.zone and event.position().y()<36:
            self._drag=('root' if event.position().y()>=18 else
                        min(('low','high'),key=lambda key:abs(note-getattr(self.zone,key))))
            self.rangeEdited.emit(self._drag,note)
        else:
            self.release_notes();self.active_note=note;self._held=note
            self.velocity=self._velocity_at(note,event.position().y());self.noteOn.emit(note,self.velocity);self.update()
    def _velocity_at(self,note,y):
        rect=next(rect for key,rect,_ in self._keys() if key==note)
        return max(1,min(127,round(1+126*(y-rect.top())/max(1,rect.height()))))
    def release_notes(self):
        if self._held is not None:self.noteOff.emit(self._held);self._held=None
        for note in set(self._computer.values()):self.noteOff.emit(note)
        self._computer.clear();self.update()
    def mouseMoveEvent(self,event):
        if not event.buttons()&Qt.LeftButton:return
        if self._drag:self.rangeEdited.emit(self._drag,self.note(event.position().x()))
        elif self._held is not None:
            note=self.note(event.position().x(),event.position().y())
            if note!=self._held:
                self.noteOff.emit(self._held);self._held=note;self.active_note=note
                self.velocity=self._velocity_at(note,event.position().y());self.noteOn.emit(note,self.velocity);self.update()
    def mouseReleaseEvent(self,event):self._drag=None;self.release_notes()
    def focusOutEvent(self,event):self._drag=None;self.release_notes();super().focusOutEvent(event)
    def cancel_pointer_gesture(self):self._drag=None;self.release_notes()
    def hideEvent(self,event):self.cancel_pointer_gesture();super().hideEvent(event)
    def wheelEvent(self,event):
        decoded=native_wheel_steps(self,event,role='octave',axis='y')
        if decoded is not None and decoded[1]:self.octave(-decoded[1])
    def contextMenuEvent(self,event):
        if self.zone and not self.audition_only:
            note=self.note(event.pos().x(),event.pos().y());menu=NativeMenu(self)
            for field in ('root','low','high'):
                action=menu.addAction(tr('creator.assign_keyboard',role=tr('creator.'+field),note=self.key_caption(note)))
                action.triggered.connect(lambda _=False,field=field:self.rangeEdited.emit(field,note))
            menu.exec(event.globalPos());menu.deleteLater()
    def keyPressEvent(self,event):
        if event.isAutoRepeat():event.accept();return
        keymap={Qt.Key_Z:60,Qt.Key_S:61,Qt.Key_X:62,Qt.Key_D:63,Qt.Key_C:64,Qt.Key_V:65,Qt.Key_G:66,Qt.Key_B:67,Qt.Key_H:68,Qt.Key_N:69,Qt.Key_J:70,Qt.Key_M:71}
        if event.key() in keymap:
            note=keymap[event.key()];self._computer[event.key()]=note;self.active_note=note;self.noteOn.emit(note,self.velocity);self.update();event.accept();return
        if event.key() in (Qt.Key_Left,Qt.Key_Right):
            self.active_note=max(0,min(127,self.active_note+(1 if event.key()==Qt.Key_Right else -1)));self.center_on(self.active_note)
            self._computer[event.key()]=self.active_note;self.noteOn.emit(self.active_note,self.velocity)
        elif event.key() in (Qt.Key_Up,Qt.Key_Down):self.velocity=max(1,min(127,self.velocity+(8 if event.key()==Qt.Key_Up else -8)))
        else:super().keyPressEvent(event)
    def keyReleaseEvent(self,event):
        if event.isAutoRepeat():return
        note=self._computer.pop(event.key(),None)
        if note is not None:self.noteOff.emit(note);self.update()
        else:super().keyReleaseEvent(event)
    def note_labels(self):
        """Octave legends use octave spans,not tiny individual key widths."""
        metrics=QFontMetrics(self.font());labels=[]
        if self.drum:return labels
        keys=self._keys()
        for note,rect,black in keys:
            if note%12:continue
            text=f'C{note//12-1}';width=metrics.horizontalAdvance(text)+4
            if self.width()>=width:
                left=max(0,min(self.width()-width,rect.left()))
                labels.append((text,QRectF(left,self.height()-21,width,20)))
        return labels

    def key_caption(self,note):
        from effect_presentation import drum_key_caption
        return drum_key_caption(note,mode=getattr(self,'drum_mode',None)) if self.drum else note_caption(note)
    def paintEvent(self,event):
        palette=widget_palette(self);p=QPainter(self)
        p.fillRect(self.rect(),QColor(palette.input))
        keys=self._keys();centers={note:rect.center().x() for note,rect,_ in keys}
        for note,rect,black in keys:
            p.fillRect(rect,QColor('#242b35' if black else '#e7edf5'))
            held=note==self._held or note in self._computer.values()
            if held or (not self.audition_only and self.zone and self.zone.low<=note<=self.zone.high):
                color=QColor(palette.accent);color.setAlpha(190 if held else 55);p.fillRect(rect,color)
            p.setPen(QPen(QColor(palette.border_subtle),1));p.drawRect(rect.adjusted(.5,.5,-.5,-.5))
        p.setPen(QColor(palette.text))
        for text,rect in self.note_labels():p.drawText(rect,Qt.AlignLeft|Qt.AlignVCenter,text)
        if self.active_note in centers:
            text=f'{("C","C♯","D","D♯","E","F","F♯","G","G♯","A","A♯","B")[self.active_note%12]}{self.active_note//12-1}'
            if self.drum:
                from effect_presentation import drum_key_caption
                text=drum_key_caption(self.active_note,mode=getattr(self,'drum_mode',None))
            width=min(self.width(),QFontMetrics(p.font()).horizontalAdvance(text)+8)
            x=max(0,min(self.width()-width,centers[self.active_note]-width/2))
            mark=QRectF(x,self.height()-21,width,20)
            p.fillRect(mark,QColor(palette.accent_soft));p.setPen(QColor(palette.text));p.drawText(mark,Qt.AlignCenter,text)
        if self.zone and not self.audition_only:
            low=centers.get(self.zone.low,0 if self.zone.low<self.first else self.width())
            high=centers.get(self.zone.high,0 if self.zone.high<self.first else self.width())
            p.fillRect(QRectF(low,1,max(1,high-low),16),QColor(palette.accent_soft))
            p.setPen(QColor(palette.text))
            for edge in (low,high):p.drawLine(QPointF(edge,1),QPointF(edge,17))
            p.drawText(QRectF(6,0,max(0,self.width()-12),18),Qt.AlignVCenter,tr('creator.keyboard_range_label',low=self.key_caption(self.zone.low),high=self.key_caption(self.zone.high)))
            if self.zone.root in centers:
                root=centers[self.zone.root];p.setPen(QPen(QColor(palette.accent),2));p.drawLine(QPointF(root,19),QPointF(root,37))
                text=tr('creator.keyboard_root_label',note=self.key_caption(self.zone.root));width=QFontMetrics(p.font()).horizontalAdvance(text)
                p.setPen(QColor(palette.text));p.drawText(QRectF(max(3,min(self.width()-width-3,root+5)),18,width,20),Qt.AlignVCenter,text)


class SampleWorkspaceJob(QThread):
    completed=Signal(int,str,object,str)

    def __init__(self,parent,generation,operation,zone,path):
        super().__init__(parent);self.generation=generation;self.operation=operation;self.zone=replace(zone);self.path=path
        from bass_bindings import bass
        self.device=bass.BASS_GetDevice()

    def run(self):
        prepared_file=False;delivered=False
        record_support_event('sample_workspace',stage='worker_begin',generation=self.generation,operation=self.operation)
        try:
            if self.operation=='analyze':result=analyze_sample(self.zone,self.isInterruptionRequested)
            elif self.operation=='normalize':
                peak=float(np.max(abs(self.zone.pcm[self.zone.start:self.zone.end or len(self.zone.pcm)])))
                result=max(-48,min(12,-1-20*math.log10(peak))) if peak>1e-9 else self.zone.gain
            else:
                from bass_bindings import bass
                if not bass.BASS_SetDevice(self.device):raise RuntimeError(tr('creator.engine_required'))
                zone=prepared_zone(self.zone,self.isInterruptionRequested)
                pcm=zone.pcm[zone.start:zone.end]*10**(zone.gain/20)
                if self.isInterruptionRequested():return
                prepared_file=True
                with wave.open(str(self.path),'wb') as file:
                    file.setnchannels(pcm.shape[1]);file.setsampwidth(2);file.setframerate(zone.rate)
                    for first in range(0,len(pcm),65536):
                        if self.isInterruptionRequested():return
                        data=np.rint(np.clip(pcm[first:first+65536]*.5,-1,1)*32767).astype('<i2').tobytes()
                        file.writeframesraw(data)
                result=(str(self.path),zone.pcm,zone.start,zone.end)
            if not self.isInterruptionRequested():
                self.completed.emit(self.generation,self.operation,result,'');delivered=True
        except Exception as exc:
            record_support_event('sample_workspace',stage='worker_error',generation=self.generation,operation=self.operation,error=str(exc))
            if not self.isInterruptionRequested():self.completed.emit(self.generation,self.operation,None,str(exc))
        finally:
            record_support_event('sample_workspace',stage='worker_end',generation=self.generation,operation=self.operation,
                cancelled=self.isInterruptionRequested(),delivered=delivered)
            if prepared_file and not delivered:
                try:Path(self.path).unlink(missing_ok=True)
                except OSError:pass


class NativeInstrumentCreator(WidthMeasuredPanel):
    """One instrument draft, drawn wave or a set of sampled key/velocity zones."""
    def __init__(self,group_dialog):
        super().__init__(group_dialog)
        self.group_dialog=group_dialog;self.draft=InstrumentDraft(name=tr('creator.default_name'),mode='sample')
        self._set_unused_address(self.draft)
        self.worker=None;self._job_failed=False;self.dirty=False;self._loading=False;self._entry_epoch=0
        self._history=[];self._history_at=-1;self._history_replay=False;self._history_batch=False;self._clean_signature=None
        self._fresh_signature=self._edit_signature(self.draft)
        self._temporary=tempfile.TemporaryDirectory(prefix='abmsynth-authoring-')
        self._path=None
        self._preview_generation=0
        self._after_save=None
        self._entry_id=None
        self._gm_names=__import__('soundfont_files')._gm_names()
        self._sample_job=None;self._sample_generation=0;self._sample_player=None;self._sample_output=None
        self._sample_media=None;self._sample_pending=False;self._sample_autoplay=False;self._sample_seek=None;self._sample_serial=0
        self._cursor_handle=None;self._cursor_anchor=None;self._cursor_position=0.;self._position_caption_at=0.
        self._cursor_precise=False;self._cursor_timer=QTimer(self);self._cursor_timer.setSingleShot(True)
        self._cursor_timer.setTimerType(Qt.PreciseTimer);self._cursor_timer.timeout.connect(self._advance_sample_cursor)
        self._pending_audition=None
        self._pending_operation=None
        self._audition_source=None;self._audition_revision=-1;self._draft_revision=0;self._held_notes={}
        self._wave_job=None;self._wave_path=None;self._wave_revision=-1;self._wave_key=None;self._wave_rate=44100
        self._wave_display_source=None
        self._workspace_motion=NativeWindowSizeMotion(self.window())
        self._workspace_motion.finished.connect(self._finish_workspace_transition)
        self._workspace_scroll_policy=None
        self._wave_debounce=QTimer(self);self._wave_debounce.setSingleShot(True);self._wave_debounce.setInterval(120)
        self._wave_debounce.timeout.connect(self._prepare_processed_wave)
        self._sample_range=None;self._analysis=None;self._sample_closing=False
        self._sample_debounce=QTimer(self);self._sample_debounce.setSingleShot(True);self._sample_debounce.setInterval(140)
        self._sample_debounce.timeout.connect(self.prepare_sample)
        self._audition_debounce=QTimer(self);self._audition_debounce.setSingleShot(True);self._audition_debounce.setInterval(250)
        self._audition_debounce.timeout.connect(self._prime_audition)
        self.setAcceptDrops(True)
        self.setMinimumSize(0,0);self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        root=set_layout(QVBoxLayout(self),0,METRICS['section_gap'])
        root.setSizeConstraint(QLayout.SetNoConstraint)
        self.editor=QWidget();self.editor.setMinimumWidth(0)
        body=set_layout(QVBoxLayout(self.editor),0,METRICS['section_gap'])
        root.addWidget(self.editor,1)
        def task_card(key):
            card=NativeCard();card.setMinimumWidth(0)
            card.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
            card.setAccessibleName(tr('creator.'+key))
            content=set_layout(QVBoxLayout(card),METRICS['card_padding'],METRICS['compact_content_gap'])
            return card,content
        self.identity_card,identity_body=task_card('card_identity')
        body.addWidget(self.identity_card)
        saved_bar=control_row_layout()
        self.saved_list=AnimatedComboBox();self.saved_list.setMinimumWidth(0)
        self.saved_list.setProperty('fitPreferenceContents',True)
        self.saved_list.setPlaceholderText(tr('creator.select_saved'))
        self.open_button=make_action_button(tr('creator.open'),self.choose_open)
        self.delete_button=make_action_button(tr('creator.delete_instrument'),self.delete_instrument)
        self.saved_list.currentIndexChanged.connect(self._saved_actions)
        saved_bar.addWidget(ElasticLabel(tr('creator.saved_instruments')))
        saved_bar.addWidget(self.saved_list,1);saved_bar.addWidget(self.open_button);saved_bar.addWidget(self.delete_button)
        identity_body.addLayout(saved_bar)
        top=control_row_layout()
        self.name=QLineEdit(self.draft.name);self.name.setMaxLength(80);self.name.setMinimumWidth(0)
        self.name.setAccessibleName(tr('creator.name'))
        # ValuePattern.SetValue uses setText,which emits textChanged but not
        # textEdited. change() already guards model-to-view restoration.
        self.name.textChanged.connect(lambda value:self.change('name',value))
        self.bank=NativeNumberBox(0,127,tr('creator.bank'));self.bank.setKeyboardTracking(False)
        self._melodic_bank=self.draft.bank if self.draft.bank<128 else 0
        self.instrument_kind=IdentityChoice(tr('creator.instrument_kind'))
        for kind in ('melodic','drums'):self.instrument_kind.addItem(tr('creator.kind_'+kind),int(kind=='drums'))
        self.program=IdentityChoice(tr('creator.instrument'))
        self.program.set_category_provider(lambda row:(tr('channel_editor.instrument_family_'+str(int(self.program.itemData(row))//8)),))
        self.drum_program=NativeNumberBox(0,127,tr('creator.program'))
        self.program_field=QStackedWidget();self.program_field.setMinimumSize(0,0)
        self.program_field.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
        self.program_field.addWidget(self.program);self.program_field.addWidget(self.drum_program)
        self._identity_address_row=top
        top.addWidget(self.instrument_kind,2)
        self.address_labels={}
        for label,control in (('name',self.name),('bank',self.bank),('program',self.program_field)):
            caption=ElasticLabel(tr('creator.'+label));self.address_labels[label]=caption
            caption.setObjectName('FieldLabel')
            caption.setBuddy(self.drum_program if label=='program' and self.draft.bank==128 else self.program if label=='program' else control)
            top.addWidget(caption,0);top.addWidget(control,3 if label=='name' else 4 if label=='program' else 1)
        self.program.setToolTip(tr('creator.program_tip'));self.drum_program.setToolTip(tr('creator.drum_program_tip'))
        self._populate_programs(self.draft.bank)
        self.bank.valueChanged.connect(lambda value:self.change('bank',value) if self.instrument_kind.value()==0 else None)
        self.instrument_kind.valueChanged.connect(self.change_instrument_kind)
        self.program.valueChanged.connect(lambda value:self.change('program',value))
        self.drum_program.valueChanged.connect(lambda value:self.change('program',value))
        identity_body.addLayout(top)
        self.address_info=QLabel();self.address_info.setWordWrap(True);self.address_info.setMinimumWidth(0)
        self.address_info.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        self.status=NativeOptionalLabel('',preferred_width=150)
        self.new_button=make_action_button(tr('creator.new'),self.new_draft)
        self.save_button=make_action_button(tr('creator.add'),self.choose_save,primary=True)
        self.identity_actions=QWidget();self.identity_actions.setMinimumSize(0,0)
        self.identity_actions.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Maximum)
        identity_commands=control_row_layout(self.identity_actions)
        identity_commands.addWidget(self.new_button);identity_commands.addWidget(self.save_button)
        self.identity_context_layout=ContextActionLayout()
        self.identity_context_layout.addWidget(self.address_info)
        self.identity_context_layout.addWidget(self.identity_actions)
        identity_body.addLayout(self.identity_context_layout)
        self.authoring_workspace=QWidget();self.authoring_workspace.setMinimumWidth(0)
        self.authoring_workspace.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        workspace=card_grid_layout(self.authoring_workspace)
        workspace.setColumnStretch(0,3);workspace.setColumnStretch(1,2)
        self.instrument_workspace=QWidget();self.instrument_workspace.setMinimumWidth(0)
        self.instrument_workspace.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        instrument_column=set_layout(QVBoxLayout(self.instrument_workspace),0,METRICS['section_gap'])
        instrument_column.setAlignment(Qt.AlignTop)
        body.addWidget(self.authoring_workspace)
        body.addStretch(1)
        self.sample_source_card,sample_body=task_card('card_source')
        self.sample_source_card.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Expanding)
        workspace.addWidget(self.sample_source_card,0,0)
        workspace.addWidget(self.instrument_workspace,0,1,Qt.AlignTop)
        # One source workspace. Drawing changes the input,not the surrounding
        # editor/card ownership;inapplicable sample tasks remain disabled.
        source_row=control_row_layout()
        self.source_choice=AnimatedComboBox();self.source_choice.setAccessibleName(tr('creator.source_input'))
        self.source_choice.addItem(tr('creator.sample_input'),'sample')
        for kind in ('sine','triangle','saw','square','silence'):
            self.source_choice.addItem(tr('creator.shape_'+kind),kind)
        self.source_choice.addItem(tr('creator.draw_input'),'draw')
        source_row.addWidget(ElasticLabel(tr('creator.source_input')));source_row.addWidget(self.source_choice,1)
        self.undo=NativeTransportButton('undo',tr('creator.undo'),lambda:self.history(-1))
        self.redo=NativeTransportButton('redo',tr('creator.redo'),lambda:self.history(1))
        source_row.addWidget(self.undo);source_row.addWidget(self.redo);sample_body.addLayout(source_row)
        self.wave_canvas=InstrumentWaveCanvas(editable=True);self.wave_canvas.waveEdited.connect(self.set_wave)
        self.wave_canvas.setToolTip(tr('creator.wave_help'))
        sample_bar=control_row_layout()
        self.import_button=make_action_button(tr('creator.import'),self.choose_samples)
        self.remove_button=make_action_button(tr('creator.remove'),self.remove_zone)
        self.sample_list=AnimatedComboBox();self.sample_list.setMinimumWidth(0)
        sample_bar.addWidget(self.import_button);sample_bar.addWidget(self.sample_list,1);sample_bar.addWidget(self.remove_button)
        sample_body.addLayout(sample_bar)
        self.sample_canvas=SampleWaveCanvas();self.sample_canvas.markerEdited.connect(self.move_marker)
        self.sample_canvas.seekRequested.connect(self.sample_seek)
        self.sample_canvas.listenRequested.connect(lambda:self.sample_play(force=True))
        self.sample_canvas.selectionChanged.connect(self.sample_selection)
        self.sample_canvas.selectionCommitted.connect(self.commit_sample_selection)
        self.source_canvas=QStackedWidget();self.source_canvas.setMinimumSize(0,0)
        self.source_canvas.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Expanding)
        self.source_canvas.addWidget(self.wave_canvas);self.source_canvas.addWidget(self.sample_canvas)
        self.source_canvas.setCurrentIndex(1)
        self.source_overview=SourceWaveOverview();self.source_overview.set_canvas(self.sample_canvas)
        self.source_canvas.currentChanged.connect(lambda index:self.source_overview.set_canvas(self.source_canvas.widget(index)))
        sample_body.addWidget(self.source_overview);sample_body.addWidget(self.source_canvas,1)
        self.sample_scroll=QScrollBar(Qt.Horizontal);self.sample_scroll.setMinimumWidth(0)
        self.sample_scroll.setAccessibleName(tr('creator.timeline_scroll'))
        self.sample_scroll.valueChanged.connect(lambda value:self.sample_canvas.set_view(value/1000000,self.sample_canvas.view[1]-self.sample_canvas.view[0]))
        self.sample_canvas.viewChanged.connect(self._sync_sample_scroll);sample_body.addWidget(self.sample_scroll);self._sync_sample_scroll()
        transport=control_row_layout()
        self.sample_zone_actions=[]
        self.sample_play_button=NativeTransportButton('play',tr('creator.sample_play'),self.sample_play,primary=True)
        self.sample_stop_button=NativeTransportButton('stop',tr('creator.sample_stop'),self.sample_stop)
        transport.addWidget(self.sample_play_button);transport.addWidget(self.sample_stop_button)
        self.sample_position=ElasticLabel(tr('creator.sample_position',position=0,duration=0),preferred_width=145);transport.addWidget(self.sample_position)
        self.audition_loop=NativeTransportButton('loop',tr('creator.audition_loop'),lambda:None)
        self.audition_loop.setCheckable(True)
        self.audition_loop.toggled.connect(self.sample_repeat_changed)
        transport.addWidget(self.audition_loop)
        self.sample_info=NativeOptionalLabel('',preferred_width=430)
        transport.addWidget(self.sample_info,1);transport.addStretch()
        for key,callback in (('zoom_in',lambda:self.sample_canvas.zoom(2)),('zoom_out',lambda:self.sample_canvas.zoom(.5)),('zoom_fit',self.sample_canvas.fit)):
            button=NativeTransportButton(key,tr('creator.'+key),callback)
            self.sample_zone_actions.append(button);transport.addWidget(button)
        sample_body.addLayout(transport)
        selection_bar=control_row_layout()
        self.selection_label=NativeOptionalLabel('',preferred_width=180);selection_bar.addWidget(self.selection_label,1);selection_bar.addStretch()
        self.selection_actions=[]
        for key,callback in (('restore_bounds',self.restore_sample_bounds),):
            button=make_action_button(tr('creator.'+key),callback);selection_bar.addWidget(button)
            if key!='restore_bounds':self.selection_actions.append(button)
            else:self.sample_zone_actions.append(button)
        sample_body.addLayout(selection_bar)
        self.sample_details=make_navigation_tabs(stable_page_height=True);self.sample_details.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        self.sample_details.setStyleSheet(flat_tab_pane_stylesheet())
        self.sample_canvas.setMinimumHeight(96)
        mapping_page=QWidget();mapping_body=set_layout(QVBoxLayout(mapping_page),0,METRICS['compact_content_gap'])
        editing_page=QWidget();editing_body=set_layout(QVBoxLayout(editing_page),0,METRICS['compact_content_gap'])
        analysis_page=QWidget();analysis_body=set_layout(QVBoxLayout(analysis_page),0,METRICS['compact_content_gap'])
        for task_layout in (mapping_body,editing_body,analysis_body):task_layout.setAlignment(Qt.AlignTop)
        fields=control_grid_layout();self.zone_fields={}
        for index,(key,lo,hi) in enumerate((('root',0,127),('low',0,127),('high',0,127),
                ('velocity_low',1,127),('velocity_high',1,127),('tune',-99,99))):
            cell=QWidget();line=set_layout(QHBoxLayout(cell),0,6)
            cell.setFixedHeight(FORM_ROW_HEIGHT)
            line.addWidget(ElasticLabel(tr('creator.'+key)),1)
            control=NoteChoice(tr('creator.'+key)) if key in ('root','low','high') else NativeNumberBox(lo,hi,tr('creator.'+key));self.zone_fields[key]=control
            if key not in ('root','low','high'):control.setToolTip(tr('creator.help_'+key))
            if key=='tune':control.setSuffix(' cents')
            control.valueChanged.connect(lambda value,field=key:self.change_zone(field,value));line.addWidget(control,1)
            fields.addWidget(cell,index//2,index%2)
        mapping_body.addLayout(fields)
        for key in ('root','low','high'):self.zone_fields[key].set_meaning(tr('creator.help_'+key))
        mapping_bar=control_row_layout()
        self.key_tracking=NativeToggleSwitch(tr('creator.key_tracking'));self.key_tracking.setAccessibleName(tr('creator.key_tracking'))
        self.key_tracking.toggled.connect(lambda value:self.change_zone('key_tracking',value))
        mapping_body.addWidget(self.key_tracking)
        for key,callback in (('single_key',self.single_key),('all_keys',self.all_keys),('auto_ranges',self.auto_ranges)):
            button=make_action_button(tr('creator.'+key),callback)
            button.setProperty('waveApplicable',key in ('single_key','all_keys'))
            self.sample_zone_actions.append(button);mapping_bar.addWidget(button)
        mapping_body.addLayout(mapping_bar)
        self.key_range=SampleKeyRange();self.key_range.rangeEdited.connect(self.change_zone)
        self.key_range.noteOn.connect(self.audition_note_on);self.key_range.noteOff.connect(self.audition_note_off)
        mapping_body.addStretch(1)
        self.sample_loop_section,loop_grid=make_control_section(tr('creator.instrument_loop'))
        mapping_body.insertWidget(mapping_body.count()-1,self.sample_loop_section)
        trim=control_grid_layout()
        for index,key in enumerate(('start','end','loop_start','loop_end')):
            cell=QWidget();line=set_layout(QHBoxLayout(cell),0,6)
            cell.setFixedHeight(FORM_ROW_HEIGHT)
            line.addWidget(ElasticLabel(tr('creator.'+key)),1)
            control=NativeDecimalBox(0,1,tr('creator.'+key),decimals=3,suffix=' ms');self.zone_fields[key]=control
            control.setToolTip(tr('creator.help_'+key))
            control.valueChanged.connect(lambda value,field=key:self.change_zone(field,round(value*self.zone().rate/1000)) if self.zone() else None);line.addWidget(control,1)
            if key in ('start','end'):trim.addWidget(cell,0,index)
            else:loop_grid.addWidget(cell,1,index-2)
        editing_body.addLayout(trim)
        self.loop=AnimatedComboBox()
        self.loop.setToolTip(tr('creator.help_loop'))
        self.loop.setProperty('fitPreferenceContents',True)
        for value in (0,1,3):self.loop.addItem(tr('creator.loop_'+str(value)),value)
        self.loop.currentIndexChanged.connect(lambda _:self.change_zone('loop',self.loop.currentData()))
        loop_grid.addWidget(self.loop,0,0,1,2)
        self.sample_controls={}
        sample_controls_grid=control_grid_layout()
        for key,lo,hi,initial,step,unit in (('speed',25,400,100,1,'%'),('pitch',-24,24,0,.01,'semitones'),('sample_gain',-48,12,0,.1,'dB')):
            control=NativeControlRow(tr('creator.'+key),lo,hi,initial,
                lambda value,key=key:self.change_zone('gain' if key=='sample_gain' else key,value/100 if key=='speed' else value),
                lambda value,key=key:tr('creator.pitch_value',value=value) if key=='pitch' else f'{value:.0f} %' if key=='speed' else f'{value:.1f} dB',
                step=step,input_steps=(step,step*10,step*100),label_width=85,value_width=93,
                entry_kind='number_integer' if key=='speed' else 'number_hundredth' if key=='pitch' else 'db_tenth',
                entry_context=lambda:(self._entry_epoch,self.draft.mode,id(self.zone().pcm) if self.zone() else None),
                tooltip=tr('creator.help_'+key))
            index=len(self.sample_controls);self.sample_controls[key]=control;sample_controls_grid.addWidget(control,index,0)
        editing_body.addLayout(sample_controls_grid)
        edit_bar=control_row_layout();self.reverse=NativeToggleSwitch(tr('creator.reverse'));self.reverse.setAccessibleName(tr('creator.reverse'))
        self.reverse.toggled.connect(lambda value:self.change_zone('reverse',value))
        edit_bar.addWidget(self.reverse)
        for key,callback in (('normalize',self.normalize_sample),('reset_transform',self.reset_sample_transform),('snap_zero',self.snap_zero)):
            if key=='snap_zero':edit_bar.addStretch(1)
            button=make_action_button(tr('creator.'+key),callback)
            if key=='reset_transform':button.setProperty('waveApplicable',True)
            if key=='snap_zero':button.setToolTip(tr('creator.snap_zero_help'))
            self.sample_zone_actions.append(button);edit_bar.addWidget(button)
        editing_body.addLayout(edit_bar)
        self.analysis_button=make_action_button(tr('creator.analyze'),self.analyze_current_sample)
        self.analysis_button.setToolTip(tr('creator.analysis_hint'))
        self.pitch_result=QLabel(tr('creator.pitch_empty'));self.pitch_result.setWordWrap(True);self.pitch_result.setMinimumWidth(0)
        self.bpm_result=QLabel(tr('creator.bpm_empty'));self.bpm_result.setWordWrap(True);self.bpm_result.setMinimumWidth(0)
        self.apply_root_button=make_action_button(tr('creator.apply_pitch'),self.apply_analyzed_root)
        self.target_bpm=NativeDecimalBox(30,300,tr('creator.target_bpm'),decimals=1,suffix=' BPM');self.target_bpm.setValue(120)
        self.apply_tempo_button=make_action_button(tr('creator.fit_bpm'),self.apply_analyzed_tempo)
        analysis_body.addWidget(self.analysis_button)
        pitch_bar=control_row_layout();pitch_bar.addWidget(self.pitch_result,1);pitch_bar.addWidget(self.apply_root_button);analysis_body.addLayout(pitch_bar)
        analysis_body.addWidget(self.bpm_result)
        bpm_bar=control_row_layout();bpm_bar.addWidget(ElasticLabel(tr('creator.target_bpm')));bpm_bar.addWidget(self.target_bpm,1);bpm_bar.addWidget(self.apply_tempo_button)
        analysis_body.addLayout(bpm_bar)
        for page,key in ((mapping_page,'mapping_tab'),(editing_page,'editing_tab'),(analysis_page,'analysis_tab')):
            self.sample_details.addTab(page,tr('creator.'+key))
        sample_body.addWidget(self.sample_details)
        self.parameters_card,properties_body=task_card('card_parameters')
        self.instrument_scope_caption=ElasticLabel(tr('creator.instrument_scope'))
        self.instrument_scope_caption.setObjectName('CardTitle')
        scope_header=control_row_layout();scope_header.addWidget(self.instrument_scope_caption,1)
        properties_body.addLayout(scope_header)
        self.instrument_details=make_navigation_tabs(stable_page_height=True)
        self.instrument_details.setStyleSheet(flat_tab_pane_stylesheet())
        properties_body.addWidget(self.instrument_details)
        self.source_choice.currentIndexChanged.connect(self._source_input_changed)
        self.sample_list.currentIndexChanged.connect(self.select_zone)
        # Persistent peer tasks in the right instrument column.
        # Envelope and timbre are instrument-wide,not per-zone copies.
        self.sound_panel=QWidget();self.sound_panel.setMinimumWidth(0)
        sound_layout=set_layout(QVBoxLayout(self.sound_panel),0,METRICS['section_gap'])
        sound_layout.setAlignment(Qt.AlignTop)
        # The page's tab already identifies the envelope. A second framed
        # title repeats that hierarchy and costs useful editing height.
        self.envelope_section=QWidget();self.envelope_section.setMinimumWidth(0)
        envelope_body=set_layout(QVBoxLayout(self.envelope_section),0,0)
        tone_body=control_grid_layout()
        tone_body.setAlignment(Qt.AlignTop)
        tone_body.setColumnStretch(0,1)
        self.controls={}
        for key,lo,hi,step in (('attack',0,5,.001),('decay',0,5,.001),('sustain',0,100,1),('release',0,5,.001),('gain',-48,0,.1)):
            time_field=key in ('attack','decay','release')
            control=(EnvelopeTimeRow if time_field else NativeControlRow)(tr('creator.'+key),lo,hi,getattr(self.draft,key),
                lambda value,field=key:self.change(field,value),
                lambda value,field=key:f'{value:.1f} dB' if field=='gain' else f'{value:.0f} %' if field=='sustain' else f'{value:.3f} s',
                step=step,input_steps=(step,step*10,step*100),label_width=50,value_width=64,
                preserve_value_precision=time_field,snap_navigation=time_field,
                tooltip=tr('creator.help_'+key),entry_kind='number_thousandth' if time_field else 'db_tenth' if key=='gain' else 'number_integer',
                entry_context=lambda:self._entry_epoch)
            self.controls[key]=control
        for key in ('envelope_delay','envelope_hold'):
            spec=NATIVE_SOUND_PARAMETERS[key]
            control=EnvelopeTimeRow(tr('creator.'+key),spec.minimum,spec.maximum,spec.default,
                lambda value,field=key:self.change(field,value),lambda value:f'{value:.3f} s',
                step=spec.step,input_steps=(.001,.01,.1),label_width=50,value_width=64,
                preserve_value_precision=True,snap_navigation=True,
                tooltip=tr('creator.help_'+key),entry_kind='number_thousandth',entry_context=lambda:self._entry_epoch)
            self.controls[key]=control
        for index,key in enumerate(('envelope_delay','attack','envelope_hold','decay','sustain','release')):
            tone_body.addWidget(self.controls[key],index,0)
        envelope_body.addLayout(tone_body);sound_layout.addWidget(self.envelope_section)
        self.timbre_page=QWidget();self.timbre_page.setMinimumWidth(0)
        timbre_layout=set_layout(QVBoxLayout(self.timbre_page),0,0)
        timbre_layout.setAlignment(Qt.AlignTop)
        sound_groups=control_grid_layout()
        self.sound_sections={}
        for section,keys in (('filter',('filter_cutoff','filter_resonance')),
                             ('vibrato',('vibrato_rate','vibrato_depth','vibrato_delay')),
                             ('tremolo',('tremolo_rate','tremolo_depth','tremolo_delay'))):
            group,group_body=make_control_section(tr('creator.section_'+section))
            knob_row=(set_layout(QVBoxLayout(),0,METRICS['compact_content_gap']) if section=='filter' else control_row_layout())
            group_body.addLayout(knob_row,0,0)
            for index,key in enumerate(keys):
                spec=NATIVE_SOUND_PARAMETERS[key];time_field=spec.unit=='seconds'
                unit={'seconds':'s','hz':'Hz','db':'dB','cents':'cents'}[spec.unit]
                precision=3 if time_field else 0 if key=='filter_cutoff' or spec.unit=='cents' else 1 if spec.unit=='db' else 2
                formatter=lambda value,unit=unit,precision=precision:f'{value:.{precision}f} {unit}'
                callback=lambda value,field=key:self.change(field,value)
                control=NativeKnobField(tr('creator.'+key),spec.minimum,spec.maximum,spec.default,callback,formatter,
                    tooltip=tr('creator.help_'+key),editable=True,value_scale=1000 if spec.step<1 else 1,value_step=spec.step,
                    preserve_value_precision=time_field,
                    edit_context=lambda:self._entry_epoch,
                    entry_domain=numeric_entry_domain('number_thousandth' if time_field or spec.unit=='hz' and spec.step<1 else
                        'number_integer' if precision==0 else 'number_tenth',spec.minimum,spec.maximum))
                knob_row.addWidget(control,1)
                self.controls[key]=control
            self.sound_sections[section]=group
            if section=='filter':
                # Stack the filter's knobs across the two compact LFO rows.
                group.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Expanding)
                group.layout().setAlignment(Qt.Alignment())
                group.layout().setStretch(1,1);group.layout().setStretch(2,0)
                sound_groups.addWidget(group,0,1,2,1)
            else:sound_groups.addWidget(group,0 if section=='vibrato' else 1,0)
        sound_groups.setColumnStretch(0,3);sound_groups.setColumnStretch(1,1)
        timbre_layout.addLayout(sound_groups)
        # Reset belongs to the card heading;gain owns its full control row.
        self.sound_footer=QWidget();self.sound_footer.setMinimumWidth(0)
        sound_footer=control_row_layout(self.sound_footer);sound_footer.addWidget(self.controls['gain'],1)
        self.sound_restore_button=make_action_button(tr('common.restore_defaults'),self.restore_native_sound)
        self.sound_restore_button.setToolTip(tr('creator.restore_sound_help'))
        scope_header.addWidget(self.sound_restore_button);properties_body.insertWidget(1,self.sound_footer)
        self.sound_page=QWidget();sound_page_body=set_layout(QVBoxLayout(self.sound_page),0,0)
        sound_page_body.setAlignment(Qt.AlignTop)
        self.sound_page_index=self.instrument_details.addTab(self.sound_page,tr('creator.envelope_tab'))
        sound_page_body.addWidget(self.sound_panel)
        self.timbre_page_index=self.instrument_details.addTab(self.timbre_page,tr('creator.timbre_tab'))
        self.audition_card,audition_body=task_card('card_audition')
        self.processed_card,processed_body=task_card('processed_wave')
        self.processed_caption=ElasticLabel(tr('creator.processed_wave'),preferred_width=120)
        wave_toolbar=control_row_layout();wave_toolbar.addWidget(self.processed_caption,1)
        self.processed_zoom_buttons=[]
        for key,factor in (('zoom_in',2),('zoom_out',.5),('zoom_fit',None)):
            button=NativeTransportButton(key,tr('creator.'+key),lambda _checked=False,factor=factor:self.processed_wave.fit() if factor is None else self.processed_wave.zoom(factor))
            wave_toolbar.addWidget(button);self.processed_zoom_buttons.append(button)
        processed_body.addLayout(wave_toolbar)
        self.processed_wave=ProcessedInstrumentWave();processed_body.addWidget(self.processed_wave,1)
        self.processed_scroll=QScrollBar(Qt.Horizontal);self.processed_scroll.setMinimumWidth(0)
        self.processed_scroll.setAccessibleName(tr('creator.processed_wave_scroll'))
        self.processed_scroll.valueChanged.connect(lambda value:self.processed_wave.set_view(value/1000000,self.processed_wave.view[1]-self.processed_wave.view[0]))
        processed_body.addWidget(self.processed_scroll)
        duration_row=control_row_layout()
        duration_label=ElasticLabel(tr('creator.preview_duration'))
        self.preview_duration=NativeDecimalBox(0,MAX_PREVIEW_BYTES/(8*400),tr('creator.preview_duration'),decimals=3,suffix=' s')
        self.preview_duration.setSpecialValueText(tr('creator.preview_duration_auto'))
        self.preview_duration.setSingleStep(.1);self.preview_duration.setKeyboardTracking(False)
        install_effect_tooltip(tr('creator.preview_duration_help'),duration_label,self.preview_duration)
        self.preview_duration.valueChanged.connect(lambda _value:self._queue_processed_wave())
        self.processed_readout=ElasticLabel('')
        duration_row.addWidget(duration_label);duration_row.addWidget(self.preview_duration,1);duration_row.addWidget(self.processed_readout,1)
        processed_body.addLayout(duration_row)
        self.processed_wave.viewChanged.connect(self._sync_processed_wave_view);self._sync_processed_wave_view()
        self.audition_workspace=QWidget();self.audition_workspace.setMinimumWidth(0)
        self.audition_workspace.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        audition_cards=card_grid_layout(self.audition_workspace)
        for card in (self.processed_card,self.audition_card):card.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        audition_cards.addWidget(self.processed_card,0,0);audition_cards.addWidget(self.audition_card,1,0)
        audition_cards.setColumnStretch(0,1)
        self.preview_note=69
        self.audition_caption=ElasticLabel(tr('creator.audition_note',note=note_caption(69)),preferred_width=235)
        # Keep preparation feedback next to the keyboard,within its existing
        # header. It must not grow the identity-command row and reflow cards.
        audition_header=control_row_layout();audition_header.addWidget(self.audition_caption,1)
        audition_header.addWidget(self.status)
        self.instrument_phrase_button=make_instrument_audition(self.group_dialog.owner,self._draft_phrase_request,self.audition_card)
        self.instrument_phrase_button.setEnabled(False);audition_header.addWidget(self.instrument_phrase_button)
        audition_body.addLayout(audition_header)
        audition_row=control_row_layout()
        # One keyboard serves audition and sample-range editing. Do not show a
        # second, competing keyboard on the mapping page.
        self.audition_keyboard=self.key_range
        self.key_range.audition_only=False
        audition_row.addWidget(NativeTransportButton('previous',tr('creator.octave_down'),lambda:self.audition_keyboard.octave(-1)))
        audition_row.addWidget(self.audition_keyboard,1)
        audition_row.addWidget(NativeTransportButton('next',tr('creator.octave_up'),lambda:self.audition_keyboard.octave(1)))
        audition_body.addLayout(audition_row)
        self.keyboard_scroll=QScrollBar(Qt.Horizontal);self.keyboard_scroll.setRange(0,(128-self.key_range.span)*1000);self.keyboard_scroll.setPageStep(self.key_range.span*1000)
        self.keyboard_scroll.setValue(round(self.key_range.first*1000));self.keyboard_scroll.setSingleStep(12000)
        self.keyboard_scroll.setAccessibleName(tr('creator.keyboard_scroll'))
        self.keyboard_scroll.valueChanged.connect(lambda value:self.key_range.set_first(value/1000))
        self.key_range.viewChanged.connect(self._sync_keyboard_scroll);audition_body.addWidget(self.keyboard_scroll)
        instrument_column.addWidget(self.parameters_card)
        instrument_column.addWidget(self.audition_workspace)
        for control in self.findChildren(QWidget):
            if hasattr(control,'setAutoDefault'):control.setAutoDefault(False)
        QShortcut(QKeySequence.Undo,self,activated=lambda:self.history(-1))
        QShortcut(QKeySequence.Redo,self,activated=lambda:self.history(1))
        sample_shortcut=QShortcut(QKeySequence(Qt.Key_Space),self.sample_canvas,activated=self.sample_play)
        sample_shortcut.setContext(Qt.WidgetShortcut)
        self.select_zone();self._history_buttons();self.refresh_saved();self.refresh_address();apply_native_text_metrics(self)
        self._reset_history()
        self._sync_source_actions()
        align_form_labels((control.title for control in self.controls.values() if isinstance(control,NativeControlRow)),
                          50,0)
        # Peer tabs reserve the largest page at this width. Switching tasks
        # changes neither card ownership nor extent nor outer-window fitting.
        # The host is attached after construction. Install on the first show.
        QTimer.singleShot(0,self,self._fit_workspace)

    def _lease_workspace_scroll(self):
        scroll=getattr(self.group_dialog,'_creator_scroll',None)
        if scroll is not None and self._workspace_scroll_policy is None and automatic_window_fit_allowed(self.window()):
            self._workspace_scroll_policy=scroll.verticalScrollBarPolicy()
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def _finish_workspace_transition(self):
        if self._workspace_motion._started_at is not None:return
        scroll=getattr(self.group_dialog,'_creator_scroll',None)
        if scroll is not None and self._workspace_scroll_policy is not None:
            scroll.setVerticalScrollBarPolicy(self._workspace_scroll_policy);self._workspace_scroll_policy=None

    def _fit_workspace(self,*,settled=False):
        # Opening the creator adds real controls beyond the first routing
        # page's16-row opening preference. Grow only if these do not fit;
        # never turn preferred widths into a horizontal floor or shrink back.
        if not self.isVisible() or not automatic_window_fit_allowed(self.window()):return
        if not settled:
            # One bounded follow-up after Qt applies the nested page geometry.
            # Never attach this fitter to manual resize or animation frames.
            QTimer.singleShot(1,self,lambda:self._fit_workspace(settled=True))
        # Invalidate from the leaf pages upward. A layout-only invalidation
        # leaves QWidget's ancestor size hints stale until a later event.
        for layout in reversed(self.findChildren(QLayout)):
            layout.invalidate()
            host=layout.parentWidget()
            if host is not None:host.updateGeometry()
        self.layout().activate()
        window=self.window()
        scroll=getattr(self.group_dialog,'_creator_scroll',None)
        capacity=scroll.viewport().height() if scroll is not None else self.height()
        deficit=self._measured_height()-capacity
        if deficit<=0:return
        target=window.height()+deficit
        if self.screen() and QApplication.platformName()!='offscreen':
            target=min(target,self.screen().availableGeometry().height()-(window.frameGeometry().height()-window.height()))
        if target>window.height():
            motion=self._workspace_motion
            if motion._started_at is None or target>motion.motion.endValue().height():
                self._lease_workspace_scroll()
                motion.start(QSize(window.width(),target),expanding=True)

    def _sync_sample_scroll(self):
        a,b=self.sample_canvas.view;page=round((b-a)*1000000)
        self.sample_scroll.blockSignals(True);self.sample_scroll.setPageStep(page);self.sample_scroll.setRange(0,1000000-page)
        self.sample_scroll.setSingleStep(max(1,page//8));self.sample_scroll.setValue(round(a*1000000));self.sample_scroll.setEnabled(page<1000000);self.sample_scroll.blockSignals(False)

    def _sync_keyboard_scroll(self):
        self.keyboard_scroll.blockSignals(True)
        self.keyboard_scroll.setRange(0,(128-self.key_range.span)*1000);self.keyboard_scroll.setPageStep(self.key_range.span*1000)
        self.keyboard_scroll.setValue(round(self.key_range.first*1000));self.keyboard_scroll.blockSignals(False)

    def _sync_processed_wave_view(self):
        canvas=self.processed_wave;available=bool(canvas.values.size)
        a,b=canvas.view;page=round((b-a)*1000000)
        self.processed_scroll.blockSignals(True)
        self.processed_scroll.setRange(0,1000000-page);self.processed_scroll.setPageStep(page)
        self.processed_scroll.setSingleStep(max(1,page//8));self.processed_scroll.setValue(round(a*1000000))
        self.processed_scroll.setEnabled(available and page<1000000);self.processed_scroll.blockSignals(False)
        for button in self.processed_zoom_buttons:button.setEnabled(available)
        if not available:self.processed_readout.clear();return
        readout=tr('creator.processed_wave_readout',span=(b-a)*len(canvas.values)/canvas.rate*1000)
        self.processed_readout.setText(tr('creator.processed_wave_updating',readout=readout) if canvas.pending else readout)

    def change(self,key,value):
        if not self._loading:
            previous=getattr(self.draft,key)
            if (np.array_equal(previous,value) if isinstance(previous,np.ndarray) else previous==value):return
            # Naming changes persistence,not PCM or the prepared MIDI voice.
            if key!='name':
                self._draft_revision+=1
                self._invalidate_processed_wave()
                self._audition_debounce.start()
            setattr(self.draft,key,value);self.dirty=True
            self._sync_source_actions()
            self.status.clear()
            if not self._source_ready(report=True):self.stop_preview()
            if key in ('bank','program'):
                if key=='bank':self._populate_programs(value)
                else:self._sync_address_fields()
                self.refresh_address()
            self._remember_edit()

    def _source_input_changed(self,index):
        if self._loading:return
        choice=self.source_choice.itemData(index)
        returning=choice=='sample' and self.draft.mode!='sample'
        if choice!='sample' and self.draft.mode=='sample':self._sample_task_index=self.sample_details.currentIndex()
        if choice!='sample':
            self.sample_stop();self._sample_debounce.stop()
            self._analysis=None
            if self._sample_job:self._sample_job.requestInterruption()
        def choose():
            if choice!='sample' and self.draft.mode!='wave':self.draft.wave_peak_fit=True
            self.change('mode','sample' if choice=='sample' else 'wave')
            if choice not in ('sample','draw'):
                self.draft.wave_peak_fit=True
                self.set_wave(wave_preset(choice),source=choice)
        self._batch_edit(choose)
        self.select_zone()
        if returning:self.sample_details.setCurrentIndex(getattr(self,'_sample_task_index',0))

    def _read_source_choice(self):
        choice='sample' if self.draft.mode=='sample' else next((kind for kind in
            ('sine','triangle','saw','square','silence') if np.array_equal(self.draft.wave,wave_preset(kind))),'draw')
        blocked=self.source_choice.blockSignals(True)
        self.source_choice.setCurrentIndex(self.source_choice.findData(choice));self.source_choice.blockSignals(blocked)

    @staticmethod
    def _edit_signature(draft):
        return (tuple((key,value) for key,value in vars(draft).items() if key not in ('wave','zones')),
                draft.wave.tobytes(),tuple((id(zone.pcm),tuple((key,value) for key,value in vars(zone).items() if key!='pcm'))
                                          for zone in draft.zones))

    def _reset_history(self):
        self._history=[self.draft.snapshot()];self._history_at=0
        self._clean_signature=self._edit_signature(self.draft);self.dirty=False;self._history_buttons()
        self._sync_source_actions()

    def _remember_edit(self):
        if self._loading or self._history_replay or self._history_batch:return
        signature=self._edit_signature(self.draft)
        if self._history_at>=0 and signature==self._edit_signature(self._history[self._history_at]):
            self.dirty=signature!=self._clean_signature;self._sync_source_actions();self.group_dialog._sync_save_actions();return
        self._history=self._history[:self._history_at+1]+[self.draft.snapshot()]
        self._history=self._history[-64:];self._history_at=len(self._history)-1
        self.dirty=signature!=self._clean_signature;self._history_buttons()
        self._sync_source_actions()
        self.group_dialog._sync_save_actions()

    def _batch_edit(self,callback):
        self._history_batch=True
        try:callback()
        finally:self._history_batch=False
        self._remember_edit()

    def restore_native_sound(self):
        defaults=InstrumentDraft()
        def restore():
            for key,control in self.controls.items():
                value=getattr(defaults,key);control.set(value,False);self.change(key,value)
        self._batch_edit(restore)

    def change_instrument_kind(self,value):
        if self._loading:return
        if value and self.draft.bank<128:self._melodic_bank=self.draft.bank
        self.change('bank',128 if value else self._melodic_bank)

    def _sync_address_fields(self):
        """One projection of the stored address;disabled never means stale."""
        drums=self.draft.bank==128
        controls=(self.bank,self.instrument_kind,self.drum_program)
        blocked=[control.blockSignals(True) for control in controls]
        try:
            self.bank.setRange(128,128) if drums else self.bank.setRange(0,127)
            self.bank.setValue(self.draft.bank)
            self.bank.setEnabled(not drums)
            self.address_labels['bank'].setEnabled(not drums)
            self.bank.setToolTip(tr('creator.drum_bank_tip' if drums else 'creator.bank_tip'))
            self.address_labels['bank'].setToolTip(self.bank.toolTip())
            self.instrument_kind.setValue(int(drums))
            self.drum_program.setValue(self.draft.program)
            self.program_field.setCurrentWidget(self.drum_program if drums else self.program)
            self._identity_address_row.setStretch(self._identity_address_row.indexOf(self.name),6 if drums else 3)
            self._identity_address_row.setStretch(self._identity_address_row.indexOf(self.program_field),1 if drums else 4)
            self.address_labels['program'].setBuddy(self.drum_program if drums else self.program)
            self.address_labels['program'].setText(tr('creator.program' if drums else 'creator.instrument'))
            if not drums:self._melodic_bank=self.draft.bank
        finally:
            for control,state in zip(controls,blocked):control.blockSignals(state)

    def _set_unused_address(self,draft):
        used={(item['bank'],item['preset']) for item in getattr(self.group_dialog,'_instrument_rows',())}
        used.update((entry.custom['bank'],entry.custom['program']) for entry in self.group_dialog.group.fonts if entry.custom)
        draft.bank,draft.program=next(((bank,program) for bank in range(128) for program in range(128)
                                       if (bank,program) not in used),(0,0))

    def _populate_programs(self,bank):
        self._sync_address_fields()
        selected=self.draft.program
        self.program.blockSignals(True);self.program.clear()
        if bank==128:
            self.program.blockSignals(False)
            return
        current={(row['bank'],row['preset']):row for row in getattr(self.group_dialog,'_instrument_rows',())}
        for program in range(128):
            gm=self._gm_name(bank,program)
            row=current.get((bank,program))
            fallback=current.get((0,program)) if bank not in (0,128) and row is None else None
            sounding=row['name'] if row else tr('creator.unassigned')
            # A menu identifies a choice; the fallback explanation belongs in
            # the adjacent address context, not in128 oversized menu rows.
            compact_sound=row['name'] if row else tr('creator.unassigned')
            self.program.addItem(tr('creator.program_choice',number=program,gm=gm,current=compact_sound),program)
            self.program.setItemData(program,tr('creator.program_compact',number=program,gm=gm),AnimatedComboBox.COMPACT_TEXT_ROLE)
            self.program.setItemData(program,tr('creator.program_context',gm=gm,current=sounding),Qt.ToolTipRole)
        self.program.setValue(selected);self.program.blockSignals(False)

    def _gm_name(self,bank,program):
        return tr('creator.gm_drums') if bank==128 else self._gm_names.get((0,program),tr('creator.no_gm'))

    def refresh_address(self):
        bank,program=self.draft.bank,self.draft.program
        mode=getattr(self.group_dialog.owner,'_report_playback_snapshot',{}).get('midi_system_mode')
        self.key_range.drum_mode=mode
        self.key_range.drum=bank==128;self.key_range.update()
        for field in self.zone_fields.values():
            if isinstance(field,NoteChoice):field.set_drum(bank==128,mode)
        gm=self._gm_name(bank,program)
        rows=getattr(self.group_dialog,'_instrument_catalog_by_key',{}).get((bank,program),())
        current=next((row for row in getattr(self.group_dialog,'_instrument_rows',()) if (row['bank'],row['preset'])==(bank,program)),None)
        fallback=next((row for row in getattr(self.group_dialog,'_instrument_rows',()) if (row['bank'],row['preset'])==(0,program)),None) if bank not in (0,128) and current is None else None
        sounding=current['name'] if current else tr('creator.unassigned')
        identities=list(dict.fromkeys(tr('creator.overlap_item',name=row['name'],font=Path(row['filename']).name) for row in rows))
        names=' / '.join(identities[:4]) or tr('creator.free')
        if len(identities)>4:names=tr('creator.more_overlaps',names=names,count=len(identities)-4)
        self.address_info.setText(tr('creator.address_context',gm=gm,current=sounding,
            overlaps=names,result=tr('creator.replaces') if current else tr('creator.fallback_consequence',name=fallback['name']) if fallback else tr('creator.adds')))
        self.address_info.setToolTip('\n'.join(identities))

    def refresh_saved(self):
        self.saved_list.clear()
        for entry in self.group_dialog.group.fonts:
            if entry.custom is not None:
                self.saved_list.addItem(tr('creator.saved_choice',name=entry.custom['name'],bank=entry.custom['bank'],program=entry.custom['program']),entry.filename)
        self.saved_list.setCurrentIndex(self.saved_list.findData(self._entry_id) if self._entry_id else -1)
        self._saved_actions()
        self._sync_source_actions()

    def _saved_actions(self,*_):
        selected=self.saved_list.currentIndex()>=0
        self.open_button.setEnabled(selected);self.delete_button.setEnabled(selected)

    def delete_instrument(self):
        if (self.worker and self.worker.operation!='preview') or not self.confirm_replace():return
        if self.worker:self.worker.requestInterruption()
        index=next((i for i,entry in enumerate(self.group_dialog.group.fonts) if entry.filename==self.saved_list.currentData()),-1)
        if index<0:return
        self.group_dialog.font_table.selectRow(index);self.group_dialog.remove_font()
        if not any(entry.filename==self._entry_id for entry in self.group_dialog.group.fonts):self._entry_id=None
        self.refresh_saved();self._populate_programs(self.draft.bank);self.refresh_address()

    def set_wave(self,values,*,source='draw'):
        self.draft.wave=np.asarray(values,np.float32).copy()
        if source=='draw':
            self.draft.wave_peak_fit=True
            blocked=self.source_choice.blockSignals(True)
            self.source_choice.setCurrentIndex(self.source_choice.findData('draw'));self.source_choice.blockSignals(blocked)
        self.wave_canvas.set_wave(self.draft.wave)
        self._draft_revision+=1;self._invalidate_processed_wave();self._audition_debounce.start()
        self._remember_edit()

    def _history_buttons(self):
        self.undo.setEnabled(self._history_at>0);self.redo.setEnabled(self._history_at+1<len(self._history))

    def history(self,delta):
        if self.worker and self.worker.operation!='preview':return
        at=self._history_at+delta
        if not 0<=at<len(self._history):return
        if self.worker:self.worker.requestInterruption()
        view=self.sample_canvas.view;selected=self.sample_list.currentIndex();first=self.key_range.first
        self._history_at=at;self._history_replay=True
        try:
            self.load_draft(self._history[at].snapshot(),reset_history=False)
            self.sample_list.setCurrentIndex(max(0,min(selected,len(self.draft.zones)-1)) if self.draft.zones else -1)
            self.sample_canvas.set_view(view[0],view[1]-view[0]);self.key_range.set_first(first)
        finally:self._history_replay=False
        self.dirty=self._edit_signature(self.draft)!=self._clean_signature
        self._history_buttons();self._sample_debounce.start();self._audition_debounce.start()
        self._sync_source_actions()
        self.group_dialog._sync_save_actions()

    def zone(self):
        if self.draft.mode=='wave':
            key=(id(self.draft.wave),self.draft.wave_settings)
            if getattr(self,'_wave_zone_key',None)!=key:
                self._wave_zone_key=key;self._wave_zone=self.draft.wave_settings.zone(self.draft.wave)
            return self._wave_zone
        index=self.sample_list.currentIndex()
        return self.draft.zones[index] if 0<=index<len(self.draft.zones) else None

    def select_zone(self,*_):
        sample_mode=self.draft.mode=='sample'
        zone=self.zone();changed=sample_mode and zone is not self.sample_canvas.zone
        if changed:
            self.sample_stop();self._sample_media=None;self._analysis=None;self._sample_generation+=1;self._sample_seek=None
        self._loading=True
        try:
            for key,control in self.zone_fields.items():
                control.setEnabled(zone is not None)
                if zone:
                    time_field=key in ('start','end','loop_start','loop_end')
                    if time_field:control.setMaximum(len(zone.pcm)*1000/zone.rate)
                    control.setValue(getattr(zone,key)*1000/zone.rate if time_field else getattr(zone,key))
            self.loop.setEnabled(zone is not None)
            if zone:self.loop.setCurrentIndex(self.loop.findData(zone.loop))
            self.remove_button.setEnabled(zone is not None)
            if sample_mode:
                self.sample_canvas.set_zone(zone)
                self._sync_sample_scroll()
            self.key_range.zone=zone;self.key_range.update()
            anchor=(self.draft.mode,'wave' if self.draft.mode=='wave' else id(zone.pcm),zone.root) if zone else None
            old_anchor=getattr(self,'_mapping_anchor',None)
            if zone and anchor!=old_anchor:
                note=(min(zone.high,max(zone.low,self.preview_note)) if self.draft.mode=='wave' and
                      (not old_anchor or old_anchor[0]!='wave') else zone.root)
                self.key_range.center_on(note)
            self._mapping_anchor=anchor
            for key,control in self.sample_controls.items():
                control.setEnabled(zone is not None)
                if zone:control.set(zone.speed*100 if key=='speed' else zone.gain if key=='sample_gain' else zone.pitch,False)
            for toggle,key in ((self.reverse,'reverse'),(self.key_tracking,'key_tracking')):
                toggle.setEnabled(zone is not None)
                blocked=toggle.blockSignals(True);toggle.setChecked(bool(getattr(zone,key)) if zone else False);toggle.blockSignals(blocked)
            for button in (self.sample_play_button,self.sample_stop_button,self.analysis_button):button.setEnabled(zone is not None)
            for button in self.sample_zone_actions:button.setEnabled(zone is not None)
            self.audition_loop.setEnabled(zone is not None)
            self.apply_root_button.setEnabled(bool(self._analysis and self._analysis.get('pitch')))
            self.apply_tempo_button.setEnabled(bool(self._analysis and self._analysis.get('bpm')))
            if changed:
                self._reset_analysis_results()
            if sample_mode and (changed or zone is None):
                self.clear_selection()
            elif sample_mode and self._sample_range:self.sample_selection(*self._sample_range)
            self.sample_info.setText(tr('creator.sample_info',name=zone.name,rate=zone.rate/1000,channels=zone.pcm.shape[1],seconds=len(zone.pcm)/zone.rate) if zone else '')
            self.sample_info.setToolTip(str(zone.source_path) if zone else '')
            if zone and self._sample_media is None:
                self.sample_position.setText(tr('creator.sample_position',position=0,duration=((zone.end or len(zone.pcm))-zone.start)/zone.rate/zone.speed))
            elif zone is None:
                self.sample_position.setText(tr('creator.sample_position_empty'))
        finally:self._loading=False
        sample_mode=self.draft.mode=='sample'
        self.source_canvas.setCurrentIndex(1 if sample_mode else 0)
        self.key_range.audition_only=False
        self.sample_list.setEnabled(sample_mode);self.remove_button.setEnabled(sample_mode and zone is not None)
        self.sample_details.setEnabled(zone is not None)
        self.sample_details.setTabEnabled(2,sample_mode and zone is not None)
        self.sample_loop_section.setEnabled(sample_mode and zone is not None)
        for key in ('start','end','loop_start','loop_end'):self.zone_fields[key].setEnabled(sample_mode and zone is not None)
        self.sample_controls['speed'].setEnabled(sample_mode and zone is not None)
        self.zone_fields['root'].set_meaning(tr('creator.help_root' if sample_mode else 'creator.wave_root_help'))
        install_effect_tooltip(tr('creator.help_pitch' if sample_mode else 'creator.wave_pitch_help'),
            self.sample_controls['pitch'],self.sample_controls['pitch'].title,self.sample_controls['pitch'].slider,self.sample_controls['pitch'].value)
        self.analysis_button.setEnabled(sample_mode and zone is not None)
        if not sample_mode:
            self.sample_info.clear();self.sample_position.setText(tr('creator.sample_position_empty'));self.selection_label.clear()
        for control in (self.sample_play_button,self.sample_stop_button,self.audition_loop,self.sample_scroll,*self.sample_zone_actions):
            control.setEnabled(sample_mode and zone is not None)
        if not sample_mode:
            # Reuse the existing source commands only where they have a real
            # generated-cycle meaning. File timing/normalization stays local.
            for button in self.sample_zone_actions:
                if button.property('waveApplicable'):button.setEnabled(True)
        self._history_buttons()
        self._sync_source_actions()
        if changed and zone and sample_mode:self._sample_debounce.start()

    def change_zone(self,key,value):
        self.change_zone_values({key:value})

    def change_zone_values(self,values):
        if self.draft.mode=='wave':
            if self._loading:return
            allowed=set(vars(self.draft.wave_settings))
            if not values or not values.keys()<=allowed:return
            settings=replace(self.draft.wave_settings,**values)
            if 'low' in values:settings=replace(settings,high=max(settings.high,settings.low))
            if 'high' in values:settings=replace(settings,low=min(settings.low,settings.high))
            if 'velocity_low' in values:settings=replace(settings,velocity_high=max(settings.velocity_high,settings.velocity_low))
            if 'velocity_high' in values:settings=replace(settings,velocity_low=min(settings.velocity_low,settings.velocity_high))
            settings.validate()
            if settings==self.draft.wave_settings:return
            self.draft.wave_settings=settings;self._draft_revision+=1;self._invalidate_processed_wave();self._audition_debounce.start()
            self.select_zone();self._remember_edit();self.status.clear();return
        zone=self.zone()
        if self._loading or not zone:return
        if all(getattr(zone,key)==value for key,value in values.items()):return
        self._draft_revision+=1;self._audition_debounce.start()
        self._invalidate_processed_wave()
        audio_change=bool(values.keys()&{'start','end','speed','pitch','gain','reverse'})
        if audio_change:self.sample_stop();self._sample_media=None;self._sample_generation+=1
        for key,value in values.items():
            setattr(zone,key,bool(value) if key in ('reverse','key_tracking') else float(value) if key in ('speed','pitch','gain') else int(value))
        if 'low' in values:zone.high=max(zone.high,zone.low)
        elif 'high' in values:zone.low=min(zone.low,zone.high)
        elif 'velocity_low' in values:zone.velocity_high=max(zone.velocity_high,zone.velocity_low)
        elif 'velocity_high' in values:zone.velocity_low=min(zone.velocity_low,zone.velocity_high)
        # Keep trim and loop within a nonempty end-exclusive source interval.
        zone.start=min(zone.start,len(zone.pcm)-4)
        zone.end=max(zone.start+4,min(len(zone.pcm),zone.end))
        zone.loop_start=max(zone.start,min(zone.loop_start,zone.end-1))
        zone.loop_end=max(zone.loop_start+1,min(zone.loop_end,zone.end))
        if values.keys()&{'start','end','reverse'}:
            self._sample_seek=zone.end if zone.reverse else zone.start
            if self._sample_range and self._sample_range!=(zone.start,zone.end):self.clear_selection()
        self.dirty=True;self.select_zone();self.status.clear()
        if values.keys()&{'start','end','reverse'}:
            self._analysis=None;self._reset_analysis_results()
            self.apply_root_button.setEnabled(False);self.apply_tempo_button.setEnabled(False)
        if audio_change and self._sample_job and self._sample_job.operation=='analyze':
            self._sample_job.requestInterruption()
            self._reset_analysis_results()
        if audio_change:self._sample_debounce.start()
        self._remember_zone()

    def move_marker(self,key,value):self.change_zone(key,value)

    def _ensure_sample_player(self):
        if self._sample_player is not None:return
        record_support_event('sample_workspace',stage='player_create_begin',generation=self._sample_generation)
        from PySide6.QtMultimedia import QMediaPlayer,QAudioOutput
        self._sample_player=QMediaPlayer(self);self._sample_output=QAudioOutput(self);self._sample_output.setVolume(1.)
        self._sample_player.setAudioOutput(self._sample_output)
        self._sample_player.positionChanged.connect(self.sample_position_changed)
        self._sample_player.playbackStateChanged.connect(self.sample_playback_changed)
        self._sample_player.mediaStatusChanged.connect(self.sample_media_status_changed)
        self._sample_player.errorOccurred.connect(self._sample_player_error)
        record_support_event('sample_workspace',stage='player_create_end',generation=self._sample_generation)

    def _sample_failure(self,error):
        self._sample_debounce.stop();self._sample_pending=False;self._sample_media=None
        self.sample_stop();self.status.setText(error);self.sample_position.setText(tr('creator.sample_unavailable'))

    def _sample_player_error(self,error,message):
        if self._sample_closing:return
        record_support_event('sample_workspace',stage='decoder_error',generation=self._sample_generation,error=str(error),message=message)
        self._sample_failure(tr('creator.preview_failed'))

    def sample_repeat_changed(self,checked):
        if self._sample_player:
            from PySide6.QtMultimedia import QMediaPlayer
            self._sample_player.setLoops(QMediaPlayer.Infinite if checked else QMediaPlayer.Once)

    def sample_play(self,*,force=False):
        if self.draft.mode!='sample' or not self.zone():return
        if self._sample_autoplay and not force:
            self._sample_autoplay=False;self.sample_play_button.set_command('play',tr('creator.sample_play'));return
        from PySide6.QtMultimedia import QMediaPlayer
        record_support_event('sample_workspace',stage='play_request',generation=self._sample_generation,force=force)
        self.stop_preview();self._ensure_sample_player()
        if self._sample_player.playbackState()==QMediaPlayer.PlayingState and not force:
            self._sample_player.pause();return
        if self._sample_range:self.commit_sample_selection(*self._sample_range)
        if self._sample_media is None:
            self._sample_autoplay=True;self.sample_play_button.set_command('stop',tr('creator.cancel_sample_play'))
            self.sample_position.setText(tr('creator.preparing_sample'));self.prepare_sample();return
        self.sample_repeat_changed(self.audition_loop.isChecked())
        self._sample_player.play()

    def sample_stop(self):
        self._sample_autoplay=False
        self._detach_sample_clock()
        if self._sample_player:self._sample_player.stop()
        self._display_sample_position(0,force=True)
        if hasattr(self,'sample_play_button'):self.sample_play_button.set_command('play',tr('creator.sample_play'))

    def sample_playback_changed(self,*_):
        from PySide6.QtMultimedia import QMediaPlayer
        playing=self._sample_player and self._sample_player.playbackState()==QMediaPlayer.PlayingState
        self.sample_play_button.set_command('pause' if playing else 'play',tr('creator.sample_pause' if playing else 'creator.sample_play'))
        if self._sample_player:
            self._cursor_position=float(self._sample_player.position());self._cursor_anchor=time.perf_counter()
            if self._sample_player.mediaStatus()==QMediaPlayer.EndOfMedia:self.sample_media_status_changed(QMediaPlayer.EndOfMedia)
            elif not playing:self._display_sample_position(self._cursor_position,force=True)
        if playing:self._start_sample_clock()
        else:self._detach_sample_clock()

    def sample_position_changed(self,position):
        self._cursor_position=float(position);self._cursor_anchor=time.perf_counter()
        self._display_sample_position(float(position))

    def sample_media_status_changed(self,status):
        from PySide6.QtMultimedia import QMediaPlayer
        if status==QMediaPlayer.EndOfMedia and self._sample_media and self.zone():
            _,_,start,end=self._sample_media
            self._cursor_position=(end-start)*1000/self.zone().rate
            self._display_sample_position(self._cursor_position,force=True)

    def _display_sample_position(self,position,*,force=False):
        if not self._sample_media or not self.zone():return
        zone=self.zone();start,end=self._sample_media[2:];count=len(self._sample_media[1])
        position=max(0,min((end-start)*1000/zone.rate,position))
        frame=max(start,min(end,start+round(position*zone.rate/1000)))
        source=frame/count*len(zone.pcm)
        self.sample_canvas.set_cursor(round(len(zone.pcm)-source if zone.reverse else source))
        now=time.perf_counter()
        if force or now-self._position_caption_at>=.05:
            self._position_caption_at=now
            self.sample_position.setText(tr('creator.sample_position',position=position/1000,duration=(end-start)/zone.rate))

    def _start_sample_clock(self):
        handle=self.window().windowHandle()
        if handle is None:return
        if self._cursor_handle is not handle:
            self._detach_sample_clock();self._cursor_handle=handle;handle.installEventFilter(self)
        self._request_sample_frame()

    def _request_sample_frame(self):
        if not self._cursor_handle:return
        if request_visual_frame(self._cursor_handle,self,self._advance_sample_cursor):
            self._cursor_precise=False;self._cursor_timer.stop()
        else:
            # QWindow's platform requestUpdate fallback can be coarse on this
            # Windows/GDI path. One bounded timer only owns the playing cursor;
            # it never schedules audio or implies a physical frame deadline.
            self._cursor_precise=True
            rate=self.screen().refreshRate() if self.screen() else 60
            if not math.isfinite(rate) or rate<=0:rate=60
            if not self._cursor_timer.isActive():self._cursor_timer.start(max(1,round(1000/rate)))

    def _detach_sample_clock(self):
        self._cursor_timer.stop();self._cursor_precise=False
        if self._cursor_handle:
            remove_visual_frame(self._cursor_handle,self)
            self._cursor_handle.removeEventFilter(self);self._cursor_handle=None

    def eventFilter(self,watched,event):
        if (not self._cursor_precise and watched is self._cursor_handle and event.type()==QEvent.UpdateRequest
                and not compositor_visual_clock(watched,self)):self._advance_sample_cursor()
        return super().eventFilter(watched,event)

    def _advance_sample_cursor(self):
        from PySide6.QtMultimedia import QMediaPlayer
        if not self._sample_player or self._sample_player.playbackState()!=QMediaPlayer.PlayingState:
            self._detach_sample_clock();return
        now=time.perf_counter();position=self._cursor_position
        if (self._cursor_anchor is not None and self._sample_player.mediaStatus() not in
                (QMediaPlayer.StalledMedia,QMediaPlayer.LoadingMedia,QMediaPlayer.InvalidMedia)):
            position+=min(.25,max(0,now-self._cursor_anchor))*1000
        # Audio ends/loops at its prepared PCM boundary. This clock only paints;
        # it must not schedule pause/seek against an already-buffered decoder.
        self._display_sample_position(position)
        if self._cursor_handle:self._request_sample_frame()

    def sample_seek(self,source):
        self._sample_seek=source
        if self._sample_player and self._sample_media and self.zone():
            zone=self.zone();_,pcm,start,end=self._sample_media
            fraction=source/len(zone.pcm);fraction=1-fraction if zone.reverse else fraction
            self._sample_player.setPosition(round(max(0,min(end-start,fraction*len(pcm)-start))*1000/zone.rate))
            self._cursor_position=float(self._sample_player.position());self._cursor_anchor=time.perf_counter()

    def sample_selection(self,first,last):
        if first<0 or last-first<4:
            self._sample_range=None
            for button in self.selection_actions:button.setEnabled(False)
            zone=self.zone()
            self.selection_label.setText(tr('creator.selection_info',start=zone.start/zone.rate/zone.speed,
                end=(zone.end or len(zone.pcm))/zone.rate/zone.speed,
                duration=((zone.end or len(zone.pcm))-zone.start)/zone.rate/zone.speed) if zone else '')
            self.sample_repeat_changed(self.audition_loop.isChecked());return
        zone=self.zone();self._sample_range=(first,last)
        for button in self.selection_actions:button.setEnabled(last-first>=4)
        duration=len(zone.pcm)/zone.rate/zone.speed
        start,end=sorted(self.sample_canvas._display_fraction(value)*duration for value in (first,last))
        self.selection_label.setText(tr('creator.selection_info',start=start,end=end,duration=end-start))

    def clear_selection(self):
        self._sample_range=None;self.sample_canvas.selection=None;self.sample_canvas.update()
        self.sample_selection(-1,-1)

    def commit_sample_selection(self,first,last):
        zone=self.zone()
        if not zone:return
        first,last=sorted((max(0,min(len(zone.pcm),int(first))),max(0,min(len(zone.pcm),int(last)))))
        if last-first<4:return
        if (zone.start,zone.end)==(first,last):return
        self.sample_canvas.selection=(first,last);self.sample_selection(first,last)
        self.change_zone_values({'start':first,'end':last})
        self._sample_seek=last if zone.reverse else first

    def restore_sample_bounds(self):
        if not self.zone():return
        self.change_zone_values({'start':0,'end':len(self.zone().pcm)});self.clear_selection();self.sample_canvas.fit()

    def single_key(self):
        if self.zone():self.change_zone_values({'low':self.zone().root,'high':self.zone().root})

    def all_keys(self):
        if self.zone():self.change_zone_values({'low':0,'high':127})

    def auto_ranges(self):
        if self.draft.mode!='sample':return
        if not self.draft.zones:return
        roots=sorted(set(zone.root for zone in self.draft.zones))
        for zone in self.draft.zones:
            at=roots.index(zone.root);zone.low=0 if at==0 else (roots[at-1]+zone.root)//2+1
            zone.high=127 if at+1==len(roots) else (zone.root+roots[at+1])//2
        self.dirty=True;self.select_zone();self.status.clear()
        self._draft_revision+=1;self._invalidate_processed_wave();self._audition_debounce.start()
        self._remember_zone()

    def _remember_zone(self):
        self._remember_edit()

    def sample_history(self,delta):
        self.history(delta)

    def snap_zero(self):
        zone=self.zone()
        if not zone:return
        signal=zone.pcm[:,0]
        changes={}
        for key in ('start','end'):
            target=getattr(zone,key) or (len(signal) if key=='end' else 0)
            first=max(1,target-round(zone.rate*.005));last=min(len(signal)-1,target+round(zone.rate*.005))
            crossings=np.flatnonzero(np.signbit(signal[first-1:last-1])!=np.signbit(signal[first:last]))+first
            if len(crossings):changes[key]=int(crossings[np.argmin(abs(crossings-target))])
        if changes:self.change_zone_values(changes)

    def audition_note_on(self,note,velocity):
        self.instrument_phrase_button._tuning_audition.stop()
        if not self._source_ready(report=True):return
        self.preview_note=int(note)
        self.audition_keyboard.drum=self.draft.bank==128
        self.audition_keyboard.active_note=int(note);self.audition_keyboard.center_on(int(note))
        from effect_presentation import drum_key_caption
        caption=drum_key_caption(note) if self.draft.bank==128 else note_caption(int(note))
        self.audition_caption.setText(tr('creator.audition_velocity',note=caption,velocity=velocity))
        self._held_notes[note]=velocity
        if self._audition_source and not self._audition_source.closed and self._audition_revision==self._draft_revision:
            self._audition_source.note_on(note,velocity);return
        self.preview()

    def audition_note_off(self,note):
        self._held_notes.pop(note,None)
        if self._audition_source:self._audition_source.note_off(note)

    def audition_note(self,note):
        # Legacy control/test entry: selecting a note never imposes duration.
        self.preview_note=int(note);self.audition_keyboard.active_note=int(note);self.audition_keyboard.center_on(int(note))
        self.audition_caption.setText(tr('creator.audition_note',note=self.audition_keyboard.key_caption(int(note))))
        if self.worker:
            if self.worker.operation=='preview':self._pending_audition=int(note)
            return

    def normalize_sample(self):
        if self.draft.mode!='sample' or not self.zone() or self._sample_job:return
        self._sample_debounce.stop();self._start_sample_job('normalize',self.zone())

    def reset_sample_transform(self):
        values={'pitch':0.,'gain':0.,'reverse':False}
        if self.draft.mode=='sample':values['speed']=1.
        self.change_zone_values(values)

    def prepare_sample(self):
        if self._sample_closing or self.draft.mode!='sample' or not self.zone():return
        # An explicit Play consumes the scheduled debounce. Rebuilding an
        # already accepted generation resets QMediaPlayer to Stopped and can
        # rewrite a file still borrowed by its asynchronous decoder.
        self._sample_debounce.stop()
        if self._sample_media is not None:return
        if self._sample_job:
            if (self._sample_job.operation=='prepare' and self._sample_job.generation==self._sample_generation
                    and not self._sample_job.isInterruptionRequested()):return
            self._sample_pending=True;self._sample_job.requestInterruption();return
        self._sample_pending=False
        zone=replace(self.zone())
        self._sample_serial+=1
        path=Path(self._temporary.name)/f'sample-{self._sample_generation}-{self._sample_serial}.wav'
        self._start_sample_job('prepare',zone,path)

    def _start_sample_job(self,operation,zone,path=None):
        if self._sample_job:return
        job=SampleWorkspaceJob(self,self._sample_generation,operation,zone,path);self._sample_job=job
        self.analysis_button.setEnabled(False)
        if operation=='prepare':self.sample_position.setText(tr('creator.preparing_sample'))
        record_support_event('sample_workspace',stage='job_start',generation=self._sample_generation,operation=operation,
            rate=zone.rate,frames=len(zone.pcm),start=zone.start,end=zone.end)
        job.completed.connect(self._sample_result);job.finished.connect(self._sample_finished);job.start()

    def _sample_result(self,generation,operation,value,error):
        if generation!=self._sample_generation or self._sample_closing or self.draft.mode!='sample':return
        worker=self.sender()
        if worker is not None and (worker is not self._sample_job or worker.isInterruptionRequested()):return
        if error:
            if operation=='prepare':self._sample_failure(error)
            else:self.status.setText(error)
            return
        if operation=='analyze':
            self._analysis=value
            pitch=value['pitch'];bpm=value['bpm']
            self.pitch_result.setText(tr('creator.pitch_found',note=note_caption(pitch['note']),hz=pitch['hz'],cents=pitch['cents']) if pitch
                else tr('creator.pitch_unavailable',reason=tr('creator.analysis_reason_'+value['pitch_reason'])))
            self.bpm_result.setText(tr('creator.bpm_found',bpm=bpm['value'],beats=bpm['beats']) if bpm
                else tr('creator.bpm_unavailable',reason=tr('creator.analysis_reason_'+value['bpm_reason'])))
            self.apply_root_button.setEnabled(bool(pitch));self.apply_tempo_button.setEnabled(bool(bpm))
        elif operation=='normalize':self.change_zone('gain',value)
        else:
            try:
                self._sample_media=value;self.sample_canvas.set_processed(value[1]);self._ensure_sample_player()
                record_support_event('sample_workspace',stage='decoder_source_begin',generation=generation)
                self._sample_player.setSource(QUrl.fromLocalFile(value[0]))
                record_support_event('sample_workspace',stage='decoder_source_end',generation=generation)
                if self._sample_media is not value:return # Synchronous error already retired it.
                self.sample_position_changed(0)
                if self._sample_seek is not None:self.sample_seek(self._sample_seek)
                if self._sample_autoplay:self._sample_autoplay=False;self.sample_play(force=True)
            except Exception as exc:
                record_support_event('sample_workspace',stage='publication_error',generation=generation,error=str(exc))
                self._sample_failure(tr('creator.preview_failed'))

    def _sample_finished(self):
        worker=self.sender()
        if worker is not None and worker is not self._sample_job:return
        worker=self._sample_job;self._sample_job=None
        if worker:worker.deleteLater()
        # Only owned temporary media; the current decoder may still hold its
        # previous file, so a failed unlink is retried after the next job.
        current=self._sample_media[0] if self._sample_media else None
        for path in Path(self._temporary.name).glob('sample-*.wav'):
            if str(path)!=current:
                try:path.unlink()
                except OSError:pass
        self.analysis_button.setEnabled(self.draft.mode=='sample' and self.zone() is not None)
        pending,self._sample_pending=self._sample_pending,False
        if pending and self._sample_media is None and not self._sample_closing:
            self._sample_debounce.start(0)

    def _reset_analysis_results(self):
        self.pitch_result.setText(tr('creator.pitch_empty'));self.bpm_result.setText(tr('creator.bpm_empty'))

    def analyze_current_sample(self):
        if self.draft.mode!='sample' or self._sample_job or not self.zone():return
        self._sample_debounce.stop()
        self.analysis_button.setEnabled(False);self.pitch_result.setText(tr('creator.analyzing'));self.bpm_result.setText(tr('creator.analyzing'))
        self._start_sample_job('analyze',self.zone())

    def apply_analyzed_root(self):
        if self._analysis and self._analysis.get('pitch'):self.change_zone('root',self._analysis['pitch']['note'])

    def apply_analyzed_tempo(self):
        if self._analysis and self._analysis.get('bpm'):
            speed=self.target_bpm.value()/self._analysis['bpm']['value']
            if .25<=speed<=4:self.change_zone('speed',speed)
            else:self.status.setText(tr('creator.invalid_transform'))

    def refresh_zones(self):
        self._draft_revision+=1
        self._invalidate_processed_wave()
        self._loading=True;self.sample_list.blockSignals(True);self.sample_list.clear()
        for index,zone in enumerate(self.draft.zones):self.sample_list.addItem(tr('creator.zone_item',index=index+1,name=zone.name))
        self.sample_list.blockSignals(False);self._loading=False;self.select_zone()
        if not self._source_ready(report=True):self.stop_preview()
        else:self._audition_debounce.start() # Reopened/imported sources also refresh their processed reference.

    def remove_zone(self):
        if self.draft.mode!='sample':return
        if self.zone():self.draft.zones.pop(self.sample_list.currentIndex());self.refresh_zones();self._remember_edit()

    def choose_samples(self):
        if self.worker:return
        paths,_=QFileDialog.getOpenFileNames(self,tr('creator.import'),'',tr('creator.audio_filter'))
        if paths:
            if len(paths)+len(self.draft.zones)>MAX_ZONES:
                NativeMessageBox.warning(self,tr('creator.title'),tr('creator.zone_limit',maximum=MAX_ZONES));return
            self.start_job('import',paths)

    def dragEnterEvent(self,event):
        if event.mimeData().hasUrls() and all(url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in
                ('.wav','.aif','.aiff','.flac','.ogg','.mp3') for url in event.mimeData().urls()):event.acceptProposedAction()
        else:event.ignore()

    def dropEvent(self,event):
        paths=[url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if self.worker or not paths:return event.ignore()
        if len(paths)+len(self.draft.zones)>MAX_ZONES:
            self.status.setText(tr('creator.zone_limit',maximum=MAX_ZONES));return event.ignore()
        self.start_job('import',paths);event.acceptProposedAction()

    def confirm_replace(self):
        if not self.dirty:return True
        box=NativeMessageBox(self);box.setWindowTitle(tr('creator.title'));box.setText(tr('creator.replace_draft'))
        discard=box.addButton(tr('creator.replace'),QMessageBox.DestructiveRole)
        cancel=box.addButton(tr('common.cancel'),QMessageBox.RejectRole)
        box.setDefaultButton(cancel);box.setEscapeButton(cancel);box.exec()
        return box.clickedButton() is discard

    def new_draft(self):
        if (self.worker and self.worker.operation!='preview') or not self.confirm_replace():return
        if self.worker:self.worker.requestInterruption()
        draft=InstrumentDraft(name=tr('creator.default_name'),mode='sample');self._set_unused_address(draft)
        self._fresh_signature=self._edit_signature(draft)
        self._entry_id=None;self.load_draft(draft);self._path=None;self.refresh_saved()

    def choose_open(self):
        if self.worker and self.worker.operation!='preview':return
        entry=next((entry for entry in self.group_dialog.group.fonts if entry.filename==self.saved_list.currentData()),None)
        if entry is not None and entry.custom is not None and self.confirm_replace():
            self._opening_id=entry.filename
            self.start_job('edit',(entry.custom,self.group_dialog.group.source_path))

    def showEvent(self,event):
        self._audition_debounce.start()
        if self._entry_id and not any(entry.filename==self._entry_id for entry in self.group_dialog.group.fonts):self._entry_id=None
        self.refresh_saved();self._populate_programs(self.draft.bank);self.refresh_address()
        super().showEvent(event)
        QTimer.singleShot(0,self,self._fit_workspace)

    def hideEvent(self,event):
        self._workspace_motion.settle()
        self._finish_workspace_transition()
        self.sample_stop();self.stop_preview();self._wave_debounce.stop()
        if self._wave_job:self._wave_job.requestInterruption()
        super().hideEvent(event)

    def load_draft(self,draft,*,reset_history=True):
        self._entry_epoch+=1 # Retire entries before reused fields display another draft/undo target.
        self._pending_audition=None
        self.stop_preview();self._draft_revision+=1
        self.processed_wave.set_message(tr('creator.processed_wave_empty'),reset_view=reset_history)
        if reset_history:self._wave_display_source=None
        self._invalidate_processed_wave()
        self.sample_stop();self._sample_generation+=1;self.draft=draft;self._loading=True
        try:
            self.name.setText(draft.name)
            if reset_history:self._melodic_bank=draft.bank if draft.bank<128 else 0
            self._populate_programs(draft.bank);self.program.setValue(draft.program)
            self._read_source_choice()
            for key,control in self.controls.items():
                if key in ('attack','decay','release'):control.set_maximum(max(5,getattr(draft,key)))
                control.set(getattr(draft,key),False)
            self.wave_canvas.set_wave(draft.wave)
        finally:self._loading=False
        self.refresh_zones()
        if reset_history:self._reset_history()
        self.status.clear();self._source_ready(report=True);self.refresh_address()
        self.group_dialog._sync_save_actions()

    def _source_ready(self,*,report=False):
        # An empty sample editor is valid workspace state, not a serializable
        # instrument. Do not send passive warm-up through strict model validation.
        ready=self.draft.mode!='sample' or bool(self.draft.zones)
        if not ready and report:self.status.clear()
        return ready

    def _sync_source_actions(self):
        """Present the same source/job prerequisites that the commands use."""
        ready = self._source_ready()
        if not ready:
            self.processed_wave.set_message(tr('creator.processed_wave_empty'))
        blocking = self.worker is not None and self.worker.operation != 'preview'
        signature=self._edit_signature(self.draft)
        name=self.draft.name
        valid_name=bool(name.strip()) and not any(c in name for c in '\r\n\x00')
        # PCM is validated on decode and in the serialized apply job,never
        # scanned again just to update a command's enabled appearance.
        self.save_button.setEnabled(ready and valid_name and not blocking and
            (self._entry_id is None or signature!=self._clean_signature))
        self.save_button.setText(tr('creator.update' if self._entry_id else 'creator.add'))
        self.new_button.setEnabled(not blocking and (self._entry_id is not None or signature!=self._fresh_signature))
        self.audition_keyboard.setEnabled(ready and not blocking)
        source=self._audition_source
        self.instrument_phrase_button.setEnabled(ready and not blocking and source is not None and not source.closed
            and self._audition_revision==self._draft_revision)

    def _draft_phrase_request(self):
        source=self._audition_source
        if source is None or source.closed or self._audition_revision!=self._draft_revision:return None
        from midi_player import instrument_audition_phrase
        phrase,_family=instrument_audition_phrase(self.draft.bank,self.draft.program,drum=self.draft.bank==128)
        return dict(note=69,identity=('draft',self._draft_revision),prepared_source=source,phrase=phrase,period=32*60/116)

    def choose_save(self):
        if self.worker and self.worker.operation!='preview':return False
        self._sync_source_actions()
        if not self.save_button.isEnabled():return False
        if not self._source_ready(report=True):return False
        try:self.draft.validate()
        except ValueError as exc:NativeMessageBox.warning(self,tr('creator.title'),str(exc));return False
        current=next((row for row in getattr(self.group_dialog,'_instrument_rows',()) if
            (row['bank'],row['preset'])==(self.draft.bank,self.draft.program)),None)
        own=next((i for i,entry in enumerate(self.group_dialog.group.fonts) if entry.filename==self._entry_id),None)
        if current and current['source_index']!=own:
            box=NativeMessageBox(self);box.setWindowTitle(tr('creator.title'))
            box.setText(tr('creator.confirm_address',bank=self.draft.bank,program=self.draft.program,current=current['name']))
            replace=box.addButton(tr('creator.replace_address'),QMessageBox.AcceptRole)
            cancel=box.addButton(tr('common.cancel'),QMessageBox.RejectRole)
            box.setDefaultButton(cancel);box.setEscapeButton(cancel);box.exec()
            if box.clickedButton() is not replace:return False
        self.start_job('apply',(self.draft.snapshot(),self.group_dialog.group.source_path))
        return True

    def preview(self):
        if self.worker:return
        if not self._source_ready(report=True):return
        self.sample_stop()
        try:self.draft.validate()
        except ValueError as exc:NativeMessageBox.warning(self,tr('creator.title'),str(exc));return
        if not getattr(self.group_dialog.owner,'audio_session',None) or not self.group_dialog.owner.audio_session.stream:
            self.status.setText(tr('creator.engine_required'));return
        self.instrument_phrase_button._tuning_audition.stop()
        if self._audition_source:self._audition_source.close();self._audition_source=None
        self._preview_generation+=1
        path=Path(self._temporary.name)/f'preview-{self._preview_generation}.sf2'
        self._wave_path=str(path)
        self.start_job('preview',(self.draft.snapshot(),str(path),self._draft_revision))

    def _prime_audition(self):
        if not self._source_ready():return
        session=getattr(self.group_dialog.owner,'audio_session',None)
        if not self.isVisible() or not session or not session.stream:return
        if self.worker:self._audition_debounce.start();return
        if self._sample_job or self._sample_debounce.isActive() or self._sample_autoplay:self._audition_debounce.start();return
        if self._held_notes:return
        if self._sample_player:
            from PySide6.QtMultimedia import QMediaPlayer
            if self._sample_player.playbackState()==QMediaPlayer.PlayingState:return
        if self._audition_source and not self._audition_source.closed and self._audition_revision==self._draft_revision:
            self._queue_processed_wave();return
        self.preview()

    def start_job(self,operation,payload):
        if self.worker:
            if operation!='preview' and self.worker.operation=='preview':
                self._pending_operation=(operation,payload);self.worker.requestInterruption()
            return
        self._job_failed=False
        if operation!='preview':
            self.sample_stop()
            if self._sample_job:self._sample_job.requestInterruption()
        if operation!='preview':self.editor.setEnabled(False)
        if operation!='preview':self.audition_keyboard.setEnabled(False)
        for button in (self.new_button,self.open_button,self.delete_button,self.save_button):button.setEnabled(operation=='preview')
        self._sync_source_actions()
        self.status.setText(tr('creator.busy_'+operation))
        worker=InstrumentAuthoringJob(self,operation,payload);self.worker=worker
        worker.completed.connect(self.job_result);worker.finished.connect(self.job_finished);worker.start()

    def job_result(self,operation,value,error):
        if error:
            if (operation=='preview' and self.worker is not None and
                    (self.worker.isInterruptionRequested() or self.worker.payload[2]!=self._draft_revision)):
                return  # Undo/source changes intentionally cancelled this revision.
            self._job_failed=True;self.status.setText(error);return
        try:
            if operation=='import':
                self._history_batch=True
                try:self.draft.zones.extend(value);self.change('mode','sample');self._read_source_choice();self.refresh_zones()
                finally:self._history_batch=False
                self._remember_edit()
                self.dirty=True;self.status.setText(tr('creator.imported',count=len(value)))
                if len(self.draft.zones)>1:
                    self.status.setText(tr('creator.layers_hint'))
            elif operation=='edit':
                self._entry_id=self._opening_id;self.load_draft(value);self.refresh_saved()
            elif operation=='apply':
                record,runtime_path=value
                entry_id=self.group_dialog.add_created_instrument(record,runtime_path,self._entry_id)
                if entry_id:
                    self._entry_id=entry_id;self.dirty=False
                    self._clean_signature=self._edit_signature(self.draft);self.group_dialog._sync_save_actions()
                    self.status.setText(tr('creator.applied_group'))
                    self.refresh_saved();self._populate_programs(self.draft.bank);self.refresh_address()
                else:self._job_failed=True;self.status.setText(tr('creator.apply_failed'))
            elif operation=='preview':
                source,revision=value
                if self.worker:self.worker.prepared_source=None
                if revision!=self._draft_revision or self._sample_closing:source.close();return
                self.group_dialog.owner.audio_session.install_instrument_audition(source)
                self._audition_source=source;self._audition_revision=revision
                for note,velocity in self._held_notes.items():source.note_on(note,velocity)
                if self.worker:self._wave_rate=self.worker.sample_rate
                self._wave_revision=revision;self._queue_processed_wave()
                self.status.clear()
        except Exception as exc:self._job_failed=True;self.status.setText(str(exc))

    def job_finished(self):
        worker=self.worker;self.worker=None
        self.editor.setEnabled(True)
        self.audition_keyboard.setEnabled(True)
        for button in (self.new_button,self.open_button,self.delete_button,self.save_button):button.setEnabled(True)
        self._sync_source_actions()
        self.refresh_saved()
        self._history_buttons()
        if worker:worker.deleteLater()
        if self._pending_operation and not self._sample_closing:
            operation,payload=self._pending_operation;self._pending_operation=None
            self.start_job(operation,payload);return
        if self._pending_audition is not None:
            note,self._pending_audition=self._pending_audition,None
            QTimer.singleShot(0,self,lambda:self.audition_note(note))
        after,self._after_save=self._after_save,None
        if after and not self.dirty and not self._job_failed:QTimer.singleShot(0,self,after)

    def stop_preview(self):
        if hasattr(self,'instrument_phrase_button'):self.instrument_phrase_button._tuning_audition.stop()
        if hasattr(self,'_audition_debounce'):self._audition_debounce.stop()
        self._held_notes.clear()
        if hasattr(self,'audition_keyboard'):self.audition_keyboard.release_notes()
        if self._audition_source:self._audition_source.silence()

    def _invalidate_processed_wave(self):
        self._wave_revision=-1;self._wave_key=None;self._wave_debounce.stop()
        if self._wave_job:self._wave_job.requestInterruption()
        if hasattr(self,'processed_wave'):self.processed_wave.set_pending()

    def _processed_wave_render_key(self):
        # A stable reference belongs to the instrument,not to piano gestures.
        settings=self.draft.zones[0] if self.draft.mode=='sample' and self.draft.zones else self.draft.wave_settings
        note=min(settings.high,max(settings.low,settings.root if self.draft.mode=='sample' else 69))
        velocity=min(settings.velocity_high,max(settings.velocity_low,AUDITION_NOTE_VELOCITY))
        return (self._draft_revision,int(note),velocity,self.preview_duration.value())

    def _queue_processed_wave(self):
        if self._sample_closing:return
        key=self._processed_wave_render_key()
        if key==self._wave_key:return
        if self._wave_job and self._wave_job.key!=key:self._wave_job.requestInterruption()
        self.processed_wave.set_pending();self._wave_debounce.start()

    def _prepare_processed_wave(self):
        if self._sample_closing or not self.isVisible() or not self._source_ready():return
        if self._wave_revision!=self._draft_revision or not self._wave_path:return
        if self._wave_job:self._wave_debounce.start();return
        key=self._processed_wave_render_key()
        if key==self._wave_key:return
        worker=InstrumentWaveJob(self,key,self._wave_path,self.draft.snapshot(),self._wave_rate)
        self._wave_job=worker;worker.completed.connect(self._processed_wave_result)
        worker.finished.connect(self._processed_wave_finished);worker.start()

    def _processed_wave_result(self,key,pcm,error):
        if self._sample_closing or key!=self._processed_wave_render_key():return
        if self._wave_revision!=self._draft_revision:return
        if error:
            self.processed_wave.set_message(error);self.processed_wave.setAccessibleDescription(error);return
        pcm,peak=pcm
        identity=(self.draft.mode,tuple(id(zone.pcm) for zone in self.draft.zones) if self.draft.mode=='sample' else None)
        if identity!=self._wave_display_source:
            self.processed_wave.reset_amplitude();self._wave_display_source=identity
        self._wave_key=key;self.processed_wave.set_pcm(pcm,self._wave_rate,peak=peak)
        self.processed_wave.setAccessibleDescription('')

    def _processed_wave_finished(self):
        worker=self._wave_job;self._wave_job=None
        if worker:worker.deleteLater()
        if not self._sample_closing and worker and worker.isInterruptionRequested():self._wave_debounce.start()

    def can_close(self, after=None):
        if self.worker:
            self.status.setText(tr('creator.wait_job'));return False
        if self.dirty:
            box=NativeMessageBox(self);box.setWindowTitle(tr('creator.title'));box.setText(tr('creator.close_draft'));box.setIcon(QMessageBox.Question)
            save=box.addButton(self.save_button.text(),QMessageBox.AcceptRole)
            discard=box.addButton(tr('creator.discard'),QMessageBox.ActionRole)
            cancel=box.addButton(tr('common.cancel'),QMessageBox.RejectRole)
            box.setDefaultButton(save);box.setEscapeButton(cancel);box.exec()
            if box.clickedButton() is save:
                if self.choose_save():self._after_save=after
                return False
            if box.clickedButton() is not discard:return False
            self.dirty=False
        return True

    def shutdown(self):
        self._workspace_motion.settle()
        self._finish_workspace_transition()
        self._sample_closing=True;self._sample_debounce.stop();self._audition_debounce.stop();self.sample_stop()
        self._wave_debounce.stop()
        if self._wave_job:self._wave_job.requestInterruption();self._wave_job.wait()
        if self._sample_player:self._sample_player.setSource(QUrl())
        if self._sample_job:self._sample_job.requestInterruption();self._sample_job.wait()
        self.stop_preview()
        if self.worker:
            self.worker.requestInterruption();self.worker.wait()
            pending=getattr(self.worker,'prepared_source',None)
            if pending:pending.close();self.worker.prepared_source=None
        if self._audition_source:self._audition_source.close();self._audition_source=None
