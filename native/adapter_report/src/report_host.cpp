// Off-LS fixture: proves the adapter reporter end-to-end without LS.
// Creates a D3D11 device on a chosen DXGI adapter, loads the real
// LSC_AdapterReport.dll, drives AddonInitialize through a real IHost vtable,
// and leaves ls-adapter-report-<pid>.json for inspection.
#include <windows.h>

#include <d3d11.h>
#include <dxgi.h>

#include <cstdio>
#include <string>

#include <lsproxy/addon_exports.h>

namespace {

class TestHost : public IHost {
public:
    explicit TestHost(ID3D11Device* device) : device_(device) {}

    void Log(LsProxyLogLevel, const char* message) override {
        printf("[host] %s\n", message ? message : "");
    }
    const char* GetConfig(const char*, const char*, const char* dflt) override {
        return dflt;
    }
    void SetConfig(const char*, const char*, const char*) override {}
    void SaveConfig() override {}
    std::uint32_t GetHostVersion() override { return 300; }
    void SubscribeEvent(std::uint32_t, LsProxyEventCallback, void*) override {}
    void UnsubscribeEvent(std::uint32_t, LsProxyEventCallback) override {}
    void PublishEvent(std::uint32_t, const void*, std::uint32_t) override {}
    void* GetD3D11Device() override { return device_; }
    void* GetD3D11DeviceContext() override { return nullptr; }
    void SetPreDispatchCallback(LsProxyPreDispatchCallback, void*) override {}
    void SetPostDispatchCallback(LsProxyPostDispatchCallback, void*) override {}
    void* GetCurrentComputeShader() override { return nullptr; }
    std::uint32_t GetDispatchCount() override { return 0; }

private:
    ID3D11Device* device_;
};

}  // namespace

using AddonInitFn = void (*)(IHost*, ImGuiContext*, void*, void*, void*);

int CallInit(AddonInitFn init, IHost* host) {
    __try {
        init(host, nullptr, nullptr, nullptr, nullptr);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return GetExceptionCode();
    }
    return 0;
}

int wmain(int argc, wchar_t** argv) {
    UINT wanted = 0;
    if (argc > 1) wanted = static_cast<UINT>(_wtoi(argv[1]));

    IDXGIFactory1* probe = nullptr;
    // NOTE: CreateDXGIFactory only serves the 1.0 factory (E_NOINTERFACE for
    // anything newer); the 1.1 enumeration needs CreateDXGIFactory1. Kept
    // here as a regression probe because that exact mistake once shipped.
    using CreateFactory1 = HRESULT(WINAPI*)(REFIID, void**);
    auto createFactory1 = reinterpret_cast<CreateFactory1>(
        GetProcAddress(GetModuleHandleW(L"dxgi.dll"), "CreateDXGIFactory1"));
    HRESULT probeHr = E_NOTIMPL;
    if (createFactory1) {
        probeHr = createFactory1(__uuidof(IDXGIFactory1),
                                  reinterpret_cast<void**>(&probe));
    }
    printf("[probe] factory1 hr=0x%08lx ptr=%p\n", probeHr, (void*)probe);
    if (probeHr == S_OK && probe) {
        int count = 0;
        for (UINT i = 0; i < 16; ++i) {
            IDXGIAdapter1* adapter = nullptr;
            if (probe->EnumAdapters1(i, &adapter) != S_OK || !adapter) break;
            ++count;
            adapter->Release();
        }
        printf("[probe] EnumAdapters1 count=%d\n", count);
        probe->Release();
    } else {
        printf("[probe] factory1 unavailable\n");
    }

    IDXGIFactory* factory = nullptr;
    if (CreateDXGIFactory(__uuidof(IDXGIFactory),
                          reinterpret_cast<void**>(&factory)) != S_OK ||
        !factory) {
        printf("FAIL: no DXGI factory\n");
        return 2;
    }
    IDXGIAdapter* adapter = nullptr;
    if (factory->EnumAdapters(wanted, &adapter) != S_OK || !adapter) {
        printf("FAIL: no adapter %u\n", wanted);
        factory->Release();
        return 2;
    }
    factory->Release();

    ID3D11Device* device = nullptr;
    D3D_FEATURE_LEVEL level = D3D_FEATURE_LEVEL_11_0;
    HRESULT hr = D3D11CreateDevice(
        adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0, nullptr, 0,
        D3D11_SDK_VERSION, &device, &level, nullptr);
    adapter->Release();
    if (hr != S_OK || !device) {
        printf("FAIL: D3D11CreateDevice 0x%08lx\n", hr);
        return 2;
    }

    wchar_t path[32768]{};
    DWORD length = GetModuleFileNameW(nullptr, path, ARRAYSIZE(path));
    std::wstring dll = path;
    const size_t slash = dll.find_last_of(L"\\/");
    dll = dll.substr(0, slash + 1) + L"LSC_AdapterReport.dll";
    HMODULE module = LoadLibraryW(dll.c_str());
    if (!module) {
        printf("FAIL: LoadLibrary 0x%08lx\n", GetLastError());
        device->Release();
        return 2;
    }
    using InitFn = AddonInitFn;
    auto init = reinterpret_cast<InitFn>(
        GetProcAddress(module, "AddonInitialize"));
    if (!init) {
        printf("FAIL: no AddonInitialize export\n");
        device->Release();
        return 2;
    }
    TestHost host(device);
    if (CallInit(init, &host) != 0) {
        printf("FAIL: AddonInitialize crashed\n");
        device->Release();
        return 2;
    }
    // The host/device must outlive all reporter callbacks and worker threads.
    auto shutdown = reinterpret_cast<void (*)()>(GetProcAddress(module, "AddonShutdown"));
    if (!shutdown) {
        printf("FAIL: no AddonShutdown export\n");
        return 2;
    }
    shutdown();
    FreeLibrary(module);
    device->Release();

    wchar_t localAppData[32768]{};
    length = GetEnvironmentVariableW(L"LOCALAPPDATA", localAppData,
                                     ARRAYSIZE(localAppData));
    std::wstring report = std::wstring(localAppData) +
                          L"\\LosslessScalingHelper\\diagnostics\\"
                          L"ls-adapter-report-" +
                          std::to_wstring(GetCurrentProcessId()) + L".json";
    DWORD attributes = GetFileAttributesW(report.c_str());
    if (attributes == INVALID_FILE_ATTRIBUTES) {
        printf("FAIL: no report at %ls\n", report.c_str());
        return 2;
    }
    printf("OK %ls\n", report.c_str());
    return 0;
}
