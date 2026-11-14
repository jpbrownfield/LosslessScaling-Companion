// Included in reshade_passthrough.cpp's private namespace. LS's managed app
// and native engine both observe foreground changes and stop capture when
// their own presentation activates. That activation is intentional while
// Companion is giving ReShade keyboard input.
std::atomic<HWND> g_focusPresentation{nullptr};
std::atomic<bool> g_focusProtected{false};
std::atomic<bool> g_focusGuardReady{false};
decltype(&SetWinEventHook) g_originalSetWinEventHook = nullptr;
decltype(&UnhookWinEvent) g_originalUnhookWinEvent = nullptr;
std::mutex g_focusCallbacksMutex;
std::map<HWINEVENTHOOK, WINEVENTPROC> g_focusCallbacks;

void ProtectCompanionPresentation(HWND hwnd, bool enabled) {
    if (hwnd) g_focusPresentation = hwnd;
    g_focusProtected = enabled;
}

void CALLBACK CompanionForegroundEvent(HWINEVENTHOOK hook, DWORD event, HWND hwnd,
                                       LONG object, LONG child, DWORD thread, DWORD tick) {
    WINEVENTPROC callback = nullptr;
    {
        std::lock_guard<std::mutex> lock(g_focusCallbacksMutex);
        const auto found = g_focusCallbacks.find(hook);
        if (found != g_focusCallbacks.end()) callback = found->second;
    }
    if (!callback) return;
    if (event == EVENT_SYSTEM_FOREGROUND && hwnd && hwnd == g_focusPresentation.load()
            && IsWindow(hwnd) && BelongsToUs(hwnd)
            && (g_focusProtected || GetForegroundWindow() != hwnd)) {
        // Also discard an already-queued presentation event after close has
        // returned focus to the game. All other foreground events pass through.
        LOG_INFO("ReShade", "Keeping LS capture active for overlay input (presentation %p)", hwnd);
        return;
    }
    callback(hook, event, hwnd, object, child, thread, tick);
}

HWINEVENTHOOK WINAPI CompanionSetWinEventHook(DWORD minimum, DWORD maximum, HMODULE module,
        WINEVENTPROC callback, DWORD process, DWORD thread, DWORD flags) {
    HMODULE owner = nullptr;
    if (callback) GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                                    reinterpret_cast<LPCWSTR>(callback), &owner);
    // Limit interception to LS, its forwarded engine and managed delegates
    // (JIT stubs have no module). ReShade and other add-ons keep their callbacks.
    const bool lossless = !owner || owner == GetModuleHandleW(nullptr)
                                  || owner == GetModuleHandleW(L"Lossless_original.dll");
    if (!callback || !lossless || minimum > EVENT_SYSTEM_FOREGROUND || maximum < EVENT_SYSTEM_FOREGROUND
            || (flags & WINEVENT_INCONTEXT))
        return g_originalSetWinEventHook(minimum, maximum, module, callback, process, thread, flags);
    const auto hook = g_originalSetWinEventHook(minimum, maximum, module, CompanionForegroundEvent, process, thread, flags);
    if (!hook) return nullptr;
    try {
        std::lock_guard<std::mutex> lock(g_focusCallbacksMutex);
        g_focusCallbacks[hook] = callback;
    } catch (...) {
        g_originalUnhookWinEvent(hook);
        SetLastError(ERROR_NOT_ENOUGH_MEMORY);
        return nullptr;
    }
    return hook;
}

BOOL WINAPI CompanionUnhookWinEvent(HWINEVENTHOOK hook) {
    const BOOL result = g_originalUnhookWinEvent(hook);
    const DWORD error = GetLastError();
    if (result) {
        std::lock_guard<std::mutex> lock(g_focusCallbacksMutex);
        g_focusCallbacks.erase(hook);
    }
    SetLastError(error);
    return result;
}

bool InstallCompanionFocusGuard() {
    if (g_focusGuardReady) return true;
    const auto user = GetModuleHandleW(L"user32.dll");
    auto set = reinterpret_cast<void*>(GetProcAddress(user, "SetWinEventHook"));
    auto unset = reinterpret_cast<void*>(GetProcAddress(user, "UnhookWinEvent"));
    if (!set || !unset || !eam::hooks::Begin()) return false;
    const bool createdSet = MH_CreateHook(set, CompanionSetWinEventHook, reinterpret_cast<void**>(&g_originalSetWinEventHook)) == MH_OK;
    const bool createdUnset = createdSet && MH_CreateHook(unset, CompanionUnhookWinEvent, reinterpret_cast<void**>(&g_originalUnhookWinEvent)) == MH_OK;
    if (!createdUnset || MH_EnableHook(unset) != MH_OK || MH_EnableHook(set) != MH_OK) {
        if (createdSet) { MH_DisableHook(set); MH_RemoveHook(set); }
        if (createdUnset) { MH_DisableHook(unset); MH_RemoveHook(unset); }
        eam::hooks::End();
        LOG_ERROR("ReShade", "Could not install LS overlay foreground guard");
        return false;
    }
    // The host is pinned. Keep these detours and their one MinHook reference
    // for its lifetime: Windows retains our callback addresses after registration.
    // Feature stop disables protection; every remaining event is forwarded.
    g_focusGuardReady = true;
    LOG_INFO("ReShade", "LS overlay foreground guard installed");
    return true;
}
