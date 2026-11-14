// Integration fixture only: load the packaged host in a synthetic presentation
// process. The proprietary LS engine and its capture path are not simulated.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <cstdio>
#include <cstring>

constexpr UINT Probe = WM_APP + 41;
constexpr UINT HomeCount = WM_APP + 42;
constexpr UINT BaselineTransparent = WM_APP + 43;
constexpr UINT RegisterForegroundStop = WM_APP + 44;
constexpr UINT ForegroundEvents = WM_APP + 45;
constexpr UINT RecreatePresentation = WM_APP + 46;
WNDPROC baseline = nullptr;
unsigned homeCount = 0;
bool transparent = false;
HWINEVENTHOOK foregroundHook = nullptr;
HWND presentation = nullptr;
unsigned foregroundEvents = 0;
bool replacing = false;

void CALLBACK StopOnOwnForeground(HWINEVENTHOOK, DWORD, HWND hwnd, LONG, LONG, DWORD, DWORD) {
    ++foregroundEvents;
    // Reproduce LS stopping its presentation when its foreground listener sees
    // its own window activate. The packaged host must protect this handoff.
    if (hwnd == presentation) DestroyWindow(presentation);
}

LRESULT CALLBACK WindowProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    if (msg == Probe) return GetWindowLongPtrW(hwnd, GWLP_WNDPROC) != reinterpret_cast<LONG_PTR>(baseline);
    if (msg == HomeCount) return homeCount;
    if (msg == BaselineTransparent) { transparent = wp != 0; return 1; }
    if (msg == RegisterForegroundStop) {
        presentation = hwnd;
        foregroundHook = SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, nullptr,
                                        StopOnOwnForeground, 0, 0, WINEVENT_OUTOFCONTEXT);
        return foregroundHook != nullptr;
    }
    if (msg == ForegroundEvents) return foregroundEvents;
    if (msg == RecreatePresentation) {
        auto replacement = CreateWindowExW(WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
            L"LosslessScaling", L"LSC replacement presentation fixture", WS_POPUP,
            80, 80, 720, 480, nullptr, nullptr, GetModuleHandleW(nullptr), nullptr);
        if (!replacement) return 0;
        SetLayeredWindowAttributes(replacement, 0, 190, LWA_ALPHA);
        presentation = replacement;
        ShowWindow(replacement, SW_SHOWNOACTIVATE);
        replacing = true;
        DestroyWindow(hwnd);
        replacing = false;
        return reinterpret_cast<LRESULT>(replacement);
    }
    if (msg == WM_NCHITTEST && transparent) return HTTRANSPARENT;
    if (msg == WM_KEYDOWN && wp == VK_HOME) { ++homeCount; return 0; }
    if (msg == WM_CLOSE) { DestroyWindow(hwnd); return 0; }
    if (msg == WM_DESTROY) {
        if (replacing) return 0;
        if (foregroundHook) { UnhookWinEvent(foregroundHook); foregroundHook = nullptr; }
        PostQuitMessage(0); return 0;
    }
    if (msg == WM_PAINT) {
        PAINTSTRUCT paint{};
        HDC dc = BeginPaint(hwnd, &paint);
        RECT rect{}; GetClientRect(hwnd, &rect);
        FillRect(dc, &rect, reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1));
        const wchar_t text[] = L"Companion input integration fixture";
        TextOutW(dc, 12, 12, text, ARRAYSIZE(text)-1);
        EndPaint(hwnd, &paint);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

int main(int argc, char** argv) {
    const bool host = argc > 1 && !strcmp(argv[1], "host");
    const bool obstruction = argc > 1 && !strcmp(argv[1], "obstruction");
    auto instance = GetModuleHandleW(nullptr);
    WNDCLASSW cls{};
    cls.lpfnWndProc = WindowProc; cls.hInstance = instance;
    cls.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    cls.lpszClassName = host ? L"LosslessScaling" : L"LSCInputOtherFixture";
    if (!RegisterClassW(&cls)) return 2;
    const DWORD extended = host ? WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
                               : obstruction ? WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW : 0;
    const DWORD style = host || obstruction ? WS_POPUP : WS_OVERLAPPEDWINDOW;
    HWND hwnd = CreateWindowExW(extended, cls.lpszClassName,
        host ? L"LSC input presentation fixture" : obstruction ? L"Companion border obstruction fixture" : L"Alt-Tab away fixture",
        style, 80, 80, 720, 480, nullptr, nullptr, instance, nullptr);
    if (!hwnd) return 3;
    baseline = reinterpret_cast<WNDPROC>(GetWindowLongPtrW(hwnd, GWLP_WNDPROC));
    transparent = host;
    if (host) SetLayeredWindowAttributes(hwnd, 0, 190, LWA_ALPHA);
    ShowWindow(hwnd, SW_SHOWNOACTIVATE);
    SetTimer(hwnd, 1, 60000, [](HWND window, UINT, UINT_PTR, DWORD) { PostMessageW(window, WM_CLOSE, 0, 0); });
    std::printf("hwnd=%llu\n", reinterpret_cast<unsigned long long>(hwnd)); std::fflush(stdout);
    if (host && !LoadLibraryW(L"Lossless.dll")) {
        std::fprintf(stderr, "LoadLibrary failed: %lu\n", GetLastError()); return 4;
    }
    if (host) {
        cls.lpszClassName = L"HwndWrapper[CompanionInputFixture]";
        RegisterClassW(&cls);
        auto settings = CreateWindowExW(0, cls.lpszClassName, L"LS configuration fixture", WS_OVERLAPPEDWINDOW,
                                        100, 100, 900, 600, nullptr, nullptr, instance, nullptr);
        ShowWindow(settings, SW_SHOWNOACTIVATE);
    }
    MSG message{};
    while (GetMessageW(&message, nullptr, 0, 0) > 0) {
        TranslateMessage(&message); DispatchMessageW(&message);
    }
    return 0;
}
