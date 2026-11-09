// SPDX-License-Identifier: MIT
// Companion owns settings through stopped-profile deployment. This endpoint is
// read-only telemetry: no commands, paths, configuration, or GPU frame data.
#include "companion_bridge.h"
#include "d3d11_hook.h"
#include "real_frame_hook.h"
#include "../addon/addon_manager.h"
#include "../config/config_manager.h"
#include "../host/metrics.h"
#include "../log/logger.h"
#include "eam/real_frame_status.h"
#include "imgui.h"
#include <atomic>
#include <filesystem>
#include <fstream>
#include <string>
#include <tlhelp32.h>

namespace lsc {
namespace {
std::atomic<bool> headless{false};
std::atomic<unsigned> scalingVendor{0};
std::atomic<unsigned> nrDriverVendor{0};
std::atomic<bool> blockSm86{false};
std::atomic<void*> nvidiaSrModule{nullptr}, nvidiaNrModule{nullptr};
std::atomic<bool> automaticNr{false};
std::mutex sm86Mutex;
bool deferSm86=false, triedSm86=false;
using json = nlohmann::json;
json sm86={{"mode","Off"},{"loaderReady",false},{"gateAllowed",false},{"error",""}};

// Optional SR proxy, explicitly loaded before any upscaler initializes NGX.
// Its private deployment contains no FG runtime/pack and never occupies the
// application's version.dll/dxgi.dll slots (ReShade and LS keep their loaders).
std::string StartSm86() {
    auto& config=eam::ConfigManager::Instance();
    if (!config.IsAddonEnabled("DLSS4DLAA",false)) return {};
    const std::string mode=config.Get("DLSS4DLAA","companionSm86Mode","Off");
    if (mode.empty() || mode=="Off") return {};
    sm86["mode"]=mode;
    auto fail=[](const std::string& error) {
        sm86["error"]=error;
        // A selected optimized path must not silently run the stock upscaler.
        eam::ConfigManager::Instance().SetAddonEnabled("DLSS4DLAA",false);
        LOG_ERROR("Companion","SM86 SR: %s",error.c_str());
        return error;
    };
    if (mode!="L-INT8" && mode!="M-INT8") return fail("Unsupported SM86 SR mode");
    wchar_t exe[32768]{};
    if (!GetModuleFileNameW(nullptr,exe,32768)) return fail("Cannot locate the SM86 SR deployment");
    const auto folder=std::filesystem::path(exe).parent_path()/L"companion-runtimes"/L"SM86SR";
    const auto ini=folder/L"dlssg_sm86.ini";
    wchar_t chosen[32]{};
    GetPrivateProfileStringW(L"SuperResolution",L"Mode",L"",chosen,32,ini.c_str());
    if (std::wstring(chosen)!=std::wstring(mode.begin(),mode.end()) ||
        GetPrivateProfileIntW(L"General",L"Enabled",-1,ini.c_str())!=0 ||
        GetPrivateProfileIntW(L"Compatibility",L"SpoofArchToGame",-1,ini.c_str())!=0)
        return fail("SM86 SR configuration is missing or mismatched; reapply this profile");
    for (const auto* relative : {L"version.dll",L"dlssg_sm86/backend/sm86_backend.dll",
                                 L"dlssg_sm86/sr/nvngx_dlss.dll",L"dlssg_sm86/sr/sr_kernels.dll"})
        if (!std::filesystem::is_regular_file(folder/relative)) return fail("SM86 SR files are missing; reinstall the backend");
    // Another copy would make ours a silent standby with a different INI.
    HANDLE modules=CreateToolhelp32Snapshot(TH32CS_SNAPMODULE,GetCurrentProcessId());
    if (modules==INVALID_HANDLE_VALUE) return fail("Cannot check for an existing SM86 proxy");
    MODULEENTRY32W entry{}; entry.dwSize=sizeof(entry); bool conflict=false;
    if (Module32FirstW(modules,&entry)) do {
        if (GetProcAddress(entry.hModule,"DlssgProxy_Name")) { conflict=true;break; }
    } while (Module32NextW(modules,&entry));
    CloseHandle(modules);
    if (conflict) return fail("Another SM86 proxy is loaded; remove that copy before using Companion's SR backend");
    const auto log=folder/L"dlssg_sm86"/L"logs"/L"dlssg_sm86.log";
    std::error_code ec;
    const auto size=std::filesystem::file_size(log,ec);
    const auto start=ec ? 0 : size;
    HMODULE proxy=LoadLibraryExW((folder/L"version.dll").c_str(),nullptr,LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!proxy) return fail("SM86 SR proxy could not load (Windows error "+std::to_string(GetLastError())+")");
    HMODULE pinned=nullptr;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,
                      reinterpret_cast<LPCWSTR>(GetProcAddress(proxy,"DlssgProxy_Name")),&pinned);
    sm86["loaderReady"]=true;
    // The upstream proxy falls back to stock on some rejected devices. Trigger
    // its SR gate before loading the upscaler, and require a fresh verdict for
    // THIS PID. Loading a snippet does not create/evaluate a GPU feature.
    const HMODULE runtime=LoadLibraryExW((folder/L"dlssg_sm86"/L"sr"/L"nvngx_dlss.dll").c_str(),nullptr,LOAD_WITH_ALTERED_SEARCH_PATH);
    const auto deadline=GetTickCount64()+1500;
    while (GetTickCount64()<deadline) {
        std::ifstream stream(log,std::ios::binary);
        if (stream) {
            stream.seekg(static_cast<std::streamoff>(start));
            std::string text(262144,'\0');stream.read(text.data(),text.size());text.resize(static_cast<size_t>(stream.gcount()));
            size_t begin=0;
            while (begin<text.size()) {
                const size_t end=text.find('\n',begin);
                if (end==std::string::npos) break;
                const size_t brace=text.find('{',begin);
                if (brace<end) {
                    const auto event=json::parse(text.substr(brace,end-brace),nullptr,false);
                    if (event.is_object() && event.value("event",std::string())=="sr_gate" &&
                        event.value("pid",DWORD(0))==GetCurrentProcessId() && event.value("mode",std::string())==mode) {
                        if (!event.value("allowed",false))
                            return fail("SM86 SR unavailable: "+event.value("reason",std::string("device rejected"))+" ("+event.value("how",std::string())+")");
                        if (!runtime) return fail("SM86 SR runtime installation failed; see the backend log");
                        sm86["gateAllowed"]=true;
                        LOG_INFO("Companion","SM86 SR %s gate accepted and runtime loaded; frame inference remains to be validated",mode.c_str());
                        return {};
                    }
                }
                begin=end+1;
            }
        }
        Sleep(20);
    }
    return fail("SM86 SR did not confirm its device gate; see the backend log and reapply the profile");
}

double Seconds(FILETIME file) {
    ULARGE_INTEGER value{}; value.LowPart=file.dwLowDateTime; value.HighPart=file.dwHighDateTime;
    return double(value.QuadPart)/10000000.0-11644473600.0;
}
json Snapshot(eam::AddonManager* manager, const std::string& startupError) {
    FILETIME created{}, exited{}, kernel{}, user{}, now{};
    GetProcessTimes(GetCurrentProcess(), &created, &exited, &kernel, &user);
    GetSystemTimeAsFileTime(&now);
    json sr;{std::lock_guard<std::mutex> lock(sm86Mutex);sr=sm86;}
    const auto vendor=scalingVendor.load();
    json result={{"schema",1},{"pid",GetCurrentProcessId()},{"createdAt",Seconds(created)},
        {"sampledAt",Seconds(now)},{"headless",true},{"hostVersion",LSC_HOST_VERSION},
        {"startupError",startupError},{"addons",json::array()},{"metrics",json::object()},
        {"sm86Sr",sr},{"neuralBackend",vendor==0x1002?"AMD":vendor==0x10de?"NVIDIA":vendor?"unsupported":"waiting"}};
    {
        std::lock_guard<std::mutex> lock(manager->GetMutex());
        for (const auto& addon : manager->GetAddons()) {
            const auto status=eam::Metrics::Instance().GetStatus(addon.id);
            result["addons"].push_back({{"id",addon.id},{"name",addon.GetDisplayName()},
                {"enabled",addon.enabled},{"loaded",addon.IsLoaded()},{"faulted",addon.faulted},
                {"error",addon.errorMessage},{"status",status.text},{"level",status.level}});
        }
    }
    // Snapshot only recent samples; no telemetry collection is added to the render path.
    for (const auto& series : eam::Metrics::Instance().Snapshot(2.0))
        if (!series.samples.empty()) result["metrics"][series.addon][series.key]=series.last();
    const auto d=RealFrameHook::GetDiagnostics();
    const char* support=EamSupportText(RealFrameHook::Support());
    result["capture"]={{"support",support ? support : "Ready"},{"runtimeVersion",d.runtimeVersion},
        {"runtimeVerified",d.runtimeVerified},{"adapter",d.adapter},{"hookError",d.hookError},
        {"error",d.captureError},{"width",d.width},{"height",d.height},{"captures",d.captures},
        {"lastCaptureTick",d.lastCapture}};
    return result;
}

DWORD WINAPI Run(void* parameter) {
    auto* manager=static_cast<eam::AddonManager*>(parameter);
    std::string startupError;
    try {
        {
            std::lock_guard<std::mutex> lock(sm86Mutex);
            auto& config=eam::ConfigManager::Instance();
            deferSm86=config.IsAddonEnabled("LSC-AMDNR",false);
            automaticNr.store(deferSm86);
            if(deferSm86&&config.IsAddonEnabled("DLSS4DLAA",false))sm86["mode"]=config.Get("DLSS4DLAA","companionSm86Mode","Off");
            else if(deferSm86)sm86["mode"]="Off";
            else {startupError=StartSm86();triedSm86=true;}
        }
        if (!startupError.empty()) {
            // ScanAddons ran before this worker and cached the enable flag.
            // Nothing is loaded yet: update that guarded record as well as the
            // config so LoadAddons cannot start a stock fallback.
            std::lock_guard<std::mutex> lock(manager->GetMutex());
            for (auto& addon : manager->GetAddons()) if (addon.id=="DLSS4DLAA") {
                addon.enabled=false;addon.errorMessage=startupError;
            }
        }
        D3D11Hook::InstallDispatchHooks();
        // Keep the SDK's shared ImGui context for addon initialization. No HWND,
        // swapchain, icons, fonts, UI render loop, or GPU-stat sampler is created.
        ImGui::CreateContext();
        manager->LoadAddons();
        manager->InitializeAddons(ImGui::GetCurrentContext());
        LOG_INFO("Companion", "Headless host ready (%s)", LSC_HOST_VERSION);
    } catch (const std::exception& error) {
        startupError=error.what();
        LOG_ERROR("Companion", "Headless startup: %s", error.what());
    }
    const std::wstring name=L"\\\\.\\pipe\\LSCompanionAddonStatus-"+std::to_wstring(GetCurrentProcessId());
    // A fresh snapshot is sent only to a reader. Overlapped writes have a bounded
    // wait, so a stalled dashboard cannot stop subsequent readers or the renderer.
    for (;;) {
        HANDLE pipe=CreateNamedPipeW(name.c_str(),PIPE_ACCESS_DUPLEX|FILE_FLAG_OVERLAPPED|FILE_FLAG_FIRST_PIPE_INSTANCE,
            PIPE_TYPE_MESSAGE|PIPE_READMODE_MESSAGE|PIPE_WAIT|PIPE_REJECT_REMOTE_CLIENTS,1,262144,1,0,nullptr);
        if (pipe==INVALID_HANDLE_VALUE) { LOG_ERROR("Companion","Status pipe unavailable (%lu)",GetLastError()); return 1; }
        OVERLAPPED connection{};connection.hEvent=CreateEventW(nullptr,TRUE,FALSE,nullptr);
        if (!connection.hEvent) { CloseHandle(pipe);return 1; }
        const BOOL connected=ConnectNamedPipe(pipe,&connection);
        const DWORD error=connected ? ERROR_SUCCESS : GetLastError();
        bool ready=connected || error==ERROR_PIPE_CONNECTED;
        if (error==ERROR_IO_PENDING) {
            DWORD transferred=0;
            ready=WaitForSingleObject(connection.hEvent,INFINITE)==WAIT_OBJECT_0 &&
                GetOverlappedResult(pipe,&connection,&transferred,FALSE);
        }
        if (ready) {
            try {
                const auto payload=Snapshot(manager,startupError).dump();
                if (payload.size()<=262144) {
                    ResetEvent(connection.hEvent);
                    OVERLAPPED write{};write.hEvent=connection.hEvent;DWORD transferred=0;
                    if (!WriteFile(pipe,payload.data(),DWORD(payload.size()),&transferred,&write) && GetLastError()==ERROR_IO_PENDING) {
                        if (WaitForSingleObject(write.hEvent,500)!=WAIT_OBJECT_0) CancelIoEx(pipe,&write);
                        GetOverlappedResult(pipe,&write,&transferred,TRUE);
                    }
                    // DisconnectNamedPipe discards unread buffered data. Let the
                    // client acknowledge delivery, with a bounded wait and no commands.
                    ResetEvent(connection.hEvent);
                    OVERLAPPED receipt{};receipt.hEvent=connection.hEvent;char ack=0;
                    if (!ReadFile(pipe,&ack,1,&transferred,&receipt) && GetLastError()==ERROR_IO_PENDING) {
                        if (WaitForSingleObject(receipt.hEvent,500)!=WAIT_OBJECT_0) CancelIoEx(pipe,&receipt);
                        GetOverlappedResult(pipe,&receipt,&transferred,TRUE);
                    }
                }
            } catch (const std::exception& error) { LOG_WARN("Companion","Status snapshot: %s",error.what()); }
        }
        DisconnectNamedPipe(pipe);CloseHandle(connection.hEvent);CloseHandle(pipe);
    }
}
}
bool IsHeadless() { return headless.load(); }
void ObserveRealFrameAdapter(unsigned vendor) {
    scalingVendor.store(vendor);
    nvidiaSrModule.store(GetModuleHandleW(L"DLSS4DLAA.dll"));
    nvidiaNrModule.store(GetModuleHandleW(L"DLSSNR02.dll"));
}
void PrepareSm86ForAdapter(unsigned vendor) {
    scalingVendor.store(vendor);
    unsigned empty=0;nrDriverVendor.compare_exchange_strong(empty,vendor);
    std::lock_guard<std::mutex> lock(sm86Mutex);
    if(!deferSm86||triedSm86)return;
    triedSm86=true;
    if(vendor!=0x10de) {
        if(sm86.value("mode",std::string("Off"))!="Off") {
            blockSm86.store(true);sm86["error"]="SM86 SR requires an NVIDIA scaling adapter";
        }
        return;
    }
    if(!StartSm86().empty())blockSm86.store(true);
}
bool AllowDispatchModule(void* module) {
    const bool amd=automaticNr.load()&&(scalingVendor.load()!=0x10de||nrDriverVendor.load()==0x1002);
    if(module&&module==nvidiaSrModule.load()&&(amd||blockSm86.load()))return false;
    if(module&&module==nvidiaNrModule.load()&&amd)return false;
    return true;
}
bool StartHeadless(eam::AddonManager* manager) {
    if (!eam::ConfigManager::Instance().GlobalGetOr<bool>("companion","headless",false)) return false;
    // Lifetime is the LS process. Explicit unload is intentionally disabled; host
    // upgrades and settings changes go through Companion's stopped reload path.
    HMODULE module=nullptr;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,
        reinterpret_cast<LPCWSTR>(&StartHeadless),&module)) return false;
    HANDLE worker=CreateThread(nullptr,0,Run,manager,0,nullptr);
    if (!worker) { LOG_ERROR("Companion","Could not start headless worker");return false; }
    headless.store(true);CloseHandle(worker);return true;
}
}
