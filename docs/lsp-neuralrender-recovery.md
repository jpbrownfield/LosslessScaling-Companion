# LSP-NeuralRender source recovery

Recovery performed on 2026-09-24 for the experimental HDR investigation.

## Recovered upstream

The complete MIT-licensed `andreiday/LSP-NeuralRender` repository was recovered
from GitLab after its archive endpoint temporarily returned HTTP 503. The full
14-commit history and the `v0.2.0` tag were also fetched.

- Imported source: `native/lsp_neuralrender`
- Upstream commit: `750847223656d002e3a0f7090842defc2bae4077`
- License: MIT; the upstream notice is retained
- Integrity: every imported upstream-tracked file was SHA-256 compared with the
  recovered commit before modification

The imported source already contains the add-on, D3D11/D3D12 bridge, LSFG frame
tap, present hook, shaders, NGX forwarder, standalone harness, synthetic host
test, documentation, and packaging script.

## Local build dependency

The official NVIDIA/DLSS repository was recovered at commit
`374959484e79a640feaba44c93ac8cfb0a03f5b5` (DLSS 310.9.1). The six headers and
x64 static NGX import library required by upstream were copied into
`native/lsp_neuralrender/external/ngx`. They are excluded by the imported
`.gitignore` and must not be committed or packaged as project source.

No `nvngx_dlssnr.dll`, community-modified runtime, model, or other unofficial
NVIDIA binary was added to the source tree.

## Related source surveyed

These repositories were downloaded into the ignored `.tmp/neural-recovery`
workspace for comparison and are not vendored into the application:

| Project | Commit | Relevant material |
|---|---|---|
| `NIGos/dlss5-bridge` | `d1cc508a7097534c5c0e01a868ebe3b6657b932b` | HDR contract detection, FP16 transport, HDR10 PQ/linear conversion, NGX diagnostics |
| `Pizzawookiee/DLSS5-Feeder` | `aa6d3d876eea118be2146bd5b80b07721502b158` | D3D11/D3D12 shared textures, HDR feature flags, format-aware copy-back |
| `MotionflowOffical/UniversalDLSS5` | `e47518a74ff539df2a16c71f8aa5c0b7038e8c5a` | Feature-18 hosting and fallback paths |
| `2600th/dlss5-video-player` | `13f92b74f1e82b5802d9d60aba953234f4e53dba` | Standalone neural-runtime host and validation infrastructure |
| `FrankBarretta/LosslessProxy` | `6ca17e1f66a37f0e0f58ee21bc1b7b622f556c23` | Current proxy host implementation and add-on ABI |

No independent mirror of LSP-NeuralRender was found in the broad web/code-index
search. Because the authoritative repository became reachable again, no binary
decompilation or cached-file reconstruction was necessary.

## HDR implementation boundary

The recovered source confirms that HDR rejection is deliberate and localized:

- `FrameTap` recognizes `R16G16B16A16_FLOAT` as a possible color frame.
- `Bridge::ViewFormat` maps FP16, but `Bridge::FormatSupported` allows only
  8-bit RGBA/BGRA input.
- The model proxy is explicitly `R8G8B8A8_UNORM`.
- `Compose11` uses `saturate(frame + delta)`, which clips scRGB HDR values.

An HDR-capable fork therefore needs format-aware FP16 transport, a defined HDR
to model-domain conversion, an HDR-safe delta mapping, and composition without
clamping the original HDR signal to `[0, 1]`. The comparison repositories contain
working examples for the transport and color-space portions, but their licenses
and attribution must be retained if code is incorporated.

## Remaining build prerequisite

This machine did not have CMake or the Visual Studio MSVC toolchain discoverable
during recovery. Local building requires Visual Studio 2022 Build Tools with the
x64 C++ workload, Windows SDK, and CMake. Local installation is optional: the
Windows GitHub Actions workflow uses its hosted MSVC environment, sparsely fetches
the pinned official NGX dependency, builds the recovered source, and publishes a
separate native artifact without the SDK or community runtime.

The first Companion-maintained release is `0.3.0-lsc-hdr`. Its HDR path is deliberately
conservative: DLSSNR operates on an SDR/sRGB proxy, while the result is applied as a linear-light
residual to the original FP16 scRGB frame. This avoids clipping HDR output without claiming that
the recovered proprietary model performs native HDR inference.
