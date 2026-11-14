// Included inside reshade_passthrough.cpp's private namespace.
// All window changes run on the existing passthrough watcher, never the pipe worker.
std::atomic<bool> g_companionControlled{false};
std::atomic<int> g_companionRequest{-1};
std::atomic<bool> g_companionApplying{false};
std::atomic<bool> g_companionWanted{false};
std::atomic<int> g_companionFocusForTest{-1};
std::mutex g_companionRequestMutex;
HWND g_companionRequestTarget = nullptr;
DWORD g_companionRequestTargetPid = 0;
std::atomic<int> g_companionError{0};
HWND g_companionTarget = nullptr;
DWORD g_companionTargetPid = 0;
HWND g_companionPresentation = nullptr;
bool g_companionWasTopmost = false;

const char* CompanionErrorText() {
    switch (g_companionError.load()) {
        case 1: return "The LS presentation window was not found. Start scaling before opening the overlay.";
        case 2: return "The scaled game window changed or closed. Start scaling again.";
        case 3: return "Focus changed before overlay input could open. Return to the scaled game and retry.";
        case 4: return "The LS presentation closed while enabling overlay input. Start scaling again; see the LS log for details.";
        case 5: return "The LS overlay foreground guard could not start. Restart Lossless Scaling; see the LS log for details.";
        default: return "";
    }
}

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

void UpdateCompanionFocus();

void ApplyCompanionRequest() {
    int request;
    HWND requestedTarget;
    DWORD requestedPid;
    {
        std::lock_guard<std::mutex> lock(g_companionRequestMutex);
        request = g_companionRequest.exchange(-1);
        requestedTarget = g_companionRequestTarget;
        requestedPid = g_companionRequestTargetPid;
    }
    if (request < 0 || !g_companionControlled) return;
    g_companionApplying = true;
    struct Finished { ~Finished() { g_companionApplying = false; } } finished;
    if (request) {
        if (g_companionWanted) return;
        const HWND foreground = GetForegroundWindow();
        const HWND presentation = FindLosslessWindow();
        if (!presentation) { g_companionError=1; LOG_WARN("ReShade", "%s", CompanionErrorText());return; }
        HWND target = nullptr;
        DWORD targetPid = 0;
        if (requestedTarget || requestedPid) {
            // Companion already verified the scaled target. Z order can include
            // its border, FPS displays or other overlays between LS and the game.
            if (requestedTarget) GetWindowThreadProcessId(requestedTarget,&targetPid);
            if (!requestedTarget || !requestedPid || !IsWindow(requestedTarget) || IsIconic(requestedTarget)
                    || targetPid!=requestedPid || (targetPid==GetCurrentProcessId() && !g_ignoreFocusForTest)) {
                g_companionError=2;LOG_WARN("ReShade", "%s (PID %lu, HWND %p)",CompanionErrorText(),requestedPid,requestedTarget);return;
            }
            DWORD foregroundPid=0;GetWindowThreadProcessId(foreground,&foregroundPid);
            if (!g_ignoreFocusForTest && !BelongsToUs(foreground) && foregroundPid!=requestedPid) {
                g_companionError=3;LOG_WARN("ReShade", "%s (foreground PID %lu, target %lu)",CompanionErrorText(),foregroundPid,requestedPid);return;
            }
            target=requestedTarget;
        } else {
            if (!FocusIsOurs()) { g_companionError=3;LOG_WARN("ReShade", "%s",CompanionErrorText());return; }
            target=BelongsToUs(foreground) ? FindGameWindow(presentation) : foreground;
            if (target) GetWindowThreadProcessId(target,&targetPid);
        }
        g_companionTarget=target;
        g_companionTargetPid=targetPid;
        g_companionPresentation = presentation;
        g_companionWasTopmost = (GetWindowLongPtrW(g_companionPresentation,GWL_EXSTYLE)&WS_EX_TOPMOST)!=0;
        ProtectCompanionPresentation(presentation, true);
        g_companionWanted = true;
        // Keep the request pending until subclassing and focus changes finish.
        UpdateCompanionFocus();
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
        ProtectCompanionPresentation(nullptr, false);
    }
    LOG_INFO("ReShade", "Companion overlay requested %s", g_companionWanted ? "ON" : "OFF");
}

void UpdateCompanionFocus() {
    if (!g_companionWanted) return;
    g_companionApplying=true;
    struct Finished { ~Finished() { g_companionApplying=false; } } finished;
    DWORD targetPid=0;
    if (g_companionTarget) GetWindowThreadProcessId(g_companionTarget,&targetPid);
    if (g_companionTarget && (!targetPid || targetPid!=g_companionTargetPid)) {
        g_companionError = 2;
        LOG_WARN("ReShade", "Overlay input stopped: %s (presentation %p, target %p, PID %lu)",
                 CompanionErrorText(), g_companionPresentation, g_companionTarget, g_companionTargetPid);
        g_companionRequest=0;
        ApplyCompanionRequest();
        return;
    }
    if (!IsWindow(g_companionPresentation)) {
        // LS can tear down its presentation while temporarily suspended and
        // recreate it when the game returns. Keep the user's requested state,
        // but release input and wait for a real replacement presentation.
        g_passthroughOn=false;
        RestoreAll();
        ProtectCompanionPresentation(nullptr, false);
        const HWND replacement=FindLosslessWindow();
        if (!replacement) {
            if (g_companionPresentation)
                LOG_INFO("ReShade", "LS presentation closed; overlay input suspended until it returns (target PID %lu)",g_companionTargetPid);
            g_companionPresentation=nullptr;
            return;
        }
        g_companionPresentation=replacement;
        g_companionWasTopmost=(GetWindowLongPtrW(replacement,GWL_EXSTYLE)&WS_EX_TOPMOST)!=0;
        ProtectCompanionPresentation(replacement,true);
        if (!CompanionFocusIsOurs()) CompanionPresentationTopmost(false);
        LOG_INFO("ReShade", "Overlay input attached to replacement LS presentation %p",replacement);
    }
    const bool focused=CompanionFocusIsOurs();
    if (g_passthroughOn==focused) return;
    g_passthroughOn=focused;
    if (focused) {
        // Restore the baseline before ProcessWindow saves styles for this cycle.
        CompanionPresentationTopmost(g_companionWasTopmost);
        ProcessOwnWindows();
        if (!IsWindow(g_companionPresentation)) {
            g_companionError = 4;
            LOG_WARN("ReShade", "%s", CompanionErrorText());
            g_companionRequest = 0;
            ApplyCompanionRequest();
            return;
        }
        CompanionPresentationTopmost(true);
    } else {
        RestoreAll();
        // Even an originally topmost presentation must yield to other apps.
        CompanionPresentationTopmost(false);
    }
    LOG_INFO("ReShade", "Companion passthrough %s", focused ? "resumed" : "suspended");
}
