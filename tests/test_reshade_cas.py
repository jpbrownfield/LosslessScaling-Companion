import ctypes
import io
import os
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from companion.core.profile_manager import ProfileManager
from companion.services.reshade_cas import AMD_CAS_EFFECT, AMD_CAS_SOURCE
from companion.services.reshade_profiles import BUILTIN_PROFILES, ReshadeProfileService


class DefaultPresetPreparationTests(unittest.TestCase):
    def test_preparation_downloads_all_defaults_once_and_keeps_amd_cas_bundled(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("hdr-master/Shaders/lilium__inverse_tone_mapping.fx",
                             "technique lilium__inverse_tone_mapping { pass {} }")
            package.writestr("hdr-master/Shaders/lilium__include/include_main.fxh", "// support")
        with tempfile.TemporaryDirectory() as directory:
            service = ReshadeProfileService(ProfileManager(Path(directory)))
            service._catalog = [{"id": "22", "name": "Lilium", "shaders": ["lilium__inverse_tone_mapping.fx"],
                "downloadUrl": "https://github.com/example/hdr/archive/master.zip"}]
            with patch("companion.services.reshade_profiles.urllib.request.urlopen",
                       return_value=io.BytesIO(archive.getvalue())) as download:
                payload = service.prepare_builtin_profiles()
                service.prepare_builtin_profiles()
            self.assertEqual(download.call_count, 1)
            self.assertEqual(len(payload["profiles"]), 3)
            for profile in BUILTIN_PROFILES:
                self.assertEqual(service._missing_selected_shaders(profile), set())
                self.assertTrue((service._folder(profile.id) / "ReShade.ini").is_file())
            basic = service._folder("lsc-basic-sharpening") / "packages/lsc" / AMD_CAS_EFFECT
            self.assertEqual(basic.read_text(encoding="utf-8"), AMD_CAS_SOURCE)

    def test_failed_download_does_not_report_defaults_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ReshadeProfileService(ProfileManager(Path(directory)))
            service._catalog = [{"id": "22", "name": "Lilium", "shaders": ["lilium__inverse_tone_mapping.fx"],
                "downloadUrl": "https://github.com/example/hdr/archive/master.zip"}]
            with patch("companion.services.reshade_profiles.urllib.request.urlopen", side_effect=OSError("offline")):
                with self.assertRaisesRegex(ValueError, "default presets could not be prepared"):
                    service.prepare_builtin_profiles()
            self.assertEqual(service._missing_selected_shaders(BUILTIN_PROFILES[0]), set())


@unittest.skipUnless(os.name == "nt", "Requires the Windows D3D shader compiler")
class AmdCasShaderCompileTests(unittest.TestCase):
    def test_shader_math_compiles_for_sdr_scrgb_hdr10_and_unknown_float(self):
        # Adapt only ReShade's effect declarations and fetch intrinsic to HLSL;
        # compile the actual shipped vertex/pixel functions with Microsoft's FXC.
        source = re.sub(r"uniform float (\w+) <.*?> = ([^;]+);",
                        r"static const float \1 = \2;", AMD_CAS_SOURCE, flags=re.S)
        source = source.replace("texture CasBackBuffer : COLOR;", "Texture2D<float4> CasBackBuffer;")
        source = source.replace("sampler CasSampler { Texture = CasBackBuffer; };", "")
        source = re.sub(r"tex2Dfetch\(CasSampler, (\w+)\)", r"CasBackBuffer.Load(int3(\1, 0))", source)
        source = source[:source.index("technique LSC_AMD_CAS")]
        compile_shader = ctypes.WinDLL("d3dcompiler_47").D3DCompile
        compile_shader.restype = ctypes.c_long
        compile_shader.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
        disassemble = ctypes.WinDLL("d3dcompiler_47").D3DDisassemble
        disassemble.restype = ctypes.c_long
        disassemble.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint,
                               ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]

        def blob_bytes(blob):
            table = ctypes.cast(blob, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            pointer = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(table[3])(blob)
            size = ctypes.WINFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p)(table[4])(blob)
            return ctypes.string_at(pointer, size)

        def release(blob):
            if blob:
                table = ctypes.cast(blob, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])(blob)

        for colour_space, depth in ((1, 8), (2, 16), (3, 10), (0, 16)):
            for entry, target in ((b"CasVS", b"vs_5_0"), (b"CasPS", b"ps_5_0")):
                with self.subTest(colour_space=colour_space, entry=entry):
                    data = (f"#define BUFFER_COLOR_SPACE {colour_space}\n#define BUFFER_COLOR_BIT_DEPTH {depth}\n"
                            "#define BUFFER_WIDTH 3840\n#define BUFFER_HEIGHT 2160\n" + source).encode()
                    code, errors = ctypes.c_void_p(), ctypes.c_void_p()
                    result = compile_shader(data, len(data), b"LSC_AMD_CAS.fx", None, None,
                        entry, target, 0, 0, ctypes.byref(code), ctypes.byref(errors))
                    try:
                        message = blob_bytes(errors).decode(errors="replace") if errors else ""
                        self.assertGreaterEqual(result, 0, message)
                        if result >= 0 and entry == b"CasPS":
                            bytecode = blob_bytes(code)
                            assembly = ctypes.c_void_p()
                            try:
                                self.assertGreaterEqual(disassemble(bytecode, len(bytecode), 0, None, ctypes.byref(assembly)), 0)
                                instructions = blob_bytes(assembly).decode()
                                self.assertEqual(len(re.findall(r"^\s*ld_indexable\(", instructions, re.M)), 5,
                                                 "Sharpening must compile to exactly five texture loads")
                            finally:
                                release(assembly)
                    finally:
                        release(code)
                        release(errors)
