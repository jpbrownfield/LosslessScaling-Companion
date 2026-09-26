# User guide

This is the long version of the README's install section: what to expect on first run, what
every setting does, how to pick the working scale, and what to do when something is off.

## What this addon is, and is not

It runs NVIDIA's DLSS 5 Neural Rendering model on the frames Lossless Scaling has already
captured, inside the Lossless Scaling process, on the GPU that runs LSFG. It never touches the
game: no files in the game folder, no hooks in the game process, nothing for an anti-cheat to see.
The game does not need DLSS, motion vectors or any particular API.

It is not DLSS upscaling. The 310.8 DLSSNR build does no upsampling and ignores depth. It is a
style and detail model that edits a finished image, and the addon feeds it the image Lossless
Scaling is about to show.

## Requirements

- Lossless Scaling 3.x and a working LosslessProxy install (the addon manager opens, other addons
  load).
- An NVIDIA RTX GPU for LSFG. On a dual-GPU rig that is the display card; the game GPU can be
  anything. RTX 30 was the development hardware; RTX 40/50 should be faster but were not tested.
- `nvngx_dlssnr.dll`, the DLSSNR snippet. Not included, not linked, not distributed by this
  project. Two builds of version 310.8.0 circulate; only one of them creates its feature on
  Ampere (see *Engine errors* below).

## Install

1. Extract the release zip. Copy `addons\LSP-NeuralRender` into the Lossless Scaling folder, so the
   folder holds `LSP_NeuralRender.dll`, `nvngx.dll_lspnr.dll` and `addon.json`.
2. Put `nvngx_dlssnr.dll` next to `LosslessScaling.exe`. If you keep it elsewhere, set its full
   path under *Advanced > Snippet path* later.
3. Start Lossless Scaling, open the LosslessProxy addon manager and enable *Neural Render
   (DLSS 5)*.

To update, replace the two DLLs while Lossless Scaling is closed. To uninstall, delete the folder.
Settings live in LosslessProxy's `addons\config.json` under the key `LSP-NeuralRender`; delete
that block to reset them.

## First run

Start scaling a game with LSFG on and open the addon's settings. The status line at the top
tells you where the addon is:

| Status | Meaning |
|---|---|
| waiting for device | Lossless Scaling has not created its D3D11 device yet. Start scaling. |
| waiting for LSFG dispatches | Device seen, no LSFG passes yet. Scaling must be active. |
| engine: loading model... | The model is being created on the GPU that issued the LSFG pass. A few seconds. |
| running | The model runs and the compose lands on presented frames. |
| unsupported frame format | The captured frame is not RGBA/BGRA SDR or RGBA16F scRGB HDR. Packed 10-bit/PQ input is not accepted. |
| DISABLED: ... | The addon switched itself off. See *Troubleshooting*. *Re-arm* turns it back on. |

Below the status line, *frame* shows the captured size and format, *NR* the model time of the
last run and its average, *taps* how many real frames were seen, and the d3d11 hook line how many
entry points are hooked (5 on current Windows builds).

The log at `<Lossless Scaling>\LSP_NeuralRender.log` is rewritten on every start. Every 300
frames it writes one line with the model time, how far behind the submit the GPU started and
finished, the frame interval, how many runs were made and skipped, the present pattern, and the
newest delta's frame and offset. NGX's own logs land in the addon folder.

## Settings

Everything applies on the next frame. Only *Working scale* re-creates the model's feature, which
takes a moment and resets its temporal history.

### Model

| Control | Default | What it does |
|---|---|---|
| Style | Standard | The model's three looks: Standard, Natural, Cinematic. |
| Intensity | 1.00 | How much of the model's edit is produced. 0 is a bit-exact passthrough. The model clamps at 1. |
| Local structure | 1.00 | Fine detail strength. Unclamped: values above ~5 degrade, negatives invert the edit. |
| Local tone | 1.00 | Local contrast strength. Same range behaviour as local structure. |
| Skin structure | follow local structure | Detail strength where the model thinks there is skin. -1 follows *Local structure*. |
| Auto skin mask | on | The model's own skin detection. |
| LSFG optical flow | on | Feeds LSFG's flow to the model as motion vectors, and uses it at present time to move the delta onto generated frames. Turn it off only to diagnose the flow; the delta then stays put on generated frames. |

What each of these measurably does, knob by knob, is in [dlssnr-knobs.md](dlssnr-knobs.md).

### Pipeline

| Control | Default | What it does |
|---|---|---|
| Sampling resolution | 35% | The frame is shrunk per dimension before the model sees it. The only cost lever. Companion profiles offer Performance (25%), Balanced (35%), Quality (50%), Full (100%), and Custom. Custom leaves the value under control of this add-on menu. |
| LS's GPU work first | on | Raises Lossless Scaling's D3D11 device to GPU thread priority +7 so LSFG's own passes and presents pre-empt the model on the shared GPU. Leave it on unless you are measuring. |
| Apply strength | 1.00 | How much of the delta is added at present time. Above 1 exaggerates the model's edit. |
| Max delta | 0.50 | Clamp on the per-channel delta (0..1 scale). Limits how far one pixel may move. |
| Protect highlights above | 0.85 | Fades the delta out as the source luminance rises from here to white. Keeps the model from crushing bright areas. 1.00 turns it off. |
| Debug view | Result | *Original* shows the frame untouched, *Delta x4* the delta amplified, *Frame role* tints real frames green and generated frames red, *LSFG flow* shows the flow field. |

For scRGB HDR input, the model intentionally sees only an SDR/sRGB proxy. Its edit is decoded to
a linear-light residual and added to the original FP16 frame without clamping the result. This
keeps HDR highlights and gamut excursions intact; it does not make the unpublished model itself
HDR-aware. Leave *Protect highlights above* at its default for the safest HDR result.

The lines above the controls are live: the model input size in megapixels, the measured model
time next to the estimate (10 ms + 7 ms per megapixel on Ampere), the frame interval, how many
frames the model keeps up with, how long after the submit the GPU started and finished, and how
many presents were composed with which pattern. A warning appears when the model runs on fewer
than half of the frames.

### Choosing the sampling resolution

1. Note the frame interval (the game's frame time as Lossless Scaling sees it).
2. Raise *Sampling resolution* until the model time approaches the interval. The keep-up line should stay
   near 100%.
3. If the game or LSFG lose smoothness, back off one step. The model shares the GPU with LSFG;
   even with the priority raised, a run that fills the whole interval leaves LSFG less room.

A model that is late does not slow anything down. The present side keeps using the newest
finished delta, moved by the flow, so the enhancement lags the image by a frame or two. That is
the trade past the interval: freshness, not frame rate.

### Tap

Which of Lossless Scaling's compute passes is treated as "a new real frame arrived". *Auto* picks
the pass that reads a full-size colour texture and writes only smaller outputs (LSFG's pyramid
pass), and it re-learns after a resolution change. The table lists every dispatch shape seen, with
its count and views. *Manual* with the *TAP* button pins a row; *Clear roles* goes back to auto.
*Frame slot* overrides which of the pass's input textures is taken as the frame.

You should not need this unless a future Lossless Scaling version changes its pipeline.

### Advanced

| Control | Default | What it does |
|---|---|---|
| Watchdog (ms) | 80 | If the model takes longer than this for 30 frames in a row the addon disables itself. Raise it if you deliberately run a large working scale. |
| Snippet path | blank | Full path to `nvngx_dlssnr.dll`. Blank means the Lossless Scaling folder. |
| Restart engine | | Tears the model down and starts it again on the current LSFG adapter. |
| Dump flow probe | | Diagnostic: writes the next three tapped frames and LSFG's flow textures to the Lossless Scaling folder. |

## Troubleshooting

**The status stays at "waiting for LSFG dispatches".** Frame generation must be on in Lossless
Scaling and the game must be scaling. The addon only sees compute dispatches; with LSFG off there
is nothing to tap.

**"DISABLED: NR slower than watchdog threshold for 30 frames".** The working scale is too high
for the GPU. Lower it, press *Re-arm*, or raise the watchdog.

**"DISABLED: could not hook d3d11 Dispatch" or "could not hook dxgi Present".** Another tool in
the process replaced the hook points in a way the addon cannot chain to. Check the log for which
step failed. Overlays that hook Present (the NVIDIA overlay, RTSS) are expected and work; the
addon uses the swap chain vtable precisely so they do.

**Engine errors** (red line in the panel and `NrEngine FAILED:` in the log):

| Message | Cause |
|---|---|
| snippet probe 0x... | `nvngx_dlssnr.dll` was not found or is not a DLSSNR snippet. Check the path. |
| snippet Init_Ext: ... | NGX refused to initialise on this adapter. Virtual display adapters and non-NVIDIA cards do this. |
| CreateFeature(18): FeatureNotSupported | This snippet build does not create its feature on your GPU. On Ampere, only one of the two circulating 310.8.0 builds works. |
| adapter LUID not found | The GPU that ran LSFG disappeared (device change). The engine restarts on the next tap. |

**Presents are counted but nothing is composed.** Look at the log line at tap 60: *present
stages* shows how far each present gets. If *targeted* is zero the tap has not seen a real frame
yet; if *with delta* is zero the model has not finished a run.

**The image morphs or ghosts on generated frames.** Turn on *Debug view > LSFG flow* to confirm
the flow is being seen (the panel shows its size next to the flow checkbox). If it reads "no flow
texture seen yet", the tap did not identify LSFG's flow pass; try *Clear table* and let it
re-learn.

**The picture looks over-sharpened or noisy.** Lower *Local structure* first, then *Max delta*.
*Intensity* scales the whole edit.

**Lossless Scaling itself got choppy.** Check *LS's GPU work first* is on. Then lower the working
scale: even a prioritised model run competes for the same GPU, and on a single-GPU rig it competes
with the game as well.

## Dual-GPU notes

The addon follows the adapter that runs LSFG. Set Lossless Scaling's *Preferred GPU* to the display
card; the model then runs there and the game GPU is untouched. If Lossless Scaling briefly runs
passes on both cards during a device change, the engine waits until one adapter keeps issuing the
tap before it moves.
