"""Companion controls for Echo's JSON settings (values use Echo's text format)."""


def number(key, label, minimum, maximum, step="0.01", default=""):
    return dict(key=key, label=label, type="number", min=minimum, max=maximum, step=step, default=default)


def flag(key, label, default="0"):
    return dict(key=key, label=label, type="checkbox", default=default)


def choice(key, label, options):
    return dict(key=key, label=label, type="select", options=options)


PICTURE = [
    number("sharpen", "Sharpening", 0, 1, default="0.5"),
    number("saturation", "Saturation", 0, 2, default="1"),
    number("contrast", "Contrast", 0.5, 1.5, default="1"),
    number("brightness", "Brightness", -0.3, 0.3, default="0"),
    number("gamma", "Gamma", 0.5, 2, default="1"),
]

ADDONS = [
    dict(id="DLSS5NR01", name="DLSS 5 Neural Rendering",
         description="Optional, GPU-intensive neural rendering. Requires your imported DLSS 5 model. Enable only for profiles that need it.",
         fields=[number("workingScale", "Working resolution scale", 0.25, 1, default="1"),
                 number("modelEvery", "Run model every N frames", 1, 4, "1", "2"),
                 number("passes", "Model passes", 1, 4, "1", "1"),
                 number("intensity", "Effect intensity", 0, 1, default="1"),
                 flag("autoQuality", "Automatic quality", "1"),
                 number("autoBudgetMs", "Model time budget (ms)", 2, 15, "0.1", "6"),
                 number("autoFloor", "Minimum automatic resolution scale", 0.25, 1, default="0.6"),
                 flag("gpuLimit", "Limit model GPU usage"),
                 number("gpuLimitPercent", "GPU usage ceiling (%)", 80, 100, "1", "95"),
                 choice("style", "Model style", [["0", "Standard"], ["1", "Natural"], ["2", "Cinematic"]]),
                 number("localStructure", "Local structure", -10, 10, default="1"),
                 number("localTone", "Local tone", -10, 10, default="1"),
                 number("skinStructure", "Skin structure (-1: automatic)", -1, 10, default="-1"),
                 number("deltaSmooth", "Temporal smoothing", 0, 0.95, default="0.5"),
                 number("ghostGuard", "Ghosting guard", 0, 1, default="0.5"),
                 flag("useFlow", "Use motion flow", "1"),
                 flag("recordOn", "Background frame recording"),
                 number("recordSeconds", "Recording buffer (seconds)", 1, 60, "1", "5"),
                 number("recordBudgetMb", "Recording memory budget (MB)", 256, 65536, "1", "3072"),
                 *PICTURE]),
    dict(id="DLSS4DLAA", name="DLSS Upscaler / DLAA",
         description="Optional NVIDIA upscaling and anti-aliasing. Use NIS as the Lossless Scaling scaling type.", fields=PICTURE),
    dict(id="FSR3UPSC", name="AMD FSR Upscaler",
         description="Optional FSR upscaling. Use NIS as the Lossless Scaling scaling type.",
         fields=[flag("frameGen", "Experimental FSR frame generation"), *PICTURE]),
    dict(id="XESSUPSC", name="Intel XeSS Upscaler",
         description="Optional XeSS upscaling. Use NIS as the Lossless Scaling scaling type.", fields=PICTURE),
    dict(id="VSRUPSC", name="Video Super Resolution",
         description="Optional video upscaling. Availability depends on the installed manager package.", fields=[]),
    dict(id="ECHOFG", name="Echo frame generation (experimental)",
         description="Optional Echo frame generation. May conflict with native LS frame generation; keep disabled unless testing.", fields=[]),
    dict(id="GpuLimit", name="Echo GPU usage limit",
         description="Optional GPU throttling. Leave disabled when Companion controls performance and frame pacing.",
         fields=[dict(key="percent", label="GPU usage limit (%)", type="number", default="95", min=80, max=100)]),
    dict(id="Recorder", name="Echo recorder",
         description="Optional recording; disabled unless explicitly enabled.", fields=[]),
    dict(id="SuperLog", name="Echo Super log",
         description="Optional diagnostic capture. Its default hotkey is Ctrl+Shift+F11; avoid reusing that shortcut.", fields=[]),
    dict(id="WindowedMode", name="Windowed mode / second monitor",
         description="Echo's optional virtual-display feature. Applied when Lossless Scaling next starts.",
         fields=[flag("split_mode", "Split screen"),
                 choice("split_type", "Scaled half", [["0", "Left"], ["1", "Right"], ["2", "Top"], ["3", "Bottom"]]),
                 flag("position_mode", "Position beside the game"),
                 choice("position_side", "Position", [["0", "Left"], ["1", "Right"], ["2", "Above"], ["3", "Below"]])]),
    dict(id="ReShadePassthrough", name="ReShade input passthrough",
         description="Enabled automatically for profiles using ReShade. Its overlay hotkey follows General Settings.",
         fields=[flag("auto_click_repress", "Click and repeat the overlay hotkey", "1")]),
]

DEFAULT_ADDON_IDS = tuple(item["id"] for item in ADDONS)
