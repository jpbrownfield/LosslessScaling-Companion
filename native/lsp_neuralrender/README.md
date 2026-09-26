# LSP-NeuralRender

A [LosslessProxy](https://github.com/FrankBarretta/LosslessProxy) addon that runs NVIDIA DLSS 5
Neural Rendering (DLSSNR) on the frames Lossless Scaling captures and applies the result to every
frame Lossless Scaling presents, real and generated. Nothing is injected into the game. No ReShade.
Lossless Scaling never waits for the model.

Built for a dual-GPU setup (game on one card, Lossless Scaling and this addon on the other), and it
also works on a single GPU and on hybrid laptops. The model runs on whichever NVIDIA GPU Lossless
Scaling uses for LSFG.

- **Read-only tap.** The addon copies each new real frame out of LSFG's pipeline together with
  LSFG's own optical flow. Lossless Scaling's frame is never written.
- **Free-running model.** DLSSNR runs on its own D3D12 queue and produces a *delta* (model minus
  input). If a run is still on the GPU when the next frame arrives, that frame is skipped.
- **Present-time compose.** The newest finished delta is added to every frame Lossless Scaling
  presents, moved by LSFG's flow to where the content sits in that frame. A slow model costs
  freshness of the enhancement, never frame rate or latency.

Documentation: [User guide](docs/user-guide.md) · [Architecture](docs/architecture.md) ·
[Building](docs/building.md) · [What the model listens to](docs/dlssnr-knobs.md) ·
[Changelog](CHANGELOG.md)

## What you need

- Lossless Scaling 3.x with [LosslessProxy](https://github.com/FrankBarretta/LosslessProxy)
  installed and working.
- An NVIDIA RTX GPU running LSFG. RTX 30 was the development hardware (Ampere runs the model in
  FP16, so it is the slow case). RTX 20 should behave the same. RTX 40/50 are expected to be
  considerably faster but were not tested by the author.
- The DLSSNR snippet, `nvngx_dlssnr.dll`. NVIDIA has not released it publicly. This project does
  not ship it, link to it, or help you find it. Put your copy in the Lossless Scaling folder (or
  point the addon at it in *Advanced*).

## Install

1. Download the latest release zip and extract it. Copy the `addons\LSP-NeuralRender` folder into
   your Lossless Scaling folder so you have:
   ```
   <Lossless Scaling>\addons\LSP-NeuralRender\LSP_NeuralRender.dll
   <Lossless Scaling>\addons\LSP-NeuralRender\nvngx.dll_lspnr.dll
   <Lossless Scaling>\addons\LSP-NeuralRender\addon.json
   ```
2. Put `nvngx_dlssnr.dll` in the Lossless Scaling folder (next to `LosslessScaling.exe`).
3. Start Lossless Scaling, open the LosslessProxy addon manager, enable *Neural Render (DLSS 5)*
   and open its settings.
4. Start scaling a game. The status line goes from *waiting for LSFG dispatches* to *engine:
   loading model...* to *running*. The first model load takes a few seconds.

The addon logs to `<Lossless Scaling>\LSP_NeuralRender.log`. The [user guide](docs/user-guide.md)
walks through every setting, how to pick the working scale, and what each status and log line means.

## Cost

On an RTX 3090 a model run costs about 10 ms plus 7 ms per megapixel of model input, whatever the
pixels are. The *Working scale* is the only cost lever: the frame is shrunk by it before the model
sees it, and the delta is upsampled back at present time. At 5120x1440 a working scale of 0.35
gives a 14 to 17 ms run, which keeps up with a 48 fps game. At a working scale past the frame
interval the model skips frames and the previous delta is carried forward by the flow; the panel
shows how many frames the model keeps up with.

Companion profiles expose Performance (25%), Balanced (35%), Quality (50%), Full (100%), and
Custom sampling presets. Custom preserves later changes made directly in the add-on menu.

Because the model shares the GPU with LSFG, a heavy model run can still delay LSFG's own work on
the same card. *LS's GPU work first* raises Lossless Scaling's GPU priority so LSFG's passes and
presents pre-empt the model.

## How it works

```
capture k ─┬─ LSFG pyramid pass on frame k            <- TAP: copy frame k and LSFG's flow to the model's
           │                                             input, start a model run (skipped if the previous
           │                                             run is still on the GPU). LS's frame is never written.
           ├─ LSFG flow and interpolation passes
           └─ Present: generated a, generated b, real k  <- PRESENT HOOK: newest finished delta + this frame,
                                                           warped by the flow to where the content sits
```

An inline hook on d3d11's `Dispatch` recognises LSFG's per-real-frame pass by shape. NT-shared
textures and three shared fences connect Lossless Scaling's D3D11 device with the model's D3D12
queue, with GPU-side waits only. A patch on the swap chain's `Present` vtable slots sees every
presented frame; a compute pass on Lossless Scaling's own device adds the delta. The full design,
including why each hook is the kind it is, is in [docs/architecture.md](docs/architecture.md).

## Limitations

- RGBA8/BGRA8 SDR and RGBA16F scRGB HDR frames are supported. In HDR, DLSSNR processes an
  SDR/sRGB proxy and its edit is converted back to a linear-light residual. The original scRGB
  frame remains the base, so values above SDR white are not clipped. This is HDR-preserving
  composition, not native full-range HDR inference by the unpublished model.
- 10-bit packed HDR/PQ input is rejected because the add-on cannot reliably infer its transfer
  function from the texture format alone.
- The 310.8 DLSSNR build ignores depth and does no upsampling of its own; the addon feeds LSFG's
  optical flow as motion vectors and does the scaling itself.
- Ampere runs the model in FP16; expect the working scale to sit between 0.3 and 0.5 there.
- Only one build of the DLSSNR snippet creates its feature on Ampere. If the engine reports
  `FeatureNotSupported` at `CreateFeature`, you have the other one.

## Building

Visual Studio 2022 Build Tools, CMake 3.20+, and the NVIDIA DLSS SDK dropped into `external/ngx`
(see `external/ngx/README.md`). ImGui and MinHook are fetched by CMake. `build.bat` builds
everything; `tools\package_release.ps1` builds and zips a release. Details, the offline test host
and the measurement harness are in [docs/building.md](docs/building.md).

## License and credits

MIT (see `LICENSE`). Third parties: LosslessProxy SDK headers (MIT, `external/lsproxy-sdk`),
Dear ImGui (MIT), MinHook (BSD-2). The NVIDIA DLSS SDK and the DLSSNR snippet are NVIDIA's and
are not part of this repository.

DLSSNR is an NVIDIA technology. This addon runs NVIDIA's model on hardware and in a way NVIDIA did
not release it for. Use it at your own risk with respect to NVIDIA's terms.
