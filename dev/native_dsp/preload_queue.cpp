// Explicit candidate SoundFont I/O queue. This TU is not a DSP callback.
// It owns its native worker and copied immutable source targets, but BORROWS
// BASS/BASSMIDI function addresses and SoundFont handles. Their DLLs/handles
// must remain alive until cancel + join + borrower release + destroy succeeds.
// No Python callback, GUI, MIDI event, sound mapping, or INI operation occurs.
#define NOMINMAX
#include <windows.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <new>
#include <thread>
#include <vector>
#include "../../bassmidi.h"
#include "preload_queue_api.h"

using FontLoadAddress = BOOL(WINAPI*)(HSOUNDFONT, int, int);
using FontLoadExAddress = BOOL(WINAPI*)(HSOUNDFONT, int, int, DWORD, DWORD);
using ErrorAddress = int(WINAPI*)();
using ConfigAddress = DWORD(WINAPI*)(DWORD);

// ABI v1: method=0 calls FontLoad (full); method=1 calls FontLoadEx.
// flags allow NOWAIT/TIME only. COMPACT/NOLOAD/KEEPDEC change policy and
// are not admitted by this candidate. length=0 is full, >0 is partial.
struct FontPreloadQueueTarget {
    DWORD font;
    int preset;
    int bank;
    DWORD length;
    DWORD flags;
    DWORD method;
};
static_assert(sizeof(FontPreloadQueueTarget) == 24, "preload target ABI");
struct FontPreloadQueueResult {
    DWORD state = 0; // 0 unknown,1 queued,2 running,3 full-ready,4 partial-ready,5 failed,6 cancelled
    DWORD error = 0;
    std::uint64_t serial = 0;
    std::uint64_t beginQpc = 0;
    std::uint64_t endQpc = 0;
};
static_assert(sizeof(FontPreloadQueueResult) == 32, "preload result ABI");
static std::atomic<unsigned> fontPreloadQueueLive{0};

struct FontPreloadQueue {
    const std::uint64_t epoch;
    const std::vector<FontPreloadQueueTarget> targets;
    std::vector<FontPreloadQueueResult> results;
    std::vector<unsigned> pending;
    unsigned head = 0, tail = 0, pendingCount = 0;
    bool active = false, paused = false, stopping = false, finished = false, joined = false;
    std::uint64_t accepted = 0, completed = 0, failed = 0, cancelled = 0;
    std::atomic<unsigned> references{1};
    FontLoadAddress load;
    FontLoadExAddress loadEx;
    ErrorAddress error;
    ConfigAddress config;
    std::mutex mutex;
    std::condition_variable changed;
    std::thread worker;

    FontPreloadQueue(std::uint64_t generation, const FontPreloadQueueTarget* rows, unsigned count,
                     unsigned capacity, FontLoadAddress full, FontLoadExAddress extended,
                     ErrorAddress getError, ConfigAddress getConfig, bool gate)
        : epoch(generation), targets(rows, rows + count), results(count), pending(capacity),
          paused(gate), load(full), loadEx(extended), error(getError), config(getConfig) {
        worker = std::thread([this] { run(); });
        fontPreloadQueueLive.fetch_add(1, std::memory_order_relaxed);
    }
    ~FontPreloadQueue() { fontPreloadQueueLive.fetch_sub(1, std::memory_order_relaxed); }
    static std::uint64_t qpc() {
        LARGE_INTEGER value{};
        QueryPerformanceCounter(&value);
        return static_cast<std::uint64_t>(value.QuadPart);
    }
    void run() {
        std::unique_lock<std::mutex> lock(mutex);
        for (;;) {
            changed.wait(lock, [this] { return stopping || (!paused && pendingCount); });
            if (stopping) {
                while (pendingCount) {
                    auto& result = results[pending[head]];
                    result.state = 6; result.endQpc = qpc(); ++cancelled;
                    head = (head + 1) % static_cast<unsigned>(pending.size()); --pendingCount;
                }
                finished = true; changed.notify_all(); return;
            }
            const unsigned slot = pending[head];
            head = (head + 1) % static_cast<unsigned>(pending.size()); --pendingCount;
            auto& result = results[slot];
            const auto& target = targets[slot];
            active = true; result.state = 2; result.beginQpc = qpc(); changed.notify_all();
            lock.unlock();
            const BOOL loaded = target.method == 0
                ? load(target.font, target.preset, target.bank)
                : loadEx(target.font, target.preset, target.bank, target.length, target.flags);
            const DWORD nativeError = loaded ? 0 : static_cast<DWORD>(error());
            // NOWAIT covers sample reads, not SFZ parsing. A READY result
            // requires conservative global reader-idle evidence too. Stop
            // cancels this wait; BASS-owned readers are not our threads.
            if (loaded && (target.flags & BASS_MIDI_FONTLOAD_NOWAIT)) {
                lock.lock();
                while (!stopping && config(BASS_CONFIG_MIDI_SAMPLELOADING))
                    changed.wait_for(lock, std::chrono::milliseconds(2));
                lock.unlock();
            }
            lock.lock();
            result.error = nativeError; result.endQpc = qpc(); active = false;
            if (stopping) { result.state = 6; ++cancelled; }
            else if (!loaded) { result.state = 5; ++failed; }
            else { result.state = target.method == 0 || !target.length ? 3 : 4; ++completed; }
            changed.notify_all();
        }
    }
};

extern "C" __declspec(dllexport) unsigned __cdecl font_preload_queue_version() { return 1; }
extern "C" __declspec(dllexport) unsigned __cdecl font_preload_queue_live_count() {
    return fontPreloadQueueLive.load(std::memory_order_relaxed);
}
extern "C" __declspec(dllexport) void* __cdecl font_preload_queue_create(
    const FontPreloadQueueTarget* targets, unsigned count, std::uint64_t epoch, unsigned capacity,
    void* load, void* loadEx, void* error, void* config, int paused) {
    if (!targets || !count || count > 65536 || !capacity || capacity > 65536 ||
        !epoch || !load || !loadEx || !error || !config) return nullptr;
    for (unsigned i = 0; i < count; ++i) {
        const auto& row = targets[i];
        if (!row.font || row.preset < 0 || row.preset > 65535 || row.bank < 0 || row.bank > 65535 ||
            row.method > 1 || (row.flags & ~(BASS_MIDI_FONTLOAD_NOWAIT | BASS_MIDI_FONTLOAD_TIME)) ||
            (!row.method && (row.length || row.flags))) return nullptr;
    }
    try {
        return new FontPreloadQueue(epoch, targets, count, capacity,
            reinterpret_cast<FontLoadAddress>(load), reinterpret_cast<FontLoadExAddress>(loadEx),
            reinterpret_cast<ErrorAddress>(error), reinterpret_cast<ConfigAddress>(config), paused != 0);
    } catch (...) { return nullptr; }
}
// 1 accepted;2 this same immutable target already admitted;0 backpressure
// (caller retains it);-1 stopped;-2 wrong epoch;-3 invalid target. No MIDI
// event is accepted here. A retry/idempotent preparation is not event coalescing.
extern "C" __declspec(dllexport) int __cdecl font_preload_queue_submit(
    void* pointer, std::uint64_t epoch, unsigned slot, unsigned timeoutMs) {
    if (!pointer) return -3;
    auto& queue = *static_cast<FontPreloadQueue*>(pointer);
    std::unique_lock<std::mutex> lock(queue.mutex);
    if (epoch != queue.epoch) return -2;
    if (slot >= queue.targets.size()) return -3;
    if (queue.stopping) return -1;
    if (queue.results[slot].state) return 2;
    if (queue.pendingCount == queue.pending.size() && timeoutMs) {
        queue.changed.wait_for(lock, std::chrono::milliseconds(timeoutMs), [&queue] {
            return queue.stopping || queue.pendingCount < queue.pending.size();
        });
    }
    if (queue.stopping) return -1;
    if (queue.results[slot].state) return 2;
    if (queue.pendingCount == queue.pending.size()) return 0;
    queue.pending[queue.tail] = slot;
    queue.tail = (queue.tail + 1) % static_cast<unsigned>(queue.pending.size()); ++queue.pendingCount;
    queue.results[slot].state = 1; queue.results[slot].serial = ++queue.accepted;
    queue.changed.notify_all(); return 1;
}
extern "C" __declspec(dllexport) int __cdecl font_preload_queue_resume(void* pointer) {
    if (!pointer) return 0;
    auto& queue = *static_cast<FontPreloadQueue*>(pointer);
    std::lock_guard<std::mutex> lock(queue.mutex);
    if (queue.stopping) return 0;
    queue.paused = false; queue.changed.notify_all(); return 1;
}
// resume+wait in one native call also permits a PyDLL audit: GIL remains held
// over the entire actual BASS worker dispatch and completion interval.
extern "C" __declspec(dllexport) int __cdecl font_preload_queue_wait_idle(
    void* pointer, unsigned timeoutMs, int resume, std::uint64_t* interval) {
    if (!pointer) return 0;
    auto& queue = *static_cast<FontPreloadQueue*>(pointer);
    std::unique_lock<std::mutex> lock(queue.mutex);
    if (interval) interval[0] = FontPreloadQueue::qpc();
    if (resume) { queue.paused = false; queue.changed.notify_all(); }
    const bool done = queue.changed.wait_for(lock, std::chrono::milliseconds(timeoutMs), [&queue] {
        return !queue.pendingCount && !queue.active;
    });
    if (interval) interval[1] = FontPreloadQueue::qpc();
    return done ? 1 : 0;
}
extern "C" __declspec(dllexport) int __cdecl font_preload_queue_result(
    void* pointer, unsigned slot, FontPreloadQueueResult* out) {
    if (!pointer || !out) return 0;
    auto& queue = *static_cast<FontPreloadQueue*>(pointer);
    std::lock_guard<std::mutex> lock(queue.mutex);
    if (slot >= queue.results.size()) return 0;
    *out = queue.results[slot]; return 1;
}
extern "C" __declspec(dllexport) int __cdecl font_preload_queue_status(void* pointer, std::uint64_t* out) {
    if (!pointer || !out) return 0;
    auto& queue = *static_cast<FontPreloadQueue*>(pointer);
    std::lock_guard<std::mutex> lock(queue.mutex);
    out[0]=queue.epoch; out[1]=queue.references.load(); out[2]=queue.pendingCount; out[3]=queue.active;
    out[4]=queue.accepted; out[5]=queue.completed; out[6]=queue.failed; out[7]=queue.cancelled;
    out[8]=queue.stopping; out[9]=queue.joined; out[10]=queue.paused; out[11]=queue.finished;
    return 1;
}
extern "C" __declspec(dllexport) int __cdecl font_preload_queue_cancel(void* pointer) {
    if (!pointer) return 0;
    auto& queue = *static_cast<FontPreloadQueue*>(pointer);
    std::lock_guard<std::mutex> lock(queue.mutex);
    queue.stopping = true; queue.changed.notify_all(); return 1;
}
extern "C" __declspec(dllexport) int __cdecl font_preload_queue_join(void* pointer, unsigned timeoutMs) {
    if (!pointer) return 0;
    auto& queue = *static_cast<FontPreloadQueue*>(pointer);
    std::unique_lock<std::mutex> lock(queue.mutex);
    if (!queue.changed.wait_for(lock, std::chrono::milliseconds(timeoutMs), [&queue] { return queue.finished; })) return 0;
    // Control-owner calls join serially. It must not be invoked on this worker.
    if (!queue.joined) { lock.unlock(); queue.worker.join(); lock.lock(); queue.joined = true; }
    return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl font_preload_queue_retain(void* pointer) {
    return pointer ? static_cast<FontPreloadQueue*>(pointer)->references.fetch_add(1) + 1 : 0;
}
// Additional borrowers release only their own reference. The owner uses destroy.
extern "C" __declspec(dllexport) unsigned __cdecl font_preload_queue_release(void* pointer) {
    if (!pointer) return 0;
    auto& refs = static_cast<FontPreloadQueue*>(pointer)->references;
    unsigned count = refs.load();
    while (count > 1 && !refs.compare_exchange_weak(count, count - 1)) {}
    return count > 1 ? count - 1 : count;
}
// Refuse deletion of live worker or native borrower. Owner retains queue,
// callback DLLs and source handles after failure, then retries after detach.
extern "C" __declspec(dllexport) int __cdecl font_preload_queue_destroy(void* pointer) {
    if (!pointer) return 1;
    auto* queue = static_cast<FontPreloadQueue*>(pointer);
    { std::lock_guard<std::mutex> lock(queue->mutex);
      if (!queue->joined || queue->references.load() != 1) return 0; }
    delete queue; return 1;
}

// First-use plan v1 is a native consumer of the I/O queue, NOT an actual
// SoundFont resolver. Immutable rows reproduce the qualified legacy hook's
// ordered exact FontLoad attempts, with only statically-proven singleton
// NOTAVAIL aliases. Native system/ASYNC/selection qualification is external.
struct FontPreloadAttempt { unsigned slot, aliasSlot; };
struct FontPreloadRoute { unsigned bank, preset, first, count; };
static_assert(sizeof(FontPreloadAttempt)==8 && sizeof(FontPreloadRoute)==16,"preload plan ABI");
static std::atomic<unsigned> fontPreloadPlanLive{0};
struct FontPreloadPlan {
    FontPreloadQueue* queue;
    const std::uint64_t epoch, streamGeneration;
    const unsigned stream, mode;
    const std::vector<FontPreloadRoute> routes;
    // Incoming MIDI address domain, not a BASS SoundFont bank maximum.
    // Fixed lookup avoids both O(routeCount^2) preparation and per-Note scan.
    std::array<unsigned,129*128> routeIndex{};
    const std::vector<FontPreloadAttempt> attempts;
    std::vector<unsigned char> aliases;
    std::atomic<unsigned> references{1},active{0};
    std::atomic<bool> stopping{false},joined{false};
    std::atomic<std::uint64_t> calls{0},fullReady{0},bestEffort{0},handoffs{0},cancelled{0};
    std::mutex consumer;
    FontPreloadPlan(FontPreloadQueue* io,std::uint64_t e,std::uint64_t g,unsigned handle,unsigned policy,
                    const FontPreloadRoute* r,unsigned rc,const FontPreloadAttempt* a,unsigned ac,
                    const unsigned char* seededAliases)
        :queue(io),epoch(e),streamGeneration(g),stream(handle),mode(policy),
         routes(rc?std::vector<FontPreloadRoute>(r,r+rc):std::vector<FontPreloadRoute>{}),
         attempts(ac?std::vector<FontPreloadAttempt>(a,a+ac):std::vector<FontPreloadAttempt>{}),aliases(ac,0){
        routeIndex.fill(UINT32_MAX);
        for(unsigned i=0;i<rc;++i)routeIndex[r[i].bank*128+r[i].preset]=i;
        if(seededAliases)for(unsigned i=0;i<ac;++i)aliases[i]=seededAliases[i];
        ++fontPreloadPlanLive;
    }
    ~FontPreloadPlan(){delete queue;--fontPreloadPlanLive;}
    int full(unsigned slot,FontPreloadQueueResult& out){
        auto& q=*queue;std::unique_lock<std::mutex> lock(q.mutex);
        auto& result=q.results[slot];
        if(q.stopping||stopping.load())return -1;
        if(result.state==3){out=result;return 1;}
        // Failure is best-effort for THIS Note. The next Note retries exactly
        // as legacy; cumulative queue metrics retain failed attempts.
        if(result.state==5)result=FontPreloadQueueResult{};
        while(!result.state&&q.pendingCount==q.pending.size()&&!q.stopping)
            q.changed.wait(lock);
        if(q.stopping||stopping.load())return -1;
        if(!result.state){
            q.pending[q.tail]=slot;q.tail=(q.tail+1)%static_cast<unsigned>(q.pending.size());++q.pendingCount;
            result.state=1;result.serial=++q.accepted;q.changed.notify_all();
        }
        q.changed.wait(lock,[&]{return q.stopping||result.state>=3;});
        out=result;
        return q.stopping||stopping.load()||result.state==6?-1:result.state==3?1:2;
    }
};
extern "C" __declspec(dllexport) unsigned __cdecl font_preload_plan_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl font_preload_plan_live_count(){return fontPreloadPlanLive.load();}
extern "C" __declspec(dllexport) unsigned __cdecl font_preload_plan_cache_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl font_preload_plan_empty_version(){return 1;}
// Additive qualification: complete MIDI address index and allocation-checked
// DWORD target/attempt storage, without changing the v1 row/function ABI.
extern "C" __declspec(dllexport) unsigned __cdecl font_preload_plan_address_domain_version(){return 1;}
// v2 qualifies the legacy prefetch REQUEST address. LSB and render ASYNC do
// not change that hook's FontLoad(bankMSB/drum128,Program) arguments. This is
// independent of event-position StreamGetPreset used by tone/profile.
extern "C" __declspec(dllexport) unsigned __cdecl font_preload_plan_request_version(){return 2;}
// Pure whole-frame admission: no preload, source query, cache mutation or
// lock. The caller already borrows this immutable plan. Runtime cancellation
// after this check still requires an explicit retained-prefix stop contract.
extern "C" __declspec(dllexport) int __cdecl font_preload_plan_qualify(void* pointer,
        std::uint64_t epoch,std::uint64_t generation,unsigned stream,unsigned mode,
        unsigned bank,unsigned preset,unsigned lsb,unsigned qualification){
    if(!pointer)return 0;auto& p=*static_cast<FontPreloadPlan*>(pointer);
    if(epoch!=p.epoch||generation!=p.streamGeneration||stream!=p.stream||mode!=p.mode||p.stopping.load())return 0;
    if(p.mode!=1)return 1;
    if(qualification!=7||lsb>127||bank>128||preset>127)return 0;
    return p.routeIndex[bank*128+preset]!=UINT32_MAX;
}
extern "C" __declspec(dllexport) void* __cdecl font_preload_plan_create(
    const FontPreloadQueueTarget* targets,unsigned count,const unsigned char* fullSeeds,
    const FontPreloadRoute* routes,unsigned routeCount,const FontPreloadAttempt* attempts,unsigned attemptCount,
    const unsigned char* aliasSeeds,std::uint64_t epoch,std::uint64_t streamGeneration,unsigned stream,unsigned mode,
    unsigned capacity,void* load,void* loadEx,void* error,void* config){
    // v1 DWORD counts remain ABI-compatible. The former 65,536 target/attempt
    // cap was product policy, not FontLoad's bank/preset domain. Checked vector
    // allocation failure is an explicit preparation refusal, never truncation.
    if(!epoch||!streamGeneration||!stream||mode>2||!capacity||capacity>65536||routeCount>129*128||
       (routeCount&&!routes)||(attemptCount&&!attempts)||(count&&!targets)||
       !load||!loadEx||!error||!config||(mode==1&&((count||routeCount||attemptCount)&&(!count||!routeCount||!attemptCount))))return nullptr;
    for(unsigned i=0;i<count;++i){const auto& row=targets[i];
        if(!row.font||row.preset<0||row.preset>65535||row.bank<0||row.bank>65535||row.method||row.length||row.flags||
           (fullSeeds&&fullSeeds[i]>1))return nullptr;}
    for(unsigned i=0;i<attemptCount;++i){const auto& row=attempts[i];
        if(row.slot>=count||(row.aliasSlot!=UINT32_MAX&&row.aliasSlot>=count)||
           (aliasSeeds&&(aliasSeeds[i]>1||(aliasSeeds[i]&&row.aliasSlot==UINT32_MAX))))return nullptr;}
    std::array<unsigned char,129*128> seen{};
    for(unsigned i=0;i<routeCount;++i){const auto& row=routes[i];
        if(row.bank>128||row.preset>127||!row.count||row.first>attemptCount||row.count>attemptCount-row.first)return nullptr;
        auto& present=seen[row.bank*128+row.preset];if(present)return nullptr;present=1;}
    try{
        auto plan=std::make_unique<FontPreloadPlan>(nullptr,epoch,streamGeneration,stream,mode,routes,routeCount,
                                                  attempts,attemptCount,aliasSeeds);
        std::unique_ptr<FontPreloadQueue> queue;
        if(mode==1&&count){queue=std::make_unique<FontPreloadQueue>(epoch,targets,count,capacity,
            reinterpret_cast<FontLoadAddress>(load),reinterpret_cast<FontLoadExAddress>(loadEx),
            reinterpret_cast<ErrorAddress>(error),reinterpret_cast<ConfigAddress>(config),false);
            if(fullSeeds){std::lock_guard<std::mutex> lock(queue->mutex);
                for(unsigned i=0;i<count;++i)if(fullSeeds[i])queue->results[i].state=3;}}
        plan->queue=queue.release();return plan.release();
    }catch(...){return nullptr;}
}
// This call MUST run outside a BASS channel lock and kernel/GUI mutex. The
// input consumer retains its frame/prefix/QPC/port until completion. It makes
// no audio submission and no source query; caller sends original Note once.
// qualification bits7 = applied synchronous selection/current mapping/system
// provenance. Nonzero LSB or unqualified flags explicitly hand off unchanged.
// out[0..7]: attempts,last-error,winner-slot,winner-serial,beginQpc,endQpc,
// policy,epoch. Return1 full-ready/no-hook,2 best-effort failed => Note continues;
// 0 missing/unqualified handoff;-1cancel => original Note NOT consumed;
// -2generation mismatch;-3concurrent consumer/invalid argument.
extern "C" __declspec(dllexport) int __cdecl font_preload_plan_before_note(
    void* pointer,std::uint64_t epoch,std::uint64_t generation,unsigned stream,unsigned channel,
    unsigned bank,unsigned preset,unsigned lsb,unsigned qualification,std::uint64_t* out){
    if(!pointer||!out||channel>=128)return -3;
    auto& p=*static_cast<FontPreloadPlan*>(pointer);
    std::fill_n(out,8,0);out[2]=UINT32_MAX;out[4]=FontPreloadQueue::qpc();out[6]=p.mode;out[7]=p.epoch;
    if(epoch!=p.epoch||generation!=p.streamGeneration||stream!=p.stream)return -2;
    if(p.stopping.load())return -1;
    if(p.mode!=1){out[5]=FontPreloadQueue::qpc();return 1;}
    if(qualification!=7||lsb>127||bank>128||preset>127){++p.handoffs;return 0;}
    std::unique_lock<std::mutex> consumer(p.consumer,std::try_to_lock);
    if(!consumer.owns_lock())return -3;
    const auto routeSlot=p.routeIndex[bank*128+preset];
    if(routeSlot==UINT32_MAX){++p.handoffs;return 0;}
    const auto* route=&p.routes[routeSlot];
    ++p.active;++p.calls;
    int status=2;
    for(unsigned i=route->first;i<route->first+route->count;++i){
        const auto& attempt=p.attempts[i];FontPreloadQueueResult result{};
        unsigned slot=p.aliases[i]?attempt.aliasSlot:attempt.slot;
        status=p.full(slot,result);++out[0];out[1]=result.error;
        if(status==-1)break;
        if(status==1){out[2]=slot;out[3]=result.serial;break;}
        if(attempt.aliasSlot!=UINT32_MAX&&(!p.aliases[i])&&result.error==BASS_ERROR_NOTAVAIL){
            p.aliases[i]=1;status=p.full(attempt.aliasSlot,result);++out[0];out[1]=result.error;
            if(status==1){out[2]=attempt.aliasSlot;out[3]=result.serial;}
            break; // Verified explicit singleton alias is terminal, even failed.
        }
        if(p.aliases[i])break;
    }
    --p.active;out[5]=FontPreloadQueue::qpc();
    if(status==1)++p.fullReady;else if(status==2)++p.bestEffort;else ++p.cancelled;
    return status;
}
extern "C" __declspec(dllexport) int __cdecl font_preload_plan_status(void* pointer,std::uint64_t* out){
    if(!pointer||!out)return 0;auto& p=*static_cast<FontPreloadPlan*>(pointer);
    out[0]=p.epoch;out[1]=p.streamGeneration;out[2]=p.references.load();out[3]=p.active.load();
    out[4]=p.calls.load();out[5]=p.fullReady.load();out[6]=p.bestEffort.load();out[7]=p.handoffs.load();
    out[8]=p.cancelled.load();out[9]=p.stopping.load();out[10]=p.joined.load();out[11]=p.mode;
    return 1;
}
extern "C" __declspec(dllexport) int __cdecl font_preload_plan_io_status(void* pointer,std::uint64_t* out){
    if(!pointer||!out)return 0;auto& p=*static_cast<FontPreloadPlan*>(pointer);
    if(!p.queue){std::fill_n(out,12,0);return 1;}
    return font_preload_queue_status(p.queue,out);
}
extern "C" __declspec(dllexport) int __cdecl font_preload_plan_io_result(void* pointer,unsigned slot,FontPreloadQueueResult* out){
    if(!pointer)return 0;auto& p=*static_cast<FontPreloadPlan*>(pointer);
    return p.queue?font_preload_queue_result(p.queue,slot,out):0;
}
// Metadata only: preserve successful full keys and singleton-alias attempts
// before returning to legacy/handoff. Never wait for the I/O consumer on a
// GUI/control caller. 0 busy,1 copied,-1 invalid; failure is not full-ready.
extern "C" __declspec(dllexport) int __cdecl font_preload_plan_cache(void* pointer,
        unsigned char* full,unsigned fullCount,unsigned char* aliases,unsigned aliasCount){
    if(!pointer)return -1;auto& p=*static_cast<FontPreloadPlan*>(pointer);
    const auto targets=p.queue?p.queue->results.size():0;
    if(fullCount!=targets||aliasCount!=p.aliases.size()||(fullCount&&!full)||(aliasCount&&!aliases))return -1;
    std::unique_lock<std::mutex> consumer(p.consumer,std::try_to_lock);
    if(!consumer.owns_lock())return 0;
    if(p.queue){std::unique_lock<std::mutex> lock(p.queue->mutex,std::try_to_lock);if(!lock.owns_lock())return 0;
        for(unsigned i=0;i<fullCount;++i)full[i]=p.queue->results[i].state==3?1:0;}
    if(aliasCount)std::copy(p.aliases.begin(),p.aliases.end(),aliases);
    return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl font_preload_plan_retain(void* pointer){
    return pointer?static_cast<FontPreloadPlan*>(pointer)->references.fetch_add(1)+1:0;
}
// Root ownership must cover entry into this pure attachment validation.
// Returning1 borrows the exact immutable plan; it performs no I/O/MIDI query.
extern "C" __declspec(dllexport) int __cdecl font_preload_plan_acquire(void* pointer,
        std::uint64_t epoch,std::uint64_t generation,unsigned stream,unsigned mode){
    if(!pointer)return 0;auto& p=*static_cast<FontPreloadPlan*>(pointer);
    if(epoch!=p.epoch||generation!=p.streamGeneration||stream!=p.stream||mode!=p.mode||p.stopping.load())return 0;
    p.references.fetch_add(1);
    if(p.stopping.load()){p.references.fetch_sub(1);return 0;}
    return 1;
}
extern "C" __declspec(dllexport) unsigned __cdecl font_preload_plan_release(void* pointer){
    if(!pointer)return 0;auto& refs=static_cast<FontPreloadPlan*>(pointer)->references;unsigned count=refs.load();
    while(count>1&&!refs.compare_exchange_weak(count,count-1)){}return count>1?count-1:count;
}
extern "C" __declspec(dllexport) int __cdecl font_preload_plan_cancel(void* pointer){
    if(!pointer)return 0;auto& p=*static_cast<FontPreloadPlan*>(pointer);p.stopping.store(true);
    if(p.queue)font_preload_queue_cancel(p.queue);return 1;
}
extern "C" __declspec(dllexport) int __cdecl font_preload_plan_join(void* pointer,unsigned timeoutMs){
    if(!pointer)return 0;auto& p=*static_cast<FontPreloadPlan*>(pointer);
    if(p.queue&&!font_preload_queue_join(p.queue,timeoutMs))return 0;
    p.joined.store(true);return 1;
}
extern "C" __declspec(dllexport) int __cdecl font_preload_plan_destroy(void* pointer){
    if(!pointer)return 1;auto* p=static_cast<FontPreloadPlan*>(pointer);
    if(!p->joined.load()||p->active.load()||p->references.load()!=1)return 0;
    delete p;return 1;
}
