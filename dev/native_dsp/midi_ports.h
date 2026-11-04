// WinMM MIDI endpoint I/O. Included after the execution ABI in audio_fx.cpp.
// MIDI output calls/pipe writes run on a dedicated worker, never audio or the
// ordered synthesis actor. Input uses a native callback and bounded copy.
#include <mmsystem.h>
#include <string>

struct MidiPortRouter {
    struct Frame {unsigned port=0,size=0;};
    struct Input {
        MidiPortRouter* owner=nullptr;unsigned port=0;HMIDIIN handle=nullptr;
        std::atomic<unsigned long long> sequence{0};
        std::atomic<unsigned long long> losses{0};
        std::array<MIDIHDR,4> headers{};
        std::array<std::array<char,65536>,4> bytes{};
        std::array<std::atomic<bool>,4> returned{};
    };
    struct Output {unsigned port;HMIDIOUT handle;};
    void* execution;
    SRWLOCK lock=SRWLOCK_INIT;
    HANDLE wake=CreateEventW(nullptr,FALSE,FALSE,nullptr),thread=nullptr;
    std::atomic<bool> stopping{false};
    std::atomic<bool> closing{false},busy{false};
    std::vector<unsigned char> bytes=std::vector<unsigned char>(4*1024*1024);
    std::array<Frame,4096> frames{};
    unsigned head=0,count=0,read=0,write=0,used=0;
    std::vector<std::unique_ptr<Input>> inputs;
    std::vector<Output> outputs;
    std::array<bool,8> virtualOut{};
    std::array<std::array<HANDLE,8>,8> virtualHandles{};
    std::array<std::array<ULONGLONG,8>,8> virtualRetry{};
    std::atomic<unsigned> outputMask{0};
    std::array<unsigned char,8> status{},have{},need{};
    std::array<std::array<unsigned char,3>,8> message{};
    std::array<std::vector<unsigned char>,8> sysex;
    std::atomic<unsigned long long> received{0},sent{0},losses{0},errors{0};
    unsigned long long frequency=0,epoch=0;
    explicit MidiPortRouter(void* e):execution(e){
        LARGE_INTEGER f{},t{};QueryPerformanceFrequency(&f);QueryPerformanceCounter(&t);
        frequency=f.QuadPart;epoch=static_cast<unsigned long long>(t.QuadPart)^reinterpret_cast<uintptr_t>(this);
        for(auto& buffer:sysex)buffer.reserve(65536);
    }
    bool push(unsigned port,const void* source,unsigned size){
        if(port>=8||!source||!size||size>1024*1024||stopping.load())return false;
        AcquireSRWLockExclusive(&lock);
        if(count==frames.size()||size>bytes.size()-used){++losses;ReleaseSRWLockExclusive(&lock);return false;}
        const unsigned first=std::min(size,static_cast<unsigned>(bytes.size())-write);
        std::memcpy(bytes.data()+write,source,first);
        if(first<size)std::memcpy(bytes.data(),static_cast<const unsigned char*>(source)+first,size-first);
        write=(write+size)%static_cast<unsigned>(bytes.size());used+=size;
        frames[(head+count)%frames.size()]={port,size};++count;
        ReleaseSRWLockExclusive(&lock);SetEvent(wake);return true;
    }
    static void CALLBACK callback(HMIDIIN,UINT kind,DWORD_PTR instance,DWORD_PTR p1,DWORD_PTR){
        auto& input=*reinterpret_cast<Input*>(instance);auto& router=*input.owner;
        if(router.stopping.load()||router.closing.load())return;
        LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);
        if(kind==MIM_DATA||kind==MIM_MOREDATA){
            unsigned char b[]{static_cast<unsigned char>(p1),static_cast<unsigned char>(p1>>8),static_cast<unsigned char>(p1>>16)};
            const unsigned cmd=b[0]&0xf0u;
            const unsigned length=b[0]>=0xf8?1:cmd==0xc0||cmd==0xd0?2:b[0]==0xf1||b[0]==0xf3?2:b[0]==0xf2?3:b[0]>=0xf0?1:3;
            router.receive(input,b,length,tick.QuadPart);
        }else if(kind==MIM_LONGDATA||kind==MIM_LONGERROR){
            auto* header=reinterpret_cast<MIDIHDR*>(p1);
            if(kind==MIM_LONGERROR){++router.errors;++router.losses;++input.losses;}
            if(kind==MIM_LONGDATA&&header&&header->dwBytesRecorded){
                if(header->dwBytesRecorded<=header->dwBufferLength)router.receive(input,header->lpData,header->dwBytesRecorded,tick.QuadPart);
                else{++router.errors;++router.losses;++input.losses;}}
            for(unsigned i=0;i<input.headers.size();++i)if(header==&input.headers[i])input.returned[i].store(true);
            SetEvent(router.wake);
        }else if(kind==MIM_ERROR){++router.errors;++router.losses;++input.losses;}
    }
    void receive(Input& input,const void* data,unsigned size,unsigned long long tick){
        // No WinMM/API call, wait, allocation or GIL here. The existing native
        // actor copies and assigns the publication before callback return.
        // External output waits therefore cannot delay MIDI input admission.
        const auto identity=epoch^reinterpret_cast<uintptr_t>(&input);
        unsigned long long origin[]{tick,frequency,identity,++input.sequence,0,input.losses.load(),GetCurrentProcessId(),0x100u|input.port};
        if(!midi_execution_raw(execution,data,size,input.port,tick,0,origin)){++losses;++input.losses;}
        ++received;
    }
    bool addInput(unsigned device,unsigned port){
        if(port>=8||thread)return false;
        auto input=std::make_unique<Input>();input->owner=this;input->port=port;
        if(midiInOpen(&input->handle,device,reinterpret_cast<DWORD_PTR>(&callback),reinterpret_cast<DWORD_PTR>(input.get()),CALLBACK_FUNCTION|MIDI_IO_STATUS))return false;
        for(unsigned i=0;i<input->headers.size();++i){
            auto& h=input->headers[i];h.lpData=input->bytes[i].data();h.dwBufferLength=static_cast<DWORD>(input->bytes[i].size());
            if(midiInPrepareHeader(input->handle,&h,sizeof(h))||midiInAddBuffer(input->handle,&h,sizeof(h))){
                midiInReset(input->handle);for(auto& old:input->headers)if(old.dwFlags&MHDR_PREPARED)midiInUnprepareHeader(input->handle,&old,sizeof(old));midiInClose(input->handle);return false;
            }
        }
        inputs.push_back(std::move(input));return true;
    }
    bool addOutput(unsigned device,unsigned port){
        if(port>=8||thread)return false;HMIDIOUT handle=nullptr;
        if(midiOutOpen(&handle,device,0,0,CALLBACK_NULL))return false;
        outputs.push_back({port,handle});outputMask|=1u<<port;return true;
    }
    void emit(unsigned port,const unsigned char* data,unsigned size){
        for(const auto& out:outputs)if(out.port==port){
            MMRESULT error=0;
            if(data[0]==0xf0){
                MIDIHDR header{};header.lpData=reinterpret_cast<LPSTR>(const_cast<unsigned char*>(data));header.dwBufferLength=size;
                error=midiOutPrepareHeader(out.handle,&header,sizeof(header));
                if(!error){error=midiOutLongMsg(out.handle,&header,sizeof(header));
                    while(!error&&!(header.dwFlags&MHDR_DONE)&&!stopping.load())WaitForSingleObject(wake,1);
                    if(!(header.dwFlags&MHDR_DONE))midiOutReset(out.handle);
                    while(midiOutUnprepareHeader(out.handle,&header,sizeof(header))==MIDIERR_STILLPLAYING)Sleep(1);
                }
            }else{DWORD packed=0;for(unsigned i=0;i<std::min(size,3u);++i)packed|=static_cast<DWORD>(data[i])<<(8*i);error=midiOutShortMsg(out.handle,packed);}
            if(error)++errors;else ++sent;
        }
        if(virtualOut[port]){
            // Each MIDM_OPEN client owns a pipe instance. Connect at send time;
            // no open client is normal and never stalls synthesis.
            for(unsigned client=0;client<8&&!stopping.load();++client){
                wchar_t name[96]{};swprintf_s(name,L"\\\\.\\pipe\\ABMSynthMidiOut%u-%u",port,client);
                auto& pipe=virtualHandles[port][client];
                if(!pipe||pipe==INVALID_HANDLE_VALUE){
                    const auto now=GetTickCount64();if(now<virtualRetry[port][client])continue;
                    pipe=CreateFileW(name,GENERIC_WRITE,0,nullptr,OPEN_EXISTING,FILE_FLAG_OVERLAPPED,nullptr);
                    if(pipe==INVALID_HANDLE_VALUE){virtualRetry[port][client]=now+100;continue;}
                }
                const unsigned header[]{0x3144494du,size};
                std::vector<unsigned char> frame(sizeof(header)+size);std::memcpy(frame.data(),header,sizeof(header));std::memcpy(frame.data()+sizeof(header),data,size);
                OVERLAPPED overlap{};overlap.hEvent=CreateEventW(nullptr,TRUE,FALSE,nullptr);DWORD written=0;
                BOOL ok=WriteFile(pipe,frame.data(),static_cast<DWORD>(frame.size()),&written,&overlap);
                if(!ok&&GetLastError()==ERROR_IO_PENDING){
                    if(WaitForSingleObject(overlap.hEvent,50)==WAIT_OBJECT_0)ok=GetOverlappedResult(pipe,&overlap,&written,FALSE);
                    else{CancelIoEx(pipe,&overlap);GetOverlappedResult(pipe,&overlap,&written,TRUE);++losses;}
                }
                if(ok)++sent;else ++errors;
                CloseHandle(overlap.hEvent);
                if(!ok){CloseHandle(pipe);pipe=nullptr;}
            }
        }
    }
    void output(unsigned port,const unsigned char* data,unsigned size){
        // WinMM short messages need complete messages. Preserve running status,
        // fragmented SysEx and interspersed real-time messages per port.
        for(unsigned index=0;index<size;++index){const auto b=data[index];
            if(b>=0xf8){emit(port,&b,1);continue;}
            if(!sysex[port].empty()){
                if((b&0x80u)&&b!=0xf7){sysex[port].clear();++errors;}
                else{
                    if(sysex[port].size()>=1024*1024){sysex[port].clear();++losses;continue;}
                    sysex[port].push_back(b);if(b==0xf7){emit(port,sysex[port].data(),static_cast<unsigned>(sysex[port].size()));sysex[port].clear();}continue;
                }
            }
            if(b==0xf0){status[port]=0;have[port]=0;sysex[port].push_back(b);continue;}
            if(b&0x80){status[port]=b;have[port]=1;message[port][0]=b;
                need[port]=b>=0xf0?(b==0xf1||b==0xf3?2:b==0xf2?3:1):((b&0xe0)==0xc0?2:3);
                if(need[port]==1){emit(port,message[port].data(),1);have[port]=0;status[port]=0;}continue;}
            if(!status[port]){++errors;continue;}
            if(!have[port]){message[port][0]=status[port];have[port]=1;}
            message[port][have[port]++]=b;
            if(have[port]==need[port]){emit(port,message[port].data(),need[port]);have[port]=0;if(status[port]>=0xf0)status[port]=0;}
        }
    }
    static unsigned __stdcall run(void* pointer){
        auto& r=*static_cast<MidiPortRouter*>(pointer);std::vector<unsigned char> payload;
        try{payload.resize(1024*1024);}catch(const std::bad_alloc&){++r.errors;return 1;}
        DWORD task=0;HANDLE scheduling=AvSetMmThreadCharacteristicsW(L"Pro Audio",&task);
        while(!r.stopping.load()){
            for(auto& input:r.inputs)for(unsigned i=0;i<input->headers.size();++i)if(!r.closing.load()&&input->returned[i].exchange(false)){
                input->headers[i].dwBytesRecorded=0;if(midiInAddBuffer(input->handle,&input->headers[i],sizeof(MIDIHDR)))++r.errors;
            }
            Frame frame;bool found=false;
            AcquireSRWLockExclusive(&r.lock);
            if(r.count){frame=r.frames[r.head];const unsigned first=std::min(frame.size,static_cast<unsigned>(r.bytes.size())-r.read);
                std::memcpy(payload.data(),r.bytes.data()+r.read,first);if(first<frame.size)std::memcpy(payload.data()+first,r.bytes.data(),frame.size-first);
                r.read=(r.read+frame.size)%static_cast<unsigned>(r.bytes.size());r.used-=frame.size;r.head=(r.head+1)%static_cast<unsigned>(r.frames.size());--r.count;r.busy.store(true);found=true;}
            ReleaseSRWLockExclusive(&r.lock);
            if(!found){WaitForSingleObject(r.wake,10);continue;}
            try{r.output(frame.port,payload.data(),frame.size);}catch(const std::bad_alloc&){++r.losses;r.sysex[frame.port].clear();}
            r.busy.store(false);SetEvent(r.wake);
        }
        if(scheduling)AvRevertMmThreadCharacteristics(scheduling);return 0;
    }
    bool start(){
        thread=reinterpret_cast<HANDLE>(_beginthreadex(nullptr,0,&run,this,0,nullptr));if(!thread)return false;
        for(auto& input:inputs)if(midiInStart(input->handle)){++errors;return false;}return true;
    }
    bool close(){
        const auto deadline=GetTickCount64()+5000;closing.store(true);
        auto drain=[&](){for(;;){AcquireSRWLockShared(&lock);const bool empty=count==0&&!busy.load();ReleaseSRWLockShared(&lock);
            if(empty)return true;if(GetTickCount64()>=deadline)return false;SetEvent(wake);Sleep(1);}};
        if(thread&&WaitForSingleObject(thread,0)==WAIT_TIMEOUT){
            if(!drain())return false;
            for(unsigned port=0;port<8;++port)if(outputMask.load()&(1u<<port)){
                unsigned char silence[144]{};unsigned size=0;
                for(unsigned channel=0;channel<16;++channel)for(unsigned cc:{64u,120u,123u}){
                    silence[size++]=static_cast<unsigned char>(0xb0u|channel);silence[size++]=static_cast<unsigned char>(cc);silence[size++]=0;}
                push(port,silence,size);
            }
            if(!drain())return false;
        }
        stopping.store(true);SetEvent(wake);
        // Retain all callback buffers/handles if a driver has not returned.
        if(thread&&WaitForSingleObject(thread,5000)!=WAIT_OBJECT_0)return false;
        if(thread){CloseHandle(thread);thread=nullptr;}
        for(auto& port:virtualHandles)for(auto& pipe:port)if(pipe&&pipe!=INVALID_HANDLE_VALUE){CloseHandle(pipe);pipe=nullptr;}
        for(auto& input:inputs){if(!input->handle)continue;midiInStop(input->handle);midiInReset(input->handle);
            for(auto& h:input->headers)if((h.dwFlags&MHDR_PREPARED)&&midiInUnprepareHeader(input->handle,&h,sizeof(h)))return false;
            if(midiInClose(input->handle))return false;input->handle=nullptr;}
        inputs.clear();
        for(auto& out:outputs){if(!out.handle)continue;midiOutReset(out.handle);if(midiOutClose(out.handle))return false;out.handle=nullptr;}outputs.clear();return true;
    }
    ~MidiPortRouter(){if(wake)CloseHandle(wake);}
};
static SRWLOCK midiRouterLock=SRWLOCK_INIT;
static MidiPortRouter* midiRouter=nullptr;
static void midiRouteOutput(void* execution,unsigned port,const unsigned char* data,unsigned size){
    AcquireSRWLockShared(&midiRouterLock);
    if(midiRouter&&!midiRouter->closing.load()&&midiRouter->execution==execution&&(midiRouter->outputMask&(1u<<port)))midiRouter->push(port,data,size);
    ReleaseSRWLockShared(&midiRouterLock);
}
static void midiRouteSemantic(void* execution,unsigned channel,unsigned event){
    const unsigned cc=event==MIDI_EVENT_SOUNDOFF?120:event==MIDI_EVENT_NOTESOFF?123:event==MIDI_EVENT_RESET?121:0;
    if(!cc||channel>=128)return;
    const unsigned char data[]{static_cast<unsigned char>(0xb0u|(channel&15u)),static_cast<unsigned char>(cc),0};
    midiRouteOutput(execution,channel/16,data,3);
}
extern "C" __declspec(dllexport) unsigned __cdecl midi_ports_version(){return 1;}
extern "C" __declspec(dllexport) unsigned __cdecl midi_ports_count(unsigned input){return input?midiInGetNumDevs():midiOutGetNumDevs();}
extern "C" __declspec(dllexport) int __cdecl midi_ports_name(unsigned input,unsigned device,wchar_t* name,unsigned capacity){
    if(!name||capacity<32)return 0;
    if(input){MIDIINCAPSW caps{};if(midiInGetDevCapsW(device,&caps,sizeof(caps)))return 0;wcsncpy_s(name,capacity,caps.szPname,_TRUNCATE);}
    else{MIDIOUTCAPSW caps{};if(midiOutGetDevCapsW(device,&caps,sizeof(caps)))return 0;wcsncpy_s(name,capacity,caps.szPname,_TRUNCATE);}return 1;
}
extern "C" __declspec(dllexport) void* __cdecl midi_ports_create(void* execution){
    if(!execution)return nullptr;AcquireSRWLockExclusive(&midiRouterLock);
    if(midiRouter){ReleaseSRWLockExclusive(&midiRouterLock);return nullptr;}
    try{midiRouter=new MidiPortRouter(execution);}catch(const std::bad_alloc&){midiRouter=nullptr;}
    auto* result=midiRouter;ReleaseSRWLockExclusive(&midiRouterLock);return result;
}
extern "C" __declspec(dllexport) int __cdecl midi_ports_input(void* p,unsigned device,unsigned port){return p&&static_cast<MidiPortRouter*>(p)->addInput(device,port);}
extern "C" __declspec(dllexport) int __cdecl midi_ports_output(void* p,unsigned device,unsigned port){return p&&static_cast<MidiPortRouter*>(p)->addOutput(device,port);}
extern "C" __declspec(dllexport) int __cdecl midi_ports_virtual(void* p,unsigned port,unsigned enabled){
    if(!p||port>=8)return 0;auto& r=*static_cast<MidiPortRouter*>(p);r.virtualOut[port]=enabled!=0;if(enabled)r.outputMask|=1u<<port;return 1;}
extern "C" __declspec(dllexport) int __cdecl midi_ports_start(void* p){return p&&static_cast<MidiPortRouter*>(p)->start();}
extern "C" __declspec(dllexport) unsigned __cdecl midi_ports_stats(void* p,unsigned long long* values){
    if(!p||!values)return 0;auto& r=*static_cast<MidiPortRouter*>(p);
    const unsigned long long v[]{r.received.load(),r.sent.load(),r.losses.load(),r.errors.load()};std::copy_n(v,4,values);return 4;
}
extern "C" __declspec(dllexport) int __cdecl midi_ports_release(void* p){
    if(!p)return 1;auto* router=static_cast<MidiPortRouter*>(p);
    AcquireSRWLockExclusive(&midiRouterLock);if(midiRouter==router)midiRouter=nullptr;ReleaseSRWLockExclusive(&midiRouterLock);
    if(!router->close())return 0;delete router;return 1;
}
