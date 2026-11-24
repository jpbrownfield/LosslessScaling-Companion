"""Reviewed GPL NR recovery changes applied to the pinned corresponding source."""


def patch_nr(source, replace_once, overlay):
    nr = source / "addons/NeuralRendering"
    status_header = source / "manager/sdk/include/eam/real_frame_status.h"
    text = status_header.read_text(encoding="utf-8")
    if text.count("Capture paused:") != 6:
        raise RuntimeError("Upstream drift in NR support guidance")
    status_header.write_text(text.replace("Capture paused:", "NR bypassed; original frames continue:")
                             .replace("disable NR to continue", "NR is bypassed; original frames continue"),
                             encoding="utf-8", newline="\n")
    service = nr / "src/service.cpp"
    text = service.read_text(encoding="utf-8")
    begin = text.index("            // Keep this capture pending.")
    end = text.index("        }", begin)
    replace_once(service, text[begin:end],
                 "            // Preserve the original capture on failure; retry remains explicit.\n"
                 "            return false;\n")
    replace_once(service,
                 '            snapshot_.status = "Invalid real capture; processing paused. Disable NR to continue.";\n            continue;',
                 '            snapshot_.status = "Invalid real capture";\n            return false;')
    # Both synchronous processing errors and asynchronous initialization errors
    # retain their status. Neither may wait for a UI action on LS's render thread.
    text = service.read_text(encoding="utf-8")
    before = '; processing paused. Retry initialization or disable NR.'
    if text.count(before) != 2:
        raise RuntimeError("Upstream drift in NR failure messages")
    service.write_text(text.replace(before, '; NR bypassed. Retry initialization or disable NR.'), encoding="utf-8", newline="\n")
    replace_once(service, '    return false; // Only an explicit disable/shutdown permits an original handoff.',
                 '    return false; // Disabled or failed NR leaves the original capture intact.')
    pipeline = nr / "src/pipeline.cpp"
    replace_once(pipeline, '#include "pipeline.h"', '#include "pipeline.h"\n#include <filesystem>')
    replace_once(pipeline, '    Mark(Stage::Resources);',
                 '    // Diagnose missing input before allocating GPU resources or loading drivers.\n'
                 '    const auto model = std::filesystem::absolute(s.modelPath);\n'
                 '    if (s.modelPath.empty() || !std::filesystem::is_regular_file(model))\n'
                 '        throw std::runtime_error("User-provided nvngx_dlssnr.dll was not found: " + model.u8string());\n'
                 '    Mark(Stage::Resources);')
    addon = nr / "src/addon.cpp"
    replace_once(addon, '    settings.modelPath = Wide(modelPath);',
                 '    if (std::string(modelPath).find_first_not_of(" \\t\\r\\n") == std::string::npos) {\n'
                 '        wchar_t exe[32768]{};GetModuleFileNameW(nullptr,exe,32768);\n'
                 '        const auto path=Utf8((std::filesystem::path(exe).parent_path()/L"nvngx_dlssnr.dll").wstring());\n'
                 '        snprintf(modelPath,sizeof(modelPath),"%s",path.c_str());\n'
                 '    }\n    settings.modelPath = Wide(modelPath);')
    replace_once(addon, 'snapshot.status.find("; processing paused.")', 'snapshot.status.find("; NR bypassed.")')
    replace_once(addon,
                 '                          : blockedProfile ? support\n'
                 '                          : snapshot.failed ? "Capture paused: " + failure + ". " + neural::FailureGuidance(snapshot.status) + ". Disable NR to continue"',
                 '                          : snapshot.failed ? "NR unavailable: " + failure + ". " + neural::FailureGuidance(snapshot.status) + ". Original frames are continuing; retry NR after correcting the error"\n'
                 '                          : blockedProfile ? support')
    replace_once(addon, '0 : blockedProfile ? 2 : snapshot.failed ? 3', '0 : snapshot.failed ? 3 : blockedProfile ? 2')
    replace_once(addon, ': blockedProfile ? support : snapshot.failed ? snapshot.status', ': snapshot.failed ? snapshot.status : blockedProfile ? support')
    replace_once(addon, '    if (blockedProfile && snapshot.failed) message += "\\nPrevious pipeline error: " + snapshot.status;\n', '')
    capture = source / "manager/src/core/real_frame_hook.cpp"
    replace_once(capture, '    while(host->RealFrameRequired())Sleep(25);\n', '')
    replace_once(capture, '        if(!host->RealFrameRequired())return;\n        Sleep(25);',
                 '        return; // Unsupported NR must not block ordinary LS compute work.')
    replace_once(capture, '        PauseUnsupported(host,EamFrameSupport::UnrecognizedShader,"No bytecode identity on the LS compute shader; restart LS and check hook installation");',
                 '        // ReShade and other unrelated compute passes may have no LS identity.\n'
                 '        // Skip them; only a positively identified capture belongs to NR.')
    test = nr / "tests/status_test.cpp"
    replace_once(test, '#include <windows.h>', '#include <windows.h>\n#include <d3d11.h>\n#include <wrl/client.h>')
    replace_once(test, '    uint32_t api=0x010400;bool conflict=false;',
                 '    uint32_t api=0x010400;bool conflict=false;bool blankModel=false;')
    replace_once(test, 'const char* GetConfig(const char*,const char*,const char* d) override{return d;}',
                 'const char* GetConfig(const char*,const char* key,const char* d) override {\n'
                 '        if(std::string(key)=="modelPath")return blankModel?"":"LSC-intentionally-missing-nr-runtime.dll";\n'
                 '        return d;\n    }')
    replace_once(test, '    check("first-frame failure visible while callback is blocked",', '    check("first-frame failure visible without Settings",')
    replace_once(test, '    check("failure does not bypass required frame",pending.wait_for(std::chrono::milliseconds(50))==std::future_status::timeout);',
                 '    check("invalid frame returns without blocking render",pending.wait_for(std::chrono::milliseconds(500))==std::future_status::ready);')
    replace_once(test, '    check("shutdown releases blocked callback",!pending.get());',
                 '    check("invalid frame preserves original",!pending.get());')
    replace_once(test, '    host.api=0x010300;',
                 '    #include "companion_recovery_test.inc"\n    host.api=0x010300;')
    import shutil
    shutil.copy2(overlay / "nr_recovery_test.inc", nr / "tests/companion_recovery_test.inc")
    with (nr / "CMakeLists.txt").open("a", encoding="utf-8") as output:
        output.write('\nif(TARGET neural_statustest)\n  target_link_libraries(neural_statustest PRIVATE d3d11)\nendif()\n')
    pipeline_test = nr / "tests/pipeline_test.cpp"
    replace_once(pipeline_test,
                 'pending.wait_for(std::chrono::milliseconds(50)) != std::future_status::timeout',
                 'pending.wait_for(std::chrono::seconds(2)) != std::future_status::ready')
    replace_once(pipeline_test, 'Initialization failure did not pause capture', 'Initialization failure blocked capture')
    replace_once(pipeline_test, 'PASS failure pauses capture:', 'PASS failure preserves original capture:')
    replace_once(pipeline_test, '                settings.modelPath = std::filesystem::absolute(argv[1]).wstring();\n                service.Configure(settings, true, true);\n                if (!pending.get()) throw std::runtime_error("Retry bypassed the waiting capture");\n                Read(device.Get(), context.Get(), output.Get());',
                 '                if (pending.get()) throw std::runtime_error("Missing model unexpectedly transformed capture");\n'
                 '                settings.modelPath = std::filesystem::absolute(argv[1]).wstring();\n'
                 '                service.Configure(settings, true, true);')
