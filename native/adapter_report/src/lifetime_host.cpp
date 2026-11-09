// Run the real DLL against a host whose lifetime is controlled by the test.
// No Lossless Scaling process, scaling hotkeys, or D3D device is needed.
#include <windows.h>
#include <lsproxy/addon_exports.h>
#include "report.h"

#include <atomic>
#include <cstdio>
#include <string>
#include <thread>
#include <cstring>

namespace {
class TestHost final : public IHost {
public:
    HANDLE entered = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    HANDLE resume = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    std::atomic<bool> alive{true};
    std::atomic<bool> block{false};
    std::atomic<int> calls{0};
    std::atomic<int> invalidCalls{0};

    ~TestHost() override {
        CloseHandle(entered);
        CloseHandle(resume);
    }
    void Log(LsProxyLogLevel, const char*) override {}
    const char* GetConfig(const char*, const char*, const char* fallback) override { return fallback; }
    void SetConfig(const char*, const char*, const char*) override {}
    void SaveConfig() override {}
    std::uint32_t GetHostVersion() override { return 300; }
    void SubscribeEvent(std::uint32_t, LsProxyEventCallback, void*) override {}
    void UnsubscribeEvent(std::uint32_t, LsProxyEventCallback) override {}
    void PublishEvent(std::uint32_t, const void*, std::uint32_t) override {}
    void* GetD3D11Device() override {
        ++calls;
        if (!alive) ++invalidCalls;
        if (block) {
            SetEvent(entered);
            WaitForSingleObject(resume, 10000);
        }
        return nullptr;
    }
    void* GetD3D11DeviceContext() override { return nullptr; }
    void SetPreDispatchCallback(LsProxyPreDispatchCallback, void*) override {}
    void SetPostDispatchCallback(LsProxyPostDispatchCallback, void*) override {}
    void* GetCurrentComputeShader() override { return nullptr; }
    std::uint32_t GetDispatchCount() override { return 0; }
};

using Init = void (*)(IHost*, ImGuiContext*, void*, void*, void*);
using Shutdown = void (*)();

bool Check(bool condition, const char* reason) {
    if (!condition) printf("FAIL: %s\n", reason);
    return condition;
}

std::string ReadSnapshot(const std::wstring& path, DWORD& error) {
    // The reporter publishes by renaming a complete file. Do not let the
    // polling reader prevent that replacement while it reads the old snapshot.
    const HANDLE file = CreateFileW(path.c_str(), GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) {
        error = GetLastError();
        return {};
    }
    std::string result;
    char buffer[4096];
    DWORD read = 0;
    BOOL success = TRUE;
    error = ERROR_SUCCESS;
    while ((success = ReadFile(file, buffer, sizeof(buffer), &read, nullptr))) {
        if (!read) break;
        result.append(buffer, read);
    }
    if (!success) error = GetLastError();
    CloseHandle(file);
    return result;
}

bool Unload(HMODULE module) {
    return Check(FreeLibrary(module) != FALSE, "DLL unload failed") &&
        Check(GetModuleHandleW(L"LSC_AdapterReport.dll") == nullptr,
              "reporter DLL remained loaded after shutdown");
}
}  // namespace

int wmain(int argc, wchar_t** argv) {
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);
    if (argc != 2) return 2;
    wchar_t executable[32768]{};
    GetModuleFileNameW(nullptr, executable, ARRAYSIZE(executable));
    std::wstring path = executable;
    path.resize(path.find_last_of(L"\\/") + 1);
    path += L"LSC_AdapterReport.dll";
    const HMODULE module = LoadLibraryW(path.c_str());
    if (!Check(module != nullptr, "could not load reporter DLL")) return 1;
    const auto initialize = reinterpret_cast<Init>(GetProcAddress(module, "AddonInitialize"));
    const auto shutdown = reinterpret_cast<Shutdown>(GetProcAddress(module, "AddonShutdown"));
    if (!Check(initialize && shutdown, "missing lifecycle exports")) return 1;
    const std::wstring mode = argv[1];
    TestHost first, second;
    if (!Check(first.entered && first.resume && second.entered && second.resume,
               "could not create fixture events")) return 1;

    if (mode == L"listen") {
        initialize(&first, nullptr, nullptr, nullptr, nullptr);
        printf("READY %lu\n", GetCurrentProcessId());
        fflush(stdout);
        getchar();
        shutdown();
        first.alive = false;
        if (!Unload(module)) return 1;
    } else if (mode == L"refresh" || mode == L"refresh-locked") {
        initialize(&first, nullptr, nullptr, nullptr, nullptr);
        const auto suffix = std::to_wstring(GetCurrentProcessId());
        const HANDLE event = OpenEventW(EVENT_MODIFY_STATE, FALSE,
            (L"Local\\LSCAdapterReportRefresh-" + suffix).c_str());
        const HANDLE mapping = OpenFileMappingW(FILE_MAP_WRITE, FALSE,
            (L"Local\\LSCAdapterReportRequest-" + suffix).c_str());
        auto* request = mapping ? static_cast<lsc::RuntimeReportRequest*>(
            MapViewOfFile(mapping, FILE_MAP_WRITE, 0, 0, sizeof(lsc::RuntimeReportRequest))) : nullptr;
        if (!Check(event && request, "runtime request endpoint unavailable")) return 1;
        wchar_t local[32768]{};
        GetEnvironmentVariableW(L"LOCALAPPDATA", local, ARRAYSIZE(local));
        const auto file = std::wstring(local) + L"\\LosslessScalingHelper\\diagnostics\\ls-adapter-report-" + suffix + L".json";
        bool matched = true;
        for (char token : {'a', 'b'}) {
            HANDLE reader = INVALID_HANDLE_VALUE;
            if (mode == L"refresh-locked" && token == 'a') {
                // Model a normal CRT/Python reader that does not share delete.
                // Hold it past the old six-attempt rename retry window.
                reader = CreateFileW(file.c_str(), GENERIC_READ,
                    FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                    OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
                if (!Check(reader != INVALID_HANDLE_VALUE, "could not lock initial snapshot")) return 1;
                first.block = true;
            }
            request->version = 1;
            memset(request->requestId, token, 64);
            request->requestId[64] = 0;
            if (!Check(SetEvent(event) != FALSE, "could not signal runtime request")) return 1;
            if (reader != INVALID_HANDLE_VALUE) {
                const bool entered = WaitForSingleObject(first.entered, 6000) == WAIT_OBJECT_0;
                SetEvent(first.resume);
                if (entered) Sleep(250);
                CloseHandle(reader);
                first.block = false;
                if (!Check(entered, "refresh worker never entered its host call")) {
                    shutdown();
                    return 1;
                }
            }
            bool found = false;
            DWORD error = ERROR_SUCCESS;
            std::string json;
            const auto started = GetTickCount64();
            // CI scheduling and DXGI enumeration need not finish within one
            // second. Keep a bounded wait without re-signalling a lost request.
            while (!found && GetTickCount64() - started < 5000) {
                json = ReadSnapshot(file, error);
                found = json.find("\"requestId\":\"" + std::string(64, token) + "\"") != std::string::npos;
                if (!found) Sleep(10);
            }
            if (!found) {
                printf("FAIL: request %c timed out after %llu ms (read error %lu, host calls %d)\n",
                    token, GetTickCount64() - started, error, first.calls.load());
                printf("Last snapshot: %s\n", json.c_str());
            }
            matched = matched && found;
        }
        UnmapViewOfFile(request);
        CloseHandle(mapping);
        CloseHandle(event);
        shutdown();
        first.alive = false;
        if (!Check(matched, "fresh report did not acknowledge both unique requests") || !Unload(module)) return 1;
    } else if (mode == L"cancel") {
        // Warm DXGI's own caches before measuring our event/thread handles.
        initialize(&first, nullptr, nullptr, nullptr, nullptr);
        shutdown();
        DWORD before = 0, after = 0;
        GetProcessHandleCount(GetCurrentProcess(), &before);
        ULONGLONG shutdownTime = 0;
        for (int i = 0; i < 10; ++i) {
            initialize(&first, nullptr, nullptr, nullptr, nullptr);
            const auto started = GetTickCount64();
            shutdown();
            shutdown();  // Idempotent, including after the worker was joined.
            shutdownTime += GetTickCount64() - started;
        }
        GetProcessHandleCount(GetCurrentProcess(), &after);
        if (!Check(shutdownTime < 5000, "shutdown waited for polling sleeps") ||
            !Check(after == before, "event/thread handles leaked")) return 1;
        first.alive = false;
        if (!Unload(module)) return 1;
        // An unjoined worker resumes in unloaded code after its 3-second sleep.
        Sleep(3300);
        if (!Check(first.invalidCalls == 0, "host accessed after shutdown")) return 1;
    } else if (mode == L"in-flight") {
        initialize(&first, nullptr, nullptr, nullptr, nullptr);
        first.block = true;
        if (!Check(WaitForSingleObject(first.entered, 6000) == WAIT_OBJECT_0,
                   "worker never entered its host call")) {
            SetEvent(first.resume);
            shutdown();
            return 1;
        }
        const HANDLE finished = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        std::thread stopping([&] { shutdown(); SetEvent(finished); });
        const bool waited = WaitForSingleObject(finished, 150) == WAIT_TIMEOUT;
        SetEvent(first.resume);
        stopping.join();
        CloseHandle(finished);
        if (!Check(waited, "shutdown returned while a host call was still running")) return 1;
        first.alive = false;
        if (!Unload(module)) return 1;
    } else if (mode == L"reinitialize") {
        initialize(&first, nullptr, nullptr, nullptr, nullptr);
        initialize(&second, nullptr, nullptr, nullptr, nullptr);
        first.alive = false;
        Sleep(3300);
        shutdown();
        second.alive = false;
        if (!Check(first.invalidCalls == 0, "previous host accessed after reinitialization") ||
            !Check(first.calls == 1, "previous worker survived reinitialization") ||
            !Check(second.calls >= 2, "replacement worker did not poll its host")) return 1;
        if (!Unload(module)) return 1;
    } else {
        return 2;
    }
    printf("OK %ls\n", mode.c_str());
    return 0;
}
