#include "addon/compose11.h"
#include "addon/bridge.h"
#include "addon/compose_shaders.h"
#include <d3dcompiler.h>
#include <windows.h>
#include <cstdio>
#include <cstdarg>
#include <cstring>

#pragma comment(lib, "d3dcompiler.lib")

struct ComposeCB { uint32_t dstW, dstH; float offset, intensity, maxDelta, hiProtect; uint32_t debugView, flags; float uvPerUnitX, uvPerUnitY, pad0, pad1; };

void Compose11::Log(const char* fmt, ...) { char b[512]; va_list a; va_start(a, fmt); vsnprintf(b, sizeof b, fmt, a); va_end(a); if (m_log) m_log(b); }

bool Compose11::Init(ID3D11Device* dev, LogFn log) {
    Shutdown();
    m_log = log; m_dev = dev; m_dev->AddRef();
    ID3DBlob* cs = nullptr, * err = nullptr;
    if (FAILED(D3DCompile(kComposeHlsl, strlen(kComposeHlsl), "compose11", nullptr, nullptr, "CSCompose", "cs_5_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &cs, &err))) {
        Log("Compose11: HLSL: %s", err ? (char*)err->GetBufferPointer() : "?"); if (err) err->Release(); Shutdown(); return false; }
    HRESULT hr = m_dev->CreateComputeShader(cs->GetBufferPointer(), cs->GetBufferSize(), nullptr, &m_cs); cs->Release();
    if (FAILED(hr)) { Log("Compose11: CreateComputeShader 0x%08x", (unsigned)hr); Shutdown(); return false; }
    D3D11_BUFFER_DESC bd{}; bd.ByteWidth = sizeof(ComposeCB); bd.Usage = D3D11_USAGE_DYNAMIC; bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER; bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    if (FAILED(m_dev->CreateBuffer(&bd, nullptr, &m_cb))) { Log("Compose11: constant buffer"); Shutdown(); return false; }
    D3D11_SAMPLER_DESC sd{}; sd.Filter = D3D11_FILTER_MIN_MAG_LINEAR_MIP_POINT; sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP; sd.MaxLOD = D3D11_FLOAT32_MAX;
    if (FAILED(m_dev->CreateSamplerState(&sd, &m_samp))) { Log("Compose11: sampler"); Shutdown(); return false; }
    m_cpuMs = 0; m_runs = 0;
    Log("Compose11: ready");
    return true;
}

void Compose11::Shutdown() {
    for (auto** p : { (IUnknown**)&m_srcSrv, (IUnknown**)&m_src, (IUnknown**)&m_dstUav, (IUnknown**)&m_dst, (IUnknown**)&m_flowSrv, (IUnknown**)&m_flowRes,
                      (IUnknown**)&m_cs, (IUnknown**)&m_cb, (IUnknown**)&m_samp, (IUnknown**)&m_dev }) { if (*p) (*p)->Release(); *p = nullptr; }
    m_sw = m_sh = 0; m_sfmt = DXGI_FORMAT_UNKNOWN;
}

bool Compose11::EnsureScratch(const D3D11_TEXTURE2D_DESC& td, DXGI_FORMAT vf) {
    if (td.Width == m_sw && td.Height == m_sh && vf == m_sfmt && m_src) return true;
    for (auto** p : { (IUnknown**)&m_srcSrv, (IUnknown**)&m_src, (IUnknown**)&m_dstUav, (IUnknown**)&m_dst }) { if (*p) (*p)->Release(); *p = nullptr; }
    D3D11_TEXTURE2D_DESC d{}; d.Width = td.Width; d.Height = td.Height; d.MipLevels = 1; d.ArraySize = 1; d.Format = td.Format; d.SampleDesc.Count = 1; d.Usage = D3D11_USAGE_DEFAULT;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    D3D11_SHADER_RESOURCE_VIEW_DESC sv{}; sv.Format = vf; sv.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D; sv.Texture2D.MipLevels = 1;
    D3D11_UNORDERED_ACCESS_VIEW_DESC uv{}; uv.Format = vf; uv.ViewDimension = D3D11_UAV_DIMENSION_TEXTURE2D;
    HRESULT hr = m_dev->CreateTexture2D(&d, nullptr, &m_src);
    if (SUCCEEDED(hr)) hr = m_dev->CreateShaderResourceView(m_src, &sv, &m_srcSrv);
    d.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
    if (SUCCEEDED(hr)) hr = m_dev->CreateTexture2D(&d, nullptr, &m_dst);
    if (SUCCEEDED(hr)) hr = m_dev->CreateUnorderedAccessView(m_dst, &uv, &m_dstUav);
    if (FAILED(hr)) { Log("Compose11: scratch %ux%u fmt %d failed 0x%08x", td.Width, td.Height, (int)td.Format, (unsigned)hr); return false; }
    m_sw = td.Width; m_sh = td.Height; m_sfmt = vf;
    snprintf(m_targetInfo, sizeof m_targetInfo, "%ux%u fmt %d%s", td.Width, td.Height, (int)td.Format, (td.BindFlags & D3D11_BIND_UNORDERED_ACCESS) ? " (UAV, in place)" : " (copy back)");
    Log("Compose11: target %s", m_targetInfo);
    return true;
}

ID3D11ShaderResourceView* Compose11::FlowSrv(ID3D11Resource* flow) {
    if (!flow) return nullptr;
    if (flow == m_flowRes && m_flowSrv) return m_flowSrv;
    if (m_flowSrv) m_flowSrv->Release(); if (m_flowRes) m_flowRes->Release(); m_flowSrv = nullptr; m_flowRes = nullptr;
    D3D11_SHADER_RESOURCE_VIEW_DESC sv{}; sv.Format = DXGI_FORMAT_R16G16B16A16_FLOAT; sv.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D; sv.Texture2D.MipLevels = 1;
    if (FAILED(m_dev->CreateShaderResourceView(flow, &sv, &m_flowSrv))) { m_flowSrv = nullptr; return nullptr; }
    m_flowRes = flow; m_flowRes->AddRef();
    return m_flowSrv;
}

bool Compose11::Run(ID3D11DeviceContext* ctx, const Args& a) {
    if (!m_cs || !ctx || !a.target || !a.delta) return false;
    LARGE_INTEGER qf, q0; QueryPerformanceFrequency(&qf); QueryPerformanceCounter(&q0);
    D3D11_TEXTURE2D_DESC td; a.target->GetDesc(&td);
    const DXGI_FORMAT vf = Bridge::ViewFormat(td.Format);
    if (vf == DXGI_FORMAT_UNKNOWN || td.SampleDesc.Count != 1) {   // 8-bit, 10-bit and half-float back buffers; the delta is display-referred either way
        uint64_t key = 0xC000000000000000ull | (uint32_t)td.Format; if (key != m_lastFailKey) { m_lastFailKey = key; Log("Compose11: unsupported present format %d", (int)td.Format); }
        return false;
    }
    if (!EnsureScratch(td, vf)) return false;
    const bool direct = (td.BindFlags & D3D11_BIND_UNORDERED_ACCESS) != 0;
    ID3D11UnorderedAccessView* targetUav = nullptr;
    if (direct) { D3D11_UNORDERED_ACCESS_VIEW_DESC uv{}; uv.Format = vf; uv.ViewDimension = D3D11_UAV_DIMENSION_TEXTURE2D; if (FAILED(m_dev->CreateUnorderedAccessView(a.target, &uv, &targetUav))) targetUav = nullptr; }

    ctx->CopyResource(m_src, a.target);
    ID3D11ShaderResourceView* flowSrv = FlowSrv(a.flow);

    // constants
    ComposeCB cb{}; cb.dstW = td.Width; cb.dstH = td.Height; cb.offset = a.offset; cb.intensity = a.intensity; cb.maxDelta = a.maxDelta; cb.hiProtect = a.hiProtect;
    const bool hdrScRgb = vf == DXGI_FORMAT_R16G16B16A16_FLOAT;
    cb.debugView = a.debugView; cb.flags = (flowSrv ? 1u : 0u) | (a.isGen ? 2u : 0u) | (hdrScRgb ? 4u : 0u);
    const float fu = a.flowUnit > 0.1f ? a.flowUnit : 2.0f;
    cb.uvPerUnitX = (flowSrv && a.flowW) ? 1.0f / (fu * (float)a.flowW) : 0.0f; cb.uvPerUnitY = (flowSrv && a.flowH) ? 1.0f / (fu * (float)a.flowH) : 0.0f;
    D3D11_MAPPED_SUBRESOURCE m{}; if (SUCCEEDED(ctx->Map(m_cb, 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) { memcpy(m.pData, &cb, sizeof cb); ctx->Unmap(m_cb, 0); }

    // save LS's compute state, run, restore
    ID3D11ComputeShader* savedCs = nullptr; ID3D11ShaderResourceView* savedSrv[3] = {}; ID3D11UnorderedAccessView* savedUav = nullptr; ID3D11Buffer* savedCb = nullptr; ID3D11SamplerState* savedSamp = nullptr;
    ctx->CSGetShader(&savedCs, nullptr, nullptr); ctx->CSGetShaderResources(0, 3, savedSrv); ctx->CSGetUnorderedAccessViews(0, 1, &savedUav); ctx->CSGetConstantBuffers(0, 1, &savedCb); ctx->CSGetSamplers(0, 1, &savedSamp);
    ID3D11ShaderResourceView* srvs[3] = { m_srcSrv, a.delta, flowSrv }; ID3D11UnorderedAccessView* uavs[1] = { targetUav ? targetUav : m_dstUav };
    ID3D11ShaderResourceView* nullSrv[3] = {}; ID3D11UnorderedAccessView* nullUav[1] = {};
    ctx->CSSetShader(m_cs, nullptr, 0); ctx->CSSetShaderResources(0, 3, srvs); ctx->CSSetUnorderedAccessViews(0, 1, uavs, nullptr); ctx->CSSetConstantBuffers(0, 1, &m_cb); ctx->CSSetSamplers(0, 1, &m_samp);
    ctx->Dispatch((td.Width + 7) / 8, (td.Height + 7) / 8, 1);
    ctx->CSSetShaderResources(0, 3, nullSrv); ctx->CSSetUnorderedAccessViews(0, 1, nullUav, nullptr);
    ctx->CSSetShader(savedCs, nullptr, 0); ctx->CSSetShaderResources(0, 3, savedSrv); ctx->CSSetUnorderedAccessViews(0, 1, &savedUav, nullptr); ctx->CSSetConstantBuffers(0, 1, &savedCb); ctx->CSSetSamplers(0, 1, &savedSamp);
    if (savedCs) savedCs->Release(); for (auto* s : savedSrv) if (s) s->Release(); if (savedUav) savedUav->Release(); if (savedCb) savedCb->Release(); if (savedSamp) savedSamp->Release();
    if (!targetUav) ctx->CopyResource(a.target, m_dst); else targetUav->Release();

    m_runs++;
    LARGE_INTEGER q1; QueryPerformanceCounter(&q1); double ms = (double)(q1.QuadPart - q0.QuadPart) * 1000.0 / (double)qf.QuadPart; m_cpuMs = m_cpuMs == 0 ? ms : m_cpuMs * 0.9 + ms * 0.1;
    return true;
}
