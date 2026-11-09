// SPDX-License-Identifier: MIT
#include "pipeline.h"
#include "eam/addon_exports.h"
#include <algorithm>
#include <chrono>
#include <filesystem>
#include <cmath>

namespace {
IHost* host=nullptr;
amd_nr::Pipeline* pipeline=nullptr; // Pinned process lifetime, never destroyed in DllMain.
amd_nr::Settings settings;
bool failed=false;
std::string failure;
ULONGLONG lastStatus=0;
constexpr auto id="LSC-AMDNR";
std::wstring Wide(const std::string& s) {
    if(s.empty())return {};int n=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,s.data(),int(s.size()),nullptr,0);
    if(!n)throw std::runtime_error("Invalid UTF-8 runtime/model path");
    std::wstring w(n,0);MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,s.data(),int(s.size()),w.data(),n);return w;
}
float Number(const char* key,float fallback,float lo,float hi) {
    try {float f=std::stof(host->GetConfig("DLSSNR02",key,""));return std::isfinite(f)?std::clamp(f,lo,hi):fallback;}catch(...){return fallback;}
}
bool Flag(const char* key,bool fallback=true) {return std::string(host->GetConfig("DLSSNR02",key,fallback?"1":"0"))=="1";}
void ReadSettings() {
    auto& m=settings.model;m.struct_size=sizeof(m);m.enabled=Flag("enabled");
    m.resolution_scale=Number("scalePercent",100,25,100)/100;
    m.style=int(Number("style",0,0,2));m.intensity=Number("intensity",1,0,1);
    m.local_tone=Number("localTone",.3f,0,1);m.local_structure=Number("localStructure",.8f,0,1);
    m.skin_structure=Number("skinStructure",-1,-1,1);m.detail_strength=Number("detail",1,0,2);
    m.colour_strength=Number("colour",1,0,4);m.max_ratio=Number("maxRatio",2,1,8);
    m.max_age=int(Number("amdMaxAge",4,1,120));m.encoding=DLSS5NR_ENCODING_AUTO;
    m.exposure_mode=DLSS5NR_EXPOSURE_AUTO;m.exposure=1;m.composition=DLSS5NR_COMPOSITION_RATIO;
    m.follow_motion=1;m.network_history=Flag("amdHistory",false);m.wait_for_network=Flag("amdWait",false);
    m.wait_inside=m.wait_for_network;m.passes=int(Number("passes",1,1,8));m.keep_local_tone=0;
    m.blend_frames=int(Number("amdBlendFrames",0,0,8));m.network_preset=int(Number("preset",0,0,3));
}
bool Frame(const EamRealFrame& frame,void*) {
    if(failed) {
        if(GetTickCount64()-lastStatus>=500){host->SetStatus(id,failure.c_str(),3);lastStatus=GetTickCount64();}
        return false;
    }
    try {
        if(GetTickCount64()-lastStatus>=500)ReadSettings();
        if(!settings.model.enabled) {
            if(GetTickCount64()-lastStatus>=500){host->SetStatus(id,"AMD Neural Rendering is disabled",0);lastStatus=GetTickCount64();}
            return false;
        }
        if(!pipeline) {
            wchar_t exe[32768]{};if(!GetModuleFileNameW(nullptr,exe,DWORD(std::size(exe))))throw std::runtime_error("Cannot find LS directory");
            auto root=std::filesystem::path(exe).parent_path();
            settings.runtimeDir=(root/L"companion-runtimes/AMDNR/1.3.2").wstring();
            settings.library=(root/L"addons/LSC-AMDNR/runtime/dlss5nr_runtime.dll").wstring();
            settings.snippet=Wide(host->GetConfig("DLSSNR02","amdModelPath",""));
            if(settings.snippet.empty())settings.snippet=(root/L"nvngx_dlssnr.dll").wstring();
            else if(std::filesystem::path(settings.snippet).is_relative())settings.snippet=(root/settings.snippet).wstring();
            if(!std::filesystem::is_regular_file(settings.library))
                throw std::runtime_error("The bundled AMD adapter-selecting runtime is missing; reinstall Companion Addon Host");
            if(!std::filesystem::is_regular_file(settings.runtimeDir+L"/kernels/kernels.txt"))
                throw std::runtime_error("Install the AMD Neural Rendering runtime in Add-ons, then reapply and restart LS");
            if(!std::filesystem::is_regular_file(settings.snippet))throw std::runtime_error("Import the original signed NR 310.9.16 model or set its AMD DLL path, then restart LS");
            settings.dataDir=Wide(host->GetConfig(id,"dataPath",""));
            if(settings.dataDir.empty())throw std::runtime_error("AMD NR cache path missing; reapply the profile");
            pipeline=new amd_nr::Pipeline;
        }
        bool applied=pipeline->Process(frame,settings);
        const auto& state=pipeline->Status();
        if(!applied||state.state==DLSS5NR_STATE_FAILED) {
            std::string message(state.message,strnlen(state.message,sizeof(state.message)));
            throw std::runtime_error("AMD NR stopped: "+(message.empty()?"runtime rejected the capture; check the AMD NR log":message));
        }
        if(GetTickCount64()-lastStatus>=500) {
            const auto& s=pipeline->Status();
            std::string text=s.state==DLSS5NR_STATE_FAILED?"AMD NR failed: ":s.results?"AMD NR active: ":"AMD NR starting (no inference result yet): ";
            text.append(s.message,strnlen(s.message,sizeof(s.message)));
            host->SetStatus(id,text.c_str(),s.state==DLSS5NR_STATE_FAILED?3:s.results?1:2);lastStatus=GetTickCount64();
        }
        const auto& s=pipeline->Status();
        host->PublishMetric(id,"evaluation_ms",s.evaluation_ms,"ms");
        host->PublishMetric(id,"inference_fps",s.evaluations_per_second,"fps");
        host->PublishMetric(id,"result_age",s.result_age,"frames");
        host->PublishMetric(id,"result_latency",s.result_latency,"frames");
        host->PublishMetric(id,"results",s.results,"results");
        host->PublishMetric(id,"native_kernels",s.native_kernels,"kernels");
        if(pipeline->ComputeOrdinal()>=0)host->PublishMetric(id,"compute_device",pipeline->ComputeOrdinal(),"CUDA ordinal");
        host->PublishMetric(id,"flow_ms",pipeline->flowMs,"ms");
        host->PublishMetric(id,"cpu_wait_ms",pipeline->waitMs,"ms");
        return applied&&Flag("apply");
    } catch(const std::exception& e) {
        failed=true;failure=e.what();host->Log(EAM_LOG_ERROR,e.what());host->SetStatus(id,e.what(),3);return false;
    }
}
}
EAM_EXPORT uint32_t GetAddonCapabilities(){return EAM_CAP_REQUIRES_RESTART|EAM_CAP_D3D11_DEVICE_ACCESS;}
EAM_EXPORT const char* GetAddonName(){return "Companion AMD Neural Rendering";}
EAM_EXPORT const char* GetAddonVersion(){return "0.1.0";}
EAM_EXPORT void AddonInitialize(IHost* h,ImGuiContext*,void*,void*,void*) {
    host=h;if(!h||h->GetHostVersion()<0x010400)return;
    HMODULE self=nullptr;GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,
        reinterpret_cast<LPCWSTR>(&Frame),&self);
    ReadSettings();
    if(!h->SetRealFrameCallback(Frame,nullptr)){failed=true;h->SetStatus(id,"Real-frame stage owned by another addon",3);return;}
    h->SetStatus(id,"Waiting for an AMD real capture; backend follows the scaling adapter",0);
}
EAM_EXPORT void AddonShutdown(){if(host){host->RequireRealFrameProcessing(false);volatile bool removed=host->SetRealFrameCallback(nullptr,nullptr);(void)removed;}}
