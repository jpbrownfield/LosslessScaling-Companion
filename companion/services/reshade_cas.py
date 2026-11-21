"""Bundled ReShade port of AMD's five-tap, sharpen-only FidelityFX CAS path."""

AMD_CAS_PROVIDER = "lsc"
AMD_CAS_EFFECT = "LSC_AMD_CAS.fx"
AMD_CAS_TECHNIQUE = f"LSC_AMD_CAS@{AMD_CAS_EFFECT}"
AMD_CAS_PACKAGE = {
    "id": AMD_CAS_PROVIDER,
    "name": "AMD FidelityFX CAS (bundled)",
    "description": "Five-sample sharpening with SDR, scRGB and HDR10 support",
    "repositoryUrl": "https://github.com/GPUOpen-Effects/FidelityFX-CAS",
    "downloadUrl": "",
    "shaders": [AMD_CAS_EFFECT],
}

AMD_CAS_SOURCE = r'''// AMD FidelityFX CAS, sharpen-only, without CAS_BETTER_DIAGONALS/CAS_SLOW.
// ReShade port: exact sqrt/rcp; five fetches; green-channel adaptive weight.
// Source: https://github.com/GPUOpen-Effects/FidelityFX-CAS/blob/master/ffx-cas/ffx_cas.h
// Copyright (c) 2017-2019 Advanced Micro Devices, Inc. All rights reserved.
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in all
// copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.

#if BUFFER_COLOR_SPACE == 4
    #error "AMD CAS: HLG is not supported; use scRGB or HDR10 output."
#endif

uniform float Sharpness < ui_type = "slider"; ui_min = 0.0; ui_max = 1.0;
    ui_label = "Contrast adaptation"; > = 0.0;
uniform float Strength < ui_type = "slider"; ui_min = 0.0; ui_max = 1.0;
    ui_label = "Sharpening strength"; > = 0.8;
uniform float HdrPeakNits < hidden = true; > = 600.0;

texture CasBackBuffer : COLOR;
sampler CasSampler { Texture = CasBackBuffer; };

void CasVS(uint id : SV_VertexID, out float4 position : SV_Position)
{
    float2 uv = float2((id << 1) & 2, id & 2);
    position = float4(uv * float2(2.0, -2.0) + float2(-1.0, 1.0), 0.0, 1.0);
}

float3 CasDecode(float3 colour)
{
#if BUFFER_COLOR_SPACE == 2 || (BUFFER_COLOR_SPACE == 0 && BUFFER_COLOR_BIT_DEPTH == 16)
    return colour * (80.0 / max(HdrPeakNits, 80.0));
#elif BUFFER_COLOR_SPACE == 3
    float3 p = pow(max(colour, 0.0), 1.0 / 78.84375);
    float3 decoded = pow(max(p - 0.8359375, 0.0) / max(18.8515625 - 18.6875 * p, 1e-6), 1.0 / 0.1593017578125);
    return decoded * (10000.0 / max(HdrPeakNits, 80.0));
#else
    return float3(colour.r <= 0.04045 ? colour.r / 12.92 : pow((colour.r + 0.055) / 1.055, 2.4),
                  colour.g <= 0.04045 ? colour.g / 12.92 : pow((colour.g + 0.055) / 1.055, 2.4),
                  colour.b <= 0.04045 ? colour.b / 12.92 : pow((colour.b + 0.055) / 1.055, 2.4));
#endif
}

float3 CasEncode(float3 colour)
{
#if BUFFER_COLOR_SPACE == 2 || (BUFFER_COLOR_SPACE == 0 && BUFFER_COLOR_BIT_DEPTH == 16)
    return colour * (max(HdrPeakNits, 80.0) / 80.0);
#elif BUFFER_COLOR_SPACE == 3
    float3 p = pow(max(colour * (max(HdrPeakNits, 80.0) / 10000.0), 0.0), 0.1593017578125);
    return pow((0.8359375 + 18.8515625 * p) / (1.0 + 18.6875 * p), 78.84375);
#else
    return float3(colour.r <= 0.0031308 ? colour.r * 12.92 : 1.055 * pow(colour.r, 1.0 / 2.4) - 0.055,
                  colour.g <= 0.0031308 ? colour.g * 12.92 : 1.055 * pow(colour.g, 1.0 / 2.4) - 0.055,
                  colour.b <= 0.0031308 ? colour.b * 12.92 : 1.055 * pow(colour.b, 1.0 / 2.4) - 0.055);
#endif
}

float3 CasLoad(int2 position)
{
    // Integer loads need explicit clamping at the frame edges.
    position = clamp(position, int2(0, 0), int2(BUFFER_WIDTH - 1, BUFFER_HEIGHT - 1));
    return CasDecode(tex2Dfetch(CasSampler, position).rgb);
}

float4 CasPS(float4 position : SV_Position) : SV_Target
{
    int2 pixel = int2(position.xy);
    float4 original = tex2Dfetch(CasSampler, pixel);
    if (Strength <= 0.0) return original;
    float3 raw = CasDecode(original.rgb);
    float3 e = saturate(raw);
    float3 b = saturate(CasLoad(pixel + int2( 0, -1)));
    float3 d = saturate(CasLoad(pixel + int2(-1,  0)));
    float3 f = saturate(CasLoad(pixel + int2( 1,  0)));
    float3 h = saturate(CasLoad(pixel + int2( 0,  1)));
    // AMD's default fast path uses the green coefficient for all three channels.
    float mn = min(min(min(d.g, e.g), min(f.g, b.g)), h.g);
    float mx = max(max(max(d.g, e.g), max(f.g, b.g)), h.g);
    float amplitude = sqrt(saturate(min(mn, 1.0 - mx) / max(mx, 1e-6)));
    float weight = -amplitude / lerp(8.0, 5.0, saturate(Sharpness));
    float3 sharpened = saturate(((b + d + f + h) * weight + e) / (1.0 + 4.0 * weight));
    // Add only the sharpening delta: preserve signed scRGB and values above peak.
    return float4(CasEncode(raw + (sharpened - e) * saturate(Strength)), original.a);
}

technique LSC_AMD_CAS < ui_label = "AMD FidelityFX CAS (5 samples, HDR aware)"; >
{
    pass { VertexShader = CasVS; PixelShader = CasPS; }
}
'''
