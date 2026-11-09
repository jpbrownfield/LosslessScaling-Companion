// SPDX-License-Identifier: MIT
// Synthetic loader only. It never starts capture or loads a proprietary model.
#include <windows.h>
#include <cstdio>
#include <cwchar>
#include <string>
int main() {
    HMODULE module=LoadLibraryW(L"Lossless.dll");
    if (!module) { std::fprintf(stderr,"LoadLibrary: %lu\n",GetLastError());return 1; }
    std::puts("loaded");std::fflush(stdout);
    // Optional loader-route probe, driven after the bridge reports readiness.
    // It loads a runtime DLL but never creates/evaluates a GPU feature.
    if (std::getchar()=='r') {
        wchar_t exe[32768]{};GetModuleFileNameW(nullptr,exe,32768);
        wchar_t* slash=wcsrchr(exe,L'\\');if (!slash) return 2;slash[1]=0;
        std::wstring path=std::wstring(exe)+L"addons\\DLSS4DLAA\\dlss\\nvngx_dlss.dll";
        HMODULE runtime=LoadLibraryExW(path.c_str(),nullptr,LOAD_WITH_ALTERED_SEARCH_PATH);
        if (runtime) { wchar_t chosen[32768]{};GetModuleFileNameW(runtime,chosen,32768);std::wprintf(L"runtime=%ls\n",chosen); }
        else std::printf("runtime_error=%lu\n",GetLastError());
        std::fflush(stdout);std::getchar();
    }
    return 0;
}
