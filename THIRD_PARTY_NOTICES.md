# Third-party notices

## Optional Display Commander and Lilium effects

Companion can download the upstream x64 ReShade add-on from
[pmnoxx/display-commander](https://github.com/pmnoxx/display-commander),
licensed under [AGPL-3.0](https://github.com/pmnoxx/display-commander/blob/main/LICENSE).
It is not embedded in the Companion host. Its upstream source and third-party
notices remain available in that repository.

Lilium effects are acquired from
[EndlesslyFlowering/ReShade_HDR_shaders](https://github.com/EndlesslyFlowering/ReShade_HDR_shaders)
through the ReShade package catalog. Companion's input preparation is a separate
effect and does not modify the downloaded Lilium shader source.

## Private-settings bootstrap

The native folder-redirection helper statically links the BSD-2-Clause
[MinHook](https://github.com/TsudaKageyu/minhook) dependency at commit
`c3fcafdc10146beb5919319d0683e44e3c30d537`, also pinned by the addon host.
Packaged builds include its unmodified notice in
`licenses/MinHook/MinHook-LICENSE.txt`.

## Optional SM86 Super Resolution backend

Companion can download pinned release 0.4.1 directly from
[sdli1995/dlssg_for_sm86](https://github.com/sdli1995/dlssg_for_sm86/releases/tag/0.4.1)
for its L-INT8/M-INT8 SR modes. It is not embedded in Companion's host archive.
The upstream CPU host code is MIT, copyright (c) 2026 sdli1995; its NVIDIA-derived
INT8 kernel assets are binary-only and are not covered by that MIT license.
NVIDIA's unmodified SR runtime retains NVIDIA's terms. Deployment retains the
release's `LICENSE.md`, `THIRD_PARTY_NOTICES.txt` and `README.en.md` beside the
private SR files. The upstream FG/XeSS assets are not deployed by this integration.
The archive and individual file hashes are pinned in
[`sm86_sr.py`](companion/services/sm86_sr.py).

## Embedded dashboard

The Python desktop window uses [pywebview](https://github.com/r0x0r/pywebview)
(BSD-3-Clause), with Python.NET and Microsoft's WebView2 bindings. Packaged builds
include the Python distributions' license and dependency metadata. WebView2 Runtime
is a Microsoft component distributed under Microsoft's terms; the installer uses
Microsoft's signed Evergreen bootstrapper when the Runtime is absent.

## Companion Addon Host / FriedSnowman addon suite

Companion builds the pinned MIT host from
[FriedSnowman/ls-addon-manager](https://github.com/FriedSnowman/ls-addon-manager)
with a small headless/status overlay. The verified release supplies the unchanged
NR and upscaler addons. Versions and SHA-256 digests are recorded in
[`upstream.lock.json`](native/addon_manager/upstream.lock.json); our changes and
upgrade procedure are documented in [`BRIDGING.md`](native/addon_manager/BRIDGING.md).
The host and addon suite are separate components with their own licences.

The replacement Neural Rendering addon, `DLSSNR02`, is GPL-3.0-only. Its matching
`NeuralRendering-source.zip`, licence and component notices are retained beside
its binaries. The package also contains our patched host's source, build overlay,
and pinned build dependencies in `licenses/LSAddonManager/CompanionHost-source.zip`.
The user-provided proprietary `nvngx_dlssnr.dll` is never bundled.

The manager began as LosslessProxy by FrankBarretta. Attribution retained from
the upstream project:

> Copyright (c) 2025 FrankBarretta (LosslessProxy, the project this one started from)  
> Copyright (c) 2026 Echo-Storm

The addon host is distributed under the MIT License:

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

The upstream release contains separately licensed third-party components and runtimes,
including components from AMD, Intel, Microsoft, and NVIDIA. LS Companion keeps
the license and notice files shipped in the verified upstream payload. The upstream
[NOTICE](https://github.com/FriedSnowman/ls-addon-manager/blob/build-5/NOTICE.md) is
the authoritative component-level attribution and licensing inventory.

## Optional AMD Neural Rendering

Companion's separate MIT AMD addon reuses the MIT Upscalers FlowEstimator from
the pinned FriedSnowman release, with its upstream attribution and licence
included in the host source package. It does not copy the GPL Neural Rendering
renderer. The optional [RedDukeDev runtime](https://github.com/RedDukeDev/dlss5-nr-amd-custom)
is pinned separately in `native/amd_neuralrender/runtime.lock.json` and downloaded
on request. Selected files retain its MIT licence, ZLUDA/dependency notices,
README and THIRD_PARTY.md. Its C header and licence are vendored unchanged for
building the ABI bridge. No NVIDIA model is redistributed by Companion.

## py7zr

Special K's official 7-Zip download is read with unmodified
[py7zr](https://github.com/miurahr/py7zr), licensed under LGPL-2.1-or-later.
Packaged builds include its distribution metadata, full license and dependency
notices. Its source is available from the upstream repository; the dependency
range used to rebuild Companion is recorded in `companion/requirements.txt`.

