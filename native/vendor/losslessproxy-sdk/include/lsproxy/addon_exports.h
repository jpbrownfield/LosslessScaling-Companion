#pragma once
#include "ihost.h"

struct ImGuiContext;

#define LSPROXY_EXPORT extern "C" __declspec(dllexport)

enum LsProxyAddonCaps : std::uint32_t {
    LSPROXY_CAP_NONE = 0,
    LSPROXY_CAP_HAS_SETTINGS = 1u << 0,
    LSPROXY_CAP_REQUIRES_RESTART = 1u << 1,
    LSPROXY_CAP_PATCH_LS1_LOGIC = 1u << 3,
    LSPROXY_CAP_D3D11_DEVICE_ACCESS = 1u << 4,
    LSPROXY_CAP_DISPATCH_HOOK = 1u << 5,
};
