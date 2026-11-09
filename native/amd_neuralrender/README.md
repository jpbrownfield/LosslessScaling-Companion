# Companion AMD Neural Rendering addon

This is a separate API 1.4 real-frame transformer, `LSC_AMDNR.dll`. Companion's
host routes verified captures by the capture device's DXGI vendor: NVIDIA uses
the unchanged `DLSSNR02.dll`; AMD uses this addon. Other vendors are unsupported.
The existing profile Neural Rendering switch owns both registrations. There is
no global vendor selector and no inference on an unrelated GPU elsewhere in the
system. Changing GPU vendor in a running LS process requires restarting LS.

## Runtime and model

Install the bundled Companion Addon Host, then download **AMD Neural Rendering**
in Add-ons. Import your NR model through the existing model importer (or set
`DLSSNR02.amdModelPath`) and apply the profile. This AMD path has its own model
override so a NVIDIA-specific patched model path can remain separate. The host still requires verified
LS 3.2.2.0, WGC, and the upstream capture/scaler/LSFG combinations. The GPU must
also support the runtime's Windows HIP/ZLUDA path and shipped kernels/cache.
Install the matching Windows HIP runtime separately. A model accepted by the
NVIDIA path is not automatically guaranteed to match this AMD runtime; upstream
documents the original signed NR model 310.9.16.0. A model patched for older
NVIDIA cards cannot be translated by this backend; point the AMD override at the
original model instead. The runtime reports cache/model failures through status.
Companion checks the model's pinned SHA-256 before any driver/model loading;
different model builds require a reviewed runtime/cache update.

`runtime.lock.json` pins RedDukeDev/dlss5-nr-amd-custom **1.3.2**, its source
commit, source/release digests, C header and every selected file. Companion
builds the adapter-selecting runtime from that source and bundles it under
`addons/LSC-AMDNR/runtime/`. The 547 MB optional download supplies its unchanged
ZLUDA/NGX dependencies, NR kernels, cache and notices under
`companion-runtimes/AMDNR/1.3.2/`. Its original runtime DLL is not deployed.
Companion does not deploy the release's game proxies,
OptiScaler, DXVK, executables, NVIDIA model DLLs or FG snippet. The runtime's
shared compatibility libraries retain their upstream implementations.

The immutable package's precompiled ComputeCache is seeded once into
`<Companion config>/runtime-cache/AMDNR/1.3.2/`. Its mutable DB and logs never
become LS deployment-owned files or modify the content-addressed package.
Missing runtime/model/HIP, unsupported hardware and pipeline failures are
reported; the AMD path preserves the unenhanced capture. It never presents a
loaded DLL as proof of successful inference.

## GPU path and settings

The addon creates D3D12 on the **same DXGI adapter** as the capture D3D11 device.
Companion's existing adapter map selects Lossless Scaling's GPU. Its confirmed
capture-device LUID is passed through the runtime's existing D3D12 device
argument. Our small source patch enumerates HIP/CUDA devices, finds that same
LUID, and passes the matching device token to CUDA context creation. It selects
non-primary GPUs explicitly; an LS/DXGI index is never treated as a CUDA index.
The pinned driver exposes this LUID through HIP device properties. The resulting
binding is verified after startup and the selected CUDA ordinal is reported.
An adapter absent from HIP remains unsupported; selecting another available GPU
does not require editing visibility environment variables.
Two shared textures and shared GPU fences carry input/output between APIs. An
allocator ring prevents reuse before completion. The steady frame path queues
GPU waits; it does not read frame pixels on the CPU. CPU waits are limited to
allocator reuse/resize and published as `cpu_wait_ms`.
Companion explicitly disables the runtime's host-memory-copy and on-demand
translation modes. HIP_PATH is used to locate the installed HIP 7 DLL when its
bin folder is absent from the current process's PATH.

Motion estimation uses FriedSnowman's **MIT Upscalers FlowEstimator**, recorded
on our D3D12 list. It produces full-resolution current-to-previous pixel motion.
It does not use NVIDIA Optical Flow, LSFG's private vectors or OptiScaler's
game hooks. Resize, capture gaps and epoch changes reset flow/history. LS
capture supplies no scene depth; the runtime receives null depth rather than
fabricated depth, so temporal reconstruction cannot match a game integration
with genuine depth and engine vectors. The runtime decides what history is
usable with these inputs.

The runtime records its own capture/residual composition. We submit our list,
call `dlss5nr_present` on the same queue, then queue D3D11's output wait/copy.
AMD defaults to asynchronous inference with motion-tracked residuals and a
four-frame freshness limit. **AMD: wait for same-frame inference** enables the
runtime's experimental blocking mode, at a substantial latency/frame-rate cost.
Network history and result blending are separate profile settings.

Common scale/model/style/intensity/structure/tone/detail/colour/pass controls
map to the C API, clamped to its documented limits (8 passes, intensity/tone/
structure 1, detail 2). Apply-off comparisons continue evaluating but retain the
original output. NVIDIA flow grid/quality, picture controls, white-point/HDR
proxy and reconstruction choices do not control this backend. AMD uses automatic
exposure/encoding and ratio composition. Ported DLSS SR/RR is **not implemented**;
choose FSR, XeSS or native NIS with AMD NR. LS continues to own frame generation.

Status exposes result count, inference evaluations/s, evaluation ms, result age
and latency, native kernel count, compute-flow GPU ms and CPU wait ms. Async
network throughput and flow timing overlap; they must not be summed into a
claimed total frame cost. Benchmark image quality, pacing and end-to-end
latency on real hardware before making performance claims.

## Tests and the Ryzen 7800X3D

The 7800X3D has a small two-core integrated Radeon GPU. It can exercise D3D11/
D3D12 capture routing, shared textures and compute flow with AMD's display
driver and the iGPU enabled in firmware. It is absent from AMD's supported
Windows HIP GPU list, so successful NR inference is not an acceptance criterion
for that CPU. The shipped generic RDNA cache does not establish HIP support.
Sources: [CPU specifications](https://www.amd.com/en/products/processors/desktops/ryzen/7000-series/amd-ryzen-7-7800X3d.html),
[Windows HIP requirements](https://rocm.docs.amd.com/projects/install-on-windows/en/latest/reference/system-requirements.html).

After building, list adapters and test the iGPU without a model or HIP:

```powershell
build/native/LSC-AMDNR/LSC_AMDNRProbe.exe --list
build/native/LSC-AMDNR/LSC_AMDNRProbe.exe --check-model C:/models/nvngx_dlssnr.dll
build/native/LSC-AMDNR/LSC_AMDNRProbe.exe --adapter 0 --flow-only
build/native/LSC-AMDNR/LSC_AMDNRProbe.exe --adapter 0 --bridge-only
```

Use the index of the integrated Radeon from `--list`; these are DXGI probe
indices, not LS dropdown numbers. In Companion, select that Radeon as the
profile's scaling GPU and check the runtime status says AMD during capture.
`--flow-only` checks a known moving pattern and directly reads reset vectors.
`--bridge-only` uses a **GPU-copy test fixture**, never a model, to verify shared
textures/fences, ring reuse, resize and epoch reset. The fixture is deliberately
outside the deployable runtime directory. `--warp` substitutes software D3D for
CI only; the real addon does not fall back to software.

```powershell
$env:LSC_TEST_ADDON_HOST='1'
python -m pytest tests/test_addon_host.py tests/test_amd_nr.py
```

The host tests exercise both registration orders, NVIDIA/AMD/Intel routing,
exclusive ownership for unrelated transformers, requirement isolation, vendor
switch rejection and unloading one callback without deleting the other.
The compute selection test uses deliberately different LS/map identities, CUDA
ordinals and device tokens, and verifies that GPUs 1 and 2 are actively selected.
None of these tests proves AMD network inference, HDR image quality or speed.
Hardware acceptance still needs supported AMD hardware plus the matching NR
model: SDR/scRGB, real LSFG on/off, FSR/XeSS, ReShade, async/same-frame mode,
missing dependencies, device loss, size changes and profile restart/recovery.

## Upstream updates

Keep the FriedSnowman host lock and this runtime lock independent. Review the
new C ABI, settings limits, cache targets, ZLUDA dependencies and notices before
updating this lock; regenerate file hashes from the verified release. Vendor
headers stay byte-identical to the pin. `prepare_runtime.py` applies a small,
exact-anchor patch to internal NetworkPaths/context creation; it does not alter
the public C ABI. The complete patched runtime source is in the host source ZIP.
Rebuild via
`python scripts/build_addon_manager.py`; run routing, bridge, flow and hardware
tests. See [host bridging notes](../addon_manager/BRIDGING.md) for exact anchors.
