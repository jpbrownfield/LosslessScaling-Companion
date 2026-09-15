#include "bridge.hpp"

#include <algorithm>
#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <string>

namespace {

bool key_down(int virtual_key) {
    return virtual_key && (GetAsyncKeyState(virtual_key) & 0x8000) != 0;
}

int config_integer(IHost* host, const char* key, int fallback, int minimum, int maximum) {
    if (!host) return fallback;
    const char* value = host->GetConfig("LSP-ReShade", key, "");
    if (!value || !*value) return fallback;
    char* end = nullptr;
    const long parsed = std::strtol(value, &end, 10);
    if (end == value || *end != '\0') return fallback;
    return std::clamp(static_cast<int>(parsed), minimum, maximum);
}

bool config_boolean(IHost* host, const char* key, bool fallback) {
    return config_integer(host, key, fallback ? 1 : 0, 0, 1) != 0;
}

}  // namespace

namespace lsc {

ReShadeBridge& ReShadeBridge::instance() {
    // AddonShutdown performs the orderly stop. Deliberately avoid a static
    // destructor that could join the worker while Windows holds the loader lock.
    static ReShadeBridge* bridge = new ReShadeBridge();
    return *bridge;
}

void ReShadeBridge::log(LsProxyLogLevel level, const char* format, ...) const {
    IHost* host = nullptr;
    {
        std::lock_guard lock(mutex_);
        host = host_;
    }
    if (!host) return;
    char message[1024]{};
    va_list arguments;
    va_start(arguments, format);
    vsnprintf_s(message, sizeof(message), _TRUNCATE, format, arguments);
    va_end(arguments);
    host->Log(level, message);
}

void ReShadeBridge::start(IHost* host) {
    stop();
    if (!host) return;
    {
        std::lock_guard lock(mutex_);
        host_ = host;
        hotkey_ = config_integer(host, "hotkey_vk", VK_HOME, 1, 255);
        require_ctrl_ = config_boolean(host, "hotkey_ctrl", false);
        require_alt_ = config_boolean(host, "hotkey_alt", false);
        require_shift_ = config_boolean(host, "hotkey_shift", false);
        require_win_ = config_boolean(host, "hotkey_win", false);
        stop_event_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        if (!stop_event_) {
            host_ = nullptr;
            return;
        }
        running_ = true;
    }
    worker_ = std::thread(&ReShadeBridge::run, this);
    log(LSPROXY_LOG_INFO, "LS Companion ReShade Bridge initialized (hotkey VK=%d)", hotkey_);
}

void ReShadeBridge::stop() {
    HANDLE event = nullptr;
    {
        std::lock_guard lock(mutex_);
        event = stop_event_;
        running_ = false;
    }
    if (event) SetEvent(event);
    if (worker_.joinable() && worker_.get_id() != std::this_thread::get_id()) {
        worker_.join();
    }
    deactivate(false);
    {
        std::lock_guard lock(mutex_);
        if (stop_event_) CloseHandle(stop_event_);
        stop_event_ = nullptr;
        host_ = nullptr;
    }
}

bool ReShadeBridge::hotkey_down() const {
    if (!key_down(hotkey_)) return false;
    if (require_ctrl_ && !key_down(VK_CONTROL)) return false;
    if (require_alt_ && !key_down(VK_MENU)) return false;
    if (require_shift_ && !key_down(VK_SHIFT)) return false;
    if (require_win_ && !key_down(VK_LWIN) && !key_down(VK_RWIN)) return false;
    return true;
}

bool ReShadeBridge::activate() {
    const HWND foreground = GetForegroundWindow();
    const HWND presentation = windows::find_presentation_window();
    if (!presentation) {
        log(LSPROXY_LOG_WARN, "Activation ignored: no visible Lossless Scaling presentation window");
        return false;
    }
    const HWND target = windows::find_target_window(presentation, foreground);
    windows::InteractiveState state{};
    if (!windows::make_interactive(presentation, state)) {
        log(LSPROXY_LOG_ERROR, "Activation failed: presentation window styles could not be updated");
        return false;
    }
    if (!windows::focus(presentation)) {
        windows::restore(state);
        log(LSPROXY_LOG_WARN, "Activation failed: Windows refused presentation focus");
        return false;
    }

    // The physical hotkey was delivered to the previously focused game. Send
    // one clean copy only after LS owns focus so ReShade receives it. No mouse
    // click or arbitrary multi-hundred-ms delay is required.
    std::this_thread::sleep_for(std::chrono::milliseconds(35));
    if (!windows::send_key(presentation, static_cast<WORD>(hotkey_))) {
        windows::restore(state);
        if (target) windows::focus(target);
        log(LSPROXY_LOG_ERROR, "Activation failed: ReShade hotkey delivery was rejected");
        return false;
    }

    {
        std::lock_guard lock(mutex_);
        presentation_state_ = state;
        target_ = target;
    }
    active_ = true;
    log(
        LSPROXY_LOG_INFO,
        "Interactive ReShade mode enabled (presentation=%p target=%p)",
        static_cast<void*>(presentation),
        static_cast<void*>(target)
    );
    return true;
}

void ReShadeBridge::deactivate(bool return_focus) {
    windows::InteractiveState state{};
    HWND target = nullptr;
    {
        std::lock_guard lock(mutex_);
        state = presentation_state_;
        target = target_;
        presentation_state_ = {};
        target_ = nullptr;
    }
    active_ = false;
    windows::restore(state);
    if (return_focus && windows::is_external_window(target)) windows::focus(target);
}

void ReShadeBridge::run() {
    bool previous_hotkey = false;
    while (running_) {
        HANDLE event = nullptr;
        {
            std::lock_guard lock(mutex_);
            event = stop_event_;
        }
        if (!event || WaitForSingleObject(event, active_ ? 10 : 35) != WAIT_TIMEOUT) break;

        const bool pressed = hotkey_down();
        if (pressed && !previous_hotkey) {
            if (active_) {
                // While interactive, the physical key is already delivered to
                // ReShade. Give its WndProc a moment to close before restoring.
                std::this_thread::sleep_for(std::chrono::milliseconds(35));
                deactivate(true);
                log(LSPROXY_LOG_INFO, "Interactive ReShade mode disabled");
            } else {
                activate();
            }
        }
        previous_hotkey = pressed;

        windows::InteractiveState state{};
        if (active_) {
            std::lock_guard lock(mutex_);
            state = presentation_state_;
        }
        if (active_ && (!state.window || !IsWindow(state.window))) {
            deactivate(false);
            log(LSPROXY_LOG_WARN, "Interactive mode ended because the presentation window closed");
        }
    }
}

}  // namespace lsc
