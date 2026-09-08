#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <d3d11.h>
#include <d3dcompiler.h>

#include <algorithm>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "d3dcompiler.lib")

namespace {
struct Options {
    int width = 1280;
    int height = 720;
    int shader_load = 64;
    int duration_seconds = 0;
    bool vsync = false;
    std::wstring event_log;
};

Options g_options;
bool g_running = true;
unsigned g_flash_index = 0;
ULONGLONG g_flash_until = 0;

template <typename T> void release(T*& value) {
    if (value) value->Release();
    value = nullptr;
}

std::wstring argument_value(int argc, wchar_t** argv, int& index) {
    if (index + 1 >= argc) return L"";
    return argv[++index];
}

Options parse_options() {
    Options result;
    int argc = 0;
    wchar_t** argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) return result;
    for (int i = 1; i < argc; ++i) {
        const std::wstring arg = argv[i];
        if (arg == L"--width") result.width = _wtoi(argument_value(argc, argv, i).c_str());
        else if (arg == L"--height") result.height = _wtoi(argument_value(argc, argv, i).c_str());
        else if (arg == L"--shader-load") result.shader_load = _wtoi(argument_value(argc, argv, i).c_str());
        else if (arg == L"--duration") result.duration_seconds = _wtoi(argument_value(argc, argv, i).c_str());
        else if (arg == L"--event-log") result.event_log = argument_value(argc, argv, i);
        else if (arg == L"--vsync") result.vsync = true;
    }
    LocalFree(argv);
    result.width = std::clamp(result.width, 320, 7680);
    result.height = std::clamp(result.height, 240, 4320);
    result.shader_load = std::clamp(result.shader_load, 16, 4096);
    result.duration_seconds = std::max(0, result.duration_seconds);
    return result;
}

void record_input_event() {
    if (g_options.event_log.empty()) return;
    LARGE_INTEGER counter{}, frequency{};
    QueryPerformanceCounter(&counter);
    QueryPerformanceFrequency(&frequency);
    std::ofstream stream(std::filesystem::path(g_options.event_log), std::ios::app);
    stream << g_flash_index << ',' << counter.QuadPart << ',' << frequency.QuadPart << '\n';
}

LRESULT CALLBACK window_proc(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam) {
    switch (message) {
    case WM_KEYDOWN:
        if (wparam == VK_F15 && !(lparam & (1u << 30))) {
            ++g_flash_index;
            g_flash_until = GetTickCount64() + 120;
            record_input_event();
            return 0;
        }
        if (wparam == VK_ESCAPE) {
            DestroyWindow(hwnd);
            return 0;
        }
        break;
    case WM_CLOSE:
        DestroyWindow(hwnd);
        return 0;
    case WM_DESTROY:
        g_running = false;
        PostQuitMessage(0);
        return 0;
    case WM_SYSCOMMAND:
        if ((wparam & 0xfff0) == SC_SCREENSAVE || (wparam & 0xfff0) == SC_MONITORPOWER) return 0;
        break;
    }
    return DefWindowProcW(hwnd, message, wparam, lparam);
}

const char* shader_source = R"HLSL(
cbuffer BenchmarkConstants : register(b0) {
    float elapsed;
    int shaderLoad;
    float flashActive;
    float flashPhase;
};
struct VSOut { float4 position : SV_Position; float2 uv : TEXCOORD0; };
VSOut vs_main(uint id : SV_VertexID) {
    VSOut o;
    o.uv = float2((id << 1) & 2, id & 2);
    o.position = float4(o.uv * float2(2, -2) + float2(-1, 1), 0, 1);
    return o;
}
float4 ps_main(VSOut input) : SV_Target {
    if (flashActive > 0.5) {
        return flashPhase > 0.5 ? float4(1, 0, 1, 1) : float4(0, 1, 1, 1);
    }
    float2 p = input.uv * 2.0 - 1.0;
    p.x *= 1.7777778;
    float3 z = float3(p * 0.72, 0.35 + 0.08 * sin(elapsed));
    float accumulator = 0.0;
    [loop] for (int i = 0; i < shaderLoad; ++i) {
        float fi = (float)i;
        z = abs(z) / max(dot(z, z), 0.075) - float3(0.72, 0.63, 0.54);
        z.xy = float2(z.x * 0.997 - z.y * 0.077, z.x * 0.077 + z.y * 0.997);
        accumulator += exp(-7.0 * abs(length(z) - (0.82 + 0.07 * sin(fi * 0.11 + elapsed))));
    }
    float glow = accumulator / max(1.0, (float)shaderLoad);
    float rings = 0.5 + 0.5 * cos(12.0 * length(p) - elapsed * 2.0);
    float3 color = float3(glow * 2.2, glow * glow * 3.0, glow * 1.4 + rings * 0.12);
    color *= 0.72 + 0.28 * cos(float3(0.0, 2.1, 4.2) + elapsed * 0.3);
    return float4(saturate(pow(abs(color), 0.75)), 1.0);
}
)HLSL";

struct Constants {
    float elapsed;
    int shader_load;
    float flash_active;
    float flash_phase;
};
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, PWSTR, int show_command) {
    g_options = parse_options();
    WNDCLASSW wc{};
    wc.lpfnWndProc = window_proc;
    wc.hInstance = instance;
    wc.hCursor = LoadCursorW(nullptr, IDC_ARROW);
    wc.lpszClassName = L"LSHelperBenchmarkWindow";
    if (!RegisterClassW(&wc)) return 2;

    RECT rect{0, 0, g_options.width, g_options.height};
    const DWORD style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX;
    AdjustWindowRect(&rect, style, FALSE);
    HWND hwnd = CreateWindowExW(
        0, wc.lpszClassName, L"LS Helper GPU Benchmark - F15 latency marker",
        style, CW_USEDEFAULT, CW_USEDEFAULT, rect.right - rect.left, rect.bottom - rect.top,
        nullptr, nullptr, instance, nullptr);
    if (!hwnd) return 3;

    DXGI_SWAP_CHAIN_DESC swap_desc{};
    swap_desc.BufferDesc.Width = static_cast<UINT>(g_options.width);
    swap_desc.BufferDesc.Height = static_cast<UINT>(g_options.height);
    swap_desc.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    swap_desc.SampleDesc.Count = 1;
    swap_desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    swap_desc.BufferCount = 2;
    swap_desc.OutputWindow = hwnd;
    swap_desc.Windowed = TRUE;
    swap_desc.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;

    ID3D11Device* device = nullptr;
    ID3D11DeviceContext* context = nullptr;
    IDXGISwapChain* swap_chain = nullptr;
    D3D_FEATURE_LEVEL feature_level{};
    const D3D_FEATURE_LEVEL requested[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0};
    HRESULT hr = D3D11CreateDeviceAndSwapChain(
        nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, 0, requested, 2,
        D3D11_SDK_VERSION, &swap_desc, &swap_chain, &device, &feature_level, &context);
    if (FAILED(hr)) return 4;

    ID3D11Texture2D* back_buffer = nullptr;
    ID3D11RenderTargetView* render_target = nullptr;
    swap_chain->GetBuffer(0, __uuidof(ID3D11Texture2D), reinterpret_cast<void**>(&back_buffer));
    device->CreateRenderTargetView(back_buffer, nullptr, &render_target);
    release(back_buffer);

    ID3DBlob* vs_blob = nullptr;
    ID3DBlob* ps_blob = nullptr;
    ID3DBlob* errors = nullptr;
    hr = D3DCompile(shader_source, strlen(shader_source), nullptr, nullptr, nullptr,
                    "vs_main", "vs_5_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &vs_blob, &errors);
    release(errors);
    if (FAILED(hr)) return 5;
    hr = D3DCompile(shader_source, strlen(shader_source), nullptr, nullptr, nullptr,
                    "ps_main", "ps_5_0", D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, &ps_blob, &errors);
    release(errors);
    if (FAILED(hr)) return 6;

    ID3D11VertexShader* vertex_shader = nullptr;
    ID3D11PixelShader* pixel_shader = nullptr;
    ID3D11Buffer* constant_buffer = nullptr;
    device->CreateVertexShader(vs_blob->GetBufferPointer(), vs_blob->GetBufferSize(), nullptr, &vertex_shader);
    device->CreatePixelShader(ps_blob->GetBufferPointer(), ps_blob->GetBufferSize(), nullptr, &pixel_shader);
    D3D11_BUFFER_DESC cb_desc{};
    cb_desc.ByteWidth = sizeof(Constants);
    cb_desc.Usage = D3D11_USAGE_DYNAMIC;
    cb_desc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
    cb_desc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
    device->CreateBuffer(&cb_desc, nullptr, &constant_buffer);
    release(vs_blob);
    release(ps_blob);

    ShowWindow(hwnd, show_command);
    SetForegroundWindow(hwnd);
    const auto started = std::chrono::steady_clock::now();
    MSG message{};
    while (g_running) {
        while (PeekMessageW(&message, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
        const float elapsed = std::chrono::duration<float>(std::chrono::steady_clock::now() - started).count();
        if (g_options.duration_seconds && elapsed >= g_options.duration_seconds) break;
        D3D11_MAPPED_SUBRESOURCE mapped{};
        if (SUCCEEDED(context->Map(constant_buffer, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped))) {
            auto* constants = static_cast<Constants*>(mapped.pData);
            constants->elapsed = elapsed;
            constants->shader_load = g_options.shader_load;
            constants->flash_active = GetTickCount64() < g_flash_until ? 1.0f : 0.0f;
            constants->flash_phase = (g_flash_index & 1u) ? 1.0f : 0.0f;
            context->Unmap(constant_buffer, 0);
        }
        D3D11_VIEWPORT viewport{0, 0, static_cast<float>(g_options.width), static_cast<float>(g_options.height), 0, 1};
        context->RSSetViewports(1, &viewport);
        context->OMSetRenderTargets(1, &render_target, nullptr);
        context->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        context->VSSetShader(vertex_shader, nullptr, 0);
        context->PSSetShader(pixel_shader, nullptr, 0);
        context->PSSetConstantBuffers(0, 1, &constant_buffer);
        context->Draw(3, 0);
        swap_chain->Present(g_options.vsync ? 1 : 0, 0);
    }

    release(constant_buffer);
    release(pixel_shader);
    release(vertex_shader);
    release(render_target);
    release(swap_chain);
    release(context);
    release(device);
    DestroyWindow(hwnd);
    return 0;
}
