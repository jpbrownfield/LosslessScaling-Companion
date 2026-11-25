// WARP integration of the production Before/After path with fixture-tagged
// shaders. No LS bytecode or model is used; runtime verification is test-only.
static void TestCaptureObservation() {
    using Microsoft::WRL::ComPtr;
    eam::HostImpl host;
    ComPtr<ID3D11Device> device;ComPtr<ID3D11DeviceContext> context;
    Expect(SUCCEEDED(D3D11CreateDevice(nullptr,D3D_DRIVER_TYPE_WARP,nullptr,0,nullptr,0,
        D3D11_SDK_VERSION,&device,nullptr,&context)),"Observation WARP device");
    ComPtr<ID3DBlob> code,error;
    const char source[]="[numthreads(8,8,1)] void main() {}";
    Expect(SUCCEEDED(D3DCompile(source,sizeof(source),nullptr,nullptr,nullptr,"main","cs_5_0",0,0,&code,&error)),"Observation shader");
    ComPtr<ID3D11ComputeShader> shader;
    Expect(SUCCEEDED(device->CreateComputeShader(code->GetBufferPointer(),code->GetBufferSize(),nullptr,&shader)),"Observation compute");
    const char* identity="214a8ad496c0ffff58e9739d4be085a450d82eaaf23e2ad21dea8b251872880f";
    eam::ShaderSha256 hash{};
    auto digit=[](char c){return c<='9'?c-'0':c-'a'+10;};
    for(size_t i=0;i<hash.size();++i)hash[i]=uint8_t(digit(identity[i*2])*16+digit(identity[i*2+1]));
    shader->SetPrivateData(eam::kShaderSha256,UINT(hash.size()),hash.data());
    context->CSSetShader(shader.Get(),nullptr,0);
    D3D11_TEXTURE2D_DESC desc{};desc.Width=64;desc.Height=64;desc.MipLevels=desc.ArraySize=1;
    desc.Format=DXGI_FORMAT_R8G8B8A8_UNORM;desc.SampleDesc.Count=1;desc.BindFlags=D3D11_BIND_UNORDERED_ACCESS;
    ComPtr<ID3D11Texture2D> texture;ComPtr<ID3D11UnorderedAccessView> view;
    Expect(SUCCEEDED(device->CreateTexture2D(&desc,nullptr,&texture)),"Observation texture");
    Expect(SUCCEEDED(device->CreateUnorderedAccessView(texture.Get(),nullptr,&view)),"Observation UAV");
    auto* raw=view.Get();context->CSSetUnorderedAccessViews(0,1,&raw,nullptr);
    RealFrameHook::VerifyCaptureFixture();RealFrameHook::SetSettings(1,1,0,false);
    auto dispatch=[&](bool executed=true){RealFrameHook::Before(&host,context.Get(),8,8,1);
        if(executed)context->Dispatch(8,8,1);RealFrameHook::After(&host,context.Get(),8,8,1,executed);};
    RealFrameHook::EnableCaptureObservation(false);dispatch();
    Expect(RealFrameHook::GetDiagnostics().captures==0,"Unrequested monitoring does no capture work");
    RealFrameHook::EnableCaptureObservation(true);dispatch(false);
    Expect(RealFrameHook::GetDiagnostics().captures==0,"Skipped dispatch is not source intake");
    dispatch();dispatch();
    auto observed=RealFrameHook::GetDiagnostics();
    Expect(!host.HasRealFrameCallback()&&observed.captures==2,"Source intake works without NR");
    Expect(observed.captureAdapterObserved&&!observed.adapterLuid.empty(),"Actual capture adapter verified");
    Expect(observed.trace.counts["handoffAttempts"]==0,"Observation never invokes a transform");
    ComPtr<ID3D11UnorderedAccessView> unchanged;context->CSGetUnorderedAccessViews(0,1,&unchanged);
    Expect(unchanged==view,"Observation preserves resource bindings");
    shader->SetPrivateData(eam::kShaderSha256,0,nullptr);dispatch();
    Expect(RealFrameHook::GetDiagnostics().captures==2,"Untagged compute is not intake");
    shader->SetPrivateData(eam::kShaderSha256,UINT(hash.size()),hash.data());
    RealFrameHook::SetSettings(1,1,0,false);dispatch();
    Expect(RealFrameHook::GetDiagnostics().captures==1,"Settings generation resets intake and metadata");
    RealFrameHook::SetSettings(1,0,0,false);dispatch();
    Expect(RealFrameHook::GetDiagnostics().captures==1,"Known capture observation does not inherit NR capture-API restrictions");
    RealFrameHook::EnableCaptureObservation(false);dispatch();
    Expect(RealFrameHook::GetDiagnostics().captures==1,"Monitoring off stops independent capture collection");
    RealFrameHook::Shutdown();
    std::cout<<"Capture observation passed (NR off, adapter identity, skipped/untagged dispatch, reset, no handoff).\n";
}
