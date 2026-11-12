// SPDX-License-Identifier: MIT
#pragma once
#include <windows.h>
#include <cstdint>
#include <string>
struct IDXGISwapChain;
namespace lsc {
struct HdrProbeControl { uint32_t version, enabled, sample; float whiteNits; uint64_t heartbeat; };
static_assert(sizeof(HdrProbeControl)==24);
struct HdrProbeStatus {
    bool enabled=false, ready=false;
    uint64_t scans=0, sampledTick=0, presentTick=0;
    uint32_t pixels=0, highlights=0, gamut=0, colorSpace=0, format=0;
    float peakNits=0, sampleWhiteNits=0;
    std::string deviceName, sampleDeviceName, error;
};
void StartHdrProbe();
bool IsHdrProbeDispatch();
HdrProbeStatus HdrProbeSnapshot();
// Also used by the off-screen GPU fixture. Production calls only from Present.
void ObserveHdrPresentation(IDXGISwapChain* chain);
}
