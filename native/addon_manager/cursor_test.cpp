// SPDX-License-Identifier: MIT
// No global cursor replacements: private/invalid calls use a spy backend.
#include "cursor_guard.h"
#include "hook_util.h"
#include <cstdio>
#include <cstring>
#include <stdexcept>
static unsigned calls=0;
static HCURSOR passed=nullptr,custom=nullptr;
static DWORD passedId=0;
static BOOL WINAPI Spy(HCURSOR cursor,DWORD id) {
    ++calls;passed=cursor;passedId=id;SetLastError(ERROR_INVALID_CURSOR_HANDLE);return FALSE;
}
static void Check(bool ok,const char* name) {
    printf("%s %s\n",ok?"PASS":"FAIL",name);if(!ok)throw std::runtime_error(name);
}
static LRESULT CALLBACK Game(HWND hwnd,UINT message,WPARAM wp,LPARAM lp) {
    if(message==WM_SETCURSOR){SetCursor(custom);return TRUE;}
    return DefWindowProcW(hwnd,message,wp,lp);
}
int main(){try {
    auto arrow=LoadCursorW(nullptr,IDC_ARROW);
    Check(arrow!=nullptr,"System cursor is available");
    auto shared=static_cast<HCURSOR>(LoadImageW(nullptr,IDC_ARROW,IMAGE_CURSOR,0,0,LR_SHARED));
    Check(shared==arrow,"Reproduce the verified LS shared LoadImage cursor request");
    SetLastError(1234);
    Check(lsc::cursor::ReplaceSafely(shared,32512,Spy,LoadCursorW),"Shared self replacement succeeds without consuming the cursor");
    Check(calls==0 && GetLastError()==1234,"No global replacement and last error preserved");
    for(unsigned i=0;i<1000;++i)lsc::cursor::ReplaceSafely(shared,32512,Spy,LoadCursorW);
    Check(calls==0 && LoadCursorW(nullptr,IDC_ARROW)==arrow,"Repeated LS watcher requests preserve the shared handle");
    unsigned char andMask[128],xorMask[128]{};memset(andMask,0xff,sizeof andMask);
    xorMask[0]=0x80;custom=CreateCursor(GetModuleHandleW(nullptr),0,0,32,32,andMask,xorMask);
    Check(custom!=nullptr,"Create a game-specific cursor");
    Check(!lsc::cursor::ReplaceSafely(custom,32512,Spy,LoadCursorW) && calls==1 && passed==custom && passedId==32512,
          "Private replacements retain backend ownership and return value");
    Check(GetLastError()==ERROR_INVALID_CURSOR_HANDLE,"Backend errors propagate");
    lsc::cursor::ReplaceSafely(nullptr,32512,Spy,LoadCursorW);
    Check(calls==2 && !passed,"Null requests reach the backend");
    lsc::cursor::ReplaceSafely(shared,0x10001,Spy,LoadCursorW);
    Check(calls==3 && passedId==0x10001,"Invalid large cursor IDs are not truncated");
    lsc::cursor::ReplaceSafely(shared,32513,Spy,LoadCursorW);
    Check(calls==4,"Different system cursor slots are not mistaken for self replacement");
    auto module=GetModuleHandleW(nullptr);
    auto slot=eam::hooks::ImportSlot(module,"user32.dll","SetSystemCursor");
    Check(slot!=nullptr,"Locate a real PE import slot");
    auto original=*slot;
    Check(lsc::cursor::Install(module) && lsc::cursor::Ready(),"Install guard on the fixture's actual import table");
    Check(*slot!=original && lsc::cursor::Install(module),"Import guard installation is idempotent");
    const auto count=lsc::cursor::Prevented();
    Check(SetSystemCursor(shared,32512) && lsc::cursor::Prevented()==count+1,"Real imported API call reaches the guard without touching system cursors");
    WNDCLASSW cls{};cls.hInstance=module;cls.lpfnWndProc=Game;cls.lpszClassName=L"LSCCursorRecoveryTest";
    Check(RegisterClassW(&cls)!=0,"Register game cursor fixture");
    HWND window=CreateWindowW(cls.lpszClassName,L"cursor",WS_OVERLAPPEDWINDOW,-20000,-20000,100,100,nullptr,nullptr,module,nullptr);
    Check(window!=nullptr,"Create off-screen game window");
    SetCursor(nullptr);DWORD_PTR result=0;
    Check(SendMessageTimeoutW(window,WM_SETCURSOR,reinterpret_cast<WPARAM>(window),MAKELPARAM(HTCLIENT,WM_MOUSEMOVE),
                             SMTO_ABORTIFHUNG|SMTO_BLOCK,50,&result)!=0 && result==TRUE,
          "Bounded cursor refresh is accepted by the game");
    Check(GetCursor()==custom,"Game restores its own custom cursor rather than a forced arrow");
    SetCursor(arrow);DestroyWindow(window);DestroyCursor(custom);
    puts("Cursor guard and game-owned restoration passed; no interactive Alt-Tab claimed.");return 0;
}catch(const std::exception& error){fprintf(stderr,"%s\n",error.what());return 1;}}
