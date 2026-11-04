"""Shared human-performance settings and deterministic offline timing planner.

Product ranges are user choices,not MIDI/BASS library limits. The live native
planner uses an added timing_ms baseline;file/offline plans know future events.
"""
from dataclasses import dataclass,replace
from collections import defaultdict,deque
import random

@dataclass(frozen=True)
class HumanizationSettings:
    timing_ms:int=0
    velocity:int=0
    preserve_simultaneity:bool=False

    def validated(self):
        from i18n import tr
        if type(self.timing_ms) is not int or not 0<=self.timing_ms<=300:raise ValueError(tr('humanization.invalid_timing'))
        if type(self.velocity) is not int or not 0<=self.velocity<=64:raise ValueError(tr('humanization.invalid_velocity'))
        if type(self.preserve_simultaneity) is not bool:raise ValueError(tr('humanization.invalid_preservation'))
        return self

    @property
    def enabled(self):return bool(self.timing_ms or self.velocity)

def random_velocity(value,amount,rng):
    """A zero-velocity Note On remains Note Off;positive attacks stay positive."""
    return value if not value or not amount else max(1,min(127,value+rng.randint(-amount,amount)))

def humanize_document(document,settings,*,reset_test,speed=1.,rng=None):
    """File reference/export path. Same note-pair offset;controllers stay put.

    At the file start or after a channel state/reset boundary,an early note
    cannot precede the boundary. All attacks retain source order across channels
    and ports. Equal output times keep the original row order.
    This planner is not used on a production MIDI/audio callback.
    """
    settings.validated()
    if not settings.enabled:return document
    rng=rng or random.Random();pending=defaultdict(deque);last_on={};barrier=defaultdict(float);output=[]
    limits=[float('inf')]*len(document.events);following=defaultdict(lambda:float('inf'))
    for index in range(len(document.events)-1,-1,-1):
        event=document.events[index];data=event.data
        if not data:continue
        if data[0]>=240:
            if reset_test(data):
                for channel in range(128):following[channel]=event.seconds
            continue
        kind=data[0]&240;channel=event.channel
        if kind==144 and len(data)==3 and data[2]:limits[index]=following[channel]
        elif kind==192 or kind==176 and len(data)==3 and (data[1] in (0,32,120) or data[1]>=123):following[channel]=event.seconds
    # A later attack's state fence also bounds every preceding attack.
    ceiling=float('inf')
    for index in range(len(document.events)-1,-1,-1):
        data=document.events[index].data
        if len(data)==3 and data[0]&240==144 and data[2]:
            ceiling=min(ceiling,limits[index]);limits[index]=ceiling
    # Anchored source-time groups. Boundaries and repeated keys start a new
    # group;all member fences constrain the common shift before any output.
    groups={};members=defaultdict(list);open_groups={};group_offsets={}
    if settings.preserve_simultaneity:
        for index,event in enumerate(document.events):
            data=event.data
            if not data:continue
            if data[0]>=240:
                if reset_test(data):open_groups.clear()
                continue
            kind=data[0]&240;channel=event.channel;port=channel//16
            if kind==144 and len(data)==3 and data[2]:
                # An interleaved other port closes the earlier contiguous group.
                for other in tuple(open_groups):
                    if other!=port:open_groups.pop(other)
                group=open_groups.get(port);key=(channel,data[1])
                if group is None or not 0<=event.seconds-group[1]<=.005+1e-12 or key in group[2]:
                    group=(index,event.seconds,set());open_groups[port]=group
                group[2].add(key);groups[index]=group[0];members[group[0]].append(index)
            elif kind==192 or kind==176 and len(data)==3 and (data[1] in (0,32,120) or data[1]>=123):open_groups.pop(port,None)
        prior=defaultdict(float);floors={}
        for index,event in enumerate(document.events):
            data=event.data
            if not data:continue
            if data[0]>=240:
                if reset_test(data):
                    for channel in range(128):prior[channel]=event.seconds
            elif index in groups:floors[index]=prior[event.channel]-event.seconds
            elif data[0]&240==192 or data[0]&240==176 and len(data)==3 and (data[1] in (0,32,120) or data[1]>=123):prior[event.channel]=event.seconds
        for group,indices in members.items():
            group_offsets[group]=(max(max(-document.events[i].seconds,floors[i]) for i in indices),min(limits[i]-document.events[i].seconds for i in indices))
    chosen={};last_attack=0.
    for index,event in enumerate(document.events):
        data=event.data;stamp=event.seconds
        if data and data[0]<240:
            kind=data[0]&240;channel=event.channel
            if kind in (128,144) and len(data)==3:
                key=(channel,data[1]);on=kind==144 and data[2]>0
                if on:
                    group=groups.get(index)
                    if group is not None:
                        if group not in chosen:
                            lo,hi=group_offsets[group]
                            # Already reserved same-key starts constrain the whole
                            # next group,not just one voice in its chord.
                            lo=max(lo,last_attack-event.seconds,max(last_on.get((document.events[i].channel,document.events[i].data[1]),0.)-document.events[i].seconds for i in members[group]))
                            chosen[group]=max(lo,min(hi,rng.randint(-settings.timing_ms,settings.timing_ms)*speed/1000))
                        jitter=chosen[group]
                    else:jitter=rng.randint(-settings.timing_ms,settings.timing_ms)*speed/1000
                    stamp=max(0.,last_attack,barrier[channel],last_on.get(key,0.),min(limits[index],stamp+jitter))
                    last_attack=stamp
                    pending[key].append(stamp-event.seconds);last_on[key]=stamp
                    value=random_velocity(data[2],settings.velocity,rng)
                    data=data[:2]+bytes((value,))
                elif pending[key]:stamp=max(0.,stamp+pending[key].popleft())
            elif kind==192 or kind==176 and len(data)==3 and data[1] in (0,32,120,121,123):
                barrier[channel]=stamp
                if kind==176 and data[1] in (120,123):
                    for key in tuple(pending):
                        if key[0]==channel:pending.pop(key,None);last_on.pop(key,None)
        elif data:
            if reset_test(data):
                pending.clear();last_on.clear()
                for channel in range(128):barrier[channel]=stamp
        output.append((stamp,index,replace(event,seconds=stamp,data=data)))
    output.sort(key=lambda row:(row[0],row[1]))
    events=tuple(row[2] for row in output)
    return replace(document,events=events,duration=max(document.duration,max((e.seconds for e in events),default=0.)))
