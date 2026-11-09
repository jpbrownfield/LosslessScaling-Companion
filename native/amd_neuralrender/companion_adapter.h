// SPDX-License-Identifier: MIT
#pragma once
#include <cstdint>
#include <string>
#include <atomic>

namespace lsc_amd {
inline std::atomic<int> selectedOrdinal{-1};
inline std::atomic<uint64_t> selectedLuid{0};
struct AdapterSelection { int device=-1, ordinal=-1; };

// DXGI/LS and CUDA ordinals are unrelated. Translate the capture device's
// stable Windows LUID into the actual CUDA device token before cuCtxCreate.
template<class Count,class Device,class DeviceLuid>
bool SelectAdapter(uint64_t target,Count countDevices,Device getDevice,DeviceLuid getLuid,
                   AdapterSelection& out,std::string& error) {
    out={};
    if(!target){error="The capture device has no usable adapter LUID";return false;}
    int count=0;
    if(countDevices(&count)!=0||count<1){error="HIP exposes no compute devices";return false;}
    for(int ordinal=0;ordinal<count;++ordinal) {
        int device=-1;uint64_t luid=0;unsigned mask=0;
        if(getDevice(&device,ordinal)!=0)continue;
        if(getLuid(reinterpret_cast<char*>(&luid),&mask,device)!=0)continue;
        if(luid==target) {out.device=device;out.ordinal=ordinal;return true;}
    }
    error="No HIP device matches the capture adapter LUID; check that GPU's Windows HIP/driver support";
    return false;
}
}
