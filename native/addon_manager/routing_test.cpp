// SPDX-License-Identifier: MIT
#include "../host/host_impl.h"
#include "d3d11_hook.h"
#include "context_identity.h"
#include "eam/shader_identity.h"
#include <d3dcompiler.h>
#include <dxgi.h>
#include <windows.h>
#include <iostream>
#include <stdexcept>
namespace eam {
struct CompanionRoutingTest {
    static void Vendor(HostImpl& h,unsigned v){h.m_realVendor.store(v);}
};
}
struct Fixture {
    HMODULE dll=nullptr;
    bool (*reg)(IHost*,bool)=nullptr;void (*remove)()=nullptr;unsigned (*calls)()=nullptr;
    explicit Fixture(const wchar_t* path){dll=LoadLibraryW(path);if(!dll)throw std::runtime_error("Load routing fixture");
        reg=reinterpret_cast<decltype(reg)>(GetProcAddress(dll,"Register"));
        remove=reinterpret_cast<decltype(remove)>(GetProcAddress(dll,"Remove"));
        calls=reinterpret_cast<decltype(calls)>(GetProcAddress(dll,"Calls"));
        if(!reg||!remove||!calls)throw std::runtime_error("Fixture exports");}
};
static void Expect(bool ok,const char* text){if(!ok)throw std::runtime_error(text);}
static bool Generic(const EamRealFrame&,void*){return true;}
// Exercise ReShade's public unwrapping contract without requiring a GPU or DLL.
// This is an IUnknown proxy, never a fake ID3D11DeviceContext implementation.
struct ContextAlias final : IUnknown {
    IUnknown* native;
    ULONG references=1;
    explicit ContextAlias(IUnknown* value):native(value){}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid,void** out) override {
        if(!out)return E_POINTER;*out=nullptr;
        if(iid==lsc::kUnwrappedContext && native){native->AddRef();*out=native;return S_OK;}
        if(iid==__uuidof(IUnknown)){AddRef();*out=this;return S_OK;}
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override{return ++references;}
    ULONG STDMETHODCALLTYPE Release() override{return --references;}
};
static unsigned dispatchCallbacks=0;
static bool CountDispatch(uint32_t,uint32_t,uint32_t,void*){++dispatchCallbacks;return false;}
static void TestDispatchIdentity(const wchar_t* reshade) {
    using Microsoft::WRL::ComPtr;
    if(reshade) {
        // Explicitly opt this fixture into loading without a game ReShade.ini.
        SetEnvironmentVariableW(L"RESHADE_DISABLE_LOADING_CHECK",L"1");
        Expect(LoadLibraryW(reshade)!=nullptr,"Official ReShade DLL loads");
    }
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    Expect(SUCCEEDED(D3D11CreateDevice(nullptr,reshade?D3D_DRIVER_TYPE_HARDWARE:D3D_DRIVER_TYPE_WARP,
        nullptr,0,nullptr,0,D3D11_SDK_VERSION,&device,nullptr,&context)),"Dispatch fixture device creates");
    const auto native=lsc::NativeDispatchContext(context.Get());
    Expect(native!=nullptr,"Native immediate context resolves");
    if(reshade) {
        ComPtr<IDXGIDevice> dxgi;
        ComPtr<IDXGIAdapter> adapter;
        DXGI_ADAPTER_DESC desc{};
        const bool basic=SUCCEEDED(device.As(&dxgi)) && SUCCEEDED(dxgi->GetAdapter(&adapter)) &&
            SUCCEEDED(adapter->GetDesc(&desc)) && desc.VendorId==0x1414 && desc.DeviceId==0x8C;
        if(basic)std::cout<<"SKIP actual ReShade context proxy: Microsoft Basic Render Driver is excluded by ReShade.\n";
        else Expect(native.Get()!=context.Get(),"Official ReShade provides a distinct context proxy");
    }
    ContextAlias proxy(native.Get()),invalid(nullptr);
    Expect(lsc::NativeDispatchContext(&proxy).Get()==native.Get(),"Proxy unwrap resolves the runtime context");
    Expect(!lsc::NativeDispatchContext(&invalid),"Unrelated COM object is not a context");
    Expect(!lsc::NativeDispatchContext(nullptr),"Null context is rejected");
    eam::HostImpl host;
    D3D11Hook::Initialize(&host);
    Expect(D3D11Hook::InstallDispatchHooks()>0,"Runtime Dispatch hooks install");
    host.SetPreDispatchCallback(CountDispatch,nullptr);
    ComPtr<ID3DBlob> code,error;
    const char shader[]="[numthreads(1,1,1)] void main() {}";
    Expect(SUCCEEDED(D3DCompile(shader,sizeof(shader),nullptr,nullptr,nullptr,"main","cs_5_0",0,0,&code,&error)),"Compile fixture shader");
    D3D11Hook::Attach(device.Get(),context.Get());
    ComPtr<ID3D11ComputeShader> compute;
    Expect(SUCCEEDED(device->CreateComputeShader(code->GetBufferPointer(),code->GetBufferSize(),nullptr,&compute)),"Create identified shader");
    eam::ShaderSha256 hash{};
    Expect(eam::ReadShaderSha256(compute.Get(),hash),"Shader identity survives the device proxy");
    context->CSSetShader(compute.Get(),nullptr,0);
    for(unsigned i=0;i<3;++i)context->Dispatch(1,1,1);
    Expect(D3D11Hook::GetDispatchCount()==3 && dispatchCallbacks==3,"Every dispatch reaches callbacks exactly once");
    Expect(host.GetD3D11DeviceContext()==context.Get(),"Public host context retains its original identity");
    D3D11Hook::Shutdown();
    context->Dispatch(1,1,1);
    Expect(dispatchCallbacks==3,"Shutdown stops dispatch callbacks");
    std::cout<<"Dispatch identity passed (native, proxy contract, shader identity, exactly-once callbacks).\n";
}
int wmain(int argc,wchar_t** argv){try {
    TestDispatchIdentity(argc>1?argv[1]:nullptr);
    Fixture nv(L"routing-fixtures/DLSSNR02.dll"),amd(L"routing-fixtures/LSC_AMDNR.dll");
    for(bool amdFirst:{false,true})for(unsigned vendor:{0x10deu,0x1002u,0x8086u}) {
        eam::HostImpl host;unsigned nvBefore=nv.calls(),amdBefore=amd.calls();
        if(amdFirst){Expect(amd.reg(&host,false),"AMD registration");Expect(nv.reg(&host,true),"NV alongside AMD");}
        else {Expect(nv.reg(&host,true),"NV registration");Expect(amd.reg(&host,false),"AMD alongside NV");}
        Expect(host.DispatchHookCount()==2,"Both callbacks retained");
        Expect(!host.SetRealFrameCallback(Generic,nullptr),"Unrelated transformer must remain exclusive");
        eam::CompanionRoutingTest::Vendor(host,vendor);
        Expect(host.RealFrameRequired()==(vendor==0x10de),"Inactive NVIDIA requirement cannot pause AMD/Intel capture");
        EamRealFrame frame;Expect(host.InvokeRealFrame(frame)==(vendor!=0x8086),"Vendor route outcome");
        Expect(nv.calls()==nvBefore+(vendor==0x10de),"Only NVIDIA invokes native NR");
        Expect(amd.calls()==amdBefore+(vendor==0x1002),"Only AMD invokes AMD NR");
        nv.remove();Expect(host.DispatchHookCount()==1,"Removing NVIDIA preserves AMD");amd.remove();Expect(!host.HasRealFrameCallback(),"Both removed");
    }
    {
        eam::HostImpl host;Expect(nv.reg(&host,false)&&amd.reg(&host,false),"Register pair");
        eam::CompanionRoutingTest::Vendor(host,0x1002);EamRealFrame frame;Expect(host.InvokeRealFrame(frame),"Initial AMD capture");
        auto before=nv.calls();eam::CompanionRoutingTest::Vendor(host,0x10de);
        Expect(!host.InvokeRealFrame(frame)&&nv.calls()==before,"Vendor switch requires restart before loading another driver stack");
        amd.remove();nv.remove();
    }
    {
        eam::HostImpl host;Expect(host.SetRealFrameCallback(Generic,nullptr),"Generic registration");
        Expect(!amd.reg(&host,false),"AMD cannot displace a generic owner");
        eam::CompanionRoutingTest::Vendor(host,0x1002);EamRealFrame frame;Expect(host.InvokeRealFrame(frame),"Existing generic AMD transformer unaffected");
        host.SetRealFrameCallback(nullptr,nullptr);
    }
    {
        eam::HostImpl host;Expect(nv.reg(&host,false)&&amd.reg(&host,false),"Register cleanup pair");
        auto base=reinterpret_cast<uintptr_t>(amd.dll);
        auto dos=reinterpret_cast<const IMAGE_DOS_HEADER*>(base);
        auto nt=reinterpret_cast<const IMAGE_NT_HEADERS*>(base+dos->e_lfanew);
        Expect(host.ForgetCode(base,base+nt->OptionalHeader.SizeOfImage)==1,"Forget AMD module");
        Expect(host.DispatchHookCount()==1,"Forget AMD preserves NVIDIA");
        nv.remove();
    }
    std::cout<<"Automatic NR routing passed (both registration orders, NVIDIA/AMD/Intel, ownership, switch and cleanup).\n";return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
