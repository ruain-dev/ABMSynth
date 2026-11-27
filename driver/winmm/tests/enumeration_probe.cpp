// Private HKCU override; no machine registration, receiver, audio or messages.
#include <windows.h>
#include <mmsystem.h>
#include <mmddk.h>
#include <cstdio>
#include <string>
using Message=DWORD(WINAPI*)(UINT,UINT,DWORD_PTR,DWORD_PTR,DWORD_PTR);
int wmain(int argc,wchar_t** argv){
    if(argc!=2)return 64;
    const std::wstring name=L"Software\\ABMSynth\\EnumerationAudit-"+std::to_wstring(GetCurrentProcessId());
    HKEY root=nullptr,key=nullptr;
    if(RegCreateKeyExW(HKEY_CURRENT_USER,name.c_str(),0,nullptr,0,KEY_ALL_ACCESS,nullptr,&root,nullptr))return 65;
    int result=0;HMODULE driver=nullptr;
    if(RegCreateKeyExW(root,L"Software\\ABMSynth\\MIDIPorts",0,nullptr,0,KEY_ALL_ACCESS,nullptr,&key,nullptr) || RegOverridePredefKey(HKEY_CURRENT_USER,root))result=66;
    if(!result){driver=LoadLibraryW(argv[1]);if(!driver)result=67;}
    auto output=driver?reinterpret_cast<Message>(GetProcAddress(driver,"modMessage")):nullptr;
    auto input=driver?reinterpret_cast<Message>(GetProcAddress(driver,"midMessage")):nullptr;
    if(!result && (!output||!input))result=68;
    const DWORD profiles[][5]={{1,1,0,1,0},{4,5,10,2,2},{3,0,0,0,0}};
    for(const auto& p:profiles){
        if(result)break;
        const wchar_t* keys[]={L"Count",L"InMask",L"OutMask"};
        for(unsigned i=0;i<3;++i)if(RegSetValueExW(key,keys[i],0,REG_DWORD,reinterpret_cast<const BYTE*>(&p[i]),sizeof(DWORD)))result=69;
        if(output(0,MODM_GETNUMDEVS,0,0,0)!=p[3] || input(0,MIDM_GETNUMDEVS,0,0,0)!=p[4])result=70;
        std::printf("ports count=%lu in-mask=%lu out-mask=%lu outputs=%lu inputs=%lu result=%d\n",p[0],p[1],p[2],output(0,MODM_GETNUMDEVS,0,0,0),input(0,MIDM_GETNUMDEVS,0,0,0),result);
    }
    if(driver)FreeLibrary(driver);
    RegOverridePredefKey(HKEY_CURRENT_USER,nullptr);
    if(key)RegCloseKey(key);if(root)RegCloseKey(root);
    RegDeleteTreeW(HKEY_CURRENT_USER,name.c_str());return result;
}
