// Off-screen native integration fixture, never the installed LS executable.
#include <windows.h>
#include <dwmapi.h>

LRESULT CALLBACK Procedure(HWND window, UINT message, WPARAM wp, LPARAM lp) {
    return DefWindowProcW(window, message, wp, lp);
}

HWND MakeWindow(const wchar_t* name, DWORD style, HWND owner = nullptr) {
    WNDCLASSW type{};
    type.lpfnWndProc = Procedure;
    type.hInstance = GetModuleHandleW(nullptr);
    type.lpszClassName = name;
    RegisterClassW(&type);
    return CreateWindowExW(0, name, L"Startup guard test", style, -30000, -30000,
                           100, 100, owner, nullptr, type.hInstance, nullptr);
}

int WINAPI wWinMain(HINSTANCE, HINSTANCE, LPWSTR, int) {
    wchar_t marker[32]{};
    const bool guarded = GetEnvironmentVariableW(L"LSC_STARTUP_HIDE_UNTIL", marker, 32) != 0;
    const HWND dashboard = MakeWindow(L"HwndWrapper[StartupGuardTest]", WS_OVERLAPPEDWINDOW | WS_VISIBLE);
    if (!dashboard) return 1;
    // Ignore STARTF_USESHOWWINDOW on the first call; subsequent requests must
    // still be suppressed even when LS explicitly overrides startup settings.
    ShowWindow(dashboard, SW_SHOWNORMAL);
    ShowWindow(dashboard, SW_SHOWNORMAL);
    SetWindowPos(dashboard, nullptr, 0, 0, 0, 0,
                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_SHOWWINDOW);
    if (static_cast<bool>(IsWindowVisible(dashboard)) == guarded) return 2;
    if (guarded && !(GetWindowLongPtrW(dashboard, GWL_EXSTYLE) & WS_EX_NOACTIVATE)) return 9;
    DWORD cloaked = 0;
    if (guarded && SUCCEEDED(DwmGetWindowAttribute(dashboard, DWMWA_CLOAKED, &cloaked, sizeof(cloaked))) && !cloaked) return 3;

    const HWND overlay = MakeWindow(L"LSOverlay", WS_POPUP | WS_VISIBLE);
    const HWND echo = MakeWindow(L"LSAddonManagerClass", WS_OVERLAPPEDWINDOW | WS_VISIBLE);
    const HWND error = MakeWindow(L"LSCErrorDialog", WS_OVERLAPPEDWINDOW | WS_VISIBLE);
    const HWND owned = MakeWindow(L"HwndWrapper[OwnedDialog]", WS_OVERLAPPEDWINDOW | WS_VISIBLE, error);
    const HWND child = MakeWindow(L"HwndWrapper[ChildControl]", WS_CHILD | WS_CAPTION | WS_VISIBLE, error);
    if (!overlay || !echo || !error || !owned || !child) return 4;
    if (!IsWindowVisible(overlay) || !IsWindowVisible(echo) || !IsWindowVisible(error) ||
        !IsWindowVisible(owned) || !IsWindowVisible(child)) return 5;

    if (guarded) {
        // Process timers as an actual WPF dispatcher would. At expiry the
        // dashboard must remain hidden, uncloak, then allow a deliberate show.
        const auto until = GetTickCount64() + 1700;
        while (GetTickCount64() < until) {
            MSG message{};
            while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
                TranslateMessage(&message);
                DispatchMessageW(&message);
            }
            Sleep(5);
        }
        if (IsWindowVisible(dashboard)) return 6;
        DwmGetWindowAttribute(dashboard, DWMWA_CLOAKED, &cloaked, sizeof(cloaked));
        if (cloaked) return 7;
        if (GetWindowLongPtrW(dashboard, GWL_EXSTYLE) & WS_EX_NOACTIVATE) return 10;
        ShowWindow(dashboard, SW_SHOWNOACTIVATE);
        if (!IsWindowVisible(dashboard)) return 8;
    }
    DestroyWindow(child);
    DestroyWindow(owned);
    DestroyWindow(error);
    DestroyWindow(echo);
    DestroyWindow(overlay);
    DestroyWindow(dashboard);
    return 0;
}
