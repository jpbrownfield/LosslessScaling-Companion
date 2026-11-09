// SPDX-License-Identifier: MIT
#include "../host/host_impl.h"
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
int main(){try {
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
