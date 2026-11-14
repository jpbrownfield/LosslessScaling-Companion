# Direct DLSS5 / Neural Rendering test

Build the bundled host first, then run from the repository root:

```powershell
python scripts/test_neural_render.py --output build/nr-test-001 --ls-source 'C:/path/to/LosslessScaling.exe' --model 'C:/models/nvngx_dlssnr.dll'
```

Use a new output directory on each run. The runner uses the current pinned host
source, its exact ImGui ABI, and the unchanged `DLSSNR02.dll` and NGX forwarder
from `build/native/LSAddonManager-Companion.zip`. It records their hashes. It does
not download a model, spoof the GPU vendor, alter an installed LS copy, or read
or write the user's LS settings. Native scratch files/cache/logs live beneath
the output directory; child processes have bounded lifetimes.

The available software tests run without `--model` or `--ls-source`:

* Pass invalid captures to the **real NVIDIA DLL**; require an actionable error
  while the required capture remains pending.
* Pass actual 640x360 D3D11 textures on WARP; require the explicit unsupported
  NVIDIA capture-adapter error, unchanged input/output pixels and preserved
  caller SRV/UAV bindings.
* Shut down the add-on with a capture pending, require the callback to return,
  verify registration/requirement cleanup and no surviving status reporter.
  Repeat both cases through three initialize/shutdown cycles in one process.
* Require that neither the NVIDIA driver nor the model forwarder was loaded by
  these negative tests.
* Run the native host's NVIDIA/AMD/Intel routing test, actual software D3D12
  compute flow (known eight-pixel motion and history reset), and the explicitly
  labelled copy-fixture D3D11/D3D12 shared-texture/fence test (SDR/scRGB, resize,
  ring reuse and epoch changes). The AMD bridge test performs no NR inference.

With a hardware NVIDIA adapter and `--model`, the probe invokes the real
add-on's callback on eight D3D11 frames. It checks successful processing,
observable output edits, original-frame preservation, resource binding
restoration, SDR-to-scRGB resize/epoch transitions, native processed-frame
metrics, and shutdown. `--adapter` selects a DXGI index **for this direct-frame
probe only**. A driver/model error is saved with its native status; a hung GPU
or teardown is bounded by the parent process timeout. This synthetic workload
tests functionality, not visual quality or performance.

With `--ls-source`, the runner also verifies the original `Lossless.dll` hash
and builds the pinned host source's `eam_captureprobe`. When inference and the
desktop are available, it runs the real native LS engine against that probe's
animated window using WGC, LSFG3 and no scaler. Acceptance requires more than
30 model-processed captures, actual edited images, verified read-only originals
and downstream LSFG consumers, and zero provenance failures. This probe uses
LS's default GPU selection, which must be NVIDIA; `--adapter` does not change
that LS selection. It exercises the native engine and host capture code,
independently of Companion's managed launch/profile UI. The separate overlay
test covers the managed clone and keyboard/focus path.

`results.json` distinguishes `availableTestsPassed`, `inferenceVerified` and
`captureHookVerified`. Per-phase logs and native JSON receipts retain the actual
status text and adapter identities. Exit codes:

* **0:** all available checks and both hardware inference/capture acceptance passed.
* **1:** build error, failed assertion, driver/model error, or process timeout.
* **2:** available checks passed; hardware/model/desktop/capture prerequisites missing.

The hosted Windows environment currently exposes Microsoft Basic Render Driver
adapters and denies access to the input desktop. On that environment, a green
software test does not establish working DLSS5 inference or a working LS capture
hook. The runner reports that boundary explicitly.
