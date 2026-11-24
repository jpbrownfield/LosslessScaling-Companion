// SPDX-License-Identifier: MIT
#pragma once
#include <d3d11.h>
#include <wrl/client.h>

namespace lsc {
// ReShade v6.8.0 source/com_utils.hpp and d3d11_device_context.cpp define this
// QueryInterface contract. It returns an owned reference to the native object.
inline constexpr GUID kUnwrappedContext = {
    0x7f2c9a11, 0x3b4e, 0x4d6a, {0x81,0x2f,0x5e,0x9c,0xd3,0x7a,0x1b,0x42}
};
inline Microsoft::WRL::ComPtr<ID3D11DeviceContext> NativeDispatchContext(IUnknown* object) {
    Microsoft::WRL::ComPtr<ID3D11DeviceContext> context;
    if (!object) return context;
    Microsoft::WRL::ComPtr<IUnknown> original;
    if (SUCCEEDED(object->QueryInterface(kUnwrappedContext, &original)) && original
            && SUCCEEDED(original.As(&context))) return context;
    object->QueryInterface(IID_PPV_ARGS(&context));
    return context;
}
}
