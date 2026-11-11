"""FriedSnowman build-5 settings. Values use the upstream text representation."""


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
    dict(id="DLSSNR02", name="DLSS 5 Neural Rendering",
         description="Enhances real captures before LSFG. The capture GPU selects native NVIDIA NR or the separate AMD backend automatically. Requires LS 3.2.2.0, WGC and your imported model. AMD additionally needs the optional runtime and compatible Windows HIP hardware; use FSR, XeSS or NIS for scaling. Use LSFG 3 or FG Off; FG Off requires Scaling Off or NIS. NVIDIA model errors pause capture; AMD errors preserve the original capture. NVIDIA flow/HDR/picture controls do not apply to AMD; AMD uses compute flow, automatic exposure and ratio composition. AMD limits: 8 passes, intensity/structure/tone 1, detail 2.",
         fields=[flag("enabled", "Process captured frames", "1"),
                 number("scalePercent", "Working resolution (%)", 25, 100, "1", "100"),
                 choice("preset", "Model", [["0", "Default"], ["1", "Original (A)"], ["2", "Fast (B)"], ["3", "Preset 3"]]),
                 number("passes", "Model passes (1 is fastest)", 1, 10, "1", "1"),
                 choice("flowGrid", "Optical flow grid (pixels)", [["1", "1"], ["2", "2"], ["4", "4 (default)"]]),
                 choice("flowQuality", "Optical flow quality", [["1", "Slow / quality"], ["2", "Medium (default)"], ["3", "Fast"]]),
                 number("intensity", "Effect intensity", 0, 2, default="1"),
                 choice("style", "Model style", [["0", "Standard"], ["1", "Natural"], ["2", "Cinematic"]]),
                 number("localStructure", "Local structure", 0, 2, default="0.8"),
                 number("localTone", "Local tone", 0, 2, default="0.3"),
                 number("skinStructure", "Skin structure (-1: automatic)", -1, 2, default="-1"),
                 flag("autoMask", "Automatic skin detection", "1"),
                 number("detail", "Composition detail", 0, 4, default="1"),
                 number("colour", "Composition colour", 0, 4, default="1"),
                 number("maxRatio", "Maximum luminance ratio", 1, 8, default="2"),
                 number("whiteNits", "scRGB reference white (nits)", 80, 1000, "1", "203"),
                 choice("proxy", "HDR proxy", [["0", "Knee"], ["1", "Neutwo composed"], ["2", "Neutwo replace"], ["3", "Hybrid composed"], ["4", "Hybrid replace"]]),
                 choice("reconstruction", "Reconstruction", [["0", "Classic"], ["1", "Current-frame residual"]]),
                 flag("apply", "Apply model result (comparison still runs NR)", "1"),
                 flag("amdWait", "AMD: wait for same-frame inference (higher latency)"),
                 flag("amdHistory", "AMD: experimental network history"),
                 number("amdMaxAge", "AMD: discard results older than (frames)", 1, 120, "1", "4"),
                 number("amdBlendFrames", "AMD: result blend (frames)", 0, 8, "1", "0"),
                 dict(key="modelPath", label="NVIDIA model DLL path (blank uses imported model)", type="text"),
                 dict(key="amdModelPath", label="AMD model DLL path (original signed 310.9.16; blank uses imported model)", type="text"),
                 flag("pictureEnabled", "Enable picture adjustments"),
                 number("pictureExposure", "Picture exposure (stops)", -4, 4, default="0"),
                 *[number("picture" + key, "Picture " + key.lower(), -2, 2, default="0")
                   for key in ("Contrast", "Midtones", "Shadows", "Highlights", "Temperature", "Tint")],
                 number("pictureSaturation", "Picture saturation", 0, 4, default="1"),
                 number("pictureVibrance", "Picture vibrance", -1, 2, default="0"),
                 number("pictureSharpen", "Picture sharpening", 0, 4, default="0")]),
    dict(id="DLSS4DLAA", name="DLSS Upscaler / DLAA",
         description="Optional NVIDIA upscaling and anti-aliasing. Use NIS as the Lossless Scaling scaling type. SM86 L/M INT8 requires the optional backend in Add-ons and an RTX 30-series GPU. It pins the matching preset; gains versus the same FP16 preset need measuring, especially at DLAA/Quality.",
         fields=[choice("dlaaPreset", "DLSS upscaling preset", [["16", "Auto (E for DLAA, L for upscaling)"],
                     ["0", "NVIDIA default (K)"], ["5", "E (CNN)"], ["10", "J"], ["12", "L"], ["13", "M"]]),
                 choice("companionSm86Mode", "SM86 optimized Super Resolution", [["Off", "Off — NVIDIA runtime"],
                     ["L-INT8", "L-INT8 (RTX 30 series)"], ["M-INT8", "M-INT8 (RTX 30 series)"]]), *PICTURE]),
    dict(id="FSR3UPSC", name="AMD FSR Upscaler",
         description="Optional FSR upscaling. Use NIS as the Lossless Scaling scaling type.",
         fields=[flag("frameGen", "Experimental FSR frame generation"), *PICTURE]),
    dict(id="XESSUPSC", name="Intel XeSS Upscaler",
         description="Optional XeSS upscaling. Use NIS as the Lossless Scaling scaling type.", fields=PICTURE),
    dict(id="WindowedMode", name="Windowed mode / second monitor",
         description="Optional virtual-display feature. Applied when Lossless Scaling next starts.",
         fields=[flag("split_mode", "Split screen"),
                 choice("split_type", "Scaled half", [["0", "Left"], ["1", "Right"], ["2", "Top"], ["3", "Bottom"]]),
                 flag("position_mode", "Position beside the game"),
                 choice("position_side", "Position", [["0", "Left"], ["1", "Right"], ["2", "Above"], ["3", "Below"]])]),
    dict(id="ReShadePassthrough", name="Overlay toggle",
         description="Enabled automatically while ReShade is in use, including Display Commander without a shader preset. Its overlay hotkey follows General Settings.",
         fields=[flag("auto_click_repress", "Click and repeat the overlay hotkey", "1")]),
]

DEFAULT_ADDON_IDS = tuple(item["id"] for item in ADDONS)

# Managed controls stay in the schema so older saved values never reappear as
# unknown fields. Apply these defaults when materializing every profile, too.
MANAGED_DEFAULTS = {
    "DLSSNR02": {"enabled": "1", "apply": "1", "autoMask": "1",
                 "amdWait": "0", "amdHistory": "0", "amdBlendFrames": "0"},
    "FSR3UPSC": {"frameGen": "0"},  # LS owns frame generation.
    "ReShadePassthrough": {"auto_click_repress": "1"},
}

PRESENTATION = {
    "DLSSNR02": dict(
        summary="Enhance captured frames before frame generation. Enabling selects WGC, upgrades LSFG 1/2 to LSFG 3, and uses NIS for incompatible scalers when FG is off. An installed model is attached automatically. The scaling GPU selects the NVIDIA or AMD backend.",
        category="Enhancement",
        groups=[
            dict(label="Quality & performance", keys=["scalePercent", "preset", "passes", "intensity", "style"], expanded=True),
            dict(label="Model tuning", keys=["localStructure", "localTone", "skinStructure", "detail", "colour", "maxRatio"]),
            dict(label="NVIDIA optical flow", vendor="NVIDIA", keys=["flowGrid", "flowQuality"]),
            dict(label="NVIDIA HDR", vendor="NVIDIA", keys=["whiteNits", "proxy", "reconstruction"]),
            dict(label="NVIDIA picture adjustments", vendor="NVIDIA", keys=["pictureEnabled", "pictureExposure", "pictureContrast", "pictureMidtones", "pictureShadows", "pictureHighlights", "pictureTemperature", "pictureTint", "pictureSaturation", "pictureVibrance", "pictureSharpen"]),
            dict(label="AMD inference", vendor="AMD", keys=["amdMaxAge", "amdModelPath"]),
            dict(label="NVIDIA model location", vendor="NVIDIA", keys=["modelPath"]),
        ],
    ),
    "DLSS4DLAA": dict(summary="NVIDIA upscaling and anti-aliasing. Stays in sync with this profile’s scaling mode.", category="Upscaling", vendor="NVIDIA",
        groups=[dict(label="DLSS model", keys=["dlaaPreset", "companionSm86Mode"], expanded=True),
                dict(label="Picture adjustments", keys=[f["key"] for f in PICTURE])]),
    "FSR3UPSC": dict(summary="FSR upscaling. Stays in sync with this profile’s scaling mode.", category="Upscaling",
        groups=[dict(label="Picture adjustments", keys=[f["key"] for f in PICTURE], expanded=True)]),
    "XESSUPSC": dict(summary="XeSS upscaling, with independent picture controls.", category="Upscaling",
        groups=[dict(label="Picture adjustments", keys=[f["key"] for f in PICTURE], expanded=True)]),
    "WindowedMode": dict(summary="Position the scaled output beside the game or on part of a display.", category="Display"),
    "ReShadePassthrough": dict(summary="Lets mouse and keyboard input reach the ReShade menu. Enabled automatically while ReShade is in use.", category="Input"),
}
for addon in ADDONS:
    addon.update(PRESENTATION.get(addon["id"], {}))
    for field in addon["fields"]:
        if field["key"] in MANAGED_DEFAULTS.get(addon["id"], {}):
            field.update(hidden=True, default=MANAGED_DEFAULTS[addon["id"]][field["key"]])
