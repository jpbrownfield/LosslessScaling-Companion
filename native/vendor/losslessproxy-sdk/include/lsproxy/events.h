#pragma once
#include <cstdint>

enum LsProxyEvent : std::uint32_t {
    LSPROXY_EVENT_ADDON_LOADED = 1,
    LSPROXY_EVENT_ADDON_UNLOADED = 2,
    LSPROXY_EVENT_SETTINGS_CHANGED = 3,
    LSPROXY_EVENT_SHADER_INTERCEPTED = 4,
    LSPROXY_EVENT_HOST_SHUTDOWN = 5,
    LSPROXY_EVENT_SETTINGS_APPLIED = 6,
    LSPROXY_EVENT_D3D11_DEVICE_READY = 7,
    LSPROXY_EVENT_D3D11_DEVICE_CHANGED = 8,
    LSPROXY_EVENT_CUSTOM = 0x10000,
};

using LsProxyEventCallback = void (*)(
    std::uint32_t event_id,
    const void* data,
    std::uint32_t data_size,
    void* user_data
);
