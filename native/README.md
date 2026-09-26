# Bundled native components

`reshade_bridge` is LS Companion's maintained fork of the MIT-licensed
LSP-ReShade input-passthrough add-on. It targets the LosslessProxy 0.3.x add-on
ABI vendored under `vendor/losslessproxy-sdk`.

The fork is derived from FrankBarretta/LSP-ReShade commit
`15902acd5d532ceba97345b3e318ce60d44effbc`. The SDK snapshot is derived from
FrankBarretta/LosslessProxy commit
`6ca17e1f66a37f0e0f58ee21bc1b7b622f556c23`. Both upstream projects are MIT
licensed; their notices are retained beside the source.

The bridge deliberately has no ImGui dependency. Companion owns its defaults,
and the add-on uses the stable LosslessProxy host configuration interface.

Build from an x64 MSVC developer shell:

```powershell
cmake -S native/reshade_bridge -B build/reshade_bridge -A x64
cmake --build build/reshade_bridge --config Release
```

The DLL and manifest are copied to `build/native/LSP-ReShade/`. Packaged builds
embed both files; the graphics resolver deploys them only when a profile enables
both LosslessProxy and ReShade.

`lsp_neuralrender` is an imported snapshot of andreiday/LSP-NeuralRender at
commit `750847223656d002e3a0f7090842defc2bae4077`. Its upstream MIT license and
documentation are retained in that directory. The NVIDIA NGX headers and import
library needed to build it are local-only dependencies excluded by the imported
`.gitignore`; NVIDIA runtime DLLs are not part of the source import.

Build it from an x64 MSVC developer shell after supplying the SDK files described
in `native/lsp_neuralrender/external/ngx/README.md`:

```powershell
cmake -S native/lsp_neuralrender -B build/lsp_neuralrender -A x64
cmake --build build/lsp_neuralrender --config Release
```

The Windows GitHub Actions build performs those steps without requiring a local
Visual Studio installation. It sparsely checks out the required files from the
official `NVIDIA/DLSS` repository at a pinned commit, keeps them outside release
artifacts, and uploads a separate `LSP-NeuralRender-windows-x64-*` artifact. That
artifact does not contain `nvngx_dlssnr.dll` or any neural model/runtime.
