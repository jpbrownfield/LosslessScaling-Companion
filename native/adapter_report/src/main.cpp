#include "report.h"
#include <lsproxy/addon_exports.h>

#include <windows.h>
#include <string>

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
    if (context.request) UnmapViewOfFile(context.request);
    if (context.requestMapping) CloseHandle(context.requestMapping);
    if (context.refresh) CloseHandle(context.refresh);
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
        const auto suffix = std::to_wstring(GetCurrentProcessId());
        context.refresh = CreateEventW(nullptr, FALSE, FALSE,
            (L"Local\\LSCAdapterReportRefresh-" + suffix).c_str());
        context.requestMapping = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE,
            0, sizeof(lsc::RuntimeReportRequest), (L"Local\\LSCAdapterReportRequest-" + suffix).c_str());
        if (context.requestMapping) {
            context.request = static_cast<const lsc::RuntimeReportRequest*>(
                MapViewOfFile(context.requestMapping, FILE_MAP_READ, 0, 0, sizeof(lsc::RuntimeReportRequest)));
        }
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
    return "1.1.0";
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
