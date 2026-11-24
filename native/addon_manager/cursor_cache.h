// SPDX-License-Identifier: MIT
#pragma once
#include <windows.h>
#include <cstdint>

namespace lsc::cursor {
enum class CacheRefresh { None, FocusReturn, MissingImage, FocusSettling };

// Runs on the cursor render thread. A focus epoch comes from LS's own foreground
// callback, including when rendering is suspended and GetCursorInfo isn't called.
class CacheRecovery {
    uintptr_t owner_=0, target_=0, shape_=0;
    uint64_t epoch_=0, nextRetry_=0;
    unsigned attempts_=0;
    bool pending_=true;
    bool settling_=false;
public:
    CacheRefresh Update(uintptr_t owner, uintptr_t target, uintptr_t shape,
                        uint64_t epoch, uint64_t now, bool focused,
                        bool visible, bool cachedMatch, bool hasImage) {
        if (!owner || !target) return CacheRefresh::None;
        if (owner!=owner_ || target!=target_ || shape!=shape_ || epoch!=epoch_) {
            settling_=epoch!=epoch_;
            owner_=owner; target_=target; shape_=shape; epoch_=epoch;
            pending_=true; attempts_=0; nextRetry_=now;
        }
        if (!focused || !visible || !shape) return CacheRefresh::None;
        if (!cachedMatch) {
            // LS will build the new shape itself on return from GetCursorInfo.
            pending_=false; attempts_=1; nextRetry_=now+150;
            return CacheRefresh::None;
        }
        if (pending_) {
            pending_=false; settling_=true; attempts_=1; nextRetry_=now+150;
            return CacheRefresh::FocusReturn;
        }
        // A non-null texture may still contain the image from before suspend,
        // or a copy made while the game was resuming. Recheck only this bounded
        // focus-return burst; stable frames do not continuously upload cursors.
        if ((!hasImage || settling_) && attempts_<4 && now>=nextRetry_) {
            ++attempts_; nextRetry_=now+250;
            return hasImage?CacheRefresh::FocusSettling:CacheRefresh::MissingImage;
        }
        return CacheRefresh::None;
    }
};

bool InstallCacheRecovery(HMODULE engine);
void SetCacheRecoveryEnabled(bool enabled);
bool CacheRecoveryEnabled();
bool CacheRecoveryReady();
DWORD CacheRecoveryError();
uint64_t CacheInvalidations();
uint64_t CacheMissingImageRetries();
uint64_t CacheFocusEvents();
struct CacheTelemetry {
    uint64_t queries=0, imageQueries=0, hiddenSkips=0, unfocusedSkips=0, settlingRetries=0;
};
CacheTelemetry CacheStats();
}
