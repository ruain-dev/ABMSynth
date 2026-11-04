// ABMSynth custom post-mix DSP. No Python/GIL, allocations, disk or UI calls
// occur inside normal processing exports. Independent stream owners manage
// rack, stereo, legacy delay, limiter and read-only observation lifetimes.
// Float interleaved stereo; BASS DSPPROC ABI from the installed bass.h.
#define NOMINMAX
#include <windows.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <new>
#include <complex>
#include <memory>
#include <vector>
#include <tuple>
#include <utility>
#include <xapo.h>
#include <hrtfapoapi.h>
#include <emmintrin.h> // SSE2 is guaranteed by Windows x64; no AVX requirement.
#include <numeric>
#include <cstdint>
#include <cstring>
#include <cwchar>
#include <process.h>
#include <avrt.h>
#include <intrin.h>
#include "../../bassmidi.h" // installed MIDIFILTERPROC/BASS_MIDI_EVENT ABI
#include "preload_queue_api.h"
#include "midi_recording.h"
#include "spatial_rate.h"
#include "midi_humanization.h"

// Channel audio FX may share a BASSMIDI dry child with the original delay.
// Selection/reset must retire the delay DSP without destroying that borrower.
static bool channelFXDelayRelease(unsigned root,unsigned channel,unsigned mix);
static void channelFXDelayAcquire(unsigned root,unsigned channel,unsigned mix);

// The Python event filter always passes these three types through unchanged.
// Raw velocity/overlap/activity handling happens before BASS decodes the packet.
// No state/allocation/lock/GIL on this path; all other events still delegate,
// including unknown future events, mapping, Bank/Program, pressure and resets.
// INPUT SIDE ONLY. StreamEvents preserves RAW decoder state across calls,
// whereas ConvertEvents has no context parameter. Seed only conversion input;
// original BASS bytes/flags are never changed by this owner. Conversion state
// spans frames, modes and RPN selections using the bassmidi.h WINAPI ABI.
struct NativeRawDecodeContext {
    struct State {unsigned status=0,port=0,selectorUncertain=0;std::array<unsigned,16> selector;bool valid;
        explicit State(bool fresh=true):valid(fresh){selector.fill(0x3fffu);}};
    SRWLOCK lock=SRWLOCK_INIT;std::atomic<unsigned> references{1};unsigned stream;
    unsigned long long sequence=0;State state;DWORD(WINAPI* flagsSource)(DWORD,DWORD,DWORD)=nullptr;
    NativeRawDecodeContext(unsigned handle,bool fresh):stream(handle),state(fresh){}
    void retain(){references.fetch_add(1,std::memory_order_relaxed);}
    void release(){if(references.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
};
struct NativeRawDecodePlan {
    NativeRawDecodeContext* owner;unsigned mode;unsigned long long sequence;
    NativeRawDecodeContext::State next;bool qualified=false,selectionQualified=false,finished=false,submitted=false,successful=false;
    unsigned selections=0;int error=0;std::vector<unsigned char> original;std::vector<BASS_MIDI_EVENT> events,selectionEvents;
    NativeRawDecodePlan(NativeRawDecodeContext* context,unsigned flags,unsigned long long serial,const NativeRawDecodeContext::State& state):owner(context),mode(flags),sequence(serial),next(state){owner->retain();}
    ~NativeRawDecodePlan(){owner->release();}
};
static bool rawDecodeDeferredReset(const NativeRawDecodeContext& context,unsigned mode);
static bool rawDecodeAdvance(NativeRawDecodeContext::State& state,const unsigned char* bytes,unsigned length,unsigned mode,const NativeRawDecodeContext* policy=nullptr){
    const bool timed=(mode&(BASS_MIDI_EVENTS_TIME|BASS_MIDI_EVENTS_ABSTIME))!=0;
    if(mode&BASS_MIDI_EVENTS_NORSTATUS)state.status=0;
    unsigned at=0;
    auto variable=[&](unsigned& value){value=0;for(unsigned i=0;i<4;++i){if(at==length)return false;const unsigned byte=bytes[at++];value=(value<<7)|(byte&127u);if(!(byte&128u))return true;}return false;};
    while(at<length){
        if(timed){unsigned delta=0;if(!variable(delta)||at==length)return false;}
        unsigned byte=bytes[at++];
        if(byte==0xffu){
            if(at==length)return false;const unsigned type=bytes[at++];unsigned count=0;
            if(!variable(count)||count>length-at)return false;
            if(type==0x21u){if(count!=1)return false;if(bytes[at]>7)return false;state.port=bytes[at];}
            at+=count;continue; // BASS keeps status across a complete meta.
        }
        if(byte>=0xf8u)continue; // Realtime, including undefined F9/FD.
        if(byte>=0xf0u){
            state.status=0;
            if(byte==0xf0u){while(at<length&&bytes[at]!=0xf7u){if(bytes[at]>=128u)return false;++at;}if(at==length)return false;++at;}
            else{const unsigned count=byte==0xf1u||byte==0xf3u?1u:byte==0xf2u?2u:0u;
                if(count>length-at)return false;for(unsigned i=0;i<count;++i)if(bytes[at+i]>=128u)return false;at+=count;}
            continue;
        }
        unsigned values[2]{},dataCount=0;
        if(byte>=0x80u)state.status=byte;
        else{
            if(!state.status||(mode&BASS_MIDI_EVENTS_NORSTATUS))return true; // BASS stops this original orphan-data frame.
            values[0]=byte;dataCount=1;
        }
        const unsigned status=state.status,command=status&0xf0u;
        const unsigned count=command==0xc0u||command==0xd0u?1u:2u;
        while(dataCount<count&&at<length){const unsigned value=bytes[at];
            // Installed BASS consumes high-bit channel payload, including
            // F8, as data rather than interleaved realtime. Do not invent a
            // selector by skipping it: preserve an unknown context until the
            // complete malformed-value domain is independently qualified.
            if(value>=128u)return false;values[dataCount++]=value;++at;}
        // BASS pads a truncated channel message at this original boundary;
        // never carry missing payload bytes into the next call.
        if(command==0xb0u){auto& selected=state.selector[status&15u];
            if(values[0]==101u||values[0]==99u)selected=(selected&127u)|(values[1]<<7)|(values[0]==99u?0x4000u:0u);
            else if(values[0]==100u||values[0]==98u)selected=(selected&0x3f80u)|values[1]|(values[0]==98u?0x4000u:0u);
            else if(values[0]==121u){selected=0x3fffu;
                // Decode nulls now, then the queued RESET17 can null again
                // after a later selector reseed. No undocumented query can
                // prove its completion; uncertainty remains sticky.
                const unsigned actualChannel=(mode&0xffffu)?(mode&0xffffu)-1:state.port*16+(status&15u);
                if(actualChannel<16&&(!policy||rawDecodeDeferredReset(*policy,mode)))state.selectorUncertain|=1u<<actualChannel;}
        }
        if(mode&BASS_MIDI_EVENTS_NORSTATUS)state.status=0;
    }
    return true;
}
extern "C" __declspec(dllexport) unsigned __cdecl raw_decode_context_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl raw_decode_context_selection_version(){return 2;}
extern "C" __declspec(dllexport) int __cdecl raw_decode_context_flags_source(void* pointer,void* flags){
    if(!pointer||!flags)return 0;auto& context=*static_cast<NativeRawDecodeContext*>(pointer);
    AcquireSRWLockExclusive(&context.lock);const bool ready=context.sequence==0&&context.references.load()==1;
    if(ready)context.flagsSource=reinterpret_cast<DWORD(WINAPI*)(DWORD,DWORD,DWORD)>(flags);
    ReleaseSRWLockExclusive(&context.lock);return ready?1:0;
}
static bool rawDecodeDeferredReset(const NativeRawDecodeContext& context,unsigned mode){
    // FLUSH overrides ASYNC, not a TIME/ABSTIME future schedule. This
    // priority is documented and checked against all24 original combinations.
    if(mode&(BASS_MIDI_EVENTS_TIME|BASS_MIDI_EVENTS_ABSTIME))return true;
    if(mode&BASS_MIDI_EVENTS_FLUSH)return false;
    if(mode&BASS_MIDI_EVENTS_ASYNC)return true;
    if(!context.flagsSource)return true; // Missing policy is unknown, never guessed synchronous.
    const DWORD flags=context.flagsSource(context.stream,0,0);return flags==UINT_MAX||(flags&BASS_MIDI_ASYNC)!=0;
}
extern "C" __declspec(dllexport) void* __cdecl raw_decode_context_create(unsigned stream,unsigned fresh){
    if(!stream||fresh>1)return nullptr;try{return new NativeRawDecodeContext(stream,fresh!=0);}catch(const std::bad_alloc&){return nullptr;}}
extern "C" __declspec(dllexport) int __cdecl raw_decode_context_close(void* pointer){
    if(!pointer)return 1;auto* context=static_cast<NativeRawDecodeContext*>(pointer);if(context->references.load()!=1)return 0;context->release();return 1;}
extern "C" __declspec(dllexport) int __cdecl raw_decode_context_read(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out||capacity<21)return 0;auto& context=*static_cast<NativeRawDecodeContext*>(pointer);AcquireSRWLockShared(&context.lock);
    out[0]=context.sequence;out[1]=context.state.valid?1:0;out[2]=context.state.status;out[3]=context.state.port;out[4]=context.references.load();
    for(unsigned i=0;i<16;++i)out[5+i]=context.state.selector[i];ReleaseSRWLockShared(&context.lock);return 21;}
extern "C" __declspec(dllexport) int __cdecl raw_decode_context_read2(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out||capacity<23)return 0;auto& context=*static_cast<NativeRawDecodeContext*>(pointer);AcquireSRWLockShared(&context.lock);
    out[0]=context.sequence;out[1]=context.state.valid?1:0;out[2]=context.state.status;out[3]=context.state.port;out[4]=context.references.load();
    for(unsigned i=0;i<16;++i)out[5+i]=context.state.selector[i];out[21]=context.state.selectorUncertain;out[22]=context.flagsSource?1:0;
    ReleaseSRWLockShared(&context.lock);return 23;
}
extern "C" __declspec(dllexport) int __cdecl raw_decode_context_observe(void* pointer,unsigned stream,unsigned mode,const unsigned char* bytes,unsigned length,unsigned successful,int error){
    // Actual submission completion ONLY. Known ordinary RAW uses this stack
    // grammar advance, not64 selector seeds or ConvertEvents allocations.
    if(!pointer||stream!=static_cast<NativeRawDecodeContext*>(pointer)->stream||!(mode&BASS_MIDI_EVENTS_RAW)||successful>1||(length&&!bytes))return 0;
    auto& context=*static_cast<NativeRawDecodeContext*>(pointer);AcquireSRWLockExclusive(&context.lock);
    ++context.sequence;if(!successful)context.state.valid=false;
    else if(context.state.valid&&!rawDecodeAdvance(context.state,bytes,length,mode,&context))context.state.valid=false;
    ReleaseSRWLockExclusive(&context.lock);(void)error;return 1;
}
extern "C" __declspec(dllexport) int __cdecl raw_decode_context_event(void* pointer,unsigned stream,unsigned event,unsigned channel,unsigned param,unsigned successful,int error){
    // Actual non-RAW result ONLY. RESET17 clears the decoder selector at the
    // first16 native channels, NOT channel modulo16. Installed BASS tests
    // show native channel0 clears raw-status B0 even at persistent port1,
    // while native channel16 does not. Running status and port survive.
    if(!pointer||stream!=static_cast<NativeRawDecodeContext*>(pointer)->stream||successful>1)return 0;
    if(event!=MIDI_EVENT_RESET||!successful)return 1;
    auto& context=*static_cast<NativeRawDecodeContext*>(pointer);AcquireSRWLockExclusive(&context.lock);
    ++context.sequence;if(channel<16)context.state.selector[channel]=0x3fffu;
    ReleaseSRWLockExclusive(&context.lock);(void)param;(void)error;return 1;
}
extern "C" __declspec(dllexport) int __cdecl raw_decode_context_pending_reset(void* pointer,unsigned stream,unsigned channel,unsigned successful,int error){
    if(!pointer||stream!=static_cast<NativeRawDecodeContext*>(pointer)->stream||successful>1)return 0;
    if(!successful||channel>=16)return 1;auto& context=*static_cast<NativeRawDecodeContext*>(pointer);AcquireSRWLockExclusive(&context.lock);
    ++context.sequence;context.state.selectorUncertain|=1u<<channel;ReleaseSRWLockExclusive(&context.lock);(void)error;return 1;
}
extern "C" __declspec(dllexport) int __cdecl raw_decode_context_invalidate(void* pointer,unsigned stream,int error){
    // A failed bulk STRUCT call may have an accepted prefix. Do not invent
    // its extent, or counterfeit an actual RAW0 submission to invalidate it.
    if(!pointer||stream!=static_cast<NativeRawDecodeContext*>(pointer)->stream)return 0;
    auto& context=*static_cast<NativeRawDecodeContext*>(pointer);AcquireSRWLockExclusive(&context.lock);
    ++context.sequence;context.state.valid=false;ReleaseSRWLockExclusive(&context.lock);(void)error;return 1;
}
extern "C" __declspec(dllexport) void* __cdecl raw_decode_plan_prepare(void* pointer,unsigned mode,const unsigned char* bytes,unsigned length,void* conversion,void* failure,int* out){
    if(!pointer||!conversion||!failure||!out||(length&&!bytes)||!(mode&BASS_MIDI_EVENTS_RAW))return nullptr;std::fill_n(out,3,0);
    auto& context=*static_cast<NativeRawDecodeContext*>(pointer);NativeRawDecodeContext::State state(false);unsigned long long sequence=0;
    AcquireSRWLockShared(&context.lock);state=context.state;sequence=context.sequence;ReleaseSRWLockShared(&context.lock);
    auto convert=reinterpret_cast<DWORD(WINAPI*)(const BYTE*,DWORD,BASS_MIDI_EVENT*,DWORD,DWORD)>(conversion);
    auto error=reinterpret_cast<int(WINAPI*)()>(failure);
    try{
        auto plan=std::make_unique<NativeRawDecodePlan>(&context,mode,sequence,state);if(length)plan->original.assign(bytes,bytes+length);
        plan->selectionQualified=state.valid&&rawDecodeAdvance(plan->next,bytes,length,mode,&context);
        if(!plan->selectionQualified){plan->next.valid=false;out[0]=1;return plan.release();}
        plan->qualified=(state.selectorUncertain|plan->next.selectorUncertain)==0;
        const bool timed=(mode&(BASS_MIDI_EVENTS_TIME|BASS_MIDI_EVENTS_ABSTIME))!=0;
        const unsigned flags=(mode&BASS_MIDI_EVENTS_NORSTATUS)|(timed?BASS_MIDI_EVENTS_TIME:0u);
        std::vector<unsigned char> prefix;
        auto message=[&](const unsigned char* value,unsigned count){if(timed)prefix.push_back(0);prefix.insert(prefix.end(),value,value+count);};
        const unsigned char port0[]{0xff,0x21,1,0};message(port0,4);
        for(unsigned channel=0;channel<16;++channel){const unsigned selected=(state.selectorUncertain&(1u<<channel))?0x3fffu:state.selector[channel];const bool nrpn=(selected&0x4000u)!=0;
            const unsigned char high[]{static_cast<unsigned char>(0xb0u|channel),static_cast<unsigned char>(nrpn?99:101),static_cast<unsigned char>((selected>>7)&127u)};
            const unsigned char low[]{static_cast<unsigned char>(0xb0u|channel),static_cast<unsigned char>(nrpn?98:100),static_cast<unsigned char>(selected&127u)};message(high,3);message(low,3);}
        const unsigned char port[]{0xff,0x21,1,static_cast<unsigned char>(state.port)};message(port,4);
        if(state.status&&!(flags&BASS_MIDI_EVENTS_NORSTATUS)){const unsigned char dummy[]{static_cast<unsigned char>(state.status),0,0};message(dummy,(state.status&0xf0u)==0xc0u||(state.status&0xf0u)==0xd0u?2u:3u);}
        else{const unsigned char clear[]{0xf6};message(clear,1);} // Selector seeds must not invent a last running status.
        if(length>UINT_MAX-prefix.size()){out[1]=-2;return nullptr;}
        const DWORD before=convert(prefix.data(),static_cast<DWORD>(prefix.size()),nullptr,0,flags);
        if(before==UINT_MAX){out[1]=error();return nullptr;}
        std::vector<unsigned char> input=prefix;if(length)input.insert(input.end(),bytes,bytes+length);
        const DWORD count=convert(input.data(),static_cast<DWORD>(input.size()),nullptr,0,flags);
        if(count==UINT_MAX){out[1]=error();return nullptr;}if(count<before){out[1]=-3;return nullptr;}
        std::vector<BASS_MIDI_EVENT> decoded(count);
        if(convert(input.data(),static_cast<DWORD>(input.size()),decoded.data(),count,flags)!=count){out[1]=error();return nullptr;}
        plan->events.assign(decoded.begin()+before,decoded.end());
        for(auto& event:plan->events){if(mode&0xffffu)event.chan=(mode&0xffffu)-1;
            if(event.event==MIDI_EVENT_PROGRAM||event.event==MIDI_EVENT_BANK||event.event==MIDI_EVENT_BANK_LSB||event.event==MIDI_EVENT_DRUMS){++plan->selections;plan->selectionEvents.push_back(event);}}
        if(!plan->qualified)plan->events.clear(); // Never expose partial conversion as full events.
        out[0]=1;out[2]=1;return plan.release();
    }catch(const std::bad_alloc&){out[1]=-2;return nullptr;}
}
extern "C" __declspec(dllexport) int __cdecl raw_decode_plan_read(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out||capacity<11)return 0;const auto& plan=*static_cast<NativeRawDecodePlan*>(pointer);
    AcquireSRWLockShared(&plan.owner->lock);
    const unsigned long long values[]{plan.sequence,plan.qualified?1ull:0ull,plan.events.size(),plan.selections,plan.next.status,plan.next.port,
        static_cast<unsigned>(plan.error),plan.finished?1ull:0ull,plan.submitted?1ull:0ull,plan.successful?1ull:0ull,plan.mode};std::copy_n(values,11,out);ReleaseSRWLockShared(&plan.owner->lock);return 11;}
extern "C" __declspec(dllexport) int __cdecl raw_decode_plan_events(void* pointer,BASS_MIDI_EVENT* out,unsigned capacity){
    if(!pointer)return -1;const auto& plan=*static_cast<NativeRawDecodePlan*>(pointer);if(capacity<plan.events.size()||(!out&&!plan.events.empty()))return -2;
    std::copy(plan.events.begin(),plan.events.end(),out);return static_cast<int>(plan.events.size());}
extern "C" __declspec(dllexport) int __cdecl raw_decode_plan_read2(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out||capacity<13)return 0;const auto& plan=*static_cast<NativeRawDecodePlan*>(pointer);AcquireSRWLockShared(&plan.owner->lock);
    const unsigned long long values[]{plan.sequence,plan.qualified?1ull:0ull,plan.events.size(),plan.selections,plan.next.status,plan.next.port,
        static_cast<unsigned>(plan.error),plan.finished?1ull:0ull,plan.submitted?1ull:0ull,plan.successful?1ull:0ull,plan.mode,
        plan.selectionQualified?1ull:0ull,plan.next.selectorUncertain};std::copy_n(values,13,out);ReleaseSRWLockShared(&plan.owner->lock);return 13;
}
extern "C" __declspec(dllexport) int __cdecl raw_decode_plan_selection_events(void* pointer,BASS_MIDI_EVENT* out,unsigned capacity){
    if(!pointer)return -1;const auto& plan=*static_cast<NativeRawDecodePlan*>(pointer);if(!plan.selectionQualified)return -3;
    if(capacity<plan.selectionEvents.size()||(!out&&!plan.selectionEvents.empty()))return -2;
    std::copy(plan.selectionEvents.begin(),plan.selectionEvents.end(),out);return static_cast<int>(plan.selectionEvents.size());
}
extern "C" __declspec(dllexport) int __cdecl raw_decode_plan_validate(void* pointer){
    if(!pointer)return 0;const auto& plan=*static_cast<NativeRawDecodePlan*>(pointer);AcquireSRWLockShared(&plan.owner->lock);
    const bool accepted=!plan.finished&&plan.owner->sequence==plan.sequence;ReleaseSRWLockShared(&plan.owner->lock);return accepted?1:0;}
extern "C" __declspec(dllexport) int __cdecl raw_decode_plan_commit(void* pointer,unsigned submitted,unsigned successful,int error){
    if(!pointer||submitted>1||successful>1||(!submitted&&successful))return -1;auto& plan=*static_cast<NativeRawDecodePlan*>(pointer);auto& context=*plan.owner;
    AcquireSRWLockExclusive(&context.lock);if(plan.finished||context.sequence!=plan.sequence){ReleaseSRWLockExclusive(&context.lock);return 0;}
    plan.finished=true;plan.submitted=submitted!=0;plan.successful=successful!=0;plan.error=error;
    if(submitted){++context.sequence;if(successful)context.state=plan.next;else context.state.valid=false;}
    ReleaseSRWLockExclusive(&context.lock);return 1;}
extern "C" __declspec(dllexport) void __cdecl raw_decode_plan_release(void* pointer){delete static_cast<NativeRawDecodePlan*>(pointer);}

// Same ConvertEvents contract as raw_decode_plan_prepare, but all application
// storage is allocated BEFORE an explicit bytewalker can publish a prefix.
// This selection-only scratch never changes RAW bytes or app interception.
struct NativeRawSelectionScratch {
    std::vector<unsigned char> input;
    std::vector<BASS_MIDI_EVENT> decoded,selected;
    unsigned selections=0;
    explicit NativeRawSelectionScratch(unsigned maximum):input(maximum+256u),decoded(maximum+128u),selected(maximum){}
    bool prepare(NativeRawDecodeContext& context,unsigned mode,const unsigned char* bytes,unsigned length,void* conversion,void* failure,int& error){
        error=0;selections=0;NativeRawDecodeContext::State state(false),next(false);
        AcquireSRWLockShared(&context.lock);state=context.state;ReleaseSRWLockShared(&context.lock);next=state;
        if(!state.valid||!rawDecodeAdvance(next,bytes,length,mode,&context)){error=-3;return false;}
        const bool timed=(mode&(BASS_MIDI_EVENTS_TIME|BASS_MIDI_EVENTS_ABSTIME))!=0;
        const unsigned flags=(mode&BASS_MIDI_EVENTS_NORSTATUS)|(timed?BASS_MIDI_EVENTS_TIME:0u);
        unsigned size=0;
        auto message=[&](const unsigned char* value,unsigned count){if(timed)input[size++]=0;std::copy_n(value,count,input.data()+size);size+=count;};
        const unsigned char port0[]{0xff,0x21,1,0};message(port0,4);
        for(unsigned channel=0;channel<16;++channel){const unsigned selector=(state.selectorUncertain&(1u<<channel))?0x3fffu:state.selector[channel];const bool nrpn=(selector&0x4000u)!=0;
            const unsigned char high[]{static_cast<unsigned char>(0xb0u|channel),static_cast<unsigned char>(nrpn?99:101),static_cast<unsigned char>((selector>>7)&127u)};
            const unsigned char low[]{static_cast<unsigned char>(0xb0u|channel),static_cast<unsigned char>(nrpn?98:100),static_cast<unsigned char>(selector&127u)};message(high,3);message(low,3);}
        const unsigned char port[]{0xff,0x21,1,static_cast<unsigned char>(state.port)};message(port,4);
        if(state.status&&!(flags&BASS_MIDI_EVENTS_NORSTATUS)){const unsigned char dummy[]{static_cast<unsigned char>(state.status),0,0};message(dummy,(state.status&0xf0u)==0xc0u||(state.status&0xf0u)==0xd0u?2u:3u);}
        else{const unsigned char clear[]{0xf6};message(clear,1);}
        auto convert=reinterpret_cast<DWORD(WINAPI*)(const BYTE*,DWORD,BASS_MIDI_EVENT*,DWORD,DWORD)>(conversion);
        auto getError=reinterpret_cast<int(WINAPI*)()>(failure);
        const DWORD before=convert(input.data(),size,nullptr,0,flags);
        if(before==UINT_MAX){error=getError();return false;}
        if(length>input.size()-size){error=-2;return false;}
        std::copy_n(bytes,length,input.data()+size);size+=length;
        const DWORD count=convert(input.data(),size,nullptr,0,flags);
        if(count==UINT_MAX){error=getError();return false;}
        if(count<before||count>decoded.size()){error=-3;return false;}
        if(convert(input.data(),size,decoded.data(),count,flags)!=count){error=getError();return false;}
        for(unsigned i=before;i<count;++i){auto event=decoded[i];if(mode&0xffffu)event.chan=(mode&0xffffu)-1;
            if(event.event==MIDI_EVENT_PROGRAM||event.event==MIDI_EVENT_BANK||event.event==MIDI_EVENT_BANK_LSB||event.event==MIDI_EVENT_DRUMS){
                if(selections==selected.size()){error=-3;return false;}selected[selections++]=event;}}
        return true;
    }
};

static BOOL CALLBACK midiNoteFilter(HSTREAM handle, int track, BASS_MIDI_EVENT* event,
                                   BOOL seeking, void* user) {
    if(humanizationSeed&&event&&event->event==MIDI_EVENT_NOTE)return FALSE;
    if (event && (event->event == MIDI_EVENT_NOTE || event->event == MIDI_EVENT_NOTESOFF ||
                  event->event == MIDI_EVENT_SOUNDOFF)) return TRUE;
    auto* delegate = reinterpret_cast<MIDIFILTERPROC*>(user);
    return delegate ? delegate(handle, track, event, seeking, nullptr) : TRUE;
}
extern "C" __declspec(dllexport) void* __cdecl midi_note_filter_proc() {
    return reinterpret_cast<void*>(&midiNoteFilter);
}

// Selective BASS_SYNC_MIDI_EVENT observation. bass.h SYNCPROC ABI and
// bassmidi.h syncs: LOWORD=raw16, HIWORD=channel for these control events.
// The callback records state only: no Python/GIL, allocation, OS/BASS call,
// mutex, UI, logging or waiting. Audio events themselves are NEVER coalesced.
// Finite latest-state cells cover every watched channel/key (not a history
// ring that can overflow). Control snapshots replay latest cells in source
// order before Program/manual/reset barriers. TEMPO's24-bit ABI is NOT used.
struct NativeMidiState;
static std::atomic<unsigned> nativeMidiClockCount{0};
struct NativeMidiClock {
    std::atomic<unsigned long long> serial{0};
    std::atomic<unsigned> writers{0},references{1};
    NativeMidiClock(){++nativeMidiClockCount;}
    ~NativeMidiClock(){--nativeMidiClockCount;}
    void retain(){references.fetch_add(1,std::memory_order_relaxed);}
    void release(){if(references.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
};
extern "C" __declspec(dllexport) unsigned __cdecl midi_clock_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl midi_clock_create(){
    return new(std::nothrow) NativeMidiClock;
}
extern "C" __declspec(dllexport) void __cdecl midi_clock_status(void* pointer,unsigned long long* out){
    if(!pointer||!out)return;auto& clock=*static_cast<NativeMidiClock*>(pointer);
    out[0]=clock.serial.load(std::memory_order_acquire);out[1]=clock.writers.load(std::memory_order_acquire);
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_clock_quiet_version(){return 1;}
extern "C" __declspec(dllexport) void __cdecl midi_clock_quiet_status(void* pointer,unsigned long long* out){
    if(!pointer||!out)return;auto& clock=*static_cast<NativeMidiClock*>(pointer);
    // A writer may fully finish between the first serial and writer loads.
    // Double collect closes that stale-serial/quiet window without audio locks.
    const auto before=clock.serial.load(std::memory_order_acquire);
    const auto busy=clock.writers.load(std::memory_order_acquire);
    const auto after=clock.serial.load(std::memory_order_acquire);
    out[0]=after;out[1]=busy||before!=after;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_clock_users(void* pointer){
    return pointer?static_cast<NativeMidiClock*>(pointer)->references.load():0;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_clock_live_count(){return nativeMidiClockCount.load();}
extern "C" __declspec(dllexport) void __cdecl midi_clock_release(void* pointer){
    if(pointer)static_cast<NativeMidiClock*>(pointer)->release();
}
struct NativeMidiStateContext {NativeMidiState* owner=nullptr;unsigned event=0;};
struct NativeMidiState {
    static constexpr unsigned channels=128;
    unsigned stream, cellsCount=0,eventCount=0;
    std::array<unsigned,256> offsets{},lengths{};
    std::array<unsigned,256> eventTypes{};
    std::array<NativeMidiStateContext,256> contexts{};
    std::unique_ptr<std::atomic<unsigned long long>[]> cells;
    std::unique_ptr<std::atomic<unsigned long long>[]> touched;
    std::atomic<unsigned long long> serial{0};
    std::atomic<unsigned> writers{0},references{1};
    NativeMidiClock* clock=nullptr;
    // Own additive failure ABI1. An immutable composite preserves every error
    // DWORD and its low/high/type capture positions; ordinary observation
    // cells/ABI remain unchanged. Storage must be prepared before publication.
    // Product resource policy, not a BASS/BASSMIDI event or library maximum.
    static constexpr unsigned failureCapacityLimit=65536;
    static constexpr unsigned failureLeaseCapacityLimit=256;
    using FailureRecord=std::array<unsigned long long,10>;
    struct FailureLease {unsigned long long token=0;unsigned remaining=0;};
    SRWLOCK failureLock=SRWLOCK_INIT;
    std::unique_ptr<FailureRecord[]> failureRows;
    std::atomic<unsigned> failureCapacity{0};
    unsigned long long failureProduced=0,failureConsumed=0,failureRefused=0;
    std::array<FailureLease,failureLeaseCapacityLimit> failureLeases{};
    unsigned failureReserved=0,failureActiveLeases=0;
    std::atomic<unsigned> failureReaders{0};
    CONDITION_VARIABLE failureChanged=CONDITION_VARIABLE_INIT;
    HANDLE failureThread=nullptr;
    bool failureClosing=false,failureGrowthRequested=false;
    unsigned failureGrowthTarget=0;
    unsigned long long failureGrowthRevision=0,failureGrowthObserved=0,failureAllocationFailures=0;
    unsigned failureFailedTarget=0;
    static unsigned __stdcall growFailures(void* pointer){
        auto& s=*static_cast<NativeMidiState*>(pointer);
        for(;;){AcquireSRWLockExclusive(&s.failureLock);
            while(!s.failureGrowthRequested&&!s.failureClosing)SleepConditionVariableSRW(&s.failureChanged,&s.failureLock,INFINITE,0);
            if(s.failureClosing){ReleaseSRWLockExclusive(&s.failureLock);return 0;}
            const unsigned target=s.failureGrowthTarget;ReleaseSRWLockExclusive(&s.failureLock);
            std::unique_ptr<FailureRecord[]> staged;
            try{staged=std::make_unique<FailureRecord[]>(target);}catch(const std::bad_alloc&){}
            AcquireSRWLockExclusive(&s.failureLock);
            // Snapshot readers pin the complete immutable original arena.
            // Publishing or freeing its pointer waits for their native copy,
            // never Python notification/ACK. No allocator runs under a lock.
            while(staged&&s.failureReaders.load()&&!s.failureClosing)SleepConditionVariableSRW(&s.failureChanged,&s.failureLock,INFINITE,0);
            if(s.failureClosing){ReleaseSRWLockExclusive(&s.failureLock);return 0;}
            if(staged&&target>s.failureCapacity){
                for(auto ordinal=s.failureConsumed;ordinal<s.failureProduced;++ordinal)
                    staged[static_cast<size_t>(ordinal%target)]=s.failureRows[static_cast<size_t>(ordinal%s.failureCapacity)];
                s.failureRows.swap(staged);s.failureCapacity=target;++s.failureGrowthRevision;s.failureFailedTarget=0;
            }else if(!staged){++s.failureAllocationFailures;s.failureFailedTarget=target;}
            s.failureGrowthRequested=s.failureGrowthTarget>target;WakeAllConditionVariable(&s.failureChanged);ReleaseSRWLockExclusive(&s.failureLock);
            // The displaced arena is released on this allocation-only thread.
        }
    }
    bool requestFailureCapacity(unsigned long long extra){
        AcquireSRWLockExclusive(&failureLock);
        const auto needed=failureProduced-failureConsumed+failureReserved+extra;
        bool ready=false;
        // This is the byte-addressable arena resource bound, not MIDI/BASS.
        constexpr unsigned maximum=UINT_MAX/sizeof(FailureRecord);
        if(failureThread&&!failureClosing&&needed>failureCapacity&&needed<=maximum){
            const unsigned target=static_cast<unsigned>(std::max(needed,std::min<unsigned long long>(maximum,static_cast<unsigned long long>(failureCapacity)*2)));
            if(!failureFailedTarget||target>failureFailedTarget){failureGrowthTarget=std::max(failureGrowthTarget,target);failureGrowthRequested=true;WakeAllConditionVariable(&failureChanged);ready=true;}
            else failureFailedTarget=0; // This refusal reports OOM once; a future original frame may retry.
        }
        ReleaseSRWLockExclusive(&failureLock);return ready;
    }
    explicit NativeMidiState(unsigned handle,const unsigned* events,unsigned count,NativeMidiClock* shared=nullptr,
            const unsigned* keyedEvents=nullptr,unsigned keyedCount=0):stream(handle){
        for(unsigned i=0;i<count;++i){
            const unsigned event=events[i];
            if(std::find(eventTypes.begin(),eventTypes.begin()+eventCount,event)!=eventTypes.begin()+eventCount)continue;
            const unsigned slot=eventCount++;
            eventTypes[slot]=event;offsets[slot]=cellsCount;
            const bool keyed=event==MIDI_EVENT_KEYPRES||(keyedCount&&std::find(keyedEvents,keyedEvents+keyedCount,event)!=keyedEvents+keyedCount);
            lengths[slot]=keyed?channels*128:event==MIDI_EVENT_SYSTEM?1:channels;
            cellsCount+=lengths[slot];contexts[slot]={this,slot};
        }
        cells=std::make_unique<std::atomic<unsigned long long>[]>(cellsCount);
        for(unsigned i=0;i<cellsCount;++i)cells[i].store(0,std::memory_order_relaxed);
        touched=std::make_unique<std::atomic<unsigned long long>[]>((cellsCount+63)/64);
        for(unsigned i=0;i<(cellsCount+63)/64;++i)touched[i].store(0,std::memory_order_relaxed);
        // All allocating preparation precedes borrowing a shared clock.
        if(shared){clock=shared;clock->retain();}else clock=new NativeMidiClock;
    }
    ~NativeMidiState(){
        if(failureThread){AcquireSRWLockExclusive(&failureLock);failureClosing=true;WakeAllConditionVariable(&failureChanged);ReleaseSRWLockExclusive(&failureLock);
            WaitForSingleObject(failureThread,INFINITE);CloseHandle(failureThread);}
        if(clock)clock->release();}
    void retain(){references.fetch_add(1,std::memory_order_relaxed);}
    void release(){if(references.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
    void receive(unsigned handle,unsigned slot,unsigned data){
        if(handle!=stream||slot>=eventCount)return;
        const unsigned event=eventTypes[slot];
        const unsigned channel=data>>16,raw=data&0xffffu;
        if(channel>=channels&&event!=MIDI_EVENT_SYSTEM)return;
        const unsigned index=offsets[slot]+(event==MIDI_EVENT_SYSTEM?0:
            lengths[slot]==channels*128?channel*128+(raw&127u):channel);
        clock->writers.fetch_add(1,std::memory_order_acq_rel);
        writers.fetch_add(1,std::memory_order_acq_rel);
        const auto sequence=clock->serial.fetch_add(1,std::memory_order_acq_rel)+1;
        auto last=serial.load(std::memory_order_relaxed);
        while(last<sequence&&!serial.compare_exchange_weak(last,sequence,std::memory_order_release,std::memory_order_relaxed)){}
        //48-bit lifetime serial + exact16-bit raw. At2^48 records the observer
        // explicitly becomes unavailable; never wrap into a false old record.
        if(sequence<(1ull<<48)){
            const auto value=(sequence<<16)|raw;
            auto previous=cells[index].load(std::memory_order_relaxed);
            while(previous<value&&!cells[index].compare_exchange_weak(previous,value,
                std::memory_order_release,std::memory_order_relaxed)){}
            touched[index/64].fetch_or(1ull<<(index%64),std::memory_order_release);
        }
        writers.fetch_sub(1,std::memory_order_release);
        clock->writers.fetch_sub(1,std::memory_order_release);
    }
};
static_assert(std::atomic<unsigned long long>::is_always_lock_free,
              "MIDI state observation cannot lock an audio callback");
static void CALLBACK nativeMidiStateSync(HSYNC,DWORD handle,DWORD data,void* pointer){
    if(pointer){auto& context=*static_cast<NativeMidiStateContext*>(pointer);
        context.owner->receive(handle,context.event,data);}
}
extern "C" __declspec(dllexport) void* __cdecl midi_state_create(unsigned stream,const unsigned* events,unsigned count){
    if(!stream||!events||!count||count>256)return nullptr;
    // Full DWORD event identities, including non-MIDI MIXLEVEL/TRANSPOSE.
    // Wildcard/TEMPO need a different packed-data ABI: all-or-nothing fallback.
    for(unsigned i=0;i<count;++i)if(!events[i]||events[i]==MIDI_EVENT_TEMPO)return nullptr;
    try{return new NativeMidiState(stream,events,count);}catch(const std::bad_alloc&){return nullptr;}
}
extern "C" __declspec(dllexport) void* __cdecl midi_state_create_clock(unsigned stream,const unsigned* events,unsigned count,void* clock){
    if(!stream||!events||!count||count>256||!clock)return nullptr;
    for(unsigned i=0;i<count;++i)if(!events[i]||events[i]==MIDI_EVENT_TEMPO)return nullptr;
    try{return new NativeMidiState(stream,events,count,static_cast<NativeMidiClock*>(clock));}
    catch(const std::bad_alloc&){return nullptr;}
}
// Own additive ABI1: accepted semantic/reset aliases for KEYPRES retain every
// key, exactly like actual bassmidi.h MIDI_EVENT_KEYPRES LOBYTE/HIBYTE data.
// Shape is immutable, validated/allocated before any native borrower exists;
// no live resizing, callback allocation, additional lock or invented MIDI.
extern "C" __declspec(dllexport) unsigned __cdecl midi_state_keyed_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl midi_state_create_clock_keyed(unsigned stream,const unsigned* events,unsigned count,
        void* clock,const unsigned* keyedEvents,unsigned keyedCount){
    if(!stream||!events||!count||count>256||!clock||(keyedCount&&!keyedEvents)||keyedCount>count)return nullptr;
    for(unsigned i=0;i<count;++i)if(!events[i]||events[i]==MIDI_EVENT_TEMPO)return nullptr;
    for(unsigned i=0;i<keyedCount;++i){const auto event=keyedEvents[i];
        if(event==MIDI_EVENT_SYSTEM||std::find(events,events+count,event)==events+count||
                std::find(keyedEvents,keyedEvents+i,event)!=keyedEvents+i)return nullptr;}
    try{return new NativeMidiState(stream,events,count,static_cast<NativeMidiClock*>(clock),keyedEvents,keyedCount);}
    catch(const std::bad_alloc&){return nullptr;}
}
// A future producer must pre-admit storage for every possible failure BEFORE
// its audible operation, or retain the original failed composite elsewhere.
// Post-audio refusal is NOT permission to forget an error or repeat audio.
// This API alone does not install/replace any mapper/semantic/reset writer.
extern "C" __declspec(dllexport) unsigned __cdecl midi_state_failure_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_state_failure_prepare(void* pointer,unsigned capacity){
    if(!pointer||!capacity||capacity>NativeMidiState::failureCapacityLimit)return 0;
    auto& s=*static_cast<NativeMidiState*>(pointer);
    // The creator owns the sole reference and has not registered callbacks or
    // native borrowers. Concurrent preparation/publication is not supported.
    if(s.references.load(std::memory_order_acquire)!=1||s.writers.load(std::memory_order_acquire)||s.serial.load(std::memory_order_acquire))return 0;
    std::unique_ptr<NativeMidiState::FailureRecord[]> rows;
    try{rows=std::make_unique<NativeMidiState::FailureRecord[]>(capacity);}catch(const std::bad_alloc&){return 0;}
    AcquireSRWLockExclusive(&s.failureLock);
    const bool ready=!s.failureCapacity&&s.references.load(std::memory_order_acquire)==1&&
        !s.writers.load(std::memory_order_acquire)&&!s.serial.load(std::memory_order_acquire);
    if(ready){s.failureRows.swap(rows);s.failureCapacity=capacity;}
    ReleaseSRWLockExclusive(&s.failureLock);return ready?1:0;
}
// Process-wide monotonic IDs prevent a foreign state's token or recycled slot
// from claiming a new reservation. Exhaustion is explicit; IDs never wrap.
static std::atomic<unsigned long long> nativeFailureLeaseIdentity{0};
extern "C" __declspec(dllexport) unsigned __cdecl midi_state_failure_lease_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_state_failure_growth_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_state_failure_growth_prepare(void* pointer){
    if(!pointer)return 0;auto& s=*static_cast<NativeMidiState*>(pointer);AcquireSRWLockExclusive(&s.failureLock);
    if(!s.failureCapacity||s.failureThread||s.failureClosing||s.failureProduced||s.failureActiveLeases){ReleaseSRWLockExclusive(&s.failureLock);return 0;}
    s.failureThread=reinterpret_cast<HANDLE>(_beginthreadex(nullptr,0,&NativeMidiState::growFailures,&s,0,nullptr));
    const bool ready=s.failureThread!=nullptr;ReleaseSRWLockExclusive(&s.failureLock);return ready?1:0;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_state_failure_growth_status(void* pointer,unsigned long long* out,unsigned count){
    if(!pointer||!out||count<6)return 0;auto& s=*static_cast<NativeMidiState*>(pointer);AcquireSRWLockShared(&s.failureLock);
    const unsigned long long values[]{s.failureCapacity,s.failureGrowthRevision,s.failureAllocationFailures,s.failureGrowthRequested?1ull:0ull,s.failureReaders.load(),s.failureClosing?1ull:0ull};
    std::copy_n(values,6,out);ReleaseSRWLockShared(&s.failureLock);return 6;
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_state_failure_reserve(void* pointer,unsigned count){
    if(!pointer||!count)return 0;auto& s=*static_cast<NativeMidiState*>(pointer);
    AcquireSRWLockExclusive(&s.failureLock);
    if(!s.failureCapacity||count>s.failureCapacity){ReleaseSRWLockExclusive(&s.failureLock);return 0;}
    if(s.failureProduced-s.failureConsumed+s.failureReserved+count>s.failureCapacity||s.failureActiveLeases==s.failureLeaseCapacityLimit){
        ++s.failureRefused;ReleaseSRWLockExclusive(&s.failureLock);return 0;}
    auto identity=nativeFailureLeaseIdentity.load(std::memory_order_acquire);
    for(;;){if(identity==UINT64_MAX){++s.failureRefused;ReleaseSRWLockExclusive(&s.failureLock);return 0;}
        if(nativeFailureLeaseIdentity.compare_exchange_weak(identity,identity+1,std::memory_order_acq_rel,std::memory_order_acquire))break;}
    for(auto& lease:s.failureLeases)if(!lease.token){lease={identity+1,count};break;}
    s.failureReserved+=count;++s.failureActiveLeases;s.retain();
    ReleaseSRWLockExclusive(&s.failureLock);return identity+1;
}
extern "C" __declspec(dllexport) int __cdecl midi_state_failure_release(void* pointer,unsigned long long token){
    if(!pointer||!token)return 0;auto& s=*static_cast<NativeMidiState*>(pointer);
    AcquireSRWLockExclusive(&s.failureLock);
    for(auto& lease:s.failureLeases)if(lease.token==token){
        s.failureReserved-=lease.remaining;lease={};--s.failureActiveLeases;
        ReleaseSRWLockExclusive(&s.failureLock);s.release();return 1;
    }
    ReleaseSRWLockExclusive(&s.failureLock);return 0;
}
extern "C" __declspec(dllexport) int __cdecl midi_state_failure_lease_status(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out||capacity<6)return 0;auto& s=*static_cast<NativeMidiState*>(pointer);
    AcquireSRWLockShared(&s.failureLock);
    const unsigned long long values[]{s.failureReserved,s.failureActiveLeases,s.failureLeaseCapacityLimit,
        s.failureCapacity-(s.failureProduced-s.failureConsumed+s.failureReserved),s.references.load(std::memory_order_acquire),s.failureReaders.load(std::memory_order_acquire)};
    std::copy_n(values,6,out);ReleaseSRWLockShared(&s.failureLock);return 6;
}
static int nativeMidiFailurePush(void* pointer,unsigned long long token,unsigned channel,
        unsigned lowEvent,unsigned highEvent,unsigned typeEvent,unsigned errorDWORD,unsigned type){
    if(!pointer||channel>=NativeMidiState::channels||!lowEvent||!highEvent||lowEvent==highEvent||
            (typeEvent&&(typeEvent==lowEvent||typeEvent==highEvent))||type>65535u||(!typeEvent&&type))return -1;
    auto& s=*static_cast<NativeMidiState*>(pointer);
    auto hasEvent=[&](unsigned event){return std::find(s.eventTypes.begin(),s.eventTypes.begin()+s.eventCount,event)!=s.eventTypes.begin()+s.eventCount;};
    if(!hasEvent(lowEvent)||!hasEvent(highEvent)||(typeEvent&&!hasEvent(typeEvent)))return -1;
    AcquireSRWLockExclusive(&s.failureLock);
    if(!s.failureCapacity){ReleaseSRWLockExclusive(&s.failureLock);return -1;}
    NativeMidiState::FailureLease* lease=nullptr;
    if(token){for(auto& item:s.failureLeases)if(item.token==token){lease=&item;break;}
        if(!lease||!lease->remaining){ReleaseSRWLockExclusive(&s.failureLock);return -1;}}
    if((!lease&&s.failureProduced-s.failureConsumed+s.failureReserved>=s.failureCapacity)||s.failureProduced==UINT64_MAX){
        ++s.failureRefused;ReleaseSRWLockExclusive(&s.failureLock);return 0;}
    // Reserve the whole record before advancing any capture position. The
    // single CAS reserves consecutive positions across all shared-clock
    // writers; the native publisher lock preserves FIFO/source order.
    const unsigned slots=typeEvent?3u:2u;
    auto before=s.clock->serial.load(std::memory_order_acquire);
    s.clock->writers.fetch_add(1,std::memory_order_acq_rel);s.writers.fetch_add(1,std::memory_order_acq_rel);
    for(;;){
        if(before>=(1ull<<48)-slots){
            ++s.failureRefused;s.writers.fetch_sub(1,std::memory_order_release);s.clock->writers.fetch_sub(1,std::memory_order_release);
            ReleaseSRWLockExclusive(&s.failureLock);return 0;}
        if(s.clock->serial.compare_exchange_weak(before,before+slots,std::memory_order_acq_rel,std::memory_order_acquire))break;
    }
    const auto ordinal=s.failureProduced;auto& row=s.failureRows[static_cast<size_t>(ordinal%s.failureCapacity)];
    row={before+1,before+2,typeEvent?before+3:0ull,channel,lowEvent,highEvent,typeEvent,errorDWORD,type,ordinal};
    if(lease){--lease->remaining;--s.failureReserved;}
    ++s.failureProduced;s.writers.fetch_sub(1,std::memory_order_release);s.clock->writers.fetch_sub(1,std::memory_order_release);
    ReleaseSRWLockExclusive(&s.failureLock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_state_failure_push(void* pointer,unsigned channel,
        unsigned lowEvent,unsigned highEvent,unsigned typeEvent,unsigned errorDWORD,unsigned type){
    return nativeMidiFailurePush(pointer,0,channel,lowEvent,highEvent,typeEvent,errorDWORD,type);
}
extern "C" __declspec(dllexport) int __cdecl midi_state_failure_push_reserved(void* pointer,unsigned long long token,unsigned channel,
        unsigned lowEvent,unsigned highEvent,unsigned typeEvent,unsigned errorDWORD,unsigned type){
    if(!token)return -1;return nativeMidiFailurePush(pointer,token,channel,lowEvent,highEvent,typeEvent,errorDWORD,type);
}
extern "C" __declspec(dllexport) int __cdecl midi_state_failure_snapshot(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out)return -1;auto& s=*static_cast<NativeMidiState*>(pointer);
    AcquireSRWLockShared(&s.failureLock);
    if(!s.failureCapacity){ReleaseSRWLockShared(&s.failureLock);return -1;}
    const auto first=s.failureConsumed,count=s.failureProduced-first;
    if(count>capacity){ReleaseSRWLockShared(&s.failureLock);return -2;}
    // Readers pin immutable slots, not a producer lock. ACK cannot release or
    // recycle those slots until copying finishes; callbacks never wait on a
    // GUI-held lock during this potentially large native-only copy.
    s.failureReaders.fetch_add(1,std::memory_order_acq_rel);ReleaseSRWLockShared(&s.failureLock);
    for(unsigned long long i=0;i<count;++i){const auto& row=s.failureRows[static_cast<size_t>((first+i)%s.failureCapacity)];
        std::copy(row.begin(),row.end(),out+static_cast<size_t>(i)*10);}
    AcquireSRWLockExclusive(&s.failureLock);
    s.failureReaders.fetch_sub(1,std::memory_order_release);WakeAllConditionVariable(&s.failureChanged);
    ReleaseSRWLockExclusive(&s.failureLock);return static_cast<int>(count);
}
extern "C" __declspec(dllexport) int __cdecl midi_state_failure_ack(void* pointer,unsigned long long firstSerial){
    if(!pointer||!firstSerial)return 0;auto& s=*static_cast<NativeMidiState*>(pointer);
    AcquireSRWLockExclusive(&s.failureLock);
    const bool ready=s.failureCapacity&&!s.failureReaders.load(std::memory_order_acquire)&&s.failureConsumed<s.failureProduced&&
        s.failureRows[static_cast<size_t>(s.failureConsumed%s.failureCapacity)][0]==firstSerial;
    if(ready)++s.failureConsumed;ReleaseSRWLockExclusive(&s.failureLock);return ready?1:0;
}
extern "C" __declspec(dllexport) int __cdecl midi_state_failure_status(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out||capacity<8)return 0;auto& s=*static_cast<NativeMidiState*>(pointer);
    AcquireSRWLockShared(&s.failureLock);
    const unsigned long long values[]{s.failureProduced,s.failureConsumed,s.failureRefused,s.failureCapacity,
        s.failureProduced-s.failureConsumed,s.failureCapacity?1ull:0ull,s.clock->serial.load(std::memory_order_acquire),s.writers.load(std::memory_order_acquire)};
    std::copy_n(values,8,out);ReleaseSRWLockShared(&s.failureLock);return 8;
}
extern "C" __declspec(dllexport) void* __cdecl midi_state_proc(){return reinterpret_cast<void*>(&nativeMidiStateSync);}
extern "C" __declspec(dllexport) void* __cdecl midi_state_context(void* pointer,unsigned event){
    if(!pointer)return nullptr;
    auto& s=*static_cast<NativeMidiState*>(pointer);
    for(unsigned slot=0;slot<s.eventCount;++slot)if(s.eventTypes[slot]==event)return &s.contexts[slot];
    return nullptr;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_state_capacity(void* pointer){
    return pointer?static_cast<NativeMidiState*>(pointer)->cellsCount:0;
}
extern "C" __declspec(dllexport) void __cdecl midi_state_status(void* pointer,unsigned long long* out){
    if(!pointer||!out)return;auto& s=*static_cast<NativeMidiState*>(pointer);
    out[0]=s.serial.load(std::memory_order_acquire);out[1]=s.writers.load(std::memory_order_acquire);
}
// Out rows: event,data,serial; no partial-success cursor. Concurrent mutation
// returns -1 and leaves the cursor unchanged. No waiting/retry inside native.
extern "C" __declspec(dllexport) int __cdecl midi_state_snapshot(void* pointer,unsigned long long* out,unsigned capacity,
        unsigned long long since,unsigned long long* meta){
    if(!pointer||!out||!meta)return -2;auto& s=*static_cast<NativeMidiState*>(pointer);
    const auto before=s.serial.load(std::memory_order_acquire);
    meta[0]=before;meta[1]=s.writers.load(std::memory_order_acquire);
    if(meta[1]||before>=(1ull<<48))return -1;
    if(before==since)return 0;
    unsigned count=0;
    for(unsigned slot=0;slot<s.eventCount;++slot){
        const unsigned event=s.eventTypes[slot];
        const unsigned base=s.offsets[slot],end=base+s.lengths[slot];
        for(unsigned start=base;start<end;){
            const unsigned word=start/64,limit=std::min(end,(word+1)*64);
            auto bits=s.touched[word].load(std::memory_order_acquire);
            bits&=~((1ull<<(start%64))-1);
            if(limit%64)bits&=(1ull<<(limit%64))-1;
            while(bits){
                unsigned long bit=0;_BitScanForward64(&bit,bits);bits&=bits-1;
                const unsigned absolute=word*64+bit,index=absolute-base;
                const auto value=s.cells[absolute].load(std::memory_order_acquire);
                const auto version=value>>16;
                if(version<=since)continue;
                if(count>=capacity)return -2;
                const unsigned channel=event==MIDI_EVENT_SYSTEM?0:s.lengths[slot]==128*128?index/128:index;
                auto* row=out+3*count++;row[0]=event;row[1]=(channel<<16)|(value&0xffffu);row[2]=version;
            }
            // Persistent touched bits cannot erase a racing update. Stable
            // version cursor filters old values, sparse control reads stay cheap.
            start=limit;
        }
    }
    meta[1]=s.writers.load(std::memory_order_acquire);
    meta[0]=s.serial.load(std::memory_order_acquire);
    if(meta[1]||meta[0]!=before)return -1;
    return static_cast<int>(count);
}
extern "C" __declspec(dllexport) void __cdecl midi_state_destroy(void* pointer){
    if(pointer)static_cast<NativeMidiState*>(pointer)->release(); // Root after RemoveSync; native executor retains its borrower.
}

// Per-runtime send-floor control. One native publication per field; callbacks
// take an atomic snapshot without Python, locks, allocation or GUI ownership.
// Channel send23/24, NOT global reverb/chorus engine Level36/41.
static std::atomic<unsigned> nativeMidiSendDomainCount{0};
struct NativeMidiSendDomain {
    std::atomic<unsigned> references{1},floors{0};
    std::array<std::atomic<unsigned>,128> requests;
    NativeMidiSendDomain(){for(auto& value:requests)value.store(40,std::memory_order_relaxed);++nativeMidiSendDomainCount;}
    ~NativeMidiSendDomain(){--nativeMidiSendDomainCount;}
    void retain(){references.fetch_add(1,std::memory_order_relaxed);}
    void release(){if(references.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
    unsigned apply(unsigned event,unsigned request)const{
        const unsigned packed=floors.load(std::memory_order_acquire);
        const unsigned base=event==MIDI_EVENT_REVERB?packed&127u:(packed>>7)&127u;
        // Original two-stage formula with global_base=0 (current state policy).
        return std::min(127u,base+((127u-base)*std::min(127u,request))/127u);
    }
    void request(unsigned channel,unsigned event,unsigned value){
        const unsigned shift=event==MIDI_EVENT_REVERB?0:7,mask=127u<<shift;
        auto old=requests[channel].load(std::memory_order_relaxed);
        while(!requests[channel].compare_exchange_weak(old,(old&~mask)|(value<<shift),std::memory_order_release,std::memory_order_relaxed)){}
    }
};
extern "C" __declspec(dllexport) unsigned __cdecl midi_send_domain_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl midi_send_domain_create(){return new(std::nothrow) NativeMidiSendDomain;}
extern "C" __declspec(dllexport) int __cdecl midi_send_domain_floor(void* pointer,unsigned event,unsigned value){
    if(!pointer||value>127||(event!=MIDI_EVENT_REVERB&&event!=MIDI_EVENT_CHORUS))return 0;
    auto& domain=*static_cast<NativeMidiSendDomain*>(pointer);
    const unsigned shift=event==MIDI_EVENT_REVERB?0:7,mask=127u<<shift;
    auto previous=domain.floors.load(std::memory_order_relaxed);
    while(!domain.floors.compare_exchange_weak(previous,(previous&~mask)|(value<<shift),
        std::memory_order_release,std::memory_order_relaxed)){}
    return 1;
}
extern "C" __declspec(dllexport) void __cdecl midi_send_domain_release(void* pointer){
    if(pointer)static_cast<NativeMidiSendDomain*>(pointer)->release();
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_send_domain_users(void* pointer){
    return pointer?static_cast<NativeMidiSendDomain*>(pointer)->references.load():0;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_send_domain_live_count(){return nativeMidiSendDomainCount.load();}
extern "C" __declspec(dllexport) int __cdecl midi_send_domain_request(void* pointer,unsigned channel,unsigned event,unsigned value){
    if(!pointer||channel>=128||value>127||(event!=MIDI_EVENT_REVERB&&event!=MIDI_EVENT_CHORUS))return 0;
    static_cast<NativeMidiSendDomain*>(pointer)->request(channel,event,value);return 1;
}
struct MidiSendResult {unsigned channel,event,request,raw,accepted;int error;};
static_assert(sizeof(MidiSendResult)==24,"send apply ABI1");
// Manual/requested send update and its accepted shadow share one BASS barrier.
// Publishing the shadow in Python AFTER StreamEvent would allow a newer native
// filter write to be overwritten while the submitting thread waits for GIL.
extern "C" __declspec(dllexport) int __cdecl midi_send_domain_emit(void* pointer,unsigned stream,
        unsigned channel,unsigned event,unsigned floor,unsigned request,void* emitAddress,void* lockAddress,void* errorAddress,MidiSendResult* row,int* status){
    if(status)std::fill_n(status,3,0);
    if(!pointer||channel>=128||(event!=MIDI_EVENT_REVERB&&event!=MIDI_EVENT_CHORUS)||floor>127||request>127||!emitAddress||!lockAddress||!errorAddress||!row||!status)return -1;
    auto emit=reinterpret_cast<BOOL(WINAPI*)(DWORD,DWORD,DWORD,DWORD)>(emitAddress);
    auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(lockAddress);
    auto error=reinterpret_cast<int(WINAPI*)()>(errorAddress);
    if(!lock(stream,TRUE)){status[0]=error();status[2]=1;return 0;}
    row->channel=channel;row->event=event;row->request=request;row->raw=floor+((127u-floor)*request)/127u;
    row->accepted=emit(stream,channel,event,row->raw)!=FALSE;row->error=row->accepted?0:error();
    if(row->accepted)static_cast<NativeMidiSendDomain*>(pointer)->request(channel,event,request);
    if(!lock(stream,FALSE)){status[1]=error();status[2]=2;return -2;}
    return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_send_domain_apply(void* pointer,unsigned stream,
        const unsigned* channels,unsigned count,void* emitAddress,void* lockAddress,void* errorAddress,MidiSendResult* rows,unsigned capacity,int* status){
    if(status)std::fill_n(status,3,0);
    if(!pointer||!channels||!count||count>128||!emitAddress||!lockAddress||!errorAddress||!rows||capacity<count*2||!status)return -1;
    for(unsigned i=0;i<count;++i)if(channels[i]>=128)return -1;
    auto emit=reinterpret_cast<BOOL(WINAPI*)(DWORD,DWORD,DWORD,DWORD)>(emitAddress);
    auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(lockAddress);
    auto error=reinterpret_cast<int(WINAPI*)()>(errorAddress);
    if(!lock(stream,TRUE)){status[0]=error();status[2]=1;return 0;}
    auto& domain=*static_cast<NativeMidiSendDomain*>(pointer);
    // Snapshot AFTER the BASS barrier: use accepted native MIDI requests,
    // not Python shadows awaiting journal replay. Same original channel/event
    // order, including continued attempts after individual BOOL failures.
    const unsigned floors=domain.floors.load(std::memory_order_acquire);
    for(unsigned i=0;i<count;++i){
        const unsigned requested=domain.requests[channels[i]].load(std::memory_order_acquire);
        for(unsigned effect=0;effect<2;++effect){
            auto& row=rows[i*2+effect];const unsigned shift=effect*7,base=(floors>>shift)&127u;
            row.channel=channels[i];row.event=effect?MIDI_EVENT_CHORUS:MIDI_EVENT_REVERB;
            row.request=(requested>>shift)&127u;row.raw=base+((127u-base)*row.request)/127u;
            row.accepted=emit(stream,row.channel,row.event,row.raw)!=FALSE;row.error=row.accepted?0:error();
        }
    }
    if(!lock(stream,FALSE)){status[1]=error();status[2]=2;return -2;}
    return 1; // Never reacquire Python's GIL while this channel is locked.
}

// Prepared scalar function routing. Tables encode EXACT existing Python
// clamp/round/conversion, not a new interpolation or a changed MIDI domain.
// Native control preparation allocates/copies; callback only indexes, emits
// StreamEvent once at the original event position, and records metadata.
// bassmidi.h: StreamEvent WINAPI(HSTREAM,DWORD,DWORD,DWORD); no re-filter/sync.
struct NativeMidiMapDescriptor {unsigned slot,channel,targetSlot,targetEvent;const unsigned* table;};
static_assert(sizeof(NativeMidiMapDescriptor)==24,"function map descriptor ABI1");
struct LegacyDelayState;
static void delayMidiRetain(LegacyDelayState*);
static void delayMidiRelease(LegacyDelayState*);
static void delayMidiSend(LegacyDelayState*,unsigned);
static bool delayMidiReady(LegacyDelayState*);
struct NativeMidiMapRoute {
    const std::vector<unsigned>* table=nullptr;
    unsigned input=0,attempt=0,output=0,errorLow=0,errorHigh=0,targetEvent=0,request=256;
};
// Prepared selection state. Full-DWORD FIFO records preserve per-key scale
// parameters and repeated accepted writers; this is not the16-bit MIDI meter.
// BASS owns callback serialization; publication/removal uses its channel lock.
// The bounded FIFO must qualify before a selected event's first side effect.
struct MidiNoteAction;
struct NativeActivityIdentity {bool known;unsigned bank,program;};
struct NativeSelectionRecord {unsigned long long serial;unsigned channel,event,raw,kind,key;int error;};
static_assert(sizeof(NativeSelectionRecord)==32,"native selection metadata ABI1");
struct NativeSelectionField {unsigned event,key,neutral,flags;int neutralAmount;};
static_assert(sizeof(NativeSelectionField)==20,"native selection field ABI1");
struct NativeSelectionPreset {unsigned bank,program,flags,profileFirst,profileCount,extendedFirst,extendedCount;unsigned tone[6];};
static_assert(sizeof(NativeSelectionPreset)==52,"native selection preset ABI1");
struct NativeSelectionValue {unsigned field,raw;int amount;};
static_assert(sizeof(NativeSelectionValue)==12,"native selection value ABI1");
struct NativeSelectionRestore {unsigned oldMask,newMask,first,count;};
struct NativeSelectionKeyRevision {unsigned bank,program;unsigned long long revision;};
static_assert(sizeof(NativeSelectionKeyRevision)==16,"native selection revision ABI1");
struct NativeSelectionState;
// Optional first-stage lossless checkpoint arena. Prepared on a control
// thread; capture copies into preallocated slots and never emits audio.
struct NativeSelectionCheckpointArena {
    NativeSelectionState* owner;unsigned capacity,fields,watches;bool attached=false;
    std::atomic<unsigned long long> produced{0},consumed{0},refused{0};
    std::unique_ptr<unsigned long long[]> headers,watchRows,metadata;
    std::unique_ptr<unsigned[]> cells,orders,graphs;
    NativeSelectionCheckpointArena(NativeSelectionState* state,unsigned count,unsigned fieldCount,unsigned watchCount):
        owner(state),capacity(count),fields(fieldCount),watches(watchCount),
        headers(new unsigned long long[static_cast<size_t>(count)*24]{}),watchRows(new unsigned long long[static_cast<size_t>(count)*watchCount*2]{}),
        metadata(new unsigned long long[static_cast<size_t>(count)*11]{}),
        cells(new unsigned[static_cast<size_t>(count)*fieldCount*6]{}),orders(new unsigned[static_cast<size_t>(count)*fieldCount]{}),
        graphs(new unsigned[static_cast<size_t>(count)*514]{}){}
    void capture(const NativeSelectionState& state,unsigned long long serial,unsigned long long cursor,unsigned channel,
        unsigned event,unsigned raw,unsigned kind,unsigned key,int error);
    bool prepareCapacity(unsigned count){
        const auto head=consumed.load(std::memory_order_acquire),tail=produced.load(std::memory_order_acquire);
        if(count<=capacity)return true;
        try{
            // Input-side staging; callbacks retain the same arena object and
            // published storage until every new component is prepared.
            NativeSelectionCheckpointArena prepared(owner,count,fields,watches);
            for(auto index=head;index<tail;++index){const auto from=static_cast<size_t>(index%capacity),to=static_cast<size_t>(index%count);
                std::copy_n(headers.get()+from*24,24,prepared.headers.get()+to*24);
                std::copy_n(cells.get()+from*fields*6,fields*6,prepared.cells.get()+to*fields*6);
                std::copy_n(orders.get()+from*fields,fields,prepared.orders.get()+to*fields);
                std::copy_n(graphs.get()+from*514,514,prepared.graphs.get()+to*514);
                std::copy_n(watchRows.get()+from*watches*2,watches*2,prepared.watchRows.get()+to*watches*2);
                std::copy_n(metadata.get()+from*11,11,prepared.metadata.get()+to*11);
            }
            headers.swap(prepared.headers);cells.swap(prepared.cells);orders.swap(prepared.orders);
            graphs.swap(prepared.graphs);watchRows.swap(prepared.watchRows);metadata.swap(prepared.metadata);capacity=count;return true;
        }catch(const std::bad_alloc&){return false;}
    }
};
// Immediate RAW callbacks belong to the submitting call, even while an older
// timed/ASYNC ticket remains queued on the same stream.
static thread_local NativeSelectionState* selectionSubmittingState=nullptr;
static thread_local unsigned long long selectionSubmittingTicket=0;
struct NativeSelectionSubmissionScope {
    NativeSelectionState* priorState;unsigned long long priorTicket;
    NativeSelectionSubmissionScope(NativeSelectionState* state,unsigned long long ticket):priorState(selectionSubmittingState),priorTicket(selectionSubmittingTicket){selectionSubmittingState=state;selectionSubmittingTicket=ticket;}
    ~NativeSelectionSubmissionScope(){selectionSubmittingState=priorState;selectionSubmittingTicket=priorTicket;}
};
struct NativeSelectionState {
    static constexpr unsigned recordCapacity=65536; // Product budget, NOT BASS maximum.
    std::atomic<unsigned> references{1};NativeMidiClock* clock;unsigned stream;
    BOOL(WINAPI* emit)(HSTREAM,DWORD,DWORD,DWORD);DWORD(WINAPI* get)(HSTREAM,DWORD,DWORD);
    BOOL(WINAPI* source)(HSTREAM,DWORD,BASS_MIDI_FONT*);int(WINAPI* error)();
    std::vector<NativeSelectionField> fields;std::vector<NativeSelectionPreset> presets;std::vector<NativeSelectionValue> values;
    std::vector<unsigned char> reachableProfile,reachableExtended,reachableTouched;
    unsigned profileBound=0,extendedBound=0,touchedBound=0;
    void markReachable(unsigned field,unsigned kind){if(field>=fields.size())return;
        auto& seen=kind==0?reachableProfile:kind==1?reachableExtended:reachableTouched;
        auto& count=kind==0?profileBound:kind==1?extendedBound:touchedBound;
        if(!seen[field]){seen[field]=1;++count;}}
    void markPreset(const NativeSelectionPreset& preset){
        for(unsigned i=preset.profileFirst;i<preset.profileFirst+preset.profileCount;++i)markReachable(values[i].field,0);
        for(unsigned i=preset.extendedFirst;i<preset.extendedFirst+preset.extendedCount;++i)markReachable(values[i].field,1);}
    bool preparationMutable()const{return references.load()==1&&!produced.load()&&!reserved.load()&&!ticketCount;}
    NativeSelectionPreset fallback{0,0,0,0,0,0,0,{100,64,64,64,64,64}};
    static constexpr unsigned recordPageSize=4096;
    std::vector<std::unique_ptr<NativeSelectionRecord[]>> recordPages;
    std::atomic<unsigned> recordLimit{recordCapacity};
    std::atomic<unsigned long long> produced{0},consumed{0},refused{0},failures{0};
    std::atomic<unsigned long long> reserved{0},queuedReservation{0};
    std::atomic<unsigned long long> unticketedReservation{0};
    SRWLOCK ticketsLock=SRWLOCK_INIT;
    struct Ticket {unsigned long long id=0;unsigned requested=0,processed=0,remaining=0,state=0;};
    // Fixed, input-owned identity arena. Queued callbacks never allocate and
    // never guess an oldest ticket from StreamEvents submission order: BASS
    // sorts timed events. 65,536 is a product memory budget, not an API limit.
    static constexpr unsigned identityCapacity=65536;
    struct TicketEvents {unsigned long long first=0;unsigned count=0,position=0;bool qualified=false,ambiguous=false;unsigned nextUnclaimed=0;bool indexed=false;};
    std::array<TicketEvents,4096> ticketEvents;
    std::unique_ptr<BASS_MIDI_EVENT[]> identities{new BASS_MIDI_EVENT[identityCapacity]};
    std::unique_ptr<unsigned char[]> identityClaimed{new unsigned char[identityCapacity]};
    std::unique_ptr<unsigned[]> identityOrder{new unsigned[identityCapacity]},identityNext{new unsigned[identityCapacity]};
    // Input-only scratch: callback lookup never takes this lock. Its storage
    // is admitted together with identity capacity before source writers.
    SRWLOCK identityPreparationLock=SRWLOCK_INIT;
    std::unique_ptr<BASS_MIDI_EVENT[]> identityScratchRows{new BASS_MIDI_EVENT[identityCapacity]};
    std::unique_ptr<unsigned[]> identityScratchOrder{new unsigned[identityCapacity]};
    unsigned identityLimit=identityCapacity;
    unsigned long long identityProduced=0,identityConsumed=0;
    std::atomic<unsigned long long> ambiguousCallbacks{0};
    std::array<Ticket,4096> tickets{};unsigned ticketHead=0,ticketCount=0;unsigned long long ticketSerial=0;
    std::atomic<unsigned> settleThread{0};std::atomic<bool> settleLocked{false};
    SRWLOCK recordsLock=SRWLOCK_INIT;
    NativeSelectionCheckpointArena* checkpointArena=nullptr;
    std::array<std::atomic<unsigned>,128> requestedAddress;
    std::array<std::atomic<unsigned long long>,128> requestedTokenReset;std::array<unsigned long long,128> tokenResetSeen{};
    struct Channel {unsigned bank=0,lsb=0,program=0;bool drums=false,tokenValid=false,tokenKnown=false;int tokenBank=-1,tokenProgram=-1;unsigned tokenLSB=0;bool tokenDrums=false;
        std::vector<unsigned> profile,baseline,touched,modCurrent;std::vector<int> applied;std::vector<unsigned char> profilePresent,baselinePresent,appliedPresent,touchedPresent,modPresent;};
    std::array<Channel,128> channels;
    struct Metadata {
        bool profileDictionaryPresent=false;
        unsigned long long tokenRevision=0,tokenKeyRevision=0,sessionClear=0,routingClear=0;
        std::array<int,5> controllers{{64,64,64,64,64}};
    };
    std::array<Metadata,128> metadata;
    bool metadataReady=false;unsigned long long profileRevision=0,streamGeneration=0;
    std::vector<NativeSelectionKeyRevision> keyRevisions;
    // Additive control-side donor image. Existing SYNCPROC cells remain one
    // CAS-protected serial48/raw16 word; full-DWORD accepted selection rows
    // are written inside the EXISTING recordsLock, never a new callback lock.
    struct ProvenanceBinding {NativeMidiState* state;unsigned event,slot,role,field,source,phase;unsigned long long activation;};
    struct ProvenanceAccepted {unsigned event,field;std::array<unsigned long long,128> serial{};std::array<unsigned,128> raw{},kind{};};
    std::vector<ProvenanceBinding> provenanceBindings;
    std::vector<NativeMidiState*> provenanceRoots;
    std::vector<std::array<unsigned long long,11>> provenanceSeeds;
    std::vector<ProvenanceAccepted> provenanceAccepted;
    std::vector<unsigned long long> provenanceScratch;
    bool provenanceReady=false;unsigned provenanceSources=0,provenancePhases=0,provenanceFields=0;
    unsigned long long provenanceActivation=0,provenanceOriginClear=0,provenanceCoreWords=0;
    unsigned long long keyRevision(int bank,int program)const{
        if(bank==-1&&program==-1)return 0; // Python unresolved identity uses None, not tuple(-1,-1).
        const auto identity=std::make_pair(static_cast<unsigned>(bank),static_cast<unsigned>(program));
        const auto row=std::lower_bound(keyRevisions.begin(),keyRevisions.end(),identity,
            [](const NativeSelectionKeyRevision& value,const std::pair<unsigned,unsigned>& key){return std::make_pair(value.bank,value.program)<key;});
        return row!=keyRevisions.end()&&row->bank==identity.first&&row->program==identity.second?row->revision:0;
    }
    void metadataWords(unsigned channel,unsigned long long* out)const{
        const auto& value=metadata[channel];const unsigned long long row[]{value.profileDictionaryPresent?1ull:0ull,value.tokenRevision,
            value.tokenKeyRevision,value.sessionClear,value.routingClear,static_cast<unsigned>(value.controllers[0]),
            static_cast<unsigned>(value.controllers[1]),static_cast<unsigned>(value.controllers[2]),
            static_cast<unsigned>(value.controllers[3]),static_cast<unsigned>(value.controllers[4])};std::copy_n(row,10,out);
    }
    struct Watch {NativeMidiState* state;unsigned field,slot,kind;std::array<unsigned long long,128> serial{};};
    std::vector<Watch> watches;std::array<std::vector<unsigned>,128> touchedOrder;
    std::vector<NativeSelectionRestore> restoration;std::vector<unsigned> restoreOrder;
    bool graphReady=false,graphEnabled=false;unsigned graphChannels=0,userMix=0,userDSP=0;
    std::array<unsigned,128> graphMixes{},graphDSPs{},graphActive{};
    std::array<LegacyDelayState*,128> graphTaps{};LegacyDelayState* graphWet=nullptr;
    HSTREAM(WINAPI* getMix)(HSTREAM,DWORD)=nullptr;HDSP(WINAPI* setDSP)(DWORD,DSPPROC*,void*,int)=nullptr;
    BOOL(WINAPI* removeDSP)(DWORD,HDSP)=nullptr;BOOL(WINAPI* freeMix)(HSTREAM)=nullptr;DSPPROC* delayProc=nullptr;
    NativeSelectionState(NativeMidiClock* shared,unsigned handle,void* setter,void* reader,void* resolver,void* failure):clock(shared),stream(handle),
        emit(reinterpret_cast<BOOL(WINAPI*)(HSTREAM,DWORD,DWORD,DWORD)>(setter)),get(reinterpret_cast<DWORD(WINAPI*)(HSTREAM,DWORD,DWORD)>(reader)),
        source(reinterpret_cast<BOOL(WINAPI*)(HSTREAM,DWORD,BASS_MIDI_FONT*)>(resolver)),error(reinterpret_cast<int(WINAPI*)()>(failure)){
        recordPages.reserve(recordCapacity/recordPageSize);for(unsigned i=0;i<recordCapacity/recordPageSize;++i)recordPages.emplace_back(new NativeSelectionRecord[recordPageSize]);
        for(auto& address:requestedAddress)address.store(0,std::memory_order_relaxed);for(auto& reset:requestedTokenReset)reset.store(0,std::memory_order_relaxed);clock->retain();}
    ~NativeSelectionState(){if(capacityThread){AcquireSRWLockExclusive(&capacityLock);capacityClosing=true;WakeAllConditionVariable(&capacityChanged);ReleaseSRWLockExclusive(&capacityLock);WaitForSingleObject(capacityThread,INFINITE);CloseHandle(capacityThread);}for(auto& watch:watches)watch.state->release();for(auto* root:provenanceRoots)root->release();for(auto* tap:graphTaps)if(tap)delayMidiRelease(tap);if(graphWet)delayMidiRelease(graphWet);clock->release();}
    void retain(){references.fetch_add(1,std::memory_order_relaxed);}
    void release(){if(references.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
    NativeSelectionRecord& record(unsigned long long index){const auto slot=static_cast<unsigned>(index%recordLimit.load(std::memory_order_acquire));return recordPages[slot/recordPageSize][slot%recordPageSize];}
    bool room(unsigned count)const{const auto leases=reserved.load(std::memory_order_acquire);
        return produced.load(std::memory_order_acquire)-consumed.load(std::memory_order_acquire)+leases+count<=recordLimit.load(std::memory_order_acquire)&&
            (!checkpointArena||checkpointArena->produced.load(std::memory_order_acquire)-checkpointArena->consumed.load(std::memory_order_acquire)+leases+count<=checkpointArena->capacity);}
    bool reserve(unsigned count){auto prior=reserved.load(std::memory_order_acquire);
        for(;;){if(produced.load(std::memory_order_acquire)-consumed.load(std::memory_order_acquire)+prior+count>recordLimit.load(std::memory_order_acquire)||
            (checkpointArena&&checkpointArena->produced.load(std::memory_order_acquire)-checkpointArena->consumed.load(std::memory_order_acquire)+prior+count>checkpointArena->capacity))return false;
            if(reserved.compare_exchange_weak(prior,prior+count,std::memory_order_acq_rel)){queuedReservation.fetch_add(count,std::memory_order_release);return true;}}}
    void cancelReservation(unsigned count){auto prior=queuedReservation.load(std::memory_order_acquire);
        while(prior){const auto amount=std::min(prior,static_cast<unsigned long long>(count));
            if(queuedReservation.compare_exchange_weak(prior,prior-amount,std::memory_order_acq_rel)){reserved.fetch_sub(amount,std::memory_order_release);return;}} }
    bool reserveUnticketed(unsigned count){AcquireSRWLockExclusive(&ticketsLock);const bool accepted=reserve(count);
        if(accepted)unticketedReservation.fetch_add(count,std::memory_order_release);ReleaseSRWLockExclusive(&ticketsLock);return accepted;}
    bool claimUnticketed(unsigned count){AcquireSRWLockExclusive(&ticketsLock);trimTickets();
        // Legacy batch budgets have no identities. They cannot fund a callback
        // while any identifiable submission still owns a queued reservation.
        const auto pending=unticketedReservation.load(std::memory_order_acquire);
        const bool accepted=!ticketCount&&pending>=count;if(accepted)unticketedReservation.fetch_sub(count,std::memory_order_release);
        ReleaseSRWLockExclusive(&ticketsLock);return accepted;}
    struct StoragePreparation {
        unsigned records=0,identity=0;NativeSelectionCheckpointArena* arena=nullptr;
        std::vector<std::unique_ptr<NativeSelectionRecord[]>> pages;
        std::unique_ptr<BASS_MIDI_EVENT[]> identities;
        std::unique_ptr<unsigned char[]> claimed;
        std::unique_ptr<unsigned[]> order,next;
        std::unique_ptr<BASS_MIDI_EVENT[]> scratchRows;
        std::unique_ptr<unsigned[]> scratchOrder;
        std::unique_ptr<NativeSelectionCheckpointArena> checkpoints;
    };
    StoragePreparation retainedPreparation;
    // Opt-in native staging owner. This thread only allocates immutable storage;
    // the original actor publishes it under its same-thread BASS ChannelLock.
    SRWLOCK capacityLock=SRWLOCK_INIT;CONDITION_VARIABLE capacityChanged=CONDITION_VARIABLE_INIT;
    HANDLE capacityThread=nullptr;bool capacityClosing=false,capacityRequested=false,capacityReady=false,capacityAccepted=false;
    unsigned capacityEvents=0,capacityExtra=0;StoragePreparation capacityPrepared;
    unsigned long long capacityStages=0,capacityFailures=0;
    static unsigned __stdcall stageCapacityWorker(void* pointer){auto& s=*static_cast<NativeSelectionState*>(pointer);
        for(;;){AcquireSRWLockExclusive(&s.capacityLock);
            while(!s.capacityRequested&&!s.capacityClosing)SleepConditionVariableSRW(&s.capacityChanged,&s.capacityLock,INFINITE,0);
            if(s.capacityClosing){ReleaseSRWLockExclusive(&s.capacityLock);return 0;}
            const auto events=s.capacityEvents,extra=s.capacityExtra;ReleaseSRWLockExclusive(&s.capacityLock);
            StoragePreparation prepared;const bool accepted=s.stageCapacity(events,extra,prepared);
            AcquireSRWLockExclusive(&s.capacityLock);s.capacityPrepared=std::move(prepared);s.capacityAccepted=accepted;s.capacityReady=true;s.capacityRequested=false;
            ++s.capacityStages;if(!accepted)++s.capacityFailures;WakeAllConditionVariable(&s.capacityChanged);
            const bool closing=s.capacityClosing;ReleaseSRWLockExclusive(&s.capacityLock);if(closing)return 0;
        }}
    bool prepareNativeCapacity(){AcquireSRWLockExclusive(&capacityLock);
        if(!capacityThread&&!capacityClosing)capacityThread=reinterpret_cast<HANDLE>(_beginthreadex(nullptr,0,&NativeSelectionState::stageCapacityWorker,this,0,nullptr));
        const bool ready=capacityThread!=nullptr;ReleaseSRWLockExclusive(&capacityLock);return ready;}
    bool stageCapacityOwned(unsigned events,unsigned extra,StoragePreparation& prepared){
        if(!capacityThread)return stageCapacity(events,extra,prepared);
        AcquireSRWLockExclusive(&capacityLock);
        if(capacityClosing||capacityRequested||capacityReady){ReleaseSRWLockExclusive(&capacityLock);return false;}
        capacityEvents=events;capacityExtra=extra;capacityRequested=true;capacityReady=false;WakeAllConditionVariable(&capacityChanged);
        while(!capacityReady&&!capacityClosing)SleepConditionVariableSRW(&capacityChanged,&capacityLock,INFINITE,0);
        const bool accepted=!capacityClosing&&capacityAccepted;if(accepted)prepared=std::move(capacityPrepared);
        capacityRequested=false;capacityReady=false;WakeAllConditionVariable(&capacityChanged);ReleaseSRWLockExclusive(&capacityLock);return accepted;
    }
    std::atomic<bool> preparationHeld{false};
    bool preparationPending()const{return preparationHeld.load(std::memory_order_acquire);}
    void retainPreparation(StoragePreparation&& prepared){const bool held=!prepared.pages.empty()||prepared.identities||prepared.checkpoints;
        retainedPreparation=std::move(prepared);preparationHeld.store(held,std::memory_order_release);}
    void releasePreparation(){retainedPreparation=StoragePreparation{};preparationHeld.store(false,std::memory_order_release);}
    bool stageCapacity(unsigned events,unsigned extraRecords,StoragePreparation& prepared){
        // INPUT SIDE. Only the small sizing snapshot holds journal locks;
        // allocation/initialisation runs without ChannelLock or callback locks.
        AcquireSRWLockExclusive(&ticketsLock);trimTickets();AcquireSRWLockShared(&recordsLock);
        const auto wanted=produced.load()-consumed.load()+reserved.load()+static_cast<unsigned long long>(events)*need()+extraRecords;
        const auto identityWanted=identityProduced-identityConsumed+events;
        auto* arena=checkpointArena;
        const auto checkpointWanted=arena?arena->produced.load()-arena->consumed.load()+reserved.load()+static_cast<unsigned long long>(events)*need()+extraRecords:0;
        const unsigned oldRecords=recordLimit.load(),oldIdentity=identityLimit;
        const unsigned oldCheckpoints=arena?arena->capacity:0,checkpointFields=arena?arena->fields:0,checkpointWatches=arena?arena->watches:0;
        ReleaseSRWLockShared(&recordsLock);ReleaseSRWLockExclusive(&ticketsLock);
        if(wanted>INT_MAX-recordPageSize||identityWanted>UINT_MAX||checkpointWanted>INT_MAX)return false;
        prepared.records=std::max(oldRecords,static_cast<unsigned>((wanted+recordPageSize-1)/recordPageSize)*recordPageSize);
        prepared.identity=std::max(oldIdentity,static_cast<unsigned>(identityWanted));prepared.arena=arena;
        try{
            if(prepared.records>oldRecords){prepared.pages.reserve(prepared.records/recordPageSize);
                for(unsigned page=0;page<prepared.records/recordPageSize;++page)prepared.pages.emplace_back(new NativeSelectionRecord[recordPageSize]);}
            if(prepared.identity>oldIdentity){prepared.identities.reset(new BASS_MIDI_EVENT[prepared.identity]);prepared.claimed.reset(new unsigned char[prepared.identity]);
                prepared.order.reset(new unsigned[prepared.identity]);prepared.next.reset(new unsigned[prepared.identity]);
                prepared.scratchRows.reset(new BASS_MIDI_EVENT[prepared.identity]);prepared.scratchOrder.reset(new unsigned[prepared.identity]);}
            if(arena&&checkpointWanted>oldCheckpoints)prepared.checkpoints=std::make_unique<NativeSelectionCheckpointArena>(this,static_cast<unsigned>(checkpointWanted),checkpointFields,checkpointWatches);
            return true;
        }catch(const std::bad_alloc&){return false;}
    }
    bool publishCapacity(unsigned events,unsigned extraRecords,StoragePreparation& prepared){
        // Under ChannelLock: revalidate and copy only CURRENT unread values.
        // The preparation owns displaced old buffers until AFTER unlock.
        // Never wait for an input-side sort while blocking PCM. A competing
        // preparer retains its original input and retries this publication.
        if(!TryAcquireSRWLockExclusive(&identityPreparationLock))return false;
        AcquireSRWLockExclusive(&ticketsLock);trimTickets();AcquireSRWLockExclusive(&recordsLock);
        const auto head=consumed.load(),tail=produced.load();
        const auto wanted=tail-head+reserved.load()+static_cast<unsigned long long>(events)*need()+extraRecords;
        const auto identityWanted=identityProduced-identityConsumed+events;
        const auto checkpointWanted=checkpointArena?checkpointArena->produced.load()-checkpointArena->consumed.load()+reserved.load()+static_cast<unsigned long long>(events)*need()+extraRecords:0;
        bool accepted=checkpointArena==prepared.arena&&wanted<=std::max(recordLimit.load(),prepared.records)&&identityWanted<=std::max(identityLimit,prepared.identity)&&
            (!checkpointArena||checkpointWanted<=std::max(checkpointArena->capacity,prepared.checkpoints?prepared.checkpoints->capacity:0u));
        if(accepted){
            if(prepared.records>recordLimit.load()){
                for(auto index=head;index<tail;++index){const auto slot=static_cast<unsigned>(index%prepared.records);prepared.pages[slot/recordPageSize][slot%recordPageSize]=record(index);}
                recordPages.swap(prepared.pages);recordLimit.store(prepared.records,std::memory_order_release);}
            if(prepared.identity>identityLimit){
                for(auto index=identityConsumed;index<identityProduced;++index){prepared.identities[index%prepared.identity]=identities[index%identityLimit];prepared.claimed[index%prepared.identity]=identityClaimed[index%identityLimit];
                    prepared.order[index%prepared.identity]=identityOrder[index%identityLimit];prepared.next[index%prepared.identity]=identityNext[index%identityLimit];}
                identities.swap(prepared.identities);identityClaimed.swap(prepared.claimed);identityOrder.swap(prepared.order);identityNext.swap(prepared.next);
                identityScratchRows.swap(prepared.scratchRows);identityScratchOrder.swap(prepared.scratchOrder);identityLimit=prepared.identity;}
            if(checkpointArena&&prepared.checkpoints&&prepared.checkpoints->capacity>checkpointArena->capacity){
                auto& old=*checkpointArena;auto& nextArena=*prepared.checkpoints;
                for(auto index=old.consumed.load();index<old.produced.load();++index){const auto from=static_cast<size_t>(index%old.capacity),to=static_cast<size_t>(index%nextArena.capacity);
                    std::copy_n(old.headers.get()+from*24,24,nextArena.headers.get()+to*24);std::copy_n(old.cells.get()+from*old.fields*6,old.fields*6,nextArena.cells.get()+to*old.fields*6);
                    std::copy_n(old.orders.get()+from*old.fields,old.fields,nextArena.orders.get()+to*old.fields);std::copy_n(old.graphs.get()+from*514,514,nextArena.graphs.get()+to*514);
                    std::copy_n(old.watchRows.get()+from*old.watches*2,old.watches*2,nextArena.watchRows.get()+to*old.watches*2);}
                for(auto index=old.consumed.load();index<old.produced.load();++index)
                    std::copy_n(old.metadata.get()+static_cast<size_t>(index%old.capacity)*11,11,nextArena.metadata.get()+static_cast<size_t>(index%nextArena.capacity)*11);
                old.headers.swap(nextArena.headers);old.cells.swap(nextArena.cells);old.orders.swap(nextArena.orders);old.graphs.swap(nextArena.graphs);old.watchRows.swap(nextArena.watchRows);old.metadata.swap(nextArena.metadata);std::swap(old.capacity,nextArena.capacity);}
        }
        ReleaseSRWLockExclusive(&recordsLock);ReleaseSRWLockExclusive(&ticketsLock);ReleaseSRWLockExclusive(&identityPreparationLock);return accepted;
    }
    void cancelUnticketed(unsigned count){AcquireSRWLockExclusive(&ticketsLock);
        const auto amount=std::min(static_cast<unsigned long long>(count),unticketedReservation.load(std::memory_order_acquire));
        unticketedReservation.fetch_sub(amount,std::memory_order_release);cancelReservation(static_cast<unsigned>(amount));ReleaseSRWLockExclusive(&ticketsLock);}
    void write(unsigned ch,unsigned event,unsigned raw,unsigned kind,unsigned key,int code=0){
        AcquireSRWLockExclusive(&recordsLock);
        const auto index=produced.load(std::memory_order_relaxed);const auto serial=clock->serial.fetch_add(1,std::memory_order_acq_rel)+1;
        if(ch<128){auto& meta=metadata[ch];
            if(kind==18)meta.sessionClear=serial;
            if(kind==20||(kind==6&&event==MIDI_EVENT_DRUMS)){meta.routingClear=serial;meta.controllers.fill(64);}
            if(kind==28)meta.profileDictionaryPresent=true;
            if(provenanceReady&&((kind>=1&&kind<=5)||((kind==22||kind==29||kind==30||kind==31)&&key))){
                for(auto& row:provenanceAccepted)if(row.event==event){row.serial[ch]=serial;row.raw[ch]=raw;row.kind[ch]=kind;break;}}
        }
        // Preserve pre-error and source/reset stage state, not merely the final
        // successful value. Existing records/audio ordering remains identical.
        if(checkpointArena&&(kind==9||kind==11||kind==15||kind==18||kind==19||kind==20||(kind>=22&&kind<=31)))
            checkpointArena->capture(*this,serial,index,ch,event,raw,kind,key,code);
        record(index)={serial,ch,event,raw,kind,key,code};produced.store(index+1,std::memory_order_release);
        ReleaseSRWLockExclusive(&recordsLock);
    }
    unsigned need()const{
        // Independent writer-chain bound, no P/E exclusivity or stable source
        // assumption: up to2 profile passes,3 seed passes(each <=3 records per
        // field),2 touched passes, selection/tone/token records9. Graph may
        // remove/recreate128 channel taps plus user routing/failure records.
        // Every resolved source may differ between reads, so union ALL
        // immutable prepared sources plus old seeded state. Never count
        // only the currently requested preset or discard failed writers.
        return 11u+4u*profileBound+9u*extendedBound+2u*touchedBound+(graphEnabled?258u:0u);
    }
    void trimTickets(){while(ticketCount&&!tickets[ticketHead].remaining){
        const auto& expected=ticketEvents[ticketHead];identityConsumed=expected.first+expected.count;
        ticketHead=(ticketHead+1)%static_cast<unsigned>(tickets.size());--ticketCount;}}
    bool failedTicket(){
        AcquireSRWLockShared(&ticketsLock);bool failed=false;
        for(unsigned i=0;i<ticketCount;++i){const auto& row=tickets[(ticketHead+i)%static_cast<unsigned>(tickets.size())];
            if((row.state==2||ticketEvents[(ticketHead+i)%static_cast<unsigned>(tickets.size())].ambiguous)&&row.remaining){failed=true;break;}}
        ReleaseSRWLockShared(&ticketsLock);return failed;
    }
    unsigned long long ticket(unsigned events){
        if(!events||events>recordLimit.load(std::memory_order_acquire)/need())return 0;AcquireSRWLockExclusive(&ticketsLock);trimTickets();
        // A failed submission may still own an asynchronously queued prefix.
        // No later callback may consume its unprocessed reservation as its own.
        for(unsigned i=0;i<ticketCount;++i){const auto& prior=tickets[(ticketHead+i)%static_cast<unsigned>(tickets.size())];
            if((prior.state==2||ticketEvents[(ticketHead+i)%static_cast<unsigned>(tickets.size())].ambiguous)&&prior.remaining){ReleaseSRWLockExclusive(&ticketsLock);return 0;}}
        if(ticketCount==tickets.size()||events>identityLimit-(identityProduced-identityConsumed)||!reserve(events*need())){ReleaseSRWLockExclusive(&ticketsLock);return 0;}
        const auto slot=(ticketHead+ticketCount)%static_cast<unsigned>(tickets.size());
        ticketEvents[slot]={identityProduced,events,0,false,false};
        for(unsigned i=0;i<events;++i){const auto cell=(identityProduced+i)%identityLimit;
            identities[cell]={};identityClaimed[cell]=0;identityOrder[cell]=i;identityNext[cell]=i;}
        identityProduced+=events;
        auto& row=tickets[slot];row={++ticketSerial,events,0,events,0};++ticketCount;
        const auto id=row.id;ReleaseSRWLockExclusive(&ticketsLock);return id;
    }
    bool growTicket(unsigned long long id){AcquireSRWLockExclusive(&ticketsLock);
        for(unsigned i=0;i<ticketCount;++i){auto& row=tickets[(ticketHead+i)%static_cast<unsigned>(tickets.size())];
            if(row.id==id&&!row.state){auto& expected=ticketEvents[(ticketHead+i)%static_cast<unsigned>(tickets.size())];
                if(expected.first+expected.count!=identityProduced||identityProduced-identityConsumed==identityLimit||!reserve(need())){ReleaseSRWLockExclusive(&ticketsLock);return false;}
                const auto cell=identityProduced%identityLimit;identities[cell]={};identityClaimed[cell]=0;
                identityOrder[cell]=expected.count;identityNext[cell]=expected.count;++identityProduced;++expected.count;
                ++row.requested;++row.remaining;ReleaseSRWLockExclusive(&ticketsLock);return true;}}
        ReleaseSRWLockExclusive(&ticketsLock);return false;
    }
    bool growPreparedTicketTo(unsigned long long id,unsigned count){
        AcquireSRWLockExclusive(&ticketsLock);
        for(unsigned i=0;i<ticketCount;++i){auto& row=tickets[(ticketHead+i)%static_cast<unsigned>(tickets.size())];
            if(row.id!=id)continue;auto& expected=ticketEvents[(ticketHead+i)%static_cast<unsigned>(tickets.size())];
            if(row.state||row.processed||count<row.requested||expected.first+expected.count!=identityProduced){ReleaseSRWLockExclusive(&ticketsLock);return false;}
            const unsigned added=count-row.requested;
            if(added>identityLimit-(identityProduced-identityConsumed)||!reserve(added*need())){ReleaseSRWLockExclusive(&ticketsLock);return false;}
            for(unsigned ordinal=0;ordinal<added;++ordinal){const auto cell=identityProduced%identityLimit;
                identities[cell]={};identityClaimed[cell]=0;identityOrder[cell]=expected.count;identityNext[cell]=expected.count;
                ++identityProduced;++expected.count;}
            row.requested=count;row.remaining+=added;ReleaseSRWLockExclusive(&ticketsLock);return true;}
        ReleaseSRWLockExclusive(&ticketsLock);return false;
    }
    bool canAdmitEvents(unsigned events,unsigned extraRecords=0){AcquireSRWLockExclusive(&ticketsLock);trimTickets();
        const bool available=ticketCount<tickets.size()&&events<=identityLimit-(identityProduced-identityConsumed)&&
            events<=recordLimit.load()/need()&&extraRecords<=recordLimit.load()-events*need()&&room(events*need()+extraRecords);
        ReleaseSRWLockExclusive(&ticketsLock);return available;
    }
    bool identifyTicket(unsigned long long id,const BASS_MIDI_EVENT* rows,unsigned count,unsigned position,const unsigned* preparedOrder=nullptr){
        if((count&&!rows)||position>3)return false;AcquireSRWLockExclusive(&ticketsLock);
        for(unsigned i=0;i<ticketCount;++i){const auto slot=(ticketHead+i)%static_cast<unsigned>(tickets.size());const auto& row=tickets[slot];
            if(row.id!=id)continue;if(row.state||row.processed||row.requested!=count){ReleaseSRWLockExclusive(&ticketsLock);return false;}
            auto& expected=ticketEvents[slot];for(unsigned j=0;j<count;++j){const auto cell=(expected.first+j)%identityLimit;identities[cell]=rows[j];identityClaimed[cell]=0;}
            expected.position=position;expected.qualified=true;
            if(preparedOrder){for(unsigned rank=0;rank<count;++rank){identityOrder[(expected.first+rank)%identityLimit]=preparedOrder[rank];identityNext[(expected.first+rank)%identityLimit]=rank;}expected.indexed=true;}
            else buildIdentityIndex(expected);ReleaseSRWLockExclusive(&ticketsLock);return true;}
        ReleaseSRWLockExclusive(&ticketsLock);return false;
    }
    bool identifyLastTicketEvent(unsigned long long id,unsigned event,unsigned channel,unsigned raw){
        AcquireSRWLockExclusive(&ticketsLock);
        for(unsigned i=0;i<ticketCount;++i){const auto slot=(ticketHead+i)%static_cast<unsigned>(tickets.size());const auto& row=tickets[slot];
            if(row.id!=id||row.state)continue;auto& expected=ticketEvents[slot];
            const auto cell=(expected.first+expected.count-1)%identityLimit;identities[cell]={event,raw,channel,0,0};identityClaimed[cell]=0;
            expected.position=0;expected.qualified=true;expected.indexed=false;ReleaseSRWLockExclusive(&ticketsLock);return true;}
        ReleaseSRWLockExclusive(&ticketsLock);return false;
    }
    using IdentityKey=std::tuple<unsigned,unsigned,unsigned,unsigned,unsigned>;
    IdentityKey identityKey(const TicketEvents& expected,const BASS_MIDI_EVENT& event,int queryKind=-1)const{
        unsigned kind=0,time=0;
        if(expected.position==1)time=event.pos;
        else if(expected.position==2){kind=1;time=event.tick;}
        else if(expected.position==3){kind=queryKind<0?(event.tick?1u:0u):static_cast<unsigned>(queryKind);time=kind?event.tick:event.pos;}
        return {event.event,event.chan,event.param,kind,time};
    }
    bool stageIdentityOrder(const BASS_MIDI_EVENT* rows,unsigned count,unsigned position,std::vector<unsigned>& order){
        try{order.resize(count);for(unsigned i=0;i<count;++i)order[i]=i;TicketEvents expected;expected.position=position;
            std::sort(order.begin(),order.end(),[&](unsigned left,unsigned right){const auto a=identityKey(expected,rows[left]),b=identityKey(expected,rows[right]);return a==b?left<right:a<b;});return true;
        }catch(const std::bad_alloc&){return false;}
    }
    void buildIdentityIndex(TicketEvents& expected){
        // Input-side in-place heap sort over ring-relative indexes. No new
        // storage or callback allocation, including a wrapped ticket range.
        auto at=[&](unsigned rank)->unsigned&{return identityOrder[(expected.first+rank)%identityLimit];};
        auto less=[&](unsigned left,unsigned right){const auto a=identityKey(expected,identities[(expected.first+left)%identityLimit]);
            const auto b=identityKey(expected,identities[(expected.first+right)%identityLimit]);return a==b?left<right:a<b;};
        for(unsigned rank=0;rank<expected.count;++rank){at(rank)=rank;identityNext[(expected.first+rank)%identityLimit]=rank;}
        auto sift=[&](unsigned root,unsigned end){while(root<end/2){unsigned child=root*2+1;
            if(child+1<end&&less(at(child),at(child+1)))++child;
            if(!less(at(root),at(child)))break;std::swap(at(root),at(child));root=child;}};
        for(unsigned root=expected.count/2;root>0;)sift(--root,expected.count);
        for(unsigned end=expected.count;end>1;){std::swap(at(0),at(--end));sift(0,end);}
        expected.indexed=true;
    }
    bool finalizeTicketIndex(unsigned long long id){
        unsigned count=0,position=0;unsigned long long first=0;bool found=false;
        AcquireSRWLockShared(&ticketsLock);
        for(unsigned i=0;i<ticketCount;++i){const auto slot=(ticketHead+i)%static_cast<unsigned>(tickets.size());const auto& row=tickets[slot];const auto& expected=ticketEvents[slot];
            if(row.id==id&&!row.state){if(!expected.qualified||expected.indexed){ReleaseSRWLockShared(&ticketsLock);return true;}count=expected.count;position=expected.position;first=expected.first;found=true;break;}}
        ReleaseSRWLockShared(&ticketsLock);if(!found)return false;
        AcquireSRWLockExclusive(&identityPreparationLock);
        auto* rows=identityScratchRows.get();auto* order=identityScratchOrder.get();
        AcquireSRWLockShared(&ticketsLock);bool copied=false;
        for(unsigned i=0;i<ticketCount;++i){const auto slot=(ticketHead+i)%static_cast<unsigned>(tickets.size());const auto& row=tickets[slot];const auto& expected=ticketEvents[slot];
            if(row.id==id&&!row.state&&expected.first==first&&expected.count==count){for(unsigned j=0;j<count;++j)rows[j]=identities[(first+j)%identityLimit];copied=true;break;}}
        ReleaseSRWLockShared(&ticketsLock);if(!copied){ReleaseSRWLockExclusive(&identityPreparationLock);return false;}
        TicketEvents sorting;sorting.position=position;for(unsigned rank=0;rank<count;++rank)order[rank]=rank;
        std::sort(order,order+count,[&](unsigned left,unsigned right){const auto a=identityKey(sorting,rows[left]),b=identityKey(sorting,rows[right]);return a==b?left<right:a<b;});
        AcquireSRWLockExclusive(&ticketsLock);bool published=false;
        for(unsigned i=0;i<ticketCount;++i){const auto slot=(ticketHead+i)%static_cast<unsigned>(tickets.size());const auto& row=tickets[slot];auto& expected=ticketEvents[slot];
            if(row.id==id&&!row.state&&expected.first==first&&expected.count==count){for(unsigned rank=0;rank<count;++rank){identityOrder[(first+rank)%identityLimit]=order[rank];identityNext[(first+rank)%identityLimit]=rank;}expected.indexed=true;published=true;break;}}
        ReleaseSRWLockExclusive(&ticketsLock);ReleaseSRWLockExclusive(&identityPreparationLock);return published;
    }
    unsigned nextIdentity(TicketEvents& expected,unsigned rank){
        unsigned root=rank;while(root<expected.count){const unsigned next=identityNext[(expected.first+root)%identityLimit];if(next==root)break;root=next;}
        while(rank<expected.count){auto& next=identityNext[(expected.first+rank)%identityLimit];const unsigned prior=next;next=root;if(prior==rank)break;rank=prior;}
        return root;
    }
    unsigned findIdentity(TicketEvents& expected,const BASS_MIDI_EVENT& event,unsigned& rank){
        if(!expected.indexed)return expected.count;
        const unsigned queries=expected.position==3?2u:1u;
        for(unsigned kind=0;kind<queries;++kind){const auto key=identityKey(expected,event,expected.position==3?static_cast<int>(kind):-1);
            unsigned low=0,high=expected.count;while(low<high){const unsigned middle=low+(high-low)/2;
                const unsigned ordinal=identityOrder[(expected.first+middle)%identityLimit];
                if(identityKey(expected,identities[(expected.first+ordinal)%identityLimit])<key)low=middle+1;else high=middle;}
            low=nextIdentity(expected,low);if(low==expected.count)continue;
            const unsigned ordinal=identityOrder[(expected.first+low)%identityLimit];
            if(identityKey(expected,identities[(expected.first+ordinal)%identityLimit])==key){rank=low;return ordinal;}}
        return expected.count;
    }
    void claimIdentity(TicketEvents& expected,unsigned ordinal,unsigned rank){
        identityClaimed[(expected.first+ordinal)%identityLimit]=1;
        identityNext[(expected.first+rank)%identityLimit]=nextIdentity(expected,rank+1);
    }
    unsigned claimTicket(const BASS_MIDI_EVENT* event){AcquireSRWLockExclusive(&ticketsLock);trimTickets();
        if(!ticketCount){ReleaseSRWLockExclusive(&ticketsLock);return false;}
        Ticket* row=&tickets[ticketHead];
        if(selectionSubmittingState==this&&selectionSubmittingTicket){
            row=nullptr;
            for(unsigned i=0;i<ticketCount;++i){auto& candidate=tickets[(ticketHead+i)%static_cast<unsigned>(tickets.size())];
                if(candidate.id==selectionSubmittingTicket&&candidate.remaining){row=&candidate;
                    auto& expected=ticketEvents[(ticketHead+i)%static_cast<unsigned>(tickets.size())];
                    if(expected.qualified&&event){unsigned rank=0;const unsigned ordinal=findIdentity(expected,*event,rank);
                        if(ordinal<expected.count)claimIdentity(expected,ordinal,rank);}
                    break;}}
            if(!row){ReleaseSRWLockExclusive(&ticketsLock);return false;}
        }else if(event){
            Ticket* match=nullptr;unsigned matchedSlot=0,matchedEvent=0,matchedRank=0;bool qualified=false,ambiguous=false;
            for(unsigned i=0;i<ticketCount;++i){const auto slot=(ticketHead+i)%static_cast<unsigned>(tickets.size());auto& candidate=tickets[slot];auto& expected=ticketEvents[slot];
                if(!candidate.state||!candidate.remaining||!expected.qualified)continue;qualified=true;
                unsigned rank=0;const unsigned ordinal=findIdentity(expected,*event,rank);if(ordinal==expected.count)continue;
                if(match&&match!=&candidate){ambiguous=true;ticketEvents[matchedSlot].ambiguous=true;expected.ambiguous=true;continue;}
                match=&candidate;matchedSlot=slot;matchedEvent=ordinal;matchedRank=rank;}
            if(qualified){if(ambiguous){
                // The callback is certainly one of these original submitted
                // identities, but its individual ticket is unknowable. Fund
                // its journal from the shared reservation, without inventing
                // per-ticket processed counts. Empty-queue settlement alone
                // retires the ambiguous identities. A second reservation here
                // would fall back to Python at a full journal despite every
                // original callback already owning capacity.
                ambiguousCallbacks.fetch_add(1,std::memory_order_relaxed);
                ReleaseSRWLockExclusive(&ticketsLock);return 2;}
                if(!match){ReleaseSRWLockExclusive(&ticketsLock);return 0;}
                row=match;claimIdentity(ticketEvents[matchedSlot],matchedEvent,matchedRank);}
            else{ReleaseSRWLockExclusive(&ticketsLock);return false;}
        }else{ReleaseSRWLockExclusive(&ticketsLock);return false;}
        --row->remaining;++row->processed;ReleaseSRWLockExclusive(&ticketsLock);return true;
    }
    int settleTicket(unsigned long long id,unsigned state,bool queueDrained){AcquireSRWLockExclusive(&ticketsLock);
        for(auto& row:tickets)if(row.id==id){
            if(state<3){row.state=state;ReleaseSRWLockExclusive(&ticketsLock);return static_cast<int>(row.remaining);}
            if(row.state&&!queueDrained){ReleaseSRWLockExclusive(&ticketsLock);return -2;}
            const unsigned remaining=row.remaining;row.remaining=0;row.state=3;cancelReservation(remaining*need());trimTickets();
            ReleaseSRWLockExclusive(&ticketsLock);return static_cast<int>(remaining);
        }
        ReleaseSRWLockExclusive(&ticketsLock);return -1;
    }
    unsigned request(unsigned channel,unsigned event,unsigned raw,bool invalidate=true){
        const unsigned mask=event==MIDI_EVENT_BANK?255u:event==MIDI_EVENT_BANK_LSB?0xff00u:0xff0000u;
        const unsigned amount=std::min(raw,127u)<<(event==MIDI_EVENT_BANK?0:event==MIDI_EVENT_BANK_LSB?8:16);
        auto previous=requestedAddress[channel].load(std::memory_order_acquire);
        while(!requestedAddress[channel].compare_exchange_weak(previous,(previous&~mask)|amount,std::memory_order_acq_rel)){}
        if(invalidate&&event==MIDI_EVENT_PROGRAM)requestedTokenReset[channel].fetch_add(1,std::memory_order_release);
        return (previous&~mask)|amount;
    }
    void note(unsigned channel,const MidiNoteAction& action);
    NativeActivityIdentity noteIdentity(unsigned channel,const MidiNoteAction& action);
    bool send(unsigned ch,unsigned event,unsigned raw,unsigned kind,unsigned key){
        const bool accepted=emit(stream,ch,event,raw)!=FALSE;
        if(accepted){write(ch,event,raw,kind,key);if(key<fields.size()&&(fields[key].flags&4u)){channels[ch].modCurrent[key]=raw;channels[ch].modPresent[key]=1;}}
        else{failures.fetch_add(1,std::memory_order_relaxed);write(ch,event,raw,9,key,error());}return accepted;
    }
    const NativeSelectionPreset* preset(int bank,int program)const{
        for(const auto& row:presets)if(row.bank==static_cast<unsigned>(bank)&&row.program==static_cast<unsigned>(program))return &row;return nullptr;
    }
    const NativeSelectionPreset& current(unsigned channel,int& bank,int& program){
        auto& ch=channels[channel];bank=static_cast<int>(ch.drums?128u:ch.bank);program=static_cast<int>(ch.program);
        if(ch.lsb){BASS_MIDI_FONT active{};
            if(!source(stream,channel,&active)||!active.font){bank=program=-1;return fallback;}bank=active.bank;program=active.preset;}
        const auto* p=preset(bank,program);return p?*p:fallback;
    }
    void profile(unsigned channel,const NativeSelectionPreset& row,bool force){
        auto& ch=channels[channel];
        for(unsigned field=0;field<fields.size();++field){const auto& info=fields[field];if(!(info.flags&1u))continue;
            const NativeSelectionValue* chosen=nullptr;
            if(!(info.flags&8u)||ch.drums)for(unsigned i=row.profileFirst;i<row.profileFirst+row.profileCount;++i)if(values[i].field==field){chosen=&values[i];break;}
            const unsigned raw=chosen?chosen->raw:info.neutral;const int amount=chosen?chosen->amount:info.neutralAmount;
            const bool previous=ch.profilePresent[field]!=0;const int old=previous?static_cast<int>(ch.profile[field]):info.neutralAmount;
            if(chosen||previous){if(force||old!=amount)send(channel,info.event,raw,2,field);write(channel,info.event,static_cast<unsigned>(amount),7,field,chosen?1:0);}
            ch.profile[field]=static_cast<unsigned>(amount);ch.profilePresent[field]=chosen?1u:0u;
        }
        // Source publishes a completed profile writer even when it is empty.
        write(channel,0,0,28,0);
    }
    bool seeds(unsigned channel,const NativeSelectionPreset& row,bool force){
        auto& ch=channels[channel];
        // Restore removed fields first, in the original applied insertion order
        // supplied by the prepared field ordering. A failed restore still pops.
        unsigned oldMask=0,newMask=0,ordinal=0;for(unsigned field=0;field<fields.size();++field)if(fields[field].flags&2u){if(ch.appliedPresent[field])oldMask|=1u<<ordinal;++ordinal;}
        ordinal=0;for(unsigned field=0;field<fields.size();++field)if(fields[field].flags&2u){for(unsigned i=row.extendedFirst;i<row.extendedFirst+row.extendedCount;++i)if(values[i].field==field){newMask|=1u<<ordinal;break;}++ordinal;}
        const NativeSelectionRestore* restore=nullptr;
        for(const auto& candidate:restoration)if(candidate.oldMask==oldMask&&candidate.newMask==newMask){restore=&candidate;break;}
        if((oldMask&~newMask)&&!restore){write(channel,0,oldMask,11,UINT_MAX,-2);return false;}
        for(unsigned index=0;restore&&index<restore->count;++index){const unsigned field=restoreOrder[restore->first+index];const auto& info=fields[field];
            const NativeSelectionValue* chosen=nullptr;
            for(unsigned i=row.extendedFirst;i<row.extendedFirst+row.extendedCount;++i)if(values[i].field==field){chosen=&values[i];break;}
            if(ch.appliedPresent[field]&&!chosen){const unsigned baseline=ch.baseline[field];
                // PresetExpressionSeeds.apply pops native baseline BEFORE the
                // restore emitter/failure sink, but applied only AFTER it.
                ch.baselinePresent[field]=0;write(channel,info.event,0,10,field,0);
                send(channel,info.event,baseline,5,field);
                ch.appliedPresent[field]=0;write(channel,info.event,0,8,field,0);}
        }
        for(unsigned i=row.extendedFirst;i<row.extendedFirst+row.extendedCount;++i){const auto& chosen=values[i];const unsigned field=chosen.field;const auto& info=fields[field];
            if(!ch.baselinePresent[field]){unsigned raw=(info.flags&4u)?(ch.modPresent[field]?ch.modCurrent[field]:info.neutral):get(stream,channel,info.event);
                if(!(info.flags&4u)&&raw==UINT_MAX){write(channel,info.event,0,11,field,-1);return false;}
                ch.baseline[field]=raw;ch.baselinePresent[field]=1;write(channel,info.event,raw,10,field,1);}
            if(force||!ch.appliedPresent[field]||ch.applied[field]!=chosen.amount){if(send(channel,info.event,chosen.raw,2,field)&&(info.flags&4u)){ch.modCurrent[field]=chosen.raw;ch.modPresent[field]=1;}}
            ch.applied[field]=chosen.amount;ch.appliedPresent[field]=1;write(channel,info.event,static_cast<unsigned>(chosen.amount),8,field,1);
        }
        return true;
    }
    bool reapply(unsigned channel){
        auto& ch=channels[channel];int bank=-1,program=-1;current(channel,bank,program);
        int ignoredBank=-1,ignoredProgram=-1;const auto& row=current(channel,ignoredBank,ignoredProgram);
        const bool changed=!ch.tokenValid||ch.tokenBank!=bank||ch.tokenProgram!=program||ch.tokenLSB!=ch.lsb||ch.tokenDrums!=ch.drums;
        const bool force=!ch.tokenKnown||ch.tokenBank!=bank||ch.tokenProgram!=program||ch.tokenLSB!=ch.lsb||ch.tokenDrums!=ch.drums;
        ch.tokenValid=ch.tokenKnown=true;ch.tokenBank=bank;ch.tokenProgram=program;ch.tokenLSB=ch.lsb;ch.tokenDrums=ch.drums;
        metadata[channel].tokenRevision=profileRevision;metadata[channel].tokenKeyRevision=keyRevision(bank,program);
        if(changed){profile(channel,row,force);write(channel,static_cast<unsigned>(bank),static_cast<unsigned>(program),12,ch.lsb,ch.drums?1:0);}
        const auto& extended=current(channel,ignoredBank,ignoredProgram);if(!seeds(channel,extended,false))return false;
        for(unsigned field:touchedOrder[channel])if(ch.touchedPresent[field])send(channel,fields[field].event,ch.touched[field],4,field);
        return true;
    }
    bool apply(unsigned channel){
        int bank=-1,program=-1;const auto& row=current(channel,bank,program);
        constexpr unsigned events[]{MIDI_EVENT_MIXLEVEL,MIDI_EVENT_ATTACK,MIDI_EVENT_DECAY,MIDI_EVENT_RELEASE,MIDI_EVENT_CUTOFF,MIDI_EVENT_RESONANCE};
        for(unsigned i=0;i<6;++i){unsigned raw=row.flags&1u?(i?64u:100u):row.tone[i];unsigned kind=!(row.flags&1u)&&(row.flags&(4u<<i))?2u:1u;
            if(i){
                // Active SoundFontPresetRouting.blend / source004767E0.
                // Clamp at the product boundary, then preserve integer divide
                // order and the controller64 special case. MIXLEVEL is unblended.
                const unsigned base=std::min(raw,127u);const unsigned controller=static_cast<unsigned>(std::clamp(metadata[channel].controllers[i-1],0,127));
                raw=controller==64?base:controller<64?controller*base/64:base+(controller-63)*(127-base)/64;
                if(kind==1&&metadata[channel].controllers[i-1]!=64)kind=3; // Existing on_emit source, before controller clamp.
            }
            // Six legacy tone calls ignore refusal; they have no failure sink.
            if(emit(stream,channel,events[i],raw))write(channel,events[i],raw,kind,UINT_MAX);else failures.fetch_add(1,std::memory_order_relaxed);}
        return reapply(channel);
    }
    // Full-GM preparation operators. Session clear and routing clear are
    // separate source phases: routing zero occurs AFTER reset audio/DelayDefault.
    // Caller owns the kernel/control barrier and a sufficient record budget.
    void resetSession(unsigned channel){
        auto& ch=channels[channel];
        std::fill(ch.baselinePresent.begin(),ch.baselinePresent.end(),static_cast<unsigned char>(0));
        std::fill(ch.appliedPresent.begin(),ch.appliedPresent.end(),static_cast<unsigned char>(0));
        std::fill(ch.touchedPresent.begin(),ch.touchedPresent.end(),static_cast<unsigned char>(0));
        std::fill(ch.modPresent.begin(),ch.modPresent.end(),static_cast<unsigned char>(0));
        touchedOrder[channel].clear();
        // Do not clear profilePresent: source _profile_values survives, so a
        // removed profile field still gets its neutral restoration later.
        for(auto& watch:watches){const auto value=watch.state->cells[watch.state->offsets[watch.slot]+channel].load(std::memory_order_acquire);watch.serial[channel]=value>>16;}
        write(channel,0,0,18,0);
    }
    void resetRouting(unsigned channel){
        auto& ch=channels[channel];ch.bank=ch.lsb=ch.program=0;ch.tokenValid=ch.tokenKnown=false;
        requestedAddress[channel].store(ch.drums?0x1000000u:0u,std::memory_order_release);
        tokenResetSeen[channel]=requestedTokenReset[channel].load(std::memory_order_acquire);
        write(channel,0,0,20,0);
    }
    bool resetRefresh(unsigned channel){
        const unsigned bank=get(stream,channel,MIDI_EVENT_BANK),lsb=get(stream,channel,MIDI_EVENT_BANK_LSB),
            program=get(stream,channel,MIDI_EVENT_PROGRAM),drums=get(stream,channel,MIDI_EVENT_DRUMS);
        // Python source leaves its zero shadow in place on ANY invalid
        // readback; never invent a native maximum or partially publish fields.
        if(bank>=128||lsb>=128||program>=128||drums>=128)return true;
        auto& ch=channels[channel];const bool changed=bank!=ch.bank||lsb!=ch.lsb||program!=ch.program||(drums!=0)!=ch.drums;
        ch.bank=bank;ch.lsb=lsb;ch.program=program;ch.drums=drums!=0;ch.tokenValid=ch.tokenKnown=false;
        requestedAddress[channel].store(bank|(lsb<<8)|(program<<16)|(ch.drums?0x1000000u:0u),std::memory_order_release);
        write(channel,bank,lsb,19,program,ch.drums?1:0);
        return !changed||apply(channel);
    }
    bool resetApply(unsigned channel){return apply(channel);}
    int resetInstrument(unsigned channel,unsigned mask,unsigned bank,unsigned lsb,unsigned program){
        auto& ch=channels[channel];const bool drums=(mask&1u)?bank==128:ch.drums;
        bank=(mask&1u)?bank:(ch.drums?128u:ch.bank);lsb=(mask&2u)?lsb:ch.lsb;program=(mask&4u)?program:ch.program;
        // Source BOOL refusal stops this address chain only; scalar defaults
        // still follow. Graph/baseline exceptions stop the enclosing reset.
        if(drums!=ch.drums){
            if(!emit(stream,channel,MIDI_EVENT_DRUMS,drums?1u:0u))return 0;
            ch.drums=drums;requestedAddress[channel].store(ch.bank|(ch.lsb<<8)|(ch.program<<16)|(ch.drums?0x1000000u:0u),std::memory_order_release);
            write(channel,MIDI_EVENT_DRUMS,drums?1u:0u,27,0);
            if(channel==9)emit(stream,9,MIDI_EVENT_USERFX,drums?127u:0u);
            if(!syncGraph())return -8;
        }
        if(!drums){if(!emit(stream,channel,MIDI_EVENT_BANK,bank))return 0;
            ch.bank=bank;request(channel,MIDI_EVENT_BANK,bank,false);write(channel,MIDI_EVENT_BANK,bank,6,0);}
        if(!emit(stream,channel,MIDI_EVENT_BANK_LSB,lsb))return 0;
        ch.lsb=lsb;request(channel,MIDI_EVENT_BANK_LSB,lsb,false);write(channel,MIDI_EVENT_BANK_LSB,lsb,6,0);
        if(!emit(stream,channel,MIDI_EVENT_PROGRAM,program))return 0;
        ch.program=program;ch.tokenValid=ch.tokenKnown=false;
        requestedAddress[channel].store(ch.bank|(ch.lsb<<8)|(ch.program<<16)|(ch.drums?0x1000000u:0u),std::memory_order_release);
        write(channel,MIDI_EVENT_PROGRAM,program,6,0);
        return apply(channel)?1:-8;
    }
    void resetProfile(unsigned channel){int bank=-1,program=-1;const auto& row=current(channel,bank,program);profile(channel,row,true);}
    void observeWatches(unsigned channel){
        // Metadata-only ordered observer drain, shared by selection and Reset.
        auto& ch=channels[channel];
        for(;;){Watch* selected=nullptr;unsigned long long captured=0;
            for(auto& watch:watches){const auto value=watch.state->cells[watch.state->offsets[watch.slot]+channel].load(std::memory_order_acquire);
                if((value>>16)>watch.serial[channel]&&(!selected||(value>>16)<(captured>>16))){selected=&watch;captured=value;}}
            if(!selected)break;auto& watch=*selected;watch.serial[channel]=captured>>16;const unsigned value=static_cast<unsigned>(captured&65535u);
            if(watch.kind==0){ch.baseline[watch.field]=value;ch.baselinePresent[watch.field]=1;if(ch.touchedPresent[watch.field])ch.touched[watch.field]=value;}
            else if(watch.kind==1){if(!ch.touchedPresent[watch.field])touchedOrder[channel].push_back(watch.field);ch.touched[watch.field]=value;ch.touchedPresent[watch.field]=1;}
            else{ch.modCurrent[watch.field]=value;ch.modPresent[watch.field]=1;}}
    }
    bool resetExtensions(unsigned channel){
        // Original reapply consumes every earlier baseline/touched/MOD writer
        // before force-seeding the current preset; no new BASS input is sent.
        observeWatches(channel);
        if(!reapply(channel))return false;
        int bank=-1,program=-1;const auto& row=current(channel,bank,program);
        return seeds(channel,row,true)&&reapply(channel);
    }
    bool syncGraph(){
        if(!graphEnabled)return true;
        for(unsigned channel=0;channel<graphChannels;++channel)if(channels[channel].drums&&graphMixes[channel]){
            removeDSP(graphMixes[channel],graphDSPs[channel]);
            if(!channelFXDelayRelease(stream,channel,graphMixes[channel]))freeMix(graphMixes[channel]);
            graphMixes[channel]=graphDSPs[channel]=0;
            write(channel,0,0,13,0);}
        if(!userMix){userMix=getMix(stream,BASS_MIDI_CHAN_USERFX);
            if(userMix){userDSP=setDSP(userMix,delayProc,graphWet,20);write(9,userDSP,userMix,14,0);
                if(!userDSP)write(9,0,userMix,15,0,error());}}
        for(unsigned channel=0;channel<128;++channel)if(graphActive[channel]&&!channels[channel].drums&&!graphDSPs[channel]){
            const unsigned mix=getMix(stream,channel);
            if(mix){channelFXDelayAcquire(stream,channel,mix);graphMixes[channel]=mix;graphDSPs[channel]=setDSP(mix,delayProc,graphTaps[channel],20);
                write(channel,graphDSPs[channel],mix,13,0);if(!graphDSPs[channel])write(channel,0,mix,15,1,error());}}
        return true;
    }
    bool select(unsigned channel,unsigned event,unsigned raw,const BASS_MIDI_EVENT* original=nullptr){
        if(channel>=128)return false;
        if(event==MIDI_EVENT_DRUMS&&!graphReady)return false;
        // Unknown immediate RAW has actual decoded callbacks but no guessed
        // count. Reserve each callback independently; never charge an older
        // future/ASYNC ticket. Overflow retains the original delegate path.
        const bool unticketedCurrent=selectionSubmittingState==this&&!selectionSubmittingTicket;
        const unsigned required=need();const bool ticketed=!unticketedCurrent&&claimTicket(original);
        const bool legacyReserved=!unticketedCurrent&&!ticketed&&claimUnticketed(required);
        auto pending=queuedReservation.load(std::memory_order_acquire);bool reservation=false;
        while((ticketed||legacyReserved)&&pending>=required){if(queuedReservation.compare_exchange_weak(pending,pending-required,std::memory_order_acq_rel)){reservation=true;break;}}
        if(!reservation){if(ticketed||!reserve(required)){refused.fetch_add(1,std::memory_order_relaxed);return false;}queuedReservation.fetch_sub(required,std::memory_order_acq_rel);}
        struct Budget {NativeSelectionState& state;unsigned amount;~Budget(){state.reserved.fetch_sub(amount,std::memory_order_release);}}budget{*this,required};
        clock->writers.fetch_add(1,std::memory_order_acq_rel);auto& ch=channels[channel];
        const auto tokenReset=requestedTokenReset[channel].load(std::memory_order_acquire);
        if(tokenReset!=tokenResetSeen[channel]){ch.tokenValid=ch.tokenKnown=false;tokenResetSeen[channel]=tokenReset;}
        observeWatches(channel);
        // Original selection refusal does not suppress later shadow/tone writes.
        if(!emit(stream,channel,event,raw))failures.fetch_add(1,std::memory_order_relaxed);
        if(event==MIDI_EVENT_DRUMS){ch.drums=raw!=0;ch.bank=ch.program=ch.lsb=0;ch.tokenValid=ch.tokenKnown=false;
            requestedAddress[channel].store(ch.drums?0x1000000u:0u,std::memory_order_release);
            write(channel,event,raw!=0?1u:0u,6,0);
            if(channel==9)emit(stream,9,MIDI_EVENT_USERFX,ch.drums?127u:0u);
            if(!syncGraph()){clock->writers.fetch_sub(1,std::memory_order_release);return true;}
        }else{const unsigned address=request(channel,event,raw,false);ch.bank=address&255u;ch.lsb=(address>>8)&255u;ch.program=(address>>16)&255u;ch.drums=(address&0x1000000u)!=0;
            if(event==MIDI_EVENT_PROGRAM)ch.tokenValid=ch.tokenKnown=false;}
        if(event!=MIDI_EVENT_DRUMS)write(channel,event,std::min(raw,127u),6,0);
        if(apply(channel)){int bank=-1,program=-1;const auto& row=current(channel,bank,program);
            if(seeds(channel,row,true))reapply(channel);}
        clock->writers.fetch_sub(1,std::memory_order_release);return true;
    }
};
void NativeSelectionCheckpointArena::capture(const NativeSelectionState& state,unsigned long long serial,unsigned long long cursor,
        unsigned channel,unsigned event,unsigned raw,unsigned kind,unsigned key,int error){
    if(channel>=128)return;const auto index=produced.load(std::memory_order_relaxed);
    if(index-consumed.load(std::memory_order_acquire)>=capacity){refused.fetch_add(1,std::memory_order_relaxed);return;}
    const auto slot=static_cast<size_t>(index%capacity);const auto& ch=state.channels[channel];auto* h=headers.get()+slot*24;
    const unsigned long long values[]{serial,cursor,channel,event,raw,kind,key,static_cast<unsigned>(error),
        state.requestedAddress[channel].load(std::memory_order_acquire),ch.bank,ch.lsb,ch.program,ch.drums?1u:0u,ch.tokenValid?1u:0u,ch.tokenKnown?1u:0u,
        static_cast<unsigned>(ch.tokenBank),static_cast<unsigned>(ch.tokenProgram),ch.tokenLSB,ch.tokenDrums?1u:0u,
        static_cast<unsigned>(state.touchedOrder[channel].size()),state.graphEnabled?1u:0u,state.graphReady?1u:0u,state.graphChannels,state.userMix};
    std::copy_n(values,24,h);auto* c=cells.get()+slot*fields*6;auto* order=orders.get()+slot*fields;
    for(unsigned i=0;i<fields;++i){const unsigned row[]{ch.profile[i],ch.baseline[i],ch.touched[i],ch.modCurrent[i],static_cast<unsigned>(ch.applied[i]),
        static_cast<unsigned>(ch.profilePresent[i])|(static_cast<unsigned>(ch.baselinePresent[i])<<1)|(static_cast<unsigned>(ch.touchedPresent[i])<<2)|
        (static_cast<unsigned>(ch.modPresent[i])<<3)|(static_cast<unsigned>(ch.appliedPresent[i])<<4)};std::copy_n(row,6,c+i*6);}
    std::fill_n(order,fields,0);std::copy(state.touchedOrder[channel].begin(),state.touchedOrder[channel].end(),order);
    auto* graph=graphs.get()+slot*514;
    for(unsigned i=0;i<128;++i){graph[i]=state.graphMixes[i];graph[128+i]=state.graphDSPs[i];graph[256+i]=state.graphActive[i];graph[384+i]=state.channels[i].drums?1u:0u;}
    graph[512]=state.userMix;graph[513]=state.userDSP;auto* watch=watchRows.get()+slot*watches*2;
    auto* extra=metadata.get()+slot*11;extra[0]=state.metadataReady?1ull:0ull;state.metadataWords(channel,extra+1);
    for(unsigned i=0;i<watches;++i){const auto& source=state.watches[i];watch[i*2]=source.serial[channel];
        watch[i*2+1]=source.state->cells[source.state->offsets[source.slot]+channel].load(std::memory_order_acquire);}
    produced.store(index+1,std::memory_order_release);
}
extern "C" __declspec(dllexport) void* __cdecl midi_selection_checkpoint_create(void* pointer,unsigned capacity){
    if(!pointer||!capacity||capacity>1024)return nullptr;auto& s=*static_cast<NativeSelectionState*>(pointer);
    try{auto* arena=new NativeSelectionCheckpointArena(&s,capacity,static_cast<unsigned>(s.fields.size()),static_cast<unsigned>(s.watches.size()));s.retain();return arena;}
    catch(const std::bad_alloc&){return nullptr;}
}
struct NativeSelectionCheckpointAttach {NativeSelectionState* state;NativeSelectionCheckpointArena* arena;int accepted;};
// Invoke through channel_transaction_run kind2, so attach/detach and unlock
// remain in one native barrier. This operation does not allocate or emit.
extern "C" __declspec(dllexport) void __cdecl midi_selection_checkpoint_attach(void* pointer){
    if(!pointer)return;auto& request=*static_cast<NativeSelectionCheckpointAttach*>(pointer);request.accepted=0;
    if(!request.state)return;auto& s=*request.state;
    if(request.arena&&(request.arena->owner!=&s||request.arena->fields!=s.fields.size()||request.arena->watches!=s.watches.size()))return;
    if(s.checkpointArena&&request.arena&&s.checkpointArena!=request.arena)return;
    if(s.checkpointArena)s.checkpointArena->attached=false;s.checkpointArena=request.arena;
    if(request.arena)request.arena->attached=true;request.accepted=1;
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_checkpoint_status(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out||capacity<6)return 0;auto& arena=*static_cast<NativeSelectionCheckpointArena*>(pointer);
    AcquireSRWLockShared(&arena.owner->recordsLock);
    const unsigned long long values[]{arena.produced.load(std::memory_order_acquire),arena.consumed.load(std::memory_order_acquire),arena.refused.load(std::memory_order_acquire),arena.capacity,arena.fields,arena.watches};
    std::copy_n(values,6,out);ReleaseSRWLockShared(&arena.owner->recordsLock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_checkpoint_read(void* pointer,unsigned long long ordinal,unsigned long long* header,unsigned* cells,unsigned* order,unsigned* graph,unsigned long long* watch){
    if(!pointer||!header||!cells||!order||!graph||!watch)return 0;auto& arena=*static_cast<NativeSelectionCheckpointArena*>(pointer);
    AcquireSRWLockShared(&arena.owner->recordsLock);
    if(ordinal<arena.consumed.load(std::memory_order_acquire)||ordinal>=arena.produced.load(std::memory_order_acquire)){ReleaseSRWLockShared(&arena.owner->recordsLock);return 0;}
    const auto slot=static_cast<size_t>(ordinal%arena.capacity);std::copy_n(arena.headers.get()+slot*24,24,header);
    std::copy_n(arena.cells.get()+slot*arena.fields*6,arena.fields*6,cells);std::copy_n(arena.orders.get()+slot*arena.fields,arena.fields,order);
    std::copy_n(arena.graphs.get()+slot*514,514,graph);std::copy_n(arena.watchRows.get()+slot*arena.watches*2,arena.watches*2,watch);
    ReleaseSRWLockShared(&arena.owner->recordsLock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_checkpoint_ack(void* pointer,unsigned long long ordinal){
    if(!pointer)return 0;auto& arena=*static_cast<NativeSelectionCheckpointArena*>(pointer);
    AcquireSRWLockExclusive(&arena.owner->recordsLock);
    const auto head=arena.consumed.load(std::memory_order_acquire);
    const bool accepted=ordinal==head&&ordinal<arena.produced.load(std::memory_order_acquire);
    if(accepted)arena.consumed.store(head+1,std::memory_order_release);
    ReleaseSRWLockExclusive(&arena.owner->recordsLock);return accepted?1:0;
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_checkpoint_release(void* pointer){
    if(!pointer)return 1;auto* arena=static_cast<NativeSelectionCheckpointArena*>(pointer);
    if(arena->attached||arena->produced.load(std::memory_order_acquire)!=arena->consumed.load(std::memory_order_acquire))return 0;
    auto* owner=arena->owner;delete arena;owner->release();return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_selection_state_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_selection_state_metadata_version(){return 1;}
// Preparation only:128 rows(profile presence, previous token revisions,
// five signed DWORD controller shadows). No live source mutation or emit.
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_metadata_seed(void* pointer,unsigned long long generation,
        unsigned long long revision,const unsigned long long* rows,unsigned count,const NativeSelectionKeyRevision* keys,unsigned keyCount){
    if(!pointer||!rows||count!=128*8||(keyCount&&!keys))return 0;auto& s=*static_cast<NativeSelectionState*>(pointer);
    if(!s.preparationMutable()||s.metadataReady)return 0;
    for(unsigned channel=0;channel<128;++channel){const auto* row=rows+channel*8;if(row[0]>1)return 0;
        for(unsigned i=3;i<8;++i)if(row[i]>UINT_MAX)return 0;}
    for(unsigned i=1;i<keyCount;++i)if(std::make_pair(keys[i-1].bank,keys[i-1].program)>=std::make_pair(keys[i].bank,keys[i].program))return 0;
    std::vector<NativeSelectionKeyRevision> staged;try{if(keyCount)staged.assign(keys,keys+keyCount);}catch(const std::bad_alloc&){return 0;}
    s.keyRevisions.swap(staged);s.profileRevision=revision;s.streamGeneration=generation;
    for(unsigned channel=0;channel<128;++channel){auto& meta=s.metadata[channel];const auto* row=rows+channel*8;
        meta.profileDictionaryPresent=row[0]!=0;meta.tokenRevision=row[1];meta.tokenKeyRevision=row[2];
        for(unsigned i=0;i<5;++i)meta.controllers[i]=static_cast<int>(static_cast<unsigned>(row[3+i]));}
    s.metadataReady=true;return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_checkpoint_metadata_read(void* pointer,unsigned long long ordinal,unsigned long long* out,unsigned capacity){
    if(!pointer||!out||capacity<11)return 0;auto& arena=*static_cast<NativeSelectionCheckpointArena*>(pointer);
    AcquireSRWLockShared(&arena.owner->recordsLock);
    if(ordinal<arena.consumed.load()||ordinal>=arena.produced.load()){ReleaseSRWLockShared(&arena.owner->recordsLock);return 0;}
    std::copy_n(arena.metadata.get()+static_cast<size_t>(ordinal%arena.capacity)*11,11,out);
    ReleaseSRWLockShared(&arena.owner->recordsLock);return 1;
}
extern "C" __declspec(dllexport) void* __cdecl midi_selection_state_create(void* clock,unsigned stream,void* emit,void* get,void* source,void* error,
        const NativeSelectionField* fields,unsigned fieldCount,const NativeSelectionPreset* presets,unsigned presetCount,
        const NativeSelectionValue* values,unsigned valueCount){
    if(!clock||!stream||!emit||!get||!source||!error||!fields||!fieldCount||fieldCount>1080||
        (presetCount&&!presets)||presetCount>65536||(valueCount&&!values)||valueCount>1048576)return nullptr;
    for(unsigned i=0;i<fieldCount;++i)if(fields[i].flags>15)return nullptr;
    for(unsigned i=0;i<valueCount;++i)if(values[i].field>=fieldCount)return nullptr;
    for(unsigned i=0;i<presetCount;++i){const auto& p=presets[i];
        if(p.bank>65535||p.program>65535||p.flags>255||p.profileFirst>valueCount||p.profileCount>valueCount-p.profileFirst||
            p.extendedFirst>valueCount||p.extendedCount>valueCount-p.extendedFirst||p.tone[0]>400)return nullptr;
        for(unsigned t=1;t<6;++t)if(p.tone[t]>127)return nullptr;
        for(unsigned v=p.profileFirst;v<p.profileFirst+p.profileCount;++v)if(!(fields[values[v].field].flags&1u))return nullptr;
        for(unsigned v=p.extendedFirst;v<p.extendedFirst+p.extendedCount;++v){if(!(fields[values[v].field].flags&2u))return nullptr;
            for(unsigned prior=p.extendedFirst;prior<v;++prior)if(values[prior].field==values[v].field)return nullptr;}
        for(unsigned j=0;j<i;++j)if(p.bank==presets[j].bank&&p.program==presets[j].program)return nullptr;
    }
    try{
        auto s=std::make_unique<NativeSelectionState>(static_cast<NativeMidiClock*>(clock),stream,emit,get,source,error);
        s->fields.assign(fields,fields+fieldCount);if(presetCount)s->presets.assign(presets,presets+presetCount);if(valueCount)s->values.assign(values,values+valueCount);
        s->reachableProfile.resize(fieldCount);s->reachableExtended.resize(fieldCount);s->reachableTouched.resize(fieldCount);
        for(const auto& preset:s->presets)s->markPreset(preset);
        for(auto& ch:s->channels){ch.profile.assign(fieldCount,UINT_MAX);ch.baseline.resize(fieldCount);ch.touched.resize(fieldCount);ch.modCurrent.resize(fieldCount);ch.applied.resize(fieldCount);
            ch.profilePresent.resize(fieldCount);ch.baselinePresent.resize(fieldCount);ch.appliedPresent.resize(fieldCount);ch.touchedPresent.resize(fieldCount);ch.modPresent.resize(fieldCount);}
        for(auto& order:s->touchedOrder)order.reserve(fieldCount);
        return s.release();
    }catch(const std::bad_alloc&){return nullptr;}
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_read(void* pointer,NativeSelectionRecord* out,unsigned capacity){
    if(!pointer||!out||capacity>INT_MAX)return -1;auto& s=*static_cast<NativeSelectionState*>(pointer);AcquireSRWLockShared(&s.recordsLock);
    const auto head=s.consumed.load(std::memory_order_acquire),tail=s.produced.load(std::memory_order_acquire),count=tail-head;
    if(count>capacity){ReleaseSRWLockShared(&s.recordsLock);return -2;}
    for(unsigned long long i=0;i<count;++i)out[i]=s.record(head+i);ReleaseSRWLockShared(&s.recordsLock);return static_cast<int>(count);
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_selection_state_read_count(void* pointer){
    if(!pointer)return 0;const auto& s=*static_cast<NativeSelectionState*>(pointer);
    return static_cast<unsigned>(s.produced.load(std::memory_order_acquire)-s.consumed.load(std::memory_order_acquire));
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_seed(void* pointer,const unsigned* address,unsigned addressCount,
        const int* cells,unsigned cellCount,const NativeSelectionPreset* fallback){
    if(!pointer||!address||addressCount!=128*9||!cells||!fallback)return 0;auto& s=*static_cast<NativeSelectionState*>(pointer);
    if(!s.preparationMutable()||cellCount!=128*s.fields.size()*7)return 0;
    for(unsigned ch=0;ch<128;++ch){const auto* a=address+ch*9;
        if(a[0]>127||a[1]>127||a[2]>127||a[3]>1||a[4]>3||a[7]>127||a[8]>1)return 0;
        for(unsigned field=0;field<s.fields.size();++field){const auto* row=cells+(ch*s.fields.size()+field)*7;
            if(row[0]<0||row[0]>1||row[2]<0||row[2]>1||row[4]<0||row[4]>1)return 0;}
    }
    if(fallback->profileFirst>s.values.size()||fallback->profileCount>s.values.size()-fallback->profileFirst)return 0;
    if(fallback->extendedFirst>s.values.size()||fallback->extendedCount>s.values.size()-fallback->extendedFirst)return 0;
    for(unsigned v=fallback->profileFirst;v<fallback->profileFirst+fallback->profileCount;++v)if(!(s.fields[s.values[v].field].flags&1u))return 0;
    for(unsigned v=fallback->extendedFirst;v<fallback->extendedFirst+fallback->extendedCount;++v){if(!(s.fields[s.values[v].field].flags&2u))return 0;
        for(unsigned prior=fallback->extendedFirst;prior<v;++prior)if(s.values[prior].field==s.values[v].field)return 0;}
        for(unsigned ch=0;ch<128;++ch){auto& c=s.channels[ch];const auto* a=address+ch*9;c.bank=a[0];c.lsb=a[1];c.program=a[2];c.drums=a[3]!=0;
        s.requestedAddress[ch].store(a[0]|(a[1]<<8)|(a[2]<<16)|(a[3]<<24),std::memory_order_relaxed);
        c.tokenValid=(a[4]&1)!=0;c.tokenKnown=(a[4]&2)!=0;c.tokenBank=static_cast<int>(a[5]);c.tokenProgram=static_cast<int>(a[6]);c.tokenLSB=a[7];c.tokenDrums=a[8]!=0;
        for(unsigned field=0;field<s.fields.size();++field){const auto* row=cells+(ch*s.fields.size()+field)*7;
            c.baselinePresent[field]=static_cast<unsigned char>(row[0]);c.baseline[field]=static_cast<unsigned>(row[1]);
            c.appliedPresent[field]=static_cast<unsigned char>(row[2]);c.applied[field]=row[3];c.touchedPresent[field]=static_cast<unsigned char>(row[4]);
            c.touched[field]=static_cast<unsigned>(row[5]);c.profile[field]=static_cast<unsigned>(row[6]);c.profilePresent[field]=row[6]!=INT_MIN?1u:0u;
            if(row[6]!=INT_MIN&&(s.fields[field].flags&1u))s.markReachable(field,0);
            if(row[2]&&(s.fields[field].flags&2u))s.markReachable(field,1);
            if(row[4])s.markReachable(field,2);}
    }
    s.fallback=*fallback;s.markPreset(s.fallback);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_touch_order(void* pointer,unsigned channel,const unsigned* fields,unsigned count){
    if(!pointer||channel>=128||(count&&!fields))return 0;auto& s=*static_cast<NativeSelectionState*>(pointer);
    if(!s.preparationMutable()||count>s.fields.size())return 0;for(unsigned i=0;i<count;++i){if(fields[i]>=s.fields.size())return 0;
        for(unsigned j=0;j<i;++j)if(fields[i]==fields[j])return 0;}
    s.touchedOrder[channel].assign(fields,fields+count);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_watch(void* pointer,void* state,unsigned field,unsigned event,unsigned kind){
    if(!pointer||!state||kind>2)return 0;auto& s=*static_cast<NativeSelectionState*>(pointer);auto* journal=static_cast<NativeMidiState*>(state);
    if(!s.preparationMutable()||field>=s.fields.size()||journal->clock!=s.clock||journal->stream!=s.stream)return 0;
    unsigned slot=256;for(unsigned i=0;i<journal->eventCount;++i)if(journal->eventTypes[i]==event){slot=i;break;}if(slot==256||journal->lengths[slot]!=128)return 0;
    try{NativeSelectionState::Watch watch{journal,field,slot,kind,{}};
        watch.serial.fill(s.clock->serial.load(std::memory_order_acquire));s.watches.push_back(watch);if(kind==1)s.markReachable(field,2);journal->retain();return 1;
    }catch(const std::bad_alloc&){return 0;}
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_mod_seed(void* pointer,unsigned channel,unsigned field,unsigned present,unsigned raw){
    if(!pointer||channel>=128||present>1||raw>65535)return 0;auto& s=*static_cast<NativeSelectionState*>(pointer);
    if(s.references.load()!=1||field>=s.fields.size()||!(s.fields[field].flags&4u))return 0;s.channels[channel].modPresent[field]=static_cast<unsigned char>(present);s.channels[channel].modCurrent[field]=raw;return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_restore_order(void* pointer,const NativeSelectionRestore* rows,unsigned count,const unsigned* order,unsigned length){
    if(!pointer||(count&&!rows)||(length&&!order)||count>1048576||length>16777216)return 0;auto& s=*static_cast<NativeSelectionState*>(pointer);
    if(s.references.load()!=1||!s.restoration.empty())return 0;unsigned extended=0;for(const auto& f:s.fields)if(f.flags&2u)++extended;if(extended>31)return 0;
    const unsigned mask=(1u<<extended)-1;for(unsigned i=0;i<count;++i){const auto& r=rows[i];
        if((r.oldMask|r.newMask)&~mask||r.first>length||r.count>length-r.first)return 0;}
    for(unsigned i=0;i<length;++i)if(order[i]>=s.fields.size()||!(s.fields[order[i]].flags&2u))return 0;
    try{if(count)s.restoration.assign(rows,rows+count);if(length)s.restoreOrder.assign(order,order+length);return 1;}catch(const std::bad_alloc&){return 0;}
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_graph(void* pointer,unsigned enabled,unsigned channels,
        const unsigned* active,const unsigned* mixes,const unsigned* handles,void* const* taps,void* wet,unsigned userMix,unsigned userDSP,
        void* getMix,void* setDSP,void* removeDSP,void* freeMix,void* callback){
    if(!pointer||enabled>1||channels>128||!active||!mixes||!handles||!taps)return 0;auto& s=*static_cast<NativeSelectionState*>(pointer);
    if(!s.preparationMutable()||s.graphReady||(enabled&&(!getMix||!setDSP||!removeDSP||!freeMix||!callback||!wet)))return 0;
    for(unsigned ch=0;ch<128;++ch)if(active[ch]>1||(enabled&&active[ch]&&(!taps[ch]||!delayMidiReady(static_cast<LegacyDelayState*>(taps[ch])))))return 0;
    if(enabled&&!delayMidiReady(static_cast<LegacyDelayState*>(wet)))return 0;
    for(unsigned ch=0;ch<128;++ch){s.graphActive[ch]=active[ch];s.graphMixes[ch]=mixes[ch];s.graphDSPs[ch]=handles[ch];
        if(enabled&&active[ch]){s.graphTaps[ch]=static_cast<LegacyDelayState*>(taps[ch]);delayMidiRetain(s.graphTaps[ch]);}}
    if(enabled){s.graphWet=static_cast<LegacyDelayState*>(wet);delayMidiRetain(s.graphWet);}
    s.getMix=reinterpret_cast<HSTREAM(WINAPI*)(HSTREAM,DWORD)>(getMix);s.setDSP=reinterpret_cast<HDSP(WINAPI*)(DWORD,DSPPROC*,void*,int)>(setDSP);
    s.removeDSP=reinterpret_cast<BOOL(WINAPI*)(DWORD,HDSP)>(removeDSP);s.freeMix=reinterpret_cast<BOOL(WINAPI*)(HSTREAM)>(freeMix);s.delayProc=reinterpret_cast<DSPPROC*>(callback);
    s.graphEnabled=enabled!=0;s.graphChannels=channels;s.userMix=userMix;s.userDSP=userDSP;s.graphReady=true;return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_ack(void* pointer,unsigned long long serial){
    if(!pointer)return 0;auto& s=*static_cast<NativeSelectionState*>(pointer);AcquireSRWLockExclusive(&s.recordsLock);const auto head=s.consumed.load(std::memory_order_acquire);
    const bool accepted=head!=s.produced.load(std::memory_order_acquire)&&s.record(head).serial==serial;
    if(accepted)s.consumed.store(head+1,std::memory_order_release);ReleaseSRWLockExclusive(&s.recordsLock);return accepted?1:0;
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_release(void* pointer){
    if(!pointer)return 1;auto* s=static_cast<NativeSelectionState*>(pointer);
    if(s->references.load()==1&&(s->produced.load()!=s->consumed.load()||s->reserved.load()||s->settleLocked))return 0;s->release();return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_selection_state_references(void* pointer){return pointer?static_cast<NativeSelectionState*>(pointer)->references.load(std::memory_order_acquire):0;}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_reserve(void* pointer,unsigned events){
    if(!pointer)return 0;auto& s=*static_cast<NativeSelectionState*>(pointer);const unsigned needed=s.need();
    if(events>s.recordLimit.load()/needed)return 0;return s.reserveUnticketed(events*needed)?1:0;
}
extern "C" __declspec(dllexport) void __cdecl midi_selection_state_cancel_reservation(void* pointer,unsigned events){
    if(!pointer)return;auto& s=*static_cast<NativeSelectionState*>(pointer);const unsigned needed=s.need();
    if(events>s.recordLimit.load()/needed)return;s.cancelUnticketed(events*needed);
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_selection_state_status(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out||capacity<6)return 0;auto& s=*static_cast<NativeSelectionState*>(pointer);
    const unsigned long long values[]{s.produced.load(std::memory_order_acquire),s.consumed.load(std::memory_order_acquire),s.reserved.load(std::memory_order_acquire),
        s.queuedReservation.load(std::memory_order_acquire),s.refused.load(std::memory_order_acquire),s.failures.load(std::memory_order_acquire)};
    std::copy_n(values,6,out);return 6;
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_record_bound(void* pointer,unsigned* out,unsigned capacity){
    if(!pointer||!out||capacity<6)return 0;const auto& s=*static_cast<NativeSelectionState*>(pointer);unsigned profile=0,extended=0;
    for(const auto& field:s.fields){if(field.flags&1u)++profile;if(field.flags&2u)++extended;}
    const unsigned values[]{static_cast<unsigned>(s.fields.size()),profile,extended,static_cast<unsigned>(s.fields.size()),s.graphEnabled?258u:0u,s.need()};
    std::copy_n(values,6,out);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_reachable_bound(void* pointer,unsigned* out,unsigned capacity){
    if(!pointer||!out||capacity<5)return 0;const auto& s=*static_cast<NativeSelectionState*>(pointer);
    const unsigned values[]{s.profileBound,s.extendedBound,s.touchedBound,s.graphEnabled?258u:0u,s.need()};std::copy_n(values,5,out);return 1;
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_selection_state_ticket(void* pointer,unsigned events){return pointer?static_cast<NativeSelectionState*>(pointer)->ticket(events):0;}
// Additive prepared ownership ABI. Caller supplies original decoded identities
// BEFORE submission; callback never allocates and never changes event positions.
// position0 compares data only;1 absolute byte;2 absolute tick;3 per-event.
// A per-event row uses its tick when nonzero, otherwise its byte position.
// Ambiguous
// identities retain their reservations until a confirmed empty-queue barrier.
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_ticket_events(void* pointer,unsigned long long id,
        const BASS_MIDI_EVENT* rows,unsigned count,unsigned position){
    if(!pointer||!id||(count&&!rows)||position>3)return -1;auto& s=*static_cast<NativeSelectionState*>(pointer);
    std::vector<unsigned> order;if(!s.stageIdentityOrder(rows,count,position,order))return 0;
    return s.identifyTicket(id,rows,count,position,order.data())?1:0;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_selection_state_ticket_capacity(void* pointer){
    if(!pointer)return 0;auto& s=*static_cast<NativeSelectionState*>(pointer);AcquireSRWLockExclusive(&s.ticketsLock);s.trimTickets();
    const auto used=s.produced.load(std::memory_order_acquire)-s.consumed.load(std::memory_order_acquire)+s.reserved.load(std::memory_order_acquire);
    bool blocked=s.ticketCount==s.tickets.size();
    for(unsigned i=0;i<s.ticketCount;++i){const auto& row=s.tickets[(s.ticketHead+i)%static_cast<unsigned>(s.tickets.size())];
        if((row.state==2||s.ticketEvents[(s.ticketHead+i)%static_cast<unsigned>(s.tickets.size())].ambiguous)&&row.remaining){blocked=true;break;}}
    const unsigned limit=s.recordLimit.load(std::memory_order_acquire);
    const unsigned capacity=!blocked&&used<limit?static_cast<unsigned>(std::min(
        (limit-used)/s.need(),s.identityLimit-(s.identityProduced-s.identityConsumed))):0;
    ReleaseSRWLockExclusive(&s.ticketsLock);return capacity;
}
// bass.h ChannelLock and bassmidi.h StreamEvents WINAPI signatures. Original
// RAW bytes/flags remain unchanged. After submitted=1, a failed call may have
// played a prefix: retries may only release the lock/replay metadata.
extern "C" __declspec(dllexport) unsigned __cdecl midi_selection_state_owned_version(){return 2;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_selection_state_capacity_worker_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_capacity_worker_prepare(void* pointer){return pointer&&static_cast<NativeSelectionState*>(pointer)->prepareNativeCapacity()?1:0;}
// Additive input-side storage preparation. No original MIDI is submitted.
// ChannelLock excludes the decoder while immutable ring pages are published.
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_prepare_capacity(void* pointer,void* barrier,unsigned events,int* out){
    if(!pointer||!barrier||!out)return -1;std::fill_n(out,2,0);auto& s=*static_cast<NativeSelectionState*>(pointer);
    auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(barrier);
    if(s.settleLocked){if(s.settleThread!=GetCurrentThreadId())return -1;
        if(!lock(s.stream,FALSE)){out[1]=s.error();return -2;}s.settleLocked=false;s.settleThread=0;s.releasePreparation();}
    NativeSelectionState::StoragePreparation storage;if(!s.stageCapacity(events,0,storage))return 0;
    if(!lock(s.stream,TRUE)){out[0]=s.error();return 0;}s.settleLocked=true;s.settleThread=GetCurrentThreadId();
    const bool prepared=s.publishCapacity(events,0,storage);
    if(!lock(s.stream,FALSE)){out[1]=s.error();s.retainPreparation(std::move(storage));return -2;}s.settleLocked=false;s.settleThread=0;return prepared?1:0;
}
static int selectionStateSubmitOwned(void* pointer,void* barrier,void* submission,
        unsigned mode,const void* data,unsigned length,unsigned selections,unsigned long long* out,
        const BASS_MIDI_EVENT* identities=nullptr,unsigned identityCount=0,unsigned position=0){
    if(!pointer||!barrier||!submission||!out||(length&&!data))return -1;std::fill_n(out,6,0);
    auto& s=*static_cast<NativeSelectionState*>(pointer);auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(barrier);
    if(identities){if(identityCount!=selections||position>3)return -8;
    }
    auto submit=reinterpret_cast<DWORD(WINAPI*)(HSTREAM,DWORD,const void*,DWORD)>(submission);
    if(s.settleLocked){if(s.settleThread!=GetCurrentThreadId())return -1;
        if(!lock(s.stream,FALSE)){out[5]=static_cast<unsigned>(s.error());return -2;}s.settleLocked=false;s.settleThread=0;s.releasePreparation();}
    const bool unknown=selections==UINT_MAX;NativeSelectionState::StoragePreparation storage;
    const bool prepareCheckpoints=s.checkpointArena&&!unknown;
    if(prepareCheckpoints&&!s.stageCapacity(selections,0,storage))return -6;
    std::vector<unsigned> preparedOrder;
    if(identities&&!s.stageIdentityOrder(identities,identityCount,position,preparedOrder))return -6;
    if(!lock(s.stream,TRUE)){out[4]=static_cast<unsigned>(s.error());return 0;}
    s.settleLocked=true;s.settleThread=GetCurrentThreadId();
    // UINT_MAX is explicitly UNKNOWN, not a count or a zero-event claim.
    const bool checkpointPrepared=!prepareCheckpoints||s.publishCapacity(selections,0,storage);
    const auto ticket=checkpointPrepared&&!unknown&&selections?s.ticket(selections):0;out[0]=ticket;int result=1;
    if(!checkpointPrepared||(!unknown&&selections&&!ticket)||s.failedTicket())result=-6;
    else{
        if(ticket&&identities&&!s.identifyTicket(ticket,identities,identityCount,position,preparedOrder.data())){s.settleTicket(ticket,3,false);result=-8;}
        if(result==1){
        if(ticket)s.settleTicket(ticket,1,false);
        NativeSelectionSubmissionScope scope{&s,ticket};
        const DWORD accepted=submit(s.stream,mode,data,length);out[1]=accepted;out[3]=1;
        if(accepted==static_cast<DWORD>(-1)){out[2]=static_cast<unsigned>(s.error());if(ticket)s.settleTicket(ticket,2,false);result=2;}
        }
    }
    if(!lock(s.stream,FALSE)){out[5]=static_cast<unsigned>(s.error());s.retainPreparation(std::move(storage));return -2;}s.settleLocked=false;s.settleThread=0;return result;
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_submit_owned(void* pointer,void* barrier,void* submission,
        unsigned mode,const void* data,unsigned length,unsigned selections,unsigned long long* out){
    return selectionStateSubmitOwned(pointer,barrier,submission,mode,data,length,selections,out);
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_submit_identified(void* pointer,void* barrier,void* submission,
        unsigned mode,const void* data,unsigned length,unsigned selections,unsigned long long* out,
        const BASS_MIDI_EVENT* identities,unsigned identityCount,unsigned position){
    if(selections&&!identities){if(out)std::fill_n(out,6,0);return -8;}
    return selectionStateSubmitOwned(pointer,barrier,submission,mode,data,length,selections,out,identities,identityCount,position);
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_retry_owned_unlock(void* pointer,void* barrier,int* error){
    if(!pointer||!barrier||!error)return -1;*error=0;auto& s=*static_cast<NativeSelectionState*>(pointer);
    if(!s.settleLocked)return 1;if(s.settleThread!=GetCurrentThreadId())return -1;
    if(!reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(barrier)(s.stream,FALSE)){*error=s.error();return -2;}
    s.settleLocked=false;s.settleThread=0;s.releasePreparation();return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_preparation_pending(void* pointer){return pointer&&static_cast<NativeSelectionState*>(pointer)->preparationPending()?1:0;}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_ticket_settle(void* pointer,unsigned long long ticket,unsigned phase,unsigned queueDrained){
    if(!pointer||phase>3||queueDrained>1)return -1;return static_cast<NativeSelectionState*>(pointer)->settleTicket(ticket,phase,queueDrained!=0);
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_ticket_status(void* pointer,unsigned long long ticket,unsigned* out,unsigned capacity){
    if(!pointer||!out||capacity<4)return 0;auto& s=*static_cast<NativeSelectionState*>(pointer);AcquireSRWLockShared(&s.ticketsLock);
    for(const auto& row:s.tickets)if(row.id==ticket){const unsigned values[]{row.requested,row.processed,row.remaining,row.state};std::copy_n(values,4,out);ReleaseSRWLockShared(&s.ticketsLock);return 1;}
    ReleaseSRWLockShared(&s.ticketsLock);return 0;
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_failed_tickets(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out)return -1;auto& s=*static_cast<NativeSelectionState*>(pointer);AcquireSRWLockShared(&s.ticketsLock);unsigned count=0;
    for(unsigned i=0;i<s.tickets.size();++i){const auto& row=s.tickets[i];if((row.state==2||s.ticketEvents[i].ambiguous)&&row.remaining){if(count==capacity){ReleaseSRWLockShared(&s.ticketsLock);return -2;}out[count++]=row.id;}}
    ReleaseSRWLockShared(&s.ticketsLock);return static_cast<int>(count);
}
// A paused source owner may transfer publication recovery only with no
// thread-affine BASS unlock retained by any current/retired selection state.
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_unlock_owner(void* pointer,unsigned* out,unsigned count){
    if(!pointer||!out||count<2)return 0;auto& s=*static_cast<NativeSelectionState*>(pointer);
    out[0]=s.settleLocked?1u:0u;out[1]=s.settleThread;return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_settle_failed(void* pointer,unsigned long long ticket,void* barrier,void* reader,void* failure,int* out){
    if(!pointer||!barrier||!reader||!failure||!out)return -1;std::fill_n(out,3,0);auto& s=*static_cast<NativeSelectionState*>(pointer);
    auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(barrier);auto get=reinterpret_cast<BOOL(WINAPI*)(DWORD,DWORD,float*)>(reader);auto error=reinterpret_cast<int(WINAPI*)()>(failure);
    if(s.settleLocked){if(s.settleThread!=GetCurrentThreadId())return -1;if(!lock(s.stream,FALSE)){out[1]=error();return -2;}s.settleLocked=false;s.settleThread=0;s.releasePreparation();}
    if(!lock(s.stream,TRUE)){out[0]=error();return 0;}s.settleLocked=true;s.settleThread=GetCurrentThreadId();float pending=0;bool known=true;
    constexpr unsigned queued[]{BASS_ATTRIB_MIDI_QUEUED_ASYNC,BASS_ATTRIB_MIDI_QUEUED_BYTE,BASS_ATTRIB_MIDI_QUEUED_TICK};
    for(unsigned attribute:queued){float count=0;if(!get(s.stream,attribute,&count)){known=false;out[0]=error();break;}
        if(!std::isfinite(count)||count<0){known=false;out[0]=-1;break;}pending+=count;}
    const int result=known&&pending==0?s.settleTicket(ticket,3,true):0;
    if(!lock(s.stream,FALSE)){out[1]=error();out[2]=result;return -2;}s.settleLocked=false;s.settleThread=0;out[2]=result;return known&&pending==0&&result>=0?1:0;
}
// Explicit stream retirement ONLY: callers have stopped producers and removed
// the filter. Source-generation retirement must not cancel another generation's
// queued notes. bassmidi.h StreamEvents CANCEL discards pending events; release
// their budgets only after all three documented queues are confirmed empty.
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_cancel_pending(void* pointer,void* barrier,void* reader,void* submission,void* failure,int* out){
    if(!pointer||!barrier||!reader||!submission||!failure||!out)return -1;std::fill_n(out,3,0);auto& s=*static_cast<NativeSelectionState*>(pointer);
    auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(barrier);auto get=reinterpret_cast<BOOL(WINAPI*)(DWORD,DWORD,float*)>(reader);
    auto submit=reinterpret_cast<DWORD(WINAPI*)(HSTREAM,DWORD,const void*,DWORD)>(submission);auto error=reinterpret_cast<int(WINAPI*)()>(failure);
    if(s.settleLocked){if(s.settleThread!=GetCurrentThreadId())return -1;if(!lock(s.stream,FALSE)){out[1]=error();return -2;}s.settleLocked=false;s.settleThread=0;s.releasePreparation();}
    if(!lock(s.stream,TRUE)){out[0]=error();return 0;}s.settleLocked=true;s.settleThread=GetCurrentThreadId();
    bool known=submit(s.stream,BASS_MIDI_EVENTS_STRUCT|BASS_MIDI_EVENTS_CANCEL,nullptr,0)!=static_cast<DWORD>(-1);
    if(!known)out[0]=error();
    constexpr unsigned queued[]{BASS_ATTRIB_MIDI_QUEUED_ASYNC,BASS_ATTRIB_MIDI_QUEUED_BYTE,BASS_ATTRIB_MIDI_QUEUED_TICK};
    for(unsigned attribute:queued){if(!known)break;float count=0;if(!get(s.stream,attribute,&count)){known=false;out[0]=error();break;}
        if(!std::isfinite(count)||count!=0){known=false;out[0]=-1;break;}}
    if(known){AcquireSRWLockExclusive(&s.ticketsLock);
        for(auto& row:s.tickets)if(row.id&&row.remaining){s.cancelReservation(row.remaining*s.need());row.remaining=0;row.state=3;}
        // Legacy reserve_events batches have no ticket identity, but still own
        // real queued journal capacity. CANCEL plus all three empty queues is
        // the same retirement proof for these leases; do not strand them.
        const auto unticketed=s.unticketedReservation.exchange(0,std::memory_order_acq_rel);
        s.cancelReservation(static_cast<unsigned>(unticketed));
        s.trimTickets();ReleaseSRWLockExclusive(&s.ticketsLock);out[2]=1;}
    if(!lock(s.stream,FALSE)){out[1]=error();return -2;}s.settleLocked=false;s.settleThread=0;return known?1:0;
}
struct NativeMidiFunctionMap {
    NativeMidiState* journal;
    std::atomic<unsigned> references{1};
    BOOL(WINAPI* emit)(HSTREAM,DWORD,DWORD,DWORD);
    int(WINAPI* error)();
    std::vector<NativeMidiMapRoute> routes;
    std::vector<std::unique_ptr<std::vector<unsigned>>> tables;
    std::atomic<unsigned long long> failures{0};
    NativeMidiSendDomain* sends=nullptr;
    NativeSelectionState* selections=nullptr;
    std::array<std::array<LegacyDelayState*,2>,128> delays{};
    std::array<bool,128> delayMapped{};
    std::array<bool,128> selectionEnabled{};
    bool selectionPrepared=false;
    std::array<std::atomic<unsigned>,128> selectionLSBBlocked;
    std::array<unsigned,3> selectionSlots{};
    std::array<unsigned,6> presetSlots{};
    NativeMidiFunctionMap(unsigned count,NativeMidiState* state,void* emitAddress,void* errorAddress):
        journal(state),emit(reinterpret_cast<BOOL(WINAPI*)(HSTREAM,DWORD,DWORD,DWORD)>(emitAddress)),
        error(reinterpret_cast<int(WINAPI*)()>(errorAddress)),routes(count*128){
        journal->retain(); // A published/queued native generation outlives its Python root.
        for(auto& blocked:selectionLSBBlocked)blocked.store(0,std::memory_order_relaxed);
    }
    ~NativeMidiFunctionMap(){
        if(sends)sends->release();
        if(selections)selections->release();
        for(auto& channel:delays)for(auto* state:channel)if(state)delayMidiRelease(state);
        journal->release();
    }
    void retain(){references.fetch_add(1,std::memory_order_relaxed);}
    void release(){if(references.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
    unsigned journalSlot(unsigned event)const{
        for(unsigned i=0;i<journal->eventCount;++i)if(journal->eventTypes[i]==event)return i;
        return 256;
    }
    bool select(HSTREAM stream,unsigned channel,unsigned event,unsigned raw,const BASS_MIDI_EVENT* original=nullptr){
        if(selections&&selections->stream==stream)return selections->select(channel,event,raw,original);
        if(event==MIDI_EVENT_DRUMS)return false;
        if(channel>=128||!selectionEnabled[channel]||journal->stream!=stream)return false;
        // Nonzero LSB requires StreamGetPreset's event-position resolved source
        // in the existing profile token. Never fabricate that source from the
        // requested address or from a later Note during journal replay.
        if(event==MIDI_EVENT_BANK_LSB)selectionLSBBlocked[channel].store(raw!=0,std::memory_order_release);
        if(selectionLSBBlocked[channel].load(std::memory_order_acquire))return false;
        const unsigned address=event==MIDI_EVENT_PROGRAM?0:event==MIDI_EVENT_BANK?1:2;
        constexpr unsigned events[]={MIDI_EVENT_MIXLEVEL,MIDI_EVENT_ATTACK,MIDI_EVENT_DECAY,
            MIDI_EVENT_RELEASE,MIDI_EVENT_CUTOFF,MIDI_EVENT_RESONANCE};
        auto* clock=journal->clock;
        clock->writers.fetch_add(1,std::memory_order_acq_rel);
        // Exact legacy order: selection (full DWORD, even on failure), then
        // default mix100 and five tone64 controls. DRUMS/profile restoration
        // remains delegated. No source-preset inference, Python, or allocation.
        if(!emit(stream,channel,event,raw))failures.fetch_add(1,std::memory_order_relaxed);
        journal->receive(stream,selectionSlots[address],(channel<<16)|std::min(raw,127u));
        for(unsigned i=0;i<6;++i){
            const unsigned value=i?64u:100u;
            if(emit(stream,channel,events[i],value))journal->receive(stream,presetSlots[i],(channel<<16)|value);
            else failures.fetch_add(1,std::memory_order_relaxed);
        }
        clock->writers.fetch_sub(1,std::memory_order_release);
        return true;
    }
    bool apply(unsigned index,HSTREAM stream,unsigned channel,unsigned raw,bool keySource){
        if(index>=routes.size()||journal->stream!=stream)return false;
        const auto& route=routes[index];if(!route.table)return false;
        // Existing mapped scalar UI clamps are monotonic above raw65535.
        // KEY pressure instead explicitly extracts7 pressure bits from raw16.
        // Unmapped wire values remain entirely untouched by this preparation.
        const unsigned decoded=keySource?raw&65535u:std::min(raw,65535u);
        const unsigned packed=(*route.table)[decoded],input=packed&65535u,requested=packed>>16;
        const unsigned target=sends&&route.request!=256?sends->apply(route.targetEvent,requested):requested;
        auto* clock=journal->clock;
        // One complete source -> target -> journal transaction. A snapshot
        // cannot accept input-only or pre-emission half-state between records.
        clock->writers.fetch_add(1,std::memory_order_acq_rel);
        journal->receive(stream,route.input,(channel<<16)|input);
        if(route.targetEvent){
            journal->receive(stream,route.attempt,(channel<<16)|target);
            bool accepted;
            if(route.targetEvent==MIDI_EVENT_USERFX&&delayMapped[channel]){
                for(auto* state:delays[channel])if(state)delayMidiSend(state,target);
                accepted=true; // Custom delay updates its existing taps, NOT native UserFX send.
            }else accepted=emit(stream,channel,route.targetEvent,target)!=FALSE;
            if(accepted){
                if(route.request!=256){
                    sends->request(channel,route.targetEvent,requested);
                    journal->receive(stream,route.request,(channel<<16)|requested);
                }
                journal->receive(stream,route.output,(channel<<16)|target);
            }else{
                const unsigned code=static_cast<unsigned>(error());
                failures.fetch_add(1,std::memory_order_relaxed);
                journal->receive(stream,route.errorLow,(channel<<16)|(code&65535u));
                journal->receive(stream,route.errorHigh,(channel<<16)|(code>>16));
            }
        }
        clock->writers.fetch_sub(1,std::memory_order_release);
        return true;
    }
};
static_assert(std::atomic<unsigned>::is_always_lock_free,"selection guard must never lock an audio callback");
extern "C" __declspec(dllexport) unsigned __cdecl midi_function_map_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl midi_function_map_create(unsigned slots,void* state,
        const NativeMidiMapDescriptor* descriptors,unsigned count,void* emitAddress,void* errorAddress){
    if(!slots||slots>256||!state||(count&&!descriptors)||count>slots*128||!emitAddress||!errorAddress)return nullptr;
    auto* journal=static_cast<NativeMidiState*>(state);
    try{
        auto map=std::make_unique<NativeMidiFunctionMap>(slots,journal,emitAddress,errorAddress);
        std::vector<const unsigned*> sources;
        for(unsigned i=0;i<count;++i){
            const auto& d=descriptors[i];
            if(d.slot>=slots||d.channel>=128||d.targetSlot>=slots||!d.table)return nullptr;
            auto& route=map->routes[d.slot*128+d.channel];if(route.table)return nullptr;
            route.input=map->journalSlot(0x20000u+d.slot);route.targetEvent=d.targetEvent;
            if(route.input==256)return nullptr;
            if(d.targetEvent){
                route.attempt=map->journalSlot(0x30000u+d.targetSlot);
                route.output=map->journalSlot(d.targetEvent);
                route.errorLow=map->journalSlot(0x40000u+d.targetSlot);
                route.errorHigh=map->journalSlot(0x50000u+d.targetSlot);
                if(route.attempt==256||route.output==256||route.errorLow==256||route.errorHigh==256)return nullptr;
            }
            const auto found=std::find(sources.begin(),sources.end(),d.table);
            if(found==sources.end()){
                sources.push_back(d.table);
                map->tables.push_back(std::make_unique<std::vector<unsigned>>(d.table,d.table+65536));
                route.table=map->tables.back().get();
            }else route.table=map->tables[static_cast<size_t>(found-sources.begin())].get();
        }
        return map.release();
    }catch(const std::bad_alloc&){return nullptr;}
}
extern "C" __declspec(dllexport) void __cdecl midi_function_map_destroy(void* pointer){
    if(pointer)static_cast<NativeMidiFunctionMap*>(pointer)->release(); // Root only; filter/queued borrowers retain separately.
}
extern "C" __declspec(dllexport) int __cdecl midi_function_map_retain(void* pointer){
    if(!pointer)return 0;static_cast<NativeMidiFunctionMap*>(pointer)->retain();return 1;
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_function_map_failures(void* pointer){
    return pointer?static_cast<NativeMidiFunctionMap*>(pointer)->failures.load():0;
}
extern "C" __declspec(dllexport) int __cdecl midi_function_map_sends(void* pointer,void* domain){
    if(!pointer||!domain)return 0;auto& map=*static_cast<NativeMidiFunctionMap*>(pointer);
    if(map.sends)return 0; // Immutable generation, configured only before publication.
    for(auto& route:map.routes){
        if(!route.table||(route.targetEvent!=MIDI_EVENT_REVERB&&route.targetEvent!=MIDI_EVENT_CHORUS))continue;
        const unsigned field=map.journal->eventTypes[route.attempt]-0x30000u;
        const unsigned slot=map.journalSlot(0x60000u+field);if(slot==256)return 0;
    }
    for(auto& route:map.routes){
        if(!route.table||(route.targetEvent!=MIDI_EVENT_REVERB&&route.targetEvent!=MIDI_EVENT_CHORUS))continue;
        const unsigned field=map.journal->eventTypes[route.attempt]-0x30000u;
        route.request=map.journalSlot(0x60000u+field);
    }
    map.sends=static_cast<NativeMidiSendDomain*>(domain);map.sends->retain();return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_function_map_delay(void* pointer,unsigned channel,void* dry,void* wet){
    if(!pointer||channel>=128)return 0;auto& map=*static_cast<NativeMidiFunctionMap*>(pointer);
    if(map.delayMapped[channel])return 0;
    auto* first=static_cast<LegacyDelayState*>(dry);auto* second=static_cast<LegacyDelayState*>(wet);
    if((first&&!delayMidiReady(first))||(second&&!delayMidiReady(second)))return 0;
    if(first)delayMidiRetain(first);if(second)delayMidiRetain(second);
    map.delays[channel]={first,second};map.delayMapped[channel]=true;return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_function_map_selection(void* pointer,const unsigned* channels,unsigned count){
    if(!pointer||!channels||count!=128)return 0;
    auto& map=*static_cast<NativeMidiFunctionMap*>(pointer);
    if(map.selectionPrepared)return 0; // Never mutate a prepared/published generation.
    constexpr unsigned selection[]={MIDI_EVENT_PROGRAM,MIDI_EVENT_BANK,MIDI_EVENT_BANK_LSB};
    constexpr unsigned preset[]={MIDI_EVENT_MIXLEVEL,MIDI_EVENT_ATTACK,MIDI_EVENT_DECAY,
        MIDI_EVENT_RELEASE,MIDI_EVENT_CUTOFF,MIDI_EVENT_RESONANCE};
    for(unsigned i=0;i<3;++i){map.selectionSlots[i]=map.journalSlot(0x80000u|selection[i]);if(map.selectionSlots[i]==256)return 0;}
    for(unsigned i=0;i<6;++i){map.presetSlots[i]=map.journalSlot(0x80000u|preset[i]);if(map.presetSlots[i]==256)return 0;}
    for(unsigned i=0;i<128;++i){if(channels[i]>1)return 0;}
    for(unsigned i=0;i<128;++i)map.selectionEnabled[i]=channels[i]!=0;
    map.selectionPrepared=true;
    return 1; // Prepared generation only; publication uses the existing barrier.
}
extern "C" __declspec(dllexport) int __cdecl midi_function_map_selection_state(void* pointer,void* prepared){
    if(!pointer||!prepared)return 0;auto& map=*static_cast<NativeMidiFunctionMap*>(pointer);auto* s=static_cast<NativeSelectionState*>(prepared);
    if(map.selections||s->clock!=map.journal->clock||s->stream!=map.journal->stream)return 0;s->retain();map.selections=s;return 1;
}

// Native passthrough/mapping/default-selection branches. Non-neutral
// Bank/Program and DRUMS remain delegated; this is not complete MIDI isolation.
// All metadata/positions are untouched on passthrough.
// Isolated opt-in diagnostic ABI1. Disabled(nullptr) is the product default.
// Fixed atomic records only; no audio mutation, allocation or wait in hooks.
struct MidiPCMPhase {
    using Position=unsigned long long(WINAPI*)(unsigned,unsigned);
    struct Row{std::atomic_flag writer=ATOMIC_FLAG_INIT;std::atomic<unsigned long long> stamp{0};std::array<std::atomic<unsigned long long>,13> cells{};};
    std::array<Row,8192> rows{};std::atomic<unsigned long long> count{0},note{0},dropped{0};
    std::atomic<unsigned> refs{1};Position position;
    explicit MidiPCMPhase(void* reader):position(reinterpret_cast<Position>(reader)){}
    void retain(){refs.fetch_add(1);}void release(){if(refs.fetch_sub(1)==1)delete this;}
    void add(unsigned stage,unsigned stream,unsigned event=0,unsigned raw=0,unsigned channel=0,unsigned pos=0,unsigned tick=0,unsigned bytes=0,unsigned long long identity=0){
        LARGE_INTEGER qpc{};QueryPerformanceCounter(&qpc);const auto serial=count.fetch_add(1);
        auto& row=rows[serial%rows.size()];
        // Diagnostic only: a wrapped/in-flight collision loses a record,
        // never waits, alters MIDI, or lets an old writer overwrite a new one.
        if(row.writer.test_and_set(std::memory_order_acquire)){dropped.fetch_add(1);return;}
        if(row.stamp.load(std::memory_order_acquire)>serial*2+2){row.writer.clear(std::memory_order_release);dropped.fetch_add(1);return;}
        row.stamp.store(serial*2+1,std::memory_order_release);
        const unsigned long long data[]{serial+1,stage,static_cast<unsigned long long>(qpc.QuadPart),identity,stream,event,raw,channel,pos,tick,
            position(stream,BASS_POS_BYTE|BASS_POS_DECODE),stage==3||stage==4?position(stream,BASS_POS_BYTE|BASS_POS_DSP):~0ull,bytes};
        for(unsigned i=0;i<13;++i)row.cells[i].store(data[i],std::memory_order_relaxed);
        row.stamp.store(serial*2+2,std::memory_order_release);
        row.writer.clear(std::memory_order_release);
    }
};
extern "C" __declspec(dllexport) unsigned __cdecl midi_pcm_phase_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl midi_pcm_phase_create(void* position){return position?new(std::nothrow)MidiPCMPhase(position):nullptr;}
extern "C" __declspec(dllexport) int __cdecl midi_pcm_phase_destroy(void* pointer){if(!pointer)return 1;auto* phase=static_cast<MidiPCMPhase*>(pointer);if(phase->refs.load()!=1)return 0;phase->release();return 1;}
// Control snapshot: admission count, unavoidable overwritten prefix, rejected
// collision/stale reservations, retained owners. Never infer complete coverage
// from a full ring: rows lost to wrapping or concurrent copying stay explicit.
extern "C" __declspec(dllexport) int __cdecl midi_pcm_phase_health(void* pointer,unsigned long long* output){
    if(!pointer||!output)return 0;auto& phase=*static_cast<MidiPCMPhase*>(pointer);const auto count=phase.count.load();
    output[0]=count;output[1]=count>phase.rows.size()?count-phase.rows.size():0;output[2]=phase.dropped.load();output[3]=phase.refs.load();return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_pcm_phase_snapshot(void* pointer,unsigned long long* output,unsigned capacity,unsigned long long* total){
    if(!pointer||!output||!total)return 0;auto& phase=*static_cast<MidiPCMPhase*>(pointer);const auto end=phase.count.load();*total=end;
    const auto start=end>phase.rows.size()?end-phase.rows.size():0;unsigned copied=0;
    for(auto i=start;i<end&&copied<capacity;++i){auto& row=phase.rows[i%phase.rows.size()];const auto wanted=i*2+2;if(row.stamp.load(std::memory_order_acquire)!=wanted)continue;
        for(unsigned j=0;j<13;++j)output[copied*13+j]=row.cells[j].load(std::memory_order_relaxed);
        if(row.stamp.load(std::memory_order_acquire)==wanted)++copied;}
    return copied;
}
struct PlaybackTiming;
static void playbackTimingRetain(PlaybackTiming*);
static void playbackTimingRelease(PlaybackTiming*);
static void playbackTimingAccepted(PlaybackTiming*,HSTREAM,const BASS_MIDI_EVENT*);
struct NativeMidiFilterGate {
    MIDIFILTERPROC* delegate;
    unsigned count;
    std::array<unsigned,256> events{};
    std::unique_ptr<unsigned[]> mapped;
    unsigned channels=0;
    bool configured=false;
    NativeMidiFunctionMap* functionMap=nullptr; // Borrowed immutable prepared generation.
    MidiPCMPhase* phase=nullptr;
    PlaybackTiming* timing=nullptr; // Retained; bind/remove under the stream barrier.
    NativeMidiFilterGate(MIDIFILTERPROC* proc,const unsigned* types,unsigned length):delegate(proc),count(length){
        std::copy_n(types,length,events.begin());
        mapped=std::make_unique<unsigned[]>(length*128);
    }
    ~NativeMidiFilterGate(){if(functionMap)functionMap->release();if(phase)phase->release();if(timing)playbackTimingRelease(timing);}
};
static BOOL nativeMidiFilterGateOriginal(HSTREAM handle,int track,BASS_MIDI_EVENT* event,BOOL seeking,void* pointer){
    if(!pointer)return TRUE;auto& gate=*static_cast<NativeMidiFilterGate*>(pointer);
    if(event&&gate.configured){
        if(event->event==MIDI_EVENT_PROGRAM||event->event==MIDI_EVENT_BANK||
           event->event==MIDI_EVENT_BANK_LSB||event->event==MIDI_EVENT_DRUMS){
            if(event->chan>=gate.channels)return TRUE;
            if(event->event==MIDI_EVENT_DRUMS&&gate.functionMap)
                gate.functionMap->selectionLSBBlocked[event->chan].store(0,std::memory_order_release);
            if(gate.functionMap&&
               gate.functionMap->select(handle,event->chan,event->event,event->param,event))return FALSE;
        }else{
            if(event->chan>=gate.channels)return TRUE;
            const auto it=std::find(gate.events.begin(),gate.events.begin()+gate.count,event->event);
            if(it==gate.events.begin()+gate.count)return TRUE;
            const unsigned index=static_cast<unsigned>(it-gate.events.begin())*128+event->chan;
            const unsigned route=gate.mapped[index];
            if(!route)return TRUE;
            if(event->event==MIDI_EVENT_KEYPRES&&(event->param&127u)!=((route>>8)&127u))return TRUE;
            if(gate.functionMap&&gate.functionMap->apply(index,handle,event->chan,event->param,event->event==MIDI_EVENT_KEYPRES))return FALSE;
        }
    }
    return gate.delegate?gate.delegate(handle,track,event,seeking,nullptr):TRUE;
}
static BOOL CALLBACK nativeMidiFilterGate(HSTREAM handle,int track,BASS_MIDI_EVENT* event,BOOL seeking,void* pointer){
    if(humanizationSeed&&event&&event->event==MIDI_EVENT_NOTE)return FALSE;
    const BOOL accepted=nativeMidiFilterGateOriginal(handle,track,event,seeking,pointer);
    auto* gate=static_cast<NativeMidiFilterGate*>(pointer);
    // MIDIFILTERPROC runs immediately before this event is processed. TRUE
    // preserves the post-delegate event, including BASS's immutable byte pos.
    // A separate MIDI sync's DECODE position is only the render-block cursor.
    if(accepted&&gate&&gate->timing&&event&&!seeking)
        playbackTimingAccepted(gate->timing,handle,event);
    if(accepted&&gate&&gate->phase&&event&&!seeking&&event->event==MIDI_EVENT_NOTE&&(event->param>>8&255u)){
        const auto note=gate->phase->note.fetch_add(1)+1;
        gate->phase->add(1,handle,event->event,event->param,event->chan,event->pos,event->tick,0,note);}
    return accepted;
}
// Control owner must remove StreamSetFilter before binding/unbinding; the
// borrowed phase cannot be destroyed while the filter gate exists.
extern "C" __declspec(dllexport) int __cdecl midi_filter_gate_phase(void* pointer,void* phase){
    if(!pointer)return 0;auto& gate=*static_cast<NativeMidiFilterGate*>(pointer);auto* next=static_cast<MidiPCMPhase*>(phase);
    if(next)next->retain();auto* old=gate.phase;gate.phase=next;if(old)old->release();return 1;
}
extern "C" __declspec(dllexport) void* __cdecl midi_filter_gate_create(void* proc,const unsigned* events,unsigned count){
    if(!proc||!events||!count||count>256)return nullptr;
    try{return new NativeMidiFilterGate(reinterpret_cast<MIDIFILTERPROC*>(proc),events,count);}
    catch(const std::bad_alloc&){return nullptr;}
}
extern "C" __declspec(dllexport) void* __cdecl midi_filter_gate_proc(){return reinterpret_cast<void*>(&nativeMidiFilterGate);}
extern "C" __declspec(dllexport) void __cdecl midi_filter_gate_release_map(void* pointer){
    // Only after the owner successfully removed/replaced StreamSetFilter.
    // Keep the gate root alive if later metadata/kernel retirement refuses.
    if(!pointer)return;auto& gate=*static_cast<NativeMidiFilterGate*>(pointer);
    auto* map=gate.functionMap;gate.functionMap=nullptr;gate.configured=false;if(map)map->release();
}
extern "C" __declspec(dllexport) int __cdecl midi_filter_gate_configure(void* pointer,unsigned channels,const unsigned* routes,unsigned count){
    if(!pointer||!routes||channels>128)return 0;
    auto& gate=*static_cast<NativeMidiFilterGate*>(pointer);
    if(count!=gate.count*128)return 0;
    // Control caller owns BASS_ChannelLock while this bounded copy executes.
    // Callback never acquires a lock or waits, no concurrently mutated cells.
    std::copy_n(routes,count,gate.mapped.get());gate.channels=channels;gate.configured=true;
    if(gate.functionMap)gate.functionMap->release();gate.functionMap=nullptr;return 1; // Legacy publication restores its exact delegate semantics.
}
extern "C" __declspec(dllexport) int __cdecl midi_filter_gate_publish(void* pointer,unsigned stream,
        unsigned channels,const unsigned* routes,unsigned count,void* lockAddress,void* errorAddress,unsigned* error){
    // bass.h: BOOL WINAPI ChannelLock(DWORD,BOOL); int WINAPI ErrorGetCode().
    // CDLL releases GIL. Never return to Python BETWEEN lock and unlock: a
    // GUI GIL stall must not extend this channel's decoding exclusion window.
    if(error)*error=0;
    if(!pointer||!routes||!lockAddress||!errorAddress||channels>128||
       count!=static_cast<NativeMidiFilterGate*>(pointer)->count*128)return -1;
    auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(lockAddress);
    auto getError=reinterpret_cast<int(WINAPI*)()>(errorAddress);
    if(!lock(stream,TRUE)){if(error)*error=static_cast<unsigned>(getError());return 0;}
    midi_filter_gate_configure(pointer,channels,routes,count);
    if(!lock(stream,FALSE)){if(error)*error=static_cast<unsigned>(getError());return -2;}
    return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_filter_gate_publish_map(void* pointer,unsigned stream,
        unsigned channels,const unsigned* routes,unsigned count,void* prepared,void* lockAddress,void* errorAddress,unsigned* error){
    if(error)*error=0;
    if(!pointer||!routes||!lockAddress||!errorAddress||channels>128||
       count!=static_cast<NativeMidiFilterGate*>(pointer)->count*128)return -1;
    auto* map=static_cast<NativeMidiFunctionMap*>(prepared);
    if(map&&(map->journal->stream!=stream||map->routes.size()<count))return -1;
    auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(lockAddress);
    auto getError=reinterpret_cast<int(WINAPI*)()>(errorAddress);
    if(!lock(stream,TRUE)){if(error)*error=static_cast<unsigned>(getError());return 0;}
    if(map)map->retain(); // Retain before configure releases a possibly identical previous generation.
    midi_filter_gate_configure(pointer,channels,routes,count);
    static_cast<NativeMidiFilterGate*>(pointer)->functionMap=map;
    if(!lock(stream,FALSE)){if(error)*error=static_cast<unsigned>(getError());return -2;}
    return 1;
}
extern "C" __declspec(dllexport) void __cdecl midi_filter_gate_destroy(void* pointer){
    delete static_cast<NativeMidiFilterGate*>(pointer); // Only after filter removal succeeds.
}

// Stateless qualification only; BASS still performs raw decoding. Never
// remove/merge/reorder bytes or interpret running status on this shortcut.
extern "C" __declspec(dllexport) int __cdecl midi_passive_packet(const unsigned char* data,unsigned length){
    if(!data||!length)return 0;
    for(unsigned i=0;i<length;){
        const unsigned command=data[i]&0xf0u;
        const unsigned count=command==0xe0u?3u:command==0xd0u?2u:0u;
        if(!count||length-i<count)return 0;
        for(unsigned part=1;part<count;++part)if(data[i+part]&0x80u)return 0;
        i+=count;
    }
    return 1;
}

// Plan the EXISTING project interception walk, not a replacement MIDI decoder.
// BASS still receives the exact original/transformed wire. In particular, an
// unconsumed CC advances one byte (including malformed lookahead), while
// Program/pressure are two bytes and intercepted notes/CC are three bytes.
// No audio/state side effects, normalization, allocation or Python callback.
struct MidiRawSpan {unsigned offset,length,packed;};
static_assert(sizeof(MidiRawSpan)==12,"raw interception span ABI1");
extern "C" __declspec(dllexport) unsigned __cdecl midi_raw_plan_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_raw_plan(const unsigned char* data,unsigned length,unsigned start,
        const unsigned char* consumed,unsigned masks,MidiRawSpan* spans,unsigned capacity){
    //1MiB is this acceleration's product resource bound, NOT a MIDI/BASS cap.
    // The Python owner retains its complete legacy path outside this bound.
    if(!data||!consumed||masks!=16*128||!spans||start>length||length>1024*1024||capacity<length-start)return -1;
    unsigned count=0;
    for(unsigned offset=start;offset<length;){
        const unsigned status=data[offset],command=status&0xf0u,remaining=length-offset;
        unsigned size=1,packed=0;
        if(remaining>=2&&(command==0xc0u||command==0xd0u)){
            size=2;if(command==0xc0u)packed=status|(static_cast<unsigned>(data[offset+1])<<8);
        }else if(remaining<3)size=remaining;
        else if(command==0x80u||command==0x90u||command==0xb0u){
            const unsigned first=data[offset+1],second=data[offset+2];
            size=command!=0xb0u||(first<128&&consumed[(status&15u)*128+first])?3u:1u;
            packed=status|(first<<8)|(second<<16);
        }
        if(!packed&&count&&!spans[count-1].packed&&spans[count-1].offset+spans[count-1].length==offset)
            spans[count-1].length+=size;
        else spans[count++]={offset,size,packed};
        offset+=size;
    }
    return static_cast<int>(count);
}

// Common bounded control-side barrier. Only declared ABIs are allowed, not a
// Python callback/thunk. Prepare operation storage before locking; no native
// allocation or Python return until ChannelLock(FALSE) finishes.
struct ChannelTransactionStep {unsigned kind,first;unsigned long long second,address;};
static_assert(sizeof(ChannelTransactionStep)==24,"channel transaction ABI2");
extern "C" __declspec(dllexport) unsigned __cdecl channel_transaction_version(){return 2;}
extern "C" __declspec(dllexport) int __cdecl channel_transaction_unlock(unsigned stream,void* lockAddress,
        void* errorAddress,int* error){
    if(!error)return 0;*error=0;
    if(!lockAddress||!errorAddress){*error=-1;return 0;}
    auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(lockAddress);
    auto getError=reinterpret_cast<int(WINAPI*)()>(errorAddress);
    if(lock(stream,FALSE))return 1;
    *error=getError();return 0;
}
extern "C" __declspec(dllexport) void __cdecl channel_transaction_run(unsigned stream,
        const ChannelTransactionStep* steps,unsigned count,void* lockAddress,void* errorAddress,int* out){
    if(!out)return;
    // completed prefix, operation/lock error, unlock error, phase.
    std::fill_n(out,4,0);
    if((count&&!steps)||count>1024||!lockAddress||!errorAddress){out[3]=4;return;}
    for(unsigned i=0;i<count;++i)if(!steps[i].address||steps[i].kind<1||steps[i].kind>4){out[3]=4;return;}
    auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(lockAddress);
    auto getError=reinterpret_cast<int(WINAPI*)()>(errorAddress);
    if(!lock(stream,TRUE)){out[1]=getError();out[3]=1;return;}
    for(unsigned i=0;i<count;++i){
        const auto& step=steps[i];
        if(step.kind==1){
            auto operation=reinterpret_cast<BOOL(WINAPI*)(DWORD,DWORD)>(static_cast<uintptr_t>(step.address));
            if(!operation(step.first,static_cast<DWORD>(step.second))){out[1]=getError();out[3]=2;break;}
        }else if(step.kind==2){
            auto operation=reinterpret_cast<void(__cdecl*)(void*)>(static_cast<uintptr_t>(step.address));
            operation(reinterpret_cast<void*>(static_cast<uintptr_t>(step.second)));
        }else if(step.kind==3){
            auto operation=reinterpret_cast<void(__cdecl*)(void*,int)>(static_cast<uintptr_t>(step.address));
            operation(reinterpret_cast<void*>(static_cast<uintptr_t>(step.second)),static_cast<int>(step.first));
        }else{
            auto operation=reinterpret_cast<BOOL(WINAPI*)(DWORD)>(static_cast<uintptr_t>(step.address));
            if(!operation(step.first)){out[1]=getError();out[3]=2;break;}
        }
        ++out[0];
    }
    if(!lock(stream,FALSE)){out[2]=getError();out[3]=3;}
}

// Additive vendor parameter-only publication. Buffers are caller-owned and
// prepared before lock; accepted prefix rollback keeps the original order.
struct ChannelFXParameterStep {unsigned handle,reserved;unsigned long long values,previous;};
static_assert(sizeof(ChannelFXParameterStep)==24,"FX parameter transaction ABI1");
extern "C" __declspec(dllexport) unsigned __cdecl channel_fx_parameters_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl channel_fx_parameters_v2_version(){return 2;}
extern "C" __declspec(dllexport) void __cdecl channel_fx_parameters_v2_run(unsigned stream,
        const ChannelFXParameterStep* steps,unsigned count,void* lockAddress,void* errorAddress,void* setAddress,int* out){
    if(!out)return;std::fill_n(out,7,0);
    if(!steps||!count||count>32||!lockAddress||!errorAddress||!setAddress){out[3]=4;return;}
    for(unsigned i=0;i<count;++i)if(!steps[i].handle||steps[i].reserved||!steps[i].values||!steps[i].previous){out[3]=4;return;}
    auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(lockAddress);
    auto getError=reinterpret_cast<int(WINAPI*)()>(errorAddress);
    auto set=reinterpret_cast<BOOL(WINAPI*)(DWORD,void*)>(setAddress);
    if(!lock(stream,TRUE)){out[1]=getError();out[3]=1;return;}
    for(unsigned i=0;i<count;++i){
        if(!set(steps[i].handle,reinterpret_cast<void*>(static_cast<uintptr_t>(steps[i].values)))){
            out[1]=getError();out[3]=2;bool failed=false;
            for(unsigned previous=0;previous<i;++previous){
                if(set(steps[previous].handle,reinterpret_cast<void*>(static_cast<uintptr_t>(steps[previous].previous)))){
                    out[6]=static_cast<int>(static_cast<unsigned>(out[6])|(1u<<previous));if(!failed)++out[4];
                }else{if(!failed)out[5]=getError();failed=true;}
            }
            break;
        }
        ++out[0];
    }
    if(!lock(stream,FALSE)){out[2]=getError();out[3]=3;}
}
extern "C" __declspec(dllexport) void __cdecl channel_fx_parameters_run(unsigned stream,
        const ChannelFXParameterStep* steps,unsigned count,void* lockAddress,void* errorAddress,void* setAddress,int* out){
    if(!out)return;std::fill_n(out,6,0);
    if(!steps||!count||count>32||!lockAddress||!errorAddress||!setAddress){out[3]=4;return;}
    for(unsigned i=0;i<count;++i)if(!steps[i].handle||steps[i].reserved||!steps[i].values||!steps[i].previous){out[3]=4;return;}
    auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(lockAddress);
    auto getError=reinterpret_cast<int(WINAPI*)()>(errorAddress);
    auto set=reinterpret_cast<BOOL(WINAPI*)(DWORD,void*)>(setAddress);
    if(!lock(stream,TRUE)){out[1]=getError();out[3]=1;return;}
    for(unsigned i=0;i<count;++i){
        if(!set(steps[i].handle,reinterpret_cast<void*>(static_cast<uintptr_t>(steps[i].values)))){
            out[1]=getError();out[3]=2;
            for(unsigned previous=0;previous<i;++previous){
                if(!set(steps[previous].handle,reinterpret_cast<void*>(static_cast<uintptr_t>(steps[previous].previous)))){out[5]=getError();break;}
                ++out[4];
            }
            break;
        }
        ++out[0];
    }
    if(!lock(stream,FALSE)){out[2]=getError();out[3]=3;}
}

// Prepared vendor topology domain. Caller copies immutable target descriptions
// before FIFO admission; execution resolves old handles at its publication fence.
struct FXTopologyDescriptor {unsigned long long exact,reuse;unsigned kind,bytes;unsigned handle;int priority;const void* parameters;};
struct FXTopologyDescriptor2 {FXTopologyDescriptor base;void* pointer;unsigned long long kernel;double transition;};
struct FXTopologySnapshot {unsigned long long exact;unsigned handle,kind;int priority;unsigned reserved;};
struct FXTopologySnapshot2 {FXTopologySnapshot base;void* pointer;unsigned long long kernel;};
static_assert(sizeof(FXTopologySnapshot)==24,"FX topology snapshot ABI1");
static_assert(sizeof(FXTopologySnapshot2)==40,"FX topology snapshot ABI2");
extern "C" __declspec(dllexport) int __cdecl fx_parameters(void*,const double*,int);
extern "C" __declspec(dllexport) int __cdecl fx_kernel_transition(void*,void*,double);
extern "C" __declspec(dllexport) int __cdecl fx_kernel_transition_undo(void*,void*);
extern "C" __declspec(dllexport) int __cdecl fx_kernel_transition_commit(void*,void*);
extern "C" __declspec(dllexport) void CALLBACK fx_bass_dsp(DWORD,DWORD,void*,DWORD,void*);
struct FXTopologyValue {
    unsigned long long exact=0,reuse=0;unsigned kind=0,bytes=0,handle=0;int priority=0;
    void* pointer=nullptr;unsigned long long kernel=0;double transition=0;
    std::array<unsigned char,128> parameters{};
};
// Channel automation ABI1. All 128 payloads are copied during preparation.
// A byte mask changes only this parameter, preserving other mapped values.
struct ChannelFXMidiDescriptor {
    unsigned long long reuse;unsigned parameter,cc,bytes,kind;
    const unsigned char* mask;const unsigned char* payload;const double* values;
};
struct ChannelFXMidiRoute {
    unsigned long long reuse=0,serial=0;unsigned parameter=0,cc=0,bytes=0,kind=0;
    double observed=0;std::array<unsigned char,128> mask{};
    std::array<std::array<unsigned char,128>,128> payload{};std::array<double,128> values{};
};
struct ChannelFXMidiMap {std::vector<ChannelFXMidiRoute> routes;};
struct ChannelFXMidiSnapshot {unsigned long long reuse,serial;unsigned parameter,cc;double value;};
static_assert(sizeof(ChannelFXMidiDescriptor)==48&&sizeof(ChannelFXMidiSnapshot)==32,"channel MIDI FX ABI1");
struct FXTopologyGraph {
    std::atomic<unsigned> references{1};unsigned stream,count=0,retiredCount=0;bool faulted=false;
    // stream remains the FIFO/root generation. target is only the DSP sink.
    unsigned target=0,midiChannel=UINT_MAX;bool ownsTarget=false;FXTopologyGraph* channelNext=nullptr;
    HSTREAM(WINAPI* channelMix)(HSTREAM,DWORD)=nullptr;BOOL(WINAPI* freeMix)(HSTREAM)=nullptr;
    DWORD(WINAPI* channelEvent)(HSTREAM,DWORD,DWORD)=nullptr;
    unsigned version=1;HDSP(WINAPI* attachDSP)(DWORD,DSPPROC*,void*,int)=nullptr;BOOL(WINAPI* removeDSP)(DWORD,HDSP)=nullptr;
    BOOL(WINAPI* lock)(DWORD,BOOL);int(WINAPI* error)();HFX(WINAPI* attach)(DWORD,DWORD,int);
    BOOL(WINAPI* parameters)(DWORD,const void*);BOOL(WINAPI* priority)(DWORD,int);BOOL(WINAPI* remove)(DWORD,DWORD);
    std::array<FXTopologyValue,32> current{},retired{};
    SRWLOCK midiLock=SRWLOCK_INIT;std::shared_ptr<ChannelFXMidiMap> midi;
    unsigned long long midiSerial=0,midiFailures=0;
    FXTopologyGraph(unsigned channel,void* barrier,void* failure,void* create,void* update,void* order,void* detach):stream(channel),
        lock(reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(barrier)),error(reinterpret_cast<int(WINAPI*)()>(failure)),
        attach(reinterpret_cast<HFX(WINAPI*)(DWORD,DWORD,int)>(create)),parameters(reinterpret_cast<BOOL(WINAPI*)(DWORD,const void*)>(update)),
        priority(reinterpret_cast<BOOL(WINAPI*)(DWORD,int)>(order)),remove(reinterpret_cast<BOOL(WINAPI*)(DWORD,DWORD)>(detach)){}
    ~FXTopologyGraph();
    unsigned sink()const{return midiChannel==UINT_MAX?stream:target;}
    void retain(){references.fetch_add(1,std::memory_order_relaxed);}void release(){if(references.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
};
static SRWLOCK channelFXRegistryLock=SRWLOCK_INIT;
static FXTopologyGraph* channelFXRegistry=nullptr;
FXTopologyGraph::~FXTopologyGraph(){
    if(midiChannel!=UINT_MAX){AcquireSRWLockExclusive(&channelFXRegistryLock);
        auto** link=&channelFXRegistry;while(*link&&*link!=this)link=&(*link)->channelNext;
        if(*link)*link=channelNext;ReleaseSRWLockExclusive(&channelFXRegistryLock);}
    // Remaining DSPs still belong to AudioFXStage until output quiescence.
}
static bool channelFXDelayRelease(unsigned root,unsigned channel,unsigned mix){
    bool retained=false;AcquireSRWLockExclusive(&channelFXRegistryLock);
    for(auto* graph=channelFXRegistry;graph;graph=graph->channelNext)
        if(graph->stream==root&&graph->midiChannel==channel&&graph->target==mix){
            if(graph->count||graph->retiredCount){graph->ownsTarget=true;retained=true;}
            else{graph->target=0;graph->ownsTarget=false;}
            break;}
    ReleaseSRWLockExclusive(&channelFXRegistryLock);return retained;
}
static void channelFXDelayAcquire(unsigned root,unsigned channel,unsigned mix){
    AcquireSRWLockExclusive(&channelFXRegistryLock);
    for(auto* graph=channelFXRegistry;graph;graph=graph->channelNext)
        if(graph->stream==root&&graph->midiChannel==channel&&graph->target==mix){graph->ownsTarget=false;break;}
    ReleaseSRWLockExclusive(&channelFXRegistryLock);
}
extern "C" __declspec(dllexport) unsigned __cdecl fx_topology_channel_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl fx_topology_channel_configure(void* pointer,unsigned channel,void* getMix,void* freeMix,void* getEvent){
    if(!pointer||channel>=128||!getMix||!freeMix||!getEvent)return 0;auto& graph=*static_cast<FXTopologyGraph*>(pointer);
    if(graph.midiChannel!=UINT_MAX||graph.count||graph.retiredCount||graph.references.load()!=1)return 0;
    AcquireSRWLockExclusive(&channelFXRegistryLock);
    for(auto* other=channelFXRegistry;other;other=other->channelNext)if(other->stream==graph.stream&&other->midiChannel==channel){ReleaseSRWLockExclusive(&channelFXRegistryLock);return 0;}
    graph.midiChannel=channel;graph.channelMix=reinterpret_cast<HSTREAM(WINAPI*)(HSTREAM,DWORD)>(getMix);
    graph.freeMix=reinterpret_cast<BOOL(WINAPI*)(HSTREAM)>(freeMix);graph.channelEvent=reinterpret_cast<DWORD(WINAPI*)(HSTREAM,DWORD,DWORD)>(getEvent);
    graph.channelNext=channelFXRegistry;channelFXRegistry=&graph;ReleaseSRWLockExclusive(&channelFXRegistryLock);return 1;
}
extern "C" __declspec(dllexport) void __cdecl fx_topology_channel_target(void* pointer,unsigned* output){
    if(!output)return;output[0]=output[1]=0;if(!pointer)return;
    AcquireSRWLockShared(&channelFXRegistryLock);auto& graph=*static_cast<FXTopologyGraph*>(pointer);
    output[0]=graph.target;output[1]=graph.ownsTarget?1u:0u;ReleaseSRWLockShared(&channelFXRegistryLock);
}
extern "C" __declspec(dllexport) int __cdecl channel_fx_delay_release(unsigned root,unsigned channel,unsigned mix){return channelFXDelayRelease(root,channel,mix)?1:0;}
extern "C" __declspec(dllexport) void __cdecl channel_fx_delay_acquire(unsigned root,unsigned channel,unsigned mix){channelFXDelayAcquire(root,channel,mix);}
struct FXTopologyPlan {
    std::shared_ptr<ChannelFXMidiMap> midi;
    std::atomic<unsigned> references{1};
    FXTopologyGraph* graph;unsigned count;std::array<FXTopologyValue,32> desired{};
    explicit FXTopologyPlan(FXTopologyGraph* root,unsigned rows):graph(root),count(rows){graph->retain();}
    ~FXTopologyPlan(){graph->release();}
    void retain(){references.fetch_add(1,std::memory_order_relaxed);}void release(){if(references.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
};
extern "C" __declspec(dllexport) unsigned __cdecl channel_fx_midi_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl channel_fx_midi_prepare(void* pointer,const ChannelFXMidiDescriptor* rows,unsigned count){
    if(!pointer||count>256||(count&&!rows))return 0;auto& plan=*static_cast<FXTopologyPlan*>(pointer);
    if(plan.graph->midiChannel==UINT_MAX)return 0;
    try{auto map=std::make_shared<ChannelFXMidiMap>();map->routes.resize(count);
        for(unsigned i=0;i<count;++i){const auto& row=rows[i];
            if(!row.reuse||row.parameter>=8||(row.cc>=120&&row.cc!=UINT_MAX)||!row.bytes||row.bytes>128||!row.mask||!row.payload||!row.values)return 0;
            auto& route=map->routes[i];route.reuse=row.reuse;route.parameter=row.parameter;route.cc=row.cc;route.bytes=row.bytes;route.kind=row.kind;
            std::copy_n(row.mask,row.bytes,route.mask.begin());
            for(unsigned v=0;v<128;++v){if(!std::isfinite(row.values[v]))return 0;
                route.values[v]=row.values[v];std::copy_n(row.payload+v*row.bytes,row.bytes,route.payload[v].begin());}
        }plan.midi=std::move(map);return 1;
    }catch(const std::bad_alloc&){return 0;}
}
extern "C" __declspec(dllexport) unsigned __cdecl channel_fx_midi_snapshot(void* pointer,ChannelFXMidiSnapshot* rows,unsigned capacity,unsigned long long* failures){
    if(!pointer||!failures||(capacity&&!rows))return UINT_MAX;auto& graph=*static_cast<FXTopologyGraph*>(pointer);
    AcquireSRWLockShared(&graph.midiLock);*failures=graph.midiFailures;unsigned count=0;
    if(graph.midi)for(const auto& route:graph.midi->routes){if(count==capacity){ReleaseSRWLockShared(&graph.midiLock);return UINT_MAX;}
        rows[count++]={route.reuse,route.serial,route.parameter,route.cc,route.observed};}
    ReleaseSRWLockShared(&graph.midiLock);return count;
}
static bool channelFXMidiHas(unsigned root,unsigned channel,unsigned cc){
    bool found=false;AcquireSRWLockShared(&channelFXRegistryLock);
    for(auto* graph=channelFXRegistry;graph;graph=graph->channelNext)if(graph->stream==root&&graph->midiChannel==channel){
        AcquireSRWLockShared(&graph->midiLock);
        if(graph->midi)for(const auto& route:graph->midi->routes)if(route.cc==cc||route.cc==UINT_MAX){found=true;break;}
        ReleaseSRWLockShared(&graph->midiLock);break;}
    ReleaseSRWLockShared(&channelFXRegistryLock);return found;
}
static int channelFXMidiApply(unsigned root,unsigned channel,unsigned cc,unsigned value){
    FXTopologyGraph* target=nullptr;AcquireSRWLockShared(&channelFXRegistryLock);
    for(auto* graph=channelFXRegistry;graph;graph=graph->channelNext)if(graph->stream==root&&graph->midiChannel==channel){target=graph;target->retain();break;}
    ReleaseSRWLockShared(&channelFXRegistryLock);if(!target)return 1;auto& graph=*target;
    if(!graph.lock(root,TRUE)){AcquireSRWLockExclusive(&graph.midiLock);++graph.midiFailures;ReleaseSRWLockExclusive(&graph.midiLock);target->release();return 0;}
    AcquireSRWLockExclusive(&graph.midiLock);
    if(graph.midi)for(auto& route:graph.midi->routes){
        if(route.cc==UINT_MAX)route.cc=cc;
        if(route.cc!=cc)continue;
        bool accepted=true;
        for(unsigned i=0;i<graph.count;++i)if(graph.current[i].reuse==route.reuse){
            auto& current=graph.current[i];auto next=current;
            if(next.bytes!=route.bytes){accepted=false;break;}
            for(unsigned byte=0;byte<route.bytes;++byte)if(route.mask[byte])next.parameters[byte]=route.payload[value][byte];
            // BFX chorus: minimum and maximum sweep times must stay ordered.
            if(route.kind==1&&(route.parameter==3||route.parameter==4)){
                float low=0,high=0;std::memcpy(&low,next.parameters.data()+12,4);std::memcpy(&high,next.parameters.data()+16,4);
                if(low>high){if(route.parameter==3)std::memcpy(next.parameters.data()+16,&low,4);else std::memcpy(next.parameters.data()+12,&high,4);}}
            accepted=(next.pointer?fx_parameters(next.pointer,reinterpret_cast<const double*>(next.parameters.data()),static_cast<int>(next.bytes/8))!=0:
                graph.parameters(next.handle,next.parameters.data())!=FALSE);
            if(accepted){current.parameters=next.parameters;current.exact=0;}
            break;
        }
        if(accepted){route.observed=route.values[value];route.serial=++graph.midiSerial;}else ++graph.midiFailures;
    }
    const bool unlocked=graph.lock(root,FALSE)!=FALSE;if(!unlocked)++graph.midiFailures;
    ReleaseSRWLockExclusive(&graph.midiLock);target->release();return unlocked?1:-2;
}
extern "C" __declspec(dllexport) int __cdecl channel_fx_midi_event(unsigned root,unsigned channel,unsigned cc,unsigned value){
    if(channel>=128||cc>=120||value>=128)return 0;
    return channelFXMidiHas(root,channel,cc)?channelFXMidiApply(root,channel,cc,value):1;
}
static bool fxTopologyCopy(const FXTopologyDescriptor* rows,unsigned count,FXTopologyValue* output,bool seed){
    if(count>32||(count&&!rows))return false;
    for(unsigned i=0;i<count;++i){const auto& row=rows[i];
        if(!row.exact||!row.reuse||!row.bytes||row.bytes>128||!row.parameters||(!seed&&row.handle))return false;
        auto& value=output[i];value.exact=row.exact;value.reuse=row.reuse;value.kind=row.kind;value.bytes=row.bytes;
        value.handle=row.handle;value.priority=row.priority;std::memcpy(value.parameters.data(),row.parameters,row.bytes);
    }return true;
}
extern "C" __declspec(dllexport) unsigned __cdecl fx_topology_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl fx_topology_graph_create(unsigned stream,const FXTopologyDescriptor* rows,unsigned count,
        void* lock,void* error,void* attach,void* parameters,void* priority,void* remove){
    if(!stream||!lock||!error||!attach||!parameters||!priority||!remove)return nullptr;
    auto graph=std::unique_ptr<FXTopologyGraph>(new(std::nothrow)FXTopologyGraph(stream,lock,error,attach,parameters,priority,remove));
    if(!graph||!fxTopologyCopy(rows,count,graph->current.data(),true))return nullptr;graph->count=count;return graph.release();
}
extern "C" __declspec(dllexport) void __cdecl fx_topology_graph_release(void* pointer){if(pointer)static_cast<FXTopologyGraph*>(pointer)->release();}
extern "C" __declspec(dllexport) void* __cdecl fx_topology_plan_create(void* pointer,const FXTopologyDescriptor* rows,unsigned count){
    if(!pointer)return nullptr;auto plan=std::unique_ptr<FXTopologyPlan>(new(std::nothrow)FXTopologyPlan(static_cast<FXTopologyGraph*>(pointer),count));
    if(!plan||!fxTopologyCopy(rows,count,plan->desired.data(),false))return nullptr;return plan.release();
}
extern "C" __declspec(dllexport) void __cdecl fx_topology_plan_release(void* pointer){if(pointer)static_cast<FXTopologyPlan*>(pointer)->release();}
static bool fxTopologyCopy2(const FXTopologyDescriptor2* rows,unsigned count,FXTopologyValue* output,bool seed){
    if(count>32||(count&&!rows))return false;
    for(unsigned i=0;i<count;++i){const auto& row=rows[i];
        if(!fxTopologyCopy(&row.base,1,output+i,seed)||!std::isfinite(row.transition)||row.transition<0)return false;
        if(row.pointer&&(row.base.bytes%sizeof(double)||row.base.bytes>8*sizeof(double)))return false;
        output[i].pointer=row.pointer;output[i].kernel=row.kernel;output[i].transition=row.transition;
    }return true;
}
extern "C" __declspec(dllexport) unsigned __cdecl fx_topology_version2(){return 2;}
extern "C" __declspec(dllexport) void* __cdecl fx_topology_graph_create2(unsigned stream,const FXTopologyDescriptor2* rows,unsigned count,
        void* lock,void* error,void* attach,void* parameters,void* priority,void* remove,void* attachDSP,void* removeDSP){
    if(!stream||!lock||!error||!attach||!parameters||!priority||!remove||!attachDSP||!removeDSP)return nullptr;
    auto graph=std::unique_ptr<FXTopologyGraph>(new(std::nothrow)FXTopologyGraph(stream,lock,error,attach,parameters,priority,remove));
    if(!graph||!fxTopologyCopy2(rows,count,graph->current.data(),true))return nullptr;
    graph->version=2;graph->attachDSP=reinterpret_cast<HDSP(WINAPI*)(DWORD,DSPPROC*,void*,int)>(attachDSP);
    graph->removeDSP=reinterpret_cast<BOOL(WINAPI*)(DWORD,HDSP)>(removeDSP);graph->count=count;return graph.release();
}
extern "C" __declspec(dllexport) void* __cdecl fx_topology_plan_create2(void* pointer,const FXTopologyDescriptor2* rows,unsigned count){
    if(!pointer||static_cast<FXTopologyGraph*>(pointer)->version!=2)return nullptr;
    auto plan=std::unique_ptr<FXTopologyPlan>(new(std::nothrow)FXTopologyPlan(static_cast<FXTopologyGraph*>(pointer),count));
    if(!plan||!fxTopologyCopy2(rows,count,plan->desired.data(),false))return nullptr;return plan.release();
}
static bool fxTopologyParameters(FXTopologyGraph& graph,const FXTopologyValue& value){
    return value.pointer?fx_parameters(value.pointer,reinterpret_cast<const double*>(value.parameters.data()),static_cast<int>(value.bytes/sizeof(double)))!=0:
        graph.parameters(value.handle,value.parameters.data())!=FALSE;
}
static bool fxTopologyRemove(FXTopologyGraph& graph,const FXTopologyValue& value){
    return value.pointer?graph.removeDSP(graph.sink(),value.handle)!=FALSE:graph.remove(graph.sink(),value.handle)!=FALSE;
}
struct FXTopologyOutcome {unsigned phase=0,changed=0,unlockPending=0;int error=0,unlock=0;unsigned attached=0,updated=0,ordered=0,removed=0,rollback=0;};
static FXTopologyOutcome fxTopologyApply(FXTopologyPlan& plan){
    auto& graph=*plan.graph;FXTopologyOutcome result{};
    bool equal=graph.count==plan.count&&!graph.faulted&&!graph.retiredCount;
    for(unsigned i=0;equal&&i<plan.count;++i)equal=graph.current[i].exact==plan.desired[i].exact&&graph.current[i].priority==plan.desired[i].priority;
    if(equal){if(plan.midi){AcquireSRWLockExclusive(&graph.midiLock);graph.midi=plan.midi;ReleaseSRWLockExclusive(&graph.midiLock);}return result;}
    if(!graph.lock(graph.stream,TRUE)){result.phase=1;result.error=graph.error();return result;}
    auto finish=[&](){
        if(graph.midiChannel!=UINT_MAX&&!graph.count&&!graph.retiredCount&&graph.target&&graph.ownsTarget){
            if(graph.freeMix(graph.target)){AcquireSRWLockExclusive(&channelFXRegistryLock);graph.target=0;graph.ownsTarget=false;ReleaseSRWLockExclusive(&channelFXRegistryLock);}
            else{result.phase=2;result.error=graph.error();graph.faulted=true;}}
        if(!graph.lock(graph.stream,FALSE)){result.unlock=graph.error();result.unlockPending=1;}return result;};
    if(graph.midiChannel!=UINT_MAX&&plan.count&&!graph.target){
        const auto target=graph.channelMix(graph.stream,graph.midiChannel);
        if(!target){result.phase=2;result.error=graph.error();return finish();}
        const auto drums=graph.channelEvent(graph.stream,graph.midiChannel,MIDI_EVENT_DRUMS);
        AcquireSRWLockExclusive(&channelFXRegistryLock);graph.target=target;graph.ownsTarget=drums!=0&&drums!=UINT_MAX;ReleaseSRWLockExclusive(&channelFXRegistryLock);
    }
    // Retry former failed new attachments before preparing another graph.
    unsigned retained=0;bool failedRetired=false;
    for(unsigned i=0;i<graph.retiredCount;++i){auto value=graph.retired[i];
        if(value.handle&&!fxTopologyRemove(graph,value)){graph.retired[retained++]=value;failedRetired=true;result.error=graph.error();}}
    graph.retiredCount=retained;
    if(failedRetired){result.phase=2;graph.faulted=true;return finish();}
    auto previous=graph.current;const unsigned previousCount=graph.count;
    std::array<FXTopologyValue,32> next{};std::array<int,32> reuse{};reuse.fill(-1);std::array<bool,32> used{},created{},transitioned{};
    for(unsigned i=0;i<plan.count;++i)for(unsigned j=0;j<previousCount;++j)
        if(!used[j]&&previous[j].handle&&previous[j].exact==plan.desired[i].exact){reuse[i]=static_cast<int>(j);used[j]=true;break;}
    for(unsigned i=0;i<plan.count;++i)if(reuse[i]<0)for(unsigned j=0;j<previousCount;++j)
        if(!used[j]&&previous[j].handle&&previous[j].kind==plan.desired[i].kind&&previous[j].reuse==plan.desired[i].reuse&&
            !(previous[j].pointer&&previous[j].kernel!=plan.desired[i].kernel&&plan.desired[i].transition==0)){
            reuse[i]=static_cast<int>(j);used[j]=true;break;}
    unsigned built=0;
    for(unsigned i=0;i<plan.count;++i){auto value=plan.desired[i];
        if(reuse[i]<0){value.handle=value.pointer?graph.attachDSP(graph.sink(),fx_bass_dsp,value.pointer,value.priority):graph.attach(graph.sink(),value.kind,value.priority);created[i]=true;
            if(value.handle)result.attached|=1u<<i;
            if(!value.handle||!fxTopologyParameters(graph,value)){result.phase=2;result.error=graph.error();
                if(value.handle&&!fxTopologyRemove(graph,value))graph.retired[graph.retiredCount++]=value;
                created[i]=false;next[i]=value;built=i+1;break;}}
        else{auto& original=graph.current[static_cast<unsigned>(reuse[i])];value.handle=original.handle;value.pointer=original.pointer;
            if(original.exact!=value.exact){
                if(original.pointer&&original.kernel!=value.kernel){
                    if(!fx_kernel_transition(original.pointer,plan.desired[i].pointer,value.transition)){result.phase=2;result.error=graph.error();built=i;break;}
                    transitioned[i]=true;
                }else if(!fxTopologyParameters(graph,value)){result.phase=2;result.error=graph.error();built=i;break;}
                original.exact=value.exact;original.bytes=value.bytes;original.parameters=value.parameters;original.kernel=value.kernel;result.updated|=1u<<i;}
            if(original.priority!=value.priority){
                if(!graph.priority(value.handle,value.priority)){result.phase=2;result.error=graph.error();built=i;break;}
                original.priority=value.priority;result.ordered|=1u<<i;}}
        next[i]=value;built=i+1;
    }
    if(!result.phase)for(unsigned j=0;j<previousCount;++j)if(!used[j]){
        auto& value=graph.current[j];if(value.handle&&!fxTopologyRemove(graph,value)){result.phase=2;result.error=graph.error();break;}
        value.handle=0;result.removed|=1u<<j;}
    if(result.phase){
        graph.faulted=true;
        for(unsigned i=0;i<plan.count;++i)if(transitioned[i]){
            auto& value=graph.current[static_cast<unsigned>(reuse[i])];
            if(fx_kernel_transition_undo(value.pointer,plan.desired[i].pointer))value.kernel=previous[static_cast<unsigned>(reuse[i])].kernel;
        }
        for(unsigned j=0;j<previousCount;++j){auto& value=graph.current[j];const auto& saved=previous[j];
            if(value.exact!=saved.exact){auto restore=value;restore.bytes=saved.bytes;restore.parameters=saved.parameters;
                if(fxTopologyParameters(graph,restore)){value.exact=saved.exact;value.bytes=saved.bytes;value.parameters=saved.parameters;result.rollback|=1u<<j;}}
            if(value.priority!=saved.priority){graph.priority(value.handle,saved.priority);value.priority=saved.priority;}}
        for(unsigned i=0;i<built;++i)if(created[i]&&next[i].handle){
            if(!fxTopologyRemove(graph,next[i]))graph.retired[graph.retiredCount++]=next[i];}
    }else{
        for(unsigned i=0;i<plan.count;++i)if(transitioned[i])fx_kernel_transition_commit(next[i].pointer,plan.desired[i].pointer);
        graph.current=next;graph.count=plan.count;graph.faulted=false;result.changed=1;
        if(plan.midi){AcquireSRWLockExclusive(&graph.midiLock);graph.midi=plan.midi;ReleaseSRWLockExclusive(&graph.midiLock);}
    }
    return finish();
}

// Original EQ owns32 indexed bands AND an independent trim (33 handles).
// This is not the32-slot additional rack graph and keeps its failure policy.
struct StreamEQDescriptor {unsigned long long exact;unsigned kind,fxKind,bytes,active;int priority;unsigned handle;const void* parameters;};
struct StreamEQValue {unsigned long long exact=0;unsigned kind=0,fxKind=0,bytes=0,active=0;int priority=0;std::array<unsigned char,32> parameters{};};
struct StreamEQConfig {unsigned long long identity=0;unsigned count=0,bypassed=0;std::array<StreamEQValue,33> rows{};};
struct StreamEQSnapshot {unsigned long long identity;unsigned count,trim;struct Row{unsigned handle,kind;};std::array<Row,32> rows;};
static_assert(sizeof(StreamEQSnapshot)==272,"EQ33 completion snapshot ABI1");
// Offline export uses the same plan executor and copied snapshot without a
// live MIDI worker. It owns the original thread for any unlock retry.
extern "C" __declspec(dllexport) int __cdecl fx_topology_channel_run(void* pointer,unsigned* status,FXTopologySnapshot2* rows){
    if(!pointer||!status||!rows)return 0;auto& plan=*static_cast<FXTopologyPlan*>(pointer);auto& graph=*plan.graph;
    if(graph.midiChannel==UINT_MAX)return 0;const auto result=fxTopologyApply(plan);
    status[0]=result.phase;status[1]=static_cast<unsigned>(result.error);status[2]=static_cast<unsigned>(result.unlock);
    status[3]=graph.count;status[4]=graph.retiredCount;status[5]=graph.faulted?1:0;
    unsigned index=0;auto copy=[&](const FXTopologyValue& value){rows[index++]={{value.exact,value.handle,value.kind,value.priority,0},value.pointer,value.kernel};};
    for(unsigned i=0;i<graph.count;++i)copy(graph.current[i]);for(unsigned i=0;i<graph.retiredCount;++i)copy(graph.retired[i]);
    return result.unlockPending?2:1;
}

struct StreamEQGraph {
    std::atomic<unsigned> refs{1};unsigned stream,count=0;std::array<unsigned,33> handles{},kinds{};StreamEQConfig state,neutral;
    std::atomic<unsigned long long> catalogSequence{0},catalogState{0},catalogPending{0};
    BOOL(WINAPI* lock)(DWORD,BOOL);int(WINAPI* error)();HFX(WINAPI* attach)(DWORD,DWORD,int);BOOL(WINAPI* set)(DWORD,const void*);BOOL(WINAPI* remove)(DWORD,DWORD);
    bool pending=false;StreamEQConfig pendingState;unsigned pendingApplied=0,pendingRestored=0,pendingSteps=0;
    StreamEQGraph(unsigned channel,void* barrier,void* failure,void* create,void* update,void* detach):stream(channel),
        lock(reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(barrier)),error(reinterpret_cast<int(WINAPI*)()>(failure)),
        attach(reinterpret_cast<HFX(WINAPI*)(DWORD,DWORD,int)>(create)),set(reinterpret_cast<BOOL(WINAPI*)(DWORD,const void*)>(update)),remove(reinterpret_cast<BOOL(WINAPI*)(DWORD,DWORD)>(detach)){}
    void retain(){refs.fetch_add(1,std::memory_order_relaxed);}void release(){if(refs.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
};
struct StreamEQPlan {
    std::atomic<unsigned> refs{1};StreamEQGraph* graph;StreamEQConfig desired;
    explicit StreamEQPlan(StreamEQGraph* owner):graph(owner){graph->retain();}~StreamEQPlan(){graph->release();}
    void retain(){refs.fetch_add(1,std::memory_order_relaxed);}void release(){if(refs.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
};
static bool streamEQCopy(StreamEQConfig& config,unsigned long long identity,unsigned count,unsigned bypassed,const StreamEQDescriptor* rows){
    if(!identity||count>32||bypassed>1||!rows)return false;config.identity=identity;config.count=count;config.bypassed=bypassed;
    for(unsigned index=0;index<=count;++index){const auto& row=rows[index];if(!row.exact||!row.parameters||!row.bytes||row.bytes>32||row.active>1)return false;
        if(index<count&&(row.kind<1||row.kind>5))return false;
        auto& value=config.rows[index==count?32:index];value.exact=row.exact;value.kind=row.kind;value.fxKind=row.fxKind;value.bytes=row.bytes;
        value.active=row.active;value.priority=row.priority;std::memcpy(value.parameters.data(),row.parameters,row.bytes);
    }return true;
}
extern "C" __declspec(dllexport) unsigned __cdecl stream_equalizer_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl stream_equalizer_graph_create(unsigned stream,unsigned long long identity,unsigned count,unsigned bypassed,
        const StreamEQDescriptor* rows,unsigned long long neutralIdentity,unsigned neutralCount,const StreamEQDescriptor* neutral,
        void* lock,void* error,void* attach,void* set,void* remove){
    if(!stream||!lock||!error||!attach||!set||!remove)return nullptr;
    auto graph=std::unique_ptr<StreamEQGraph>(new(std::nothrow)StreamEQGraph(stream,lock,error,attach,set,remove));
    if(!graph||!streamEQCopy(graph->state,identity,count,bypassed,rows)||!streamEQCopy(graph->neutral,neutralIdentity,neutralCount,0,neutral))return nullptr;
    graph->count=count;for(unsigned i=0;i<count;++i){graph->handles[i]=rows[i].handle;graph->kinds[i]=rows[i].handle?rows[i].kind:0;}
    graph->handles[32]=rows[count].handle;graph->catalogState.store(identity,std::memory_order_release);return graph.release();
}
extern "C" __declspec(dllexport) void __cdecl stream_equalizer_graph_release(void* pointer){if(pointer)static_cast<StreamEQGraph*>(pointer)->release();}
extern "C" __declspec(dllexport) unsigned __cdecl stream_equalizer_catalog_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl stream_equalizer_catalog_snapshot(void* pointer,unsigned long long* output,unsigned capacity){
    // Management caller serializes Plan creation/release with this lookup.
    // Acquire pairs with the last Plan's graph release: refs==1 certifies
    // no Python Plan OR native FIFO Control can still read/write this graph.
    if(!pointer||!output||capacity<4)return 0;auto& graph=*static_cast<StreamEQGraph*>(pointer);
    const unsigned refs=graph.refs.load(std::memory_order_acquire);output[0]=refs;
    if(refs!=1)return 0;
    output[1]=graph.state.identity;output[2]=graph.neutral.identity;output[3]=graph.pending?graph.pendingState.identity:0;
    return 4;
}
extern "C" __declspec(dllexport) unsigned __cdecl stream_equalizer_catalog_live_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl stream_equalizer_allocation_snapshot(void* pointer,unsigned long long* output,unsigned capacity){
    if(!pointer||!output||capacity<3)return 0;auto& graph=*static_cast<StreamEQGraph*>(pointer);
    // Exact native owner/config object bytes, not allocator overhead/RSS.
    const auto refs=graph.refs.load(std::memory_order_acquire);
    output[0]=sizeof(StreamEQGraph);output[1]=sizeof(StreamEQPlan);output[2]=refs-1;return 3;
}
extern "C" __declspec(dllexport) unsigned __cdecl stream_equalizer_catalog_live_snapshot(void* pointer,unsigned expectedRefs,unsigned long long* output,unsigned capacity){
    if(!pointer||!output||capacity<4||!expectedRefs)return 0;auto& graph=*static_cast<StreamEQGraph*>(pointer);
    const auto sequence=graph.catalogSequence.load(std::memory_order_acquire);
    if(sequence&1u||graph.refs.load(std::memory_order_acquire)!=expectedRefs)return 0;
    output[0]=expectedRefs;output[1]=graph.catalogState.load(std::memory_order_acquire);output[2]=graph.neutral.identity;
    output[3]=graph.catalogPending.load(std::memory_order_acquire);
    if(graph.catalogSequence.load(std::memory_order_acquire)!=sequence||graph.refs.load(std::memory_order_acquire)!=expectedRefs)return 0;
    return 4;
}
extern "C" __declspec(dllexport) void* __cdecl stream_equalizer_plan_create(void* pointer,unsigned long long identity,unsigned count,unsigned bypassed,const StreamEQDescriptor* rows){
    if(!pointer)return nullptr;auto plan=std::unique_ptr<StreamEQPlan>(new(std::nothrow)StreamEQPlan(static_cast<StreamEQGraph*>(pointer)));
    if(!plan||!streamEQCopy(plan->desired,identity,count,bypassed,rows))return nullptr;return plan.release();
}
extern "C" __declspec(dllexport) void __cdecl stream_equalizer_plan_release(void* pointer){if(pointer)static_cast<StreamEQPlan*>(pointer)->release();}
struct StreamEQOutcome {unsigned phase=0,operations=0,unlockPending=0;int error=0,unlock=0;unsigned operation=0;};
static bool streamEQRemove(StreamEQGraph& graph,unsigned index,StreamEQOutcome& result){
    if(graph.handles[index]){++result.operations;if(!graph.remove(graph.stream,graph.handles[index])){result.phase=2;result.error=graph.error();result.operation=4;return false;}}
    graph.handles[index]=graph.kinds[index]=0;return true;
}
static bool streamEQDirect(StreamEQGraph& graph,const StreamEQConfig& config,StreamEQOutcome& result){
    if(graph.count!=config.count){for(unsigned i=0;i<graph.count;++i)if(!streamEQRemove(graph,i,result))return false;graph.count=config.count;}
    for(unsigned i=0;i<config.count;++i){const auto& row=config.rows[i];
        if(config.bypassed||!row.active){if(!streamEQRemove(graph,i,result))return false;continue;}
        if(graph.kinds[i]&&graph.kinds[i]!=row.kind&&!streamEQRemove(graph,i,result))return false;
        if(!graph.handles[i]){++result.operations;graph.handles[i]=graph.attach(graph.stream,row.fxKind,row.priority);graph.kinds[i]=graph.handles[i]?row.kind:0;
            if(!graph.handles[i]){result.phase=2;result.error=graph.error();result.operation=2;return false;}}
        ++result.operations;if(!graph.set(graph.handles[i],row.parameters.data())){result.phase=2;result.error=graph.error();result.operation=3;return false;}
    }
    const auto& trim=config.rows[32];
    if(config.bypassed||!trim.active)return streamEQRemove(graph,32,result);
    if(!graph.handles[32]){++result.operations;graph.handles[32]=graph.attach(graph.stream,9,7);if(!graph.handles[32]){result.phase=2;result.error=graph.error();result.operation=2;return false;}}
    ++result.operations;if(!graph.set(graph.handles[32],trim.parameters.data())){result.phase=2;result.error=graph.error();result.operation=3;return false;}return true;
}
static StreamEQOutcome streamEQApply(StreamEQPlan& plan){
    auto& graph=*plan.graph;const auto& desired=plan.desired;StreamEQOutcome result{};
    struct CatalogPublication {
        StreamEQGraph& graph;
        explicit CatalogPublication(StreamEQGraph& value):graph(value){graph.catalogSequence.fetch_add(1,std::memory_order_acq_rel);}
        ~CatalogPublication(){graph.catalogState.store(graph.state.identity,std::memory_order_release);
            graph.catalogPending.store(graph.pending?graph.pendingState.identity:0,std::memory_order_release);
            graph.catalogSequence.fetch_add(1,std::memory_order_release);}
    } publication(graph);
    if(graph.pending){graph.pending=false;if(graph.pendingApplied==graph.pendingSteps)graph.state=graph.pendingState;
        else if(graph.pendingRestored<graph.pendingApplied&&!streamEQDirect(graph,graph.state,result))return result;}
    const auto previous=graph.state;bool same=!desired.bypassed&&!previous.bypassed&&desired.count==previous.count&&desired.count==graph.count;
    for(unsigned i=0;same&&i<desired.count;++i)same=graph.handles[i]&&graph.kinds[i]==desired.rows[i].kind&&previous.rows[i].active&&desired.rows[i].active;
    same=same&&(bool(graph.handles[32])==bool(desired.rows[32].active))&&(desired.rows[32].active==previous.rows[32].active);
    std::array<ChannelFXParameterStep,33> steps{};unsigned changed=0;
    if(same){for(unsigned i=0;i<33;++i){if(i>=desired.count&&i!=32)continue;if(graph.handles[i]&&desired.rows[i].exact!=previous.rows[i].exact)
        steps[changed++]={graph.handles[i],0,reinterpret_cast<unsigned long long>(desired.rows[i].parameters.data()),reinterpret_cast<unsigned long long>(previous.rows[i].parameters.data())};}
        if(!changed){graph.state=desired;return result;}
        if(changed<=32){int out[7]{};channel_fx_parameters_v2_run(graph.stream,steps.data(),changed,reinterpret_cast<void*>(graph.lock),reinterpret_cast<void*>(graph.error),reinterpret_cast<void*>(graph.set),out);
            result.phase=out[3];result.error=out[1];result.unlock=out[2];result.operations=static_cast<unsigned>(out[0]);result.unlockPending=out[3]==3;
            result.operation=out[3]==1||out[3]==3?1:out[3]==2?3:0;
            if(result.unlockPending){graph.pending=true;graph.pendingState=desired;graph.pendingApplied=out[0];graph.pendingRestored=out[4];graph.pendingSteps=changed;}
            else if(!result.phase)graph.state=desired;
            else if(out[4]<out[0]){StreamEQOutcome restore{};streamEQDirect(graph,previous,restore);}return result;
        }
    }
    if(streamEQDirect(graph,desired,result)){graph.state=desired;return result;}
    const int first=result.error;StreamEQOutcome restore{};
    if(!streamEQDirect(graph,previous,restore)){bool cleared=true;for(unsigned i=0;i<graph.count;++i)if(!streamEQRemove(graph,i,restore)){cleared=false;break;}
        if(cleared&&streamEQRemove(graph,32,restore))graph.state=graph.neutral;}
    result.error=first;return result;
}

using Complex = std::complex<float>;
// Original global DX8/ECHO4 owners are not rack slots: priorities, failed
// parameter restoration and neutral removal are distinct source contracts.
// Immutable native plans keep these three vendor surfaces in the same MIDI
// FIFO; EQ and native stereo/limiter require their own typed state extension.
struct StreamVendorGraph {
    std::atomic<unsigned> refs{1};unsigned stream,kind,handle;int priority;
    HFX(WINAPI* attach)(DWORD,DWORD,int);BOOL(WINAPI* parameters)(HFX,const void*);
    BOOL(WINAPI* remove)(DWORD,DWORD);int(WINAPI* error)();unsigned bytes;
    std::array<unsigned char,64> current{};
    void* pointer=nullptr;unsigned sync=0;BOOL(WINAPI* lock)(DWORD,BOOL)=nullptr;
    HDSP(WINAPI* attachDSP)(DWORD,DSPPROC*,void*,int)=nullptr;BOOL(WINAPI* removeDSP)(DWORD,HDSP)=nullptr;
    HSYNC(WINAPI* attachSync)(DWORD,DWORD,QWORD,SYNCPROC*,void*)=nullptr;BOOL(WINAPI* removeSync)(DWORD,HSYNC)=nullptr;
    StreamVendorGraph(unsigned channel,unsigned type,unsigned existing,int order,unsigned size,const void* data,
        void* create,void* update,void* detach,void* failure):stream(channel),kind(type),handle(existing),priority(order),
        attach(reinterpret_cast<HFX(WINAPI*)(DWORD,DWORD,int)>(create)),parameters(reinterpret_cast<BOOL(WINAPI*)(HFX,const void*)>(update)),
        remove(reinterpret_cast<BOOL(WINAPI*)(DWORD,DWORD)>(detach)),error(reinterpret_cast<int(WINAPI*)()>(failure)),bytes(size){
        if(data&&size)std::memcpy(current.data(),data,size);
    }
    void retain(){refs.fetch_add(1,std::memory_order_relaxed);}void release(){if(refs.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
};
struct StreamVendorPlan {
    std::atomic<unsigned> refs{1};StreamVendorGraph* graph;unsigned active;
    std::array<unsigned char,64> desired{};
    void* prepared=nullptr;void* freeCallback=nullptr;
    StreamVendorPlan(StreamVendorGraph* owner,bool enabled,const void* values):graph(owner),active(enabled){graph->retain();std::memcpy(desired.data(),values,graph->bytes);}
    ~StreamVendorPlan(){graph->release();}
    void retain(){refs.fetch_add(1,std::memory_order_relaxed);}void release(){if(refs.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
};
struct StreamVendorOutcome {unsigned phase=0,committed=0,operations=0;int error=0,unlock=0;unsigned unlockPending=0,operation=0;};
struct StreamVendorSnapshot {unsigned kind,handle,committed,operations;};
struct StreamVendorSnapshot2 {StreamVendorSnapshot base;void* pointer;unsigned sync,reserved;};
extern "C" __declspec(dllexport) int __cdecl stereo_coefficient(void*,float);
extern "C" __declspec(dllexport) int __cdecl limiter_parameters(void*,double,double);
extern "C" __declspec(dllexport) void CALLBACK stereo_bass_dsp(DWORD,DWORD,void*,DWORD,void*);
extern "C" __declspec(dllexport) void CALLBACK limiter_bass_dsp(DWORD,DWORD,void*,DWORD,void*);
static_assert(sizeof(StreamVendorSnapshot2)==32,"stream effect pointer snapshot ABI2");
extern "C" __declspec(dllexport) unsigned __cdecl stream_vendor_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl stream_vendor_master_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl stream_vendor_graph_create(unsigned stream,unsigned kind,unsigned handle,
        unsigned bytes,const void* parameters,void* attach,void* update,void* remove,void* error){
    const unsigned size=kind==2?24u:kind==3?24u:kind==4||kind==7?16u:0u;
    if(!stream||!size||bytes!=size||!parameters||!attach||!update||!remove||!error)return nullptr;
    return new(std::nothrow)StreamVendorGraph(stream,kind,handle,kind==2?6:kind==3?5:kind==7?-9000:30,size,parameters,attach,update,remove,error);
}
extern "C" __declspec(dllexport) void __cdecl stream_vendor_graph_release(void* pointer){if(pointer)static_cast<StreamVendorGraph*>(pointer)->release();}
extern "C" __declspec(dllexport) unsigned __cdecl stream_vendor_borrow_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl stream_vendor_graph_references(void* pointer){
    return pointer?static_cast<StreamVendorGraph*>(pointer)->refs.load(std::memory_order_acquire):0;
}
extern "C" __declspec(dllexport) void* __cdecl stream_vendor_plan_create(void* pointer,unsigned active,const void* parameters){
    if(!pointer||active>1||!parameters)return nullptr;return new(std::nothrow)StreamVendorPlan(static_cast<StreamVendorGraph*>(pointer),active!=0,parameters);
}
extern "C" __declspec(dllexport) void __cdecl stream_vendor_plan_release(void* pointer){if(pointer)static_cast<StreamVendorPlan*>(pointer)->release();}
extern "C" __declspec(dllexport) unsigned __cdecl stream_vendor_dsp_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl stream_vendor_dsp_graph_create(unsigned stream,unsigned kind,unsigned handle,unsigned sync,
        void* pointer,const void* parameters,void* lock,void* error,void* attachDSP,void* removeDSP,void* attachSync,void* removeSync){
    if(!stream||(kind!=4&&kind!=5&&kind!=6)||!parameters||!lock||!error||!attachDSP||!removeDSP||(kind==6&&(!attachSync||!removeSync)))return nullptr;
    auto graph=std::unique_ptr<StreamVendorGraph>(new(std::nothrow)StreamVendorGraph(stream,kind,handle,kind==4?30:kind==5?2:-1000,
        kind==4?sizeof(SpatialRateParameters):kind==5?sizeof(float):2*sizeof(double),parameters,nullptr,nullptr,nullptr,error));
    if(!graph)return nullptr;graph->pointer=pointer;graph->sync=sync;graph->lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(lock);
    graph->attachDSP=reinterpret_cast<HDSP(WINAPI*)(DWORD,DSPPROC*,void*,int)>(attachDSP);
    graph->removeDSP=reinterpret_cast<BOOL(WINAPI*)(DWORD,HDSP)>(removeDSP);
    graph->attachSync=reinterpret_cast<HSYNC(WINAPI*)(DWORD,DWORD,QWORD,SYNCPROC*,void*)>(attachSync);
    graph->removeSync=reinterpret_cast<BOOL(WINAPI*)(DWORD,HSYNC)>(removeSync);return graph.release();
}
extern "C" __declspec(dllexport) void* __cdecl stream_vendor_dsp_plan_create(void* pointer,unsigned active,const void* parameters,void* prepared,void* callback){
    if(!pointer||active>1||!parameters)return nullptr;auto& graph=*static_cast<StreamVendorGraph*>(pointer);
    if((graph.kind!=4&&graph.kind!=5&&graph.kind!=6)||(active&&!prepared)||(graph.kind==6&&active&&!callback))return nullptr;
    auto plan=std::unique_ptr<StreamVendorPlan>(new(std::nothrow)StreamVendorPlan(&graph,active!=0,parameters));
    if(!plan)return nullptr;plan->prepared=prepared;plan->freeCallback=callback;return plan.release();
}
static StreamVendorOutcome streamDSPApply(StreamVendorPlan& plan){
    auto& graph=*plan.graph;StreamVendorOutcome result{};
    auto fail=[&](unsigned operation){result.phase=2;result.error=graph.error();result.operation=operation;};
    auto remove=[&](){
        if(!graph.handle&&!graph.sync)return true;
        ++result.operations;if(!graph.lock(graph.stream,TRUE)){result.phase=1;result.error=graph.error();result.operation=1;return false;}
        bool success=true;
        if(graph.handle){++result.operations;if(graph.removeDSP(graph.stream,graph.handle))graph.handle=0;else{fail(6);success=false;}}
        if(success&&graph.sync){++result.operations;if(graph.removeSync(graph.stream,graph.sync))graph.sync=0;else{fail(8);success=false;}}
        ++result.operations;if(!graph.lock(graph.stream,FALSE)){result.unlock=graph.error();result.unlockPending=1;result.phase=3;if(!result.operation)result.operation=1;}
        return success&&!result.unlockPending;
    };
    if(!plan.active){if(!remove())return result;result.committed=1;return result;}
    if(graph.kind==4){
        SpatialRateParameters parameters{};std::memcpy(&parameters,plan.desired.data(),sizeof(parameters));
        auto& state=*static_cast<SpatialRateState*>(plan.prepared);
        ++result.operations;if(!graph.lock(graph.stream,TRUE)){result.phase=1;result.error=graph.error();result.operation=1;return result;}
        const bool replacing=graph.pointer!=plan.prepared;
        ++result.operations;bool updated=((graph.handle&&!replacing)||state.reset())&&state.parameters(parameters);
        if(!updated)fail(11);
        if(updated&&replacing&&graph.handle){
            ++result.operations;if(graph.removeDSP(graph.stream,graph.handle))graph.handle=0;else{fail(6);updated=false;}
        }
        if(updated)graph.pointer=plan.prepared;
        if(updated&&!graph.handle){++result.operations;graph.handle=graph.attachDSP(graph.stream,spatial_rate_bass_dsp,graph.pointer,30);if(!graph.handle)fail(5);}
        ++result.operations;if(!graph.lock(graph.stream,FALSE)){result.unlock=graph.error();result.unlockPending=1;result.phase=3;if(!result.operation)result.operation=1;}
        if(result.phase)return result;
    }else if(graph.kind==5){
        if(!graph.pointer)graph.pointer=plan.prepared;
        float coefficient=0;std::memcpy(&coefficient,plan.desired.data(),sizeof(float));
        ++result.operations;if(!stereo_coefficient(graph.pointer,coefficient)){fail(9);return result;}
        if(!graph.handle){++result.operations;graph.handle=graph.attachDSP(graph.stream,stereo_bass_dsp,graph.pointer,2);
            if(!graph.handle){fail(5);return result;}}
    }else{
        if(!graph.handle){
            if(!remove())return result;graph.pointer=plan.prepared;
        }
        double parameters[2];std::memcpy(parameters,plan.desired.data(),sizeof(parameters));
        ++result.operations;if(!limiter_parameters(graph.pointer,parameters[0],parameters[1])){fail(10);return result;}
        if(!graph.handle){++result.operations;graph.handle=graph.attachDSP(graph.stream,limiter_bass_dsp,graph.pointer,-1000);
            if(!graph.handle){fail(5);return result;}
            ++result.operations;graph.sync=graph.attachSync(graph.stream,BASS_SYNC_FREE,0,reinterpret_cast<SYNCPROC*>(plan.freeCallback),nullptr);
            if(!graph.sync){fail(7);const int first=result.error;if(!remove())return result;result.error=first;result.operation=7;return result;}
        }
    }
    std::memcpy(graph.current.data(),plan.desired.data(),graph.bytes);result.committed=1;return result;
}
static StreamVendorOutcome streamVendorApply(StreamVendorPlan& plan){
    auto& graph=*plan.graph;StreamVendorOutcome result{};
    if(graph.attachDSP)return streamDSPApply(plan);
    auto fail=[&](unsigned operation){result.phase=2;result.error=graph.error();result.operation=operation;};
    auto remove=[&](){++result.operations;if(!graph.remove(graph.stream,graph.handle)){fail(4);return false;}graph.handle=0;return true;};
    if(!plan.active){if(graph.handle&&!remove())return result;result.committed=1;return result;}
    const bool created=!graph.handle;
    if(created){++result.operations;graph.handle=graph.attach(graph.stream,graph.kind==2?0x10014u:graph.kind==3?1u:graph.kind==7?BASS_FX_VOLUME:8u,graph.priority);
        if(!graph.handle){fail(2);return result;}}
    ++result.operations;
    if(!graph.parameters(graph.handle,plan.desired.data())){
        fail(3);const int first=result.error;
        if(graph.kind==2){
            if(created){if(!remove())return result;}
            else{++result.operations;if(!graph.parameters(graph.handle,graph.current.data())){
                if(!remove())return result;result.committed=2;}}
        }
        result.error=first;result.operation=3;return result;
    }
    std::memcpy(graph.current.data(),plan.desired.data(),graph.bytes);result.committed=1;return result;
}
constexpr double pi = 3.14159265358979323846;
constexpr int convolutionBlock = 256, fftSize = 512;
// Implementation safety bound, not a BASS/library maximum. Product candidates
// through768k and an independent1536k witness are qualified separately. This
// keeps frame counts, bounded preparation and transition counters representable.
constexpr double maximumDspRate = 1536000;
struct FixedFFTPlan {
    std::array<unsigned,fftSize> reversal{};
    std::array<Complex,fftSize-1> forward{},inverse{};
    FixedFFTPlan() {
        for(unsigned i=1,j=0;i<fftSize;++i) {
            unsigned bit=fftSize>>1;
            for(;j&bit;bit>>=1)j^=bit;
            j^=bit;reversal[i]=j;
        }
        unsigned offset=0;
        for(unsigned length=2;length<=fftSize;length<<=1) {
            // Match the old roots AND sequential binary32 recurrence exactly.
            // No direct-angle twiddles, reassociation, FFT size or latency change.
            const double forwardAngle=-2*pi/static_cast<double>(length);
            const double inverseAngle=2*pi/static_cast<double>(length);
            const Complex forwardRoot(static_cast<float>(std::cos(forwardAngle)),static_cast<float>(std::sin(forwardAngle)));
            const Complex inverseRoot(static_cast<float>(std::cos(inverseAngle)),static_cast<float>(std::sin(inverseAngle)));
            Complex forwardWeight(1,0),inverseWeight(1,0);
            for(unsigned j=0;j<length/2;++j) {
                forward[offset+j]=forwardWeight;inverse[offset+j]=inverseWeight;
                forwardWeight*=forwardRoot;inverseWeight*=inverseRoot;
            }
            offset+=length/2;
        }
    }
};
static const FixedFFTPlan& fixedFFTPlan() {
    // Preparation calls this before any audio callback can own a Convolver.
    // Fixed arrays have no heap storage; the shared plan is immutable thereafter.
    static const FixedFFTPlan plan;
    return plan;
}
static void fft(std::vector<Complex>& data, bool inverse) {
    const size_t count = data.size();
    if(count==fftSize) {
        const auto& plan=fixedFFTPlan();
        const auto& weights=inverse?plan.inverse:plan.forward;
        for(unsigned i=1;i<fftSize;++i)
            if(i<plan.reversal[i])std::swap(data[i],data[plan.reversal[i]]);
        unsigned offset=0;
        for(unsigned length=2;length<=fftSize;length<<=1) {
            for(unsigned start=0;start<fftSize;start+=length)
                for(unsigned j=0;j<length/2;++j) {
                    const auto even=data[start+j],odd=data[start+j+length/2]*weights[offset+j];
                    data[start+j]=even+odd;data[start+j+length/2]=even-odd;
                }
            offset+=length/2;
        }
        if(inverse)for(auto& item:data)item/=static_cast<float>(count);
        return;
    }
    for (size_t i = 1, j = 0; i < count; ++i) {
        size_t bit = count >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) std::swap(data[i], data[j]);
    }
    for (size_t length = 2; length <= count; length <<= 1) {
        const double angle = (inverse ? 2 : -2) * pi / static_cast<double>(length);
        const Complex root(static_cast<float>(std::cos(angle)), static_cast<float>(std::sin(angle)));
        for (size_t start = 0; start < count; start += length) {
            Complex weight(1, 0);
            for (size_t j = 0; j < length / 2; ++j) {
                const auto even = data[start + j], odd = data[start + j + length / 2] * weight;
                data[start + j] = even + odd; data[start + j + length / 2] = even - odd;
                weight *= root;
            }
        }
    }
    if (inverse) for (auto& item : data) item /= static_cast<float>(count);
}

struct Convolver {
    std::vector<std::vector<Complex>> kernels[4], history[2];
    std::vector<Complex> accumulator[2];
    bool activeRoute[4]{};
    float input[2][convolutionBlock]{}, output[2][convolutionBlock]{}, overlap[2][convolutionBlock]{};
    float dry[2][convolutionBlock]{};
    int position = 0, head = 0, parts;
    float wet = 1, gain = 1;
    explicit Convolver(const std::array<std::vector<float>, 4>& impulse) {
        (void)fixedFFTPlan(); // Control-side preparation, never first-use audio work.
        size_t length = 1;
        for (const auto& ir : impulse) length = std::max(length, ir.size());
        parts = static_cast<int>((length + convolutionBlock - 1) / convolutionBlock);
        for (int channel = 0; channel < 2; ++channel) {
            history[channel].assign(parts, std::vector<Complex>(fftSize));
            accumulator[channel].resize(fftSize);
        }
        for (int route = 0; route < 4; ++route) {
            activeRoute[route] = std::any_of(impulse[route].begin(), impulse[route].end(), [](float value) { return value != 0; });
            kernels[route].assign(parts, std::vector<Complex>(fftSize));
            for (int part = 0; part < parts; ++part) {
                auto& kernel = kernels[route][part];
                for (int i = 0; i < convolutionBlock; ++i) {
                    const size_t source = static_cast<size_t>(part * convolutionBlock + i);
                    if (source < impulse[route].size()) kernel[i] = impulse[route][source];
                }
                fft(kernel, false);
            }
        }
    }
    void nextBlock() {
        for (int channel = 0; channel < 2; ++channel) {
            auto& spectrum = history[channel][head];
            std::fill(spectrum.begin(), spectrum.end(), Complex{});
            for (int i = 0; i < convolutionBlock; ++i) spectrum[i] = input[channel][i];
            fft(spectrum, false);
            std::fill(accumulator[channel].begin(), accumulator[channel].end(), Complex{});
        }
        for (int outputChannel = 0; outputChannel < 2; ++outputChannel) {
            auto& sum = accumulator[outputChannel];
            for (int inputChannel = 0; inputChannel < 2; ++inputChannel) {
                if (!activeRoute[outputChannel * 2 + inputChannel]) continue;
                for (int part = 0; part < parts; ++part) {
                    const auto& source = history[inputChannel][(head - part + parts) % parts];
                    const auto& kernel = kernels[outputChannel * 2 + inputChannel][part];
                    static_assert(sizeof(Complex) == 2 * sizeof(float));
                    const float* a = reinterpret_cast<const float*>(source.data());
                    const float* b = reinterpret_cast<const float*>(kernel.data());
                    float* target = reinterpret_cast<float*>(sum.data());
                    const __m128 signs = _mm_set_ps(1, -1, 1, -1);
                    for (int i = 0; i < fftSize * 2; i += 4) {
                        const __m128 x = _mm_loadu_ps(a + i), y = _mm_loadu_ps(b + i);
                        const __m128 real = _mm_shuffle_ps(x, x, _MM_SHUFFLE(2, 2, 0, 0));
                        const __m128 imag = _mm_shuffle_ps(x, x, _MM_SHUFFLE(3, 3, 1, 1));
                        const __m128 swapped = _mm_shuffle_ps(y, y, _MM_SHUFFLE(2, 3, 0, 1));
                        const __m128 product = _mm_add_ps(_mm_mul_ps(real, y), _mm_mul_ps(_mm_mul_ps(imag, swapped), signs));
                        _mm_storeu_ps(target + i, _mm_add_ps(_mm_loadu_ps(target + i), product));
                    }
                }
            }
            fft(sum, true);
            for (int i = 0; i < convolutionBlock; ++i) {
                output[outputChannel][i] = sum[i].real() + overlap[outputChannel][i];
                overlap[outputChannel][i] = sum[i + convolutionBlock].real();
            }
        }
        head = (head + 1) % parts;
    }
    void process(float* samples, unsigned frames) {
        for (unsigned frame = 0; frame < frames; ++frame) {
            for (int channel = 0; channel < 2; ++channel) {
                const float original = samples[2 * frame + channel];
                samples[2 * frame + channel] = gain * (wet * output[channel][position] + (1 - wet) * dry[channel][position]);
                dry[channel][position] = original;
                input[channel][position] = original;
            }
            if (++position == convolutionBlock) { nextBlock(); position = 0; }
        }
    }
};

static std::vector<float> resampleIR(const std::vector<float>& input, double rate, double sourceRate = 48000) {
    if (rate == sourceRate) return input;
    const double ratio = rate / sourceRate;
    std::vector<float> result(static_cast<size_t>(std::ceil(input.size() * ratio)));
    const double cutoff = std::min(1.0, ratio);
    // Common integer sample-rate ratios have a tiny polyphase table (96/48k
    // has only2 phases). Precompute sinc/window coefficients outside rendering
    // instead of millions of sin/cos calls while a position control is edited.
    const unsigned targetRate = static_cast<unsigned>(rate), originRate = static_cast<unsigned>(sourceRate);
    const unsigned divisor = std::gcd(targetRate, originRate);
    const unsigned phases = targetRate / divisor;
    std::vector<std::array<double, 64>> coefficients;
    auto weight = [cutoff, ratio](double distance) {
        if (std::abs(distance) >= 32) return 0.;
        const double argument = pi * distance * cutoff;
        const double sinc = std::abs(argument) < 1e-12 ? cutoff : std::sin(argument) / (pi * distance);
        return sinc * (.5 + .5 * std::cos(pi * distance / 32)) / ratio;
    };
    if (rate == targetRate && sourceRate == originRate && phases <= 1024) {
        coefficients.resize(phases);
        for (unsigned phase = 0; phase < phases; ++phase)
            for (int tap = 0; tap < 64; ++tap)
                coefficients[phase][tap] = weight(static_cast<double>(phase) / phases - (tap - 31));
    }
    for (size_t i = 0; i < result.size(); ++i) {
        const double position = static_cast<double>(i) / ratio;
        const int center = coefficients.empty() ? static_cast<int>(position)
            : static_cast<int>(i * originRate / targetRate);
        const unsigned phase = static_cast<unsigned>((i * (originRate / divisor)) % phases);
        double value = 0;
        for (int j = center - 31; j <= center + 32; ++j) {
            if (j < 0 || j >= static_cast<int>(input.size())) continue;
            const double distance = position - j;
            value += input[j] * (coefficients.empty() ? weight(distance) : coefficients[phase][j - center + 31]);
        }
        result[i] = static_cast<float>(value);
    }
    return result;
}

struct ReleaseCOM {
    template<class T> void operator()(T* pointer) const noexcept { if (pointer) pointer->Release(); }
};
static bool hrtfImpulse(HMODULE library, double azimuth, double elevation, double distance,
                        std::vector<float>& left, std::vector<float>& right) {
    auto create = reinterpret_cast<decltype(&CreateHrtfApo)>(GetProcAddress(library, "CreateHrtfApo"));
    if (!create) return false;
    HrtfApoInit init{};
    IXAPO* apo = nullptr;
    if (FAILED(create(&init, &apo))) return false;
    std::unique_ptr<IXAPO, ReleaseCOM> apoOwner(apo);
    IXAPOHrtfParameters* settings = nullptr;
    bool success = false;
    if (SUCCEEDED(apo->QueryInterface(__uuidof(IXAPOHrtfParameters), reinterpret_cast<void**>(&settings)))) {
        std::unique_ptr<IXAPOHrtfParameters, ReleaseCOM> settingsOwner(settings);
        const double az = azimuth * pi / 180, el = elevation * pi / 180;
        HrtfPosition position{static_cast<float>(distance * std::cos(el) * std::sin(az)),
            static_cast<float>(distance * std::sin(el)), static_cast<float>(-distance * std::cos(el) * std::cos(az))};
        if (FAILED(settings->SetSourcePosition(&position)) || FAILED(settings->SetEnvironment(HrtfEnvironment::Outdoors))) {
            return false;
        }
        WAVEFORMATEX inputFormat{WAVE_FORMAT_IEEE_FLOAT, 1, 48000, 48000 * 4, 4, 32, 0};
        WAVEFORMATEX outputFormat{WAVE_FORMAT_IEEE_FLOAT, 2, 48000, 48000 * 8, 8, 32, 0};
        XAPO_LOCKFORPROCESS_PARAMETERS inputLock{&inputFormat, 1024}, outputLock{&outputFormat, 1024};
        if (SUCCEEDED(apo->LockForProcess(1, &inputLock, 1, &outputLock))) {
            std::array<float, 1024> mono{}; std::array<float, 2048> stereo{};
            left.resize(16384); right.resize(16384); mono[0] = .25f;
            size_t last = 0;
            for (int block = 0; block < 16; ++block) {
                XAPO_PROCESS_BUFFER_PARAMETERS input{mono.data(), XAPO_BUFFER_VALID, 1024};
                XAPO_PROCESS_BUFFER_PARAMETERS output{stereo.data(), XAPO_BUFFER_VALID, 1024};
                apo->Process(1, &input, 1, &output, TRUE);
                for (int i = 0; i < 1024; ++i) {
                    const size_t index = static_cast<size_t>(block * 1024 + i);
                    left[index] = stereo[2 * i] * 4; right[index] = stereo[2 * i + 1] * 4;
                    if (std::abs(left[index]) > 1e-8 || std::abs(right[index]) > 1e-8) last = index;
                }
                mono.fill(0);
            }
            left.resize(last + 1); right.resize(last + 1);
            apo->UnlockForProcess(); success = last != 0;
        }
    }
    return success;
}

struct SaturationQuality {
    std::array<std::array<double,65>,2> coefficients{};
    double up[2][65]{}, down[2][65]{}, dry[2][17]{};
    unsigned position=0,dryPosition=0,factor=1;
    SaturationQuality() {
        for (unsigned index=0;index<2;++index) {
            const unsigned oversample=index?4:2, length=16*oversample+1;
            const double cutoff=.47/oversample, center=(length-1)*.5;
            double sum=0;
            for (unsigned tap=0;tap<length;++tap) {
                const double distance=tap-center;
                const double sinc=distance==0?2*cutoff:std::sin(2*pi*cutoff*distance)/(pi*distance);
                const double window=.42-.5*std::cos(2*pi*tap/(length-1))+.08*std::cos(4*pi*tap/(length-1));
                coefficients[index][tap]=sinc*window; sum+=coefficients[index][tap];
            }
            for (auto& value:coefficients[index]) value/=sum;
        }
    }
    void select(unsigned next) {
        if (next==factor) return;
        factor=next; position=dryPosition=0;
        for (auto& channel:up) std::fill_n(channel,65,0.);
        for (auto& channel:down) std::fill_n(channel,65,0.);
        for (auto& channel:dry) std::fill_n(channel,17,0.);
    }
    void process(float* pair,double drive,double wet,double output) {
        const auto& kernel=coefficients[factor==4?1:0];
        const unsigned length=16*factor+1;
        double filtered[2]{};
        for (unsigned phase=0;phase<factor;++phase) {
            for (unsigned channel=0;channel<2;++channel) {
                up[channel][position]=phase==0?static_cast<double>(pair[channel])*factor:0.;
                double interpolated=0.;
                for (unsigned tap=0;tap<length;++tap) interpolated+=kernel[tap]*up[channel][(position+length-tap)%length];
                down[channel][position]=std::tanh(interpolated*drive);
                if (phase==0)
                    for (unsigned tap=0;tap<length;++tap) filtered[channel]+=kernel[tap]*down[channel][(position+length-tap)%length];
            }
            position=(position+1)%length;
        }
        for (unsigned channel=0;channel<2;++channel) {
            dry[channel][dryPosition]=pair[channel];
            const double delayed=dry[channel][(dryPosition+1)%17];
            pair[channel]=static_cast<float>((delayed+(filtered[channel]-delayed)*wet)*output);
        }
        dryPosition=(dryPosition+1)%17;
    }
};

static void processSaturation(SaturationQuality& state,float* pair,double drive,double wetPercent,double output) {
    if (state.factor>1) { state.process(pair,drive,wetPercent/100.,output); return; }
    for (unsigned channel=0;channel<2;++channel) {
        const double input=pair[channel];
        pair[channel]=static_cast<float>((input+(std::tanh(input*drive)-input)*wetPercent/100.0)*output);
    }
}
struct Effect {
    int kind;
    double sampleRate;
    std::array<std::atomic<double>, 8> target{};
    std::atomic<unsigned> revision{0};
    double active[8]{};
    double phase = 0, gain = 0;
    double previousInput[2]{}, previousOutput[2]{};
    float held[2]{};
    int holdCounter = 0, gateHold = 0;
    bool gateOpen = false;
    double rampGain=1,rampTarget=1,rampStep=0;
    unsigned rampRemaining=0;
    bool rampInitialized=false;
    std::unique_ptr<SaturationQuality> saturation;
    std::unique_ptr<SaturationQuality> previousSaturation;
    std::atomic<unsigned> qualityRemaining{0},qualitySelected{1};
    unsigned qualityTotal=0,qualityWarmup=0;
    bool qualityInitialized=false;
    std::unique_ptr<Convolver> convolution;
    std::unique_ptr<Convolver> previousConvolution;
    std::atomic<unsigned> kernelRemaining{0};
    unsigned kernelTotal=0;
    unsigned kernelWarmup=0;
    std::array<float,1024> kernelScratch{}; //512 stereo frames, fixed at prepare.
    Effect* kernelPendingSource=nullptr;
    double kernelPreviousParameters[8]{};
    bool kernelPendingFade=false;
    Effect(int type, double rate) : kind(type), sampleRate(rate) {
        if (type==4) {
            saturation=std::make_unique<SaturationQuality>();
            previousSaturation=std::make_unique<SaturationQuality>();
        }
    }
};
extern "C" __declspec(dllexport) void* __cdecl fx_create(int kind, double rate) {
    if (kind < 0 || kind > 11 || kind == 8 || kind == 9 || !std::isfinite(rate) || rate < 8000 || rate > maximumDspRate) return nullptr;
    try { return new Effect(kind, rate); } catch (...) { return nullptr; }
}
extern "C" __declspec(dllexport) unsigned __cdecl fx_quality_version() { return 1; }
extern "C" __declspec(dllexport) int __cdecl fx_parameters(void* pointer, const double* values, int count) {
    if (!pointer || !values || count < 1 || count > 8) return 0;
    for (int i = 0; i < count; ++i) if (!std::isfinite(values[i])) return 0;
    auto* effect = static_cast<Effect*>(pointer);
    effect->revision.fetch_add(1, std::memory_order_acq_rel);
    for (int i = 0; i < count; ++i) effect->target[i].store(values[i], std::memory_order_relaxed);
    effect->revision.fetch_add(1, std::memory_order_release);
    return 1;
}
extern "C" __declspec(dllexport) void __cdecl fx_destroy(void* pointer) { delete static_cast<Effect*>(pointer); }
extern "C" __declspec(dllexport) unsigned __cdecl fx_version() { return 1; }
extern "C" __declspec(dllexport) unsigned __cdecl fx_max_rate() { return static_cast<unsigned>(maximumDspRate); }
extern "C" __declspec(dllexport) void* __cdecl fx_create_hrtf(double rate, const double* p) {
    if (!p || !std::isfinite(rate) || rate < 8000 || rate > maximumDspRate) return nullptr;
    for (int i = 0; i < 6; ++i) if (!std::isfinite(p[i])) return nullptr;
    const HRESULT apartment = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    HMODULE library = LoadLibraryExW(L"HrtfApo.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
    std::array<std::vector<float>, 4> impulse;
    bool success = false;
    try {
        success = library && hrtfImpulse(library, p[0] - p[2] / 2, p[1], p[3], impulse[0], impulse[2])
                          && hrtfImpulse(library, p[0] + p[2] / 2, p[1], p[3], impulse[1], impulse[3]);
    } catch (...) { success = false; }
    if (library) FreeLibrary(library);
    if (SUCCEEDED(apartment)) CoUninitialize();
    if (!success) return nullptr;
    try {
        for (auto& ir : impulse) ir = resampleIR(ir, rate);
        auto effect = std::make_unique<Effect>(8, rate);
        effect->convolution = std::make_unique<Convolver>(impulse);
        effect->convolution->wet = static_cast<float>(p[4] / 100);
        effect->convolution->gain = static_cast<float>(std::pow(10.0, p[5] / 20));
        const double mix[2]{p[4], p[5]}; fx_parameters(effect.get(), mix, 2);
        return effect.release();
    } catch (...) { return nullptr; }
}
// IR channels: mono -> identical independent L/R; stereo -> LL/RR;
// four channels -> LL, LR, RL, RR (true-stereo matrix). No normalization:
// measured IR gain is data; user controls wet and output explicitly.
extern "C" __declspec(dllexport) void* __cdecl fx_create_convolution(double rate, const float* data,
        unsigned frames, unsigned channels, double sourceRate, double wet, double outputDB) {
    if (!data || !frames || (channels != 1 && channels != 2 && channels != 4)
        || !std::isfinite(rate) || rate < 8000 || rate > maximumDspRate
        || !std::isfinite(sourceRate) || sourceRate < 8000 || sourceRate > maximumDspRate
        || frames > sourceRate * 2 || !std::isfinite(wet) || wet < 0 || wet > 100
        || !std::isfinite(outputDB) || outputDB < -48 || outputDB > 12) return nullptr;
    try {
        std::array<std::vector<float>, 4> impulse;
        for (auto& route : impulse) route.resize(frames);
        for (unsigned frame = 0; frame < frames; ++frame) {
            for (unsigned channel = 0; channel < channels; ++channel)
                if (!std::isfinite(data[frame * channels + channel])) return nullptr;
            if (channels == 4) {
                for (int route = 0; route < 4; ++route) impulse[route][frame] = data[frame * 4 + route];
            } else {
                impulse[0][frame] = data[frame * channels];
                impulse[3][frame] = data[frame * channels + channels - 1];
            }
        }
        for (auto& ir : impulse) ir = resampleIR(ir, rate, sourceRate);
        auto effect = std::make_unique<Effect>(9, rate);
        effect->convolution = std::make_unique<Convolver>(impulse);
        effect->convolution->wet = static_cast<float>(wet / 100);
        effect->convolution->gain = static_cast<float>(std::pow(10., outputDB / 20));
        const double mix[2]{wet, outputDB}; fx_parameters(effect.get(), mix, 2);
        return effect.release();
    } catch (...) { return nullptr; }
}
// Additive candidate ABI. CONTROL ONLY: caller holds its BASS channel lock;
// both contexts were prepared off audio, at the same rate and kernel kind.
// Swaps move-only ownership; source receives retired kernels and is destroyed
// by the control owner AFTER unlocking. Audio never deletes a Convolver.
extern "C" __declspec(dllexport) unsigned __cdecl fx_kernel_transition_version() { return 1; }
extern "C" __declspec(dllexport) unsigned __cdecl fx_quality_transition_version() { return 1; }
extern "C" __declspec(dllexport) unsigned __cdecl fx_quality_transition_remaining(void* pointer) {
    return pointer?static_cast<Effect*>(pointer)->qualityRemaining.load(std::memory_order_acquire):0;
}
extern "C" __declspec(dllexport) unsigned __cdecl fx_quality_selected(void* pointer) {
    if (!pointer || static_cast<Effect*>(pointer)->kind!=4) return 0;
    return static_cast<Effect*>(pointer)->qualitySelected.load(std::memory_order_acquire);
}
extern "C" __declspec(dllexport) unsigned __cdecl fx_kernel_transition_remaining(void* pointer) {
    return pointer?static_cast<Effect*>(pointer)->kernelRemaining.load(std::memory_order_acquire):0;
}
extern "C" __declspec(dllexport) int __cdecl fx_kernel_transition(void* target,void* prepared,double milliseconds) {
    if (!target || !prepared || target==prepared || !std::isfinite(milliseconds) || milliseconds<0 || milliseconds>5000) return 0;
    auto& e=*static_cast<Effect*>(target); auto& next=*static_cast<Effect*>(prepared);
    if ((e.kind!=8 && e.kind!=9) || e.kind!=next.kind || e.sampleRate!=next.sampleRate
            || !e.convolution || !next.convolution || next.previousConvolution
            || e.kernelRemaining.load(std::memory_order_acquire)) return 0;
    for (unsigned index=0;index<8;++index)
        e.kernelPreviousParameters[index]=e.target[index].load(std::memory_order_relaxed);
    const unsigned count=milliseconds>0?std::max(1u,static_cast<unsigned>(std::round(e.sampleRate*milliseconds/1000.))):0;
    auto retired=std::move(e.previousConvolution);
    auto old=std::move(e.convolution);
    e.convolution=std::move(next.convolution);
    if (count) {
        e.previousConvolution=std::move(old);
        next.convolution=std::move(retired);
    } else {
        next.convolution=std::move(old);
        next.previousConvolution=std::move(retired);
    }
    double parameters[8]{};
    for (unsigned index=0;index<8;++index) parameters[index]=next.target[index].load(std::memory_order_relaxed);
    fx_parameters(&e,parameters,8);
    e.kernelTotal=count;
    e.kernelWarmup=count?convolutionBlock:0;
    e.kernelRemaining.store(count+e.kernelWarmup,std::memory_order_release);
    e.kernelPendingSource=&next;
    e.kernelPendingFade=count!=0;
    return 1;
}
// Transaction rollback is permitted only before rendering or Commit, under
// the SAME channel lock. Restore whole kernel/history, not merely GUI params.
extern "C" __declspec(dllexport) int __cdecl fx_kernel_transition_undo(void* target,void* prepared) {
    if (!target || !prepared) return 0;
    auto& e=*static_cast<Effect*>(target); auto& next=*static_cast<Effect*>(prepared);
    if (e.kernelPendingSource!=&next) return 0;
    if (e.kernelPendingFade) {
        auto retired=std::move(next.convolution);
        next.convolution=std::move(e.convolution);
        e.convolution=std::move(e.previousConvolution);
        e.previousConvolution=std::move(retired);
    } else {
        std::swap(e.convolution,next.convolution);
        e.previousConvolution=std::move(next.previousConvolution);
    }
    fx_parameters(&e,e.kernelPreviousParameters,8);
    e.kernelRemaining.store(0,std::memory_order_release);
    e.kernelPendingSource=nullptr;
    return 1;
}
extern "C" __declspec(dllexport) int __cdecl fx_kernel_transition_commit(void* target,void* prepared) {
    if (!target || !prepared) return 0;
    auto& e=*static_cast<Effect*>(target);
    if (e.kernelPendingSource!=prepared) return 0;
    e.kernelPendingSource=nullptr; return 1;
}
extern "C" __declspec(dllexport) void __cdecl fx_process(void* pointer, float* samples, unsigned frames) {
    if (!pointer || !samples) return;
    auto& e = *static_cast<Effect*>(pointer);
    double p[8]{};
    const auto before = e.revision.load(std::memory_order_acquire);
    for (int j = 0; j < 8; ++j) p[j] = e.target[j].load(std::memory_order_relaxed);
    const auto after = e.revision.load(std::memory_order_acquire);
    if (!(before & 1) && before == after) std::copy(p, p + 8, e.active);
    else std::copy(e.active, e.active + 8, p); // Bounded read; never spin on the audio thread.
    if (e.convolution) {
        if (frames) e.kernelPendingSource=nullptr; // Too late for pre-render undo.
        e.convolution->wet = static_cast<float>(p[0] / 100);
        e.convolution->gain = static_cast<float>(std::pow(10., p[1] / 20));
        unsigned remaining=e.kernelRemaining.load(std::memory_order_relaxed);
        unsigned position=0;
        while (remaining && position<frames) {
            const unsigned count=std::min({512u,remaining,frames-position});
            float* current=samples+2*position;
            std::copy(current,current+2*count,e.kernelScratch.begin());
            e.convolution->process(current,count);
            e.previousConvolution->process(e.kernelScratch.data(),count);
            for (unsigned index=0;index<count;++index) {
                const unsigned elapsed=e.kernelTotal+e.kernelWarmup-remaining+index+1;
                const double blend=elapsed<=e.kernelWarmup?0.:
                    static_cast<double>(elapsed-e.kernelWarmup)/e.kernelTotal;
                for (unsigned channel=0;channel<2;++channel) {
                    const unsigned sample=2*index+channel;
                    const double old=e.kernelScratch[sample];
                    current[sample]=static_cast<float>(old+(static_cast<double>(current[sample])-old)*blend);
                }
            }
            position+=count; remaining-=count;
        }
        e.kernelRemaining.store(remaining,std::memory_order_release);
        if (position) {
            if (position<frames) e.convolution->process(samples+2*position,frames-position);
            return;
        }
        e.convolution->process(samples, frames); return;
    }
    const double rate = e.sampleRate;
    // Prepare only coefficients consumed by this effect. Keep the original
    // expressions and precise FP order; this is not a quality/domain change.
    const double drive = (e.kind==4 || e.kind==10) ? std::pow(10.0, p[0] / 20.0) : 0.;
    const double output = e.kind==4 ? std::pow(10.0, p[2] / 20.0) : 0.;
    const double threshold = e.kind==5 ? std::pow(10.0, p[0] / 20.0) : 0.;
    const double closeThreshold = e.kind==5 ? threshold * std::pow(10.0, -p[4] / 20.0) : 0.;
    const double attack = e.kind==5 ? std::exp(-1.0 / (std::max(.1, p[1]) * rate / 1000.0)) : 0.;
    const double release = e.kind==5 ? std::exp(-1.0 / (std::max(1.0, p[3]) * rate / 1000.0)) : 0.;
    const double dc = e.kind==6 ? std::exp(-2.0 * pi * p[0] / rate) : 0.;
    const double quantum = e.kind==3 ? std::ldexp(1.0, std::clamp(static_cast<int>(p[0]), 2, 24) - 1) : 0.;
    if (e.kind==4) {
        const unsigned requested=1u << std::clamp(static_cast<int>(p[3]),0,2);
        if (p[4]<=0) e.qualityRemaining.store(0,std::memory_order_relaxed);
        if (requested!=e.saturation->factor && !e.qualityRemaining.load(std::memory_order_relaxed)) {
            if (e.qualityInitialized && p[4]>0) {
                *e.previousSaturation=*e.saturation; // bounded fixed state, no allocation.
                e.qualityTotal=std::max(1u,static_cast<unsigned>(std::round(rate*std::clamp(p[4],0.,5000.)/1000.)));
                e.qualityWarmup=requested>1?16u:0u;
                e.qualityRemaining.store(e.qualityTotal+e.qualityWarmup,std::memory_order_relaxed);
            }
            e.saturation->select(requested);
            e.qualitySelected.store(requested,std::memory_order_release);
        }
        e.qualityInitialized=true;
    }
    if (e.kind==10) {
        if (!e.rampInitialized || p[1]<=0) { e.rampGain=e.rampTarget=drive; e.rampRemaining=0; e.rampInitialized=true; }
        else if (e.rampTarget!=drive) {
            e.rampTarget=drive;
            e.rampRemaining=std::max(1u,static_cast<unsigned>(std::round(rate*p[1]/1000.)));
            e.rampStep=(drive-e.rampGain)/e.rampRemaining;
        }
    }
    for (unsigned i = 0; i < frames; ++i) {
        float* pair = samples + 2 * i;
        if (!std::isfinite(pair[0]) || !std::isfinite(pair[1])) { pair[0] = pair[1] = 0; continue; }
        const double sine = e.kind<=2 ? std::sin(2.0 * pi * e.phase) : 0.;
        switch (e.kind) {
        case 0: { // Tremolo: attenuation only, linked stereo.
            const int waveform = static_cast<int>(p[2]);
            const double wave = waveform == 1 ? 1.0 - 4.0 * std::abs(e.phase - .5)
                              : waveform == 2 ? (e.phase < .5 ? 1.0 : -1.0) : sine;
            const double amount = std::clamp(p[1] / 100.0, 0.0, 1.0);
            const float gain = static_cast<float>(1.0 - amount * (1.0 - wave) * .5);
            pair[0] *= gain; pair[1] *= gain;
            break;
        }
        case 1: { // Stereo balance auto-pan, never exceeding original side level.
            const double pan = sine * std::clamp(p[1] / 100.0, 0.0, 1.0);
            pair[0] *= static_cast<float>(std::sqrt(1.0 - std::max(0.0, pan)));
            pair[1] *= static_cast<float>(std::sqrt(1.0 + std::min(0.0, pan)));
            break;
        }
        case 2: { // Ring modulation: one carrier shared by L/R.
            const float multiplier = static_cast<float>(1.0 + (sine - 1.0) * p[1] / 100.0);
            pair[0] *= multiplier; pair[1] *= multiplier;
            break;
        }
        case 3: { // Quantization + sample hold. Clipping is intentional only here.
            if (e.holdCounter-- <= 0) {
                for (int channel = 0; channel < 2; ++channel)
                    e.held[channel] = static_cast<float>(std::round(std::clamp(static_cast<double>(pair[channel]), -1.0, 1.0) * quantum) / quantum);
                e.holdCounter = std::clamp(static_cast<int>(p[1]), 1, 64) - 1;
            }
            for (int channel = 0; channel < 2; ++channel)
                pair[channel] += static_cast<float>((e.held[channel] - pair[channel]) * p[2] / 100.0);
            break;
        }
        case 4: { // Soft saturation, independent output gain; not a true-peak limiter.
            float old[2]{pair[0],pair[1]};
            processSaturation(*e.saturation,pair,drive,p[1],output);
            const unsigned remaining=e.qualityRemaining.load(std::memory_order_relaxed);
            if (remaining) {
                processSaturation(*e.previousSaturation,old,drive,p[1],output);
                const unsigned elapsed=e.qualityTotal+e.qualityWarmup-remaining+1;
                const double blend=elapsed<=e.qualityWarmup?0.:
                    static_cast<double>(elapsed-e.qualityWarmup)/e.qualityTotal;
                for (unsigned channel=0;channel<2;++channel)
                    pair[channel]=static_cast<float>(static_cast<double>(old[channel])+
                        (static_cast<double>(pair[channel])-old[channel])*blend);
                e.qualityRemaining.store(remaining-1,std::memory_order_release);
            }
            break;
        }
        case 5: { // Stereo-linked gate: hold + hysteresis, no per-side image shift.
            const double peak = std::max(std::abs(pair[0]), std::abs(pair[1]));
            if (peak >= threshold) { e.gateOpen = true; e.gateHold = static_cast<int>(p[2] * rate / 1000.0); }
            else if (e.gateHold > 0) --e.gateHold;
            else if (peak < closeThreshold) e.gateOpen = false;
            const double target = e.gateOpen ? 1.0 : 0.0;
            const double coefficient = e.gateOpen ? attack : release;
            e.gain = target + (e.gain - target) * coefficient;
            pair[0] *= static_cast<float>(e.gain); pair[1] *= static_cast<float>(e.gain);
            break;
        }
        case 6: { // First-order DC blocker, no absolute audio clipping.
            for (int channel = 0; channel < 2; ++channel) {
                const double input = pair[channel];
                const double result = input - e.previousInput[channel] + dc * e.previousOutput[channel];
                e.previousInput[channel] = input; e.previousOutput[channel] = result;
                pair[channel] = static_cast<float>(result);
            }
            break;
        }
        case 7:
            if (p[0] != 0) std::swap(pair[0], pair[1]);
            if (p[1] != 0) pair[0] = -pair[0];
            if (p[2] != 0) pair[1] = -pair[1];
            break;
        case 10:
            if (e.rampRemaining) {
                e.rampGain+=e.rampStep;
                if (!--e.rampRemaining) e.rampGain=e.rampTarget;
            }
            pair[0] *= static_cast<float>(e.rampGain); pair[1] *= static_cast<float>(e.rampGain);
            break;
        case 11: {
            const double left = pair[0], right = pair[1];
            pair[0] = static_cast<float>((p[0] * left + p[1] * right) / 100);
            pair[1] = static_cast<float>((p[2] * left + p[3] * right) / 100);
            break;
        }
        }
        if (e.kind <= 2) { e.phase += p[0] / rate; e.phase -= std::floor(e.phase); }
    }
}
extern "C" __declspec(dllexport) void CALLBACK fx_bass_dsp(DWORD, DWORD, void* buffer, DWORD bytes, void* user) {
    fx_process(user, static_cast<float*>(buffer), bytes / (2 * sizeof(float)));
}

// Additive ABI1 extension for the legacy Mid/Side stage (priority2), NOT a
// rack slot or an algorithm change. Keep binary32 k and double intermediates
// identical to audio_engine._apply_stereo_width. MSVC /fp:precise, no contract.
struct StereoWidthState {
    std::atomic<float> coefficient{0.0f};
};
static_assert(std::atomic<float>::is_always_lock_free,
              "Stereo audio publication must never use a mutex");
extern "C" __declspec(dllexport) unsigned __cdecl stereo_version() { return 1; }
extern "C" __declspec(dllexport) void* __cdecl stereo_create() {
    return new (std::nothrow) StereoWidthState();
}
extern "C" __declspec(dllexport) int __cdecl stereo_coefficient(void* pointer, float value) {
    if (!pointer || !std::isfinite(value) || value < -1.0f || value > 2.0f) return 0;
    static_cast<StereoWidthState*>(pointer)->coefficient.store(value, std::memory_order_release);
    return 1;
}
extern "C" __declspec(dllexport) void __cdecl stereo_destroy(void* pointer) {
    delete static_cast<StereoWidthState*>(pointer);
}
extern "C" __declspec(dllexport) void __cdecl stereo_process(void* pointer, float* samples, unsigned frames) {
    if (!pointer || !samples) return;
    const double k = static_cast<double>(static_cast<StereoWidthState*>(pointer)->coefficient.load(std::memory_order_acquire));
    if (std::abs(k) < 1e-7) return;
    const double scale = 1.0 + k;
    for (unsigned frame = 0; frame < frames; ++frame) {
        float* pair = samples + 2 * frame;
        const double left = pair[0], right = pair[1];
        const double mid = 0.5 * (left + right);
        const double side = 0.5 * (left - right) * scale;
        pair[0] = static_cast<float>(mid + side);
        pair[1] = static_cast<float>(mid - side);
    }
}
extern "C" __declspec(dllexport) void CALLBACK stereo_bass_dsp(DWORD, DWORD, void* buffer, DWORD bytes, void* user) {
    stereo_process(user, static_cast<float*>(buffer), bytes / (2 * sizeof(float)));
}

// Legacy channel delay ABI1. Allocate maximum supported Time outside DSPPROC.
// One packed block snapshot: frames32/kind2/feedback7/gain8/send-active1.
// gain code is the existing Python four-pass nearest-even curve (control side).
static std::atomic<unsigned> nativeLegacyDelayCount{0};
struct LegacyDelayState {
    std::vector<float> ring;
    const unsigned capacity;
    const bool wetOnly;
    std::atomic<unsigned long long> parameters{1};
    std::atomic<unsigned> references{1};
    std::atomic<unsigned> midiTableStatus{0}; //0empty,1control preparation,2immutable ready.
    std::array<unsigned char,201*128> midiGain{}; // Prepared before DSP/map borrowers attach.
    unsigned length = 1, write = 0, filled = 0;
    LegacyDelayState(unsigned frames, bool wet)
        : ring(static_cast<size_t>(frames) * 2, 0.0f), capacity(frames), wetOnly(wet) {++nativeLegacyDelayCount;}
    ~LegacyDelayState(){--nativeLegacyDelayCount;}
};
static void delayMidiRetain(LegacyDelayState* state){state->references.fetch_add(1,std::memory_order_relaxed);}
static void delayMidiRelease(LegacyDelayState* state){if(state->references.fetch_sub(1,std::memory_order_acq_rel)==1)delete state;}
static bool delayMidiReady(LegacyDelayState* state){
    return state->midiTableStatus.load(std::memory_order_acquire)==2&&(state->parameters.load(std::memory_order_acquire)&(1ull<<63))!=0;
}
static void delayMidiSend(LegacyDelayState* state,unsigned send){
    const auto mask=127ull<<41;
    auto previous=state->parameters.load(std::memory_order_relaxed);
    while(!state->parameters.compare_exchange_weak(previous,(previous&~mask)|(static_cast<unsigned long long>(send)<<41),
        std::memory_order_release,std::memory_order_relaxed)){}
}
extern "C" __declspec(dllexport) unsigned __cdecl delay_midi_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl delay_midi_users(void* pointer){
    return pointer?static_cast<LegacyDelayState*>(pointer)->references.load():0;
}
extern "C" __declspec(dllexport) unsigned __cdecl delay_midi_live_count(){return nativeLegacyDelayCount.load();}
extern "C" __declspec(dllexport) unsigned __cdecl delay_midi_requested(void* pointer){
    return pointer?static_cast<unsigned>((static_cast<LegacyDelayState*>(pointer)->parameters.load()>>41)&127u):0;
}
extern "C" __declspec(dllexport) int __cdecl delay_midi_table(void* pointer,const unsigned char* table,unsigned count){
    if(!pointer||!table||count!=201*128)return 0;
    if(std::any_of(table,table+count,[](unsigned char gain){return gain>127;}))return 0;
    auto& state=*static_cast<LegacyDelayState*>(pointer);unsigned empty=0;
    if(!state.midiTableStatus.compare_exchange_strong(empty,1,std::memory_order_acq_rel))return 0;
    std::copy_n(table,count,state.midiGain.begin());state.midiTableStatus.store(2,std::memory_order_release);return 1;
}
extern "C" __declspec(dllexport) void __cdecl delay_midi_retain(void* pointer){if(pointer)delayMidiRetain(static_cast<LegacyDelayState*>(pointer));}
extern "C" __declspec(dllexport) void __cdecl delay_midi_release(void* pointer){if(pointer)delayMidiRelease(static_cast<LegacyDelayState*>(pointer));}
extern "C" __declspec(dllexport) int __cdecl delay_midi_send(void* pointer,unsigned send){
    if(!pointer||send>127||!delayMidiReady(static_cast<LegacyDelayState*>(pointer)))return 0;
    delayMidiSend(static_cast<LegacyDelayState*>(pointer),send);return 1;
}
extern "C" __declspec(dllexport) int __cdecl delay_midi_controls(void* pointer,unsigned frames,unsigned kind,unsigned feedback,unsigned level){
    if(!pointer||!frames||kind>3||feedback>100||level>200)return 0;
    auto& state=*static_cast<LegacyDelayState*>(pointer);
    if(frames>state.capacity||state.midiTableStatus.load(std::memory_order_acquire)!=2)return 0;
    const unsigned long long settings=frames|(static_cast<unsigned long long>(kind)<<32)|
        (static_cast<unsigned long long>(feedback)<<34)|(static_cast<unsigned long long>(level)<<48)|(1ull<<63);
    auto previous=state.parameters.load(std::memory_order_relaxed);
    // Preserve the concurrent native raw send while changing only global
    // time/type/feedback/level. No stale Python shadow can overwrite that send.
    while(!state.parameters.compare_exchange_weak(previous,settings|((previous&(1ull<<63))?previous&(127ull<<41):0),
        std::memory_order_release,std::memory_order_relaxed)){}
    return 1;
}
static_assert(std::atomic<unsigned long long>::is_always_lock_free,
              "Delay parameter publication must never use a mutex");
extern "C" __declspec(dllexport) unsigned __cdecl delay_version() { return 1; }
extern "C" __declspec(dllexport) void* __cdecl delay_create(unsigned capacity, int wetOnly) {
    if (!capacity || (wetOnly != 0 && wetOnly != 1)) return nullptr;
    try { return new LegacyDelayState(capacity, wetOnly != 0); }
    catch (...) { return nullptr; } // Never unwind a C ABI call; no callback allocation.
}
extern "C" __declspec(dllexport) void __cdecl delay_destroy(void* pointer) {
    if(pointer)delayMidiRelease(static_cast<LegacyDelayState*>(pointer));
}
extern "C" __declspec(dllexport) int __cdecl delay_parameters(void* pointer,
    unsigned frames, unsigned kind, unsigned feedback, unsigned gain, unsigned active) {
    if (!pointer || !frames || kind > 3 || feedback > 100 || gain > 127 || active > 1) return 0;
    auto& state = *static_cast<LegacyDelayState*>(pointer);
    if (frames > state.capacity) return 0;
    const unsigned long long packed = frames | (static_cast<unsigned long long>(kind) << 32)
        | (static_cast<unsigned long long>(feedback) << 34)
        | (static_cast<unsigned long long>(gain) << 41)
        | (static_cast<unsigned long long>(active) << 49);
    state.parameters.store(packed, std::memory_order_release);
    return 1;
}
extern "C" __declspec(dllexport) void __cdecl delay_process(void* pointer, float* samples, unsigned frames) {
    if (!pointer || !samples || !frames) return;
    auto& state = *static_cast<LegacyDelayState*>(pointer);
    const auto packed = state.parameters.load(std::memory_order_acquire);
    const bool midi=(packed&(1ull<<63))!=0;
    const unsigned send=static_cast<unsigned>((packed>>41)&127u);
    if (midi?send==0:!(packed & (1ull << 49))) {
        state.write = state.filled = 0;
        if (state.wetOnly) std::fill_n(samples, static_cast<size_t>(frames) * 2, 0.0f);
        return;
    }
    const unsigned length = static_cast<unsigned>(packed);
    if (length != state.length) {
        state.length = length; state.write = state.filled = 0;
        // Fresh fill overwrites every used cell before any delayed sample is
        // read. Clearing capacity-sized storage would only waste callback time.
    }
    const unsigned kind = static_cast<unsigned>((packed >> 32) & 3);
    if (!kind) {
        if (state.wetOnly) std::fill_n(samples, static_cast<size_t>(frames) * 2, 0.0f);
        return;
    }
    const unsigned gainCode = midi?state.midiGain[static_cast<size_t>((packed>>48)&255u)*128+send]:
        static_cast<unsigned>((packed >> 41) & 255);
    const float gain = static_cast<float>(std::min(static_cast<double>(gainCode) / 128.0, .95));
    const float feedback = static_cast<float>(static_cast<double>((packed >> 34) & 127) / 100.0);
    for (unsigned i = 0; i < frames; ++i) {
        float* pair = samples + static_cast<size_t>(i) * 2;
        float* ring = state.ring.data() + static_cast<size_t>(state.write) * 2;
        const float left = pair[0], right = pair[1];
        if (state.filled >= length) {
            float wetLeft = ring[0], wetRight = ring[1];
            if (kind == 2) std::swap(wetLeft, wetRight);
            else if (kind == 3) {
                const float sum = wetLeft + wetRight;
                wetLeft = wetRight = .5f * sum;
            }
            pair[0] = state.wetOnly ? wetLeft : left + wetLeft;
            pair[1] = state.wetOnly ? wetRight : right + wetRight;
            // Explicit binary32 stages reproduce NumPy float32 ufuncs, not a
            // reassociated double/FMA expression. Build uses /fp:precise.
            const float feedLeft = wetLeft * feedback, feedRight = wetRight * feedback;
            const float sumLeft = left + feedLeft, sumRight = right + feedRight;
            ring[0] = sumLeft * gain; ring[1] = sumRight * gain;
        } else {
            if (state.wetOnly) pair[0] = pair[1] = 0.0f;
            ring[0] = left * gain; ring[1] = right * gain;
            ++state.filled;
        }
        if (++state.write == length) state.write = 0;
    }
}
extern "C" __declspec(dllexport) void CALLBACK delay_bass_dsp(unsigned, unsigned, void* buffer, unsigned bytes, void* user) {
    delay_process(user, static_cast<float*>(buffer), bytes / (2 * sizeof(float)));
}

// Meter ABI1: one DSP producer, serialized control-side readers. The three
// slots have exclusive producer/consumer/mailbox ownership (acq_rel exchange).
// Never read a concurrently written non-atomic slot, even with a seqlock.
struct MeterFrame {
    std::array<float, 4096> wave{};
    unsigned count = 0;
    unsigned long long revision = 0;
    std::array<unsigned long long, 512> timingStamp{}, timingFrames{};
    unsigned timingCount = 0;
};
struct MeterState {
    const unsigned channels, rate, stride;
    std::array<float, 4096> history{};
    std::array<MeterFrame, 3> slots{};
    unsigned write = 0, count = 0, producer = 0, consumer = 1;
    unsigned long long revision = 0;
    std::atomic<unsigned> published{2}; // low2 bits slot; bit2 new-data
    std::atomic<unsigned long long> peaks{0};
    std::atomic<bool> presenting{true}, registered{false};
    std::atomic<unsigned> references{1}; // control owner + BASS registration
    std::array<unsigned long long,512> timingStamp{}, timingFrames{};
    unsigned timingWrite=0, timingCount=0;
    unsigned long long frequency=0;
    MeterState(unsigned chans, unsigned hz, unsigned step)
        : channels(chans), rate(hz), stride(step) {
        LARGE_INTEGER value{}; QueryPerformanceFrequency(&value);
        frequency=static_cast<unsigned long long>(value.QuadPart);
    }
};
static_assert(std::atomic<unsigned>::is_always_lock_free && std::atomic<bool>::is_always_lock_free,
              "Meter publication/reference types must be lock-free");
static unsigned floatBits(float value) { unsigned bits; std::memcpy(&bits, &value, sizeof(bits)); return bits; }
static float bitsFloat(unsigned bits) { float value; std::memcpy(&value, &bits, sizeof(value)); return value; }
static unsigned long long meterPeakPair(float left, float right) {
    return floatBits(left) | (static_cast<unsigned long long>(floatBits(right)) << 32);
}
extern "C" __declspec(dllexport) unsigned __cdecl meter_version() { return 1; }
extern "C" __declspec(dllexport) void* __cdecl meter_create(unsigned channels, unsigned rate, unsigned stride) {
    if (!channels || !rate || !stride) return nullptr;
    return new (std::nothrow) MeterState(channels, rate, stride);
}
extern "C" __declspec(dllexport) void __cdecl meter_release(void* pointer) {
    if (pointer && static_cast<MeterState*>(pointer)->references.fetch_sub(1, std::memory_order_acq_rel) == 1)
        delete static_cast<MeterState*>(pointer);
}
extern "C" __declspec(dllexport) int __cdecl meter_arm(void* pointer) {
    if (!pointer) return 0;
    auto& state = *static_cast<MeterState*>(pointer);
    if (state.registered.exchange(true, std::memory_order_acq_rel)) return 0;
    state.references.fetch_add(1, std::memory_order_relaxed);
    return 1;
}
extern "C" __declspec(dllexport) void __cdecl meter_removed(void* pointer) {
    if (pointer && static_cast<MeterState*>(pointer)->registered.exchange(false, std::memory_order_acq_rel))
        meter_release(pointer);
}
extern "C" __declspec(dllexport) int __cdecl meter_is_registered(void* pointer) {
    return pointer && static_cast<MeterState*>(pointer)->registered.load(std::memory_order_acquire);
}
extern "C" __declspec(dllexport) void __cdecl meter_clear(void* pointer, int presenting) {
    // Control path only, producer stopped/briefly BASS_ChannelLock-ed; reader serialized.
    if (!pointer) return;
    auto& state = *static_cast<MeterState*>(pointer);
    state.presenting.store(presenting != 0, std::memory_order_release);
    state.peaks.store(0, std::memory_order_release);
    state.write = state.count = 0;
    state.timingWrite=state.timingCount=0;
    ++state.revision;
    state.producer = 0; state.consumer = 1; state.published.store(2, std::memory_order_release);
    for (auto& slot : state.slots) { slot.count = slot.timingCount = 0; slot.revision = state.revision; }
}
extern "C" __declspec(dllexport) void __cdecl meter_process(void* pointer, const float* samples, unsigned bytes) {
    if (!pointer || !samples || bytes < 8) return;
    auto& state = *static_cast<MeterState*>(pointer);
    const unsigned frames = bytes / sizeof(float) / state.channels;
    if (!frames) return;
    LARGE_INTEGER stamp{}; QueryPerformanceCounter(&stamp);
    state.timingStamp[state.timingWrite]=static_cast<unsigned long long>(stamp.QuadPart);
    state.timingFrames[state.timingWrite]=frames;
    state.timingWrite=(state.timingWrite+1)%512;
    state.timingCount=std::min(512u,state.timingCount+1);
    const bool presenting=state.presenting.load(std::memory_order_acquire);
    float left = 0, right = 0;
    bool badLeft = false, badRight = false;
    for (unsigned frame = 0; presenting && frame < frames; ++frame) {
        const float* pair = samples + static_cast<size_t>(frame) * state.channels;
        const float l = pair[0], r = state.channels > 1 ? pair[1] : l;
        badLeft |= !std::isfinite(l); badRight |= !std::isfinite(r);
        left = std::max(left, std::abs(l)); right = std::max(right, std::abs(r));
        if (frame % state.stride == 0) {
            const float sum = l + r;
            float wave = state.channels > 1 ? sum * .5f : l;
            if (!std::isfinite(wave)) wave = 0;
            state.history[state.write] = wave;
            state.write = (state.write + 1) % 4096;
            state.count = std::min(4096u, state.count + 1);
        }
    }
    if (badLeft) left = 0;
    if (badRight) right = 0;
    auto previous = state.peaks.load(std::memory_order_acquire);
    const auto combined = meterPeakPair(std::max(left, bitsFloat(static_cast<unsigned>(previous))),
                                       std::max(right, bitsFloat(static_cast<unsigned>(previous >> 32))));
    if (!state.peaks.compare_exchange_strong(previous, combined, std::memory_order_acq_rel)) {
        // Only the one reader can conflict and writes zero; single producer.
        // Preserve the larger pending window across the exchange boundary.
        const auto retry = meterPeakPair(std::max(left, bitsFloat(static_cast<unsigned>(previous))),
                                        std::max(right, bitsFloat(static_cast<unsigned>(previous >> 32))));
        state.peaks.compare_exchange_strong(previous, retry, std::memory_order_acq_rel);
    }
    auto& slot = state.slots[state.producer];
    slot.timingCount=state.timingCount;
    const unsigned timingStart=(state.timingWrite+512-state.timingCount)%512;
    // The logical ring order is exactly the old modulo traversal, but each
    // physical contiguous span can be copied directly. Publication cadence,
    // exclusive triple-buffer ownership and every recorded sample remain.
    const unsigned timingFirst=std::min(state.timingCount,512u-timingStart);
    std::copy_n(state.timingStamp.data()+timingStart,timingFirst,slot.timingStamp.data());
    std::copy_n(state.timingStamp.data(),state.timingCount-timingFirst,slot.timingStamp.data()+timingFirst);
    std::copy_n(state.timingFrames.data()+timingStart,timingFirst,slot.timingFrames.data());
    std::copy_n(state.timingFrames.data(),state.timingCount-timingFirst,slot.timingFrames.data()+timingFirst);
    slot.count = state.count; slot.revision = ++state.revision;
    const unsigned start = (state.write + 4096 - state.count) % 4096;
    const unsigned waveFirst=std::min(state.count,4096u-start);
    std::copy_n(state.history.data()+start,waveFirst,slot.wave.data());
    std::copy_n(state.history.data(),state.count-waveFirst,slot.wave.data()+waveFirst);
    state.producer = state.published.exchange(state.producer | 4, std::memory_order_acq_rel) & 3;
}
extern "C" __declspec(dllexport) void __cdecl meter_peaks(void* pointer, float* output) {
    if (!pointer || !output) return;
    const auto pair = static_cast<MeterState*>(pointer)->peaks.exchange(0, std::memory_order_acq_rel);
    output[0] = bitsFloat(static_cast<unsigned>(pair)); output[1] = bitsFloat(static_cast<unsigned>(pair >> 32));
}
extern "C" __declspec(dllexport) unsigned __cdecl meter_snapshot(void* pointer, float* output,
    unsigned capacity, unsigned long long* revision) {
    if (!pointer || !output || capacity < 4096 || !revision) return 0;
    auto& state = *static_cast<MeterState*>(pointer);
    if (state.published.load(std::memory_order_acquire) & 4)
        state.consumer = state.published.exchange(state.consumer, std::memory_order_acq_rel) & 3;
    const auto& slot = state.slots[state.consumer];
    std::copy_n(slot.wave.data(), slot.count, output);
    *revision = slot.revision;
    return slot.count;
}
extern "C" __declspec(dllexport) void CALLBACK meter_bass_dsp(unsigned, unsigned, void* buffer, unsigned bytes, void* user) {
    if (!buffer) { meter_removed(user); return; } // BASS removal notification, no PCM processing.
    meter_process(user, static_cast<const float*>(buffer), bytes);
}
// One serialized control reader shares the established triple-buffer ownership;
// no seqlock data race, allocation, Python/Qt call or PCM mutation in the DSP.
extern "C" __declspec(dllexport) unsigned __cdecl meter_timing_snapshot(void* pointer,
    unsigned long long* output,unsigned capacity,unsigned long long* frequency,unsigned long long* revision) {
    if(!pointer || !output || capacity<512 || !frequency || !revision) return 0;
    auto& state=*static_cast<MeterState*>(pointer);
    if(state.published.load(std::memory_order_acquire)&4)
        state.consumer=state.published.exchange(state.consumer,std::memory_order_acq_rel)&3;
    const auto& slot=state.slots[state.consumer];
    for(unsigned i=0;i<slot.timingCount;++i) { output[2*i]=slot.timingStamp[i];output[2*i+1]=slot.timingFrames[i]; }
    *frequency=state.frequency;*revision=slot.revision;
    return slot.timingCount;
}

// Limiter ABI1 candidate: retain the established block-local 4x FIR/risk
// algorithm, not a new certified true-peak detector. Storage depends on delay,
// never callback length. Python prepares the exact reference coefficients.
struct LimiterParameters { double ceiling = .8556009012083958, release = .0033277839454767255; };
struct LimiterState {
    const unsigned lookahead, grain, delay, future, ringCapacity;
    std::array<double, 68> kernels{};
    std::array<std::array<float, 2>, 8> history{};
    std::vector<std::array<float, 2>> pending, pendingNext, sourceRing;
    std::vector<double> peakRing, weights;
    std::array<LimiterParameters, 3> configs{};
    unsigned writer = 0, reader = 1;
    std::atomic<unsigned> published{2};
    double gain = 1;
    LimiterState(unsigned ahead, unsigned slice, const double* taps)
        : lookahead(ahead), grain(slice), delay(ahead + 8),
          future((ahead + slice - 1) / slice), ringCapacity(delay + 16 + slice),
          pending(delay), pendingNext(delay), sourceRing(ringCapacity),
          peakRing(static_cast<size_t>(future) + 1), weights(static_cast<size_t>(future) + 1) {
        std::copy_n(taps, 68, kernels.data());
        for (unsigned i = 0; i <= future; ++i)
            weights[i] = std::max(0.0, 1.0 - static_cast<double>(i) * grain / lookahead);
    }
    float sample(long long position, unsigned channel, const float* source, unsigned written) const {
        if (position < 0) return history[static_cast<size_t>(position + 8)][channel];
        if (position < delay) return pending[static_cast<size_t>(position)][channel];
        const auto input = static_cast<unsigned>(position - delay);
        if (input < written) return sourceRing[input % ringCapacity][channel];
        const float value = source[static_cast<size_t>(input) * 2 + channel];
        return std::isfinite(value) ? value : 0.0f;
    }
    double peakBlock(unsigned block, unsigned count, const float* source, unsigned written) const {
        double maximum = 0;
        const auto end = std::min(static_cast<unsigned long long>(count) + lookahead,
                                  (static_cast<unsigned long long>(block) + 1) * grain);
        for (auto position = static_cast<unsigned long long>(block) * grain; position < end; ++position) {
            // SIMD across independent L/R, never across summation taps:
            // ascending tap order and binary64 rounding remain unchanged.
            __m128d sums[4] = {_mm_setzero_pd(),_mm_setzero_pd(),_mm_setzero_pd(),_mm_setzero_pd()};
            for (unsigned tap = 0; tap < 17; ++tap) {
                const auto index = static_cast<long long>(position) - 8 + tap;
                const auto pair = _mm_set_pd(sample(index,1,source,written),sample(index,0,source,written));
                for (unsigned phase = 0; phase < 4; ++phase)
                    sums[phase] = _mm_add_pd(sums[phase],_mm_mul_pd(pair,_mm_set1_pd(kernels[phase*17+tap])));
            }
            const auto mask = _mm_castsi128_pd(_mm_set1_epi64x(0x7fffffffffffffffll));
            auto peaks = _mm_setzero_pd();
            for (const auto sum : sums) peaks = _mm_max_pd(peaks,_mm_and_pd(sum,mask));
            alignas(16) double values[2]; _mm_store_pd(values,peaks);
            maximum = std::max(maximum,std::max(values[0],values[1]));
        }
        return maximum;
    }
};
extern "C" __declspec(dllexport) unsigned __cdecl limiter_version() { return 1; }
extern "C" __declspec(dllexport) void* __cdecl limiter_create(unsigned lookahead, unsigned grain, const double* kernels) {
    if (!lookahead || !grain || !kernels || lookahead > 1000000 || grain > 1000000) return nullptr;
    for (unsigned i = 0; i < 68; ++i) if (!std::isfinite(kernels[i])) return nullptr;
    try { return new LimiterState(lookahead, grain, kernels); }
    catch (...) { return nullptr; }
}
extern "C" __declspec(dllexport) void __cdecl limiter_destroy(void* pointer) {
    delete static_cast<LimiterState*>(pointer); // control-only, DSP detached first
}
extern "C" __declspec(dllexport) int __cdecl limiter_parameters(void* pointer, double ceiling, double release) {
    if (!pointer || !std::isfinite(ceiling) || !std::isfinite(release) || ceiling <= 0 || release <= 0 || release > 1) return 0;
    auto& state = *static_cast<LimiterState*>(pointer);
    state.configs[state.writer] = {ceiling, release};
    state.writer = state.published.exchange(state.writer | 4, std::memory_order_acq_rel) & 3;
    return 1;
}
extern "C" __declspec(dllexport) void __cdecl limiter_process(void* pointer, float* samples, unsigned count) {
    if (!pointer || !samples || !count) return;
    auto& state = *static_cast<LimiterState*>(pointer);
    if (state.published.load(std::memory_order_acquire) & 4)
        state.reader = state.published.exchange(state.reader, std::memory_order_acq_rel) & 3;
    const auto parameters = state.configs[state.reader];
    const auto ringCount = state.future + 1;
    for (unsigned block = 0; block < ringCount; ++block)
        state.peakRing[block] = state.peakBlock(block, count, samples, 0);
    const unsigned blocks = count / state.grain + (count % state.grain != 0);
    for (unsigned block = 0; block < blocks; ++block) {
        const unsigned start = block * state.grain;
        if (block) state.peakRing[(block + state.future) % ringCount] = state.peakBlock(block + state.future, count, samples, start);
        double risk = 0;
        for (unsigned offset = 0; offset < ringCount; ++offset) {
            const double peak = state.peakRing[(block + offset) % ringCount];
            const double value = std::max(0.0, 1.0 - parameters.ceiling / std::max(peak, 1e-12)) * state.weights[offset];
            risk = std::max(risk, value);
        }
        state.gain = std::min(1.0 - risk, state.gain + parameters.release * (1.0 - state.gain));
        const float gain = static_cast<float>(state.gain);
        const unsigned end = std::min(count, start + state.grain);
        // Save original inputs before replacing them with delayed output.
        for (unsigned i = start; i < end; ++i) {
            for (unsigned channel = 0; channel < 2; ++channel) {
                const float value = samples[static_cast<size_t>(i) * 2 + channel];
                state.sourceRing[i % state.ringCapacity][channel] = std::isfinite(value) ? value : 0.0f;
            }
        }
        for (unsigned i = start; i < end; ++i)
            for (unsigned channel = 0; channel < 2; ++channel)
                samples[static_cast<size_t>(i) * 2 + channel] = state.sample(i, channel, samples, end) * gain;
    }
    auto nextHistory = state.history;
    for (unsigned i = 0; i < 8; ++i)
        for (unsigned channel = 0; channel < 2; ++channel)
            nextHistory[i][channel] = state.sample(static_cast<long long>(count) - 8 + i, channel, samples, count);
    for (unsigned i = 0; i < state.delay; ++i)
        for (unsigned channel = 0; channel < 2; ++channel)
            state.pendingNext[i][channel] = state.sample(static_cast<unsigned long long>(count) + i, channel, samples, count);
    state.history = nextHistory;
    state.pending.swap(state.pendingNext);
}
extern "C" __declspec(dllexport) void CALLBACK limiter_bass_dsp(DWORD, DWORD, void* buffer, DWORD bytes, void* user) {
    // Lifetime is owned by a stream owner which detaches before destruction.
    limiter_process(user, static_cast<float*>(buffer), bytes / (2 * sizeof(float)));
}

// Development observation ABI: time native GetData itself, not the Python/GIL
// delay after it returns. No output-backend replacement or underrun inference.
using NativeGetData = unsigned (WINAPI*)(unsigned,void*,unsigned);
struct RenderProbe {
    NativeGetData getData;
    std::array<unsigned long long,4096> ticks{};
    unsigned count=0,write=0;
    unsigned long long frequency=0;
    explicit RenderProbe(NativeGetData function):getData(function) {
        LARGE_INTEGER value{}; QueryPerformanceFrequency(&value); frequency=value.QuadPart;
    }
};
extern "C" __declspec(dllexport) unsigned __cdecl render_probe_version() { return 1; }
extern "C" __declspec(dllexport) void* __cdecl render_probe_create(void* getData) {
    if (!getData) return nullptr;
    return new (std::nothrow) RenderProbe(reinterpret_cast<NativeGetData>(getData));
}
extern "C" __declspec(dllexport) void __cdecl render_probe_destroy(void* pointer) { delete static_cast<RenderProbe*>(pointer); }
extern "C" __declspec(dllexport) unsigned __cdecl render_probe_get_data(void* pointer,unsigned stream,void* buffer,unsigned bytes) {
    if (!pointer) return 0xffffffffu;
    auto& state=*static_cast<RenderProbe*>(pointer);
    LARGE_INTEGER begin{},end{};QueryPerformanceCounter(&begin);
    const unsigned result=state.getData(stream,buffer,bytes);
    QueryPerformanceCounter(&end);
    state.ticks[state.write]=end.QuadPart-begin.QuadPart;
    state.write=(state.write+1)%4096;state.count=std::min(4096u,state.count+1);
    return result;
}
extern "C" __declspec(dllexport) unsigned __cdecl render_probe_snapshot(void* pointer,unsigned long long* output,unsigned long long* frequency) {
    if (!pointer || !output || !frequency) return 0;
    const auto& state=*static_cast<RenderProbe*>(pointer);
    const unsigned start=(state.write+4096-state.count)%4096;
    for (unsigned index=0;index<state.count;++index) output[index]=state.ticks[(start+index)%4096];
    *frequency=state.frequency;return state.count;
}

// Native output-callback measurement. Installed STREAMPROC/ASIOPROC/WASAPIPROC
// examples feed FLOAT decoders with GetData. Production ASIO/exclusive owners
// use gain1; development probes may independently use a quiet gain.
// Stamped atomic slots allow concurrent control snapshots without an audio lock.
using NativeError = unsigned (WINAPI*)();
using NativeAsioCPU = float (WINAPI*)();
static_assert(std::atomic<unsigned long long>::is_always_lock_free && std::atomic<double>::is_always_lock_free,
              "Playback observation atomics must not wait on audio threads");
struct OutputProbe {
    MidiPCMPhase* phase=nullptr;
    struct Sample {
        std::atomic<unsigned long long> stamp{0};
        std::array<std::atomic<unsigned long long>,6> values{};
    };
    NativeGetData getData;
    NativeError getError;
    unsigned stream;
    double gain;
    unsigned long long frequency=0;
    std::atomic<unsigned long long> admitted{0};
    std::array<Sample,4096> samples{};
    std::atomic<unsigned long long> completed{0}, shortBytes{0}, failures{0}, lateBlocks{0}, gaps{0};
    std::atomic<unsigned long long> lastStart{0}, lastEnd{0}, lastBytes{0};
    std::atomic<double> rate{0};
    unsigned long long previousStart=0, previousBytes=0;
    // ASIO has no hardware-underrun counter in the public BASSASIO ABI.
    // Track bounded source-supply progress separately from one long gap:
    // completion QPC minus delivered sample duration must not drift by more
    // than one whole block within the last256 completions. This is supply
    // lateness evidence, NOT a claim of a lost device buffer or audible click.
    std::atomic<bool> progressEnabled{false};
    std::array<double,256> progressPhases{};
    unsigned progressCount=0,progressCursor=0;
    unsigned long long progressBytes=0;
    bool progressActive=false;
    std::atomic<unsigned long long> progressIncidents{0};
    std::atomic<double> progressExcessMs{0},progressPeakMs{0};
    // bassasio.h ASIONOTIFYPROC(DWORD,void*): callback records only atomic
    // notices. Stop/Free/Init is performed later by the control owner.
    std::atomic<unsigned long long> asioRateNotices{0},asioResetNotices{0};
    NativeAsioCPU asioCPU=nullptr;
    std::atomic<unsigned long long> asioOverloadIncidents{0};
    std::atomic<double> asioProcessingPeak{0};
    bool asioWasOverloaded=false;
    OutputProbe(NativeGetData data,NativeError error,unsigned source,double outputGain):getData(data),getError(error),stream(source),gain(outputGain) {
        LARGE_INTEGER value{};QueryPerformanceFrequency(&value);frequency=value.QuadPart;
    }
    ~OutputProbe(){if(phase)phase->release();}
    unsigned render(void* buffer,unsigned bytes) {
        // BASSASIO GetCPU includes its processing after ASIOPROC, such as
        // SRC/format conversion. Read its published utilization at every
        // callback so a sparse Python/GUI poll cannot discard reported peaks.
        // This is not a per-buffer completion stamp or hardware-underrun count.
        if(asioCPU) {
            const double cpu=asioCPU();
            if(std::isfinite(cpu)&&cpu>=0) {
                if(cpu>asioProcessingPeak.load(std::memory_order_relaxed))asioProcessingPeak.store(cpu,std::memory_order_relaxed);
                const bool overloaded=cpu>=100;
                if(overloaded&&!asioWasOverloaded)asioOverloadIncidents.fetch_add(1,std::memory_order_relaxed);
                asioWasOverloaded=overloaded;
            }
        }
        const auto ticket=admitted.fetch_add(1,std::memory_order_relaxed);
        auto& record=samples[ticket%4096];
        record.stamp.store(2*(ticket+1)+1,std::memory_order_release);
        LARGE_INTEGER begin{},end{};QueryPerformanceCounter(&begin);
        if(phase)phase->add(5,stream,0,0,0,0,0,bytes,ticket+1);
        const unsigned raw=getData(stream,buffer,bytes);
        const unsigned error=raw==0xffffffffu?getError():0;
        const unsigned returned=raw==0xffffffffu?0:std::min(raw,bytes);
        if(gain!=1){
            auto* values=static_cast<float*>(buffer);
            for(unsigned index=0;index<returned/sizeof(float);++index)values[index]=static_cast<float>(values[index]*gain);
        }
        QueryPerformanceCounter(&end);
        if(phase)phase->add(6,stream,0,0,0,0,0,returned,ticket+1);
        const double hz=rate.load(std::memory_order_relaxed);
        if(hz>0) {
            const double budget=bytes/8.0/hz;
            if(returned<bytes)shortBytes.fetch_add(bytes-returned,std::memory_order_relaxed);
            if(error)failures.fetch_add(1,std::memory_order_relaxed);
            if((end.QuadPart-begin.QuadPart)/static_cast<double>(frequency)>budget)
                lateBlocks.fetch_add(1,std::memory_order_relaxed);
            // Called once per joined stereo device block, not source DSP slices.
            // More than one whole additional block of dispatch lateness.
            if(previousStart && (begin.QuadPart-previousStart)/static_cast<double>(frequency)>2*previousBytes/8.0/hz)
                gaps.fetch_add(1,std::memory_order_relaxed);
            if(progressEnabled.load(std::memory_order_relaxed)) {
                progressBytes+=bytes;
                const double completionPhase=end.QuadPart/static_cast<double>(frequency)-progressBytes/8.0/hz;
                double floor=completionPhase;
                for(unsigned index=0;index<progressCount;++index)floor=std::min(floor,progressPhases[index]);
                const double excess=std::max(0.0,completionPhase-floor-budget)*1000.0;
                const bool active=excess>0;
                if(active&&!progressActive)progressIncidents.fetch_add(1,std::memory_order_relaxed);
                progressActive=active;
                progressExcessMs.store(excess,std::memory_order_relaxed);
                if(excess>progressPeakMs.load(std::memory_order_relaxed))progressPeakMs.store(excess,std::memory_order_relaxed);
                // The first ASIO fill occurs during Start, before the steady
                // device-switch sequence (and includes SRC priming frames).
                // Its individual short/error/late checks remain active, but
                // it cannot anchor the steady completion-phase history.
                if(progressBytes!=bytes) {
                    progressPhases[progressCursor]=completionPhase;progressCursor=(progressCursor+1)%256;
                    if(progressCount<256)++progressCount;
                }
            }
            previousStart=begin.QuadPart;previousBytes=bytes;
        }
        lastStart.store(begin.QuadPart,std::memory_order_relaxed);
        lastEnd.store(end.QuadPart,std::memory_order_relaxed);
        lastBytes.store(bytes,std::memory_order_relaxed);
        completed.fetch_add(1,std::memory_order_release);
        const unsigned long long data[6]{static_cast<unsigned long long>(begin.QuadPart),
            static_cast<unsigned long long>(end.QuadPart),bytes,returned,error,GetCurrentThreadId()};
        for(unsigned index=0;index<6;++index)record.values[index].store(data[index],std::memory_order_relaxed);
        record.stamp.store(2*(ticket+1),std::memory_order_release);
        return returned;
    }
};
// Bind before output callback admission; remove/join output before unbinding.
extern "C" __declspec(dllexport) int __cdecl output_probe_phase(void* pointer,void* phase){
    if(!pointer)return 0;auto& owner=*static_cast<OutputProbe*>(pointer);auto* next=static_cast<MidiPCMPhase*>(phase);
    if(next)next->retain();auto* old=owner.phase;owner.phase=next;if(old)old->release();return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl output_probe_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl output_probe_create(void* data,void* error,unsigned source,double gain){
    if(!data||!error||!source||!std::isfinite(gain)||gain<0||gain>1)return nullptr;
    return new(std::nothrow)OutputProbe(reinterpret_cast<NativeGetData>(data),reinterpret_cast<NativeError>(error),source,gain);
}
extern "C" __declspec(dllexport) void __cdecl output_probe_destroy(void* pointer){delete static_cast<OutputProbe*>(pointer);}
extern "C" __declspec(dllexport) void __cdecl output_probe_observe(void* pointer,double rate){
    if(pointer && std::isfinite(rate) && rate>0)static_cast<OutputProbe*>(pointer)->rate.store(rate,std::memory_order_release);
}
extern "C" __declspec(dllexport) void __cdecl output_probe_health(void* pointer,unsigned long long* out){
    if(!pointer||!out)return;
    auto& s=*static_cast<OutputProbe*>(pointer);
    out[0]=s.completed.load(std::memory_order_acquire);out[1]=s.shortBytes.load();out[2]=s.failures.load();
    out[3]=s.lateBlocks.load();out[4]=s.gaps.load();out[5]=s.lastStart.load();out[6]=s.lastEnd.load();
    out[7]=s.lastBytes.load();out[8]=s.frequency;
}
// Control configuration before ASIO callback admission; no reset while live.
// Additive ABI preserves old health9 and old libraries' original behaviour.
extern "C" __declspec(dllexport) void __cdecl output_probe_progress_enable(void* pointer,int enabled){
    if(pointer)static_cast<OutputProbe*>(pointer)->progressEnabled.store(enabled!=0,std::memory_order_release);
}
extern "C" __declspec(dllexport) void __cdecl output_probe_progress_health(void* pointer,
        unsigned long long* incidents,double* current,double* peak){
    if(!pointer||!incidents||!current||!peak)return;
    auto& s=*static_cast<OutputProbe*>(pointer);
    *incidents=s.progressIncidents.load();*current=s.progressExcessMs.load();*peak=s.progressPeakMs.load();
}
extern "C" __declspec(dllexport) void CALLBACK output_probe_asio_notify(unsigned notice,void* pointer){
    if(!pointer)return;auto& s=*static_cast<OutputProbe*>(pointer);
    if(notice==1)s.asioRateNotices.fetch_add(1,std::memory_order_relaxed);
    else if(notice==2)s.asioResetNotices.fetch_add(1,std::memory_order_relaxed);
}
extern "C" __declspec(dllexport) void __cdecl output_probe_asio_notices(void* pointer,unsigned long long* output){
    if(!pointer||!output)return;auto& s=*static_cast<OutputProbe*>(pointer);
    output[0]=s.asioRateNotices.load();output[1]=s.asioResetNotices.load();
}
extern "C" __declspec(dllexport) void __cdecl output_probe_asio_cpu(void* pointer,void* cpu){
    if(pointer)static_cast<OutputProbe*>(pointer)->asioCPU=reinterpret_cast<NativeAsioCPU>(cpu);
}
extern "C" __declspec(dllexport) void __cdecl output_probe_asio_metrics(void* pointer,unsigned long long* incidents,double* peak){
    if(!pointer||!incidents||!peak)return;auto& s=*static_cast<OutputProbe*>(pointer);
    *incidents=s.asioOverloadIncidents.load();*peak=s.asioProcessingPeak.load();
}
extern "C" __declspec(dllexport) unsigned CALLBACK output_probe_stream(unsigned,void* buffer,unsigned bytes,void* user){
    return user?static_cast<OutputProbe*>(user)->render(buffer,bytes):0;
}
extern "C" __declspec(dllexport) unsigned CALLBACK output_probe_wasapi(void* buffer,unsigned bytes,void* user){
    return user?static_cast<OutputProbe*>(user)->render(buffer,bytes):0;
}
extern "C" __declspec(dllexport) unsigned CALLBACK output_probe_asio(BOOL input,unsigned,void* buffer,unsigned bytes,void* user){
    return user&&!input?static_cast<OutputProbe*>(user)->render(buffer,bytes):0;
}
extern "C" __declspec(dllexport) unsigned __cdecl output_probe_snapshot(void* pointer,unsigned long long* output,
        unsigned capacity,unsigned long long* frequency,unsigned long long* total){
    if(!pointer||!output||!frequency||!total)return 0;
    auto& state=*static_cast<OutputProbe*>(pointer);
    const auto end=state.admitted.load(std::memory_order_acquire);
    const auto start=end>4096?end-4096:0;
    unsigned copied=0;
    for(auto ticket=start;ticket<end&&copied<capacity;++ticket){
        auto& record=state.samples[ticket%4096];
        const auto expected=2*(ticket+1);
        if(record.stamp.load(std::memory_order_acquire)!=expected)continue;
        unsigned long long values[6]{};
        for(unsigned index=0;index<6;++index)values[index]=record.values[index].load(std::memory_order_relaxed);
        if(record.stamp.load(std::memory_order_acquire)!=expected)continue;
        std::copy_n(values,6,output+6*copied);++copied;
    }
    *frequency=state.frequency;*total=end;return copied;
}

// Received Note On -> containing final source PCM block. Control submissions
// are serialized by EngineRuntime.midi_lock; BASS serializes this stream's
// sync/DSP consumers. Audio callbacks only touch preallocated atomic storage.
using NativeGetPosition = unsigned long long (WINAPI*)(unsigned,unsigned);
struct PlaybackTiming {
    MidiPCMPhase* phase=nullptr;
    struct OriginHealth {
        unsigned magic,version,pid,reserved;
        unsigned long long epoch,frequency;
        alignas(8) volatile LONG64 losses;
    };
    static_assert(sizeof(OriginHealth)==40,"origin health ABI changed");
    struct OriginView {HANDLE mapping;OriginHealth* data;HANDLE process;};
    std::vector<OriginView> origins; // control/input only, never accessed by audio
    SRWLOCK originLock=SRWLOCK_INIT;
    std::atomic<unsigned> references{1};
    struct Ingress {
        std::atomic<unsigned long long> head{0},tail{0};
        std::array<std::array<std::atomic<unsigned long long>,7>,8> origins{};
    };
    struct Record {
        std::atomic<unsigned long long> stamp{0};
        std::array<std::atomic<unsigned long long>,15> values{};
        std::atomic<bool> moduloPosition{false};
        unsigned long long boundaryStart=~0ull; // Same serialized render owner.
    };
    NativeGetPosition position;
    unsigned stream,channels;
    unsigned long long frequency=0,cursor=0;
    std::unique_ptr<Ingress[]> ingress;
    std::array<Record,8192> records{};
    std::atomic<unsigned long long> admitted{0},omitted{0},stalls{0},fxLate{0},blocks{0};
    std::atomic<unsigned long long> missingOrigin{0},transportLoss{0},invalidOrigin{0};
    std::atomic<double> sampleRate{0};
    unsigned long long fxBegin=0;
    std::atomic<bool> freed{false};
    unsigned filterOwners=0; // Stream barrier serialized, not a render wait.
    PlaybackTiming(NativeGetPosition function,unsigned source,unsigned count):position(function),stream(source),channels(count),
        ingress(new Ingress[static_cast<size_t>(count)*128]) {
        LARGE_INTEGER value{};QueryPerformanceFrequency(&value);frequency=value.QuadPart;
    }
    ~PlaybackTiming(){if(phase)phase->release();for(auto& origin:origins){UnmapViewOfFile(origin.data);CloseHandle(origin.mapping);if(origin.process)CloseHandle(origin.process);}}
    void retain(){references.fetch_add(1,std::memory_order_relaxed);}
    void release(){if(references.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
    bool watchUnlocked(unsigned pid,unsigned long long epoch){
        for(auto& origin:origins)if(origin.data->pid==pid&&origin.data->epoch==epoch)return true;
        // Product observation capacity: fail closed, never silently evict a
        // producer that might still fail after its final musical frame.
        if(origins.size()>=1024)return false;
        wchar_t name[128]{};swprintf_s(name,L"Local\\ABMSynthMidiOriginV1-%u-%llu",pid,epoch);
        HANDLE mapping=OpenFileMappingW(FILE_MAP_READ,FALSE,name);if(!mapping)return false;
        auto* view=static_cast<OriginHealth*>(MapViewOfFile(mapping,FILE_MAP_READ,0,0,sizeof(OriginHealth)));
        if(!view||view->magic!=0x314f4d41||view->version!=1||view->pid!=pid||view->epoch!=epoch||view->frequency!=frequency){
            if(view)UnmapViewOfFile(view);CloseHandle(mapping);return false;
        }
        HANDLE process=OpenProcess(SYNCHRONIZE,FALSE,pid);
        try{origins.push_back({mapping,view,process});}catch(...){if(process)CloseHandle(process);UnmapViewOfFile(view);CloseHandle(mapping);return false;}
        return true;
    }
    bool watch(unsigned pid,unsigned long long epoch){
        AcquireSRWLockExclusive(&originLock);const bool result=watchUnlocked(pid,epoch);ReleaseSRWLockExclusive(&originLock);return result;
    }
    void receive(unsigned channel,unsigned key,unsigned long long tick,unsigned long long read=0,
                 unsigned long long pid=0,unsigned long long epoch=0,unsigned long long sequence=0,unsigned long long quality=2,unsigned intentionalMs=0) {
        if(channel>=channels||key>=128||!tick){omitted.fetch_add(1);return;}
        auto& queue=ingress[static_cast<size_t>(channel)*128+key];
        const auto tail=queue.tail.load(std::memory_order_relaxed),head=queue.head.load(std::memory_order_acquire);
        if(tail-head>=8){omitted.fetch_add(1);return;} // observation only, never alter MIDI
        const unsigned long long values[7]{tick,read?read:tick,pid,epoch,sequence,quality,static_cast<unsigned long long>(intentionalMs)*frequency/1000};
        for(unsigned i=0;i<7;++i)queue.origins[tail%8][i].store(values[i],std::memory_order_relaxed);
        queue.tail.store(tail+1,std::memory_order_release);
    }
    void accepted(unsigned data,unsigned long long pos,bool modulo=false) {
        const unsigned channel=data>>16,key=data&127,velocity=(data>>8)&255;
        if(!velocity||velocity==255||channel>=channels)return;
        auto& queue=ingress[static_cast<size_t>(channel)*128+key];
        auto head=queue.head.load(std::memory_order_relaxed);const auto tail=queue.tail.load(std::memory_order_acquire);
        unsigned long long arrival=0;
        unsigned long long origin[7]{};
        while(head<tail&&!arrival){
            for(unsigned i=0;i<7;++i)origin[i]=queue.origins[head%8][i].load(std::memory_order_relaxed);
            ++head;arrival=origin[0];
        }
        queue.head.store(head,std::memory_order_release);
        if(!arrival)return; // unregistered internal/file/UI note is not a sample
        if(pos==~0ull){omitted.fetch_add(1);return;}
        LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);
        const auto ticket=admitted.fetch_add(1,std::memory_order_relaxed);
        auto& row=records[ticket%8192];row.stamp.store(2*(ticket+1)+1,std::memory_order_release);
        row.moduloPosition.store(modulo,std::memory_order_relaxed);
        row.boundaryStart=~0ull;
        const unsigned long long values[15]{ticket+1,arrival,static_cast<unsigned long long>(tick.QuadPart),pos,channel,key,0,0,0,
            origin[1],origin[2],origin[3],origin[4],origin[5],origin[6]};
        for(unsigned i=0;i<15;++i)row.values[i].store(values[i],std::memory_order_relaxed);
        row.stamp.store(2*(ticket+1),std::memory_order_release);
    }
    void note(unsigned data) {
        const unsigned channel=data>>16,velocity=(data>>8)&255;
        if(!velocity||velocity==255||channel>=channels)return;
        if(phase)phase->add(2,stream,MIDI_EVENT_NOTE,data&65535u,channel);
        // v2 consumes registered origins once, at the accepted filter event.
        // A later SYNC/no-FILTER call cannot consume stale accepted positions.
        if(filterOwners)return;
        accepted(data,position(stream,BASS_POS_BYTE|BASS_POS_DECODE));
    }
    void pcm(unsigned bytes) {
        const auto start=position(stream,BASS_POS_BYTE|BASS_POS_DSP);
        if(start==~0ull){omitted.fetch_add(1);return;}
        LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);
        const double rate=sampleRate.load(std::memory_order_relaxed);
        if(fxBegin&&rate>0&&(tick.QuadPart-fxBegin)/static_cast<double>(frequency)>bytes/8.0/rate)
            fxLate.fetch_add(1,std::memory_order_relaxed);
        blocks.fetch_add(1,std::memory_order_release);
        if(phase)phase->add(4,stream,0,0,0,0,0,bytes,blocks.load());
        const auto end=admitted.load(std::memory_order_acquire);
        if(end-cursor>8192){omitted.fetch_add(end-cursor-8192);cursor=end-8192;}
        for(;cursor<end;++cursor) {
            auto& row=records[cursor%8192];const auto expected=2*(cursor+1);
            if(row.stamp.load(std::memory_order_acquire)!=expected)break;
            auto pos=row.values[3].load(std::memory_order_relaxed);
            if(row.moduloPosition.load(std::memory_order_relaxed)){
                // Resolve the DWORD event position only against the real
                // QWORD final DSP block. Never infer its epoch from DECODE.
                if(bytes>UINT_MAX){omitted.fetch_add(1);continue;}
                if(row.boundaryStart!=~0ull&&row.boundaryStart!=start){omitted.fetch_add(1);continue;}
                const auto delta=static_cast<unsigned>(static_cast<unsigned>(pos)-static_cast<unsigned>(start));
                if(delta>=bytes){
                    if(delta==bytes&&row.boundaryStart==~0ull&&start<=~0ull-bytes){row.boundaryStart=start+bytes;break;}
                    // Accepted filter callbacks occur before processing this
                    // block. Anything outside it (except its exact end) is an
                    // unqualified observation, not a note for a future wrap.
                    omitted.fetch_add(1);continue;
                }
                if(start>~0ull-delta){omitted.fetch_add(1);continue;}
                pos=start+delta;row.values[3].store(pos,std::memory_order_relaxed);
            }
            if(pos>=start+bytes)break;
            // A note position older than this block is still a pending note
            // for this same stream, but cannot be silently assigned to it.
            if(pos<start){omitted.fetch_add(1);continue;}
            row.values[7].store(bytes/8,std::memory_order_relaxed);
            row.values[8].store(start,std::memory_order_relaxed);
            row.values[6].store(tick.QuadPart,std::memory_order_release);
        }
    }
};
static void playbackTimingRetain(PlaybackTiming* timing){timing->retain();++timing->filterOwners;}
static void playbackTimingRelease(PlaybackTiming* timing){--timing->filterOwners;timing->release();}
static void playbackTimingAccepted(PlaybackTiming* timing,HSTREAM stream,const BASS_MIDI_EVENT* event){
    if(stream!=timing->stream||event->event!=MIDI_EVENT_NOTE)return;
    timing->accepted((event->chan<<16)|(event->param&65535u),event->pos,true);
}
extern "C" __declspec(dllexport) unsigned __cdecl playback_timing_position_version(){return 2;}
extern "C" __declspec(dllexport) int __cdecl midi_filter_gate_timing(void* pointer,void* timing,unsigned stream){
    if(!pointer)return 0;auto& gate=*static_cast<NativeMidiFilterGate*>(pointer);
    auto* next=static_cast<PlaybackTiming*>(timing);
    if(next&&(next->stream!=stream||next->freed.load()||(next!=gate.timing&&next->filterOwners)))return 0;
    if(next==gate.timing)return 1;
    if(next)playbackTimingRetain(next);auto* old=gate.timing;gate.timing=next;
    if(old)playbackTimingRelease(old);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_filter_gate_timing_publish(void* pointer,void* timing,unsigned stream,
    void* lockAddress,void* errorAddress,int* error){
    if(error){error[0]=0;error[1]=0;}if(!pointer||!lockAddress||!errorAddress)return -1;
    auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(lockAddress);
    auto getError=reinterpret_cast<int(WINAPI*)()>(errorAddress);
    if(!lock(stream,TRUE)){if(error)*error=getError();return 0;}
    const int result=midi_filter_gate_timing(pointer,timing,stream);
    if(error)error[1]=result;
    if(!lock(stream,FALSE)){if(error)*error=getError();return -2;}
    return result?1:-1;
}
// Caller owns source ChannelLock for attach/detach; no new audio callback.
extern "C" __declspec(dllexport) int __cdecl playback_timing_phase(void* pointer,void* phase){
    if(!pointer)return 0;auto& owner=*static_cast<PlaybackTiming*>(pointer);auto* next=static_cast<MidiPCMPhase*>(phase);
    if(next)next->retain();auto* old=owner.phase;owner.phase=next;if(old)old->release();return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl playback_timing_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl playback_timing_origin_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl playback_timing_watch_origin(void* pointer,unsigned pid,unsigned long long epoch){
    return pointer&&static_cast<PlaybackTiming*>(pointer)->watch(pid,epoch);
}
// Control-side only, serialized by PlaybackTiming.lock. Report final loss
// BEFORE retiring a terminated process view; no wait in any audio callback.
extern "C" __declspec(dllexport) unsigned __cdecl playback_timing_origin_health(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out)return 0;auto& s=*static_cast<PlaybackTiming*>(pointer);unsigned count=0;
    AcquireSRWLockExclusive(&s.originLock);
    for(auto it=s.origins.begin();it!=s.origins.end()&&count<capacity;){
        out[3*count]=it->data->pid;out[3*count+1]=it->data->epoch;
        out[3*count+2]=static_cast<unsigned long long>(it->data->losses);++count;
        if(it->process&&WaitForSingleObject(it->process,0)==WAIT_OBJECT_0){
            UnmapViewOfFile(it->data);CloseHandle(it->mapping);CloseHandle(it->process);it=s.origins.erase(it);
        }else ++it;
    }
    ReleaseSRWLockExclusive(&s.originLock);return count;
}
extern "C" __declspec(dllexport) void* __cdecl playback_timing_create(void* position,unsigned stream,unsigned channels){
    if(!position||!stream||!channels||channels>4096)return nullptr;
    try{return new PlaybackTiming(reinterpret_cast<NativeGetPosition>(position),stream,channels);}catch(...){return nullptr;}
}
extern "C" __declspec(dllexport) void __cdecl playback_timing_destroy(void* pointer){if(pointer)static_cast<PlaybackTiming*>(pointer)->release();}
extern "C" __declspec(dllexport) void __cdecl playback_timing_rate(void* pointer,double rate){if(pointer)static_cast<PlaybackTiming*>(pointer)->sampleRate.store(rate);}
// Under the source ChannelLock: reset note correlation, not audio or cumulative
// source health. Preserve admitted sequence to keep existing GUI readers safe.
extern "C" __declspec(dllexport) void __cdecl playback_timing_reset(void* pointer){
    if(!pointer)return;auto& s=*static_cast<PlaybackTiming*>(pointer);
    for(size_t i=0;i<static_cast<size_t>(s.channels)*128;++i){auto& q=s.ingress[i];q.head.store(q.tail.load());}
    s.cursor=s.admitted.load();s.omitted.store(0);
    for(auto& r:s.records)r.values[6].store(0,std::memory_order_release);
}
// Additive ABI: v1 observation exports remain compatible. Input health is
// cumulative for the attached stream and deliberately survives note Reset.
extern "C" __declspec(dllexport) void __cdecl playback_timing_input_fault(void* pointer,unsigned kind){
    if(!pointer)return;auto& s=*static_cast<PlaybackTiming*>(pointer);
    if(kind==1)s.missingOrigin.fetch_add(1);else if(kind==2)s.transportLoss.fetch_add(1);else s.invalidOrigin.fetch_add(1);
}
extern "C" __declspec(dllexport) void __cdecl playback_timing_receive_origin(void* pointer,unsigned channel,unsigned key,
    unsigned long long ingress,unsigned long long read,unsigned long long pid,unsigned long long epoch,unsigned long long sequence,unsigned quality){
    if(pointer)static_cast<PlaybackTiming*>(pointer)->receive(channel,key,ingress,read,pid,epoch,sequence,quality);
}
extern "C" __declspec(dllexport) unsigned long long __cdecl playback_timing_now(){LARGE_INTEGER t{};QueryPerformanceCounter(&t);return t.QuadPart;}
// Timestamp completed kernel delivery BEFORE ctypes reacquires Python's GIL.
// Exact original synchronous ReadFile semantics, error and byte count retained.
extern "C" __declspec(dllexport) int __cdecl playback_pipe_read(void* handle,void* buffer,unsigned bytes,unsigned* received,unsigned long long* stamp){
    const BOOL ok=ReadFile(handle,buffer,bytes,reinterpret_cast<DWORD*>(received),nullptr);
    const DWORD error=GetLastError();LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);
    if(stamp)*stamp=static_cast<unsigned long long>(tick.QuadPart);
    SetLastError(error);return ok;
}
static bool playbackReadExact(HANDLE handle,void* target,unsigned bytes){
    auto* out=static_cast<unsigned char*>(target);
    while(bytes){DWORD count=0;if(!ReadFile(handle,out,bytes,&count,nullptr)||!count)return false;out+=count;bytes-=count;}
    return true;
}
// One whole existing MID1/MID2 frame in native code: Python/GIL cannot insert
// an unmeasured delay between header and payload reads. No readahead, receive
// thread, driver ABI, queue policy or event coalescing changes.
extern "C" __declspec(dllexport) int __cdecl playback_pipe_frame(void* handle,void* buffer,unsigned capacity,unsigned* bytes,
        long long* sequence,unsigned long long* stamp){
    unsigned header[2]{};
    if(!buffer||!bytes||!sequence||!stamp||!playbackReadExact(handle,header,8))return 0;
    if((header[0]!=0x3144494Du&&header[0]!=0x3244494Du)||!header[1]||header[1]>capacity||header[1]>1024*1024)return -1;
    *sequence=0;
    if(header[0]==0x3244494Du&&!playbackReadExact(handle,sequence,8))return 0;
    if(!playbackReadExact(handle,buffer,header[1]))return 0;
    LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);*stamp=tick.QuadPart;*bytes=header[1];
    return header[0]==0x3244494Du?2:1;
}
// MID3 64-byte header: common8 + ingress/frequency/epoch/sequence/trace/loss6x8
// + processId/flags2x4. Read the whole frame natively before reacquiring GIL.
// origin[0..7] = ingress,frequency,epoch,sequence,trace,loss,pid,flags.
static int playbackReadOriginHeader(void* handle,unsigned capacity,unsigned* bytes,
        long long* sequence,unsigned long long* origin){
    unsigned header[2]{};
    if(!bytes||!sequence||!origin||!playbackReadExact(handle,header,8))return 0;
    if((header[0]!=0x3144494Du&&header[0]!=0x3244494Du&&header[0]!=0x3344494Du)||!header[1]||header[1]>capacity||header[1]>1024*1024)return -1;
    std::fill_n(origin,8,0ull);*sequence=0;
    if(header[0]==0x3344494Du){
        if(!playbackReadExact(handle,origin,48))return 0;
        unsigned tail[2]{};if(!playbackReadExact(handle,tail,8))return 0;
        origin[6]=tail[0];origin[7]=tail[1];*sequence=static_cast<long long>(origin[4]);
    }else if(header[0]==0x3244494Du&&!playbackReadExact(handle,sequence,8))return 0;
    *bytes=header[1];
    return header[0]==0x3344494Du?3:header[0]==0x3244494Du?2:1;
}
extern "C" __declspec(dllexport) int __cdecl playback_pipe_frame_origin(void* handle,void* buffer,unsigned capacity,unsigned* bytes,
        long long* sequence,unsigned long long* stamp,unsigned long long* origin){
    if(!buffer||!stamp)return 0;
    const int result=playbackReadOriginHeader(handle,capacity,bytes,sequence,origin);
    if(result<=0||!playbackReadExact(handle,buffer,*bytes))return result<=0?result:0;
    LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);*stamp=tick.QuadPart;
    return result;
}
// MIDI ingress, NOT an audio callback. A dedicated native thread owns reads;
// Python processing/GIL cannot stall that reader until bounded backpressure.
// Wire MID1/2/3 and original QPC remain unchanged. Never merge MIDI payloads.
// Windows SRW/condition-variable waits are predicate loops (spurious wakeups).
struct MidiPipeQueue;
// One execution owner, many independent ingress pipes. Frames receive a
// publication ticket, never a rewritten driver timestamp/sequence. This
// lifetime is independent of the worker so a completing reader can safely
// return a borrowed notification after detach/join.
struct MidiIngressSession {
    struct Client {MidiPipeQueue* queue=nullptr;unsigned long long id=0;std::atomic<unsigned long long> metadata{0};unsigned port=0;};
    SRWLOCK lock=SRWLOCK_INIT;
    HANDLE ready=CreateEventW(nullptr,FALSE,FALSE,nullptr);
    std::atomic<unsigned> references{1};
    // Eight original pipe clients plus one session-owned direct producer.
    // The extra slot is not another MIDI port or a reduced external limit.
    std::array<Client,9> clients{};
    unsigned long long readCount=0,popped=0,clientSerial=0;
    bool accepting=true;
    void retain(){references.fetch_add(1,std::memory_order_relaxed);}
    void release(){if(references.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
    ~MidiIngressSession(){if(ready)CloseHandle(ready);}
};
struct MidiPipeQueue {
    static constexpr unsigned byteCapacity=4*1024*1024, frameCapacity=4096;
    struct Frame {
        unsigned kind=0, size=0;
        unsigned directPort=0;
        long long trace=0;
        unsigned long long read=0, origin[8]{},publication=0;
    };
    HANDLE pipe, thread=nullptr, readerReady=nullptr;
    bool listener=false;
    DWORD expectedSession=0;
    unsigned connected=0,sessionError=0,readerThread=0,listenerReady=0,mmcssReverted=0,mmcssRevertError=0;
    SRWLOCK lock=SRWLOCK_INIT;
    CONDITION_VARIABLE changed=CONDITION_VARIABLE_INIT;
    std::vector<unsigned char> storage;
    std::vector<Frame> frames;
    unsigned head=0, count=0, byteHead=0, byteTail=0, used=0;
    bool stopping=false, ended=false;
    void* consumer=nullptr;
    MidiIngressSession* session=nullptr;
    unsigned activePops=0;
    int terminal=0;
    unsigned long long readCount=0, popped=0, totalBytes=0, peakFrames=0,
        peakBytes=0, pressureWaits=0, maxAge=0, batches=0, mmcss=0, schedulingError=0;
    explicit MidiPipeQueue(HANDLE input):pipe(input),storage(byteCapacity),frames(frameCapacity) {}
    ~MidiPipeQueue(){if(readerReady)CloseHandle(readerReady);}
    MidiIngressSession* borrowSession(){
        AcquireSRWLockExclusive(&lock);auto* value=session;if(value)value->retain();ReleaseSRWLockExclusive(&lock);return value;
    }
    void notifySession(){auto* value=borrowSession();if(value){SetEvent(value->ready);value->release();}}
    bool commit(Frame& frame,bool received){
        for(;;){
            auto* value=borrowSession();if(value)AcquireSRWLockExclusive(&value->lock);
            AcquireSRWLockExclusive(&lock);
            if(session!=value){ReleaseSRWLockExclusive(&lock);if(value){ReleaseSRWLockExclusive(&value->lock);value->release();}continue;}
            if(!received||stopping||(value&&(!value->accepting||value->readCount==UINT64_MAX))){
                used-=frame.size;ended=true;if(value&&value->readCount==UINT64_MAX)terminal=-5;
                WakeAllConditionVariable(&changed);ReleaseSRWLockExclusive(&lock);
                if(value){SetEvent(value->ready);ReleaseSRWLockExclusive(&value->lock);value->release();}return false;
            }
            if(value)frame.publication=++value->readCount;
            ++readCount;totalBytes+=frame.size;frames[(head+count)%frameCapacity]=frame;++count;
            peakFrames=std::max(peakFrames,static_cast<unsigned long long>(count));WakeAllConditionVariable(&changed);
            ReleaseSRWLockExclusive(&lock);
            if(value){SetEvent(value->ready);ReleaseSRWLockExclusive(&value->lock);value->release();}return true;
        }
    }
    static unsigned __stdcall run(void* pointer) {
        auto& q=*static_cast<MidiPipeQueue*>(pointer);
        // Thread-owned registration; never alters system registry or GUI priority.
        DWORD index=0;
        HANDLE task=AvSetMmThreadCharacteristicsW(L"Pro Audio",&index);
        const DWORD error=task?0:GetLastError();
        AcquireSRWLockExclusive(&q.lock);
        q.mmcss=task?1:0;q.schedulingError=error;
        q.readerThread=GetCurrentThreadId();
        ReleaseSRWLockExclusive(&q.lock);
        if(q.readerReady)SetEvent(q.readerReady);
        if(q.listener){
            const BOOL accepted=ConnectNamedPipe(q.pipe,nullptr);
            const DWORD connectError=accepted?0:GetLastError();
            ULONG session=0;
            // Fail closed before reading ANY header/payload, including a client
            // already connected between CreateNamedPipe and this native wait.
            const BOOL sessionRead=(accepted||connectError==ERROR_PIPE_CONNECTED)?GetNamedPipeClientSessionId(q.pipe,&session):FALSE;
            const DWORD sessionError=sessionRead?0:GetLastError();
            AcquireSRWLockExclusive(&q.lock);
            if(!sessionRead||session!=q.expectedSession||q.stopping){
                q.sessionError=sessionRead?(session!=q.expectedSession?ERROR_ACCESS_DENIED:0):sessionError;
                q.terminal=q.stopping?0:-6;q.ended=true;
            }else q.connected=1;
            const bool mayRead=q.connected!=0;
            ReleaseSRWLockExclusive(&q.lock);
            if(!mayRead){
                const BOOL reverted=task?AvRevertMmThreadCharacteristics(task):FALSE;
                const DWORD revertError=task&&!reverted?GetLastError():0;
                AcquireSRWLockExclusive(&q.lock);q.mmcssReverted=reverted?1:0;q.mmcssRevertError=revertError;
                WakeAllConditionVariable(&q.changed);ReleaseSRWLockExclusive(&q.lock);
                q.notifySession();return 0;
            }
        }
        for (;;) {
            AcquireSRWLockExclusive(&q.lock);
            const bool stop=q.stopping;
            ReleaseSRWLockExclusive(&q.lock);
            if(stop)break;
            Frame f;
            const int result=playbackReadOriginHeader(q.pipe,1024*1024,&f.size,&f.trace,f.origin);
            AcquireSRWLockExclusive(&q.lock);
            if(result<=0){q.terminal=result;q.ended=true;WakeAllConditionVariable(&q.changed);
                ReleaseSRWLockExclusive(&q.lock);q.notifySession();break;}
            f.kind=static_cast<unsigned>(result);
            // Reserve storage before payload read. Uncommitted bytes cannot be
            // popped/reused; no scratch->queue copy or per-event allocation.
            while(!q.stopping&&(q.count==frameCapacity||f.size>byteCapacity-q.used)){
                ++q.pressureWaits;
                SleepConditionVariableSRW(&q.changed,&q.lock,INFINITE,0);
            }
            if(q.stopping){ReleaseSRWLockExclusive(&q.lock);break;}
            const unsigned position=q.byteTail;
            const unsigned first=std::min(f.size,byteCapacity-position);
            q.byteTail=(q.byteTail+f.size)%byteCapacity;q.used+=f.size;
            q.peakBytes=std::max(q.peakBytes,static_cast<unsigned long long>(q.used));
            ReleaseSRWLockExclusive(&q.lock);
            const bool received=playbackReadExact(q.pipe,q.storage.data()+position,first)&&
                (first==f.size||playbackReadExact(q.pipe,q.storage.data(),f.size-first));
            LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);f.read=tick.QuadPart;
            if(!q.commit(f,received))break;
        }
        const BOOL reverted=task?AvRevertMmThreadCharacteristics(task):FALSE;
        const DWORD revertError=task&&!reverted?GetLastError():0;
        AcquireSRWLockExclusive(&q.lock);q.ended=true;q.mmcssReverted=reverted?1:0;q.mmcssRevertError=revertError;
        WakeAllConditionVariable(&q.changed);ReleaseSRWLockExclusive(&q.lock);
        q.notifySession();
        return 0;
    }
    void requestStop(){
        AcquireSRWLockExclusive(&lock);stopping=true;WakeAllConditionVariable(&changed);
        ReleaseSRWLockExclusive(&lock);
        if(thread)CancelSynchronousIo(thread);
        notifySession();
    }
    bool join(){
        requestStop();
        // Cancellation is an attempt, not a completion barrier. Recheck the
        // between-read race and retain ownership on timeout, never free a borrower.
        for(unsigned i=0;i<200;++i){
            const DWORD result=WaitForSingleObject(thread,10);
            if(result==WAIT_OBJECT_0)return true;
            if(result==WAIT_FAILED)return false;
            CancelSynchronousIo(thread);
        }
        return false;
    }
};
extern "C" __declspec(dllexport) void* __cdecl midi_pipe_queue_create(void* handle){
    if(!handle||handle==INVALID_HANDLE_VALUE)return nullptr;
    try{
        auto q=std::make_unique<MidiPipeQueue>(handle);
        q->thread=reinterpret_cast<HANDLE>(_beginthreadex(nullptr,0,&MidiPipeQueue::run,q.get(),0,nullptr));
        if(!q->thread)return nullptr;
        return q.release();
    }catch(const std::bad_alloc&){return nullptr;}
}
// Additive listener ABI. Handle stays Python-owned; release must confirm native
// Connect/Read owner termination before DisconnectNamedPipe/CloseHandle.
extern "C" __declspec(dllexport) void* __cdecl midi_pipe_queue_create_listener(void* handle,unsigned expectedSession){
    if(!handle||handle==INVALID_HANDLE_VALUE)return nullptr;
    try{
        auto q=std::make_unique<MidiPipeQueue>(handle);q->listener=true;q->expectedSession=expectedSession;
        q->readerReady=CreateEventW(nullptr,TRUE,FALSE,nullptr);if(!q->readerReady)return nullptr;
        q->thread=reinterpret_cast<HANDLE>(_beginthreadex(nullptr,0,&MidiPipeQueue::run,q.get(),0,nullptr));
        if(!q->thread)return nullptr;
        // Allocation, MMCSS registration and owner identity are published before
        // the caller may mark the prepared server pool ready. No GIL callback.
        const DWORD ready=WaitForSingleObject(q->readerReady,2000);
        const DWORD readyError=ready==WAIT_FAILED?GetLastError():0;
        AcquireSRWLockExclusive(&q->lock);q->listenerReady=ready==WAIT_OBJECT_0?1:0;
        if(ready!=WAIT_OBJECT_0)q->sessionError=ready==WAIT_TIMEOUT?ERROR_TIMEOUT:readyError;
        ReleaseSRWLockExclusive(&q->lock);
        // A failed ready wait still returns the owned root for stop/join retry;
        // never destroy beneath a started thread or report the pool ready.
        return q.release();
    }catch(const std::bad_alloc&){return nullptr;}
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_pipe_queue_listener_snapshot(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out||capacity<7)return 0;
    auto& q=*static_cast<MidiPipeQueue*>(pointer);AcquireSRWLockExclusive(&q.lock);
    const unsigned long long values[]={q.listener?1ull:0ull,q.connected,q.sessionError,q.readerThread,q.listenerReady,q.mmcssReverted,q.mmcssRevertError};
    std::copy_n(values,7,out);ReleaseSRWLockExclusive(&q.lock);return 7;
}
// Each descriptor is13 uint64: kind,size,offset,trace,read,origin[8]. Up to
// capacity already-arrived frames; no batching timer or MIDI reorder. Return
// 0=EOF/stop, -1=bad frame (after draining valid predecessors), -2=bad buffers.
static int midiQueuePop(void* pointer,void* output,unsigned bytes,
        unsigned long long* descriptors,unsigned capacity,void* consumer=nullptr,const std::atomic<bool>* cancelled=nullptr,
        const std::atomic<bool>* controls=nullptr,const std::atomic<unsigned long long>* fence=nullptr,
        DWORD timeout=INFINITE,const std::atomic<bool>* clockChanged=nullptr){
    if(!pointer||!output||!descriptors||!capacity)return -2;
    auto& q=*static_cast<MidiPipeQueue*>(pointer);
    AcquireSRWLockExclusive(&q.lock);
    if(q.consumer!=consumer){ReleaseSRWLockExclusive(&q.lock);return -3;}
    ++q.activePops;
    auto controlReady=[&](){return controls&&fence&&controls->load()&&q.popped>=fence->load();};
    while(!q.count&&!q.stopping&&!q.ended&&!(cancelled&&cancelled->load())&&!controlReady()){
        if(clockChanged&&clockChanged->load()){--q.activePops;ReleaseSRWLockExclusive(&q.lock);return -9;}
        if(!SleepConditionVariableSRW(&q.changed,&q.lock,timeout,0)){
            const DWORD error=GetLastError();--q.activePops;ReleaseSRWLockExclusive(&q.lock);return error==ERROR_TIMEOUT?-9:-5;
        }
    }
    if(controlReady()&&!(cancelled&&cancelled->load())){--q.activePops;ReleaseSRWLockExclusive(&q.lock);return -4;}
    if(q.stopping||!q.count||(cancelled&&cancelled->load())){const int result=q.stopping||(cancelled&&cancelled->load())?0:q.terminal;
        --q.activePops;ReleaseSRWLockExclusive(&q.lock);return result;}
    unsigned copied=0, n=0;LARGE_INTEGER now{};QueryPerformanceCounter(&now);
    while(q.count&&n<capacity){
        const auto& f=q.frames[q.head];
        if(f.size>bytes-copied)break;
        auto* dst=static_cast<unsigned char*>(output)+copied;
        const unsigned first=std::min(f.size,MidiPipeQueue::byteCapacity-q.byteHead);
        std::memcpy(dst,q.storage.data()+q.byteHead,first);
        if(first<f.size)std::memcpy(dst+first,q.storage.data(),f.size-first);
        auto* row=descriptors+n*13;
        row[0]=f.kind;row[1]=f.size;row[2]=copied;row[3]=static_cast<unsigned long long>(f.trace);row[4]=f.read;
        std::copy_n(f.origin,8,row+5);
        q.maxAge=std::max(q.maxAge,static_cast<unsigned long long>(now.QuadPart)-f.read);
        copied+=f.size;q.byteHead=(q.byteHead+f.size)%MidiPipeQueue::byteCapacity;
        q.used-=f.size;q.head=(q.head+1)%MidiPipeQueue::frameCapacity;--q.count;++n;++q.popped;
    }
    if(n)++q.batches;
    --q.activePops;WakeAllConditionVariable(&q.changed);ReleaseSRWLockExclusive(&q.lock);
    return n?static_cast<int>(n):-2;
}
extern "C" __declspec(dllexport) int __cdecl midi_pipe_queue_pop(void* pointer,void* output,unsigned bytes,
        unsigned long long* descriptors,unsigned capacity){return midiQueuePop(pointer,output,bytes,descriptors,capacity);}
extern "C" __declspec(dllexport) void __cdecl midi_pipe_queue_stop(void* pointer){
    if(pointer)static_cast<MidiPipeQueue*>(pointer)->requestStop();
}
extern "C" __declspec(dllexport) int __cdecl midi_pipe_queue_release(void* pointer){
    if(!pointer)return 1;auto* q=static_cast<MidiPipeQueue*>(pointer);
    AcquireSRWLockExclusive(&q->lock);const bool busy=q->consumer||q->activePops;
    if(!busy)q->stopping=true; // Atomic with new consumer admission, before join releases this lock.
    ReleaseSRWLockExclusive(&q->lock);
    if(busy)return 0; // Never free a queue beneath a native/Python pop borrower.
    if(!q->join())return 0;
    CloseHandle(q->thread);delete q;return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_pipe_queue_snapshot(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out||capacity<14)return 0;
    auto& q=*static_cast<MidiPipeQueue*>(pointer);AcquireSRWLockExclusive(&q.lock);
    const unsigned long long values[]={q.readCount,q.popped,q.totalBytes,q.count,q.used,q.peakFrames,
        q.peakBytes,q.pressureWaits,q.maxAge,q.batches,q.mmcss,q.schedulingError,
        static_cast<unsigned long long>(q.ended),static_cast<unsigned long long>(q.stopping)};
    std::copy_n(values,14,out);ReleaseSRWLockExclusive(&q.lock);return 14;
}
extern "C" __declspec(dllexport) void __cdecl playback_timing_receive(void* pointer,unsigned channel,unsigned key,unsigned long long tick){
    if(pointer)static_cast<PlaybackTiming*>(pointer)->receive(channel,key,tick);
}
extern "C" __declspec(dllexport) void __cdecl playback_timing_cancel(void* pointer,unsigned long long tick){
    if(!pointer)return;auto& s=*static_cast<PlaybackTiming*>(pointer);
    for(size_t i=0;i<static_cast<size_t>(s.channels)*128;++i)for(auto& origin:s.ingress[i].origins)
        if(origin[0].load()==tick)origin[0].store(0,std::memory_order_release);
}

// CC semantic executor. Raw decoding remains BASS's job. Prepared masks
// identify Learn/function/state paths still owned by the legacy interpreter;
// qualification of the ENTIRE frame happens before any audio or metadata.
struct MidiNoteAction {unsigned packed,channel;unsigned long long count,qpc;};
static_assert(sizeof(MidiNoteAction)==24,"native note action ABI1");
// Standalone presentation reducer ABI1. It owns no BASS handle and is NOT
// installed on the execution worker yet. Source contracts: SoundFontActivity
// (soundfont_runtime.py) and ChannelMidiActivity / NativeNoteKernel.replay
// (midi_input_runtime.py). Unknown-source On changes channel activity only;
// every Off still consumes the first known-source onset on that key.
// This is an input/control-worker component, never an audio callback. Mutable
// vectors may allocate here; allocation failure precedes all logical changes.
// SRW protects this owner alone: no BASS/worker/Python lock is acquired beneath
// it. https://learn.microsoft.com/en-us/windows/win32/sync/slim-reader-writer--srw--locks
struct NativeMidiActivity {
    using U64=unsigned long long;
    static constexpr U64 absent=~0ull;
    struct Run {unsigned address;U64 count,ordinal;};
    struct Key {U64 activity=0,overlap=0;std::vector<Run> runs;size_t head=0;};
    struct Channel {std::array<Key,256> keys;U64 activity=0,overlap=0,qpc=0,phase=0;double initial=0;unsigned velocity=0;};
    struct Preset {unsigned address;U64 active=0,visual=absent,phase=0;double initial=0;unsigned release=~0u;bool played=false;};
    SRWLOCK lock=SRWLOCK_INIT;std::atomic<unsigned> references{1};
    std::vector<Channel> channels;std::vector<Preset> presets;
    std::vector<unsigned> available;bool availableKnown=false;
    U64 revision=0,ordinal=0,frequency=0;
    // Optional chord observation,not an audio/completion queue. Fixed storage
    // never allocates or applies reader backpressure. Old history may be overwritten.
    struct ChordRow {U64 serial=0,packed=0,channel=0,qpc=0;};
    std::array<ChordRow,8192> chordRows{};U64 chordSerial=0,chordFloor=0;
    void chordAction(unsigned packed,unsigned channel,U64 qpc){
        if(chordSerial==absent)return;
        ++chordSerial;chordRows[(chordSerial-1)%chordRows.size()]={chordSerial,packed,channel,qpc};
    }
    void chordBaseline(){if(chordSerial!=absent)++chordSerial;chordFloor=chordSerial;}
    void* attachedOwner=nullptr;DWORD reservationThread=0;bool shadowDirty=false;
    // Four immutable PRESENTATION cells are a GUI resource policy, not an
    // event/FIFO limit. A reader may pin a cell indefinitely; the producer
    // skips a presentation update instead of waiting or dropping MIDI state.
    struct View {std::atomic<U64> state{0};std::vector<std::array<U64,6>> channels;
        std::vector<std::array<U64,7>> presets;U64 revision=0,frequency=0,qpc=0,validity=0;};
    std::array<View,4> views;std::atomic<U64> currentView{0},visibleRevision{0};
    std::atomic<U64> validity{3},publishedRevision{absent},publishedValidity{0};
    std::atomic_flag publishing=ATOMIC_FLAG_INIT;unsigned viewGeneration=0;
    std::array<std::atomic<U64>,4> viewMetrics{}; // published/BUSY/allocation failure/generation exhaustion
    explicit NativeMidiActivity(unsigned count):channels(count){
        LARGE_INTEGER value{};QueryPerformanceFrequency(&value);frequency=static_cast<U64>(value.QuadPart);
        for(auto& metric:viewMetrics)metric.store(0,std::memory_order_relaxed);
    }
    void retain(){references.fetch_add(1,std::memory_order_relaxed);}
    void release(){if(references.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
    void commit(){++revision;visibleRevision.store(revision,std::memory_order_release);}
    void qualify(bool known){
        const U64 old=validity.load(std::memory_order_relaxed),epoch=old>>1;
        // Exhaustion stays permanently unknown, never wraps into a fresh ABA.
        validity.store(epoch==(absent>>1)?absent-1:((epoch+1)<<1)|(known?1ull:0ull),std::memory_order_release);
    }
    static std::array<U64,7> presetValues(const Preset& row){U64 bits=0;std::memcpy(&bits,&row.initial,sizeof(bits));
        return {row.address,row.active,row.played?1ull:0ull,row.visual,row.phase,bits,
            row.release==~0u?absent:static_cast<U64>(row.release)};}
    static std::array<U64,6> channelValues(const Channel& row){U64 bits=0;std::memcpy(&bits,&row.initial,sizeof(bits));
        return {row.activity,row.velocity,row.qpc,row.overlap,row.phase,bits};}
    auto find(unsigned address){return std::lower_bound(presets.begin(),presets.end(),address,
        [](const Preset& row,unsigned key){return row.address<key;});}
    Preset& ensure(unsigned address){auto at=find(address);if(at==presets.end()||at->address!=address)
        at=presets.insert(at,Preset{address});return *at;}
    template<class T> static void reserveMore(std::vector<T>& rows,size_t extra){
        if(extra>rows.max_size()-rows.size())throw std::bad_alloc();
        const size_t needed=rows.size()+extra;if(needed<=rows.capacity())return;
        const size_t growth=rows.capacity()<=rows.max_size()/2?std::max<size_t>(8,rows.capacity()*2):rows.max_size();
        rows.reserve(std::max(needed,growth));
    }
    double envelope(U64 active,U64 phase,double initial,U64 qpc)const{
        // Match the original float monotonic-seconds subtraction, including
        // non-monotone observation clocks: elapsed is clamped, not the QPC.
        const double elapsed=std::max(0.0,static_cast<double>(qpc)/static_cast<double>(frequency)-
            static_cast<double>(phase)/static_cast<double>(frequency));
        if(active){if(initial<=60)return initial;return std::max(58.0,initial-150.0*elapsed);}
        return std::max(0.0,initial-500.0*elapsed);
    }
    double strength(const Preset& row,U64 qpc)const{return row.visual==absent?0:envelope(row.visual,row.phase,row.initial,qpc);}
    bool playedAddress(unsigned bank,unsigned program,unsigned& result)const{
        auto contains=[&](unsigned address){return std::binary_search(available.begin(),available.end(),address);};
        result=bank|(program<<16);if(!availableKnown)return true;
        if(bank>=128){result=128u|(program<<16);if(contains(result))return true;result=128;return contains(result);}
        if(contains(result))return true;result=program<<16;return contains(result);
    }
    void eraseVisual(Preset& row){row.visual=absent;row.phase=0;row.initial=0;row.release=~0u;}
    void clearActive(){for(auto& state:channels){for(auto& key:state.keys){key.activity=0;key.overlap=0;key.runs.clear();key.head=0;}
        state.activity=0;state.overlap=0;state.velocity=0;state.qpc=0;state.phase=0;state.initial=0;}
        for(auto& row:presets){row.active=0;eraseVisual(row);}chordAction(0x78b0u,~0u,0);commit();}
    void clearChannel(unsigned channel,U64 qpc,bool clearOverlap){
        auto& state=channels[channel];
        for(auto& key:state.keys){
            for(size_t i=key.head;i<key.runs.size();++i){const auto& run=key.runs[i];auto& row=*find(run.address);
                row.active-=run.count;
                if(row.visual!=absent){const U64 remaining=row.visual>run.count?row.visual-run.count:0;
                    if(remaining){const double initial=strength(row,qpc);row.visual=remaining;row.phase=qpc;row.initial=initial;}
                    else eraseVisual(row);}
            }
            key.runs.clear();key.head=0;key.activity=0;if(clearOverlap)key.overlap=0;
        }
        for(auto& row:presets)if(row.release==channel)eraseVisual(row);
        state.activity=0;state.velocity=0;state.qpc=0;state.phase=0;state.initial=0;if(clearOverlap)state.overlap=0;
        chordAction(0x78b0u,channel,qpc);
    }
    bool action(unsigned packed,unsigned channel,U64 overlap,U64 qpc,bool known,unsigned bank,unsigned program){
        if(channel>=channels.size()||packed>0xffffffu||bank>65535u||program>65535u||revision==absent)return false;
        const unsigned status=packed&240u,key=(packed>>8)&255u,velocity=(packed>>16)&255u;
        if(status==0xb0u){
            if((key!=120&&key!=121&&key!=123&&key!=124&&key!=125&&key!=126&&key!=127)||velocity||overlap)return false;
            clearChannel(channel,qpc,key!=121);commit();return true;
        }
        if(status!=0x80u&&status!=0x90u)return false;
        auto& state=channels[channel];auto& note=state.keys[key];
        const U64 otherOverlap=state.overlap-note.overlap;
        if(overlap>absent-otherOverlap)return false;
        const bool on=status==0x90u&&velocity!=0;unsigned played=0;
        const unsigned address=bank|(program<<16);
        const bool markPlayed=on&&known&&velocity>=5&&playedAddress(bank,program,played);
        if(on){
            if(note.activity==absent||state.activity==absent||ordinal==absent)return false;
            if(known){auto at=find(address);if(at!=presets.end()&&at->address==address&&
                    (at->active==absent||at->visual==absent-1))return false;
                // Reserve all possibly new identities and the FIFO node before
                // Channel/Overlap/Played/Visual/FIFO state is touched.
                try{reserveMore(presets,2);
                    if(note.runs.empty()||note.runs.back().address!=address||note.runs.back().ordinal+note.runs.back().count!=ordinal+1)
                        reserveMore(note.runs,1);
                }catch(...){return false;}
            }
            state.initial=velocity>=5?100.0:envelope(state.activity,state.phase,state.initial,qpc);state.phase=qpc;
            ++ordinal;++note.activity;++state.activity;state.velocity=velocity;state.qpc=qpc;
            if(known){ensure(address);if(markPlayed)ensure(played).played=true;
                auto& row=*find(address);++row.active;
                const double initial=velocity>=5?100.0:strength(row,qpc);
                row.visual=row.visual==absent?1:row.visual+1;row.phase=qpc;row.initial=initial;row.release=~0u;
                if(!note.runs.empty()&&note.runs.back().address==address&&note.runs.back().ordinal+note.runs.back().count==ordinal)
                    ++note.runs.back().count;
                else note.runs.push_back(Run{address,1,ordinal});
            }
        }else{
            if(note.activity){state.initial=envelope(state.activity,state.phase,state.initial,qpc);state.phase=qpc;
                --note.activity;--state.activity;}
            if(note.head<note.runs.size()){
                auto& run=note.runs[note.head];auto& row=*find(run.address);
                --row.active;const double initial=strength(row,qpc);
                const U64 remaining=row.visual==absent?0:(row.visual?row.visual-1:0);
                row.visual=remaining;row.phase=qpc;row.initial=initial;if(!remaining)row.release=channel;
                --run.count;++run.ordinal;if(!run.count)++note.head;
                if(note.head==note.runs.size()){note.runs.clear();note.head=0;}
                else if(note.head>1024&&note.head>note.runs.size()/2){
                    note.runs.erase(note.runs.begin(),note.runs.begin()+note.head);note.head=0;
                }
            }
        }
        note.overlap=overlap;state.overlap=otherOverlap+overlap;chordAction(packed,channel,qpc);commit();return true;
    }
    U64 fifoSize()const{U64 result=0;for(const auto& channel:channels)for(const auto& key:channel.keys)
        result+=static_cast<U64>(key.runs.size()-key.head);return result;}
};
// Caller retains this owner through every concurrent operation. Retain/release
// is ownership, not an admission gate: never call retain on a freed pointer.
extern "C" __declspec(dllexport) unsigned __cdecl native_activity_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl native_activity_create(unsigned channels){
    if(!channels||channels>128)return nullptr;try{return new NativeMidiActivity(channels);}catch(...){return nullptr;}
}
extern "C" __declspec(dllexport) void __cdecl native_activity_retain(void* pointer){if(pointer)static_cast<NativeMidiActivity*>(pointer)->retain();}
extern "C" __declspec(dllexport) void __cdecl native_activity_release(void* pointer){if(pointer)static_cast<NativeMidiActivity*>(pointer)->release();}
extern "C" __declspec(dllexport) int __cdecl native_activity_action(void* pointer,unsigned packed,unsigned channel,
        unsigned long long overlap,unsigned long long qpc,unsigned known,unsigned bank,unsigned program){
    if(!pointer||known>1||!qpc)return 0;auto& s=*static_cast<NativeMidiActivity*>(pointer);
    AcquireSRWLockExclusive(&s.lock);const bool ok=!s.attachedOwner&&s.action(packed,channel,overlap,qpc,known!=0,bank,program);
    ReleaseSRWLockExclusive(&s.lock);return ok?1:0;
}
// kind1=active clear (one channel or all), kind2=history only (all),
// kind3=all active/history (all). UINT_MAX means all channels.
extern "C" __declspec(dllexport) int __cdecl native_activity_clear(void* pointer,unsigned channel,unsigned kind,unsigned long long qpc){
    if(!pointer||kind<1||kind>3||!qpc)return 0;auto& s=*static_cast<NativeMidiActivity*>(pointer);
    if((channel!=~0u&&channel>=s.channels.size())||(kind!=1&&channel!=~0u))return 0;
    AcquireSRWLockExclusive(&s.lock);if(s.attachedOwner||s.revision==NativeMidiActivity::absent){ReleaseSRWLockExclusive(&s.lock);return 0;}
    if(kind==1&&channel!=~0u)s.clearChannel(channel,qpc,true);
    else{
        if(kind!=2){for(auto& state:s.channels){for(auto& key:state.keys){key.activity=0;key.overlap=0;key.runs.clear();key.head=0;}
            state.activity=0;state.overlap=0;state.velocity=0;state.qpc=0;state.phase=0;state.initial=0;}
            for(auto& row:s.presets){row.active=0;s.eraseVisual(row);}}
        if(kind!=2)s.chordAction(0x78b0u,~0u,qpc);
        if(kind!=1)for(auto& row:s.presets)row.played=false;
    }
    s.commit();ReleaseSRWLockExclusive(&s.lock);return 1;
}
// Available identities use low16 bank/high16 program, not MIDI CC0/32.
// Stage/sort before locking; failed allocation leaves the old set intact.
extern "C" __declspec(dllexport) int __cdecl native_activity_available(void* pointer,const unsigned* addresses,unsigned count,unsigned known){
    if(!pointer||known>1||(count&&!addresses)||(!known&&count))return 0;
    std::vector<unsigned> staged;try{if(count)staged.assign(addresses,addresses+count);
        std::sort(staged.begin(),staged.end());staged.erase(std::unique(staged.begin(),staged.end()),staged.end());}catch(...){return 0;}
    auto& s=*static_cast<NativeMidiActivity*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.attachedOwner||s.revision==NativeMidiActivity::absent){ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.available.swap(staged);s.availableKnown=known!=0;s.commit();ReleaseSRWLockExclusive(&s.lock);return 1;
}
// Source visual_state removes expired release entries. Keep that mutation
// explicit: snapshot queries never advance/prune presentation history.
extern "C" __declspec(dllexport) int __cdecl native_activity_prune(void* pointer,unsigned long long qpc){
    if(!pointer||!qpc)return 0;auto& s=*static_cast<NativeMidiActivity*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.attachedOwner||s.revision==NativeMidiActivity::absent){ReleaseSRWLockExclusive(&s.lock);return 0;}
    bool changed=false;for(auto& row:s.presets)if(row.visual==0&&s.strength(row,qpc)==0){s.eraseVisual(row);changed=true;}
    if(changed)s.commit();ReleaseSRWLockExclusive(&s.lock);return 1;
}
// Snapshot ABI1: six U64 header fields revision/channels/presetRows/fifoRows/
// availableKnown/QPCfrequency. Copy capacities are ROWS, never byte counts.
// All capacities/revision are checked BEFORE any caller output is written.
extern "C" __declspec(dllexport) unsigned __cdecl native_activity_snapshot_sizes(void* pointer,unsigned long long* header,unsigned capacity){
    if(!pointer||!header||capacity<6)return 0;auto& s=*static_cast<NativeMidiActivity*>(pointer);AcquireSRWLockShared(&s.lock);
    const unsigned long long values[]{s.revision,static_cast<unsigned long long>(s.channels.size()),
        static_cast<unsigned long long>(s.presets.size()),s.fifoSize(),s.availableKnown?1ull:0ull,s.frequency};
    std::copy_n(values,6,header);ReleaseSRWLockShared(&s.lock);return 6;
}
extern "C" __declspec(dllexport) int __cdecl native_activity_snapshot_copy(void* pointer,unsigned long long revision,
        unsigned long long* channels,unsigned channelCapacity,unsigned long long* presets,unsigned presetCapacity,
        unsigned long long* fifo,unsigned fifoCapacity){
    if(!pointer)return 0;auto& s=*static_cast<NativeMidiActivity*>(pointer);AcquireSRWLockShared(&s.lock);
    if(s.revision!=revision){ReleaseSRWLockShared(&s.lock);return -1;}
    const auto fifoCount=s.fifoSize();
    if(!channels||channelCapacity<s.channels.size()||presetCapacity<s.presets.size()||fifoCapacity<fifoCount||
            (!presets&&!s.presets.empty())||(!fifo&&fifoCount)){ReleaseSRWLockShared(&s.lock);return 0;}
    for(size_t i=0;i<s.channels.size();++i){const auto& row=s.channels[i];const unsigned long long values[]{row.activity,row.velocity,row.qpc,row.overlap};
        std::copy_n(values,4,channels+i*4);}
    for(size_t i=0;i<s.presets.size();++i){const auto values=NativeMidiActivity::presetValues(s.presets[i]);
        std::copy_n(values.data(),7,presets+i*7);}
    size_t index=0;for(size_t channel=0;channel<s.channels.size();++channel)for(unsigned key=0;key<256;++key){const auto& note=s.channels[channel].keys[key];
        for(size_t i=note.head;i<note.runs.size();++i){const auto& row=note.runs[i];const unsigned long long values[]{
            static_cast<unsigned long long>(channel),key,row.address,row.count,row.ordinal};std::copy_n(values,5,fifo+index++*5);}}
    ReleaseSRWLockShared(&s.lock);return 1;
}
// Fixed channels*256 rows [channel,key,channelActivityCount,rawOverlapCount].
// 256 preserves the original malformed-byte Python keys rather than silently
// clipping them to the MIDI standard's 0..127 native audio-key domain.
extern "C" __declspec(dllexport) int __cdecl native_activity_counts_copy(void* pointer,unsigned long long revision,
        unsigned long long* rows,unsigned capacity){
    if(!pointer||!rows)return 0;auto& s=*static_cast<NativeMidiActivity*>(pointer);AcquireSRWLockShared(&s.lock);
    if(s.revision!=revision){ReleaseSRWLockShared(&s.lock);return -1;}
    if(capacity<s.channels.size()*256){ReleaseSRWLockShared(&s.lock);return 0;}
    for(size_t channel=0;channel<s.channels.size();++channel)for(unsigned key=0;key<256;++key){const auto& row=s.channels[channel].keys[key];
        const unsigned long long values[]{static_cast<unsigned long long>(channel),key,row.activity,row.overlap};
        std::copy_n(values,4,rows+(channel*256+key)*4);}
    ReleaseSRWLockShared(&s.lock);return 1;
}
// Full transfer ABI1 is CONTROL-side only, never a GUI polling shortcut.
// Header extends debug snapshot's six fields with availableRows/ordinal/ABI.
// Copy includes every field required by a later ordered ownership handoff.
extern "C" __declspec(dllexport) unsigned __cdecl native_activity_transfer_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl native_activity_transfer_header(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out||capacity<9)return 0;auto& s=*static_cast<NativeMidiActivity*>(pointer);AcquireSRWLockShared(&s.lock);
    const unsigned long long values[]{s.revision,static_cast<unsigned long long>(s.channels.size()),
        static_cast<unsigned long long>(s.presets.size()),s.fifoSize(),s.availableKnown?1ull:0ull,s.frequency,
        static_cast<unsigned long long>(s.available.size()),s.ordinal,1};
    std::copy_n(values,9,out);ReleaseSRWLockShared(&s.lock);return 9;
}
extern "C" __declspec(dllexport) unsigned __cdecl native_activity_channel_visual_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl native_activity_transfer_header_v2(void* pointer,unsigned long long* out,unsigned capacity){
    const auto count=native_activity_transfer_header(pointer,out,capacity);if(count==9)out[8]=2;return count;
}
static int activityTransferCopy(bool extended,void* pointer,unsigned long long revision,
        unsigned long long* channels,unsigned channelCapacity,unsigned long long* presets,unsigned presetCapacity,
        unsigned long long* fifo,unsigned fifoCapacity,unsigned long long* counts,unsigned countCapacity,
        unsigned* available,unsigned availableCapacity){
    if(!pointer)return 0;auto& s=*static_cast<NativeMidiActivity*>(pointer);AcquireSRWLockShared(&s.lock);
    if(s.revision!=revision){ReleaseSRWLockShared(&s.lock);return -1;}
    const auto fifoCount=s.fifoSize();
    if(!channels||!counts||channelCapacity<s.channels.size()||countCapacity<s.channels.size()*256||
            presetCapacity<s.presets.size()||fifoCapacity<fifoCount||availableCapacity<s.available.size()||
            (!presets&&!s.presets.empty())||(!fifo&&fifoCount)||(!available&&!s.available.empty())){
        ReleaseSRWLockShared(&s.lock);return 0;}
    for(size_t ch=0;ch<s.channels.size();++ch){const auto& channel=s.channels[ch];
        const auto values=NativeMidiActivity::channelValues(channel);const size_t stride=extended?6:4;
        std::copy_n(values.data(),stride,channels+ch*stride);
        for(unsigned key=0;key<256;++key){const auto& row=channel.keys[key];
            const unsigned long long cells[]{static_cast<unsigned long long>(ch),key,row.activity,row.overlap};std::copy_n(cells,4,counts+(ch*256+key)*4);}}
    for(size_t i=0;i<s.presets.size();++i){const auto values=NativeMidiActivity::presetValues(s.presets[i]);std::copy_n(values.data(),7,presets+i*7);}
    size_t index=0;for(size_t ch=0;ch<s.channels.size();++ch)for(unsigned key=0;key<256;++key){const auto& note=s.channels[ch].keys[key];
        for(size_t i=note.head;i<note.runs.size();++i){const auto& row=note.runs[i];const unsigned long long values[]{
            static_cast<unsigned long long>(ch),key,row.address,row.count,row.ordinal};std::copy_n(values,5,fifo+index++*5);}}
    if(!s.available.empty())std::copy(s.available.begin(),s.available.end(),available);
    ReleaseSRWLockShared(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl native_activity_transfer_copy(void* p,unsigned long long r,
        unsigned long long* c,unsigned nc,unsigned long long* s,unsigned ns,unsigned long long* f,unsigned nf,
        unsigned long long* k,unsigned nk,unsigned* a,unsigned na){return activityTransferCopy(false,p,r,c,nc,s,ns,f,nf,k,nk,a,na);}
extern "C" __declspec(dllexport) int __cdecl native_activity_transfer_copy_v2(void* p,unsigned long long r,
        unsigned long long* c,unsigned nc,unsigned long long* s,unsigned ns,unsigned long long* f,unsigned nf,
        unsigned long long* k,unsigned nk,unsigned* a,unsigned na){return activityTransferCopy(true,p,r,c,nc,s,ns,f,nf,k,nk,a,na);}
static std::unique_ptr<NativeMidiActivity> activityTransferStage(const unsigned long long* header,unsigned headerCount,
        const unsigned long long* channels,unsigned channelCount,const unsigned long long* presets,unsigned presetCount,
        const unsigned long long* fifo,unsigned fifoCount,const unsigned long long* counts,unsigned countCount,
        const unsigned* available,unsigned availableCount){
    using State=NativeMidiActivity;using U64=State::U64;
    if(!header||headerCount!=9||(header[8]!=1&&header[8]!=2)||!channelCount||channelCount>128||header[1]!=channelCount||
            header[2]!=presetCount||header[3]!=fifoCount||header[4]>1||header[6]!=availableCount||
            (!header[4]&&availableCount)||!channels||!counts||
            countCount!=channelCount*256||(!presets&&presetCount)||(!fifo&&fifoCount)||(!available&&availableCount))return nullptr;
    try{
        auto staged=std::make_unique<State>(channelCount);if(header[5]!=staged->frequency)return nullptr;
        staged->ordinal=header[7];staged->availableKnown=header[4]!=0;
        if(availableCount){staged->available.assign(available,available+availableCount);
            if(!std::is_sorted(staged->available.begin(),staged->available.end())||
                std::adjacent_find(staged->available.begin(),staged->available.end())!=staged->available.end())return nullptr;}
        staged->presets.reserve(presetCount);
        for(unsigned i=0;i<presetCount;++i){const auto* p=presets+static_cast<size_t>(i)*7;double initial=0;std::memcpy(&initial,p+5,sizeof(initial));
            if(p[0]>UINT_MAX||(i&&p[0]<=presets[(static_cast<size_t>(i)-1)*7])||p[2]>1||
                    !std::isfinite(initial)||initial<0||initial>100||
                    (p[6]!=State::absent&&p[6]>=channelCount)||
                    (p[3]!=State::absent&&(p[3]!=p[1]||(p[3]&&p[6]!=State::absent)))||
                    (p[3]==State::absent&&p[1])||
                    (p[3]==State::absent&&(p[4]||initial||p[6]!=State::absent)))return nullptr;
            State::Preset row{static_cast<unsigned>(p[0])};row.active=p[1];row.played=p[2]!=0;row.visual=p[3];row.phase=p[4];
            row.initial=initial;row.release=p[6]==State::absent?~0u:static_cast<unsigned>(p[6]);staged->presets.push_back(row);}
        for(unsigned ch=0;ch<channelCount;++ch){auto& c=staged->channels[ch];const auto* p=channels+ch*(header[8]==2?6:4);
            if(p[1]>255)return nullptr;c.velocity=static_cast<unsigned>(p[1]);c.qpc=p[2];
            if(header[8]==2){double initial=0;std::memcpy(&initial,p+5,sizeof(initial));
                if(!std::isfinite(initial)||initial<0||initial>100)return nullptr;c.phase=p[4];c.initial=initial;}
            for(unsigned key=0;key<256;++key){const auto* cell=counts+(static_cast<size_t>(ch)*256+key)*4;
                if(cell[0]!=ch||cell[1]!=key||cell[2]>State::absent-c.activity||cell[3]>State::absent-c.overlap)return nullptr;
                c.keys[key].activity=cell[2];c.keys[key].overlap=cell[3];c.activity+=cell[2];c.overlap+=cell[3];}
            if(c.activity!=p[0]||c.overlap!=p[3])return nullptr;}
        std::vector<U64> sums(presetCount,0),keySums(countCount,0);
        std::vector<std::pair<U64,U64>> intervals;intervals.reserve(fifoCount);
        U64 previousCell=0,previousEnd=0;bool first=true;
        for(unsigned i=0;i<fifoCount;++i){const auto* p=fifo+static_cast<size_t>(i)*5;
            if(p[0]>=channelCount||p[1]>=256||p[2]>UINT_MAX||!p[3]||!p[4]||p[4]>header[7]||p[3]-1>header[7]-p[4])return nullptr;
            const U64 cell=p[0]*256+p[1],end=p[4]+p[3]-1;
            if(!first&&(cell<previousCell||(cell==previousCell&&p[4]<=previousEnd)))return nullptr;
            previousCell=cell;previousEnd=end;first=false;
            auto found=staged->find(static_cast<unsigned>(p[2]));if(found==staged->presets.end()||found->address!=p[2])return nullptr;
            const auto presetIndex=static_cast<size_t>(found-staged->presets.begin());
            if(p[3]>State::absent-sums[presetIndex]||p[3]>State::absent-keySums[static_cast<size_t>(cell)])return nullptr;
            sums[presetIndex]+=p[3];keySums[static_cast<size_t>(cell)]+=p[3];
            staged->channels[static_cast<size_t>(p[0])].keys[static_cast<size_t>(p[1])].runs.push_back(
                State::Run{static_cast<unsigned>(p[2]),p[3],p[4]});intervals.emplace_back(p[4],end);}
        std::sort(intervals.begin(),intervals.end());for(size_t i=1;i<intervals.size();++i)if(intervals[i].first<=intervals[i-1].second)return nullptr;
        for(unsigned i=0;i<presetCount;++i)if(staged->presets[i].active!=sums[i])return nullptr;
        for(unsigned ch=0;ch<channelCount;++ch)for(unsigned key=0;key<256;++key)
            if(keySums[ch*256+key]>staged->channels[ch].keys[key].activity)return nullptr;
        return staged;
    }catch(...){return nullptr;}
}
// Standalone/initial preparation only. A generic pause is not permission to
// overwrite an attached actor; its future typed handoff must prove ownership.
// Staging validates all cross-row totals/ordinals BEFORE the target changes.
extern "C" __declspec(dllexport) int __cdecl native_activity_transfer_import(void* pointer,unsigned long long revision,
        const unsigned long long* header,unsigned headerCount,const unsigned long long* channels,unsigned channelCount,
        const unsigned long long* presets,unsigned presetCount,const unsigned long long* fifo,unsigned fifoCount,
        const unsigned long long* counts,unsigned countCount,const unsigned* available,unsigned availableCount){
    if(!pointer)return 0;auto staged=activityTransferStage(header,headerCount,channels,channelCount,presets,presetCount,
        fifo,fifoCount,counts,countCount,available,availableCount);if(!staged)return 0;
    auto& s=*static_cast<NativeMidiActivity*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.revision!=revision){ReleaseSRWLockExclusive(&s.lock);return -1;}
    if(s.attachedOwner||s.reservationThread||s.shadowDirty||s.channels.size()!=staged->channels.size()||s.revision==NativeMidiActivity::absent){
        ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.channels.swap(staged->channels);s.presets.swap(staged->presets);s.available.swap(staged->available);
    s.availableKnown=staged->availableKnown;s.ordinal=staged->ordinal;s.chordBaseline();s.commit();
    ReleaseSRWLockExclusive(&s.lock);return 1;
}
// ABI1 chord rows are four uint64 fields: serial/packed/channel/QPC.
// Atomic copy of bounded history and current logical counts. A busy writer
// returns -1; observation retries without changing MIDI or audio ownership.
extern "C" __declspec(dllexport) unsigned __cdecl native_activity_chord_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl native_activity_chord_copy(void* pointer,unsigned long long cursor,
        unsigned long long* header,unsigned headerCapacity,unsigned long long* rows,unsigned rowCapacity,
        unsigned long long* counts,unsigned countCapacity){
    if(!pointer||!header||headerCapacity<6||!rows||rowCapacity<8192||!counts)return 0;
    auto& s=*static_cast<NativeMidiActivity*>(pointer);
    if(countCapacity<s.channels.size()*128)return 0;
    if(!TryAcquireSRWLockShared(&s.lock))return -1;
    const auto serial=s.chordSerial;
    const auto first=std::max(s.chordFloor+1,serial>=8192?serial-8192+1:1ull);
    const bool complete=cursor>=first-1&&cursor<=serial;
    unsigned copied=0;
    if(complete)for(auto next=std::max(first,cursor+1);next<=serial&&next!=0;++next){
        const auto& row=s.chordRows[(next-1)%s.chordRows.size()];
        auto* out=rows+copied*4;out[0]=row.serial;out[1]=row.packed;out[2]=row.channel;out[3]=row.qpc;++copied;
    }
    for(size_t ch=0;ch<s.channels.size();++ch)for(unsigned key=0;key<128;++key)
        counts[ch*128+key]=s.channels[ch].keys[key].activity;
    header[0]=serial;header[1]=complete?1:0;header[2]=s.validity.load(std::memory_order_relaxed)&1;
    header[3]=s.frequency;header[4]=copied;header[5]=s.channels.size();
    ReleaseSRWLockShared(&s.lock);return 1;
}
// Immutable presentation ABI1. A caller-owned, zeroed, eight-byte-aligned
// 24-byte lease avoids native allocation on a GUI read. Interlocked state:
// 0=free,-1=acquiring,1=held,3=copying,-2=releasing. Each successful acquire
// retains the owner until release. The root MUST already be borrowed before
// acquire: no raw-pointer retain can repair a freed root or an ABA race.
// A held lease buffer must not be cloned, overwritten or used as an output
// buffer: its single caller-owned identity is part of the lifetime contract.
// https://learn.microsoft.com/en-us/windows/win32/api/winnt/nf-winnt-interlockedcompareexchange64
struct NativeMidiActivityViewLease {NativeMidiActivity* owner;unsigned long long tag;volatile LONG64 state;};
static_assert(sizeof(NativeMidiActivityViewLease)==24&&alignof(NativeMidiActivityViewLease)==8,"activity presentation lease ABI1");
static NativeMidiActivityViewLease* activityViewLease(void* pointer,unsigned bytes){
    if(!pointer||bytes!=sizeof(NativeMidiActivityViewLease)||reinterpret_cast<uintptr_t>(pointer)%8)return nullptr;
    return static_cast<NativeMidiActivityViewLease*>(pointer);
}
static void activityViewHeader(NativeMidiActivity& owner,const NativeMidiActivity::View& view,unsigned generation,unsigned long long* header){
    const unsigned long long values[]{generation,view.revision,owner.visibleRevision.load(std::memory_order_acquire),
        static_cast<unsigned long long>(view.channels.size()),static_cast<unsigned long long>(view.presets.size()),view.frequency,view.qpc};
    std::copy_n(values,7,header);
}
extern "C" __declspec(dllexport) unsigned __cdecl native_activity_view_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl native_activity_view_lease_size(){return static_cast<unsigned>(sizeof(NativeMidiActivityViewLease));}
extern "C" __declspec(dllexport) unsigned __cdecl native_activity_view_prepare_version(){return 1;}
// Initial control-side capacity hint, never a preset/domain limit. Subsequent
// growth remains supported. No live attached writer or pinned reader can be
// resized by this preparation API.
extern "C" __declspec(dllexport) int __cdecl native_activity_view_prepare(void* pointer,unsigned presetCapacity){
    if(!pointer)return 0;auto& s=*static_cast<NativeMidiActivity*>(pointer);
    if(s.publishing.test_and_set(std::memory_order_acquire))return 0;
    struct PrepareLease {std::atomic_flag& flag;~PrepareLease(){flag.clear(std::memory_order_release);}} lease{s.publishing};
    AcquireSRWLockExclusive(&s.lock);bool valid=!s.attachedOwner&&!s.reservationThread;
    for(const auto& view:s.views)if(static_cast<unsigned>(view.state.load(std::memory_order_acquire)))valid=false;
    if(valid)try{for(auto& view:s.views){view.channels.reserve(s.channels.size());view.presets.reserve(presetCapacity);}}
        catch(...){valid=false;}
    ReleaseSRWLockExclusive(&s.lock);return valid?1:0;
}
// Input/control producer ONLY. No audio callback or GUI-driven publication.
// 1=published,2=BUSY/slots pinned (presentation skipped),0=invalid/resource.
extern "C" __declspec(dllexport) int __cdecl native_activity_view_publish(void* pointer,unsigned long long qpc){
    if(!pointer||!qpc)return 0;auto& s=*static_cast<NativeMidiActivity*>(pointer);
    if(!(s.validity.load(std::memory_order_acquire)&1ull))return 0;
    if(s.publishing.test_and_set(std::memory_order_acquire)){s.viewMetrics[1].fetch_add(1,std::memory_order_relaxed);return 2;}
    struct PublishLease {std::atomic_flag& flag;~PublishLease(){flag.clear(std::memory_order_release);}} publishing{s.publishing};
    if(s.viewGeneration==UINT_MAX){s.viewMetrics[3].fetch_add(1,std::memory_order_relaxed);return 0;} // Never wrap into ABA.
    const auto current=s.currentView.load(std::memory_order_acquire);unsigned winner=4;unsigned long long previous=0;
    for(unsigned index=0;index<s.views.size();++index){
        if(current&&static_cast<unsigned>(current)==index)continue;auto& view=s.views[index];
        auto state=view.state.load(std::memory_order_acquire);if(static_cast<unsigned>(state))continue;
        const auto writing=(state&0xffffffff00000000ull)|UINT_MAX;
        if(view.state.compare_exchange_strong(state,writing,std::memory_order_acq_rel,std::memory_order_acquire)){
            winner=index;previous=state;break;}
    }
    if(winner==4){s.viewMetrics[1].fetch_add(1,std::memory_order_relaxed);return 2;}
    auto& view=s.views[winner];bool prepared=false;
    // The GUI never takes this lock. Final attachment has one native writer;
    // this isolated component also tolerates explicitly concurrent writers.
    AcquireSRWLockShared(&s.lock);
    try{
        if(view.channels.capacity()<s.channels.size())NativeMidiActivity::reserveMore(view.channels,s.channels.size()-view.channels.size());
        if(view.presets.capacity()<s.presets.size())NativeMidiActivity::reserveMore(view.presets,s.presets.size()-view.presets.size());
        view.channels.resize(s.channels.size());view.presets.resize(s.presets.size());
        for(size_t i=0;i<s.channels.size();++i)view.channels[i]=NativeMidiActivity::channelValues(s.channels[i]);
        for(size_t i=0;i<s.presets.size();++i)view.presets[i]=NativeMidiActivity::presetValues(s.presets[i]);
        view.revision=s.revision;view.frequency=s.frequency;view.qpc=qpc;view.validity=s.validity.load(std::memory_order_acquire);
        prepared=!s.shadowDirty&&(view.validity&1ull)!=0;
    }catch(...){s.viewMetrics[2].fetch_add(1,std::memory_order_relaxed);}
    ReleaseSRWLockShared(&s.lock);
    if(!prepared){view.state.store(previous,std::memory_order_release);return 0;}
    const auto generation=++s.viewGeneration;view.state.store(static_cast<unsigned long long>(generation)<<32,std::memory_order_release);
    s.currentView.store((static_cast<unsigned long long>(generation)<<32)|winner,std::memory_order_release);
    s.publishedRevision.store(view.revision,std::memory_order_release);s.publishedValidity.store(view.validity,std::memory_order_release);
    s.viewMetrics[0].fetch_add(1,std::memory_order_relaxed);return 1;
}
// Header7: viewGeneration/sourceRevision/currentCanonicalRevision/channelRows/
// presetRows/QPCfrequency/publishQPC. Only currentCanonicalRevision is a live
// atomic observation; every presentation row belongs to sourceRevision.
extern "C" __declspec(dllexport) int __cdecl native_activity_view_acquire(void* pointer,void* storage,unsigned bytes,
        unsigned long long* header,unsigned capacity){
    auto* lease=activityViewLease(storage,bytes);if(!pointer||!lease||!header||capacity<7)return 0;
    if(InterlockedCompareExchange64(&lease->state,-1,0)!=0)return 2;
    auto& s=*static_cast<NativeMidiActivity*>(pointer);s.retain();
    for(unsigned attempt=0;attempt<8;++attempt){const auto tag=s.currentView.load(std::memory_order_acquire);
        if(!tag)break;const unsigned index=static_cast<unsigned>(tag),generation=static_cast<unsigned>(tag>>32);
        if(index>=s.views.size())break;auto& view=s.views[index];auto state=view.state.load(std::memory_order_acquire);
        const unsigned readers=static_cast<unsigned>(state);
        if(static_cast<unsigned>(state>>32)!=generation||readers>=UINT_MAX-1)continue;
        if(!view.state.compare_exchange_strong(state,state+1,std::memory_order_acq_rel,std::memory_order_acquire))continue;
        if(s.currentView.load(std::memory_order_acquire)!=tag){view.state.fetch_sub(1,std::memory_order_release);continue;}
        lease->owner=&s;lease->tag=tag;activityViewHeader(s,view,generation,header);InterlockedExchange64(&lease->state,1);return 1;
    }
    InterlockedExchange64(&lease->state,0);s.release();return 2;
}
// Copies from the pinned immutable cell only. It takes no canonical, worker,
// kernel or BASS lock, and never allocates. Capacity refusal writes nothing.
static int activityViewCopy(bool extended,void* storage,unsigned bytes,
        unsigned long long* header,unsigned headerCapacity,unsigned long long* channels,unsigned channelCapacity,
        unsigned long long* presets,unsigned presetCapacity){
    auto* lease=activityViewLease(storage,bytes);if(!lease||!header||headerCapacity<7)return 0;
    if(InterlockedCompareExchange64(&lease->state,3,1)!=1)return 2;
    auto& s=*lease->owner;const unsigned index=static_cast<unsigned>(lease->tag),generation=static_cast<unsigned>(lease->tag>>32);
    auto& view=s.views[index];
    if(!channels||channelCapacity<view.channels.size()||presetCapacity<view.presets.size()||(!presets&&!view.presets.empty())){
        InterlockedExchange64(&lease->state,1);return 0;}
    const size_t stride=extended?6:4;
    for(size_t i=0;i<view.channels.size();++i)std::copy_n(view.channels[i].data(),stride,channels+i*stride);
    for(size_t i=0;i<view.presets.size();++i)std::copy_n(view.presets[i].data(),7,presets+i*7);
    activityViewHeader(s,view,generation,header);InterlockedExchange64(&lease->state,1);return 1;
}
extern "C" __declspec(dllexport) int __cdecl native_activity_view_copy(void* s,unsigned b,unsigned long long* h,unsigned nh,
        unsigned long long* c,unsigned nc,unsigned long long* p,unsigned np){return activityViewCopy(false,s,b,h,nh,c,nc,p,np);}
extern "C" __declspec(dllexport) int __cdecl native_activity_view_copy_v2(void* s,unsigned b,unsigned long long* h,unsigned nh,
        unsigned long long* c,unsigned nc,unsigned long long* p,unsigned np){return activityViewCopy(true,s,b,h,nh,c,nc,p,np);}
extern "C" __declspec(dllexport) int __cdecl native_activity_view_release(void* storage,unsigned bytes){
    auto* lease=activityViewLease(storage,bytes);if(!lease)return 0;
    const LONG64 previous=InterlockedCompareExchange64(&lease->state,-2,1);
    if(previous!=1)return previous==0?0:2;
    auto* owner=lease->owner;const unsigned index=static_cast<unsigned>(lease->tag);
    owner->views[index].state.fetch_sub(1,std::memory_order_release);owner->release();
    // Keep state=-2 through all owner access: a new acquire cannot reuse the
    // caller lease while an old release is still reading its owner/tag.
    InterlockedExchange64(&lease->state,0);return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl native_activity_view_stats(void* pointer,unsigned long long* values,unsigned capacity){
    if(!pointer||!values||capacity<4)return 0;auto& s=*static_cast<NativeMidiActivity*>(pointer);
    for(unsigned i=0;i<4;++i)values[i]=s.viewMetrics[i].load(std::memory_order_acquire);return 4;
}
extern "C" __declspec(dllexport) unsigned __cdecl native_activity_view_quality_version(){return 1;}
// Additive pinned-view quality: captured/live validity and source/live revision.
// Uses only the lease CAS and atomic loads, never canonical/kernel/BASS locks.
extern "C" __declspec(dllexport) unsigned __cdecl native_activity_view_quality(void* storage,unsigned bytes,
        unsigned long long* values,unsigned capacity){
    auto* lease=activityViewLease(storage,bytes);if(!lease||!values||capacity<4)return 0;
    if(InterlockedCompareExchange64(&lease->state,3,1)!=1)return 2;
    auto& owner=*lease->owner;const auto& view=owner.views[static_cast<unsigned>(lease->tag)];
    unsigned long long quality=0,revision=0;bool coherent=false;
    for(unsigned i=0;i<8;++i){quality=owner.validity.load(std::memory_order_acquire);
        revision=owner.visibleRevision.load(std::memory_order_acquire);
        if(quality==owner.validity.load(std::memory_order_acquire)){coherent=true;break;}}
    if(coherent){values[0]=view.validity;values[1]=quality;values[2]=view.revision;values[3]=revision;}
    InterlockedExchange64(&lease->state,1);return coherent?4u:2u;
}
NativeActivityIdentity NativeSelectionState::noteIdentity(unsigned channel,const MidiNoteAction& action){
    const unsigned request=requestedAddress[channel].load(std::memory_order_acquire);unsigned bank=request&0x1000000u?128u:request&255u,program=(request>>16)&255u;bool known=true;
    if((action.packed&0xf0u)==0x90u&&(action.packed&0x7f0000u)&&(request&0xff00u)){BASS_MIDI_FONT active{};
        if(!source(stream,channel,&active)||!active.font)known=false;else{bank=static_cast<unsigned>(active.bank);program=static_cast<unsigned>(active.preset);}}
    return {known,bank&65535u,program&65535u};
}
void NativeSelectionState::note(unsigned channel,const MidiNoteAction& action){
    const auto identity=noteIdentity(channel,action);const unsigned address=identity.bank|(identity.program<<16);
    write(channel,static_cast<unsigned>(action.qpc),static_cast<unsigned>(action.qpc>>32),identity.known?16u:17u,action.packed,static_cast<int>(address));
}
// Shared original-wire iterator for the already qualified explicit channel
// family plus complete system-common/realtime spans. It never converts RAW,
// invents running status, splits StreamEvents or interprets SysEx/meta. Every
// input-side reservation, simulation, emission and timing pass uses the same
// widths, so non-channel bytes cannot misalign a later original Note/CC.
// bassmidi.h RAW/NORSTATUS and StreamEvents documentation remain the decoder
// authority. A zero width is a pristine compatibility handoff, NOT a drop.
static unsigned midiOriginalSpan(const unsigned char* input,unsigned length,unsigned at){
    if(!input||at>=length)return 0;const unsigned status=input[at];unsigned width=0;
    if(status>=0x80u&&status<0xf0u)width=(status&0xf0u)==0xc0u||(status&0xf0u)==0xd0u?2u:3u;
    else if(status>=0xf8u&&status<=0xfeu)width=1; // Reserved F9/FD stay verbatim.
    else if(status==0xf1u||status==0xf3u)width=2;
    else if(status==0xf2u)width=3;
    else if(status==0xf6u||status==0xf7u)width=1;
    if(!width||width>length-at)return 0;
    for(unsigned part=1;part<width;++part)if(input[at+part]>=0x80u)return 0;
    return width;
}
struct MidiNoteKernel {
    SRWLOCK lock=SRWLOCK_INIT;
    void* executionOwner=nullptr;
    bool executionPaused=false;
    unsigned channels;
    bool overlap=false;
    MidiHumanization humanization;
    std::atomic<unsigned> references{1};
    NativeRawDecodeContext* rawContext=nullptr;
    NativeMidiActivity* activity=nullptr;bool activitySourcesReady=false,activityDirty=false,activityAuthoritative=false;
    std::array<NativeActivityIdentity,128> activitySources{};
    std::array<unsigned,128*128> activityOns{},activityTouched{};
    std::array<unsigned long long,128*128> activityEpochs{};unsigned long long activityEpoch=0;
    unsigned activityDepth=0;bool activityKeep=false;
    unsigned long long activityNotes=0,activityClears=0,activityRefusals=0,activityFrameActions=0;
    std::array<unsigned,128> velocity{};
    std::array<bool,128> mixerMuted{};
    std::array<unsigned long long,128*128> counts{},previous{},epochs{};
    std::array<unsigned,128*128> touched{};
    unsigned long long epoch=0;
    DWORD(WINAPI* emit)(HSTREAM,DWORD,void*,DWORD);
    int(WINAPI* error)();
    MidiNoteKernel(unsigned size,void* submit,void* failure):channels(size),
        emit(reinterpret_cast<DWORD(WINAPI*)(HSTREAM,DWORD,void*,DWORD)>(submit)),
        error(reinterpret_cast<int(WINAPI*)()>(failure)){std::iota(velocity.begin(),velocity.end(),0u);}
    ~MidiNoteKernel(){if(rawContext)rawContext->release();if(activity){AcquireSRWLockExclusive(&activity->lock);
        activity->attachedOwner=nullptr;activity->reservationThread=0;ReleaseSRWLockExclusive(&activity->lock);activity->release();}}
    void retain(){references.fetch_add(1,std::memory_order_relaxed);}
    void release(){if(references.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
    // A reservation is ownership, NOT a held lock. BASS may run while the
    // canonical owner is reserved; all unrelated public mutations refuse.
    bool prepareActivity(const unsigned char* input,unsigned length,unsigned port,unsigned budget,bool implicit=false){
        if(!activity)return true;if(activityDirty){++activityRefusals;return false;}
        auto& state=*activity;AcquireSRWLockExclusive(&state.lock);
        if(activityDepth||activityKeep){const bool same=state.reservationThread==GetCurrentThreadId();
            if(same)++activityDepth;else ++activityRefusals;ReleaseSRWLockExclusive(&state.lock);return same;}
        if(state.attachedOwner!=this||state.reservationThread||state.revision>UINT64_MAX-budget){
            ++activityRefusals;ReleaseSRWLockExclusive(&state.lock);return false;}
        if(++activityEpoch==0){activityEpochs.fill(0);activityEpoch=1;}
        unsigned touchedCount=0,totalOn=0;std::array<unsigned,128> channelOn{};
        for(unsigned i=0;i<length;){const unsigned command=input[i]&0xf0u,width=implicit&&input[i]<128u?1u:midiOriginalSpan(input,length,i);
            if(!width){++activityRefusals;ReleaseSRWLockExclusive(&state.lock);return false;}
            if(command==0x90u&&input[i+2]){const unsigned ch=port*16+(input[i]&15u),key=input[i+1],index=ch*128+key;
                if(activityEpochs[index]!=activityEpoch){activityEpochs[index]=activityEpoch;activityOns[index]=0;activityTouched[touchedCount++]=index;}
                ++activityOns[index];++channelOn[ch];++totalOn;}
            i+=width;
        }
        bool ok=state.ordinal<=UINT64_MAX-totalOn;
        for(unsigned ch=0;ok&&ch<channels;++ch)ok=state.channels[ch].activity<=UINT64_MAX-channelOn[ch]&&
            state.channels[ch].overlap<=UINT64_MAX-channelOn[ch];
        for(const auto& row:state.presets)if(row.active>UINT64_MAX-totalOn||
            (row.visual!=NativeMidiActivity::absent&&row.visual>UINT64_MAX-1-totalOn)){ok=false;break;}
        for(unsigned i=0;ok&&i<touchedCount;++i){const unsigned index=activityTouched[i];const auto& key=state.channels[index/128].keys[index%128];
            ok=key.activity<=UINT64_MAX-activityOns[index];}
        if(ok)try{NativeMidiActivity::reserveMore(state.presets,static_cast<size_t>(totalOn)*2);
            for(unsigned i=0;i<touchedCount;++i){const unsigned index=activityTouched[i];
                NativeMidiActivity::reserveMore(state.channels[index/128].keys[index%128].runs,activityOns[index]);}}
            catch(...){ok=false;}
        if(ok){state.reservationThread=GetCurrentThreadId();activityDepth=1;activityFrameActions=0;}
        else ++activityRefusals;ReleaseSRWLockExclusive(&state.lock);return ok;
    }
    void endActivity(bool keep=false){if(!activity)return;auto& state=*activity;AcquireSRWLockExclusive(&state.lock);
        if(keep)activityKeep=true;if(activityDepth)--activityDepth;
        if(!activityDepth&&!activityKeep)state.reservationThread=0;ReleaseSRWLockExclusive(&state.lock);}
    void finishActivityPending(){activityKeep=false;endActivity();}
    bool observeActivity(const MidiNoteAction& action,const NativeActivityIdentity& source){
        if(!activity)return true;auto& state=*activity;AcquireSRWLockExclusive(&state.lock);
        const bool valid=state.reservationThread==GetCurrentThreadId()&&state.attachedOwner==this&&!activityDirty;
        const bool ok=valid&&state.action(action.packed,action.channel,action.count,action.qpc,source.known,source.bank,source.program);
        if(ok){++activityFrameActions;if((action.packed&0xf0u)==0xb0u)++activityClears;else ++activityNotes;}
        else{activityDirty=true;state.shadowDirty=true;state.qualify(false);++activityRefusals;}ReleaseSRWLockExclusive(&state.lock);return ok;
    }
    void clearActivity(){if(!activity)return;auto& state=*activity;AcquireSRWLockExclusive(&state.lock);
        if(state.reservationThread==GetCurrentThreadId()&&state.revision!=UINT64_MAX&&!activityDirty){state.clearActive();++activityClears;++activityFrameActions;}
        else{activityDirty=true;state.shadowDirty=true;state.qualify(false);++activityRefusals;}ReleaseSRWLockExclusive(&state.lock);}
    void dirtyActivity(){if(!activity)return;AcquireSRWLockExclusive(&activity->lock);
        activityDirty=true;activity->shadowDirty=true;activity->qualify(false);ReleaseSRWLockExclusive(&activity->lock);}
    // Caller owns note lock and qualified counters/grammar. Original actions
    // stay distinct from overlap-generated/suppressed wire events.
    void transform(unsigned status,unsigned key,unsigned value,unsigned channel,unsigned char* output,unsigned& bytes,MidiNoteAction& action){
        const unsigned index=channel*128+key;const bool on=(status&0xf0u)==0x90u&&value;
        const auto pair=humanization.note(channel,key,on);
        const unsigned processed=on?velocity[humanization.velocity(value)]:value;
        bool suppress=false;
        if(on){
            const auto previousCount=counts[index]++;
            suppress=mixerMuted[channel];
            if(overlap&&previousCount&&!suppress){humanization.span(bytes,channel,key,0,pair);output[bytes++]=static_cast<unsigned char>(0x80u|(status&15u));output[bytes++]=static_cast<unsigned char>(key);output[bytes++]=0;}
        }else{if(counts[index])--counts[index];suppress=overlap&&counts[index]!=0;}
        LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);
        action={status|(key<<8)|(value<<16),channel,counts[index],static_cast<unsigned long long>(tick.QuadPart)};
        if(!suppress){humanization.span(bytes,channel,key,on?processed:0,pair);output[bytes++]=static_cast<unsigned char>(status);output[bytes++]=static_cast<unsigned char>(key);output[bytes++]=static_cast<unsigned char>(processed);}
    }
};
struct MidiActivityLease {
    MidiNoteKernel* notes=nullptr;unsigned* pending=nullptr;bool ready=true;
    MidiActivityLease(MidiNoteKernel* owner,const unsigned char* input,unsigned length,unsigned port,unsigned budget,unsigned* retained=nullptr,bool implicit=false):pending(retained){
        if(owner&&owner->activity){ready=owner->prepareActivity(input,length,port,budget,implicit);if(ready)notes=owner;}}
    ~MidiActivityLease(){if(notes)notes->endActivity(pending&&*pending);}
};
struct MidiNoteLease {
    MidiNoteKernel* state;
    explicit MidiNoteLease(MidiNoteKernel* value):state(value){if(state)AcquireSRWLockExclusive(&state->lock);}
    ~MidiNoteLease(){if(state)ReleaseSRWLockExclusive(&state->lock);}
    void suspend(){if(state){ReleaseSRWLockExclusive(&state->lock);state=nullptr;}}
    void resume(MidiNoteKernel* value){state=value;if(state)AcquireSRWLockExclusive(&state->lock);}
};
struct MidiCCAction {unsigned packed,channel,event,raw,accepted;int error;unsigned long long qpc;};
static_assert(sizeof(MidiCCAction)==32,"CC action ABI1");
// Additive pre-RAW scalar semantics. All allocations/copies occur at create.
// flags: field 1=saved expression,2=MOD readback,4=GS/XG dynamic neutral;
// route 1=mapped input,2=saved expression,4=attempted timbre.
struct MidiCCSemanticValue {int input,amount;unsigned raw;};
struct MidiCCSemanticField {unsigned event;int low,high,neutral;unsigned flags;const int* decoder;};
struct MidiCCSemanticDescriptor {unsigned channel,cc,source,destination,event,flags;const MidiCCSemanticValue* table;};
static_assert(sizeof(MidiCCSemanticField)==32&&sizeof(MidiCCSemanticDescriptor)==32,"scalar semantics ABI1");
struct MidiCCSemanticRoute {unsigned source,destination,event,flags;std::array<MidiCCSemanticValue,128> table;};
struct MidiCCArmPatch {unsigned captured,channel,cc,first,count,blocked;};
static_assert(sizeof(MidiCCArmPatch)==24,"armed successor patch ABI1");
struct MidiCCSemanticState {
    std::atomic<unsigned> references{1};NativeMidiState* journal;NativeMidiState* mod;NativeMidiState* mappings;NativeMidiState* resetJournal=nullptr;
    std::atomic<unsigned long long> failures{0};
    DWORD(WINAPI* get)(HSTREAM,DWORD,DWORD);
    struct Field {unsigned event,flags;int low,high,neutral;std::vector<int> decoder;
        std::array<int,128> saved{},modRaw{};std::array<bool,128> present{},modPresent{},timbrePresent{},sourceMapped{};
        std::array<unsigned long long,128> modSerial{},timbreSerial{};std::array<unsigned,7> slots{};unsigned request=256,delayRequest=256,timbreWatch=256;};
    std::vector<Field> fields;std::vector<MidiCCSemanticRoute> routes;
    std::array<unsigned,128*128> first{},counts{};
    std::array<unsigned,128> resetFirst{},resetCount{};bool resetScalarReady=false;
    std::vector<unsigned> routeOrder;std::array<std::vector<MidiCCArmPatch>,120> successors;
    bool indirect=false,armed=false,armAll=false;unsigned armChannel=0,armKey=0,armDestination=0,armActive=0;
    unsigned long long armRevision=0;
    std::array<int,128> modeSaved{};std::array<bool,128> modePresent{};
    std::array<unsigned,5> modeSlots{};bool modeReady=false;
    MidiCCSemanticState(NativeMidiState* state,void* reader,NativeMidiState* observed,NativeMidiState* mapped):
        journal(state),mod(observed),mappings(mapped),get(reinterpret_cast<DWORD(WINAPI*)(HSTREAM,DWORD,DWORD)>(reader)){}
    ~MidiCCSemanticState(){journal->release();if(mod)mod->release();if(mappings)mappings->release();if(resetJournal)resetJournal->release();}
    void retain(){references.fetch_add(1,std::memory_order_relaxed);}
    void release(){if(references.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
    const MidiCCSemanticRoute& route(unsigned i)const{return routes[indirect?routeOrder[i]:i];}
    unsigned slot(unsigned event)const{for(unsigned i=0;i<journal->eventCount;++i)if(journal->eventTypes[i]==event)return i;return 256;}
    void write(unsigned channel,unsigned field,unsigned kind,unsigned raw){auto* destination=kind==6?resetJournal:journal;if(destination)destination->receive(journal->stream,fields[field].slots[kind],(channel<<16)|(raw&65535u));}
    unsigned long long observed(NativeMidiState* state,unsigned channel,unsigned event)const{
        if(!state)return 0;for(unsigned i=0;i<state->eventCount;++i)if(state->eventTypes[i]==event)
            return state->cells[state->offsets[i]+channel].load(std::memory_order_acquire);return 0;
    }
    void refreshTimbre(unsigned channel,Field& field){
        if(!mappings||field.timbreWatch==256)return;
        const auto value=mappings->cells[mappings->offsets[field.timbreWatch]+channel].load(std::memory_order_acquire);
        // Function-map attempts precede the target API, including failed APIs.
        // This is presence only: existing journals retain value/provenance replay.
        if((value>>16)>field.timbreSerial[channel]){field.timbrePresent[channel]=true;field.timbreSerial[channel]=value>>16;}
    }
    int neutral(unsigned channel,const Field& field)const{
        if(field.flags&4u){const unsigned system=get(journal->stream,0,MIDI_EVENT_SYSTEM);if(system==3||system==4||system==5)return 0;}
        (void)channel;return field.neutral;
    }
    int current(unsigned channel,Field& field,int fallback){
        if(field.flags&2u){
            const auto value=std::max(observed(mod,channel,field.event),observed(mappings,channel,field.event));
            if((value>>16)>field.modSerial[channel]){field.modRaw[channel]=static_cast<int>(value&65535u);field.modPresent[channel]=true;field.modSerial[channel]=value>>16;}
            return field.modPresent[channel]?field.decoder[static_cast<unsigned>(field.modRaw[channel])]:neutral(channel,field);
        }
        const unsigned raw=get(journal->stream,channel,field.event);
        if(raw==0xffffffffu)return fallback;
        // Expression readback clamps except MOD; unknown oversized raw keeps
        // the monotonic documented domain edge. All allowed encodings <=65535.
        return field.decoder[std::min(raw,65535u)];
    }
};
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_semantic_version(){return 1;}
// Additive compound mode ABI: flags8/16/24 are Mono/Poly/Legato selectors,
// not three independent boolean states. Local bassmidi.h: MODE22,0/1/2.
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_semantic_mode_version(){return 1;}
// Reset defaults use the saved-expression MODE setter, not CC126/127 RAW.
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_semantic_reset_mode_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_semantic_mode_seed(void* pointer,const int* seeds,unsigned count){
    if(!pointer||!seeds||count!=256)return 0;auto& s=*static_cast<MidiCCSemanticState*>(pointer);
    // Immutable generation is seeded before publication/borrowers exist.
    if(s.references.load()!=1||s.modeReady)return 0;
    for(unsigned ch=0;ch<128;++ch)if(seeds[ch*2]<0||seeds[ch*2]>1||seeds[ch*2+1]<0||seeds[ch*2+1]>2)return 0;
    constexpr unsigned kinds[]{0x140000,0x150000,0x160000,0x170000,0x180000};
    for(unsigned i=0;i<5;++i){s.modeSlots[i]=s.slot(kinds[i]);if(s.modeSlots[i]==256)return 0;}
    for(unsigned ch=0;ch<128;++ch){s.modePresent[ch]=seeds[ch*2]!=0;s.modeSaved[ch]=seeds[ch*2+1];}
    s.modeReady=true;return 1;
}
extern "C" __declspec(dllexport) void* __cdecl midi_cc_semantic_create(void* state,void* reader,void* observed,void* mapped,
        const MidiCCSemanticField* fields,unsigned fieldCount,const MidiCCSemanticDescriptor* descriptors,unsigned count,
        const int* seeds,unsigned seedCount){
    if(!state||!reader||!fields||!fieldCount||fieldCount>40||(count&&!descriptors)||count>128*120*40||!seeds||seedCount!=fieldCount*128*4)return nullptr;
    auto* journal=static_cast<NativeMidiState*>(state);auto* mod=static_cast<NativeMidiState*>(observed);auto* mappings=static_cast<NativeMidiState*>(mapped);
    if((mod&&(mod->clock!=journal->clock||mod->stream!=journal->stream))||(mappings&&(mappings->clock!=journal->clock||mappings->stream!=journal->stream)))return nullptr;
    try{
        auto s=std::make_unique<MidiCCSemanticState>(journal,reader,mod,mappings);
        // Acquire borrowers immediately so every failed preparation releases exactly once.
        journal->retain();if(mod)mod->retain();if(mappings)mappings->retain();
        s->fields.resize(fieldCount);
        for(unsigned i=0;i<fieldCount;++i){auto& f=s->fields[i];const auto& d=fields[i];
            if(d.flags>15||d.low>d.high||d.high-d.low>65535||((d.flags&1)&&(!d.decoder||d.high-d.low>32767)))return nullptr;
            f.event=d.event;f.flags=d.flags;f.low=d.low;f.high=d.high;f.neutral=d.neutral;
            if(d.decoder)f.decoder.assign(d.decoder,d.decoder+65536);
            constexpr unsigned kinds[]{0xc0000,0xd0000,0xe0000,0xf0000,0x100000,0x110000};
            for(unsigned k=0;k<6;++k){f.slots[k]=s->slot(kinds[k]+i);if(f.slots[k]==256)return nullptr;}
            f.slots[6]=256;
            if(d.event==MIDI_EVENT_REVERB||d.event==MIDI_EVENT_CHORUS){f.request=s->slot(0x120000+i);if(f.request==256)return nullptr;}
            if(d.event==MIDI_EVENT_USERFX)f.delayRequest=s->slot(0x130000+i);
            for(unsigned ch=0;ch<128;++ch){const unsigned at=(i*128+ch)*4;
                if(seeds[at]<0||seeds[at]>1||seeds[at+2]<0||seeds[at+2]>((d.flags&8u)?7:1)||seeds[at+3]<0||seeds[at+3]>65535)return nullptr;
                f.present[ch]=seeds[at]!=0;f.saved[ch]=seeds[at+1];f.modPresent[ch]=(seeds[at+2]&1)!=0;f.modRaw[ch]=seeds[at+3];
                f.timbrePresent[ch]=(seeds[at+2]&2)!=0;f.sourceMapped[ch]=(seeds[at+2]&4)!=0;
                f.modSerial[ch]=journal->clock->serial.load(std::memory_order_acquire);
            }
        }
        s->routes.reserve(count);
        for(unsigned i=0;i<count;++i){const auto& d=descriptors[i];
            const bool compound=(d.flags&24u)!=0,resetMode=(d.flags&32u)!=0;
            const bool pooled=d.channel==128&&d.cc==128;
            if((!pooled&&(d.channel>=128||d.cc>=120))||d.source>=fieldCount||d.flags>63||!d.table||
                (resetMode&&(!pooled||d.flags!=32||d.event!=MIDI_EVENT_MODE||d.destination!=UINT_MAX))||
                (compound&&(d.flags&7u||d.event!=MIDI_EVENT_MODE||d.destination!=UINT_MAX))||
                (!compound&&!resetMode&&d.event&&(d.destination>=fieldCount||s->fields[d.destination].event!=d.event||
                    ((d.flags&2u)!=0)!=((s->fields[d.destination].flags&1u)!=0))))return nullptr;
            const unsigned index=pooled?0:d.channel*128+d.cc;
            if(!pooled){if(!s->counts[index])s->first[index]=i;
                else if(s->first[index]+s->counts[index]!=i)return nullptr;}
            MidiCCSemanticRoute route{};route.source=d.source;route.destination=d.destination;route.event=d.event;route.flags=d.flags;
            std::copy_n(d.table,128,route.table.begin());
            for(const auto& v:route.table)if(resetMode?(v.amount<0||v.amount>2||v.input!=v.amount||v.raw!=static_cast<unsigned>(v.amount)):compound?(v.amount<0||v.amount>1||v.input!=v.amount||v.raw!=static_cast<unsigned>(v.amount)):
                (v.input<s->fields[d.source].low||v.input>s->fields[d.source].high||v.raw>65535||
                (d.event&&(v.amount<s->fields[d.destination].low||v.amount>s->fields[d.destination].high))))return nullptr;
            s->routes.push_back(route);if(!pooled)++s->counts[index];
        }
        return s.release();
    }catch(const std::bad_alloc&){return nullptr;}
}
extern "C" __declspec(dllexport) void __cdecl midi_cc_semantic_release(void* pointer){if(pointer)static_cast<MidiCCSemanticState*>(pointer)->release();}
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_semantic_timbre_watch_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_semantic_timbre_watch(void* pointer,unsigned field,unsigned event){
    if(!pointer)return 0;auto& s=*static_cast<MidiCCSemanticState*>(pointer);
    // Existing semantic create owns the mapped journal borrower; no new root.
    // Immutable preparation only, on the same stream/clock validated by create.
    if(s.references.load()!=1||!s.mappings||field>=s.fields.size()||!(s.fields[field].flags&8u)||
        s.fields[field].timbreWatch!=256||(event&0xff0000u)!=0x30000u)return 0;
    unsigned slot=256;for(unsigned i=0;i<s.mappings->eventCount;++i)if(s.mappings->eventTypes[i]==event){slot=i;break;}
    if(slot==256)return 0;
    s.fields[field].timbreWatch=slot;s.fields[field].timbreSerial.fill(s.journal->clock->serial.load(std::memory_order_acquire));return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_semantic_reset_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_semantic_reset_prepare(void* pointer,void* journal,const unsigned* rows,unsigned count){
    if(!pointer||!journal||!rows||count!=256)return 0;auto& s=*static_cast<MidiCCSemanticState*>(pointer);
    if(s.references.load()!=1||s.resetScalarReady)return 0;
    auto* target=static_cast<NativeMidiState*>(journal);if(target->stream!=s.journal->stream||target->clock!=s.journal->clock)return 0;
    for(unsigned i=0;i<s.fields.size();++i){unsigned slot=256;for(unsigned k=0;k<target->eventCount;++k)if(target->eventTypes[k]==0x1b0000u+i){slot=k;break;}
        if(slot==256)return 0;s.fields[i].slots[6]=slot;}
    for(unsigned ch=0;ch<128;++ch){const unsigned first=rows[ch*2],length=rows[ch*2+1];
        if(first>s.routes.size()||length>s.routes.size()-first)return 0;
        for(unsigned index=first;index<first+length;++index)if(s.routes[index].flags&24u)return 0;
    }
    for(unsigned ch=0;ch<128;++ch){s.resetFirst[ch]=rows[ch*2];s.resetCount[ch]=rows[ch*2+1];}
    target->retain();s.resetJournal=target;s.resetScalarReady=true;return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_semantic_arm_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_semantic_arm_prepare(void* pointer,unsigned channel,unsigned key,unsigned destination,
        unsigned all,unsigned long long revision,unsigned active,const unsigned* order,unsigned orderCount,const MidiCCArmPatch* patches,unsigned patchCount){
    if(!pointer||channel>=128||all>1||!active||active>128||!order||orderCount>262144||!patches||patchCount>30720)return 0;
    auto& s=*static_cast<MidiCCSemanticState*>(pointer);if(s.references.load()!=1||s.indirect||s.armed)return 0;
    for(unsigned i=0;i<orderCount;++i)if(order[i]>=s.routes.size())return 0;
    for(unsigned i=0;i<patchCount;++i){const auto& p=patches[i];
        if(p.captured>=120||p.channel>=active||p.cc>=120||p.blocked>1||p.first>orderCount||p.count>orderCount-p.first)return 0;}
    try{
        // Base route positions remain valid by reserving an identity prefix.
        s.routeOrder.resize(s.routes.size());std::iota(s.routeOrder.begin(),s.routeOrder.end(),0u);
        s.routeOrder.insert(s.routeOrder.end(),order,order+orderCount);
        for(unsigned i=0;i<patchCount;++i){auto p=patches[i];p.first+=static_cast<unsigned>(s.routes.size());s.successors[p.captured].push_back(p);}
        s.armChannel=channel;s.armKey=key;s.armDestination=destination;s.armAll=all!=0;s.armRevision=revision;s.armActive=active;
        s.indirect=true;s.armed=true;return 1;
    }catch(const std::bad_alloc&){s.routeOrder.clear();for(auto& rows:s.successors)rows.clear();return 0;}
}
// Execution idleness follows entry into the ORIGINAL application parser, not
// driver/read QPC or delayed metadata replay. QPC includes suspend time; Windows
// 8+ wait timeouts do not, so waits are only bounded wakeups for this predicate.
// https://learn.microsoft.com/en-us/windows/win32/sysinfo/acquiring-high-resolution-time-stamps
// https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject
struct MidiExecutionIdle {
    SRWLOCK lock=SRWLOCK_INIT;
    unsigned long long frequency=0,epoch=0,last=0,claims=0,completed=0,claimedEpoch=0;
    unsigned delay=0;
    bool fired=false,frame=false,frameEntered=false;
    std::atomic<bool> changed{false};
    MidiExecutionIdle(){LARGE_INTEGER f{};QueryPerformanceFrequency(&f);frequency=static_cast<unsigned long long>(f.QuadPart);}
    void configure(unsigned seconds){AcquireSRWLockExclusive(&lock);delay=seconds;++epoch;last=0;fired=false;
        changed.store(true,std::memory_order_release);ReleaseSRWLockExclusive(&lock);}
    void beginFrame(){AcquireSRWLockExclusive(&lock);frame=true;frameEntered=false;ReleaseSRWLockExclusive(&lock);}
    void endFrame(){AcquireSRWLockExclusive(&lock);frame=false;ReleaseSRWLockExclusive(&lock);}
    bool enter(){AcquireSRWLockExclusive(&lock);const bool admitted=frame&&!frameEntered;
        if(admitted){LARGE_INTEGER now{};QueryPerformanceCounter(&now);last=static_cast<unsigned long long>(now.QuadPart);fired=false;frameEntered=true;}
        ReleaseSRWLockExclusive(&lock);return admitted;}
    DWORD wait(){AcquireSRWLockShared(&lock);const DWORD result=delay&&last&&!fired?100u:INFINITE;ReleaseSRWLockShared(&lock);return result;}
    bool due(unsigned long long tick)const{return delay&&last&&!fired&&!frame&&tick>=last&&tick-last>=frequency*delay;}
    bool claim(){AcquireSRWLockExclusive(&lock);LARGE_INTEGER now{};QueryPerformanceCounter(&now);
        const bool admitted=due(static_cast<unsigned long long>(now.QuadPart));
        if(admitted){fired=true;claimedEpoch=epoch;++claims;}ReleaseSRWLockExclusive(&lock);return admitted;}
    void complete(){AcquireSRWLockExclusive(&lock);++completed;ReleaseSRWLockExclusive(&lock);}
};
// DLL-image token authority: UINT64_MAX is exhaustion, never token reuse.
static std::atomic<unsigned long long> midiCCFrameSequence{0};
static unsigned long long midiCCNextFrameReceipt(){
    auto current=midiCCFrameSequence.load(std::memory_order_relaxed);
    while(current!=UINT64_MAX){if(midiCCFrameSequence.compare_exchange_weak(current,current+1,std::memory_order_acq_rel))return current+1;}
    return 0;
}
struct MidiCCKernel {
    SRWLOCK lock=SRWLOCK_INIT;
    SRWLOCK frameAPILock=SRWLOCK_INIT;
    // Existing product frame limit, not a MIDI/BASS native maximum. Allocate
    // with the kernel on control-side creation, never in a filter/DSP callback.
    // Prepare once on the constructor/control path. Embedding 1 MiB in this
    // value type exceeds the default Windows stack for existing native users.
    // No resize or allocation is allowed while an original frame executes.
    std::vector<unsigned char> frameOriginal=std::vector<unsigned char>(1024*1024);
    unsigned long long frameReceipt=0;
    unsigned frameStream=0,frameMode=0,framePort=0,frameLength=0,frameConsumed=0;
    bool frameMixed=false;
    PlaybackTiming* frameTiming=nullptr;
    std::array<unsigned long long,7> frameOrigin{};
    void clearFrameReceipt(){
        releaseFailureCredit();
        if(frameTiming)frameTiming->release();frameTiming=nullptr;
        frameReceipt=0;frameStream=frameMode=framePort=frameLength=frameConsumed=0;frameMixed=false;frameOrigin.fill(0);
    }
    void finishFrameIfDone(){if(frameReceipt&&frameConsumed==frameLength&&!pendingStream&&!semanticActive&&!semanticFramePending&&
        !resetTailStream&&!gmActive)clearFrameReceipt();}
    unsigned channels,options=0,pendingStream=0,pendingThread=0;
    std::array<unsigned char,128*128> blocked{};
    std::array<bool,128> delayEnabled{};
    std::array<std::array<LegacyDelayState*,2>,128> delays{};
    NativeMidiSendDomain* sends;
    MidiNoteKernel* notes=nullptr;
    NativeMidiState* journal=nullptr;
    MidiCCSemanticState* semantics=nullptr;
    // Optional FIFO producer ownership. Prepared control-side, never allocate
    // or call Python in the emitter. Credit covers one original compound;
    // it survives prefix pressure, retained unlock and exact frame resume.
    NativeMidiState* failureOwner=nullptr;
    unsigned long long failureToken=0;
    bool failureRecordPending=false;
    // Explicit compatibility admission, disabled until callback-state handoff
    // is qualified. An arbitrary mutable callback must observe its failed
    // action BEFORE any suffix audio; immutable error records alone cannot
    // reconstruct already-rendered PCM or overwritten global prefix state.
    bool failureFenceEnabled=false,failureFencePending=false;
    bool deferredFailureLog=false;
    // FILE admission observes a callback mode published at the actor fence,
    // without acquiring kernel.lock under the ingress admission lock.
    std::atomic<bool> compatibilityCallback{false};
    unsigned failureFenceStream=0;
    unsigned long long failureFenceSerial=0;
    std::array<unsigned,6> failureRecord{}; // channel, low/high/type IDs, DWORD, type.
    bool failureRowsPending()const{
        auto* owner=semantics?semantics->journal:nullptr;if(!owner)return false;
        AcquireSRWLockShared(&owner->failureLock);
        const bool pending=owner->failureProduced!=owner->failureConsumed||owner->failureReaders.load();
        ReleaseSRWLockShared(&owner->failureLock);return pending;
    }
    bool releaseFailureCredit(){
        if(failureRecordPending)return false;
        if(failureToken&&!midi_state_failure_release(failureOwner,failureToken))return false;
        failureToken=0;failureOwner=nullptr;return true;
    }
    NativeMidiState* manualJournal=nullptr;
    NativeSelectionState* selection=nullptr;
    void* genericConversion=nullptr;unsigned genericRate=0,genericFaultStream=0;int genericFault=0;
    // Borrowed only while the original native actor exists; cleared after join.
    // A resource-failed selection observation may wait without losing its
    // original stack/wire/cursor, but must obey explicit producer cancellation.
    std::atomic<bool>* executionStop=nullptr;
    std::array<std::array<unsigned,4>,10> genericDelayPresets{};
    std::array<unsigned,128> genericDelayTimes{},genericDelayFeedback{};
    bool genericReady=false;
    unsigned long long semanticRetiredFailures=0;
    unsigned pendingSemanticNext=0,pendingSemanticEnd=0,pendingSemanticChannel=0,pendingSemanticValue=0;
    // Semantic continuation is NOT BASS ChannelLock ownership. Keep a last
    // route active until its retained unlock/receipt has actually completed.
    bool semanticActive=false,pendingSemanticResetSeed=false;
    unsigned pendingSemanticStream=0;
    bool semanticFramePending=false,semanticFrameEnd=false,semanticFrameDrumRestore=false;
    std::array<int,32> semanticFrameResetTable{};unsigned semanticFrameResetMask=7,semanticFrameResetUsed=0;
    std::array<unsigned,120> auxSlots{};
    std::array<unsigned,2> requestSlots{};
    std::array<unsigned,3> acceptedSlots{};
    bool pendingClockLease=false;
    unsigned pendingModeFlags=0,pendingModePort=0;
    std::array<MidiNoteAction,4096> compoundActions{};unsigned compoundCount=0;
    std::array<std::array<unsigned long long,12>,256> captures{};unsigned captureHead=0,captureCount=0;unsigned long long captureSerial=0;
    // Capture rows are semantic receipts, not audio. When enabled, this
    // control-side-prepared arena moves the old 256-row producer prefix out of
    // the callback-facing FIFO. Python still ACKs the same serials in order;
    // no row is coalesced or auto-committed.
    std::vector<std::array<unsigned long long,12>> retainedCaptures;
    unsigned retainedCaptureHead=0,retainedCaptureCount=0;bool retainedCaptureEnabled=false;
    CONDITION_VARIABLE captureChanged=CONDITION_VARIABLE_INIT;HANDLE captureThread=nullptr;
    bool captureClosing=false,captureGrowthRequested=false;
    unsigned long long captureGrowthRevision=0,captureGrowthObserved=0,captureAllocationFailures=0;
    void* firstuse=nullptr;unsigned long long fontEpoch=0,fontGeneration=0;unsigned fontStream=0,fontMode=0;
    std::array<unsigned,512> fontRequests{};
    bool resetReady=false;
    std::array<bool,128> resetAdmitted{},resetDrums{};
    unsigned long long resetLearnRevision=0;
    unsigned resetArmChannel=UINT_MAX;
    struct GMEvent {unsigned event,raw,flags;};
    std::vector<GMEvent> gmDefaults,gmMasters;
    bool gmReady=false,gmActive=false,gmGS=true,gmDelay=false,gmExtensions=false,manualResetReady=false;
    unsigned gmIgnoreModes=0; // bit0 GM1,bit1 GM2,bit2 XG; GS owns gmGS for legacy ABI.
    bool fileResetReady=false;unsigned fileResetSubphase=0,fileOriginClearSlot=256,fileResetSource=31;
    struct FileExpression {unsigned channel,field;};
    std::vector<FileExpression> fileExpressionOrder;
    std::vector<std::vector<unsigned>> fileParameterTables;
    unsigned gmChannels=0,gmDefaultReverb=40,gmFrames=0,gmPhase=0,gmChannel=0,gmKind=0;
    unsigned gmStream=0,gmMode=0,gmPort=0,gmSystemMode=0;bool gmSystemModePending=false;std::array<unsigned long long,7> gmOrigin{};
    int gmFatal=0,gmError=0;
    std::vector<unsigned> gmSelection,gmSendChannels; // Distinct source address/accessor orders.
    std::array<std::array<unsigned,4>,128> resetAddresses{};bool resetAddressesReady=false;
    bool pendingGMSemantic=false;
    bool pendingResetScalar=false;
    std::array<unsigned,16> resetOrder{};unsigned resetOrderCount=0,resetOrderNext=0;
    bool resetTailReady=false,resetTailExtensions=false;unsigned resetTailPhase=0,resetTailStream=0;
    int resetTailFault=0,resetTailError=0;
    void* executionOwner=nullptr;
    MidiExecutionIdle* idle=nullptr; // Borrowed only while the execution owner exists.
    std::atomic<unsigned> executionThread{0};
    bool executionPaused=false;
    // Actual explicit pipe stop only; never synthesize a GM/event failure.
    // kind1=input/CC121,2=incoming system Reset,3=manual Reset,4=idle Reset.
    unsigned cancelledStream=0,cancelledKind=0;unsigned long long cancelledReceipt=0;
    MidiCCSemanticState* cancelledGeneration=nullptr; // Retained until exact cancellation ACK.
    DWORD(WINAPI* rawEmit)(HSTREAM,DWORD,const void*,DWORD);
    NativeRawDecodeContext* rawContext=nullptr;
    DWORD(WINAPI* channelFlags)(DWORD,DWORD,DWORD)=nullptr;
    BOOL(WINAPI* selectionQueueReader)(DWORD,DWORD,float*)=nullptr;
    bool selectionSettlementControlOnly=false;
    void* emitAddress;void* lockAddress;void* errorAddress;
    MidiCCKernel(unsigned count,NativeMidiSendDomain* domain,void* raw,void* emit,void* barrier,void* error):
        channels(count),sends(domain),rawEmit(reinterpret_cast<DWORD(WINAPI*)(HSTREAM,DWORD,const void*,DWORD)>(raw)),retainedCaptures(4096),
        emitAddress(emit),lockAddress(barrier),errorAddress(error){sends->retain();}
    ~MidiCCKernel(){
        if(captureThread){AcquireSRWLockExclusive(&lock);captureClosing=true;WakeAllConditionVariable(&captureChanged);ReleaseSRWLockExclusive(&lock);WaitForSingleObject(captureThread,INFINITE);CloseHandle(captureThread);}
        releaseFailureCredit();for(auto& pair:delays)for(auto* tap:pair)if(tap)delayMidiRelease(tap);if(notes)notes->release();if(journal)journal->release();if(semantics)semantics->release();if(manualJournal)manualJournal->release();if(selection)selection->release();if(firstuse)font_preload_plan_release(firstuse);if(rawContext)rawContext->release();sends->release();}
    static unsigned __stdcall growCaptureStorage(void* pointer){
        auto& s=*static_cast<MidiCCKernel*>(pointer);
        for(;;){
            AcquireSRWLockExclusive(&s.lock);
            while(!s.captureGrowthRequested&&!s.captureClosing)SleepConditionVariableSRW(&s.captureChanged,&s.lock,INFINITE,0);
            if(s.captureClosing){ReleaseSRWLockExclusive(&s.lock);return 0;}
            const auto oldSize=s.retainedCaptures.size();ReleaseSRWLockExclusive(&s.lock);
            std::vector<std::array<unsigned long long,12>> staged;bool allocated=false;
            try{if(oldSize<=UINT_MAX/24u){staged.resize(oldSize*2);allocated=true;}}catch(const std::bad_alloc&){}
            AcquireSRWLockExclusive(&s.lock);
            if(allocated&&s.retainedCaptures.size()==oldSize){
                for(unsigned i=0;i<s.retainedCaptureCount;++i)staged[i]=s.retainedCaptures[(s.retainedCaptureHead+i)%static_cast<unsigned>(oldSize)];
                s.retainedCaptures.swap(staged);s.retainedCaptureHead=0;++s.captureGrowthRevision;
            }else if(!allocated)++s.captureAllocationFailures;
            s.captureGrowthRequested=false;WakeAllConditionVariable(&s.captureChanged);ReleaseSRWLockExclusive(&s.lock);
            // staged releases the old storage outside every kernel/audio lock.
        }
    }
    unsigned slot(unsigned event)const{
        for(unsigned i=0;i<journal->eventCount;++i)if(journal->eventTypes[i]==event)return i;return 256;
    }
};
static bool midiCCCaptureCapacity(MidiCCKernel& s,unsigned needed){
    if(!s.retainedCaptureEnabled)return needed<=s.captures.size()-s.captureCount;
    if(s.retainedCaptureCount+s.captureCount>s.retainedCaptures.size()||needed>s.retainedCaptures.size()-s.retainedCaptureCount-s.captureCount){
        s.captureGrowthRequested=true;WakeAllConditionVariable(&s.captureChanged);return false;
    }return true;
}
static bool midiCCCaptureEnsureRoom(MidiCCKernel& s,unsigned needed=1){
    if(!midiCCCaptureCapacity(s,needed))return false;
    if(needed<=static_cast<unsigned>(s.captures.size())-s.captureCount)return true;
    if(!s.retainedCaptureEnabled)return false;
    if(s.retainedCaptureCount+s.captureCount>s.retainedCaptures.size())return false;
    for(unsigned i=0;i<s.captureCount;++i)
        s.retainedCaptures[(s.retainedCaptureHead+s.retainedCaptureCount+i)%static_cast<unsigned>(s.retainedCaptures.size())]=
            s.captures[(s.captureHead+i)%static_cast<unsigned>(s.captures.size())];
    s.retainedCaptureCount+=s.captureCount;s.captureHead=0;s.captureCount=0;return true;
}
static unsigned midiCCCaptureTotal(const MidiCCKernel& s){return s.retainedCaptureCount+s.captureCount;}
// The native worker may wait for native allocator/storage progress, never for
// Python semantic ACK. OOM falls back to the existing lossless pressure owner.
static bool midiCCCaptureGrowthRetry(MidiCCKernel& s){
    AcquireSRWLockExclusive(&s.lock);
    while(s.captureGrowthRequested&&!s.captureClosing)SleepConditionVariableSRW(&s.captureChanged,&s.lock,INFINITE,0);
    const bool progressed=s.captureGrowthRevision!=s.captureGrowthObserved;
    s.captureGrowthObserved=s.captureGrowthRevision;ReleaseSRWLockExclusive(&s.lock);return progressed;
}
// A pressure refusal has released kernel/BASS mutation leases. Allocate on the
// selection's native staging thread, then publish on this SAME original actor.
// Return-2 retains the actual unlock owner and displaced buffers for retry_ex.
static int midiCCSelectionGrowthRetry(MidiCCKernel& kernel,unsigned stream){
    AcquireSRWLockShared(&kernel.lock);auto* state=kernel.selection;
    if(!state||!state->capacityThread){ReleaseSRWLockShared(&kernel.lock);return 0;}
    // A whole Program/Bank frame also owns ordered ASYNC identities. Their
    // ring can fill before core/checkpoint rows; grow both admission domains.
    AcquireSRWLockExclusive(&state->ticketsLock);state->trimTickets();
    const bool identityFull=state->identityProduced-state->identityConsumed>=state->identityLimit;
    const unsigned events=identityFull?std::max(1u,state->identityLimit):0u;
    const bool ticketBlocked=state->ticketCount==state->tickets.size();
    ReleaseSRWLockExclusive(&state->ticketsLock);
    if(ticketBlocked||(!identityFull&&state->room(state->need()))){ReleaseSRWLockShared(&kernel.lock);return 0;}
    state->retain();ReleaseSRWLockShared(&kernel.lock);
    struct Lease{NativeSelectionState* state;~Lease(){state->release();}}lease{state};
    const unsigned extra=std::max(state->need(),state->checkpointArena?state->checkpointArena->capacity:state->recordLimit.load());
    NativeSelectionState::StoragePreparation prepared;if(!state->stageCapacityOwned(events,extra,prepared))return 0;
    AcquireSRWLockExclusive(&kernel.lock);
    if(kernel.selection!=state||kernel.pendingStream){ReleaseSRWLockExclusive(&kernel.lock);return 0;}
    auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(kernel.lockAddress);
    if(!lock(stream,TRUE)){ReleaseSRWLockExclusive(&kernel.lock);return 0;}
    const bool accepted=state->publishCapacity(events,extra,prepared);
    if(!lock(stream,FALSE)){kernel.pendingStream=stream;kernel.pendingThread=GetCurrentThreadId();state->retainPreparation(std::move(prepared));ReleaseSRWLockExclusive(&kernel.lock);return -2;}
    ReleaseSRWLockExclusive(&kernel.lock);return accepted?1:0;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_raw_context(void* pointer,void* context){
    if(!pointer)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);auto* next=static_cast<NativeRawDecodeContext*>(context);
    AcquireSRWLockExclusive(&s.lock);
    if(s.cancelledStream||s.frameReceipt||s.pendingStream||s.semanticActive||s.semanticFramePending||(s.executionOwner&&!s.executionPaused)||(next&&s.journal&&next->stream!=s.journal->stream)){ReleaseSRWLockExclusive(&s.lock);return 0;}
    if(next)next->retain();auto* previous=s.rawContext;s.rawContext=next;if(previous)previous->release();ReleaseSRWLockExclusive(&s.lock);return 1;
}
static void midiCCObserveResetResult(MidiCCKernel& kernel,unsigned stream,unsigned event,unsigned channel,unsigned param,bool accepted,int error){
    if(!kernel.rawContext||event!=MIDI_EVENT_RESET||!accepted)return;
    const DWORD flags=kernel.channelFlags?kernel.channelFlags(stream,0,0):UINT_MAX;
    if(flags==UINT_MAX||(flags&BASS_MIDI_ASYNC)){
        // StreamEvent does not trigger MIDI_EVENT syncs. Acceptance on an
        // ASYNC stream is not evidence that its selector-reset has processed.
        // Retain uncertainty instead of claiming a premature null selector.
        raw_decode_context_pending_reset(kernel.rawContext,stream,channel,1u,error);return;
    }
    raw_decode_context_event(kernel.rawContext,stream,event,channel,param,1u,error);
}
extern "C" __declspec(dllexport) int __cdecl midi_note_kernel_raw_context(void* pointer,void* context){
    if(!pointer)return 0;auto& s=*static_cast<MidiNoteKernel*>(pointer);auto* next=static_cast<NativeRawDecodeContext*>(context);
    AcquireSRWLockExclusive(&s.lock);if(s.executionOwner&&!s.executionPaused){ReleaseSRWLockExclusive(&s.lock);return 0;}
    if(next)next->retain();auto* previous=s.rawContext;s.rawContext=next;if(previous)previous->release();ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_selection(void* pointer,void* prepared){
    if(!pointer)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);auto* selection=static_cast<NativeSelectionState*>(prepared);AcquireSRWLockExclusive(&s.lock);
    if(s.cancelledStream||s.frameReceipt||s.pendingStream||s.semanticActive||s.semanticFramePending||(s.executionOwner&&!s.executionPaused)||(selection&&(!s.journal||selection->clock!=s.journal->clock||selection->stream!=s.journal->stream))){ReleaseSRWLockExclusive(&s.lock);return 0;}
    if(selection)selection->retain();auto* previous=s.selection;s.selection=selection;if(previous)previous->release();ReleaseSRWLockExclusive(&s.lock);return 1;
}
// bass.h DWORD WINAPI BASS_ChannelFlags(DWORD,DWORD,DWORD). Optional additive
// observation, not a mutation of the original StreamEvents mode/stream flags.
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_channel_flags(void* pointer,void* callback){
    if(!pointer||!callback)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.cancelledStream||s.frameReceipt||s.pendingStream||s.semanticActive||s.semanticFramePending||(s.executionOwner&&!s.executionPaused)){ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.channelFlags=reinterpret_cast<DWORD(WINAPI*)(DWORD,DWORD,DWORD)>(callback);ReleaseSRWLockExclusive(&s.lock);return 1;
}
// bass.h BASS_ChannelGetAttribute and bassmidi.h QUEUED_* read-only domains.
// Prepared before actor start; no pointer changes while a worker owns it.
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_selection_queue_reader(void* pointer,void* callback){
    if(!pointer||!callback)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.executionOwner||s.pendingStream){ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.selectionQueueReader=reinterpret_cast<BOOL(WINAPI*)(DWORD,DWORD,float*)>(callback);ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_selection_settlement_phase(void* pointer,unsigned* values,unsigned capacity){
    if(!pointer||!values||capacity<5)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockShared(&s.lock);
    auto* state=s.selection;values[0]=s.executionOwner&&s.selectionQueueReader&&!s.compatibilityCallback&&!s.selectionSettlementControlOnly?1u:0u;
    values[1]=s.executionPaused?1u:0u;values[2]=s.executionThread.load();
    values[3]=state&&state->settleLocked.load()?1u:0u;values[4]=state?state->settleThread.load():0u;
    ReleaseSRWLockShared(&s.lock);return 5;
}
// Candidate full-reset phase primitive, not an admitted GM/SysEx executor.
// Source call/stream lock boundaries remain per event; no blanket BASS lock.
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_selection_reset_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_selection_reset(void* pointer,unsigned channel,unsigned stage,unsigned long long* result){
    if(result)std::fill_n(result,4,0ull);
    if(!pointer||!result||channel>=128||stage>4)return -1;
    auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.cancelledStream||s.frameReceipt||s.pendingStream||s.semanticActive||s.semanticFramePending||(s.executionOwner&&!s.executionPaused&&s.executionThread.load()!=GetCurrentThreadId())||!s.selection){ReleaseSRWLockExclusive(&s.lock);return -3;}
    auto& state=*s.selection;const unsigned budget=state.need();
    // This is a direct reset phase, never a future RAW callback ticket.
    // Reserve outside queuedReservation so an ASYNC selection cannot claim
    // this phase's budget during publication.
    auto prior=state.reserved.load(std::memory_order_acquire);
    for(;;){if(state.produced.load(std::memory_order_acquire)-state.consumed.load(std::memory_order_acquire)+prior+budget>state.recordLimit.load(std::memory_order_acquire)||!state.room(budget)){ReleaseSRWLockExclusive(&s.lock);return 0;}
        if(state.reserved.compare_exchange_weak(prior,prior+budget,std::memory_order_acq_rel))break;}
    result[2]=state.produced.load(std::memory_order_acquire);state.clock->writers.fetch_add(1,std::memory_order_acq_rel);result[0]=1;
    bool completed=true;
    if(stage==0)state.resetSession(channel);
    else if(stage==1)state.resetRouting(channel);
    else if(stage==2)completed=state.resetRefresh(channel);
    else if(stage==3)completed=state.resetApply(channel);
    else completed=state.syncGraph();
    result[1]=completed?1ull:0ull;result[3]=state.produced.load(std::memory_order_acquire);
    state.clock->writers.fetch_sub(1,std::memory_order_release);state.reserved.fetch_sub(budget,std::memory_order_release);
    ReleaseSRWLockExclusive(&s.lock);return completed?1:-2;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_firstuse_version(){return 1;}
// Prepared compound reset. Defaults are immutable; preset/profile resolution
// remains live in the existing selection state at the original source phase.
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_gm_version(){return 2;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_reset_address_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_reset_tail_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_reset_tail_prepare(void* pointer,unsigned flags){
    if(!pointer||flags>3)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.cancelledStream||s.frameReceipt||s.pendingStream||s.semanticActive||s.semanticFramePending||s.resetOrderNext<s.resetOrderCount||(s.executionOwner&&!s.executionPaused)||
       ((flags&1u)&&(!s.selection||!s.semantics||!s.resetReady||!s.resetAddressesReady||!s.notes))){ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.resetTailReady=(flags&1u)!=0;s.resetTailExtensions=(flags&2u)!=0;s.resetTailPhase=s.resetTailStream=0;s.resetTailFault=s.resetTailError=0;
    ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_reset_addresses(void* pointer,const unsigned* rows,unsigned count){
    if(!pointer||!rows||count!=512)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.cancelledStream||s.frameReceipt||s.gmActive||s.pendingStream||s.semanticActive||s.semanticFramePending||(s.executionOwner&&!s.executionPaused)){ReleaseSRWLockExclusive(&s.lock);return 0;}
    for(unsigned ch=0;ch<128;++ch){const auto* row=rows+ch*4;if(row[0]>7||row[1]>128||row[2]>127||row[3]>127){ReleaseSRWLockExclusive(&s.lock);return 0;}}
    for(unsigned ch=0;ch<128;++ch)std::copy_n(rows+ch*4,4,s.resetAddresses[ch].begin());s.resetAddressesReady=true;
    ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_gm_prepare(void* pointer,const unsigned* channels,unsigned count,
        const unsigned* defaults,unsigned defaultCount,const unsigned* masters,unsigned masterCount,unsigned reverb,unsigned flags,unsigned frames,
        const unsigned* sendChannels,unsigned sendCount){
    if(!pointer||!channels||!count||count>128||!defaults||!defaultCount||defaultCount>40||masterCount>2||(masterCount&&!masters)||reverb>127||flags>63||!sendChannels||!sendCount||sendCount>128)return 0;
    auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.cancelledStream||s.frameReceipt||s.gmActive||s.pendingStream||s.semanticActive||s.semanticFramePending||(s.executionOwner&&!s.executionPaused)||!s.selection||!s.notes||!s.semantics||!s.resetReady){ReleaseSRWLockExclusive(&s.lock);return 0;}
    std::array<bool,128> seen{};
    for(unsigned i=0;i<count;++i)if(channels[i]>=s.channels||seen[channels[i]]){ReleaseSRWLockExclusive(&s.lock);return 0;}else seen[channels[i]]=true;
    seen.fill(false);for(unsigned i=0;i<sendCount;++i)if(sendChannels[i]>=s.channels||seen[sendChannels[i]]){ReleaseSRWLockExclusive(&s.lock);return 0;}else seen[sendChannels[i]]=true;
    if(flags&2u)for(const auto& pair:s.delays)for(auto* tap:pair)if(tap&&(!frames||frames>tap->capacity)){ReleaseSRWLockExclusive(&s.lock);return 0;}
    try{s.gmSelection.assign(channels,channels+count);s.gmSendChannels.assign(sendChannels,sendChannels+sendCount);s.gmDefaults.clear();s.gmMasters.clear();
        // Live master commands may add either prepared reset baseline without
        // allocating on the MIDI actor. This bounded domain is fine/coarse.
        s.gmMasters.reserve(2);
        for(unsigned i=0;i<defaultCount;++i)s.gmDefaults.push_back({defaults[i*3],defaults[i*3+1],defaults[i*3+2]});
        for(unsigned i=0;i<masterCount;++i)s.gmMasters.push_back({masters[i*2],masters[i*2+1],0});
        s.gmDefaultReverb=reverb;s.gmGS=(flags&1u)!=0;s.gmIgnoreModes=flags>>3;s.gmDelay=(flags&2u)!=0;s.gmExtensions=(flags&4u)!=0;s.gmFrames=frames;s.gmChannels=count;s.gmReady=true;s.gmFatal=s.gmError=0;
    }catch(const std::bad_alloc&){s.gmReady=false;ReleaseSRWLockExclusive(&s.lock);return 0;}
    ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_firstuse(void* pointer,void* plan,unsigned long long epoch,
        unsigned long long generation,unsigned stream,unsigned mode,const unsigned* requests,unsigned count){
    if(!pointer||(plan&&(!requests||count!=512||!stream||mode>2)))return 0;
    auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.cancelledStream||s.frameReceipt||s.pendingStream||s.semanticActive||s.semanticFramePending||(s.executionOwner&&!s.executionPaused)){ReleaseSRWLockExclusive(&s.lock);return 0;}
    if(plan&&(!s.journal||s.journal->stream!=stream||!font_preload_plan_acquire(plan,epoch,generation,stream,mode))){ReleaseSRWLockExclusive(&s.lock);return 0;}
    auto* previous=s.firstuse;s.firstuse=plan;s.fontEpoch=epoch;s.fontGeneration=generation;s.fontStream=stream;s.fontMode=mode;
    if(plan)std::copy_n(requests,512,s.fontRequests.begin());if(previous)font_preload_plan_release(previous);
    ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_semantics(void* pointer,void* prepared){
    if(!pointer)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);auto* generation=static_cast<MidiCCSemanticState*>(prepared);
    AcquireSRWLockExclusive(&s.lock);
    if(s.failureToken||s.failureRecordPending){ReleaseSRWLockExclusive(&s.lock);return 0;}
    if(s.cancelledStream||s.frameReceipt||s.pendingStream||s.semanticActive||s.semanticFramePending||(s.executionOwner&&!s.executionPaused)||(generation&&(!s.journal||generation->journal->clock!=s.journal->clock||generation->journal->stream!=s.journal->stream))){ReleaseSRWLockExclusive(&s.lock);return 0;}
    if(generation)generation->retain();auto* previous=s.semantics;s.semantics=generation;if(previous){s.semanticRetiredFailures+=previous->failures.load();previous->release();}
    ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_manual_journal(void* pointer,void* state){
    if(!pointer||!state)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);auto* journal=static_cast<NativeMidiState*>(state);
    AcquireSRWLockExclusive(&s.lock);
    if(s.manualJournal||s.pendingStream||s.executionOwner||!s.journal||journal->clock!=s.journal->clock||journal->stream!=s.journal->stream){ReleaseSRWLockExclusive(&s.lock);return 0;}
    journal->retain();s.manualJournal=journal;ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_semantic_configure(void* pointer,void* prepared,unsigned options,const unsigned char* blocked,unsigned count){
    if(!pointer||options>3||!blocked||count!=128*128)return 0;
    auto& s=*static_cast<MidiCCKernel*>(pointer);auto* generation=static_cast<MidiCCSemanticState*>(prepared);
    AcquireSRWLockExclusive(&s.lock);
    if(s.failureToken||s.failureRecordPending){ReleaseSRWLockExclusive(&s.lock);return 0;}
    if(s.cancelledStream||s.frameReceipt||s.pendingStream||s.semanticActive||s.semanticFramePending||(s.executionOwner&&!s.executionPaused)||(generation&&(!s.journal||generation->journal->clock!=s.journal->clock||generation->journal->stream!=s.journal->stream))){ReleaseSRWLockExclusive(&s.lock);return 0;}
    if(generation)generation->retain();auto* previous=s.semantics;s.semantics=generation;
    s.options=options;std::copy_n(blocked,count,s.blocked.begin());if(previous){s.semanticRetiredFailures+=previous->failures.load();previous->release();}
    ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_cc_kernel_semantic_failures(void* pointer){
    if(!pointer)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockShared(&s.lock);
    const auto count=s.semanticRetiredFailures+(s.semantics?s.semantics->failures.load():0);ReleaseSRWLockShared(&s.lock);return count;
}
// Own producer ABI, not a new BASS event. Default unprepared journals retain
// their old ABI. This fixes error multiplicity, NOT the global failure-prefix
// callback state contract; normal product activation remains unqualified.
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_semantic_failure_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_failure_fence_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_failure_fence_configure(void* pointer,unsigned enabled){
    if(!pointer||enabled>1)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);
    AcquireSRWLockExclusive(&s.lock);
    if(s.executionOwner||s.frameReceipt||s.pendingStream||s.semanticActive||s.semanticFramePending||s.gmActive||
            s.resetTailStream||s.failureToken||s.failureRecordPending||s.failureFencePending||
            (enabled&&(!s.semantics||!s.semantics->journal->failureCapacity))){ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.failureFenceEnabled=enabled!=0;ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_failure_fence_snapshot(void* pointer,unsigned long long* values,unsigned capacity){
    if(!pointer||!values||capacity!=4)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);
    AcquireSRWLockShared(&s.lock);values[0]=s.failureFencePending?1u:0u;values[1]=s.failureFenceStream;
    values[2]=s.failureFenceSerial;values[3]=s.frameReceipt;ReleaseSRWLockShared(&s.lock);return 4;
}
// Direct-call component acknowledgement ONLY. A live execution owner needs a
// separate exact delivered-ticket state/command handoff, not this shortcut.
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_failure_fence_ack(void* pointer,unsigned stream,
        unsigned long long serial,unsigned long long receipt){
    if(!pointer||!serial)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.executionOwner||!s.failureFencePending||s.failureFenceStream!=stream||s.failureFenceSerial!=serial||
            s.frameReceipt!=receipt||s.pendingStream||s.failureRecordPending||
            !s.failureOwner){
        ReleaseSRWLockExclusive(&s.lock);return 0;}
    AcquireSRWLockShared(&s.failureOwner->failureLock);
    const bool drained=s.failureOwner->failureProduced==s.failureOwner->failureConsumed&&!s.failureOwner->failureReaders.load();
    ReleaseSRWLockShared(&s.failureOwner->failureLock);
    if(!drained){ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.failureFencePending=false;s.failureFenceStream=0;s.failureFenceSerial=0;
    ReleaseSRWLockExclusive(&s.lock);return 1;
}
static unsigned midiCCFailureBudget(const MidiCCSemanticState& state,unsigned begin,unsigned end){
    unsigned budget=0;
    for(unsigned i=begin;i<end;++i){const auto& route=state.route(i);
        if((route.flags&32u)||(route.flags&24u)||((route.flags&2u)&&route.event&&route.event!=MIDI_EVENT_USERFX))++budget;
    }return budget;
}
static bool midiCCFailureGrowthRetry(MidiCCKernel& s){
    AcquireSRWLockExclusive(&s.lock);auto* owner=s.semantics?s.semantics->journal:nullptr;
    if(owner)owner->retain();ReleaseSRWLockExclusive(&s.lock);if(!owner)return false;
    AcquireSRWLockExclusive(&owner->failureLock);
    while(owner->failureGrowthRequested&&!owner->failureClosing)SleepConditionVariableSRW(&owner->failureChanged,&owner->failureLock,INFINITE,0);
    const bool progressed=owner->failureGrowthRevision!=owner->failureGrowthObserved;
    owner->failureGrowthObserved=owner->failureGrowthRevision;ReleaseSRWLockExclusive(&owner->failureLock);owner->release();return progressed;
}
static int midiCCReserveFailures(MidiCCKernel& s,unsigned long long budget){
    auto* owner=s.semantics?s.semantics->journal:nullptr;
    if(!owner||!owner->failureCapacity)return 1;
    if(s.failureToken)return s.failureOwner==owner?1:-1;
    if(s.failureRecordPending)return -1;
    if(!budget)return 1;
    // Impossible WHOLE operation is pristine unsupported; occupied capacity
    // is temporary pressure. Neither permits an audio prefix or silent drop.
    if(budget>owner->failureCapacity)return owner->requestFailureCapacity(budget)?-6:0;
    const auto token=midi_state_failure_reserve(owner,static_cast<unsigned>(budget));
    if(!token){owner->requestFailureCapacity(budget);return -6;}
    s.failureOwner=owner;s.failureToken=token;return 1;
}
static int midiCCPendingFailure(MidiCCKernel& s){
    if(!s.failureRecordPending)return 1;
    const auto& row=s.failureRecord;
    const int stored=midi_state_failure_push_reserved(s.failureOwner,s.failureToken,row[0],row[1],row[2],row[3],row[4],row[5]);
    if(stored==1){
        s.failureRecordPending=false;
        if(s.failureFenceEnabled){
            s.failureFencePending=true;s.failureFenceStream=s.failureOwner->stream;
            s.failureFenceSerial=s.failureOwner->clock->serial.load(std::memory_order_acquire);
            return -6;
        }
        return 1;
    }
    return stored<0?-1:-6; // Metadata-only retry; audio was already attempted.
}
static int midiCCRecordFailure(MidiCCKernel& s,unsigned channel,unsigned low,unsigned high,unsigned kind,int code,unsigned type){
    auto& state=*s.semantics;
    if(!state.journal->failureCapacity){
        state.journal->receive(state.journal->stream,state.slot(low),(channel<<16)|(static_cast<unsigned>(code)&65535u));
        state.journal->receive(state.journal->stream,state.slot(high),(channel<<16)|(static_cast<unsigned>(code)>>16));
        if(kind)state.journal->receive(state.journal->stream,state.slot(kind),(channel<<16)|type);
        return 1;
    }
    if(!s.failureToken||s.failureOwner!=state.journal||s.failureRecordPending)return -1;
    s.failureRecord={channel,low,high,kind,static_cast<unsigned>(code),type};
    s.failureRecordPending=true;return midiCCPendingFailure(s);
}
static unsigned long long midiCCResetFailureBudget(const MidiCCKernel& s){
    if(!s.semantics)return 0;unsigned long long budget=0;
    for(unsigned i=0;i<s.gmChannels;++i){const unsigned channel=s.gmSelection[i],first=s.semantics->resetFirst[channel];
        budget+=midiCCFailureBudget(*s.semantics,first,first+s.semantics->resetCount[channel]);
    }return budget;
}
// Additive pre-RAW CC121 preparation. The mask denotes an empty application
// post-RAW default pass, not the documented native controller-reset domain.
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_reset_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_reset_configure(void* pointer,const unsigned* values,unsigned count,unsigned long long revision){
    if(!pointer||!values||count!=384)return 0;
    for(unsigned i=0;i<count;++i)if(values[i]>1)return 0;
    auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.cancelledStream||s.frameReceipt||s.pendingStream||s.semanticActive||s.semanticFramePending||(s.executionOwner&&!s.executionPaused)||midiCCCaptureTotal(s)||!s.journal||s.slot(0xb0000u+MIDI_EVENT_USERFX)==256){ReleaseSRWLockExclusive(&s.lock);return 0;}
    unsigned arm=UINT_MAX;
    for(unsigned ch=0;ch<128;++ch)if(values[ch*3+2]){if(arm!=UINT_MAX){ReleaseSRWLockExclusive(&s.lock);return 0;}arm=ch;}
    for(unsigned ch=0;ch<128;++ch){s.resetAdmitted[ch]=values[ch*3]!=0;s.resetDrums[ch]=values[ch*3+1]!=0;}
    s.resetArmChannel=arm;
    s.resetLearnRevision=revision;s.resetReady=true;ReleaseSRWLockExclusive(&s.lock);return 1;
}
// Returns -2 only with same-thread BASS ownership retained. The continuation
// starts AFTER the attempted route; unlock retries never resend its audio.
struct MidiCCCompoundContext {unsigned mode,port;MidiNoteAction* actions;unsigned capacity;unsigned long long* count;};
struct MidiCCModeResult {unsigned selected=0,changed=0,accepted=1,raw=0;int error=0;};
static int midiCCModeApply(MidiCCKernel& s,unsigned stream,unsigned channel,const MidiCCSemanticRoute& route,
        unsigned value,MidiCCCompoundContext* context,MidiCCModeResult* result=nullptr,bool manual=false){
    auto& state=*s.semantics;if(!state.modeReady||!s.notes)return 0;
    MidiActivityLease activityLease(s.notes,nullptr,0,0,1,&s.pendingStream);
    if(!activityLease.ready)return -6;
    const int saved=state.modePresent[channel]?state.modeSaved[channel]:0;
    const unsigned observed=state.get(stream,channel,MIDI_EVENT_MODE);
    const int current=observed==UINT_MAX?saved:static_cast<int>(std::min(observed,2u));
    const bool enabled=route.table[value].amount!=0;const unsigned kind=(route.flags>>3)&3u;
    const int selected=kind==3?(enabled?2:current==2?1:current):kind==1?(enabled?(current?current:1):0):enabled?0:(current?current:1);
    if(result){result->selected=static_cast<unsigned>(selected);result->changed=selected!=current;result->raw=selected!=2;}
    if(selected==current)return 1;
    auto save=[&](){state.modePresent[channel]=selected!=0;state.modeSaved[channel]=selected;
        state.journal->receive(stream,state.modeSlots[0],(channel<<16)|static_cast<unsigned>(selected)|(selected?0x8000u:0u));};
    auto error=reinterpret_cast<int(WINAPI*)()>(s.errorAddress);
    int failureStatus=1;
    auto failure=[&](int code,unsigned type){state.failures.fetch_add(1,std::memory_order_relaxed);
        if(!manual){failureStatus=midiCCRecordFailure(s,channel,0x160000u,0x170000u,0x180000u,code,type);return;}
        // Explicit GUI manual commands already own immutable result records.
        // Their queue/ACK cutover is separate from this semantic producer.
        state.journal->receive(stream,state.modeSlots[1+1],(channel<<16)|(static_cast<unsigned>(code)&65535u));
        state.journal->receive(stream,state.modeSlots[3],(channel<<16)|(static_cast<unsigned>(code)>>16));
        state.journal->receive(stream,state.modeSlots[4],(channel<<16)|type);};
    if(selected!=2&&!s.notes->humanization.cancel(stream,channel))return -6;
    if(selected==2){
        // Existing saved-expression setter writes before the BOOL result.
        save();auto emit=reinterpret_cast<BOOL(WINAPI*)(HSTREAM,DWORD,DWORD,DWORD)>(s.emitAddress);
        const bool accepted=emit(stream,channel,MIDI_EVENT_MODE,2)!=FALSE;
        const int code=accepted?0:error();
        if(result){result->accepted=accepted?1u:0u;result->error=code;}
        if(accepted){if(!manual)state.journal->receive(stream,state.modeSlots[1],(channel<<16)|2u);}
        else failure(code,2);return failureStatus;
    }
    // These markers clear only overlap/current notes/activity; played history
    // survives. A generated mode command is not a new Note/jitter origin.
    MidiNoteAction* action=nullptr;
    if(context){if(*context->count>=context->capacity)return -6;action=context->actions+(*context->count)++;}
    else{if(s.compoundCount==s.compoundActions.size())return -6;action=&s.compoundActions[s.compoundCount++];}
    const unsigned cc=selected==1?126u:127u;LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);
    *action={0xb0u|(channel&15u)|(cc<<8),channel,0,static_cast<unsigned long long>(tick.QuadPart)};
    if(s.notes->activity)s.notes->observeActivity(*action,{false,0,0});
    std::fill_n(s.notes->counts.begin()+channel*128,128,0ull);
    const unsigned flags=context?context->mode:s.pendingModeFlags,port=context?context->port:s.pendingModePort;
    unsigned char wire[]{0xff,0x21,1,static_cast<unsigned char>(port),static_cast<unsigned char>(0xb0u|(channel&15u)),static_cast<unsigned char>(cc),0};
    const DWORD accepted=s.rawEmit(stream,flags,wire+(s.channels>16?0:4),s.channels>16?7:3);
    const int code=accepted==UINT_MAX?error():0;
    if(s.rawContext)raw_decode_context_observe(s.rawContext,stream,flags,wire+(s.channels>16?0:4),s.channels>16?7:3,accepted!=UINT_MAX,code);
    if(result){result->accepted=accepted==UINT_MAX?0u:1u;result->error=code;}
    // Recursive RAW failure does NOT suppress the outer saved-mode writer.
    save();if(accepted==UINT_MAX)failure(code,1);return failureStatus;
}
static int midiCCSemanticApply(MidiCCKernel& s,unsigned stream,unsigned channel,unsigned value,unsigned begin,unsigned end,MidiCCCompoundContext* context=nullptr,bool resetSeed=false){
    if(s.failureFencePending)return -6;
    const int recorded=midiCCPendingFailure(s);if(recorded!=1)return recorded;
    if(s.semanticActive&&(s.pendingSemanticStream!=stream||s.pendingSemanticChannel!=channel||s.pendingSemanticValue!=value||s.pendingSemanticEnd!=end||s.pendingSemanticNext!=begin))return -1;
    if(s.semanticActive&&context&&(context->mode!=s.pendingModeFlags||context->port!=s.pendingModePort))return -1;
    s.semanticActive=true;s.pendingSemanticStream=stream;s.pendingSemanticChannel=channel;
    s.pendingSemanticValue=value;s.pendingSemanticNext=begin;s.pendingSemanticEnd=end;s.pendingSemanticResetSeed=resetSeed;
    if(context){s.pendingModeFlags=context->mode;s.pendingModePort=context->port;}
    auto& state=*s.semantics;auto emit=reinterpret_cast<BOOL(WINAPI*)(HSTREAM,DWORD,DWORD,DWORD)>(s.emitAddress);
    auto error=reinterpret_cast<int(WINAPI*)()>(s.errorAddress);
    for(unsigned i=begin;i<end;++i){const auto& route=state.route(i);const auto& v=route.table[value];
        if(route.flags&32u){
            // Original set_expression compares BOTH saved and observed values;
            // unavailable readback falls back to saved, never fabricated zero.
            const int saved=state.modePresent[channel]?state.modeSaved[channel]:0;
            const unsigned raw=state.get(stream,channel,MIDI_EVENT_MODE);
            const int current=raw==UINT_MAX?saved:static_cast<int>(std::min(raw,2u));
            if(v.amount==saved&&v.amount==current)continue;
            state.modePresent[channel]=v.amount!=0;state.modeSaved[channel]=v.amount;
            state.journal->receive(stream,state.modeSlots[0],(channel<<16)|v.raw|(v.amount?0x8000u:0u));
            if(emit(stream,channel,MIDI_EVENT_MODE,v.raw)){
                const unsigned slot=state.slot(0x190000);if(slot!=256)state.journal->receive(stream,slot,(channel<<16)|v.raw);
            }else{const int code=error();state.failures.fetch_add(1,std::memory_order_relaxed);
                const int stored=midiCCRecordFailure(s,channel,0x160000u,0x170000u,0x180000u,code,2);
                if(stored!=1){s.pendingSemanticNext=i+1;return stored;}}
            continue;
        }
        if(route.flags&24u){const int applied=midiCCModeApply(s,stream,channel,route,value,context);
            if(applied!=1){s.pendingSemanticNext=i+((s.failureRecordPending||s.failureFencePending)?1u:0u);return applied;}continue;}
        if(route.flags&1u)state.write(channel,route.source,0,static_cast<unsigned>(v.input-state.fields[route.source].low));
        if(!route.event)continue;
        auto& field=state.fields[route.destination];
        if(route.flags&4u){state.fields[route.destination].timbrePresent[channel]=true;state.write(channel,route.destination,2,v.raw);}
        if(route.flags&2u){
            const int saved=field.present[channel]?field.saved[channel]:state.neutral(channel,field);
            if(v.amount==saved&&v.amount==state.current(channel,field,saved))continue;
            field.present[channel]=v.amount!=state.neutral(channel,field);field.saved[channel]=v.amount;
            // Offset by low; pop is decoded against current system by replay.
            state.write(channel,route.destination,1,static_cast<unsigned>(v.amount-field.low)|(field.present[channel]?0x8000u:0u));
        }
        unsigned raw=v.raw;bool accepted=false;int code=0,pending=0;
        if(route.event==MIDI_EVENT_REVERB||route.event==MIDI_EVENT_CHORUS){
            const unsigned floors=s.sends->floors.load(std::memory_order_acquire),floor=route.event==MIDI_EVENT_REVERB?floors&127u:(floors>>7)&127u;
            MidiSendResult applied{};int errors[3]{};
            pending=midi_send_domain_emit(s.sends,stream,channel,route.event,floor,v.raw,s.emitAddress,s.lockAddress,s.errorAddress,&applied,errors);
            raw=floor+((127u-floor)*v.raw)/127u;accepted=applied.accepted!=0;code=pending==0?errors[0]:applied.error;
            if(pending==-2){s.pendingStream=stream;s.pendingThread=GetCurrentThreadId();s.pendingSemanticNext=i+1;s.pendingSemanticEnd=end;s.pendingSemanticChannel=channel;s.pendingSemanticValue=value;
                if(context){s.pendingModeFlags=context->mode;s.pendingModePort=context->port;}}
        }else if(route.event==MIDI_EVENT_USERFX){for(auto* tap:s.delays[channel])if(tap)delayMidiSend(tap,v.raw);accepted=true;}
        else{accepted=emit(stream,channel,route.event,v.raw)!=FALSE;if(!accepted)code=error();}
        if(accepted){
            if(field.request!=256)state.journal->receive(stream,field.request,(channel<<16)|v.raw);
            state.write(channel,route.destination,resetSeed?6u:3u,raw);
            if(field.flags&2u){field.modRaw[channel]=static_cast<int>(raw);field.modPresent[channel]=true;field.modSerial[channel]=state.journal->clock->serial.load(std::memory_order_acquire);}
        }else{state.failures.fetch_add(1,std::memory_order_relaxed);if(route.flags&2u){
            const int stored=midiCCRecordFailure(s,channel,0x100000u+route.destination,0x110000u+route.destination,0,code,0);
            if(stored!=1){s.pendingSemanticNext=i+1;return pending==-2?-2:stored;}
        }}
        if(pending==-2)return -2;
    }
    s.semanticActive=false;s.pendingSemanticStream=0;s.pendingSemanticNext=0;s.pendingSemanticEnd=0;return 1;
}
static int midiCCSemanticContinue(MidiCCKernel& s,unsigned stream,MidiCCCompoundContext* context=nullptr){
    if(!s.semanticActive)return 1;
    if(s.pendingSemanticStream!=stream)return -1;
    if(s.pendingStream)return -3;
    return midiCCSemanticApply(s,stream,s.pendingSemanticChannel,s.pendingSemanticValue,
        s.pendingSemanticNext,s.pendingSemanticEnd,context,s.pendingSemanticResetSeed);
}
static int midiCCResetScalarContinue(MidiCCKernel& s,unsigned stream){
    const int remaining=midiCCSemanticContinue(s,stream);if(remaining!=1)return remaining;
    if(s.resetTailReady){
        if(s.resetTailStream!=stream)return -1;
        if(s.resetTailFault)return s.resetTailFault;
        while(s.resetOrderNext<s.resetOrderCount){
            const unsigned channel=s.resetOrder[s.resetOrderNext];auto& selection=*s.selection;
            if(s.resetTailPhase==3){++s.resetOrderNext;s.resetTailPhase=0;continue;}
            const unsigned budget=selection.need();auto reserved=selection.reserved.load(std::memory_order_acquire);
            for(;;){if(selection.produced.load(std::memory_order_acquire)-selection.consumed.load(std::memory_order_acquire)+reserved+budget>selection.recordLimit.load(std::memory_order_acquire)||!selection.room(budget))return -6;
                if(selection.reserved.compare_exchange_weak(reserved,reserved+budget,std::memory_order_acq_rel))break;}
            auto release=[&](){selection.reserved.fetch_sub(budget,std::memory_order_release);};
            auto fatal=[&](){s.resetTailFault=-8;s.resetTailError=reinterpret_cast<int(WINAPI*)()>(s.errorAddress)();s.pendingResetScalar=false;release();return -8;};
            if(s.resetTailPhase==0){
                if(s.resetTailExtensions&&!selection.resetExtensions(channel))return fatal();
                s.resetTailPhase=1;
            }else if(s.resetTailPhase==1){
                const auto& address=s.resetAddresses[channel];
                if(address[0]&&selection.resetInstrument(channel,address[0],address[1],address[2],address[3])==-8)return fatal();
                const auto& ch=selection.channels[channel];auto* request=s.fontRequests.data()+channel*4;
                request[0]=ch.drums?128u:ch.bank;request[1]=ch.program;request[2]=ch.lsb;
                s.resetTailPhase=2;
            }else{
                const unsigned first=s.semantics->resetFirst[channel],count=s.semantics->resetCount[channel];
                s.resetTailPhase=3; // Advance BEFORE a retained unlock, never resend its scalar prefix.
                const int applied=count?midiCCSemanticApply(s,stream,channel,0,first,first+count,nullptr,true):1;
                if(applied!=1){s.pendingResetScalar=true;release();return applied;}
            }
            release();
        }
        s.pendingResetScalar=false;s.resetTailStream=0;return 1;
    }
    while(s.resetOrderNext<s.resetOrderCount){const unsigned channel=s.resetOrder[s.resetOrderNext++];
        if(!s.semantics||!s.semantics->resetScalarReady)continue;
        const unsigned first=s.semantics->resetFirst[channel],count=s.semantics->resetCount[channel];
        const int applied=count?midiCCSemanticApply(s,stream,channel,0,first,first+count,nullptr,true):1;
        if(applied!=1){s.pendingResetScalar=true;return applied;}
    }
    s.pendingResetScalar=false;return 1;
}
// Pinned CPython3.14.7 integer-set ordering, at most16 consecutive addresses.
// No objects, allocation or Python/hash callbacks on the native MIDI thread.
struct MidiResetChannelSet {
    std::array<int,32> table;unsigned mask=7,used=0;
    MidiResetChannelSet(){table.fill(-1);}
    void add(unsigned channel){unsigned perturb=channel,index=channel&mask;
        for(;;){const unsigned probes=index+9<=mask?9u:0u;bool inserted=false;
            for(unsigned offset=0;offset<=probes;++offset){auto& value=table[index+offset];if(value==static_cast<int>(channel))return;
                if(value<0){value=static_cast<int>(channel);inserted=true;break;}}
            if(inserted)break;perturb>>=5;index=(index*5+1+perturb)&mask;
        }
        ++used;if(used*5>=mask*3){const auto previous=table;table.fill(-1);mask=31;
            for(int value:previous)if(value>=0)table[static_cast<unsigned>(value)&mask]=value;}
    }
};
struct MidiCCJournalLease {
    MidiCCKernel& state;bool retain=false;
    explicit MidiCCJournalLease(MidiCCKernel& value):state(value){if(state.journal)state.journal->clock->writers.fetch_add(1,std::memory_order_acq_rel);}
    ~MidiCCJournalLease(){if(state.journal&&!retain)state.journal->clock->writers.fetch_sub(1,std::memory_order_release);}
};
// Same receipt finalization for explicit semantic resume and the existing
// public unlock retry. No caller migration is required to finish a last CC.
static int midiCCFinishSemanticFrame(MidiCCKernel& s,unsigned stream){
    if(!s.semanticFramePending||!s.semanticFrameEnd)return 1;
    // A last intercepted CC left no final RAW bytes. The legacy `if out`
    // therefore does not restore drum UserFX after that earlier prefix flush.
    s.resetOrderCount=s.resetOrderNext=0;
    for(unsigned index=0;index<=s.semanticFrameResetMask;++index)if(s.semanticFrameResetTable[index]>=0)
        s.resetOrder[s.resetOrderCount++]=static_cast<unsigned>(s.semanticFrameResetTable[index]);
    s.semanticFramePending=false;s.semanticFrameEnd=false;s.semanticFrameDrumRestore=false;
    if(s.resetTailReady&&s.resetOrderCount){s.resetTailStream=stream;s.resetTailPhase=0;s.resetTailFault=s.resetTailError=0;}
    return s.resetOrderCount?midiCCResetScalarContinue(s,stream):1;
}
// Own additive ABI: a paused frame's semantic remainder precedes its next
// ORIGINAL byte/phase. No RAW replay, cancellation, new timing or audio retry.
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_semantic_resume_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_semantic_pending(void* pointer,unsigned stream){
    if(!pointer||!stream)return -1;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockShared(&s.lock);
    if(!s.semanticActive&&!s.semanticFramePending){ReleaseSRWLockShared(&s.lock);return 0;}
    if(!s.journal||s.journal->stream!=stream){ReleaseSRWLockShared(&s.lock);return -1;}
    // 1=resume before any next byte, 2=prefix-owned original suffix still
    // needs its final source post-RAW tail; 0=no semantic receipt owner.
    const int result=s.semanticActive?(s.pendingSemanticStream==stream?1:-1):s.semanticFramePending?(s.semanticFrameEnd?1:2):0;
    ReleaseSRWLockShared(&s.lock);return result;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_semantic_resume(void* pointer,unsigned stream){
    if(!pointer||!stream)return -1;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(!s.journal||s.journal->stream!=stream){ReleaseSRWLockExclusive(&s.lock);return -1;}
    if(s.cancelledStream||s.pendingStream||(s.executionOwner&&s.executionThread.load()!=GetCurrentThreadId())){ReleaseSRWLockExclusive(&s.lock);return -3;}
    MidiNoteLease notes(s.notes);MidiCCJournalLease lease(s);
    int result=midiCCSemanticContinue(s,stream);
    if(result==1)result=midiCCFinishSemanticFrame(s,stream);
    if(result==-2&&s.journal){lease.retain=true;s.pendingClockLease=true;}
    if(result==1)s.finishFrameIfDone();
    ReleaseSRWLockExclusive(&s.lock);return result;
}
// No implicit cancellation on a new frame. Only the explicit post-stop owner
// retirement may discard an UNEXECUTED semantic/compound suffix after all
// original prefix metadata has been acknowledged. This is not completion.
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_semantic_abort(void* pointer,unsigned stream){
    if(!pointer||!stream)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    // An ordinary retirement check must not cancel an unrelated GM/reset
    // owner. No semantic receipt means this additive check is a no-op.
    if(!s.semanticActive&&!s.semanticFramePending){ReleaseSRWLockExclusive(&s.lock);return 1;}
    if(!s.journal||s.journal->stream!=stream||s.executionOwner||s.pendingStream||s.pendingClockLease||
       midiCCCaptureTotal(s)||s.compoundCount||(s.selection&&s.selection->produced.load()!=s.selection->consumed.load())){
        ReleaseSRWLockExclusive(&s.lock);return 0;}
    if(s.semanticActive&&s.pendingSemanticStream!=stream){ReleaseSRWLockExclusive(&s.lock);return 0;}
    if(s.selection){auto& selection=*s.selection;AcquireSRWLockShared(&selection.ticketsLock);
        bool pendingTicket=false;
        for(unsigned index=0;index<selection.ticketCount;++index)
            if(selection.tickets[(selection.ticketHead+index)%static_cast<unsigned>(selection.tickets.size())].remaining){pendingTicket=true;break;}
        const bool reserved=selection.reserved.load()||selection.queuedReservation.load()||selection.unticketedReservation.load()||pendingTicket;
        ReleaseSRWLockShared(&selection.ticketsLock);
        if(reserved){ReleaseSRWLockExclusive(&s.lock);return 0;}}
    if(s.failureRowsPending()||!s.releaseFailureCredit()){ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.semanticActive=false;s.pendingSemanticStream=0;s.pendingSemanticNext=s.pendingSemanticEnd=0;
    s.semanticFramePending=false;s.semanticFrameEnd=false;s.semanticFrameDrumRestore=false;
    s.pendingGMSemantic=false;s.pendingResetScalar=false;s.resetOrderNext=s.resetOrderCount;
    s.resetTailStream=0;s.gmActive=false;s.gmStream=0;
    ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_reset_tail_pending(void* pointer,unsigned stream){
    if(!pointer||!stream)return -1;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockShared(&s.lock);
    const int result=!s.resetTailReady||!s.resetTailStream?0:s.resetTailStream!=stream?-1:
        (s.resetTailFault||s.resetOrderNext<s.resetOrderCount)?1:0;ReleaseSRWLockShared(&s.lock);return result;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_reset_tail_abort(void* pointer,unsigned stream){
    if(!pointer||!stream)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    // Explicit post-stop retirement only, not ordinary retry or a new frame.
    // Keep the captured fatal/error evidence; cancel only its unexecuted tail.
    if(s.executionOwner||s.pendingStream||midiCCCaptureTotal(s)||s.compoundCount||!s.resetTailFault||s.resetTailStream!=stream||
       (s.selection&&s.selection->produced.load()!=s.selection->consumed.load())){ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.resetOrderNext=s.resetOrderCount;s.resetTailStream=0;s.pendingResetScalar=false;
    ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_reset_tail(void* pointer,unsigned stream){
    if(!pointer||!stream)return -1;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.cancelledStream||s.pendingStream||(s.executionOwner&&s.executionThread.load()!=GetCurrentThreadId())){ReleaseSRWLockExclusive(&s.lock);return -3;}
    if(!s.resetTailStream){ReleaseSRWLockExclusive(&s.lock);return 1;}
    MidiNoteLease notes(s.notes);MidiCCJournalLease lease(s);
    const int result=midiCCResetScalarContinue(s,stream);
    if(result==-2){lease.retain=true;s.pendingClockLease=true;}
    if(result==1)s.finishFrameIfDone();
    ReleaseSRWLockExclusive(&s.lock);return result;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl midi_cc_kernel_create(unsigned channels,void* domain,void* raw,void* emit,void* lock,void* error){
    if(!channels||channels>128||!domain||!raw||!emit||!lock||!error)return nullptr;
    try{return new(std::nothrow)MidiCCKernel(channels,static_cast<NativeMidiSendDomain*>(domain),raw,emit,lock,error);}
    catch(const std::bad_alloc&){return nullptr;}
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_notes(void* pointer,void* state){
    if(!pointer||!state)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);auto* notes=static_cast<MidiNoteKernel*>(state);
    AcquireSRWLockExclusive(&s.lock);
    if(s.cancelledStream||s.frameReceipt||s.notes||s.pendingStream||notes->channels!=s.channels){ReleaseSRWLockExclusive(&s.lock);return 0;}
    notes->retain();s.notes=notes;ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_journal(void* pointer,void* state){
    if(!pointer||!state)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);
    AcquireSRWLockExclusive(&s.lock);
    if(s.cancelledStream||s.frameReceipt||s.journal||s.pendingStream){ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.journal=static_cast<NativeMidiState*>(state);
    bool valid=s.journal->clock!=nullptr;
    for(unsigned cc=0;cc<120;++cc){s.auxSlots[cc]=s.slot(0xa0000u+cc);valid=valid&&s.auxSlots[cc]!=256;}
    for(unsigned i=0;i<2;++i){s.requestSlots[i]=s.slot(0xb0000u+(i?MIDI_EVENT_CHORUS:MIDI_EVENT_REVERB));valid=valid&&s.requestSlots[i]!=256;}
    constexpr unsigned events[]{MIDI_EVENT_REVERB,MIDI_EVENT_CHORUS,MIDI_EVENT_USERFX};
    for(unsigned i=0;i<3;++i){s.acceptedSlots[i]=s.slot(events[i]);valid=valid&&s.acceptedSlots[i]!=256;}
    if(valid)s.journal->retain();else s.journal=nullptr;
    ReleaseSRWLockExclusive(&s.lock);return valid?1:0;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_configure(void* pointer,unsigned options,const unsigned char* blocked,unsigned count){
    if(!pointer||options>3||!blocked||count!=128*128)return 0;
    auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.cancelledStream||s.frameReceipt||s.pendingStream||s.semanticActive||s.semanticFramePending||(s.executionOwner&&!s.executionPaused)){ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.options=options;std::copy_n(blocked,count,s.blocked.begin());ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_delay(void* pointer,unsigned channel,void* dry,void* wet){
    if(!pointer||channel>=128)return 0;
    auto* first=static_cast<LegacyDelayState*>(dry);auto* second=static_cast<LegacyDelayState*>(wet);
    if((first&&!delayMidiReady(first))||(second&&!delayMidiReady(second)))return 0;
    auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.cancelledStream||s.frameReceipt||s.pendingStream||s.semanticActive||s.semanticFramePending||(s.executionOwner&&!s.executionPaused)){ReleaseSRWLockExclusive(&s.lock);return 0;}
    if(first)delayMidiRetain(first);if(second)delayMidiRetain(second);
    for(auto* previous:s.delays[channel])if(previous)delayMidiRelease(previous);
    s.delays[channel]={first,second};s.delayEnabled[channel]=true;ReleaseSRWLockExclusive(&s.lock);return 1;
}
// Own additive ABI1: retry protects every remaining scalar writer, including
// GM/manual continuations that did not retain an earlier capture lease.
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_retry_clock_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_cancel_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_retry_ex(void* pointer,int* error,unsigned flags){
    if(!pointer||!error||flags>1)return 0;*error=0;
    auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(!s.pendingStream){ReleaseSRWLockExclusive(&s.lock);return 1;}
    if(s.pendingThread!=GetCurrentThreadId()){*error=-1;ReleaseSRWLockExclusive(&s.lock);return -1;}
    // GM/manual phases release their writer before returning a retained
    // ChannelLock. Their remaining scalar routes still need one aggregate
    // capture lease during retry; an old retained lease is independent.
    MidiCCJournalLease retryLease(s);
    auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(s.lockAddress);
    const bool accepted=lock(s.pendingStream,FALSE)!=FALSE;
    if(accepted){
        s.pendingStream=0;s.pendingThread=0;
        if(s.selection)s.selection->releasePreparation();
        MidiNoteLease noteLease(s.notes);
        // flags1 retires ONLY the actual same-thread ChannelLock owner.
        // The actor publishes cancellation of unexecuted semantic/RAW/reset
        // suffix separately; prefix metadata still needs its original ACK.
        if(flags){
            if(s.notes)s.notes->finishActivityPending();
            if(s.pendingClockLease){s.journal->clock->writers.fetch_sub(1,std::memory_order_release);s.pendingClockLease=false;}
            ReleaseSRWLockExclusive(&s.lock);return 1;
        }
        int remaining=midiCCSemanticContinue(s,s.journal->stream);
        if(remaining==1)remaining=midiCCFinishSemanticFrame(s,s.journal->stream);
        if(remaining==-2){
            if(s.journal&&!s.pendingClockLease){retryLease.retain=true;s.pendingClockLease=true;}
            *error=reinterpret_cast<int(WINAPI*)()>(s.errorAddress)();ReleaseSRWLockExclusive(&s.lock);return 0;
        }
        if(remaining==1&&s.pendingResetScalar){const int tail=midiCCResetScalarContinue(s,s.journal->stream);
            if(tail==-2){if(s.journal&&!s.pendingClockLease){retryLease.retain=true;s.pendingClockLease=true;}
                *error=reinterpret_cast<int(WINAPI*)()>(s.errorAddress)();ReleaseSRWLockExclusive(&s.lock);return 0;}
            // Pressure/fatal keeps the cursor, but this same-thread BASS
            // unlock completed. Worker queries pending tail before finishing.
        }
        if(remaining==1)s.pendingGMSemantic=false;
        if(s.notes)s.notes->finishActivityPending();
        if(s.pendingClockLease){s.journal->clock->writers.fetch_sub(1,std::memory_order_release);s.pendingClockLease=false;}
        s.finishFrameIfDone(); // Preserve old retry -> replay -> close on a last CC.
    }
    else *error=reinterpret_cast<int(WINAPI*)()>(s.errorAddress)();
    ReleaseSRWLockExclusive(&s.lock);return accepted?1:0;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_retry(void* pointer,int* error){
    return midi_cc_kernel_retry_ex(pointer,error,0);
}
// Actor records the original cancellation owner; this does not discard
// prefix state, selection tickets, reservation credits or failure records.
static bool midiCCMarkCancellation(MidiCCKernel& s,unsigned stream,unsigned kind,unsigned long long receipt){
    AcquireSRWLockExclusive(&s.lock);
    const bool valid=stream&&receipt&&kind>=1&&kind<=5&&s.journal&&s.journal->stream==stream&&
        s.executionOwner&&s.executionThread.load()==GetCurrentThreadId()&&!s.pendingStream&&!s.pendingClockLease&&
        (!s.cancelledStream||(s.cancelledStream==stream&&s.cancelledKind==kind&&s.cancelledReceipt==receipt&&s.cancelledGeneration==s.semantics))&&
        (kind==1||((kind==5?s.gmKind==2:kind==3?s.gmKind==1:s.gmKind==0)&&s.gmStream==stream));
    if(valid&&!s.cancelledStream){s.cancelledStream=stream;s.cancelledKind=kind;s.cancelledReceipt=receipt;
        s.cancelledGeneration=s.semantics;if(s.cancelledGeneration)s.cancelledGeneration->retain();}
    ReleaseSRWLockExclusive(&s.lock);return valid;
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_cc_kernel_cancel_receipt(void* pointer,unsigned stream){
    if(!pointer||!stream)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockShared(&s.lock);
    const auto receipt=s.cancelledStream==stream?s.cancelledReceipt:0;ReleaseSRWLockShared(&s.lock);return receipt;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_cancel_owned(void* pointer,unsigned stream,unsigned long long receipt){
    if(!pointer||!stream)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(!s.cancelledStream){ReleaseSRWLockExclusive(&s.lock);return receipt?0:1;}
    // Post join and ordered prefix ACK are mandatory. No timed discard and
    // no BASS call here; a false return retains the exact original owner.
    if(s.cancelledStream!=stream||!receipt||s.cancelledReceipt!=receipt||s.cancelledGeneration!=s.semantics||
       !s.journal||s.journal->stream!=stream||s.executionOwner||s.pendingStream||s.pendingClockLease||
       midiCCCaptureTotal(s)||s.compoundCount||(s.selection&&s.selection->produced.load()!=s.selection->consumed.load())){
        ReleaseSRWLockExclusive(&s.lock);return 0;}
    if(s.selection){auto& selection=*s.selection;AcquireSRWLockShared(&selection.ticketsLock);
        bool pending=false;
        for(unsigned index=0;index<selection.ticketCount;++index)
            if(selection.tickets[(selection.ticketHead+index)%static_cast<unsigned>(selection.tickets.size())].remaining){pending=true;break;}
        const bool reserved=selection.reserved.load()||selection.queuedReservation.load()||selection.unticketedReservation.load()||pending;
        ReleaseSRWLockShared(&selection.ticketsLock);if(reserved){ReleaseSRWLockExclusive(&s.lock);return 0;}}
    if(s.semanticActive&&s.pendingSemanticStream!=stream){ReleaseSRWLockExclusive(&s.lock);return 0;}
    if(s.cancelledKind!=1&&s.gmStream!=stream){ReleaseSRWLockExclusive(&s.lock);return 0;}
    auto pendingFailures=[](NativeMidiState* journal){if(!journal)return false;AcquireSRWLockShared(&journal->failureLock);
        const bool pending=journal->failureProduced!=journal->failureConsumed||journal->failureReserved||journal->failureActiveLeases||journal->failureReaders.load();
        ReleaseSRWLockShared(&journal->failureLock);return pending;};
    // Release ONLY unspent credits after the joined original owner stops.
    // Published errors remain immutable and must ACK before cancellation.
    if(!s.releaseFailureCredit()){ReleaseSRWLockExclusive(&s.lock);return 0;}
    if(pendingFailures(s.journal)||pendingFailures(s.semantics?s.semantics->journal:nullptr)||pendingFailures(s.manualJournal)){
        ReleaseSRWLockExclusive(&s.lock);return 0;}
    // Explicit cancelled-generation retirement has joined the original actor
    // and ACKed every prefix/failure/read lease above. It may now retire this
    // generation's error fence. Ordinary stop/DRAIN/semantic abort must not.
    s.failureFencePending=false;s.failureFenceStream=0;s.failureFenceSerial=0;
    s.semanticActive=false;s.pendingSemanticStream=0;s.pendingSemanticNext=s.pendingSemanticEnd=0;s.pendingSemanticResetSeed=false;
    s.semanticFramePending=false;s.semanticFrameEnd=false;s.semanticFrameDrumRestore=false;
    s.pendingGMSemantic=false;
    if(s.cancelledKind==1){s.pendingResetScalar=false;s.resetOrderNext=s.resetOrderCount;s.resetTailStream=0;}
    else{s.gmActive=false;s.gmStream=0;}
    s.cancelledStream=s.cancelledKind=0;s.cancelledReceipt=0;
    // Immutable original suffix retirement is explicit, post join/prefix ACK.
    s.clearFrameReceipt();
    if(s.cancelledGeneration){s.cancelledGeneration->release();s.cancelledGeneration=nullptr;}
    ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_take_actions(void* pointer,MidiNoteAction* out,unsigned capacity){
    if(!pointer||!out)return -1;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(capacity<s.compoundCount){ReleaseSRWLockExclusive(&s.lock);return -2;}
    const unsigned count=s.compoundCount;std::copy_n(s.compoundActions.begin(),count,out);s.compoundCount=0;
    ReleaseSRWLockExclusive(&s.lock);return static_cast<int>(count);
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_capture_read(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out||capacity<12)return -1;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockShared(&s.lock);
    if(!midiCCCaptureTotal(s)){ReleaseSRWLockShared(&s.lock);return 0;}
    if(s.retainedCaptureCount)std::copy(s.retainedCaptures[s.retainedCaptureHead].begin(),s.retainedCaptures[s.retainedCaptureHead].end(),out);
    else std::copy(s.captures[s.captureHead].begin(),s.captures[s.captureHead].end(),out);
    ReleaseSRWLockShared(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_capture_snapshot(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out||capacity>UINT_MAX/12u)return -1;auto& s=*static_cast<MidiCCKernel*>(pointer);
    if(!TryAcquireSRWLockShared(&s.lock))return -3;
    const unsigned count=midiCCCaptureTotal(s);if(capacity<count){ReleaseSRWLockShared(&s.lock);return -2;}
    for(unsigned i=0;i<s.retainedCaptureCount;++i)std::copy(s.retainedCaptures[(s.retainedCaptureHead+i)%static_cast<unsigned>(s.retainedCaptures.size())].begin(),
        s.retainedCaptures[(s.retainedCaptureHead+i)%static_cast<unsigned>(s.retainedCaptures.size())].end(),out+i*12);
    for(unsigned i=0;i<s.captureCount;++i)std::copy(s.captures[(s.captureHead+i)%static_cast<unsigned>(s.captures.size())].begin(),
        s.captures[(s.captureHead+i)%static_cast<unsigned>(s.captures.size())].end(),out+(s.retainedCaptureCount+i)*12);
    ReleaseSRWLockShared(&s.lock);return static_cast<int>(count);
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_capture_ack(void* pointer,unsigned long long serial){
    if(!pointer)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    bool accepted=false;
    if(s.retainedCaptureCount&&s.retainedCaptures[s.retainedCaptureHead][0]==serial){s.retainedCaptureHead=(s.retainedCaptureHead+1)%static_cast<unsigned>(s.retainedCaptures.size());--s.retainedCaptureCount;accepted=true;}
    else if(!s.retainedCaptureCount&&s.captureCount&&s.captures[s.captureHead][0]==serial){s.captureHead=(s.captureHead+1)%static_cast<unsigned>(s.captures.size());--s.captureCount;accepted=true;}
    ReleaseSRWLockExclusive(&s.lock);return accepted?1:0;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_capture_retained_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_capture_retained_prepare(void* pointer){
    if(!pointer)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(!s.executionOwner&&!s.retainedCaptureEnabled){
        s.captureThread=reinterpret_cast<HANDLE>(_beginthreadex(nullptr,0,&MidiCCKernel::growCaptureStorage,&s,0,nullptr));
        if(s.captureThread)s.retainedCaptureEnabled=true;
    }
    const bool accepted=s.retainedCaptureEnabled;ReleaseSRWLockExclusive(&s.lock);return accepted?1:0;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_capture_count(void* pointer){
    if(!pointer)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockShared(&s.lock);const auto count=midiCCCaptureTotal(s);ReleaseSRWLockShared(&s.lock);return count;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_destroy(void* pointer){
    if(!pointer)return 1;auto* s=static_cast<MidiCCKernel*>(pointer);
    // Caller stop/join barrier first. Pending BASS ownership is never discarded.
    if(s->frameReceipt||s->pendingStream||s->semanticActive||s->semanticFramePending||s->executionOwner||midiCCCaptureTotal(*s)||s->compoundCount||s->gmActive||s->resetTailStream||s->cancelledStream||s->failureRecordPending)return 0;
    if(s->failureRowsPending()||!s->releaseFailureCredit())return 0;delete s;return 1;
}
// Executes a prepared compatibility reset, retaining an exact phase/channel
// cursor when the selection metadata FIFO needs ACK. No ordinary RAW boundary
// or blanket BASS lock is introduced; source StreamEvent calls stay immediate.
static int midiCCGMContinue(MidiCCKernel& s,unsigned stream,void* timing){
    {MidiCCJournalLease lease(s);const int remaining=midiCCSemanticContinue(s,stream);
        if(remaining!=1){if(remaining==-2&&s.journal){lease.retain=true;s.pendingClockLease=true;}return remaining;}}
    s.pendingGMSemantic=false;
    auto& selection=*s.selection;auto emit=reinterpret_cast<BOOL(WINAPI*)(HSTREAM,DWORD,DWORD,DWORD)>(s.emitAddress);
    auto failure=reinterpret_cast<int(WINAPI*)()>(s.errorAddress);
    while(s.gmActive){
        const unsigned budget=selection.need();auto reserved=selection.reserved.load(std::memory_order_acquire);
        for(;;){if(selection.produced.load(std::memory_order_acquire)-selection.consumed.load(std::memory_order_acquire)+reserved+budget>selection.recordLimit.load(std::memory_order_acquire)||!selection.room(budget))return -6;
            if(selection.reserved.compare_exchange_weak(reserved,reserved+budget,std::memory_order_acq_rel))break;}
        s.journal->clock->writers.fetch_add(1,std::memory_order_acq_rel);
        auto release=[&](){s.journal->clock->writers.fetch_sub(1,std::memory_order_release);selection.reserved.fetch_sub(budget,std::memory_order_release);};
        // Source exceptions stop the request. A retry is not permission to
        // emit its suffix; metadata may drain and then release this owner.
        auto fatal=[&](){s.gmFatal=-8;s.gmError=failure();s.gmActive=false;release();return -8;};
        auto event=[&](unsigned channel,unsigned kind,unsigned raw,bool observed=true){
            const bool accepted=emit(stream,channel,kind,raw)!=FALSE;
            if(kind==MIDI_EVENT_RESET)midiCCObserveResetResult(s,stream,kind,channel,raw,accepted,accepted?0:failure());
            if(observed)selection.write(channel,kind,raw,22,accepted?1u:0u,accepted?0:failure());
            return accepted;};
        const unsigned channel=s.gmChannel<s.gmSelection.size()?s.gmSelection[s.gmChannel]:0;
        if(s.gmPhase==0){
            if(!midiCCCaptureEnsureRoom(s)){release();return -6;}
            // Original timing reset alone owns a ChannelLock. All subsequent
            // reset events intentionally have no aggregate decoding exclusion.
            if(timing){auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(s.lockAddress);
                if(!lock(stream,TRUE))return fatal();
                playback_timing_reset(timing);
                if(!lock(stream,FALSE)){s.pendingStream=stream;s.pendingThread=GetCurrentThreadId();s.gmPhase=1;release();return -2;}}
            s.gmPhase=1;release();continue;
        }
        if(s.gmPhase==1){
            if(!midiCCCaptureEnsureRoom(s)){release();return -6;}
            if(s.gmSystemModePending){
                // bassmidi.h SYSTEMEX includes every allocated MIDI port.
                // Consume this prefix once, including retained ACK/unlock
                // continuations. The existing reset then restores user seeds.
                const unsigned mode=s.gmSystemMode;s.gmSystemModePending=false;
                if(!event(0,MIDI_EVENT_SYSTEMEX,mode,false))return fatal();
            }
            const auto before=s.resetLearnRevision;if(s.resetArmChannel!=UINT_MAX){s.resetArmChannel=UINT_MAX;++s.resetLearnRevision;}
            if(s.semantics){s.semantics->armed=false;s.semantics->armRevision=s.resetLearnRevision;
                for(auto& field:s.semantics->fields){field.present.fill(false);field.modPresent.fill(false);field.timbrePresent.fill(false);field.modSerial.fill(s.journal->clock->serial.load());field.timbreSerial.fill(s.journal->clock->serial.load());}
                s.semantics->modePresent.fill(false);}
            s.notes->counts.fill(0);s.notes->clearActivity();
            for(unsigned ch=0;ch<128;++ch){s.sends->request(ch,MIDI_EVENT_REVERB,s.gmDefaultReverb);s.sends->request(ch,MIDI_EVENT_CHORUS,0);
                for(auto* tap:s.delays[ch])if(tap)delayMidiSend(tap,0);
                for(unsigned cc=0;cc<120;++cc)s.journal->receive(stream,s.auxSlots[cc],ch<<16);}
            const auto serial=s.journal->clock->serial.fetch_add(1,std::memory_order_acq_rel)+1;
            s.captures[(s.captureHead+s.captureCount++)%static_cast<unsigned>(s.captures.size())]={serial,before,s.resetLearnRevision,0,0,0,128,0,s.gmChannels,0,0,0};
            AcquireSRWLockExclusive(&selection.recordsLock);selection.provenanceOriginClear=serial;ReleaseSRWLockExclusive(&selection.recordsLock);
            ++s.gmPhase;s.gmChannel=0;
        }else if(s.gmPhase==2){selection.resetSession(s.gmChannel);if(++s.gmChannel==128){++s.gmPhase;s.gmChannel=0;}
        }else if(s.gmPhase==3){
            event(channel,MIDI_EVENT_SOUNDOFF,0,false);event(channel,MIDI_EVENT_RESET,0,false);event(channel,MIDI_EVENT_NOTESOFF,0,false);
            for(const auto& item:s.gmDefaults){const unsigned system=item.flags&1u?selection.get(stream,0,MIDI_EVENT_SYSTEM):0;
                event(channel,item.event,(item.flags&1u)&&(system==3||system==4||system==5)?0:item.raw);}
            for(auto* tap:s.delays[channel])if(tap)delayMidiSend(tap,0);
            if(++s.gmChannel==s.gmChannels){++s.gmPhase;s.gmChannel=0;}
        }else if(s.gmPhase==4){
            if(s.gmDelay){if(!midiCCCaptureEnsureRoom(s)){release();return -6;}
                for(const auto& pair:s.delays)for(auto* tap:pair)if(tap)delay_midi_controls(tap,s.gmFrames,3,35,100);
                const auto serial=s.journal->clock->serial.fetch_add(1,std::memory_order_acq_rel)+1;
                s.captures[(s.captureHead+s.captureCount++)%static_cast<unsigned>(s.captures.size())]={serial,0,0,0,0,0,129,0,0,0,0,0};}
            ++s.gmPhase;
        }else if(s.gmPhase==5){selection.resetRouting(channel);if(++s.gmChannel==s.gmChannels){++s.gmPhase;s.gmChannel=0;}
        }else if(s.gmPhase==6){
            const bool accepted=selection.resetRefresh(channel);++s.gmChannel;
            {const auto& address=selection.channels[channel];auto* request=s.fontRequests.data()+channel*4;
                request[0]=address.drums?128u:address.bank;request[1]=address.program;request[2]=address.lsb;}
            if(s.gmChannel==s.gmChannels){++s.gmPhase;s.gmChannel=0;}
            if(!accepted)return fatal();
        }else if(s.gmPhase==7){const bool accepted=selection.syncGraph();++s.gmPhase;if(!accepted)return fatal();
        }else if(s.gmPhase==8){
            const bool accepted=selection.resetApply(channel);if(++s.gmChannel==s.gmChannels){++s.gmPhase;s.gmChannel=0;}
            if(!accepted)return fatal();
        }else if(s.gmPhase==9){
            const unsigned target=s.gmSendChannels[s.gmChannel];
            event(target,MIDI_EVENT_REVERB,s.sends->apply(MIDI_EVENT_REVERB,s.gmDefaultReverb));event(target,MIDI_EVENT_CHORUS,s.sends->apply(MIDI_EVENT_CHORUS,0));
            if(++s.gmChannel==s.gmSendChannels.size()){++s.gmPhase;s.gmChannel=0;}
        }else if(s.gmPhase==10){
            constexpr unsigned kinds[]{MIDI_EVENT_VOLUME,MIDI_EVENT_PAN,MIDI_EVENT_MODULATION,MIDI_EVENT_PITCH,MIDI_EVENT_SUSTAIN,MIDI_EVENT_SOSTENUTO,MIDI_EVENT_SOFT,MIDI_EVENT_CHANPRES};
            constexpr unsigned values[]{100,64,0,8192,0,0,0,0};for(unsigned i=0;i<8;++i)event(channel,kinds[i],values[i],false);
            if(++s.gmChannel==s.gmChannels){++s.gmPhase;s.gmChannel=0;}
        }else if(s.gmPhase==11){event(9,MIDI_EVENT_USERFX,selection.channels[9].drums?127:0,false);++s.gmPhase;
        }else if(s.gmPhase==12){for(const auto& item:s.gmMasters)event(0,item.event,item.raw);++s.gmPhase;
        }else if(s.gmPhase==13){int bank=-1,program=-1;const auto& row=selection.current(channel,bank,program);selection.profile(channel,row,true);
            if(++s.gmChannel==s.gmChannels){++s.gmPhase;s.gmChannel=0;}
        }else if(s.gmPhase==14){event(channel,MIDI_EVENT_PORTAMENTO,127);if(++s.gmChannel==s.gmChannels){++s.gmPhase;s.gmChannel=0;}
        }else if(s.gmPhase==15){
            // Extension restore precedes saved scalar defaults, even when
            // the current profile contains no selected extension fields.
            const bool accepted=!s.gmExtensions||selection.resetExtensions(channel);if(!accepted)return fatal();
            if(s.resetAddressesReady){const auto& address=s.resetAddresses[channel];
                if(address[0]&&selection.resetInstrument(channel,address[0],address[1],address[2],address[3])==-8)return fatal();}
            {const auto& address=selection.channels[channel];auto* request=s.fontRequests.data()+channel*4;
                request[0]=address.drums?128u:address.bank;request[1]=address.program;request[2]=address.lsb;}
            const unsigned first=s.semantics->resetFirst[channel],count=s.semantics->resetCount[channel];
            ++s.gmChannel;if(s.gmChannel==s.gmChannels){++s.gmPhase;s.gmChannel=0;}
            const int applied=count?midiCCSemanticApply(s,stream,channel,0,first,first+count,nullptr,true):1;
            if(applied!=1){s.pendingGMSemantic=true;release();return applied;}
        }else{s.gmActive=false;s.gmPhase=s.gmChannel=0;}
        release();
    }
    return 1;
}
static int midiCCGMRun(void* pointer,unsigned stream,void* timing,unsigned systemMode,bool explicitMode=false){
    if(!pointer||!stream)return -1;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.cancelledStream||!s.gmReady||s.pendingStream||(s.executionOwner&&s.executionThread.load()!=GetCurrentThreadId())){ReleaseSRWLockExclusive(&s.lock);return -3;}
    if(s.gmFatal){const int result=s.gmFatal;ReleaseSRWLockExclusive(&s.lock);return result;}
    if(s.gmActive&&s.gmKind!=0){ReleaseSRWLockExclusive(&s.lock);return -1;}
    MidiNoteLease notes(s.notes);MidiActivityLease activityLease(s.notes,nullptr,0,0,(!s.gmActive||s.gmPhase<=1)?1u:0u,&s.pendingStream);
    if(!activityLease.ready){ReleaseSRWLockExclusive(&s.lock);return -4;}
    if(!s.gmActive){const int ready=midiCCReserveFailures(s,midiCCResetFailureBudget(s));
        if(ready!=1){ReleaseSRWLockExclusive(&s.lock);return ready;}
        s.gmActive=true;s.gmKind=0;s.gmStream=stream;s.gmPhase=s.gmChannel=0;s.gmSystemMode=systemMode;s.gmSystemModePending=explicitMode||systemMode!=0;}
    if(s.gmStream!=stream){ReleaseSRWLockExclusive(&s.lock);return -1;}
    const int status=midiCCGMContinue(s,stream,timing);
    if((status==1&&!s.gmActive)||status==-8)s.releaseFailureCredit();
    ReleaseSRWLockExclusive(&s.lock);return status;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_gm_run(void* pointer,unsigned stream,void* timing){
    // Retain the public standalone compatibility-reset operator and its ABI.
    return midiCCGMRun(pointer,stream,timing,0);
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_system_mode_reset_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_gm_status(void* pointer,int* out,unsigned capacity){
    if(!pointer||!out||capacity<5)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockShared(&s.lock);
    const int values[]{s.gmActive?1:0,static_cast<int>(s.gmPhase),static_cast<int>(s.gmChannel),s.gmFatal,s.gmError};
    std::copy_n(values,5,out);ReleaseSRWLockShared(&s.lock);return 1;
}
// Compatibility admission is WHOLE original wire equality. An embedded reset
// plus Notes is ordinary RAW, not permission to consume a SYSTEM submessage.
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_system_reset_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_system_reset_kind(void* pointer,unsigned stream,const unsigned char* data,unsigned length){
    if(!pointer||!stream||(length&&!data))return -1;
    constexpr unsigned char gm1[]{0xf0,0x7e,0x7f,0x09,0x01,0xf7},gm2[]{0xf0,0x7e,0x7f,0x09,0x03,0xf7};
    constexpr unsigned char gs[]{0xf0,0x41,0x10,0x42,0x12,0x40,0x00,0x7f,0x00,0x41,0xf7};
    constexpr unsigned char xg[]{0xf0,0x43,0x10,0x4c,0x00,0x00,0x7e,0x00,0xf7};
    const bool gm=length==6&&(!memcmp(data,gm1,6)||!memcmp(data,gm2,6));
    const bool roland=length==11&&!memcmp(data,gs,11),yamaha=length==9&&!memcmp(data,xg,9);if(!gm&&!roland&&!yamaha)return 0;
    auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockShared(&s.lock);
    const unsigned ignored=gm?(data[4]==3?2u:1u):yamaha?4u:0u;
    const int kind=!s.gmReady||!s.journal||s.journal->stream!=stream?-1:
        (s.gmIgnoreModes&ignored)?3:gm?1:yamaha?0:s.gmGS?2:3;
    ReleaseSRWLockShared(&s.lock);return kind;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_system_reset_pending(void* pointer,unsigned stream){
    if(!pointer||!stream)return -1;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockShared(&s.lock);
    const int result=s.gmStream&&s.gmKind==0&&(s.gmActive||s.gmFatal)?(s.gmStream==stream?1:-1):0;
    ReleaseSRWLockShared(&s.lock);return result;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_system_reset_abort(void* pointer,unsigned stream){
    if(!pointer||!stream)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.executionOwner||s.pendingStream||midiCCCaptureTotal(s)||s.compoundCount||!s.gmFatal||s.gmStream!=stream||
       (s.selection&&s.selection->produced.load()!=s.selection->consumed.load())){ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.gmStream=0;s.gmActive=false;ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_manual_reset_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_manual_reset_prepare(void* pointer){
    if(!pointer)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    const bool ready=s.gmReady&&s.resetAddressesReady&&s.semantics&&s.selection&&s.notes&&!s.gmActive&&!s.pendingStream&&(!s.executionOwner||s.executionPaused);
    if(ready)s.manualResetReady=true;ReleaseSRWLockExclusive(&s.lock);return ready?1:0;
}
// UI/startup Reset has a distinct source order. It is not an incoming GM reset
// and deliberately does not clear control origins or increment reset revision.
static int midiCCManualResetContinue(MidiCCKernel& s,unsigned stream,void* timing){
    {MidiCCJournalLease lease(s);const int remaining=midiCCSemanticContinue(s,stream);
        if(remaining!=1){if(remaining==-2&&s.journal){lease.retain=true;s.pendingClockLease=true;}return remaining;}}
    s.pendingGMSemantic=false;
    auto& selection=*s.selection;auto emit=reinterpret_cast<BOOL(WINAPI*)(HSTREAM,DWORD,DWORD,DWORD)>(s.emitAddress);
    auto failure=reinterpret_cast<int(WINAPI*)()>(s.errorAddress);
    while(s.gmActive){
        const unsigned budget=selection.need();auto reserved=selection.reserved.load(std::memory_order_acquire);
        for(;;){if(selection.produced.load(std::memory_order_acquire)-selection.consumed.load(std::memory_order_acquire)+reserved+budget>selection.recordLimit.load(std::memory_order_acquire)||!selection.room(budget))return -6;
            if(selection.reserved.compare_exchange_weak(reserved,reserved+budget,std::memory_order_acq_rel))break;}
        s.journal->clock->writers.fetch_add(1,std::memory_order_acq_rel);
        auto release=[&](){s.journal->clock->writers.fetch_sub(1,std::memory_order_release);selection.reserved.fetch_sub(budget,std::memory_order_release);};
        auto fatal=[&](){s.gmFatal=-8;s.gmError=failure();s.gmActive=false;release();return -8;};
        auto event=[&](unsigned channel,unsigned kind,unsigned raw,bool observed=true){const bool accepted=emit(stream,channel,kind,raw)!=FALSE;
            if(kind==MIDI_EVENT_RESET)midiCCObserveResetResult(s,stream,kind,channel,raw,accepted,accepted?0:failure());
            if(observed)selection.write(channel,kind,raw,29,accepted?1u:0u,accepted?0:failure());return accepted;};
        const unsigned channel=s.gmChannel<s.gmSelection.size()?s.gmSelection[s.gmChannel]:0;
        if(s.gmPhase==0){
            if(timing){auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(s.lockAddress);if(!lock(stream,TRUE))return fatal();
                playback_timing_reset(timing);if(!lock(stream,FALSE)){s.pendingStream=stream;s.pendingThread=GetCurrentThreadId();s.gmPhase=1;release();return -2;}}
            s.gmPhase=1;release();continue;
        }else if(s.gmPhase==1){
            if(!midiCCCaptureEnsureRoom(s)){release();return -6;}
            const auto before=s.resetLearnRevision;if(s.resetArmChannel!=UINT_MAX){s.resetArmChannel=UINT_MAX;++s.resetLearnRevision;}
            s.semantics->armed=false;s.semantics->armRevision=s.resetLearnRevision;
            for(auto& field:s.semantics->fields){field.present.fill(false);field.modPresent.fill(false);field.timbrePresent.fill(false);field.modSerial.fill(s.journal->clock->serial.load());field.timbreSerial.fill(s.journal->clock->serial.load());}
            s.semantics->modePresent.fill(false);s.notes->counts.fill(0);s.notes->clearActivity();
            for(unsigned ch=0;ch<128;++ch)for(unsigned cc=0;cc<120;++cc)s.journal->receive(stream,s.auxSlots[cc],ch<<16);
            const auto serial=s.journal->clock->serial.fetch_add(1,std::memory_order_acq_rel)+1;
            s.captures[(s.captureHead+s.captureCount++)%static_cast<unsigned>(s.captures.size())]={serial,before,s.resetLearnRevision,0,0,0,130,0,0,0,0,0};
            ++s.gmPhase;s.gmChannel=0;
        }else if(s.gmPhase==2){selection.resetSession(s.gmChannel);if(++s.gmChannel==128){++s.gmPhase;s.gmChannel=0;}
        }else if(s.gmPhase==3){selection.resetRouting(channel);if(++s.gmChannel==s.gmChannels){++s.gmPhase;s.gmChannel=0;}
        }else if(s.gmPhase==4){
            event(channel,MIDI_EVENT_SOUNDOFF,0,false);event(channel,MIDI_EVENT_RESET,0,false);event(channel,MIDI_EVENT_NOTESOFF,0,false);
            for(const auto& item:s.gmDefaults){const unsigned system=item.flags&1u?selection.get(stream,0,MIDI_EVENT_SYSTEM):0;
                event(channel,item.event,(item.flags&1u)&&(system==3||system==4||system==5)?0:item.raw);}
            if(selection.resetInstrument(channel,7,(channel%16==9)?128u:0u,0,0)==-8)return fatal();
            {const auto& address=selection.channels[channel];auto* request=s.fontRequests.data()+channel*4;
                request[0]=address.drums?128u:address.bank;request[1]=address.program;request[2]=address.lsb;}
            if(++s.gmChannel==s.gmChannels){++s.gmPhase;s.gmChannel=0;}
        }else if(s.gmPhase==5){
            if(!s.gmChannel){if(!midiCCCaptureEnsureRoom(s)){release();return -6;}
                for(unsigned ch=0;ch<128;++ch){s.sends->request(ch,MIDI_EVENT_REVERB,s.gmDefaultReverb);s.sends->request(ch,MIDI_EVENT_CHORUS,0);}
                const auto serial=s.journal->clock->serial.fetch_add(1,std::memory_order_acq_rel)+1;
                s.captures[(s.captureHead+s.captureCount++)%static_cast<unsigned>(s.captures.size())]={serial,0,0,0,0,0,131,0,0,0,0,0};}
            const unsigned target=s.gmSendChannels[s.gmChannel];
            event(target,MIDI_EVENT_REVERB,s.sends->apply(MIDI_EVENT_REVERB,s.gmDefaultReverb));event(target,MIDI_EVENT_CHORUS,s.sends->apply(MIDI_EVENT_CHORUS,0));
            if(++s.gmChannel==s.gmSendChannels.size()){++s.gmPhase;s.gmChannel=0;}
        }else if(s.gmPhase==6){
            constexpr unsigned kinds[]{MIDI_EVENT_VOLUME,MIDI_EVENT_PAN,MIDI_EVENT_MODULATION,MIDI_EVENT_PITCH,MIDI_EVENT_SUSTAIN,MIDI_EVENT_SOSTENUTO,MIDI_EVENT_SOFT,MIDI_EVENT_CHANPRES};
            constexpr unsigned values[]{100,64,0,8192,0,0,0,0};for(unsigned i=0;i<8;++i)event(channel,kinds[i],values[i],false);
            if(++s.gmChannel==s.gmChannels){++s.gmPhase;s.gmChannel=0;}
        }else if(s.gmPhase==7){
            if(!midiCCCaptureEnsureRoom(s)){release();return -6;}
            for(unsigned ch=0;ch<128;++ch){for(auto* tap:s.delays[ch])if(tap)delayMidiSend(tap,0);for(unsigned cc=0;cc<120;++cc)s.journal->receive(stream,s.auxSlots[cc],ch<<16);}
            const auto serial=s.journal->clock->serial.fetch_add(1,std::memory_order_acq_rel)+1;
            s.captures[(s.captureHead+s.captureCount++)%static_cast<unsigned>(s.captures.size())]={serial,0,0,0,0,0,132,0,0,0,0,0};++s.gmPhase;
        }else if(s.gmPhase==8){
            if(s.gmDelay){if(!midiCCCaptureEnsureRoom(s)){release();return -6;}for(const auto& pair:s.delays)for(auto* tap:pair)if(tap)delay_midi_controls(tap,s.gmFrames,3,35,100);
                const auto serial=s.journal->clock->serial.fetch_add(1,std::memory_order_acq_rel)+1;s.captures[(s.captureHead+s.captureCount++)%static_cast<unsigned>(s.captures.size())]={serial,0,0,0,0,0,129,0,0,0,0,0};}++s.gmPhase;
        }else if(s.gmPhase==9){for(const auto& item:s.gmMasters)event(0,item.event,item.raw);++s.gmPhase;
        }else if(s.gmPhase==10){int bank=-1,program=-1;selection.profile(channel,selection.current(channel,bank,program),true);if(++s.gmChannel==s.gmChannels){++s.gmPhase;s.gmChannel=0;}
        }else if(s.gmPhase==11){event(channel,MIDI_EVENT_PORTAMENTO,127);if(++s.gmChannel==s.gmChannels){++s.gmPhase;s.gmChannel=0;}
        }else if(s.gmPhase==12){
            if(s.gmExtensions&&!selection.resetExtensions(channel))return fatal();const auto& address=s.resetAddresses[channel];
            if(address[0]&&selection.resetInstrument(channel,address[0],address[1],address[2],address[3])==-8)return fatal();
            {const auto& changed=selection.channels[channel];auto* request=s.fontRequests.data()+channel*4;
                request[0]=changed.drums?128u:changed.bank;request[1]=changed.program;request[2]=changed.lsb;}
            const unsigned first=s.semantics->resetFirst[channel],count=s.semantics->resetCount[channel];
            if(++s.gmChannel==s.gmChannels){++s.gmPhase;s.gmChannel=0;}
            const int applied=count?midiCCSemanticApply(s,stream,channel,0,first,first+count,nullptr,true):1;
            if(applied!=1){s.pendingGMSemantic=true;release();return applied;}
        }else{s.gmActive=false;s.gmPhase=s.gmChannel=0;}
        release();
    }return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_manual_reset_run(void* pointer,unsigned stream,void* timing){
    if(!pointer||!stream)return -1;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.cancelledStream||!s.manualResetReady||s.pendingStream||(s.executionOwner&&s.executionThread.load()!=GetCurrentThreadId())){ReleaseSRWLockExclusive(&s.lock);return -3;}
    if(s.gmFatal){const int result=s.gmFatal;ReleaseSRWLockExclusive(&s.lock);return result;}
    if(s.gmActive&&s.gmKind!=1){ReleaseSRWLockExclusive(&s.lock);return -1;}
    MidiNoteLease notes(s.notes);MidiActivityLease activityLease(s.notes,nullptr,0,0,(!s.gmActive||s.gmPhase<=1)?1u:0u,&s.pendingStream);
    if(!activityLease.ready){ReleaseSRWLockExclusive(&s.lock);return -4;}
    if(!s.gmActive){const int ready=midiCCReserveFailures(s,midiCCResetFailureBudget(s));
        if(ready!=1){ReleaseSRWLockExclusive(&s.lock);return ready;}
        s.gmActive=true;s.gmKind=1;s.gmStream=stream;s.gmPhase=s.gmChannel=0;}
    if(s.gmStream!=stream){ReleaseSRWLockExclusive(&s.lock);return -1;}
    const int result=midiCCManualResetContinue(s,stream,timing);
    if((result==1&&!s.gmActive)||result==-8)s.releaseFailureCredit();
    ReleaseSRWLockExclusive(&s.lock);return result;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_manual_reset_pending(void* pointer,unsigned stream){
    if(!pointer||!stream)return -1;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockShared(&s.lock);
    const int result=s.gmStream&&s.gmKind==1&&(s.gmActive||s.gmFatal)?(s.gmStream==stream?1:-1):0;
    ReleaseSRWLockShared(&s.lock);return result;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_manual_reset_abort(void* pointer,unsigned stream){
    if(!pointer||!stream)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.executionOwner||s.pendingStream||midiCCCaptureTotal(s)||s.compoundCount||!s.gmFatal||s.gmKind!=1||s.gmStream!=stream||
       (s.selection&&s.selection->produced.load()!=s.selection->consumed.load())){ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.gmStream=0;s.gmActive=false;ReleaseSRWLockExclusive(&s.lock);return 1;
}
// File transport Reset is NOT UI Reset or incoming SYSTEM. Original source:
// reset_file_transport_state -> per-channel _reset_channel_state -> apply_all
// -> all-channel reset defaults. Same emit/selection/semantic primitives;
// preserve the two default passes instead of collapsing their audio calls.
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_file_reset_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_selection_state_channel_origin_clear_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_file_reset_prepare(void* pointer,
        const unsigned* order,unsigned count,const unsigned* offsets,unsigned fieldCount,const unsigned* parameters,unsigned parameterCount){
    if(!pointer||(count&&!order)||!offsets||!parameters||count>16384||parameterCount>2000000)return 0;
    auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(!s.gmReady||!s.semantics||!s.selection||!s.notes||!s.resetAddressesReady||!s.resetReady||
       s.frameReceipt||s.pendingStream||s.gmActive||(s.executionOwner&&!s.executionPaused)||fieldCount!=s.semantics->fields.size()){
        ReleaseSRWLockExclusive(&s.lock);return 0;}
    const auto slot=s.slot(0x1d0000u);if(slot==256){ReleaseSRWLockExclusive(&s.lock);return 0;}
    try{
        std::vector<MidiCCKernel::FileExpression> rows;std::vector<std::vector<unsigned>> tables(fieldCount);
        for(unsigned i=0;i<fieldCount;++i){const auto& field=s.semantics->fields[i];const unsigned begin=offsets[i*2],length=offsets[i*2+1];
            if(begin>parameterCount||length>parameterCount-begin||(length&&length!=static_cast<unsigned>(field.high-field.low+1))){ReleaseSRWLockExclusive(&s.lock);return 0;}
            tables[i].assign(parameters+begin,parameters+begin+length);}
        for(unsigned i=0;i<count;++i){const unsigned channel=order[i*2],field=order[i*2+1];
            if(channel>=s.channels||field>fieldCount||(field<fieldCount&&tables[field].empty())){ReleaseSRWLockExclusive(&s.lock);return 0;}
            for(const auto& row:rows)if(row.channel==channel&&row.field==field){ReleaseSRWLockExclusive(&s.lock);return 0;}
            rows.push_back({channel,field});}
        s.fileExpressionOrder.swap(rows);s.fileParameterTables.swap(tables);s.fileOriginClearSlot=slot;s.fileResetReady=true;
    }catch(const std::bad_alloc&){ReleaseSRWLockExclusive(&s.lock);return 0;}
    ReleaseSRWLockExclusive(&s.lock);return 1;
}
static int midiCCFileResetContinue(MidiCCKernel& s,unsigned stream,void* timing){
    {MidiCCJournalLease lease(s);const int remaining=midiCCSemanticContinue(s,stream);
        if(remaining!=1){if(remaining==-2){lease.retain=true;s.pendingClockLease=true;}return remaining;}}
    s.pendingGMSemantic=false;auto& selection=*s.selection;auto& state=*s.semantics;
    auto emit=reinterpret_cast<BOOL(WINAPI*)(HSTREAM,DWORD,DWORD,DWORD)>(s.emitAddress);
    auto error=reinterpret_cast<int(WINAPI*)()>(s.errorAddress);
    while(s.gmActive){
        const unsigned budget=selection.need();auto reserved=selection.reserved.load(std::memory_order_acquire);
        for(;;){if(!selection.room(budget)||selection.produced.load()-selection.consumed.load()+reserved+budget>selection.recordLimit.load())return -6;
            if(selection.reserved.compare_exchange_weak(reserved,reserved+budget,std::memory_order_acq_rel))break;}
        s.journal->clock->writers.fetch_add(1,std::memory_order_acq_rel);
        auto release=[&](){s.journal->clock->writers.fetch_sub(1,std::memory_order_release);selection.reserved.fetch_sub(budget,std::memory_order_release);};
        auto fatal=[&](){s.gmFatal=-8;s.gmError=error();s.gmActive=false;release();return -8;};
        auto event=[&](unsigned channel,unsigned kind,unsigned raw){const bool accepted=emit(stream,channel,kind,raw)!=FALSE;const int code=accepted?0:error();
            if(kind==MIDI_EVENT_RESET)midiCCObserveResetResult(s,stream,kind,channel,raw,accepted,code);
            selection.write(channel,kind,raw,s.gmPhase==1?30u:s.fileResetSource,accepted?1u:0u,code);return accepted;};
        const unsigned channel=s.gmChannel<s.gmSelection.size()?s.gmSelection[s.gmChannel]:0;
        if(s.gmPhase==0){
            if(timing){auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(s.lockAddress);if(!lock(stream,TRUE))return fatal();
                playback_timing_reset(timing);if(!lock(stream,FALSE)){s.pendingStream=stream;s.pendingThread=GetCurrentThreadId();s.gmPhase=1;release();return -2;}}
            s.gmPhase=1;s.gmChannel=0;s.fileResetSubphase=0;
        }else if(s.gmPhase==1){
            if(s.fileResetSubphase==0){
                // clear_origins(channel) precedes the first direct audio call.
                s.journal->receive(stream,s.fileOriginClearSlot,channel<<16);s.fileResetSubphase=1;
            }else if(s.fileResetSubphase==1){
                event(channel,MIDI_EVENT_SOUNDOFF,0);event(channel,MIDI_EVENT_RESET,0);event(channel,MIDI_EVENT_NOTESOFF,0);s.fileResetSubphase=2;
            }else if(s.fileResetSubphase==2){
                if(!midiCCCaptureEnsureRoom(s)){release();return -6;}
                const auto before=s.resetLearnRevision;if(s.resetArmChannel==channel){s.resetArmChannel=UINT_MAX;++s.resetLearnRevision;state.armed=false;state.armRevision=s.resetLearnRevision;}
                for(auto& field:state.fields){field.present[channel]=false;field.modPresent[channel]=false;field.timbrePresent[channel]=false;
                    field.modSerial[channel]=field.timbreSerial[channel]=s.journal->clock->serial.load();}
                state.modePresent[channel]=false;std::fill_n(s.notes->counts.data()+channel*128,128,0ull);
                LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);const MidiNoteAction clear{0xb0u|(120u<<8),channel,0,static_cast<unsigned long long>(tick.QuadPart)};
                s.notes->observeActivity(clear,{});
                selection.resetRouting(channel);selection.resetSession(channel);
                for(unsigned cc=0;cc<120;++cc)s.journal->receive(stream,s.auxSlots[cc],channel<<16);
                const auto serial=s.journal->clock->serial.fetch_add(1,std::memory_order_acq_rel)+1;
                s.captures[(s.captureHead+s.captureCount++)%static_cast<unsigned>(s.captures.size())]={serial,before,s.resetLearnRevision,channel,0,0,134,0,0,0,0,0};
                s.fileResetSubphase=3;
            }else if(s.fileResetSubphase==3){
                for(const auto& item:s.gmDefaults){const auto system=item.flags&1u?selection.get(stream,0,MIDI_EVENT_SYSTEM):0;
                    event(channel,item.event,(item.flags&1u)&&(system==3||system==4||system==5)?0:item.raw);}s.fileResetSubphase=4;
            }else if(s.fileResetSubphase==4||s.fileResetSubphase==5){
                // Original receive_cc91/93 uses one send-domain lock each.
                // Advance after its accepted call before retained unlock.
                const unsigned effect=s.fileResetSubphase-4,kind=effect?MIDI_EVENT_CHORUS:MIDI_EVENT_REVERB;
                const unsigned request=effect?0u:s.gmDefaultReverb,floors=s.sends->floors.load(std::memory_order_acquire),floor=(floors>>(effect*7))&127u;
                MidiSendResult row{};int status[3]{};const int result=midi_send_domain_emit(s.sends,stream,channel,kind,floor,request,s.emitAddress,s.lockAddress,s.errorAddress,&row,status);
                if(result<=0&&result!=-2)return fatal();
                if(row.accepted){s.journal->receive(stream,s.requestSlots[effect],(channel<<16)|request);selection.write(channel,kind,row.raw,30,1);}
                ++s.fileResetSubphase;
                if(result==-2){s.pendingStream=stream;s.pendingThread=GetCurrentThreadId();release();return -2;}
            }else if(s.fileResetSubphase==6){
                // Same dedicated UserFX/tap send operator as ordinary CC94.
                for(auto* tap:s.delays[channel])if(tap)delayMidiSend(tap,0);
                s.journal->receive(stream,s.slot(0xb0000u+MIDI_EVENT_USERFX),channel<<16);s.fileResetSubphase=7;
            }else if(s.fileResetSubphase==7){
                selection.observeWatches(channel);
                const int selected=selection.resetInstrument(channel,7,channel%16==9?128u:0u,0,0);if(selected==-8)return fatal();
                const auto& address=selection.channels[channel];auto* request=s.fontRequests.data()+channel*4;
                request[0]=address.drums?128u:address.bank;request[1]=address.program;request[2]=address.lsb;s.fileResetSubphase=8;
            }else if(s.fileResetSubphase==8){
                constexpr unsigned kinds[]{MIDI_EVENT_VOLUME,MIDI_EVENT_PAN,MIDI_EVENT_MODULATION,MIDI_EVENT_PITCH,MIDI_EVENT_SUSTAIN,MIDI_EVENT_SOSTENUTO,MIDI_EVENT_SOFT,MIDI_EVENT_CHANPRES};
                constexpr unsigned values[]{100,64,0,8192,0,0,0,0};for(unsigned i=0;i<8;++i)event(channel,kinds[i],values[i]);s.fileResetSubphase=9;
            }else if(s.fileResetSubphase==9){
                if(s.gmExtensions&&!selection.resetExtensions(channel))return fatal();s.fileResetSubphase=10;
            }else if(s.fileResetSubphase==10){
                const auto& address=s.resetAddresses[channel];
                selection.observeWatches(channel);
                if(address[0]&&selection.resetInstrument(channel,address[0],address[1],address[2],address[3])==-8)return fatal();
                s.fileResetSubphase=11;
            }else{
                const auto first=state.resetFirst[channel],count=state.resetCount[channel];
                if(++s.gmChannel==s.gmChannels){s.gmPhase=2;s.gmChannel=0;}s.fileResetSubphase=0;
                const int applied=count?midiCCSemanticApply(s,stream,channel,0,first,first+count,nullptr,true):1;
                if(applied!=1){s.pendingGMSemantic=true;release();return applied;}
            }
        }else if(s.gmPhase==2){for(const auto& item:s.gmMasters)event(0,item.event,item.raw);s.gmPhase=3;s.gmChannel=0;
        }else if(s.gmPhase==3){int bank=-1,program=-1;selection.profile(channel,selection.current(channel,bank,program),true);
            if(++s.gmChannel==s.gmChannels){s.gmPhase=4;s.gmChannel=0;}
        }else if(s.gmPhase==4){
            unsigned raw=127;for(unsigned field=0;field<state.fields.size();++field)if(state.fields[field].event==MIDI_EVENT_PORTAMENTO&&state.fields[field].present[channel]){
                const auto& f=state.fields[field];raw=s.fileParameterTables[field][static_cast<unsigned>(f.saved[channel]-f.low)];break;}
            event(channel,MIDI_EVENT_PORTAMENTO,raw);if(++s.gmChannel==s.gmChannels){s.gmPhase=5;s.gmChannel=0;}
        }else if(s.gmPhase==5){
            // Bound each reservation to one original expression call.
            if(s.gmChannel<s.fileExpressionOrder.size()){
                const auto& row=s.fileExpressionOrder[s.gmChannel++];
                if(row.field==state.fields.size()){
                    if(state.modePresent[row.channel])event(row.channel,MIDI_EVENT_MODE,static_cast<unsigned>(state.modeSaved[row.channel]));
                }else{const auto& field=state.fields[row.field];
                    if(field.present[row.channel]&&field.event!=MIDI_EVENT_PORTAMENTO)
                        event(row.channel,field.event,s.fileParameterTables[row.field][static_cast<unsigned>(field.saved[row.channel]-field.low)]);}
            }else{s.gmPhase=6;s.gmChannel=0;}
        }else if(s.gmPhase==6){
            // Source's second, whole-channel default pass is NOT redundant.
            const auto& address=s.resetAddresses[channel];if(s.gmExtensions&&!selection.resetExtensions(channel))return fatal();
            selection.observeWatches(channel);
            if(address[0]&&selection.resetInstrument(channel,address[0],address[1],address[2],address[3])==-8)return fatal();
            const auto first=state.resetFirst[channel],count=state.resetCount[channel];
            if(++s.gmChannel==s.gmChannels){s.gmPhase=7;s.gmChannel=0;}
            const int applied=count?midiCCSemanticApply(s,stream,channel,0,first,first+count,nullptr,true):1;
            if(applied!=1){s.pendingGMSemantic=true;release();return applied;}
        }else{s.gmActive=false;s.gmPhase=s.gmChannel=0;}
        release();
    }return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_file_reset_run(void* pointer,unsigned stream,void* timing,unsigned source){
    if(!pointer||!stream||(source!=29&&source!=31))return -1;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(!s.fileResetReady||s.cancelledStream||s.pendingStream||(s.executionOwner&&s.executionThread.load()!=GetCurrentThreadId())){ReleaseSRWLockExclusive(&s.lock);return -3;}
    if(s.gmFatal){const int result=s.gmFatal;ReleaseSRWLockExclusive(&s.lock);return result;}
    if(s.gmActive&&(s.gmKind!=2||s.gmStream!=stream||s.fileResetSource!=source)){ReleaseSRWLockExclusive(&s.lock);return -1;}
    MidiNoteLease notes(s.notes);MidiActivityLease activity(s.notes,nullptr,0,0,(!s.gmActive||s.gmPhase<=1)?s.gmChannels:0u,&s.pendingStream);
    if(!activity.ready){ReleaseSRWLockExclusive(&s.lock);return -4;}
    if(!s.gmActive){const auto ready=midiCCReserveFailures(s,midiCCResetFailureBudget(s)*2);if(ready!=1){ReleaseSRWLockExclusive(&s.lock);return ready;}
        s.gmActive=true;s.gmKind=2;s.fileResetSource=source;s.gmStream=stream;s.gmPhase=s.gmChannel=s.fileResetSubphase=0;}
    const int result=midiCCFileResetContinue(s,stream,timing);if((result==1&&!s.gmActive)||result==-8)s.releaseFailureCredit();
    ReleaseSRWLockExclusive(&s.lock);return result;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_file_reset_pending(void* pointer,unsigned stream){
    if(!pointer||!stream)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockShared(&s.lock);
    const int result=s.gmStream&&s.gmKind==2&&(s.gmActive||s.gmFatal)?(s.gmStream==stream?1:-1):0;
    ReleaseSRWLockShared(&s.lock);return result;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_file_reset_abort(void* pointer,unsigned stream){
    if(!pointer||!stream)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.executionOwner||s.pendingStream||midiCCCaptureTotal(s)||s.compoundCount||!s.gmFatal||s.gmKind!=2||s.gmStream!=stream||
       (s.selection&&s.selection->produced.load()!=s.selection->consumed.load())){ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.gmStream=0;s.gmActive=false;ReleaseSRWLockExclusive(&s.lock);return 1;
}
// Source byte-walker passive family, NOT normalized decoded MIDI. In particular
// implicit running-status notes have no original app velocity/overlap/preload or
// explicit-Note timing registration. BASS alone decodes these unchanged bytes.
static bool midiCCPassiveOriginal(const MidiCCKernel& s,unsigned port,const unsigned char* input,unsigned length,bool selection=false){
    unsigned status=0;bool known=false;
    if(s.rawContext){AcquireSRWLockShared(&s.rawContext->lock);status=s.rawContext->state.status;known=s.rawContext->state.valid;ReleaseSRWLockShared(&s.rawContext->lock);}
    for(unsigned i=0;i<length;){const unsigned byte=input[i++];
        if(byte<128u){
            if(!known)return false;
            const unsigned command=status&0xf0u,channel=port*16+(status&15u);
            // Running Bank/Program/RPN may produce selection callbacks which
            // need original identities; do not bypass their owned-ticket gate.
            if(status&&((!selection&&(command==0xb0u||command==0xc0u))||channel>=s.channels))return false;
            if(status&&(command==0xa0u||command==0xd0u||command==0xe0u)){
                const unsigned slot=command==0xa0u?120u:command==0xd0u?121u:122u;
                if(s.blocked[channel*128+slot])return false;
            }
            continue;
        }
        // F9/FD are reserved, separately audited, but the legacy walk sends
        // them verbatim just like defined realtime. FF is BASS meta, not here.
        if(byte>=0xf8u&&byte<=0xfeu)continue;
        unsigned count=0;
        if(byte==0xf1u||byte==0xf3u)count=1;
        else if(byte==0xf2u)count=2;
        else if(byte!=0xf6u&&byte!=0xf7u)return false;
        if(count>length-i)return false;
        for(unsigned j=0;j<count;++j)if(input[i+j]>=128u)return false;
        i+=count;status=0;known=true;
    }
    return true;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_passive_raw_version(){return 1;}
// Full, valid original SysEx. The decoder context/selection preparation is the
// same existing BASS ConvertEvents contract; no hand-written Roland/XG audio
// decoder or fake completed ASYNC value is introduced. This input-side helper
// may prepare metadata storage, but never runs from a DSP/filter callback.
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_generic_raw_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_generic_raw_prepare(void* pointer,void* conversion,unsigned rate,
        const unsigned* presets,unsigned presetCount,const unsigned* times,const unsigned* feedback,unsigned count){
    if(!pointer||!conversion||!rate||!presets||presetCount!=40||!times||!feedback||count!=128)return 0;
    auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.frameReceipt||s.pendingStream||s.genericFault||!s.gmReady||!s.rawContext||!s.selection||
       (s.executionOwner&&!s.executionPaused)){ReleaseSRWLockExclusive(&s.lock);return 0;}
    for(unsigned i=0;i<10;++i)if(presets[i*4]>3||presets[i*4+1]>200||!presets[i*4+2]||presets[i*4+2]>2000||presets[i*4+3]>100){ReleaseSRWLockExclusive(&s.lock);return 0;}
    for(unsigned i=0;i<128;++i)if(times[i]<10||times[i]>1000||feedback[i]>100){ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.genericConversion=conversion;s.genericRate=rate;
    for(unsigned i=0;i<10;++i)std::copy_n(presets+i*4,4,s.genericDelayPresets[i].begin());
    std::copy_n(times,128,s.genericDelayTimes.begin());std::copy_n(feedback,128,s.genericDelayFeedback.begin());
    s.genericReady=true;ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_generic_raw_abort(void* pointer,unsigned stream){
    if(!pointer||!stream)return 0;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(!s.genericFault){ReleaseSRWLockExclusive(&s.lock);return 1;}
    if(s.genericFaultStream!=stream||s.executionOwner||s.pendingStream||midiCCCaptureTotal(s)||s.compoundCount||s.failureRowsPending()||
       !s.selection||s.selection->produced.load()!=s.selection->consumed.load()||s.selection->reserved.load()||
       s.selection->queuedReservation.load()||s.selection->unticketedReservation.load()||s.selection->failedTicket()){
        ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.genericFault=0;s.genericFaultStream=0;s.clearFrameReceipt();ReleaseSRWLockExclusive(&s.lock);return 1;
}
// Allocate the exact 48-bit journal identity BEFORE a SysEx can change taps.
// CAS shares the clock with every real writer; a load-only preflight races.
static unsigned long long midiCCSysExCaptureSerial(NativeMidiClock& clock){
    auto before=clock.serial.load(std::memory_order_acquire);
    for(;;){
        if(before>=(1ull<<48)-1)return 0;
        if(clock.serial.compare_exchange_weak(before,before+1,std::memory_order_acq_rel,std::memory_order_acquire))return before+1;
    }
}
static int midiCCWholeSysEx(MidiCCKernel& s,unsigned stream,unsigned mode,unsigned port,
        const unsigned char* input,unsigned length,unsigned char* output,unsigned long long* result,void* timing,const unsigned long long* origin){
    if(!s.genericReady||s.genericFault||length<2||input[0]!=0xf0u||input[length-1]!=0xf7u)return 0;
    for(unsigned i=1;i+1<length;++i)if(input[i]>=128u)return 0; // Source lookahead traps remain separately owned.
    unsigned bytes=0;if(s.channels>16){const unsigned char prefix[]{0xff,0x21,1,static_cast<unsigned char>(port)};std::copy_n(prefix,4,output);bytes=4;}
    std::copy_n(input,length,output+bytes);bytes+=length;
    int conversionResult[3]{};
    auto* prepared=static_cast<NativeRawDecodePlan*>(raw_decode_plan_prepare(s.rawContext,mode,output,bytes,
        s.genericConversion,s.errorAddress,conversionResult));
    if(!prepared)return 0;
    std::unique_ptr<NativeRawDecodePlan> plan(prepared);
    if(!plan->selectionQualified||!plan->qualified)return 0;
    // Budget all decoded selection callbacks plus every source address refresh
    // and the final existing graph sync BEFORE changing delay or sending audio.
    auto& selection=*s.selection;const unsigned refreshBudget=(s.gmChannels+1u)*selection.need();
    NativeSelectionState::StoragePreparation storage;
    if(!selection.stageCapacityOwned(plan->selections,refreshBudget,storage))return -6;
    std::vector<unsigned> identityOrder;
    if(plan->selections&&!selection.stageIdentityOrder(plan->selectionEvents.data(),plan->selections,0,identityOrder))return -6;
    auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(s.lockAddress);
    if(!lock(stream,TRUE))return -6;
    const bool capacity=selection.publishCapacity(plan->selections,refreshBudget,storage);
    if(!lock(stream,FALSE)){s.pendingStream=stream;s.pendingThread=GetCurrentThreadId();selection.retainPreparation(std::move(storage));return -2;}
    if(!capacity)return -6;
    unsigned delayKind=0,delayValue=0;
    if(s.gmDelay&&length==11&&input[1]==0x41&&input[2]==0x10&&input[3]==0x42&&input[4]==0x12&&input[5]==0x40&&input[6]==1&&
       input[9]==128u-((static_cast<unsigned>(input[5])+input[6]+input[7]+input[8])&127u)){
        if(input[7]==0x50){delayKind=1;delayValue=(input[8]<=7?input[8]:0)+2;}
        else if(input[7]==0x52){delayKind=2;delayValue=s.genericDelayTimes[input[8]];}
        else if(input[7]==0x58){delayKind=3;delayValue=s.genericDelayFeedback[input[8]];}
    }
    if(delayKind&&!midiCCCaptureEnsureRoom(s))return -6;
    auto reserved=selection.reserved.load(std::memory_order_acquire);
    for(;;){if(!selection.room(refreshBudget)||selection.produced.load()-selection.consumed.load()+reserved+refreshBudget>selection.recordLimit.load())return -6;
        if(selection.reserved.compare_exchange_weak(reserved,reserved+refreshBudget,std::memory_order_acq_rel))break;}
    struct TailBudget {NativeSelectionState& state;unsigned amount;~TailBudget(){state.reserved.fetch_sub(amount,std::memory_order_release);}} tail{selection,refreshBudget};
    const auto ticket=plan->selections?selection.ticket(plan->selections):0;
    if(plan->selections&&(!ticket||!selection.identifyTicket(ticket,plan->selectionEvents.data(),plan->selections,0,identityOrder.data()))){
        if(ticket)selection.settleTicket(ticket,3,false);return -6;}
    MidiCCJournalLease lease(s);
    const auto delaySerial=delayKind?midiCCSysExCaptureSerial(*s.journal->clock):0;
    if(delayKind&&!delaySerial){
        if(ticket)selection.settleTicket(ticket,3,false);
        s.genericFault=-8;s.genericFaultStream=stream;result[4]=static_cast<unsigned>(-1);return -8;
    }
    if(delayKind){
        // Exact original distinction: preset changes melodic-nine too;
        // time/feedback changes only states[0..127], not melodic-nine dry.
        for(unsigned ch=0;ch<128;++ch)for(unsigned wet=0;wet<2;++wet){auto* tap=s.delays[ch][wet];if(!tap||(delayKind!=1&&ch==9&&!wet))continue;
            const auto old=tap->parameters.load(std::memory_order_acquire);unsigned frames=static_cast<unsigned>(old),kind=static_cast<unsigned>((old>>32)&3u),
                feedback=static_cast<unsigned>((old>>34)&127u),level=static_cast<unsigned>((old>>48)&255u);
            if(delayKind==1){const auto& values=s.genericDelayPresets[delayValue];kind=values[0];level=values[1];frames=std::max(1u,static_cast<unsigned>(static_cast<unsigned long long>(s.genericRate)*values[2]/1000));feedback=values[3];}
            else if(delayKind==2)frames=std::max(1u,static_cast<unsigned>(static_cast<unsigned long long>(s.genericRate)*delayValue/1000));
            else feedback=delayValue;
            if(!delay_midi_controls(tap,frames,kind,feedback,level)){s.genericFault=-8;s.genericFaultStream=stream;result[4]=static_cast<unsigned>(-1);return -8;}
        }
        s.captures[(s.captureHead+s.captureCount++)%static_cast<unsigned>(s.captures.size())]={delaySerial,0,0,delayKind,delayValue,0,133,0,0,0,0,0};
    }
    if(ticket)selection.settleTicket(ticket,1,false);
    NativeSelectionSubmissionScope scope{&selection,ticket};
    const DWORD admitted=s.rawEmit(stream,mode,output,bytes);
    const int error=admitted==UINT_MAX?reinterpret_cast<int(WINAPI*)()>(s.errorAddress)():0;
    raw_decode_context_observe(s.rawContext,stream,mode,output,bytes,admitted!=UINT_MAX,error);
    result[0]=bytes;result[2]=1;result[3]=admitted==UINT_MAX?0:admitted;result[4]=static_cast<unsigned>(error);result[5]=length;
    if(admitted==UINT_MAX){if(ticket)selection.settleTicket(ticket,2,false);if(timing&&origin[0])playback_timing_cancel(timing,origin[0]);return 2;}
    // Source post-RAW order: channel9 restore, complete address readback/apply,
    // then drum/melodic topology synchronization. Deferred RAW deliberately
    // reads exactly the source's currently observable values, never a guessed
    // future SysEx value or FLUSH to force completion.
    auto emit=reinterpret_cast<BOOL(WINAPI*)(HSTREAM,DWORD,DWORD,DWORD)>(s.emitAddress);
    emit(stream,9,MIDI_EVENT_USERFX,selection.channels[9].drums?127u:0u);
    for(unsigned channel:s.gmSelection){
        if(!selection.resetRefresh(channel)){s.genericFault=-8;s.genericFaultStream=stream;result[4]=static_cast<unsigned>(selection.error());return -8;}
        const auto& address=selection.channels[channel];auto* request=s.fontRequests.data()+channel*4;
        request[0]=address.drums?128u:address.bank;request[1]=address.program;request[2]=address.lsb;
        s.resetDrums[channel]=address.drums;
    }
    if(!selection.syncGraph()){s.genericFault=-8;s.genericFaultStream=stream;result[4]=static_cast<unsigned>(selection.error());return -8;}
    return 1;
}
// Implicit CC/Program belongs to BASS's persistent RAW decoder, not the
// app's explicit-byte interception semantics. Prepare only original selection
// callback identities; never insert status bytes, synthesize Note actions,
// arm Learn, apply app CC6/94/send conversion or split this RAW submission.
static int midiCCImplicitOriginal(MidiCCKernel& s,unsigned stream,unsigned mode,unsigned port,
        const unsigned char* input,unsigned length,unsigned char* output,unsigned long long* result,void* timing,const unsigned long long* origin){
    if(!s.genericReady||!s.selection||!s.rawContext||!midiCCPassiveOriginal(s,port,input,length,true))return 0;
    unsigned bytes=0;if(s.channels>16){const unsigned char prefix[]{0xff,0x21,1,static_cast<unsigned char>(port)};std::copy_n(prefix,4,output);bytes=4;}
    std::copy_n(input,length,output+bytes);bytes+=length;
    int conversionResult[3]{};
    auto* prepared=static_cast<NativeRawDecodePlan*>(raw_decode_plan_prepare(s.rawContext,mode,output,bytes,
        s.genericConversion,s.errorAddress,conversionResult));
    if(!prepared)return 0;
    std::unique_ptr<NativeRawDecodePlan> plan(prepared);
    // RPN uncertainty affects full value conversion, not these exact decoded
    // Bank/Program identities. Keep original bytes and selection-only domain.
    if(!plan->selectionQualified)return 0;
    auto& selection=*s.selection;NativeSelectionState::StoragePreparation storage;
    if(!selection.stageCapacityOwned(plan->selections,0,storage))return -6;
    std::vector<unsigned> identityOrder;
    if(plan->selections&&!selection.stageIdentityOrder(plan->selectionEvents.data(),plan->selections,0,identityOrder))return -6;
    if(plan->selections){
        auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(s.lockAddress);
        if(!lock(stream,TRUE))return -6;
        const bool capacity=selection.publishCapacity(plan->selections,0,storage);
        if(!lock(stream,FALSE)){s.pendingStream=stream;s.pendingThread=GetCurrentThreadId();selection.retainPreparation(std::move(storage));return -2;}
        if(!capacity)return -6;
    }
    const auto ticket=plan->selections?selection.ticket(plan->selections):0;
    if(plan->selections&&(!ticket||!selection.identifyTicket(ticket,plan->selectionEvents.data(),plan->selections,0,identityOrder.data()))){
        if(ticket)selection.settleTicket(ticket,3,false);return -6;}
    MidiCCJournalLease lease(s);
    if(ticket)selection.settleTicket(ticket,1,false);
    NativeSelectionSubmissionScope scope{&selection,ticket};
    const DWORD admitted=s.rawEmit(stream,mode,output,bytes);
    const int error=admitted==UINT_MAX?reinterpret_cast<int(WINAPI*)()>(s.errorAddress)():0;
    raw_decode_context_observe(s.rawContext,stream,mode,output,bytes,admitted!=UINT_MAX,error);
    result[0]=bytes;result[2]=1;result[3]=admitted==UINT_MAX?0:admitted;result[4]=static_cast<unsigned>(error);result[5]=length;
    if(admitted==UINT_MAX){if(ticket)selection.settleTicket(ticket,2,false);if(timing&&origin[0])playback_timing_cancel(timing,origin[0]);return 2;}
    return 1;
}
static int midiCCRun(void* pointer,unsigned stream,unsigned mode,unsigned port,
        const unsigned char* input,unsigned length,unsigned char* output,unsigned capacity,MidiCCAction* actions,unsigned actionCapacity,
        unsigned long long* result,MidiNoteAction* noteActions=nullptr,unsigned noteCapacity=0,unsigned long long* noteCount=nullptr,
        void* timing=nullptr,const unsigned long long* origin=nullptr,bool receiptAuthorized=false){
    // [wire bytes,actions,raw calls,raw admitted,signed error bits,consumed bytes,
    //  individual send failures,individual send calls].1=finished,2=raw failure,
    // -2=unlock pending,-3=previous unlock pending,0=unsupported,-1=ABI.
    if(result)std::fill_n(result,8,0ull);
    if(noteCount)*noteCount=0;
    constexpr unsigned allowed=BASS_MIDI_EVENTS_RAW|BASS_MIDI_EVENTS_FILTER|BASS_MIDI_EVENTS_SYNC|BASS_MIDI_EVENTS_ASYNC;
    if(!pointer||!stream||!input||!output||!actions||!result||port>=8||mode&~allowed||
        (mode&BASS_MIDI_EVENTS_RAW)!=BASS_MIDI_EVENTS_RAW)return -1;
    const bool mixed=noteActions&&noteCount&&origin;
    if(!length||length>1024*1024||(!mixed&&length%3))return 0; // Product resource policy, NOT MIDI maximum.
    if(capacity<length*5+4||actionCapacity<length/3||(mixed&&noteCapacity<length/3))return -1;
    auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.executionOwner&&s.executionThread.load()!=GetCurrentThreadId()){ReleaseSRWLockExclusive(&s.lock);return -5;}
    if(s.pendingStream){ReleaseSRWLockExclusive(&s.lock);return -3;}
    if(s.journal&&s.journal->stream!=stream){ReleaseSRWLockExclusive(&s.lock);return -1;}
    // A public caller cannot prove that new bytes are the original suffix.
    // Explicit semantic_resume owns the old receipt; never replay or finish
    // its prefix merely because a different frame uses the same stream.
    if((s.frameReceipt&&!receiptAuthorized)||s.semanticActive||(s.semanticFramePending&&!receiptAuthorized)){
        ReleaseSRWLockExclusive(&s.lock);return -3;}
    if(s.selection&&s.selection->failedTicket()){
        // Same bounded prefix-ACK barrier as metadata pressure, before ANY
        // new frame's parser/audio/state mutation. The previous failure is not
        // retried: its remaining budget retires only after native queues drain.
        ReleaseSRWLockExclusive(&s.lock);return -6;
    }
    if(mixed&&!s.notes){ReleaseSRWLockExclusive(&s.lock);return -1;}
    if(mixed&&s.genericReady&&input[0]==0xf0u){
        const int resultCode=midiCCWholeSysEx(s,stream,mode,port,input,length,output,result,timing,origin);
        ReleaseSRWLockExclusive(&s.lock);return resultCode;
    }
    if(mixed&&midiCCPassiveOriginal(s,port,input,length)){
        // Reuse the ordinary RAW ABI, port prefix, writer lease, decoder result
        // and retained receipt. No callback/heap allocation/selection tickets,
        // no app note actions or invented registration for implicit BASS notes.
        MidiCCJournalLease lease(s);unsigned bytes=0;
        if(s.channels>16){const unsigned char prefix[]{0xff,0x21,1,static_cast<unsigned char>(port)};
            std::copy_n(prefix,4,output);bytes=4;}
        std::copy_n(input,length,output+bytes);bytes+=length;
        const unsigned admitted=s.rawEmit(stream,mode,output,bytes);
        const int code=admitted==UINT_MAX?reinterpret_cast<int(WINAPI*)()>(s.errorAddress)():0;
        if(s.rawContext)raw_decode_context_observe(s.rawContext,stream,mode,output,bytes,admitted!=UINT_MAX,code);
        result[0]=bytes;result[2]=1;result[3]=admitted==UINT_MAX?0:admitted;
        result[4]=static_cast<unsigned>(code);result[5]=length;
        if(admitted==UINT_MAX&&timing&&origin[0])playback_timing_cancel(timing,origin[0]);
        ReleaseSRWLockExclusive(&s.lock);return admitted==UINT_MAX?2:1;
    }
    if(mixed&&s.genericReady&&midiCCPassiveOriginal(s,port,input,length,true)){
        const int original=midiCCImplicitOriginal(s,stream,mode,port,input,length,output,result,timing,origin);
        ReleaseSRWLockExclusive(&s.lock);return original;
    }
    // Armed successors can contain compound clear actions. The whole mixed
    // consumer owns their original action FIFO; a CC-only probe cannot.
    if(!mixed&&s.semantics&&s.semantics->armed){ReleaseSRWLockExclusive(&s.lock);return 0;}
    // Original app bytewalker forwards low data bytes without interpreting
    // their implicit status. Keep that source distinction even inside one
    // frame containing explicit channel messages. BASS decodes the resulting
    // unchanged data; only actual RAW selection identities are prepared below.
    bool implicitData=false;
    auto originalSpan=[&](unsigned at){return input[at]<128u?1u:midiOriginalSpan(input,length,at);};
    for(unsigned at=0;at<length;){const unsigned width=originalSpan(at);if(!width){ReleaseSRWLockExclusive(&s.lock);return 0;}
        implicitData=implicitData||input[at]<128u;at+=width;}
    if(implicitData){
        bool ready=false;if(s.rawContext){AcquireSRWLockShared(&s.rawContext->lock);ready=s.rawContext->state.valid;ReleaseSRWLockShared(&s.rawContext->lock);}
        if(!mixed||!s.genericReady||!s.selection||!ready||!s.executionStop){ReleaseSRWLockExclusive(&s.lock);return 0;}
    }
    std::unique_ptr<NativeRawSelectionScratch> implicitScratch;
    if(implicitData){try{implicitScratch=std::make_unique<NativeRawSelectionScratch>(length*5u+4u);}
        catch(const std::bad_alloc&){ReleaseSRWLockExclusive(&s.lock);return 0;}}
    unsigned requiredNotes=0;
    unsigned requiredCaptures=0;
    unsigned requiredSelections=0;
    unsigned long long requiredFailures=0;
    std::array<bool,128> resetFailureChannels{};
    unsigned anticipatedCapture=120;
    bool anticipatedArmed=s.semantics&&s.semantics->armed;
    auto projected=[&](unsigned channel,unsigned cc,unsigned& first,unsigned& count,unsigned& blocked){
        const unsigned index=channel*128+cc;first=s.semantics?s.semantics->first[index]:0;count=s.semantics?s.semantics->counts[index]:0;blocked=s.blocked[index];
        if(anticipatedCapture<120)for(const auto& patch:s.semantics->successors[anticipatedCapture])if(patch.channel==channel&&patch.cc==cc){first=patch.first;count=patch.count;blocked=patch.blocked;break;}
    };
    for(unsigned i=0;i<length;){
        const unsigned width=originalSpan(i);
        if(!width){ReleaseSRWLockExclusive(&s.lock);return 0;}
        if(input[i]<128u){i+=width;continue;}
        if(input[i]>=0xf0u){if(!mixed){ReleaseSRWLockExclusive(&s.lock);return 0;}i+=width;continue;}
        const unsigned command=input[i]&0xf0u,channel=port*16+(input[i]&15u);
        if(i+width>length||channel>=s.channels||input[i+1]>127||(width==3&&input[i+2]>127)){
            ReleaseSRWLockExclusive(&s.lock);return 0;
        }
        if(command==0xb0u){
            const unsigned cc=input[i+1];
            if(cc<120&&anticipatedArmed&&(s.semantics->armAll||s.semantics->armChannel==channel)){
                if(channel>=s.semantics->armActive){ReleaseSRWLockExclusive(&s.lock);return 0;}
                if(anticipatedCapture==120){++requiredCaptures;anticipatedCapture=cc;anticipatedArmed=false;}
            }
            unsigned first=0,count=0,blocked=0;projected(channel,cc,first,count,blocked);
            const bool intercepted=cc<120&&count!=0;
            if((cc==0||cc==32)&&!intercepted)++requiredSelections;
            const bool transport=cc==120||cc==121||cc>=123;
            if((cc>=120&&(!mixed||!transport||!s.resetReady||(cc==121&&!s.resetAdmitted[channel])))||((cc==0||cc==32)&&!intercepted&&!s.selection)||(cc<120&&blocked)||((s.options&2)&&cc==94&&!intercepted&&!s.delayEnabled[channel])){
                ReleaseSRWLockExclusive(&s.lock);return 0;
            }
            if(transport){++requiredNotes;if(cc==121){++requiredCaptures;
                resetFailureChannels[channel]=true;
                if(anticipatedArmed&&s.semantics->armChannel==channel)anticipatedArmed=false;}}
            if(intercepted){const unsigned end=first+count;
                requiredFailures+=midiCCFailureBudget(*s.semantics,first,end);
                for(unsigned r=first;r<end;++r){const auto& route=s.semantics->route(r);
                    if(route.event==MIDI_EVENT_USERFX&&!s.delayEnabled[channel]){ReleaseSRWLockExclusive(&s.lock);return 0;}
                    if(route.flags&24u){if(!mixed||!s.semantics->modeReady){ReleaseSRWLockExclusive(&s.lock);return 0;}++requiredNotes;}}}
        }else if(!mixed||(command!=0x80u&&command!=0x90u&&command!=0xa0u&&command!=0xd0u&&command!=0xe0u&&(command!=0xc0u||!s.selection))){
            ReleaseSRWLockExclusive(&s.lock);return 0;
        }
        if(command==0xa0u||command==0xd0u||command==0xe0u){
            const unsigned slot=command==0xa0u?120:command==0xd0u?121:122;
            if(s.blocked[channel*128+slot]){ReleaseSRWLockExclusive(&s.lock);return 0;}
        }
        if(command==0xc0u)++requiredSelections;
        if(command==0x90u&&input[i+2]&&s.firstuse){const unsigned* request=s.fontRequests.data()+channel*4;
            if(s.fontStream!=stream||
                !font_preload_plan_qualify(s.firstuse,s.fontEpoch,s.fontGeneration,stream,s.fontMode,request[0],request[1],request[2],request[3])){ReleaseSRWLockExclusive(&s.lock);return 0;}}
        if(command==0x80u||command==0x90u)++requiredNotes;
        i+=width;
    }
    if(!s.retainedCaptureEnabled&&requiredCaptures>s.captures.size()){
        // One RAW frame may contain more resets than the finite metadata
        // FIFO. It cannot ever fit even after ACK, so use the exact legacy
        // handoff before any prefix effect, rather than permanent pressure.
        ReleaseSRWLockExclusive(&s.lock);return 0;
    }
    if(!midiCCCaptureEnsureRoom(s,requiredCaptures)){ReleaseSRWLockExclusive(&s.lock);return -6;}
    if(mixed&&noteCapacity<requiredNotes){ReleaseSRWLockExclusive(&s.lock);return -1;}
    if(s.semantics)for(unsigned channel=0;channel<128;++channel)if(resetFailureChannels[channel]){
        const unsigned first=s.semantics->resetFirst[channel];
        requiredFailures+=midiCCFailureBudget(*s.semantics,first,first+s.semantics->resetCount[channel]);
    }
    const int failuresReady=midiCCReserveFailures(s,requiredFailures);
    if(failuresReady!=1){ReleaseSRWLockExclusive(&s.lock);return failuresReady;}
    MidiNoteLease noteLease(mixed?s.notes:nullptr);
    if(mixed){
        auto& notes=*s.notes;if(++notes.epoch==0){notes.epochs.fill(0);notes.epoch=1;}
        unsigned changed=0;bool overflow=false;
        // Simulate ALL note counters before audio/journal side effects, then
        // restore. No overflow after an already-emitted CC prefix is allowed.
        for(unsigned i=0;i<length;){
            const unsigned command=input[i]&0xf0u,width=originalSpan(i);
            if(input[i]<128u){i+=width;continue;}
            if(command==0x80u||command==0x90u){
                const unsigned index=(port*16+(input[i]&15u))*128+input[i+1];
                if(notes.epochs[index]!=notes.epoch){notes.epochs[index]=notes.epoch;notes.previous[index]=notes.counts[index];notes.touched[changed++]=index;}
                if(command==0x90u&&input[i+2]){if(notes.counts[index]==UINT64_MAX){overflow=true;break;}++notes.counts[index];}
                else if(notes.counts[index])--notes.counts[index];
            }else if(command==0xb0u&&input[i+1]>=120&&input[i+1]!=121){
                const unsigned base=(port*16+(input[i]&15u))*128;
                for(unsigned key=0;key<128;++key){const unsigned index=base+key;
                    if(notes.epochs[index]!=notes.epoch){notes.epochs[index]=notes.epoch;notes.previous[index]=notes.counts[index];notes.touched[changed++]=index;}
                    notes.counts[index]=0;}
            }
            i+=width;
        }
        for(unsigned j=0;j<changed;++j){const unsigned index=notes.touched[j];notes.counts[index]=notes.previous[index];}
        if(overflow){ReleaseSRWLockExclusive(&s.lock);return -4;}
    }
    if(mixed&&s.notes->activity&&!s.selection&&!s.notes->activitySourcesReady){++s.notes->activityRefusals;ReleaseSRWLockExclusive(&s.lock);return 0;}
    MidiActivityLease activityLease(mixed?s.notes:nullptr,input,length,port,requiredNotes,&s.pendingStream,implicitData);
    if(!activityLease.ready){ReleaseSRWLockExclusive(&s.lock);return -4;}
    // An intercepted explicit CC may change which implicit bytes finally
    // reach BASS. Reserve a conservative whole original byte bound before
    // app side effects; exact callback identities use the real output wire.
    if(implicitData)requiredSelections=std::max(requiredSelections,length);
    if(requiredSelections&&s.selection){
        bool asynchronous=(mode&BASS_MIDI_EVENTS_ASYNC)!=0;
        if(s.channelFlags){const DWORD flags=s.channelFlags(stream,0,0);
            if(flags==UINT_MAX){result[4]=static_cast<unsigned>(reinterpret_cast<int(WINAPI*)()>(s.errorAddress)());ReleaseSRWLockExclusive(&s.lock);return -6;}
            asynchronous=asynchronous||(flags&BASS_MIDI_ASYNC)!=0;}
        if(!s.selection->canAdmitEvents(requiredSelections,requiredSelections)){
            // Prepare the WHOLE original SYNC/ASYNC frame before parser/state
            // writer. No capacity-driven prefix submission may invent a new
            // BASS async timing offset. Native input worker allocation only.
            NativeSelectionState::StoragePreparation storage;
            if(!s.selection->stageCapacityOwned(requiredSelections,requiredSelections,storage)){ReleaseSRWLockExclusive(&s.lock);return -6;}
            auto lock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(s.lockAddress);
            if(!lock(stream,TRUE)){result[4]=static_cast<unsigned>(reinterpret_cast<int(WINAPI*)()>(s.errorAddress)());ReleaseSRWLockExclusive(&s.lock);return -6;}
            // One requested-shadow record per original selection precedes
            // RAW in the source parser, independently of callback records.
            const bool prepared=s.selection->publishCapacity(requiredSelections,requiredSelections,storage);
            if(!lock(stream,FALSE)){s.pendingStream=stream;s.pendingThread=GetCurrentThreadId();result[4]=static_cast<unsigned>(reinterpret_cast<int(WINAPI*)()>(s.errorAddress)());s.selection->retainPreparation(std::move(storage));ReleaseSRWLockExclusive(&s.lock);return -2;}
            if(!prepared){ReleaseSRWLockExclusive(&s.lock);return -6;}
        }
    }
    MidiCCJournalLease lease(s); // Includes gaps between source/raw/send effects.
    MidiResetChannelSet resetChannels;
    if(mixed)s.notes->humanization.begin(stream,origin);
    unsigned bytes=0,head=0,records=0;unsigned long long selectionTicket=0;bool selectionPending=false,restoreDrumUserFX=false;
    int observerFailure=0;
    if(s.semanticFramePending){resetChannels.table=s.semanticFrameResetTable;resetChannels.mask=s.semanticFrameResetMask;resetChannels.used=s.semanticFrameResetUsed;
        restoreDrumUserFX=s.semanticFrameDrumRestore;}
    auto begin=[&](){if(bytes==head&&s.channels>16){output[bytes++]=0xff;output[bytes++]=0x21;output[bytes++]=1;output[bytes++]=static_cast<unsigned char>(port);}};
    auto flush=[&](){
        if(bytes==head)return true;
        if(mixed&&timing&&origin[0]){
            unsigned i=head+(s.channels>16?4u:0u);
            while(i<bytes){
                if(output[i]<128u){++i;continue;} // Original implicit notes have no app timing registration.
                const unsigned command=output[i]&0xf0u,width=midiOriginalSpan(output,bytes,i);
                if(!width)break; // Admission proved all transformed output spans.
                if(command==0x90u&&output[i+2]){
                    const unsigned channel=port*16+(output[i]&15u),key=output[i+1];
                    static_cast<PlaybackTiming*>(timing)->receive(channel,key,origin[0],origin[1],origin[2],origin[3],origin[4],origin[6]?origin[5]:2,s.notes->humanization.delayAt(i));
                }
                i+=width;
            }
        }
        if(implicitData){
            auto refuseObserver=[&](int error){
                // The parser may already own requested-shadow/CC prefix writes.
                // No RAW attempt happened: settle only this unsubmitted ticket
                // and retain the exact frame receipt for terminal ACK/retirement.
                if(selectionTicket)s.selection->settleTicket(selectionTicket,3,false);selectionTicket=0;
                observerFailure=s.executionStop->load(std::memory_order_acquire)?-6:-8;
                s.semanticFramePending=true;s.semanticFrameEnd=result[5]==length;s.semanticFrameDrumRestore=restoreDrumUserFX;
                s.semanticFrameResetTable=resetChannels.table;s.semanticFrameResetMask=resetChannels.mask;s.semanticFrameResetUsed=resetChannels.used;
                if(observerFailure==-8){s.genericFault=-8;s.genericFaultStream=stream;}
                result[4]=static_cast<unsigned>(error);return false;
            };
            int error=0;bool waited=false;
            for(;;){
                if(waited&&s.executionStop->load(std::memory_order_acquire))error=BASS_ERROR_MEM;
                else if(implicitScratch->prepare(*s.rawContext,mode,output+head,bytes-head,s.genericConversion,s.errorAddress,error))break;
                if(error==BASS_ERROR_MEM&&!s.executionStop->load(std::memory_order_acquire)){
                    // ConvertEvents is only the selection observer, not RAW
                    // audio. Preserve this exact native stack continuation.
                    // No BASS lock is held; let PCM/ASYNC callbacks progress.
                    // The unpaused execution owner prevents source mutation.
                    waited=true;noteLease.suspend();ReleaseSRWLockExclusive(&s.lock);Sleep(1);
                    AcquireSRWLockExclusive(&s.lock);noteLease.resume(s.notes);continue;
                }
                // No RAW was attempted. Retire only its unsubmitted reservation,
                // retain an explicit prefix owner and never fabricate RAW2.
                return refuseObserver(error);
            }
            // Keep the same pre-prefix reservation while extending identities
            // for implicit callbacks. Replacing it via ticket() could reject
            // a late ambiguous prior ASYNC ticket AFTER original side effects
            // and incorrectly turn healthy input into a skipped RAW failure.
            const auto count=implicitScratch->selections;
            if(selectionTicket){if(!s.selection->growPreparedTicketTo(selectionTicket,count))return refuseObserver(-1);}
            else selectionTicket=count?s.selection->ticket(count):0;
            if(count&&(!selectionTicket||!s.selection->identifyTicket(selectionTicket,implicitScratch->selected.data(),count,0)))return refuseObserver(-1);
        }else if(s.selection&&selectionTicket)s.selection->finalizeTicketIndex(selectionTicket);
        if(s.selection&&selectionTicket)s.selection->settleTicket(selectionTicket,1,false);
        NativeSelectionSubmissionScope scope{s.selection,selectionTicket};
        const DWORD admitted=mixed&&(!s.notes->humanization.spans.empty()||s.notes->humanization.failed)?
            s.notes->humanization.emit(stream,mode,output+head,bytes-head,head):s.rawEmit(stream,mode,output+head,bytes-head);
        const int rawError=admitted==UINT_MAX?reinterpret_cast<int(WINAPI*)()>(s.errorAddress)():0;
        if(s.rawContext)raw_decode_context_observe(s.rawContext,stream,mode,output+head,bytes-head,admitted!=UINT_MAX,rawError);
        ++result[2];head=bytes;
        if(admitted==0xffffffffu){result[4]=static_cast<unsigned>(rawError);
            if(s.selection&&selectionTicket)s.selection->settleTicket(selectionTicket,2,false);selectionTicket=0;
            s.semanticFramePending=false;s.semanticFrameEnd=false;s.semanticFrameDrumRestore=false;
            if(mixed&&timing&&origin[0])playback_timing_cancel(timing,origin[0]);return false;}
        selectionTicket=0;result[3]+=admitted;return true;
    };
    for(unsigned i=0;i<length;){
        const unsigned width=originalSpan(i),status=input[i];
        if(status<128u){begin();output[bytes++]=static_cast<unsigned char>(status);++i;result[5]=i;continue;}
        if(status>=0xf0u){begin();std::copy_n(input+i,width,output+bytes);bytes+=width;i+=width;result[5]=i;continue;}
        const unsigned command=status&0xf0u,channel=port*16+(status&15u);
        // Original observe_cc Learn interception precedes Bank selection.
        // A just-captured CC0/32 is an automation source and is consumed by
        // the selected successor; it must not reach the selection fast path.
        const bool pendingLearn=command==0xb0u&&s.semantics&&s.semantics->armed&&
            (s.semantics->armAll||s.semantics->armChannel==channel);
        const bool selectionWire=s.selection&&(command==0xc0u||(command==0xb0u&&(input[i+1]==0||input[i+1]==32)&&!pendingLearn&&!(s.semantics&&s.semantics->counts[channel*128+input[i+1]])));
        if(mixed&&selectionWire)s.notes->humanization.boundary(channel);
        if(selectionWire){
            const bool reserved=selectionTicket?s.selection->growTicket(selectionTicket):(selectionTicket=s.selection->ticket(1))!=0;
            if(!reserved){
                if(!flush()){result[0]=bytes;ReleaseSRWLockExclusive(&s.lock);return observerFailure?observerFailure:2;}
                result[0]=bytes;result[5]=i;ReleaseSRWLockExclusive(&s.lock);return -6;
            }
            const unsigned event=command==0xc0u?MIDI_EVENT_PROGRAM:input[i+1]==0?MIDI_EVENT_BANK:MIDI_EVENT_BANK_LSB;
            const unsigned raw=input[i+width-1];
            if(!s.selection->identifyLastTicketEvent(selectionTicket,event,channel,raw)){
                result[0]=bytes;result[5]=i;ReleaseSRWLockExclusive(&s.lock);return -6;
            }
            const unsigned address=s.selection->request(channel,event,raw);
            s.selection->write(channel,event,raw,6,0);
            if(s.firstuse){auto* request=s.fontRequests.data()+channel*4;request[0]=address&0x1000000u?128u:address&255u;
                request[1]=(address>>16)&255u;request[2]=(address>>8)&255u;}
            selectionPending=true;begin();std::copy_n(input+i,width,output+bytes);bytes+=width;i+=width;result[5]=i;continue;
        }
        if(command!=0xb0u){
            if(command==0x80u||command==0x90u){
                if(command==0x90u&&input[i+2]&&selectionPending){if(!flush()){result[0]=bytes;ReleaseSRWLockExclusive(&s.lock);return observerFailure?observerFailure:2;}selectionPending=false;}
                if(s.selection&&!s.notes->activityAuthoritative&&!s.selection->room(1)){if(!flush()){result[0]=bytes;ReleaseSRWLockExclusive(&s.lock);return observerFailure?observerFailure:2;}
                    result[0]=bytes;result[5]=i;ReleaseSRWLockExclusive(&s.lock);return -6;}
                if(command==0x90u&&input[i+2]&&s.firstuse&&s.fontMode==1){
                    // Legacy preload occurs before this original Note but
                    // does not split a buffered RAW prefix. Keep bytes/head.
                    void* plan=s.firstuse;font_preload_plan_retain(plan);auto* generation=s.semantics;if(generation)generation->retain();
                    const auto epoch=s.fontEpoch,fontGeneration=s.fontGeneration;const auto* request=s.fontRequests.data()+channel*4;
                    const unsigned bank=request[0],program=request[1],lsb=request[2],qualification=request[3];
                    noteLease.suspend();ReleaseSRWLockExclusive(&s.lock);
                    unsigned long long preloadResult[8]{};
                    const int loaded=font_preload_plan_before_note(plan,epoch,fontGeneration,stream,channel,bank,program,lsb,qualification,preloadResult);
                    AcquireSRWLockExclusive(&s.lock);noteLease.resume(s.notes);
                    const bool current=s.firstuse==plan&&s.semantics==generation&&s.fontEpoch==epoch&&s.fontGeneration==fontGeneration&&s.fontStream==stream;
                    if(generation)generation->release();font_preload_plan_release(plan);
                    if(!current||loaded<=0){
                        // Preserve a processed prefix and leave THIS Note
                        // untouched for positioned ownership transfer.
                        result[5]=i;const bool accepted=flush();result[0]=bytes;ReleaseSRWLockExclusive(&s.lock);return accepted?-7:observerFailure?observerFailure:2;
                    }
                }
                const unsigned before=bytes;begin();
                s.notes->transform(status,input[i+1],input[i+2],channel,output,bytes,noteActions[(*noteCount)++]);
                const auto& action=noteActions[*noteCount-1];
                if(s.notes->activity){const auto identity=s.selection?s.selection->noteIdentity(channel,action):s.notes->activitySources[channel];
                    s.notes->observeActivity(action,identity);
                    if(s.selection&&!s.notes->activityAuthoritative){const unsigned address=identity.bank|(identity.program<<16);
                        s.selection->write(channel,static_cast<unsigned>(action.qpc),static_cast<unsigned>(action.qpc>>32),
                            identity.known?16u:17u,action.packed,static_cast<int>(address));}}
                else if(s.selection)s.selection->note(channel,action);
                // Suppressed Off does not create a spurious port-only RAW call.
                if(bytes==before+(s.channels>16&&before==head?4u:0u))bytes=before;
            }else{begin();std::copy_n(input+i,width,output+bytes);bytes+=width;}
            i+=width;result[5]=i;continue;
        }
        const unsigned cc=input[i+1],value=input[i+2];
        LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);
        if(cc>=120){
            if(mixed&&cc!=121){
                if(!flush()||!s.notes->humanization.cancel(stream,channel)){result[0]=bytes;ReleaseSRWLockExclusive(&s.lock);return 2;}
                if(timing)for(unsigned key=0;key<128;++key){auto& q=static_cast<PlaybackTiming*>(timing)->ingress[channel*128+key];q.head.store(q.tail.load());}
            }
            if(cc==121){
                // Whole-frame capacity was admitted before any original side
                // effects. Move only the completed producer prefix when this
                // frame exceeds256 rows; never overwrite an unread receipt.
                if(!midiCCCaptureEnsureRoom(s)){result[0]=bytes;result[5]=i;ReleaseSRWLockExclusive(&s.lock);return -6;}
                resetChannels.add(channel);
                // Original auxiliary/Learn/delay/session writers precede the
                // containing RAW call, and survive a later RAW rejection.
                if(s.journal)for(unsigned source=0;source<120;++source)s.journal->receive(stream,s.auxSlots[source],channel<<16);
                const auto before=s.resetLearnRevision;
                if(s.resetArmChannel==channel){s.resetArmChannel=UINT_MAX;++s.resetLearnRevision;
                    if(s.semantics&&s.semantics->armed&&s.semantics->armChannel==channel){s.semantics->armed=false;++s.semantics->armRevision;}}
                const auto version=s.journal->clock->serial.fetch_add(1,std::memory_order_acq_rel)+1;
                auto& capture=s.captures[(s.captureHead+s.captureCount)%static_cast<unsigned>(s.captures.size())];
                capture={version,before,s.resetLearnRevision,channel,0,0,121,0,0,static_cast<unsigned long long>(tick.QuadPart),channel,0};
                ++s.captureCount;++s.captureSerial;
                for(auto* tap:s.delays[channel])if(tap)delayMidiSend(tap,0);
                s.journal->receive(stream,s.slot(0xb0000u+MIDI_EVENT_USERFX),channel<<16);
                if(s.resetDrums[channel])restoreDrumUserFX=true;
                if(s.semanticFramePending){s.semanticFrameResetTable=resetChannels.table;s.semanticFrameResetMask=resetChannels.mask;s.semanticFrameResetUsed=resetChannels.used;
                    s.semanticFrameDrumRestore=restoreDrumUserFX;}
            }else std::fill_n(s.notes->counts.begin()+channel*128,128,0ull);
            noteActions[(*noteCount)++]={0xb0u|(channel&15u)|(cc<<8),channel,0,static_cast<unsigned long long>(tick.QuadPart)};
            if(s.notes->activity)s.notes->observeActivity(noteActions[*noteCount-1],{false,0,0});
            begin();std::copy_n(input+i,3,output+bytes);bytes+=3;i+=3;result[5]=i;continue;
        }
        // Aux observation precedes flush exactly as the original interpreter.
        auto& row=actions[records++];row={status|(cc<<8)|(value<<16),channel,0,value,0,0,static_cast<unsigned long long>(tick.QuadPart)};
        result[1]=records;
        if(s.journal)s.journal->receive(stream,s.auxSlots[cc],(channel<<16)|value);
        // The same owned RAW executor covers external input and SMF playback.
        // Flush older notes before changing a channel processor; retain the
        // original CC meaning and every existing scalar/Learn destination.
        if(channelFXMidiHas(stream,channel,cc)){
            if(!flush()){result[0]=bytes;ReleaseSRWLockExclusive(&s.lock);return observerFailure?observerFailure:2;}
            if(channelFXMidiApply(stream,channel,cc,value)==-2){
                s.pendingStream=stream;s.pendingThread=GetCurrentThreadId();
                if(s.journal){lease.retain=true;s.pendingClockLease=true;}
                result[0]=bytes;result[5]=i;ReleaseSRWLockExclusive(&s.lock);return -2;
            }
        }
        if(s.semantics&&s.semantics->armed&&(s.semantics->armAll||s.semantics->armChannel==channel)){
            if(!midiCCCaptureEnsureRoom(s)){result[0]=bytes;result[5]=i;ReleaseSRWLockExclusive(&s.lock);return -6;}
            auto& state=*s.semantics;auto& capture=s.captures[(s.captureHead+s.captureCount)%static_cast<unsigned>(s.captures.size())];
            ++s.captureSerial;const auto captureVersion=s.journal->clock->serial.fetch_add(1,std::memory_order_acq_rel)+1;
            capture={captureVersion,state.armRevision,state.armRevision+1,state.armChannel,state.armKey,state.armDestination,
                cc,state.armAll?1ull:0ull,state.armActive,static_cast<unsigned long long>(tick.QuadPart),channel,value};
            for(const auto& patch:state.successors[cc]){const unsigned index=patch.channel*128+patch.cc;
                state.first[index]=patch.first;state.counts[index]=patch.count;s.blocked[index]=static_cast<unsigned char>(patch.blocked);}
            state.armed=false;s.resetArmChannel=UINT_MAX;++state.armRevision;s.resetLearnRevision=state.armRevision;++s.captureCount;
        }
        if(s.semantics&&s.semantics->counts[channel*128+cc]){
            if(!flush()){result[0]=bytes;ReleaseSRWLockExclusive(&s.lock);return observerFailure?observerFailure:2;}
            const unsigned first=s.semantics->first[channel*128+cc],end=first+s.semantics->counts[channel*128+cc];
            MidiCCCompoundContext context{mode,port,noteActions,noteCapacity,noteCount};
            const int applied=midiCCSemanticApply(s,stream,channel,value,first,end,mixed?&context:nullptr);
            if(applied!=1){
                s.semanticFramePending=true;s.semanticFrameEnd=i+3==length;s.semanticFrameDrumRestore=restoreDrumUserFX;
                s.semanticFrameResetTable=resetChannels.table;s.semanticFrameResetMask=resetChannels.mask;s.semanticFrameResetUsed=resetChannels.used;
                if(applied==-2&&s.journal){lease.retain=true;s.pendingClockLease=true;}
                result[0]=bytes;result[5]=i+3;ReleaseSRWLockExclusive(&s.lock);return applied;
            }
        }else if(cc==91||cc==93||((s.options&2)&&cc==94)){
            if(!flush()){result[0]=bytes;ReleaseSRWLockExclusive(&s.lock);return observerFailure?observerFailure:2;}
            row.event=cc==91?MIDI_EVENT_REVERB:cc==93?MIDI_EVENT_CHORUS:MIDI_EVENT_USERFX;
            if(cc==94){
                for(auto* tap:s.delays[channel])if(tap)delayMidiSend(tap,value);
                row.accepted=1; // Existing custom tap send, not native UserFX.
            }else{
                MidiSendResult applied{};int errors[3]{};
                const unsigned packed=s.sends->floors.load(std::memory_order_acquire),floor=cc==91?packed&127u:(packed>>7)&127u;
                const int accepted=midi_send_domain_emit(s.sends,stream,channel,row.event,floor,value,
                    s.emitAddress,s.lockAddress,s.errorAddress,&applied,errors);
                row.raw=floor+((127u-floor)*value)/127u;row.accepted=applied.accepted;
                row.error=accepted==0?errors[0]:applied.error;result[7]+=accepted!=0;
                if(!row.accepted)++result[6];
                if(accepted==-2){
                    s.pendingStream=stream;s.pendingThread=GetCurrentThreadId();result[4]=static_cast<unsigned>(errors[1]);
                    if(row.accepted&&s.journal){
                        s.journal->receive(stream,s.requestSlots[cc==93],(channel<<16)|value);
                        s.journal->receive(stream,s.acceptedSlots[cc==93],(channel<<16)|row.raw);
                    }
                    if(s.journal){lease.retain=true;s.pendingClockLease=true;}
                    result[0]=bytes;result[5]=i+3;ReleaseSRWLockExclusive(&s.lock);return -2;
                }
            }
            if(row.accepted&&s.journal){
                if(cc!=94)s.journal->receive(stream,s.requestSlots[cc==93],(channel<<16)|value);
                s.journal->receive(stream,s.acceptedSlots[cc==91?0:cc==93?1:2],(channel<<16)|row.raw);
            }
        }else{
            begin();
            if(s.semantics&&cc>=71&&cc<=75){
                constexpr unsigned toneEvents[]{MIDI_EVENT_RESONANCE,MIDI_EVENT_RELEASE,MIDI_EVENT_ATTACK,MIDI_EVENT_CUTOFF,MIDI_EVENT_DECAY};
                for(unsigned fieldIndex=0;fieldIndex<s.semantics->fields.size();++fieldIndex){auto& field=s.semantics->fields[fieldIndex];
                    if(field.event==toneEvents[cc-71]&&(field.flags&8u))s.semantics->refreshTimbre(channel,field);
                    if(field.event==toneEvents[cc-71]&&(field.flags&8u)&&field.timbrePresent[channel]&&!field.sourceMapped[channel]){
                        // Legacy parser updates an already-touched override
                        // before RAW audio, even if that RAW later fails.
                        s.semantics->write(channel,fieldIndex,2,value);break;
                    }
                }
            }
            if(cc==6&&(s.options&1)){
                const unsigned char expanded[]{static_cast<unsigned char>(status),101,0,static_cast<unsigned char>(status),100,0,
                    static_cast<unsigned char>(status),6,static_cast<unsigned char>(value),static_cast<unsigned char>(status),101,127,static_cast<unsigned char>(status),100,127};
                std::copy_n(expanded,15,output+bytes);bytes+=15;
            }else{std::copy_n(input+i,3,output+bytes);bytes+=3;}
        }
        result[5]=i+3;
        i+=3;
    }
    const bool finalRaw=bytes!=head;
    const bool accepted=flush();
    if(accepted&&finalRaw&&restoreDrumUserFX){
        // The source always restores channel9, even when CC121 targeted a
        // drum channel on another port. No altered ASYNC/SYNC or raw boundary.
        auto emit=reinterpret_cast<BOOL(WINAPI*)(HSTREAM,DWORD,DWORD,DWORD)>(s.emitAddress);
        emit(stream,9,MIDI_EVENT_USERFX,s.resetDrums[9]?127u:0u);
    }
    if(accepted){
        s.resetOrderCount=s.resetOrderNext=0;
        for(unsigned index=0;index<=resetChannels.mask;++index)if(resetChannels.table[index]>=0)s.resetOrder[s.resetOrderCount++]=static_cast<unsigned>(resetChannels.table[index]);
        s.semanticFramePending=false;s.semanticFrameEnd=false;s.semanticFrameDrumRestore=false;
        if(s.resetTailReady&&s.resetOrderCount){s.resetTailStream=stream;s.resetTailPhase=0;s.resetTailFault=s.resetTailError=0;}
        const int tail=s.resetOrderCount?midiCCResetScalarContinue(s,stream):1;
        if(tail!=1){if(tail==-2&&s.journal){lease.retain=true;s.pendingClockLease=true;}result[0]=bytes;ReleaseSRWLockExclusive(&s.lock);return tail;}
    }
    result[0]=bytes;ReleaseSRWLockExclusive(&s.lock);return accepted?1:observerFailure?observerFailure:2;
}
// Both existing public run exports and the native worker use this receipt
// owner. A continuation is the original immutable suffix, never new bytes
// that happen to share the stream. No CANCEL/FLUSH flags or invented RAW split.
static int midiCCOwnedRun(void* pointer,unsigned stream,unsigned mode,unsigned port,
        const unsigned char* input,unsigned length,unsigned char* output,unsigned capacity,MidiCCAction* actions,unsigned actionCapacity,
        unsigned long long* result,MidiNoteAction* noteActions=nullptr,unsigned noteCapacity=0,unsigned long long* noteCount=nullptr,
        void* timing=nullptr,const unsigned long long* origin=nullptr,unsigned long long receipt=0,unsigned offset=0){
    if(result)std::fill_n(result,8,0ull);if(noteCount)*noteCount=0;
    if(!pointer||!stream||!input||!result||!output||!actions||length>1024*1024)return -1;
    // Preserve the original pristine empty-frame refusal. There is no prefix
    // or suffix to own, and creating an immediately complete receipt would
    // incorrectly change the public CC-only status from unsupported to success.
    if(!length&&!receipt)return 0;
    auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&s.frameAPILock);
    AcquireSRWLockExclusive(&s.lock);
    const bool mixed=noteActions&&noteCount&&origin;
    if(s.executionOwner&&s.executionThread.load()!=GetCurrentThreadId()){
        ReleaseSRWLockExclusive(&s.lock);ReleaseSRWLockExclusive(&s.frameAPILock);return -5;}
    if(!receipt){
        if(s.cancelledStream||s.frameReceipt||s.pendingStream||s.semanticActive||s.semanticFramePending||s.gmActive||s.resetTailStream){
            ReleaseSRWLockExclusive(&s.lock);ReleaseSRWLockExclusive(&s.frameAPILock);return -3;}
        receipt=midiCCNextFrameReceipt();
        if(!receipt){ReleaseSRWLockExclusive(&s.lock);ReleaseSRWLockExclusive(&s.frameAPILock);return -9;}
        s.frameReceipt=receipt;s.frameStream=stream;s.frameMode=mode;s.framePort=port;
        s.frameLength=length;s.frameConsumed=0;s.frameMixed=mixed;
        std::copy_n(input,length,s.frameOriginal.begin());
        if(origin)std::copy_n(origin,7,s.frameOrigin.begin());
        s.frameTiming=static_cast<PlaybackTiming*>(timing);if(s.frameTiming)s.frameTiming->retain();
    }else{
        const bool valid=s.frameReceipt==receipt&&s.frameStream==stream&&s.frameMode==mode&&s.framePort==port&&
            s.frameLength==length&&s.frameConsumed==offset&&s.frameMixed==mixed&&s.frameTiming==timing&&
            std::equal(s.frameOriginal.begin(),s.frameOriginal.begin()+length,input)&&
            (!mixed||std::equal(s.frameOrigin.begin(),s.frameOrigin.end(),origin));
        if(!valid||offset>length){ReleaseSRWLockExclusive(&s.lock);ReleaseSRWLockExclusive(&s.frameAPILock);return -1;}
        if(s.pendingStream){ReleaseSRWLockExclusive(&s.lock);ReleaseSRWLockExclusive(&s.frameAPILock);return -3;}
    }
    const unsigned previous=s.frameConsumed;
    ReleaseSRWLockExclusive(&s.lock);
    int outcome=1;unsigned captured=0;
    if(receipt&&midi_cc_kernel_semantic_pending(pointer,stream)==1){
        outcome=midi_cc_kernel_semantic_resume(pointer,stream);
        if(mixed){const int rows=midi_cc_kernel_take_actions(pointer,noteActions,noteCapacity);
            if(rows<0)outcome=-6;else captured=static_cast<unsigned>(rows);}
    }
    if(outcome==1&&previous<length){
        unsigned long long suffixNotes=0;
        outcome=midiCCRun(pointer,stream,mode,port,s.frameOriginal.data()+previous,length-previous,
            output,capacity,actions,actionCapacity,result,mixed?noteActions+captured:nullptr,
            mixed?noteCapacity-captured:0,mixed?&suffixNotes:nullptr,s.frameTiming,mixed?s.frameOrigin.data():nullptr,true);
        if(mixed)*noteCount=captured+suffixNotes;
    }else{
        if(mixed)*noteCount=captured;
        if(outcome==1&&midi_cc_kernel_reset_tail_pending(pointer,stream)==1)outcome=midi_cc_kernel_reset_tail(pointer,stream);
    }
    AcquireSRWLockExclusive(&s.lock);
    // Result consumed is relative to THIS continuation, preserving worker
    // aggregation. The receipt header records the absolute original offset.
    if(s.frameReceipt==receipt&&result[5]<=length-previous)s.frameConsumed=previous+static_cast<unsigned>(result[5]);
    else if(s.frameReceipt==receipt)outcome=-1;
    const bool complete=outcome==1&&s.frameConsumed==length&&!s.semanticActive&&!s.semanticFramePending&&!s.resetTailStream;
    // Legacy RAW failure abandons the unexecuted suffix. It is NOT resumable.
    // A pristine unsupported frame has no prefix to own and may hand off.
    if(complete||outcome==2||((outcome==0||outcome==-1||outcome==-4||outcome==-7)&&s.frameConsumed==0&&!s.semanticActive&&!s.semanticFramePending))s.clearFrameReceipt();
    ReleaseSRWLockExclusive(&s.lock);ReleaseSRWLockExclusive(&s.frameAPILock);return outcome;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_cc_kernel_frame_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_frame_status(void* pointer,unsigned long long* output,unsigned capacity){
    if(!pointer||!output||capacity<8)return -1;auto& s=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockShared(&s.lock);
    const unsigned long long row[]{s.frameReceipt,s.frameStream,s.frameMode,s.framePort,s.frameLength,s.frameConsumed,s.frameMixed?1ull:0ull,
        s.semanticActive||s.semanticFramePending?1ull:0ull};std::copy_n(row,8,output);ReleaseSRWLockShared(&s.lock);return 8;
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_frame_resume(void* pointer,unsigned long long receipt,unsigned offset,
        unsigned stream,unsigned mode,unsigned port,const unsigned char* original,unsigned length,
        unsigned char* output,unsigned capacity,MidiCCAction* actions,unsigned actionCapacity,
        MidiNoteAction* notes,unsigned noteCapacity,unsigned long long* noteCount,void* timing,const unsigned long long* origin,unsigned long long* result){
    if(!receipt)return -1;
    return midiCCOwnedRun(pointer,stream,mode,port,original,length,output,capacity,actions,actionCapacity,result,
        notes,noteCapacity,noteCount,timing,origin,receipt,offset);
}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_run(void* pointer,unsigned stream,unsigned mode,unsigned port,
        const unsigned char* input,unsigned length,unsigned char* output,unsigned capacity,MidiCCAction* actions,unsigned actionCapacity,unsigned long long* result){
    return midiCCOwnedRun(pointer,stream,mode,port,input,length,output,capacity,actions,actionCapacity,result);
}
extern "C" __declspec(dllexport) int __cdecl midi_mixed_kernel_run(void* pointer,unsigned stream,unsigned mode,unsigned port,
        const unsigned char* input,unsigned length,unsigned char* output,unsigned capacity,MidiCCAction* actions,unsigned actionCapacity,
        MidiNoteAction* notes,unsigned noteCapacity,unsigned long long* noteCount,void* timing,const unsigned long long* origin,unsigned long long* result){
    if(!notes||!noteCount||!origin)return -1;
    return midiCCOwnedRun(pointer,stream,mode,port,input,length,output,capacity,actions,actionCapacity,result,notes,noteCapacity,noteCount,timing,origin);
}

// Native semantic building block, called by the input executor, NOT DSP.
// No Python callbacks or GUI locks during transformation. Enabled timing
// reserves actor-owned storage;the neutral transform stays allocation-free.
// No actor-owned storage is touched by an audio callback during
// submission. Native serialization protects control edits and counter state.
// Metadata keeps ORIGINAL note actions even when wire Note Off is suppressed.
extern "C" __declspec(dllexport) unsigned __cdecl midi_note_kernel_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_note_kernel_humanization_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_note_kernel_humanization_bind(void* pointer,void* position,void* convert,void* lock){
    if(!pointer||!position||!convert||!lock)return 0;auto& s=*static_cast<MidiNoteKernel*>(pointer);
    AcquireSRWLockExclusive(&s.lock);
    if(s.executionOwner&&!s.executionPaused){ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.humanization.bind(reinterpret_cast<void*>(s.emit),position,convert,lock);ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_note_kernel_humanization(void* pointer,unsigned timing,unsigned velocity){
    return pointer&&static_cast<MidiNoteKernel*>(pointer)->humanization.configure(timing,velocity)?1:0;
}
extern "C" __declspec(dllexport) int __cdecl midi_note_kernel_humanization_grouped(void* pointer,unsigned timing,unsigned velocity,unsigned preserve){
    return pointer&&preserve<=1&&static_cast<MidiNoteKernel*>(pointer)->humanization.configure(timing,velocity,preserve!=0)?1:0;
}
extern "C" __declspec(dllexport) int __cdecl midi_note_kernel_humanization_cancel(void* pointer,unsigned stream,unsigned channel){
    if(!pointer||!stream||(channel>=128&&channel!=UINT_MAX))return 0;auto& s=*static_cast<MidiNoteKernel*>(pointer);
    AcquireSRWLockExclusive(&s.lock);
    if(s.executionOwner&&!s.executionPaused){ReleaseSRWLockExclusive(&s.lock);return 0;}
    const bool result=s.humanization.cancel(stream,channel);ReleaseSRWLockExclusive(&s.lock);return result?1:0;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_note_kernel_activity_version(){return 1;}
static int midiActivityAttach(void* pointer,void* activity,bool seeded){
    if(!pointer)return 0;auto& s=*static_cast<MidiNoteKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    // A generic paused flag also covers prefix ACK/unlock, not a proven
    // attachment publication fence. Attach before execution creation or after
    // joined retirement; live replacement needs a separate typed command.
    if(s.executionOwner||s.activityDepth||s.activityKeep){ReleaseSRWLockExclusive(&s.lock);return 0;}
    auto* next=static_cast<NativeMidiActivity*>(activity);
    if(next==s.activity){ReleaseSRWLockExclusive(&s.lock);return 1;}
    if(next){if(next->channels.size()!=s.channels){ReleaseSRWLockExclusive(&s.lock);return 0;}
        AcquireSRWLockExclusive(&next->lock);bool clean=!next->attachedOwner&&!next->reservationThread&&!next->shadowDirty;
        if(seeded){
            // Complete validated transfer preserves high-byte shadow keys;
            // only the actual0..127 native audio counters are projected here.
            for(unsigned ch=0;ch<128;++ch)for(unsigned key=0;key<128;++key){
                const auto expected=ch<s.channels?next->channels[ch].keys[key].overlap:0;
                if(s.counts[ch*128+key]!=expected)clean=false;}
        }else{
            if(!std::all_of(s.counts.begin(),s.counts.end(),[](unsigned long long count){return count==0;}))clean=false;
            for(const auto& ch:next->channels)for(const auto& key:ch.keys)
                if(key.activity||key.overlap||key.head<key.runs.size())clean=false;
        }
        if(!clean){ReleaseSRWLockExclusive(&next->lock);ReleaseSRWLockExclusive(&s.lock);return 0;}
        next->retain();next->attachedOwner=&s;ReleaseSRWLockExclusive(&next->lock);}
    auto* previous=s.activity;s.activity=next;s.activityDirty=false;s.activityFrameActions=0;s.activityAuthoritative=false;
    if(previous){AcquireSRWLockExclusive(&previous->lock);previous->attachedOwner=nullptr;
        ReleaseSRWLockExclusive(&previous->lock);previous->release();}
    ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_note_kernel_activity_attach(void* pointer,void* activity){return midiActivityAttach(pointer,activity,false);}
extern "C" __declspec(dllexport) unsigned __cdecl midi_note_kernel_activity_seed_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_note_kernel_activity_seed_attach(void* pointer,void* activity){return midiActivityAttach(pointer,activity,true);}
// Explicit lifetime admission only. This is not enabled by merely attaching a
// shadow observer. No live/prefix-pause toggling: all borrowed executor owners
// must first be joined. The full transfer supplies initial FIFO/counter state.
extern "C" __declspec(dllexport) unsigned __cdecl midi_note_kernel_activity_authority_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_note_kernel_activity_authority(void* pointer,unsigned enabled){
    if(!pointer||enabled>1)return 0;auto& s=*static_cast<MidiNoteKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    bool ok=!s.executionOwner&&!s.activityDepth&&!s.activityKeep;
    if(enabled){ok=ok&&s.activity&&s.activitySourcesReady&&!s.activityDirty;
        if(ok){AcquireSRWLockShared(&s.activity->lock);ok=s.activity->attachedOwner==&s&&!s.activity->reservationThread&&
            !s.activity->shadowDirty&&(s.activity->validity.load(std::memory_order_acquire)&1)!=0;
            ReleaseSRWLockShared(&s.activity->lock);}}
    if(ok)s.activityAuthoritative=enabled!=0;ReleaseSRWLockExclusive(&s.lock);return ok?1:0;
}
// count is channel ROWS, each three unsigneds known/bank/program. Prepared
// fallback is explicit data, never a fabricated CC0/32/Program interpretation.
extern "C" __declspec(dllexport) int __cdecl midi_note_kernel_activity_sources(void* pointer,const unsigned* rows,unsigned count){
    if(!pointer||!rows)return 0;auto& s=*static_cast<MidiNoteKernel*>(pointer);if(count!=s.channels)return 0;
    for(unsigned i=0;i<count;++i)if(rows[i*3]>1||rows[i*3+1]>65535||rows[i*3+2]>65535)return 0;
    AcquireSRWLockExclusive(&s.lock);
    if(s.executionOwner||s.activityDepth||s.activityKeep){ReleaseSRWLockExclusive(&s.lock);return 0;}
    for(unsigned i=0;i<count;++i)s.activitySources[i]={rows[i*3]!=0,rows[i*3+1],rows[i*3+2]};s.activitySourcesReady=true;
    ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_note_kernel_activity_snapshot(void* pointer,unsigned long long* out,unsigned capacity){
    if(!pointer||!out||capacity<8)return 0;auto& s=*static_cast<MidiNoteKernel*>(pointer);AcquireSRWLockShared(&s.lock);
    const unsigned long long values[]{s.activity?1ull:0ull,s.activity?s.activity->visibleRevision.load(std::memory_order_acquire):0,
        s.activityNotes,s.activityClears,s.activityRefusals,s.activityFrameActions,s.activitySourcesReady?1ull:0ull,s.activityDirty?1ull:0ull};
    std::copy_n(values,8,out);ReleaseSRWLockShared(&s.lock);return 8;
}
extern "C" __declspec(dllexport) void* __cdecl midi_note_kernel_create(unsigned channels,void* emit,void* error){
    if(!channels||channels>128||!emit||!error)return nullptr;
    return new(std::nothrow) MidiNoteKernel(channels,emit,error);
}
extern "C" __declspec(dllexport) void __cdecl midi_note_kernel_destroy(void* pointer){
    if(pointer)static_cast<MidiNoteKernel*>(pointer)->release(); // Root only; input/control completion barrier FIRST.
}
extern "C" __declspec(dllexport) int __cdecl midi_note_kernel_configure(void* pointer,
        const unsigned* values,unsigned count,unsigned overlap){
    if(!pointer||!values||count!=128||overlap>1)return 0;
    for(unsigned i=0;i<128;++i)if(values[i]>127)return 0;
    auto& s=*static_cast<MidiNoteKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.executionOwner&&!s.executionPaused){ReleaseSRWLockExclusive(&s.lock);return 0;}
    std::copy_n(values,128,s.velocity.begin());s.overlap=overlap!=0;ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_note_kernel_seed(void* pointer,const unsigned long long* counts,unsigned count){
    if(!pointer||!counts||count!=128*128)return 0;
    auto& s=*static_cast<MidiNoteKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.activity||(s.executionOwner&&!s.executionPaused)){ReleaseSRWLockExclusive(&s.lock);return 0;}
    std::copy_n(counts,count,s.counts.begin());ReleaseSRWLockExclusive(&s.lock);return 1;
}
// Initial/control handoff only; active-session mask changes use the FIFO.
extern "C" __declspec(dllexport) int __cdecl midi_note_kernel_mixer_seed(void* pointer,const unsigned char* mask,unsigned count){
    if(!pointer||!mask||count!=128)return 0;auto& s=*static_cast<MidiNoteKernel*>(pointer);
    AcquireSRWLockExclusive(&s.lock);
    if(!s.executionOwner)for(unsigned i=0;i<count;++i)s.mixerMuted[i]=mask[i]!=0;
    ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_note_kernel_snapshot(void* pointer,unsigned long long* counts,unsigned capacity){
    if(!pointer||!counts||capacity<128*128)return 0;
    auto& s=*static_cast<MidiNoteKernel*>(pointer);AcquireSRWLockExclusive(&s.lock);
    std::copy(s.counts.begin(),s.counts.end(),counts);ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_note_kernel_frequency(){
    LARGE_INTEGER value{};QueryPerformanceFrequency(&value);return value.QuadPart;
}
extern "C" __declspec(dllexport) int __cdecl midi_note_kernel_run(void* pointer,unsigned stream,unsigned mode,unsigned port,
        const unsigned char* input,unsigned length,unsigned char* output,unsigned capacity,MidiNoteAction* actions,unsigned actionCapacity,
        void* timing,const unsigned long long* origin,unsigned long long* result){
    // result[0..4]: output bytes, original action count, admitted native events,
    // full signed-error bits, submission count. Status1=accepted,2=failed,
    //3=all suppressed/no submit. 0=unsupported,-1=ABI,-2=counter overflow:
    // ALL no side effects before fallback. Timing must borrow THIS DLL image.
    if(result)std::fill_n(result,5,0ull);
    constexpr unsigned allowed=BASS_MIDI_EVENTS_RAW|BASS_MIDI_EVENTS_FILTER|BASS_MIDI_EVENTS_SYNC|BASS_MIDI_EVENTS_ASYNC;
    if(!pointer||!stream||!input||!output||!actions||!origin||!result||port>=8||
       mode&~allowed||(mode&BASS_MIDI_EVENTS_RAW)!=BASS_MIDI_EVENTS_RAW)return -1;
    if(!length||length>1024*1024||length%3)return 0; // Component resource/grammar, NOT MIDI admission.
    auto& s=*static_cast<MidiNoteKernel*>(pointer);
    if(capacity<length*2+4||actionCapacity<length/3)return -1;
    // Qualify the WHOLE frame before effects. Malformed/running-status/mixed
    // frames retain their exact original interpreter, including prefix effects.
    for(unsigned i=0;i<length;i+=3){
        const unsigned command=input[i]&0xf0u,channel=port*16+(input[i]&15u);
        if((command!=0x80u&&command!=0x90u)||channel>=s.channels||input[i+1]>127||input[i+2]>127)return 0;
    }
    AcquireSRWLockExclusive(&s.lock);
    if(s.executionOwner){ReleaseSRWLockExclusive(&s.lock);return -3;}
    if(++s.epoch==0){s.epochs.fill(0);s.epoch=1;}
    // Counter-only admission must finish before transform writes caller buffers
    // or stamps QPC. Future activity consumers cannot roll back prefix identity
    // FIFO/Played effects merely by restoring these overlap counters.
    unsigned changed=0;
    for(unsigned i=0;i<length;i+=3){
        const unsigned status=input[i],key=input[i+1],velocity=input[i+2],channel=port*16+(status&15u),index=channel*128+key;
        if(s.epochs[index]!=s.epoch){s.epochs[index]=s.epoch;s.previous[index]=s.counts[index];s.touched[changed++]=index;}
        const bool on=(status&0xf0u)==0x90u&&velocity;
        if(on&&s.counts[index]==UINT64_MAX){
            for(unsigned j=0;j<changed;++j){const unsigned cell=s.touched[j];s.counts[cell]=s.previous[cell];}
            ReleaseSRWLockExclusive(&s.lock);return -2; // No wrap, cap, partial emit or false success.
        }
        if(on)++s.counts[index];else if(s.counts[index])--s.counts[index];
    }
    for(unsigned j=0;j<changed;++j){const unsigned cell=s.touched[j];s.counts[cell]=s.previous[cell];}
    if(s.activity&&!s.activitySourcesReady){++s.activityRefusals;ReleaseSRWLockExclusive(&s.lock);return 0;}
    MidiActivityLease activityLease(&s,input,length,port,length/3);
    if(!activityLease.ready){ReleaseSRWLockExclusive(&s.lock);return -4;}
    s.humanization.begin(stream,origin);
    unsigned bytes=s.channels>16?4u:0u,records=0;
    if(bytes){output[0]=0xff;output[1]=0x21;output[2]=1;output[3]=static_cast<unsigned char>(port);}
    for(unsigned i=0;i<length;i+=3){
        const unsigned status=input[i],key=input[i+1],velocity=input[i+2],channel=port*16+(status&15u);
        s.transform(status,key,velocity,channel,output,bytes,actions[records++]);
        if(s.activity)s.observeActivity(actions[records-1],s.activitySources[channel]);
    }
    result[1]=records;
    const unsigned prefix=s.channels>16?4u:0u;
    if(bytes==prefix){ReleaseSRWLockExclusive(&s.lock);return 3;}
    result[0]=bytes;
    // Original driver-entry/read/pid/epoch/sequence/quality, never the action
    // processing QPC. Observe transformed nonzero Note Ons exactly as old
    // register_raw; zero-velocity transformation is a native Note Off.
    if(timing&&origin[0])for(unsigned i=prefix;i<bytes;i+=3){
        if((output[i]&0xf0u)!=0x90u||!output[i+2])continue;
        const unsigned channel=port*16+(output[i]&15u),key=output[i+1];
        static_cast<PlaybackTiming*>(timing)->receive(channel,key,origin[0],origin[1],origin[2],origin[3],origin[4],origin[6]?origin[5]:2,s.humanization.delayAt(i));
    }
    result[4]=1;const DWORD admitted=s.humanization.spans.empty()&&!s.humanization.failed?s.emit(stream,mode,output,bytes):s.humanization.emit(stream,mode,output,bytes,0);result[2]=admitted;
    const bool accepted=admitted!=0xffffffffu;
    const int rawError=accepted?0:s.error();
    if(s.rawContext)raw_decode_context_observe(s.rawContext,stream,mode,output,bytes,accepted,rawError);
    if(!accepted){result[3]=static_cast<unsigned>(rawError);if(timing&&origin[0])playback_timing_cancel(timing,origin[0]);}
    // Existing parser mutates overlap/activity even when BASS rejects audio.
    // Do NOT silently retry or roll those changes back on native failure.
    ReleaseSRWLockExclusive(&s.lock);return accepted?1:2;
}
static void CALLBACK playbackNote(HSYNC,DWORD,DWORD data,void* pointer){if(pointer)static_cast<PlaybackTiming*>(pointer)->note(data);}
// Input/control origin validator shared across reconnects/consumers. Same
// MID3 validity/sequence/loss semantics as WinMMMidiPipeServer._set_origin.
// New-producer allocation is input-side only, never a DSP callback. History
// lifetime matches the existing server dictionary; no arbitrary producer cap.
struct MidiExecutionOrigin {
    struct Producer {unsigned long long pid,epoch,sequence,losses;};
    SRWLOCK lock=SRWLOCK_INIT;
    std::atomic<unsigned> references{1};
    unsigned long long frequency;
    std::vector<Producer> producers;
    MidiExecutionOrigin(){LARGE_INTEGER f{};QueryPerformanceFrequency(&f);frequency=f.QuadPart;producers.reserve(8);}
    void retain(){references.fetch_add(1,std::memory_order_relaxed);}
    void release(){if(references.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
    void prepare(const unsigned long long* descriptor,unsigned long long* prepared,unsigned long long& delta,unsigned long long& gap){
        const auto* source=descriptor+5;const auto read=descriptor[4];
        const bool flagsValid=source[7]==0||((source[7]&~7ull)==0x100)||((source[7]&~7ull)==0x80000000ull);
        bool valid=descriptor[0]==3&&source[1]==frequency&&source[0]>0&&source[2]>0&&source[3]>0&&source[6]>0&&flagsValid&&source[0]<=read+1;
        delta=gap=0;
        if(valid){
            AcquireSRWLockExclusive(&lock);
            auto it=std::find_if(producers.begin(),producers.end(),[&](const Producer& p){return p.pid==source[6]&&p.epoch==source[2];});
            if(it!=producers.end()){
                if(source[3]!=it->sequence+1)gap=1;
                delta=source[5]>it->losses?source[5]-it->losses:0;
                if(source[5]<it->losses)valid=false;
                it->sequence=source[3];it->losses=source[5];
            }else{
                delta=source[5];
                try{producers.push_back({source[6],source[2],source[3],source[5]});}
                catch(const std::bad_alloc&){valid=false;gap=2;} // Explicit unavailable tracker, no false clean interval.
            }
            ReleaseSRWLockExclusive(&lock);
        }
        const unsigned long long result[]{valid?std::min(source[0],read):read,read,source[6],source[2],source[3],valid?1ull:0ull,1};
        std::copy_n(result,7,prepared);
    }
};
extern "C" __declspec(dllexport) void* __cdecl midi_execution_origin_create(){try{return new MidiExecutionOrigin;}catch(const std::bad_alloc&){return nullptr;}}
extern "C" __declspec(dllexport) void __cdecl midi_execution_origin_release(void* pointer){if(pointer)static_cast<MidiExecutionOrigin*>(pointer)->release();}

// Native queue consumer. One frozen prepared kernel/queue lease; product
// command FIFO/live publication admission is still a separate required gate.
// Original notes use a bounded FIFO, NEVER observational coalescing. Filled
// output storage backpressures execution and then ingress, never drops events.
static void midiRouteOutput(void*,unsigned,const unsigned char*,unsigned);
static void midiRouteSemantic(void*,unsigned,unsigned);
struct MidiExecutionWorker {
    int settleFailedSelection(){
        AcquireSRWLockExclusive(&kernel->lock);auto* state=kernel->selection;auto reader=kernel->selectionQueueReader;
        if(!state||!reader||kernel->compatibilityCallback){ReleaseSRWLockExclusive(&kernel->lock);return 0;}
        struct PhaseLease{MidiCCKernel* value;bool locked=true;~PhaseLease(){if(locked)ReleaseSRWLockExclusive(&value->lock);}
            void yield(){ReleaseSRWLockExclusive(&value->lock);locked=false;Sleep(1);AcquireSRWLockExclusive(&value->lock);locked=true;}}phase{kernel};
        state->retain();
        struct Lease{NativeSelectionState* value;~Lease(){value->release();}}lease{state};
        if(kernel->selectionSettlementControlOnly){if(state->failedTicket())return 0;kernel->selectionSettlementControlOnly=false;}
        while(state->settleLocked){int error=0;const int outcome=midi_selection_state_retry_owned_unlock(state,kernel->lockAddress,&error);
            if(outcome==1)break;if(outcome!=-2)return -5;phase.yield();}
        if(!state->failedTicket()&&!state->settleLocked)return 0;
        std::array<unsigned long long,4096> tickets{};
        const int ticketTotal=midi_selection_state_failed_tickets(state,tickets.data(),static_cast<unsigned>(tickets.size()));
        if(ticketTotal<=0)return 0;
        for(int index=0;index<ticketTotal;++index){
            for(;;){int result[3]{};
                const int outcome=midi_selection_state_settle_failed(state,tickets[index],kernel->lockAddress,
                    reinterpret_cast<void*>(reader),kernel->errorAddress,result);
                // Native retry remains on the actual lock-owning actor. A
                // failed unlock is never transferred to Python or another
                // thread, and no original submission is repeated.
                if(outcome==-2){
                    for(;;){int error=0;const int unlocked=midi_selection_state_retry_owned_unlock(state,kernel->lockAddress,&error);
                        if(unlocked==1)break;if(unlocked!=-2)return -5;phase.yield();}
                    if(result[0]){kernel->selectionSettlementControlOnly=true;return -5;}
                    return state->failedTicket()?-6:1;
                }
                if(outcome==1)break;
                if(stopping.load()||explicitStop.load())return -5;
                // The unchanged source barrier waits for ALL three queues to
                // empty. No queue CANCEL or speculative budget retirement.
                if(outcome==0&&!result[0])return -6;
                kernel->selectionSettlementControlOnly=true;return -5;
            }
        }
        return 1;
    }
    struct Control;
    // Prepared file playback uses this SAME ingress consumer and RAW kernel.
    // These are product admission bounds matching MidiDocument, not BASS maxima.
    // Only steady scheduling is native here: file Reset/chase/EOF release remain
    // explicit facade checkpoints until their original contracts are qualified.
    struct FileRow {double seconds;unsigned offset,size,port,reserved;};
    static_assert(sizeof(FileRow)==24,"native prepared file row ABI1");
    struct FileTicket {unsigned long long publication,row,received;bool acked=false;int shiftedKey=-1;bool chase=false;};
    struct FileSchedule {
        std::vector<FileRow> rows;std::vector<unsigned char> bytes;
        FileHumanization human;
        std::vector<FileRow> chaseRows;std::vector<unsigned char> chaseBytes;
        unsigned chaseNext=0;bool chasing=false,chasePrepared=false,chaseResetQueued=false,chaseReady=false,chaseResume=false,chaseFailed=false;
        std::vector<FileTicket> tickets;std::array<bool,128> muted{};
        double duration=0,position=0,speed=1;unsigned next=0,acked=0;
        unsigned long long anchor=0,frequency=0,lastComplete=0;int transpose=0;
        bool playing=false,complete=false;
        std::unique_ptr<Control> release;
        unsigned long long releaseTicket=0;bool releaseQueued=false,releaseDone=false,releaseAcked=true;
        bool releaseFailed=false;unsigned releasePhase=0;int releaseError=0;
        std::unique_ptr<Control> loopPanic,loopReset;
        unsigned long long loopIterations=0;unsigned loopSource=29,loopQueued=0,loopActive=0,loopPhase=0;
        int loopError=0;bool looping=false,loopRestartAllowed=false,loopFailed=false,loopPressure=false;
        double at(unsigned long long tick)const{
            return std::min(duration,position+(playing?static_cast<double>(tick-anchor)/frequency*speed:0));
        }
        const FileRow& sourceRow(const FileTicket& ticket)const{return (ticket.chase?chaseRows:rows)[static_cast<unsigned>(ticket.row)];}
        const unsigned char* sourceBytes(const FileTicket& ticket)const{return (ticket.chase?chaseBytes:bytes).data()+sourceRow(ticket).offset;}
    };
    std::unique_ptr<FileSchedule> file;
    // File-only key transformation happens AFTER this actor popped the original
    // publication, not when it was queued: prior Bank/Program/Reset/control
    // publications must have established the channel's actual drum identity.
    // Same transpose_midi contract: only three-byte Note/Off/Poly Pressure,
    // preserve percussion, drop an out-of-domain key instead of clamping.
    bool fileTransform(unsigned length){
        if(!ingress||inputClient.load()!=UINT64_MAX)return true;
        AcquireSRWLockExclusive(acceptanceLock());bool keep=true;
        if(file&&!file->tickets.empty()){
            auto& ticket=file->tickets.back();
            if(ticket.publication==inputPublication&&!ticket.acked&&ticket.row<(ticket.chase?file->chaseRows.size():file->rows.size())){
                const auto& row=file->sourceRow(ticket);
                if(file->transpose&&length==3&&row.size==length&&row.port==port&&kernel->selection){
                    const unsigned command=input[0]&0xf0u,channel=port*16+(input[0]&15u);
                    const bool drums=(kernel->selection->requestedAddress[channel].load(std::memory_order_acquire)&0x1000000u)!=0;
                    if((command==0x80u||command==0x90u||command==0xa0u)&&!drums){
                        const int key=static_cast<int>(input[1])+file->transpose;
                        if(key<0||key>127){ticket.acked=true;++file->acked;file->lastComplete=inputPublication;keep=false;}
                        else{ticket.shiftedKey=key;input[1]=static_cast<unsigned char>(key);}
                    }
                }
            }
        }
        ReleaseSRWLockExclusive(acceptanceLock());
        if(!keep)inputClient.store(0);
        return keep;
    }
    // Call only under acceptanceLock(). Native look-ahead owns due-order;
    // original RAW frames and the single ingress execution owner remain unchanged.
    void fileAdmit(){
        if(!file||(!file->playing&&!file->chasing)||!ingress||!ingress->accepting||!directQueue)return;
        LARGE_INTEGER stamp{};QueryPerformanceCounter(&stamp);const auto tick=static_cast<unsigned long long>(stamp.QuadPart);
        auto& plan=*file;
        const bool varying=!plan.chasing&&(kernel->notes->humanization.timing()||plan.human.active);
        const double position=varying?plan.position+(plan.playing?static_cast<double>(tick-plan.anchor)/plan.frequency*plan.speed:0):plan.at(tick);
        if(plan.chasing&&!plan.chaseReady)return;
        if(!plan.tickets.empty()&&plan.tickets.back().publication>plan.lastComplete)return;
        // Reclaim only fully ACKed original owners. Retained source/trace/error
        // receipts survive loop boundaries; capacity is prepared off the actor.
        if(plan.acked==plan.tickets.size()){plan.tickets.clear();plan.acked=0;}
        auto& sourceRows=plan.chasing?plan.chaseRows:plan.rows;
        auto& sourceBytes=plan.chasing?plan.chaseBytes:plan.bytes;
        auto& next=plan.chasing?plan.chaseNext:plan.next;
        if(varying){
            if(!plan.human.active)plan.human.reset(next);
            try{plan.human.fill(sourceRows,sourceBytes,position,plan.speed,kernel->notes->humanization.timing(),kernel->notes->humanization.preserves(),tick,plan.frequency);}
            catch(const std::bad_alloc&){plan.chaseFailed=true;plan.playing=false;return;}
        }
        auto consume=[&](bool skipped=false){if(varying){if(skipped)plan.human.skip();else plan.human.commit();next=plan.human.frontier;}else ++next;};
        while(varying?plan.human.ready(tick):next<sourceRows.size()&&(plan.chasing||sourceRows[next].seconds<=position)){
            const unsigned sourceIndex=varying?plan.human.peek().index:next;
            const auto& row=sourceRows[sourceIndex];
            if(!row.size){consume();continue;}
            if(plan.tickets.size()==plan.tickets.capacity()){plan.loopPressure=true;return;}
            plan.loopPressure=false;
            auto& q=*directQueue;AcquireSRWLockExclusive(&q.lock);
            if(q.count==MidiPipeQueue::frameCapacity||row.size>MidiPipeQueue::byteCapacity-q.used||ingress->readCount==UINT64_MAX){
                ReleaseSRWLockExclusive(&q.lock);return;}
            // Queue original bytes. Non-neutral keys are resolved only after
            // this publication reaches the single actor's execution boundary.
            const auto* source=sourceBytes.data()+row.offset;
            const unsigned command=source[0]&0xf0u;
            const unsigned channel=row.port*16+(source[0]&15u);
            if(command==0x90u&&source[row.size-1]&&plan.muted[channel]){
                consume(true);ReleaseSRWLockExclusive(&q.lock);continue;}
            MidiPipeQueue::Frame frame;frame.kind=2;frame.size=row.size;frame.directPort=row.port;frame.read=tick;frame.trace=0;
            const unsigned first=std::min(row.size,MidiPipeQueue::byteCapacity-q.byteTail);
            std::memcpy(q.storage.data()+q.byteTail,source,first);
            if(first<row.size)std::memcpy(q.storage.data(),source+first,row.size-first);
            q.byteTail=(q.byteTail+row.size)%MidiPipeQueue::byteCapacity;q.used+=row.size;
            frame.publication=++ingress->readCount;++q.readCount;q.totalBytes+=row.size;
            q.frames[(q.head+q.count)%MidiPipeQueue::frameCapacity]=frame;++q.count;
            q.peakFrames=std::max(q.peakFrames,static_cast<unsigned long long>(q.count));
            q.peakBytes=std::max(q.peakBytes,static_cast<unsigned long long>(q.used));
            plan.tickets.push_back({frame.publication,sourceIndex,tick,false,-1,plan.chasing});consume();
            SetEvent(ingress->ready);WakeAllConditionVariable(&q.changed);ReleaseSRWLockExclusive(&q.lock);
            // Admit only one event per consumer iteration: preserve execution-time
            // Bank/Program/Reset drum state before transforming the following row.
            return;
        }
        if(plan.chasing){plan.chasing=false;plan.chaseReady=false;if(plan.chaseResume){plan.playing=true;plan.anchor=tick;}return;}
        if(plan.next==plan.rows.size()&&position>=plan.duration){
            if(plan.looping&&kernel->compatibilityCallback.load(std::memory_order_acquire)){
                // Keep the completed original iteration/receipts. The existing
                // Python EOF path owns callback-bearing Panic/FILE Reset.
                plan.looping=false;plan.loopRestartAllowed=false;plan.loopPressure=false;
                plan.complete=true;plan.position=plan.duration;plan.playing=false;return;
            }
            if(plan.looping&&plan.duration>0){
                if(plan.acked!=plan.tickets.size()){plan.loopPressure=true;return;}
                if(!plan.loopPanic||!plan.loopReset||controlCount+2>controls.size()||controlSerial>UINT64_MAX-2){
                    plan.loopPressure=true;return;}
                const auto fence=acceptedFence();
                for(auto* command:{plan.loopPanic.get(),plan.loopReset.get()}){
                    command->ticket=++controlSerial;command->fence=fence;command->submitted=tick;
                    command->transportComplete=false;command->transportPhase=0;command->transportError=0;
                }
                if(!controlCount)controlFence.store(fence);
                controls[(controlHead+controlCount)%controls.size()]=std::move(plan.loopPanic);++controlCount;
                controls[(controlHead+controlCount)%controls.size()]=std::move(plan.loopReset);++controlCount;
                controlsPending.store(true);plan.loopQueued=2;plan.loopRestartAllowed=true;plan.loopPressure=false;
            }else if(plan.release){
                if(controlCount==controls.size())return;
                plan.release->fence=acceptedFence();
                if(!controlCount)controlFence.store(plan.release->fence);
                controls[(controlHead+controlCount)%controls.size()]=std::move(plan.release);
                ++controlCount;controlsPending.store(true);plan.releaseQueued=true;
            }else plan.complete=true;
            plan.position=plan.duration;plan.playing=false;
        }
    }
    DWORD fileWait(){
        AcquireSRWLockShared(acceptanceLock());DWORD result=INFINITE;
        if(file&&file->chasing&&file->chaseReady)result=1;
        else if(file&&file->playing){LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);
            double next=file->next<file->rows.size()?file->rows[file->next].seconds:file->duration;
            const bool varying=file->human.active||kernel->notes->humanization.timing();
            const double at=varying?file->position+static_cast<double>(tick.QuadPart-file->anchor)/file->frequency*file->speed:file->at(tick.QuadPart);
            if(varying){
                next=file->human.future.empty()?file->duration:at+(static_cast<double>(file->human.peek().target)-tick.QuadPart)/file->frequency*file->speed;
                if(file->human.cursor<file->rows.size())next=std::min(next,file->rows[file->human.cursor].seconds-.3*file->speed);
            }
            const double remaining=(next-at)/file->speed;
            result=static_cast<DWORD>(std::max(1.0,std::min(20000.0,std::ceil(remaining*1000))));}
        ReleaseSRWLockShared(acceptanceLock());return result;
    }
    static constexpr unsigned inputCapacity=1024*1024,recordCapacity=4096,noteCapacity=16*1024*1024;
    // Prepared scalar controls, same native StreamEvent ABI as existing UI.
    // Publication fence is already-admitted ingress frames, not wall-clock
    // guesswork or a batching delay. General generation/FX/free commands remain.
    struct ControlStep {unsigned channel,event,param;};
    struct ControlResult {unsigned channel,event,param,accepted;int error;};
    struct ManualStep {unsigned channel,source,destination,flags;int input,amount;unsigned event,param;};
    struct InstrumentStep {unsigned channel,mask,bank,lsb,program;};
    struct PresetToneStep {unsigned bank,program,flags,tone[6];};
    struct Control {
        unsigned long long ticket=0,fence=0,submitted=0;
        bool continueOnError=false;
        bool macro=false,batch=false,manualReset=false,sourceEdit=false,traceFence=false;unsigned batchChannel=0,fileResetSource=0,systemMode=0;void* macroGetter=nullptr;
        bool systemModeCommand=false;unsigned systemStage=0,systemRestoreNext=0;std::vector<ControlStep> systemRestore;
        bool manualPanic=false;
        bool mixerMask=false;std::array<bool,128> mixerMuted{};
        bool transportRelease=false;unsigned loopStage=0,transportPhase=0;int transportError=0;bool transportComplete=false;bool chaseReset=false;
        unsigned activityKind=0;bool activityAvailableKnown=false;std::vector<unsigned> activityAvailable;
        std::vector<BASS_MIDI_EVENT> batchEvents;
        bool sendFloor=false;unsigned floorEvent=0,floorValue=0;
        bool master=false;std::array<unsigned,2> masterPresent{};
        unsigned resetPolicyMask=0;std::array<unsigned,6> resetPolicyValues{};
        std::vector<InstrumentStep> instruments;
        std::vector<PresetToneStep> presetTone;void* presetToneLock=nullptr;unsigned presetToneChannels=0;
        std::vector<MidiSendResult> floorRows;
        FXTopologyPlan* topology=nullptr;
        StreamVendorPlan* streamEffect=nullptr;
        StreamEQPlan* equalizer=nullptr;
        std::array<unsigned char,20+sizeof(StreamEQSnapshot)> equalizerResult{};
        std::array<unsigned char,20+sizeof(StreamVendorSnapshot2)> streamEffectResult{};
        std::array<unsigned char,20+64*sizeof(FXTopologySnapshot2)> topologyResult{};
        std::vector<ControlStep> steps;std::vector<ControlResult> results;
        std::vector<ManualStep> manualSteps;MidiCCSemanticState* generation=nullptr;
        Control()=default;
        Control(const ControlStep* input,unsigned count):steps(input,input+count),results(count){}
        Control(const ManualStep* input,unsigned count,MidiCCSemanticState* state):results(count),manualSteps(input,input+count),generation(state){generation->retain();}
        ~Control(){if(generation)generation->release();if(topology)topology->release();if(streamEffect)streamEffect->release();if(equalizer)equalizer->release();}
    };
    static_assert(sizeof(ControlStep)==12&&sizeof(ControlResult)==20&&sizeof(ManualStep)==32,"execution control/manual ABI1");
    std::array<std::unique_ptr<Control>,256> controls; // Product resource policy, not BASS limit.
    unsigned controlHead=0,controlCount=0;
    unsigned long long controlSerial=0;
    std::atomic<bool> controlsPending{false};
    std::atomic<unsigned long long> controlFence{0};
    struct Record {std::array<unsigned long long,32> values{};unsigned noteBytes=0,payloadBytes=0;unsigned long long client=0,publication=0;unsigned clientSlot=0;bool activityApplied=false;};
    // Producer-space reclamation is NOT semantic/client ACK. A separate native
    // non-audio thread copies exact records/bytes into retained owners. Python
    // still commits the original FIFO through the unchanged read/resume ABI.
    struct RetainedRecord {Record record;std::vector<unsigned char> bytes;std::unique_ptr<RetainedRecord> next;};
    std::unique_ptr<RetainedRecord> retainedHead;RetainedRecord* retainedTail=nullptr;
    unsigned long long retainedCount=0,retainedBytes=0,retainedTransferred=0,retainedAllocationFailures=0;
    HANDLE retentionThread=nullptr;bool retentionEnabled=false,retentionEnded=false,retentionClosing=false;
    MidiPipeQueue* queue;MidiCCKernel* kernel;MidiExecutionOrigin* origins;PlaybackTiming* timing;
    MidiIngressSession* ingress=nullptr;
    std::unique_ptr<MidiPipeQueue> directQueue;
    std::atomic<unsigned long long> inputClient{0};unsigned inputSlot=0;unsigned long long inputPublication=0;
    unsigned stream,mode,port;
    HANDLE thread=nullptr;
    SRWLOCK lock=SRWLOCK_INIT;CONDITION_VARIABLE changed=CONDITION_VARIABLE_INIT;
    std::atomic<bool> stopping{false},started{false};
    std::atomic<bool> explicitStop{false};
    std::atomic<bool> ended{false};
    std::atomic<bool> acceptingControls{true};
    std::atomic<bool> traceEnabled{false};
    MidiRecordingSink* recording=nullptr; // retained; lock protects bind/capture
    std::atomic<bool> recordingAttached{false};
    // Optional debugging has no audio/completion owner. Admit a whole begin/end
    // pair or account for its loss; never wait for Python or copy a whole SysEx.
    // Product budget, not MIDI/BASS domain: matches the original 256-byte log
    // preview. Mandatory receipts stay lossless in their separate FIFO.
    struct TracePair {
        std::array<unsigned long long,34> begin{},end{};
        std::array<unsigned char,256> prefix{};
        unsigned size=0;bool beginRead=false;
    };
    static constexpr unsigned traceCapacity=2048;
    std::vector<TracePair> tracePairs;
    unsigned traceHead=0,traceCount=0;
    unsigned long long traceProduced=0,traceDropped=0,traceRead=0;
    MidiExecutionIdle idle;
    bool paused=false,delivered=false;
    unsigned resume=0,pendingKind=0,head=0,count=0,noteHead=0,noteTail=0,noteUsed=0;
    unsigned long long serial=0,pendingTicket=0;
    // Additive metadata-copy batch: exact original records, never MIDI merging.
    // Native client leases survive until the same consumer ACKs the whole
    // copied batch. Storage/ring limits remain unchanged.
    struct MetadataBatchOwner {unsigned long long ticket=0,client=0;unsigned slot=0;bool activityApplied=false;};
    std::array<MetadataBatchOwner,64> metadataBatch{};
    unsigned metadataBatchCount=0;
    unsigned long long metadataBatchReads=0,metadataBatchRecords=0,metadataBatchAcks=0;
    unsigned long long activityReadTicket=0;bool activityReadApplied=false;
    unsigned long long activityClearBaseline=0;
    unsigned long long presentationAttempt=0;
    std::vector<Record> records;
    std::vector<unsigned char> noteStorage,input,output;
    std::vector<MidiCCAction> actions;
    std::vector<MidiNoteAction> notes;
    std::array<unsigned long long,22> metrics{};
    MidiExecutionWorker(MidiPipeQueue* q,MidiCCKernel* k,MidiExecutionOrigin* o,PlaybackTiming* t,unsigned handle,unsigned flags,unsigned p):
        queue(q),kernel(k),origins(o),timing(t),stream(handle),mode(flags),port(p),records(recordCapacity),noteStorage(noteCapacity),
        input(inputCapacity),output(inputCapacity*5+4),actions(inputCapacity/3+1),notes((inputCapacity/3+1)*4){tracePairs.resize(traceCapacity);}
    ~MidiExecutionWorker(){
        if(recording)recording->release();
        if(retentionThread){AcquireSRWLockExclusive(&lock);retentionClosing=true;WakeAllConditionVariable(&changed);ReleaseSRWLockExclusive(&lock);
            WaitForSingleObject(retentionThread,INFINITE);CloseHandle(retentionThread);}
        if(directQueue&&directQueue->session){directQueue->session->release();directQueue->session=nullptr;}
        if(timing)timing->release();origins->release();if(ingress)ingress->release();
    }
    bool directPending(){
        if(!ingress||!directQueue)return false;
        AcquireSRWLockExclusive(&ingress->lock);AcquireSRWLockExclusive(&directQueue->lock);
        const bool pending=directQueue->count||ingress->clients[8].metadata.load()||inputClient.load()==UINT64_MAX||
            (file&&(file->playing||file->acked!=file->tickets.size()));
        ReleaseSRWLockExclusive(&directQueue->lock);ReleaseSRWLockExclusive(&ingress->lock);return pending;
    }
    void cancelDirectQueued(){
        // This runs ONLY after a terminal/explicit actor cancellation, never
        // during stop()'s existing directPending DRAIN branch. Every accepted
        // direct RAW keeps its original completion owner and receives an
        // explicit unexecuted receipt; no BASS call or fake successful PCM.
        if(!ingress||!directQueue)return;
        for(;;){
            AcquireSRWLockExclusive(&ingress->lock);AcquireSRWLockExclusive(&directQueue->lock);
            auto& q=*directQueue;
            if(file){file->playing=false;if(file->chasing){file->chasing=false;file->chaseFailed=true;file->chaseResume=false;file->chasePrepared=false;}}
            if(!q.count){ReleaseSRWLockExclusive(&q.lock);ReleaseSRWLockExclusive(&ingress->lock);break;}
            const auto frame=q.frames[q.head];
            const unsigned first=std::min(frame.size,MidiPipeQueue::byteCapacity-q.byteHead);
            std::memcpy(input.data(),q.storage.data()+q.byteHead,first);
            if(first<frame.size)std::memcpy(input.data()+first,q.storage.data(),frame.size-first);
            inputClient.store(UINT64_MAX);inputSlot=8;inputPublication=frame.publication;port=frame.directPort;
            q.used-=frame.size;q.byteHead=(q.byteHead+frame.size)%MidiPipeQueue::byteCapacity;
            q.head=(q.head+1)%MidiPipeQueue::frameCapacity;--q.count;++q.popped;++q.batches;
            ingress->popped=std::max(ingress->popped,frame.publication);WakeAllConditionVariable(&q.changed);
            ReleaseSRWLockExclusive(&q.lock);ReleaseSRWLockExclusive(&ingress->lock);
            std::array<unsigned long long,32> record{};
            record[1]=9;record[2]=static_cast<unsigned long long>(static_cast<long long>(-9));
            record[3]=frame.kind;record[4]=frame.size;record[5]=frame.read;std::copy_n(frame.origin,8,record.data()+6);
            LARGE_INTEGER now{};QueryPerformanceCounter(&now);record[31]=now.QuadPart;
            publish(record,0);inputClient.store(0);
        }
    }
    SRWLOCK* acceptanceLock(){return ingress?&ingress->lock:&queue->lock;}
    unsigned long long acceptedFence()const{return ingress?ingress->readCount:queue->readCount;}
    unsigned long long poppedFence()const{return ingress?ingress->popped:queue->popped;}
    void wakeIngress(){if(ingress)SetEvent(ingress->ready);else WakeAllConditionVariable(&queue->changed);}
    NativeMidiActivity* presentationOwner()const{return kernel&&kernel->notes?kernel->notes->activity:nullptr;}
    bool presentationPending(NativeMidiActivity& state)const{
        const auto quality=state.validity.load(std::memory_order_acquire);
        return (quality&1ull)&&
            (quality!=state.publishedValidity.load(std::memory_order_acquire)||
             state.visibleRevision.load(std::memory_order_acquire)!=state.publishedRevision.load(std::memory_order_acquire));
    }
    void publishPresentation(bool force=false){
        auto* state=presentationOwner();if(!state||!presentationPending(*state))return;
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);const auto tick=static_cast<unsigned long long>(now.QuadPart);
        if(!force&&presentationAttempt&&tick-presentationAttempt<state->frequency*17/1000)return;
        presentationAttempt=tick;native_activity_view_publish(state,tick);
    }
    DWORD presentationWait(){
        const DWORD normal=idle.wait();auto* state=presentationOwner();
        if(!state||!presentationPending(*state))return normal;
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);const auto tick=static_cast<unsigned long long>(now.QuadPart);
        const auto period=std::max<unsigned long long>(1,state->frequency*17/1000);
        const auto elapsed=presentationAttempt?tick-presentationAttempt:period;
        const auto remaining=elapsed>=period?1:period-elapsed;
        const DWORD wait=static_cast<DWORD>(std::max<unsigned long long>(1,(remaining*1000+state->frequency-1)/state->frequency));
        return std::min(normal,wait);
    }
    bool claimIdle(){
        // Queue admission and the claim share this lock. Already accepted
        // ingress/control work wins; work admitted after the claim follows
        // the reset, rather than racing a GUI timer's direct BASS calls.
        AcquireSRWLockExclusive(acceptanceLock());bool ready=!stopping.load()&&acceptingControls.load()&&
            controlCount==0&&acceptedFence()==poppedFence();
        if(ready&&ingress){for(auto& client:ingress->clients)if(client.queue){AcquireSRWLockShared(&client.queue->lock);
            ready=client.queue->count==0;ReleaseSRWLockShared(&client.queue->lock);if(!ready)break;}}
        else if(ready)ready=queue->count==0&&!queue->stopping&&!queue->ended;
        const bool claimed=ready&&idle.claim();ReleaseSRWLockExclusive(acceptanceLock());return claimed;
    }
    int pop(unsigned long long* descriptor){
        idle.changed.store(false,std::memory_order_release);
        if(!ingress)return midiQueuePop(queue,input.data(),inputCapacity,descriptor,1,this,&stopping,&controlsPending,&controlFence,presentationWait(),&idle.changed);
        for(;;){
            AcquireSRWLockExclusive(&ingress->lock);
            if(stopping.load()){ReleaseSRWLockExclusive(&ingress->lock);return 0;}
            fileAdmit();
            if(controlsPending.load()&&ingress->popped>=controlFence.load()){ReleaseSRWLockExclusive(&ingress->lock);return -4;}
            unsigned winner=9;unsigned long long ticket=UINT64_MAX;
            for(unsigned i=0;i<9;++i){auto* q=ingress->clients[i].queue;if(!q)continue;
                AcquireSRWLockExclusive(&q->lock);
                if(q->count&&q->frames[q->head].publication<ticket){ticket=q->frames[q->head].publication;winner=i;}
                ReleaseSRWLockExclusive(&q->lock);
            }
            if(winner<9){
                auto& client=ingress->clients[winner];auto* q=client.queue;AcquireSRWLockExclusive(&q->lock);
                const auto& frame=q->frames[q->head];const unsigned first=std::min(frame.size,MidiPipeQueue::byteCapacity-q->byteHead);
                std::memcpy(input.data(),q->storage.data()+q->byteHead,first);
                if(first<frame.size)std::memcpy(input.data()+first,q->storage.data(),frame.size-first);
                descriptor[0]=frame.kind;descriptor[1]=frame.size;descriptor[2]=0;descriptor[3]=static_cast<unsigned long long>(frame.trace);
                descriptor[4]=frame.read;std::copy_n(frame.origin,8,descriptor+5);
                LARGE_INTEGER now{};QueryPerformanceCounter(&now);q->maxAge=std::max(q->maxAge,static_cast<unsigned long long>(now.QuadPart)-frame.read);
                inputClient.store(client.id);inputSlot=winner;inputPublication=frame.publication;port=winner==8?frame.directPort:client.port;
                if(frame.kind==3&&(frame.origin[7]&~7ull)==0x80000000ull)port=static_cast<unsigned>(frame.origin[7]&7);
                ingress->popped=frame.publication;q->used-=frame.size;q->byteHead=(q->byteHead+frame.size)%MidiPipeQueue::byteCapacity;
                q->head=(q->head+1)%MidiPipeQueue::frameCapacity;--q->count;++q->popped;++q->batches;WakeAllConditionVariable(&q->changed);
                ReleaseSRWLockExclusive(&q->lock);ReleaseSRWLockExclusive(&ingress->lock);return 1;
            }
            ReleaseSRWLockExclusive(&ingress->lock);
            if(idle.changed.load(std::memory_order_acquire))return -9;
            // Late ASYNC selection callbacks may create an ambiguous owner
            // while pop is waiting internally, after the last original MIDI.
            // Bound only the outstanding selection lifetime's wait so the
            // original actor can service its queue-zero barrier. Ordinary
            // idle sessions retain their old wait; no global timer change.
            bool selectionPending=false;
            {AcquireSRWLockShared(&kernel->lock);auto* selected=kernel->selection;
                selectionPending=selected&&(selected->queuedReservation.load()||selected->failedTicket());
                ReleaseSRWLockShared(&kernel->lock);}
            const DWORD wait=std::min(presentationWait(),fileWait());
            const DWORD result=WaitForSingleObject(ingress->ready,selectionPending?std::min(wait,20ul):wait);
            if(result==WAIT_TIMEOUT&&selectionPending)return -4;
            if(result==WAIT_TIMEOUT)return -9;
            if(result!=WAIT_OBJECT_0)return -5;
        }
    }
    void metric(unsigned slot,unsigned long long amount=1){AcquireSRWLockExclusive(&lock);metrics[slot]+=amount;ReleaseSRWLockExclusive(&lock);}
    void setPaused(bool value){
        AcquireSRWLockExclusive(&kernel->lock);kernel->executionPaused=value;
        AcquireSRWLockExclusive(&kernel->notes->lock);kernel->notes->executionPaused=value;ReleaseSRWLockExclusive(&kernel->notes->lock);
        ReleaseSRWLockExclusive(&kernel->lock);
        AcquireSRWLockExclusive(&lock);paused=value;ReleaseSRWLockExclusive(&lock);
    }
    static unsigned __stdcall retainRecords(void* pointer){
        auto& s=*static_cast<MidiExecutionWorker*>(pointer);
        for(;;){
            AcquireSRWLockExclusive(&s.lock);
            while(!s.count&&!s.ended&&!s.retentionClosing)SleepConditionVariableSRW(&s.changed,&s.lock,INFINITE,0);
            if(s.retentionClosing||(!s.count&&s.ended)){s.retentionEnded=true;WakeAllConditionVariable(&s.changed);ReleaseSRWLockExclusive(&s.lock);return 0;}
            const auto expected=s.records[s.head].values[0];const unsigned bytes=s.records[s.head].noteBytes+s.records[s.head].payloadBytes;
            ReleaseSRWLockExclusive(&s.lock);
            std::unique_ptr<RetainedRecord> node;
            try{node=std::make_unique<RetainedRecord>();node->bytes.resize(bytes);}
            catch(const std::bad_alloc&){
                // Retain the original prefix and every lease. Memory exhaustion
                // is real backpressure, never optional loss or fake success.
                AcquireSRWLockExclusive(&s.lock);++s.retainedAllocationFailures;WakeAllConditionVariable(&s.changed);ReleaseSRWLockExclusive(&s.lock);Sleep(1);continue;
            }
            AcquireSRWLockExclusive(&s.lock);
            if(!s.count||s.records[s.head].values[0]!=expected){ReleaseSRWLockExclusive(&s.lock);continue;}
            node->record=s.records[s.head];
            const unsigned first=std::min(bytes,noteCapacity-s.noteHead);
            if(first)std::memcpy(node->bytes.data(),s.noteStorage.data()+s.noteHead,first);
            if(first<bytes)std::memcpy(node->bytes.data()+first,s.noteStorage.data(),bytes-first);
            s.noteHead=(s.noteHead+bytes)%noteCapacity;s.noteUsed-=bytes;
            s.head=(s.head+1)%recordCapacity;--s.count;
            auto* tail=node.get();if(s.retainedTail)s.retainedTail->next=std::move(node);else s.retainedHead=std::move(node);
            s.retainedTail=tail;++s.retainedCount;s.retainedBytes+=bytes;++s.retainedTransferred;
            // Neither client metadata nor file-ticket ACK is changed here.
            WakeAllConditionVariable(&s.changed);ReleaseSRWLockExclusive(&s.lock);
        }
    }
    unsigned long long mandatoryFront()const {
        if(retentionEnabled)return retainedHead?retainedHead->record.values[0]:count?records[head].values[0]:UINT64_MAX;
        return count?records[head].values[0]:UINT64_MAX;
    }
    // Called with lock held by the sole metadata reader. Global serial keeps
    // source observations before their accepted trace-stop fence, but traces
    // never retain ingress leases or participate in mandatory ACK pressure.
    bool traceFirst()const {
        if(!traceCount)return false;
        const auto& pair=tracePairs[traceHead];const auto ticket=pair.beginRead?pair.end[0]:pair.begin[0];
        return ticket<mandatoryFront();
    }
    void publishTrace(const std::array<unsigned long long,32>& record,unsigned long long begin,unsigned long long end,unsigned bytes){
        AcquireSRWLockExclusive(&lock);
        if(traceCount==traceCapacity||serial>UINT64_MAX-2){++traceDropped;ReleaseSRWLockExclusive(&lock);return;}
        auto& pair=tracePairs[(traceHead+traceCount)%traceCapacity];
        std::copy(record.begin(),record.end(),pair.begin.begin());
        pair.begin[0]=++serial;pair.begin[1]=12;pair.begin[2]=port;pair.begin[4]=bytes;
        pair.size=std::min(bytes,256u);pair.begin[24]=pair.size;pair.begin[31]=begin;
        pair.begin[32]=inputClient.load();pair.begin[33]=inputPublication;
        pair.end=pair.begin;pair.end[0]=++serial;pair.end[1]=13;pair.end[31]=end;
        std::copy_n(input.data(),pair.size,pair.prefix.data());pair.beginRead=false;
        ++traceCount;++traceProduced;WakeAllConditionVariable(&changed);ReleaseSRWLockExclusive(&lock);
    }
    void publish(std::array<unsigned long long,32>& values,unsigned noteCount,const unsigned char* controlData=nullptr,unsigned actionOffset=0){
        const unsigned notesBytes=noteCount*sizeof(MidiNoteAction);
        const unsigned payloadBytes=values[1]==2||values[1]==3||values[1]==4||values[1]==5||values[1]==12||values[1]==13?static_cast<unsigned>(values[4]):0;
        const unsigned bytes=notesBytes+payloadBytes;
        AcquireSRWLockExclusive(&lock);
        // Stop is not permission to discard already accepted Note metadata.
        // A caller must drain it before release; timeout retains every owner.
        while(count==recordCapacity||bytes>noteCapacity-noteUsed){++metrics[14];SleepConditionVariableSRW(&changed,&lock,INFINITE,0);}
        values[0]=++serial;
        if(values[1]==2||values[1]==3||values[1]==5||values[1]==6||values[1]==8||values[1]==10||values[1]==11){pendingTicket=serial;pendingKind=static_cast<unsigned>(values[1]);metrics[13]=pendingKind;delivered=false;resume=0;}
        auto& record=records[(head+count)%recordCapacity];record.values=values;record.values[24]=noteCount;record.noteBytes=notesBytes;record.payloadBytes=payloadBytes;
        // Optional activity ownership is absent for the standalone ordered
        // record publisher as well as older/unattached execution fixtures.
        // Do not require an audio kernel merely to preserve/inspect records.
        record.activityApplied=kernel&&kernel->notes&&kernel->notes->activity&&!kernel->notes->activityDirty&&
            (noteCount||((values[1]==1||values[1]==4||values[1]==15)&&kernel->notes->activityClears!=activityClearBaseline));
        record.client=values[1]==4||values[1]==5||values[1]==11?0:inputClient.load();record.publication=record.client?inputPublication:0;record.clientSlot=inputSlot;
        if(ingress&&record.client)ingress->clients[inputSlot].metadata.fetch_add(1);
        auto copy=[&](const unsigned char* source,unsigned size){
            const unsigned first=std::min(size,noteCapacity-noteTail);std::memcpy(noteStorage.data()+noteTail,source,first);
            if(first<size)std::memcpy(noteStorage.data(),source+first,size-first);noteTail=(noteTail+size)%noteCapacity;
        };
        copy(reinterpret_cast<const unsigned char*>(notes.data()+actionOffset),notesBytes);copy(controlData?controlData:input.data(),payloadBytes);
        noteUsed+=bytes;++count;
        metrics[15]=std::max(metrics[15],static_cast<unsigned long long>(count));metrics[16]=std::max(metrics[16],static_cast<unsigned long long>(noteUsed));
        WakeAllConditionVariable(&changed);ReleaseSRWLockExclusive(&lock);
    }
    unsigned musicalActions(unsigned actionCount)const{
        // B0 activity-clear markers share ordering, but are not original Note
        // events and must not inflate timing/activity statistics.
        unsigned result=0;
        for(unsigned i=0;i<actionCount;++i){const unsigned command=notes[i].packed&0xf0u;if(command==0x80u||command==0x90u)++result;}
        return result;
    }
    bool nativeObserverCompletion(const std::array<unsigned long long,32>& values,unsigned long long captureBefore,
            const MidiCCSemanticState* semanticBefore,unsigned long long failuresBefore,bool nativeFile=false)const{
        // Suppress ONLY redundant successful canonical observations from
        // explicit pipe input or the separately verified native file owner.
        // Original audio, ordinary direct receipts, trace/error/handoff/
        // control/health/source/semantic records are never merged or dropped.
        if(!kernel||!kernel->notes||!kernel->notes->activityAuthoritative||!kernel->notes->activity||
                kernel->notes->activityDirty||values[1]!=1||values[2]!=1||
                (ingress?(!inputClient.load()||(inputClient.load()==UINT64_MAX&&!nativeFile)):!queue)||
                values[21]||values[22]||!values[23]||values[13]||values[26]||values[29]||
                kernel->captureSerial!=captureBefore||kernel->semantics!=semanticBefore||
                (kernel->semantics&&kernel->semantics->failures.load(std::memory_order_acquire)!=failuresBefore&&!kernel->deferredFailureLog)||
                !values[4]||values[27]!=values[4])return false;
        for(unsigned i=0;i<values[4];){const unsigned command=input[i]&0xf0u,width=command==0xd0u?2u:3u;
            if(command!=0x80u&&command!=0x90u&&command!=0xa0u&&command!=0xb0u&&command!=0xd0u&&command!=0xe0u)return false;
            if(i+width>values[4]||input[i+1]>127||(width==3&&input[i+2]>127))return false;
            // Source/Reset/Learn require ordered semantic receipts. Ordinary
            // CC metadata lives in the existing latest atomic state cells;
            // original audio events themselves remain one-for-one RAW calls.
            if(command==0xb0u&&(input[i+1]==0||input[i+1]==32||input[i+1]>=120))return false;
            i+=width;}
        return true;
    }
    bool nativeFileObserverCompletion(const std::array<unsigned long long,32>& values,unsigned long long captureBefore,
            const MidiCCSemanticState* semanticBefore,unsigned long long failuresBefore){
        // Only this native prepared-file producer has no Python submission
        // callback/registration to satisfy. Ordinary direct RAW retains ALL
        // of its receipts even when on_complete happens to be null.
        if(!ingress||inputClient.load()!=UINT64_MAX||
                !nativeObserverCompletion(values,captureBefore,semanticBefore,failuresBefore,true))return false;
        AcquireSRWLockExclusive(acceptanceLock());bool completed=false;
        if(file&&!file->tickets.empty()){
            auto& ticket=file->tickets.back();
            if(ticket.publication==inputPublication&&!ticket.acked&&ticket.row<(ticket.chase?file->chaseRows.size():file->rows.size())){
                const auto& row=file->sourceRow(ticket);
                // Publication identity, original source port/size/bytes and
                // admission QPC must agree. A different direct actor, a suffix,
                // callback-bearing publication or reused ticket cannot qualify.
                if(row.port==port&&row.size==values[4]&&ticket.received==values[5]&&
                        ((ticket.shiftedKey<0&&std::equal(file->sourceBytes(ticket),file->sourceBytes(ticket)+row.size,input.data()))||
                         (ticket.shiftedKey>=0&&row.size==3&&input[0]==file->sourceBytes(ticket)[0]&&
                          input[1]==ticket.shiftedKey&&input[2]==file->sourceBytes(ticket)[2]))){
                    ticket.acked=true;++file->acked;completed=true;
                }
            }
        }
        ReleaseSRWLockExclusive(acceptanceLock());return completed;
    }
    void publishActions(std::array<unsigned long long,32>& values,unsigned actionCount){
        // A maximal frame may add several ordered clear markers per CC.
        // Never wait for a single record larger than the finite byte ring.
        // Retain existing read-buffer capacity and original frame fields;7 is
        // an intermediate metadata chunk, NOT another audio submission/frame.
        constexpr unsigned chunkCapacity=inputCapacity/3+1;
        const auto disposition=values[1];unsigned offset=0;
        while(actionCount-offset>chunkCapacity){values[1]=7;publish(values,chunkCapacity,nullptr,offset);offset+=chunkCapacity;}
        values[1]=disposition;publish(values,actionCount-offset,nullptr,offset);
    }
    unsigned waitResume(){
        AcquireSRWLockExclusive(&lock);
        while(!resume)SleepConditionVariableSRW(&changed,&lock,INFINITE,0);
        const unsigned result=resume;resume=0;pendingTicket=0;pendingKind=0;metrics[13]=0;delivered=false;ReleaseSRWLockExclusive(&lock);return result;
    }
    std::unique_ptr<Control> takeControl(bool cancel=false){
        AcquireSRWLockExclusive(acceptanceLock());
        std::unique_ptr<Control> result;
        if(controlCount&&(cancel||poppedFence()>=controls[controlHead]->fence)){
            result=std::move(controls[controlHead]);controlHead=(controlHead+1)%static_cast<unsigned>(controls.size());--controlCount;
            if(controlCount)controlFence.store(controls[controlHead]->fence);
            controlsPending.store(controlCount!=0);
        }
        ReleaseSRWLockExclusive(acceptanceLock());return result;
    }
    void runIdleReset(){
        activityClearBaseline=kernel->notes->activityClears;
        // Same GM source-phase cursor as the original timer, not UI Reset.
        // Pressure and retained unlocks resume this cursor, never re-run audio.
        std::array<unsigned long long,32> record{};record[13]=4096;
        unsigned successful=0,phase=0;int failure=0;
        auto error=reinterpret_cast<int(WINAPI*)()>(kernel->errorAddress);
        for(;;){
            const int settlement=settleFailedSelection();
            if(settlement==-6){Sleep(1);continue;}
            int outcome=settlement==-5?-6:midi_cc_kernel_gm_run(kernel,stream,timing);
            if(outcome==-6&&settlement!=-5){const int growth=midiCCSelectionGrowthRetry(*kernel,stream);if(growth==1)continue;if(growth==-2)outcome=-2;}
            if(outcome==1){successful=1;failure=0;break;}
            AcquireSRWLockShared(&kernel->lock);failure=outcome==-8?kernel->gmError:error();ReleaseSRWLockShared(&kernel->lock);
            if(outcome==-6){if(midiCCCaptureGrowthRetry(*kernel)||midiCCFailureGrowthRetry(*kernel))continue;setPaused(true);record[1]=10;publish(record,0);const auto action=waitResume();setPaused(false);
                if(action==3||explicitStop.load()){phase=5;stopping.store(true);break;}continue;}
            if(outcome==-2){
                setPaused(true);record[1]=5;
                do{record[12]=static_cast<unsigned>(failure);publish(record,0);waitResume();int code=0;
                    if(midi_cc_kernel_retry_ex(kernel,&code,explicitStop.load()?1u:0u)==1)break;failure=code;}while(true);
                setPaused(false);
                if(explicitStop.load()){phase=5;stopping.store(true);break;}
                if(midi_cc_kernel_system_reset_pending(kernel,stream)==0){successful=1;failure=0;break;}
                continue;
            }
            phase=2;stopping.store(true);break;
        }
        if(successful)idle.complete();
        record[1]=15;record[9]=successful;record[10]=phase;record[11]=static_cast<unsigned>(failure);record[12]=0;
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);record[31]=static_cast<unsigned long long>(now.QuadPart);publish(record,0);
        if(phase==5)midiCCMarkCancellation(*kernel,stream,4,record[0]);
    }
    void runControl(Control& command,bool cancelled=false){
        activityClearBaseline=kernel->notes->activityClears;
        std::array<unsigned long long,32> record{};
        record[1]=4;record[6]=command.ticket;record[7]=command.fence;record[8]=command.submitted;
        // phase0=completed,1=lock failed,2=event failed,5=explicit stop cancel.
        // Report attempted per-step results and keep full signed error values.
        unsigned attempted=0,successful=0,phase=cancelled?5u:0u;int failure=0,unlockFailure=0;
        unsigned long long controlNoteCount=0;
        auto lockChannel=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(kernel->lockAddress);
        auto emit=reinterpret_cast<BOOL(WINAPI*)(DWORD,DWORD,DWORD,DWORD)>(kernel->emitAddress);
        auto error=reinterpret_cast<int(WINAPI*)()>(kernel->errorAddress);
        auto* clock=kernel->journal->clock;
        if(command.traceFence){
            // Disable only at the accepted-ingress FIFO fence. Disabling when
            // the tail is queued would omit already accepted queued frames.
            traceEnabled.store(false,std::memory_order_release);
            AcquireSRWLockShared(&lock);record[9]=traceProduced;record[11]=traceDropped;ReleaseSRWLockShared(&lock);
            record[1]=14;record[13]=2048;record[10]=cancelled?5u:0u;publish(record,0);return;
        }
        if(command.sourceEdit){
            // Exact accepted-ingress/control FIFO fence. No BASS lock is held
            // during source I/O. New ingress stays in its original queue.
            record[13]=1024;record[10]=cancelled?5u:0u;
            if(cancelled){record[1]=4;publish(record,0);return;}
            setPaused(true);record[1]=11;publish(record,0);waitResume();setPaused(false);
            return;
        }
        if(!command.presetTone.empty()){
            // Stored preset tone policy is not a font/source replacement.
            // The same accepted-ingress FIFO as manual channel controls owns
            // the edit. Pending TIME notes keep their deadlines and sources.
            record[13]=8388608;
            auto channelLock=reinterpret_cast<BOOL(WINAPI*)(DWORD,BOOL)>(command.presetToneLock);
            bool locked=false,policyChanged=false;
            if(!cancelled){
                locked=channelLock(stream,TRUE)!=FALSE;
                if(!locked){phase=1;failure=error();cancelled=true;}
            }
            if(!cancelled){
                AcquireSRWLockExclusive(&kernel->lock);auto* selected=kernel->selection;
                const bool valid=kernel->executionOwner==this&&selected&&selected->stream==stream&&!kernel->gmActive&&!kernel->pendingStream;
                if(!valid){phase=6;failure=-4;}
                else{
                    std::array<bool,128> affected{};unsigned affectedCount=0;
                    for(unsigned ch=0;ch<command.presetToneChannels;++ch){
                        int bank=-1,program=-1;selected->current(ch,bank,program);
                        for(const auto& row:command.presetTone)if(bank==static_cast<int>(row.bank)&&program==static_cast<int>(row.program)){
                            affected[ch]=true;++affectedCount;break;}
                    }
                    const unsigned budget=affectedCount*selected->need();
                    if(!selected->room(budget)){phase=6;failure=-6;}
                    else try{
                        // Allocate on this control worker,never a render or
                        // filter callback. Reserve before changing any policy.
                        selected->presets.reserve(selected->presets.size()+command.presetTone.size());
                        selected->keyRevisions.reserve(selected->keyRevisions.size()+command.presetTone.size());
                        const auto oldKeys=selected->keyRevisions.size();
                        if(selected->provenanceReady)
                            selected->provenanceScratch.reserve(selected->provenanceScratch.size()+3*command.presetTone.size());
                        selected->reserved.fetch_add(budget,std::memory_order_acq_rel);
                        clock->writers.fetch_add(1,std::memory_order_acq_rel);
                        for(const auto& row:command.presetTone){
                            auto found=std::find_if(selected->presets.begin(),selected->presets.end(),[&](const NativeSelectionPreset& p){return p.bank==row.bank&&p.program==row.program;});
                            if(found==selected->presets.end()){
                                auto item=selected->fallback;item.bank=row.bank;item.program=row.program;item.extendedCount=0;
                                selected->presets.push_back(item);found=selected->presets.end()-1;
                            }
                            found->flags=(found->flags&3u)|row.flags;std::copy_n(row.tone,6,found->tone);
                            const auto key=std::make_pair(row.bank,row.program);
                            auto revision=std::lower_bound(selected->keyRevisions.begin(),selected->keyRevisions.end(),key,
                                [](const NativeSelectionKeyRevision& value,const std::pair<unsigned,unsigned>& identity){return std::make_pair(value.bank,value.program)<identity;});
                            if(revision==selected->keyRevisions.end()||revision->bank!=row.bank||revision->program!=row.program)
                                selected->keyRevisions.insert(revision,{row.bank,row.program,1});
                            else ++revision->revision;
                        }
                        if(selected->provenanceReady){
                            const auto added=3*(selected->keyRevisions.size()-oldKeys);
                            selected->provenanceCoreWords+=added;
                            selected->provenanceScratch.resize(selected->provenanceScratch.size()+added);
                        }
                        policyChanged=true;
                        for(unsigned ch=0;ch<command.presetToneChannels;++ch)if(affected[ch]){
                            selected->observeWatches(ch);const bool okay=selected->apply(ch);
                            command.results[attempted++]={ch,MIDI_EVENT_MIXLEVEL,0,okay?1u:0u,okay?0:error()};
                            if(okay)++successful;else{phase=2;failure=error();}
                        }
                        clock->writers.fetch_sub(1,std::memory_order_release);
                        selected->reserved.fetch_sub(budget,std::memory_order_release);
                    }catch(const std::bad_alloc&){phase=6;failure=BASS_ERROR_MEM;}
                }
                ReleaseSRWLockExclusive(&kernel->lock);
            }
            record[23]=policyChanged?1u:0u;
            while(locked&&!channelLock(stream,FALSE)){
                record[12]=static_cast<unsigned>(error());record[1]=5;record[10]=3;
                record[4]=attempted*sizeof(ControlResult);publish(record,0,reinterpret_cast<const unsigned char*>(command.results.data()));waitResume();
            }
            record[1]=4;record[4]=attempted*sizeof(ControlResult);record[9]=successful;record[10]=phase;record[11]=static_cast<unsigned>(failure);
            record[12]=0;LARGE_INTEGER now{};QueryPerformanceCounter(&now);record[31]=now.QuadPart;
            publish(record,0,reinterpret_cast<const unsigned char*>(command.results.data()));return;
        }
        if(!command.instruments.empty()){
            // Original set_channel_instrument, not a RAW/filter selection.
            // Untouched address components resolve here, AFTER prior ingress.
            // resetInstrument preserves each BOOL prefix, UserFX/drum graph,
            // tone/controller blend, profile, restore and touched writers.
            record[13]=2097152;
            for(const auto& step:command.instruments){
                if(cancelled)break;
                int outcome=0;
                for(;;){
                    const int settled=settleFailedSelection();
                    if(settled==-6){Sleep(1);continue;}
                    AcquireSRWLockExclusive(&kernel->lock);
                    auto* selected=kernel->selection;
                    const bool valid=settled!=-5&&kernel->executionOwner==this&&selected&&
                        kernel->journal&&kernel->journal->stream==stream&&selected->graphReady&&
                        !kernel->gmActive&&!kernel->pendingStream;
                    if(!valid){ReleaseSRWLockExclusive(&kernel->lock);phase=6;failure=-4;cancelled=true;break;}
                    const unsigned budget=selected->need();
                    if(!selected->room(budget)){
                        ReleaseSRWLockExclusive(&kernel->lock);
                        // Drain before THIS row; no already applied prefix is
                        // reissued after metadata pressure or decode retries.
                        setPaused(true);record[1]=10;record[4]=0;publish(record,0);
                        const auto action=waitResume();setPaused(false);record[1]=4;
                        if(action==3||explicitStop.load()){phase=5;cancelled=true;stopping.store(true);break;}
                        continue;
                    }
                    selected->reserved.fetch_add(budget,std::memory_order_acq_rel);
                    clock->writers.fetch_add(1,std::memory_order_acq_rel);
                    selected->observeWatches(step.channel);
                    outcome=selected->resetInstrument(step.channel,step.mask,step.bank,step.lsb,step.program);
                    const int code=outcome>0?0:error();
                    command.results[attempted++]={step.channel,MIDI_EVENT_PROGRAM,selected->channels[step.channel].program,outcome>0?1u:0u,code};
                    selected->reserved.fetch_sub(budget,std::memory_order_release);
                    clock->writers.fetch_sub(1,std::memory_order_release);
                    ReleaseSRWLockExclusive(&kernel->lock);
                    if(outcome>0)++successful;else{phase=2;failure=code;}
                    // BOOL refusal skips the remaining address chain, exactly
                    // as Python. A graph/baseline exception stops the batch.
                    if(outcome<0)cancelled=true;
                    break;
                }
            }
            record[1]=4;record[4]=attempted*sizeof(ControlResult);record[9]=successful;record[10]=phase;record[11]=static_cast<unsigned>(failure);
            LARGE_INTEGER now{};QueryPerformanceCounter(&now);record[31]=now.QuadPart;
            publish(record,0,reinterpret_cast<const unsigned char*>(command.results.data()));return;
        }
        if(command.master){
            // Master fine/coarse are global BASSMIDI events (bassmidi.h 83/84).
            // The original setter stores its reset baseline BEFORE StreamEvent,
            // including BOOL failure. Preserve that order on this same ingress
            // FIFO, without source-edit publication or an extra BASS lock.
            record[13]=524288;
            AcquireSRWLockExclusive(&kernel->lock);
            const bool valid=kernel->executionOwner==this&&kernel->journal&&
                kernel->journal->stream==stream&&kernel->gmReady&&!kernel->gmActive&&
                kernel->gmMasters.capacity()>=2&&kernel->gmMasters.size()<=2&&
                std::all_of(kernel->gmMasters.begin(),kernel->gmMasters.end(),[](const MidiCCKernel::GMEvent& item){
                    return item.event==MIDI_EVENT_MASTER_FINETUNE||item.event==MIDI_EVENT_MASTER_COARSETUNE;});
            ReleaseSRWLockExclusive(&kernel->lock);
            if(!cancelled&&!valid){cancelled=true;phase=6;failure=-4;}
            if(!cancelled){
                clock->writers.fetch_add(1,std::memory_order_acq_rel);
                for(unsigned index=0;index<command.steps.size();++index){
                    const auto& step=command.steps[index];
                    AcquireSRWLockExclusive(&kernel->lock);
                    auto found=std::find_if(kernel->gmMasters.begin(),kernel->gmMasters.end(),[&](const MidiCCKernel::GMEvent& item){return item.event==step.event;});
                    if(command.masterPresent[index]){
                        if(found!=kernel->gmMasters.end())found->raw=step.param;
                        else{
                            // reserve(2) was performed during paused preparation;
                            // keep original apply_all order: fine then coarse.
                            auto before=std::find_if(kernel->gmMasters.begin(),kernel->gmMasters.end(),[&](const MidiCCKernel::GMEvent& item){return item.event>step.event;});
                            kernel->gmMasters.insert(before,{step.event,step.param,0});
                        }
                    }else if(found!=kernel->gmMasters.end())kernel->gmMasters.erase(found);
                    ReleaseSRWLockExclusive(&kernel->lock);
                    record[14]|=static_cast<unsigned long long>(command.masterPresent[index])<<index;
                    const bool accepted=emit(stream,0,step.event,step.param)!=FALSE;
                    const int code=accepted?0:error();
                    command.results[attempted++]={0,step.event,step.param,accepted?1u:0u,code};
                    if(accepted)++successful;else{phase=2;failure=code;}
                    // Reset master fine/coarse originally attempts both setters
                    // even when the first event is rejected. Never retry audio.
                }
                clock->writers.fetch_sub(1,std::memory_order_release);
            }
            record[4]=attempted*sizeof(ControlResult);record[9]=successful;record[10]=phase;record[11]=static_cast<unsigned>(failure);
            LARGE_INTEGER now{};QueryPerformanceCounter(&now);record[31]=now.QuadPart;
            publish(record,0,reinterpret_cast<const unsigned char*>(command.results.data()));return;
        }
        if(command.resetPolicyMask){
            // Future Reset/SysEx policy only. These IDs belong to this receipt,
            // not BASSMIDI event numbers; no fake StreamEvent or note is sent.
            record[13]=1048576;record[14]=command.resetPolicyMask;
            if(!cancelled){
                clock->writers.fetch_add(1,std::memory_order_acq_rel);
                AcquireSRWLockExclusive(&kernel->lock);
                const bool valid=kernel->executionOwner==this&&kernel->journal&&
                    kernel->journal->stream==stream&&kernel->gmReady&&!kernel->gmActive;
                if(valid){
                    if(command.resetPolicyMask&1u)kernel->gmDefaultReverb=command.resetPolicyValues[0];
                    if(command.resetPolicyMask&2u)kernel->gmGS=command.resetPolicyValues[1]!=0;
                    if(command.resetPolicyMask&4u)kernel->gmDelay=command.resetPolicyValues[2]!=0;
                    for(unsigned index=3;index<6;++index)if(command.resetPolicyMask&(1u<<index)){
                        const unsigned bit=1u<<(index-3);
                        kernel->gmIgnoreModes=(kernel->gmIgnoreModes&~bit)|(command.resetPolicyValues[index]?0:bit);
                    }
                    for(unsigned index=0;index<6;++index)if(command.resetPolicyMask&(1u<<index)){
                        command.results[attempted++]={UINT_MAX,index+1,command.resetPolicyValues[index],1,0};++successful;
                    }
                }else{phase=6;failure=-4;}
                ReleaseSRWLockExclusive(&kernel->lock);
                clock->writers.fetch_sub(1,std::memory_order_release);
            }
            record[4]=attempted*sizeof(ControlResult);record[9]=successful;record[10]=phase;record[11]=static_cast<unsigned>(failure);
            LARGE_INTEGER now{};QueryPerformanceCounter(&now);record[31]=now.QuadPart;
            publish(record,0,reinterpret_cast<const unsigned char*>(command.results.data()));return;
        }
        if(command.activityKind){
            // Pure state command, ordered by the same accepted-ingress FIFO
            // fence. No BASS call, original Note or timing origin is invented.
            record[13]=8192;record[4]=sizeof(ControlResult);
            auto& notesOwner=*kernel->notes;AcquireSRWLockExclusive(&notesOwner.lock);
            auto* activity=notesOwner.activity;
            if(!cancelled&&activity&&!notesOwner.activityDirty){AcquireSRWLockExclusive(&activity->lock);
                if(activity->attachedOwner==&notesOwner&&!activity->reservationThread&&activity->revision!=NativeMidiActivity::absent){
                    if(command.activityKind==1)for(auto& row:activity->presets)row.played=false;
                    else{activity->available.swap(command.activityAvailable);activity->availableKnown=command.activityAvailableKnown;}
                    activity->commit();successful=1;
                }else phase=6;
                ReleaseSRWLockExclusive(&activity->lock);
            }else if(!cancelled)phase=6;
            ReleaseSRWLockExclusive(&notesOwner.lock);
            command.results[0]={UINT_MAX,command.activityKind,command.activityAvailableKnown?1u:0u,successful,0};
            record[9]=successful;record[10]=phase;
            LARGE_INTEGER now{};QueryPerformanceCounter(&now);record[31]=now.QuadPart;
            publish(record,0,reinterpret_cast<const unsigned char*>(command.results.data()));return;
        }
        if(command.manualReset){
            if(!cancelled&&kernel->notes&&!kernel->notes->humanization.cancel(stream)){phase=2;failure=error();cancelled=true;}
            record[13]=command.systemModeCommand?4194304:command.chaseReset?262144:command.loopStage==2?65536:command.fileResetSource?131072:512;
            if(!cancelled)for(;;){
                const int settlement=settleFailedSelection();
                if(settlement==-6){Sleep(1);continue;}
                if(settlement!=-5&&command.systemModeCommand&&command.systemStage==1){
                    // Copied UI/global MIDI values,not a new DSP topology or
                    // GUI callback. Cursor advances only after each real call.
                    for(;command.systemRestoreNext<command.systemRestore.size();++command.systemRestoreNext){
                        const auto& step=command.systemRestore[command.systemRestoreNext];
                        if(!emit(stream,step.channel,step.event,step.param)){failure=error();phase=2;break;}
                    }
                    if(phase==2)break;command.systemStage=2;
                }
                int outcome=settlement==-5?-6:command.systemModeCommand&&command.systemStage==0?midiCCGMRun(kernel,stream,timing,command.systemMode,true):command.fileResetSource?midi_cc_kernel_file_reset_run(kernel,stream,timing,command.fileResetSource):midi_cc_kernel_manual_reset_run(kernel,stream,timing);
                if(outcome==-6&&settlement!=-5){const int growth=midiCCSelectionGrowthRetry(*kernel,stream);if(growth==1)continue;if(growth==-2)outcome=-2;}
                // Report the final successful operation, not the error of an
                // earlier retained-unlock attempt (BASS errors are per-thread).
                if(outcome==1){
                    if(command.systemModeCommand&&command.systemStage==0){command.systemStage=1;continue;}
                    for(unsigned channel=0;channel<kernel->channels;++channel){midiRouteSemantic(this,channel,MIDI_EVENT_SOUNDOFF);midiRouteSemantic(this,channel,MIDI_EVENT_RESET);}
                    successful=1;failure=0;unlockFailure=0;record[12]=0;break;}
                AcquireSRWLockShared(&kernel->lock);
                failure=outcome==-8?kernel->gmError:error();
                ReleaseSRWLockShared(&kernel->lock);
                if(outcome==-6){
                    if(midiCCCaptureGrowthRetry(*kernel)||midiCCFailureGrowthRetry(*kernel))continue;
                    setPaused(true);record[1]=10;record[4]=0;record[10]=0;publish(record,0);const auto action=waitResume();setPaused(false);
                    if(action==3||explicitStop.load()){phase=5;stopping.store(true);break;}continue;
                }
                if(outcome==-2){
                    setPaused(true);record[1]=5;record[4]=0;
                    do{record[12]=static_cast<unsigned>(failure);publish(record,0);waitResume();int retryError=0;
                        if(midi_cc_kernel_retry_ex(kernel,&retryError,explicitStop.load()?1u:0u)==1)break;failure=retryError;}while(true);
                    setPaused(false);
                    if(explicitStop.load()){phase=5;stopping.store(true);break;}
                    const int pending=command.systemModeCommand&&command.systemStage==0?midi_cc_kernel_system_reset_pending(kernel,stream):command.fileResetSource?midi_cc_kernel_file_reset_pending(kernel,stream):midi_cc_kernel_manual_reset_pending(kernel,stream);
                    if(pending==0){
                        if(command.systemModeCommand&&command.systemStage==0){command.systemStage=1;continue;}
                        successful=1;failure=0;unlockFailure=0;record[12]=0;break;}
                    continue;
                }
                phase=2;break;
            }
            command.results[0]={0,command.systemModeCommand?static_cast<unsigned>(MIDI_EVENT_SYSTEMEX):0u,command.systemMode,successful?1u:0u,failure};record[1]=4;record[4]=sizeof(ControlResult);
            record[9]=successful;record[10]=phase;record[11]=static_cast<unsigned>(failure);
            command.transportComplete=true;command.transportPhase=phase;command.transportError=failure;
            LARGE_INTEGER now{};QueryPerformanceCounter(&now);record[31]=now.QuadPart;
            publish(record,0,reinterpret_cast<const unsigned char*>(command.results.data()));
            if(phase==5&&!cancelled)midiCCMarkCancellation(*kernel,stream,command.systemModeCommand&&command.systemStage==0?2u:command.fileResetSource?5u:3u,record[0]);return;
        }
        if(command.equalizer){
            record[13]=64|256;auto& plan=*command.equalizer;auto& graph=*plan.graph;StreamEQOutcome outcome{};
            if(cancelled)outcome.phase=5;else if(graph.stream!=stream)outcome.phase=6;
            else{clock->writers.fetch_add(1,std::memory_order_acq_rel);outcome=streamEQApply(plan);}
            auto snapshot=[&](){
                const ControlResult header{0,1,outcome.operations,outcome.phase==0?1u:0u,outcome.error};
                StreamEQSnapshot state{};state.identity=graph.state.identity;state.count=graph.count;state.trim=graph.handles[32];
                for(unsigned i=0;i<32;++i)state.rows[i]={graph.handles[i],graph.kinds[i]};
                std::memcpy(command.equalizerResult.data(),&header,sizeof(header));std::memcpy(command.equalizerResult.data()+sizeof(header),&state,sizeof(state));
                record[4]=command.equalizerResult.size();record[9]=outcome.phase==0?1u:0u;record[10]=outcome.phase;
                record[11]=static_cast<unsigned>(outcome.error);record[12]=static_cast<unsigned>(outcome.unlock);
                record[23]=outcome.operation;
            };
            if(outcome.unlockPending){setPaused(true);
                do{snapshot();record[1]=5;publish(record,0,command.equalizerResult.data());waitResume();
                    if(graph.lock(stream,FALSE))break;outcome.unlock=graph.error();}while(true);
                setPaused(false);}
            if(!cancelled&&outcome.phase!=6)clock->writers.fetch_sub(1,std::memory_order_release);
            snapshot();record[1]=4;LARGE_INTEGER now{};QueryPerformanceCounter(&now);record[31]=now.QuadPart;
            publish(record,0,command.equalizerResult.data());return;
        }
        if(command.streamEffect){
            record[13]=64;auto& plan=*command.streamEffect;auto& graph=*plan.graph;StreamVendorOutcome outcome{};
            if(graph.attachDSP)record[13]|=128;
            if(cancelled)outcome.phase=5;
            else if(graph.stream!=stream)outcome.phase=6;
            else{clock->writers.fetch_add(1,std::memory_order_acq_rel);outcome=streamVendorApply(plan);}
            auto snapshot=[&](){
                const ControlResult header{0,graph.kind,outcome.operations,outcome.phase==0?1u:0u,outcome.error};
                const StreamVendorSnapshot state{graph.kind,graph.handle,outcome.committed,outcome.operations};
                std::memcpy(command.streamEffectResult.data(),&header,sizeof(header));
                if(graph.attachDSP){const StreamVendorSnapshot2 extended{state,graph.pointer,graph.sync,0};
                    std::memcpy(command.streamEffectResult.data()+sizeof(header),&extended,sizeof(extended));record[4]=sizeof(header)+sizeof(extended);
                }else{std::memcpy(command.streamEffectResult.data()+sizeof(header),&state,sizeof(state));record[4]=sizeof(header)+sizeof(state);}
                record[9]=outcome.phase==0?1u:0u;record[10]=outcome.phase;record[11]=static_cast<unsigned>(outcome.error);record[12]=static_cast<unsigned>(outcome.unlock);record[23]=outcome.operation;
            };
            if(outcome.unlockPending){
                AcquireSRWLockExclusive(&this->lock);paused=true;ReleaseSRWLockExclusive(&this->lock);
                do{snapshot();record[1]=5;publish(record,0,command.streamEffectResult.data());waitResume();
                    if(graph.lock(stream,FALSE))break;outcome.unlock=graph.error();}while(true);
                AcquireSRWLockExclusive(&this->lock);paused=false;ReleaseSRWLockExclusive(&this->lock);
            }
            if(!cancelled&&outcome.phase!=6)clock->writers.fetch_sub(1,std::memory_order_release);
            snapshot();record[1]=4;LARGE_INTEGER now{};QueryPerformanceCounter(&now);record[31]=now.QuadPart;
            publish(record,0,command.streamEffectResult.data());return;
        }
        if(command.topology){
            record[13]=4;auto& plan=*command.topology;auto& graph=*plan.graph;FXTopologyOutcome outcome{};
            if(graph.version==2)record[13]|=8;
            if(cancelled)outcome.phase=5;
            else if(graph.stream!=stream)outcome.phase=6;
            else{clock->writers.fetch_add(1,std::memory_order_acq_rel);outcome=fxTopologyApply(plan);}
            auto snapshot=[&](){
                const ControlResult header{0,0,graph.count+graph.retiredCount,outcome.phase==0?1u:0u,outcome.error};
                std::memcpy(command.topologyResult.data(),&header,sizeof(header));unsigned offset=sizeof(header);
                auto copy=[&](const FXTopologyValue& value){const FXTopologySnapshot row{value.exact,value.handle,value.kind,value.priority,0};
                    if(graph.version==2){const FXTopologySnapshot2 extended{row,value.pointer,value.kernel};
                        std::memcpy(command.topologyResult.data()+offset,&extended,sizeof(extended));offset+=sizeof(extended);
                    }else{std::memcpy(command.topologyResult.data()+offset,&row,sizeof(row));offset+=sizeof(row);}};
                for(unsigned i=0;i<graph.count;++i)copy(graph.current[i]);for(unsigned i=0;i<graph.retiredCount;++i)copy(graph.retired[i]);
                record[4]=offset;record[9]=outcome.phase==0?1u:0u;record[10]=outcome.phase;record[11]=static_cast<unsigned>(outcome.error);
                record[12]=static_cast<unsigned>(outcome.unlock);record[14]=graph.count;record[15]=graph.retiredCount;record[16]=graph.faulted?1u:0u;
                record[17]=outcome.changed;record[18]=outcome.attached;record[19]=outcome.updated;record[20]=outcome.ordered;record[21]=outcome.removed;record[22]=outcome.rollback;
            };
            if(outcome.unlockPending){
                AcquireSRWLockExclusive(&this->lock);paused=true;ReleaseSRWLockExclusive(&this->lock);
                do{snapshot();record[1]=5;publish(record,0,command.topologyResult.data());waitResume();
                    if(graph.lock(stream,FALSE))break;outcome.unlock=graph.error();}while(true);
                AcquireSRWLockExclusive(&this->lock);paused=false;ReleaseSRWLockExclusive(&this->lock);
            }
            if(!cancelled&&outcome.phase!=6)clock->writers.fetch_sub(1,std::memory_order_release);
            snapshot();record[1]=4;LARGE_INTEGER now{};QueryPerformanceCounter(&now);record[31]=now.QuadPart;
            publish(record,0,command.topologyResult.data());return;
        }
        if(command.batch){
            record[13]=16;
            if(!cancelled){
                clock->writers.fetch_add(1,std::memory_order_acq_rel);
                // Original general FLUSH batch, including a zero-event flush.
                // No additional channel lock or per-event SYNC substitution.
                const unsigned batchCount=static_cast<unsigned>(command.batchEvents.size());
                const auto processed=kernel->rawEmit(stream,BASS_MIDI_EVENTS_FLUSH,command.batchEvents.data(),batchCount);
                const bool accepted=processed!=UINT_MAX&&processed==batchCount;const int code=accepted?0:error();
                if(kernel->rawContext){
                    const bool resets=std::any_of(command.batchEvents.begin(),command.batchEvents.end(),[](const BASS_MIDI_EVENT& row){return row.event==MIDI_EVENT_RESET;});
                    if(resets){
                        if(accepted){for(const auto& row:command.batchEvents)if(row.event==MIDI_EVENT_RESET)
                            raw_decode_context_event(kernel->rawContext,stream,row.event,row.chan,row.param,1u,0);
                        }else raw_decode_context_invalidate(kernel->rawContext,stream,code);
                    }
                }
                command.results[0]={command.batchChannel,0,processed,accepted?1u:0u,code};attempted=1;
                if(accepted)successful=1;else{phase=2;failure=code;}
                clock->writers.fetch_sub(1,std::memory_order_release);
            }
            record[4]=attempted*sizeof(ControlResult);record[9]=successful;record[10]=phase;record[11]=static_cast<unsigned>(failure);
            LARGE_INTEGER now{};QueryPerformanceCounter(&now);record[31]=now.QuadPart;
            publish(record,0,reinterpret_cast<const unsigned char*>(command.results.data()));return;
        }
        if(command.macro){
            record[13]=2;
            if(!cancelled){
                clock->writers.fetch_add(1,std::memory_order_acq_rel);
                const auto& step=command.steps.front();BASS_MIDI_EVENT macro{step.event,step.param,step.channel,0,0};
                // Exact legacy FLUSH/read compound: no extra outer channel
                // lock, RAW/SYNC substitution, normalization or worker wait.
                const auto processed=kernel->rawEmit(stream,BASS_MIDI_EVENTS_FLUSH,&macro,1);
                const bool accepted=processed==1;const int code=accepted?0:error();
                command.results[attempted++]={step.channel,step.event,processed,accepted?1u:0u,code};
                if(accepted){
                    ++successful;auto get=reinterpret_cast<DWORD(WINAPI*)(HSTREAM,DWORD,DWORD)>(command.macroGetter);
                    for(unsigned index=1;index<command.steps.size();++index){
                        const auto& read=command.steps[index];const unsigned value=get(stream,read.channel,read.event);
                        const bool available=value!=UINT_MAX;const int readError=available?0:error();
                        command.results[attempted++]={read.channel,read.event,value,available?1u:0u,readError};
                        if(available)++successful;else{if(phase==0)failure=readError;phase=2;}
                    }
                }else{phase=2;failure=code;}
                clock->writers.fetch_sub(1,std::memory_order_release);
            }
            record[4]=attempted*sizeof(ControlResult);record[9]=successful;record[10]=phase;record[11]=static_cast<unsigned>(failure);
            LARGE_INTEGER now{};QueryPerformanceCounter(&now);record[31]=now.QuadPart;
            publish(record,0,reinterpret_cast<const unsigned char*>(command.results.data()));return;
        }
        const bool manual=command.generation!=nullptr;record[13]=manual?1u:0u;
        if(command.sendFloor){record[13]|=32;record[14]=command.floorEvent;record[15]=command.floorValue;}
        if(manual){
            AcquireSRWLockExclusive(&kernel->lock);
            // A queued request may not silently use a new routing/saved-state
            // generation after handoff. Report unexecuted identity mismatch.
            if(!cancelled&&(kernel->semantics!=command.generation||!kernel->manualJournal)){cancelled=true;phase=6;}
        }
        unsigned releaseChannels=0;
        if(command.transportRelease||command.mixerMask){
            // Source/address handoff refreshes this existing original Reset
            // address contract at the same actor fence. Read execution-time
            // scope, not the scope that existed when Play was pressed.
            AcquireSRWLockShared(&kernel->lock);
            if(kernel->gmReady&&kernel->gmChannels<=kernel->channels){
                releaseChannels=kernel->gmChannels;
                for(unsigned index=0;index<releaseChannels;++index)
                    if(kernel->gmSelection[index]!=index){releaseChannels=0;break;}
            }
            ReleaseSRWLockShared(&kernel->lock);
            if(!releaseChannels){cancelled=true;phase=6;failure=-4;}
        }
        MidiNoteLease activityNotes((manual||command.transportRelease||command.manualPanic)?kernel->notes:nullptr);
        unsigned activityBudget=0;if(manual)for(const auto& step:command.manualSteps)if(step.flags&24u)++activityBudget;
        if(command.transportRelease)activityBudget=releaseChannels;
        if(command.manualPanic)activityBudget=static_cast<unsigned>(std::count_if(command.steps.begin(),command.steps.end(),
            [](const ControlStep& step){return step.event==MIDI_EVENT_SOUNDOFF;}));
        MidiActivityLease activityLease((manual||command.transportRelease||command.manualPanic)&&activityBudget?kernel->notes:nullptr,nullptr,0,0,activityBudget);
        if(!activityLease.ready){cancelled=true;phase=6;failure=-4;}
        if(!cancelled){
            clock->writers.fetch_add(1,std::memory_order_acq_rel);
            if(command.sendFloor){
                // Original setter publishes the floor before apply_all's BASS
                // barrier, even if that barrier fails. Requests resolve below
                // at execution, never from a stale GUI/Python shadow.
                midi_send_domain_floor(kernel->sends,command.floorEvent,command.floorValue);record[16]=1;
            }
            // Legacy silence_channel_notes emits each direct release event
            // without a BASS channel lock. EOF must retain that call contract:
            // adding a new lock admission could skip all release events while
            // falsely reporting completed EOF. Existing Note/activity leases
            // and capture writer protect metadata independently of BASS audio.
            if(!command.transportRelease&&!lockChannel(stream,TRUE)){phase=1;failure=error();}
            else{
                const auto previousMutes=kernel->notes?kernel->notes->mixerMuted:std::array<bool,128>{};
                if(command.mixerMask)kernel->notes->mixerMuted=command.mixerMuted;
                for(const auto& step:command.steps){
                    if(command.mixerMask&&previousMutes[step.channel])continue;
                    if((command.transportRelease||command.mixerMask)&&step.channel>=releaseChannels)continue;
                    unsigned parameter=step.param,request=0;
                    if(command.sendFloor){
                        const unsigned shift=step.event==MIDI_EVENT_REVERB?0:7;
                        request=(kernel->sends->requests[step.channel].load(std::memory_order_acquire)>>shift)&127u;
                        parameter=kernel->sends->apply(step.event,request);
                    }
                    if(kernel->notes&&(step.event==MIDI_EVENT_SOUNDOFF||step.event==MIDI_EVENT_NOTESOFF)){
                        if(!kernel->notes->humanization.cancel(stream,step.channel)){phase=2;failure=error();break;}
                        if(timing)for(unsigned key=0;key<128;++key){auto& q=timing->ingress[step.channel*128+key];q.head.store(q.tail.load());}
                    }
                    const bool accepted=emit(stream,step.channel,step.event,parameter)!=FALSE;
                    if(accepted)midiRouteSemantic(this,step.channel,step.event);
                    const int code=accepted?0:error();
                    midiCCObserveResetResult(*kernel,stream,step.event,step.channel,parameter,accepted,code);
                    if(command.sendFloor)command.floorRows[attempted]={step.channel,step.event,request,parameter,accepted?1u:0u,code};
                    command.results[attempted++]={step.channel,step.event,parameter,accepted?1u:0u,code};
                    if(command.sendFloor&&accepted&&kernel->manualJournal){
                        auto* journal=kernel->manualJournal;
                        for(unsigned i=0;i<journal->eventCount;++i)if(journal->eventTypes[i]==step.event){
                            journal->receive(stream,i,(step.channel<<16)|parameter);break;}
                    }
                    if(accepted)++successful;
                    else{phase=2;failure=code;if(!command.continueOnError)break;}
                    if((command.manualPanic&&step.event==MIDI_EVENT_SOUNDOFF)||
                       (command.transportRelease&&(step.event==MIDI_EVENT_NOTESOFF||(command.loopStage==1&&step.event==MIDI_EVENT_SOUNDOFF)))){
                        // Same per-channel clear even if a direct event fails.
                        // CC123 is metadata only; never send it through remapping.
                        std::fill_n(kernel->notes->counts.data()+step.channel*128,128,0ull);
                        LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);
                        const MidiNoteAction action{0xb0u|(123u<<8),step.channel,0,static_cast<unsigned long long>(tick.QuadPart)};
                        kernel->notes->observeActivity(action,{});notes[controlNoteCount++]=action;
                    }
                }
                if(manual)for(const auto& step:command.manualSteps){
                    auto& state=*command.generation;
                    if(step.flags&24u){
                        MidiCCSemanticRoute route{};route.flags=step.flags;route.event=MIDI_EVENT_MODE;route.destination=UINT_MAX;
                        route.table[step.param].amount=step.amount;
                        MidiCCCompoundContext context{mode,step.channel/16,notes.data(),static_cast<unsigned>(notes.size()),&controlNoteCount};
                        MidiCCModeResult result{};
                        const int applied=midiCCModeApply(*kernel,stream,step.channel,route,step.param,&context,&result,true);
                        const bool accepted=applied==1&&result.accepted!=0;
                        const int code=applied==1?result.error:-1;
                        command.results[attempted++]={step.channel,MIDI_EVENT_MODE,result.selected,accepted?(result.changed?1u:2u):0u,code};
                        if(accepted){
                            ++successful;
                            if(result.changed&&!result.raw){auto* journal=kernel->manualJournal;
                                for(unsigned i=0;i<journal->eventCount;++i)if(journal->eventTypes[i]==MIDI_EVENT_MODE){journal->receive(stream,i,(step.channel<<16)|result.selected);break;}}
                        }else{phase=2;failure=code;if(!command.continueOnError)break;}
                        continue;
                    }
                    if(step.flags&1u)state.write(step.channel,step.source,0,static_cast<unsigned>(step.input-state.fields[step.source].low));
                    if(step.destination==UINT32_MAX){command.results[attempted++]={step.channel,0,0,2,0};++successful;continue;}
                    auto& field=state.fields[step.destination];
            if(step.flags&4u){field.timbrePresent[step.channel]=true;state.write(step.channel,step.destination,2,step.param);}
                    if(step.flags&2u){
                        const int neutral=state.neutral(step.channel,field),saved=field.present[step.channel]?field.saved[step.channel]:neutral;
                        if(step.amount==saved&&step.amount==state.current(step.channel,field,saved)){
                            command.results[attempted++]={step.channel,step.event,step.param,2,0};++successful;continue;
                        }
                        field.present[step.channel]=step.amount!=neutral;field.saved[step.channel]=step.amount;
                        state.write(step.channel,step.destination,1,static_cast<unsigned>(step.amount-field.low)|(field.present[step.channel]?0x8000u:0u));
                    }
                    unsigned raw=step.param;bool accepted=false;
                    if(step.event==MIDI_EVENT_REVERB||step.event==MIDI_EVENT_CHORUS){
                        // Requested send is not saved expression. Resolve the
                        // current floor at execution, not at GUI enqueue time.
                        const unsigned floors=kernel->sends->floors.load(std::memory_order_acquire);
                        const unsigned floor=step.event==MIDI_EVENT_REVERB?floors&127u:(floors>>7)&127u;
                        raw=floor+((127u-floor)*step.param)/127u;
                        accepted=emit(stream,step.channel,step.event,raw)!=FALSE;
                        if(accepted){kernel->sends->request(step.channel,step.event,step.param);
                            state.journal->receive(stream,field.request,(step.channel<<16)|step.param);}
                    }else if(step.event==MIDI_EVENT_USERFX){
                        // Legacy delay writes requested state before publishing
                        // dry/wet retained taps. It never emits USERFX itself.
                        state.journal->receive(stream,field.delayRequest,(step.channel<<16)|step.param);
                        for(auto* tap:kernel->delays[step.channel])if(tap)delayMidiSend(tap,step.param);
                        accepted=true;
                    }else accepted=emit(stream,step.channel,step.event,raw)!=FALSE;
                    if(step.event==MIDI_EVENT_RESET)midiCCObserveResetResult(*kernel,stream,step.event,step.channel,raw,accepted,accepted?0:error());
                    const int code=accepted?0:error();command.results[attempted++]={step.channel,step.event,step.param,accepted?1u:0u,code};
                    if(accepted){
                        // One accepted manual event has one provenance writer.
                        // The scalar semantic success slot records native_midi;
                        // publishing it as well would consume two origin IDs.
                        // MOD metadata replays from the manual journal instead.
                        auto* journal=kernel->manualJournal;
                        for(unsigned i=0;i<journal->eventCount;++i)if(journal->eventTypes[i]==step.event){journal->receive(stream,i,(step.channel<<16)|raw);break;}
                        if(field.flags&2u){field.modRaw[step.channel]=static_cast<int>(raw);field.modPresent[step.channel]=true;field.modSerial[step.channel]=clock->serial.load();}
                        ++successful;
                    }else{
                        state.failures.fetch_add(1);
                        if(step.flags&2u){state.write(step.channel,step.destination,4,static_cast<unsigned>(code)&65535u);state.write(step.channel,step.destination,5,static_cast<unsigned>(code)>>16);}
                        phase=2;failure=code;if(!command.continueOnError)break;
                    }
                }
                if(!command.transportRelease&&!lockChannel(stream,FALSE)){
                    // No return to Python while BASS is locked, including the
                    // retry. GUI only receives a control notification/fault.
                    AcquireSRWLockExclusive(&this->lock);paused=true;ReleaseSRWLockExclusive(&this->lock);
                    record[1]=5;record[4]=attempted*(command.sendFloor?sizeof(MidiSendResult):sizeof(ControlResult));record[9]=successful;record[10]=phase;
                    record[11]=static_cast<unsigned>(failure);
                    // Never park with the input/control mutex held: another
                    // submitter may own a Python registration lease while
                    // native unlock notification/ACK must still be readable.
                    if(manual)ReleaseSRWLockExclusive(&kernel->lock);
                    do{
                        unlockFailure=error();record[12]=static_cast<unsigned>(unlockFailure);
                        publish(record,0,command.sendFloor?reinterpret_cast<const unsigned char*>(command.floorRows.data()):reinterpret_cast<const unsigned char*>(command.results.data()));waitResume();
                    }while(!lockChannel(stream,FALSE));
                    if(manual)AcquireSRWLockExclusive(&kernel->lock);
                    AcquireSRWLockExclusive(&this->lock);paused=false;ReleaseSRWLockExclusive(&this->lock);
                }
            }
            clock->writers.fetch_sub(1,std::memory_order_release);
        }
        if(manual)ReleaseSRWLockExclusive(&kernel->lock);
        record[1]=4;record[4]=attempted*(command.sendFloor?sizeof(MidiSendResult):sizeof(ControlResult));record[9]=successful;record[10]=phase;
        if(command.transportRelease)record[13]|=command.loopStage==1?32768:16384;
        record[11]=static_cast<unsigned>(failure);record[12]=static_cast<unsigned>(unlockFailure);
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);record[31]=now.QuadPart;
        if(command.transportRelease&&!command.loopStage){
            AcquireSRWLockExclusive(acceptanceLock());
            if(file&&file->releaseTicket==command.ticket){
                file->releaseDone=true;
                // Direct-emitter failure still counts as an attempted legacy
                // release (continueOnError + canonical clear). Reservation,
                // address-contract refusal and cancellation do not.
                file->releaseFailed=!releaseChannels||attempted!=releaseChannels*3;
                file->releasePhase=phase;file->releaseError=failure;
                file->complete=!file->releaseFailed;
            }
            ReleaseSRWLockExclusive(acceptanceLock());
        }
        command.transportComplete=true;command.transportPhase=phase;command.transportError=failure;
        publish(record,static_cast<unsigned>(controlNoteCount),command.sendFloor?reinterpret_cast<const unsigned char*>(command.floorRows.data()):reinterpret_cast<const unsigned char*>(command.results.data()));
    }
    void runOwnedControl(std::unique_ptr<Control> command,bool cancelled=false){
        if(!command)return;
        const unsigned loopStage=command->loopStage;
        if(loopStage){AcquireSRWLockExclusive(acceptanceLock());if(file){file->loopActive=loopStage;if(loopStage==2&&file->loopFailed)cancelled=true;}ReleaseSRWLockExclusive(acceptanceLock());}
        runControl(*command,cancelled);
        if(command->chaseReset){
            AcquireSRWLockExclusive(acceptanceLock());
            if(file){auto& plan=*file;plan.chaseResetQueued=false;plan.chaseFailed=!command->transportComplete||command->transportPhase!=0;plan.chaseReady=!plan.chaseFailed;if(plan.chaseFailed){plan.chasing=false;plan.chaseResume=false;}}
            ReleaseSRWLockExclusive(acceptanceLock());return;
        }
        if(!loopStage)return;
        AcquireSRWLockExclusive(acceptanceLock());
        if(file){auto& plan=*file;plan.loopActive=0;if(plan.loopQueued)--plan.loopQueued;
            // Event refusals in the original panic continue and clear activity.
            // A phase1/5/6 or incomplete Reset never permits a new iteration.
            const bool accepted=command->transportComplete&&
                (loopStage==1?command->transportPhase!=1&&command->transportPhase!=5&&command->transportPhase!=6:command->transportPhase==0);
            if(!accepted){plan.loopFailed=true;plan.loopRestartAllowed=false;plan.loopPhase=command->transportPhase;plan.loopError=command->transportError;}
            if(loopStage==1)plan.loopPanic=std::move(command);else plan.loopReset=std::move(command);
            if(loopStage==2&&plan.loopRestartAllowed&&!plan.loopFailed&&!stopping.load()){
                LARGE_INTEGER now{};QueryPerformanceCounter(&now);
                plan.tickets.clear();plan.acked=0;plan.lastComplete=0;plan.next=0;plan.human.reset(0);plan.position=0;plan.anchor=now.QuadPart;
                plan.complete=false;plan.playing=true;++plan.loopIterations;
            }
        }
        ReleaseSRWLockExclusive(acceptanceLock());
    }
    static unsigned __stdcall run(void* pointer){
        auto& s=*static_cast<MidiExecutionWorker*>(pointer);
        s.kernel->executionThread.store(GetCurrentThreadId());
        DWORD index=0;HANDLE task=AvSetMmThreadCharacteristicsW(L"Pro Audio",&index);const DWORD schedulingError=task?0:GetLastError();
        AcquireSRWLockExclusive(&s.lock);s.metrics[6]=task?1:0;s.metrics[7]=schedulingError;s.metrics[8]=GetCurrentThreadId();ReleaseSRWLockExclusive(&s.lock);
        unsigned long long descriptor[13]{};
        for(;;){
            // Reset signals originate under the MIDI kernel lock. Drain their
            // file look-ahead cancellation here,after that lock is released.
            // Never acquire the file acceptance lock inside a MIDI callback.
            const unsigned long long clear[2]{s.kernel->notes->humanization.clearMask[0].exchange(0),s.kernel->notes->humanization.clearMask[1].exchange(0)};
            if(clear[0]||clear[1]){AcquireSRWLockExclusive(s.acceptanceLock());
                if(s.file&&s.file->human.active){auto& f=*s.file;LARGE_INTEGER now{};QueryPerformanceCounter(&now);const auto at=f.at(now.QuadPart);
                    if(clear[0]==~0ull&&clear[1]==~0ull)f.human.cancelPast(f.rows,f.bytes,at,UINT_MAX);
                    else for(unsigned ch=0;ch<128;++ch)if(clear[ch/64]&(1ull<<(ch%64)))f.human.cancelPast(f.rows,f.bytes,at,ch);
                    f.next=f.human.frontier;}
                ReleaseSRWLockExclusive(s.acceptanceLock());}
            s.publishPresentation(); // Outside every BASS/kernel/Note/canonical mutation scope.
            if(s.stopping.load()){
                s.cancelDirectQueued();
                while(auto command=s.takeControl(true))s.runOwnedControl(std::move(command),true);
                break;
            }
            // A successfully accepted ASYNC frame can become ambiguous only
            // when a later PCM update invokes its callback. Service the same
            // original queue-zero settlement even with no new MIDI input;
            // otherwise the last frame's metadata could wait on Python.
            const int idleSettlement=s.settleFailedSelection();
            if(idleSettlement==-6){Sleep(1);continue;}
            if(auto command=s.takeControl()){s.runOwnedControl(std::move(command));continue;}
            const int available=s.pop(descriptor);
            if(available==-9){if(s.claimIdle())s.runIdleReset();continue;}
            if(available==-4)continue; // Prepared command reached its exact ingress publication fence.
            if(available==0&&s.stopping.load())continue; // Explicit cancel results before ownership release.
            if(available<=0){s.publishPresentation(true);s.metric(21,static_cast<unsigned long long>(static_cast<long long>(available)));break;}
            if(!s.fileTransform(static_cast<unsigned>(descriptor[1])))continue;
            bool humanFile=false;
            if(s.ingress&&s.inputClient.load()==UINT64_MAX){AcquireSRWLockShared(s.acceptanceLock());
                humanFile=s.file&&!s.file->tickets.empty()&&s.file->tickets.back().publication==s.inputPublication;
                ReleaseSRWLockShared(s.acceptanceLock());}
            HumanFileScope humanFileScope(humanFile);
            midiRouteOutput(&s,s.port,s.input.data(),static_cast<unsigned>(descriptor[1]));
            // Original _forward_midi_bytes records nonempty input before its
            // bytearray/passive/SysEx/parser/preload work. Preserve that one
            // source entry, not transport QPC or a later delegated suffix.
            s.idle.beginFrame();if(descriptor[1]&&s.stream)s.idle.enter();
            s.activityClearBaseline=s.kernel->notes->activityClears;
            const auto frameCaptureSerial=s.kernel->captureSerial;
            const auto* frameSemantics=s.kernel->semantics;
            const auto frameSemanticFailures=frameSemantics?frameSemantics->failures.load(std::memory_order_acquire):0;
            s.metric(0);s.metric(17,descriptor[1]);
            std::array<unsigned long long,32> record{};record[3]=descriptor[0];record[4]=descriptor[1];record[5]=descriptor[4];std::copy_n(descriptor+5,8,record.data()+6);
            if(descriptor[0]!=3)record[10]=descriptor[3]; // MID2 trace survives without inventing a MID3 origin.
            s.origins->prepare(descriptor,record.data()+14,record[21],record[22]);
            const bool driver=record[19]!=0;
            const bool health=!s.timing||!driver||(descriptor[12]&~7ull)==0x100||s.timing->watch(static_cast<unsigned>(record[16]),record[17]);record[23]=health?1:0;
            if(!driver&&descriptor[0]==3)s.metric(19);
            if(record[21]||record[22])s.metric(20);
            if(s.timing){
                // App-owned explicit CC packets contain no Note On and have
                // no external producer admission to measure. Keep their RAW
                // bytes/provenance unqualified; do not turn a GUI knob into
                // a missing external NOTE origin. External clients, internal
                // notes/SysEx/running-status and file packets retain the strict
                // original fault accounting; no timestamp/source is invented.
                bool nativeFile=false;
                if(s.inputClient.load()==UINT64_MAX){
                    AcquireSRWLockShared(s.acceptanceLock());
                    if(s.file){const auto& tickets=s.file->tickets;
                        const auto found=std::lower_bound(tickets.begin(),tickets.end(),s.inputPublication,
                            [](const auto& ticket,unsigned long long id){return ticket.publication<id;});
                        nativeFile=found!=tickets.end()&&found->publication==s.inputPublication&&!found->acked;
                    }
                    ReleaseSRWLockShared(s.acceptanceLock());
                }
                const bool internalCC=s.inputClient.load()==UINT64_MAX&&!nativeFile&&descriptor[0]==2&&
                    descriptor[1]==3&&(s.input[0]&0xf0u)==0xb0u&&s.input[1]<120&&s.input[2]<128;
                if(descriptor[0]!=3&&!internalCC)s.timing->missingOrigin.fetch_add(1);
                else if(descriptor[0]==3&&!driver)s.timing->invalidOrigin.fetch_add(1);
                if(record[21]||record[22])s.timing->transportLoss.fetch_add(1);
            }
            unsigned consumed=0,totalNotes=0;
            // Optional observation is independent of mandatory record/byte
            // pressure. Original ingress preview, never rewritten output.
            const bool traceFrame=s.traceEnabled.load(std::memory_order_acquire);
            // Original bytes and driver/read QPC, once before any transform.
            // Optional recording owns its disk thread; GUI/GIL and audio never
            // participate. Existing input dispatch remains authoritative.
            if(s.recordingAttached.load(std::memory_order_acquire)&&(s.inputClient.load()!=UINT64_MAX||(descriptor[12]&~7ull)==0x100)){
                AcquireSRWLockShared(&s.lock);
                if(s.recording){
                    if(record[21]||record[22])s.recording->transportLoss(record[21]+record[22]);
                    s.recording->append(0,record[19]?record[14]:record[5],s.port,s.input.data(),static_cast<unsigned>(descriptor[1]));
                }
                ReleaseSRWLockShared(&s.lock);
            }
            const int systemKind=midi_cc_kernel_system_reset_kind(s.kernel,s.stream,s.input.data(),static_cast<unsigned>(descriptor[1]));
            if((systemKind==1||systemKind==2||(systemKind==0&&descriptor[1]==9&&s.input[0]==0xf0&&s.input[1]==0x43&&s.input[3]==0x4c&&s.input[6]==0x7e))&&s.kernel->notes&&!s.kernel->notes->humanization.cancel(s.stream)){
                s.stopping.store(true);break;
            }
            LARGE_INTEGER traceBegin{},traceEnd{};if(traceFrame)QueryPerformanceCounter(&traceBegin);
            for(;;){
                unsigned long long noteCount=0,result[8]{};
                const int pendingTail=midi_cc_kernel_reset_tail_pending(s.kernel,s.stream);
                const bool ownedFrame=s.kernel->frameReceipt!=0;
                const bool semanticOnly=!ownedFrame&&midi_cc_kernel_semantic_pending(s.kernel,s.stream)==1;
                const bool tailOnly=!ownedFrame&&consumed==descriptor[1]&&pendingTail==1;
                const bool systemReset=systemKind==1||systemKind==2;
                const int settlement=s.settleFailedSelection();
                if(settlement==-6){Sleep(1);continue;}
                const unsigned resetMode=systemKind==2?MIDI_SYSTEM_GS:
                    systemKind==1?(s.input[4]==3?MIDI_SYSTEM_GM2:MIDI_SYSTEM_GM1):0;
                int outcome=settlement==-5?-6:s.kernel->compatibilityCallback?0:semanticOnly?midi_cc_kernel_semantic_resume(s.kernel,s.stream):systemKind==3?1:systemReset?midiCCGMRun(s.kernel,s.stream,s.timing,resetMode):
                    tailOnly?midi_cc_kernel_reset_tail(s.kernel,s.stream):
                    midiCCOwnedRun(s.kernel,s.stream,s.mode,s.port,s.input.data(),static_cast<unsigned>(descriptor[1]),
                    s.output.data(),static_cast<unsigned>(s.output.size()),s.actions.data(),static_cast<unsigned>(s.actions.size()),result,
                    s.notes.data()+totalNotes,static_cast<unsigned>(s.notes.size())-totalNotes,&noteCount,s.timing,record.data()+14,
                    ownedFrame?s.kernel->frameReceipt:0,ownedFrame?consumed:0);
                if(systemKind>0&&!semanticOnly&&consumed==0)result[5]=descriptor[1];
                if(semanticOnly){const int captured=midi_cc_kernel_take_actions(s.kernel,s.notes.data()+totalNotes,static_cast<unsigned>(s.notes.size())-totalNotes);
                    if(captured<0){s.stopping.store(true);break;}noteCount=static_cast<unsigned>(captured);}
                if((tailOnly||systemReset)&&outcome<0){
                    AcquireSRWLockShared(&s.kernel->lock);
                    result[4]=static_cast<unsigned>(outcome==-8?(systemReset?s.kernel->gmError:s.kernel->resetTailError):
                        reinterpret_cast<int(WINAPI*)()>(s.kernel->errorAddress)());
                    ReleaseSRWLockShared(&s.kernel->lock);
                }
                totalNotes+=static_cast<unsigned>(noteCount);consumed+=static_cast<unsigned>(result[5]);
                record[2]=static_cast<unsigned long long>(static_cast<long long>(outcome));record[24]=totalNotes;record[25]+=result[2];record[26]=result[4];
                record[27]=consumed;record[28]+=result[3];record[29]+=result[6];record[30]+=result[7];
                LARGE_INTEGER now{};QueryPerformanceCounter(&now);record[31]=now.QuadPart;
                if(outcome==-6){
                    // ASYNC callbacks can make identical outstanding ticket
                    // identities ambiguous AFTER the top-of-loop settlement
                    // check but BEFORE this frame's ticket admission. That
                    // is native queue settlement, not Python metadata pressure.
                    // Re-enter the same actor/three-queue original barrier;
                    // never guess an oldest ticket or CANCEL queued audio.
                    if(s.explicitStop.load()&&s.kernel->semanticFramePending){
                        record[1]=9;record[2]=static_cast<unsigned long long>(static_cast<long long>(-9));
                        s.publishActions(record,totalNotes);midiCCMarkCancellation(*s.kernel,s.stream,systemReset?2u:1u,record[0]);s.stopping.store(true);break;
                    }
                    bool settleAgain=false;
                    {AcquireSRWLockShared(&s.kernel->lock);
                        settleAgain=s.kernel->selection&&!s.kernel->selectionSettlementControlOnly&&s.kernel->selection->failedTicket();
                        ReleaseSRWLockShared(&s.kernel->lock);}
                    if(settleAgain){const int settled=s.settleFailedSelection();
                        if(settled==1)continue;if(settled==-6){Sleep(1);continue;}}
                    const int growth=settlement==-5?0:midiCCSelectionGrowthRetry(*s.kernel,s.stream);
                    if(growth==1)continue;
                    if(growth==-2){s.setPaused(true);record[1]=3;s.publish(record,0);
                        do{s.waitResume();int code=0;if(midi_cc_kernel_retry_ex(s.kernel,&code,s.explicitStop.load()?1u:0u)==1)break;record[26]=static_cast<unsigned>(code);s.publish(record,0);}while(true);
                        s.setPaused(false);if(!s.explicitStop.load())continue;
                        record[1]=9;record[2]=static_cast<unsigned long long>(static_cast<long long>(-9));s.publishActions(record,totalNotes);midiCCMarkCancellation(*s.kernel,s.stream,systemReset?2u:1u,record[0]);s.stopping.store(true);break;}
                    if(midiCCCaptureGrowthRetry(*s.kernel)||midiCCFailureGrowthRetry(*s.kernel))continue;
                    // Semantic action FIFO pressure: kernel has released its
                    // writer/lock leases. Publish the processed prefix once,
                    // drain metadata, then continue the SAME original suffix.
                    s.setPaused(true);record[1]=6;s.publishActions(record,totalNotes);s.metric(4,s.musicalActions(totalNotes));totalNotes=0;
                    const auto action=s.waitResume();s.setPaused(false);
                    if(action==3||s.explicitStop.load()){
                        record[1]=9;record[2]=static_cast<unsigned long long>(static_cast<long long>(-9));
                        s.publishActions(record,0);midiCCMarkCancellation(*s.kernel,s.stream,systemReset?2u:1u,record[0]);
                        s.metric(2);s.stopping.store(true);break;}
                    continue;
                }
                if(outcome==-7){
                    if(s.kernel->semanticFramePending){
                        // A preserved Reset prefix belongs to this receipt.
                        // Do not let a generic RAW handoff lose its owner.
                        record[1]=9;s.publishActions(record,totalNotes);s.metric(2);s.stopping.store(true);break;
                    }
                    // First-use cancellation/generation fault left this Note
                    // untouched; earlier RAW audio already completed. Commit
                    // its original metadata before offering only the suffix.
                    s.setPaused(true);record[1]=8;s.publishActions(record,totalNotes);s.metric(4,s.musicalActions(totalNotes));totalNotes=0;
                    const auto action=s.waitResume();
                    if(action==3||s.stopping.load()){s.setPaused(false);s.stopping.store(true);break;}
                    // Prefix actions were delivered/ACKed before this resume.
                    // The existing legacy handoff owns an immutable copy of
                    // this whole original frame plus its consumed byte offset.
                    // Transfer ONLY the native input receipt; selection/ASYNC
                    // tickets and source borrowers retain their own lifetime.
                    {AcquireSRWLockExclusive(&s.kernel->lock);s.kernel->clearFrameReceipt();ReleaseSRWLockExclusive(&s.kernel->lock);}
                    {MidiNoteLease noteLease(s.kernel->notes);s.kernel->notes->dirtyActivity();}
                    record[1]=2;s.publish(record,0);const auto ownership=s.waitResume();s.setPaused(false);
                    if(ownership==1)s.metric(3);if(ownership==3)s.stopping.store(true);break;
                }
                if(outcome==0||outcome==-4){
                    if(semanticOnly||midi_cc_kernel_semantic_pending(s.kernel,s.stream)>0){
                        // A semantic owner has already played a prefix. It is
                        // NOT a pristine preflight refusal or a whole-RAW handoff.
                        record[1]=9;s.publishActions(record,totalNotes);s.metric(2);s.stopping.store(true);break;
                    }
                    {MidiNoteLease noteLease(s.kernel->notes);s.kernel->notes->dirtyActivity();}
                    s.setPaused(true);record[1]=2;s.publish(record,0);const auto action=s.waitResume();
                    if(action==1)s.metric(3);s.setPaused(false);
                    if(action==3)s.stopping.store(true);break; // Handoff ownership explicitly accepted by caller.
                }
                if(outcome==-2){
                    s.setPaused(true);record[1]=3;s.publish(record,0);
                    // Unlock may ONLY be retried on this original BASS owner.
                    // Never exit/free a locked stream on a stop request.
                    for(;;){
                        s.waitResume();int error=0;const int released=midi_cc_kernel_retry_ex(s.kernel,&error,s.explicitStop.load()?1u:0u);
                        if(released==1){const int captured=midi_cc_kernel_take_actions(s.kernel,s.notes.data()+totalNotes,static_cast<unsigned>(s.notes.size())-totalNotes);
                            if(captured<0){s.stopping.store(true);break;}totalNotes+=static_cast<unsigned>(captured);break;}
                        record[26]=static_cast<unsigned>(error);s.publish(record,0);
                    }
                    s.setPaused(false);
                    if(s.explicitStop.load()){
                        record[1]=9;record[2]=static_cast<unsigned long long>(static_cast<long long>(-9));
                        s.publishActions(record,totalNotes);midiCCMarkCancellation(*s.kernel,s.stream,systemReset?2u:1u,record[0]);
                        s.metric(2);s.stopping.store(true);break;
                    }
                    if(s.kernel->frameReceipt||consumed<descriptor[1]||midi_cc_kernel_semantic_pending(s.kernel,s.stream)==1||midi_cc_kernel_reset_tail_pending(s.kernel,s.stream)==1||
                       (systemReset&&midi_cc_kernel_system_reset_pending(s.kernel,s.stream)==1))continue;
                    record[2]=1;
                }else if(outcome==-8&&(midi_cc_kernel_reset_tail_pending(s.kernel,s.stream)==1||
                        (systemReset&&midi_cc_kernel_system_reset_pending(s.kernel,s.stream)==1))){
                    // A reset tail already owns its successfully played prefix.
                    // Fatal is terminal retained evidence, never an empty RAW
                    // legacy handoff which could silently certify completion.
                    record[1]=9;s.publishActions(record,totalNotes);s.metric(2);s.stopping.store(true);break;
                }else if(outcome!=1&&outcome!=2){
                    if(semanticOnly||midi_cc_kernel_semantic_pending(s.kernel,s.stream)>0){
                        record[1]=9;s.publishActions(record,totalNotes);s.metric(2);s.stopping.store(true);break;
                    }
                    // Explicit protocol/config fault, not a false processed frame.
                    {MidiNoteLease noteLease(s.kernel->notes);s.kernel->notes->dirtyActivity();}
                    s.setPaused(true);record[1]=2;s.publish(record,0);const auto action=s.waitResume();s.setPaused(false);
                    if(action==3)s.stopping.store(true);break;
                }
                if(s.kernel->frameReceipt)continue; // Last-byte receipt still needs terminal completion.
                if(semanticOnly&&(consumed<descriptor[1]||midi_cc_kernel_reset_tail_pending(s.kernel,s.stream)==1||
                    (systemReset&&midi_cc_kernel_system_reset_pending(s.kernel,s.stream)==1)))continue;
                if(traceFrame)QueryPerformanceCounter(&traceEnd);
                record[1]=1;
                const bool observerOnly=s.nativeObserverCompletion(record,frameCaptureSerial,frameSemantics,frameSemanticFailures)||
                     s.nativeFileObserverCompletion(record,frameCaptureSerial,frameSemantics,frameSemanticFailures);
                if(!observerOnly)s.publishActions(record,totalNotes);
                s.metric(1);s.metric(4,s.musicalActions(totalNotes));s.metric(18,record[29]);
                if(outcome==2)s.metric(2);break;
            }
            // The final failed RAW has no later input to wake settlement.
            // Its immutable completion/error was published above; retire only
            // confirmed queue-zero credits natively before waiting for input.
            // Python metadata may observe pending False, but never steals the
            // active actor's lock. A real query fault retains the old control
            // error path and exact ticket rather than fabricating queue zero.
            for(;;){const int settlement=s.settleFailedSelection();
                if(settlement!=-6||s.stopping.load())break;Sleep(1);}
            if(traceFrame){
                // Both timestamps are captured before copying trace records;
                // delayed Python logging never becomes a fake audio time.
                if(!traceEnd.QuadPart)QueryPerformanceCounter(&traceEnd);
                s.publishTrace(record,traceBegin.QuadPart,traceEnd.QuadPart,static_cast<unsigned>(descriptor[1]));}
            s.inputClient.store(0);
            if(s.ingress){AcquireSRWLockExclusive(s.acceptanceLock());
                if(s.file&&!s.file->tickets.empty()&&s.file->tickets.back().publication==s.inputPublication)
                    s.file->lastComplete=s.inputPublication;
                ReleaseSRWLockExclusive(s.acceptanceLock());}
            s.idle.endFrame();
        }
        if(task){const bool reverted=AvRevertMmThreadCharacteristics(task)!=FALSE;const unsigned error=reverted?0:GetLastError();
            AcquireSRWLockExclusive(&s.lock);s.metrics[9]=reverted?1:0;s.metrics[10]=error;ReleaseSRWLockExclusive(&s.lock);}
        AcquireSRWLockExclusive(s.acceptanceLock());s.acceptingControls.store(false);
        const bool cancelled=s.stopping.load()||(!s.ingress&&s.queue->stopping);ReleaseSRWLockExclusive(s.acceptanceLock());
        if(cancelled)s.cancelDirectQueued();
        // EOF admission closes before final drain, but read must not observe
        // ended until every accepted control completion has been published.
        while(auto command=s.takeControl(true))s.runOwnedControl(std::move(command),cancelled);
        AcquireSRWLockExclusive(&s.lock);s.ended.store(true);s.metrics[11]=1;WakeAllConditionVariable(&s.changed);ReleaseSRWLockExclusive(&s.lock);
        return 0;
    }
    void stop(){
        // An accepted direct frame has a caller-owned completion, unlike a
        // pipe whose producer can retain its original queue. Close admission
        // but let already accepted frames/metadata drain before stopping.
        if(directPending()){
            AcquireSRWLockExclusive(acceptanceLock());if(ingress)ingress->accepting=false;
            acceptingControls.store(false);wakeIngress();ReleaseSRWLockExclusive(acceptanceLock());return;
        }
        explicitStop.store(true,std::memory_order_release);
        stopping.store(true);
        AcquireSRWLockExclusive(acceptanceLock());if(ingress)ingress->accepting=false;wakeIngress();ReleaseSRWLockExclusive(acceptanceLock());
        if(!started.exchange(true))ResumeThread(thread);
        AcquireSRWLockExclusive(&lock);metrics[12]=1;WakeAllConditionVariable(&changed);ReleaseSRWLockExclusive(&lock);
    }
};
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_idle_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_execution_idle_configure(void* pointer,unsigned seconds){
    if(!pointer||seconds>600)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    AcquireSRWLockShared(&s.kernel->lock);const bool ready=!seconds||s.kernel->gmReady;ReleaseSRWLockShared(&s.kernel->lock);
    if(!ready||s.stopping.load())return 0;s.idle.configure(seconds);s.wakeIngress();return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_execution_idle_enter(void* pointer){
    if(!pointer)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);AcquireSRWLockShared(&s.idle.lock);
    const bool active=s.idle.frame;ReleaseSRWLockShared(&s.idle.lock);if(!active)return 0;return s.idle.enter()?1:2;
}
// Read-only same predicate for diagnostics/boundary audits; claiming always
// obtains real QPC inside the worker and cannot use a caller-provided clock.
extern "C" __declspec(dllexport) int __cdecl midi_execution_idle_due(void* pointer,unsigned long long tick){
    if(!pointer)return 0;auto& s=static_cast<MidiExecutionWorker*>(pointer)->idle;
    AcquireSRWLockShared(&s.lock);const bool result=s.due(tick);ReleaseSRWLockShared(&s.lock);return result?1:0;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_idle_snapshot(void* pointer,unsigned long long* values,unsigned capacity){
    if(!pointer||!values||capacity<10)return 0;auto& s=static_cast<MidiExecutionWorker*>(pointer)->idle;AcquireSRWLockShared(&s.lock);
    const unsigned long long data[]{s.frequency,s.epoch,s.delay,s.last,s.fired?1ull:0ull,s.claims,s.completed,s.claimedEpoch,s.frame?1ull:0ull,s.frameEntered?1ull:0ull};
    std::copy_n(data,10,values);ReleaseSRWLockShared(&s.lock);return 10;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_actions_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_control_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_manual_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_manual_send_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_manual_mode_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_trace_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_execution_recording_bind(void* pointer,void* sink){
    if(!pointer)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);auto* next=static_cast<MidiRecordingSink*>(sink);
    if(next)next->retain();AcquireSRWLockExclusive(&s.lock);auto* old=s.recording;s.recording=next;s.recordingAttached.store(next!=nullptr,std::memory_order_release);ReleaseSRWLockExclusive(&s.lock);
    if(old)old->release();return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_retained_records_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_execution_retained_records_prepare(void* pointer){
    if(!pointer)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.started.load()||s.retentionThread||s.count||s.retainedCount||s.stopping.load()){ReleaseSRWLockExclusive(&s.lock);return 0;}
    s.retentionThread=reinterpret_cast<HANDLE>(_beginthreadex(nullptr,0,&MidiExecutionWorker::retainRecords,&s,0,nullptr));
    if(s.retentionThread)s.retentionEnabled=true;const bool accepted=s.retentionEnabled;ReleaseSRWLockExclusive(&s.lock);return accepted?1:0;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_retained_records_snapshot(void* pointer,unsigned long long* values,unsigned capacity){
    if(!pointer||!values||capacity<6)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);AcquireSRWLockShared(&s.lock);
    values[0]=s.retainedCount;values[1]=s.retainedBytes;values[2]=s.retainedTransferred;values[3]=s.retainedAllocationFailures;
    values[4]=s.retentionEnabled?1:0;values[5]=s.retentionEnded?1:0;ReleaseSRWLockShared(&s.lock);return 6;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_trace_observation_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_trace_observation_snapshot(void* pointer,unsigned long long* values,unsigned capacity){
    if(!pointer||!values||capacity<4)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);AcquireSRWLockShared(&s.lock);
    values[0]=s.traceProduced;values[1]=s.traceDropped;values[2]=s.traceRead;values[3]=s.traceCount;
    ReleaseSRWLockShared(&s.lock);return 4;
}
extern "C" __declspec(dllexport) int __cdecl midi_execution_trace_enable(void* pointer,unsigned enabled){
    if(!pointer||enabled>1)return 0;static_cast<MidiExecutionWorker*>(pointer)->traceEnabled.store(enabled!=0,std::memory_order_release);return 1;
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_trace_finish(void* pointer){
    if(!pointer)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    try{const MidiExecutionWorker::ControlStep dummy{};auto command=std::make_unique<MidiExecutionWorker::Control>(&dummy,1);
        command->steps.clear();command->results.clear();command->traceFence=true;
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;
        const auto ticket=command->ticket;if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_manual(void* pointer,
        const MidiExecutionWorker::ManualStep* steps,unsigned count,unsigned continueOnError){
    if(!pointer||!steps||!count||count>1024||continueOnError>1)return 0;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);AcquireSRWLockExclusive(&s.kernel->lock);
    auto* state=s.kernel->semantics;
    if(!state||!s.kernel->manualJournal){ReleaseSRWLockExclusive(&s.kernel->lock);return 0;}
    for(unsigned i=0;i<count;++i){const auto& step=steps[i];
        if(step.flags&24u){
            if(step.channel>=s.kernel->channels||step.flags>24||(step.flags&7u)||step.source||step.destination!=UINT_MAX||
               step.input<0||step.input>1||step.amount!=step.input||step.param!=static_cast<unsigned>(step.input)||
               step.event!=MIDI_EVENT_MODE||!state->modeReady||!s.kernel->notes){ReleaseSRWLockExclusive(&s.kernel->lock);return 0;}
            continue;
        }
        if(step.channel>=s.kernel->channels||step.source>=state->fields.size()||step.flags>7||
           step.input<state->fields[step.source].low||step.input>state->fields[step.source].high){ReleaseSRWLockExclusive(&s.kernel->lock);return 0;}
        if(step.destination==UINT32_MAX){if(step.flags!=1||step.event||step.param||step.amount){ReleaseSRWLockExclusive(&s.kernel->lock);return 0;}continue;}
        if(step.destination>=state->fields.size()){ReleaseSRWLockExclusive(&s.kernel->lock);return 0;}
        const auto& field=state->fields[step.destination];
        // Stateful sends use their typed request/tap paths above, never the
        // raw scalar emitter. MODE's compound contract remains separate.
        if(field.event!=step.event||step.event==MIDI_EVENT_MODE||step.param>65535||
           ((step.event==MIDI_EVENT_REVERB||step.event==MIDI_EVENT_CHORUS)&&(field.request==256||step.param>127))||
           (step.event==MIDI_EVENT_USERFX&&(field.delayRequest==256||step.param>127||!s.kernel->delayEnabled[step.channel]))||
           step.amount<field.low||step.amount>field.high||((step.flags&2u)!=0)!=((field.flags&1u)!=0)||
           ((step.flags&4u)!=0)!=((field.flags&8u)!=0)) {ReleaseSRWLockExclusive(&s.kernel->lock);return 0;}
    }
    try{
        auto command=std::make_unique<MidiExecutionWorker::Control>(steps,count,state);
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());ReleaseSRWLockExclusive(&s.kernel->lock);return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();command->continueOnError=continueOnError!=0;
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;const auto ticket=command->ticket;
        if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());ReleaseSRWLockExclusive(&s.kernel->lock);return ticket;
    }catch(const std::bad_alloc&){ReleaseSRWLockExclusive(&s.kernel->lock);return 0;}
}
static bool executionControlEvent(unsigned event){
    // These change input/source/counter/reset identity: their ordered native
    // semantics and table publication are separate gates, not guessed here.
    return event!=MIDI_EVENT_END&&event!=MIDI_EVENT_END_TRACK&&event!=MIDI_EVENT_NOTE&&
        event!=MIDI_EVENT_PROGRAM&&event!=MIDI_EVENT_BANK&&event!=MIDI_EVENT_BANK_LSB&&event!=MIDI_EVENT_DRUMS&&
        event!=MIDI_EVENT_DEFDRUMS&&event!=MIDI_EVENT_RESET&&event!=MIDI_EVENT_NOTESOFF&&event!=MIDI_EVENT_SOUNDOFF&&
        event!=MIDI_EVENT_SYSTEM&&event!=MIDI_EVENT_SYSTEMEX&&event<MIDI_EVENT_NOTES;
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_control(void* pointer,
        const MidiExecutionWorker::ControlStep* steps,unsigned count,unsigned continueOnError){
    if(!pointer||!steps||!count||count>1024||continueOnError>1)return 0;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    for(unsigned i=0;i<count;++i)if(steps[i].channel>=s.kernel->channels||!executionControlEvent(steps[i].event))return 0;
    try{
        // All control allocation/copies occur on the submitting control thread,
        // before queue lock. MIDI worker receives immutable scalar storage.
        auto command=std::make_unique<MidiExecutionWorker::Control>(steps,count);
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();command->continueOnError=continueOnError!=0;
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;
        const auto ticket=command->ticket;
        if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}
// Explicit Panic port: keep note/reset events out of the generic scalar API.
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_panic(void* pointer){
    if(!pointer)return 0;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    try{
        std::vector<MidiExecutionWorker::ControlStep> steps;steps.reserve(s.kernel->channels*3);
        for(unsigned ch=0;ch<s.kernel->channels;++ch)
            for(unsigned event:{MIDI_EVENT_SUSTAIN,MIDI_EVENT_SOSTENUTO,MIDI_EVENT_SOUNDOFF})steps.push_back({ch,event,0});
        auto command=std::make_unique<MidiExecutionWorker::Control>(steps.data(),static_cast<unsigned>(steps.size()));
        command->manualPanic=true;command->continueOnError=true;
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;const auto ticket=command->ticket;
        if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}
// Runtime mixer controls are not file masks and do not change CC/gain state.
// A null mask means channel-only Panic; a128-byte mask is the full live mask.
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_mixer_version(){return 1;}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_mixer(void* pointer,
        const unsigned char* mask,unsigned channel){
    if(!pointer)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    if(!s.kernel->notes||(!mask&&channel>=s.kernel->channels))return 0;
    if(mask)for(unsigned i=0;i<128;++i)if(mask[i]>1)return 0;
    try{
        auto command=std::make_unique<MidiExecutionWorker::Control>();
        command->manualPanic=true;command->continueOnError=true;command->mixerMask=mask!=nullptr;
        command->steps.reserve(s.kernel->channels*3);
        for(unsigned ch=0;ch<s.kernel->channels;++ch){
            if(mask)command->mixerMuted[ch]=mask[ch]!=0;
            if(mask?mask[ch]!=0:ch==channel)
                for(unsigned event:{MIDI_EVENT_SUSTAIN,MIDI_EVENT_SOSTENUTO,MIDI_EVENT_SOUNDOFF})command->steps.push_back({ch,event,0});
        }
        command->results.resize(command->steps.size());
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;const auto ticket=command->ticket;
        if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_instrument_version(){return 1;}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_instruments(void* pointer,
        const MidiExecutionWorker::InstrumentStep* steps,unsigned count){
    if(!pointer||!steps||!count||count>128)return 0;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    for(unsigned index=0;index<count;++index){const auto& row=steps[index];
        if(row.channel>=s.kernel->channels||!row.mask||row.mask>7||row.bank>128||row.lsb>127||row.program>127)return 0;
        for(unsigned previous=0;previous<index;++previous)if(steps[previous].channel==row.channel)return 0;
    }
    AcquireSRWLockShared(&s.kernel->lock);
    const bool ready=s.kernel->selection&&s.kernel->selection->graphReady&&s.kernel->journal&&
        s.kernel->journal->stream==s.stream&&s.kernel->executionOwner==&s;
    ReleaseSRWLockShared(&s.kernel->lock);if(!ready)return 0;
    try{
        auto command=std::make_unique<MidiExecutionWorker::Control>();
        command->instruments.assign(steps,steps+count);command->results.resize(count);
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;const auto ticket=command->ticket;
        if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_master_version(){return 1;}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_master(void* pointer,
        const MidiExecutionWorker::ControlStep* steps,const unsigned* present,unsigned count){
    if(!pointer||!steps||!present||!count||count>2)return 0;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    for(unsigned index=0;index<count;++index){
        const auto& step=steps[index];
        if(step.channel||present[index]>1)return 0;
        if(step.event==MIDI_EVENT_MASTER_FINETUNE){if(step.param>16383||(!present[index]&&step.param!=8192))return 0;}
        else if(step.event==MIDI_EVENT_MASTER_COARSETUNE){if(step.param<40||step.param>88||(!present[index]&&step.param!=64))return 0;}
        else return 0;
        for(unsigned previous=0;previous<index;++previous)if(steps[previous].event==step.event)return 0;
    }
    try{
        auto command=std::make_unique<MidiExecutionWorker::Control>(steps,count);command->master=true;
        std::copy_n(present,count,command->masterPresent.begin());
        // Allocation is complete before admission; the exact worker/kernel and
        // stream lease are retained by the existing immutable Control owner.
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;const auto ticket=command->ticket;
        if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_reset_policy_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_reset_policy2_version(){return 1;}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_reset_policy2(void* pointer,
        unsigned mask,unsigned reverb,unsigned gsReset,unsigned delaySysEx,unsigned modeReceive){
    if(!pointer||!mask||(mask&~63u)||reverb>127||gsReset>1||delaySysEx>1||modeReceive>7)return 0;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    try{
        auto command=std::make_unique<MidiExecutionWorker::Control>();command->resetPolicyMask=mask;
        command->resetPolicyValues={reverb,gsReset,delaySysEx,modeReceive&1u,(modeReceive>>1)&1u,(modeReceive>>2)&1u};
        unsigned count=0;for(unsigned index=0;index<6;++index)count+=static_cast<unsigned>((mask&(1u<<index))!=0);
        command->results.resize(count);
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;const auto ticket=command->ticket;
        if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_reset_policy(void* pointer,
        unsigned mask,unsigned reverb,unsigned gsReset,unsigned delaySysEx){
    if(mask&~7u)return 0;
    return midi_execution_reset_policy2(pointer,mask,reverb,gsReset,delaySysEx,7);
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_macro_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_reset_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_source_edit_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_activity_control_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_activity_source_version(){return 1;}
// Source editor already owns a delivered type11 fence. Do NOT enqueue a
// command behind that paused worker and then wait for it (self-deadlock).
// Ordinary prefix ACK/unlock pauses are not valid publication fences.
extern "C" __declspec(dllexport) int __cdecl midi_execution_activity_available_at(void* pointer,unsigned long long ticket,
        const unsigned* available,unsigned count,unsigned known){
    if(!pointer||!ticket||known>1||(count&&!available)||(!known&&count))return 0;
    std::vector<unsigned> staged;try{if(count){staged.assign(available,available+count);std::sort(staged.begin(),staged.end());
        staged.erase(std::unique(staged.begin(),staged.end()),staged.end());}}catch(...){return 0;}
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);if(!s.kernel||!s.kernel->notes)return 0;
    AcquireSRWLockExclusive(&s.lock);
    if(!s.paused||!s.delivered||s.resume||s.pendingTicket!=ticket||s.pendingKind!=11){ReleaseSRWLockExclusive(&s.lock);return 0;}
    auto& notes=*s.kernel->notes;AcquireSRWLockExclusive(&notes.lock);auto* activity=notes.activity;bool applied=false;
    if(activity&&!notes.activityDirty&&!notes.activityDepth&&!notes.activityKeep){AcquireSRWLockExclusive(&activity->lock);
        if(activity->attachedOwner==&notes&&!activity->reservationThread&&activity->revision!=NativeMidiActivity::absent){
            activity->available.swap(staged);activity->availableKnown=known!=0;activity->commit();applied=true;}
        ReleaseSRWLockExclusive(&activity->lock);}
    ReleaseSRWLockExclusive(&notes.lock);ReleaseSRWLockExclusive(&s.lock);
    if(applied){LARGE_INTEGER now{};QueryPerformanceCounter(&now);native_activity_view_publish(activity,static_cast<unsigned long long>(now.QuadPart));}
    return applied?1:0;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_activity_restore_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_activity_presentation_version(){return 1;}
// Additive read-only selection CORE image. This is not accepted-origin or
// Reset/Learn ownership transfer; their ordered journals remain mandatory.
// Immutable dimensions permit control-side allocation before ChannelLock.
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_selection_snapshot_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_selection_snapshot2_version(){return 1;}
// Product handoff admission includes read-only handling of completed ticket
// ring slots. Older snapshot images remain component-readable, not automatic
// selection-owner hydration at generic/type11 handoff.
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_selection_handoff_version(){return 1;}
// Additive product admission for compound origin/input/selection/activity
// stage+commit at the delivered fence. Ordered failures/Learn/config journals
// remain mandatory; this version does NOT authorize journal suppression.
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_provenance_handoff_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_failure_notification_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_cc_kernel_failure_notification_prepare(void* pointer,unsigned logOnly){
    if(!pointer||logOnly>1)return 0;auto& kernel=*static_cast<MidiCCKernel*>(pointer);AcquireSRWLockExclusive(&kernel.lock);
    if(kernel.executionOwner||kernel.frameReceipt||kernel.pendingStream||kernel.semanticActive||kernel.failureFencePending||
            !kernel.semantics||!kernel.semantics->journal->failureCapacity){ReleaseSRWLockExclusive(&kernel.lock);return 0;}
    kernel.deferredFailureLog=logOnly!=0;kernel.compatibilityCallback=logOnly==0;ReleaseSRWLockExclusive(&kernel.lock);return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_execution_failure_notification_at(void* pointer,unsigned long long ticket,unsigned logOnly){
    if(!pointer||!ticket||logOnly>1)return 0;auto& worker=*static_cast<MidiExecutionWorker*>(pointer);AcquireSRWLockExclusive(&worker.lock);
    if(!worker.kernel||!worker.paused||!worker.delivered||worker.resume||worker.pendingTicket!=ticket||
            (worker.pendingKind!=11&&worker.pendingKind!=2)){ReleaseSRWLockExclusive(&worker.lock);return 0;}
    auto& kernel=*worker.kernel;AcquireSRWLockExclusive(&kernel.lock);
    if(kernel.executionOwner!=&worker||!kernel.executionPaused||kernel.frameReceipt||kernel.pendingStream||kernel.semanticActive||
            kernel.semanticFramePending||kernel.gmActive||kernel.resetTailStream||kernel.failureFencePending||!kernel.semantics||
            !kernel.semantics->journal->failureCapacity){ReleaseSRWLockExclusive(&kernel.lock);ReleaseSRWLockExclusive(&worker.lock);return 0;}
    auto* journal=kernel.semantics->journal;AcquireSRWLockShared(&journal->failureLock);
    const bool drained=journal->failureProduced==journal->failureConsumed&&!journal->failureReaders.load()&&!journal->failureActiveLeases&&!journal->failureReserved;
    ReleaseSRWLockShared(&journal->failureLock);
    if(drained){kernel.deferredFailureLog=logOnly!=0;kernel.compatibilityCallback=logOnly==0;}
    ReleaseSRWLockExclusive(&kernel.lock);ReleaseSRWLockExclusive(&worker.lock);return drained?1:0;
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_selection_state_snapshot_words(void* pointer){
    if(!pointer)return 0;const auto& s=*static_cast<NativeSelectionState*>(pointer);
    return 16ull+3ull*s.watches.size()+128ull*(14ull+7ull*s.fields.size()+2ull*s.watches.size())+514ull;
}
static unsigned long long selectionSnapshotWords2(const NativeSelectionState& s){
    return midi_selection_state_snapshot_words(const_cast<NativeSelectionState*>(&s))+5ull+128ull*10ull+3ull*s.keyRevisions.size();
}
// Read-only reuse of the complete existing selection/provenance codec at a
// failed action. This does NOT authorize generation replacement while an
// original frame or semantic suffix still owns that generation.
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_failure_snapshot_version(){return 1;}
static bool executionFailureSnapshotFence(const MidiExecutionWorker& worker){
    return worker.pendingKind==6&&worker.kernel&&worker.kernel->failureFencePending&&
        worker.kernel->failureFenceStream==worker.stream&&worker.kernel->failureFenceSerial&&
        worker.kernel->semantics&&worker.kernel->semantics->journal&&
        worker.kernel->semantics->journal->clock->serial.load(std::memory_order_acquire)==worker.kernel->failureFenceSerial;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_selection_state_provenance_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_selection_snapshot3_version(){return 1;}
// binding7: state,event,role,field,source,phase,activation; role1=origin,
//2=input offset,3=SYSTEM clear,4=CC121 subset. Seed11: domain,ch,field,key,
//event,raw int64 bits,source,phase,presence,opaque prior sequence,activation.
// Preparation allocates before retaining donor roots. Counts are own codec
// dimensions, NOT native library maxima. No callback allocation/new locks.
extern "C" __declspec(dllexport) int __cdecl midi_selection_state_provenance_seed(void* pointer,
        const unsigned long long* bindings,unsigned bindingCount,const unsigned long long* seeds,unsigned seedCount,
        const unsigned* events,unsigned eventCount,unsigned fieldCount,unsigned sourceCount,unsigned phaseCount,
        unsigned long long activation){
    if(!pointer||(bindingCount&&!bindings)||(seedCount&&!seeds)||(eventCount&&!events)||!fieldCount||!sourceCount||!phaseCount)return 0;
    auto& s=*static_cast<NativeSelectionState*>(pointer);
    if(!s.preparationMutable()||!s.metadataReady||s.provenanceReady||s.clock->writers.load()||s.clock->serial.load()!=activation||activation>=(1ull<<48))return 0;
    try{
        std::vector<NativeSelectionState::ProvenanceBinding> stagedBindings;
        std::vector<NativeMidiState*> stagedRoots;
        std::vector<std::array<unsigned long long,11>> stagedSeeds;
        std::vector<NativeSelectionState::ProvenanceAccepted> stagedEvents;
        unsigned long long cells=0;
        for(unsigned i=0;i<bindingCount;++i){const auto* row=bindings+static_cast<size_t>(i)*7;
            if(!row[0]||row[1]>UINT_MAX||row[2]<1||row[2]>5||row[3]>=fieldCount||row[4]>=sourceCount||row[5]>=phaseCount||row[6]!=activation)return 0;
            auto* donor=reinterpret_cast<NativeMidiState*>(row[0]);
            if(donor->clock!=s.clock||donor->stream!=s.stream)return 0;
            unsigned slot=256;for(unsigned n=0;n<donor->eventCount;++n)if(donor->eventTypes[n]==row[1]){slot=n;break;}
            if(slot==256||(row[2]==3&&(row[1]!=MIDI_EVENT_SYSTEM||donor->lengths[slot]!=1))||
                    (row[2]==4&&row[1]!=MIDI_EVENT_RESET)||(row[2]==2&&donor->lengths[slot]!=128)||
                    (row[2]==5&&(row[1]!=0x1d0000u||donor->lengths[slot]!=128)))return 0;
            for(const auto& prior:stagedBindings)if(prior.state==donor&&prior.event==row[1]&&prior.role==row[2]&&prior.field==row[3])return 0;
            stagedBindings.push_back({donor,static_cast<unsigned>(row[1]),slot,static_cast<unsigned>(row[2]),static_cast<unsigned>(row[3]),
                static_cast<unsigned>(row[4]),static_cast<unsigned>(row[5]),activation});cells+=donor->lengths[slot];
            if(std::find(stagedRoots.begin(),stagedRoots.end(),donor)==stagedRoots.end())stagedRoots.push_back(donor);
        }
        for(unsigned i=0;i<seedCount;++i){const auto* row=seeds+static_cast<size_t>(i)*11;
            if(row[0]<1||row[0]>2||row[1]>=128||row[2]>=fieldCount||(row[3]!=UINT_MAX&&row[3]>127)||row[4]>UINT_MAX||
                    row[6]>=sourceCount||row[7]>=phaseCount||row[8]>1||row[10]!=activation)return 0;
            for(const auto& prior:stagedSeeds)if(std::equal(row,row+4,prior.begin()))return 0;
            std::array<unsigned long long,11> copy{};std::copy_n(row,11,copy.begin());stagedSeeds.push_back(copy);
        }
        for(unsigned i=0;i<eventCount;++i){const auto event=events[i*2],field=events[i*2+1];
            if(!event||field>=fieldCount||(i&&stagedEvents.back().event>=event))return 0;
            stagedEvents.push_back({event,field,{},{},{}});
        }
        const auto core=selectionSnapshotWords2(s);
        const auto required=core+14ull+8ull*bindingCount+cells+11ull*seedCount+386ull*eventCount;
        if(required>UINT_MAX)return 0;
        std::vector<unsigned long long> scratch(static_cast<size_t>(required));
        if(s.clock->writers.load(std::memory_order_acquire)||s.clock->serial.load(std::memory_order_acquire)!=activation)return 0;
        for(auto* root:stagedRoots)root->retain();
        s.provenanceBindings.swap(stagedBindings);s.provenanceRoots.swap(stagedRoots);s.provenanceSeeds.swap(stagedSeeds);
        s.provenanceAccepted.swap(stagedEvents);s.provenanceScratch.swap(scratch);s.provenanceCoreWords=core;
        s.provenanceActivation=activation;s.provenanceSources=sourceCount;s.provenancePhases=phaseCount;s.provenanceFields=fieldCount;s.provenanceReady=true;return 1;
    }catch(const std::bad_alloc&){return 0;}
}
struct MidiSelectionSnapshotRequest {
    void* execution;unsigned long long ticket;NativeSelectionState* selection;unsigned stream;
    unsigned long long* words;unsigned capacity;int status;
};
// Source-only fast publication. The catalog's audible control policy is
// unchanged (qualified by the control owner). Keep the canonical parser,
// selection, provenance, Learn, Note and semantic generations in place.
// This is NOT a lock-free FontSetFonts promise: BASS can stop voices whose
// samples were replaced. Run inside channel_transaction_run kind2 so the GIL
// is never reacquired while the BASS channel is locked.
struct MidiSourceRefreshRequest {
    void* execution;unsigned long long ticket;NativeSelectionState* selection;
    unsigned stream,channels,operation;unsigned* addresses;int status;
};
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_source_refresh_version(){return 1;}
extern "C" __declspec(dllexport) void __cdecl midi_execution_source_refresh_at(void* pointer){
    if(!pointer)return;auto& r=*static_cast<MidiSourceRefreshRequest*>(pointer);r.status=0;
    if(!r.execution||!r.selection||!r.channels||r.channels>128||r.operation>2||!r.addresses)return;
    auto& worker=*static_cast<MidiExecutionWorker*>(r.execution);AcquireSRWLockExclusive(&worker.lock);
    if(!worker.kernel||!worker.paused||!worker.delivered||worker.resume||worker.pendingTicket!=r.ticket||worker.pendingKind!=11){
        ReleaseSRWLockExclusive(&worker.lock);return;}
    auto& k=*worker.kernel;AcquireSRWLockExclusive(&k.lock);auto& s=*r.selection;
    if(k.selection!=r.selection||k.executionOwner!=&worker||!k.executionPaused||k.compatibilityCallback||
        k.cancelledStream||k.pendingStream||k.frameReceipt||k.semanticActive||k.semanticFramePending||k.gmActive||k.resetTailStream||
        s.stream!=r.stream||s.profileBound||s.extendedBound||s.preparationPending()||s.settleLocked||
        s.clock->writers.load()||s.reserved.load()||s.queuedReservation.load()||s.unticketedReservation.load()){
        ReleaseSRWLockExclusive(&k.lock);ReleaseSRWLockExclusive(&worker.lock);return;}
    bool pending=false;AcquireSRWLockShared(&s.ticketsLock);
    for(unsigned i=0;i<s.ticketCount;++i)if(s.tickets[(s.ticketHead+i)%static_cast<unsigned>(s.tickets.size())].remaining){pending=true;break;}
    ReleaseSRWLockShared(&s.ticketsLock);
    // No allocation or partial mutation on insufficient journal capacity.
    // Per-channel writes below reuse the independently bounded apply chain.
    if(pending||(r.operation!=2&&!s.room(r.channels*s.need()))){ReleaseSRWLockExclusive(&k.lock);ReleaseSRWLockExclusive(&worker.lock);return;}
    if(r.operation==1){
        const unsigned budget=r.channels*s.need();
        if(!s.reserve(budget)){ReleaseSRWLockExclusive(&k.lock);ReleaseSRWLockExclusive(&worker.lock);return;}
        s.queuedReservation.fetch_sub(budget,std::memory_order_release);s.clock->writers.fetch_add(1,std::memory_order_acq_rel);
        for(unsigned ch=0;ch<r.channels;++ch){
            const auto address=s.requestedAddress[ch].load();auto& current=s.channels[ch];
            current.bank=address&127u;current.lsb=(address>>8)&127u;current.program=(address>>16)&127u;current.drums=(address&0x1000000u)!=0;
            const auto reset=s.requestedTokenReset[ch].load();
            if(reset!=s.tokenResetSeen[ch]){current.tokenValid=current.tokenKnown=false;s.tokenResetSeen[ch]=reset;}
            s.observeWatches(ch);
            if(!s.apply(ch)){r.status=-8;break;}
        }
        s.clock->writers.fetch_sub(1,std::memory_order_release);s.reserved.fetch_sub(budget,std::memory_order_release);
    }
    for(unsigned ch=0;ch<128;++ch){const auto address=s.requestedAddress[ch].load();auto* out=r.addresses+ch*4;
        out[0]=(address&0x1000000u)?128u:address&127u;out[1]=(address>>16)&127u;out[2]=(address>>8)&127u;out[3]=0;
        // Resolved source identity for the activity fallback only. It never
        // rewrites the Note's retained identity or the incoming MIDI bank.
        int bank=-1,program=-1;s.current(ch,bank,program);
        out[3]=(bank>=0&&program>=0)?(static_cast<unsigned>(bank)<<16)|static_cast<unsigned>(program):UINT_MAX;
    }
    if(!r.status)r.status=1;
    ReleaseSRWLockExclusive(&k.lock);ReleaseSRWLockExclusive(&worker.lock);
}
// Unlike the standalone preparation-size helper, this live sizing call never
// dereferences a displaced adapter. The current kernel retains the selected
// root under this lock, even if publication races the later copy (which then
// refuses the old identity rather than touching it).
static unsigned long long selectionSnapshotWordsAt(void* pointer,unsigned long long ticket,void* selection,unsigned level){
    if(!pointer||!ticket||!selection)return 0;auto& worker=*static_cast<MidiExecutionWorker*>(pointer);
    AcquireSRWLockExclusive(&worker.lock);unsigned long long result=0;
    if(worker.kernel&&worker.paused&&worker.delivered&&!worker.resume&&worker.pendingTicket==ticket&&
            (worker.pendingKind==2||worker.pendingKind==11||executionFailureSnapshotFence(worker))){
        auto& kernel=*worker.kernel;AcquireSRWLockExclusive(&kernel.lock);
        if(kernel.executionOwner==&worker&&kernel.executionPaused&&kernel.selection==selection&&(level<2||kernel.selection->metadataReady))
            result=level==3?(kernel.selection->provenanceReady?kernel.selection->provenanceScratch.size():0):
                level==2?selectionSnapshotWords2(*kernel.selection):midi_selection_state_snapshot_words(kernel.selection);
        ReleaseSRWLockExclusive(&kernel.lock);
    }
    ReleaseSRWLockExclusive(&worker.lock);return result;
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_selection_snapshot_words_at(void* pointer,unsigned long long ticket,void* selection){
    return selectionSnapshotWordsAt(pointer,ticket,selection,1);
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_selection_snapshot_words_at2(void* pointer,unsigned long long ticket,void* selection){
    return selectionSnapshotWordsAt(pointer,ticket,selection,2);
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_selection_snapshot_words_at3(void* pointer,unsigned long long ticket,void* selection){return selectionSnapshotWordsAt(pointer,ticket,selection,3);}
// channel_transaction_run kind2 only: BASS_ChannelLock/copy/unlock all stay
// native. Exact delivered ingress ownership, not SRW lock acquisition order,
// defines the snapshot boundary. Capacity/busy failures leave words untouched.
static void selectionSnapshotCopy(void* pointer,unsigned level){
    if(!pointer)return;auto& request=*static_cast<MidiSelectionSnapshotRequest*>(pointer);request.status=0;
    if(!request.execution||!request.ticket||!request.selection||!request.words)return;
    auto& worker=*static_cast<MidiExecutionWorker*>(request.execution);
    AcquireSRWLockExclusive(&worker.lock);
    if(!worker.kernel||!worker.paused||!worker.delivered||worker.resume||worker.pendingTicket!=request.ticket||
            (worker.pendingKind!=2&&worker.pendingKind!=11&&!executionFailureSnapshotFence(worker))){
        ReleaseSRWLockExclusive(&worker.lock);return;}
    auto& kernel=*worker.kernel;AcquireSRWLockExclusive(&kernel.lock);auto& s=*request.selection;
    const bool failureFence=executionFailureSnapshotFence(worker);
    if(kernel.executionOwner!=&worker||!kernel.executionPaused||kernel.selection!=&s||kernel.cancelledStream||kernel.pendingStream||
            (!failureFence&&(kernel.frameReceipt||kernel.semanticActive||kernel.semanticFramePending||kernel.gmActive||kernel.resetTailStream))||
            s.stream!=request.stream||s.preparationPending()||s.settleLocked||
            (level>=2&&!s.metadataReady)||(level==3&&(!s.provenanceReady||s.provenanceCoreWords!=selectionSnapshotWords2(s)))){
        request.status=-3;ReleaseSRWLockExclusive(&kernel.lock);ReleaseSRWLockExclusive(&worker.lock);return;}
    AcquireSRWLockShared(&s.ticketsLock);
    // A fully processed ticket can remain in the ring until the next
    // submission trims it. It owns no callbacks/reservation and must not
    // reject a delivered generic handoff after that last Program event.
    // Read only: never trim/ACK a ticket merely to obtain a snapshot.
    bool pendingTicket=false;
    for(unsigned i=0;i<s.ticketCount;++i)
        if(s.tickets[(s.ticketHead+i)%static_cast<unsigned>(s.tickets.size())].remaining){pendingTicket=true;break;}
    if(s.reserved.load()||s.queuedReservation.load()||s.unticketedReservation.load()||pendingTicket){
        request.status=-3;ReleaseSRWLockShared(&s.ticketsLock);ReleaseSRWLockExclusive(&kernel.lock);ReleaseSRWLockExclusive(&worker.lock);return;}
    const auto required=level==3?s.provenanceScratch.size():level==2?selectionSnapshotWords2(s):midi_selection_state_snapshot_words(&s);
    if(required>request.capacity){
        request.status=-2;ReleaseSRWLockShared(&s.ticketsLock);ReleaseSRWLockExclusive(&kernel.lock);ReleaseSRWLockExclusive(&worker.lock);return;}
    AcquireSRWLockShared(&s.recordsLock);
    const auto capture=s.clock->serial.load(std::memory_order_acquire);
    if(level==3&&(s.clock->writers.load(std::memory_order_acquire)||capture>=(1ull<<48))){request.status=-3;ReleaseSRWLockShared(&s.recordsLock);ReleaseSRWLockShared(&s.ticketsLock);ReleaseSRWLockExclusive(&kernel.lock);ReleaseSRWLockExclusive(&worker.lock);return;}
    auto* out=level==3?s.provenanceScratch.data():request.words;const unsigned fields=static_cast<unsigned>(s.fields.size()),watches=static_cast<unsigned>(s.watches.size());
    const unsigned long long header[]{level,128,fields,watches,s.produced.load(),s.consumed.load(),capture,
        s.failures.load(),s.refused.load(),s.graphEnabled?1ull:0ull,s.graphReady?1ull:0ull,s.graphChannels,
        s.userMix,s.userDSP,s.recordLimit.load(),required};
    std::copy_n(header,16,out);out+=16;
    for(const auto& watch:s.watches){*out++=watch.field;*out++=watch.slot;*out++=watch.kind;}
    for(unsigned channel=0;channel<128;++channel){const auto& ch=s.channels[channel];
        const unsigned long long address[]{s.requestedAddress[channel].load(),s.requestedTokenReset[channel].load(),s.tokenResetSeen[channel],
            ch.bank,ch.lsb,ch.program,ch.drums?1ull:0ull,ch.tokenValid?1ull:0ull,ch.tokenKnown?1ull:0ull,
            static_cast<unsigned>(ch.tokenBank),static_cast<unsigned>(ch.tokenProgram),ch.tokenLSB,ch.tokenDrums?1ull:0ull,s.touchedOrder[channel].size()};
        std::copy_n(address,14,out);out+=14;
        for(unsigned field=0;field<fields;++field){
            *out++=ch.profile[field];*out++=ch.baseline[field];*out++=ch.touched[field];*out++=ch.modCurrent[field];*out++=static_cast<unsigned>(ch.applied[field]);
            *out++=static_cast<unsigned>(ch.profilePresent[field])|(static_cast<unsigned>(ch.baselinePresent[field])<<1)|
                (static_cast<unsigned>(ch.touchedPresent[field])<<2)|(static_cast<unsigned>(ch.modPresent[field])<<3)|(static_cast<unsigned>(ch.appliedPresent[field])<<4);
        }
        std::fill_n(out,fields,0ull);std::copy(s.touchedOrder[channel].begin(),s.touchedOrder[channel].end(),out);out+=fields;
        // Do NOT fold or consume outstanding watched writers. Both sides of
        // their captured frontier are part of the core image.
        for(const auto& watch:s.watches){*out++=watch.serial[channel];*out++=watch.state->cells[watch.state->offsets[watch.slot]+channel].load(std::memory_order_acquire);}
    }
    for(unsigned channel=0;channel<128;++channel)*out++=s.graphMixes[channel];
    for(unsigned channel=0;channel<128;++channel)*out++=s.graphDSPs[channel];
    for(unsigned channel=0;channel<128;++channel)*out++=s.graphActive[channel];
    for(unsigned channel=0;channel<128;++channel)*out++=s.channels[channel].drums?1ull:0ull;
    *out++=s.userMix;*out++=s.userDSP;
    if(level>=2){*out++=1;*out++=s.metadataReady?1:0;*out++=s.streamGeneration;*out++=s.profileRevision;*out++=s.keyRevisions.size();
        for(unsigned channel=0;channel<128;++channel){s.metadataWords(channel,out);out+=10;}
        for(const auto& revision:s.keyRevisions){*out++=revision.bank;*out++=revision.program;*out++=revision.revision;}}
    if(level==3){
        unsigned long long totalCells=0;for(const auto& binding:s.provenanceBindings)totalCells+=binding.state->lengths[binding.slot];
        const unsigned long long footer[]{1,s.streamGeneration,s.provenanceActivation,capture,s.provenanceSources,s.provenancePhases,
            s.provenanceBindings.size(),s.provenanceSeeds.size(),s.provenanceAccepted.size(),s.provenanceOriginClear,totalCells,required,s.provenanceCoreWords,s.provenanceFields};
        std::copy_n(footer,14,out);out+=14;
        for(const auto& binding:s.provenanceBindings){const auto length=binding.state->lengths[binding.slot];
            const unsigned long long row[]{reinterpret_cast<unsigned long long>(binding.state),binding.event,binding.role,binding.field,binding.source,binding.phase,binding.activation,length};
            std::copy_n(row,8,out);out+=8;
            for(unsigned i=0;i<length;++i)*out++=binding.state->cells[binding.state->offsets[binding.slot]+i].load(std::memory_order_acquire);
        }
        for(const auto& seed:s.provenanceSeeds){std::copy(seed.begin(),seed.end(),out);out+=11;}
        for(const auto& row:s.provenanceAccepted){*out++=row.event;*out++=row.field;
            for(unsigned i=0;i<128;++i){*out++=row.serial[i];*out++=row.raw[i];*out++=row.kind[i];}}
        if(s.clock->writers.load(std::memory_order_acquire)||s.clock->serial.load(std::memory_order_acquire)!=capture)request.status=-3;
        else{std::copy(s.provenanceScratch.begin(),s.provenanceScratch.end(),request.words);request.status=1;}
    }else request.status=1;
    ReleaseSRWLockShared(&s.recordsLock);ReleaseSRWLockShared(&s.ticketsLock);
    ReleaseSRWLockExclusive(&kernel.lock);ReleaseSRWLockExclusive(&worker.lock);
}
extern "C" __declspec(dllexport) void __cdecl midi_execution_selection_snapshot_copy(void* pointer){selectionSnapshotCopy(pointer,1);}
extern "C" __declspec(dllexport) void __cdecl midi_execution_selection_snapshot_copy2(void* pointer){selectionSnapshotCopy(pointer,2);}
extern "C" __declspec(dllexport) void __cdecl midi_execution_selection_snapshot_copy3(void* pointer){selectionSnapshotCopy(pointer,3);}
// Complete reconciliation after an owned legacy handoff or source edit. A
// paused flag alone never grants mutation authority: the exact delivered
// ticket must still be waiting, including refusal after resume is accepted.
// Stage/copy on the control thread, then commit canonical state, physical
// overlap counters and fallback identities together. No BASS/audio call.
// SRW locks are not FIFO; the delivered ingress fence supplies ordering:
// https://learn.microsoft.com/en-us/windows/win32/sync/slim-reader-writer--srw--locks
extern "C" __declspec(dllexport) int __cdecl midi_execution_activity_restore_at(void* pointer,unsigned long long ticket,
        unsigned long long revision,const unsigned long long* header,unsigned headerCount,
        const unsigned long long* channels,unsigned channelCount,const unsigned long long* presets,unsigned presetCount,
        const unsigned long long* fifo,unsigned fifoCount,const unsigned long long* counts,unsigned countCount,
        const unsigned* available,unsigned availableCount,const unsigned* sources,unsigned sourceCount){
    if(!pointer||!ticket||!sources||!sourceCount||sourceCount>128)return 0;
    std::array<NativeActivityIdentity,128> stagedSources{};
    for(unsigned i=0;i<sourceCount;++i){const auto* row=sources+i*3;
        if(row[0]>1||row[1]>65535||row[2]>65535)return 0;
        stagedSources[i]={row[0]!=0,row[1],row[2]};}
    auto staged=activityTransferStage(header,headerCount,channels,channelCount,presets,presetCount,
        fifo,fifoCount,counts,countCount,available,availableCount);if(!staged)return 0;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);if(!s.kernel||!s.kernel->notes)return 0;
    AcquireSRWLockExclusive(&s.lock);
    if(!s.paused||!s.delivered||s.resume||s.pendingTicket!=ticket||(s.pendingKind!=2&&s.pendingKind!=11)){
        ReleaseSRWLockExclusive(&s.lock);return 0;}
    auto& kernel=*s.kernel;AcquireSRWLockExclusive(&kernel.lock);auto& notes=*kernel.notes;
    if(kernel.executionOwner!=&s||!kernel.executionPaused||kernel.cancelledStream||kernel.frameReceipt||kernel.pendingStream||kernel.semanticActive||kernel.semanticFramePending||kernel.gmActive||kernel.resetTailStream||
            sourceCount!=notes.channels||channelCount!=notes.channels){
        ReleaseSRWLockExclusive(&kernel.lock);ReleaseSRWLockExclusive(&s.lock);return 0;}
    AcquireSRWLockExclusive(&notes.lock);auto* activity=notes.activity;int result=0;
    if(activity&&notes.executionOwner==&s&&notes.executionPaused&&!notes.activityDepth&&!notes.activityKeep){
        AcquireSRWLockExclusive(&activity->lock);
        if(activity->revision!=revision)result=-1;
        else if(activity->attachedOwner==&notes&&!activity->reservationThread&&activity->revision!=NativeMidiActivity::absent&&
                activity->channels.size()==staged->channels.size()){
            activity->channels.swap(staged->channels);activity->presets.swap(staged->presets);activity->available.swap(staged->available);
            activity->availableKnown=staged->availableKnown;activity->ordinal=staged->ordinal;
            notes.counts.fill(0);
            for(unsigned ch=0;ch<notes.channels;++ch)for(unsigned key=0;key<128;++key)
                notes.counts[ch*128+key]=activity->channels[ch].keys[key].overlap;
            notes.activitySources=stagedSources;notes.activitySourcesReady=true;
            notes.activityDirty=false;activity->shadowDirty=false;activity->commit();activity->qualify(true);result=1;
        }
        ReleaseSRWLockExclusive(&activity->lock);
    }
    ReleaseSRWLockExclusive(&notes.lock);ReleaseSRWLockExclusive(&kernel.lock);ReleaseSRWLockExclusive(&s.lock);
    if(result==1){LARGE_INTEGER now{};QueryPerformanceCounter(&now);native_activity_view_publish(activity,static_cast<unsigned long long>(now.QuadPart));}
    return result;
}
// kind1=Played history clear, kind2=Available replacement. Prepared immutable
// identities are low16 bank/high16 program. Audio/Note state is untouched.
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_activity_control(void* pointer,unsigned kind,
        const unsigned* available,unsigned count,unsigned known){
    if(!pointer||kind<1||kind>2||known>1||(count&&!available)||(!known&&count)||(kind==1&&(count||known)))return 0;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);if(!s.kernel||!s.kernel->notes)return 0;
    AcquireSRWLockShared(&s.kernel->notes->lock);const bool ready=s.kernel->notes->activity&&!s.kernel->notes->activityDirty;
    ReleaseSRWLockShared(&s.kernel->notes->lock);if(!ready)return 0;
    try{
        auto command=std::make_unique<MidiExecutionWorker::Control>();command->results.resize(1);
        command->activityKind=kind;command->activityAvailableKnown=known!=0;
        if(count){command->activityAvailable.assign(available,available+count);
            std::sort(command->activityAvailable.begin(),command->activityAvailable.end());
            command->activityAvailable.erase(std::unique(command->activityAvailable.begin(),command->activityAvailable.end()),command->activityAvailable.end());}
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;
        const auto ticket=command->ticket;if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(...){return 0;}
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_source_edit(void* pointer){
    if(!pointer)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    try{
        const MidiExecutionWorker::ControlStep dummy{};auto command=std::make_unique<MidiExecutionWorker::Control>(&dummy,1);
        command->steps.clear();command->results.clear();command->sourceEdit=true;
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;
        const auto ticket=command->ticket;if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_preset_tone_version(){return 1;}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_preset_tone(void* pointer,
    const MidiExecutionWorker::PresetToneStep* rows,unsigned count,unsigned channels,void* channelLock){
    if(!pointer||!rows||!count||count>65536||!channels||channels>128||!channelLock)return 0;
    for(unsigned i=0;i<count;++i){
        if(rows[i].bank>65535||rows[i].program>65535||(rows[i].flags&~252u)||rows[i].tone[0]>400)return 0;
        for(unsigned k=1;k<6;++k)if(rows[i].tone[k]>127)return 0;
    }
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    try{
        auto command=std::make_unique<MidiExecutionWorker::Control>();
        command->presetTone.assign(rows,rows+count);command->results.resize(channels);
        command->presetToneChannels=channels;command->presetToneLock=channelLock;
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;
        const auto ticket=command->ticket;if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}
static unsigned long long midiExecutionResetPlan(void* pointer,unsigned source,int systemMode=-1,const MidiExecutionWorker::ControlStep* restore=nullptr,unsigned count=0){
    if(!pointer)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    const bool modeCommand=systemMode>=0;
    AcquireSRWLockShared(&s.kernel->lock);const bool ready=modeCommand?(s.kernel->gmReady&&s.kernel->manualResetReady):source?s.kernel->fileResetReady:s.kernel->manualResetReady;ReleaseSRWLockShared(&s.kernel->lock);
    if(!ready)return 0;
    try{
        const MidiExecutionWorker::ControlStep dummy{};auto command=std::make_unique<MidiExecutionWorker::Control>(&dummy,1);
        command->manualReset=true;command->fileResetSource=source;command->systemModeCommand=modeCommand;command->systemMode=modeCommand?static_cast<unsigned>(systemMode):0;
        if(count)command->systemRestore.assign(restore,restore+count);
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;
        const auto ticket=command->ticket;if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_reset(void* pointer){return midiExecutionResetPlan(pointer,0);}
// bassmidi.h SYSTEMEX: explicit one-shot reset, not a mode lock or SysEx filter.
// The existing FIFO/reset owner retains stream and metadata through completion.
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_system_mode_version(){return 3;}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_system_mode(void* pointer,unsigned mode,const MidiExecutionWorker::ControlStep* restore,unsigned count){
    if(mode>4||count>32||(count&&!restore))return 0;
    for(unsigned i=0;i<count;++i)if(restore[i].channel||!(restore[i].event==MIDI_EVENT_MASTERVOL||
        (restore[i].event>=MIDI_EVENT_REVERB_TIME&&restore[i].event<=MIDI_EVENT_CHORUS_REVERB)))return 0;
    return midiExecutionResetPlan(pointer,0,mode,restore,count);
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_file_reset_version(){return 1;}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_file_reset(void* pointer,unsigned source){
    return source==29||source==31?midiExecutionResetPlan(pointer,source):0;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_fx_topology_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_stream_effect_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_equalizer_version(){return 1;}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_equalizer(void* pointer,void* prepared){
    if(!pointer||!prepared)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);auto& plan=*static_cast<StreamEQPlan*>(prepared);
    if(plan.graph->stream!=s.stream)return 0;
    try{const MidiExecutionWorker::ControlStep dummy{};auto command=std::make_unique<MidiExecutionWorker::Control>(&dummy,1);
        command->steps.clear();command->equalizer=&plan;plan.retain();AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;
        const auto ticket=command->ticket;if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_stream_effect(void* pointer,void* prepared){
    if(!pointer||!prepared)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);auto& plan=*static_cast<StreamVendorPlan*>(prepared);
    if(plan.graph->stream!=s.stream)return 0;
    try{
        const MidiExecutionWorker::ControlStep dummy{};auto command=std::make_unique<MidiExecutionWorker::Control>(&dummy,1);
        command->steps.clear();command->streamEffect=&plan;plan.retain();
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;
        const auto ticket=command->ticket;if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_fx_topology(void* pointer,void* prepared){
    if(!pointer||!prepared)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);auto& plan=*static_cast<FXTopologyPlan*>(prepared);
    if(plan.graph->stream!=s.stream)return 0;
    try{
        const MidiExecutionWorker::ControlStep dummy{};auto command=std::make_unique<MidiExecutionWorker::Control>(&dummy,1);
        command->steps.clear();command->topology=&plan;plan.retain();
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;const auto ticket=command->ticket;
        if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_macro(void* pointer,
        unsigned channel,unsigned event,unsigned parameter,const unsigned* reads,unsigned count,void* getter){
    if(!pointer||!reads||!count||count>32||!getter||(event!=MIDI_EVENT_REVERB_MACRO&&event!=MIDI_EVENT_CHORUS_MACRO))return 0;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);if(channel>=s.kernel->channels)return 0;
    try{
        std::array<MidiExecutionWorker::ControlStep,33> steps{};steps[0]={channel,event,parameter};
        for(unsigned index=0;index<count;++index)steps[index+1]={channel,reads[index],0};
        auto command=std::make_unique<MidiExecutionWorker::Control>(steps.data(),count+1);command->macro=true;command->macroGetter=getter;
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;const auto ticket=command->ticket;
        if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}

extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_batch_version(){return 1;}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_batch(void* pointer,unsigned channel,
        const MidiExecutionWorker::ControlStep* steps,unsigned count){
    if(!pointer||(count&&!steps)||count>1024)return 0;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);if(channel>=s.kernel->channels)return 0;
    for(unsigned i=0;i<count;++i)if(steps[i].channel>=s.kernel->channels)return 0;
    try{
        const MidiExecutionWorker::ControlStep dummy{};
        auto command=std::make_unique<MidiExecutionWorker::Control>(&dummy,0);
        command->batch=true;command->batchChannel=channel;command->results.resize(1);command->batchEvents.resize(count);
        for(unsigned i=0;i<count;++i)command->batchEvents[i]={steps[i].event,steps[i].param,steps[i].channel,0,0};
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;const auto ticket=command->ticket;
        if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}

extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_send_floor_version(){return 1;}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_send_floor(void* pointer,void* domain,
        unsigned event,unsigned value,const unsigned* channels,unsigned count){
    if(!pointer||!domain||!channels||!count||count>128||value>127||(event!=MIDI_EVENT_REVERB&&event!=MIDI_EVENT_CHORUS))return 0;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    if(domain!=s.kernel->sends||!s.kernel->manualJournal)return 0;
    for(unsigned i=0;i<count;++i){if(channels[i]>=s.kernel->channels)return 0;for(unsigned j=0;j<i;++j)if(channels[i]==channels[j])return 0;}
    try{
        std::array<MidiExecutionWorker::ControlStep,256> steps{};
        for(unsigned i=0;i<count;++i){steps[i*2]={channels[i],MIDI_EVENT_REVERB,0};steps[i*2+1]={channels[i],MIDI_EVENT_CHORUS,0};}
        auto command=std::make_unique<MidiExecutionWorker::Control>(steps.data(),count*2);
        command->sendFloor=true;command->floorEvent=event;command->floorValue=value;command->continueOnError=true;command->floorRows.resize(count*2);
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(s.stopping.load()||!s.acceptingControls.load()||s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        command->ticket=++s.controlSerial;command->fence=s.acceptedFence();
        LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;const auto ticket=command->ticket;
        if(!s.controlCount)s.controlFence.store(command->fence);
        s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}

static void* midiExecutionCreate(void* queue,void* kernel,void* origins,void* timing,unsigned stream,unsigned mode,unsigned port,bool multi){
    constexpr unsigned allowed=BASS_MIDI_EVENTS_RAW|BASS_MIDI_EVENTS_FILTER|BASS_MIDI_EVENTS_SYNC|BASS_MIDI_EVENTS_ASYNC;
    if((!queue&&!multi)||!kernel||!origins||!stream||port>=8||mode&~allowed||(mode&BASS_MIDI_EVENTS_RAW)!=BASS_MIDI_EVENTS_RAW||
        (timing&&static_cast<PlaybackTiming*>(timing)->stream!=stream))return nullptr;
    auto* q=static_cast<MidiPipeQueue*>(queue);auto* k=static_cast<MidiCCKernel*>(kernel);auto* o=static_cast<MidiExecutionOrigin*>(origins);
    if(!k->notes||!k->journal||k->journal->stream!=stream)return nullptr;
    try{
        auto s=std::make_unique<MidiExecutionWorker>(q,k,o,static_cast<PlaybackTiming*>(timing),stream,mode,port);
        o->retain();if(timing)static_cast<PlaybackTiming*>(timing)->retain();
        if(multi){
            s->ingress=new(std::nothrow)MidiIngressSession;if(!s->ingress||!s->ingress->ready)return nullptr;
            s->directQueue=std::make_unique<MidiPipeQueue>(nullptr);
            auto& client=s->ingress->clients[8];client.queue=s->directQueue.get();client.id=UINT64_MAX;
            s->directQueue->consumer=s.get();s->directQueue->session=s->ingress;s->ingress->retain();
        }
        if(q){AcquireSRWLockExclusive(&q->lock);
            if(q->consumer||q->activePops||q->stopping){ReleaseSRWLockExclusive(&q->lock);return nullptr;}q->consumer=s.get();ReleaseSRWLockExclusive(&q->lock);}
        AcquireSRWLockExclusive(&k->lock);
        auto unbind=[&](){if(q){AcquireSRWLockExclusive(&q->lock);q->consumer=nullptr;ReleaseSRWLockExclusive(&q->lock);}};
        if(k->executionOwner||k->cancelledStream||k->frameReceipt||k->pendingStream||k->semanticActive||k->semanticFramePending){ReleaseSRWLockExclusive(&k->lock);unbind();return nullptr;}
        AcquireSRWLockExclusive(&k->notes->lock);
        if(k->notes->executionOwner){ReleaseSRWLockExclusive(&k->notes->lock);ReleaseSRWLockExclusive(&k->lock);
            unbind();return nullptr;}
        k->notes->executionOwner=s.get();k->notes->executionPaused=false;ReleaseSRWLockExclusive(&k->notes->lock);
        k->executionOwner=s.get();k->executionStop=&s->stopping;k->idle=&s->idle;k->executionThread.store(0);ReleaseSRWLockExclusive(&k->lock);
        s->thread=reinterpret_cast<HANDLE>(_beginthreadex(nullptr,0,&MidiExecutionWorker::run,s.get(),CREATE_SUSPENDED,nullptr));
        if(!s->thread){AcquireSRWLockExclusive(&k->lock);k->executionOwner=nullptr;k->executionStop=nullptr;k->idle=nullptr;
            AcquireSRWLockExclusive(&k->notes->lock);k->notes->executionOwner=nullptr;ReleaseSRWLockExclusive(&k->notes->lock);ReleaseSRWLockExclusive(&k->lock);
            unbind();return nullptr;}
        return s.release();
    }catch(const std::bad_alloc&){return nullptr;}
}
extern "C" __declspec(dllexport) void* __cdecl midi_execution_create(void* queue,void* kernel,void* origins,void* timing,unsigned stream,unsigned mode,unsigned port){
    return midiExecutionCreate(queue,kernel,origins,timing,stream,mode,port,false);
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_multi_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl midi_execution_multi_create(void* kernel,void* origins,void* timing,unsigned stream,unsigned mode){
    return midiExecutionCreate(nullptr,kernel,origins,timing,stream,mode,0,true);
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_raw_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_file_schedule_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_file_transpose_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_file_chase_version(){return 1;}
// Control-side chase preparation. Reset and reconstruction run on the same
// actor and accepted fence as ordinary file events; no shared BASS CANCEL.
extern "C" __declspec(dllexport) int __cdecl midi_execution_file_chase_prepare(void* pointer,
        const MidiExecutionWorker::FileRow* rows,unsigned count,const unsigned char* bytes,unsigned size,
        unsigned index,double position,int transpose,const unsigned char* muted){
    if(!pointer||(count&&!rows)||(size&&!bytes)||count>2000000||size>64*1024*1024||!muted||!std::isfinite(position)||position<0||transpose<-24||transpose>24)return 0;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);if(!s.ingress||!s.directQueue)return 0;
    AcquireSRWLockShared(&s.kernel->lock);const bool ready=s.kernel->fileResetReady&&!s.kernel->compatibilityCallback;ReleaseSRWLockShared(&s.kernel->lock);if(!ready)return 0;
    try{
        std::vector<MidiExecutionWorker::FileRow> preparedRows;if(count)preparedRows.assign(rows,rows+count);
        std::vector<unsigned char> preparedBytes;if(size)preparedBytes.assign(bytes,bytes+size);
        std::vector<MidiExecutionWorker::FileTicket> preparedTickets;
        AcquireSRWLockShared(s.acceptanceLock());const auto originalCount=s.file?s.file->rows.size():0;ReleaseSRWLockShared(s.acceptanceLock());
        preparedTickets.reserve(std::max(originalCount,preparedRows.size()));
        for(const auto& row:preparedRows)if(row.seconds!=0||row.port>=8||row.reserved||row.offset>size||row.size>size-row.offset||row.size>MidiExecutionWorker::inputCapacity)return 0;
        const MidiExecutionWorker::ControlStep dummy{};auto reset=std::make_unique<MidiExecutionWorker::Control>(&dummy,1);reset->manualReset=true;reset->fileResetSource=29;reset->chaseReset=true;
        AcquireSRWLockExclusive(s.acceptanceLock());int accepted=0;
        if(s.file&&!s.file->playing&&!s.file->chasing&&!s.file->loopQueued&&!s.file->loopActive&&s.file->acked==s.file->tickets.size()&&s.file->releaseAcked&&index<=s.file->rows.size()&&position<=s.file->duration&&!s.stopping.load()&&s.acceptingControls.load()&&s.controlCount<s.controls.size()&&s.controlSerial!=UINT64_MAX){
            auto& plan=*s.file;plan.tickets.swap(preparedTickets);plan.chaseRows.swap(preparedRows);plan.chaseBytes.swap(preparedBytes);plan.chaseNext=0;plan.position=position;plan.next=index;plan.human.reset(index);plan.transpose=transpose;plan.complete=false;plan.chaseFailed=false;plan.chasing=true;plan.chasePrepared=true;plan.chaseReady=false;plan.chaseResume=false;plan.chaseResetQueued=true;plan.tickets.clear();plan.acked=0;plan.lastComplete=0;
            for(unsigned i=0;i<128;++i)plan.muted[i]=muted[i]!=0;LARGE_INTEGER stamp{};QueryPerformanceCounter(&stamp);reset->submitted=stamp.QuadPart;reset->ticket=++s.controlSerial;reset->fence=s.acceptedFence();if(!s.controlCount)s.controlFence.store(reset->fence);s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(reset);++s.controlCount;s.controlsPending.store(true);accepted=1;s.wakeIngress();
        }
        ReleaseSRWLockExclusive(s.acceptanceLock());return accepted;
    }catch(const std::bad_alloc&){return 0;}
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_file_loop_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_execution_file_loop_configure(void* pointer,unsigned enabled,unsigned source){
    if(!pointer||enabled>1||(source!=29&&source!=31))return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    AcquireSRWLockShared(&s.kernel->lock);const bool ready=s.kernel->fileResetReady&&s.kernel->gmChannels<=s.kernel->channels;
    ReleaseSRWLockShared(&s.kernel->lock);if(enabled&&!ready)return 0;
    try{
        std::unique_ptr<MidiExecutionWorker::Control> panic,reset;
        if(enabled){std::vector<MidiExecutionWorker::ControlStep> steps;steps.reserve(s.kernel->channels*3);
            for(unsigned ch=0;ch<s.kernel->channels;++ch)for(unsigned event:{MIDI_EVENT_SUSTAIN,MIDI_EVENT_SOSTENUTO,MIDI_EVENT_SOUNDOFF})steps.push_back({ch,event,0});
            panic=std::make_unique<MidiExecutionWorker::Control>(steps.data(),static_cast<unsigned>(steps.size()));
            panic->transportRelease=true;panic->continueOnError=true;panic->loopStage=1;
            const MidiExecutionWorker::ControlStep dummy{};reset=std::make_unique<MidiExecutionWorker::Control>(&dummy,1);
            reset->manualReset=true;reset->fileResetSource=source;reset->loopStage=2;
        }
        AcquireSRWLockExclusive(s.acceptanceLock());int accepted=0;
        if(s.file&&!s.file->playing&&!s.file->loopQueued&&!s.file->loopActive&&(s.file->chasePrepared||s.file->acked==s.file->tickets.size())&&s.file->releaseAcked){
            auto& plan=*s.file;plan.loopPanic.swap(panic);plan.loopReset.swap(reset);plan.looping=enabled!=0;plan.loopSource=source;
            plan.loopFailed=false;plan.loopPressure=false;plan.loopPhase=0;plan.loopError=0;plan.loopRestartAllowed=false;accepted=1;}
        ReleaseSRWLockExclusive(s.acceptanceLock());return accepted;
    }catch(const std::bad_alloc&){return 0;}
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_file_loop_snapshot(void* pointer,unsigned long long* values,unsigned capacity){
    if(!pointer||!values||capacity<6)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);AcquireSRWLockShared(s.acceptanceLock());
    if(!s.file){ReleaseSRWLockShared(s.acceptanceLock());return 0;}const auto& plan=*s.file;
    const unsigned long long result[]{plan.looping?1ull:0ull,plan.loopIterations,plan.loopQueued,plan.loopActive,plan.loopPhase,
        static_cast<unsigned long long>(static_cast<long long>(plan.loopError))};
    std::copy_n(result,6,values);ReleaseSRWLockShared(s.acceptanceLock());return 6;
}
// ABI2 adds explicit unexecuted-release state3. Old ABI1 facades decline
// native release instead of waiting forever on a status they cannot handle.
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_file_release_version(){return 2;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_file_release_failure_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_file_release_failure(void* pointer,unsigned* values){
    if(!pointer||!values)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    if(!s.ingress)return 0;AcquireSRWLockShared(s.acceptanceLock());
    if(!s.file){ReleaseSRWLockShared(s.acceptanceLock());return 0;}
    values[0]=s.file->releasePhase;values[1]=static_cast<unsigned>(s.file->releaseError);
    ReleaseSRWLockShared(s.acceptanceLock());return 2;
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_file_release_prepare(void* pointer,unsigned channels){
    if(!pointer||!channels)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    if(channels>s.kernel->channels)return 0;
    AcquireSRWLockShared(&s.kernel->lock);
    const bool matching=s.kernel->gmReady&&s.kernel->gmChannels==channels;
    ReleaseSRWLockShared(&s.kernel->lock);if(!matching)return 0;
    try{
        std::vector<MidiExecutionWorker::ControlStep> steps;steps.reserve(s.kernel->channels*3);
        for(unsigned channel=0;channel<s.kernel->channels;++channel)
            for(unsigned event:{MIDI_EVENT_SUSTAIN,MIDI_EVENT_SOSTENUTO,MIDI_EVENT_NOTESOFF})steps.push_back({channel,event,0});
        auto command=std::make_unique<MidiExecutionWorker::Control>(steps.data(),static_cast<unsigned>(steps.size()));
        command->transportRelease=true;command->continueOnError=true;
        AcquireSRWLockExclusive(s.acceptanceLock());unsigned long long ticket=0;
        if(s.file&&!s.file->playing&&!s.file->release&&s.file->releaseAcked&&
                !s.stopping.load()&&s.acceptingControls.load()&&s.controlSerial!=UINT64_MAX){
            ticket=++s.controlSerial;command->ticket=ticket;s.file->releaseTicket=ticket;
            LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;
            s.file->releaseQueued=false;s.file->releaseDone=false;s.file->releaseAcked=false;s.file->release=std::move(command);
            s.file->releaseFailed=false;s.file->releasePhase=0;s.file->releaseError=0;
        }
        ReleaseSRWLockExclusive(s.acceptanceLock());return ticket;
    }catch(const std::bad_alloc&){return 0;}
}
// op0 retires only an unexecuted prepared command; op1 ACKs copied completion.
extern "C" __declspec(dllexport) int __cdecl midi_execution_file_release_retire(void* pointer,unsigned long long ticket,unsigned op){
    if(!pointer||!ticket||op>1)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    AcquireSRWLockExclusive(s.acceptanceLock());int result=0;
    if(s.file&&s.file->releaseTicket==ticket){auto& file=*s.file;
        if(!op&&file.release&&!file.playing){file.release.reset();file.releaseTicket=0;file.releaseAcked=true;result=1;}
        else if(op&&file.releaseQueued&&file.releaseDone){file.releaseAcked=true;result=1;}
    }
    ReleaseSRWLockExclusive(s.acceptanceLock());return result;
}
// Additive qualification marker: successful canonical-only native file
// completions need no terminal ring receipt, but source/trace/error/Learn/
// Reset/handoff/semantic-change receipts remain mandatory and backpressure.
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_file_observer_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_execution_file_prepare(void* pointer,
        const MidiExecutionWorker::FileRow* rows,unsigned count,const unsigned char* bytes,unsigned size,double duration){
    if(!pointer||!rows||!count||count>2000000||!bytes||size>64*1024*1024||!std::isfinite(duration)||duration<0)return 0;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);if(!s.ingress||!s.directQueue||!s.kernel->selection)return 0;
    try{auto plan=std::make_unique<MidiExecutionWorker::FileSchedule>();
        plan->rows.assign(rows,rows+count);plan->bytes.assign(bytes,bytes+size);plan->tickets.reserve(count);plan->duration=duration;
        LARGE_INTEGER frequency{};if(!QueryPerformanceFrequency(&frequency)||frequency.QuadPart<=0)return 0;plan->frequency=frequency.QuadPart;
        double previous=0;for(const auto& row:plan->rows){
            if(!std::isfinite(row.seconds)||row.seconds<previous||row.seconds>duration||row.port>=8||row.reserved||
                row.offset>size||row.size>size-row.offset||row.size>MidiExecutionWorker::inputCapacity)return 0;previous=row.seconds;}
        plan->human.prepare(plan->rows,plan->bytes);plan->human.reset(0);
        AcquireSRWLockExclusive(s.acceptanceLock());AcquireSRWLockShared(&s.directQueue->lock);
        const bool ready=!s.stopping.load()&&s.acceptingControls.load()&&!s.directQueue->count&&!s.ingress->clients[8].metadata.load()&&
            s.inputClient.load()!=UINT64_MAX&&(!s.file||(!s.file->playing&&!s.file->loopQueued&&!s.file->loopActive&&s.file->acked==s.file->tickets.size()&&s.file->releaseAcked));
        ReleaseSRWLockShared(&s.directQueue->lock);if(ready)s.file=std::move(plan);ReleaseSRWLockExclusive(s.acceptanceLock());return ready?1:0;
    }catch(const std::bad_alloc&){return 0;}
}
// Live file-only mask: publish targeted voice clearing at the original FIFO
// fence, without pausing/reanchoring the schedule or waiting for metadata ACK.
// SoundOff16, Sustain15 and Sostenuto76 are per-channel bassmidi.h events.
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_file_mutes_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_execution_file_mutes(void* pointer,
        const unsigned char* muted,unsigned count,unsigned long long* ticket){
    if(ticket)*ticket=0;
    if(!pointer||!muted||count!=128||!ticket)return 0;
    for(unsigned i=0;i<count;++i)if(muted[i]>1)return 0;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    try{
        auto command=std::make_unique<MidiExecutionWorker::Control>();
        command->steps.reserve(s.kernel->channels*3);command->results.resize(s.kernel->channels*3);
        command->manualPanic=true;command->continueOnError=true;
        AcquireSRWLockExclusive(s.acceptanceLock());
        if(!s.file||s.stopping.load()||s.ended.load()||!s.acceptingControls.load()){
            ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
        auto& plan=*s.file;
        for(unsigned ch=0;ch<s.kernel->channels;++ch)if(muted[ch]&&!plan.muted[ch])
            for(unsigned event:{MIDI_EVENT_SUSTAIN,MIDI_EVENT_SOSTENUTO,MIDI_EVENT_SOUNDOFF})command->steps.push_back({ch,event,0});
        if(!command->steps.empty()){
            if(s.controlCount==s.controls.size()||s.controlSerial==UINT64_MAX){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
            command->ticket=++s.controlSerial;command->fence=s.acceptedFence();
            LARGE_INTEGER now{};QueryPerformanceCounter(&now);command->submitted=now.QuadPart;*ticket=command->ticket;
            if(!s.controlCount)s.controlFence.store(command->fence);
            s.controls[(s.controlHead+s.controlCount)%s.controls.size()]=std::move(command);++s.controlCount;s.controlsPending.store(true);
        }
        for(unsigned i=0;i<count;++i)plan.muted[i]=muted[i]!=0;
        s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return 1;
    }catch(const std::bad_alloc&){return 0;}
}
// Command0 pauses future admissions; already accepted RAW must drain unchanged.
// Command1 starts an already prepared/chased cursor. Command2 clears only after
// every published original receipt ACK; refusal preserves the owner for retry.
extern "C" __declspec(dllexport) int __cdecl midi_execution_file_command(void* pointer,unsigned command,
        unsigned index,double position,double speed,int transpose,const unsigned char* muted){
    if(!pointer||command>2||!std::isfinite(position)||!std::isfinite(speed)||speed<.25||speed>4||transpose< -24||transpose>24)return 0;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);if(!s.ingress)return 0;
    AcquireSRWLockExclusive(s.acceptanceLock());int accepted=0;
    if(s.file){auto& plan=*s.file;LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);
        if(command==0){plan.position=plan.at(tick.QuadPart);plan.playing=false;plan.chaseResume=false;plan.loopRestartAllowed=false;plan.loopPressure=false;accepted=1;}
        else if(command==2){if(!plan.playing&&!plan.chasing&&!plan.chaseResetQueued&&!plan.loopQueued&&!plan.loopActive&&plan.acked==plan.tickets.size()&&plan.releaseAcked){s.file.reset();accepted=1;}}
        else if(plan.chasePrepared&&!plan.chaseFailed&&muted&&index==plan.next&&position==plan.position&&transpose==plan.transpose){plan.speed=speed;plan.chaseResume=true;plan.chasePrepared=false;if(!plan.chasing){plan.playing=true;plan.anchor=tick.QuadPart;}accepted=1;}
        else if(!s.stopping.load()&&!s.ended.load()&&s.acceptingControls.load()&&s.ingress->accepting&&
                !plan.playing&&!plan.loopQueued&&!plan.loopActive&&(!plan.releaseQueued||plan.releaseAcked)&&plan.acked==plan.tickets.size()&&muted&&index<=plan.rows.size()&&position>=0&&position<=plan.duration){
            const bool continuing=plan.human.active&&position==plan.position&&index==plan.next;
            if(!continuing)plan.human.reset(index);
            else plan.human.reanchor(plan.rows,plan.bytes,position,speed,s.kernel->notes->humanization.preserves(),tick.QuadPart,plan.frequency);
            plan.position=position;plan.anchor=tick.QuadPart;plan.speed=speed;plan.transpose=transpose;plan.next=index;plan.complete=false;
            plan.tickets.clear();plan.acked=0;plan.lastComplete=0;plan.releaseQueued=false;plan.releaseDone=false;
            plan.releaseFailed=false;plan.releasePhase=0;plan.releaseError=0;plan.loopFailed=false;plan.loopRestartAllowed=plan.looping;plan.loopPressure=false;
            for(unsigned i=0;i<128;++i)plan.muted[i]=muted[i]!=0;plan.playing=true;accepted=1;}}
    if(accepted)s.wakeIngress();ReleaseSRWLockExclusive(s.acceptanceLock());return accepted;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_file_humanization_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_file_history(void* pointer,unsigned offset,unsigned* indices,unsigned capacity,unsigned* meta){
    if(!pointer||!indices||!meta||capacity>2048)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);if(!s.ingress)return 0;
    AcquireSRWLockShared(s.acceptanceLock());unsigned count=0;
    if(s.file){const auto& h=s.file->human;meta[0]=h.active;meta[1]=h.prefix;meta[2]=static_cast<unsigned>(h.history.size());
        for(;offset+count<h.history.size()&&count<capacity;++count)indices[count]=h.history[offset+count].index;}
    ReleaseSRWLockShared(s.acceptanceLock());return count;
}
extern "C" __declspec(dllexport) int __cdecl midi_execution_file_human_pause(void* pointer){
    if(!pointer)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);if(!s.ingress)return 0;
    AcquireSRWLockExclusive(s.acceptanceLock());bool ok=s.file&&!s.file->playing&&s.file->acked==s.file->tickets.size();
    if(ok){auto& p=*s.file;p.human.pause(p.rows,p.position);p.next=p.human.active?p.human.frontier:p.next;}
    ReleaseSRWLockExclusive(s.acceptanceLock());return ok?1:0;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_file_snapshot(void* pointer,double* position,unsigned long long* values){
    if(!pointer||!position||!values)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);if(!s.ingress)return 0;
    AcquireSRWLockShared(s.acceptanceLock());if(!s.file){ReleaseSRWLockShared(s.acceptanceLock());return 0;}
    const auto& plan=*s.file;LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);*position=plan.at(tick.QuadPart);
    values[0]=plan.next;values[1]=(plan.loopFailed||plan.chaseFailed)?3:(plan.playing||plan.chasing||plan.chaseResetQueued||plan.loopQueued||plan.loopActive)?1:plan.releaseFailed?3:plan.complete?2:0;
    values[2]=plan.tickets.size()-plan.acked+(plan.releaseQueued&&!plan.releaseAcked?1:0)+plan.loopQueued+(plan.chasing?1:0);
    ReleaseSRWLockShared(s.acceptanceLock());return 3;
}
// A successful lookup distinguishes native file publications from ordinary
// Python-owned direct RAW. The original frame is retained unchanged.
extern "C" __declspec(dllexport) int __cdecl midi_execution_file_receipt(void* pointer,unsigned long long publication,
        unsigned long long* values,unsigned char* bytes,unsigned capacity,unsigned ack){
    if(!pointer||!publication||!values||ack>1)return -1;auto& s=*static_cast<MidiExecutionWorker*>(pointer);if(!s.ingress)return 0;
    AcquireSRWLockExclusive(s.acceptanceLock());if(!s.file){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
    auto& plan=*s.file;auto found=std::lower_bound(plan.tickets.begin(),plan.tickets.end(),publication,
        [](const MidiExecutionWorker::FileTicket& row,unsigned long long ticket){return row.publication<ticket;});
    if(found==plan.tickets.end()||found->publication!=publication){ReleaseSRWLockExclusive(s.acceptanceLock());return 0;}
    const auto& row=plan.sourceRow(*found);
    values[0]=row.port;values[1]=row.size;values[2]=found->received;values[3]=found->row;
    if(ack){if(!found->acked){found->acked=true;++plan.acked;}}
    else if(!bytes||capacity<row.size){ReleaseSRWLockExclusive(s.acceptanceLock());return -2;}
    else{std::memcpy(bytes,plan.sourceBytes(*found),row.size);
        if(found->shiftedKey>=0&&row.size==3)bytes[1]=static_cast<unsigned char>(found->shiftedKey);}
    ReleaseSRWLockExclusive(s.acceptanceLock());return 1;
}
// Copied original RAW frame, the SAME ingress publication sequence/worker as
// WinMM. Admission is nonblocking: a full existing4MiB/4096-frame resource
// policy returns0 before copying or advancing any source/decoder state.
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_raw(void* pointer,const void* payload,unsigned length,
        unsigned port,unsigned long long received,long long trace,const unsigned long long* origin){
    if(!pointer||!payload||!length||length>MidiExecutionWorker::inputCapacity||port>=8||!received)return 0;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);if(!s.ingress||!s.directQueue)return 0;
    auto& session=*s.ingress;auto& q=*s.directQueue;
    AcquireSRWLockExclusive(&session.lock);AcquireSRWLockExclusive(&q.lock);
    if(s.stopping.load()||!session.accepting||q.stopping||q.count==MidiPipeQueue::frameCapacity||
        length>MidiPipeQueue::byteCapacity-q.used||session.readCount==UINT64_MAX){
        ReleaseSRWLockExclusive(&q.lock);ReleaseSRWLockExclusive(&session.lock);return 0;
    }
    MidiPipeQueue::Frame frame;frame.kind=origin?3u:2u;frame.size=length;frame.directPort=port;frame.read=received;frame.trace=trace;
    if(origin)std::copy_n(origin,8,frame.origin);
    const unsigned first=std::min(length,MidiPipeQueue::byteCapacity-q.byteTail);
    std::memcpy(q.storage.data()+q.byteTail,payload,first);
    if(first<length)std::memcpy(q.storage.data(),static_cast<const unsigned char*>(payload)+first,length-first);
    q.byteTail=(q.byteTail+length)%MidiPipeQueue::byteCapacity;q.used+=length;
    frame.publication=++session.readCount;++q.readCount;q.totalBytes+=length;q.frames[(q.head+q.count)%MidiPipeQueue::frameCapacity]=frame;++q.count;
    q.peakFrames=std::max(q.peakFrames,static_cast<unsigned long long>(q.count));q.peakBytes=std::max(q.peakBytes,static_cast<unsigned long long>(q.used));
    SetEvent(session.ready);WakeAllConditionVariable(&q.changed);
    ReleaseSRWLockExclusive(&q.lock);ReleaseSRWLockExclusive(&session.lock);return frame.publication;
}
extern "C" __declspec(dllexport) int __cdecl midi_execution_raw_pending(void* pointer){
    return pointer&&static_cast<MidiExecutionWorker*>(pointer)->directPending()?1:0;
}
extern "C" __declspec(dllexport) unsigned long long __cdecl midi_execution_attach(void* pointer,void* queue,unsigned port){
    if(!pointer||!queue||port>=8)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);auto* q=static_cast<MidiPipeQueue*>(queue);
    if(!s.ingress)return 0;auto& session=*s.ingress;AcquireSRWLockExclusive(&session.lock);AcquireSRWLockExclusive(&q->lock);
    unsigned slot=8;for(unsigned i=0;i<8;++i)if(!session.clients[i].queue){slot=i;break;}
    if(s.stopping.load()||!session.accepting||q->consumer||q->activePops||q->stopping||q->session||slot==8||
       session.clientSerial>=UINT64_MAX-1||q->count>UINT64_MAX-session.readCount){ReleaseSRWLockExclusive(&q->lock);ReleaseSRWLockExclusive(&session.lock);return 0;}
    const auto id=++session.clientSerial;auto& client=session.clients[slot];client.queue=q;client.id=id;client.port=port;client.metadata.store(0);
    for(unsigned i=0;i<q->count;++i)q->frames[(q->head+i)%MidiPipeQueue::frameCapacity].publication=++session.readCount;
    q->consumer=&s;q->session=&session;session.retain();SetEvent(session.ready);
    ReleaseSRWLockExclusive(&q->lock);ReleaseSRWLockExclusive(&session.lock);return id;
}
extern "C" __declspec(dllexport) int __cdecl midi_execution_client_snapshot(void* pointer,unsigned long long id,unsigned long long* values,unsigned capacity){
    if(!pointer||!id||!values||capacity<8)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);if(!s.ingress)return 0;
    AcquireSRWLockExclusive(&s.ingress->lock);
    for(auto& client:s.ingress->clients)if(client.queue&&client.id==id){auto* q=client.queue;AcquireSRWLockExclusive(&q->lock);
        const unsigned long long fields[]{id,q->count,q->ended?1ull:0ull,q->stopping?1ull:0ull,client.metadata.load(),s.inputClient.load()==id?1ull:0ull,
            q->readCount,static_cast<unsigned long long>(static_cast<long long>(q->terminal))};std::copy_n(fields,8,values);
        ReleaseSRWLockExclusive(&q->lock);ReleaseSRWLockExclusive(&s.ingress->lock);return 1;}
    ReleaseSRWLockExclusive(&s.ingress->lock);return 0;
}
extern "C" __declspec(dllexport) int __cdecl midi_execution_detach(void* pointer,unsigned long long id){
    if(!pointer||!id||id==UINT64_MAX)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);if(!s.ingress)return 0;
    AcquireSRWLockExclusive(&s.ingress->lock);
    for(auto& client:s.ingress->clients)if(client.queue&&client.id==id){auto* q=client.queue;AcquireSRWLockExclusive(&q->lock);
        if(q->count||(!q->ended&&!q->stopping)||client.metadata.load()||s.inputClient.load()==id){ReleaseSRWLockExclusive(&q->lock);ReleaseSRWLockExclusive(&s.ingress->lock);return 0;}
        q->consumer=nullptr;q->session=nullptr;client.queue=nullptr;ReleaseSRWLockExclusive(&q->lock);
        SetEvent(s.ingress->ready);ReleaseSRWLockExclusive(&s.ingress->lock);s.ingress->release();return 1;}
    ReleaseSRWLockExclusive(&s.ingress->lock);return 0;
}
extern "C" __declspec(dllexport) int __cdecl midi_execution_start(void* pointer){
    if(!pointer)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    if(s.started.exchange(true))return 0;return ResumeThread(s.thread)!=static_cast<DWORD>(-1);
}
extern "C" __declspec(dllexport) int __cdecl midi_execution_read(void* pointer,unsigned long long* descriptor,unsigned fields,
        MidiNoteAction* notes,unsigned capacity,unsigned char* payload,unsigned bytes){
    if(!pointer||!descriptor||fields<32||!notes||!payload)return -1;auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    AcquireSRWLockExclusive(&s.lock);
    if(s.metadataBatchCount){ReleaseSRWLockExclusive(&s.lock);return -3;}
    while(s.retentionEnabled?(!s.retainedHead&&!s.traceFirst()&&!s.retentionEnded):(!s.count&&!s.traceCount&&!s.ended))
        SleepConditionVariableSRW(&s.changed,&s.lock,INFINITE,0);
    if(s.traceFirst()){
        auto& pair=s.tracePairs[s.traceHead];
        if(bytes<pair.size){ReleaseSRWLockExclusive(&s.lock);return -2;}
        const auto& values=pair.beginRead?pair.end:pair.begin;
        std::copy_n(values.begin(),std::min(fields,34u),descriptor);std::copy_n(pair.prefix.data(),pair.size,payload);
        s.activityReadTicket=values[0];s.activityReadApplied=false;++s.traceRead;
        if(pair.beginRead){s.traceHead=(s.traceHead+1)%MidiExecutionWorker::traceCapacity;--s.traceCount;}
        else pair.beginRead=true;
        ReleaseSRWLockExclusive(&s.lock);return 1;
    }
    if(s.retentionEnabled){
        if(!s.retainedHead){ReleaseSRWLockExclusive(&s.lock);return 0;}
        const auto& record=s.retainedHead->record;const auto& values=record.values;
        const bool handoff=values[1]==2||values[1]==3||values[1]==5||values[1]==6||values[1]==8||values[1]==10||values[1]==11;
        if(record.noteBytes/sizeof(MidiNoteAction)>capacity||bytes<record.payloadBytes){ReleaseSRWLockExclusive(&s.lock);return -2;}
        std::copy(values.begin(),values.end(),descriptor);if(fields>=34){descriptor[32]=record.client;descriptor[33]=record.publication;}
        s.activityReadTicket=values[0];s.activityReadApplied=record.activityApplied;
        if(record.noteBytes)std::memcpy(notes,s.retainedHead->bytes.data(),record.noteBytes);
        if(record.payloadBytes)std::memcpy(payload,s.retainedHead->bytes.data()+record.noteBytes,record.payloadBytes);
        if(s.ingress&&record.client)s.ingress->clients[record.clientSlot].metadata.fetch_sub(1);
        if(handoff&&s.pendingTicket==values[0])s.delivered=true;
        s.retainedBytes-=record.noteBytes+record.payloadBytes;--s.retainedCount;++s.metrics[5];
        auto consumed=std::move(s.retainedHead);s.retainedHead=std::move(consumed->next);if(!s.retainedHead)s.retainedTail=nullptr;
        WakeAllConditionVariable(&s.changed);ReleaseSRWLockExclusive(&s.lock);return 1;
    }
    if(!s.count){ReleaseSRWLockExclusive(&s.lock);return 0;}
    const auto& record=s.records[s.head];const auto& values=record.values;
    const bool handoff=values[1]==2||values[1]==3||values[1]==5||values[1]==6||values[1]==8||values[1]==10||values[1]==11;
    if(record.noteBytes/sizeof(MidiNoteAction)>capacity||bytes<record.payloadBytes){ReleaseSRWLockExclusive(&s.lock);return -2;}
    std::copy(values.begin(),values.end(),descriptor);
    if(fields>=34){descriptor[32]=record.client;descriptor[33]=record.publication;}
    s.activityReadTicket=record.values[0];s.activityReadApplied=record.activityApplied;
    auto copy=[&](unsigned char* output,unsigned size){
        const unsigned first=std::min(size,MidiExecutionWorker::noteCapacity-s.noteHead);std::memcpy(output,s.noteStorage.data()+s.noteHead,first);
        if(first<size)std::memcpy(output+first,s.noteStorage.data(),size-first);s.noteHead=(s.noteHead+size)%MidiExecutionWorker::noteCapacity;
    };
    copy(reinterpret_cast<unsigned char*>(notes),record.noteBytes);copy(payload,record.payloadBytes);
    s.noteUsed-=record.noteBytes+record.payloadBytes;
    if(s.ingress&&record.client)s.ingress->clients[record.clientSlot].metadata.fetch_sub(1);
    if(handoff&&s.pendingTicket==values[0])s.delivered=true;
    s.head=(s.head+1)%MidiExecutionWorker::recordCapacity;--s.count;++s.metrics[5];
    WakeAllConditionVariable(&s.changed);ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_read_batch_version(){return 1;}
// Activity receipt is side metadata. Descriptor[6..13] is original MID3
// origin data: never put flags in that range or alter any old ABI stride.
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_activity_receipt_version(){return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_execution_activity_receipt(void* pointer,unsigned long long ticket){
    if(!pointer||!ticket)return -1;auto& s=*static_cast<MidiExecutionWorker*>(pointer);AcquireSRWLockShared(&s.lock);
    int result=-1;if(s.activityReadTicket==ticket)result=s.activityReadApplied?1:0;
    else for(unsigned i=0;i<s.metadataBatchCount;++i)if(s.metadataBatch[i].ticket==ticket){result=s.metadataBatch[i].activityApplied?1:0;break;}
    ReleaseSRWLockShared(&s.lock);return result;
}
// rows:35 uint64s (original ABI34 + packed-note offset). Only disposition1,
// payload-free completions qualify. A barrier/large record remains untouched
// and returns-2 for the original single-record reader.0 is confirmed EOF.
extern "C" __declspec(dllexport) int __cdecl midi_execution_read_notes_batch(void* pointer,
        unsigned long long* descriptors,unsigned records,unsigned fields,MidiNoteAction* notes,unsigned capacity){
    if(!pointer||!descriptors||!notes||!records||records>64||fields!=35)return -1;
    auto& s=*static_cast<MidiExecutionWorker*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(s.metadataBatchCount){ReleaseSRWLockExclusive(&s.lock);return -3;}
    // The retained path uses exact single-owner reads. A future batch extension
    // must retain all original lease/commit frontiers, not re-enable ring reads.
    if(s.retentionEnabled){ReleaseSRWLockExclusive(&s.lock);return -2;}
    while(!s.count&&!s.traceCount&&!s.ended)SleepConditionVariableSRW(&s.changed,&s.lock,INFINITE,0);
    if(s.traceFirst()){ReleaseSRWLockExclusive(&s.lock);return -2;}
    if(!s.count){ReleaseSRWLockExclusive(&s.lock);return 0;}
    unsigned used=0,read=0;
    while(s.count&&read<records){
        if(s.traceFirst())break;
        const auto& record=s.records[s.head];const auto& values=record.values;
        const unsigned actions=record.noteBytes/sizeof(MidiNoteAction);
        if(values[1]!=1||record.payloadBytes||actions>capacity-used)break;
        auto* output=descriptors+read*fields;std::copy(values.begin(),values.end(),output);
        output[32]=record.client;output[33]=record.publication;output[34]=used;
        const unsigned first=std::min(record.noteBytes,MidiExecutionWorker::noteCapacity-s.noteHead);
        auto* destination=reinterpret_cast<unsigned char*>(notes+used);
        std::memcpy(destination,s.noteStorage.data()+s.noteHead,first);
        if(first<record.noteBytes)std::memcpy(destination+first,s.noteStorage.data(),record.noteBytes-first);
        s.noteHead=(s.noteHead+record.noteBytes)%MidiExecutionWorker::noteCapacity;
        s.noteUsed-=record.noteBytes;used+=actions;
        s.metadataBatch[read]={values[0],record.client,record.clientSlot,record.activityApplied};
        s.head=(s.head+1)%MidiExecutionWorker::recordCapacity;--s.count;++s.metrics[5];++read;
    }
    if(read){s.metadataBatchCount=read;++s.metadataBatchReads;s.metadataBatchRecords+=read;WakeAllConditionVariable(&s.changed);}
    ReleaseSRWLockExclusive(&s.lock);return read?static_cast<int>(read):-2;
}
extern "C" __declspec(dllexport) int __cdecl midi_execution_metadata_batch_ack(void* pointer,unsigned long long last,unsigned count){
    if(!pointer||!count)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);AcquireSRWLockExclusive(&s.lock);
    if(count!=s.metadataBatchCount||s.metadataBatch[count-1].ticket!=last){ReleaseSRWLockExclusive(&s.lock);return 0;}
    for(unsigned index=0;index<count;++index){const auto& owner=s.metadataBatch[index];
        if(s.ingress&&owner.client)s.ingress->clients[owner.slot].metadata.fetch_sub(1);}
    s.metadataBatchCount=0;++s.metadataBatchAcks;WakeAllConditionVariable(&s.changed);
    ReleaseSRWLockExclusive(&s.lock);return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_metadata_batch_snapshot(void* pointer,unsigned long long* values,unsigned count){
    if(!pointer||!values||count<4)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);AcquireSRWLockShared(&s.lock);
    values[0]=s.metadataBatchReads;values[1]=s.metadataBatchRecords;values[2]=s.metadataBatchAcks;values[3]=s.metadataBatchCount;
    ReleaseSRWLockShared(&s.lock);return 4;
}
extern "C" __declspec(dllexport) int __cdecl midi_execution_resume(void* pointer,unsigned long long ticket,unsigned action){
    if(!pointer||!ticket||action<1||action>3)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    AcquireSRWLockExclusive(&s.lock);
    // A later frame's pending unlock can block aggregate replay of an earlier
    // FIFO Note record. Permit the control-only same-thread retry BEFORE that
    // notification is read, without reordering musical metadata or handoffs.
    const bool valid=s.paused&&(s.delivered||((s.pendingKind==3||s.pendingKind==5)&&action==2))&&s.pendingTicket==ticket&&!s.resume&&
        (((s.pendingKind==3||s.pendingKind==5)&&action==2)||(s.pendingKind==2&&(action==1||action==3))||
         ((s.pendingKind==6||s.pendingKind==8)&&(action==2||action==3))||((s.pendingKind==10||s.pendingKind==11)&&action==2));
    if(valid){s.resume=action;WakeAllConditionVariable(&s.changed);}ReleaseSRWLockExclusive(&s.lock);return valid?1:0;
}
extern "C" __declspec(dllexport) void __cdecl midi_execution_stop(void* pointer){if(pointer)static_cast<MidiExecutionWorker*>(pointer)->stop();}
extern "C" __declspec(dllexport) unsigned __cdecl midi_execution_snapshot(void* pointer,unsigned long long* values,unsigned capacity){
    if(!pointer||!values||capacity<26)return 0;auto& s=*static_cast<MidiExecutionWorker*>(pointer);
    AcquireSRWLockExclusive(&s.lock);std::copy(s.metrics.begin(),s.metrics.end(),values);
    values[22]=s.count+s.retainedCount;values[23]=s.noteUsed+s.retainedBytes;values[24]=s.paused;values[25]=s.pendingTicket;ReleaseSRWLockExclusive(&s.lock);return 26;
}
extern "C" __declspec(dllexport) int __cdecl midi_execution_release(void* pointer){
    if(!pointer)return 1;auto* s=static_cast<MidiExecutionWorker*>(pointer);s->stop();if(s->directPending())return 0;
    if(WaitForSingleObject(s->thread,2000)!=WAIT_OBJECT_0)return 0;
    AcquireSRWLockExclusive(&s->lock);const bool busy=s->count||s->retainedCount||s->traceCount||s->paused||s->metadataBatchCount;
    ReleaseSRWLockExclusive(&s->lock);if(busy)return 0;
    if(s->retentionThread&&WaitForSingleObject(s->retentionThread,2000)!=WAIT_OBJECT_0)return 0;
    AcquireSRWLockExclusive(&s->kernel->lock);s->kernel->executionOwner=nullptr;s->kernel->executionStop=nullptr;s->kernel->idle=nullptr;s->kernel->executionThread.store(0);
    AcquireSRWLockExclusive(&s->kernel->notes->lock);s->kernel->notes->executionOwner=nullptr;s->kernel->notes->executionPaused=false;ReleaseSRWLockExclusive(&s->kernel->notes->lock);
    ReleaseSRWLockExclusive(&s->kernel->lock);
    if(s->ingress){
        AcquireSRWLockExclusive(&s->ingress->lock);
        for(auto& client:s->ingress->clients)if(client.queue){auto* q=client.queue;q->requestStop();AcquireSRWLockExclusive(&q->lock);
            q->consumer=nullptr;q->session=nullptr;client.queue=nullptr;ReleaseSRWLockExclusive(&q->lock);s->ingress->release();}
        ReleaseSRWLockExclusive(&s->ingress->lock);
    }else{AcquireSRWLockExclusive(&s->queue->lock);s->queue->consumer=nullptr;ReleaseSRWLockExclusive(&s->queue->lock);}
    CloseHandle(s->thread);delete s;return 1;
}
static void CALLBACK playbackStall(HSYNC,DWORD,DWORD data,void* pointer){if(pointer&&!data)static_cast<PlaybackTiming*>(pointer)->stalls.fetch_add(1);}
static void CALLBACK playbackFree(HSYNC,DWORD,DWORD,void* pointer){if(pointer)static_cast<PlaybackTiming*>(pointer)->freed.store(true,std::memory_order_release);}
static void CALLBACK playbackPcm(HDSP,DWORD,void* buffer,DWORD bytes,void* pointer){if(pointer&&buffer)static_cast<PlaybackTiming*>(pointer)->pcm(bytes);}
static void CALLBACK playbackBegin(HDSP,DWORD channel,void* buffer,DWORD bytes,void* pointer){if(pointer&&buffer){LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);auto& owner=*static_cast<PlaybackTiming*>(pointer);owner.fxBegin=tick.QuadPart;if(owner.phase)owner.phase->add(3,channel,0,0,0,0,0,bytes,owner.blocks.load()+1);}}
extern "C" __declspec(dllexport) void* __cdecl playback_timing_proc(unsigned kind){
    return kind==0?reinterpret_cast<void*>(&playbackNote):kind==1?reinterpret_cast<void*>(&playbackPcm):
        kind==2?reinterpret_cast<void*>(&playbackStall):kind==3?reinterpret_cast<void*>(&playbackFree):reinterpret_cast<void*>(&playbackBegin);
}
extern "C" __declspec(dllexport) int __cdecl playback_timing_freed(void* pointer){return pointer&&static_cast<PlaybackTiming*>(pointer)->freed.load();}
static unsigned playbackTimingSnapshot(void* pointer,unsigned long long* out,unsigned capacity,unsigned long long* meta,unsigned width){
    if(!pointer||!out||!meta)return 0;auto& s=*static_cast<PlaybackTiming*>(pointer);
    LARGE_INTEGER now{};QueryPerformanceCounter(&now);const auto cutoff=static_cast<unsigned long long>(now.QuadPart)-s.frequency;
    const auto end=s.admitted.load(std::memory_order_acquire),begin=end>8192?end-8192:0;
    unsigned count=0;
    unsigned long long prior[15]{};bool havePrior=false;
    for(auto ticket=begin;ticket<end&&count<capacity;++ticket){
        auto& row=s.records[ticket%8192];const auto expected=2*(ticket+1);
        if(row.stamp.load(std::memory_order_acquire)!=expected)continue;
        const auto ready=row.values[6].load(std::memory_order_acquire);
        if(!ready)continue;
        unsigned long long values[15]{};for(unsigned i=0;i<width;++i)values[i]=row.values[i].load(std::memory_order_relaxed);
        if(row.stamp.load(std::memory_order_acquire)!=expected)continue;
        // Extended consumer needs independent old references for every producer.
        // Return the bounded history; Python selects the exact1s interval.
        if(width==9&&ready<cutoff){std::copy_n(values,9,prior);havePrior=true;continue;}
        // Attribute variation to the new note in1s; keep one older reference.
        if(!count&&havePrior&&capacity>1){std::copy_n(prior,9,out);++count;}
        if(count>=capacity)break;
        std::copy_n(values,width,out+width*count);++count;
    }
    meta[0]=s.frequency;meta[1]=end;meta[2]=s.omitted.load();meta[3]=s.stalls.load();
    meta[4]=s.fxLate.load();meta[5]=s.blocks.load();return count;
}
extern "C" __declspec(dllexport) unsigned __cdecl playback_timing_snapshot(void* pointer,unsigned long long* out,unsigned capacity,unsigned long long* meta){
    return playbackTimingSnapshot(pointer,out,capacity,meta,9);
}
extern "C" __declspec(dllexport) unsigned __cdecl playback_timing_humanized_version(){return 1;}
static unsigned playbackTimingOriginSnapshot(void* pointer,unsigned long long* out,unsigned capacity,unsigned long long* meta,unsigned width){
    const auto count=playbackTimingSnapshot(pointer,out,capacity,meta,width);
    if(pointer&&meta){auto& s=*static_cast<PlaybackTiming*>(pointer);meta[6]=s.missingOrigin.load();meta[7]=s.transportLoss.load();meta[8]=s.invalidOrigin.load();
        // Read-only interlocked state would require a writeable mapping. This
        // driver publishes an aligned64-bit atomic LONG64; x64 atomic acquire
        // load is a single read with no write/lock/RMW to FILE_MAP_READ memory.
        AcquireSRWLockShared(&s.originLock);
        for(auto& origin:s.origins){
            const auto loss=static_cast<unsigned long long>(origin.data->losses);
            if(loss)meta[7]+=loss;
        }
        ReleaseSRWLockShared(&s.originLock);
    }
    return count;
}

extern "C" __declspec(dllexport) unsigned __cdecl playback_timing_snapshot_origin(void* pointer,unsigned long long* out,unsigned capacity,unsigned long long* meta){
    return playbackTimingOriginSnapshot(pointer,out,capacity,meta,14);
}
extern "C" __declspec(dllexport) unsigned __cdecl playback_timing_snapshot_humanized(void* pointer,unsigned long long* out,unsigned capacity,unsigned long long* meta){
    return playbackTimingOriginSnapshot(pointer,out,capacity,meta,15);
}

// Opt-in processed MIDI timestamp. BASS_SYNC_MIDI_EVENT|MIXTIME calls this at
// native event processing, not when a Python producer returns or paints UI.
// No Python/GIL, heap work, audio mutation or waiting in this callback. The
// control owner must remove the sync under ChannelLock before destroying data.
struct MidiProcessedProbe {
    struct Sample {
        std::atomic<unsigned long long> stamp{0};
        std::array<std::atomic<unsigned long long>,5> values{};
    };
    unsigned long long frequency=0;
    std::atomic<unsigned long long> admitted{0};
    std::array<Sample,4096> samples{};
    MidiProcessedProbe() {
        LARGE_INTEGER value{};QueryPerformanceFrequency(&value);frequency=value.QuadPart;
    }
};
static void CALLBACK midiProcessedSync(HSYNC,DWORD channel,DWORD data,void* user) {
    if(!user)return;
    auto& state=*static_cast<MidiProcessedProbe*>(user);
    const auto ticket=state.admitted.fetch_add(1,std::memory_order_relaxed);
    auto& record=state.samples[ticket%4096];
    record.stamp.store(2*(ticket+1)+1,std::memory_order_release);
    LARGE_INTEGER value{};QueryPerformanceCounter(&value);
    const unsigned long long values[5]{ticket+1,static_cast<unsigned long long>(value.QuadPart),
        channel,data,GetCurrentThreadId()};
    for(unsigned i=0;i<5;++i)record.values[i].store(values[i],std::memory_order_relaxed);
    record.stamp.store(2*(ticket+1),std::memory_order_release);
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_processed_probe_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl midi_processed_probe_create(){return new(std::nothrow)MidiProcessedProbe;}
extern "C" __declspec(dllexport) void __cdecl midi_processed_probe_destroy(void* pointer){delete static_cast<MidiProcessedProbe*>(pointer);}
extern "C" __declspec(dllexport) void* __cdecl midi_processed_probe_proc(){return reinterpret_cast<void*>(&midiProcessedSync);}
extern "C" __declspec(dllexport) unsigned __cdecl midi_processed_probe_snapshot(void* pointer,unsigned long long* output,
        unsigned capacity,unsigned long long* frequency,unsigned long long* total) {
    if(!pointer||!output||!frequency||!total)return 0;
    auto& state=*static_cast<MidiProcessedProbe*>(pointer);
    const auto end=state.admitted.load(std::memory_order_acquire);
    const auto start=end>4096?end-4096:0;
    unsigned copied=0;
    for(auto ticket=start;ticket<end&&copied<capacity;++ticket){
        auto& record=state.samples[ticket%4096];
        const auto expected=2*(ticket+1);
        if(record.stamp.load(std::memory_order_acquire)!=expected)continue;
        unsigned long long values[5]{};
        for(unsigned i=0;i<5;++i)values[i]=record.values[i].load(std::memory_order_relaxed);
        if(record.stamp.load(std::memory_order_acquire)!=expected)continue;
        std::copy_n(values,5,output+5*copied);++copied;
    }
    *frequency=state.frequency;*total=end;return copied;
}

// Private instrument audition is mixed before the existing output effects.
// The decoder/font are prepared off-thread. No Python callback,allocation,
// file access or GUI lock occurs in this DSP. Detach under the parent channel
// lock before releasing this immutable source owner (bass.h DSPPROC ABI).
struct InstrumentAuditionMix {
    DWORD source;
    DWORD(WINAPI* getData)(DWORD,void*,DWORD);
    std::array<float,4096> scratch{};
    std::atomic<unsigned> failures{0};
    std::atomic<uint64_t> renderedFrames{0},silentFrames{0};
    std::vector<BASS_MIDI_EVENT> sequence;
    DWORD(WINAPI* sendEvents)(DWORD,DWORD,const void*,DWORD)=nullptr;
    uint64_t cursor=0,cycle=0,period=0;
    size_t eventIndex=0;
    InstrumentAuditionMix(DWORD stream,void* function):source(stream),getData(reinterpret_cast<DWORD(WINAPI*)(DWORD,void*,DWORD)>(function)){}
};
static void CALLBACK instrumentAuditionDSP(HDSP,DWORD,void* buffer,DWORD length,void* user){
    if(!user||!buffer||length%8)return;
    auto& owner=*static_cast<InstrumentAuditionMix*>(user);auto* target=static_cast<float*>(buffer);
    unsigned remaining=length/sizeof(float);
    while(remaining){
        const auto amount=std::min<unsigned>(remaining,static_cast<unsigned>(owner.scratch.size()));
        const auto bytes=amount*static_cast<unsigned>(sizeof(float));
        // Immutable control-prepared phrase. Schedule sample offsets before
        // decoding this block;no loop seek/reset,Python timer or allocation.
        if(owner.period){
            const uint64_t end=owner.cursor+amount/2;
            while(owner.cycle+owner.sequence[owner.eventIndex].pos/8<end){
                auto event=owner.sequence[owner.eventIndex];
                const uint64_t due=owner.cycle+event.pos/8;
                event.pos=static_cast<DWORD>((due-owner.cursor)*8);
                if(owner.sendEvents(owner.source,BASS_MIDI_EVENTS_TIME|BASS_MIDI_EVENTS_SYNC,&event,1)!=1){
                    owner.failures.fetch_add(1,std::memory_order_relaxed);return;
                }
                if(++owner.eventIndex==owner.sequence.size()){
                    owner.eventIndex=0;owner.cycle+=owner.period;
                }
            }
            owner.cursor=end;
        }
        const DWORD read=owner.getData(owner.source,owner.scratch.data(),bytes);
        if(read==static_cast<DWORD>(-1)||read>bytes||read%8){owner.failures.fetch_add(1,std::memory_order_relaxed);return;}
        for(unsigned i=0;i<read/sizeof(float);++i)target[i]+=owner.scratch[i];
        if(read!=bytes){owner.failures.fetch_add(1,std::memory_order_relaxed);return;}
        // Passive completion observation. Never consume a second decoder block.
        bool silent=true;
        for(unsigned i=0;i<amount;++i)if(std::abs(owner.scratch[i])>0.000001f){silent=false;break;}
        owner.silentFrames.store(silent?owner.silentFrames.load(std::memory_order_relaxed)+amount/2:0,std::memory_order_relaxed);
        owner.renderedFrames.fetch_add(amount/2,std::memory_order_relaxed);
        target+=amount;remaining-=amount;
    }
}
extern "C" __declspec(dllexport) unsigned __cdecl instrument_audition_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl instrument_audition_create(unsigned source,void* getData){
    if(!source||!getData)return nullptr;return new(std::nothrow)InstrumentAuditionMix(source,getData);
}
extern "C" __declspec(dllexport) void* __cdecl instrument_audition_proc(){return reinterpret_cast<void*>(&instrumentAuditionDSP);}
extern "C" __declspec(dllexport) unsigned __cdecl instrument_audition_sequence_version(){return 2;}
extern "C" __declspec(dllexport) int __cdecl instrument_audition_sequence(void* pointer,const BASS_MIDI_EVENT* events,
    unsigned count,uint64_t period,void* sendEvents){
    if(!pointer||!events||!count||count>16384||!period||!sendEvents)return 0;
    auto& owner=*static_cast<InstrumentAuditionMix*>(pointer);
    if(owner.cursor||owner.period)return 0; // Control-side,before attaching DSP only.
    for(unsigned i=0;i<count;++i){
        if(events[i].event!=MIDI_EVENT_NOTE||events[i].chan>=16||events[i].pos%8||events[i].pos/8>=period||
           (i&&events[i].pos<events[i-1].pos))return 0;
    }
    try{owner.sequence.assign(events,events+count);}catch(...){return 0;}
    owner.period=period;owner.sendEvents=reinterpret_cast<DWORD(WINAPI*)(DWORD,DWORD,const void*,DWORD)>(sendEvents);
    return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl instrument_audition_failures(void* pointer){
    return pointer?static_cast<InstrumentAuditionMix*>(pointer)->failures.load(std::memory_order_relaxed):0;
}
extern "C" __declspec(dllexport) int __cdecl instrument_audition_progress(void* pointer,uint64_t* frames,uint64_t* silent){
    if(!pointer||!frames||!silent)return 0;
    const auto& owner=*static_cast<InstrumentAuditionMix*>(pointer);
    *frames=owner.renderedFrames.load(std::memory_order_relaxed);
    *silent=owner.silentFrames.load(std::memory_order_relaxed);return 1;
}
extern "C" __declspec(dllexport) void __cdecl instrument_audition_destroy(void* pointer){delete static_cast<InstrumentAuditionMix*>(pointer);}

#include "midi_ports.h"
