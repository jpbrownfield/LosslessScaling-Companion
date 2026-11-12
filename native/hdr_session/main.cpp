// SPDX-License-Identifier: MIT
// Process lifetime is the signal consumed by an unmodified Gloam running-app rule.
#include <windows.h>
#include <cstdlib>
#include <algorithm>
int WINAPI wWinMain(HINSTANCE,HINSTANCE,PWSTR command,int) {
    wchar_t* end=nullptr;const auto pid=wcstoul(command,&end,10);
    if(!pid||!end||*end)return 2;
    HANDLE parent=OpenProcess(SYNCHRONIZE,FALSE,pid);
    HANDLE input=GetStdHandle(STD_INPUT_HANDLE);
    if(!parent||!input||input==INVALID_HANDLE_VALUE)return 3;
    // Tests can distinguish Windows process startup from the heartbeat lifetime.
    DWORD written=0;WriteFile(GetStdHandle(STD_OUTPUT_HANDLE),"1",1,&written,nullptr);
    ULONGLONG last=GetTickCount64();
    while(WaitForSingleObject(parent,100)==WAIT_TIMEOUT) {
        DWORD bytes=0;
        if(!PeekNamedPipe(input,nullptr,0,nullptr,&bytes,nullptr))break;
        if(bytes) {char buffer[256];DWORD read=0;if(!ReadFile(input,buffer,std::min(bytes,DWORD(sizeof(buffer))),&read,nullptr)||!read)break;last=GetTickCount64();}
        if(GetTickCount64()-last>8000)break;
    }
    CloseHandle(parent);return 0;
}
