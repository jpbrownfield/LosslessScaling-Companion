// SPDX-License-Identifier: MIT
#pragma once
#include <windows.h>
#include <cstdint>
namespace lsc::cursor {
using Replace = BOOL (WINAPI*)(HCURSOR, DWORD);
using Load = HCURSOR (WINAPI*)(HINSTANCE, LPCWSTR);
// Kept separate so tests can verify forwarding without changing desktop cursors.
BOOL ReplaceSafely(HCURSOR cursor, DWORD id, Replace original, Load load);
bool Install(HMODULE engine);
bool Ready();
uint64_t Prevented();
DWORD Error();
}
