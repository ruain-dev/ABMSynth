// Native channel-partitioned SoundFont synthesis. The original HMIDI remains
// the authoritative MIDI controller/filter and final DSP stream. Borrowed
// independent FLOAT/DECODE owners synthesize whole MIDI channels in parallel.
// bass.h/bassmidi.h provide every WINAPI/CALLBACK signature below. TIME events
// are forwarded at their original absolute PCM positions, not block-start.
#define NOMINMAX
#include <windows.h>
#include <avrt.h>
#include "../../bass.h"
#include "../../bassmidi.h"
#include <algorithm>
#include <array>
#include <atomic>
#include <condition_variable>
#include <cstring>
#include <cstdint>
#include <memory>
#include <mutex>
#include <thread>
#include <unordered_map>
#include <vector>

namespace {
constexpr unsigned OwnerCount=16; // Caller plus15 persistent native workers.
struct Functions {
    void *event,*events,*setFilter,*setFonts,*getChannel,*setAttribute,*getAttribute;
    void *setSync,*removeSync,*getData,*setDSP,*removeDSP,*error,*setDevice,*getDevice;
    void *channelLock,*getPosition,*streamCreate,*streamFree,*channelFlags;
};
Functions api{};
using Event=BOOL(WINAPI*)(DWORD,DWORD,DWORD,DWORD);
using Events=DWORD(WINAPI*)(DWORD,DWORD,const void*,DWORD);
using Filter=BOOL(WINAPI*)(DWORD,BOOL,MIDIFILTERPROC*,void*);
using Fonts=BOOL(WINAPI*)(DWORD,const void*,DWORD);
using Channel=DWORD(WINAPI*)(DWORD,DWORD);
using SetAttribute=BOOL(WINAPI*)(DWORD,DWORD,float);
using GetAttribute=BOOL(WINAPI*)(DWORD,DWORD,float*);
using SetSync=HSYNC(WINAPI*)(DWORD,DWORD,QWORD,SYNCPROC*,void*);
using RemoveSync=BOOL(WINAPI*)(DWORD,HSYNC);
using GetData=DWORD(WINAPI*)(DWORD,void*,DWORD);
using SetDSP=HDSP(WINAPI*)(DWORD,DSPPROC*,void*,int);
using RemoveDSP=BOOL(WINAPI*)(DWORD,HDSP);
using Error=int(WINAPI*)();
using SetDevice=BOOL(WINAPI*)(DWORD);
using GetDevice=DWORD(WINAPI*)();
using ChannelLock=BOOL(WINAPI*)(DWORD,BOOL);
using GetPosition=QWORD(WINAPI*)(DWORD,DWORD);
using StreamCreate=HSTREAM(WINAPI*)(DWORD,DWORD,DWORD,STREAMPROC*,void*);
using StreamFree=BOOL(WINAPI*)(DWORD);
using ChannelFlags=DWORD(WINAPI*)(DWORD,DWORD,DWORD);
using FontInitUser=DWORD(WINAPI*)(const BASS_FILEPROCS*,void*,DWORD);
using FontLoad=BOOL(WINAPI*)(DWORD,int,int);
using FontFree=BOOL(WINAPI*)(DWORD);
template<class T> T fn(void* value){return reinterpret_cast<T>(value);}
thread_local int facadeError=0;
thread_local DWORD notificationMain=0;
thread_local QWORD notificationDecode=0,notificationDSP=0;
struct Context;
using Tick=unsigned long long;
Tick clockTick(){LARGE_INTEGER value{};QueryPerformanceCounter(&value);return static_cast<Tick>(value.QuadPart);}
bool diagnosticOn(Context*);
void diagnosticMetrics(Context*,std::array<Tick,4>&);
struct MainBarrier;
void diagnosticBarrier(Context*,Tick,Tick,Tick,Tick,const MainBarrier&);
struct DiagnosticDuration {
    std::atomic<Tick>* target;Tick begin;
    explicit DiagnosticDuration(std::atomic<Tick>* value):target(value),begin(value?clockTick():0){}
    ~DiagnosticDuration(){if(target)target->fetch_add(clockTick()-begin,std::memory_order_relaxed);}
};
struct MainBarrier {
    DWORD stream;bool locked;Context* diagnostic=nullptr;Tick before=0,acquired=0;
    unsigned operation=0,mode=0,detail=0;Tick boundaryTick=0,nativeTicks=0,applyTicks=0,deliverTicks=0;
    std::array<Tick,4> metrics{},finalMetrics{};
    explicit MainBarrier(DWORD value,Context* capture=nullptr,unsigned op=0,unsigned flags=0,unsigned count=0):stream(value),operation(op),mode(flags),detail(count){
        diagnostic=diagnosticOn(capture)?capture:nullptr;if(diagnostic)before=clockTick();
        locked=fn<ChannelLock>(api.channelLock)(value,TRUE)!=FALSE;if(diagnostic)acquired=clockTick();}
    ~MainBarrier(){const Tick release=diagnostic?clockTick():0;
        // Snapshot under the still-held primary lock. Reading after unlock
        // could accidentally include the next render's health queries.
        if(diagnostic)diagnosticMetrics(diagnostic,finalMetrics);
        if(locked&&!fn<ChannelLock>(api.channelLock)(stream,FALSE))facadeError=fn<Error>(api.error)();
        if(diagnostic)diagnosticBarrier(diagnostic,before,acquired,release,clockTick(),*this);}
    void boundaryAcquired(){if(diagnostic){boundaryTick=clockTick();diagnosticMetrics(diagnostic,metrics);}}
};
struct Context;
thread_local Context* filterContext=nullptr;
thread_local DWORD filterPosition=0;
thread_local DWORD submissionMain=0,submissionMode=0;
constexpr DWORD ScalarMarker=0x7ffffffeu;
QWORD resolvePosition(QWORD anchor,DWORD position){
    // MIDIFILTERPROC exposes a DWORD PCM address. Recover its nearest live
    // epoch relative to the owners' 64-bit render clock, including wrap.
    // This is not interpretation of arbitrary absolute-time future requests.
    std::int64_t delta=static_cast<std::uint32_t>(position-static_cast<DWORD>(anchor));
    if(delta>=0x80000000ll)delta-=0x100000000ll;
    if(delta<0){const QWORD behind=static_cast<QWORD>(-delta);return behind>anchor?0:anchor-behind;}
    return anchor+static_cast<QWORD>(delta);
}
void CALLBACK collectUserFX(HDSP,DWORD,void*,DWORD,void*);
DWORD CALLBACK userFXSource(HSTREAM,void*,DWORD,void*);
void CALLBACK collectCommonFX(HDSP,DWORD,void*,DWORD,void*);
void CALLBACK feedCommonFX(HDSP,DWORD,void*,DWORD,void*);
void CALLBACK feedCommonUserFX(HDSP,DWORD,void*,DWORD,void*);
void CALLBACK freeCommonUserFX(HSYNC,DWORD,DWORD,void*);
struct SyncRow {Context* context; SYNCPROC* callback;void* user;DWORD type;QWORD parameter;
    std::array<HSYNC,OwnerCount> handles{};std::weak_ptr<SyncRow> self;std::uint64_t order=0;bool active=true;};
std::mutex registryLock;
std::unordered_map<DWORD,std::shared_ptr<Context>> registry;
std::shared_ptr<Context> find(DWORD stream){
    std::lock_guard<std::mutex> guard(registryLock);
    const auto it=registry.find(stream);return it==registry.end()?nullptr:it->second;
}
struct Context {
    DWORD main,device;unsigned maximum,rate;
    std::array<DWORD,OwnerCount> owners{};
    std::array<std::vector<float>,OwnerCount> buffers;
    std::array<std::thread,OwnerCount-1> threads;
    std::recursive_mutex boundary;
    std::mutex workLock;std::condition_variable ready,complete;
    bool stopping=false;unsigned finished=0,frames=0,started=0;std::uint64_t epoch=0;
    // Fixed block threshold: larger spans use all16 render threads. Tiny
    // event-bounded spans stay on the caller to avoid waking15 workers.
    static constexpr unsigned ParallelMinimumFrames=64;
    std::atomic<unsigned long long> serialSpans{0},parallelSpans{0},serialFrames{0},parallelFrames{0};
    // Additive, opt-in diagnostic ABI1. No audio allocations or reader locks.
    // Atomic payload avoids the C++ data race of a plain-payload seqlock.
    static constexpr unsigned DiagnosticWords=48,DiagnosticCapacity=128;
    struct DiagnosticRow {std::atomic<Tick> generation{0};std::array<std::atomic<Tick>,DiagnosticWords> values{};};
    std::array<DiagnosticRow,DiagnosticCapacity> diagnosticRows;
    std::array<DiagnosticRow,DiagnosticCapacity> diagnosticLocks;
    std::array<DiagnosticRow,DiagnosticCapacity> diagnosticSources;
    std::array<DiagnosticRow,DiagnosticCapacity> diagnosticReserves;
    std::atomic<Tick> reservePublished{0},reserveDropped{0};
    std::atomic<Tick> sourcePublished{0},sourceDropped{0},appliedCount{0},reserveTicks{0},fanoutTicks{0},healthTicks{0};
    std::atomic<bool> diagnosticEnabled{false};std::atomic<Tick> diagnosticPublished{0};
    Tick diagnosticFrequency=0;
    bool diagnosticFrame=false;std::array<Tick,DiagnosticWords> diagnosticBlock{};
    std::array<Tick,OwnerCount> pullBegin{},pullEnd{};
    std::array<std::atomic<Tick>,OwnerCount> pullLast{},pullMaximum{},pullLastBegin{},pullLastEnd{};
    std::atomic<Tick> diagnosticSpans{0},lastDispatch{0},lastMainEnd{0},lastWorkerBegin{0},lastJoin{0},maximumJoin{0};
    std::atomic<Tick> eventCalls{0},eventLastWait{0},eventMaximumWait{0},eventLastHold{0},eventMaximumHold{0};
    std::atomic<Tick> eventWaitSum{0},eventHoldSum{0},eventLastBegin{0},eventLastAcquired{0},eventLastRelease{0},eventLastEnd{0};
    std::atomic<Tick> lastBlock{0},maximumBlock{0};
    std::atomic<Tick> lockPublished{0},lockDropped{0},lockLastWait{0},lockMaximumWait{0},lockWaitSum{0};
    static void diagnosticMaximum(std::atomic<Tick>& value,Tick candidate){
        Tick previous=value.load(std::memory_order_relaxed);
        while(previous<candidate&&!value.compare_exchange_weak(previous,candidate,std::memory_order_relaxed)){}
    }
    std::array<DWORD,OwnerCount> bytes{};
    std::atomic<bool> poisoned{false};std::atomic<int> failure{0};
    struct Pending {BASS_MIDI_EVENT event;QWORD position;std::uint64_t order;bool sync;};
    struct PendingLater {
        bool operator()(const Pending& a,const Pending& b)const{
            // A min-heap preserves native source time, then accepted order;
            // owner identity and worker completion never break an event tie.
            return a.position!=b.position?a.position>b.position:a.order>b.order;
        }
    };
    std::vector<Pending> pending;
    struct Scalar {DWORD id;BASS_MIDI_EVENT event;};
    std::vector<Scalar> scalars;
    DWORD scalarSerial=0;
    std::uint64_t acceptedOrder=0,applyingOrder=0;
    QWORD applyingPosition=0,renderPosition=0;
    bool applying=false,asyncSource=false;
    // Deferred callbacks do not carry the original StreamEvents mode. The
    // retained mode is valid only while pending submissions share SYNC policy.
    bool deferredSync=true;
    MIDIFILTERPROC* delegate=nullptr;void* delegateUser=nullptr;
    std::array<bool,128> drums{},defaultDrums{};
    HDSP dsp=0;
    struct WetTap {Context* context;unsigned index;};
    std::array<WetTap,OwnerCount> wetTaps;
    std::array<DWORD,OwnerCount> wetChannels{},wetDSPs{},wetCursors{};
    std::array<std::vector<float>,OwnerCount> wetBuffers;
    std::vector<float> wetSum,wetOutput;
    DWORD wetStream=0,wetRead=0,wetFrames=0;
    bool wetPrepared=false;
    // Optional middle mode: actual send-weighted inputs from the same owners
    // feed one original native FX engine BEFORE the main's final DSP chain.
    // 0=Reverb,1=Chorus,2=UserFX. No reconstructed SF2/drum send arithmetic.
    static constexpr unsigned CommonBusCount=3;
    struct CommonTap {Context* context=nullptr;unsigned owner=0,bus=0,cursor=0;DWORD channel=0;HDSP dsp=0;HSYNC freeSync=0;};
    DWORD commonCarrier=0;unsigned commonMask=0;bool commonPrepared=false,commonCold=true;
    std::array<std::array<CommonTap,CommonBusCount>,OwnerCount> commonCollectors;
    std::array<CommonTap,CommonBusCount> commonFeeds;
    CommonTap commonUserFeed;
    std::array<std::array<std::vector<float>,CommonBusCount>,OwnerCount> commonBuffers;
    std::array<std::vector<float>,CommonBusCount> commonSums;
    std::vector<float> commonOutput,commonDry;
    std::vector<std::shared_ptr<SyncRow>> syncs;
    struct Notification {std::shared_ptr<SyncRow> row;HSYNC handle;DWORD data;QWORD decode,dsp;std::uint64_t order;};
    std::array<std::vector<Notification>,OwnerCount> notifications;
    std::vector<Notification> orderedNotifications;
    std::uint64_t syncOrder=0;
    Context(DWORD anchor,const DWORD* handles,unsigned capacity,unsigned frequency):main(anchor),
        device(fn<GetDevice>(api.getDevice)()),maximum(capacity),rate(frequency){
        std::copy_n(handles,OwnerCount,owners.begin());
        for(auto& buffer:buffers)buffer.resize(static_cast<size_t>(maximum)*2);
        for(unsigned i=0;i<OwnerCount;++i){wetTaps[i]={this,i};wetBuffers[i].resize(static_cast<size_t>(maximum)*2);}
        wetSum.resize(static_cast<size_t>(maximum)*2);wetOutput.resize(static_cast<size_t>(maximum)*2);
        for(auto& rows:notifications)rows.reserve(65536);
        pending.reserve(65536);scalars.reserve(65536);
        orderedNotifications.reserve(65536*OwnerCount);
        LARGE_INTEGER qpcFrequency{};QueryPerformanceFrequency(&qpcFrequency);
        diagnosticFrequency=static_cast<Tick>(qpcFrequency.QuadPart);
        asyncSource=(fn<ChannelFlags>(api.channelFlags)(main,0,0)&BASS_MIDI_ASYNC)!=0;
        renderPosition=fn<GetPosition>(api.getPosition)(owners[0],BASS_POS_BYTE|BASS_POS_DECODE);
        // bassmidi.md StreamCreate: channel10 alone is initially percussion;
        // MIDI ports establish their own defaults explicitly after growth.
        for(unsigned i=0;i<128;++i)drums[i]=defaultDrums[i]=i==9;
    }
    // Each base MIDI channel retains one decoder, including after drum-mode
    // changes. Additional ports share the same channel-index owner, not state.
    unsigned owner(unsigned channel) const {return channel%OwnerCount;}
    static DWORD commonBus(unsigned bus){return bus==0?BASS_MIDI_CHAN_REVERB:bus==1?BASS_MIDI_CHAN_CHORUS:BASS_MIDI_CHAN_USERFX;}
    bool seedCommonRoutes(){
        // Font is private, fully preloaded silent PCM with NOFX; preparation
        // never changes source notes, fonts or exposed channel addresses.
        // Installed2.4.16 initializes send caches only on a real Note. Kill it
        // before decode. The first render reports zero active carrier samples.
        for(unsigned bus=0;bus<CommonBusCount;++bus){
            const std::array<std::pair<DWORD,DWORD>,10> settings{{
                {MIDI_EVENT_DRUMS,0},{MIDI_EVENT_VOLUME,127},{MIDI_EVENT_EXPRESSION,127},
                {MIDI_EVENT_PAN,64},{MIDI_EVENT_MIXLEVEL,100},
                {MIDI_EVENT_REVERB,bus==0?127u:0u},{MIDI_EVENT_CHORUS,bus==1?127u:0u},
                {MIDI_EVENT_USERFX,bus==2?127u:0u},{MIDI_EVENT_NOTE,60u|(1u<<8)},
                {MIDI_EVENT_SOUNDOFF,0}}};
            for(const auto& item:settings)if(!fn<Event>(api.event)(commonCarrier,bus,item.first,item.second)){
                fail(fn<Error>(api.error)());return false;}
        }
        return true;
    }
    bool commonEvent(const BASS_MIDI_EVENT& event){
        if(!commonCarrier)return true;
        const DWORD type=event.event;
        // CC sends/program/pressure/pitch affect the genuine source owners,
        // never our internal feed channels. Global effect state has one owner.
        if((type>=MIDI_EVENT_REVERB_MACRO&&type<=MIDI_EVENT_CHORUS_REVERB)||
           (type>=MIDI_EVENT_USERFX_LEVEL&&type<=MIDI_EVENT_USERFX_CHORUS)||
           type==MIDI_EVENT_SYSTEM||type==MIDI_EVENT_SYSTEMEX){
            if(!fn<Event>(api.event)(commonCarrier,0,type,event.param)){
                fail(fn<Error>(api.error)());return false;}
            if(type==MIDI_EVENT_SYSTEM||type==MIDI_EVENT_SYSTEMEX)return seedCommonRoutes();
        }
        return true;
    }
    bool retireCommon(){
        // The public delay graph may already have freed this borrowed child.
        // Its FREE sync clears these handles before Context can be retired.
        auto& user=commonUserFeed;
        if(user.dsp){if(!fn<RemoveDSP>(api.removeDSP)(user.channel,user.dsp))return false;user.dsp=0;}
        if(user.freeSync){if(!fn<RemoveSync>(api.removeSync)(user.channel,user.freeSync))return false;user.freeSync=0;}
        user.channel=0;user.cursor=0;
        for(auto& rows:commonCollectors)for(auto& tap:rows){
            if(tap.dsp){if(!fn<RemoveDSP>(api.removeDSP)(tap.channel,tap.dsp))return false;tap.dsp=0;}
            if(tap.channel){if(!fn<StreamFree>(api.streamFree)(tap.channel))return false;tap.channel=0;}
        }
        for(auto& tap:commonFeeds){
            if(tap.dsp){if(!fn<RemoveDSP>(api.removeDSP)(tap.channel,tap.dsp))return false;tap.dsp=0;}
            if(tap.channel){if(!fn<StreamFree>(api.streamFree)(tap.channel))return false;tap.channel=0;}
        }
        commonCarrier=0;commonMask=0;commonPrepared=false;return true;
    }
    bool prepareCommon(DWORD carrier,unsigned mask){
        if(!carrier||carrier==main||commonCarrier||wetStream||!mask||(mask&~7u))return false;
        for(DWORD ownerHandle:owners)if(carrier==ownerHandle)return false;
        commonCarrier=carrier;commonMask=mask;
        for(auto& rows:commonBuffers)for(auto& samples:rows)samples.resize(static_cast<size_t>(maximum)*2);
        for(auto& samples:commonSums)samples.resize(static_cast<size_t>(maximum)*2);
        commonOutput.resize(static_cast<size_t>(maximum)*2);
        commonDry.resize(static_cast<size_t>(maximum)*2);
        for(unsigned i=0;i<OwnerCount;++i)for(unsigned bus=0;bus<CommonBusCount;++bus){
            if(!(commonMask&(1u<<bus)))continue;
            auto& tap=commonCollectors[i][bus];tap.context=this;tap.owner=i;tap.bus=bus;
            tap.channel=fn<Channel>(api.getChannel)(owners[i],commonBus(bus));
            if(!tap.channel){fail(fn<Error>(api.error)());return false;}
            tap.dsp=fn<SetDSP>(api.setDSP)(tap.channel,collectCommonFX,&tap,2147483647);
            if(!tap.dsp){fail(fn<Error>(api.error)());return false;}
        }
        for(unsigned bus=0;bus<CommonBusCount;++bus){
            auto& tap=commonFeeds[bus];tap.context=this;tap.bus=bus;
            tap.channel=fn<Channel>(api.getChannel)(carrier,bus);
            if(!tap.channel){fail(fn<Error>(api.error)());return false;}
            tap.dsp=fn<SetDSP>(api.setDSP)(tap.channel,feedCommonFX,&tap,2147483647);
            if(!tap.dsp){fail(fn<Error>(api.error)());return false;}
        }
        return seedCommonRoutes();
    }
    void address(unsigned type,unsigned channel,unsigned parameter){
        if(channel<drums.size()&&(type==MIDI_EVENT_DRUMS||type==MIDI_EVENT_DEFDRUMS)){
            drums[channel]=parameter!=0;if(type==MIDI_EVENT_DEFDRUMS)defaultDrums[channel]=drums[channel];}
        if(type==MIDI_EVENT_SYSTEM||type==MIDI_EVENT_SYSTEMEX)drums=defaultDrums;
    }
    void fail(int error){failure.store(error?error:BASS_ERROR_UNKNOWN);poisoned.store(true);facadeError=failure.load();}
    unsigned dispatchMinimum()const{return ParallelMinimumFrames;}
    bool clone(const BASS_MIDI_EVENT& source,bool triggerSync=true){
        if(poisoned.load()){facadeError=failure.load();return false;}
        if(pending.size()==pending.capacity()){fail(BASS_ERROR_BUSY);return false;}
        BASS_MIDI_EVENT event=source;event.tick=0;
        pending.push_back({event,resolvePosition(renderPosition,event.pos),++acceptedOrder,triggerSync});
        std::push_heap(pending.begin(),pending.end(),PendingLater{});return true;
    }
    bool apply(const Pending& item){
        BASS_MIDI_EVENT event=item.event;event.tick=event.pos=0;
        address(event.event,event.chan,event.param);
        // Header-defined global MIDI settings reach all decoders. Channel
        // controls/program/pressure stay with their channel's one decoder.
        const DWORD type=event.event;
        const bool global=type==MIDI_EVENT_SYSTEM||type==MIDI_EVENT_SYSTEMEX||type==MIDI_EVENT_TEMPO||
            type==MIDI_EVENT_MASTERVOL||type==MIDI_EVENT_MASTER_FINETUNE||type==MIDI_EVENT_MASTER_COARSETUNE||
            (type>=MIDI_EVENT_REVERB_MACRO&&type<=MIDI_EVENT_CHORUS_REVERB)||
            (type>=MIDI_EVENT_USERFX_LEVEL&&type<=MIDI_EVENT_USERFX_CHORUS);
        const unsigned first=global?0:owner(event.chan);
        const unsigned last=global?OwnerCount:first+1;
        applying=true;applyingOrder=item.order;applyingPosition=item.position;
        {DiagnosticDuration diagnostic(diagnosticEnabled.load(std::memory_order_relaxed)?&fanoutTicks:nullptr);
        for(unsigned i=first;i<last;++i){
            if(fn<Events>(api.events)(owners[i],BASS_MIDI_EVENTS_STRUCT|
                    (item.sync?BASS_MIDI_EVENTS_SYNC:0),&event,1)!=1){applying=false;fail(fn<Error>(api.error)());return false;}
        }}
        if(!commonEvent(event)){applying=false;return false;}
        if(commonCarrier&&event.event==MIDI_EVENT_NOTE&&(event.param>>8&255u)>0)commonCold=false;
        if(diagnosticEnabled.load(std::memory_order_relaxed))appliedCount.fetch_add(1,std::memory_order_relaxed);
        applying=false;return !poisoned.load();
    }
    bool applyDue(){
        while(!pending.empty()&&pending.front().position<=renderPosition){
            std::pop_heap(pending.begin(),pending.end(),PendingLater{});
            // Remove the retained item before native callbacks. Reentrant
            // submissions can safely append to the same bounded heap.
            const Pending item=pending.back();pending.pop_back();
            if(!apply(item))return false;
        }
        return !poisoned.load();
    }
    void pull(unsigned index){
        if(diagnosticFrame)pullBegin[index]=clockTick();
        if(!fn<SetDevice>(api.setDevice)(device)){bytes[index]=DWORD(-1);fail(fn<Error>(api.error)());}
        else{bytes[index]=fn<GetData>(api.getData)(owners[index],buffers[index].data(),frames*8);
            if(bytes[index]!=frames*8)fail(fn<Error>(api.error)());}
        if(diagnosticFrame)pullEnd[index]=clockTick();
    }
    bool retireWet(){
        for(unsigned i=0;i<OwnerCount;++i){
            if(wetDSPs[i]){if(!fn<RemoveDSP>(api.removeDSP)(wetChannels[i],wetDSPs[i]))return false;wetDSPs[i]=0;}
            if(wetChannels[i]){if(!fn<StreamFree>(api.streamFree)(wetChannels[i]))return false;wetChannels[i]=0;}
        }
        if(wetStream){if(!fn<StreamFree>(api.streamFree)(wetStream))return false;wetStream=0;}
        wetPrepared=false;return true;
    }
    DWORD prepareWet(){
        if(commonCarrier){
            auto& tap=commonUserFeed;
            if(tap.channel)return tap.channel;
            tap.context=this;tap.bus=2;
            tap.channel=fn<Channel>(api.getChannel)(commonCarrier,BASS_MIDI_CHAN_USERFX);
            if(!tap.channel){fail(fn<Error>(api.error)());return 0;}
            tap.freeSync=fn<SetSync>(api.setSync)(tap.channel,BASS_SYNC_FREE,0,freeCommonUserFX,&tap);
            if(!tap.freeSync){tap.channel=0;fail(fn<Error>(api.error)());return 0;}
            tap.dsp=fn<SetDSP>(api.setDSP)(tap.channel,feedCommonUserFX,&tap,2147483647);
            if(!tap.dsp){
                const int error=fn<Error>(api.error)();
                if(fn<RemoveSync>(api.removeSync)(tap.channel,tap.freeSync)){tap.freeSync=0;tap.channel=0;}
                fail(error);return 0;
            }
            return tap.channel;
        }
        if(wetStream)return wetStream;
        // USERFX inputs retain BASSMIDI's per-key sends. Native collectors do
        // not decode MIDI taps with GetData. Only the synthetic combined bus
        // owns a readable stream and runs the project's original wet DSP once.
        wetStream=fn<StreamCreate>(api.streamCreate)(rate,2,BASS_SAMPLE_FLOAT|BASS_STREAM_DECODE,userFXSource,this);
        if(!wetStream)return 0;
        for(unsigned i=0;i<OwnerCount;++i){
            wetChannels[i]=fn<Channel>(api.getChannel)(owners[i],BASS_MIDI_CHAN_USERFX);
            if(!wetChannels[i]){fail(fn<Error>(api.error)());return 0;}
            wetDSPs[i]=fn<SetDSP>(api.setDSP)(wetChannels[i],collectUserFX,&wetTaps[i],2147483647);
            if(!wetDSPs[i]){fail(fn<Error>(api.error)());return 0;}
        }
        return wetStream;
    }
    void start(){
        for(unsigned i=1;i<OwnerCount;++i)threads[i-1]=std::thread([this,i]{
            DWORD task=0;HANDLE mmcss=AvSetMmThreadCharacteristicsW(L"Pro Audio",&task);
            std::uint64_t previous=0;
            std::unique_lock<std::mutex> lock(workLock);
            ++started;complete.notify_one();
            for(;;){ready.wait(lock,[&]{return stopping||epoch!=previous;});if(stopping)break;
                previous=epoch;lock.unlock();pull(i);lock.lock();++finished;complete.notify_one();}
            lock.unlock();if(mmcss)AvRevertMmThreadCharacteristics(mmcss);
        });
        std::unique_lock<std::mutex> lock(workLock);complete.wait(lock,[&]{return started==OwnerCount-1;});
    }
    void stop(){
        {std::lock_guard<std::mutex> guard(workLock);stopping=true;ready.notify_all();}
        for(auto& thread:threads)if(thread.joinable())thread.join();
    }
    ~Context(){stop();}
    void deliver(){
        orderedNotifications.clear();
        for(auto& rows:notifications){orderedNotifications.insert(orderedNotifications.end(),rows.begin(),rows.end());rows.clear();}
        std::sort(orderedNotifications.begin(),orderedNotifications.end(),[](const Notification& a,const Notification& b){
            if(a.decode!=b.decode)return a.decode<b.decode;
            if(a.order!=b.order)return a.order<b.order;
            return a.row&&b.row?a.row->order<b.row->order:bool(a.row);});
        for(size_t i=0;i<orderedNotifications.size();++i){const auto row=orderedNotifications[i];
            if(!row.row||!row.row->active)continue;
            const DWORD previousMain=notificationMain;const QWORD previousDecode=notificationDecode,previousDSP=notificationDSP;
            notificationMain=main;notificationDecode=row.decode;notificationDSP=row.dsp;
            row.row->callback(row.row->handles[0],main,row.data,row.row->user);
            notificationMain=previousMain;notificationDecode=previousDecode;notificationDSP=previousDSP;
            if(row.row->type&BASS_SYNC_ONETIME)row.row->active=false;
        }
        orderedNotifications.clear();
    }
    void renderSpan(float* destination,unsigned count){
        if(poisoned.load()){std::fill_n(destination,static_cast<size_t>(count)*2,0.f);return;}
        if(wetStream){wetCursors.fill(0);
            for(auto& samples:wetBuffers)std::fill_n(samples.data(),static_cast<size_t>(count)*2,0.f);}
        if(commonCarrier){
            commonPrepared=false;
            for(unsigned i=0;i<OwnerCount;++i)for(unsigned bus=0;bus<CommonBusCount;++bus){
                commonCollectors[i][bus].cursor=0;
                std::fill_n(commonBuffers[i][bus].data(),static_cast<size_t>(count)*2,0.f);}
            for(auto& tap:commonFeeds)tap.cursor=0;
            commonUserFeed.cursor=0;
        }
        const bool serial=count<dispatchMinimum();
        const Tick dispatch=diagnosticFrame?clockTick():0;Tick dispatched=0,joinBegin=0,joinEnd=0;
        if(serial){
            serialSpans.fetch_add(1,std::memory_order_relaxed);serialFrames.fetch_add(count,std::memory_order_relaxed);
            frames=count;
            // Preserve each independent decoder and its DSP/voice state;
            // only the dispatch mechanism changes for short spans.
            for(unsigned i=0;i<OwnerCount;++i)pull(i);
        }else{
            parallelSpans.fetch_add(1,std::memory_order_relaxed);parallelFrames.fetch_add(count,std::memory_order_relaxed);
            {std::lock_guard<std::mutex> lock(workLock);frames=count;finished=0;++epoch;ready.notify_all();}
            if(diagnosticFrame)dispatched=clockTick();
            pull(0);
            if(diagnosticFrame)joinBegin=clockTick();
            {std::unique_lock<std::mutex> lock(workLock);complete.wait(lock,[&]{return finished==OwnerCount-1;});}
            if(diagnosticFrame)joinEnd=clockTick();
        }
        if(diagnosticFrame){
            ++diagnosticBlock[serial?7:8];diagnosticSpans.fetch_add(1,std::memory_order_relaxed);
            if(!diagnosticBlock[9])diagnosticBlock[9]=dispatch;
            diagnosticBlock[10]=serial?pullEnd[OwnerCount-1]:dispatched;diagnosticBlock[11]=pullEnd[0];
            const Tick joined=serial?0:joinEnd-joinBegin;diagnosticBlock[12]+=joined;
            diagnosticBlock[13]=std::max(diagnosticBlock[13],joined);
            Tick workerLatest=0;
            // Preserve the legacy ABI1 four-owner slots without overrunning
            // its48 words. These slots are not all16-owner diagnostics.
            for(unsigned i=0;i<4;++i){
                if(!diagnosticBlock[16+i*2])diagnosticBlock[16+i*2]=pullBegin[i];
                diagnosticBlock[17+i*2]=pullEnd[i];
                const Tick duration=pullEnd[i]-pullBegin[i];diagnosticBlock[24+i]+=duration;
                diagnosticBlock[28+i]=std::max(diagnosticBlock[28+i],duration);
                if(!serial&&i){diagnosticBlock[32+i]=std::max(diagnosticBlock[32+i],pullBegin[i]-dispatch);
                    workerLatest=std::max(workerLatest,pullBegin[i]);}
                pullLast[i].store(duration,std::memory_order_relaxed);diagnosticMaximum(pullMaximum[i],duration);
                pullLastBegin[i].store(pullBegin[i],std::memory_order_relaxed);pullLastEnd[i].store(pullEnd[i],std::memory_order_relaxed);
            }
            diagnosticBlock[14]=std::max({diagnosticBlock[14],diagnosticBlock[33],diagnosticBlock[34],diagnosticBlock[35]});
            lastDispatch.store(dispatch,std::memory_order_relaxed);lastMainEnd.store(pullEnd[0],std::memory_order_relaxed);
            lastWorkerBegin.store(workerLatest,std::memory_order_relaxed);lastJoin.store(joined,std::memory_order_relaxed);diagnosticMaximum(maximumJoin,joined);
            diagnosticBlock[15]=joinEnd;
        }
        // A note observer may query logical-main BASS positions. Deliver only
        // on this main render thread after joining, never from a shard worker
        // waiting for the main channel lock held by its parent.
        deliver();
        if(poisoned.load()){std::fill_n(destination,static_cast<size_t>(count)*2,0.f);return;}
        std::copy_n(buffers[0].data(),static_cast<size_t>(count)*2,destination);
        for(unsigned ownerIndex=1;ownerIndex<OwnerCount;++ownerIndex)
            for(size_t sample=0;sample<static_cast<size_t>(count)*2;++sample)
                destination[sample]=destination[sample]+buffers[ownerIndex][sample];
        if(commonCarrier){
            std::copy_n(destination,static_cast<size_t>(count)*2,commonDry.data());
            for(unsigned bus=0;bus<CommonBusCount;++bus){
                auto& sum=commonSums[bus];std::copy_n(commonBuffers[0][bus].data(),static_cast<size_t>(count)*2,sum.data());
                for(unsigned i=0;i<OwnerCount;++i){
                    const unsigned got=commonCollectors[i][bus].cursor;
                    if(got&&got!=count){fail(BASS_ERROR_ILLPARAM);std::fill_n(destination,static_cast<size_t>(count)*2,0.f);return;}
                    if(i)for(size_t sample=0;sample<static_cast<size_t>(count)*2;++sample)sum[sample]+=commonBuffers[i][bus][sample];
                }
            }
            // Do not advance native chorus/reverb across the initial silence.
            // Reset after music does not re-enter cold state: preserve tails.
            if(commonCold){
                for(const auto& bus:commonSums){
                    if(std::any_of(bus.begin(),bus.begin()+static_cast<size_t>(count)*2,[](float value){return value!=0.f;})){commonCold=false;break;}
                }
                if(commonCold)return;
            }
            commonPrepared=true;
            const DWORD got=fn<GetData>(api.getData)(commonCarrier,commonOutput.data(),count*8);
            commonPrepared=false;
            if(got!=count*8||poisoned.load()){
                if(!poisoned.load())fail(fn<Error>(api.error)());std::fill_n(destination,static_cast<size_t>(count)*2,0.f);return;}
            for(const auto& tap:commonFeeds)if(tap.cursor!=count){fail(BASS_ERROR_ILLPARAM);std::fill_n(destination,static_cast<size_t>(count)*2,0.f);return;}
            if(commonUserFeed.dsp&&commonUserFeed.cursor!=count){fail(BASS_ERROR_ILLPARAM);std::fill_n(destination,static_cast<size_t>(count)*2,0.f);return;}
            // Carrier feed2 supplies actual dry minus the other two feeds.
            // Native final mix now contains dry exactly once plus shared FX.
            std::copy_n(commonOutput.data(),static_cast<size_t>(count)*2,destination);
        }
        if(wetStream){
            std::copy_n(wetBuffers[0].data(),static_cast<size_t>(count)*2,wetSum.data());
            for(unsigned i=1;i<OwnerCount;++i)for(size_t sample=0;sample<static_cast<size_t>(count)*2;++sample)
                wetSum[sample]=wetSum[sample]+wetBuffers[i][sample];
            wetRead=0;wetFrames=count;wetPrepared=true;
            const DWORD got=fn<GetData>(api.getData)(wetStream,wetOutput.data(),count*8);
            wetPrepared=false;
            if(got!=count*8){fail(fn<Error>(api.error)());std::fill_n(destination,static_cast<size_t>(count)*2,0.f);return;}
            for(size_t sample=0;sample<static_cast<size_t>(count)*2;++sample)destination[sample]+=wetOutput[sample];
        }
    }
    void render(float* destination,unsigned count){
        const bool capture=diagnosticEnabled.load(std::memory_order_relaxed);
        const Tick entered=capture?clockTick():0;
        std::lock_guard<std::recursive_mutex> guard(boundary);
        diagnosticFrame=capture;
        if(capture){diagnosticBlock.fill(0);diagnosticBlock[1]=entered;diagnosticBlock[2]=clockTick();
            diagnosticBlock[4]=renderPosition;diagnosticBlock[5]=count;diagnosticBlock[6]=GetCurrentThreadId();
            diagnosticBlock[36]=eventCalls.load(std::memory_order_relaxed);}
        const QWORD end=renderPosition+static_cast<QWORD>(count)*8;
        unsigned offset=0;
        while(!poisoned.load()){
            if(!applyDue())break;
            if(renderPosition>=end){deliver();break;}
            QWORD next=std::min(end,renderPosition+static_cast<QWORD>(maximum)*8);
            if(!pending.empty())next=std::min(next,pending.front().position);
            if(next<=renderPosition||((next-renderPosition)%8)){fail(BASS_ERROR_ILLPARAM);break;}
            const unsigned span=static_cast<unsigned>((next-renderPosition)/8);
            renderSpan(destination+static_cast<size_t>(offset)*2,span);
            offset+=span;renderPosition=next;
        }
        if(poisoned.load())std::fill_n(destination,static_cast<size_t>(count)*2,0.f);
        if(capture){
            diagnosticBlock[3]=clockTick();diagnosticBlock[37]=eventCalls.load(std::memory_order_relaxed);
            diagnosticBlock[38]=eventLastWait.load(std::memory_order_relaxed);diagnosticBlock[39]=eventMaximumWait.load(std::memory_order_relaxed);
            diagnosticBlock[40]=eventLastHold.load(std::memory_order_relaxed);diagnosticBlock[41]=eventMaximumHold.load(std::memory_order_relaxed);
            diagnosticBlock[42]=eventLastBegin.load(std::memory_order_relaxed);diagnosticBlock[43]=eventLastAcquired.load(std::memory_order_relaxed);
            diagnosticBlock[44]=eventLastRelease.load(std::memory_order_relaxed);diagnosticBlock[45]=eventLastEnd.load(std::memory_order_relaxed);
            float activeVoices=0;
            {DiagnosticDuration diagnostic(&healthTicks);
                for(DWORD handle:owners){float active=0;
                    if(!fn<GetAttribute>(api.getAttribute)(handle,BASS_ATTRIB_MIDI_VOICES_ACTIVE,&active)){
                        fail(fn<Error>(api.error)());break;}
                    activeVoices+=active;}}
            diagnosticBlock[46]=poisoned.load()?1:0;diagnosticBlock[47]=static_cast<Tick>(activeVoices);
            const Tick sequence=diagnosticPublished.load(std::memory_order_relaxed)+1;diagnosticBlock[0]=sequence;
            auto& row=diagnosticRows[(sequence-1)%DiagnosticCapacity];
            row.generation.exchange(sequence*2-1,std::memory_order_acq_rel);
            for(unsigned i=0;i<DiagnosticWords;++i)row.values[i].store(diagnosticBlock[i],std::memory_order_relaxed);
            row.generation.store(sequence*2,std::memory_order_release);diagnosticPublished.store(sequence,std::memory_order_release);
            const Tick elapsed=diagnosticBlock[3]-entered;lastBlock.store(elapsed,std::memory_order_relaxed);diagnosticMaximum(maximumBlock,elapsed);
        }
        diagnosticFrame=false;
    }
};
bool diagnosticOn(Context* context){return context&&context->diagnosticEnabled.load(std::memory_order_relaxed);}
void diagnosticMetrics(Context* context,std::array<Tick,4>& values){
    values={context->appliedCount.load(std::memory_order_relaxed),context->reserveTicks.load(std::memory_order_relaxed),
        context->fanoutTicks.load(std::memory_order_relaxed),context->healthTicks.load(std::memory_order_relaxed)};
}
void diagnosticBarrier(Context* context,Tick begin,Tick acquired,Tick release,Tick end,const MainBarrier& barrier){
    const Tick wait=acquired-begin,hold=release-acquired;
    context->eventLastWait.store(wait,std::memory_order_relaxed);Context::diagnosticMaximum(context->eventMaximumWait,wait);
    context->eventLastHold.store(hold,std::memory_order_relaxed);Context::diagnosticMaximum(context->eventMaximumHold,hold);
    context->eventWaitSum.fetch_add(wait,std::memory_order_relaxed);context->eventHoldSum.fetch_add(hold,std::memory_order_relaxed);
    context->eventLastBegin.store(begin,std::memory_order_relaxed);context->eventLastAcquired.store(acquired,std::memory_order_relaxed);
    context->eventLastRelease.store(release,std::memory_order_relaxed);context->eventLastEnd.store(end,std::memory_order_relaxed);
    context->eventCalls.fetch_add(1,std::memory_order_relaxed);
    const Tick sequence=context->sourcePublished.fetch_add(1,std::memory_order_acq_rel)+1;
    auto& row=context->diagnosticSources[(sequence-1)%Context::DiagnosticCapacity];
    Tick previous=row.generation.load(std::memory_order_relaxed);
    if((previous&1)||!row.generation.compare_exchange_strong(previous,sequence*2-1,std::memory_order_acq_rel)){
        context->sourceDropped.fetch_add(1,std::memory_order_relaxed);return;}
    const auto& metrics=barrier.finalMetrics;
    const Tick values[]{sequence,begin,acquired,release,end,GetCurrentThreadId(),barrier.operation,barrier.mode,barrier.detail,wait,hold,
        barrier.boundaryTick?barrier.boundaryTick-acquired:0,barrier.nativeTicks,barrier.applyTicks,barrier.deliverTicks,
        metrics[0]-barrier.metrics[0],metrics[1]-barrier.metrics[1],metrics[2]-barrier.metrics[2],metrics[3]-barrier.metrics[3],context->poisoned.load()?1ULL:0ULL};
    for(unsigned i=0;i<20;++i)row.values[i].store(values[i],std::memory_order_relaxed);
    row.generation.store(sequence*2,std::memory_order_release);
}
BOOL WINAPI channelLock(DWORD stream,BOOL locked){
    auto context=find(stream);
    if(!diagnosticOn(context.get()))return fn<ChannelLock>(api.channelLock)(stream,locked);
    // NativeExecutor's OUTER lock occurs before facade StreamEvents. Its
    // genuine wait must be timed here, not hidden by the recursive inner lock.
    const Tick begin=clockTick();const BOOL result=fn<ChannelLock>(api.channelLock)(stream,locked);const Tick end=clockTick();
    if(locked){const Tick wait=end-begin;context->lockLastWait.store(wait,std::memory_order_relaxed);
        Context::diagnosticMaximum(context->lockMaximumWait,wait);context->lockWaitSum.fetch_add(wait,std::memory_order_relaxed);}
    const Tick sequence=context->lockPublished.fetch_add(1,std::memory_order_acq_rel)+1;
    auto& row=context->diagnosticLocks[(sequence-1)%Context::DiagnosticCapacity];
    Tick previous=row.generation.load(std::memory_order_relaxed);
    if((previous&1)||!row.generation.compare_exchange_strong(previous,sequence*2-1,std::memory_order_acq_rel)){
        context->lockDropped.fetch_add(1,std::memory_order_relaxed);return result;}
    const Tick values[]{sequence,begin,end,stream,GetCurrentThreadId(),locked?1ULL:0ULL,result?1ULL:0ULL,end-begin};
    for(unsigned i=0;i<8;++i)row.values[i].store(values[i],std::memory_order_relaxed);
    row.generation.store(sequence*2,std::memory_order_release);return result;
}
void CALLBACK collectUserFX(HDSP,DWORD,void* data,DWORD bytes,void* user){
    const auto& tap=*static_cast<Context::WetTap*>(user);auto& context=*tap.context;
    if(!data||bytes%8)return;
    const DWORD count=bytes/8,offset=context.wetCursors[tap.index];
    if(offset>context.frames||count>context.frames-offset){context.fail(BASS_ERROR_ILLPARAM);std::fill_n(static_cast<float*>(data),bytes/4,0.f);return;}
    std::copy_n(static_cast<float*>(data),bytes/4,context.wetBuffers[tap.index].data()+static_cast<size_t>(offset)*2);
    context.wetCursors[tap.index]+=count;
    // The combined bus alone contributes processed wet output.
    std::fill_n(static_cast<float*>(data),bytes/4,0.f);
}
DWORD CALLBACK userFXSource(HSTREAM,void* data,DWORD bytes,void* user){
    auto& context=*static_cast<Context*>(user);
    if(!data||bytes%8||!context.wetPrepared)return BASS_STREAMPROC_END;
    const DWORD count=bytes/8;
    if(context.wetRead>context.wetFrames||count>context.wetFrames-context.wetRead){context.fail(BASS_ERROR_ILLPARAM);return BASS_STREAMPROC_END;}
    std::copy_n(context.wetSum.data()+static_cast<size_t>(context.wetRead)*2,bytes/4,static_cast<float*>(data));
    context.wetRead+=count;return bytes;
}
void CALLBACK collectCommonFX(HDSP,DWORD,void* data,DWORD bytes,void* user){
    auto& tap=*static_cast<Context::CommonTap*>(user);auto& context=*tap.context;
    if(!data||bytes%8){context.fail(BASS_ERROR_ILLPARAM);return;}
    const unsigned count=bytes/8;
    if(tap.cursor>context.frames||count>context.frames-tap.cursor){
        context.fail(BASS_ERROR_ILLPARAM);std::fill_n(static_cast<float*>(data),bytes/4,0.f);return;}
    std::copy_n(static_cast<float*>(data),bytes/4,
        context.commonBuffers[tap.owner][tap.bus].data()+static_cast<size_t>(tap.cursor)*2);
    tap.cursor+=count;std::fill_n(static_cast<float*>(data),bytes/4,0.f);
}
void CALLBACK feedCommonFX(HDSP,DWORD,void* data,DWORD bytes,void* user){
    auto& tap=*static_cast<Context::CommonTap*>(user);auto& context=*tap.context;
    if(!data||bytes%8){context.fail(BASS_ERROR_ILLPARAM);return;}
    const unsigned count=bytes/8;
    if(!context.commonPrepared||tap.cursor>context.frames||count>context.frames-tap.cursor){
        context.fail(BASS_ERROR_ILLPARAM);std::fill_n(static_cast<float*>(data),bytes/4,0.f);return;}
    if(tap.bus==2){
        auto* output=static_cast<float*>(data);const size_t start=static_cast<size_t>(tap.cursor)*2;
        for(size_t i=0;i<bytes/4;++i)output[i]=context.commonDry[start+i]-(context.commonSums[0][start+i]+context.commonSums[1][start+i]);
    }else std::copy_n(context.commonSums[tap.bus].data()+static_cast<size_t>(tap.cursor)*2,bytes/4,static_cast<float*>(data));
    tap.cursor+=count;
}
void CALLBACK feedCommonUserFX(HDSP,DWORD,void* data,DWORD bytes,void* user){
    auto& tap=*static_cast<Context::CommonTap*>(user);auto& context=*tap.context;
    if(!data||bytes%8){context.fail(BASS_ERROR_ILLPARAM);return;}
    const unsigned count=bytes/8;
    if(!context.commonPrepared||tap.cursor>context.frames||count>context.frames-tap.cursor){
        context.fail(BASS_ERROR_ILLPARAM);std::fill_n(static_cast<float*>(data),bytes/4,0.f);return;}
    // Keep the three existing carrier feeds and native reverb/chorus exactly
    // unchanged. Only the UserFX input is replaced,before the original delay
    // DSP,with actual per-channel/per-drum-key send-weighted owner samples.
    std::copy_n(context.commonSums[2].data()+static_cast<size_t>(tap.cursor)*2,bytes/4,static_cast<float*>(data));
    tap.cursor+=count;
}
void CALLBACK freeCommonUserFX(HSYNC,DWORD,DWORD,void* user){
    // bass.h BASS_SYNC_FREE: the child owns DSP/sync removal on free. The
    // control owner serializes child retirement against the main renderer.
    auto& tap=*static_cast<Context::CommonTap*>(user);
    tap.channel=0;tap.dsp=0;tap.freeSync=0;tap.cursor=0;
}
void CALLBACK replacePCM(HDSP,DWORD,void* buffer,DWORD length,void* user){
    auto& context=*static_cast<Context*>(user);
    if(!buffer||length%8)return;
    context.render(static_cast<float*>(buffer),length/8);
}
BOOL CALLBACK filter(DWORD stream,int track,BASS_MIDI_EVENT* event,BOOL seeking,void* user){
    auto& context=*static_cast<Context*>(user);
    std::lock_guard<std::recursive_mutex> guard(context.boundary);
    const auto previousContext=filterContext;const DWORD previousPosition=filterPosition;
    filterContext=&context;filterPosition=event?event->pos:0;
    if(event&&event->event==ScalarMarker){
        const auto found=std::find_if(context.scalars.begin(),context.scalars.end(),[&](const Context::Scalar& row){return row.id==event->param;});
        if(found!=context.scalars.end()){
            BASS_MIDI_EVENT resolved=found->event;resolved.pos=event->pos;resolved.tick=0;
            const bool accepted=resolved.event==MIDI_EVENT_NOTE||fn<Event>(api.event)(stream,resolved.chan,resolved.event,resolved.param)!=FALSE;
            if(!accepted)context.fail(fn<Error>(api.error)());
            else if(!context.clone(resolved,false))context.fail(context.failure.load());
            context.scalars.erase(found);filterContext=previousContext;filterPosition=previousPosition;return FALSE;
        }
    }
    BOOL accepted=context.delegate?context.delegate(stream,track,event,seeking,context.delegateUser):TRUE;
    filterContext=previousContext;filterPosition=previousPosition;
    if(!accepted||!event)return accepted;
    const bool triggerSync=submissionMain==stream?(submissionMode&BASS_MIDI_EVENTS_SYNC)!=0:context.deferredSync;
    if(!context.clone(*event,triggerSync))return FALSE;
    // The controller receives every controller/program/system event, but no
    // NOTE voice exists on it. The accepted owner's NOTE sync is authoritative.
    return event->event==MIDI_EVENT_NOTE?FALSE:TRUE;
}
BOOL WINAPI event(DWORD stream,DWORD channel,DWORD type,DWORD parameter){
    auto context=find(stream);if(!context)return fn<Event>(api.event)(stream,channel,type,parameter);
    MainBarrier barrier(stream,context.get(),1,type,channel);if(!barrier.locked)return FALSE;
    std::lock_guard<std::recursive_mutex> guard(context->boundary);
    barrier.boundaryAcquired();
    if(context->poisoned.load()){facadeError=context->failure.load();return FALSE;}
    float channels=0;
    if(!fn<GetAttribute>(api.getAttribute)(stream,BASS_ATTRIB_MIDI_CHANS,&channels))return FALSE;
    if(channel>=channels){facadeError=BASS_ERROR_ILLPARAM;return FALSE;}
    if(filterContext==context.get()){
        if(type!=MIDI_EVENT_NOTE&&!fn<Event>(api.event)(stream,channel,type,parameter))return FALSE;
        const BASS_MIDI_EVENT resolved{type,parameter,channel,0,filterPosition};
        return context->clone(resolved,false)?TRUE:FALSE;
    }
    if(context->asyncSource){
        if(context->scalars.size()==context->scalars.capacity()){facadeError=BASS_ERROR_BUSY;return FALSE;}
        DWORD id=++context->scalarSerial;if(!id)id=++context->scalarSerial;
        if(std::any_of(context->scalars.begin(),context->scalars.end(),[&](const Context::Scalar& row){return row.id==id;})){
            facadeError=BASS_ERROR_BUSY;return FALSE;}
        context->scalars.push_back({id,{type,parameter,channel,0,0}});
        const BASS_MIDI_EVENT marker{ScalarMarker,id,channel,0,0};
        const DWORD result=fn<Events>(api.events)(stream,BASS_MIDI_EVENTS_STRUCT|BASS_MIDI_EVENTS_FILTER,&marker,1);
        if(result!=1){const int error=fn<Error>(api.error)();
            const auto found=std::find_if(context->scalars.begin(),context->scalars.end(),[&](const Context::Scalar& row){return row.id==id;});
            if(found!=context->scalars.end())context->scalars.erase(found);facadeError=error;return FALSE;}
        return context->poisoned.load()?FALSE:TRUE;
    }
    const Tick nativeBegin=barrier.diagnostic?clockTick():0;
    if(type!=MIDI_EVENT_NOTE&&!fn<Event>(api.event)(stream,channel,type,parameter))return FALSE;
    if(barrier.diagnostic)barrier.nativeTicks=clockTick()-nativeBegin;
    const BASS_MIDI_EVENT resolved{type,parameter,channel,0,static_cast<DWORD>(context->renderPosition)};
    const Tick applyBegin=barrier.diagnostic?clockTick():0;
    if(!context->clone(resolved,false)||!context->applyDue())return FALSE;
    const Tick deliverBegin=barrier.diagnostic?clockTick():0;
    if(barrier.diagnostic)barrier.applyTicks=deliverBegin-applyBegin;
    context->deliver();if(barrier.diagnostic)barrier.deliverTicks=clockTick()-deliverBegin;
    return context->poisoned.load()?FALSE:TRUE;
}
DWORD WINAPI events(DWORD stream,DWORD mode,const void* data,DWORD count){
    auto context=find(stream);if(!context)return fn<Events>(api.events)(stream,mode,data,count);
    MainBarrier barrier(stream,context.get(),2,mode,count);if(!barrier.locked)return DWORD(-1);
    std::lock_guard<std::recursive_mutex> guard(context->boundary);
    barrier.boundaryAcquired();
    if(context->poisoned.load()){facadeError=context->failure.load();return DWORD(-1);}
    // ALWAYS run the final bridge for submitted events, including callers with
    // no project filter. A bridge must never parse/change RAW running status.
    const bool wantedSync=(mode&BASS_MIDI_EVENTS_SYNC)!=0;
    if(wantedSync!=context->deferredSync&&!(mode&BASS_MIDI_EVENTS_CANCEL)){
        for(DWORD attr:{BASS_ATTRIB_MIDI_QUEUED_ASYNC,BASS_ATTRIB_MIDI_QUEUED_BYTE,BASS_ATTRIB_MIDI_QUEUED_TICK}){
            float queued=0;if(!fn<GetAttribute>(api.getAttribute)(stream,attr,&queued))return DWORD(-1);
            if(queued>0){facadeError=BASS_ERROR_BUSY;return DWORD(-1);}}
    }
    const DWORD previousMain=submissionMain,previousMode=submissionMode;
    submissionMain=stream;submissionMode=mode;
    context->deferredSync=(mode&BASS_MIDI_EVENTS_SYNC)!=0;
    const Tick nativeBegin=barrier.diagnostic?clockTick():0;
    const DWORD result=fn<Events>(api.events)(stream,mode|BASS_MIDI_EVENTS_FILTER,data,count);
    if(barrier.diagnostic)barrier.nativeTicks=clockTick()-nativeBegin;
    submissionMain=previousMain;submissionMode=previousMode;
    if(result==DWORD(-1))return result;
    if(mode&BASS_MIDI_EVENTS_CANCEL)context->scalars.clear();
    // Outside the primary filter, synchronous submission is synchronous in
    // sample-voice creation and NOTE observer delivery too; no PCM is pulled.
    if(filterContext!=context.get()&&!context->asyncSource&&!(mode&BASS_MIDI_EVENTS_ASYNC)){
        const Tick applyBegin=barrier.diagnostic?clockTick():0;
        if(!context->applyDue())return DWORD(-1);
        const Tick deliverBegin=barrier.diagnostic?clockTick():0;
        if(barrier.diagnostic)barrier.applyTicks=deliverBegin-applyBegin;
        context->deliver();if(barrier.diagnostic)barrier.deliverTicks=clockTick()-deliverBegin;}
    return context->poisoned.load()?DWORD(-1):result;
}
BOOL WINAPI setFilter(DWORD stream,BOOL seeking,MIDIFILTERPROC* proc,void* user){
    auto context=find(stream);if(!context)return fn<Filter>(api.setFilter)(stream,seeking,proc,user);
    MainBarrier barrier(stream);if(!barrier.locked)return FALSE;
    std::lock_guard<std::recursive_mutex> guard(context->boundary);
    // Seeking-filter installation is independent from the playback bridge.
    if(seeking)return fn<Filter>(api.setFilter)(stream,seeking,proc,user);
    if(!fn<Filter>(api.setFilter)(stream,FALSE,filter,context.get()))return FALSE;
    context->delegate=proc;context->delegateUser=user;return TRUE;
}
BOOL WINAPI fonts(DWORD stream,const void* rows,DWORD count){
    auto context=find(stream);if(!context)return fn<Fonts>(api.setFonts)(stream,rows,count);
    MainBarrier barrier(stream);if(!barrier.locked)return FALSE;
    std::lock_guard<std::recursive_mutex> guard(context->boundary);
    if(!fn<Fonts>(api.setFonts)(stream,rows,count))return FALSE;
    for(DWORD owner:context->owners)if(!fn<Fonts>(api.setFonts)(owner,rows,count)){
        context->fail(fn<Error>(api.error)());return FALSE;}return TRUE;
}
DWORD WINAPI channel(DWORD stream,DWORD number){
    auto context=find(stream);if(!context)return fn<Channel>(api.getChannel)(stream,number);
    MainBarrier barrier(stream);if(!barrier.locked)return 0;
    std::lock_guard<std::recursive_mutex> guard(context->boundary);
    if(number<128)return fn<Channel>(api.getChannel)(context->owners[context->owner(number)],number);
    if(number==BASS_MIDI_CHAN_USERFX)return context->prepareWet();
    facadeError=BASS_ERROR_NOTAVAIL;return 0;
}
BOOL WINAPI setAttribute(DWORD stream,DWORD type,float value){
    auto context=find(stream);if(!context)return fn<SetAttribute>(api.setAttribute)(stream,type,value);
    MainBarrier barrier(stream);if(!barrier.locked)return FALSE;
    std::lock_guard<std::recursive_mutex> guard(context->boundary);
    if(context->poisoned.load()){facadeError=context->failure.load();return FALSE;}
    float previousChannels=0;
    if(type==BASS_ATTRIB_MIDI_CHANS&&!fn<GetAttribute>(api.getAttribute)(stream,type,&previousChannels))return FALSE;
    if(!fn<SetAttribute>(api.setAttribute)(stream,type,value))return FALSE;
    float actualChannels=0;
    if(type==BASS_ATTRIB_MIDI_CHANS&&!fn<GetAttribute>(api.getAttribute)(stream,type,&actualChannels)){
        context->fail(fn<Error>(api.error)());return FALSE;}
    // Only explicit control-side changes set VOICES. Never adjust at Note On
    // or while pulling PCM. The limit is per independent decoder.
    if(type==BASS_ATTRIB_MIDI_CHANS||type==BASS_ATTRIB_MIDI_SRC||type==BASS_ATTRIB_MIDI_CPU||type==BASS_ATTRIB_MIDI_VOICES)
        for(DWORD owner:context->owners){
            float ownerValue=type==BASS_ATTRIB_MIDI_CHANS?actualChannels:value;
            // bassmidi.md: CPU=0 is automatic100 only in BASS update threads;
            // independent workers must retain that shared-output policy.
            if(type==BASS_ATTRIB_MIDI_CPU&&value==0&&!(fn<ChannelFlags>(api.channelFlags)(stream,0,0)&BASS_STREAM_DECODE))ownerValue=100;
            if(!fn<SetAttribute>(api.setAttribute)(owner,type,ownerValue)){
                context->fail(fn<Error>(api.error)());return FALSE;}}
    if(type==BASS_ATTRIB_MIDI_REVERB){
        // The attribute is an overall native FX gain, not a channel send.
        // Previously it reached the silent controller only in parallel mode.
        if(context->commonCarrier){
            if(!fn<SetAttribute>(api.setAttribute)(context->commonCarrier,type,value)){
                context->fail(fn<Error>(api.error)());return FALSE;}
        }
        if(!context->commonCarrier||context->commonMask!=7u)for(DWORD ownerHandle:context->owners)if(!fn<SetAttribute>(api.setAttribute)(ownerHandle,type,value)){
            context->fail(fn<Error>(api.error)());return FALSE;}
    }
    if(context->commonCarrier&&type==BASS_ATTRIB_MIDI_SRC&&
       !fn<SetAttribute>(api.setAttribute)(context->commonCarrier,type,value)){
        context->fail(fn<Error>(api.error)());return FALSE;}
    if(type==BASS_ATTRIB_MIDI_CHANS&&actualChannels>previousChannels){
        // bassmidi.md CHANS promises new channels are melodic. Installed2.4.16
        // growth can expose uninitialized DRUMS bytes; establish that exact
        // documented baseline only for new channels before actor DEFDRUMS.
        // Native StreamEvent emits neither filter nor SYNC. On an ASYNC main
        // its initialization remains ahead of subsequent submitted events.
        for(unsigned channel=static_cast<unsigned>(previousChannels);channel<static_cast<unsigned>(actualChannels)&&channel<128;++channel){
            context->drums[channel]=context->defaultDrums[channel]=false;
            if(!fn<Event>(api.event)(stream,channel,MIDI_EVENT_DEFDRUMS,0)){
                context->fail(fn<Error>(api.error)());return FALSE;}
            for(DWORD owner:context->owners)if(!fn<Event>(api.event)(owner,channel,MIDI_EVENT_DEFDRUMS,0)){
                context->fail(fn<Error>(api.error)());return FALSE;}
        }
    }
    return TRUE;
}
BOOL WINAPI getAttribute(DWORD stream,DWORD type,float* result){
    auto context=find(stream);if(!context)return fn<GetAttribute>(api.getAttribute)(stream,type,result);
    if(type!=BASS_ATTRIB_MIDI_VOICES_ACTIVE)return fn<GetAttribute>(api.getAttribute)(stream,type,result);
    if(!result){facadeError=BASS_ERROR_ILLPARAM;return FALSE;}
    MainBarrier barrier(stream);if(!barrier.locked)return FALSE;
    std::lock_guard<std::recursive_mutex> guard(context->boundary);float total=0;
    for(DWORD owner:context->owners){float current=0;if(!fn<GetAttribute>(api.getAttribute)(owner,type,&current))return FALSE;total+=current;}
    *result=total;return TRUE;
}
void CALLBACK sync(HSYNC handle,DWORD channelHandle,DWORD data,void* user){
    auto& row=*static_cast<SyncRow*>(user);auto& context=*row.context;
    for(unsigned i=0;i<OwnerCount;++i)if(context.owners[i]==channelHandle){
        auto& rows=context.notifications[i];
        if(rows.size()==rows.capacity()){context.fail(BASS_ERROR_BUSY);return;}
        rows.push_back({row.self.lock(),handle,data,fn<GetPosition>(api.getPosition)(channelHandle,BASS_POS_BYTE|BASS_POS_DECODE),
            fn<GetPosition>(api.getPosition)(channelHandle,BASS_POS_BYTE|BASS_POS_DSP),context.applyingOrder});return;}
}
HSYNC WINAPI setSync(DWORD stream,DWORD type,QWORD parameter,SYNCPROC* proc,void* user){
    auto context=find(stream);
    if(!context||(type&0xffffffu)!=BASS_SYNC_MIDI_EVENT||parameter!=MIDI_EVENT_NOTE)
        return fn<SetSync>(api.setSync)(stream,type,parameter,proc,user);
    if(!proc){facadeError=BASS_ERROR_ILLPARAM;return 0;}
    if(type&BASS_SYNC_THREAD){facadeError=BASS_ERROR_NOTAVAIL;return 0;}
    MainBarrier barrier(stream);if(!barrier.locked)return 0;
    std::lock_guard<std::recursive_mutex> guard(context->boundary);
    auto row=std::make_shared<SyncRow>();row->self=row;row->context=context.get();row->callback=proc;row->user=user;row->type=type;row->parameter=parameter;row->order=++context->syncOrder;
    for(unsigned i=0;i<OwnerCount;++i){row->handles[i]=fn<SetSync>(api.setSync)(context->owners[i],type&~BASS_SYNC_ONETIME,parameter,sync,row.get());
        if(!row->handles[i]){for(unsigned j=0;j<i;++j)fn<RemoveSync>(api.removeSync)(context->owners[j],row->handles[j]);return 0;}}
    const HSYNC result=row->handles[0];context->syncs.push_back(std::move(row));return result;
}
BOOL WINAPI removeSync(DWORD stream,HSYNC handle){
    auto context=find(stream);if(!context)return fn<RemoveSync>(api.removeSync)(stream,handle);
    MainBarrier barrier(stream);if(!barrier.locked)return FALSE;
    std::lock_guard<std::recursive_mutex> guard(context->boundary);
    for(auto it=context->syncs.begin();it!=context->syncs.end();++it)if((*it)->handles[0]==handle){
        bool result=true;for(unsigned i=0;i<OwnerCount;++i)if((*it)->handles[i]){
            if(fn<RemoveSync>(api.removeSync)(context->owners[i],(*it)->handles[i]))(*it)->handles[i]=0;else result=false;}
        if(result){auto* retired=it->get();
            retired->active=false;
            for(auto& pending:context->notifications)for(auto& notification:pending)
                if(notification.row.get()==retired)notification.row.reset();
            context->syncs.erase(it);}return result?TRUE:FALSE;}
    return fn<RemoveSync>(api.removeSync)(stream,handle);
}
int WINAPI error(){if(facadeError){const int code=facadeError;facadeError=0;return code;}return fn<Error>(api.error)();}
QWORD WINAPI getPosition(DWORD stream,DWORD mode){
    if(stream==notificationMain){if(mode==(BASS_POS_BYTE|BASS_POS_DECODE))return notificationDecode;
        if(mode==(BASS_POS_BYTE|BASS_POS_DSP))return notificationDSP;}
    return fn<GetPosition>(api.getPosition)(stream,mode);
}
BOOL WINAPI streamFree(DWORD stream){
    std::shared_ptr<Context> context;
    {std::lock_guard<std::mutex> guard(registryLock);for(const auto& item:registry)
        if(item.second->wetStream==stream){context=item.second;break;}}
    if(!context)return fn<StreamFree>(api.streamFree)(stream);
    MainBarrier barrier(context->main);if(!barrier.locked)return FALSE;
    std::lock_guard<std::recursive_mutex> guard(context->boundary);
    return context->retireWet()?TRUE:FALSE;
}
// Small immutable memory SF2 used solely to initialize the private carrier.
// BASS_FILEPROCS callbacks are native memory reads, not filesystem/Python/GIL.
struct CarrierFont {
    std::vector<unsigned char> data;std::atomic<unsigned> cursor{0};
    std::atomic<QWORD> reads{0};DWORD handle=0;FontFree free=nullptr;
    BASS_FILEPROCS procs{};
};
void CALLBACK carrierFileClose(void*){}
QWORD CALLBACK carrierFileLength(void* user){return static_cast<CarrierFont*>(user)->data.size();}
DWORD CALLBACK carrierFileRead(void* output,DWORD requested,void* user){
    auto& file=*static_cast<CarrierFont*>(user);if(!output)return 0;
    unsigned cursor=file.cursor.load(std::memory_order_relaxed),count=0;
    do{if(cursor>=file.data.size())return 0;
        count=std::min(requested,static_cast<DWORD>(file.data.size()-cursor));
    }while(!file.cursor.compare_exchange_weak(cursor,cursor+count,std::memory_order_relaxed));
    std::memcpy(output,file.data.data()+cursor,count);file.reads.fetch_add(1,std::memory_order_relaxed);return count;
}
BOOL CALLBACK carrierFileSeek(QWORD position,void* user){
    auto& file=*static_cast<CarrierFont*>(user);if(position>file.data.size())return FALSE;
    file.cursor.store(static_cast<unsigned>(position),std::memory_order_relaxed);return TRUE;
}
}
#define API extern "C" __declspec(dllexport)
API unsigned __cdecl multicore_synth_common_version(){return 2;}
API void* __cdecl multicore_carrier_font_create(const void* bytes,unsigned length,void* initialize,void* load,void* release,unsigned* handle,unsigned* loaded){
    if(!bytes||!length||length>65536||!initialize||!load||!release||!handle||!loaded)return nullptr;
    *handle=*loaded=0;
    try{
        auto file=std::make_unique<CarrierFont>();
        const auto* source=static_cast<const unsigned char*>(bytes);file->data.assign(source,source+length);
        file->free=fn<FontFree>(release);file->procs={carrierFileClose,carrierFileLength,carrierFileRead,carrierFileSeek};
        file->handle=fn<FontInitUser>(initialize)(&file->procs,file.get(),BASS_MIDI_FONT_NOFX);
        if(!file->handle)return nullptr;
        *handle=file->handle;*loaded=fn<FontLoad>(load)(file->handle,0,0)?1u:0u;
        // Adopt even a failed preload so the caller can retain/retry FontFree.
        return file.release();
    }catch(...){return nullptr;}
}
API int __cdecl multicore_carrier_font_destroy(void* pointer){
    auto* file=static_cast<CarrierFont*>(pointer);if(!file)return 1;
    if(file->handle&&!file->free(file->handle))return 0;delete file;return 1;
}
API QWORD __cdecl multicore_carrier_font_reads(void* pointer){
    return pointer?static_cast<CarrierFont*>(pointer)->reads.load(std::memory_order_relaxed):0;
}
API unsigned __cdecl multicore_synth_version(){return 2;}
API unsigned __cdecl multicore_synth_layout(){return OwnerCount|(OwnerCount<<16);}
API unsigned __cdecl multicore_synth_policy(){return 1;} // Per-decoder cap; no population/quota compensation.
API QWORD __cdecl multicore_synth_resolve_position(QWORD anchor,DWORD position){return resolvePosition(anchor,position);}
API void* __cdecl multicore_synth_create(DWORD main,const DWORD* handles,unsigned count,unsigned maximum,unsigned rate,const Functions* functions){
    if(!main||!handles||count!=OwnerCount||!maximum||maximum>1048576||!rate||!functions)return nullptr;
    const auto* addresses=reinterpret_cast<void* const*>(functions);for(unsigned i=0;i<20;++i)if(!addresses[i])return nullptr;
    for(unsigned i=0;i<count;++i){if(!handles[i]||handles[i]==main)return nullptr;for(unsigned j=0;j<i;++j)if(handles[i]==handles[j])return nullptr;}
    try{
        {std::lock_guard<std::mutex> guard(registryLock);if(registry.count(main))return nullptr;api=*functions;}
        auto context=std::make_shared<Context>(main,handles,maximum,rate);context->start();
        if(!fn<Filter>(api.setFilter)(main,FALSE,filter,context.get()))return nullptr;
        // PlaybackTiming's INT_MAX begin tap must precede replacement synthesis
        // so its whole render/wait contribution is included in DSP lateness.
        context->dsp=fn<SetDSP>(api.setDSP)(main,replacePCM,context.get(),2147483646);
        if(!context->dsp){fn<Filter>(api.setFilter)(main,FALSE,nullptr,nullptr);return nullptr;}
        std::lock_guard<std::mutex> guard(registryLock);registry.emplace(main,context);return context.get();
    }catch(...){return nullptr;}
}
API int __cdecl multicore_synth_destroy(void* pointer){
    if(!pointer)return 1;auto* context=static_cast<Context*>(pointer);std::shared_ptr<Context> retained;
    {std::lock_guard<std::mutex> guard(registryLock);const auto it=registry.find(context->main);
        if(it==registry.end()||it->second.get()!=pointer)return 0;retained=it->second;}
    // Caller has stopped output, input and all actor owners first. Failed
    // removal retains registry and borrowed stream ownership for explicit retry.
    std::lock_guard<std::recursive_mutex> guard(context->boundary);
    if(context->dsp&&!fn<RemoveDSP>(api.removeDSP)(context->main,context->dsp))return 0;context->dsp=0;
    if(!fn<Filter>(api.setFilter)(context->main,FALSE,nullptr,nullptr))return 0;
    if(!context->retireWet())return 0;
    if(!context->retireCommon())return 0;
    for(auto& row:context->syncs)for(unsigned i=0;i<OwnerCount;++i)if(row->handles[i]){
        if(!fn<RemoveSync>(api.removeSync)(context->owners[i],row->handles[i]))return 0;row->handles[i]=0;}
    context->syncs.clear();context->stop();
    {std::lock_guard<std::mutex> lock(registryLock);registry.erase(context->main);}return 1;
}
API int __cdecl multicore_synth_common_effects(void* pointer,DWORD carrier,unsigned mask){
    if(!pointer)return 0;auto* context=static_cast<Context*>(pointer);
    MainBarrier barrier(context->main);if(!barrier.locked)return 0;
    std::lock_guard<std::recursive_mutex> guard(context->boundary);
    try{return context->prepareCommon(carrier,mask)?1:0;}catch(...){context->fail(BASS_ERROR_MEM);return 0;}
}
API unsigned __cdecl multicore_synth_common_carrier(void* pointer){
    return pointer?static_cast<Context*>(pointer)->commonCarrier:0;
}
API int __cdecl multicore_synth_health(void* pointer,unsigned* output){
    if(!pointer||!output)return 0;auto& context=*static_cast<Context*>(pointer);
    output[0]=context.poisoned.load()?1u:0u;output[1]=static_cast<unsigned>(context.failure.load());output[2]=OwnerCount;
    return 1;
}
API int __cdecl multicore_synth_dispatch_statistics(void* pointer,unsigned long long* output){
    if(!pointer||!output)return 0;auto& context=*static_cast<Context*>(pointer);
    output[0]=context.serialSpans.load(std::memory_order_relaxed);output[1]=context.parallelSpans.load(std::memory_order_relaxed);
    output[2]=context.serialFrames.load(std::memory_order_relaxed);output[3]=context.parallelFrames.load(std::memory_order_relaxed);
    output[4]=context.dispatchMinimum();return 1;
}
// Diagnostics ABI1 reserves four per-owner groups in its fixed 39-word
// layout. Keep those legacy slots bounded even though synthesis now has16 owners.
API unsigned __cdecl multicore_synth_diagnostics_version(){return 1;}
API int __cdecl multicore_synth_diagnostics_enable(void* pointer,int enabled){
    if(!pointer)return 0;static_cast<Context*>(pointer)->diagnosticEnabled.store(enabled!=0,std::memory_order_release);return 1;
}
API Tick __cdecl multicore_synth_diagnostics_clock_cost(unsigned count){
    if(!count||count>10000000)return 0;const Tick begin=clockTick();Tick last=begin;
    for(unsigned i=0;i<count;++i)last=clockTick();return last-begin;
}
API int __cdecl multicore_synth_diagnostics_statistics(void* pointer,Tick* output){
    if(!pointer||!output)return 0;auto& c=*static_cast<Context*>(pointer);
    output[0]=c.diagnosticFrequency;output[1]=c.diagnosticEnabled.load()?1:0;output[2]=c.diagnosticPublished.load();output[3]=c.diagnosticSpans.load();
    for(unsigned i=0;i<4;++i){output[4+i]=c.pullLast[i].load();output[8+i]=c.pullMaximum[i].load();
        output[12+i]=c.pullLastBegin[i].load();output[16+i]=c.pullLastEnd[i].load();}
    output[20]=c.lastDispatch.load();output[21]=c.lastMainEnd.load();output[22]=c.lastWorkerBegin.load();output[23]=c.lastJoin.load();
    output[24]=c.maximumJoin.load();output[25]=c.eventLastWait.load();output[26]=c.eventMaximumWait.load();
    output[27]=c.eventLastHold.load();output[28]=c.eventMaximumHold.load();output[29]=c.eventCalls.load();
    output[30]=c.maximumBlock.load();output[31]=c.lastBlock.load();output[32]=c.eventWaitSum.load();output[33]=c.eventHoldSum.load();
    output[34]=c.lockPublished.load();output[35]=c.lockDropped.load();output[36]=c.lockLastWait.load();
    output[37]=c.lockMaximumWait.load();output[38]=c.lockWaitSum.load();return 1;
}
API unsigned __cdecl multicore_synth_diagnostics_snapshot(void* pointer,Tick* output,unsigned capacity,Tick* metadata,int locks){
    if(!pointer||!output||!metadata||!capacity)return 0;auto& c=*static_cast<Context*>(pointer);
    if(locks<0||locks>3)return 0;
    const unsigned words=locks==3?24u:locks==2?20u:locks==1?8u:Context::DiagnosticWords;
    const Tick total=locks==3?c.reservePublished.load(std::memory_order_acquire):locks==2?c.sourcePublished.load(std::memory_order_acquire):locks==1?c.lockPublished.load(std::memory_order_acquire):c.diagnosticPublished.load(std::memory_order_acquire);
    const Tick available=std::min<Tick>(total,std::min(capacity,Context::DiagnosticCapacity));unsigned copied=0;
    for(Tick sequence=total-available+1;sequence<=total;++sequence){
        auto& row=locks==3?c.diagnosticReserves[(sequence-1)%Context::DiagnosticCapacity]:locks==2?c.diagnosticSources[(sequence-1)%Context::DiagnosticCapacity]:locks==1?c.diagnosticLocks[(sequence-1)%Context::DiagnosticCapacity]:c.diagnosticRows[(sequence-1)%Context::DiagnosticCapacity];
        for(unsigned attempt=0;attempt<3;++attempt){
            const Tick before=row.generation.load(std::memory_order_acquire);if(before!=sequence*2)break;
            for(unsigned i=0;i<words;++i)output[static_cast<size_t>(copied)*words+i]=row.values[i].load(std::memory_order_relaxed);
            std::atomic_thread_fence(std::memory_order_acquire);
            if(row.generation.load(std::memory_order_relaxed)==before){++copied;break;}
        }
    }
    metadata[0]=1;metadata[1]=words;metadata[2]=c.diagnosticFrequency;metadata[3]=total;metadata[4]=copied;
    metadata[5]=c.diagnosticEnabled.load()?1:0;metadata[6]=locks==3?c.reserveDropped.load():locks==2?c.sourceDropped.load():locks==1?c.lockDropped.load():0;metadata[7]=Context::DiagnosticCapacity;return copied;
}
#define PROC(name,function) API void* __cdecl multicore_synth_##name##_proc(){return reinterpret_cast<void*>(&function);}
PROC(event,event) PROC(events,events) PROC(filter,setFilter) PROC(fonts,fonts) PROC(channel,channel)
PROC(set_attribute,setAttribute) PROC(get_attribute,getAttribute) PROC(set_sync,setSync) PROC(remove_sync,removeSync) PROC(error,error)
PROC(get_position,getPosition)
PROC(lock,channelLock)
PROC(stream_free,streamFree)
