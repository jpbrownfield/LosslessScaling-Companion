#pragma once
#include "events.h"
#include <cstdint>

enum LsProxyLogLevel : std::uint8_t {
    LSPROXY_LOG_TRACE = 0,
    LSPROXY_LOG_DEBUG = 1,
    LSPROXY_LOG_INFO = 2,
    LSPROXY_LOG_WARN = 3,
    LSPROXY_LOG_ERROR = 4,
};

using LsProxyPreDispatchCallback = bool (*)(
    std::uint32_t, std::uint32_t, std::uint32_t, void*
);
using LsProxyPostDispatchCallback = void (*)(
    std::uint32_t, std::uint32_t, std::uint32_t, void*
);

// ABI snapshot from LosslessProxy 0.3.x. Keep method order unchanged.
struct IHost {
    virtual ~IHost() = default;
    virtual void Log(LsProxyLogLevel level, const char* message) = 0;
    virtual const char* GetConfig(
        const char* addon_id, const char* key, const char* default_value = ""
    ) = 0;
    virtual void SetConfig(const char* addon_id, const char* key, const char* value) = 0;
    virtual void SaveConfig() = 0;
    virtual std::uint32_t GetHostVersion() = 0;
    virtual void SubscribeEvent(
        std::uint32_t event_id, LsProxyEventCallback callback, void* user_data = nullptr
    ) = 0;
    virtual void UnsubscribeEvent(std::uint32_t event_id, LsProxyEventCallback callback) = 0;
    virtual void PublishEvent(
        std::uint32_t event_id, const void* data = nullptr, std::uint32_t data_size = 0
    ) = 0;
    virtual void* GetD3D11Device() = 0;
    virtual void* GetD3D11DeviceContext() = 0;
    virtual void SetPreDispatchCallback(
        LsProxyPreDispatchCallback callback, void* user_data = nullptr
    ) = 0;
    virtual void SetPostDispatchCallback(
        LsProxyPostDispatchCallback callback, void* user_data = nullptr
    ) = 0;
    virtual void* GetCurrentComputeShader() = 0;
    virtual std::uint32_t GetDispatchCount() = 0;
};
