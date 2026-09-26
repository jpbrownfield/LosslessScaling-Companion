#include <d3dcompiler.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include "engine/nr_shaders.h"
#include "addon/compose_shaders.h"

#pragma comment(lib, "d3dcompiler.lib")

static bool Compile(const char* source, const char* name, const char* entry) {
    ID3DBlob* code = nullptr;
    ID3DBlob* errors = nullptr;
    const HRESULT hr = D3DCompile(source, std::strlen(source), name, nullptr, nullptr, entry, "cs_5_0",
                                  D3DCOMPILE_ENABLE_STRICTNESS | D3DCOMPILE_WARNINGS_ARE_ERRORS,
                                  0, &code, &errors);
    if (FAILED(hr)) {
        std::fprintf(stderr, "%s/%s failed (0x%08lx): %s\n", name, entry,
                     static_cast<unsigned long>(hr),
                     errors ? static_cast<const char*>(errors->GetBufferPointer()) : "no diagnostics");
    }
    if (errors) errors->Release();
    if (code) code->Release();
    return SUCCEEDED(hr);
}

static float Clamp01(float v) { return std::max(0.0f, std::min(1.0f, v)); }
static float LinearToSrgb(float v) {
    v = Clamp01(v);
    return v < 0.0031308f ? 12.92f * v : 1.055f * std::pow(v, 1.0f / 2.4f) - 0.055f;
}
static float SrgbToLinear(float v) {
    v = Clamp01(v);
    return v < 0.04045f ? v / 12.92f : std::pow((v + 0.055f) / 1.055f, 2.4f);
}
static float ComposeHdr(float source, float delta) {
    const float proxy = LinearToSrgb(source);
    return source + SrgbToLinear(proxy + delta) - SrgbToLinear(proxy);
}
static bool Near(float a, float b, float epsilon = 0.0001f) { return std::fabs(a - b) <= epsilon; }

int main() {
    bool ok = true;
    for (const char* entry : { "CSDown", "CSFlowToMvec", "CSDelta" })
        ok = Compile(kNrModelHlsl, "nr_shaders", entry) && ok;
    ok = Compile(kComposeHlsl, "compose11", "CSCompose") && ok;

    // Zero model delta must be identity across SDR, HDR highlights, and negative
    // scRGB gamut excursions. This catches accidental final-output saturation.
    for (float source : { -0.25f, 0.0f, 0.18f, 1.0f, 2.0f, 7.5f }) {
        const float result = ComposeHdr(source, 0.0f);
        if (!Near(result, source)) {
            std::fprintf(stderr, "HDR identity failed: %.6f became %.6f\n", source, result);
            ok = false;
        }
    }

    // A model edit in the SDR region must be converted back to linear light, not
    // added as though an sRGB delta were already linear.
    const float edited = ComposeHdr(0.18f, 0.05f);
    if (!(edited > 0.18f && edited < 0.30f)) {
        std::fprintf(stderr, "HDR SDR-region edit is out of bounds: %.6f\n", edited);
        ok = false;
    }

    // A positive edit cannot clip an HDR source merely because its model proxy is
    // already at SDR white.
    if (!Near(ComposeHdr(4.0f, 0.25f), 4.0f)) {
        std::fprintf(stderr, "HDR highlight was clipped or shifted\n");
        ok = false;
    }

    std::printf("shader compilation and HDR composition policy: %s\n", ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}
