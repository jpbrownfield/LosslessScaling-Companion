#include <windows.h>
#include "../adapter_report/src/report.h"
#include <cwchar>
#include <string>

namespace {
LONG started = 0;

std::wstring Environment(const wchar_t* name) {
    wchar_t value[32768]{};
    const auto size = GetEnvironmentVariableW(name, value, ARRAYSIZE(value));
    return size && size < ARRAYSIZE(value) ? std::wstring(value, size) : std::wstring();
}

DWORD WINAPI Collect(void*) {
    try {
        const auto directory = Environment(L"LSC_ADAPTER_REPORT_DIR");
        const auto token = Environment(L"LSC_ADAPTER_REPORT_TOKEN");
        if (directory.empty() || token.size() != 64 ||
                token.find_first_not_of(L"0123456789abcdef") != std::wstring::npos) return 1;
        // Let LS finish loading its engine/proxy. Never load one into LS here.
        for (int i = 0; i < 60 && !GetModuleHandleW(L"Lossless.dll"); ++i) Sleep(100);
        if (!GetModuleHandleW(L"Lossless.dll")) return 1;
        Sleep(300);
        std::string request;
        for (const auto character : token) request.push_back(static_cast<char>(character));
        const auto json = lsc::CollectAdapterSnapshot(true, request);
        const auto target = directory + L"\\ls-adapter-report-" + std::to_wstring(GetCurrentProcessId()) + L".json";
        const auto temporary = target + L"." + token + L".tmp";
        HANDLE file = CreateFileW(temporary.c_str(), GENERIC_WRITE, 0, nullptr,
                                  CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file == INVALID_HANDLE_VALUE) return 1;
        DWORD written = 0;
        const auto ok = WriteFile(file, json.data(), static_cast<DWORD>(json.size()), &written, nullptr);
        CloseHandle(file);
        if (!ok || written != json.size() || !MoveFileExW(temporary.c_str(), target.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
            DeleteFileW(temporary.c_str());
            return 1;
        }
        return 0;
    } catch (...) {
        return 1;
    }
}
}

void StartAdapterProbe(HMODULE module) noexcept {
    wchar_t token[65]{};
    if (GetEnvironmentVariableW(L"LSC_ADAPTER_REPORT_TOKEN", token, ARRAYSIZE(token)) != 64) return;
    if (InterlockedCompareExchange(&started, 1, 0)) return;
    HMODULE pinned = nullptr;
    // This bounded worker has no borrowed host/context. Pinning prevents code
    // unload even after the parent's short-lived window hook has been removed.
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
                          reinterpret_cast<LPCWSTR>(&StartAdapterProbe), &pinned)) return;
    HANDLE thread = CreateThread(nullptr, 0, Collect, nullptr, 0, nullptr);
    if (thread) CloseHandle(thread);
    (void)module;
}
