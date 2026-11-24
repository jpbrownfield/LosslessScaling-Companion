// SPDX-License-Identifier: GPL-3.0-only
// Included by the pinned addon's status test. No NVIDIA model or hardware needed.
    {
        Microsoft::WRL::ComPtr<ID3D11Device> device;
        Microsoft::WRL::ComPtr<ID3D11DeviceContext> captureContext;
        check("recovery WARP device",SUCCEEDED(D3D11CreateDevice(nullptr,D3D_DRIVER_TYPE_WARP,
            nullptr,0,nullptr,0,D3D11_SDK_VERSION,&device,nullptr,&captureContext)));
        D3D11_TEXTURE2D_DESC desc{};
        desc.Width=desc.Height=32;desc.MipLevels=desc.ArraySize=1;
        desc.Format=DXGI_FORMAT_R8G8B8A8_UNORM;desc.SampleDesc.Count=1;
        desc.BindFlags=D3D11_BIND_SHADER_RESOURCE|D3D11_BIND_UNORDERED_ACCESS;
        Microsoft::WRL::ComPtr<ID3D11Texture2D> original,output;
        check("recovery original texture",device&&SUCCEEDED(device->CreateTexture2D(&desc,nullptr,&original)));
        check("recovery output texture",device&&SUCCEEDED(device->CreateTexture2D(&desc,nullptr,&output)));
        EamRealFrame frame{};frame.context=captureContext.Get();frame.original=original.Get();frame.output=output.Get();
        frame.width=frame.height=32;frame.encoding=EamFrameEncoding::Sdr709;
        for(bool blank:{false,true}) {
            host.blankModel=blank;host.support=EamFrameSupport::Ready;
            init(&host,context,reinterpret_cast<void*>(alloc),reinterpret_cast<void*>(free),data);
            auto invoke=[&]{std::lock_guard<std::mutex> lock(host.callbackMutex);return host.callback(frame,host.user);};
            auto result=std::async(std::launch::async,invoke);
            const bool returned=result.wait_for(std::chrono::seconds(2))==std::future_status::ready;
            check("missing runtime releases render callback",returned);
            if(returned) {
                check("missing runtime preserves original capture",!result.get());
                // The host records CallbackFailed after the false callback result.
                host.support=EamFrameSupport::CallbackFailed;
                check("model error takes priority over callback support",until([&]{return host.Has("nvngx_dlssnr.dll was not found:",3);}));
                check("original-frame fallback is explicit",host.Has("Original frames are continuing",3));
                check("blank model uses executable default",!blank||host.Has("nvngx_dlssnr.dll.",3));
                const auto before=GetTickCount64();
                check("failed pipeline does not retry every capture",!invoke()&&GetTickCount64()-before<250);
                captureContext->Flush();
            }
            shutdown();
            if(!returned)check("shutdown releases failed model callback",!result.get());
        }
    }
