#pragma once

// Kept in a header so the CI shader checker can compile the exact shader used by the addon.
static const char* kComposeHlsl = R"HLSL(
SamplerState sLin : register(s0);
Texture2D<float4>   tSrc   : register(t0);
Texture2D<float4>   tDelta : register(t1);
Texture2D<float4>   tFlow  : register(t2);
RWTexture2D<float4> uOut   : register(u0);

cbuffer CB : register(b0) {
    uint2  dstSize;
    float  offset;
    float  intensity;
    float  maxDelta;
    float  hiProtect;
    uint   debugView;
    uint   flags;        // bit0 flow bound, bit1 generated frame, bit2 linear scRGB HDR target
    float2 uvPerUnit;
    float2 pad;
};
static const float3 kLuma = float3(0.299, 0.587, 0.114);

float3 LinearToSrgb(float3 c) {
    c = saturate(c);
    return lerp(12.92 * c, 1.055 * pow(c, 1.0 / 2.4) - 0.055, step(0.0031308, c));
}
float3 SrgbToLinear(float3 c) {
    c = saturate(c);
    return lerp(c / 12.92, pow((c + 0.055) / 1.055, 2.4), step(0.04045, c));
}

[numthreads(8, 8, 1)]
void CSCompose(uint3 id : SV_DispatchThreadID) {
    if (id.x >= dstSize.x || id.y >= dstSize.y) return;
    float4 o = tSrc[id.xy];
    float2 uv = (float2(id.xy) + 0.5) / float2(dstSize);
    float4 fl = (flags & 1u) ? tFlow.SampleLevel(sLin, uv, 0) : float4(0, 0, 0, 0);
    float2 suv = offset > 0.0 ? uv - offset * fl.zw * uvPerUnit : uv + offset * fl.xy * uvPerUnit;
    float3 d = tDelta.SampleLevel(sLin, suv, 0).rgb;
    float sourceLuma = dot(o.rgb, kLuma);
    float wh = hiProtect < 0.999 ? 1.0 - smoothstep(hiProtect, 1.0, sourceLuma) : 1.0;
    d = clamp(d * intensity, -maxDelta, maxDelta) * wh;
    float3 r;
    if (flags & 4u) {
        // DLSSNR sees an SDR/sRGB proxy. Convert its edit to a linear-light residual
        // and add it to the untouched scRGB source without clamping HDR or gamut.
        float3 proxy = LinearToSrgb(o.rgb);
        float3 enhanced = saturate(proxy + d);
        r = o.rgb + (SrgbToLinear(enhanced) - SrgbToLinear(proxy));
    } else {
        r = saturate(o.rgb + d);
    }
    if      (debugView == 1) r = o.rgb;
    else if (debugView == 2) r = saturate(0.5 + d * 4.0);
    else if (debugView == 3) r = saturate(r + ((flags & 2u) ? float3(0.15, 0, 0) : float3(0, 0.15, 0)));
    else if (debugView == 4) r = saturate(float3(0.5 + fl.xy / 16.0, 0.5));
    uOut[id.xy] = float4(r, o.a);
}
)HLSL";
