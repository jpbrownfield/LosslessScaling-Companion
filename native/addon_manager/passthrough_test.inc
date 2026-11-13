// Uses the upstream feature harness's off-screen, message-pumping window.
static void CompanionPassthroughOnce() {
    const HWND foreground = GetForegroundWindow();
    WindowThread window;
    window.Start();
    const HWND hwnd = window.hwnd;
    // Plain input flags exercise subclass/restore without raising or focusing it.
    SetWindowLongPtrW(hwnd, GWL_EXSTYLE, WS_EX_TOOLWINDOW);
    const auto original = GetWindowLongPtrW(hwnd, GWLP_WNDPROC);
    ConfigManager::Instance().Set("ReShadePassthrough", "companion_control", "1");
    features::SetOn(0, true);
    features::reshade::ForcePassthroughForTest(true); // bypass foreground only in this isolated test
    Check("Companion control is available", features::reshade::CompanionControlAvailable());
    auto wait = [hwnd, original](bool enabled) {
        for (int i=0;i<200;++i) {
            if (!features::reshade::CompanionControlPending() &&
                !features::reshade::Status().empty()==enabled &&
                (GetWindowLongPtrW(hwnd,GWLP_WNDPROC)!=original)==enabled) return true;
            Sleep(10);
        }
        return false;
    };
    features::reshade::RequestCompanionPassthrough(false);
    Check("explicit disable restores native window input", wait(false));
    features::reshade::RequestCompanionPassthrough(true);
    Check("explicit enable installs native passthrough", wait(true));
    Check("enabled presentation is topmost", (GetWindowLongPtrW(hwnd,GWL_EXSTYLE)&WS_EX_TOPMOST)!=0);
    features::reshade::RequestCompanionPassthrough(true);
    Check("repeating enable leaves passthrough enabled", wait(true));
    features::reshade::CompanionFocusForTest(0);
    Check("focus loss suspends native input", wait(false));
    Check("suspension preserves the overlay preference", features::reshade::CompanionOverlayRequested());
    Check("suspended presentation is not topmost", !(GetWindowLongPtrW(hwnd,GWL_EXSTYLE)&WS_EX_TOPMOST));
    features::reshade::CompanionFocusForTest(1);
    Check("returning focus resumes native input", wait(true));
    Check("returning focus restores topmost", (GetWindowLongPtrW(hwnd,GWL_EXSTYLE)&WS_EX_TOPMOST)!=0);
    features::reshade::RequestCompanionPassthrough(false);
    Check("explicit close restores the original window procedure", wait(false));
    Check("explicit close clears the overlay preference", !features::reshade::CompanionOverlayRequested());
    Check("explicit close restores original non-topmost state", !(GetWindowLongPtrW(hwnd,GWL_EXSTYLE)&WS_EX_TOPMOST));
    features::reshade::RequestCompanionPassthrough(true);
    Check("overlay can be reopened", wait(true));
    features::reshade::CompanionFocusForTest(0);
    Check("overlay can be suspended again", wait(false));
    features::reshade::RequestCompanionPassthrough(false);
    Check("explicit close while suspended completes", wait(false));
    features::reshade::CompanionFocusForTest(1);
    Sleep(150);
    Check("closed overlay stays off when focus returns", features::reshade::Status().empty() &&
          !features::reshade::CompanionOverlayRequested());
    // An LS presentation can already be topmost before passthrough starts.
    SetWindowPos(hwnd,HWND_TOPMOST,0,0,0,0,SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE);
    features::reshade::RequestCompanionPassthrough(true);
    Check("originally topmost presentation enables", wait(true));
    features::reshade::CompanionFocusForTest(0);
    Check("originally topmost presentation suspends", wait(false));
    Check("suspension demotes even an originally topmost presentation", !(GetWindowLongPtrW(hwnd,GWL_EXSTYLE)&WS_EX_TOPMOST));
    features::reshade::CompanionFocusForTest(1);
    Check("originally topmost presentation resumes", wait(true));
    features::reshade::RequestCompanionPassthrough(false);
    Check("originally topmost presentation closes", wait(false));
    Check("close preserves its original topmost state", (GetWindowLongPtrW(hwnd,GWL_EXSTYLE)&WS_EX_TOPMOST)!=0);
    SetWindowPos(hwnd,HWND_NOTOPMOST,0,0,0,0,SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE);
    Check("control does not move foreground in the off-screen test", GetForegroundWindow()==foreground);
    features::reshade::ForcePassthroughForTest(false);
    features::SetOn(0, false);
    Check("stopped feature cannot accept control", !features::reshade::CompanionControlAvailable());
    window.Stop();
}
