#pragma once

#include "window_target.hpp"
#include <lsproxy/ihost.h>

#include <atomic>
#include <mutex>
#include <thread>

namespace lsc {

class ReShadeBridge {
public:
    static ReShadeBridge& instance();

    void start(IHost* host);
    void stop();

private:
    ReShadeBridge() = default;
    ~ReShadeBridge() = default;
    ReShadeBridge(const ReShadeBridge&) = delete;
    ReShadeBridge& operator=(const ReShadeBridge&) = delete;

    void run();
    bool hotkey_down() const;
    bool activate();
    void deactivate(bool return_focus);
    void log(LsProxyLogLevel level, const char* format, ...) const;

    mutable std::mutex mutex_;
    IHost* host_ = nullptr;
    std::thread worker_;
    HANDLE stop_event_ = nullptr;
    std::atomic<bool> running_{false};
    std::atomic<bool> active_{false};
    int hotkey_ = VK_END;
    bool require_ctrl_ = false;
    bool require_alt_ = false;
    bool require_shift_ = false;
    bool require_win_ = false;
    HWND target_ = nullptr;
    windows::InteractiveState presentation_state_{};
};

}  // namespace lsc
