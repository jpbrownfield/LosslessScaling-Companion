// SPDX-License-Identifier: MIT
// Included inside namespace eam. The SDK ABI is unchanged. Only the reviewed
// NVIDIA/AMD NR pair may share registration; other transformers stay exclusive.
static bool IsNrModule(void* module,const wchar_t* name) {
    wchar_t path[32768]{};
    if(!module||!GetModuleFileNameW(static_cast<HMODULE>(module),path,32768))return false;
    const wchar_t* filename=wcsrchr(path,L'\\');return _wcsicmp(filename?filename+1:path,name)==0;
}
static thread_local HostImpl* routedHost=nullptr;
static thread_local unsigned routedVendor=0;
static thread_local Microsoft::WRL::ComPtr<ID3D11Device> routedDevice;
unsigned HostImpl::SelectRealFrameAdapter(void* pointer) {
    if(!pointer)return m_realVendor.load(std::memory_order_acquire);
    if(routedHost==this&&routedDevice.Get()==pointer)return routedVendor;
    auto* device=static_cast<ID3D11Device*>(pointer);
    Microsoft::WRL::ComPtr<IDXGIDevice> dxgi;
    Microsoft::WRL::ComPtr<IDXGIAdapter> adapter;DXGI_ADAPTER_DESC desc{};
    if(FAILED(device->QueryInterface(IID_PPV_ARGS(&dxgi)))||FAILED(dxgi->GetAdapter(&adapter))||FAILED(adapter->GetDesc(&desc))) {
        routedHost=this;routedDevice=device;routedVendor=0;
        return 0;
    }
    lsc::ObserveRealFrameAdapter(desc.VendorId);
    m_realVendor.store(desc.VendorId,std::memory_order_release);
    routedHost=this;routedDevice=device;routedVendor=desc.VendorId;
    return routedVendor;
}
bool HostImpl::RealFrameRequired() const {
    if(!m_nativeNr.load(std::memory_order_acquire))return m_realRequired.load(std::memory_order_acquire);
    const auto vendor=routedHost==this?routedVendor:m_realVendor.load(std::memory_order_acquire);
    const auto bound=m_boundRealVendor.load(std::memory_order_acquire);
    if(bound&&bound!=vendor)return false;
    if(vendor==0x1002)return m_amdRealRequired.load(std::memory_order_acquire);
    if(vendor&&vendor!=0x10de)return false;
    return m_realRequired.load(std::memory_order_acquire);
}
bool HostImpl::SetRealFrameCallback(EamRealFrameCallback cb,void* user) {
    void* module=ModuleAt(_ReturnAddress());
    const bool amd=IsNrModule(module,L"LSC_AMDNR.dll");
    std::lock_guard<std::mutex> lock(m_realMutex);
    auto& hook=amd?m_amdRealHook:m_realHook;
    auto& owner=amd?m_amdRealModule:m_realModule;
    auto& required=amd?m_amdRealRequired:m_realRequired;
    if(hook.callback&&hook.module!=module)return false;
    if(cb&&amd&&m_realHook.callback&&!IsNrModule(m_realHook.module,L"DLSSNR02.dll"))return false;
    if(cb&&!amd&&m_amdRealHook.callback&&!IsNrModule(module,L"DLSSNR02.dll"))return false;
    hook={cb,user,cb?module:nullptr};owner.store(cb?module:nullptr,std::memory_order_release);
    if(!amd)m_nativeNr.store(cb&&IsNrModule(module,L"DLSSNR02.dll"),std::memory_order_release);
    if(!cb)required.store(false,std::memory_order_release);
    m_realCount.store((m_realHook.callback?1:0)+(m_amdRealHook.callback?1:0),std::memory_order_release);
    return true;
}
EamFrameSupport HostImpl::GetRealFrameSupport(){return RealFrameHook::Support();}
void HostImpl::RequireRealFrameProcessing(bool required) {
    const auto module=ModuleAt(_ReturnAddress());
    if(m_realModule.load(std::memory_order_acquire)==module)m_realRequired.store(required,std::memory_order_release);
    if(m_amdRealModule.load(std::memory_order_acquire)==module)m_amdRealRequired.store(required,std::memory_order_release);
}
bool HostImpl::InvokeRealFrame(const EamRealFrame& frame) {
    auto* ctx=static_cast<ID3D11DeviceContext*>(frame.context);
    Microsoft::WRL::ComPtr<ID3D11Device> device;if(ctx)ctx->GetDevice(&device);
    const auto vendor=SelectRealFrameAdapter(device.Get());
    if(!vendor&&device){SetStatus("LSC-AMDNR","Cannot identify the capture adapter; neural rendering is unavailable",3);return false;}
    if(m_nativeNr.load()||m_amdRealModule.load()) {
        unsigned empty=0;
        m_boundRealVendor.compare_exchange_strong(empty,vendor);
        if(m_boundRealVendor.load()!=vendor) {
            SetStatus(vendor==0x10de?"DLSSNR02":"LSC-AMDNR","Scaling GPU vendor changed; restart Lossless Scaling before enabling NR on it",3);
            return false;
        }
    }
    if((m_nativeNr.load()||m_amdRealModule.load())&&vendor&&vendor!=0x10de&&vendor!=0x1002){
        SetStatus("LSC-AMDNR","Neural rendering unavailable on this scaling adapter (AMD or NVIDIA required)",2);return false;
    }
    // Delay NVIDIA proxy loading until a verified real capture picks its GPU.
    // Other LS devices/compute passes must not choose a process-wide CUDA stack.
    lsc::PrepareSm86ForAdapter(vendor);
    std::lock_guard<std::mutex> lock(m_realMutex);
    const bool useAmd=vendor==0x1002&&(m_nativeNr.load()||m_amdRealModule.load());
    auto& hook=useAmd?m_amdRealHook:m_realHook;
    if(!hook.callback)return false;
    bool applied=false;
    if(!CallReal(hook.callback,frame,hook.owner,&applied)) {
        RealFrameHook::CallbackFailed();
        LOG_ERROR("Host","Real-frame transform faulted");
        while(RealFrameRequired())Sleep(25);
        if(useAmd){m_amdRealModule.store(nullptr);m_amdRealRequired.store(false);}
        else {m_realModule.store(nullptr);m_realRequired.store(false);}
        hook={};m_realCount.store((m_realHook.callback?1:0)+(m_amdRealHook.callback?1:0),std::memory_order_release);return false;
    }
    if(!applied&&RealFrameRequired())RealFrameHook::CallbackFailed();
    while(!applied&&RealFrameRequired())Sleep(25);
    return applied;
}
