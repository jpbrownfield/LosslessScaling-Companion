#include "bridge.hpp"
#include <lsproxy/addon_exports.h>

#include <windows.h>

LSPROXY_EXPORT void AddonInitialize(
    IHost* host,
    ImGuiContext*,
    void*,
    void*,
    void*
) {
    lsc::ReShadeBridge::instance().start(host);
}

LSPROXY_EXPORT void AddonShutdown() {
    lsc::ReShadeBridge::instance().stop();
}

LSPROXY_EXPORT std::uint32_t GetAddonCapabilities() {
    return LSPROXY_CAP_REQUIRES_RESTART;
}

LSPROXY_EXPORT const char* GetAddonName() {
    return "LS Companion ReShade Bridge";
}

LSPROXY_EXPORT const char* GetAddonVersion() {
    return "1.0.0";
}

LSPROXY_EXPORT const char* GetAddonAuthor() {
    return "LS Companion contributors; derived from LSP-ReShade";
}

LSPROXY_EXPORT const char* GetAddonDescription() {
    return "Focus-safe keyboard and mouse access for ReShade on Lossless Scaling.";
}

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(module);
    return TRUE;
}
