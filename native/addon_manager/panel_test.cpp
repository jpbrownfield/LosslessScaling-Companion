// Off-screen test of the panel's actual renderer. Optionally pass the full path
// to an official ReShade DLL to verify exclusion while its system hooks run.
#include "../gui/window/window_device.h"
#include <dxgi1_2.h>
#include <wrl/client.h>
#include <cstdio>

using Microsoft::WRL::ComPtr;
// ReShade v6.8.0's device/swap-chain proxy IIDs, used only by this test.
static const GUID reshadeDevice = {0x72299288,0x2C68,0x4AD8,{0x94,0x5D,0x2B,0xFB,0x5A,0xA9,0xC6,0x09}};
static const GUID reshadeChain = {0x1F445F9F,0x9887,0x4C4C,{0x90,0x55,0x4E,0x3B,0xAD,0xAF,0xCC,0xA8}};
static int failures=0;
static bool Check(const char* name, bool ok) {
    std::printf("%s  %s\n",ok ? "PASS" : "FAIL",name);
    if (!ok) ++failures;
    return ok;
}
static bool Proxied(IUnknown* object, REFIID iid) {
    ComPtr<IUnknown> proxy;
    return SUCCEEDED(object->QueryInterface(iid,&proxy));
}

int wmain(int argc, wchar_t** argv) {
    std::setvbuf(stdout,nullptr,_IONBF,0);
    if (argc>1) {
        if (!Check("official ReShade test DLL loads",LoadLibraryW(argv[1])!=nullptr)) return 1;
        // Positive control: prove ReShade is intercepting D3D11 in this process.
        ComPtr<ID3D11Device> hardware;
        if (!Check("hardware control device creates",SUCCEEDED(D3D11CreateDevice(nullptr,
            D3D_DRIVER_TYPE_HARDWARE,nullptr,0,nullptr,0,D3D11_SDK_VERSION,&hardware,nullptr,nullptr)))) return 1;
        ComPtr<IDXGIDevice> dxgi;
        ComPtr<IDXGIAdapter> adapter;
        DXGI_ADAPTER_DESC desc{};
        const bool basic=SUCCEEDED(hardware.As(&dxgi)) && SUCCEEDED(dxgi->GetAdapter(&adapter)) &&
            SUCCEEDED(adapter->GetDesc(&desc)) && desc.VendorId==0x1414 && desc.DeviceId==0x8C;
        if (basic) std::puts("SKIP  hardware proxy control: Microsoft Basic Render Driver is also excluded by ReShade");
        else if (!Check("ReShade hooks the hardware control device",Proxied(hardware.Get(),reshadeDevice))) return 1;
    }
    HWND hwnd=CreateWindowExW(0,L"STATIC",L"Companion panel renderer test",WS_POPUP,
        -32000,-32000,160,90,nullptr,nullptr,GetModuleHandleW(nullptr),nullptr);
    if (!Check("off-screen panel window creates",hwnd!=nullptr)) return 1;
    {
        eam::window::WindowDevice panel;
        if (!Check("panel renderer creates",panel.Create(hwnd))) return 1;
        ComPtr<IDXGIDevice> dxgi;
        ComPtr<IDXGIAdapter> adapter;
        ComPtr<IDXGIAdapter1> adapter1;
        DXGI_ADAPTER_DESC1 desc{};
        const bool software=SUCCEEDED(panel.Device()->QueryInterface(IID_PPV_ARGS(&dxgi))) &&
            SUCCEEDED(dxgi->GetAdapter(&adapter)) && SUCCEEDED(adapter.As(&adapter1)) &&
            SUCCEEDED(adapter1->GetDesc1(&desc)) && (desc.Flags&DXGI_ADAPTER_FLAG_SOFTWARE);
        Check("panel uses its separate software renderer",software);
        Check("ReShade does not proxy the panel device",!Proxied(panel.Device(),reshadeDevice));
        Check("ReShade does not proxy the panel swap chain",!Proxied(panel.SwapChain(),reshadeChain));
        for (int resized=0;resized<2;++resized) {
            if (resized) panel.Resize(240,135);
            const float color[4]={0.25f,0.5f,0.75f,1.f};
            panel.Context()->ClearRenderTargetView(panel.BackBuffer(),color);
            ComPtr<ID3D11Texture2D> buffer,readback;
            if (!Check("panel back buffer is available",SUCCEEDED(panel.SwapChain()->GetBuffer(0,IID_PPV_ARGS(&buffer))))) break;
            D3D11_TEXTURE2D_DESC texture{};buffer->GetDesc(&texture);
            texture.Usage=D3D11_USAGE_STAGING;texture.BindFlags=0;texture.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
            if (!Check("panel pixels can be read back",SUCCEEDED(panel.Device()->CreateTexture2D(&texture,nullptr,&readback)))) break;
            panel.Context()->CopyResource(readback.Get(),buffer.Get());
            D3D11_MAPPED_SUBRESOURCE map{};
            if (Check("panel pixels map",SUCCEEDED(panel.Context()->Map(readback.Get(),0,D3D11_MAP_READ,0,&map)))) {
                const auto* p=static_cast<const unsigned char*>(map.pData);
                Check("panel colors are preserved",p[0]==64 && p[1]==128 && p[2]==191 && p[3]==255);
                panel.Context()->Unmap(readback.Get(),0);
            }
            Check("panel presents without an effect runtime",SUCCEEDED(panel.SwapChain()->Present(0,DXGI_PRESENT_TEST)));
        }
    }
    DestroyWindow(hwnd);
    return failures ? 1 : 0;
}
