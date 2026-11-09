// SPDX-License-Identifier: MIT
#include "pipeline.h"
#include <iostream>
#include <vector>
#include <cmath>
#include <cstring>
#include <stdexcept>
#include <filesystem>

namespace amd_nr {
std::string ProbeFlow(int adapterIndex,bool warp) {
    ComPtr<IDXGIFactory4> factory;Check(CreateDXGIFactory1(IID_PPV_ARGS(&factory)),"DXGI factory");
    ComPtr<IDXGIAdapter> adapter;
    if(warp)Check(factory->EnumWarpAdapter(IID_PPV_ARGS(&adapter)),"WARP adapter");
    else Check(factory->EnumAdapters(UINT(adapterIndex),&adapter),"Selected adapter");
    ComPtr<ID3D12Device> device;Check(D3D12CreateDevice(adapter.Get(),D3D_FEATURE_LEVEL_11_0,IID_PPV_ARGS(&device)),"D3D12 device");
    D3D12_COMMAND_QUEUE_DESC q{};ComPtr<ID3D12CommandQueue> queue;Check(device->CreateCommandQueue(&q,IID_PPV_ARGS(&queue)),"Queue");
    ComPtr<ID3D12CommandAllocator> alloc;Check(device->CreateCommandAllocator(q.Type,IID_PPV_ARGS(&alloc)),"Allocator");
    ComPtr<ID3D12GraphicsCommandList> list;Check(device->CreateCommandList(0,q.Type,alloc.Get(),nullptr,IID_PPV_ARGS(&list)),"List");
    ComPtr<ID3D12Fence> fence;Check(device->CreateFence(0,D3D12_FENCE_FLAG_NONE,IID_PPV_ARGS(&fence)),"Fence");
    HANDLE event=CreateEventW(nullptr,FALSE,FALSE,nullptr);if(!event)throw std::runtime_error("Event failed");
    constexpr unsigned w=256,h=192;
    D3D12_HEAP_PROPERTIES heap{};heap.Type=D3D12_HEAP_TYPE_DEFAULT;
    auto texture=[&](DXGI_FORMAT format,D3D12_RESOURCE_STATES state){D3D12_RESOURCE_DESC d{};d.Dimension=D3D12_RESOURCE_DIMENSION_TEXTURE2D;
        d.Width=w;d.Height=h;d.DepthOrArraySize=d.MipLevels=1;d.SampleDesc.Count=1;d.Format=format;d.Flags=D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
        ComPtr<ID3D12Resource> r;Check(device->CreateCommittedResource(&heap,D3D12_HEAP_FLAG_NONE,&d,state,nullptr,IID_PPV_ARGS(&r)),"Probe texture");return r;};
    auto input=texture(DXGI_FORMAT_R8G8B8A8_UNORM,D3D12_RESOURCE_STATE_COPY_DEST);
    auto motion=texture(DXGI_FORMAT_R16G16_FLOAT,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    auto distrust=texture(DXGI_FORMAT_R8_UNORM,D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT footprint{};UINT64 bytes=0;auto desc=input->GetDesc();device->GetCopyableFootprints(&desc,0,1,0,&footprint,nullptr,nullptr,&bytes);
    heap.Type=D3D12_HEAP_TYPE_UPLOAD;D3D12_RESOURCE_DESC buffer{};buffer.Dimension=D3D12_RESOURCE_DIMENSION_BUFFER;buffer.Width=bytes;
    buffer.Height=buffer.DepthOrArraySize=buffer.MipLevels=1;buffer.SampleDesc.Count=1;buffer.Layout=D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    ComPtr<ID3D12Resource> upload;Check(device->CreateCommittedResource(&heap,D3D12_HEAP_FLAG_NONE,&buffer,D3D12_RESOURCE_STATE_GENERIC_READ,nullptr,IID_PPV_ARGS(&upload)),"Upload");
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT motionFootprint{};auto motionDesc=motion->GetDesc();
    device->GetCopyableFootprints(&motionDesc,0,1,0,&motionFootprint,nullptr,nullptr,&bytes);
    heap.Type=D3D12_HEAP_TYPE_READBACK;buffer.Width=bytes;ComPtr<ID3D12Resource> readback;
    Check(device->CreateCommittedResource(&heap,D3D12_HEAP_FLAG_NONE,&buffer,D3D12_RESOURCE_STATE_COPY_DEST,nullptr,IID_PPV_ARGS(&readback)),"Motion readback");
    FlowEstimator flow;if(!flow.Init(device.Get(),[](const char* s){std::cerr<<s<<'\n';})||!flow.Ensure(w,h))throw std::runtime_error("Flow initialization failed");
    auto barrier=[&](D3D12_RESOURCE_STATES before,D3D12_RESOURCE_STATES after){D3D12_RESOURCE_BARRIER b{};b.Type=D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        b.Transition={input.Get(),0,before,after};list->ResourceBarrier(1,&b);};
    double movingLength=0,resetLength=0;
    for(unsigned frame=0;frame<3;++frame){
        if(frame){Check(alloc->Reset(),"Reset allocator");Check(list->Reset(alloc.Get(),nullptr),"Reset list");barrier(D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE,D3D12_RESOURCE_STATE_COPY_DEST);}
        unsigned char* pixels=nullptr;D3D12_RANGE empty{};Check(upload->Map(0,&empty,reinterpret_cast<void**>(&pixels)),"Map upload");
        for(unsigned y=0;y<h;++y)for(unsigned x=0;x<w;++x){unsigned sx=(x+w-(frame?8:0))%w;unsigned n=(sx/7*137+y/9*79+(sx*y)%251)%256;
            auto* p=pixels+y*footprint.Footprint.RowPitch+x*4;p[0]=p[1]=p[2]=static_cast<unsigned char>(n);p[3]=255;}
        upload->Unmap(0,nullptr);D3D12_TEXTURE_COPY_LOCATION dst{};dst.pResource=input.Get();dst.Type=D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        D3D12_TEXTURE_COPY_LOCATION src{};src.pResource=upload.Get();src.Type=D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;src.PlacedFootprint=footprint;
        list->CopyTextureRegion(&dst,0,0,0,&src,nullptr);barrier(D3D12_RESOURCE_STATE_COPY_DEST,D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE);
        if(frame==2)flow.Forget();flow.Record(list.Get(),0,input.Get(),desc.Format,motion.Get(),distrust.Get());
        if(frame==2) {
            D3D12_RESOURCE_BARRIER b{};b.Type=D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
            b.Transition={motion.Get(),0,D3D12_RESOURCE_STATE_UNORDERED_ACCESS,D3D12_RESOURCE_STATE_COPY_SOURCE};list->ResourceBarrier(1,&b);
            D3D12_TEXTURE_COPY_LOCATION rd{};rd.pResource=readback.Get();rd.Type=D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;rd.PlacedFootprint=motionFootprint;
            D3D12_TEXTURE_COPY_LOCATION mv{};mv.pResource=motion.Get();mv.Type=D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;list->CopyTextureRegion(&rd,0,0,0,&mv,nullptr);
        }
        Check(list->Close(),"Close probe");ID3D12CommandList* lists[]={list.Get()};queue->ExecuteCommandLists(1,lists);Check(queue->Signal(fence.Get(),frame+1),"Signal probe");
        Check(fence->SetEventOnCompletion(frame+1,event),"Probe event");if(WaitForSingleObject(event,30000)!=WAIT_OBJECT_0)throw std::runtime_error("Flow probe timed out");
        flow.ReadStats(0);if(frame==1)movingLength=flow.LastLength();
        if(frame==2) {
            // Read the actual reset vectors, since the estimator intentionally
            // publishes no motion statistics on its first/reset frame.
            unsigned char* mapped=nullptr;D3D12_RANGE range{0,SIZE_T(bytes)};
            Check(readback->Map(0,&range,reinterpret_cast<void**>(&mapped)),"Read reset motion");
            for(unsigned y=0;y<h;++y)for(unsigned x=0;x<w*2;++x){
                uint16_t value=0;memcpy(&value,mapped+y*motionFootprint.Footprint.RowPitch+x*2,2);
                if((value&0x7fff)!=0)resetLength=1;
            }
            readback->Unmap(0,&empty);
        }
    }
    flow.Shutdown();CloseHandle(event);
    if(!std::isfinite(movingLength)||movingLength<1||!std::isfinite(resetLength)||resetLength>.1)
        throw std::runtime_error("Flow motion/reset validation failed: "+std::to_string(movingLength)+" / "+std::to_string(resetLength));
    return "Compute flow passed: motion="+std::to_string(movingLength)+" px, reset="+std::to_string(resetLength)+" px. No NR inference tested.";
}
}
static void ProbeBridge(int index,bool warp) {
    using namespace amd_nr;
    ComPtr<IDXGIFactory4> factory;Check(CreateDXGIFactory1(IID_PPV_ARGS(&factory)),"Factory");ComPtr<IDXGIAdapter> adapter;
    if(warp)Check(factory->EnumWarpAdapter(IID_PPV_ARGS(&adapter)),"WARP");else Check(factory->EnumAdapters(UINT(index),&adapter),"Adapter");
    ComPtr<ID3D11Device> dev;ComPtr<ID3D11DeviceContext> ctx;
    Check(D3D11CreateDevice(adapter.Get(),D3D_DRIVER_TYPE_UNKNOWN,nullptr,0,nullptr,0,D3D11_SDK_VERSION,&dev,nullptr,&ctx),"D3D11 bridge probe");
    wchar_t exe[32768]{};GetModuleFileNameW(nullptr,exe,32768);auto root=std::filesystem::path(exe).parent_path();
    Settings settings;settings.runtimeDir=(root/L"probe-fixture").wstring();settings.dataDir=root.wstring();
    settings.model.struct_size=sizeof(settings.model);settings.model.enabled=1;
    Pipeline pipeline(true); // Copy fixture: no HIP adapter selection or inference.
    for(unsigned iteration=0;iteration<6;++iteration) {
        const bool hdr=iteration>=3;unsigned pixelBytes=hdr?8:4;
        unsigned w=iteration<3?256:320,h=192;
        D3D11_TEXTURE2D_DESC d{};d.Width=w;d.Height=h;d.MipLevels=d.ArraySize=1;d.SampleDesc.Count=1;
        d.Format=hdr?DXGI_FORMAT_R16G16B16A16_FLOAT:DXGI_FORMAT_R8G8B8A8_UNORM;d.BindFlags=D3D11_BIND_SHADER_RESOURCE|D3D11_BIND_UNORDERED_ACCESS;
        std::vector<uint32_t> pixels(w*h*(hdr?2:1));for(unsigned p=0;p<pixels.size();++p)
            pixels[p]=hdr?0x38003c00u+(p%128):0xff000000|((p*7919+iteration*1237)&0xffffff);
        D3D11_SUBRESOURCE_DATA data{pixels.data(),w*pixelBytes,0};ComPtr<ID3D11Texture2D> input,output,readback;
        Check(dev->CreateTexture2D(&d,&data,&input),"Probe input");Check(dev->CreateTexture2D(&d,nullptr,&output),"Probe output");
        EamRealFrame f;f.context=ctx.Get();f.original=input.Get();f.output=output.Get();f.width=w;f.height=h;f.sequence=iteration+1;f.epoch=iteration<3?1:2;
        f.encoding=hdr?EamFrameEncoding::ScRgb:EamFrameEncoding::Sdr709;
        if(!pipeline.Process(f,settings))throw std::runtime_error("Bridge fixture did not process frame");
        d.Usage=D3D11_USAGE_STAGING;d.BindFlags=0;d.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
        Check(dev->CreateTexture2D(&d,nullptr,&readback),"Probe readback");ctx->CopyResource(readback.Get(),output.Get());
        D3D11_MAPPED_SUBRESOURCE mapped{};Check(ctx->Map(readback.Get(),0,D3D11_MAP_READ,0,&mapped),"Read bridge output");
        bool equal=true;for(unsigned y=0;y<h;++y)if(memcmp(static_cast<unsigned char*>(mapped.pData)+y*mapped.RowPitch,
            reinterpret_cast<unsigned char*>(pixels.data())+y*w*pixelBytes,w*pixelBytes))equal=false;
        ctx->Unmap(readback.Get(),0);if(!equal)throw std::runtime_error("D3D11/D3D12 bridge output differs from capture");
        if(pipeline.Status().results)throw std::runtime_error("Copy fixture incorrectly reported inference");
    }
    std::cout<<"GPU bridge passed (SDR/scRGB shared textures/fences, ring reuse, resize and epoch reset). Copy fixture only; no NR inference tested.\n";
}
int wmain(int argc,wchar_t** argv) {
    try {
        if(argc==3&&std::wstring(argv[1])==L"--check-model") {
            amd_nr::VerifyModel(argv[2]);std::cout<<"AMD NR model matches the pinned runtime. No NR inference tested.\n";return 0;
        }
        if(argc==2&&std::wstring(argv[1])==L"--list") {
            amd_nr::ComPtr<IDXGIFactory1> f;amd_nr::Check(CreateDXGIFactory1(IID_PPV_ARGS(&f)),"Factory");
            for(UINT i=0;;++i){amd_nr::ComPtr<IDXGIAdapter1>a;if(f->EnumAdapters1(i,&a)==DXGI_ERROR_NOT_FOUND)break;
                DXGI_ADAPTER_DESC1 d{};a->GetDesc1(&d);std::wcout<<i<<L": "<<d.Description<<L" ("<<amd_nr::BackendForVendor(d.VendorId)<<L")\n";}return 0;
        }
        bool warp=false,flow=false,bridge=false;int index=0;
        for(int i=1;i<argc;++i){std::wstring a=argv[i];if(a==L"--warp")warp=true;else if(a==L"--flow-only")flow=true;
            else if(a==L"--bridge-only")bridge=true;
            else if(a==L"--adapter"&&i+1<argc)index=std::stoi(argv[++i]);else throw std::runtime_error("Unknown probe argument");}
        if(bridge){ProbeBridge(index,warp);return 0;}
        if(!flow)throw std::runtime_error("Usage: --list | --check-model DLL | [--adapter INDEX | --warp] --flow-only|--bridge-only");
        std::cout<<amd_nr::ProbeFlow(index,warp)<<'\n';return 0;
    } catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}
}
