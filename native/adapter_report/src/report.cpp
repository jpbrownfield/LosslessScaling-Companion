// Adapter ground truth reporter: runs inside the LS process as a
// LosslessProxy add-on and records which physical GPU LS renders with,
// in which enumeration order, and which Settings IDs are active.
// Companion reads the JSON instead of probing GPU IDs by trial scaling.
#include "report.h"

#include <dxgi.h>
#include <d3d11.h>
#include <lsproxy/addon_exports.h>

#include <cstdio>
#include <cwchar>
#include <oleauto.h>
#include <stdexcept>

namespace lsc {
constexpr wchar_t kSoftwareDesc[] = L"Microsoft Basic Render Driver";

bool SameAdapter(const AdapterInfo& a, const AdapterInfo& b) {
    return a.luidLow == b.luidLow && a.luidHigh == b.luidHigh &&
           a.vendorId == b.vendorId && a.deviceId == b.deviceId;
}

AdapterInfo FromDesc(const DXGI_ADAPTER_DESC& desc) {
    AdapterInfo info;
    info.description = desc.Description;
    info.vendorId = desc.VendorId;
    info.deviceId = desc.DeviceId;
    info.subSysId = desc.SubSysId;
    info.revision = desc.Revision;
    info.luidLow = desc.AdapterLuid.LowPart;
    info.luidHigh = desc.AdapterLuid.HighPart;
    info.isSoftware = wcsstr(desc.Description, kSoftwareDesc) != nullptr;
    return info;
}

void AppendEnum(IUnknown* factory, bool useAdapters1,
                std::vector<AdapterInfo>& out) {
    if (!factory) return;
    auto* factory0 = static_cast<IDXGIFactory*>(factory);
    auto* factory1 = static_cast<IDXGIFactory1*>(factory);
    for (UINT i = 0; i < 256; ++i) {
        if (useAdapters1) {
            IDXGIAdapter1* adapter1 = nullptr;
            if (factory1->EnumAdapters1(i, &adapter1) != S_OK || !adapter1) break;
            DXGI_ADAPTER_DESC desc{};
            if (adapter1->GetDesc(&desc) == S_OK) out.push_back(FromDesc(desc));
            adapter1->Release();
            continue;
        }
        IDXGIAdapter* adapter = nullptr;
        if (factory0->EnumAdapters(i, &adapter) != S_OK || !adapter) break;
        DXGI_ADAPTER_DESC desc{};
        if (adapter->GetDesc(&desc) == S_OK) out.push_back(FromDesc(desc));
        adapter->Release();
    }
    // No releases here: the caller owns the factory object.
}

int IndexOf(const std::vector<AdapterInfo>& list, const AdapterInfo& target) {
    for (size_t i = 0; i < list.size(); ++i) {
        if (SameAdapter(list[i], target)) return static_cast<int>(i);
    }
    return -1;
}

void AppendOutputs(IDXGIAdapter* adapter, std::vector<OutputInfo>& out) {
    if (!adapter) return;
    for (UINT i = 0; i < 32; ++i) {
        IDXGIOutput* output = nullptr;
        if (adapter->EnumOutputs(i, &output) != S_OK || !output) break;
        DXGI_OUTPUT_DESC desc{};
        if (output->GetDesc(&desc) == S_OK) {
            OutputInfo info;
            info.deviceName = desc.DeviceName;
            info.left = desc.DesktopCoordinates.left;
            info.top = desc.DesktopCoordinates.top;
            info.right = desc.DesktopCoordinates.right;
            info.bottom = desc.DesktopCoordinates.bottom;
            info.attached = desc.AttachedToDesktop != FALSE;
            out.push_back(info);
        }
        output->Release();
    }
}

// Best-effort scan of every native profile in Settings.xml. LS itself owns
// profile membership (users add/rename titles), so the reporter records them
// all instead of assuming a fixed title.
bool ReadSettingsProfiles(std::vector<SettingsProfile>& profiles,
                          int& rootGpu) {
    profiles.clear();
    rootGpu = -1;
    wchar_t localAppData[32768]{};
    DWORD length = GetEnvironmentVariableW(L"LOCALAPPDATA", localAppData,
                                           ARRAYSIZE(localAppData));
    if (!length || length >= ARRAYSIZE(localAppData)) return false;
    std::wstring path = std::wstring(localAppData) +
                        L"\\Lossless Scaling\\Settings.xml";
    FILE* file = nullptr;
    if (_wfopen_s(&file, path.c_str(), L"rb") != 0 || !file) return false;
    std::string text;
    char chunk[4096]{};
    size_t count = 0;
    while (count < 131072 &&
           (count = fread(chunk, 1, sizeof(chunk), file)) > 0) {
        text.append(chunk, count);
        if (text.size() > 131072) break;
    }
    fclose(file);
    auto number_after = [&](size_t from, const char* tag, int& value) {
        const std::string open = std::string("<") + tag + ">";
        const size_t begin = text.find(open, from);
        if (begin == std::string::npos) return false;
        value = atoi(text.c_str() + begin + open.size());
        return true;
    };
    int root = 0;
    if (number_after(0, "GpuPreference", root)) rootGpu = root;
    size_t title = 0;
    while ((title = text.find("<Title>", title)) != std::string::npos) {
        const size_t end = text.find("</Title>", title);
        if (end == std::string::npos) break;
        SettingsProfile profile;
        const std::string name = text.substr(
            title + 7, (std::min)(end - title - 7, size_t{128}));
        profile.title.assign(name.begin(), name.end());
        number_after(title, "PreferredGpuId", profile.gpu);
        number_after(title, "OutputDisplayId", profile.display);
        bool known = false;
        for (const auto& existing : profiles) {
            if (existing.title == profile.title) {
                known = true;
                break;
            }
        }
        if (!known) profiles.push_back(profile);
        title = end + 8;
    }
    return !profiles.empty();
}

void EscapeJson(const std::wstring& value, std::string& out) {
    for (wchar_t c : value) {
        switch (c) {
            case L'"': out += "\\\""; break;
            case L'\\': out += "\\\\"; break;
            default:
                if (c < 0x20 || c >= 0x80) {
                    char buffer[8]{};
                    sprintf_s(buffer, "\\u%04x", c);
                    out += buffer;
                } else if (c < 0x80) {
                    out += static_cast<char>(c);
                }
        }
    }
}

void AppendAdapterJson(const AdapterInfo& info, std::string& out) {
    out += "{\"description\":\"";
    EscapeJson(info.description, out);
    char buffer[256]{};
    sprintf_s(buffer,
              "\",\"vendorId\":%u,\"deviceId\":%u,\"subSysId\":%u,"
              "\"revision\":%u,\"luidLow\":%u,\"luidHigh\":%d,"
              "\"isSoftware\":%s}",
              info.vendorId, info.deviceId, info.subSysId, info.revision,
              info.luidLow, info.luidHigh, info.isSoftware ? "true" : "false");
    out += buffer;
}

std::string Serialize(const AdapterReport& report) {
    std::string out = "{\"schema\":1,\"pid\":";
    out += std::to_string(report.pid);
    out += ",\"hostVersion\":" + std::to_string(report.hostVersion);
    out += ",\"hasDevice\":";
    out += report.hasDevice ? "true" : "false";
    out += ",\"device\":";
    if (report.hasDevice) {
        AppendAdapterJson(report.device, out);
    } else {
        out += "null";
    }
    auto list = [&](const char* name, const std::vector<AdapterInfo>& items) {
        out += ",\"";
        out += name;
        out += "\":[";
        for (size_t i = 0; i < items.size(); ++i) {
            if (i) out += ",";
            AppendAdapterJson(items[i], out);
        }
        out += "]";
    };
    list("enumAdapters", report.enumAdapters);
    list("enumAdapters1", report.enumAdapters1);
    out += ",\"deviceIndexAdapters\":" + std::to_string(report.deviceIndexAdapters);
    out += ",\"deviceIndexAdapters1\":" + std::to_string(report.deviceIndexAdapters1);
    out += ",\"deviceOutputs\":[";
    for (size_t i = 0; i < report.deviceOutputs.size(); ++i) {
        if (i) out += ",";
        const auto& output = report.deviceOutputs[i];
        out += "{\"deviceName\":\"";
        EscapeJson(output.deviceName, out);
        char buffer[128]{};
        sprintf_s(buffer, "\",\"rect\":[%ld,%ld,%ld,%ld],\"attached\":%s}",
                  output.left, output.top, output.right, output.bottom,
                  output.attached ? "true" : "false");
        out += buffer;
    }
    out += "],\"settingsProfiles\":[";
    for (size_t i = 0; i < report.settingsProfiles.size(); ++i) {
        if (i) out += ",";
        out += "{\"title\":\"";
        EscapeJson(report.settingsProfiles[i].title, out);
        out += "\",\"gpu\":";
        out += std::to_string(report.settingsProfiles[i].gpu);
        out += ",\"display\":";
        out += std::to_string(report.settingsProfiles[i].display);
        out += "}";
    }
    out += "],\"rootGpuPreference\":";
    out += std::to_string(report.rootGpuPreference);
    out += ",\"requestId\":\"" + report.requestId + "\",\"lsAdapterNames\":[";
    for (size_t i = 0; i < report.lsAdapterNames.size(); ++i) {
        if (i) out += ",";
        out += "\"";
        EscapeJson(report.lsAdapterNames[i], out);
        out += "\"";
    }
    out += "]";
    out += "}";
    return out;
}

bool WriteReportFile(const std::wstring& path, const std::string& content) {
    HANDLE file = CreateFileW(path.c_str(), GENERIC_WRITE, 0, nullptr,
                              CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;
    DWORD written = 0;
    const BOOL ok = WriteFile(file, content.data(),
                              static_cast<DWORD>(content.size()), &written,
                              nullptr);
    CloseHandle(file);
    return ok && written == content.size();
}

static AdapterReport CollectReport(void* d3d11Device, std::uint32_t hostVersion) {
    AdapterReport report;
    report.pid = GetCurrentProcessId();
    report.hostVersion = hostVersion;

    IDXGIAdapter* deviceAdapter = nullptr;
    if (d3d11Device) {
        auto* device = static_cast<ID3D11Device*>(d3d11Device);
        IDXGIDevice* dxgiDevice = nullptr;
        if (device->QueryInterface(__uuidof(IDXGIDevice),
                                   reinterpret_cast<void**>(&dxgiDevice)) == S_OK &&
            dxgiDevice) {
            if (dxgiDevice->GetAdapter(&deviceAdapter) != S_OK || !deviceAdapter) {
                deviceAdapter = nullptr;
            }
            dxgiDevice->Release();
        }
    }
    if (deviceAdapter) {
        DXGI_ADAPTER_DESC desc{};
        if (deviceAdapter->GetDesc(&desc) == S_OK) {
            report.device = FromDesc(desc);
            report.hasDevice = true;
        }
        AppendOutputs(deviceAdapter, report.deviceOutputs);
    }

    IDXGIFactory* factory = nullptr;
    if (CreateDXGIFactory(__uuidof(IDXGIFactory),
                          reinterpret_cast<void**>(&factory)) == S_OK &&
        factory) {
        AppendEnum(factory, false, report.enumAdapters);
        // NOTE: CreateDXGIFactory only serves the 1.0 factory; the 1.1
        // enumeration needs its own entry point, not a QueryInterface.
        IDXGIFactory1* factory1 = nullptr;
        if (CreateDXGIFactory1(__uuidof(IDXGIFactory1),
                               reinterpret_cast<void**>(&factory1)) == S_OK &&
            factory1) {
            AppendEnum(factory1, true, report.enumAdapters1);
            factory1->Release();
        }
        factory->Release();
    }
    if (report.hasDevice) {
        report.deviceIndexAdapters = IndexOf(report.enumAdapters, report.device);
        report.deviceIndexAdapters1 = IndexOf(report.enumAdapters1, report.device);
    }
    int rootGpu = -1;
    if (ReadSettingsProfiles(report.settingsProfiles, rootGpu)) {
        report.rootGpuPreference = rootGpu;
    }
    if (deviceAdapter) deviceAdapter->Release();

    return report;
}

// Keep SEH separate from functions with C++ destructors. Never load a proxy or
// engine on behalf of the probe; LS must already have loaded its own module.
static BSTR ReadLsNames() {
    HMODULE module = nullptr;
    BSTR result = nullptr;
    __try {
        // Retain an already-loaded engine for the entire call. LS/proxy
        // shutdown must not unload the getter underneath this worker.
        GetModuleHandleExW(0, L"Lossless.dll", &module);
        const auto getter = module ? reinterpret_cast<BSTR(__cdecl*)()>(
            GetProcAddress(module, "GetAdapterNames")) : nullptr;
        result = getter ? getter() : nullptr;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        result = nullptr;
    }
    if (module) FreeLibrary(module);
    return result;
}

std::string CollectAdapterSnapshot(bool readLsNames, const std::string& requestId) {
    auto report = CollectReport(nullptr, 0);
    report.requestId = requestId;
    if (readLsNames) {
        BSTR names = ReadLsNames();
        if (names) {
            try {
                const auto size = SysStringLen(names);
                if (size > 16384) throw std::length_error("Oversized LS adapter list");
                const std::wstring text(names, size);
                size_t from = 0;
                for (size_t to = 0; to <= text.size(); ++to) {
                    if (to == text.size() || text[to] == L';') {
                        if (to > from) report.lsAdapterNames.push_back(text.substr(from, to - from));
                        from = to + 1;
                    }
                }
            } catch (...) {
                SysFreeString(names);
                throw;
            }
            SysFreeString(names);
        }
    }
    return Serialize(report);
}

static bool WriteAdapterReportImpl(void* d3d11Device, std::uint32_t hostVersion, const char* requestId) {
    auto report = CollectReport(d3d11Device, hostVersion);
    if (requestId) report.requestId = requestId;
    wchar_t localAppData[32768]{};
    DWORD length = GetEnvironmentVariableW(L"LOCALAPPDATA", localAppData,
                                           ARRAYSIZE(localAppData));
    if (!length || length >= ARRAYSIZE(localAppData)) return false;
    std::wstring directory = std::wstring(localAppData) +
                             L"\\LosslessScalingHelper\\diagnostics";
    CreateDirectoryW(directory.c_str(), nullptr);
    wchar_t name[64]{};
    swprintf_s(name, L"ls-adapter-report-%u.json", report.pid);
    const auto target = directory + L"\\" + name;
    const auto temporary = target + L".runtime-tmp";
    if (!WriteReportFile(temporary, Serialize(report))) return false;
    for (int attempt = 0; attempt < 6; ++attempt) {
        if (MoveFileExW(temporary.c_str(), target.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) return true;
        const auto error = GetLastError();
        if (error != ERROR_SHARING_VIOLATION && error != ERROR_LOCK_VIOLATION) break;
        Sleep(2);  // Readers may briefly hold a file without FILE_SHARE_DELETE.
    }
    DeleteFileW(temporary.c_str());
    return false;
}

bool WriteAdapterReport(void* d3d11Device, std::uint32_t hostVersion, const char* requestId) {
    // Reporting must not unwind an allocation/string exception into the host
    // or terminate the process from the native worker entry point.
    try {
        return WriteAdapterReportImpl(d3d11Device, hostVersion, requestId);
    } catch (...) {
        return false;
    }
}

bool ReportHostAdapter(void* host, bool allowEmptyReport, const char* requestId) noexcept {
    // No C++ objects with destructors here so SEH stays legal. The lifecycle
    // owner joins this call before permitting the host to be destroyed.
    ID3D11Device* retained = nullptr;
    bool written = false;
    __try {
        auto* api = static_cast<IHost*>(host);
        auto* borrowed = api ? static_cast<ID3D11Device*>(api->GetD3D11Device()) : nullptr;
        if (borrowed) {
            borrowed->AddRef();
            retained = borrowed;
        }
        if (retained || allowEmptyReport) {
            const auto version = api ? api->GetHostVersion() : 0;
            written = WriteAdapterReport(retained, version, requestId);
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        written = false;
    }
    const bool hasDevice = retained != nullptr;
    if (retained) {
        __try {
            retained->Release();
        } __except (EXCEPTION_EXECUTE_HANDLER) {
        }
    }
    return hasDevice && written;
}

DWORD WINAPI ReportRetryThread(void* argument) {
    const auto* context = static_cast<const ReportRetryContext*>(argument);
    int retries = 10;
    const bool canRefresh = context->refresh && context->request;
    const HANDLE events[] = {context->stop, context->refresh};
    for (;;) {
        const DWORD timeout = retries ? 3000 : INFINITE;
        const auto result = canRefresh ? WaitForMultipleObjects(2, events, FALSE, timeout)
                                       : WaitForSingleObject(context->stop, timeout);
        if (result == WAIT_OBJECT_0 || result == WAIT_FAILED) return 0;
        if (canRefresh && result == WAIT_OBJECT_0 + 1) {
            RuntimeReportRequest request = *context->request;
            bool valid = request.version == 1 && request.requestId[64] == '\0';
            for (int i = 0; i < 64 && valid; ++i) {
                const char c = request.requestId[i];
                valid = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
            }
            if (valid) ReportHostAdapter(context->host, true, request.requestId);
        } else if (result == WAIT_TIMEOUT) {
            --retries;
            if (ReportHostAdapter(context->host)) retries = 0;
        }
        if (!canRefresh && !retries) return 0;
    }
}

}  // namespace lsc
