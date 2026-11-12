// SPDX-License-Identifier: MIT
// Real D3D11 fixture: no LS restart, Gloam settings, or desktop color changes.
#include "hdr_probe.h"
#include <d3d11.h>
#include <dxgi1_4.h>
#include <wrl/client.h>
#include <cstdio>
using Microsoft::WRL::ComPtr;
int main() {
    lsc::StartHdrProbe();
    HANDLE map=OpenFileMappingW(FILE_MAP_ALL_ACCESS,FALSE,(L"Local\\LSCHdrProbe-"+std::to_wstring(GetCurrentProcessId())).c_str());
    auto* control=map?static_cast<lsc::HdrProbeControl*>(MapViewOfFile(map,FILE_MAP_ALL_ACCESS,0,0,24)):nullptr;
    if(!control)return 2;
    HWND window=CreateWindowExW(WS_EX_NOACTIVATE|WS_EX_TOOLWINDOW,L"STATIC",L"HDR test fixture",WS_POPUP,
        -32000,-32000,320,180,nullptr,nullptr,GetModuleHandleW(nullptr),nullptr);
    ShowWindow(window,SW_SHOWNOACTIVATE);
    DXGI_SWAP_CHAIN_DESC d{};d.BufferDesc.Width=320;d.BufferDesc.Height=180;d.BufferDesc.Format=DXGI_FORMAT_R16G16B16A16_FLOAT;
    d.SampleDesc.Count=1;d.BufferUsage=DXGI_USAGE_RENDER_TARGET_OUTPUT;d.BufferCount=2;d.OutputWindow=window;d.Windowed=TRUE;
    d.SwapEffect=DXGI_SWAP_EFFECT_FLIP_DISCARD;
    ComPtr<ID3D11Device> dev;ComPtr<ID3D11DeviceContext> ctx;ComPtr<IDXGISwapChain> chain;
    if(FAILED(D3D11CreateDeviceAndSwapChain(nullptr,D3D_DRIVER_TYPE_HARDWARE,nullptr,0,nullptr,0,D3D11_SDK_VERSION,&d,&chain,&dev,nullptr,&ctx)))return 3;
    ComPtr<ID3D11Texture2D> back;chain->GetBuffer(0,IID_PPV_ARGS(&back));
    ComPtr<ID3D11RenderTargetView> rtv;dev->CreateRenderTargetView(back.Get(),nullptr,&rtv);
    if(!rtv)return 4;
    lsc::ObserveHdrPresentation(chain.Get());
    if(lsc::HdrProbeSnapshot().scans)return 5;
    *control={1,1,1,400,GetTickCount64()};
    auto sample=[&](float value) {
        const float c[4]={value,value,value,1};ctx->ClearRenderTargetView(rtv.Get(),c);
        for(int i=0;i<4;++i) {control->heartbeat=GetTickCount64();lsc::ObserveHdrPresentation(chain.Get());ctx->Flush();Sleep(550);}
        return lsc::HdrProbeSnapshot();
    };
    const auto sdr=sample(5.0f);
    if(sdr.pixels!=256*144||sdr.highlights||!sdr.error.empty()) {printf("SDR failed: %s (%u pixels/%u highlights)\n",sdr.error.c_str(),sdr.pixels,sdr.highlights);return 6;}
    const auto hdr=sample(12.5f);
    if(hdr.highlights!=hdr.pixels||hdr.peakNits<990||hdr.sampleWhiteNits!=400||hdr.sampleDeviceName.empty())return 7;
    control->sample=0;const auto pausedCount=hdr.scans;
    Sleep(550);control->heartbeat=GetTickCount64();lsc::ObserveHdrPresentation(chain.Get());
    if(lsc::HdrProbeSnapshot().scans!=pausedCount||!lsc::HdrProbeSnapshot().enabled)return 10;
    control->enabled=0;const auto count=lsc::HdrProbeSnapshot().scans;
    for(int i=0;i<3;++i) {lsc::ObserveHdrPresentation(chain.Get());Sleep(100);}
    if(lsc::HdrProbeSnapshot().scans!=count)return 8;
    *control={1,1,1,400,GetTickCount64()-5000};lsc::ObserveHdrPresentation(chain.Get());
    if(lsc::HdrProbeSnapshot().enabled)return 9;
    control->enabled=0;DestroyWindow(window);UnmapViewOfFile(control);CloseHandle(map);
    printf("HDR GPU fixture passed: disabled gate, SDR white, HDR highlights, sample identity, latched scan pause, async readback, lease expiry.\n");
    return 0;
}
