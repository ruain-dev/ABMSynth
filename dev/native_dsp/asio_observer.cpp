// Independently authored Windows COM forwarding adapter. ASIO ABI declaration
// checked against official ASIO2.3 specification / installed driver. No SDK
// implementation, vendor binary patch, global driver registration or PCM DSP.
// BASSASIO AddDevice owns factory/adapter lifetime. Original driver owns buffers.
#define NOMINMAX
#include <windows.h>
#include <objbase.h>
#include <atomic>
#include <cstring>
#include <new>
#include <vector>
#include <array>
#include <algorithm>
#include <cmath>
#ifdef ABMSYNTH_ASIO_DIAGNOSTIC
// Opt-in analysis image only. Copy original final driver buffers, never edit.
struct DiagnosticBlock { unsigned long long begin,end,position,offset;unsigned long frames,flags;unsigned long long clock;long index; };
static std::vector<float> captured;
static std::vector<DiagnosticBlock> capturedBlocks;
static size_t capturedSamples=0,capturedCount=0;
static unsigned long long captureLost=0;
static bool captureEnabled=false;
#endif

#pragma pack(push,4)
struct SampleStamp { unsigned long hi,lo; };
struct TimeInfo { double speed;SampleStamp clock,position;double rate;unsigned long flags;char reserved[12]; };
struct TimeCode { double speed;SampleStamp samples;unsigned long flags;char future[64]; };
struct DriverTime { long reserved[4];TimeInfo info;TimeCode code; };
struct Channel { long channel,input,active,group,type;char name[32]; };
struct Buffer { long input,channel;void* pointers[2]; };
struct Callbacks { void (*buffer)(long,long);void (*rate)(double);
    long (*message)(long,long,void*,double*);DriverTime* (*time)(DriverTime*,long,long); };
// Ordered ABI, not a copied SDK implementation. long is32bits on Windows x64.
struct Driver: IUnknown {
    virtual long init(void*)=0;virtual void name(char*)=0;virtual long version()=0;virtual void error(char*)=0;
    virtual long start()=0;virtual long stop()=0;virtual long channels(long*,long*)=0;
    virtual long latencies(long*,long*)=0;virtual long bufferSize(long*,long*,long*,long*)=0;
    virtual long canRate(double)=0;virtual long getRate(double*)=0;virtual long setRate(double)=0;
    virtual long clocks(void*,long*)=0;virtual long setClock(long)=0;
    virtual long position(SampleStamp*,SampleStamp*)=0;virtual long channelInfo(Channel*)=0;
    virtual long create(Buffer*,long,long,Callbacks*)=0;virtual long dispose()=0;
    virtual long panel()=0;virtual long future(long,void*)=0;virtual long ready()=0;
};
#pragma pack(pop)
static_assert(sizeof(SampleStamp)==8&&sizeof(TimeInfo)==48&&sizeof(Channel)==52&&sizeof(Buffer)==24,"ASIO Windows ABI");
static const GUID proxyId={0xb4708bc6,0xa02d,0x471c,{0x91,0x37,0xe0,0x3d,0x75,0xf1,0x9b,0xa8}};
static GUID targetId{};
static std::atomic<unsigned long> objects{0};
static std::atomic<unsigned long long> counters[8]{};
static std::atomic<unsigned long long> completion[4]{};
static std::atomic<double> completionBudgetMs{0};
static std::atomic<unsigned long long> deliveryLateBlocks{0};
static std::atomic<double> deliveryExcessMs{0},deliveryPeakMs{0};
struct Adapter;
static Adapter* callbackOwner=nullptr; // only one active output; set before Start, clear after joined Stop.
struct Adapter final:Driver {
    std::atomic<ULONG> references{1};Driver* original=nullptr;Callbacks downstream{},forwarded{};
    long frames=0;unsigned long long previous=0;bool valid=false;
    unsigned long long frequency=0;double deviceRate=0,budgetSeconds=0;
    // Driver sample progression versus QPC. A late callback must not receive
    // a new full budget merely because the host started measuring it late.
    // This is bounded observed timeline lateness, not a vendor underrun count.
    std::array<double,256> deliveryPhases{};unsigned deliveryCount=0,deliveryCursor=0,deliveryWarmup=0;
    bool deliveryValid=false;unsigned long long deliveryPrevious=0;
    void resetDelivery(){deliveryCount=deliveryCursor=deliveryWarmup=0;deliveryValid=false;deliveryExcessMs=0;}
    void observeDelivery(unsigned long long begin,unsigned long long end){
        if(!valid||deviceRate<=0||budgetSeconds<=0){resetDelivery();return;}
        if(deliveryValid&&previous<=deliveryPrevious){resetDelivery();}
        deliveryPrevious=previous;deliveryValid=true;
        // Start is not the steady USB timeline: exclude startup priming fills.
        if(++deliveryWarmup<=2)return;
        const double beginPhase=static_cast<double>(begin)/frequency-static_cast<double>(previous)/deviceRate;
        if(deliveryCount){
            double earliest=deliveryPhases[0];for(unsigned i=1;i<deliveryCount;++i)earliest=std::min(earliest,deliveryPhases[i]);
            const double endPhase=static_cast<double>(end)/frequency-static_cast<double>(previous)/deviceRate;
            const double excess=std::max(0.0,endPhase-earliest-budgetSeconds)*1000;
            deliveryExcessMs=excess;if(excess>deliveryPeakMs.load(std::memory_order_relaxed))deliveryPeakMs=excess;
            if(excess>0)++deliveryLateBlocks;
        }
        deliveryPhases[deliveryCursor]=beginPhase;deliveryCursor=(deliveryCursor+1)%256;
        if(deliveryCount<256)++deliveryCount;
    }
    void complete(unsigned long long begin){
        LARGE_INTEGER end{};QueryPerformanceCounter(&end);
        const auto ticks=static_cast<unsigned long long>(end.QuadPart)-begin;
        const auto us=ticks*1000000/frequency;
        ++completion[0];completion[2]=us;
        if(us>completion[3].load(std::memory_order_relaxed))completion[3]=us;
        if(budgetSeconds>0&&static_cast<double>(ticks)/frequency>budgetSeconds)++completion[1];
        observeDelivery(begin,static_cast<unsigned long long>(end.QuadPart));
    }
    std::atomic<bool> resetPosition{true};
#ifdef ABMSYNTH_ASIO_STAGING
    // Retain BASSASIO's original SRC/format algorithm,
    // but isolate in-progress buffers from driver-owned transfer buffers.
    struct Staged {Buffer actual{};std::vector<unsigned char> memory[2];};
    std::vector<Staged> staged;long activeIndex=-1;bool deferredReady=false;
    void finish(long index){
        if(index<0||index>1){activeIndex=-1;return;}
        for(auto& buffer:staged)std::memcpy(buffer.actual.pointers[index],buffer.memory[index].data(),buffer.memory[index].size());
        activeIndex=-1;if(deferredReady){deferredReady=false;original->ready();}
    }
#endif
#ifdef ABMSYNTH_ASIO_DIAGNOSTIC
    Buffer outputBuffers[2]{};long outputTypes[2]{};unsigned outputCount=0;
    void capture(long index,unsigned long long begin,unsigned long long pos,unsigned long flags,unsigned long long clock){
        if(!captureEnabled||outputCount!=2||frames<=0||index<0||index>1)return;
        LARGE_INTEGER end{};QueryPerformanceCounter(&end);
        const size_t samples=static_cast<size_t>(frames)*2;
        if(capturedSamples+samples>captured.size()||capturedCount>=capturedBlocks.size()){captureLost+=samples*4;return;}
        for(unsigned ch=0;ch<2;++ch){
            const auto type=outputTypes[ch];const auto* data=static_cast<const unsigned char*>(outputBuffers[ch].pointers[index]);
            for(long i=0;i<frames;++i){float v=0;
                if(type==19)std::memcpy(&v,data+i*4,4);
                else if(type==18||type==27){long raw=0;std::memcpy(&raw,data+i*4,4);v=static_cast<float>(static_cast<double>(raw)/(type==18?2147483648.0:8388608.0));}
                captured[capturedSamples+i*2+ch]=v;
            }
        }
        capturedBlocks[capturedCount++]={begin,static_cast<unsigned long long>(end.QuadPart),pos,capturedSamples/2,static_cast<unsigned long>(frames),flags,clock,index};capturedSamples+=samples;
    }
#endif
    Adapter(Driver* p):original(p){++objects;LARGE_INTEGER f{};QueryPerformanceFrequency(&f);frequency=f.QuadPart;}
    ~Adapter(){original->Release();if(callbackOwner==this)callbackOwner=nullptr;--objects;}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID id,void** p) override {
        if(!p)return E_POINTER;*p=nullptr;
        if(id==IID_IUnknown||id==proxyId||id==targetId){*p=static_cast<Driver*>(this);AddRef();return S_OK;}
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override{return ++references;}
    ULONG STDMETHODCALLTYPE Release() override {ULONG n=--references;if(!n)delete this;return n;}
    void observe(const SampleStamp& stamp,unsigned long flags){
        ++counters[0];
        if(resetPosition.exchange(false,std::memory_order_relaxed)){valid=false;resetDelivery();}
        if(!(flags&2)){++counters[3];valid=false;counters[5]=0;return;}
        const unsigned long long pos=(static_cast<unsigned long long>(stamp.hi)<<32)|stamp.lo;
        if(valid&&pos>previous){
            const auto delta=pos-previous;
            if(delta>static_cast<unsigned long long>(frames)&&frames>0&&delta%frames==0){
                counters[1].fetch_add(delta/frames-1,std::memory_order_relaxed);++counters[2];
            }else if(delta!=static_cast<unsigned long long>(frames)){++counters[3];}
        }else if(valid){++counters[3];}
        previous=pos;valid=true;counters[5]=1;
    }
    static void switched(long index,long direct){
        auto* s=callbackOwner;if(!s)return;
        LARGE_INTEGER begin{};QueryPerformanceCounter(&begin);
#ifdef ABMSYNTH_ASIO_STAGING
        s->activeIndex=index;s->deferredReady=false;
#endif
        SampleStamp pos{},clock{};const auto result=s->original->position(&pos,&clock);
        s->observe(pos,result==0?2u:0u);
        if(s->downstream.buffer)s->downstream.buffer(index,direct);
#ifdef ABMSYNTH_ASIO_STAGING
        s->finish(index);
#endif
        s->complete(static_cast<unsigned long long>(begin.QuadPart));
    }
    static DriverTime* timed(DriverTime* t,long index,long direct){
        auto* s=callbackOwner;if(!s)return t;
        LARGE_INTEGER begin{};QueryPerformanceCounter(&begin);
#ifdef ABMSYNTH_ASIO_STAGING
        s->activeIndex=index;s->deferredReady=false;
#endif
        SampleStamp empty{};s->observe(t?t->info.position:empty,t?t->info.flags:0);
        DriverTime* result=t;
        if(s->downstream.time)result=s->downstream.time(t,index,direct);
        else if(s->downstream.buffer)s->downstream.buffer(index,direct);
#ifdef ABMSYNTH_ASIO_STAGING
        s->finish(index);
#endif
        s->complete(static_cast<unsigned long long>(begin.QuadPart));
#ifdef ABMSYNTH_ASIO_DIAGNOSTIC
        s->capture(index,static_cast<unsigned long long>(begin.QuadPart),s->previous,t?t->info.flags:0,t?(static_cast<unsigned long long>(t->info.clock.hi)<<32)|t->info.clock.lo:0);
#endif
        return result;
    }
    static void changed(double rate){auto* s=callbackOwner;if(!s)return;s->resetPosition=true;++counters[6];if(s->downstream.rate)s->downstream.rate(rate);}
    static long message(long selector,long value,void* data,double* opt){
        auto* s=callbackOwner;if(!s)return 0;
        if(selector==1&&(value==7||value==15))return 1;
        if(selector==7)return 1; // time-info observation, downstream buffer fallback keeps original fill.
        if(selector==15){++counters[4];return 1;}
        if(selector==3||selector==5||selector==4){s->resetPosition=true;++counters[6];}
        return s->downstream.message?s->downstream.message(selector,value,data,opt):0;
    }
    long init(void* h) override{return original->init(h);}
    void name(char* p) override{original->name(p);}long version() override{return original->version();}
    void error(char* p) override{original->error(p);}
    long start() override{
        resetDelivery();
        resetPosition=true;deviceRate=0;budgetSeconds=0;original->getRate(&deviceRate);
        if(deviceRate>0&&frames>0){long input=0,output=0;long available=frames;
            // Official ASIOGetLatencies: next buffer begins sounding after
            // outputLatency samples. Completion later than that is already late,
            // even if a complete processing period has not elapsed (HiBy816<1024).
            if(original->latencies(&input,&output)==0&&output>0&&output<available)available=output;
            budgetSeconds=available/deviceRate;}
        completionBudgetMs=budgetSeconds*1000;return original->start();}
    long stop() override{auto r=original->stop();if(r==0){resetPosition=true;counters[5]=0;}return r;}
    long channels(long* a,long* b) override{return original->channels(a,b);}
    long latencies(long* a,long* b) override{return original->latencies(a,b);}
    long bufferSize(long* a,long* b,long* c,long* d) override{return original->bufferSize(a,b,c,d);}
    long canRate(double r) override{return original->canRate(r);}
    long getRate(double* r) override{return original->getRate(r);}
    long setRate(double r) override{resetPosition=true;return original->setRate(r);}
    long clocks(void* p,long* n) override{return original->clocks(p,n);}
    long setClock(long n) override{resetPosition=true;return original->setClock(n);}
    long position(SampleStamp* a,SampleStamp* b) override{return original->position(a,b);}
    long channelInfo(Channel* p) override{return original->channelInfo(p);}
    long create(Buffer* p,long n,long count,Callbacks* callbacks) override{
        if(!callbacks)return -998;
        downstream=*callbacks;frames=count;valid=false;resetPosition=true;
        for(auto& v:counters)v=0;counters[7]=static_cast<unsigned long long>(count);
        for(auto& v:completion)v=0;
        deliveryLateBlocks=0;deliveryPeakMs=0;resetDelivery();
        forwarded={switched,changed,message,timed};callbackOwner=this;
        auto result=original->create(p,n,count,&forwarded);
#ifdef ABMSYNTH_ASIO_STAGING
        staged.clear();
        if(result==0){try{
            staged.reserve(static_cast<size_t>(n));
            for(long i=0;i<n;++i)if(!p[i].input){Channel ch{};ch.channel=p[i].channel;ch.input=0;
                if(original->channelInfo(&ch)!=0||ch.type!=18)continue;
                staged.emplace_back();auto& b=staged.back();b.actual=p[i];
                for(unsigned j=0;j<2;++j){b.memory[j].resize(static_cast<size_t>(count)*4);std::memset(p[i].pointers[j],0,b.memory[j].size());p[i].pointers[j]=b.memory[j].data();}
            }
        }catch(...){original->dispose();staged.clear();result=-999;}}
#endif
#ifdef ABMSYNTH_ASIO_DIAGNOSTIC
        outputCount=0;
        if(result==0)for(long i=0;i<n&&outputCount<2;++i)if(!p[i].input){Channel channel{};channel.channel=p[i].channel;channel.input=0;
            if(original->channelInfo(&channel)==0){outputBuffers[outputCount]=p[i];outputTypes[outputCount++]=channel.type;}}
#endif
        if(result!=0)callbackOwner=nullptr;
        return result;
    }
    long dispose() override{auto result=original->dispose();if(result==0){if(callbackOwner==this)callbackOwner=nullptr;valid=false;counters[5]=0;
#ifdef ABMSYNTH_ASIO_STAGING
        staged.clear();activeIndex=-1;deferredReady=false;
#endif
    }return result;}
    long panel() override{return original->panel();}
    long future(long s,void* p) override{return original->future(s,p);}
    long ready() override{
#ifdef ABMSYNTH_ASIO_STAGING
        if(activeIndex>=0&&!staged.empty()){deferredReady=true;return 0;}
#endif
        return original->ready();}
};
struct Factory final:IClassFactory {
    std::atomic<ULONG> references{1};
    Factory(){++objects;}~Factory(){--objects;}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID id,void** p) override{if(!p)return E_POINTER;*p=nullptr;if(id==IID_IUnknown||id==IID_IClassFactory){*p=this;AddRef();return S_OK;}return E_NOINTERFACE;}
    ULONG STDMETHODCALLTYPE AddRef() override{return ++references;}
    ULONG STDMETHODCALLTYPE Release() override{auto n=--references;if(!n)delete this;return n;}
    HRESULT STDMETHODCALLTYPE CreateInstance(IUnknown* outer,REFIID id,void** p) override {
        if(!p)return E_POINTER;*p=nullptr;if(outer)return CLASS_E_NOAGGREGATION;
        Driver* source=nullptr;auto hr=CoCreateInstance(targetId,nullptr,CLSCTX_INPROC_SERVER,targetId,reinterpret_cast<void**>(&source));
        if(FAILED(hr))return hr;
        auto* adapter=new(std::nothrow)Adapter(source);if(!adapter){source->Release();return E_OUTOFMEMORY;}
        hr=adapter->QueryInterface(id,p);adapter->Release();return hr;
    }
    HRESULT STDMETHODCALLTYPE LockServer(BOOL lock) override{if(lock)++objects;else --objects;return S_OK;}
};
STDAPI DllGetClassObject(REFCLSID id,REFIID iid,void** p){
    if(id!=proxyId)return CLASS_E_CLASSNOTAVAILABLE;auto* f=new(std::nothrow)Factory;if(!f)return E_OUTOFMEMORY;
    auto hr=f->QueryInterface(iid,p);f->Release();return hr;
}
STDAPI DllCanUnloadNow(){return objects.load()==0?S_OK:S_FALSE;}
extern "C" __declspec(dllexport) int __cdecl asio_observer_configure(const GUID* id){
    if(!id||objects.load()!=0||*id==proxyId)return 0;targetId=*id;return 1;
}
extern "C" __declspec(dllexport) void __cdecl asio_observer_snapshot(unsigned long long* output){
    if(output)for(unsigned i=0;i<8;++i)output[i]=counters[i].load(std::memory_order_relaxed);
}
// Full driver callback work, including BASSASIO SRC/format and final commit.
// A period/driver-reported playback-deadline miss is timing evidence,
// not a claimed analog-pop counter. The USB transfer deadline can be earlier.
extern "C" __declspec(dllexport) void __cdecl asio_observer_completion(unsigned long long* output){
    if(output)for(unsigned i=0;i<4;++i)output[i]=completion[i].load(std::memory_order_relaxed);
}
extern "C" __declspec(dllexport) double __cdecl asio_observer_completion_budget(){return completionBudgetMs.load(std::memory_order_relaxed);}
extern "C" __declspec(dllexport) void __cdecl asio_observer_delivery(unsigned long long* late,double* excess,double* peak){
    if(late)*late=deliveryLateBlocks.load(std::memory_order_relaxed);
    if(excess)*excess=deliveryExcessMs.load(std::memory_order_relaxed);
    if(peak)*peak=deliveryPeakMs.load(std::memory_order_relaxed);
}
#ifdef ABMSYNTH_ASIO_DIAGNOSTIC
extern "C" __declspec(dllexport) void __cdecl asio_diagnostic_begin(){
    captured.resize(96000*2*80);capturedBlocks.resize(50000);capturedSamples=capturedCount=0;captureLost=0;captureEnabled=true;
}
extern "C" __declspec(dllexport) int __cdecl asio_diagnostic_device(double* rate,long* input,long* output){
    auto* s=callbackOwner;if(!s)return 0;return s->original->getRate(rate)==0&&s->original->latencies(input,output)==0;
}
extern "C" __declspec(dllexport) void __cdecl asio_diagnostic_snapshot(const float** pcm,unsigned long long* samples,const DiagnosticBlock** blocks,unsigned long long* count,unsigned long long* lost){
    captureEnabled=false;*pcm=captured.data();*samples=capturedSamples;*blocks=capturedBlocks.data();*count=capturedCount;*lost=captureLost;
}
#endif
