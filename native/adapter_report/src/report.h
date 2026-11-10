#pragma once
#include <windows.h>

#include <cstdint>
#include <string>
#include <vector>

namespace lsc {

// Snapshot of the graphics adapters exactly as seen inside the LS process:
// the D3D11 device LS renders with, plus both DXGI enumeration orders.
// Companion matches these against its own inventory by PCI identity, so no
// blind GPU-ID probing is needed to route scaling at full speed.
struct AdapterInfo {
    std::wstring description;
    std::uint32_t vendorId = 0;
    std::uint32_t deviceId = 0;
    std::uint32_t subSysId = 0;
    std::uint32_t revision = 0;
    std::uint32_t luidLow = 0;
    std::int32_t luidHigh = 0;
    bool isSoftware = false;
};

struct OutputInfo {
    std::wstring deviceName;
    LONG left = 0;
    LONG top = 0;
    LONG right = 0;
    LONG bottom = 0;
    bool attached = false;
};

struct SettingsProfile {
    std::wstring title;
    int gpu = 0;
    int display = 0;
};

struct AdapterReport {
    std::uint32_t pid = 0;
    std::uint32_t hostVersion = 0;
    bool hasDevice = false;
    AdapterInfo device;
    std::vector<AdapterInfo> enumAdapters;
    std::vector<AdapterInfo> enumAdapters1;
    // Index of the device adapter inside each enumeration (-1 when absent).
    int deviceIndexAdapters = -1;
    int deviceIndexAdapters1 = -1;
    std::vector<OutputInfo> deviceOutputs;
    // Every native LS profile found in Settings.xml plus the root preference.
    // LS numbers GPUs per its own enumeration, so titles anchor numbers.
    std::vector<SettingsProfile> settingsProfiles;
    int rootGpuPreference = -1;
    std::vector<std::wstring> lsAdapterNames;
    std::string requestId;
    std::string lsAdapterStatus = "not_requested";
    std::uint32_t lsAdapterError = 0;
};

// No device creation or scaling. The optional names call is the exact export
// used by the supported LS UI and must only be enabled after its ABI is checked.
std::string CollectAdapterSnapshot(bool readLsNames = false, const std::string& requestId = {});

// Gathers the report and writes ls-adapter-report-<pid>.json under the
// companion diagnostics directory. Never throws; best effort only.
bool WriteAdapterReport(void* d3d11Device, std::uint32_t hostVersion, const char* requestId = nullptr);

// Shared request mailbox. Set the event only after writing this entire packet.
// A random 64-hex nonce ties the response to one activation, not just a PID.
struct RuntimeReportRequest {
    std::uint32_t version;
    char requestId[65];
};
static_assert(sizeof(RuntimeReportRequest) == 72);

// The owner keeps this context and its stop event alive until the worker joins.
struct ReportRetryContext {
    void* host = nullptr;
    HANDLE stop = nullptr;
    HANDLE refresh = nullptr;
    HANDLE requestMapping = nullptr;
    const RuntimeReportRequest* request = nullptr;
};

// Keeps a COM reference to any returned device until report collection finishes.
// Returns true only after a report containing a device was written.
bool ReportHostAdapter(void* host, bool allowEmptyReport = false, const char* requestId = nullptr) noexcept;

// Retries startup for ~30s, then sleeps until a refresh request or shutdown.
// No polling or per-frame callbacks after startup. The host must call
// AddonShutdown before destroying IHost or unloading this DLL.
DWORD WINAPI ReportRetryThread(void* context);

}  // namespace lsc
