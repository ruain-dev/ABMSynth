// Read-only final PCM capture. Independent of synthesis/DSP state.
// bass.h: DSPPROC(DWORD,DWORD,void*,DWORD,void*),NULL buffer removes registration.
// One producer and one serialized consumer. Full queue drops NEW analysis
// samples,never overwrites a consumer span or blocks audio. No callback alloc.
#include <windows.h>
#include <atomic>
#include <vector>
#include <algorithm>
#include <cmath>
#include <new>
#include <cstdint>
struct Capture {
    unsigned channels,rate;
    std::vector<float> data;
    std::atomic<uint64_t> read{0},write{0},gaps{0};
    std::atomic<unsigned> refs{1};
    std::atomic<bool> registered{false};
    Capture(unsigned c,unsigned r):channels(c),rate(r),data(size_t(r)*2){}
};
static_assert(std::atomic<uint64_t>::is_always_lock_free,"Capture counters must not block audio");
extern "C" __declspec(dllexport) unsigned __cdecl tempo_capture_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl tempo_capture_create(unsigned c,unsigned r){
    if(!c||!r||size_t(r)>SIZE_MAX/(2*sizeof(float)))return nullptr;
    try{return new Capture(c,r);}catch(...){return nullptr;}
}
extern "C" __declspec(dllexport) void __cdecl tempo_capture_release(void* p){
    if(p&&static_cast<Capture*>(p)->refs.fetch_sub(1,std::memory_order_acq_rel)==1)delete static_cast<Capture*>(p);
}
extern "C" __declspec(dllexport) void __cdecl tempo_capture_removed(void* p){
    if(p&&static_cast<Capture*>(p)->registered.exchange(false,std::memory_order_acq_rel))tempo_capture_release(p);
}
extern "C" __declspec(dllexport) int __cdecl tempo_capture_arm(void* p){
    if(!p)return 0;auto& s=*static_cast<Capture*>(p);
    if(s.registered.exchange(true,std::memory_order_acq_rel))return 0;
    s.refs.fetch_add(1,std::memory_order_relaxed);return 1;
}
extern "C" __declspec(dllexport) void CALLBACK tempo_capture_dsp(unsigned,unsigned,void* buffer,unsigned bytes,void* p){
    if(!p)return;if(!buffer){tempo_capture_removed(p);return;}
    auto& s=*static_cast<Capture*>(p);const auto* x=static_cast<const float*>(buffer);
    const uint64_t frames=bytes/sizeof(float)/s.channels;
    const uint64_t w=s.write.load(std::memory_order_relaxed),r=s.read.load(std::memory_order_acquire);
    if(frames>s.data.size()-(w-r)){s.gaps.fetch_add(1,std::memory_order_relaxed);return;}
    for(uint64_t i=0;i<frames;++i){
        const float l=x[i*s.channels],rr=s.channels>1?x[i*s.channels+1]:l;
        const float v=s.channels>1?l*.5f+rr*.5f:l;
        s.data[size_t((w+i)%s.data.size())]=std::isfinite(v)?v:0.f;
    }
    s.write.store(w+frames,std::memory_order_release);
}
extern "C" __declspec(dllexport) unsigned __cdecl tempo_capture_read(void* p,float* out,unsigned capacity,uint64_t* metadata){
    if(!p||!out||!metadata)return 0;auto& s=*static_cast<Capture*>(p);
    const uint64_t r=s.read.load(std::memory_order_relaxed),w=s.write.load(std::memory_order_acquire);
    const unsigned n=static_cast<unsigned>(std::min<uint64_t>(capacity,w-r));
    const size_t begin=size_t(r%s.data.size()),first=std::min<size_t>(n,s.data.size()-begin);
    std::copy_n(s.data.data()+begin,first,out);std::copy_n(s.data.data(),n-first,out+first);
    metadata[0]=r;metadata[1]=s.gaps.load(std::memory_order_acquire);
    s.read.store(r+n,std::memory_order_release);return n;
}
