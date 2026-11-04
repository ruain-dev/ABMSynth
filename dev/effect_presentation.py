"""Shared effect labels, units, and tooltip identifiers."""

from __future__ import annotations

import math
import cmath
from dataclasses import dataclass

from i18n import tr


# Configuration views, never audio analyzers. These models have no BASS handles.
FX_VISUAL_FAMILIES = {
    **dict.fromkeys(('parametric_eq','low_pass','high_pass','low_shelf','high_shelf','band_pass','notch'),'filter'),
    'compressor_fx':'compressor','gate':'gate','saturator':'saturation',
    'distortion':'xy','distortion_dx8':'xy','bitcrusher':'crusher',
    'echo':'echo','echo_dx8':'echo','tremolo':'modulation','auto_pan':'modulation',
    'volume_envelope':'envelope',
}


@dataclass(frozen=True)
class FXVisualHandle:
    """Normalized drawing position plus an editable parameter on either axis."""
    x: float
    y: float
    xkey: str = ''
    ykey: str = ''


@dataclass(frozen=True)
class FXVisualPlot:
    family: str
    title: str
    note: str
    xticks: tuple
    yticks: tuple
    curves: tuple
    handles: tuple
    xdomain: tuple
    ydomain: tuple
    logarithmic: bool = False
    bars: bool = False


def fx_filter_coefficients(kind, values, sample_rate):
    """RBJ BQF equations; same BW/Q/shelf choices as audio_fx_native_data.

    BASS_BFX_BQF uses digital-octave bandwidth (priority over Q). Local
    bass_fx.md defines domains; the PCM audit checks these equations separately.
    """
    frequency=min(values['frequency'],sample_rate*.49)
    w=2*math.pi*frequency/sample_rate;c=math.cos(w);s=math.sin(w)
    a=10**(values.get('gain',0)/40)
    if kind in ('low_shelf','high_shelf'):
        alpha=s/2*math.sqrt((a+1/a)*(1/values['slope']-1)+2)
    elif 'bandwidth' in values:
        alpha=s*math.sinh(min(350,math.log(2)/2*values['bandwidth']*w/max(s,1e-12)))
    else:alpha=s/(2*values['q'])
    if kind=='low_pass':b=((1-c)/2,1-c,(1-c)/2);d=(1+alpha,-2*c,1-alpha)
    elif kind=='high_pass':b=((1+c)/2,-(1+c),(1+c)/2);d=(1+alpha,-2*c,1-alpha)
    elif kind=='band_pass':b=(alpha,0,-alpha);d=(1+alpha,-2*c,1-alpha)
    elif kind=='notch':b=(1,-2*c,1);d=(1+alpha,-2*c,1-alpha)
    elif kind=='parametric_eq':b=(1+alpha*a,-2*c,1-alpha*a);d=(1+alpha/a,-2*c,1-alpha/a)
    else:
        root=2*math.sqrt(a)*alpha
        if kind=='low_shelf':
            b=(a*((a+1)-(a-1)*c+root),2*a*((a-1)-(a+1)*c),a*((a+1)-(a-1)*c-root))
            d=((a+1)+(a-1)*c+root,-2*((a-1)+(a+1)*c),(a+1)+(a-1)*c-root)
        else:
            b=(a*((a+1)+(a-1)*c+root),-2*a*((a-1)+(a+1)*c),a*((a+1)+(a-1)*c-root))
            d=((a+1)-(a-1)*c+root,2*((a-1)-(a+1)*c),(a+1)-(a-1)*c-root)
    return tuple(v/d[0] for v in b),tuple(v/d[0] for v in d)


def fx_filter_db(kind,values,sample_rate,frequency):
    b,a=fx_filter_coefficients(kind,values,sample_rate)
    return _fx_filter_db_coefficients(b,a,sample_rate,frequency)


def _fx_filter_db_coefficients(b,a,sample_rate,frequency):
    z=cmath.exp(-2j*math.pi*frequency/sample_rate)
    return 20*math.log10(max(1e-12,abs((b[0]+b[1]*z+b[2]*z*z)/(a[0]+a[1]*z+a[2]*z*z))))


def audio_fx_visual_plot(kind, values, sample_rate, *, envelope_span=None):
    """Bounded static geometry from accepted values. No side effects or timers."""
    family=FX_VISUAL_FAMILIES[kind];v=values;rate=max(1,float(sample_rate))
    points=257;u=tuple(i/(points-1) for i in range(points))
    handles=[];curves=[];xd=(0.,1.);yd=(-1.,1.);log=False;bars=False
    title=tr('audio_fx.visual.'+family);note=tr('audio_fx.visual.settings')
    xt=((0,'0'),(1,'1'));yt=((0,'−1'),(.5,'0'),(1,'1'))
    def norm(value,domain):return (value-domain[0])/(domain[1]-domain[0])
    if family=='filter':
        xd=(20.,min(20000.,rate*.49));yd=(-36.,24.);log=True
        def nx(f):return math.log(max(xd[0],f)/xd[0])/math.log(xd[1]/xd[0])
        b,a=fx_filter_coefficients(kind,v,rate)
        curve=tuple((x,norm(_fx_filter_db_coefficients(b,a,rate,xd[0]*(xd[1]/xd[0])**x),yd)) for x in u)
        curves=[curve];xt=tuple((nx(f),f'{f/1000:g}k' if f>=1000 else f'{f:g}') for f in (20,100,1000,10000) if f<=xd[1])
        yt=tuple((norm(db,yd),f'{db:+g}') for db in (-36,-24,-12,0,12,24))
        gainkey='gain' if 'gain' in v else ''
        center=v['frequency']
        height=max(yd[0],min(yd[1],_fx_filter_db_coefficients(b,a,rate,min(center,rate*.49))))
        handles=[FXVisualHandle(nx(center),norm(height,yd),'frequency',gainkey)]
        note=tr('audio_fx.visual.frequency_note')
    elif family=='compressor':
        xd=(-60.,0.);yd=(-84.,24.)
        def output(db):return (db if db<=v['threshold'] else v['threshold']+(db-v['threshold'])/v['ratio'])+v['output']
        curves=[tuple((x,norm(output(-60+60*x),yd)) for x in u)]
        handles=[FXVisualHandle(norm(v['threshold'],xd),norm(v['threshold']+v['output'],yd),'threshold')]
        if v['threshold']<0:handles.append(FXVisualHandle(1,norm(output(0),yd),'','ratio'))
        xt=tuple((norm(d,xd),str(d)) for d in (-60,-40,-20,0));yt=tuple((norm(d,yd),str(d)) for d in (-80,-60,-40,-20,0,24))
        note=tr('audio_fx.visual.compressor_note')
    elif family=='gate':
        xd=(-108.,0.);yd=(0.,1.)
        close=v['threshold']-v['hysteresis'];op=v['threshold']
        curves=[((0,0),(norm(op,xd),0),(norm(op,xd),1),(1,1)),
                ((0,0),(norm(close,xd),0),(norm(close,xd),1),(1,1))]
        handles=[FXVisualHandle(norm(op,xd),.75,'threshold'),FXVisualHandle(norm(close,xd),.25,'hysteresis')]
        xt=tuple((norm(d,xd),str(d)) for d in (-90,-60,-30,0));yt=((0,tr('audio_fx.visual.closed')),(1,tr('audio_fx.visual.open')))
        note=tr('audio_fx.visual.gate_note')
    elif family=='saturation':
        xd=(-1.,1.);gain=10**(v['drive']/20);out=10**(v['output']/20);mix=v['wet']/100
        bound=max(1.5,math.ceil(max(1,(1-mix+math.tanh(gain)*mix)*out)*1.15*2)/2)
        yd=(-bound,bound)
        curves=[tuple((x,norm(((2*x-1)*(1-mix)+math.tanh((2*x-1)*gain)*mix)*out,yd)) for x in u),((0,norm(-1,yd)),(1,norm(1,yd)))]
        # Control point is a parameter pad, not an arbitrary shaper point.
        handles=[];xt=((0,'−1'),(.5,'0'),(1,'+1'));yt=tuple((norm(y,yd),f'{y:g}') for y in (-bound,0,bound))
        note=tr('audio_fx.visual.saturation_note')
    elif family=='xy':
        xkey,ykey=('drive','wet') if kind=='distortion' else ('edge','output')
        xd=(0.,5.) if kind=='distortion' else (0.,100.);yd=(0.,100.) if kind=='distortion' else (-60.,0.)
        handles=[FXVisualHandle(norm(v[xkey],xd),norm(v[ykey],yd),xkey,ykey)]
        xt=((0,str(xd[0])),(1,str(xd[1])));yt=((0,str(yd[0])),(1,str(yd[1])))
        note=tr('audio_fx.visual.xy_note',horizontal=audio_fx_parameter_caption(kind,xkey),vertical=audio_fx_parameter_caption(kind,ykey))
    elif family=='crusher':
        # Reference sine: four cycles at32 samples/cycle, not actual input PCM.
        q=2**(int(v['bits'])-1);hold=int(v['hold']);mix=v['wet']/100
        raw=[.9*math.sin(2*math.pi*i/32) for i in range(129)];processed=[];held=0
        for i,x in enumerate(raw):
            if i%hold==0:
                value=x*q;held=math.copysign(math.floor(abs(value)+.5),value)/q
            processed.append(x+(held-x)*mix)
        steps=[]
        for i,x in enumerate(processed):
            if i:steps.append((i/128,(processed[i-1]+1)/2))
            steps.append((i/128,(x+1)/2))
        curves=[tuple((i/128,(x+1)/2) for i,x in enumerate(raw)),tuple(steps)]
        xd=(0.,128.);xt=tuple((i/128,str(i)) for i in (0,32,64,96,128))
        note=tr('audio_fx.visual.crusher_note',rate=f'{rate/hold/1000:g}')
    elif family=='echo':
        xd=(0.,4000.);yd=(-1.,1.);bars=True
        feedback=v['feedback']/100;wet=v.get('wet',0)/100
        delays=[('delay',v['delay'])] if kind=='echo' else [('left_delay',v['left_delay']),('right_delay',v['right_delay'])]
        for key,delay in delays:
            curves.append(tuple((n*delay/4000,(1+wet*feedback**(n-1))/2) for n in range(1,min(32,int(4000/delay))+1)))
            handles.append(FXVisualHandle(delay/4000,(1+wet)/2,key))
        if wet>0 and delays[0][1]*2<=4000:
            handles.append(FXVisualHandle(delays[0][1]*2/4000,(1+wet*feedback)/2,'','feedback'))
        xt=tuple((t/4000,f'{t/1000:g}s') for t in (0,1000,2000,3000,4000));note=tr('audio_fx.visual.echo_note')
    elif family=='modulation':
        xd=(0.,2.);yd=(0.,1.) if kind=='tremolo' else (-1.,1.)
        depth=v['depth']/100
        def wave(x):
            phase=(x*2)%1
            return 1-4*abs(phase-.5) if v.get('waveform',0)==1 else (1 if phase<.5 else -1) if v.get('waveform',0)==2 else math.sin(2*math.pi*x*2)
        curves=[tuple((x,1-depth/2+depth/2*wave(x) if kind=='tremolo' else (1+depth*wave(x))/2) for x in u)]
        trough=.375 if v.get('waveform',0)==0 else .0 if v.get('waveform',0)==1 else .375
        handles=[FXVisualHandle(trough if kind=='tremolo' else .125,1-depth if kind=='tremolo' else (1+depth)/2,'','depth')]
        xt=((0,'0'),(.5,f'{1/v["rate"]:g}s'),(1,f'{2/v["rate"]:g}s'))
        yt=((0,'0'),(1,'1')) if kind=='tremolo' else ((0,tr('audio_fx.visual.left')),(.5,tr('audio_fx.visual.center')),(1,tr('audio_fx.visual.right')))
        note=tr('audio_fx.visual.modulation_note')
    else: # volume_envelope: BASS_FX_VOLUME_ENV's three linear segments.
        xd=(0.,30000.);yd=(0.,200.)
        t1=v['attack'];t2=t1+v['hold'];t3=t2+v['release']
        curves=[((0,v['start_level']/200),(t1/30000,v['peak_level']/200),(t2/30000,v['peak_level']/200),(t3/30000,v['end_level']/200),(1,v['end_level']/200))]
        # The model uses a separate view-domain set below so small default
        # envelopes remain readable, while drag gestures freeze their viewport.
        unit=10**math.floor(math.log10(max(t3*1.2,1)))
        span=envelope_span or max(500.,min(30000.,math.ceil(t3*1.2/unit)*unit))
        xd=(0.,span);curves=[tuple((x*30000/span,y) for x,y in curves[0])]
        handles=[FXVisualHandle(0,v['start_level']/200,'','start_level'),
                 FXVisualHandle(t1/span,v['peak_level']/200,'attack','peak_level'),
                 FXVisualHandle(t2/span,v['peak_level']/200,'hold'),
                 FXVisualHandle(t3/span,v['end_level']/200,'release','end_level')]
        xt=tuple((x,f'{span*x/1000:g}s') for x in (0,.5,1));yt=((0,'0%'),(.5,'100%'),(1,'200%'))
        note=tr('audio_fx.visual.envelope_note')
    return FXVisualPlot(family,title,note,xt,yt,tuple(curves),tuple(handles),xd,yd,log,bars)


def audio_fx_visual_edit(kind,values,plot,handle,x,y):
    """Map a graph point to existing parameters, not a second value authority."""
    x=max(0,min(1,x));y=max(0,min(1,y));result={}
    dx=(plot.xdomain[0]*(plot.xdomain[1]/plot.xdomain[0])**x if plot.logarithmic else plot.xdomain[0]+x*(plot.xdomain[1]-plot.xdomain[0]))
    dy=plot.ydomain[0]+y*(plot.ydomain[1]-plot.ydomain[0])
    if handle.xkey:
        key=handle.xkey
        if kind=='gate' and key=='hysteresis':dx=values['threshold']-dx
        elif kind=='volume_envelope':
            if key=='hold':dx-=values['attack']
            elif key=='release':dx-=values['attack']+values['hold']
        result[key]=dx
    if handle.ykey:
        key=handle.ykey
        if key=='ratio':dy=max(1,(0-values['threshold'])/max(.001,dy-values['output']-values['threshold']))
        elif key=='gain' and kind in ('low_shelf','high_shelf'):dy*=2
        elif key=='feedback' and plot.family=='echo':dy=dy*100/max(.001,values['wet']/100)
        elif kind=='tremolo' and key=='depth':dy=(1-y)*100
        elif kind=='auto_pan' and key=='depth':dy=max(0,2*y-1)*100
        result[key]=dy
    return result


BMSYNTH_CHORUS_PRESETS = (
    "Default", "Off", "GS Chorus 1", "GS Chorus 2", "GS Chorus 3",
    "GS Chorus 4", "GS Feedback", "GS Flanger", "GS Delay", "GS Delay (FB)",
    "XG Chorus 1", "XG Chorus 2", "XG Chorus 3", "XG Chorus 4",
    "XG Celeste 1", "XG Celeste 2", "XG Celeste 3", "XG Celeste 4",
    "XG Flanger 1", "XG Flanger 2", "XG Flanger 3",
)

EQ_CENTER_INPUT_STEPS = (100, 200, 500)  # key / wheel detent / track click, Hz


def knob_input_steps(span: int, quantum: int = 1) -> tuple[int, int, int]:
    """Key/wheel/page ticks: 0.1/0.5/5% rounded half up to fine precision.

    Coarse domains retain at least 1/5/10 fine increments, bounded by the
    available span. This is input speed, never value quantization or a clamp
    on native events, loaded values, exact reset or direct numeric entry.
    """
    if type(span) is not int or span < 0 or type(quantum) is not int or quantum < 1:
        raise ValueError('knob navigation domain')
    if span == 0:
        return (1, 1, 1)
    quantum = min(quantum, span)
    available = max(1, span // quantum)
    def rounded(divisor):
        return (2 * span + divisor * quantum) // (2 * divisor * quantum)
    key = max(1, rounded(1000))
    wheel = max(key + 1, key * 5, rounded(200))
    page = max(wheel + 1, key * 10, rounded(20))
    if available >= 3:
        key = min(key, available - 2)
        wheel = min(wheel, available - 1)
    return tuple(min(available, count) * quantum for count in (key, wheel, page))


def delay_type_labels() -> tuple[str, str, str, str]:
    return (
        tr("delay.type.off"), tr("delay.type.mono"),
        tr("delay.type.cross"), tr("delay.type.center"),
    )


NEGATIVE_INFINITY = "\u2212\u221e"


@dataclass(frozen=True)
class NumericEntryDomain:
    """Display-unit editing boundary for one existing slider domain.

    Formatting remains in ``format_effect_value``; this maps only the number
    a user can type back to the same raw slider/event value. A no-op edit is
    suppressed by the widget so precise stored tuning is never rounded merely
    by opening and closing the editor.
    """

    kind: str
    raw_minimum: float
    raw_maximum: float
    decimals: int = 0
    scale: float = 1.0
    offset: float = 0.0
    signed: bool = False

    @property
    def minimum(self):
        return -100.0 if self.kind == "drum_fine" else (self.raw_minimum - self.offset) / self.scale

    @property
    def maximum(self):
        return 100.0 if self.kind == "drum_fine" else (self.raw_maximum - self.offset) / self.scale

    def to_entry(self, raw):
        raw = float(raw)
        if self.kind == "drum_fine":
            return (raw - 64) * 100 / (64 if raw < 64 else 63)
        return (raw - self.offset) / self.scale

    def to_raw(self, number):
        number = float(number)
        if not math.isfinite(number) or not self.minimum <= number <= self.maximum:
            raise ValueError("numeric entry domain")
        if self.kind == "drum_fine":
            return round(64 + number * (64 if number < 0 else 63) / 100)
        return self.offset + number * self.scale

    def validate_raw(self, value):
        """Product editing domain only; never a clamp on external native MIDI."""
        number = float(value)
        if not math.isfinite(number) or not self.raw_minimum <= number <= self.raw_maximum:
            raise ValueError('numeric raw domain')
        return number

    def entry_text(self, number):
        spec = f"{'+' if self.signed else ''}.{self.decimals}f"
        return format(number, spec) if self.decimals else (
            f"{round(number):+d}" if self.signed else str(round(number))
        )


def numeric_entry_domain(kind: str, minimum: float, maximum: float) -> NumericEntryDomain:
    """One conversion inventory for effect and detailed-performance labels."""
    scaled = {"tenths_ms": (10, 1), "centihz": (100, 2)}
    precision = {"number_tenth": 1, "number_hundredth": 2, "number_thousandth": 3,
                 "semitones_decimal": 1, "db_decimal": 1, "seconds_decimal": 2,
                 "db_tenth": 1, "ratio": 1, "ratio_decimal": 3,
                 "stereo_multiplier": 2, "master_cents": 2,
                 "hz_decimal": 2, "drum_fine": 1}
    offsets = {"bipolar_percent": 100, "midi_offset": 64,
               "drum_coarse": 64, "drum_pan": 64, "pitch_wheel": 8192}
    signed = {"bipolar_percent", "midi_offset", "drum_coarse", "drum_pan",
              "drum_fine", "cents", "master_cents", "signed_semitones",
              "semitones_decimal", "db", "db_decimal", "db_tenth",
              "signed_percent", "pitch_wheel"}
    scale, scaled_decimals = scaled.get(kind, (1, 0))
    return NumericEntryDomain(
        kind, float(minimum), float(maximum),
        decimals=precision.get(kind, scaled_decimals), scale=scale,
        offset=offsets.get(kind, 0), signed=kind in signed,
    )


def _integer(value: float | int) -> int:
    return int(round(float(value)))


def _signed(value: float, precision: int = 0) -> str:
    return f"{value:+.{precision}f}".replace("-", "\u2212")


def _cents(value: float) -> str:
    unit = "cent" if abs(value) == 1 else "cents"
    return tr(f"effect_unit.{unit}", amount=_signed(value, 0 if abs(value - round(value)) < 1e-8 else 2))


# Presentation only: stable AudioFXParameter.key values and saved tuples are
# unchanged. bass_fx.h/bass.h field meanings differ for these shared keys.
_AUDIO_FX_CAPTION_OVERRIDES = {
    ('bitcrusher','hold'):'sample_hold',
    ('auto_gain','rate'):'gain_rate',
    ('auto_gain','gain'):'amplification',
    ('ring_mod','frequency'):'carrier_frequency',
    ('dc_block','frequency'):'cutoff_frequency',
    ('phaser','frequency'):'base_frequency',
    ('auto_wah','frequency'):'base_frequency',
    **{(kind,'frequency'):'cutoff_frequency' for kind in ('low_pass','high_pass','low_shelf','high_shelf')},
    **{(kind,'frequency'):'center_frequency' for kind in ('parametric_eq','band_pass','notch','all_pass')},
}


def audio_fx_parameter_caption(kind, key):
    """Same parameter meaning in forms and naming/a11y audits."""
    return tr(f"audio_fx.parameter.{_AUDIO_FX_CAPTION_OVERRIDES.get((kind,key),key)}")


def audio_fx_parameter_value(parameter,value):
    """Same precision/units for parameter editors and Reset-default comparison."""
    if parameter.choices:
        choice=parameter.choices[round((value-parameter.minimum)/parameter.step)]
        return (tr('audio_fx.value',value=choice.replace('-','−'),unit=parameter.unit) if parameter.key=='phase'
                else choice if choice.isdecimal() else tr('audio_fx.choice.'+choice))
    decimals=max(0,-round(math.log10(parameter.step)))
    return tr('audio_fx.value',value=f'{value:.{decimals}f}'.replace('-','−'),unit=parameter.unit).strip()


def audio_fx_parameter_tooltip(kind, key):
    """Audible meaning, not ranges or an inline-editor tutorial."""
    overrides = {
        ("distortion", "drive"): "distortion_drive", ("saturator", "drive"): "saturation_drive",
        ("gate", "threshold"): "gate_threshold", ("gate", "attack"): "gate_attack",
        ("gate", "release"): "gate_release", ("gate", "hold"): "gate_hold",
        ("volume_envelope", "attack"): "envelope_attack", ("volume_envelope", "release"): "envelope_release",
        ("volume_envelope", "hold"): "envelope_hold", ("bitcrusher", "hold"): "sample_hold",
        ("auto_gain", "rate"): "gain_rate", ("rotate", "rate"): "rotate_rate",
        ("gargle", "rate"): "gargle_rate", ("auto_gain", "gain"): "auto_gain_gain",
        ("ring_mod", "frequency"): "carrier_frequency", ("dc_block", "frequency"): "dc_frequency",
        ("phaser", "frequency"): "sweep_frequency", ("auto_wah", "frequency"): "sweep_frequency",
        ("freeverb", "width"): "reverb_width",
        **{(kind,'frequency'):'cutoff_frequency' for kind in ('low_pass','high_pass','low_shelf','high_shelf')},
    }
    return tr(f"audio_fx.tip.{overrides.get((kind, key), key)}")


def format_effect_value(value: float | int, kind: str) -> str:
    integer = _integer(value)
    if kind in {"gain_db", "reverb_level"}:
        if integer <= 0:
            return f"{tr('common.off')} ({NEGATIVE_INFINITY} dB)"
        return f"{integer} ({_signed(20.0 * math.log10(integer / 100.0), 1)} dB)"
    if kind == "ms":
        return f"{integer} ms"
    if kind == "tenths_ms":
        return f"{integer / 10.0:.1f} ms"
    if kind == "centihz":
        return f"{integer / 100.0:.2f} Hz"
    if kind == "hz":
        return tr("common.off") if integer == 0 else f"{integer} Hz"
    if kind == "db":
        return f"{_signed(integer)} dB"
    if kind == "db_decimal":
        return f"{_signed(float(value), 1)} dB"
    if kind == "semitones_decimal":
        number = float(value)
        unit = "semitone" if abs(number) == 1 else "semitones"
        return tr(f"effect_unit.{unit}", count=f"{number:.1f}")
    if kind == "db_tenth":
        return f"{_signed(float(value), 1)} dBTP"
    if kind == "semitones":
        unit = "semitone" if abs(integer) == 1 else "semitones"
        return tr(f"effect_unit.{unit}", count=integer)
    if kind == "signed_semitones":
        unit = "semitone" if abs(integer) == 1 else "semitones"
        return tr(f"effect_unit.{unit}", count=_signed(integer))
    if kind == "ratio":
        return f"{float(value):.1f}:1"
    if kind == "ratio_decimal":
        return f"{float(value):.3f}"
    if kind == "cents":
        return _cents(integer)
    if kind == "master_cents":
        return _cents(float(value))
    if kind == "hz_decimal":
        return f"{float(value):.2f} Hz"
    if kind == "drum_coarse":
        return format_effect_value(integer - 64, "semitones")
    if kind == "drum_fine":
        cents = (integer - 64) * 100 / (64 if integer < 64 else 63)
        return _cents(cents) if integer in (0, 64, 127) else tr("effect_unit.drum_fine", value=_signed(cents, 1))
    if kind == "midi_offset":
        return _signed(integer - 64)
    if kind == "pitch_wheel":
        return _signed(integer - 8192)
    if kind == "drum_amount":
        return tr("effect_unit.drum_original") if integer == 127 else f"{integer} / 127"
    if kind == "drum_pan":
        if integer == 128:
            return tr("effect_unit.drum_original")
        if integer == 64:
            return tr("effect_unit.pan_center")
        if integer == 0:
            return tr("effect_unit.pan_random")
        return tr("effect_unit.pan_left" if integer < 64 else "effect_unit.pan_right",
                  amount=abs(integer - 64))
    if kind == "bipolar_percent":
        return f"{integer - 100}%".replace("-", "\u2212")
    if kind == "percent":
        return f"{integer} %"
    if kind == "signed_percent":
        return f"{_signed(integer)} %"
    if kind in {"delay_level", "midi_send"}:
        return str(integer)
    if kind == "stereo_multiplier":
        return f"{max(0.0, min(3.0, float(value))):.2f}"
    if kind == "spatial_mix":
        if integer <= 0:
            return tr("common.off")
        mix_db = -((21 - integer) ** 2) / 20.0
        return f"{integer} ({_signed(mix_db, 1)} dB)"
    if kind == "velocity_gap_native":
        return f"{max(0, integer)} %"
    raise ValueError(f"Unknown effect value kind: {kind}")


EFFECT_TOOLTIPS = {
    "humanize_timing":"humanization.timing_help",
    "humanize_velocity":"humanization.velocity_help",
    "humanize_preserve_simultaneity":"humanization.preservation_help",
    "eq_preset": "tooltip.equalizer.preset",
    "eq_graph": "tooltip.equalizer.graph",
    "eq_filter": "tooltip.equalizer.filter_kind",
    "eq_output_trim": "tooltip.equalizer.output_trim",
    "reverb_preset": "tooltip.reverb.preset", "reverb_level": "tooltip.reverb.level",
    "reverb_time": "tooltip.reverb.time", "reverb_delay": "tooltip.reverb.delay",
    "reverb_hi": "tooltip.reverb.high_cutoff", "reverb_lo": "tooltip.reverb.low_cutoff",
    "chorus_preset": "tooltip.chorus.preset", "ch_level": "tooltip.chorus.level",
    "ch_delay": "tooltip.chorus.delay", "ch_depth": "tooltip.chorus.depth",
    "ch_rate": "tooltip.chorus.rate", "ch_feedback": "tooltip.chorus.feedback",
    "ch_to_reverb": "tooltip.chorus.to_reverb", "delay_preset": "tooltip.delay.preset",
    "delay_type": "tooltip.delay.type", "delay_level": "tooltip.delay.level",
    "delay_time": "tooltip.delay.time", "delay_feedback": "tooltip.delay.feedback",
    "delay_sysex_enabled": "tooltip.delay_sysex.enabled",
    "extra_delay_details_expanded": "tooltip.extended_delay.details",
    "extra_delay_time": "tooltip.extended_delay.time",
    "extra_delay_wet": "tooltip.extended_delay.wet",
    "extra_delay_feedback": "tooltip.extended_delay.feedback",
    "extra_delay_ping_pong": "tooltip.extended_delay.ping_pong",
    "global_reverb": "tooltip.other.global_reverb",
    "compressor_enabled": "tooltip.dynamics.compressor_enabled",
    "comp_threshold": "tooltip.dynamics.threshold",
    "comp_ratio": "tooltip.dynamics.ratio",
    "comp_attack": "tooltip.dynamics.attack",
    "comp_release": "tooltip.dynamics.release",
    "comp_makeup": "tooltip.dynamics.makeup",
    "limiter_enabled": "tooltip.dynamics.limiter_enabled",
    "limiter_ceiling": "tooltip.dynamics.limiter_ceiling",
    "limiter_release": "tooltip.dynamics.limiter_release",
    "tuning_master": "tooltip.performance.master_tuning",
    "tuning_hz": "tooltip.performance.reference_hz",
    "tuning_coarse": "tooltip.performance.coarse_tuning",
    "scale_tuning": "tooltip.performance.scale_tuning",
    "expression_channel": "tooltip.performance.expression_channel",
    "pitch_range": "tooltip.performance.pitch_range",
    "fine_tuning": "tooltip.performance.fine_tuning",
    "coarse_tuning": "tooltip.performance.channel_coarse_tuning",
    "expression_level": "tooltip.performance.expression_level",
    "vibrato_rate": "tooltip.performance.vibrato_rate",
    "vibrato_depth": "tooltip.performance.vibrato_depth",
    "vibrato_delay": "tooltip.performance.vibrato_delay",
    "portamento": "tooltip.performance.portamento",
    "portamento_time": "tooltip.performance.portamento_time",
    "pan_fine": "channel_editor.pan_fine_tooltip",
    "mode_mono": "channel_editor.mode_mono_tooltip",
    "mode_poly": "channel_editor.mode_poly_tooltip",
    "mode_legato": "channel_editor.mode_legato_tooltip",
    "cutoff": "tooltip.performance.cutoff",
    "resonance": "tooltip.performance.resonance",
    "attack": "tooltip.performance.attack",
    "decay": "tooltip.performance.decay",
    "release": "tooltip.performance.release",
    "mix_level": "channel_editor.mix_level_tooltip",
    "transpose": "channel_editor.transpose_tooltip",
    "channel_pressure_vibrato": "channel_editor.channel_pressure_vibrato_tooltip",
    "channel_pressure_pitch": "channel_editor.channel_pressure_pitch_tooltip",
    "channel_pressure_filter": "channel_editor.channel_pressure_filter_tooltip",
    "channel_pressure_volume": "channel_editor.channel_pressure_volume_tooltip",
    "key_pressure_vibrato": "channel_editor.key_pressure_vibrato_tooltip",
    "key_pressure_pitch": "channel_editor.key_pressure_pitch_tooltip",
    "key_pressure_filter": "channel_editor.key_pressure_filter_tooltip",
    "key_pressure_volume": "channel_editor.key_pressure_volume_tooltip",
    "sustain": "channel_editor.sustain_tooltip",
    "sostenuto": "channel_editor.sostenuto_tooltip",
    "soft": "channel_editor.soft_tooltip",
    "channel_pressure": "channel_editor.channel_pressure_tooltip",
    "drum_key": "tooltip.performance.drum_key",
    "drum_level": "tooltip.performance.drum_level",
    "drum_pan": "tooltip.performance.drum_pan",
    "drum_reverb": "tooltip.performance.drum_reverb",
    "drum_chorus": "tooltip.performance.drum_chorus",
    "drum_userfx": "tooltip.performance.drum_userfx",
    "drum_coarse_tuning": "tooltip.performance.drum_coarse_tuning",
    "drum_fine_tuning": "tooltip.performance.drum_fine_tuning",
    "drum_cutoff": "tooltip.performance.drum_cutoff",
    "drum_resonance": "tooltip.performance.drum_resonance",
    "global_chorus": "tooltip.other.global_chorus", "stereo": "tooltip.other.stereo",
    "dx_level": "tooltip.other.spatial",
    "spatial_details_expanded": "tooltip.other.spatial_details_expanded",
    "spatial_input_gain": "tooltip.other.spatial_input_gain",
    "spatial_time": "tooltip.other.spatial_time",
    "spatial_hf_ratio": "tooltip.other.spatial_hf_ratio",
    "spatial_sample_rate": "tooltip.other.spatial_sample_rate",
    "velocity": "tooltip.other.velocity", "overnote": "tooltip.other.overnote",
    "gs_reset": "tooltip.other.gs_reset",
    "ignore_gm1_reset": "tooltip.other.ignore_system_reset",
    "ignore_gm2_reset": "tooltip.other.ignore_system_reset",
    "ignore_xg_reset": "tooltip.other.ignore_system_reset",
}

EFFECT_TOOLTIPS.update({"eq_center": "tooltip.equalizer.center", "eq_width": "tooltip.equalizer.width",
                        "eq_gain": "tooltip.equalizer.gain"})
EFFECT_TOOLTIPS.update({f"modulation_{target}": f"channel_editor.modulation_{target}_tooltip"
                        for target in ("vibrato", "pitch", "filter", "volume")})


def effect_tooltip(key: str) -> str:
    modes={'ignore_gm1_reset':'GM1','ignore_gm2_reset':'GM2','gs_reset':'GS','ignore_xg_reset':'XG'}
    if key in modes:return tr('tooltip.other.ignore_system_reset',mode=modes[key])
    if key in ('drum_reverb','drum_chorus','drum_userfx'):
        return tr('tooltip.performance.drum_send_audio_effect_condition',description=tr(EFFECT_TOOLTIPS[key]))
    return tr(EFFECT_TOOLTIPS[key])


def drum_key_caption(key,*,mode=None):
    """GM2/GS common names,or XG extensions;unknown means numeric only.

    These are standard-kit reference names,not parsed sample-zone identities.
    GS and XG disagree below35,so never present either as a universal mapping.
    """
    key=int(key)
    name=(tr(f'drum_key.xg.{key}') if mode==3 and key in (13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34)
          else tr(f'drum_key.names.{key}') if 27<=key<=87 else None)
    return tr('drum_key.item',key=key,name=name) if name else str(key)
