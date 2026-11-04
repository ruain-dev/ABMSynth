// Optional raw MIDI recording, before transforms. Never called from audio.
// Preallocated bounded ring; disk writes belong to a dedicated native thread.
#pragma once
#include <windows.h>
#include <process.h>
#include <atomic>
#include <array>
#include <vector>
#include <algorithm>
#include <cstring>

struct MidiRecordingSink {
    static constexpr unsigned capacity=8*1024*1024; // recording budget, not MIDI domain
    std::atomic<unsigned> references{1};
    SRWLOCK lock=SRWLOCK_INIT;CONDITION_VARIABLE changed=CONDITION_VARIABLE_INIT;
    std::vector<unsigned char> ring;
    HANDLE file=INVALID_HANDLE_VALUE,thread=nullptr;
    unsigned head=0,used=0,state=1,error=0;
    bool ending=false;
    unsigned long long frames=0,lost=0,written=0,frequency=0,started=0,activeStart=0,elapsed=0;
    static unsigned long long now(){LARGE_INTEGER v{};QueryPerformanceCounter(&v);return v.QuadPart;}
    explicit MidiRecordingSink(const wchar_t* path):ring(capacity){
        LARGE_INTEGER f{};QueryPerformanceFrequency(&f);frequency=f.QuadPart;started=activeStart=now();
        file=CreateFileW(path,GENERIC_WRITE,FILE_SHARE_READ,nullptr,CREATE_NEW,FILE_ATTRIBUTE_TEMPORARY,nullptr);
        if(file==INVALID_HANDLE_VALUE){error=GetLastError();return;}
        const unsigned long long header[]{0x3143455244494d41ull,frequency}; // AMIDREC1, QPC Hz
        DWORD count=0;
        if(!WriteFile(file,header,sizeof(header),&count,nullptr)||count!=sizeof(header)){error=GetLastError();if(!error)error=ERROR_WRITE_FAULT;return;}
        thread=reinterpret_cast<HANDLE>(_beginthreadex(nullptr,0,&MidiRecordingSink::writeLoop,this,0,nullptr));
        if(!thread){error=ERROR_NOT_ENOUGH_MEMORY;return;}
        append(1,started,0,nullptr,0);
    }
    ~MidiRecordingSink(){
        AcquireSRWLockExclusive(&lock);ending=true;WakeAllConditionVariable(&changed);ReleaseSRWLockExclusive(&lock);
        if(thread){WaitForSingleObject(thread,INFINITE);CloseHandle(thread);}
        if(file!=INVALID_HANDLE_VALUE)CloseHandle(file);
    }
    void retain(){references.fetch_add(1,std::memory_order_relaxed);}
    void release(){if(references.fetch_sub(1,std::memory_order_acq_rel)==1)delete this;}
    void put(const void* data,unsigned size){
        if(!size)return;
        const unsigned tail=(head+used)%capacity,first=std::min(size,capacity-tail);
        std::memcpy(ring.data()+tail,data,first);std::memcpy(ring.data(),static_cast<const unsigned char*>(data)+first,size-first);used+=size;
    }
    void appendLocked(unsigned kind,unsigned long long tick,unsigned port,const unsigned char* bytes,unsigned size){
        if(ending||error)return;
        if(size>capacity-24||size+24>capacity-used){++lost;return;}
        const unsigned long long header[]{tick,(static_cast<unsigned long long>(kind)<<32)|port,size};
        put(header,sizeof(header));put(bytes,size);if(kind==0)++frames;
        WakeConditionVariable(&changed);
    }
    void append(unsigned kind,unsigned long long tick,unsigned port,const unsigned char* bytes,unsigned size){
        AcquireSRWLockExclusive(&lock);appendLocked(kind,tick,port,bytes,size);ReleaseSRWLockExclusive(&lock);
    }
    void transportLoss(unsigned long long count){
        AcquireSRWLockExclusive(&lock);lost+=count;ReleaseSRWLockExclusive(&lock);
    }
    bool segment(unsigned target){
        AcquireSRWLockExclusive(&lock);
        if(ending||(error&&target==1)||target<1||target>2){ReleaseSRWLockExclusive(&lock);return false;}
        if(state!=target){const auto tick=now();if(state==1)elapsed+=tick-activeStart;
            else activeStart=tick;appendLocked(target,tick,0,nullptr,0);state=target;}
        ReleaseSRWLockExclusive(&lock);return true;
    }
    static unsigned __stdcall writeLoop(void* opaque){
        auto& s=*static_cast<MidiRecordingSink*>(opaque);std::array<unsigned char,65536> block{};
        for(;;){
            AcquireSRWLockExclusive(&s.lock);
            while(!s.used&&!s.ending)SleepConditionVariableSRW(&s.changed,&s.lock,INFINITE,0);
            if(!s.used&&s.ending){ReleaseSRWLockExclusive(&s.lock);break;}
            const unsigned size=std::min(static_cast<unsigned>(block.size()),s.used),first=std::min(size,capacity-s.head);
            std::memcpy(block.data(),s.ring.data()+s.head,first);std::memcpy(block.data()+first,s.ring.data(),size-first);
            s.head=(s.head+size)%capacity;s.used-=size;const bool failed=s.error!=0;ReleaseSRWLockExclusive(&s.lock);
            DWORD count=0;unsigned failure=0;
            if(!failed&&(!WriteFile(s.file,block.data(),size,&count,nullptr)||count!=size)){failure=GetLastError();if(!failure)failure=ERROR_WRITE_FAULT;}
            AcquireSRWLockExclusive(&s.lock);if(failure)s.error=failure;else if(!failed)s.written+=size;ReleaseSRWLockExclusive(&s.lock);
        }
        return 0;
    }
};

extern "C" __declspec(dllexport) unsigned __cdecl midi_recording_version(){return 1;}
extern "C" __declspec(dllexport) void* __cdecl midi_recording_create(const wchar_t* path,unsigned* error){
    if(!path||!error)return nullptr;*error=0;
    try{auto* s=new MidiRecordingSink(path);if(s->error){*error=s->error;s->release();return nullptr;}return s;}
    catch(const std::bad_alloc&){*error=ERROR_NOT_ENOUGH_MEMORY;return nullptr;}
}
extern "C" __declspec(dllexport) void __cdecl midi_recording_release(void* p){if(p)static_cast<MidiRecordingSink*>(p)->release();}
extern "C" __declspec(dllexport) int __cdecl midi_recording_segment(void* p,unsigned target){return p&&static_cast<MidiRecordingSink*>(p)->segment(target)?1:0;}
extern "C" __declspec(dllexport) int __cdecl midi_recording_finish(void* p){
    if(!p)return 0;auto& s=*static_cast<MidiRecordingSink*>(p);
    AcquireSRWLockExclusive(&s.lock);
    if(!s.ending){const auto tick=MidiRecordingSink::now();if(s.state==1)s.elapsed+=tick-s.activeStart;
        s.appendLocked(3,tick,0,nullptr,0);s.ending=true;s.state=2;WakeAllConditionVariable(&s.changed);}
    ReleaseSRWLockExclusive(&s.lock);
    if(s.thread&&WaitForSingleObject(s.thread,5000)!=WAIT_OBJECT_0)return 0;
    if(s.file!=INVALID_HANDLE_VALUE){
        if(!FlushFileBuffers(s.file)){AcquireSRWLockExclusive(&s.lock);if(!s.error)s.error=GetLastError();ReleaseSRWLockExclusive(&s.lock);}
        CloseHandle(s.file);s.file=INVALID_HANDLE_VALUE;
    }return 1;
}
extern "C" __declspec(dllexport) int __cdecl midi_recording_status(void* p,unsigned long long* out){
    if(!p||!out)return 0;auto& s=*static_cast<MidiRecordingSink*>(p);AcquireSRWLockShared(&s.lock);
    const unsigned long long v[]{s.frames,s.lost,s.error,s.elapsed+(s.state==1?MidiRecordingSink::now()-s.activeStart:0),s.frequency,s.state,s.used,s.written};
    std::copy_n(v,8,out);ReleaseSRWLockShared(&s.lock);return 1;
}
