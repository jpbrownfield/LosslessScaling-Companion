// SPDX-License-Identifier: MIT
#pragma once
#include <windows.h>
#include <d3d11_4.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <wrl/client.h>
#include <array>
#include <string>
#include <functional>
#include "eam/real_frame.h"
#include "engine/flow_estimator.h"
#include "dlss5nr.h"

namespace amd_nr {
using Microsoft::WRL::ComPtr;
inline const char* BackendForVendor(unsigned vendor) {
    return vendor==0x10de ? "NVIDIA" : vendor==0x1002 ? "AMD" : "unsupported";
}
struct Settings {
    std::wstring runtimeDir, snippet, dataDir, library;
    dlss5nr_settings model{};
};
struct RuntimeApi {
    HMODULE module=nullptr;
    decltype(&dlss5nr_create) create=nullptr;
    decltype(&dlss5nr_destroy) destroy=nullptr;
    decltype(&dlss5nr_default_settings) defaults=nullptr;
    decltype(&dlss5nr_set_settings) settings=nullptr;
    decltype(&dlss5nr_process) process=nullptr;
    decltype(&dlss5nr_present) present=nullptr;
    decltype(&dlss5nr_get_status) status=nullptr;
    int (*selectedOrdinal)()=nullptr;
    uint64_t (*selectedLuid)()=nullptr;
    void Load(const std::wstring& path,bool requireAdapterSelection=false);
};
class Pipeline {
public:
    explicit Pipeline(bool softwareProbe=false):softwareProbe_(softwareProbe){}
    // Process lifetime in LS: the addon is pinned. Explicit probe destruction
    // waits for its own GPU work and never runs under the Windows loader lock.
    ~Pipeline();
    bool Process(const EamRealFrame&, const Settings&);
    const dlss5nr_status& Status() const { return status_; }
    int ComputeOrdinal() const { return api_.selectedOrdinal?api_.selectedOrdinal():-1; }
    double waitMs=0, flowMs=0;
    uint64_t frames=0;
private:
    void Initialize(ID3D11Device*, const Settings&);
    void Resize(unsigned w,unsigned h,DXGI_FORMAT format);
    void Wait(uint64_t value);
    ComPtr<ID3D11Device5> device11_;
    ComPtr<ID3D11DeviceContext4> context11_;
    ComPtr<ID3D12Device> device_;
    ComPtr<ID3D12CommandQueue> queue_;
    ComPtr<ID3D12Fence> done_,capture_;
    ComPtr<ID3D11Fence> done11_,capture11_;
    ComPtr<ID3D12Resource> input_,output_,motion_,distrust_;
    ComPtr<ID3D11Texture2D> input11_,output11_;
    struct Slot { ComPtr<ID3D12CommandAllocator> allocator; ComPtr<ID3D12GraphicsCommandList> list; uint64_t fence=0; };
    std::array<Slot,4> slots_;
    FlowEstimator flow_;
    RuntimeApi api_;
    dlss5nr_context* runtime_=nullptr;
    dlss5nr_status status_{};
    Settings settings_;
    HANDLE event_=nullptr;
    uint64_t signal_=0,epoch_=0,sequence_=0;
    unsigned width_=0,height_=0;
    DXGI_FORMAT format_=DXGI_FORMAT_UNKNOWN;
    bool first_=true;
    bool softwareProbe_=false;
};
void Check(HRESULT result,const char* operation);
void VerifyModel(const std::wstring& path);
// Hardware-independent D3D12 compute-flow test; no model/runtime inference.
std::string ProbeFlow(int adapterIndex,bool warp);
}
