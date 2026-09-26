# Changelog

## 0.3.0-lsc-hdr (2026-09-24)

- Added Lossless Scaling RGBA16F/scRGB HDR capture support.
- DLSSNR continues to receive a conservative 8-bit sRGB proxy; its edit is converted into a
  linear-light residual and applied to the original unclamped scRGB presentation frame.
- Kept the original SDR composition path unchanged and continue to reject ambiguous packed
  RGB10/PQ input.
- Added a GPU-independent CI test that compiles every runtime HLSL entry point and verifies HDR
  identity, highlight preservation, and SDR-region edits.
- The Companion build now packages this fork and installs it instead of downloading upstream
  0.2.0, and no longer forces the selected Lossless Scaling profile's HDR setting off.

## 0.2.0 (2026-09-04)

Lossless Scaling never waits for the model any more.

- Read-only tap: the addon copies the real frame and LSFG's optical flow out at LSFG's
  per-real-frame pass and never writes Lossless Scaling's textures.
- Free-running model on its own D3D12 queue. A frame is skipped when the previous run is still on
  the GPU. The model writes a work-resolution delta (model minus input) into one of three
  NT-shared slots.
- Present-time compose: a swap-chain vtable patch (Present / Present1) sees every frame Lossless
  Scaling presents; a compute pass on Lossless Scaling's own D3D11 device adds the newest finished
  delta, moved by LSFG's flow to where the content sits in that frame. Generated frames get the
  right fraction of the flow; the tap learns how many presents LS makes per real frame and
  whether the real frame comes first or last.
- Three shared fences (copyIn, done, used) keep the two queues in step with GPU-side waits only.
- Removed the inline and delayed scheduling modes, the resolve pass, the window, the masks,
  luma-only, delta smoothing and the guided upsample. All of them either put the model's time on
  LS's queue or produced visible artifacts through LSFG's interpolation.
- Panel: keep-up ratio, GPU start/done times relative to submit, tap CPU time, present pattern,
  compose count and CPU time, newest delta and its offset; debug views for original, delta x4,
  frame role and LSFG flow.
- Forwarder: calls `NVSDK_NGX_D3D12_PopulateParameters_Impl` and exposes the snippet's
  scaling-ratio callback. Harness: `--perf N` and `--perfscan`.
- Offline test host drives a real flip swap chain with an X3 present pattern and checks that the
  tap is read-only, the compose lands, and a resolution switch is followed.

## 0.1.0 (2026-09-03)

- First working addon: LosslessProxy target, ImGui settings panel, inline d3d11 Dispatch hook,
  frame tap by dispatch shape, D3D11/D3D12 bridge, DLSSNR feature 18 through the forwarder.
- Inline and async modes; working-scale cost lever; LSFG optical flow as the model's motion
  vectors.
- Standalone harness measuring the model and every knob it reads (`docs/dlssnr-knobs.md`).
