#include "report.h"
#include <lsproxy/addon_exports.h>

#include <windows.h>

LSPROXY_EXPORT void AddonInitialize(
    IHost* host,
    ImGuiContext*,
    void*,
    void*,
    void*
) {
    // Never let reporting break scaling: all failures are swallowed inside.
    __try {
        void* device = host ? host->GetD3D11Device() : nullptr;
        std::uint32_t version = host ? host->GetHostVersion() : 0;
        lsc::WriteAdapterReport(device, version);
        // LS often creates its D3D device after add-ons load. A short-lived
        // watcher re-reports once a device exists; AddonInitialize itself
        // returns immediately so startup is never blocked.
        HANDLE thread = CreateThread(
            nullptr, 0, lsc::ReportRetryThread, host, 0, nullptr);
        if (thread) CloseHandle(thread);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
    }
}

LSPROXY_EXPORT void AddonShutdown() {
}

LSPROXY_EXPORT std::uint32_t GetAddonCapabilities() {
    return LSPROXY_CAP_REQUIRES_RESTART;
}

LSPROXY_EXPORT const char* GetAddonName() {
    return "LS Companion Adapter Report";
}

LSPROXY_EXPORT const char* GetAddonVersion() {
    return "1.0.0";
}

LSPROXY_EXPORT const char* GetAddonAuthor() {
    return "LS Companion contributors";
}

LSPROXY_EXPORT const char* GetAddonDescription() {
    return "Reports the physical GPU Lossless Scaling renders with so the "
           "companion can route scaling without trial-and-error probing.";
}

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(module);
    (void)module;
    return TRUE;
}
