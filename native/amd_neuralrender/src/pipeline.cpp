// SPDX-License-Identifier: MIT
#include "pipeline.h"
#include <chrono>
#include <filesystem>
#include <sstream>
#include <stdexcept>
#include <fstream>
#include <bcrypt.h>

namespace amd_nr {
void Check(HRESULT hr,const char* op) {
    if (FAILED(hr)) { std::ostringstream s; s<<op<<" failed (0x"<<std::hex<<unsigned(hr)<<")"; throw std::runtime_error(s.str()); }
}
void VerifyModel(const std::wstring& path) {
    struct Hash {
        BCRYPT_ALG_HANDLE algorithm=nullptr;BCRYPT_HASH_HANDLE hash=nullptr;
        ~Hash(){if(hash)BCryptDestroyHash(hash);if(algorithm)BCryptCloseAlgorithmProvider(algorithm,0);}
    } state;
    Check(BCryptOpenAlgorithmProvider(&state.algorithm,BCRYPT_SHA256_ALGORITHM,nullptr,0),"Open model hash");
    Check(BCryptCreateHash(state.algorithm,&state.hash,nullptr,0,nullptr,0,0),"Create model hash");
    std::ifstream input(std::filesystem::path(path),std::ios::binary);
    if(!input)throw std::runtime_error("Cannot read the AMD NR model DLL");
    std::array<char,65536> buffer{};
    while(input){input.read(buffer.data(),buffer.size());auto bytes=input.gcount();
        if(bytes)Check(BCryptHashData(state.hash,reinterpret_cast<PUCHAR>(buffer.data()),ULONG(bytes),0),"Hash model");}
    if(!input.eof())throw std::runtime_error("Failed to read the complete AMD NR model DLL");
    unsigned char digest[32]{};Check(BCryptFinishHash(state.hash,digest,sizeof(digest),0),"Finish model hash");
    const char hex[]="0123456789abcdef";std::string text;
    for(auto byte:digest){text+=hex[byte>>4];text+=hex[byte&15];}
    if(text!=LSC_AMD_MODEL_SHA256)
        throw std::runtime_error("AMD NR needs the original signed 310.9.16 model matching this runtime; patched/different models cannot use its cache");
}
void RuntimeApi::Load(const std::wstring& path,bool requireAdapterSelection) {
    // Never replace another CUDA stack in this process. Adapter changes need LS restart.
    if (GetModuleHandleW(L"nvcuda.dll")) throw std::runtime_error("CUDA already loaded; restart Lossless Scaling on the AMD adapter");
    module=LoadLibraryExW(path.c_str(),nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (!module) throw std::runtime_error("Cannot load AMD NR runtime; install the matching Windows HIP runtime and check dependencies");
    if(requireAdapterSelection) {
        auto version=reinterpret_cast<unsigned(*)()>(GetProcAddress(module,"lsc_dlss5nr_adapter_selection_version"));
        if(!version||version()!=1)throw std::runtime_error("AMD adapter-selection bridge is missing; reinstall Companion Addon Host");
        selectedOrdinal=reinterpret_cast<decltype(selectedOrdinal)>(GetProcAddress(module,"lsc_dlss5nr_selected_cuda_ordinal"));
        selectedLuid=reinterpret_cast<decltype(selectedLuid)>(GetProcAddress(module,"lsc_dlss5nr_selected_adapter_luid"));
        if(!selectedOrdinal||!selectedLuid)throw std::runtime_error("AMD adapter-selection exports are incomplete");
    }
#define LOAD(member,name) member=reinterpret_cast<decltype(member)>(GetProcAddress(module,name)); if(!member) throw std::runtime_error("AMD runtime missing " name)
    LOAD(create,"dlss5nr_create"); LOAD(destroy,"dlss5nr_destroy"); LOAD(defaults,"dlss5nr_default_settings");
    LOAD(settings,"dlss5nr_set_settings"); LOAD(process,"dlss5nr_process"); LOAD(present,"dlss5nr_present"); LOAD(status,"dlss5nr_get_status");
#undef LOAD
}
static void PrepareRuntimeEnvironment(const Settings& settings) {
    SetEnvironmentVariableW(L"DLSS5NR_COPY",L"0");
    SetEnvironmentVariableW(L"DLSS5NR_ALLOW_TRANSLATION",L"0");
    SetEnvironmentVariableW(L"ZLUDA_CACHE_DIR",(settings.dataDir+L"/ComputeCache").c_str());
    SetEnvironmentVariableW(L"DLSS5NR_GATE_LOG",(settings.dataDir+L"/gate.log").c_str());
    wchar_t stream[8]{};
    if(!(GetEnvironmentVariableW(L"DLSS5NR_PER_THREAD_STREAM",stream,8)&&stream[0]==L'0'))
        SetEnvironmentVariableW(L"ZLUDA_PER_THREAD_STREAM",L"1");
    // HIP SDK's bin directory is not necessarily on PATH in an already-running
    // Companion session. Load its runtime explicitly when HIP_PATH is available.
    wchar_t hipRoot[32768]{};
    if(GetEnvironmentVariableW(L"HIP_PATH",hipRoot,32768)) {
        auto hip=std::filesystem::path(hipRoot)/L"bin/amdhip64_7.dll";
        if(std::filesystem::is_regular_file(hip)&&!GetModuleHandleW(L"amdhip64_7.dll")&&
           !LoadLibraryExW(hip.c_str(),nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_DEFAULT_DIRS))
            throw std::runtime_error("Cannot load Windows HIP 7 from HIP_PATH; check its dependencies and restart LS");
    }
}
static void Barrier(ID3D12GraphicsCommandList* list,ID3D12Resource* r,D3D12_RESOURCE_STATES from,D3D12_RESOURCE_STATES to) {
    if(from==to)return;
    D3D12_RESOURCE_BARRIER b{}; b.Type=D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b.Transition={r,D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES,from,to}; list->ResourceBarrier(1,&b);
}
static ComPtr<ID3D12Resource> Texture(ID3D12Device* d,unsigned w,unsigned h,DXGI_FORMAT format) {
    D3D12_HEAP_PROPERTIES heap{}; heap.Type=D3D12_HEAP_TYPE_DEFAULT;
    D3D12_RESOURCE_DESC desc{}; desc.Dimension=D3D12_RESOURCE_DIMENSION_TEXTURE2D; desc.Width=w;desc.Height=h;
    desc.DepthOrArraySize=1;desc.MipLevels=1;desc.Format=format;desc.SampleDesc.Count=1;
    desc.Flags=D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
    ComPtr<ID3D12Resource> out;
    Check(d->CreateCommittedResource(&heap,D3D12_HEAP_FLAG_NONE,&desc,
          D3D12_RESOURCE_STATE_UNORDERED_ACCESS,nullptr,IID_PPV_ARGS(&out)),"Create texture");
    return out;
}
void Pipeline::Wait(uint64_t value) {
    if(!value||done_->GetCompletedValue()>=value)return;
    auto t=std::chrono::steady_clock::now(); Check(done_->SetEventOnCompletion(value,event_),"Fence event");
    if(WaitForSingleObject(event_,2000)!=WAIT_OBJECT_0)throw std::runtime_error("AMD NR GPU work timed out");
    waitMs+=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-t).count();
}
Pipeline::~Pipeline() {
    try { if(done_)Wait(signal_); } catch(...) { return; }
    if(runtime_)api_.destroy(runtime_);
    flow_.Shutdown(); if(event_)CloseHandle(event_);
    // The runtime's worker dependencies remain loaded until process exit.
}
void Pipeline::Initialize(ID3D11Device* device11,const Settings& settings) {
    Check(device11->QueryInterface(IID_PPV_ARGS(&device11_)),"D3D11.4 device");
    ComPtr<ID3D11DeviceContext> ctx; device11->GetImmediateContext(&ctx);Check(ctx.As(&context11_),"D3D11.4 context");
    ComPtr<IDXGIDevice> dxgi;Check(device11->QueryInterface(IID_PPV_ARGS(&dxgi)),"DXGI device");
    ComPtr<IDXGIAdapter> adapter;Check(dxgi->GetAdapter(&adapter),"Scaling adapter");DXGI_ADAPTER_DESC desc{};
    Check(adapter->GetDesc(&desc),"Scaling adapter description");
    if(desc.VendorId!=0x1002&&!(softwareProbe_&&desc.VendorId==0x1414))
        throw std::runtime_error("AMD NR requires the actual scaling adapter to be AMD");
    if(!softwareProbe_)VerifyModel(settings.snippet);
    Check(D3D12CreateDevice(adapter.Get(),D3D_FEATURE_LEVEL_11_0,IID_PPV_ARGS(&device_)),"D3D12 on scaling adapter");
    settings_=settings;
    if(!softwareProbe_)PrepareRuntimeEnvironment(settings);
    api_.Load(settings.library.empty()?settings.runtimeDir+L"/dlss5nr_runtime.dll":settings.library,!softwareProbe_);
    D3D12_COMMAND_QUEUE_DESC q{};q.Type=D3D12_COMMAND_LIST_TYPE_DIRECT;Check(device_->CreateCommandQueue(&q,IID_PPV_ARGS(&queue_)),"Create queue");
    Check(device_->CreateFence(0,D3D12_FENCE_FLAG_SHARED,IID_PPV_ARGS(&done_)),"Completion fence");
    Check(device_->CreateFence(0,D3D12_FENCE_FLAG_SHARED,IID_PPV_ARGS(&capture_)),"Capture fence");
    auto shareFence=[&](ID3D12Fence* src,ComPtr<ID3D11Fence>& dst){HANDLE handle=nullptr;
        Check(device_->CreateSharedHandle(src,nullptr,GENERIC_ALL,nullptr,&handle),"Share fence");
        HRESULT hr=device11_->OpenSharedFence(handle,IID_PPV_ARGS(&dst));CloseHandle(handle);Check(hr,"Open D3D11 fence");};
    shareFence(done_.Get(),done11_);shareFence(capture_.Get(),capture11_);
    event_=CreateEventW(nullptr,FALSE,FALSE,nullptr);if(!event_)throw std::runtime_error("Create GPU fence event failed");
    for(auto& slot:slots_) {
        Check(device_->CreateCommandAllocator(q.Type,IID_PPV_ARGS(&slot.allocator)),"Create allocator");
        Check(device_->CreateCommandList(0,q.Type,slot.allocator.Get(),nullptr,IID_PPV_ARGS(&slot.list)),"Create command list");
        Check(slot.list->Close(),"Close new list");
    }
    if(!flow_.Init(device_.Get(),[](const char* msg){OutputDebugStringA(msg);}))throw std::runtime_error("Compute optical flow initialization failed");
    UINT64 freq=0;Check(queue_->GetTimestampFrequency(&freq),"Timestamp frequency");flow_.SetTimestampFrequency(freq);
    dlss5nr_create_info info{};info.struct_size=sizeof(info);info.device=device_.Get();
    info.runtime_dir=settings.runtimeDir.c_str();info.snippet_path=settings.snippet.c_str();info.data_dir=settings.dataDir.c_str();
    if(api_.create(&info,&runtime_)!=0||!runtime_)throw std::runtime_error("AMD NR runtime creation failed");
}
void Pipeline::Resize(unsigned w,unsigned h,DXGI_FORMAT format) {
    Wait(signal_); width_=w;height_=h;format_=format;
    // Create on D3D11 so the allocation carries its D3D11 sharing metadata.
    // Opening an ordinary D3D12 allocation in D3D11 is rejected by some drivers.
    auto share=[&](ComPtr<ID3D11Texture2D>& source,ComPtr<ID3D12Resource>& target){
        D3D11_TEXTURE2D_DESC d{};d.Width=w;d.Height=h;d.MipLevels=d.ArraySize=1;d.Format=format;d.SampleDesc.Count=1;
        d.BindFlags=D3D11_BIND_SHADER_RESOURCE|D3D11_BIND_UNORDERED_ACCESS;
        d.MiscFlags=D3D11_RESOURCE_MISC_SHARED|D3D11_RESOURCE_MISC_SHARED_NTHANDLE;
        Check(device11_->CreateTexture2D(&d,nullptr,source.ReleaseAndGetAddressOf()),"Create shared D3D11 texture");
        ComPtr<IDXGIResource1> resource;Check(source.As(&resource),"Shared DXGI resource");HANDLE handle=nullptr;
        Check(resource->CreateSharedHandle(nullptr,DXGI_SHARED_RESOURCE_READ|DXGI_SHARED_RESOURCE_WRITE,nullptr,&handle),"Share texture");
        HRESULT hr=device_->OpenSharedHandle(handle,IID_PPV_ARGS(target.ReleaseAndGetAddressOf()));CloseHandle(handle);Check(hr,"Open shared D3D12 texture");};
    share(input11_,input_);share(output11_,output_);
    motion_=Texture(device_.Get(),w,h,DXGI_FORMAT_R16G16_FLOAT);distrust_=Texture(device_.Get(),w,h,DXGI_FORMAT_R8_UNORM);
    if(!flow_.Ensure(w,h))throw std::runtime_error("Compute flow allocation failed");flow_.Forget();first_=true;
}
bool Pipeline::Process(const EamRealFrame& frame,const Settings& settings) {
    waitMs=0;
    auto* ctx=static_cast<ID3D11DeviceContext*>(frame.context);
    auto* original=static_cast<ID3D11Texture2D*>(frame.original);
    auto* output=static_cast<ID3D11Texture2D*>(frame.output);
    if(!ctx||!original||!output)throw std::runtime_error("Missing real-frame textures");
    ComPtr<ID3D11Device> dev;ctx->GetDevice(&dev);
    if(!device_)Initialize(dev.Get(),settings);
    if(dev.Get()!=static_cast<ID3D11Device*>(device11_.Get()))throw std::runtime_error("Scaling device changed; restart Lossless Scaling");
    D3D11_TEXTURE2D_DESC desc{};original->GetDesc(&desc);
    if(desc.Format!=DXGI_FORMAT_R8G8B8A8_UNORM&&desc.Format!=DXGI_FORMAT_R16G16B16A16_FLOAT)
        throw std::runtime_error("AMD NR capture format is unsupported");
    if(width_!=frame.width||height_!=frame.height||format_!=desc.Format)Resize(frame.width,frame.height,desc.Format);
    unsigned index=unsigned(frames%slots_.size());auto& slot=slots_[index];Wait(slot.fence);flow_.ReadStats(index);
    double times[4]{};if(flow_.TakeStageTimes(times))flowMs=times[0]+times[1]+times[2]+times[3];
    bool reset=first_||frame.epoch!=epoch_||frame.sequence!=sequence_+1;
    if(reset)flow_.Forget();
    uint64_t serial=++signal_;
    D3D11_BOX region{0,0,0,width_,height_,1};
    context11_->CopySubresourceRegion(input11_.Get(),0,0,0,0,original,0,&region);
    Check(context11_->Signal(capture11_.Get(),serial),"Signal capture");context11_->Flush();
    Check(queue_->Wait(capture_.Get(),serial),"Wait for capture");
    Check(slot.allocator->Reset(),"Reset allocator");Check(slot.list->Reset(slot.allocator.Get(),nullptr),"Reset list");
    auto* list=slot.list.Get();
    Barrier(list,input_.Get(),D3D12_RESOURCE_STATE_COMMON,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    flow_.Record(list,int(index),input_.Get(),format_,motion_.Get(),distrust_.Get(),0,frame.encoding==EamFrameEncoding::ScRgb?1:0);
    Barrier(list,motion_.Get(),D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
    api_.settings(runtime_,&settings.model);
    dlss5nr_frame f{};f.struct_size=sizeof(f);f.command_list=list;f.input=input_.Get();f.output=output_.Get();
    f.input_state=D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;f.output_state=D3D12_RESOURCE_STATE_COMMON;
    f.width=width_;f.height=height_;f.motion=motion_.Get();f.motion_state=D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;
    f.motion_width=width_;f.motion_height=height_;f.motion_scale_x=f.motion_scale_y=1;f.pre_exposure=1;
    f.hdr=frame.encoding==EamFrameEncoding::ScRgb;f.reset=reset;
    // Capture has no scene depth. Do not manufacture depth or claim game motion.
    int result=api_.process(runtime_,&f);
    Barrier(list,motion_.Get(),D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    Barrier(list,input_.Get(),D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_COMMON);
    Check(list->Close(),"Close frame list");ID3D12CommandList* lists[]={list};queue_->ExecuteCommandLists(1,lists);
    // The runtime's CPU/network worker must learn about this submission before
    // we expose output to D3D11. It may enqueue additional GPU work here.
    api_.present(runtime_,queue_.Get());Check(queue_->Signal(done_.Get(),serial),"Signal output");slot.fence=serial;
    Check(context11_->Wait(done11_.Get(),serial),"Wait for NR output");
    if(result==0)context11_->CopySubresourceRegion(output,0,0,0,0,output11_.Get(),0,&region);
    status_.struct_size=sizeof(status_);api_.status(runtime_,&status_);
    if(api_.selectedLuid&&status_.state==DLSS5NR_STATE_RUNNING) {
        const auto luid=device_->GetAdapterLuid();
        const auto expected=(uint64_t(uint32_t(luid.HighPart))<<32)|luid.LowPart;
        if(api_.selectedLuid()!=expected)throw std::runtime_error("AMD compute context did not bind to the selected capture adapter");
    }
    epoch_=frame.epoch;sequence_=frame.sequence;first_=false;++frames;
    return result==0;
}
}
