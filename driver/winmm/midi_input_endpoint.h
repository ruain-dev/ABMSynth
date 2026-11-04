// ABMSynth -> arbitrary WinMM host. Each open owns its buffers and one of
// eight per-port pipe slots. No host callback runs on ABMSynth's audio actor.
#include <atomic>
namespace {
struct MidiInputEndpoint {
    MIDIOPENDESC description{};DWORD callbackFlags=0;unsigned port=0,slot=8;
    HANDLE thread=nullptr,ready=CreateEventW(nullptr,TRUE,FALSE,nullptr),stop=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    SRWLOCK lock=SRWLOCK_INIT;std::vector<MIDIHDR*> buffers;
    std::atomic<bool> started{false};std::atomic<HANDLE> pipe{INVALID_HANDLE_VALUE};DWORD anchor=0;
    void notify(UINT kind,DWORD_PTR one=0,DWORD_PTR two=0){
        DriverCallback(description.dwCallback,callbackFlags,reinterpret_cast<HDRVR>(description.hMidi),kind,description.dwInstance,one,two);
    }
    bool transfer(HANDLE handle,void* data,DWORD size){
        DWORD done=0;while(done<size){
            OVERLAPPED overlap{};overlap.hEvent=CreateEventW(nullptr,TRUE,FALSE,nullptr);DWORD got=0;
            BOOL ok=ReadFile(handle,static_cast<BYTE*>(data)+done,size-done,&got,&overlap);
            if(!ok&&GetLastError()==ERROR_IO_PENDING){HANDLE wait[]{stop,overlap.hEvent};
                if(WaitForMultipleObjects(2,wait,FALSE,INFINITE)==WAIT_OBJECT_0+1)ok=GetOverlappedResult(handle,&overlap,&got,FALSE);
                else{CancelIoEx(handle,&overlap);GetOverlappedResult(handle,&overlap,&got,TRUE);}}
            CloseHandle(overlap.hEvent);if(!ok||!got)return false;done+=got;
        }return true;
    }
    void deliver(const BYTE* data,unsigned size){
        if(!started.load()||!size)return;
        const auto tick=timeGetTime()-anchor;
        if(data[0]==0xf0){
            unsigned offset=0;
            while(offset<size){
                AcquireSRWLockExclusive(&lock);MIDIHDR* header=nullptr;
                if(!buffers.empty()){header=buffers.front();buffers.erase(buffers.begin());
                    const auto copy=std::min<unsigned>(size-offset,header->dwBufferLength);std::memcpy(header->lpData,data+offset,copy);offset+=copy;header->dwBytesRecorded=copy;
                    header->dwFlags=(header->dwFlags&~MHDR_INQUEUE)|MHDR_DONE;}
                ReleaseSRWLockExclusive(&lock);
                if(!header){notify(MIM_ERROR,0,tick);return;}notify(MIM_LONGDATA,reinterpret_cast<DWORD_PTR>(header),tick);
            }
        }else{DWORD packed=0;for(unsigned i=0;i<std::min(size,3u);++i)packed|=static_cast<DWORD>(data[i])<<(i*8);notify(MIM_DATA,packed,tick);}
    }
    static DWORD WINAPI run(void* pointer){
        auto& self=*static_cast<MidiInputEndpoint*>(pointer);std::vector<BYTE> data(1024*1024);
        while(WaitForSingleObject(self.stop,0)!=WAIT_OBJECT_0){
            HANDLE handle=INVALID_HANDLE_VALUE;
            for(unsigned i=0;i<8;++i){
                if(self.slot<8&&self.slot!=i)continue;
                wchar_t name[96]{};swprintf_s(name,L"\\\\.\\pipe\\ABMSynthMidiOut%u-%u",self.port,i);
                handle=CreateNamedPipeW(name,PIPE_ACCESS_INBOUND|FILE_FLAG_OVERLAPPED|FILE_FLAG_FIRST_PIPE_INSTANCE,
                    PIPE_TYPE_BYTE|PIPE_READMODE_BYTE|PIPE_WAIT|PIPE_REJECT_REMOTE_CLIENTS,1,0,65536,0,nullptr);
                if(handle!=INVALID_HANDLE_VALUE){self.slot=i;break;}
            }
            self.pipe.store(handle);SetEvent(self.ready);if(handle==INVALID_HANDLE_VALUE)return 0;
            OVERLAPPED connection{};connection.hEvent=CreateEventW(nullptr,TRUE,FALSE,nullptr);
            BOOL ok=ConnectNamedPipe(handle,&connection);const auto error=GetLastError();
            if(!ok&&error==ERROR_PIPE_CONNECTED)ok=TRUE;
            else if(!ok&&error==ERROR_IO_PENDING){HANDLE wait[]{self.stop,connection.hEvent};
                if(WaitForMultipleObjects(2,wait,FALSE,INFINITE)==WAIT_OBJECT_0+1){DWORD ignored=0;ok=GetOverlappedResult(handle,&connection,&ignored,FALSE);}
                else{CancelIoEx(handle,&connection);DWORD ignored=0;GetOverlappedResult(handle,&connection,&ignored,TRUE);}}
            CloseHandle(connection.hEvent);
            if(ok)while(WaitForSingleObject(self.stop,0)!=WAIT_OBJECT_0){DWORD header[2]{};
                if(!self.transfer(handle,header,sizeof(header))||header[0]!=0x3144494du||!header[1]||header[1]>data.size()||!self.transfer(handle,data.data(),header[1]))break;
                self.deliver(data.data(),header[1]);}
            self.pipe.store(INVALID_HANDLE_VALUE);DisconnectNamedPipe(handle);CloseHandle(handle);
        }return 0;
    }
    void reset(){started.store(false);std::vector<MIDIHDR*> pending;
        AcquireSRWLockExclusive(&lock);pending.swap(buffers);for(auto* h:pending)h->dwFlags=(h->dwFlags&~MHDR_INQUEUE)|MHDR_DONE;ReleaseSRWLockExclusive(&lock);
        for(auto* h:pending)notify(MIM_LONGDATA,reinterpret_cast<DWORD_PTR>(h),0);
    }
    ~MidiInputEndpoint(){if(thread)CloseHandle(thread);if(ready)CloseHandle(ready);if(stop)CloseHandle(stop);}
};
}
extern "C" __declspec(dllexport) DWORD WINAPI midMessage(UINT deviceId,UINT message,DWORD_PTR user,DWORD_PTR one,DWORD_PTR two){
    if(message==MIDM_GETNUMDEVS)return IsABMSynthRunning()?PortCount(true):0;
    auto* input=reinterpret_cast<MidiInputEndpoint*>(user);
    if(message==MIDM_GETDEVCAPS){
        if(!one||deviceId>=PortCount(true))return MMSYSERR_BADDEVICEID;
        wchar_t name[MAXPNAMELEN]{};PortName(PortAddress(true,deviceId),true,name);
        if(two>=sizeof(MIDIINCAPSW)){auto* caps=reinterpret_cast<MIDIINCAPSW*>(one);*caps={};caps->wMid=1;caps->wPid=0x4142;caps->vDriverVersion=0x0100;lstrcpynW(caps->szPname,name,MAXPNAMELEN);}
        else if(two>=sizeof(MIDIINCAPSA)){auto* caps=reinterpret_cast<MIDIINCAPSA*>(one);*caps={};caps->wMid=1;caps->wPid=0x4142;caps->vDriverVersion=0x0100;PortNameAnsi(name,caps->szPname);}
        else return MMSYSERR_INVALPARAM;return MMSYSERR_NOERROR;
    }
    if(message==MIDM_OPEN){
        if(!user||!one||deviceId>=PortCount(true))return MMSYSERR_BADDEVICEID;
        auto* created=new(std::nothrow) MidiInputEndpoint;if(!created)return MMSYSERR_NOMEM;
        created->description=*reinterpret_cast<MIDIOPENDESC*>(one);created->callbackFlags=HIWORD(static_cast<DWORD>(two))&7;created->port=PortAddress(true,deviceId);
        created->thread=CreateThread(nullptr,0,&MidiInputEndpoint::run,created,0,nullptr);
        if(!created->thread){delete created;return MMSYSERR_NOMEM;}
        if(WaitForSingleObject(created->ready,2000)!=WAIT_OBJECT_0||created->slot>=8){SetEvent(created->stop);WaitForSingleObject(created->thread,INFINITE);delete created;return MMSYSERR_ALLOCATED;}
        *reinterpret_cast<DWORD_PTR*>(user)=reinterpret_cast<DWORD_PTR>(created);created->notify(MIM_OPEN);return MMSYSERR_NOERROR;
    }
    if(!input)return MMSYSERR_INVALHANDLE;
    switch(message){
    case MIDM_PREPARE:if(!one)return MMSYSERR_INVALPARAM;reinterpret_cast<MIDIHDR*>(one)->dwFlags|=MHDR_PREPARED;return 0;
    case MIDM_UNPREPARE:if(!one)return MMSYSERR_INVALPARAM;if(reinterpret_cast<MIDIHDR*>(one)->dwFlags&MHDR_INQUEUE)return MIDIERR_STILLPLAYING;reinterpret_cast<MIDIHDR*>(one)->dwFlags&=~MHDR_PREPARED;return 0;
    case MIDM_ADDBUFFER:{if(!one||two<sizeof(MIDIHDR))return MMSYSERR_INVALPARAM;auto* h=reinterpret_cast<MIDIHDR*>(one);
        if(!(h->dwFlags&MHDR_PREPARED)||!h->lpData||!h->dwBufferLength)return MIDIERR_UNPREPARED;
        AcquireSRWLockExclusive(&input->lock);if(h->dwFlags&MHDR_INQUEUE){ReleaseSRWLockExclusive(&input->lock);return MIDIERR_STILLPLAYING;}
        h->dwBytesRecorded=0;h->dwFlags=(h->dwFlags&~MHDR_DONE)|MHDR_INQUEUE;input->buffers.push_back(h);ReleaseSRWLockExclusive(&input->lock);return 0;}
    case MIDM_START:input->anchor=timeGetTime();input->started.store(true);return 0;
    case MIDM_STOP:input->started.store(false);return 0;
    case MIDM_RESET:input->reset();return 0;
    case MIDM_CLOSE:{AcquireSRWLockShared(&input->lock);const bool busy=!input->buffers.empty();ReleaseSRWLockShared(&input->lock);if(busy)return MIDIERR_STILLPLAYING;
        SetEvent(input->stop);if(WaitForSingleObject(input->thread,5000)!=WAIT_OBJECT_0)return MMSYSERR_ERROR;input->notify(MIM_CLOSE);delete input;return 0;}
    default:return MMSYSERR_NOTSUPPORTED;
    }
}
