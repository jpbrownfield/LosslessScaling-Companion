// SPDX-License-Identifier: MIT
#include "cursor_guard.h"
#include "hook_util.h"
#include <atomic>
namespace lsc::cursor {
namespace {
std::atomic<bool> ready{false};
std::atomic<uint64_t> prevented{0};
std::atomic<DWORD> error{0};
Replace replace=nullptr;
HMODULE installed=nullptr;
BOOL WINAPI Guard(HCURSOR cursor,DWORD id) { return ReplaceSafely(cursor,id,replace,LoadCursorW); }
}
BOOL ReplaceSafely(HCURSOR cursor,DWORD id,Replace original,Load load) {
    const DWORD saved=GetLastError();
    // The verified LS 3.2.2 cursor watcher loads OCR_NORMAL with LR_SHARED,
    // then passes that shared handle to SetSystemCursor every 100 ms while
    // outside the capture. SetSystemCursor consumes/destroys its argument.
    // Replacing a system cursor with itself cannot change its image, but can
    // invalidate the shared handle used by the game. Keep that handle alive.
    if(cursor && id<=0xffff && cursor==load(nullptr,MAKEINTRESOURCEW(id))) {
        ++prevented;SetLastError(saved);return TRUE;
    }
    SetLastError(saved);
    // Private replacement cursors, invalid requests and their ownership/error
    // semantics are unchanged. In particular, never destroy a shared cursor.
    return original(cursor,id);
}
bool Install(HMODULE engine) {
    if(ready)return installed==engine;
    if(!engine){error=ERROR_MOD_NOT_FOUND;return false;}
    void** slot=eam::hooks::ImportSlot(engine,"user32.dll","SetSystemCursor");
    if(!slot){error=ERROR_PROC_NOT_FOUND;return false;}
    replace=reinterpret_cast<Replace>(*slot);
    if(!eam::hooks::SwapSlot(slot,reinterpret_cast<void*>(&Guard))) {
        error=GetLastError();if(!error)error=ERROR_ACCESS_DENIED;return false;
    }
    // Only this engine's import changes. The managed host stays pinned, so an
    // engine cursor watcher can safely retain this address through shutdown.
    installed=engine;error=0;ready=true;return true;
}
bool Ready(){return ready;}
uint64_t Prevented(){return prevented;}
DWORD Error(){return error;}
}
