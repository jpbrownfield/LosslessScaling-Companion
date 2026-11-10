#pragma once
#include <windows.h>
#include "../../third_party/nlohmann/json.hpp"

namespace lsc { nlohmann::json ComponentStatus(); }
extern "C" __declspec(dllexport) DWORD WINAPI LSCInitializeComponents();
