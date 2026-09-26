// Model-side shaders, compiled at runtime with D3DCompile (cs_5_0). One root signature, three PSOs.
//   t0..t3  SRVs      u0,u1 UAVs      b0 root constants (12 dwords)      s0 static linear-clamp sampler
//
// Per model run:  CSDown (frame -> proxy)  ->  CSFlowToMvec (LSFG flow -> mvec)  ->  [model]  ->
//                 CSDelta (model - proxy -> the shared work-res delta)
// The delta is applied to LS's presented frames on the D3D11 side (addon/compose11.cpp).
#pragma once

static const char* kNrModelHlsl = R"HLSL(
SamplerState sLin : register(s0);
Texture2D<float4>   tOrig  : register(t0);   // full-res frame
Texture2D<float4>   tProxy : register(t1);   // work-res proxy: the frame as the model saw it
Texture2D<float4>   tNr    : register(t2);   // CSDelta: model output
Texture2D<float4>   tAux   : register(t3);   // CSFlowToMvec: LSFG flow (RGBA16F)
RWTexture2D<float4> uOut   : register(u0);
RWTexture2D<float2> uMv    : register(u1);   // CSFlowToMvec only

cbuffer CB : register(b0) {
    uint2 dstSize;    // size of the texture being written
    uint2 srcSize;    // size of the texture being read
    uint  flags;      // bit0: a flow texture is bound; bit1: source is linear scRGB HDR
    float flowScale;  // CSFlowToMvec: work-res pixels per flow unit
    uint2 pad0; uint4 pad1;
};

float3 LinearToSrgb(float3 c) {
    c = saturate(c);
    return lerp(12.92 * c, 1.055 * pow(c, 1.0 / 2.4) - 0.055, step(0.0031308, c));
}

// Frame -> proxy. Four bilinear taps spread over the work texel's footprint: a plain bilinear fetch aliases past
// a 2x reduction, and aliasing in the model's input becomes noise in its output. DLSSNR remains in its proven
// 8-bit display-referred domain: an scRGB source is clipped to SDR white and encoded to sRGB for the proxy.
[numthreads(8, 8, 1)]
void CSDown(uint3 id : SV_DispatchThreadID) {
    if (id.x >= dstSize.x || id.y >= dstSize.y) return;
    float2 step = 1.0 / float2(dstSize);
    float2 uv = (float2(id.xy) + 0.5) * step;
    float2 q = step * 0.25;
    float4 c = tOrig.SampleLevel(sLin, uv + float2(-q.x, -q.y), 0) + tOrig.SampleLevel(sLin, uv + float2( q.x, -q.y), 0) +
               tOrig.SampleLevel(sLin, uv + float2(-q.x,  q.y), 0) + tOrig.SampleLevel(sLin, uv + float2( q.x,  q.y), 0);
    c *= 0.25;
    if (flags & 2u) c.rgb = LinearToSrgb(c.rgb);
    uOut[id.xy] = c;
}

// LSFG flow (xy = current -> previous frame) -> work-res motion vectors in work-res pixels. Zeros without flow.
[numthreads(8, 8, 1)]
void CSFlowToMvec(uint3 id : SV_DispatchThreadID) {
    if (id.x >= dstSize.x || id.y >= dstSize.y) return;
    float2 uv = (float2(id.xy) + 0.5) / float2(dstSize);
    uMv[id.xy] = (flags & 1u) ? tAux.SampleLevel(sLin, uv, 0).xy * flowScale : float2(0, 0);
}

// Work-res delta = model - proxy (signed, RGBA16F).
[numthreads(8, 8, 1)]
void CSDelta(uint3 id : SV_DispatchThreadID) {
    if (id.x >= dstSize.x || id.y >= dstSize.y) return;
    uOut[id.xy] = float4(tNr[id.xy].rgb - tProxy[id.xy].rgb, 0);
}
)HLSL";
