// Only Companion-created LosslessScaling.exe processes are armed. No global hooks,
// proxy DLL replacement, remote memory writes, or code patching are used.
#include <windows.h>
#include <commctrl.h>
#include <dwmapi.h>
#include <cwchar>
#include <new>
#include <string>
#include <vector>

namespace {
constexpr wchar_t kDeadline[] = L"LSC_STARTUP_HIDE_UNTIL";
constexpr UINT_PTR kSubclass = 0x4C534347;
constexpr UINT_PTR kTimer = 0x4C534348;
HMODULE module = nullptr;
thread_local const wchar_t* launch_stage = L"idle";

ULONGLONG Deadline() {
    static const ULONGLONG deadline = [] {
        wchar_t image[32768]{};
        if (!GetModuleFileNameW(nullptr, image, ARRAYSIZE(image))) return 0ULL;
        const wchar_t* name = wcsrchr(image, L'\\');
        if (_wcsicmp(name ? name + 1 : image, L"LosslessScaling.exe")) return 0ULL;
        wchar_t value[32]{};
        if (!GetEnvironmentVariableW(kDeadline, value, ARRAYSIZE(value))) return 0ULL;
        const ULONGLONG parsed = _wcstoui64(value, nullptr, 10);
        const auto now = GetTickCount64();
        return parsed > now && parsed - now <= 30000 ? parsed : 0ULL;
    }();
    return deadline;
}

bool Armed() { return GetTickCount64() < Deadline(); }

bool ConfigurationWindow(HWND window, LONG_PTR style, HWND owner) {
    if ((style & WS_CHILD) || (style & WS_CAPTION) != WS_CAPTION || owner) return false;
    wchar_t name[256]{};
    GetClassNameW(window, name, ARRAYSIZE(name));
    // LS's WPF dashboard only. Borderless scaling surfaces, Echo's panel, native
    // error dialogs and other processes must continue to function normally.
    return wcsncmp(name, L"HwndWrapper[", 12) == 0;
}

LRESULT CALLBACK WindowProc(HWND window, UINT message, WPARAM wp, LPARAM lp,
                            UINT_PTR, DWORD_PTR original_noactivate) {
    if (!Armed() || message == WM_NCDESTROY) {
        KillTimer(window, kTimer);
        RemoveWindowSubclass(window, WindowProc, kSubclass);
        const auto style = GetWindowLongPtrW(window, GWL_EXSTYLE);
        SetWindowLongPtrW(window, GWL_EXSTYLE,
            (style & ~WS_EX_NOACTIVATE) | static_cast<LONG_PTR>(original_noactivate));
        if (message != WM_NCDESTROY) ShowWindow(window, SW_HIDE);
        const BOOL cloak = FALSE;
        DwmSetWindowAttribute(window, DWMWA_CLOAK, &cloak, sizeof(cloak));
        return DefSubclassProc(window, message, wp, lp);
    }
    if (message == WM_MOUSEACTIVATE) return MA_NOACTIVATE;
    if (message == WM_TIMER && wp == kTimer) return 0;
    const auto result = DefSubclassProc(window, message, wp, lp);
    if (message == WM_WINDOWPOSCHANGING) {
        auto* position = reinterpret_cast<WINDOWPOS*>(lp);
        position->flags = (position->flags & ~SWP_SHOWWINDOW) | SWP_HIDEWINDOW;
    }
    return result;
}

void CALLBACK StartupEvent(HWINEVENTHOOK, DWORD event, HWND window, LONG object,
                           LONG child, DWORD, DWORD) {
    if (Armed() && window && object == OBJID_WINDOW && child == CHILDID_SELF &&
        (event == EVENT_OBJECT_CREATE || event == EVENT_OBJECT_SHOW)) {
        if (ConfigurationWindow(window, GetWindowLongPtrW(window, GWL_STYLE),
                                GetWindow(window, GW_OWNER))) {
            // The subclass can outlive the installer's hook while a queued
            // window message is in flight. Pin our own tiny module, not LS.
            HMODULE pinned = nullptr;
            DWORD_PTR existing = 0;
            if (GetWindowSubclass(window, WindowProc, kSubclass, &existing)) {
                ShowWindow(window, SW_HIDE);
                return;
            }
            const auto style = GetWindowLongPtrW(window, GWL_EXSTYLE);
            if (GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                                  GET_MODULE_HANDLE_EX_FLAG_PIN,
                                  reinterpret_cast<LPCWSTR>(&StartupEvent), &pinned) &&
                SetWindowSubclass(window, WindowProc, kSubclass, style & WS_EX_NOACTIVATE)) {
                SetWindowLongPtrW(window, GWL_EXSTYLE, style | WS_EX_NOACTIVATE);
                const BOOL cloak = TRUE;
                // Cloaking is defense in depth for ShowWindow paths that
                // skip WM_WINDOWPOSCHANGING; no geometry/DPI changes.
                DwmSetWindowAttribute(window, DWMWA_CLOAK, &cloak, sizeof(cloak));
                SetTimer(window, kTimer, 50, nullptr);
                ShowWindow(window, SW_HIDE);
            }
        }
    }
}

struct Session {
    std::wstring path;
    DWORD duration;
    HANDLE ready = nullptr;
    HANDLE stop = nullptr;
    HANDLE thread = nullptr;
    DWORD pid = 0;
    DWORD error = ERROR_SUCCESS;
};

DWORD RunGuardImpl(void* argument) {
    auto* state = static_cast<Session*>(argument);
    const auto& path = state->path;
    const auto slash = path.find_last_of(L"\\/");
    LPWCH inherited = GetEnvironmentStringsW();
    if (!inherited) {
        state->error = GetLastError();
        SetEvent(state->ready);
        return 0;
    }
    std::vector<wchar_t> environment;
    for (const wchar_t* item = inherited; *item; item += wcslen(item) + 1) {
        if (!_wcsnicmp(item, kDeadline, ARRAYSIZE(kDeadline) - 1) &&
            item[ARRAYSIZE(kDeadline) - 1] == L'=') continue;
        environment.insert(environment.end(), item, item + wcslen(item) + 1);
    }
    FreeEnvironmentStringsW(inherited);
    const auto marker = std::wstring(kDeadline) + L"=" + std::to_wstring(GetTickCount64() + state->duration);
    environment.insert(environment.end(), marker.c_str(), marker.c_str() + marker.size() + 1);
    environment.push_back(L'\0');
    std::wstring command = L"\"" + path + L"\"";
    const auto directory = path.substr(0, slash);
    STARTUPINFOW startup{sizeof(startup)};
    startup.dwFlags = STARTF_USESHOWWINDOW;
    startup.wShowWindow = SW_HIDE;
    PROCESS_INFORMATION process{};
    if (!CreateProcessW(path.c_str(), command.data(), nullptr, nullptr, FALSE,
                        CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
                        environment.data(), directory.c_str(), &startup, &process)) {
        state->error = GetLastError();
        SetEvent(state->ready);
        return 0;
    }
    // A process-filtered in-context event hook can be registered before user32
    // initializes (unlike WH_CBT). It maps this DLL only into this new LS PID,
    // on whichever thread creates the dashboard, not other desktop processes.
    HWINEVENTHOOK hook = SetWinEventHook(EVENT_OBJECT_CREATE, EVENT_OBJECT_SHOW,
        module, StartupEvent, process.dwProcessId, 0, WINEVENT_INCONTEXT);
    if (!hook || ResumeThread(process.hThread) == static_cast<DWORD>(-1)) {
        state->error = GetLastError();
        if (!state->error) state->error = ERROR_GEN_FAILURE;
        if (hook) UnhookWinEvent(hook);
        TerminateProcess(process.hProcess, state->error);
        WaitForSingleObject(process.hProcess, 5000);
        CloseHandle(process.hThread);
        CloseHandle(process.hProcess);
        SetEvent(state->ready);
        return 0;
    }
    CloseHandle(process.hThread);
    state->pid = process.dwProcessId;
    SetEvent(state->ready);
    const auto until = GetTickCount64() + state->duration;
    HANDLE handles[] = {state->stop, process.hProcess};
    while (true) {
        const auto now = GetTickCount64();
        if (now >= until) break;
        auto result = MsgWaitForMultipleObjects(2, handles, FALSE,
            static_cast<DWORD>(until - now), QS_ALLINPUT);
        if (result != WAIT_OBJECT_0 + 2) break;
        MSG message{};
        while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
    }
    UnhookWinEvent(hook); // Must be called by its installing thread.
    CloseHandle(process.hProcess);
    return 0;
}

DWORD WINAPI RunGuard(void* argument) {
    try {
        return RunGuardImpl(argument);
    } catch (...) {
        // All allocating work is before CreateProcess: failure cannot leave a
        // suspended child behind or unwind a C++ exception into Python.
        auto* state = static_cast<Session*>(argument);
        state->error = ERROR_NOT_ENOUGH_MEMORY;
        SetEvent(state->ready);
        return 0;
    }
}
}

extern "C" __declspec(dllexport) void WINAPI LSCReleaseStartupGuard(void* session) {
    auto* state = static_cast<Session*>(session);
    if (!state) return;
    SetEvent(state->stop);
    if (state->thread) {
        WaitForSingleObject(state->thread, INFINITE);
        CloseHandle(state->thread);
    }
    if (state->ready) CloseHandle(state->ready);
    if (state->stop) CloseHandle(state->stop);
    delete state;
}

BOOL LaunchHiddenImpl(
    const wchar_t* executable, DWORD duration, DWORD* pid, void** session) {
    launch_stage = L"validate";
    if (!executable || !pid || !session || duration < 100 || duration > 30000) {
        SetLastError(ERROR_INVALID_PARAMETER);
        return FALSE;
    }
    *session = nullptr;
    *pid = 0;
    const std::wstring path(executable);
    const auto slash = path.find_last_of(L"\\/");
    if (slash == std::wstring::npos || _wcsicmp(path.c_str() + slash + 1, L"LosslessScaling.exe")) {
        SetLastError(ERROR_INVALID_NAME);
        return FALSE;
    }
    Session* state = new (std::nothrow) Session{path, duration};
    if (!state) {
        SetLastError(ERROR_NOT_ENOUGH_MEMORY);
        return FALSE;
    }
    launch_stage = L"install_process_event_hook";
    state->ready = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    state->stop = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (state->ready && state->stop) state->thread = CreateThread(nullptr, 0, RunGuard, state, 0, nullptr);
    if (!state->thread) {
        DWORD error = GetLastError();
        LSCReleaseStartupGuard(state);
        SetLastError(error);
        return FALSE;
    }
    WaitForSingleObject(state->ready, INFINITE);
    if (state->error) {
        DWORD error = state->error;
        LSCReleaseStartupGuard(state);
        SetLastError(error);
        return FALSE;
    }
    *pid = state->pid;
    *session = state;
    launch_stage = L"running";
    return TRUE;
}

extern "C" __declspec(dllexport) BOOL WINAPI LSCLaunchHidden(
    const wchar_t* executable, DWORD duration, DWORD* pid, void** session) {
    try {
        return LaunchHiddenImpl(executable, duration, pid, session);
    } catch (...) {
        SetLastError(ERROR_NOT_ENOUGH_MEMORY);
        return FALSE;
    }
}

extern "C" __declspec(dllexport) const wchar_t* WINAPI LSCStartupGuardLastStage() {
    return launch_stage;
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) module = instance;
    return TRUE;
}
