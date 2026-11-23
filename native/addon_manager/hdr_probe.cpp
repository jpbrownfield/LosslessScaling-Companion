// SPDX-License-Identifier: MIT
#include "hdr_probe.h"
#include <cstdio>
#include <d3d11.h>
#include <d3dcompiler.h>
#include <dxgi1_4.h>
#include <MinHook.h>
#include <wrl/client.h>
#include <atomic>
#include <cmath>
#include <map>
#include <mutex>
#include <cstring>
#include <stdexcept>
using Microsoft::WRL::ComPtr;
namespace lsc { namespace {
std::mutex gate;
HdrProbeStatus status;
HdrProbeControl* control=nullptr;
HANDLE mapping=nullptr;
std::map<IDXGISwapChain*, DXGI_COLOR_SPACE_TYPE> colors;
uint64_t nextSample=0;
IDXGISwapChain* sampledChain=nullptr;
ComPtr<ID3D11Device> device;
ComPtr<ID3D11ComputeShader> shader;
ComPtr<ID3D11Buffer> stats, staging, constants;
ComPtr<ID3D11UnorderedAccessView> statsView;
ComPtr<ID3D11Texture2D> copy;
ComPtr<ID3D11ShaderResourceView> image;
bool pending=false;
std::string pendingDeviceName;
float pendingWhite=0;
thread_local bool scanning=false;
std::mutex pacingGate;
PacingProbe pacing;
PacingWindows pacingWindows;
IDXGISwapChain* pacingChain=nullptr;
double pacingLast=0,pacingMetadataLast=0;
std::atomic<bool> pacingEnabled{true};
std::atomic<uint64_t> observedPresents{0},successfulPresents{0},eligiblePresents{0},pacingDrops{0};
uint64_t bindingContext=0, pacingGeneration=1, sourceWindow=0, outputWindow=0, deviceToken=0;
DWORD sourcePid=0;
uint64_t focusEpoch=0;
HMONITOR outputMonitor=nullptr;
DWORD refreshRate=0, monitorWidth=0, monitorHeight=0;
RECT sourceRect{}, outputRect{};
std::atomic<uint64_t> resizeEpoch{0};uint64_t observedResizeEpoch=0;
std::array<IDXGISwapChain*,8> rejectedChains{};size_t rejectedNext=0;
bool focused=false, identityVerified=false;
std::string presentationLuid;
thread_local bool insidePresent=false;

double ClockMs() {
    static const double scale=[] { LARGE_INTEGER frequency{};QueryPerformanceFrequency(&frequency);return 1000.0/frequency.QuadPart; }();
    LARGE_INTEGER tick{};QueryPerformanceCounter(&tick);return tick.QuadPart*scale;
}
void ResetPacing() {
    pacing=PacingProbe{};pacingWindows.Reset();pacingChain=nullptr;pacingLast=0;pacingMetadataLast=0;
    deviceToken=0;presentationLuid.clear();rejectedChains.fill(nullptr);++pacingGeneration;
}
void RecordPresentation(IDXGISwapChain* chain,double entered,double returned,HRESULT result) {
    ++observedPresents;
    if(result!=S_OK)return;
    ++successfulPresents;
    std::unique_lock lock(pacingGate,std::try_to_lock);
    if(!lock.owns_lock()) {++pacingDrops;return;}
    if(observedResizeEpoch!=resizeEpoch.load()) {observedResizeEpoch=resizeEpoch.load();ResetPacing();}
    // Metadata discovery touches COM/USER32 on selection and at most once per
    // two seconds, before completing a window, to catch device/pointer reuse.
    // No stealing a bound output after inactivity or accepting a helper chain.
    if(pacingChain!=chain || entered-pacingMetadataLast>=2000) {
        for(auto* rejected:rejectedChains)if(rejected==chain)return;
        DXGI_SWAP_CHAIN_DESC desc{};
        if(FAILED(chain->GetDesc(&desc))||!desc.OutputWindow||!IsWindowVisible(desc.OutputWindow)||IsIconic(desc.OutputWindow)||desc.BufferDesc.Width<160||desc.BufferDesc.Height<90)return;
        wchar_t title[128]{},klass[128]{};
        GetWindowTextW(desc.OutputWindow,title,128);GetClassNameW(desc.OutputWindow,klass,128);
        if(wcscmp(title,L"LosslessScaling")||wcsstr(klass,L"HwndWrapper")||wcsstr(klass,L"ImGui")) {rejectedChains[rejectedNext++%rejectedChains.size()]=chain;return;}
        if(outputWindow && reinterpret_cast<uint64_t>(desc.OutputWindow)!=outputWindow)return;
        ComPtr<IDXGIDevice> dxgiDevice;ComPtr<IDXGIAdapter> adapter;
        if(FAILED(chain->GetDevice(IID_PPV_ARGS(&dxgiDevice)))||FAILED(dxgiDevice->GetAdapter(&adapter)))return;
        DXGI_ADAPTER_DESC ad{};if(FAILED(adapter->GetDesc(&ad)))return;
        char luid[32]{};std::snprintf(luid,sizeof(luid),"%08x:%08x",unsigned(ad.AdapterLuid.HighPart),unsigned(ad.AdapterLuid.LowPart));
        const auto token=reinterpret_cast<uint64_t>(dxgiDevice.Get());
        if(pacingChain && (pacingChain!=chain || deviceToken!=token || presentationLuid!=luid))ResetPacing();
        pacingMetadataLast=entered;
        presentationLuid=luid;deviceToken=reinterpret_cast<uint64_t>(dxgiDevice.Get());
        pacingChain=chain;
    }
    ++eligiblePresents;
    if(pacingLast && entered-pacingLast>2000) {ResetPacing();return;}
    if(sourcePid)pacingWindows.Observe(entered,returned,pacingDrops.load());
    else pacing.Observe(entered,returned);
    pacingLast=entered;
}
const char* source=R"(
Texture2D<float4> frame : register(t0);
RWStructuredBuffer<uint> result : register(u0);
cbuffer Params : register(b0) { uint width, height; float white; uint unused; };
[numthreads(16,16,1)] void main(uint3 id:SV_DispatchThreadID) {
 if(id.x>=256 || id.y>=144)return;
 uint2 p=min(uint2((id.x+0.5)*width/256.0,(id.y+0.5)*height/144.0),uint2(width-1,height-1));
 float3 c=frame.Load(int3(p,0)).rgb;
 if(any(isnan(c))||any(isinf(c)))return;
 float y=max(0.0,dot(c,float3(0.2126,0.7152,0.0722))*80.0);
 InterlockedAdd(result[0],1);
 if(y>white*1.10)InterlockedAdd(result[1],1);
 if(min(c.r,min(c.g,c.b)) < -0.01*white/80.0 && y>1.0)InterlockedAdd(result[2],1);
 InterlockedMax(result[3],asuint(y));
})";
void Reset() {
    device.Reset();shader.Reset();stats.Reset();staging.Reset();constants.Reset();
    statsView.Reset();copy.Reset();image.Reset();pending=false;sampledChain=nullptr;
}
void Check(HRESULT hr,const char* message) { if(FAILED(hr))throw std::runtime_error(message); }
void Initialize(ID3D11Device* dev) {
    Reset();device=dev;
    ComPtr<ID3DBlob> code,error;
    Check(D3DCompile(source,strlen(source),nullptr,nullptr,nullptr,"main","cs_5_0",D3DCOMPILE_OPTIMIZATION_LEVEL3,0,&code,&error),"HDR statistics shader compilation failed");
    Check(dev->CreateComputeShader(code->GetBufferPointer(),code->GetBufferSize(),nullptr,&shader),"HDR shader creation failed");
    D3D11_BUFFER_DESC b{};b.ByteWidth=16;b.Usage=D3D11_USAGE_DEFAULT;b.BindFlags=D3D11_BIND_UNORDERED_ACCESS;
    b.MiscFlags=D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;b.StructureByteStride=4;
    Check(dev->CreateBuffer(&b,nullptr,&stats),"HDR counter allocation failed");
    Check(dev->CreateUnorderedAccessView(stats.Get(),nullptr,&statsView),"HDR counter view failed");
    b.Usage=D3D11_USAGE_STAGING;b.BindFlags=0;b.MiscFlags=0;b.StructureByteStride=0;b.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
    Check(dev->CreateBuffer(&b,nullptr,&staging),"HDR readback allocation failed");
    b.Usage=D3D11_USAGE_DEFAULT;b.BindFlags=D3D11_BIND_CONSTANT_BUFFER;b.CPUAccessFlags=0;
    Check(dev->CreateBuffer(&b,nullptr,&constants),"HDR constants allocation failed");
}
using Present=HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain*,UINT,UINT);
using Present1=HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain1*,UINT,UINT,const DXGI_PRESENT_PARAMETERS*);
using Resize=HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain*,UINT,UINT,UINT,DXGI_FORMAT,UINT);
Resize originalResize=nullptr;
HRESULT STDMETHODCALLTYPE OnResize(IDXGISwapChain* chain,UINT count,UINT width,UINT height,DXGI_FORMAT format,UINT flags) {
    ++resizeEpoch;return originalResize(chain,count,width,height,format,flags);
}
using SetColor=HRESULT(STDMETHODCALLTYPE*)(IDXGISwapChain3*,DXGI_COLOR_SPACE_TYPE);
Present originalPresent=nullptr;Present1 originalPresent1=nullptr;SetColor originalColor=nullptr;
HRESULT STDMETHODCALLTYPE OnPresent(IDXGISwapChain* chain,UINT interval,UINT flags) {
    if(insidePresent||(flags&DXGI_PRESENT_TEST))return originalPresent(chain,interval,flags);
    insidePresent=true;const bool monitor=pacingEnabled.load(std::memory_order_relaxed);const double entered=monitor?ClockMs():0;
    if(!(flags&DXGI_PRESENT_TEST))ObserveHdrPresentation(chain);
    const auto result=originalPresent(chain,interval,flags);
    if(monitor)RecordPresentation(chain,entered,ClockMs(),result);insidePresent=false;return result;
}
HRESULT STDMETHODCALLTYPE OnPresent1(IDXGISwapChain1* chain,UINT interval,UINT flags,const DXGI_PRESENT_PARAMETERS* p) {
    if(insidePresent||(flags&DXGI_PRESENT_TEST))return originalPresent1(chain,interval,flags,p);
    insidePresent=true;const bool monitor=pacingEnabled.load(std::memory_order_relaxed);const double entered=monitor?ClockMs():0;
    if(!(flags&DXGI_PRESENT_TEST))ObserveHdrPresentation(chain);
    const auto result=originalPresent1(chain,interval,flags,p);
    if(monitor)RecordPresentation(chain,entered,ClockMs(),result);insidePresent=false;return result;
}
HRESULT STDMETHODCALLTYPE OnColor(IDXGISwapChain3* chain,DXGI_COLOR_SPACE_TYPE color) {
    const auto hr=originalColor(chain,color);
    if(SUCCEEDED(hr)) { ++resizeEpoch;std::lock_guard lock(gate);if(colors.size()>128)colors.clear();colors[chain]=color; }
    return hr;
}
}
void ObserveHdrPresentation(IDXGISwapChain* chain) {
    if(!control)return;
    const auto now=GetTickCount64();
    std::unique_lock lock(gate,std::try_to_lock);if(!lock.owns_lock())return;
    status.enabled=control->version==1 && control->enabled==1 && now>=control->heartbeat && now-control->heartbeat<4000;
    if(!status.enabled) { if(device)Reset();status.ready=false;return; }
    DXGI_SWAP_CHAIN_DESC desc{};if(FAILED(chain->GetDesc(&desc)))return;
    // Exclude dashboard, WPF, composition-only, minimized and tiny helper chains.
    if(!desc.OutputWindow || !IsWindowVisible(desc.OutputWindow) || IsIconic(desc.OutputWindow)
       || desc.BufferDesc.Width<160 || desc.BufferDesc.Height<90)return;
    wchar_t klass[128]{};GetClassNameW(desc.OutputWindow,klass,128);
    if(wcsstr(klass,L"HwndWrapper")||wcsstr(klass,L"ImGui"))return;
    if(now<nextSample)return;nextSample=now+500;
    status.presentTick=now;
    status.format=desc.BufferDesc.Format;
    const auto found=colors.find(chain);
    status.colorSpace=found!=colors.end()?found->second:
        (desc.BufferDesc.Format==DXGI_FORMAT_R16G16B16A16_FLOAT?DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709:DXGI_COLOR_SPACE_RGB_FULL_G22_NONE_P709);
    MONITORINFOEXW monitor{};monitor.cbSize=sizeof(monitor);
    if(GetMonitorInfoW(MonitorFromWindow(desc.OutputWindow,MONITOR_DEFAULTTONEAREST),&monitor)) {
        char name[128]{};WideCharToMultiByte(CP_UTF8,0,monitor.szDevice,-1,name,128,nullptr,nullptr);
        status.deviceName=name;
    }
    if(!control->sample) {if(device)Reset();status.ready=true;return;}
    if(status.colorSpace!=DXGI_COLOR_SPACE_RGB_FULL_G10_NONE_P709 || status.format!=DXGI_FORMAT_R16G16B16A16_FLOAT) {
        if(device)Reset();status.ready=true;return;
    }
    try {
        ComPtr<ID3D11Device> dev;Check(chain->GetDevice(IID_PPV_ARGS(&dev)),"Cannot inspect presentation device");
        if(dev.Get()!=device.Get())Initialize(dev.Get());
        ComPtr<ID3D11DeviceContext> ctx;dev->GetImmediateContext(&ctx);
        if(pending) {
            if(sampledChain!=chain) {Reset();return;}
            D3D11_MAPPED_SUBRESOURCE read{};
            if(SUCCEEDED(ctx->Map(staging.Get(),0,D3D11_MAP_READ,D3D11_MAP_FLAG_DO_NOT_WAIT,&read))) {
                const auto* data=static_cast<const uint32_t*>(read.pData);
                status.pixels=data[0];status.highlights=data[1];status.gamut=data[2];
                memcpy(&status.peakNits,&data[3],4);ctx->Unmap(staging.Get(),0);
                status.sampledTick=now;status.ready=true;pending=false;
                status.sampleDeviceName=pendingDeviceName;status.sampleWhiteNits=pendingWhite;
            } else return;
        }
        ComPtr<ID3D11Texture2D> back;Check(chain->GetBuffer(0,IID_PPV_ARGS(&back)),"Cannot inspect presentation frame");
        D3D11_TEXTURE2D_DESC texture{};back->GetDesc(&texture);
        D3D11_TEXTURE2D_DESC previous{};if(copy)copy->GetDesc(&previous);
        if(!copy||previous.Width!=texture.Width||previous.Height!=texture.Height) {
            copy.Reset();image.Reset();texture.Usage=D3D11_USAGE_DEFAULT;
            texture.BindFlags=D3D11_BIND_SHADER_RESOURCE;texture.CPUAccessFlags=0;texture.MiscFlags=0;
            Check(dev->CreateTexture2D(&texture,nullptr,&copy),"Cannot allocate HDR sample texture");
            Check(dev->CreateShaderResourceView(copy.Get(),nullptr,&image),"Cannot create HDR sample view");
        }
        // A GPU-only copy avoids requiring SRV access on LS's swapchain buffer.
        // The shader touches a 256x144 grid; only 16 bytes cross back to the CPU.
        ctx->CopyResource(copy.Get(),back.Get());
        float white=control->whiteNits;if(!std::isfinite(white)||white<80||white>10000)white=400;
        struct {uint32_t width,height;float white;uint32_t pad;} params{texture.Width,texture.Height,white,0};
        ctx->UpdateSubresource(constants.Get(),0,nullptr,&params,0,0);
        const UINT zeros[4]{};ctx->ClearUnorderedAccessViewUint(statsView.Get(),zeros);
        // Preserve every compute binding this pass modifies, including class instances.
        ComPtr<ID3D11ComputeShader> oldShader;ID3D11ClassInstance* classes[256]{};UINT classCount=256;
        ComPtr<ID3D11ShaderResourceView> oldImage;ComPtr<ID3D11UnorderedAccessView> oldStats;ComPtr<ID3D11Buffer> oldConstants;
        ctx->CSGetShader(&oldShader,classes,&classCount);ctx->CSGetShaderResources(0,1,&oldImage);
        ctx->CSGetUnorderedAccessViews(0,1,&oldStats);ctx->CSGetConstantBuffers(0,1,&oldConstants);
        ctx->CSSetShader(shader.Get(),nullptr,0);auto* srv=image.Get();auto* uav=statsView.Get();auto* cb=constants.Get();
        ctx->CSSetShaderResources(0,1,&srv);ctx->CSSetUnorderedAccessViews(0,1,&uav,nullptr);ctx->CSSetConstantBuffers(0,1,&cb);
        scanning=true;ctx->Dispatch(16,9,1);scanning=false;
        srv=oldImage.Get();uav=oldStats.Get();cb=oldConstants.Get();
        ctx->CSSetShaderResources(0,1,&srv);ctx->CSSetUnorderedAccessViews(0,1,&uav,nullptr);ctx->CSSetConstantBuffers(0,1,&cb);
        ctx->CSSetShader(oldShader.Get(),classes,classCount);for(UINT i=0;i<classCount;++i)if(classes[i])classes[i]->Release();
        ctx->CopyResource(staging.Get(),stats.Get());pending=true;sampledChain=chain;
        pendingDeviceName=status.deviceName;pendingWhite=white;++status.scans;status.error.clear();
    } catch(const std::exception& e) {status.ready=false;status.error=e.what();Reset();}
}
HdrProbeStatus HdrProbeSnapshot() {std::lock_guard lock(gate);return status;}
void EnablePresentationMonitoring(bool enabled) {
    std::lock_guard lock(pacingGate);if(pacingEnabled.exchange(enabled)!=enabled)ResetPacing();
}
void BindPresentation(DWORD pid,uint64_t source,uint64_t output,uint64_t context) {
    std::lock_guard lock(pacingGate);
    DWORD owner=0;GetWindowThreadProcessId(reinterpret_cast<HWND>(source),&owner);
    DWORD outputOwner=0;GetWindowThreadProcessId(reinterpret_cast<HWND>(output),&outputOwner);
    wchar_t title[128]{};GetWindowTextW(reinterpret_cast<HWND>(output),title,128);
    if(!pid||owner!=pid||outputOwner!=GetCurrentProcessId()||wcscmp(title,L"LosslessScaling")) {sourcePid=0;sourceWindow=outputWindow=bindingContext=0;identityVerified=false;ResetPacing();return;}
    if(sourcePid!=pid||sourceWindow!=source||outputWindow!=output||bindingContext!=context) {
        sourcePid=pid;sourceWindow=source;outputWindow=output;bindingContext=context;ResetPacing();
    }
}
PacingSnapshot PresentationPacingSnapshot(uint64_t observedFocusEpoch) {
    PacingWindows completed;
    PacingProbe recent;
    PacingSnapshot metadata;
    {
        std::lock_guard lock(pacingGate);
        const auto source=reinterpret_cast<HWND>(sourceWindow),output=reinterpret_cast<HWND>(outputWindow);
        DWORD owner=0,outputOwner=0;GetWindowThreadProcessId(source,&owner);GetWindowThreadProcessId(output,&outputOwner);
        const auto foreground=GetForegroundWindow();
        const bool hasFocus=foreground==source||foreground==output;
        const auto epoch=observedFocusEpoch;
        const auto monitor=MonitorFromWindow(output,MONITOR_DEFAULTTONULL);
        MONITORINFOEXW info{};info.cbSize=sizeof(info);DEVMODEW mode{};mode.dmSize=sizeof(mode);
        if(monitor&&GetMonitorInfoW(monitor,&info))EnumDisplaySettingsW(info.szDevice,ENUM_CURRENT_SETTINGS,&mode);
        const bool valid=sourcePid&&owner==sourcePid&&outputOwner==GetCurrentProcessId()
            &&IsWindowVisible(source)&&!IsIconic(source)&&IsWindowVisible(output)&&!IsIconic(output)&&hasFocus&&mode.dmDisplayFrequency;
        RECT sr{},orr{};GetClientRect(source,&sr);GetClientRect(output,&orr);
        if(memcmp(&sr,&sourceRect,sizeof(sr))||memcmp(&orr,&outputRect,sizeof(orr)))ResetPacing();
        sourceRect=sr;outputRect=orr;
        if(valid!=identityVerified||hasFocus!=focused||epoch!=focusEpoch||monitor!=outputMonitor
            ||refreshRate!=mode.dmDisplayFrequency||monitorWidth!=mode.dmPelsWidth||monitorHeight!=mode.dmPelsHeight)ResetPacing();
        identityVerified=valid;focused=hasFocus;focusEpoch=epoch;outputMonitor=monitor;
        refreshRate=mode.dmDisplayFrequency;monitorWidth=mode.dmPelsWidth;monitorHeight=mode.dmPelsHeight;
        completed=pacingWindows;if(!sourcePid)recent=pacing;
        metadata.enabled=pacingEnabled.load();metadata.identityVerified=identityVerified;metadata.focused=focused;
        metadata.generation=pacingGeneration;metadata.sourcePid=sourcePid;metadata.sourceWindow=sourceWindow;
        metadata.outputWindow=outputWindow;metadata.chain=reinterpret_cast<uint64_t>(pacingChain);metadata.device=deviceToken;
        metadata.monitor=reinterpret_cast<uint64_t>(outputMonitor);metadata.refreshHz=refreshRate;
        metadata.monitorWidth=monitorWidth;metadata.monitorHeight=monitorHeight;
        metadata.context=bindingContext;metadata.presentationLuid=presentationLuid;
        metadata.dropped=pacingDrops.load();metadata.ageMs=pacingLast?ClockMs()-pacingLast:1e9;
    }
    // Sorting is exclusively on the telemetry reader, after releasing the render mutex.
    auto result=completed.Snapshot();
    if(!sourcePid)result=metadata.ageMs>2000?PacingSnapshot{}:recent.Snapshot(); // legacy measurement only
    result.enabled=metadata.enabled;result.identityVerified=metadata.identityVerified;result.focused=metadata.focused;
    result.generation=metadata.generation;result.sourcePid=metadata.sourcePid;result.sourceWindow=metadata.sourceWindow;
    result.outputWindow=metadata.outputWindow;result.chain=metadata.chain;result.device=metadata.device;
    result.monitor=metadata.monitor;result.refreshHz=metadata.refreshHz;result.monitorWidth=metadata.monitorWidth;result.monitorHeight=metadata.monitorHeight;
    result.context=metadata.context;result.presentationLuid=metadata.presentationLuid;result.totalDropped=metadata.dropped;
    result.ageMs=result.endedMs?ClockMs()-result.endedMs:metadata.ageMs;
    result.observedPresents=observedPresents.load();result.successfulPresents=successfulPresents.load();
    result.eligiblePresents=eligiblePresents.load();return result;
}
bool IsHdrProbeDispatch() {return scanning;}
void StartHdrProbe() {
    mapping=CreateFileMappingW(INVALID_HANDLE_VALUE,nullptr,PAGE_READWRITE,0,sizeof(HdrProbeControl),
        (L"Local\\LSCHdrProbe-"+std::to_wstring(GetCurrentProcessId())).c_str());
    if(mapping)control=static_cast<HdrProbeControl*>(MapViewOfFile(mapping,FILE_MAP_ALL_ACCESS,0,0,sizeof(HdrProbeControl)));
    if(!control)return;*control={};
    // Discover shared DXGI entry points on an invisible fixture, outside DllMain.
    HWND window=CreateWindowExW(0,L"STATIC",L"",WS_POPUP,0,0,16,16,nullptr,nullptr,GetModuleHandleW(nullptr),nullptr);
    DXGI_SWAP_CHAIN_DESC d{};d.BufferDesc.Width=16;d.BufferDesc.Height=16;d.BufferDesc.Format=DXGI_FORMAT_R8G8B8A8_UNORM;
    d.SampleDesc.Count=1;d.BufferUsage=DXGI_USAGE_RENDER_TARGET_OUTPUT;d.BufferCount=2;d.OutputWindow=window;d.Windowed=TRUE;d.SwapEffect=DXGI_SWAP_EFFECT_DISCARD;
    ComPtr<ID3D11Device> dev;ComPtr<IDXGISwapChain> chain;
    auto hr=D3D11CreateDeviceAndSwapChain(nullptr,D3D_DRIVER_TYPE_HARDWARE,nullptr,0,nullptr,0,D3D11_SDK_VERSION,&d,&chain,&dev,nullptr,nullptr);
    if(SUCCEEDED(hr)) {
        MH_Initialize();auto** table=*reinterpret_cast<void***>(chain.Get());
        if(MH_CreateHook(table[13],reinterpret_cast<void*>(&OnResize),reinterpret_cast<void**>(&originalResize))==MH_OK)MH_EnableHook(table[13]);
        if(MH_CreateHook(table[8],reinterpret_cast<void*>(&OnPresent),reinterpret_cast<void**>(&originalPresent))==MH_OK)MH_EnableHook(table[8]);
        ComPtr<IDXGISwapChain1> one;if(SUCCEEDED(chain.As(&one))) {
            auto** t=*reinterpret_cast<void***>(one.Get());
            if(MH_CreateHook(t[22],reinterpret_cast<void*>(&OnPresent1),reinterpret_cast<void**>(&originalPresent1))==MH_OK)MH_EnableHook(t[22]);
        }
        ComPtr<IDXGISwapChain3> three;if(SUCCEEDED(chain.As(&three))) {
            auto** t=*reinterpret_cast<void***>(three.Get());
            if(MH_CreateHook(t[38],reinterpret_cast<void*>(&OnColor),reinterpret_cast<void**>(&originalColor))==MH_OK)MH_EnableHook(t[38]);
        }
        if(!originalPresent)status.error="DXGI presentation hooks unavailable";
    } else status.error="Cannot initialize DXGI HDR observer";
    chain.Reset();dev.Reset();if(window)DestroyWindow(window);
}
}
