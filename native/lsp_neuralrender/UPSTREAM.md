# Upstream provenance

- Project: `andreiday/LSP-NeuralRender`
- Source: <https://gitlab.com/andreiday/LSP-NeuralRender>
- Imported commit: `750847223656d002e3a0f7090842defc2bae4077`
- Imported: 2026-09-24
- Upstream license: MIT (`LICENSE`)

The source tree was imported without its nested Git metadata. The original
license, documentation, LosslessProxy SDK notices, and build files are retained.

The NVIDIA NGX SDK is a build dependency with separate NVIDIA licensing. Local
copies under `external/ngx/include` and `external/ngx/lib` are intentionally
ignored and must not be committed or packaged as project source. Likewise,
`nvngx_dlssnr.dll` and other community runtime binaries are not part of this
source import.

## Lossless Scaling Companion fork

The local `0.3.0-lsc-hdr` fork accepts Lossless Scaling's RGBA16F/scRGB capture path. It keeps the
unpublished DLSSNR model in an SDR/sRGB proxy domain and applies the model result as a bounded
linear-light residual to the original unclamped HDR frame. This is an independently maintained
compatibility change and is not represented as an upstream or NVIDIA implementation.
