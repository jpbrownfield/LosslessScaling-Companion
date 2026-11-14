// SPDX-License-Identifier: MIT
// Direct test of the packaged, unchanged NVIDIA addon. No fake NR backend.
// Synthetic D3D11 frames test its ABI/pipeline, not Lossless Scaling's capture hook.
#include <eam/addon_sdk.h>
#include <imgui.h>
#include <nlohmann/json.hpp>
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <wrl/client.h>
#include <atomic>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <future>
#include <iostream>
#include <map>
#include <mutex>
#include <stdexcept>
#include <vector>
using Microsoft::WRL::ComPtr;
using Json = nlohmann::json;
static void Check(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }
static void Hr(HRESULT hr, const char* message) { Check(SUCCEEDED(hr), message); }
static std::string Utf8(const wchar_t* value) {
    int n=WideCharToMultiByte(CP_UTF8,0,value,-1,nullptr,0,nullptr,nullptr);
    std::string text(n,0);WideCharToMultiByte(CP_UTF8,0,value,-1,text.data(),n,nullptr,nullptr);
    if (!text.empty()) text.pop_back();return text;
}
struct Host : IHost {
    std::mutex mutex;
    std::string status;
    int level=0;
    unsigned reports=0;
    std::map<std::string,std::string> config;
    std::map<std::string,double> metrics;
    std::vector<std::string> logs;
    EamRealFrameCallback callback=nullptr;
    void* user=nullptr;
    std::atomic<bool> required{false};
    void Log(EamLogLevel,const char* text) override { std::lock_guard<std::mutex> lock(mutex);logs.emplace_back(text); }
    const char* GetConfig(const char*,const char* key,const char* fallback) override {
        auto it=config.find(key);return it==config.end()?fallback:it->second.c_str();
    }
    void SetConfig(const char*,const char* key,const char* value) override { config[key]=value; }
    void SaveConfig() override {}
    uint32_t GetHostVersion() override { return 0x010400; }
    void SubscribeEvent(uint32_t,EamEventCallback,void*) override {}
    void UnsubscribeEvent(uint32_t,EamEventCallback) override {}
    void PublishEvent(uint32_t,const void*,uint32_t) override {}
    void* GetD3D11Device() override { return nullptr; }
    void* GetD3D11DeviceContext() override { return nullptr; }
    void SetPreDispatchCallback(EamPreDispatchCallback,void*) override {}
    void SetPostDispatchCallback(EamPostDispatchCallback,void*) override {}
    void* GetCurrentComputeShader() override { return nullptr; }
    uint32_t GetDispatchCount() override { return 0; }
    void SetStatus(const char*,const char* text,int severity) override {
        std::lock_guard<std::mutex> lock(mutex);status=text;level=severity;++reports;
    }
    void PublishMetric(const char*,const char* key,double value,const char*) override {
        std::lock_guard<std::mutex> lock(mutex);metrics[key]=value;
    }
    void* GetDispatchingContext() override { return nullptr; }
    void* CreateImage(const void*,uint32_t,uint32_t,uint32_t) override { return nullptr; }
    void ReleaseImage(void*) override {}
    bool SetRealFrameCallback(EamRealFrameCallback cb,void* value) override { callback=cb;user=value;return true; }
    EamFrameSupport GetRealFrameSupport() override { return EamFrameSupport::Ready; }
    void RequireRealFrameProcessing(bool value) override { required=value; }
    Json Snapshot() {
        std::lock_guard<std::mutex> lock(mutex);
        return {{"status",status},{"level",level},{"reports",reports},{"metrics",metrics},{"logs",logs}};
    }
};
template<class F> static bool Until(F predicate,unsigned milliseconds=10000) {
    const auto deadline=GetTickCount64()+milliseconds;
    while (!predicate() && GetTickCount64()<deadline) Sleep(10);
    return predicate();
}
static std::vector<unsigned char> Pixels(ID3D11Device* device,ID3D11DeviceContext* context,ID3D11Texture2D* texture) {
    D3D11_TEXTURE2D_DESC d{};texture->GetDesc(&d);
    const unsigned bpp=d.Format==DXGI_FORMAT_R16G16B16A16_FLOAT?8:4;
    d.Usage=D3D11_USAGE_STAGING;d.BindFlags=d.MiscFlags=0;d.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
    ComPtr<ID3D11Texture2D> staging;Hr(device->CreateTexture2D(&d,nullptr,&staging),"Create readback");
    context->CopyResource(staging.Get(),texture);D3D11_MAPPED_SUBRESOURCE mapped{};
    Hr(context->Map(staging.Get(),0,D3D11_MAP_READ,0,&mapped),"Readback frame");
    std::vector<unsigned char> result(size_t(d.Width)*d.Height*bpp);
    for(unsigned y=0;y<d.Height;++y)memcpy(result.data()+size_t(y)*d.Width*bpp,
        static_cast<unsigned char*>(mapped.pData)+size_t(y)*mapped.RowPitch,size_t(d.Width)*bpp);
    context->Unmap(staging.Get(),0);return result;
}
int wmain(int argc,wchar_t** argv) {
    Json report={{"passed",false},{"inferenceVerified",false},{"captureHookVerified",false}};
    std::filesystem::path result;
    try {
        std::filesystem::path addon,model;
        std::wstring mode=L"unsupported";int index=0;bool list=false;
        for(int i=1;i<argc;++i) {
            std::wstring arg=argv[i];
            if(arg==L"--list")list=true;
            else if(i+1<argc && arg==L"--addon")addon=argv[++i];
            else if(i+1<argc && arg==L"--model")model=argv[++i];
            else if(i+1<argc && arg==L"--mode")mode=argv[++i];
            else if(i+1<argc && arg==L"--adapter")index=std::stoi(argv[++i]);
            else if(i+1<argc && arg==L"--result")result=argv[++i];
            else throw std::runtime_error("Unknown/missing probe argument");
        }
        ComPtr<IDXGIFactory1> factory;Hr(CreateDXGIFactory1(IID_PPV_ARGS(&factory)),"Create DXGI factory");
        Json adapters=Json::array();
        for(UINT i=0;;++i) {
            ComPtr<IDXGIAdapter1> a;HRESULT hr=factory->EnumAdapters1(i,&a);if(hr==DXGI_ERROR_NOT_FOUND)break;
            Hr(hr,"Enumerate adapter");DXGI_ADAPTER_DESC1 d{};Hr(a->GetDesc1(&d),"Describe adapter");
            adapters.push_back({{"index",i},{"name",Utf8(d.Description)},{"vendorId",d.VendorId},
                {"deviceId",d.DeviceId},{"software",bool(d.Flags&DXGI_ADAPTER_FLAG_SOFTWARE)}});
        }
        report["adapters"]=adapters;
        if(list) { report["passed"]=true;std::cout<<report.dump()<<std::endl;return 0; }
        Check(mode==L"invalid"||mode==L"unsupported"||mode==L"inference","Unknown probe mode");
        const bool inference=mode==L"inference";
        report["mode"]=Utf8(mode.c_str());
        Check(std::filesystem::is_regular_file(addon),"Packaged addon DLL missing");
        if(inference) {
            Check(std::filesystem::is_regular_file(model),"User-provided NR model missing");
            Check(index>=0 && size_t(index)<adapters.size(),"Invalid DXGI adapter index");
            Check(adapters[index]["vendorId"]==0x10de && !adapters[index]["software"].get<bool>(),"Hardware inference requires an NVIDIA adapter");
        }
        ComPtr<ID3D11Device> device;ComPtr<ID3D11DeviceContext> context;
        if(inference) {
            ComPtr<IDXGIAdapter1> a;Hr(factory->EnumAdapters1(index,&a),"Select NVIDIA adapter");
            Hr(D3D11CreateDevice(a.Get(),D3D_DRIVER_TYPE_UNKNOWN,nullptr,0,nullptr,0,D3D11_SDK_VERSION,
                &device,nullptr,&context),"Create NVIDIA D3D11 device");report["adapter"]=adapters[index];
        } else {
            Hr(D3D11CreateDevice(nullptr,D3D_DRIVER_TYPE_WARP,nullptr,0,nullptr,0,D3D11_SDK_VERSION,
                &device,nullptr,&context),"Create negative-test WARP device");report["softwareNegativeTest"]=true;
        }
        HMODULE dll=LoadLibraryExW(addon.c_str(),nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_SYSTEM32);
        Check(dll!=nullptr,"Load real addon DLL");
        auto initialize=reinterpret_cast<AddonInit_t>(GetProcAddress(dll,"AddonInitialize"));
        auto shutdown=reinterpret_cast<AddonShutdown_t>(GetProcAddress(dll,"AddonShutdown"));
        Check(initialize&&shutdown,"Missing addon exports");
        auto* imgui=ImGui::CreateContext();ImGuiMemAllocFunc alloc;ImGuiMemFreeFunc freeFn;void* user;
        ImGui::GetAllocatorFunctions(&alloc,&freeFn,&user);
        report["cycles"]=Json::array();
        for(unsigned cycle=0;cycle<(inference?1u:3u);++cycle) {
            Host host;host.config["modelPath"]=Utf8(model.c_str());host.config["scalePercent"]="100";
            host.config["enabled"]="1";host.config["apply"]="1";
            initialize(&host,imgui,reinterpret_cast<void*>(alloc),reinterpret_cast<void*>(freeFn),user);
            Check(host.callback && host.required,"Addon failed to register its required real-frame callback");
            auto callback=host.callback;auto callbackUser=host.user;
            const unsigned frames=inference?8:1;unsigned accepted=0,edited=0;
            for(unsigned f=0;f<frames;++f) {
                // Resize, HDR and epoch changes exercise retirement/reinitialization.
                const bool hdr=inference && f>=4;const unsigned w=f>=4?704:640,h=360,bpp=hdr?8:4;
                D3D11_TEXTURE2D_DESC d{};d.Width=w;d.Height=h;d.MipLevels=d.ArraySize=1;d.SampleDesc.Count=1;
                d.Format=hdr?DXGI_FORMAT_R16G16B16A16_FLOAT:DXGI_FORMAT_R8G8B8A8_UNORM;
                d.BindFlags=D3D11_BIND_SHADER_RESOURCE|D3D11_BIND_UNORDERED_ACCESS;
                std::vector<uint32_t> data(size_t(w)*h*(bpp/4));
                for(unsigned p=0;p<data.size();++p)data[p]=hdr?0x38003c00u+(p+f)%128:0xff000000u|((p*7919+f*1237)&0xffffff);
                D3D11_SUBRESOURCE_DATA initial{data.data(),w*bpp,0};
                ComPtr<ID3D11Texture2D> original,output;
                Hr(device->CreateTexture2D(&d,&initial,&original),"Create capture texture");
                Hr(device->CreateTexture2D(&d,&initial,&output),"Create output texture");
                const auto before=Pixels(device.Get(),context.Get(),original.Get());
                ComPtr<ID3D11ShaderResourceView> sentinelSrv;
                ComPtr<ID3D11UnorderedAccessView> sentinelUav;
                Hr(device->CreateShaderResourceView(original.Get(),nullptr,&sentinelSrv),"Create state sentinel SRV");
                Hr(device->CreateUnorderedAccessView(output.Get(),nullptr,&sentinelUav),"Create state sentinel UAV");
                auto* srv=sentinelSrv.Get();auto* uav=sentinelUav.Get();
                context->CSSetShaderResources(3,1,&srv);context->CSSetUnorderedAccessViews(2,1,&uav,nullptr);
                EamRealFrame frame;frame.context=context.Get();frame.original=original.Get();frame.output=output.Get();
                frame.width=w;frame.height=h;frame.sequence=f+1;frame.epoch=f>=4?2:1;
                frame.encoding=hdr?EamFrameEncoding::ScRgb:EamFrameEncoding::Sdr709;
                if(mode==L"invalid")frame=EamRealFrame{};
                auto pending=std::async(std::launch::async,[&]{return callback(frame,callbackUser);});
                if(!inference) {
                    const std::string expected=mode==L"invalid"?"Invalid real capture":"NR requires the NVIDIA capture adapter";
                    const bool errorPublished=Until([&]{auto s=host.Snapshot();return s["level"]==3 && s["status"].get<std::string>().find(expected)!=std::string::npos;});
                    const bool captureWaiting=pending.wait_for(std::chrono::milliseconds(50))==std::future_status::timeout;
                    report["cycles"].push_back(host.Snapshot());
                    shutdown();
                    Check(pending.wait_for(std::chrono::seconds(10))==std::future_status::ready,"Shutdown failed to release pending capture");
                    Check(!pending.get() && !host.callback && !host.required,"Shutdown did not release callback/required state");
                    Check(errorPublished,"Expected actionable error was not published");
                    Check(captureWaiting,"Required NR error silently passed an unprocessed frame");
                    auto stopped=host.Snapshot()["reports"];Sleep(600);
                    Check(host.Snapshot()["reports"]==stopped,"Status reporter survived shutdown");
                } else {
                    // Python owns the process timeout, including hung driver/shutdown paths.
                    std::string lastStatus;
                    const auto deadline=GetTickCount64()+90000;
                    while(pending.wait_for(std::chrono::milliseconds(100))!=std::future_status::ready) {
                        auto snapshot=host.Snapshot();const auto status=snapshot["status"].get<std::string>();
                        if(status!=lastStatus) { std::cout<<"NR_PROGRESS "<<snapshot.dump()<<std::endl;lastStatus=status; }
                        if(snapshot["level"]==3 || GetTickCount64()>=deadline) {
                            report["cycles"].push_back(snapshot);
                            report["error"]=snapshot["level"]==3?status:"NR inference callback timed out";
                            if(!result.empty()) { std::ofstream file(result);file<<report.dump(2)<<'\n'; }
                            shutdown();
                            Check(false,"NR did not complete inference; see the native result/status log");
                        }
                    }
                    Check(pending.get(),"NR rejected a real texture");++accepted;
                }
                ComPtr<ID3D11ShaderResourceView> restoredSrv;ComPtr<ID3D11UnorderedAccessView> restoredUav;
                context->CSGetShaderResources(3,1,&restoredSrv);context->CSGetUnorderedAccessViews(2,1,&restoredUav);
                Check(restoredSrv==sentinelSrv && restoredUav==sentinelUav,"Addon did not restore the caller's D3D11 resource bindings");
                srv=nullptr;uav=nullptr;context->CSSetShaderResources(3,1,&srv);context->CSSetUnorderedAccessViews(2,1,&uav,nullptr);
                Check(Pixels(device.Get(),context.Get(),original.Get())==before,"Addon changed the read-only original capture");
                const auto after=Pixels(device.Get(),context.Get(),output.Get());
                if(!inference)Check(after==before,"Failed NR changed the original output");
                else if(after!=before)++edited;
            }
            if(inference) {
                Check(Until([&]{return host.Snapshot()["metrics"].value("accepted",0.0)>=4;}),"No completed model frames reported for the resized HDR epoch");
                auto evidence=host.Snapshot();evidence["callbacksAccepted"]=accepted;evidence["framesEdited"]=edited;
                report["cycles"].push_back(evidence);
                Check(accepted==8 && edited>0,"No observable neural output across the eight accepted frames");
                shutdown();Check(!host.callback && !host.required,"Inference shutdown left registration active");
                report["inferenceVerified"]=true;
            }
        }
        if(!inference)Check(!GetModuleHandleW(L"nvcuda.dll")&&!GetModuleHandleW(L"NeuralRendering_nvngx.dll"),"Negative test loaded the NVIDIA driver/model stack");
        FreeLibrary(dll);ImGui::DestroyContext(imgui);report["passed"]=true;
    } catch(const std::exception& error) { report["error"]=error.what(); }
    if(!result.empty()) { std::ofstream file(result);file<<report.dump(2)<<'\n'; }
    std::cout<<report.dump()<<std::endl;return report["passed"].get<bool>()?0:1;
}
