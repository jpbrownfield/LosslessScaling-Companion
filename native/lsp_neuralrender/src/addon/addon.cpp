// LSP-NeuralRender — LosslessProxy addon host.
//
// Where it sits in Lossless Scaling's pipeline (all on LS's render thread, all on the display GPU):
//
//   capture k ─┬─ LSFG pyramid pass on frame k            <- TAP: copy frame k (+ LSFG flow) to the model's input,
//              │                                             start a model run on it (skipped if the previous run
//              │                                             is still on the GPU). LS's frame is never touched.
//              ├─ LSFG flow, interpolation composes ...
//              └─ Present (generated a, generated b, real k) <- PRESENT HOOK: add the newest finished delta to the
//                                                             frame about to be shown, moved by LSFG's own flow to
//                                                             where that content sits in this frame.
//
// LS's queue never waits for the model, so the model can only ever be late for a frame, never slow LS down.
// The delta for frame k is normally finished before frame k is shown; until then the previous delta is warped
// forward. Settings live in an ImGui panel inside LosslessProxy's Addon Manager.
#include <lsproxy/addon_sdk.h>
#include "imgui.h"
#include "engine/nr_engine.h"
#include "addon/bridge.h"
#include "addon/frame_tap.h"
#include "addon/dispatch_hook.h"
#include "addon/present_hook.h"
#include "addon/compose11.h"
#include <d3d11.h>
#include <dxgi.h>
#include <windows.h>
#include <atomic>
#include <mutex>
#include <thread>
#include <string>
#include <vector>
#include <algorithm>
#include <cstdio>
#include <cstdarg>

static const char* kAddonId = "LSP-NeuralRender";

// ------------------------------------------------------------------ state
struct Config {
    bool enabled = true;
    NrParams p;
    int tapMode = 0;          // 0 auto, 1 manual
    int frameSlot = -1;       // -1 auto (highest large SRV slot)
    std::string tickSig, tapSig;
    float watchdogMs = 80.0f;
    std::string snippetPath;  // empty = <LS dir>\nvngx_dlssnr.dll
    bool lsFirst = true;      // raise LS's GPU thread priority so its work pre-empts the model on the shared GPU
};
static IHost* g_host = nullptr;
static Config g_cfg; static std::mutex g_cfgMu;
static NrEngine g_engine; static Bridge g_bridge; static FrameTap g_tap; static Compose11 g_compose;
static std::atomic<bool> g_armed{ false }, g_killed{ false }, g_engineStarting{ false }, g_requestReset{ false };
static std::string g_status = "waiting for device", g_killReason; static std::mutex g_statusMu;
static ID3D11Device* g_dev = nullptr; static ID3D11DeviceContext* g_ctx = nullptr;
static LUID g_luid{}; static std::string g_adapterName; static bool g_hasDisplay = false; static bool g_engineLuidValid = false; static LUID g_engineLuid{};
static std::thread g_initThread;
static std::wstring g_lsDir, g_addonDir;
static FILE* g_logFile = nullptr; static std::mutex g_logMu;
static uint64_t g_nrRuns = 0; static double g_lastNrMs = 0, g_avgNrMs = 0, g_lastTotalMs = 0; static uint32_t g_watchdogHits = 0;
static std::string g_frameInfo;
// Everything below the dispatch/present hooks runs under g_tapMu: the tap, the compose, bridge/engine teardown, device bookkeeping.
static std::mutex g_tapMu;
static thread_local bool t_ownWork = false;         // our own compose dispatch is on LS's context: the dispatch hook must ignore it
struct SeenDevice { ID3D11Device* dev; bool ok; LUID luid; };
static std::vector<SeenDevice> g_seenDevs;          // devices that have dispatched, classified once each (not AddRef'd; reset on device events)
static ID3D11Device* g_tapDev = nullptr;            // the device the bridge is bound to
static LUID g_tapLuid{}; static bool g_tapLuidValid = false;   // adapter of the device that issues the LSFG tap; the engine follows it
static LUID g_pendingLuid{}; static uint32_t g_pendingTaps = 0; // taps seen on an adapter other than the engine's (switch after kSwitchTaps)
static const uint32_t kSwitchTaps = 20;
static LUID g_failedLuid{}; static bool g_failedLuidValid = false; // adapter the engine last failed on (no automatic retry there)
static bool SameLuid(const LUID& a, const LUID& b) { return a.LowPart == b.LowPart && a.HighPart == b.HighPart; }
static uint64_t g_otherDispatches = 0; static int g_hookCount = 0; static std::string g_tapDevInfo = "none yet";
// present side
static uint64_t g_presents = 0, g_lsPresents = 0, g_composed = 0; static IDXGISwapChain* g_lastSwap = nullptr; static bool g_lastSwapIsLs = false;
static uint64_t g_lastDelta = 0; static double g_lastOffset = 0; static double g_lastTarget = 0;
static uint64_t g_presentStage[8] = {};   // diagnostics: how far each present gets

static void SetStatus(const char* s) { std::lock_guard<std::mutex> lk(g_statusMu); g_status = s; }
static std::string GetStatus() { std::lock_guard<std::mutex> lk(g_statusMu); return g_status; }

static void Log(const char* fmt, ...) {
    char b[1024]; va_list a; va_start(a, fmt); vsnprintf(b, sizeof b, fmt, a); va_end(a);
    std::lock_guard<std::mutex> lk(g_logMu);
    if (g_host) g_host->Log(LSPROXY_LOG_INFO, b);
    if (g_logFile) { fprintf(g_logFile, "%s\n", b); fflush(g_logFile); }
}
static void Kill(const char* why) { g_killed = true; { std::lock_guard<std::mutex> lk(g_statusMu); g_killReason = why; } Log("DISABLED: %s", why); }

// ------------------------------------------------------------------ config persistence
static std::string CfgGet(const char* k, const char* def) { return g_host ? g_host->GetConfig(kAddonId, k, def) : def; }
static void CfgSet(const char* k, const std::string& v) { if (g_host) g_host->SetConfig(kAddonId, k, v.c_str()); }
static float CfgGetF(const char* k, float d) { std::string s = CfgGet(k, ""); return s.empty() ? d : (float)atof(s.c_str()); }
static int CfgGetI(const char* k, int d) { std::string s = CfgGet(k, ""); return s.empty() ? d : atoi(s.c_str()); }
static void CfgSetF(const char* k, float v) { char b[32]; snprintf(b, 32, "%g", v); CfgSet(k, b); }
static void CfgSetI(const char* k, int v) { CfgSet(k, std::to_string(v)); }

static void LoadConfig() {
    std::lock_guard<std::mutex> lk(g_cfgMu); Config& c = g_cfg;
    c.enabled = CfgGetI("enabled", 1) != 0;
    c.p.style = CfgGetI("style", 0); c.p.useAutoMask = CfgGetI("autoMask", 1);
    c.p.intensity = CfgGetF("intensity", 1.0f); c.p.localStructure = CfgGetF("localStructure", 1.0f); c.p.localTone = CfgGetF("localTone", 1.0f); c.p.skinStructure = CfgGetF("skinStructure", -1.0f);
    c.p.useFlow = CfgGetI("useFlow", 1) != 0; c.p.flowUnit = CfgGetF("flowUnit", 2.0f);
    c.p.workingScale = CfgGetF("workingScale", 0.35f); c.p.composeIntensity = CfgGetF("composeIntensity", 1.0f); c.p.maxDelta = CfgGetF("maxDelta", 0.5f);
    c.p.hiProtect = CfgGetF("hiProtect", 0.85f); c.p.debugView = CfgGetI("debugView", 0);
    if (c.p.debugView > 4) c.p.debugView = 0;
    c.lsFirst = CfgGetI("lsFirst", 1) != 0;
    c.tapMode = CfgGetI("tapMode", 0); c.frameSlot = CfgGetI("frameSlot", -1); c.tickSig = CfgGet("tickSig", ""); c.tapSig = CfgGet("tapSig", "");
    c.watchdogMs = CfgGetF("watchdogMs", 80.0f); c.snippetPath = CfgGet("snippetPath", "");
}
static void SaveConfig() {
    Config c; { std::lock_guard<std::mutex> lk(g_cfgMu); c = g_cfg; }
    CfgSetI("enabled", c.enabled); CfgSetI("style", c.p.style); CfgSetI("autoMask", c.p.useAutoMask);
    CfgSetF("intensity", c.p.intensity); CfgSetF("localStructure", c.p.localStructure); CfgSetF("localTone", c.p.localTone); CfgSetF("skinStructure", c.p.skinStructure);
    CfgSetI("useFlow", c.p.useFlow); CfgSetF("flowUnit", c.p.flowUnit);
    CfgSetF("workingScale", c.p.workingScale); CfgSetF("composeIntensity", c.p.composeIntensity); CfgSetF("maxDelta", c.p.maxDelta);
    CfgSetF("hiProtect", c.p.hiProtect); CfgSetI("debugView", c.p.debugView);
    CfgSetI("lsFirst", c.lsFirst);
    CfgSetI("tapMode", c.tapMode); CfgSetI("frameSlot", c.frameSlot); CfgSet("tickSig", c.tickSig); CfgSet("tapSig", c.tapSig);
    CfgSetF("watchdogMs", c.watchdogMs); CfgSet("snippetPath", c.snippetPath);
    if (g_host) g_host->SaveConfig();
}
static void ApplyTapRoles() {
    Config c; { std::lock_guard<std::mutex> lk(g_cfgMu); c = g_cfg; }
    DispatchSig tick, tap; tick.Parse(c.tickSig); tap.Parse(c.tapSig);
    g_tap.SetRoles(tick, tap, c.tapMode == 1 ? FrameTap::Manual : FrameTap::Auto, c.frameSlot);
}

// ------------------------------------------------------------------ engine lifecycle
static std::wstring Narrow2Wide(const std::string& s) { std::wstring w(s.size(), L' '); int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), &w[0], (int)w.size()); w.resize(n > 0 ? n : 0); return w; }
static std::string Wide2Narrow(const std::wstring& w) { std::string s(w.size() * 3, ' '); int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), &s[0], (int)s.size(), nullptr, nullptr); s.resize(n > 0 ? n : 0); return s; }

static void StartEngine(LUID luid) {
    if (g_engineStarting.exchange(true)) return;
    if (g_initThread.joinable()) g_initThread.join();
    g_initThread = std::thread([luid]() {
        { std::lock_guard<std::mutex> lk(g_tapMu); g_bridge.Shutdown(); if (g_engine.IsReady() || g_engine.IsFailed()) g_engine.Shutdown(); }
        std::string sp; { std::lock_guard<std::mutex> lk(g_cfgMu); sp = g_cfg.snippetPath; }
        std::wstring snippet = sp.empty() ? g_lsDir + L"\\nvngx_dlssnr.dll" : Narrow2Wide(sp);
        SetStatus("engine: loading model...");
        bool ok = g_engine.Init(luid, g_addonDir + L"\\" LSPNR_FORWARDER_FILENAME, snippet, g_addonDir, g_lsDir, [](const char* m) { Log("%s", m); });
        g_engineLuid = luid; g_engineLuidValid = true;
        if (!ok) { g_failedLuid = luid; g_failedLuidValid = true; Log("engine failed on LUID %08x:%08x; it will start again when LS runs LSFG on another NVIDIA adapter", luid.HighPart, luid.LowPart); }
        SetStatus(ok ? "engine ready" : g_engine.Stats().lastError);
        g_engineStarting = false;
    });
}

static void DropDevice() {   // under g_tapMu
    g_bridge.Shutdown(); g_compose.Shutdown(); g_tap.Reset(); g_seenDevs.clear(); g_tapDev = nullptr; g_lastSwap = nullptr; g_lastSwapIsLs = false;
}
static void OnDeviceEvent(uint32_t id, const void*, uint32_t, void*) {
    if (id == LSPROXY_EVENT_D3D11_DEVICE_CHANGED) {
        std::lock_guard<std::mutex> lk(g_tapMu);
        DropDevice(); g_dev = nullptr; g_ctx = nullptr; return;
    }
    // DEVICE_READY
    g_dev = (ID3D11Device*)g_host->GetD3D11Device(); g_ctx = (ID3D11DeviceContext*)g_host->GetD3D11DeviceContext();
    { std::lock_guard<std::mutex> lk(g_tapMu); DropDevice(); }
    g_hasDisplay = false; g_adapterName = "?";
    if (!g_dev) return;
    IDXGIDevice* dx = nullptr;
    if (SUCCEEDED(g_dev->QueryInterface(IID_PPV_ARGS(&dx))) && dx) {
        IDXGIAdapter* ad = nullptr;
        if (SUCCEEDED(dx->GetAdapter(&ad)) && ad) {
            DXGI_ADAPTER_DESC d; ad->GetDesc(&d); g_luid = d.AdapterLuid; g_adapterName = Wide2Narrow(d.Description);
            IDXGIOutput* o = nullptr; g_hasDisplay = SUCCEEDED(ad->EnumOutputs(0, &o)) && o; if (o) o->Release();
            bool nvidia = (d.VendorId == 0x10DE);
            g_armed = nvidia;
            Log("device %p on '%s' LUID %08x:%08x display=%d -> %s", g_dev, g_adapterName.c_str(), d.AdapterLuid.HighPart, d.AdapterLuid.LowPart, g_hasDisplay, nvidia ? "NVIDIA, ok" : "not NVIDIA, ignored");
            if (!nvidia) SetStatus("waiting: LS device is not an NVIDIA adapter");
            else if (!g_engine.IsReady()) SetStatus("waiting for LSFG dispatches");
            ad->Release();
        }
        dx->Release();
    }
    // The engine is started from the tap, on the adapter of the device that actually runs LSFG (single GPU,
    // hybrid laptop, or either card of a dual-GPU rig) — not from device events, which fire for every device LS makes.
}

// ------------------------------------------------------------------ the tap (LS render thread)
static const char* FmtName(uint32_t f) {
    switch (f) { case 87: return "BGRA8"; case 91: return "BGRA8s"; case 28: return "RGBA8"; case 29: return "RGBA8s"; case 24: return "RGB10A2"; case 10: return "RGBA16F"; case 34: return "RG16F"; case 16: return "RG32F"; case 49: return "RG8"; case 41: return "R32F"; case 54: return "R16F"; case 61: return "R8"; case 26: return "R11G11B10F"; case 2: return "RGBA32F"; default: return "?"; }
}

static void OnPresent(IDXGISwapChain* sc, void* user);
static void ReleaseDecision(TapDecision& d) { if (d.frame) d.frame->Release(); if (d.flow) d.flow->Release(); d.frame = nullptr; d.flow = nullptr; }
static bool TapBody(ID3D11DeviceContext* ctx, uint32_t x, uint32_t y, uint32_t z) {
    TapDecision d; bool run = g_tap.Observe(ctx, x, y, z, d);
    if (!run) { ReleaseDecision(d); return false; }
    if (!g_tapDev) { ReleaseDecision(d); return false; }
    LUID want{}; for (auto& s : g_seenDevs) if (s.dev == g_tapDev) want = s.luid;
    g_tapLuid = want; g_tapLuidValid = true;
    bool engineMatches = g_engine.IsReady() && g_engineLuidValid && SameLuid(g_engineLuid, want);
    if (!engineMatches) {
        ReleaseDecision(d);
        if (g_engineStarting) return false;
        if (g_engine.IsFailed() && g_failedLuidValid && SameLuid(g_failedLuid, want)) return false;   // this adapter can't run NGX; wait for LS to move
        // Hysteresis: LS creates devices on every adapter and may run this pass on more than one for a moment.
        // Only follow an adapter that keeps issuing the tap while the engine's adapter stays silent.
        bool firstStart = !g_engineLuidValid;
        if (!firstStart) { if (!SameLuid(g_pendingLuid, want)) { g_pendingLuid = want; g_pendingTaps = 0; } if (++g_pendingTaps < kSwitchTaps) return false; }
        g_pendingTaps = 0;
        Log("engine follows the LSFG device: starting on LUID %08x:%08x", want.HighPart, want.LowPart); StartEngine(want);
        return false;   // table keeps filling while the model loads
    }
    g_pendingTaps = 0;
    if (!g_bridge.IsReady() && !g_bridge.Init(g_tapDev, ctx, &g_engine, [](const char* m) { Log("%s", m); })) { Kill("bridge init failed"); ReleaseDecision(d); return false; }
    if (!g_compose.IsReady() && !g_compose.Init(g_tapDev, [](const char* m) { Log("%s", m); })) { Kill("compose init failed"); ReleaseDecision(d); return false; }
    if (!PresentHook::Installed() && !PresentHook::Install(g_tapDev, OnPresent, nullptr, [](const char* m) { Log("%s", m); })) { Kill("could not hook dxgi Present"); ReleaseDecision(d); return false; }
    D3D11_TEXTURE2D_DESC td; d.frame->GetDesc(&td);
    { char b[96]; snprintf(b, sizeof b, "%ux%u %s slot %d", td.Width, td.Height, FmtName(td.Format), d.frameSlot); std::lock_guard<std::mutex> lk(g_statusMu); g_frameInfo = b; }
    if (!g_bridge.Ensure(td.Width, td.Height, td.Format)) { SetStatus("unsupported frame format"); ReleaseDecision(d); return false; }
    NrParams p; float wd; bool lsFirst; { std::lock_guard<std::mutex> lk(g_cfgMu); p = g_cfg.p; wd = g_cfg.watchdogMs; lsFirst = g_cfg.lsFirst; }
    g_bridge.SetLsGpuPriority(lsFirst ? 7 : 0);
    bool reset = g_requestReset.exchange(false);
    ID3D11ShaderResourceView* saved[8] = {}; ctx->CSGetShaderResources(0, 8, saved);
    ID3D11ShaderResourceView* nulls[8] = {}; ctx->CSSetShaderResources(0, 8, nulls);
    bool started = g_bridge.Submit(d.frame, d.flow, d.flowW, d.flowH, p, reset, g_tap.Taps());
    ctx->CSSetShaderResources(0, 8, saved); for (auto* s : saved) if (s) s->Release();
    ReleaseDecision(d);
    const NrStats& st = g_engine.Stats();
    if (started) { g_nrRuns++; g_lastNrMs = st.nrMs; g_lastTotalMs = st.totalMs; g_avgNrMs = g_avgNrMs == 0 ? st.nrMs : g_avgNrMs * 0.95 + st.nrMs * 0.05; }
    if (g_engine.IsFailed()) Kill(st.lastError);
    if (st.nrMs > wd) { if (++g_watchdogHits >= 30) Kill("NR slower than watchdog threshold for 30 frames"); } else g_watchdogHits = 0;
    if (g_nrRuns == 1) SetStatus("running");
    const uint64_t taps = g_tap.Taps();
    if (taps == 1 || taps == 60 || taps % 300 == 0)
        Log("tap #%llu: model %.1f ms (avg %.1f), run %.1f ms, GPU start +%.1f done +%.1f ms after submit, tap CPU %.2f ms, interval %.1f ms, runs %llu skipped %llu, fails %llu | presents %llu (%s), composed %llu, compose CPU %.2f ms, last delta frame %llu offset %.2f",
            (unsigned long long)taps, st.nrMs, g_avgNrMs, st.totalMs, st.startMs, st.doneMs, g_bridge.CpuMs(), g_bridge.IntervalMs(), (unsigned long long)g_bridge.Runs(), (unsigned long long)g_bridge.Skipped(), (unsigned long long)st.fails,
            (unsigned long long)g_lsPresents, g_tap.PresentPattern(), (unsigned long long)g_composed, g_compose.CpuMs(), (unsigned long long)g_lastDelta, g_lastOffset);
    if (taps == 60) PresentHook::DumpState([](const char* m) { Log("%s", m); });
    if (taps == 60) Log("present stages: hook hits %u, body %llu, ready %llu, noted %llu, targeted %llu, with delta %llu", PresentHook::Hits(), (unsigned long long)g_presentStage[0], (unsigned long long)g_presentStage[1], (unsigned long long)g_presentStage[2], (unsigned long long)g_presentStage[3], (unsigned long long)g_presentStage[4]);
    return false;   // never skip LS's own dispatch
}
static int SehFilter(unsigned code, const char* where) { char b[64]; snprintf(b, sizeof b, "exception 0x%08x in %s", code, where); Kill(b); return EXCEPTION_EXECUTE_HANDLER; }
static bool TapGuarded(ID3D11DeviceContext* ctx, uint32_t x, uint32_t y, uint32_t z) {
    __try { return TapBody(ctx, x, y, z); } __except (SehFilter(GetExceptionCode(), "tap")) { return false; }
}
// The inline hook sees every device in the process (LS creates several, on both GPUs). Classify each
// device once: only the one on the armed adapter (NVIDIA, drives the display) is tapped. Runs under g_tapMu.
static bool DeviceUsable(ID3D11DeviceContext* ctx) {
    ID3D11Device* dev = nullptr; ctx->GetDevice(&dev); if (!dev) return false;
    for (auto& s : g_seenDevs) if (s.dev == dev) { dev->Release(); return s.ok; }
    bool ok = false; std::string name = "?"; LUID luid{};
    IDXGIDevice* dx = nullptr;
    if (SUCCEEDED(dev->QueryInterface(IID_PPV_ARGS(&dx))) && dx) {
        IDXGIAdapter* ad = nullptr;
        if (SUCCEEDED(dx->GetAdapter(&ad)) && ad) {
            DXGI_ADAPTER_DESC d; ad->GetDesc(&d); luid = d.AdapterLuid; name = Wide2Narrow(d.Description);
            ok = (d.VendorId == 0x10DE);
            ad->Release();
        }
        dx->Release();
    }
    if (g_seenDevs.size() < 32) g_seenDevs.push_back({ dev, ok, luid });
    if (ok && g_tapDev != dev) { g_bridge.Shutdown(); g_compose.Shutdown(); g_tap.Reset(); g_tapDev = dev; g_lastSwap = nullptr; g_lastSwapIsLs = false; }
    char b[192]; snprintf(b, sizeof b, "%p on %s (LUID %08x) -> %s", (void*)dev, name.c_str(), (unsigned)luid.LowPart, ok ? "TAPPED" : "ignored");
    if (ok) { std::lock_guard<std::mutex> lk(g_statusMu); g_tapDevInfo = b; }
    Log("dispatching device %s", b);
    dev->Release(); return ok;
}
static void ShapeStr(const DispatchSig& s, char* out, size_t n);
// Every few seconds, if new dispatch shapes appeared, write the whole table to the log so the
// fingerprints are available even without the panel. Runs under g_tapMu.
static void MaybeDumpTable() {
    static uint64_t lastQpc = 0; static size_t lastSize = 0; static LARGE_INTEGER freq = {};
    if (!freq.QuadPart) QueryPerformanceFrequency(&freq);
    LARGE_INTEGER q; QueryPerformanceCounter(&q);
    if ((uint64_t)q.QuadPart - lastQpc < (uint64_t)freq.QuadPart * 5) return;
    lastQpc = q.QuadPart;
    auto rows = g_tap.Snapshot(); if (rows.size() == lastSize) return; lastSize = rows.size();
    std::sort(rows.begin(), rows.end(), [](const DispatchEntry& a, const DispatchEntry& b) { return a.count > b.count; });
    DispatchSig tick, tap; g_tap.GetRoles(tick, tap);
    Log("--- dispatch table: %zu shapes, %llu dispatches, ticks %llu, taps %llu, roles %s/%s ---", rows.size(), (unsigned long long)g_tap.Dispatches(), (unsigned long long)g_tap.Ticks(), (unsigned long long)g_tap.Taps(), tick.Empty() ? "no-tick" : "tick", tap.Empty() ? "no-tap" : "tap");
    char b[512]; int n = 0;
    for (auto& e : rows) { if (++n > 40) break; ShapeStr(e.sig, b, sizeof b); Log("  %6u x (%u,%u,%u) %s%s", e.count, e.sig.x, e.sig.y, e.sig.z, b, e.roleAuto == 1 ? " [auto TICK]" : e.roleAuto == 2 ? " [auto TAP]" : ""); }
}
static bool OnDispatch(ID3D11DeviceContext* ctx, UINT x, UINT y, UINT z, void*) {
    if (t_ownWork || g_killed || g_engineStarting || !ctx) return false;
    { std::lock_guard<std::mutex> lk(g_cfgMu); if (!g_cfg.enabled) return false; }
    std::lock_guard<std::mutex> lk(g_tapMu);
    if (!DeviceUsable(ctx)) { g_otherDispatches++; return false; }
    bool r = TapGuarded(ctx, x, y, z);
    MaybeDumpTable();
    return r;
}

// ------------------------------------------------------------------ the present hook (LS render thread)
// Every Present of LS's output swap chain: add the newest finished delta to the frame about to be shown.
static void PresentBody(IDXGISwapChain* sc) {
    g_presents++; g_presentStage[0]++;
    if (!g_tapDev || !g_bridge.IsReady() || !g_compose.IsReady()) return;
    g_presentStage[1]++;
    // LS's output swap chain lives on the tapped device (the proxy's own windows, if any, do not)
    bool isLs;
    if (sc == g_lastSwap) isLs = g_lastSwapIsLs;
    else { ID3D11Device* dev = nullptr; sc->GetDevice(IID_PPV_ARGS(&dev)); isLs = (dev == g_tapDev); if (dev) dev->Release(); g_lastSwap = sc; g_lastSwapIsLs = isLs; Log("present: swap chain %p on %s device", (void*)sc, isLs ? "the tapped" : "another"); }
    if (!isLs) return;
    g_lsPresents++;
    PresentInfo pi = g_tap.NotePresent();
    g_presentStage[2]++;
    if (pi.target < 0) return;
    g_presentStage[3]++;
    ID3D11ShaderResourceView* dsrv = nullptr; uint32_t ww = 0, wh = 0;
    const uint64_t d = g_bridge.NewestDelta(&dsrv, &ww, &wh);
    if (!d) return;
    g_presentStage[4]++;
    NrParams p; { std::lock_guard<std::mutex> lk(g_cfgMu); p = g_cfg.p; }
    ID3D11Texture2D* bb = nullptr;
    if (FAILED(sc->GetBuffer(0, IID_PPV_ARGS(&bb))) || !bb) return;
    uint32_t fw = 0, fh = 0; ID3D11Resource* flow = p.useFlow ? g_tap.NewestFlow(fw, fh) : nullptr;
    Compose11::Args a; a.target = bb; a.delta = dsrv; a.flow = flow; a.flowW = fw; a.flowH = fh; a.flowUnit = p.flowUnit;
    a.offset = (float)(pi.target - (double)d); a.intensity = p.composeIntensity; a.maxDelta = p.maxDelta; a.hiProtect = p.hiProtect; a.debugView = p.debugView; a.isGen = pi.gen;
    g_bridge.BeginDeltaUse(d);
    t_ownWork = true; bool ok = g_compose.Run(g_bridge.Context(), a); t_ownWork = false;
    g_bridge.EndDeltaUse(d);
    if (ok) { g_composed++; g_lastDelta = d; g_lastOffset = a.offset; g_lastTarget = pi.target; }
    if (flow) flow->Release(); bb->Release();
}
static void PresentGuarded(IDXGISwapChain* sc) {
    __try { PresentBody(sc); } __except (SehFilter(GetExceptionCode(), "present")) { t_ownWork = false; }
}
static void OnPresent(IDXGISwapChain* sc, void*) {
    if (g_killed || g_engineStarting || !sc) return;
    { std::lock_guard<std::mutex> lk(g_cfgMu); if (!g_cfg.enabled) return; }
    std::lock_guard<std::mutex> lk(g_tapMu);
    PresentGuarded(sc);
}

// ------------------------------------------------------------------ panel
static void ShapeStr(const DispatchSig& s, char* out, size_t n) {
    int k = 0; for (int i = 0; i < 8; ++i) if (s.srv[i].valid) k += snprintf(out + k, n - k, "S%d:%ux%u %s ", i, s.srv[i].w, s.srv[i].h, FmtName(s.srv[i].fmt));
    for (int i = 0; i < 4; ++i) if (s.uav[i].valid) k += snprintf(out + k, n - k, "U%d:%ux%u %s ", i, s.uav[i].w, s.uav[i].h, FmtName(s.uav[i].fmt));
}

LSPROXY_EXPORT void AddonRenderSettings() {
    Config c; { std::lock_guard<std::mutex> lk(g_cfgMu); c = g_cfg; }
    bool changed = false, createChanged = false, tapChanged = false;

    // status
    std::string status = GetStatus(), frameInfo; { std::lock_guard<std::mutex> lk(g_statusMu); frameInfo = g_frameInfo; }
    if (g_killed) { ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(1, 0.35f, 0.35f, 1)); ImGui::Text("DISABLED: %s", g_killReason.c_str()); ImGui::PopStyleColor(); ImGui::SameLine(); if (ImGui::SmallButton("Re-arm")) { g_killed = false; g_watchdogHits = 0; } }
    else { ImGui::PushStyleColor(ImGuiCol_Text, g_nrRuns ? ImVec4(0.3f, 0.9f, 0.3f, 1) : ImVec4(0.9f, 0.8f, 0.3f, 1)); ImGui::Text("%s", status.c_str()); ImGui::PopStyleColor(); }
    ImGui::Text("last LS device: %s %s   engine: %s", g_adapterName.c_str(), g_hasDisplay ? "(drives a display)" : "(no display output)", g_engineLuidValid ? "on the LSFG device" : "not started");
    ImGui::Text("frame: %s   NR %.1f ms (avg %.1f)  run %.1f ms   runs %llu   fails %llu", frameInfo.c_str(), g_lastNrMs, g_avgNrMs, g_lastTotalMs, (unsigned long long)g_nrRuns, (unsigned long long)g_engine.Stats().fails);
    ImGui::Text("dispatches %llu  ticks %llu  taps %llu  gate:%s  float-slot %d", (unsigned long long)g_tap.Dispatches(), (unsigned long long)g_tap.Ticks(), (unsigned long long)g_tap.Taps(), g_tap.GateName(), g_engine.Stats().floatSlot);
    { std::string tdi; { std::lock_guard<std::mutex> lk(g_statusMu); tdi = g_tapDevInfo; }
      ImGui::Text("d3d11 hook: %d entry points   other-adapter dispatches: %llu   tapped device: %s", g_hookCount, (unsigned long long)g_otherDispatches, tdi.c_str()); }
    if (g_engine.IsFailed()) { ImGui::TextColored(ImVec4(1, 0.4f, 0.4f, 1), "engine: %s", g_engine.Stats().lastError); ImGui::SameLine(); if (ImGui::SmallButton("Retry engine")) { g_engineLuidValid = false; if (g_tapLuidValid) StartEngine(g_tapLuid); } }
    ImGui::Separator();

    changed |= ImGui::Checkbox("Enable Neural Rendering", &c.enabled);
    ImGui::SameLine(); if (ImGui::SmallButton("Reset history")) g_requestReset = true;

    if (ImGui::CollapsingHeader("Model", ImGuiTreeNodeFlags_DefaultOpen)) {
        // Read by the model at every evaluate: changes apply on the next frame. Ranges are what the model honours
        // (docs/dlssnr-knobs.md): intensity clamps at 1, the local strengths do not clamp at all.
        int style = (int)c.p.style; const char* styles[] = { "Standard", "Natural", "Cinematic" };
        if (ImGui::Combo("Style", &style, styles, 3)) { c.p.style = style; changed = true; }
        changed |= ImGui::SliderFloat("Intensity", &c.p.intensity, 0.0f, 1.0f, "%.2f");
        changed |= ImGui::SliderFloat("Local structure", &c.p.localStructure, -2.0f, 5.0f, "%.2f");
        changed |= ImGui::SliderFloat("Local tone", &c.p.localTone, -2.0f, 5.0f, "%.2f");
        changed |= ImGui::SliderFloat("Skin structure", &c.p.skinStructure, -1.0f, 3.0f, c.p.skinStructure <= -0.99f ? "follow local structure" : "%.2f");
        bool am = c.p.useAutoMask != 0;
        if (ImGui::Checkbox("Auto skin mask", &am)) { c.p.useAutoMask = am; changed = true; }
        changed |= ImGui::Checkbox("LSFG optical flow (model motion vectors, and moves the delta onto generated frames)", &c.p.useFlow);
        { const NrStats& fs = g_engine.Stats(); ImGui::SameLine(); if (fs.hasFlow) ImGui::TextDisabled("(flow %ux%u)", fs.flowW, fs.flowH); else ImGui::TextDisabled("(no flow texture seen yet)"); }
    }
    if (ImGui::CollapsingHeader("Pipeline", ImGuiTreeNodeFlags_DefaultOpen)) {
        // The model costs ~10 ms + ~7 ms per megapixel on Ampere. The working scale is the only cost lever: past the
        // frame interval the model simply skips frames and the present side carries the last delta forward.
        int samplingPercent = (int)(c.p.workingScale * 100.0f + 0.5f);
        if (ImGui::SliderInt("Sampling resolution", &samplingPercent, 25, 100, "%d%%")) {
            c.p.workingScale = samplingPercent / 100.0f; createChanged = true;
        }
        {
            const NrStats& st = g_engine.Stats();
            if (g_bridge.Width()) {
                float mp = (float)st.workW * (float)st.workH / 1e6f; float est = 10.0f + 7.0f * mp; double iv = g_bridge.IntervalMs();
                uint64_t runs = g_bridge.Runs(), skipped = g_bridge.Skipped(), seen = runs + skipped;
                ImGui::Text("frame %ux%u -> model input %ux%u (%.2f MP): model %.1f ms (avg %.1f), estimate %.0f ms, frame interval %.1f ms", g_bridge.Width(), g_bridge.Height(), st.workW, st.workH, mp, st.nrMs, g_avgNrMs, est, iv);
                ImGui::Text("model keeps up with %llu of %llu frames (%.0f%%); GPU start +%.1f / done +%.1f ms after submit; tap CPU %.2f ms", (unsigned long long)runs, (unsigned long long)seen, seen ? 100.0 * runs / seen : 0.0, st.startMs, st.doneMs, g_bridge.CpuMs());
                ImGui::Text("presents: %llu on LS's swap chain (%s), composed %llu, compose CPU %.2f ms, target %s; newest delta = frame %llu, applied at offset %.2f frames",
                    (unsigned long long)g_lsPresents, g_tap.PresentPattern(), (unsigned long long)g_composed, g_compose.CpuMs(), g_compose.TargetInfo(), (unsigned long long)g_lastDelta, g_lastOffset);
                if (seen > 30 && runs * 2 < seen) ImGui::TextColored(ImVec4(1, 0.6f, 0.3f, 1), "the model runs on fewer than half of the frames: the delta is carried across frames by the flow. Lower the working scale for a fresher result.");
            } else ImGui::TextDisabled("(no frame tapped yet)");
        }
        changed |= ImGui::Checkbox("LS's GPU work first (its device gets GPU thread priority +7, the model yields to it)", &c.lsFirst);
        changed |= ImGui::SliderFloat("Apply strength", &c.p.composeIntensity, 0.0f, 2.0f);
        changed |= ImGui::SliderFloat("Max delta", &c.p.maxDelta, 0.05f, 1.0f);
        changed |= ImGui::SliderFloat("Protect highlights above", &c.p.hiProtect, 0.5f, 1.0f, c.p.hiProtect >= 0.999f ? "off" : "%.2f");
        int dv = (int)c.p.debugView; const char* views[] = { "Result", "Original", "Delta x4", "Frame role (green real, red generated)", "LSFG flow" };
        if (ImGui::Combo("Debug view", &dv, views, 5)) { c.p.debugView = dv; changed = true; }
    }
    if (ImGui::CollapsingHeader("Tap (which LS dispatch is the new real frame)")) {
        int mode = c.tapMode; if (ImGui::RadioButton("Auto", mode == 0)) { mode = 0; } ImGui::SameLine(); if (ImGui::RadioButton("Manual", mode == 1)) { mode = 1; }
        if (mode != c.tapMode) { c.tapMode = mode; tapChanged = true; }
        int fs = c.frameSlot + 1; const char* slots[] = { "auto (highest)", "S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7" };
        if (ImGui::Combo("Frame slot", &fs, slots, 9)) { c.frameSlot = fs - 1; tapChanged = true; }
        DispatchSig tick, tap; g_tap.GetRoles(tick, tap); char b[512];
        ShapeStr(tick, b, sizeof b); ImGui::TextWrapped("TICK: %s  (%u,%u,%u) %s", tick.Empty() ? "-" : "", tick.x, tick.y, tick.z, b);
        ShapeStr(tap, b, sizeof b);  ImGui::TextWrapped("TAP : %s  (%u,%u,%u) %s", tap.Empty() ? "-" : "", tap.x, tap.y, tap.z, b);
        if (ImGui::SmallButton("Clear table")) g_tap.ClearTable(); ImGui::SameLine();
        if (ImGui::SmallButton("Clear roles")) { c.tickSig.clear(); c.tapSig.clear(); tapChanged = true; }
        auto rows = g_tap.Snapshot(); std::sort(rows.begin(), rows.end(), [](const DispatchEntry& a, const DispatchEntry& b) { return a.count > b.count; });
        if (ImGui::BeginTable("disp", 5, ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingStretchProp)) {
            ImGui::TableSetupColumn("count"); ImGui::TableSetupColumn("groups"); ImGui::TableSetupColumn("views"); ImGui::TableSetupColumn("auto"); ImGui::TableSetupColumn("use"); ImGui::TableHeadersRow();
            int n = 0;
            for (auto& e : rows) {
                if (++n > 24) break;
                ImGui::TableNextRow(); ImGui::PushID((int)(e.key & 0x7fffffff));
                ImGui::TableNextColumn(); ImGui::Text("%u", e.count);
                ImGui::TableNextColumn(); ImGui::Text("%u,%u,%u", e.sig.x, e.sig.y, e.sig.z);
                ImGui::TableNextColumn(); ShapeStr(e.sig, b, sizeof b); ImGui::TextWrapped("%s", b);
                ImGui::TableNextColumn(); ImGui::Text("%s", e.roleAuto == 1 ? "TICK" : e.roleAuto == 2 ? "TAP" : "");
                ImGui::TableNextColumn();
                if (ImGui::SmallButton("TAP")) { c.tapSig = e.sig.Serialize(); c.tapMode = 1; tapChanged = true; } ImGui::SameLine();
                if (ImGui::SmallButton("TICK")) { c.tickSig = e.sig.Serialize(); c.tapMode = 1; tapChanged = true; }
                ImGui::PopID();
            }
            ImGui::EndTable();
        }
    }
    if (ImGui::CollapsingHeader("Advanced")) {
        changed |= ImGui::SliderFloat("Watchdog (ms)", &c.watchdogMs, 20.0f, 200.0f, "%.0f");
        char sp[512]; strncpy(sp, c.snippetPath.c_str(), sizeof sp); sp[sizeof sp - 1] = 0;
        if (ImGui::InputText("Snippet path (blank = LS folder)", sp, sizeof sp)) { c.snippetPath = sp; changed = true; }
        if (ImGui::SmallButton("Restart engine")) { g_engineLuidValid = false; g_killed = false; if (g_tapLuidValid) StartEngine(g_tapLuid); }
        ImGui::SameLine();
        if (ImGui::SmallButton("Dump flow probe (3 frames)")) g_tap.ArmProbe(g_lsDir);
        { std::string ps = g_tap.ProbeStatus(); if (ps != "idle") ImGui::TextWrapped("%s", ps.c_str()); }
        ImGui::TextDisabled("present hook: %s, %u presents seen in the process   log: <LS folder>\\LSP_NeuralRender.log", PresentHook::Installed() ? "installed" : "not yet", PresentHook::Hits());
    }

    if (changed || createChanged || tapChanged) {
        { std::lock_guard<std::mutex> lk(g_cfgMu); g_cfg = c; }
        SaveConfig();
        if (tapChanged) ApplyTapRoles();
        if (createChanged) g_requestReset = true;   // the engine re-creates the feature on the next tap (CreateKeysEqual)
    }
}

// ------------------------------------------------------------------ exports
static void AddonInitializeBody(IHost* host, ImGuiContext* ctx, void* allocFunc, void* freeFunc, void* userData) {
    ImGui::SetCurrentContext(ctx);
    ImGui::SetAllocatorFunctions((ImGuiMemAllocFunc)allocFunc, (ImGuiMemFreeFunc)freeFunc, userData);
    g_host = host;
    wchar_t exe[MAX_PATH]; GetModuleFileNameW(nullptr, exe, MAX_PATH); *wcsrchr(exe, L'\\') = 0; g_lsDir = exe;
    HMODULE self = nullptr; GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT, (LPCWSTR)&AddonInitializeBody, &self);
    wchar_t mod[MAX_PATH]; GetModuleFileNameW(self, mod, MAX_PATH); *wcsrchr(mod, L'\\') = 0; g_addonDir = mod;
    g_logFile = _wfopen((g_lsDir + L"\\LSP_NeuralRender.log").c_str(), L"w");
    LoadConfig(); ApplyTapRoles();
    host->SubscribeEvent(LSPROXY_EVENT_D3D11_DEVICE_READY, OnDeviceEvent, nullptr);
    host->SubscribeEvent(LSPROXY_EVENT_D3D11_DEVICE_CHANGED, OnDeviceEvent, nullptr);
    Log("LSP-NeuralRender initialised (host version 0x%x), addon dir %ls", host->GetHostVersion(), g_addonDir.c_str());
    // Own inline hook on d3d11.dll instead of the host's vtable patch (see dispatch_hook.h for why). The Present hook
    // needs a device to find dxgi's entry points; it is installed at the first tap.
    g_hookCount = DispatchHook::Install(OnDispatch, nullptr, [](const char* m) { Log("%s", m); });
    if (g_hookCount <= 0) Kill("could not hook d3d11 Dispatch");
    // No manual DEVICE_READY replay here: the host's last device pointer may already be destroyed
    // (LS creates and drops devices constantly); we only touch devices inside the event or from a live context.
}
static int InitFilter(EXCEPTION_POINTERS* ep) {
    void* addr = ep && ep->ExceptionRecord ? ep->ExceptionRecord->ExceptionAddress : nullptr;
    unsigned code = ep && ep->ExceptionRecord ? ep->ExceptionRecord->ExceptionCode : 0;
    HMODULE m = nullptr; wchar_t modName[MAX_PATH] = L"?";
    if (addr && GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT, (LPCWSTR)addr, &m)) GetModuleFileNameW(m, modName, MAX_PATH);
    const wchar_t* base = wcsrchr(modName, L'\\'); base = base ? base + 1 : modName;
    char b[256]; snprintf(b, sizeof b, "exception 0x%08x in AddonInitialize at %ls+0x%llx", code, base, (unsigned long long)((uintptr_t)addr - (uintptr_t)m));
    Kill(b);
    return EXCEPTION_EXECUTE_HANDLER;
}
LSPROXY_EXPORT void AddonInitialize(IHost* host, ImGuiContext* ctx, void* allocFunc, void* freeFunc, void* userData) {
    __try { AddonInitializeBody(host, ctx, allocFunc, freeFunc, userData); }
    __except (InitFilter(GetExceptionInformation())) {}
}

LSPROXY_EXPORT void AddonShutdown() {
    Log("shutting down");
    g_killed = true;
    PresentHook::Uninstall();
    DispatchHook::Uninstall();
    if (g_host) { g_host->UnsubscribeEvent(LSPROXY_EVENT_D3D11_DEVICE_READY, OnDeviceEvent); g_host->UnsubscribeEvent(LSPROXY_EVENT_D3D11_DEVICE_CHANGED, OnDeviceEvent); }
    if (g_initThread.joinable()) g_initThread.join();
    { std::lock_guard<std::mutex> lk(g_tapMu); g_bridge.Shutdown(); g_compose.Shutdown(); g_engine.Shutdown(); }
    SaveConfig();
    if (g_logFile) { fclose(g_logFile); g_logFile = nullptr; }
    g_host = nullptr;
}

LSPROXY_EXPORT uint32_t GetAddonCapabilities() { return LSPROXY_CAP_HAS_SETTINGS | LSPROXY_CAP_D3D11_DEVICE_ACCESS; }
LSPROXY_EXPORT const char* GetAddonName() { return "Neural Render (DLSS 5)"; }
LSPROXY_EXPORT const char* GetAddonVersion() { return "0.2.0"; }
LSPROXY_EXPORT const char* GetAddonAuthor() { return "andreiday"; }
LSPROXY_EXPORT const char* GetAddonDescription() { return "Runs NVIDIA DLSS 5 Neural Rendering on Lossless Scaling's real frames on the display GPU and applies the result to every presented frame, without ever making LS wait."; }
