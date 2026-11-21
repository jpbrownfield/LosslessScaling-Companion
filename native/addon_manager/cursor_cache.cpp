// SPDX-License-Identifier: MIT
#include "cursor_cache.h"
#include "hook_util.h"
#include <MinHook.h>
#include <intrin.h>
#include <atomic>
#include <cstring>
#include <mutex>
#include "cursor_cache_contract.h"

namespace lsc::cursor {
namespace {
std::mutex installMutex;
std::atomic<bool> ready{false};
std::atomic<bool> enabled{true};
std::atomic<DWORD> error{0};
std::atomic<uint64_t> focusEpoch{0}, focusEvents{0}, invalidations{0}, missingImageRetries{0};
BYTE* engineBase=nullptr;
decltype(&GetCursorInfo) originalGetCursorInfo=nullptr;
WINEVENTPROC originalFocusEvent=nullptr;

void CALLBACK ObserveFocusEvent(HWINEVENTHOOK hook, DWORD event, HWND window,
                               LONG object, LONG child, DWORD thread, DWORD tick) {
    // Use LS's real notifications: polling the render loop misses focus changes
    // while the renderer is suspended. Every event still reaches LS unchanged.
    if (event==EVENT_SYSTEM_FOREGROUND) {++focusEvents;++focusEpoch;}
    originalFocusEvent(hook,event,window,object,child,thread,tick);
}

BOOL WINAPI ObserveCursorInfo(PCURSORINFO info) {
    const auto caller=_ReturnAddress();
    const BOOL result=originalGetCursorInfo(info);
    const DWORD saved=GetLastError();
    // Only the verified cursor-image update site owns the adjacent cached
    // handle and image pointer. Other GetCursorInfo callers stay untouched.
    if (ready && enabled && result && info && info->cbSize==sizeof(CURSORINFO)
            && caller==engineBase+contract::kCursorInfoReturn
            && engineBase[contract::kMultiDisplay]) {
        auto* owner=reinterpret_cast<BYTE*>(info)-contract::kCursorInfoOffset;
        HCURSOR cached=nullptr;
        void* image=nullptr;
        HWND target=nullptr;
        std::memcpy(&cached,owner+contract::kCachedShapeOffset,sizeof(cached));
        std::memcpy(&image,owner+contract::kImageOffset,sizeof(image));
        std::memcpy(&target,engineBase+contract::kTargetWindow,sizeof(target));
        thread_local CacheRecovery recovery;
        const auto reason=recovery.Update(reinterpret_cast<uintptr_t>(owner),
            reinterpret_cast<uintptr_t>(target),reinterpret_cast<uintptr_t>(info->hCursor),
            focusEpoch.load(),GetTickCount64(),GetForegroundWindow()==target,
            info->flags==CURSOR_SHOWING,cached==info->hCursor,image!=nullptr);
        if (reason!=CacheRefresh::None) {
            // Invalidate only the identity, on LS's render thread. LS releases
            // the old image and builds its replacement immediately after this
            // API returns. Never change the reported cursor or its visibility.
            cached=nullptr;
            std::memcpy(owner+contract::kCachedShapeOffset,&cached,sizeof(cached));
            ++invalidations;
            if (reason==CacheRefresh::MissingImage) ++missingImageRetries;
        }
    }
    SetLastError(saved);
    return result;
}

bool MatchesContract(HMODULE module) {
    if (!module) return false;
    const auto* base=reinterpret_cast<const BYTE*>(module);
    const auto* dos=reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
    if (dos->e_magic!=IMAGE_DOS_SIGNATURE) return false;
    const auto* nt=reinterpret_cast<const IMAGE_NT_HEADERS*>(base+dos->e_lfanew);
    if (nt->Signature!=IMAGE_NT_SIGNATURE || nt->FileHeader.Machine!=IMAGE_FILE_MACHINE_AMD64
            || nt->FileHeader.TimeDateStamp!=contract::kTimestamp
            || nt->OptionalHeader.SizeOfImage!=contract::kImageSize) return false;
    return std::memcmp(base+contract::kEventRva,contract::kEvent,sizeof(contract::kEvent))==0
        && std::memcmp(base+contract::kUpdateRva,contract::kUpdate,sizeof(contract::kUpdate))==0
        && std::memcmp(base+contract::kSettingsRva,contract::kSettings,sizeof(contract::kSettings))==0
        && std::memcmp(base+contract::kActivateRva,contract::kActivate,sizeof(contract::kActivate))==0;
}
}

bool InstallCacheRecovery(HMODULE engine) {
    std::lock_guard<std::mutex> lock(installMutex);
    if (ready) return engineBase==reinterpret_cast<BYTE*>(engine);
    // Private layout access is deliberately limited to the supplied LS build.
    // Unknown engines retain the shared-handle guard and native cursor behavior.
    if (!MatchesContract(engine)) {error=ERROR_REVISION_MISMATCH;return false;}
    void** slot=eam::hooks::ImportSlot(engine,"user32.dll","GetCursorInfo");
    if (!slot) {error=ERROR_PROC_NOT_FOUND;return false;}
    if (!eam::hooks::Begin()) {error=ERROR_FUNCTION_FAILED;return false;}
    auto* event=reinterpret_cast<BYTE*>(engine)+contract::kEventRva;
    const auto created=MH_CreateHook(event,ObserveFocusEvent,reinterpret_cast<void**>(&originalFocusEvent));
    if (created!=MH_OK) {eam::hooks::End();error=ERROR_FUNCTION_FAILED;return false;}
    engineBase=reinterpret_cast<BYTE*>(engine);
    originalGetCursorInfo=reinterpret_cast<decltype(originalGetCursorInfo)>(*slot);
    if (MH_EnableHook(event)!=MH_OK) {
        MH_RemoveHook(event);eam::hooks::End();error=ERROR_FUNCTION_FAILED;return false;
    }
    if (!eam::hooks::SwapSlot(slot,reinterpret_cast<void*>(&ObserveCursorInfo))) {
        const DWORD failure=GetLastError();
        MH_DisableHook(event);MH_RemoveHook(event);eam::hooks::End();
        error=failure?failure:ERROR_ACCESS_DENIED;return false;
    }
    // The host is pinned for the engine lifetime, as is its existing IAT guard.
    error=0;ready=true;return true;
}
bool CacheRecoveryReady(){return ready;}
void SetCacheRecoveryEnabled(bool value){if(enabled.exchange(value)!=value)++focusEpoch;}
bool CacheRecoveryEnabled(){return enabled;}
DWORD CacheRecoveryError(){return error;}
uint64_t CacheInvalidations(){return invalidations;}
uint64_t CacheMissingImageRetries(){return missingImageRetries;}
uint64_t CacheFocusEvents(){return focusEvents;}
}
