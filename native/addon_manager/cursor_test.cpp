// SPDX-License-Identifier: MIT
// No global cursor replacements: private/invalid calls use a spy backend.
#include "cursor_guard.h"
#include "cursor_cache.h"
#include "hook_util.h"
#include <cstdio>
#include <cstring>
#include <stdexcept>
#include <initializer_list>
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
static void TestImageCache() {
    using lsc::cursor::CacheRecovery;
    using lsc::cursor::CacheRefresh;
    Check(lsc::cursor::CacheRecoveryEnabled(),"Custom cursor recovery defaults on");
    lsc::cursor::SetCacheRecoveryEnabled(false);
    Check(!lsc::cursor::CacheRecoveryEnabled(),"Window behavior preference can disable native recovery");
    const auto events=lsc::cursor::CacheFocusEvents();
    lsc::cursor::SetCacheRecoveryEnabled(true);
    Check(lsc::cursor::CacheRecoveryEnabled() && lsc::cursor::CacheFocusEvents()==events,
          "Enabling recovery does not fabricate foreground events");
    CacheRecovery recovery;
    auto update=[&](uint64_t epoch,uint64_t now,bool focused=true,bool visible=true,
                    bool same=true,bool image=true,uintptr_t shape=71) {
        return recovery.Update(11,21,shape,epoch,now,focused,visible,same,image);
    };
    Check(update(0,0,true,true,false,false)==CacheRefresh::None,
          "New shape is built by LS without an extra invalidation");
    bool stable=true;
    for(unsigned i=1;i<1000;++i) stable=stable && update(0,i)==CacheRefresh::None;
    Check(stable,"Stable valid image is retained across 999 frames");
    Check(update(1,1000,false)==CacheRefresh::None,"Focus loss never requests a rebuild");
    Check(update(2,1100)==CacheRefresh::FocusReturn,
          "Focus return rebuilds a custom cursor even when its handle is unchanged");
    Check(update(2,1101)==CacheRefresh::None,"Successful focus recovery is not repeated per frame");
    Check(update(4,1200,true,false)==CacheRefresh::None,"Intentional cursor hiding is preserved");
    Check(update(4,1250)==CacheRefresh::FocusReturn,"Pending recovery waits until the game shows a cursor");
    Check(update(5,1300,true,true,false,false)==CacheRefresh::None,"Changed shape keeps LS's own rebuild");
    Check(update(5,1400,true,true,true,false)==CacheRefresh::None,"Missing image retry is delayed");
    for (auto tick : {1450ULL,1700ULL,1950ULL})
        Check(update(5,tick,true,true,true,false)==CacheRefresh::MissingImage,"Failed image build can retry");
    Check(update(5,9000,true,true,true,false)==CacheRefresh::None,"Missing image retries are bounded");
    Check(update(5,9100,true,true,false,false,72)==CacheRefresh::None,"A new game shape resets the retry budget");
    Check(update(5,9250,true,true,true,false,72)==CacheRefresh::MissingImage,"New shape can recover independently");
    Check(recovery.Update(12,22,72,5,9300,true,true,true,true)==CacheRefresh::FocusReturn,
          "A new renderer and target do not inherit a stale cache generation");
    Check(recovery.Update(12,0,72,5,9400,true,true,true,true)==CacheRefresh::None,
          "Unknown scaling target is left alone");
    Check(recovery.Update(12,22,0,5,9500,true,true,true,false)==CacheRefresh::None,
          "A null cursor is never made visible by cache recovery");
    CacheRecovery settling;
    Check(settling.Update(11,21,71,1,0,true,true,true,true)==CacheRefresh::FocusReturn,"Focus burst starts with one refresh");
    for(auto tick:{150ULL,400ULL,650ULL})
        Check(settling.Update(11,21,71,1,tick,true,true,true,true)==CacheRefresh::FocusSettling,
              "Focus return rechecks a non-null stale image after resume");
    Check(settling.Update(11,21,71,1,900,true,true,true,true)==CacheRefresh::None,
          "Focus settling uploads stop after four total attempts");
    Check(settling.Update(11,21,71,2,1000,false,true,true,true)==CacheRefresh::None,
          "Unrelated focus never refreshes the cursor");
    Check(settling.Update(11,21,71,2,1100,true,false,true,true)==CacheRefresh::None,
          "Settling never makes an intentionally hidden cursor visible");
    CacheRecovery natural;
    Check(natural.Update(11,21,71,0,0,true,true,false,true)==CacheRefresh::None,
          "Initial natural image build does not start a focus retry burst");
    Check(natural.Update(11,21,71,1,1000,true,true,false,true)==CacheRefresh::None,
          "LS handles a changed cache identity during focus return");
    Check(natural.Update(11,21,71,1,1150,true,true,true,true)==CacheRefresh::FocusSettling,
          "Natural focus-return rebuild also rechecks a non-null stale image");
}
int main(){try {
    TestImageCache();
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
    CURSORINFO info{sizeof(CURSORINFO)};GetCursorInfo(&info);
    auto infoSlot=eam::hooks::ImportSlot(module,"user32.dll","GetCursorInfo");
    Check(infoSlot!=nullptr,"Locate fixture cursor-info import for the compatibility check");
    auto unmodifiedInfo=*infoSlot;
    auto slot=eam::hooks::ImportSlot(module,"user32.dll","SetSystemCursor");
    Check(slot!=nullptr,"Locate a real PE import slot");
    auto original=*slot;
    Check(lsc::cursor::Install(module) && lsc::cursor::Ready(),"Install guard on the fixture's actual import table");
    Check(*slot!=original && lsc::cursor::Install(module),"Import guard installation is idempotent");
    Check(!lsc::cursor::CacheRecoveryReady() && lsc::cursor::CacheRecoveryError()==ERROR_REVISION_MISMATCH
          && *infoSlot==unmodifiedInfo,"Unknown engine retains its original cursor-info import and private state");
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
    puts("Cursor guard, cache recovery and game-owned restoration passed; no interactive Alt-Tab claimed.");return 0;
}catch(const std::exception& error){fprintf(stderr,"%s\n",error.what());return 1;}}
