// SPDX-License-Identifier: MIT
#include <windows.h>
extern "C" __declspec(dllexport) void ApplySettings() {}
BOOL APIENTRY DllMain(HMODULE, DWORD, LPVOID) { return TRUE; }
