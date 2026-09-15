#pragma once

#include <windows.h>

namespace lsc::windows {

struct InteractiveState {
    HWND window = nullptr;
    LONG_PTR style = 0;
    LONG_PTR extended_style = 0;
    bool was_topmost = false;
    bool changed = false;
};

HWND find_presentation_window();
HWND find_target_window(HWND presentation, HWND preferred);
bool make_interactive(HWND window, InteractiveState& state);
void restore(const InteractiveState& state);
bool focus(HWND window);
bool send_key(HWND window, WORD virtual_key);
bool is_external_window(HWND window);

}  // namespace lsc::windows
