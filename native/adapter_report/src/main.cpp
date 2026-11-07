#include "report.h"
#include <lsproxy/addon_exports.h>

#include <windows.h>

namespace {
SRWLOCK lifecycle = SRWLOCK_INIT;
HANDLE worker = nullptr;
lsc::ReportRetryContext context;

// Called under lifecycle, outside DllMain. Never time out and return while a
// worker can still execute this DLL or dereference its borrowed IHost pointer.
void StopReporter() {
    if (context.stop) SetEvent(context.stop);
    if (worker) {
        WaitForSingleObject(worker, INFINITE);
        CloseHandle(worker);
        worker = nullptr;
    }
    if (context.stop) CloseHandle(context.stop);
    context = {};
}
}  // namespace

LSPROXY_EXPORT void AddonInitialize(
    IHost* host,
    ImGuiContext*,
    void*,
    void*,
    void*
) {
    AcquireSRWLockExclusive(&lifecycle);
    StopReporter();
    // A repeated initialize replaces the previous worker only after it exits.
    // The host owns its device, so retain it during each reporting attempt.
    if (host) {
        lsc::ReportHostAdapter(host, true);
        // Preserve the delayed refresh even when startup already has a device:
        // LS can replace its initial device while scaling initializes.
        context.host = host;
        context.stop = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        if (context.stop) {
            worker = CreateThread(nullptr, 0, lsc::ReportRetryThread, &context, 0, nullptr);
        }
        if (!worker) StopReporter();
    }
    ReleaseSRWLockExclusive(&lifecycle);
}

LSPROXY_EXPORT void AddonShutdown() {
    AcquireSRWLockExclusive(&lifecycle);
    StopReporter();
    ReleaseSRWLockExclusive(&lifecycle);
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
