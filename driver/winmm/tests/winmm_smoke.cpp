#include <windows.h>
#include <mmsystem.h>
#include <mmddk.h>
#include <cstdio>
#include <vector>
#include <array>
#include <cstring>
#include <algorithm>
#include <initializer_list>

using ModMessage = DWORD(WINAPI*)(UINT, UINT, DWORD_PTR, DWORD_PTR, DWORD_PTR);

volatile LONG completedBuffers = 0;
using DriverCallbackType=BOOL(WINAPI*)(DWORD_PTR,DWORD,HDRVR,DWORD,DWORD_PTR,DWORD_PTR,DWORD_PTR);
DriverCallbackType originalDriverCallback=nullptr;
BOOL WINAPI TraceDriverCallback(DWORD_PTR callback,DWORD flags,HDRVR device,DWORD message,DWORD_PTR user,DWORD_PTR first,DWORD_PTR second)
{
    if (message==MOM_DONE)
    {
        HMODULE module=nullptr; wchar_t path[MAX_PATH]{};
        GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,reinterpret_cast<LPCWSTR>(callback),&module);
        GetModuleFileNameW(module,path,MAX_PATH);
        auto* header=reinterpret_cast<MIDIHDR*>(first);
        std::wprintf(L"done_before:callback_module=%ls;offset=%llu;flags=%lu;dwUser=%llu;reserved0=%llu;reserved1=%llu;next=%llu\n",path,
            static_cast<unsigned long long>(callback-reinterpret_cast<DWORD_PTR>(module)),header->dwFlags,
            static_cast<unsigned long long>(header->dwUser),static_cast<unsigned long long>(header->dwReserved[0]),
            static_cast<unsigned long long>(header->dwReserved[1]),static_cast<unsigned long long>(reinterpret_cast<DWORD_PTR>(header->lpNext)));
    }
    const BOOL result=originalDriverCallback(callback,flags,device,message,user,first,second);
    std::printf("driver_callback:msg=%lu;flags=%lu;callback=%llu;device=%llu;user=%llu;first=%llu;result=%d\n",
        message,flags,static_cast<unsigned long long>(callback),static_cast<unsigned long long>(reinterpret_cast<DWORD_PTR>(device)),
        static_cast<unsigned long long>(user),static_cast<unsigned long long>(first),result);
    std::fflush(stdout); return result;
}
bool TraceRegisteredCallbacks()
{
    wchar_t path[MAX_PATH]{}; if (!GetSystemDirectoryW(path,MAX_PATH)) return false;
    wcscat_s(path,L"\\ABMSynthMapper.drv");
    HMODULE module=LoadLibraryW(path); if (!module) return false;
    auto* base=reinterpret_cast<BYTE*>(module);
    auto* dos=reinterpret_cast<IMAGE_DOS_HEADER*>(base);
    auto* nt=reinterpret_cast<IMAGE_NT_HEADERS*>(base+dos->e_lfanew);
    auto* imports=reinterpret_cast<IMAGE_IMPORT_DESCRIPTOR*>(base+nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT].VirtualAddress);
    for (;imports->Name;++imports)
    {
        if (_stricmp(reinterpret_cast<char*>(base+imports->Name),"WINMM.dll")) continue;
        auto* names=reinterpret_cast<IMAGE_THUNK_DATA*>(base+imports->OriginalFirstThunk);
        auto* addresses=reinterpret_cast<IMAGE_THUNK_DATA*>(base+imports->FirstThunk);
        for (;names->u1.AddressOfData;++names,++addresses)
        {
            if (IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal)) continue;
            auto* name=reinterpret_cast<IMAGE_IMPORT_BY_NAME*>(base+names->u1.AddressOfData);
            if (std::strcmp(reinterpret_cast<char*>(name->Name),"DriverCallback")) continue;
            auto* slot=reinterpret_cast<void**>(&addresses->u1.Function);
            originalDriverCallback=reinterpret_cast<DriverCallbackType>(*slot);
            DWORD protection=0; if (!VirtualProtect(slot,sizeof(void*),PAGE_READWRITE,&protection)) return false;
            *slot=reinterpret_cast<void*>(&TraceDriverCallback);
            DWORD ignored=0; VirtualProtect(slot,sizeof(void*),protection,&ignored);
            return true;
        }
    }
    return false;
}
void CALLBACK Completed(HMIDIOUT, UINT message, DWORD_PTR, DWORD_PTR, DWORD_PTR)
{
    if (message == MOM_DONE) InterlockedIncrement(&completedBuffers);
}

// Real registered WinMM ABI, not modMessage called directly. The receiver
// audit consumes bytes without synthesizing, so this emits no audible notes.
int RegisteredProbe(bool trace=false)
{
    if (trace && !TraceRegisteredCallbacks()) return 111;
    UINT device=0xffffffff;
    for (UINT i=0;i<midiOutGetNumDevs();++i)
    {
        MIDIOUTCAPSW caps{};
        if (!midiOutGetDevCapsW(i,&caps,sizeof(caps)) && wcscmp(caps.szPname,L"ABMSynth MIDI In")==0) { device=i; break; }
    }
    if (device==0xffffffff) return 101;
    DWORD before=0,after=0; GetProcessHandleCount(GetCurrentProcess(),&before);
    for (UINT selected : {device,static_cast<UINT>(MIDI_MAPPER)})
    {
        HMIDIOUT output{};
        const MMRESULT opened=midiOutOpen(&output,selected,reinterpret_cast<DWORD_PTR>(&Completed),0,CALLBACK_FUNCTION);
        if (opened) return 102;
        if (midiOutShortMsg(output,0x004907b0)) return 103;
        std::vector<char> bytes(1024*1024,0x55);
        bytes.front()=static_cast<char>(0xf0); bytes.back()=static_cast<char>(0xf7);
        MIDIHDR header{}; header.lpData=bytes.data(); header.dwBufferLength=static_cast<DWORD>(bytes.size());
        if (midiOutPrepareHeader(output,&header,sizeof(header))) return 104;
        const LONG completed=InterlockedCompareExchange(&completedBuffers,0,0);
        LARGE_INTEGER frequency{},start{},end{}; QueryPerformanceFrequency(&frequency); QueryPerformanceCounter(&start);
        const MMRESULT sent=midiOutLongMsg(output,&header,sizeof(header)); QueryPerformanceCounter(&end);
        if (sent) return 105;
        const ULONGLONG until=GetTickCount64()+5000;
        while ((!(InterlockedCompareExchange(reinterpret_cast<volatile LONG*>(&header.dwFlags),0,0)&MHDR_DONE)
                || InterlockedCompareExchange(&completedBuffers,0,0)==completed) && GetTickCount64()<until) Sleep(1);
        if (!(header.dwFlags&MHDR_DONE) || InterlockedCompareExchange(&completedBuffers,0,0)!=completed+1)
        {
            std::printf("registered:completion_failure:id=%u;flags=%lu;before=%ld;callbacks=%ld\n",selected,header.dwFlags,completed,InterlockedCompareExchange(&completedBuffers,0,0));
            return 106;
        }
        if (midiOutUnprepareHeader(output,&header,sizeof(header))) return 107;
        std::fill(bytes.begin(),bytes.end(),static_cast<char>(0x11));
        // MOM_DONE is not receiver acknowledgement. The audit controller
        // confirms receipt before testing Reset (which cancels pending data).
        std::puts("registered-buffer-returned"); std::fflush(stdout); std::getchar();
        if (midiOutReset(output)) return 108;
        if (midiOutShortMsg(output,0x005807b0)) return 109;
        if (midiOutClose(output)) return 110;
        std::printf("registered:id=%u;long_caller_ms=%.6f;done=1;reset=0;close=0\n",selected,
            static_cast<double>(end.QuadPart-start.QuadPart)*1000/frequency.QuadPart); std::fflush(stdout);
    }
    GetProcessHandleCount(GetCurrentProcess(),&after);
    std::printf("registered:handles_before=%lu;after=%lu\n",before,after);
    // WinMM owns process-lifetime loader/cache handles, separate from our
    // private direct-driver100-cycle leak oracle. Do not call first-open delta
    // a driver leak or silently impose the direct-DLL handle count here.
    return 0;
}

// Actual registered WinMM sender for normal-launch physical acceptance.
// No LoadLibrary/modMessage shortcut, import patch, registry write or Reset.
// The receiver must acknowledge its PCM tail before releasing the close gate.
int RegisteredTimingProbe(bool burst)
{
    UINT device=0xffffffff;
    for (UINT i=0;i<midiOutGetNumDevs();++i)
    {
        MIDIOUTCAPSW caps{};
        if (!midiOutGetDevCapsW(i,&caps,sizeof(caps)) && wcscmp(caps.szPname,L"ABMSynth MIDI In")==0)
        {
            if (device!=0xffffffff) return 131; // Ambiguous endpoint, do not guess.
            device=i;
        }
    }
    if (device==0xffffffff) return 132;
    HMIDIOUT output{};
    const MMRESULT opened=midiOutOpen(&output,device,0,0,CALLBACK_NULL);
    if (opened) { std::printf("winmm-open-error:%u\n",opened); return 133; }
    // Prove the module WinMM actually loaded in this process. Do not load a
    // second DLL just to produce an apparently matching provenance string.
    HMODULE module=GetModuleHandleW(L"ABMSynthMapper.drv");
    if (!module) module=GetModuleHandleW(L"ABMSynthMidi.drv");
    wchar_t loaded[MAX_PATH]{};
    if (!module || !GetModuleFileNameW(module,loaded,MAX_PATH))
    {
        midiOutClose(output); return 134;
    }
    LARGE_INTEGER frequency{}, begin{}, now{};
    QueryPerformanceFrequency(&frequency);
    std::printf("winmm-device:%u\nproducer-pid:%lu\nfrequency:%llu\nburst:%u\n",device,GetCurrentProcessId(),
        static_cast<unsigned long long>(frequency.QuadPart),burst ? 2048u : 0u);
    std::wprintf(L"winmm-loaded-module:%ls\n",loaded);
    std::puts("winmm-ready"); std::fflush(stdout);
    if (std::getchar()==EOF) { midiOutClose(output); return 135; }
    HANDLE timer=CreateWaitableTimerW(nullptr,FALSE,nullptr);
    if (!timer) { midiOutClose(output); return 136; }
    QueryPerformanceCounter(&begin);
    unsigned sequence=0;
    auto send=[&](DWORD message,LONGLONG due) {
        LARGE_INTEGER before{},after{}; QueryPerformanceCounter(&before);
        const MMRESULT result=midiOutShortMsg(output,message);
        QueryPerformanceCounter(&after);
        std::printf("winmm-timed:%u;%llu;%llu;%llu;%lu;%u\n",sequence++,
            static_cast<unsigned long long>(due),static_cast<unsigned long long>(before.QuadPart),
            static_cast<unsigned long long>(after.QuadPart),message,result);
        return result;
    };
    MMRESULT error=0;
    for (unsigned tick=0;tick<150 && !error;++tick)
    {
        const LONGLONG due=begin.QuadPart+static_cast<LONGLONG>(tick)*frequency.QuadPart/50;
        QueryPerformanceCounter(&now);
        if (due>now.QuadPart)
        {
            LARGE_INTEGER delay{};delay.QuadPart=-std::max<LONGLONG>(1,(due-now.QuadPart)*10000000/frequency.QuadPart);
            if (!SetWaitableTimer(timer,&delay,0,nullptr,nullptr,FALSE)) { error=137; break; }
            WaitForSingleObject(timer,INFINITE);
        }
        if (burst && tick==35)
            for (unsigned index=0;index<2048 && !error;++index)
                error=send(0xb0|(2u<<8)|((index%128)<<16),due);
        if (!error) error=send(0xb0|(2u<<8)|((tick%128)<<16),due);
        if (tick%10==0 && !error) error=send(0x90|((60+tick/10)<<8)|(24u<<16),due);
        if (tick%10==5 && !error) error=send(0x80|((60+tick/10)<<8),due);
    }
    if (!error) error=send(0xb0|(120u<<8),begin.QuadPart+3*frequency.QuadPart);
    CloseHandle(timer);
    std::printf("winmm-sent:%u;error:%u\n",sequence,error);
    std::puts("winmm-tail-ready");std::fflush(stdout);
    // Successful midiOutShortMsg is only driver admission. A controller
    // independently observes Note Off/CC120 and PCM before allowing Close.
    if (std::getchar()==EOF && !error) error=138;
    const MMRESULT closed=midiOutClose(output);
    std::printf("winmm-close:%u\n",closed);std::fflush(stdout);
    return error || closed ? 139 : 0;
}

volatile LONG portShorts=0,portLongs=0,portBytes=0;
volatile LONG portOrderErrors=0;
void CALLBACK PortInputCallback(HMIDIIN,UINT kind,DWORD_PTR verify,DWORD_PTR one,DWORD_PTR){
    if(verify&&kind==MIM_DATA){const LONG index=portShorts;
        if((one&0xffff)!=0x4ab3||((one>>16)&127)!=static_cast<DWORD_PTR>(index%128))InterlockedIncrement(&portOrderErrors);}
    if(kind==MIM_DATA)InterlockedIncrement(&portShorts);
    if(kind==MIM_LONGDATA){auto* header=reinterpret_cast<MIDIHDR*>(one);InterlockedExchangeAdd(&portBytes,header->dwBytesRecorded);InterlockedIncrement(&portLongs);}
}

// Generic Windows endpoint pair. With loopback fixtures this exercises the
// public WinMM path through native input, actor and native output, not a
// direct callback invocation. No MIDI notes or audible output are sent.
int EndpointBridgeProbe(UINT inputDevice,UINT outputDevice){
    HMIDIIN input=nullptr;HMIDIOUT output=nullptr;
    MIDIINCAPSW in{};MIDIOUTCAPSW out{};
    if(midiInGetDevCapsW(inputDevice,&in,sizeof(in))||midiOutGetDevCapsW(outputDevice,&out,sizeof(out)))return 190;
    std::wprintf(L"bridge-input:%ls\nbridge-output:%ls\n",in.szPname,out.szPname);
    if(midiInOpen(&input,inputDevice,reinterpret_cast<DWORD_PTR>(&PortInputCallback),1,CALLBACK_FUNCTION))return 191;
    std::array<char,64> data{};MIDIHDR received{};received.lpData=data.data();received.dwBufferLength=static_cast<DWORD>(data.size());
    if(midiInPrepareHeader(input,&received,sizeof(received))||midiInAddBuffer(input,&received,sizeof(received))||midiInStart(input))return 192;
    if(midiOutOpen(&output,outputDevice,0,0,CALLBACK_NULL))return 193;
    std::puts("bridge-ready");std::fflush(stdout);if(std::getchar()==EOF)return 194;
    for(unsigned i=0;i<512;++i)if(midiOutShortMsg(output,0xb3|(74<<8)|((i%128)<<16)))return 195;
    char sysex[]{static_cast<char>(0xf0),0x7d,1,2,3,static_cast<char>(0xf7)};
    MIDIHDR sent{};sent.lpData=sysex;sent.dwBufferLength=sizeof(sysex);
    if(midiOutPrepareHeader(output,&sent,sizeof(sent))||midiOutLongMsg(output,&sent,sizeof(sent)))return 196;
    const ULONGLONG deadline=GetTickCount64()+10000;
    while((portShorts<512||portLongs<1||!(sent.dwFlags&MHDR_DONE))&&GetTickCount64()<deadline)Sleep(1);
    const bool exact=portShorts==512&&portLongs==1&&portBytes==6&&!portOrderErrors&&std::memcmp(data.data(),sysex,6)==0;
    std::printf("bridge:shorts=%ld;longs=%ld;bytes=%ld;order_errors=%ld\n",portShorts,portLongs,portBytes,portOrderErrors);std::fflush(stdout);
    if(!(sent.dwFlags&MHDR_DONE))midiOutReset(output);
    if(midiOutUnprepareHeader(output,&sent,sizeof(sent))||midiOutClose(output))return 197;
    midiInStop(input);midiInReset(input);
    if(midiInUnprepareHeader(input,&received,sizeof(received))||midiInClose(input))return 198;
    return exact?0:199;
}
int PortEndpointProbe(ModMessage mid,ModMessage mod,bool listen=false){
    for(unsigned port=0;port<8;++port){MIDIINCAPSW in{};MIDIOUTCAPSW out{};
        if(mid(port,MIDM_GETDEVCAPS,0,reinterpret_cast<DWORD_PTR>(&in),sizeof(in))||mod(port,MODM_GETDEVCAPS,0,reinterpret_cast<DWORD_PTR>(&out),sizeof(out)))return 170;
        if(!wcsstr(in.szPname,L"Out")||!wcsstr(out.szPname,L"In"))return 171;
    }
    DWORD_PTR user=0;MIDIOPENDESC description{};description.hMidi=reinterpret_cast<HMIDI>(1);description.dwCallback=reinterpret_cast<DWORD_PTR>(&PortInputCallback);
    if(mid(3,MIDM_OPEN,reinterpret_cast<DWORD_PTR>(&user),reinterpret_cast<DWORD_PTR>(&description),CALLBACK_FUNCTION))return 172;
    std::array<char,64> data{};MIDIHDR header{};header.lpData=data.data();header.dwBufferLength=static_cast<DWORD>(data.size());
    if(mid(3,MIDM_PREPARE,user,reinterpret_cast<DWORD_PTR>(&header),sizeof(header))||mid(3,MIDM_ADDBUFFER,user,reinterpret_cast<DWORD_PTR>(&header),sizeof(header)))return 173;
    if(mid(3,MIDM_CLOSE,user,0,0)!=MIDIERR_STILLPLAYING)return 174;
    if(mid(3,MIDM_START,user,0,0))return 175;
    std::puts("ports-ready");std::fflush(stdout);
    const wchar_t* name=L"\\\\.\\pipe\\ABMSynthMidiOut3-0";
    if(!listen){
    HANDLE pipe=CreateFileW(name,GENERIC_WRITE,0,nullptr,OPEN_EXISTING,0,nullptr);if(pipe==INVALID_HANDLE_VALUE)return 176;
    auto send=[&](const std::vector<BYTE>& wire){DWORD written=0;DWORD fields[]{0x3144494d,static_cast<DWORD>(wire.size())};
        return WriteFile(pipe,fields,sizeof(fields),&written,nullptr)&&written==sizeof(fields)&&WriteFile(pipe,wire.data(),static_cast<DWORD>(wire.size()),&written,nullptr)&&written==wire.size();};
    for(unsigned i=0;i<256;++i)if(!send({0x90,60,90})||!send({0x80,60,0}))return 177;
    if(!send({0xf0,0x7e,0x7f,9,1,0xf7}))return 178;
    CloseHandle(pipe);
    }
    DWORD start=GetTickCount();while((portShorts<512||portLongs<1)&&GetTickCount()-start<3000)Sleep(1);
    if(portShorts!=512||portLongs!=1||portBytes!=6)return 179;
    if(mid(3,MIDM_UNPREPARE,user,reinterpret_cast<DWORD_PTR>(&header),sizeof(header)))return 180;
    if(mid(3,MIDM_RESET,user,0,0)||mid(3,MIDM_CLOSE,user,0,0))return 181;
    std::puts("ports: PASS eight addresses, In/Out names,512 ordered short messages,SysEx,queued-buffer Close refusal,Reset/close");return 0;
}

int wmain(int argc, wchar_t* argv[])
{
    if(argc==4&&wcscmp(argv[1],L"--endpoint-bridge")==0)return EndpointBridgeProbe(static_cast<UINT>(_wtoi(argv[2])),static_cast<UINT>(_wtoi(argv[3])));
    if (argc==2 && wcscmp(argv[1],L"--winmm-timing")==0) return RegisteredTimingProbe(false);
    if (argc==2 && wcscmp(argv[1],L"--winmm-timing-burst")==0) return RegisteredTimingProbe(true);
    if (argc==2 && wcscmp(argv[1],L"--load-mapper")==0)
    {
        HMODULE mapper=LoadLibraryW(L"midimap.dll"); if (!mapper) return 112;
        DebugBreak(); FreeLibrary(mapper); return 0;
    }
    if (argc==2 && wcscmp(argv[1],L"--registered")==0) return RegisteredProbe();
    if (argc==2 && wcscmp(argv[1],L"--registered-trace")==0) return RegisteredProbe(true);
    if (argc != 2 && argc != 3)
        return 64;

    HMODULE module = LoadLibraryW(argv[1]);
    if (!module)
        return 65;
    auto modMessage = reinterpret_cast<ModMessage>(GetProcAddress(module, "modMessage"));
    if (!modMessage)
        return 66;
    if(argc==3&&(wcscmp(argv[2],L"--ports")==0||wcscmp(argv[2],L"--ports-listen")==0)){
        auto mid=reinterpret_cast<ModMessage>(GetProcAddress(module,"midMessage"));return mid?PortEndpointProbe(mid,modMessage,wcscmp(argv[2],L"--ports-listen")==0):169;}

    if (argc==3 && wcscmp(argv[2],L"--cycles")==0)
    {
        DWORD before=0,after=0;
        GetProcessHandleCount(GetCurrentProcess(),&before);
        for (unsigned iteration=0;iteration<100;++iteration)
        {
            DWORD_PTR handle=0; MIDIOPENDESC description{};
            if (modMessage(0,MODM_OPEN,reinterpret_cast<DWORD_PTR>(&handle),reinterpret_cast<DWORD_PTR>(&description),0)) return 81;
            const DWORD message=0xb0 | (7<<8) | ((iteration%128)<<16);
            if (modMessage(0,MODM_DATA,handle,message,0)) return 82;
            if (modMessage(0,MODM_CLOSE,handle,0,0)) return 83;
        }
        GetProcessHandleCount(GetCurrentProcess(),&after);
        std::printf("cycles:before=%lu;after=%lu;count=100\n",before,after); std::fflush(stdout);
        FreeLibrary(module);
        return after>before+1 ? 84 : 0;
    }

    DWORD_PTR instance = 0;
    MIDIOPENDESC openDesc{};
    openDesc.dwCallback = reinterpret_cast<DWORD_PTR>(&Completed);
    if (modMessage(0, MODM_OPEN, reinterpret_cast<DWORD_PTR>(&instance), reinterpret_cast<DWORD_PTR>(&openDesc), CALLBACK_FUNCTION) != MMSYSERR_NOERROR)
        return 67;
    if(argc==3 && (wcscmp(argv[2],L"--loss-detail-short")==0 || wcscmp(argv[2],L"--loss-detail-long")==0))
    {
        if(modMessage(0,MODM_DATA,instance,0x004907b0,0))return 151;
        std::puts("loss-ready");std::fflush(stdout);std::getchar();
        DWORD result=0;
        if(wcscmp(argv[2],L"--loss-detail-short")==0)
            result=modMessage(0,MODM_DATA,instance,0x00643c90,0);
        else {
            std::vector<char> data{static_cast<char>(0x91),62,96,static_cast<char>(0xb2),2,37,
                static_cast<char>(0xe3),0,65,static_cast<char>(0xf0)};
            data.insert(data.end(),64,0x55);data.push_back(static_cast<char>(0xf7));
            MIDIHDR header{};header.lpData=data.data();header.dwBufferLength=static_cast<DWORD>(data.size());
            if(modMessage(0,MODM_PREPARE,instance,reinterpret_cast<DWORD_PTR>(&header),sizeof(header)))return 152;
            result=modMessage(0,MODM_LONGDATA,instance,reinterpret_cast<DWORD_PTR>(&header),sizeof(header));
            if(modMessage(0,MODM_UNPREPARE,instance,reinterpret_cast<DWORD_PTR>(&header),sizeof(header)))return 153;
        }
        std::printf("loss-admitted:%lu\n",result);std::fflush(stdout);std::getchar();
        const DWORD closed=modMessage(0,MODM_CLOSE,instance,0,0);FreeLibrary(module);
        return result || closed ? 154 : 0;
    }
    if (argc==3 && (wcscmp(argv[2],L"--origin")==0 || wcscmp(argv[2],L"--origin-tail")==0))
    {
        if(modMessage(0,MODM_DATA,instance,0x000001b0,0))return 125;
        std::puts("origin-ready");std::fflush(stdout);std::getchar();
        for(unsigned index=0;index<512;++index)
            if(modMessage(0,MODM_DATA,instance,0xb0|(2u<<8)|((index%128)<<16),0))return 126;
        auto note=[&](unsigned key){
            LARGE_INTEGER before{},after{};QueryPerformanceCounter(&before);
            const DWORD result=modMessage(0,MODM_DATA,instance,0x90|(key<<8)|(24u<<16),0);
            QueryPerformanceCounter(&after);
            std::printf("origin-note:%u;%llu;%llu;%lu\n",key,
                static_cast<unsigned long long>(before.QuadPart),static_cast<unsigned long long>(after.QuadPart),result);
            std::fflush(stdout);return result;
        };
        if(note(60))return 127;
        std::puts("origin-queued");std::fflush(stdout);std::getchar();
        if(note(62))return 128;
        if(wcscmp(argv[2],L"--origin-tail")==0){
            std::puts("origin-tail-ready");std::fflush(stdout);std::getchar();
            // Receiver was removed after healthy notes; no following frame can
            // carry this final accepted write failure back to the observer.
            if(modMessage(0,MODM_DATA,instance,0x00003e80,0))return 130;
        }
        const DWORD closed=modMessage(0,MODM_CLOSE,instance,0,0);
        FreeLibrary(module);return closed?129:0;
    }
    if (argc==3 && (wcscmp(argv[2],L"--long-entry")==0 || wcscmp(argv[2],L"--long-entry-tail")==0))
    {
        // Non-installed probe: real MODM_LONGDATA entry bracket and caller
        // buffer ownership. Driver transport/protocol/source is unchanged.
        auto sendLong=[&](unsigned index,std::initializer_list<BYTE> bytes)->DWORD {
            std::vector<BYTE> payload(bytes); MIDIHDR header{};
            header.lpData=reinterpret_cast<LPSTR>(payload.data());
            header.dwBufferLength=static_cast<DWORD>(payload.size());
            const DWORD prepared=modMessage(0,MODM_PREPARE,instance,reinterpret_cast<DWORD_PTR>(&header),sizeof(header));
            std::printf("long-prepare:%u;%lu;%lu\n",index,prepared,header.dwFlags);
            if(prepared)return prepared;
            LARGE_INTEGER before{},after{};QueryPerformanceCounter(&before);
            const DWORD sent=modMessage(0,MODM_LONGDATA,instance,reinterpret_cast<DWORD_PTR>(&header),sizeof(header));
            QueryPerformanceCounter(&after);
            const DWORD flags=header.dwFlags;
            const LONG done=InterlockedCompareExchange(&completedBuffers,0,0);
            const DWORD unprepared=modMessage(0,MODM_UNPREPARE,instance,reinterpret_cast<DWORD_PTR>(&header),sizeof(header));
            std::printf("long-entry:%u;%llu;%llu;%lu;%lu;%lu;%ld;%lu;%lu;",index,
                static_cast<unsigned long long>(before.QuadPart),static_cast<unsigned long long>(after.QuadPart),
                sent,static_cast<DWORD>(payload.size()),flags,done,unprepared,header.dwFlags);
            for(BYTE value:payload)std::printf("%02x",static_cast<unsigned>(value));
            std::puts("");std::fflush(stdout);
            // MOM_DONE returns ownership of this buffer, not PCM/transport ACK.
            // Mutate it immediately; the receiver must see the original copy.
            std::fill(payload.begin(),payload.end(),static_cast<BYTE>(0x11));
            if(sent||unprepared)return sent?sent:unprepared;
            return (!(flags&MHDR_DONE)||(flags&MHDR_INQUEUE)||done!=static_cast<LONG>(index+1)||header.dwFlags) ? 141 : 0;
        };
        if(sendLong(0,{0xb0,2,32,0x90,60,24,0xd0,32,0xe0,0,64}))return 142;
        std::puts("long-phase-ready:0");std::fflush(stdout);std::getchar();
        if(sendLong(1,{0xb0,7,73,0xb0,0,0,0xb0,32,0,0xc0,0,0xb0,1,64,0x90,62,24,0x90,64,24}))return 143;
        std::puts("long-phase-ready:1");std::fflush(stdout);std::getchar();
        if(sendLong(2,{0x80,60,0,0x80,62,0,0x80,64,0,0xb0,120,0}))return 144;
        std::puts("long-phase-ready:2");std::fflush(stdout);std::getchar();
        if(wcscmp(argv[2],L"--long-entry-tail")==0){
            if(sendLong(3,{0x80,60,0}))return 145;
        }
        const DWORD closed=modMessage(0,MODM_CLOSE,instance,0,0);
        std::printf("long-close:%lu\n",closed);std::fflush(stdout);FreeLibrary(module);
        return closed?146:0;
    }
    if (argc==3 && wcscmp(argv[2],L"--timing-3s")==0)
    {
        // Diagnostic producer in another process: GUI/Python cannot move this
        // clock. Sends only quiet notes and CC2; installed driver is unmodified.
        LARGE_INTEGER frequency{}, begin{}, now{};
        QueryPerformanceFrequency(&frequency); QueryPerformanceCounter(&begin);
        HANDLE timer=CreateWaitableTimerW(nullptr,FALSE,nullptr);
        if (!timer) return 122;
        unsigned sequence=0;
        auto send=[&](DWORD message) {
            LARGE_INTEGER before{},after{}; QueryPerformanceCounter(&before);
            const DWORD result=modMessage(0,MODM_DATA,instance,message,0);
            QueryPerformanceCounter(&after);
            std::printf("timed:%u;%llu;%llu;%lu;%lu\n",sequence++,
                static_cast<unsigned long long>(before.QuadPart),static_cast<unsigned long long>(after.QuadPart),message,result);
            return result;
        };
        std::printf("frequency:%llu\n",static_cast<unsigned long long>(frequency.QuadPart));
        DWORD error=0;
        for (unsigned tick=0;tick<150 && !error;++tick)
        {
            const LONGLONG due=begin.QuadPart+static_cast<LONGLONG>(tick)*frequency.QuadPart/50;
            QueryPerformanceCounter(&now);
            if (due>now.QuadPart) {
                LARGE_INTEGER delay{}; delay.QuadPart=-std::max<LONGLONG>(1,(due-now.QuadPart)*10000000/frequency.QuadPart);
                if (!SetWaitableTimer(timer,&delay,0,nullptr,nullptr,FALSE)) { error=123; break; }
                WaitForSingleObject(timer,INFINITE);
            }
            error=send(0xb0 | (2u<<8) | ((tick%128)<<16));
            if (tick%10==0 && !error) error=send(0x90 | ((60+tick/10)<<8) | (24u<<16));
            if (tick%10==5 && !error) error=send(0x80 | ((60+tick/10)<<8));
        }
        if (!error) error=send(0xb0 | (120u<<8));
        Sleep(100); CloseHandle(timer);
        const DWORD closed=modMessage(0,MODM_CLOSE,instance,0,0);
        std::fflush(stdout); FreeLibrary(module);
        return error || closed ? 124 : 0;
    }
    if (argc==3 && (wcscmp(argv[2],L"--reconnect")==0 || wcscmp(argv[2],L"--reconnect-gap")==0))
    {
        if (modMessage(0,MODM_DATA,instance,0x004107b0,0)) return 120;
        std::puts("reconnect-ready"); std::fflush(stdout); std::getchar();
        if (wcscmp(argv[2],L"--reconnect-gap")==0)
        {
            DWORD failed = 0;
            const ULONGLONG until = GetTickCount64()+2000;
            while (!failed && GetTickCount64()<until)
            {
                failed = modMessage(0,MODM_DATA,instance,0x004207b0,0);
                Sleep(1);
            }
            if (failed != MIDIERR_NOTREADY) return 122;
            std::puts("reconnect-failed"); std::fflush(stdout); std::getchar();
        }
        // Controller has stopped/recreated the receiver. Same MIDI handle;
        // deliberately no MODM_RESET, Close or new Open before this message.
        const DWORD sent=modMessage(0,MODM_DATA,instance,0x005807b0,0);
        const DWORD closed=modMessage(0,MODM_CLOSE,instance,0,0);
        std::printf("reconnect:send=%lu;close=%lu\n",sent,closed); std::fflush(stdout);
        FreeLibrary(module);
        return sent || closed ? 121 : 0;
    }
    if (argc==3 && wcscmp(argv[2],L"--disconnect")==0)
    {
        if (modMessage(0,MODM_DATA,instance,0x000001b0,0)) return 85;
        std::puts("disconnect-ready"); std::fflush(stdout); std::getchar();
        DWORD result=MMSYSERR_NOERROR;
        const ULONGLONG until=GetTickCount64()+3000;
        while (!result && GetTickCount64()<until)
        {
            result=modMessage(0,MODM_DATA,instance,0x000002b0,0); Sleep(1);
        }
        if (result!=MIDIERR_NOTREADY) return 86;
        const DWORD repeated=modMessage(0,MODM_DATA,instance,0x000003b0,0);
        std::printf("disconnect-failed=%lu;repeated=%lu\n",result,repeated); std::fflush(stdout); std::getchar();
        const DWORD reset=modMessage(0,MODM_RESET,instance,0,0);
        const DWORD send=modMessage(0,MODM_DATA,instance,0x005807b0,0);
        const DWORD closed=modMessage(0,MODM_CLOSE,instance,0,0);
        std::printf("disconnect-recovery:reset=%lu;send=%lu;closed=%lu\n",reset,send,closed); std::fflush(stdout);
        FreeLibrary(module);
        return reset || send || closed || repeated!=MIDIERR_NOTREADY ? 87 : 0;
    }
    if (argc==3 && wcscmp(argv[2],L"--order")==0)
    {
        const DWORD shorts[]={0x000003c0,0x00643c90,0x00003c80};
        for (DWORD message:shorts) if (modMessage(0,MODM_DATA,instance,message,0)) return 88;
        char data[]={static_cast<char>(0xf0),0x7e,0x7f,0x09,0x01,static_cast<char>(0xf7)};
        MIDIHDR header{}; header.lpData=data; header.dwBufferLength=sizeof(data);
        if (modMessage(0,MODM_PREPARE,instance,reinterpret_cast<DWORD_PTR>(&header),sizeof(header))) return 89;
        if (modMessage(0,MODM_LONGDATA,instance,reinterpret_cast<DWORD_PTR>(&header),sizeof(header))) return 90;
        const ULONGLONG until=GetTickCount64()+3000;
        while (!(InterlockedCompareExchange(reinterpret_cast<volatile LONG*>(&header.dwFlags),0,0)&MHDR_DONE) && GetTickCount64()<until) Sleep(1);
        if (!(header.dwFlags&MHDR_DONE) || completedBuffers!=1) return 91;
        if (modMessage(0,MODM_UNPREPARE,instance,reinterpret_cast<DWORD_PTR>(&header),sizeof(header))) return 92;
        std::puts("order-buffer-returned"); std::fflush(stdout); std::getchar();
        if (modMessage(0,MODM_RESET,instance,0,0)) return 93;
        if (modMessage(0,MODM_DATA,instance,0x005807b0,0)) return 94;
        const DWORD closed=modMessage(0,MODM_CLOSE,instance,0,0);
        FreeLibrary(module); std::puts("order-complete"); return closed ? 95 : 0;
    }
    if (argc == 3 && (wcscmp(argv[2], L"--queue-contract") == 0 || wcscmp(argv[2], L"--queue-loss") == 0))
    {
        if (modMessage(0, MODM_DATA, instance, 0x000001b0, 0)) return 69;
        std::vector<char> payload(1024*1024,0x55); payload.front()=static_cast<char>(0xf0); payload.back()=static_cast<char>(0xf7);
        std::array<MIDIHDR,8> headers{};
        DWORD accepted=0, rejected=0;
        for (auto& header:headers)
        {
            header.lpData=payload.data(); header.dwBufferLength=static_cast<DWORD>(payload.size());
            if (modMessage(0, MODM_LONGDATA, instance, reinterpret_cast<DWORD_PTR>(&header),sizeof(header))!=MIDIERR_UNPREPARED) return 72;
            if (modMessage(0, MODM_PREPARE, instance, reinterpret_cast<DWORD_PTR>(&header),sizeof(header))) return 73;
            const DWORD result=modMessage(0, MODM_LONGDATA, instance, reinterpret_cast<DWORD_PTR>(&header),sizeof(header));
            if (!result) ++accepted;
            else if (result==MIDIERR_NOTREADY) ++rejected;
            else return 74;
        }
        if (!accepted || !rejected) return 75;
        for (auto& header:headers)
        {
            if (header.dwFlags&MHDR_INQUEUE) return 77;
            // Buffers are caller-owned again; native queue owns copies. The
            // retained private queue is cancelled/drained separately by Reset.
            if (modMessage(0,MODM_UNPREPARE,instance,reinterpret_cast<DWORD_PTR>(&header),sizeof(header))) return 76;
        }
        std::puts("queue-contract-ready"); std::fflush(stdout);
        if(wcscmp(argv[2],L"--queue-loss")==0)std::getchar(); // reader retains failure evidence before producer Close
        LARGE_INTEGER frequency{},begin{},end{};
        QueryPerformanceFrequency(&frequency); QueryPerformanceCounter(&begin);
        const DWORD reset=modMessage(0,MODM_RESET,instance,0,0);
        QueryPerformanceCounter(&end);
        for (auto& header:headers)
        {
            if (header.dwFlags&MHDR_INQUEUE) return 78;
            if (modMessage(0,MODM_UNPREPARE,instance,reinterpret_cast<DWORD_PTR>(&header),sizeof(header))) return 79;
        }
        const DWORD closed=modMessage(0,MODM_CLOSE,instance,0,0);
        std::printf("queue-contract:accepted=%lu;rejected=%lu;callbacks=%ld;reset=%lu;closed=%lu;reset-ms=%.6f\n",
            accepted,rejected,completedBuffers,reset,closed,(end.QuadPart-begin.QuadPart)*1000./frequency.QuadPart);
        std::fflush(stdout);
        FreeLibrary(module);
        return reset || closed || static_cast<DWORD>(completedBuffers)!=accepted ? 80 : 0;
    }
    if (argc == 3 && wcscmp(argv[2], L"--backpressure") == 0)
    {
        const DWORD first = modMessage(0, MODM_DATA, instance, 0x000001b0, 0);
        if (first) return 69;
        std::vector<char> data(1024 * 1024, 0x55);
        data.front() = static_cast<char>(0xf0); data.back() = static_cast<char>(0xf7);
        MIDIHDR header{}; header.lpData = data.data(); header.dwBufferLength = static_cast<DWORD>(data.size());
        if (modMessage(0, MODM_PREPARE, instance, reinterpret_cast<DWORD_PTR>(&header), sizeof(header))) return 70;
        std::puts("backpressure-send-start"); std::fflush(stdout);
        LARGE_INTEGER frequency{}, begin{}, end{};
        QueryPerformanceFrequency(&frequency); QueryPerformanceCounter(&begin);
        const DWORD result = modMessage(0, MODM_LONGDATA, instance, reinterpret_cast<DWORD_PTR>(&header), sizeof(header));
        QueryPerformanceCounter(&end);
        std::printf("backpressure-result=%lu;call-ms=%.6f;header-flags=%lu\n", result,
            (end.QuadPart-begin.QuadPart)*1000.0/frequency.QuadPart, header.dwFlags);
        std::fflush(stdout);
        // Mutating the returned caller buffer must not alter queued bytes.
        std::fill(data.begin(),data.end(),static_cast<char>(0x11));
        const ULONGLONG until = GetTickCount64()+5000;
        while (!(InterlockedCompareExchange(reinterpret_cast<volatile LONG*>(&header.dwFlags),0,0)&MHDR_DONE)
               && GetTickCount64()<until) Sleep(1);
        const DWORD unprepared = modMessage(0, MODM_UNPREPARE, instance, reinterpret_cast<DWORD_PTR>(&header), sizeof(header));
        const DWORD closed = modMessage(0, MODM_CLOSE, instance, 0, 0);
        FreeLibrary(module);
        return result || unprepared || closed ? 71 : 0;
    }
    const DWORD noteOn = 0x00643c90;
    const DWORD sendResult = modMessage(0, MODM_DATA, instance, noteOn, 0);
    const DWORD closeResult = modMessage(0, MODM_CLOSE, instance, 0, 0);
    FreeLibrary(module);
    return sendResult == MMSYSERR_NOERROR && closeResult == MMSYSERR_NOERROR ? 0 : 68;
}
