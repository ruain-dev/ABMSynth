// Installed WinMM ABI acceptance. No BASS/audio/Python or private modMessage.
#define NOMINMAX
#include <windows.h>
#include <mmsystem.h>
#include <array>
#include <atomic>
#include <cstdio>
#include <mutex>
#include <thread>
#include <vector>
#include <cstring>
struct Capture{std::mutex lock;std::vector<DWORD> shorts;std::vector<unsigned char> sysex;};
static void CALLBACK received(HMIDIIN,UINT kind,DWORD_PTR context,DWORD_PTR one,DWORD_PTR){
    auto& c=*reinterpret_cast<Capture*>(context);std::lock_guard guard(c.lock);
    if(kind==MIM_DATA)c.shorts.push_back(static_cast<DWORD>(one));
    if(kind==MIM_LONGDATA){auto* h=reinterpret_cast<MIDIHDR*>(one);c.sysex.insert(c.sysex.end(),h->lpData,h->lpData+h->dwBytesRecorded);}
}
struct Receiver{
    std::array<HANDLE,2> pipes{};std::array<std::thread,2> threads;
    std::array<HANDLE,8> writers{};std::atomic<bool> stopping{false};std::atomic<unsigned> frames{0},errors{0};
    bool exact(HANDLE h,void* p,DWORD size){DWORD n=0;auto* b=static_cast<unsigned char*>(p);while(size&&!stopping){if(!ReadFile(h,b,size,&n,nullptr)||!n)return false;b+=n;size-=n;}return !size;}
    bool start(){
        for(unsigned i=0;i<pipes.size();++i){pipes[i]=CreateNamedPipeW(L"\\\\.\\pipe\\ABMSynthMidiIn",PIPE_ACCESS_INBOUND|(i?0:FILE_FLAG_FIRST_PIPE_INSTANCE),PIPE_TYPE_BYTE|PIPE_WAIT|PIPE_REJECT_REMOTE_CLIENTS,8,0,65536,0,nullptr);
            if(pipes[i]==INVALID_HANDLE_VALUE)return false;}
        for(unsigned i=0;i<pipes.size();++i)threads[i]=std::thread([this,i]{auto pipe=pipes[i];
            while(!stopping){if(!ConnectNamedPipe(pipe,nullptr)&&GetLastError()!=ERROR_PIPE_CONNECTED){if(!stopping)++errors;break;}
                while(!stopping){std::array<unsigned char,64> header{};if(!exact(pipe,header.data(),8))break;
                    DWORD magic=0,size=0;std::memcpy(&magic,header.data(),4);std::memcpy(&size,header.data()+4,4);
                    if(magic!=0x3344494d||!size||size>1024*1024){++errors;break;}
                    if(!exact(pipe,header.data()+8,56))break;DWORD flags=0;std::memcpy(&flags,header.data()+60,4);unsigned port=flags&7;
                    std::vector<unsigned char> bytes(size);if(!exact(pipe,bytes.data(),size))break;
                    auto& writer=writers[port];if(!writer||writer==INVALID_HANDLE_VALUE){wchar_t name[96]{};swprintf_s(name,L"\\\\.\\pipe\\ABMSynthMidiOut%u-0",port);writer=CreateFileW(name,GENERIC_WRITE,0,nullptr,OPEN_EXISTING,0,nullptr);}
                    DWORD fields[]{0x3144494d,size},n=0;
                    if(writer==INVALID_HANDLE_VALUE||!WriteFile(writer,fields,sizeof(fields),&n,nullptr)||n!=sizeof(fields)||!WriteFile(writer,bytes.data(),size,&n,nullptr)||n!=size){++errors;break;}++frames;
                }DisconnectNamedPipe(pipe);
            }
        });return true;
    }
    ~Receiver(){stopping=true;for(auto& t:threads)if(t.joinable())CancelSynchronousIo(t.native_handle());
        for(auto& t:threads)if(t.joinable())t.join();
        for(auto h:pipes)if(h&&h!=INVALID_HANDLE_VALUE)CloseHandle(h);
        for(auto h:writers)if(h&&h!=INVALID_HANDLE_VALUE)CloseHandle(h);}
};
int wmain(int argc,wchar_t** argv){
    const bool legacy=argc>1&&!wcscmp(argv[1],L"--legacy");
    Receiver server;if(!server.start()){std::printf("receiver busy error=%lu\n",GetLastError());return 10;}
    for(unsigned port:{0u,3u}){
        wchar_t inName[32]{},outName[32]{};swprintf_s(inName,L"ABM QA %u Out",port);swprintf_s(outName,L"ABM QA %u In",port);
        UINT in=~0u,out=~0u;
        for(UINT id=0;id<midiInGetNumDevs();++id){MIDIINCAPSW c{};if(!midiInGetDevCapsW(id,&c,sizeof(c))&&!wcscmp(c.szPname,inName))in=id;}
        for(UINT id=0;id<midiOutGetNumDevs();++id){MIDIOUTCAPSW c{};if(!midiOutGetDevCapsW(id,&c,sizeof(c))&&!wcscmp(c.szPname,outName)){out=id;if(c.wTechnology!=(legacy&&port==0?MOD_MAPPER:MOD_MIDIPORT))return 11;}}
        if(in==~0u||out==~0u){std::printf("missing port=%u ins=%u outs=%u\n",port,midiInGetNumDevs(),midiOutGetNumDevs());return 12;}
        Capture capture;HMIDIIN input=nullptr;HMIDIOUT output=nullptr;
        if(midiInOpen(&input,in,reinterpret_cast<DWORD_PTR>(received),reinterpret_cast<DWORD_PTR>(&capture),CALLBACK_FUNCTION))return 13;
        std::array<char,65536> buffer{};MIDIHDR header{};header.lpData=buffer.data();header.dwBufferLength=static_cast<DWORD>(buffer.size());
        if(midiInPrepareHeader(input,&header,sizeof(header))||midiInAddBuffer(input,&header,sizeof(header))||midiInStart(input))return 14;
        if(midiOutOpen(&output,out,0,0,CALLBACK_NULL))return 15;
        if(legacy&&port==0){HMIDIOUT mapped=nullptr;
            // Mapper caps may name a proxy. Prove delivery,not its caption.
            if(midiOutOpen(&mapped,MIDI_MAPPER,0,0,0)||midiOutShortMsg(mapped,0x002a15b0))return 22;
            bool routed=false;const auto until=GetTickCount64()+3000;
            do{{std::lock_guard guard(capture.lock);routed=!capture.shorts.empty()&&capture.shorts.front()==0x002a15b0;}if(routed)break;Sleep(1);}while(GetTickCount64()<until);
            std::printf("default_route_delivered=%u\n",unsigned(routed));if(!routed||midiOutClose(mapped))return 22;
            Sleep(20);{std::lock_guard guard(capture.lock);capture.shorts.clear();}
        }
        std::vector<DWORD> expected;
        for(unsigned i=0;i<128;++i)expected.push_back(0xb0u|((i%16))|(74<<8)|(i<<16));
        for(unsigned i=0;i<16;++i){expected.push_back(0x90u|i|(60<<8)|(1<<16));expected.push_back(0x80u|i|(60<<8));}
        for(auto msg:expected)if(midiOutShortMsg(output,msg))return 16;
        std::array<char,8> sysex{{-16,0x7d,1,2,3,4,5,-9}};MIDIHDR sent{};sent.lpData=sysex.data();sent.dwBufferLength=static_cast<DWORD>(sysex.size());
        if(midiOutPrepareHeader(output,&sent,sizeof(sent))||midiOutLongMsg(output,&sent,sizeof(sent)))return 17;
        const auto deadline=GetTickCount64()+5000;bool exact=false;
        do{{std::lock_guard guard(capture.lock);exact=capture.shorts==expected&&capture.sysex==std::vector<unsigned char>({0xf0,0x7d,1,2,3,4,5,0xf7});}if(exact)break;Sleep(1);}while(GetTickCount64()<deadline);
        std::printf("port=%u in=%u out=%u shorts=%zu sysex=%zu exact=%u default_mapper=%u\n",port,in,out,capture.shorts.size(),capture.sysex.size(),unsigned(exact),unsigned(legacy&&port==0));std::fflush(stdout);
        if(!exact)return 18;
        if(midiOutUnprepareHeader(output,&sent,sizeof(sent))||midiOutReset(output)||midiOutClose(output))return 19;
        if(midiInStop(input)||midiInReset(input)||midiInUnprepareHeader(input,&header,sizeof(header))||midiInClose(input))return 20;
    }
    std::printf("PASS frames=%u errors=%u\n",server.frames.load(),server.errors.load());return server.errors?21:0;
}
