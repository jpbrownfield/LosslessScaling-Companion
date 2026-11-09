"""Keep the public C ABI unchanged; select compute from its existing D3D12 device."""
from pathlib import Path
import argparse
import shutil


def replace_once(path, before, after):
    text = path.read_text(encoding="utf-8")
    if text.count(before) != 1:
        raise RuntimeError(f"AMD runtime upstream drift in {path}: expected one exact anchor")
    path.write_text(text.replace(before, after), encoding="utf-8", newline="\n")


def prepare(source):
    core = source / "runtime/core"
    shutil.copy2(Path(__file__).with_name("companion_adapter.h"), core / "companion_adapter.h")
    replace_once(core / "network.h", "struct NetworkPaths {",
        "struct NetworkPaths {\n    uint64_t adapter_luid = 0;\n    bool require_adapter = false;")
    replace_once(core / "runtime.cpp", "    c->device = info->device;",
        "    c->device = info->device;\n"
        "    const auto adapter = info->device->GetAdapterLuid();\n"
        "    c->paths.adapter_luid = (uint64_t(uint32_t(adapter.HighPart)) << 32) | adapter.LowPart;\n"
        "    c->paths.require_adapter = true;")
    replace_once(core / "upscaler.cpp", "    paths.kind = u.kind;",
        "    paths.kind = u.kind;\n"
        "    const auto adapter = info.device->GetAdapterLuid();\n"
        "    paths.adapter_luid = (uint64_t(uint32_t(adapter.HighPart)) << 32) | adapter.LowPart;\n"
        "    paths.require_adapter = true;")
    cpp = core / "network.cpp"
    replace_once(cpp, '#include "network.h"', '#include "network.h"\n#include "companion_adapter.h"')
    replace_once(cpp, '    bool ready = false;     // the part above exists',
        '    uint64_t adapter_luid = 0;\n    bool ready = false;     // the part above exists')
    replace_once(cpp,
        '            CUdevice device = 0;\n'
        '            if (!cu_ok(g.cu.cuDeviceGet(&device, 0), "cuDeviceGet", error)) return false;',
        '            CUdevice device = 0;\n'
        '            if (paths.require_adapter) {\n'
        '                auto getLuid = reinterpret_cast<int (*)(char*, unsigned*, int)>(GetProcAddress(g.gate, "cuDeviceGetLuid"));\n'
        '                if (!getLuid) { error = "The AMD driver cannot expose adapter LUIDs"; return false; }\n'
        '                lsc_amd::AdapterSelection selected;\n'
        '                if (!lsc_amd::SelectAdapter(paths.adapter_luid, g.cu.cuDeviceGetCount, g.cu.cuDeviceGet, getLuid, selected, error)) return false;\n'
        '                device = selected.device;\n'
        '                g.adapter_luid = paths.adapter_luid;\n'
        '                lsc_amd::selectedOrdinal.store(selected.ordinal);\n'
        '                lsc_amd::selectedLuid.store(g.adapter_luid);\n'
        '                log(2, "Companion selected CUDA ordinal %d for capture LUID %016llx", selected.ordinal, (unsigned long long)g.adapter_luid);\n'
        '            } else if (!cu_ok(g.cu.cuDeviceGet(&device, 0), "cuDeviceGet", error)) return false;')
    replace_once(cpp, '        ++g.users;\n',
        '        if (paths.require_adapter && g.adapter_luid != paths.adapter_luid) {\n'
        '            error = "Compute adapter changed; restart Lossless Scaling to recreate its context";\n'
        '            return false;\n'
        '        }\n        ++g.users;\n')


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    prepare(parser.parse_args().source)
