// Included in namespace RealFrameHook. Observation is independent of transforms:
// no model, texture allocation/copy, resource replacement or GPU synchronization.
namespace {
std::atomic<ULONGLONG> observationUntil{0};
struct Observation {
    bool pending=false;
    uint64_t config=0, metadataConfig=0;
    D3D11_TEXTURE2D_DESC desc{};
    ComPtr<ID3D11Device> device, metadataDevice;
    UINT metadataWidth=0,metadataHeight=0;
    DXGI_FORMAT metadataFormat=DXGI_FORMAT_UNKNOWN;
};
thread_local Observation observation;

bool ObserveIntake(ID3D11DeviceContext* ctx,ID3D11Texture2D* texture,uint64_t config) {
    if(config!=settings.load()||!texture)return false;
    auto& o=observation;
    texture->GetDesc(&o.desc);
    if(!o.desc.Width||!o.desc.Height||o.desc.SampleDesc.Count!=1||o.desc.ArraySize!=1)return false;
    ctx->GetDevice(o.device.ReleaseAndGetAddressOf());
    if(o.metadataDevice!=o.device||o.metadataConfig!=config||o.metadataWidth!=o.desc.Width||
       o.metadataHeight!=o.desc.Height||o.metadataFormat!=o.desc.Format) {
        // Metadata work is bounded to a device/settings/texture change. A reader
        // must never make the render thread wait; retry on a later capture.
        std::unique_lock lock(diagnosticMutex,std::try_to_lock);
        if(!lock.owns_lock())return false;
        ComPtr<IDXGIDevice> dxgi;ComPtr<IDXGIAdapter> adapter;DXGI_ADAPTER_DESC ad{};
        if(FAILED(o.device.As(&dxgi))||FAILED(dxgi->GetAdapter(&adapter))||FAILED(adapter->GetDesc(&ad)))return false;
        char name[512]{},luid[32]{};
        WideCharToMultiByte(CP_UTF8,0,ad.Description,-1,name,sizeof name,nullptr,nullptr);
        snprintf(luid,sizeof luid,"%08X:%08X",unsigned(ad.AdapterLuid.HighPart),unsigned(ad.AdapterLuid.LowPart));
        diagnostic.adapter=name;diagnostic.adapterLuid=luid;diagnostic.captureAdapterObserved=true;
        diagnostic.width=o.desc.Width;diagnostic.height=o.desc.Height;diagnostic.format=o.desc.Format;
        o.metadataDevice=o.device;o.metadataConfig=config;o.metadataWidth=o.desc.Width;
        o.metadataHeight=o.desc.Height;o.metadataFormat=o.desc.Format;
    }
    o.config=config;o.pending=true;return true;
}
}
void EnableCaptureObservation(bool enabled) {
    // Renewed by a bound controller, so stopping/crashing Companion expires the
    // extra dispatch observation without requiring an explicit shutdown message.
    observationUntil.store(enabled?GetTickCount64()+5000:0,std::memory_order_relaxed);
}
bool CaptureObservationEnabled() {
    const auto until=observationUntil.load(std::memory_order_relaxed);
    return until&&GetTickCount64()<until;
}
#ifdef LSC_CAPTURE_OBSERVER_TEST
void VerifyCaptureFixture(){runtimeVerified=true;runtimeChecked=true;}
#endif

void Before(eam::HostImpl* host,ID3D11DeviceContext* ctx,UINT x,UINT y,UINT z) {
    state.candidate.Reset();state.savedView.Reset();state.replace=false;observation.pending=false;
    ++dispatches;lastDispatch=GetTickCount64();
    const bool callback=host->HasRealFrameCallback(),monitor=CaptureObservationEnabled();
    if(!callback&&!monitor)return;
    captureTrace.Count(callback?CaptureStep::WithCallback:CaptureStep::NoCallback);
    const auto config=settings.load();
    const unsigned fg=config&255,capture=(config>>8)&255,scaler=(config>>16)&255;
    const auto problem=EamProfileSupport(runtimeVerified,config!=0,fg,capture,scaler);
    const bool transform=callback&&problem==EamFrameSupport::Ready;
    if(callback&&!transform){captureTrace.Count(CaptureStep::ProfileWait);support=problem;}
    // NR's backend/profile restrictions are not observation restrictions. Only
    // a trusted runtime and positively identified input shader may be observed.
    if(!runtimeVerified||!config||(!transform&&!monitor))return;
    ComPtr<ID3D11ComputeShader> cs;ctx->CSGetShader(&cs,nullptr,nullptr);eam::ShaderSha256 sha{};
    if(!eam::ReadShaderSha256(cs.Get(),sha)){captureTrace.Count(CaptureStep::MissingIdentity);return;}
    if(transform)captureTrace.Sample(sha,x,y,z);
    if(fg==1&&Match(sha,kCapture)) {
        captureTrace.Count(CaptureStep::CaptureMatched);
        ComPtr<ID3D11UnorderedAccessView> output;ctx->CSGetUnorderedAccessViews(0,1,&output);
        auto t=Texture(output.Get());
        if(!t){if(transform)PauseUnsupported(host,EamFrameSupport::UnsupportedTexture,"LS capture output texture is missing");return;}
        D3D11_TEXTURE2D_DESC d{};t->GetDesc(&d);
        if(x!=(d.Width+7)/8||y!=(d.Height+7)/8||z!=1) {
            if(transform)PauseUnsupported(host,EamFrameSupport::UnsupportedTexture,"LS capture dispatch/texture shape differs from the supported path");return;
        }
        ObserveIntake(ctx,t.Get(),config);
        if(transform) {
            if(d.MiscFlags!=0){PauseUnsupported(host,EamFrameSupport::UnsupportedTexture,"LS capture texture flags differ from the supported NR path");return;}
            state.candidate=t;state.candidateSettings=config;
        }
    } else if(fg==0&&(scaler==0?Match(sha,kCopy):(Match(sha,kNis)||Match(sha,kNisHdr)||Match(sha,kNis256)||Match(sha,kNisHdr256)))) {
        captureTrace.Count(CaptureStep::ScalerMatched);
        ComPtr<ID3D11ShaderResourceView> view;ctx->CSGetShaderResources(0,1,&view);
        auto t=Texture(view.Get());
        if(!t){if(transform)PauseUnsupported(host,EamFrameSupport::UnsupportedTexture,"LS scaler input texture is missing");return;}
        D3D11_SHADER_RESOURCE_VIEW_DESC v{};view->GetDesc(&v);
        if(v.ViewDimension!=D3D11_SRV_DIMENSION_TEXTURE2D||v.Texture2D.MostDetailedMip!=0) {
            if(transform)PauseUnsupported(host,EamFrameSupport::UnsupportedTexture,"LS scaler input view is not a mip-zero 2D texture");return;
        }
        ObserveIntake(ctx,t.Get(),config);
        if(transform&&Transform(host,ctx,t.Get(),config)) {
            state.savedView=view;
            auto* replacement=state.transformedView.Get();ctx->CSSetShaderResources(0,1,&replacement);state.replace=true;
        }
    } else captureTrace.Count(CaptureStep::UnmatchedShader);
}
void After(eam::HostImpl* host,ID3D11DeviceContext* ctx,UINT,UINT,UINT,bool executed) {
    if(state.replace){auto* view=state.savedView.Get();ctx->CSSetShaderResources(0,1,&view);state.replace=false;}
    if(observation.pending) {
        if(executed&&observation.config==settings.load()){++captures;lastCapture=GetTickCount64();}
        observation.pending=false;
    }
    if(!state.candidate)return;
    auto candidate=state.candidate;state.candidate.Reset();
    if(!executed){captureTrace.Count(CaptureStep::CaptureSkipped);return;}
    ComPtr<ID3D11UnorderedAccessView> uav;ctx->CSGetUnorderedAccessViews(0,1,&uav);
    ID3D11UnorderedAccessView* empty=nullptr;ctx->CSSetUnorderedAccessViews(0,1,&empty,nullptr);
    if(Transform(host,ctx,candidate.Get(),state.candidateSettings))ctx->CopyResource(candidate.Get(),state.transformed.Get());
    auto* restore=uav.Get();ctx->CSSetUnorderedAccessViews(0,1,&restore,nullptr);
}
