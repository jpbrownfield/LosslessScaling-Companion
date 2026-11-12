"""Apply the small Companion overlay to an otherwise unmodified pinned checkout."""
from pathlib import Path
import argparse
import shutil


def replace_once(path, before, after):
    text = path.read_text(encoding="utf-8")
    if text.count(before) != 1:
        raise RuntimeError(f"Upstream drift in {path}: expected exactly one overlay anchor")
    path.write_text(text.replace(before, after), encoding="utf-8", newline="\n")


def patch_managed_startup(main):
    legacy = 'eam::GuiManager::StartGuiThread(addons.get(), [] { D3D11Hook::InstallDispatchHooks(); });'
    current = ('eam::GuiManager::StartGuiThread(addons.get(), [lossless] {\n'
               '        D3D11Hook::InstallDispatchHooks();\n'
               '        eam::features::scaling::InstallHooks(lossless);\n'
               '    });')
    text = main.read_text(encoding="utf-8")
    if text.count(legacy) + text.count(current) != 1:
        raise RuntimeError(f"Upstream drift in {main}: expected one known GUI startup sequence")
    before = current if current in text else legacy
    setup = ', [lossless] { eam::features::scaling::InstallHooks(lossless); }' if before == current else ''
    replace_once(main, before,
        'if (!lsc::StartManagedHost(addons.get(), [](eam::AddonManager* manager) { '
        'eam::GuiManager::StartGuiThread(manager); }' + setup + '))\n        ' + before)


def prepare(source: Path):
    overlay = Path(__file__).resolve().parent
    manager = source / "manager"
    host_h = manager / "src/host/host_impl.h"
    host_cpp = manager / "src/host/host_impl.cpp"
    replace_once(host_h, 'private:\n', 'private:\n    friend struct CompanionRoutingTest;\n')
    replace_once(host_h, 'bool RealFrameRequired() const { return m_realRequired.load(std::memory_order_acquire); }',
                 'bool RealFrameRequired() const;\n    unsigned SelectRealFrameAdapter(void* device);')
    replace_once(host_h, 'Hook<EamRealFrameCallback> m_realHook{};',
                 'Hook<EamRealFrameCallback> m_realHook{}, m_amdRealHook{};\n'
                 '    std::atomic<void*> m_amdRealModule{nullptr};\n'
                 '    std::atomic<bool> m_amdRealRequired{false};\n'
                 '    std::atomic<bool> m_nativeNr{false};\n'
                 '    std::atomic<uint32_t> m_boundRealVendor{0};\n'
                 '    std::atomic<uint32_t> m_realVendor{0};')
    replace_once(host_cpp, '#include "host_impl.h"',
                 '#include "host_impl.h"\n#include "../core/companion_bridge.h"\n'
                 '#include <d3d11.h>\n#include <dxgi.h>\n#include <wrl/client.h>')
    text = host_cpp.read_text(encoding="utf-8")
    begin = text.index('bool HostImpl::SetRealFrameCallback(')
    end = text.index('// ---- live status and metrics', begin)
    replace_once(host_cpp, text[begin:end], '#include "../core/companion_real_frame.inc"\n\n')
    replace_once(host_cpp, '    for (auto& hook : m_preHooks) {',
                 '    for (auto& hook : m_preHooks) {\n        if(!lsc::AllowDispatchModule(hook.module))continue;')
    replace_once(host_cpp, '        if (!CallPost(hook.callback, x, y, z, hook.owner))',
                 '        if (lsc::AllowDispatchModule(hook.module) && !CallPost(hook.callback, x, y, z, hook.owner))')
    replace_once(host_cpp, 'm_realHook = {}; m_realModule.store(nullptr); m_realRequired.store(false); m_realCount.store(0);',
                 'm_realHook = {}; m_realModule.store(nullptr); m_realRequired.store(false); m_realCount.store(m_amdRealHook.callback ? 1 : 0);')
    replace_once(host_cpp, '    return removed;\n}',
                 '    {\n        std::lock_guard<std::mutex> lock(m_realMutex);\n'
                 '        if (inside(m_amdRealHook)) {\n'
                 '            m_amdRealHook={};m_amdRealModule.store(nullptr);m_amdRealRequired.store(false);\n'
                 '            m_realCount.store(m_realHook.callback ? 1 : 0);++removed;\n'
                 '        }\n    }\n    return removed;\n}')
    # Resolve from the dispatching context before the upstream required-NR gate.
    # Cache per thread/context: no adapter enumeration on every compute pass.
    capture = manager / "src/core/real_frame_hook.cpp"
    replace_once(capture, '    if(!host->HasRealFrameCallback())return;',
                 '    if(!host->HasRealFrameCallback())return;\n'
                 '    thread_local Microsoft::WRL::ComPtr<ID3D11DeviceContext> routedContext;\n'
                 '    thread_local ComPtr<ID3D11Device> routedDevice;\n'
                 '    if(routedContext.Get()!=ctx) {\n'
                 '        ctx->GetDevice(routedDevice.ReleaseAndGetAddressOf());routedContext=ctx;\n    }\n'
                 '    host->SelectRealFrameAdapter(routedDevice.Get());')
    main = manager / "src/core/main.cpp"
    replace_once(main, '#include "../gui/gui_manager.h"',
                 '#include "../gui/gui_manager.h"\n#include "companion_bridge.h"')
    patch_managed_startup(main)
    replace_once(main, 'DWORD reason, LPVOID)', 'DWORD reason, LPVOID reserved)')
    replace_once(main, 'g_manager->Stop();\n        g_manager.reset();',
                 '// Managed modules are pinned. Process teardown must not join workers\n'
                 '        // or destroy GPU resources while Windows holds the loader lock.\n'
                 '        if (reserved && lsc::IsManagedHost()) { g_manager.release(); return TRUE; }\n'
                 '        g_manager->Stop();\n        g_manager.reset();')
    # Companion shows the existing HWND from its own shortcuts. Keep the GUI's
    # hidden flag in sync so an external ShowWindow wakes its blocked render loop.
    replace_once(manager / "src/gui/gui_manager.cpp", '    case WM_SIZE:',
                 '    case WM_SHOWWINDOW:\n        g.hidden = !wParam;\n        break;\n\n    case WM_SIZE:')
    # Companion ships a patched host. Its own updater must remain the source of
    # updates, and upstream's consent prompt must not reveal the hidden panel.
    updates = manager / "src/gui/widgets/update_notice.cpp"
    if updates.is_file():
        managed = ('ConfigManager::Instance().GlobalGetOr<bool>("companion", "managed", false) || '
                   'ConfigManager::Instance().GlobalGetOr<bool>("companion", "headless", false)')
        replace_once(updates, 'void InitializeUpdateCheck(updates::Checker& checker) {',
                     'void InitializeUpdateCheck(updates::Checker& checker) {\n'
                     f'    if ({managed}) {{ checker.SetPolicy(updates::Policy::Disabled); return; }}')
        replace_once(updates, 'void RenderUpdateSettings(updates::Checker& checker) {',
                     'void RenderUpdateSettings(updates::Checker& checker) {\n'
                     f'    if ({managed}) {{ ImGui::TextDisabled("Updates are provided by Lossless Scaling Companion."); return; }}')
    dispatch = manager / "src/core/d3d11_hook.cpp"
    replace_once(dispatch, '#include "d3d11_hook.h"', '#include "d3d11_hook.h"\n#include "hdr_probe.h"')
    replace_once(dispatch, 'if (t_dispatching || !ctx || !IsLossless(ctx))',
                 'if (lsc::IsHdrProbeDispatch() || t_dispatching || !ctx || !IsLossless(ctx))')
    for name in ("companion_bridge.h", "companion_bridge.cpp", "component_loader.h", "component_loader.cpp", "hdr_probe.h", "hdr_probe.cpp", "companion_real_frame.inc", "headless_smoke.cpp", "fake_engine.cpp", "routing_fixture.cpp", "routing_test.cpp"):
        shutil.copy2(overlay / name, manager / "src/core" / name)
    with (manager / "CMakeLists.txt").open("a", encoding="utf-8") as output:
        output.write('\n# Companion overlay: rendering sources and SDK are unchanged.\n'
                     'target_sources(Lossless PRIVATE src/core/companion_bridge.cpp src/core/component_loader.cpp src/core/hdr_probe.cpp)\n'
                     'foreach(test_target eam_uipreview eam_coretest eam_sampletest eam_guitest eam_captureprobe)\n'
                     '  target_sources(${test_target} PRIVATE src/core/companion_bridge.cpp src/core/component_loader.cpp src/core/hdr_probe.cpp)\n'
                     '  target_compile_definitions(${test_target} PRIVATE LSC_HOST_VERSION="${LSC_HOST_VERSION}")\n'
                     'endforeach()\n'
                     'target_compile_definitions(Lossless PRIVATE LSC_HOST_VERSION="${LSC_HOST_VERSION}")\n'
                     'target_compile_options(Lossless PRIVATE /MP4)\n'
                     'set_property(TARGET Lossless PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")\n'
                     'add_executable(lsc_headless_smoke src/core/headless_smoke.cpp)\n'
                     'add_library(lsc_fake_engine SHARED src/core/fake_engine.cpp)\n'
                     'set_target_properties(lsc_fake_engine PROPERTIES OUTPUT_NAME "Lossless_original")\n'
                     'get_target_property(routing_sources eam_coretest SOURCES)\n'
                     'list(REMOVE_ITEM routing_sources tools/core_test.cpp)\n'
                     'add_executable(lsc_routing_test ${routing_sources} src/core/routing_test.cpp)\n'
                     'get_target_property(routing_includes eam_coretest INCLUDE_DIRECTORIES)\n'
                     'target_include_directories(lsc_routing_test PRIVATE ${routing_includes})\n'
                     'target_compile_definitions(lsc_routing_test PRIVATE LSC_HOST_VERSION="${LSC_HOST_VERSION}")\n'
                     'target_compile_options(lsc_routing_test PRIVATE /MP4)\n'
                     'target_link_libraries(lsc_routing_test d3d11 dxgi d3dcompiler bcrypt minhook)\n'
                     'foreach(vendor DLSSNR02 LSC_AMDNR)\n'
                     '  add_library(lsc_fixture_${vendor} SHARED src/core/routing_fixture.cpp)\n'
                     '  target_include_directories(lsc_fixture_${vendor} PRIVATE sdk/include)\n'
                     '  set_target_properties(lsc_fixture_${vendor} PROPERTIES OUTPUT_NAME ${vendor} RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_BINARY_DIR}/Release/routing-fixtures")\n'
                     '  add_dependencies(lsc_routing_test lsc_fixture_${vendor})\n'
                     'endforeach()\n')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    prepare(parser.parse_args().source)
