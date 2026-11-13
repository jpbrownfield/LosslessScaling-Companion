// Included inside reshade_passthrough.cpp's private namespace.
// All window changes run on the existing passthrough watcher, never the pipe worker.
std::atomic<bool> g_companionControlled{false};
std::atomic<int> g_companionRequest{-1};
std::atomic<bool> g_companionApplying{false};
std::atomic<bool> g_companionWanted{false};
std::atomic<int> g_companionFocusForTest{-1};
HWND g_companionTarget = nullptr;
DWORD g_companionTargetPid = 0;
HWND g_companionPresentation = nullptr;
bool g_companionWasTopmost = false;

void CompanionPresentationTopmost(bool topmost) {
    if (g_companionPresentation && IsWindow(g_companionPresentation))
        SetWindowPos(g_companionPresentation, topmost ? HWND_TOPMOST : HWND_NOTOPMOST,
                     0,0,0,0,SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE|SWP_NOOWNERZORDER);
}

bool CompanionFocusIsOurs() {
    const int testFocus = g_companionFocusForTest;
    if (testFocus >= 0) return testFocus != 0;
    if (g_ignoreFocusForTest) return true;
    const HWND foreground = GetForegroundWindow();
    if (!foreground) return false;
    if (BelongsToUs(foreground)) return true;
    DWORD targetPid=0, foregroundPid=0;
    if (g_companionTarget && IsWindow(g_companionTarget))
        GetWindowThreadProcessId(g_companionTarget,&targetPid);
    GetWindowThreadProcessId(foreground,&foregroundPid);
    return targetPid && targetPid==g_companionTargetPid && targetPid==foregroundPid;
}

void ApplyCompanionRequest() {
    const int request = g_companionRequest.exchange(-1);
    if (request < 0 || !g_companionControlled) return;
    g_companionApplying = true;
    struct Finished { ~Finished() { g_companionApplying = false; } } finished;
    if (request) {
        if (g_companionWanted || !FocusIsOurs()) return;
        const HWND foreground = GetForegroundWindow();
        g_companionTarget = BelongsToUs(foreground) ? FindGameWindow(FindLosslessWindow()) : foreground;
        g_companionTargetPid = 0;
        if (g_companionTarget) GetWindowThreadProcessId(g_companionTarget,&g_companionTargetPid);
        g_companionPresentation = FindLosslessWindow();
        if (!g_companionPresentation) return;
        g_companionWasTopmost = (GetWindowLongPtrW(g_companionPresentation,GWL_EXSTYLE)&WS_EX_TOPMOST)!=0;
        g_companionWanted = true;
    } else {
        const HWND target = g_companionTarget;
        // Restore focus only when the presentation still owns it. Alt+Tab must
        // never bring the game back over the application the user selected.
        const bool ownsFocus = BelongsToUs(GetForegroundWindow());
        const bool focused = CompanionFocusIsOurs();
        g_companionWanted = false;
        g_passthroughOn = false;
        RestoreAll();
        CompanionPresentationTopmost(focused && g_companionWasTopmost);
        g_companionTarget = nullptr;
        g_companionTargetPid = 0;
        g_companionPresentation = nullptr;
        if (ownsFocus && target && IsWindow(target) && !IsIconic(target)) SetForegroundWindow(target);
    }
    LOG_INFO("ReShade", "Companion overlay requested %s", g_companionWanted ? "ON" : "OFF");
}

void UpdateCompanionFocus() {
    if (!g_companionWanted) return;
    DWORD targetPid=0;
    if (g_companionTarget) GetWindowThreadProcessId(g_companionTarget,&targetPid);
    if (!IsWindow(g_companionPresentation) ||
        (g_companionTarget && (!targetPid || targetPid!=g_companionTargetPid))) {
        g_companionRequest=0;
        ApplyCompanionRequest();
        return;
    }
    const bool focused=CompanionFocusIsOurs();
    if (g_passthroughOn==focused) return;
    g_companionApplying=true;
    struct Finished { ~Finished() { g_companionApplying=false; } } finished;
    g_passthroughOn=focused;
    if (focused) {
        // Restore the baseline before ProcessWindow saves styles for this cycle.
        CompanionPresentationTopmost(g_companionWasTopmost);
        ProcessOwnWindows();
        CompanionPresentationTopmost(true);
    } else {
        RestoreAll();
        // Even an originally topmost presentation must yield to other apps.
        CompanionPresentationTopmost(false);
    }
    LOG_INFO("ReShade", "Companion passthrough %s", focused ? "resumed" : "suspended");
}
