#include "window_target.hpp"

#include <algorithm>
#include <cstdint>
#include <iterator>
#include <limits>
#include <string_view>

namespace {

struct Candidate {
    HWND window = nullptr;
    std::int64_t score = std::numeric_limits<std::int64_t>::min();
};

std::int64_t area(const RECT& rectangle) {
    const auto width = std::max<LONG>(0, rectangle.right - rectangle.left);
    const auto height = std::max<LONG>(0, rectangle.bottom - rectangle.top);
    return static_cast<std::int64_t>(width) * height;
}

std::int64_t overlap_area(const RECT& left, const RECT& right) {
    RECT overlap{};
    return IntersectRect(&overlap, &left, &right) ? area(overlap) : 0;
}

bool usable(HWND window) {
    if (!window || !IsWindow(window) || !IsWindowVisible(window) || IsIconic(window)) {
        return false;
    }
    RECT rectangle{};
    return GetWindowRect(window, &rectangle) && area(rectangle) >= 64 * 64;
}

bool shell_window(HWND window) {
    wchar_t class_name[128]{};
    GetClassNameW(window, class_name, static_cast<int>(std::size(class_name)));
    const std::wstring_view name(class_name);
    return name == L"Progman" || name == L"WorkerW" || name == L"Shell_TrayWnd" ||
           name == L"Windows.UI.Core.CoreWindow";
}

struct PresentationSearch {
    DWORD process_id = 0;
    Candidate best;
};

BOOL CALLBACK enumerate_presentation(HWND window, LPARAM parameter) {
    auto& search = *reinterpret_cast<PresentationSearch*>(parameter);
    DWORD process_id = 0;
    GetWindowThreadProcessId(window, &process_id);
    if (process_id != search.process_id || !usable(window)) return TRUE;

    RECT rectangle{};
    GetWindowRect(window, &rectangle);
    const LONG_PTR extended = GetWindowLongPtrW(window, GWL_EXSTYLE);
    std::int64_t score = area(rectangle);
    if (extended & WS_EX_TOPMOST) score += score / 2;
    if (extended & WS_EX_TOOLWINDOW) score -= score / 2;
    if (GetWindow(window, GW_OWNER)) score -= score / 3;

    const HMONITOR monitor = MonitorFromWindow(window, MONITOR_DEFAULTTONEAREST);
    MONITORINFO info{sizeof(info)};
    if (monitor && GetMonitorInfoW(monitor, &info)) {
        const auto monitor_area = area(info.rcMonitor);
        const auto overlap = overlap_area(rectangle, info.rcMonitor);
        if (monitor_area && overlap * 100 >= monitor_area * 80) score += monitor_area;
    }
    if (score > search.best.score) search.best = {window, score};
    return TRUE;
}

struct TargetSearch {
    DWORD own_process_id = 0;
    RECT presentation_bounds{};
    Candidate best;
    std::int64_t z_order_bonus = 1'000'000;
};

BOOL CALLBACK enumerate_target(HWND window, LPARAM parameter) {
    auto& search = *reinterpret_cast<TargetSearch*>(parameter);
    DWORD process_id = 0;
    GetWindowThreadProcessId(window, &process_id);
    if (process_id == search.own_process_id || !usable(window) || shell_window(window)) {
        search.z_order_bonus = std::max<std::int64_t>(0, search.z_order_bonus - 1'000);
        return TRUE;
    }
    const LONG_PTR extended = GetWindowLongPtrW(window, GWL_EXSTYLE);
    if (extended & WS_EX_TOOLWINDOW) return TRUE;

    RECT rectangle{};
    GetWindowRect(window, &rectangle);
    std::int64_t score = overlap_area(rectangle, search.presentation_bounds) * 4;
    score += area(rectangle);
    score += search.z_order_bonus;
    if (GetWindow(window, GW_OWNER)) score -= area(rectangle) / 2;
    if (score > search.best.score) search.best = {window, score};
    search.z_order_bonus = std::max<std::int64_t>(0, search.z_order_bonus - 1'000);
    return TRUE;
}

}  // namespace

namespace lsc::windows {

bool is_external_window(HWND window) {
    if (!usable(window) || shell_window(window)) return false;
    DWORD process_id = 0;
    GetWindowThreadProcessId(window, &process_id);
    return process_id && process_id != GetCurrentProcessId();
}

HWND find_presentation_window() {
    PresentationSearch search{GetCurrentProcessId()};
    EnumWindows(enumerate_presentation, reinterpret_cast<LPARAM>(&search));
    return search.best.window;
}

HWND find_target_window(HWND presentation, HWND preferred) {
    if (is_external_window(preferred)) return GetAncestor(preferred, GA_ROOT);
    if (!presentation || !IsWindow(presentation)) return nullptr;
    TargetSearch search{GetCurrentProcessId()};
    GetWindowRect(presentation, &search.presentation_bounds);
    EnumWindows(enumerate_target, reinterpret_cast<LPARAM>(&search));
    return search.best.window;
}

bool make_interactive(HWND window, InteractiveState& state) {
    if (!window || !IsWindow(window)) return false;
    state.window = window;
    state.style = GetWindowLongPtrW(window, GWL_STYLE);
    state.extended_style = GetWindowLongPtrW(window, GWL_EXSTYLE);
    state.was_topmost = (state.extended_style & WS_EX_TOPMOST) != 0;

    // Preserve WS_EX_LAYERED: removing it can destroy the presentation surface
    // or turn it black. Only remove flags that explicitly reject activation/input.
    const LONG_PTR style = state.style & ~static_cast<LONG_PTR>(WS_DISABLED);
    const LONG_PTR extended = state.extended_style &
        ~static_cast<LONG_PTR>(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE);
    SetLastError(ERROR_SUCCESS);
    if (style != state.style && !SetWindowLongPtrW(window, GWL_STYLE, style) && GetLastError()) {
        return false;
    }
    SetLastError(ERROR_SUCCESS);
    if (extended != state.extended_style &&
        !SetWindowLongPtrW(window, GWL_EXSTYLE, extended) && GetLastError()) {
        SetWindowLongPtrW(window, GWL_STYLE, state.style);
        return false;
    }
    state.changed = style != state.style || extended != state.extended_style;
    SetWindowPos(
        window, HWND_TOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOOWNERZORDER | SWP_FRAMECHANGED | SWP_SHOWWINDOW
    );
    return true;
}

void restore(const InteractiveState& state) {
    if (!state.window || !IsWindow(state.window)) return;
    if (state.changed) {
        SetWindowLongPtrW(state.window, GWL_STYLE, state.style);
        SetWindowLongPtrW(state.window, GWL_EXSTYLE, state.extended_style);
    }
    SetWindowPos(
        state.window, state.was_topmost ? HWND_TOPMOST : HWND_NOTOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOOWNERZORDER | SWP_FRAMECHANGED
    );
}

bool focus(HWND window) {
    if (!window || !IsWindow(window)) return false;
    if (IsIconic(window)) ShowWindow(window, SW_RESTORE);

    const HWND foreground = GetForegroundWindow();
    const DWORD current_thread = GetCurrentThreadId();
    const DWORD target_thread = GetWindowThreadProcessId(window, nullptr);
    const DWORD foreground_thread = foreground
        ? GetWindowThreadProcessId(foreground, nullptr) : 0;

    const bool attach_target = target_thread && target_thread != current_thread &&
        AttachThreadInput(current_thread, target_thread, TRUE);
    const bool attach_foreground = foreground_thread && foreground_thread != current_thread &&
        foreground_thread != target_thread &&
        AttachThreadInput(current_thread, foreground_thread, TRUE);

    BringWindowToTop(window);
    const bool accepted = SetForegroundWindow(window) != FALSE;
    SetActiveWindow(window);
    SetFocus(window);

    if (attach_foreground) AttachThreadInput(current_thread, foreground_thread, FALSE);
    if (attach_target) AttachThreadInput(current_thread, target_thread, FALSE);

    return accepted || GetAncestor(GetForegroundWindow(), GA_ROOT) == GetAncestor(window, GA_ROOT);
}

bool send_key(HWND window, WORD virtual_key) {
    if (!window || !virtual_key || !focus(window)) return false;
    INPUT input[2]{};
    input[0].type = INPUT_KEYBOARD;
    input[0].ki.wVk = virtual_key;
    input[1] = input[0];
    input[1].ki.dwFlags = KEYEVENTF_KEYUP;
    return SendInput(static_cast<UINT>(std::size(input)), input, sizeof(INPUT)) ==
        static_cast<UINT>(std::size(input));
}

}  // namespace lsc::windows
