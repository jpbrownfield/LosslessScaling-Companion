// Fixture only. Exercises the inspected cdecl/owned-BSTR contract; never ship.
#include <windows.h>
#include <oleauto.h>

extern "C" __declspec(dllexport) BSTR __cdecl GetAdapterNames() {
    return SysAllocString(L"NVIDIA GeForce RTX 3090;NVIDIA GeForce RTX 3090;NVIDIA GeForce RTX 5050;Test GPU \u03a9;");
}
